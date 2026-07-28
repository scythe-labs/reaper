# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a forwarded header is worth: the one place peer trust is decided.

Reaper is usually reached through a reverse proxy, so the only record of the browser's
real address and scheme is a header the proxy adds. But a header is just text a client
can type, so honoring one unconditionally hands an attacker whatever it decides -- a
rotating fake address to dodge a per-IP lockout, or a fake ``https`` that flips the
session cookie's ``Secure`` flag on a plain-HTTP install and makes the browser drop it.

So every forwarded header is read through :func:`peer_is_trusted_proxy`: honored only
when the operator turned reverse-proxy trust on in Settings and the DIRECT peer is one
of the proxies they listed, ignored from anyone else. This lived in
``api.middleware`` and covered ``X-Forwarded-For`` alone, while
``X-Forwarded-Proto`` was taken from any peer at all (S-7); it is here so both
consumers -- the rate-limit key and the cookie flags -- read one decision rather than
two lookalike ones.

**That "one place" claim rests on the server not having decided it already, so the
server is configured to abstain.** uvicorn ships ``proxy_headers=True`` with
``forwarded_allow_ips="127.0.0.1"``, and its ``ProxyHeadersMiddleware`` rewrites
``scope["client"]`` and ``scope["scheme"]`` from these same headers before any
application code runs. Where Reaper's peer really is loopback -- host networking, a
same-host proxy published to ``127.0.0.1:8420``, another container sharing the netns,
a dev server -- :func:`peer_address` below would then be reading a value the caller
wrote, and would faithfully report the spoof. Reaper's ``CMD`` therefore passes
``--no-proxy-headers`` (``Dockerfile``, ``scripts/dev-local.sh``, and the ``README``
dev line), which ``tests/test_repo_hygiene.py`` pins so it cannot quietly go missing.
"""

from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

from starlette.requests import Request


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


def peer_address(request: Request) -> str:
    """The direct peer's address, or ``"unknown"`` for a connection without one."""
    client = request.client
    return client.host if client is not None else "unknown"


def _trusted_networks(request: Request) -> tuple[IPv4Network | IPv6Network, ...]:
    """The proxies the operator listed, loaded onto app state at boot and on save.

    Empty when reverse-proxy trust is off, which is the default -- and which is what makes
    "off" mean every forwarded header is ignored, not merely unused.
    """
    networks: tuple[IPv4Network | IPv6Network, ...] = getattr(
        request.app.state, "trusted_proxies", ()
    )
    return networks


def peer_is_trusted_proxy(request: Request) -> bool:
    """Whether this connection's DIRECT peer is a proxy the operator chose to trust.

    The only gate on believing any ``X-Forwarded-*`` header. Note it is the peer that is
    tested, never a value out of the header itself: an address a client can write cannot
    be allowed to vouch for the header it was written in.
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

    The direct peer address, unless the operator turned on reverse-proxy trust in
    Settings -> General AND the peer is one of the proxies they listed -- only then is
    ``X-Forwarded-For`` consulted, walking from its rightmost hop and returning the
    first address that is not a trusted proxy. Anyone else's forwarded header is
    attacker-controlled noise and is ignored, so a stranger cannot rotate spoofed
    addresses to dodge the per-IP lockout. Any parse trouble falls back to the peer.
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
