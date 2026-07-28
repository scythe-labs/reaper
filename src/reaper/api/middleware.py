# SPDX-License-Identifier: AGPL-3.0-or-later
"""The gate every request passes through.

Three jobs, all enforced here rather than sprinkled across routers, so that a new
endpoint is protected by *existing at all* rather than by remembering to add a
dependency:

* **Authentication.** Every ``/api`` route requires a valid session, except the
  handful that must work *before* you are logged in -- the health probe and the
  ``/api/auth`` endpoints themselves. There is no per-route opt-in; a route is
  behind auth unless this file names it as open.

* **The API key lane.** A request presenting ``X-Api-Key`` is judged on the key alone
  (constant-time digest compare, per-address backoff on bad guesses) and never falls
  back to a cookie. A valid key acts as the operator EXCEPT on the fenced routes --
  arming, executing, sign-in and key management -- which always need the browser
  session. See ``_handle_api_key``.

* **CSRF.** A logged-in admin's browser must not be tricked into issuing a
  state-changing request by a page on another origin. Every unsafe method
  (POST/PUT/PATCH/DELETE) must carry a custom header our own frontend always
  sends -- a cross-site attacker cannot set one without a CORS preflight, which
  this server never grants -- and, when the browser provides ``Sec-Fetch-Site``,
  it must not say ``cross-site``. This runs for the auth endpoints too: forging a
  login is itself an attack.

Both jobs only know how to read an ``http`` connection, so ``__call__`` refuses every
other kind rather than waving it through (``_refuse_scope``). ``lifespan`` is the one
exception: it is startup/shutdown plumbing, carries no session to check, and the app
never starts if it does not reach the router. Nothing else is served today -- Reaper
declares no ``websocket`` route -- and this is what keeps that true by construction: a
websocket added later is refused here until someone teaches this file to authenticate a
handshake, rather than being born with no session check and no CSRF. It would need
both: the browser WebSocket API cannot send our ``X-Reaper-CSRF`` header, so a handshake
has to read the session cookie and validate ``Origin`` itself.

Implemented as raw ASGI, not ``BaseHTTPMiddleware``: the guard reads only headers
and cookies and then calls the app with the original ``receive``/``send``, so it
never buffers the request body or wraps the response. A ``BaseHTTPMiddleware``
wrapper can stall a streaming or long-lived response by holding it until the
handler returns; a pass-through cannot. (Scan progress is *polled* today over
``GET /api/scan/status``, not streamed. An SSE progress stream would be a plain
``http`` GET, so it would inherit this guard's auth without revisiting this file.)
"""

from __future__ import annotations

import hashlib
import hmac
import json
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from reaper.auth.ratelimit import Throttle
from reaper.auth.sessions import resolve_session_from_cookies

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Reachable without a session. The health probe (liveness checks run
# unauthenticated) and everything under /api/auth (you cannot log in if logging in
# requires being logged in).
_OPEN_EXACT = frozenset({"/api/health"})
_OPEN_PREFIX = "/api/auth/"

#: Bad API keys back off per address, same shape as the login throttle: a guessed
#: header must not be a free brute-force channel just because it skips the login form.
api_key_throttle = Throttle(threshold=5, base_delay=2.0, max_delay=300.0, decay=900.0)

#: Reads an API key must never see. Every other read is open to the key (it is for
#: scripts), so this stays a tiny denylist -- but "open to scripts" is not the same as
#: "safe to hand out", and the two things here are the ones that carry more than they
#: appear to:
#:
#: * The key itself, and the backup download -- they hand back a stored secret in the
#:   clear. The backup is the crown jewels: the whole database plus the master key that
#:   decrypts every credential.
#: * The logs. They are not a secret store, but they are a running transcript of the
#:   library -- titles, the people who watched them, root folders -- and the download
#:   concatenates every rotating file, so one GET is the whole history. The operator is
#:   told a key can "read your library"; that has to mean the catalog, not everyone's
#:   viewing. Denied for the same reason the backup is (S-3).
#:
#: ``PUT /api/logs/level`` is a write, so the allowlist below already refuses it.
#:
#: Each entry pairs the paths with the phrase ``api_key_scope_description`` names them by,
#: for the reason given on the write list below.
_API_KEY_READS_DENIED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("the key itself", ("/api/settings/general/api-key",)),
    ("the backup download", ("/api/settings/backup/download",)),
    ("the logs", ("/api/logs", "/api/logs/download")),
)
_API_KEY_READ_DENY = frozenset(path for _, paths in _API_KEY_READS_DENIED for path in paths)

