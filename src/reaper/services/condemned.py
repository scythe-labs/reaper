# SPDX-License-Identifier: AGPL-3.0-or-later
"""The effective condemned set: what a reap may actually act on, right now.

A snapshot is frozen evidence, but the owner keeps deciding after it is taken: a hand
**spare** removes an item from everything destructive, and a hand **reap** adds one --
the owner looked and decided -- unless a hard safety stop still holds. Before this
module, only the spare half was applied post-snapshot; a hand reap silently waited for
the next scan while the queue showed no change and the plan excluded it.

This is the ONE place that assembles "condemned, as of this moment":

    scan-condemned rows  -  hand-spares  +  hand-reaps that decide_verdict honors

Everything that acts or counts imports it -- grace (and through it the Leaving Soon
shelf), the planner's step expansion, the confirmation-phrase count, the executor's
per-item keep-set, and the per-show numbers the queue shows beside destructive buttons
-- so the number beside a button is derived from the exact set the server will act on.

The decision itself is NOT here. ``decide_verdict`` is the single decision function;
:func:`reap_override_verdict` only plumbs a frozen row's stored facts into it. Anything
unreadable in those facts reads as *blocked*, which decide_verdict resolves toward
keeping the file.
"""

from __future__ import annotations

import json

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.db.models import Candidate
from reaper.engine.verdict import STRUCTURAL_GATES, block_holds_reap, decide_verdict
from reaper.services import whitelist


def reap_override_verdict(explanation_json: str, *, score: int) -> str:
    """What a hand reap does to one stored row: ``"condemn"`` or ``"protect"``.

    Inputs are derived from the frozen explanation -- which protections fired, whether
    any could not be checked, whether the Plex match was clean -- and handed to
    ``decide_verdict`` with ``override="reap"``. That branch consults only
    ``blocked_holds_reap`` and ``safety_protected`` (a structural gate: streaming right
    now, or a file no *arr manages); the score, coverage and thresholds are never read on
    it, so the zeros below are inert plumbing, pinned by a test, not hidden policy.

    A block that could not be *checked* holds the reap (fail-closed); a block that is a
    deliberate "the owner should decide" flag -- the keep-rule conflict -- does not, since
    the reap IS that decision (:func:`reaper.engine.verdict.block_holds_reap`). A malformed
    or unreadable explanation reads as blocked: not being able to see why an item was kept
    is not permission to remove it.
    """
    try:
        exp = json.loads(explanation_json)
        if not isinstance(exp, dict):
            raise ValueError("explanation is not an object")
    except (ValueError, TypeError):
        return decide_verdict(
            protected=False,
            blocked=True,
            score=score,
            coverage_bp=0,
            condemn_at=0,
            coverage_floor_bp=0,
            override="reap",
            safety_protected=True,
        )

    fired = [e for e in exp.get("protections_fired") or [] if isinstance(e, dict)]
    unknown = [e for e in exp.get("protections_unknown") or [] if isinstance(e, dict)]
    match_status = (exp.get("match") or {}).get("status")
    bad_match = match_status in ("unmatched", "ambiguous")

    return decide_verdict(
        protected=bool(fired),
        blocked=bool(unknown) or bad_match,
        # A block holds the reap unless it is a deliberate "you decide" deferral (the
        # keep-rule conflict); a bad Plex match always holds. Read off the same primitive
        # the scan uses, so a stored explanation and a live evaluation agree.
        blocked_holds_reap=bad_match
        or any(
            block_holds_reap(str(e.get("gate") or ""), str(e.get("detail") or "")) for e in unknown
        ),
        score=score,
        coverage_bp=0,
        condemn_at=0,
        coverage_floor_bp=0,
        override="reap",
        safety_protected=any(str(e.get("gate")) in STRUCTURAL_GATES for e in fired),
    )


def reap_is_effective(candidate: Candidate) -> bool:
    """Whether a hand reap on this frozen row actually condemns it."""
    if candidate.verdict == "condemn":
        return True
    return (
        reap_override_verdict(candidate.explanation_json, score=int(candidate.score)) == "condemn"
    )


def effective_verdict(candidate: Candidate, decisions: dict[str, str]) -> str:
    """The lane an item lands in once its hand override is applied.

    The backend twin of the frontend's ``handFate``, collapsed to the three stored lanes: a
    hand spare -- and a hand reap the engine will not honor yet -- keep the item (``"protect"``);
    a honored hand reap condemns it (``"condemn"``); with no override the pure-policy verdict
    stands. This is the ONE place a stored (pure-policy) verdict is turned into an effective
    lane, so the review-queue tab filter and the scan-summary counts route through it and can
    never disagree with :func:`effective_condemned`.
    """
    override = whitelist.effective_override(candidate.media_key, decisions)
    if override == "spare":
        return "protect"
    if override == "reap":
        return "condemn" if reap_is_effective(candidate) else "protect"
    return candidate.verdict


