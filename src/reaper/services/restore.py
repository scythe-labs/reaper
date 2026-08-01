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
3. **Swap** (:func:`apply_pending_restore`) runs at start, before migrations, from
   :mod:`reaper.preflight` -- which the container entrypoint and ``scripts/dev-local.sh``
   both run, and which nothing else may skip. If ``READY`` is present and the
   staged database reads as SQLite, it moves the current data aside (kept for
   recovery) and moves the staged files into place. ``alembic upgrade head`` then
   brings the restored database current.

The operator asks for that start from the browser (``POST
/api/settings/backup/restore/restart``, which stops the process and lets the container's
restart policy bring it back) or by restarting the container themselves. Either way this
module's part is identical: it is armed, and the next boot swaps.

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

import hmac
import json
import os
import re
import secrets as pysecrets
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
from reaper.db.models import AUTH_BEARING_TABLES
from reaper.secrets import KEY_FILENAME, SALT_FILENAME
from reaper.services.app_settings import DESTRUCTIVE_KEY
from reaper.services.backup import (
    BACKUP_FORMAT,
    DB_ARCNAME,
    MANIFEST_NAME,
    MAX_DB_BYTES,
    RESTORE_TMP_PREFIX,
    _read_revision,
)

log = structlog.get_logger(__name__)

#: The staging directory under ``data/`` and the marker that arms the swap. The marker
#: is written last (see :func:`arm`), so its mere presence means "a verified, password-
#: confirmed restore is ready" -- an interrupted upload never leaves one behind.
PENDING_DIR = "pending-restore"
READY_MARKER = "READY"

#: Written just before the first file is moved, and removed only when the last one is.
#: Its presence at boot means an earlier boot was killed PART WAY THROUGH the swap, which
#: is the one state where "the staged database is missing" must not be read as "the
#: staging is broken": the staged database is missing because it is already the live one
#: (B2-21). Discarding there would delete the backup's secret.key -- the only copy of the
#: key for the database now serving -- while printing that the current data was kept.
#: It carries the ``pre-restore-*`` directory name, so a resumed boot can name it.
SWAP_MARKER = "SWAPPING"

#: Binds a password-confirmed arm to the exact content the operator reviewed. Minted per
#: staging (see :func:`stage_upload`), returned in the summary, and required back by
#: :func:`arm` -- if a second session re-stages between review and confirm, the staging (and
#: this token) is replaced, so the stale token no longer matches and the arm is refused
#: (rule 73). Not a secret: a nonce that says "still the same staged backup," living in the
#: 0700 staging dir.
TOKEN_MARKER = "TOKEN"  # noqa: S105 -- a marker filename, not a secret

