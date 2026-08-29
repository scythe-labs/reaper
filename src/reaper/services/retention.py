# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounding what the scans leave behind.

Nothing here deletes media. "Pruning" in this codebase means ``services.season_pruning``,
which removes seasons from disk through Sonarr. This module only deletes rows Reaper wrote
about itself: it touches no file, no *arr, and no Plex, and is called *sweep* throughout to
keep the two apart.

A scan is a snapshot (``services.snapshot``): every item's evidence is frozen and hashed
before anything is scored, so one ``Candidate`` row is written per item per scan whatever
its verdict, whether condemn, abstain, or protect. That design stays. Without a sweep,
nothing ever deleted an old snapshot, and the queue reads only the newest one, so the table
grew by the whole library on every scan with no limit.

This trims the table to the newest :data:`KEEP_SNAPSHOTS` and lets the schema do the
deleting: ``Candidate.snapshot_id`` is ``ondelete="CASCADE"``, and ``db.session`` turns
SQLite's foreign keys on, so dropping a snapshot row drops its candidates with it.
``SeasonPruneEvidence.snapshot_id`` is a second such cascade, one row per show carrying the
frozen inputs the policy simulator replays a season rule from. Those two cascades, plus
``ReapRun``'s ``RESTRICT``, are the whole foreign-key graph into ``snapshot``, and nothing
at all points at ``candidate``. This module needs no code change to sweep a new cascade, so
the sentence naming this graph must be updated whenever a new one is added.

That graph does not show the reader this module most has to answer to.
``ActionStep.media_key`` is a soft join with no foreign key behind it, and
``executor._rolling_30d_deletions`` uses it to price the operator's rolling delete budget:
it joins each step back to the candidate row of the snapshot its own run was planned
against, across every run in the trailing thirty days. It is an inner join, so a missing
candidate row does not fail; it silently drops that deletion out of the tally, so the cap
reads light and the executor could spend past what the operator set. What keeps it whole is
the run exclusion below, and nothing else. So this is a safety interlock, not just deference
to a schema constraint, and narrowing it to live or recent runs would silently unprice the
cap. ``executor`` and ``api.runs`` read a run's own snapshot the same way, and
``api.whitelist._resolve_title`` reads across all history to answer "do we know this item",
a read this bounds rather than breaks, and its refusal names the bound as a scan count.

Two things are never swept, and both are the keep direction.

A snapshot a run is bound to. ``ReapRun.snapshot_id`` is ``ondelete="RESTRICT"``, so the
schema refuses to drop one anyway; this module excludes them up front rather than relying
on that failure, because a run's approval is bound to the exact rows it was planned against
(``services.planner``), the journal is the audit trail, and the rolling delete cap is
priced off those same candidate rows. They stay excluded whatever the run's state: a
finished run's record is what an operator goes back to read, and the cap looks thirty days
back through runs that finished long ago.

An operator's own decision. Hand spares and forced reaps live in ``WhitelistEntry``, keyed
by ``media_key`` because they are a decision about a file, not a property of the scan that
happened to surface it, and the first-flagged clock and the watch high-water mark are keyed
the same way. None of them is reachable from a snapshot, so sweeping one cannot lose an
operator decision or restart a grace window.

Preexisting installs are the case this is built for. An install that has been scanning for
months arrives here with a backlog to drain in one pass, so the delete is batched into
small transactions rather than one that would hold SQLite's write lock across millions of
rows while the UI waits behind it. SQLite also never returns freed pages to the filesystem
on its own, so an operator whose database is suddenly mostly dead space would see no disk
come back: :func:`compact_if_fragmented` closes that gap, and it runs rarely on purpose
(see its own note).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import structlog
from sqlalchemy import Select, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.config import DATABASE_FILENAME
from reaper.db.models import ReapRun, Snapshot

log = structlog.get_logger(__name__)

#: How many of the newest scans survive a sweep. Everything older goes, unless a run is
#: bound to it. A count, not a duration: thirty is about a month of nightly scanning, a day
#: and a half of hourly, and no time at all on an install scanned by hand, and it is far more
#: than the queue uses, which reads only the single newest. The database cost of raising
#: this scales linearly with it; see ``docs/LEARNINGS.md`` for measured sizes.
KEEP_SNAPSHOTS = 30

#: Snapshots deleted per transaction. Small on purpose: each one cascades to a whole
#: library's worth of candidate rows, and a backlog drained in a single statement would
#: hold the write lock past the 5s the UI and a scheduled scan wait behind it
#: (``db.session``).
SWEEP_BATCH = 10

