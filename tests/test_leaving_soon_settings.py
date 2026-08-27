# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Leaving Soon switches, the library choices, and the About facts.

The shelf settings moved from a host-only environment variable into the database, edited
under Settings -> Plex. The rules pinned here:

* The shelf is off on a fresh install, and the read-only opt-in follows the environment only
  until a value is stored. After that, the stored value always wins, the same
  seed-then-database pattern as the deletion switch.
* The library choices survive a re-sync, default new libraries on, and an empty enabled set
  simply turns every shelf off. This scopes a warning, never a deletion.
* A manual shelf update while the feature is off is refused with a plain-words reason, and
  the automatic after-scan pass is a silent no-op in that state, so a hermetic scan never
  reaches for the network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.models import PlexServer
from reaper.db.session import create_engine, create_session_factory
from reaper.engine.reason import Reason
from reaper.services import app_settings, leaving_soon


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(data_dir=tmp_path, secret_key="test-key", **overrides)  # type: ignore[arg-type]


class TestTheStoredSwitches:
    async def test_the_shelf_is_off_on_a_fresh_install(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            assert await app_settings.leaving_soon_enabled(session) is False

    async def test_the_unarmed_opt_in_seeds_from_the_environment_until_stored(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """On the first run, the environment default answers. Once a value is stored, the
        stored value always wins, even over a later change to the environment, so a switch
        flipped in the UI is never clobbered by a stale env var."""
        env_on = _settings(tmp_path, allow_unarmed_leaving_soon=True)
        env_off = _settings(tmp_path)

        async with factory() as session:
            assert await app_settings.leaving_soon_unarmed(session, env_on) is True
            assert await app_settings.leaving_soon_unarmed(session, env_off) is False

            await app_settings.set_leaving_soon_unarmed(session, allowed=False)
            await session.commit()

        async with factory() as session:
            # Stored False beats env True. The UI's choice is the truth now.
            assert await app_settings.leaving_soon_unarmed(session, env_on) is False

    async def test_the_stored_opt_in_reaches_runtime_safety(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        async with factory() as session:
            await app_settings.set_leaving_soon_unarmed(session, allowed=True)
            await session.commit()

        async with factory() as session:
            safety = await app_settings.runtime_safety(session, _settings(tmp_path))
        assert safety.leaving_soon_write_allowed is True
        assert safety.destructive_allowed is False  # the opt-in never widens deletion


class TestTheLibraryStore:
    async def test_libraries_round_trip_and_filter_to_enabled(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        libs = [
            {"key": 2, "title": "Movies", "kind": "movie", "enabled": True},
            {"key": 5, "title": "TV", "kind": "show", "enabled": False},
        ]
        async with factory() as session:
            await app_settings.set_plex_libraries(session, libs)
            await session.commit()

        async with factory() as session:
            stored = await app_settings.get_plex_libraries(session)
            enabled = await app_settings.enabled_plex_libraries(session)
        assert [lib["key"] for lib in stored] == [2, 5]
        assert [lib["key"] for lib in enabled] == [2]


class TestTheSettingsRoutes:
    def test_defaults_read_off_and_never_updated(self, client: TestClient) -> None:
        body = client.get("/api/settings/leaving-soon").json()
        assert body == {
            "enabled": False,
            "allow_unarmed": False,
            "name": "Leaving Soon",
            "applied_name": "Leaving Soon",
            "last": None,
            "last_skip": None,
        }

    async def test_a_scan_that_skipped_the_shelf_reaches_the_row(
        self, client: TestClient, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Both records ride together, because the row needs both to say anything useful. The
        skip is what happened last, and the completed pass is where the counts still on the
        shelf came from. Both are reported side by side rather than resolved here. The reader
        prefers the newer one, the way ``ScanRow`` does for a scan that crashed."""
        async with factory() as session:
            await app_settings.set_leaving_soon_last(
                session,
                at="2026-08-03T02:00:00+00:00",
                movies=280,
                seasons=311,
                applied=True,
                ok=True,
                reason=Reason("shelf_updated", {"added": 4, "removed": 1}),
            )
            await app_settings.set_leaving_soon_last_skip(
                session,
                at="2026-08-04T20:06:00+00:00",
                reason=Reason("error.leaving_soon.skip_unreachable"),
            )
            await session.commit()

        body = client.get("/api/settings/leaving-soon").json()

        assert body["last_skip"] == {
            "at": "2026-08-04T20:06:00+00:00",
            "result_reason": {"k": "error.leaving_soon.skip_unreachable", "p": None},
        }
        # The completed pass survives the skip. Overwriting it would take the shelf's only
        # true counts down with a pass that wrote nothing to Plex.
        assert (body["last"]["movies"], body["last"]["seasons"]) == (280, 311)
        assert body["last"]["result_reason"] == {
            "k": "shelf_updated",
            "p": {"added": 4, "removed": 1},
        }

    async def test_a_last_pass_stored_before_the_typed_conversion_still_reads(
        self, client: TestClient, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A row written before this typed reason format existed carries a bare English
        phrase under ``"result"`` instead of a wire-encoded reason. It reads back as
        ``Reason("legacy", {"text": ...})``. The row here is written straight into storage,
        since ``set_leaving_soon_last`` only ever writes the new shape now."""
        async with factory() as session:
            await app_settings._set(
                session,
                app_settings.LEAVING_SOON_LAST_KEY,
                {
                    "at": "2026-05-01T02:00:00+00:00",
                    "movies": 10,
                    "seasons": 3,
                    "applied": True,
                    "ok": True,
                    "result": "6 added, 1 cleared",
                },
            )
            await session.commit()

        body = client.get("/api/settings/leaving-soon").json()

        assert body["last"]["result_reason"] == {
            "k": "legacy",
            "p": {"text": "6 added, 1 cleared"},
        }

    async def test_a_skip_stored_before_the_typed_conversion_still_reads(
        self, client: TestClient, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A row written before this typed reason format existed carries a bare English
        phrase under ``"result"`` instead of a wire-encoded reason. An old row like this must
        still read back correctly, as ``Reason("legacy", {"text": ...})``, exactly as
        ``engine.reason.from_wire`` already does for a bare stored string. It just stops
        composing through the catalog.

        This writes straight into the row rather than through
        ``set_leaving_soon_last_skip``, which only ever writes the new shape now. The point
        is a value an older build left behind, which today's setter can no longer produce.
        """
        async with factory() as session:
            await app_settings._set(
                session,
                app_settings.LEAVING_SOON_LAST_SKIP_KEY,
                {"at": "2026-07-01T00:00:00+00:00", "result": "Reaper couldn't reach Plex"},
            )
            await session.commit()

        body = client.get("/api/settings/leaving-soon").json()

        assert body["last_skip"] == {
            "at": "2026-07-01T00:00:00+00:00",
            "result_reason": {"k": "legacy", "p": {"text": "Reaper couldn't reach Plex"}},
        }

    def test_the_switches_flip_and_stick(self, client: TestClient) -> None:
        body = client.put(
            "/api/settings/leaving-soon", json={"enabled": True, "allow_unarmed": True}
        ).json()
        assert body["enabled"] is True
        assert body["allow_unarmed"] is True

        # Partial update: one switch changes, the other keeps its stored value.
        body = client.put("/api/settings/leaving-soon", json={"enabled": False}).json()
        assert body["enabled"] is False
        assert body["allow_unarmed"] is True

    def test_the_name_saves_and_says_which_one_plex_still_shows(self, client: TestClient) -> None:
        """Saving a name stores it and nothing else. Moving the shelf is a whole-library
        reconcile per library, so the next pass does it, and until then `applied_name` is
        what an operator will actually find in their library."""
        body = client.put("/api/settings/leaving-soon", json={"name": "Last chance"}).json()
        assert body["name"] == "Last chance"
        assert body["applied_name"] == "Leaving Soon"

        # It sticks, and the switches beside it are untouched by a name-only save.
        body = client.get("/api/settings/leaving-soon").json()
        assert body["name"] == "Last chance"
        assert body["enabled"] is False

    def test_an_empty_name_goes_back_to_the_default(self, client: TestClient) -> None:
        """Clearing the box is how the help says to reset it, so the empty string has to
        mean the default rather than an unnamed shelf. Whitespace is the same answer: Plex
        would take " " as a title and nobody could find it again."""
        client.put("/api/settings/leaving-soon", json={"name": "Last chance"})
        assert client.put("/api/settings/leaving-soon", json={"name": ""}).json()["name"] == (
            "Leaving Soon"
        )
        client.put("/api/settings/leaving-soon", json={"name": "Last chance"})
        assert client.put("/api/settings/leaving-soon", json={"name": "   "}).json()["name"] == (
            "Leaving Soon"
        )

    def test_a_name_past_the_bound_is_refused_rather_than_quietly_cut(
        self, client: TestClient
    ) -> None:
        """A truncated name is a shelf the operator did not ask for, and they would only
        find out by looking in Plex."""
        over = "x" * (app_settings.LEAVING_SOON_NAME_MAX + 1)
        assert client.put("/api/settings/leaving-soon", json={"name": over}).status_code == 422

    async def test_a_clean_pass_records_the_name_it_wrote(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """What Plex shows is only true once every library has actually been written. Until
        then the old name is still the one the next pass has to look under."""
        async with factory() as session:
            assert await app_settings.get_leaving_soon_applied_name(session) == "Leaving Soon"
            await app_settings.set_leaving_soon_applied_name(session, "Last chance")
            await session.commit()
        async with factory() as session:
            assert await app_settings.get_leaving_soon_applied_name(session) == "Last chance"

    async def test_library_choices_apply_to_the_stored_list(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        # No sync has run (no Plex linked), so the list is empty and choices are no-ops.
        assert client.get("/api/settings/plex/libraries").json() == []

        # Seed a stored list the way a sync would, then choose through the API. (The sync
        # route itself needs a live server. The merge rule is a client concern.)
        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        async with factory() as session:
            await app_settings.set_plex_libraries(
                session,
                [
                    {"key": 2, "title": "Movies", "kind": "movie", "enabled": True},
                    {"key": 5, "title": "TV", "kind": "show", "enabled": True},
                ],
            )
            await session.commit()
        await engine.dispose()

        # Turn one off. An unknown key is ignored rather than invented.
        body = client.put("/api/settings/plex/libraries", json={"enabled_keys": [2, 999]}).json()
        assert [(lib["key"], lib["enabled"]) for lib in body] == [(2, True), (5, False)]

        # An empty selection turns every shelf off. That is a legitimate state for a warning
        # feature, never an "everything" expansion.
        body = client.put("/api/settings/plex/libraries", json={"enabled_keys": []}).json()
        assert all(lib["enabled"] is False for lib in body)

    def test_a_pass_with_no_libraries_says_the_same_thing_on_both_surfaces(
        self, client: TestClient, sync_db: Engine
    ) -> None:
        """The shelf on, a server linked, and no library turned on. One pass, one sentence.

        The response and the pass's own stored summary must always agree. If they were
        computed separately, one could say "preview only, nothing written" while the other
        reported failure. Both halves are asserted here, because a fix on either alone would
        leave the contradiction standing.

        This is hermetic. With no library enabled, the reconcile visits no section, so the
        client is built and closed without ever connecting.
        """
        box: SecretBox = client.app.state.secret_box  # type: ignore[attr-defined]
        with Session(sync_db) as session:
            session.add(
                PlexServer(
                    machine_identifier="abc123",
                    name="Example Server",
                    connection_uri="http://plex.local:32400",
                    token_enc=box.encrypt("plex-token"),
                    created_at=utcnow(),
                )
            )
            session.commit()
        client.put("/api/settings/leaving-soon", json={"enabled": True})

        body = client.post("/api/leaving-soon/sync").json()

        assert body["result_reason"] == {"k": "shelf_no_libraries", "p": None}
        assert body["ok"] is False
        stored = client.get("/api/settings/leaving-soon").json()["last"]
        assert (stored["ok"], stored["result_reason"]) == (body["ok"], body["result_reason"])

    def test_a_manual_update_while_off_is_refused_in_plain_words(self, client: TestClient) -> None:
        resp = client.post("/api/leaving-soon/sync")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "error.leaving_soon.disabled"
        assert "Leaving Soon is off" in body["detail"]

    def test_a_manual_update_without_plex_names_the_missing_link(self, client: TestClient) -> None:
        client.put("/api/settings/leaving-soon", json={"enabled": True})
        resp = client.post("/api/leaving-soon/sync")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "error.leaving_soon.unlinked"
        assert "linked Plex server" in body["detail"]

    def test_a_linked_server_that_will_not_answer_is_a_502_in_reapers_words(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``PlexError`` covers more than one failure kind here, so the route must not map
        every instance of it to the same status. A missing link
        (``error.leaving_soon.unlinked``) is a configuration problem and stays a 400. A
        linked server that stops answering is an upstream failure and must read as 502, not
        400, or the operator gets blamed for something that was not their request.

        Both the status and the message lead are asserted, since getting either one wrong
        alone would still leave half a fix.

        The client's own diagnostic text still trails Reaper's own sentence, the same order
        the sibling routes use (`api/plex.py`, `api/plex_trash.py`, `api/lists.py`). The
        operator reads the outcome in Reaper's words first, with the raw diagnostic behind
        it, rather than the diagnostic alone under a status that blames them for it.
        """
        from reaper.clients.plex import PlexError
        from reaper.engine.reason import to_wire
        from reaper.services import leaving_soon as service

        async def _stalled(*args: object, **kwargs: object) -> object:
            raise PlexError("error.plexclient.paging_failed", what="movie listing for section 3")

        monkeypatch.setattr(service, "_plex_client", _stalled)
        client.put("/api/settings/leaving-soon", json={"enabled": True})

        resp = client.post("/api/leaving-soon/sync")

        assert resp.status_code == 502
        body = resp.json()
        assert body["code"] == "error.plex.unreachable"
        # `{error}` is a composed sentence, not a raw client string. It carries the client's
        # own coded failure whole, rather than flattening it at the raise site.
        assert body["params"]["error"] == to_wire(
            PlexError(
                "error.plexclient.paging_failed", what="movie listing for section 3"
            ).as_reason()
        )
        assert body["detail"].startswith("Reaper couldn't reach Plex")
        assert "movie listing for section 3" in body["detail"]

    def test_about_reports_the_facts(self, client: TestClient) -> None:
        body = client.get("/api/about").json()
        assert body["license"] == "AGPL-3.0"
        assert body["version"]
        assert body["data_dir"]
        # The databases exist (the app booted on them), so the sizes are real numbers.
        assert body["reaper_db_bytes"] > 0
        assert body["cache_db_bytes"] >= 0


class TestTheAfterScanPass:
    async def test_it_is_a_silent_no_op_when_off_with_no_webhook(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """This is the hermetic promise. A scan on a fresh install must not reach for Plex or
        Discord. Disabled shelf + no webhook = nothing to do, nothing raised."""
        await leaving_soon.after_scan(factory, _settings(tmp_path), SecretBox("test-key"))
        async with factory() as session:
            assert await app_settings.get_leaving_soon_announced(session) == set()
