# SPDX-License-Identifier: AGPL-3.0-or-later
"""The gate every request passes through.

Two jobs, both enforced here rather than sprinkled across routers, so that a new
endpoint is protected by *existing at all* rather than by remembering to add a
dependency:

* **Authentication.** Every ``/api`` route requires a valid session, except the
  handful that must work *before* you are logged in -- the health probe and the
  ``/api/auth`` endpoints themselves. There is no per-route opt-in; a route is
  behind auth unless this file names it as open.

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

import json

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from reaper.auth.cookie import read_session_token
from reaper.auth.sessions import resolve_session

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Reachable without a session. The health probe (liveness checks run
# unauthenticated) and everything under /api/auth (you cannot log in if logging in
# requires being logged in).
_OPEN_EXACT = frozenset({"/api/health"})
_OPEN_PREFIX = "/api/auth/"


def _is_open(path: str) -> bool:
    return path in _OPEN_EXACT or path.startswith(_OPEN_PREFIX)


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

        if scope["method"] not in _SAFE_METHODS and not _csrf_ok(request):
            await _reject(send, 403, "This request was blocked by Reaper's CSRF protection.")
            return

        if _is_open(path):
            await self.app(scope, receive, send)
            return

        token = read_session_token(request.cookies)
        factory = request.app.state.session_factory
        async with factory() as session:
            user = await resolve_session(session, token)
            await session.commit()  # persist the throttled last_seen bump / expiry prune

        if user is None:
            await _reject(send, 401, "Not authenticated.")
            return

        await self.app(scope, receive, send)
