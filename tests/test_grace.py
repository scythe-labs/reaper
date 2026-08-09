# SPDX-License-Identifier: AGPL-3.0-or-later
"""The grace period.

Grace is derived, not stored: an item's place in the window is its ``first_flagged_at``
plus the owner's ``grace_days``. These tests pin the partition (counting down vs cleared),
the clamp on "days remaining", the two exclusions that keep the countdown honest --
only the latest snapshot's condemned set, and never a spared item -- and that the clock
lookup survives a condemned set larger than SQLite will bind in one statement.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db import KEY_CHUNK
from reaper.db.base import Base
from reaper.db.models import Candidate, FirstFlagged, Snapshot
from reaper.db.session import create_engine, create_session_factory
from reaper.services import grace, whitelist

GB = 1024**3
NOW = utcnow()

#: The most bound variables SQLite will accept in one statement, read off the driver this
#: suite runs against rather than remembered. It is version-dependent -- 999 on builds older
#: than 3.32, 32,766 on newer ones -- so a hard-coded number would size the chunking test
#: against a ceiling that is not the one the code will hit (rule 144).
VARIABLE_CEILING = sqlite3.connect(":memory:").getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")
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
        """Canceling a grace spares the item; it must drop out at once, even though the
        frozen snapshot still says condemn."""
        snap = await _snapshot(session)
        await _condemn(session, snapshot_id=snap, media_key="radarr:1:1", flagged_days_ago=2)
        await whitelist.set_override(
            session, media_key="radarr:1:1", title="Item", decision="spare", note=None
        )

        report = await grace.grace_report(session, grace_days=14, now=NOW)

        assert report.in_grace == []
        assert report.total_bytes_in_grace == 0

    async def test_a_spare_on_the_whole_show_covers_its_seasons(
        self, session: AsyncSession
    ) -> None:
        """Sparing a show pulls its condemned seasons out of the countdown too. The
        planner and executor honor the show-level spare, so a grace view still calling
        a season "ready" would be a false alarm about a file nothing will touch."""
        snap = await _snapshot(session)
        await _condemn(session, snapshot_id=snap, media_key="sonarr:1:5:s2", flagged_days_ago=30)
        await whitelist.set_override(
            session, media_key="sonarr:1:5", title="Show", decision="spare", note=None
        )

        report = await grace.grace_report(session, grace_days=14, now=NOW)

        assert report.in_grace == []
        assert report.ready == []

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


async def _condemn_many(
    session: AsyncSession, *, snapshot_id: int, keys: list[str], flagged_days_ago: float
) -> None:
    """:func:`_condemn` for a library-sized set, in two executemany writes.

    Deliberately NOT one multi-row ``INSERT``: that binds a variable per column per row and
    would hit the same ceiling the test is about, from the fixture side.
    """
    flagged = NOW - timedelta(days=flagged_days_ago)
    await session.execute(
        insert(Candidate),
        [
            {
                "snapshot_id": snapshot_id,
                "media_key": key,
                "title": f"Item {key}",
                "media_type": "movie",
                "size_bytes": 5 * GB,
                "verdict": "condemn",
                "score": 90,
                "coverage_bp": 10_000,
                "explanation_json": "{}",
                "created_at": NOW,
            }
            for key in keys
        ],
    )
    await session.execute(
        insert(FirstFlagged),
        [
            {"media_key": key, "first_flagged_at": flagged, "last_seen_condemned_at": NOW}
            for key in keys
        ],
    )
    await session.flush()


class TestTheClockLookupIsChunked:
    async def test_a_condemned_set_past_the_variable_ceiling_still_reports(
        self, session: AsyncSession
    ) -> None:
        """Rule 94: the grace report reads its clocks in chunks, never one whole-set ``IN``.

        Nothing bounds the condemned set -- it is every item the latest snapshot condemned,
        plus every hand reap, minus spares -- so one expanding ``IN`` over it binds a variable
        per item and a large enough library raised ``OperationalError`` on the read itself.
        That is not an ``IntegrationError``, so nothing caught it and the whole report failed
        (#556).

        Sized one key past the ceiling this driver reports, so it fails the moment the loop in
        ``grace_report`` is collapsed back to a single statement -- driven red against exactly
        that edit.

        The count is not the whole assertion. Every item here was flagged 30 days ago against a
        14-day window, so a chunk whose rows never came back would take the missing-clock
        fallback, be dated ``now``, and land in ``in_grace``: an empty ``in_grace`` beside a full
        ``ready`` is what says the clocks were read for all of them, not merely that no
        exception escaped.
        """
        snap = await _snapshot(session)
        keys = [f"radarr:1:{n}" for n in range(VARIABLE_CEILING + 1)]
        await _condemn_many(session, snapshot_id=snap, keys=keys, flagged_days_ago=30)

        report = await grace.grace_report(session, grace_days=14, now=NOW)

        assert len(report.ready) == len(keys)
        assert report.in_grace == []
        assert {i.media_key for i in report.ready} == set(keys)
        assert report.total_bytes_ready == len(keys) * 5 * GB

    async def test_the_chunk_bound_is_under_what_sqlite_will_bind(self) -> None:
        """The shared bound is a bound, not a decoration.

        ``KEY_CHUNK`` is one declaration for every scan-sized ``IN`` in the tree, so a raise
        past what this driver accepts would break all of them at once, and the failure is a
        dead scan rather than a slow one.
        """
        assert 0 < KEY_CHUNK <= VARIABLE_CEILING
