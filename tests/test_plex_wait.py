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
from unittest import mock

import pytest
import requests
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from reaper.api import runs
from reaper.clients.plex import GuardedSession, PlexClient, PlexError
from reaper.config import RuntimeSafety
from reaper.services import app_settings, plex_wait, scan_runner, scheduler
from reaper.services.executor import ReapGateway, TrashPurge


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
    """An app whose reap wait reads a scripted Plex. ``launches`` records each scan start.

    ``safeties`` answers each read of the deletion switch in turn, the last one repeating.
    ``built`` records the switch each set of clients was built with. ``gateway`` makes the
    gateway handed back for that switch.
    """
    app = _app()
    app.launches = []
    app.plex = SimpleNamespace()
    app.safeties = [RuntimeSafety()]
    app.built = []
    app.gateway = lambda safety: ReapGateway()

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def runtime_safety(*_a: Any, **_k: Any) -> RuntimeSafety:
        safeties: list[RuntimeSafety] = app.safeties
        return safeties.pop(0) if len(safeties) > 1 else safeties[0]

    async def build(*_a: Any, safety: RuntimeSafety, **_k: Any) -> Any:
        app.built.append(safety)
        plex = PlexClient("http://plex.local:32400", "token", safety=RuntimeSafety())
        plex.is_scanning = app.plex.is_scanning  # type: ignore[method-assign]
        return app.gateway(safety), [plex]

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


ARMED = RuntimeSafety(destructive_enabled=True)
READ_ONLY = RuntimeSafety(destructive_enabled=False)


class _TrashPlex:
    """Plex for the purge. ``trash`` is each section's trash, by key. Each section holds
    ``counts`` items, 100 when unset. ``events`` is the list the scan polls write to, so the
    order of polls and purges reads back."""

    def __init__(
        self,
        events: list[str],
        trash: dict[int, set[int]],
        *,
        counts: dict[int, int] | None = None,
        unreadable: bool = False,
    ) -> None:
        self._events = events
        self._trash = trash
        self._counts = counts or {}
        self._unreadable = unreadable
        self.waiting_at_purge: list[bool] = []

    async def item_count(self, section_key: int) -> int:
        return self._counts.get(section_key, 100)

    async def trash_keys(self, section_key: int) -> set[int]:
        if self._unreadable:
            raise PlexError("error.plexclient.call_failed", what="list the trash", detail="down")
        return set(self._trash.get(section_key, set()))

    async def empty_trash(self, section_key: int) -> None:
        self.waiting_at_purge.append(plex_wait.waiting_since() is not None)
        self._events.append(f"empty {section_key}")


class _Radarr:
    def __init__(self, *, accessible: bool = True) -> None:
        self._accessible = accessible

    async def root_folders(self) -> list[dict[str, Any]]:
        return [{"path": "/movies", "accessible": self._accessible}]


class _GuardedPlex(PlexClient):
    """A real ``empty_trash`` that sends its PUT through a real guard, built from the switch
    this client was given. The trash read is scripted to hold only the run's listing."""

    def __init__(self, safety: RuntimeSafety) -> None:
        super().__init__("http://plex.test", "t", safety=safety)
        session = GuardedSession(self._safety)

        def section(key: int) -> Any:
            url = f"http://plex.test/library/sections/{key}/emptyTrash"
            return SimpleNamespace(emptyTrash=lambda: session.put(url))

        self._server = SimpleNamespace(library=SimpleNamespace(sectionByID=section))  # type: ignore[assignment]

    async def item_count(self, section_key: int) -> int:
        return 100

    async def trash_keys(self, section_key: int) -> set[int]:
        return {11}


def _purge(**changes: Any) -> TrashPurge:
    """Films lost listing 11 and Films 4K lost 21 and 22. Shows was rescanned and lost none."""
    fields: dict[str, Any] = {
        "deleted": {1: frozenset({11}), 2: frozenset({21, 22}), 3: frozenset()},
        "titles": {1: "Films", 2: "Films 4K", 3: "Shows"},
        "instances": frozenset({("radarr", 1)}),
    }
    return TrashPurge(**{**fields, **changes})


#: Each section's trash holds what the run removed there. Shows holds a key it did not.
_OURS = {1: {11}, 2: {21, 22}, 3: {31}}


