# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wait for Plex to finish the folder scans a reap asked for, then let a scan start.

A reap sends Plex one folder scan per emptied folder. Plex runs them one at a time, so a big
reap keeps Plex busy long after the last request. A Reaper scan that reads Plex during that
time sees titles that are still disappearing. This module holds the wait. It only reads.

While the wait runs, ``waiting_since`` is set. Every path that would start a scan or read the
Plex library checks it and declines.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

import structlog

log = structlog.get_logger(__name__)

#: Plex may not have started its first scan yet when the reap ends.
GRACE_S = 60.0
POLL_S = 20.0
#: A gap can sit between two queued scans, so one quiet read proves nothing.
QUIET_POLLS = 3
CEILING_S = 3 * 3600.0

_since: datetime | None = None


def waiting_since() -> datetime | None:
    """When the current wait began, or ``None`` when no wait is running."""
    return _since


def begin() -> None:
    global _since
    _since = datetime.now(UTC)


def end() -> None:
    global _since
    _since = None


async def wait_until_quiet(
    is_scanning: Callable[[], Awaitable[bool]],
    *,
    grace: float = GRACE_S,
    poll: float = POLL_S,
    quiet_polls: int = QUIET_POLLS,
    ceiling: float = CEILING_S,
) -> Literal["quiet", "ceiling"]:
    """Poll until Plex reports no scan for ``quiet_polls`` reads in a row, or ``ceiling`` passes.

    ``is_scanning`` answers ``True`` when it cannot tell, so an unreadable Plex never ends
    the wait. Any busy read resets the count.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.sleep(grace)
    quiet = 0
    while loop.time() - started < ceiling:
        quiet = 0 if await is_scanning() else quiet + 1
        if quiet >= quiet_polls:
            return "quiet"
        await asyncio.sleep(poll)
    return "ceiling"
