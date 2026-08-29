# SPDX-License-Identifier: AGPL-3.0-or-later
"""Override a verdict by hand. Spare an item, or force it onto the reap list.

The review queue answers "what would Reaper delete?" These routes let the owner
answer back. A spare takes effect in two places (see ``services.whitelist``). The
next scan judges the item PROTECT, and any plan built in the meantime excludes it,
regardless of a frozen snapshot's stale verdict. A reap is the inverse. The owner has
looked and wants the file gone, so the next scan forces it onto the list, unless a
hard safety gate still applies (something streaming now, or a file no *arr manages),
which always wins.

A media_key can identify a whole show, in which case the decision covers every
season.

Nothing here deletes. Removing an override does not reap anything. It returns the
file to being judged by the policy again, so it re-enters the review queue as a
candidate.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.api import tags as api_tags
from reaper.api.deps import session_factory
from reaper.api.errors import refuse
from reaper.api.schemas import OverrideIn, RemovedOut, WhitelistEntryOut
from reaper.clock import utcnow
from reaper.db.models import Candidate, FirstFlagged, Snapshot, WhitelistEntry
from reaper.services import retention, whitelist
from reaper.services.condemned import reap_is_effective
from reaper.services.profiles import active_profile_settings
from reaper.services.snapshot import record_first_flagged_bulk

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=[api_tags.REVIEW])


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
    """Return the display title for an override key, read from its latest surviving
    candidate.

    The title is never trusted from the client. The media_key is the identity, and an
    override for a key none of the kept scans holds is more likely a bug than an
    intention, so this refuses with 404. A show-level key matches on ``group_key`` and
    prefers the show's title over a season's.

    ``services.retention`` is what bounds "kept". The select spans every snapshot
    deliberately, because the question is "do we know this item", not "is it in the
    queue". The sweep leaves only the newest ``KEEP_SNAPSHOTS`` to span, though, so the
    refusal claims no more than a missing record. It never claims that no scan ever
    held one, and it names the window it speaks for as a scan count, not a duration,
    reading that count off the same constant the sweep honors.

    Nothing reaches this refusal today. Every SPA path passes a key drawn from the
    current queue (``WhyPanel``, ``ShowPanel``, and ``ReviewQueue``'s bulk bar). An API
    key is refused outright on both write routes by ``api.middleware._API_KEY_WRITES``,
    which admits scanning, planning, the policy, and the profile, and nothing else.
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
        refuse(404, "error.whitelist.unknown_item", keep_scans=retention.KEEP_SNAPSHOTS)
    if candidate.group_key == media_key and candidate.group_title:
        return candidate.group_title
    return candidate.title


async def _affected_candidates(session: AsyncSession, media_key: str) -> list[Candidate]:
    """Return the latest snapshot's rows a decision on this key covers. This is the
    item itself, or every season of a show-level key. Empty before the first scan."""
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
    """Update grace bookkeeping for an override change, so the countdown matches the
    click.

    The grace clock tracks how long an item has been on the reap list. An override
    moves an item on or off that list at once, so the clock moves with it instead of
    waiting for the next scan.

    When the item lands on the list, because a scan condemned it with no override, or
    because a hand reap the engine honors put it there, its clock is recorded, or
    re-recorded, through the scan's own decision
    (``snapshot.record_first_flagged_bulk``, which sets it once and resets it only on
    a genuine return to the list). That keeps "on the list since" and the Leaving Soon
    warning true from the moment of the click.

    When the item leaves the list, because it was spared or judged keep or abstain,
    its clock is deleted. This is a cleanup, since a stale hand-reap timestamp can
    never shorten a later real condemnation. It is also a safety reset: sparing a
    scan-condemned item takes it off the list and clears its clock, so a later
    un-spare re-enters it with a fresh window instead of coasting on a weeks-old
    timestamp that would drop it straight past grace with no Leaving Soon warning.
    ``_apply_first_flag`` alone would not reset the clock here, since a deliberate
    spare looks the same as a brief scan outage from a gap in the timeline alone, so
    this function forces the reset by deleting it.

    ``cleared_spare`` covers a path the logic above cannot see on its own. It handles
    clearing a spare that left an item invisibly condemned. This happens when a timed
    spare expires and a later scan re-condemns the item while every surface in the UI
    still shows it as "spared", burning down a hand-reap clock nobody knew was
    running. When that spare is cleared, the item lands back on the list carrying
    that stale clock, which ``record_first_flagged_bulk`` would otherwise honor. So
    when the override being removed is a protective spare, this function deletes
    every affected clock first and lets the recorder write a fresh window. A separate
    durable expiry purge already stops this burn-down from happening in the first
    place. Deleting the clock here is a second layer of protection.
    """
    decisions = await whitelist.overrides(session)
    rows = await _affected_candidates(session, media_key)
    if not rows:
        return

    def on_reap_list(candidate: Candidate) -> bool:
        # This is what counts as effectively condemned, what the grace clock tracks. A
        # hand spare takes an item off the list even when the scan condemned it. A
        # hand reap puts it on only if the engine honors it. With no override, the
        # frozen scan verdict decides.
        ov = whitelist.effective_override(candidate.media_key, decisions)
        if ov == "spare":
            return False
        if ov == "reap":
            return reap_is_effective(candidate)
        return candidate.verdict == "condemn"

    if cleared_spare:
        # Never trust a timestamp accrued while the item was invisible to the operator.
        # This wipes every affected clock, so whatever lands back on the list below
        # earns a fresh window.
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


def _log_override(
    media_key: str, decision: str, *, prior: str | None, spare_days: int | None
) -> None:
    """Record a hand decision, at INFO level.

    This is the only durable record of the operator's override once it is cleared.
    Setting an override leaves a `WhitelistEntry` row, but `remove_override` deletes
    that row and `_sync_grace_clocks` wipes the grace clock in the same call. This log
    line is what still says the override ever existed.

    INFO, not DEBUG, because this is the highest-stakes thing a person does by hand
    here. It happens only a few times a session, and nobody turns on Debug before
    making a decision they want explained later. `prior` is what makes the line show
    a transition, not just a snapshot.
    """
    log.info(
        "whitelist.override",
        media_key=media_key,
        decision=decision,
        prior=prior,
        spare_days=spare_days,
    )


@router.post("/override")
async def set_override(request: Request, payload: OverrideIn) -> WhitelistEntryOut:
    """Override an item's verdict by hand. Spare it, or force it onto the reap list.

    Switching decision in place is fine. Reaping an already-spared item flips it to
    reap. A reap never overrides a hard safety gate. That gate is enforced at scan
    time, not here.
    """
    async with session_factory(request)() as session:
        title = await _resolve_title(session, payload.media_key)
        prior = await whitelist.override_for(session, payload.media_key)
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
        _log_override(
            payload.media_key, payload.decision, prior=prior, spare_days=payload.spare_days
        )
        return out


@router.delete("/override/{media_key}")
async def clear_override(
    request: Request, media_key: str, include_seasons: bool = False
) -> RemovedOut:
    """Remove any override, spare or reap. This is the decision-neutral name for the
    same action.

    ``include_seasons`` widens a show key's clear to its season-level rows too. The
    review queue's bulk bar sends it, because a selected show card shows every season's
    hand mark, so its clear must cover what it showed. The level-scoped controls (show
    panel, season rows) never send it, so each still reverses only the key it lit.
    ``cleared_spare`` fires if ANY removed row was a spare: a spare among them may have
    been keeping a season off the list, so every affected clock resets to a fresh
    window rather than trusting one accrued while the item was covered.
    """
    async with session_factory(request)() as session:
        if include_seasons:
            cleared = await whitelist.remove_show_overrides(session, show_key=media_key)
            await _sync_grace_clocks(
                session,
                media_key,
                cleared_spare=any(decision == "spare" for _, decision in cleared),
            )
            await session.commit()
            for key, decision in cleared:
                _log_override(key, "cleared", prior=decision, spare_days=None)
            return RemovedOut(removed=bool(cleared))
        prior = await whitelist.override_for(session, media_key)
        removed = await whitelist.remove_override(session, media_key=media_key)
        await _sync_grace_clocks(session, media_key, cleared_spare=prior == "spare")
        await session.commit()
    if removed:
        _log_override(media_key, "cleared", prior=prior, spare_days=None)
    return RemovedOut(removed=removed)
