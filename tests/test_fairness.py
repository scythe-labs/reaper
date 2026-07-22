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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from reaper.clients.base import IntegrationError
from reaper.clients.seerr import (
    MediaRequest,
    QuotaStatus,
    Requester,
    SeerrUser,
    TitleInfo,
    UserQuota,
)
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Candidate, Snapshot
from reaper.db.session import create_cache_engine, create_engine, create_session_factory
from reaper.engine.requester import WatchEvidence
from reaper.services import fairness, history_sync
from reaper.services.fairness import (
    UNMATCHED_AFTER_SCAN,
    UNMATCHED_NO_ID,
    UNMATCHED_SET_ASIDE,
    CandidateInfo,
    ReclaimableTitle,
    roll_up,
)

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
    seerr_id: int | None = None,
    portal_key: str = "",
) -> MediaRequest:
    return MediaRequest(
        request_id=request_id,
        media_type=media_type,
        is_4k=False,
        status=5,
        requested_at=NOW - timedelta(days=500),
        requester=Requester(
            seerr_user_id=seerr_id if seerr_id is not None else (plex_id or 0),
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
        portal_key=portal_key,
    )


def _cand(
    *,
    cid: int = 1,
    verdict: str = "condemn",
    size: int | None = 5 * GB,
    tmdb: int | None = 1,
    imdb: str | None = "tt1",
    tvdb: int | None = None,
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
        tvdb_id=tvdb,
    )


def _user(*, seerr_id: int, plex_id: int | None, name: str = "U", count: int = 0) -> SeerrUser:
    return SeerrUser(
        seerr_user_id=seerr_id,
        plex_id=plex_id,
        username=name.lower(),
        display_name=name,
        email=None,
        request_count=count,
    )


def _q(limit: int | None, days: int | None, restricted: bool) -> QuotaStatus:
    return QuotaStatus(limit=limit, days=days, used=0, remaining=None, restricted=restricted)


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

    def test_a_tv_request_does_not_bind_a_same_numbered_movie_candidate(self) -> None:
        """TMDB movie ids and TV ids overlap numerically. A TV request for tmdb 5 must not be
        charged a movie candidate that happens to carry movie-tmdb 5 (rule 6/29)."""
        tv_req = _req(plex_id=100, name="Alice", tmdb=5, imdb=None, media_type="tv")
        movie_cand = _cand(cid=1, verdict="condemn", tmdb=5, imdb=None, media_type="movie")
        report = roll_up([tv_req], [movie_cand], {})
        # No TV candidate with tmdb 5 exists, so the request is simply not in the scan.
        assert report.not_in_scan == 1
        assert report.rows == []
        assert report.total_reclaimable_items == 0

    def test_a_tv_show_binds_its_request_by_tvdb_when_the_candidate_has_no_tmdb(self) -> None:
        """Sonarr is tvdb-native and does not always carry a tmdb id, so a season candidate can
        store only imdb + tvdb. Its Seerr request is tmdb-keyed and often has no imdb, leaving
        tvdb the only id both sides share. The join must bind on it, or a show that WAS scanned
        reads as "not in the last scan" (rule 29)."""
        tv_req = _req(plex_id=100, name="Alice", media_type="tv", tmdb=77, tvdb=9001, imdb=None)
        season_cand = _cand(
            cid=1,
            verdict="condemn",
            size=5 * GB,
            media_type="season",
            tmdb=None,
            tvdb=9001,
            imdb="tt55",
            group_key="tv:9001",
            group_title="A Show",
        )
        report = roll_up([tv_req], [season_cand], {})
        assert report.not_in_scan == 0
        assert report.total_reclaimable_items == 1
        assert report.total_reclaimable_bytes == 5 * GB

    def test_a_request_carrying_only_a_tvdb_id_is_joinable_not_no_id(self) -> None:
        """A tvdb id is a joinable id. A request with only tvdb (no tmdb, no imdb) whose show
        the scan has not seen is set-aside like any other joinable miss, never lumped into the
        truly id-less no-id bucket."""
        req = _req(plex_id=100, name="Alice", media_type="tv", tmdb=None, tvdb=9001, imdb=None)
        report = roll_up([req], [], {}, snapshot_at=NOW)
        assert report.not_in_scan == 1
        (u,) = report.unmatched
        assert u.reason == UNMATCHED_SET_ASIDE

    def test_a_movie_and_a_show_sharing_a_tmdb_number_stay_separate(self) -> None:
        movie_req = _req(
            plex_id=100, name="Alice", tmdb=5, imdb=None, media_type="movie", request_id=1
        )
        tv_req = _req(plex_id=200, name="Bob", tmdb=5, imdb=None, media_type="tv", request_id=2)
        movie_cand = _cand(
            cid=1,
            verdict="condemn",
            size=2 * GB,
            tmdb=5,
            imdb=None,
            media_type="movie",
            rating_key=1,
        )
        season_cand = _cand(
            cid=2,
            verdict="condemn",
            size=3 * GB,
            tmdb=5,
            imdb=None,
            media_type="season",
            group_key="tv:5",
            group_title="A Show",
            rating_key=2,
        )
        report = roll_up([movie_req, tv_req], [movie_cand, season_cand], {})
        by_name = {r.name: r for r in report.rows}
        assert by_name["Alice"].reclaimable_bytes == 2 * GB  # the movie, not the show
        assert by_name["Bob"].reclaimable_bytes == 3 * GB  # the show, not the movie
        assert report.total_reclaimable_items == 2
        assert report.total_reclaimable_bytes == 5 * GB

    def test_two_unlinked_requesters_stay_separate_rows(self) -> None:
        """Seerr local users not linked to Plex have no plex_id. Keying rows on plex_id folded
        every such person into one row under the first name; the Seerr id keeps them apart, and
        each is credited with their own request of a shared title (rule 12)."""
        reqs = [
            _req(plex_id=None, seerr_id=11, name="Ada", tmdb=1, imdb=None, request_id=1),
            _req(plex_id=None, seerr_id=22, name="Bea", tmdb=1, imdb=None, request_id=2),
        ]
        report = roll_up(
            reqs, [_cand(cid=9, verdict="condemn", size=4 * GB, tmdb=1, imdb=None)], {}
        )
        assert {r.name for r in report.rows} == {"Ada", "Bea"}
        assert all(r.requests_made == 1 for r in report.rows)
        assert all(r.reclaimable_bytes == 4 * GB for r in report.rows)
        # The file is deleted once, however many unlinked users asked for it.
        assert report.total_reclaimable_items == 1

    def test_two_portals_reusing_one_seerr_id_stay_separate_rows(self) -> None:
        """Each Seerr numbers its own users, so a user id is unique only within one portal:
        id 5 on the primary and id 5 on the secondary are different people. Two unlinked local
        users who share an id across portals must not merge into one row (the reported bug).
        The portal each request came from keeps them apart."""
        reqs = [
            _req(plex_id=None, seerr_id=5, name="Primary Pat", portal_key="1", request_id=1),
            _req(plex_id=None, seerr_id=5, name="Secondary Sam", portal_key="2", request_id=2),
        ]
        report = roll_up(reqs, [_cand(cid=9, verdict="condemn", size=4 * GB)], {})
        assert {r.name for r in report.rows} == {"Primary Pat", "Secondary Sam"}
        assert all(r.requests_made == 1 for r in report.rows)

    def test_one_plex_person_across_two_portals_is_one_row(self) -> None:
        """The same Plex account requesting through both portals is one human, so it folds into
        one row even though the two portals gave it different Seerr ids. A Plex-linked account
        keys on its Plex id, which is the same everywhere it appears."""
        reqs = [
            _req(plex_id=42, seerr_id=3, name="Dana", portal_key="1", request_id=1, tmdb=1),
            _req(plex_id=42, seerr_id=8, name="Dana", portal_key="2", request_id=2, tmdb=2),
        ]
        cands = [
            _cand(cid=1, verdict="condemn", size=4 * GB, tmdb=1, imdb="tt1"),
            _cand(cid=2, verdict="condemn", size=6 * GB, tmdb=2, imdb="tt2"),
        ]
        report = roll_up(reqs, cands, {})
        (row,) = report.rows
        assert row.name == "Dana" and row.requests_made == 2

    def test_the_same_title_via_a_tmdb_and_an_imdb_request_counts_once(self) -> None:
        """One request carries tmdb+imdb (groups by tmdb), another only imdb (groups by imdb);
        both bind the same candidate. The items total dedupes by candidate, like the bytes."""
        reqs = [
            _req(plex_id=100, name="Alice", tmdb=1, imdb="tt1", request_id=1),
            _req(plex_id=200, name="Bob", tmdb=None, imdb="tt1", request_id=2),
        ]
        report = roll_up(
            reqs, [_cand(cid=9, verdict="condemn", size=6 * GB, tmdb=1, imdb="tt1")], {}
        )
        assert report.total_reclaimable_items == 1
        assert report.total_reclaimable_bytes == 6 * GB

    def test_an_unmeasured_reclaimable_title_carries_a_null_size(self) -> None:
        """A condemned title the arr would not size shows "size unknown" (a null), never a
        false 0 B, and its bytes stay out of the totals."""
        report = roll_up(
            [_req(plex_id=100, name="Alice")],
            [_cand(cid=7, verdict="condemn", size=None, title="Unsized")],
            {},
        )
        (row,) = report.rows
        assert row.reclaimable == [
            ReclaimableTitle(title="Unsized", size_bytes=None, item_id=7, group_key=None)
        ]
        assert row.reclaimable_bytes == 0
        assert report.total_reclaimable_bytes == 0
        assert report.total_reclaimable_items == 1


class TestUnmatched:
    """The "not in the last scan" list: what each unmatched request is, and why. The count
    stays exactly the requests behind the list, so the card and the panel never disagree."""

    def test_no_id_request_is_listed_and_reasoned(self) -> None:
        report = roll_up([_req(plex_id=100, name="Alice", tmdb=None, imdb=None)], [], {})
        assert report.not_in_scan == 1
        (u,) = report.unmatched
        assert u.reason == UNMATCHED_NO_ID
        assert u.requested_by == ["Alice"]
        assert u.request_count == 1
        # The name is filled in later (by _enrich_titles), never guessed in the pure roll-up.
        assert u.title is None

    def test_media_that_arrived_after_the_scan_is_added_since(self) -> None:
        """available_at (NOW-400d) is AFTER the scan clock (NOW-450d), so the scan could not
        have seen it: added since, not set aside."""
        req = _req(plex_id=100, name="Alice", tmdb=999, imdb=None)
        report = roll_up([req], [], {}, snapshot_at=NOW - timedelta(days=450))
        (u,) = report.unmatched
        assert u.reason == UNMATCHED_AFTER_SCAN

    def test_media_present_at_scan_time_is_set_aside(self) -> None:
        """available_at (NOW-400d) is BEFORE the scan clock (NOW-300d): it was on the server
        during the scan but produced no candidate, so it was set aside, not added since."""
        req = _req(plex_id=100, name="Alice", tmdb=999, imdb=None)
        report = roll_up([req], [], {}, snapshot_at=NOW - timedelta(days=300))
        (u,) = report.unmatched
        assert u.reason == UNMATCHED_SET_ASIDE

    def test_without_a_scan_clock_the_reason_stays_set_aside(self) -> None:
        """No snapshot time to compare against rounds toward the honest catch-all, never a
        reassuring "added since" that cannot be proven."""
        req = _req(plex_id=100, name="Alice", tmdb=999, imdb=None)
        report = roll_up([req], [], {})
        (u,) = report.unmatched
        assert u.reason == UNMATCHED_SET_ASIDE

    def test_co_requesters_merge_into_one_titled_row_the_count_still_totals_requests(
        self,
    ) -> None:
        """Two people asking for the same not-in-scan title is one panel row (one title) but
        two requests, so not_in_scan stays 2 and the row names both."""
        reqs = [
            _req(plex_id=100, name="Alice", tmdb=999, imdb=None, request_id=1),
            _req(plex_id=200, name="Bob", tmdb=999, imdb=None, request_id=2),
        ]
        report = roll_up(reqs, [], {})
        (u,) = report.unmatched
        assert u.request_count == 2
        assert u.requested_by == ["Alice", "Bob"]
        assert report.not_in_scan == sum(x.request_count for x in report.unmatched) == 2

    def test_a_4k_request_marks_the_row_4k_and_the_type_is_tv(self) -> None:
        req = _req(plex_id=100, name="Alice", tmdb=999, imdb=None, media_type="tv")
        object.__setattr__(req, "is_4k", True)  # frozen dataclass; flip just this field
        report = roll_up([req], [], {})
        (u,) = report.unmatched
        assert u.is_4k is True
        assert u.media_type == "tv"

    def test_a_matched_title_never_appears_in_the_unmatched_list(self) -> None:
        report = roll_up(
            [_req(plex_id=100, name="Alice", tmdb=1, imdb="tt1")],
            [_cand(cid=1, verdict="protect", tmdb=1, imdb="tt1")],
            {},
        )
        assert report.unmatched == []
        assert report.not_in_scan == 0


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


# ---------------------------------------------------------------------------
# build_report reads every Seerr (the reported "second portal is missing" bug)
# ---------------------------------------------------------------------------


_UNLIMITED = QuotaStatus(limit=None, days=None, used=0, remaining=None, restricted=False)


class _FakeSeerr:
    def __init__(
        self,
        requests: list[MediaRequest],
        users: list[SeerrUser] | None = None,
        quotas: dict[int, UserQuota] | None = None,
        titles: dict[int, TitleInfo] | None = None,
        instance_key: str = "",
        base_url: str = "https://seerr.example",
    ) -> None:
        self._requests = requests
        self._users = users or []
        self._quotas = quotas or {}
        self._titles = titles or {}
        self.instance_key = instance_key
        self.base_url = base_url

    async def all_requests(self, *, filter_: str = "available") -> list[MediaRequest]:
        return self._requests

    async def users(self, *, take: int = 100) -> list[SeerrUser]:
        return self._users

    async def quota(self, user_id: int) -> UserQuota:
        return self._quotas.get(user_id, UserQuota(movie=_UNLIMITED, tv=_UNLIMITED))

    async def title(self, *, tmdb_id: int, media_type: str) -> TitleInfo:
        info = self._titles.get(tmdb_id)
        if info is None:
            raise IntegrationError("seerr", f"no title for {tmdb_id}")
        return info


class _Broken:
    async def all_requests(self, *, filter_: str = "available") -> list[MediaRequest]:
        raise IntegrationError("seerr", "down")

    async def users(self, *, take: int = 100) -> list[SeerrUser]:
        raise IntegrationError("seerr", "down")

    async def quota(self, user_id: int) -> UserQuota:
        raise IntegrationError("seerr", "down")


@pytest.fixture
async def report_env(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], AsyncEngine]]:
    """A session factory holding one snapshot with a single condemned movie at tmdb=1, plus
    a cache engine, so ``build_report`` has a real scan to sit on."""
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    main = create_engine(settings)
    async with main.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    cache = create_cache_engine(settings)
    await history_sync.ensure_schema(cache)
    factory = create_session_factory(main)
    async with factory() as session:
        snap = Snapshot(
            created_at=NOW, policy_hash="p" * 64, horizon_at=NOW, item_count=1, degraded=False
        )
        session.add(snap)
        await session.flush()
        session.add(
            Candidate(
                snapshot_id=snap.id,
                media_key="radarr:1:1",
                title="A Film",
                media_type="movie",
                size_bytes=5 * GB,
                verdict="condemn",
                score=80,
                coverage_bp=10_000,
                explanation_json="{}",
                tmdb_id=1,
                imdb_id="tt1",
                plex_rating_key=555,
                created_at=NOW,
            )
        )
        await session.commit()
    yield factory, cache
    await main.dispose()
    await cache.dispose()


