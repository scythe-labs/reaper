# SPDX-License-Identifier: AGPL-3.0-or-later
"""Putting a backup back: validate an uploaded archive, stage it, swap it in on boot.

The other half of :mod:`reaper.services.backup`. Restore is deliberately a
stage-and-restart operation, never a live swap.

1. **Prepare** (:func:`stage_upload`) unpacks an uploaded archive into
   ``data/pending-restore/``, but only after it proves the file is a real Reaper
   backup whose schema this build can serve. Nothing is armed yet: without the
   ``READY`` marker, the staged files are inert, so a half-finished or abandoned
   upload can never be swapped in.
2. **Arm** (:func:`arm`) runs only after the admin password is verified at the API
   edge. It forces deletion off inside the staged database, so the restored install
   boots read-only and is never armed on someone else's decision, then writes the
   ``READY`` marker last. Writing that marker is what arms the swap.
3. **Swap** (:func:`apply_pending_restore`) runs at start, before migrations, from
   :mod:`reaper.preflight`, which the container entrypoint and
   ``scripts/dev-local.sh`` both run and which nothing else may skip. If ``READY``
   is present and the staged database reads as SQLite, it moves the current data
   aside for recovery and moves the staged files into place. ``alembic upgrade
   head`` then brings the restored database current.

The operator asks for that start from the browser (``POST
/api/settings/backup/restore/restart``, which stops the process and lets the container's
restart policy bring it back) or by restarting the container themselves. Either way this
module's part is identical: it is armed, and the next boot swaps.

Every ambiguity resolves toward keeping the live data. A staged database that does not
read as SQLite is discarded rather than swapped. An upload from a newer Reaper than this
one is refused before it is ever staged, because this build could not run its schema.

The schema gate is the load-bearing safety check. A backup carries the Alembic revision
it was cut at. This build knows a fixed set of revisions, the migration scripts shipped
in the image (:func:`reaper.db.schema_gate.known_revisions`). If the backup's revision is
one this build knows, boot's ``alembic upgrade head`` can carry it forward. If it is
unknown, the backup came from a newer Reaper with migrations this build does not have,
and restoring it would serve a schema the code cannot understand, so it is refused. The
same set answers the same question about the live database at boot, which is why it is
declared once, over there.
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
from reaper.config import LAUNCHER_CONF_NAME, Settings
from reaper.db import schema_gate
from reaper.db.models import AUTH_BEARING_TABLES
from reaper.refusal import Refusal
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
#: is written last (see :func:`arm`), so its mere presence means "a verified,
#: password-confirmed restore is ready". An interrupted upload never leaves one behind.
PENDING_DIR = "pending-restore"
READY_MARKER = "READY"

#: Written just before the first file is moved, and removed only when the last one is.
#: Its presence at boot means an earlier boot was killed part way through the swap. That
#: is the one state where "the staged database is missing" must not be read as "the
#: staging is broken": the staged database is missing because it is already the live one.
#: Discarding there would delete the backup's secret.key, the only copy of the key for
#: the database now serving, while printing that the current data was kept.
#: It carries the ``pre-restore-*`` directory name, so a resumed boot can name it.
SWAP_MARKER = "SWAPPING"

#: Binds a password-confirmed arm to the exact content the operator reviewed. Minted per
#: staging (see :func:`stage_upload`), returned in the summary, and required back by
#: :func:`arm`. If a second session re-stages between review and confirm, the staging
#: (and this token) is replaced, so the stale token no longer matches and the arm is
#: refused. Not a secret: a nonce that says "still the same staged backup," living in
#: the 0700 staging dir.
TOKEN_MARKER = "TOKEN"  # noqa: S105 -- a marker filename, not a secret

#: How wide a staging token is, minted here and bounded at the API edge off this same
#: declaration, so the producer and the fields that accept it cannot drift apart. Hex,
#: so the string is twice the byte count.
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
#: modest size and expand without bound, so the copy is capped as it runs: a decompression
#: bomb hits the ceiling and is refused rather than filling the disk. The database ceiling
#: is the same constant the backup side refuses to exceed, so this build can always restore
#: what it can produce; the key, salt, and manifest are all tiny by comparison.
_MEMBER_CAPS = {
    MANIFEST_NAME: 1 * 1024 * 1024,
    DB_ARCNAME: MAX_DB_BYTES,
    KEY_FILENAME: 64 * 1024,
    SALT_FILENAME: 64 * 1024,
    LAUNCHER_CONF_NAME: 64 * 1024,
}


class RestoreError(Refusal):
    """A restore that must not proceed. A catalog code plus raw parameters, and an HTTP
    status.

    Raised toward keeping the live data: a malformed file, a newer-version backup, or a
    staged copy that cannot be verified all become one of these rather than a swap.
    """


@dataclass(frozen=True)
class RestoreSummary:
    """What an accepted, staged backup is, for the operator to confirm before arming."""

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


#: The one set of revisions this build can serve, shared with the boot gate that refuses a
#: live database at an unknown revision (:mod:`reaper.db.schema_gate`). Both refusals
#: answer the same question about the same build, so they read one declaration rather than
#: two copies that could drift apart. Re-exported under this module's name because the
#: restore side is where the question was first asked and where callers look for it.
known_revisions = schema_gate.known_revisions


def _check_schema(revision: str) -> str:
    """Refuse a backup this build cannot serve; return the verdict for one it can.

    A revision this build does not know came from a newer Reaper, refused (409).
    Otherwise it is ``"current"`` (matches head) or ``"older"`` (an ancestor this build
    will upgrade on boot).

    A database carrying no ``alembic_version`` row is refused by :func:`_summarize`, which
    needs the revision for its manifest cross-check and so asks first. ``str`` keeps that
    the only copy of the refusal: a caller that skipped it cannot type-check.
    """
    try:
        known = known_revisions()
    except Exception as exc:  # any failure to read the migrations fails closed
        log.warning("restore.schema_unverifiable", error=str(exc))
        raise RestoreError("error.restore.schema_unverifiable") from exc
    if revision not in known:
        raise RestoreError("error.restore.newer_than_build", status=409)
    return "current" if revision == schema_gate.current_head() else "older"


# ---------------------------------------------------------------------------
# Prepare: unpack and validate an uploaded archive into the staging directory.
# ---------------------------------------------------------------------------


def _member_writer(out_path: Path, *, owner_only: bool) -> Any:
    """Open an extracted member for writing, owner-only from creation when it is a secret.

    The key and salt must be 0600 the instant they exist. The 0700 staging dir shields
    them while staged, but ``apply_pending_restore`` moves them into the data dir, a host
    bind mount, where a write-then-chmod window would leave the master key world-readable
    through boot and migrations. ``os.open`` with ``O_EXCL`` and mode 0600 closes that
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
                raise RestoreError("error.restore.archive_too_large")
            out.write(chunk)
    return written


