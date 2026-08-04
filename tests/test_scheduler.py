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
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from reaper.api.runs import ReapStatus
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.models import Instance, InstanceKind
from reaper.db.session import create_engine, create_session_factory
from reaper.main import create_app
from reaper.secrets import resolve_secret_key
from reaper.services import app_settings, imdb_dataset, retention, scan_runner, scheduler
from reaper.services.imdb_dataset import ImdbRatings
from reaper.services.update_check import UpdateChecker, UpdateStatus


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
            settings=settings,
            update_checker=UpdateChecker(),
            timezone=ZoneInfo("UTC"),
            reap_running=lambda: False,
        )
        # build_scheduler returns an unstarted scheduler; jobs are inspectable without
        # starting it, and there is nothing to shut down.
        job_ids = {job.id for job in sched.get_jobs()}
        assert job_ids == {
            "refresh_ratings",
            "refresh_curated_lists",
            "full_history_sweep",
            "check_for_updates",
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
                or "sweep" in job.func.__name__
                # The update check reads one anonymous GitHub URL and writes nothing
                # anywhere -- no credentials, no client that can mutate, no *arr and no
                # Plex (`scheduler.check_for_updates`). It earns the fourth verb rather
                # than being renamed to fit one of the three.
                or job.func.__name__ == "check_for_updates"
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
            settings=settings,
            update_checker=UpdateChecker(),
            timezone=ZoneInfo("UTC"),
            reap_running=lambda: False,
        )
        job = next(j for j in sched.get_jobs() if j.id == scheduler.SNAPSHOT_SWEEP_JOB_ID)
        wait = (job.trigger.start_date - utcnow()).total_seconds()

        assert 0 < wait <= scheduler.SNAPSHOT_SWEEP_STARTUP_DELAY_S
        assert wait < scheduler.SNAPSHOT_SWEEP_INTERVAL_S
        await engine.dispose()

    async def test_the_snapshot_sweep_never_settles_on_a_fixed_time_of_day(
        self, tmp_path: Path
    ) -> None:
        """Twelve hours is an exact multiple of an hour, so an unjittered interval fires at
        the same second forever -- and a firing that runs late does not move it, because the
        next one is computed from the scheduled time rather than the actual one. That was
        harmless until the compaction was gated on a live scan or reap (#325): a scan on a
        cron whose period divides twelve hours now collides with every firing or with none,
        and a collision means the vacuum never runs again rather than running twelve hours
        later, so an upgraded install's freed pages stay on disk for the life of the process.

        Asserted on the fire times, not on the trigger's ``jitter`` attribute: the attribute
        reads the same whether or not the trigger honors it (rule 118). Consecutive firings
        must land at different offsets within the interval, and each gap must still sit
        inside one interval plus the spread, so a jitter large enough to reorder firings or
        small enough to round away would both fail."""
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        engine = create_engine(settings)
        sched = scheduler.build_scheduler(
            engine,
            tmp_path,
            session_factory=create_session_factory(engine),
            secret_box=SecretBox(resolve_secret_key(settings)),
            settings=settings,
            update_checker=UpdateChecker(),
            timezone=ZoneInfo("UTC"),
            reap_running=lambda: False,
        )
        trigger = next(
            j for j in sched.get_jobs() if j.id == scheduler.SNAPSHOT_SWEEP_JOB_ID
        ).trigger

        fires = []
        previous = None
        for _ in range(6):
            previous = trigger.get_next_fire_time(previous, trigger.start_date)
            fires.append(previous)

        phases = {
            (fire - trigger.start_date).total_seconds() % scheduler.SNAPSHOT_SWEEP_INTERVAL_S
            for fire in fires
        }
        assert len(phases) == len(fires)
        for earlier, later in pairwise(fires):
            gap = (later - earlier).total_seconds()
            assert (
                scheduler.SNAPSHOT_SWEEP_INTERVAL_S
                <= gap
                <= scheduler.SNAPSHOT_SWEEP_INTERVAL_S + scheduler.SNAPSHOT_SWEEP_JITTER_S
            )
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
            settings=settings,
            update_checker=UpdateChecker(),
            timezone=ZoneInfo("UTC"),
            reap_running=lambda: False,
        )

        job = next(j for j in sched.get_jobs() if j.id == scheduler.SNAPSHOT_SWEEP_JOB_ID)

        assert list(job.args)[:2] == [factory, sweep_dir]
        await engine.dispose()

    async def test_the_snapshot_sweep_is_handed_a_way_to_ask_whether_a_reap_is_live(
        self, tmp_path: Path
    ) -> None:
        """A reap's live flag lives on app state, which the scheduler has no handle on, so it
        arrives as a predicate. Wired wrong it would still have the right arity, and the job
        would compact straight through a reap (#325).

        The predicate is passed answering True, which no default could produce -- a job wired
        to a placeholder would read False here (rule 141)."""
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        engine = create_engine(settings)
        sched = scheduler.build_scheduler(
            engine,
            tmp_path,
            session_factory=create_session_factory(engine),
            secret_box=SecretBox(resolve_secret_key(settings)),
            settings=settings,
            update_checker=UpdateChecker(),
            timezone=ZoneInfo("UTC"),
            reap_running=lambda: True,
        )

        job = next(j for j in sched.get_jobs() if j.id == scheduler.SNAPSHOT_SWEEP_JOB_ID)

        assert job.args[2]() is True
        await engine.dispose()

    def test_the_predicate_the_real_app_wires_reads_the_live_reap(self, tmp_path: Path) -> None:
        """The test above proves the job calls whatever it was handed, which a placeholder
        would satisfy just as well. This boots the real app and pins the whole chain --
        ``main`` to ``api.runs.reap_in_flight`` to ``app.state.reap_status`` -- so the
        interlock cannot be wired to something that never says True (#325).

        Driven in both states off one predicate object, because a lambda captured before the
        status existed would answer False forever and read as correct in the False case."""
        settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
        sync_engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(sync_engine)
        sync_engine.dispose()

        app = create_app(settings)
        with TestClient(app):
            job = next(
                j for j in app.state.scheduler.get_jobs() if j.id == scheduler.SNAPSHOT_SWEEP_JOB_ID
            )
            asks_the_app = job.args[2]

            assert asks_the_app() is False
            app.state.reap_status = ReapStatus(running=True)
            assert asks_the_app() is True

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

        await scheduler.sweep_old_snapshots(None, tmp_path, lambda: False)  # type: ignore[arg-type]

        assert attempted == [tmp_path]

    @pytest.mark.parametrize("live", ["scan", "reap"])
    async def test_compaction_yields_to_a_live_scan_or_reap_but_the_sweep_still_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: str
    ) -> None:
        """``VACUUM`` rewrites the whole file under the write lock and every app connection
        waits only 5s for it (``db.session``). Measured on local NVMe with the app's own
        pragmas, a 2.4 GB database vacuums for 8s and the concurrent write fails -- a size
        inside the range ``retention.KEEP_SNAPSHOTS`` documents as the steady state, so it
        needs no unusual storage. A scan loses every source read it made, since it commits
        once at the end; a reap loses a journal step and wedges (#325, #327).

        Both arms are pinned because they read two different flags from two different homes,
        so one wired and one not is the likely half-fix. The sweep itself is asserted to have
        run in both: its batches are short writes, and skipping them would let the database
        grow without limit again (#315), which is a bigger harm than a deferred vacuum."""
        swept, attempted = [], []

        async def _sweep(_factory: object) -> int:
            swept.append(True)
            return 3

        async def _compact(data_dir: Path) -> bool:
            attempted.append(data_dir)
            return True

        monkeypatch.setattr(retention, "sweep_old_snapshots", _sweep)
        monkeypatch.setattr(retention, "compact_if_fragmented", _compact)
        monkeypatch.setattr(scan_runner, "_scan_running", live == "scan")

        await scheduler.sweep_old_snapshots(  # type: ignore[arg-type]
            None, tmp_path, lambda: live == "reap"
        )

        assert swept == [True]
        assert attempted == []

    async def test_a_sweep_that_raises_does_not_escape_the_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A database busy with a scan must not stop the scheduler, and nothing downstream
        depends on this having run -- the worst case of a skipped firing is that the next
        one has one more scan to drop."""

        async def _boom(_factory: object) -> int:
            raise OSError("database is locked")

        monkeypatch.setattr(retention, "sweep_old_snapshots", _boom)

        await scheduler.sweep_old_snapshots(None, tmp_path, lambda: False)  # type: ignore[arg-type]

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

    @staticmethod
    def _wire_lists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Settings, SecretBox]:
        """Stand in for the *arr and Plex clients the pass builds, and hand back its config.

        The job reads every source now, not just the IMDb mirror, so it goes through
        ``scan_runner.build_sources`` the way the Lists screen's own check does. What each
        test below is about is the bookkeeping the pass records, so the clients are stubbed
        and ``sync_protection_lists`` is what carries the outcome.
        """

        async def no_sources(*args: object, **kwargs: object) -> tuple[object, ...]:
            return ([], [], None, [], None)

        monkeypatch.setattr(scheduler.scan_runner, "build_sources", no_sources)
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        return settings, SecretBox(resolve_secret_key(settings))

    async def test_a_successful_list_refresh_records_ok(
        self,
        cache_engine: AsyncEngine,
        main_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings, box = self._wire_lists(monkeypatch, tmp_path)

        async def fake_sync(*args: object, **kwargs: object) -> dict[str, int]:
            return {"imdb-top250-list1": 250}

        monkeypatch.setattr(scheduler.snapshot_service, "sync_protection_lists", fake_sync)
        await scheduler.refresh_curated_lists(cache_engine, main_factory, settings, box)

        last = await self._last(main_factory, "refresh_curated_lists")
        assert last == {"at": last["at"], "ok": True, "result": "Lists refreshed"}  # type: ignore[index]

    async def test_a_failed_list_refresh_records_not_ok(
        self,
        cache_engine: AsyncEngine,
        main_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every list in the pass came back an error, so the line says so outright.

        ``sync_protection_lists`` does not raise on a source that is down -- it records the
        reason per slug and leaves that list's stored membership alone (rule 2) -- so the
        failure reaches this job as an error VALUE, never as an exception.
        """
        settings, box = self._wire_lists(monkeypatch, tmp_path)

        async def all_bad(*args: object, **kwargs: object) -> dict[str, str]:
            return {"imdb-top250-list1": "error: source down"}

        monkeypatch.setattr(scheduler.snapshot_service, "sync_protection_lists", all_bad)
        await scheduler.refresh_curated_lists(cache_engine, main_factory, settings, box)

        last = await self._last(main_factory, "refresh_curated_lists")
        assert last is not None
        assert last["ok"] is False
        assert last["result"] == "Couldn't refresh lists"

    async def test_one_failing_list_is_counted_beside_the_ones_that_worked(
        self,
        cache_engine: AsyncEngine,
        main_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A partial pass names both halves. One list down must not read as a total failure:
        the two that refreshed really did, and their titles are protected on fresh membership.
        ``sync_protection_lists`` guards per provider, so one bad list never ends the pass."""
        settings, box = self._wire_lists(monkeypatch, tmp_path)

        async def one_bad(*args: object, **kwargs: object) -> dict[str, int | str]:
            return {
                "imdb-top250-list1": "error: that list is gone upstream",
                "imdb-top250-list2": 250,
                "plex-collection-never-reap-list3": 12,
            }

        monkeypatch.setattr(scheduler.snapshot_service, "sync_protection_lists", one_bad)
        await scheduler.refresh_curated_lists(cache_engine, main_factory, settings, box)

        last = await self._last(main_factory, "refresh_curated_lists")
        assert last is not None
        assert last["ok"] is False
        assert last["result"] == "Refreshed 2 lists, 1 couldn't be checked"

    async def test_a_pass_that_cannot_reach_its_sources_records_not_ok(
        self,
        cache_engine: AsyncEngine,
        main_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A misconfigured install cannot build clients at all, and that is a refusal the job
        records rather than an exception escaping into the scheduler unrecorded."""

        # Its own stub, not `_wire_lists`: this one is about `build_sources` REFUSING.
        settings, box = self._wire_lists(monkeypatch, tmp_path)

        async def refuse(*args: object, **kwargs: object) -> tuple[object, ...]:
            raise scheduler.scan_runner.ScanConfigError("no sources configured")

        monkeypatch.setattr(scheduler.scan_runner, "build_sources", refuse)
        await scheduler.refresh_curated_lists(cache_engine, main_factory, settings, box)

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


class TestTheUpdateCheckJob:
    """The check runs on a schedule now, which is the whole point of the job: before it,
    ``UpdateChecker.status()`` had one caller -- the About route -- so an install nobody
    signed in to never checked at all, under a panel saying Reaper checked a few times a
    day (#464). These pin what each state writes to the Jobs page's last-run line, and
    that the job asks rather than repeating a cached answer."""

    async def _last(
        self, factory: async_sessionmaker[AsyncSession], job_id: str
    ) -> dict[str, object] | None:
        async with factory() as session:
            return (await app_settings.get_job_last_runs(session)).get(job_id)

    @staticmethod
    def _checker(status: UpdateStatus) -> object:
        """A checker that answers ``status`` from ``refresh`` and counts both doors, so a
        job rewired to the cache-serving one fails here rather than reporting a six-hour-old
        answer as a check that just ran."""

        class _Stub:
            def __init__(self) -> None:
                self.refreshed = 0
                self.statused = 0

            async def refresh(self) -> UpdateStatus:
                self.refreshed += 1
                return status

            async def status(self) -> UpdateStatus:
                self.statused += 1
                return status

        return _Stub()

    async def test_a_newer_release_is_recorded_and_logged_for_a_headless_install(
        self, main_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The log line is the only place this lands on a server nobody opens, so it is INFO
        rather than DEBUG.

        Read off a stub logger rather than ``capture_logs``: a module logger that was
        materialized while ``cache_logger_on_first_use`` was live is permanently deaf to it
        (conftest's ``_capturable_logs``), and this module's logger is used by half the suite,
        so the assertion would pass or fail on which tests shared the worker (rule 119/133)."""
        said: list[str] = []

        class _Recorder:
            def info(self, event: str, **_kw: object) -> None:
                said.append(event)

            def warning(self, event: str, **_kw: object) -> None:
                said.append(event)

        monkeypatch.setattr(scheduler, "log", _Recorder())
        checker = self._checker(
            UpdateStatus(
                channel="release",
                enabled=True,
                current="2026.8.1",
                latest="2026.9.1",
                update_available=True,
            )
        )
        await scheduler.check_for_updates(checker, main_factory)  # type: ignore[arg-type]

        assert checker.refreshed == 1  # type: ignore[attr-defined]
        assert checker.statused == 0  # type: ignore[attr-defined]
        assert "scheduler.update_available" in said
        last = await self._last(main_factory, "check_for_updates")
        assert last is not None
        assert last["ok"] is True
        assert last["result"] == "Reaper 2026.9.1 is out"

    async def test_being_current_records_a_plain_success(
        self, main_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        checker = self._checker(
            UpdateStatus(
                channel="release",
                enabled=True,
                current="2026.8.1",
                latest="2026.8.1",
                update_available=False,
            )
        )
        await scheduler.check_for_updates(checker, main_factory)  # type: ignore[arg-type]

        last = await self._last(main_factory, "check_for_updates")
        assert last is not None
        assert last["ok"] is True
        assert last["result"] == "You are on the newest release"

    async def test_a_moved_dev_branch_reads_as_the_dev_branch_not_a_release(
        self, main_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A dev build has no release number to name, so the release sentence would print
        "Reaper dev (def5678) is out" at an operator who is not on the release channel."""
        checker = self._checker(
            UpdateStatus(
                channel="dev",
                enabled=True,
                current="dev (abc1234)",
                latest="dev (def5678)",
                update_available=True,
            )
        )
        await scheduler.check_for_updates(checker, main_factory)  # type: ignore[arg-type]

        last = await self._last(main_factory, "check_for_updates")
        assert last is not None
        assert last["result"] == "The dev branch has moved since this build"

    async def test_an_unanswerable_check_records_a_failure_not_a_green_tick(
        self, main_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Unreachable, rate-limited, or an unorderable version pair: the checker maps all
        three to unknown, and unknown is not "you are up to date"."""
        checker = self._checker(UpdateStatus(channel="release", enabled=True, current="2026.8.1"))
        await scheduler.check_for_updates(checker, main_factory)  # type: ignore[arg-type]

        last = await self._last(main_factory, "check_for_updates")
        assert last is not None
        assert last["ok"] is False
        assert last["result"] == "Couldn't check for updates"

    async def test_the_off_switch_is_recorded_as_off_never_as_a_check_that_ran(
        self, main_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Rule 55: ``REAPER_UPDATE_CHECK=false`` governs the scheduled path too. The
        checker answers disabled without sending anything, and the run says so rather than
        reading as a check that found nothing."""
        checker = self._checker(UpdateStatus(channel="release", enabled=False, current="2026.8.1"))
        await scheduler.check_for_updates(checker, main_factory)  # type: ignore[arg-type]

        last = await self._last(main_factory, "check_for_updates")
        assert last is not None
        assert last["ok"] is True
        assert last["result"] == "Update checks are off"

    async def test_an_unexpected_crash_is_recorded_rather_than_stopping_the_scheduler(
        self, main_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        class _Boom:
            async def refresh(self) -> UpdateStatus:
                raise RuntimeError("something nobody mapped")

        await scheduler.check_for_updates(_Boom(), main_factory)  # type: ignore[arg-type]

        last = await self._last(main_factory, "check_for_updates")
        assert last is not None
        assert last["ok"] is False
        assert last["result"] == "Couldn't check for updates"


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
