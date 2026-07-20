# SPDX-License-Identifier: AGPL-3.0-or-later
"""A hand reap takes effect the moment it is set -- honestly.

Before this pass a reap override changed nothing until the next scan: the queue kept its
green pill, the counts and the plan excluded the item, and grace never started. Now one
module (``services.condemned``) assembles the effective condemned set -- scan-condemned
minus hand-spares plus hand-reaps the engine honors -- and grace, the planner, the
confirmation counts and the executor all read it. Pinned here:

* ``reap_override_verdict`` plumbs a frozen row into ``decide_verdict`` and nothing
  else: cautious protections lose, structural stops and unchecked protections win, and
  an unreadable explanation reads as blocked (kept);
* the effective set adds and removes the right rows, including show-level decisions and
  a season spared back out of a reaped show;
* grace and the planner see a hand-reap immediately, and the plan refuses a refused one;
* the override routes start the grace clock on an effective reap and remove it again
  when the reap is withdrawn, so a stale hand-reap timestamp can never shorten a later,
  real condemnation's window (rule 4).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import ActionStep, Candidate, FirstFlagged, Snapshot
from reaper.db.session import create_engine, create_session_factory
from reaper.main import create_app
from reaper.services import grace, whitelist
from reaper.services.condemned import (
    effective_condemned,
    reap_is_effective,
    reap_override_verdict,
)
from reaper.services.planner import PlanError, build_plan
from tests._auth import login

GB = 1024**3
NOW = utcnow()

CAUTIOUS = json.dumps(
    {"protections_fired": [{"gate": "season_progression", "detail": "currently airing"}]}
)
STRUCTURAL = json.dumps(
    {"protections_fired": [{"gate": "streaming_now", "detail": "playing right now"}]}
)
UNMANAGED = json.dumps(
    {"protections_fired": [{"gate": "unmanaged", "detail": "no *arr manages this file"}]}
)
BLOCKED = json.dumps(
    {"protections_unknown": [{"gate": "curated_list", "detail": "could not check the list"}]}
)
UNMATCHED = json.dumps({"match": {"status": "unmatched"}})
CLEAN_ABSTAIN = json.dumps({"threshold": 70})


class TestReapOverrideVerdict:
    def test_a_cautious_protection_loses_to_the_owner(self) -> None:
        assert reap_override_verdict(CAUTIOUS, score=50) == "condemn"

    def test_a_structural_stop_still_wins(self) -> None:
        assert reap_override_verdict(STRUCTURAL, score=99) == "protect"
        assert reap_override_verdict(UNMANAGED, score=99) == "protect"

    def test_an_unchecked_protection_still_wins(self) -> None:
        assert reap_override_verdict(BLOCKED, score=99) == "protect"

    def test_a_match_problem_reads_as_blocked(self) -> None:
        assert reap_override_verdict(UNMATCHED, score=99) == "protect"

    def test_a_clean_abstain_condemns(self) -> None:
        assert reap_override_verdict(CLEAN_ABSTAIN, score=10) == "condemn"

    def test_an_unreadable_explanation_keeps_the_file(self) -> None:
        assert reap_override_verdict("not json", score=99) == "protect"
        assert reap_override_verdict("[1, 2]", score=99) == "protect"

    def test_the_score_is_inert_on_the_reap_branch(self) -> None:
        """decide_verdict's reap branch never reads the score or the thresholds; the
        zeros this module passes are plumbing, not policy. If this ever fails, the
        engine's decision order changed and services.condemned must be revisited."""
        for score in (0, 50, 100):
            assert reap_override_verdict(CAUTIOUS, score=score) == "condemn"
            assert reap_override_verdict(STRUCTURAL, score=score) == "protect"


# --- fixtures over a real database ------------------------------------------


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


async def _snapshot(session: AsyncSession) -> int:
    snap = Snapshot(
        created_at=NOW, policy_hash="p" * 64, scoring_hash="s" * 64, horizon_at=NOW, item_count=0
    )
    session.add(snap)
    await session.flush()
    return snap.id


