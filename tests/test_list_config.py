# SPDX-License-Identifier: AGPL-3.0-or-later
"""The list DEFINITIONS: naming a list, pointing it somewhere, and taking it away (#475).

Membership is somebody else's data mirrored into ``cache.db`` and rebuilt on every sync. A
definition is not rebuildable from anything, so it lives in ``reaper.db`` and is migrated and
backed up. These pin the boundary between the two, and every refusal on the way in.

**Everything here fails closed toward keeping.** A configuration that could never match
anything is refused while the operator is looking at the box that is empty, rather than
syncing to empty and sitting on the screen reading "Nothing on it". Removing a list withdraws
a protection, so the API pairs the delete with ``list_rules.detach_list`` -- the pairing is
pinned in ``tests/test_list_rules.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.session import create_engine, create_session_factory
from reaper.main import create_app
from reaper.services import list_config
from reaper.services.lists import ListSource
from tests._auth import login


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


@pytest.fixture
async def session(tmp_path: Path) -> Iterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    sync = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(sync)
    sync.dispose()
    engine = create_engine(settings)
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


class TestWhatShipsWithReaper:
    async def test_the_first_read_seeds_the_two_default_lists(self, session: AsyncSession) -> None:
        """The two lists the default policy's keep rules name, so the screen is never
        empty on a fresh install and those rules never point at nothing (rule 25)."""
        rows = await list_config.all_lists(session)

        assert [(r.name, r.source) for r in rows] == [
            ("IMDb Top 250", "imdb"),
            ("Titles you've tagged", "arr_tag"),
        ]
        assert json.loads(rows[0].config_json) == {"preset": "top250"}
        assert json.loads(rows[1].config_json) == {"tags": ["reaper-keep"], "match": "any"}

    async def test_reading_twice_does_not_seed_twice(self, session: AsyncSession) -> None:
        await list_config.all_lists(session)
        rows = await list_config.all_lists(session)

        assert len(rows) == 2

    async def test_a_deleted_seeded_list_is_not_resurrected(self, session: AsyncSession) -> None:
        """The seed runs exactly once, tracked by a flag rather than by the rows: an
        operator who removed a shipped list must not find it back on the next read."""
        rows = await list_config.all_lists(session)
        await list_config.delete(session, rows[0].id)

        names = [r.name for r in await list_config.all_lists(session)]

        assert names == ["Titles you've tagged"]

    async def test_a_renamed_seeded_list_keeps_its_new_name(self, session: AsyncSession) -> None:
        """Same flag, other direction: a rename must not spawn a second shipped copy
        beside the operator's."""
        [imdb, _tags] = await list_config.all_lists(session)
        await list_config.update(session, imdb.id, name="Films worth keeping")

        names = [r.name for r in await list_config.all_lists(session)]

        assert names == ["Films worth keeping", "Titles you've tagged"]


