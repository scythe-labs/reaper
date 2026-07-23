# SPDX-License-Identifier: AGPL-3.0-or-later
"""The server time zone the scheduler runs its timed jobs on.

Before this setting existed the scheduler had no zone, so APScheduler used the container's
own -- UTC in most images -- and a scan an owner set for "2 AM" quietly fired at 2 AM UTC.
These pin the fix:

* the effective zone is stored value, then the REAPER_TIMEZONE seed, then the host's own
  zone, and a bad value at any layer falls THROUGH to the next rather than raising -- the
  scheduler can always build a zone from what it returns;
* changing the zone re-applies every timed job in place, so "0 2 * * *" fires at 2 AM in the
  new zone the moment it is saved, not at the next restart;
* an unknown zone is refused at the API edge and changes nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.session import create_engine, create_session_factory
from reaper.main import create_app
from reaper.secrets import resolve_secret_key
from reaper.services import app_settings, scheduler
from tests._auth import login


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(data_dir=tmp_path, secret_key="test-key", **overrides)  # type: ignore[arg-type]


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(_settings(tmp_path))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A logged-in client over an empty database: exactly a fresh install."""
    settings = _settings(tmp_path)
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestTheEffectiveZoneResolves:
    async def test_a_fresh_install_uses_a_real_host_zone(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Nothing stored and no seed: the host's own zone answers, and it is always a real
        one -- so the scheduler can build a ZoneInfo from it without a fallback."""
        async with factory() as session:
            resolved = await app_settings.get_timezone(session, _settings(tmp_path))
        assert app_settings.is_valid_timezone(resolved)

    async def test_the_env_seed_answers_until_a_value_is_stored(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        seeded = _settings(tmp_path, timezone="Asia/Tokyo")
        async with factory() as session:
            assert await app_settings.get_timezone(session, seeded) == "Asia/Tokyo"
            # Once stored, the stored value wins -- even over the seed.
            await app_settings.set_timezone(session, "Europe/Paris")
            assert await app_settings.get_timezone(session, seeded) == "Europe/Paris"

    async def test_a_bad_stored_value_falls_through_to_the_seed(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Fail safe: a corrupt stored zone (only reachable by a hand-edited DB) must not
        crash the resolver -- it falls through to the seed, which is valid."""
        seeded = _settings(tmp_path, timezone="Asia/Tokyo")
        async with factory() as session:
            await app_settings.set_timezone(session, "Bogus/Nowhere")
            assert await app_settings.get_timezone(session, seeded) == "Asia/Tokyo"

    async def test_a_bad_seed_falls_through_to_the_host_zone(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        bad_seed = _settings(tmp_path, timezone="Not/AZone")
        async with factory() as session:
            resolved = await app_settings.get_timezone(session, bad_seed)
        assert app_settings.is_valid_timezone(resolved)

    def test_is_valid_timezone(self) -> None:
        assert app_settings.is_valid_timezone("America/New_York")
        assert app_settings.is_valid_timezone("UTC")
        assert not app_settings.is_valid_timezone("Mars/Phobos")
        assert not app_settings.is_valid_timezone("")


class TestReschedulingMovesEveryJob:
    async def test_a_new_zone_rebuilds_the_scan_and_upkeep_triggers(self, tmp_path: Path) -> None:
        """Each cron trigger carries its own zone, so moving the clock must rebuild them all.
        Built in UTC, then moved to New York: every job's trigger reads the new zone and the
        scan's 2 AM lands at 06:00/07:00 UTC (EDT/EST), never 02:00 UTC.

        Rescheduling only ever happens on a running scheduler (startup starts it before
        applying stored schedules; the API moves the live one), so the test runs it started --
        on an unstarted scheduler ``add_job(replace_existing=True)`` merely queues a second
        pending copy instead of replacing the trigger.
        """
        settings = _settings(tmp_path)
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        box = SecretBox(resolve_secret_key(settings))
        sched = scheduler.build_scheduler(
            engine, tmp_path, session_factory=factory, secret_box=box, timezone=ZoneInfo("UTC")
        )
        sched.start()
        try:
            ny = ZoneInfo("America/New_York")
            scheduler.reschedule_timezone(
                sched,
                ny,
                settings=settings,
                session_factory=factory,
                cache_engine=engine,
                secret_box=box,
                data_dir=tmp_path,
                scan_cron="0 2 * * *",
                maintenance={},
            )

            scan = sched.get_job(scheduler.SCAN_JOB_ID)
            assert scan is not None
            assert str(scan.trigger.timezone) == "America/New_York"
            # Every upkeep job moved too -- one clock, not a mix.
            for job_id in scheduler.MAINTENANCE_JOB_IDS:
                assert str(sched.get_job(job_id).trigger.timezone) == "America/New_York"

            now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)  # summer -> EDT (-04:00)
            fire = scan.trigger.get_next_fire_time(None, now)
            assert fire.hour == 2  # 2 AM local, whatever the season
            assert fire.astimezone(UTC).hour == 6
        finally:
            sched.shutdown(wait=False)
            await engine.dispose()


class TestTheApiSavesAndReschedules:
    def test_saving_a_zone_moves_the_live_scan_job(self, client: TestClient) -> None:
        assert (
            client.put("/api/settings/general", json={"timezone": "America/New_York"}).json()[
                "timezone"
            ]
            == "America/New_York"
        )
        client.put("/api/settings/jobs/scheduled_scan/schedule", json={"cron": "0 2 * * *"})

        def scan_next() -> datetime:
            jobs = client.get("/api/settings/schedule").json()["jobs"]
            at = next(j["next_run_at"] for j in jobs if j["id"] == "scheduled_scan")
            return datetime.fromisoformat(at)

        ny = scan_next()
        assert ny.hour == 2  # 2 AM in New York

        # Re-home the server to Los Angeles: 2 AM there is a different instant, and the live
        # job must already reflect it -- proof the change rescheduled, not just stored.
        client.put("/api/settings/general", json={"timezone": "America/Los_Angeles"})
        la = scan_next()
        assert la.hour == 2  # still 2 AM local, now Pacific
        assert la.astimezone(UTC) != ny.astimezone(UTC)

    def test_an_unknown_zone_is_refused_and_changes_nothing(self, client: TestClient) -> None:
        client.put("/api/settings/general", json={"timezone": "America/New_York"})
        for bad in ("Mars/Phobos", ""):
            assert client.put("/api/settings/general", json={"timezone": bad}).status_code == 422
        assert client.get("/api/settings/general").json()["timezone"] == "America/New_York"
