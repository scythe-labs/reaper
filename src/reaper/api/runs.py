# SPDX-License-Identifier: AGPL-3.0-or-later
"""Building, reviewing, dry-running, and executing a reap plan.

This is where the reap loop meets the owner. A plan is built from the
latest snapshot's condemned set, journalled, and shown, with every step
and the exact request it would issue. It can be dry-run to prove the
whole chain end to end without deleting, and finally executed for real.

The real execution path (``POST /runs/{id}/execute``) is the one endpoint
that deletes, and it is gated hard. Deletion must be enabled on the host,
and the caller must echo back the plan's exact content-bound confirmation
phrase ("REAP 7 SOULS 214 GB"). Underneath, the executor's own
interlocks, the manifest re-check, caps, canary, the streaming veto, and
the played-since-approval check, each run and each resolves toward
keeping the file. The dry run stays fully offline: it walks every
interlock and sends nothing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from contextlib import AsyncExitStack

import structlog
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.aio import report_background_failure
from reaper.api import tags as api_tags
from reaper.api.deps import newest_snapshot, session_factory, state_singleton
from reaper.api.errors import refuse, refuse_from, validation_error_items
from reaper.api.scan import launch_scan
from reaper.api.schemas import (
    ActionStepOut,
    CreateRunIn,
    ExecuteRunIn,
    ProfileSettingsIO,
    RunCheckOut,
    RunListOut,
    RunOut,
    RunOutcomeOut,
    RunOutcomeReadOut,
    RunOutcomesOut,
    RunReportOut,
    RunStepsOut,
    RunSummaryOut,
)
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.models import ActionStep, Candidate, ReapRun, RunState, StepState
from reaper.engine.explanation import ReasonKey
from reaper.engine.policy import ProfileSettings
from reaper.engine.reason import Reason, from_stored, to_wire
from reaper.refusal import english
from reaper.services import app_settings, run_totals, whitelist
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

#: The two ``/profile`` routes below carry the caps, and the operator edits
#: those on the Policy page, not on Reap (``PolicyEditor``'s "Pace and
#: limits"). They get their own router because FastAPI concatenates a
#: route-level tag with its router's instead of letting the route override
#: it, so ``@router.get("/profile", tags=[POLICY])`` would file one
#: operation under two sections, which ``tests/test_openapi_tags.py``
#: refuses.
profile_router = APIRouter(prefix="/api", tags=[api_tags.POLICY])


#: How many journal rows a run's detail response carries, and the default page of the steps
#: route. A plan of 500 seasons is 1,500 rows, each with a path and a stringified request body,
#: and the table draws 50 of them. The rest are a route away rather than in every response.
#: The outcomes route (GET /runs/{id}/outcomes) reuses this same constant: it pages the
#: same journal at item granularity instead of step granularity, so producer and consumer
#: agree on one page size rather than each guessing (rule 131).
STEP_PAGE = 50

#: The default and max page of GET /runs, named so a page size an operator's history view
#: reads is a declaration rather than a bare literal repeated at the call site.
RUN_LIST_PAGE = 50

#: A step this run has decided, one way or another. Filters GET /runs/{id}/outcomes to
#: items with something to report; a step still PENDING or SENT has not been reached yet.
_DECIDED_STATES = (StepState.VERIFIED, StepState.FAILED, StepState.SKIPPED)


def _reason_key(reason: Reason | None) -> ReasonKey | None:
    """Return a typed reason as the wire shape an optional response field
    carries. This is one conversion, used directly for a live,
    possibly-absent :class:`~reaper.engine.reason.Reason` (``_report_out``'s
    ``RunReportOut.aborted_reason``), and through :func:`_thaw_reason` for
    one recovered from a stored journal column. ``RunOutcomeOut.detail_reason``
    and ``RunCheckOut.label_reason`` are never absent, so ``_report_out``
    wires those two straight through ``ReasonKey.model_validate(to_wire(...))``
    instead."""
    return None if reason is None else ReasonKey.model_validate(to_wire(reason))


def _thaw_reason(stored: str | None) -> ReasonKey | None:
    """Return the typed key for a stored journal reason column
    (``ActionStep.error``, ``ReapRun.aborted_reason``), through
    ``engine.reason.from_stored``. A row written after typed reasons landed
    decodes its JSON. One written before thaws as a ``legacy`` reason. This
    returns it as the wire shape :func:`_reason_key` gives a live one.
    """
    return _reason_key(from_stored(stored))


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
    """Return the caps and grace the operator saved, or refuse if they could
    not be read.

    ``active_profile`` deliberately never raises. It is read by the scan,
    by execute, and by the very settings page an operator would use to
    repair a broken blob, so a hard error there would take out the fix
    along with everything else. Instead it falls back to the shipped
    defaults and flags that, and those defaults can be looser than what
    was saved: a run cap of 5 becomes 10, a grace of 30 becomes 14.

    Every route that acts on those numbers must call this function
    instead of ``active_profile_settings`` directly, which drops that
    flag on the floor. Calling the plain accessor here would leave the
    one route that deletes bounded by numbers nobody chose, with nothing
    anywhere saying so. The scan path already degrades on this
    (``scan_runner``). This is the same refusal for the routes that plan,
    preview, and send.

    This is deliberately not applied to the other readers, which display
    rather than delete. ``list_runs`` renders numbers it never acts on,
    and taking out the page an operator reads to see what is pending
    would be the wrong blast radius for a fault the deleting route
    already refuses. The policy editor's two reads only size a warning.
    ``api/whitelist``'s grace-clock write is the one worth revisiting: a
    default grace is shorter than a saved one, so it starts the clock
    early. But refusing there would refuse a spare, which is the
    operator's keep action, and no reap can run meanwhile, since the scan
    degrades.
    """
    profile = await active_profile(session)
    if profile.repaired:
        refuse(409, "error.runs.limits_unreadable")
    return profile.settings


class _RunReads:
    """Per-request memo for the three reads every run's output needs.

    ``list_runs`` renders up to 200 runs. Without this memo, each one
    would independently re-read the hand overrides, the reap profile, and
    the whole effective condemned set of its snapshot, pulling every
    condemned row in the snapshot into memory once per run. Runs on a
    page overwhelmingly share a snapshot, so each read happens once here
    instead, and every run that needs it gets the same answer.

    Scoped to one request and never held across one. The overrides and
    the allowance are deliberately read live, so the count and total in
    front of the owner describe the settings in force now. A cache that
    outlived the request would bring back exactly the staleness those
    live reads exist to prevent.
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
    """Return exactly the candidates this run would actually delete,
    deduped to one per item.

    This dedupes by media_key because a season is three steps sharing one
    key, and counting per step would triple its size and item count in
    the very phrase the owner types to approve. The item is the unit the
    executor's caps count, so the review surface and the enforcement
    surface agree. ``dict.fromkeys`` dedupes in plan order.

    The membership is the effective condemned set (services.condemned),
    re-read now, exactly as the executor derives it. An item spared after
    the plan was built has steps but will not be deleted, so it must not
    inflate the confirmation phrase, and a hand reap whose override was
    since removed drops out the same way. Recomputing this at execute
    time also makes a post-plan override change the expected phrase, so
    the owner is asked to reload and re-confirm the changed reap instead
    of approving a count that no longer matches.

    Items with no measured size drop out here too, exactly as they do in
    the executor's ``_deletable``, and for the same reason: the send
    paths refuse them, so counting them would put a count in front of the
    owner describing a different set than the one the run will act on.
    Unless the operator's allowance is open, in which case they are acted
    on and belong in the count, read live below, so both surfaces agree.
    """
    memo = reads if reads is not None else _RunReads(session)
    if steps is None:
        steps = await _run_steps(session, run)
    by_key = await memo.condemned(run.snapshot_id)
    # The allowance is read here, at the moment the numbers are produced,
    # so the count and total in front of the owner describe the set the
    # executor will act on under the settings in force now, not the ones
    # in force when the plan was built.
    allow_unmeasured = await memo.allow_unmeasured()
    return [
        by_key[k]
        for k in dict.fromkeys(s.media_key for s in steps)
        if k in by_key and (allow_unmeasured or size_confirmed(by_key[k]))
    ]


