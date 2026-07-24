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

from datetime import timedelta, tzinfo
from pathlib import Path

import structlog
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_SUBMITTED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from reaper.clients.tautulli import TautulliClient
from reaper.clock import utcnow
from reaper.config import RuntimeSafety, Settings
from reaper.crypto import SecretBox
from reaper.db.models import Instance, InstanceKind
from reaper.services import app_settings, history_sync, imdb_dataset, lists, scan_runner
from reaper.services.imdb_dataset import ImdbRatings

log = structlog.get_logger(__name__)

#: The job id for the optional automatic scan. One job, reconciled in place when the
#: owner changes the schedule -- never stacked.
SCAN_JOB_ID = "scheduled_scan"

#: The background upkeep jobs and the cron they run on out of the box. Every one is
#: read-only (refresh/sweep), staggered off peak hours, and now operator-editable: a
#: stored override (see ``app_settings.get_maintenance_schedules``) wins over these, and
#: an owner may turn any of them off entirely. Absence of a stored value falls back here.
DEFAULT_MAINTENANCE_CRONS: dict[str, str] = {
    "refresh_ratings": "30 3 * * *",
    "refresh_curated_lists": "45 3 * * *",
    "full_history_sweep": "0 4 * * *",
}

#: The upkeep jobs, in display order. The scan is scheduled separately (its own key).
MAINTENANCE_JOB_IDS: tuple[str, ...] = tuple(DEFAULT_MAINTENANCE_CRONS)

#: Every job whose schedule the owner may edit, scan first. Drives the Jobs settings list.
SCHEDULABLE_JOB_IDS: tuple[str, ...] = (SCAN_JOB_ID, *MAINTENANCE_JOB_IDS)


#: Skip a scheduled ratings refresh when the dataset was synced this recently. IMDb
#: publishes the ratings dataset once a day, so a re-download inside a day fetches identical
#: bytes: this is what makes an aggressive schedule (the shared presets go down to hourly)
#: harmless -- roughly one download a day whatever the cron -- rather than 24 full downloads
#: for no new data. A day-apart schedule is always older than this, so it always runs.
RATINGS_MIN_REFRESH_INTERVAL = timedelta(hours=20)


async def _record_run(
    session_factory: async_sessionmaker[AsyncSession] | None,
    job_id: str,
    *,
    ok: bool,
    result: str,
) -> None:
    """Persist an upkeep job's last completion so the Jobs page can show its last-run line.

    A no-op when ``session_factory`` is ``None`` -- the startup catch-up calls the job
    callables directly, without one, and a catch-up refresh is not an on-schedule/by-hand run.
    Never lets this bookkeeping break the job it records: a failed write is logged and
    swallowed, exactly like each job's own error handling.
    """
    if session_factory is None:
        return
    try:
        async with session_factory() as session:
            await app_settings.set_job_last_run(
                session, job_id, at=utcnow().isoformat(), ok=ok, result=result
            )
            await session.commit()
    except Exception as exc:
        log.warning("scheduler.record_run_failed", job=job_id, error=str(exc))


