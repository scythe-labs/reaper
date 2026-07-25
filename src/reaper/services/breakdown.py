# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reap breakdown: what a reap would remove, and why. Read-only.

Three things, all derived from the latest snapshot and the owner's live overrides, so the
numbers match the exact set the planner will act on (``services.condemned``):

  * **the ledger** -- what the policy condemned, what the owner spared or added by hand,
    and the net a reap would remove (with the count that could not be measured);
  * **the movie/season split** of that net set;
  * **why the policy condemned them** -- a participation tally over each condemned row's
    frozen signals. A title trips several signals at once, so the counts overlap and sum
    past the total; this is a "how many condemned titles trip each signal", never a
    partition.

Deletes nothing and plans nothing. The plan itself is still built, dry-run, and executed
through the runs API; this only explains what that plan would cover.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.db.models import Candidate, Snapshot
from reaper.services import whitelist
from reaper.services.condemned import effective_condemned, held_reaps


@dataclass(frozen=True)
class SignalCount:
    """How many condemned titles one signal pushed toward removal, and their measured size."""

    id: str
    count: int
    bytes: int
    unknown_size: int


@dataclass(frozen=True)
class ReapBreakdown:
    has_snapshot: bool
    # The ledger. Counts are the reap decision (measured and unmeasured together); the byte
    # figures sum only what has a size, with the unmeasured carried as a separate count.
    policy_condemned: int
    policy_condemned_bytes: int
    policy_condemned_unknown: int
    hand_spared: int
    spares_expired: int
    """The share of ``hand_spared`` whose clock has already passed -- spares that are keeping a
    condemned title out of this plan even though the operator's decision has run out.

    A spare's expiry is realized ONLY by a scan (``whitelist.purge_expired_spares``), so between
    the clock passing and the next scan the planner, this ledger and the executor all still read
    it and the file is genuinely kept. Reported so the Reap page can say why those titles are
    missing and that a scan releases them, instead of leaving them silently absent. Counted with
    the same ``<= now`` boundary the purge uses, so this is exactly what a scan would let go."""
    hand_reaped: int
    hand_reaped_bytes: int
    hand_reaped_unknown: int
    hand_reaped_held: int
    """Hand reaps the engine refuses to honor yet (blocked evidence, a structural gate), so
    they are NOT in ``will_reap``. Reported so an operator who marked N items and sees fewer
    reaped is told the rest are held, never silently dropped (PR-2)."""
    will_reap: int
    will_reap_bytes: int
    will_reap_unknown: int
    # The movie/season split of the net set, with the unmeasured share of each. The planner
    # holds unmeasured items back while the unknown-size allowance is 0, so the page needs
    # both halves to subtract the same rows the headline does -- otherwise the split and the
    # total state two different numbers for one reap (rule 30).
    movies: int
    movies_unknown: int
    seasons: int
    seasons_unknown: int
    # Why the policy condemned them: participation over the frozen condemned rows.
    condemned_by: list[SignalCount]


def _empty() -> ReapBreakdown:
    return ReapBreakdown(
        has_snapshot=False,
        policy_condemned=0,
        policy_condemned_bytes=0,
        policy_condemned_unknown=0,
        hand_spared=0,
        spares_expired=0,
        hand_reaped=0,
        hand_reaped_bytes=0,
        hand_reaped_unknown=0,
        hand_reaped_held=0,
        will_reap=0,
        will_reap_bytes=0,
        will_reap_unknown=0,
        movies=0,
        movies_unknown=0,
        seasons=0,
        seasons_unknown=0,
        condemned_by=[],
    )


def _adds_signals(explanation_json: str) -> set[str]:
    """The signal ids that pushed one stored row toward removal.

    A signal "adds" when it contributed pressure. Rows frozen since the ``state`` field
    shipped carry it explicitly; older rows fall back to a positive contribution, which is
    the same fact.

    Guarded at all four layers a stored explanation can be corrupt at (rule 96): the parse,
    the top-level shape, each entry's shape, and the contribution value itself, which is
    read as a number or not at all -- ``float("")`` on a hand-edited row raises a ValueError
    the same way the parse does. An unreadable explanation contributes nothing rather than
    failing the whole breakdown, and empty is the cautious reading here: this tally only
    explains rows the planner has ALREADY put on the removal list, so a missing entry
    under-explains a removal and can never cause one.
    """
    try:
        exp = json.loads(explanation_json)
    except (ValueError, TypeError):
        return set()
    if not isinstance(exp, dict):
        return set()
    out: set[str] = set()
    for entry in exp.get("signals") or []:
        if not isinstance(entry, dict) or "id" not in entry:
            continue
        state = entry.get("state")
        contribution = entry.get("contribution")
        adds = state == "adds" or (
            state is None and isinstance(contribution, int | float) and contribution > 0
        )
        if adds:
            out.add(str(entry["id"]))
    return out