class TestBuildReportMergesSeerrs:
    async def test_a_requester_only_in_the_second_portal_still_appears(
        self, report_env: tuple[async_sessionmaker[AsyncSession], AsyncEngine]
    ) -> None:
        # Alice used portal one, Bob only ever used portal two. Both must land on the board.
        factory, cache = report_env
        first = _FakeSeerr([_req(plex_id=1, name="Alice", tmdb=1)])
        second = _FakeSeerr([_req(plex_id=2, name="Bob", tmdb=1, request_id=2)])
        report = await fairness.build_report(
            session_factory=factory,  # type: ignore[arg-type]
            seerrs=[first, second],  # type: ignore[list-item]
            cache_engine=cache,
        )
        assert {r.name for r in report.rows} == {"Alice", "Bob"}
        assert report.total_requests == 2

    async def test_a_not_in_scan_request_is_named_from_seerr_and_classified(
        self, report_env: tuple[async_sessionmaker[AsyncSession], AsyncEngine]
    ) -> None:
        """A request whose title the scan never saw is listed, named from Seerr's TMDB proxy,
        and reasoned. The snapshot clock here is NOW, and this request arrived NOW-400d, so it
        was present at scan time: set aside, not added since."""
        factory, cache = report_env
        seerr = _FakeSeerr(
            [_req(plex_id=1, name="Alice", tmdb=42, imdb=None)],
            titles={42: TitleInfo(title="Some Requested Film", year=2021)},
        )
        report = await fairness.build_report(
            session_factory=factory,  # type: ignore[arg-type]
            seerrs=[seerr],  # type: ignore[list-item]
            cache_engine=cache,
        )
        assert report.not_in_scan == 1
        (u,) = report.unmatched
        assert u.title == "Some Requested Film"
        assert u.year == 2021
        assert u.reason == UNMATCHED_SET_ASIDE

    async def test_an_unnamed_title_falls_back_gracefully_never_blocks_the_report(
        self, report_env: tuple[async_sessionmaker[AsyncSession], AsyncEngine]
    ) -> None:
        """A title lookup that fails leaves the row unnamed (the view shows a generic label),
        and the report is still built: naming is best-effort, exactly like quota enrichment."""
        factory, cache = report_env
        seerr = _FakeSeerr([_req(plex_id=1, name="Alice", tmdb=77, imdb=None)])  # no titles
        report = await fairness.build_report(
            session_factory=factory,  # type: ignore[arg-type]
            seerrs=[seerr],  # type: ignore[list-item]
            cache_engine=cache,
        )
        (u,) = report.unmatched
        assert u.title is None
        assert report.not_in_scan == 1

    async def test_one_unreachable_portal_fails_hard_never_partial(
        self, report_env: tuple[async_sessionmaker[AsyncSession], AsyncEngine]
    ) -> None:
        # A read-only report must 502 (propagate) rather than quietly drop a portal and look
        # complete: the endpoint maps this IntegrationError to a 502.
        factory, cache = report_env
        good = _FakeSeerr([_req(plex_id=1, name="Alice", tmdb=1)])
        with pytest.raises(IntegrationError):
            await fairness.build_report(
                session_factory=factory,  # type: ignore[arg-type]
                seerrs=[good, _Broken()],  # type: ignore[list-item]
                cache_engine=cache,
            )


