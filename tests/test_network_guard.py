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

#: Every exit the guard is installed on, each driven below. `getaddrinfo` is where all of this
#: suite's outbound traffic actually goes; the five siblings answer the same question through
#: the same resolver and every one of them escaped the first version of this guard; `connect`
#: and `connect_ex` are for the address that never needs resolving; `sendto` and `sendmsg` are
#: for UDP, which reaches a host with neither a lookup nor a connect and really did put bytes
#: on the wire while unhooked.
#:
#: **A count of hooks INSTALLED is not a count of what is covered**, which is why
#: `_REFUSED_THROUGH` and `_ALLOWED_THROUGH` exist: every entry point that reads the allowlist
#: is driven with a refused host AND an allowed one, so narrowing the check inside any one of
#: them cannot leave this file green (rule 145).
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
    """It goes on in ``pytest_configure``, and the scoping is the reason.

    A fixture cannot cover this. ``_hermetic`` is function-scoped, so anything higher-scoped
    is set up before it -- and the one session-scoped fixture in the suite boots an app, which
    is exactly the setup a network guard would want to be watching. Installing at configure
    time means there is no window at all.
    """
    installed = _hook_state()
    assert sum(installed.values()) == _HOOKS, (
        f"{sum(installed.values())} of {_HOOKS} network hooks are installed:\n"
        + "\n".join(f"  {name}: {on}" for name, on in sorted(installed.items()))
        + "\n\nAn uninstalled hook refuses nothing, and every assertion in this file that does\n"
        "not drive that particular call still passes."
    )


#: Every entry point that refuses a host, as (label, call). Driven against an address that must
#: be refused, and again against each allowlist member, which is what covers the case a hook
#: count cannot: a check narrowed inside one of them while the others stay right.
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

    `getfqdn` is the reason this is a sweep rather than one call: it swallows its own errors
    and returns the name unchanged, so it would have gone on answering forever without ever
    looking like a failure. All five resolver siblings escaped the first version of this guard,
    and `sendto` put five bytes on the wire, because only `getaddrinfo` and the two `connect`
    forms were hooked (rule 72 -- the siblings of the thing you fixed).
    """
    with pytest.raises(NetworkReached):
        _REFUSED_THROUGH[entry](UNREACHABLE_IP)
    _network_attempts.clear()


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


@pytest.mark.parametrize("entry", ["connect", "connect_ex", "sendto", "sendmsg"])
def test_the_allowlist_is_read_the_same_way_by_the_socket_hooks(entry: str) -> None:
    """The allowlist reaches `connect`, `connect_ex` and `sendto`, not `getaddrinfo` alone.

    This is the gap a hook count leaves open. `_HOOKS` says nine entry points are installed and
    the test above drives seven allowlist members, but only through `getaddrinfo` -- so
    narrowing the check inside `connect` to, say, `127.0.0.1` alone left every assertion in
    this file green while the guard refused a loopback address a test was entitled to use.

    Driven on `127.0.0.1` because it is the one member every hook can actually be handed: the
    wildcards are bind addresses and a lookup form, not somewhere to send a packet.
    """
    with contextlib.suppress(OSError):
        _REFUSED_THROUGH[entry]("127.0.0.1")
    assert not _network_attempts, f"{entry} refused 127.0.0.1, which is on the allowlist"


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

    Two ways this went the other way. An unhashable address (`["192.0.2.1", 80]`) raised
    `TypeError` straight out of the guard -- a crash where a refusal belongs, and one that
    reads as a bug in the test rather than as a blocked call. And a bare `str`/`bytes` was
    mapped to `None`, which is on the allowlist: a second, silent AF_UNIX exemption stacked
    on the explicit family check, allowing anything spelled that way whatever its family.
    """
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkReached),
    ):
        sock.connect(address)  # type: ignore[arg-type]
    _network_attempts.clear()


async def test_a_refusal_on_a_worker_thread_is_blamed_on_the_test_that_started_it() -> None:
    """`_network_attempts` outlives the test, so what it records has to name an owner.

    A refusal is written down before it is raised, and the list is read in teardown -- so an
    attempt made on a thread that outlives its test lands in the NEXT test's teardown. Recorded
    against whatever test was running at the time, that reads as a red run pointing at innocent
    code, which is the worst shape a guard can fail in.

    The owner is a `ContextVar`, and `asyncio.to_thread` copies the context, so the worker below
    is attributed here rather than to the thread it happens to run on. A bare `threading.Thread`
    inherits no context and reads the default instead, which is honest rather than wrong.
    """

    def dial() -> None:
        with contextlib.suppress(BaseException):
            socket.getaddrinfo(UNREACHABLE, 443)

    await asyncio.to_thread(dial)
    assert _network_attempts == [
        f"tests/test_network_guard.py::{_OWNER_TEST}: resolved '{UNREACHABLE}'"
    ]
    _network_attempts.clear()
