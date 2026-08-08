# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session-wide test configuration.

Four guarantees, applied to every test:

**Cheap Argon2.** The hasher is patched to minimal cost parameters before any test
runs. Production defaults (time_cost=3, memory_cost=65536) are intentionally slow; on a
CI runner that can add several minutes to the suite for the 100+ tests that hash or
verify a password via fixtures. The patch is safe: tests care only that authentication
accepts the right password and rejects the wrong one, not about the hash's resistance
to offline cracking.

**Cheap at-rest KDF.** The same trade for ``crypto._derive_fernet_key``, whose scrypt cost
every ``create_app`` lifespan pays once, and which 495 tests reach through a ``client``
fixture. See :func:`pytest_configure`, which is where it is applied and why.

**No developer state.** The ``_hermetic`` fixture below keeps every test off the
developer's real ``.env``/``.env.local``, whether or not the test boots the app. Without
it, any test that constructs ``Settings`` silently reads the repo-root ``.env`` (copying
real service keys into throwaway test databases), and any test that starts the app
lifespan seeds instances from ``.env.local`` and kicks off the ~280 MB IMDb dataset
download -- slow, flaky, and different from CI.

**No network.** A separate guard, because that one blocks no sockets and said it did for
long enough that seven tests were resolving a real hostname behind it. See
:class:`NetworkReached`: name resolution and connection to anything but the loopback are
refused, and the test is failed in teardown even if its own code caught the refusal.

