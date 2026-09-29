# SPDX-License-Identifier: AGPL-3.0-or-later
"""GuardedTransport and the Tautulli allow-list are the tests that matter most in the suite.
They stand between a bug and someone's media library."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import httpx2
import pytest
import respx
from structlog.testing import capture_logs

from reaper.clients import arr
from reaper.clients.arr import RadarrClient, SonarrClient
from reaper.clients.base import GuardedTransport, IntegrationError, SafetyViolationError
from reaper.clients.tautulli import READ_COMMANDS, TautulliClient
from reaper.config import RuntimeSafety

# The migrated clients speak httpx2. respx cannot intercept an httpx2 client, so the mocks
# ride the ``httpx2_mock`` fixture, a respx.Router retargeted at httpcore2.
# ``assert_all_called`` is off to match respx's own ``@respx.mock`` default, since some tests
# register a route that a refusal path never reaches.
pytestmark = pytest.mark.httpx2(assert_all_called=False)

READ_ONLY = RuntimeSafety(destructive_enabled=False)
ARMED = RuntimeSafety(destructive_enabled=True)


def json_body(route: respx.Route) -> Any:
    """The JSON body of the most recent request to a mocked route."""
    return json.loads(route.calls.last.request.content)


class TestMutationsAreBlocked:
    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
    async def test_every_mutating_method_is_refused_in_read_only_mode(self, method: str) -> None:
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(SafetyViolationError, match="Blocked"):
                await client._send(method, "/api/v3/movie/1")

    async def test_an_armed_mutation_is_still_blocked_without_a_journal_entry(self) -> None:
        """Enabling deletion is not enough. A destructive call must also be
        declared to the action journal *before* it is sent, so that a crash
        mid-run leaves a record of what was attempted."""
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            with pytest.raises(SafetyViolationError, match="wasn't declared to the action journal"):
                await client._send("DELETE", "/api/v3/movie/1")

    async def test_a_declared_mutation_passes_when_armed(self, httpx2_mock: respx.Router) -> None:
        """The one path that is allowed to delete."""
        route = httpx2_mock.delete("https://radarr.test/api/v3/movie/1").mock(
            return_value=httpx.Response(200, json={})
        )
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            response = await client._client.request(
                "DELETE",
                "/api/v3/movie/1",
                extensions={"reaper_mutation_approved": True},
            )

        assert response.status_code == 200
        assert route.called

    async def test_reads_are_never_blocked(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get("https://radarr.test/api/v3/system/status").mock(
            return_value=httpx.Response(200, json={"version": "6.3.0"})
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            assert (await client.system_status())["version"] == "6.3.0"


class TestTlsVerificationReachesTheTransport:
    """The per-instance "check the server's certificate" switch only works if the flag
    survives all the way to the httpx2 transport. This pins both directions, plus the
    default. Nothing may quietly construct an unverified client."""

    @staticmethod
    def _spy_on_transport(monkeypatch: pytest.MonkeyPatch, seen: list[object]) -> None:
        real = httpx2.AsyncHTTPTransport

        def spy(*args: Any, **kwargs: Any) -> httpx2.AsyncHTTPTransport:
            seen.append(kwargs.get("verify"))
            return real(*args, **kwargs)

        monkeypatch.setattr("reaper.clients.base.httpx2.AsyncHTTPTransport", spy)

    @pytest.mark.parametrize("verify", [True, False])
    async def test_the_verify_flag_lands_on_the_httpx_transport(
        self, monkeypatch: pytest.MonkeyPatch, verify: bool
    ) -> None:
        seen: list[object] = []
        self._spy_on_transport(monkeypatch, seen)
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY, verify=verify):
            pass
        assert seen == [verify]

    async def test_verification_is_on_when_no_one_says_otherwise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[object] = []
        self._spy_on_transport(monkeypatch, seen)
        async with TautulliClient("https://tautulli.test", "k", safety=READ_ONLY):
            pass
        assert seen == [True]


class TestClosingAClientReleasesItsSockets:
    """Someone must own closing the connection. ``scan_runner`` enters or push-callbacks
    every Radarr, Sonarr, Tautulli and Seerr client it builds, and each close must actually
    release its sockets. ``AsyncClient.aclose()`` only closes its own transport, which here
    is the guard, and ``AsyncBaseTransport.aclose`` is a no-op by default. Without an
    override, closing the client would never reach the real connection pool."""

    async def test_the_wrapped_transport_is_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        closed: list[bool] = []
        real = httpx2.AsyncHTTPTransport

        class WatchedTransport(real):  # type: ignore[misc,valid-type]
            async def aclose(self) -> None:
                closed.append(True)
                await super().aclose()

        monkeypatch.setattr("reaper.clients.base.httpx2.AsyncHTTPTransport", WatchedTransport)

        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY):
            pass

        assert closed == [True]

    async def test_the_guard_delegates_rather_than_inheriting_the_no_op(self) -> None:
        """Pinned on the guard directly too. The client-level test above would still pass if
        someone gave ``BaseClient`` its own second close path, and the defect would be back
        for anyone constructing a ``GuardedTransport`` some other way."""
        closed: list[bool] = []

        class WatchedTransport(httpx2.AsyncHTTPTransport):
            async def aclose(self) -> None:
                closed.append(True)
                await super().aclose()

        inner = WatchedTransport()
        await GuardedTransport(inner, READ_ONLY).aclose()

        assert closed == [True]


class TestTypedMutationMethods:
    """The destructive *arr calls the executor actually issues. Each goes through
    ``_mutate``, so it declares intent to the guard. Each is still refused unless deletion
    is enabled on the host. The parameters differ per *arr, and getting them wrong deletes
    without excluding (Radarr) or fails to prune (Sonarr)."""

    async def test_radarr_delete_movie_sends_the_right_params_when_armed(
        self, httpx2_mock: respx.Router
    ) -> None:
        route = httpx2_mock.delete("https://radarr.test/api/v3/movie/42").mock(
            return_value=httpx.Response(200, json={})
        )
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            await client.delete_movie(42)

        assert route.called
        query = route.calls.last.request.url.params
        # Radarr's own parameter names, both set true, to delete the files and add the
        # import exclusion.
        assert query["deleteFiles"] == "true"
        assert query["addImportExclusion"] == "true"
        assert "addImportListExclusion" not in query  # never Sonarr's

    async def test_radarr_delete_movie_is_refused_read_only(self) -> None:
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(SafetyViolationError, match="Blocked"):
                await client.delete_movie(42)

    async def test_sonarr_unmonitor_targets_the_season_and_is_guarded(
        self, httpx2_mock: respx.Router
    ) -> None:
        route = httpx2_mock.post("https://sonarr.test/api/v3/seasonpass").mock(
            return_value=httpx.Response(200, json={})
        )
        async with SonarrClient("https://sonarr.test", "k", safety=ARMED) as client:
            await client.unmonitor_season(7, 3)

        assert route.called
        body = json_body(route)
        assert body["series"][0]["id"] == 7
        assert body["series"][0]["seasons"][0] == {"seasonNumber": 3, "monitored": False}

    async def test_sonarr_delete_episode_files_sends_the_id_list(
        self, httpx2_mock: respx.Router
    ) -> None:
        route = httpx2_mock.delete("https://sonarr.test/api/v3/episodefile/bulk").mock(
            return_value=httpx.Response(200, json={})
        )
        async with SonarrClient("https://sonarr.test", "k", safety=ARMED) as client:
            await client.delete_episode_files([11, 22, 33])

        assert route.called
        assert json_body(route)["episodeFileIds"] == [11, 22, 33]
        assert route.calls.last.request.extensions["timeout"]["read"] == arr._DELETE_CEILING

    async def test_deleting_an_empty_id_list_sends_nothing(self, httpx2_mock: respx.Router) -> None:
        route = httpx2_mock.delete("https://sonarr.test/api/v3/episodefile/bulk")
        async with SonarrClient("https://sonarr.test", "k", safety=ARMED) as client:
            await client.delete_episode_files([])
        assert not route.called

    async def test_sonarr_mutations_are_refused_read_only(self) -> None:
        async with SonarrClient("https://sonarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(SafetyViolationError, match="Blocked"):
                await client.unmonitor_season(7, 3)
            with pytest.raises(SafetyViolationError, match="Blocked"):
                await client.delete_episode_files([1])


class TestTautulliAllowList:
    """Tautulli needs more than HTTP-method filtering to stay read-only.

    Its API is GET /api/v2?cmd=..., its key is full admin, and delete_library,
    delete_history and restart are all GETs. GuardedTransport alone would let every one of
    them through.
    """

    @pytest.mark.parametrize(
        "cmd",
        [
            "delete_library",
            "delete_history",
            "delete_all_library_history",
            "restart",
            "delete_user",
            "set_config",
            "delete_media_info_cache",
        ],
    )
    async def test_destructive_commands_are_refused(
        self, cmd: str, httpx2_mock: respx.Router
    ) -> None:
        route = httpx2_mock.get("https://tautulli.test/api/v2")

        async with TautulliClient("https://tautulli.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(SafetyViolationError, match="read-only allow-list"):
                await client.call(cmd)

        # The important check is that the request was never even constructed.
        assert not route.called

    async def test_destructive_commands_are_refused_even_when_deletion_is_enabled(
        self, httpx2_mock: respx.Router
    ) -> None:
        """Arming Reaper to delete *media* must not arm it to delete a user's
        Tautulli history. Reaper never writes to Tautulli, in any mode."""
        route = httpx2_mock.get("https://tautulli.test/api/v2")

        async with TautulliClient("https://tautulli.test", "k", safety=ARMED) as client:
            with pytest.raises(SafetyViolationError):
                await client.call("delete_library")

        assert not route.called

    async def test_allowed_read_commands_pass(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get("https://tautulli.test/api/v2").mock(
            return_value=httpx.Response(
                200, json={"response": {"result": "success", "data": {"stream_count": 0}}}
            )
        )
        async with TautulliClient("https://tautulli.test", "k", safety=READ_ONLY) as client:
            assert (await client.activity())["stream_count"] == 0

    def test_the_allow_list_contains_nothing_destructive(self) -> None:
        """A guard against a careless future addition."""
        forbidden = ("delete", "remove", "restart", "set_", "update", "edit", "add_", "import")
        for cmd in READ_COMMANDS:
            assert not cmd.startswith(forbidden), f"{cmd!r} looks destructive"

    async def test_an_error_envelope_becomes_an_exception(self, httpx2_mock: respx.Router) -> None:
        """Tautulli returns HTTP 200 with result='error'. Read naively, a failed
        history query would look the same as an empty one, meaning 'never watched'."""
        httpx2_mock.get("https://tautulli.test/api/v2").mock(
            return_value=httpx.Response(
                200, json={"response": {"result": "error", "message": "Invalid apikey"}}
            )
        )
        async with TautulliClient("https://tautulli.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError, match="Invalid apikey"):
                await client.history(rating_key=1)


class TestArrDeletionParametersDiffer:
    """Sonarr and Radarr use different parameter names on different routes, and
    each silently ignores the other's and returns 200. Confirmed from both
    projects' OpenAPI specs. Encoding them on the class means a call site cannot
    pick the wrong one."""

    def test_sonarr_and_radarr_disagree(self) -> None:
        assert SonarrClient.exclusion_param == "addImportListExclusion"
        assert RadarrClient.exclusion_param == "addImportExclusion"
        assert SonarrClient.exclusion_param != RadarrClient.exclusion_param

        assert SonarrClient.exclusion_path == "/importlistexclusion"
        assert RadarrClient.exclusion_path == "/exclusions"
        assert SonarrClient.exclusion_path != RadarrClient.exclusion_path


class TestErrorClassification:
    """A wrong key and a service outage must not be treated alike. Invalidating
    credentials on a transient 500 would have the owner re-entering keys during
    every blip."""

    @pytest.mark.parametrize(("status", "is_auth"), [(401, True), (403, True), (500, False)])
    async def test_auth_failures_are_distinguished_from_outages(
        self, status: int, is_auth: bool, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get("https://radarr.test/api/v3/system/status").mock(
            return_value=httpx.Response(status)
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError) as exc:
                await client.system_status()

        assert exc.value.is_auth_failure is is_auth


class TestTheArrRefusalIsOnTheRecordToo:
    """The arr-side counterpart to ``TestEveryRefusalIsOnTheRecord`` in
    ``test_plex_guard.py``.

    Both guards refuse through ``base.refuse_mutation``. This one stands in front of
    ``DELETE /api/v3/movie/{id}?deleteFiles=true``, the larger blast radius of the two.
    Without this pin, the whole http refusal-logging path could be reverted to an inline
    raise and the suite would still stay green.

    ``reason`` is the discriminator this test asserts on, never the human-readable sentence.
    """

    @pytest.mark.parametrize(
        ("safety", "declared", "reason"),
        [
            (READ_ONLY, False, "not_armed"),
            (ARMED, False, "not_declared"),
        ],
    )
    async def test_a_blocked_arr_write_says_why(
        self, safety: RuntimeSafety, declared: bool, reason: str
    ) -> None:
        transport = GuardedTransport(httpx2.AsyncHTTPTransport(), safety)
        request = httpx2.Request("DELETE", "https://radarr.test/api/v3/movie/7")
        if declared:
            request.extensions["reaper_mutation_approved"] = True

        with capture_logs() as logs, pytest.raises(SafetyViolationError):
            await transport.handle_async_request(request)

        blocked = [line for line in logs if line["event"] == "http.write_blocked"]
        assert len(blocked) == 1, logs
        assert blocked[0]["reason"] == reason
        assert blocked[0]["method"] == "DELETE"
        assert blocked[0]["path"] == "/api/v3/movie/7"

    async def test_the_path_carries_no_api_key(self) -> None:
        """The *arr key rides a header, but a query string reaches the log the same way a
        Plex token would, so the split is asserted here as well."""
        transport = GuardedTransport(httpx2.AsyncHTTPTransport(), READ_ONLY)
        request = httpx2.Request("DELETE", "https://radarr.test/api/v3/movie/7?apikey=supersecret")

        with capture_logs() as logs, pytest.raises(SafetyViolationError):
            await transport.handle_async_request(request)

        blocked = [line for line in logs if line["event"] == "http.write_blocked"]
        assert len(blocked) == 1, logs
        assert blocked[0]["path"] == "/api/v3/movie/7"
        assert "supersecret" not in repr(blocked)

    async def test_an_allowed_arr_write_says_nothing(self, httpx2_mock: respx.Router) -> None:
        """The control. A guard that logged every mutation would bury the refusals."""
        httpx2_mock.delete(host="radarr.test", path="/api/v3/movie/7").mock(
            return_value=httpx.Response(200, json={})
        )
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            with capture_logs() as logs:
                await client.delete_movie(7, delete_files=True, add_exclusion=False)

        assert [line for line in logs if line["event"] == "http.write_blocked"] == []


_BIN_CONFIG: dict[str, Any] = {
    "id": 1,
    "recycleBin": "",
    "recycleBinCleanupDays": 7,
    "fileDate": "none",
}


class TestPuttingARecycleBinBack:
    """Turning a Sonarr or Radarr recycle bin off needs deletion armed. Putting it back
    does not, so disarming mid-reap never leaves a bin off."""

    async def test_putting_a_bin_back_goes_through_with_deletion_off(
        self, httpx2_mock: respx.Router
    ) -> None:
        route = httpx2_mock.put("https://sonarr.test/api/v3/config/mediamanagement/1").mock(
            return_value=httpx.Response(202, json=1)
        )
        async with SonarrClient("https://sonarr.test", "k", safety=READ_ONLY) as client:
            await client.restore_recycle_bin(dict(_BIN_CONFIG), "/recycle/sonarr")

        # The whole settings object goes back, with only the bin changed.
        assert json_body(route) == {**_BIN_CONFIG, "recycleBin": "/recycle/sonarr"}

    async def test_the_exemption_follows_a_base_path(self, httpx2_mock: respx.Router) -> None:
        route = httpx2_mock.put("https://proxy.test/sonarr/api/v3/config/mediamanagement/1").mock(
            return_value=httpx.Response(202, json=1)
        )
        async with SonarrClient("https://proxy.test/sonarr", "k", safety=READ_ONLY) as client:
            await client.restore_recycle_bin(dict(_BIN_CONFIG), "/recycle/sonarr")
        assert route.called

    @pytest.mark.parametrize(
        ("base", "put"),
        [
            ("https://proxy.test/My%20Sonarr", "https://proxy.test/My%20Sonarr"),
            ("https://proxy.test/a/./sonarr", "https://proxy.test/a/sonarr"),
            ("https://proxy.test/%7Euser", "https://proxy.test/~user"),
        ],
    )
    async def test_the_exemption_follows_an_encoded_or_dotted_base_path(
        self, httpx2_mock: respx.Router, base: str, put: str
    ) -> None:
        route = httpx2_mock.put(f"{put}/api/v3/config/mediamanagement/1").mock(
            return_value=httpx.Response(202, json=1)
        )
        async with SonarrClient(base, "k", safety=READ_ONLY) as client:
            await client.restore_recycle_bin(dict(_BIN_CONFIG), "/recycle/sonarr")
        assert route.called

    async def test_turning_a_bin_off_is_refused_with_deletion_off(self) -> None:
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(SafetyViolationError, match="Blocked"):
                await client.turn_off_recycle_bin({**_BIN_CONFIG, "recycleBin": "/r"})

    async def test_turning_a_bin_off_sends_the_whole_object_when_armed(
        self, httpx2_mock: respx.Router
    ) -> None:
        route = httpx2_mock.put("https://radarr.test/api/v3/config/mediamanagement/1").mock(
            return_value=httpx.Response(202, json=1)
        )
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            await client.turn_off_recycle_bin({**_BIN_CONFIG, "recycleBin": "/r"})
        assert json_body(route) == _BIN_CONFIG

    async def test_a_restore_cannot_turn_a_bin_off(self) -> None:
        async with SonarrClient("https://sonarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(ValueError, match="needs a folder"):
                await client.restore_recycle_bin(dict(_BIN_CONFIG), "")

    async def test_the_restore_flag_reaches_no_other_path(self) -> None:
        """The exemption is the settings path alone. A delete marked as a restore is still
        refused with deletion off."""
        async with SonarrClient("https://sonarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(SafetyViolationError, match="Blocked"):
                await client._mutate(
                    "DELETE", "/api/v3/episodefile/bulk", json={"episodeFileIds": [1]}, restore=True
                )


MOVIE = "https://radarr.test/api/v3/movie/1"
BULK = "https://sonarr.test/api/v3/episodefile/bulk"
STATUS = "/api/v3/system/status"


@pytest.fixture
def fast_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arr, "_PING_EVERY", 0.02)
    monkeypatch.setattr(arr, "_PING_TIMEOUT", 0.5)
    monkeypatch.setattr(arr, "_MISSES_ALLOWED", 3)


class _Slow:
    """A DELETE that answers once ``release`` is set, and records a cancel.

    ``asyncio.sleep`` is instant under test, so the delete waits on an event instead.
    """

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.canceled = False

    async def answer(self, request: httpx.Request) -> httpx.Response:
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.canceled = True
            raise
        return httpx.Response(200, json={})


@pytest.mark.usefixtures("fast_watch")
class TestAWatchedDelete:
    """A delete waits while the instance answers pings and gives up when it stops."""

    async def test_a_slow_delete_with_answering_pings_returns(
        self, httpx2_mock: respx.Router
    ) -> None:
        slow = _Slow()
        delete = httpx2_mock.delete(MOVIE).mock(side_effect=slow.answer)

        def answer(request: httpx.Request) -> httpx.Response:
            if ping.call_count >= 4:
                slow.release.set()
            return httpx.Response(200, json={})

        ping = httpx2_mock.get(f"https://radarr.test{STATUS}").mock(side_effect=answer)
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            await _delete_movie(client)
        assert delete.call_count == 1
        assert ping.call_count >= 3

    async def test_pings_that_stop_answering_end_the_delete_with_no_status(
        self, httpx2_mock: respx.Router
    ) -> None:
        slow = _Slow()
        httpx2_mock.delete(MOVIE).mock(side_effect=slow.answer)
        ping = httpx2_mock.get(f"https://radarr.test{STATUS}").mock(
            return_value=httpx.Response(503)
        )
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            with pytest.raises(IntegrationError) as caught:
                await _delete_movie(client)
        assert caught.value.status is None
        assert caught.value.code == "error.integration.timed_out"
        assert caught.value.read_timed_out
        assert ping.call_count == arr._MISSES_ALLOWED
        assert slow.canceled

    async def test_an_answered_ping_resets_the_miss_count(self, httpx2_mock: respx.Router) -> None:
        slow = _Slow()
        delete = httpx2_mock.delete(MOVIE).mock(side_effect=slow.answer)
        answers = iter([503, 503, 200, 503, 503, 200, 503, 503])

        def answer(request: httpx.Request) -> httpx.Response:
            status = next(answers, 200)
            if ping.call_count >= 8:
                slow.release.set()
            return httpx.Response(status)

        ping = httpx2_mock.get(f"https://radarr.test{STATUS}").mock(side_effect=answer)
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            await _delete_movie(client)
        assert delete.call_count == 1
        assert ping.call_count >= 8

    async def test_the_deletes_own_error_surfaces_unchanged(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.delete(MOVIE).mock(return_value=httpx.Response(500, text="boom"))
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            with pytest.raises(IntegrationError) as caught:
                await _delete_movie(client)
        assert caught.value.status == 500

    async def test_cancelling_the_caller_cancels_the_delete_and_sends_no_more_pings(
        self, httpx2_mock: respx.Router
    ) -> None:
        slow = _Slow()
        httpx2_mock.delete(MOVIE).mock(side_effect=slow.answer)
        ping = httpx2_mock.get(f"https://radarr.test{STATUS}").mock(
            return_value=httpx.Response(200, json={})
        )
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            call = asyncio.ensure_future(_delete_movie(client))
            await asyncio.sleep(0.05)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            pings = ping.call_count
            await asyncio.sleep(0.1)
        assert slow.canceled
        assert ping.call_count == pings

    async def test_the_delete_carries_the_ceiling_as_its_read_limit(
        self, httpx2_mock: respx.Router
    ) -> None:
        route = httpx2_mock.delete(MOVIE).mock(return_value=httpx.Response(200, json={}))
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            await _delete_movie(client)
        assert route.calls.last.request.extensions["timeout"]["read"] == arr._DELETE_CEILING

    async def test_both_deletes_go_through_the_watched_path(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.delete(MOVIE).mock(return_value=httpx.Response(200, json={}))
        httpx2_mock.delete(BULK).mock(return_value=httpx.Response(200, json={}))
        watched: list[str] = []
        real = arr.ArrClient._watched_delete

        async def spy(self: arr.ArrClient, path: str, **kw: Any) -> None:
            watched.append(path)
            await real(self, path, **kw)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(arr.ArrClient, "_watched_delete", spy)
            async with RadarrClient("https://radarr.test", "k", safety=ARMED) as radarr:
                await _delete_movie(radarr)
            async with SonarrClient("https://sonarr.test", "k", safety=ARMED) as sonarr:
                await sonarr.delete_episode_files([1])
        assert watched == ["/api/v3/movie/1", "/api/v3/episodefile/bulk"]


async def _delete_movie(client: RadarrClient) -> None:
    await client.delete_movie(1)