async def _run_out(
    session: AsyncSession, run: ReapRun, *, reads: _RunReads | None = None
) -> RunOut:
    # Steps are fetched once and handed down. Without this, this function
    # and _planned_candidates would each query them separately, so every
    # run would cost two step reads instead of one.
    steps = await _run_steps(session, run)
    planned = await _planned_candidates(session, run, steps=steps, reads=reads)

    return RunOut(
        id=run.id,
        snapshot_id=run.snapshot_id,
        state=run.state.value,
        item_count=len(planned),
        # The same set the phrase is derived from. The total covers the
        # items that have a size and never absorbs the ones that do not.
        # When the allowance has admitted any, the phrase says so with its
        # own suffix, instead of letting this number quietly run low.
        total_bytes=plan_bytes(planned)[0],
        confirmation_phrase=confirmation_phrase(planned) if planned else "REAP 0 SOULS 0 GB",
        held_back_unknown_size=run.held_back_unknown_size,
        step_count=len(steps),
        # The window is applied here, never in `_run_steps` or
        # `_planned_candidates`. Both of those feed the confirmation phrase.
        # `planned` above comes off the full list, and `execute_run`
        # re-derives the same phrase through `_planned_candidates` at send
        # time. A limit in either function would shrink the phrase and the
        # server's expectation together, so the comparison would still
        # pass, while `services.executor` loads its own steps and deletes
        # every one. The operator would type REAP 50 SOULS and 500 would go.
        #
        # So this slices the iterable below, and must never rebind `steps`.
        # Two later uses of that name read the full list, and a rebinding
        # would break whichever one sits above it: the `_planned_candidates`
        # call, the failure this comment is about, and `step_count` just
        # above, which would then report the window as the plan and
        # silently empty the "N more steps" line the operator reads instead
        # of the rows.
        # `GET /api/runs/{id}/steps` serves anything past the window.
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
                error_reason=_thaw_reason(s.error),
            )
            for s in steps[:STEP_PAGE]
        ],
    )