**No real backoff.** ``asyncio.sleep`` is collapsed to a single event-loop tick before
any test runs (see below). Nothing in the suite asserts on real elapsed time, so paying
the real delay for a client retry or a poll loop only burns wall clock for no signal.
"""

import asyncio
import logging
import socket
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, NoReturn

import pytest
import structlog
from fastapi.testclient import TestClient
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from sqlalchemy import Engine
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Private, because structlog exposes no public name for the proxy ``get_logger`` returns
# and no public way to un-freeze one. ``tests/test_capturable_loggers.py`` drives the real
# freezing path, so an upgrade that renames or re-implements this fails there rather than
# turning the guard below into a silent no-op.
from structlog._config import BoundLoggerLazyProxy

import reaper.auth.passwords as _passwords
from reaper import crypto, logbuffer
from reaper.auth.ratelimit import (
    argon2_gate,
    login_throttle,
    password_throttle,
    recover_throttle,
)
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.session import create_engine as create_async_engine
from reaper.db.session import create_session_factory
from reaper.logging import _NOISY_LOGGERS
from reaper.main import create_app
from tests._auth import login

_passwords._hasher = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8, parallelism=1),))

_real_derive_fernet_key = crypto._derive_fernet_key

#: Every scrypt cost ``crypto`` declares, each mapped to its own small one. Built from the
#: declaration rather than transcribed, so raising ``_SCRYPT_N`` extends this by itself and
#: retiring a cost drops it. Distinct in, distinct out is the whole property here: the
#: compatibility suite is made of "data written at cost A still opens", which says nothing at
#: all once A and B derive the same key.
_CHEAP_SCRYPT_N = {
    n: 2 ** (2 + i) for i, n in enumerate(sorted({crypto._SCRYPT_N, *crypto._SUPERSEDED_SCRYPT_N}))
}


def _cheap_derive_fernet_key(secret: str, salt: bytes, n: int = crypto._SCRYPT_N) -> bytes:
    """Stand in for ``crypto._derive_fernet_key`` at a cost no test reads."""
    cheap = _CHEAP_SCRYPT_N.get(n)
    if cheap is None:
        raise AssertionError(
            f"scrypt cost {n} is not one crypto.py declares, so there is no cheap cost here "
            "that is guaranteed distinct from the others. Register it in "
            "_SUPERSEDED_SCRYPT_N, or call the real derivation."
        )
    return _real_derive_fernet_key(secret, salt, cheap)


def pytest_configure() -> None:
    """Cheapen the at-rest KDF for the session, without collapsing what it distinguishes.

    ``crypto._SCRYPT_N`` is 64 MiB and about 124ms per derivation, deliberately: the threat it
    answers is an offline attacker holding a leaked database. Every ``create_app`` lifespan
    derives one, and 495 tests take a ``client`` fixture, so production cost here is about 30%
    of this suite's wall clock and buys no assertion anywhere.

    **The constant is not patched, the function is.** ``test_kdf_and_session_upkeep.py`` reads
    ``_SCRYPT_N`` and ``_SUPERSEDED_SCRYPT_N`` directly -- it pins that new data is written
    under the highest cost, and five of its boxes are built at a hardcoded old one -- so moving
    either constant breaks the suite that exists to prove nothing was stranded.

    **And the mapping is injective, because that suite cannot tell if it is not.** Every
    compatibility test there reads "written at cost A, opens today"; under a wrapper sending
    every cost to one key they all hold vacuously, measured at 30 passing with four proving
    nothing. ``test_the_cheap_kdf_derives_a_different_key_per_cost`` is what carries that
    instead (rule 145).

    In ``pytest_configure`` rather than at module level, so ``import tests.conftest`` cannot
    apply it either. It cannot reach production by any route: ``tests/`` is outside the wheel,
    there is no root ``conftest.py``, and nothing in ``src/`` imports this file.

    The network guard below installs here for the same reason, plus one of its own: a
    session-scoped fixture is set up before any function-scoped one, so a guard that were a
    fixture would not be watching while such a fixture booted an app.
    """
    crypto._derive_fernet_key = _cheap_derive_fernet_key
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.socket.connect = _guarded_connect  # type: ignore[assignment]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[assignment]


# --------------------------------------------------------------------------------------
# No network
#
# Measured over the whole suite before this landed, which is the population the guard is
# reconciled against rather than a shape it was assumed to have:
#
#   2,023 socketpair() calls and the 4,046 AF_UNIX sockets they are made of, across 1,674
#   tests -- every one asyncio's loop self-pipe, which TestClient builds per ``with`` block
#   on a worker thread. A guard that refuses sockets rather than destinations breaks all of
#   them, which is why this one hooks neither ``socket()`` nor ``bind()``.
#
#   4 AF_INET sockets and 2 connects, all loopback, all in test_launcher's TestLoopbackGuard,
#   which listens on a port and connects to it to prove the port-in-use check reads it.
#
#   9 getaddrinfo calls: 2 loopback from that same pair of tests, and 7 to a real public
#   hostname, from seven tests whose Plex link flow ends in a library autosync that goes over
#   plexapi. respx mocks httpx; plexapi is ``requests``, so those seven really resolved a name
#   over the network on every run, and their failure was swallowed on purpose.
#
# So getaddrinfo is where a hook sees anything at all, and connect is here for the case DNS
# never reaches: a literal address, which ``socket.connect`` takes without resolving.
# --------------------------------------------------------------------------------------

#: Every host a test may reach. `None` and `""` are what a wildcard bind resolves, `0.0.0.0`
#: and `::` are the wildcard itself: all four are a listener, which reaches nobody.
_LOOPBACK_HOSTS = frozenset(
    {None, "", "localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}  # noqa: S104 -- not a bind
)

#: Every attempt the guard refused during the current test. Read in teardown, because the
#: raise alone is not enough -- see `NetworkReached`.
_network_attempts: list[str] = []

_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


class NetworkReached(BaseException):
    """A test reached for the network.

    **Deliberately off ``Exception``**, which is the whole reason this guard works. The code
    it fires inside catches broadly and on purpose: ``PlexClient._connect`` maps every failure
    to ``PlexError`` so a caller has one error to handle, and ``_sync_libraries_after_link``
    swallows every exception because its docstring promises the sign-in never strands on a
    failed library refresh. Both are right. But a guard raising an ordinary exception into
    them is caught, logged, and the test goes green having reached the network anyway -- which
    is exactly how seven tests spent a DNS round trip each without anyone noticing.

    The teardown assertion below is the second half, for the case something catches
    ``BaseException`` or the attempt happens on a thread whose exception nobody re-raises.
    """


def _refuse(what: str, target: object) -> NoReturn:
    attempt = f"{what} {target!r}"
    _network_attempts.append(attempt)
    raise NetworkReached(
        f"a test reached the network: {attempt}\n"
        "Tests are hermetic. Mock the client (respx for httpx) or stub the seam -- and note "
        "that respx does NOT cover plexapi, which speaks requests."
    )


def _host_of(address: object) -> object:
    """The host half of a socket address, for the shapes ``connect`` accepts.

    AF_INET is ``(host, port)`` and AF_INET6 is ``(host, port, flowinfo, scope_id)``; an
    AF_UNIX address is a path, as a `str` or `bytes`, and reaches no network at all.
    """
    if isinstance(address, tuple) and address:
        return address[0]
    return None if isinstance(address, (str, bytes)) else address


def _guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    if host not in _LOOPBACK_HOSTS:
        _refuse("resolved", host)
    return _real_getaddrinfo(host, port, *args, **kwargs)


def _guarded_connect(self: socket.socket, address: Any) -> None:
    if self.family != socket.AF_UNIX and _host_of(address) not in _LOOPBACK_HOSTS:
        _refuse("connected to", address)
    return _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    if self.family != socket.AF_UNIX and _host_of(address) not in _LOOPBACK_HOSTS:
        _refuse("connected to", address)
    return _real_connect_ex(self, address)


@pytest.fixture(autouse=True)
def _no_network() -> Iterator[None]:
    """Fail the test that reached out, even if its own code swallowed the refusal."""
    _network_attempts.clear()
    yield
    attempts = list(_network_attempts)
    _network_attempts.clear()
    assert not attempts, (
        "this test reached the network:\n"
        + "\n".join(f"  {a}" for a in attempts)
        + "\n\nIt was refused, so nothing left the machine. This runs in teardown so that a "
        "refusal\nsomething caught on the way out still fails the test rather than being "
        "logged and\nforgotten. Mock the client or stub the seam: respx covers httpx and does "
        "NOT cover\nplexapi, which speaks requests."
    )


_real_async_sleep = asyncio.sleep


async def _instant_async_sleep(delay: float, result: object = None) -> object:
    """Stand in for ``asyncio.sleep`` everywhere: real duration, one real tick.

    Production code really waits out retry backoff (``clients/base.py``'s ``@retry``),
    the plex.tv pin-poll loop (``clients/plextv.py``'s ``wait_for_pin``), and Discord's
    ``Retry-After``. A test that provokes two retries pays ~1.5s of pure idle time for a
    delay no assertion reads. Still awaits the real ``sleep(0)`` (rather than returning
    immediately) so code that relies on ``asyncio.sleep`` to yield to the event loop keeps
    working unchanged.

    **It does not move the clock.** ``loop.time()`` still advances in real wall-clock, so a
    loop bounded by a DEADLINE rather than by a sleep count does not finish early here -- it
    spins through the whole remaining window as fast as it can. A test of such a deadline
    takes the ``slept`` fixture below and drives the clock itself; passing a short real
    timeout instead measures the machine (rule 133).
    """
    return await _real_async_sleep(0, result)


asyncio.sleep = _instant_async_sleep  # type: ignore[assignment]


@pytest.fixture
async def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every delay the code under test asked for, in order, on a clock the test owns.

    Sleeping is already instant (above); this writes the delay down and moves ``loop.time()``
    forward by exactly that much. A loop bounded by a deadline is then driven by the sleeps
    the test provoked, and the recorded delays are what the assertions read: what was slept
    is the behavior, where elapsed wall clock is only evidence about the machine.

    Both halves fix a real defect in the pin-poll tests (#346). A real window is a race the
    loop can lose -- a fifth of a second held ~1,073 polls on an idle machine and 11 under
    load, a ninetyfold swing with nothing about the code changed. And elapsed time could not
    fail: an unclipped ``sleep(86400)`` is instant too, so a test asserting it returned
    quickly passed with the deadline interlock deleted (rule 118).

    The clock starts on a whole second so every deadline sum stays exact in binary. The loop
    under test leaves at ``now == deadline``, and an ulp of drift buys a spurious extra poll.
    """
    recorded: list[float] = []
    instant = asyncio.sleep
    loop = asyncio.get_running_loop()
    now = float(int(loop.time()))

    async def _record(delay: float, result: object = None) -> object:
        nonlocal now
        recorded.append(delay)
        now += delay
        return await instant(0, result)

    monkeypatch.setattr(asyncio, "sleep", _record)
    monkeypatch.setattr(loop, "time", lambda: now)
    return recorded


async def _no_catch_up(*_args: object, **_kwargs: object) -> None:
    return None


def uncache_module_loggers() -> None:
    """Thaw every module logger frozen by an earlier test. See :func:`_capturable_logs`.

    structlog freezes by assigning ``finalized_bind`` onto the proxy as an INSTANCE
    attribute, shadowing the class ``bind`` that would otherwise re-read the current
    configuration. Deleting that attribute is the whole thaw: the next call falls back to
    the class method and sees whatever ``capture_logs`` has installed.

    Every logger in ``src/`` is a module-level ``log = structlog.get_logger(__name__)``,
    so walking the loaded ``reaper`` modules reaches all of them --
    ``tests/test_capturable_loggers.py`` pins that against the source tree, because a
    logger built any other way would drop out of this walk without a word.
    """
    for name, module in list(sys.modules.items()):
        if module is None or (name != "reaper" and not name.startswith("reaper.")):
            continue
        for value in vars(module).values():
            if isinstance(value, BoundLoggerLazyProxy):
                value.__dict__.pop("bind", None)


@pytest.fixture(autouse=True)
def _capturable_logs() -> None:
    """Keep ``structlog.testing.capture_logs`` working across the whole suite.

    ``configure_logging`` (called by ``test_foundations`` and by every ``create_app``
    boot) sets ``cache_logger_on_first_use=True``. The first time a module logger is used
    while that flag is live, structlog PERMANENTLY replaces that logger proxy's ``bind``
    with a closure holding the configuration of that moment -- after which
    ``reset_defaults`` will not undo it. Clearing the flag before each test keeps
    capturable loggers from ever freezing. Tests that assert on ``configure_logging``
    itself call it inside their own body, so this starting state does not affect them.

    Clearing the flag is only half of it, and the missing half was a test failing for a
    reason having nothing to do with what it tested (#481). A test that boots an app sets
    the flag INSIDE its own body and then logs, so a logger freezes while this fixture is
    not looking, and no later clearing of the flag thaws it. It stays capturable for a
    while, because ``capture_logs`` mutates the configured processor list in place; the
    frozen logger goes deaf only once a LATER boot installs a fresh list, which the next
    ``create_app`` does. Which tests share a worker is decided by timing under ``-n auto``,
    so whether a later ``capture_logs`` assertion saw its events came down to scheduling.
    So thaw as well as prevent: afterwards this guard holds whatever an earlier test did.
    """
    structlog.configure(cache_logger_on_first_use=False)
    uncache_module_loggers()


@pytest.fixture(autouse=True)
def _no_file_sink_left_behind() -> Iterator[None]:
    """Rule 133: no test leaves the log file sink pointing into its own ``tmp_path``.

    ``configure_file_logging`` sets three module globals, and every ``create_app`` boot
    reaches it through ``configure_logging(data_dir)`` -- so hundreds of tests open a sink
    and none of them owned the teardown. Two log modules cleaned up after themselves; what
    neither could do is clean up before the test that runs next on the worker.

    Two things then ride on the ordering. A sink left open on a temp dir takes every later
    append anywhere in the suite, and one failed write flips the one-shot degraded flag for
    everything after it. And ``log_files()`` answers from the leftover ``_log_path``, which
    is how ``test_an_unwritable_data_dir_degrades_to_no_files`` went red in CI on an
    unrelated branch: setup on an unwritable dir swallows the error and returns with the
    previous sink deliberately untouched, so "no files" held only when the previous sink was
    not a neighbor's.
    """
    yield
    with logbuffer._file_lock:
        sink, logbuffer._file_sink = logbuffer._file_sink, None
        logbuffer._log_path = None
        # A degradation test flips this process-global; reset it so the next test starts healthy.
        logbuffer._file_sink_healthy = True
    if sink is not None:
        sink.close()


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    """Save and restore everything ``configure_logging`` / ``logbuffer.set_level`` mutate.

    Opt-in, because it is only needed by the handful of tests that call those functions in
    their own body -- but for those it is not optional. ``configure_logging`` is entirely
    process-global: it sets the root level via ``basicConfig``, attaches a ring handler to
    the root logger, lowers every noisy library logger, and re-runs ``structlog.configure``.
    All of it outlives the test and lands on whatever the xdist worker picks up next.

    Shared here rather than copied per module: it lived in ``test_logging_quiet`` while its
    direct sibling in ``test_foundations`` called ``configure_logging`` with no cleanup at
    all, which is exactly the shape a shared fixture prevents.
    """
    root = logging.getLogger()
    saved_root = root.level
    saved_handlers = list(root.handlers)
    saved = {name: logging.getLogger(name).level for name in _NOISY_LOGGERS}
    saved_ring = logbuffer.level_name()
    try:
        yield
    finally:
        # Order matters, and it used to be wrong: ``logbuffer.set_level`` sets the ROOT
        # logger's level as well as the ring's, so restoring the ring last silently
        # re-clobbered the root level this fixture had just put back. Ring first, root last.
        logbuffer.set_level(saved_ring)
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_root)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test is hermetic: no ``.env`` reads, no startup seeding.

    Not the network, which this fixture never touched however it read (rule 7/24). The
    ``_no_network`` guard above is what holds that, and it is a separate mechanism because
    this one is about what the app is *configured* from and that one about where a socket is
    allowed to go.

    * ``Settings`` never reads the developer's dotenv files -- ``env_file`` is cleared
      for the duration of the test, so ``Settings(data_dir=tmp_path, ...)`` gets exactly
      the fields the test passes (and real environment variables, which CI controls).
    * The app lifespan's instance seeding (``load_raw_env``) and startup catch-up (the
      IMDb dataset download, scheduler catch-up) are stubbed out, exactly as the
      settings-API tests always did locally. Tests that exercise the real functions
      import them from their own modules, which this does not touch.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setattr("reaper.main.load_raw_env", lambda _s: {})
    monkeypatch.setattr("reaper.main.catch_up_on_startup", _no_catch_up)
    # The auth throttles and the Argon2 gate are process-global singletons; a lockout
    # provoked by one test (every TestClient shares the same client address) must never
    # bleed into the next.
    login_throttle.reset()
    recover_throttle.reset()
    password_throttle.reset()
    argon2_gate.reset()


