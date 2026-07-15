# SPDX-License-Identifier: AGPL-3.0-or-later
"""The grace period.

Grace is derived, not stored: an item's place in the window is its ``first_flagged_at``
plus the owner's ``grace_days``. These tests pin the partition (counting down vs cleared),
the clamp on "days remaining", and the two exclusions that keep the countdown honest --
only the latest snapshot's condemned set, and never a spared item.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Candidate, FirstFlagged, Snapshot
from reaper.db.session import create_engine, create_session_factory
from reaper.services import grace, whitelist

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


async def _condemn(
    session: AsyncSession,
    *,
    snapshot_id: int,
    media_key: str,
    flagged_days_ago: float,
    size: int = 5 * GB,
    verdict: str = "condemn",
) -> None:
    session.add(
        Candidate(
            snapshot_id=snapshot_id,
            media_key=media_key,
            title=f"Item {media_key}",
            media_type="movie",
            size_bytes=size,
            verdict=verdict,
            score=90,
            coverage_bp=10_000,
            explanation_json="{}",
            created_at=NOW,
        )
    )
    if verdict == "condemn":
        flagged = NOW - timedelta(days=flagged_days_ago)
        session.add(
            FirstFlagged(media_key=media_key, first_flagged_at=flagged, last_seen_condemned_at=NOW)
        )
    await session.flush()


async def _snapshot(session: AsyncSession) -> int:
    snap = Snapshot(
        created_at=NOW, policy_hash="p" * 64, scoring_hash="s" * 64, horizon_at=NOW, item_count=0
    )
    session.add(snap)
    await session.flush()
    return snap.id


class TestPartition:
    async def test_recent_flags_are_in_grace_old_ones_are_ready(
        self, session: AsyncSession
    ) -> None:
        snap = await _snapshot(session)
        await _condemn(session, snapshot_id=snap, media_key="radarr:1:1", flagged_days_ago=2)
        await _condemn(session, snapshot_id=snap, media_key="radarr:1:2", flagged_days_ago=30)

        report = await grace.grace_report(session, grace_days=14, now=NOW)

        assert [i.media_key for i in report.in_grace] == ["radarr:1:1"]
        assert [i.media_key for i in report.ready] == ["radarr:1:2"]

    async def test_days_remaining_is_clamped_at_zero(self, session: AsyncSession) -> None:
        snap = await _snapshot(session)
        await _condemn(session, snapshot_id=snap, media_key="radarr:1:1", flagged_days_ago=100)

        report = await grace.grace_report(session, grace_days=14, now=NOW)

        assert report.ready[0].days_remaining == 0  # not -86

    async def test_in_grace_sorted_soonest_to_clear_first(self, session: AsyncSession) -> None:
        snap = await _snapshot(session)
        await _condemn(session, snapshot_id=snap, media_key="radarr:1:1", flagged_days_ago=1)
        await _condemn(session, snapshot_id=snap, media_key="radarr:1:2", flagged_days_ago=10)

        report = await grace.grace_report(session, grace_days=14, now=NOW)

        # The one flagged longer ago clears sooner, so it leads.
        assert [i.media_key for i in report.in_grace] == ["radarr:1:2", "radarr:1:1"]

    async def test_totals_sum_each_bucket(self, session: AsyncSession) -> None:
        snap = await _snapshot(session)
        await _condemn(
            session, snapshot_id=snap, media_key="radarr:1:1", flagged_days_ago=2, size=3 * GB
        )
        await _condemn(
            session, snapshot_id=snap, media_key="radarr:1:2", flagged_days_ago=30, size=8 * GB
        )

        report = await grace.grace_report(session, grace_days=14, now=NOW)

        assert report.total_bytes_in_grace == 3 * GB
        assert report.total_bytes_ready == 8 * GB


class TestExclusions:
    async def test_a_spared_item_leaves_the_countdown(self, session: AsyncSession) -> None:
        """Cancelling a grace spares the item; it must drop out at once, even though the
        frozen snapshot still says condemn."""
        snap = await _snapshot(session)
        await _condemn(session, snapshot_id=snap, media_key="radarr:1:1", flagged_days_ago=2)
        await whitelist.spare(session, media_key="radarr:1:1", title="Item", note=None)

        report = await grace.grace_report(session, grace_days=14, now=NOW)

        assert report.in_grace == []
        assert report.total_bytes_in_grace == 0

    async def test_only_the_latest_snapshot_counts(self, session: AsyncSession) -> None:
        old = await _snapshot(session)
        await _condemn(session, snapshot_id=old, media_key="radarr:1:9", flagged_days_ago=2)
        new = await _snapshot(session)
        await _condemn(session, snapshot_id=new, media_key="radarr:1:1", flagged_days_ago=2)

        report = await grace.grace_report(session, grace_days=14, now=NOW)

        assert [i.media_key for i in report.in_grace] == ["radarr:1:1"]

    async def test_no_snapshot_is_an_empty_report(self, session: AsyncSession) -> None:
        report = await grace.grace_report(session, grace_days=14, now=NOW)
        assert report.in_grace == [] and report.ready == []
