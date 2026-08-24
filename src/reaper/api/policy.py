# SPDX-License-Identifier: AGPL-3.0-or-later
"""The policy editor's own routes: read it, save it, validate it, probe one signal.

Saving a policy changes what a LATER scan will condemn. It removes nothing by itself, and it
cannot: the one route that deletes is ``POST /api/runs/{id}/execute`` in ``api/runs.py``, and
it re-derives everything from the stored plan rather than from anything written here.

``_to_body`` and ``_candidate_media_type`` are read by ``api/simulate.py``, which asks what a
policy WOULD do without saving it. One conversion, so the simulated policy and the saved one
cannot drift.
"""

from __future__ import annotations

import json
from typing import assert_never

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.api import tags as api_tags
from reaper.api.deps import session_factory
from reaper.api.errors import refuse, validation_error_items
from reaper.api.schemas import (
    ConditionIn,
    GateSettingOut,
    PolicyBodyOut,
    PolicyIn,
    PolicyOut,
    PolicyProbeIn,
    PolicyProbeOut,
    PolicyValidateIn,
    PolicyWarningOut,
    RewatchOddsBlockOut,
    RewatchOddsFitOut,
    SignalSettingIn,
)
from reaper.clock import utcnow
from reaper.db.models import (
    Candidate,
    Instance,
    InstanceKind,
    Snapshot,
)
from reaper.db.models import Policy as PolicyModel
from reaper.engine.dormancy import history_reach_days
from reaper.engine.gates import wilson_upper
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    ConditionSpec,
    GateSetting,
    PolicyBody,
    ProfileSettings,
    SignalSetting,
)
from reaper.engine.policy_migrations import PolicyRepair
from reaper.engine.policy_warnings import inspect
from reaper.engine.preview import UnprobableSignalError, probe_signal
from reaper.engine.reason import to_wire
from reaper.engine.signals import SignalConfig
from reaper.services.history_sync import horizon
from reaper.services.profiles import active_policy, active_policy_row, active_profile_settings

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api")


def _to_body(payload: PolicyIn) -> PolicyBody:
    """Build the domain policy, translating its refusals into a 422.

    The wire schema deliberately does NOT re-implement the domain rules -- a vote floor
    of 0, a dormancy floor under 5 days, a run cap above the rolling cap. Those live in
    ``engine.policy``, where they are enforced for every caller including the CLI and
    the scheduler.

    But a domain ``ValidationError`` raised inside a route is a **500**, and the owner
    would see "Internal Server Error" instead of "a vote floor of 0 makes the rating
    floor meaningless -- it would protect an 8.3 drawn from 388 votes". So it is caught
    and re-raised with the reason intact.
    """
    try:
        return PolicyBody(
            media_type=payload.media_type,
            condemn_at=payload.condemn_at,
            coverage_floor_bp=payload.coverage_floor_bp,
            keep_last_seasons=payload.keep_last_seasons,
            keep_first_season=payload.keep_first_season,
            keep_last_scope=payload.keep_last_scope,
            season_lookahead=payload.season_lookahead,
            keep_in_progress=payload.keep_in_progress,
            in_progress_hold_days=payload.in_progress_hold_days,
            keep_specials=payload.keep_specials,
            protect_incomplete_seasons=payload.protect_incomplete_seasons,
            flag_keep_conflicts=payload.flag_keep_conflicts,
            gates=tuple(
                GateSetting(
                    gate=g.gate,
                    enabled=g.enabled,
                    threshold=g.threshold,
                    window_days=g.window_days,
                )
                for g in payload.gates
            ),
            signals=tuple(
                SignalSetting(
                    signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor
                )
                for s in payload.signals
            ),
            protect_conditions=tuple(
                ConditionSpec(field=c.field, op=c.op, value=c.value)
                for c in payload.protect_conditions
            ),
            # Already engine specs (BooleanCondemnSpec / GradedCondemnSpec) -- passed through.
            custom_condemn=tuple(payload.custom_condemn),
            graded_keeps=tuple(payload.graded_keeps),
            rewatch_keep_enabled=payload.rewatch_keep_enabled,
            rewatch_keep_discount=payload.rewatch_keep_discount,
            rewatch_min_viewings=payload.rewatch_min_viewings,
            rewatch_recent_days=payload.rewatch_recent_days,
            # Already engine specs (RatingRuleSpec) -- passed through, validated on the wire.
            keep_rating_rules=tuple(payload.keep_rating_rules),
            keep_rating_match=payload.keep_rating_match,
        )
    except ValidationError as exc:
        raise HTTPException(422, detail=validation_error_items(exc.errors())) from exc


