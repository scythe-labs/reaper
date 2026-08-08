# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backtesting.

A backtest flatters itself unless you are careful, and a flattering backtest is
worse than none: it gives the owner false confidence in a threshold that will eat
their library. These tests pin the honesty.
"""

from __future__ import annotations

from datetime import timedelta

from reaper.clock import utcnow
from reaper.engine.backtest import (
    NO_ADDED_AT_REASON,
    BacktestResult,
    Item,
    facts_as_of,
    rewatch_prior,
)
from reaper.engine.calibration import Bucket, RewatchPrior
from reaper.engine.gates import GateConfig, ServerPopularityGate
from reaper.engine.observation import Known, Unknown

NOW = utcnow()
CUTOFF = NOW - timedelta(days=365)
HORIZON = NOW - timedelta(days=3000)


def _item(added_days_ago: int | None = 1000) -> Item:
    """``added_days_ago=None`` is a record Plex reports no arrival date for."""
    return Item(
        rating_key=1,
        title="A Film",
        size_bytes=8_000_000_000,
        added_at=None if added_days_ago is None else NOW - timedelta(days=added_days_ago),
        imdb_rating_tenths=73,
        imdb_votes=500_000,
    )


def _empty_bucket_prior() -> RewatchPrior:
    """Calibrated as a whole (no THIN buckets), but the old-age bucket is EMPTY --
    nothing in the history is that old, so it has no rate at all."""
    return RewatchPrior(
        buckets=(
            Bucket(low=0, high=365, samples=40, rewatched=10),
            Bucket(low=365, high=10**9, samples=0, rewatched=0),
        ),
        population=40,
        window_days=365,
        computed_at=NOW,
    )


class TestExpectedRegretRateWithEmptyBuckets:
    """One condemned item landing in an empty bucket must not crash the report --
    ``rate_for`` deliberately raises for it, so that item borrows the fallback curve."""

    def test_an_item_in_an_empty_bucket_borrows_the_fallback(self) -> None:
        result = BacktestResult(cutoff=CUTOFF, condemn_at=70, prior=_empty_bucket_prior())
        result.condemned_dormancy = [400.0]  # lands in the empty bucket

        assert result.prior_is_derived is True
        assert result.expected_regret_rate == rewatch_prior(400.0)
        assert result.expected_rate_borrowed_items == 1

    def test_mixed_provenance_is_averaged_and_reported(self) -> None:
        result = BacktestResult(cutoff=CUTOFF, condemn_at=70, prior=_empty_bucket_prior())
        result.condemned_dormancy = [100.0, 400.0]
        result.condemned.append(("A Film", 90.0, 1_000))

        expected = (0.25 + rewatch_prior(400.0)) / 2
        assert abs(result.expected_regret_rate - expected) < 1e-9
        assert result.expected_rate_borrowed_items == 1
        # summary() funnels through the same rates -- it must render, not raise, and
        # it must say the baseline is partly borrowed.
        assert "borrow the fallback curve" in result.summary()


class TestFactsAreRebuiltAsOfTheCutoff:
    """The point of a backtest is to judge with the information available AT THE
    TIME. Using today's facts would just re-derive today's answer."""

    def test_a_play_after_the_cutoff_is_invisible_to_the_scorer(self) -> None:
        """The item was watched a month ago -- but we are judging it as of a year
        ago, when it had sat untouched. The scorer must not see the future."""
        plays = [(100, NOW - timedelta(days=30))]

        facts = facts_as_of(_item(), plays, cutoff=CUTOFF, horizon=HORIZON)

        assert facts is not None
        assert isinstance(facts.distinct_watchers, Known)
        assert facts.distinct_watchers.value == 0  # nobody had watched it *by then*

    def test_days_unwatched_is_measured_from_the_cutoff_not_from_today(self) -> None:
        plays = [(100, NOW - timedelta(days=500))]  # 135 days before cutoff

        facts = facts_as_of(_item(), plays, cutoff=CUTOFF, horizon=HORIZON)

        assert facts is not None
        assert isinstance(facts.days_observed_unwatched, Known)
        assert round(facts.days_observed_unwatched.value) == 135

    def test_an_item_added_after_the_cutoff_cannot_be_judged(self) -> None:
        """It did not exist. Judging it would be fiction, so it is skipped and
        counted -- the backtest's coverage is visible, not assumed."""
        assert facts_as_of(_item(added_days_ago=100), [], cutoff=CUTOFF, horizon=HORIZON) is None

    def test_a_never_played_item_counts_from_when_it_arrived(self) -> None:
        """Not from epoch 0. The whole trap: 'days since last play' is null for
        exactly the items we care most about, and coercing null to 1970 reads as
        ~20,600 days unwatched -- maximum condemnation pressure for the item we know
        least about."""
        facts = facts_as_of(_item(added_days_ago=1000), [], cutoff=CUTOFF, horizon=HORIZON)

        assert facts is not None
        assert isinstance(facts.days_observed_unwatched, Known)
        assert round(facts.days_observed_unwatched.value) == 635  # 1000 - 365

    def test_the_reach_is_measured_from_the_cutoff_not_from_today(self) -> None:
        """A replay standing at the cutoff had only the history that existed then.

        The reach is what tells ``ServerPopularityGate`` whether a windowed watcher count
        covers the window it names, so measuring it from today would hand the rehearsal
        evidence the live scan did not have at that date -- the gate would answer where
        production could not, and the backtest would flatter the policy.
        """
        facts = facts_as_of(_item(added_days_ago=1000), [], cutoff=CUTOFF, horizon=HORIZON)

        assert facts is not None
        assert isinstance(facts.history_reach_days, Known)
        # 3000 (horizon) - 365 (cutoff), not 3000.
        assert round(facts.history_reach_days.value) == 2635

    def test_a_mirror_too_young_for_the_window_is_reported_not_scored_as_zero(self) -> None:
        """A rehearsal that could not run must not read as a policy that deletes nothing.

        With the mirror installed 500 days ago and a cutoff a year back, only 135 days of
        history stood behind the popularity window at that date, so the count is a lower
        bound and every item blocks. The old summary printed "would have deleted 0 items"
        and "No regrets", which is what a maximally cautious policy also prints.
        """
        recent_horizon = NOW - timedelta(days=500)
        facts = facts_as_of(_item(added_days_ago=1000), [], cutoff=CUTOFF, horizon=recent_horizon)

        assert facts is not None
        assert isinstance(facts.history_reach_days, Known)
        assert round(facts.history_reach_days.value) == 135

        gate = ServerPopularityGate(GateConfig(threshold=3, window_days=365))
        assert gate.evaluate(facts).blocked is True

        result = BacktestResult(cutoff=CUTOFF, condemn_at=70, considered=4, blocked_unreadable=4)
        assert "could not check      4" in result.summary()
        assert "100% of them" in result.summary()

    def test_a_run_with_nothing_blocked_stays_quiet_about_it(self) -> None:
        """The line is a report of trouble, so it must not appear when there is none."""
        result = BacktestResult(cutoff=CUTOFF, condemn_at=70, considered=4)

        assert "could not check" not in result.summary()

    def test_a_never_played_item_older_than_the_horizon_counts_from_the_horizon(
        self,
    ) -> None:
        """We cannot claim it went unwatched for longer than we have been watching.
        Tautulli cannot import history from before it was installed, so anything
        older than the horizon has no evidence either way."""
        ancient = Item(
            rating_key=1,
            title="Old",
            size_bytes=1,
            added_at=NOW - timedelta(days=5000),  # older than the horizon
            imdb_rating_tenths=70,
            imdb_votes=1000,
        )
        recent_horizon = NOW - timedelta(days=700)

        facts = facts_as_of(ancient, [], cutoff=CUTOFF, horizon=recent_horizon)

        assert facts is not None
        assert isinstance(facts.days_observed_unwatched, Known)
        # 700 (horizon) - 365 (cutoff) = 335, NOT 5000-365.
        assert round(facts.days_observed_unwatched.value) == 335


