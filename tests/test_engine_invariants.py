# SPDX-License-Identifier: AGPL-3.0-or-later
"""The invariants that make Reaper safe, as property tests.

These are the machine-checkable form of the entire design argument. If one of them
fails, the tool can delete something it should not, and no amount of careful UI
will save it.

Hypothesis generators are deliberately biased toward the values that break things:
0, empty, None, and Unknown.
"""

from __future__ import annotations

import ast
import inspect
import json
from dataclasses import fields as dataclass_fields
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from reaper.engine import fields, gates
from reaper.engine.gates import (
    ABSTAIN,
    PROTECT,
    DataHorizonGate,
    Evaluation,
    Facts,
    Gate,
    GateConfig,
    GateId,
    GateResult,
    MinDormancyGate,
    RatingFloorGate,
    RatingRule,
    ServerPopularityGate,
    StreamingNowGate,
    evaluate_all,
)
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.engine.policy import DEFAULT_MOVIE_POLICY, PolicyBody
from reaper.engine.signals import (
    MAX_SCORE,
    CustomSignalConfig,
    KeepConfig,
    Score,
    SignalConfig,
    SignalId,
    SignalState,
    evaluate_keep,
    evaluate_signal,
    score,
)
from reaper.engine.verdict import STRUCTURAL_GATES
from reaper.ratings import Rating, RatingSource
from reaper.services.condemned import reap_override_verdict_decoded
from reaper.services.snapshot import _explain, _verdict

_IMDB_BAR = RatingRule(source=RatingSource.IMDB, floor=75, min_votes=1000)


def _hand_reap(evaluation: Evaluation, score_value: int, policy: PolicyBody) -> str:
    """What a hand reap on this evaluation actually decides, through the production path.

    A reap never reaches the scan's ``_verdict``: the scan freezes an explanation, and
    ``condemned.reap_override_verdict_decoded`` re-decides from that document hours later
    when the operator presses Reap. So the frozen document is produced here by the real
    writer (``snapshot._explain``) rather than hand-typed, and a field the writer stops
    emitting fails these tests instead of quietly changing what the read side can see.
    """
    frozen = Score(value=float(score_value), coverage=1.0, results=[])
    stored = json.loads(_explain(evaluation, frozen, policy))
    return reap_override_verdict_decoded(stored, score=score_value)


def _imdb(value: float, votes: int) -> tuple[Rating, ...]:
    """One frozen IMDb rating, as the scan would carry it into ``Facts.ratings``."""
    return (Rating(source=RatingSource.IMDB, value=value, votes=votes, provider="imdb-dataset"),)


# --- generators -------------------------------------------------------------


def observations[T](
    value_strategy: st.SearchStrategy[T],
) -> st.SearchStrategy[Observation[T]]:
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
        on_lists=draw(observations(st.text(max_size=20))),
        # Drawn across the popularity gate's 365-day window in both directions, so the
        # sweep keeps reaching the gate's answering branches. Left at its default this is
        # `Absent`, which the gate reads as un-checkable: every example would block, and
        # the invariants below would hold for a reason that has nothing to do with them.
        history_reach_days=draw(observations(st.floats(0, 5000, allow_nan=False))),
    )


def _on_list_gate(name: str = "My keep list") -> fields.CustomProtectGate:
    """List membership protects through the operator's own ``on_list`` rule now; the
    whitelist and curated-list gate classes are deleted."""
    return fields.CustomProtectGate(fields.Condition(field="on_list", op=fields.Op.EQ, value=name))


ALL_GATES: list[Gate] = [
    RatingFloorGate(rules=(_IMDB_BAR,)),
    StreamingNowGate(GateConfig()),
    ServerPopularityGate(GateConfig(threshold=3)),
    _on_list_gate(),
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
    @settings(max_examples=300, deadline=None)
    def test_every_gate_outcome_is_protect_or_abstain(self, item: Facts) -> None:
        for result in evaluate_all(ALL_GATES, item).results:
            assert result.outcome in (PROTECT, ABSTAIN)


class TestOnlyTwoGatesMayEverRefuseAHandReap:
    """The membership guard on the ONE set that still holds a hand reap (rule 103).

    This class used to guard the opposite list. A blocked gate released a hand reap only
    when its gate was in ``verdict.DEFERRABLE_BLOCK_GATES`` *and* its producer set
    ``defers_to_owner``, and nothing pinned the membership half: growing the frozenset
    failed no test, so a gate could join it in silence. That list is gone -- no block holds
    a hand reap now -- and the same drift risk moved, whole, onto ``STRUCTURAL_GATES``,
    which is what a reap cannot pass. Both directions of a change to it are dangerous, and
    only one of them looks it: dropping a member RELEASES a file, and adding one takes away
    an overrule the owner is entitled to and hides the fact behind a "safety" set.
    """

    def test_the_structural_list_is_exactly_the_two_stops_that_are_not_judgments(self) -> None:
        """Editing this set must be a deliberate change that fails this test first, because
        the reviewer then has to answer the question membership does not: is the gate a fact
        about whether the file can be REMOVED at all, or a judgment about whether it is
        WANTED? Only the first belongs here. Deleting mid-stream breaks a session someone is
        watching, and a file no *arr manages has no path to delete through -- so overruling
        either cannot give the owner what they asked for. Everything else (dormancy, rating,
        popularity, a curated list, the keep list, the season keep-rule conflict) is a
        cautious judgment, and a judgment is the owner's to overrule."""
        assert frozenset({GateId.STREAMING_NOW, GateId.UNMANAGED}) == STRUCTURAL_GATES

    def test_only_a_fired_structural_gate_refuses_and_a_blocked_one_does_not(self) -> None:
        """The set is consulted for a gate that FIRED, and that distinction is the whole
        reason it is safe for a blocked one to pass.

        A fired ``streaming_now`` is "somebody is playing this"; a blocked one is "we could
        not read who is playing", frozen well before anything is sent. The live veto at send
        time (``executor._being_watched_now``) re-polls Plex per item and spares on any read
        failure, so it both supersedes the frozen guess and fails closed where the scan could
        only guess. Asserted through the production caller, never a transcription of what it
        passes (rule 119).

        The two gates are named here rather than read out of ``STRUCTURAL_GATES``, which
        looks like the tidier spelling and is the one that cannot fail: emptying the set
        empties the loop, and a vacuous sweep reads as a proof while asserting nothing
        (rule 118). Written out, shrinking the set fails this test as well as the membership
        one above."""
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 1})
        for gate in (GateId.STREAMING_NOW, GateId.UNMANAGED):
            fired = Evaluation(results=[GateResult(gate, PROTECT, detail="the stop fired")])
            unreadable = Evaluation(
                results=[GateResult(gate, ABSTAIN, detail="could not check it", blocked=True)]
            )

            assert _hand_reap(fired, 100, policy) == "protect", gate
            assert _hand_reap(unreadable, 100, policy) == "condemn", gate
            # ...and with nobody deciding by hand, the block still keeps it off every
            # automatic path. That is the job a block kept.
            assert _verdict(unreadable, 100, 10_000, policy) == "abstain", gate


