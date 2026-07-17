# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Settings -> Logs tab: read the ring, change the level.

Reading is an incremental poll -- the UI asks for "everything after the last line I
have" every couple of seconds while Live, and stops asking while paused. There is no
stream to hold open and nothing stateful on the server beyond the ring itself.

The level PUT is the one mutating route: it persists the choice (the stored value wins
over ``REAPER_LOG_LEVEL`` forever after, like every env-seeded switch) and applies it
immediately -- no restart, because the logging pipeline consults the live level per
event (see ``reaper.logbuffer``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from reaper import logbuffer
from reaper.services import app_settings

router = APIRouter(prefix="/api")


class LogLineOut(BaseModel):
    seq: int
    ts: str
    level: str
    text: str


class LogsOut(BaseModel):
    lines: list[LogLineOut]
    last_seq: int
    """The newest sequence number the ring has seen -- the cursor for the next poll."""
    level: str
    """The level Reaper is recording at right now, so the picker stays honest."""


class LogLevelIn(BaseModel):
    level: str


@router.get("/logs")
async def get_logs(
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
) -> LogsOut:
    """The log lines newer than ``after``, oldest first.

    ``after=0`` (the first poll) returns the newest ``limit`` lines still in memory.
    Lines older than the ring's window are gone; the UI says so instead of pretending.
    """
    lines = logbuffer.RING.since(after, limit=limit)
    return LogsOut(
        lines=[
            LogLineOut(seq=line.seq, ts=line.ts, level=line.level, text=line.text) for line in lines
        ],
        last_seq=logbuffer.RING.last_seq(),
        level=logbuffer.level_name(),
    )


@router.put("/logs/level")
async def put_log_level(request: Request, payload: LogLevelIn) -> LogsOut:
    """Change how much Reaper records. Applies immediately and persists.

    Only the levels the UI offers are accepted; hiding warnings from a tool that
    deletes files is not a choice we sell.
    """
    canonical = logbuffer.normalize_level(payload.level)
    if canonical is None:
        raise HTTPException(422, "Pick Debug, Info, or Warning.")
    factory = request.app.state.session_factory
    async with factory() as session:
        await app_settings.set_log_level(session, canonical)
        await session.commit()
    logbuffer.set_level(canonical)
    return LogsOut(lines=[], last_seq=logbuffer.RING.last_seq(), level=logbuffer.level_name())
