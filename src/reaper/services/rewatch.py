# SPDX-License-Identifier: AGPL-3.0-or-later
"""What counts as a play, and when a viewing starts over.

One derivation module for both stages of the rewatch plan, so the two cannot drift. TV's
own formulation has cleared its own validation (``docs/LEARNINGS.md``, "TV: the
replay-period formulation clears the lift bar") and is period-based, telling a rewatch
apart from a release binge (:func:`replay_period_count`), where the movie lane is a plain
viewing count (:func:`viewing_count`).

This module freezes raw inputs only: a qualified viewing count, and the most recent
qualified play, from an out-of-sample backtest against one heavy-rewatch library
(``docs/LEARNINGS.md``, "Frequency plus recency is the signal that survived", and
``docs/history/REWATCH_PLAN.md``, Stage 1). Every play-derived count here goes through
:func:`qualifies` first: in that backtest, over half of apparently cyclic titles owed
their pattern to abandoned plays under 50% complete. Whether a title's frozen stats amount
to a rewatch habit is a policy-configurable bar decided in ``engine/signals.py``, not here,
so an operator's threshold edit replays against these frozen facts without a re-scan.

Stage 2 adds the rewatch-probability fit: :func:`fit_blocks` buckets a movie population's
dormancy at cutoff against whether each one was watched again within a year. It refits
every scan and is never stored as a property of a title, because a stored rate would go
stale the moment the library changes shape. Movies deleted during the lookback year cannot
be counted, since there is no candidate row left to measure, which nudges every bucket's
rate up, the keep direction. That bias was measured and is strictly upward
(``docs/LEARNINGS.md``). This module does not correct for it, because the deletion dates
the correction would need are unknowable here, and guessing at them would push numbers
toward deletion on an assumption.
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
from reaper.services import history_sync

#: A play more than this many days after the PREVIOUS play starts a new viewing. A gap of
#: exactly this many days shares the same viewing.
VIEWING_GAP_DAYS = 7

#: A qualified episode play more than this many days after the PREVIOUS play starts a new
#: show-level viewing period. A gap of exactly this many days shares the same period. 30,
#: not ``VIEWING_GAP_DAYS``'s 7: a weekly airing run's 7-day spacing must bridge into one
#: period rather than fragment into one per episode (``docs/history/REWATCH_PLAN.md``, TV
#: section).
SHOW_PERIOD_GAP_DAYS = 30


def qualifies(watched_status: float | None, percent_complete: int) -> bool:
    """Whether one ``watch_event`` row counts as a play.

    ``watched_status`` decides it first, when Tautulli reported one: 0.5 or higher
    qualifies. With no status, ``percent_complete`` decides instead: 50 or higher
    qualifies. With neither informative (no status, and 0 percent complete), the play
    still counts, because an unknown value resolves toward keeping. This matches
    ``season_scan.py``'s rule that a missing ``watched_status`` reads as "possibly
    watched," never as "not watched." Filtering by media type (movie or episode, never
    track) happens in the caller's SQL, not here.
    """
    if watched_status is not None:
        return watched_status >= 0.5
    if percent_complete == 0:
        return True
    return percent_complete >= 50


def viewing_count(play_times: Sequence[datetime]) -> int:
    """Cluster qualified plays (any user) into viewings.

    Plays are sorted ascending. A play more than ``VIEWING_GAP_DAYS`` after the PREVIOUS
    play, not the viewing's start, opens a new viewing. Equal timestamps, and a gap of
    exactly ``VIEWING_GAP_DAYS``, share a viewing. Empty input is zero viewings.
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


def replay_period_count(plays: Sequence[tuple[datetime, int]]) -> int:
    """Count replay periods among a show's qualified episode plays.

    Each element is ``(play time, episode identity)``. Episode identity is the episode's
    own Plex rating key. The fallback of ``(parent_rating_key, media_index)`` is not
    implemented here, because ``watch_event.rating_key`` is ``NOT NULL``
    (``services/history_sync.py``'s schema) and live validation found every episode row
    carrying its own key, so no fallback is needed (``docs/LEARNINGS.md``, TV entry, last
    bullet).

    Plays are sorted ascending, then clustered into periods by
    :data:`SHOW_PERIOD_GAP_DAYS`: a play more than that many days after the PREVIOUS play,
    not the period's start, opens a new period. :func:`viewing_count` applies the same
    rule to movies.

    A period counts as a replay period when it holds at least two distinct episodes and
    at least a quarter of its distinct episodes were already played in an EARLIER period
    (``seen``, the union over every period walked so far). The quarter is inclusive,
    computed as ``4 * overlap >= distinct`` in integer arithmetic rather than a float
    division, so 1 of 4 replays but 1 of 5 does not. This threshold is what tells a
    rewatch apart from a viewer simply following new episodes as they air
    (``docs/history/REWATCH_PLAN.md``, TV section, and ``docs/LEARNINGS.md``, TV entry): a
    period below either floor reads as following new episodes, never as a replay.

    Returns the count of replay periods. Empty input is zero.
    """
    if not plays:
        return 0
    ordered = sorted(plays, key=lambda play: play[0])
    gap = timedelta(days=SHOW_PERIOD_GAP_DAYS)

    periods: list[set[int]] = [{ordered[0][1]}]
    previous = ordered[0][0]
    for played, episode_key in ordered[1:]:
        if played - previous > gap:
            periods.append(set())
        periods[-1].add(episode_key)
        previous = played

    seen: set[int] = set()
    replay_periods = 0
    for period in periods:
        distinct = len(period)
        overlap = len(period & seen)
        if distinct >= 2 and 4 * overlap >= distinct:
            replay_periods += 1
        seen |= period
    return replay_periods


