# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scales -- the requester roll-up over the last scan.

Two halves, tested apart: the pure roll-up (``roll_up``, no instance or DB needed), which
joins requests to the scan's candidates and lets the scan's verdict decide what is
reclaimable; and the watch-evidence query against a real ``watch_event`` table (a movie
keys on rating_key, a season on its parent, a show on its grandparent).

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
from reaper.engine.requester import WatchEvidence
from reaper.services import fairness, history_sync
from reaper.services.fairness import CandidateInfo, ReclaimableTitle, roll_up

GB = 1024**3
NOW = utcnow()


def _req(
    *,
    plex_id: int | None,
    name: str,
    tmdb: int | None = 1,
    imdb: str | None = "tt1",
    request_id: int = 1,
    media_type: str = "movie",
    tvdb: int | None = None,
) -> MediaRequest:
    return MediaRequest(
        request_id=request_id,
        media_type=media_type,
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
        tmdb_id=tmdb,
        tvdb_id=tvdb,
        imdb_id=imdb,
        plex_rating_key=None,  # Scales joins on external ids, never the (stale-prone) key.
        arr_id=1,
        arr_instance_id=0,
        available_at=NOW - timedelta(days=400),
    )


def _cand(
    *,
    cid: int = 1,
    verdict: str = "condemn",
    size: int = 5 * GB,
    tmdb: int | None = 1,
    imdb: str | None = "tt1",
    rating_key: int | None = 555,
    media_type: str = "movie",
    group_key: str | None = None,
    group_title: str | None = None,
    title: str = "A Film",
) -> CandidateInfo:
    return CandidateInfo(
        candidate_id=cid,
        plex_rating_key=rating_key,
        verdict=verdict,
        size_bytes=size,
        title=title,
        media_type=media_type,
        group_key=group_key,
        group_title=group_title,
        tmdb_id=tmdb,
        imdb_id=imdb,
    )