def _extract(archive_path: Path, dest: Path) -> dict[str, Any]:
    """Unpack the known members of an archive into ``dest`` and return the manifest.

    Only the members named in :data:`_MEMBER_CAPS` are extracted, each to a fixed bare
    filename under ``dest``. A member's own path is never honored, so a crafted archive
    cannot write outside the staging directory. The manifest and the database are
    required. The key and salt are optional, absent for an env-supplied key, and so is
    ``launcher.conf``, absent from a container's backup, which has no such file.

    Anything else in the archive is skipped rather than refused, which is what lets a
    newer Reaper's backup restore into an older one: a member this build has never heard
    of costs only the settings that member carried, never the whole restore.
    """
    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar:
                if member.name not in _MEMBER_CAPS:
                    continue  # ignore anything unexpected; never trust a member's own path
                if not member.isfile():
                    raise RestoreError("error.restore.malformed")
                stream = tar.extractfile(member)
                if stream is None:
                    raise RestoreError("error.restore.malformed")
                secret = member.name in (KEY_FILENAME, SALT_FILENAME)
                _copy_capped(
                    stream, dest / member.name, _MEMBER_CAPS[member.name], owner_only=secret
                )
                seen.add(member.name)
    except RestoreError:
        raise  # a validation refusal, not a read failure, so keep its own message
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise RestoreError("error.restore.unreadable_archive") from exc

    if MANIFEST_NAME not in seen or DB_ARCNAME not in seen:
        raise RestoreError("error.restore.missing_contents")

    try:
        manifest = json.loads((dest / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise RestoreError("error.restore.manifest_unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise RestoreError("error.restore.not_a_backup")

    with (dest / DB_ARCNAME).open("rb") as handle:
        if handle.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
            raise RestoreError("error.restore.database_unreadable")

    return manifest


def _opt_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _summarize(manifest: dict[str, Any], staged: Path, token: str) -> RestoreSummary:
    """Turn a validated manifest into the summary the operator confirms against.

    Runs the schema gate (:func:`_check_schema`), which raises on a backup this build
    can't serve, and refuses a manifest that claims a bundled key but ships none, the
    sign of a tampered or truncated archive.

    The schema gate must read the staged database's own ``alembic_version``, never trust
    the manifest's claim: a repacked archive whose manifest names a known revision while
    its database is any other SQLite file would otherwise pass and be swapped in, and
    then boot's ``alembic upgrade head`` would run against a mismatched schema. The
    manifest's claim, when present, must agree with the artifact itself, or the backup is
    refused as altered.
    """
    db_revision = _read_revision(staged / DB_ARCNAME)
    if db_revision is None:
        raise RestoreError("error.restore.database_unverifiable")
    manifest_revision = _opt_str(manifest.get("alembic_revision"))
    if manifest_revision is not None and manifest_revision != db_revision:
        raise RestoreError("error.restore.manifest_mismatch")
    verdict = _check_schema(db_revision)

    key_in_backup = (staged / KEY_FILENAME).is_file()
    if manifest.get("key_source") == "file" and not key_in_backup:
        raise RestoreError("error.restore.missing_key")

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


def clear_unarmed_staging(settings: Settings) -> bool:
    """Remove a staged-but-unarmed restore, and report whether one was there.

    A staging is reachable only through the token :func:`stage_upload` mints and hands
    back to the browser, which holds it in memory and never writes it down. So a staging
    that outlives the page that made it is already unreachable: :func:`arm` needs
    ``_token_matches`` and :func:`clear_pending` needs the same token, and nothing else
    reclaims it either. The boot swap returns early without ``READY``, and
    ``backup.sweep_stale_temp`` matches only the dotted temp prefixes. What sits there is
    a whole ``reaper.db`` and, when the backup carried one, the ``secret.key`` and
    ``secret.salt`` that decrypt it: an unowned second copy of at-rest key material that
    would otherwise sit there until some later upload happens to land on top of it.
    Nobody can arm it, but it never goes away on its own, which is the half that matters.

    An armed staging is never touched by this function. That one the operator confirmed
    with their password, and the restart it is waiting for is the same restart that runs
    this.
    """
    pending = settings.data_dir / PENDING_DIR
    if not pending.is_dir() or (pending / READY_MARKER).is_file():
        return False
    shutil.rmtree(pending, ignore_errors=True)
    log.info("restore.unarmed_staging_cleared")
    return True


def stage_upload(settings: Settings, archive_path: Path) -> RestoreSummary:
    """Validate an uploaded archive and stage it, un-armed, for a later confirm.

    Extraction and validation happen in a private temp directory. Only a fully accepted
    backup replaces the staging directory, so a refusal never touches live data and
    leaves the operator's own file on their disk untouched. The staged copy carries no
    ``READY`` marker: it cannot be swapped in until :func:`arm` writes one.

    Any unarmed staging from an earlier upload is cleared before this one is read
    (:func:`clear_unarmed_staging`), because choosing a second file abandons the first.
    The upload card drops the first token before it sends the second file, so on a
    refusal that earlier staging would otherwise survive with nobody able to name it. A
    rejected upload leaves live data exactly as it was, but the staging is not live data,
    so it does not get that same protection.
    """
    settings.ensure_data_dir()
    data_dir = settings.data_dir
    clear_unarmed_staging(settings)
    tmp = Path(tempfile.mkdtemp(prefix=RESTORE_TMP_PREFIX, dir=data_dir))
    staged = False
    try:
        manifest = _extract(archive_path, tmp)
        # Mint the staging token and write it beside the staged files, so it travels with
        # the atomic rename below and binds this exact staging to the confirm.
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

    This makes the restored install boot read-only whatever the backup had stored.
    Writes the value the way :mod:`reaper.services.app_settings` reads it, JSON in
    ``value_json`` and an integer epoch in ``updated_at``, and reuses
    :data:`DESTRUCTIVE_KEY` so the key cannot drift from the reader.
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
        raise RestoreError("error.restore.prepare_failed") from exc
    finally:
        con.close()


def _force_recovery_off(conf_path: Path) -> None:
    """Comment out any ``REAPER_RECOVERY`` line in the staged ``launcher.conf``.

    The same obligation as :func:`_purge_auth_state`, one file over: a backup taken while
    recovery mode was armed carries ``REAPER_RECOVERY=true``, and restoring that verbatim
    would arm recovery on the target at its next boot, minting a sign-in code and writing
    it to ``recovery.txt`` for anyone holding the data folder. The backup would then be a
    way into an install it was only ever supposed to rebuild.

    This must comment the line out rather than delete it, so an operator who did want it
    can see it and uncomment it, and so the file still reads as one they wrote. A missing
    or unreadable file needs no action: this only ever removes a permission, so finding
    nothing to remove is the safe outcome and never worth refusing a restore over.
    """
    try:
        text = conf_path.read_text(encoding="utf-8")
    except OSError:
        return

    out: list[str] = []
    disarmed = False
    for line in text.splitlines():
        stripped = line.strip()
        # Matches the reader's own shape (`launcher.load_launcher_conf`): an active line is
        # one that is not blank, does not start with '#', and carries an '='. Anything else
        # already sets nothing, so it is passed through untouched.
        active = bool(stripped) and not stripped.startswith("#") and "=" in stripped
        if active and stripped.partition("=")[0].strip() == "REAPER_RECOVERY":
            out.append("# Turned off by a restore. Uncomment only if you are locked out.")
            out.append(f"# {line.strip()}")
            disarmed = True
            continue
        out.append(line)

    if not disarmed:
        return
    try:
        conf_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    except OSError as exc:
        # Refuse rather than arm the target: this is the one failure here that would leave
        # the operator with a live way in they did not ask for (the prime directive).
        raise RestoreError("error.restore.prepare_failed") from exc
    log.warning("restore.recovery_disarmed")


#: The identifier pattern a purgeable table name must match. The ``DELETE`` below is built
#: by interpolation, because SQLite has no bind parameter for a table name, so this proves
#: the name is a bare identifier first rather than trusting that it came from our own
#: module. The check costs nothing and does not depend on where the list came from.
_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _purge_auth_state(db_path: Path) -> None:
    """Clear sessions, recovery tokens, and pending logins from the staged database.

    A restore replaces the whole database, which is a wholesale credential change: the
    backup's password hash comes back into effect, so its live sessions and recovery
    tokens must not also come back. Otherwise a session or reset link valid when the
    backup was taken would work again after the swap, defeating a later
    sign-out-everywhere. Runs in the same fail-closed step as forcing deletion off; a
    table absent in an older backup simply has nothing to purge.

    Which tables those are is declared beside the models
    (:data:`~reaper.db.models.AUTH_BEARING_TABLES`), not here, so a new one cannot be
    added to the schema and silently ride a restore through unpurged.
    """
    con = sqlite3.connect(db_path)
    try:
        for table in AUTH_BEARING_TABLES:
            if not _TABLE_NAME.match(table):  # pragma: no cover -- guards the f-string below
                raise RestoreError("error.restore.prepare_failed")
            present = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            if present:
                con.execute(f"DELETE FROM {table}")  # noqa: S608 -- identifier checked above
        con.commit()
    except sqlite3.OperationalError as exc:
        raise RestoreError("error.restore.prepare_failed") from exc
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

    Called only after the admin password is verified at the API edge. ``token`` must
    equal the one minted for this staging, returned in the summary the operator
    reviewed. If a different backup was staged since, the token no longer matches and
    the arm is refused, so the password can only ever arm the content that was actually
    reviewed. The ``READY`` marker is written last, so a crash between preparing the
    database and arming leaves the staging inert rather than half-armed.

    The marker is also cleared first, which is what makes
    ``error.restore.prepare_failed``'s "nothing was restored" true rather than nearly
    true. Neither check above refuses an arm over a staging that is already armed: the
    token file survives an arm, so a confirm retried after a client-side timeout runs
    the three steps again with ``READY`` on disk. Clearing the marker first means a
    failed arm disarms rather than leaving the swap armed while the operator is told
    nothing happened, which is the keep direction.
    """
    pending = settings.data_dir / PENDING_DIR
    staged_db = pending / DB_ARCNAME
    if not _looks_like_sqlite(staged_db):
        raise RestoreError("error.restore.nothing_staged")
    if not _token_matches(pending, token):
        raise RestoreError("error.restore.staged_changed", status=409)
    (pending / READY_MARKER).unlink(missing_ok=True)
    _force_destructive_off(staged_db)
    _force_recovery_off(pending / LAUNCHER_CONF_NAME)
    _purge_auth_state(staged_db)
    (pending / READY_MARKER).write_text(
        utcnow().strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8"
    )
    log.warning("restore.armed")


def clear_pending(settings: Settings, token: str | None = None) -> bool:
    """Discard a staged or armed restore. Safe to call when nothing is staged.

    ``token`` scopes the discard to one staging, the way :func:`arm` scopes the swap to
    the content that was reviewed. With a token, the discard happens only if that token
    is still the one minted for what is staged now; a staging replaced since belongs to
    whoever staged it, and this returns ``False`` having removed nothing. Without a
    token, the discard is unconditional, which is what a deliberate Cancel on an armed
    restore needs: it holds no summary and no token, and it must still be able to clear
    anything.

    Returns whether a staging was actually removed, so the route can say what happened
    rather than reporting an ownership refusal, or a call that found nothing at all, as a
    discard. Both of those are ordinary arrivals here rather than errors: this is reached
    from an unmount, and the operator may have cleared the staging from somewhere else
    first.
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
#: recovers from it on the next open. It validates that log only by its own header and
#: frame checksums, never by any binding to the database file beside it, so a WAL left
#: over from a different database replays into whatever ``reaper.db`` it finds. That is
#: why these travel with the database everywhere below, and never on their own.
_DB_SIDECARS = (f"{DB_ARCNAME}-wal", f"{DB_ARCNAME}-shm")
_LIVE_FILES = (DB_ARCNAME, *_DB_SIDECARS, KEY_FILENAME, SALT_FILENAME, LAUNCHER_CONF_NAME)


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

    The name is read back from :data:`SWAP_MARKER` and treated as untrusted input even
    though this process wrote it: taken as a bare filename, and accepted only if it
    still looks like one of ours. Anything else, such as an empty marker, a hand-edited
    one, or a folder since deleted, gets a fresh directory rather than a guess, because
    the one thing that must not happen here is the previous data being written
    somewhere the operator will not find it.
    """
    named = Path(marker.read_text(encoding="utf-8").strip()).name
    if named.startswith(PRE_RESTORE_PREFIX):
        recovery = data_dir / named
        if recovery.is_dir():
            return recovery
        if not recovery.exists():
            # Named, prefixed, and simply not there any more: remake it. A marker naming
            # something that exists but is not a directory falls through to a fresh one
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
    for name in (DB_ARCNAME, KEY_FILENAME, SALT_FILENAME, LAUNCHER_CONF_NAME):
        staged = pending / name
        if staged.exists():
            shutil.move(str(staged), str(data_dir / name))
    shutil.rmtree(pending, ignore_errors=True)


def _discard_unusable(data_dir: Path, pending: Path) -> None:
    """Throw away a staging whose database will not read, but keep its key material.

    An ``rmtree`` here would delete ``secret.key`` and ``secret.salt`` along with the
    rest. They are small, they are the only copy of what decrypts that backup's
    credentials, and an operator who staged the wrong file may well want to try the
    right one from the same source. Parking them costs nothing.
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
    database does not read as SQLite, the staging is discarded, bar its key material, and
    the live data is left untouched. Otherwise the current database, its write-ahead log,
    and the key and salt are moved into a timestamped ``pre-restore-*`` directory for
    recovery, and the staged files take their place. ``alembic upgrade head``, next in
    the entrypoint, then brings the restored database current.

    Each file move is an atomic rename, but a swap moves several files, so a kill
    between them (a host reboot, an OOM, a ``docker stop`` timeout) can leave the
    database replaced and the key still staged. :data:`SWAP_MARKER` records that a swap
    is under way, so the next boot finishes it instead of reading a missing staged
    database as a broken staging and discarding the key that opens the database now
    serving.
    """
    data_dir = settings.data_dir
    pending = data_dir / PENDING_DIR
    if not (pending / READY_MARKER).is_file():
        return False

    marker = pending / SWAP_MARKER
    if marker.is_file():
        recovery = _recovery_dir(data_dir, marker)
        # Finish the move-aside a killed run may have left half-done, and only then move
        # the staged files in. The two steps must run in that order, and whether the
        # move-aside is still needed depends on whether the staged database is still
        # staged.
        #
        # While the staged database is still in `pending`, nothing has been swapped in
        # yet, so anything at data/reaper.db is the PREVIOUS database, and it and its
        # sidecars belong in the recovery folder.
        #
        # Once `_move_staged_in` has already moved it across, this must do nothing but
        # finish that move. data/reaper.db is then the RESTORED database, and
        # data/reaper.db-wal is its own log, not the previous one's. Moving the sidecars
        # aside "just in case" at that point would take the app's own live WAL away from
        # the restored database, losing every transaction still in it, and would
        # overwrite the recovery copy's own log with a foreign one: SQLite replays
        # whatever WAL it finds beside a database file, whether or not it belongs to it.
        # That is the exact corruption _DB_SIDECARS exists to prevent, so the only safe
        # answer once the staged database is already gone is to leave data/ alone.
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
        # Armed but unusable, and nothing has been moved yet, so the live data really is
        # untouched and saying so is true. Fail closed.
        _discard_unusable(data_dir, pending)
        sys.stderr.write(
            "reaper: a staged restore was unreadable and was discarded; current data kept\n"
        )
        return False

    recovery = _unique_dir(data_dir, f"{PRE_RESTORE_PREFIX}{utcnow().strftime('%Y%m%dT%H%M%SZ')}")
    marker.write_text(recovery.name + "\n", encoding="utf-8")
    # Move the live copy aside first, its -wal and -shm travel with it, then move the
    # staged copy in. Same filesystem, so each move is an atomic rename, but a swap is
    # several of them, and the marker written just above is what lets the next boot pick
    # up between any two (see the resume path at the top of this function).
    _move_aside(data_dir, recovery, _LIVE_FILES)
    _move_staged_in(data_dir, pending)

    sys.stderr.write(
        "reaper: restored the database from a staged backup; the previous data is saved in "
        f"data/{recovery.name} (remove it once the restore looks good)\n"
    )
    return True
