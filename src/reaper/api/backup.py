# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings -> Backup: download everything Reaper cannot rebuild, and put it back.

The download half carries the precious database plus the key and salt that decrypt it
(see :mod:`reaper.services.backup` for what is inside and why), which makes that route
as sensitive as the master key: it is fenced off the API-key lane in
:mod:`reaper.api.middleware` -- an automation credential must never be able to
exfiltrate the key that unlocks every stored password -- and, like every ``/api``
route, it requires a signed-in session.

The restore half (see :mod:`reaper.services.restore`) is a two-step, stage-and-restart
flow. ``prepare`` validates an uploaded archive and stages it, un-armed. ``confirm``
verifies the admin password -- behind the same lockout and Argon2 gate as arming
deletion -- then forces deletion off in the staged database and arms the swap. The
actual replacement happens at the next container start, before migrations, which
``restart`` is a way of asking for from the browser rather than from a shell. None of the
restore routes are on the API-key lane: they are unsafe methods outside the automation
allowlist, so a key cannot reach them, ``restart`` least of all -- it stops the app.
"""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
from collections.abc import Iterator
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.background import BackgroundTask

from reaper.api import tags as api_tags
from reaper.api.auth import (
    _client_ip,
    _throttled,
    _verify_admin_password,
    record_password_failure,
)
from reaper.api.runs import reap_in_flight
from reaper.api.schemas import OkOut, RestoreCancelOut
from reaper.auth.ratelimit import password_throttle
from reaper.buildinfo import build_version
from reaper.config import Settings
from reaper.secrets import env_key_active, key_file_path
from reaper.services import admin_password, app_settings, backup, restore

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/settings/backup", tags=[api_tags.BACKUP])

#: An upload larger than this is refused before it is written to disk. The archive is
#: gzip-compressed, so it is far smaller than the database inside; this ceiling is
#: generous for even a very large library and only exists to stop a runaway upload.
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024


class BackupInfoOut(BaseModel):
    reaper_db_bytes: int
    """The live database size: roughly what the download weighs before compression."""
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
    """What an uploaded, accepted backup is -- shown for the operator to confirm."""

    app_version: str | None
    created_at: str | None
    verdict: str
    key_in_backup: bool
    reaper_db_bytes: int
    token: str
    """Handed back at confirm time so the arm binds to the exact backup reviewed here."""


class RestoreConfirmIn(BaseModel):
    password: str | None = Field(default=None, max_length=128)
    """Bounded, like every other field that reaches Argon2 (``SafetyIn``, ``AdminPasswordIn``,
    ``WatchEvidenceResetIn``): hashing unbounded input is a CPU-exhaustion vector, and this one
    was the only member of that set without the bound."""
    token: str | None = Field(default=None, max_length=restore.TOKEN_MAX_LEN)
    """Bounded off the width the staging mints (rule 95/131), as ``RestoreCancelIn.token`` is."""


class RestoreCancelIn(BaseModel):
    token: str | None = Field(default=None, max_length=restore.TOKEN_MAX_LEN)
    """Which staging to discard, where the caller knows: the discard happens only while that
    token is still the staged one. Absent means discard whatever is there, which is what the
    armed card's Cancel needs -- it holds no summary to take a token from."""


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


@router.get("")
async def backup_info(request: Request) -> BackupInfoOut:
    """What a backup would contain and weigh, when one was last taken, and whether a
    restore is staged and waiting for a restart."""
    settings = _settings(request)
    async with _factory(request)() as session:
        last = await app_settings.get_last_backup_at(session)
    return BackupInfoOut(
        reaper_db_bytes=backup.db_size_on_disk(settings.database_path),
        last_backup_at=last,
        # Self-sufficiency follows runtime precedence, not file existence: an env-supplied
        # key wins over any lingering secret.key, so it is not what a backup would bundle
        # (rule 76). Matches backup._build_into's key_included.
        key_in_backup=key_file_path(settings).is_file() and not env_key_active(settings),
        app_version=build_version(),
        restore_armed=restore.is_armed(settings),
    )


