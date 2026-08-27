# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests the signal-quality corrections.

These tests pin corrected behavior for signals and gates where an earlier version
measurably hurt the scorer, favoring films people were more likely to watch over ones
they were not. See docs/SIGNALS.md and docs/LEARNINGS.md for the measurements behind
each one.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from reaper.engine.gates import (
    ABSTAIN,
    PROTECT,
    REWATCH_BLOCK_FLOOR_N,
    Facts,
    GateConfig,
    GateId,
    MediaKind,
    MinDormancyGate,
    RewatchOddsGate,
    wilson_upper,
)
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.engine.policy import DEFAULT_MOVIE_POLICY, GateSetting
from reaper.engine.signals import SignalConfig, SignalId, evaluate_signal
from tests._reasons import text


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
    )


GATE = MinDormancyGate(GateConfig(threshold=1095))

SEASON_SIGNAL = SignalConfig(SignalId.SEASON_RANK, weight=15, saturate_at=5)


def _ranked(rank: int | None) -> Facts:
    """A show's season, at a given place counting back from the newest one on disk."""
    season = (
        Known(value=rank, source="sonarr")
        if rank is not None
        else Unknown(reason="Sonarr unreachable", source="sonarr")
    )
    return replace(_facts(900), season_rank=season)


class TestTheSeasonRankSignalNamesTheSeasonItMeans:
    """Rank 1 is the most recent season with files on disk
    (``clients.sonarr_stats.rank_seasons``). The wording must never call the newest
    season an owner has "an older season" while also scoring it toward deletion.
    """

    def test_rank_one_is_the_newest_season(self) -> None:
        result = evaluate_signal(SEASON_SIGNAL, _ranked(1))

        assert text(result.detail) == "the newest season on disk"
        assert "older" not in text(result.detail)

    def test_the_rest_of_the_ramp_counts_back_in_order(self) -> None:
        assert (
            text(evaluate_signal(SEASON_SIGNAL, _ranked(2)).detail)
            == "the second-newest season on disk"
        )
        assert (
            text(evaluate_signal(SEASON_SIGNAL, _ranked(3)).detail)
            == "the third-newest season on disk"
        )
        assert (
            text(evaluate_signal(SEASON_SIGNAL, _ranked(8)).detail)
            == "the 8th-newest season on disk"
        )

    def test_the_newest_season_still_carries_the_pressure_it_always_did(self) -> None:
        """Only the wording changed, not the score. The rank still raises the deletion
        score exactly as before, and the wording must never contradict that.
        """
        newest = evaluate_signal(SEASON_SIGNAL, _ranked(1))
        oldest = evaluate_signal(SEASON_SIGNAL, _ranked(8))

        assert newest.pressure < oldest.pressure
        assert 0.0 <= newest.pressure <= newest.weight

    def test_an_unreadable_rank_claims_nothing(self) -> None:
        result = evaluate_signal(SEASON_SIGNAL, _ranked(None))

        assert text(result.detail) == "could not tell which season this is"
        assert result.evaluated is False
        assert result.pressure == 0.0


