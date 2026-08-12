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

import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar, NoReturn, Self

import httpx2
import structlog
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from reaper.config import RuntimeSafety

log = structlog.get_logger(__name__)


def trace_call(
    service: str,
    method: str,
    path: str,
    status: int | None,
    started: float,
    *,
    mutation: bool = False,
) -> None:
    """One DEBUG line per outbound call: which service, what was asked, what came back.

    Nothing else records that one of these calls happened. The HTTP libraries would, but
    they are pinned to WARNING on purpose (``logging._NOISY_LOGGERS``) because they log the
    URL verbatim and the structlog scrubber never sees a stdlib record, so this is the only
    trace there can be. ``client.retry`` says a blip happened; this says the call happened,
    how long it took, and how it ended -- which is what "the scan sat there for four minutes"
    needs.

    **This is the one place the line's shape is defined, and all three client surfaces emit
    through here so they cannot drift (rule 72).** `BaseClient._send` covers the *arr calls;
    `PlexClient`'s `GuardedSession.request` covers every Plex read and the `refresh_path` and
    `empty_trash` calls on the deletion path; `PublicClient._stream_once` covers the ratings
    dataset, the longest single outbound operation in the app. Each passes
    ``urlsplit(url).path`` and never the URL, since plexapi puts ``X-Plex-Token`` in the query
    string (rule 13). One outbound call stays out: `notify/discord.py`'s webhook POST
    (rule 33) carries its secret in the URL path itself, so tracing it by path would log the
    credential this line keeps out.

    **``path`` is the argument, never the post-redirect target and never
    ``response.request.url``**: a Location header carries its own query string, and Tautulli
    and MDBList both put their key in one (rule 13). ``params`` and ``headers`` are never
    logged for the same reason. The scrubber would catch the known key names, but not logging
    a credential is a stronger guarantee than redacting one.

    ``status=None`` is a call that never got an answer -- a timeout or an unreachable host --
    which is the shape a scan stuck on one service takes.

    The line is DEBUG-only, so it reaches the 2000-line ring only when the operator turns Debug
    on. A scan's GUID sweep pages hundreds of Plex calls in at once; that is accepted, because
    under Debug those are the lines a stuck scan is read by.

    **Emitting can never raise.** `GuardedSession.request` runs under ``asyncio.to_thread`` on
    the deletion path, where a raise from a trace would surface as a failed mutation, so the
    emit is swallowed: a trace must never break the call it describes.
    """
    with contextlib.suppress(Exception):
        log.debug(
            "client.call",
            service=service,
            method=method.upper(),
            path=path,
            status=status,
            duration_ms=round((time.monotonic() - started) * 1000),
            mutation=mutation,
        )


# Methods that cannot change remote state.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

DEFAULT_TIMEOUT = httpx2.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

#: Redirect statuses handled by hand -- see ``BaseClient._send`` and ``_mutate``.
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


def _origin(url: httpx2.URL) -> tuple[str, str, int | None]:
    """(scheme, host, port) with the scheme's default port filled in, so
    ``http://a.local`` and ``http://a.local:80`` compare as the same origin."""
    port = url.port
    if port is None:
        port = {"http": 80, "https": 443}.get(url.scheme)
    return (url.scheme, url.host or "", port)


def _log_retry(retry_state: RetryCallState) -> None:
    """Trace a transient transport retry at DEBUG.

    Without this, tenacity retries in silence: an exhausted three-attempt failure reads
    exactly like a first-try one, and "did this time out three times or die instantly?"
    has no answer in the log. Fires between attempts, so the two intermediate retries of a
    failing read become visible only when someone turns Debug on. Logs the service and the
    attempt number only -- never the path or params, which carry credentials (Tautulli and
    MDBList put their api key in the query string).
    """
    service = getattr(retry_state.args[0], "service", "http") if retry_state.args else "http"
    sleep = getattr(retry_state.next_action, "sleep", None)
    exc = retry_state.outcome.exception() if retry_state.outcome is not None else None
    log.debug(
        "client.retry",
        service=service,
        attempt=retry_state.attempt_number,
        wait=round(sleep, 2) if sleep is not None else None,
        error=type(exc).__name__ if exc is not None else None,
    )


