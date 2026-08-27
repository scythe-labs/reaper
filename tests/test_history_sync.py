# SPDX-License-Identifier: AGPL-3.0-or-later
"""The watch-history mirror. Fast incremental sync, and a regression check that works.

Two things this proves:

* An incremental sync fetches only the delta via Tautulli's ``after`` filter. It never
  re-walks the whole history every time, which on a large library took minutes per scan.
* The regression check compares Tautulli's own reported total against last time, so a
  reset or prune is actually detected. An earlier check compared our local mirror's row
  count instead, which only ever grows (INSERT OR REPLACE, no deletes), so it could never
  fire.

A fake Tautulli stands in for the real API and records how it was called, so the tests can
assert the `after` filter was used rather than a full walk.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from reaper.clients.base import IntegrationError, http_failure, transport_failure
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.session import create_engine
from reaper.services import history_sync
from reaper.services.history_sync import HistoryRegressionError, sync
from tests._fakes import PagingTautulli

NOW = int(utcnow().timestamp())
DAY = 86_400


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
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))
    yield eng
    await eng.dispose()


async def _count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text("SELECT COUNT(*) FROM watch_event"))).scalar_one())


class TestIncrementalSyncFetchesOnlyTheDelta:
    async def test_the_first_sync_walks_everything(self, engine: AsyncEngine) -> None:
        fake = PagingTautulli([_row(i, days_ago=i) for i in range(1, 11)])

        await sync(engine, fake)

        assert await _count(engine) == 10
        # A fresh mirror has nothing to be "after", so the walk is unfiltered.
        assert fake.after_calls[0] is None

    async def test_the_second_sync_asks_only_for_recent_rows(self, engine: AsyncEngine) -> None:
        """The incremental sync must pass an ``after`` date, so Tautulli returns the
        delta instead of the full history."""
        history = [_row(i, days_ago=i) for i in range(1, 11)]
        fake = PagingTautulli(history)
        await sync(engine, fake)

        # A new play arrives, so re-sync.
        history.insert(0, _row(11, days_ago=0))
        fake.total = 11
        fake.after_calls.clear()

        await sync(engine, fake)

        assert await _count(engine) == 11  # the new row landed
        # It asked with an `after` date (not a full walk from the start of history).
        assert fake.after_calls[0] is not None

    async def test_a_full_sync_re_walks_everything(self, engine: AsyncEngine) -> None:
        """The nightly sweep that catches backfilled old events must not use `after`."""
        fake = PagingTautulli([_row(i, days_ago=i) for i in range(1, 6)])
        await sync(engine, fake)
        fake.after_calls.clear()

        await sync(engine, fake, full=True)

        assert fake.after_calls[0] is None

    async def test_a_live_session_with_no_row_id_is_skipped(self, engine: AsyncEngine) -> None:
        """row_id is null only for in-progress sessions, not history yet."""
        fake = PagingTautulli([_row(None, days_ago=0), _row(1, days_ago=1), _row(2, days_ago=2)])

        await sync(engine, fake)

        assert await _count(engine) == 2  # the live session was not recorded


class TestThePageLoopFetchesEveryRow:
    """The mirror's depth is what the horizon gate reads, and a shallow horizon is the
    single largest mass-deletion vector this codebase has. Every item older than the
    mirror looks never-played. So the paging loop must not truncate on a source that is
    merely less tidy than expected."""

    async def test_a_source_that_reports_no_total_is_still_paged_to_the_end(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`int(page.get("recordsFiltered") or 0)` made "not told" identical to "none",
        and 0 ended the loop after page one, silently keeping a single page of history."""
        monkeypatch.setattr(history_sync, "PAGE_SIZE", 2)

        class _NoTotal(PagingTautulli):
            async def history(self, **kwargs: Any) -> dict[str, Any]:
                page = await super().history(**kwargs)
                page.pop("recordsFiltered")
                return page

        rows = [_row(n, days_ago=n) for n in range(1, 8)]
        await sync(engine, _NoTotal(rows), full=True)

        assert await _count(engine) == 7

    async def test_a_short_middle_page_does_not_skip_the_rest(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Advancing by the constant walked `start` past rows nobody had fetched."""
        monkeypatch.setattr(history_sync, "PAGE_SIZE", 4)

        class _ShortSecondPage(PagingTautulli):
            def __init__(self, rows: list[dict[str, Any]]) -> None:
                super().__init__(rows)
                self.pages = 0

            async def history(self, **kwargs: Any) -> dict[str, Any]:
                page = await super().history(**kwargs)
                if kwargs.get("length", 100) > 1:
                    self.pages += 1
                    if self.pages == 2:
                        # A page that came back short without being the last one.
                        page["data"] = page["data"][:1]
                return page

        rows = [_row(n, days_ago=n) for n in range(1, 13)]
        await sync(engine, _ShortSecondPage(rows), full=True)

        assert await _count(engine) == 12

    async def test_a_source_that_never_ends_is_stopped(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Terminating on an empty page alone would spin forever against a source that
        ignores `start` and reports no total. Bounded, and the shorter mirror keeps."""
        monkeypatch.setattr(history_sync, "PAGE_SIZE", 2)
        monkeypatch.setattr(history_sync, "MAX_HISTORY_PAGES", 3)

        class _Endless(PagingTautulli):
            async def history(self, **kwargs: Any) -> dict[str, Any]:
                page = await super().history(**kwargs)
                if kwargs.get("length", 100) > 1:
                    page["data"] = self.rows[:2]  # same page, forever
                    page.pop("recordsFiltered")
                return page

        rows = [_row(n, days_ago=n) for n in range(1, 8)]
        await sync(engine, _Endless(rows), full=True)

        assert await _count(engine) == 2  # it stopped, and kept what it read


class _TimingOutTautulli(PagingTautulli):
    """A Tautulli that answers a page of at most ``serves`` rows and fails on anything
    larger, recording every length it was asked for.

    The failure is built by the production mapper (``clients.base.transport_failure``), so
    what the walk sees here is what a real timeout arrives as."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        serves: int,
        error: type[httpx2.TimeoutException],
    ) -> None:
        super().__init__(rows)
        self.serves = serves
        self.error = error
        self.lengths: list[int] = []

    async def history(self, **kwargs: Any) -> dict[str, Any]:
        length = int(kwargs.get("length", 100))
        if length > 1:  # the regression check's length=1 probe is not a page
            self.lengths.append(length)
            if length > self.serves:
                raise transport_failure("tautulli", self.error("the page did not finish"))
        return await super().history(**kwargs)


class TestASlowSourceShrinksThePageInsteadOfAbortingTheSweep:
    """The full sweep is the only thing that catches a row Tautulli backfills with an old
    timestamp, so a sweep that stops running leaves a watched title reading never watched,
    in the condemn direction, three days at a time until someone notices.

    A source that merely gets slower must not stop the sweep for good. ``PAGE_SIZE`` asks
    for a large page against a read budget each page already spends most of, and a naive
    retry would re-send the identical oversized page rather than a smaller one, so a
    persistently slower Tautulli would fail every sweep from then on."""

    async def test_a_read_timeout_on_the_first_page_shrinks_and_keeps_paging(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every row still lands. The walk halves the page and carries on from the same
        offset, rather than losing the whole sweep to the first oversized request.

        The page cap is set to exactly the number of pages the walk needs, so a shrink that
        charged itself against that cap would run the mirror short. That is the claim the
        comment on ``MAX_HISTORY_PAGES`` makes, and nothing else pins it."""
        monkeypatch.setattr(history_sync, "PAGE_SIZE", 8)
        monkeypatch.setattr(history_sync, "MIN_PAGE_SIZE", 2)
        monkeypatch.setattr(history_sync, "MAX_HISTORY_PAGES", 3)
        fake = _TimingOutTautulli(
            [_row(n, days_ago=n) for n in range(1, 13)], serves=4, error=httpx2.ReadTimeout
        )

        await sync(engine, fake, full=True)

        assert await _count(engine) == 12
        assert fake.lengths == [8, 4, 4, 4]

    async def test_a_failure_that_is_not_a_read_timeout_still_aborts_at_once(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same source, same page sizes, only the timeout kind differs. A connect timeout
        is a host that never answered, and a smaller page cannot fix that, so the walk must
        not spend a shrink cycle on one."""
        monkeypatch.setattr(history_sync, "PAGE_SIZE", 8)
        monkeypatch.setattr(history_sync, "MIN_PAGE_SIZE", 2)
        fake = _TimingOutTautulli(
            [_row(n, days_ago=n) for n in range(1, 13)], serves=4, error=httpx2.ConnectTimeout
        )

        with pytest.raises(IntegrationError, match="Timed out"):
            await sync(engine, fake, full=True)

        assert fake.lengths == [8]  # asked once, never shrank

    async def test_a_source_too_slow_for_the_smallest_page_raises_at_the_floor(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shrink is bounded. At ``MIN_PAGE_SIZE`` the source is not merely slow, so the
        walk raises and the scheduler records a failed sweep, rather than halving forever."""
        monkeypatch.setattr(history_sync, "PAGE_SIZE", 8)
        monkeypatch.setattr(history_sync, "MIN_PAGE_SIZE", 2)
        fake = _TimingOutTautulli(
            [_row(n, days_ago=n) for n in range(1, 13)], serves=1, error=httpx2.ReadTimeout
        )

        with pytest.raises(IntegrationError, match="Timed out"):
            await sync(engine, fake, full=True)

        assert fake.lengths == [8, 4, 2]


class _RowsTautulliCannotServe(PagingTautulli):
    """A Tautulli that answers HTTP 500 for any page holding one of the ``bad`` offsets,
    which is what the live one does for a row its history formatter cannot render. Records
    every ``(start, length)`` it was asked, probe included. The failure is built by the
    production mapper (``clients.base.http_failure``), so the walk sees a real 500.
    """

    def __init__(self, rows: list[dict[str, Any]], *, bad: set[int], status: int = 500) -> None:
        super().__init__(rows)
        self.bad = bad
        self.status = status
        self.asked: list[tuple[int, int]] = []

    async def history(self, **kwargs: Any) -> dict[str, Any]:
        start, length = int(kwargs.get("start", 0)), int(kwargs.get("length", 100))
        self.asked.append((start, length))
        if any(start <= offset < start + length for offset in self.bad):
            response = httpx2.Response(
                self.status, request=httpx2.Request("GET", "http://tautulli.example/api/v2")
            )
            raise http_failure("tautulli", response, "GET", "/api/v2")
        return await super().history(**kwargs)


class TestARowTheSourceCannotServeIsSteppedOverNotFatal:
    """Tautulli renders ``get_history`` rows in Python after the query, and a row it cannot
    render fails the whole page it is on with a 500, at any page size. Without stepping over
    it, every row sharing that page with a bad one would go unmirrored no matter what page
    size or timeout is tried. The walk isolates the row, steps over it, and says so
    (``MAX_UNSERVABLE_ROWS``)."""

    async def test_the_rows_beside_a_bad_one_still_land_and_the_count_is_reported(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live shape: the two oldest rows are the bad ones. Every other row lands, the
        state carries the count, and the ledger shows the halving down to one row, the two
        skips, and the walk ending on the empty page past them."""
        monkeypatch.setattr(history_sync, "PAGE_SIZE", 8)
        monkeypatch.setattr(history_sync, "MIN_PAGE_SIZE", 2)
        fake = _RowsTautulliCannotServe([_row(n, days_ago=n) for n in range(1, 13)], bad={10, 11})

        state = await sync(engine, fake, full=True)

        assert await _count(engine) == 10
        assert state.unservable == 2
        assert fake.asked == [
            (0, 1),  # the regression check's probe
            (0, 8),
            (8, 8),
            (8, 4),
            (8, 2),
            (10, 4),
            (10, 2),
            (10, 1),  # stands alone and still fails, stepped over
            (11, 1),  # the second one, straight from a one-row page
            (12, 1),  # past the end, empty, done
        ]

    async def test_the_page_climbs_back_after_a_bad_row_in_the_middle(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bad row early in a long history must not leave the rest of the walk at one row
        a page. The page doubles on every page that lands until it is back at the top."""
        monkeypatch.setattr(history_sync, "PAGE_SIZE", 8)
        monkeypatch.setattr(history_sync, "MIN_PAGE_SIZE", 2)
        fake = _RowsTautulliCannotServe([_row(n, days_ago=n) for n in range(1, 21)], bad={5})

        state = await sync(engine, fake, full=True)

        assert await _count(engine) == 19
        assert state.unservable == 1
        assert [length for start, length in fake.asked if start > 5] == [1, 2, 4, 8]

    async def test_too_many_bad_rows_is_a_broken_source_and_raises(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The skip is bounded. Past ``MAX_UNSERVABLE_ROWS`` the walk raises the domain error
        with the count, and what landed before it stays in the mirror."""
        monkeypatch.setattr(history_sync, "PAGE_SIZE", 8)
        monkeypatch.setattr(history_sync, "MIN_PAGE_SIZE", 2)
        monkeypatch.setattr(history_sync, "MAX_UNSERVABLE_ROWS", 1)
        fake = _RowsTautulliCannotServe([_row(n, days_ago=n) for n in range(1, 13)], bad={10, 11})

        with pytest.raises(IntegrationError) as exc:
            await sync(engine, fake, full=True)
        assert exc.value.code == "error.integration.history_rows_unservable"
        assert exc.value.params == {"count": 2}

        assert await _count(engine) == 10

    @pytest.mark.parametrize("status", [404, 502, 503])
    async def test_any_other_status_still_aborts_at_once(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        """A 502 or 503 is a proxy saying the source is away, and a 4xx is the request
        itself. No smaller page fixes either, so the walk must not spend the descent."""
        monkeypatch.setattr(history_sync, "PAGE_SIZE", 8)
        fake = _RowsTautulliCannotServe(
            [_row(n, days_ago=n) for n in range(1, 13)], bad={3}, status=status
        )

        with pytest.raises(IntegrationError) as exc:
            await sync(engine, fake, full=True)
        assert exc.value.status == status

        assert fake.asked == [(0, 1), (0, 8)]  # the probe, one page, never shrank


class TestTheWalkAsksForHistoryWithoutActivity:
    """Tautulli appends its temporary session table to ``get_history`` as activity unless
    told not to, and a session that table holds without a start time fails every page it
    is on with HTTP 500. The walk drops a row with no ``row_id`` anyway, so it asks without
    activity, and the regression probe counts the same rows the walk reads."""

    async def test_every_page_and_the_probe_pass_zero(self, engine: AsyncEngine) -> None:
        """The client's default is ``None`` (Tautulli decides), so a call that omits the
        argument is distinguishable from one that passes 0."""
        fake = PagingTautulli([_row(n, days_ago=n) for n in range(1, 10)])

        await sync(engine, fake, full=True)

        assert fake.include_activity, "nothing was asked"
        assert set(fake.include_activity) == {0}


class TestTheSweepCarriesItsOwnReadBudget:
    """A page of the sweep asks for tens of thousands of rows, and the calls beside it on
    the same client answer a browser, so the read budget cannot be a property of the client
    itself. Sharing one budget forces a choice between a slow browser and a sweep that times
    out early, so each page carries its own.
    """

    async def test_every_page_asks_with_the_sweeps_budget_and_the_probe_does_not(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The value is patched away from the shipped one, so a hardcoded number or an
        omitted argument fails here rather than reading the same either way."""
        monkeypatch.setattr(history_sync, "PAGE_SIZE", 4)
        monkeypatch.setattr(history_sync, "PAGE_READ_TIMEOUT", 7.5)
        fake = PagingTautulli([_row(n, days_ago=n) for n in range(1, 10)])

        await sync(engine, fake, full=True)

        # First call is the regression check's one-row probe, which keeps the client's
        # shared budget. Every page after it carries the sweep's.
        assert fake.read_timeouts[0] is None
        assert set(fake.read_timeouts[1:]) == {7.5}
        assert len(fake.read_timeouts) > 2  # it really did page


class TestOnlyOneWalkOfTheHistoryRunsAtATime:
    """The scan's incremental sync and the full sweep are on independent schedules, and both
    write ``watch_event``. Running at once, they double the load on Tautulli and meet on the
    cache write lock, where one page can hold it well past the database's own busy timeout.

    The sweep must never be the one that yields. A sweep that skips whenever something else
    is running is a sweep that stops running, and the scan cron is the operator's, so "skip
    if busy" can quietly mean "never again". So the second caller waits.
    """

    async def test_a_second_sync_cannot_start_while_one_is_in_flight(
        self, engine: AsyncEngine
    ) -> None:
        """The control half is the whole test. "The scan had not reached Tautulli" only
        means the lock held it if the scan would otherwise have got there, and every await
        inside ``_sync`` before its first request goes out to a thread. So the same pump
        runs first with nothing in flight, where it must arrive. Without that control, this
        test would still pass with the lock deleted, because it would only be reading its
        own scheduling."""
        gate = asyncio.Event()
        holding = asyncio.Event()
        reached: list[str] = []

        class _Gated(PagingTautulli):
            def __init__(self, rows: list[dict[str, Any]], name: str, *, blocks: bool) -> None:
                super().__init__(rows)
                self.name = name
                self.blocks = blocks

            async def history(self, **kwargs: Any) -> dict[str, Any]:
                reached.append(self.name)
                if self.blocks:
                    holding.set()
                    await gate.wait()
                return await super().history(**kwargs)

        async def pump() -> None:
            # Real database round trips are used here, not a bare `sleep(0)`. The awaits a
            # sync makes before its first request are executor hops, which a loop yielding
            # only to itself does not advance.
            for _ in range(20):
                await _count(engine)

        # The pump reads `watch_event`, so the table has to exist before it runs. Otherwise
        # the pump races the first sync's own `ensure_schema`, and the test fails on its own
        # harness rather than on the lock.
        await history_sync.ensure_schema(engine)
        rows = [_row(n, days_ago=n) for n in range(1, 6)]

        uncontended = asyncio.create_task(sync(engine, _Gated(rows, "control", blocks=False)))
        await pump()
        assert "control" in reached, "the pump is too short to prove anything about the lock"
        await uncontended
        reached.clear()

        sweep = asyncio.create_task(sync(engine, _Gated(rows, "sweep", blocks=True), full=True))
        await holding.wait()  # the sweep is inside the lock and parked on its first request
        scan = asyncio.create_task(sync(engine, _Gated(rows, "scan", blocks=False)))
        await pump()

        # Same pump, and this time the scan has not reached Tautulli at all, not even the
        # regression check's probe, which is inside the lock because it reads the stored
        # total and writes it back.
        assert set(reached) == {"sweep"}
        assert not scan.done()

        gate.set()
        await sweep
        await scan
        # Every one of the sweep's calls precedes every one of the scan's. The two walks
        # did not interleave, which is the property the cache write lock needs.
        assert reached == ["sweep"] * reached.count("sweep") + ["scan"] * reached.count("scan")

    async def test_the_waiting_caller_says_so(self, engine: AsyncEngine) -> None:
        """A scan that sits on "syncing watch history" for minutes needs a reason in the log,
        or the wait reads as a hang."""
        gate = asyncio.Event()
        entered = asyncio.Event()

        class _Blocking(PagingTautulli):
            async def history(self, **kwargs: Any) -> dict[str, Any]:
                entered.set()
                await gate.wait()
                return await super().history(**kwargs)

        rows = [_row(n, days_ago=n) for n in range(1, 4)]
        sweep = asyncio.create_task(sync(engine, _Blocking(rows), full=True))
        await entered.wait()
        with capture_logs() as events:
            scan = asyncio.create_task(sync(engine, PagingTautulli(rows)))
            for _ in range(50):
                await asyncio.sleep(0)
            waited = [e for e in events if e["event"] == "history.sync_waiting"]

        gate.set()
        await sweep
        await scan
        # The waiter is the scan, and it says which kind of sync is waiting.
        assert [e["full"] for e in waited] == [False]


class TestRegressionDetection:
    async def test_the_first_sync_sets_a_baseline_and_does_not_raise(
        self, engine: AsyncEngine
    ) -> None:
        fake = PagingTautulli([_row(i, days_ago=i) for i in range(1, 6)], total=5)
        await sync(engine, fake)  # must not raise
        assert await history_sync._last_tautulli_total(engine) == 5

    async def test_a_shrunk_history_raises(self, engine: AsyncEngine) -> None:
        """Tautulli's total drops sharply, whether from a reset, a prune, or a restore.
        Every 'never watched' verdict is now suspect, so the sync stops before writing."""
        fake = PagingTautulli([_row(i, days_ago=i) for i in range(1, 101)], total=100)
        await sync(engine, fake)

        # Tautulli now reports far fewer rows.
        shrunk = PagingTautulli([_row(i, days_ago=i) for i in range(1, 11)], total=10)

        with pytest.raises(HistoryRegressionError, match="shrank from 100"):
            await sync(engine, shrunk)

    async def test_a_small_wobble_does_not_raise(self, engine: AsyncEngine) -> None:
        """Grouping can nudge the reported count by a row or two. That is not a reset."""
        fake = PagingTautulli([_row(i, days_ago=i) for i in range(1, 101)], total=100)
        await sync(engine, fake)

        nudged = PagingTautulli([_row(i, days_ago=i) for i in range(1, 100)], total=98)
        await sync(engine, nudged)  # 98 >= 100*0.95, fine

    async def test_the_check_is_not_fooled_by_our_growing_mirror(self, engine: AsyncEngine) -> None:
        """An earlier check compared our mirror's row count, which only grows, so a real
        Tautulli shrink was invisible to it. This checks that the regression check keys on
        Tautulli's total instead. Our mirror still has 100 rows, but Tautulli reporting 10
        must still raise."""
        fake = PagingTautulli([_row(i, days_ago=i) for i in range(1, 101)], total=100)
        await sync(engine, fake)
        assert await _count(engine) == 100

        shrunk = PagingTautulli([_row(i, days_ago=i) for i in range(1, 11)], total=10)
        with pytest.raises(HistoryRegressionError):
            await sync(engine, shrunk)

        # Our mirror is untouched. We keep what we hold even when the sync raises.
        assert await _count(engine) == 100


class TestOverlapNeverDropsARow:
    async def test_a_same_day_play_after_the_last_sync_is_captured(
        self, engine: AsyncEngine
    ) -> None:
        """`after` is date-granular, so the overlap must re-ask for our newest day, or a
        play recorded later that same day would fall in the gap and be lost forever."""
        newest = _row(1, days_ago=0)
        fake = PagingTautulli([newest])
        await sync(engine, fake)

        # Another play the same day, a few hours later (higher row_id, same date bucket).
        later_same_day = dict(newest, row_id=2, rating_key=999, date=newest["date"] + 3600)
        fake.rows.insert(0, later_same_day)
        fake.total = 2

        await sync(engine, fake)

        assert await _count(engine) == 2  # the same-day play was not missed


class TestTheIngestClockIsSeparateFromTheWatchingClock:
    """ "Did the ingest run" and "did anybody watch anything" are different questions, and
    only the first one can tell a stalled sync from a library nobody touched this week."""

    async def test_there_is_no_sync_time_before_the_first_sync(self, engine: AsyncEngine) -> None:
        assert await history_sync.last_synced_at(engine) is None

    async def test_a_sync_that_returned_nothing_still_moves_the_clock(
        self, engine: AsyncEngine
    ) -> None:
        """Nobody watched anything, so the newest event stays empty. The ingest ran, so the
        sync clock moves. Degrading a scan on the newest event would call this library
        broken."""
        await sync(engine, PagingTautulli([]))

        assert (await history_sync.state(engine)).latest is None
        synced = await history_sync.last_synced_at(engine)
        assert synced is not None
        assert abs((utcnow() - synced).total_seconds()) < 60


class TestAnUnreportedCompletionIsStoredAsUnknown:
    """``sync`` is the only place a NULL ``watched_status`` is ever written, so this is
    where the "we were not told" / "did not finish" distinction is won or lost. Tests that
    insert NULLs with raw SQL prove the queries read one correctly. Only these prove one is
    actually produced.

    Collapsing the two makes a viewer look further behind than they are, and the season
    they are part-way through loses its mid-binge protection.
    """

    async def _status(self, engine: AsyncEngine, row_id: int) -> float | None:
        async with engine.connect() as conn:
            value = (
                await conn.execute(
                    text("SELECT watched_status FROM watch_event WHERE row_id = :id"),
                    {"id": row_id},
                )
            ).scalar_one()
        return None if value is None else float(value)

    async def test_a_missing_status_is_stored_as_null(self, engine: AsyncEngine) -> None:
        row = _row(1, days_ago=1)
        del row["watched_status"]
        await sync(engine, PagingTautulli([row]))

        assert await self._status(engine, 1) is None

    async def test_an_empty_status_is_stored_as_null(self, engine: AsyncEngine) -> None:
        """Tautulli sends "" rather than omitting the key on some rows."""
        await sync(engine, PagingTautulli([dict(_row(1, days_ago=1), watched_status="")]))

        assert await self._status(engine, 1) is None

    async def test_a_reported_zero_round_trips_as_zero(self, engine: AsyncEngine) -> None:
        """The other direction. A real "started it, did not finish" must not become
        unknown. Pinning only the NULL side would let `None` swallow both facts."""
        await sync(engine, PagingTautulli([dict(_row(1, days_ago=1), watched_status=0)]))

        assert await self._status(engine, 1) == 0.0

    async def test_the_two_are_distinguishable_after_a_sync(self, engine: AsyncEngine) -> None:
        """Both facts in one history, told apart in storage."""
        unknown = _row(1, days_ago=1)
        del unknown["watched_status"]
        rows = [unknown, dict(_row(2, days_ago=2), watched_status=0)]

        await sync(engine, PagingTautulli(rows))

        assert await self._status(engine, 1) is None
        assert await self._status(engine, 2) == 0.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),  # key absent from Tautulli's payload
        ("", None),  # key present but empty
        ("not a number", None),
        ({}, None),  # a shape that is valid JSON but not a number
        (0, 0.0),  # genuinely started, did not finish
        (0.0, 0.0),
        ("0", 0.0),
        ("0.5", 0.5),
        (1, 1.0),  # completed
    ],
)
def test_float_or_none_keeps_unknown_apart_from_zero(value: object, expected: float | None) -> None:
    assert history_sync._float_or_none(value) == expected


def test_overlap_is_at_least_a_day() -> None:
    # A guard on the constant. Any smaller overlap risks losing a same-day play.
    assert timedelta(days=1) <= history_sync.INCREMENTAL_OVERLAP


class TestEnsureSchema:
    """The rebuild path decides DROP/CREATE from the shape read inside the write
    transaction, never from a read taken before the lock. These pin the observable outcomes
    of that decision. A stale table is rebuilt empty, and a current one is left untouched."""

    async def _live_columns(self, engine: AsyncEngine) -> tuple[tuple[str, str, int], ...]:
        async with engine.connect() as conn:
            cols = (await conn.execute(text("PRAGMA table_info(watch_event)"))).all()
        return tuple((row[1], str(row[2]).upper(), int(row[3])) for row in cols)

    async def test_a_stale_shaped_table_is_rebuilt_empty_to_the_current_shape(
        self, engine: AsyncEngine
    ) -> None:
        # A table missing the newest column, carrying a row from before the shape change.
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS watch_event"))
            await conn.execute(
                text(
                    "CREATE TABLE watch_event ("
                    "row_id INTEGER PRIMARY KEY, rating_key INTEGER NOT NULL, "
                    "parent_rating_key INTEGER, grandparent_rating_key INTEGER, "
                    "user_id INTEGER NOT NULL, watched_at INTEGER NOT NULL, "
                    "watched_status REAL, percent_complete INTEGER NOT NULL, "
                    "media_type TEXT NOT NULL)"  # no media_index -> stale
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO watch_event (rating_key, user_id, watched_at, "
                    "percent_complete, media_type) VALUES (1, 1, 1, 100, 'movie')"
                )
            )

        await history_sync.ensure_schema(engine)

        # Rebuilt to the current shape, and the untrustworthy pre-change row is gone.
        assert await self._live_columns(engine) == history_sync._WATCH_EVENT_COLUMNS
        assert await _count(engine) == 0

    async def _live_indexes(self, engine: AsyncEngine) -> set[str]:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'index' AND tbl_name = 'watch_event'"
                    )
                )
            ).all()
        return {str(row[0]) for row in rows}

    async def test_every_declared_index_lands_on_a_fresh_cache(self, engine: AsyncEngine) -> None:
        await history_sync.ensure_schema(engine)
        assert set(history_sync.INDEXES) <= await self._live_indexes(engine)

    async def test_a_missing_index_is_added_without_dropping_the_mirror(
        self, engine: AsyncEngine
    ) -> None:
        """An index added after an install synced must reach that install.

        Indexes are not columns, so the shape check cannot see one missing. An index
        declared only in the table DDL would look current forever and never actually get
        created, leaving every fairness query full-scanning. The only way to force it
        through the shape check would be to bump the column tuple, which drops the whole
        mirror and costs a full re-sync from Tautulli just to add an index. So a missing
        index is noticed by name and created in place instead.
        """
        await history_sync.ensure_schema(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO watch_event (rating_key, user_id, watched_at, "
                    "percent_complete, media_type) VALUES (1, 1, 1, 100, 'movie')"
                )
            )
            # An install that synced before this index was declared.
            await conn.execute(text("DROP INDEX ix_watch_event_parent_key"))
        assert "ix_watch_event_parent_key" not in await self._live_indexes(engine)

        await history_sync.ensure_schema(engine)

        assert "ix_watch_event_parent_key" in await self._live_indexes(engine)
        assert await _count(engine) == 1  # and the mirror survived

    async def test_the_parent_key_filter_uses_its_index(self, engine: AsyncEngine) -> None:
        """The Scales board and the person drawer both filter on ``parent_rating_key``.

        Unindexed, each 500-key chunk would be a full scan of a table holding every play
        the server ever recorded. This asserts the planner picks the index rather than
        timing the query, so it fails if the index is ever dropped from ``INDEXES``.
        """
        await history_sync.ensure_schema(engine)
        async with engine.connect() as conn:
            plan = (
                await conn.execute(
                    text(
                        "EXPLAIN QUERY PLAN SELECT row_id FROM watch_event "
                        "WHERE parent_rating_key IN (1, 2, 3)"
                    )
                )
            ).all()
        assert "ix_watch_event_parent_key" in " ".join(str(row[3]) for row in plan)

    async def test_a_current_table_is_left_untouched(self, engine: AsyncEngine) -> None:
        await history_sync.ensure_schema(engine)  # creates it at the current shape
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO watch_event (rating_key, user_id, watched_at, "
                    "percent_complete, media_type) VALUES (1, 1, 1, 100, 'movie')"
                )
            )

        await history_sync.ensure_schema(engine)  # no-op, shape already current

        assert await _count(engine) == 1  # the row survived, no rebuild
