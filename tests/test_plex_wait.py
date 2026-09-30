# SPDX-License-Identifier: AGPL-3.0-or-later
"""The wait for Plex to finish a reap's folder scans, and what it holds back.

A reap sends Plex hundreds of folder scans and Plex runs them one at a time. Reaper waits for
them before it scans, so the scan does not read titles that are still disappearing. Every
timing is patched small. ``conftest`` makes ``asyncio.sleep`` instant, and the ``slept``
fixture drives the loop clock, so the ceiling and the grace are asserted as recorded delays.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from reaper.api import runs
from reaper.clients.plex import PlexClient
from reaper.config import RuntimeSafety
from reaper.services import app_settings, plex_wait, scan_runner, scheduler


@pytest.fixture(autouse=True)
def _no_wait_left_behind() -> Any:
    plex_wait.end()
    yield
    plex_wait.end()


def _script(*reads: bool) -> Any:
    """An ``is_scanning`` answering from a script, then ``False`` forever. Records each call."""
    calls: list[int] = []
    queue = list(reads)

    async def is_scanning() -> bool:
        calls.append(len(calls))
        return queue.pop(0) if queue else False

    is_scanning.calls = calls  # type: ignore[attr-defined]
    return is_scanning


class TestWaitUntilQuiet:
    async def test_it_ends_after_three_quiet_polls_in_a_row(self, slept: list[float]) -> None:
        reads = _script()

        result = await plex_wait.wait_until_quiet(
            reads, grace=60, poll=20, quiet_polls=3, ceiling=10_000
        )

        assert result == "quiet"
        assert len(reads.calls) == 3
        assert slept == [60, 20, 20]

    async def test_a_busy_read_starts_the_count_again(self, slept: list[float]) -> None:
        # Two quiet reads, then a scan between two queued ones, then three quiet reads.
        reads = _script(False, False, True)

        result = await plex_wait.wait_until_quiet(
            reads, grace=0, poll=20, quiet_polls=3, ceiling=10_000
        )

        assert result == "quiet"
        assert len(reads.calls) == 6

    async def test_it_keeps_waiting_while_a_scan_runs(self, slept: list[float]) -> None:
        reads = _script(*([True] * 50))

        result = await plex_wait.wait_until_quiet(
            reads, grace=0, poll=20, quiet_polls=3, ceiling=10_000
        )

        assert result == "quiet"
        assert len(reads.calls) == 53

    async def test_the_ceiling_ends_a_wait_that_never_goes_quiet(self, slept: list[float]) -> None:
        async def always_busy() -> bool:
            return True

        result = await plex_wait.wait_until_quiet(
            always_busy, grace=60, poll=20, quiet_polls=3, ceiling=200
        )

        assert result == "ceiling"
        # The grace, then a poll every 20 s until 200 s have passed.
        assert slept[0] == 60
        assert sum(slept) == 200

    async def test_the_ceiling_holds_when_sleep_does_not_move_the_clock(self) -> None:
        async def always_busy() -> bool:
            return True

        result = await asyncio.wait_for(plex_wait.wait_until_quiet(always_busy), 30)

        assert result == "ceiling"

    async def test_nothing_is_read_during_the_grace(self, slept: list[float]) -> None:
        reads = _script()

        await plex_wait.wait_until_quiet(reads, grace=60, poll=20, quiet_polls=1, ceiling=10_000)

        assert slept[0] == 60
        assert len(reads.calls) == 1

    async def test_it_waits_out_a_recorded_queue_of_531_scans(self, slept: list[float]) -> None:
        """Replays ``plex_scan_timeline.json``, the scan times Plex showed after a 531-item reap."""
        scans = json.loads(
            (Path(__file__).parent / "fixtures" / "plex_scan_timeline.json").read_text()
        )["scans"]
        last_end = max(end for _, end in scans)
        loop = asyncio.get_running_loop()
        t0 = loop.time()

        async def is_scanning() -> bool:
            now = loop.time() - t0
            return any(start <= now < end for start, end in scans)

        result = await plex_wait.wait_until_quiet(is_scanning)

        ended = loop.time() - t0
        quiet_window = plex_wait.QUIET_POLLS * plex_wait.POLL_S
        assert result == "quiet"
        assert ended > last_end
        assert ended <= last_end + quiet_window + plex_wait.POLL_S


class TestAnUnreadablePlexIsBusy:
    async def test_is_scanning_answers_busy_when_the_activity_list_fails(self) -> None:
        client = PlexClient("http://plex.local:32400", "token", safety=RuntimeSafety())

        class Broken:
            @property
            def activities(self) -> list[Any]:
                raise RuntimeError("Plex is down")

        client._server = Broken()  # type: ignore[assignment]
        assert await client.is_scanning() is True

    async def test_only_an_activity_titled_scanning_counts(self) -> None:
        client = PlexClient("http://plex.local:32400", "token", safety=RuntimeSafety())
        client._server = SimpleNamespace(  # type: ignore[assignment]
            activities=[SimpleNamespace(title="Refreshing Sub")] * 21
        )
        assert await client.is_scanning() is False

        client._server = SimpleNamespace(  # type: ignore[assignment]
            activities=[SimpleNamespace(title="Refreshing Sub"), SimpleNamespace(title="Scanning")]
        )
        assert await client.is_scanning() is True

    async def test_an_unreadable_plex_never_ends_the_wait(self, slept: list[float]) -> None:
        client = PlexClient("http://plex.local:32400", "token", safety=RuntimeSafety())

        class Broken:
            @property
            def activities(self) -> list[Any]:
                raise RuntimeError("Plex is down")

        client._server = Broken()  # type: ignore[assignment]

        result = await plex_wait.wait_until_quiet(
            client.is_scanning, grace=0, poll=20, quiet_polls=3, ceiling=200
        )

        assert result == "ceiling"


def _app() -> Any:
    state = SimpleNamespace(settings=None, secret_box=None, session_factory=None)
    return SimpleNamespace(state=state)


@pytest.fixture
def waiting_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    """An app whose reap wait reads a scripted Plex. ``launches`` records each scan start."""
    app = _app()
    app.launches = []
    app.plex = SimpleNamespace()

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def runtime_safety(*_a: Any, **_k: Any) -> RuntimeSafety:
        return RuntimeSafety()

    async def build(*_a: Any, **_k: Any) -> Any:
        plex = PlexClient("http://plex.local:32400", "token", safety=RuntimeSafety())
        plex.is_scanning = app.plex.is_scanning  # type: ignore[method-assign]
        return None, [plex]

    app.state.session_factory = lambda: _Session()
    monkeypatch.setattr(app_settings, "runtime_safety", runtime_safety)
    monkeypatch.setattr(runs, "build_reap_gateway", build)
    monkeypatch.setattr(runs, "launch_scan", lambda a: app.launches.append("scan"))
    return app


class TestTheWaitBeforeTheScan:
    async def test_the_scan_starts_once_plex_goes_quiet_and_the_wait_clears(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        waiting_app.plex.is_scanning = _script(True, True)

        await runs.start_plex_wait(waiting_app)
        assert plex_wait.waiting_since() is not None
        assert waiting_app.launches == []

        await waiting_app.state.plex_wait_task

        assert waiting_app.launches == ["scan"]
        assert plex_wait.waiting_since() is None

    async def test_the_scan_does_not_start_while_plex_is_busy(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        gate = asyncio.Event()

        async def busy_until_released() -> bool:
            await asyncio.sleep(0)
            return not gate.is_set()

        waiting_app.plex.is_scanning = busy_until_released
        await runs.start_plex_wait(waiting_app)
        for _ in range(20):
            await asyncio.sleep(0)

        assert waiting_app.launches == []
        assert plex_wait.waiting_since() is not None

        gate.set()
        await waiting_app.state.plex_wait_task
        assert waiting_app.launches == ["scan"]

    async def test_a_cancel_starts_no_scan_and_clears_the_wait(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        waiting_app.plex.is_scanning = _script(*([True] * 10_000))
        await runs.start_plex_wait(waiting_app)
        await asyncio.sleep(0)

        task = waiting_app.state.plex_wait_task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert waiting_app.launches == []
        assert plex_wait.waiting_since() is None

    async def test_a_second_reap_replaces_the_wait_and_scans_once(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        waiting_app.plex.is_scanning = _script(*([True] * 10_000))
        await runs.start_plex_wait(waiting_app)
        await asyncio.sleep(0)
        first = waiting_app.state.plex_wait_task

        waiting_app.plex.is_scanning = _script()
        await runs.start_plex_wait(waiting_app)
        await waiting_app.state.plex_wait_task

        assert first.cancelled()
        assert waiting_app.launches == ["scan"]


class TestWhatTheWaitHoldsBack:
    def test_a_scan_start_is_refused_during_the_wait_and_works_after(
        self, client: TestClient
    ) -> None:
        plex_wait.begin()
        refused = client.post("/api/scan/start")
        status = client.get("/api/scan/status").json()
        plex_wait.end()

        assert refused.status_code == 409
        assert refused.json()["code"] == "error.scan.waiting_for_plex"
        assert status["waiting_for_plex_since"] is not None
        after = client.get("/api/scan/status").json()
        assert after["waiting_for_plex_since"] is None
        assert client.post("/api/scan/start").status_code == 200

    def test_the_leaving_soon_update_is_refused_during_the_wait(self, client: TestClient) -> None:
        plex_wait.begin()
        refused = client.post("/api/leaving-soon/sync")
        plex_wait.end()

        assert refused.status_code == 409
        assert refused.json()["code"] == "error.scan.waiting_for_plex"

    async def test_the_scheduled_scan_skips_during_the_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran: list[int] = []

        async def run_scan(**_: Any) -> Any:
            ran.append(1)
            return SimpleNamespace(id=1, item_count=0)

        monkeypatch.setattr(scan_runner, "run_scan", run_scan)
        plex_wait.begin()
        await scheduler.scheduled_scan(None, None, None, None)  # type: ignore[arg-type]
        assert ran == []

        plex_wait.end()
        await scheduler.scheduled_scan(None, None, None, None)  # type: ignore[arg-type]
        assert ran == [1]


def test_shutdown_cancels_a_wait_still_running(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never_quiet(*_a: Any, **_k: Any) -> str:
        await asyncio.Event().wait()
        return "quiet"

    launches: list[str] = []
    monkeypatch.setattr(plex_wait, "wait_until_quiet", never_quiet)
    monkeypatch.setattr(runs, "launch_scan", lambda a: launches.append("scan"))
    app = client.app

    async def start() -> None:
        await runs.start_plex_wait(app)  # type: ignore[arg-type]

    client.portal.call(start)  # type: ignore[union-attr]
    task = app.state.plex_wait_task  # type: ignore[attr-defined]
    assert plex_wait.waiting_since() is not None

    client.__exit__(None, None, None)

    assert task.done()
    assert plex_wait.waiting_since() is None
    assert launches == []