class TestTheMinDormancyGate:
    """A hard gate, not a signal weight. A weight can be outvoted by other signals, which
    is exactly the failure this gate exists to prevent: letting enough other pressure
    condemn a film that is still likely to be watched again.
    """

    def test_a_film_dormant_less_than_the_floor_is_protected(self) -> None:
        """At 400 days dormant, the measured rewatch probability is about 31%. Deleting a
        film at those odds would be close to a coin flip on whether a real person watches
        it again.
        """
        result = GATE.evaluate(_facts(400))

        assert result.outcome == PROTECT
        # The floor of 1095 days reads as "3 years". The wording says "unwatched", never
        # "last watched", since a never-played item's clock runs from when it arrived, not
        # from a play that never happened.
        assert "unwatched" in text(result.detail)
        assert "3 years" in text(result.detail)

    def test_a_film_past_the_floor_is_not_protected_by_this_gate(self) -> None:
        """Beyond three years the rewatch rate drops to 8%, and past five to 2%."""
        result = GATE.evaluate(_facts(1500))

        assert result.outcome == ABSTAIN
        assert "Unwatched for" in text(result.detail)
        assert "3 years" in text(result.detail)  # the floor, humanized

    @pytest.mark.parametrize(
        ("days", "protects"),
        [(1094, True), (1095, False), (1096, False)],
        ids=["a-day-short-of-the-floor", "exactly-at-the-floor", "a-day-past-it"],
    )
    def test_the_floor_is_the_first_day_the_gate_stops_protecting(
        self, days: int, protects: bool
    ) -> None:
        """The boundary itself, which 400 and 1500 leave 695 days away on either side.

        ``dormant < floor`` protects, so a title dormant for exactly the floor is the
        first one the gate lets go. If the comparison ever became ``<=`` instead, it
        would hold that title back for one more day, which is the keep direction and
        would never put a file at risk. But this gate is the one a weight cannot outvote,
        so an undefended boundary on it is one refactor away from mattering.
        """
        result = GATE.evaluate(_facts(days))

        assert (result.outcome == PROTECT) is protects

    def test_dormancy_that_is_genuinely_absent_keeps_the_file_rather_than_raising(self) -> None:
        """The one arm of this gate no fact builder can currently reach, tested anyway.

        Both builders emit ``Known`` or ``Unknown`` for this field, so nothing in a scan
        produces the ``Absent`` below. ``_facts(None)`` above yields ``Unknown``, which
        ``_blocked`` answers first with a different hold. The guard is still load-bearing.
        Delete it, and the gate falls through to ``dormant.value``, which raises
        ``AttributeError`` on an ``Absent`` and takes the item's whole verdict down with
        it. This is driven against the gate directly, since the builders offer no route to
        it.

        PROTECT, rather than blocked, is the point of the assertion. ``Absent`` means "we
        looked and there is genuinely no watch history", a state that can be described.
        ``Unknown`` means "we could not look". Either way, dormancy cannot be established
        long enough to condemn the file, so it stays.
        """
        facts = replace(_facts(400), days_observed_unwatched=Absent(source="tautulli"))

        result = GATE.evaluate(facts)

        assert result.outcome == PROTECT
        assert result.blocked is False
        assert "dormancy cannot be established" in text(result.detail)

    def test_a_gigantic_low_rated_film_is_still_protected_if_it_is_too_recent(
        self,
    ) -> None:
        """A gate does not look at the score at all. This item is 50 GB, rated 6.0, and
        watched by nobody, so it would score near the top under any weighting. It is still
        400 days dormant, so it is spared regardless.
        """
        result = GATE.evaluate(_facts(400))

        assert result.outcome == PROTECT

    def test_unknown_dormancy_blocks_rather_than_condemns(self) -> None:
        result = GATE.evaluate(_facts(None))

        assert result.outcome == ABSTAIN
        assert result.blocked is True

    def test_the_floor_refuses_a_value_below_five_days(self) -> None:
        """Guards against a typo. To remove things faster than 5 days, an operator must
        turn the protection off entirely, rather than setting the threshold near zero."""
        with pytest.raises(ValidationError, match="at least 5 days"):
            GateSetting(gate=GateId.MIN_DORMANCY, threshold=4)
        assert GateSetting(gate=GateId.MIN_DORMANCY, threshold=5)

    def test_disabling_it_is_allowed_but_must_be_explicit(self) -> None:
        """Turning the protection off must be a deliberate decision, not an accident. An
        operator may disable it outright, but may not disable it by quietly setting the
        threshold to zero.
        """
        assert GateSetting(gate=GateId.MIN_DORMANCY, enabled=False, threshold=0)


