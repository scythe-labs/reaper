# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounding what the scans leave behind.

**Nothing here deletes media.** "Pruning" in this codebase means ``services.season_pruning``,
which removes seasons from disk through Sonarr; this module deletes rows Reaper wrote about
itself, touches no file, no *arr and no Plex, and is spelled *sweep* throughout to keep the
two apart.

A scan is a snapshot (``services.snapshot``): every item's evidence is frozen and hashed
before anything is scored, so one ``Candidate`` row is written per item per scan whatever
its verdict -- condemn, abstain and protect alike. That is the design and it stays. What
was missing is the other half of it. Nothing ever deleted a snapshot, and every read path
takes only the newest one, so the table grew by the whole library on every scan and the
database grew without limit (#315). Measured, a row costs about 4 KB, two thirds of it the
frozen ``facts_json`` and the why-panel's ``explanation_json``, so a six-thousand-item
library added roughly 24 MB per scan forever.

This trims the table to the newest :data:`KEEP_SNAPSHOTS` and lets the schema do the
deleting: ``Candidate.snapshot_id`` is ``ondelete="CASCADE"`` and ``db.session`` turns
SQLite's foreign keys on, so dropping a snapshot row drops its candidates with it. Nothing
else hangs off either table -- the whole foreign-key graph into ``snapshot`` is that
cascade plus ``ReapRun``'s ``RESTRICT``.

**Two things are never swept, and both are the keep direction.**

*A snapshot a run is bound to.* ``ReapRun.snapshot_id`` is ``ondelete="RESTRICT"``, so the
schema refuses to drop one anyway; this module excludes them up front rather than leaning
on that raise, because a run's approval is bound to the exact rows it was planned against
(``services.planner``) and the journal is the audit trail. They are excluded whatever the
run's state: a finished run's record is the part an operator goes back to read.

*An operator's own decision.* Hand spares and forced reaps live in ``WhitelistEntry``,
keyed by ``media_key`` and deliberately "a decision about a file, not a property of the
scan that happened to surface it", and the first-flagged clock and the watch high-water
mark are keyed the same way. None of them is reachable from a snapshot, so sweeping one
cannot lose an operator decision or restart a grace window.

**Preexisting installs are the load-bearing case.** An install that has been scanning for
months arrives here with a backlog to drain in one pass, so the delete is batched into
small transactions rather than one that would hold SQLite's write lock across millions of
rows while the UI waits behind it. SQLite also never returns freed pages to the filesystem
on its own, so an operator whose database is suddenly mostly dead space would see no disk
come back: :func:`compact_if_fragmented` is what closes that gap, and it is deliberately
rare (see its own note).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import structlog
from sqlalchemy import Select, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.db.models import ReapRun, Snapshot

log = structlog.get_logger(__name__)

#: How many of the newest scans survive a sweep. Everything older goes, unless a run is
#: bound to it. Thirty is roughly a month of nightly scanning, which is far more than any
#: read path uses -- every one of them takes the single newest snapshot. The cost is
#: linear and worth knowing before raising it: at ~4 KB per item per scan, thirty scans of
#: a six-thousand-item library is about 700 MB, and of a twenty-thousand-item library
#: about 2.4 GB.
KEEP_SNAPSHOTS = 30

#: Snapshots deleted per transaction. Small on purpose: each one cascades to a whole
#: library's worth of candidate rows, and a backlog drained in a single statement would
#: hold the write lock long enough for the UI and a scheduled scan to time out behind it.
SWEEP_BATCH = 10

#: Runaway guard on the drain loop, not a retention policy -- 50,000 snapshots is far past
#: any real backlog (hourly scans for five years). Hitting it logs and stops; the next
#: firing continues where this one left off.
MAX_SWEEP_BATCHES = 5_000

#: Compaction thresholds. ``VACUUM`` rewrites the whole file under an exclusive lock and
#: needs room for a second copy while it runs, so it must not fire after every sweep -- in
#: the steady state one snapshot of thirty is freed, some 3%, and those pages are reused by
#: the next scan anyway. It earns its lock only when a large share of a large file is dead,
#: which in practice means the first sweep after an install that had been keeping every
#: scan since it was installed upgrades to a version that trims them.
COMPACT_MIN_FREE_RATIO = 0.25
COMPACT_MIN_FREE_BYTES = 64 * 1024 * 1024


def _doomed(keep: int) -> Select[tuple[int]]:
    """Snapshot ids eligible to be swept, oldest first.

    Two exclusions, both fail-closed. ``NOT IN`` over an empty ``reap_run`` is true for
    every row, which is the right answer for an install that has never reaped; over a NULL
    it would be unknown and delete nothing, which is the safe direction and the only one
    that would matter if ``snapshot_id`` ever became nullable.
    """
    newest = select(Snapshot.id).order_by(Snapshot.id.desc()).limit(keep)
    return (
        select(Snapshot.id)
        .where(
            Snapshot.id.not_in(newest),
            Snapshot.id.not_in(select(ReapRun.snapshot_id)),
        )
        .order_by(Snapshot.id)
    )


async def sweep_old_snapshots(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    keep: int = KEEP_SNAPSHOTS,
) -> int:
    """Drop every snapshot past the newest ``keep``. Returns how many went.

    Owns its transactions rather than taking a session, because committing between batches
    is the point: a preexisting install may have thousands to drain, and each one cascades
    to a whole library of candidate rows.

    ``keep < 1`` raises rather than clamping. A zero would take the newest snapshot too and
    empty the review queue, which is rule 1's shape exactly: an empty selection must never
    expand to everything.
    """
    if keep < 1:
        raise ValueError(f"keep must be at least 1, got {keep}")

    removed = 0
    for _ in range(MAX_SWEEP_BATCHES):
        async with session_factory() as session:
            doomed = list((await session.execute(_doomed(keep).limit(SWEEP_BATCH))).scalars().all())
            if not doomed:
                return removed
            await session.execute(delete(Snapshot).where(Snapshot.id.in_(doomed)))
            await session.commit()
        removed += len(doomed)

    log.warning("retention.sweep_backstop_hit", removed=removed, batches=MAX_SWEEP_BATCHES)
    return removed


def _compact_sync(db_path: Path) -> bool:
    """The blocking half of :func:`compact_if_fragmented`. Returns whether it vacuumed.

    ``isolation_level=None`` is required, not tidiness: ``VACUUM`` cannot run inside a
    transaction, and the driver's default opens one on the first statement it takes for
    DML. The busy timeout lets it wait out an in-flight write rather than failing at once,
    the same courtesy ``services.backup`` extends to ``VACUUM INTO``.
    """
    con = sqlite3.connect(db_path, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        pages = int(con.execute("PRAGMA page_count").fetchone()[0])
        free = int(con.execute("PRAGMA freelist_count").fetchone()[0])
        page_size = int(con.execute("PRAGMA page_size").fetchone()[0])
        if not pages:
            return False
        if free / pages < COMPACT_MIN_FREE_RATIO or free * page_size < COMPACT_MIN_FREE_BYTES:
            return False
        con.execute("VACUUM")
        return True
    finally:
        con.close()


async def compact_if_fragmented(data_dir: Path) -> bool:
    """Return freed pages to the filesystem, but only when there are enough to matter.

    Runs in a worker thread: rewriting a multi-hundred-megabyte file would otherwise stall
    the event loop for the whole vacuum, exactly as ``services.backup`` found for
    ``VACUUM INTO``. Raises whatever SQLite raises -- a disk with no room for the second
    copy is the realistic failure, and the caller logs it and moves on, since a database
    that is merely larger than it needs to be still works.
    """
    return await asyncio.to_thread(_compact_sync, data_dir / "reaper.db")
