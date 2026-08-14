# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rewatch derivation: what counts as a play, and how plays cluster into viewings.

Every number here is a ratio or a shape, never a real title or host (the repo's golden
rule): all fixtures use placeholder rating keys.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clock import from_epoch, utcnow
from reaper.config import Settings
from reaper.db.session import create_engine
from reaper.engine.gates import REWATCH_BLOCK_FLOOR_N, Facts
from reaper.engine.observation import Absent, Known, Unknown
from reaper.services import history_sync, lists, rewatch, snapshot
from reaper.services.snapshot import RawItem, ScanContext, build_facts

DAY = 86_400
NOW_EPOCH = 1_700_000_000  # an arbitrary, fixed instant; only offsets from it matter


def _at(epoch: int) -> datetime:
    """``from_epoch``, unwrapped for a caller that needs a plain ``datetime`` (a required
    ``cutoff`` argument): every epoch this module passes is a fixed nonzero constant, so the
    null-epoch case ``from_epoch`` guards against never applies here."""
    dt = from_epoch(epoch)
    assert dt is not None
    return dt


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))
    yield eng
    await eng.dispose()


async def _insert(
    engine: AsyncEngine,
    *,
    rating_key: int,
    watched_at: int,
    watched_status: float | None,
    percent_complete: int,
    media_type: str = "movie",
    user_id: int = 1,
    grandparent_rating_key: int | None = None,
) -> None:
    await history_sync.ensure_schema(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO watch_event "
                "(rating_key, grandparent_rating_key, user_id, watched_at, watched_status, "
                " percent_complete, media_type) "
                "VALUES (:rating_key, :grandparent_rating_key, :user_id, :watched_at, "
                " :watched_status, :percent_complete, :media_type)"
            ),
            {
                "rating_key": rating_key,
                "grandparent_rating_key": grandparent_rating_key,
                "user_id": user_id,
                "watched_at": watched_at,
                "watched_status": watched_status,
                "percent_complete": percent_complete,
                "media_type": media_type,
            },
        )


class TestQualifiesTable:
    """rule: the play filter, exact table from ``docs/history/REWATCH_PLAN.md`` Stage 1."""

    @pytest.mark.parametrize(
        ("watched_status", "percent_complete", "expected"),
        [
            # Both uninformative: unknown resolves toward keeping, so the play counts.
            (None, 0, True),
            # Abandoned play: no status, and under half complete. Load-bearing per
            # docs/LEARNINGS.md -- unfiltered, these are what fake most apparent cycles.
            (None, 30, False),
            (None, 49, False),
            (None, 50, True),
            (None, 100, True),
            # A definite status of 0 is a definite non-qualifying play, distinct from
            # "no status at all" even though percent_complete reads the same 0 either way.
            (0.0, 0, False),
            # watched_status decides it once reported, whatever percent_complete says.
            (0.49, 100, False),
            (0.5, 0, True),
            (1.0, 0, True),
        ],
    )
    def test_the_table(
        self, watched_status: float | None, percent_complete: int, expected: bool
    ) -> None:
        assert rewatch.qualifies(watched_status, percent_complete) is expected


