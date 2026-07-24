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
   that could not be *checked*; those still protect. A block that is a deliberate "the
   owner should decide" flag (the keep-rule conflict) is the one exception a reap does
   overrule -- the reap IS the decision it asked for (see :func:`block_holds_reap`).
2. A protection that fired protects.
3. A protection that could not be checked, or a keep-rule conflict flagged for a human,
   abstains: not being able to look -- or looking and finding the rule fights the
   evidence -- is not the same as looking and finding nothing.
4. Coverage below the floor abstains: too little evidence to condemn on.
5. At or above the threshold condemns; below it, abstain.

It decides on the STORED, ROUNDED integers (score, and coverage in basis points), never
on underlying floats, so a surface that has only the stored row reaches the same verdict
as the scan that had everything in hand.
"""

from __future__ import annotations

from collections.abc import Iterable

from reaper.engine.gates import GateId, GateResult

#: The protections a manual "reap" override may NOT overrule -- a file that is streaming
#: right now must not be deleted, and an unmanaged file has no path to delete through.
#: Everything else (dormancy, rating, popularity, a curated list, the keep list) is a
#: *cautious* judgment the owner is entitled to overrule by hand.
STRUCTURAL_GATES = frozenset({GateId.STREAMING_NOW, GateId.UNMANAGED})

#: Gates whose *blocked* result is a deliberate "the owner should decide" flag, not a
#: source Reaper could not read. Today: the season keep-rule conflict (``season_scan.
#: guard_result``) -- a season the keep rule would prune, but that was watched MORE than a
#: season the rule keeps, so Reaper refuses to auto-approve and leaves the call to a human.
#: Like any block it forces ABSTAIN (needs a look) when the owner has not decided, but a
#: hand *reap* overrules it: the reap IS the decision the flag asked for. This is the
#: opposite of a protection that could not be checked (a plumbing failure), which still
#: holds a reap, fail-closed. A gate listed here must NEVER emit a *blocked* result for a
#: real plumbing failure; if one ever does, the ``could not check ...`` detail keeps it
#: fail-closed regardless (see :func:`block_holds_reap`), so the gate id alone can never
#: open a fail-open path.
DEFERRABLE_BLOCK_GATES = frozenset({GateId.SEASON_PROGRESSION})
_DEFERRABLE_GATE_VALUES = frozenset(g.value for g in DEFERRABLE_BLOCK_GATES)


def block_holds_reap(gate_value: str, detail: str) -> bool:
    """Whether one *blocked* gate result must still hold a hand reap.

    A block holds a reap by default: not being able to confirm a protection is not
    permission to remove the file (fail-closed). The one exception is a deliberate "the
    owner should decide" deferral (:data:`DEFERRABLE_BLOCK_GATES`, today the keep-rule
    conflict) -- a hand reap IS that decision, so it does not hold. Belt-and-suspenders: a
    ``could not check ...`` detail is a genuine plumbing failure even on a deferrable gate,
    so it always holds; the gate id alone never opens a fail-open path.

    Takes primitives (a gate's string value and its detail) so the one classifier serves
    both the GateResult path (scan, simulator replay) and the stored-explanation path
    (``services.condemned.reap_override_verdict``) without a second transcription.
    """
    defers_to_owner = gate_value in _DEFERRABLE_GATE_VALUES and not detail.startswith(
        "could not check"
    )
    return not defers_to_owner


def reap_held_by_blocks(results: Iterable[GateResult]) -> bool:
    """True if any *blocked* result among ``results`` must hold a hand reap (fail-closed).

    The GateResult-level companion to :func:`block_holds_reap`, so ``snapshot._verdict`` and
    the simulator replay classify blocks the same way. A snapshot with no blocked result, or
    only deferrable ones (the keep-rule conflict), returns ``False`` -- a hand reap is
    honored. A single un-checkable protection returns ``True`` and the reap is held.
    """
    return any(r.blocked and block_holds_reap(r.gate.value, r.detail) for r in results)


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
    blocked_holds_reap: bool | None = None,
) -> str:
    """PROTECT beats everything. Then blocked. Then coverage. Then the score.

    ``protected`` -- a protection fired. ``blocked`` -- a protection could not be checked,
    OR a keep-rule conflict flagged the item for a human; either forces ABSTAIN when the
    owner has not decided. ``safety_protected`` -- a structural gate
    (:data:`STRUCTURAL_GATES`) fired; consulted only for a ``reap`` override, which beats
    the cautious protections but never a safety stop. ``blocked_holds_reap`` -- of the
    blocking, whether it must still hold a hand reap; defaults to ``blocked`` (every block
    holds, the fail-closed default). A caller that can tell a "you decide" deferral (the
    keep-rule conflict) from a "could not check" plumbing failure passes the narrower value
    (see :func:`reap_held_by_blocks`), so a hand reap overrules the deferral it was asked to
    settle but never a block Reaper could not confirm. ``override`` is the owner's hand
    decision: ``"reap"`` forces condemn past cautious protections and a deferral; a
    ``"spare"`` is expressed upstream as an injected PROTECT result and needs no case here.
    """
    if blocked_holds_reap is None:
        blocked_holds_reap = blocked
    if override == "reap":
        return "protect" if (blocked_holds_reap or safety_protected) else "condemn"
    if protected:
        return "protect"
    if blocked:
        return "abstain"
    if coverage_bp < coverage_floor_bp:
        return "abstain"
    if score >= condemn_at:
        return "condemn"
    return "abstain"
