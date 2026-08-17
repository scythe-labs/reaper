# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one condemn/abstain/protect decision.

Every surface that answers "what happens to this item?" -- the scan
(``services.snapshot``), the threshold simulator (``api.simulate.simulate``) and the
stored-row reap re-decision (``services.condemned``) -- imports THIS function. The rule
exists because the decision once lived in three transcriptions, and a ``>`` for a ``>=`` at
the exact threshold in one of them is the kind of drift no reviewer catches: the review queue and
the policy editor disagreeing about a real title at the very boundary the owner is
tuning.

The decision, in order:

1. A hand ``reap`` override condemns -- the owner looked and decided -- but never past a
   hard safety stop: something streaming right now, or an unmanaged file (that gate is
   retired, so only a stored explanation can still carry one). See
   :data:`STRUCTURAL_GATES`, and note those two are stops of a different KIND from every
   other protection here. They are not cautious judgments about whether a file is wanted;
   they are facts about whether it can be removed at all. Deleting mid-stream breaks a
   playing session, and a file no *arr manages has no path to delete through, so
   overruling either does not get the owner what they asked for.
2. A protection that could not be *checked* does NOT hold a hand reap. That is a
   deliberate choice and it reverses an earlier one. A block means Reaper could not
   answer a question; the owner standing at the panel, reading exactly which check came
   back empty, can answer it, and refusing them is Reaper overruling the better-informed
   party. The alternative is worse for safety, not better: an operator whose shallow
   watch history makes every reap bounce deletes the file outside Reaper instead, with no
   journal, no interlocks and no record.

   What still protects them is the layer that cannot be argued with, and it is a live read
   rather than a frozen guess: ``services.executor._being_watched_now`` re-polls Plex per
   item at send time and spares on ANY failure to read, and
   ``services.executor._watched_since_approval`` refuses an item played since the operator
   approved it. Underneath both, the transport guard refuses any mutation unless the host
   is armed and the intent was journalled first. A scan-time block was never the last line
   of defense and was the wrong place to hang this.
3. A protection that fired protects.
4. A protection that could not be checked, or a keep-rule conflict flagged for a human,
   abstains: not being able to look -- or looking and finding the rule fights the
   evidence -- is not the same as looking and finding nothing. Abstain is where a block
   still does its whole job. It keeps the file out of every automatic path, so nothing is
   removed on evidence Reaper could not gather; what changed is only that a HUMAN may now
   say otherwise.
5. Coverage below the floor abstains: too little evidence to condemn on.
6. At or above the threshold condemns; below it, abstain.

It decides on the STORED, ROUNDED integers (score, and coverage in basis points), never
on underlying floats, so a surface that has only the stored row reaches the same verdict
as the scan that had everything in hand.
"""

from __future__ import annotations

from typing import Literal

from reaper.engine.gates import GateId

#: The three verdicts :func:`decide_verdict` returns, and the app's central vocabulary. It was
#: declared only in TypeScript (``frontend/src/api.ts``'s ``Verdict``) while Python passed it
#: around as a bare ``str``, so nothing on this side could disagree loudly. Typing it puts every
#: ``return`` below under mypy, which is what lets ``scripts.baseline_capture`` read the set from
#: here rather than re-deriving it from the AST (rule 103).
Verdict = Literal["condemn", "protect", "abstain"]

#: The owner's hand decision about one item. ``api.schemas.OverrideIn.decision`` reads this
#: rather than restating the pair (rule 131), and ``frontend/src/api.ts``'s ``Override`` is its
#: mirror. Kept out of the response models on purpose: those validate rows already on disk, and
#: nothing constrains ``whitelist.decision`` or ``candidate.verdict`` at the database, so
#: narrowing them would turn a legacy value into a 500 on the review queue.
Override = Literal["spare", "reap"]

#: The ONLY protections a manual "reap" override may not overrule, and the reason they are
#: the only two: neither is a judgment about whether the file is WANTED. Deleting a file
#: that is streaming right now breaks a session someone is watching, and an unmanaged file
#: has no path to delete through -- so overruling either cannot give the owner what they
#: asked for. Everything else (dormancy, rating, popularity, a curated list, the keep list,
#: the season keep-rule conflict) is a *cautious judgment*, and the owner is entitled to
#: overrule a judgment by hand whether the protection FIRED or merely could not be CHECKED.
#: The module docstring carries why "could not be checked" stopped holding.
#:
#: ``UNMANAGED`` is here for STORED EXPLANATIONS only: its gate was retired (see
#: ``engine.gates``) because it could never fire, so no new scan can produce a fired
#: ``unmanaged`` result. Keeping the id listed costs nothing and means that if one ever is
#: read back off disk, it still holds a hand reap rather than becoming overrulable -- the
#: keep direction. Removing it would be the only change here that could release a file.
#:
#: Consulted for a gate that FIRED. A *blocked* structural gate -- "could not tell whether
#: it is streaming" -- does not hold the reap, and does not need to:
#: ``services.executor._being_watched_now`` re-polls Plex for every item at send time and
#: returns "watched" on any read failure, so the live check both supersedes the scan-time
#: one and fails closed where the scan could only guess.
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
    """PROTECT beats everything. Then blocked. Then coverage. Then the score.

    ``protected`` -- a protection fired. ``blocked`` -- a protection could not be checked,
    OR a keep-rule conflict flagged the item for a human; either forces ABSTAIN when the
    owner has not decided. ``safety_protected`` -- a structural gate
    (:data:`STRUCTURAL_GATES`) fired; consulted only for a ``reap`` override, which beats
    every cautious protection but never a structural stop.

    ``blocked_holds_reap`` -- whether anything about this item must hold a hand reap
    *besides* a structural stop. **A gate that could not be checked is no longer one of
    those things.** ``condemned.reap_override_verdict_decoded`` passes only its two
    non-gate holds -- a bad Plex match (the row may not be the file the owner is looking
    at, so this is identity, not judgment) and an explanation this code could not parse
    (they cannot consent to reasons the panel never rendered). Neither is a protection;
    both are "we do not know WHAT this is", which is a different question from "we could
    not check whether it is wanted".

    The second of those is enforced by running the panel's own validation, not a likeness of
    it: ``engine.explanation.read_explanation`` is the single definition both readers call
    (rule 104). It was two definitions, and the narrower one sat on the destructive path --
    the panel refused any bad field anywhere in the document, the reap path tested only
    whether the protections lists were the right shape, so a row with a string where a
    signal's contribution belongs rendered blank and reaped anyway (#142). The sentence in
    parentheses above is the promise; that function is what makes it one.

    **There is exactly one reap caller**, and the scan is not it: ``snapshot._verdict`` takes
    no override at all, because a hand reap is applied after the freeze by ``effective_fate``
    off the frozen explanation and re-decided from that explanation here.

    The parameter defaults to ``blocked`` rather than to ``False``, and that one caller
    reaches the default on one arm: ``reap_override_verdict_decoded``'s early return for an
    explanation that is not a JSON object, which passes no value here at all. It resolves to
    hold, which is what that row wants anyway (it also passes ``safety_protected=True``), so
    the default is load-bearing there rather than vestigial. It stays fail-closed so a future
    caller that forgets to think about it inherits the cautious answer -- the same reasoning
    that keeps ``UNMANAGED`` in :data:`STRUCTURAL_GATES`.

    ``override`` is the owner's hand decision: ``"reap"`` forces condemn past every
    cautious protection, fired or unverifiable alike; a ``"spare"`` is expressed upstream
    as an injected PROTECT result and needs no case here.
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