# --------------------------------------------------------------------------------------
# Booting a throwaway install
#
# Four fixtures, layered, because the boot preamble was written by hand in 25 files: the
# sync boot 44 times, the async boot 27, and ``TestClient(create_app(...)) + login(...)``
# 32, seven of them byte for byte. What varies between those files is what they SEED, and
# that is what a file-local ``client`` should be left holding -- so these take the boot and
# nothing else, and a file with its own seeding overrides ``client`` and asks for
# ``settings`` or ``sync_db`` instead of rewriting the four lines above it.
#
# **Every one of them is function-scoped, and that is load-bearing rather than a default.**
# ``_hermetic`` above is function-scoped and takes a function-scoped ``monkeypatch``, so
# anything higher-scoped is set up BEFORE it: measured, such a fixture sees the real
# ``catch_up_on_startup`` and ``env_file`` still pointing at the developer's dotenv files,
# and an app booted there reads their credentials and starts the IMDb download (rule 37).
# The one fixture in the suite that is session-scoped, ``test_openapi_tags.schema``, applies
# those three patches itself for exactly this reason.
# --------------------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A throwaway install rooted at ``tmp_path``, with its schema already created.

    The schema is made here rather than by whatever opens the database first, because the
    app's own lifespan reads it on the way up -- it seeds instances and checks a local admin
    exists -- so a ``create_app`` against an empty file fails before any test body runs.
    """
    resolved = Settings(data_dir=tmp_path, secret_key="test-key")
    engine = sa_create_engine(resolved.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return resolved


@pytest.fixture
def sync_db(settings: Settings) -> Iterator[Engine]:
    """A sync engine on that install, for a test that seeds rows before the app boots."""
    engine = sa_create_engine(settings.sync_database_url)
    yield engine
    engine.dispose()


@pytest.fixture
async def async_factory(settings: Settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """The app's own session factory on that install, built the way production builds it.

    ``create_session_factory`` sets ``expire_on_commit=False``, so a row read back through
    the session that wrote it comes out of the identity map without touching the database.
    A test asserting on durable state opens a SECOND session from this factory (#340).
    """
    engine = create_async_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A signed-in client on that install, with the CSRF header defaulted.

    Signed in, because the API is behind the auth gate and almost every test that reaches a
    route only cares that it is through. A test about the gate itself builds its own client
    on ``settings`` and stays out.
    """
    with TestClient(create_app(settings)) as booted:
        login(booted, settings)
        yield booted