def _polls(app: Any, *reads: bool) -> list[str]:
    """Script the scan polls, then ``False`` forever. Returns the events list they write."""
    events: list[str] = []
    queue = list(reads)

    async def is_scanning() -> bool:
        events.append("poll")
        return queue.pop(0) if queue else False

    app.plex.is_scanning = is_scanning
    return events


def _serve(app: Any, plex: Any, radarrs: dict[int, Any] | None = None) -> None:
    app.gateway = lambda safety: ReapGateway(radarr=radarrs or {1: _Radarr()}, plex=plex)


async def _wait(app: Any, purge: TrashPurge | None) -> None:
    await runs.start_plex_wait(app, purge)
    await app.state.plex_wait_task


class TestTheTrashPurgeWaitsForPlex:
    async def test_it_empties_each_section_whose_trash_is_only_ours_once_plex_goes_quiet(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        events = _polls(waiting_app, True, True, True)
        _serve(waiting_app, _TrashPlex(events, _OURS))

        await _wait(waiting_app, _purge())

        assert events == ["poll"] * 6 + ["empty 1", "empty 2"]
        assert waiting_app.launches == ["scan"]

    async def test_the_trash_is_emptied_while_the_wait_still_holds_scans_and_reaps(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        events = _polls(waiting_app)
        plex = _TrashPlex(events, _OURS)
        _serve(waiting_app, plex)

        await _wait(waiting_app, _purge())

        assert plex.waiting_at_purge == [True, True]
        assert plex_wait.waiting_since() is None

    async def test_a_wait_that_hits_its_ceiling_empties_nothing(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        events = _polls(waiting_app, *([True] * 10_000))
        _serve(waiting_app, _TrashPlex(events, _OURS))

        await _wait(waiting_app, _purge())

        assert set(events) == {"poll"}
        assert sum(slept) == plex_wait.CEILING_S
        assert waiting_app.launches == ["scan"]

    async def test_an_import_during_the_wait_cannot_hide_a_foreign_item_in_the_trash(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        """Films held 100. The run's 11 and someone else's 999 went to the trash, and one new
        title came in, so the section holds 99, one fewer, as if only 11 had gone."""
        events = _polls(waiting_app)
        _serve(waiting_app, _TrashPlex(events, {1: {11, 999}}, counts={1: 99}))

        await _wait(waiting_app, _purge(deleted={1: frozenset({11})}))

        assert set(events) == {"poll"}

    async def test_a_title_in_the_trash_before_the_reap_is_not_purged(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        events = _polls(waiting_app)
        _serve(waiting_app, _TrashPlex(events, {1: {500, 11}, 2: {21, 22, 600}}))

        await _wait(waiting_app, _purge())

        assert set(events) == {"poll"}

    async def test_an_empty_trash_is_not_purged(self, waiting_app: Any, slept: list[float]) -> None:
        """Plex has not trashed anything the run removed, so there is nothing to confirm."""
        events = _polls(waiting_app)
        _serve(waiting_app, _TrashPlex(events, {}))

        await _wait(waiting_app, _purge())

        assert set(events) == {"poll"}

    async def test_a_trash_read_that_fails_empties_nothing(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        events = _polls(waiting_app)
        _serve(waiting_app, _TrashPlex(events, _OURS, unreadable=True))

        await _wait(waiting_app, _purge())

        assert set(events) == {"poll"}

    @pytest.mark.parametrize(
        ("trash", "count"),
        [
            # The section's own listing, with titles the run never touched. It also carries
            # a collection the section count leaves out, so the sizes differ.
            ({11, 12, 13}, 2),
            # A section whose only remaining listing is the run's own. Only the size says
            # this read is the whole section and not the trash.
            ({11}, 1),
        ],
    )
    async def test_a_trash_read_that_returns_the_whole_section_empties_nothing(
        self, waiting_app: Any, slept: list[float], trash: set[int], count: int
    ) -> None:
        """A Plex that ignores ``trash=1`` answers with the section's whole listing."""
        events = _polls(waiting_app)
        _serve(waiting_app, _TrashPlex(events, {1: trash}, counts={1: count}))

        await _wait(waiting_app, _purge(deleted={1: frozenset({11})}))

        assert set(events) == {"poll"}

    async def test_a_root_folder_down_at_purge_time_empties_nothing(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        events = _polls(waiting_app)
        _serve(waiting_app, _TrashPlex(events, _OURS), {1: _Radarr(accessible=False)})

        await _wait(waiting_app, _purge())

        assert set(events) == {"poll"}

    async def test_an_instance_gone_by_purge_time_empties_nothing(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        """Radarr 1 was enabled when the reap started. After the wait only Radarr 2 is set
        up, so nothing can say whether Radarr 1's folders are still mounted."""
        events = _polls(waiting_app)
        _serve(waiting_app, _TrashPlex(events, _OURS), {2: _Radarr()})

        await _wait(waiting_app, _purge())

        assert set(events) == {"poll"}

    @pytest.mark.parametrize(("after", "sent"), [(ARMED, 1), (READ_ONLY, 0)])
    async def test_deletion_turned_off_during_the_wait_makes_the_guard_refuse(
        self, waiting_app: Any, slept: list[float], after: RuntimeSafety, sent: int
    ) -> None:
        """The switch is read again after the wait, and the purge's clients are built from
        that read. The armed case is the control: the same purge reaches the transport."""
        _polls(waiting_app)
        waiting_app.safeties = [ARMED, after]
        radarrs: dict[int, Any] = {1: _Radarr()}
        waiting_app.gateway = lambda safety: ReapGateway(radarr=radarrs, plex=_GuardedPlex(safety))

        with (
            mock.patch.object(requests.Session, "send", autospec=True) as send,
            capture_logs() as logs,
        ):
            send.return_value = requests.Response()
            await _wait(waiting_app, _purge(deleted={1: frozenset({11})}))

        assert waiting_app.built == [ARMED, after]
        assert send.call_count == sent
        if sent:
            prepared = send.call_args.args[1]
            assert (prepared.method, prepared.url) == (
                "PUT",
                "http://plex.test/library/sections/1/emptyTrash",
            )
        else:
            blocked = [e for e in logs if e["event"] == "plex.write_blocked"]
            assert [e.get("reason") for e in blocked] == ["not_armed"]

    async def test_a_cancel_during_the_wait_empties_nothing(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        events = _polls(waiting_app, *([True] * 10_000))
        _serve(waiting_app, _TrashPlex(events, _OURS))
        await runs.start_plex_wait(waiting_app, _purge())
        await asyncio.sleep(0)

        task = waiting_app.state.plex_wait_task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert set(events) <= {"poll"}
        assert waiting_app.launches == []

    async def test_a_section_that_lost_no_listings_is_never_purged(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        """Shows was rescanned, but the run removed nothing from it. Its trash holds a key
        the run did not remove, so the subset test refuses it too. This pins the outcome,
        not which check gave it."""
        events = _polls(waiting_app)
        _serve(waiting_app, _TrashPlex(events, _OURS))

        await _wait(waiting_app, _purge(deleted={3: frozenset()}))

        assert set(events) == {"poll"}

    async def test_a_wait_with_nothing_captured_empties_nothing(
        self, waiting_app: Any, slept: list[float]
    ) -> None:
        """A restart drops what the reap captured, and the reap's error path never had it.
        The wait still scans, and builds no clients for a purge."""
        events = _polls(waiting_app)
        _serve(waiting_app, _TrashPlex(events, _OURS))

        await _wait(waiting_app, None)

        assert set(events) == {"poll"}
        assert len(waiting_app.built) == 1
        assert waiting_app.launches == ["scan"]


class TestWhatTheWaitHoldsBack:
    def test_a_scan_start_is_refused_during_the_wait_and_works_after(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The scan this starts must not reach the database. Shutdown cancels a scan
        # mid-query, and that can hang the client's close (#1048).
        async def run_scan(**_: Any) -> Any:
            return SimpleNamespace(id=1)

        monkeypatch.setattr(scan_runner, "run_scan", run_scan)
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

    def test_a_reap_is_refused_during_the_wait_and_starts_no_run(self, client: TestClient) -> None:
        plex_wait.begin()
        try:
            refused = client.post("/api/runs/1/execute", json={"confirmation_phrase": "x"})
            running = client.get("/api/runs/execute/status").json()["running"]
        finally:
            plex_wait.end()

        assert refused.status_code == 409
        assert refused.json()["code"] == "error.scan.waiting_for_plex"
        assert running is False

    def test_a_practice_run_is_refused_during_the_wait(self, client: TestClient) -> None:
        plex_wait.begin()
        try:
            refused = client.post("/api/runs/1/dry-run")
        finally:
            plex_wait.end()

        assert refused.status_code == 409
        assert refused.json()["code"] == "error.scan.waiting_for_plex"

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
