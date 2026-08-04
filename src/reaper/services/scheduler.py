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
* **Refresh every list on Settings, Lists** daily, independent of scans -- the same pass a
  scan runs, so a tag or a collection edited between scans starts protecting within a day.
* **Check whether a newer Reaper exists**, daily. Not upkeep for the scan like the two
  above: it is here because a check that only ran when someone opened the UI never ran at
  all on the servers most likely to need it (#464).

**This scheduler never deletes media.** Deletion runs happen through the executor, under
the destructive-action guard, and are not scheduled here -- automated deletion is an M8
concern gated behind an earned autonomy grant, and wiring it to a timer before that
machinery exists would be exactly the wrong shortcut. It does delete Reaper's *own*
bookkeeping, which is a different thing entirely and needs no arming: expired auth
sessions, and scans older than the retention window (``services.retention``). Neither
touches a file, an *arr, or Plex.

APScheduler 3.x (4.x is still alpha). ``AsyncIOScheduler`` shares the app's event loop,
and ``coalesce=True`` + ``max_instances=1`` mean a job that overruns its interval -- a
first ratings load can take a while -- never stacks a second copy on top of itself.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import timedelta, tzinfo
from pathlib import Path

import structlog
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_SUBMITTED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from reaper.auth import sessions
from reaper.clients.plex import PlexError
from reaper.clients.tautulli import TautulliClient
from reaper.clock import utcnow
from reaper.config import RuntimeSafety, Settings
from reaper.crypto import SecretBox
from reaper.db.models import Instance, InstanceKind
from reaper.services import (
    app_settings,
    history_sync,
    imdb_dataset,
    list_config,
    retention,
    scan_runner,
)
from reaper.services import snapshot as snapshot_service
from reaper.services.imdb_dataset import ImdbRatings
from reaper.services.update_check import UpdateChecker

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
    "check_for_updates": "15 4 * * *",
}

#: The upkeep jobs, in display order. The scan is scheduled separately (its own key).
MAINTENANCE_JOB_IDS: tuple[str, ...] = tuple(DEFAULT_MAINTENANCE_CRONS)

#: Every job whose schedule the owner may edit, scan first. Drives the Jobs settings list.
SCHEDULABLE_JOB_IDS: tuple[str, ...] = (SCAN_JOB_ID, *MAINTENANCE_JOB_IDS)

#: Housekeeping that deliberately does NOT appear in that list. Deleting sessions whose
#: 30-day window has already closed is not a choice an operator should have to make, and
#: an off switch on it would only ever let the table grow (PR-13). It is on an interval
#: rather than a cron for the same reason it has no switch: there is no hour of the day
#: this belongs at, so it never has to be re-based when the server time zone changes.
SESSION_SWEEP_JOB_ID = "sweep_expired_sessions"
SESSION_SWEEP_INTERVAL_S = 12 * 60 * 60

#: Trimming the scan history is housekeeping for the same reason, and stays off the
#: schedulable list on the same argument: an off switch here could only ever let the
#: database grow, which is the state that made it grow without limit in the first place
#: (#315). Nothing reads a scan older than the newest one, so there is no window an
#: operator would want to widen and nothing they lose by this running. Twelve-hourly on
#: an interval rather than a cron, again like the session sweep -- there is no hour of the
#: day this belongs at, so a time-zone change never has to re-base it.
SNAPSHOT_SWEEP_JOB_ID = "sweep_old_snapshots"
SNAPSHOT_SWEEP_INTERVAL_S = 12 * 60 * 60

#: The floor on how long after boot the first sweep runs; ``SNAPSHOT_SWEEP_JITTER_S`` adds
#: up to half an hour on top. An ``IntervalTrigger`` given no start date
#: first fires a whole interval in, which for the session sweep is fine -- an expired
#: session authorizes nothing whether or not its row is still there, so the table sitting
#: there another twelve hours costs nothing. This one is different in exactly the case it
#: was written for: an install upgrading with months of scans behind it has a database
#: that is mostly dead weight *now*, and making the operator wait half a day to see any of
#: it come back is the wrong answer. Minutes, not seconds, so the first sweep and its
#: possible vacuum stay out of the way of the startup ratings catch-up.
SNAPSHOT_SWEEP_STARTUP_DELAY_S = 5 * 60