class TestPopularityIsWindowed:
    """The bug the first real backtest exposed.

    Counting watchers over ALL TIME protects a film that five people watched years ago
    and nobody has touched since -- which is exactly the film we exist to find. On a
    long-lived server the overwhelming majority of titles clear an all-time watcher
    threshold, and only a fraction clear the same threshold within the last year.

    So the unwindowed version silently disables the entire scorer: a backtest at every
    threshold then finds almost nothing to delete, and the tool looks "safe" while
    being broken.
    """

    def test_old_watchers_do_not_count_toward_recent_popularity(self) -> None:
        plays = [
            (1, CUTOFF - timedelta(days=1500)),  # long before the window
            (2, CUTOFF - timedelta(days=1400)),
            (3, CUTOFF - timedelta(days=1300)),
        ]

        facts = facts_as_of(
            _item(added_days_ago=2000),
            plays,
            cutoff=CUTOFF,
            horizon=HORIZON,
            popularity_window_days=365,
        )

        assert facts is not None
        assert isinstance(facts.distinct_watchers, Known)
        assert facts.distinct_watchers.value == 0  # none within the window

        # ...but the all-time count is still available, for display.
        assert isinstance(facts.distinct_watchers_all_time, Known)
        assert facts.distinct_watchers_all_time.value == 3

    def test_recent_watchers_do_count(self) -> None:
        plays = [
            (1, CUTOFF - timedelta(days=30)),
            (2, CUTOFF - timedelta(days=60)),
        ]

        facts = facts_as_of(
            _item(added_days_ago=2000),
            plays,
            cutoff=CUTOFF,
            horizon=HORIZON,
            popularity_window_days=365,
        )

        assert facts is not None
        assert isinstance(facts.distinct_watchers, Known)
        assert facts.distinct_watchers.value == 2

    def test_the_same_user_twice_is_one_watcher(self) -> None:
        plays = [
            (1, CUTOFF - timedelta(days=30)),
            (1, CUTOFF - timedelta(days=20)),
        ]

        facts = facts_as_of(_item(added_days_ago=2000), plays, cutoff=CUTOFF, horizon=HORIZON)

        assert facts is not None
        assert isinstance(facts.distinct_watchers, Known)
        assert facts.distinct_watchers.value == 1