#: The only writes an API key may drive: scanning, planning, and editing the policy and
#: reap profile. Everything else that changes state stays behind the signed-in browser --
#: not just the three irreversible authorities (arming deletion, executing a reap, and
#: changing sign-in or the key itself), but ALL setting/credential changes. A config write
#: can hand a stored token to an operator-supplied address or loosen the proxy trust the
#: login lockout keys on, so those must not ride a header-only credential.
#:
#: The paths and the words the operator reads are ONE declaration, because they drifted:
#: the reference's auth box described the fence by what it excludes, named three
#: exclusions, and a key holder scripting against it met a 403 on a dozen more. A list of
#: exclusions falls behind every time this fence tightens; a list of permissions cannot,
#: since a route only becomes reachable by being added here, phrase and all (rule 103).
_API_KEY_WRITES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("start a scan", ("/api/scan/start",)),
    # The dry run is matched by shape rather than by name, so it has no path to list here;
    # it rides this phrase instead, and ``_api_key_allowed`` is where it is admitted.
    ("plan a run and dry run it", ("/api/runs",)),
    ("edit the policy", ("/api/policy", "/api/policy/validate", "/api/policy/simulate")),
    ("change the run limits and grace", ("/api/profile",)),
)
_API_KEY_WRITE_ALLOW = frozenset(path for _, paths in _API_KEY_WRITES for path in paths)

#: Open to this guard, and refused by the handler anyway. ``/api/auth/me`` answers "who is
#: signed in", a question a header credential does not have: the path is under
#: ``_OPEN_PREFIX``, so the key lane never judges it, and then ``api/auth.py``'s handler
#: answers 401 because the cookie resolves to nobody.
#:
#: The fence is not what turns the key away here, but the key IS turned away, and the
#: caller cannot tell the two apart from the response. So this is declared in the same
#: shape as the lists above -- paths beside the phrase the auth box names them by -- and
#: read by ``api_key_refused``, which is the question the reference has to answer.
_SIGNED_IN_ONLY_READS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("who you are signed in as", ("/api/auth/me",)),
)
_SIGNED_IN_ONLY = frozenset(path for _, paths in _SIGNED_IN_ONLY_READS for path in paths)


def _listed(phrases: tuple[str, ...]) -> str:
    """``a, b, and c`` -- the phrases as one readable clause."""
    if len(phrases) < 3:
        return " and ".join(phrases)
    return f"{', '.join(phrases[:-1])}, and {phrases[-1]}"


def _read_exclusions() -> str:
    """Every read a key does not get, denied by this fence or by the handler itself.

    Both belong in one clause because the caller meets one wall, not two: a script author
    told the key "reads everything except" three things still hits a 401 on the fourth.
    """
    denied = (*_API_KEY_READS_DENIED, *_SIGNED_IN_ONLY_READS)
    return _listed(tuple(phrase for phrase, _ in denied))


def _write_permissions() -> str:
    return _listed(tuple(phrase for phrase, _ in _API_KEY_WRITES))


def api_key_scope_description() -> str:
    """What an API key can do, in the operator's words, built from the fence itself.

    The API reference renders this in its auth box (``main.openapi_with_api_key``), where
    it is the whole basis on which someone decides what to point a script at. Generated
    rather than written so it cannot outlive the fence it describes: every phrase comes
    from ``_API_KEY_WRITES``/``_API_KEY_READS_DENIED``, the same declarations
    ``_api_key_allowed`` enforces.
    """
    return (
        "The instance API key from Settings, General. It reads everything except "
        f"{_read_exclusions()}. It writes only these: {_write_permissions()}. Every other "
        "write is refused, including changing a setting, turning deletion on, and running "
        "a reap."
    )


def api_key_refusal_detail(method: str) -> str:
    """What the key holder is told at the moment the fence turns them away.

    The third place this fence is described in the operator's words, and the only one they
    read while it is stopping them, so it is generated from the same two declarations as
    the auth box above rather than written beside them. It used to say "Changing any
    setting, arming deletion, and running a reap stay behind your password" while
    ``/api/profile`` sat in the write allowlist -- so the same response that refused one
    write told the caller the run caps were out of reach, in the request right before the
    one that turned them off (S-2). That is the drift ``api_key_scope_description``'s
    comment describes, in the copy nobody thought to generate.

    Says only the half that explains THIS refusal: a denied read hears which reads are
    denied, a refused write hears which writes are allowed. The other half would be twice
    the words for the question the caller did not ask.
    """
    if method in _SAFE_METHODS:
        return (
            "This needs the web app, signed in. An API key reads everything except "
            f"{_read_exclusions()}."
        )
    return (
        f"This needs the web app, signed in. An API key writes only these: {_write_permissions()}."
    )