@router.post("/runs")
async def create_run(request: Request, payload: CreateRunIn | None = None) -> RunOut:
    """Build a plan from the latest snapshot's condemned set. This journals
    it and sends nothing.

    With no body, the plan covers the whole condemned set. With
    ``media_keys``, it covers just those items: "reap selected", the safe
    path for a first single deletion. The plan is bound to that snapshot
    and policy, and records a content hash of exactly what it would
    delete, so it cannot later be executed against a different library
    than reviewed.
    """
    # An omitted field (no body, or ``media_keys=null``) means "the whole
    # condemned set". An explicit empty list means "nothing selected" and
    # must never collapse to the whole set. ``[]`` is falsy, so a naive
    # truthiness test would invert a select-nothing request into a
    # select-everything plan. This passes the empty set through instead,
    # so build_plan fails closed on it. Deletion planning must never turn
    # "nothing" into "everything".
    only = (
        set(payload.media_keys) if payload is not None and payload.media_keys is not None else None
    )
    async with session_factory(request)() as session:
        snapshot = await newest_snapshot(session)
        if snapshot is None:
            refuse(404, "error.runs.no_scan_to_plan")

        try:
            # ``approved_by`` records that the plan was built through the
            # API rather than naming a person. This route is reachable
            # unattended, and every run therefore stores the same string.
            # It is a column on the run, not a field on any response.
            run = await build_plan(
                session,
                snapshot_id=snapshot.id,
                approved_by="api",
                only_media_keys=only,
                max_unmeasured=(await _saved_limits_or_refuse(session)).max_unmeasured_per_run,
            )
        except PlanError as exc:
            refuse_from(exc)

        out = await _run_out(session, run)
        await session.commit()
        return out


@router.get("/runs")
async def list_runs(
    request: Request,
    # Bounded at the boundary, both ends. Without a lower bound, a negative
    # limit would pass straight through to ``LIMIT -1``, which SQLite reads
    # as no limit, returning every run ever made. Cheap rows make that less
    # costly than it would otherwise be, but not bounded, so the bound
    # stays here, where a bad value is refused instead of clamped silently.
    limit: int = Query(RUN_LIST_PAGE, ge=1, le=200),
    offset: int = Query(0, ge=0),
    executed_only: bool = Query(False),
) -> RunListOut:
    """Return the recent plans, as stored rows and nothing more (see
    ``RunSummaryOut``), plus how many rows match in total.

    This deliberately is not ``RunOut``. That shape's counts, totals, and
    phrase are each derived from the effective condemned set of the run's
    snapshot, so building it per run would read the whitelist, the
    profile, and the whole candidate table once per row, fifty times on
    every visit to the Reap page. Opening a run goes to
    ``GET /runs/{id}``, which derives them for the one run being looked at.

    ``offset`` pages the whole history: nothing bounds how many runs a
    long-lived install has executed, unlike a scan's 30-snapshot retention.

    ``executed_only`` drops every row still PLANNED, a plan that was built
    (the head Reap button, a standalone practice run) and never executed,
    from both the page and ``total``: filtering a page after it is fetched
    would leave the two disagreeing with each other and with the true
    count. The SPA's one caller, the Reap page's history, passes it true;
    the default stays permissive for raw API readers, for whom a planned
    row is data, not noise.
    """
    async with session_factory(request)() as session:
        rows_stmt = select(ReapRun).order_by(ReapRun.id.desc()).limit(limit).offset(offset)
        count_stmt = select(func.count()).select_from(ReapRun)
        if executed_only:
            rows_stmt = rows_stmt.where(ReapRun.state != RunState.PLANNED)
            count_stmt = count_stmt.where(ReapRun.state != RunState.PLANNED)
        runs = list((await session.execute(rows_stmt)).scalars().all())
        total = (await session.execute(count_stmt)).scalar_one()
        # No memo needed: nothing here is derived, so there is no expensive read to share.
        return RunListOut(
            runs=[
                RunSummaryOut(
                    id=r.id,
                    state=r.state.value,
                    approved_at=r.approved_at.isoformat(),
                    finished_at=r.finished_at.isoformat() if r.finished_at else None,
                    aborted_reason=_thaw_reason(r.aborted_reason),
                    deleted_items=r.deleted_items,
                    deleted_bytes=r.deleted_bytes,
                    deleted_unmeasured=r.deleted_unmeasured,
                    skipped=r.skipped,
                )
                for r in runs
            ],
            total=total,
        )


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: int) -> RunOut:
    """One run, with the first page of its journal.

    ``steps`` is a window, not the whole plan. ``step_count`` says how many rows there are and
    ``GET /api/runs/{run_id}/steps`` serves the rest.
    """
    async with session_factory(request)() as session:
        run = await session.get(ReapRun, run_id)
        if run is None:
            refuse(404, "error.runs.not_found")
        return await _run_out(session, run)


