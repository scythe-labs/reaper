# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background maintenance: keeping the caches fresh so a scan can actually judge.

Reaper's scoring rests on data that goes stale: the IMDb ratings dataset (refreshed
daily at the source) and the curated protection lists. If nobody refreshes them, a
fresh install degrades every snapshot forever -- the ratings gate cannot evaluate, so
nothing may be reaped -- and an old install slowly drifts onto stale ratings.

So this scheduler exists to do the unglamorous upkeep:

* **Refresh the IMDb ratings dataset** nightly, and **once on startup if it is stale or
  missing**. The startup catch-up is what makes a fresh install work: without it the
  first scan (and every scan until a day boundary happened to pass) would degrade.
* **Refresh the curated lists** (the IMDb Top 250) daily, independent of scans.

**This scheduler never deletes anything.** It only downloads and caches. Deletion runs
happen through the executor, under the destructive-action guard, and are not scheduled
here -- automated deletion is an M8 concern gated behind an earned autonomy grant, and
wiring it to a timer before that machinery exists would be exactly the wrong shortcut.

APScheduler 3.x (4.x is still alpha). ``AsyncIOScheduler`` shares the app's event loop,
and ``coalesce=True`` + ``max_instances=1`` mean a job that overruns its interval -- a
first ratings load can take a while -- never stacks a second copy on top of itself.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from reaper.clients.tautulli import TautulliClient
from reaper.config import RuntimeSafety, Settings
from reaper.crypto import SecretBox
from reaper.db.models import Instance, InstanceKind
from reaper.services import history_sync, imdb_dataset, lists, scan_runner
from reaper.services.imdb_dataset import ImdbRatings

log = structlog.get_logger(__name__)

#: The job id for the optional automatic scan. One job, reconciled in place when the
#: owner changes the schedule -- never stacked.
SCAN_JOB_ID = "scheduled_scan"


async def refresh_ratings(cache_engine: AsyncEngine, data_dir: Path) -> None:
    """Download and load the IMDb ratings dataset. Idempotent; safe to run any time."""
    try:
        rows = await imdb_dataset.refresh(cache_engine, data_dir)
        log.info("scheduler.ratings_refreshed", rows=rows)
    except Exception as exc:
        # Leaves the previous dataset in place (load swaps atomically). A stale dataset
        # is caught by the snapshot's own degradation check; a crashed scheduler would
        # silently stop all upkeep, which is worse.
        log.warning("scheduler.ratings_refresh_failed", error=str(exc))


async def refresh_curated_lists(cache_engine: AsyncEngine) -> None:
    """Refresh the curated protection lists that need no per-scan client (the Top 250)."""
    try:
        count = await lists.sync(cache_engine, lists.ImdbTop250(), mode=lists.ListMode.HARD)
        log.info("scheduler.lists_refreshed", **{lists.ImdbTop250().slug: count})
    except Exception as exc:
        log.warning("scheduler.lists_refresh_failed", error=str(exc))


async def full_history_sweep(
    session_factory: async_sessionmaker[AsyncSession],
    cache_engine: AsyncEngine,
    secret_box: SecretBox,
) -> None:
    """A nightly FULL re-walk of Tautulli's history, to catch backfilled old events.

    Per-scan syncs are incremental (fast, date-filtered) and therefore blind to a row
    Tautulli backfills with an *old* timestamp -- a manual history import, or a delayed
    play. This full sweep re-reads everything and reconciles, so any such row is picked
    up within a day. It also re-runs the regression check against Tautulli's real total.

    Read-only. If no Tautulli is configured, there is nothing to sweep.
    """
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    select(Instance).where(
                        Instance.kind == InstanceKind.TAUTULLI, Instance.enabled.is_(True)
                    )
                )
            )
            .scalars()
            .first()
        )

    if row is None:
        return

    client = TautulliClient(
        row.base_url,
        secret_box.decrypt(row.api_key_enc),
        safety=RuntimeSafety(destructive_enabled=False),
    )
    try:
        async with client:
            state = await history_sync.sync(cache_engine, client, full=True)
        log.info("scheduler.history_swept", rows=state.rows)
    except Exception as exc:
        log.warning("scheduler.history_sweep_failed", error=str(exc))


