# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plex sign-in, and the check that keeps strangers out.

The attack these guard against is not exotic. plex.tv issues a valid token to
anyone with a free Plex account. If Reaper logs in whoever authenticates, then
*any person on the internet* can reach an admin console that deletes a media
library. Maintainerr has no auth at all. Seerr trusts whoever logs in first.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from reaper.clients.base import IntegrationError
from reaper.clients.plextv import (
    PIN_POLL_INTERVAL,
    PIN_RATE_LIMIT_BACKOFF,
    PIN_RATE_LIMIT_MAX_BACKOFF,
    PlexConnection,
    PlexTvClient,
    plex_headers,
)
from reaper.config import RuntimeSafety

pytestmark = pytest.mark.httpx2(assert_all_called=False)

SAFETY = RuntimeSafety(destructive_enabled=False)
CID = "reaper-uuid-1234"
OUR_SERVER = "abc123machineid"
PIN_URL = "https://plex.tv/api/v2/pins/77"


def _resource(
    *,
    client_identifier: str,
    owned: bool,
    provides: str = "server",
    name: str = "Plex",
) -> dict[str, object]:
    return {
        "name": name,
        "clientIdentifier": client_identifier,
        "owned": owned,
        "provides": provides,
        "accessToken": "server-scoped-token",
        "connections": [
            {
                "uri": "https://192-168-1-50.abc.plex.direct:32400",
                "address": "192.168.1.50",
                "port": 32400,
                "local": True,
                "relay": False,
                "protocol": "https",
            }
        ],
    }