class TestRollUp:
    def test_a_condemned_unwatched_request_is_reclaimable_and_links_to_its_item(self) -> None:
        report = roll_up(
            [_req(plex_id=100, name="Alice")],
            [_cand(cid=7, verdict="condemn", size=8 * GB, title="Dead Weight")],
            {},
        )
        (row,) = report.rows
        assert row.name == "Alice"
        assert row.requests_made == 1
        assert row.played_by_them == 0
        assert row.reclaimable_items == 1
        assert row.reclaimable_bytes == 8 * GB
        assert row.reclaimable == [
            ReclaimableTitle(title="Dead Weight", size_bytes=8 * GB, item_id=7, group_key=None)
        ]
        assert report.total_reclaimable_items == 1
        assert report.total_reclaimable_bytes == 8 * GB

    def test_a_protected_title_is_never_reclaimable_even_if_the_requester_never_watched(
        self,
    ) -> None:
        """The Little Fockers case: nobody on this row watched it, but the scan protects it
        (watched too recently, on a keep list, ...). Scales must never contradict Review."""
        report = roll_up(
            [_req(plex_id=100, name="Alice")],
            [_cand(verdict="protect", title="Kept By The Scan")],
            {},
        )
        assert report.total_reclaimable_items == 0
        assert report.rows[0].reclaimable_items == 0

    def test_an_abstained_title_is_not_reclaimable(self) -> None:
        """Abstain is 'kept to be safe', so it is not offered up either -- reclaimable is the
        condemn lane alone."""
        report = roll_up([_req(plex_id=100, name="Alice")], [_cand(verdict="abstain")], {})
        assert report.total_reclaimable_items == 0

    def test_a_watched_request_counts_as_played(self) -> None:
        report = roll_up(
            [_req(plex_id=100, name="Alice")],
            [_cand(verdict="protect", rating_key=555)],
            {"555": WatchEvidence(plays_by_user={100: 3}, distinct_watchers=1)},
        )
        assert report.rows[0].played_by_them == 1

    def test_watched_is_keyed_on_the_candidates_rating_key_not_the_requests(self) -> None:
        """The whole point of sitting on the scan: watches are found by the candidate's own
        key, so a stale key on the Seerr request can no longer read a play as never-watched.
        The requester carries no rating key at all here, and is still credited with the play."""
        report = roll_up(
            [_req(plex_id=100, name="Alice")],
            [_cand(verdict="condemn", rating_key=900)],
            {"900": WatchEvidence(plays_by_user={100: 1}, distinct_watchers=1)},
        )
        assert report.rows[0].played_by_them == 1
        # Still reclaimable: the scan condemned it (watched long ago, now dormant).
        assert report.rows[0].reclaimable_items == 1

    def test_a_shared_reclaimable_title_counts_once_in_the_total(self) -> None:
        reqs = [
            _req(plex_id=100, name="Alice", request_id=1),
            _req(plex_id=200, name="Bob", request_id=2),
        ]
        report = roll_up(reqs, [_cand(cid=9, verdict="condemn", size=10 * GB)], {})
        assert {r.name for r in report.rows} == {"Alice", "Bob"}
        assert all(r.reclaimable_bytes == 10 * GB for r in report.rows)
        # ...but deduped in the total: the file is deleted once.
        assert report.total_reclaimable_items == 1
        assert report.total_reclaimable_bytes == 10 * GB

    def test_a_request_the_scan_has_not_seen_is_not_in_scan(self) -> None:
        # Request points at tmdb 999; the only candidate is tmdb 1.
        report = roll_up(
            [_req(plex_id=100, name="Alice", tmdb=999, imdb="tt999")], [_cand(tmdb=1)], {}
        )
        assert report.not_in_scan == 1
        assert report.rows == []
        assert report.total_reclaimable_items == 0

    def test_a_request_with_no_external_id_is_not_in_scan(self) -> None:
        report = roll_up([_req(plex_id=100, name="Alice", tmdb=None, imdb=None)], [], {})
        assert report.not_in_scan == 1

    def test_not_in_scan_is_counted_per_request(self) -> None:
        reqs = [
            _req(plex_id=100, name="Alice", tmdb=999, imdb=None, request_id=1),
            _req(plex_id=200, name="Bob", tmdb=999, imdb=None, request_id=2),
        ]
        report = roll_up(reqs, [], {})
        assert report.not_in_scan == 2

    def test_a_show_links_to_its_group_and_charges_its_condemned_seasons(self) -> None:
        """A show maps to several season candidates. Reclaimable is the sum of the CONDEMNED
        seasons' disk, and the chip opens the show (its group), not one season."""
        req = _req(plex_id=100, name="Alice", tmdb=7, imdb=None, media_type="tv")
        cands = [
            _cand(
                cid=1,
                verdict="condemn",
                size=3 * GB,
                tmdb=7,
                imdb=None,
                rating_key=801,
                media_type="season",
                group_key="tv:7",
                group_title="A Show",
                title="Season 1",
            ),
            _cand(
                cid=2,
                verdict="protect",
                size=4 * GB,
                tmdb=7,
                imdb=None,
                rating_key=802,
                media_type="season",
                group_key="tv:7",
                group_title="A Show",
                title="Season 2",
            ),
        ]
        report = roll_up([req], cands, {})
        (row,) = report.rows
        # Granted is the whole show; reclaimable is only the condemned season.
        assert row.gb_granted_bytes == 7 * GB
        assert row.reclaimable_bytes == 3 * GB
        assert row.reclaimable == [
            ReclaimableTitle(title="A Show", size_bytes=3 * GB, item_id=None, group_key="tv:7")
        ]

    def test_rows_are_ordered_by_disk_granted(self) -> None:
        reqs = [
            _req(plex_id=100, name="Small", tmdb=1, imdb=None, request_id=1),
            _req(plex_id=200, name="Big", tmdb=2, imdb=None, request_id=2),
        ]
        cands = [
            _cand(cid=1, verdict="protect", size=1 * GB, tmdb=1, imdb=None),
            _cand(cid=2, verdict="protect", size=50 * GB, tmdb=2, imdb=None),
        ]
        report = roll_up(reqs, cands, {})
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
    engine: AsyncEngine,
    *,
    rating_key: int,
    user_id: int,
    parent: int | None = None,
    gp: int | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO watch_event (rating_key, parent_rating_key, "
                "grandparent_rating_key, user_id, watched_at, watched_status, "
                "percent_complete, media_type) "
                "VALUES (:rk, :pk, :gp, :uid, 1, 1, 100, 'movie')"
            ),
            {"rk": rating_key, "pk": parent, "gp": gp, "uid": user_id},
        )


class TestEvidenceIndex:
    async def test_movie_plays_key_on_rating_key(self, cache_engine: AsyncEngine) -> None:
        await _insert_event(cache_engine, rating_key=555, user_id=100)
        await _insert_event(cache_engine, rating_key=555, user_id=100)
        await _insert_event(cache_engine, rating_key=555, user_id=200)

        evidence = await fairness._evidence_index(cache_engine, {555})

        assert evidence["555"].plays_by(100) == 2
        assert evidence["555"].plays_by(200) == 1
        assert evidence["555"].distinct_watchers == 2

    async def test_season_plays_roll_up_to_the_parent(self, cache_engine: AsyncEngine) -> None:
        """Episode plays carry the season as their parent, so a season candidate finds them
        via the parent key."""
        await _insert_event(cache_engine, rating_key=9001, user_id=100, parent=770, gp=42)
        await _insert_event(cache_engine, rating_key=9002, user_id=100, parent=770, gp=42)

        evidence = await fairness._evidence_index(cache_engine, {770})

        assert evidence["770"].plays_by(100) == 2

    async def test_show_plays_roll_up_to_the_grandparent(self, cache_engine: AsyncEngine) -> None:
        await _insert_event(cache_engine, rating_key=9001, user_id=100, parent=770, gp=42)
        await _insert_event(cache_engine, rating_key=9002, user_id=100, parent=771, gp=42)

        evidence = await fairness._evidence_index(cache_engine, {42})

        assert evidence["42"].plays_by(100) == 2

    async def test_a_key_with_no_history_is_absent(self, cache_engine: AsyncEngine) -> None:
        evidence = await fairness._evidence_index(cache_engine, {999})
        assert "999" not in evidence
