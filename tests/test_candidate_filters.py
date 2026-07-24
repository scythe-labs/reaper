# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review queue's filters: search, media type, and requested-only.

Filters narrow the frozen snapshot, they never re-decide it. Each one is a display-side
convenience over candidates that were already judged, and the queue also carries the
display fields (poster, blurb, requested-by, show grouping) captured at scan time.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Candidate, Snapshot, WhitelistEntry
from reaper.main import create_app

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
    return Candidate(**base)  # type: ignore[arg-type]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)

    now = utcnow()
    with Session(engine) as session:
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
                    # substrings; a filter for Comedy must not drag this row along.
                    genres_json='["Comedy Special"]',
                    # A distinct library whose name contains "Movies": the library filter must
                    # match the whole name, not drag this in on a filter for "Movies".
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
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


def _titles(rows: list[dict[str, object]]) -> set[str]:
    return {str(r["title"]) for r in rows}


class TestFilters:
    def test_unfiltered_returns_every_condemned_item(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn").json()
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
        rows = client.get("/api/candidates?verdict=condemn&search=alpha").json()
        assert _titles(rows) == {"Example Alpha"}

    def test_search_also_matches_the_show_name(self, client: TestClient) -> None:
        # "mid" matches the show name, carried on the season row's group_title.
        rows = client.get("/api/candidates?verdict=condemn&search=mid").json()
        assert _titles(rows) == {"Example Mid · Season 5"}

    def test_media_type_filter(self, client: TestClient) -> None:
        movies = client.get("/api/candidates?verdict=condemn&media_type=movie").json()
        # Example Zulu is a movie too, but spared by hand, so it rides the Kept lane now.
        assert _titles(movies) == {"Example Alpha"}
        kept_movies = client.get("/api/candidates?verdict=protect&media_type=movie").json()
        assert _titles(kept_movies) == {"Example Zulu"}
        seasons = client.get("/api/candidates?verdict=condemn&media_type=season").json()
        assert _titles(seasons) == {"Example Mid · Season 5"}

    def test_requested_yes_keeps_just_requested_media(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&requested=yes").json()
        assert _titles(rows) == {
            "Example Alpha",
            "Example Mid · Season 5",
        }  # Example Zulu had no requester

    def test_requested_no_keeps_just_the_unrequested(self, client: TestClient) -> None:
        # The only unrequested title, Example Zulu, is spared by hand, so it rides the Kept lane.
        rows = client.get("/api/candidates?verdict=protect&requested=no").json()
        assert _titles(rows) == {"Example Zulu"}  # the only one nobody asked for

    def test_filters_stack(self, client: TestClient) -> None:
        # media_type AND requested are ANDed, not either-or.
        rows = client.get("/api/candidates?verdict=condemn&media_type=movie&requested=yes").json()
        assert _titles(rows) == {"Example Alpha"}  # a movie AND requested; the season is excluded


class TestGenreFilter:
    def test_a_genre_matches_the_whole_term_only(self, client: TestClient) -> None:
        # "Comedy" must match ["Comedy", ...] and NOT ["Comedy Special"].
        rows = client.get("/api/candidates?verdict=condemn&genre=Comedy").json()
        assert _titles(rows) == {"Example Alpha"}

    def test_a_malformed_genre_row_is_skipped_not_an_error(self, client: TestClient) -> None:
        # The season row's genres_json does not parse; it never matches and never 500s.
        response = client.get("/api/candidates?verdict=condemn&genre=Horror")
        assert response.status_code == 200
        assert _titles(response.json()) == {"Example Alpha"}

    def test_an_unseen_genre_matches_nothing(self, client: TestClient) -> None:
        response = client.get("/api/candidates?verdict=condemn&genre=Western")
        assert response.json() == []
        assert response.headers["X-Total-Count"] == "0"


class TestLibraryFilter:
    def test_the_library_rides_along_on_every_row(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn").json()
        by_title = {str(r["title"]): r["library"] for r in rows}
        assert by_title["Example Alpha"] == "Movies"
        assert by_title["Example Mid · Season 5"] == "TV Shows"

    def test_library_keeps_only_that_section(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&library=Movies").json()
        assert _titles(rows) == {"Example Alpha"}

    def test_library_matches_the_whole_name_not_a_substring(self, client: TestClient) -> None:
        # A filter for "Movies" must not drag in the "4K Movies" library. Example Zulu (the
        # "4K Movies" title) is spared by hand, so query the Kept lane it now rides.
        rows = client.get("/api/candidates", params={"verdict": "protect", "library": "4K Movies"})
        assert _titles(rows.json()) == {"Example Zulu"}

    def test_an_unseen_library_matches_nothing(self, client: TestClient) -> None:
        response = client.get("/api/candidates?verdict=condemn&library=Anime")
        assert response.json() == []
        assert response.headers["X-Total-Count"] == "0"

    def test_it_stacks_with_media_type(self, client: TestClient) -> None:
        rows = client.get(
            "/api/candidates",
            params={"verdict": "condemn", "library": "TV Shows", "media_type": "season"},
        )
        assert _titles(rows.json()) == {"Example Mid · Season 5"}


class TestOverrideFilter:
    def test_spared_by_hand(self, client: TestClient) -> None:
        # A hand spare moves the item onto the Kept lane (its stored verdict stays pure policy);
        # the spare filter finds it there, not on the Condemned lane it left.
        response = client.get("/api/candidates?verdict=protect&override=spare")
        assert _titles(response.json()) == {"Example Zulu"}
        # The totals describe the filtered set, exactly what the page is drawn from.
        assert response.headers["X-Total-Count"] == "1"

    def test_a_show_level_reap_covers_its_season(self, client: TestClient) -> None:
        # The override sits on the SHOW key (sonarr:1:5); the season row inherits it.
        rows = client.get("/api/candidates?verdict=condemn&override=reap").json()
        assert _titles(rows) == {"Example Mid · Season 5"}

    def test_untouched_items_only(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&override=none").json()
        assert _titles(rows) == {"Example Alpha"}

    def test_it_stacks_with_the_other_filters(self, client: TestClient) -> None:
        # A spare override AND a requester: Zulu is spared but was never requested.
        rows = client.get("/api/candidates?verdict=condemn&override=spare&requested=yes").json()
        assert rows == []


class TestSort:
    def test_by_title_ascending_uses_the_show_name(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&sort=title&order=asc").json()
        # The season sorts under its show name ("Example Mid"), not its own full season title.
        # Example Zulu is spared by hand, so it rides the Kept lane and is not among these.
        assert [r["title"] for r in rows] == [
            "Example Alpha",
            "Example Mid · Season 5",
        ]

    def test_by_year_descending_is_newest_first(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&sort=year&order=desc").json()
        # Newest first; the season carries no year and sorts last. Example Zulu (1995) is spared by
        # hand and rides the Kept lane, so Example Alpha now leads the condemned lane.
        assert [r["title"] for r in rows] == ["Example Alpha", "Example Mid · Season 5"]