async def refresh_ratings(
    cache_engine: AsyncEngine,
    data_dir: Path,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Download and load the IMDb ratings dataset. Idempotent; safe to run any time.

    Short-circuits when the dataset was refreshed within ``RATINGS_MIN_REFRESH_INTERVAL``, so
    an aggressive schedule cannot re-pull the same daily-published data on repeat. The startup
    catch-up gates on the 14-day staleness itself, so a genuinely stale dataset (which is far
    older than the window) still refreshes there.
    """
    try:
        state = await ImdbRatings(cache_engine).state()
        if (
            state.synced_at is not None
            and utcnow() - state.synced_at < RATINGS_MIN_REFRESH_INTERVAL
        ):
            log.info("scheduler.ratings_fresh_skip", synced_at=state.synced_at.isoformat())
            await _record_run(
                session_factory, "refresh_ratings", ok=True, result="Already up to date"
            )
            return
        rows = await imdb_dataset.refresh(cache_engine, data_dir)
        log.info("scheduler.ratings_refreshed", rows=rows)
        await _record_run(session_factory, "refresh_ratings", ok=True, result="Ratings refreshed")
    except Exception as exc:
        # Leaves the previous dataset in place (load swaps atomically). A stale dataset
        # is caught by the snapshot's own degradation check; a crashed scheduler would
        # silently stop all upkeep, which is worse. The state read is inside this try too,
        # so a broken cache (locked/corrupt cache.db) is recorded as a failed run instead of
        # escaping unrecorded.
        log.warning("scheduler.ratings_refresh_failed", error=str(exc))
        await _record_run(
            session_factory, "refresh_ratings", ok=False, result="Couldn't refresh ratings"
        )


async def refresh_curated_lists(
    cache_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Refresh the curated protection lists that need no per-scan client (the Top 250)."""
    try:
        count = await lists.sync(cache_engine, lists.ImdbTop250(), mode=lists.ListMode.HARD)
        log.info("scheduler.lists_refreshed", **{lists.ImdbTop250().slug: count})
        await _record_run(
            session_factory, "refresh_curated_lists", ok=True, result="Lists refreshed"
        )
    except Exception as exc:
        log.warning("scheduler.lists_refresh_failed", error=str(exc))
        await _record_run(
            session_factory, "refresh_curated_lists", ok=False, result="Couldn't refresh lists"
        )


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
    try:
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
            await _record_run(
                session_factory, "full_history_sweep", ok=True, result="No history source"
            )
            return

        client = TautulliClient(
            row.base_url,
            secret_box.decrypt(row.api_key_enc),
            safety=RuntimeSafety(destructive_enabled=False),
            verify=row.verify_tls,
        )
        async with client:
            state = await history_sync.sync(cache_engine, client, full=True)
        log.info("scheduler.history_swept", rows=state.rows)
        await _record_run(session_factory, "full_history_sweep", ok=True, result="History updated")
    except Exception as exc:
        # The instance lookup and client construction are inside this try too, so a broken
        # DB read or a bad decrypt is recorded as a failed run instead of escaping unrecorded.
        log.warning("scheduler.history_sweep_failed", error=str(exc))
        await _record_run(
            session_factory, "full_history_sweep", ok=False, result="Couldn't update history"
        )


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
    except scan_runner.ScanInProgressError:
        # A scan started from the browser is still running; landing a second one on top
        # would double-read every source and race the grace clock. Skip this firing --
        # the next scheduled one runs normally.
        log.info("scheduler.scan_skipped", reason="a scan is already running")
    except scan_runner.ScanConfigError as exc:
        log.info("scheduler.scan_skipped", reason=str(exc))
    except Exception as exc:
        # A genuine crash (unlike the two quiet skips above) writes no snapshot, so the
        # Jobs page would otherwise keep showing whatever the last snapshot said forever.
        # Recorded here so ScanRow can prefer this over a stale snapshot (see get_schedule).
        log.warning("scheduler.scan_failed", error=str(exc))
        await _record_run(session_factory, SCAN_JOB_ID, ok=False, result="Scan failed")


def apply_scan_schedule(
    scheduler: AsyncIOScheduler,
    cron: str | None,
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    cache_engine: AsyncEngine,
    secret_box: SecretBox,
    timezone: tzinfo,
) -> None:
    """Reconcile the automatic-scan job to a cron string (or remove it if ``None``).

    ``cron`` is a standard 5-field crontab expression, read in ``timezone`` -- the server
    zone from ``app_settings.get_timezone`` -- so "0 2 * * *" fires at 2 AM there, not in
    the container's own zone. A malformed cron raises ``ValueError`` (surfaced to the caller
    as a 422) rather than being silently dropped -- an owner who thinks they scheduled a
    nightly scan should not find nothing ran.
    """
    if cron is None:
        if scheduler.get_job(SCAN_JOB_ID) is not None:
            scheduler.remove_job(SCAN_JOB_ID)
        return

    trigger = CronTrigger.from_crontab(cron, timezone=timezone)  # ValueError on a bad expression
    scheduler.add_job(
        scheduled_scan,
        trigger,
        args=[settings, session_factory, cache_engine, secret_box],
        id=SCAN_JOB_ID,
        replace_existing=True,
    )
    log.info("scheduler.scan_scheduled", cron=cron)


def _maintenance_specs(
    cache_engine: AsyncEngine,
    data_dir: Path,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    secret_box: SecretBox,
) -> dict[str, tuple[object, list[object]]]:
    """The (callable, args) each upkeep job is added with. One place, so wiring a job at
    build time and re-wiring it on a schedule change can never drift apart."""
    return {
        "refresh_ratings": (refresh_ratings, [cache_engine, data_dir, session_factory]),
        "refresh_curated_lists": (refresh_curated_lists, [cache_engine, session_factory]),
        "full_history_sweep": (full_history_sweep, [session_factory, cache_engine, secret_box]),
    }


def effective_maintenance_cron(job_id: str, stored: dict[str, str | None]) -> str | None:
    """The cron a job actually runs on: a stored override (which may be ``None`` for off)
    wins; an absent job falls back to its built-in default."""
    if job_id in stored:
        return stored[job_id]
    return DEFAULT_MAINTENANCE_CRONS.get(job_id)


def apply_maintenance_schedule(
    scheduler: AsyncIOScheduler,
    job_id: str,
    cron: str | None,
    *,
    cache_engine: AsyncEngine,
    data_dir: Path,
    session_factory: async_sessionmaker[AsyncSession],
    secret_box: SecretBox,
    timezone: tzinfo,
) -> None:
    """Reconcile one upkeep job to a cron string, or remove it when ``cron`` is ``None``.

    ``cron`` is read in ``timezone`` (the server zone), so every timed job shares one clock
    with the scan. ``None`` means the owner turned the job off; the job is dropped from the
    scheduler but can still be run once by hand (see :func:`run_maintenance_now`). A malformed
    cron raises ``ValueError`` (surfaced as a 422) rather than being silently dropped.
    """
    specs = _maintenance_specs(
        cache_engine, data_dir, session_factory=session_factory, secret_box=secret_box
    )
    if job_id not in specs:
        raise KeyError(job_id)
    func, args = specs[job_id]
    if cron is None:
        if scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)
        log.info("scheduler.maintenance_off", job=job_id)
        return
    trigger = CronTrigger.from_crontab(cron, timezone=timezone)  # ValueError on a bad expression
    scheduler.add_job(func, trigger, args=args, id=job_id, replace_existing=True)
    log.info("scheduler.maintenance_scheduled", job=job_id, cron=cron)


