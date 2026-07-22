# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scan-lane review fixes.

Covers the findings addressed in the scan lane of the code review:

* the movie -> Plex join must fail closed on duplicate titles, exactly as the season
  path does, instead of last-write-wins into a title map;
* the backtest must reach the condemn verdict the SAME way production does -- on the
  rounded score and with the coverage floor -- so an honest replay does not diverge at
  the boundary the owner is tuning;
* ``expected_regret_rate`` must degrade to the fallback curve for a partially-calibrated
  prior rather than crashing the whole report;
* the grace clock must restart when a rescued item is re-condemned after a real gap.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from reaper.clients.plex import PlexError
from reaper.clock import from_epoch, utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import FirstFlagged
from reaper.db.session import create_engine, create_session_factory
from reaper.engine import backtest as bt
from reaper.engine import identity
from reaper.engine.backtest import BacktestResult, Item, run
from reaper.engine.calibration import Bucket, RewatchPrior
from reaper.engine.policy import DEFAULT_MOVIE_POLICY
from reaper.engine.signals import Score
from reaper.ratings import Rating, RatingSource
from reaper.services import history_sync
from reaper.services.snapshot import (
    _fold_merged_watch_stats,
    _insert_first_flags,
    _raw_items,
    _watch_stats,
    build_movie_index,
    protection_sync_degradations,
    record_first_flagged_bulk,
)

# Second precision: the timestamp columns round-trip through SQLite at whole-second
# precision, so a microsecond-bearing utcnow() would not compare equal after a fetch.
NOW = utcnow().replace(microsecond=0)


class TestMovieDecisionLog:
    """Every movie Radarr returns emits one greppable decision line, so "why isn't my movie
    in the queue" is answerable from the log -- the movie twin of season_scan.series_decision."""

    def _index(self) -> identity.PlexIndex:
        return identity.PlexIndex.build(
            [identity.PlexItem(rating_key=1, title="A Film", year=2020, added_at=NOW)]
        )

    def test_a_movie_with_a_file_logs_candidate(self) -> None:
        movie = {
            "id": 7,
            "title": "A Film",
            "year": 2020,
            "tmdbId": 500,
            "hasFile": True,
            "sizeOnDisk": 1234,
        }
        with capture_logs() as logs:
            _raw_items([movie], self._index(), instance_id=3)
        decisions = [e for e in logs if e["event"] == "scan.movie_decision"]
        assert len(decisions) == 1
        d = decisions[0]
        assert d["outcome"] == "candidate"
        assert d["title"] == "A Film"
        assert d["tmdb_id"] == 500
        assert d["has_file"] is True
        assert d["size_bytes"] == 1234
        assert d["instance_id"] == 3

    def test_a_movie_without_a_file_logs_no_file(self) -> None:
        movie = {"id": 8, "title": "Not Downloaded", "year": 2021, "hasFile": False}
        with capture_logs() as logs:
            items = _raw_items([movie], self._index(), instance_id=3)
        assert items == []  # dropped: nothing on disk to reap
        decisions = [e for e in logs if e["event"] == "scan.movie_decision"]
        assert len(decisions) == 1
        d = decisions[0]
        assert d["outcome"] == "no_file"
        assert d["has_file"] is False
        assert d["size_bytes"] is None


# ---------------------------------------------------------------------------
# The movie -> Plex join: duplicate titles must fail closed (the high finding).
# ---------------------------------------------------------------------------