class TestARecordWithNoArrivalDateIsStillRehearsed:
    """The rehearsal takes the live scan's thaw, not one of its own (#277).

    `reference_instant` measures from a play alone, so a missing arrival date is not on its
    own a reason to refuse. This lane used to drop those records before asking, which is a
    second thaw rule wearing a filter's clothes: a movie with no arrival date and an old play
    is condemned by a live scan and never entered the rehearsal at all, so the replay
    under-reported precisely what production would remove.
    """

    def test_a_play_is_enough_to_judge_a_record_with_no_arrival_date(self) -> None:
        """The whole defect, in one item. 500 days before NOW is 135 before the cutoff."""
        plays = [(7, NOW - timedelta(days=500))]

        facts = facts_as_of(_item(added_days_ago=None), plays, cutoff=CUTOFF, horizon=HORIZON)

        assert facts is not None, "a record with a play has an instant to measure from"
        assert isinstance(facts.days_observed_unwatched, Known)
        assert round(facts.days_observed_unwatched.value) == 135

    def test_that_record_reports_no_span_for_its_all_time_count(self) -> None:
        """The one field a play cannot stand in for: how far back the mirror must reach for
        an all-time count is a question about the arrival, and Unknown withholds the
        protection rather than asserting a span nobody measured (rule 93)."""
        facts = facts_as_of(
            _item(added_days_ago=None),
            [(7, NOW - timedelta(days=500))],
            cutoff=CUTOFF,
            horizon=HORIZON,
        )

        assert facts is not None
        assert isinstance(facts.days_since_added, Unknown)
        assert facts.days_since_added.reason == NO_ADDED_AT_REASON

    def test_neither_a_play_nor_an_arrival_date_is_still_refused(self) -> None:
        """The narrowing goes, the honesty stays: with no instant to measure from there is
        no rehearsal to run, and inventing one would score the item on nothing."""
        assert facts_as_of(_item(added_days_ago=None), [], cutoff=CUTOFF, horizon=HORIZON) is None

    def test_a_play_after_the_cutoff_does_not_thaw_it(self) -> None:
        """`past` is filtered to the cutoff first, so a play the replay must not see cannot
        smuggle the record in through the arrival-date arm either."""
        plays = [(7, NOW - timedelta(days=30))]  # 335 days AFTER the cutoff

        assert (
            facts_as_of(_item(added_days_ago=None), plays, cutoff=CUTOFF, horizon=HORIZON) is None
        )


