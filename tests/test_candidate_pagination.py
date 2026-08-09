# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review queue is paged.

A real library runs to thousands of protected titles; returning them in one payload was
capping the list and hiding the tail (the bug: "thousands scanned, fewer than a thousand
shown"). The endpoint now returns a page of ``limit`` rows at ``offset`` and reports the
full filtered set -- a count and a byte total measured *before* the page window -- in the
envelope's ``total`` and ``total_bytes``, so the header can read the whole set's count while
only a page is on the wire.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

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
#: How many of the condemned rows Reaper could not measure. Non-zero on purpose: a byte SUM
#: skips a NULL without saying so, and a fixture where every row has a size cannot tell the
#: unmeasured count from a hardcoded zero (rule 141). The Kept lane keeps all its sizes, so
#: the two lanes pin the count from both directions.
N_UNMEASURED = 3


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, secret_key="k")
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
                size_bytes=None if i < N_UNMEASURED else SIZE,
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
    def test_the_totals_report_the_whole_filtered_set(self, client: TestClient) -> None:
        page = client.get("/api/candidates?verdict=condemn&limit=100&offset=0").json()
        assert page["total"] == N_CONDEMN
        # The count is every row; the byte total is only the rows that have a size, and
        # `unknown_size` is what it could not include. Three separate figures, so a queue
        # header reading the sum alone cannot pass off an unmeasured library as a small one.
        assert page["total_bytes"] == (N_CONDEMN - N_UNMEASURED) * SIZE
        assert page["unknown_size"] == N_UNMEASURED
        # ...even though only a page is on the wire.
        assert len(page["items"]) == 100

    def test_a_page_is_capped_even_when_more_exist(self, client: TestClient) -> None:
        # The default page (no limit) no longer returns the whole set.
        assert len(client.get("/api/candidates?verdict=condemn").json()["items"]) == 100

    def test_offset_walks_the_whole_set_without_gaps_or_overlap(self, client: TestClient) -> None:
        seen: list[str] = []
        for offset in range(0, N_CONDEMN, 100):
            page = client.get(f"/api/candidates?verdict=condemn&limit=100&offset={offset}").json()
            # The envelope says where it starts, which is what the queue asks the next page from.
            assert page["offset"] == offset
            seen.extend(str(row["media_key"]) for row in page["items"])
        assert len(seen) == N_CONDEMN
        assert len(set(seen)) == N_CONDEMN  # every key exactly once

    def test_the_last_page_is_short_and_then_empty(self, client: TestClient) -> None:
        short = client.get("/api/candidates?verdict=condemn&limit=100&offset=200").json()
        past_the_end = client.get("/api/candidates?verdict=condemn&limit=100&offset=250").json()
        assert len(short["items"]) == 50
        assert len(past_the_end["items"]) == 0
        # A page past the end still reports the whole set, so the queue's header does not
        # blank out when the operator scrolls to the bottom.
        assert past_the_end["total"] == N_CONDEMN

    def test_totals_track_the_filter_not_the_snapshot(self, client: TestClient) -> None:
        # A different verdict is a different filtered set, so its totals differ.
        page = client.get("/api/candidates?verdict=protect&limit=100&offset=0").json()
        assert page["total"] == N_PROTECT
        assert page["total_bytes"] == N_PROTECT * SIZE
        # Every Kept row has a size, so the condemned lane's unmeasured three do not leak in.
        assert page["unknown_size"] == 0

    def test_the_page_names_the_snapshot_it_came_from(self, client: TestClient) -> None:
        # The queue compares this against the newest completed scan to tell when a fresher
        # snapshot has landed under an open review. It is the latest snapshot's id, and it
        # rides on every filtered page (the verdict does not change which snapshot is read).
        latest_id = client.get("/api/snapshots/latest").json()["id"]
        for path in (
            "/api/candidates?verdict=condemn&limit=100&offset=0",
            "/api/candidates?verdict=protect&limit=100&offset=0",
        ):
            assert client.get(path).json()["snapshot_id"] == latest_id


@pytest.fixture
def unscanned_client(tmp_path: Path) -> Iterator[TestClient]:
    """A database with the tables and no snapshot: what an operator sees before the first
    scan finishes."""
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


def test_before_the_first_scan_the_page_is_whole(unscanned_client: TestClient) -> None:
    """Every field, not the two the headers used to carry.

    The header form set ``X-Total-Count`` and ``X-Total-Bytes`` on this branch and neither
    of the other two, so the browser read a missing ``X-Unknown-Size-Count`` as zero and a
    missing ``X-Snapshot-Id`` as null by two different defaults it wrote itself. One model
    answers the whole shape or it does not answer at all.
    """
    page = unscanned_client.get("/api/candidates?verdict=condemn&offset=40").json()
    assert page == {
        "items": [],
        "groups": [],
        "total": 0,
        "total_bytes": 0,
        "unknown_size": 0,
        "offset": 40,
        "snapshot_id": None,
    }


# ---------------------------------------------------------------------------
# B-13: show cards must state what "Reap now" will plan, even across page breaks.
# ---------------------------------------------------------------------------

SEASON_SIZE = 3_000_000_000
N_SEASONS = 6


