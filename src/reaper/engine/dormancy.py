# SPDX-License-Identifier: AGPL-3.0-or-later
"""Count how many days an item has gone unwatched. Both scans use these helpers.

A never-played item has no "last played" date. Code that treats the missing date
as zero makes the item look unwatched since 1970, the strongest possible case
for deleting it. :func:`reference_instant` picks a real start date instead, such
as when the item was added.

:func:`dormancy_days` rounds down, so rounding always favors keeping: 9.9 days
counts as 9, and an item one hour short of a 90-day threshold stays kept.
"""

from __future__ import annotations

from datetime import datetime


def reference_instant(
    *, last_played: datetime | None, added_at: datetime | None, horizon: datetime
) -> datetime | None:
    """The instant dormancy is measured from, or ``None`` when there is none.

    Returns the last play if there is one. Otherwise it returns whichever is later:
    when the item arrived, or how far back the watch history reaches. It never returns
    a date before the watch history begins, because a history that only covers the last
    year cannot say an item has been ignored for five years. An item older than the
    history reads instead as "not watched within our reach."

    Every caller applies this same rule, so dormancy means the same thing everywhere it
    is measured. A record with neither a play nor an arrival date has nothing to measure
    from, so this returns ``None`` for it, and each caller reads that as Unknown: it
    blocks the dormancy gates and keeps the file. A play alone is enough to measure from,
    even with no arrival date, because dormancy is days since the last play.
    """
    if last_played is not None:
        return last_played
    if added_at is None:
        return None
    return max(added_at, horizon)


def dormancy_days(reference: datetime, *, now: datetime) -> int:
    """Whole days between ``reference`` and ``now``, rounded down.

    Returns a negative number when ``reference`` is in the future. Treat a negative
    result as unjudgeable, never as zero: a play after the cutoff means the evidence
    and the clock disagree, and scoring it as zero would score the item on that
    disagreement.
    """
    return (now - reference).days


def history_reach_days(horizon: datetime, *, now: datetime) -> int:
    """How many days of watch history we hold, counting back from ``horizon``.

    :func:`reference_instant` uses the horizon to stop dormancy from claiming more time
    than we can see. This answers the other half: how wide a window the history covers,
    which a watcher count needs to know before a count below the floor can be read as
    "nobody watched it" (``gates.ServerPopularityGate``).

    Rounded down like :func:`dormancy_days`, but for the opposite reason: shortening this
    number can only withhold a protection Reaper is not sure of. It can never grant one
    Reaper has not earned.
    """
    return (now - horizon).days