#: Runaway guard on the drain loop, not a retention policy: 50,000 snapshots is far past
#: any real backlog (hourly scans for five years). Hitting it logs and stops; the next
#: firing continues where this one left off.
MAX_SWEEP_BATCHES = 5_000

#: Compaction thresholds. ``VACUUM`` rewrites the whole file under an exclusive lock and
#: needs room for a second copy while it runs, so it must not fire after every sweep: in the
#: steady state only one snapshot in thirty is freed, and those pages are reused by the next
#: scan anyway. It earns its lock only when a large share of a large file is dead, which in
#: practice means the first sweep after an install that had been keeping every scan since it
#: was installed upgrades to a version that trims them.
COMPACT_MIN_FREE_RATIO = 0.25
COMPACT_MIN_FREE_BYTES = 64 * 1024 * 1024


def _doomed(keep: int) -> Select[tuple[int]]:
    """Snapshot ids eligible to be swept, oldest first.

    Two exclusions, both erring toward keeping. ``NOT IN`` over an empty ``reap_run`` is
    true for every row, the right answer for an install that has never reaped. Over a NULL
    it would be unknown and delete nothing, which is the safe outcome and the only one that
    would matter if ``snapshot_id`` ever became nullable.
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
    empty the review queue: an empty selection must never expand to everything.
    """
    if keep < 1:
        raise ValueError(f"keep must be at least 1, got {keep}")

    removed = 0
    try:
        for _ in range(MAX_SWEEP_BATCHES):
            async with session_factory() as session:
                doomed = list(
                    (await session.execute(_doomed(keep).limit(SWEEP_BATCH))).scalars().all()
                )
                if not doomed:
                    return removed
                await session.execute(delete(Snapshot).where(Snapshot.id.in_(doomed)))
                await session.commit()
            removed += len(doomed)
    except Exception:
        # The batches already committed are kept, and the caller's log otherwise says only
        # that this firing failed. Without this count, a large upgrade drain reports the
        # same way whether it dropped none of them or all but the last, and the count is
        # what tells an operator whether to wait or step in.
        log.warning("retention.sweep_interrupted", removed=removed)
        raise

    log.warning("retention.sweep_backstop_hit", removed=removed, batches=MAX_SWEEP_BATCHES)
    return removed


def _compact_sync(db_path: Path) -> bool:
    """The blocking half of :func:`compact_if_fragmented`. Returns whether it vacuumed.

    ``isolation_level=None`` is required, not a style choice: ``VACUUM`` cannot run inside a
    transaction, and the driver's default opens one on the first statement it takes for
    DML. The 30s busy timeout this connection sets lets it wait out an in-flight write
    rather than failing at once. This timeout belongs to this connection alone, not the app
    engine's; ``services.backup`` sets its own for ``VACUUM INTO``.

    The checkpoint is what actually hands the disk back; ``VACUUM`` alone does not. The
    database is in WAL mode (``db.session``), so the rewrite lands in ``reaper.db-wal`` and
    the main file stays at its high-water mark while any other connection is open, which is
    always true, since the app's engine pools connections for the life of the process.
    Without the checkpoint below, the log would say compacted while the file on disk stayed
    the same size until the next container restart. The truncating checkpoint reclaims the
    freed space immediately, even with a reader open.

    A checkpoint that cannot finish reports a non-zero first element rather than raising.
    It is logged and not retried: the pages are already free, the next restart truncates
    the file on its own, and the sweep that matters has committed either way.
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
        blocked = int(con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0])
        if blocked:
            log.warning("retention.checkpoint_blocked")
        return True
    finally:
        con.close()


async def compact_if_fragmented(data_dir: Path) -> bool:
    """Return freed pages to the filesystem, but only when there are enough to matter.

    The caller must decide whether now is the moment. The rewrite holds the write lock for
    its whole duration, which can exceed the 5s every app connection waits
    (``db.session``) on a large enough database, so a scan or reap writing across it loses
    that write. ``scheduler.sweep_old_snapshots`` is the only caller and checks for a live
    scan or reap first; a second caller owes the same check, since nothing here can see them.

    Runs in a worker thread: rewriting a multi-hundred-megabyte file would otherwise stall
    the event loop for the whole vacuum, the same problem ``services.backup`` avoids for
    ``VACUUM INTO``. Raises whatever SQLite raises; a disk with no room for the second copy
    is the realistic failure, and the caller logs it and moves on, since a database that is
    merely larger than it needs to be still works. In WAL mode the peak disk usage is the
    main file plus the WAL plus the vacuum's temporary copy, so closer to three times the
    database size than two.
    """
    return await asyncio.to_thread(_compact_sync, data_dir / DATABASE_FILENAME)