class TestViewingClustering:
    def test_no_plays_is_zero_viewings(self) -> None:
        assert rewatch.viewing_count([]) == 0

    def test_one_play_is_one_viewing(self) -> None:
        assert rewatch.viewing_count([datetime(2026, 1, 1, tzinfo=UTC)]) == 1

    def test_equal_timestamps_share_a_viewing(self) -> None:
        t = datetime(2026, 1, 1, tzinfo=UTC)
        assert rewatch.viewing_count([t, t, t]) == 1

    def test_exactly_the_gap_shares_a_viewing(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        later = start + timedelta(days=rewatch.VIEWING_GAP_DAYS)
        assert rewatch.viewing_count([start, later]) == 1

    def test_one_second_past_the_gap_splits(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        later = start + timedelta(days=rewatch.VIEWING_GAP_DAYS, seconds=1)
        assert rewatch.viewing_count([start, later]) == 2

    def test_clusters_from_the_previous_play_not_the_viewing_start(self) -> None:
        """Three plays six days apart each: none crosses the gap from its own previous
        play, so this is one viewing, even though the first and last are 12 days apart."""
        start = datetime(2026, 1, 1, tzinfo=UTC)
        plays = [start, start + timedelta(days=6), start + timedelta(days=12)]
        assert rewatch.viewing_count(plays) == 1

    def test_unsorted_input_is_sorted_first(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        later = start + timedelta(days=1)
        assert rewatch.viewing_count([later, start]) == 1


_PERIOD_START = datetime(2026, 1, 1, tzinfo=UTC)


def _play(days: float, episode: int, *, seconds: float = 0) -> tuple[datetime, int]:
    """One ``(play time, episode identity)`` pair, ``days``/``seconds`` offset from a fixed
    origin -- only the offsets between plays matter to ``replay_period_count``."""
    return _PERIOD_START + timedelta(days=days, seconds=seconds), episode


class TestReplayPeriodCount:
    """rule: the TV period/replay derivation (docs/history/REWATCH_PLAN.md, TV section;
    docs/LEARNINGS.md, "TV: the replay-period formulation clears the lift bar"). Expectations
    are written from that spec, never from the implementation (rule 119)."""

    def test_empty_input_is_zero(self) -> None:
        assert rewatch.replay_period_count([]) == 0

    def test_one_period_first_watch_only_is_zero(self) -> None:
        # Three new episodes, all inside the gap: one period, and a first period never has
        # anything in `seen` to overlap against.
        plays = [_play(0, 1), _play(1, 2), _play(2, 3)]
        assert rewatch.replay_period_count(plays) == 0

    def test_two_periods_of_the_same_episodes_is_one_replay(self) -> None:
        plays = [
            _play(0, 1),
            _play(1, 2),  # period 1: {1, 2}, first period, not a replay; seen = {1, 2}
            _play(40, 1),
            _play(41, 2),  # period 2: {1, 2} again, full overlap -> replay
        ]
        assert rewatch.replay_period_count(plays) == 1

    def test_quarter_boundary_four_distinct_one_seen_is_a_replay(self) -> None:
        plays = [
            _play(0, 1),  # period 1: {1}, seeds `seen`
            _play(40, 1),
            _play(41, 2),
            _play(42, 3),
            _play(43, 4),  # period 2: {1, 2, 3, 4}, overlap 1 of 4 -- exactly a quarter
        ]
        assert rewatch.replay_period_count(plays) == 1

    def test_quarter_boundary_five_distinct_one_seen_is_not_a_replay(self) -> None:
        plays = [
            _play(0, 1),
            _play(40, 1),
            _play(41, 2),
            _play(42, 3),
            _play(43, 4),
            _play(44, 5),  # period 2: {1..5}, overlap 1 of 5 -- under a quarter
        ]
        assert rewatch.replay_period_count(plays) == 0

    def test_a_single_episode_period_is_never_a_replay_even_with_full_overlap(self) -> None:
        plays = [
            _play(0, 1),
            _play(1, 2),  # period 1: {1, 2}, not a replay; seen = {1, 2}
            _play(40, 1),  # period 2: {1} alone -- 100% overlap, but only one distinct episode
        ]
        assert rewatch.replay_period_count(plays) == 0

    def test_a_single_episode_first_period_feeds_seen_for_the_second(self) -> None:
        """Period 1 holds one episode and is never itself a replay (the floor above), but
        its identity still lands in `seen`: period 2 clears the quarter bar only because of
        it (without period 1, its overlap would be zero)."""
        plays = [
            _play(0, 9),  # period 1: {9} alone, not a replay
            _play(40, 9),
            _play(41, 10),
            _play(42, 11),
            _play(43, 12),  # period 2: {9, 10, 11, 12}, overlap 1 of 4 via episode 9
        ]
        assert rewatch.replay_period_count(plays) == 1

    def test_exactly_the_gap_shares_a_period(self) -> None:
        gap = rewatch.SHOW_PERIOD_GAP_DAYS
        plays = [
            _play(0, 1),
            _play(1, 2),  # period 1: {1, 2}, not a replay; seen = {1, 2}
            _play(100, 1),  # period 2 starts, one distinct episode so far
            _play(100 + gap, 2),  # exactly the gap from the PREVIOUS play: shares period 2
        ]
        # Merged, period 2 is {1, 2}: full overlap with `seen`, and a replay.
        assert rewatch.replay_period_count(plays) == 1

    def test_one_second_past_the_gap_splits(self) -> None:
        gap = rewatch.SHOW_PERIOD_GAP_DAYS
        plays = [
            _play(0, 1),
            _play(1, 2),  # period 1: {1, 2}, not a replay; seen = {1, 2}
            _play(100, 1),  # period 2: {1} alone
            _play(100 + gap, 2, seconds=1),  # one second past the gap: period 3, not 2
        ]
        # Split, periods 2 and 3 each hold one distinct episode: neither clears the floor,
        # whatever their overlap with `seen`.
        assert rewatch.replay_period_count(plays) == 0

    def test_unsorted_input_is_sorted_first(self) -> None:
        plays = [
            _play(40, 1),
            _play(41, 2),
            _play(0, 1),
            _play(1, 2),
        ]
        assert rewatch.replay_period_count(plays) == 1

    def test_a_weekly_airing_run_stays_one_period_and_is_not_a_replay(self) -> None:
        """Ten weekly episodes, all new: SHOW_PERIOD_GAP_DAYS=30 bridges the 7-day airing
        gaps into one period, and nothing in it repeats an earlier period."""
        plays = [_play(7 * i, i) for i in range(10)]
        assert rewatch.replay_period_count(plays) == 0


class TestMovieRewatchStatsEndToEnd:
    async def test_a_track_row_is_excluded(self, engine: AsyncEngine) -> None:
        await _insert(
            engine,
            rating_key=100,
            watched_at=NOW_EPOCH,
            watched_status=1.0,
            percent_complete=100,
            media_type="track",
        )

        stats = await rewatch.movie_rewatch_stats(engine, {100})

        assert stats == {}

    async def test_an_episode_row_is_excluded(self, engine: AsyncEngine) -> None:
        await _insert(
            engine,
            rating_key=100,
            watched_at=NOW_EPOCH,
            watched_status=1.0,
            percent_complete=100,
            media_type="episode",
        )

        stats = await rewatch.movie_rewatch_stats(engine, {100})

        assert stats == {}

    async def test_an_unqualified_play_moves_nothing(self, engine: AsyncEngine) -> None:
        await _insert(
            engine,
            rating_key=200,
            watched_at=NOW_EPOCH,
            watched_status=None,
            percent_complete=10,
        )

        stats = await rewatch.movie_rewatch_stats(engine, {200})

        assert stats == {}

    async def test_a_qualified_play_is_counted(self, engine: AsyncEngine) -> None:
        await _insert(
            engine,
            rating_key=300,
            watched_at=NOW_EPOCH,
            watched_status=1.0,
            percent_complete=100,
        )

        stats = await rewatch.movie_rewatch_stats(engine, {300})

        assert stats[300].viewings == 1
        assert stats[300].last_play == from_epoch(NOW_EPOCH)

    async def test_a_merged_group_folds_onto_the_canonical_key(self, engine: AsyncEngine) -> None:
        """Two listings of one file. A play under either listing counts toward the
        canonical key, and the two are clustered together over their union."""
        await _insert(
            engine,
            rating_key=400,
            watched_at=NOW_EPOCH - 60 * DAY,
            watched_status=1.0,
            percent_complete=100,
        )
        await _insert(
            engine,
            rating_key=401,  # the other listing of the same file
            watched_at=NOW_EPOCH,
            watched_status=1.0,
            percent_complete=100,
        )

        stats = await rewatch.movie_rewatch_stats(engine, {400}, groups={400: (400, 401)})

        assert stats[400].viewings == 2  # 60 days apart, well past the gap
        assert stats[400].last_play == from_epoch(NOW_EPOCH)  # the later listing wins

    async def test_a_key_outside_the_candidate_set_is_not_fetched(
        self, engine: AsyncEngine
    ) -> None:
        await _insert(
            engine,
            rating_key=500,
            watched_at=NOW_EPOCH,
            watched_status=1.0,
            percent_complete=100,
        )

        stats = await rewatch.movie_rewatch_stats(engine, {999})

        assert stats == {}

    async def test_a_key_with_no_rows_at_all_is_absent_not_zero(self, engine: AsyncEngine) -> None:
        stats = await rewatch.movie_rewatch_stats(engine, {999})

        assert stats == {}  # caller reads a missing key as zero viewings

    async def test_an_empty_candidate_set_short_circuits(self, engine: AsyncEngine) -> None:
        stats = await rewatch.movie_rewatch_stats(engine, set())

        assert stats == {}


class TestTheOnlyUnqualifiedPlaysPairingThroughBuildFacts:
    """A movie with plays, none of them qualified, is missing from ``movie_rewatch_stats``
    -- the same "no rows" shape as a movie with no plays at all. ``snapshot.build_facts``
    must read that as ``Known(0)`` viewings paired with an ``Absent`` last-play, never as
    ``Unknown``: the mirror was read, and there is genuinely nothing to measure the last
    qualified play from (rule 93). Exercised through the real fact builder, because the
    pairing is a property of how the two observations are read together."""

    async def test_zero_viewings_pairs_with_an_absent_last_play(self, engine: AsyncEngine) -> None:
        await _insert(
            engine,
            rating_key=700,
            watched_at=NOW_EPOCH,
            watched_status=None,
            percent_complete=20,  # abandoned: under half, no reported status
        )
        stats = await rewatch.movie_rewatch_stats(engine, {700})
        assert stats == {}  # confirms the premise: nothing qualified was recorded

        item = RawItem(
            media_key="radarr:1:700",
            title="a title",
            media_type="movie",
            size_bytes=1,
            imdb_id=None,
            tmdb_id=None,
            plex_rating_key=700,
            added_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        facts = build_facts(
            item,
            ScanContext(horizon=datetime(2019, 1, 1, tzinfo=UTC)),
            membership_index=lists.MembershipIndex({}, {}, {}, {}),
            imdb={},
            last_played={},
            watchers_window={},
            watchers_all_time={},
            whitelisted=set(),
            rewatch=stats,
        )

        assert facts.rewatch_viewings == Known(value=0, source="tautulli")
        assert isinstance(facts.rewatch_last_play_days, Absent)


class TestShowRewatchStatsEndToEnd:
    """``rewatch.show_rewatch_stats``: episode plays folded onto their show via
    ``grandparent_rating_key``, and ``viewings`` derived from `replay_period_count`, not a
    plain viewing count (docs/history/REWATCH_PLAN.md, TV section)."""

    async def test_plays_land_under_the_show_via_grandparent_key(self, engine: AsyncEngine) -> None:
        # Two replay periods of the same two episodes, mirroring
        # TestReplayPeriodCount.test_two_periods_of_the_same_episodes_is_one_replay.
        for rating_key, offset in ((10, 0), (11, DAY)):
            await _insert(
                engine,
                rating_key=rating_key,
                grandparent_rating_key=2000,
                watched_at=NOW_EPOCH + offset,
                watched_status=1.0,
                percent_complete=100,
                media_type="episode",
            )
        for rating_key, offset in ((10, 40 * DAY), (11, 41 * DAY)):
            await _insert(
                engine,
                rating_key=rating_key,
                grandparent_rating_key=2000,
                watched_at=NOW_EPOCH + offset,
                watched_status=1.0,
                percent_complete=100,
                media_type="episode",
            )

        stats = await rewatch.show_rewatch_stats(engine, {2000})

        assert stats[2000].viewings == 1
        assert stats[2000].last_play == from_epoch(NOW_EPOCH + 41 * DAY)

    async def test_a_movie_row_is_excluded(self, engine: AsyncEngine) -> None:
        await _insert(
            engine,
            rating_key=100,
            grandparent_rating_key=2000,
            watched_at=NOW_EPOCH,
            watched_status=1.0,
            percent_complete=100,
            media_type="movie",
        )

        stats = await rewatch.show_rewatch_stats(engine, {2000})

        assert stats == {}

    async def test_another_shows_row_is_excluded(self, engine: AsyncEngine) -> None:
        await _insert(
            engine,
            rating_key=200,
            grandparent_rating_key=3000,  # not in the candidate set below
            watched_at=NOW_EPOCH,
            watched_status=1.0,
            percent_complete=100,
            media_type="episode",
        )

        stats = await rewatch.show_rewatch_stats(engine, {2000})

        assert stats == {}

    async def test_an_unqualified_play_does_not_count(self, engine: AsyncEngine) -> None:
        await _insert(
            engine,
            rating_key=300,
            grandparent_rating_key=2100,
            watched_at=NOW_EPOCH,
            watched_status=None,
            percent_complete=10,  # abandoned: under half, no reported status
            media_type="episode",
        )

        stats = await rewatch.show_rewatch_stats(engine, {2100})

        assert stats == {}  # the one filter this feature's play counts are allowed to use

    async def test_last_play_is_the_max_qualified_play(self, engine: AsyncEngine) -> None:
        await _insert(
            engine,
            rating_key=400,
            grandparent_rating_key=2200,
            watched_at=NOW_EPOCH,
            watched_status=1.0,
            percent_complete=100,
            media_type="episode",
        )
        await _insert(
            engine,
            rating_key=401,
            grandparent_rating_key=2200,
            watched_at=NOW_EPOCH + 10 * DAY,  # later, but unqualified
            watched_status=None,
            percent_complete=10,
            media_type="episode",
        )

        stats = await rewatch.show_rewatch_stats(engine, {2200})

        assert stats[2200].last_play == from_epoch(NOW_EPOCH)

    async def test_a_show_with_no_qualified_plays_is_absent(self, engine: AsyncEngine) -> None:
        stats = await rewatch.show_rewatch_stats(engine, {2300})

        assert stats == {}  # caller reads a missing key as zero viewings

    async def test_a_key_outside_the_candidate_set_is_not_fetched(
        self, engine: AsyncEngine
    ) -> None:
        await _insert(
            engine,
            rating_key=500,
            grandparent_rating_key=2400,
            watched_at=NOW_EPOCH,
            watched_status=1.0,
            percent_complete=100,
            media_type="episode",
        )

        stats = await rewatch.show_rewatch_stats(engine, {9999})

        assert stats == {}

    async def test_an_empty_candidate_set_short_circuits(self, engine: AsyncEngine) -> None:
        stats = await rewatch.show_rewatch_stats(engine, set())

        assert stats == {}


# ---------------------------------------------------------------------------
# Stage 2: the rewatch-probability fit (#554)
# ---------------------------------------------------------------------------


class TestFitBlocksBuckets:
    """Bucket edges, half-open (lo, hi] (docs/history/REWATCH_PLAN.md, Stage 2 Fit)."""

    def test_365_lands_in_the_first_bucket(self) -> None:
        curve = rewatch.fit_blocks([(365.0, True)] * 40)
        assert len(curve) == 1
        assert (curve[0].lo_days, curve[0].hi_days) == (0.0, 365.0)

    def test_366_lands_in_the_second_bucket(self) -> None:
        curve = rewatch.fit_blocks([(366.0, True)] * 40)
        assert len(curve) == 1
        assert (curve[0].lo_days, curve[0].hi_days) == (365.0, 548.0)

    def test_an_empty_bucket_is_dropped(self) -> None:
        # Nothing lands in the first bucket at all, so the curve starts at the second.
        curve = rewatch.fit_blocks([(400.0, True)] * 40)
        assert len(curve) == 1
        assert curve[0].lo_days == 365.0

    def test_no_pairs_is_an_empty_curve(self) -> None:
        assert rewatch.fit_blocks([]) == ()


class TestFitBlocksMonotoneMerge:
    def test_merge_fires_on_an_inversion_and_pools_n_k_and_range(self) -> None:
        # Bucket 1 (0,365]: rate .2. Bucket 2 (365,548]: rate .8 -- higher, an inversion,
        # since the rate must not RISE with more dormancy.
        pairs = (
            [(100.0, False)] * 8
            + [(100.0, True)] * 2  # bucket 1: n=10, k=2
            + [(400.0, False)] * 2
            + [(400.0, True)] * 8  # bucket 2: n=10, k=8
        )
        curve = rewatch.fit_blocks(pairs)
        assert len(curve) == 1
        merged = curve[0]
        assert (merged.lo_days, merged.hi_days) == (0.0, 548.0)
        assert (merged.n, merged.k) == (20, 10)

    def test_a_monotone_curve_is_a_no_op(self) -> None:
        pairs = (
            [(100.0, True)] * 8
            + [(100.0, False)] * 2  # bucket 1: rate .8
            + [(400.0, True)] * 2
            + [(400.0, False)] * 8  # bucket 2: rate .2, already decreasing
        )
        curve = rewatch.fit_blocks(pairs)
        assert len(curve) == 2
        assert (curve[0].n, curve[0].k) == (10, 8)
        assert (curve[1].n, curve[1].k) == (10, 2)


class TestBlockFor:
    def test_dormancy_past_the_fitted_range_is_none(self) -> None:
        curve = (rewatch.RewatchBlock(lo_days=0.0, hi_days=365.0, n=40, k=10),)
        assert rewatch.block_for(curve, 400.0) is None

    def test_dormancy_inside_a_dropped_gap_is_none(self) -> None:
        # The 365-730 bucket had nothing in it and was dropped: a gap, not a merge target.
        curve = (
            rewatch.RewatchBlock(lo_days=0.0, hi_days=365.0, n=40, k=10),
            rewatch.RewatchBlock(lo_days=730.0, hi_days=1095.0, n=40, k=5),
        )
        assert rewatch.block_for(curve, 500.0) is None

    def test_dormancy_inside_a_block_is_returned(self) -> None:
        block = rewatch.RewatchBlock(lo_days=365.0, hi_days=548.0, n=40, k=10)
        curve = (block,)
        assert rewatch.block_for(curve, 400.0) is block


class TestCohortBlock:
    """`cohort_block` combines the lookup with the reach withhold (rule 104, and
    docs/history/REWATCH_PLAN.md, Stage 2, "Floor")."""

    BLOCK = rewatch.RewatchBlock(lo_days=365.0, hi_days=548.0, n=40, k=10)

    def test_none_when_no_block_covers_the_dormancy(self) -> None:
        curve = (rewatch.RewatchBlock(lo_days=0.0, hi_days=365.0, n=40, k=10),)
        assert rewatch.cohort_block(curve, 400.0, reach_days=1000.0) is None

    def test_reach_exactly_at_the_blocks_near_edge_is_withheld(self) -> None:
        assert rewatch.cohort_block((self.BLOCK,), 400.0, reach_days=365.0) is None

    def test_reach_one_day_short_of_the_near_edge_is_withheld(self) -> None:
        assert rewatch.cohort_block((self.BLOCK,), 400.0, reach_days=364.0) is None

    def test_reach_one_day_past_the_near_edge_is_not_withheld(self) -> None:
        assert rewatch.cohort_block((self.BLOCK,), 400.0, reach_days=366.0) is self.BLOCK


class TestTrainingPair:
    """The population rule, pure (docs/history/REWATCH_PLAN.md, Stage 2 Fit)."""

    CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)

    def test_unknown_added_date_and_no_play_is_withheld(self) -> None:
        assert rewatch.training_pair(None, added_at=None, cutoff=self.CUTOFF) is None

    def test_unknown_added_date_and_an_old_play_is_fitted(self) -> None:
        last_play = self.CUTOFF - timedelta(days=400)
        outcome = rewatch.RewatchOutcome(
            last_play_at_or_before_cutoff=last_play, watched_again=True
        )
        pair = rewatch.training_pair(outcome, added_at=None, cutoff=self.CUTOFF)
        assert pair == (400.0, True)

    def test_a_too_recent_added_date_with_no_play_is_withheld(self) -> None:
        """Added inside the lookback year: fitting it against the added date would be a
        negative dormancy, so it is withheld instead."""
        added_at = self.CUTOFF + timedelta(days=10)
        assert rewatch.training_pair(None, added_at=added_at, cutoff=self.CUTOFF) is None

    def test_an_added_date_at_or_before_cutoff_with_no_play_is_fitted(self) -> None:
        added_at = self.CUTOFF - timedelta(days=1000)
        pair = rewatch.training_pair(None, added_at=added_at, cutoff=self.CUTOFF)
        assert pair == (1000.0, False)

    def test_added_date_exactly_at_cutoff_is_fitted(self) -> None:
        assert rewatch.training_pair(None, added_at=self.CUTOFF, cutoff=self.CUTOFF) == (0.0, False)

    def test_a_play_wins_over_an_added_date(self) -> None:
        last_play = self.CUTOFF - timedelta(days=50)
        added_at = self.CUTOFF - timedelta(days=2000)
        outcome = rewatch.RewatchOutcome(
            last_play_at_or_before_cutoff=last_play, watched_again=False
        )
        pair = rewatch.training_pair(outcome, added_at=added_at, cutoff=self.CUTOFF)
        assert pair == (50.0, False)


class TestMovieRewatchOutcomesEndToEnd:
    """`rewatch.movie_rewatch_outcomes`: any-completion plays, chunked, folded over merges."""

    async def test_a_key_outside_the_candidate_set_is_not_read(self, engine: AsyncEngine) -> None:
        """The trap the fit must never fall into: a rating key the caller did not hand in
        must never surface in the result, whatever rows exist for it."""
        await _insert(
            engine, rating_key=500, watched_at=NOW_EPOCH, watched_status=1.0, percent_complete=100
        )
        outcomes = await rewatch.movie_rewatch_outcomes(engine, {999}, cutoff=_at(NOW_EPOCH + DAY))
        assert outcomes == {}

    async def test_an_empty_candidate_set_short_circuits(self, engine: AsyncEngine) -> None:
        outcomes = await rewatch.movie_rewatch_outcomes(engine, set(), cutoff=_at(NOW_EPOCH))
        assert outcomes == {}

    async def test_an_abandoned_play_still_counts(self, engine: AsyncEngine) -> None:
        """Unlike `movie_rewatch_stats`, no `qualifies()` filter: an under-50%, no-status
        play still sets the last play before cutoff (docs/history/REWATCH_PLAN.md, Stage 2 Fit)."""
        await _insert(
            engine, rating_key=600, watched_at=NOW_EPOCH, watched_status=None, percent_complete=10
        )
        outcomes = await rewatch.movie_rewatch_outcomes(engine, {600}, cutoff=_at(NOW_EPOCH + DAY))
        assert outcomes[600].last_play_at_or_before_cutoff == from_epoch(NOW_EPOCH)
        assert outcomes[600].watched_again is False

    async def test_a_play_after_cutoff_within_the_year_is_watched_again(
        self, engine: AsyncEngine
    ) -> None:
        await _insert(
            engine,
            rating_key=700,
            watched_at=NOW_EPOCH + 100 * DAY,
            watched_status=1.0,
            percent_complete=100,
        )
        outcomes = await rewatch.movie_rewatch_outcomes(engine, {700}, cutoff=_at(NOW_EPOCH))
        assert outcomes[700].watched_again is True
        assert outcomes[700].last_play_at_or_before_cutoff is None

    async def test_a_play_past_the_outcome_window_does_not_count(self, engine: AsyncEngine) -> None:
        await _insert(
            engine,
            rating_key=800,
            watched_at=NOW_EPOCH + 400 * DAY,  # past the 365-day outcome window
            watched_status=1.0,
            percent_complete=100,
        )
        outcomes = await rewatch.movie_rewatch_outcomes(engine, {800}, cutoff=_at(NOW_EPOCH))
        assert outcomes == {}

    async def test_a_merged_group_folds_onto_the_canonical_key(self, engine: AsyncEngine) -> None:
        await _insert(
            engine,
            rating_key=900,
            watched_at=NOW_EPOCH - 10 * DAY,
            watched_status=1.0,
            percent_complete=100,
        )
        await _insert(
            engine,
            rating_key=901,  # the other listing of the same file
            watched_at=NOW_EPOCH,
            watched_status=1.0,
            percent_complete=100,
        )
        outcomes = await rewatch.movie_rewatch_outcomes(
            engine, {900}, cutoff=_at(NOW_EPOCH + DAY), groups={900: (900, 901)}
        )
        assert outcomes[900].last_play_at_or_before_cutoff == from_epoch(NOW_EPOCH)


ANCIENT = datetime(2000, 1, 1, tzinfo=UTC)


def _scanned_facts(
    *,
    rating_key: int | None = 900,
    watch_blind_reason: str | None = None,
    rewatch_curve: rewatch.RewatchCurve | None,
    last_played: dict[int, datetime] | None = None,
    horizon: datetime = datetime(1990, 1, 1, tzinfo=UTC),
) -> Facts:
    """One movie item's ``Facts`` as a scan builds them, so a rewatch-cohort test states
    only the fit and the watch inputs under test."""
    item = RawItem(
        media_key="radarr:1:900",
        title="a title",
        media_type="movie",
        size_bytes=1,
        imdb_id=None,
        tmdb_id=None,
        plex_rating_key=rating_key,
        added_at=ANCIENT,
    )
    return build_facts(
        item,
        ScanContext(horizon=horizon),
        membership_index=lists.MembershipIndex({}, {}, {}, {}),
        imdb={},
        last_played=last_played or {},
        watchers_window={},
        watchers_all_time={},
        whitelisted=set(),
        watch_blind_reason=watch_blind_reason,
        rewatch_curve=rewatch_curve,
    )


class TestRewatchCohortFacts:
    """`snapshot.build_facts`'s ``rewatch_cohort_n``/``rewatch_cohort_k``: the states table
    (docs/history/REWATCH_PLAN.md, Stage 2, "Population in snapshot.build_facts")."""

    def test_no_plex_key_is_unknown(self) -> None:
        facts = _scanned_facts(rating_key=None, rewatch_curve=None)
        assert isinstance(facts.rewatch_cohort_n, Unknown)
        assert isinstance(facts.rewatch_cohort_k, Unknown)

    def test_watch_blind_is_unknown(self) -> None:
        facts = _scanned_facts(watch_blind_reason="went blind", rewatch_curve=None)
        assert isinstance(facts.rewatch_cohort_n, Unknown)
        assert isinstance(facts.rewatch_cohort_k, Unknown)

    def test_no_fit_ran_is_unknown(self) -> None:
        facts = _scanned_facts(rewatch_curve=None)
        assert isinstance(facts.rewatch_cohort_n, Unknown)

    def test_a_measured_block_freezes_known(self) -> None:
        # One block spanning every possible dormancy, so this reading is time-independent.
        curve = (rewatch.RewatchBlock(lo_days=0.0, hi_days=None, n=40, k=12),)
        facts = _scanned_facts(rewatch_curve=curve)
        assert facts.rewatch_cohort_n == Known(value=40, source="tautulli")
        assert facts.rewatch_cohort_k == Known(value=12, source="tautulli")

    def test_a_thin_block_still_freezes_known(self) -> None:
        curve = (rewatch.RewatchBlock(lo_days=0.0, hi_days=None, n=5, k=1),)
        facts = _scanned_facts(rewatch_curve=curve)
        assert facts.rewatch_cohort_n == Known(value=5, source="tautulli")
        assert facts.rewatch_cohort_k == Known(value=1, source="tautulli")

    def test_past_the_fitted_range_is_unknown(self) -> None:
        # A narrow block near zero; the item's ancient added_at puts its dormancy well
        # past it.
        curve = (rewatch.RewatchBlock(lo_days=0.0, hi_days=1.0, n=40, k=10),)
        facts = _scanned_facts(rewatch_curve=curve)
        assert isinstance(facts.rewatch_cohort_n, Unknown)
        assert isinstance(facts.rewatch_cohort_k, Unknown)

    def test_withheld_by_reach_is_unknown(self) -> None:
        # An unclamped dormancy from a real play far outside the mirror's reach, and a
        # block that covers it but starts past that reach -- the withhold's job.
        now = utcnow()
        curve = (rewatch.RewatchBlock(lo_days=200.0, hi_days=None, n=40, k=10),)
        facts = _scanned_facts(
            rewatch_curve=curve,
            last_played={900: now - timedelta(days=500)},
            horizon=now - timedelta(days=100),
        )
        assert isinstance(facts.rewatch_cohort_n, Unknown)


def _known_cohort(block: rewatch.RewatchBlock) -> Facts:
    """A movie item's ``Facts`` carrying ``block``'s counts, as a scan that found it freezes
    them."""
    return replace(
        _scanned_facts(rewatch_curve=None),
        rewatch_cohort_n=Known(value=block.n, source="t"),
        rewatch_cohort_k=Known(value=block.k, source="t"),
    )


class TestRewatchOddsContext:
    """`snapshot._rewatch_odds_context`: the explanation block's three states, and the
    season lane's ``None`` (docs/history/REWATCH_PLAN.md, Stage 2, "Storage and display")."""

    def test_season_lane_is_none_whatever_the_block(self) -> None:
        facts = replace(
            _scanned_facts(rewatch_curve=None),
            rewatch_cohort_n=Absent(source="t"),
            rewatch_cohort_k=Absent(source="t"),
        )
        block = rewatch.RewatchBlock(lo_days=0.0, hi_days=None, n=40, k=10)
        assert snapshot._rewatch_odds_context(facts, None) is None
        assert snapshot._rewatch_odds_context(facts, block) is None

    def test_no_usable_block_is_no_history(self) -> None:
        # No fit ran, so both cohort facts are already Unknown.
        facts = _scanned_facts(rewatch_curve=None)
        assert snapshot._rewatch_odds_context(facts, None) == {
            "n": 0,
            "k": 0,
            "lo_days": 0.0,
            "hi_days": None,
            "state": "no_history",
        }

    def test_measured_at_or_above_the_floor(self) -> None:
        block = rewatch.RewatchBlock(lo_days=365.0, hi_days=548.0, n=REWATCH_BLOCK_FLOOR_N, k=9)
        facts = _known_cohort(block)
        assert snapshot._rewatch_odds_context(facts, block) == {
            "n": 30,
            "k": 9,
            "lo_days": 365.0,
            "hi_days": 548.0,
            "state": "measured",
        }

    def test_thin_below_the_floor(self) -> None:
        block = rewatch.RewatchBlock(lo_days=365.0, hi_days=548.0, n=REWATCH_BLOCK_FLOOR_N - 1, k=5)
        facts = _known_cohort(block)
        ctx = snapshot._rewatch_odds_context(facts, block)
        assert ctx is not None
        assert ctx["state"] == "thin"


class TestTheZeroDormancyEdge:
    """A title played the day of the cutoff (dormancy exactly 0) is real data, not a gap.

    Found on live data during stage 2 verification: 18 of ~3,500 candidates sat at exactly
    zero days and a strict (0, 365] first bucket dropped them from the fit while
    ``block_for`` sent their panel to the no-history state. The first bucket is closed at
    zero in both places, and rule 104 holds the two edges together.
    """

    def test_a_zero_dormancy_pair_trains_the_first_bucket(self) -> None:
        curve = rewatch.fit_blocks([(0.0, True)] + [(10.0, False)] * 3)
        first = curve[0]
        assert (first.lo_days, first.n, first.k) == (0.0, 4, 1)

    def test_a_zero_dormancy_item_reads_the_first_block(self) -> None:
        curve = rewatch.fit_blocks([(5.0, True)] * 40)
        assert rewatch.block_for(curve, 0.0) is curve[0]
