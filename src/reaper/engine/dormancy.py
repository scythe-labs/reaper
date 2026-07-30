# SPDX-License-Identifier: AGPL-3.0-or-later
"""How long an item has sat unwatched, derived in exactly one place.

``days_observed_unwatched`` is the single biggest source of deletion pressure in the whole
scorer, and it is *derived*, not read: "days since last play" is null for precisely the
items this tool exists to find, and every naive implementation coerces that null to epoch 0
-- roughly 20,600 days, the maximum pressure the scale can express, for the item we know
least about. So the reference instant is chosen deliberately (:func:`reference_instant`) and
the day count is floored (:func:`dormancy_days`).

The live scan, the season scan, the backtest and the prior calibration all answer the same
question, and each used to answer it in its own arithmetic. That is the dual derivation rule
3 warns about: calibration bucketed on an integer floor while the backtest scored a float, so
one item could bucket one side of a boundary and score the other. One helper, four callers.

**Floored, never rounded or ceiled** (rule 31). Dormancy sits on the condemn lane, so
reducing its precision must move it toward *less* pressure: 9.9 days dormant is 9, and an
item one hour short of a 90-day floor stays kept for that hour.
"""

from __future__ import annotations

from datetime import datetime
from typing import overload


@overload
def reference_instant(
    *, last_played: datetime | None, added_at: datetime, horizon: datetime
) -> datetime: ...


@overload
def reference_instant(
    *, last_played: datetime | None, added_at: datetime | None, horizon: datetime
) -> datetime | None: ...


def reference_instant(
    *, last_played: datetime | None, added_at: datetime | None, horizon: datetime
) -> datetime | None:
    """The instant dormancy is measured from, or ``None`` when there is no such instant.

    The last play if there is one. Failing that, whichever is *later* of when the item
    arrived and how far back the watch mirror reaches -- never epoch 0, and never a date
    before our evidence begins. A mirror that only goes back a year cannot honestly say a
    file has been ignored for five, so an item older than the mirror is measured from the
    mirror's edge and reads as "not watched within our reach".

    **The thaw lives here, not in the callers** (rule 104). A record with neither a play nor
    an arrival date has nothing to measure from, and this returns ``None`` for it so each
    lane renders one shared answer as its own ``Unknown`` -- which blocks both dormancy gates
    and abstains, so the item is kept. A play alone is enough: dormancy *is* days since the
    last play, so a missing arrival date is not on its own a reason to refuse to measure.

    That last sentence used to be true of one lane only. `snapshot.build_facts` took Unknown
    the moment ``added_at`` was missing, whatever history it held, while
    `season_scan.build_season_facts` measured from the play and judged the season -- one
    derived value with two thaw rules, and the movie lane's why-panel telling the operator
    dormancy could not be measured with a play for that item in scope. Measured before the
    lanes were joined: across 41 stored snapshots and ~[redacted] movie rows -- the only rows that
    can reach a movie-lane arm, out of [redacted] counted in all -- that arm was reached zero
    times, so no observed verdict moved (`docs/LEARNINGS.md`). The overloads let the two
    callers that narrow ``added_at`` themselves (`engine/backtest.py`,
    `engine/calibration.py`) keep a non-optional result without a None-check that cannot fire.
    """
    if last_played is not None:
        return last_played
    if added_at is None:
        return None
    return max(added_at, horizon)


def dormancy_days(reference: datetime, *, now: datetime) -> int:
    """Whole days between ``reference`` and ``now``, floored.

    Negative when ``reference`` is in the future, which callers treat as "cannot be judged"
    rather than clamping to zero: a play after the cutoff means the evidence and the clock
    disagree, and inventing a 0 there would score an item on a contradiction.
    """
    return (now - reference).days


def history_reach_days(horizon: datetime, *, now: datetime) -> int:
    """How many days of watch history we hold -- the *reach* of the evidence.

    :func:`reference_instant` uses the horizon to stop dormancy claiming more time than we
    can see. This answers the other half: how much of a *window* the mirror covers, which
    is what a windowed watcher count needs before a number below the floor may be read as
    "nobody watched it" (``gates.ServerPopularityGate``).

    Floored like :func:`dormancy_days`, and for the same reason read the other way round:
    reach sits on the *keep* lane, so shortening it can only withhold a protection Reaper
    is not sure of, never grant one it has not earned (rule 31).
    """
    return (now - horizon).days
