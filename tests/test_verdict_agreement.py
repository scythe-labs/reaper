# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scan and the simulator must never disagree about the same item.

Both answer *"would this policy delete this?"* against the same snapshot: the scan with
the full evidence in hand, the simulator later from nothing but the stored row. If the
answers can differ, the product lies to you: the review queue shows an item under "not
judged" while the policy editor counts it as one of the deletions.

Found by running the real UI against a real library: the scan compared the *float*
score (69.7) against the threshold and abstained, but persisted ``round(69.7)`` = 70,
and the simulator, which only ever sees the stored 70, condemned a real film. The fix
is structural rather than a patched comparison: round once, store that, and make
**everything** decide on the stored integer.
"""

from __future__ import annotations

import json

import pytest

from reaper.engine.gates import ABSTAIN, PROTECT, Evaluation, GateId, GateResult
from reaper.engine.policy import DEFAULT_MOVIE_POLICY, PolicyBody
from reaper.engine.signals import Score
from reaper.engine.verdict import decide_verdict
from reaper.services.condemned import reap_override_verdict_decoded
from reaper.services.snapshot import _explain, _verdict

CLEAN = Evaluation(
    results=[
        GateResult(
            GateId.RATING_FLOOR,
            ABSTAIN,
            detail="5.4 on IMDb from 6,000 votes, below the 7.5 you keep.",
        )
    ]
)
PROTECTED = Evaluation(
    results=[
        GateResult(
            GateId.RATING_FLOOR, PROTECT, detail="well rated: 8.2 on IMDb from 120,000 votes"
        )
    ]
)
BLOCKED = Evaluation(
    results=[
        GateResult(
            GateId.SERVER_POPULARITY,
            ABSTAIN,
            detail="could not check watch history: Tautulli did not respond",
            blocked=True,
        )
    ]
)


def _hand_reap(evaluation: Evaluation, score: int, policy: PolicyBody) -> str:
    """What a hand reap on this evaluation actually decides, through the production path.

    A reap never reaches ``snapshot._verdict``: the scan freezes an explanation, and
    ``condemned.reap_override_verdict_decoded`` re-decides from that document when the
    operator presses Reap -- in the queue, in the plan, and in the executor's per-item
    re-read. So the frozen document is produced here by the real writer
    (``snapshot._explain``) rather than hand-typed, and a field the writer stops emitting
    fails these tests instead of quietly changing what the read side can see.
    """
    frozen = Score(value=float(score), coverage=1.0, results=[])
    stored = json.loads(_explain(evaluation, frozen, policy))
    return reap_override_verdict_decoded(stored, score=score)


def _simulator_verdict(score: int, coverage_bp: int, condemn_at: int, floor_bp: int) -> str:
    """The simulator's re-decision for a clean stored row, as ``api.routes.simulate``
    actually makes it: the REAL shared function, with the stored integers.

    Not a transcription. The route imports ``decide_verdict`` and calls it exactly like
    this for every row it re-decides (protected, blocked and overridden rows are never
    re-decided -- pinned in ``TestRowsTheSimulatorMustNotReDecide`` below and in the
    route-level tests in ``test_api.py``), so a regression in the shared function fails
    here, and a route that stops using it fails the route-level boundary test.
    """
    return decide_verdict(
        protected=False,
        blocked=False,
        score=score,
        coverage_bp=coverage_bp,
        condemn_at=condemn_at,
        coverage_floor_bp=floor_bp,
    )


class TestTheStoredScoreIsTheDecidingScore:
    def test_a_score_exactly_on_the_threshold_is_condemned(self) -> None:
        """``condemn_at`` is documented as the score *at or above* which an item is a
        candidate. An item showing 70 against a threshold of 70 must not be spared --
        the owner reading the table has no way to know a hidden 69.7 was the real
        number."""
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 70})

        assert _verdict(CLEAN, 70, 10_000, policy) == "condemn"

    def test_one_below_the_threshold_is_not(self) -> None:
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 70})

        assert _verdict(CLEAN, 69, 10_000, policy) == "abstain"

    @pytest.mark.parametrize("score", range(0, 101, 5))
    @pytest.mark.parametrize("condemn_at", [1, 40, 70, 91, 100])
    def test_the_scan_and_the_simulator_agree_everywhere(self, score: int, condemn_at: int) -> None:
        """The invariant, swept across the whole grid. There is no score and no
        threshold at which the review queue and the policy editor may disagree."""
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": condemn_at})

        scan = _verdict(CLEAN, score, 10_000, policy)
        simulator = _simulator_verdict(score, 10_000, condemn_at, policy.coverage_floor_bp)

        assert scan == simulator

    @pytest.mark.parametrize("coverage_bp", [0, 2_500, 4_999, 5_000, 10_000])
    def test_they_agree_on_the_coverage_floor_too(self, coverage_bp: int) -> None:
        """The other boundary, and the same trap: coverage is stored in basis points
        and compared in basis points, by both."""
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 70})

        scan = _verdict(CLEAN, 95, coverage_bp, policy)
        simulator = _simulator_verdict(95, coverage_bp, 70, policy.coverage_floor_bp)

        assert scan == simulator


class TestAProtectionStillBeatsTheScore:
    """The simulator never re-decides a protected row. This pins the reason why."""

    def test_a_protected_item_is_protected_at_any_score(self) -> None:
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 1})

        assert _verdict(PROTECTED, 100, 10_000, policy) == "protect"

    def test_a_protection_that_could_not_be_checked_abstains(self) -> None:
        """ "We could not look" is not "we looked and it was fine"."""
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 1})

        assert _verdict(BLOCKED, 100, 10_000, policy) == "abstain"


class TestRowsTheSimulatorMustNotReDecide:
    """The two row kinds the simulator keeps out of the score comparison, and the scan
    behavior that makes that the ONLY correct treatment. The simulator marks a stored
    row with non-empty ``protections_unknown`` abstained at any threshold, and keeps an
    overridden row at its stored verdict; these sweeps pin that the scan agrees at every
    score and threshold, so the route's skip can never drift from production."""

    @pytest.mark.parametrize("score", range(0, 101, 10))
    @pytest.mark.parametrize("condemn_at", [1, 40, 70, 91, 100])
    def test_a_blocked_row_abstains_at_every_score_and_threshold(
        self, score: int, condemn_at: int
    ) -> None:
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": condemn_at})

        assert _verdict(BLOCKED, score, 10_000, policy) == "abstain"

    @pytest.mark.parametrize("score", range(0, 101, 10))
    @pytest.mark.parametrize("condemn_at", [1, 40, 70, 91, 100])
    def test_a_hand_reaped_row_condemns_at_every_threshold(
        self, score: int, condemn_at: int
    ) -> None:
        """A reap override pins the verdict regardless of the score or the threshold --
        which is exactly why the simulator must keep an overridden row at its stored
        verdict instead of re-deciding it on score."""
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": condemn_at})

        assert _hand_reap(CLEAN, score, policy) == "condemn"


