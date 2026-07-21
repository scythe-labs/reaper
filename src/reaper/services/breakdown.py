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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.db.models import Candidate, Snapshot
from reaper.services import whitelist
from reaper.services.condemned import effective_condemned


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
    hand_reaped: int
    hand_reaped_bytes: int
    hand_reaped_unknown: int
    will_reap: int
    will_reap_bytes: int
    will_reap_unknown: int
    # The movie/season split of the net set.
    movies: int
    seasons: int
    # Why the policy condemned them: participation over the frozen condemned rows.
    condemned_by: list[SignalCount]


def _empty() -> ReapBreakdown:
    return ReapBreakdown(
        has_snapshot=False,
        policy_condemned=0,
        policy_condemned_bytes=0,
        policy_condemned_unknown=0,
        hand_spared=0,
        hand_reaped=0,
        hand_reaped_bytes=0,
        hand_reaped_unknown=0,
        will_reap=0,
        will_reap_bytes=0,
        will_reap_unknown=0,
        movies=0,
        seasons=0,
        condemned_by=[],
    )


def _adds_signals(explanation_json: str) -> set[str]:
    """The signal ids that pushed one stored row toward removal.

    A signal "adds" when it contributed pressure. Rows frozen since the ``state`` field
    shipped carry it explicitly; older rows fall back to a positive contribution, which is
    the same fact. Defensive like ``_fired_gates`` in routes: an unreadable explanation
    contributes nothing rather than failing the whole breakdown.
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
        adds = state == "adds" or (state is None and float(entry.get("contribution") or 0) > 0)
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
    hand_spared = sum(
        1 for c in condemned_rows if whitelist.effective_override(c.media_key, decisions) == "spare"
    )

    will_reap = len(effective)
    will_bytes = sum(c.size_bytes for c in effective if c.size_bytes is not None)
    will_unknown = sum(1 for c in effective if c.size_bytes is None)
    movies = sum(1 for c in effective if c.media_type == "movie")
    seasons = sum(1 for c in effective if c.media_type == "season")

    # A hand reap is a net row the policy did not condemn on its own.
    hand_reaped_rows = [c for c in effective if c.verdict != "condemn"]
    hand_reaped = len(hand_reaped_rows)
    hand_reaped_bytes = sum(c.size_bytes for c in hand_reaped_rows if c.size_bytes is not None)
    hand_reaped_unknown = sum(1 for c in hand_reaped_rows if c.size_bytes is None)

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
        hand_reaped=hand_reaped,
        hand_reaped_bytes=hand_reaped_bytes,
        hand_reaped_unknown=hand_reaped_unknown,
        will_reap=will_reap,
        will_reap_bytes=will_bytes,
        will_reap_unknown=will_unknown,
        movies=movies,
        seasons=seasons,
        condemned_by=condemned_by,
    )