class TestUnknownNeverCondemns:
    """The invariant that survives an outage."""

    @given(item=facts())
    @settings(max_examples=300, deadline=None)
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
    @settings(max_examples=300, deadline=None)
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
            degraded = replace(item, **{field_name: Unknown(reason="outage", source="test")})  # type: ignore[arg-type]
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


#: Every fact the catalog's gates can read is readable here, so a case below is the only
#: thing unreadable in its own run. Deliberately not `_rating_facts` or `_popularity_facts`:
#: both leave fields `Absent` that a gate under test would then block on for a reason the
#: case never named.
_ALL_READABLE = Facts(
    title="A Film",
    days_observed_unwatched=Known(value=1200.0, source="t"),
    distinct_watchers=Known(value=0, source="t"),
    distinct_watchers_all_time=Known(value=0, source="t"),
    size_bytes=Known(value=1, source="t"),
    imdb_rating_tenths=Known(value=90, source="t"),
    imdb_votes=Known(value=5000, source="t"),
    season_rank=Absent(source="t"),
    is_streaming_now=Known(value=False, source="t"),
    is_managed=Known(value=True, source="t"),
    in_curated_list=Absent(source="t"),
    is_whitelisted=Known(value=False, source="t"),
    # Deeper than the popularity window, or that gate blocks on its reach instead of on
    # the watcher count this table is about, and every row would pass for the wrong reason.
    history_reach_days=Known(value=4000.0, source="t"),
)

#: One row per `gates._blocked` call site: the gate, and the fact that call reads.
#: `RatingFloorGate` reads two and so has two rows. Reconciled against the catalog by
#: `test_every_fail_closed_guard_in_the_catalog_has_a_case` below, so a gate that gains a
#: guard fails here until it gains a row.
FAIL_CLOSED_GUARDS: list[tuple[Gate, str]] = [
    (RatingFloorGate(rules=(_IMDB_BAR,)), "imdb_rating_tenths"),
    (RatingFloorGate(rules=(_IMDB_BAR,)), "imdb_votes"),
    (StreamingNowGate(GateConfig()), "is_streaming_now"),
    (ServerPopularityGate(GateConfig(threshold=3)), "distinct_watchers"),
    (MinDormancyGate(GateConfig(threshold=1095)), "days_observed_unwatched"),
    (DataHorizonGate(GateConfig()), "days_observed_unwatched"),
]

_GUARD_IDS = [f"{gate.__class__.__name__}.{field}" for gate, field in FAIL_CLOSED_GUARDS]


def _blocked_call_sites() -> set[tuple[str, str]]:
    """Every `_blocked(...)` call in the gate catalog, as (class, fact field).

    Read out of the source rather than hand-listed, so the population the table below claims
    to cover is the population that exists (rule 145). The shape it accepts is asserted, not
    assumed (rule 147): a guard spelled some other way than ``_blocked(self.id, facts.x, …)``
    fails the walk instead of dropping out of it, which would quietly shrink both the scan
    and the table it is reconciled against.
    """
    source = Path(inspect.getfile(gates)).read_text()
    sites: set[tuple[str, str]] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if not (isinstance(call.func, ast.Name) and call.func.id == "_blocked"):
                continue
            observed = call.args[1]
            assert isinstance(observed, ast.Attribute), f"{node.name}: {ast.dump(observed)}"
            assert isinstance(observed.value, ast.Name) and observed.value.id == "facts"
            sites.add((node.name, observed.attr))
    return sites


class TestEveryFailClosedGuardKeepsTheFile:
    """`gates._blocked` at each call site, driven one gate at a time.

    The property above cannot stand in for these. It reads `if result.blocked:` over whatever
    the sweep produced, so deleting a guard yields *fewer* blocked results and the conditional
    over the smaller set still holds -- green, while the gate it covered stopped failing
    closed. Rule 118: where the guard upstream makes a branch indiscriminable, drive the
    interlock directly.

    What each case pins is the difference between "we could not check this protection" and
    "this protection did not fire", which is the whole of rule 93 at the gate layer.
    """

    @pytest.mark.parametrize(("gate", "field"), FAIL_CLOSED_GUARDS, ids=_GUARD_IDS)
    def test_an_unreadable_fact_blocks_the_gate(self, gate: Gate, field: str) -> None:
        """An Unknown input abstains *and* raises the blocked flag, naming its own cause.

        The "could not check" prefix is load-bearing beyond this assertion: `api.routes._chip`
        routes a detail starting with it to "Some checks couldn't run" rather than "left for
        you to decide", and `WhyPanel` splits it into check and cause.
        """
        outage = Unknown(reason="the source is down", source="t")
        unreadable = replace(_ALL_READABLE, **{field: outage})  # type: ignore[arg-type]

        result = gate.evaluate(unreadable)

        assert result.blocked is True
        assert result.outcome == ABSTAIN
        assert result.detail.startswith("could not check")
        assert "the source is down" in result.detail

    @pytest.mark.parametrize(("gate", "field"), FAIL_CLOSED_GUARDS, ids=_GUARD_IDS)
    def test_a_readable_fact_does_not_block_the_gate(self, gate: Gate, field: str) -> None:
        """The other half, without which the case above holds for a gate that blocks on
        everything. `_ALL_READABLE` answers every one of these fields, so the only thing
        separating the two runs is the Unknown."""
        result = gate.evaluate(_ALL_READABLE)

        assert result.blocked is False, field

    def test_every_fail_closed_guard_in_the_catalog_has_a_case(self) -> None:
        """The table covers the guards that exist, not the guards it was written against.

        Set equality both ways: a gate that gains a `_blocked` call fails until it gains a
        row, and a row whose guard was deleted fails rather than passing vacuously.
        """
        assert _blocked_call_sites() == {
            (gate.__class__.__name__, field) for gate, field in FAIL_CLOSED_GUARDS
        }


