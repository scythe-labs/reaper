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

from collections.abc import Iterator
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from reaper import logbuffer
from reaper.api import tags as api_tags
from reaper.api.errors import refuse
from reaper.services import app_settings

router = APIRouter(prefix="/api", tags=[api_tags.LOGS])

#: Read the on-disk log files in modest chunks rather than loading up to three 20 MiB
#: files into memory at once.
_DOWNLOAD_CHUNK = 64 * 1024


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
    files_kept: int
    """How many rotating log files the server keeps (the live file plus its backups). The
    Logs tab renders this instead of a hardcoded number, so its "newest N files" copy tracks
    the backend constant (rules 66/67)."""


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
        files_kept=logbuffer.files_retained(),
    )


# A text file, not JSON. Same published-shape correction as ``api/poster.py`` and
# ``api/backup.py``'s download (rule 72).
@router.get(
    "/logs/download",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/plain": {}}}},
)
async def download_logs(request: Request) -> StreamingResponse:
    """The full log as one downloadable text file.

    Concatenates the rotating files on disk oldest-first, so the operator gets a fuller
    trail than the in-memory window keeps -- the right thing to attach to a bug report.
    Before anything has been written to disk (or if file logging could not start), it
    falls back to the in-memory ring so the download is never empty. Streamed file by
    file, so three 20 MiB files never sit in memory at once.

    If the on-disk mirror went degraded mid-run (a read-only remount, a full disk), the
    files end where writing stopped; the in-memory ring is appended after them, behind a
    marker, so the download carries the recent lines that never reached disk rather than
    ending silently at the failure (rule 82).
    """
    files = logbuffer.log_files()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"reaper-logs-{stamp}.log"

    def body() -> Iterator[bytes]:
        if files:
            for path in files:
                try:
                    with path.open("rb") as handle:
                        while chunk := handle.read(_DOWNLOAD_CHUNK):
                            yield chunk
                except OSError:
                    # A file rotated away mid-read is gone, not an error worth failing the
                    # whole download over; skip it and stream what remains.
                    continue
            if not logbuffer.file_sink_healthy():
                yield (
                    b"\n=== Log file writing failed at some point above. The lines below are "
                    b"from memory and may overlap the on-disk trail. ===\n"
                )
                for line in logbuffer.dump_lines():
                    yield f"{line.ts} {line.level:<7} {line.text}\n".encode()
        else:
            for line in logbuffer.dump_lines():
                yield f"{line.ts} {line.level:<7} {line.text}\n".encode()

    return StreamingResponse(
        body(),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.put("/logs/level")
async def put_log_level(request: Request, payload: LogLevelIn) -> LogsOut:
    """Change how much Reaper records. Applies immediately and persists.

    Only the levels the UI offers are accepted; hiding warnings from a tool that
    deletes files is not a choice we sell. ``logbuffer.LEVELS`` is wider than that on
    purpose, since ``REAPER_LOG_LEVEL`` may carry ERROR (#700), so this narrows to
    ``UI_LEVELS`` rather than to whatever the normalizer will take.
    """
    canonical = logbuffer.normalize_level(payload.level)
    if canonical is None or canonical not in logbuffer.UI_LEVELS:
        refuse(422, "error.logs.bad_level")
    factory = request.app.state.session_factory
    async with factory() as session:
        await app_settings.set_log_level(session, canonical)
        await session.commit()
    logbuffer.set_level(canonical)
    return LogsOut(
        lines=[],
        last_seq=logbuffer.RING.last_seq(),
        level=logbuffer.level_name(),
        files_kept=logbuffer.files_retained(),
    )
