# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fairness leaderboard.

Two halves, tested apart: the pure per-person roll-up (no instance needed), and the
watch-evidence query against a real ``watch_event`` table (movies key on rating_key, a
show's episodes roll up to its grandparent key).

Names and titles here are placeholders -- the aggregation does not care what they say.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clients.seerr import MediaRequest, Requester
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.session import create_cache_engine
from reaper.engine.requester import RequesterPolicy, WatchEvidence
from reaper.services import fairness, history_sync
from reaper.services.fairness import MediaInfo, evaluate_fairness

GB = 1024**3
NOW = utcnow()
POLICY = RequesterPolicy(unwatched_days=90)


def _request(
    *,
    plex_id: int | None,
    name: str,
    rating_key: str | None,
    days_available: float | None = 400,
    request_id: int = 1,
) -> MediaRequest:
    return MediaRequest(
        request_id=request_id,
        media_type="movie",
        is_4k=False,
        status=5,
        requested_at=NOW - timedelta(days=500),
        requester=Requester(
            seerr_user_id=plex_id or 0,
            plex_id=plex_id,
            username=name.lower(),
            display_name=name,
            email=None,
        ),
        tmdb_id=1,
        tvdb_id=None,
        imdb_id="tt1",
        plex_rating_key=rating_key,
        arr_id=1,
        arr_instance_id=0,
        available_at=(NOW - timedelta(days=days_available)) if days_available is not None else None,
    )


def _tv_request(
    *, tvdb_id: int | None, rating_key: str | None, request_id: int = 1
) -> MediaRequest:
    return MediaRequest(
        request_id=request_id,
        media_type="tv",
        is_4k=False,
        status=5,
        requested_at=NOW - timedelta(days=500),
        requester=Requester(seerr_user_id=1, plex_id=1, username="a", display_name="A", email=None),
        tmdb_id=None,
        tvdb_id=tvdb_id,
        imdb_id=None,
        plex_rating_key=rating_key,
        arr_id=1,
        arr_instance_id=0,
        available_at=NOW - timedelta(days=400),
    )


class _FakeRadarr:
    """A stand-in exposing just the ``movies()`` call ``_arr_sizes`` uses."""

    def __init__(self, movies: list[dict[str, object]]) -> None:
        self._movies = movies

    async def movies(self) -> list[dict[str, object]]:
        return self._movies


class _FakeSonarr:
    """A stand-in exposing just the ``series()`` call ``_arr_sizes`` uses."""

    def __init__(self, series: list[dict[str, object]]) -> None:
        self._series = series

    async def series(self) -> list[dict[str, object]]:
        return self._series


class TestArrSizes:
    """File sizes come from the *arr -- the authority on what is on disk -- for movies and
    TV alike (Radarr sizes movies, Sonarr sizes shows), joined to each Seerr request by its
    external id and keyed by the same Plex rating key the watch join uses. This is what the
    scan already does; Tautulli's file_size (which lags for movies and is zero for shows) is
    only a fallback."""

    async def test_movie_size_comes_from_radarr(self) -> None:
        req = _request(plex_id=1, name="A", rating_key="700")  # tmdb_id=1
        radarr = _FakeRadarr([{"tmdbId": 1, "sizeOnDisk": 9 * GB}])
        sizes = await fairness._arr_sizes([radarr], [], [req])  # type: ignore[list-item]
        assert sizes == {"700": 9 * GB}

    async def test_show_size_comes_from_sonarr(self) -> None:
        req = _tv_request(tvdb_id=99, rating_key="700")
        sonarr = _FakeSonarr([{"tvdbId": 99, "statistics": {"sizeOnDisk": 12 * GB}}])
        sizes = await fairness._arr_sizes([], [sonarr], [req])  # type: ignore[list-item]
        assert sizes == {"700": 12 * GB}

    async def test_the_larger_instance_wins_for_a_duplicated_title(self) -> None:
        # The same film in an HD and a 4K library: the request was for one, and a
        # "disk you asked for" view should not undercount the 4K copy.
        req = _request(plex_id=1, name="A", rating_key="700")  # tmdb_id=1
        hd = _FakeRadarr([{"tmdbId": 1, "sizeOnDisk": 4 * GB}])
        uhd = _FakeRadarr([{"tmdbId": 1, "sizeOnDisk": 40 * GB}])
        sizes = await fairness._arr_sizes([hd, uhd], [], [req])  # type: ignore[list-item]
        assert sizes == {"700": 40 * GB}

    async def test_no_arr_configured_yields_no_sizes(self) -> None:
        req = _tv_request(tvdb_id=99, rating_key="700")
        assert await fairness._arr_sizes([], [], [req]) == {}


class TestRollUp:
    def test_a_watched_request_counts_as_played_and_is_not_reclaimable(self) -> None:
        reqs = [_request(plex_id=100, name="Alice", rating_key="555")]
        evidence = {"555": WatchEvidence(plays_by_user={100: 3}, distinct_watchers=1)}
        media = {"555": MediaInfo(title="Some Film", size_bytes=5 * GB)}

        report = evaluate_fairness(reqs, evidence, media, POLICY, now=NOW)

        (row,) = report.rows
        assert row.name == "Alice"
        assert row.requests_made == 1
        assert row.played_by_them == 1
        assert row.reclaimable_items == 0
        assert report.total_reclaimable_items == 0

    def test_an_unwatched_aged_request_is_reclaimable(self) -> None:
        reqs = [_request(plex_id=100, name="Alice", rating_key="555", days_available=400)]
        media = {"555": MediaInfo(title="Dead Weight", size_bytes=8 * GB)}

        report = evaluate_fairness(reqs, {}, media, POLICY, now=NOW)

        (row,) = report.rows
        assert row.reclaimable_items == 1
        assert row.reclaimable_bytes == 8 * GB
        assert row.unwatched_titles == ["Dead Weight"]
        assert report.total_reclaimable_bytes == 8 * GB

    def test_a_shared_reclaimable_title_counts_once_in_the_total(self) -> None:
        """Two people requested it, nobody watched it. It shows on both rows, but the file
        is deleted once -- so the report total must not double-count its bytes."""
        reqs = [
            _request(plex_id=100, name="Alice", rating_key="555", request_id=1),
            _request(plex_id=200, name="Bob", rating_key="555", request_id=2),
        ]
        media = {"555": MediaInfo(title="Nobody Watched", size_bytes=10 * GB)}

        report = evaluate_fairness(reqs, {}, media, POLICY, now=NOW)

        assert {r.name for r in report.rows} == {"Alice", "Bob"}
        assert all(r.reclaimable_bytes == 10 * GB for r in report.rows)
        # ...but deduped in the total.
        assert report.total_reclaimable_items == 1
        assert report.total_reclaimable_bytes == 10 * GB

    def test_a_request_watched_by_someone_else_protects_it(self) -> None:
        """Alice never watched what she asked for, but Bob did. Not reclaimable -- deleting
        it would punish Bob for Alice's request."""
        reqs = [_request(plex_id=100, name="Alice", rating_key="555")]
        evidence = {"555": WatchEvidence(plays_by_user={200: 2}, distinct_watchers=1)}
        media = {"555": MediaInfo(title="Bob's Favourite", size_bytes=5 * GB)}

        report = evaluate_fairness(reqs, evidence, media, POLICY, now=NOW)

        assert report.total_reclaimable_items == 0
        assert report.rows[0].played_by_them == 0

    def test_an_unmatched_request_is_surfaced_not_condemned(self) -> None:
        """No rating key: unjudgeable. It must be counted as such, never as reclaimable."""
        reqs = [_request(plex_id=100, name="Alice", rating_key=None)]

        report = evaluate_fairness(reqs, {}, {}, POLICY, now=NOW)

        assert report.unmatched_requests == 1
        assert report.total_reclaimable_items == 0

    def test_rows_are_ordered_by_disk_granted(self) -> None:
        reqs = [
            _request(plex_id=100, name="Small", rating_key="1", request_id=1),
            _request(plex_id=200, name="Big", rating_key="2", request_id=2),
        ]
        media = {
            "1": MediaInfo(title="a", size_bytes=1 * GB),
            "2": MediaInfo(title="b", size_bytes=50 * GB),
        }
        report = evaluate_fairness(reqs, {}, media, POLICY, now=NOW)
        assert [r.name for r in report.rows] == ["Big", "Small"]


# ---------------------------------------------------------------------------
# The evidence query, against a real cache table.
# ---------------------------------------------------------------------------


@pytest.fixture
async def cache_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = create_cache_engine(settings)
    await history_sync.ensure_schema(engine)
    yield engine
    await engine.dispose()


async def _insert_event(
    engine: AsyncEngine, *, rating_key: int, user_id: int, gp: int | None = None
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO watch_event (rating_key, parent_rating_key, "
                "grandparent_rating_key, user_id, watched_at, watched_status, "
                "percent_complete, media_type) VALUES (:rk, NULL, :gp, :uid, 1, 1, 100, 'movie')"
            ),
            {"rk": rating_key, "gp": gp, "uid": user_id},
        )


class TestEvidenceIndex:
    async def test_movie_plays_key_on_rating_key(self, cache_engine: AsyncEngine) -> None:
        await _insert_event(cache_engine, rating_key=555, user_id=100)
        await _insert_event(cache_engine, rating_key=555, user_id=100)
        await _insert_event(cache_engine, rating_key=555, user_id=200)

        evidence = await fairness._evidence_index(cache_engine, {"555"})

        assert evidence["555"].plays_by(100) == 2
        assert evidence["555"].plays_by(200) == 1
        assert evidence["555"].distinct_watchers == 2

    async def test_tv_plays_roll_up_to_the_grandparent(self, cache_engine: AsyncEngine) -> None:
        """A show's episodes each carry their own rating key, but the request points at the
        show. Plays must be found via the grandparent key or a binged series looks
        unwatched."""
        await _insert_event(cache_engine, rating_key=9001, user_id=100, gp=42)
        await _insert_event(cache_engine, rating_key=9002, user_id=100, gp=42)

        evidence = await fairness._evidence_index(cache_engine, {"42"})

        assert evidence["42"].plays_by(100) == 2

    async def test_a_key_with_no_history_is_absent(self, cache_engine: AsyncEngine) -> None:
        evidence = await fairness._evidence_index(cache_engine, {"999"})
        assert "999" not in evidence
