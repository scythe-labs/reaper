# SPDX-License-Identifier: AGPL-3.0-or-later
"""Building, reviewing, dry-running, and executing a reap plan.

This is where the reap loop meets the owner. A plan is built from the latest snapshot's
condemned set, journalled, and shown -- every step, with the exact request it would
issue -- can be dry-run to prove the whole chain end to end without deleting, and finally
executed for real.

The real execution path (``POST /runs/{id}/execute``) is the one endpoint that deletes,
and it is gated hard: deletion must be enabled on the host, and the caller must echo back
the plan's exact content-bound confirmation phrase ("REAP 7 ITEMS 214 GB"). Underneath, the
executor's own interlocks -- manifest re-check, caps, canary, the streaming veto and the
played-since-approval check -- each run and each resolves toward keeping the file. The
dry run remains fully offline: it walks every interlock and sends nothing.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.api.schemas import (
    ActionStepOut,
    CreateRunIn,
    ExecuteRunIn,
    ProfileSettingsIO,
    RunCheckOut,
    RunOut,
    RunOutcomeOut,
    RunReportOut,
)
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.models import ActionStep, Candidate, ReapRun, Snapshot
from reaper.engine.policy import ProfileSettings
from reaper.services import app_settings, whitelist
from reaper.services.condemned import effective_condemned
from reaper.services.executor import ExecutionError, Executor, RunReport, size_confirmed
from reaper.services.planner import PlanError, build_plan, confirmation_phrase, plan_bytes
from reaper.services.profiles import active_profile_settings, save_profile_settings
from reaper.services.scan_runner import build_reap_gateway

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api")


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


async def _planned_candidates(session: AsyncSession, run: ReapRun) -> list[Candidate]:
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
    steps = await _run_steps(session, run)
    decisions = await whitelist.overrides(session)
    by_key = await effective_condemned(session, run.snapshot_id, decisions)
    # The allowance is read here, at the moment the numbers are produced, so the count
    # and total in front of the owner describe the set the executor will act on under the
    # settings in force NOW -- not the ones in force when the plan was built.
    allow_unmeasured = (await active_profile_settings(session)).max_unmeasured_per_run > 0
    return [
        by_key[k]
        for k in dict.fromkeys(s.media_key for s in steps)
        if k in by_key and (allow_unmeasured or size_confirmed(by_key[k]))
    ]


async def _run_out(session: AsyncSession, run: ReapRun) -> RunOut:
    steps = await _run_steps(session, run)
    planned = await _planned_candidates(session, run)

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
        confirmation_phrase=confirmation_phrase(planned) if planned else "REAP 0 ITEMS 0 GB",
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
                policy_hash=snapshot.policy_hash,
                approved_by="api",
                only_media_keys=only,
                max_unmeasured=(await active_profile_settings(session)).max_unmeasured_per_run,
            )
        except PlanError as exc:
            raise HTTPException(422, str(exc)) from exc

        out = await _run_out(session, run)
        await session.commit()
        return out


@router.get("/runs")
async def list_runs(request: Request, limit: int = 50) -> list[RunOut]:
    async with _sessions(request)() as session:
        runs = list(
            (
                await session.execute(
                    select(ReapRun).order_by(ReapRun.id.desc()).limit(min(limit, 200))
                )
            )
            .scalars()
            .all()
        )
        return [await _run_out(session, r) for r in runs]


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
        profile_settings = await active_profile_settings(session)
        executor = Executor(session, safety=safety, settings=profile_settings, dry_run=True)
        try:
            report = await executor.execute(run_id)
        except ExecutionError as exc:
            # A voided run (changed manifest, already executed) is a 409: the plan is no
            # longer valid, and the owner needs to re-plan rather than retry.
            raise HTTPException(409, str(exc)) from exc
        await session.commit()

    return _report_out(report)


@router.post("/runs/{run_id}/execute")
async def execute_run(request: Request, run_id: int, payload: ExecuteRunIn) -> RunReportOut:
    """Execute a real reap. **The one endpoint in Reaper that deletes.**

    Every gate must pass, and each resolves toward keeping the file:

    1. **Deletion must be enabled on the host** (403 otherwise). The transport guard would
       refuse the calls anyway; this is the earlier, clearer refusal.
    2. **The typed confirmation must match the plan's current phrase exactly** (409
       otherwise). The phrase is recomputed here from the plan, so a stale tab -- whose
       phrase was for a different plan -- cannot replay it.
    3. **The executor's own interlocks** -- manifest re-check, caps abort-not-truncate, the
       canary, the per-item streaming veto and played-since-approval check -- each run and
       can still spare or abort. A voided run comes back 409.

    The scheduler never calls this. A real reap is a deliberate act by a person who typed
    the phrase, not something a timer can trigger.
    """
    settings: Settings = request.app.state.settings
    box: SecretBox = request.app.state.secret_box
    factory = _sessions(request)

    async with factory() as session:
        safety = await app_settings.runtime_safety(session, settings)
        if not safety.destructive_allowed:
            raise HTTPException(403, safety.why_blocked() or "Deletion is turned off.")

        run = await session.get(ReapRun, run_id)
        if run is None:
            raise HTTPException(404, "No such run.")

        planned = await _planned_candidates(session, run)
        expected = confirmation_phrase(planned) if planned else "REAP 0 ITEMS 0 GB"
        if payload.confirmation_phrase.strip() != expected:
            raise HTTPException(
                409,
                # Plain interpolation, not repr: the operator sees the phrase exactly as it
                # must be typed, with no engineer-style quoting around it.
                f"That confirmation does not match this plan. Expected: {expected}. The "
                "plan may have changed since the page loaded. Reload, review, and confirm "
                "again.",
            )
        profile_settings = await active_profile_settings(session)

    # Build the live clients and run in a fresh session. The executor commits the journal
    # durably as it goes -- the EXECUTING claim before the first send, every step mark, and
    # the final run state -- so a crash mid-run leaves an accurate record of what was done;
    # the commit below is only a backstop for anything still pending. Every client is
    # closed on the way out, however the run ends.
    gateway, closers = await build_reap_gateway(factory, box, safety=safety)

    async def _armed_now() -> bool:
        # The executor's mid-run kill switch: a FRESH session per read, because the run's
        # own session caches rows across its per-item commits and would keep reporting
        # the switch as it stood when the run began.
        async with factory() as check_session:
            return await app_settings.destructive_enabled(check_session, settings)

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
        )
        try:
            report = await executor.execute(run_id)
        except ExecutionError as exc:
            # A refused run (disarmed, no clients, changed manifest, already executed).
            raise HTTPException(409, str(exc)) from exc
        await run_session.commit()

    log.info(
        "reap.executed",
        run_id=report.run_id,
        state=report.state.value,
        deleted_items=report.deleted_items,
        deleted_bytes=report.deleted_bytes,
        skipped=report.skipped,
    )
    return _report_out(report)


def _report_out(report: RunReport) -> RunReportOut:
    return RunReportOut(
        run_id=report.run_id,
        dry_run=report.dry_run,
        state=report.state.value,
        aborted_reason=report.aborted_reason,
        would_delete_items=report.deleted_items,
        deleted_bytes=report.deleted_bytes,
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


def _settings_out(settings: ProfileSettings) -> ProfileSettingsIO:
    return ProfileSettingsIO(
        max_items_per_run=settings.max_items_per_run,
        max_bytes_per_run=settings.max_bytes_per_run,
        max_items_per_30d=settings.max_items_per_30d,
        max_bytes_per_30d=settings.max_bytes_per_30d,
        grace_days=settings.grace_days,
        require_approval=settings.require_approval,
        max_unmeasured_per_run=settings.max_unmeasured_per_run,
    )


@router.get("/profile")
async def get_profile(request: Request) -> ProfileSettingsIO:
    """The caps and grace settings a run obeys. Built-in defaults until one is saved."""
    async with _sessions(request)() as session:
        return _settings_out(await active_profile_settings(session))


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
            grace_days=payload.grace_days,
            require_approval=payload.require_approval,
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
