# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backtesting.

A backtest flatters itself unless you are careful, and a flattering backtest is
worse than none: it gives the owner false confidence in a threshold that will eat
their library. These tests pin the honesty.
"""

from __future__ import annotations

from datetime import timedelta

from reaper.clock import utcnow
from reaper.engine.backtest import Item, facts_as_of
from reaper.engine.observation import Known

NOW = utcnow()
CUTOFF = NOW - timedelta(days=365)
HORIZON = NOW - timedelta(days=3000)


def _item(added_days_ago: int = 1000) -> Item:
    return Item(
        rating_key=1,
        title="A Film",
        size_bytes=8_000_000_000,
        added_at=NOW - timedelta(days=added_days_ago),
        imdb_rating_tenths=73,
        imdb_votes=500_000,
    )


class TestFactsAreRebuiltAsOfTheCutoff:
    """The point of a backtest is to judge with the information available AT THE
    TIME. Using today's facts would just re-derive today's answer."""

    def test_a_play_after_the_cutoff_is_invisible_to_the_scorer(self) -> None:
        """The item was watched a month ago -- but we are judging it as of a year
        ago, when it had sat untouched. The scorer must not see the future."""
        plays = [(100, NOW - timedelta(days=30), "alice")]

        facts = facts_as_of(_item(), plays, cutoff=CUTOFF, horizon=HORIZON)

        assert facts is not None
        assert isinstance(facts.distinct_watchers, Known)
        assert facts.distinct_watchers.value == 0  # nobody had watched it *by then*

    def test_days_unwatched_is_measured_from_the_cutoff_not_from_today(self) -> None:
        plays = [(100, NOW - timedelta(days=500), "alice")]  # 135 days before cutoff

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
            (1, CUTOFF - timedelta(days=1500), "a"),  # long before the window
            (2, CUTOFF - timedelta(days=1400), "b"),
            (3, CUTOFF - timedelta(days=1300), "c"),
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
            (1, CUTOFF - timedelta(days=30), "a"),
            (2, CUTOFF - timedelta(days=60), "b"),
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
            (1, CUTOFF - timedelta(days=30), "a"),
            (1, CUTOFF - timedelta(days=20), "a"),
        ]

        facts = facts_as_of(_item(added_days_ago=2000), plays, cutoff=CUTOFF, horizon=HORIZON)

        assert facts is not None
        assert isinstance(facts.distinct_watchers, Known)
        assert facts.distinct_watchers.value == 1


class TestTheRequesterGateIsNotApplied:
    """`others_watching` means "somebody other than the person who requested it".

    In a general policy there is no requester, so "others" is everyone -- and the
    gate would protect anything ever played by anyone, silently disabling the
    scorer. It is Absent here, deliberately.
    """

    def test_others_watching_is_absent_in_a_backtest(self) -> None:
        facts = facts_as_of(
            _item(), [(1, CUTOFF - timedelta(days=1), "a")], cutoff=CUTOFF, horizon=HORIZON
        )

        assert facts is not None
        assert not isinstance(facts.others_watching, Known)
