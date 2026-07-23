# SPDX-License-Identifier: AGPL-3.0-or-later
"""Putting a backup back: validate an uploaded archive, stage it, swap it in on boot.

The other half of :mod:`reaper.services.backup`. Restore is deliberately a
*stage-and-restart* operation, never a live swap:

1. **Prepare** (:func:`stage_upload`) unpacks an uploaded archive into
   ``data/pending-restore/``, but only after it proves the file is a real Reaper
   backup whose schema this build can serve. Nothing is armed yet: without the
   ``READY`` marker the staged files are inert, so a half-finished or abandoned
   upload can never be swapped in.
2. **Arm** (:func:`arm`) runs only after the admin password is verified at the API
   edge. It forces deletion OFF inside the staged database (so the restored install
   boots read-only, never armed on someone else's decision) and then writes the
   ``READY`` marker *last* -- the marker is the arm.
3. **Swap** (:func:`apply_pending_restore`) runs at container start, before
   migrations, from the entrypoint's preflight. If ``READY`` is present and the
   staged database reads as SQLite, it moves the current data aside (kept for
   recovery) and moves the staged files into place. ``alembic upgrade head`` then
   brings the restored database current.

Every ambiguity resolves toward keeping the live data. A staged database that does
not read as SQLite is discarded rather than swapped; an upload from a newer Reaper
than this one is refused before it is ever staged, because this build could not run
its schema.

The schema gate is the load-bearing safety check. A backup carries the Alembic
revision it was cut at. This build knows a fixed set of revisions (the migration
scripts shipped in the image). If the backup's revision is one we know, boot's
``alembic upgrade head`` can carry it forward. If it is unknown, the backup came
from a *newer* Reaper with migrations this build does not have -- restoring it would
serve a schema the code cannot understand, so it is refused.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.secrets import KEY_FILENAME, SALT_FILENAME
from reaper.services.app_settings import DESTRUCTIVE_KEY
from reaper.services.backup import BACKUP_FORMAT, DB_ARCNAME, MANIFEST_NAME

log = structlog.get_logger(__name__)

#: The staging directory under ``data/`` and the marker that arms the swap. The marker
#: is written last (see :func:`arm`), so its mere presence means "a verified, password-
#: confirmed restore is ready" -- an interrupted upload never leaves one behind.
PENDING_DIR = "pending-restore"
READY_MARKER = "READY"

#: What the current data is moved into before the staged copy takes its place, so a bad
#: restore is recoverable. Timestamped, and never touched again by Reaper.
PRE_RESTORE_PREFIX = "pre-restore-"

#: A SQLite file starts with this 16-byte string. Used to refuse a staged "database"
#: that is not one *before* it could ever replace the live database.
SQLITE_MAGIC = b"SQLite format 3\x00"

_COPY_CHUNK = 1024 * 1024

#: Per-member ceilings while unpacking an untrusted archive. A gzip member can claim a
#: modest size and expand without bound, so the copy is capped as it runs -- a decompression
#: bomb hits the ceiling and is refused rather than filling the disk. The database ceiling is
#: generous (a large real library is well under it); the key, salt, and manifest are tiny.
_MEMBER_CAPS = {
    MANIFEST_NAME: 1 * 1024 * 1024,
    DB_ARCNAME: 8 * 1024 * 1024 * 1024,
    KEY_FILENAME: 64 * 1024,
    SALT_FILENAME: 64 * 1024,
}


class RestoreError(Exception):
    """A restore that must not proceed, carrying operator-facing copy and an HTTP status.

    Raised toward keeping the live data: a malformed file, a newer-version backup, or a
    staged copy that cannot be verified all become one of these rather than a swap.
    """

    def __init__(self, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RestoreSummary:
    """What an accepted, staged backup is -- for the operator to confirm before arming."""

    app_version: str | None
    """The Reaper version that wrote the backup, for the "From ..." line."""
    created_at: str | None
    """When the backup was taken (ISO 8601, UTC), or ``None`` if the file didn't say."""
    revision: str | None
    """The schema revision the backup sits at. Always known to this build once accepted."""
    verdict: str
    """``"current"`` when the backup matches this build's schema, ``"older"`` when this
    build will upgrade it on the next boot. Both are safe to restore."""
    key_in_backup: bool
    """Whether the encryption key travels inside the backup. ``False`` for an env-supplied
    key, so the target must set ``REAPER_SECRET_KEY`` or saved credentials won't decrypt."""
    reaper_db_bytes: int
    """The staged database size on disk."""


# ---------------------------------------------------------------------------
# The schema gate: which revisions this build knows how to serve.
# ---------------------------------------------------------------------------


