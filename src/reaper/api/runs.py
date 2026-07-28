# SPDX-License-Identifier: AGPL-3.0-or-later
"""Building, reviewing, dry-running, and executing a reap plan.

This is where the reap loop meets the owner. A plan is built from the latest snapshot's
condemned set, journalled, and shown -- every step, with the exact request it would
issue -- can be dry-run to prove the whole chain end to end without deleting, and finally
executed for real.

The real execution path (``POST /runs/{id}/execute``) is the one endpoint that deletes,
and it is gated hard: deletion must be enabled on the host, and the caller must echo back
the plan's exact content-bound confirmation phrase ("REAP 7 SOULS 214 GB"). Underneath, the
executor's own interlocks -- manifest re-check, caps, canary, the streaming veto and the
played-since-approval check -- each run and each resolves toward keeping the file. The
dry run remains fully offline: it walks every interlock and sends nothing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from contextlib import AsyncExitStack

import structlog
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.api import tags as api_tags
from reaper.api.scan import launch_scan
from reaper.api.schemas import (
    ActionStepOut,
    CreateRunIn,
    ExecuteRunIn,
    ProfileSettingsIO,
    RunCheckOut,
    RunOut,
    RunOutcomeOut,
    RunReportOut,
    RunSummaryOut,
)
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.models import ActionStep, Candidate, ReapRun, RunState, Snapshot
from reaper.engine.policy import ProfileSettings
from reaper.services import app_settings, whitelist
from reaper.services.condemned import effective_condemned
from reaper.services.executor import (
    ExecutionError,
    Executor,
    ReapGateway,
    ReapProgress,
    RunReport,
    size_confirmed,
)
from reaper.services.planner import PlanError, build_plan, confirmation_phrase, plan_bytes
from reaper.services.profiles import (
    active_profile,
    active_profile_settings,
    save_profile_settings,
)
from reaper.services.scan_runner import build_reap_gateway

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=[api_tags.REAP])


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


async def _latest_snapshot(session: AsyncSession) -> Snapshot | None:
    return (
        await session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1))
    ).scalar_one_or_none()


async def _run_steps(session: AsyncSession, run: ReapRun) -> list[ActionStep]:
    return list(
        (
            await session.execute(
                select(ActionStep)
                .where(ActionStep.run_id == run.id)
                .order_by(ActionStep.ordinal, ActionStep.id)
            )
        )
        .scalars()
        .all()
    )


async def _saved_limits_or_refuse(session: AsyncSession) -> ProfileSettings:
    """The caps and grace the operator saved, or a refusal saying they could not be read.

    ``active_profile`` deliberately never raises: it is read by the scan, by execute, and by
    the very settings page an operator would use to repair a broken blob, so a hard error
    would take out the fix along with everything else. What it does instead is fall back to
    the SHIPPED DEFAULTS and flag it, and those defaults can be looser than what was saved --
    a run cap of 5 becomes 10, a grace of 30 becomes 14.

    Every route that acts on those numbers used to call ``active_profile_settings``, which
    drops the flag on the floor. So a profile that stopped validating left the one route that
    deletes bounded by numbers nobody chose, with nothing anywhere saying so. The scan path
    already degrades on this (``scan_runner``); this is the same refusal for the routes that
    plan, preview and send (rules 65/91).

    Deliberately NOT applied to the other readers, which display rather than delete (rule
    72, deferred in writing): ``list_runs`` renders numbers it never acts on, and taking out
    the page an operator reads to see what is pending would be the wrong blast radius for a
    fault the deleting route already refuses; the policy editor's two reads only size a
    warning. ``api/whitelist``'s grace-clock write is the one worth revisiting -- a default
    grace is SHORTER than a saved one, so it starts the clock early -- but refusing there
    would refuse a *spare*, which is the operator's keep action, and no reap can run
    meanwhile because the scan degrades.
    """
    profile = await active_profile(session)
    if profile.repaired:
        raise HTTPException(
            409,
            "Reaper couldn't read the limits you saved, so it won't reap. Open Policy, go "
            "to Pace and limits, and save your limits again.",
        )
    return profile.settings


class _RunReads:
    """Per-request memo for the three reads every run's output needs.

    ``list_runs`` renders up to 200 runs, and each one independently re-read the hand
    overrides, the reap profile, and the WHOLE effective condemned set of its snapshot --
    that last one being every condemned row in the snapshot, pulled into memory once per
    run. Runs on a page overwhelmingly share a snapshot, so each read happens once here
    and every run that needs it gets the same answer (P-2).

    Scoped to ONE request and never held across one. The overrides and the allowance are
    deliberately read live, so the count and total in front of the owner describe the
    settings in force now; a cache that outlived the request would put back exactly the
    staleness those live reads exist to prevent.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._decisions: dict[str, str] | None = None
        self._allow_unmeasured: bool | None = None
        self._condemned: dict[int, dict[str, Candidate]] = {}

    async def decisions(self) -> dict[str, str]:
        if self._decisions is None:
            self._decisions = await whitelist.overrides(self._session)
        return self._decisions

    async def allow_unmeasured(self) -> bool:
        if self._allow_unmeasured is None:
            settings = await active_profile_settings(self._session)
            self._allow_unmeasured = settings.max_unmeasured_per_run > 0
        return self._allow_unmeasured

    async def condemned(self, snapshot_id: int) -> dict[str, Candidate]:
        cached = self._condemned.get(snapshot_id)
        if cached is None:
            cached = await effective_condemned(self._session, snapshot_id, await self.decisions())
            self._condemned[snapshot_id] = cached
        return cached