def _api_key_allowed(method: str, path: str) -> bool:
    """What one API key may reach, deny-by-default.

    Reads are open except the handful that reveal a stored secret; writes are closed
    except the explicit automation allowlist. Because a new route is closed to the key
    until it is deliberately opened here, adding one can never silently widen what a
    leaked key can do -- the failure a denylist had (the exfiltrating Plex-connection
    write and the proxy-trust write were both reachable simply by not being listed).
    """
    if method in _SAFE_METHODS:
        return path not in _API_KEY_READ_DENY
    if path in _API_KEY_WRITE_ALLOW:
        return True
    # Planning a specific run: its dry run, but never its execute.
    return path.startswith("/api/runs/") and path.endswith("/dry-run")


def _is_open(path: str) -> bool:
    return path in _OPEN_EXACT or path.startswith(_OPEN_PREFIX)


def api_key_refused(method: str, path: str) -> bool:
    """Will this route turn away a caller holding nothing but an API key?

    The one question a script author has per route, and the reference's per-operation
    marking is this answer, so the document cannot offer a credential the request will not
    accept.

    **Two ways to be turned away, and both count**, because the caller cannot tell them
    apart from the response. This fence refuses it (``_api_key_allowed``, the interesting
    case), or the path is open to the guard and the handler refuses an anonymous caller on
    its own (``_SIGNED_IN_ONLY_READS``). This function used to answer only the first and
    say so, which read as rigor: the second is route logic, not a fence. But the reference
    consumed the answer as "does a key reach this", so ``GET /api/auth/me`` was published
    as key-reachable and answers 401 -- one route where the marking claimed the opposite
    of what it measured, in a document written to end exactly that.

    Every other open path answers False. The guard asks for no credential there, so the
    key is neither accepted nor what stands between the caller and an answer;
    ``no_credential_needed`` is how the reference says THAT, rather than leaving it to
    inherit a requirement it does not have.

    Takes the templated path (``/api/runs/{run_id}/dry-run``) as readily as a concrete
    one, which is what lets the schema be annotated: every allowlist entry is a static
    path, and the dry run's prefix-and-suffix test reads the same on either spelling.
    """
    if path in _SIGNED_IN_ONLY:
        return True
    return not _is_open(path) and not _api_key_allowed(method, path)


def no_credential_needed(path: str) -> bool:
    """Does this route ask an anonymous caller for nothing at all?

    True for the health probe and the sign-in endpoints, which have to work before anyone
    is signed in, minus the ones that refuse anonymously anyway (``_SIGNED_IN_ONLY_READS``
    is subtracted, or this would contradict ``api_key_refused`` on the same path).

    A CSRF header is still required on the unsafe ones. That is not a credential and this
    does not claim otherwise -- ``main``'s ``Session`` scheme description is where the
    header is named.
    """
    return _is_open(path) and path not in _SIGNED_IN_ONLY


