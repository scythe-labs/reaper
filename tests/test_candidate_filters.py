# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review queue's filters: search, media type, and requested-only.

Filters narrow the frozen snapshot. They never re-decide it. Each one is a display-side
convenience over candidates that were already judged, and the queue also carries the
display fields (poster, blurb, requested-by, show grouping) captured at scan time.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from reaper.api.review import _split_search_year
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.models import Candidate, Snapshot, WhitelistEntry
from reaper.main import create_app
from reaper.services.whitelist import show_key

from ._auth import login


def _candidate(**kw: object) -> Candidate:
    base: dict[str, object] = {
        "media_type": "movie",
        "size_bytes": 1_000_000_000,
        "verdict": "condemn",
        "score": 90,
        "coverage_bp": 10_000,
        "explanation_json": "{}",
        "created_at": utcnow(),
    }
    base.update(kw)
    return Candidate(**base)


@pytest.fixture
def client(settings: Settings, sync_db: Engine) -> Iterator[TestClient]:
    now = utcnow()
    with Session(sync_db) as session:
        snap = Snapshot(
            created_at=now, policy_hash="a" * 64, horizon_at=now, item_count=3, degraded=False
        )
        session.add(snap)
        session.flush()
        session.add_all(
            [
                _candidate(
                    snapshot_id=snap.id,
                    media_key="radarr:1:10",
                    plex_rating_key=555,  # -> poster served from /api/poster/555
                    title="Example Alpha",
                    year=1979,
                    summary="A crew is hunted.",
                    requested_by="Alice",
                    genres_json='["Comedy", "Horror"]',
                    library_title="Movies",
                ),
                _candidate(
                    snapshot_id=snap.id,
                    media_key="radarr:1:11",
                    title="Example Zulu",
                    year=1995,
                    requested_by=None,
                    # "Comedy Special" proves the genre filter matches whole terms, not
                    # substrings. A filter for Comedy must not drag this row along.
                    genres_json='["Comedy Special"]',
                    # A distinct library whose name contains "Movies". The library filter
                    # must match the whole name, not drag this in on a filter for "Movies".
                    library_title="4K Movies",
                ),
                _candidate(
                    snapshot_id=snap.id,
                    media_key="sonarr:1:5:5",
                    title="Example Mid · Season 5",
                    media_type="season",
                    group_key="sonarr:1:5",
                    group_title="Example Mid",
                    requested_by="Bob",
                    # Malformed on purpose: the genre filter must skip it, never 500.
                    genres_json="not json",
                    library_title="TV Shows",
                ),
            ]
        )
        # Hand overrides: one item-level spare, and one show-level reap the season
        # inherits through whitelist.effective_override's own-key-beats-show precedence.
        session.add_all(
            [
                WhitelistEntry(
                    media_key="radarr:1:11", title="Example Zulu", decision="spare", created_at=now
                ),
                WhitelistEntry(
                    media_key="sonarr:1:5", title="Example Mid", decision="reap", created_at=now
                ),
            ]
        )
        session.commit()
    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


def _titles(rows: list[dict[str, object]]) -> set[str]:
    return {str(r["title"]) for r in rows}


