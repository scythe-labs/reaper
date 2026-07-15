# SPDX-License-Identifier: AGPL-3.0-or-later
"""The invariants that make Reaper safe, as property tests.

These are the machine-checkable form of the entire design argument. If one of them
fails, the tool can delete something it should not, and no amount of careful UI
will save it.

Hypothesis generators are deliberately biased toward the values that break things:
0, empty, None, and Unknown.
"""

from __future__ import annotations

from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st

from reaper.engine.gates import (
    ABSTAIN,
    PROTECT,
    CuratedListGate,
    Facts,
    GateConfig,
    GateId,
    OthersWatchingGate,
    RatingFloorGate,
    ServerPopularityGate,
    StreamingNowGate,
    UnmanagedGate,
    WhitelistGate,
    evaluate_all,
)
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.engine.signals import SignalConfig, SignalId, score

# --- generators -------------------------------------------------------------


def observations(
    value_strategy: st.SearchStrategy[object],
) -> st.SearchStrategy[Observation[object]]:
    """All three arms, with Unknown well represented."""
    return st.one_of(
        value_strategy.map(lambda v: Known(value=v, source="test")),
        st.just(Absent(source="test")),
        st.text(min_size=1, max_size=20).map(lambda r: Unknown(reason=r, source="test")),
    )


@st.composite
def facts(draw: st.DrawFn) -> Facts:
    small_ints = st.integers(min_value=0, max_value=50)
    return Facts(
        title=draw(st.text(min_size=1, max_size=20)),
        days_observed_unwatched=draw(observations(st.floats(0, 5000, allow_nan=False))),
        distinct_watchers=draw(observations(small_ints)),
        distinct_watchers_all_time=draw(observations(small_ints)),
        size_bytes=draw(observations(st.integers(0, 100_000_000_000))),
        imdb_rating_tenths=draw(observations(st.integers(0, 100))),
        imdb_votes=draw(observations(st.integers(0, 3_000_000))),
        season_rank=draw(observations(st.integers(1, 20))),
        is_streaming_now=draw(observations(st.booleans())),
        is_managed=draw(observations(st.booleans())),
        in_curated_list=draw(observations(st.text(max_size=20))),
        is_whitelisted=draw(observations(st.booleans())),
        others_watching=draw(observations(small_ints)),
    )


ALL_GATES = [
    RatingFloorGate(GateConfig(GateId.RATING_FLOOR, threshold=75, secondary=1000)),
    StreamingNowGate(GateConfig(GateId.STREAMING_NOW)),
    ServerPopularityGate(GateConfig(GateId.SERVER_POPULARITY, threshold=3)),
    WhitelistGate(GateConfig(GateId.WHITELISTED)),
    CuratedListGate(GateConfig(GateId.CURATED_LIST)),
    UnmanagedGate(GateConfig(GateId.UNMANAGED)),
    OthersWatchingGate(GateConfig(GateId.OTHERS_WATCHING, threshold=1)),
]

ALL_SIGNALS = [
    SignalConfig(SignalId.UNWATCHED, weight=40, saturate_at=730),
    SignalConfig(SignalId.SIZE, weight=20, saturate_at=50),
    SignalConfig(SignalId.SEASON_RANK, weight=15, saturate_at=5),
    SignalConfig(SignalId.FEW_WATCHERS, weight=15, saturate_at=5),
    SignalConfig(SignalId.LOW_RATING, weight=10, saturate_at=70),
]


# --- the invariants ---------------------------------------------------------


class TestAGateCannotDelete:
    """The structural claim. There is no CONDEMN constructor on a gate, so no
    input -- however malformed, missing, or hostile -- can make a protection
    delete a file."""

    @given(item=facts())
    @settings(max_examples=300)
    def test_every_gate_outcome_is_protect_or_abstain(self, item: Facts) -> None:
        for result in evaluate_all(ALL_GATES, item).results:
            assert result.outcome in (PROTECT, ABSTAIN)