async def _requests_app_configured(session: AsyncSession) -> bool:
    """Whether an enabled Seerr exists, which is what lets ``inspect`` say that a
    "requested only" keep-last scope has nothing to read and is quietly doing nothing."""
    row = (
        await session.execute(
            select(Instance.id).where(
                Instance.kind == InstanceKind.SEERR, Instance.enabled.is_(True)
            )
        )
    ).first()
    return row is not None


async def _history_reach_days(request: Request) -> float | None:
    """How far back the watch mirror goes, for ``policy_warnings.inspect``, or ``None`` if unknown.

    The second world-fact a policy cannot see about itself. It lets ``inspect`` say that a
    popularity window longer than the mirror blocks ``gates.ServerPopularityGate``
    library-wide, so the scan condemns nothing until the window comes down or history
    accrues.

    Derived through ``dormancy.history_reach_days`` off ``history_sync.horizon``, which is
    exactly how ``services.snapshot.ScanContext`` derives the reach the gate then reads
    (rule 104). The editor must not answer this question a second way, or it could advise
    against a window the scan is perfectly happy with.

    Reading it must never cost the operator their policy editor, so a mirror that will not
    answer resolves to ``None`` -- "could not tell", which ``inspect`` treats as silence.
    That is the safe direction here and only here: the warning gates nothing destructive, so
    the worst a miss can do is withhold advice, while a guess would tell an operator their
    window is useless when it is fine. A scan reading the same horizon degrades instead
    (``services.snapshot``, rule 28); this is not the scan pipeline.
    """
    try:
        earliest = await horizon(request.app.state.cache_engine)
    except (SQLAlchemyError, OSError, AttributeError):
        log.warning("policy.history_reach_unreadable", exc_info=True)
        return None
    return None if earliest is None else float(history_reach_days(earliest, now=utcnow()))