class TestTheMovieJoinAtTheScanLane:
    """End-to-end proof that ``_raw_items`` threads the shared resolver correctly. Two
    films can share a title (remakes), and neither of these fixtures carries an external
    id, so an ambiguous title must leave the candidate unmatched (Unknown facts -> ABSTAIN,
    executor spares a keyless item) and a year that singles out one Plex row must bind it.
    The resolver's own tier logic is unit-tested in ``test_identity.py``."""

    def _index(self) -> identity.PlexIndex:
        # A remake pair, distinguished only by year; no ids -> the title+year backstop.
        return identity.PlexIndex.build(
            [
                identity.PlexItem(rating_key=11, title="The Mummy", year=1999, added_at=NOW),
                identity.PlexItem(rating_key=22, title="The Mummy", year=2017, added_at=NOW),
            ]
        )

    def test_raw_items_leaves_an_ambiguous_movie_unmatched(self) -> None:
        movie = {"id": 1, "title": "The Mummy", "hasFile": True, "sizeOnDisk": 1}
        items = _raw_items([movie], self._index(), instance_id=1)
        assert len(items) == 1
        assert items[0].plex_rating_key is None
        assert items[0].added_at is None

    def test_raw_items_binds_a_disambiguated_movie_by_title(self) -> None:
        movie = {"id": 1, "title": "The Mummy", "year": 2017, "hasFile": True, "sizeOnDisk": 1}
        items = _raw_items([movie], self._index(), instance_id=1)
        assert items[0].plex_rating_key == 22
        assert items[0].added_at == NOW
        assert items[0].matched_by is identity.MatchedBy.TITLE_YEAR

    def test_raw_items_binds_by_tmdb_across_a_title_difference(self) -> None:
        """The whole point of id matching: a Plex row whose title differs (a regional or
        renamed title) still binds when the tmdb id agrees."""
        index = identity.PlexIndex.build(
            [
                identity.PlexItem(
                    rating_key=42,
                    title="A Regional Title",
                    year=2020,
                    added_at=NOW,
                    ids=identity.ExternalIds.of(tmdb=1001),
                )
            ]
        )
        movie = {
            "id": 1,
            "title": "The Original Title",
            "year": 2020,
            "tmdbId": 1001,
            "hasFile": True,
            "sizeOnDisk": 1,
        }
        items = _raw_items([movie], index, instance_id=1)
        assert items[0].plex_rating_key == 42
        assert items[0].matched_by is identity.MatchedBy.TMDB

    def test_raw_items_abstains_when_a_shared_id_names_two_rows(self) -> None:
        """A duplicate tmdb across two Plex items is ambiguous -> keyless candidate."""
        index = identity.PlexIndex.build(
            [
                identity.PlexItem(
                    rating_key=1,
                    title="A",
                    year=2020,
                    added_at=NOW,
                    ids=identity.ExternalIds.of(tmdb=1001),
                ),
                identity.PlexItem(
                    rating_key=2,
                    title="B",
                    year=2020,
                    added_at=NOW,
                    ids=identity.ExternalIds.of(tmdb=1001),
                ),
            ]
        )
        movie = {
            "id": 9,
            "title": "A",
            "year": 2020,
            "tmdbId": 1001,
            "hasFile": True,
            "sizeOnDisk": 1,
        }
        items = _raw_items([movie], index, instance_id=1)
        assert items[0].plex_rating_key is None
        assert items[0].matched_by is None

    def test_raw_items_narrows_a_shared_id_by_the_movies_file_name(self) -> None:
        """The split-library case through the scan path: two Plex rows share the tmdb id
        (an HD and a 4K section), and the movie's own file name picks its copy."""
        index = identity.PlexIndex.build(
            [
                identity.PlexItem(
                    rating_key=1,
                    title="Example",
                    year=2020,
                    added_at=NOW,
                    ids=identity.ExternalIds.of(tmdb=1001),
                    file_basename="example (2020) 1080p.mkv",
                    files=(identity.PlexFile("example (2020) 1080p.mkv"),),
                ),
                identity.PlexItem(
                    rating_key=2,
                    title="Example",
                    year=2020,
                    added_at=NOW,
                    ids=identity.ExternalIds.of(tmdb=1001),
                    file_basename="example (2020) 2160p.mkv",
                    files=(identity.PlexFile("example (2020) 2160p.mkv"),),
                ),
            ]
        )
        movie = {
            "id": 9,
            "title": "Example",
            "year": 2020,
            "tmdbId": 1001,
            "hasFile": True,
            "sizeOnDisk": 1,
            "movieFile": {"relativePath": "Example (2020) 2160p.mkv"},
        }
        items = _raw_items([movie], index, instance_id=1)
        assert items[0].plex_rating_key == 2
        assert items[0].added_at == NOW
        assert items[0].matched_by is identity.MatchedBy.ID_AND_BASENAME
        assert items[0].match_status is identity.MatchStatus.MATCHED

    def test_raw_items_merges_byte_identical_twin_listings(self) -> None:
        """The curated re-list case through the scan path: two Plex rows share the tmdb
        id AND the file name, and Radarr's exact byte size proves they are one file
        listed twice. The item binds to the earliest listing and carries every listing's
        key, so its watch stats can be read from all of them."""
        earlier = NOW - timedelta(days=900)
        index = identity.PlexIndex.build(
            [
                identity.PlexItem(
                    rating_key=1,
                    title="Example",
                    year=2020,
                    added_at=earlier,
                    ids=identity.ExternalIds.of(tmdb=1001),
                    file_basename="example (2020).mkv",
                    files=(identity.PlexFile("example (2020).mkv", 7_000),),
                ),
                identity.PlexItem(
                    rating_key=2,
                    title="Example",
                    year=2020,
                    added_at=NOW,
                    ids=identity.ExternalIds.of(tmdb=1001),
                    file_basename="example (2020).mkv",
                    files=(identity.PlexFile("example (2020).mkv", 7_000),),
                ),
            ]
        )
        movie = {
            "id": 9,
            "title": "Example",
            "year": 2020,
            "tmdbId": 1001,
            "hasFile": True,
            "sizeOnDisk": 7_050,
            "movieFile": {"relativePath": "Example (2020).mkv", "size": 7_000},
        }
        items = _raw_items([movie], index, instance_id=1)
        assert items[0].plex_rating_key == 1
        assert items[0].added_at == earlier
        assert items[0].matched_by is identity.MatchedBy.MERGED_LISTINGS
        assert items[0].match_status is identity.MatchStatus.MATCHED
        assert items[0].merged_rating_keys == (1, 2)


