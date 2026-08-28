# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP foundations for every integration.

``GuardedTransport`` is the most important class in Reaper.

Every outbound request passes through it, and it refuses anything that could mutate
a remote system unless deletion has been explicitly enabled and the caller has
declared the intent. Dry-run is therefore a property of the transport, not a
convention kept alive by scattered ``if dry_run:`` checks at each call site. A
missed check at one call site would delete a file for real.

This guard deliberately does not rely on two things:

* HTTP method alone. Tautulli's API is ``GET /api/v2?cmd=...``, and its key has
  full admin rights, so ``cmd=delete_library`` and ``cmd=restart`` are both GETs.
  Filtering on method alone would let those straight through, so the Tautulli
  client also enforces a command allow-list (see ``clients/tautulli.py``).
* Trusting callers. The guard is enforced at the transport, the lowest layer Reaper
  controls, so a new code path cannot forget to check it.
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
from reaper.engine.reason import Reason
from reaper.refusal import MESSAGES, english

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

    Nothing else records that one of these calls happened. The HTTP libraries would,
    but they are pinned to WARNING on purpose (``logging._NOISY_LOGGERS``), because
    they log the URL verbatim and the structlog scrubber never sees a stdlib log
    record. This is the only trace of an outbound call that can exist.
    ``client.retry`` says a blip happened. This says the call happened, how long it
    took, and how it ended, which is what answers "the scan sat there for four
    minutes."

    This is the one place the line's shape is defined, and all three client surfaces
    emit through here so they cannot drift apart. :meth:`BaseClient._send` covers the
    *arr calls. :class:`PlexClient`'s ``GuardedSession.request`` covers every Plex
    read and the ``refresh_path`` and ``empty_trash`` calls on the deletion path.
    :meth:`PublicClient._stream_once` covers the ratings dataset, the longest single
    outbound operation in the app. Each passes ``urlsplit(url).path`` and never the
    URL, because plexapi puts ``X-Plex-Token`` in the query string. One outbound call
    stays out: ``notify/discord.py``'s webhook POST carries its secret in the URL
    path itself, so tracing it by path would log the credential this line exists to
    keep out.

    ``path`` is the argument passed in, never the post-redirect target and never
    ``response.request.url``. A Location header carries its own query string, and
    Tautulli and MDBList both put their key in one. ``params`` and ``headers`` are
    never logged for the same reason. The scrubber would catch the known key names,
    but never logging a credential is a stronger guarantee than redacting one after
    the fact.

    ``status=None`` means the call never got an answer, such as a timeout or an
    unreachable host. That is the shape a scan stuck on one service takes.

    This line is DEBUG-only, so it reaches the 2000-line ring buffer only when the
    operator turns Debug on. A scan's GUID sweep pages hundreds of Plex calls in at
    once, and that is accepted, because those are exactly the lines that explain a
    stuck scan once Debug is on.

    Emitting this line can never raise. ``GuardedSession.request`` runs under
    ``asyncio.to_thread`` on the deletion path, where a raised exception from a
    trace would surface as a failed mutation. So the emit is wrapped to swallow any
    error: a trace must never break the call it describes.
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