async def _planned_candidates(
    session: AsyncSession,
    run: ReapRun,
    *,
    steps: Sequence[ActionStep] | None = None,
    reads: _RunReads | None = None,
) -> list[Candidate]:
    """Exactly the candidates this run would actually delete, deduped to one per item.

    Deduped by media_key because a season is THREE steps sharing one key, and counting per
    step would triple its size and item count in the very phrase the owner types to approve.
    The item is the unit the executor's caps count, so the review surface and the enforcement
    surface agree. ``dict.fromkeys`` dedupes in plan order.

    The membership is the EFFECTIVE condemned set (services.condemned), re-read now, exactly
    as the executor derives it: an item spared after the plan was built has steps but will
    not be deleted, so it must not inflate the confirmation phrase, and a hand reap whose
    override was since removed drops out the same way. Recomputed at execute time, this also
    makes a post-plan override change the expected phrase -- so the owner is asked to reload
    and re-confirm the changed reap rather than approving a count that no longer matches.

    Items with no measured size drop out here too, exactly as they do in the executor's
    ``_deletable``, and for the same reason: the send paths refuse them, so counting them
    would put a count in front of the owner describing a different set than the one the
    run will act on. Unless the operator's allowance is open, in which case they ARE acted
    on and belong in the count -- read live below, so both surfaces agree.
    """
    memo = reads if reads is not None else _RunReads(session)
    if steps is None:
        steps = await _run_steps(session, run)
    by_key = await memo.condemned(run.snapshot_id)
    # The allowance is read here, at the moment the numbers are produced, so the count
    # and total in front of the owner describe the set the executor will act on under the
    # settings in force NOW -- not the ones in force when the plan was built.
    allow_unmeasured = await memo.allow_unmeasured()
    return [
        by_key[k]
        for k in dict.fromkeys(s.media_key for s in steps)
        if k in by_key and (allow_unmeasured or size_confirmed(by_key[k]))
    ]


async def _run_out(
    session: AsyncSession, run: ReapRun, *, reads: _RunReads | None = None
) -> RunOut:
    # Steps are fetched ONCE and handed down: this and _planned_candidates each used to
    # query them, so every run cost two step reads instead of one (P-2).
    steps = await _run_steps(session, run)
    planned = await _planned_candidates(session, run, steps=steps, reads=reads)

    return RunOut(
        id=run.id,
        snapshot_id=run.snapshot_id,
        policy_hash=run.policy_hash,
        state=run.state.value,
        item_count=len(planned),
        # The same set the phrase is derived from. The total covers the items that have
        # a size and never absorbs the ones that do not; when the allowance has admitted
        # any, the phrase says so with its own suffix rather than letting this number
        # quietly run low.
        total_bytes=plan_bytes(planned)[0],
        confirmation_phrase=confirmation_phrase(planned) if planned else "REAP 0 SOULS 0 GB",
        held_back_unknown_size=run.held_back_unknown_size,
        approved_manifest_hash=run.approved_manifest_hash,
        approved_by=run.approved_by,
        approved_at=run.approved_at.isoformat(),
        steps=[
            ActionStepOut(
                media_key=s.media_key,
                ordinal=s.ordinal,
                kind=s.kind,
                method=s.method,
                path=s.path,
                body=json.loads(s.body_json) if s.body_json else None,
                state=s.state.value,
                is_canary=s.ordinal == 0,
            )
            for s in steps
        ],
    )


