# SPDX-License-Identifier: AGPL-3.0-or-later
"""What counts as a play, and when a viewing starts over.

Successor to the deleted ``engine/calibration.py``: one derivation module for both stages
of the rewatch plan, not two that could drift (rule 104). Movies only in this release; TV
is deferred behind its own validation (``docs/REWATCH_PLAN.md``, TV section).

This module freezes raw inputs only -- qualified viewing count, and the most recent
qualified play -- from an out-of-sample backtest against one heavy-rewatch library
(``docs/LEARNINGS.md``, "Frequency plus recency is the signal that survived";
``docs/REWATCH_PLAN.md``, Stage 1). Every play-derived count this feature adds goes through
:func:`qualifies`: unfiltered, over half of apparently cyclic titles in that backtest owed
their pattern to abandoned sub-50%-complete plays. Whether a title's frozen stats amount to
a rewatch habit is a policy-configurable bar decided in ``engine/signals.py``, not here, so
an operator's threshold edit replays against these frozen facts without a re-scan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clock import from_epoch
from reaper.db import KEY_CHUNK
from reaper.services import history_sync

#: A play more than this many days after the PREVIOUS play starts a new viewing. A gap of
#: exactly this many days shares the same viewing.
VIEWING_GAP_DAYS = 7


def qualifies(watched_status: float | None, percent_complete: int) -> bool:
    """Whether one ``watch_event`` row counts as a play.

    In order: a reported ``watched_status`` decides it (>= 0.5 qualifies, quantized
    against the operator's own Tautulli threshold); with no status, ``percent_complete``
    decides it (>= 50 qualifies); with both uninformative (no status, 0 percent complete)
    the play counts, because unknown resolves toward keeping. Matches
    ``season_scan.py``'s precedent that a NULL ``watched_status`` is "possibly watched,"
    never "not watched." Media-type filtering (movie or episode, never track) happens in
    the caller's SQL, not here.
    """
    if watched_status is not None:
        return watched_status >= 0.5
    if percent_complete == 0:
        return True
    return percent_complete >= 50


def viewing_count(play_times: Sequence[datetime]) -> int:
    """Cluster qualified plays (any user) into viewings.

    Sorted ascending; a play more than ``VIEWING_GAP_DAYS`` after the PREVIOUS play (not
    the viewing's start) opens a new viewing. Equal timestamps, and a gap of exactly
    ``VIEWING_GAP_DAYS``, share a viewing. Empty input is zero viewings.
    """
    if not play_times:
        return 0
    ordered = sorted(play_times)
    gap = timedelta(days=VIEWING_GAP_DAYS)
    viewings = 1
    previous = ordered[0]
    for played in ordered[1:]:
        if played - previous > gap:
            viewings += 1
        previous = played
    return viewings


@dataclass(frozen=True, slots=True)
class RewatchStats:
    """One movie's qualified viewing history, folded over any merged Plex listings."""

    viewings: int
    last_play: datetime | None
    """The most recent QUALIFIED play, never the most recent play of any kind."""


async def movie_rewatch_stats(
    engine: AsyncEngine,
    rating_keys: set[int],
    *,
    groups: Mapping[int, tuple[int, ...]] | None = None,
) -> dict[int, RewatchStats]:
    """Qualified viewing stats for movies in ``rating_keys``, from the local history mirror.

    ``groups`` maps a canonical rating key to every listing key of a merged Plex bind, the
    same mapping ``snapshot._fold_merged_watch_stats`` folds watch stats over: plays of any
    member count toward the canonical key, and are clustered together over the union
    (rule 72). A key absent from ``groups`` maps to itself. A key with at least one
    qualified play gets an entry; a caller reads a missing key as zero viewings.
    """
    if not rating_keys:
        return {}
    await history_sync.ensure_schema(engine)

    groups = groups or {}
    # Every listing key this scan needs rows for: each candidate's own key, plus every
    # other member of its merged group, mapped back to the canonical key so a play
    # recorded under any listing folds onto the one RewatchStats entry for the group.
    listing_to_canonical: dict[int, int] = {}
    for canonical in rating_keys:
        for member in groups.get(canonical) or (canonical,):
            listing_to_canonical[member] = canonical
    all_keys = sorted(listing_to_canonical)

    plays: dict[int, list[datetime]] = {}
    async with engine.connect() as conn:
        # Chunked on db.KEY_CHUNK, like every sibling that expands an IN over a
        # scan-sized key set (snapshot._fold_merged_watch_stats, rule 94).
        for start in range(0, len(all_keys), KEY_CHUNK):
            chunk = all_keys[start : start + KEY_CHUNK]
            rows = (
                await conn.execute(
                    text(
                        "SELECT rating_key, watched_at, watched_status, percent_complete "
                        "FROM watch_event WHERE media_type = 'movie' AND rating_key IN :keys"
                    ).bindparams(bindparam("keys", expanding=True)),
                    {"keys": chunk},
                )
            ).all()
            for row in rows:
                # The one filter this feature's play counts are allowed to use (module
                # docstring). Never re-expressed as a second SQL WHERE clause.
                if not qualifies(row.watched_status, int(row.percent_complete)):
                    continue
                played = from_epoch(row.watched_at)
                if played is None:
                    continue
                canonical = listing_to_canonical[int(row.rating_key)]
                plays.setdefault(canonical, []).append(played)

    return {
        key: RewatchStats(viewings=viewing_count(times), last_play=max(times))
        for key, times in plays.items()
    }