def _alembic_dir() -> Path:
    """Locate the shipped ``alembic/`` directory in both dev and the container.

    It sits at the project root beside ``src/`` (dev) or is copied to the image
    workdir (``/app/alembic``). Walk up from this module until the migration
    environment is found, so the gate never depends on the current directory.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "alembic" / "env.py").is_file():
            return parent / "alembic"
    raise RestoreError(
        "Reaper couldn't check this backup against its own version. Try again.",
        status=500,
    )


def known_revisions() -> frozenset[str]:
    """Every Alembic revision id this build ships, from the migration scripts.

    The migration modules import only Alembic and SQLAlchemy, so walking them is
    side-effect free. A backup whose revision is in this set can be carried forward
    by ``alembic upgrade head``; one that is not came from a newer Reaper.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(_alembic_dir()))
    script = ScriptDirectory.from_config(config)
    return frozenset(revision.revision for revision in script.walk_revisions())


def _current_head() -> str | None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(_alembic_dir()))
    try:
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:  # head is cosmetic; a miss just reads as "older"
        return None


def _check_schema(revision: str | None) -> str:
    """Refuse a backup this build cannot serve; return the verdict for one it can.

    ``None`` means the snapshot carried no ``alembic_version`` row -- a corrupt or
    foreign database, refused. A revision this build does not know came from a newer
    Reaper, also refused (409). Otherwise it is ``"current"`` (matches head) or
    ``"older"`` (an ancestor this build will upgrade on boot).
    """
    if revision is None:
        raise RestoreError(
            "The database in this backup couldn't be verified. "
            "It may be damaged, or not a Reaper backup."
        )
    try:
        known = known_revisions()
    except RestoreError:
        raise
    except Exception as exc:  # any failure to read the migrations fails closed
        log.warning("restore.schema_unverifiable", error=str(exc))
        raise RestoreError(
            "Reaper couldn't check this backup against its own version. "
            "Try again, or update Reaper."
        ) from exc
    if revision not in known:
        raise RestoreError(
            "This backup was made by a newer version of Reaper than the one running. "
            "Update Reaper to that version or later, then restore.",
            status=409,
        )
    return "current" if revision == _current_head() else "older"


# ---------------------------------------------------------------------------
# Prepare: unpack and validate an uploaded archive into the staging directory.
# ---------------------------------------------------------------------------


def _copy_capped(source: Any, out_path: Path, cap: int) -> int:
    """Copy one archive member to disk, refusing anything past ``cap`` bytes."""
    written = 0
    with out_path.open("wb") as out:
        while chunk := source.read(_COPY_CHUNK):
            written += len(chunk)
            if written > cap:
                raise RestoreError("This backup is larger than Reaper can restore.")
            out.write(chunk)
    return written


