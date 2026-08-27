# SPDX-License-Identifier: AGPL-3.0-or-later
"""plex.tv: sign-in, server discovery, and the ownership check.

This module does two distinct jobs.

**Setup.** Instead of making the owner hunt for an ``X-Plex-Token`` through the
"View XML" trick, they sign in with Plex and pick their server from a list. Reaper
keeps the server's ``clientIdentifier`` (which is the same value as the
``machineIdentifier``), a reachable connection URI, and the resource's
``accessToken``.

**Login.** "Sign in with Plex" proves who someone is, but does not prove they should
be let in. plex.tv issues a valid token to any Plex account on the internet, and
``/api/v2/user`` will describe any of them. A naive implementation would let a
stranger into a tool that can delete a media library. For comparison, Maintainerr
ships with no auth at all, and Seerr trusts whoever logs in first.

So after the PIN flow, this module asks plex.tv, using that user's own token, which
servers they own, and requires this machine's id to be among them. ``owned`` is
answered relative to the token making the request, which is what makes it a real
check. It must never be answered with Reaper's own stored admin token, because that
token would always answer "yes".

Three details are easy to get wrong:

* ``/api/v2/resources`` requires ``X-Plex-Client-Identifier`` and returns XML unless
  asked for JSON. The older ``/api/resources`` (v1, XML) silently omits some owned
  servers, which breaks a server picker.
* ``X-Plex-Client-Identifier`` must be byte-identical across the PIN creation, the
  auth URL, and the poll. If it differs, ``authToken`` stays null forever, and it
  looks exactly as if the user never approved.
* This module stays on the legacy PIN flow (``plex.tv/api/v2/pins``). The newer JWT
  flow makes ``/api/v2/resources`` hand back a JWT per resource, and Plex Media
  Server rejects JWTs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx2
import structlog

from reaper.clients.base import BaseClient, IntegrationError, unexpected_body
from reaper.config import RuntimeSafety

log = structlog.get_logger(__name__)

PLEX_TV = "https://plex.tv"
AUTH_URL = "https://app.plex.tv/auth"

# A person signing in on another device needs seconds to a minute, and plex.tv
# rate-limits this endpoint. Polling once a second earns a 429 (observed) long before
# anyone has finished typing a password. plex.tv's own clients poll at a similar pace.
PIN_POLL_INTERVAL = 2.0
PIN_TIMEOUT = 300.0

# A 429 mid-poll does not mean the sign-in failed. It means we polled too eagerly,
# and the fix is to wait longer, not to abandon the flow and make the owner start over.
PIN_RATE_LIMIT_BACKOFF = 5.0

# The most this client will honor from a server-supplied ``Retry-After``. A few
# seconds from plex.tv is worth honoring as back-pressure. Anything naming minutes or
# hours, such as a rate limiter in front of plex.tv or a proxy on the LAN, would leave
# the sign-in sleeping far past its own deadline, with the operator stuck watching
# "Waiting..." with nothing to do but Ctrl-C. An external server does not get to set
# an unbounded sleep; notify/discord.py caps the same header for the same reason.
PIN_RATE_LIMIT_MAX_BACKOFF = 30.0


def plex_headers(client_identifier: str, *, version: str) -> dict[str, str]:
    """The identifying headers Plex expects.

    ``X-Plex-Client-Identifier`` keys the device row on the user's Plex account, so it
    is generated once at first boot and never regenerated. python-plexapi defaults it
    to ``hex(getnode())``, the machine's MAC address, which is unstable in a container
    and leaks hardware detail. This module does not use that default.
    """
    return {
        "Accept": "application/json",
        "X-Plex-Product": "Reaper",
        "X-Plex-Version": version,
        "X-Plex-Client-Identifier": client_identifier,
        "X-Plex-Device": "Reaper",
        "X-Plex-Device-Name": "Reaper",
        "X-Plex-Platform": "Python",
    }


@dataclass(frozen=True)
class PlexPin:
    pin_id: int
    code: str

    def auth_url(self, client_identifier: str, forward_url: str | None = None) -> str:
        params = {"clientID": client_identifier, "code": self.code}
        if forward_url:
            params["forwardUrl"] = forward_url
        # The fragment form is what Plex's own web auth expects.
        return f"{AUTH_URL}#?{urlencode(params)}"


@dataclass(frozen=True)
class PlexAccount:
    account_id: int
    uuid: str
    username: str
    email: str | None
    thumb: str | None


@dataclass(frozen=True)
class PlexConnection:
    uri: str
    address: str
    port: int
    local: bool
    relay: bool
    protocol: str

    @property
    def rank(self) -> tuple[int, int]:
        """Preference order: local before remote, https before http, relay last.

        Relay is bandwidth-capped and proxied through Plex, so it is a fallback and
        never a default.
        """
        location = 2 if self.relay else (0 if self.local else 1)
        scheme = 0 if self.protocol == "https" else 1
        return (location, scheme)


@dataclass(frozen=True)
class PlexResource:
    """A server (or other device) on the account."""

    name: str
    client_identifier: str
    """This is the PMS machineIdentifier. There is no separate field."""

    owned: bool
    provides: str
    access_token: str | None
    connections: list[PlexConnection]
    product_version: str | None = None
    presence: bool = False

    @property
    def is_server(self) -> bool:
        # Split on commas, because 'provides' is a comma-separated list. A plain
        # substring check would also match a hypothetical "pubsub-server".
        return "server" in [p.strip() for p in (self.provides or "").split(",")]

    def preferred_connections(self) -> list[PlexConnection]:
        return sorted(self.connections, key=lambda c: c.rank)


def _parse_resource(payload: dict[str, Any]) -> PlexResource:
    return PlexResource(
        name=str(payload.get("name") or "Plex Server"),
        client_identifier=str(payload.get("clientIdentifier") or ""),
        owned=bool(payload.get("owned")),
        provides=str(payload.get("provides") or ""),
        access_token=payload.get("accessToken") or None,
        product_version=payload.get("productVersion"),
        presence=bool(payload.get("presence")),
        connections=[
            PlexConnection(
                uri=str(c.get("uri") or ""),
                address=str(c.get("address") or ""),
                port=int(c.get("port") or 32400),
                local=bool(c.get("local")),
                relay=bool(c.get("relay")),
                protocol=str(c.get("protocol") or "https"),
            )
            for c in (payload.get("connections") or [])
        ],
    )


class PlexTvClient(BaseClient):
    """Talks to plex.tv, not to the media server."""

    service: ClassVar[str] = "plex.tv"

    def __init__(
        self,
        client_identifier: str,
        *,
        safety: RuntimeSafety,
        version: str = "0.1.0",
    ) -> None:
        super().__init__(
            PLEX_TV,
            safety=safety,
            headers=plex_headers(client_identifier, version=version),
            # Signing in is a POST. It reaches plex.tv, not a media server, and
            # cannot delete anything, so requiring the owner to enable deletion
            # before they can log in would make no sense. This path is narrowly
            # allow-listed rather than exempting the whole client, so the exemption
            # stays auditable.
            non_media_mutations=frozenset({"/api/v2/pins"}),
        )
        self.client_identifier = client_identifier

    async def _post(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """plex.tv's PIN creation is a POST. See ``non_media_mutations`` above.

        Routed through ``_send`` so a transport error, such as a plex.tv outage, or a
        non-JSON body, such as a maintenance page served with HTTP 200, becomes an
        ``IntegrationError``. That is the same normalization ``get_json`` gives every
        read. Without it, ``owns_server``'s ``except IntegrationError`` guard could
        not fail closed on a raw ``httpx2`` error.
        """
        response = await self._send("POST", path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise unexpected_body(self.service, response, path) from exc

    async def create_pin(self) -> PlexPin:
        data = await self._post("/api/v2/pins", params={"strong": "true"})
        return PlexPin(pin_id=int(data["id"]), code=str(data["code"]))

    async def check_pin(self, pin_id: int) -> str | None:
        """Poll a PIN. Returns the auth token once the user has approved it."""
        data = await self.get_json(f"/api/v2/pins/{pin_id}")
        token = (data or {}).get("authToken")
        return str(token) if token else None

    async def wait_for_pin(self, pin_id: int, *, timeout: float = PIN_TIMEOUT) -> str | None:
        """Poll until approved or the deadline passes.

        This backend polls, not the browser. A Plex auth token is a full-power
        credential for the user's entire account, and it should never pass through a
        web page. (Overseerr posts it from the browser to its own API.)

        A ``429`` from plex.tv is back-pressure, not an error. It means this client
        polled too eagerly, and the right response is to wait longer and keep going.
        Letting it propagate would abort a sign-in the owner has not even finished.

        ``timeout`` is a real bound, not just a loop condition. Every sleep is
        clipped to whatever time is left, so the call returns at the deadline and
        reports the sign-in as incomplete instead of sitting inside a sleep the
        server chose.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        async def _wait(seconds: float) -> bool:
            """Sleep, but never past the deadline. False once there is no time left."""
            remaining = deadline - loop.time()
            if remaining <= 0.0:
                return False
            await asyncio.sleep(min(seconds, remaining))
            return True

        while loop.time() < deadline:
            try:
                token = await self.check_pin(pin_id)
            except IntegrationError as exc:
                if exc.status == 429:
                    # Honor the server's own pacing when it provides one, capped. The
                    # fixed backoff below is only the fallback for a bare 429 with no
                    # pacing given.
                    backoff = min(
                        exc.retry_after or PIN_RATE_LIMIT_BACKOFF, PIN_RATE_LIMIT_MAX_BACKOFF
                    )
                    if not await _wait(backoff):
                        break
                    continue
                raise
            if token:
                return token
            if not await _wait(PIN_POLL_INTERVAL):
                break
        return None

    async def account(self, user_token: str) -> PlexAccount:
        """Who this token belongs to. This proves identity only, not permission.

        Routed through ``get_json`` so a plex.tv outage or a non-JSON (maintenance)
        body surfaces as ``IntegrationError`` rather than a raw ``httpx2`` error or
        ``ValueError`` that would slip past ``owns_server``'s fail-closed guard.
        """
        data = await self.get_json("/api/v2/user", headers={"X-Plex-Token": user_token})
        if not isinstance(data, dict):
            raise IntegrationError(
                self.service, "error.integration.unexpected_shape", path="/api/v2/user"
            )
        return PlexAccount(
            account_id=int(data["id"]),
            uuid=str(data.get("uuid") or ""),
            username=str(data.get("username") or data.get("title") or "plex-user"),
            email=data.get("email"),
            thumb=data.get("thumb"),
        )

    async def resources(self, user_token: str) -> list[PlexResource]:
        """Every device this token can see, with ``owned`` reported relative to that
        token.

        Uses the v2 JSON endpoint. The v1 XML endpoint silently omits some owned
        servers, which would make a server picker lie by leaving some out.

        Routed through ``get_json`` so a transport error or a non-JSON body becomes
        an ``IntegrationError``. That is what lets ``owns_server`` fail closed on a
        plex.tv outage. A raw ``httpx2.ConnectTimeout``, or a ``ValueError`` from a
        maintenance HTML page, would slip past its ``except IntegrationError`` and
        turn the authorization check into an uncaught 500, an open door disguised as
        a crash.
        """
        payload = await self.get_json(
            "/api/v2/resources",
            params={"includeHttps": 1, "includeRelay": 1},
            headers={"X-Plex-Token": user_token},
        )
        if not isinstance(payload, list):
            raise IntegrationError(
                self.service, "error.integration.unexpected_shape", path="/api/v2/resources"
            )
        return [_parse_resource(r) for r in payload]

    async def owned_servers(self, user_token: str) -> list[PlexResource]:
        return [r for r in await self.resources(user_token) if r.owned and r.is_server]

    async def owns_server(self, user_token: str, machine_identifier: str) -> bool:
        """The authorization check. Does this user own this server?

        Fails closed. An empty machine identifier matches nothing, so an
        unconfigured Reaper lets nobody in rather than letting everybody in.
        """
        if not machine_identifier:
            log.warning("plex.owner_check_no_machine_id")
            return False

        try:
            servers = await self.owned_servers(user_token)
        except IntegrationError as exc:
            # A plex.tv outage must not become an open door.
            log.warning("plex.owner_check_failed", error=str(exc))
            return False

        return any(s.client_identifier == machine_identifier for s in servers)


