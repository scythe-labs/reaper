# SPDX-License-Identifier: AGPL-3.0-or-later
"""A local mirror of Tautulli's watch history.

Reaper needs to ask questions Tautulli's API cannot answer in one call: *"as of a
year ago, who had watched this, and how long had it sat untouched?"* Answering those over a
whole library needs the whole history in a form we can query, not paginate.

A mature Tautulli install holds hundreds of thousands of rows. The first pull walks all
of them, once, in large pages. After that it is genuinely incremental: it asks Tautulli
for only the rows since the newest one we hold, using ``get_history``'s ``after`` filter,
which turns a per-scan sync from minutes into seconds. (Measured: ``after`` a day ago
returned a couple hundred rows against a six-figure history.)

Two facts about Tautulli's API shape this, both verified live and neither obvious:

* **You cannot sort ``get_history`` by insertion id.** ``order_column=id`` is ignored;
  the only reliable order is newest-first by *watched date*. So incremental sync filters
  by date (``after``), not by a stable row cursor.
* **Date-filtering can miss a backfilled old event.** If Tautulli imports history, or
  Plex reports a play with an old timestamp, that row lands with an *old* date and an
  ``after=<recent>`` sync skips it. So a **nightly full sweep** (``full=True``, run by
  the scheduler) re-walks everything and catches any backfill. Per-scan syncs stay fast;
  correctness is restored within a day.

Rows are written with ``INSERT OR REPLACE`` keyed on the stable ``row_id``, so re-fetching
the overlap window (or a whole full sweep) is idempotent and never duplicates.

## The horizon

The oldest row we hold is Reaper's **data horizon**, and it is load-bearing.
Tautulli cannot import Plex history from before it was installed, so *everything*
watched before that point looks never-watched. On a server that has run Tautulli for
years the horizon is deep and this barely matters -- but on a fresh install it is
*yesterday*, and a naive tool would conclude the entire library is abandoned and
delete all of it. The horizon is therefore recorded on every snapshot and enforced by
a gate, not left as an assumption.

This is the single largest mass-deletion vector in the ecosystem, and it is why the
horizon is stored, not computed on the fly.

## Regression detection

If Tautulli's history *shrinks*, someone has reset, pruned or restored its database. Our
evidence just changed underneath us, and any "never watched" verdict is suspect.

The check compares **Tautulli's own reported total** (``recordsTotal``, which the API
returns on every call) against the total we recorded on the previous sync. It cannot be
based on our local mirror's row count, because we write with ``INSERT OR REPLACE`` and
never delete -- so our mirror only ever grows, and a mirror-based check can never fire.
(It did not, before: the guard was a silent no-op.) Storing Tautulli's total each sync
is what makes the guard real.
"""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clients.tautulli import TautulliClient
from reaper.clock import from_epoch, utcnow

#: How far Tautulli's total may fall between syncs before we treat it as a reset/prune.
#: A little slack, because grouping can nudge the reported count by a row or two.
REGRESSION_THRESHOLD = 0.95

#: On an incremental sync, re-ask for two days before our newest row. ``after`` is
#: date-granular and Tautulli's exact date-boundary semantics (inclusive or exclusive,
#: whose midnight) are unverified, so the overlap is generous rather than minimal: it
#: keeps a play recorded late on our newest day from being skipped, and INSERT OR
#: REPLACE makes the re-fetch free.
INCREMENTAL_OVERLAP = timedelta(days=2)

log = structlog.get_logger(__name__)

PAGE_SIZE = 25_000

#: Hard stop on the history paging loop, for a source that serves page after page without
#: ever reporting a total. At PAGE_SIZE rows a page this is far past any real library's
#: history, so it never truncates a genuine sync -- it only stops a runaway one.
MAX_HISTORY_PAGES = 1_000
"""Tautulli serves a 25k page in about the same time as a 1k page, so large pages
are strictly better: 17 requests instead of 422."""