# ---------------------------------------------------------------------------
# build_movie_index: Tautulli spine + plexapi enrichment, fail-closed on sweep failure.
# ---------------------------------------------------------------------------


class _FakeTautulli:
    """The minimum of the Tautulli client that ``build_movie_index`` touches."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    async def libraries(self) -> list[dict[str, object]]:
        return [{"section_type": "movie", "section_id": 1}]

    async def library_media_info(
        self, section_id: int, *, start: int = 0, length: int = 1000, **_: object
    ) -> dict[str, object]:
        return {"data": self._rows if start == 0 else []}


class _FakePlexSweep:
    def __init__(self, items: dict[int, identity.PlexItem]) -> None:
        self._items = items

    async def library_guid_index(
        self, *, section_type: str, allowed_sections: set[int] | None = None
    ) -> dict[int, identity.PlexItem]:
        return self._items


class _FakePlexBrokenSweep:
    async def library_guid_index(
        self, *, section_type: str, allowed_sections: set[int] | None = None
    ) -> dict[int, object]:
        raise PlexError("the sweep blew up")


_SPINE_ROWS = [{"rating_key": 100, "title": "Example", "year": 2020, "added_at": "1700000000"}]


class TestBuildMovieIndex:
    async def test_ids_and_basename_enrich_the_tautulli_spine(self) -> None:
        """The plexapi sweep joins onto the spine by rating key, and added_at STILL comes
        from Tautulli (the dormancy floor must not shift)."""
        swept = {
            100: identity.PlexItem(
                rating_key=100,
                title="Example",
                year=2020,
                added_at=from_epoch("1600000000"),  # differs from the spine on purpose
                ids=identity.ExternalIds.of(tmdb=1001),
                file_basename="example (2020).mkv",
                video_resolution="1080",
                content_rating="PG-13",
                runtime_minutes=95,
                ratings=(
                    Rating(
                        source=RatingSource.ROTTEN_TOMATOES_CRITIC,
                        value=7.7,
                        votes=None,
                        provider="plex",
                    ),
                ),
            )
        }
        index = await build_movie_index(
            _FakeTautulli(_SPINE_ROWS),  # type: ignore[arg-type]
            _FakePlexSweep(swept),  # type: ignore[arg-type]
            degrade=lambda _r: None,
        )
        item = index.by_rating_key[100]
        assert item.added_at == from_epoch("1700000000")  # from the spine, not the sweep
        assert item.ids.tmdb == 1001
        assert item.file_basename == "example (2020).mkv"
        assert index.by_tmdb[1001] == [100]
        # The display metadata must survive the spine rebuild -- this loop copies
        # fields one by one, and a field missed here silently never reaches a card.
        assert item.video_resolution == "1080"
        assert item.content_rating == "PG-13"
        assert item.runtime_minutes == 95
        assert [r.source for r in item.ratings] == [RatingSource.ROTTEN_TOMATOES_CRITIC]

    async def test_an_item_the_tautulli_cache_has_not_listed_still_enters_the_index(self) -> None:
        """Tautulli's media-info listing is a cache and lags fresh additions. An item the
        plexapi sweep sees but the spine does not must still enter the index (with Plex's
        own added-at), or the resolver falsely reports a matched item as unmatched."""
        fresh = identity.PlexItem(
            rating_key=200,
            title="Example Fresh",
            year=2026,
            added_at=from_epoch("1700000500"),
            ids=identity.ExternalIds.of(tmdb=2002),
            file_basename="example fresh (2026).mkv",
        )
        swept = {
            100: identity.PlexItem(
                rating_key=100,
                title="Example",
                year=2020,
                added_at=None,
                ids=identity.ExternalIds.of(tmdb=1001),
                file_basename="example (2020).mkv",
            ),
            200: fresh,
        }
        index = await build_movie_index(
            _FakeTautulli(_SPINE_ROWS),  # type: ignore[arg-type]
            _FakePlexSweep(swept),  # type: ignore[arg-type]
            degrade=lambda _r: None,
        )
        assert index.by_rating_key[200] == fresh  # in the index, Plex's added_at intact
        res = identity.resolve_movie(
            ids=identity.ExternalIds.of(tmdb=2002),
            title="Example Fresh",
            year=2026,
            file_basename=None,
            index=index,
        )
        assert res.rating_key == 200
        assert res.status is identity.MatchStatus.MATCHED

    async def test_a_sweep_failure_degrades_but_still_matches_by_title(self) -> None:
        """rule #2: a failed GUID sweep degrades the snapshot (un-executable) rather than
        silently continuing -- but items still fall through to the title+year backstop."""
        reasons: list[str] = []
        index = await build_movie_index(
            _FakeTautulli(_SPINE_ROWS),  # type: ignore[arg-type]
            _FakePlexBrokenSweep(),  # type: ignore[arg-type]
            degrade=reasons.append,
        )
        assert reasons and "GUID sweep failed" in reasons[0]
        assert index.by_rating_key[100].ids.empty  # nothing enriched
        res = identity.resolve_movie(
            ids=identity.ExternalIds(), title="Example", year=2020, file_basename=None, index=index
        )
        assert res.rating_key == 100  # the backstop still works

    async def test_no_plex_client_means_no_enrichment_and_no_degrade(self) -> None:
        """A movie-only deployment with no Plex configured: no ids, no degrade for the
        sweep (it was already un-executable, since a real reap refuses without Plex)."""
        reasons: list[str] = []
        index = await build_movie_index(
            _FakeTautulli(_SPINE_ROWS),  # type: ignore[arg-type]
            None,
            degrade=reasons.append,
        )
        assert reasons == []
        assert index.by_rating_key[100].ids.empty


# ---------------------------------------------------------------------------
# The backtest verdict must match production: rounded score + coverage floor.
# ---------------------------------------------------------------------------


@pytest.fixture
async def cache_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))  # type: ignore[call-arg]
    await history_sync.ensure_schema(eng)  # the empty watch_event table _plays reads
    yield eng
    await eng.dispose()


# ---------------------------------------------------------------------------
# Merged listings: one file listed twice in Plex, plays split across the rating keys.
# ---------------------------------------------------------------------------


async def _play_event(
    engine: AsyncEngine, row_id: int, rating_key: int, user_id: int, when: datetime
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO watch_event (row_id, rating_key, user_id, watched_at, "
                "watched_status, percent_complete, media_type) "
                "VALUES (:id, :rk, :user, :at, 1.0, 100, 'movie')"
            ),
            {"id": row_id, "rk": rating_key, "user": user_id, "at": int(when.timestamp())},
        )


class TestMergedWatchStatsFold:
    """The fold reads a merged group's plays as a UNION onto the canonical key: exact
    distinct watchers (one person through two listings counts once), the latest play
    through any listing, and nothing else's stats touched."""

    async def test_the_group_union_lands_on_the_canonical_key(
        self, cache_engine: AsyncEngine
    ) -> None:
        old = NOW - timedelta(days=400)  # outside the 365-day window
        recent = NOW - timedelta(days=10)
        await _play_event(cache_engine, 1, 100, 1, old)  # user 1, canonical listing
        await _play_event(cache_engine, 2, 200, 1, recent)  # user 1 again, other listing
        await _play_event(cache_engine, 3, 200, 2, recent)  # user 2, other listing only
        await _play_event(cache_engine, 4, 200, 9, old)  # user 9, old, other listing
        await _play_event(cache_engine, 5, 300, 3, recent)  # an unrelated item

        last_played, window, ever = await _watch_stats(
            cache_engine, rating_keys={100, 300}, window_days=365
        )
        assert ever.get(100) == 1  # before the fold: the canonical key alone

        await _fold_merged_watch_stats(
            cache_engine,
            groups={100: (100, 200)},
            window_days=365,
            last_played=last_played,
            watchers_window=window,
            watchers_all_time=ever,
        )
        assert last_played[100] == from_epoch(int(recent.timestamp()))
        assert window[100] == 2  # users 1 and 2; user 9's play is outside the window
        assert ever[100] == 3  # user 1 counted once despite playing both listings
        # The unrelated item's stats are untouched.
        assert ever.get(300) == 1

    async def test_plays_only_under_the_other_listing_still_count(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Every play sits under the file's OTHER listing; without the fold the canonical
        key would read 'never watched', the exact under-count that condemns."""
        recent = NOW - timedelta(days=5)
        await _play_event(cache_engine, 1, 200, 7, recent)

        last_played, window, ever = await _watch_stats(
            cache_engine, rating_keys={100}, window_days=365
        )
        assert 100 not in last_played

        await _fold_merged_watch_stats(
            cache_engine,
            groups={100: (100, 200)},
            window_days=365,
            last_played=last_played,
            watchers_window=window,
            watchers_all_time=ever,
        )
        assert last_played[100] == from_epoch(int(recent.timestamp()))
        assert window[100] == 1
        assert ever[100] == 1

    async def test_a_group_with_no_plays_changes_nothing(self, cache_engine: AsyncEngine) -> None:
        last_played, window, ever = await _watch_stats(
            cache_engine, rating_keys={100}, window_days=365
        )
        await _fold_merged_watch_stats(
            cache_engine,
            groups={100: (100, 200)},
            window_days=365,
            last_played=last_played,
            watchers_window=window,
            watchers_all_time=ever,
        )
        assert 100 not in last_played
        assert 100 not in window
        assert 100 not in ever


def _bt_item() -> Item:
    return Item(
        rating_key=1,
        title="A Film",
        size_bytes=8_000_000_000,
        added_at=NOW - timedelta(days=1000),
        imdb_rating_tenths=70,
        imdb_votes=1000,
    )


class TestTheBacktestVerdictMatchesProduction:
    async def test_a_score_of_69_6_is_condemned_because_it_rounds_to_70(
        self, cache_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Production condemns on ``round(69.6) == 70 >= 70``. A backtest that compared
        the raw float would skip it and under-count regret at the exact boundary the
        owner is tuning. It must round first, like production."""
        monkeypatch.setattr(
            bt, "score", lambda *a, **k: Score(value=69.6, coverage=1.0, results=[])
        )
        policy = DEFAULT_MOVIE_POLICY.model_copy(
            update={"condemn_at": 70, "coverage_floor_bp": 5000}
        )

        result = await run(
            cache_engine,
            [_bt_item()],
            policy,
            gates=[],
            cutoff=NOW - timedelta(days=365),
            horizon=NOW - timedelta(days=3000),
        )
        assert len(result.condemned) == 1

    async def test_a_low_coverage_item_is_abstained_by_the_floor(
        self, cache_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Production abstains below ``coverage_floor_bp``; the backtest never checked
        coverage and would over-count deletions. Now it honors the floor."""
        monkeypatch.setattr(
            bt, "score", lambda *a, **k: Score(value=95.0, coverage=0.40, results=[])
        )
        policy = DEFAULT_MOVIE_POLICY.model_copy(
            update={"condemn_at": 70, "coverage_floor_bp": 5000}
        )

        result = await run(
            cache_engine,
            [_bt_item()],
            policy,
            gates=[],
            cutoff=NOW - timedelta(days=365),
            horizon=NOW - timedelta(days=3000),
        )
        assert result.condemned == []  # 4000bp coverage is below the 5000bp floor


# ---------------------------------------------------------------------------
# A partially-calibrated prior degrades to the fallback rather than crashing.
# ---------------------------------------------------------------------------


class TestExpectedRegretRateDegradesGracefully:
    def test_a_thin_bucket_prior_falls_back_instead_of_raising(self) -> None:
        """A single condemned item landing in a thin bucket must not crash the whole
        report (lift/beats_random/summary all funnel through here). An uncalibrated prior
        degrades to the shared fallback curve, consistent with ``prior_is_derived``."""
        thin = RewatchPrior(
            buckets=(Bucket(low=1095, high=1825, samples=12, rewatched=3),),  # 12 < MIN_SAMPLES
            population=12,
            window_days=365,
            computed_at=NOW,
        )
        assert thin.calibrated is False  # the danger the fix guards

        result = BacktestResult(cutoff=NOW, condemn_at=70, prior=thin)
        result.condemned_dormancy.append(1200.0)  # would raise NotCalibratedError via rate_for

        # Must NOT raise, and must use the fallback curve (rewatch_prior(1200) == 0.19).
        assert result.prior_is_derived is False
        assert result.expected_regret_rate == pytest.approx(0.19)
        _ = result.lift  # exercises the whole funnel without crashing

    def test_a_calibrated_prior_is_still_used(self) -> None:
        """When every bucket is thick, the derived prior is honored -- the fix only
        changes behavior for the uncalibrated case."""
        prior = RewatchPrior(
            buckets=(Bucket(low=1095, high=1825, samples=100, rewatched=40),),
            population=100,
            window_days=365,
            computed_at=NOW,
        )
        assert prior.calibrated is True

        result = BacktestResult(cutoff=NOW, condemn_at=70, prior=prior)
        result.condemned_dormancy.append(1200.0)
        assert result.prior_is_derived is True
        assert result.expected_regret_rate == pytest.approx(0.40)


# ---------------------------------------------------------------------------
# The grace clock restarts when a rescued item is re-condemned after a gap.
# ---------------------------------------------------------------------------


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


class TestTheGraceClockRestartsOnReCondemnation:
    async def test_a_return_after_a_long_gap_restarts_the_clock(
        self, session: AsyncSession
    ) -> None:
        """Condemned long ago, rescued, then re-condemned a full dormancy period later:
        the item must serve a FRESH grace window, or it drops straight into ``ready`` with
        no countdown and no Leaving Soon warning, and is reaped with zero grace."""
        long_ago = NOW - timedelta(days=400)
        session.add(
            FirstFlagged(
                media_key="radarr:1:1",
                first_flagged_at=long_ago,
                # Last seen condemned 400 days ago -- it left the condemned set for far
                # longer than a grace window and has now returned.
                last_seen_condemned_at=long_ago,
            )
        )
        await session.flush()

        await record_first_flagged_bulk(session, ["radarr:1:1"], NOW, grace_days=14)

        row = await session.get(FirstFlagged, "radarr:1:1")
        assert row is not None
        assert row.first_flagged_at == NOW  # the clock restarted
        assert row.last_seen_condemned_at == NOW

    async def test_an_uninterrupted_condemn_does_not_reset_the_clock(
        self, session: AsyncSession
    ) -> None:
        """The other direction: an item that stayed condemned (or missed only a snapshot
        or two to a transient outage) keeps its original clock, so it can age out."""
        started = NOW - timedelta(days=5)
        session.add(
            FirstFlagged(
                media_key="radarr:1:2",
                first_flagged_at=started,
                last_seen_condemned_at=NOW - timedelta(days=1),  # gap well under the window
            )
        )
        await session.flush()

        await record_first_flagged_bulk(session, ["radarr:1:2"], NOW, grace_days=14)

        row = await session.get(FirstFlagged, "radarr:1:2")
        assert row is not None
        assert row.first_flagged_at == started  # NOT moved
        assert row.last_seen_condemned_at == NOW  # only the last-seen bumped


class TestTheGraceRecorderToleratesACompetingWriter:
    async def test_an_already_present_key_does_not_abort_the_insert(
        self, session: AsyncSession
    ) -> None:
        """Two writers racing on one key: the loser's insert must do nothing (the winner's
        row already says "condemned around now"), never raise a primary-key collision out
        of a scan. Simulated by inserting a row the recorder's read predates."""
        earlier = NOW - timedelta(hours=1)
        session.add(
            FirstFlagged(
                media_key="radarr:1:9", first_flagged_at=earlier, last_seen_condemned_at=earlier
            )
        )
        await session.flush()

        await _insert_first_flags(
            session,
            [
                FirstFlagged(
                    media_key="radarr:1:9", first_flagged_at=NOW, last_seen_condemned_at=NOW
                )
            ],
        )

        row = await session.get(FirstFlagged, "radarr:1:9")
        assert row is not None
        assert row.first_flagged_at == earlier  # the winner's clock survives


# ---------------------------------------------------------------------------
# A failed whitelist sync with an empty keep-list degrades the snapshot.
# ---------------------------------------------------------------------------


class TestProtectionSyncDegradations:
    async def test_a_failed_whitelist_with_no_members_degrades(
        self, cache_engine: AsyncEngine
    ) -> None:
        """A whitelist that failed to sync and has no membership fails OPEN -- the worst
        direction. It must degrade the snapshot so no reap runs against an empty keep-list."""
        await _seed_list(cache_engine, slug="reaper-keep", kind="whitelist", members=0)
        reasons = await protection_sync_degradations(cache_engine, {"reaper-keep": "error: boom"})
        assert any("reaper-keep" in r for r in reasons)

    async def test_a_failed_whitelist_with_a_fresh_copy_does_not_degrade(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The atomic swap preserved prior membership and the last successful sync is
        recent, so the keep-list still protects -- a transient failure need not stop the
        scan."""
        await _seed_list(
            cache_engine,
            slug="reaper-keep",
            kind="whitelist",
            members=3,
            last_synced_at=int(NOW.timestamp()) - 3600,  # an hour ago
        )
        reasons = await protection_sync_degradations(cache_engine, {"reaper-keep": "error: boom"})
        assert reasons == []

    async def test_a_failed_whitelist_stale_beyond_the_bound_degrades(
        self, cache_engine: AsyncEngine
    ) -> None:
        """P-6: stale-but-populated protects only within the bound. Past it, every title
        keep-tagged since the last successful sync has been unprotected for days, so the
        snapshot degrades until a sync succeeds."""
        await _seed_list(
            cache_engine,
            slug="reaper-keep",
            kind="whitelist",
            members=3,
            last_synced_at=int((NOW - timedelta(days=3)).timestamp()),
        )
        reasons = await protection_sync_degradations(cache_engine, {"reaper-keep": "error: boom"})
        assert any("reaper-keep" in r and "hours old" in r for r in reasons)

    async def test_a_failed_whitelist_with_members_but_no_sync_record_degrades(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Members with no last-successful-sync stamp: recency cannot be confirmed, so it
        is not assumed (fail closed)."""
        await _seed_list(
            cache_engine, slug="reaper-keep", kind="whitelist", members=3, last_synced_at=None
        )
        reasons = await protection_sync_degradations(cache_engine, {"reaper-keep": "error: boom"})
        assert any("reaper-keep" in r for r in reasons)

    async def test_a_failed_curated_hard_list_with_no_members_degrades(
        self, cache_engine: AsyncEngine
    ) -> None:
        """A curated list feeds a HARD gate: its members are PROTECTED outright, so an empty
        one fails OPEN exactly as an empty whitelist does. The gate protects nothing and a
        scan could reap the very titles the list exists to save. The first sync of the IMDb
        Top 250 failing must degrade the snapshot, not pass silently."""
        await _seed_list(cache_engine, slug="imdb-top-250", kind="curated", members=0)
        reasons = await protection_sync_degradations(cache_engine, {"imdb-top-250": "error: 503"})
        assert any("imdb-top-250" in r for r in reasons)

    async def test_a_failed_curated_hard_list_with_members_does_not_degrade_on_staleness(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Recency is a keep-list concern. A curated external list churns slowly and its
        stored copy keeps protecting, so a stale-but-populated one does not degrade: only
        the whitelist applies the staleness bound."""
        await _seed_list(
            cache_engine,
            slug="imdb-top-250",
            kind="curated",
            members=250,
            last_synced_at=int((NOW - timedelta(days=30)).timestamp()),
        )
        reasons = await protection_sync_degradations(cache_engine, {"imdb-top-250": "error: 503"})
        assert reasons == []

    async def test_a_failed_soft_list_does_not_degrade(self, cache_engine: AsyncEngine) -> None:
        """A SOFT-mode list only feeds a scoring nudge; losing it never unprotects a kept
        title, so even an empty one does not make the snapshot un-executable. This is the
        real axis: degrade on mode=hard, never on mode=soft."""
        await _seed_list(cache_engine, slug="soft-list", kind="curated", mode="soft", members=0)
        reasons = await protection_sync_degradations(cache_engine, {"soft-list": "error: 503"})
        assert reasons == []

    async def test_a_successful_sync_never_degrades(self, cache_engine: AsyncEngine) -> None:
        reasons = await protection_sync_degradations(cache_engine, {"reaper-keep": 12})
        assert reasons == []


async def _seed_list(
    engine: AsyncEngine,
    *,
    slug: str,
    kind: str,
    members: int,
    mode: str = "hard",
    last_synced_at: int | None = None,
) -> None:
    """Write a protection_list row (with mode + kind) and ``members`` membership rows."""
    from reaper.services import lists

    await lists.ensure_schema(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO protection_list "
                "(slug, display_name, mode, kind, weight, last_error, last_synced_at) "
                "VALUES (:slug, :slug, :mode, :kind, 0, 'error: boom', :synced) "
                "ON CONFLICT(slug) DO UPDATE SET mode = :mode, kind = :kind, "
                "last_synced_at = :synced"
            ),
            {"slug": slug, "kind": kind, "mode": mode, "synced": last_synced_at},
        )
        for i in range(members):
            await conn.execute(
                text(
                    "INSERT INTO protection_list_item "
                    "(slug, media_type, imdb_id, tmdb_id, tvdb_id, title, rank) "
                    "VALUES (:slug, 'movie', :imdb, NULL, NULL, :title, NULL)"
                ),
                {"slug": slug, "imdb": f"tt{i:07d}", "title": f"Film {i}"},
            )
