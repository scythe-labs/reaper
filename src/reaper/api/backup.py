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
actual replacement happens at the next container start, before migrations. None of the
restore routes are on the API-key lane: they are unsafe methods outside the automation
allowlist, so a key cannot reach them.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.background import BackgroundTask

from reaper.api.auth import _client_ip, _throttled, _verify_admin_password
from reaper.auth.ratelimit import password_throttle
from reaper.buildinfo import build_version
from reaper.config import Settings
from reaper.secrets import env_key_active, key_file_path
from reaper.services import admin_password, app_settings, backup, restore

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/settings/backup")

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
    """Whether a confirmed restore is staged and waiting for a container restart."""


class RestoreSummaryOut(BaseModel):
    """What an uploaded, accepted backup is -- shown for the operator to confirm."""

    app_version: str | None
    created_at: str | None
    revision: str | None
    verdict: str
    key_in_backup: bool
    reaper_db_bytes: int
    token: str
    """Handed back at confirm time so the arm binds to the exact backup reviewed here."""


class RestoreConfirmIn(BaseModel):
    password: str | None = None
    token: str | None = None


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
        reaper_db_bytes=backup.db_size_on_disk(settings.data_dir / "reaper.db"),
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


@router.get("/download")
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
        revision=summary.revision,
        verdict=summary.verdict,
        key_in_backup=summary.key_in_backup,
        reaper_db_bytes=summary.reaper_db_bytes,
        token=summary.token,
    )


@router.post("/restore/confirm")
async def restore_confirm(request: Request, payload: RestoreConfirmIn) -> dict[str, bool]:
    """Verify the admin password, then arm the staged restore.

    Gated exactly like arming deletion (:func:`reaper.api.settings.set_safety`): the
    same per-IP and per-account lockout and Argon2 concurrency gate, because a restore is
    as consequential as arming and its confirm is a password-guessing surface too. The
    ``token`` from the prepare summary binds this confirm to the exact backup reviewed, so
    a backup swapped in by another session since cannot be armed by this password (rule 73).
    On success the staged database is forced read-only, its inherited sessions are cleared,
    and the swap is armed; the operator restarts the container to finish.
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
            for key in keys:
                password_throttle.record_failure(key)
            raise HTTPException(403, "That password didn't match. Nothing was restored.")
        for key in keys:
            password_throttle.record_success(key)

    try:
        await asyncio.to_thread(restore.arm, settings, payload.token)
    except restore.RestoreError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    log.warning("restore.confirmed")
    return {"ok": True}


@router.post("/restore/cancel")
async def restore_cancel(request: Request) -> dict[str, bool]:
    """Discard a staged or armed restore. Turns off the "restart to finish" state."""
    await asyncio.to_thread(restore.clear_pending, _settings(request))
    log.info("restore.canceled")
    return {"ok": True}
