# SPDX-License-Identifier: AGPL-3.0-or-later
"""Calibration: deriving the rewatch prior from the owner's OWN history.

The prior is the baseline the scorer must beat. Shipping it as a constant is a bug:
every library has its own rhythm, and a household of three has nothing in common with
a server used by a hundred people.

Two ways this goes catastrophically wrong, both of which happened while building
Reaper:

1. **The wrong population** — computing the baseline over a different set than the
   scorer judges. Inverts the sign of every lift number downstream.
2. **Too few samples** — a bucket of nine items yields 0%, 11%, 22%... none of which
   mean anything. A lift figure built on noise is worse than none, because it looks
   like evidence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.session import create_engine
from reaper.engine import backtest
from reaper.engine.calibration import (
    MIN_SAMPLES,
    Bucket,
    NotCalibratedError,
    derive,
)
from reaper.services.history_sync import SCHEMA

NOW = utcnow()
CUTOFF = NOW - timedelta(days=365)
HORIZON = NOW - timedelta(days=3000)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))  # type: ignore[call-arg]
    async with eng.begin() as conn:
        for statement in SCHEMA.strip().split(";"):
            if statement.strip():
                await conn.execute(text(statement))
    yield eng
    await eng.dispose()


async def _play(engine: AsyncEngine, row_id: int, rating_key: int, when: datetime) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO watch_event (row_id, rating_key, user_id, watched_at, "
                "watched_status, percent_complete, media_type) "
                "VALUES (:id, :rk, 1, :at, 1.0, 100, 'movie')"
            ),
            {"id": row_id, "rk": rating_key, "at": int(when.timestamp())},
        )


class TestDerive:
    async def test_it_measures_the_rewatch_rate_per_bucket(self, engine: AsyncEngine) -> None:
        """40 films last played ~1,200 days before the cutoff. 10 of them get watched
        again in the year after. The 1095-1825 bucket should read 25%."""
        keys = set(range(1, 41))
        added = {k: NOW - timedelta(days=2500) for k in keys}

        row = 0
        for k in keys:
            row += 1
            await _play(engine, row, k, CUTOFF - timedelta(days=1200))
        for k in list(keys)[:10]:
            row += 1
            await _play(engine, row, k, CUTOFF + timedelta(days=30))  # rewatched

        prior = await derive(
            engine, rating_keys=keys, cutoff=CUTOFF, horizon=HORIZON, added_at=added
        )

        assert prior.rate_for(1200) == pytest.approx(0.25)
        assert prior.calibrated is True

    async def test_a_never_played_film_counts_from_when_it_arrived(
        self, engine: AsyncEngine
    ) -> None:
        """Not from epoch 0. The trap: 'days since last play' is null for exactly the
        items we care most about, and coercing that to 1970 reads as ~20,600 days
        unwatched -- maximum condemnation pressure for the item we know least about."""
        keys = set(range(1, 41))
        added = {k: CUTOFF - timedelta(days=1200) for k in keys}  # arrived, never played

        prior = await derive(
            engine, rating_keys=keys, cutoff=CUTOFF, horizon=HORIZON, added_at=added
        )

        # All 40 land in the 1095-1825 bucket (age from arrival), not the 1825+ one.
        bucket = next(b for b in prior.buckets if b.low == 1095)
        assert bucket.samples == 40

    async def test_a_film_added_after_the_cutoff_is_excluded(self, engine: AsyncEngine) -> None:
        """It did not exist yet. Including it would pad a bucket with an item that
        could not possibly have been watched or condemned."""
        keys = {1}
        added = {1: NOW - timedelta(days=10)}  # arrived AFTER the cutoff

        prior = await derive(
            engine, rating_keys=keys, cutoff=CUTOFF, horizon=HORIZON, added_at=added
        )

        assert prior.population == 0


async def _episode_play(engine: AsyncEngine, row_id: int, show_key: int, when: datetime) -> None:
    """One per-episode play. TV history rows carry the show only as the grandparent."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO watch_event (row_id, rating_key, grandparent_rating_key, "
                " user_id, watched_at, watched_status, percent_complete, media_type) "
                "VALUES (:row_id, :rk, :gp, 1, :at, 1, 100, 'episode')"
            ),
            {
                "row_id": row_id,
                "rk": 100_000 + row_id,
                "gp": show_key,
                "at": int(when.timestamp()),
            },
        )


