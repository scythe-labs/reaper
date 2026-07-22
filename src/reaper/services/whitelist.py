# SPDX-License-Identifier: AGPL-3.0-or-later
"""The manual whitelist: files the owner spared by hand.

A whitelist that silently fails to protect is the worst bug this project can ship, so
this module does exactly one thing and does it plainly: it maps ``media_key`` to a
durable "never reap" record, and it is consulted in *two* independent places --

* the **scan**, where a spared key protects the item at judge time (it becomes a
  ``protect`` verdict and leaves the "would delete" list), and
* the **planner**, which excludes spared keys from a run regardless of what a frozen
  snapshot's candidate row still says.

The second is not redundant. A snapshot is immutable evidence; sparing an item after a
scan cannot reach back and rewrite its stored verdict. Without the planner filter, a
plan built in the window between "spare" and the next scan would still target the file
the owner just told us to keep. Belt, and separately, suspenders.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clock import utcnow
from reaper.db.models import WhitelistEntry


def show_key(media_key: str) -> str | None:
    """The show's key for a season media_key -- ``sonarr:i:series:n`` -> ``sonarr:i:series``.

    This is what lets a single override on a whole show apply to all of its seasons. A movie
    key (``radarr:i:id``) has no show, so this returns ``None``.
    """
    parts = media_key.split(":")
    if len(parts) == 4 and parts[0] == "sonarr":
        return ":".join(parts[:3])
    return None


def effective_override(media_key: str, decisions: dict[str, str]) -> str | None:
    """The manual decision that governs an item -- its own, or the one on its show.

    An item's own key wins over its show's, so you can reap a whole show yet spare one season
    back. ``None`` when neither the item nor its show is overridden.
    """
    if media_key in decisions:
        return decisions[media_key]
    show = show_key(media_key)
    if show is not None and show in decisions:
        return decisions[show]
    return None


def effective_spare_expiry(
    media_key: str, decisions: dict[str, str], expiries: dict[str, datetime | None]
) -> datetime | None:
    """When the spare in effect on this item stops protecting -- own key first, then its show.

    Mirrors :func:`effective_override`'s precedence, so the countdown a row shows belongs to the
    same spare that keeps it. ``None`` means the effective spare is forever (or the item is not
    spared at all): call this only when :func:`effective_override` already returned ``"spare"``,
    and then ``None`` reads as "kept forever".
    """
    if media_key in decisions:
        return expiries.get(media_key)
    show = show_key(media_key)
    if show is not None and show in decisions:
        return expiries.get(show)
    return None


async def overrides(session: AsyncSession) -> dict[str, str]:
    """``media_key -> decision`` for every manual override -- what live consumers read once.

    A timed spare stays in force here until the next scan realizes its expiry
    (:func:`overrides_effective_at`). That is deliberate: between scans an expired spare keeps
    protecting the file, failing toward keeping it rather than reaping it early on a clock tick
    that no scan has yet re-anchored a grace window for.
    """
    rows = await session.execute(select(WhitelistEntry.media_key, WhitelistEntry.decision))
    return dict(rows.tuples().all())


async def overrides_effective_at(session: AsyncSession, now: datetime) -> dict[str, str]:
    """Manual overrides with EXPIRED hand-spares dropped as of ``now`` -- what the scan judges on.

    This is the ONE place a timed spare's clock is realized. A spare whose ``spare_expires_at``
    has passed no longer protects, so the scan re-judges the item exactly as if it were never
    spared -- which re-condemns it if the policy still would, and (because the spare took its
    grace clock off the list when it was set) writes a FRESH first-flagged timestamp, so it
    re-enters on a full grace window, never a spent one (rule 4). Reaps never expire.
    """
    rows = await session.execute(
        select(WhitelistEntry.media_key, WhitelistEntry.decision, WhitelistEntry.spare_expires_at)
    )
    out: dict[str, str] = {}
    for media_key, decision, expires_at in rows.tuples().all():
        if decision == "spare" and expires_at is not None and expires_at <= now:
            continue
        out[media_key] = decision
    return out


async def spare_expiries(session: AsyncSession) -> dict[str, datetime | None]:
    """``media_key -> spare_expires_at`` for every SPARE row (``None`` = kept forever).

    Reaps are omitted -- they never expire. Feeds the review queue's countdown alongside
    :func:`overrides`, so the card can say how long a spare has left.
    """
    rows = await session.execute(
        select(WhitelistEntry.media_key, WhitelistEntry.spare_expires_at).where(
            WhitelistEntry.decision == "spare"
        )
    )
    return dict(rows.tuples().all())


async def override_for(session: AsyncSession, media_key: str) -> str | None:
    """The decision on one media_key ("spare" | "reap"), or ``None`` -- a targeted check."""
    entry = await session.get(WhitelistEntry, media_key)
    return entry.decision if entry is not None else None


async def is_spared(session: AsyncSession, media_key: str) -> bool:
    """Whether one media_key has been hand-spared -- a targeted check for a single item."""
    return await override_for(session, media_key) == "spare"


async def list_spared(session: AsyncSession) -> list[WhitelistEntry]:
    """The overridden items, newest first, for the "Spared" view."""
    rows = await session.execute(select(WhitelistEntry).order_by(WhitelistEntry.created_at.desc()))
    return list(rows.scalars().all())


async def set_override(
    session: AsyncSession,
    *,
    media_key: str,
    title: str,
    decision: str,
    note: str | None,
    spare_days: int = 0,
    now: datetime | None = None,
) -> WhitelistEntry:
    """Record a manual override -- ``"spare"`` (keep) or ``"reap"`` (force onto the reap list).

    ``spare_days`` is how long a *spare* keeps the item: ``0`` (the default) means forever, the
    original behavior; a positive count means keep it for that many days from ``now``, after
    which the next scan re-judges it. It is ignored for a reap, which never expires -- and
    setting a reap clears any prior expiry, so flipping a timed spare to reap and back does not
    resurrect a stale clock.

    Idempotent, and switches decision in place: reaping an already-spared item flips it to reap.
    Flushes so the override is visible to any read later in the same unit of work; the caller
    owns the commit. ``title`` is denormalized in for display; the media_key is identity.
    """
    now = now or utcnow()
    expires_at = (
        now + timedelta(days=spare_days) if decision == "spare" and spare_days > 0 else None
    )
    entry = await session.get(WhitelistEntry, media_key)
    if entry is None:
        entry = WhitelistEntry(
            media_key=media_key,
            title=title,
            note=note,
            decision=decision,
            spare_expires_at=expires_at,
            created_at=now,
        )
        session.add(entry)
    else:
        entry.title = title
        entry.note = note
        entry.decision = decision
        entry.spare_expires_at = expires_at
    await session.flush()
    return entry


async def spare(
    session: AsyncSession,
    *,
    media_key: str,
    title: str,
    note: str | None,
    spare_days: int = 0,
    now: datetime | None = None,
) -> WhitelistEntry:
    """Record a spare -- a ``"spare"`` override. Kept as the common-case shorthand.

    ``spare_days`` is the keep length (``0`` = forever); see :func:`set_override`.
    """
    return await set_override(
        session,
        media_key=media_key,
        title=title,
        decision="spare",
        note=note,
        spare_days=spare_days,
        now=now,
    )


async def remove_override(session: AsyncSession, *, media_key: str) -> bool:
    """Remove any override (spare or reap). Returns whether a row existed to remove.

    Removing an override does not delete anything: it merely lets the file be *judged* again
    on the next scan by the policy alone.
    """
    entry = await session.get(WhitelistEntry, media_key)
    if entry is None:
        return False
    await session.delete(entry)
    await session.flush()
    return True


async def unspare(session: AsyncSession, *, media_key: str) -> bool:
    """Backwards-compatible alias for :func:`remove_override`."""
    return await remove_override(session, media_key=media_key)
