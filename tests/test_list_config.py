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
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.api import lists as list_config_api
from reaper.clients.plex import PlexError
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.session import create_engine, create_session_factory
from reaper.main import create_app
from reaper.services import list_config
from reaper.services.lists import ListSource
from tests._auth import login


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


@pytest.fixture
async def session(tmp_path: Path) -> Iterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="k")
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

    async def test_an_unreadable_body_is_dropped_for_a_reader_and_raises_for_a_sync(
        self, session: AsyncSession
    ) -> None:
        """A row that will not decode must not read to the sync as a row the operator
        deleted: absent from the definitions, its slug is outside every family's ``current``
        set, so the retire sweep disables the membership it is still protecting with, with
        only a log line to say so. ``strict`` routes that to the registry-unreadable state
        the scan already has, which builds nothing and retires nothing (rules 65/91).

        Tolerant for a reader, because the screen an operator would fix the row from must
        still render the rows beside it.
        """
        row = await list_config.create(
            session, name="Broken", source="arr_tag", config={"tags": ["a"]}
        )
        row.config_json = "{not json"
        await session.commit()

        readable = await list_config.definitions(session)
        assert row.id not in {d.id for d in readable}
        assert {d.name for d in readable} == {"IMDb Top 250", "Titles you've tagged"}

        with pytest.raises(list_config.ListRegistryUnreadableError):
            await list_config.definitions(session, strict=True)

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
        with pytest.raises(list_config.ListConfigError, match="looks like ls000000000"):
            await list_config.create(
                session, name="Mine", source="imdb", config={"list_id": "watchlist"}
            )

    def test_the_modal_spells_every_refusal_the_way_this_module_does(self) -> None:
        """``ListModal.tsx`` says these same sentences before the round trip.

        Two copies of one requirement, which is rule 144's hazard: each side was pinned by its
        own test, nothing bound the pair, and a one-sided edit left both suites green. The
        failure message names the other file, because a comment asking a future author to
        remember the second copy does nothing.
        """
        modal = (
            Path(__file__).resolve().parents[1] / "frontend/src/components/ListModal.tsx"
        ).read_text(encoding="utf-8")
        for sentence in (
            "Give the list a name, so you can pick it out on the Policy screen.",
            "Say which Plex library to look in.",
            "Say which collection in that library to read.",
            "Add at least one tag, spelled as it appears in Sonarr or Radarr.",
            "Paste the list's id or URL. An IMDb list id looks like ls000000000.",
        ):
            assert sentence in modal, (
                f"services/list_config.py refuses with {sentence!r}, and "
                "frontend/src/components/ListModal.tsx no longer says it before the round "
                "trip. Edit both, or drop the browser-side check."
            )

    def test_the_name_length_is_one_number_in_three_places(self) -> None:
        """The one refusal the modal ENFORCES rather than repeating, and the reason it has to.

        ``_clean_name`` writes the sentence an operator should read, and it can never fire:
        both schemas bound ``name`` at 100 and FastAPI validates before the route runs, so a
        101-character name comes back as Pydantic's "String should have at most 100
        characters", which ``api.ts``'s ``reason()`` joins into "The list wasn't saved: …"
        (rule 21). The schema bound stays (rule 95) and the box stops taking characters at
        the same number instead, which makes the state unreachable from the UI. Nobody types
        101 characters; a paste gets there in one go.

        Three declarations of one number, held together here rather than by a comment asking
        a future author to remember the other two (rules 131, 144).
        """
        from reaper.api.schemas import ListConfigIn, ListConfigPatch

        limit = 100
        too_long = "x" * (limit + 1)
        with pytest.raises(list_config.ListConfigError, match="too long"):
            list_config._clean_name(too_long)
        assert list_config._clean_name("x" * limit) == "x" * limit

        for model in (ListConfigIn, ListConfigPatch):
            bounds = [
                m.max_length
                for m in model.model_fields["name"].metadata
                if hasattr(m, "max_length")
            ]
            assert bounds == [limit], f"{model.__name__}.name bounds the length at {bounds}"

        modal = (
            Path(__file__).resolve().parents[1] / "frontend/src/components/ListModal.tsx"
        ).read_text(encoding="utf-8")
        assert f"export const LIST_NAME_MAX = {limit};" in modal, (
            "services/list_config.py refuses a name longer than 100 and api/schemas.py 422s "
            "before it can, so frontend/src/components/ListModal.tsx caps the box at the same "
            "number. Move all three, or the operator meets Pydantic's wording instead."
        )
        assert "maxLength={LIST_NAME_MAX}" in modal, (
            "frontend/src/components/ListModal.tsx declares LIST_NAME_MAX and no longer binds "
            "it to the name input, so the length 422 is reachable by paste again."
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
    def test_adding_answers_with_the_cleaned_row_and_no_policy_use(
        self, client: TestClient
    ) -> None:
        """Rule 39: the form re-seeds from what was STORED, not from what it sent. Those
        differ on every save that trimmed anything, and this one trims two tags. The
        response carries an EMPTY policy use: adding a list writes no rule, so the row reads
        "Not used by your policy yet" until the operator sets one on Policy."""
        r = client.post(
            "/api/lists/configured",
            json={"name": "  Keep  ", "source": "arr_tag", "config": {"tags": [" keep ", "gold"]}},
        )

        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Keep"
        assert body["config"] == {"tags": ["keep", "gold"], "match": "any"}
        assert body["policy_use"] == []

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


def _definition(**overrides: object) -> list_config.ListDefinition:
    base: dict[str, object] = {
        "id": 1,
        "name": "Keep",
        "source": ListSource.ARR_TAG,
        "config": {"tags": ["keep"], "match": "any"},
        "enabled": True,
    }
    return list_config.ListDefinition(**{**base, **overrides})  # type: ignore[arg-type]


class TestTheRegistryFingerprint:
    """What a scan records so the simulator can tell its evidence went stale (#512).

    ``Snapshot.list_config_hash`` is this value, and ``api.routes.simulate`` refuses when it
    no longer matches. Every assertion below is about which edits an operator can make that
    change what a scan would GATHER, so the cases are chosen from that question rather than
    from the fields the function happens to read.
    """

    def test_every_edit_that_moves_membership_moves_it(self) -> None:
        """One table, written from what each edit does to a title's membership.

        Retagging changes which titles match; repointing changes where they are read from;
        the NAME changes what a keep rule matches, because ``lists.on_list_fact`` joins the
        names rather than the ids; and switching one off withdraws it altogether.
        """
        base = _definition()
        moved = {
            "retagged": _definition(config={"tags": ["other"], "match": "any"}),
            "match mode": _definition(config={"tags": ["keep"], "match": "all"}),
            "renamed": _definition(name="Keep Forever"),
            "repointed": _definition(source=ListSource.PLEX_WATCHLIST, config={}),
            "switched off": _definition(enabled=False),
        }
        for what, edited in moved.items():
            assert list_config.fingerprint([edited]) != list_config.fingerprint([base]), (
                f"{what} left the fingerprint alone"
            )

    def test_editing_a_list_that_is_switched_off_costs_no_scan(self) -> None:
        """A disabled list gathers nothing, so nothing it says can go stale.

        The enabled rows are what the sync builds providers from, so this is the difference
        between a fingerprint that refuses when membership can have changed and one that
        refuses whenever any row was touched -- and a panel that refuses too often is one an
        operator stops reading.
        """
        off = _definition(enabled=False)
        retagged_while_off = _definition(enabled=False, config={"tags": ["other"]})

        assert list_config.fingerprint([off]) == list_config.fingerprint([retagged_while_off])

    def test_the_answer_does_not_depend_on_row_order(self) -> None:
        first, second = _definition(id=1), _definition(id=2, name="Other")

        assert list_config.fingerprint([first, second]) == list_config.fingerprint([second, first])

    def test_an_empty_registry_still_answers(self) -> None:
        """An install with no lists has a fingerprint like any other, so its snapshots
        compare rather than falling into the unknown branch that refuses forever."""
        assert list_config.fingerprint([]) != ""
        assert list_config.fingerprint([]) == list_config.fingerprint([])
        assert list_config.fingerprint([]) != list_config.fingerprint([_definition()])

    @pytest.mark.anyio
    async def test_a_registry_that_cannot_be_read_answers_none(self, tmp_path: Path) -> None:
        """Fail closed: unknown, never "no lists configured" (rules 65/91).

        A fallback to the empty registry's hash would match any install that happens to have
        no lists, and preview against membership nobody could confirm. ``None`` is not that,
        and it is also not a value to COMPARE: a snapshot that degraded for the same
        unreadable registry recorded ``None`` too, so each caller tests either side for it
        and refuses (``api.routes.simulate``, ``services.executor``), rather than resting on
        an inequality that reads two unknowns as agreement.

        The same row is read twice, readable then not, so the ``None`` is pinned to the
        decode failure and not to anything else about this database (rule 141).
        """
        settings = Settings(data_dir=tmp_path, secret_key="k")
        sync_engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(sync_engine)
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO list_config (name, source, config_json, enabled, built_in,"
                    " created_at) VALUES ('Keep', 'arr_tag', '{\"tags\": [\"keep\"]}', 1, 0, 0)"
                )
            )
        sync_engine.dispose()

        engine = create_engine(settings)
        try:
            async with create_session_factory(engine)() as session:
                readable = await list_config.current_fingerprint(session)
            assert readable is not None, "a registry that reads fine has to answer"

            corrupt_engine = sa_create_engine(settings.sync_database_url)
            with corrupt_engine.begin() as conn:
                conn.execute(text("UPDATE list_config SET config_json = 'not json'"))
            corrupt_engine.dispose()

            async with create_session_factory(engine)() as session:
                assert await list_config.current_fingerprint(session) is None
        finally:
            await engine.dispose()


