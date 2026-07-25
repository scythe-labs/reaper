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

Because both names can be in the jar at once, two rules hold everywhere below:

* **Reading returns every token, never just the first name that exists.** A stale
  cookie under one name must not shadow a live session under the other. The caller
  resolves them in order (:func:`reaper.auth.sessions.resolve_session_from_cookies`).
* **Clearing uses the flag each NAME requires, not the request's.** See
  :func:`clear_session_cookie` -- getting this wrong is what locked operators out.
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

    An operator running HTTPS behind a proxy they have NOT listed gets ``False`` here,
    and so gets a plain, non-``Secure`` cookie. That is a real hardening downgrade, and
    it is why the proxy-trust setting says so in the UI: the honest fix is to list the
    proxy, not to believe an unauthenticated header.
    """
    if request.url.scheme == "https":
        return True
    if not peer_is_trusted_proxy(request):
        return False
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"


def read_session_tokens(cookies: Mapping[str, str]) -> tuple[str, ...]:
    """Every session token the jar carries, ``__Host-`` name first.

    Returns both, deduplicated, because a jar can legitimately hold both names at once:
    after a scheme change, or after a delete the browser refused. This used to return
    ``secure or plain``, which took the first cookie that merely EXISTED -- so a dead
    ``__Host-`` cookie outranked the live plain one written by every later sign-in, and
    the operator could not get back in. The caller resolves these in order and keeps the
    first that is really live (:func:`reaper.auth.sessions.resolve_session_from_cookies`).
    """
    tokens: list[str] = []
    for name in (_SECURE_NAME, _PLAIN_NAME):
        token = cookies.get(name)
        if token and token not in tokens:
            tokens.append(token)
    return tuple(tokens)


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


def clear_session_cookie(response: Response) -> None:
    """Delete both names, each with the ``Secure`` flag that name itself requires.

    Both go, so a scheme change (or a stale plain cookie left over from an HTTP session
    before TLS was added) cannot leave a zombie behind.

    The flag is a property of the NAME, not of the request, and that is the whole point.
    A browser only accepts a ``__Host-`` cookie carrying ``Secure``, deletion included.
    Clearing it with the request's own flag therefore broke the most common install
    shape: TLS terminated at a reverse proxy the operator had not listed under proxy
    trust, where :func:`is_secure_request` returns ``False``. The delete went out without
    ``Secure``, the browser discarded it, and the cookie survived in the jar with its
    database row already gone. Every later sign-in wrote the plain name, the dead
    ``__Host-`` cookie outranked it on read, and sign-in silently stopped working until
    the operator cleared cookies by hand or the 30-day window lapsed.

    A delete carries no value, so marking it ``Secure`` leaks nothing. On a genuinely
    plain-HTTP install a browser may refuse the ``Secure`` delete, but no ``__Host-``
    cookie was ever set there for it to refuse. The plain name is cleared with
    ``secure=False`` so its delete is accepted on either scheme.
    """
    response.delete_cookie(key=_SECURE_NAME, path="/", samesite="lax", secure=True, httponly=True)
    response.delete_cookie(key=_PLAIN_NAME, path="/", samesite="lax", secure=False, httponly=True)
