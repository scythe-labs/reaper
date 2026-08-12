# SPDX-License-Identifier: AGPL-3.0-or-later
"""The signal-quality corrections.

Every case here comes from discovering, by replaying real watch history, that the first
scorer was **worse than useless** -- it selected films *more* likely to be watched than a
random film of the same age (-50% lift). The instrument that measured it is gone; the finding
is in ``docs/SIGNALS.md`` and the corrections are here.

See docs/SIGNALS.md and docs/LEARNINGS.md.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

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
from reaper.engine.signals import SignalConfig, SignalId, evaluate_signal


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
    """Rank 1 is the most recent season *with files on disk*
    (``clients.sonarr_stats.rank_seasons``). The signal used to describe every ranked
    season as "an older season", so the newest season a show has was shown to the owner
    as an old one, in the same breath as the pressure being charged against it."""

    def test_rank_one_is_the_newest_season(self) -> None:
        result = evaluate_signal(SEASON_SIGNAL, _ranked(1))

        assert result.detail == "the newest season on disk"
        assert "older" not in result.detail

    def test_the_rest_of_the_ramp_counts_back_in_order(self) -> None:
        assert (
            evaluate_signal(SEASON_SIGNAL, _ranked(2)).detail == "the second-newest season on disk"
        )
        assert (
            evaluate_signal(SEASON_SIGNAL, _ranked(3)).detail == "the third-newest season on disk"
        )
        assert evaluate_signal(SEASON_SIGNAL, _ranked(8)).detail == "the 8th-newest season on disk"

    def test_the_newest_season_still_carries_the_pressure_it_always_did(self) -> None:
        """The wording changed, not a number. The rank still ramps as it did, which is
        exactly why the wording had to stop contradicting it."""
        newest = evaluate_signal(SEASON_SIGNAL, _ranked(1))
        oldest = evaluate_signal(SEASON_SIGNAL, _ranked(8))

        assert newest.pressure < oldest.pressure
        assert 0.0 <= newest.pressure <= newest.weight

    def test_an_unreadable_rank_claims_nothing(self) -> None:
        result = evaluate_signal(SEASON_SIGNAL, _ranked(None))

        assert result.detail == "could not tell which season this is"
        assert result.evaluated is False
        assert result.pressure == 0.0


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

    @pytest.mark.parametrize(
        ("days", "protects"),
        [(1094, True), (1095, False), (1096, False)],
        ids=["a-day-short-of-the-floor", "exactly-at-the-floor", "a-day-past-it"],
    )
    def test_the_floor_is_the_first_day_the_gate_stops_protecting(
        self, days: int, protects: bool
    ) -> None:
        """The boundary itself, which 400 and 1500 leave 695 days away on either side.

        ``dormant < floor`` protects, so a title dormant for exactly the floor is the first
        one the gate lets go. Nothing drove a value near it, which left the comparison free
        to become ``<=`` and hold that title back for another day -- the keep direction, so
        no file was ever at risk, but this is the gate its own docstring calls the one a
        weight cannot outvote, and an undefended boundary on it is one refactor from
        mattering. Sits here rather than in an issue because the demonstration was cheap
        and the test is three lines (#242).
        """
        result = GATE.evaluate(_facts(days))

        assert (result.outcome == PROTECT) is protects

    def test_dormancy_that_is_genuinely_absent_keeps_the_file_rather_than_raising(self) -> None:
        """The one arm of this gate no fact builder can currently reach, tested anyway.

        Both builders emit ``Known`` or ``Unknown`` for this field, so nothing in a scan
        produces the ``Absent`` below, and ``_facts(None)`` above yields ``Unknown``, which
        ``_blocked`` answers first with a different hold. The guard is still load-bearing:
        delete it and the gate falls through to ``dormant.value``, which raises
        ``AttributeError`` on an ``Absent`` and takes the item's whole verdict with it. Rule 118
        -- an unreachable tripwire with no test is one refactor from silently becoming a
        permissive ABSTAIN -- and it is driven against the gate directly because the builders
        offer no route to it.

        PROTECT rather than blocked is the point of the assertion. ``Absent`` is "we looked and
        there is genuinely no watch history", a state we can describe, where ``Unknown`` is "we
        could not look" (rule 93); either way we cannot establish that it has sat long enough,
        so the file stays.
        """
        facts = replace(_facts(400), days_observed_unwatched=Absent(source="tautulli"))

        result = GATE.evaluate(facts)

        assert result.outcome == PROTECT
        assert result.blocked is False
        assert "dormancy cannot be established" in result.detail

    def test_a_gigantic_low_rated_film_is_still_protected_if_it_is_too_recent(
        self,
    ) -> None:
        """A gate does not care about the score. This item is 50 GB, rated 6.0, watched
        by nobody -- it would score near the top under any weighting. It is still 400
        days dormant, so it is spared regardless."""
        result = GATE.evaluate(_facts(400))

        assert result.outcome == PROTECT

    def test_unknown_dormancy_blocks_rather_than_condemns(self) -> None:
        result = GATE.evaluate(_facts(None))

        assert result.outcome == ABSTAIN
        assert result.blocked is True

    def test_the_floor_refuses_a_value_below_five_days(self) -> None:
        """A small fat-finger guard: to remove things faster than 5 days, turn the
        protection off rather than setting the threshold near zero."""
        with pytest.raises(ValidationError, match="at least 5 days"):
            GateSetting(gate=GateId.MIN_DORMANCY, threshold=4)
        assert GateSetting(gate=GateId.MIN_DORMANCY, threshold=5)

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
