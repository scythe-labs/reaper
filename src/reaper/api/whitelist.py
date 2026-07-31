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

from reaper.api import tags as api_tags
from reaper.api.schemas import OverrideIn, SpareIn, WhitelistEntryOut
from reaper.clock import utcnow
from reaper.db.models import Candidate, FirstFlagged, Snapshot, WhitelistEntry
from reaper.services import retention, whitelist
from reaper.services.condemned import reap_is_effective
from reaper.services.profiles import active_profile_settings
from reaper.services.snapshot import record_first_flagged_bulk

router = APIRouter(prefix="/api", tags=[api_tags.REVIEW])


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _out(entry: WhitelistEntry) -> WhitelistEntryOut:
    return WhitelistEntryOut(
        media_key=entry.media_key,
        title=entry.title,
        note=entry.note,
        decision=entry.decision,
        spare_expires_at=(
            entry.spare_expires_at.isoformat() if entry.spare_expires_at is not None else None
        ),
        created_at=entry.created_at.isoformat(),
    )


async def _resolve_title(session: AsyncSession, media_key: str) -> str:
    """The display title for an override key, read from its latest surviving candidate.

    The title is never trusted from the client -- the media_key is the identity, and an
    override for a key none of the kept scans holds is more likely a bug than an intention
    (404). A show-level key matches on ``group_key`` and prefers the show's title over a
    season's.

    **``services.retention`` is what bounds "kept".** The select spans every snapshot
    deliberately, because the question is "do we know this item" and not "is it in the
    queue" -- but the sweep leaves only the newest ``KEEP_SNAPSHOTS`` to span. So the
    refusal claims no more than that, a missing record, never that no scan ever held one,
    and it names the window it speaks for. A scan COUNT, not a duration, which is why the
    copy reads it off the constant the sweep honors (#326).

    Nothing reaches the refusal today. Every SPA path passes a key drawn from the current
    queue (``WhyPanel``, ``ShowPanel``, and ``ReviewQueue``'s bulk bar), and an API key is
    refused both write routes outright by ``api.middleware._API_KEY_WRITES``, which admits
    scanning, planning, the policy and the profile and nothing else.
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
        raise HTTPException(
            404,
            "Reaper has no record of that item. "
            f"It keeps only the last {retention.KEEP_SNAPSHOTS} scans.",
        )
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


async def _sync_grace_clocks(
    session: AsyncSession, media_key: str, *, cleared_spare: bool = False
) -> None:
    """Grace bookkeeping for an override change, so the countdown matches the click.

    The grace clock tracks one thing: how long an item has been on the reap list. An
    override moves an item on or off that list at once, so the clock moves with it rather
    than waiting for the next scan.

    ON the list -- effectively condemned: a scan condemnation with no override, or a hand
    reap the engine honors. Its clock is (re)recorded through the scan's own decision
    (``snapshot.record_first_flagged_bulk``: set once, and reset only on a genuine return),
    so "on the list since" and the Leaving Soon warning read true from the moment of the
    click.

    OFF the list -- spared, or judged keep/abstain: its clock is deleted. That is a cleanup
    (a stale hand-reap timestamp can never shorten a later real condemnation) and, just as
    important, a safety reset: a scan-condemned item the owner SPARES leaves the list and
    loses its clock, so a later un-spare re-enters it with a FRESH window instead of coasting
    on a weeks-old timestamp that would drop it straight past grace with no Leaving Soon
    warning (rule 4). ``_apply_first_flag`` alone would not reset here -- a deliberate spare
    is indistinguishable from a brief scan outage by gap -- so the delete forces the reset.

    ``cleared_spare`` closes the one path the OFF-list delete cannot see: clearing a spare that
    left an item invisibly condemned (an expired timed spare whose scans re-condemned it and
    burned a clock down while every surface still showed "spared" -- B-2). On such a clear the
    item lands back ON the list carrying a stale clock ``record_first_flagged_bulk`` would honor,
    so when a protective spare is the override being removed we delete every affected clock FIRST
    and let the recorder write a fresh window (rule 71). Phase 1's durable expiry purge already
    prevents the burn-down from arising; this is the belt to that suspenders.
    """
    decisions = await whitelist.overrides(session)
    rows = await _affected_candidates(session, media_key)
    if not rows:
        return

    def on_reap_list(candidate: Candidate) -> bool:
        # What the grace clock tracks: effectively condemned. A hand spare takes an item off
        # the list even when the scan condemned it; a hand reap puts it on only if the engine
        # honors it; with no override, the frozen scan verdict decides.
        ov = whitelist.effective_override(candidate.media_key, decisions)
        if ov == "spare":
            return False
        if ov == "reap":
            return reap_is_effective(candidate)
        return candidate.verdict == "condemn"

    if cleared_spare:
        # Never trust a timestamp accrued while the item was invisible to the operator: wipe
        # every affected clock so whatever lands back on the list below earns a fresh window.
        for candidate in rows:
            clock = await session.get(FirstFlagged, candidate.media_key)
            if clock is not None:
                await session.delete(clock)
        await session.flush()

    on_list = [c.media_key for c in rows if on_reap_list(c)]
    if on_list:
        profile = await active_profile_settings(session)
        await record_first_flagged_bulk(session, on_list, utcnow(), grace_days=profile.grace_days)
    for candidate in rows:
        if on_reap_list(candidate):
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
            session,
            media_key=payload.media_key,
            title=title,
            note=payload.note,
            spare_days=payload.spare_days,
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
            spare_days=payload.spare_days,
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
        prior = await whitelist.override_for(session, media_key)
        removed = await whitelist.remove_override(session, media_key=media_key)
        await _sync_grace_clocks(session, media_key, cleared_spare=prior == "spare")
        await session.commit()
    return {"removed": removed}


@router.delete("/override/{media_key}")
async def clear_override(request: Request, media_key: str) -> dict[str, bool]:
    """Remove any override (spare or reap) -- the decision-neutral name for the same action."""
    async with _sessions(request)() as session:
        prior = await whitelist.override_for(session, media_key)
        removed = await whitelist.remove_override(session, media_key=media_key)
        await _sync_grace_clocks(session, media_key, cleared_spare=prior == "spare")
        await session.commit()
    return {"removed": removed}
