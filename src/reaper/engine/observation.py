# SPDX-License-Identifier: AGPL-3.0-or-later
"""What we know, what isn't there, and what we couldn't find out.

Three states, and conflating the last two is how media-pruning tools delete
things they shouldn't:

* ``Known(value)``  -- we asked, and here is the answer.
* ``Absent``        -- we asked, and there genuinely is no value. *Nobody watched
  this.* That is real evidence, and it may condemn.
* ``Unknown(why)``  -- we could not ask. Tautulli timed out; the IMDb dataset is
  stale; Plex has not matched the item. This is **not** evidence of anything, and
  it may never condemn.

The distinction is why this module exists. An empty list from a *successful* query
means "nobody watched it". An empty list from a *failed* query means "we have no
idea" -- and they look identical if you only model presence and absence. Every
competitor that conflates them eventually deletes a beloved film during an API outage.

So: a failed call must produce ``Unknown``, never ``Absent`` and never ``[]``.
There is a test per adapter asserting exactly that.

``mypy --strict`` enforces exhaustive matching on all three arms, which turns
"unknown may only protect" from a convention someone will forget into a compile
error.
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
    """Which integration answered -- shown in the why-panel, because a number
    without provenance cannot be interpreted (Plex's `audience_rating` held IMDb on
    one server and Rotten Tomatoes on another)."""

    kind: Literal["known"] = "known"


@dataclass(frozen=True, slots=True)
class Absent:
    """We asked, and there is genuinely no value.

    Real evidence. "Nobody has ever played this" is *exactly* what we are looking
    for, so this arm is allowed to contribute condemnation pressure.
    """

    source: str
    kind: Literal["absent"] = "absent"


@dataclass(frozen=True, slots=True)
class Unknown:
    """We could not find out.

    Never evidence. An outage, a stale dataset, an unmatched item, a user whose
    history recording is disabled. This arm may only ever *protect*.
    """

    reason: str | Reason
    """A bare catalog id (``"imdb_unreadable"``), for a producer with no params to carry --
    ``gates.blocked_reason``/``fields.blocked_reason`` wrap it as ``Reason(f"cause.{reason}")``
    the same as always. A producer that needs a param (the movie/season media select on the
    match-status and no-added-at/no-size causes, ``gates.no_key_reason`` and friends) hands in
    the full ``Reason`` instead, already carrying the ``cause.`` id and its params, and the two
    wrappers above pass it through unchanged (rule 104: one encoding, whichever shape arrives).
    """
    source: str
    kind: Literal["unknown"] = "unknown"


type Observation[T] = Known[T] | Absent | Unknown