class HistoryRegressionError(RuntimeError):
    """Tautulli's reported history total shrank sharply between syncs.

    Someone reset, pruned or restored the database. Every "never watched" verdict we
    would now produce is suspect, so the sync stops before writing and no run proceeds
    until a human looks. Our existing mirror is preserved -- stopping loses nothing.
    """


@dataclass(frozen=True)
class HistoryState:
    rows: int
    earliest: datetime | None
    latest: datetime | None

    @property
    def horizon(self) -> datetime | None:
        """The data horizon. Nothing before this can be judged."""
        return self.earliest


SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_event (
    row_id                 INTEGER PRIMARY KEY,
    rating_key             INTEGER NOT NULL,
    parent_rating_key      INTEGER,
    grandparent_rating_key INTEGER,
    user_id                INTEGER NOT NULL,
    watched_at             INTEGER NOT NULL,
    -- How much of the item was played, 0.0 to 1.0. NULL means Tautulli did not say,
    -- which is NOT the same as 0.0 ("started it, did not finish"): the sequential guard
    -- reads a completed episode as watched_status = 1, so an unrecorded status that was
    -- silently stored as 0.0 makes a viewer look further behind than they are, and the
    -- season they are about to watch loses its protection. See _progress_for_user.
    watched_status         REAL,
    percent_complete       INTEGER NOT NULL,
    media_type             TEXT    NOT NULL,
    -- The episode number (Tautulli media_index) for TV rows, NULL for movies and for rows
    -- synced before this column existed. Powers episode-precise mid-binge protection.
    media_index            INTEGER
);