#: Redirect statuses handled by hand. See ``BaseClient._send`` and ``_mutate``.
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

    Without this, tenacity retries in silence. An exhausted three-attempt failure
    would then read exactly like a first-try one, leaving no answer in the log to
    "did this time out three times or fail instantly?" This fires between attempts,
    so the two intermediate retries of a failing read become visible once Debug is
    on. Logs only the service and the attempt number, never the path or params,
    which can carry credentials (Tautulli and MDBList put their api key in the query
    string).
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
    """The one retry policy for a transient transport failure: three attempts,
    backing off between them.

    Named and shared instead of repeated, so every read in this codebase, whether a
    JSON call through :meth:`BaseClient._request` or a streamed dataset download
    through :meth:`PublicClient.stream_to`, survives a blip on the same terms. A
    second copy of this policy would be a second set of numbers to keep in step with
    this one.

    The decorated function must let raw ``httpx2`` transport errors escape. The
    retry predicate matches those errors specifically, so a function that maps them
    to ``IntegrationError`` before returning would never match, and the backoff
    around it would never fire. That is why the error mapping lives one layer out,
    in every caller here.
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

    The HTTP-date form is legal but rare on these APIs. It reads as ``None`` rather
    than being guessed at, and callers fall back to their own pacing.
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

    This is a bug, not a condition to handle. Something tried to write while the
    application is in read-only mode. It carries a catalog code and raw params the
    same way :class:`IntegrationError` does, in the same ``error.integration.*``
    namespace. The transport guard's own refusal counts as a failure at this layer
    just as a client's does, only for a write instead of a read. A param may itself
    be a :class:`Reason` (``why``, the nested ``error.safety.*`` cause from
    ``RuntimeSafety.why_blocked``), the same shape a stored explanation's own params
    can carry.
    """

    def __init__(self, code: str, /, **params: str | int | float | bool | Reason) -> None:
        self.code = code
        self.params: dict[str, str | int | float | bool | Reason] = params
        super().__init__(str(self))

    def __str__(self) -> str:
        # Through `english()`, not a bare `str.format`, so a nested `Reason` param
        # (`why`) composes into its own sentence instead of printing the raw
        # dataclass. This is the same approach `refusal.Refusal.__str__` uses.
        return english(self.as_reason())

    def as_reason(self) -> Reason:
        return Reason(self.code, dict(self.params))


def refuse_mutation(
    event: str,
    method: str,
    path: str,
    *,
    reason: str,
    code: str,
    **params: str | int | float | bool | Reason,
) -> NoReturn:
    """Log one blocked write, then raise it. Both guards refuse through here.

    A refusal is the most serious thing either guard does, but by itself it left
    almost nothing in the log: nothing was written at the point of refusal, so the
    only trace was whatever the caller did with the exception. The executor's
    ``_flush_refreshes`` and ``_finalize_plex`` catch ``Exception`` on purpose,
    because a reap must not fail on a follow-up step, and each then logs the guard's
    own message under an event that names the wrong cause. Reading that message for
    a cause would break the moment its wording changed, so ``reason`` exists to give
    a caller something stable to match on instead. ``sync_shelves._reconcile``
    catches only ``PlexError``, so a refusal, a different, sibling exception, passes
    through it untouched. One arm in ``plex.PlexClient._call`` is what keeps every
    write refusal passing through unchanged rather than being caught and relabeled.

    Raising from this one function, rather than at each call site, is what makes
    all of this reliable. A refusal added later cannot arrive without its log line.
    ``reason`` is a stable code a caller can match on, never a sentence that might
    get reworded later. ``path`` is already stripped of any credential at both call
    sites. ``code`` is the catalog id the caller's params render through (an
    ``error.integration.write_*`` entry), and the guard's message is composed the
    same way every other refusal's is, rather than being written by hand at each
    call site.
    """
    log.warning(event, method=method.upper(), path=path, reason=reason)
    raise SafetyViolationError(code, method=method.upper(), path=path, **params)


