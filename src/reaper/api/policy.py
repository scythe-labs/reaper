# SPDX-License-Identifier: AGPL-3.0-or-later
"""The policy editor's own routes. Read it, save it, validate it, probe one signal.

Saving a policy changes what a later scan will condemn. It removes nothing by
itself, and it cannot. The one route that deletes is
``POST /api/runs/{id}/execute`` in ``api/runs.py``, and it re-derives
everything from the stored plan rather than from anything written here.

``_to_body`` and ``_candidate_media_type`` are read by ``api/simulate.py``,
which asks what a policy would do without saving it. This is one conversion,
so the simulated policy and the saved one cannot drift.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
from reaper.api.review import _decode_explanation
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
    ThresholdCurveCountsOnlyOut,
    ThresholdCurveCountsOnlyRowOut,
    ThresholdCurveMeasuredOut,
    ThresholdCurveMeasuredRowOut,
    ThresholdCurveNoScanOut,
    ThresholdCurveOut,
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
from reaper.engine.signals import MAX_SCORE, SignalConfig
from reaper.engine.verdict import decide_verdict
from reaper.services.condemned import has_blocked_protections_decoded
from reaper.services.history_sync import horizon
from reaper.services.profiles import active_policy, active_policy_row, active_profile_settings

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api")


def _to_body(payload: PolicyIn) -> PolicyBody:
    """Build the domain policy, translating its refusals into a 422.

    The wire schema never re-implements the domain rules, such as a vote floor
    of 0, a dormancy floor under 5 days, or a run cap above the rolling cap.
    Those live in ``engine.policy``, where they are enforced for every
    caller, including the CLI and the scheduler.

    A domain ``ValidationError`` raised inside a route would otherwise be a
    500, and the owner would see "Internal Server Error" instead of "a vote
    floor of 0 makes the rating floor meaningless, it would protect an 8.3
    drawn from 388 votes". So this catches it and re-raises with the reason
    intact.
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
            # Already engine specs (BooleanCondemnSpec / GradedCondemnSpec). Passed through.
            custom_condemn=tuple(payload.custom_condemn),
            graded_keeps=tuple(payload.graded_keeps),
            rewatch_keep_enabled=payload.rewatch_keep_enabled,
            rewatch_keep_discount=payload.rewatch_keep_discount,
            rewatch_min_viewings=payload.rewatch_min_viewings,
            rewatch_recent_days=payload.rewatch_recent_days,
            # Already engine specs (RatingRuleSpec). Passed through, validated on the wire.
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
    """Return how far back the watch mirror goes, for ``policy_warnings.inspect``,
    or ``None`` if unknown.

    This is the second world-fact a policy cannot see about itself. It lets
    ``inspect`` say that a popularity window longer than the mirror blocks
    ``gates.ServerPopularityGate`` library-wide, so the scan condemns nothing
    until the window comes down or history accrues.

    This derives the value through ``dormancy.history_reach_days`` off
    ``history_sync.horizon``, exactly how ``services.snapshot.ScanContext``
    derives the reach the gate then reads. The editor must answer this
    question the same way the scan does, or it could advise against a window
    the scan is perfectly happy with.

    Reading it must never cost the operator their policy editor, so a mirror
    that will not answer resolves to ``None``, meaning "could not tell",
    which ``inspect`` treats as silence. That is the safe direction here, and
    only here. The warning gates nothing destructive, so the worst a miss can
    do is withhold advice, while a guess would tell an operator their window
    is useless when it is fine. A scan reading the same horizon degrades
    instead (``services.snapshot``). This is not the scan pipeline.
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
        # Off the shipped policy for this media type, never off the body being
        # returned. The whole point is to say what the operator's numbers were
        # before they were theirs.
        default_signals=[
            SignalSettingIn(
                signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor
            )
            for s in (
                DEFAULT_TV_POLICY if body.media_type == "tv" else DEFAULT_MOVIE_POLICY
            ).signals
        ],
        repairs=list(repairs),
        # The served body model, not the request one. Every other row rebuilt
        # below is loosened or matched by its wire model. `SignalSettingIn`
        # and `ConditionIn` ask for strictly less than `engine.policy`'s own
        # `SignalSetting` and `ConditionSpec`, so a body that loaded already
        # satisfies them. The gate row is the one place a stricter wire model
        # would break this: the wire model must not refuse a retired gate id
        # the loader keeps on purpose, or rebuilding this response would 500
        # on the editor.
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
            # Only draft warnings here. The load-time repairs are their own
            # field, not warnings. The editor renders warnings from
            # re-validating the draft, so anything attached to the GET
            # response directly would never reach the page at all.
            PolicyWarningOut(field=w.field, reason=to_wire(w.reason), severity=w.severity)
            # The operator's saved settings, not the defaults. Passing a bare
            # `ProfileSettings()` here would make every settings-based
            # warning unreachable, since the caps and the approval switch
            # live on the profile, so inspecting a stand-in could never show
            # a warning about any of them. A settings warning therefore only
            # appears once the change is saved, not while it is being typed.
            # The savebar writes policy and profile together, so that is one
            # click away.
            for w in inspect(
                body,
                settings,
                requests_app_configured=requests_app_configured,
                history_reach_days=history_reach_days,
            )
        ],
    )


def _candidate_media_type(policy_media_type: str) -> str:
    """Return the candidate ``media_type`` a policy governs. A TV policy scores seasons."""
    return "season" if policy_media_type == "tv" else "movie"


@router.get("/policy/rewatch-odds", tags=[api_tags.POLICY])
async def rewatch_odds_fit(
    request: Request,
    media_type: str = Query("movie", pattern="^(movie|tv)$"),
) -> RewatchOddsFitOut:
    """Return the latest scan's fitted rewatch ladder, for the Policy page's
    rungs and echo.

    This aggregates from the per-candidate ``rewatch_odds`` blocks the scan
    froze, and never refits here, so the page states what the gate will
    actually compare and the two cannot disagree. A row that is thin, has no
    usable block, or cannot be read at all contributes to ``total_items`` and
    no block, the conservative display answer. This route decides nothing
    about a reap. It covers both lanes: a movie policy scores movies, a TV
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


