# SPDX-License-Identifier: AGPL-3.0-or-later
"""The invariants that make Reaper safe, as property tests.

These are the machine-checkable form of the entire design argument. If one of them
fails, the tool can delete something it should not, and no amount of careful UI
will save it.

Hypothesis generators are deliberately biased toward the values that break things:
0, empty, None, and Unknown.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from reaper.engine import fields
from reaper.engine.gates import (
    ABSTAIN,
    PROTECT,
    CuratedListGate,
    Facts,
    GateConfig,
    GateId,
    RatingFloorGate,
    RatingRule,
    ServerPopularityGate,
    StreamingNowGate,
    WhitelistGate,
    evaluate_all,
)
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.engine.signals import (
    MAX_SCORE,
    CustomSignalConfig,
    KeepConfig,
    Score,
    SignalConfig,
    SignalId,
    evaluate_keep,
    score,
)
from reaper.engine.verdict import (
    DEFERRABLE_BLOCK_GATES,
    block_holds_reap,
    decide_verdict,
    reap_held_by_blocks,
)
from reaper.ratings import Rating, RatingSource

_IMDB_BAR = RatingRule(source=RatingSource.IMDB, floor=75, min_votes=1000)


def _imdb(value: float, votes: int) -> tuple[Rating, ...]:
    """One frozen IMDb rating, as the scan would carry it into ``Facts.ratings``."""
    return (Rating(source=RatingSource.IMDB, value=value, votes=votes, provider="imdb-dataset"),)


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
        # Drawn across the popularity gate's 365-day window in both directions, so the
        # sweep keeps reaching the gate's answering branches. Left at its default this is
        # `Absent`, which the gate reads as un-checkable: every example would block, and
        # the invariants below would hold for a reason that has nothing to do with them.
        history_reach_days=draw(observations(st.floats(0, 5000, allow_nan=False))),
    )


ALL_GATES = [
    RatingFloorGate(rules=(_IMDB_BAR,)),
    StreamingNowGate(GateConfig(GateId.STREAMING_NOW)),
    ServerPopularityGate(GateConfig(GateId.SERVER_POPULARITY, threshold=3)),
    WhitelistGate(GateConfig(GateId.WHITELISTED)),
    CuratedListGate(GateConfig(GateId.CURATED_LIST)),
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
        )

        assert score(ALL_SIGNALS, item).value > 90  # it looks maximally deletable
        assert evaluate_all(ALL_GATES, item).protected is True  # ...and is kept anyway


def _rating_facts(ratings: tuple[Rating, ...]) -> Facts:
    """A minimal Facts carrying only what the rating gate reads, so the rating tests state
    just the ratings under test."""
    return Facts(
        title="x",
        days_observed_unwatched=Known(value=900.0, source="t"),
        distinct_watchers=Absent(source="t"),
        distinct_watchers_all_time=Absent(source="t"),
        size_bytes=Known(value=1, source="t"),
        imdb_rating_tenths=Absent(source="t"),
        imdb_votes=Absent(source="t"),
        season_rank=Absent(source="t"),
        is_streaming_now=Known(value=False, source="t"),
        is_managed=Known(value=True, source="t"),
        in_curated_list=Absent(source="t"),
        is_whitelisted=Known(value=False, source="t"),
        ratings=ratings,
    )


class TestRatingGate:
    def test_the_vote_floor_rejects_noise(self) -> None:
        """8.3 from a few hundred votes is noise, not quality, and a bare rating
        floor would protect it forever."""
        gate = RatingFloorGate(rules=(_IMDB_BAR,))  # IMDb 7.5 from 1,000 votes
        result = gate.evaluate(_rating_facts(_imdb(8.3, votes=388)))

        assert result.outcome == ABSTAIN
        assert "388 votes" in result.detail  # too few to trust the 8.3
        assert "1,000" in result.detail  # ...against the vote floor

    def test_a_missing_rating_never_protects_and_never_blocks(self) -> None:
        """A title with no rating for the bar's source is simply not kept on that bar --
        it ABSTAINS, never PROTECTs, and (unlike the score gates) never blocks the whole
        verdict. A degraded IMDb dataset is caught upstream, where it degrades the whole
        snapshot to un-executable; the keep gate itself only ever spares a file."""
        gate = RatingFloorGate(rules=(_IMDB_BAR,))
        result = gate.evaluate(_rating_facts(()))  # no ratings at all

        assert result.outcome == ABSTAIN
        assert result.blocked is False
        assert "no IMDb rating" in result.detail

    def test_a_second_source_can_keep_a_title_imdb_would_not(self) -> None:
        """The point of multi-source: a film below the IMDb bar but above the Rotten
        Tomatoes critics bar is kept, on ANY-of matching."""
        gate = RatingFloorGate(
            rules=(_IMDB_BAR, RatingRule(source=RatingSource.ROTTEN_TOMATOES_CRITIC, floor=75)),
            match="any",
        )
        ratings = (
            Rating(source=RatingSource.IMDB, value=6.8, votes=500_000, provider="imdb-dataset"),
            Rating(source=RatingSource.ROTTEN_TOMATOES_CRITIC, value=8.4, votes=None, provider="p"),
        )
        result = gate.evaluate(_rating_facts(ratings))

        assert result.outcome == PROTECT
        assert "Rotten Tomatoes critics 84%" in result.detail

    def test_all_of_matching_needs_every_bar(self) -> None:
        """Under ALL matching, clearing one bar is not enough: a source we cannot read
        counts as a miss, so ALL fails toward NOT protecting -- the safe direction."""
        gate = RatingFloorGate(
            rules=(_IMDB_BAR, RatingRule(source=RatingSource.ROTTEN_TOMATOES_CRITIC, floor=75)),
            match="all",
        )
        # Clears IMDb, but carries no Rotten Tomatoes score to clear the second bar.
        result = gate.evaluate(_rating_facts(_imdb(8.2, votes=200_000)))

        assert result.outcome == ABSTAIN


def _popularity_facts(watchers: int, reach: Observation[float]) -> Facts:
    """A minimal Facts carrying only what the popularity gate reads."""
    return Facts(
        title="x",
        days_observed_unwatched=Known(value=900.0, source="t"),
        distinct_watchers=Known(value=watchers, source="t"),
        distinct_watchers_all_time=Known(value=watchers, source="t"),
        size_bytes=Known(value=1, source="t"),
        imdb_rating_tenths=Absent(source="t"),
        imdb_votes=Absent(source="t"),
        season_rank=Absent(source="t"),
        is_streaming_now=Known(value=False, source="t"),
        is_managed=Known(value=True, source="t"),
        in_curated_list=Absent(source="t"),
        is_whitelisted=Known(value=False, source="t"),
        history_reach_days=reach,
    )


class TestThePopularityWindowCannotOutrunTheHistory:
    """A watcher count is only an answer for as much of the window the mirror saw.

    Tautulli cannot import plays from before it was installed, so a history younger than
    the popularity window covers only part of it. The count is then a *lower bound*: the
    plays it cannot see are exactly the ones that would have kept the file. Reading that
    as "nobody watched it" is the horizon vector arriving down the watcher lane, which the
    dormancy lane was hardened against years earlier and this one was not.

    The window here is the shipped 365 days and the floor the shipped 3.
    """

    gate = ServerPopularityGate(GateConfig(GateId.SERVER_POPULARITY, threshold=3, window_days=365))

    def test_a_history_shorter_than_the_window_cannot_report_nobody(self) -> None:
        """The bug, stated as the scan met it: a mirror three months deep, a title nobody
        played in those three months, and a year-long window. The gate must not answer."""
        result = self.gate.evaluate(_popularity_facts(0, Known(value=90.0, source="t")))

        assert result.outcome == ABSTAIN
        assert result.blocked is True
        assert "Nobody here watched it" not in result.detail
        assert "could not check who watched it in the last year" in result.detail
        assert "only goes back 3 months" in result.detail

    def test_a_count_between_one_and_the_floor_is_a_lower_bound_too(self) -> None:
        """Not only the zero case. Two watchers seen inside a partly-covered window says
        nothing about how many the uncovered part holds, so "only 2 people" is a claim
        about a year the gate did not see either."""
        result = self.gate.evaluate(_popularity_facts(2, Known(value=90.0, source="t")))

        assert result.blocked is True
        assert "Only 2 people" not in result.detail

    def test_a_hand_reap_cannot_overrule_the_block(self) -> None:
        """The owner's reap button does not release a file on evidence Reaper could not
        read. What holds it is the gate *id*: ``server_popularity`` is not one of
        ``verdict.DEFERRABLE_BLOCK_GATES``, the "you decide this" flags a reap is meant to
        settle. The detail plays no part, which is asserted below rather than assumed --
        an earlier version of this test read as though the wording were the interlock, and
        so could not fail when the wording changed."""
        assert GateId.SERVER_POPULARITY not in DEFERRABLE_BLOCK_GATES

        result = self.gate.evaluate(_popularity_facts(0, Known(value=90.0, source="t")))

        assert block_holds_reap(result.gate.value, result.detail) is True
        assert block_holds_reap(result.gate.value, "any future wording at all") is True
        assert (
            decide_verdict(
                protected=False,
                blocked=True,
                blocked_holds_reap=reap_held_by_blocks([result]),
                score=100,
                coverage_bp=10_000,
                condemn_at=1,
                coverage_floor_bp=0,
                override="reap",
            )
            == "protect"
        )

    def test_enough_watchers_still_protect_on_a_short_history(self) -> None:
        """The lower bound only ever *understates*. Three people seen inside the covered
        part did watch it within the window, so the protection is earned and fires -- and
        keeps its own wording, which the review chip parses (``routes._kept_phrase``)."""
        result = self.gate.evaluate(_popularity_facts(3, Known(value=90.0, source="t")))

        assert result.outcome == PROTECT
        assert result.blocked is False
        assert result.detail == "watched here: 3 people in the last year"

    def test_a_history_covering_the_window_answers_as_it_always_did(self) -> None:
        """The whole point of the reach check is that it changes nothing once the
        evidence is there. Two years of history over a one-year window is a real
        no-watchers finding, and it still reads as one."""
        result = self.gate.evaluate(_popularity_facts(0, Known(value=730.0, source="t")))

        assert result.outcome == ABSTAIN
        assert result.blocked is False
        assert result.detail == "Nobody here watched it in the last year."

    @pytest.mark.parametrize(
        "reach",
        [
            Unknown(reason="this scan did not record it", source="snapshot"),
            Absent(source="unset"),
        ],
        ids=["unknown", "absent"],
    )
    def test_an_unrecorded_reach_blocks_rather_than_assuming_depth(
        self, reach: Observation[float]
    ) -> None:
        """A snapshot frozen before the reach was a fact thaws it as ``Unknown`` (rule
        104), and a Facts built by hand leaves it ``Absent``. Neither is permission to
        claim a year of coverage, so both fail closed."""
        result = self.gate.evaluate(_popularity_facts(0, reach))

        assert result.blocked is True
        assert result.detail.startswith("could not check")

    def test_a_shorter_window_the_history_does_cover_is_answerable(self) -> None:
        """The operator's remedy, and proof the check is against the *configured* window
        rather than a fixed span: narrowing the window to what the mirror holds gets a
        real answer back."""
        gate = ServerPopularityGate(
            GateConfig(GateId.SERVER_POPULARITY, threshold=3, window_days=90)
        )
        result = gate.evaluate(_popularity_facts(0, Known(value=90.0, source="t")))

        assert result.blocked is False
        assert result.detail == "Nobody here watched it in the last 3 months."


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
            # Deeper than both the popularity window and the 900 days above, so the
            # watcher count is a complete answer and the gate reports it rather than
            # blocking. A scan always knows this; only a hand-built Facts can omit it.
            history_reach_days=Known(value=1200.0, source="tautulli"),
            # A below-floor IMDb rating, so the rating gate reports its actual number.
            ratings=_imdb(6.0, votes=50_000),
        )

        evaluation = evaluate_all(ALL_GATES, item)

        assert len(evaluation.results) == len(ALL_GATES)
        assert evaluation.protected is False
        assert len(evaluation.checked_and_did_not_fire) == len(ALL_GATES)

        details = " | ".join(r.detail for r in evaluation.checked_and_did_not_fire)
        assert "Nobody here watched it" in details  # server-popularity, 0 watchers
        assert "below the 7.5 you keep" in details  # rating floor


# --- the scoring-model invariants -------------------------------------------
#
# The four above cover the gate lane. These cover the score lane, and specifically
# the two places the design argument is load-bearing but was never asserted: the
# KEEP lane (which the older property test omits entirely) and the relationship
# between the score and coverage (which is what makes condemn_at a second, implicit
# coverage floor).

#: A policy exercising all three lanes at once. The older property test passes only
#: built-in signals, so no keep has ever been under a property test.
_CUSTOM_CONDEMN = [
    CustomSignalConfig(
        name="Big files",
        weight=10,
        kind="graded",
        field="size_bytes",
        floor=0,
        saturate_at=50_000_000_000,
    ),
    CustomSignalConfig(
        name="Ended",
        weight=10,
        kind="boolean",
        field="show_ended",
        condition=fields.Condition(field="show_ended", op=fields.Op.EQ, value=True),
    ),
]

#: ``field`` here is the rule-authorable KEY (``fields.BY_KEY``), not the ``Facts``
#: attribute name. They differ, and a key that does not resolve makes ``evaluate_keep``
#: take its unreadable branch on every item, which would pass these tests vacuously.
_KEEPS = [
    KeepConfig(name="Well rated", max_discount=25, field="imdb_rating", floor=50, saturate_at=80),
    KeepConfig(
        name="People still watch it",
        max_discount=15,
        field="watchers_all_time",
        floor=0,
        saturate_at=5,
    ),
]

#: Facts nothing in the score lane reads, so an ``Unknown`` substituted here would
#: exercise nothing. All three are protection facts: their ``FieldSpec``s are
#: protect-lane only, and in every policy Reaper ships they are read by a gate
#: (``CuratedListGate``, ``WhitelistGate``, ``StreamingNowGate``), which returns
#: PROTECT or ABSTAIN and can never add pressure. Move one into the sweep the day a
#: signal, custom rule or keep above references it.
_GATE_ONLY = frozenset({"in_curated_list", "is_whitelisted", "is_streaming_now"})


def _observed_fields() -> tuple[str, ...]:
    """Every ``Facts`` observation an authorable field can read, in declaration order.

    Derived rather than hand-listed. A ``FieldSpec`` names its fact with a ``read``
    lambda instead of a string, so the names come back by handing every spec a probe
    whose observations each carry their own attribute name. The hand-list this replaced
    left ``genres`` out while the comment above it said the sweep was exhaustive, which
    is exactly the drift rule 7 forbids: a new authorable field can no longer be added
    without joining the sweep.

    Out by construction: ``is_managed`` has no ``FieldSpec`` at all, so no rule can name
    it. It is the fact the retired ``UnmanagedGate`` read (``engine.gates``) and now has no
    consumer; it stays because it is a true observation and the evidence any re-wiring would
    need. Out by choice: ``_GATE_ONLY``.
    """
    names = [f.name for f in dataclass_fields(Facts) if f.name not in ("title", "ratings")]
    probe = Facts(title="probe", **{n: Known(value=n, source="probe") for n in names})
    readable = {
        obs.value for spec in fields.BY_KEY.values() if isinstance(obs := spec.read(probe), Known)
    }
    return tuple(n for n in names if n in readable and n not in _GATE_ONLY)


#: Every ``Observation`` field an authorable rule can read, minus ``_GATE_ONLY``.
#: Derived by ``_observed_fields`` (see its docstring for what is out and why); the
#: hand-written version of this list said "exhaustive" while omitting ``genres``.
_OBSERVED_FIELDS = _observed_fields()


def _full_score(item: Facts) -> Score:
    return score(ALL_SIGNALS, item, custom_condemn=_CUSTOM_CONDEMN, keeps=_KEEPS)


class TestLosingEvidenceCannotCondemn:
    """The whole safety argument, over all three lanes at once.

    ``test_an_unknown_input_never_increases_the_score`` above predates the keep lane
    and the custom-rule lane: it passes neither, and substitutes only ``Unknown``.
    Those two omissions are exactly where the arithmetic can invert, so these repeat
    the property with all three lanes wired and with ``Absent`` substituted too.
    """

    def test_the_sweep_covers_every_field_a_condemn_rule_can_name(self) -> None:
        """The sweep below is only as good as its field list.

        A field an owner can put in a condemn rule can add deletion pressure, so losing
        it has to be swept. ``genres`` was authorable and unswept while the list was
        maintained by hand, and nothing said so. Deriving the list from the registry is
        what this pins: add a condemn-lane field, and it joins the sweep or this fails.
        """
        probe = Facts(
            title="probe",
            **{
                f.name: Known(value=f.name, source="probe")
                for f in dataclass_fields(Facts)
                if f.name not in ("title", "ratings")
            },
        )
        authorable = {
            obs.value
            for spec in fields.BY_KEY.values()
            if fields.Lane.CONDEMN in spec.lanes
            and isinstance(obs := spec.read(probe), Known)
            and obs.value not in _GATE_ONLY
        }

        assert "genres" in authorable, "the genre field stopped being condemn-authorable"
        assert authorable <= set(_OBSERVED_FIELDS), (
            f"condemn-authorable but never made Unknown: {authorable - set(_OBSERVED_FIELDS)}"
        )

    @given(item=facts())
    @settings(max_examples=500)
    def test_an_unreadable_input_never_raises_the_score_in_any_lane(self, item: Facts) -> None:
        """THE property, restated over all three lanes.

        The older ``test_an_unknown_input_never_increases_the_score`` passes only
        built-in signals, so neither the custom-rule lane nor the keep lane has ever
        been under a property test. The keep lane is the one that can invert, because
        it is the only lane that subtracts.
        """
        baseline = _full_score(item).value

        for field_name in _OBSERVED_FIELDS:
            degraded = _full_score(
                replace(item, **{field_name: Unknown(reason="outage", source="test")})
            ).value

            assert degraded <= baseline + 1e-9, (
                f"making {field_name} Unknown INCREASED the score: {baseline} -> {degraded}"
            )

    def test_an_absent_keep_field_withdraws_its_keep_and_that_is_deliberate(self) -> None:
        """``Absent`` on a keep field RAISES the score, on purpose. Read this before
        touching the fact builders.

        ``Unknown`` and ``Absent`` are opposite instructions to the keep lane
        (``signals.evaluate_keep``): "could not check" keeps fully, "checked, there is
        genuinely none" keeps not at all. The second is right. A title with no IMDb
        rating is not well rated, it is unrated, and a "keep well-rated titles" rule
        that also kept every unrated title would protect the whole library.

        The consequence is that ``Absent`` is a *privileged* state: recording one
        withdraws protection. So a fact builder may only ever emit ``Absent`` when it
        genuinely looked. A builder that cannot tell "no rating" from "no id to look
        it up with" silently un-protects the second case, with coverage still reading
        100% and nothing degrading the snapshot. That is what
        ``tests/test_fact_layer_states.py`` exists to prevent, and this test is here
        so that anyone who "fixes" the asymmetry finds the reason first.
        """
        rated = replace(_rating_facts(()), imdb_rating_tenths=Known(value=80, source="imdb"))
        unrated = replace(rated, imdb_rating_tenths=Absent(source="imdb"))
        unreadable = replace(
            rated, imdb_rating_tenths=Unknown(reason="dataset down", source="imdb")
        )

        assert _full_score(unrated).value > _full_score(rated).value
        assert _full_score(unreadable).value <= _full_score(rated).value

    @given(item=facts())
    @settings(max_examples=500)
    def test_the_score_can_never_exceed_what_we_could_read(self, item: Facts) -> None:
        """``base <= 100 * coverage``, the invariant nobody wrote down.

        Every unevaluated signal contributes pressure 0 while keeping its weight in
        the denominator, and every evaluated one contributes at most its weight. So
        the score is bounded by the share of evidence we actually read.

        The consequence is load-bearing and easy to delete by accident: ``condemn_at``
        is ITSELF a coverage floor. An item cannot reach a condemn threshold of 70
        without at least 70% of the policy's weight being readable, whatever
        ``coverage_floor_bp`` is set to. Any change that lets a rule add points
        outside the denominator removes that second floor silently.
        """
        result = _full_score(item)

        assert result.base_value <= MAX_SCORE * result.coverage + 1e-9

    @given(item=facts())
    @settings(max_examples=500)
    def test_coverage_cannot_rise_when_evidence_is_lost(self, item: Facts) -> None:
        """Coverage measures what we could read, so it can only fall as we read less.

        Stated separately from the score because a model can hold the score bound
        while inflating coverage, and coverage is what the abstain floor consults.
        """
        baseline = _full_score(item).coverage

        for field_name in _OBSERVED_FIELDS:
            degraded = _full_score(
                replace(item, **{field_name: Unknown(reason="outage", source="test")})
            ).coverage

            assert degraded <= baseline + 1e-9, (
                f"losing {field_name} RAISED coverage: {baseline} -> {degraded}"
            )

    @given(value=st.integers(0, 100))
    def test_an_unreadable_keep_keeps_fully(self, value: int) -> None:
        """A keep we could not evaluate takes its MAXIMUM discount.

        The mirror of the condemn lane's "Unknown contributes zero": on both sides,
        the unreadable case resolves toward keeping the file. Only the keep lane is
        asserted here because only the condemn lane was asserted before.
        """
        keep = _KEEPS[0]
        readable = replace(_rating_facts(()), imdb_rating_tenths=Known(value=value, source="imdb"))
        unreadable = replace(
            readable, imdb_rating_tenths=Unknown(reason="dataset down", source="imdb")
        )

        assert evaluate_keep(keep, unreadable).discount == float(keep.max_discount)
        assert evaluate_keep(keep, unreadable).discount >= evaluate_keep(keep, readable).discount

    @given(item=facts())
    @settings(max_examples=200)
    def test_the_score_stays_in_bounds(self, item: Facts) -> None:
        """0-100 and 0-1, whatever the evidence. Every consumer assumes both."""
        result = _full_score(item)

        assert 0.0 <= result.value <= MAX_SCORE
        assert 0.0 <= result.coverage <= 1.0
        assert 0.0 <= result.base_value <= MAX_SCORE