def _policy_out(
    body: PolicyBody,
    name: str,
    *,
    requests_app_configured: bool,
    settings: ProfileSettings,
    history_reach_days: float | None = None,
    repairs: tuple[PolicyRepair, ...] = (),
) -> PolicyOut:
    return PolicyOut(
        policy_hash=body.policy_hash(),
        name=name,
        history_reach_days=history_reach_days,
        # Off the shipped policy for this media type, never off the body being returned:
        # the whole point is to say what the operator's numbers were BEFORE they were theirs.
        default_signals=[
            SignalSettingIn(
                signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor
            )
            for s in (
                DEFAULT_TV_POLICY if body.media_type == "tv" else DEFAULT_MOVIE_POLICY
            ).signals
        ],
        repairs=list(repairs),
        # The SERVED body model, not the request one. Every other row rebuilt below is
        # loosened or matched by its wire model -- `SignalSettingIn` and `ConditionIn` ask
        # strictly less than `engine.policy`'s own `SignalSetting` and `ConditionSpec`, so a
        # body that loaded satisfies them -- and the gate row was the single place the wire
        # model asked for MORE than the engine did. It refuses a retired id the loader keeps
        # on purpose, which turned this rebuild into a 500 on the editor (#627).
        body=PolicyBodyOut(
            name=name,
            media_type=body.media_type,
            condemn_at=body.condemn_at,
            coverage_floor_bp=body.coverage_floor_bp,
            keep_last_seasons=body.keep_last_seasons,
            keep_first_season=body.keep_first_season,
            keep_last_scope=body.keep_last_scope,
            season_lookahead=body.season_lookahead,
            keep_in_progress=body.keep_in_progress,
            in_progress_hold_days=body.in_progress_hold_days,
            keep_specials=body.keep_specials,
            protect_incomplete_seasons=body.protect_incomplete_seasons,
            flag_keep_conflicts=body.flag_keep_conflicts,
            gates=[
                GateSettingOut(
                    gate=g.gate,
                    enabled=g.enabled,
                    threshold=g.threshold,
                    window_days=g.window_days,
                )
                for g in body.gates
            ],
            signals=[
                SignalSettingIn(
                    signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor
                )
                for s in body.signals
            ],
            protect_conditions=[
                ConditionIn(field=c.field, op=c.op, value=c.value) for c in body.protect_conditions
            ],
            custom_condemn=list(body.custom_condemn),
            graded_keeps=list(body.graded_keeps),
            rewatch_keep_enabled=body.rewatch_keep_enabled,
            rewatch_keep_discount=body.rewatch_keep_discount,
            rewatch_min_viewings=body.rewatch_min_viewings,
            rewatch_recent_days=body.rewatch_recent_days,
            keep_rating_rules=list(body.keep_rating_rules),
            keep_rating_match=body.keep_rating_match,
        ),
        warnings=[
            # Only draft warnings here. The LOAD-time repairs are their own field, not
            # warnings: the editor renders warnings from re-validating the DRAFT, so
            # anything attached to the GET response never reaches the page at all. That
            # was a real silent drop.
            PolicyWarningOut(field=w.field, reason=to_wire(w.reason), severity=w.severity)
            # The operator's SAVED settings, not the defaults. Passing ProfileSettings()
            # here made every settings-based warning unreachable: the caps and the
            # approval switch live on the profile, so inspecting a stand-in meant the
            # editor could never show a warning about any of them. A settings warning
            # therefore appears once the change is saved rather than as it is typed --
            # the savebar writes policy and profile together, so that is one click away.
            for w in inspect(
                body,
                settings,
                requests_app_configured=requests_app_configured,
                history_reach_days=history_reach_days,
            )
        ],
    )


def _candidate_media_type(policy_media_type: str) -> str:
    """The candidate ``media_type`` a policy governs: a TV policy scores *seasons*."""
    return "season" if policy_media_type == "tv" else "movie"


@router.get("/policy/rewatch-odds", tags=[api_tags.POLICY])
async def rewatch_odds_fit(
    request: Request,
    media_type: str = Query("movie", pattern="^(movie|tv)$"),
) -> RewatchOddsFitOut:
    """The latest scan's fitted rewatch ladder, for the Policy page's rungs and echo.

    Aggregated from the per-candidate ``rewatch_odds`` blocks the scan froze, never refit
    here (rule 104): the page states what the gate will actually compare, and the two cannot
    disagree. A row that is thin, has no usable block, or cannot be read at all contributes
    to ``total_items`` and no block -- the conservative display answer (rule 96); this route
    decides nothing about a reap. Covers both lanes: a movie policy scores movies, a TV
    policy scores seasons (``_candidate_media_type``).
    """
    blocks: dict[tuple[float, float | None], dict[str, int]] = {}
    total = 0
    async with session_factory(request)() as session:
        newest = (
            await session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1))
        ).scalar_one_or_none()
        if newest is None:
            return RewatchOddsFitOut(blocks=[], total_items=0)
        rows = (
            await session.execute(
                select(Candidate.explanation_json).where(
                    Candidate.snapshot_id == newest.id,
                    Candidate.media_type == _candidate_media_type(media_type),
                )
            )
        ).scalars()
        for blob in rows:
            total += 1
            try:
                context = json.loads(blob or "{}").get("rewatch_odds")
            except (ValueError, TypeError):
                context = None
            if not isinstance(context, dict) or context.get("state") in ("no_history", "thin"):
                continue
            try:
                key = (float(context["lo_days"]), context["hi_days"])
                n, k = int(context["n"]), int(context["k"])
            except (KeyError, TypeError, ValueError):
                continue
            entry = blocks.setdefault(key, {"n": n, "k": k, "items": 0})
            entry["items"] += 1
    return RewatchOddsFitOut(
        blocks=[
            RewatchOddsBlockOut(
                lo_days=lo,
                hi_days=hi,
                n=entry["n"],
                k=entry["k"],
                upper_bound_pct=round(wilson_upper(entry["k"], entry["n"]) * 100, 1),
                items=entry["items"],
            )
            for (lo, hi), entry in sorted(blocks.items(), key=lambda pair: pair[0][0])
        ],
        total_items=total,
    )