@dataclass(frozen=True, slots=True)
class RatioCandidate:
    """One stored candidate's inputs to the threshold curve, decoded once from
    its frozen verdict, score, coverage, and explanation. This is everything
    :func:`threshold_curve_rows` needs, with no JSON or session left in it.
    """

    protected: bool
    """A protection fired, whatever threshold is tried. Mirrors
    ``Candidate.verdict == "protect"``. ``decide_verdict`` checks PROTECT
    before the threshold, so the stored verdict already answers this
    regardless of what ``condemn_at`` produced it."""

    blocked: bool
    """A protection could not be checked, whatever threshold is tried
    (``condemned.has_blocked_protections_decoded``). A stored ``verdict`` of
    "condemn" or "protect" already proves this False, since decide_verdict
    abstains on a blocked row before ever reading the score. This still reads
    the same way for every row, since the explanation is already decoded
    once for ``rewatch_odds`` below."""

    score: int
    coverage_bp: int

    rewatch_odds: Mapping[str, object] | None
    """This item's stored Stage 2 cohort context
    (``services.snapshot._rewatch_odds_context``), or ``None`` when the row carries none at
    all (an explanation this build cannot read, or one frozen before the field existed)."""


#: The legal ``condemn_at`` domain (``engine.policy.PolicyBody.condemn_at``,
#: ``ge=1, le=100``, never 0, which would condemn everything the gates do not
#: save). The curve is built over every score in it rather than a narrower
#: band, so a title scoring high enough to flag only at the very top of the
#: range is never missed because it sat outside a shortcut range.
_CONDEMN_AT_DOMAIN: range = range(1, MAX_SCORE + 1)


def _cohort(rewatch_odds: object) -> tuple[int, int, str] | None:
    """Return one candidate's stored rewatch-odds cohort as ``(n, k, state)``,
    or ``None`` when there is nothing usable to read. That covers not a
    stored block at all, an ``n`` that is not a positive int (the
    ``"no_history"`` placeholder stores ``n=0``), or a ``k`` that is not an
    int. ``bool`` is rejected explicitly, since JSON's ``true``/``false``
    satisfy ``isinstance(_, int)`` and would otherwise read as a cohort of
    size 1 or 0.
    """
    if not isinstance(rewatch_odds, Mapping):
        return None
    n, k, state = rewatch_odds.get("n"), rewatch_odds.get("k"), rewatch_odds.get("state")
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        return None
    if isinstance(k, bool) or not isinstance(k, int):
        return None
    return (n, k, state) if isinstance(state, str) else None


