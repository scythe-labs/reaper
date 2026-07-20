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
from reaper.engine.verdict import STRUCTURAL_GATES, decide_verdict
from reaper.services import whitelist


def reap_override_verdict(explanation_json: str, *, score: int) -> str:
    """What a hand reap does to one stored row: ``"condemn"`` or ``"protect"``.

    Inputs are derived from the frozen explanation -- which protections fired, whether
    any could not be checked, whether the Plex match was clean -- and handed to
    ``decide_verdict`` with ``override="reap"``. That branch consults only ``blocked``
    and ``safety_protected`` (a structural gate: streaming right now, or a file no
    *arr manages); the score, coverage and thresholds are never read on it, so the
    zeros below are inert plumbing, pinned by a test, not hidden policy.

    A malformed or unreadable explanation reads as blocked: not being able to see why
    an item was kept is not permission to remove it.
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

    return decide_verdict(
        protected=bool(fired),
        blocked=bool(unknown) or match_status in ("unmatched", "ambiguous"),
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
    if not reap_keys:
        return out

    # A reap decision names an item (a movie's or a season's media_key) or a whole show
    # (the seasons' group_key). Fetch the not-already-condemned rows it could cover.
    overridden = (
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
    for c in overridden:
        # A season spared back out of a hand-reaped show resolves to "spare" here.
        if whitelist.effective_override(c.media_key, decisions) != "reap":
            continue
        if reap_is_effective(c):
            out[c.media_key] = c
    return out