def _cohort(n: Observation[int], k: Observation[int]) -> Facts:
    """A dormancy-matched Facts, varying only the two cohort observations
    ``RewatchOddsGate`` reads. Built off ``_facts`` so every other field stays readable and
    cannot be the reason a case abstains."""
    return replace(_facts(900), rewatch_cohort_n=n, rewatch_cohort_k=k)


class TestTheRewatchOddsGate:
    """An opt-in protection. It keeps anything whose dormancy cohort gets watched again at
    or above the operator's percentage, compared against the Wilson 95% upper bound of the
    cohort's rate rather than its point estimate.
    """

    def test_fires_when_the_upper_bound_clears_the_threshold(self) -> None:
        """This case uses n=50, k=20, a 40% point rate against a 35% threshold, not the
        gate's shipped 25% default. That is comfortably inside both the point rate and the
        bound.
        """
        facts = _cohort(Known(value=50, source="fit"), Known(value=20, source="fit"))

        result = RewatchOddsGate(GateConfig(threshold=35)).evaluate(facts)

        assert result.outcome == PROTECT
        assert "20 of 50" in text(result.detail)

    def test_fires_on_the_upper_bound_even_when_the_point_rate_is_under_the_threshold(
        self,
    ) -> None:
        """Pins the small-library design explicitly. n=30, k=6 is a 20% point rate, under
        a 35% threshold, but the Wilson upper bound of that cohort is about 37.3%, which
        clears it. A gate comparing the point rate alone would abstain here. Comparing the
        upper bound protects instead, so a small library never loses protection to
        sampling noise.
        """
        assert 100 * 6 / 30 < 35  # the point rate does NOT clear the threshold...
        assert wilson_upper(6, 30) * 100 > 35  # ...but the upper bound does

        facts = _cohort(Known(value=30, source="fit"), Known(value=6, source="fit"))

        result = RewatchOddsGate(GateConfig(threshold=35)).evaluate(facts)

        assert result.outcome == PROTECT

    def test_a_zero_of_thirty_cohort_does_not_claim_it_keeps_getting_watched(self) -> None:
        """0 of 30 measured still clears a 10% floor on the Wilson upper bound alone
        (about 11%, ``gates.wilson_upper``), so the gate protects. But the sentence must
        not claim these titles "keep getting watched" when not one of the 30 measured did.
        It quotes the bound that ``RewatchOddsGate.evaluate`` actually compared, with the
        raw count alongside it, never the raw rate standing in for a claim.
        """
        facts = _cohort(Known(value=30, source="fit"), Known(value=0, source="fit"))
        assert wilson_upper(0, 30) * 100 >= 10  # the bound clears the floor...
        assert 100 * 0 / 30 < 10  # ...though the point rate does not

        result = RewatchOddsGate(GateConfig(threshold=10)).evaluate(facts)

        assert result.outcome == PROTECT
        rendered = text(result.detail)
        assert "keep getting watched" not in rendered, rendered
        assert "0 of 30" in rendered, rendered
        assert "11%" in rendered, rendered

    def test_the_abstain_sentence_never_reaches_the_floor_it_is_under(self) -> None:
        """3 of 31 at a 25% floor abstains on a 24.9 bound, but plain rounding would
        render "25%, under the 25% you keep", a number claimed to be under itself.
        ``RewatchOddsGate.evaluate`` clamps the displayed bound below the floor. The
        decision itself is still made on the unrounded value.
        """
        facts = _cohort(Known(value=31, source="fit"), Known(value=3, source="fit"))
        raw = wilson_upper(3, 31) * 100
        assert raw < 25  # abstains...
        assert round(raw) == 25  # ...but unclamped rounding reaches the floor

        result = RewatchOddsGate(GateConfig(threshold=25)).evaluate(facts)

        assert result.outcome == ABSTAIN
        rendered = text(result.detail)
        assert "24%" in rendered, rendered
        assert "under the 25%" in rendered, rendered

    def test_a_cohort_under_the_floor_abstains_whatever_the_threshold(self) -> None:
        """One title under ``REWATCH_BLOCK_FLOOR_N`` is too few to trust, whether the
        operator's percentage is barely above zero or almost the whole scale."""
        facts = _cohort(
            Known(value=REWATCH_BLOCK_FLOOR_N - 1, source="fit"),
            Known(value=REWATCH_BLOCK_FLOOR_N - 1, source="fit"),
        )

        for threshold in (1, 99):
            result = RewatchOddsGate(GateConfig(threshold=threshold)).evaluate(facts)
            assert result.outcome == ABSTAIN
            assert text(result.detail) == "Too few titles like this to say."

    def test_an_unknown_cohort_abstains_without_blocking(self) -> None:
        """The one documented deviation from every other gate's fail-closed ``_blocked``
        arm, and the gate's own docstring says so. Items whose history is genuinely
        unreadable are already blocked by the dormancy and popularity gates reading the
        same sources, so failing quietly here removes no protection.
        """
        facts = _cohort(
            Unknown(reason="the fit could not read this item", source="fit"),
            Unknown(reason="the fit could not read this item", source="fit"),
        )

        result = RewatchOddsGate(GateConfig(threshold=25)).evaluate(facts)

        assert result.outcome == ABSTAIN
        assert result.blocked is False

    def test_an_absent_cohort_does_not_apply(self) -> None:
        """Covers the season lane, and any hand-built Facts. The fit ships no TV answer,
        so there is genuinely nothing to compare, an ``Absent`` rather than a failed read.
        ``_facts`` leaves both cohort fields at their default, which is ``Absent``.
        """
        result = RewatchOddsGate(GateConfig(threshold=25)).evaluate(_facts(900))

        assert result.outcome == ABSTAIN
        assert text(result.detail) == "Does not apply here."

    def test_the_boundary_is_inclusive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``wilson_upper(k, n) * 100 >= floor``. A bound landing exactly on the operator's
        percentage still fires, not only one strictly above it. The real function's inputs
        never land on a round percentage, so this test pins what it returns instead of
        hunting for a coincidental (k, n) that would.
        """
        monkeypatch.setattr("reaper.engine.gates.wilson_upper", lambda k, n: 0.5)
        facts = _cohort(Known(value=40, source="fit"), Known(value=20, source="fit"))

        result = RewatchOddsGate(GateConfig(threshold=50)).evaluate(facts)

        assert result.outcome == PROTECT

    @pytest.mark.parametrize(("media_type", "noun"), [("movie", "titles"), ("season", "shows")])
    def test_every_cohort_reason_names_the_media_type_the_policy_scans(
        self, media_type: MediaKind, noun: str
    ) -> None:
        """All four Reasons this gate emits off the cohort say what is being compared, and
        a TV policy compares shows, not titles.

        Both lanes are swept because a branch hardcoding either noun would still pass a
        test that only checks one value.

        The season lane is the half that discriminates here. ``reaper.i18n.format_icu``
        falls a select with no param back to its ``other`` branch, so a Reason that dropped
        ``mediaType`` still renders "titles", and the movie row passes either way. i18next
        does not fall back. It prints the raw template instead, and that half is pinned on
        the frontend, in ``why.test.ts``'s matching block, which is what proves a stored
        row still renders real words.
        """
        other = "shows" if noun == "titles" else "titles"
        gate = RewatchOddsGate(GateConfig(threshold=35), media_type=media_type)
        unreadable = Unknown(reason="the fit could not read this item", source="fit")
        floor_n = Known(value=REWATCH_BLOCK_FLOOR_N - 1, source="fit")
        # n=100 k=10 is a 10% point rate whose Wilson upper bound is well under 35%, so the
        # last branch is reached rather than the PROTECT above it.
        under = _cohort(Known(value=100, source="fit"), Known(value=10, source="fit"))
        assert wilson_upper(10, 100) * 100 < 35

        sentences = {
            "rewatch_no_history": gate.evaluate(_cohort(unreadable, unreadable)).detail,
            "rewatch_thin": gate.evaluate(_cohort(floor_n, floor_n)).detail,
            "rewatch_watched_again": gate.evaluate(
                _cohort(Known(value=50, source="fit"), Known(value=20, source="fit"))
            ).detail,
            "rewatch_under_floor": gate.evaluate(under).detail,
        }

        for reason_id, detail in sentences.items():
            assert detail.id == reason_id
            assert f"{noun} like this" in text(detail), reason_id
            assert other not in text(detail), reason_id


class TestWilsonUpper:
    """The Wilson 95% upper bound of ``k/n``, read by the gate above and the Policy
    page's consequence echo (``api.policy``)."""

    def test_zero_successes_reduces_to_the_standard_closed_form(self) -> None:
        """At k=0, p=0, so ``p*(1-p)`` drops out. The center term is z^2/(2n), and the
        spread term reduces to the same z^2/(2n), summing to z^2/n over a denominator of
        1 + z^2/n, which is z^2 / (n + z^2), the textbook zero-count Wilson bound. This
        checks that closed form directly, independent of ``wilson_upper``'s own
        arithmetic.
        """
        z = 1.96
        n = 30

        assert wilson_upper(0, n) == pytest.approx(z * z / (n + z * z))

    def test_all_successes_reaches_the_top_of_the_scale(self) -> None:
        """At k=n, p=1, so ``p*(1-p)`` drops out again. The center term is 1 + z^2/(2n),
        and the spread term is the same z^2/(2n), summing to 1 + z^2/n over a denominator
        of 1 + z^2/n, which is exactly 1, whatever n is.
        """
        assert wilson_upper(40, 40) == pytest.approx(1.0)

    def test_a_hand_checked_value(self) -> None:
        """k=25, n=100, a 25% point rate, computed here by hand.

        z=1.96, z^2=3.8416, z^2/n=0.038416, denominator=1.038416
        center = 0.25 + 3.8416/200 = 0.269208
        spread = 1.96 * sqrt(0.25*0.75/100 + 3.8416/40000)
               = 1.96 * sqrt(0.001875 + 0.00009604) = 1.96 * 0.044396 = 0.087016
        upper = (0.269208 + 0.087016) / 1.038416 = 0.343...

        This matches the commonly cited Wilson interval for p=0.25, n=100, about 0.175 to
        0.343.
        """
        assert wilson_upper(25, 100) == pytest.approx(0.34301, abs=5e-5)

    def test_n_zero_is_defined_as_zero(self) -> None:
        assert wilson_upper(5, 0) == 0.0