class TestRefusingAConfigurationThatCouldNeverMatch:
    """Rule 108's shape one level up: a list saved with no collection or no tags syncs to
    empty and then reads as "Nothing on it", which is indistinguishable from a collection the
    operator has not filled in yet. The refusal names the box while they are looking at it."""

    async def test_a_plex_list_needs_a_library(self, session: AsyncSession) -> None:
        with pytest.raises(list_config.ListConfigError, match="which Plex library"):
            await list_config.create(
                session,
                name="Keep",
                source="plex_collection",
                config={"collection": "Never Reap"},
            )

    async def test_a_plex_list_needs_a_collection(self, session: AsyncSession) -> None:
        with pytest.raises(list_config.ListConfigError, match="which collection"):
            await list_config.create(
                session, name="Keep", source="plex_collection", config={"library": "Films"}
            )

    async def test_a_tag_list_needs_at_least_one_tag(self, session: AsyncSession) -> None:
        with pytest.raises(list_config.ListConfigError, match="at least one tag"):
            await list_config.create(session, name="Keep", source="arr_tag", config={"tags": []})

    async def test_whitespace_is_not_a_tag(self, session: AsyncSession) -> None:
        """A tag that strips to nothing would be looked up as "" and never resolve, which
        ``ArrTagRule`` reads as a MISSING container and fails the whole list over."""
        with pytest.raises(list_config.ListConfigError, match="at least one tag"):
            await list_config.create(
                session, name="Keep", source="arr_tag", config={"tags": ["  ", ""]}
            )

    @pytest.mark.parametrize(
        ("saved", "stored"),
        [
            (["keep", "keep"], ["keep"]),
            (["Keep", "keep"], ["Keep"]),
            (["keep", "KEEP", "gold"], ["keep", "gold"]),
            (["Keep", " keep "], ["Keep"]),
        ],
    )
    async def test_one_tag_is_stored_once_however_it_is_capitalized(
        self, session: AsyncSession, saved: list[str], stored: list[str]
    ) -> None:
        """Sonarr and Radarr lower-case every label, so two spellings are one tag upstream.
        Stored twice they collapsed to one tag id at fetch time and only the later spelling
        was counted, so the Lists screen showed a chip reading zero for a tag protecting
        everything it names (#509). The first spelling is the one kept: it is what the
        operator typed before the duplicate."""
        row = await list_config.create(
            session, name=f"Keep {'-'.join(saved)}", source="arr_tag", config={"tags": saved}
        )

        assert json.loads(row.config_json)["tags"] == stored

    async def test_a_stored_duplicate_reads_as_one_tag(self, session: AsyncSession) -> None:
        """The read side too, so a body saved before the check above existed corrects itself
        rather than waiting for the operator to re-save a list to fix a number."""
        row = await list_config.create(
            session, name="Keep", source="arr_tag", config={"tags": ["Keep"]}
        )
        row.config_json = json.dumps({"tags": ["Keep", "keep", "KEEP"], "match": "all"})
        await session.commit()

        [definition] = [d for d in await list_config.definitions(session) if d.id == row.id]

        assert definition.tags == ("Keep",)

    async def test_a_list_needs_a_name(self, session: AsyncSession) -> None:
        with pytest.raises(list_config.ListConfigError, match="Give the list a name"):
            await list_config.create(
                session, name="   ", source="arr_tag", config={"tags": ["keep"]}
            )

    @pytest.mark.parametrize("name", ["Kids, Holiday", "Keep,Hold", "A, B, C"])
    async def test_a_list_name_cannot_carry_a_comma(self, session: AsyncSession, name: str) -> None:
        """A comma is the separator the ``on_list`` fact is joined and split on, so a name
        holding one is never an element of its own fact.

        The auto-attached keep rule then matches nothing while the Lists row and the Policy
        screen both render it as an outright protection, and an item on "Kids, Holiday"
        satisfies a rule naming a different list called "Holiday". Refused at the save
        boundary, the way rule 108 refuses a rule value that strips to nothing.
        """
        with pytest.raises(list_config.ListConfigError, match="can't have a comma"):
            await list_config.create(
                session, name=name, source="arr_tag", config={"tags": ["keep"]}
            )

    async def test_a_rename_cannot_put_a_comma_in_the_name(self, session: AsyncSession) -> None:
        """The same refusal on the edit path, which is the one an operator reaches by renaming
        rather than by adding (rule 72's sibling of the check above)."""
        row = await list_config.create(
            session, name="Keep", source="arr_tag", config={"tags": ["a"]}
        )

        with pytest.raises(list_config.ListConfigError, match="can't have a comma"):
            await list_config.update(session, row.id, name="Keep, Hold")

    @pytest.mark.parametrize("second", ["Keep", "keep", "KEEP", "  keep  "])
    async def test_two_lists_cannot_share_a_name(self, session: AsyncSession, second: str) -> None:
        """A rule naming a list has to mean exactly one list, or the protection points at
        whichever row was written last.

        Swept over capitalization because every reader case-folds the name (rule 88), so a
        second row differing only in case is a second row answering to one keep rule: it
        never got a rule of its own, and deleting either one stripped that rule and stopped
        the other protecting, untouched and unannounced (#508)."""
        await list_config.create(session, name="Keep", source="arr_tag", config={"tags": ["a"]})

        with pytest.raises(list_config.ListConfigError, match="already have a list"):
            await list_config.create(
                session,
                name=second,
                source="plex_collection",
                config={"library": "F", "collection": "C"},
            )

    async def test_a_rename_cannot_take_another_list_s_name(self, session: AsyncSession) -> None:
        """The same refusal on the edit path, which is the one an operator reaches by
        renaming rather than by adding (rule 72's sibling of the check above)."""
        await list_config.create(session, name="Keep", source="arr_tag", config={"tags": ["a"]})
        other = await list_config.create(
            session, name="Hold", source="arr_tag", config={"tags": ["b"]}
        )

        with pytest.raises(list_config.ListConfigError, match="already have a list"):
            await list_config.update(session, other.id, name="KEEP")

    async def test_a_list_can_be_renamed_to_its_own_name_in_another_case(
        self, session: AsyncSession
    ) -> None:
        """Fixing your own list's capitalization is not a collision with itself. Without the
        self-exclusion the operator could never restyle a name they already own."""
        row = await list_config.create(
            session, name="keep", source="arr_tag", config={"tags": ["a"]}
        )

        renamed = await list_config.update(session, row.id, name="Keep")

        assert renamed.name == "Keep"

    async def test_the_retired_curated_source_is_refused(self, session: AsyncSession) -> None:
        """``curated`` left the source vocabulary when the IMDb provider generalized; a
        hand-crafted save naming it is refused like any unknown source."""
        with pytest.raises(list_config.ListConfigError, match="where the list comes from"):
            await list_config.create(session, name="Mine", source="curated", config={})

    async def test_an_unknown_source_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(list_config.ListConfigError, match="where the list comes from"):
            await list_config.create(session, name="Mine", source="rss", config={})

    async def test_an_imdb_list_needs_a_preset_or_a_list_id(self, session: AsyncSession) -> None:
        with pytest.raises(list_config.ListConfigError, match="Paste the list's id"):
            await list_config.create(session, name="Mine", source="imdb", config={})

    async def test_an_unknown_imdb_preset_is_refused(self, session: AsyncSession) -> None:
        """A preset the mirror does not serve would 404 on every sync, so it is refused
        while the operator is looking at the picker."""
        with pytest.raises(list_config.ListConfigError, match="IMDb presets"):
            await list_config.create(
                session, name="Mine", source="imdb", config={"preset": "top1000"}
            )

    async def test_a_malformed_imdb_list_id_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(list_config.ListConfigError, match="looks like ls005421403"):
            await list_config.create(
                session, name="Mine", source="imdb", config={"list_id": "watchlist"}
            )


