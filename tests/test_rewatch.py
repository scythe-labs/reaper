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

from reaper.clock import from_epoch
from reaper.config import Settings
from reaper.db.session import create_engine
from reaper.engine.observation import Absent, Known
from reaper.services import history_sync, lists, rewatch
from reaper.services.snapshot import RawItem, ScanContext, build_facts

DAY = 86_400
NOW_EPOCH = 1_700_000_000  # an arbitrary, fixed instant; only offsets from it matter


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