class IntegrationError(RuntimeError):
    """An integration could not be reached, or returned an error.

    ``code`` is a catalog id. It is either an ``error.integration.*`` entry in
    ``reaper.refusal.MESSAGES``, or an ``error.instance.*``/``error.plexclient.*``
    entry for a sibling raiser in this layer. ``params`` are the raw values its
    template fills in, such as ``service`` for the product name, or ``status``,
    ``path``, ``method`` and ``detail`` for a library's own message text carried
    verbatim rather than reworded. ``__str__`` renders through the same catalog
    :class:`~reaper.refusal.Refusal` does, so any existing ``except`` block that
    reads ``str(exc)`` for a log line keeps reading a full sentence.
    :meth:`as_reason` is what a caller nests inside its own refusal instead, so the
    code, not a frozen sentence, survives to reach the browser.
    """

    def __init__(
        self,
        service: str,
        code: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        read_timed_out: bool = False,
        **params: str | int | float | bool,
    ) -> None:
        self.service = service
        self.code = code
        self.params: dict[str, str | int | float | bool] = params
        self.status = status
        self.retry_after = retry_after
        """Seconds the server asked to wait for, from a numeric Retry-After header, or None."""
        self.read_timed_out = read_timed_out
        """The service accepted the request but did not finish sending the body
        within the read budget.

        This is a statement about how much was asked for, so a caller that asked for
        a large page can ask for a smaller one instead and get an answer.
        ``history_sync.sync`` is the caller that does this. Set only by
        :func:`transport_failure`, and only for a ``ReadTimeout``. A connect or pool
        timeout says nothing about the size of the request, so shrinking it would
        not help. ``False`` means "not known to be a read timeout", which leaves a
        caller raising rather than retrying. This is a typed flag rather than a
        match on the message text, because that text is operator copy and gets
        reworded.

        Never re-send a mutation because of this flag. :meth:`BaseClient._mutate`
        maps its transport errors through the same function, so a DELETE whose
        answer did not arrive carries this flag too. There, it means the request
        reached the service and the write may already have applied. Only the
        executor's verification step can settle that, never the response (see
        ``_mutate``). This flag is a fact about a read that can safely be retried
        smaller, not about a write."""
        super().__init__(str(self))

    def _render_params(self) -> dict[str, str | int | float | bool]:
        """``self.params`` plus ``status``, when it is set.

        ``status`` is its own constructor argument, not a ``**params`` entry,
        because every caller needs it typed for :attr:`is_auth_failure` and a
        route's own status-mapping logic. That means a template naming ``{status}``
        (``error.integration.http_failure`` and similar) would otherwise never see
        it. This is the one place that gap is closed, for both renderers below.
        """
        if self.status is None:
            return self.params
        return {**self.params, "status": self.status}

    def __str__(self) -> str:
        template = MESSAGES.get(self.code, self.code)
        try:
            return f"{self.service}: {template.format(**self._render_params())}"
        except (KeyError, IndexError):
            return f"{self.service}: {template}"

    def as_reason(self) -> Reason:
        """This error's code and params, as the typed container a nested ``{error}``
        param carries. This mirrors what :meth:`reaper.refusal.Refusal.as_reason`
        does, one layer further from the operator. ``service`` rides along as a
        param, so a template that wants the product name has it without a second
        lookup. No template uses ``{service}`` today, but a caller that wants one
        can add it."""
        return Reason(self.code, {"service": self.service, **self._render_params()})

    @property
    def is_auth_failure(self) -> bool:
        """401 or 403 means the credential is wrong, which is different from the
        service being down.

        The distinction matters. A wrong key should prompt re-authentication, while
        a 5xx or a timeout must not, or a transient outage would send the owner to
        re-enter keys for no reason.
        """
        return self.status in (401, 403)


def transport_failure(service: str, exc: httpx2.TransportError) -> IntegrationError:
    """The request never got an answer. It timed out, or the host was not reachable.

    ``TimeoutException`` is itself a ``TransportError``, so callers catch just the
    one type, and the split between the two happens here.

    A ``ReadTimeout`` also carries ``read_timed_out``, so a caller can tell the one
    kind of timeout that a smaller request would fix apart from the ones it would
    not, even though both kinds now share one operator-facing code. Which kind it
    was stays a fact on the exception, it just stops being two different sentences
    shown to the operator.
    """
    if isinstance(exc, httpx2.TimeoutException):
        return IntegrationError(
            service,
            "error.integration.timed_out",
            read_timed_out=isinstance(exc, httpx2.ReadTimeout),
        )
    return IntegrationError(service, "error.integration.unreachable", detail=str(exc))


def refused_redirect(
    service: str, response: httpx2.Response, method: str, path: str
) -> IntegrationError:
    """A redirect Reaper will not follow. The caller decides which ones qualify."""
    return IntegrationError(
        service,
        "error.integration.refused_redirect",
        status=response.status_code,
        method=method,
        path=path,
    )