@pytest.fixture
def tv_client(tmp_path: Path) -> Iterator[TestClient]:
    """A snapshot where one show's condemned seasons straddle any small page: six
    seasons at descending scores, padded with condemned movies between them."""
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)

    now = utcnow()
    with Session(engine) as session:
        snap = Snapshot(
            created_at=now,
            policy_hash="a" * 64,
            horizon_at=now,
            item_count=N_SEASONS + 20,
            degraded=False,
        )
        session.add(snap)
        session.flush()
        rows = [
            Candidate(
                snapshot_id=snap.id,
                media_key=f"sonarr:1:42:{n}",
                title=f"Season {n}",
                media_type="season",
                size_bytes=SEASON_SIZE,
                verdict="condemn",
                # Interleave with the movies below so a small page splits the show.
                score=90 - n * 10,
                coverage_bp=10_000,
                explanation_json="{}",
                group_key="sonarr:1:42",
                group_title="A Long Show",
                created_at=now,
            )
            for n in range(1, N_SEASONS + 1)
        ]
        rows += [
            Candidate(
                snapshot_id=snap.id,
                media_key=f"radarr:1:{i}",
                title=f"Movie {i:04d}",
                media_type="movie",
                size_bytes=SIZE,
                verdict="condemn",
                score=95 - i * 4,
                coverage_bp=10_000,
                explanation_json="{}",
                created_at=now,
            )
            for i in range(20)
        ]
        session.add_all(rows)
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


def _rollup(page: dict[str, Any], group_key: str) -> dict[str, Any]:
    """The one rollup for a show, and proof there is exactly one.

    Sent once per show now, not stamped on each season row, so a duplicate would be the
    regression this shape was made to remove.
    """
    entries = [g for g in page["groups"] if g["group_key"] == group_key]
    assert len(entries) == 1, f"expected one rollup for {group_key}, got {len(entries)}"
    return cast("dict[str, Any]", entries[0])


class TestGroupCondemnedTotals:
    def test_a_page_holding_part_of_a_show_still_reports_the_whole_plan(
        self, tv_client: TestClient
    ) -> None:
        """The failure this guards: a small first page holds two of six seasons, and the
        card built from it used to say "2 seasons" while Reap now planned all six. The
        show's rollup describes the whole snapshot however little of it the page holds."""
        page = tv_client.get("/api/candidates?verdict=condemn&limit=10&offset=0").json()
        seasons = [r for r in page["items"] if r["group_key"] == "sonarr:1:42"]
        assert seasons, "the page should hold at least one of the show's seasons"
        assert len(seasons) < N_SEASONS, "the show must straddle the page for this test"

        rollup = _rollup(page, "sonarr:1:42")
        assert rollup["condemned_count"] == N_SEASONS
        assert rollup["condemned_bytes"] == N_SEASONS * SEASON_SIZE
        # Every season of the show, not the ones this page happened to fetch: the strip and a
        # whole-show Reap are both judged over it.
        assert len(rollup["seasons"]) == N_SEASONS

    def test_movies_bring_no_rollup(self, tv_client: TestClient) -> None:
        """A movie is its own card and has no show to roll up, so it contributes no entry."""
        page = tv_client.get("/api/candidates?verdict=condemn&limit=10&offset=0").json()
        movies = [r for r in page["items"] if r["media_type"] == "movie"]
        assert movies
        assert {g["group_key"] for g in page["groups"]} == {"sonarr:1:42"}

    def test_a_hand_spared_season_leaves_the_plan_totals(self, tv_client: TestClient) -> None:
        """The totals must match the planner exactly, and the planner drops hand-spares:
        sparing one season shrinks the card's numbers by exactly that season."""
        spare = tv_client.post(
            "/api/override",
            json={"media_key": f"sonarr:1:42:{N_SEASONS}", "decision": "spare"},
        )
        assert spare.status_code == 200, spare.text

        page = tv_client.get("/api/candidates?verdict=condemn&limit=10&offset=0").json()
        rollup = _rollup(page, "sonarr:1:42")
        assert rollup["condemned_count"] == N_SEASONS - 1
        assert rollup["condemned_bytes"] == (N_SEASONS - 1) * SEASON_SIZE
        # The spared season leaves the plan and stays on the strip, which is what draws it
        # as kept rather than dropping it out of the show.
        assert len(rollup["seasons"]) == N_SEASONS

    def test_sparing_the_whole_show_zeroes_the_plan(self, tv_client: TestClient) -> None:
        spare = tv_client.post(
            "/api/override", json={"media_key": "sonarr:1:42", "decision": "spare"}
        )
        assert spare.status_code == 200, spare.text

        # A whole-show spare moves its seasons onto the Kept lane (their stored verdict stays pure
        # policy); the plan they would have fed is now empty.
        page = tv_client.get("/api/candidates?verdict=protect&limit=10&offset=0").json()
        assert [r for r in page["items"] if r["group_key"] == "sonarr:1:42"]
        rollup = _rollup(page, "sonarr:1:42")
        assert rollup["condemned_count"] == 0
        assert rollup["condemned_bytes"] == 0

    def test_a_show_split_across_pages_carries_its_rollup_on_both(
        self, tv_client: TestClient
    ) -> None:
        """The queue merges rollups by key across every page it has fetched, so a show first
        seen on a later page must bring its own. Reading only the first page would leave that
        show's card with no count at all, beside its Reap button (rule 30)."""
        pages = [
            tv_client.get(f"/api/candidates?verdict=condemn&limit=10&offset={offset}").json()
            for offset in (0, 10, 20)
        ]
        carrying = [p for p in pages if any(g["group_key"] == "sonarr:1:42" for g in p["groups"])]
        assert len(carrying) > 1, "the fixture must split the show across pages"
        # Same whole-snapshot figures on every page that carries it, so which copy a merge
        # keeps cannot change the number.
        assert {
            (g["condemned_count"], g["condemned_bytes"], len(g["seasons"]))
            for p in carrying
            for g in p["groups"]
            if g["group_key"] == "sonarr:1:42"
        } == {(N_SEASONS, N_SEASONS * SEASON_SIZE, N_SEASONS)}
