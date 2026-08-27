# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session-wide test configuration.

Every test gets four guarantees:

**Cheap Argon2.** The hasher runs at minimal cost before any test runs, instead of the slow
production settings. Tests only check that the right password passes and the wrong one fails,
so the weaker hash costs nothing in coverage and saves most of the suite's wall-clock time.

**Cheap at-rest KDF.** ``crypto._derive_fernet_key`` gets the same trade, since every
``create_app`` lifespan runs it once. See :func:`pytest_configure` for where and why.

**No developer state.** The ``_hermetic`` fixture below keeps every test off the developer's
real ``.env``/``.env.local``, whether or not the test boots the app. Without it, building a
``Settings`` reads the repo-root ``.env`` and copies real service keys into throwaway test
databases, and booting the app lifespan seeds instances from ``.env.local`` and starts the
IMDb dataset download.

**No network.** :class:`NetworkReached` refuses name resolution and connection to anything but
loopback, and fails the test in teardown even if the test's own code caught the refusal.

**No real backoff.** ``asyncio.sleep`` collapses to a single event-loop tick before any test
runs (see below). Nothing in the suite asserts on real elapsed time, so paying a client
retry's or poll loop's real delay burns wall clock for no signal.
"""

import asyncio
import contextvars
import logging
import shutil
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

# structlog has no public name for the proxy ``get_logger`` returns, and no public way to
# un-freeze one, so this imports the private one. ``tests/test_capturable_loggers.py`` drives
# the real freezing path, so a structlog upgrade that changes this breaks that test instead of
# silently turning the guard below into a no-op.
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

#: Maps every scrypt cost ``crypto`` declares to its own small cost, built from the declaration
#: instead of a hardcoded list, so raising ``_SCRYPT_N`` picks it up automatically. Each real
#: cost must map to a different cheap cost. The compatibility tests check that data written at
#: one cost still opens today, and that proves nothing if two costs derive the same key.
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
    """Make the at-rest KDF cheap for the whole test session, without losing what it must prove.

    Production's scrypt cost is deliberately expensive. It defends against an offline attacker
    who steals the database. Every ``create_app`` lifespan derives a key with it, so patching
    this function instead of skipping it keeps the real code path while removing its cost.

    The function is patched, not the constant. ``test_kdf_and_session_upkeep.py`` reads
    ``_SCRYPT_N`` and ``_SUPERSEDED_SCRYPT_N`` directly. It checks that new data is written
    under the highest cost, and some of its fixtures are built at a hardcoded old cost. Changing
    either constant would break that test.

    Each real cost must map to a different cheap cost. The compatibility tests there check that
    data written at one cost still opens today. If two costs mapped to the same key, those
    checks would pass without proving anything.
    ``test_the_cheap_kdf_derives_a_different_key_per_cost`` is the test that catches that case.

    This patch runs in ``pytest_configure`` rather than at module level, so importing this
    module never applies it by itself. It cannot reach production: ``tests/`` ships outside the
    wheel, there is no root ``conftest.py``, and nothing in ``src/`` imports this file.

    The network guard below is installed here too, for its own reason. Pytest sets up a
    session-scoped fixture before any function-scoped one, so if the guard were a fixture, it
    would miss an app that a session-scoped fixture boots.
    """
    crypto._derive_fernet_key = _cheap_derive_fernet_key
    socket.getaddrinfo = _guarded_getaddrinfo
    for name in _real_resolvers:
        setattr(socket, name, _guarded_resolver(name))
    socket.socket.connect = _guarded_connect  # type: ignore[assignment]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[assignment]
    socket.socket.sendto = _guarded_sendto  # type: ignore[assignment]
    socket.socket.sendmsg = _guarded_sendmsg  # type: ignore[assignment]


# --------------------------------------------------------------------------------------
# No network
#
# The guard hooks ``getaddrinfo`` and ``connect``, not ``socket()`` or ``bind()``. The suite
# opens many local AF_UNIX and loopback sockets, including asyncio's loop self-pipe (which
# TestClient builds per ``with`` block on a worker thread) and the loopback probes in
# test_launcher's TestLoopbackGuard. A guard that refused sockets outright would break all of
# them.
#
# getaddrinfo is where a hostname lookup is visible at all, and connect covers a literal
# address that never goes through a lookup.
# --------------------------------------------------------------------------------------

#: Every host a test may reach. A wildcard bind resolves to `None` or `""`, and `0.0.0.0` and
#: `::` are the wildcard address itself. All four describe a listener, not a destination one
#: could connect to.
_LOOPBACK_HOSTS = frozenset(
    {None, "", "localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}  # noqa: S104 -- not a bind
)

#: Every attempt the guard refused during the current test. Teardown reads this list because
#: raising alone is not enough. See `NetworkReached`.
_network_attempts: list[str] = []

#: Which test a refusal belongs to. This uses a `ContextVar` instead of a plain global because a
#: refusal can happen on a thread. `asyncio.to_thread` copies the context, so a worker is
#: attributed to the test that spawned it. A bare `threading.Thread` starts with an empty
#: context and reads the default value instead, which is also correct. A plain global would
#: read whichever test is running now, so a thread that outlives its own test would report a
#: failure against a different, innocent test.
_owner: contextvars.ContextVar[str] = contextvars.ContextVar("owner", default="<collection>")

_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_sendto = socket.socket.sendto
_real_sendmsg = socket.socket.sendmsg
#: The resolver siblings. Each one answers the same question `getaddrinfo` does and reaches the
#: same underlying resolver, so guarding only `getaddrinfo` and leaving these unguarded would
#: let a test resolve a real hostname through one of them instead.
_real_resolvers = {
    name: getattr(socket, name)
    for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo", "getfqdn")
}


class NetworkReached(BaseException):
    """A test reached the network.

    This subclasses ``BaseException`` instead of ``Exception``, which is what makes the guard
    work. Code under test catches broadly on purpose. ``PlexClient._connect`` maps every
    failure to ``PlexError`` so a caller has one error to handle, and
    ``_sync_libraries_after_link`` catches every exception because its docstring promises the
    sign-in never fails on a broken library refresh. Both are right to do that. But an ordinary
    exception raised into either one would be caught, logged, and let the test pass green even
    though it reached the network.

    The teardown assertion below covers the other case. Something might catch
    ``BaseException`` itself, or the attempt might happen on a thread whose exception nobody
    re-raises.

    This guard has three blind spots, found by driving each one directly.
    ``_socket.socket.connect`` is the C base class, so code that reaches it directly bypasses
    the patch (nothing in the tree does today). A raw ``SOCK_RAW``/``AF_PACKET`` socket needs
    root and is out of scope. And a subprocess runs in its own address space, so a test that
    shells out is unguarded. ``test_launcher.py`` patches ``subprocess.run`` and launches
    nothing, and ``test_repo_hygiene.py`` runs ``git`` against the checkout on disk. Neither one
    reaches the network, but a future test that shells out and does would go unseen here.
    """


def _refuse(what: str, target: object) -> NoReturn:
    attempt = f"{what} {target!r}"
    _network_attempts.append(f"{_owner.get()}: {attempt}")
    raise NetworkReached(
        f"a test reached the network: {attempt}\n"
        "Tests are hermetic. Mock the client (respx for httpx) or stub the seam -- and note "
        "that respx does NOT cover plexapi, which speaks requests."
    )


def _host_of(address: object) -> object:
    """The host part of a socket address, for every shape ``connect`` accepts.

    AF_INET is ``(host, port)`` and AF_INET6 is ``(host, port, flowinfo, scope_id)``. Any other
    shape is returned as-is and refused, since an address this function does not recognize must
    fail closed rather than be let through.
    """
    if isinstance(address, tuple) and address:
        return address[0]
    return address


def _is_allowed(host: object) -> bool:
    """Whether ``host`` is one of the addresses a test may reach.

    This checks exact membership only, never a prefix or a parsed value, so every loopback
    spelling outside the seven exact strings is refused. That includes real loopback addresses
    like `127.0.0.2`, `127.1`, `2130706433`, `[::1]`, `LOCALHOST`, a trailing-dot `localhost.`,
    and `b"127.0.0.1"`. A test that needs one of those adds it to the allowlist above, on
    purpose.
    """
    try:
        return host in _LOOPBACK_HOSTS
    except TypeError:  # an unhashable address is not on any allowlist
        return False


def _guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    if not _is_allowed(host):
        _refuse("resolved", host)
    return _real_getaddrinfo(host, port, *args, **kwargs)


def _guarded_resolver(name: str) -> Any:
    """One of `getaddrinfo`'s siblings, guarded on its first argument."""
    real = _real_resolvers[name]

    def guarded(host: Any = None, *args: Any, **kwargs: Any) -> Any:
        if not _is_allowed(_host_of(host)):
            _refuse(f"resolved via {name}", host)
        return real(host, *args, **kwargs) if host is not None else real()

    return guarded


