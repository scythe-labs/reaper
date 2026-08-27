# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one condemn, abstain, or protect decision.

Every surface that answers "what happens to this item?" imports this function: the scan
(``services.snapshot``), the threshold simulator (``api.simulate.simulate``), and the
stored-row reap re-decision (``services.condemned``). A second copy of this logic risks a
small mismatch, such as ``>`` instead of ``>=`` at the exact threshold, that no reviewer
would catch: the review queue and the policy editor could then disagree about a real title
sitting right at the boundary the owner is tuning.

The decision, in order:

1. A hand "reap" override condemns the item, since the owner looked and decided. It never
   overrides a hard safety stop: something streaming right now, or an unmanaged file (this
   gate is retired; only a stored explanation can still carry that reason). See
   :data:`STRUCTURAL_GATES`. These two differ in kind from every other protection here:
   they are facts about whether the file can be removed at all, not judgments about
   whether it is wanted. Deleting mid-stream breaks a playing session, and a file no
   *arr manages has no path to delete through, so overriding either would not get the
   owner what they asked for.
2. A protection that could not be checked does not hold a hand reap. A block means Reaper
   could not answer a question. The owner, standing at the panel and reading exactly which
   check came back empty, can answer it themselves, and refusing them there would be
   Reaper overruling the person with more information. The alternative is worse for
   safety: an operator whose shallow watch history makes every reap bounce would delete
   the file outside Reaper instead, with no journal, no interlocks, and no record.

   What still protects the item is a live check that cannot be argued with:
   ``services.executor._being_watched_now`` re-polls Plex for each item right before
   sending it, and spares the item on any failure to read; ``services.executor.
   _watched_since_approval`` refuses an item played since the operator approved it. Below
   both, the transport guard refuses any mutating request unless the host is armed and the
   intent was journalled first.
3. A protection that fired protects the item.
4. A protection that could not be checked, or a keep-rule conflict flagged for a human,
   abstains. Not being able to look, or looking and finding the rule disagrees with the
   evidence, is not the same as looking and finding nothing. This keeps the file out of
   every automatic path, so nothing is removed on evidence Reaper could not gather. Only a
   human may now decide otherwise.
5. Coverage below the floor abstains: too little evidence to condemn on.
6. A score at or above the threshold condemns. Below it, the item abstains.

The decision runs on the stored, rounded integers (the score, and coverage in basis
points), never on the underlying floats, so a surface with only the stored row reaches the
same verdict as the scan that had everything in hand.
"""

from __future__ import annotations

from typing import Literal

from reaper.engine.gates import GateId

#: The three verdicts :func:`decide_verdict` returns, and the app's central vocabulary for
#: what happens to an item. Typing it here puts every ``return`` below under mypy, and lets
#: ``scripts.baseline_capture`` read the set directly from here instead of re-deriving it
#: from the source code.
Verdict = Literal["condemn", "protect", "abstain"]

#: The owner's hand decision about one item. ``api.schemas.OverrideIn.decision`` reads this
#: type rather than restating the pair, and ``frontend/src/api.ts``'s ``Override`` is its
#: mirror. It stays out of the response models on purpose: those validate rows already on
#: disk, and nothing constrains ``whitelist.decision`` or ``candidate.verdict`` at the
#: database, so narrowing them here would turn an old stored value into a server error on
#: the review queue.
Override = Literal["spare", "reap"]

#: The only two protections a manual "reap" override cannot overrule, and why just these
#: two: neither is a judgment about whether the file is wanted. Deleting a file that is
#: streaming right now breaks a session someone is watching, and an unmanaged file has no
#: path to delete through, so overriding either would not get the owner what they asked
#: for. Everything else (dormancy, rating, popularity, a curated list, the keep list, the
#: season keep-rule conflict) is a cautious judgment, and the owner may overrule a judgment
#: by hand whether it fired or merely could not be checked. The module docstring explains
#: why a block does not hold a reap.
#:
#: ``UNMANAGED`` is here only for reading old stored explanations. Its gate is retired (see
#: ``engine.gates``) because it could never fire, so no new scan produces a fired
#: ``unmanaged`` result. Keeping the id listed costs nothing, and means that if an old row
#: is ever read back, it still holds a hand reap instead of becoming overridable, which is
#: the safer direction. Removing it would be the only change here that could release a
#: file.
#:
#: This set is only consulted for a gate that fired. A blocked structural gate ("could not
#: tell whether it is streaming") does not hold the reap, and does not need to:
#: ``services.executor._being_watched_now`` re-polls Plex for every item right before
#: sending it, and treats any read failure as "watched," so this live check both replaces
#: the scan-time one and fails closed where the scan could only guess.
STRUCTURAL_GATES = frozenset({GateId.STREAMING_NOW, GateId.UNMANAGED})


def decide_verdict(
    *,
    protected: bool,
    blocked: bool,
    score: int,
    coverage_bp: int,
    condemn_at: int,
    coverage_floor_bp: int,
    override: Override | None = None,
    safety_protected: bool = False,
    blocked_holds_reap: bool | None = None,
) -> Verdict:
    """Decide the verdict, checked in order: protect, then blocked, then coverage, then score.

    ``protected`` means a protection fired. ``blocked`` means a protection could not be
    checked, or a keep-rule conflict flagged the item for a human; either forces ABSTAIN
    when the owner has not decided. ``safety_protected`` means a structural gate
    (:data:`STRUCTURAL_GATES`) fired. It is consulted only for a "reap" override, which
    beats every cautious protection but never a structural stop.

    ``blocked_holds_reap`` says whether anything about this item must hold a hand reap
    besides a structural stop. A gate that could not be checked does not count as one of
    those things. ``condemned.reap_override_verdict_decoded`` passes only two non-gate holds
    here: a bad Plex match, since the row may not be the file the owner is looking at, and
    an explanation this code could not parse, since the owner cannot consent to reasons the
    panel never showed them. Neither is a protection. Both mean "we do not know what this
    is," which is different from "we could not check whether it is wanted."

    The parsing check runs the panel's own validation function,
    ``engine.explanation.read_explanation``, rather than a copy of it, so both readers
    agree on what counts as readable. A separate, narrower check on the reap path once let
    a malformed row render blank in the panel but still reap.

    There is exactly one reap caller, and the scan is not it. ``snapshot._verdict`` takes
    no override at all: a hand reap is applied after the scan, by ``effective_fate``
    reading the frozen explanation, and re-decided from that explanation here.

    ``blocked_holds_reap`` defaults to ``blocked`` rather than to ``False``. The one caller
    that reaches this default is ``reap_override_verdict_decoded``'s early return for an
    explanation that is not valid JSON, which passes no value here at all; it resolves to
    holding the reap, which is what that row wants anyway (it also passes
    ``safety_protected=True``). The default stays fail-closed so a future caller that
    forgets to think about it inherits the cautious answer, the same reasoning that keeps
    ``UNMANAGED`` in :data:`STRUCTURAL_GATES`.

    ``override`` is the owner's hand decision. "reap" forces condemn past every cautious
    protection, whether it fired or could simply not be verified. "spare" is expressed
    upstream as an injected PROTECT result, so it needs no case here.
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
