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

Stage 2 adds the rewatch-PROBABILITY fit: :func:`fit_blocks` buckets a movie population's
dormancy-at-cutoff against whether each one was watched again within a year, refit every
scan and never persisted as a property of a title -- year-over-year movement of a few points
per bucket is normal, and a stored rate would go stale the moment the library changes shape.
Movies deleted during the lookback year are uncountable by construction (there is no
candidate row left to measure), which nudges every bucket's rate up, the keep direction
(rule 31's spirit). That bias was measured and is strictly upward (``docs/LEARNINGS.md``);
this module does not attempt a ghost-corrected rate, because the deletion dates it would
need are unknowable here and the correction would push numbers in the deletion direction on
an assumption.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clock import from_epoch
from reaper.db import KEY_CHUNK
from reaper.engine.dormancy import dormancy_days
from reaper.engine.gates import REWATCH_BLOCK_FLOOR_N
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


# ---------------------------------------------------------------------------
# Stage 2: the rewatch-probability fit (#554)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RewatchBlock:
    """One pooled dormancy block from the Stage 2 fit: a half-open ``(lo_days, hi_days]``
    range (``hi_days=None`` is the open tail) and the cohort measured in it.

    ``n`` and ``k`` are the pooled counts after :func:`fit_blocks`'s monotonicity merge --
    a block born from several source buckets carries their combined counts and outer range,
    not any one bucket's own. The rate is ``k / n``, derived and never stored separately
    (``docs/REWATCH_PLAN.md``, Stage 2)."""

    lo_days: float
    hi_days: float | None
    n: int
    k: int


@dataclass(frozen=True, slots=True)
class RewatchCurve:
    """The whole fitted curve for one scan: every surviving block, ascending by ``lo_days``.

    Refit every scan and never persisted as a property of a title (module docstring)."""

    blocks: tuple[RewatchBlock, ...]


#: Bucket edges for the point estimate, half-open ``(lo, hi]`` (docs/REWATCH_PLAN.md, Stage
#: 2 Fit). The open tail past the last edge is the sentinel ``None`` appended in
#: :func:`fit_blocks`, never a literal number, so nothing above 1825 days is silently
#: excluded from the fit.
_BUCKET_EDGES: tuple[float, ...] = (0.0, 365.0, 548.0, 730.0, 1095.0, 1825.0)


def _rate(block: RewatchBlock) -> float:
    return block.k / block.n


def _pool(a: RewatchBlock, b: RewatchBlock) -> RewatchBlock:
    """Merge two adjacent blocks: the pooled counts, and the outer dormancy range."""
    return RewatchBlock(lo_days=a.lo_days, hi_days=b.hi_days, n=a.n + b.n, k=a.k + b.k)


def fit_blocks(pairs: Sequence[tuple[float, bool]]) -> RewatchCurve:
    """Fit the rewatch-probability curve over training ``(dormancy_days_at_cutoff,
    watched_again)`` pairs (docs/REWATCH_PLAN.md, Stage 2 Fit).

    Pure: every pair is already computed by the caller (:func:`training_pair`), so this has
    no database, no clock, and no candidate-set boundary to police -- that trap belongs to
    :func:`movie_rewatch_outcomes` and is pinned there, per the plan.

    Buckets half-open on the fixed edges above; point estimate ``k / n`` per bucket; empty
    buckets are dropped before merging. Then adjacent buckets are merged wherever the rate
    RISES with more dormancy (pool-adjacent-violators, run left to right on a stack): a
    merge can itself violate against the block below it, so each merge re-checks the new
    top before moving on. Without the merge, a threshold hold could protect a more-dormant
    bucket while skipping a less-dormant one right beside it.
    """
    # The open tail is appended as its own (lo, None) pair rather than folded into `edges`
    # itself: a trailing `None` there would type every edge `float | None`, including the
    # near ones that are never open, and `itertools.pairwise` has no way to say only the
    # last element of a pair may be it.
    bounds: list[tuple[float, float | None]] = list(itertools.pairwise(_BUCKET_EDGES))
    bounds.append((_BUCKET_EDGES[-1], None))
    raw: list[RewatchBlock] = []
    for lo, hi in bounds:
        n = 0
        k = 0
        for days, watched_again in pairs:
            # The first bucket is closed at zero: a dormancy of exactly 0 days is a title
            # played the day of the cutoff, and a strict (0, 365] dropped it from the fit
            # while `block_for` told its panel "not enough watch history" about the one
            # title watched most recently of all (found on live data, 18 of ~3,500).
            if (days > lo or (lo == 0 and days == 0)) and (hi is None or days <= hi):
                n += 1
                k += 1 if watched_again else 0
        if n > 0:
            raw.append(RewatchBlock(lo_days=lo, hi_days=hi, n=n, k=k))

    pooled: list[RewatchBlock] = []
    for block in raw:
        pooled.append(block)
        while len(pooled) > 1 and _rate(pooled[-1]) > _rate(pooled[-2]):
            b = pooled.pop()
            a = pooled.pop()
            pooled.append(_pool(a, b))
    return RewatchCurve(blocks=tuple(pooled))


def block_for(curve: RewatchCurve, dormancy_days: float) -> RewatchBlock | None:
    """Which (merged) block a CURRENT dormancy falls in, or ``None`` when nothing in the
    fitted curve covers it: past the fitted range, or inside a bucket that was dropped
    empty at fit time and never absorbed into a neighbor."""
    for block in curve.blocks:
        # Same closed-at-zero first edge as the fit's bucketing above (rule 104: the two
        # must agree, or a just-watched title trains the curve and then reads as unmeasured).
        if (dormancy_days > block.lo_days or (block.lo_days == 0 and dormancy_days == 0)) and (
            block.hi_days is None or dormancy_days <= block.hi_days
        ):
            return block
    return None


#: A (possibly merged) block under this cohort size displays no number and can never fire
#: the opt-in protective hold (docs/REWATCH_PLAN.md, Stage 2: "Floor"). One declaration,
#: re-exported: the engine's ``RewatchOddsGate`` reads the same bar and an engine module may
#: not import a service, so the number lives in ``engine/gates.py``.
BLOCK_FLOOR_N = REWATCH_BLOCK_FLOOR_N


def block_withheld(block: RewatchBlock, reach_days: float) -> bool:
    """Whether ``block`` sits deeper than the watch mirror reaches, so it is withheld until
    history grows into it (docs/REWATCH_PLAN.md, Stage 2: "A block deeper than the mirror
    reaches is withheld until history grows into it").

    True when the block's NEAR edge is at or past the mirror's reach
    (``Facts.history_reach_days``, ``services.history_sync.horizon``): the mirror cannot
    have observed a real outcome that far back, so a block starting there is not evidence
    yet, whatever its point estimate says.
    """
    return block.lo_days >= reach_days


def cohort_block(
    curve: RewatchCurve, dormancy_days: float, *, reach_days: float
) -> RewatchBlock | None:
    """The current item's usable block, or ``None`` when there is nothing to report: past
    the fitted range, a dropped bucket, or one :func:`block_withheld` holds back.

    The one place :func:`block_for` and :func:`block_withheld` are combined (rule 104), so
    every reader -- the fact builder and the explanation writer alike -- gets the identical
    decision over the identical curve and dormancy, rather than two hand-written copies
    that could disagree.
    """
    block = block_for(curve, dormancy_days)
    if block is None or block_withheld(block, reach_days):
        return None
    return block


@dataclass(frozen=True, slots=True)
class RewatchOutcome:
    """One movie's raw Stage 2 training inputs, before dormancy is derived against a cutoff."""

    last_play_at_or_before_cutoff: datetime | None
    watched_again: bool
    """Any play at all -- any user, any completion -- in the 365 days after the cutoff."""


async def movie_rewatch_outcomes(
    engine: AsyncEngine,
    rating_keys: set[int],
    *,
    cutoff: datetime,
    groups: Mapping[int, tuple[int, ...]] | None = None,
) -> dict[int, RewatchOutcome]:
    """Per-movie training inputs for the Stage 2 fit, from the local history mirror.

    Unlike :func:`movie_rewatch_stats`, this counts EVERY play, any completion: the fit's
    dormancy anchor and outcome are any-play, not the stage 1 keep's qualified filter
    (docs/REWATCH_PLAN.md, Stage 2 Fit -- "any user, any completion", unlike the stage 1
    keep's qualified filter). Chunked on ``db.KEY_CHUNK`` like the stage 1 sibling above
    (rule 94), and folds a merged Plex bind's listings onto its canonical key the same way
    (``groups``). A key with no rows in either window is absent from the result; a caller
    reads a missing key as "no play near the cutoff either side".
    """
    if not rating_keys:
        return {}
    await history_sync.ensure_schema(engine)

    groups = groups or {}
    # Same fold as movie_rewatch_stats above: every listing key this scan needs rows for,
    # mapped back to the canonical key a merged bind's plays are folded onto.
    listing_to_canonical: dict[int, int] = {}
    for canonical in rating_keys:
        for member in groups.get(canonical) or (canonical,):
            listing_to_canonical[member] = canonical
    all_keys = sorted(listing_to_canonical)

    cutoff_epoch = int(cutoff.timestamp())
    outcome_end_epoch = int((cutoff + timedelta(days=365)).timestamp())

    last_before: dict[int, datetime] = {}
    watched_again: set[int] = set()
    async with engine.connect() as conn:
        # Chunked on db.KEY_CHUNK, like every sibling that expands an IN over a
        # scan-sized key set (movie_rewatch_stats above, rule 94).
        for start in range(0, len(all_keys), KEY_CHUNK):
            chunk = all_keys[start : start + KEY_CHUNK]
            rows = (
                await conn.execute(
                    text(
                        "SELECT rating_key, watched_at FROM watch_event "
                        "WHERE media_type = 'movie' AND rating_key IN :keys"
                    ).bindparams(bindparam("keys", expanding=True)),
                    {"keys": chunk},
                )
            ).all()
            for row in rows:
                # No `qualifies()` filter here -- see the docstring: this counts any play,
                # any completion, unlike every play-derived count stage 1 added.
                played_epoch = int(row.watched_at)
                canonical = listing_to_canonical[int(row.rating_key)]
                if played_epoch <= cutoff_epoch:
                    played = from_epoch(played_epoch)
                    if played is not None and (
                        canonical not in last_before or played > last_before[canonical]
                    ):
                        last_before[canonical] = played
                elif played_epoch <= outcome_end_epoch:
                    watched_again.add(canonical)

    return {
        key: RewatchOutcome(
            last_play_at_or_before_cutoff=last_before.get(key),
            watched_again=key in watched_again,
        )
        for key in rating_keys
        if key in last_before or key in watched_again
    }


def training_pair(
    outcome: RewatchOutcome | None,
    *,
    added_at: datetime | None,
    cutoff: datetime,
) -> tuple[float, bool] | None:
    """One candidate's ``(dormancy_days_at_cutoff, watched_again)`` training pair, or
    ``None`` when it is withheld from the population.

    docs/REWATCH_PLAN.md, Stage 2 Fit: dormancy at cutoff is cutoff minus the last play at
    or before cutoff; failing a play, cutoff minus the added date -- and only when the added
    date is itself at or before cutoff, since an item added inside the lookback year has no
    honest measurement at this cutoff and would otherwise fit a negative dormancy. That is
    the population rule stated plainly: an item with an unknown (or too-recent) added date
    and no play at or before cutoff is withheld, never fitted.

    The day count itself goes through the shared ``engine.dormancy.dormancy_days`` (rule
    104), with ``cutoff`` standing in for "now". ``outcome=None`` reads as no play near the
    cutoff either side, exactly as a missing key from :func:`movie_rewatch_outcomes` does.
    """
    last_play = outcome.last_play_at_or_before_cutoff if outcome is not None else None
    if last_play is not None:
        reference = last_play
    elif added_at is not None and added_at <= cutoff:
        reference = added_at
    else:
        return None
    watched_again = outcome.watched_again if outcome is not None else False
    return float(dormancy_days(reference, now=cutoff)), watched_again
