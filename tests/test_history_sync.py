# SPDX-License-Identifier: AGPL-3.0-or-later
"""The watch-history mirror: fast incremental sync, and a regression check that works.

Two things this proves, both of which were wrong before:

* An incremental sync fetches only the delta via Tautulli's ``after`` filter -- it does
  NOT re-walk the whole history every time. On a large library that was minutes per scan.
* The regression check compares **Tautulli's own reported total** against last time, so a
  reset/prune is actually detected. The old check compared our local mirror's row count,
  which only ever grows (INSERT OR REPLACE, no deletes), so it could never fire.

A fake Tautulli stands in for the real API and records how it was called, so the tests
can assert the `after` filter was used rather than a full walk.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.session import create_engine
from reaper.services import history_sync
from reaper.services.history_sync import HistoryRegressionError, sync

NOW = int(utcnow().timestamp())
DAY = 86_400


class FakeTautulli:
    """Records the ``after`` argument of each history() call and serves canned rows.

    ``rows`` is the full history newest-first (as Tautulli returns it). When ``after`` is
    given, only rows on/after that date are served -- mimicking the real filter -- so a
    test can assert an incremental sync fetched few rows, not all of them.
    """

    def __init__(self, rows: list[dict[str, Any]], *, total: int | None = None) -> None:
        self.rows = rows
        self.total = total if total is not None else len(rows)
        self.after_calls: list[str | None] = []

    async def history(
        self, *, length: int = 100, start: int = 0, after: str | None = None, **_: Any
    ) -> dict[str, Any]:
        # The length=1 probe used by the regression check does not count as a page fetch.
        if length > 1:
            self.after_calls.append(after)

        served = self.rows
        if after is not None:
            cutoff = _date_to_epoch(after)
            served = [r for r in self.rows if int(r["date"]) >= cutoff]

        window = served[start : start + length]
        return {
            "data": window,
            "recordsFiltered": len(served),
            "recordsTotal": self.total,
        }


def _date_to_epoch(date_str: str) -> int:
    from datetime import UTC, datetime

    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def _row(row_id: int | None, days_ago: int) -> dict[str, Any]:
    return {
        "row_id": row_id,
        "rating_key": 100 + (row_id or 0),
        "user_id": 1,
        "date": NOW - days_ago * DAY,
        "watched_status": 1,
        "percent_complete": 100,
        "media_type": "movie",
    }


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))  # type: ignore[call-arg]
    yield eng
    await eng.dispose()


async def _count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text("SELECT COUNT(*) FROM watch_event"))).scalar_one())


class TestIncrementalSyncFetchesOnlyTheDelta:
    async def test_the_first_sync_walks_everything(self, engine: AsyncEngine) -> None:
        fake = FakeTautulli([_row(i, days_ago=i) for i in range(1, 11)])

        await sync(engine, fake)  # type: ignore[arg-type]

        assert await _count(engine) == 10
        # A fresh mirror has nothing to be "after", so the walk is unfiltered.
        assert fake.after_calls[0] is None

    async def test_the_second_sync_asks_only_for_recent_rows(self, engine: AsyncEngine) -> None:
        """The whole point: the incremental sync must pass an ``after`` date, so Tautulli
        returns the delta instead of the full history."""
        history = [_row(i, days_ago=i) for i in range(1, 11)]
        fake = FakeTautulli(history)
        await sync(engine, fake)  # type: ignore[arg-type]

        # A new play arrives; re-sync.
        history.insert(0, _row(11, days_ago=0))
        fake.total = 11
        fake.after_calls.clear()

        await sync(engine, fake)  # type: ignore[arg-type]

        assert await _count(engine) == 11  # the new row landed
        # It asked with an `after` date (not a full walk from the start of history).
        assert fake.after_calls[0] is not None

    async def test_a_full_sync_re_walks_everything(self, engine: AsyncEngine) -> None:
        """The nightly sweep that catches backfilled old events must NOT use `after`."""
        fake = FakeTautulli([_row(i, days_ago=i) for i in range(1, 6)])
        await sync(engine, fake)  # type: ignore[arg-type]
        fake.after_calls.clear()

        await sync(engine, fake, full=True)  # type: ignore[arg-type]

        assert fake.after_calls[0] is None

    async def test_a_live_session_with_no_row_id_is_skipped(self, engine: AsyncEngine) -> None:
        """row_id is null only for in-progress sessions -- not history yet."""
        fake = FakeTautulli([_row(None, days_ago=0), _row(1, days_ago=1), _row(2, days_ago=2)])

        await sync(engine, fake)  # type: ignore[arg-type]

        assert await _count(engine) == 2  # the live session was not recorded


class TestRegressionDetection:
    async def test_the_first_sync_sets_a_baseline_and_does_not_raise(
        self, engine: AsyncEngine
    ) -> None:
        fake = FakeTautulli([_row(i, days_ago=i) for i in range(1, 6)], total=5)
        await sync(engine, fake)  # type: ignore[arg-type]  # must not raise
        assert await history_sync._last_tautulli_total(engine) == 5

    async def test_a_shrunk_history_raises(self, engine: AsyncEngine) -> None:
        """Tautulli's total drops sharply -- reset, prune or restore. Every 'never
        watched' verdict is now suspect, so the sync stops before writing."""
        fake = FakeTautulli([_row(i, days_ago=i) for i in range(1, 101)], total=100)
        await sync(engine, fake)  # type: ignore[arg-type]

        # Tautulli now reports far fewer rows.
        shrunk = FakeTautulli([_row(i, days_ago=i) for i in range(1, 11)], total=10)

        with pytest.raises(HistoryRegressionError, match="shrank from 100"):
            await sync(engine, shrunk)  # type: ignore[arg-type]

    async def test_a_small_wobble_does_not_raise(self, engine: AsyncEngine) -> None:
        """Grouping can nudge the reported count by a row or two; that is not a reset."""
        fake = FakeTautulli([_row(i, days_ago=i) for i in range(1, 101)], total=100)
        await sync(engine, fake)  # type: ignore[arg-type]

        nudged = FakeTautulli([_row(i, days_ago=i) for i in range(1, 100)], total=98)
        await sync(engine, nudged)  # type: ignore[arg-type]  # 98 >= 100*0.95, fine

    async def test_the_check_is_not_fooled_by_our_growing_mirror(self, engine: AsyncEngine) -> None:
        """The old check compared our mirror's row count, which only grows -- so a real
        Tautulli shrink was invisible. This asserts the check keys on Tautulli's total:
        our mirror still has 100 rows, but Tautulli reporting 10 must still raise."""
        fake = FakeTautulli([_row(i, days_ago=i) for i in range(1, 101)], total=100)
        await sync(engine, fake)  # type: ignore[arg-type]
        assert await _count(engine) == 100

        shrunk = FakeTautulli([_row(i, days_ago=i) for i in range(1, 11)], total=10)
        with pytest.raises(HistoryRegressionError):
            await sync(engine, shrunk)  # type: ignore[arg-type]

        # Our mirror is untouched -- we preserve what we hold.
        assert await _count(engine) == 100


class TestOverlapNeverDropsARow:
    async def test_a_same_day_play_after_the_last_sync_is_captured(
        self, engine: AsyncEngine
    ) -> None:
        """`after` is date-granular, so the overlap must re-ask for our newest day, or a
        play recorded later that same day would fall in the gap and be lost forever."""
        newest = _row(1, days_ago=0)
        fake = FakeTautulli([newest])
        await sync(engine, fake)  # type: ignore[arg-type]

        # Another play the SAME day, a few hours later (higher row_id, same date bucket).
        later_same_day = dict(newest, row_id=2, rating_key=999, date=newest["date"] + 3600)
        fake.rows.insert(0, later_same_day)
        fake.total = 2

        await sync(engine, fake)  # type: ignore[arg-type]

        assert await _count(engine) == 2  # the same-day play was not missed


def test_overlap_is_at_least_a_day() -> None:
    # A guard on the constant: any smaller overlap risks losing a same-day play.
    assert timedelta(days=1) <= history_sync.INCREMENTAL_OVERLAP