class TestCheckingTheListsNow:
    """``POST /api/lists/sync`` -- the Lists screen's "Check now".

    ``test_protection_sync.py`` covers ``sync_protection_lists`` itself at length. What had no
    test at all is the route around it, which is where this pass can go wrong in the direction
    that matters: it builds the sources, decides whether Plex answered, and decides whether to
    run at all. Each of those resolves toward leaving the stored membership alone, because a
    check that runs on half an answer retires slugs (rule 115) and a retired slug is a
    protection that stopped covering.
    """

    @staticmethod
    def _sources(monkeypatch: pytest.MonkeyPatch, *, plex: object | None = None) -> None:
        async def fake_build(factory: object, settings: object, box: object, **kw: object) -> Any:
            return ([], [], None, [], plex)

        monkeypatch.setattr(list_config_api.scan_runner, "build_sources", fake_build)

    @staticmethod
    def _syncs(monkeypatch: pytest.MonkeyPatch, result: dict[str, object]) -> dict[str, Any]:
        """Stand in for the sync, and record whether it was called at all."""
        seen: dict[str, Any] = {}

        async def fake_sync(engine: object, **kw: object) -> dict[str, object]:
            seen["called"] = True
            seen.update(kw)
            return result

        monkeypatch.setattr(list_config_api.snapshot, "sync_protection_lists", fake_sync)
        return seen

    def test_a_list_saved_in_a_form_reaper_cannot_read_stops_the_whole_check(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not just that list: none of them.

        This pass retires every slug the registry no longer produces, so a row that will not
        decode is indistinguishable from one the operator deleted -- and running anyway would
        switch off the membership it is still protecting with (rules 65/91).
        """
        self._sources(monkeypatch)
        seen = self._syncs(monkeypatch, {})
        # Read once so the shipped lists are actually seeded -- the registry fills in lazily,
        # and corrupting an empty table would leave a valid registry and prove nothing.
        assert client.get("/api/lists/configured").json()
        engine = sa_create_engine(Settings(data_dir=tmp_path, secret_key="k").sync_database_url)
        with engine.begin() as conn:
            assert conn.execute(text("UPDATE list_config SET config_json = 'not json'")).rowcount
        engine.dispose()

        response = client.post("/api/lists/sync", json={})

        assert response.status_code == 409, response.text
        assert "can't read" in response.json()["detail"]
        assert not seen, "checked the lists anyway, which retires what it could not read"

    def test_plex_not_answering_is_said_plainly_and_retires_nothing(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plex fails closed here exactly as it does in a scan: no live server, no collection
        provider, so nothing is synced for one and nothing is retired either."""

        class _Plex:
            async def connect(self) -> object:
                raise PlexError("unreachable (boom)")

        self._sources(monkeypatch, plex=_Plex())
        self._syncs(monkeypatch, {"imdb:1": 12})

        response = client.post("/api/lists/sync", json={})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["plex_error"] and "couldn't reach Plex" in body["plex_error"]
        # The rest of the pass still ran, so a Plex outage does not stop the *arr tag sweeps.
        assert body["checked"] == 1
        assert body["failed"] == 0

    def test_a_source_reaper_cannot_build_is_a_plain_refusal(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def refuses(factory: object, settings: object, box: object, **kw: object) -> Any:
            raise list_config_api.scan_runner.ScanConfigError("Add a Radarr before checking.")

        monkeypatch.setattr(list_config_api.scan_runner, "build_sources", refuses)
        seen = self._syncs(monkeypatch, {})

        response = client.post("/api/lists/sync", json={})

        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "Add a Radarr before checking."
        assert not seen

    def test_it_counts_the_checks_that_landed_apart_from_the_ones_that_failed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A count each, from the shape ``sync_protection_lists`` answers in: an int is a
        membership size, an ``error:`` string is a list whose check did not land."""
        self._sources(monkeypatch)
        self._syncs(
            monkeypatch,
            {"imdb:1": 250, "arr_tag:2": 0, "plex_collection:3": "error: unreachable"},
        )

        body = client.post("/api/lists/sync", json={}).json()

        assert body["checked"] == 2, "a list that matched nothing was still checked"
        assert body["failed"] == 1
        assert body["plex_error"] is None

    def test_checking_one_list_passes_that_list_through(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The narrowed pass, which must reach the service as a narrowing rather than being
        widened to everything here -- ``sync_protection_lists`` retires nothing when it is
        given one list, and that promise depends on the id arriving."""
        self._sources(monkeypatch)
        seen = self._syncs(monkeypatch, {"imdb:1": 5})

        assert client.post("/api/lists/sync", json={"list_id": 1}).status_code == 200
        assert seen["only"] == 1


class TestTheUniqueNameConstraintIsTheThingThatHolds:
    """The backstop under ``_refuse_name_twice``, driven rather than argued.

    The pre-check reads and then writes, and can be beaten between the two, so the NOCASE
    unique column is what actually holds -- and what it raises, ``IntegrityError``, says
    nothing an operator can act on. Losing the handler turns a lost race into a 500 on the
    Lists screen. The race is simulated by taking the pre-check out of the way, which is the
    only way to reach the constraint from one thread; both callers get their own case because
    the handler is duplicated in each (rule 72).
    """

    @staticmethod
    def _lose_the_race(monkeypatch: pytest.MonkeyPatch) -> None:
        async def never_refuses(session: object, name: str, *, this_row: int | None) -> None:
            return None

        monkeypatch.setattr(list_config, "_refuse_name_twice", never_refuses)

    async def test_creating_a_second_list_with_a_taken_name_says_so(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await list_config.create(session, name="Keep", source="arr_tag", config={"tags": ["keep"]})
        self._lose_the_race(monkeypatch)

        with pytest.raises(list_config.ListConfigError, match="already have a list with that name"):
            await list_config.create(
                session, name="keep", source="arr_tag", config={"tags": ["other"]}
            )

    async def test_renaming_onto_a_taken_name_says_so(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await list_config.create(session, name="Keep", source="arr_tag", config={"tags": ["keep"]})
        other = await list_config.create(
            session, name="Also keep", source="arr_tag", config={"tags": ["gold"]}
        )
        self._lose_the_race(monkeypatch)

        with pytest.raises(list_config.ListConfigError, match="already have a list with that name"):
            await list_config.update(session, other.id, name="KEEP")

    async def test_the_rolled_back_session_still_works(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rollback is why the refusal is usable: without it the session stays poisoned
        and the operator's next save fails for a reason that has nothing to do with it."""
        await list_config.create(session, name="Keep", source="arr_tag", config={"tags": ["keep"]})
        self._lose_the_race(monkeypatch)
        with pytest.raises(list_config.ListConfigError):
            await list_config.create(
                session, name="keep", source="arr_tag", config={"tags": ["other"]}
            )

        made = await list_config.create(
            session, name="Something else", source="arr_tag", config={"tags": ["gold"]}
        )

        assert made.name == "Something else"


class TestARowStaysOnScreenSoTheOperatorCanFixIt:
    """A definition Reaper cannot decode still renders, and deleting one that is gone says so.

    Both are the same instinct on the screen an operator repairs a list from. Raising the bad
    row off the list would hide the only control that rewrites it -- Edit saves through
    ``_clean_config``, which is the way out -- so the body reads as empty instead (rule 96).
    """

    def test_a_body_that_will_not_parse_renders_as_an_empty_one(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        assert client.get("/api/lists/configured").json()
        engine = sa_create_engine(Settings(data_dir=tmp_path, secret_key="k").sync_database_url)
        with engine.begin() as conn:
            assert conn.execute(
                text("UPDATE list_config SET config_json = 'not json' WHERE source = 'arr_tag'")
            ).rowcount
        engine.dispose()

        response = client.get("/api/lists/configured")

        assert response.status_code == 200, response.text
        rows = {r["source"]: r for r in response.json()}
        assert rows["arr_tag"]["config"] == {}, "an unreadable body has to read as empty"
        # And the rest of the screen is unharmed, which is the point of not raising.
        assert rows["imdb"]["config"] == {"preset": "top250"}

    def test_deleting_a_list_that_is_already_gone_says_so(self, client: TestClient) -> None:
        response = client.delete("/api/lists/configured/9999")

        assert response.status_code == 400, response.text
        assert "no longer exists" in response.json()["detail"]
