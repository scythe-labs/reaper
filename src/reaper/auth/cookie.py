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

#: The name the API reference documents the browser session under. A schema's cookie
#: field takes one string and this pair is two names, so the plain one stands for both
#: and the scheme's description names the ``__Host-`` twin an HTTPS install gets instead.
#: Documentation only: nothing reads a cookie by this constant, so it can never become a
#: third spelling the resolver above does not know.
DOCUMENTED_SESSION_COOKIE = _PLAIN_NAME


def _forwarded_proto(request: Request) -> str:
    """The browser leg's scheme as ``X-Forwarded-Proto`` claims it, or ``""`` if unclaimed.

    The leftmost hop is the one nearest the browser, lowercased for comparison. Says
    nothing about whether the claim may be believed -- that is
    :func:`reaper.auth.proxy.peer_is_trusted_proxy`, and every caller asks it first.
    """
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()


def is_secure_request(request: Request) -> bool:
    """Is the BROWSER's leg HTTPS -- directly, or via a terminating proxy?

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

    **A trusted proxy's claim outranks the transport scheme, in both directions.** This
    used to return ``True`` the moment ``request.url.scheme`` was ``https``, before asking
    anything about trust, and that was wrong twice over. It answered about the wrong leg:
    a proxy that speaks HTTPS to Reaper while serving the browser over plain HTTP got a
    ``Secure``/``__Host-`` cookie the browser drops. And the scheme is not always the
    transport's -- an upstream ``ProxyHeadersMiddleware`` derives it from this same
    header, so short-circuiting on it laundered the unauthenticated claim into a trusted
    answer. Reaper's own image runs uvicorn ``--no-proxy-headers`` so no such layer exists
    (see the ``CMD`` in ``Dockerfile``); this does not depend on that being remembered.
    An untrusted peer claiming ``https`` therefore gets ``False`` outright -- a claim
    Reaper may not believe can never be the reason it calls a connection secure.

    An operator running HTTPS behind a proxy they have NOT listed gets ``False`` here,
    and so gets a plain, non-``Secure`` cookie. That is a real hardening downgrade, and
    it is why the proxy-trust setting says so in the UI: the honest fix is to list the
    proxy, not to believe an unauthenticated header.
    """
    forwarded = _forwarded_proto(request)
    if peer_is_trusted_proxy(request):
        return forwarded == "https" if forwarded else request.url.scheme == "https"
    if forwarded == "https":
        return False
    return request.url.scheme == "https"


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


def _delete(response: Response, name: str) -> None:
    """Emit the delete for one cookie name, with the ``Secure`` flag that name requires.

    The flag is a property of the NAME, not of the request, and that is the whole point.
    A browser only accepts a ``__Host-`` cookie carrying ``Secure``, deletion included, so
    clearing it with the request's own flag broke the most common install shape: TLS
    terminated at a reverse proxy the operator had not listed under proxy trust, where
    :func:`is_secure_request` returns ``False``. The delete went out without ``Secure``,
    the browser discarded it, and the cookie survived in the jar with its database row
    already gone. Every later sign-in wrote the plain name, the dead ``__Host-`` cookie
    outranked it on read, and sign-in silently stopped working until the operator cleared
    cookies by hand or the 30-day window lapsed.

    A delete carries no value, so marking it ``Secure`` leaks nothing. On a genuinely
    plain-HTTP install a browser ignores the ``Secure`` delete, but no ``__Host-`` cookie
    was ever set there for it to clear, and a ``Secure`` cookie is never *sent* over plain
    HTTP either, so nothing can shadow anything. The plain name is cleared with
    ``secure=False`` so its delete is accepted on either scheme.
    """
    response.delete_cookie(
        key=name, path="/", samesite="lax", secure=name == _SECURE_NAME, httponly=True
    )


def set_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    """Write the session cookie under the name this connection calls for, and clear the other.

    Clearing the other name is what keeps exactly ONE session cookie in the jar, and it is
    load-bearing rather than tidy. Writing one name and leaving the other alone let a
    still-LIVE cookie under the unused name outlive every later sign-in: the read prefers
    ``__Host-``, so with one in the jar the app kept authenticating as whoever owned it.
    Signing in as a second admin appeared to succeed and the app stayed signed in as the
    first. :func:`read_session_tokens` and
    :func:`reaper.auth.sessions.resolve_session_from_cookies` close the case where the
    shadowing cookie is DEAD; only this closes the case where it is alive.

    Switching scheme still never strands a session. Moving to HTTPS, the new ``__Host-``
    cookie is written and the plain one cleared. Moving away from it, the plain cookie is
    written and the ``__Host-`` delete goes out ``Secure``: over an HTTPS browser leg it
    lands, and over a genuinely plain-HTTP one the browser would not have sent that cookie
    in the first place.
    """
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
    _delete(response, _PLAIN_NAME if secure else _SECURE_NAME)


def clear_session_cookie(response: Response) -> None:
    """Delete both names, each with the ``Secure`` flag that name itself requires.

    Both go, so a scheme change (or a stale plain cookie left over from an HTTP session
    before TLS was added) cannot leave a zombie behind. :func:`_delete` explains why the
    flag follows the name rather than the request, and what it cost when it did not.
    """
    _delete(response, _SECURE_NAME)
    _delete(response, _PLAIN_NAME)
