# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rewatch derivation: what counts as a play, and how plays cluster into viewings.

Every number here is a ratio or a shape, never a real title or host (the repo's golden
rule): all fixtures use placeholder rating keys.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clock import from_epoch, utcnow
from reaper.config import Settings
from reaper.db.session import create_engine
from reaper.engine.gates import Facts
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
) -> None:
    await history_sync.ensure_schema(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO watch_event "
                "(rating_key, user_id, watched_at, watched_status, percent_complete, "
                " media_type) "
                "VALUES (:rating_key, :user_id, :watched_at, :watched_status, "
                " :percent_complete, :media_type)"
            ),
            {
                "rating_key": rating_key,
                "user_id": user_id,
                "watched_at": watched_at,
                "watched_status": watched_status,
                "percent_complete": percent_complete,
                "media_type": media_type,
            },
        )


class TestQualifiesTable:
    """rule: the play filter, exact table from ``docs/REWATCH_PLAN.md`` Stage 1."""

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


# ---------------------------------------------------------------------------
# Stage 2: the rewatch-probability fit (#554)
# ---------------------------------------------------------------------------


class TestFitBlocksBuckets:
    """Bucket edges, half-open (lo, hi] (docs/REWATCH_PLAN.md, Stage 2 Fit)."""

    def test_365_lands_in_the_first_bucket(self) -> None:
        curve = rewatch.fit_blocks([(365.0, True)] * 40)
        assert len(curve.blocks) == 1
        assert (curve.blocks[0].lo_days, curve.blocks[0].hi_days) == (0.0, 365.0)

    def test_366_lands_in_the_second_bucket(self) -> None:
        curve = rewatch.fit_blocks([(366.0, True)] * 40)
        assert len(curve.blocks) == 1
        assert (curve.blocks[0].lo_days, curve.blocks[0].hi_days) == (365.0, 548.0)

    def test_an_empty_bucket_is_dropped(self) -> None:
        # Nothing lands in the first bucket at all, so the curve starts at the second.
        curve = rewatch.fit_blocks([(400.0, True)] * 40)
        assert len(curve.blocks) == 1
        assert curve.blocks[0].lo_days == 365.0

    def test_no_pairs_is_an_empty_curve(self) -> None:
        assert rewatch.fit_blocks([]) == rewatch.RewatchCurve(blocks=())


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
        assert len(curve.blocks) == 1
        merged = curve.blocks[0]
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
        assert len(curve.blocks) == 2
        assert (curve.blocks[0].n, curve.blocks[0].k) == (10, 8)
        assert (curve.blocks[1].n, curve.blocks[1].k) == (10, 2)


class TestBlockFor:
    def test_dormancy_past_the_fitted_range_is_none(self) -> None:
        curve = rewatch.RewatchCurve(
            blocks=(rewatch.RewatchBlock(lo_days=0.0, hi_days=365.0, n=40, k=10),)
        )
        assert rewatch.block_for(curve, 400.0) is None

    def test_dormancy_inside_a_dropped_gap_is_none(self) -> None:
        # The 365-730 bucket had nothing in it and was dropped: a gap, not a merge target.
        curve = rewatch.RewatchCurve(
            blocks=(
                rewatch.RewatchBlock(lo_days=0.0, hi_days=365.0, n=40, k=10),
                rewatch.RewatchBlock(lo_days=730.0, hi_days=1095.0, n=40, k=5),
            )
        )
        assert rewatch.block_for(curve, 500.0) is None

    def test_dormancy_inside_a_block_is_returned(self) -> None:
        block = rewatch.RewatchBlock(lo_days=365.0, hi_days=548.0, n=40, k=10)
        curve = rewatch.RewatchCurve(blocks=(block,))
        assert rewatch.block_for(curve, 400.0) is block


class TestBlockWithheld:
    """The withhold helper at the reach boundary (docs/REWATCH_PLAN.md, Stage 2, "Floor")."""

    BLOCK = rewatch.RewatchBlock(lo_days=365.0, hi_days=548.0, n=40, k=10)

    def test_reach_exactly_at_the_blocks_near_edge_is_withheld(self) -> None:
        assert rewatch.block_withheld(self.BLOCK, 365.0) is True

    def test_reach_one_day_short_of_the_near_edge_is_withheld(self) -> None:
        assert rewatch.block_withheld(self.BLOCK, 364.0) is True

    def test_reach_one_day_past_the_near_edge_is_not_withheld(self) -> None:
        assert rewatch.block_withheld(self.BLOCK, 366.0) is False