class _ProbeClient(BaseClient):
    """A one-shot ``/identity`` probe against a single advertised Plex address.

    Its own client, because it talks to a media server rather than plex.tv, at a URL
    that is not known until the resource list comes back. This lets the probe use the
    same shared machinery as every other client: the guarded transport, the retry on
    a transient blip, the same-origin redirect policy, and the mapped errors.
    """

    service: ClassVar[str] = "plex"

    async def identity(self) -> httpx2.Response | None:
        """The ``/identity`` response, or ``None`` when the address does not answer.

        ``_send`` maps both a 4xx/5xx response and a transport failure to
        ``IntegrationError``, so every "it did not answer usefully" case arrives here
        as one thing and reads as ``None``. Both callers below treat that the same way.
        """
        try:
            return await self._send("GET", "/identity")
        except IntegrationError:
            return None


async def _ask_identity(
    connection: PlexConnection, token: str, *, timeout: float, verify: bool
) -> httpx2.Response | None:
    """GET ``/identity`` on this address, or ``None`` if the address does not answer.

    ``/identity`` is a good probe: it needs little or no authentication, it is cheap,
    and it names the server that answered. ``verify`` defaults on at both callers.
    Turning it off is the operator's explicit choice for a self-signed HTTPS server,
    the same per-service opt-out the *arr clients have, threaded through the link
    flow and stored on the server row.

    This uses ``_ProbeClient`` rather than a bare ``httpx2.AsyncClient``, so the
    probe gets the same guarded transport, retry on a transient blip, same-origin
    redirect policy, and mapped errors every other read in this package gets.

    ``timeout`` bounds the whole probe, including its retries, not each individual
    attempt. The caller walks a server's advertised addresses one at a time
    (``plex_link.reachable_connection``), so an address that silently drops every
    packet must cost that bound once, and no more. Otherwise, adding the retry layer
    would have quietly tripled how long linking takes when a server has one dead
    address to get past.
    """
    client = _ProbeClient(
        connection.uri,
        safety=RuntimeSafety(destructive_enabled=False),
        headers={"X-Plex-Token": token, "Accept": "application/json"},
        verify=verify,
        timeout=httpx2.Timeout(timeout),
    )
    try:
        async with client, asyncio.timeout(timeout):
            return await client.identity()
    except TimeoutError:
        return None