class TestFoldQuota:
    def test_no_readable_quota_reads_as_unlimited_never_a_made_up_cap(self) -> None:
        line = fairness._fold_quota([])
        assert line.unlimited is True and line.at_limit is False

    def test_tightest_finite_limit_wins_and_at_limit_is_or_ed(self) -> None:
        line = fairness._fold_quota([_q(5, 30, False), _q(1, 14, True)])
        assert (line.limit, line.days, line.at_limit) == (1, 14, True)


class TestEnrichAccounts:
    async def test_sums_counts_and_ors_restriction_across_portals(self) -> None:
        # One person with an account on two portals: counts add, and each type's cap is the
        # tightest across portals with restriction OR-ed. Movie and TV stay independent.
        a = _FakeSeerr(
            [],
            users=[_user(seerr_id=10, plex_id=1, name="Alex", count=100)],
            quotas={10: UserQuota(movie=_q(1, 14, True), tv=_UNLIMITED)},
        )
        b = _FakeSeerr(
            [],
            users=[_user(seerr_id=20, plex_id=1, name="Alex", count=69)],
            quotas={20: UserQuota(movie=_UNLIMITED, tv=_q(1, 60, False))},
        )
        out = await fairness._enrich_accounts([a, b], {1})  # type: ignore[list-item]
        pq = out[1]
        assert pq.seerr_total == 169
        assert (pq.movie.limit, pq.movie.days, pq.movie.at_limit) == (1, 14, True)
        assert (pq.tv.limit, pq.tv.days, pq.tv.at_limit) == (1, 60, False)

    async def test_a_broken_portal_is_skipped_not_fatal(self) -> None:
        good = _FakeSeerr([], users=[_user(seerr_id=10, plex_id=1, count=5)])
        out = await fairness._enrich_accounts([good, _Broken()], {1})  # type: ignore[list-item]
        assert out[1].seerr_total == 5

    async def test_an_unmatched_requester_has_no_seerr_account(self) -> None:
        good = _FakeSeerr([], users=[_user(seerr_id=10, plex_id=1, count=5)])
        out = await fairness._enrich_accounts([good], {None})  # type: ignore[list-item]
        assert out == {}


