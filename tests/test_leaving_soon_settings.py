# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Leaving Soon switches, the library choices, and the About facts.

The shelf settings moved from a host-only environment variable into the database, edited
under Settings -> Plex. The rules pinned here:

* the shelf is OFF on a fresh install, and the read-only opt-in follows the environment
  only until a value is stored -- the stored value wins forever after (the same
  seed-then-DB pattern as the deletion switch);
* the library choices survive a re-sync, default new libraries ON, and an empty enabled
  set simply turns every shelf off (this scopes a warning, never a deletion);
* a manual shelf update while the feature is off is refused with a plain-words reason,
  and the automatic after-scan pass is a silent no-op in that state, so a hermetic scan
  never reaches for the network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.session import create_engine, create_session_factory
from reaper.services import app_settings, leaving_soon


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
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
        """First run: the env default answers. Once a value is stored, the stored value
        wins -- including winning over a LATER change to the environment, so a switch
        flipped in the UI is never clobbered by a stale env var."""
        env_on = _settings(tmp_path, allow_unarmed_leaving_soon=True)
        env_off = _settings(tmp_path)

        async with factory() as session:
            assert await app_settings.leaving_soon_unarmed(session, env_on) is True
            assert await app_settings.leaving_soon_unarmed(session, env_off) is False

            await app_settings.set_leaving_soon_unarmed(session, allowed=False)
            await session.commit()

        async with factory() as session:
            # Stored False beats env True: the UI's choice is the truth now.
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
            "last": None,
            "last_skip": None,
        }

    async def test_a_scan_that_skipped_the_shelf_reaches_the_row(
        self, client: TestClient, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Both records ride together, because the row needs both to say anything useful:
        the skip is what happened last, and the completed pass is where the counts still on
        the shelf came from. Reported side by side rather than resolved here -- the reader
        prefers the newer one, the way ``ScanRow`` does for a scan that crashed."""
        async with factory() as session:
            await app_settings.set_leaving_soon_last(
                session,
                at="2026-08-03T02:00:00+00:00",
                movies=280,
                seasons=311,
                applied=True,
                ok=True,
                result="4 added, 1 cleared",
            )
            await app_settings.set_leaving_soon_last_skip(
                session, at="2026-08-04T20:06:00+00:00", result="Reaper couldn't reach Plex"
            )
            await session.commit()

        body = client.get("/api/settings/leaving-soon").json()

        assert body["last_skip"] == {
            "at": "2026-08-04T20:06:00+00:00",
            "result": "Reaper couldn't reach Plex",
        }
        # The completed pass survives the skip. Overwriting it would take the shelf's only
        # true counts down with a pass that wrote nothing to Plex.
        assert (body["last"]["movies"], body["last"]["seasons"]) == (280, 311)

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

    async def test_library_choices_apply_to_the_stored_list(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        # No sync has run (no Plex linked), so the list is empty and choices are no-ops.
        assert client.get("/api/settings/plex/libraries").json() == []

        # Seed a stored list the way a sync would, then choose through the API. (The
        # sync route itself needs a live server; the merge rule is a client concern.)
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
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

        # Turn one off; an unknown key is ignored rather than invented.
        body = client.put("/api/settings/plex/libraries", json={"enabled_keys": [2, 999]}).json()
        assert [(lib["key"], lib["enabled"]) for lib in body] == [(2, True), (5, False)]

        # An empty selection turns every shelf off -- a legitimate state for a warning
        # feature, never an "everything" expansion.
        body = client.put("/api/settings/plex/libraries", json={"enabled_keys": []}).json()
        assert all(lib["enabled"] is False for lib in body)

    def test_a_manual_update_while_off_is_refused_in_plain_words(self, client: TestClient) -> None:
        resp = client.post("/api/leaving-soon/sync")
        assert resp.status_code == 400
        assert "Leaving Soon is off" in resp.json()["detail"]

    def test_a_manual_update_without_plex_names_the_missing_link(self, client: TestClient) -> None:
        client.put("/api/settings/leaving-soon", json={"enabled": True})
        resp = client.post("/api/leaving-soon/sync")
        assert resp.status_code == 400
        assert "linked Plex server" in resp.json()["detail"]

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
        """The hermetic promise: a scan on a fresh install must not reach for Plex or
        Discord. Disabled shelf + no webhook = nothing to do, nothing raised."""
        await leaving_soon.after_scan(factory, _settings(tmp_path), SecretBox("test-key"))
        async with factory() as session:
            assert await app_settings.get_leaving_soon_announced(session) == set()
