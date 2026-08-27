# SPDX-License-Identifier: AGPL-3.0-or-later
"""What we know, what genuinely isn't there, and what we couldn't find out.

Three states. Treating the last two as the same thing is how a media-pruning tool
deletes something it should not:

* ``Known(value)``: we asked, and here is the answer.
* ``Absent``: we asked, and there genuinely is no value. Nobody watched this.
  That is real evidence, and it may add deletion pressure.
* ``Unknown(why)``: we could not ask. Tautulli timed out, the IMDb dataset is
  stale, or Plex has not matched the item. This is not evidence of anything, and
  it may never add deletion pressure.

An empty list from a query that succeeded means "nobody watched it". An empty list
from a query that failed means "we have no idea". The two look the same if code
only tracks presence and absence, so a failed call must produce ``Unknown``, never
``Absent`` and never ``[]``. Each adapter has a test asserting exactly that.

``mypy --strict`` enforces exhaustive matching on all three states, so a place that
forgets to handle ``Unknown`` fails to type-check instead of shipping a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from reaper.engine.reason import Reason


@dataclass(frozen=True, slots=True)
class Known[T]:
    """We asked, and got an answer."""

    value: T
    source: str
    """Which integration answered. Shown in the why-panel, because a number means
    nothing without knowing where it came from: Plex's `audience_rating` field held
    IMDb ratings on one server and Rotten Tomatoes ratings on another."""

    kind: Literal["known"] = "known"


@dataclass(frozen=True, slots=True)
class Absent:
    """We asked, and there is genuinely no value.

    This is real evidence. "Nobody has ever played this" is exactly what raises
    the deletion score, so this state is allowed to do that.
    """

    source: str
    kind: Literal["absent"] = "absent"


@dataclass(frozen=True, slots=True)
class Unknown:
    """We could not find out.

    This is never evidence. An outage, a stale dataset, an unmatched item, or a
    user whose history recording is disabled all land here. This state may only
    ever protect a file, never add to its deletion score.
    """

    reason: str | Reason
    """Why we could not find out.

    A bare catalog id (``"imdb_unreadable"``) covers a producer with no extra values to
    carry: ``gates.blocked_reason``/``fields.blocked_reason`` wrap it as
    ``Reason(f"cause.{reason}")``. A producer that needs a value attached (for example,
    which wording to use for a movie versus a season, in ``gates.no_key_reason`` and
    similar functions) passes a full ``Reason`` instead, already carrying its id and
    values, and those wrappers pass it through unchanged.
    """
    source: str
    kind: Literal["unknown"] = "unknown"


type Observation[T] = Known[T] | Absent | Unknown
