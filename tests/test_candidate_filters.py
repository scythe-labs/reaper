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
from reaper.db.models import Candidate, Snapshot
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
                ),
                _candidate(
                    snapshot_id=snap.id,
                    media_key="radarr:1:11",
                    title="Example Zulu",
                    year=1995,
                    requested_by=None,
                ),
                _candidate(
                    snapshot_id=snap.id,
                    media_key="sonarr:1:5:5",
                    title="Example Mid · Season 5",
                    media_type="season",
                    group_key="sonarr:1:5",
                    group_title="Example Mid",
                    requested_by="Bob",
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
        assert len(rows) == 3
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
        assert _titles(movies) == {"Example Alpha", "Example Zulu"}
        seasons = client.get("/api/candidates?verdict=condemn&media_type=season").json()
        assert _titles(seasons) == {"Example Mid · Season 5"}

    def test_requested_yes_keeps_just_requested_media(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&requested=yes").json()
        assert _titles(rows) == {
            "Example Alpha",
            "Example Mid · Season 5",
        }  # Example Zulu had no requester

    def test_requested_no_keeps_just_the_unrequested(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&requested=no").json()
        assert _titles(rows) == {"Example Zulu"}  # the only one nobody asked for

    def test_filters_stack(self, client: TestClient) -> None:
        # media_type AND requested are ANDed, not either-or.
        rows = client.get("/api/candidates?verdict=condemn&media_type=movie&requested=yes").json()
        assert _titles(rows) == {"Example Alpha"}  # a movie AND requested; the season is excluded


class TestSort:
    def test_by_title_ascending_uses_the_show_name(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&sort=title&order=asc").json()
        # The season sorts under its show name ("Example Mid"), not its own full season title.
        assert [r["title"] for r in rows] == [
            "Example Alpha",
            "Example Mid · Season 5",
            "Example Zulu",
        ]

    def test_by_year_descending_is_newest_first(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&sort=year&order=desc").json()
        # Newest first; the season carries no year and sorts last.
        assert [r["title"] for r in rows][:2] == ["Example Zulu", "Example Alpha"]
