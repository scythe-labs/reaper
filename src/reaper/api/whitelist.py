# SPDX-License-Identifier: AGPL-3.0-or-later
"""Overriding a verdict by hand -- sparing or reaping an item.

The review queue answers "what would Reaper delete?"; these routes let the owner answer
back. A **spare** takes effect in two places (see ``services.whitelist``): the next scan
judges the item PROTECT, and any plan built in the meantime excludes it regardless of a
frozen snapshot's stale verdict. A **reap** is the inverse: the owner has looked and wants
the file gone, so the next scan forces it onto the list -- short of a hard safety gate
(something streaming now, or a file no *arr manages), which still wins.

A media_key may identify a whole show, in which case the decision covers every season.

Nothing here deletes. Removing an override does not reap anything -- it merely returns the
file to being *judged* by the policy again; it re-enters the review queue as a candidate.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.api.schemas import OverrideIn, SpareIn, WhitelistEntryOut
from reaper.clock import utcnow
from reaper.db.models import Candidate, FirstFlagged, Snapshot, WhitelistEntry
from reaper.services import whitelist
from reaper.services.condemned import reap_is_effective
from reaper.services.profiles import active_profile_settings
from reaper.services.snapshot import record_first_flagged_bulk

router = APIRouter(prefix="/api")


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _out(entry: WhitelistEntry) -> WhitelistEntryOut:
    return WhitelistEntryOut(
        media_key=entry.media_key,
        title=entry.title,
        note=entry.note,
        decision=entry.decision,
        created_at=entry.created_at.isoformat(),
    )


async def _resolve_title(session: AsyncSession, media_key: str) -> str:
    """The display title for an override key, read from its latest candidate.

    The title is never trusted from the client -- the media_key is the identity, and an
    override for a key no scan has ever seen is more likely a bug than an intention (404).
    A show-level key matches on ``group_key`` and prefers the show's title over a season's.
    """
    candidate = (
        await session.execute(
            select(Candidate)
            .where(or_(Candidate.media_key == media_key, Candidate.group_key == media_key))
            .order_by(Candidate.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise HTTPException(404, "No scanned item has that identity; nothing to override.")
    if candidate.group_key == media_key and candidate.group_title:
        return candidate.group_title
    return candidate.title


async def _affected_candidates(session: AsyncSession, media_key: str) -> list[Candidate]:
    """The latest snapshot's rows a decision on this key covers: the item itself, or
    every season of a show-level key. Empty before the first scan."""
    latest = (
        await session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1))
    ).scalar_one_or_none()
    if latest is None:
        return []
    rows = await session.execute(
        select(Candidate).where(
            Candidate.snapshot_id == latest.id,
            or_(Candidate.media_key == media_key, Candidate.group_key == media_key),
        )
    )
    return list(rows.scalars().all())


async def _sync_grace_clocks(session: AsyncSession, media_key: str) -> None:
    """Grace bookkeeping for an override change, so the countdown matches the click.

    A hand reap enters the effective condemned set immediately (services.condemned), so
    its grace clock starts now -- written through the scan's own clock decision
    (``snapshot.record_first_flagged_bulk``: set once, reset only on a genuine return),
    which is what makes "on the list since" and the Leaving Soon warning true from the
    moment the owner clicks rather than from the next scan.

    The reverse direction cleans up: removing the reap (or flipping it to spare) deletes
    the clock again for rows the scan did not condemn, so a stale hand-reap timestamp
    can never shorten the grace window of a later, real condemnation (rule 4). Rows the
    scan condemned keep their scan-owned clock untouched.
    """
    decisions = await whitelist.overrides(session)
    rows = await _affected_candidates(session, media_key)
    if not rows:
        return

    def reaped_now(candidate: Candidate) -> bool:
        return whitelist.effective_override(
            candidate.media_key, decisions
        ) == "reap" and reap_is_effective(candidate)

    to_start = [c.media_key for c in rows if reaped_now(c)]
    if to_start:
        profile = await active_profile_settings(session)
        await record_first_flagged_bulk(session, to_start, utcnow(), grace_days=profile.grace_days)
    for candidate in rows:
        if candidate.verdict == "condemn" or reaped_now(candidate):
            continue
        clock = await session.get(FirstFlagged, candidate.media_key)
        if clock is not None:
            await session.delete(clock)
    await session.flush()


@router.get("/whitelist")
async def list_whitelist(request: Request) -> list[WhitelistEntryOut]:
    async with _sessions(request)() as session:
        return [_out(e) for e in await whitelist.list_spared(session)]


@router.post("/whitelist")
async def spare_item(request: Request, payload: SpareIn) -> WhitelistEntryOut:
    """Spare an item so it is never reaped -- the common-case shorthand for an override."""
    async with _sessions(request)() as session:
        title = await _resolve_title(session, payload.media_key)
        entry = await whitelist.spare(
            session, media_key=payload.media_key, title=title, note=payload.note
        )
        await _sync_grace_clocks(session, payload.media_key)
        out = _out(entry)
        await session.commit()
        return out


@router.post("/override")
async def set_override(request: Request, payload: OverrideIn) -> WhitelistEntryOut:
    """Override an item's verdict by hand -- spare it, or force it onto the reap list.

    Switching decision in place is fine: reaping an already-spared item flips it to reap.
    A reap never overrides a hard safety gate; that is enforced at scan time, not here.
    """
    async with _sessions(request)() as session:
        title = await _resolve_title(session, payload.media_key)
        entry = await whitelist.set_override(
            session,
            media_key=payload.media_key,
            title=title,
            decision=payload.decision,
            note=payload.note,
        )
        await _sync_grace_clocks(session, payload.media_key)
        out = _out(entry)
        await session.commit()
        return out


@router.delete("/whitelist/{media_key}")
async def unspare_item(request: Request, media_key: str) -> dict[str, bool]:
    """Remove any override (spare or reap). Returns whether one existed. This does not delete
    the file -- it lets the item be judged by the policy again on the next scan."""
    async with _sessions(request)() as session:
        removed = await whitelist.remove_override(session, media_key=media_key)
        await _sync_grace_clocks(session, media_key)
        await session.commit()
    return {"removed": removed}


@router.delete("/override/{media_key}")
async def clear_override(request: Request, media_key: str) -> dict[str, bool]:
    """Remove any override (spare or reap) -- the decision-neutral name for the same action."""
    async with _sessions(request)() as session:
        removed = await whitelist.remove_override(session, media_key=media_key)
        await _sync_grace_clocks(session, media_key)
        await session.commit()
    return {"removed": removed}