@router.post("/runs")
async def create_run(request: Request, payload: CreateRunIn | None = None) -> RunOut:
    """Build a plan from the latest snapshot's condemned set. Journals it; sends nothing.

    With no body, the plan covers the whole condemned set. With ``media_keys``, it covers
    just those items -- "reap selected", and the safe path for a first single deletion. The
    plan is bound to that snapshot and policy and records a content hash of exactly what it
    would delete, so it cannot later be executed against a different library than reviewed.
    """
    # An OMITTED field (no body, or ``media_keys=null``) means "the whole condemned set".
    # An explicit empty list means "nothing selected" and must NOT collapse to the whole
    # set: ``[]`` is falsy, so a naive truthiness test would invert a select-nothing request
    # into a select-everything plan. Pass the empty set through instead, so build_plan fails
    # closed on it -- deletion planning must never turn "nothing" into "everything".
    only = (
        set(payload.media_keys) if payload is not None and payload.media_keys is not None else None
    )
    async with _sessions(request)() as session:
        snapshot = await _latest_snapshot(session)
        if snapshot is None:
            raise HTTPException(404, "No scan has run yet, so there is nothing to plan.")

        try:
            # approved_by is the authenticated admin once auth is wired into these routes;
            # until then the plan records that it was built from the API, unattended.
            run = await build_plan(
                session,
                snapshot_id=snapshot.id,
                approved_by="api",
                only_media_keys=only,
                max_unmeasured=(await _saved_limits_or_refuse(session)).max_unmeasured_per_run,
            )
        except PlanError as exc:
            raise HTTPException(422, str(exc)) from exc

        out = await _run_out(session, run)
        await session.commit()
        return out


@router.get("/runs")
async def list_runs(
    request: Request,
    # Bounded at the boundary, both ends. Without a lower bound a negative limit passed
    # straight through to ``LIMIT -1``, which SQLite reads as NO limit: one request
    # returned every run ever made (B-8). Cheap rows make that less costly than it was,
    # not bounded -- so the bound stays here, where a bad value is refused rather than
    # clamped silently.
    limit: int = Query(50, ge=1, le=200),
) -> list[RunSummaryOut]:
    """The recent plans, as stored rows and nothing more (see ``RunSummaryOut``).

    Deliberately NOT ``RunOut``: that shape's counts, totals and phrase are each derived
    from the effective condemned set of the run's snapshot, so building it per run read
    the whitelist, the profile and the whole candidate table once per row -- fifty times
    on every visit to the Reap page (P-3). Opening a run goes to ``GET /runs/{id}``,
    which derives them for the one run being looked at.
    """
    async with _sessions(request)() as session:
        runs = list(
            (await session.execute(select(ReapRun).order_by(ReapRun.id.desc()).limit(limit)))
            .scalars()
            .all()
        )
        # No memo needed: nothing here is derived, so there is no expensive read to share.
        return [
            RunSummaryOut(
                id=r.id,
                snapshot_id=r.snapshot_id,
                state=r.state.value,
                approved_by=r.approved_by,
                approved_at=r.approved_at.isoformat(),
                aborted_reason=r.aborted_reason,
                held_back_unknown_size=r.held_back_unknown_size,
            )
            for r in runs
        ]


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: int) -> RunOut:
    async with _sessions(request)() as session:
        run = await session.get(ReapRun, run_id)
        if run is None:
            raise HTTPException(404, "No such run.")
        return await _run_out(session, run)


