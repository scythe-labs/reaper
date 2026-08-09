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

from reaper.clock import utcnow
from reaper.db.models import Candidate, Snapshot
from reaper.services import whitelist
from reaper.services.condemned import effective_condemned, held_reaps


@dataclass(frozen=True)
class SignalCount:
    """How many condemned titles one signal pushed toward removal."""

    id: str
    count: int


@dataclass(frozen=True)
class ReapBreakdown:
    has_snapshot: bool
    # The ledger. Counts are the reap decision (measured and unmeasured together); the byte
    # figures sum only what has a size, with the unmeasured carried as a separate count.
    policy_condemned: int
    policy_condemned_bytes: int
    hand_spared: int
    spares_expired: int
    """The share of ``hand_spared`` a scan would hand straight back to policy -- titles kept out
    of this plan by a spare whose clock has already passed.

    A spare's expiry is realized ONLY by a scan (``whitelist.purge_expired_spares``), so between
    the clock passing and the next scan the planner, this ledger and the executor all still read
    it and the file is genuinely kept. Reported so the Reap page can say why those titles are
    missing and that a scan releases them, instead of leaving them silently absent.

    Counted as "still spared after the purge?", via ``whitelist.without_expired_spares`` -- the
    same rule the scan judges on -- and not as "has its own clock passed?". The two differ where
    spares nest: a season spared for 10 days inside a show spared forever has an expired clock,
    but the purge deletes only the season's row and the show's spare keeps it anyway. That title
    is not one a scan would release, and the page must not promise it is.

    Titles, not spares: one whole-show spare holding five condemned seasons counts five, which
    is what the operator's copy says."""
    hand_reaped: int
    hand_reaped_bytes: int
    hand_reaped_held: int
    """Hand reaps the engine refuses to honor yet (a fired structural gate, or a row it
    cannot identify -- NOT merely evidence it could not check, which no longer holds), so
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
        hand_spared=0,
        spares_expired=0,
        hand_reaped=0,
        hand_reaped_bytes=0,
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

    # A hand spare removes a policy-condemned row from the reap set.
    spared_rows = [
        c for c in condemned_rows if whitelist.effective_override(c.media_key, decisions) == "spare"
    ]
    hand_spared = len(spared_rows)

    # ...and of those, the ones a scan would hand straight back to policy. Counted over the
    # SPARED condemned rows only, not every expired whitelist row, because the Reap page's
    # claim is "these are not in this plan" -- true only of a title the policy condemned and a
    # spare is holding back.
    #
    # The test is "would this still be spared after the purge", not "has its own clock passed":
    # the two differ when spares nest. A season spared for 10 days inside a show spared forever
    # has an expired clock of its own, but the scan deletes only the season's row and the show's
    # spare goes on keeping it -- so counting it would send the operator scanning for a title
    # that cannot move, which is exactly the false promise the notice exists to avoid (rule 61).
    # `without_expired_spares` is the same rule `overrides_effective_at` judges on, so this
    # count is what the next scan really releases.
    now = utcnow()
    expiries = await whitelist.spare_expiries(session)
    surviving = whitelist.without_expired_spares(decisions, expiries, now)
    spares_expired = sum(
        1 for c in spared_rows if whitelist.effective_override(c.media_key, surviving) != "spare"
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

    # The operator's reap marks the engine refuses to honor yet: counted (not dropped) so the
    # ledger can say "N of your reap marks are held" rather than silently under-report (PR-2).
    hand_reaped_held = len(await held_reaps(session, latest.id, decisions))

    # Participation over the frozen condemned rows: for each signal that added pressure, how
    # many condemned titles carry it. Overlapping by design. The per-signal byte totals this
    # used to accumulate beside the counts went with the wire fields nothing read.
    counts: dict[str, int] = {}
    for row in condemned_rows:
        for sid in _adds_signals(row.explanation_json):
            counts[sid] = counts.get(sid, 0) + 1
    condemned_by = [SignalCount(id=sid, count=counts[sid]) for sid in counts]
    condemned_by.sort(key=lambda s: (-s.count, s.id))

    return ReapBreakdown(
        has_snapshot=True,
        policy_condemned=policy_condemned,
        policy_condemned_bytes=policy_bytes,
        hand_spared=hand_spared,
        spares_expired=spares_expired,
        hand_reaped=hand_reaped,
        hand_reaped_bytes=hand_reaped_bytes,
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
