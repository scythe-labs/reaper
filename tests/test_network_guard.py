# SPDX-License-Identifier: AGPL-3.0-or-later
"""Proves the suite reaches no real network, instead of only asserting that it does not.

The guard that blocks outbound network calls lives in ``tests/conftest.py``. This file
reconciles what that guard actually catches.

A guard that fires on nothing has zero false positives too, which is the failure mode this
file is written against. Hooking only ``connect()`` looks like it should work, but measured,
the suite's own outbound traffic never reaches it: name resolution fails first, so a
``connect``-only guard would report a clean suite while watching a call nobody makes. So the
tests below drive the guard through the real stack rather than calling ``socket`` directly,
and the set of hosts it allows is written out and tested one host at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import Callable
from typing import Any

import pytest

from reaper.clients.plex import PlexClient
from reaper.config import RuntimeSafety
from tests.conftest import (
    _LOOPBACK_HOSTS,
    NetworkReached,
    _guarded_connect,
    _guarded_connect_ex,
    _guarded_getaddrinfo,
    _guarded_sendmsg,
    _guarded_sendto,
    _network_attempts,
    _real_resolvers,
)

#: A name that cannot resolve even if the guard is gone, so a regression here is a failed
#: assertion rather than a real lookup. `.invalid` is reserved for exactly this (RFC 2606).
UNREACHABLE = "nothing.invalid"

#: Documentation range, reserved and unroutable (RFC 5737). Used where the point is to skip
#: resolution entirely and hand `connect` a literal.
UNREACHABLE_IP = "192.0.2.1"

#: Name of the test whose recorded attempt is asserted verbatim below.
_THIS_TEST = "test_a_refusal_is_written_down_even_when_something_catches_it"

#: The other test that asserts its own recorded attempt verbatim.
_OWNER_TEST = "test_a_refusal_on_a_worker_thread_is_blamed_on_the_test_that_started_it"

#: Every exit the guard installs on, each driven below. `getaddrinfo` is where all of this
#: suite's outbound traffic actually goes. The five resolver siblings answer the same
#: question through the same resolver, so they need the same coverage. `connect` and
#: `connect_ex` cover an address that never needs resolving. `sendto` and `sendmsg` cover
#: UDP, which can reach a host with neither a lookup nor a connect call.
#:
#: A count of hooks INSTALLED is not a count of what is covered. `_REFUSED_THROUGH` and
#: `_ALLOWED_THROUGH` exist because every entry point that reads the allowlist has to be
#: driven with a refused host and an allowed one, so narrowing the check inside any one of
#: them cannot leave this file green.
_HOOKS = 10


def _hook_state() -> dict[str, bool]:
    """Which of the guard's entry points are the guarded functions rather than the originals."""
    installed = {
        "getaddrinfo": socket.getaddrinfo is _guarded_getaddrinfo,
        "connect": socket.socket.connect is _guarded_connect,
        "connect_ex": socket.socket.connect_ex is _guarded_connect_ex,
        "sendto": socket.socket.sendto is _guarded_sendto,
        "sendmsg": socket.socket.sendmsg is _guarded_sendmsg,
    }
    installed.update(
        {name: getattr(socket, name) is not real for name, real in _real_resolvers.items()}
    )
    return installed


def test_the_guard_is_installed_process_wide_rather_than_per_test() -> None:
    """The guard installs in ``pytest_configure``, which covers every fixture scope.

    A fixture cannot cover this. ``_hermetic`` is function-scoped, so anything higher-scoped
    runs before it, including the one session-scoped fixture that boots the app, which is
    exactly the setup a network guard needs to watch. Installing at configure time means
    there is no window before the guard is active.
    """
    installed = _hook_state()
    assert sum(installed.values()) == _HOOKS, (
        f"{sum(installed.values())} of {_HOOKS} network hooks are installed:\n"
        + "\n".join(f"  {name}: {on}" for name, on in sorted(installed.items()))
        + "\n\nAn uninstalled hook refuses nothing, and every assertion in this file that does\n"
        "not drive that particular call still passes."
    )