#: How wide a staging token is, minted here and bounded at the API edge off this same
#: declaration so the producer and the fields that accept it cannot drift apart (rules 95
#: and 131). Hex, so twice the byte count.
TOKEN_BYTES = 32
TOKEN_MAX_LEN = TOKEN_BYTES * 2

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
#: the same constant the BACKUP side refuses to exceed, so this build can always restore what
#: it can produce (PR-11); the key, salt, and manifest are tiny.
_MEMBER_CAPS = {
    MANIFEST_NAME: 1 * 1024 * 1024,
    DB_ARCNAME: MAX_DB_BYTES,
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
    token: str
    """The staging token (see :data:`TOKEN_MARKER`). The operator hands it back at confirm
    time, and :func:`arm` refuses if the staged content changed since this summary was cut."""


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


def _member_writer(out_path: Path, *, owner_only: bool) -> Any:
    """Open an extracted member for writing, owner-only from creation when it is a secret.

    The key and salt must be 0600 the instant they exist: the 0700 staging dir shields them
    while staged, but ``apply_pending_restore`` moves them into the data dir (a host bind
    mount) where a write-then-chmod window would leave the master key world-readable through
    boot and migrations (rule 83/14). ``os.open`` with ``O_EXCL`` and mode 0600 closes that
    window; the staging dir is freshly made and empty, so ``O_EXCL`` never clashes.
    """
    if not owner_only:
        return out_path.open("wb")
    old_umask = os.umask(0o077)
    try:
        fd = os.open(str(out_path), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    finally:
        os.umask(old_umask)
    return os.fdopen(fd, "wb")


def _copy_capped(source: Any, out_path: Path, cap: int, *, owner_only: bool = False) -> int:
    """Copy one archive member to disk, refusing anything past ``cap`` bytes."""
    written = 0
    with _member_writer(out_path, owner_only=owner_only) as out:
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
                secret = member.name in (KEY_FILENAME, SALT_FILENAME)
                _copy_capped(
                    stream, dest / member.name, _MEMBER_CAPS[member.name], owner_only=secret
                )
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


def _summarize(manifest: dict[str, Any], staged: Path, token: str) -> RestoreSummary:
    """Turn a validated manifest into the summary the operator confirms against.

    Runs the schema gate (raising on a backup this build can't serve) and refuses a
    manifest that claims a bundled key but ships none -- a tampered or truncated archive.

    The schema gate reads the staged database's OWN ``alembic_version``, never the
    manifest's claim: a repacked archive whose manifest names a known revision while its
    database is any other SQLite file would otherwise pass and be swapped in, then boot's
    ``alembic upgrade head`` runs against a mismatched schema (rule 74). The manifest's
    claim, when present, must agree with the artifact or the backup is refused as altered.
    """
    db_revision = _read_revision(staged / DB_ARCNAME)
    if db_revision is None:
        raise RestoreError(
            "The database in this backup couldn't be verified. "
            "It may be damaged, or not a Reaper backup."
        )
    manifest_revision = _opt_str(manifest.get("alembic_revision"))
    if manifest_revision is not None and manifest_revision != db_revision:
        raise RestoreError(
            "This backup's description doesn't match the database inside it. "
            "It may be damaged or altered."
        )
    verdict = _check_schema(db_revision)

    key_in_backup = (staged / KEY_FILENAME).is_file()
    if manifest.get("key_source") == "file" and not key_in_backup:
        raise RestoreError("This backup is missing its encryption key and can't be restored.")

    return RestoreSummary(
        app_version=_opt_str(manifest.get("app_version")),
        created_at=_opt_str(manifest.get("created_at")),
        revision=db_revision,
        verdict=verdict,
        key_in_backup=key_in_backup,
        reaper_db_bytes=(staged / DB_ARCNAME).stat().st_size,
        token=token,
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
    tmp = Path(tempfile.mkdtemp(prefix=RESTORE_TMP_PREFIX, dir=data_dir))
    staged = False
    try:
        manifest = _extract(archive_path, tmp)
        # Mint the staging token and write it beside the staged files, so it travels with
        # the atomic rename below and binds this exact staging to the confirm (rule 73).
        token = pysecrets.token_hex(TOKEN_BYTES)
        (tmp / TOKEN_MARKER).write_text(token + "\n", encoding="utf-8")
        summary = _summarize(manifest, tmp, token)
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


#: The identifier pattern a purgeable table name must match. The ``DELETE`` below is built
#: by interpolation because SQLite has no bind parameter for a table name, so the name is
#: proved to be a bare identifier first rather than trusted for coming from our own module
#: -- the check costs nothing and does not depend on where the list came from.
_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _purge_auth_state(db_path: Path) -> None:
    """Clear sessions, recovery tokens, and pending logins from the staged database.

    A restore replaces the whole database, which is a wholesale credential change: the
    backup's password hash returns, so its live sessions and recovery tokens must not
    (rule 75/12). Otherwise a session or reset link valid when the backup was taken would
    work again after the swap, defeating a later sign-out-everywhere. Runs in the same
    fail-closed step as forcing deletion off; a table absent in an older backup simply has
    nothing to purge.

    Which tables those are is declared beside the models
    (:data:`~reaper.db.models.AUTH_BEARING_TABLES`), not here, so a new one cannot be
    added to the schema and silently ride a restore through (R-3).
    """
    con = sqlite3.connect(db_path)
    try:
        for table in AUTH_BEARING_TABLES:
            if not _TABLE_NAME.match(table):  # pragma: no cover -- guards the f-string below
                raise RestoreError("Reaper couldn't prepare this backup to restore.")
            present = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            if present:
                con.execute(f"DELETE FROM {table}")  # noqa: S608 -- identifier checked above
        con.commit()
    except sqlite3.OperationalError as exc:
        raise RestoreError("Reaper couldn't prepare this backup to restore.") from exc
    finally:
        con.close()


def _token_matches(pending: Path, provided: str | None) -> bool:
    """Whether ``provided`` equals the token minted when this staging was created."""
    if not provided:
        return False
    try:
        stored = (pending / TOKEN_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(stored) and hmac.compare_digest(stored, provided.strip())


def is_armed(settings: Settings) -> bool:
    """Whether a verified restore is staged and armed, waiting for a restart."""
    return (settings.data_dir / PENDING_DIR / READY_MARKER).is_file()


def arm(settings: Settings, token: str | None) -> None:
    """Force deletion off in the staged database and arm the swap.

    Called only after the admin password is verified at the API edge. ``token`` must equal
    the one minted for this staging (returned in the summary the operator reviewed): if a
    different backup was staged since, the token no longer matches and the arm is refused,
    so the password can only ever arm the content that was actually reviewed (rule 73). The
    ``READY`` marker is written last, so a crash between preparing the database and arming
    leaves the staging inert rather than half-armed.
    """
    pending = settings.data_dir / PENDING_DIR
    staged_db = pending / DB_ARCNAME
    if not _looks_like_sqlite(staged_db):
        raise RestoreError("There's no backup ready to restore. Choose a file first.")
    if not _token_matches(pending, token):
        raise RestoreError(
            "The staged backup changed since you reviewed it. Check it again before restoring.",
            status=409,
        )
    _force_destructive_off(staged_db)
    _purge_auth_state(staged_db)
    (pending / READY_MARKER).write_text(
        utcnow().strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8"
    )
    log.warning("restore.armed")


def clear_pending(settings: Settings, token: str | None = None) -> bool:
    """Discard a staged (or armed) restore. Safe to call when nothing is staged.

    ``token`` scopes the discard to one staging, the way :func:`arm` scopes the swap to the
    content that was reviewed (rule 73). With a token, the discard happens only if that token
    is still the one minted for what is staged now; a staging replaced since belongs to
    whoever staged it, and this returns ``False`` having removed nothing. Without a token the
    discard is unconditional, which is what a deliberate Cancel on an armed restore needs: it
    holds no summary and no token, and it must still be able to clear anything.

    Returns whether a staging was actually removed, so the route can say what happened rather
    than reporting an ownership refusal -- or a call that found nothing at all -- as a discard
    (#387). Both of those are ordinary arrivals here rather than errors: this is reached from
    an unmount, and the operator may have cleared the staging from anywhere else first.
    """
    pending = settings.data_dir / PENDING_DIR
    if token is not None and not _token_matches(pending, token):
        log.info("restore.cancel_not_owned")
        return False
    existed = pending.exists()
    shutil.rmtree(pending, ignore_errors=True)
    return existed


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
    candidate.mkdir(parents=True, mode=0o700)
    return candidate


#: SQLite keeps the database's recent transactions in a sidecar write-ahead log, and
#: recovers from it on the next open. It validates that log by its own header and frame
#: checksums alone -- never by any binding to the database file beside it -- so a WAL left
#: over from a DIFFERENT database replays into whatever ``reaper.db`` it finds. That is
#: why these travel with the database everywhere below, and never on their own.
_DB_SIDECARS = (f"{DB_ARCNAME}-wal", f"{DB_ARCNAME}-shm")
_LIVE_FILES = (DB_ARCNAME, *_DB_SIDECARS, KEY_FILENAME, SALT_FILENAME)


def _move_aside(data_dir: Path, recovery: Path, names: tuple[str, ...]) -> None:
    """Move the named live files into ``recovery``. Idempotent.

    A name already gone is skipped, so an interrupted swap can finish by simply running
    this again over the same set.
    """
    for name in names:
        live = data_dir / name
        if live.exists():
            shutil.move(str(live), str(recovery / name))


def _recovery_dir(data_dir: Path, marker: Path) -> Path:
    """The pre-restore folder a half-finished swap was moving the live data into.

    The name is read back from :data:`SWAP_MARKER`, so it is treated as untrusted input
    even though this process wrote it: taken as a bare filename, and accepted only if it
    still looks like one of ours. Anything else (an empty marker, a hand-edited one, a
    folder since deleted) gets a fresh directory rather than a guess, because the one
    thing that must not happen here is the previous data being written somewhere the
    operator will not find it.
    """
    named = Path(marker.read_text(encoding="utf-8").strip()).name
    if named.startswith(PRE_RESTORE_PREFIX):
        recovery = data_dir / named
        if recovery.is_dir():
            return recovery
        if not recovery.exists():
            # Named, prefixed, and simply not there any more: remake it. A marker naming
            # something that exists but is NOT a directory falls through to a fresh one
            # rather than raising, because refusing to boot over a hand-edited marker
            # would strand the very restore this path exists to finish.
            recovery.mkdir(parents=True, mode=0o700)
            return recovery
    return _unique_dir(data_dir, f"{PRE_RESTORE_PREFIX}{utcnow().strftime('%Y%m%dT%H%M%SZ')}")


def _move_staged_in(data_dir: Path, pending: Path) -> None:
    """Move whatever the staging still holds into ``data/``, then drop the staging.

    Each name is skipped when it is already gone, so this is safe to run again after an
    interrupted swap: a file that made it across on the previous attempt stays where it
    is rather than being looked for and missed.
    """
    for name in (DB_ARCNAME, KEY_FILENAME, SALT_FILENAME):
        staged = pending / name
        if staged.exists():
            shutil.move(str(staged), str(data_dir / name))
    shutil.rmtree(pending, ignore_errors=True)


def _discard_unusable(data_dir: Path, pending: Path) -> None:
    """Throw away a staging whose database will not read, KEEPING its key material.

    An ``rmtree`` here would delete ``secret.key`` and ``secret.salt`` along with the
    rest. They are small, they are the only copy of what decrypts that backup's
    credentials, and an operator who staged the wrong file may well want to try the right
    one from the same source (B2-21). Parking them costs nothing.
    """
    kept = [name for name in (KEY_FILENAME, SALT_FILENAME) if (pending / name).is_file()]
    if kept:
        parked = _unique_dir(
            data_dir, f"{PRE_RESTORE_PREFIX}{utcnow().strftime('%Y%m%dT%H%M%SZ')}-keys"
        )
        for name in kept:
            shutil.move(str(pending / name), str(parked / name))
        sys.stderr.write(
            f"reaper: the staged backup's key files were moved to data/{parked.name}\n"
        )
    shutil.rmtree(pending, ignore_errors=True)


def apply_pending_restore(settings: Settings) -> bool:
    """Swap a staged, armed backup into place. Returns whether a swap happened.

    Run at boot before migrations. Resolves toward keeping the live data: if the staged
    database does not read as SQLite, the staging is discarded (bar its key material) and
    the live data is left untouched. Otherwise the current database, its write-ahead log,
    and the key and salt are moved into a timestamped ``pre-restore-*`` directory for
    recovery, and the staged files take their place. ``alembic upgrade head`` (next in
    the entrypoint) then brings the restored database current.

    The moves are two atomic renames per file, but a swap is several files, so a kill
    between them (a host reboot, an OOM, a ``docker stop`` timeout) can leave the
    database replaced and the key still staged. :data:`SWAP_MARKER` records that a swap
    is under way so the next boot finishes it instead of reading a missing staged
    database as a broken staging and discarding the key that opens the database now
    serving (B2-21).
    """
    data_dir = settings.data_dir
    pending = data_dir / PENDING_DIR
    if not (pending / READY_MARKER).is_file():
        return False

    marker = pending / SWAP_MARKER
    if marker.is_file():
        recovery = _recovery_dir(data_dir, marker)
        # Finish the move-aside the killed run may have left half-done, and only then move
        # the staged files in. This used to move the staged files in directly, which was
        # right for the case it was written for (the database across, the key still
        # staged) and wrong one rename earlier: a kill between moving the live database
        # and moving its write-ahead log left that log in data/, where the restored
        # database then landed beside it and SQLite replayed a foreign WAL into it.
        #
        # Whether to finish it at all depends on whether the staged database is still
        # staged. While it is, nothing has been swapped in yet, so anything at
        # data/reaper.db is the PREVIOUS database and it and its sidecars belong in the
        # recovery folder.
        #
        # Once _move_staged_in has moved it across, this must do NOTHING but finish that
        # move. data/reaper.db is then the RESTORED database, and data/reaper.db-wal is
        # ITS log, not the previous one's. Sweeping the sidecars "just in case" here read
        # as harmless because the branch looked unreachable, and it is not: _move_staged_in
        # ends in an rmtree that swallows its own failure (a read-only directory, a held
        # file on a network mount), so the marker can survive a swap that really did
        # complete. Every later boot would then take this branch with the app's own live
        # WAL sitting beside the restored database, and move it away -- losing every
        # transaction still in it, and overwriting the recovery copy's own log with a
        # foreign one. That is the exact corruption _DB_SIDECARS exists to prevent, so the
        # only safe answer here is to leave data/ alone.
        if (pending / DB_ARCNAME).is_file():
            _move_aside(data_dir, recovery, _LIVE_FILES)
        _move_staged_in(data_dir, pending)
        sys.stderr.write(
            "reaper: finished a restore that was interrupted on an earlier start; the "
            f"previous data is saved in data/{recovery.name} (remove it once the restore "
            "looks good)\n"
        )
        return True

    staged_db = pending / DB_ARCNAME
    if not _looks_like_sqlite(staged_db):
        # Armed but unusable, and nothing has been moved yet -- so the live data really is
        # untouched and saying so is true. Fail closed.
        _discard_unusable(data_dir, pending)
        sys.stderr.write(
            "reaper: a staged restore was unreadable and was discarded; current data kept\n"
        )
        return False

    recovery = _unique_dir(data_dir, f"{PRE_RESTORE_PREFIX}{utcnow().strftime('%Y%m%dT%H%M%SZ')}")
    marker.write_text(recovery.name + "\n", encoding="utf-8")
    # Move the live copy aside first (its -wal/-shm travel with it), then move the
    # staged copy in. Same filesystem, so each move is an atomic rename -- but a swap is
    # several of them, and the marker written just above is what lets the next boot pick
    # up between any two (see the resume path at the top of this function).
    _move_aside(data_dir, recovery, _LIVE_FILES)
    _move_staged_in(data_dir, pending)

    sys.stderr.write(
        "reaper: restored the database from a staged backup; the previous data is saved in "
        f"data/{recovery.name} (remove it once the restore looks good)\n"
    )
    return True