def _measured_or_thin_rate(rewatch_odds: object) -> float | None:
    """Return this candidate's own comeback rate, read straight from its
    cohort. This is the Wilson 95% upper bound of ``k``/``n``
    (``gates.wilson_upper``) for both ``"measured"`` and ``"thin"`` states,
    the same bound the hold gate compares and the Policy page displays,
    never the point rate. The bound is strictly positive for any finite
    cohort, which matters here: a measured cohort with zero comebacks read
    as a plain ``0.0`` would poison :func:`threshold_curve_rows`'s ``max()``
    fallback, zero the expected mistakes, and let every row on the curve
    understate its risk.

    This returns ``None`` for anything else, including ``"no_history"``.
    :func:`threshold_curve_rows` falls back to the worst rate this scan
    measured anywhere else instead of reading that as a bare zero, since
    missing data must never make deletion look safer than it is.
    """
    cohort = _cohort(rewatch_odds)
    if cohort is None:
        return None
    n, k, state = cohort
    if state in ("measured", "thin"):
        return wilson_upper(k, n)
    return None


def _mistake_probability(rewatch_odds: object, *, fallback: float) -> float:
    """Return one flagged candidate's contribution to expected mistakes. This
    is its own measured or Wilson-bounded rate, or, when that is missing,
    past the fitted range, or a state this build does not recognize,
    ``fallback``, the worst rate this scan measured anywhere."""
    rate = _measured_or_thin_rate(rewatch_odds)
    return fallback if rate is None else rate


def threshold_curve_rows(
    candidates: Sequence[RatioCandidate], *, coverage_floor_bp: int
) -> ThresholdCurveMeasuredOut | ThresholdCurveCountsOnlyOut:
    """Compute the whole score-to-consequence curve behind the delete-threshold
    slider, over already-decoded rows from the newest scan. For every legal
    ``condemn_at`` (:data:`_CONDEMN_AT_DOMAIN`), this says how many titles the
    scan would flag and, where this scan measured a trusted rewatch cohort
    anywhere, about how many of them its own history says come back. It
    computes this once for every score, so the editor can answer any slider
    position locally with zero requests while dragging.

    This re-decides each candidate through the one decision function
    (``engine.verdict.decide_verdict``, never a hand-rolled
    ``score >= threshold``) and sums the flagged titles' own dormancy
    cohorts' comeback probability (:func:`_mistake_probability`).

    A row is included only where ``flagged > 0``. A threshold flagging
    nothing has no consequence to state, and the frontend already renders
    the "nothing scores this high" sentence for any score above the last
    included row. Since ``decide_verdict`` is monotone in score, flagged is
    non-increasing across the domain, so the rows form one leading run and
    the highest-scoring row is the curve's peak.

    This returns ``counts_only`` when no candidate anywhere in this scan has
    a cohort this server trusts. The count is real, but making up a comeback
    estimate with nothing to base it on would be worse than not showing one.
    An empty candidate set, from no scan or nothing on this media type's
    lane, has no population to ever flag anything, so it reads the same way,
    with an empty row list. Every score then falls through to "nothing on
    the last scan scores this high", which is true of it. This gives the
    same answer every time for the same stored scan and history, since it
    uses no clock and no randomness.
    """
    if not candidates:
        return ThresholdCurveCountsOnlyOut(rows=[])

    fallback_pool = [
        rate for c in candidates if (rate := _measured_or_thin_rate(c.rewatch_odds)) is not None
    ]
    if not fallback_pool:
        # No candidate anywhere in this scan has a cohort this server trusts.
        # The fit never found a band with REWATCH_BLOCK_FLOOR_N (30) or more
        # titles. Nothing below would have a real number to compare against,
        # so the count stands alone.
        counted_rows: list[ThresholdCurveCountsOnlyRowOut] = []
        for threshold in _CONDEMN_AT_DOMAIN:
            flagged = sum(
                1
                for c in candidates
                if decide_verdict(
                    protected=c.protected,
                    blocked=c.blocked,
                    score=c.score,
                    coverage_bp=c.coverage_bp,
                    condemn_at=threshold,
                    coverage_floor_bp=coverage_floor_bp,
                )
                == "condemn"
            )
            if flagged:
                counted_rows.append(
                    ThresholdCurveCountsOnlyRowOut(score=threshold, flagged=flagged)
                )
        return ThresholdCurveCountsOnlyOut(rows=counted_rows)
    fallback_probability = max(fallback_pool)

    weighted = [
        (
            c.protected,
            c.blocked,
            c.score,
            c.coverage_bp,
            _mistake_probability(c.rewatch_odds, fallback=fallback_probability),
        )
        for c in candidates
    ]

    measured_rows: list[ThresholdCurveMeasuredRowOut] = []
    for threshold in _CONDEMN_AT_DOMAIN:
        flagged = 0
        mistakes = 0.0
        for protected, blocked, score, coverage_bp, probability in weighted:
            verdict = decide_verdict(
                protected=protected,
                blocked=blocked,
                score=score,
                coverage_bp=coverage_bp,
                condemn_at=threshold,
                coverage_floor_bp=coverage_floor_bp,
            )
            if verdict == "condemn":
                flagged += 1
                mistakes += probability
        if flagged == 0:
            continue
        # mistakes > 0 is guaranteed here. Every contributing probability is
        # strictly positive (`_measured_or_thin_rate`'s own guarantee), and at
        # least one flagged title contributes one, so `math.ceil` is always
        # at least 1. This is pinned in the tests.
        measured_rows.append(
            ThresholdCurveMeasuredRowOut(
                score=threshold, flagged=flagged, expected_mistakes=math.ceil(mistakes)
            )
        )
    return ThresholdCurveMeasuredOut(rows=measured_rows)