#: Spread on every firing, so the sweep never settles at a fixed time of day. Without it an
#: ``IntervalTrigger`` fires at the same second forever: twelve hours is an exact multiple of
#: an hour, and a firing that runs late does not shift the phase, because the next one is
#: computed from the scheduled time and not the actual one. A scan on a cron whose period
#: divides twelve hours therefore either misses every firing or collides with all of them.
#: That was harmless until the compaction was gated on a live scan or reap (#325): now a
#: collision skips ``retention.compact_if_fragmented`` at *every* firing, so an upgraded
#: install's freed pages stay on disk for the life of the process and the gate's own promise
#: that the next firing reattempts is false. APScheduler adds ``uniform(0, jitter)`` to the
#: previous *jittered* time, so the phase walks forward instead of settling anywhere new.
#: The session sweep shares the interval and needs none of this: it has no skip branch, so
#: it runs whatever it collides with, and a fixed phase costs it nothing (rule 72).
SNAPSHOT_SWEEP_JITTER_S = 30 * 60


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
        loaded = await imdb_dataset.refresh(cache_engine, data_dir)
        log.info("scheduler.ratings_refreshed", rows=loaded.rows, skipped=loaded.skipped)
        # A load that dropped an unusual share of rows still "succeeded", and the rows it
        # kept are good -- but the ratings it lost are protections that stop keeping
        # titles, so the operator is told rather than left with a green tick.
        result = (
            "Ratings refreshed, but a lot of the file could not be read. Fewer titles "
            "now have a rating to protect them."
            if loaded.drifted
            else "Ratings refreshed"
        )
        await _record_run(session_factory, "refresh_ratings", ok=True, result=result)
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
    settings: Settings | None = None,
    secret_box: SecretBox | None = None,
) -> None:
    """Refresh EVERY list on Settings, Lists, the way a scan does.

    It used to refresh only the IMDb ones, because those need no client -- and that left the
    other three sources with no automatic refresh between scans at all, while the screen
    presents all four as one uniform thing. So this now runs the same pass a scan runs
    (``snapshot.sync_protection_lists``), which is also what the screen's own "Check now"
    calls: a second way to refresh a protection list would be a second set of fail-closed
    rules to keep in step (rule 3/22's shape for the safety path).

    The job id is still ``refresh_curated_lists``. It is a stored schedule key, so renaming it
    would orphan the cron an operator saved; only what it does and what it is called on screen
    have changed.

    **This pass retires**, unlike the old one, and that is a durable protection-DISABLING
    write. It is safe here for the reasons ``sync_protection_lists`` states and enforces, not
    on trust: a family is retired only when the configuration it is judged against was
    actually readable, and only when its own sync did not raise -- so an *arr that is down at
    3:45 AM keeps every list it feeds (rule 115).

    Plex is optional and fails CLOSED exactly as it does in a scan and on the Lists screen:
    with no live server no collection provider is built, so nothing is synced for one and
    nothing is retired either, and the stored membership stays as the last good check left it.

    ``session_factory``, ``settings`` and ``secret_box`` are optional only because the
    last-run bookkeeping tolerates their absence; with no way to read the definitions or reach
    the sources there is nothing to refresh.
    """
    if session_factory is None or settings is None or secret_box is None:
        log.warning("scheduler.lists_refresh_skipped", reason="not wired")
        return

    async with AsyncExitStack() as stack:
        try:
            # Not a scan: this reads the *arr and Plex and nothing else, so it does not carry
            # a scan's Tautulli precondition -- an install with Plex linked and no Tautulli
            # still gets its collections refreshed.
            radarrs, sonarrs, _tautulli, _seerrs, plex = await scan_runner.build_sources(
                session_factory,
                settings,
                secret_box,
                stack=stack,
                require_scan_sources=False,
            )
        except scan_runner.ScanConfigError as exc:
            log.warning("scheduler.lists_refresh_failed", error=str(exc))
            await _record_run(
                session_factory, "refresh_curated_lists", ok=False, result="Couldn't refresh lists"
            )
            return

        plex_server: object | None = None
        if plex is not None:
            try:
                plex_server = await plex.connect()
            except PlexError as exc:
                log.warning("scheduler.lists_refresh_plex_unreachable", error=str(exc))

        try:
            async with session_factory() as session:
                # ``strict``: this pass retires, so a row that will not decode has to stop it
                # rather than read as one the operator deleted (rules 65/91, 115).
                definitions = await list_config.definitions(session, strict=True)
        except Exception as exc:
            log.warning("scheduler.lists_refresh_failed", error=str(exc))
            await _record_run(
                session_factory, "refresh_curated_lists", ok=False, result="Couldn't refresh lists"
            )
            return

        synced = await snapshot_service.sync_protection_lists(
            cache_engine,
            definitions=definitions,
            radarrs=radarrs,
            sonarrs=sonarrs,
            plex_server=plex_server,
        )

    failed = sum(1 for v in synced.values() if isinstance(v, str) and v.startswith("error:"))
    checked = sum(1 for v in synced.values() if isinstance(v, int))
    log.info("scheduler.lists_refreshed", checked=checked, failed=failed)
    if not failed:
        result = "Lists refreshed"
    elif not checked:
        result = "Couldn't refresh lists"
    else:
        # Only the partial outcome needs the count: a total failure and a clean pass each say
        # so in one phrase, and restating the per-list reasons here would be the same refusal
        # written twice -- every one of them is already on the row it belongs to (rule 144).
        result = f"Refreshed {checked} lists, {failed} couldn't be checked"
    await _record_run(session_factory, "refresh_curated_lists", ok=not failed, result=result)


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