class TestTheReplayRefusesPlaysTheMirrorCanNoLongerShow:
    """The rehearsal's copy of `snapshot.build_facts`'s blind arms (#277).

    A re-added file carries a fresh rating key while its earlier plays stay filed under the
    old one, so "no rows" is ambiguous between churn and a genuinely unwatched item. The live
    scan refuses to read that as zero; so must the replay, and doubly so, because the same
    churn hides the LATER plays -- a replay that condemns such an item records no regret for
    it either and reports the policy as safer than it is.
    """

    BLIND = "plays recorded on an earlier scan are no longer readable"

    def test_dormancy_and_both_counts_go_unknown_together(self) -> None:
        """One mirror answered all three, so one doubt withdraws all three."""
        facts = facts_as_of(
            _item(added_days_ago=2000),
            [(7, CUTOFF - timedelta(days=10))],
            cutoff=CUTOFF,
            horizon=HORIZON,
            watch_blind_reason=self.BLIND,
        )

        assert facts is not None
        for observation in (
            facts.days_observed_unwatched,
            facts.distinct_watchers,
            facts.distinct_watchers_all_time,
        ):
            assert isinstance(observation, Unknown)
            assert observation.reason == self.BLIND

    def test_the_doubt_beats_a_reading_that_would_have_measured(self) -> None:
        """Checked BEFORE the measurement, not after it. The same item read honestly comes
        back Known, so this pins the order rather than an item that had nothing to say."""
        item, plays = _item(added_days_ago=2000), [(7, CUTOFF - timedelta(days=10))]

        honest = facts_as_of(item, plays, cutoff=CUTOFF, horizon=HORIZON)
        assert honest is not None
        assert isinstance(honest.days_observed_unwatched, Known)

        blinded = facts_as_of(
            item, plays, cutoff=CUTOFF, horizon=HORIZON, watch_blind_reason=self.BLIND
        )
        assert blinded is not None
        assert isinstance(blinded.days_observed_unwatched, Unknown)

    def test_a_blind_item_is_still_rehearsed_rather_than_dropped(self) -> None:
        """Unknown blocks the dormancy gates and abstains, which keeps the file. Dropping it
        instead would take it out of the coverage count as well, hiding the population this
        doubt exists for from the one report that should show it."""
        facts = facts_as_of(
            _item(added_days_ago=None),
            [],
            cutoff=CUTOFF,
            horizon=HORIZON,
            watch_blind_reason=self.BLIND,
        )

        assert facts is not None, "no arrival date and no readable play is still a rehearsal"


# A TestTheRequesterGateIsNotApplied class lived here, pinning that a backtest leaves
# `others_watching` Absent so the requester gate could not protect everything ever played.
# The gate and the fact are both gone (no builder ever produced a Known value, so it could
# never protect anything); the invariant it guarded went with them.
