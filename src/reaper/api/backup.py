# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings -> Backup: download everything Reaper cannot rebuild, and put it back.

The download half carries the database plus the key and salt that decrypt it (see
:mod:`reaper.services.backup` for what is inside and why). That makes the route as
sensitive as the master key, so it is fenced off the API-key lane in
:mod:`reaper.api.middleware`. An automation credential must never be able to
exfiltrate the key that unlocks every stored password. Like every ``/api`` route, it
also requires a signed-in session.

The restore half (see :mod:`reaper.services.restore`) is a two-step, stage-and-restart
flow. ``prepare`` validates an uploaded archive and stages it, un-armed. ``confirm``
verifies the admin password, behind the same lockout and Argon2 gate as arming
deletion, then forces deletion off in the staged database and arms the swap. The
actual replacement happens at the next container start, before migrations, and
``restart`` is a way of asking for that from the browser instead of from a shell.
None of the restore routes are on the API-key lane. They are unsafe methods outside
the automation allowlist, so a key cannot reach any of them, ``restart`` least of
all, since that route stops the app.
"""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
from collections.abc import Iterator
from pathlib import Path

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from reaper.api import tags as api_tags
from reaper.api.deps import (
    client_ip,
    require_admin_password,
    runtime_settings,
    session_factory,
)
from reaper.api.errors import refuse, refuse_from
from reaper.api.runs import reap_in_flight
from reaper.api.schemas import OkOut, RestoreCancelOut
from reaper.buildinfo import build_version
from reaper.config import Settings
from reaper.secrets import env_key_active, key_file_path
from reaper.services import admin_password, app_settings, backup, restore

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/settings/backup", tags=[api_tags.BACKUP])

#: An upload larger than this is refused before it is written to disk. The archive
#: is gzip-compressed, so it is far smaller than the database inside. This ceiling is
#: generous for even a very large library, and only exists to stop a runaway upload.
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024


class BackupInfoOut(BaseModel):
    reaper_db_bytes: int
    """The live database size, roughly what the download weighs before compression."""
    last_backup_at: str | None
    """When a backup was last downloaded, or ``None`` if never."""
    key_in_backup: bool
    """Whether the encryption key travels inside the backup. ``False`` when the operator
    supplies ``REAPER_SECRET_KEY`` from the environment, so there is no key file to bundle
    and a restore needs that same value set on the target."""
    app_version: str
    restore_armed: bool
    """Whether a confirmed restore is staged and waiting for Reaper to restart."""


class RestoreSummaryOut(BaseModel):
    """What an uploaded, accepted backup is. Shown for the operator to confirm."""

    app_version: str | None
    created_at: str | None
    verdict: str
    key_in_backup: bool
    reaper_db_bytes: int
    token: str
    """Handed back at confirm time so the arm binds to the exact backup reviewed here."""


class RestoreConfirmIn(BaseModel):
    password: str | None = Field(default=None, max_length=128)
    """Bounded, like every other field that reaches Argon2 (``SafetyIn``,
    ``AdminPasswordIn``, ``WatchEvidenceResetIn``). Hashing unbounded input is a
    CPU-exhaustion vector."""
    token: str | None = Field(default=None, max_length=restore.TOKEN_MAX_LEN)
    """Bounded to the width the staging mints, as ``RestoreCancelIn.token`` is."""


class RestoreCancelIn(BaseModel):
    token: str | None = Field(default=None, max_length=restore.TOKEN_MAX_LEN)
    """Which staging to discard, when the caller knows. The discard happens only
    while that token is still the staged one. Absent means discard whatever is
    there, which is what the armed card's Cancel needs, since it holds no summary to
    take a token from."""


@router.get("")
async def backup_info(request: Request) -> BackupInfoOut:
    """What a backup would contain and weigh, when one was last taken, and whether a
    restore is staged and waiting for a restart."""
    settings = runtime_settings(request)
    async with session_factory(request)() as session:
        last = await app_settings.get_last_backup_at(session)
    return BackupInfoOut(
        reaper_db_bytes=backup.db_size_on_disk(settings.database_path),
        last_backup_at=last,
        # Self-sufficiency follows runtime precedence, not file existence. An
        # env-supplied key wins over any lingering secret.key, so that key is not
        # what a backup would bundle. Matches backup._build_into's key_included.
        key_in_backup=key_file_path(settings).is_file() and not env_key_active(settings),
        app_version=build_version(),
        restore_armed=restore.is_armed(settings),
    )


async def _record_backup_taken(request: Request, created_at: str) -> None:
    """Record when a backup was secured. This runs as the response's background
    task, after the last byte is sent, so a download that dies mid-stream never
    claims a copy exists that the operator does not have."""
    async with session_factory(request)() as session:
        await app_settings.set_last_backup_at(session, created_at)
        await session.commit()


# An archive, not JSON. Same response-class fix as the downloads in ``api/poster.py``
# and ``api/logs.py``. The media type here matches what the response actually sets
# below, not a generic byte stream. The route sends `nosniff` beside it, so a
# generated client has nothing but this declaration to dispatch on.
@router.get(
    "/download",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/gzip": {}}}},
)
async def download_backup(request: Request) -> StreamingResponse:
    """Build the backup and stream it to the browser as one downloadable file."""
    settings = runtime_settings(request)
    archive = await backup.create_backup(settings)

    # From here to the returned response, any failure would strand the finished
    # archive. Its cleanup lives in the stream generator's finally block, which
    # never runs if this never returns the response, so this cleans it up and
    # re-raises.
    try:
        size = archive.path.stat().st_size

        def body() -> Iterator[bytes]:
            try:
                with archive.path.open("rb") as handle:
                    while chunk := handle.read(backup.DOWNLOAD_CHUNK):
                        yield chunk
            finally:
                backup.cleanup(archive)

        log.info("backup.downloaded", revision=archive.revision, bytes=size)
        return StreamingResponse(
            body(),
            media_type="application/gzip",
            headers={
                "Content-Disposition": f'attachment; filename="{archive.filename}"',
                "Content-Length": str(size),
                "X-Content-Type-Options": "nosniff",
            },
            # Records "last backup" only after the stream completes, never before.
            background=BackgroundTask(_record_backup_taken, request, archive.created_at),
        )
    except BaseException:
        backup.cleanup(archive)
        raise


async def _spool_body(request: Request, settings: Settings) -> Path:
    """Stream the raw request body to a temp file under ``data/``, capped.

    The upload arrives as the raw request body, not multipart, so it never buffers
    whole in memory. Each chunk is written straight to disk, and the total is
    bounded, so a runaway upload is refused instead of filling the disk. The caller
    removes the file.
    """
    settings.ensure_data_dir()
    fd, name = tempfile.mkstemp(prefix=backup.RESTORE_UPLOAD_PREFIX, dir=settings.data_dir)
    path = Path(name)
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    refuse(413, "error.backup.upload_too_large")
                out.write(chunk)
    except BaseException:
        # mkstemp created it, so it exists to remove. os.unlink (not Path.unlink) because
        # pathlib methods are barred in async functions (ASYNC240).
        os.unlink(path)  # noqa: PTH108
        raise
    if total == 0:
        os.unlink(path)  # noqa: PTH108 -- see above
        refuse(400, "error.backup.no_file_uploaded")
    return path


@router.post("/restore/prepare")
async def restore_prepare(request: Request) -> RestoreSummaryOut:
    """Validate an uploaded backup and stage it, un-armed, for a later confirm.

    Refuses a file that is not a Reaper backup, or one from a newer Reaper whose schema
    this build cannot serve. On success the archive is staged but not armed. Nothing is
    swapped until ``confirm`` verifies the admin password.
    """
    settings = runtime_settings(request)
    archive_path = await _spool_body(request, settings)
    try:
        summary = await asyncio.to_thread(restore.stage_upload, settings, archive_path)
    except restore.RestoreError as exc:
        refuse_from(exc)
    finally:
        archive_path.unlink(missing_ok=True)
    # Field for field off the staging summary, except `revision`. An Alembic id is
    # not operator copy, and `RestoreSummaryOut` does not declare it, which is what
    # drops it here.
    return RestoreSummaryOut.model_validate(summary, from_attributes=True)


@router.post("/restore/confirm")
async def restore_confirm(request: Request, payload: RestoreConfirmIn) -> OkOut:
    """Verify the admin password, then arm the staged restore.

    This is gated exactly like arming deletion
    (:func:`reaper.api.settings.set_safety`). It calls the same gate,
    :func:`reaper.api.deps.require_admin_password`, because a restore is as
    consequential as arming, and its confirm is a password-guessing surface too. The
    ``token`` from the prepare summary binds this confirm to the exact backup
    reviewed, so a backup swapped in by another session since cannot be armed by
    this password. On success the staged database is forced read-only, its
    inherited sessions are cleared, and the swap is armed. The swap itself happens
    on the next start, which the operator asks for with ``restore/restart`` or by
    restarting the container themselves.
    """
    settings = runtime_settings(request)
    keys = (f"ip:{client_ip(request)}", "account:restore")
    async with session_factory(request)() as session:
        if not await admin_password.has_password(session):
            refuse(400, "error.backup.no_password_set")
        await require_admin_password(
            session,
            payload.password or "",
            keys=keys,
            gate="restore",
            code="error.auth.restore_password_mismatch",
        )

    try:
        await asyncio.to_thread(restore.arm, settings, payload.token)
    except restore.RestoreError as exc:
        refuse_from(exc)
    log.warning("restore.confirmed")
    return OkOut(ok=True)


@router.post("/restore/cancel")
async def restore_cancel(
    request: Request, payload: RestoreCancelIn | None = None
) -> RestoreCancelOut:
    """Discard a staged or armed restore. Turns off the "waiting to finish" state.

    With a ``token``, the discard is scoped to that staging and refuses to touch one
    replaced since. This is what an unattended caller needs, since the card's
    unmount reclaim fires with no operator watching, and two live restore cards
    (Settings in one tab, the wizard's door in another) can each hold a summary
    while only the later one is staged. Without a token it discards whatever is
    there, which is what the operator's own Cancel means.

    ``cleared`` says whether a staging was actually removed. An ownership refusal
    and a call that found nothing both report this as false. Neither is an error.
    Nothing was lost, and the archive is still on the operator's disk.
    """
    cleared = await asyncio.to_thread(
        restore.clear_pending, runtime_settings(request), payload.token if payload else None
    )
    if cleared:
        log.info("restore.canceled")
    return RestoreCancelOut(ok=True, cleared=cleared)


#: How long the stop waits after the response has been handed to the server. The browser is
#: told first and the process goes second, so the operator sees "Stopping" rather than a dead
#: connection with no explanation. Short, because nothing useful happens in the window.
_STOP_DELAY_SECONDS = 0.5


def _stop_this_process() -> None:
    """Ask this process to stop, the way stopping the container asks.

    This must send ``SIGTERM`` to itself, never ``sys.exit`` or ``os._exit``. Only
    the signal triggers uvicorn's graceful shutdown, which runs
    :func:`reaper.main.lifespan`'s ``finally`` block, where a reap still in flight is
    canceled and awaited so the executor marks the run ABORTED and commits its
    journal. Exiting by any other means would drop a deleting run mid-step with the
    row still reading EXECUTING.

    Reaper cannot see its own restart policy from inside the container, so nothing
    here promises it comes back. What it can promise is what it left behind. The
    staged restore is on disk, the swap has not run, and starting the container
    applies it.
    """
    os.kill(os.getpid(), signal.SIGTERM)


async def _stop_after_response() -> None:
    await asyncio.sleep(_STOP_DELAY_SECONDS)
    log.warning("restore.restart_requested")
    _stop_this_process()


# The body is built by hand so the stop can ride out on a background task, which a
# plain return cannot carry. ``response_model`` is what names the shape in the
# published document. Without it, the route would publish an empty schema, which
# reads as "some JSON" and is worse than the free-form map its siblings published.
# The function keeps returning the ``JSONResponse`` rather than moving the task onto
# a ``BackgroundTasks`` dependency, since that would change when the signal fires
# relative to the last byte, which is what the docstring below explains.
@router.post("/restore/restart", response_model=OkOut)
async def restore_restart(request: Request) -> JSONResponse:
    """Stop Reaper, so the staged restore is applied on the way back up.

    This is the last step of a restore, as a button. The first-run wizard opens onto
    this flow too.

    **Two refusals, both resolving toward doing nothing:**

    * Without an armed restore, this is a general-purpose "stop the app" endpoint,
      which Reaper does not offer. It refuses, so the only state this route can
      reach is one where stopping is what the operator was already asking for.
    * While a reap is in flight, it refuses too. Shutdown does handle that case. The
      run is canceled, awaited, and recorded ABORTED. But handling it is not a
      reason to interrupt the one path that deletes files. The operator has a
      graceful Stop for the run, and a staged restore will wait. The window between
      this check and the signal stays open regardless. The shutdown path is what
      closes it.

    This route is not on the API-key lane. A key writes only what
    :mod:`reaper.api.middleware`'s allowlist names, and this route is not on it, so
    it is refused simply by being outside that list. That is deliberate, since
    stopping the app is not automation's to do.
    """
    settings = runtime_settings(request)
    if not await asyncio.to_thread(restore.is_armed, settings):
        refuse(409, "error.backup.restore_not_waiting")
    if reap_in_flight(request.app):
        refuse(409, "error.backup.reap_in_progress")
    # The stop rides the response's background task, so it runs after the last byte is on its
    # way and the browser has an answer to render. A route that stopped the process inline
    # would close the connection first and leave the operator looking at a network error.
    return JSONResponse({"ok": True}, background=BackgroundTask(_stop_after_response))
