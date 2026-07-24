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
from datetime import datetime, timedelta
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
# The keep-rule conflict: the season guard flags a prunable season watched MORE than one
# the rule keeps, and hands the call to a human. It rides in `protections_unknown` (blocked,
# so the scan abstains and asks for a look) but its detail is a plain-language decision for
# the owner, never a "could not check ...". A hand reap IS that decision, so it condemns.
KEEP_RULE_CONFLICT = json.dumps(
    {
        "protections_unknown": [
            {
                "gate": "season_progression",
                "detail": "5 people watched this season, more than one your keep rule protects.",
            }
        ]
    }
)
# Belt-and-suspenders: even a deferrable gate holds the reap if its block is a genuine
# plumbing failure ("could not check ..."), so the gate id alone can never open a fail-open
# path. (The season guard does not emit such a block today; this pins that it stays safe.)
CONFLICT_BUT_PLUMBING = json.dumps(
    {
        "protections_unknown": [
            {"gate": "season_progression", "detail": "could not check the sequential guard"}
        ]
    }
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

    def test_a_keep_rule_conflict_loses_to_the_owner(self) -> None:
        """The season keep-rule conflict is a deliberate "you decide" flag, not a
        protection Reaper could not check. A hand reap is exactly the decision it asked
        for, so it condemns -- unlike an unchecked protection, which still holds."""
        assert reap_override_verdict(KEEP_RULE_CONFLICT, score=90) == "condemn"

    def test_a_deferrable_gate_that_actually_failed_still_wins(self) -> None:
        """A "could not check ..." block holds the reap even on a deferrable gate: the
        gate id alone never opens a fail-open path (belt-and-suspenders)."""
        assert reap_override_verdict(CONFLICT_BUT_PLUMBING, score=90) == "protect"

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
            assert reap_override_verdict(KEEP_RULE_CONFLICT, score=score) == "condemn"
            assert reap_override_verdict(STRUCTURAL, score=score) == "protect"
            assert reap_override_verdict(CONFLICT_BUT_PLUMBING, score=score) == "protect"


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


def _seed_clock(
    tmp_path: Path,
    media_key: str,
    *,
    first_flagged_at: datetime,
    last_seen_condemned_at: datetime,
) -> None:
    """Force a grace clock into a chosen (possibly stale) state, standing in for the scans that
    re-condemned an item while it still showed spared -- the B-2 burn-down."""
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    with Session(engine) as s:
        s.merge(
            FirstFlagged(
                media_key=media_key,
                first_flagged_at=first_flagged_at,
                last_seen_condemned_at=last_seen_condemned_at,
            )
        )
        s.commit()
    engine.dispose()


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

    def test_sparing_a_scan_condemned_item_restarts_its_clock_on_unspare(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """A scan-condemned item the owner SPARES leaves the reap list, so its clock is
        dropped; un-sparing re-enters it on a FRESH window rather than the weeks-old one it
        left with, which would drop it straight past grace with no warning (rule 4).
        radarr:1:21 was first flagged at the fixture's NOW."""
        original = _clock_rows(tmp_path)["radarr:1:21"]
        # Spared: off the list, clock gone.
        client.post("/api/override", json={"media_key": "radarr:1:21", "decision": "spare"})
        assert "radarr:1:21" not in _clock_rows(tmp_path)
        # Un-spared: back on the list, but the countdown starts now -- not the spent one.
        client.delete("/api/override/radarr:1:21")
        refreshed = _clock_rows(tmp_path)
        assert "radarr:1:21" in refreshed
        assert refreshed["radarr:1:21"] > original

    def test_clearing_a_spare_restarts_a_clock_burned_down_while_invisible(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """B-2 defense in depth (rule 71): if an item was invisibly re-condemned while it still
        showed spared (the burn-down Phase 1's durable purge now prevents), clearing the stale
        spare must NOT coast on the spent clock -- it restarts on a fresh window. Without the
        cleared_spare wipe, record_first_flagged_bulk would honor the weeks-old first_flagged."""
        # Spared: off the list, its scan clock is dropped.
        client.post("/api/override", json={"media_key": "radarr:1:21", "decision": "spare"})
        assert "radarr:1:21" not in _clock_rows(tmp_path)
        # The invisible burn-down: first flagged three weeks ago, last seen condemned yesterday
        # -- exactly what the recorder would treat as a clock still legitimately running.
        _seed_clock(
            tmp_path,
            "radarr:1:21",
            first_flagged_at=NOW - timedelta(days=21),
            last_seen_condemned_at=NOW - timedelta(days=1),
        )
        # Clearing the spare re-enters the item on a FRESH window, not the spent one.
        client.delete("/api/override/radarr:1:21")
        refreshed = _clock_rows(tmp_path)["radarr:1:21"]
        assert refreshed > NOW - timedelta(days=2)

    def test_the_queue_reports_whether_a_reap_took(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        client.post("/api/override", json={"media_key": "radarr:1:22", "decision": "reap"})
        client.post("/api/override", json={"media_key": "radarr:1:23", "decision": "reap"})

        # A hand reap the engine honors moves the item onto the Condemned lane; one it will not
        # honor yet (a held reap) stays on the Kept lane, its stored verdict pure policy beneath.
        condemned = {
            r["media_key"]: r for r in client.get("/api/candidates?verdict=condemn&limit=50").json()
        }
        kept = {
            r["media_key"]: r for r in client.get("/api/candidates?verdict=protect&limit=50").json()
        }

        assert condemned["radarr:1:22"]["override_effective"] is True
        assert kept["radarr:1:23"]["override_effective"] is False


# --- override views in API responses: own vs inherited-from-show ---------------


@pytest.fixture
def show_client(tmp_path: Path) -> Iterator[TestClient]:
    """A logged-in client over one show with two seasons: one the scan condemned, one it
    cautiously kept. Both carry the show's group key, so a whole-show override reaches them."""
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    show = "sonarr:1:42"
    with Session(engine) as s:
        snap = Snapshot(
            created_at=NOW,
            policy_hash="p" * 64,
            scoring_hash="s" * 64,
            horizon_at=NOW,
            item_count=2,
        )
        s.add(snap)
        s.flush()
        for n, verdict, explanation in ((1, "condemn", "{}"), (2, "protect", CAUTIOUS)):
            s.add(
                Candidate(
                    snapshot_id=snap.id,
                    media_key=f"{show}:{n}",
                    title=f"Season {n}",
                    media_type="season",
                    size_bytes=GB,
                    verdict=verdict,
                    score=80,
                    coverage_bp=10_000,
                    explanation_json=explanation,
                    group_key=show,
                    group_title="A Show",
                    created_at=NOW,
                )
            )
        s.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestOverrideViewsInResponses:
    """The three views a control needs: the decision in effect (colors the row), the item's
    OWN decision (what the control toggles), and the show's decision (the note's source)."""

    SHOW = "sonarr:1:42"

    def _seasons(self, client: TestClient) -> dict[str, Any]:
        group = client.get(f"/api/groups/{self.SHOW}").json()
        return {s["media_key"]: s for s in group["seasons"]}

    def test_a_whole_show_spare_reads_as_inherited_on_each_season(
        self, show_client: TestClient
    ) -> None:
        show_client.post("/api/override", json={"media_key": self.SHOW, "decision": "spare"})
        group = show_client.get(f"/api/groups/{self.SHOW}").json()
        # The whole-show control toggles the show key, so the group reports the show's decision.
        assert group["show_override"] == "spare"
        season = self._seasons(show_client)[f"{self.SHOW}:1"]
        assert season["override"] == "spare"  # effective: the row reads kept
        assert season["override_own"] is None  # nothing of its own for a season control to undo
        assert season["show_override"] == "spare"  # what the "kept by the whole show" note names

    def test_a_season_spared_on_its_own_owns_it(self, show_client: TestClient) -> None:
        show_client.post("/api/override", json={"media_key": f"{self.SHOW}:1", "decision": "spare"})
        group = show_client.get(f"/api/groups/{self.SHOW}").json()
        assert group["show_override"] is None  # the show itself is undecided
        season = self._seasons(show_client)[f"{self.SHOW}:1"]
        assert season["override"] == "spare"
        assert season["override_own"] == "spare"  # its own key: the control can clear it
        assert season["show_override"] is None  # no whole-show note

    def test_an_own_reap_wins_over_a_show_spare_in_the_views(self, show_client: TestClient) -> None:
        show_client.post("/api/override", json={"media_key": self.SHOW, "decision": "spare"})
        show_client.post("/api/override", json={"media_key": f"{self.SHOW}:1", "decision": "reap"})
        season = self._seasons(show_client)[f"{self.SHOW}:1"]
        assert season["override"] == "reap"  # the item's own key wins: it will be removed
        assert season["override_own"] == "reap"
        assert season["show_override"] == "spare"  # the note still names the show's choice

    def test_a_movie_owns_its_effective_decision(self, client: TestClient) -> None:
        client.post("/api/override", json={"media_key": "radarr:1:22", "decision": "spare"})
        rows = client.get("/api/candidates?verdict=protect&limit=50").json()
        movie = {r["media_key"]: r for r in rows}["radarr:1:22"]
        assert movie["override"] == "spare"
        assert movie["override_own"] == "spare"  # no show to inherit from
        assert movie["show_override"] is None