class TestBuildReportEnriches:
    async def test_rows_carry_the_seerr_total_and_which_limit_is_hit(
        self, report_env: tuple[async_sessionmaker[AsyncSession], AsyncEngine]
    ) -> None:
        factory, cache = report_env
        portal = _FakeSeerr(
            [_req(plex_id=1, name="Alice", tmdb=1)],
            users=[_user(seerr_id=1, plex_id=1, name="Alice", count=169)],
            quotas={1: UserQuota(movie=_q(1, 14, True), tv=_UNLIMITED)},
        )
        report = await fairness.build_report(
            session_factory=factory,  # type: ignore[arg-type]
            seerrs=[portal],  # type: ignore[list-item]
            cache_engine=cache,
        )
        (row,) = report.rows
        assert row.seerr_total == 169
        assert row.movie_at_limit is True and row.tv_at_limit is False

    async def test_unreadable_accounts_leave_totals_none_not_a_blocked_page(
        self, report_env: tuple[async_sessionmaker[AsyncSession], AsyncEngine]
    ) -> None:
        # Requests read fine; the user list does not. The board still renders, minus totals.
        factory, cache = report_env

        class _RequestsOnly(_FakeSeerr):
            async def users(self, *, take: int = 100) -> list[SeerrUser]:
                raise IntegrationError("seerr", "user list down")

        portal = _RequestsOnly([_req(plex_id=1, name="Alice", tmdb=1)])
        report = await fairness.build_report(
            session_factory=factory,  # type: ignore[arg-type]
            seerrs=[portal],  # type: ignore[list-item]
            cache_engine=cache,
        )
        (row,) = report.rows
        assert row.seerr_total is None and row.movie_at_limit is False


