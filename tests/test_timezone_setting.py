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

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.models import AppSetting
from reaper.db.session import create_engine, create_session_factory
from reaper.main import create_app
from reaper.secrets import resolve_secret_key
from reaper.services import app_settings, scheduler
from reaper.services.update_check import UpdateChecker
from tests._auth import login


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(data_dir=tmp_path, secret_key="test-key", **overrides)  # type: ignore[arg-type]


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
        self, async_factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Nothing stored and no seed: the host's own zone answers, and it is always a real
        one -- so the scheduler can build a ZoneInfo from it without a fallback."""
        async with async_factory() as session:
            resolved = await app_settings.get_timezone(session, _settings(tmp_path))
        assert app_settings.is_valid_timezone(resolved)

    async def test_the_env_seed_answers_until_a_value_is_stored(
        self, async_factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        seeded = _settings(tmp_path, timezone="Asia/Tokyo")
        async with async_factory() as session:
            assert await app_settings.get_timezone(session, seeded) == "Asia/Tokyo"
            # Once stored, the stored value wins -- even over the seed.
            await app_settings.set_timezone(session, "Europe/Paris")
            assert await app_settings.get_timezone(session, seeded) == "Europe/Paris"

    async def test_a_bad_stored_value_falls_through_to_the_seed(
        self, async_factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Fail safe: a corrupt stored zone (only reachable by a hand-edited DB) must not
        crash the resolver -- it falls through to the seed, which is valid."""
        seeded = _settings(tmp_path, timezone="Asia/Tokyo")
        async with async_factory() as session:
            await app_settings.set_timezone(session, "Bogus/Nowhere")
            assert await app_settings.get_timezone(session, seeded) == "Asia/Tokyo"

    async def test_a_bad_seed_falls_through_to_the_host_zone(
        self, async_factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        bad_seed = _settings(tmp_path, timezone="Not/AZone")
        async with async_factory() as session:
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
        async_factory = create_session_factory(engine)
        box = SecretBox(resolve_secret_key(settings))
        sched = scheduler.build_scheduler(
            engine,
            tmp_path,
            session_factory=async_factory,
            secret_box=box,
            settings=settings,
            update_checker=UpdateChecker(),
            timezone=ZoneInfo("UTC"),
            reap_running=lambda: False,
        )
        sched.start()
        try:
            ny = ZoneInfo("America/New_York")
            scheduler.apply_stored_schedules(
                sched,
                ny,
                settings=settings,
                session_factory=async_factory,
                cache_engine=engine,
                secret_box=box,
                update_checker=UpdateChecker(),
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

    async def test_a_malformed_stored_cron_is_skipped_never_500s_or_half_applies(
        self, tmp_path: Path
    ) -> None:
        """PR-4: a stored-but-malformed cron must not raise out of the timezone save or leave
        the scheduler half-moved. apply_stored_schedules wraps each apply in a ValueError
        guard, so a bad scan cron and a bad upkeep override are logged and skipped while every
        well-formed job still moves to the new zone.

        This used to say "the same guard startup uses", which was the claim rule 87 wanted and
        not what the tree did: startup had its own copy. Startup is now the same call, and the
        boot half is driven in ``TestBootAppliesWhatIsStored`` rather than asserted here."""
        settings = _settings(tmp_path)
        engine = create_engine(settings)
        async_factory = create_session_factory(engine)
        box = SecretBox(resolve_secret_key(settings))
        sched = scheduler.build_scheduler(
            engine,
            tmp_path,
            session_factory=async_factory,
            secret_box=box,
            settings=settings,
            update_checker=UpdateChecker(),
            timezone=ZoneInfo("UTC"),
            reap_running=lambda: False,
        )
        sched.start()
        try:
            bad_job = scheduler.MAINTENANCE_JOB_IDS[0]
            ny = ZoneInfo("America/New_York")
            # Must not raise, though both the scan cron and one upkeep override are malformed.
            scheduler.apply_stored_schedules(
                sched,
                ny,
                settings=settings,
                session_factory=async_factory,
                cache_engine=engine,
                secret_box=box,
                update_checker=UpdateChecker(),
                data_dir=tmp_path,
                scan_cron="not a cron",
                maintenance={bad_job: "also not a cron"},
            )

            # The malformed scan was skipped, so no scan job was created.
            assert sched.get_job(scheduler.SCAN_JOB_ID) is None
            for job_id in scheduler.MAINTENANCE_JOB_IDS:
                zone = str(sched.get_job(job_id).trigger.timezone)
                if job_id == bad_job:
                    # The bad override was skipped: this job kept its prior zone, not half-moved.
                    assert zone == "UTC"
                else:
                    assert zone == "America/New_York"
        finally:
            sched.shutdown(wait=False)
            await engine.dispose()

    async def test_a_stored_row_naming_a_job_this_build_lacks_is_reported(
        self, tmp_path: Path
    ) -> None:
        """A retired job id left behind in the settings table is said out loud, not skipped.

        Boot reached this as a ``KeyError`` out of ``apply_maintenance_schedule``, because it
        iterated the stored rows; the timezone save iterated the known jobs and so never saw
        it at all. Sharing one function is what makes both paths report it, which is the half
        of rule 87 that a shared *guard* does not cover on its own: the guards already matched,
        the populations did not.

        Every real job still moves, so an unreadable row cannot cost the zone change.
        """
        settings = _settings(tmp_path)
        engine = create_engine(settings)
        async_factory = create_session_factory(engine)
        box = SecretBox(resolve_secret_key(settings))
        sched = scheduler.build_scheduler(
            engine,
            tmp_path,
            session_factory=async_factory,
            secret_box=box,
            settings=settings,
            update_checker=UpdateChecker(),
            timezone=ZoneInfo("UTC"),
            reap_running=lambda: False,
        )
        sched.start()
        try:
            with capture_logs() as events:
                scheduler.apply_stored_schedules(
                    sched,
                    ZoneInfo("America/New_York"),
                    settings=settings,
                    session_factory=async_factory,
                    cache_engine=engine,
                    secret_box=box,
                    update_checker=UpdateChecker(),
                    data_dir=tmp_path,
                    scan_cron=None,
                    maintenance={"refresh_ratings": "0 5 * * *", "retired_job": "0 6 * * *"},
                )
            unknown = [e for e in events if e["event"] == "scheduler.unknown_maintenance_job"]
            assert [e["job"] for e in unknown] == ["retired_job"]
            # It is reported, never wired: a job this build has no callable for cannot run.
            assert sched.get_job("retired_job") is None
            for job_id in scheduler.MAINTENANCE_JOB_IDS:
                assert str(sched.get_job(job_id).trigger.timezone) == "America/New_York"
        finally:
            sched.shutdown(wait=False)
            await engine.dispose()


class TestBootAppliesWhatIsStored:
    """What a restart puts in the job table, which is the thing nothing pinned.

    The startup replay used to be its own copy of the ladder in ``main.py``, and the finding
    that it was a copy also recorded that only the shared function had a test. Boot now calls
    that function, so these pin the wiring *and* the answer to the question the merge raises:
    a job the owner never touched still runs on its built-in default, and one they turned off
    stays off across the restart.
    """

    def _boot(self, tmp_path: Path, stored: dict[str, object]) -> TestClient:
        settings = _settings(tmp_path)
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            for key, value in stored.items():
                session.add(AppSetting(key=key, value_json=json.dumps(value), updated_at=utcnow()))
            session.commit()
        engine.dispose()
        return TestClient(create_app(settings))

    def test_an_override_is_applied_a_default_is_kept_and_an_off_job_stays_off(
        self, tmp_path: Path
    ) -> None:
        """Three states in one boot, because they are three different code paths.

        ``refresh_ratings`` carries an override, ``check_for_updates`` is stored as off, and
        ``refresh_curated_lists`` and ``full_history_sweep`` were never touched. Before this
        change boot iterated the stored rows, so the two untouched jobs were left on the
        triggers ``build_scheduler`` wired; it now re-applies them from the same declaration.
        The trigger is identical either way, and asserting the cron proves that rather than
        assuming it.
        """
        prefix = app_settings.MAINTENANCE_SCHEDULE_PREFIX
        with self._boot(
            tmp_path,
            {
                app_settings.SCAN_SCHEDULE_KEY: "0 2 * * *",
                f"{prefix}refresh_ratings": "15 1 * * *",
                f"{prefix}check_for_updates": None,
            },
        ) as client:
            sched = client.app.state.scheduler  # type: ignore[attr-defined]

            def cron_of(job_id: str) -> str:
                job = sched.get_job(job_id)
                fields = {f.name: str(f) for f in job.trigger.fields}
                return f"{fields['minute']} {fields['hour']}"

            assert cron_of(scheduler.SCAN_JOB_ID) == "0 2"
            assert cron_of("refresh_ratings") == "15 1"  # the override won
            # Untouched, so still on the built-in default -- and read from the declaration,
            # not transcribed, or the assertion would pin a copy of the value (rule 119).
            for job_id in ("refresh_curated_lists", "full_history_sweep"):
                default = scheduler.DEFAULT_MAINTENANCE_CRONS[job_id].split()
                assert cron_of(job_id) == f"{default[0]} {default[1]}"
            # Stored as off. `build_scheduler` wires every default, so this job exists until
            # the replay removes it -- which is exactly the work boot's own loop was doing.
            assert sched.get_job("check_for_updates") is None

    def test_a_malformed_stored_cron_does_not_stop_the_boot(self, tmp_path: Path) -> None:
        """Rule 87's guard, driven through the real boot rather than the shared function.

        A hand-edited or newly-rejected cron string must leave an app the owner can log in to
        and fix it from, so the bad job keeps its default and every other job is scheduled.

        A second, *well-formed* override rides along deliberately. Without it this test cannot
        fail: delete the replay entirely and the bad cron is never parsed, so "no scan job" and
        "refresh_ratings still has a trigger" both hold for the wrong reason -- rule 119's
        environmental accident, and rule 118's rule that a test which cannot discriminate must
        not read as a proof. The good override is what makes the guard's survival mean the
        replay ran and kept going.
        """
        prefix = app_settings.MAINTENANCE_SCHEDULE_PREFIX
        with self._boot(
            tmp_path,
            {
                app_settings.SCAN_SCHEDULE_KEY: "not a cron",
                f"{prefix}refresh_ratings": "also not a cron",
                f"{prefix}full_history_sweep": "40 2 * * *",
            },
        ) as client:
            sched = client.app.state.scheduler  # type: ignore[attr-defined]
            assert sched.get_job(scheduler.SCAN_JOB_ID) is None  # skipped, not crashed
            assert sched.get_job("refresh_ratings") is not None  # kept its default
            # The replay reached past the bad row: this override is applied, not defaulted.
            swept = {f.name: str(f) for f in sched.get_job("full_history_sweep").trigger.fields}
            assert f"{swept['minute']} {swept['hour']}" == "40 2"
            assert client.get("/api/health").status_code == 200


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
