# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plex sign-in, and the check that keeps strangers out.

The attack these guard against is not exotic. plex.tv issues a valid token to
anyone with a free Plex account. If Reaper logs in whoever authenticates, then
*any person on the internet* can reach an admin console that deletes a media
library. Maintainerr has no auth at all; Seerr trusts whoever logs in first.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from reaper.clients.plextv import PlexConnection, PlexTvClient, plex_headers
from reaper.config import RuntimeSafety

SAFETY = RuntimeSafety(destructive_enabled=False)
CID = "reaper-uuid-1234"
OUR_SERVER = "abc123machineid"


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

    @respx.mock
    async def test_the_owner_is_admitted(self) -> None:
        respx.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(
                200, json=[_resource(client_identifier=OUR_SERVER, owned=True)]
            )
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.owns_server("their-token", OUR_SERVER) is True

    @respx.mock
    async def test_a_stranger_with_a_valid_plex_account_is_refused(self) -> None:
        """The whole point. They authenticated successfully -- plex.tv gave them a
        real token -- but they own no server, so they are not our owner."""
        respx.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(200, json=[])
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.owns_server("stranger-token", OUR_SERVER) is False

    @respx.mock
    async def test_someone_who_owns_a_different_server_is_refused(self) -> None:
        """A Plex user with their own server at home must not get into *our* admin
        console. Checking merely 'do you own any server' would admit them."""
        respx.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(
                200,
                json=[_resource(client_identifier="someone-elses-server", owned=True)],
            )
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.owns_server("other-owner-token", OUR_SERVER) is False

    @respx.mock
    async def test_a_shared_user_of_our_server_is_refused(self) -> None:
        """The most realistic attack: one of the ~100 people you share your Plex
        library with. They can see the server -- it appears in their resources --
        but `owned` is false. Reaper is admin-only."""
        respx.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(
                200,
                json=[_resource(client_identifier=OUR_SERVER, owned=False)],
            )
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.owns_server("friend-token", OUR_SERVER) is False

    @respx.mock
    async def test_a_non_server_resource_does_not_satisfy_the_check(self) -> None:
        """A Plex *client* (a phone, a TV) also appears in resources and can be
        `owned`. Only a resource that provides 'server' counts."""
        respx.get("https://plex.tv/api/v2/resources").mock(
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

    @respx.mock
    async def test_provides_is_matched_on_word_boundaries(self) -> None:
        """'provides' is comma-separated. A substring check would match
        'pubsub-server' and admit a device that is not a media server."""
        respx.get("https://plex.tv/api/v2/resources").mock(
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
        """Fail closed. Before setup, no machine id is stored -- and an empty id
        must match nothing rather than everything."""
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.owns_server("token", "") is False

    @respx.mock
    async def test_a_plex_tv_outage_is_not_an_open_door(self) -> None:
        """If we cannot verify ownership, we do not grant it. The local admin
        account is the way in when plex.tv is down -- not a degraded check."""
        respx.get("https://plex.tv/api/v2/resources").mock(return_value=httpx.Response(503))
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.owns_server("token", OUR_SERVER) is False


class TestServerDiscovery:
    @respx.mock
    async def test_only_owned_servers_are_offered_in_the_picker(self) -> None:
        respx.get("https://plex.tv/api/v2/resources").mock(
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

    @respx.mock
    async def test_the_resource_carries_a_token_so_no_manual_paste_is_needed(self) -> None:
        respx.get("https://plex.tv/api/v2/resources").mock(
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
    @respx.mock
    async def test_the_auth_url_carries_the_same_client_identifier(self) -> None:
        """It must be byte-identical across PIN creation, the auth URL and the poll.
        If it differs, authToken stays null forever -- and it looks exactly as
        though the user simply never approved."""
        respx.post("https://plex.tv/api/v2/pins").mock(
            return_value=httpx.Response(201, json={"id": 42, "code": "ABCD"})
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            pin = await client.create_pin()

        url = pin.auth_url(CID, forward_url="http://reaper.local/setup")

        assert f"clientID={CID}" in url
        assert "code=ABCD" in url
        assert "forwardUrl=" in url

    @respx.mock
    async def test_an_unapproved_pin_yields_no_token(self) -> None:
        respx.get("https://plex.tv/api/v2/pins/42").mock(
            return_value=httpx.Response(200, json={"id": 42, "authToken": None})
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.check_pin(42) is None

    @respx.mock
    async def test_an_approved_pin_yields_the_token(self) -> None:
        respx.get("https://plex.tv/api/v2/pins/42").mock(
            return_value=httpx.Response(200, json={"id": 42, "authToken": "user-token"})
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert await client.check_pin(42) == "user-token"


class TestTheSignInExemptionIsNarrow:
    """Signing in is a POST, so it must be permitted even in read-only mode --
    requiring the owner to enable deletion before they may log in would be absurd.

    But the exemption must be exactly one path, not a license for the plex.tv
    client to write anything. plex.tv has genuinely destructive endpoints
    (DELETE /devices/{id} unregisters a device; /api/v2/users/signout invalidates
    tokens), and none of them are ours to call."""

    @respx.mock
    async def test_pin_creation_is_allowed_in_read_only_mode(self) -> None:
        respx.post("https://plex.tv/api/v2/pins").mock(
            return_value=httpx.Response(201, json={"id": 1, "code": "AAAA"})
        )
        async with PlexTvClient(CID, safety=SAFETY) as client:
            assert (await client.create_pin()).code == "AAAA"

    @respx.mock
    async def test_every_other_plex_tv_mutation_is_still_blocked(self) -> None:
        from reaper.clients.base import SafetyViolationError

        route = respx.delete("https://plex.tv/devices/999")

        async with PlexTvClient(CID, safety=SAFETY) as client:
            with pytest.raises(SafetyViolationError, match="Blocked DELETE"):
                await client._send("DELETE", "/devices/999")

        assert not route.called

    @respx.mock
    async def test_signout_is_blocked(self) -> None:
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
        """python-plexapi defaults this to hex(getnode()) -- the MAC address --
        which is unstable in a container and leaks hardware detail."""
        import uuid

        assert plex_headers(str(uuid.uuid4()), version="0.1.0")["X-Plex-Client-Identifier"]


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_a_revoked_token_is_refused(status: int) -> None:
    respx.get("https://plex.tv/api/v2/resources").mock(return_value=httpx.Response(status))
    async with PlexTvClient(CID, safety=SAFETY) as client:
        assert await client.owns_server("stale-token", OUR_SERVER) is False
