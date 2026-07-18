# SPDX-License-Identifier: AGPL-3.0-or-later
"""The grace period: the countdown between "condemned" and "actually gone".

Reaper never deletes the instant it condemns something. A condemned item sits in a grace
window of ``grace_days`` first, during which it can be cancelled by hand, rescued by
anyone watching it, or simply reconsidered when the next scan re-judges it. Only after
the window closes is it eligible for a real deletion -- and that step is still supervised
and, for now, deliberately unbuilt.

This module computes where each currently-condemned item sits in that window. There is no
new state to store: the clock is ``FirstFlagged.first_flagged_at`` (set once, never moved
while an item stays condemned, precisely so a transient outage cannot reset it), and the
window length is the owner's ``grace_days``. Grace status is therefore *derived*, not
tracked -- one source of truth, nothing to drift.

It deletes nothing. Cancelling a grace is spelt "spare it" (the manual whitelist); rescue
happens naturally, because a play resets dormancy and the next scan no longer condemns it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clock import utcnow
from reaper.db.models import FirstFlagged, Snapshot
from reaper.services import whitelist
from reaper.services.condemned import effective_condemned


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
    size_bytes: int
    first_flagged_at: datetime
    grace_ends_at: datetime
    days_remaining: int
    """Whole days until the window closes. 0 once it has closed -- never negative, because
    "how much longer" stops being a useful question the moment the answer is 'none'."""
    in_grace: bool


@dataclass(frozen=True)
class GraceReport:
    grace_days: int
    in_grace: list[GraceItem]
    """Still counting down, soonest to clear first -- the ones a cancel would catch."""
    ready: list[GraceItem]
    """The window has closed. Eligible for the (deferred, supervised) reap; shown so the
    owner knows what would go, not so anything goes automatically."""
    total_bytes_in_grace: int
    total_bytes_ready: int


async def grace_report(
    session: AsyncSession, *, grace_days: int, now: datetime | None = None
) -> GraceReport:
    """Where every currently-condemned item sits in its grace window.

    Grace is a property of the *latest* snapshot's EFFECTIVE condemned set
    (services.condemned): an item no longer condemned (rescued, spared, or re-judged)
    has left grace by definition, and a hand-reaped item is in it from the moment the
    owner clicks.
    """
    now = now or utcnow()
    window = timedelta(days=grace_days)

    latest = (
        await session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1))
    ).scalar_one_or_none()
    if latest is None:
        return GraceReport(grace_days, [], [], 0, 0)

    # The effective set, not the frozen one: a spare leaves the countdown at once (rather
    # than lingering until re-scan), and a hand reap ENTERS it at once -- the owner's
    # decision starts the same grace window and Leaving Soon warning a scan condemn gets.
    decisions = await whitelist.overrides(session)
    condemned = list((await effective_condemned(session, latest.id, decisions)).values())
    flagged = {
        f.media_key: f.first_flagged_at
        for f in (
            await session.execute(
                select(FirstFlagged).where(
                    FirstFlagged.media_key.in_([c.media_key for c in condemned] or [""])
                )
            )
        ).scalars()
    }

    in_grace: list[GraceItem] = []
    ready: list[GraceItem] = []
    for candidate in condemned:
        # No clock row should be impossible (the scan writes one on every condemn), but if
        # one is missing, treat the item as having *just* entered grace -- the safe reading,
        # since it keeps the file longer rather than shorter.
        started = flagged.get(candidate.media_key, now)
        ends = started + window
        remaining = max(0, (ends - now).days)
        item = GraceItem(
            media_key=candidate.media_key,
            candidate_id=candidate.id,
            plex_rating_key=candidate.plex_rating_key,
            title=candidate.title,
            media_type=candidate.media_type,
            size_bytes=int(candidate.size_bytes),
            first_flagged_at=started,
            grace_ends_at=ends,
            days_remaining=remaining,
            in_grace=now < ends,
        )
        (in_grace if item.in_grace else ready).append(item)

    in_grace.sort(key=lambda i: i.grace_ends_at)  # soonest to clear first
    ready.sort(key=lambda i: i.size_bytes, reverse=True)  # biggest reclaim first
    return GraceReport(
        grace_days=grace_days,
        in_grace=in_grace,
        ready=ready,
        total_bytes_in_grace=sum(i.size_bytes for i in in_grace),
        total_bytes_ready=sum(i.size_bytes for i in ready),
    )