class TestAnOnListRuleHoldsWhatIsOnTheList:
    """The arm that fires the protection, driven directly.

    List membership protects through the operator's own ``on_list`` keep rule now
    (``fields.CustomProtectGate``), so this class pins the same three claims the deleted
    gate classes carried: a listed title is held and the list is named, a title on no list
    is a checked miss, and an unreadable membership blocks rather than passing. Deleting
    the membership arm turns "on your list" into a green miss behind a green run: a
    protection withdrawn from every listed title in the library, with nothing to say so.
    Rule 118, in the condemn direction.
    """

    def test_a_listed_title_is_protected_and_the_list_is_named(self) -> None:
        """The hold fires, and the detail names the list the rule matched.

        The fact is the multi convention's comma-joined names, so an item on two lists is
        matched per element, and the match is case-insensitive on both sides (rule 88):
        the rule spells the list "awards shortlist" and the fact carries the operator's
        own casing.
        """
        gate = _on_list_gate("awards shortlist")
        listed = replace(
            _ALL_READABLE, on_lists=Known(value="Awards Shortlist, Second List", source="t")
        )

        result = gate.evaluate(listed)

        assert result.outcome == PROTECT
        assert result.blocked is False
        assert result.detail == "your rule: List membership includes awards shortlist"

    def test_a_title_on_other_lists_only_is_a_checked_miss(self) -> None:
        """eq on a multi-valued fact is per element, never a whole-string comparison --
        but it must still be an exact element match, or "keep" would match "keep list"."""
        gate = _on_list_gate("My keep list")
        elsewhere = replace(
            _ALL_READABLE, on_lists=Known(value="My keep list extended", source="t")
        )

        result = gate.evaluate(elsewhere)

        assert result.outcome == ABSTAIN
        assert result.blocked is False

    def test_a_title_on_no_list_abstains_and_says_so(self) -> None:
        """The other half, without which the fired case holds for a gate that protects
        everything. Its sentence is what the why-panel prints under the checks that ran
        and did not fire, where a blank reads as a check that never ran."""
        gate = _on_list_gate()
        unlisted = replace(_ALL_READABLE, on_lists=Absent(source="t"))

        result = gate.evaluate(unlisted)

        assert result.outcome == ABSTAIN
        assert result.blocked is False
        assert result.detail == "checked your rule: On one of your lists: none recorded"

    def test_an_unreadable_membership_blocks_the_rule(self) -> None:
        """Rule 93: a membership that could not be read is ``Unknown``, and the rule
        blocks -- amber, "could not check" -- rather than reporting a green miss that
        withdraws the protection library-wide."""
        gate = _on_list_gate()
        unreadable = replace(
            _ALL_READABLE, on_lists=Unknown(reason="the list sync failed", source="t")
        )

        result = gate.evaluate(unreadable)

        assert result.outcome == ABSTAIN
        assert result.blocked is True
        assert result.detail.startswith("could not check")
        assert "the list sync failed" in result.detail


class TestScoreBounds:
    @given(item=facts())
    @settings(max_examples=300, deadline=None)
    def test_the_score_stays_in_range(self, item: Facts) -> None:
        result = score(ALL_SIGNALS, item)
        assert 0.0 <= result.value <= 100.0
        assert 0.0 <= result.coverage <= 1.0

    @given(item=facts())
    @settings(max_examples=200, deadline=None)
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
    def test_a_listed_item_is_protected_at_any_score(self) -> None:
        """The keep list's job, in its new spelling: an ``on_list`` rule naming the list
        the item is on holds it whatever it scores."""
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
            is_whitelisted=Known(value=True, source="t"),
            on_lists=Known(value="My keep list", source="t"),  # <- the operator's list
            # Deeper than the popularity window, so the zero watcher count is a real
            # measurement and FEW_WATCHERS charges its full pressure. The point of this
            # test is that a maximal score still loses to a protection, so the score has
            # to actually be maximal.
            history_reach_days=Known(value=4000.0, source="t"),
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


#: How a bar of 75 from 1,000 votes reads for every source Reaper knows, written from the
#: spec rather than from the branch that produces it (rule 119): a percentage source leads
#: with its name, a 0-10 source leads with its number and carries the vote clause. Keyed on
#: the whole enum, so a source added to `RatingSource` fails here until someone decides which
#: word order it takes -- the population, not just the two members that motivated the table
#: (rule 145).
_BAR_TEXT: dict[RatingSource, str] = {
    RatingSource.IMDB: "7.5 on IMDb from 1,000 votes",
    RatingSource.TMDB: "7.5 on TMDb from 1,000 votes",
    RatingSource.ROTTEN_TOMATOES_CRITIC: "Rotten Tomatoes critics 75%",
    RatingSource.ROTTEN_TOMATOES_AUDIENCE: "Rotten Tomatoes audience 75%",
    RatingSource.METACRITIC: "Metacritic 75%",
    RatingSource.TRAKT: "7.5 on Trakt from 1,000 votes",
    RatingSource.TVDB: "7.5 on TVDB from 1,000 votes",
    RatingSource.UNKNOWN: "7.5 on an unknown source from 1,000 votes",
}

#: How "we found no rating at all from this source" reads for every source, written from
#: the spec the same way. Keyed on the whole enum for the same reason (rule 145), and
#: pinning the same position contract: the label follows a preposition, never an article
#: or a bare "no", which is what rendered "no an unknown source rating" (#338).
_MISS_TEXT: dict[RatingSource, str] = {
    source: f"no rating on {label} (you keep {bar})"
    for source, label, bar in (
        (RatingSource.IMDB, "IMDb", "7.5 on IMDb from 1,000 votes"),
        (RatingSource.TMDB, "TMDb", "7.5 on TMDb from 1,000 votes"),
        (
            RatingSource.ROTTEN_TOMATOES_CRITIC,
            "Rotten Tomatoes critics",
            "Rotten Tomatoes critics 75%",
        ),
        (
            RatingSource.ROTTEN_TOMATOES_AUDIENCE,
            "Rotten Tomatoes audience",
            "Rotten Tomatoes audience 75%",
        ),
        (RatingSource.METACRITIC, "Metacritic", "Metacritic 75%"),
        (RatingSource.TRAKT, "Trakt", "7.5 on Trakt from 1,000 votes"),
        (RatingSource.TVDB, "TVDB", "7.5 on TVDB from 1,000 votes"),
        (RatingSource.UNKNOWN, "an unknown source", "7.5 on an unknown source from 1,000 votes"),
    )
}