@dataclass(frozen=True, slots=True)
class RewatchStats:
    """One title's qualified viewing history: a movie's plain viewing count (folded over
    any merged Plex listings, :func:`movie_rewatch_stats`) or a show's replay-period count
    (:func:`show_rewatch_stats`)."""

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
    same mapping ``snapshot._fold_merged_watch_stats`` folds watch stats over. Plays of
    any member count toward the canonical key, and are clustered together over the union.
    A key absent from ``groups`` maps to itself. A key with at least one qualified play
    gets an entry, and a caller reads a missing key as zero viewings.
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
        # Chunked on db.KEY_CHUNK, like every sibling that expands an IN clause over a
        # scan-sized key set (snapshot._fold_merged_watch_stats).
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
                # The one filter this feature's play counts use, per the module
                # docstring. It stays here rather than in a second SQL WHERE clause,
                # so the two can never disagree.
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


async def show_rewatch_stats(engine: AsyncEngine, show_keys: set[int]) -> dict[int, RewatchStats]:
    """Qualified viewing stats for shows in ``show_keys``, from the local history mirror.

    Mirrors :func:`movie_rewatch_stats`'s shape, with two differences. It takes no
    ``groups`` parameter, following the same precedent as the TV lane's per-season watch
    stats (``season_scan.season_watch_stats``), which also do not fold merged Plex
    listings. If TV's watch stats ever gain a merge fold, this function should gain one
    too. And a show's ``viewings`` is :func:`replay_period_count`'s replay-period count,
    not a plain viewing count, per the TV formulation's validation
    (``docs/history/REWATCH_PLAN.md``, TV section).

    A show key with at least one qualified play gets an entry, and a caller reads a
    missing key as zero viewings, the same contract as the movie function above.
    """
    if not show_keys:
        return {}
    await history_sync.ensure_schema(engine)

    all_keys = sorted(show_keys)
    plays: dict[int, list[tuple[datetime, int]]] = {}
    async with engine.connect() as conn:
        # Chunked on db.KEY_CHUNK, like every sibling that expands an IN clause over a
        # scan-sized key set (movie_rewatch_stats above).
        for start in range(0, len(all_keys), KEY_CHUNK):
            chunk = all_keys[start : start + KEY_CHUNK]
            rows = (
                await conn.execute(
                    text(
                        "SELECT grandparent_rating_key, rating_key, watched_at, "
                        "watched_status, percent_complete FROM watch_event "
                        "WHERE media_type = 'episode' AND grandparent_rating_key IN :keys"
                    ).bindparams(bindparam("keys", expanding=True)),
                    {"keys": chunk},
                )
            ).all()
            for row in rows:
                # The one filter this feature's play counts use, per the module
                # docstring. It stays here rather than in a second SQL WHERE clause,
                # so the two can never disagree.
                if not qualifies(row.watched_status, int(row.percent_complete)):
                    continue
                played = from_epoch(row.watched_at)
                if played is None:
                    continue
                show_key = int(row.grandparent_rating_key)
                plays.setdefault(show_key, []).append((played, int(row.rating_key)))

    return {
        key: RewatchStats(
            viewings=replay_period_count(show_plays),
            last_play=max(played for played, _ in show_plays),
        )
        for key, show_plays in plays.items()
    }


# ---------------------------------------------------------------------------
# Stage 2: the rewatch-probability fit
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RewatchBlock:
    """One pooled dormancy block from the Stage 2 fit: a half-open ``(lo_days, hi_days]``
    range (``hi_days=None`` is the open tail) and the cohort measured in it.

    ``n`` and ``k`` are the pooled counts after :func:`fit_blocks`'s monotonicity merge. A
    block born from several source buckets carries their combined counts and outer range,
    not any one source bucket's own. The rate is ``k / n``, derived on the fly and never
    stored separately (``docs/history/REWATCH_PLAN.md``, Stage 2)."""

    lo_days: float
    hi_days: float | None
    n: int
    k: int


