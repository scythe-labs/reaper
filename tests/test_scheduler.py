# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background maintenance keeps the caches fresh so a scan can judge.

The one that matters most: the startup catch-up. Without it a fresh install refreshes
the ratings dataset only at 3:30am, so every scan until then degrades and nothing can be
reaped -- the tool looks broken on day one. These prove the catch-up fires when the data
is stale, skips the download when it is warm, and never deletes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.models import Instance, InstanceKind
from reaper.db.session import create_engine, create_session_factory
from reaper.secrets import resolve_secret_key
from reaper.services import app_settings, imdb_dataset, retention, scheduler
from reaper.services.imdb_dataset import ImdbRatings


@pytest.fixture
async def cache_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))  # type: ignore[call-arg]
    yield eng
    await eng.dispose()


class TestStartupCatchUp:
    async def test_it_refreshes_when_the_dataset_is_missing(
        self, cache_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh install has no ratings dataset, so the catch-up must fetch it now --
        otherwise the first scan degrades and stays that way until the nightly job."""
        called: list[str] = []

        async def fake_refresh(engine: AsyncEngine, data_dir: Path) -> None:
            called.append("refreshed")

        monkeypatch.setattr(scheduler, "refresh_ratings", fake_refresh)

        await scheduler.catch_up_on_startup(cache_engine, tmp_path)

        assert called == ["refreshed"]  # the empty dataset is stale, so it fired

    async def test_it_skips_the_download_when_the_dataset_is_warm(
        self, cache_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restart of a healthy install must not re-download ~280 MB it already has."""
        # Seed a fresh, non-degraded dataset state. state() checks that the ratings
        # DATA table exists AND that the sync-metadata row is recent, so both are needed.
        from sqlalchemy import text

        from reaper.clock import utcnow

        async with cache_engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS imdb_rating "
                    "(tconst TEXT PRIMARY KEY, average_rating REAL, num_votes INTEGER)"
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS imdb_dataset_sync "
                    "(id INTEGER PRIMARY KEY, synced_at INTEGER NOT NULL, "
                    "row_count INTEGER NOT NULL)"
                )
            )
            await conn.execute(
                text(
                    "INSERT OR REPLACE INTO imdb_dataset_sync (id, synced_at, row_count) "
                    "VALUES (1, :ts, :n)"
                ),
                {"ts": int(utcnow().timestamp()), "n": 1_000_000},
            )

        assert (await ImdbRatings(cache_engine).state()).degraded() is False

        called: list[str] = []

        async def fake_refresh(engine: AsyncEngine, data_dir: Path) -> None:
            called.append("refreshed")

        monkeypatch.setattr(scheduler, "refresh_ratings", fake_refresh)

        await scheduler.catch_up_on_startup(cache_engine, tmp_path)

        assert called == []  # warm dataset, no download


async def _seed_synced(engine: AsyncEngine, *, hours_ago: float) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS imdb_rating "
                "(tconst TEXT PRIMARY KEY, average_rating REAL, num_votes INTEGER)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS imdb_dataset_sync "
                "(id INTEGER PRIMARY KEY, synced_at INTEGER NOT NULL, row_count INTEGER NOT NULL)"
            )
        )
        await conn.execute(
            text(
                "INSERT OR REPLACE INTO imdb_dataset_sync (id, synced_at, row_count) "
                "VALUES (1, :ts, :n)"
            ),
            {"ts": int((utcnow() - timedelta(hours=hours_ago)).timestamp()), "n": 1_000_000},
        )