class TestRatingGate:
    @pytest.mark.parametrize(
        ("value", "protects"),
        [(7.6, True), (7.5, True), (7.4, False)],
        ids=["above-the-bar", "exactly-at-the-bar", "one-tenth-under"],
    )
    def test_the_bar_is_read_in_tenths_and_compared_on_the_ten_point_scale(
        self, value: float, protects: bool
    ) -> None:
        """A floor of 75 is 7.5, and both sides of that conversion have to be pinned.

        The policy stores every rating floor in tenths so the hash stays byte-stable, and
        ratings arrive on the 0-10 scale, so the gate divides by 10 to compare them. Nothing
        drove a rating at the bar or a tenth under it, which left the divisor free to move:
        at ``floor / 9`` a configured 7.5 silently becomes a bar of 8.33 and every title
        between them loses a protection the operator still sees configured, and at
        ``floor / 11`` it becomes 6.8 and keeps titles they never asked to keep. A
        conversion is only pinned by a case that would land differently under it.
        """
        gate = RatingFloorGate(rules=(_IMDB_BAR,))  # IMDb 7.5 from 1,000 votes

        result = gate.evaluate(_rating_facts(_imdb(value, votes=5_000)))

        assert (result.outcome == PROTECT) is protects

    @pytest.mark.parametrize(
        ("min_votes", "clause"),
        [(0, ""), (1, " from 1 vote"), (2, " from 2 votes"), (1000, " from 1,000 votes")],
        ids=["no-vote-floor", "one-vote", "two-votes", "a-thousand"],
    )
    def test_the_bar_names_its_vote_floor_and_counts_one_as_one_vote(
        self, min_votes: int, clause: str
    ) -> None:
        """The clause is present exactly when there is a vote floor, and reads as English.

        Three copies of this phrase existed -- here, ``Rating.describe`` and
        ``Rating.describe_for_user`` -- and all three said "from 1 votes", because every
        case that drove them used a count in the thousands. A vote floor of 1 is a legal
        policy and a title with a single vote is ordinary, so all three were reachable.
        They now derive from ``ratings.describe_votes`` (rule 104), and this pins the
        presence of the clause in both directions as well as its wording: with no floor
        there is nothing honest to print, and "from 0 votes" would read as a measurement
        rather than as its absence.
        """
        bar = RatingRule(source=RatingSource.IMDB, floor=75, min_votes=min_votes)

        assert bar.describe_bar() == f"7.5 on IMDb{clause}"

    @pytest.mark.parametrize("source", list(_BAR_TEXT), ids=[s.value for s in _BAR_TEXT])
    def test_each_source_states_its_bar_in_its_own_word_order(self, source: RatingSource) -> None:
        """A percentage bar reads "Rotten Tomatoes critics 75%", never "75% on Rotten
        Tomatoes critics".

        The case above sweeps the vote clause but only ever on IMDb, so the percentage arm
        had nothing pinning either spelling and could be deleted without a word: the bar
        would render in the 0-10 sources' word order, with a stray vote clause behind it on
        sources that count no votes. The operator reads this string in the why-panel and
        beside the bar in the policy editor, and both are supposed to echo what they typed.
        """
        bar = RatingRule(source=source, floor=75, min_votes=1000)

        assert bar.describe_bar() == _BAR_TEXT[source]

    def test_the_table_of_word_orders_covers_every_source(self) -> None:
        """Set equality both ways, so the sweep above is over the sources that exist rather
        than the sources it was written against."""
        assert set(_BAR_TEXT) == set(RatingSource)

    @pytest.mark.parametrize("source", list(_MISS_TEXT), ids=[s.value for s in _MISS_TEXT])
    def test_each_source_reads_as_english_where_the_item_has_no_rating_at_all(
        self, source: RatingSource
    ) -> None:
        """The miss phrase places a label too, and placed it after "no" until #338.

        "no IMDb rating" reads fine, which is how this survived; "no an unknown source
        rating" does not, and the operator meets it in the why-panel under the checks that
        did not fire -- beside a file they are deciding whether to delete. The label now
        follows a preposition here as it does in ``describe_bar`` above, so the sentence
        holds for whatever a label turns out to be.
        """
        bar = RatingRule(source=source, floor=75, min_votes=1000)

        assert RatingFloorGate(rules=(bar,))._miss_phrase(bar, None) == _MISS_TEXT[source]

    def test_the_table_of_miss_phrases_covers_every_source(self) -> None:
        """Same set equality as the word orders above: a source added to the enum owes a
        decision about how it reads with no rating, not a default that happens to parse."""
        assert set(_MISS_TEXT) == set(RatingSource)

    def test_a_policy_with_no_bars_set_says_nothing_is_configured(self) -> None:
        """A rating gate switched on with an empty rule set still owes the operator a
        sentence.

        Not a hypothetical state: it is exactly what `policy.recover_rating_rules` exists to
        repair, and that function's own docstring quotes this sentence as the symptom the
        operator would have seen. Deleting the arm drops through to the miss-list return,
        which joins an empty list and renders `"."` -- a bare period in the panel whose job
        is to be believed (rule 21). Pinned as an exact string because the failure is that
        there is no string.
        """
        gate = RatingFloorGate(rules=())

        result = gate.evaluate(_rating_facts(()))

        assert result.outcome == ABSTAIN
        assert result.blocked is False
        assert result.detail == "No rating is set that would keep a title."

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
        assert "no rating on IMDb" in result.detail

    @pytest.mark.parametrize(
        ("unreadable", "check"),
        [
            ("imdb_rating_tenths", "could not check the IMDb rating"),
            ("imdb_votes", "could not check the IMDb vote count"),
        ],
        ids=["the-rating", "the-vote-count"],
    )
    def test_an_unreadable_imdb_number_blocks_where_a_missing_one_does_not(
        self, unreadable: str, check: str
    ) -> None:
        """Rule 93 at the gate: ``Absent`` is "we looked and there is no rating", which the
        test above pins as a clean ABSTAIN; ``Unknown`` is "we could not read IMDb at all",
        and that must fail closed and BLOCK, or a dataset outage silently withdraws this
        protection from every title carrying it.

        Nothing discriminated the two arms. ``_rating_facts`` hardcodes both numbers
        ``Absent``, so no test in this class could reach the ``Unknown`` one, and
        ``TestUnknownNeverCondemns``'s property is stated as *if* blocked *then* abstain --
        deleting the guard produces FEWER blocked results, which a conditional over a
        smaller set still satisfies. Driven by a mutation run (#243): with the guard
        removed, these same facts come back unblocked, so the assertion below is the only
        thing standing between an IMDb outage and a library-wide loss of the rating keep.

        Both halves are here rather than split, because the contrast IS the claim: one
        observation kind blocks and the other does not, on facts identical otherwise.
        """
        gate = RatingFloorGate(rules=(_IMDB_BAR,))
        facts = _rating_facts(())  # no rating either way; only the observation kind differs

        assert gate.evaluate(facts).blocked is False  # Absent: nothing to keep it on

        result = gate.evaluate(
            replace(facts, **{unreadable: Unknown(reason="dataset down", source="imdb")})  # type: ignore[arg-type]
        )

        assert result.blocked is True
        assert result.outcome == ABSTAIN
        assert check in result.detail

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

    def test_all_of_matching_names_the_bar_that_did_clear(self) -> None:
        """A near miss under ALL says what cleared as well as what did not.

        The gate has two ABSTAIN sentences and only one of them credits the bar the title
        passed. The case above drives this arm and asserts nothing about it, so deleting the
        arm printed the misses alone: a title an inch under one of two bars reads as though
        it cleared neither, and the operator cannot see how close the file came to being
        kept. The second bar is present and below its floor here rather than missing
        entirely, so the "below the bar you keep" miss is exercised alongside the credit.
        """
        gate = RatingFloorGate(
            rules=(_IMDB_BAR, RatingRule(source=RatingSource.ROTTEN_TOMATOES_CRITIC, floor=75)),
            match="all",
        )
        ratings = (
            Rating(source=RatingSource.IMDB, value=8.2, votes=200_000, provider="imdb-dataset"),
            Rating(source=RatingSource.ROTTEN_TOMATOES_CRITIC, value=4.0, votes=None, provider="p"),
        )

        result = gate.evaluate(_rating_facts(ratings))

        assert result.outcome == ABSTAIN
        assert result.detail == (
            "cleared 8.2 on IMDb from 200,000 votes, but not every bar you asked for: "
            "Rotten Tomatoes critics 40%, below the 75% you keep."
        )