@router.post("/runs/{run_id}/dry-run")
async def dry_run(request: Request, run_id: int) -> RunReportOut:
    """Walk the plan end to end with every interlock, and send nothing.

    This is the proof: the manifest re-check, the caps and the canary ordering all run
    for real, and every mutating call is recorded rather than issued. What a dry run
    deliberately does NOT prove are the live per-item vetoes (someone streaming the item
    right now, a play landing after approval, a missing rating key, a file that grew
    since approval, deletion switched off mid-run): those are moment-of-deletion checks
    that only run on a real send, where the moment is real. The transport guard sits
    underneath as the independent backstop.
    """
    settings: Settings = request.app.state.settings

    async with _sessions(request)() as session:
        # Read-only safety by construction here: the executor's dry_run does not send, and
        # even if it tried, this ceiling forbids it. Read both switches so a UI emergency
        # stop is reflected, not just the env flag.
        safety = await app_settings.runtime_safety(session, settings)
        run = await session.get(ReapRun, run_id)
        if run is None:
            raise HTTPException(404, "No such run.")

        # The owner's configured caps, not a hardcoded default. This is what lets a real
        # (large) condemned set be simulated: the cap is a decision the owner makes.
        profile_settings = await _saved_limits_or_refuse(session)
        executor = Executor(session, safety=safety, settings=profile_settings, dry_run=True)
        try:
            report = await executor.execute(run_id)
        except ExecutionError as exc:
            # A voided run (changed manifest, already executed) is a 409: the plan is no
            # longer valid, and the owner needs to re-plan rather than retry.
            raise HTTPException(409, str(exc)) from exc
        await session.commit()

    return _report_out(report)


class ReapStatus(BaseModel):
    """Where a running reap has got to. Polled by the browser; mutated in place by the
    running job -- the same shape and pattern as ``ScanStatus``.

    A reap runs **detached from the request that starts it**, so it survives navigating away
    and closing the tab. This is how the browser follows a run and re-attaches to one already
    in flight, and where the Stop it can reach from any screen is read and set. Cumulative
    counts, never deltas, so a dropped poll loses no ground.
    """

    running: bool = False
    run_id: int | None = None
    stopping: bool = False
    """The operator pressed Stop. Set by ``POST /runs/{id}/stop``, read by the executor
    before each item -- a graceful halt (an ExecutionError at the next item), never a hard
    task cancel, so the abort path still tidies Plex for what was already removed."""
    phase: str = "idle"  # idle | reaping | complete | aborted | error
    done: int = 0
    total: int = 0
    deleted_items: int = 0
    deleted_bytes: int = 0
    skipped: int = 0
    title: str = ""
    error: str | None = None
    report: RunReportOut | None = None
    """The after-action report, set once the run ends. Null while running; the browser reads
    it when ``running`` turns false to render the per-item checklist without a second call."""


def _reap_status(app: FastAPI) -> ReapStatus:
    status: ReapStatus | None = getattr(app.state, "reap_status", None)
    if status is None:
        status = ReapStatus()
        app.state.reap_status = status
    return status


def _preflight_refusal(gateway: ReapGateway) -> str | None:
    """The executor's client-presence refusals, checked synchronously so the endpoint returns
    them immediately (a clear 409) instead of only through the status poll after the task has
    started. The executor re-checks the very same things as its own backstop -- this is the
    earlier, clearer refusal, the way the arm gate mirrors the transport guard -- so the two
    messages must stay in step."""
    if gateway.plex is None:
        return (
            "Refusing a real run without Plex: the active-stream veto (re-polled before "
            "every delete) cannot run, and deleting blind to who is watching is exactly "
            "what must never happen."
        )
    if gateway.tautulli is None:
        return (
            "Refusing a real run without Tautulli: the played-since-approval check cannot "
            "run, and the grace period exists precisely so a late view can still spare an item."
        )
    return None