async def _ratio_candidates(
    session: AsyncSession, *, snapshot_id: int, candidate_media_type: str
) -> list[RatioCandidate]:
    """Return every candidate of one snapshot on one lane, decoded once into
    what :func:`threshold_curve_rows` needs. This mirrors ``rewatch_odds_fit``'s
    own read of the same rows, in one query, with no second trip per candidate.
    """
    rows = (
        await session.execute(
            select(
                Candidate.verdict,
                Candidate.score,
                Candidate.coverage_bp,
                Candidate.explanation_json,
            ).where(
                Candidate.snapshot_id == snapshot_id,
                Candidate.media_type == candidate_media_type,
            )
        )
    ).all()
    out: list[RatioCandidate] = []
    for verdict, score, coverage_bp, explanation_json in rows:
        exp = _decode_explanation(explanation_json)
        out.append(
            RatioCandidate(
                protected=verdict == "protect",
                blocked=has_blocked_protections_decoded(exp),
                score=int(score),
                coverage_bp=int(coverage_bp),
                rewatch_odds=exp.get("rewatch_odds") if isinstance(exp, dict) else None,
            )
        )
    return out


@router.get("/policy/threshold-curve", tags=[api_tags.POLICY])
async def threshold_curve(
    request: Request,
    media_type: str = Query("movie", pattern="^(movie|tv)$"),
) -> ThresholdCurveOut:
    """Return the whole per-score curve behind the delete-threshold slider's
    consequence sentence, from the newest scan and this server's own fitted
    rewatch curve.

    Read-only. Nothing here saves anything, and nothing here is a setting.
    The operator still sets ``condemn_at`` on the slider exactly as before,
    and this route only states what that position means. It covers both
    lanes: a movie policy scores movies, a TV policy scores seasons
    (``_candidate_media_type``), the same split ``rewatch_odds_fit`` reads.

    This is one request per media type, not one per slider position. The
    frontend re-decides every row locally as the operator drags, since
    :func:`threshold_curve_rows` already computed the whole domain. This
    returns ``no_scan`` when there is nothing to read yet. The frontend
    renders nothing in that state, and in a failed or still-loading read,
    instead of a locked or error notice, since a plain score control needs
    no scan to work.
    """
    async with session_factory(request)() as session:
        newest = (
            await session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1))
        ).scalar_one_or_none()
        if newest is None:
            return ThresholdCurveNoScanOut()
        candidates = await _ratio_candidates(
            session,
            snapshot_id=newest.id,
            candidate_media_type=_candidate_media_type(media_type),
        )
        active = await active_policy(session, media_type)
    return threshold_curve_rows(candidates, coverage_floor_bp=active.body.coverage_floor_bp)