def _popularity_facts(
    watchers: int | None,
    reach: Observation[float],
    *,
    added_days: float = 800.0,
    absent: bool = False,
) -> Facts:
    """A minimal Facts carrying only what the popularity gate reads.

    ``added_days`` is how long the item has been on the server -- the span an ALL-TIME
    count needs the mirror to cover, which the windowed gate does not read but the
    operator-authored lanes do (``Facts.days_since_added``). Defaulted well past the
    reaches these tests use, so a test that does not name it is exercising the window.

    ``absent`` makes the watcher count a genuine ``Absent`` rather than a ``Known`` zero --
    "the mirror was read and holds no play for this title", which is what the gate's own
    substitution of 0 stands in for and a different fact from a counted zero (rule 93).
    """
    counted: Observation[int] = (
        Absent(source="t") if absent else Known(value=watchers or 0, source="t")
    )
    return Facts(
        title="x",
        days_observed_unwatched=Known(value=900.0, source="t"),
        distinct_watchers=counted,
        distinct_watchers_all_time=counted,
        size_bytes=Known(value=1, source="t"),
        imdb_rating_tenths=Absent(source="t"),
        imdb_votes=Absent(source="t"),
        season_rank=Absent(source="t"),
        is_streaming_now=Known(value=False, source="t"),
        is_managed=Known(value=True, source="t"),
        in_curated_list=Absent(source="t"),
        is_whitelisted=Known(value=False, source="t"),
        history_reach_days=reach,
        days_since_added=Known(value=added_days, source="p"),
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

    gate = ServerPopularityGate(GateConfig(threshold=3, window_days=365))

    def test_a_history_shorter_than_the_window_cannot_report_nobody(self) -> None:
        """The bug, stated as the scan met it: a mirror three months deep, a title nobody
        played in those three months, and a year-long window. The gate must not answer."""
        result = self.gate.evaluate(_popularity_facts(0, Known(value=90.0, source="t")))

        assert result.outcome == ABSTAIN
        assert result.blocked is True
        assert "Nobody here watched it" not in result.detail
        assert "could not check who watched it in the last year" in result.detail
        assert "only goes back 3 months" in result.detail

    @pytest.mark.parametrize(
        "count", [3, 4, 10, 500], ids=["at-the-floor", "one-over", "ten", "many"]
    )
    def test_a_count_at_or_over_the_floor_protects(self, count: int) -> None:
        """The floor is a floor, not a target, and only a count ABOVE it can say so.

        Every other case here sits at the floor or under it, which left ``count >= floor``
        free to become ``count == floor``: the protection would then hold for a title watched
        by exactly three people and withdraw from one watched by five hundred, silently
        un-protecting the most-watched titles on the server while every test stayed green.
        A comparison needs a case on each side of it, and "well over" is the side a floor
        makes easy to forget.
        """
        result = self.gate.evaluate(_popularity_facts(count, Known(value=400.0, source="t")))

        assert result.outcome == PROTECT
        assert result.blocked is False

    @pytest.mark.parametrize(
        ("floor", "count", "phrase"),
        [
            (1, 1, "1 person"),
            (1, 2, "2 people"),
            (3, 1, "Only 1 person"),
            (3, 2, "Only 2 people"),
        ],
        ids=["one-watcher-protects", "two-watchers-protect", "one-watcher-short", "two-short"],
    )
    def test_one_watcher_is_a_person_on_both_the_kept_and_the_not_kept_line(
        self, floor: int, count: int, phrase: str
    ) -> None:
        """Both arms pluralize, and both were unpinned at a count of one.

        The gate says "1 person watched it" when the protection fires and "Only 1 person
        watched it" when it does not, and the two are separate expressions on separate
        lines. Every case here drove a count of 0, 2 or 3, so nothing distinguished the
        singular from the plural on either -- and deleting the not-kept line's assignment
        outright raises, which no test noticed either. Rule 21: an operator reading
        "1 people watched it" is reading a bug, in the panel whose job is to be believed.
        """
        gate = ServerPopularityGate(GateConfig(threshold=floor, window_days=365))

        result = gate.evaluate(_popularity_facts(count, Known(value=400.0, source="t")))

        assert phrase in result.detail

    def test_a_genuinely_absent_watcher_count_is_nobody_and_not_somebody(self) -> None:
        """``Absent`` means the mirror was read and holds no play for this title (rule 93),
        so it has to floor at zero watchers.

        The gate substitutes 0 for a non-``Known`` count, and with a watcher floor of 1 the
        difference between substituting 0 and substituting 1 is the difference between
        deleting an unwatched title and protecting the entire library. Nothing drove that
        substitution, because every other case here carries a ``Known`` count.
        """
        gate = ServerPopularityGate(GateConfig(threshold=1, window_days=365))

        result = gate.evaluate(_popularity_facts(None, Known(value=400.0, source="t"), absent=True))

        assert result.outcome == ABSTAIN
        assert result.blocked is False

    def test_a_count_between_one_and_the_floor_is_a_lower_bound_too(self) -> None:
        """Not only the zero case. Two watchers seen inside a partly-covered window says
        nothing about how many the uncovered part holds, so "only 2 people" is a claim
        about a year the gate did not see either."""
        result = self.gate.evaluate(_popularity_facts(2, Known(value=90.0, source="t")))

        assert result.blocked is True
        assert "Only 2 people" not in result.detail

    def test_the_block_stops_every_automatic_path_but_not_the_owners_own_hand(self) -> None:
        """What the block is for, and what it is not for -- the two asserted together,
        because the reach fix is only worth anything if the first survives the second.

        This test used to assert the reap was refused, and the refusal was carried by the
        gate *id*: ``server_popularity`` was absent from a list of gates a reap could settle.
        That list is gone. A block now means one thing -- Reaper could not answer a question
        -- and it answers it the same way for every automatic path: no scan condemns on it,
        no plan carries it, nothing is removed unattended. What it no longer does is refuse
        the one party better placed to answer than the scan was: an owner reading the panel
        that says, in the gate's own words, that their history does not go back far enough.
        Refusing them was the worse trade for safety, since a reap that always bounces is a
        file deleted outside Reaper with no journal and no interlocks.

        Nothing on the result plays any part in either answer, which is asserted rather than
        assumed: an earlier version read as though the wording were the interlock, and so
        could not fail when the wording changed."""
        assert GateId.SERVER_POPULARITY not in STRUCTURAL_GATES

        result = self.gate.evaluate(_popularity_facts(0, Known(value=90.0, source="t")))
        evaluation = Evaluation(results=[result])
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 1})

        assert result.blocked is True
        # Un-overridden, at a score that would otherwise condemn twice over.
        assert _verdict(evaluation, 100, 10_000, policy) == "abstain"
        # And the owner may still settle it by hand.
        assert _hand_reap(evaluation, 100, policy) == "condemn"

    def test_enough_watchers_still_protect_on_a_short_history(self) -> None:
        """The lower bound only ever *understates*. Three people seen inside the covered
        part did watch it within the window, so the protection is earned and fires -- and
        keeps its own wording, which the review chip parses (``routes._kept_phrase``)."""
        result = self.gate.evaluate(_popularity_facts(3, Known(value=90.0, source="t")))

        assert result.outcome == PROTECT
        assert result.blocked is False
        assert result.detail == "watched here: 3 people in the last year"

    def test_a_history_covering_the_window_answers_as_it_always_did(self) -> None:
        """The reach check changes nothing once the evidence is there. Two years of
        history over a one-year window is a real no-watchers finding, and it still
        reads as one."""
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

    def test_a_reach_just_under_the_window_never_reads_as_longer_than_it(self) -> None:
        """``humanize_days`` counts a month as 30 days and a year as 365, so 364 days
        renders "12 months, 4 days" while the 365-day window renders "year". Both are
        true, and printing them side by side told the operator their history goes back
        longer than the window Reaper says it cannot cover. Inside the margin the block
        states the comparison instead, which is shorter and cannot invert."""
        for reach in (336.0, 360.0, 364.0):
            result = self.gate.evaluate(_popularity_facts(0, Known(value=reach, source="t")))

            assert result.blocked is True
            assert result.detail == (
                "could not check who watched it in the last year: "
                "your watch history does not go back that far"
            ), f"reach {reach}"

        # A month or more short still names the number, which is the useful case: the
        # comparison alone would not tell them how much history they actually have.
        named = self.gate.evaluate(_popularity_facts(0, Known(value=335.0, source="t")))
        assert "only goes back 11 months, 5 days" in named.detail

    def test_a_shorter_window_the_history_does_cover_is_answerable(self) -> None:
        """The operator's remedy, and proof the check is against the *configured* window
        rather than a fixed span: narrowing the window to what the mirror holds gets a
        real answer back."""
        gate = ServerPopularityGate(GateConfig(threshold=3, window_days=90))
        result = gate.evaluate(_popularity_facts(0, Known(value=90.0, source="t")))

        assert result.blocked is False
        assert result.detail == "Nobody here watched it in the last 3 months."