def run_maintenance_now(
    scheduler: AsyncIOScheduler,
    job_id: str,
    *,
    cache_engine: AsyncEngine,
    data_dir: Path,
    session_factory: async_sessionmaker[AsyncSession],
    secret_box: SecretBox,
) -> None:
    """Fire an upkeep job immediately, whether or not it is on a schedule.

    A scheduled job is nudged in place (its cron is untouched, so the next regular run still
    happens). A job the owner turned off is run as a one-shot that removes itself afterward,
    so "run now" never quietly turns the schedule back on.
    """
    specs = _maintenance_specs(
        cache_engine, data_dir, session_factory=session_factory, secret_box=secret_box
    )
    if job_id not in specs:
        raise KeyError(job_id)
    func, args = specs[job_id]
    job = scheduler.get_job(job_id)
    if job is not None:
        job.modify(next_run_time=utcnow())
    else:
        scheduler.add_job(
            func, "date", run_date=utcnow(), args=args, id=job_id, replace_existing=True
        )


def track_running_jobs(scheduler: AsyncIOScheduler) -> set[str]:
    """Return a live set of the job ids currently executing.

    APScheduler emits ``SUBMITTED`` when a job hands off to the executor and
    ``EXECUTED``/``ERROR`` when it finishes; mirroring those into a set gives the Jobs page an
    honest "running now" signal for each job without inventing a status store. Register this
    before the scheduler starts so the very first firing is seen.
    """
    running: set[str] = set()
    scheduler.add_listener(lambda e: running.add(e.job_id), EVENT_JOB_SUBMITTED)
    scheduler.add_listener(
        lambda e: running.discard(e.job_id), EVENT_JOB_EXECUTED | EVENT_JOB_ERROR
    )
    return running


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
    timezone: tzinfo,
) -> AsyncIOScheduler:
    """Wire the nightly jobs. The caller starts it and holds it on app state.

    Times are staggered and run in ``timezone`` -- the server zone from
    ``app_settings.get_timezone`` -- so 03:30 means 03:30 there, the same clock the scan
    uses. IMDb publishes the dataset once a day; there is no value in hammering it, and 03:30
    keeps the heavy download off peak viewing hours. The history sweep runs a little later
    still, since it is a full re-walk of Tautulli.
    """
    scheduler = AsyncIOScheduler(
        timezone=timezone,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )

    specs = _maintenance_specs(
        cache_engine, data_dir, session_factory=session_factory, secret_box=secret_box
    )
    for job_id, cron in DEFAULT_MAINTENANCE_CRONS.items():
        func, args = specs[job_id]
        scheduler.add_job(
            func,
            CronTrigger.from_crontab(cron, timezone=timezone),
            args=args,
            id=job_id,
            replace_existing=True,
        )
    return scheduler


def reschedule_timezone(
    scheduler: AsyncIOScheduler,
    timezone: tzinfo,
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    cache_engine: AsyncEngine,
    secret_box: SecretBox,
    data_dir: Path,
    scan_cron: str | None,
    maintenance: dict[str, str | None],
) -> None:
    """Re-apply every timed job under a new server time zone, in place.

    Each cron trigger carries its own zone, so moving the clock means rebuilding every
    trigger -- the scan and all upkeep jobs -- with the new one. The stored crons decide what
    to rebuild: a job the owner turned off stays off, an overridden one keeps its override,
    and an untouched one falls back to its default. Called when the time zone changes in the
    UI so every "next run" recomputes immediately; startup wires the same jobs directly.

    Each ``apply_*`` is wrapped in the same ``ValueError`` guard startup uses (rule 87): a
    stored-but-malformed cron (hand-edited, or a future parser tightening) is logged and
    skipped, so one bad cron can never 500 the timezone save or half-apply the zone -- moving
    some jobs and leaving the rest -- which is exactly what boot already survives.
    """
    try:
        apply_scan_schedule(
            scheduler,
            scan_cron,
            settings=settings,
            session_factory=session_factory,
            cache_engine=cache_engine,
            secret_box=secret_box,
            timezone=timezone,
        )
    except ValueError:
        log.warning("scheduler.bad_scan_cron", cron=scan_cron)
    for job_id in MAINTENANCE_JOB_IDS:
        cron = effective_maintenance_cron(job_id, maintenance)
        try:
            apply_maintenance_schedule(
                scheduler,
                job_id,
                cron,
                cache_engine=cache_engine,
                data_dir=data_dir,
                session_factory=session_factory,
                secret_box=secret_box,
                timezone=timezone,
            )
        except (ValueError, KeyError):
            log.warning("scheduler.bad_maintenance_cron", job=job_id, cron=cron)
