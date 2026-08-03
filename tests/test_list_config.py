# SPDX-License-Identifier: AGPL-3.0-or-later
"""The list DEFINITIONS: naming a list, pointing it somewhere, and taking it away (#475).

Membership is somebody else's data mirrored into ``cache.db`` and rebuilt on every sync. A
definition is not rebuildable from anything, so it lives in ``reaper.db`` and is migrated and
backed up. These pin the boundary between the two, and every refusal on the way in.

**Everything here fails closed toward keeping.** Removing a list withdraws a protection, so
the shipped one cannot be removed at all; a configuration that could never match anything is
refused while the operator is looking at the box that is empty, rather than syncing to empty
and sitting on the screen reading "Nothing on it".
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
    async def test_the_first_read_creates_the_shipped_list(self, session: AsyncSession) -> None:
        """So the screen is never empty on a fresh install, and the operator can see what a
        list looks like before making one."""
        rows = await list_config.all_lists(session)

        assert [r.name for r in rows] == ["IMDb Top 250"]
        assert rows[0].built_in is True

    async def test_reading_twice_does_not_create_it_twice(self, session: AsyncSession) -> None:
        await list_config.all_lists(session)
        rows = await list_config.all_lists(session)

        assert len(rows) == 1

    async def test_a_renamed_shipped_list_keeps_its_new_name(self, session: AsyncSession) -> None:
        """Matched on ``built_in`` rather than on the name, or an operator who renamed it gets
        a second row beside theirs on every read -- and two lists answering to one protection."""
        [row] = await list_config.all_lists(session)
        await list_config.update(session, row.id, name="Films worth keeping")

        rows = await list_config.all_lists(session)

        assert [r.name for r in rows] == ["Films worth keeping"]


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

    async def test_a_list_needs_a_name(self, session: AsyncSession) -> None:
        with pytest.raises(list_config.ListConfigError, match="Give the list a name"):
            await list_config.create(
                session, name="   ", source="arr_tag", config={"tags": ["keep"]}
            )

    async def test_two_lists_cannot_share_a_name(self, session: AsyncSession) -> None:
        """A rule naming a list has to mean exactly one list, or the protection points at
        whichever row was written last."""
        await list_config.create(session, name="Keep", source="arr_tag", config={"tags": ["a"]})

        with pytest.raises(list_config.ListConfigError, match="already have a list"):
            await list_config.create(
                session,
                name="Keep",
                source="plex_collection",
                config={"library": "F", "collection": "C"},
            )

    async def test_the_shipped_kind_cannot_be_added_by_hand(self, session: AsyncSession) -> None:
        with pytest.raises(list_config.ListConfigError, match="ships with"):
            await list_config.create(session, name="Mine", source="curated", config={})

    async def test_an_unknown_source_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(list_config.ListConfigError, match="where the list comes from"):
            await list_config.create(session, name="Mine", source="rss", config={})


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


class TestRemoving:
    async def test_the_shipped_list_cannot_be_removed(self, session: AsyncSession) -> None:
        """The default policy carries a keep rule naming it, and a rule naming a list that is
        gone reads as a live protection covering nothing (rule 25). Off is the other verb."""
        [row] = await list_config.all_lists(session)

        with pytest.raises(list_config.ListConfigError, match="ships with Reaper"):
            await list_config.delete(session, row.id)

    async def test_the_route_refuses_it_too(self, client: TestClient) -> None:
        """The refusal the operator actually meets. Pinned through HTTP as well as at the
        service, because the route is the only half a browser can reach."""
        [shipped] = client.get("/api/lists/configured").json()

        r = client.delete(f"/api/lists/configured/{shipped['id']}")

        assert r.status_code == 400
        assert "ships with Reaper" in r.json()["detail"]
        assert len(client.get("/api/lists/configured").json()) == 1

    async def test_a_list_the_operator_made_is_removable(self, session: AsyncSession) -> None:
        row = await list_config.create(
            session, name="Keep", source="arr_tag", config={"tags": ["keep"]}
        )

        await list_config.delete(session, row.id)

        assert [r.name for r in await list_config.all_lists(session)] == ["IMDb Top 250"]

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
    def test_adding_answers_with_the_cleaned_row(self, client: TestClient) -> None:
        """Rule 39: the form re-seeds from what was STORED, not from what it sent. Those
        differ on every save that trimmed anything, and this one trims two tags."""
        r = client.post(
            "/api/lists/configured",
            json={"name": "  Keep  ", "source": "arr_tag", "config": {"tags": [" keep ", "gold"]}},
        )

        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Keep"
        assert body["config"] == {"tags": ["keep", "gold"], "match": "any"}
        assert body["enabled"] is True
        assert body["built_in"] is False

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

    def test_switching_a_list_off_leaves_its_body_alone(self, client: TestClient) -> None:
        """Rule 1: an omitted field and an explicit one are different requests. The switch
        sends `enabled` alone, so it cannot rewrite where the list points on the way past."""
        made = client.post(
            "/api/lists/configured",
            json={
                "name": "Keep",
                "source": "plex_collection",
                "config": {"library": "Films", "collection": "Never Reap"},
            },
        ).json()

        r = client.patch(f"/api/lists/configured/{made['id']}", json={"enabled": False})

        assert r.status_code == 200
        assert r.json()["enabled"] is False
        assert r.json()["config"] == {"library": "Films", "collection": "Never Reap"}

    def test_editing_one_that_is_gone_says_so(self, client: TestClient) -> None:
        r = client.patch("/api/lists/configured/9999", json={"name": "Keep"})

        assert r.status_code == 400
        assert "no longer exists" in r.json()["detail"]