@router.get("/policy", tags=[api_tags.POLICY])
async def get_policy(request: Request, media_type: str = "movie") -> PolicyOut:
    """Load the active policy for a media type, so the editor opens on what is in force.

    A stored body that no longer validates must never raise here. ``active_policy``
    re-parses stored JSON through ``PolicyBody``, so any rule tightened after that row was
    written turns this route into a 500 and locks the operator out of the one page that
    fixes it. Two recoveries, in order:

    1. **Rescale.** A body written before removal weights had to total 100 is repaired by
       ``policy_migrations.rebalance``, which keeps the operator's tuning. The exact rescale cannot
       move a score, but integer rounding can, by more than a point (see that function's
       docstring for the worked cases) -- which is precisely why it comes back as an
       *unsaved draft*: the operator's own tuning, in the new units, with nothing written
       until they look at it and press Save. Their approvals stay valid until they do.
    2. **Fall back.** Anything we cannot repair opens on the shipped default, saying so,
       so nobody mistakes it for what is in force.

    A third recovery runs on a body that loads perfectly: a rating bar written before the
    bar moved off the gate row is restored (``policy_migrations.recover_rating_rules``),
    because that body loads cleanly while keeping nothing. It comes back as an unsaved draft too.
    """
    async with session_factory(request)() as session:
        active = await active_policy(session, media_type)
        body, name = active.body, active.name
        has_requests_app = await _requests_app_configured(session)
        settings = await active_profile_settings(session)
    return _policy_out(
        body,
        name,
        requests_app_configured=has_requests_app,
        settings=settings,
        history_reach_days=await _history_reach_days(request),
        # The repairs read very differently to an operator -- "your policy, in new units"
        # versus "your policy is gone" versus "a protection was put back" -- so each is its
        # own `PolicyRepair`, never inferred from the name (an operator's own policy is
        # often called "default") and never collapsed into one "needs saving" boolean.
        repairs=active.repairs,
    )


@router.post("/policy", tags=[api_tags.POLICY])
async def save_policy(request: Request, payload: PolicyIn) -> PolicyOut:
    """Save a policy. **Append-only: this never updates a row.**

    Re-saving the policy already in force is a no-op rather than a duplicate -- the hash
    is the identity, so an owner who opens the editor and saves without changing anything
    does not fork the audit trail. Only the *active* row may short-circuit like that:
    content matching an older, superseded row still appends a fresh row, because "in
    force" means "newest row for the media type". Skipping that write is how a revert
    used to vanish -- 200, reverted body in the response, old policy still active.

    Note what this does *not* do: it does not arm anything. Reaper still cannot delete,
    and a saved policy takes effect on the next scan.
    """
    body = _to_body(payload)
    policy_hash = body.policy_hash()
    reach_days = await _history_reach_days(request)

    async with session_factory(request)() as session:
        active = await active_policy_row(session, body.media_type)
        has_requests_app = await _requests_app_configured(session)
        settings = await active_profile_settings(session)

        if active is not None and active.policy_hash == policy_hash:
            # Content-identical to the policy in force: nothing is written and the name
            # is NOT changed. Echo the *persisted* name, not the discarded request name,
            # so the success response matches what the next GET /api/policy will show --
            # otherwise a name-only edit looks like it stuck when it silently did not.
            return _policy_out(
                body,
                active.name,
                requests_app_configured=has_requests_app,
                settings=settings,
                history_reach_days=reach_days,
            )

        session.add(
            PolicyModel(
                policy_hash=policy_hash,
                body_json=body.model_dump_json(),
                media_type=body.media_type,
                name=payload.name,
                created_at=utcnow(),
            )
        )
        await session.commit()

    return _policy_out(
        body,
        payload.name,
        requests_app_configured=has_requests_app,
        settings=settings,
        history_reach_days=reach_days,
    )