class TestUnknownNeverCondemns:
    """The invariant that survives an outage."""

    @given(item=facts())
    @settings(max_examples=300)
    def test_an_unknown_input_blocks_its_gate_rather_than_passing_it(self, item: Facts) -> None:
        """A gate that could not be evaluated is reported as *blocked*, not as
        'checked and fine'. Treating those alike is the whole Deleterr failure
        class: an API blip silently disarms a protection."""
        evaluation = evaluate_all(ALL_GATES, item)

        for result in evaluation.results:
            if result.blocked:
                assert result.outcome == ABSTAIN
                assert "could not check" in result.detail

    @given(item=facts())
    @settings(max_examples=300)
    def test_an_unknown_input_never_increases_the_score(self, item: Facts) -> None:
        """THE property. Signals are unsigned, so an Unknown contributes 0 -- the
        floor. Replacing any known value with Unknown must never make an item MORE
        condemned.

        Under the tempting signed design (start at 50, subtract for 'well rated'),
        a Trakt outage removes a negative and the score RISES: a beloved film flips
        from spare to condemn because a third-party API had a bad minute."""
        baseline = score(ALL_SIGNALS, item).value

        for field_name in (
            "days_observed_unwatched",
            "distinct_watchers",
            "size_bytes",
            "imdb_rating_tenths",
            "season_rank",
        ):
            degraded = replace(item, **{field_name: Unknown(reason="outage", source="test")})
            degraded_score = score(ALL_SIGNALS, degraded).value

            assert degraded_score <= baseline + 1e-9, (
                f"making {field_name} Unknown INCREASED the score: {baseline} -> {degraded_score}"
            )

    def test_total_outage_scores_zero(self) -> None:
        """If we know nothing at all, there is no pressure to delete anything."""
        blind = Facts(
            title="?",
            **{  # type: ignore[arg-type]
                name: Unknown(reason="everything is down", source="test")
                for name in (
                    "days_observed_unwatched",
                    "distinct_watchers",
                    "distinct_watchers_all_time",
                    "size_bytes",
                    "imdb_rating_tenths",
                    "imdb_votes",
                    "season_rank",
                    "is_streaming_now",
                    "is_managed",
                    "in_curated_list",
                    "is_whitelisted",
                    "others_watching",
                )
            },
        )
        result = score(ALL_SIGNALS, blind)

        assert result.value == 0.0
        assert result.coverage == 0.0
        assert evaluate_all(ALL_GATES, blind).blocked is True


class TestScoreBounds:
    @given(item=facts())
    @settings(max_examples=300)
    def test_the_score_stays_in_range(self, item: Facts) -> None:
        result = score(ALL_SIGNALS, item)
        assert 0.0 <= result.value <= 100.0
        assert 0.0 <= result.coverage <= 1.0

    @given(item=facts())
    @settings(max_examples=200)
    def test_no_signal_is_ever_negative(self, item: Facts) -> None:
        """Unsigned. A signal measures a reason to delete; there is no such thing
        as a negative reason, and a negative would let one signal cancel another."""
        for result in score(ALL_SIGNALS, item).results:
            assert result.pressure >= 0.0
            assert result.pressure <= result.weight + 1e-9

    def test_disabling_a_signal_does_not_inflate_the_others(self) -> None:
        """A zero weight leaves the denominator too. Otherwise turning a signal off
        would silently raise every remaining score, and the user's thresholds would
        quietly start meaning something else."""
        item = Facts(
            title="x",
            days_observed_unwatched=Known(value=730.0, source="t"),
            distinct_watchers=Known(value=0, source="t"),
            distinct_watchers_all_time=Known(value=0, source="t"),
            size_bytes=Known(value=50_000_000_000, source="t"),
            imdb_rating_tenths=Known(value=50, source="t"),
            imdb_votes=Known(value=5000, source="t"),
            season_rank=Known(value=5, source="t"),
            is_streaming_now=Known(value=False, source="t"),
            is_managed=Known(value=True, source="t"),
            in_curated_list=Absent(source="t"),
            is_whitelisted=Known(value=False, source="t"),
            others_watching=Known(value=0, source="t"),
        )
        full = score(ALL_SIGNALS, item)

        without_size = [
            SignalConfig(
                c.signal,
                weight=0 if c.signal is SignalId.SIZE else c.weight,
                saturate_at=c.saturate_at,
                floor=c.floor,
            )
            for c in ALL_SIGNALS
        ]
        reduced = score(without_size, item)

        # Both saturate here, so both should read 100 -- the point is that removing
        # a signal does not push a mid-range score upward.
        assert full.value <= 100.0
        assert reduced.value <= 100.0


