# SPDX-License-Identifier: AGPL-3.0-or-later
"""Building a portable backup of everything Reaper cannot rebuild.

A backup is one gzip-compressed tar with up to five members:

    manifest.json   what this archive is, and the schema revision it was cut at
    reaper.db       a consistent, compacted snapshot of the precious database
    secret.key      the master key that decrypts every stored credential -- present
                    when Reaper minted it into a file, absent when the operator
                    supplies ``REAPER_SECRET_KEY`` from the environment (then it is
                    never written to disk, so there is nothing to bundle)
    secret.salt     the per-install KDF salt paired with that key
    launcher.conf   the port, bind address and icon settings of an install that has
                    no other place to keep them (Windows, macOS, the snap). Present
                    whenever the file exists, so a backup carries everything its own
                    install shape stored; an install that does not read the file
                    simply ignores it after a restore

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

Two things build one: the operator pressing Download, and
:func:`snapshot_before_migration`, which runs at boot when a pending schema change asks to
be snapshotted first (#566). The second keeps its archives in ``data/pre-migration/`` and
prunes to the newest few; both write the same format, so either restores through the same
Settings -> Backup card.
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
from reaper.config import LAUNCHER_CONF_NAME, Settings
from reaper.db import schema_gate
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

#: Where :func:`snapshot_before_migration` leaves its archives, and how many it keeps.
#:
#: Under ``data/`` rather than anywhere else, because that is the folder an operator already
#: mounts, already backs up, and already knows to look in -- and because the move out of the
#: build's temp dir is then a rename on the same filesystem rather than a copy of the whole
#: database. Ordinary ``.reaper`` archives, so recovery is the restore the app already has
#: (Settings -> Backup, which accepts exactly this file) rather than a second path written
#: for this one case.
#:
#: Three, because the point of the file is the upgrade that just went wrong, and the release
#: before it, and one to spare. Each one is roughly the size of ``reaper.db`` compressed, and
#: they are only ever written by a schema change that asked for one, so the folder does not
#: grow on its own between releases.
PRE_MIGRATION_DIR = "pre-migration"
PRE_MIGRATION_PREFIX = "pre-migration-"
KEEP_PRE_MIGRATION = 3

#: What the operator is told when the snapshot could not be written. Declared here, beside
#: the code that fails, and printed by :mod:`reaper.preflight`, which is what turns it into a
#: refusal (rule 144). It says the update did not run, which preflight makes true by
#: returning non-zero before ``alembic upgrade head`` -- and it claims nothing about the state
#: of the data, because the staged-restore swap runs above this and may already have replaced
#: it (rule 126).
SNAPSHOT_FAILED = (
    "Reaper stopped. It couldn't save a backup of your database, and the update waiting to "
    "run changes the database, so Reaper didn't run it. Free up disk space in Reaper's data "
    "folder, then start Reaper again."
)


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


#: The Alembic revision a database file sits at, for the manifest this writes and for the
#: gate the restore side runs against it. One reader, in ``reaper.db.schema_gate``, because
#: the boot gate asks the same question of the LIVE database and a second copy of the query
#: is a second answer to drift from (rule 104). ``None`` means no ``alembic_version`` row:
#: production always has one, a database built straight from the models in a test has none,
#: and the restore side treats a missing revision conservatively.
_read_revision = schema_gate.stored_revision


def _build_sync(settings: Settings, created_at: str) -> BackupArchive:
    """The blocking build: snapshot the database, then tar+gzip it with the key files.

    Runs in a worker thread (see :func:`create_backup`) because ``VACUUM INTO`` and
    gzip of a multi-hundred-megabyte database would otherwise stall the event loop.
    """
    settings.ensure_data_dir()
    data_dir = settings.data_dir
    tmp_dir = Path(tempfile.mkdtemp(prefix=BACKUP_TMP_PREFIX, dir=data_dir))
    # Anything past mkdtemp that raises (VACUUM INTO failing on a full disk, a locked
    # database past the 5s busy timeout, a gzip write error) must not strand the temp dir
    # and its partial multi-GB snapshot -- that would make a disk-full worse (PR-3).
    try:
        return _build_into(settings, created_at, tmp_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _build_into(settings: Settings, created_at: str, tmp_dir: Path) -> BackupArchive:
    """Build the archive inside an already-created temp dir (see :func:`_build_sync`)."""
    snapshot = tmp_dir / DB_ARCNAME

    # A consistent, compacted copy of the live database. VACUUM INTO reads inside a
    # transaction, so a concurrent scan write cannot tear the snapshot; the 5s busy
    # timeout this connection sets lets it wait out an in-flight write rather than
    # failing at once. The figure is this connection's own, not the app engine's.
    con = sqlite3.connect(settings.database_path)
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
    # The launcher's settings, on the installs that have them. A Windows, macOS, or snap
    # operator sets the port, the bind address, and the tray icon HERE and nowhere else, so
    # a backup without it restores an install that has forgotten every one of them -- the
    # database carries none of it. Carried whenever the file exists rather than gated on the
    # install shape, so a backup taken anywhere holds everything that shape stored; a
    # container that restores it simply never reads the file (`launcher.reads_launcher_conf`).
    conf_path = settings.data_dir / LAUNCHER_CONF_NAME
    conf_included = conf_path.is_file()
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
            "launcher_conf": conf_included,
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
        if conf_included:
            tar.add(conf_path, arcname=LAUNCHER_CONF_NAME)

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


def _prune_pre_migration(directory: Path) -> None:
    """Keep the newest :data:`KEEP_PRE_MIGRATION` snapshots and remove the rest.

    Sorted by name, which is sorted by time: the stamp is ``YYYYMMDDTHHMMSS``, so the
    lexicographic order is the chronological one. A file that will not delete is reported
    and not raised on -- the snapshot this boot just wrote is already on disk, and refusing
    the migration over a stale file that would not go is a boot the operator loses for
    nothing.
    """
    snapshots = sorted(directory.glob(f"{PRE_MIGRATION_PREFIX}*.reaper"))
    for stale in snapshots[:-KEEP_PRE_MIGRATION]:
        try:
            stale.unlink()
        except OSError as exc:
            log.warning("backup.pre_migration_prune_failed", name=stale.name, error=str(exc))


def snapshot_before_migration(settings: Settings, revision: str | None) -> Path:
    """Copy the database into ``data/pre-migration/`` before a migration that asked for it.

    Called from :mod:`reaper.preflight`, which every way of starting Reaper runs immediately
    before ``alembic upgrade head`` -- the container entrypoint, ``scripts/dev-local.sh`` and
    :func:`reaper.launcher.main` alike, so one call site covers all three and none of them
    can drift out of it. Which migrations ask is
    :func:`reaper.db.schema_gate.needs_snapshot`.

    Raises rather than returning on failure, and preflight turns that into a refusal: a
    migration that can destroy data must not run with nothing to go back to, so a full disk
    stops the boot instead of taking the update unprotected.

    ``revision`` is the revision the database sits at, and it goes in the filename because
    that is the fact an operator needs when picking which snapshot to restore. The archive is
    a normal backup, key material and all, so it restores through Settings -> Backup like any
    other.
    """
    created_at = utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    archive = _build_sync(settings, created_at)
    try:
        # 0700 at creation, never widened afterwards: each archive carries `secret.key`, so
        # the folder holding them is as sensitive as the key (rule 14/83). `mkdir` masks with
        # the umask, which can only remove bits, so this is a ceiling and not a floor.
        directory = settings.data_dir / PRE_MIGRATION_DIR
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = created_at.replace("-", "").replace(":", "")[:15]  # 20260723T100000
        target = directory / f"{PRE_MIGRATION_PREFIX}{stamp}-{revision or 'unknown'}.reaper"
        # The build wrote it inside a 0700 mkdtemp, so it has never been reachable; this
        # keeps it owner-only if the operator later moves it somewhere that is.
        archive.path.chmod(0o600)
        shutil.move(str(archive.path), str(target))
    finally:
        cleanup(archive)
    _prune_pre_migration(directory)
    log.warning("backup.pre_migration_snapshot", name=target.name, revision=revision)
    return target


def sweep_stale_temp(settings: Settings) -> int:
    """Remove crash-leftover backup/restore temp entries under ``data/``. Returns the count.

    A healthy backup or restore removes its own temp dir or upload spool; anything under
    ``data/`` matching :data:`_STALE_TEMP_PREFIXES` at boot is debris from a crash mid-run
    (PR-3). Called from preflight, which runs before the app starts, so nothing is in flight
    to race. Only the dotted temp prefixes match, so the ``pending-restore`` staging, the
    ``pre-restore-*`` recovery copies and the ``pre-migration/`` snapshots (none of them
    dotted) are never touched.
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