@router.get("/runs/{run_id}/steps")
async def get_run_steps(
    request: Request,
    run_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(STEP_PAGE, ge=1, le=500),
) -> RunStepsOut:
    """Return a window of one run's journal, for reading past what the run
    detail carries.

    Reads only. Nothing here feeds the confirmation phrase or any count
    the operator acts on. That is `_planned_candidates`, which the detail
    route and the execute route both derive from the whole step list. A
    cap belongs here and in `_run_out`'s serialization, never in the
    shared helper underneath them.
    """
    async with session_factory(request)() as session:
        run = await session.get(ReapRun, run_id)
        if run is None:
            refuse(404, "error.runs.not_found")
        steps = await _run_steps(session, run)
        return RunStepsOut(
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
                    error_reason=_thaw_reason(s.error),
                )
                for s in steps[offset : offset + limit]
            ],
            step_count=len(steps),
            offset=offset,
        )


async def _run_outcomes(session: AsyncSession, run: ReapRun) -> list[ActionStep]:
    """Every item's outcome so far, oldest (the canary) first: the terminal delete step
    of every item that has reached one.

    One row per item already: a season's reversible unmonitor/verify steps never carry
    the item's own kind (``run_totals.TERMINAL_DELETE_KINDS`` is the fixed pair that
    does), so there is no grouping to do. An item still PENDING or SENT has not been
    decided yet, so it is filtered out here rather than in the caller, which is what lets
    a run mid-flight and one long finished answer from the very same read: the list
    simply grows as the run goes, and is complete once it ends.
    """
    steps = (
        (
            await session.execute(
                select(ActionStep)
                .where(
                    ActionStep.run_id == run.id,
                    ActionStep.kind.in_(run_totals.TERMINAL_DELETE_KINDS),
                )
                .order_by(ActionStep.ordinal, ActionStep.id)
            )
        )
        .scalars()
        .all()
    )
    return [s for s in steps if s.state in _DECIDED_STATES]


@router.get("/runs/{run_id}/outcomes")
async def get_run_outcomes(
    request: Request,
    run_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(STEP_PAGE, ge=1, le=500),
) -> RunOutcomesOut:
    """Per-item outcomes, reconstructed from the durable journal and the frozen
    candidates it condemned, rather than the in-memory report a real send builds
    (``ReapStatus.report``, gone the moment the process restarts).

    Answers a run still executing exactly as it answers one long finished, from the same
    read: see ``_run_outcomes``. This is what lets an item status log follow a run in
    flight and a reopened history view render the same way, off one source instead of
    two that could disagree.
    """
    async with session_factory(request)() as session:
        run = await session.get(ReapRun, run_id)
        if run is None:
            refuse(404, "error.runs.not_found")
        decided = await _run_outcomes(session, run)
        page = decided[offset : offset + limit]

        candidates: dict[str, Candidate] = {}
        media_keys = [s.media_key for s in page]
        if media_keys:
            rows = (
                (
                    await session.execute(
                        select(Candidate).where(
                            Candidate.snapshot_id == run.snapshot_id,
                            Candidate.media_key.in_(media_keys),
                        )
                    )
                )
                .scalars()
                .all()
            )
            candidates = {c.media_key: c for c in rows}

        return RunOutcomesOut(
            outcomes=[
                RunOutcomeReadOut(
                    media_key=s.media_key,
                    title=candidates[s.media_key].title if s.media_key in candidates else "",
                    kind=s.kind,
                    size_bytes=(
                        candidates[s.media_key].size_bytes if s.media_key in candidates else None
                    ),
                    state=s.state.value,
                    error_reason=_thaw_reason(s.error),
                    is_canary=s.ordinal == 0,
                    file_removed=s.file_removed_at is not None,
                )
                for s in page
            ],
            outcome_count=len(decided),
            offset=offset,
        )


