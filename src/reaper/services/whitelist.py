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

from sqlalchemy import delete, select
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
    """``media_key -> decision`` for every manual override, as of this read.

    Cheap enough to re-read on a hot path: a two-column Core select, not an ORM entity load,
    so it does not go through a session's identity map and a caller re-querying inside its
    own long transaction still sees other sessions' committed rows. The executor relies on
    exactly that -- it re-reads this before every item of a real run, so a Spare clicked
    while a reap is in flight still keeps the file.

    A timed spare stays in force here until the next scan realizes its expiry. That is
    deliberate: between scans an expired spare keeps protecting the file, failing toward keeping
    it rather than reaping it early on a clock tick that no scan has yet re-anchored a grace
    window for. The scan realizes expiry in two coupled steps in the SAME transaction:
    :func:`overrides_effective_at` drops the expired spare from the map it judges on, and
    :func:`purge_expired_spares` deletes the row so this function -- and every consumer that
    reads it (planner, executor, grace, review queue) -- converges the moment the snapshot
    commits. Reading the map without the purge would strand the expired spare here forever.
    """
    rows = await session.execute(select(WhitelistEntry.media_key, WhitelistEntry.decision))
    return dict(rows.tuples().all())


async def purge_expired_spares(session: AsyncSession, now: datetime) -> list[str]:
    """Delete every hand-spare whose clock has passed as of ``now`` -- the durable half of expiry.

    :func:`overrides_effective_at` drops an expired spare from the map the scan JUDGES on, but
    that is an in-memory view; the row itself lives on, and every live consumer (planner,
    executor, grace, review queue) reads :func:`overrides`, which keeps returning it. This is
    the ONE place the expiry is realized in storage. Call it inside the scan transaction with
    the SAME ``now`` the judge used, right after :func:`overrides_effective_at`: the moment the
    snapshot commits, every consumer converges on the re-condemned item, and the scan's own
    ``record_first_flagged_bulk`` has already written it a FRESH grace clock (the spare took the
    item's clock off the list when it was set, so the re-condemn earns a full window, never a
    spent one -- rule 4). Without this, an expired spare dead-ends: unplannable, un-executable,
    still shown "spared", until the operator manually clears it (rules 7/23/24/70).

    Deletes by predicate (not an ``IN`` list of keys) so there is no SQLite variable-limit
    ceiling, and returns the purged media_keys for a count-only log line. Reaps never expire,
    so only spares are ever touched.
    """
    predicate = (
        (WhitelistEntry.decision == "spare")
        & WhitelistEntry.spare_expires_at.is_not(None)
        & (WhitelistEntry.spare_expires_at <= now)
    )
    keys = [
        key
        for (key,) in (
            await session.execute(select(WhitelistEntry.media_key).where(predicate))
        ).tuples()
    ]
    if not keys:
        return []
    await session.execute(delete(WhitelistEntry).where(predicate))
    return keys


def without_expired_spares(
    decisions: dict[str, str], expiries: dict[str, datetime | None], now: datetime
) -> dict[str, str]:
    """``decisions`` with every hand-spare whose clock has passed dropped, as of ``now``.

    The rule for what a purge leaves behind, as a pure function, so the scan that performs the
    purge and any surface that wants to say what one WOULD release cannot drift apart on the
    boundary (rule 104). ``<=`` is the boundary :func:`purge_expired_spares` deletes on.

    Note the two maps are read together on purpose: ``expiries`` holds spare rows only, so a
    reap can never be dropped here, and a spare missing from it is a forever spare, which has
    no clock to pass.
    """
    return {
        key: decision
        for key, decision in decisions.items()
        if not (
            decision == "spare"
            and (expires_at := expiries.get(key)) is not None
            and expires_at <= now
        )
    }


async def overrides_effective_at(session: AsyncSession, now: datetime) -> dict[str, str]:
    """Manual overrides with EXPIRED hand-spares dropped as of ``now`` -- what the scan judges on.

    The read half of realizing a timed spare's clock: a spare whose ``spare_expires_at`` has
    passed no longer protects, so the scan re-judges the item exactly as if it were never spared
    -- which re-condemns it if the policy still would, and (because the spare took its grace
    clock off the list when it was set) writes a FRESH first-flagged timestamp, so it re-enters
    on a full grace window, never a spent one (rule 4). This drops the spare from the judged map
    only; :func:`purge_expired_spares`, called in the same transaction with the same ``now``,
    deletes the row so every live consumer converges. Reaps never expire.

    One query, then :func:`without_expired_spares` applies the rule -- the same call the reap
    ledger makes to count what a scan would let go.
    """
    rows = await session.execute(
        select(WhitelistEntry.media_key, WhitelistEntry.decision, WhitelistEntry.spare_expires_at)
    )
    decisions: dict[str, str] = {}
    expiries: dict[str, datetime | None] = {}
    for media_key, decision, expires_at in rows.tuples().all():
        decisions[media_key] = decision
        if decision == "spare":
            expiries[media_key] = expires_at
    return without_expired_spares(decisions, expiries, now)


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
