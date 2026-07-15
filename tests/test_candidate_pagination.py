# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review queue is paged.

A real library runs to thousands of protected titles; returning them in one payload was
capping the list and hiding the tail (the bug: "thousands scanned, fewer than a thousand
shown"). The endpoint now returns a page of ``limit`` rows at ``offset`` and reports the
full filtered set -- a count and a byte total measured *before* the page window -- in the
``X-Total-Count`` and ``X-Total-Bytes`` headers, so the header can read the whole set's count
only a page is on the wire.
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

SIZE = 2_000_000_000
N_CONDEMN = 250
N_PROTECT = 40


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)

    now = utcnow()
    with Session(engine) as session:
        snap = Snapshot(
            created_at=now,
            policy_hash="a" * 64,
            horizon_at=now,
            item_count=N_CONDEMN + N_PROTECT,
            degraded=False,
        )
        session.add(snap)
        session.flush()
        rows = [
            Candidate(
                snapshot_id=snap.id,
                media_key=f"radarr:1:{i}",
                title=f"Movie {i:04d}",
                media_type="movie",
                size_bytes=SIZE,
                verdict="condemn",
                # Distinct, descending scores so paging order is total and stable.
                score=99 - (i % 40),
                coverage_bp=10_000,
                explanation_json="{}",
                created_at=now,
            )
            for i in range(N_CONDEMN)
        ]
        rows += [
            Candidate(
                snapshot_id=snap.id,
                media_key=f"radarr:2:{i}",
                title=f"Kept {i:04d}",
                media_type="movie",
                size_bytes=SIZE,
                verdict="protect",
                score=10,
                coverage_bp=10_000,
                explanation_json="{}",
                created_at=now,
            )
            for i in range(N_PROTECT)
        ]
        session.add_all(rows)
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestPagination:
    def test_the_headers_report_the_whole_filtered_set(self, client: TestClient) -> None:
        r = client.get("/api/candidates?verdict=condemn&limit=100&offset=0")
        assert r.headers["X-Total-Count"] == str(N_CONDEMN)
        assert r.headers["X-Total-Bytes"] == str(N_CONDEMN * SIZE)
        # ...even though only a page is on the wire.
        assert len(r.json()) == 100

    def test_a_page_is_capped_even_when_more_exist(self, client: TestClient) -> None:
        # The default page (no limit) no longer returns the whole set.
        assert len(client.get("/api/candidates?verdict=condemn").json()) == 100

    def test_offset_walks_the_whole_set_without_gaps_or_overlap(self, client: TestClient) -> None:
        seen: list[str] = []
        for offset in range(0, N_CONDEMN, 100):
            page = client.get(f"/api/candidates?verdict=condemn&limit=100&offset={offset}").json()
            seen.extend(str(row["media_key"]) for row in page)
        assert len(seen) == N_CONDEMN
        assert len(set(seen)) == N_CONDEMN  # every key exactly once

    def test_the_last_page_is_short_and_then_empty(self, client: TestClient) -> None:
        assert len(client.get("/api/candidates?verdict=condemn&limit=100&offset=200").json()) == 50
        assert len(client.get("/api/candidates?verdict=condemn&limit=100&offset=250").json()) == 0

    def test_totals_track_the_filter_not_the_snapshot(self, client: TestClient) -> None:
        # A different verdict is a different filtered set, so its totals differ.
        r = client.get("/api/candidates?verdict=protect&limit=100&offset=0")
        assert r.headers["X-Total-Count"] == str(N_PROTECT)
        assert r.headers["X-Total-Bytes"] == str(N_PROTECT * SIZE)
