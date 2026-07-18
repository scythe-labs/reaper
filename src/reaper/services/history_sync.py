# SPDX-License-Identifier: AGPL-3.0-or-later
"""A local mirror of Tautulli's watch history.

Reaper needs to ask questions Tautulli's API cannot answer in one call: *"as of a
year ago, who had watched this, and how long had it sat untouched?"* -- and then,
*"who watched it afterwards?"* That is the backtest, and it needs the whole history
in a form we can query, not paginate.

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
the overlap day (or a whole full sweep) is idempotent and never duplicates.

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
    watched_status         REAL    NOT NULL,
    percent_complete       INTEGER NOT NULL,
    media_type             TEXT    NOT NULL,
    -- The episode number (Tautulli media_index) for TV rows, NULL for movies and for rows
    -- synced before this column existed. Powers episode-precise mid-binge protection.
    media_index            INTEGER
);
CREATE INDEX IF NOT EXISTS ix_watch_event_rating_key ON watch_event (rating_key, watched_at);
CREATE INDEX IF NOT EXISTS ix_watch_event_gp_key
    ON watch_event (grandparent_rating_key, watched_at);
CREATE INDEX IF NOT EXISTS ix_watch_event_watched_at ON watch_event (watched_at);

-- Tautulli's own reported total at each sync. The regression check compares against
-- this, not our mirror's count (which never shrinks -- see the module docstring).
CREATE TABLE IF NOT EXISTS history_sync_state (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    tautulli_total INTEGER NOT NULL,
    synced_at      INTEGER NOT NULL
);
"""


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


async def ensure_schema(engine: AsyncEngine) -> None:
    """Create the ``watch_event`` table if it is not there yet.

    Called by ``sync`` and before every read. The cache database is rebuildable and can
    be deleted at any time -- so on a fresh install, or after someone clears the cache,
    reading it must find an empty table rather than crash with ``no such table``. A
    missing table should read as "no history yet" (which degrades the snapshot loudly),
    never as an opaque SQL error a hundred frames deep in the scan.
    """
    async with engine.begin() as conn:
        for statement in SCHEMA.strip().split(";"):
            if statement.strip():
                await conn.execute(text(statement))
        # CREATE TABLE IF NOT EXISTS never alters an existing table, so add media_index
        # explicitly on installs whose watch_event predates it. Idempotent -- guarded by the
        # column check, and the nightly full sweep backfills the values within a day.
        cols = (await conn.execute(text("PRAGMA table_info(watch_event)"))).all()
        if "media_index" not in {row[1] for row in cols}:
            await conn.execute(text("ALTER TABLE watch_event ADD COLUMN media_index INTEGER"))


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

    # Incremental only when we already hold history AND know its newest instant. Filter
    # by date with a day of overlap: `after` is date-granular, so re-asking for our
    # newest day guarantees no gap, and INSERT OR REPLACE makes the overlap free.
    after: str | None = None
    if not full and before.rows and before.latest is not None:
        after = (before.latest - INCREMENTAL_OVERLAP).strftime("%Y-%m-%d")

    inserted = 0
    start = 0
    total: int | None = None

    while True:
        page = await client.history(length=PAGE_SIZE, start=start, after=after)
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
                continue
            watched_at = from_epoch(row.get("date") or row.get("started"))
            user_id = row.get("user_id")
            rating_key = row.get("rating_key")
            if watched_at is None or user_id is None or rating_key is None:
                continue

            batch.append(
                {
                    "row_id": int(row_id),
                    "rating_key": int(rating_key),
                    "parent_rating_key": _int_or_none(row.get("parent_rating_key")),
                    "grandparent_rating_key": _int_or_none(row.get("grandparent_rating_key")),
                    "user_id": int(user_id),
                    "watched_at": int(watched_at.timestamp()),
                    "watched_status": float(row.get("watched_status") or 0),
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

        start += PAGE_SIZE
        log.info("history.page", fetched=start, of=total, inserted=inserted)
        if total is not None and start >= total:
            break

    after_state = await _state(engine)
    log.info(
        "history.synced",
        rows=after_state.rows,
        inserted=inserted,
        incremental=after is not None,
        horizon=after_state.earliest.date().isoformat() if after_state.earliest else None,
    )
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
        raise HistoryRegressionError(
            f"Tautulli's history shrank from {previous_total:,} rows to {current_total:,}. "
            "Someone has reset, pruned or restored its database. Reaper will not judge "
            "anything as unwatched until this is explained. The mirror Reaper already "
            "holds is preserved, so nothing is lost by stopping here."
        )

    await _store_tautulli_total(engine, current_total)
    if previous_total is None:
        log.info("history.regression_baseline", tautulli_total=current_total)


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


async def days_since_horizon(engine: AsyncEngine) -> float | None:
    earliest = await horizon(engine)
    if earliest is None:
        return None
    return (utcnow() - earliest).total_seconds() / 86_400