class TestFilters:
    def test_unfiltered_returns_every_condemned_item(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn").json()["items"]
        # Two, not three: Example Zulu is condemned by policy but spared by hand, so it rides the
        # Kept lane now while its stored verdict stays pure "condemn" underneath. The reaped season
        # stays here, its hand reap being effective.
        assert len(rows) == 2
        # The display fields ride along.
        alpha = next(r for r in rows if r["title"] == "Example Alpha")
        assert alpha["year"] == 1979
        # The poster is served from Plex through our proxy, keyed by the rating key.
        assert alpha["poster_url"] == "/api/poster/555"
        assert alpha["requested_by"] == "Alice"

    def test_search_matches_title(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&search=alpha").json()["items"]
        assert _titles(rows) == {"Example Alpha"}

    def test_search_understands_a_year_typed_after_the_title(self, client: TestClient) -> None:
        # The queue prints the year in its own span beside the title, so the operator
        # reads "Example Alpha 1979" as one string and types it back. It used to match
        # nothing, since the year lives in its own column and was never in `title`.
        for term in ("Example Alpha 1979", "Example Alpha (1979)", "alpha 1979"):
            rows = client.get(f"/api/candidates?verdict=condemn&search={term}").json()["items"]
            assert _titles(rows) == {"Example Alpha"}, term

    def test_search_with_the_wrong_year_finds_nothing(self, client: TestClient) -> None:
        # The year narrows. It is not decoration. Example Alpha is 1979.
        rows = client.get("/api/candidates?verdict=condemn&search=Example Alpha 1980").json()[
            "items"
        ]
        assert rows == []

    def test_search_also_matches_the_show_name(self, client: TestClient) -> None:
        # "mid" matches the show name, carried on the season row's group_title.
        rows = client.get("/api/candidates?verdict=condemn&search=mid").json()["items"]
        assert _titles(rows) == {"Example Mid · Season 5"}

    def test_media_type_filter(self, client: TestClient) -> None:
        movies = client.get("/api/candidates?verdict=condemn&media_type=movie").json()["items"]
        # Example Zulu is a movie too, but spared by hand, so it rides the Kept lane now.
        assert _titles(movies) == {"Example Alpha"}
        kept_movies = client.get("/api/candidates?verdict=protect&media_type=movie").json()["items"]
        assert _titles(kept_movies) == {"Example Zulu"}
        seasons = client.get("/api/candidates?verdict=condemn&media_type=season").json()["items"]
        assert _titles(seasons) == {"Example Mid · Season 5"}

    def test_requested_yes_keeps_just_requested_media(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&requested=yes").json()["items"]
        assert _titles(rows) == {
            "Example Alpha",
            "Example Mid · Season 5",
        }  # Example Zulu had no requester

    def test_requested_no_keeps_just_the_unrequested(self, client: TestClient) -> None:
        # The only unrequested title, Example Zulu, is spared by hand, so it rides the Kept lane.
        rows = client.get("/api/candidates?verdict=protect&requested=no").json()["items"]
        assert _titles(rows) == {"Example Zulu"}  # the only one nobody asked for

    def test_filters_stack(self, client: TestClient) -> None:
        # media_type AND requested are ANDed, not either-or.
        rows = client.get("/api/candidates?verdict=condemn&media_type=movie&requested=yes").json()[
            "items"
        ]
        assert _titles(rows) == {"Example Alpha"}  # a movie AND requested, the season is out


class TestGenreFilter:
    def test_a_genre_matches_the_whole_term_only(self, client: TestClient) -> None:
        # "Comedy" must match ["Comedy", ...], not ["Comedy Special"].
        rows = client.get("/api/candidates?verdict=condemn&genre=Comedy").json()["items"]
        assert _titles(rows) == {"Example Alpha"}

    def test_a_malformed_genre_row_is_skipped_not_an_error(self, client: TestClient) -> None:
        # The season row's genres_json does not parse. It never matches and never 500s.
        response = client.get("/api/candidates?verdict=condemn&genre=Horror")
        assert response.status_code == 200
        assert _titles(response.json()["items"]) == {"Example Alpha"}

    def test_an_unseen_genre_matches_nothing(self, client: TestClient) -> None:
        page = client.get("/api/candidates?verdict=condemn&genre=Western").json()
        assert page["items"] == []
        assert page["total"] == 0


class TestCollectionFilter:
    """``collection`` is genre's sibling: same predicate, over ``collections_json``
    instead of ``genres_json``. Collections are navigation, never protection. This
    filter only narrows the frozen snapshot, same as every other one on this route."""

    @pytest.fixture
    def client(self, settings: Settings, sync_db: Engine) -> Iterator[TestClient]:
        now = utcnow()
        with Session(sync_db) as session:
            snap = Snapshot(
                created_at=now, policy_hash="e" * 64, horizon_at=now, item_count=2, degraded=False
            )
            session.add(snap)
            session.flush()
            session.add_all(
                [
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:50",
                        title="Example India",
                        collections_json='["Alpha Trilogy", "Best Of", "Zeta Anthology"]',
                    ),
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:51",
                        title="Example Juliet",
                        # Malformed on purpose: the collection filter must skip it,
                        # never 500, the same defense the genre filter already has.
                        collections_json="not json",
                    ),
                ]
            )
            session.commit()
        with TestClient(create_app(settings)) as c:
            login(c, settings)
            yield c

    def test_a_title_in_three_collections_is_returned_under_all_three(
        self, client: TestClient
    ) -> None:
        for name in ("Alpha Trilogy", "Best Of", "Zeta Anthology"):
            rows = client.get(
                "/api/candidates", params={"verdict": "condemn", "collection": name}
            ).json()["items"]
            assert _titles(rows) == {"Example India"}, name

    def test_a_malformed_collection_row_is_skipped_not_an_error(self, client: TestClient) -> None:
        response = client.get(
            "/api/candidates", params={"verdict": "condemn", "collection": "Alpha Trilogy"}
        )
        assert response.status_code == 200
        assert _titles(response.json()["items"]) == {"Example India"}

    def test_an_unseen_collection_matches_nothing(self, client: TestClient) -> None:
        page = client.get(
            "/api/candidates", params={"verdict": "condemn", "collection": "Nonexistent"}
        ).json()
        assert page["items"] == []
        assert page["total"] == 0

    def test_vocabulary_values_lists_the_collection_names(self, client: TestClient) -> None:
        # The same fixture, through /api/vocabulary/values: proves _VALUE_COLUMNS' new
        # "collection" entry, keyed off the column, decodes the JSON array and skips the
        # malformed row rather than raising.
        body = client.get("/api/vocabulary/values", params={"field": "collection"}).json()
        assert body["field"] == "collection"
        assert set(body["values"]) == {"Alpha Trilogy", "Best Of", "Zeta Anthology"}


class TestCollectionSizesOnTheSnapshot:
    """``collection_sizes_json`` rides the snapshot route as ``collection_sizes``. The
    collection screen's header reads it for Plex's own member count."""

    @pytest.fixture
    def client(self, settings: Settings, sync_db: Engine) -> Iterator[TestClient]:
        now = utcnow()
        with Session(sync_db) as session:
            session.add(
                Snapshot(
                    created_at=now,
                    policy_hash="f" * 64,
                    horizon_at=now,
                    item_count=0,
                    degraded=False,
                    collection_sizes_json='{"Alpha Trilogy": 8, "not-an-int": "nope"}',
                )
            )
            session.commit()
        with TestClient(create_app(settings)) as c:
            login(c, settings)
            yield c

    def test_the_map_rides_the_snapshot_route(self, client: TestClient) -> None:
        body = client.get("/api/snapshots/latest").json()
        # The non-int value is dropped rather than guessed at. It was never a size Plex
        # reported, so a wrong number is worse than a missing one.
        assert body["collection_sizes"] == {"Alpha Trilogy": 8}


class TestSearchReachesCollectionNames:
    """``search`` gains collection names, matched partially. Typing a franchise finds its
    members. A row lands in one of three blocks (0 exact title, 1 partial title/show, 2
    collection-name), carried on the response as ``search_rank``, and the three blocks
    always sort ahead of each other regardless of the operator's chosen ``sort``. Only
    the order *within* a block follows it. A block-2 row's ``matched_collection`` is the
    collection that actually matched, never the chip's usual smallest-first pick."""

    @pytest.fixture
    def client(self, settings: Settings, sync_db: Engine) -> Iterator[TestClient]:
        now = utcnow()
        with Session(sync_db) as session:
            snap = Snapshot(
                created_at=now, policy_hash="9" * 64, horizon_at=now, item_count=5, degraded=False
            )
            session.add(snap)
            session.flush()
            session.add_all(
                [
                    # Block 0: the term typed exactly.
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:70",
                        title="Nova",
                        year=2020,
                        score=50,
                        size_bytes=500,
                    ),
                    # Block 1: the term inside the title, two rows so within-block order
                    # can be told apart from the fixed block order.
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:71",
                        title="Nova Prime",
                        year=1999,
                        score=90,
                        size_bytes=300,
                    ),
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:72",
                        title="Nova Zulu",
                        year=2010,
                        score=10,
                        size_bytes=700,
                    ),
                    # Block 2: the title itself never mentions the term, only a
                    # collection does. This is search "reaching" a collection name at
                    # all.
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:73",
                        title="Reel Delta",
                        year=2001,
                        score=99,
                        size_bytes=999,
                        collections_json='["Nova Collection"]',
                    ),
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:74",
                        title="Reel Alpha",
                        year=2030,
                        score=1,
                        size_bytes=1,
                        # "Something Else" sorts first (the chip's usual element-0 pick);
                        # only "Nova Vault" matches the search, and that is the one the
                        # row must report.
                        collections_json='["Something Else", "Nova Vault"]',
                    ),
                ]
            )
            session.commit()
        with TestClient(create_app(settings)) as c:
            login(c, settings)
            yield c

    def test_a_collection_only_match_is_found_at_all(self, client: TestClient) -> None:
        # Neither "Reel Delta" nor "Reel Alpha" mentions "nova" in its own title. Only
        # their collections do. Search reaching collection names is what puts them here.
        rows = client.get("/api/candidates?verdict=condemn&search=nova").json()["items"]
        assert _titles(rows) == {"Nova", "Nova Prime", "Nova Zulu", "Reel Delta", "Reel Alpha"}

    def test_a_multi_collection_row_reports_the_match_not_the_smallest(
        self, client: TestClient
    ) -> None:
        rows = client.get("/api/candidates?verdict=condemn&search=nova").json()["items"]
        reel_alpha = next(r for r in rows if r["title"] == "Reel Alpha")
        assert reel_alpha["search_rank"] == 2
        assert reel_alpha["matched_collection"] == "Nova Vault"

    def test_a_title_match_carries_no_matched_collection(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&search=nova").json()["items"]
        nova = next(r for r in rows if r["title"] == "Nova")
        assert nova["search_rank"] == 0
        assert nova["matched_collection"] is None

    @pytest.mark.parametrize(
        ("sort", "order"),
        [("score", "desc"), ("score", "asc"), ("size", "desc"), ("year", "asc"), ("title", "asc")],
    )
    def test_the_three_blocks_stay_in_order_under_every_sort_key(
        self, client: TestClient, sort: str, order: str
    ) -> None:
        rows = client.get(
            "/api/candidates",
            params={"verdict": "condemn", "search": "nova", "sort": sort, "order": order},
        ).json()["items"]
        ranks = [int(r["search_rank"]) for r in rows]
        # Non-decreasing: every block-0 row precedes every block-1 row precedes every
        # block-2 row, whatever the operator asked the *rest* of the ordering to do.
        assert ranks == sorted(ranks), (sort, order, ranks)
        assert ranks == [0, 1, 1, 2, 2]

    def test_within_a_block_the_operators_own_sort_still_applies(self, client: TestClient) -> None:
        by_score = client.get(
            "/api/candidates",
            params={"verdict": "condemn", "search": "nova", "sort": "score", "order": "desc"},
        ).json()["items"]
        by_title = client.get(
            "/api/candidates",
            params={"verdict": "condemn", "search": "nova", "sort": "title", "order": "asc"},
        ).json()["items"]
        # Block 1 (indices 1-2): score-desc puts "Nova Prime" (90) before "Nova Zulu"
        # (10), and title-asc puts them in the same order ("Prime" < "Zulu"), so this
        # pair alone cannot tell the two sorts apart.
        assert [r["title"] for r in by_score[1:3]] == ["Nova Prime", "Nova Zulu"]
        assert [r["title"] for r in by_title[1:3]] == ["Nova Prime", "Nova Zulu"]
        # Block 2 (indices 3-4) is where the two sorts disagree: score-desc wants "Reel
        # Delta" (99) before "Reel Alpha" (1), and title-asc wants "Reel Alpha" before
        # "Reel Delta". The flip proves the block is sorted by the operator's *own* key,
        # not by some fixed order the server picked for it.
        assert [r["title"] for r in by_score[3:5]] == ["Reel Delta", "Reel Alpha"]
        assert [r["title"] for r in by_title[3:5]] == ["Reel Alpha", "Reel Delta"]


class TestSearchMatchedCollectionOnlyAppliesToBlockTwo:
    """A title match's own collections can independently contain the search term too.
    That must not leak a ``matched_collection`` onto a row that did not need one to be
    found. Only a block-2 row (search_rank == 2) carries it."""

    @pytest.fixture
    def client(self, settings: Settings, sync_db: Engine) -> Iterator[TestClient]:
        now = utcnow()
        with Session(sync_db) as session:
            snap = Snapshot(
                created_at=now, policy_hash="8" * 64, horizon_at=now, item_count=1, degraded=False
            )
            session.add(snap)
            session.flush()
            session.add(
                _candidate(
                    snapshot_id=snap.id,
                    media_key="radarr:1:80",
                    title="Nova",
                    # The row's own collection also matches "nova". This is exactly the
                    # case a naive "did any collection match" read would misreport.
                    collections_json='["Nova Boxset"]',
                )
            )
            session.commit()
        with TestClient(create_app(settings)) as c:
            login(c, settings)
            yield c

    def test_a_title_matched_row_never_carries_a_matched_collection(
        self, client: TestClient
    ) -> None:
        rows = client.get("/api/candidates?verdict=condemn&search=nova").json()["items"]
        nova = next(r for r in rows if r["title"] == "Nova")
        assert nova["search_rank"] == 0
        assert nova["matched_collection"] is None


class TestVerdictAny:
    """``verdict=any`` is every stored lane at once, unfiltered. This is what the
    collection screen needs so a title's siblings show up whatever fate each one got. No
    hand-override lane-shift step runs for it. Nothing is excluded from one named lane,
    so there is nothing to move in or out of."""

    @pytest.fixture
    def client(self, settings: Settings, sync_db: Engine) -> Iterator[TestClient]:
        now = utcnow()
        with Session(sync_db) as session:
            snap = Snapshot(
                created_at=now, policy_hash="f" * 64, horizon_at=now, item_count=3, degraded=False
            )
            session.add(snap)
            session.flush()
            session.add_all(
                [
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:60",
                        title="Example Foxtrot",
                        verdict="condemn",
                    ),
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:61",
                        title="Example Golf",
                        verdict="protect",
                    ),
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:62",
                        title="Example Hotel",
                        verdict="abstain",
                    ),
                ]
            )
            session.commit()
        with TestClient(create_app(settings)) as c:
            login(c, settings)
            yield c

    def test_any_mixes_every_stored_fate(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=any").json()["items"]
        assert _titles(rows) == {"Example Foxtrot", "Example Golf", "Example Hotel"}

    def test_a_named_lane_still_narrows_to_just_that_lane(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn").json()["items"]
        assert _titles(rows) == {"Example Foxtrot"}


class TestLibraryFilter:
    def test_the_library_rides_along_on_every_row(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn").json()["items"]
        by_title = {str(r["title"]): r["library"] for r in rows}
        assert by_title["Example Alpha"] == "Movies"
        assert by_title["Example Mid · Season 5"] == "TV Shows"

    def test_library_keeps_only_that_section(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&library=Movies").json()["items"]
        assert _titles(rows) == {"Example Alpha"}

    def test_library_matches_the_whole_name_not_a_substring(self, client: TestClient) -> None:
        # A filter for "Movies" must not drag in the "4K Movies" library. Example Zulu (the
        # "4K Movies" title) is spared by hand, so query the Kept lane it now rides.
        rows = client.get("/api/candidates", params={"verdict": "protect", "library": "4K Movies"})
        assert _titles(rows.json()["items"]) == {"Example Zulu"}

    def test_an_unseen_library_matches_nothing(self, client: TestClient) -> None:
        page = client.get("/api/candidates?verdict=condemn&library=Anime").json()
        assert page["items"] == []
        assert page["total"] == 0

    def test_it_stacks_with_media_type(self, client: TestClient) -> None:
        rows = client.get(
            "/api/candidates",
            params={"verdict": "condemn", "library": "TV Shows", "media_type": "season"},
        )
        assert _titles(rows.json()["items"]) == {"Example Mid · Season 5"}


class TestOverrideFilter:
    def test_spared_by_hand(self, client: TestClient) -> None:
        # A hand spare moves the item onto the Kept lane (its stored verdict stays pure
        # policy). The spare filter finds it there, not on the Condemned lane it left.
        page = client.get("/api/candidates?verdict=protect&override=spare").json()
        assert _titles(page["items"]) == {"Example Zulu"}
        # The totals describe the filtered set, exactly what the page is drawn from.
        assert page["total"] == 1

    def test_a_show_level_reap_covers_its_season(self, client: TestClient) -> None:
        # The override sits on the *show* key (sonarr:1:5). The season row inherits it.
        rows = client.get("/api/candidates?verdict=condemn&override=reap").json()["items"]
        assert _titles(rows) == {"Example Mid · Season 5"}

    def test_untouched_items_only(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&override=none").json()["items"]
        assert _titles(rows) == {"Example Alpha"}

    def test_it_stacks_with_the_other_filters(self, client: TestClient) -> None:
        # A spare override AND a requester: Zulu is spared but was never requested.
        rows = client.get("/api/candidates?verdict=condemn&override=spare&requested=yes").json()[
            "items"
        ]
        assert rows == []

    def test_a_season_spared_on_its_own_key_beats_its_shows_reap(self, client: TestClient) -> None:
        """The precedence still lives in one function, not in SQL.

        The filter narrows in SQL to the rows that could possibly carry a decision, and
        asks the real ``effective_override`` about each, rather than resolving every row
        in the lane in Python. This is the case that would break if the narrowing ever
        tried to decide as well. The season's own key must win over its show's.
        """
        assert (
            client.post(
                "/api/override",
                json={"media_key": "sonarr:1:5:5", "title": "Season 5", "decision": "spare"},
            ).status_code
            < 300
        )
        spared = client.get("/api/candidates?verdict=protect&override=spare").json()["items"]
        assert "Example Mid · Season 5" in _titles(spared)
        # And it is no longer counted under its show's reap.
        reaped = client.get("/api/candidates?verdict=condemn&override=reap").json()["items"]
        assert _titles(reaped) == set()

    def test_a_row_reports_both_the_spare_it_toggles_and_the_one_that_covers_it(
        self, client: TestClient
    ) -> None:
        """Two spares, two questions, two fields. The wire must carry both.

        The season is spared for a day and its show forever. ``spare_expires_at`` is the
        spare in force by precedence, which is what the row's Spare control toggles and
        clears, so it must stay the season's own date. ``spare_covers_until`` is when the
        file stops being kept, which is what every color and every sentence about its
        fate reads, and the show's forever spare outlasts the season's day. It must be
        null.

        Reading one field for both jobs is what drew "expired" over a file the show
        keeps and promised a re-judgment that changes nothing. This is pinned at the
        route because the derivation being right (``test_whitelist``) does not mean it
        is plumbed.
        """
        for key, title, days in (
            ("sonarr:1:5", "Example Mid", 0),  # the whole show, forever
            ("sonarr:1:5:5", "Season 5", 1),  # the season, for a day
        ):
            assert (
                client.post(
                    "/api/override",
                    json={
                        "media_key": key,
                        "title": title,
                        "decision": "spare",
                        "spare_days": days,
                    },
                ).status_code
                < 300
            )

        rows = client.get("/api/candidates?verdict=protect&override=spare").json()["items"]
        season = next(r for r in rows if r["media_key"] == "sonarr:1:5:5")
        assert season["spare_expires_at"] is not None, "the control still toggles the season's own"
        assert season["spare_covers_until"] is None, "the show's forever spare outlasts it"

    def test_a_season_covers_itself_when_no_show_spare_outlasts_it(
        self, client: TestClient
    ) -> None:
        """The other side of the same branch, and the one that keeps the fix honest.

        The show above this season is set to *reap*, so it contributes no cover. When
        the season's own spare runs out the file really is handed back. Both fields must
        answer with the season's own date, or every expired season spare would read as
        covered.
        """
        assert (
            client.post(
                "/api/override",
                json={
                    "media_key": "sonarr:1:5:5",
                    "title": "Season 5",
                    "decision": "spare",
                    "spare_days": 1,
                },
            ).status_code
            < 300
        )
        rows = client.get("/api/candidates?verdict=protect&override=spare").json()["items"]
        season = next(r for r in rows if r["media_key"] == "sonarr:1:5:5")
        assert season["spare_covers_until"] == season["spare_expires_at"] is not None

    def test_untouched_means_neither_its_own_key_nor_its_shows(self, client: TestClient) -> None:
        """``override=none`` is the anti-set of every effective decision.

        It used to be built by pulling every media_key in the lane into Python and binding
        them all back into one IN, which on a large library was nearly every row and could
        run past SQLite's bound-variable ceiling into a 500. It is derived from the
        operator's decisions now, so what must not change is which rows it names.
        """
        rows = client.get("/api/candidates?verdict=condemn&override=none").json()["items"]
        # Alpha has no decision at any level. The season is covered by its *show*'s
        # reap and Zulu by its own spare, so neither is untouched.
        assert _titles(rows) == {"Example Alpha"}


class TestTheShowKeyInvariantTheFilterRelieson:
    """``Candidate.group_key`` and ``whitelist.show_key`` must name the same thing.

    The override filter narrows in SQL with ``group_key IN (decided keys)`` before asking
    the real decision function about each row. That narrowing is only a superset if a
    season's stored ``group_key`` is exactly the key ``show_key`` derives from its
    media_key. If the two ever drifted, a season kept by a whole-show spare would fall
    out of the narrowed set and be reported as untouched. The season scan builds
    ``group_key`` as ``sonarr:{instance}:{series}``. This pins that they agree.
    """

    def test_a_seasons_group_key_is_its_show_key(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=protect&media_type=season").json()["items"]
        rows += client.get("/api/candidates?verdict=condemn&media_type=season").json()["items"]
        assert rows, "the fixture must hold at least one season for this to mean anything"
        for row in rows:
            assert row["group_key"] == show_key(str(row["media_key"]))

    def test_a_movie_has_neither(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&media_type=movie").json()["items"]
        assert rows
        for row in rows:
            assert row["group_key"] is None
            assert show_key(str(row["media_key"])) is None


class TestSort:
    def test_by_title_ascending_uses_the_show_name(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&sort=title&order=asc").json()["items"]
        # The season sorts under its show name ("Example Mid"), not its own full season title.
        # Example Zulu is spared by hand, so it rides the Kept lane and is not among these.
        assert [r["title"] for r in rows] == [
            "Example Alpha",
            "Example Mid · Season 5",
        ]

    def test_by_year_descending_is_newest_first(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&sort=year&order=desc").json()["items"]
        # Newest first. The season carries no year and sorts last. Example Zulu (1995)
        # is spared by hand and rides the Kept lane, so Example Alpha now leads the
        # condemned lane.
        assert [r["title"] for r in rows] == ["Example Alpha", "Example Mid · Season 5"]


class TestYearInSearch:
    """A release year typed after a title narrows the search instead of matching nothing.

    The queue prints the year in its own span beside the title, and Scales prints it the same
    way, so the operator reads "Example Delta 2049" as one string and types it back. The year
    lives in its own column and was never inside ``title``, so every such search used to come
    back empty.

    Two rows the fixture below is built to tell apart, because a number on the end of a
    title has two readings and the predicate has to try both:

    * **Example Delta 2049**, released 2017. The number is part of the *name*. Only the
      whole-string arm finds it. The stem-plus-year arm asks for year 2049 and it is 2017.
    * **Example Echo**, released 2049. The number is the *year*. Only the stem-plus-year
      arm finds it. The whole string is not in its title.

    Either arm alone leaves one of them unfindable by the exact string the UI prints.
    """

    @pytest.fixture
    def client(self, settings: Settings, sync_db: Engine) -> Iterator[TestClient]:
        now = utcnow()
        with Session(sync_db) as session:
            snap = Snapshot(
                created_at=now, policy_hash="b" * 64, horizon_at=now, item_count=2, degraded=False
            )
            session.add(snap)
            session.flush()
            session.add_all(
                [
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:20",
                        title="Example Delta 2049",
                        year=2017,
                    ),
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:21",
                        title="Example Echo",
                        year=2049,
                    ),
                ]
            )
            session.commit()
        with TestClient(create_app(settings)) as c:
            login(c, settings)
            yield c

    def _found(self, client: TestClient, term: str) -> set[str]:
        return _titles(client.get("/api/candidates", params={"search": term}).json()["items"])

    def test_a_title_ending_in_a_year_it_predates_is_findable_by_its_whole_name(
        self, client: TestClient
    ) -> None:
        assert self._found(client, "Example Delta 2049") == {"Example Delta 2049"}

    def test_a_year_typed_after_a_title_narrows_to_that_year(self, client: TestClient) -> None:
        assert self._found(client, "Example Echo 2049") == {"Example Echo"}
        # Parentheses and a bare space are the two ways the year is printed across the app.
        assert self._found(client, "Example Echo (2049)") == {"Example Echo"}
        # And it narrows: the same title with the wrong year is not a near miss, it is a miss.
        assert self._found(client, "Example Echo 2017") == set()

    def test_the_year_can_come_from_either_column(self, client: TestClient) -> None:
        # "Example Delta" is 2017, so the stem-plus-year arm finds it by its real year even
        # though its title ends in a different one.
        assert self._found(client, "Example Delta 2017") == {"Example Delta 2049"}

    def test_a_year_alone_asks_for_that_year_and_still_reads_as_text(
        self, client: TestClient
    ) -> None:
        # Both readings again: Echo came out in 2049 and only the year arm finds it.
        # Delta has 2049 in its *name* and was released in 2017, so only the text arm
        # does. Answering with one half read as a broken search when it matched titles
        # only, and would lose a title named after a year if it matched the column only.
        assert self._found(client, "2049") == {"Example Delta 2049", "Example Echo"}

    def test_a_year_alone_narrows_to_that_year(self, client: TestClient) -> None:
        # Not "every row": 2017 is Delta's release year and nothing else here, so Echo stays out.
        assert self._found(client, "2017") == {"Example Delta 2049"}


class TestSearchIsLiteralText:
    """What the operator types means itself, not a SQL pattern.

    ``%`` and ``_`` are the two characters ``LIKE`` reserves. The term went into the
    pattern unescaped, so both were live wildcards: ``a_pha`` matched "Example Alpha".
    It only ever over-matched. A title genuinely containing ``%`` still found itself,
    because the wildcard matches the empty string, which is why it went unnoticed.

    The fixture holds the pairs that tell a literal from a wildcard: two titles one underscore
    apart, and one containing a real ``%`` and a real backslash.
    """

    @pytest.fixture
    def client(self, settings: Settings, sync_db: Engine) -> Iterator[TestClient]:
        now = utcnow()
        with Session(sync_db) as session:
            snap = Snapshot(
                created_at=now, policy_hash="c" * 64, horizon_at=now, item_count=3, degraded=False
            )
            session.add(snap)
            session.flush()
            session.add_all(
                [
                    _candidate(snapshot_id=snap.id, media_key="radarr:1:30", title="Example Alpha"),
                    _candidate(
                        snapshot_id=snap.id, media_key="radarr:1:31", title="Example Alpaca"
                    ),
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:32",
                        title="Example 100% Complete",
                    ),
                    _candidate(
                        snapshot_id=snap.id, media_key="radarr:1:33", title="Example A\\B Sequel"
                    ),
                ]
            )
            session.commit()
        with TestClient(create_app(settings)) as c:
            login(c, settings)
            yield c

    def _found(self, client: TestClient, term: str) -> set[str]:
        return _titles(client.get("/api/candidates", params={"search": term}).json()["items"])

    def test_an_underscore_matches_only_an_underscore(self, client: TestClient) -> None:
        # As a wildcard this found "Example Alpha". As a literal it finds nothing, because no
        # title here contains one.
        assert self._found(client, "a_pha") == set()
        # The control: the same search with the real character finds the row.
        assert self._found(client, "alpha") == {"Example Alpha"}

    def test_a_percent_matches_only_a_percent(self, client: TestClient) -> None:
        # As a wildcard this spanned the gap and matched "Example 100% Complete".
        assert self._found(client, "Example%Complete") == set()

    def test_a_title_containing_a_percent_is_found_by_typing_it(self, client: TestClient) -> None:
        # The direction the old behavior got right by accident, and the one an escape
        # can break: this also pins that the backslash is escaped *before* the `%`.
        # Escaping them the other way round turns the escape into a literal backslash
        # plus a live wildcard, and this row stops matching its own name.
        assert self._found(client, "100% Complete") == {"Example 100% Complete"}

    def test_a_backslash_means_a_backslash(self, client: TestClient) -> None:
        # The escape character itself has to survive being searched for.
        assert self._found(client, "A\\B") == {"Example A\\B Sequel"}

    def test_a_lone_wildcard_is_not_a_way_to_list_everything(self, client: TestClient) -> None:
        # A bare "%" used to be every row in the lane. It is now a search for one character,
        # and exactly one title here has it.
        assert self._found(client, "%") == {"Example 100% Complete"}
        assert self._found(client, "_") == set()


class TestSplitSearchYear:
    """The trailing-year split stays linear in the term, and keeps its readings.

    The separator run ahead of the year used to live in the pattern as ``[\\s,·-]*``,
    which backtracks quadratically when the term is mostly whitespace, a string an
    operator can paste into search. The run is stripped in code now. The flood case
    pins the complexity. Quadratic needs minutes on that input where linear needs
    microseconds, so the bound sits a thousandfold above one and far under the other.
    """

    def test_the_readings_survive_the_rewrite(self) -> None:
        # The spellings the app itself prints beside a title, and the bare-adjacent form.
        assert _split_search_year("Example Delta 2049") == ("Example Delta", 2049)
        assert _split_search_year("Example Delta (2049)") == ("Example Delta", 2049)
        assert _split_search_year("Example Delta, 2049") == ("Example Delta", 2049)
        assert _split_search_year("2049") == ("", 2049)
        assert _split_search_year("(2049)") == ("", 2049)
        assert _split_search_year("Example Delta") == ("Example Delta", None)

    def test_a_whitespace_flood_returns_at_once(self) -> None:
        flood = "\t" * 100_000
        started = time.perf_counter()
        assert _split_search_year(flood) == (flood, None)
        assert time.perf_counter() - started < 1.0