def _guarded_connect(self: socket.socket, address: Any) -> None:
    if self.family != socket.AF_UNIX and not _is_allowed(_host_of(address)):
        _refuse("connected to", address)
    return _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    if self.family != socket.AF_UNIX and not _is_allowed(_host_of(address)):
        _refuse("connected to", address)
    return _real_connect_ex(self, address)


def _guarded_sendto(self: socket.socket, *args: Any) -> int:
    # UDP sends to a host with no connect and no address lookup, so this is the only place this
    # guard can see the destination.
    if self.family != socket.AF_UNIX and args and not _is_allowed(_host_of(args[-1])):
        _refuse("sent to", args[-1])
    return _real_sendto(self, *args)


def _guarded_sendmsg(self: socket.socket, *args: Any) -> int:
    if self.family != socket.AF_UNIX and len(args) > 3 and not _is_allowed(_host_of(args[3])):
        _refuse("sent to", args[3])
    return _real_sendmsg(self, *args)


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail the test that reached the network, even if its own code caught the refusal.

    This never clears the attempt list on the way in. A refusal during collection, or on a
    thread that outlives the test that started it, has no teardown of its own to read it, so
    the next test's teardown reports it instead. Each line names the test it actually belongs
    to, not whichever test happens to be running when it is read.
    """
    _owner.set(request.node.nodeid)
    yield
    attempts, _network_attempts[:] = list(_network_attempts), []
    mine = request.node.nodeid
    assert not attempts, (
        "the network was reached:\n"
        + "\n".join(
            f"  {a}" if not a.startswith(f"{mine}: ") else f"  {a[len(mine) + 2 :]}"
            for a in attempts
        )
        + "\n\nIt was refused, so nothing left the machine. This runs in teardown so that a "
        "refusal\nsomething caught on the way out still fails the test rather than being "
        "logged and\nforgotten -- so a line above naming ANOTHER test is that test's fault, "
        "reported here\nbecause its own teardown had already run. Mock the client or stub the "
        "seam: respx\ncovers httpx and does NOT cover plexapi, which speaks requests."
    )


_real_async_sleep = asyncio.sleep


async def _instant_async_sleep(delay: float, result: object = None) -> object:
    """Replace ``asyncio.sleep`` everywhere, so a delay of any length takes one real tick.

    Production code really waits out retry backoff (``clients/base.py``'s ``@retry``), the
    plex.tv pin-poll loop (``clients/plextv.py``'s ``wait_for_pin``), and Discord's
    ``Retry-After``. This skips that wait for delays no assertion reads. It still awaits a real
    ``sleep(0)`` instead of returning immediately, so code that relies on ``asyncio.sleep`` to
    yield to the event loop keeps working.

    This does not move the clock. ``loop.time()`` still advances in real wall-clock time, so a
    loop bounded by a deadline, rather than by a count of sleeps, still spins through the whole
    window as fast as it can. A test of a deadline like that should use the ``slept`` fixture
    below, which drives the clock itself. A short real timeout instead measures the speed of the
    machine running the test, not the code.
    """
    return await _real_async_sleep(0, result)


asyncio.sleep = _instant_async_sleep  # type: ignore[assignment]


@pytest.fixture
async def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every delay the code under test asked for, in order, on a clock this fixture owns.

    Sleeping is already instant (above). This also records each delay and moves
    ``loop.time()`` forward by that amount, so a loop bounded by a deadline runs through the
    same sequence of sleeps a real clock would drive it through. Assertions read the recorded
    delays, not elapsed wall-clock time. A real deadline loop keeps spinning even when sleep is
    instant, so a test that measures how fast it returned would still pass with the deadline
    check removed.

    The clock starts on a whole second so every deadline sum stays exact in binary. The loop
    under test exits at ``now == deadline``, and even a small drift below that would cost it
    one extra poll.
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

    structlog freezes a logger by assigning ``finalized_bind`` onto its proxy as an instance
    attribute, which shadows the class ``bind`` method that would otherwise re-read the
    current configuration. Deleting that attribute undoes the freeze. The next call falls back
    to the class method and picks up whatever ``capture_logs`` installed.

    Every logger in ``src/`` is a module-level ``log = structlog.get_logger(__name__)``, so
    walking the loaded ``reaper`` modules reaches all of them.
    ``tests/test_capturable_loggers.py`` checks that against the source tree, because a logger
    built any other way would silently drop out of this walk.
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

    ``configure_logging`` (called by ``test_foundations`` and by every ``create_app`` boot)
    sets ``cache_logger_on_first_use=True``. The first time a module logger is used while that
    flag is set, structlog permanently replaces that logger proxy's ``bind`` with a closure
    holding the configuration from that moment, and ``reset_defaults`` cannot undo it. Clearing
    the flag before each test stops loggers from freezing in the first place. A test that
    asserts on ``configure_logging`` itself calls it inside its own body, so this starting
    state does not affect it.

    Clearing the flag before each test is not enough on its own. A test that boots an app sets
    the flag inside its own body and then logs, so a logger can freeze while this fixture has
    already run. That logger stays frozen because ``capture_logs`` mutates the configured
    processor list in place, and a frozen logger only reads a fresh list again once a later
    boot installs one. Which tests share a worker depends on scheduling, so whether a later
    ``capture_logs`` assertion sees its events would depend on test order. This fixture undoes
    the freeze as well as preventing it, so it always starts from a clean state regardless of
    what the previous test did.
    """
    structlog.configure(cache_logger_on_first_use=False)
    uncache_module_loggers()


@pytest.fixture(autouse=True)
def _no_file_sink_left_behind() -> Iterator[None]:
    """No test leaves the log file sink pointing into its own ``tmp_path``.

    ``configure_file_logging`` sets three module globals, and every ``create_app`` boot
    reaches it through ``configure_logging(data_dir)``, so hundreds of tests open a sink with
    nothing else owning its teardown. This fixture is that teardown, run before the next test
    on the same worker.

    Two things depend on the ordering. A sink left open on a deleted temp dir takes every
    later log write in the suite, and one failed write there flips the one-shot degraded flag
    for every test that runs after it. ``log_files()`` also reads its answer from the leftover
    ``_log_path``, so a test expecting "no files" on an unwritable data dir only sees that if
    the previous sink was already cleared here.
    """
    yield
    with logbuffer._file_lock:
        sink, logbuffer._file_sink = logbuffer._file_sink, None
        logbuffer._log_path = None
        # A degradation test flips this process-global. Reset it here so the next test starts
        # healthy.
        logbuffer._file_sink_healthy = True
    if sink is not None:
        sink.close()


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    """Save and restore everything ``configure_logging`` / ``logbuffer.set_level`` mutate.

    Only the handful of tests that call those functions in their own body need this, but for
    them it is required, not optional. ``configure_logging`` is entirely process-global: it
    sets the root level via ``basicConfig``, attaches a ring handler to the root logger,
    lowers every noisy library logger, and re-runs ``structlog.configure``. All of that
    outlives the test and lands on whatever the xdist worker picks up next.

    This fixture is shared rather than copied into each test module, so every caller gets the
    same cleanup instead of one module forgetting it.
    """
    root = logging.getLogger()
    saved_root = root.level
    saved_handlers = list(root.handlers)
    saved = {name: logging.getLogger(name).level for name in _NOISY_LOGGERS}
    saved_ring = logbuffer.level_name()
    try:
        yield
    finally:
        # Order matters here. ``logbuffer.set_level`` sets the root logger's level as well as
        # the ring's, so restoring the ring last would silently overwrite the root level this
        # fixture just put back. Ring first, root last.
        logbuffer.set_level(saved_ring)
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_root)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every test hermetic. It skips ``.env`` reads and startup seeding.

    This does not cover the network. The ``_no_network`` guard above handles that
    separately, because this fixture controls what the app is configured from, and that one
    controls where a socket may connect.

    * ``Settings`` never reads the developer's dotenv files. ``env_file`` is cleared for the
      duration of the test, so ``Settings(data_dir=tmp_path, ...)`` gets exactly the fields
      the test passes, plus real environment variables, which CI controls.
    * The app lifespan's instance seeding (``load_raw_env``) and startup catch-up (the IMDb
      dataset download, scheduler catch-up) are stubbed out. A test that exercises the real
      functions imports them from their own modules instead, which this fixture leaves
      untouched.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setattr("reaper.main.load_raw_env", lambda _s: {})
    monkeypatch.setattr("reaper.main.catch_up_on_startup", _no_catch_up)
    # The auth throttles and the Argon2 gate are process-global singletons. A lockout
    # provoked by one test must never carry over into the next, since every TestClient shares
    # the same client address.
    login_throttle.reset()
    recover_throttle.reset()
    password_throttle.reset()
    argon2_gate.reset()