def _extract(archive_path: Path, dest: Path) -> dict[str, Any]:
    """Unpack the known members of an archive into ``dest`` and return the manifest.

    Only the four expected members are extracted, each to a fixed bare filename under
    ``dest`` -- a member's own path is never honored, so a crafted archive cannot write
    outside the staging directory. The manifest and the database are required; the key
    and salt are optional (absent for an env-supplied key).
    """
    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar:
                if member.name not in _MEMBER_CAPS:
                    continue  # ignore anything unexpected; never trust a member's path
                if not member.isfile():
                    raise RestoreError("This backup file is malformed.")
                stream = tar.extractfile(member)
                if stream is None:
                    raise RestoreError("This backup file is malformed.")
                _copy_capped(stream, dest / member.name, _MEMBER_CAPS[member.name])
                seen.add(member.name)
    except RestoreError:
        raise  # a validation refusal, not a read failure -- keep its message
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise RestoreError("This isn't a readable Reaper backup file.") from exc

    if MANIFEST_NAME not in seen or DB_ARCNAME not in seen:
        raise RestoreError("This isn't a Reaper backup: some of its contents are missing.")

    try:
        manifest = json.loads((dest / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise RestoreError("This backup's description couldn't be read.") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise RestoreError("This isn't a Reaper backup file.")

    with (dest / DB_ARCNAME).open("rb") as handle:
        if handle.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
            raise RestoreError("The database inside this backup isn't readable.")

    return manifest


def _opt_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _summarize(manifest: dict[str, Any], staged: Path) -> RestoreSummary:
    """Turn a validated manifest into the summary the operator confirms against.

    Runs the schema gate (raising on a backup this build can't serve) and refuses a
    manifest that claims a bundled key but ships none -- a tampered or truncated archive.
    """
    revision = _opt_str(manifest.get("alembic_revision"))
    verdict = _check_schema(revision)

    key_in_backup = (staged / KEY_FILENAME).is_file()
    if manifest.get("key_source") == "file" and not key_in_backup:
        raise RestoreError("This backup is missing its encryption key and can't be restored.")

    return RestoreSummary(
        app_version=_opt_str(manifest.get("app_version")),
        created_at=_opt_str(manifest.get("created_at")),
        revision=revision,
        verdict=verdict,
        key_in_backup=key_in_backup,
        reaper_db_bytes=(staged / DB_ARCNAME).stat().st_size,
    )


def _replace_dir(target: Path, source: Path) -> None:
    """Atomically make ``source`` the new ``target`` directory (same filesystem)."""
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    source.replace(target)


def stage_upload(settings: Settings, archive_path: Path) -> RestoreSummary:
    """Validate an uploaded archive and stage it, un-armed, for a later confirm.

    Extraction and validation happen in a private temp directory; only a fully
    accepted backup replaces the staging directory. On any refusal the temp directory
    is removed and no staging is touched, so a rejected upload leaves the prior state
    (and any live data) exactly as it was. The staged copy carries no ``READY`` marker
    -- it cannot be swapped in until :func:`arm` writes one.
    """
    settings.ensure_data_dir()
    data_dir = settings.data_dir
    tmp = Path(tempfile.mkdtemp(prefix=".restore-tmp-", dir=data_dir))
    staged = False
    try:
        manifest = _extract(archive_path, tmp)
        summary = _summarize(manifest, tmp)
        _replace_dir(data_dir / PENDING_DIR, tmp)
        staged = True  # tmp was renamed into place; the finally must not remove it
        log.info("restore.staged", revision=summary.revision, verdict=summary.verdict)
        return summary
    finally:
        if not staged:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Arm: force deletion off in the staged database, then write the READY marker.
# ---------------------------------------------------------------------------


def _force_destructive_off(db_path: Path) -> None:
    """Set ``destructive_enabled = false`` in the staged database.

    So the restored install boots read-only whatever the backup had stored. Writes the
    value the way :mod:`reaper.services.app_settings` reads it -- JSON in ``value_json``,
    an integer epoch in ``updated_at`` -- reusing :data:`DESTRUCTIVE_KEY` so the key can't
    drift from the reader.
    """
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO app_setting (key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value_json = excluded.value_json, updated_at = excluded.updated_at",
            (DESTRUCTIVE_KEY, json.dumps(False), int(utcnow().timestamp())),
        )
        con.commit()
    except sqlite3.OperationalError as exc:
        raise RestoreError("Reaper couldn't prepare this backup to restore.") from exc
    finally:
        con.close()


def is_armed(settings: Settings) -> bool:
    """Whether a verified restore is staged and armed, waiting for a restart."""
    return (settings.data_dir / PENDING_DIR / READY_MARKER).is_file()


def arm(settings: Settings) -> None:
    """Force deletion off in the staged database and arm the swap.

    Called only after the admin password is verified at the API edge. The ``READY``
    marker is written last, so a crash between forcing deletion off and arming leaves
    the staging inert rather than half-armed.
    """
    pending = settings.data_dir / PENDING_DIR
    staged_db = pending / DB_ARCNAME
    if not _looks_like_sqlite(staged_db):
        raise RestoreError("There's no backup ready to restore. Choose a file first.")
    _force_destructive_off(staged_db)
    (pending / READY_MARKER).write_text(
        utcnow().strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8"
    )
    log.warning("restore.armed")


def clear_pending(settings: Settings) -> None:
    """Discard a staged (or armed) restore. Safe to call when nothing is staged."""
    shutil.rmtree(settings.data_dir / PENDING_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# Swap: run at container start, before migrations, from preflight.
# ---------------------------------------------------------------------------


def _looks_like_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def _unique_dir(parent: Path, name: str) -> Path:
    """A directory named ``name`` under ``parent``, suffixed if that already exists."""
    candidate = parent / name
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{name}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def apply_pending_restore(settings: Settings) -> bool:
    """Swap a staged, armed backup into place. Returns whether a swap happened.

    Run at boot before migrations. Resolves toward keeping the live data: if the
    staged database does not read as SQLite, the whole staging is discarded and the
    live data is left untouched. Otherwise the current database, its write-ahead log,
    and the key and salt are moved into a timestamped ``pre-restore-*`` directory for
    recovery, and the staged files take their place. ``alembic upgrade head`` (next in
    the entrypoint) then brings the restored database current.
    """
    data_dir = settings.data_dir
    pending = data_dir / PENDING_DIR
    if not (pending / READY_MARKER).is_file():
        return False

    staged_db = pending / DB_ARCNAME
    if not _looks_like_sqlite(staged_db):
        # Armed but unusable: discard it and keep the live data. Fail closed.
        shutil.rmtree(pending, ignore_errors=True)
        sys.stderr.write(
            "reaper: a staged restore was unreadable and was discarded; current data kept\n"
        )
        return False

    recovery = _unique_dir(data_dir, f"{PRE_RESTORE_PREFIX}{utcnow().strftime('%Y%m%dT%H%M%SZ')}")
    # Move the live copy aside first (its -wal/-shm travel with it), then move the
    # staged copy in. Same filesystem, so each move is an atomic rename.
    for name in (DB_ARCNAME, f"{DB_ARCNAME}-wal", f"{DB_ARCNAME}-shm", KEY_FILENAME, SALT_FILENAME):
        live = data_dir / name
        if live.exists():
            shutil.move(str(live), str(recovery / name))
    for name in (DB_ARCNAME, KEY_FILENAME, SALT_FILENAME):
        staged = pending / name
        if staged.exists():
            shutil.move(str(staged), str(data_dir / name))
    shutil.rmtree(pending, ignore_errors=True)

    sys.stderr.write(
        "reaper: restored the database from a staged backup; the previous data is saved in "
        f"data/{recovery.name} (remove it once the restore looks good)\n"
    )
    return True
