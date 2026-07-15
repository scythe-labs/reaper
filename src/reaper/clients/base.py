# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP foundations for every integration.

``GuardedTransport`` is the most important class in Reaper.

Every outbound request passes through it, and it refuses anything that could
mutate a remote system unless deletion has been explicitly enabled *and* the
caller has declared the intent. Dry-run is therefore a property of the transport,
not a convention maintained by scattered ``if dry_run:`` checks at call sites --
which is precisely the bug Deleterr shipped (#291: "dry-run mutates state"). A
missed check there is a deleted file.

Two things it deliberately does *not* rely on:

* **HTTP method alone.** Tautulli's API is ``GET /api/v2?cmd=...``, and its key is
  full admin -- ``cmd=delete_library`` and ``cmd=restart`` are both GETs. Method
  filtering would wave those straight through, so the Tautulli client also
  enforces a command allow-list (see ``clients/tautulli.py``).
* **Trusting callers.** The guard is enforced at the transport, the lowest layer
  we control, so a new code path cannot forget it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Self

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from reaper.config import RuntimeSafety

log = structlog.get_logger(__name__)

# Methods that cannot change remote state.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class SafetyViolationError(RuntimeError):
    """A mutating request was attempted while destructive actions are disabled.

    This is a bug, not a condition to handle: something tried to write when the
    application is in read-only mode.
    """


class IntegrationError(RuntimeError):
    """An integration could not be reached, or returned an error."""

    def __init__(self, service: str, message: str, *, status: int | None = None) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service
        self.status = status

    @property
    def is_auth_failure(self) -> bool:
        """401/403 mean the credential is wrong -- distinct from the service being down.

        The distinction matters: a wrong key should prompt re-authentication, while
        a 5xx or a timeout must not, or a transient outage would have the owner
        re-entering keys for no reason.
        """
        return self.status in (401, 403)


class GuardedTransport(httpx.AsyncBaseTransport):
    """Refuses mutating requests unless deletion is enabled and intended.

    ``non_media_mutations`` is a narrow, explicit allow-list of paths that may be
    written to even in read-only mode, because they *cannot reach media*. The only
    member today is plex.tv's PIN creation: signing in is a POST, and requiring the
    owner to enable deletion before they may log in would be absurd.

    It is an allow-list of exact paths rather than a per-client opt-out, so the
    exemption is auditable in one place and a new client cannot quietly acquire a
    licence to write.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        safety: RuntimeSafety,
        *,
        non_media_mutations: frozenset[str] = frozenset(),
    ) -> None:
        self._inner = inner
        self._safety = safety
        self._non_media_mutations = non_media_mutations

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method.upper() not in SAFE_METHODS:
            path = request.url.path

            if path not in self._non_media_mutations:
                # An explicit, per-request opt-in. Set only by the action executor,
                # which writes the intent to the durable journal *before* the call.
                intended = request.extensions.get("reaper_mutation_approved") is True

                if not self._safety.destructive_allowed:
                    raise SafetyViolationError(
                        f"Blocked {request.method} {path}. {self._safety.why_blocked()}"
                    )
                if not intended:
                    raise SafetyViolationError(
                        f"Blocked {request.method} {path}: this mutation was not declared "
                        "to the action journal. Destructive calls must go through the "
                        "action executor so that they are recorded before they are sent."
                    )

        return await self._inner.handle_async_request(request)


class BaseClient:
    """Shared HTTP behaviour: auth headers, retries, error mapping, redaction."""

    service: ClassVar[str] = "http"

    def __init__(
        self,
        base_url: str,
        *,
        safety: RuntimeSafety,
        headers: Mapping[str, str] | None = None,
        verify: bool = True,
        timeout: httpx.Timeout | None = None,
        non_media_mutations: frozenset[str] = frozenset(),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._safety = safety
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=dict(headers or {}),
            timeout=timeout or DEFAULT_TIMEOUT,
            transport=GuardedTransport(
                httpx.AsyncHTTPTransport(verify=verify, retries=0),
                safety,
                non_media_mutations=non_media_mutations,
            ),
            follow_redirects=True,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Issue one request and let raw httpx transport errors escape, so tenacity retries.

        The retry predicate above matches ``httpx.TransportError``/``TimeoutException``, so
        those must reach the decorator *unmapped*. That is exactly why the error mapping
        lives one layer out in :meth:`_send` and not here: an earlier version mapped a
        transient timeout to ``IntegrationError`` inside the retried body, the predicate
        never matched, and the exponential backoff was dead code -- every momentary blip
        aborted the whole scan on the first attempt with zero retries.
        """
        return await self._client.request(method, path, params=params, json=json, headers=headers)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Issue a read -- retried on transient transport errors -- and map failures.

        The retries happen in :meth:`_request`; by the time a transport error surfaces here
        it has already survived every attempt, so it is final and becomes an
        ``IntegrationError``. A 4xx/5xx is a definite answer from the service rather than a
        transport failure, so it is never retried, only mapped.

        ``headers`` are per-request extras (e.g. plex.tv's ``X-Plex-Token``, which differs
        per call and so cannot live on the client's default headers).
        """
        try:
            response = await self._request(method, path, params=params, json=json, headers=headers)
        except httpx.TimeoutException as exc:
            # Name the actual timeout kind: a ConnectTimeout (5s), WriteTimeout (10s) or
            # PoolTimeout (5s) is not the read timeout, and reporting a fixed "30s" would
            # misdirect an operator diagnosing a connectivity problem.
            raise IntegrationError(self.service, f"timed out ({type(exc).__name__})") from exc
        except httpx.TransportError as exc:
            raise IntegrationError(self.service, f"unreachable ({exc})") from exc

        if response.status_code >= 400:
            raise IntegrationError(
                self.service,
                f"HTTP {response.status_code} for {method} {path}",
                status=response.status_code,
            )
        return response

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        response = await self._send("GET", path, params=params, headers=headers)
        try:
            return response.json()
        except ValueError as exc:
            raise IntegrationError(
                self.service,
                f"expected JSON from {path}, got {response.headers.get('content-type')}",
            ) from exc

    async def _mutate(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        """Issue ONE mutating request, declared to the transport guard.

        Two deliberate differences from :meth:`_send`, both because this changes remote
        state and ``_send`` does not:

        * **It is not retried.** ``_send`` retries transient transport errors; a retried
          DELETE can double-apply (delete a re-created item, or fail a second exclusion
          add). Here a timeout is surfaced once and the executor's *verification* step --
          re-reading the world -- is the source of truth about whether it landed, never
          the HTTP response.
        * **It declares intent.** The ``reaper_mutation_approved`` extension is the token
          :class:`GuardedTransport` requires. The guard still independently checks that
          deletion is enabled on the host, so setting this flag cannot by itself delete
          anything -- it only marks a call the executor has already journalled.

        Callers are the typed mutation methods on the *arr clients; nothing else sets the
        extension, so a stray write from some other path is refused, not waved through.
        """
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json,
                extensions={"reaper_mutation_approved": True},
            )
        except httpx.TimeoutException as exc:
            # Report the actual timeout kind (connect/write/pool/read), not a fixed "30s":
            # for a mutation especially, "could not connect" and "the server was slow to
            # answer" call for different operator responses.
            raise IntegrationError(self.service, f"timed out ({type(exc).__name__})") from exc
        except httpx.TransportError as exc:
            raise IntegrationError(self.service, f"unreachable ({exc})") from exc

        if response.status_code >= 400:
            raise IntegrationError(
                self.service,
                f"HTTP {response.status_code} for {method} {path}",
                status=response.status_code,
            )
        return response