#: Every entry point that refuses a host, as (label, call). Each is driven against an address
#: that must be refused, and again against each allowlist member. That second pass is what
#: catches a check narrowed inside one entry point while the others stay correct.
_REFUSED_THROUGH: dict[str, Callable[[Any], object]] = {
    "getaddrinfo": lambda host: socket.getaddrinfo(host, 443),
    "gethostbyname": lambda host: socket.gethostbyname(host),
    "gethostbyname_ex": lambda host: socket.gethostbyname_ex(host),
    "gethostbyaddr": lambda host: socket.gethostbyaddr(host),
    "getnameinfo": lambda host: socket.getnameinfo((host, 0), 0),
    "getfqdn": lambda host: socket.getfqdn(host),
    "connect": lambda host: _tcp().connect((host, 80)),
    "connect_ex": lambda host: _tcp().connect_ex((host, 80)),
    "sendto": lambda host: _udp().sendto(b"probe", (host, 9)),
    "sendmsg": lambda host: _udp().sendmsg([b"probe"], [], 0, (host, 9)),
}


def _tcp() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    return sock


def _udp() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


@pytest.mark.parametrize("entry", sorted(_REFUSED_THROUGH))
def test_every_entry_point_refuses_a_host_off_the_loopback(entry: str) -> None:
    """Each hook, driven with an address it must refuse.

    `getfqdn` is the reason this is a sweep rather than one call. It swallows its own errors
    and returns the name unchanged, so a broken hook here would go on answering forever
    without ever looking like a failure. Testing every entry point individually is what
    catches a hook that is missing while the others are in place.
    """
    with pytest.raises(NetworkReached):
        _REFUSED_THROUGH[entry](UNREACHABLE_IP)
    _network_attempts.clear()


def test_a_name_lookup_off_the_loopback_is_refused() -> None:
    with pytest.raises(NetworkReached, match=UNREACHABLE):
        socket.getaddrinfo(UNREACHABLE, 443)
    _network_attempts.clear()


def test_a_connection_to_a_literal_address_is_refused_without_any_lookup() -> None:
    """A literal address needs no name resolution, so ``getaddrinfo`` never sees it.

    Nothing in the suite makes this kind of call today, which is exactly why it is tested
    here. A hook count reconciled only against calls the suite happens to make would leave
    this case uncovered while still reading as complete.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(NetworkReached, match=UNREACHABLE_IP):
            sock.connect((UNREACHABLE_IP, 80))
        with pytest.raises(NetworkReached, match=UNREACHABLE_IP):
            sock.connect_ex((UNREACHABLE_IP, 80))
    _network_attempts.clear()


@pytest.mark.parametrize("host", sorted(_LOOPBACK_HOSTS, key=repr))
def test_every_allowed_host_really_is_allowed(host: str | None) -> None:
    """Drives one allowlist member per test.

    A set-equality assertion over the whole allowlist would not tell a member the guard
    allows from one it refuses, so each member is driven individually. A wildcard
    (``0.0.0.0``, ``::``, ``None``, ``""``) is a listener rather than a destination, which is
    why it is on the list at all.

    This asserts that the guard let the call through, not that the call succeeded. The two
    come apart on ``""``, where glibc raises ``gaierror`` for an empty host but macOS
    resolves it to the loopback. Either answer means the real resolver ran, which is the
    whole claim here. A refusal raises ``NetworkReached``, which is not caught in this test
    and fails it on the spot.
    """
    with contextlib.suppress(socket.gaierror):
        socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM)
    assert not _network_attempts, f"{host!r} is on the allowlist and was refused anyway"


@pytest.mark.parametrize("entry", ["connect", "connect_ex", "sendto", "sendmsg"])
def test_the_allowlist_is_read_the_same_way_by_the_socket_hooks(entry: str) -> None:
    """The allowlist is read the same way by `connect`, `connect_ex`, `sendto`, and
    `sendmsg`, not by `getaddrinfo` alone.

    This closes a gap a hook count alone leaves open. `_HOOKS` counts ten entry points as
    installed, and the earlier test drives seven allowlist members, but only through
    `getaddrinfo`. Narrowing the check inside `connect` to, say, `127.0.0.1` alone would
    leave every assertion in that file green while the guard refused a loopback address a
    test was entitled to use.

    This is driven on `127.0.0.1` because it is the one member every hook can actually be
    handed: the wildcards are bind addresses or a lookup-only form, not something to send a
    packet to.
    """
    with contextlib.suppress(OSError):
        _REFUSED_THROUGH[entry]("127.0.0.1")
    assert not _network_attempts, f"{entry} refused 127.0.0.1, which is on the allowlist"


def test_the_allowlist_is_the_loopback_and_the_wildcard_and_nothing_else() -> None:
    """The allowlist is written out here, so widening it takes a deliberate two-file edit."""
    written_out_here = frozenset(
        {None, "", "localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}  # noqa: S104 -- not a bind
    )
    assert written_out_here == _LOOPBACK_HOSTS, (
        "the network allowlist moved. Every entry is a host a test may reach: the loopback,\n"
        "and the wildcard spellings that mean 'listen' rather than 'dial'. Anything else on\n"
        "this list is a hole, and the test above drives whatever is on it."
    )


def test_a_unix_socketpair_is_not_touched() -> None:
    """A Unix socketpair used for asyncio's self-pipe is not something the guard touches.

    Every ``TestClient`` block builds one of these on a worker thread. They reach no host,
    so the guard, which only hooks name resolution and the send and connect calls, never
    sees them.
    """
    left, right = socket.socketpair()
    with left, right:
        left.send(b"ping")
        assert right.recv(4) == b"ping"
    assert not _network_attempts


async def test_the_guard_sits_on_the_path_the_suite_s_real_traffic_takes() -> None:
    """Drives the guard through plexapi's real request path, not through a stub.

    This is the one test here that would notice the guard being hooked somewhere real code
    does not pass through. ``PlexClient._connect`` builds a plexapi server, which sends
    requests through ``requests`` over ``urllib3`` over ``socket.getaddrinfo``, a stack
    ``respx`` does not intercept, because respx is an httpx transport.

    What comes out is ``NetworkReached``, not ``PlexError``, which is a second proof in the
    same call. ``_connect`` maps every ``Exception`` to ``PlexError`` so its callers have one
    error to handle. A guard that raised an ordinary exception would be converted right
    there, and the refusal would read as an unreachable server instead of a blocked call.
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
    """The teardown check that also catches a caller that swallows the exception.

    ``NetworkReached`` inherits from ``Exception``, which covers a normal, broad catch, and
    there are two of those on the path above. It does not cover a bare ``except:``, an
    exception swallowed on a thread nobody joins, or a future whose result is never read.
    For those cases, the attempt is recorded before it is raised, and ``_no_network`` fails
    the test in teardown on anything left in the list. That check cannot be caught at all.
    """
    with contextlib.suppress(BaseException):
        socket.getaddrinfo(UNREACHABLE, 443)
    assert _network_attempts == [
        f"tests/test_network_guard.py::{_THIS_TEST}: resolved '{UNREACHABLE}'"
    ]
    _network_attempts.clear()