class TestRatingsRefreshFreshnessGuard:
    """The scheduled refresh short-circuits when the dataset was pulled recently, so an
    aggressive schedule (the shared presets go down to hourly) cannot re-download the same
    daily-published data on repeat (PF-1)."""

    async def test_a_recent_refresh_short_circuits_the_download(
        self, cache_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _seed_synced(cache_engine, hours_ago=1)
        called: list[str] = []

        async def fake_download(engine: AsyncEngine, data_dir: Path) -> int:
            called.append("downloaded")
            return 0

        monkeypatch.setattr(scheduler.imdb_dataset, "refresh", fake_download)
        await scheduler.refresh_ratings(cache_engine, tmp_path)
        assert called == []  # synced an hour ago, well within the window

    async def test_a_stale_dataset_still_refreshes(
        self, cache_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _seed_synced(cache_engine, hours_ago=25)
        called: list[str] = []

        async def fake_download(engine: AsyncEngine, data_dir: Path) -> int:
            called.append("downloaded")
            return 7

        monkeypatch.setattr(scheduler.imdb_dataset, "refresh", fake_download)
        await scheduler.refresh_ratings(cache_engine, tmp_path)
        assert called == ["downloaded"]  # 25h old, past the 20h window


class TestTheSchedulerIsUpkeepOnly:
    async def test_it_schedules_only_refresh_jobs_never_a_deletion(self, tmp_path: Path) -> None:
        """A timer must never be able to trigger a reap. Automated deletion is gated
        behind an earned autonomy grant, not a cron entry -- so the scheduler's whole job
        list is refreshes."""
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        engine = create_engine(settings)
        sched = scheduler.build_scheduler(
            engine,
            tmp_path,
            session_factory=create_session_factory(engine),
            secret_box=SecretBox(resolve_secret_key(settings)),
            timezone=ZoneInfo("UTC"),
        )
        # build_scheduler returns an unstarted scheduler; jobs are inspectable without
        # starting it, and there is nothing to shut down.
        job_ids = {job.id for job in sched.get_jobs()}
        assert job_ids == {
            "refresh_ratings",
            "refresh_curated_lists",
            "full_history_sweep",
            # Housekeeping, deliberately absent from the operator's Jobs list: deleting
            # sessions whose window has already closed is not a choice to hand over, and an
            # off switch on it could only ever let the table grow (PR-13). Trimming the
            # scan history is off it on the same argument (#315), and deletes rows Reaper
            # wrote about itself -- never a file, an *arr or Plex.
            scheduler.SESSION_SWEEP_JOB_ID,
            scheduler.SNAPSHOT_SWEEP_JOB_ID,
        }
        # Every job is a refresh/sweep. Nothing here touches the executor or an *arr
        # delete -- automated deletion is gated behind an autonomy grant, not a cron entry.
        for job in sched.get_jobs():
            assert "delete" not in job.func.__name__
            assert "reap" not in job.func.__name__
            assert "execute" not in job.func.__name__
            assert (
                "sync" in job.func.__name__
                or "refresh" in job.func.__name__
                or ("sweep" in job.func.__name__)
            )
        await engine.dispose()

    async def test_the_snapshot_sweep_runs_shortly_after_boot_not_a_full_interval_in(
        self, tmp_path: Path
    ) -> None:
        """An install upgrading with months of scans behind it has a database that is
        mostly dead weight now, so an interval trigger's default first fire -- a whole
        twelve hours in -- is the wrong answer. Pinned against the interval rather than a
        literal, so raising one cannot quietly turn the delay back into a full period."""
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        engine = create_engine(settings)
        sched = scheduler.build_scheduler(
            engine,
            tmp_path,
            session_factory=create_session_factory(engine),
            secret_box=SecretBox(resolve_secret_key(settings)),
            timezone=ZoneInfo("UTC"),
        )
        job = next(j for j in sched.get_jobs() if j.id == scheduler.SNAPSHOT_SWEEP_JOB_ID)
        wait = (job.trigger.start_date - utcnow()).total_seconds()

        assert 0 < wait <= scheduler.SNAPSHOT_SWEEP_STARTUP_DELAY_S
        assert wait < scheduler.SNAPSHOT_SWEEP_INTERVAL_S
        await engine.dispose()

    async def test_the_snapshot_sweep_is_handed_the_folder_the_database_is_in(
        self, tmp_path: Path
    ) -> None:
        """The compaction opens ``data_dir / "reaper.db"`` with a raw sqlite3 connection, so
        a wrong folder here does not fail: it CREATES an empty second database there and
        vacuums that, while the real one is never compacted. The arity is the same either
        way, so APScheduler's own argument check cannot see it.

        The scheduler is given a folder that is not the engine's (rule 141) -- pinning the
        path the engine was built from would hold just as well if the job derived its own.
        """
        db_dir, sweep_dir = tmp_path / "db", tmp_path / "swept"
        db_dir.mkdir()
        settings = Settings(data_dir=db_dir, secret_key="k")  # type: ignore[call-arg]
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        sched = scheduler.build_scheduler(
            engine,
            sweep_dir,
            session_factory=factory,
            secret_box=SecretBox(resolve_secret_key(settings)),
            timezone=ZoneInfo("UTC"),
        )

        job = next(j for j in sched.get_jobs() if j.id == scheduler.SNAPSHOT_SWEEP_JOB_ID)

        assert list(job.args) == [factory, sweep_dir]
        await engine.dispose()

    async def test_compaction_is_attempted_on_a_firing_that_swept_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Gating compaction on this firing's sweep would make the upgrade firing the only
        one that could ever hand disk back: it drains the whole backlog at once, so every
        later firing removes nothing. A compaction lost to a full disk, a locked database
        or a canceled shutdown would then never be reattempted until a new scan pushed the
        count past the window, which on an install with auto-scan off is never.

        ``compact_if_fragmented`` is already gated on its own thresholds, so calling it
        every firing costs three pragmas when it declines."""
        attempted: list[Path] = []

        async def _swept_nothing(_factory: object) -> int:
            return 0

        async def _record(data_dir: Path) -> bool:
            attempted.append(data_dir)
            return False

        monkeypatch.setattr(retention, "sweep_old_snapshots", _swept_nothing)
        monkeypatch.setattr(retention, "compact_if_fragmented", _record)

        await scheduler.sweep_old_snapshots(None, tmp_path)  # type: ignore[arg-type]

        assert attempted == [tmp_path]

    async def test_a_sweep_that_raises_does_not_escape_the_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A database busy with a scan must not stop the scheduler, and nothing downstream
        depends on this having run -- the worst case of a skipped firing is that the next
        one has one more scan to drop."""

        async def _boom(_factory: object) -> int:
            raise OSError("database is locked")

        monkeypatch.setattr(retention, "sweep_old_snapshots", _boom)

        await scheduler.sweep_old_snapshots(None, tmp_path)  # type: ignore[arg-type]

    async def test_the_snapshot_sweep_is_not_operator_schedulable(self) -> None:
        """Same argument as the session sweep: an off switch on it could only ever let the
        database grow, which is the state it exists to end (#315). Nothing reads a scan
        older than the newest one, so there is no window an operator would want to widen."""
        assert scheduler.SNAPSHOT_SWEEP_JOB_ID not in scheduler.SCHEDULABLE_JOB_IDS
        assert scheduler.SNAPSHOT_SWEEP_JOB_ID not in scheduler.DEFAULT_MAINTENANCE_CRONS


@pytest.fixture
async def main_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A real main-database session factory (with the ``AppSetting`` table) so an upkeep job
    can record its last run. Its own file, kept apart from the cache engine above."""
    data_dir = tmp_path / "main"
    data_dir.mkdir()
    settings = Settings(data_dir=data_dir, secret_key="k")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


class TestUpkeepJobsRecordTheirLastRun:
    """Every upkeep job records when it last finished, whether it succeeded, and a short
    result -- the store behind the Jobs page's one last-run line per job. The scan and
    Leaving Soon read a SUCCESSFUL run from their own sources, so those are not exercised
    here; the scan's own failure-only recording has its own test class below."""

    async def _last(
        self, factory: async_sessionmaker[AsyncSession], job_id: str
    ) -> dict[str, object] | None:
        async with factory() as session:
            return (await app_settings.get_job_last_runs(session)).get(job_id)

    async def test_a_successful_list_refresh_records_ok(
        self,
        cache_engine: AsyncEngine,
        main_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_sync(*args: object, **kwargs: object) -> int:
            return 250

        monkeypatch.setattr(scheduler.lists, "sync", fake_sync)
        await scheduler.refresh_curated_lists(cache_engine, main_factory)

        last = await self._last(main_factory, "refresh_curated_lists")
        assert last == {"at": last["at"], "ok": True, "result": "Lists refreshed"}  # type: ignore[index]

    async def test_a_failed_list_refresh_records_not_ok(
        self,
        cache_engine: AsyncEngine,
        main_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def boom(*args: object, **kwargs: object) -> int:
            raise RuntimeError("source down")

        monkeypatch.setattr(scheduler.lists, "sync", boom)
        await scheduler.refresh_curated_lists(cache_engine, main_factory)

        last = await self._last(main_factory, "refresh_curated_lists")
        assert last is not None
        assert last["ok"] is False
        assert last["result"] == "Couldn't refresh lists"

    async def test_a_fresh_skip_still_records_a_run(
        self,
        cache_engine: AsyncEngine,
        tmp_path: Path,
        main_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Synced an hour ago: the download short-circuits, but the owner who pressed "Run
        # now" still gets a truthful last-run line rather than "hasn't run yet".
        await _seed_synced(cache_engine, hours_ago=1)
        await scheduler.refresh_ratings(cache_engine, tmp_path, main_factory)

        last = await self._last(main_factory, "refresh_ratings")
        assert last is not None
        assert last["ok"] is True
        assert last["result"] == "Already up to date"

    async def test_a_successful_ratings_refresh_records_ok(
        self,
        cache_engine: AsyncEngine,
        tmp_path: Path,
        main_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_synced(cache_engine, hours_ago=25)  # stale, so it downloads

        async def fake_download(engine: AsyncEngine, data_dir: Path) -> imdb_dataset.LoadResult:
            return imdb_dataset.LoadResult(rows=7, skipped=0)

        monkeypatch.setattr(scheduler.imdb_dataset, "refresh", fake_download)
        await scheduler.refresh_ratings(cache_engine, tmp_path, main_factory)

        last = await self._last(main_factory, "refresh_ratings")
        assert last is not None
        assert last["ok"] is True
        assert last["result"] == "Ratings refreshed"

    async def test_the_startup_catch_up_records_nothing(
        self, cache_engine: AsyncEngine, tmp_path: Path
    ) -> None:
        # Called without a session factory (as the startup catch-up does), recording is a
        # no-op -- a catch-up refresh is not an on-schedule or by-hand run.
        await _seed_synced(cache_engine, hours_ago=1)
        await scheduler.refresh_ratings(cache_engine, tmp_path)  # no factory, must not raise

    async def test_a_ratings_state_read_failure_still_records_not_ok(
        self,
        cache_engine: AsyncEngine,
        tmp_path: Path,
        main_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The freshness check itself (``ImdbRatings.state()``) used to run before the
        try/except, so a broken cache read would escape unrecorded. It is inside the try
        now, so a crash there is a recorded failure, not a silent gap."""

        async def boom(self: object) -> object:
            raise RuntimeError("cache.db is locked")

        monkeypatch.setattr(ImdbRatings, "state", boom)
        await scheduler.refresh_ratings(cache_engine, tmp_path, main_factory)

        last = await self._last(main_factory, "refresh_ratings")
        assert last is not None
        assert last["ok"] is False
        assert last["result"] == "Couldn't refresh ratings"

    async def test_a_history_sweep_instance_lookup_failure_still_records_not_ok(
        self,
        cache_engine: AsyncEngine,
        main_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The Tautulli instance lookup and client construction used to run before the
        try/except, so a broken DB read or a bad decrypt would escape unrecorded. Both are
        inside the try now."""

        class _BoomSecretBox:
            def decrypt(self, value: str) -> str:
                raise ValueError("secret key rotated")

        async with main_factory() as session:
            session.add(
                Instance(
                    kind=InstanceKind.TAUTULLI,
                    name="t",
                    enabled=True,
                    base_url="http://tautulli.example",
                    api_key_enc="not-really-encrypted",
                    verify_tls=True,
                    created_at=utcnow(),
                )
            )
            await session.commit()

        await scheduler.full_history_sweep(main_factory, cache_engine, _BoomSecretBox())  # type: ignore[arg-type]

        last = await self._last(main_factory, "full_history_sweep")
        assert last is not None
        assert last["ok"] is False
        assert last["result"] == "Couldn't update history"


class TestScheduledScanRecordsOnlyItsFailure:
    """A scheduled scan that crashes outright writes no snapshot, so it is the one
    exception to 'the scan reads its own last-run line from the snapshot' (see
    JOB_LAST_RUN_PREFIX): the crash is recorded under ``scheduler.SCAN_JOB_ID`` so the
    Jobs page can still show it failed, instead of silently repeating whatever the last
    successful snapshot said. A quiet, expected skip (no Radarr/Tautulli configured yet,
    or a scan already running) is not a failure and must record nothing."""

    async def _last(
        self, factory: async_sessionmaker[AsyncSession], job_id: str
    ) -> dict[str, object] | None:
        async with factory() as session:
            return (await app_settings.get_job_last_runs(session)).get(job_id)

    async def test_a_crashed_scan_records_a_failure(
        self,
        cache_engine: AsyncEngine,
        tmp_path: Path,
        main_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from reaper.services import scan_runner

        async def boom(**kwargs: object) -> object:
            raise RuntimeError("radarr unreachable")

        monkeypatch.setattr(scan_runner, "run_scan", boom)
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        await scheduler.scheduled_scan(settings, main_factory, cache_engine, None)  # type: ignore[arg-type]

        last = await self._last(main_factory, scheduler.SCAN_JOB_ID)
        assert last is not None
        assert last["ok"] is False
        assert last["result"] == "Scan failed"

    async def test_a_misconfigured_skip_records_nothing(
        self,
        cache_engine: AsyncEngine,
        tmp_path: Path,
        main_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from reaper.services import scan_runner

        async def config_error(**kwargs: object) -> object:
            raise scan_runner.ScanConfigError("no Radarr configured yet")

        monkeypatch.setattr(scan_runner, "run_scan", config_error)
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        await scheduler.scheduled_scan(settings, main_factory, cache_engine, None)  # type: ignore[arg-type]

        assert await self._last(main_factory, scheduler.SCAN_JOB_ID) is None

    async def test_a_scan_already_running_records_nothing(
        self,
        cache_engine: AsyncEngine,
        tmp_path: Path,
        main_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from reaper.services import scan_runner

        async def in_progress(**kwargs: object) -> object:
            raise scan_runner.ScanInProgressError("a scan is already running")

        monkeypatch.setattr(scan_runner, "run_scan", in_progress)
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        await scheduler.scheduled_scan(settings, main_factory, cache_engine, None)  # type: ignore[arg-type]

        assert await self._last(main_factory, scheduler.SCAN_JOB_ID) is None
