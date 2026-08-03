# SPDX-License-Identifier: AGPL-3.0-or-later
"""Building a portable backup of everything Reaper cannot rebuild.

A backup is one gzip-compressed tar with up to four members:

    manifest.json   what this archive is, and the schema revision it was cut at
    reaper.db       a consistent, compacted snapshot of the precious database
    secret.key      the master key that decrypts every stored credential -- present
                    when Reaper minted it into a file, absent when the operator
                    supplies ``REAPER_SECRET_KEY`` from the environment (then it is
                    never written to disk, so there is nothing to bundle)
    secret.salt     the per-install KDF salt paired with that key

The key and salt travel WITH the database on purpose. Without them a restored
``reaper.db`` cannot decrypt a single stored credential (see :mod:`reaper.crypto`
and :mod:`reaper.secrets`), so a database-only backup silently loses every
Sonarr/Radarr key and the Plex token. Bundling them makes the backup
self-sufficient: dropped onto a fresh install it just decrypts. The cost is that
the file is as sensitive as the key itself -- which is why the download route is
fenced off the API-key lane and the operator copy says to guard it like a password.

The cache database is deliberately left out: it is large and rebuilds itself from
Tautulli, the IMDb dataset, and the lists on the next scan (see
``Settings.cache_database_url``). Backing it up would multiply the size for data
that is not a source of truth.

The snapshot is taken with SQLite's ``VACUUM INTO``: it reads a consistent view
inside one transaction (so a scan writing mid-backup cannot tear the copy), writes
one defragmented file with no side ``-wal`` / ``-shm``, and drops free pages so the
archive is smaller than the live database on disk.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from reaper.buildinfo import build_version
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.secrets import (
    KEY_FILENAME,
    SALT_FILENAME,
    env_key_active,
    key_file_path,
    salt_file_path,
)

log = structlog.get_logger(__name__)

MANIFEST_NAME = "manifest.json"
DB_ARCNAME = "reaper.db"
BACKUP_FORMAT = "reaper-backup"
BACKUP_FORMAT_VERSION = 1

#: Streamed to the browser in modest chunks so a large archive never sits in memory.
DOWNLOAD_CHUNK = 64 * 1024

#: The largest ``reaper.db`` that can travel in a backup, enforced on BOTH sides from this
#: one constant: the restore caps the extracted member here, and :func:`_build_into`
#: refuses to write an archive past it. A backup its own restore would reject is worse
#: than no backup, because the operator believes they are covered until the day they are
#: not (PR-11). 64 GiB is far above any real install -- ``reaper.db`` holds decisions and
#: credentials, never media, and the rebuildable cache is not in here at all -- and the
#: restore's extraction is streamed and capped as it runs, so the ceiling costs nothing to
#: raise. It is a decompression-bomb bound, not a sizing estimate.
MAX_DB_BYTES = 64 * 1024 * 1024 * 1024

#: Temp-entry prefixes under ``data/``. A backup builds in :data:`BACKUP_TMP_PREFIX`; the
#: restore side stages in :data:`RESTORE_TMP_PREFIX` and spools its upload in
#: :data:`RESTORE_UPLOAD_PREFIX` (both used by :mod:`reaper.services.restore` and
#: :mod:`reaper.api.backup`, imported from here so the vocabulary lives in one place). All
#: three name transient work a healthy run removes itself; anything matching them at boot
#: is crash debris that :func:`sweep_stale_temp` clears (PR-3).
BACKUP_TMP_PREFIX = ".backup-tmp-"
RESTORE_TMP_PREFIX = ".restore-tmp-"
RESTORE_UPLOAD_PREFIX = ".restore-upload-"
_STALE_TEMP_PREFIXES = (BACKUP_TMP_PREFIX, RESTORE_TMP_PREFIX, RESTORE_UPLOAD_PREFIX)


class BackupTooLargeError(RuntimeError):
    """The database is past :data:`MAX_DB_BYTES`, so no archive was written.

    Refusing beats writing a file the restore side will not accept: the operator finds out
    now, while they still have every option, rather than during a recovery (PR-11).
    """


@dataclass(frozen=True)
class BackupArchive:
    """A built archive on disk, plus the facts the caller needs to serve and log it.

    ``tmp_dir`` is the caller's to remove once the file has been streamed out
    (see :func:`cleanup`); everything else describes the archive.
    """

    path: Path
    tmp_dir: Path
    filename: str
    created_at: str
    revision: str | None
    manifest: dict[str, Any]


def db_size_on_disk(base: Path) -> int:
    """The size of one SQLite database on disk, including its live WAL.

    The ``-wal`` file holds committed writes not yet checkpointed into the main
    file, so counting the bare ``.db`` alone under-reports what the disk holds and
    what a backup will weigh.
    """
    total = 0
    for path in (base, base.with_name(base.name + "-wal")):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _read_revision(db_path: Path) -> str | None:
    """The Alembic revision a snapshot sits at, or ``None`` when the table is absent.

    Production always has an ``alembic_version`` row; a database built straight from
    the models in a test carries none. The restore side treats a missing revision
    conservatively, so reporting ``None`` here is safe rather than a hard error.
    """
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    return str(row[0]) if row and row[0] else None


def _build_sync(settings: Settings, created_at: str) -> BackupArchive:
    """The blocking build: snapshot the database, then tar+gzip it with the key files.

    Runs in a worker thread (see :func:`create_backup`) because ``VACUUM INTO`` and
    gzip of a multi-hundred-megabyte database would otherwise stall the event loop.
    """
    settings.ensure_data_dir()
    data_dir = settings.data_dir
    tmp_dir = Path(tempfile.mkdtemp(prefix=BACKUP_TMP_PREFIX, dir=data_dir))
    # Anything past mkdtemp that raises (VACUUM INTO failing on a full disk, a locked
    # database past the busy timeout, a gzip write error) must not strand the temp dir
    # and its partial multi-GB snapshot -- that would make a disk-full worse (PR-3).
    try:
        return _build_into(settings, created_at, tmp_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _build_into(settings: Settings, created_at: str, tmp_dir: Path) -> BackupArchive:
    """Build the archive inside an already-created temp dir (see :func:`_build_sync`)."""
    data_dir = settings.data_dir
    snapshot = tmp_dir / DB_ARCNAME

    # A consistent, compacted copy of the live database. VACUUM INTO reads inside a
    # transaction, so a concurrent scan write cannot tear the snapshot; the short
    # busy timeout lets it wait out an in-flight write rather than failing at once.
    con = sqlite3.connect(data_dir / "reaper.db")
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("VACUUM INTO ?", (str(snapshot),))
    finally:
        con.close()

    # The active key decides what travels. An env-supplied key always wins over any file
    # on disk (resolve_secret_key precedence), so a lingering secret.key is inactive: never
    # bundle it, and report key_source "env" so the target is told it still needs the env
    # var (rule 76). The salt is install state, minted even for an env key, so it travels
    # whenever it exists.
    snapshot_bytes = snapshot.stat().st_size
    if snapshot_bytes > MAX_DB_BYTES:
        raise BackupTooLargeError(
            "Reaper's database is larger than a backup can hold, so this backup was not "
            "written. A backup that can't be restored is worse than none."
        )

    env_key = env_key_active(settings)
    key_path = key_file_path(settings)
    salt_path = salt_file_path(settings)
    key_included = key_path.is_file() and not env_key
    salt_included = salt_path.is_file()
    revision = _read_revision(snapshot)

    manifest: dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": created_at,
        "app_version": build_version(),
        "alembic_revision": revision,
        "reaper_db_bytes": snapshot_bytes,
        "key_source": "file" if key_included else "env",
        "contents": {
            "reaper_db": True,
            "secret_key": key_included,
            "secret_salt": salt_included,
            "cache_db": False,
        },
    }
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")

    stamp = created_at.replace("-", "").replace(":", "")[:15]  # 20260723T100000
    archive_path = tmp_dir / f"reaper-backup-{stamp}.reaper"
    with tarfile.open(archive_path, "w:gz") as tar:
        # Manifest first so a restore can read it from the front of the stream without
        # unpacking the (large) database that follows.
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest_bytes)
        info.mtime = 0
        tar.addfile(info, io.BytesIO(manifest_bytes))
        tar.add(snapshot, arcname=DB_ARCNAME)
        if key_included:
            tar.add(key_path, arcname=KEY_FILENAME)
        if salt_included:
            tar.add(salt_path, arcname=SALT_FILENAME)

    # The snapshot now lives inside the archive; drop the loose copy so the temp dir
    # holds one file while the (possibly large) archive is streamed out.
    snapshot.unlink(missing_ok=True)

    return BackupArchive(
        path=archive_path,
        tmp_dir=tmp_dir,
        filename=archive_path.name,
        created_at=created_at,
        revision=revision,
        manifest=manifest,
    )


async def create_backup(settings: Settings) -> BackupArchive:
    """Build the backup archive off the event loop and hand back where it landed."""
    created_at = utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return await asyncio.to_thread(_build_sync, settings, created_at)


def cleanup(archive: BackupArchive) -> None:
    """Remove the temp dir the archive was built in. Safe to call more than once."""
    shutil.rmtree(archive.tmp_dir, ignore_errors=True)


def sweep_stale_temp(settings: Settings) -> int:
    """Remove crash-leftover backup/restore temp entries under ``data/``. Returns the count.

    A healthy backup or restore removes its own temp dir or upload spool; anything under
    ``data/`` matching :data:`_STALE_TEMP_PREFIXES` at boot is debris from a crash mid-run
    (PR-3). Called from preflight, which runs before the app starts, so nothing is in flight
    to race. Only the dotted temp prefixes match, so the ``pending-restore`` staging and the
    ``pre-restore-*`` recovery copies (no leading dot) are never touched.
    """
    swept = 0
    try:
        entries = list(settings.data_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(_STALE_TEMP_PREFIXES):
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue
        swept += 1
    return swept
