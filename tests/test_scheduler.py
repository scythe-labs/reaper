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
from reaper.db.session import create_engine, create_session_factory
from reaper.secrets import resolve_secret_key
from reaper.services import app_settings, scheduler
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
        assert job_ids == {"refresh_ratings", "refresh_curated_lists", "full_history_sweep"}
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
    Leaving Soon keep their own last-run sources, so they are not exercised here."""

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

        async def fake_download(engine: AsyncEngine, data_dir: Path) -> int:
            return 7

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