class TestSizeIsNotInTheDefaultScore:
    """Size measures how much space is reclaimed, not how unlikely a file is to be
    watched again. A large file is often large because it is a popular 4K release, so
    scoring on size alone would target the most-watched content in the library.
    """

    def test_size_carries_no_weight_by_default(self) -> None:
        weights = {s.signal: s.weight for s in DEFAULT_MOVIE_POLICY.signals}

        assert SignalId.SIZE not in weights

    def test_dormancy_dominates_the_default_score(self) -> None:
        weights = {s.signal: s.weight for s in DEFAULT_MOVIE_POLICY.signals}
        total = sum(weights.values())

        assert weights[SignalId.UNWATCHED] / total > 0.6

    def test_the_dormancy_signal_matches_the_measured_curve(self) -> None:
        """Floor at 365, below which about a third of films come back. Saturates at
        1825, beyond which the rate is 2%.
        """
        unwatched = next(s for s in DEFAULT_MOVIE_POLICY.signals if s.signal is SignalId.UNWATCHED)

        assert unwatched.floor == 365
        assert unwatched.saturate_at == 1825

    def test_the_default_policy_gates_on_dormancy(self) -> None:
        gate = next(g for g in DEFAULT_MOVIE_POLICY.gates if g.gate is GateId.MIN_DORMANCY)

        assert gate.enabled
        assert gate.threshold == 1095