async def probe_connection(
    connection: PlexConnection, token: str, *, timeout: float = 5.0, verify: bool = True
) -> bool:
    """Is this connection actually reachable?

    Checks reachability only. It does not check which server answered. Its caller is
    the link flow (``services/plex_link.reachable_connection``), which walks the
    addresses plex.tv itself just advertised for one resource, so the answer is
    already scoped to the right server. Requiring an identity check here would make a
    Plex server that does not report one impossible to link at all, giving up a real
    capability for a check this path does not need.

    The caller that does need an identity check is ``api/plex.plex_set_connection``,
    where the address is typed by hand and could be any server on the network. It
    calls ``connection_identity`` and compares.
    """
    response = await _ask_identity(connection, token, timeout=timeout, verify=verify)
    return response is not None and response.status_code < 400


async def connection_identity(
    connection: PlexConnection, token: str, *, timeout: float = 5.0, verify: bool = True
) -> str | None:
    """Which server answers at this address, or ``None`` if none can be confirmed.

    ``None`` covers four cases a caller must treat the same way: unreachable, an
    error status, a body that will not parse, and a body that does not name a
    server. None of them is evidence that the expected server is there. A reachable
    server that will not say who it is is exactly what an identity check exists to
    catch, so this is deliberately stricter than ``probe_connection``.
    """
    response = await _ask_identity(connection, token, timeout=timeout, verify=verify)
    if response is None or response.status_code >= 400:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    container = body.get("MediaContainer") if isinstance(body, dict) else None
    identifier = container.get("machineIdentifier") if isinstance(container, dict) else None
    return identifier if isinstance(identifier, str) and identifier else None