class TestCohortBlock:
    """`cohort_block` combines `block_for` and `block_withheld` (rule 104)."""

    def test_none_when_no_block_covers_the_dormancy(self) -> None:
        curve = rewatch.RewatchCurve(
            blocks=(rewatch.RewatchBlock(lo_days=0.0, hi_days=365.0, n=40, k=10),)
        )
        assert rewatch.cohort_block(curve, 400.0, reach_days=1000.0) is None

    def test_none_when_the_covering_block_is_withheld(self) -> None:
        curve = rewatch.RewatchCurve(
            blocks=(rewatch.RewatchBlock(lo_days=365.0, hi_days=548.0, n=40, k=10),)
        )
        assert rewatch.cohort_block(curve, 400.0, reach_days=100.0) is None

    def test_the_block_when_usable(self) -> None:
        block = rewatch.RewatchBlock(lo_days=365.0, hi_days=548.0, n=40, k=10)
        curve = rewatch.RewatchCurve(blocks=(block,))
        assert rewatch.cohort_block(curve, 400.0, reach_days=1000.0) is block


class TestTrainingPair:
    """The population rule, pure (docs/REWATCH_PLAN.md, Stage 2 Fit)."""

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
        play still sets the last play before cutoff (docs/REWATCH_PLAN.md, Stage 2 Fit)."""
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


def _minimal_facts(**overrides: object) -> Facts:
    """The smallest ``Facts`` that constructs, so a rewatch-cohort test overrides only the
    two fields under test rather than restating every unrelated one."""
    base: dict[str, object] = {
        "title": "a title",
        "days_observed_unwatched": Known(value=900.0, source="t"),
        "distinct_watchers": Absent(source="t"),
        "distinct_watchers_all_time": Absent(source="t"),
        "size_bytes": Known(value=1, source="t"),
        "imdb_rating_tenths": Absent(source="t"),
        "imdb_votes": Absent(source="t"),
        "season_rank": Absent(source="t"),
        "is_streaming_now": Known(value=False, source="t"),
        "is_managed": Known(value=True, source="t"),
        "in_curated_list": Absent(source="t"),
        "is_whitelisted": Known(value=False, source="t"),
    }
    base.update(overrides)
    return Facts(**base)  # type: ignore[arg-type]


class TestRewatchCohortFacts:
    """`snapshot.build_facts`'s ``rewatch_cohort_n``/``rewatch_cohort_k``: the states table
    (docs/REWATCH_PLAN.md, Stage 2, "Population in snapshot.build_facts")."""

    ANCIENT = datetime(2000, 1, 1, tzinfo=UTC)

    def _facts(
        self,
        *,
        rating_key: int | None = 900,
        watch_blind_reason: str | None = None,
        rewatch_curve: rewatch.RewatchCurve | None,
        last_played: dict[int, datetime] | None = None,
        horizon: datetime = datetime(1990, 1, 1, tzinfo=UTC),
    ) -> Facts:
        item = RawItem(
            media_key="radarr:1:900",
            title="a title",
            media_type="movie",
            size_bytes=1,
            imdb_id=None,
            tmdb_id=None,
            plex_rating_key=rating_key,
            added_at=self.ANCIENT,
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

    def test_no_plex_key_is_unknown(self) -> None:
        facts = self._facts(rating_key=None, rewatch_curve=None)
        assert isinstance(facts.rewatch_cohort_n, Unknown)
        assert isinstance(facts.rewatch_cohort_k, Unknown)

    def test_watch_blind_is_unknown(self) -> None:
        facts = self._facts(watch_blind_reason="went blind", rewatch_curve=None)
        assert isinstance(facts.rewatch_cohort_n, Unknown)
        assert isinstance(facts.rewatch_cohort_k, Unknown)

    def test_no_fit_ran_is_unknown(self) -> None:
        facts = self._facts(rewatch_curve=None)
        assert isinstance(facts.rewatch_cohort_n, Unknown)

    def test_a_measured_block_freezes_known(self) -> None:
        # One block spanning every possible dormancy, so this reading is time-independent.
        curve = rewatch.RewatchCurve(
            blocks=(rewatch.RewatchBlock(lo_days=0.0, hi_days=None, n=40, k=12),)
        )
        facts = self._facts(rewatch_curve=curve)
        assert facts.rewatch_cohort_n == Known(value=40, source="tautulli")
        assert facts.rewatch_cohort_k == Known(value=12, source="tautulli")

    def test_a_thin_block_still_freezes_known(self) -> None:
        curve = rewatch.RewatchCurve(
            blocks=(rewatch.RewatchBlock(lo_days=0.0, hi_days=None, n=5, k=1),)
        )
        facts = self._facts(rewatch_curve=curve)
        assert facts.rewatch_cohort_n == Known(value=5, source="tautulli")
        assert facts.rewatch_cohort_k == Known(value=1, source="tautulli")

    def test_past_the_fitted_range_is_unknown(self) -> None:
        # A narrow block near zero; the item's ancient added_at puts its dormancy well
        # past it.
        curve = rewatch.RewatchCurve(
            blocks=(rewatch.RewatchBlock(lo_days=0.0, hi_days=1.0, n=40, k=10),)
        )
        facts = self._facts(rewatch_curve=curve)
        assert isinstance(facts.rewatch_cohort_n, Unknown)
        assert isinstance(facts.rewatch_cohort_k, Unknown)

    def test_withheld_by_reach_is_unknown(self) -> None:
        # An unclamped dormancy from a real play far outside the mirror's reach, and a
        # block that covers it but starts past that reach -- block_withheld's job.
        now = utcnow()
        curve = rewatch.RewatchCurve(
            blocks=(rewatch.RewatchBlock(lo_days=200.0, hi_days=None, n=40, k=10),)
        )
        facts = self._facts(
            rewatch_curve=curve,
            last_played={900: now - timedelta(days=500)},
            horizon=now - timedelta(days=100),
        )
        assert isinstance(facts.rewatch_cohort_n, Unknown)


class TestRewatchOddsContext:
    """`snapshot._rewatch_odds_context`: the explanation block's three states, and the
    season lane's ``None`` (docs/REWATCH_PLAN.md, Stage 2, "Storage and display")."""

    def test_season_lane_is_none_whatever_the_block(self) -> None:
        facts = _minimal_facts(
            rewatch_cohort_n=Absent(source="t"), rewatch_cohort_k=Absent(source="t")
        )
        block = rewatch.RewatchBlock(lo_days=0.0, hi_days=None, n=40, k=10)
        assert snapshot._rewatch_odds_context(facts, None) is None
        assert snapshot._rewatch_odds_context(facts, block) is None

    def test_no_usable_block_is_no_history(self) -> None:
        facts = _minimal_facts(
            rewatch_cohort_n=Unknown(reason="r", source="t"),
            rewatch_cohort_k=Unknown(reason="r", source="t"),
        )
        assert snapshot._rewatch_odds_context(facts, None) == {
            "n": 0,
            "k": 0,
            "lo_days": 0.0,
            "hi_days": None,
            "state": "no_history",
        }

    def test_measured_at_or_above_the_floor(self) -> None:
        block = rewatch.RewatchBlock(lo_days=365.0, hi_days=548.0, n=rewatch.BLOCK_FLOOR_N, k=9)
        facts = _minimal_facts(
            rewatch_cohort_n=Known(value=block.n, source="t"),
            rewatch_cohort_k=Known(value=block.k, source="t"),
        )
        assert snapshot._rewatch_odds_context(facts, block) == {
            "n": 30,
            "k": 9,
            "lo_days": 365.0,
            "hi_days": 548.0,
            "state": "measured",
        }

    def test_thin_below_the_floor(self) -> None:
        block = rewatch.RewatchBlock(lo_days=365.0, hi_days=548.0, n=rewatch.BLOCK_FLOOR_N - 1, k=5)
        facts = _minimal_facts(
            rewatch_cohort_n=Known(value=block.n, source="t"),
            rewatch_cohort_k=Known(value=block.k, source="t"),
        )
        ctx = snapshot._rewatch_odds_context(facts, block)
        assert ctx is not None
        assert ctx["state"] == "thin"
