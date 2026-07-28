# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scan and the simulator must never disagree about the same item.

They are two implementations of one question -- *"would this policy delete this?"* --
and they run against the same snapshot. The scan decides it while it has the full
evidence in hand; the simulator re-decides it later from nothing but the stored row.
If those two answers can differ, the product lies to you: the review queue shows an
item under "not judged" while the policy editor counts it as one of the deletions.

Found by running the real UI against a real library: the scan compared the *float*
score (69.7) against the threshold and abstained, but persisted ``round(69.7)`` = 70.
The simulator, which only ever sees the stored 70, condemned it. The queue and the
editor disagreed about a real film, at the same threshold, on the same snapshot.

The fix is structural rather than a patched comparison: round once, store that, and
make **everything** decide on the stored integer.
"""

from __future__ import annotations

import pytest

from reaper.engine.gates import ABSTAIN, PROTECT, Evaluation, GateId, GateResult
from reaper.engine.policy import DEFAULT_MOVIE_POLICY
from reaper.engine.verdict import decide_verdict
from reaper.services.snapshot import _verdict

CLEAN = Evaluation(
    results=[GateResult(GateId.RATING_FLOOR, ABSTAIN, detail="checked: not well-rated enough")]
)
PROTECTED = Evaluation(
    results=[GateResult(GateId.RATING_FLOOR, PROTECT, detail="IMDb 8.2 -- above your floor")]
)
BLOCKED = Evaluation(
    results=[
        GateResult(
            GateId.SERVER_POPULARITY,
            ABSTAIN,
            detail="could not reach Tautulli",
            blocked=True,
        )
    ]
)


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

        assert _verdict(CLEAN, score, 10_000, policy, override="reap") == "condemn"


class TestAReapOverrideForcesCondemnButNeverPastSafety:
    """A manual ``reap`` is the owner looking at the item and overruling the score. It must
    beat the *cautious* protections -- but never a hard safety gate, and never a protection
    that could not be checked. Getting this wrong deletes a file someone is watching."""

    def test_a_reap_override_condemns_an_item_that_scored_far_too_low(self) -> None:
        """Score 0 against a threshold of 100 -- nothing would condemn this on its own. The
        override does, because the owner said so."""
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 100})

        assert _verdict(CLEAN, 0, 10_000, policy, override="reap") == "condemn"

    def test_a_reap_override_does_not_delete_something_streaming_now(self) -> None:
        """The one line that matters: a hand reap must not beat the active-stream veto."""
        streaming = Evaluation(
            results=[GateResult(GateId.STREAMING_NOW, PROTECT, detail="being watched right now")]
        )
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 1})

        assert _verdict(streaming, 100, 10_000, policy, override="reap") == "protect"

    def test_a_reap_override_does_not_delete_an_unmanaged_file(self) -> None:
        """No *arr owns it, so there is no path to delete through -- reaping it is a lie."""
        unmanaged = Evaluation(
            results=[GateResult(GateId.UNMANAGED, PROTECT, detail="no *arr manages this file")]
        )
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 1})

        assert _verdict(unmanaged, 100, 10_000, policy, override="reap") == "protect"

    def test_a_reap_override_yields_when_a_protection_could_not_be_checked(self) -> None:
        """If we could not confirm nobody is streaming it, we do not force-delete it."""
        blocked = Evaluation(
            results=[
                GateResult(
                    GateId.STREAMING_NOW, ABSTAIN, detail="could not reach Tautulli", blocked=True
                )
            ]
        )
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 1})

        assert _verdict(blocked, 100, 10_000, policy, override="reap") == "protect"

    def test_a_reap_override_beats_a_soft_protection_like_a_rating_floor(self) -> None:
        """A rating floor is a *cautious* keep, not a safety guarantee -- the owner may
        overrule it. Only STREAMING_NOW and UNMANAGED are inviolable."""
        assert _verdict(PROTECTED, 0, 10_000, DEFAULT_MOVIE_POLICY, override="reap") == "condemn"

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
        assert _verdict(conflict, 90, 10_000, policy, override="reap") == "condemn"

    def test_a_reap_override_still_yields_when_the_season_guard_could_not_run(self) -> None:
        """Belt-and-suspenders: a deferrable gate whose block is a real plumbing failure
        still holds the reap. The gate id alone never opens a fail-open path, so a failure
        on the season guard is treated like any unchecked protection.

        Note what carries it: the result simply does not claim ``defers_to_owner``, which
        is the default. That is the fix for the arm that shipped broken -- it used to be
        inferred from the detail starting "could not check", and the one real message it
        had to catch opens with the watcher count instead."""
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
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 1})

        assert _verdict(plumbing, 100, 10_000, policy, override="reap") == "protect"

    def test_a_conflict_whose_comparison_was_refused_holds_the_reap(self) -> None:
        """The same fail-closed arm in the wording the season guard really emits, which is
        what made this reachable: the message names the count first and only then says the
        comparison could not be made, so ``startswith("could not check")`` never saw it and
        a hand reap removed the season. Nothing about the wording decides it now."""
        refused = Evaluation(
            results=[
                GateResult(
                    GateId.SEASON_PROGRESSION,
                    ABSTAIN,
                    detail=(
                        "40 people watched Season 1. Reaper could not check who watched "
                        "Season 4, which it is keeping because it is one of the newest "
                        "seasons your rule keeps. Kept for now."
                    ),
                    blocked=True,
                )
            ]
        )
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 1})

        assert _verdict(refused, 100, 10_000, policy, override="reap") == "protect"
