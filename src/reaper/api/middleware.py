# SPDX-License-Identifier: AGPL-3.0-or-later
"""The gate every request passes through.

This enforces three jobs here, rather than sprinkled across routers, so a new
endpoint is protected by existing at all, not by remembering to add a dependency.

* **Authentication.** Every ``/api`` route requires a valid session, except the
  handful that must work before signing in: the health probe and the
  ``/api/auth`` endpoints themselves. There is no per-route opt-in. A route is
  behind auth unless this file names it as open.

* **The API key lane.** A request presenting ``X-Api-Key`` is judged on the key
  alone (constant-time digest compare, per-address backoff on bad guesses) and
  never falls back to a cookie. A valid key acts as the operator except on the
  fenced routes, which always need the browser session: arming, executing,
  sign-in, and key management. See ``_handle_api_key``.

* **CSRF.** A logged-in admin's browser must not be tricked into issuing a
  state-changing request by a page on another origin. Every unsafe method
  (POST/PUT/PATCH/DELETE) must carry a custom header the frontend always sends. A
  cross-site attacker cannot set that header without a CORS preflight, which this
  server never grants. When the browser provides ``Sec-Fetch-Site``, it must not
  say ``cross-site``. This runs for the auth endpoints too, since forging a login
  is itself an attack.

These jobs only know how to read an ``http`` connection, so ``__call__`` refuses
every other kind instead of waving it through (``_refuse_scope``). ``lifespan`` is
the one exception. It is startup and shutdown plumbing, carries no session to
check, and the app never starts if it does not reach the router. Reaper declares
no ``websocket`` route today, and this is what keeps that true by construction. A
websocket added later is refused here until someone teaches this file to
authenticate a handshake, instead of being born with no session check and no
CSRF. It would need both, since the browser WebSocket API cannot send the
``X-Reaper-CSRF`` header, so a handshake would have to read the session cookie
and validate ``Origin`` itself.

This is implemented as raw ASGI, not ``BaseHTTPMiddleware``. The guard reads only
headers and cookies, then calls the app with the original ``receive``/``send``,
so it never buffers the request body or wraps the response. A
``BaseHTTPMiddleware`` wrapper can stall a streaming or long-lived response by
holding it until the handler returns. A pass-through cannot. Scan progress is
polled today over ``GET /api/scan/status``, not streamed. An SSE progress stream
would be a plain ``http`` GET, so it would inherit this guard's auth without
revisiting this file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import structlog
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from reaper.api.errors import refusal_body
from reaper.auth.proxy import client_ip
from reaper.auth.ratelimit import Throttle
from reaper.auth.sessions import resolve_session_from_cookies

log = structlog.get_logger(__name__)


def _refused(request: Request, path: str, status: int, gate: str) -> None:
    """Say which gate turned a request away, at DEBUG.

    Two gates answer an indistinguishable 403, a blocked CSRF check and an API key
    that is valid but fenced off this route. Two more answer an indistinguishable
    401, a wrong key and no session at all. Each of the four has a different fix,
    and nothing else records which one fired. uvicorn's access log does not
    propagate to the root logger, so it reaches neither the Logs tab nor the file
    the operator downloads, and the response body is gone the moment the caller
    drops it.

    Never log the presented key, any prefix of it, or a cookie value. This logs
    only the address and the path.
    """
    log.debug("auth.refused", gate=gate, status=status, path=path, client=client_ip(request))


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Reachable without a session. The health probe (liveness checks run
# unauthenticated) and everything under /api/auth (you cannot log in if logging in
# requires being logged in).
_OPEN_EXACT = frozenset({"/api/health"})
_OPEN_PREFIX = "/api/auth/"

#: Bad API keys back off per address, the same shape as the login throttle. A
#: guessed header must not be a free brute-force channel just because it skips the
#: login form.
api_key_throttle = Throttle(threshold=5, base_delay=2.0, max_delay=300.0, decay=900.0)

#: Reads an API key must never see. Every other read is open to the key, since it
#: is for scripts, so this stays a tiny denylist. But "open to scripts" is not the
#: same as "safe to hand out", and the things here carry more than they appear to.
#:
#: * The key itself, and the backup download, hand back a stored secret in the
#:   clear. The backup is the most sensitive artifact in the app: the whole
#:   database plus the master key that decrypts every credential.
#: * The logs are not a secret store, but they are a running transcript of the
#:   library, including titles, the people who watched them, and root folders.
#:   The download concatenates every rotating file, so one GET is the whole
#:   history. This denies it for the same reason as the backup.
#: * The fairness reads are one person's whole viewing breakdown.
#:
#: The fairness entry is what keeps the privacy claim in
#: ``api_key_scope_description`` true. That claim says a key can "read your
#: library", meaning the catalog, not everyone's viewing. It holds because these
#: routes sit behind the browser, not because the copy stopped claiming it. A key
#: is for scripts that scan, plan, and edit the policy. Who watched what is an
#: input to none of those, and the operator handing a key to a third-party
#: dashboard is not the person whose viewing it would disclose.
#:
#: ``PUT /api/logs/level`` is a write, so the allowlist below already refuses it.
#:
#: Matched as subtrees (``_denies_read``), never as exact paths, so a route added
#: under one of these is denied by being born there, rather than by someone
#: remembering to list it.
#:
#: This is narrower than the allowlist the writes get, and that difference matters
#: here. Inside these four subtrees, deny-by-default. Outside them, open by
#: default. A new top-level read is reachable by a key the moment it exists
#: (``_api_key_allowed("GET", "/api/watch-history")`` is True today, and would be
#: for any name). So the refusal the operator is told, "cannot see who watched
#: what", describes a category, while this list can only ever enumerate paths. It
#: is true because these are the only routes that pair a person with a play, and
#: it stays true only while that holds. A viewing read born outside
#: ``/api/fairness`` would make the sentence false with nothing failing, since the
#: generated copy is built from the phrases and the phrase would not move. A
#: viewing-adjacent route belongs on this list before it ships, not after someone
#: notices.
#:
#: Each entry pairs its paths with the phrase ``api_key_scope_description`` names
#: them by, for the reason given on the write list below.
_API_KEY_READS_DENIED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("the key itself", ("/api/settings/general/api-key",)),
    ("the backup download", ("/api/settings/backup/download",)),
    ("the logs", ("/api/logs", "/api/logs/download")),
    ("who watched what", ("/api/fairness", "/api/fairness/people/{identity}")),
)
_API_KEY_READ_DENY = frozenset(path for _, paths in _API_KEY_READS_DENIED for path in paths)


def _denies_read(path: str) -> bool:
    """Does this read path fall under a denied entry, its subtree included?

    The subtree test also lets one entry cover both a templated path and the
    concrete path it serves. ``/api/fairness`` covers
    ``/api/fairness/people/{identity}``, as the schema spells it, and
    ``/api/fairness/people/<someone>``, as a request does, so ``api_key_refused``
    reads the same on either. That is what the reference annotates from.
    """
    return any(path == denied or path.startswith(f"{denied}/") for denied in _API_KEY_READ_DENY)


#: The only writes an API key may drive: scanning, planning, and editing the
#: policy and reap profile. Everything else that changes state stays behind the
#: signed-in browser. That includes not just the three irreversible authorities
#: (arming deletion, executing a reap, and changing sign-in or the key itself),
#: but every setting and credential change. A config write can hand a stored
#: token to an operator-supplied address, or loosen the proxy trust the login
#: lockout keys on, so those must not ride a header-only credential.
#:
#: The paths and the words the operator reads come from one declaration, because
#: a separate list of exclusions drifted from what the fence actually enforces. A
#: list of exclusions falls behind every time this fence tightens, since it
#: describes the fence by what it leaves out. A list of permissions cannot fall
#: behind, since a route only becomes reachable by being added here, phrase and
#: all.
_API_KEY_WRITES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("start a scan", ("/api/scan/start",)),
    # The dry run is matched by shape rather than by name, so it has no path to
    # list here. It rides this phrase instead, and ``_api_key_allowed`` is where
    # it is admitted.
    ("plan a run and dry run it", ("/api/runs",)),
    ("edit the policy", ("/api/policy", "/api/policy/validate", "/api/policy/simulate")),
    ("change the run limits and grace", ("/api/profile",)),
)
_API_KEY_WRITE_ALLOW = frozenset(path for _, paths in _API_KEY_WRITES for path in paths)

#: Open to this guard, and refused by the handler anyway. ``/api/auth/me`` answers
#: "who is signed in", a question a header credential does not have. The path is
#: under ``_OPEN_PREFIX``, so the key lane never judges it, and then
#: ``api/auth.py``'s handler answers 401 because the cookie resolves to nobody.
#:
#: The fence is not what turns the key away here, but the key is turned away all
#: the same, and the caller cannot tell the two apart from the response. So this
#: is declared in the same shape as the lists above, paths beside the phrase the
#: auth box names them by, and read by ``api_key_refused``, which is the question
#: the reference has to answer.
_SIGNED_IN_ONLY_READS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("who you are signed in as", ("/api/auth/me",)),
)
_SIGNED_IN_ONLY = frozenset(path for _, paths in _SIGNED_IN_ONLY_READS for path in paths)


def _listed(phrases: tuple[str, ...]) -> str:
    """Join the phrases into one readable clause, like ``a, b, and c``.

    This stays English deliberately, even where the sentence around it is
    translated. The two refusal bodies these lists ride in are catalog strings
    (``error.auth.api_key_read_denied`` and ``api_key_write_denied``), so a
    translated setup renders a translated sentence with an English list inside
    it. That is intentional. The reader here is a script or a terminal holding a
    header credential, never the localized browser, and the same phrases feed the
    API reference's auth box (``api_key_scope_description`` below), which stays
    English whatever the operator picked. The comma-and joining is English
    grammar of its own besides, so localizing the phrases alone would not finish
    the job.
    """
    if len(phrases) < 3:
        return " and ".join(phrases)
    return f"{', '.join(phrases[:-1])}, and {phrases[-1]}"


def _read_exclusions() -> str:
    """Return every read a key does not get, denied either by this fence or by the
    handler itself.

    Combining both into one clause matters because the caller meets one wall, not
    two. If this listed only the fence's own exclusions, a script author reading
    "reads everything except" three things would still hit an unlisted 401 on the
    fourth.
    """
    denied = (*_API_KEY_READS_DENIED, *_SIGNED_IN_ONLY_READS)
    return _listed(tuple(phrase for phrase, _ in denied))


def _write_permissions() -> str:
    return _listed(tuple(phrase for phrase, _ in _API_KEY_WRITES))


def api_key_scope_description() -> str:
    """Describe what an API key can do, in the operator's words, built from the
    fence itself.

    The API reference renders this in its auth box
    (``main.openapi_with_api_key``), where it is the whole basis on which someone
    decides what to point a script at. This is generated rather than written, so
    it cannot outlive the fence it describes. Every phrase comes from
    ``_API_KEY_WRITES`` and ``_API_KEY_READS_DENIED``, the same declarations
    ``_api_key_allowed`` enforces.
    """
    return (
        "The instance API key from Settings, General. It reads everything except "
        f"{_read_exclusions()}. It writes only these: {_write_permissions()}. Every other "
        "write is refused, including changing a setting, turning deletion on, and running "
        "a reap."
    )


def api_key_refusal(method: str) -> tuple[str, dict[str, str]]:
    """Return what the key holder is told at the moment the fence turns them away,
    as a code and its params. A denied read hears which reads are denied. A
    refused write hears which writes are allowed.

    This is the third place the fence is described in the operator's words, and
    the only one they read while it is stopping them. It is generated from the
    same two declarations as the auth box above, instead of being written beside
    them, so the message can never drift from what the fence actually allows.
    """
    if method in _SAFE_METHODS:
        return "error.auth.api_key_read_denied", {"exclusions": _read_exclusions()}
    return "error.auth.api_key_write_denied", {"permissions": _write_permissions()}


def _api_key_allowed(method: str, path: str) -> bool:
    """Decide what one API key may reach, deny-by-default.

    Reads are open except the handful that reveal a stored secret. Writes are
    closed except the explicit automation allowlist. A new route is closed to the
    key until it is deliberately opened here, so adding one can never silently
    widen what a leaked key can do. A denylist cannot make that guarantee. Any
    write not explicitly named as forbidden would be reachable by default.
    """
    if method in _SAFE_METHODS:
        return not _denies_read(path)
    if path in _API_KEY_WRITE_ALLOW:
        return True
    # Planning a specific run. Its dry run, but never its execute.
    return path.startswith("/api/runs/") and path.endswith("/dry-run")


def _is_open(path: str) -> bool:
    return path in _OPEN_EXACT or path.startswith(_OPEN_PREFIX)


def api_key_refused(method: str, path: str) -> bool:
    """Will this route turn away a caller holding nothing but an API key?

    This is the one question a script author has per route, and it is the answer
    the reference's per-operation marking uses, so the document never offers a
    credential the request will not actually accept.

    Two ways to be turned away both count here, because the caller cannot tell
    them apart from the response. Either this fence refuses the request
    (``_api_key_allowed``, the interesting case), or the path is open to the guard
    and the handler refuses an anonymous caller on its own
    (``_SIGNED_IN_ONLY_READS``). Counting only the fence's own refusal would read
    as more rigorous, since the second case is route logic, not a fence, but the
    reference needs the answer to "does a key reach this", not "does this fence
    stop it". ``GET /api/auth/me`` is the case that shows the difference: it is
    open to this guard, yet a key still cannot use it, because the handler always
    answers 401 for it.

    Every other open path answers False. The guard asks for no credential there,
    so the key is neither accepted nor what stands between the caller and an
    answer. ``no_credential_needed`` is how the reference says that, instead of
    leaving the route to inherit a requirement it does not have.

    This takes the templated path (``/api/runs/{run_id}/dry-run``) as readily as
    a concrete one, which is what lets the schema be annotated. Every allowlist
    entry is a static path, and the dry run's prefix-and-suffix test reads the
    same on either spelling.
    """
    if path in _SIGNED_IN_ONLY:
        return True
    return not _is_open(path) and not _api_key_allowed(method, path)


def no_credential_needed(path: str) -> bool:
    """Does this route ask an anonymous caller for nothing at all?

    True for the health probe and the sign-in endpoints, which have to work
    before anyone is signed in, minus the ones that refuse anonymously anyway.
    ``_SIGNED_IN_ONLY_READS`` is subtracted, or this would contradict
    ``api_key_refused`` on the same path.

    A CSRF header is still required on the unsafe ones. That is not a
    credential, and this does not claim otherwise. ``main``'s ``Session`` scheme
    description is where the header is named.
    """
    return _is_open(path) and path not in _SIGNED_IN_ONLY


async def _refuse_scope(scope: Scope, receive: Receive, send: Send) -> None:
    """Turn away a connection this guard cannot authenticate.

    A websocket is refused the way ASGI expects and Starlette's own router does
    it. This closes it before accepting, which the server turns into an HTTP
    403. ``1008`` is the protocol's "policy violation". Any other scope type
    gets nothing at all. Returning without calling the app is itself the
    refusal, and inventing a reply for a protocol this server does not speak
    would be guessing.
    """
    if scope["type"] == "websocket":
        await WebSocketClose(code=1008, reason="Reaper serves no websocket routes.")(
            scope, receive, send
        )


#: The header a write must carry, and the one value this guard accepts.
#:
#: This is declared rather than spelled inline, because four other places state
#: it, and a value is easier to get wrong than a name. Both copies in
#: ``main.py``, the reference page's ``onBeforeRequest`` hook and the
#: ``Session`` scheme sentence, are generated from these two constants, so they
#: cannot drift. The fifth is across a language boundary and cannot be generated
#: the same way: ``frontend/src/api.ts``'s ``CSRF_HEADER``, pinned by
#: ``frontend/src/api.test.ts``. That is the one a change here has to be
#: carried to by hand, which is why the test guarding the page names it in its
#: failure message.
CSRF_HEADER = "X-Reaper-CSRF"
CSRF_VALUE = "1"


def _csrf_ok(request: Request) -> bool:
    # The load-bearing check. This is a header no cross-origin form or simple
    # request can set. The frontend sends it on every request (see
    # frontend/src/api.ts).
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        return False
    # A second, proxy-safe check. Sec-Fetch-Site is set by the browser from the
    # actual context and survives a dev proxy, unlike an Origin/Host comparison,
    # which changeOrigin rewriting breaks. It is absent on older browsers, where
    # the header check alone still holds.
    site = request.headers.get("sec-fetch-site")
    return site is None or site in ("same-origin", "same-site", "none")


async def _reject(send: Send, status: int, code: str, params: dict[str, Any] | None = None) -> None:
    body = json.dumps(refusal_body(status, code, params)).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class AuthGuard:
    """ASGI middleware enforcing authentication and CSRF on the API surface."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def _traced(self, scope: Scope, receive: Receive, send: Send, path: str) -> None:
        """Run the request, then say what it was and how it ended, at DEBUG.

        This is Reaper's own request line, since uvicorn's does not reach the
        operator. Its ``uvicorn.access`` logger does not propagate to the root
        logger the ring handler sits on, so a downloaded log carries no HTTP
        lines at all. Emitting this line also keeps it on the level switch, where
        turning Debug on for a reproduction is the point. An access line per
        request at INFO would bury the decision lines the Logs tab exists for.

        This logs only the path, never the query string. An operator's own
        routes carry no credentials today, but this is the one place a future
        one would leak.

        The Logs tab's own poll is not traced. It reads the same bounded ring
        this writes to, on a 2-second timer, so tracing it would spend the
        operator's history on the act of watching it. With Debug on and a reap
        running, the poll and the 1 Hz status reads together turn the ring over
        in well under an hour, during exactly the operation Debug was turned on
        for. Changing the level and downloading the log are operator actions,
        and those stay traced.
        """
        if scope["method"] == "GET" and path == "/api/logs":
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        status = 0

        async def watched(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, watched)
        finally:
            log.debug(
                "http.request",
                method=scope["method"],
                path=path,
                status=status or None,
                duration_ms=round((time.monotonic() - started) * 1000),
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type: str = scope["type"]
        if scope_type == "lifespan":
            # Startup or shutdown, not a request. There is no session to check,
            # and the app never starts if this does not reach the router.
            await self.app(scope, receive, send)
            return
        if scope_type != "http":
            # Everything below reads headers, cookies, and a method from an http
            # request. Refuse anything else instead of handing it to the app
            # unauthenticated.
            await _refuse_scope(scope, receive, send)
            return

        path: str = scope["path"]
        # Everything that is not the API, the built SPA, its assets, the login
        # page itself, is served without a session. Otherwise the operator could
        # never reach the screen that logs them in.
        if not path.startswith("/api"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)  # header/cookie/url reads only. The body is never touched.

        # The API-key lane, for scripts and other apps. This is explicit and
        # exclusive. A request that presents X-Api-Key is judged on the key
        # alone, never falling back to a cookie session. No CSRF check runs on
        # this lane. CSRF defends cookie credentials a browser attaches
        # automatically, and no cross-site page can set this header without a
        # preflight this server never grants.
        provided_key = request.headers.get("x-api-key")
        if provided_key is not None and not _is_open(path):
            await self._handle_api_key(scope, receive, send, request, provided_key, path)
            return

        if scope["method"] not in _SAFE_METHODS and not _csrf_ok(request):
            _refused(request, path, 403, "csrf")
            await _reject(send, 403, "error.auth.csrf_blocked")
            return

        if _is_open(path):
            await self._traced(scope, receive, send, path)
            return

        factory = request.app.state.session_factory
        async with factory() as session:
            user, _ = await resolve_session_from_cookies(session, request.cookies)
            await session.commit()  # persist the throttled last_seen bump / expiry prune

        if user is None:
            _refused(request, path, 401, "no_session")
            await _reject(send, 401, "error.auth.not_authenticated")
            return

        await self._traced(scope, receive, send, path)

    async def _handle_api_key(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        request: Request,
        provided: str,
        path: str,
    ) -> None:
        """Judge one request on its API key. Throttle bad guesses, fence the
        password-only routes, and otherwise let it through as the operator.

        Compared as SHA-256 digests in constant time. The digest is cached on
        ``app.state`` at startup and on every key change, so the hot path never
        touches the database or the encryption layer.
        """
        throttle_key = f"api-key:{client_ip(request)}"
        if api_key_throttle.retry_after(throttle_key) > 0:
            _refused(request, path, 429, "api_key_throttled")
            await _reject(send, 429, "error.auth.api_key_throttled")
            return

        digest: bytes | None = getattr(request.app.state, "api_key_digest", None)
        provided_digest = hashlib.sha256(provided.encode("utf-8")).digest()
        if digest is None or not hmac.compare_digest(provided_digest, digest):
            locked_for = api_key_throttle.record_failure(throttle_key)
            # The lockout crossing logs as a warning, matching its sibling on the
            # local-login path (the `auth.local_locked_out` event in
            # `api/auth.py`). Repeated bad keys against an internet-facing
            # install are something the operator should see without having
            # turned anything on. The individual failures stay at DEBUG.
            if locked_for > 0:
                log.warning(
                    "auth.api_key_locked_out",
                    client=client_ip(request),
                    retry_after=round(locked_for),
                )
            else:
                _refused(request, path, 401, "api_key_invalid")
            await _reject(send, 401, "error.auth.api_key_invalid")
            return
        api_key_throttle.record_success(throttle_key)

        if not _api_key_allowed(scope["method"], path):
            _refused(request, path, 403, "api_key_not_allowed_here")
            code, params = api_key_refusal(scope["method"])
            await _reject(send, 403, code, params)
            return

        await self._traced(scope, receive, send, path)