async def scheduled_scan(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    cache_engine: AsyncEngine,
    secret_box: SecretBox,
) -> None:
    """Run one automatic scan. Read-only, exactly like the button in the UI.

    A scan never deletes -- it refreshes the review queue -- so scheduling one is safe and
    needs no arming. A misconfigured install (no Radarr/Tautulli yet) is a quiet skip, not
    an error: the schedule may have been set before the services were added.
    """
    try:
        snapshot = await scan_runner.run_scan(
            settings=settings,
            session_factory=session_factory,
            cache_engine=cache_engine,
            box=secret_box,
        )
        log.info("scheduler.scan_complete", snapshot=snapshot.id, items=snapshot.item_count)
    except scan_runner.ScanConfigError as exc:
        log.info("scheduler.scan_skipped", reason=str(exc))
    except Exception as exc:
        log.warning("scheduler.scan_failed", error=str(exc))


def apply_scan_schedule(
    scheduler: AsyncIOScheduler,
    cron: str | None,
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    cache_engine: AsyncEngine,
    secret_box: SecretBox,
) -> None:
    """Reconcile the automatic-scan job to a cron string (or remove it if ``None``).

    ``cron`` is a standard 5-field crontab expression. A malformed one raises
    ``ValueError`` (surfaced to the caller as a 422) rather than being silently dropped --
    an owner who thinks they scheduled a nightly scan should not find nothing ran.
    """
    if cron is None:
        if scheduler.get_job(SCAN_JOB_ID) is not None:
            scheduler.remove_job(SCAN_JOB_ID)
        return

    trigger = CronTrigger.from_crontab(cron)  # ValueError on a bad expression
    scheduler.add_job(
        scheduled_scan,
        trigger,
        args=[settings, session_factory, cache_engine, secret_box],
        id=SCAN_JOB_ID,
        replace_existing=True,
    )
    log.info("scheduler.scan_scheduled", cron=cron)


async def catch_up_on_startup(cache_engine: AsyncEngine, data_dir: Path) -> None:
    """Run the refreshes that a fresh (or long-idle) install needs *now*, not at 3am.

    Only refreshes the ratings dataset if it is actually stale or missing, so a warm
    install that restarts does not re-download 280 MB it already has. This is the single
    step that turns "fresh install degrades every snapshot" into "fresh install works".
    """
    state = await ImdbRatings(cache_engine).state()
    if state.degraded():
        log.info("scheduler.ratings_stale_on_startup", rows=state.row_count)
        await refresh_ratings(cache_engine, data_dir)


def build_scheduler(
    cache_engine: AsyncEngine,
    data_dir: Path,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    secret_box: SecretBox,
) -> AsyncIOScheduler:
    """Wire the nightly jobs. The caller starts it and holds it on app state.

    Times are staggered and in UTC. IMDb publishes the dataset once a day; there is no
    value in hammering it, and 03:30 keeps the heavy download off peak viewing hours. The
    history sweep runs a little later still, since it is a full re-walk of Tautulli.
    """
    scheduler = AsyncIOScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600}
    )

    scheduler.add_job(
        refresh_ratings,
        CronTrigger(hour=3, minute=30),
        args=[cache_engine, data_dir],
        id="refresh_ratings",
        replace_existing=True,
    )
    scheduler.add_job(
        refresh_curated_lists,
        CronTrigger(hour=3, minute=45),
        args=[cache_engine],
        id="refresh_curated_lists",
        replace_existing=True,
    )
    scheduler.add_job(
        full_history_sweep,
        CronTrigger(hour=4, minute=0),
        args=[session_factory, cache_engine, secret_box],
        id="full_history_sweep",
        replace_existing=True,
    )
    return scheduler