class TestTvJoinsOnTheGrandparentKey:
    """TV history rows are per-episode; the population passes SHOW keys. Joining shows
    on rating_key finds nothing, reads every show as never-rewatched, and the derived
    prior then claims a near-zero baseline that inverts every lift number."""

    async def test_a_show_population_reads_episode_history(self, engine: AsyncEngine) -> None:
        keys = set(range(1, 41))
        added = {k: CUTOFF - timedelta(days=1200) for k in keys}
        row = 0
        for k in keys:  # every show watched (as episodes) well before the cutoff
            row += 1
            await _episode_play(engine, row, k, CUTOFF - timedelta(days=1200))
        for k in list(keys)[:10]:  # a quarter rewatched after it
            row += 1
            await _episode_play(engine, row, k, CUTOFF + timedelta(days=30))

        prior = await derive(
            engine,
            rating_keys=keys,
            cutoff=CUTOFF,
            horizon=HORIZON,
            added_at=added,
            media_type="tv",
        )

        assert prior.rate_for(1200) == pytest.approx(0.25)


class TestTheSampleFloor:
    """A bucket of nine items yields 0%, 11%, 22%... none of which mean anything."""

    async def test_a_thin_bucket_is_reported_as_uncalibrated(self, engine: AsyncEngine) -> None:
        keys = set(range(1, 6))  # only 5 items
        added = {k: NOW - timedelta(days=2500) for k in keys}
        for i, k in enumerate(keys, start=1):
            await _play(engine, i, k, CUTOFF - timedelta(days=1200))

        prior = await derive(
            engine, rating_keys=keys, cutoff=CUTOFF, horizon=HORIZON, added_at=added
        )

        assert prior.calibrated is False
        assert "too few to say" in prior.describe()

    async def test_asking_for_an_uncalibrated_rate_raises(self, engine: AsyncEngine) -> None:
        """It does not guess. A default here would silently become the number the owner
        tunes their thresholds against."""
        keys = set(range(1, 6))
        added = {k: NOW - timedelta(days=2500) for k in keys}
        for i, k in enumerate(keys, start=1):
            await _play(engine, i, k, CUTOFF - timedelta(days=1200))

        prior = await derive(
            engine, rating_keys=keys, cutoff=CUTOFF, horizon=HORIZON, added_at=added
        )

        with pytest.raises(NotCalibratedError, match="not enough history"):
            prior.rate_for(1200)

    async def test_an_empty_population_raises(self, engine: AsyncEngine) -> None:
        with pytest.raises(NotCalibratedError):
            await derive(engine, rating_keys=set(), cutoff=CUTOFF, horizon=HORIZON, added_at={})

    def test_the_floor_is_a_real_number_not_zero(self) -> None:
        assert MIN_SAMPLES >= 30

    def test_a_bucket_below_the_floor_has_no_rate(self) -> None:
        assert Bucket(low=0, high=365, samples=5, rewatched=1).rate is None
        assert Bucket(low=0, high=365, samples=100, rewatched=25).rate == 0.25


class TestTheBacktestUsesTheDerivedPrior:
    """This test exists because the wiring silently did NOT happen the first time.

    The `prior` argument was accepted by `run()` and then never passed into the result.
    mypy was happy -- an unused parameter is perfectly legal -- and the backtest quietly
    kept using the hardcoded fallback while reporting a lift number that looked fine.

    An unused argument is invisible to the type checker. It needs a test."""

    def test_a_derived_prior_reaches_the_result(self) -> None:
        prior = _prior(rate=0.40)
        result = backtest.BacktestResult(cutoff=NOW, condemn_at=60, prior=prior)
        result.condemned = [("Film", 80.0, 1)]
        result.condemned_dormancy = [1200.0]

        # The fallback would say 19% for a 1,200-day-dormant film. The derived prior
        # says 40%. If the wiring is broken, this reads 19%.
        assert result.expected_regret_rate == pytest.approx(0.40)
        assert result.prior_is_derived is True

    def test_without_a_prior_it_falls_back_and_says_so(self) -> None:
        result = backtest.BacktestResult(cutoff=NOW, condemn_at=60)
        result.condemned = [("Film", 80.0, 1)]
        result.condemned_dormancy = [1200.0]

        assert result.expected_regret_rate == pytest.approx(0.19)  # the fallback curve
        assert result.prior_is_derived is False
        assert "borrowed from another library" in result.summary()

    def test_a_derived_prior_is_labelled_as_measured_here(self) -> None:
        result = backtest.BacktestResult(cutoff=NOW, condemn_at=60, prior=_prior(rate=0.40))
        result.condemned = [("Film", 80.0, 1)]
        result.condemned_dormancy = [1200.0]

        assert "measured on your library" in result.summary()


def _prior(*, rate: float) -> object:
    from reaper.engine.calibration import BUCKETS, RewatchPrior

    return RewatchPrior(
        buckets=tuple(
            Bucket(low=lo, high=hi, samples=100, rewatched=int(rate * 100)) for lo, hi in BUCKETS
        ),
        population=600,
        window_days=365,
        computed_at=NOW,
    )
