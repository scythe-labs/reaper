# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scan-history sweep.

Nothing ever deleted a snapshot, so the candidate table grew by the whole library on every
scan and the database grew without limit (#315). These tests pin the window itself, the
cascade that makes it cheap, the two things the sweep must never take (a snapshot a run is
bound to, and an operator's own decision), and the batching that lets a preexisting install
drain a backlog without holding the write lock across the lot.

``keep`` is deliberately passed as something other than :data:`retention.KEEP_SNAPSHOTS`
throughout (rule 141): pinning the production default would hold whether or not the
argument reached the query. One test pins the shipped constant on its own, since thirty is
a promise to the operator about how much disk this costs.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import (
    Candidate,
    FirstFlagged,
    ReapRun,
    RunState,
    Snapshot,
    WhitelistEntry,
)
from reaper.db.session import create_engine, create_session_factory
from reaper.services import retention

NOW = utcnow()


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


async def _snapshot(session: AsyncSession, *, items: int = 0) -> int:
    """One scan, with ``items`` candidate rows hanging off it."""
    snap = Snapshot(
        created_at=NOW,
        policy_hash="p" * 64,
        scoring_hash="s" * 64,
        horizon_at=NOW,
        item_count=items,
    )
    session.add(snap)
    await session.flush()
    for n in range(items):
        session.add(
            Candidate(
                snapshot_id=snap.id,
                media_key=f"radarr:1:{n}",
                title=f"Item {n}",
                media_type="movie",
                size_bytes=1024,
                verdict="condemn",
                score=90,
                coverage_bp=10_000,
                explanation_json="{}",
                created_at=NOW,
            )
        )
    await session.flush()
    return snap.id


async def _seed(
    factory: async_sessionmaker[AsyncSession], count: int, *, items: int = 0
) -> list[int]:
    async with factory() as session:
        ids = [await _snapshot(session, items=items) for _ in range(count)]
        await session.commit()
    return ids


async def _snapshot_ids(factory: async_sessionmaker[AsyncSession]) -> list[int]:
    async with factory() as session:
        return list((await session.execute(select(Snapshot.id).order_by(Snapshot.id))).scalars())


async def _candidate_count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return int((await session.execute(select(func.count(Candidate.id)))).scalar_one())


def _pages(data_dir: Path) -> tuple[int, int]:
    """``(page_count, freelist_count)`` -- the file's real shape, not its size on disk.

    The database is in WAL mode, so committed pages sit in ``reaper.db-wal`` until a
    checkpoint and the main file's byte size says nothing about how much of it is dead.
    These two pragmas are what :func:`retention._compact_sync` itself decides on.
    """
    con = sqlite3.connect(data_dir / "reaper.db", isolation_level=None)
    try:
        return (
            int(con.execute("PRAGMA page_count").fetchone()[0]),
            int(con.execute("PRAGMA freelist_count").fetchone()[0]),
        )
    finally:
        con.close()


class TestTheWindow:
    async def test_it_keeps_the_newest_and_drops_the_rest(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed(factory, 7)

        removed = await retention.sweep_old_snapshots(factory, keep=3)

        assert removed == 4
        assert await _snapshot_ids(factory) == ids[-3:]

    async def test_a_history_shorter_than_the_window_is_left_alone(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed(factory, 3)

        assert await retention.sweep_old_snapshots(factory, keep=5) == 0
        assert await _snapshot_ids(factory) == ids

    async def test_an_install_that_has_never_scanned_sweeps_nothing(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The empty case is the one that must not raise on a fresh install: the sweep
        fires twice a day from boot, long before the first scan on many installs."""
        assert await retention.sweep_old_snapshots(factory, keep=4) == 0

    async def test_the_candidates_go_with_their_snapshot(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The cascade is what makes this cheap, and it is a schema property -- delete it
        from the model and the rows are orphaned rather than removed."""
        await _seed(factory, 5, items=4)
        assert await _candidate_count(factory) == 20

        await retention.sweep_old_snapshots(factory, keep=2)

        assert await _candidate_count(factory) == 8

    async def test_the_shipped_window_is_thirty(self) -> None:
        """A promise about disk: at ~4 KB per item per scan this is what the operator pays
        to keep a month of history, so changing it is a deliberate act, not a tweak."""
        assert retention.KEEP_SNAPSHOTS == 30


class TestWhatTheSweepMayNotTake:
    async def test_a_snapshot_a_run_is_bound_to_survives_forever(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The journal is the audit trail and a run's approval is bound to the exact rows
        it was planned against, so the oldest scan in the database stays if a run points at
        it -- whatever the run's state, and however far past the window it falls."""
        ids = await _seed(factory, 6, items=2)
        async with factory() as session:
            session.add(
                ReapRun(
                    snapshot_id=ids[0],
                    policy_hash="p" * 64,
                    state=RunState.COMPLETED,
                    approved_manifest_hash="m" * 64,
                    approved_by="owner",
                    approved_at=NOW,
                )
            )
            await session.commit()

        removed = await retention.sweep_old_snapshots(factory, keep=2)

        assert removed == 3
        assert await _snapshot_ids(factory) == [ids[0], *ids[-2:]]
        # Its candidates are the rows the run's approval was bound to, so they stay too.
        assert await _candidate_count(factory) == 6

    async def test_operator_decisions_outlive_the_scans_that_surfaced_them(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A hand spare and the grace clock are keyed by media, not by scan -- deliberately
        "a decision about a file, not a property of the scan that happened to surface it".
        Sweeping the scan that first showed the item must not drop either, or a spare would
        silently lapse and a grace window would restart from zero."""
        await _seed(factory, 4, items=1)
        async with factory() as session:
            session.add(
                WhitelistEntry(
                    media_key="radarr:1:0",
                    title="Item 0",
                    decision="spare",
                    created_at=NOW,
                )
            )
            session.add(
                FirstFlagged(
                    media_key="radarr:1:0",
                    first_flagged_at=NOW,
                    last_seen_condemned_at=NOW,
                )
            )
            await session.commit()

        await retention.sweep_old_snapshots(factory, keep=1)

        async with factory() as session:
            spares = list((await session.execute(select(WhitelistEntry.media_key))).scalars())
            flags = list((await session.execute(select(FirstFlagged.media_key))).scalars())
        assert spares == ["radarr:1:0"]
        assert flags == ["radarr:1:0"]

    async def test_a_window_below_one_raises_rather_than_emptying_the_queue(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Rule 1: an empty selection must never expand to everything. A zero here would
        take the newest snapshot too, which is the one every read path reads."""
        await _seed(factory, 3)

        with pytest.raises(ValueError, match="keep must be at least 1"):
            await retention.sweep_old_snapshots(factory, keep=0)

        assert len(await _snapshot_ids(factory)) == 3


class TestPreexistingInstalls:
    async def test_a_backlog_larger_than_one_batch_drains_in_a_single_pass(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The upgrade case: an install that has been scanning for months arrives with far
        more than one batch to drop, and must not need a fortnight of firings to catch up.
        Sized off the real batch constant so raising it cannot silently make this a
        single-batch test that proves nothing about the loop."""
        backlog = retention.SWEEP_BATCH * 2 + 3
        ids = await _seed(factory, backlog + 2, items=1)

        removed = await retention.sweep_old_snapshots(factory, keep=2)

        assert removed == backlog
        assert await _snapshot_ids(factory) == ids[-2:]
        assert await _candidate_count(factory) == 2


class TestCompaction:
    """``VACUUM`` is what actually hands disk back, since SQLite never returns freed pages
    on its own. The thresholds are monkeypatched down rather than met for real: proving the
    decision needs a fragmented file, not a 64 MB one.
    """

    async def test_it_declines_a_database_with_nothing_to_reclaim(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        await _seed(factory, 2, items=2)

        assert await retention.compact_if_fragmented(tmp_path) is False

    async def test_it_reclaims_a_mostly_empty_one(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed(factory, 40, items=60)
        await retention.sweep_old_snapshots(factory, keep=1)
        pages_before, free_before = _pages(tmp_path)
        assert free_before > 0, "the sweep should have left dead pages to reclaim"
        monkeypatch.setattr(retention, "COMPACT_MIN_FREE_BYTES", 1024)

        assert await retention.compact_if_fragmented(tmp_path) is True

        pages_after, free_after = _pages(tmp_path)
        assert free_after == 0
        assert pages_after < pages_before

    async def test_a_large_file_with_a_small_dead_share_is_left_alone(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both thresholds have to agree. With the byte floor out of the way the ratio is
        what declines the lock, which is the steady state: one snapshot of thirty freed is
        some 3% of the file, and the next scan reuses those pages anyway."""
        await _seed(factory, 40, items=60)
        await retention.sweep_old_snapshots(factory, keep=39)
        monkeypatch.setattr(retention, "COMPACT_MIN_FREE_BYTES", 1024)

        assert await retention.compact_if_fragmented(tmp_path) is False