@router.post("/runs/{run_id}/dry-run")
async def dry_run(request: Request, run_id: int) -> RunReportOut:
    """Walk the plan end to end with every interlock, and send nothing.

    This is the proof. The manifest re-check, the caps, and the canary
    ordering all run for real, and every mutating call is recorded
    instead of issued. A dry run never proves the live per-item vetoes:
    someone streaming the item right now, a play landing after approval,
    a missing rating key, a file that grew since approval, or deletion
    switched off mid-run. Those are moment-of-deletion checks that only
    run on a real send, where the moment is real. The transport guard
    sits underneath as the independent backstop.
    """
    settings: Settings = request.app.state.settings

    async with session_factory(request)() as session:
        # Read-only safety by construction here. The executor's dry_run
        # does not send, and even if it tried, this ceiling forbids it.
        # This reads both switches, so a UI emergency stop is reflected,
        # not just the env flag.
        safety = await app_settings.runtime_safety(session, settings)
        run = await session.get(ReapRun, run_id)
        if run is None:
            refuse(404, "error.runs.not_found")

        # The owner's configured caps, not a hardcoded default. This is what lets a real
        # (large) condemned set be simulated: the cap is a decision the owner makes.
        profile_settings = await _saved_limits_or_refuse(session)
        executor = Executor(session, safety=safety, settings=profile_settings, dry_run=True)
        try:
            report = await executor.execute(run_id)
        except ExecutionError as exc:
            # A voided run (changed manifest, already executed) is a 409.
            # The plan is no longer valid, and the owner needs to re-plan
            # instead of retrying.
            refuse_from(exc)
        await session.commit()

    return _report_out(report)


class ReapStatus(BaseModel):
    """Where a running reap has got to. The browser polls it, and the
    running job mutates it in place, the same shape and pattern as
    ``ScanStatus``.

    A reap runs detached from the request that starts it, so it survives
    navigating away and closing the tab. This is how the browser follows
    a run and re-attaches to one already in flight, and where the Stop it
    can reach from any screen is read and set. These are cumulative
    counts, never deltas, so a dropped poll loses no ground.
    """

    running: bool = False
    run_id: int | None = None
    stopping: bool = False
    """The operator pressed Stop. Set by ``POST /runs/{id}/stop``, read by
    the executor before each item. This is a graceful halt, an
    ExecutionError at the next item, never a hard task cancel, so the
    abort path still tidies Plex for what was already removed."""
    phase: str = "idle"  # idle | reaping | complete | aborted | error
    done: int = 0
    total: int = 0
    deleted_items: int = 0
    deleted_bytes: int = 0
    skipped: int = 0
    title: str = ""
    #: Why the run stopped, as a typed reason. This is the executor's own
    #: catalog code (``exc.as_reason()``) on a refused run, or
    #: ``error.reap.unexpected`` for anything else. ``null`` while running
    #: and on a clean finish.
    error_reason: ReasonKey | None = None


def _reap_status(app: FastAPI) -> ReapStatus:
    return state_singleton(app, "reap_status", ReapStatus)


def reap_in_flight(app: FastAPI) -> bool:
    """Whether a reap is mid-run, for callers that must not take the
    database write lock.

    This reads the status without creating one, so a predicate can be
    asked before any reap has ever started. ``scheduler.sweep_old_snapshots``
    is the caller. A reap commits its journal per step, and a lock it
    loses leaves the run wedged, so the housekeeping ``VACUUM`` yields to
    it instead of the other way round.
    """
    status: ReapStatus | None = getattr(app.state, "reap_status", None)
    return status is not None and status.running


def _preflight_refusal(gateway: ReapGateway) -> str | None:
    """Return the executor's client-presence refusals, checked synchronously
    so the endpoint returns them immediately, a clear 409, instead of only
    through the status poll after the task has started. The executor
    re-checks the very same things as its own backstop. This is the
    earlier, clearer refusal, the way the arm gate mirrors the transport
    guard, so the two messages must stay in step."""
    if gateway.plex is None:
        return "error.runs.preflight_no_plex"
    if gateway.tautulli is None:
        return "error.runs.preflight_no_tautulli"
    return None


