# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small asyncio helpers: the scan's concurrent fan-outs, and per-loop mutual exclusion.

Bare ``asyncio.gather`` is the wrong tool where this codebase fans out against an
operator's live services: on the first failure it re-raises immediately but leaves
every sibling task *running* and unobserved -- reads that keep hammering Plex,
Tautulli and the *arrs after the scan is already dead, plus late failures that
surface as "exception was never retrieved" noise at teardown. These helpers keep
gather's shape but reap the survivors: cancel, drain, log, then re-raise the
original failure. Every concurrent fan-out in the scan pipeline goes through here,
so there is exactly one cancellation discipline to reason about.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import structlog

log = structlog.get_logger(__name__)


async def reap(tasks: Sequence[asyncio.Task[Any]]) -> None:
    """Cancel and drain every task, keeping any late failure observed.

    Called only while unwinding from a primary failure, so a reaped task's own
    error is logged rather than raised -- the first failure is the one the caller
    is already propagating, and losing the others silently would hide a
    misconfiguration the operator should see in the log.
    """
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except BaseException as exc:  # observed and logged
            log.warning("aio.reaped_failure", error=str(exc))


async def gather_reaped(*aws: Awaitable[Any]) -> list[Any]:
    """Run awaitables concurrently; on the first failure reap the rest, then re-raise.

    The results list is in argument order, exactly like ``asyncio.gather``. Unlike
    gather, a failure cannot leave siblings running detached: they are canceled and
    awaited before the failure propagates, so a caller that catches it knows no
    stray read is still in flight.
    """
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        await reap(tasks)
        raise


def report_background_failure(task: asyncio.Task[Any]) -> None:
    """Log why a detached task died, instead of letting asyncio mumble at GC time (rule 102).

    A task nobody awaits keeps its exception until it is garbage collected, and then all
    the operator gets is a bare "Task exception was never retrieved" with no name attached
    (PR-12). Cancellation is the normal shutdown path and says nothing.

    Lives here rather than in ``main``, which imports the api routers: two of the three
    callers are routers, so importing it from ``main`` would close a cycle. Pass ``name=``
    to ``create_task``, or the log names the task ``Task-7``.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("background_task_failed", task=task.get_name(), error=str(exc))


def per_loop_lock() -> Callable[[], asyncio.Lock]:
    """A getter returning this event loop's lock, created on first use.

    A module-level ``asyncio.Lock`` binds to a loop and then raises on every other, and the
    suite runs a fresh loop per test (rule 37). In production there is one loop, hence one
    lock, which is what serializes a section across every concurrent caller in the process.

    **The binding happens on a CONTENDED acquire, not on any acquire**, measured rather than
    assumed: ``Lock.acquire`` returns on a fast path that never reads the running loop, so a
    shared lock survives any number of uncontended ones and raises the first time two callers
    actually meet on a second loop. That is why the shared version fails intermittently
    instead of on the second test, and why the test for this drives the contended case.

    **Weak-keyed with one bound, stated because it is not the obvious one**: a loop whose lock
    was never contended is collected with its entry, and a contended one is not, since
    ``asyncio.Lock`` stores the loop on itself and the value then keeps the key alive. That
    costs one lock per loop that contended, which is nothing in a process with one loop and
    bounded by the suite otherwise.

    **It carries mutual exclusion and no schema policy**, deliberately. Three callers:
    ``history_sync._rebuild_lock``, ``lists._widen_lock`` and ``leaving_soon._pass_lock``,
    and only the first two guard schema at all. ``history_sync`` DROPS a stale ``watch_event``
    under its lock and ``lists.ensure_schema`` must never do that, since dropping unprotects
    every keep list until the next sync refills it. ``imdb_dataset`` takes no lock here and
    needs none: it never drops a live table, it builds a staging copy and renames it in, and
    a missing table degrades on the read side rather than reading as an empty one.
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
