# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deriving the rewatch prior from *this* server's own history.

**Engine-complete, not yet reachable.** :func:`derive` has no production caller: only
``engine.backtest`` imports it, and nothing routes, schedules or CLIs a backtest. So no
number an operator sees today comes from here, and no gate threshold is fitted by it --
``MinDormancyGate`` enforces the operator's own stored number, and the backtest falls
back to ``backtest.FALLBACK_REWATCH_PRIOR``. Nothing operator-facing may claim otherwise
until :func:`derive` is wired (rule 24). This note mirrors the one at the top of
``engine.backtest``, which was written and this module then went without.

The prior -- "how likely is a film dormant for N days to be watched again this year" --
is the baseline the scorer must beat. It is **not a constant**, and shipping it as one
is a bug: every library has its own rhythm. A household of three has a different curve
from a server with a hundred users.

## Two ways to get this catastrophically wrong

Both were made while building Reaper, and each produced a confident, articulate,
completely false conclusion.

**1. The wrong population.** The baseline must be computed over *exactly* the set the
scorer judges: items in the library, with a file, that the scorer could actually
condemn. Computing it over "everything that ever appeared in the watch log" also
includes **items long since deleted from the library**, which by definition are never
watched again -- and their structural zeroes drag every bucket down by a factor of two
to six. That miscalibration made a *working* scorer look like it had **-120% lift**.

**2. Too few samples.** A bucket with nine items yields a rate of 0%, 11%, 22%... and
none of those numbers mean anything. A prior built on noise produces a lift figure
built on noise, which is worse than no lift figure at all, because it looks like
evidence.

So: the population is passed in explicitly, and a bucket with too few samples is not
guessed at -- it is reported as uncalibrated, and lift is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clock import from_epoch
from reaper.engine.dormancy import dormancy_days, reference_instant

log = structlog.get_logger(__name__)

#: The dormancy buckets. Boundaries chosen to be dense where the curve moves.
BUCKETS: tuple[tuple[int, int], ...] = (
    (0, 365),
    (365, 548),
    (548, 730),
    (730, 1095),
    (1095, 1825),
    (1825, 10**9),
)

MIN_SAMPLES = 30
"""Below this, a bucket's rate is noise. It is reported as uncalibrated rather than
guessed at -- a prior built on noise produces a lift figure built on noise, which is
worse than none because it looks like evidence."""


class NotCalibratedError(RuntimeError):
    """There is not enough history to establish a baseline.

    Raised rather than returning a default, because a default here would silently
    become the number the owner tunes their thresholds against.
    """


@dataclass(frozen=True, slots=True)
class Bucket:
    low: int
    high: int
    samples: int
    rewatched: int

    @property
    def rate(self) -> float | None:
        """None when there is not enough evidence to say."""
        if self.samples < MIN_SAMPLES:
            return None
        return self.rewatched / self.samples

    @property
    def calibrated(self) -> bool:
        """Is this bucket safe to reason about?

        An **empty** bucket is fine -- it simply means no items are that old, and we
        will never be asked about one. A **thin** bucket is the danger: 9 items yields
        0%, 11%, 22%..., and none of those numbers mean anything, but they *look* like
        measurements.
        """
        return self.samples == 0 or self.samples >= MIN_SAMPLES

    @property
    def is_thin(self) -> bool:
        return 0 < self.samples < MIN_SAMPLES

    def label(self) -> str:
        high = "∞" if self.high >= 10**9 else str(self.high)
        return f"{self.low}-{high}d"


@dataclass(frozen=True, slots=True)
class RewatchPrior:
    """This server's own curve."""

    buckets: tuple[Bucket, ...]
    population: int
    window_days: int
    computed_at: datetime

    @property
    def calibrated(self) -> bool:
        """No bucket is *thin*.

        Empty buckets are fine -- nothing is that old, so nothing will ask. It is the
        buckets with a handful of items that lie convincingly.
        """
        return not any(b.is_thin for b in self.buckets)

    def rate_for(self, days_dormant: float) -> float:
        """The baseline probability for an item of this age.

        Raises rather than guessing. A default here would silently become the number
        the owner tunes their thresholds against.
        """
        for bucket in self.buckets:
            if bucket.low <= days_dormant < bucket.high:
                rate = bucket.rate
                if rate is None:
                    raise NotCalibratedError(
                        f"The {bucket.label()} bucket has only {bucket.samples} items "
                        f"(need {MIN_SAMPLES}). There is not enough history here to say "
                        "what a film of that age usually does, so lift cannot be "
                        "measured and no threshold should be trusted."
                    )
                return rate
        raise NotCalibratedError(f"No bucket covers {days_dormant:.0f} days.")

    def describe(self) -> str:
        lines = [
            f"Rewatch rates for your library ({self.population:,} items, "
            f"{self.window_days}-day window):",
        ]
        for bucket in self.buckets:
            rate = bucket.rate
            if rate is not None:
                shown = f"{rate:.0%}"
            elif bucket.samples == 0:
                shown = "-- none this old"
            else:
                shown = "-- too few to say"
            lines.append(f"  dormant {bucket.label():>12}  n={bucket.samples:>5,}  {shown}")
        if not self.calibrated:
            lines.append("")
            lines.append(
                "  ⚠ Some buckets are uncalibrated. Lift cannot be measured for items "
                "in those age ranges."
            )
        return "\n".join(lines)