async def _row(
    session: AsyncSession,
    snapshot_id: int,
    media_key: str,
    *,
    verdict: str,
    explanation: str = "{}",
    group_key: str | None = None,
    size: int = 2 * GB,
) -> None:
    session.add(
        Candidate(
            snapshot_id=snapshot_id,
            media_key=media_key,
            title=f"Item {media_key}",
            media_type="season" if media_key.count(":") == 3 else "movie",
            size_bytes=size,
            verdict=verdict,
            score=80,
            coverage_bp=10_000,
            explanation_json=explanation,
            group_key=group_key,
            created_at=NOW,
        )
    )
    await session.flush()


class TestEffectiveCondemned:
    async def test_spares_leave_and_honored_reaps_join(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            snap = await _snapshot(session)
            await _row(session, snap, "radarr:1:1", verdict="condemn")
            await _row(session, snap, "radarr:1:2", verdict="condemn")
            await _row(session, snap, "radarr:1:3", verdict="protect", explanation=CAUTIOUS)
            await _row(session, snap, "radarr:1:4", verdict="protect", explanation=STRUCTURAL)
            await whitelist.set_override(
                session, media_key="radarr:1:2", title="t", decision="spare", note=None
            )
            for key in ("radarr:1:3", "radarr:1:4"):
                await whitelist.set_override(
                    session, media_key=key, title="t", decision="reap", note=None
                )
            decisions = await whitelist.overrides(session)

            effective = await effective_condemned(session, snap, decisions)

        # 1 stays, 2 is spared out, 3's cautious protection loses to the owner,
        # 4's structural stop still wins.
        assert sorted(effective) == ["radarr:1:1", "radarr:1:3"]

    async def test_a_show_level_reap_reaches_seasons_but_not_a_spared_one(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            snap = await _snapshot(session)
            show = "sonarr:1:42"
            await _row(
                session, snap, f"{show}:1", verdict="protect", explanation=CAUTIOUS, group_key=show
            )
            await _row(
                session,
                snap,
                f"{show}:2",
                verdict="abstain",
                explanation=CLEAN_ABSTAIN,
                group_key=show,
            )
            await whitelist.set_override(
                session, media_key=show, title="t", decision="reap", note=None
            )
            # Season 2 is spared back out by its own key: the item's key wins.
            await whitelist.set_override(
                session, media_key=f"{show}:2", title="t", decision="spare", note=None
            )
            decisions = await whitelist.overrides(session)

            effective = await effective_condemned(session, snap, decisions)

        assert sorted(effective) == [f"{show}:1"]

    async def test_grace_starts_the_moment_the_owner_reaps(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            snap = await _snapshot(session)
            await _row(session, snap, "radarr:1:5", verdict="protect", explanation=CAUTIOUS)
            await whitelist.set_override(
                session, media_key="radarr:1:5", title="t", decision="reap", note=None
            )

            report = await grace.grace_report(session, grace_days=14, now=NOW)

        assert [i.media_key for i in report.in_grace] == ["radarr:1:5"]

    async def test_the_plan_includes_an_honored_reap_and_refuses_a_refused_one(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            snap = await _snapshot(session)
            await _row(session, snap, "radarr:1:6", verdict="condemn")
            await _row(session, snap, "radarr:1:7", verdict="protect", explanation=CAUTIOUS)
            await _row(session, snap, "radarr:1:8", verdict="protect", explanation=STRUCTURAL)
            for key in ("radarr:1:7", "radarr:1:8"):
                await whitelist.set_override(
                    session, media_key=key, title="t", decision="reap", note=None
                )

            run = await build_plan(
                session, snapshot_id=snap, policy_hash="p" * 64, approved_by="test"
            )
            await session.flush()

            planned_keys = {
                s.media_key
                for s in (
                    (await session.execute(select(ActionStep).where(ActionStep.run_id == run.id)))
                    .scalars()
                    .all()
                )
            }
            # The honored reap is planned; the structural refusal is not.
            assert planned_keys == {"radarr:1:6", "radarr:1:7"}

            # Naming the refused one explicitly fails loudly, never silently shrinks.
            with pytest.raises(PlanError):
                await build_plan(
                    session,
                    snapshot_id=snap,
                    policy_hash="p" * 64,
                    approved_by="test",
                    only_media_keys={"radarr:1:8"},
                )

    async def test_reap_is_effective_reads_the_frozen_row(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            snap = await _snapshot(session)
            await _row(session, snap, "radarr:1:9", verdict="condemn")
            await _row(session, snap, "radarr:1:10", verdict="protect", explanation=STRUCTURAL)
            fetched = (
                (await session.execute(select(Candidate).where(Candidate.snapshot_id == snap)))
                .scalars()
                .all()
            )
            by_key = {c.media_key: c for c in fetched}
        assert reap_is_effective(by_key["radarr:1:9"]) is True
        assert reap_is_effective(by_key["radarr:1:10"]) is False


# --- the override routes start and stop the grace clock -----------------------


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A logged-in client over a database seeded with one snapshot and three rows:
    a scan-condemned movie, a cautiously-protected one, and a structurally-protected
    one. Sync seeding, because the app builds its own async engine over the same file."""
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        snap = Snapshot(
            created_at=NOW,
            policy_hash="p" * 64,
            scoring_hash="s" * 64,
            horizon_at=NOW,
            item_count=3,
        )
        s.add(snap)
        s.flush()

        def add(media_key: str, verdict: str, explanation: str) -> None:
            s.add(
                Candidate(
                    snapshot_id=snap.id,
                    media_key=media_key,
                    title=f"Item {media_key}",
                    media_type="movie",
                    size_bytes=GB,
                    verdict=verdict,
                    score=80,
                    coverage_bp=10_000,
                    explanation_json=explanation,
                    created_at=NOW,
                )
            )

        add("radarr:1:21", "condemn", "{}")
        add("radarr:1:22", "protect", CAUTIOUS)
        add("radarr:1:23", "protect", STRUCTURAL)
        s.add(
            FirstFlagged(media_key="radarr:1:21", first_flagged_at=NOW, last_seen_condemned_at=NOW)
        )
        s.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


def _clock_rows(tmp_path: Path) -> dict[str, Any]:
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    with Session(engine) as s:
        rows = {f.media_key: f.first_flagged_at for f in s.query(FirstFlagged).all()}
    engine.dispose()
    return rows


class TestOverrideRoutesAndTheGraceClock:
    def test_an_honored_reap_starts_the_clock_and_withdrawing_it_stops_it(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        response = client.post(
            "/api/override", json={"media_key": "radarr:1:22", "decision": "reap"}
        )
        assert response.status_code == 200, response.text
        assert "radarr:1:22" in _clock_rows(tmp_path)

        response = client.delete("/api/override/radarr:1:22")
        assert response.status_code == 200
        assert "radarr:1:22" not in _clock_rows(tmp_path)

    def test_a_refused_reap_never_starts_a_clock(self, client: TestClient, tmp_path: Path) -> None:
        response = client.post(
            "/api/override", json={"media_key": "radarr:1:23", "decision": "reap"}
        )
        assert response.status_code == 200, response.text
        assert "radarr:1:23" not in _clock_rows(tmp_path)

    def test_unreaping_a_scan_condemned_item_keeps_the_scan_clock(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """The scan owns that clock: the item is still condemned, so its countdown must
        survive an override coming and going."""
        client.post("/api/override", json={"media_key": "radarr:1:21", "decision": "reap"})
        client.delete("/api/override/radarr:1:21")
        assert "radarr:1:21" in _clock_rows(tmp_path)

    def test_flipping_a_reap_to_spare_removes_the_hand_clock(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        client.post("/api/override", json={"media_key": "radarr:1:22", "decision": "reap"})
        assert "radarr:1:22" in _clock_rows(tmp_path)
        client.post("/api/override", json={"media_key": "radarr:1:22", "decision": "spare"})
        assert "radarr:1:22" not in _clock_rows(tmp_path)

    def test_the_queue_reports_whether_a_reap_took(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        client.post("/api/override", json={"media_key": "radarr:1:22", "decision": "reap"})
        client.post("/api/override", json={"media_key": "radarr:1:23", "decision": "reap"})

        rows = client.get("/api/candidates?verdict=protect&limit=50").json()
        by_key = {r["media_key"]: r for r in rows}

        assert by_key["radarr:1:22"]["override_effective"] is True
        assert by_key["radarr:1:23"]["override_effective"] is False