@router.post("/runs/{run_id}/execute")
async def execute_run(request: Request, run_id: int, payload: ExecuteRunIn) -> ReapStatus:
    """Start a real reap. **The one endpoint in Reaper that deletes.**

    Every gate must pass, and each resolves toward keeping the file:

    1. **Deletion must be enabled on the host** (403 otherwise). The transport guard would
       refuse the calls anyway; this is the earlier, clearer refusal.
    2. **The typed confirmation must match the plan's current phrase exactly** (409
       otherwise). The phrase is recomputed here from the plan, so a stale tab -- whose
       phrase was for a different plan -- cannot replay it.
    3. **The executor's own interlocks** -- manifest re-check, caps abort-not-truncate, the
       canary, the per-item streaming veto and played-since-approval check -- each run and
       can still spare or abort.

    The gates above run synchronously, so a bad request is refused immediately. The reap
    itself then runs **detached** (like a scan) on ``app.state.reap_task``, reporting to
    ``app.state.reap_status`` which the browser polls: a long run survives navigating away
    and closing the tab, and Stop is reachable from any screen. This returns the initial
    status, not the finished report -- the report lands on the status when the run ends.

    The scheduler never calls this. A real reap is a deliberate act by a person who typed
    the phrase, not something a timer can trigger.
    """
    settings: Settings = request.app.state.settings
    box: SecretBox = request.app.state.secret_box
    factory = _sessions(request)
    app = request.app

    # Claim the single reap slot SYNCHRONOUSLY, with no await between the check and the set,
    # so two execute requests racing each other cannot both pass -- a check-then-await-then-set
    # would let both see ``running`` false and start two reaps over the one shared status. Every
    # display field is reset in the same synchronous stretch, so a poll can never catch a
    # half-initialized status (a stale report beside a fresh ``running``). Released on any
    # synchronous refusal below, before the task is ever created.
    status = _reap_status(app)
    if status.running:
        raise HTTPException(
            409, "A reap is already running. Wait for it to finish, or stop it, first."
        )
    status.running = True
    status.run_id = run_id
    status.stopping = False
    status.phase = "reaping"
    status.done = 0
    status.total = 0
    status.deleted_items = 0
    status.deleted_bytes = 0
    status.skipped = 0
    status.title = ""
    status.error = None
    status.report = None

    try:
        async with factory() as session:
            safety = await app_settings.runtime_safety(session, settings)
            if not safety.destructive_allowed:
                raise HTTPException(403, safety.why_blocked() or "Deletion is turned off.")

            run = await session.get(ReapRun, run_id)
            if run is None:
                raise HTTPException(404, "No such run.")

            planned = await _planned_candidates(session, run)
            expected = confirmation_phrase(planned) if planned else "REAP 0 SOULS 0 GB"
            if payload.confirmation_phrase.strip() != expected:
                raise HTTPException(
                    409,
                    # Plain interpolation, not repr: the operator sees the phrase exactly as
                    # it must be typed, with no engineer-style quoting around it.
                    f"That confirmation does not match this plan. Expected: {expected}. The "
                    "plan may have changed since the page loaded. Reload, review, and confirm "
                    "again.",
                )
            profile_settings = await _saved_limits_or_refuse(session)
            status.total = len(planned)

        # Build the live clients now, in the request, so a misconfigured run is refused
        # immediately -- and hand the SAME clients to the background task, which enters and
        # closes them. On a refusal we enter-and-exit the built-but-unused clients to close
        # them cleanly rather than leak them.
        gateway, closers = await build_reap_gateway(factory, box, safety=safety)
        if (preflight := _preflight_refusal(gateway)) is not None:
            async with AsyncExitStack() as closing:
                for client in closers:
                    await closing.enter_async_context(client)
            raise HTTPException(409, preflight)
    except Exception:
        # ANY synchronous failure before the task is created releases the slot -- not only an
        # HTTPException refusal, but a crypto/DB error out of build_reap_gateway or a session
        # read. Catching only HTTPException here would leave ``running`` stuck True with no
        # task to ever clear it, wedging the one deletion endpoint at a permanent 409 until
        # restart. HTTPException is an Exception, so the 4xx refusals still propagate unchanged.
        status.running = False
        status.phase = "idle"
        status.run_id = None
        raise

    async def _armed_now() -> bool:
        # The executor's mid-run kill switch: a FRESH session per read, because the run's
        # own session caches rows across its per-item commits and would keep reporting
        # the switch as it stood when the run began.
        async with factory() as check_session:
            return await app_settings.destructive_enabled(check_session, settings)

    async def _stop_now() -> bool:
        # The explicit Stop, read off the polled status the browser sets from any screen.
        # Graceful by construction: the executor raises ExecutionError at the next item, so
        # its abort path runs and still tidies Plex. Never a task.cancel().
        return status.stopping

    def on_progress(p: ReapProgress) -> None:
        status.done = p.done
        status.total = p.total
        status.deleted_items = p.deleted_items
        status.deleted_bytes = p.deleted_bytes
        status.skipped = p.skipped
        status.title = p.title

    async def _reap() -> None:
        try:
            # The gateway was built in the request (for the presence pre-check); enter and
            # close its clients here, in the task, so their lifetime is the run's -- not the
            # starting request's, which returns as soon as the task is scheduled.
            async with AsyncExitStack() as stack:
                for client in closers:
                    await stack.enter_async_context(client)
                run_session = await stack.enter_async_context(factory())
                executor = Executor(
                    run_session,
                    safety=safety,
                    settings=profile_settings,
                    dry_run=False,
                    gateway=gateway,
                    armed_recheck=_armed_now,
                    stop_recheck=_stop_now,
                    progress=on_progress,
                )
                # Bracket the run: a real deletion takes minutes and, until now, logged
                # nothing until it finished. This is the "a real reap began" line, so the
                # log shows the start even if the process dies mid-run.
                log.info("reap.started", run_id=run_id, planned=status.total)
                report = await executor.execute(run_id)
                await run_session.commit()
            status.report = _report_out(report)
            status.phase = "aborted" if report.state == RunState.ABORTED else "complete"
            status.deleted_items = report.deleted_items
            status.deleted_bytes = report.deleted_bytes
            status.skipped = report.skipped
            log.info(
                "reap.executed",
                run_id=report.run_id,
                state=report.state.value,
                deleted_items=report.deleted_items,
                deleted_bytes=report.deleted_bytes,
                deleted_unmeasured=report.deleted_unmeasured,
                skipped=report.skipped,
                # Why an aborted run stopped (a cap breach, a failed canary, a changed
                # manifest) lived only in the UI report; carry it here too, or the log
                # cannot tell one abort from another.
                aborted_reason=report.aborted_reason,
            )
            # Removing files leaves the last snapshot's queue and policy preview stale, so
            # kick a fresh scan -- on a completed OR a stopped run alike, as long as at least
            # one file was actually removed. Nothing removed means nothing went stale.
            # ``library_changed``, not ``deleted_items``: a movie Radarr deleted whose import
            # exclusion never landed ends FAILED, and reading the confirmed count alone left
            # the queue offering files that were already gone.
            if report.library_changed:
                launch_scan(app)
        except ExecutionError as exc:
            # A refused run the executor raised rather than executed (changed manifest, not
            # PLANNED, missing clients). The run row is untouched; surface the reason to the
            # poller so the sheet can show it.
            status.phase = "error"
            status.error = str(exc)
            log.info("reap.execute_refused", run_id=run_id, error=str(exc))
        except Exception as exc:
            # A background task must never crash silently -- surface it as an error the UI
            # shows. A crash can land here AFTER items were already removed (on_progress has
            # incremented the counts), so if anything was deleted the queue is stale and must
            # be rescanned, exactly as on a clean finish. A hard cancel (shutdown) raises
            # CancelledError, which is not an Exception, so it does NOT reach here and does not
            # kick a scan as the app is going down.
            status.phase = "error"
            status.error = str(exc)
            log.warning("reap.background_failed", run_id=run_id, error=str(exc))
            if status.deleted_items > 0:
                launch_scan(app)
        finally:
            status.running = False
            status.stopping = False

    # Held on app.state so the task is not garbage-collected mid-run. Deliberately NOT tied
    # to this request's lifetime -- that decoupling is the whole point.
    app.state.reap_task = asyncio.create_task(_reap())
    return status


