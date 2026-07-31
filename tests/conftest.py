# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session-wide test configuration.

Three hermeticity guarantees, applied to every test:

**Cheap Argon2.** The hasher is patched to minimal cost parameters before any test
runs. Production defaults (time_cost=3, memory_cost=65536) are intentionally slow; on a
CI runner that can add several minutes to the suite for the 100+ tests that hash or
verify a password via fixtures. The patch is safe: tests care only that authentication
accepts the right password and rejects the wrong one, not about the hash's resistance
to offline cracking.

**No developer state, no network.** The autouse fixture below keeps every test off the
developer's real ``.env``/``.env.local`` and off the network, whether or not the test
boots the app. Without it, any test that constructs ``Settings`` silently reads the
repo-root ``.env`` (copying real service keys into throwaway test databases), and any
test that starts the app lifespan seeds instances from ``.env.local`` and kicks off the
~280 MB IMDb dataset download -- slow, flaky, and different from CI.

**No real backoff.** ``asyncio.sleep`` is collapsed to a single event-loop tick before
any test runs (see below). Nothing in the suite asserts on real elapsed time, so paying
the real delay for a client retry or a poll loop only burns wall clock for no signal.
"""

import asyncio
import logging
from collections.abc import Iterator

import pytest
import structlog
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

import reaper.auth.passwords as _passwords
from reaper import logbuffer
from reaper.auth.ratelimit import (
    argon2_gate,
    login_throttle,
    password_throttle,
    recover_throttle,
)
from reaper.config import Settings
from reaper.logging import _NOISY_LOGGERS

_passwords._hasher = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8, parallelism=1),))

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


@pytest.fixture(autouse=True)
def _capturable_logs() -> None:
    """Keep ``structlog.testing.capture_logs`` working across the whole suite.

    ``configure_logging`` (called by ``test_foundations`` and by every ``create_app``
    boot) sets ``cache_logger_on_first_use=True``. The first time a module logger is used
    while that flag is live, structlog PERMANENTLY replaces that logger proxy's ``bind``
    with a closure holding the then-current processors -- after which ``capture_logs``
    can never intercept it, and even ``reset_defaults`` will not undo it. Left alone, the
    flag persists across tests, so a scan logger materialized after one of those tests is
    deaf to every later ``capture_logs`` assertion (an ordering-dependent failure in the
    full suite that a single-file run never shows). Clearing the flag before each test
    keeps capturable loggers from ever caching. Tests that assert on ``configure_logging``
    itself call it inside their own body, so this starting state does not affect them.
    """
    structlog.configure(cache_logger_on_first_use=False)


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
    """Every test is hermetic: no ``.env`` reads, no startup seeding, no network.

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