@pytest.mark.parametrize(
    "address",
    [
        pytest.param(["192.0.2.1", 80], id="a-list-instead-of-a-tuple"),
        pytest.param(bytearray(b"/tmp/sock"), id="an-unhashable-path"),
        pytest.param("192.0.2.1", id="a-bare-string"),
        pytest.param(b"192.0.2.1", id="bare-bytes"),
    ],
)
def test_an_address_the_guard_cannot_parse_is_refused_rather_than_raised_through(
    address: object,
) -> None:
    """An address shape the guard does not recognize fails closed, like everything here.

    This guards two specific failure shapes. An unhashable address (`["192.0.2.1", 80]`)
    must not raise `TypeError` out of the guard itself, since a crash there reads as a bug
    in the test rather than as a blocked call. A bare `str` or `bytes` address must not be
    silently mapped to `None`, because `None` is on the allowlist, and mapping to it would
    exempt anything spelled that way from the family check.
    """
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkReached),
    ):
        sock.connect(address)  # type: ignore[arg-type]
    _network_attempts.clear()


async def test_a_refusal_on_a_worker_thread_is_blamed_on_the_test_that_started_it() -> None:
    """`_network_attempts` outlives the test, so each entry has to name its own owner.

    A refusal is written down before it is raised, and the list is read in teardown.
    Without an owner, an attempt made on a thread that outlives its test would land in the
    NEXT test's teardown, recorded against whatever test happened to be running. That would
    read as a failure in innocent code, the worst shape a guard can fail in.

    The owner is a `ContextVar`, and `asyncio.to_thread` copies the context, so the worker
    below is attributed to the test that started it, not to the thread it happens to run on.
    A bare `threading.Thread` inherits no context and reads the default owner instead, which
    is honest rather than wrong.
    """

    def dial() -> None:
        with contextlib.suppress(BaseException):
            socket.getaddrinfo(UNREACHABLE, 443)

    await asyncio.to_thread(dial)
    assert _network_attempts == [
        f"tests/test_network_guard.py::{_OWNER_TEST}: resolved '{UNREACHABLE}'"
    ]
    _network_attempts.clear()
