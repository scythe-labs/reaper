# SPDX-License-Identifier: AGPL-3.0-or-later
"""Asyncio helpers: run concurrent tasks safely, share one lock per event loop, and log
a background task's exception instead of losing it.

Plain ``asyncio.gather`` is unsafe here because this code fans out against an
operator's live services. On the first failure, ``gather`` re-raises right away but
leaves every other task running and unobserved: those tasks keep hitting Plex,
Tautulli, and the *arr apps after the scan has already failed, and their later
failures show up only as "exception was never retrieved" noise. :func:`gather_reaped`
keeps ``gather``'s interface but cancels and awaits the survivors first, then
re-raises the original failure. Every concurrent fan-out in the scan goes through it,
so there is one cancellation rule to remember instead of many.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import structlog

log = structlog.get_logger(__name__)


async def reap(tasks: Sequence[asyncio.Task[Any]]) -> None:
    """Cancel every task, wait for each to finish, and log any error it raises.

    Runs only while unwinding from an earlier failure. That failure is what the
    caller re-raises, so a reaped task's own error is logged instead of raised:
    losing it silently could hide a misconfiguration the operator needs to see in
    the log.
    """
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except BaseException as exc:  # awaiting retrieves it, so asyncio won't warn later
            log.warning("aio.reaped_failure", error=str(exc))


async def gather_reaped(*aws: Awaitable[Any]) -> list[Any]:
    """Run awaitables concurrently. On the first failure, cancel and await the rest,
    then re-raise it.

    Returns results in argument order, like ``asyncio.gather``. Unlike ``gather``, a
    failure here never leaves a sibling task running: :func:`reap` cancels and awaits
    every task first, so a caller that catches the failure knows no other read is
    still in flight.
    """
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        await reap(tasks)
        raise


def report_background_failure(task: asyncio.Task[Any]) -> None:
    """Log why a detached task died, instead of letting asyncio report it only at
    garbage-collection time.

    A task nobody awaits keeps its exception until Python garbage-collects it. At
    that point the operator sees only a bare "Task exception was never retrieved"
    with no task name attached. A canceled task is a normal shutdown and logs
    nothing.

    Lives here instead of in ``main``, which imports the API routers: two of the
    three callers are routers, and importing this from ``main`` would create a
    circular import. Pass ``name=`` to ``create_task``, or the log names the task
    ``Task-7``.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("aio.background_task_failed", task=task.get_name(), error=str(exc))


def per_loop_lock() -> Callable[[], asyncio.Lock]:
    """Return a getter for this event loop's lock, creating the lock on first use.

    A module-level ``asyncio.Lock`` binds to whichever loop first acquires it and
    then raises on every other loop, and the test suite runs a fresh loop per test.
    In production there is one loop, so this holds one lock, and that lock
    serializes a section across every concurrent caller in the process.

    The lock binds to a loop on its first CONTENDED acquire, not on any acquire:
    ``Lock.acquire`` has a fast path that never reads the running loop, so an
    uncontended lock never binds. A shared lock only raises the first time two
    callers actually contend for it on a second loop, which is why a stale shared
    lock fails intermittently rather than on first use, and why the test for this
    drives the contended case.

    Keys the dictionary weakly, with one exception: a loop whose lock was never
    contended is garbage-collected along with its dictionary entry, but a
    contended one is not, because ``asyncio.Lock`` stores the loop on itself and
    that reference keeps the dictionary key alive. That holds one lock per loop
    that ever contended, which costs nothing in a process with one loop.

    This function only provides mutual exclusion. It enforces no policy about what
    a caller does under the lock. Three callers use it: ``history_sync._rebuild_lock``,
    ``lists._widen_lock``, and ``leaving_soon._pass_lock``, and only the first two
    guard schema changes. ``history_sync`` drops a stale ``watch_event`` row under
    its lock, but ``lists.ensure_schema`` must never drop a row that way: dropping it
    would stop every keep list from protecting anything until the next sync refills
    it. ``imdb_dataset`` takes no lock here and needs none. It never drops a live
    table, it builds a new copy and renames it into place, and a missing table
    degrades gracefully on the read side instead of reading as empty.
    """
    locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
        weakref.WeakKeyDictionary()
    )

    def get() -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            locks[loop] = lock
        return lock

    return get
