# SPDX-License-Identifier: AGPL-3.0-or-later
"""The grace period: the countdown between "condemned" and "actually gone".

A condemned item sits in a grace window of ``grace_days``, during which it can be spared by
hand, rescued by anyone watching it, or simply reconsidered when the next scan re-judges it.

**The window is a notice, not a gate.** Nothing on the deletion path reads it: neither
``services.planner`` nor ``services.executor`` imports this module, and ``leaving_soon`` is
its only consumer. An unexpired window does not stop a send. What actually keeps a file at
send time is the executor's own live interlocks, re-checked per item
(``Executor._being_watched_now`` and ``Executor._watched_since_approval``, both called in
``_one_delete``), plus the manual spare and the fact that every deletion is started by hand.
Treat the countdown as the owner's chance to catch an item, never as a lock that holds it:
do not write code or operator copy that treats an unexpired window as protection.

This module computes where each currently-condemned item sits in that window. There is no
new state to store. The clock is ``FirstFlagged.first_flagged_at``, set once and never
moved while an item stays condemned, so a transient outage cannot reset it. The window
length is the owner's ``grace_days``. Grace status is derived, not tracked, so there is one
source of truth and nothing to drift.

It deletes nothing. Canceling a grace is spelled "spare it" (the manual whitelist). Rescue
happens naturally: a play resets dormancy, and the next scan no longer condemns the item.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import batched

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clock import utcnow
from reaper.db import KEY_CHUNK
from reaper.db.models import FirstFlagged, Snapshot
from reaper.services import whitelist
from reaper.services.condemned import effective_condemned

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class GraceItem:
    media_key: str
    candidate_id: int
    """The snapshot row this countdown is about, so the UI can open its reasoning."""
    plex_rating_key: int | None
    title: str
    media_type: str
    """"movie" or "season". Carried so movie-scoped features (the Leaving Soon label,
    which lives in the movie library) can exclude non-movie items rather than write a
    season's rating key into a movie section."""
    size_bytes: int | None
    """None when nothing would report a size. The countdown still runs and the item still
    shows in the list, but its size stays out of every total. The planner holds it back
    from a run unless the operator's unmeasured allowance admits it
    (``planner.build_plan``, ``executor._may_send_unmeasured``)."""

    first_flagged_at: datetime
    grace_ends_at: datetime
    days_remaining: int
    """Whole days until the window closes. 0 once it has closed, never negative: once the
    answer is "none", "how much longer" stops being a useful question."""
    in_grace: bool


@dataclass(frozen=True)
class GraceReport:
    grace_days: int
    in_grace: list[GraceItem]
    """Still counting down, soonest to clear first. Already plannable and deletable: the
    countdown is what the owner sees, not a hold on the file (see the module docstring).
    A spare ends it at any point, before or after it runs out."""
    ready: list[GraceItem]
    """The countdown has run out. The planner treats this list and ``in_grace`` the same
    way, so the split says who has had their notice, not what has unlocked."""
    total_bytes_in_grace: int
    """A sum of what is known. An item the *arr could not size is left out, rather than
    counted as zero, so a total beside an unmeasured item reads low by that item."""

    total_bytes_ready: int


async def grace_report(
    session: AsyncSession, *, grace_days: int, now: datetime | None = None
) -> GraceReport:
    """Where every currently-condemned item sits in its grace window.

    Grace is a property of the latest snapshot's effective condemned set
    (``services.condemned``). An item no longer condemned, whether rescued, spared, or
    re-judged, has left grace. A hand-reaped item enters grace the moment the owner
    clicks.
    """
    now = now or utcnow()
    window = timedelta(days=grace_days)

    latest = (
        await session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1))
    ).scalar_one_or_none()
    if latest is None:
        return GraceReport(grace_days, [], [], 0, 0)

    # The effective set, not the frozen one: a spare leaves the countdown at once instead
    # of lingering until the next scan, and a hand reap enters it at once. The owner's own
    # decision starts the same grace window and Leaving Soon warning a scan condemn gets.
    decisions = await whitelist.overrides(session)
    condemned = list((await effective_condemned(session, latest.id, decisions)).values())
    # One statement per KEY_CHUNK keys, never one holding the whole condemned set. The
    # expanding IN binds one variable per key, and SQLite refuses a statement that binds
    # more than it allows. Chunks are disjoint keys merged into one map by media_key, so
    # the merge is exact. Nothing below reads this in row order; the two output lists are
    # sorted explicitly.
    flagged: dict[str, datetime] = {}
    for chunk in batched(sorted(c.media_key for c in condemned), KEY_CHUNK, strict=False):
        rows = (
            await session.execute(select(FirstFlagged).where(FirstFlagged.media_key.in_(chunk)))
        ).scalars()
        flagged.update({f.media_key: f.first_flagged_at for f in rows})

    in_grace: list[GraceItem] = []
    ready: list[GraceItem] = []
    for candidate in condemned:
        # The scan writes a clock row on every condemn, so one should never be missing. If
        # it is, treat the item as having just entered grace: the safe reading, since it
        # keeps the file longer, not shorter.
        started = flagged.get(candidate.media_key)
        if started is None:
            # A condemn was written without its grace clock, a real integrity bug, so this
            # item's countdown is now a guess. Surface it.
            log.warning(
                "grace.missing_clock",
                media_key=candidate.media_key,
                candidate_id=candidate.id,
            )
            started = now
        ends = started + window
        remaining = max(0, (ends - now).days)
        item = GraceItem(
            media_key=candidate.media_key,
            candidate_id=candidate.id,
            plex_rating_key=candidate.plex_rating_key,
            title=candidate.title,
            media_type=candidate.media_type,
            size_bytes=candidate.size_bytes,
            first_flagged_at=started,
            grace_ends_at=ends,
            days_remaining=remaining,
            in_grace=now < ends,
        )
        (in_grace if item.in_grace else ready).append(item)

    in_grace.sort(key=lambda i: i.grace_ends_at)  # soonest to clear first
    # Biggest reclaim first. Unmeasured items sort last instead of being treated as 0
    # bytes and buried in the middle by accident; they sit together at the end, past
    # where a reader scanning for the big wins is already done looking.
    ready.sort(key=lambda i: (i.size_bytes is None, -(i.size_bytes or 0)))
    return GraceReport(
        grace_days=grace_days,
        in_grace=in_grace,
        ready=ready,
        total_bytes_in_grace=sum(i.size_bytes for i in in_grace if i.size_bytes is not None),
        total_bytes_ready=sum(i.size_bytes for i in ready if i.size_bytes is not None),
    )
