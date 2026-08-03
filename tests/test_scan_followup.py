# SPDX-License-Identifier: AGPL-3.0-or-later
"""A scan requested mid-scan runs AFTER the current one, never silently merges into it.

The failure this pins was observed live: a policy save fires an auto-rescan, but a scan
was already running -- started under the OLD policy. The start request "followed" the
running scan, its snapshot landed carrying the old policy's hashes, and the policy
page's "needs a fresh scan" notice never cleared, however long the owner waited. A scan
reads the library under the policies in force when it *begins*, so the only honest
response to a start request mid-run is to queue exactly one follow-up run.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from reaper.api import scan as scan_api
from reaper.services import scan_runner


def _request() -> Any:
    """The minimal duck-typed Request the route reads: ``app.state`` attributes only.
    The fake ``run_scan`` ignores every constructor input, so placeholders suffice."""
    state = SimpleNamespace(
        settings=None,
        secret_box=None,
        cache_engine=None,
        session_factory=None,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def test_a_start_request_mid_scan_queues_exactly_one_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    gate = asyncio.Event()

    async def fake_run_scan(**_: Any) -> Any:
        calls.append(len(calls))
        await gate.wait()
        return SimpleNamespace(id=len(calls))

    monkeypatch.setattr(scan_runner, "run_scan", fake_run_scan)
    request = _request()

    status = await scan_api.start_scan(request)
    assert status.running is True
    await asyncio.sleep(0)  # let the background task enter run_scan
    assert calls == [0]

    # Two more start requests while the scan runs: both fold into ONE queued follow-up.
    second = await scan_api.start_scan(request)
    third = await scan_api.start_scan(request)
    assert second is status and third is status
    assert status.followup_queued is True
    assert calls == [0]  # nothing launched in parallel

    gate.set()  # the first run finishes; the queued follow-up starts and finishes too
    await request.app.state.scan_task

    assert calls == [0, 1]  # exactly two runs: the original plus ONE follow-up
    assert status.running is False
    assert status.followup_queued is False
    assert status.phase == "complete"
    # The reported snapshot is the follow-up's -- the one that saw the saved policy.
    assert status.snapshot_id == 2


async def test_running_stays_true_across_the_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser polls ``running`` and refreshes the simulator on the running->stopped
    edge. A false edge between the two runs would re-simulate against the stale snapshot
    and flash the very notice the follow-up exists to clear."""
    seen_running: list[bool] = []
    gates = [asyncio.Event(), asyncio.Event()]

    async def fake_run_scan(**_: Any) -> Any:
        call = len(seen_running)
        seen_running.append(True)
        await gates[call].wait()
        return SimpleNamespace(id=call + 1)

    monkeypatch.setattr(scan_runner, "run_scan", fake_run_scan)
    request = _request()

    status = await scan_api.start_scan(request)
    await asyncio.sleep(0)
    await scan_api.start_scan(request)  # queue the follow-up

    gates[0].set()  # first run completes; the follow-up begins
    for _ in range(100):  # bounded: a broken hand-off must fail, not hang
        if len(seen_running) >= 2:
            break
        await asyncio.sleep(0)
    assert len(seen_running) == 2, "the follow-up run never started"
    assert status.running is True  # no false "done" edge between the runs
    assert status.followup_queued is False  # consumed by the hand-off

    gates[1].set()
    await request.app.state.scan_task
    assert status.running is False


async def test_an_errored_run_drops_the_queued_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rerunning after a failure would repeat (or mask) the error the owner needs to
    see; the queue dies with the run and the error is what the UI shows."""
    calls: list[int] = []
    gate = asyncio.Event()

    async def fake_run_scan(**_: Any) -> Any:
        calls.append(len(calls))
        await gate.wait()
        raise scan_runner.ScanConfigError("no sources are configured yet")

    monkeypatch.setattr(scan_runner, "run_scan", fake_run_scan)
    request = _request()

    status = await scan_api.start_scan(request)
    await asyncio.sleep(0)
    await scan_api.start_scan(request)
    assert status.followup_queued is True

    gate.set()
    await request.app.state.scan_task

    assert calls == [0]  # the follow-up never ran
    assert status.running is False
    assert status.followup_queued is False
    assert status.phase == "error"
    assert status.error is not None
