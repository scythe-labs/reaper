# SPDX-License-Identifier: AGPL-3.0-or-later
"""GuardedTransport and the Tautulli allow-list.

These are the tests that matter most in the whole suite. Everything else is
correctness; this is the thing standing between a bug and someone's media library.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import httpx2
import pytest
import respx

from reaper.clients.arr import RadarrClient, SonarrClient
from reaper.clients.base import IntegrationError, SafetyViolationError
from reaper.clients.tautulli import READ_COMMANDS, TautulliClient
from reaper.config import RuntimeSafety

# The migrated clients speak httpx2; respx cannot intercept an httpx2 client, so the mocks
# ride the ``httpx2_mock`` fixture (a respx.Router retargeted at httpcore2). ``assert_all_called``
# is off to match the old ``@respx.mock`` default: some tests register a route a refusal
# path never reaches.
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
            with pytest.raises(SafetyViolationError, match="not declared to the action journal"):
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
    """The per-instance "check the server's certificate" switch is only real if the flag
    survives all the way to the httpx2 transport. Pin both directions, and pin the
    default: nothing may quietly construct an unverified client."""

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


class TestTypedMutationMethods:
    """The destructive *arr calls the executor actually issues. Each goes through
    ``_mutate``, so it declares intent to the guard -- and each is still refused unless
    deletion is enabled on the host. The parameters differ per *arr, and getting them
    wrong deletes without excluding (Radarr) or fails to prune (Sonarr)."""

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
        # Radarr's spelling, and both true: delete the files, add the import exclusion.
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
    """HTTP-method filtering is not sufficient for Tautulli.

    Its API is GET /api/v2?cmd=..., its key is full admin, and delete_library,
    delete_history and restart are all GETs. GuardedTransport would wave every one
    of them straight through.
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

        # The decisive assertion: the request was never even constructed.
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
        history query would look like an empty history -- i.e. 'never watched'."""
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
    """A wrong key and a service outage must not be treated alike: invalidating
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