@router.post("/runs/{run_id}/execute")
async def execute_run(request: Request, run_id: int, payload: ExecuteRunIn) -> ReapStatus:
    """Start a real reap. This is the one endpoint in Reaper that deletes.

    Every gate must pass, and each resolves toward keeping the file.

    1. **Deletion must be enabled on the host** (403 otherwise). The
       transport guard would refuse the calls anyway. This is the
       earlier, clearer refusal.
    2. **The typed confirmation must match the plan's current phrase
       exactly** (409 otherwise). The phrase is recomputed here from the
       plan, so a stale tab, whose phrase was for a different plan,
       cannot replay it.
    3. **The executor's own interlocks**, the manifest re-check, caps
       abort-not-truncate, the canary, the per-item streaming veto, and
       the played-since-approval check, each run and can still spare or
       abort.

    The gates above run synchronously, so a bad request is refused
    immediately. The reap itself then runs detached (like a scan) on
    ``app.state.reap_task``, reporting to ``app.state.reap_status``,
    which the browser polls. A long run survives navigating away and
    closing the tab, and Stop is reachable from any screen. This returns
    the initial status, not the finished report. The report lands on the
    status when the run ends.

    The scheduler never calls this. A real reap is a deliberate act by a
    person who typed the phrase, not something a timer can trigger.
    """
    settings: Settings = request.app.state.settings
    box: SecretBox = request.app.state.secret_box
    factory = session_factory(request)
    app = request.app

    # Claims the single reap slot synchronously, with no await between the
    # check and the set, so two execute requests racing each other cannot
    # both pass. A check-then-await-then-set would let both see ``running``
    # false and start two reaps over the one shared status. Every display
    # field is reset in the same synchronous stretch, so a poll can never
    # catch a half-initialized status, a stale report beside a fresh
    # ``running``. This is released on any synchronous refusal below,
    # before the task is ever created.
    status = _reap_status(app)
    if status.running:
        # This logs here rather than through the handler below, which is
        # the one refusal that cannot pass through it. This request has not
        # claimed the slot, and the handler releases it. Routing the loser
        # through that path would clear the winning run's ``running`` flag
        # while its task keeps deleting, opening the slot for a third
        # request to start a second reap over the one shared status.
        log.info("reap.refused", run_id=run_id, status=409, code="error.runs.already_running")
        refuse(409, "error.runs.already_running")
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
    status.error_reason = None

    try:
        async with factory() as session:
            safety = await app_settings.runtime_safety(session, settings)
            if not safety.destructive_allowed:
                # Same two conditions and the same two codes as the
                # executor's own backstop check
                # (services.executor.Executor.execute), so the two cannot
                # drift apart.
                if safety.recovery_mode:
                    refuse(403, "error.safety.recovery_mode_active")
                refuse(403, "error.safety.deletion_off")

            run = await session.get(ReapRun, run_id)
            if run is None:
                refuse(404, "error.runs.not_found")

            planned = await _planned_candidates(session, run)
            expected = confirmation_phrase(planned) if planned else "REAP 0 SOULS 0 GB"
            if payload.confirmation_phrase.strip() != expected:
                refuse(409, "error.runs.confirmation_mismatch", expected=expected)
            profile_settings = await _saved_limits_or_refuse(session)
            status.total = len(planned)

        # Builds the live clients now, in the request, so a misconfigured
        # run is refused immediately, and hands the same clients to the
        # background task, which enters and closes them. On a refusal this
        # enter-and-exits the built-but-unused clients, to close them
        # cleanly instead of leaking them.
        gateway, closers = await build_reap_gateway(factory, box, safety=safety)
        if (preflight_code := _preflight_refusal(gateway)) is not None:
            async with AsyncExitStack() as closing:
                for client in closers:
                    await closing.enter_async_context(client)
            refuse(409, preflight_code)
    except Exception as exc:
        # Any synchronous failure before the task is created releases the
        # slot, not only an HTTPException refusal, but a crypto or DB error
        # out of build_reap_gateway or a session read. Catching only
        # HTTPException here would leave ``running`` stuck True with no
        # task to ever clear it, wedging the one deletion endpoint at a
        # permanent 409 until restart. HTTPException is an Exception, so
        # the 4xx refusals still propagate unchanged.
        status.running = False
        status.phase = "idle"
        status.run_id = None
        # Every synchronous refusal raised after the slot was claimed logs
        # here. The slot-claim 409 above is the one that cannot reach here,
        # and it logs itself. This route is the only one that deletes, and
        # its first line otherwise would be `reap.started`, emitted only
        # after every interlock has already passed. Without this log, "I
        # typed the phrase, pressed Reap, and nothing happened" would leave
        # no record anywhere: the browser showed a message the operator has
        # since dismissed, and the server kept none of it. The status and
        # detail together name which interlock refused, because they are
        # exactly what the operator was shown.
        #
        # This logs at INFO, not DEBUG, because nobody gets to reproduce
        # this one with the level turned up. The state that refused it, a
        # plan built under a policy since edited, a phrase that no longer
        # matches the content, has moved on by the time they try.
        if isinstance(exc, HTTPException):
            log.info("reap.refused", run_id=run_id, status=exc.status_code, detail=exc.detail)
        else:
            log.warning("reap.refused", run_id=run_id, error=str(exc))
        raise

    async def _armed_now() -> bool:
        # The executor's mid-run kill switch. This uses a fresh session per
        # read, because the run's own session caches rows across its
        # per-item commits and would keep reporting the switch as it stood
        # when the run began.
        async with factory() as check_session:
            return await app_settings.destructive_enabled(check_session, settings)

    async def _stop_now() -> bool:
        # The explicit Stop, read off the polled status the browser sets
        # from any screen. This is graceful by construction. The executor
        # raises ExecutionError at the next item, so its abort path runs
        # and still tidies Plex. This must never be a task.cancel().
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
            # The gateway was built in the request, for the presence
            # pre-check. This enters and closes its clients here, in the
            # task, so their lifetime is the run's, not the starting
            # request's, which returns as soon as the task is scheduled.
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
                # This brackets the run. A real deletion takes minutes, and
                # without this line nothing would log until it finished.
                # This is the "a real reap began" line, so the log shows
                # the start even if the process dies mid-run.
                log.info("reap.started", run_id=run_id, planned=status.total)
                report = await executor.execute(run_id)
                try:
                    # This is a second layer, not the durability itself.
                    # The executor commits its own journal per item and its
                    # terminal row at the end, so this only flushes the run
                    # object it left in the session. It is never fatal.
                    # There is nothing here that is not already on disk,
                    # and the run is over either way.
                    await run_session.commit()
                except Exception as exc:
                    log.warning("reap.trailing_commit_failed", run_id=run_id, error=str(exc))
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
                # Why an aborted run stopped (a cap breach, a failed
                # canary, a changed manifest) otherwise lives only in the
                # UI report. This carries it here too, or the log cannot
                # tell one abort from another.
                aborted_reason=english(report.aborted_reason) if report.aborted_reason else None,
            )
            # Removing files leaves the last snapshot's queue and policy
            # preview stale, so this kicks a fresh scan, on a completed or
            # a stopped run alike, as long as at least one file was
            # actually removed. Nothing removed means nothing went stale.
            # This checks ``library_changed``, not ``deleted_items``. A
            # movie Radarr deleted whose import exclusion never landed
            # ends FAILED, and reading the confirmed count alone would
            # leave the queue offering files that were already gone.
            if report.library_changed:
                launch_scan(app)
        except ExecutionError as exc:
            # A refused run the executor raised rather than executed
            # (changed manifest, not PLANNED, missing clients). The run
            # row is untouched. This surfaces the reason to the poller so
            # the sheet can show it.
            status.phase = "error"
            status.error_reason = ReasonKey.model_validate(to_wire(exc.as_reason()))
            log.info("reap.execute_refused", run_id=run_id, error=str(exc))
        except Exception as exc:
            # A background task must never crash silently. This surfaces
            # it as an error the UI shows. A crash can land here after
            # items were already removed (on_progress has incremented the
            # counts), so if anything was deleted the queue is stale and
            # must be rescanned, exactly as on a clean finish. A hard
            # cancel (shutdown) raises CancelledError, which is not an
            # Exception, so it does not reach here and does not kick a
            # scan while the app is going down.
            status.phase = "error"
            status.error_reason = ReasonKey.model_validate(
                to_wire(Reason("error.reap.unexpected", {"error": str(exc)}))
            )
            log.warning("reap.background_failed", run_id=run_id, error=str(exc))
            if status.deleted_items > 0:
                launch_scan(app)
        finally:
            status.running = False
            status.stopping = False

    # This is held on app.state so the task is not garbage-collected
    # mid-run. Its lifetime is deliberately not tied to this request's
    # lifetime, since the reap must outlive the request that started it.
    task = asyncio.create_task(_reap(), name="reap")
    # `_reap` catches Exception itself, so what reaches this callback is a
    # raise from the `finally` block above, or any non-Exception
    # BaseException. `status.running = False` lives in that block, so a
    # failure there leaves the sheet polling a reap that has already
    # stopped, with nothing else to say why.
    task.add_done_callback(report_background_failure)
    app.state.reap_task = task
    return status