#: The whole fitted curve for one scan: every surviving block, ascending by ``lo_days``.
#: Refit every scan and never persisted as a property of a title (module docstring).
type RewatchCurve = tuple[RewatchBlock, ...]


#: Bucket edges for the point estimate, half-open ``(lo, hi]`` (docs/history/REWATCH_PLAN.md, Stage
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
    watched_again)`` pairs (docs/history/REWATCH_PLAN.md, Stage 2 Fit).

    Pure. Every pair is already computed by the caller (:func:`training_pair`), so this
    function touches no database and no clock, and polices no candidate-set boundary: that
    belongs to :func:`movie_rewatch_outcomes`, which is where the plan pins it.

    Buckets are half-open on the fixed edges above, with a point estimate of ``k / n`` per
    bucket. Empty buckets are dropped before merging. Adjacent buckets are then merged
    wherever the rate rises with more dormancy, using the pool-adjacent-violators method
    run left to right on a stack: a merge can itself violate the block below it, so each
    merge re-checks the new top before moving on. Without this merge, a threshold could
    protect a more-dormant bucket while skipping a less-dormant one right beside it.
    """
    # The open tail is appended as its own (lo, None) pair rather than folded into
    # `edges`. A trailing `None` there would type every edge `float | None`, including
    # the near ones that are never open, and `itertools.pairwise` has no way to say only
    # the last element of a pair may be it.
    bounds: list[tuple[float, float | None]] = list(itertools.pairwise(_BUCKET_EDGES))
    bounds.append((_BUCKET_EDGES[-1], None))
    raw: list[RewatchBlock] = []
    for lo, hi in bounds:
        n = 0
        k = 0
        for days, watched_again in pairs:
            # The first bucket is closed at zero. A dormancy of exactly 0 days is a title
            # played the day of the cutoff, and if this bucket were open at zero, the
            # title watched most recently of all would drop out of the fit entirely.
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
    return tuple(pooled)


def block_for(curve: RewatchCurve, dormancy_days: float) -> RewatchBlock | None:
    """Which (merged) block a CURRENT dormancy falls in, or ``None`` when nothing in the
    fitted curve covers it: past the fitted range, or inside a bucket that was dropped
    empty at fit time and never absorbed into a neighbor."""
    for block in curve:
        # Same closed-at-zero first edge as the fit's bucketing above. The two must agree,
        # or a just-watched title trains the curve and then reads back as unmeasured.
        if (dormancy_days > block.lo_days or (block.lo_days == 0 and dormancy_days == 0)) and (
            block.hi_days is None or dormancy_days <= block.hi_days
        ):
            return block
    return None


def cohort_block(
    curve: RewatchCurve, dormancy_days: float, *, reach_days: float
) -> RewatchBlock | None:
    """The current item's usable block, or ``None`` when there is nothing to report: past
    the fitted range, a dropped bucket, or one deeper than the watch mirror reaches.

    A block is withheld until history grows into it when its NEAR edge is at or past the
    mirror's reach (``Facts.history_reach_days``, ``services.history_sync.horizon``). The
    mirror cannot have observed a real outcome that far back, so a block starting there is
    not evidence yet, whatever its point estimate says (docs/history/REWATCH_PLAN.md,
    Stage 2: "Floor").

    This is the one place the lookup and the withhold are combined, so every reader, the
    fact builder and the explanation writer alike, gets the identical decision over the
    identical curve and dormancy, rather than two hand-written copies that could disagree.
    """
    block = block_for(curve, dormancy_days)
    if block is None or block.lo_days >= reach_days:
        return None
    return block


#: Why the rewatch-probability cohort has nothing to show: no fit ran this scan (an empty
#: population), this dormancy falls outside the fitted range or inside a bucket dropped
#: empty at fit time, or its block is withheld until the mirror's reach grows into it. One
#: reason covers all four, because the operator's takeaway is the same either way: no
#: number to show (docs/history/REWATCH_PLAN.md, Stage 2). It lives here rather than in a
#: per-lane module so both lanes' fact builders, movies' ``snapshot.build_facts`` and TV's
#: once its cohort fields read a fit, name the same reason instead of each growing its own
#: spelling.
#:
#: This is named by the usual ``*_REASON`` convention, but it has no route to the
#: why-panel's CAUSE slot. ``rewatch_cohort_n``/``rewatch_cohort_k`` feed only the
#: ``rewatch_odds`` context block (``snapshot._rewatch_odds_context``), read by its typed
#: ``state``, never by this reason text. ``tests/test_review_chips.py``'s
#: ``_NO_PANEL_ROUTE`` checks that claim rather than assuming it.
NO_REWATCH_ESTIMATE_REASON = "no_rewatch_estimate"


@dataclass(frozen=True, slots=True)
class RewatchOutcome:
    """One title's raw Stage 2 training inputs, before dormancy is derived against a cutoff."""

    last_play_at_or_before_cutoff: datetime | None
    watched_again: bool
    """Any play at all, any user, any completion, in the 365 days after the cutoff."""