def http_failure(
    service: str, response: httpx2.Response, method: str, path: str
) -> IntegrationError:
    """The service answered with a 4xx or 5xx, carrying its own Retry-After if it sent
    one.

    Reading the header here, in one place, is what keeps this consistent: every
    caller of this function gets a Retry-After when the service sent one, rather
    than only some of them.
    """
    return IntegrationError(
        service,
        "error.integration.http_failure",
        status=response.status_code,
        retry_after=_retry_after_seconds(response),
        method=method,
        path=path,
    )


def unexpected_body(service: str, response: httpx2.Response, path: str) -> IntegrationError:
    """A 200 response whose body will not parse as JSON.

    Raised by ``get_json`` and by ``plextv._post``, which normalizes its own POST
    for the reason given in its own docstring. This shares its code with
    ``get_list``/``get_dict``'s "parsed, but the wrong shape" refusal
    (``error.integration.unexpected_shape``), because both mean the same thing to
    the operator. The content type is recorded in the log line the caller already
    writes, not in the text shown to the operator.
    """
    return IntegrationError(service, "error.integration.unexpected_shape", path=path)


class GuardedTransport(httpx2.AsyncBaseTransport):
    """Refuses mutating requests unless deletion is enabled and the caller has
    declared its intent.

    ``non_media_mutations`` is a narrow, explicit allow-list of paths that may be
    written to even in read-only mode, because they cannot reach any media. The
    only member today is plex.tv's PIN creation: signing in is a POST, and
    requiring the owner to enable deletion before they can log in would make no
    sense.

    This is an allow-list of exact paths, not a per-client opt-out, so the
    exemption stays auditable in one place and a new client cannot quietly gain
    permission to write.
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

        ``AsyncBaseTransport.aclose`` is a no-op by default, so without this
        override, ``BaseClient.aclose()`` would also do nothing real: the call would
        reach ``AsyncClient.aclose()``, which closes only its transport, which is
        this guard, and stop there. The real ``AsyncHTTPTransport`` underneath, and
        its connection pool, would never be told to close, and only garbage
        collection would eventually reclaim the sockets. ``PlexClient.aclose`` is
        the twin method that already handles this same hazard for its own client.
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
                        code="error.integration.write_not_armed",
                        why=self._safety.why_blocked() or "",
                    )
                if not intended:
                    refuse_mutation(
                        "http.write_blocked",
                        request.method,
                        path,
                        reason="not_declared",
                        code="error.integration.write_not_declared",
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
            # Never auto-follow. httpx2 would re-send the credential headers
            # (X-Api-Key and similar) wherever Location points. Redirect policy
            # lives in _send, which allows a few same-origin hops for reads, and
            # _mutate, which refuses every redirect outright.
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
        read_timeout: float | None = None,
    ) -> httpx2.Response:
        """Issue one request and let raw httpx2 transport errors escape, so tenacity
        can retry them.

        The error mapping lives one layer out, in :meth:`_send`, for the reason
        :func:`transient_retry`'s docstring gives: mapping a transient error to
        ``IntegrationError`` inside this retried function would stop the retry
        predicate from ever matching, so the backoff below it would never fire.

        ``read_timeout`` widens the read budget for one call only. A client's
        timeout applies to every method on it, so a bulk read that legitimately
        takes a minute cannot borrow that margin from the client without giving the
        same minute to a call that is answering a browser. Only the read leg moves.
        Connect, write and pool say nothing about how much data was asked for.
        Passing ``timeout=None`` to httpx2 means no timeout at all, not the
        client's own timeout, so the untouched case sends the sentinel value
        instead.
        """
        budget: Any = httpx2.USE_CLIENT_DEFAULT
        if read_timeout is not None:
            shared = self._client.timeout
            budget = httpx2.Timeout(
                connect=shared.connect, read=read_timeout, write=shared.write, pool=shared.pool
            )
        return await self._client.request(
            method, path, params=params, json=json, headers=headers, timeout=budget
        )

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
        read_timeout: float | None = None,
    ) -> httpx2.Response:
        """Issue a read, retried on transient transport errors, and map any failure.

        The retries happen inside :meth:`_request`. By the time a transport error
        reaches here, it has already survived every attempt, so it is final and
        becomes an ``IntegrationError``. A 4xx or 5xx is a definite answer from the
        service rather than a transport failure, so it is never retried, only
        mapped.

        Redirects are followed here, never by httpx2 itself (the client sets
        ``follow_redirects=False``), because auto-following would re-send the
        credential headers wherever Location points. A read may follow a few
        same-origin hops, such as a reverse proxy adding a trailing slash. A
        cross-origin redirect is refused outright, because the API key must never
        leave the configured origin. A redirected mutation is refused in
        :meth:`_mutate`.

        ``headers`` are per-request extras, such as plex.tv's ``X-Plex-Token``,
        which differs per call and so cannot live on the client's default headers.
        ``read_timeout`` is a per-request read budget for one bulk read, described
        on :meth:`_request`.
        """
        started = time.monotonic()
        status: int | None = None
        try:
            target = path
            send_params = params
            for _ in range(4):  # the request itself, plus at most three same-origin redirects
                try:
                    response = await self._request(
                        method,
                        target,
                        params=send_params,
                        json=json,
                        headers=headers,
                        read_timeout=read_timeout,
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
                        "error.integration.cross_origin_redirect_refused",
                        status=response.status_code,
                        method=method,
                        path=path,
                    )
                target = str(next_url)
                send_params = None  # the Location URL already carries its query string
            else:
                raise IntegrationError(
                    self.service, "error.integration.too_many_redirects", method=method, path=path
                )

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
        read_timeout: float | None = None,
    ) -> Any:
        response = await self._send(
            "GET", path, params=params, headers=headers, read_timeout=read_timeout
        )
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

        A 200 response carrying something else, such as a reverse proxy's HTML
        error page or a changed schema, is never the same as "there are none of
        these". Coercing it to ``[]`` would read a broken response as an empty
        library, which would silently drop every item in it from the scan while
        still reporting the run as complete.

        There is deliberately no ``default=`` or ``coerce=`` parameter. That
        parameter would be the defect itself: a helper that can be told not to
        raise reopens this exact problem at every call site that uses it. A
        genuinely empty array is still empty, and still answers the question
        honestly.
        """
        data = await self.get_json(path, params=params, headers=headers)
        if not isinstance(data, list):
            raise IntegrationError(self.service, "error.integration.unexpected_shape", path=path)
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
            raise IntegrationError(self.service, "error.integration.unexpected_shape", path=path)
        return data

    async def _mutate(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> httpx2.Response:
        """Issue one mutating request, declared to the transport guard.

        Two things are deliberately different from :meth:`_send`, both because this
        changes remote state and ``_send`` does not:

        * It is not retried. ``_send`` retries transient transport errors, but a
          retried DELETE can double-apply, such as deleting a re-created item or
          failing a second exclusion add. Here a timeout surfaces once, and the
          executor's verification step, re-reading the world afterward, is the
          source of truth about whether the write landed, never the HTTP response.
        * It declares intent. The ``reaper_mutation_approved`` extension is the
          token :class:`GuardedTransport` requires before it will let a mutation
          through. The guard still independently checks that deletion is enabled on
          the host, so setting this flag alone cannot delete anything. It only marks
          a call the executor has already journalled.

        Callers are the typed mutation methods on the *arr clients. Nothing else
        sets the extension, so a stray write from some other path is refused, not
        waved through.
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
                # Every redirect is refused here, never replayed. Auto-following
                # would re-issue the approved call, credential headers, mutation
                # approval and all, at whatever URL the upstream server chose, even
                # a compromised one. `_send` refuses a narrower set, since a read may
                # follow a same-origin hop.
                raise refused_redirect(self.service, response, method, path)
            if response.status_code >= 400:
                raise http_failure(self.service, response, method, path)
            return response
        finally:
            self._trace(method, path, status, started, mutation=True)
