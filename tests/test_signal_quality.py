# SPDX-License-Identifier: AGPL-3.0-or-later
"""The signal-quality corrections.

Every case here comes from discovering, by backtest, that the first scorer was
**worse than useless** -- it selected films *more* likely to be watched than a random
film of the same age (-50% lift).

See docs/SIGNALS.md and docs/LEARNINGS.md.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from reaper.clock import utcnow
from reaper.engine.backtest import BacktestResult, Regret, rewatch_prior
from reaper.engine.gates import (
    ABSTAIN,
    PROTECT,
    Facts,
    GateConfig,
    GateId,
    MinDormancyGate,
)
from reaper.engine.observation import Absent, Known, Unknown
from reaper.engine.policy import DEFAULT_MOVIE_POLICY, GateSetting
from reaper.engine.signals import SignalId


def _facts(days_dormant: float | None) -> Facts:
    dormancy = (
        Known(value=days_dormant, source="tautulli")
        if days_dormant is not None
        else Unknown(reason="Tautulli unreachable", source="tautulli")
    )
    return Facts(
        title="A Film",
        days_observed_unwatched=dormancy,
        distinct_watchers=Known(value=0, source="t"),
        distinct_watchers_all_time=Known(value=1, source="t"),
        size_bytes=Known(value=50_000_000_000, source="radarr"),
        imdb_rating_tenths=Known(value=60, source="imdb"),
        imdb_votes=Known(value=5000, source="imdb"),
        season_rank=Absent(source="radarr"),
        is_streaming_now=Known(value=False, source="t"),
        is_managed=Known(value=True, source="radarr"),
        in_curated_list=Absent(source="lists"),
        is_whitelisted=Known(value=False, source="plex"),
        others_watching=Absent(source="t"),
    )


GATE = MinDormancyGate(GateConfig(GateId.MIN_DORMANCY, threshold=1095))


class TestTheMinDormancyGate:
    """A GATE, not a weight. A weight can be outvoted -- and that is exactly how the
    first engine condemned films with a 30% chance of coming back."""

    def test_a_film_dormant_less_than_the_floor_is_protected(self) -> None:
        """400 days dormant. Measured rewatch probability: ~31%. Deleting here is a
        coin-flip against a real person."""
        result = GATE.evaluate(_facts(400))

        assert result.outcome == PROTECT
        # The floor of 1095 days reads as "3 years". "Untouched", never "last watched":
        # a never-played item's clock runs from its arrival, not from a play.
        assert "untouched" in result.detail
        assert "3 years" in result.detail

    def test_a_film_past_the_floor_is_not_protected_by_this_gate(self) -> None:
        """Beyond three years the rewatch rate drops to 8%, and past five to 2%."""
        result = GATE.evaluate(_facts(1500))

        assert result.outcome == ABSTAIN
        assert "Untouched for" in result.detail
        assert "3 years" in result.detail  # the floor, humanised

    def test_a_gigantic_low_rated_film_is_still_protected_if_it_is_too_recent(
        self,
    ) -> None:
        """The whole point of a gate. This item is 50 GB, rated 6.0, watched by nobody
        -- it would score near the top under any weighting. It is still 400 days
        dormant, so it is spared regardless."""
        result = GATE.evaluate(_facts(400))

        assert result.outcome == PROTECT

    def test_unknown_dormancy_blocks_rather_than_condemns(self) -> None:
        result = GATE.evaluate(_facts(None))

        assert result.outcome == ABSTAIN
        assert result.blocked is True

    def test_the_floor_cannot_be_set_below_the_measured_safe_line(self) -> None:
        """Not a stylistic bound. Under a year, ~30% of dormant films come back."""
        with pytest.raises(ValidationError, match="coin-flip"):
            GateSetting(gate=GateId.MIN_DORMANCY, threshold=90)

    def test_disabling_it_is_allowed_but_must_be_explicit(self) -> None:
        """A decision, not a slip: you may turn the protection off, but you may not
        neuter it by setting the threshold to zero."""
        assert GateSetting(gate=GateId.MIN_DORMANCY, enabled=False, threshold=0)


class TestSizeIsNotInTheDefaultScore:
    """Size measures REWARD (how much you reclaim), not RISK (how unlikely it is to be
    watched). A 50 GB file is a 4K blockbuster -- big files are big *because* they are
    popular. Including size condemned the most-watched content in the library and
    produced -50% lift."""

    def test_size_carries_no_weight_by_default(self) -> None:
        weights = {s.signal: s.weight for s in DEFAULT_MOVIE_POLICY.signals}

        assert SignalId.SIZE not in weights

    def test_dormancy_dominates_the_default_score(self) -> None:
        weights = {s.signal: s.weight for s in DEFAULT_MOVIE_POLICY.signals}
        total = sum(weights.values())

        assert weights[SignalId.UNWATCHED] / total > 0.6

    def test_the_dormancy_signal_matches_the_measured_curve(self) -> None:
        """Floor at 365 (below which ~a third of films come back); saturating at 1825
        (beyond which the rate is 2%)."""
        unwatched = next(s for s in DEFAULT_MOVIE_POLICY.signals if s.signal is SignalId.UNWATCHED)

        assert unwatched.floor == 365
        assert unwatched.saturate_at == 1825

    def test_the_default_policy_gates_on_dormancy(self) -> None:
        gate = next(g for g in DEFAULT_MOVIE_POLICY.gates if g.gate is GateId.MIN_DORMANCY)

        assert gate.enabled
        assert gate.threshold == 1095


class TestTheRewatchPrior:
    """The measured ground truth. The most useful table in the project."""

    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (100, 0.61),
            (400, 0.31),
            (600, 0.32),
            (900, 0.30),
            (1200, 0.19),
            (2000, 0.13),
        ],
    )
    def test_the_curve(self, days: int, expected: float) -> None:
        assert rewatch_prior(days) == expected

    def test_it_falls_with_age(self) -> None:
        """Older means less likely to be watched. If this ever inverts, the data has
        changed and every default in the engine needs re-deriving."""
        assert rewatch_prior(100) > rewatch_prior(1200) > rewatch_prior(2000)

    def test_there_is_no_cliff(self) -> None:
        """The finding that reframes the whole product.

        A film dormant for FIVE YEARS still has a 13% chance of being watched next
        year. Deletion is never free on an active library -- there is only cheaper and
        dearer. Any tool claiming a five-year-old file is safe to remove is lying.

        This is why the grace period and the human approval gate are not decoration."""
        assert rewatch_prior(2000) > 0.10


class TestLift:
    """The number that decides a signal's fate.

    Positive: the scorer beats an age-matched coin-flip.
    Negative: the signals are WORSE THAN NOTHING and must not ship.
    """

    def _result(self, *, condemned: int, regrets: int, dormancy: float) -> BacktestResult:
        result = BacktestResult(cutoff=utcnow(), condemn_at=60)
        result.condemned = [(f"Film {i}", 80.0, 1) for i in range(condemned)]
        result.condemned_dormancy = [dormancy] * condemned
        result.regrets = [_regret(f"Film {i}") for i in range(regrets)]
        return result

    def test_a_scorer_that_beats_its_age_group_has_positive_lift(self) -> None:
        # 1,200 days dormant => 19% expected. We regret only 5 of 100 => 5%.
        result = self._result(condemned=100, regrets=5, dormancy=1200)

        assert result.expected_regret_rate == pytest.approx(0.19)
        assert result.regret_rate == pytest.approx(0.05)
        assert result.lift > 0
        assert result.beats_random is True

    def test_a_scorer_worse_than_its_age_group_has_negative_lift(self) -> None:
        """Selecting films MORE likely to be watched than their age implies. Not a
        tuning problem -- a broken scorer, and it must not ship."""
        result = self._result(condemned=100, regrets=30, dormancy=1200)  # 19% expected

        assert result.lift < 0
        assert result.beats_random is False

    def test_the_summary_refuses_to_endorse_a_negative_lift_policy(self) -> None:
        result = self._result(condemned=100, regrets=30, dormancy=1200)

        assert "NOT BEATING AGE ALONE" in result.summary()

    def test_the_summary_endorses_a_positive_lift_policy(self) -> None:
        result = self._result(condemned=100, regrets=5, dormancy=1200)

        assert "earns its keep" in result.summary()


def _regret(title: str) -> Regret:
    return Regret(
        title=title,
        watched_by="someone",
        watched_at=utcnow(),
        days_after_cutoff=100,
        size_bytes=1,
        score=80.0,
    )