async def movie_rewatch_outcomes(
    engine: AsyncEngine,
    rating_keys: set[int],
    *,
    cutoff: datetime,
    groups: Mapping[int, tuple[int, ...]] | None = None,
) -> dict[int, RewatchOutcome]:
    """Per-movie training inputs for the Stage 2 fit, from the local history mirror.

    Unlike :func:`movie_rewatch_stats`, this counts every play, any completion: the fit's
    dormancy anchor and outcome are any-play, not the stage 1 keep's qualified filter
    (docs/history/REWATCH_PLAN.md, Stage 2 Fit, "any user, any completion"). Chunked on
    ``db.KEY_CHUNK`` like the stage 1 function above, and folds a merged Plex bind's
    listings onto its canonical key the same way (``groups``). A key with no rows in
    either window is absent from the result, and a caller reads a missing key as "no play
    near the cutoff either side".
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
        # Chunked on db.KEY_CHUNK, like every sibling that expands an IN clause over a
        # scan-sized key set (movie_rewatch_stats above).
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
                # This counts any play, any completion, with no `qualifies()` filter,
                # unlike every play-derived count stage 1 added (see the docstring).
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


async def show_rewatch_outcomes(
    engine: AsyncEngine,
    show_keys: set[int],
    *,
    cutoff: datetime,
) -> dict[int, RewatchOutcome]:
    """Per-show training inputs for the Stage 2 fit, from the local history mirror.

    Mirrors :func:`movie_rewatch_outcomes`'s shape. It takes no ``groups`` parameter,
    following the same precedent as :func:`show_rewatch_stats` above.

    Unlike :func:`show_rewatch_stats`, this counts every play, any completion: the fit's
    dormancy anchor and outcome are any-play, not the stage 1 keep's qualified filter
    (docs/history/REWATCH_PLAN.md, Stage 2 Fit, "any user, any completion"). Chunked on
    ``db.KEY_CHUNK`` like the movie function above. A key with no rows in either window is
    absent from the result, and a caller reads a missing key as "no play near the cutoff
    either side".
    """
    if not show_keys:
        return {}
    await history_sync.ensure_schema(engine)

    all_keys = sorted(show_keys)
    cutoff_epoch = int(cutoff.timestamp())
    outcome_end_epoch = int((cutoff + timedelta(days=365)).timestamp())

    last_before: dict[int, datetime] = {}
    watched_again: set[int] = set()
    async with engine.connect() as conn:
        # Chunked on db.KEY_CHUNK, like every sibling that expands an IN clause over a
        # scan-sized key set (movie_rewatch_outcomes above).
        for start in range(0, len(all_keys), KEY_CHUNK):
            chunk = all_keys[start : start + KEY_CHUNK]
            rows = (
                await conn.execute(
                    text(
                        "SELECT grandparent_rating_key, watched_at FROM watch_event "
                        "WHERE media_type = 'episode' AND grandparent_rating_key IN :keys"
                    ).bindparams(bindparam("keys", expanding=True)),
                    {"keys": chunk},
                )
            ).all()
            for row in rows:
                # This counts any play, any completion, with no `qualifies()` filter,
                # unlike every play-derived count stage 1 added (see the docstring).
                played_epoch = int(row.watched_at)
                show_key = int(row.grandparent_rating_key)
                if played_epoch <= cutoff_epoch:
                    played = from_epoch(played_epoch)
                    if played is not None and (
                        show_key not in last_before or played > last_before[show_key]
                    ):
                        last_before[show_key] = played
                elif played_epoch <= outcome_end_epoch:
                    watched_again.add(show_key)

    return {
        key: RewatchOutcome(
            last_play_at_or_before_cutoff=last_before.get(key),
            watched_again=key in watched_again,
        )
        for key in show_keys
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

    Dormancy at cutoff is cutoff minus the last play at or before cutoff
    (docs/history/REWATCH_PLAN.md, Stage 2 Fit). Failing a play, it falls back to cutoff
    minus the added date, and only when the added date is itself at or before cutoff: an
    item added inside the lookback year has no honest measurement at this cutoff and
    would otherwise fit a negative dormancy. Stated plainly, the population rule is that
    an item with an unknown (or too-recent) added date and no play at or before cutoff is
    withheld, never fitted.

    The day count itself goes through the shared ``engine.dormancy.dormancy_days``, with
    ``cutoff`` standing in for "now". ``outcome=None`` reads as no play near the cutoff
    either side, exactly as a missing key from :func:`movie_rewatch_outcomes` does.
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
