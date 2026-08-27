# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a forwarded header is worth: the one place peer trust is decided.

Reaper is usually reached through a reverse proxy, so the only record of the browser's
real address and scheme is a header the proxy adds. A header is just text a client can
type, though, so honoring it unconditionally hands an attacker whatever it wants: a
rotating fake address to dodge a per-IP lockout, or a fake ``https`` that flips the
session cookie's ``Secure`` flag on a plain-HTTP install and makes the browser drop it.

So every forwarded header is read through :func:`peer_is_trusted_proxy`: honored only
when the operator has turned reverse-proxy trust on in Settings and the direct peer is
one of the proxies they listed, ignored from anyone else. Both consumers, the
rate-limit key and the cookie flags, read this one decision rather than two separate
checks.

**This one decision only holds if the server has not already made its own.** uvicorn
ships with ``proxy_headers=True`` and ``forwarded_allow_ips="127.0.0.1"``, and its
``ProxyHeadersMiddleware`` rewrites ``scope["client"]`` and ``scope["scheme"]`` from
these same headers before any application code runs. Where Reaper's peer really is
loopback (host networking, a same-host proxy published to ``127.0.0.1:8420``, another
container sharing the network namespace, a dev server), :func:`peer_address` below
would then read a value the caller wrote and faithfully report the spoof. Every launch
of the app therefore passes ``--no-proxy-headers``, and
``tests/test_repo_hygiene.py::test_every_uvicorn_launch_disables_proxy_headers`` checks
that every launch carries that flag, rather than relying on a hand-kept list of
launches to stay current.
"""

from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

from starlette.requests import Request


def parse_proxy_networks(entries: list[str]) -> tuple[IPv4Network | IPv6Network, ...]:
    """Parse stored trusted-proxy entries into networks, dropping anything malformed.

    A single address becomes its /32 (or /128) network. Dropping rather than raising is
    the fail-closed direction here: an unparseable entry trusts nobody extra.
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


def peer_address(request: Request) -> str:
    """The direct peer's address, or ``"unknown"`` for a connection without one."""
    client = request.client
    return client.host if client is not None else "unknown"


def _trusted_networks(request: Request) -> tuple[IPv4Network | IPv6Network, ...]:
    """The proxies the operator listed, loaded onto app state at boot and on save.

    Empty when reverse-proxy trust is off, which is the default. Then every forwarded
    header is ignored outright.
    """
    networks: tuple[IPv4Network | IPv6Network, ...] = getattr(
        request.app.state, "trusted_proxies", ()
    )
    return networks


def peer_is_trusted_proxy(request: Request) -> bool:
    """Whether this connection's direct peer is a proxy the operator chose to trust.

    The only gate on believing any ``X-Forwarded-*`` header. This checks the peer's own
    address, never a value out of the header itself, because an address a client can
    write must not be allowed to vouch for the header it was written in.
    """
    networks = _trusted_networks(request)
    if not networks:
        return False
    try:
        peer_ip = ip_address(peer_address(request))
    except ValueError:
        return False
    return any(peer_ip in net for net in networks)


def client_ip(request: Request) -> str:
    """The address rate limits key on.

    Returns the direct peer's address, unless the operator turned on reverse-proxy trust
    in Settings -> General and the peer is one of the proxies they listed. Only then does
    this consult ``X-Forwarded-For``: it walks from the rightmost hop and returns the
    first address that is not itself a trusted proxy. A forwarded header from anyone else
    is ignored, since a stranger could otherwise rotate spoofed addresses to dodge the
    per-IP lockout. Any parse trouble falls back to the peer's own address.
    """
    peer = peer_address(request)
    if not peer_is_trusted_proxy(request):
        return peer
    networks = _trusted_networks(request)
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            hop_ip = ip_address(hop)
        except ValueError:
            return peer
        if not any(hop_ip in net for net in networks):
            return str(hop_ip)
    return peer