@router.get("/runs/execute/status")
async def reap_status(request: Request) -> ReapStatus:
    """The current, or last, reap's progress. This is cheap, and the browser
    polls it while a reap runs, and reads it once on load to re-attach to
    one already in flight."""
    return _reap_status(request.app)


@router.post("/runs/{run_id}/stop")
async def stop_run(request: Request, run_id: int) -> ReapStatus:
    """Stop the running reap, gracefully. Reachable from any screen.

    This sets the flag the executor reads before its next item, so the
    run halts after the item in flight, never mid-item, and its abort
    path still tidies Plex for what was removed. This leaves deletion
    armed. It stops one run. It does not disarm the host. Turning
    deletion off (Policy -> Deletion) remains the separate, independent
    kill switch. This is idempotent, and a no-op with 409 if this run is
    not the one running."""
    status = _reap_status(request.app)
    if not status.running or status.run_id != run_id:
        # The sibling of the execute route's slot-claim refusal. The
        # success path logs, and without this the refusal would not, so "I
        # pressed Stop and it kept going" would leave the same nothing.
        # This is usually a Stop aimed at a run that already finished,
        # exactly the state that has moved on by the time anyone looks.
        log.info("reap.stop_refused", run_id=run_id, status=409, code="error.runs.not_running")
        refuse(409, "error.runs.not_running")
    status.stopping = True
    log.info("reap.stop_requested", run_id=run_id)
    return status