async def reap_breakdown(session: AsyncSession) -> ReapBreakdown:
    """What a reap built right now would remove, and why the policy condemned it."""
    latest = (
        await session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1))
    ).scalar_one_or_none()
    if latest is None:
        return _empty()

    decisions = await whitelist.overrides(session)

    condemned_rows = (
        (
            await session.execute(
                select(Candidate).where(
                    Candidate.snapshot_id == latest.id, Candidate.verdict == "condemn"
                )
            )
        )
        .scalars()
        .all()
    )
    # The exact set the planner will act on: frozen condemned, minus hand-spares, plus the
    # hand-reaps the engine honors. One source of truth, so the ledger's total matches the
    # confirmation phrase the owner will approve.
    effective = list((await effective_condemned(session, latest.id, decisions)).values())

    policy_condemned = len(condemned_rows)
    policy_bytes = sum(c.size_bytes for c in condemned_rows if c.size_bytes is not None)
    policy_unknown = sum(1 for c in condemned_rows if c.size_bytes is None)

    # A hand spare removes a policy-condemned row from the reap set.
    spared_rows = [
        c for c in condemned_rows if whitelist.effective_override(c.media_key, decisions) == "spare"
    ]
    hand_spared = len(spared_rows)

    # ...and of those, the ones whose clock has already passed. Counted over the SPARED
    # condemned rows only, not every expired whitelist row, because the Reap page's claim is
    # "these are not in this plan" -- true only of a title the policy condemned and a spare is
    # holding back. The expiry read is the effective one (own key, else the show's), matching
    # the spare that is actually doing the keeping.
    now = datetime.now(UTC)
    expiries = await whitelist.spare_expiries(session)
    spares_expired = sum(
        1
        for c in spared_rows
        if (exp := whitelist.effective_spare_expiry(c.media_key, decisions, expiries)) is not None
        and exp <= now
    )

    will_reap = len(effective)
    will_bytes = sum(c.size_bytes for c in effective if c.size_bytes is not None)
    will_unknown = sum(1 for c in effective if c.size_bytes is None)
    movies = sum(1 for c in effective if c.media_type == "movie")
    movies_unknown = sum(1 for c in effective if c.media_type == "movie" and c.size_bytes is None)
    seasons = sum(1 for c in effective if c.media_type == "season")
    seasons_unknown = sum(1 for c in effective if c.media_type == "season" and c.size_bytes is None)

    # A hand reap is a net row the policy did not condemn on its own.
    hand_reaped_rows = [c for c in effective if c.verdict != "condemn"]
    hand_reaped = len(hand_reaped_rows)
    hand_reaped_bytes = sum(c.size_bytes for c in hand_reaped_rows if c.size_bytes is not None)
    hand_reaped_unknown = sum(1 for c in hand_reaped_rows if c.size_bytes is None)

    # The operator's reap marks the engine refuses to honor yet: counted (not dropped) so the
    # ledger can say "N of your reap marks are held" rather than silently under-report (PR-2).
    hand_reaped_held = len(await held_reaps(session, latest.id, decisions))

    # Participation over the frozen condemned rows: for each signal that added pressure,
    # how many condemned titles carry it, and their measured size. Overlapping by design.
    counts: dict[str, int] = {}
    sizes: dict[str, int] = {}
    unknowns: dict[str, int] = {}
    for row in condemned_rows:
        for sid in _adds_signals(row.explanation_json):
            counts[sid] = counts.get(sid, 0) + 1
            if row.size_bytes is None:
                unknowns[sid] = unknowns.get(sid, 0) + 1
            else:
                sizes[sid] = sizes.get(sid, 0) + row.size_bytes
    condemned_by = [
        SignalCount(
            id=sid, count=counts[sid], bytes=sizes.get(sid, 0), unknown_size=unknowns.get(sid, 0)
        )
        for sid in counts
    ]
    condemned_by.sort(key=lambda s: (-s.count, s.id))

    return ReapBreakdown(
        has_snapshot=True,
        policy_condemned=policy_condemned,
        policy_condemned_bytes=policy_bytes,
        policy_condemned_unknown=policy_unknown,
        hand_spared=hand_spared,
        spares_expired=spares_expired,
        hand_reaped=hand_reaped,
        hand_reaped_bytes=hand_reaped_bytes,
        hand_reaped_unknown=hand_reaped_unknown,
        hand_reaped_held=hand_reaped_held,
        will_reap=will_reap,
        will_reap_bytes=will_bytes,
        will_reap_unknown=will_unknown,
        movies=movies,
        movies_unknown=movies_unknown,
        seasons=seasons,
        seasons_unknown=seasons_unknown,
        condemned_by=condemned_by,
    )
