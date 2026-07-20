# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small asyncio helpers for the scan's concurrent fan-outs.

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
from collections.abc import Awaitable, Sequence
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
        except BaseException as exc:  # observed and logged, never lost
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