@router.post("/policy/probe", tags=[api_tags.POLICY])
async def probe_policy(payload: PolicyProbeIn) -> PolicyProbeOut:
    """Try one policy rule against one value, and answer from the engine.

    The editor asks this while the operator drags a signal's probe. It exists so that number
    is not computed in the browser: a second copy of the ramp beside the control that tunes
    deletions would be free to drift from the scorer, and would read as authoritative while
    doing it (rule 3/22). The answer here comes from the same ``evaluate_signal`` a scan
    runs.

    Stateless and read-only. It touches no snapshot and no session, which is why it is fast
    enough to sit under a slider, and why it can say nothing about the operator's library --
    it describes the RULE, not any item. ``engine.preview`` carries what that costs.

    A second probe kind is a member on ``PolicyProbeIn`` and an arm below; see
    ``engine.preview``.
    """
    # The engine's own settings model, so a probe refuses what a save refuses -- including
    # `floor < saturate_at`, which lives there and nowhere else (rule 131/104).
    try:
        setting = SignalSetting(
            signal=payload.signal,
            weight=payload.weight,
            saturate_at=payload.saturate_at,
            floor=payload.floor,
        )
    except ValidationError:
        refuse(422, "error.policy.probe_range_invalid")

    match payload.kind:
        case "signal":
            try:
                answer = probe_signal(
                    SignalConfig(
                        signal=setting.signal,
                        weight=setting.weight,
                        saturate_at=setting.saturate_at,
                        floor=setting.floor,
                    ),
                    payload.value,
                )
            except UnprobableSignalError:
                refuse(422, "error.policy.probe_unprobable")
        case _ as unreachable:
            assert_never(unreachable)
    return PolicyProbeOut(points=answer.points)


@router.post("/policy/validate", tags=[api_tags.POLICY])
async def validate_policy(request: Request, payload: PolicyValidateIn) -> PolicyOut:
    """Validate, hash, and inspect.

    Validation refuses what is *provably* wrong. ``inspect`` warns about what is merely
    *probably* wrong -- and no validator can tell those apart, because the values are
    legal either way. The archetype: an IMDb floor of 96 is a legal 9.6, and is
    indistinguishable from a Rotten Tomatoes 96 typed into the wrong box.

    This is the route the editor calls as you type, so it is where the warnings are
    actually read. It takes a session for one reason: one warning is about the world
    outside the policy (a "requested only" scope with no Seerr to read), and a policy
    cannot see that from its own fields.
    """
    async with session_factory(request)() as session:
        has_requests_app = await _requests_app_configured(session)
        settings = await active_profile_settings(session)
    if payload.draft_max_unmeasured_per_run is not None:
        # The editor's unknown-size box is the one control whose warning renders beneath it
        # while showing an unsaved value, so the check runs against what is on screen rather
        # than what is stored (see PolicyValidateIn). Bounds are enforced on the wire by the
        # field itself, so this cannot widen the allowance past what a save would accept.
        settings = settings.model_copy(
            update={"max_unmeasured_per_run": payload.draft_max_unmeasured_per_run}
        )
    return _policy_out(
        _to_body(payload),
        payload.name,
        requests_app_configured=has_requests_app,
        settings=settings,
        history_reach_days=await _history_reach_days(request),
    )