# --------------------------------------------------------------------------------------
# Booting a throwaway install
#
# Four fixtures, layered. What varies between test files is what they seed, so a file-local
# ``client`` fixture should hold only that seeding. These four fixtures take the boot itself
# and nothing else, so a file with its own seeding overrides ``client`` and asks for
# ``settings`` or ``sync_db`` instead of rewriting the boot from scratch.
#
# Every fixture here that builds a ``Settings`` or boots an app is function-scoped, and that
# is required, not a default. ``_hermetic`` above is function-scoped and takes a
# function-scoped ``monkeypatch``, so pytest sets up any higher-scoped fixture before it runs.
# A higher-scoped fixture that booted an app would see the real ``catch_up_on_startup`` and
# the developer's dotenv files still in place, and would read real credentials and start the
# IMDb download. The one booting fixture in the suite that is session-scoped,
# ``test_openapi_tags.schema``, applies those same three patches itself for exactly this
# reason. ``_schema_template`` below is session-scoped too, and that is safe. It builds no
# ``Settings`` and boots nothing, only ``create_all`` against a bare file path.
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _schema_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An empty database with every table created, built once per worker and copied per test.

    Copying the file is cheaper than running ``create_all`` again for every test that takes
    ``settings``. This builds the schema from a bare URL on purpose. It never constructs a
    ``Settings``, so there is nothing here for a dotenv file to reach.
    """
    path = tmp_path_factory.mktemp("schema") / "template.db"
    engine = sa_create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


@pytest.fixture
def settings(tmp_path: Path, _schema_template: Path) -> Settings:
    """A throwaway install rooted at ``tmp_path``, with its schema already created.

    The schema is in place before anything opens the database, because the app's own lifespan
    reads it on the way up. It seeds instances and checks that a local admin exists, so a
    ``create_app`` against an empty file would fail before any test body runs. This copies
    ``_schema_template`` instead of running a fresh ``create_all`` for each test.
    """
    resolved = Settings(data_dir=tmp_path, secret_key="test-key")
    shutil.copyfile(_schema_template, resolved.database_path)
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

    ``create_session_factory`` sets ``expire_on_commit=False``, so a row read back through the
    session that wrote it comes from the identity map without touching the database. A test
    that asserts on durable state opens a second session from this factory instead.
    """
    engine = create_async_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A signed-in client on that install, with the CSRF header set by default.

    It signs in because the API sits behind the auth gate, and almost every test that reaches
    a route only cares that it gets through. A test about the gate itself builds its own
    client on ``settings`` instead.
    """
    with TestClient(create_app(settings)) as booted:
        login(booted, settings)
        yield booted