def parse_proxy_networks(entries: list[str]) -> tuple[IPv4Network | IPv6Network, ...]:
    """Parse stored trusted-proxy entries into networks, dropping anything malformed.

    A single address becomes its /32 (or /128) network. Dropping rather than raising is
    the fail-closed direction here: an unparseable entry trusts NOBODY extra.
    """
    networks: list[IPv4Network | IPv6Network] = []
    for entry in entries:
        cleaned = entry.strip()
        if not cleaned:
            continue
        try:
            networks.append(ip_network(cleaned, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def client_ip(request: Request) -> str:
    """The address rate limits key on.

    The direct peer address, unless the operator turned on reverse-proxy trust in
    Settings -> General AND the peer is one of the proxies they listed -- only then is
    ``X-Forwarded-For`` consulted, walking from its rightmost hop and returning the
    first address that is not a trusted proxy. Anyone else's forwarded header is
    attacker-controlled noise and is ignored, so a stranger cannot rotate spoofed
    addresses to dodge the per-IP lockout. Any parse trouble falls back to the peer.
    """
    client = request.client
    peer = client.host if client is not None else "unknown"
    proxies: tuple[IPv4Network | IPv6Network, ...] = getattr(
        request.app.state, "trusted_proxies", ()
    )
    if not proxies:
        return peer
    try:
        peer_ip = ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_ip in net for net in proxies):
        return peer
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            hop_ip = ip_address(hop)
        except ValueError:
            return peer
        if not any(hop_ip in net for net in proxies):
            return str(hop_ip)
    return peer


async def _refuse_scope(scope: Scope, receive: Receive, send: Send) -> None:
    """Turn away a connection this guard cannot authenticate.

    A websocket is refused the way ASGI expects and Starlette's own router does it:
    close before accepting, which the server turns into an HTTP 403. ``1008`` is the
    protocol's "policy violation". Any other scope type gets nothing at all --
    returning without calling the app is itself the refusal, and inventing a reply for
    a protocol we do not speak would be guessing.
    """
    if scope["type"] == "websocket":
        await WebSocketClose(code=1008, reason="Reaper serves no websocket routes.")(
            scope, receive, send
        )


def _csrf_ok(request: Request) -> bool:
    # The load-bearing check: a header no cross-origin form or simple request can
    # set. Our frontend sends it on every request (see frontend/src/api.ts).
    if request.headers.get("x-reaper-csrf") != "1":
        return False
    # Belt and suspenders, and proxy-safe: Sec-Fetch-Site is set by the browser
    # from the actual context and survives a dev proxy (unlike an Origin/Host
    # comparison, which changeOrigin rewriting breaks). Absent on older browsers,
    # where the header check alone still holds.
    site = request.headers.get("sec-fetch-site")
    return site is None or site in ("same-origin", "same-site", "none")


async def _reject(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
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

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type: str = scope["type"]
        if scope_type == "lifespan":
            # Startup/shutdown, not a request: there is no session to check, and the
            # app never starts if this does not reach the router.
            await self.app(scope, receive, send)
            return
        if scope_type != "http":
            # Everything below reads headers, cookies and a method: an http request.
            # Refuse anything else instead of handing it to the app unauthenticated.
            await _refuse_scope(scope, receive, send)
            return

        path: str = scope["path"]
        # Everything that is not the API -- the built SPA, its assets, the login
        # page itself -- is served without a session, or you could never reach the
        # screen that logs you in.
        if not path.startswith("/api"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)  # header/cookie/url reads only; the body is never touched

        # The API-key lane, for scripts and other apps. Explicit and exclusive: a
        # request that presents X-Api-Key is judged on the key alone, never falling
        # back to a cookie session. No CSRF check on this lane -- CSRF defends cookie
        # credentials a browser attaches automatically, and no cross-site page can set
        # this header without a preflight this server never grants.
        provided_key = request.headers.get("x-api-key")
        if provided_key is not None and not _is_open(path):
            await self._handle_api_key(scope, receive, send, request, provided_key, path)
            return

        if scope["method"] not in _SAFE_METHODS and not _csrf_ok(request):
            await _reject(send, 403, "This request was blocked by Reaper's CSRF protection.")
            return

        if _is_open(path):
            await self.app(scope, receive, send)
            return

        factory = request.app.state.session_factory
        async with factory() as session:
            user, _ = await resolve_session_from_cookies(session, request.cookies)
            await session.commit()  # persist the throttled last_seen bump / expiry prune

        if user is None:
            await _reject(send, 401, "Not authenticated.")
            return

        await self.app(scope, receive, send)

    async def _handle_api_key(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        request: Request,
        provided: str,
        path: str,
    ) -> None:
        """Judge one request on its API key: throttle bad guesses, fence the
        password-only routes, and otherwise let it through as the operator.

        Compared as SHA-256 digests in constant time. The digest is cached on
        ``app.state`` at startup and on every key change, so the hot path never
        touches the database or the encryption layer.
        """
        throttle_key = f"api-key:{client_ip(request)}"
        if api_key_throttle.retry_after(throttle_key) > 0:
            await _reject(
                send, 429, "Too many bad API keys from this address. Wait a moment and try again."
            )
            return

        digest: bytes | None = getattr(request.app.state, "api_key_digest", None)
        provided_digest = hashlib.sha256(provided.encode("utf-8")).digest()
        if digest is None or not hmac.compare_digest(provided_digest, digest):
            api_key_throttle.record_failure(throttle_key)
            await _reject(send, 401, "That API key is not valid.")
            return
        api_key_throttle.record_success(throttle_key)

        if not _api_key_allowed(scope["method"], path):
            await _reject(send, 403, api_key_refusal_detail(scope["method"]))
            return

        await self.app(scope, receive, send)