async def _record_backup_taken(request: Request, created_at: str) -> None:
    """Record when a backup was secured. Runs as the response's background task, after the
    last byte is sent, so a download that dies mid-stream never claims a copy exists that
    the operator does not have (rule 85, I-2)."""
    async with _factory(request)() as session:
        await app_settings.set_last_backup_at(session, created_at)
        await session.commit()


# An archive, not JSON. Same published-shape correction as ``api/poster.py`` and
# ``api/logs.py``'s download (rule 72).
@router.get(
    "/download",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def download_backup(request: Request) -> StreamingResponse:
    """Build the backup and stream it to the browser as one downloadable file."""
    settings = _settings(request)
    archive = await backup.create_backup(settings)

    # From here to the returned response, any failure would strand the finished archive
    # (its cleanup lives in the stream generator's finally, which never runs if we never
    # return the response), so clean it up and re-raise (PR-3).
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
            # Records "last backup" only after the stream completes, never before (I-2).
            background=BackgroundTask(_record_backup_taken, request, archive.created_at),
        )
    except BaseException:
        backup.cleanup(archive)
        raise


async def _spool_body(request: Request, settings: Settings) -> Path:
    """Stream the raw request body to a temp file under ``data/``, capped.

    The upload arrives as the raw request body (not multipart), so it never buffers
    whole in memory: each chunk is written straight to disk, and the total is bounded so
    a runaway upload is refused rather than filling the disk. The caller removes the file.
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
                    raise HTTPException(413, "That file is too large to be a Reaper backup.")
                out.write(chunk)
    except BaseException:
        # mkstemp created it, so it exists to remove. os.unlink (not Path.unlink) because
        # pathlib methods are barred in async functions (ASYNC240).
        os.unlink(path)  # noqa: PTH108
        raise
    if total == 0:
        os.unlink(path)  # noqa: PTH108 -- see above
        raise HTTPException(400, "No file was uploaded.")
    return path


@router.post("/restore/prepare")
async def restore_prepare(request: Request) -> RestoreSummaryOut:
    """Validate an uploaded backup and stage it, un-armed, for a later confirm.

    Refuses a file that is not a Reaper backup, or one from a newer Reaper whose schema
    this build cannot serve. On success the archive is staged but not armed: nothing is
    swapped until ``confirm`` verifies the admin password.
    """
    settings = _settings(request)
    archive_path = await _spool_body(request, settings)
    try:
        summary = await asyncio.to_thread(restore.stage_upload, settings, archive_path)
    except restore.RestoreError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    finally:
        archive_path.unlink(missing_ok=True)
    return RestoreSummaryOut(
        app_version=summary.app_version,
        created_at=summary.created_at,
        verdict=summary.verdict,
        key_in_backup=summary.key_in_backup,
        reaper_db_bytes=summary.reaper_db_bytes,
        token=summary.token,
    )


@router.post("/restore/confirm")
async def restore_confirm(request: Request, payload: RestoreConfirmIn) -> OkOut:
    """Verify the admin password, then arm the staged restore.

    Gated exactly like arming deletion (:func:`reaper.api.settings.set_safety`): the
    same per-IP and per-account lockout and Argon2 concurrency gate, because a restore is
    as consequential as arming and its confirm is a password-guessing surface too. The
    ``token`` from the prepare summary binds this confirm to the exact backup reviewed, so
    a backup swapped in by another session since cannot be armed by this password (rule 73).
    On success the staged database is forced read-only, its inherited sessions are cleared,
    and the swap is armed; the swap itself happens on the next start, which the operator asks
    for with ``restore/restart`` or by restarting the container themselves.
    """
    settings = _settings(request)
    keys = (f"ip:{_client_ip(request)}", "account:restore")
    async with _factory(request)() as session:
        if not await admin_password.has_password(session):
            raise HTTPException(
                400,
                "Set an admin password first. It's what confirms a restore.",
            )
        _throttled(password_throttle, *keys)
        ok = await _verify_admin_password(session, payload.password or "")
        if not ok:
            record_password_failure(password_throttle, keys, gate="restore")
            raise HTTPException(403, "That password didn't match. Nothing was restored.")
        for key in keys:
            password_throttle.record_success(key)

    try:
        await asyncio.to_thread(restore.arm, settings, payload.token)
    except restore.RestoreError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    log.warning("restore.confirmed")
    return OkOut(ok=True)


@router.post("/restore/cancel")
async def restore_cancel(
    request: Request, payload: RestoreCancelIn | None = None
) -> RestoreCancelOut:
    """Discard a staged or armed restore. Turns off the "waiting to finish" state.

    With a ``token`` the discard is scoped to that staging and refuses to touch one replaced
    since, which is what an unattended caller needs: the card's unmount reclaim fires with no
    operator watching, and two live restore cards (Settings in one tab, the wizard's door in
    another) can each hold a summary while only the later one is staged (#387). Without a
    token it discards whatever is there, which is what the operator's own Cancel means.

    ``cleared`` says whether a staging was actually removed, which an ownership refusal and a
    call that found nothing both report as false. Neither is an error: nothing was lost, and
    the archive is still on the operator's disk.
    """
    cleared = await asyncio.to_thread(
        restore.clear_pending, _settings(request), payload.token if payload else None
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

    ``SIGTERM`` to ourselves, never ``sys.exit`` and never ``os._exit``: the signal is what
    uvicorn turns into its graceful shutdown, and the graceful shutdown is what runs
    :func:`reaper.main.lifespan`'s ``finally`` -- where a reap still in flight is canceled
    *and awaited* so the executor marks the run ABORTED and commits its journal (rule 128).
    Leaving by any other door would drop a deleting run mid-step with the row still reading
    EXECUTING.

    Reaper cannot see its own restart policy from inside the container, so nothing here
    promises it comes back. What it can promise is what it left behind: the staged restore is
    on disk, the swap has not run, and starting the container applies it.
    """
    os.kill(os.getpid(), signal.SIGTERM)


async def _stop_after_response() -> None:
    await asyncio.sleep(_STOP_DELAY_SECONDS)
    log.warning("restore.restart_requested")
    _stop_this_process()


# The body is built by hand so the stop can ride out on a background task, which a plain
# return cannot carry. ``response_model`` is what names the shape in the published document;
# without it the route publishes an EMPTY schema, which reads as "some JSON" and is worse
# than the free-form map its siblings published. The function keeps returning the
# ``JSONResponse``: moving the task onto a ``BackgroundTasks`` dependency would change when
# the signal fires relative to the last byte, which is what the docstring below argues about.
@router.post("/restore/restart", response_model=OkOut)
async def restore_restart(request: Request) -> JSONResponse:
    """Stop Reaper, so the staged restore is applied on the way back up.

    The last step of a restore was a shell command in another window, at the end of a flow
    that is otherwise entirely in the browser, and the first-run wizard now opens onto that
    flow (#386). This is that step, as a button.

    **Two refusals, both resolving toward doing nothing:**

    * Without an armed restore this is a general-purpose "stop the app" endpoint, which is
      not a thing Reaper offers. It refuses, so the only state this route can reach is the
      one where stopping is what the operator was already being asked to do.
    * While a reap is in flight it refuses too. Shutdown does handle that case -- the run is
      canceled, awaited, and recorded ABORTED -- but "handled" is not a reason to interrupt
      the one path that deletes files, and the operator has a graceful Stop for the run and a
      staged restore that will wait. The window between this check and the signal is not
      closed by it; that is what the shutdown path is for.

    It is not on the API-key lane. A key writes only what
    :mod:`reaper.api.middleware`'s allowlist names, and this is not on it, so it is refused by
    being born outside it -- deliberately, because stopping the app is not automation's to do.
    """
    settings = _settings(request)
    if not await asyncio.to_thread(restore.is_armed, settings):
        raise HTTPException(409, "There's no restore waiting, so nothing was stopped.")
    if reap_in_flight(request.app):
        raise HTTPException(
            409, "A reap is running. Let it finish or stop it, then restart Reaper."
        )
    # The stop rides the response's background task, so it runs after the last byte is on its
    # way and the browser has an answer to render. A route that stopped the process inline
    # would close the connection first and leave the operator looking at a network error.
    return JSONResponse({"ok": True}, background=BackgroundTask(_stop_after_response))
