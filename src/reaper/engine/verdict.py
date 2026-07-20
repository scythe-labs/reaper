# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one condemn/abstain/protect decision.

Every surface that answers "what happens to this item?" -- the scan
(``services.snapshot``), the threshold simulator (``api.routes.simulate``) and the
backtest (``engine.backtest``) -- imports THIS function. The rule exists because the
decision once lived in three transcriptions, and a ``>`` for a ``>=`` at the exact
threshold in one of them is the kind of drift no reviewer catches: the review queue and
the policy editor disagreeing about a real title at the very boundary the owner is
tuning.

The decision, in order:

1. A hand ``reap`` override condemns -- the owner looked and decided -- but never past a
   hard safety stop (something streaming right now, an unmanaged file) or a protection
   that could not be checked; those still protect.
2. A protection that fired protects.
3. A protection that could not be checked abstains: not being able to look is not the
   same as looking and finding nothing.
4. Coverage below the floor abstains: too little evidence to condemn on.
5. At or above the threshold condemns; below it, abstain.

It decides on the STORED, ROUNDED integers (score, and coverage in basis points), never
on underlying floats, so a surface that has only the stored row reaches the same verdict
as the scan that had everything in hand.
"""

from __future__ import annotations

from reaper.engine.gates import GateId

#: The protections a manual "reap" override may NOT overrule -- a file that is streaming
#: right now must not be deleted, and an unmanaged file has no path to delete through.
#: Everything else (dormancy, rating, popularity, a curated list, the keep list) is a
#: *cautious* judgment the owner is entitled to overrule by hand.
STRUCTURAL_GATES = frozenset({GateId.STREAMING_NOW, GateId.UNMANAGED})


def decide_verdict(
    *,
    protected: bool,
    blocked: bool,
    score: int,
    coverage_bp: int,
    condemn_at: int,
    coverage_floor_bp: int,
    override: str | None = None,
    safety_protected: bool = False,
) -> str:
    """PROTECT beats everything. Then blocked. Then coverage. Then the score.

    ``protected`` -- a protection fired. ``blocked`` -- a protection could not be
    checked. ``safety_protected`` -- a structural gate (:data:`STRUCTURAL_GATES`) fired;
    consulted only for a ``reap`` override, which beats the cautious protections but
    never a safety stop. ``override`` is the owner's hand decision: ``"reap"`` forces
    condemn past cautious protections; a ``"spare"`` is expressed upstream as an injected
    PROTECT result and needs no case here.
    """
    if override == "reap":
        return "protect" if (blocked or safety_protected) else "condemn"
    if protected:
        return "protect"
    if blocked:
        return "abstain"
    if coverage_bp < coverage_floor_bp:
        return "abstain"
    if score >= condemn_at:
        return "condemn"
    return "abstain"