@router.get("/runs/execute/status")
async def reap_status(request: Request) -> ReapStatus:
    """The current (or last) reap's progress. Cheap; the browser polls it while a reap runs,
    and reads it once on load to re-attach to one already in flight."""
    return _reap_status(request.app)


@router.post("/runs/{run_id}/stop")
async def stop_run(request: Request, run_id: int) -> ReapStatus:
    """Stop the running reap, gracefully. Reachable from any screen.

    Sets the flag the executor reads before its next item, so the run halts after the item
    in flight (never mid-item) and its abort path still tidies Plex for what was removed.
    Leaves deletion armed: this stops one run, it does not disarm the host. Turning deletion
    off (Policy -> Deletion) remains the separate, independent kill switch. Idempotent, and a
    no-op with 409 if this run is not the one running."""
    status = _reap_status(request.app)
    if not status.running or status.run_id != run_id:
        raise HTTPException(409, "That run is not currently running.")
    status.stopping = True
    log.info("reap.stop_requested", run_id=run_id)
    return status


def _report_out(report: RunReport) -> RunReportOut:
    return RunReportOut(
        run_id=report.run_id,
        dry_run=report.dry_run,
        state=report.state.value,
        aborted_reason=report.aborted_reason,
        would_delete_items=report.deleted_items,
        deleted_bytes=report.deleted_bytes,
        deleted_unmeasured=report.deleted_unmeasured,
        skipped=report.skipped,
        outcomes=[
            RunOutcomeOut(
                media_key=o.media_key,
                title=o.title,
                kind=o.kind,
                state=o.state.value,
                detail=o.detail,
                checks=[RunCheckOut(label=c.label, ok=c.ok) for c in o.checks],
            )
            for o in report.outcomes
        ],
    )