-- Tautulli's own reported total at each sync. The regression check compares against
-- this, not our mirror's count (which never shrinks -- see the module docstring).
CREATE TABLE IF NOT EXISTS history_sync_state (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    tautulli_total INTEGER NOT NULL,
    synced_at      INTEGER NOT NULL
);
"""

#: Every index on ``watch_event``, by name. Kept OUT of ``SCHEMA`` on purpose: an index can
#: be added to a cache that already holds rows, where a column change cannot. The shape
#: check below compares COLUMNS, so an index added to ``SCHEMA`` would look current on
#: every existing install and simply never be created -- and the only way to force it would
#: be to bump the column tuple, which drops the whole mirror and costs a full re-sync from
#: Tautulli to land an index. ``ensure_schema`` instead notices a missing index by name and
#: creates it in place, leaving the mirrored rows alone.
#:
#: Every column a fairness query filters on wants one here: the board and the person drawer
#: run ``WHERE ... IN (:keys)`` per 500-key chunk, and an unindexed column turns each chunk
#: into a full scan of a table that holds every play the server has ever recorded (P-4).
INDEXES = {
    "ix_watch_event_rating_key": (
        "CREATE INDEX IF NOT EXISTS ix_watch_event_rating_key ON watch_event "
        "(rating_key, watched_at)"
    ),
    "ix_watch_event_parent_key": (
        "CREATE INDEX IF NOT EXISTS ix_watch_event_parent_key ON watch_event "
        "(parent_rating_key, watched_at)"
    ),
    "ix_watch_event_gp_key": (
        "CREATE INDEX IF NOT EXISTS ix_watch_event_gp_key ON watch_event "
        "(grandparent_rating_key, watched_at)"
    ),
    "ix_watch_event_watched_at": (
        "CREATE INDEX IF NOT EXISTS ix_watch_event_watched_at ON watch_event (watched_at)"
    ),
}


async def _last_tautulli_total(engine: AsyncEngine) -> int | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT tautulli_total FROM history_sync_state WHERE id = 1"))
        ).first()
    return int(row.tautulli_total) if row else None


async def _store_tautulli_total(engine: AsyncEngine, total: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR REPLACE INTO history_sync_state (id, tautulli_total, synced_at) "
                "VALUES (1, :total, :ts)"
            ),
            {"total": total, "ts": int(utcnow().timestamp())},
        )


#: Every column ``SCHEMA`` declares on ``watch_event``, in order, as
#: ``(name, declared type, notnull)`` -- the first three fields ``PRAGMA table_info``
#: returns after ``cid``. Names alone are not enough: the change that made
#: ``watched_status`` nullable moved no name and no position, so a name-only comparison
#: would call an upgraded install's table current and leave the old ``0.0`` rows in place.
#: ``row_id INTEGER PRIMARY KEY`` is a rowid alias, which sqlite reports as ``notnull=0``.
#: Keep this and ``SCHEMA`` together. The fast path in ``ensure_schema`` only runs ``SCHEMA``
#: (and its companion ``history_sync_state`` table) when THIS tuple mismatches, so a new
#: table added to ``SCHEMA`` must also change this tuple (bump a column, add one) or existing
#: caches will never execute it. Indexes are the exception and live in ``INDEXES``, which
#: ``ensure_schema`` reconciles by name on every call -- adding one there needs no change
#: here and costs no rebuild.
_WATCH_EVENT_COLUMNS = (
    ("row_id", "INTEGER", 0),
    ("rating_key", "INTEGER", 1),
    ("parent_rating_key", "INTEGER", 0),
    ("grandparent_rating_key", "INTEGER", 0),
    ("user_id", "INTEGER", 1),
    ("watched_at", "INTEGER", 1),
    ("watched_status", "REAL", 0),
    ("percent_complete", "INTEGER", 1),
    ("media_type", "TEXT", 1),
    ("media_index", "INTEGER", 0),
)


#: One rebuild lock per event loop, created lazily so it always binds to the running loop.
#: A single module-level ``asyncio.Lock`` would bind to whichever loop first awaited it and
#: raise on every other -- and the test suite runs a fresh loop per test. Weak-keyed on the
#: loop so a closed loop's lock is collected. In production there is exactly one loop, hence
#: one lock, which is what serializes the rebuild across every concurrent caller in the
#: process (the scan, the fairness route, the nightly sync).
_rebuild_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _rebuild_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _rebuild_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _rebuild_locks[loop] = lock
    return lock


async def ensure_schema(engine: AsyncEngine) -> None:
    """Create ``watch_event`` if it is not there, and REBUILD it if its shape is stale.

    Called by ``sync`` and before every read. The cache database is rebuildable and can
    be deleted at any time -- so on a fresh install, or after someone clears the cache,
    reading it must find an empty table rather than crash with ``no such table``. A
    missing table should read as "no history yet" (which degrades the snapshot loudly),
    never as an opaque SQL error a hundred frames deep in the scan.

    **Cache tables are never migrated.** The Alembic baseline says so explicitly: they
    live here, are created by raw DDL, and are rebuildable by definition. So a table
    whose columns no longer match ``SCHEMA`` is dropped and recreated empty rather than
    patched into shape. Three reasons, and the last is the one that matters:

    * There is nothing to preserve. ``sync`` refetches from Tautulli, and an empty table
      makes the next sync a FULL one automatically, so the cache heals itself.
    * A migration is code that runs once, on a path no test exercises twice, against a
      shape nobody has locally. Pre-release that is pure risk for no gain.
    * Carried-over rows are exactly the untrustworthy ones. When ``watched_status``
      became nullable, every existing ``0.0`` was already ambiguous -- reported, or
      invented by the old ``or 0`` coercion, with no way to tell. Preserving them would
      have kept the bad data the shape change existed to fix.

    The cost is one full re-sync, and a scan in between degrades loudly rather than
    judging on a thin mirror.
    """
    # Common path: the table exists and its shape is current. Check that in a READ
    # connection -- no write lock, no fsync -- and return. ensure_schema runs several times
    # per scan (state, watch stats, the liveness read), and each one was opening a write
    # transaction on cache.db purely to re-run idempotent DDL that changed nothing.
    async with engine.connect() as conn:
        cols = (await conn.execute(text("PRAGMA table_info(watch_event)"))).all()
        live = tuple((row[1], str(row[2]).upper(), int(row[3])) for row in cols)
        shape_ok = bool(cols) and live == _WATCH_EVENT_COLUMNS
        # An index can be added to a cache that already holds rows, so a shape that is
        # otherwise current still gets checked for one that is missing -- that is how a new
        # entry in INDEXES reaches an install that synced before it existed, without
        # dropping the mirror. Reading sqlite_master costs one indexed lookup.
        missing = (
            set(INDEXES)
            - {
                str(row[0])
                for row in (
                    await conn.execute(
                        text(
                            "SELECT name FROM sqlite_master "
                            "WHERE type = 'index' AND tbl_name = 'watch_event'"
                        )
                    )
                ).all()
            }
            if shape_ok
            else set(INDEXES)
        )
    if shape_ok and not missing:
        return

    # Only here do we take the write transaction: the table is missing (fresh or cleared
    # cache -> create it, and the next sync becomes a full one automatically) or its shape
    # is stale (drop and recreate empty -- rebuild, never migrate; see the docstring).
    #
    # Serialize the whole rebuild behind a process-level asyncio.Lock and re-read the shape
    # INSIDE it, deciding from THAT, never from the pre-lock read above. The LOCK is the
    # mutual exclusion here, not any database write lock: pysqlite runs the PRAGMA and the
    # DROP/CREATE in autocommit with no BEGIN IMMEDIATE, so nothing at the SQLite level stops
    # two concurrent callers (the scan, the fairness route, the nightly sync -- all one
    # process) from both seeing "stale" and the second re-DROPping the table the first just
    # rebuilt (a redundant double rebuild; the nightly full sweep refills either way). Under
    # the lock the re-read is authoritative, and the pre-lock read only decides whether taking
    # the lock and the transaction is worth it at all.
    async with _rebuild_lock(), engine.begin() as conn:
        cols = (await conn.execute(text("PRAGMA table_info(watch_event)"))).all()
        live = tuple((row[1], str(row[2]).upper(), int(row[3])) for row in cols)
        if not cols or live != _WATCH_EVENT_COLUMNS:
            if cols:  # present but stale -> rebuild, never migrate (see the docstring)
                log.info("history.cache.rebuilding", reason="watch_event shape is stale")
                await conn.execute(text("DROP TABLE watch_event"))
            for statement in SCHEMA.strip().split(";"):
                if statement.strip():
                    await conn.execute(text(statement))
        # Always, and last: after a rebuild the table has no indexes at all, and on a
        # current table this is what back-fills one added since the install last synced.
        # Every statement is IF NOT EXISTS, so re-running them costs nothing. Named in a
        # stable order so two callers racing here issue the same statements.
        for name in sorted(INDEXES):
            await conn.execute(text(INDEXES[name]))


async def _state(engine: AsyncEngine) -> HistoryState:
    await ensure_schema(engine)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) AS n, MIN(watched_at) AS lo, MAX(watched_at) AS hi "
                    "FROM watch_event"
                )
            )
        ).one()
    return HistoryState(
        rows=int(row.n or 0),
        earliest=from_epoch(row.lo),
        latest=from_epoch(row.hi),
    )


async def sync(
    engine: AsyncEngine,
    client: TautulliClient,
    *,
    full: bool = False,
) -> HistoryState:
    """Pull history into the local mirror.

    Incremental by default -- fetches only rows since our newest, via Tautulli's
    ``after`` filter, which is fast. Pass ``full=True`` for the nightly sweep that
    re-walks everything and catches any backfilled old events (see the module docstring).
    A fresh, empty mirror always does a full walk regardless of ``full``.
    """
    await ensure_schema(engine)

    before = await _state(engine)

    # Regression check FIRST, against Tautulli's own reported total -- before we write
    # anything. If the source shrank, we stop rather than judge against changed evidence.
    await _check_regression(engine, client)

    # Incremental only when we already hold history AND know its newest instant. Filter by
    # date with INCREMENTAL_OVERLAP (two days) of overlap: `after` is date-granular and
    # Tautulli's exact boundary semantics are unverified, so re-asking for our newest days
    # guarantees no gap, and INSERT OR REPLACE makes the overlap free.
    after: str | None = None
    if not full and before.rows and before.latest is not None:
        after = (before.latest - INCREMENTAL_OVERLAP).strftime("%Y-%m-%d")

    inserted = 0
    start = 0
    total: int | None = None
    skipped_live = 0
    skipped_malformed = 0

    pages = 0
    while True:
        page = await client.history(length=PAGE_SIZE, start=start, after=after)
        pages += 1
        rows = page.get("data") or []
        if total is None:
            # recordsFiltered reflects the `after` filter; on an incremental sync it is
            # the small delta, on a full sync it is the whole history.
            total = int(page.get("recordsFiltered") or 0)
        if not rows:
            break

        batch = []
        for row in rows:
            row_id = row.get("row_id")
            # row_id is null only for live/in-progress sessions (verified against a real
            # instance); those are not history yet, so skipping them is correct.
            if row_id is None:
                skipped_live += 1
                continue
            watched_at = from_epoch(row.get("date") or row.get("started"))
            user_id = row.get("user_id")
            rating_key = row.get("rating_key")
            if watched_at is None or user_id is None or rating_key is None:
                skipped_malformed += 1
                continue

            batch.append(
                {
                    "row_id": int(row_id),
                    "rating_key": int(rating_key),
                    "parent_rating_key": _int_or_none(row.get("parent_rating_key")),
                    "grandparent_rating_key": _int_or_none(row.get("grandparent_rating_key")),
                    "user_id": int(user_id),
                    "watched_at": int(watched_at.timestamp()),
                    # NULL, not 0.0, when Tautulli omitted it. `or 0` collapsed "not
                    # watched" and "we were not told" into the same number, and once
                    # written the two are indistinguishable forever. See the column note.
                    "watched_status": _float_or_none(row.get("watched_status")),
                    "percent_complete": int(row.get("percent_complete") or 0),
                    "media_type": str(row.get("media_type") or "unknown"),
                    # Episode number for TV rows; None for movies. Fail-safe: a NULL here
                    # leaves that season "position unknown" and the guard falls back to the
                    # season-level protection, never under-protecting.
                    "media_index": _int_or_none(row.get("media_index")),
                }
            )

        if batch:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT OR REPLACE INTO watch_event "
                        "(row_id, rating_key, parent_rating_key, grandparent_rating_key, "
                        " user_id, watched_at, watched_status, percent_complete, media_type, "
                        " media_index) "
                        "VALUES (:row_id, :rating_key, :parent_rating_key, "
                        " :grandparent_rating_key, :user_id, :watched_at, :watched_status, "
                        " :percent_complete, :media_type, :media_index)"
                    ),
                    batch,
                )
            inserted += len(batch)

        # Advance by what the page actually held, never by the constant. A middle page
        # shorter than PAGE_SIZE used to move `start` past rows nobody had fetched, and
        # a truncated mirror reads as a shallow horizon -- which is the single largest
        # mass-deletion vector this codebase has (rule 56).
        start += len(rows)
        log.info("history.page", fetched=start, of=total, inserted=inserted)
        # `total` is trusted only when it is actually there. `int(... or 0)` makes a
        # Tautulli that omits recordsFiltered indistinguishable from one reporting an
        # empty history, and 0 ended the loop after page one. Falsy means "we were not
        # told how many": keep paging until a page comes back empty.
        if total and start >= total:
            break
        if pages >= MAX_HISTORY_PAGES:
            # Only reachable if the source keeps serving non-empty pages without ever
            # reporting a total. Stop rather than spin; the horizon gate treats the
            # shorter mirror as less evidence, which keeps files rather than deleting.
            log.warning("history.page_cap_reached", pages=pages, fetched=start)
            break

    after_state = await _state(engine)
    log.info(
        "history.synced",
        rows=after_state.rows,
        inserted=inserted,
        incremental=after is not None,
        horizon=after_state.earliest.date().isoformat() if after_state.earliest else None,
    )
    # Malformed rows (missing timestamp, user, or rating key) are dropped plays: they
    # overstate dormancy and push items toward condemnation, so a nonzero count is a
    # data-quality signal worth a warning. Live/in-progress skips are expected and stay
    # at debug.
    if skipped_malformed:
        log.warning("history.rows_malformed", malformed=skipped_malformed, live=skipped_live)
    elif skipped_live:
        log.debug("history.rows_skipped", live=skipped_live)
    return after_state


async def _check_regression(engine: AsyncEngine, client: TautulliClient) -> None:
    """Detect a Tautulli reset/prune, and record the new total.

    Compares Tautulli's own reported total against the total we saw last time. A
    significant drop means the source shrank -- reset, pruned or restored -- and our
    "never watched" evidence just changed underneath us. We raise before writing anything.

    On the very first sync there is no prior total, so there is nothing to compare; we
    just record it. This deliberately does NOT compare our mirror's row count, which
    never shrinks (INSERT OR REPLACE, no deletes) and so could never detect a regression.
    """
    page = await client.history(length=1, start=0)
    current_total = int(page.get("recordsTotal") or page.get("recordsFiltered") or 0)

    previous_total = await _last_tautulli_total(engine)
    if previous_total is not None and current_total < previous_total * REGRESSION_THRESHOLD:
        # A safety halt: this stops all judgment. HistoryRegressionError is a RuntimeError,
        # not an IntegrationError, so the scan's `except IntegrationError` does not catch it
        # and the whole scan aborts on the exception string alone. Warn here so the reason
        # is in the structured log regardless of how the top-level handler renders it.
        log.warning(
            "history.regression_detected",
            previous_total=previous_total,
            current_total=current_total,
        )
        raise HistoryRegressionError(
            f"Tautulli's history shrank from {previous_total:,} rows to {current_total:,}. "
            "Someone has reset, pruned or restored its database. Reaper will not judge "
            "anything as unwatched until this is explained. The mirror Reaper already "
            "holds is preserved, so nothing is lost by stopping here."
        )

    await _store_tautulli_total(engine, current_total)
    if previous_total is None:
        log.info("history.regression_baseline", tautulli_total=current_total)


def _float_or_none(value: object) -> float | None:
    """A completion fraction Tautulli actually sent, or ``None``.

    Deliberately NOT ``float(value or 0)``: a genuine 0.0 ("started it, did not finish")
    and a missing field are different facts, and the sequential guard acts on the
    difference. Once collapsed they cannot be separated again.
    """
    if not isinstance(value, int | float | str) or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    if not isinstance(value, int | float | str) or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def state(engine: AsyncEngine) -> HistoryState:
    return await _state(engine)


async def horizon(engine: AsyncEngine) -> datetime | None:
    return (await _state(engine)).earliest


async def last_synced_at(engine: AsyncEngine) -> datetime | None:
    """When the ingest last ran, or ``None`` if it never has.

    The liveness signal ``HistoryState.latest`` is not. ``history_sync_state.synced_at`` is
    written by :func:`_store_tautulli_total` whenever Tautulli answered and its history had
    not shrunk, so this moves on a quiet library while ``MAX(watched_at)`` stands still.
    A stalled ingest and a quiet library share the same newest event, so degrading a scan on
    that reads users who went away for the weekend as a broken pipeline. This is what the
    scan's staleness guard degrades on instead (``services.snapshot``).

    Precise about what it marks: ``_store_tautulli_total`` is called from
    ``_check_regression``, which runs *before* the page walk, so this says "the source
    answered", not "the walk finished". That is the right signal for the guard, which is
    distinguishing a reachable source from an unreachable one.
    """
    await ensure_schema(engine)
    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT synced_at FROM history_sync_state WHERE id = 1"))
        ).first()
    return from_epoch(row.synced_at) if row else None