def _report_out(report: RunReport) -> RunReportOut:
    return RunReportOut(
        run_id=report.run_id,
        dry_run=report.dry_run,
        state=report.state.value,
        aborted_reason=_reason_key(report.aborted_reason),
        would_delete_items=report.would_delete_items if report.dry_run else report.deleted_items,
        deleted_bytes=report.would_delete_bytes if report.dry_run else report.deleted_bytes,
        deleted_unmeasured=(
            report.would_delete_unmeasured if report.dry_run else report.deleted_unmeasured
        ),
        skipped=report.skipped,
        outcomes=[
            RunOutcomeOut(
                media_key=o.media_key,
                title=o.title,
                kind=o.kind,
                state=o.state.value,
                # This does not use `_reason_key`. `o.detail` and `c.label`
                # are never `None` (`StepOutcome.detail` and
                # `StepCheck.label` carry no `| None`), and the field they
                # fill is required, so this stays the non-optional wire
                # shape instead of widening to `_reason_key`'s
                # `ReasonKey | None`.
                detail_reason=ReasonKey.model_validate(to_wire(o.detail)),
                checks=[
                    RunCheckOut(label_reason=ReasonKey.model_validate(to_wire(c.label)), ok=c.ok)
                    for c in o.checks
                ],
                is_canary=o.is_canary,
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


@profile_router.get("/profile")
async def get_profile(request: Request) -> ProfileSettingsIO:
    """Return the pace settings. These are the caps a run obeys, and
    separately the grace window, which only drives a notice (see
    ``services.grace``). Built-in defaults apply until one is saved.

    Reports ``settings_recovered`` when the stored blob was unreadable and
    these are the shipped defaults, so the Pace page can tell the
    operator to save again."""
    async with session_factory(request)() as session:
        profile = await active_profile(session)
    return _settings_out(profile.settings, recovered=profile.fell_back)


@profile_router.put("/profile")
async def update_profile(request: Request, payload: ProfileSettingsIO) -> ProfileSettingsIO:
    """Update the caps and grace settings.

    The domain enforces the invariants. A per-run cap may not exceed the
    rolling 30-day cap, and grace is at least a week. So a nonsensical
    combination comes back as a 422 with the reason, never a silent clamp
    that would let a run do more than the owner meant.

    Saving these settings deletes nothing and arms nothing. Removing
    files takes turning deletion on and typing the confirmation phrase
    for the exact plan reviewed, on a different screen. There is no on
    switch on the profile itself.
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
        raise HTTPException(422, detail=validation_error_items(exc.errors())) from exc

    async with session_factory(request)() as session:
        saved = await save_profile_settings(session, settings)
        await session.commit()
    return _settings_out(saved)
