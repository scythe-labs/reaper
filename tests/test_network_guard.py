# SPDX-License-Identifier: AGPL-3.0-or-later
"""The suite reaches no network, and this is what makes that a fact rather than a claim.

``tests/conftest.py`` said "no network" for long enough that seven tests were resolving a
real hostname behind the sentence, on every run, for as long as anyone had been reading it
(rule 7/24). The guard those seven now sit behind is in ``conftest``; the reconciliation of
what it sees is here.

**A guard that fires on nothing has zero false positives too**, which is the failure mode
this file is written against. The obvious hook point is ``connect()``, and measured, the
suite's own outbound traffic never reaches it: name resolution fails first, so a
``connect``-only guard would report a clean suite while watching a call nobody makes. So
the tests below drive the guard through the real stack rather than through ``socket``
directly, and the population it allows is written out and driven member by member
(rule 145).
"""

from __future__ import annotations

import contextlib
import socket

import pytest

from reaper.clients.plex import PlexClient
from reaper.config import RuntimeSafety
from tests.conftest import (
    _LOOPBACK_HOSTS,
    NetworkReached,
    _guarded_connect,
    _guarded_connect_ex,
    _guarded_getaddrinfo,
    _network_attempts,
)

#: A name that cannot resolve even if the guard is gone, so a regression here is a failed
#: assertion rather than a real lookup. `.invalid` is reserved for exactly this (RFC 2606).
UNREACHABLE = "nothing.invalid"

#: Documentation range, reserved and unroutable (RFC 5737). Used where the point is to skip
#: resolution entirely and hand `connect` a literal.
UNREACHABLE_IP = "192.0.2.1"

#: Every place the guard is installed. Three, because two of them see nothing on their own:
#: `getaddrinfo` is where all of this suite's outbound traffic actually goes, and `connect`
#: and `connect_ex` are for the address that never needs resolving.
_HOOKS = 3


def test_the_guard_is_installed_process_wide_rather_than_per_test() -> None:
    """It goes on in ``pytest_configure``, and the scoping is the reason.

    A fixture cannot cover this. ``_hermetic`` is function-scoped, so anything higher-scoped
    is set up before it -- and the one session-scoped fixture in the suite boots an app, which
    is exactly the setup a network guard would want to be watching. Installing at configure
    time means there is no window at all.
    """
    installed = [
        socket.getaddrinfo is _guarded_getaddrinfo,
        socket.socket.connect is _guarded_connect,
        socket.socket.connect_ex is _guarded_connect_ex,
    ]
    assert sum(installed) == _HOOKS, (
        f"{sum(installed)} of {_HOOKS} network hooks are installed: {installed}.\n"
        "An uninstalled hook refuses nothing, and every assertion in this file that does not\n"
        "drive that particular call still passes."
    )


def test_a_name_lookup_off_the_loopback_is_refused() -> None:
    with pytest.raises(NetworkReached, match=UNREACHABLE):
        socket.getaddrinfo(UNREACHABLE, 443)
    _network_attempts.clear()


def test_a_connection_to_a_literal_address_is_refused_without_any_lookup() -> None:
    """The case ``getaddrinfo`` never sees: an address that needs no resolving.

    Nothing in the suite does this today, which is precisely why it is driven here. A hook
    count reconciled against calls the suite happens to make would leave this one uncovered
    and read as complete.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(NetworkReached, match=UNREACHABLE_IP):
            sock.connect((UNREACHABLE_IP, 80))
        with pytest.raises(NetworkReached, match=UNREACHABLE_IP):
            sock.connect_ex((UNREACHABLE_IP, 80))
    _network_attempts.clear()


@pytest.mark.parametrize("host", sorted(_LOOPBACK_HOSTS, key=repr))
def test_every_allowed_host_really_is_allowed(host: str | None) -> None:
    """Each member of the allowlist, driven.

    The set is the population this guard is reconciled against, and a set-equality assertion
    over it would not tell a member the guard allows from one it refuses (rule 145). A wildcard
    (``0.0.0.0``, ``::``, ``None``, ``""``) is a listener rather than a destination, which is
    why it is on the list at all.

    **What is asserted is that the guard let the call through, not that the call succeeded**,
    and the two come apart on ``""``: glibc raises ``gaierror`` for an empty host where macOS
    resolves it to the loopback. Either answer is the real resolver answering, which is the
    whole claim. A refusal is a ``NetworkReached``, which is not caught here and fails the
    test on the spot.
    """
    with contextlib.suppress(socket.gaierror):
        socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM)
    assert not _network_attempts, f"{host!r} is on the allowlist and was refused anyway"


def test_the_allowlist_is_the_loopback_and_the_wildcard_and_nothing_else() -> None:
    """Written out here so widening it is a two-file edit somebody has to mean."""
    written_out_here = frozenset(
        {None, "", "localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}  # noqa: S104 -- not a bind
    )
    assert written_out_here == _LOOPBACK_HOSTS, (
        "the network allowlist moved. Every entry is a host a test may reach: the loopback,\n"
        "and the wildcard spellings that mean 'listen' rather than 'dial'. Anything else on\n"
        "this list is a hole, and the test above drives whatever is on it."
    )


def test_a_unix_socketpair_is_not_touched() -> None:
    """The 4,046-strong population a naive guard breaks.

    Every ``TestClient`` block builds an asyncio loop self-pipe on a worker thread, and the
    suite made 2,023 of them the last time this was counted. They reach nobody, so the guard
    hooks neither ``socket()`` nor ``bind()`` and never sees one.
    """
    left, right = socket.socketpair()
    with left, right:
        left.send(b"ping")
        assert right.recv(4) == b"ping"
    assert not _network_attempts


async def test_the_guard_sits_on_the_path_the_suite_s_real_traffic_takes() -> None:
    """Driven through plexapi, which is where the seven live violations were.

    This is the anti-vacuity test, and the only one here that would notice the guard being
    hooked somewhere real code does not pass through. ``PlexClient._connect`` builds a
    plexapi server, which is ``requests`` over ``urllib3`` over ``socket.getaddrinfo`` -- a
    stack ``respx`` does not intercept, because respx is an httpx transport.

    **What comes out is ``NetworkReached`` and not ``PlexError``**, and that is the second
    proof in the same call. ``_connect`` maps every ``Exception`` to ``PlexError`` so its
    callers have one error to handle; a guard raising an ordinary exception would be
    converted right there and the refusal would read as an unreachable server.
    """
    client = PlexClient(
        f"https://{UNREACHABLE}:32400",
        "token",
        safety=RuntimeSafety(),
        verify=False,
    )
    try:
        with pytest.raises(NetworkReached, match=UNREACHABLE):
            await client.connect()
    finally:
        await client.aclose()
    _network_attempts.clear()


def test_a_refusal_is_written_down_even_when_something_catches_it() -> None:
    """The teardown half, which is what covers a caller that catches ``BaseException``.

    ``NetworkReached`` off ``Exception`` handles the broad-but-ordinary catches, and there
    are two of them on the path above. It does not handle a bare ``except:``, an exception
    swallowed on a thread nobody joins, or a future whose result is never read. So the
    attempt is recorded before it is raised, and ``_no_network`` fails the test in teardown
    on anything left in the list -- which is the assertion that cannot be caught at all.
    """
    with contextlib.suppress(BaseException):
        socket.getaddrinfo(UNREACHABLE, 443)
    assert _network_attempts == [f"resolved '{UNREACHABLE}'"]
    _network_attempts.clear()