def _settings_out(settings: ProfileSettings, *, recovered: bool = False) -> ProfileSettingsIO:
    return ProfileSettingsIO(
        max_items_per_run=settings.max_items_per_run,
        max_bytes_per_run=settings.max_bytes_per_run,
        max_items_per_30d=settings.max_items_per_30d,
        max_bytes_per_30d=settings.max_bytes_per_30d,
        caps_enabled=settings.caps_enabled,
        grace_days=settings.grace_days,
        max_unmeasured_per_run=settings.max_unmeasured_per_run,
        settings_recovered=recovered,
    )


@router.get("/profile")
async def get_profile(request: Request) -> ProfileSettingsIO:
    """The pace settings: the caps a run obeys, plus the grace window it does *not* (the
    countdown is a notice, see ``services.grace``). Built-in defaults until one is saved.

    Reports ``settings_recovered`` when the stored blob was unreadable and these are the
    shipped defaults, so the Pace page can tell the operator to save again (rule 65)."""
    async with _sessions(request)() as session:
        profile = await active_profile(session)
    return _settings_out(profile.settings, recovered=profile.fell_back)


@router.put("/profile")
async def update_profile(request: Request, payload: ProfileSettingsIO) -> ProfileSettingsIO:
    """Update the caps and grace settings.

    The domain enforces the invariants (a per-run cap may not exceed the rolling 30-day
    cap; grace is at least a week), so a nonsensical combination comes back as a 422 with
    the reason -- never a silent clamp that would let a run do more than the owner meant.
    Saving does not enable the profile; acting is a separate, deliberate switch.
    """
    try:
        settings = ProfileSettings(
            max_items_per_run=payload.max_items_per_run,
            max_bytes_per_run=payload.max_bytes_per_run,
            max_items_per_30d=payload.max_items_per_30d,
            max_bytes_per_30d=payload.max_bytes_per_30d,
            caps_enabled=payload.caps_enabled,
            grace_days=payload.grace_days,
            max_unmeasured_per_run=payload.max_unmeasured_per_run,
        )
    except ValidationError as exc:
        raise HTTPException(
            422,
            detail=[
                {
                    "loc": [str(p) for p in e["loc"]],
                    "msg": e["msg"].removeprefix("Value error, "),
                    "type": e["type"],
                }
                for e in exc.errors()
            ],
        ) from exc

    async with _sessions(request)() as session:
        saved = await save_profile_settings(session, settings)
        await session.commit()
    return _settings_out(saved)