async def _reap_overridden_rows(
    session: AsyncSession, snapshot_id: int, reap_keys: list[str]
) -> list[Candidate]:
    """The not-already-condemned rows a reap decision could cover.

    A reap decision names an item (a movie's or a season's media_key) or a whole show (the
    seasons' ``group_key``). Shared by :func:`effective_condemned` and :func:`held_reaps` so
    the two never disagree on which rows a reap even touches.
    """
    if not reap_keys:
        return []
    return list(
        (
            await session.execute(
                select(Candidate).where(
                    Candidate.snapshot_id == snapshot_id,
                    Candidate.verdict != "condemn",
                    or_(
                        Candidate.media_key.in_(reap_keys),
                        Candidate.group_key.in_(reap_keys),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )


async def effective_condemned(
    session: AsyncSession, snapshot_id: int, decisions: dict[str, str]
) -> dict[str, Candidate]:
    """Every candidate in this snapshot a reap may act on, keyed by ``media_key``.

    ``decisions`` is ``whitelist.overrides(session)``, passed in so one read serves the
    caller's whole request. Spares win over everything (a per-season spare beats a
    show-level reap, exactly as ``effective_override`` resolves it); a hand reap adds a
    row only when :func:`reap_override_verdict` condemns it.
    """
    condemned = (
        (
            await session.execute(
                select(Candidate).where(
                    Candidate.snapshot_id == snapshot_id, Candidate.verdict == "condemn"
                )
            )
        )
        .scalars()
        .all()
    )
    out = {
        c.media_key: c
        for c in condemned
        if whitelist.effective_override(c.media_key, decisions) != "spare"
    }

    reap_keys = sorted(k for k, d in decisions.items() if d == "reap")
    for c in await _reap_overridden_rows(session, snapshot_id, reap_keys):
        # A season spared back out of a hand-reaped show resolves to "spare" here.
        if whitelist.effective_override(c.media_key, decisions) != "reap":
            continue
        if reap_is_effective(c):
            out[c.media_key] = c
    return out


async def overridden_lane_shifts(
    session: AsyncSession, snapshot_id: int, decisions: dict[str, str]
) -> list[tuple[Candidate, str, str]]:
    """Every candidate a hand override moves to a lane other than its pure-policy verdict.

    Only an overridden row can change lanes, so only overridden rows are read here -- the raw
    ``verdict == lane`` query stays the cheap indexed path, and a caller splices these few moves
    onto it (the tab filter) or applies them as count deltas (the scan summary). Returns
    ``(candidate, from_lane, to_lane)`` for each row whose :func:`effective_verdict` differs from
    its stored verdict. A show-level override reaches its seasons by ``group_key``; a per-item
    override by ``media_key``.
    """
    if not decisions:
        return []
    keys = sorted(decisions)
    affected = (
        (
            await session.execute(
                select(Candidate).where(
                    Candidate.snapshot_id == snapshot_id,
                    or_(Candidate.media_key.in_(keys), Candidate.group_key.in_(keys)),
                )
            )
        )
        .scalars()
        .all()
    )
    shifts: list[tuple[Candidate, str, str]] = []
    for c in affected:
        effective = effective_verdict(c, decisions)
        if effective != c.verdict:
            shifts.append((c, c.verdict, effective))
    return shifts


async def held_reaps(
    session: AsyncSession, snapshot_id: int, decisions: dict[str, str]
) -> list[Candidate]:
    """The hand reaps the engine will NOT honor yet: a reap override on a row the scan did not
    condemn, whose reap is refused (blocked evidence, a structural gate). These are HELD, not
    in the reap set -- the complement of what :func:`effective_condemned` admits from the same
    overridden rows, surfaced so the breakdown can report the operator's marks that are on
    hold rather than dropping them silently (PR-2). Reuses ``reap_is_effective``, never a
    second copy of the honor test.
    """
    reap_keys = sorted(k for k, d in decisions.items() if d == "reap")
    return [
        c
        for c in await _reap_overridden_rows(session, snapshot_id, reap_keys)
        if whitelist.effective_override(c.media_key, decisions) == "reap"
        and not reap_is_effective(c)
    ]