class TestEveryReaderOfTheSameCountHonorsTheReach:
    """Rule 140's sweep. The reach was recorded and the gate above taught to fail closed
    past it, while three other readers of the same two counts went on reading them at full
    confidence: the operator's own protect rules, the graded keeps, and the built-in
    ``FEW_WATCHERS`` signal. A bound honored in one lane and silently not in the next is
    indistinguishable from the bug the bound was added to fix.

    The shipped window is 365 days throughout, and ``_popularity_facts`` puts the item on
    the server 800 days ago -- so a 90-day mirror can support neither count.
    """

    WINDOW = 365

    def _protect(self, field: str, value: int) -> fields.CustomProtectGate:
        return fields.CustomProtectGate(
            fields.Condition(field=field, op=fields.Op.GTE, value=value),
            window_days=self.WINDOW,
        )

    def test_an_operator_keep_rule_does_not_report_green_over_history_reaper_lacks(
        self,
    ) -> None:
        """The headline. A Tautulli installed three months ago, a title three people
        watched two years ago: no mirror rows, so ``watchers_all_time >= 1`` does not
        match and the panel used to say green, "checked your rule: 0 people have ever
        watched it, under your 1". The operator's own protection covered nothing."""
        result = self._protect("watchers_all_time", 1).evaluate(
            _popularity_facts(0, Known(value=90.0, source="t"))
        )

        assert result.outcome == ABSTAIN
        assert result.blocked is True  # amber, not green
        assert "checked your rule" not in result.detail
        assert result.detail.startswith("could not check")
        assert "only goes back 3 months" in result.detail

    def test_the_built_in_gate_and_an_operator_rule_agree_on_identical_facts(self) -> None:
        """Side by side, which is how the split was found: the built-in gate returned
        ABSTAIN blocked=True on exactly these Facts while the operator's own rule returned
        ABSTAIN blocked=False. One bound, one answer, whichever lane asks."""
        item = _popularity_facts(0, Known(value=90.0, source="t"))
        built_in = ServerPopularityGate(GateConfig(threshold=1, window_days=self.WINDOW)).evaluate(
            item
        )
        authored = self._protect("recent_watchers", 1).evaluate(item)

        assert built_in.blocked is authored.blocked is True

    def test_a_count_that_already_clears_the_rule_still_fires(self) -> None:
        """The asymmetry, and it is what keeps the bound from costing protections. A
        truncated count only ever UNDERSTATES, so three watchers already seen inside the
        covered part cannot be un-seen by a deeper mirror: the protection is earned and
        fires. Blocking here would withdraw a keep the operator's rule did win."""
        for field in ("recent_watchers", "watchers_all_time"):
            result = self._protect(field, 3).evaluate(
                _popularity_facts(3, Known(value=90.0, source="t"))
            )

            assert result.outcome == PROTECT, field
            assert result.blocked is False, field

    def test_a_mirror_that_covers_the_span_answers_exactly_as_before(self) -> None:
        """The check changes nothing once the evidence is there -- the same promise the
        gate's own reach test makes."""
        item = _popularity_facts(0, Known(value=1200.0, source="t"))

        for field in ("recent_watchers", "watchers_all_time"):
            result = self._protect(field, 1).evaluate(item)

            assert result.outcome == ABSTAIN, field
            assert result.blocked is False, field
            assert result.detail.startswith("checked your rule"), field

    def test_the_all_time_bound_is_the_items_own_age_not_the_window(self) -> None:
        """A year of history answers a year-long window, and still cannot say who has
        *ever* watched a file that arrived 800 days ago. Reading the two counts against
        one span is what made the all-time half look already fixed.

        This is also why dormancy cannot stand in for the arrival date:
        ``days_observed_unwatched`` is clamped to the mirror's edge, so it can never
        exceed the reach and comparing the two would pronounce every all-time count
        complete."""
        item = _popularity_facts(0, Known(value=400.0, source="t"), added_days=800.0)

        assert self._protect("recent_watchers", 1).evaluate(item).blocked is False
        assert self._protect("watchers_all_time", 1).evaluate(item).blocked is True

    def test_an_unrecorded_arrival_date_blocks_rather_than_assuming(self) -> None:
        """A snapshot frozen before ``days_since_added`` was a fact thaws it Unknown
        (rule 104). That is not permission to claim the mirror covers the item's life."""
        item = replace(
            _popularity_facts(0, Known(value=1200.0, source="t")),
            days_since_added=Unknown(reason="this scan did not record it", source="snapshot"),
        )

        assert self._protect("watchers_all_time", 1).evaluate(item).blocked is True

    def test_the_window_reaches_the_rule_through_the_real_gate_builder(self) -> None:
        """The plumbing, not a hand-built gate. ``build_gates`` is what hands the policy's
        window to each authored protection; without it every such rule would read as
        un-establishable and block the whole library. Both directions are asserted, so a
        window that never arrives cannot pass as the bound working."""
        from reaper.engine.policy import DEFAULT_MOVIE_POLICY, ConditionSpec
        from reaper.services.scan_runner import build_gates

        policy = DEFAULT_MOVIE_POLICY.model_copy(
            update={
                "protect_conditions": (
                    ConditionSpec(field="recent_watchers", op=fields.Op.GTE, value=1),
                )
            }
        )
        authored = [g for g in build_gates(policy) if isinstance(g, fields.CustomProtectGate)]
        assert len(authored) == 1
        assert authored[0].window_days == policy.popularity_window_days()

        short = authored[0].evaluate(_popularity_facts(0, Known(value=90.0, source="t")))
        covered = authored[0].evaluate(_popularity_facts(0, Known(value=1200.0, source="t")))

        assert short.blocked is True
        assert covered.blocked is False

    def test_a_graded_keep_on_a_truncated_count_takes_the_full_discount(self) -> None:
        """The second reader. ``evaluate_keep`` already gives the full discount on an
        Unknown, so the correct treatment existed and was bypassed on a Known(0) the
        mirror cannot support: the keep quietly shrank to nothing on evidence nobody has.
        """
        keep = KeepConfig(
            name="Keep what people have watched",
            max_discount=30,
            field="watchers_all_time",
            floor=0,
            saturate_at=3,
        )

        short = evaluate_keep(
            keep, _popularity_facts(0, Known(value=90.0, source="t")), window_days=self.WINDOW
        )
        covered = evaluate_keep(
            keep, _popularity_facts(0, Known(value=1200.0, source="t")), window_days=self.WINDOW
        )

        assert short.discount == 30.0
        assert short.evaluated is False
        assert "could not check" in short.detail
        # ...and a mirror that does cover it reports the real, zero, discount.
        assert covered.discount == 0.0
        assert covered.evaluated is True

    def test_few_watchers_withdraws_its_pressure_and_lets_coverage_see_the_hole(self) -> None:
        """The third reader, and the one that can condemn. The signal read the same
        truncated count for its PRESSURE, and ``score()`` counted it evaluated because the
        input was Known -- so its weight landed in the numerator AND coverage resolved to
        1.0, which is what kept ``coverage_floor_bp`` from ever seeing the gap.
        """
        configs = [
            SignalConfig(SignalId.UNWATCHED, weight=80, saturate_at=730),
            SignalConfig(SignalId.FEW_WATCHERS, weight=20, saturate_at=3),
        ]
        short = score(
            configs, _popularity_facts(0, Known(value=90.0, source="t")), window_days=self.WINDOW
        )
        covered = score(
            configs, _popularity_facts(0, Known(value=1200.0, source="t")), window_days=self.WINDOW
        )

        few = {r.signal: r for r in short.results}[SignalId.FEW_WATCHERS]
        few_covered = {r.signal: r for r in covered.results}[SignalId.FEW_WATCHERS]

        # What it used to do on the 90-day mirror, and what it correctly does once the
        # mirror reaches the identical plays: full pressure, reported at total coverage.
        assert few_covered.pressure == 20.0
        assert covered.coverage == 1.0

        assert few.pressure == 0.0
        assert few.evaluated is False
        assert few.state is SignalState.UNREADABLE
        # The weight stays in the denominator, so the hole is visible to the coverage
        # floor instead of hidden behind a coverage of 1.0 (rule 31).
        assert short.coverage == pytest.approx(0.8)
        assert "could not tell who watched it" in few.detail

    def test_a_count_we_could_not_read_names_its_own_cause_not_the_mirror_s_depth(
        self,
    ) -> None:
        """A season Plex never matched, on a server whose mirror is short.

        Two things are wrong at once and only one of them is the operator's problem. The
        reach check must not answer for an input that was never readable: told "your watch
        history only goes back 3 months", an operator goes and lengthens their Tautulli
        retention, and the season is still unmatched afterwards. Both twins resolve the
        unreadable input first (``gates.ServerPopularityGate``'s ``_blocked``,
        ``fields.evaluate``); this pins the third against them (rule 72).
        """
        facts = replace(
            _popularity_facts(0, Known(value=90.0, source="t")),
            distinct_watchers=Unknown(reason="Plex has not matched this season", source="p"),
        )
        result = evaluate_signal(
            SignalConfig(SignalId.FEW_WATCHERS, weight=20, saturate_at=3),
            facts,
            window_days=self.WINDOW,
        )

        # Same numbers either way -- withheld, weight retained. Only the sentence differs.
        assert result.pressure == 0.0
        assert result.evaluated is False
        assert result.state is SignalState.UNREADABLE
        assert result.detail == "could not tell who watched it"
        assert "watch history only goes back" not in result.detail

    def test_a_genuine_absence_is_still_bounded_by_the_mirror_that_observed_it(self) -> None:
        """The exemption above is for ``Unknown`` ONLY, and this is why.

        "We looked and there is genuinely nothing" is a claim about the window. A mirror
        that does not span the window cannot support it -- it establishes only that nothing
        happened in the part it holds -- so rule 93's precondition (a GENUINE absence) is
        not met and rule 140 governs as the more specific. Exempting ``Absent`` alongside
        ``Unknown`` would report coverage 1.0 on a count the mirror cannot establish, which
        is the hole this whole branch exists to close, and would put this signal on the
        opposite side of its own twin: ``ServerPopularityGate._blocked`` matches ``Unknown``
        only, so an ``Absent`` count falls through to the gate's shortfall arm and blocks.
        """
        cfg = SignalConfig(SignalId.FEW_WATCHERS, weight=20, saturate_at=3)
        absent = replace(
            _popularity_facts(0, Known(value=90.0, source="t")),
            distinct_watchers=Absent(source="t"),
        )
        short = evaluate_signal(cfg, absent, window_days=self.WINDOW)

        assert short.state is SignalState.UNREADABLE
        assert short.evaluated is False
        assert "watch history only goes back" in short.detail

        # ...and once the mirror does span the window, the same absence is real evidence:
        # evaluated, weight retained, coverage intact, no pressure (rule 93).
        covered = evaluate_signal(
            cfg,
            replace(absent, history_reach_days=Known(value=1200.0, source="t")),
            window_days=self.WINDOW,
        )
        assert covered.state is SignalState.NOT_APPLICABLE
        assert covered.evaluated is True
        assert covered.pressure == 0.0

    def test_a_graded_removal_rule_on_the_same_count_is_bounded_too(self) -> None:
        """The authored twin of the signal above. A removal rule graded on "people who
        watched it recently" charges pressure off the same lower bound, so it takes the
        same treatment: none, with the weight retained."""
        rule = CustomSignalConfig(
            name="Nobody watches it",
            weight=20,
            kind="graded",
            field="recent_watchers",
            floor=0,
            saturate_at=3,
        )
        short = score(
            [],
            _popularity_facts(0, Known(value=90.0, source="t")),
            custom_condemn=[rule],
            window_days=self.WINDOW,
        )

        assert short.results[0].pressure == 0.0
        assert short.results[0].evaluated is False
        assert short.coverage == 0.0

    def test_a_condemn_rule_that_already_exceeds_its_bar_is_still_measured(self) -> None:
        """The asymmetry on the condemn lane. "at most 2 watchers" that a truncated count
        already EXCEEDS stays exceeded however much more history arrives, so it is a real
        finding and reads as one -- the bound withholds only what a deeper mirror could
        overturn."""
        item = _popularity_facts(9, Known(value=90.0, source="t"))

        exceeded = fields.evaluate(
            fields.Condition(field="recent_watchers", op=fields.Op.LTE, value=2),
            item,
            window_days=self.WINDOW,
        )
        # ...while the matching direction, which a deeper mirror could overturn, does not.
        overturnable = fields.evaluate(
            fields.Condition(field="recent_watchers", op=fields.Op.LTE, value=20),
            item,
            window_days=self.WINDOW,
        )

        assert exceeded.blocked is False
        assert exceeded.matched is False
        assert overturnable.blocked is True


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
#: protect-lane only, read by ``StreamingNowGate`` or by an operator-authored protect
#: rule (``whitelisted``, ``on_list`` -> ``fields.CustomProtectGate``), each of which
#: returns PROTECT or ABSTAIN and can never add pressure. Move one into the sweep the
#: day a signal, custom rule or keep above references it.
_GATE_ONLY = frozenset({"on_lists", "is_whitelisted", "is_streaming_now"})


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
    probe = Facts(title="probe", **{n: Known(value=n, source="probe") for n in names})  # type: ignore[arg-type]
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
            **{  # type: ignore[arg-type]
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
    @settings(max_examples=500, deadline=None)
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
                replace(item, **{field_name: Unknown(reason="outage", source="test")})  # type: ignore[arg-type]
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
    @settings(max_examples=500, deadline=None)
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
    @settings(max_examples=500, deadline=None)
    def test_coverage_cannot_rise_when_evidence_is_lost(self, item: Facts) -> None:
        """Coverage measures what we could read, so it can only fall as we read less.

        Stated separately from the score because a model can hold the score bound
        while inflating coverage, and coverage is what the abstain floor consults.
        """
        baseline = _full_score(item).coverage

        for field_name in _OBSERVED_FIELDS:
            degraded = _full_score(
                replace(item, **{field_name: Unknown(reason="outage", source="test")})  # type: ignore[arg-type]
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
    @settings(max_examples=200, deadline=None)
    def test_the_score_stays_in_bounds(self, item: Facts) -> None:
        """0-100 and 0-1, whatever the evidence. Every consumer assumes both."""
        result = _full_score(item)

        assert 0.0 <= result.value <= MAX_SCORE
        assert 0.0 <= result.coverage <= 1.0
        assert 0.0 <= result.base_value <= MAX_SCORE
