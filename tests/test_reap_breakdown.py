# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reap breakdown: what a reap would remove, and why.

Pins the ledger (policy verdict, hand-spares out, hand-reaps in, the net), the movie/season
split, and the by-reason participation tally -- which counts every condemned title that
trips each signal, so the counts overlap and never partition.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Candidate, Snapshot
from reaper.db.session import create_engine, create_session_factory
from reaper.services import breakdown, whitelist

GB = 1024**3
NOW = utcnow()


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


def _explain(adds: list[str], *, keeps: list[str] | None = None) -> str:
    """A frozen explanation whose ``signals`` block adds the given ids and (optionally)
    carries an ``argues_keep`` signal that must never be counted."""
    signals = [
        {"id": sid, "contribution": 10.0, "weight": 70, "evaluated": True, "state": "adds"}
        for sid in adds
    ]
    signals += [
        {"id": sid, "contribution": 0.0, "weight": 20, "evaluated": True, "state": "argues_keep"}
        for sid in keeps or []
    ]
    return json.dumps({"signals": signals, "protections_fired": [], "protections_unknown": []})


async def _add(
    session: AsyncSession,
    *,
    snapshot_id: int,
    media_key: str,
    verdict: str = "condemn",
    media_type: str = "movie",
    size: int | None = 5 * GB,
    explanation: str | None = None,
) -> None:
    session.add(
        Candidate(
            snapshot_id=snapshot_id,
            media_key=media_key,
            title=f"Item {media_key}",
            media_type=media_type,
            size_bytes=size,
            verdict=verdict,
            score=90,
            coverage_bp=10_000,
            explanation_json=explanation if explanation is not None else _explain(["unwatched"]),
            created_at=NOW,
        )
    )
    await session.flush()


async def _snapshot(session: AsyncSession) -> int:
    snap = Snapshot(
        created_at=NOW, policy_hash="p" * 64, scoring_hash="s" * 64, horizon_at=NOW, item_count=0
    )
    session.add(snap)
    await session.flush()
    return snap.id


async def test_no_snapshot_is_empty(session: AsyncSession) -> None:
    report = await breakdown.reap_breakdown(session)
    assert report.has_snapshot is False
    assert report.will_reap == 0
    assert report.condemned_by == []


async def test_ledger_without_overrides(session: AsyncSession) -> None:
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1", size=3 * GB)
    await _add(
        session, snapshot_id=snap, media_key="sonarr:1:2:s1", media_type="season", size=8 * GB
    )

    report = await breakdown.reap_breakdown(session)

    assert report.has_snapshot is True
    assert report.policy_condemned == 2
    assert report.hand_spared == 0
    assert report.hand_reaped == 0
    assert report.will_reap == 2
    assert report.will_reap_bytes == 11 * GB
    assert report.movies == 1
    assert report.seasons == 1


async def test_a_hand_spare_leaves_the_net(session: AsyncSession) -> None:
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1")
    await _add(session, snapshot_id=snap, media_key="radarr:1:2")
    await whitelist.set_override(
        session, media_key="radarr:1:1", title="x", decision="spare", note=None
    )

    report = await breakdown.reap_breakdown(session)

    assert report.policy_condemned == 2
    assert report.hand_spared == 1
    assert report.will_reap == 1


async def test_a_hand_reap_joins_the_net(session: AsyncSession) -> None:
    """A row the policy did not condemn, forced on by hand, is a net reap the policy
    verdict never had."""
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1")  # condemned
    # An abstained row with a clean explanation resolves to condemn under a hand reap.
    await _add(
        session, snapshot_id=snap, media_key="radarr:1:9", verdict="abstain", explanation="{}"
    )
    await whitelist.set_override(
        session, media_key="radarr:1:9", title="x", decision="reap", note=None
    )

    report = await breakdown.reap_breakdown(session)

    assert report.policy_condemned == 1
    assert report.hand_reaped == 1
    assert report.will_reap == 2


async def test_by_reason_participation_overlaps(session: AsyncSession) -> None:
    snap = await _snapshot(session)
    # One trips two signals, the other trips one. Counts overlap: unwatched=2, low_rating=1.
    await _add(
        session,
        snapshot_id=snap,
        media_key="radarr:1:1",
        explanation=_explain(["unwatched", "low_rating"], keeps=["few_watchers"]),
    )
    await _add(
        session, snapshot_id=snap, media_key="radarr:1:2", explanation=_explain(["unwatched"])
    )

    report = await breakdown.reap_breakdown(session)

    by = {s.id: s.count for s in report.condemned_by}
    assert by == {"unwatched": 2, "low_rating": 1}
    # An argues_keep signal is never counted as a reason to remove.
    assert "few_watchers" not in by
    # Sorted most-common first.
    assert report.condemned_by[0].id == "unwatched"


async def test_state_absent_falls_back_to_contribution(session: AsyncSession) -> None:
    """Rows frozen before the ``state`` field: a positive contribution still counts as a
    driver, a zero one does not."""
    snap = await _snapshot(session)
    exp = json.dumps(
        {
            "signals": [
                {"id": "unwatched", "contribution": 40.0, "weight": 70, "evaluated": True},
                {"id": "size", "contribution": 0.0, "weight": 10, "evaluated": True},
            ],
            "protections_fired": [],
            "protections_unknown": [],
        }
    )
    await _add(session, snapshot_id=snap, media_key="radarr:1:1", explanation=exp)

    report = await breakdown.reap_breakdown(session)

    by = {s.id: s.count for s in report.condemned_by}
    assert by == {"unwatched": 1}


async def test_unreadable_explanation_still_counts_the_item(session: AsyncSession) -> None:
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1", explanation="not json")

    report = await breakdown.reap_breakdown(session)

    assert report.policy_condemned == 1
    assert report.will_reap == 1
    assert report.condemned_by == []  # no signals readable, but the file is still on the list


async def test_unmeasured_item_is_carried_as_a_count(session: AsyncSession) -> None:
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1", size=None)
    await _add(session, snapshot_id=snap, media_key="radarr:1:2", size=4 * GB)

    report = await breakdown.reap_breakdown(session)

    assert report.will_reap == 2
    assert report.will_reap_bytes == 4 * GB
    assert report.will_reap_unknown == 1