class TestOwnershipCheck:
    """The authorization boundary."""

    async def test_the_owner_is_admitted(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(
                200, json=[_resource(client_identifier=OUR_SERVER, owned=True)]
            )
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.owns_server("their-token", OUR_SERVER) is True

    async def test_a_stranger_with_a_valid_plex_account_is_refused(
        self, httpx2_mock: respx.Router
    ) -> None:
        """plex.tv gave them a real token, so they authenticated successfully. They
        own no server, though, so they are not our owner."""
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(200, json=[])
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.owns_server("stranger-token", OUR_SERVER) is False

    async def test_someone_who_owns_a_different_server_is_refused(
        self, httpx2_mock: respx.Router
    ) -> None:
        """A Plex user with their own server at home must not get into *our* admin
        console. Checking merely 'do you own any server' would admit them."""
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(
                200,
                json=[_resource(client_identifier="someone-elses-server", owned=True)],
            )
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.owns_server("other-owner-token", OUR_SERVER) is False

    async def test_a_shared_user_of_our_server_is_refused(self, httpx2_mock: respx.Router) -> None:
        """A person you share your Plex library with can see the server, since it
        appears in their resources, but `owned` is false for them. Reaper is
        admin-only, so they are refused."""
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(
                200,
                json=[_resource(client_identifier=OUR_SERVER, owned=False)],
            )
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.owns_server("friend-token", OUR_SERVER) is False

    async def test_a_non_server_resource_does_not_satisfy_the_check(
        self, httpx2_mock: respx.Router
    ) -> None:
        """A Plex *client* (a phone, a TV) also appears in resources and can be
        `owned`. Only a resource that provides 'server' counts."""
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(
                200,
                json=[
                    _resource(
                        client_identifier=OUR_SERVER, owned=True, provides="player,controller"
                    )
                ],
            )
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.owns_server("token", OUR_SERVER) is False

    async def test_provides_is_matched_on_word_boundaries(self, httpx2_mock: respx.Router) -> None:
        """'provides' is comma-separated. A substring check would match
        'pubsub-server' and admit a device that is not a media server."""
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(
                200,
                json=[
                    _resource(client_identifier=OUR_SERVER, owned=True, provides="pubsub-server")
                ],
            )
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.owns_server("token", OUR_SERVER) is False

    async def test_an_unconfigured_reaper_admits_nobody(self) -> None:
        """Fail closed. Before setup, no machine id is stored, so an empty id must
        match nothing rather than matching everything."""
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.owns_server("token", "") is False

    async def test_a_plex_tv_outage_is_not_an_open_door(self, httpx2_mock: respx.Router) -> None:
        """The check never grants ownership it cannot verify. When plex.tv is down,
        the local admin account is the way in, not a check that passes by default."""
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(return_value=httpx.Response(503))
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.owns_server("token", OUR_SERVER) is False


class TestServerDiscovery:
    async def test_only_owned_servers_are_offered_in_the_picker(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(
                200,
                json=[
                    _resource(client_identifier="mine", owned=True, name="Mine"),
                    _resource(client_identifier="shared", owned=False, name="A friend's"),
                    _resource(
                        client_identifier="phone", owned=True, provides="player", name="Phone"
                    ),
                ],
            )
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            servers = await client.owned_servers("token")

        assert [s.name for s in servers] == ["Mine"]

    async def test_the_resource_carries_a_token_so_no_manual_paste_is_needed(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(
                200, json=[_resource(client_identifier=OUR_SERVER, owned=True)]
            )
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            server = (await client.owned_servers("token"))[0]

        assert server.access_token == "server-scoped-token"
        assert server.client_identifier == OUR_SERVER  # this IS the machineIdentifier


class TestConnectionPreference:
    def _c(self, *, local: bool, relay: bool, protocol: str) -> PlexConnection:
        return PlexConnection(
            uri="https://x", address="a", port=32400, local=local, relay=relay, protocol=protocol
        )

    def test_local_https_wins_and_relay_loses(self) -> None:
        """Relay is bandwidth-capped and proxied through Plex, so it is a fallback,
        never a default."""
        connections = [
            self._c(local=False, relay=True, protocol="https"),
            self._c(local=False, relay=False, protocol="https"),
            self._c(local=True, relay=False, protocol="http"),
            self._c(local=True, relay=False, protocol="https"),
        ]
        ranked = sorted(connections, key=lambda c: c.rank)

        assert (ranked[0].local, ranked[0].protocol) == (True, "https")
        assert ranked[-1].relay is True


class TestPinFlow:
    async def test_the_auth_url_carries_the_same_client_identifier(
        self, httpx2_mock: respx.Router
    ) -> None:
        """It must be byte-identical across PIN creation, the auth URL, and the poll.
        If it differs, authToken stays null forever, and it looks exactly as though
        the user simply never approved."""
        httpx2_mock.post("https://plex.tv/api/v2/pins").mock(
            return_value=httpx.Response(201, json={"id": 42, "code": "ABCD"})
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            pin = await client.create_pin()

        url = pin.auth_url(CID, forward_url="http://reaper.local/setup")

        assert f"clientID={CID}" in url
        assert "code=ABCD" in url
        assert "forwardUrl=" in url

    async def test_an_unapproved_pin_yields_no_token(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get("https://plex.tv/api/v2/pins/42").mock(
            return_value=httpx.Response(200, json={"id": 42, "authToken": None})
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.check_pin(42) is None

    async def test_an_approved_pin_yields_the_token(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get("https://plex.tv/api/v2/pins/42").mock(
            return_value=httpx.Response(200, json={"id": 42, "authToken": "user-token"})
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.check_pin(42) == "user-token"


class TestWaitingForAnApprovedPin:
    """The poll loop that waits for a full-power Plex account token.

    Two branches carry the real risk. A ``429`` response means "slow down", not an error,
    so aborting on one would kill a sign-in the owner has not finished yet. The deadline is
    a real bound, not just a loop condition, so a server asking for a long wait cannot hold
    the call past its timeout.

    The tests record each sleep instead of waiting it out, using the ``slept`` fixture
    (``conftest.py``), which also owns the clock those sleeps advance. That makes the pacing
    assertable: a honored ``Retry-After`` would otherwise be invisible, since the only
    observable would be that the call eventually returned.
    """

    async def test_a_token_that_arrives_on_a_later_poll_is_returned(
        self, httpx2_mock: respx.Router, slept: list[float]
    ) -> None:
        """The ordinary sign-in. The owner takes a few seconds to approve, and the loop
        keeps asking at its own interval until the token appears."""
        httpx2_mock.get(PIN_URL).mock(
            side_effect=[
                httpx.Response(200, json={"id": 77, "authToken": None}),
                httpx.Response(200, json={"id": 77, "authToken": None}),
                httpx.Response(200, json={"id": 77, "authToken": "user-token"}),
            ]
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            token = await client.wait_for_pin(77, timeout=60.0)

        assert token == "user-token"
        assert slept == [PIN_POLL_INTERVAL, PIN_POLL_INTERVAL]

    async def test_a_rate_limit_waits_the_time_the_server_asked_for_and_carries_on(
        self, httpx2_mock: respx.Router, slept: list[float]
    ) -> None:
        """plex.tv asks for seven seconds, so the loop waits seven instead of its own
        fixed fallback, then keeps polling instead of failing the sign-in."""
        httpx2_mock.get(PIN_URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "7"}, json={}),
                httpx.Response(200, json={"id": 77, "authToken": "user-token"}),
            ]
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            token = await client.wait_for_pin(77, timeout=60.0)

        assert token == "user-token"
        assert slept == [7.0]

    async def test_a_bare_rate_limit_falls_back_to_the_fixed_backoff(
        self, httpx2_mock: respx.Router, slept: list[float]
    ) -> None:
        """No ``Retry-After`` to honor, so the client picks its own pace. Polling straight
        on would earn a second refusal."""
        httpx2_mock.get(PIN_URL).mock(
            side_effect=[
                httpx.Response(429, json={}),
                httpx.Response(200, json={"id": 77, "authToken": "user-token"}),
            ]
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            token = await client.wait_for_pin(77, timeout=60.0)

        assert token == "user-token"
        assert slept == [PIN_RATE_LIMIT_BACKOFF]

    async def test_an_extravagant_retry_after_is_capped(
        self, httpx2_mock: respx.Router, slept: list[float]
    ) -> None:
        """The server's pacing is capped, not followed exactly. An hour-long wait would
        strand the sign-in in a single sleep while the owner watches a code that expires
        first."""
        httpx2_mock.get(PIN_URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "3600"}, json={}),
                httpx.Response(200, json={"id": 77, "authToken": "user-token"}),
            ]
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            token = await client.wait_for_pin(77, timeout=600.0)

        assert token == "user-token"
        assert slept == [PIN_RATE_LIMIT_MAX_BACKOFF]

    async def test_the_deadline_clips_a_backoff_the_server_chose(
        self, httpx2_mock: respx.Router, slept: list[float]
    ) -> None:
        """A twenty-second backoff is honored while the window has room for it, then it is
        clipped to the fifteen seconds left, and the sign-in is reported as not completed.
        The call never sits inside a sleep the server chose past the deadline the caller set.

        The window is spent in sleeps rather than wall-clock time. The ``slept`` fixture
        advances the clock by each delay, so the two sleeps below are exactly what uses up
        the window.
        """
        httpx2_mock.get(PIN_URL).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "20"}, json={})
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            token = await client.wait_for_pin(77, timeout=35.0)

        assert token is None
        assert slept == [20.0, 15.0]

    async def test_an_unapproved_pin_gives_up_at_the_deadline(
        self, httpx2_mock: respx.Router, slept: list[float]
    ) -> None:
        """Nobody approved it. The loop polls at its own interval for the whole window,
        then returns "not completed" instead of raising, so the route above can tell the
        browser to keep waiting or start over.

        A window of exactly three poll intervals produces exactly three polls, which is
        what this test asserts.
        """
        route = httpx2_mock.get(PIN_URL).mock(
            return_value=httpx.Response(200, json={"id": 77, "authToken": None})
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            token = await client.wait_for_pin(77, timeout=3 * PIN_POLL_INTERVAL)

        assert token is None
        assert route.call_count == 3
        assert slept == [PIN_POLL_INTERVAL] * 3

    async def test_a_real_failure_is_not_swallowed_as_back_pressure(
        self, httpx2_mock: respx.Router
    ) -> None:
        """Only a 429 means "slow down". A 500 is plex.tv being broken, and polling it for
        five minutes would hide the outage behind a sign-in that never completes."""
        httpx2_mock.get(PIN_URL).mock(return_value=httpx.Response(500, json={}))

        async with PlexTvClient(CID, safety=SAFETY) as client:
            with pytest.raises(IntegrationError):
                await client.wait_for_pin(77, timeout=60.0)


class TestTheSignInExemptionIsNarrow:
    """Signing in is a POST, so it must be permitted even in read-only mode. Requiring
    the owner to enable deletion before they can log in would make no sense.

    The exemption covers exactly one path, though, not a license for the plex.tv
    client to write anything. plex.tv has genuinely destructive endpoints. DELETE
    /devices/{id} unregisters a device, and /api/v2/users/signout invalidates tokens.
    None of them are ours to call."""

    async def test_pin_creation_is_allowed_in_read_only_mode(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.post("https://plex.tv/api/v2/pins").mock(
            return_value=httpx.Response(201, json={"id": 1, "code": "AAAA"})
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert (await client.create_pin()).code == "AAAA"

    async def test_every_other_plex_tv_mutation_is_still_blocked(
        self, httpx2_mock: respx.Router
    ) -> None:
        from reaper.clients.base import SafetyViolationError

        route = httpx2_mock.delete("https://plex.tv/devices/999")

        async with PlexTvClient(CID, safety=SAFETY) as client:
            with pytest.raises(SafetyViolationError, match="Blocked DELETE"):
                await client._send("DELETE", "/devices/999")

        assert not route.called

    async def test_signout_is_blocked(self, httpx2_mock: respx.Router) -> None:
        """Nothing in Reaper should ever invalidate the owner's Plex tokens."""
        from reaper.clients.base import SafetyViolationError

        async with PlexTvClient(CID, safety=SAFETY) as client:
            with pytest.raises(SafetyViolationError):
                await client._send("POST", "/api/v2/users/signout")


class TestHeaders:
    def test_the_client_identifier_is_sent_and_json_is_requested(self) -> None:
        """/api/v2/resources *requires* X-Plex-Client-Identifier and returns XML
        unless JSON is asked for."""
        headers = plex_headers(CID, version="0.1.0")

        assert headers["X-Plex-Client-Identifier"] == CID
        assert headers["Accept"] == "application/json"
        assert headers["X-Plex-Product"] == "Reaper"

    def test_the_identifier_is_not_derived_from_hardware(self) -> None:
        """python-plexapi defaults this to hex(getnode()), the MAC address, which
        is unstable in a container and leaks hardware detail."""
        import uuid

        assert plex_headers(str(uuid.uuid4()), version="0.1.0")["X-Plex-Client-Identifier"]


@pytest.mark.parametrize("status", [401, 403])
async def test_a_revoked_token_is_refused(status: int, httpx2_mock: respx.Router) -> None:
    httpx2_mock.get("https://plex.tv/api/v2/resources").mock(return_value=httpx.Response(status))
    async with PlexTvClient(CID, safety=SAFETY) as client:
        assert await client.owns_server("stale-token", OUR_SERVER) is False