class TestWhatIsStored:
    async def test_a_saved_tag_list_keeps_its_tags_and_match(self, session: AsyncSession) -> None:
        row = await list_config.create(
            session,
            name="Keep",
            source="arr_tag",
            config={"tags": [" keep ", "gold"], "match": "all"},
        )

        assert json.loads(row.config_json) == {"tags": ["keep", "gold"], "match": "all"}

    async def test_an_unrecognized_match_reads_as_any(self, session: AsyncSession) -> None:
        """ANY is the wider list, which is the keep direction, and it is the same default
        ``ListDefinition.match`` applies -- the two spellings of it must agree."""
        row = await list_config.create(
            session, name="Keep", source="arr_tag", config={"tags": ["keep"], "match": "some"}
        )

        assert json.loads(row.config_json)["match"] == "any"

    async def test_an_imdb_preset_is_stored_as_the_preset(self, session: AsyncSession) -> None:
        row = await list_config.create(
            session, name="Mine", source="imdb", config={"preset": "popular"}
        )

        assert json.loads(row.config_json) == {"preset": "popular"}

    @pytest.mark.parametrize(
        "pasted",
        [
            "ls005421403",
            "https://www.imdb.com/list/ls005421403/",
            "www.imdb.com/list/ls005421403?ref_=hm",
        ],
        ids=["bare-id", "full-url", "url-with-query"],
    )
    async def test_a_pasted_imdb_url_yields_its_id(
        self, session: AsyncSession, pasted: str
    ) -> None:
        """The id is extracted from wherever it sits in the paste, rather than the paste
        being bounced back for retyping."""
        row = await list_config.create(
            session, name="Mine", source="imdb", config={"list_id": pasted}
        )

        assert json.loads(row.config_json) == {"list_id": "ls005421403"}

    async def test_a_watchlist_stores_an_empty_config(self, session: AsyncSession) -> None:
        """Nothing to configure: the watchlist is the signed-in account's own, so whatever
        arrives in ``config`` is dropped rather than stored as meaningless keys."""
        row = await list_config.create(
            session, name="Mine", source="plex_watchlist", config={"stray": "key"}
        )

        assert json.loads(row.config_json) == {}

    async def test_the_imdb_variant_reads_preset_then_list_id(self, session: AsyncSession) -> None:
        """The provider path for each stored shape, and the fallback for a body that names
        neither: the Top 250, the list Reaper has always shipped, never a path the mirror
        will 404."""
        preset = await list_config.create(
            session, name="P", source="imdb", config={"preset": "popular"}
        )
        custom = await list_config.create(
            session, name="C", source="imdb", config={"list_id": "ls005421403"}
        )

        by_id = {d.id: d for d in await list_config.definitions(session)}

        assert by_id[preset.id].imdb_variant == "popular"
        assert by_id[custom.id].imdb_variant == "ls005421403"
        # The seeded default rides along as the fallback shape.
        assert (
            list_config.ListDefinition(
                id=99, name="X", source=ListSource.IMDB, config={}, enabled=True
            ).imdb_variant
            == "top250"
        )


