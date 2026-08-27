# SPDX-License-Identifier: AGPL-3.0-or-later
"""The session cookie: how it is named, set, read, and cleared.

The cookie's *name* needs explaining. The ``__Host-`` prefix is the strongest form: a
browser forces ``Secure``, ``Path=/`` and no ``Domain`` on it, so a cookie carrying that
name can never be scoped to a parent domain or sent over plain HTTP. But a browser also
silently drops a ``__Host-`` cookie that arrives without ``Secure``, and many self-hosted
installs run on a plain-HTTP LAN address like ``http://192.168.1.10:8420``. Always using
the secure name there means sign-in looks like it works and then nothing happens, with no
error anywhere.

So the cookie name follows the connection: the ``__Host-`` name over HTTPS, a plain name
otherwise. Reading honors both, and logout clears both, so switching scheme never strands
a session.

Because both names can be in the jar at once, two rules hold everywhere below:

* **Reading returns every token, never just the first name that exists.** A stale
  cookie under one name must not hide a live session under the other. The caller
  checks them in order (:func:`reaper.auth.sessions.resolve_session_from_cookies`).
* **Clearing uses the flag each name requires, not the request's own flag.** See
  :func:`clear_session_cookie`.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import Request, Response

from reaper.auth.proxy import peer_is_trusted_proxy
from reaper.auth.tokens import SESSION_TTL

_SECURE_NAME = "__Host-reaper_session"
_PLAIN_NAME = "reaper_session"
_MAX_AGE = int(SESSION_TTL.total_seconds())

#: The cookie name shown in the API reference. The reference's schema needs one string,
#: but Reaper actually uses two names, so this constant picks the plain one, and the
#: schema's description mentions the ``__Host-`` name an HTTPS install gets instead.
#: Documentation only: no code reads a cookie by this constant, so it can never become a
#: third spelling the resolver above does not know.
DOCUMENTED_SESSION_COOKIE = _PLAIN_NAME


def _forwarded_proto(request: Request) -> str:
    """The scheme ``X-Forwarded-Proto`` claims for the browser's own leg of the
    connection, as opposed to the proxy's separate connection to Reaper, or ``""`` if the
    header is absent.

    The leftmost hop is the one nearest the browser, lowercased for comparison. This only
    reads the claim; whether to believe it is
    :func:`reaper.auth.proxy.peer_is_trusted_proxy`, which every caller asks first.
    """
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()


def is_secure_request(request: Request) -> bool:
    """Whether the browser's own connection is HTTPS, directly or through a terminating
    proxy.

    Behind a reverse proxy Reaper itself speaks plain HTTP, and only
    ``X-Forwarded-Proto`` records that the browser's side was encrypted. This function
    honors that header only from a peer the operator listed as a trusted proxy, the same
    check ``X-Forwarded-For`` goes through
    (:func:`reaper.auth.proxy.peer_is_trusted_proxy`). An untrusted peer's claim is never
    honored, because a header a client can freely write must not be allowed to decide an
    auth cookie's flags: honoring it from anyone would let a caller order up a
    ``Secure``/``__Host-`` cookie that a plain-HTTP browser then drops silently, the exact
    failure the ``__Host-`` note above warns about.

    **A trusted proxy's claim outranks the raw transport scheme, in both directions.**
    ``request.url.scheme`` is not always the browser's real scheme: a proxy that speaks
    HTTPS to Reaper while serving the browser over plain HTTP makes the raw scheme read
    HTTPS when the browser's own leg is not. An upstream ``ProxyHeadersMiddleware`` can
    also derive that raw scheme from this same header, so trusting it over an explicit
    distrust would launder an unverified claim into a trusted one. Reaper's own image
    disables that middleware with uvicorn's ``--no-proxy-headers`` (see the ``CMD`` in
    ``Dockerfile``), and this function does not depend on that being remembered: an
    untrusted peer claiming ``https`` gets ``False`` regardless of the raw scheme.

    An operator running HTTPS behind a proxy they have not listed as trusted also gets
    ``False`` here, and so a plain, non-``Secure`` cookie. That is a real downgrade, and
    the proxy-trust setting says so in the UI: the fix is to list the proxy, not to trust
    an unverified header.
    """
    forwarded = _forwarded_proto(request)
    if peer_is_trusted_proxy(request):
        return forwarded == "https" if forwarded else request.url.scheme == "https"
    if forwarded == "https":
        return False
    return request.url.scheme == "https"


def read_session_tokens(cookies: Mapping[str, str]) -> tuple[str, ...]:
    """Every session token the cookie jar carries, ``__Host-`` name first.

    Returns both, deduplicated, because a jar can legitimately hold both names at once:
    after a scheme change, or after a delete the browser refused. The token that exists
    first is not necessarily the one still valid, so the caller checks each token in turn
    and keeps the first that actually resolves to a live session
    (:func:`reaper.auth.sessions.resolve_session_from_cookies`), rather than trusting
    whichever name happens to come first.
    """
    tokens: list[str] = []
    for name in (_SECURE_NAME, _PLAIN_NAME):
        token = cookies.get(name)
        if token and token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def _delete(response: Response, name: str) -> None:
    """Send the delete for one cookie name, with the ``Secure`` flag that name requires.

    The flag follows the cookie's name, never the request's own scheme. A browser only
    accepts a ``__Host-`` cookie that carries ``Secure``, deletion included. If the delete
    for ``__Host-reaper_session`` went out without ``Secure`` (which happens whenever
    :func:`is_secure_request` returns ``False``, such as behind a reverse proxy the
    operator has not listed as trusted), the browser would ignore it and keep the dead
    cookie. Because reading prefers the ``__Host-`` name, that dead cookie would then
    outrank every live plain-name cookie written by later sign-ins, and sign-in would look
    broken until the operator cleared cookies by hand or the 30-day session expired.

    Marking a delete ``Secure`` leaks nothing, since a delete carries no value. On a
    genuinely plain-HTTP install a browser ignores the ``Secure`` delete, but no
    ``__Host-`` cookie was ever set there to need clearing, and a ``Secure`` cookie is
    never sent over plain HTTP either, so nothing is left behind. The plain name is
    cleared with ``secure=False`` so its own delete is accepted on either scheme.
    """
    response.delete_cookie(
        key=name, path="/", samesite="lax", secure=name == _SECURE_NAME, httponly=True
    )


def set_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    """Write the session cookie under the name this connection calls for, and clear the other.

    Clearing the other name keeps exactly one session cookie in the jar, and this matters:
    a live cookie left under the unused name would still work, since reading prefers the
    ``__Host-`` name. A second admin signing in would then appear to succeed while the app
    kept authenticating requests as the first admin. :func:`read_session_tokens` and
    :func:`reaper.auth.sessions.resolve_session_from_cookies` handle the case where the
    leftover cookie is dead; only clearing it here handles the case where it is still live.

    Switching scheme never strands a session either way. Moving to HTTPS writes the new
    ``__Host-`` cookie and clears the plain one. Moving away from HTTPS writes the plain
    cookie and sends the ``__Host-`` delete marked ``Secure``: that delete reaches the
    browser over an HTTPS connection, and a genuinely plain-HTTP browser would never have
    sent that cookie in the first place.
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
    """Delete both cookie names, each with the ``Secure`` flag that name itself requires.

    Deleting both means a scheme change, or a stale plain cookie left from before TLS was
    added, can never leave one behind. :func:`_delete` explains why the flag follows the
    name rather than the request.
    """
    _delete(response, _SECURE_NAME)
    _delete(response, _PLAIN_NAME)