@router.get("/policy", tags=[api_tags.POLICY])
async def get_policy(request: Request, media_type: str = "movie") -> PolicyOut:
    """Load the active policy for a media type, so the editor opens on what is in force.

    A stored body that no longer validates must never raise here.
    ``active_policy`` re-parses stored JSON through ``PolicyBody``, so any
    rule tightened after that row was written would otherwise turn this
    route into a 500 and lock the operator out of the one page that fixes
    it. Two recoveries run, in order.

    1. **Rescale.** A body written before removal weights had to total 100
       is repaired by ``policy_migrations.rebalance``, which keeps the
       operator's tuning. The exact rescale cannot move a score, but integer
       rounding can, by more than a point (see that function's docstring for
       the worked cases). That is why it comes back as an unsaved draft: the
       operator's own tuning, in the new units, with nothing written until
       they look at it and press Save. Their approvals stay valid until they
       do.
    2. **Fall back.** Anything this cannot repair opens on the shipped
       default, saying so, so nobody mistakes it for what is in force.

    A third recovery runs on a body that loads perfectly. A rating bar
    written before the bar moved off the gate row is restored
    (``policy_migrations.recover_rating_rules``), because that body loads
    cleanly while keeping nothing. It comes back as an unsaved draft too.
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
        # The repairs read very differently to an operator. "Your policy, in
        # new units" is not "your policy is gone", and neither is "a
        # protection was put back". So each is its own `PolicyRepair`, never
        # inferred from the name (an operator's own policy is often called
        # "default") and never collapsed into one "needs saving" boolean.
        repairs=active.repairs,
    )


@router.post("/policy", tags=[api_tags.POLICY])
async def save_policy(request: Request, payload: PolicyIn) -> PolicyOut:
    """Save a policy. This is append-only: it never updates a row.

    Re-saving the policy already in force is a no-op rather than a
    duplicate. The hash is the identity, so an owner who opens the editor
    and saves without changing anything does not fork the audit trail. Only
    the active row may short-circuit like that. Content matching an older,
    superseded row still appends a fresh row, because "in force" means
    "newest row for the media type". Skipping that write for a superseded
    row would silently keep the old policy active while returning 200 with
    the reverted body, as if the revert had taken effect.

    This does not arm anything. Reaper still cannot delete, and a saved
    policy takes effect on the next scan.
    """
    body = _to_body(payload)
    policy_hash = body.policy_hash()
    reach_days = await _history_reach_days(request)

    async with session_factory(request)() as session:
        active = await active_policy_row(session, body.media_type)
        has_requests_app = await _requests_app_configured(session)
        settings = await active_profile_settings(session)

        if active is not None and active.policy_hash == policy_hash:
            # Content-identical to the policy in force. Nothing is written,
            # and the name is not changed. This echoes the persisted name,
            # not the discarded request name, so the success response
            # matches what the next GET /api/policy will show. Otherwise a
            # name-only edit would look like it stuck when it silently did
            # not.
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

    The editor asks this while the operator drags a signal's probe. This
    exists so that number is not computed in the browser. A second copy of
    the ramp beside the control that tunes deletions would be free to drift
    from the scorer, and would read as authoritative while doing it. The
    answer here comes from the same ``evaluate_signal`` a scan runs.

    Stateless and read-only. It touches no snapshot and no session, which is
    why it is fast enough to sit under a slider, and why it can say nothing
    about the operator's library. It describes the rule, not any item.
    ``engine.preview`` carries what that costs.

    A second probe kind is a member on ``PolicyProbeIn`` and an arm below.
    See ``engine.preview``.
    """
    # The engine's own settings model, so a probe refuses what a save
    # refuses. This includes `floor < saturate_at`, which lives there and
    # nowhere else.
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

    Validation refuses what is provably wrong. ``inspect`` warns about what
    is merely probably wrong, and no validator can tell those apart, because
    the values are legal either way. For example, an IMDb floor of 96 is a
    legal 9.6, indistinguishable from a Rotten Tomatoes 96 typed into the
    wrong box.

    This is the route the editor calls as the operator types, so it is
    where the warnings are actually read. It takes a session for one
    reason: one warning is about the world outside the policy, such as a
    "requested only" scope with no Seerr to read, and a policy cannot see
    that from its own fields.
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
