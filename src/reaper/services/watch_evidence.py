# SPDX-License-Identifier: AGPL-3.0-or-later
"""Has this item's watch history gone unreadable since we last looked?

A Plex rating key is not stable. Let a file go away and come back and Plex issues a NEW
one, while Tautulli keeps every earlier play filed under the OLD one. That is established
from Tautulli's source, not assumed: the only code that rewrites a historical rating key is
``datafactory.update_metadata_details``, reached from the "Fix Metadata" button or the API
command of the same name, and no scheduled task, websocket handler or library refresh calls
it. Tautulli's own maintainer ships a standalone script for the same repair. So a re-added
file's earlier plays stay where they are, indefinitely.

Reaper reads the mirror by the key the item carries *now*. It finds nothing and reports
``Known(0)`` watchers with maximum dormancy: an affirmative "nobody ever watched this"
about a title somebody watched, which is pressure in the condemn direction on exactly the
item that deserves it least. The read path cannot tell the two apart, because a churned key
and a never-watched item produce the identical answer -- no rows.

**The invariant that can tell them apart.** All-time watch evidence only ever grows. The
mirror is written ``INSERT OR REPLACE`` and never deletes, and a Tautulli reset or prune is
caught separately by ``history_sync._check_regression``, which degrades the whole scan. So
an item whose all-time watcher count *falls to zero*, or whose last play moves *earlier in
time*, has undergone a transition no library can perform. Something changed underneath the
read, and the honest answer is ``Unknown`` -- which blocks the gate and takes the full keep
discount (rule 93) -- not a measured zero.

**Why the mark outlives the snapshots.** Comparing against the previous snapshot alone
would let the first blind scan write zero as the new baseline; after that 0 -> 0 is not a
fall and nothing ever notices again. ``WatchHighWater`` is therefore keyed on the durable
``media_key`` and only ever raised.

**What this deliberately does NOT flag.** A never-watched item reads zero on every scan,
and 0 -> 0 is not a fall, so it never fires and stays condemnable -- which is the entire
point of the feature and the reason this check is safe to run library-wide. A count that
merely *drops* (five watchers to three) is not flagged either: it does not cross the
never-watched boundary that drives the condemn lane, and the last-played check below
catches the case where such a drop also inflates dormancy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.db.models import WatchHighWater

log = structlog.get_logger(__name__)

#: Rows per upsert in :func:`record`, matching ``record_first_flagged_bulk`` and
#: ``snapshot._WATCH_KEY_CHUNK``. Each row binds four variables, so a library-sized write in
#: one statement overflows SQLite's variable ceiling and aborts the scan (rule 94). The read
#: side needs no companion constant: :func:`recall_all` takes the whole table and binds
#: nothing.
_CHUNK = 500

#: What the operator is told when the check fires. One string for both branches, because
#: they mean one thing: we measured plays for this title before and cannot see them now.
#: Says the outcome, names no internal machinery (rule 21).
BLIND_REASON = "plays recorded on an earlier scan are no longer readable"


@dataclass(frozen=True)
class Reading:
    """What one scan measured for one item, before it is judged."""

    watchers_all_time: int
    last_played_at: datetime | None


@dataclass(frozen=True)
class Mark:
    """The most watch evidence ever measured for one item."""

    watchers_all_time: int
    last_played_at: datetime | None


def went_blind(mark: Mark | None, reading: Reading) -> str | None:
    """Why this item's watch history is unreadable, or ``None`` if it reads honestly.

    Call only on an item that matched a Plex item this scan. An unmatched item already
    takes ``Unknown`` for these facts from its own branch, and has no reading to compare.
    """
    if mark is None:
        # Never measured before, so there is nothing this could have fallen from. A brand
        # new item -- and every item on the first scan after this ships -- lands here.
        return None
    if mark.watchers_all_time > 0 and reading.watchers_all_time <= 0:
        return BLIND_REASON
    if mark.last_played_at is not None and (
        reading.last_played_at is None or reading.last_played_at < mark.last_played_at
    ):
        # A play cannot un-happen, so the latest one cannot move backwards. This catches
        # the partial case the counter misses: some plays still visible under the current
        # key, but the most recent one was under a key that moved, so dormancy inflates.
        return BLIND_REASON
    return None


async def recall_all(session: AsyncSession) -> dict[str, Mark]:
    """Every stored mark, keyed by ``media_key``. Keys with no mark are simply absent.

    The whole table rather than a filtered read, for one reason: the TV lane needs its
    marks *before* it has computed a single season ``media_key`` (they are derived from
    Sonarr coordinates inside ``season_scan.gather``, which runs as a concurrent task and
    holds no session), so there is nothing to filter on yet. One row per item ever seen
    makes this library-sized, and unlike a filtered read it binds no variables at all, so
    it cannot meet SQLite's ceiling (rule 94).
    """
    rows = (await session.execute(select(WatchHighWater))).scalars()
    return {
        row.media_key: Mark(
            watchers_all_time=int(row.watchers_all_time),
            last_played_at=row.last_played_at,
        )
        for row in rows
    }


async def record(session: AsyncSession, readings: Mapping[str, Reading], *, now: datetime) -> None:
    """Raise each item's mark to cover this scan's reading. Never lowers one.

    The raise is expressed in SQL rather than computed here and written back, so two
    writers cannot read the same mark and have the lower one win: ``ON CONFLICT`` re-reads
    the stored row inside the write (rule 58).

    A reading the check just flagged as blind is passed in like any other. That is
    deliberate -- it cannot lower the mark, so a blind scan leaves the mark standing and
    the next scan asks the same question against the same evidence.
    """
    rows = [
        {
            "media_key": key,
            "watchers_all_time": max(0, reading.watchers_all_time),
            "last_played_at": reading.last_played_at,
            "updated_at": now,
        }
        for key, reading in readings.items()
    ]
    if not rows:
        return
    for start in range(0, len(rows), _CHUNK):
        chunk = rows[start : start + _CHUNK]
        stmt = sqlite_insert(WatchHighWater).values(chunk)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[WatchHighWater.media_key],
                set_={
                    "watchers_all_time": func.max(
                        stmt.excluded.watchers_all_time, WatchHighWater.watchers_all_time
                    ),
                    # SQLite's scalar max() returns NULL if any argument is NULL, which
                    # would erase a real mark the first time an item read blind. So the
                    # NULL cases are handled before max() ever sees them.
                    "last_played_at": case(
                        (
                            WatchHighWater.last_played_at.is_(None),
                            stmt.excluded.last_played_at,
                        ),
                        (
                            stmt.excluded.last_played_at.is_(None),
                            WatchHighWater.last_played_at,
                        ),
                        else_=func.max(stmt.excluded.last_played_at, WatchHighWater.last_played_at),
                    ),
                    "updated_at": stmt.excluded.updated_at,
                },
            )
        )


async def forget_all(session: AsyncSession) -> int:
    """Drop every mark, and report how many went. The operator's "start fresh".

    Needed because the marks are evidence about a library that may no longer exist. Rebuild
    a Plex library without repairing Tautulli's history and every item reads zero, so every
    item that was ever watched fires the check at once: correct, but it leaves nothing
    reapable and no amount of re-scanning clears it. Forgetting the marks accepts the new
    library as the baseline.
    """
    # CursorResult carries rowcount; the Result the stubs infer for execute() does not.
    result = await session.execute(delete(WatchHighWater))
    gone = int(getattr(result, "rowcount", 0) or 0)
    log.info("watch_evidence.forgotten", marks=gone)
    return gone


def reading_for(
    rating_key: int | None,
    watchers_all_time: Mapping[int, int],
    last_played: Mapping[int, datetime],
) -> Reading | None:
    """This scan's reading for one item, or ``None`` if it was never looked up.

    An unmatched item has no rating key, so it has no reading and must not be recorded as
    zero: that would write a mark of zero which the moment the item DOES match and its real
    plays appear reads as a rise, and -- worse -- an item whose match is merely flaky would
    have its true mark held down to zero and never fire the check at all.
    """
    if rating_key is None:
        return None
    return Reading(
        watchers_all_time=int(watchers_all_time.get(rating_key, 0)),
        last_played_at=last_played.get(rating_key),
    )