class TestBuildPersonDetail:
    async def test_lists_a_persons_titles_with_fate_and_co_requesters(
        self, report_env: tuple[async_sessionmaker[AsyncSession], AsyncEngine]
    ) -> None:
        factory, cache = report_env
        portal = _FakeSeerr(
            [
                _req(plex_id=1, name="Alice", tmdb=1),
                _req(plex_id=2, name="Bob", tmdb=1, request_id=2),
            ],
            users=[_user(seerr_id=1, plex_id=1, name="Alice", count=169)],
        )
        detail = await fairness.build_person_detail(
            session_factory=factory,  # type: ignore[arg-type]
            seerrs=[portal],  # type: ignore[list-item]
            cache_engine=cache,
            identity="plex:1",
        )
        assert detail is not None
        assert detail.name == "Alice" and detail.seerr_total == 169
        assert detail.requests_in_scan == 1 and detail.reclaimable_items == 1
        (title,) = detail.titles
        assert title.verdict == "condemn" and title.item_id is not None
        # The co-requester is named, so a shared title is never read as one person's alone.
        assert title.co_requesters == ("Bob",)
        # The poster is proxied through our image route, falling back to the item's own key
        # when it has no separate poster key.
        assert title.poster_url == "/api/poster/555"

    async def test_name_links_to_the_requesters_portal_profile(
        self, report_env: tuple[async_sessionmaker[AsyncSession], AsyncEngine]
    ) -> None:
        """The panel links the name to this person's page on the portal they requested
        through ({base_url}/users/{id}), built from their own request so it needs no extra
        Seerr read."""
        factory, cache = report_env
        portal = _FakeSeerr(
            [_req(plex_id=1, name="Alice", tmdb=1, seerr_id=7, portal_key="p1")],
            base_url="https://seerr.example",
            instance_key="p1",
        )
        detail = await fairness.build_person_detail(
            session_factory=factory,  # type: ignore[arg-type]
            seerrs=[portal],  # type: ignore[list-item]
            cache_engine=cache,
            identity="plex:1",
        )
        assert detail is not None
        assert detail.profile_url == "https://seerr.example/users/7"

    async def test_profile_url_is_none_without_a_user_id(
        self, report_env: tuple[async_sessionmaker[AsyncSession], AsyncEngine]
    ) -> None:
        """No Seerr user id on the request means no page to link to: the name stays plain
        text rather than a dead link."""
        factory, cache = report_env
        portal = _FakeSeerr([_req(plex_id=1, name="Alice", tmdb=1, seerr_id=0)])
        detail = await fairness.build_person_detail(
            session_factory=factory,  # type: ignore[arg-type]
            seerrs=[portal],  # type: ignore[list-item]
            cache_engine=cache,
            identity="plex:1",
        )
        assert detail is not None
        assert detail.profile_url is None

    async def test_an_unknown_key_is_none(
        self, report_env: tuple[async_sessionmaker[AsyncSession], AsyncEngine]
    ) -> None:
        factory, cache = report_env
        portal = _FakeSeerr([_req(plex_id=1, name="Alice", tmdb=1)])
        detail = await fairness.build_person_detail(
            session_factory=factory,  # type: ignore[arg-type]
            seerrs=[portal],  # type: ignore[list-item]
            cache_engine=cache,
            identity="plex:999",
        )
        assert detail is None

    async def test_a_title_the_person_watched_is_counted(
        self, report_env: tuple[async_sessionmaker[AsyncSession], AsyncEngine]
    ) -> None:
        factory, cache = report_env
        await _insert_event(cache, rating_key=555, user_id=1)  # Alice (plex 1) played it
        portal = _FakeSeerr([_req(plex_id=1, name="Alice", tmdb=1)])
        detail = await fairness.build_person_detail(
            session_factory=factory,  # type: ignore[arg-type]
            seerrs=[portal],  # type: ignore[list-item]
            cache_engine=cache,
            identity="plex:1",
        )
        assert detail is not None
        assert detail.played_by_them == 1 and detail.titles[0].watched_by_them == 1

    async def test_a_persons_not_in_scan_request_is_listed_and_named(
        self, report_env: tuple[async_sessionmaker[AsyncSession], AsyncEngine]
    ) -> None:
        """Alice asked for one title the scan has (tmdb=1) and one it never saw (tmdb=2). The
        drawer lists the in-scan one as a title and the other in her not-in-scan panel, named
        from Seerr and counted -- so her panel reads as most of what she asked for, not all."""
        factory, cache = report_env
        portal = _FakeSeerr(
            [
                _req(plex_id=1, name="Alice", tmdb=1, imdb="tt1", request_id=1),
                _req(plex_id=1, name="Alice", tmdb=2, imdb=None, request_id=2),
            ],
            titles={2: TitleInfo(title="A Title The Scan Missed", year=2020)},
        )
        detail = await fairness.build_person_detail(
            session_factory=factory,  # type: ignore[arg-type]
            seerrs=[portal],  # type: ignore[list-item]
            cache_engine=cache,
            identity="plex:1",
        )
        assert detail is not None
        assert detail.requests_in_scan == 1
        assert detail.not_in_scan == 1
        (u,) = detail.unmatched
        assert u.title == "A Title The Scan Missed"
        assert u.reason == UNMATCHED_SET_ASIDE
        assert u.request_count == 1

    async def test_distinct_episodes_counts_episodes_not_replays(
        self, report_env: tuple[async_sessionmaker[AsyncSession], AsyncEngine]
    ) -> None:
        _factory, cache = report_env
        # Two plays of one episode plus one play of another, all under season 770.
        await _insert_event(cache, rating_key=9001, user_id=1, parent=770, gp=42)
        await _insert_event(cache, rating_key=9001, user_id=1, parent=770, gp=42)
        await _insert_event(cache, rating_key=9002, user_id=1, parent=770, gp=42)
        eps = await fairness._distinct_episodes(cache, plex_id=1, season_keys={770})
        # Two distinct episodes, not three raw plays -- the panel's "N episodes watched".
        assert eps == {770: 2}