async def check_for_updates(
    update_checker: UpdateChecker,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Ask GitHub whether a newer Reaper exists, on a schedule rather than on a page load.

    This is the job that makes the About panel's promise true. The check used to run only
    when the UI asked for it, so an install nobody signs in to for weeks never checked at
    all and sat on an old build with nothing to say so (#464).

    Read-only, and the lightest job here: one anonymous GET, no credentials, nothing about
    the library in it. It takes :meth:`UpdateChecker.refresh` rather than ``status`` so a
    firing (or a Run now) genuinely asks instead of repeating a cached answer, and it fills
    the same cache the About route reads, so the answer is already there when someone does
    open Reaper.

    ``REAPER_UPDATE_CHECK=false`` governs this path as it governs the route (rule 55): the
    checker returns the disabled answer without sending anything, and the run is recorded
    saying so rather than reading as a check that happened.

    The result strings are the Jobs page's own copy for these states; the About row
    (`frontend/src/components/Settings.tsx`, ``UpdateCell``) says the same six things in
    its own words, and the two are edited together (rule 144).
    """
    try:
        status = await update_checker.refresh()
        if not status.enabled:
            await _record_run(
                session_factory, "check_for_updates", ok=True, result="Update checks are off"
            )
            return
        if status.update_available is None:
            # Not a crash: an unreachable GitHub, a rate limit, or a version pair that
            # cannot be ordered all land here, and the checker has already logged why.
            # Recorded as a failed run so the Jobs page shows it rather than a green tick
            # over a check that answered nothing.
            await _record_run(
                session_factory,
                "check_for_updates",
                ok=False,
                result="Couldn't check for updates",
            )
            return
        dev = status.channel == "dev"
        if status.update_available:
            # INFO, not DEBUG: on a headless install the log is the only place this can
            # land between one sign-in and the next.
            log.info(
                "scheduler.update_available",
                channel=status.channel,
                latest=status.latest,
                behind=len(status.changes),
            )
            result = (
                "The dev branch has moved since this build"
                if dev
                else f"Reaper {status.latest} is out"
            )
        else:
            result = "This build matches the dev branch" if dev else "You are on the newest release"
        await _record_run(session_factory, "check_for_updates", ok=True, result=result)
    except Exception as exc:
        # The checker maps its own network failures to "unknown" above, so reaching here
        # means something unexpected -- and an unexpected failure in the least important
        # job on the scheduler must not stop the ones that keep scans working.
        log.warning("scheduler.update_check_failed", error=str(exc))
        await _record_run(
            session_factory, "check_for_updates", ok=False, result="Couldn't check for updates"
        )


async def sweep_expired_sessions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Delete auth sessions whose window has closed. Not operator-schedulable (see the id).

    Swallows its own failures like every other job here: a locked database must not stop
    the scheduler, and nothing depends on this having run -- an expired session is refused
    on sight whether or not its row is still there.
    """
    try:
        async with session_factory() as session:
            removed = await sessions.sweep_expired(session)
            await session.commit()
        if removed:
            log.info("scheduler.sessions_swept", removed=removed)
    except Exception as exc:
        log.warning("scheduler.session_sweep_failed", error=str(exc))


async def sweep_old_snapshots(
    session_factory: async_sessionmaker[AsyncSession],
    data_dir: Path,
    reap_running: Callable[[], bool],
) -> None:
    """Trim the scan history to the retention window. Not operator-schedulable (see the id).

    Swallows its own failures like every other job here: nothing downstream depends on
    this having run, and a database busy with a scan must not stop the scheduler. The
    worst case of a skipped firing is that the next one has one more scan to drop.

    Compaction is attempted on every firing, not only one that swept something, and it
    declines unless the freed share is large enough to be worth an exclusive lock
    (``retention.compact_if_fragmented``). In the steady state it declines, at the cost of
    three pragmas; on the first firing after an install upgrades with months of scans
    behind it, that is the run that hands the disk space back.

    **Gating it on this firing's sweep would make that run the only one.** The upgrade
    drains the whole backlog at once, so every later firing removes nothing -- and a
    compaction lost to a full disk, a locked database or a canceled shutdown would never
    be reattempted until a new scan pushed the count past the window again, which on an
    install whose auto-scan is off is never.

    **It is gated on a live scan or reap, though, and that is a different argument.** The
    ``VACUUM`` rewrites the whole file under the write lock, and every app connection waits
    only 5s for it (``db.session``). Measured on local NVMe with the app's own pragmas, a
    2.4 GB database vacuums for 8s and the concurrent write fails -- and 2.4 GB is inside the
    range ``retention.KEEP_SNAPSHOTS`` documents as the steady state, so this needs no unusual
    storage to bite (#325). A scan loses every source read it made, since it commits once at
    the end; a reap loses a journal step and wedges (#327). Both outrank housekeeping, so the
    housekeeping yields. This skips the compaction, never the sweep, whose batches are short
    writes that take the lock no longer than the app's own.

    **What makes the next firing a real reattempt is the jitter, not the interval.** A bare
    twelve-hour interval fires at the same second of the same two hours forever, so a scan on
    a cron whose period divides twelve hours would collide with every firing or none -- and a
    collision would mean the compaction never ran again, not that it ran twelve hours later.
    ``SNAPSHOT_SWEEP_JITTER_S`` is what keeps that from being a permanent skip; the reasoning
    is on the constant.
    """
    try:
        removed = await retention.sweep_old_snapshots(session_factory)
        if removed:
            log.info("scheduler.snapshots_swept", removed=removed)
        # Checked here rather than at the top of the job: the sweep runs first and, on the
        # upgrade drain this feature exists for, it runs for a while. A check made before it
        # would be answering about a moment that has passed.
        busy = "a scan is running" if scan_runner.scan_running() else None
        if busy is None and reap_running():
            busy = "a reap is running"
        if busy is not None:
            log.info("scheduler.compaction_skipped", reason=busy)
        elif await retention.compact_if_fragmented(data_dir):
            log.info("scheduler.database_compacted")
    except Exception as exc:
        # The sweep commits per batch, so a failure part-way through keeps whatever it
        # already dropped and the next firing continues from there.
        log.warning("scheduler.snapshot_sweep_failed", error=str(exc))


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
    settings: Settings,
    update_checker: UpdateChecker,
) -> dict[str, tuple[object, list[object]]]:
    """The (callable, args) each upkeep job is added with. One place, so wiring a job at
    build time and re-wiring it on a schedule change can never drift apart.

    ``update_checker`` is the app's own instance, not a fresh one: the job and the About
    route share one cache, so a scheduled check leaves the answer ready for the next page
    load instead of the two asking GitHub separately.
    """
    return {
        "refresh_ratings": (refresh_ratings, [cache_engine, data_dir, session_factory]),
        "refresh_curated_lists": (
            refresh_curated_lists,
            [cache_engine, session_factory, settings, secret_box],
        ),
        "full_history_sweep": (full_history_sweep, [session_factory, cache_engine, secret_box]),
        "check_for_updates": (check_for_updates, [update_checker, session_factory]),
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
    settings: Settings,
    update_checker: UpdateChecker,
    timezone: tzinfo,
) -> None:
    """Reconcile one upkeep job to a cron string, or remove it when ``cron`` is ``None``.

    ``cron`` is read in ``timezone`` (the server zone), so every timed job shares one clock
    with the scan. ``None`` means the owner turned the job off; the job is dropped from the
    scheduler but can still be run once by hand (see :func:`run_maintenance_now`). A malformed
    cron raises ``ValueError`` (surfaced as a 422) rather than being silently dropped.
    """
    specs = _maintenance_specs(
        cache_engine,
        data_dir,
        session_factory=session_factory,
        secret_box=secret_box,
        settings=settings,
        update_checker=update_checker,
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
    settings: Settings,
    update_checker: UpdateChecker,
) -> None:
    """Fire an upkeep job immediately, whether or not it is on a schedule.

    A scheduled job is nudged in place (its cron is untouched, so the next regular run still
    happens). A job the owner turned off is run as a one-shot that removes itself afterward,
    so "run now" never quietly turns the schedule back on.
    """
    specs = _maintenance_specs(
        cache_engine,
        data_dir,
        session_factory=session_factory,
        secret_box=secret_box,
        settings=settings,
        update_checker=update_checker,
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
    settings: Settings,
    update_checker: UpdateChecker,
    timezone: tzinfo,
    reap_running: Callable[[], bool],
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
        cache_engine,
        data_dir,
        session_factory=session_factory,
        secret_box=secret_box,
        settings=settings,
        update_checker=update_checker,
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
    scheduler.add_job(
        sweep_expired_sessions,
        IntervalTrigger(seconds=SESSION_SWEEP_INTERVAL_S),
        args=[session_factory],
        id=SESSION_SWEEP_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        sweep_old_snapshots,
        IntervalTrigger(
            seconds=SNAPSHOT_SWEEP_INTERVAL_S,
            start_date=utcnow() + timedelta(seconds=SNAPSHOT_SWEEP_STARTUP_DELAY_S),
            jitter=SNAPSHOT_SWEEP_JITTER_S,
        ),
        args=[session_factory, data_dir, reap_running],
        id=SNAPSHOT_SWEEP_JOB_ID,
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
    update_checker: UpdateChecker,
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
                settings=settings,
                update_checker=update_checker,
                timezone=timezone,
            )
        except (ValueError, KeyError):
            log.warning("scheduler.bad_maintenance_cron", job=job_id, cron=cron)
