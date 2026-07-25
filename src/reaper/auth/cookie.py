# SPDX-License-Identifier: AGPL-3.0-or-later
"""The session cookie: how it is named, set, read, and cleared.

The one subtlety worth stating out loud is the cookie *name*. The ``__Host-``
prefix is the strongest form -- it forces ``Secure``, ``Path=/`` and no ``Domain``,
so a cookie carrying it cannot be scoped to a parent domain or written over plain
HTTP. But that strength is also a trap: a browser silently **drops** a ``__Host-``
cookie that arrives without ``Secure``, and a great many self-hosted deployments
run on a plain-HTTP LAN address like ``http://192.168.1.10:8420``. Hardcoding the
secure name there produces the worst kind of bug -- "I log in and nothing happens,
no error anywhere".

So the name follows the connection: the ``__Host-`` name over HTTPS, a plain name
otherwise. Both are honored on read, and both are cleared on logout, so switching
scheme never strands a session.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import Request, Response

from reaper.auth.proxy import peer_is_trusted_proxy
from reaper.auth.tokens import SESSION_TTL

_SECURE_NAME = "__Host-reaper_session"
_PLAIN_NAME = "reaper_session"
_MAX_AGE = int(SESSION_TTL.total_seconds())


def is_secure_request(request: Request) -> bool:
    """Is this connection HTTPS -- directly, or via a terminating proxy?

    Behind a reverse proxy the app speaks plain HTTP, and only
    ``X-Forwarded-Proto`` records that the browser's leg was encrypted. Honor it,
    so the cookie is marked ``Secure`` in exactly the cases where it can be -- but
    only from a peer the operator listed as a proxy, exactly as ``X-Forwarded-For``
    is honored (:func:`reaper.auth.proxy.peer_is_trusted_proxy`).

    The header used to be believed from anyone (S-7). On a plain-HTTP install that let
    a caller name the cookie for itself: send ``X-Forwarded-Proto: https`` and get back
    a ``Secure``/``__Host-`` cookie the browser then drops over HTTP, which is a sign-in
    that silently does nothing -- the exact failure the ``__Host-`` note above exists to
    avoid. It reaches only the sender's own response, but a header deciding an auth
    cookie's flags should not be attacker-writable at all.
    """
    if request.url.scheme == "https":
        return True
    if not peer_is_trusted_proxy(request):
        return False
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"


def read_session_token(cookies: Mapping[str, str]) -> str | None:
    return cookies.get(_SECURE_NAME) or cookies.get(_PLAIN_NAME)


def set_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    response.set_cookie(
        key=_SECURE_NAME if secure else _PLAIN_NAME,
        value=token,
        max_age=_MAX_AGE,
        # httpOnly: a cookie a page script can read is a cookie an XSS can steal.
        httponly=True,
        # Lax, not Strict: a bookmark or a link into the app must still arrive logged in.
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    # Delete both names. A scheme change (or a stale plain cookie left over from an
    # HTTP session before TLS was added) must not leave a zombie behind.
    for name in (_SECURE_NAME, _PLAIN_NAME):
        response.delete_cookie(key=name, path="/", samesite="lax", secure=secure, httponly=True)