class TestRemoving:
    async def test_every_list_is_removable_the_seeded_ones_included(
        self, session: AsyncSession
    ) -> None:
        """A list acts through its keep rules now, and the API route deletes those in the
        same request (``list_rules.detach_list``), so no rule goes on naming a list that is
        gone (rule 25). With the pairing in place there is nothing left to refuse."""
        rows = await list_config.all_lists(session)
        for row in list(rows):
            await list_config.delete(session, row.id)

        assert await list_config.all_lists(session) == []

    async def test_a_list_the_operator_made_is_removable(self, session: AsyncSession) -> None:
        row = await list_config.create(
            session, name="Keep", source="arr_tag", config={"tags": ["keep"]}
        )

        await list_config.delete(session, row.id)

        assert [r.name for r in await list_config.all_lists(session)] == [
            "IMDb Top 250",
            "Titles you've tagged",
        ]

    async def test_removing_one_that_is_gone_says_so(self, session: AsyncSession) -> None:
        with pytest.raises(list_config.ListConfigError, match="no longer exists"):
            await list_config.delete(session, 9999)


class TestDecodingForTheSync:
    """``definitions`` is what the sync builds providers from, so what it drops is a list that
    stops protecting. Every drop here is deliberate and named."""

    async def test_a_disabled_list_is_returned_not_omitted(self, session: AsyncSession) -> None:
        """The sync needs to SEE a disabled definition: it builds no provider for one, and
        the retire sweep then disables its stored membership. A definition simply missing
        from this list is indistinguishable from one that never existed, and its membership
        would go on protecting -- so the switch on the screen would do nothing."""
        row = await list_config.create(
            session, name="Keep", source="arr_tag", config={"tags": ["keep"]}
        )
        await list_config.update(session, row.id, enabled=False)

        found = await list_config.definitions(session)

        assert [(d.name, d.enabled) for d in found if d.id == row.id] == [("Keep", False)]

    async def test_a_body_that_will_not_parse_is_dropped_not_guessed(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """Unreadable is not empty (rule 93). No provider is built, so the stored membership
        is left exactly as the last good sync left it, rather than replaced by a sync of a
        configuration nobody could read."""
        row = await list_config.create(
            session, name="Keep", source="arr_tag", config={"tags": ["keep"]}
        )
        await session.execute(
            text("UPDATE list_config SET config_json = 'not json' WHERE id = :id"),
            {"id": row.id},
        )
        await session.commit()
        # A raw UPDATE goes around the identity map, which would otherwise keep handing back
        # the instance this session already loaded, body and all. Production never sees this:
        # every request opens its own session and reads the row from the file.
        session.expire_all()

        found = await list_config.definitions(session)

        # Gone from what the sync builds providers from, so nothing overwrites its membership.
        assert row.id not in [d.id for d in found]
        # Still a row, so the operator can see it on the screen and Edit rewrites the body
        # through `_clean_config` -- which is the only way back out of this state.
        assert row.id in [r.id for r in await list_config.all_lists(session)]

    async def test_the_tags_and_match_are_typed_for_the_provider(
        self, session: AsyncSession
    ) -> None:
        row = await list_config.create(
            session,
            name="Keep",
            source="arr_tag",
            config={"tags": ["keep", "gold"], "match": "all"},
        )

        [found] = [d for d in await list_config.definitions(session) if d.id == row.id]

        assert found.tags == ("keep", "gold")
        assert found.match == "all"
        assert found.source is ListSource.ARR_TAG


class TestTheRoutes:
    def test_adding_answers_with_the_cleaned_row_and_its_policy_use(
        self, client: TestClient
    ) -> None:
        """Rule 39: the form re-seeds from what was STORED, not from what it sent. Those
        differ on every save that trimmed anything, and this one trims two tags. The
        response also carries how Policy now uses the list, because the create attached
        the keeps-it-outright rule in the same request."""
        r = client.post(
            "/api/lists/configured",
            json={"name": "  Keep  ", "source": "arr_tag", "config": {"tags": [" keep ", "gold"]}},
        )

        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Keep"
        assert body["config"] == {"tags": ["keep", "gold"], "match": "any"}
        assert {(u["media_type"], u["strength"]) for u in body["policy_use"]} == {
            ("movie", "hard"),
            ("tv", "hard"),
        }

    def test_a_refusal_reaches_the_operator_in_the_services_own_words(
        self, client: TestClient
    ) -> None:
        """Not reworded at the route. A second phrasing of one refusal is the copy that
        drifts from the check enforcing it (rule 144)."""
        r = client.post(
            "/api/lists/configured",
            json={"name": "Keep", "source": "plex_collection", "config": {"library": "Films"}},
        )

        assert r.status_code == 400
        assert r.json()["detail"] == "Say which collection in that library to read."

    def test_editing_the_config_leaves_the_name_alone(self, client: TestClient) -> None:
        """Rule 1: an omitted field and an explicit one are different requests. The edit
        sends `config` alone, so it cannot rename the list on the way past."""
        made = client.post(
            "/api/lists/configured",
            json={
                "name": "Keep",
                "source": "plex_collection",
                "config": {"library": "Films", "collection": "Never Reap"},
            },
        ).json()

        r = client.patch(
            f"/api/lists/configured/{made['id']}",
            json={"config": {"library": "Films", "collection": "Keep Forever"}},
        )

        assert r.status_code == 200
        assert r.json()["name"] == "Keep"
        assert r.json()["config"] == {"library": "Films", "collection": "Keep Forever"}

    def test_editing_one_that_is_gone_says_so(self, client: TestClient) -> None:
        r = client.patch("/api/lists/configured/9999", json={"name": "Keep"})

        assert r.status_code == 400
        assert "no longer exists" in r.json()["detail"]
