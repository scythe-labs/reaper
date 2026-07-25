# SPDX-License-Identifier: AGPL-3.0-or-later
"""plex.tv -- sign-in, server discovery, and the ownership check.

Two jobs, and they are distinct.

**Setup.** Rather than making the owner hunt for an ``X-Plex-Token`` via the "View
XML" trick, they sign in with Plex and pick their server from a list. We keep the
server's ``clientIdentifier`` (which *is* the ``machineIdentifier``), a reachable
connection URI, and the resource's ``accessToken``.

**Login.** The bug that matters: *"Sign in with Plex" authenticates but does not
authorize.* plex.tv will happily issue a valid token to **any Plex account on the
internet**, and ``/api/v2/user`` will happily describe them. A naive
implementation therefore lets a stranger into a tool that can delete a media
library. For calibration: Maintainerr ships with no auth at all, and Seerr trusts
whoever logs in first.

So after the PIN flow we ask plex.tv, **using that user's own token**, which
servers they own -- and require *this* machine id to be among them. ``owned`` is
relative to the token making the request, which is exactly what makes it a real
check, and exactly why it must never be our stored admin token (that would always
answer "yes").

Three details that are easy to get wrong:

* ``/api/v2/resources`` **requires** ``X-Plex-Client-Identifier`` and returns XML
  unless asked for JSON. The older ``/api/resources`` (v1, XML) *silently omits
  some owned servers*, which is fatal for a server picker.
* ``X-Plex-Client-Identifier`` must be byte-identical across the PIN creation, the
  auth URL, and the poll. If it differs, ``authToken`` stays null forever and it
  looks exactly as though the user never approved.
* We stay on the **legacy PIN flow** (``plex.tv/api/v2/pins``). Authenticating via
  the newer JWT flow makes ``/api/v2/resources`` hand back JWTs per resource --
  and Plex Media Server rejects JWTs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx2
import structlog

from reaper.clients.base import BaseClient, IntegrationError
from reaper.config import RuntimeSafety

log = structlog.get_logger(__name__)

PLEX_TV = "https://plex.tv"
AUTH_URL = "https://app.plex.tv/auth"

# A human is signing in on another device: they need seconds to a minute, and plex.tv
# rate-limits this endpoint. Polling once a second earns a 429 (observed) long before
# anyone has finished typing a password. plex.tv's own clients poll at a similar pace.
PIN_POLL_INTERVAL = 2.0
PIN_TIMEOUT = 300.0

# A 429 mid-poll is not a failure of the sign-in -- it means *we polled too eagerly*, and
# the fix is to wait longer, not to abandon the flow and make the owner start over.
PIN_RATE_LIMIT_BACKOFF = 5.0

# The most we will honor from a server-supplied ``Retry-After``. plex.tv naming a few
# seconds is back-pressure worth honoring; anything naming minutes or hours -- a rate
# limiter in front of plex.tv, a proxy interposed on the LAN -- would park the sign-in on
# a sleep far past the deadline it was given, with the terminal showing "Waiting..." and
# nothing to do but Ctrl-C (S2-2). An external server does not get to set an unbounded
# sleep; notify/discord.py caps the same header for the same reason.
PIN_RATE_LIMIT_MAX_BACKOFF = 30.0


def plex_headers(client_identifier: str, *, version: str) -> dict[str, str]:
    """The identifying headers Plex expects.

    ``X-Plex-Client-Identifier`` keys the device row on the user's Plex account, so
    it is generated once at first boot and never regenerated. (python-plexapi
    defaults it to ``hex(getnode())`` -- the machine's MAC address -- which is
    unstable in a container and leaks hardware detail. We do not use that default.)
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
    """This IS the PMS machineIdentifier. There is no separate field."""

    owned: bool
    provides: str
    access_token: str | None
    connections: list[PlexConnection]
    product_version: str | None = None
    presence: bool = False

    @property
    def is_server(self) -> bool:
        # Split on commas: 'provides' is a comma-separated list, and a substring
        # check would also match a hypothetical "pubsub-server".
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
            # cannot delete anything -- requiring the owner to enable deletion
            # before they may log in would be absurd. Narrowly allow-listed rather
            # than exempting the client, so the exemption stays auditable.
            non_media_mutations=frozenset({"/api/v2/pins"}),
        )
        self.client_identifier = client_identifier

    async def _post(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """plex.tv's PIN creation is a POST. See ``non_media_mutations`` above.

        Routed through ``_send`` so a transport error (a plex.tv outage) or a non-JSON body
        (a maintenance page served with HTTP 200) becomes an ``IntegrationError`` -- the same
        normalization ``get_json`` gives every read. Without it, ``owns_server``'s
        ``except IntegrationError`` guard could not fail closed on a raw ``httpx2`` error.
        """
        response = await self._send("POST", path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise IntegrationError(
                self.service,
                f"expected JSON from {path}, got {response.headers.get('content-type')}",
            ) from exc

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

        The backend polls, not the browser: a Plex auth token is a full-power
        credential for the user's entire account, and it should never pass through
        a web page. (Overseerr posts it from the browser to its own API.)

        A ``429`` from plex.tv is treated as back-pressure, not as an error: it means we
        polled too eagerly, and the correct response is to wait longer and keep going.
        Letting it propagate would abort a sign-in the owner has not even finished --
        which is exactly the failure that made the very first link attempt die.

        ``timeout`` is a real bound, not just a loop condition: every sleep is clipped to
        what is left of it, so the call returns at the deadline and reports the sign-in as
        not completed rather than sitting inside a sleep the server chose (S2-2).
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
                    # Honor the server's own pacing when it names one, capped: the fixed
                    # backoff is only the fallback for a bare 429.
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
        """Who this token belongs to. Authentication, not authorization.

        Routed through ``get_json`` so a plex.tv outage or a non-JSON (maintenance) body
        surfaces as ``IntegrationError`` rather than a raw ``httpx2``/``ValueError`` that
        would escape ``owns_server``'s fail-closed guard.
        """
        data = await self.get_json("/api/v2/user", headers={"X-Plex-Token": user_token})
        if not isinstance(data, dict):
            raise IntegrationError(self.service, "could not read the Plex account")
        return PlexAccount(
            account_id=int(data["id"]),
            uuid=str(data.get("uuid") or ""),
            username=str(data.get("username") or data.get("title") or "plex-user"),
            email=data.get("email"),
            thumb=data.get("thumb"),
        )

    async def resources(self, user_token: str) -> list[PlexResource]:
        """Every device this token can see, with ``owned`` relative to that token.

        Uses the v2 JSON endpoint. The v1 XML one silently omits some owned
        servers, which would make a server picker lie by omission.

        Routed through ``get_json`` so a transport error or a non-JSON body becomes an
        ``IntegrationError``. This is what makes ``owns_server`` able to fail closed on a
        plex.tv outage: a raw ``httpx2.ConnectTimeout`` (or a ``ValueError`` from a
        maintenance HTML page) would slip past its ``except IntegrationError`` and turn the
        authorization check into an uncaught 500 -- an open door dressed as a crash.
        """
        payload = await self.get_json(
            "/api/v2/resources",
            params={"includeHttps": 1, "includeRelay": 1},
            headers={"X-Plex-Token": user_token},
        )
        if not isinstance(payload, list):
            raise IntegrationError(self.service, "/resources did not return an array")
        return [_parse_resource(r) for r in payload]

    async def owned_servers(self, user_token: str) -> list[PlexResource]:
        return [r for r in await self.resources(user_token) if r.owned and r.is_server]

    async def owns_server(self, user_token: str, machine_identifier: str) -> bool:
        """**The authorization check.** Does this user own *this* server?

        Fails closed: an empty machine identifier matches nothing, so an
        unconfigured Reaper lets nobody in rather than everybody.
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
    """A one-shot reachability probe against a single advertised Plex address.

    Its own client because it talks to a media server rather than plex.tv, at a URL that
    is not known until the resource list comes back. It exists so the probe rides the
    shared machinery -- the guarded transport, the retry on a transient blip, the
    same-origin redirect policy, the mapped errors -- instead of the bare
    ``httpx2.AsyncClient`` it used to build inline (I-4).
    """

    service: ClassVar[str] = "plex"

    async def reachable(self) -> bool:
        """Whether ``/identity`` answers. Any mapped failure reads as "no"."""
        try:
            await self._send("GET", "/identity")
        except IntegrationError:
            return False
        return True


async def probe_connection(
    connection: PlexConnection, token: str, *, timeout: float = 5.0, verify: bool = True
) -> bool:
    """Is this connection actually reachable?

    ``/identity`` is the right probe: it is unauthenticated-ish, cheap, and returns
    the machineIdentifier, so it doubles as a check that we reached the server we
    think we did. ``verify`` defaults on; turning it off is the operator's explicit
    choice for a self-signed HTTPS server (the same per-service opt-out the *arr
    clients have), threaded through the link flow and stored on the server row.

    ``timeout`` bounds the WHOLE probe, retries included, not each attempt. The caller
    walks a server's advertised addresses one at a time (``plex_link
    .reachable_connection``), so an address that black-holes packets must cost that
    bound once and no more -- otherwise adding the retry layer would have quietly
    tripled how long linking takes on a server with a dead address to get past.
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
            return await client.reachable()
    except TimeoutError:
        return False
