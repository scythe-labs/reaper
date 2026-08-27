# SPDX-License-Identifier: AGPL-3.0-or-later
"""The asyncio helpers. Per-loop mutual exclusion.

``gather_reaped`` and ``reap`` are exercised through the scan pipeline that uses them.
``per_loop_lock`` is not exercised anywhere by its own properties, and neither of two
earlier, duplicated test files actually proved the properties they claimed in prose.

Every test here contends the lock, which is the whole point. ``asyncio.Lock.acquire``
returns on a fast path that never reads the running loop, so an uncontended acquire binds
nothing. A test that takes the lock alone would pass against a shared module-level lock
too, and would read as a proof of the thing this module exists for.
"""

from __future__ import annotations

import asyncio
import gc
import weakref

from reaper.aio import per_loop_lock


async def _contend(lock: asyncio.Lock) -> None:
    """Two callers actually meeting on the lock, which is what binds it to this loop."""

    async def hold() -> None:
        async with lock:
            await asyncio.sleep(0)

    await asyncio.gather(hold(), hold())


def test_each_loop_gets_its_own_lock() -> None:
    """The whole reason this is a factory. A lock binds to the loop it was first contended
    on and raises on every other, and the suite runs a fresh loop per test."""
    lock_for = per_loop_lock()
    seen: list[asyncio.Lock] = []

    async def take() -> None:
        lock = lock_for()
        seen.append(lock)
        await _contend(lock)

    asyncio.run(take())
    asyncio.run(take())

    assert seen[0] is not seen[1]


def test_a_shared_lock_is_what_this_prevents() -> None:
    """The control, and the reason the test above is not enough on its own. Two distinct
    objects say nothing about the failure. One lock contended on two loops raises, so a
    module-level ``asyncio.Lock`` would take the suite down from whichever test second
    reached it under contention. That would happen intermittently, since an uncontended
    acquire is silent."""
    shared = asyncio.Lock()
    asyncio.run(_contend(shared))

    try:
        asyncio.run(_contend(shared))
    except RuntimeError as exc:
        assert "bound to a different event loop" in str(exc)
    else:  # pragma: no cover - the failure this module exists to prevent
        raise AssertionError("a contended lock was reused across loops without raising")


def test_one_loop_gets_one_lock() -> None:
    """Mutual exclusion is the point, so two callers on one loop must meet the same lock."""
    lock_for = per_loop_lock()

    async def both() -> tuple[asyncio.Lock, asyncio.Lock]:
        return lock_for(), lock_for()

    first, second = asyncio.run(both())
    assert first is second


def test_a_loop_that_never_contended_takes_its_lock_with_it() -> None:
    """The registry is weak-keyed, which the original copies claimed and neither showed. A
    strong dict would hold one lock per loop the process ever ran, and the suite runs
    thousands.

    This is named for the case it actually pins. A lock that *was* contended stores the
    loop on itself, so the value keeps the key alive and the entry outlives the loop. That
    case is recorded on ``per_loop_lock`` and costs one lock per contended loop, which is
    nothing in a process with one loop.
    """
    lock_for = per_loop_lock()
    held: list[weakref.ref[asyncio.Lock]] = []

    async def take() -> None:
        held.append(weakref.ref(lock_for()))

    asyncio.run(take())
    gc.collect()

    assert held[0]() is None, "a lock outlived the loop it was made for"