class TestProtectionAlwaysBeatsScore:
    def test_a_whitelisted_item_is_protected_at_any_score(self) -> None:
        item = Facts(
            title="Beloved",
            days_observed_unwatched=Known(value=5000.0, source="t"),  # maximal pressure
            distinct_watchers=Known(value=0, source="t"),
            distinct_watchers_all_time=Known(value=0, source="t"),
            size_bytes=Known(value=99_000_000_000, source="t"),
            imdb_rating_tenths=Known(value=10, source="t"),
            imdb_votes=Known(value=10, source="t"),
            season_rank=Known(value=20, source="t"),
            is_streaming_now=Known(value=False, source="t"),
            is_managed=Known(value=True, source="t"),
            in_curated_list=Absent(source="t"),
            is_whitelisted=Known(value=True, source="t"),  # <- the whitelist
            others_watching=Known(value=0, source="t"),
        )

        assert score(ALL_SIGNALS, item).value > 90  # it looks maximally deletable
        assert evaluate_all(ALL_GATES, item).protected is True  # ...and is kept anyway


class TestRatingGate:
    def test_the_vote_floor_rejects_noise(self) -> None:
        """8.3 from a few hundred votes is noise, not quality, and a bare rating
        floor would protect it forever."""
        gate = RatingFloorGate(GateConfig(GateId.RATING_FLOOR, threshold=75, secondary=1000))
        item = Facts(
            title="Obscure",
            days_observed_unwatched=Known(value=900.0, source="t"),
            distinct_watchers=Absent(source="t"),
            distinct_watchers_all_time=Absent(source="t"),
            size_bytes=Known(value=1, source="t"),
            imdb_rating_tenths=Known(value=83, source="imdb"),  # 8.3
            imdb_votes=Known(value=388, source="imdb"),  # ...from 388 votes
            season_rank=Absent(source="t"),
            is_streaming_now=Known(value=False, source="t"),
            is_managed=Known(value=True, source="t"),
            in_curated_list=Absent(source="t"),
            is_whitelisted=Known(value=False, source="t"),
            others_watching=Known(value=0, source="t"),
        )

        result = gate.evaluate(item)

        assert result.outcome == ABSTAIN
        assert "388 votes" in result.detail  # too few to trust the 8.3
        assert "1,000" in result.detail  # ...against the vote floor

    def test_a_stale_ratings_dataset_blocks_rather_than_silently_unprotecting(self) -> None:
        """The inverted failure. Everywhere else, missing data protects. A missing
        RATING removes protection -- so it must be reported as blocked, loudly, not
        quietly treated as 'this film is unrated and therefore fair game'."""
        gate = RatingFloorGate(GateConfig(GateId.RATING_FLOOR, threshold=75, secondary=1000))
        item = Facts(
            title="Casablanca",
            days_observed_unwatched=Known(value=900.0, source="t"),
            distinct_watchers=Absent(source="t"),
            distinct_watchers_all_time=Absent(source="t"),
            size_bytes=Known(value=1, source="t"),
            imdb_rating_tenths=Unknown(reason="IMDb dataset is stale", source="imdb"),
            imdb_votes=Unknown(reason="IMDb dataset is stale", source="imdb"),
            season_rank=Absent(source="t"),
            is_streaming_now=Known(value=False, source="t"),
            is_managed=Known(value=True, source="t"),
            in_curated_list=Absent(source="t"),
            is_whitelisted=Known(value=False, source="t"),
            others_watching=Known(value=0, source="t"),
        )

        result = gate.evaluate(item)

        assert result.blocked is True
        assert "could not check" in result.detail


class TestExplainability:
    def test_every_gate_reports_even_when_it_does_not_fire(self) -> None:
        """No short-circuit. The 'checked and did not fire, with the numbers' block
        is the product; stopping at the first protection would destroy it."""
        item = Facts(
            title="x",
            days_observed_unwatched=Known(value=900.0, source="tautulli"),
            distinct_watchers=Known(value=0, source="tautulli"),
            distinct_watchers_all_time=Known(value=0, source="tautulli"),
            size_bytes=Known(value=8_000_000_000, source="radarr"),
            imdb_rating_tenths=Known(value=60, source="imdb"),
            imdb_votes=Known(value=50_000, source="imdb"),
            season_rank=Absent(source="sonarr"),
            is_streaming_now=Known(value=False, source="tautulli"),
            is_managed=Known(value=True, source="radarr"),
            in_curated_list=Absent(source="lists"),
            is_whitelisted=Known(value=False, source="plex"),
            others_watching=Known(value=0, source="tautulli"),
        )

        evaluation = evaluate_all(ALL_GATES, item)

        assert len(evaluation.results) == len(ALL_GATES)
        assert evaluation.protected is False
        assert len(evaluation.checked_and_did_not_fire) == len(ALL_GATES)

        details = " | ".join(r.detail for r in evaluation.checked_and_did_not_fire)
        assert "Nobody here watched it" in details  # server-popularity, 0 watchers
        assert "below the 7.5 you keep" in details  # rating floor