async def derive(
    engine: AsyncEngine,
    *,
    rating_keys: set[int],
    cutoff: datetime,
    horizon: datetime,
    added_at: dict[int, datetime],
    media_type: str = "movie",
    window_days: int = 365,
) -> RewatchPrior:
    """Measure the rewatch curve for one server.

    ``rating_keys`` **must be exactly the population the scorer judges** -- items
    currently in the library, with a file on disk, that a policy could actually
    condemn. Passing "every key in the watch log" instead is the mistake that inverts
    the sign of every lift number downstream, because that set also contains items long
    since deleted, whose structural zeroes drag the whole curve down.

    It is a required argument, not a default, so it cannot be got wrong by omission.

    ``media_type`` is the POPULATION's type ("movie" or "tv"). For TV the join reads
    per-episode history through ``grandparent_rating_key`` -- see below.
    """
    if not rating_keys:
        raise NotCalibratedError("No items were supplied, so there is nothing to measure.")

    cutoff_epoch = int(cutoff.timestamp())
    window_end = int((cutoff + timedelta(days=window_days)).timestamp())

    # TV history rows are PER-EPISODE (media_type='episode', keyed by the episode) while
    # the population passes SHOW keys, so the join reads the episodes' grandparent key,
    # exactly like backtest._plays. Joining shows on rating_key would match nothing:
    # every show would read never-rewatched and the derived prior would claim a near-0%
    # baseline, inverting every lift number downstream.
    key_column, history_media_type = (
        ("grandparent_rating_key", "episode") if media_type == "tv" else ("rating_key", "movie")
    )

    async with engine.connect() as conn:
        before = {
            int(r.key): from_epoch(r.last)
            for r in (
                await conn.execute(
                    text(
                        f"SELECT {key_column} AS key, MAX(watched_at) AS last "  # noqa: S608 -- column from a literal two-way map
                        "FROM watch_event "
                        "WHERE media_type = :mt AND watched_at <= :cut "
                        f"GROUP BY {key_column}"
                    ),
                    {"mt": history_media_type, "cut": cutoff_epoch},
                )
            ).all()
        }
        after = {
            int(r.key)
            for r in (
                await conn.execute(
                    text(
                        f"SELECT DISTINCT {key_column} AS key FROM watch_event "  # noqa: S608 -- column from a literal two-way map
                        "WHERE media_type = :mt AND watched_at > :cut AND watched_at <= :end"
                    ),
                    {"mt": history_media_type, "cut": cutoff_epoch, "end": window_end},
                )
            ).all()
        }

    counts = {(lo, hi): [0, 0] for lo, hi in BUCKETS}

    for key in rating_keys:
        arrived = added_at.get(key)
        if arrived is None or arrived > cutoff:
            continue  # It did not exist yet. Judging it would be fiction.

        # The one dormancy derivation (engine/dormancy.py), shared with the live scan, the
        # season scan and the backtest -- so an item cannot bucket by one arithmetic here
        # and score by another there.
        reference = reference_instant(
            last_played=before.get(key), added_at=arrived, horizon=horizon
        )
        dormant = dormancy_days(reference, now=cutoff)
        if dormant < 0:
            continue

        for low, high in BUCKETS:
            if low <= dormant < high:
                counts[(low, high)][0] += 1
                if key in after:
                    counts[(low, high)][1] += 1
                break

    buckets = tuple(
        Bucket(low=lo, high=hi, samples=counts[(lo, hi)][0], rewatched=counts[(lo, hi)][1])
        for lo, hi in BUCKETS
    )
    prior = RewatchPrior(
        buckets=buckets,
        population=sum(b.samples for b in buckets),
        window_days=window_days,
        computed_at=cutoff,
    )

    log.info(
        "calibration.derived",
        population=prior.population,
        calibrated=prior.calibrated,
        rates=[b.rate for b in buckets],
    )
    return prior