def transient_retry[**P, T](fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """The one retry policy for a transient transport failure: three attempts, backing off.

    Named and shared rather than repeated, so every read this codebase issues -- a JSON call
    through :meth:`BaseClient._request`, a streamed dataset download through
    :meth:`PublicClient.stream_to` -- survives a blip on the same terms. A second copy would
    be a second set of numbers to keep in step.

    **The decorated body must let raw ``httpx2`` transport errors escape.** The predicate
    matches those, so a body that maps them to ``IntegrationError`` first never matches, and
    the backoff below it becomes dead code -- which is exactly why the mapping lives one
    layer out in every caller here.
    """
    return retry(
        retry=retry_if_exception_type((httpx2.TransportError, httpx2.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        before_sleep=_log_retry,
        reraise=True,
    )(fn)


def _retry_after_seconds(response: httpx2.Response) -> float | None:
    """The Retry-After header as seconds, when the server sent a numeric one.

    The HTTP-date form is legal but rare on these APIs; it reads as None rather than
    guessed at, and callers fall back to their own pacing.
    """
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class SafetyViolationError(RuntimeError):
    """A mutating request was attempted while destructive actions are disabled.

    This is a bug, not a condition to handle: something tried to write when the
    application is in read-only mode.
    """


def refuse_mutation(event: str, method: str, path: str, *, reason: str, message: str) -> NoReturn:
    """Record a blocked write, then raise it. Both guards refuse through here.

    A refusal is the loudest thing either guard does and it was the quietest thing in the
    log: nothing was written at the point of refusal, so the only trace was whatever the
    caller made of the exception. The executor's ``_best_effort_refresh`` and
    ``_finalize_plex`` catch ``Exception`` deliberately, because a reap must not fail on a
    follow-up, and each logs the guard's own sentence under an event naming the wrong
    cause. What was missing there is a discriminator, since rules 92/93 forbid reading the
    sentence. ``sync_shelves._reconcile`` catches ``PlexError`` alone, so a shelf refusal
    escapes it untouched -- what keeps it escaping is now ONE arm, in ``plex.PlexClient._call``,
    where it was eight per-method copies when this line was written. C14 settled that collapse
    and it landed; this line is the insurance that survived it, since it covers every write
    either guard refuses rather than the eight that had thought put into them.

    Raising from here rather than at each site is what makes that structural. A refusal
    added later cannot arrive without its log line, and the ``reason`` is the discriminator
    rules 92/93 ask for: something a reader matches on, never a sentence that will be
    reworded. ``path`` is already token-free at both call sites (rule 13).
    """
    log.warning(event, method=method.upper(), path=path, reason=reason)
    raise SafetyViolationError(message)


class IntegrationError(RuntimeError):
    """An integration could not be reached, or returned an error."""

    def __init__(
        self,
        service: str,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        read_timed_out: bool = False,
    ) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service
        self.status = status
        self.retry_after = retry_after
        """Seconds the server asked us to wait (a numeric Retry-After), or None."""
        self.read_timed_out = read_timed_out
        """The service took the request and did not finish the body inside the read budget.

        That is a statement about how much was asked for, so a caller that asked for a large
        page can ask for a smaller one and get an answer. ``history_sync.sync`` is the caller
        that does. Set only by :func:`transport_failure`, and only for a ``ReadTimeout``: a
        connect or pool timeout says nothing about the size of the request, and shrinking it
        would not help. ``False`` therefore means "not known to be one", which leaves a caller
        raising rather than retrying. A typed flag rather than a match on the message, which
        is operator copy and will be reworded (rule 92)."""

    @property
    def is_auth_failure(self) -> bool:
        """401/403 mean the credential is wrong -- distinct from the service being down.

        The distinction matters: a wrong key should prompt re-authentication, while
        a 5xx or a timeout must not, or a transient outage would have the owner
        re-entering keys for no reason.
        """
        return self.status in (401, 403)


# Three transport and status failures, each worded once.
#
# Each sentence used to be written at the site that raised it: four in ``_send``, the same four
# in ``_mutate``, and three of them again in ``PublicClient``, so four sentences stood as eleven
# copies. Rule 144 is what that costs. The wording is what an operator reads when a service
# stops answering, and rewording one copy leaves the others saying something else about the
# same failure -- which is how ``public.py`` came to spell one of them with a hardcoded ``GET``
# where ``base.py`` names the method.
#
# ``too many redirects`` is one more failure this layer raises and is NOT fenced here. It is
# written once per file rather than twice, and the two spellings differ the same way: ``GET``
# hardcoded in ``public.py``, which only ever issues one, against ``{method}`` here.
# ``test_every_client_failure_sentence_is_worded_in_exactly_one_place`` fences the four below
# and names this one as out.
#
# ``refuse_mutation`` above is the same move for the guard's refusal, for the same reason.


def transport_failure(service: str, exc: httpx2.TransportError) -> IntegrationError:
    """The request never got an answer: it timed out, or the host was not reachable.

    Name the actual timeout kind. A ConnectTimeout (5s), WriteTimeout (10s) or PoolTimeout
    (5s) is not the read timeout, so a fixed "30s" sends an operator to the wrong place. For a
    mutation, "could not connect" and "the server was slow to answer" call for different next
    steps (rule 10).

    ``TimeoutException`` is itself a ``TransportError``, so callers catch the one type and the
    split happens here, in the order the two ``except`` arms used to sit in.

    A ``ReadTimeout`` also carries ``read_timed_out``, so a caller can tell the one timeout
    kind that a smaller request would fix from the ones it would not.
    """
    if isinstance(exc, httpx2.TimeoutException):
        return IntegrationError(
            service,
            f"timed out ({type(exc).__name__})",
            read_timed_out=isinstance(exc, httpx2.ReadTimeout),
        )
    return IntegrationError(service, f"unreachable ({exc})")


def refused_redirect(
    service: str, response: httpx2.Response, method: str, path: str
) -> IntegrationError:
    """A redirect Reaper will not follow. The caller decides which ones qualify."""
    return IntegrationError(
        service,
        f"refused redirect (HTTP {response.status_code}) for {method} {path}",
        status=response.status_code,
    )


def http_failure(
    service: str, response: httpx2.Response, method: str, path: str
) -> IntegrationError:
    """The service answered with a 4xx or 5xx, carrying its own Retry-After if it sent one.

    Reading the header here is what makes it uniform: the streamed public download raised
    this sentence without it, so a Retry-After from a mirror reached two of the three raise
    sites.
    """
    return IntegrationError(
        service,
        f"HTTP {response.status_code} for {method} {path}",
        status=response.status_code,
        retry_after=_retry_after_seconds(response),
    )


def unexpected_body(service: str, response: httpx2.Response, path: str) -> IntegrationError:
    """A 200 whose body would not parse as JSON, named by the content type that came back.

    Naming the type is what makes it diagnosable: an auth proxy's HTML login page and a
    gateway's text error read the same as "it did not work" without it.

    Raised by ``get_json`` and by ``plextv._post``, which normalizes its own POST for the
    reason its docstring gives (rule 72).
    """
    return IntegrationError(
        service,
        f"expected JSON from {path}, got {response.headers.get('content-type')}",
    )


class GuardedTransport(httpx2.AsyncBaseTransport):
    """Refuses mutating requests unless deletion is enabled and intended.

    ``non_media_mutations`` is a narrow, explicit allow-list of paths that may be
    written to even in read-only mode, because they *cannot reach media*. The only
    member today is plex.tv's PIN creation: signing in is a POST, and requiring the
    owner to enable deletion before they may log in would be absurd.

    It is an allow-list of exact paths rather than a per-client opt-out, so the
    exemption is auditable in one place and a new client cannot quietly acquire a
    license to write.
    """

    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport,
        safety: RuntimeSafety,
        *,
        non_media_mutations: frozenset[str] = frozenset(),
    ) -> None:
        self._inner = inner
        self._safety = safety
        self._non_media_mutations = non_media_mutations

    async def aclose(self) -> None:
        """Close the transport this one wraps.

        ``AsyncBaseTransport.aclose`` is a no-op, and inheriting it made every
        ``BaseClient.aclose()`` a no-op too: the call reached ``AsyncClient.aclose()``, which
        closes exactly one thing -- its transport -- which was this guard. The real
        ``AsyncHTTPTransport`` and its connection pool were never told, so nothing but GC
        reclaimed the sockets, and the ownership discipline rule 34 exists for released
        nothing however carefully ``scan_runner`` entered each client. ``PlexClient.aclose``
        is the twin that already did this, and names the same hazard.
        """
        await self._inner.aclose()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.method.upper() not in SAFE_METHODS:
            path = request.url.path

            if path not in self._non_media_mutations:
                # An explicit, per-request opt-in. Set only by the action executor,
                # which writes the intent to the durable journal *before* the call.
                intended = request.extensions.get("reaper_mutation_approved") is True

                if not self._safety.destructive_allowed:
                    refuse_mutation(
                        "http.write_blocked",
                        request.method,
                        path,
                        reason="not_armed",
                        message=f"Blocked {request.method} {path}. {self._safety.why_blocked()}",
                    )
                if not intended:
                    refuse_mutation(
                        "http.write_blocked",
                        request.method,
                        path,
                        reason="not_declared",
                        message=(
                            f"Blocked {request.method} {path}: this mutation was not declared "
                            "to the action journal. Destructive calls must go through the "
                            "action executor so that they are recorded before they are sent."
                        ),
                    )

        return await self._inner.handle_async_request(request)


class BaseClient:
    """Shared HTTP behavior: auth headers, retries, error mapping, redaction."""

    service: ClassVar[str] = "http"

    def __init__(
        self,
        base_url: str,
        *,
        safety: RuntimeSafety,
        headers: Mapping[str, str] | None = None,
        verify: bool = True,
        timeout: httpx2.Timeout | None = None,
        non_media_mutations: frozenset[str] = frozenset(),
        allow_cross_origin_redirects: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._safety = safety
        # Only the credential-less public fetchers may set this (see clients.public):
        # with an API key on the default headers, a cross-origin redirect is exfiltration.
        self._allow_cross_origin_redirects = allow_cross_origin_redirects
        self._client = httpx2.AsyncClient(
            base_url=self.base_url,
            headers=dict(headers or {}),
            timeout=timeout or DEFAULT_TIMEOUT,
            transport=GuardedTransport(
                httpx2.AsyncHTTPTransport(verify=verify, retries=0),
                safety,
                non_media_mutations=non_media_mutations,
            ),
            # Never auto-follow: httpx2 would re-send the credential headers (X-Api-Key
            # and kin) wherever Location points. Redirect policy lives in _send (a few
            # same-origin hops for reads) and _mutate (refused outright).
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @transient_retry
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        """Issue one request and let raw httpx2 transport errors escape, so tenacity retries.

        The error mapping lives one layer out in :meth:`_send` and not here for the reason
        :func:`transient_retry` spells out: an earlier version mapped a transient timeout to
        ``IntegrationError`` inside the retried body, the predicate never matched, and the
        exponential backoff was dead code -- every momentary blip aborted the whole scan on
        the first attempt with zero retries.
        """
        return await self._client.request(method, path, params=params, json=json, headers=headers)

    def _trace(
        self, method: str, path: str, status: int | None, started: float, *, mutation: bool = False
    ) -> None:
        """One line per `BaseClient` call, emitted through :func:`trace_call`."""
        trace_call(self.service, method, path, status, started, mutation=mutation)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        """Issue a read -- retried on transient transport errors -- and map failures.

        The retries happen in :meth:`_request`; by the time a transport error surfaces here
        it has already survived every attempt, so it is final and becomes an
        ``IntegrationError``. A 4xx/5xx is a definite answer from the service rather than a
        transport failure, so it is never retried, only mapped.

        Redirects are followed HERE, never by httpx2 (``follow_redirects=False`` on the
        client): auto-following would re-send the credential headers wherever Location
        points. A read may follow a few SAME-ORIGIN hops (a reverse proxy adding a
        trailing slash, say); a cross-origin redirect is refused outright, because the
        API key must never leave the configured origin. A redirected mutation is refused
        in :meth:`_mutate`.

        ``headers`` are per-request extras (e.g. plex.tv's ``X-Plex-Token``, which differs
        per call and so cannot live on the client's default headers).
        """
        started = time.monotonic()
        status: int | None = None
        try:
            target = path
            send_params = params
            for _ in range(4):  # the request itself, plus at most three same-origin redirects
                try:
                    response = await self._request(
                        method, target, params=send_params, json=json, headers=headers
                    )
                except httpx2.TransportError as exc:
                    raise transport_failure(self.service, exc) from exc

                status = response.status_code
                if response.status_code not in _REDIRECTS:
                    break
                location = response.headers.get("location")
                if method.upper() not in ("GET", "HEAD") or not location:
                    raise refused_redirect(self.service, response, method, path)
                next_url = response.request.url.join(location)
                if not self._allow_cross_origin_redirects and _origin(next_url) != _origin(
                    httpx2.URL(self.base_url)
                ):
                    raise IntegrationError(
                        self.service,
                        f"refused cross-origin redirect for {method} {path}: the credential "
                        f"headers must never leave {httpx2.URL(self.base_url).host!r}",
                        status=response.status_code,
                    )
                target = str(next_url)
                send_params = None  # the Location URL already carries its query string
            else:
                raise IntegrationError(self.service, f"too many redirects for {method} {path}")

            if response.status_code >= 400:
                raise http_failure(self.service, response, method, path)
            return response
        finally:
            self._trace(method, path, status, started)

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
            raise unexpected_body(self.service, response, path) from exc

    async def get_list(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> list[Any]:
        """A GET whose body must be a JSON array. A body of any other shape raises.

        A 200 carrying something else -- a reverse proxy's HTML error page, a schema
        change -- is never "there are none of these". Coerced to ``[]``, an auth proxy's
        JSON error page once read as an empty library: every movie on that Radarr
        silently left the scan, the snapshot stayed executable, and the operator was told
        a complete run over a partial library (rules 28 and 93).

        There is deliberately no ``default=`` or ``coerce=`` parameter. That parameter is
        the defect, and a helper that can be asked not to raise reopens it at every call
        site at once. A genuinely empty array is still empty and still answers the
        question.
        """
        data = await self.get_json(path, params=params, headers=headers)
        if not isinstance(data, list):
            raise IntegrationError(self.service, f"{path} did not return a list")
        return list(data)

    async def get_dict(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """A GET whose body must be a JSON object. See :meth:`get_list` for why."""
        data = await self.get_json(path, params=params, headers=headers)
        if not isinstance(data, dict):
            raise IntegrationError(self.service, f"{path} did not return an object")
        return data

    async def _mutate(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> httpx2.Response:
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
        started = time.monotonic()
        status: int | None = None
        try:
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    extensions={"reaper_mutation_approved": True},
                )
            except httpx2.TransportError as exc:
                raise transport_failure(self.service, exc) from exc

            status = response.status_code
            if response.status_code in _REDIRECTS:
                # EVERY redirect is refused here, never replayed: auto-following would
                # re-issue the approved call -- credential headers, mutation approval and
                # all -- at whatever URL the (possibly compromised) upstream chose. `_send`
                # refuses a narrower set, since a read may follow a same-origin hop.
                raise refused_redirect(self.service, response, method, path)
            if response.status_code >= 400:
                raise http_failure(self.service, response, method, path)
            return response
        finally:
            self._trace(method, path, status, started, mutation=True)