class TestAReapOverrideForcesCondemnButNeverPastSafety:
    """A manual ``reap`` is the owner looking at the item and overruling the score.

    It beats every *cautious* protection -- fired or merely unverifiable -- and stops only
    at a structural gate that FIRED (:data:`verdict.STRUCTURAL_GATES`: streaming right now,
    or a file no *arr manages). Getting the structural half wrong deletes a file someone is
    watching.

    The unverifiable half reversed deliberately, and these tests carry the new answer rather
    than the old one. A blocked gate means Reaper could not answer a question; the owner
    standing at the panel that names the failed check can answer it. What replaced the
    scan-time hold is a live read at send time -- ``executor._being_watched_now`` re-polls
    Plex per item and spares on ANY failure to read, pinned by
    ``test_reap_loop.py::TestStreamingVeto::test_plex_unreadable_fails_closed`` -- plus
    ``executor._watched_since_approval`` and the transport guard beneath both.
    """

    def test_a_reap_override_condemns_an_item_that_scored_far_too_low(self) -> None:
        """Score 0 against a threshold of 100 -- nothing would condemn this on its own. The
        override does, because the owner said so."""
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 100})

        assert _hand_reap(CLEAN, 0, policy) == "condemn"

    def test_a_reap_override_does_not_delete_something_streaming_now(self) -> None:
        """The one line that matters: a hand reap must not beat the active-stream veto."""
        streaming = Evaluation(
            results=[
                GateResult(GateId.STREAMING_NOW, PROTECT, detail="someone is watching it right now")
            ]
        )
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 1})

        assert _hand_reap(streaming, 100, policy) == "protect"

    def test_a_reap_override_does_not_delete_an_unmanaged_file(self) -> None:
        """No *arr owns it, so there is no path to delete through -- reaping it is a lie.

        The detail here answers to no live producer and cannot be quoted from one: the gate
        was retired as unreachable, and ``GateId.UNMANAGED`` survives only so a stored
        explanation written before that still decodes (``engine.gates``, below
        ``DataHorizonGate``). This row IS that legacy shape, and its half of
        ``STRUCTURAL_GATES`` is kept for it, so the string stays hand-written on purpose.
        """
        unmanaged = Evaluation(
            results=[GateResult(GateId.UNMANAGED, PROTECT, detail="no *arr manages this file")]
        )
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 1})

        assert _hand_reap(unmanaged, 100, policy) == "protect"

    def test_a_reap_override_passes_a_streaming_check_that_could_not_run(self) -> None:
        """The reversal, on the gate where it reads worst -- and the reason it is still safe.

        A *blocked* streaming gate is "we could not read who is playing", frozen minutes or
        hours before anything is sent. It no longer holds the reap, because the check that
        decides this now happens at send time: ``executor._being_watched_now`` re-polls Plex
        for this very item and returns "watched" on any read failure, so the live veto both
        supersedes the scan-time guess and fails closed where the scan could only guess. A
        *fired* streaming gate is a different claim and still holds it (above).

        With no override the block still does its whole job one line down: ABSTAIN, so no
        automatic path touches the item. Both are asserted, because the value of this change
        is exactly that a human may say otherwise while nothing else may."""
        blocked = Evaluation(
            results=[
                GateResult(
                    GateId.STREAMING_NOW,
                    ABSTAIN,
                    detail="could not check active streams: Tautulli did not respond",
                    blocked=True,
                )
            ]
        )
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 1})

        assert _verdict(blocked, 100, 10_000, policy) == "abstain"
        assert _hand_reap(blocked, 100, policy) == "condemn"

    @pytest.mark.parametrize(
        "gate",
        [
            GateId.SERVER_POPULARITY,
            GateId.CUSTOM,
            GateId.MIN_DORMANCY,
            GateId.SEASON_PROGRESSION,
        ],
        ids=lambda g: str(g.value),
    )
    def test_a_reap_override_condemns_past_any_gate_that_could_not_be_checked(
        self, gate: GateId
    ) -> None:
        """Swept across four distinct gates, not one, because the old rule was expressed as
        a gate-id membership test and a single case cannot tell "no gate holds" from "this
        gate is on the permitted list". ``min_dormancy`` is in the sweep deliberately: it is
        the gate ``engine.gates`` calls the most important one, so if any block were still to
        hold, that is the block a reader would expect it to be.

        The paired ABSTAIN is what keeps this from reading as a blanket loosening. A block
        still keeps the item out of every automatic path; all that changed is that the owner
        may answer it."""
        # Abstract on purpose, unlike the fixtures elsewhere in this file: the sweep runs one
        # row per gate and each gate names its own subject ("active streams", "watch
        # history"), so no single real sentence fits. Nothing here reads the wording -- what
        # is asserted is the verdict either side of the override.
        blocked = Evaluation(
            results=[GateResult(gate, ABSTAIN, detail="could not check it", blocked=True)]
        )
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 1})

        assert _verdict(blocked, 100, 10_000, policy) == "abstain"
        assert _hand_reap(blocked, 100, policy) == "condemn"

    #: One row per shape a reap can meet, with the answer written out by hand from
    #: ``engine.verdict``'s docstring rather than derived from either implementation
    #: (rule 119). Used by the agreement sweep below.
    _REAP_SHAPES: tuple[tuple[str, list[GateResult], str], ...] = (
        ("nothing fired, nothing blocked", [], "condemn"),
        (
            "a cautious protection fired",
            [
                GateResult(
                    GateId.RATING_FLOOR,
                    PROTECT,
                    detail="well rated: 8.2 on IMDb from 120,000 votes",
                )
            ],
            "condemn",
        ),
        (
            "something is playing right now",
            [GateResult(GateId.STREAMING_NOW, PROTECT, detail="someone is watching it right now")],
            "protect",
        ),
        (
            "no *arr manages the file",
            [GateResult(GateId.UNMANAGED, PROTECT, detail="no *arr manages this file")],
            "protect",
        ),
        (
            "a popularity check that could not run",
            [
                GateResult(
                    GateId.SERVER_POPULARITY,
                    ABSTAIN,
                    detail="could not check watch history: Tautulli did not respond",
                    blocked=True,
                )
            ],
            "condemn",
        ),
        (
            "a streaming check that could not run",
            [
                GateResult(
                    GateId.STREAMING_NOW,
                    ABSTAIN,
                    detail="could not check active streams: Tautulli did not respond",
                    blocked=True,
                )
            ],
            "condemn",
        ),
        (
            "a season conflict the guard settled",
            [
                GateResult(
                    GateId.SEASON_PROGRESSION,
                    ABSTAIN,
                    detail="watched more than a season your rule keeps",
                    blocked=True,
                    defers_to_owner=True,
                )
            ],
            "condemn",
        ),
        (
            "a season conflict the guard refused to settle",
            [
                GateResult(
                    GateId.SEASON_PROGRESSION,
                    ABSTAIN,
                    detail="could not check who watched Season 4",
                    blocked=True,
                    defers_to_owner=False,
                )
            ],
            "condemn",
        ),
        (
            "a structural stop beside a check that could not run",
            [
                GateResult(
                    GateId.STREAMING_NOW, PROTECT, detail="someone is watching it right now"
                ),
                GateResult(
                    GateId.SERVER_POPULARITY,
                    ABSTAIN,
                    detail="could not check watch history: Tautulli did not respond",
                    blocked=True,
                ),
            ],
            "protect",
        ),
    )

    @pytest.mark.parametrize(("label", "results", "expected"), _REAP_SHAPES, ids=lambda x: x)
    def test_the_stored_row_reaps_each_shape_the_way_the_spec_says(
        self, label: str, results: list[GateResult], expected: str
    ) -> None:
        """One answer per shape a reap can meet, from the ONE caller that decides them.

        ``condemned.reap_override_verdict_decoded`` re-decides the item hours after the scan
        off nothing but the frozen explanation, and it serves three surfaces from there: the
        queue's Reap button, the plan, and the executor's per-item re-read. Nothing else
        decides a reap -- the scan's ``_verdict`` takes no override, so there is no second
        implementation to hold this in step with, and this sweep is the whole of the reap
        branch's coverage rather than half of an agreement pair.

        The frozen explanation is produced by the real writer (``snapshot._explain``) from an
        ``Evaluation``, never hand-typed, so a field the writer stops emitting fails here
        instead of silently changing what the read side can see. The expected answer is
        written out in ``_REAP_SHAPES`` from the decision's spec (rule 119), so a shape the
        reader gets wrong still fails even though only one implementation is left."""
        evaluation = Evaluation(results=results)
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 100})

        assert _hand_reap(evaluation, 42, policy) == expected, label

    def test_a_reap_override_beats_a_soft_protection_like_a_rating_floor(self) -> None:
        """A rating floor is a *cautious* keep, not a safety guarantee -- the owner may
        overrule it. Only STREAMING_NOW and UNMANAGED are inviolable."""
        assert _hand_reap(PROTECTED, 0, DEFAULT_MOVIE_POLICY) == "condemn"

    def test_a_reap_override_overrules_a_keep_rule_conflict(self) -> None:
        """The keep-rule conflict flags a season for a human to decide -- a *blocked*
        ABSTAIN that says "you decide" rather than "could not check", which the producer
        marks with ``defers_to_owner``. With no override it abstains (needs a look); a hand
        reap IS the decision it asked for, so it condemns -- unlike a protection that could
        not be checked, which still holds."""
        conflict = Evaluation(
            results=[
                GateResult(
                    GateId.SEASON_PROGRESSION,
                    ABSTAIN,
                    detail="5 people watched it, more than a season your keep rule protects",
                    blocked=True,
                    defers_to_owner=True,
                )
            ]
        )
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 100})

        assert _verdict(conflict, 90, 10_000, policy) == "abstain"
        assert _hand_reap(conflict, 90, policy) == "condemn"

    def test_the_wording_of_a_season_block_decides_nothing_either_way(self) -> None:
        """The three season-guard block shapes reach ONE verdict, and no sentence moves it.

        This used to be the fail-closed half: a conflict whose comparison was refused held
        the reap while the settleable one released it, and the arm meant to tell them apart
        tested ``detail.startswith("could not check")`` -- which the one message it existed
        for never matched, because that message opens with the watcher count. The split is
        gone, so the wording trap cannot come back through this door; what remains asserted
        is that it decides nothing.

        ``defers_to_owner`` is still set and still varied here, because it still picks the
        operator's chip (``api.routes._chip``, pinned in ``test_review_chips.py``). It must
        not pick the verdict. Each shape also abstains with no override -- the item still
        goes to a human first."""
        details = {
            # The settleable comparison: made, and the keep rule lost it.
            True: (
                "40 people watched Season 1, more than watched Season 4, which Reaper is "
                "keeping because it is one of the newest seasons your rule keeps. Left for "
                "you to decide instead of removing it."
            ),
            # The refused one, in the wording the guard really emits: the count comes first,
            # so a prefix test never saw the refusal.
            False: (
                "40 people watched Season 1. Reaper could not check who watched Season 4, "
                "which it is keeping because it is one of the newest seasons your rule "
                "keeps. Left for you to decide instead of removing it."
            ),
        }
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 1})

        for defers, detail in details.items():
            conflict = Evaluation(
                results=[
                    GateResult(
                        GateId.SEASON_PROGRESSION,
                        ABSTAIN,
                        detail=detail,
                        blocked=True,
                        defers_to_owner=defers,
                    )
                ]
            )

            assert _verdict(conflict, 100, 10_000, policy) == "abstain", defers
            assert _hand_reap(conflict, 100, policy) == "condemn", defers

        # And a plumbing failure on the same gate, whose detail DOES carry the retired
        # prefix, lands in the same place. The prefix is not a hold and never was one.
        plumbing = Evaluation(
            results=[
                GateResult(
                    GateId.SEASON_PROGRESSION,
                    ABSTAIN,
                    detail="could not check the sequential guard",
                    blocked=True,
                )
            ]
        )

        assert _verdict(plumbing, 100, 10_000, policy) == "abstain"
        assert _hand_reap(plumbing, 100, policy) == "condemn"
