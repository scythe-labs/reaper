# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings -> Restore: validate an uploaded backup, stage it, swap it in on boot.

What is pinned here, all of it resolving toward keeping the live data:

* the schema gate accepts a backup this build knows how to serve and refuses one from a
  newer Reaper (409) or one whose database can't be verified;
* a crafted archive can't write outside the staging directory, and a decompression
  bomb, a non-archive, a missing member, or a non-SQLite database are all refused;
* preparing stages the files but does NOT arm the swap -- only a password-confirmed
  ``arm`` writes the READY marker, and it forces deletion off in the staged database;
* the boot swap replaces the database only when armed and the staged copy reads as
  SQLite, keeps the previous data in a recovery directory, and is a no-op otherwise;
* ``restart`` stops the app only where a restore is armed and no reap is running, and it
  stops by the signal that lets a run in flight record itself;
* the restore routes are fenced off the API-key lane and need a signed-in session.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import sqlite3
import stat
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.api import backup as backup_api
from reaper.api.middleware import _api_key_allowed, api_key_refused
from reaper.api.runs import ReapStatus
from reaper.config import Settings
from reaper.db.base import Base
from reaper.main import create_app
from reaper.services import app_settings, restore
from reaper.services.restore import RestoreError
from tests._auth import TEST_PASSWORD, clear_admin_password

# A revision this build actually ships, so the schema gate accepts it. Iteration order of
# the frozenset is unimportant: any shipped revision is one boot's migrations can serve.
KNOWN_REVISION = next(iter(restore.known_revisions()))
UNKNOWN_REVISION = "0000newerversion"

#: For ``_make_archive``: put the manifest's claimed revision into the database itself. Pass
#: an explicit ``db_revision`` to decouple the two (the tampered-archive case S-2 guards).
_SAME_AS_MANIFEST = object()


# --- archive builders --------------------------------------------------------


def _tiny_sqlite(path: Path, *, revision: str | None = None, with_auth: bool = False) -> None:
    """A minimal real SQLite database carrying the one table ``arm`` writes to.

    ``revision`` writes an ``alembic_version`` row the way a real ``reaper.db`` carries it,
    so the restore schema gate reads the artifact's own revision (S-2). ``with_auth`` adds
    the session/recovery/pending-login tables with a row each, so the arm-time purge (S-3)
    has something to clear.
    """
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE app_setting "
            "(key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at INTEGER NOT NULL)"
        )
        if revision is not None:
            con.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
            con.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        if with_auth:
            con.execute("CREATE TABLE auth_session (token_hash TEXT PRIMARY KEY)")
            con.execute("INSERT INTO auth_session VALUES ('sess')")
            con.execute("CREATE TABLE recovery_token (token_hash TEXT PRIMARY KEY)")
            con.execute("INSERT INTO recovery_token VALUES ('rec')")
            con.execute("CREATE TABLE pending_plex_login (id INTEGER PRIMARY KEY)")
            con.execute("INSERT INTO pending_plex_login VALUES (1)")
        con.commit()
    finally:
        con.close()


def _make_archive(
    dest: Path,
    *,
    revision: str | None = KNOWN_REVISION,
    db_revision: object = _SAME_AS_MANIFEST,
    key_source: str = "file",
    with_key: bool = True,
    with_salt: bool = True,
    with_auth: bool = False,
    fmt: str = "reaper-backup",
    include_manifest: bool = True,
    include_db: bool = True,
    db_bytes: bytes | None = None,
    extra_member: tuple[str, bytes] | None = None,
) -> Path:
    """Build a Reaper-shaped backup archive with every knob a test needs to bend."""
    work = dest.parent / f"{dest.name}.work"
    work.mkdir()
    db_path = work / "reaper.db"
    if include_db:
        if db_bytes is not None:
            db_path.write_bytes(db_bytes)
        else:
            db_rev = revision if db_revision is _SAME_AS_MANIFEST else db_revision
            _tiny_sqlite(db_path, revision=db_rev, with_auth=with_auth)  # type: ignore[arg-type]

    manifest = {
        "format": fmt,
        "format_version": 1,
        "created_at": "2026-07-20T10:00:00Z",
        "app_version": "9.9.9",
        "alembic_revision": revision,
        "reaper_db_bytes": db_path.stat().st_size if include_db else 0,
        "key_source": key_source,
        "contents": {
            "reaper_db": include_db,
            "secret_key": with_key,
            "secret_salt": with_salt,
            "cache_db": False,
        },
    }
    with tarfile.open(dest, "w:gz") as tar:
        if include_manifest:
            payload = json.dumps(manifest).encode("utf-8")
            info = tarfile.TarInfo("manifest.json")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        if include_db:
            tar.add(db_path, arcname="reaper.db")
        if with_key:
            key_path = work / "secret.key"
            key_path.write_text("themasterkey\n", encoding="utf-8")
            tar.add(key_path, arcname="secret.key")
        if with_salt:
            salt_path = work / "secret.salt"
            salt_path.write_text("00112233\n", encoding="utf-8")
            tar.add(salt_path, arcname="secret.salt")
        if extra_member is not None:
            name, content = extra_member
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return dest


def _settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path, secret_key="k")
    s.ensure_data_dir()
    return s


def _destructive_value(db_path: Path) -> str | None:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT value_json FROM app_setting WHERE key = 'destructive_enabled'"
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else None


# --- the schema gate ---------------------------------------------------------


class TestSchemaGate:
    def test_it_accepts_a_revision_this_build_knows(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        archive = _make_archive(tmp_path / "backup.reaper")
        summary = restore.stage_upload(settings, archive)
        assert summary.revision == KNOWN_REVISION
        assert summary.verdict in {"current", "older"}
        assert restore.is_armed(settings) is False  # staged, not armed

    def test_it_refuses_a_backup_from_a_newer_reaper(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        archive = _make_archive(tmp_path / "backup.reaper", revision=UNKNOWN_REVISION)
        with pytest.raises(RestoreError) as excinfo:
            restore.stage_upload(settings, archive)
        assert excinfo.value.status == 409
        # Nothing was staged: a refused upload leaves the state untouched.
        assert not (settings.data_dir / restore.PENDING_DIR).exists()

    def test_it_refuses_a_database_it_cannot_verify(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        archive = _make_archive(tmp_path / "backup.reaper", revision=None)
        with pytest.raises(RestoreError):
            restore.stage_upload(settings, archive)

    def test_it_reads_the_revision_from_the_database_not_the_manifest(self, tmp_path: Path) -> None:
        # S-2: a manifest claiming a known revision cannot launder a database that carries a
        # different (here newer/unknown) one -- the artifact's own version is what is gated.
        settings = _settings(tmp_path)
        archive = _make_archive(
            tmp_path / "backup.reaper", revision=KNOWN_REVISION, db_revision=UNKNOWN_REVISION
        )
        with pytest.raises(RestoreError):
            restore.stage_upload(settings, archive)
        assert not (settings.data_dir / restore.PENDING_DIR).exists()

    def test_it_refuses_a_db_with_no_revision_even_if_the_manifest_claims_one(
        self, tmp_path: Path
    ) -> None:
        # S-2: the manifest's good claim must not paper over a database that carries no
        # alembic_version at all (a repacked or foreign file).
        settings = _settings(tmp_path)
        archive = _make_archive(
            tmp_path / "backup.reaper", revision=KNOWN_REVISION, db_revision=None
        )
        with pytest.raises(RestoreError):
            restore.stage_upload(settings, archive)


# --- untrusted-archive handling ----------------------------------------------


class TestArchiveSafety:
    def test_a_non_archive_is_refused(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        junk = tmp_path / "backup.reaper"
        junk.write_bytes(b"this is not a gzip tar")
        with pytest.raises(RestoreError):
            restore.stage_upload(settings, junk)

    def test_a_missing_database_is_refused(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        archive = _make_archive(tmp_path / "backup.reaper", include_db=False)
        with pytest.raises(RestoreError):
            restore.stage_upload(settings, archive)

    def test_a_non_sqlite_database_is_refused(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        archive = _make_archive(tmp_path / "backup.reaper", db_bytes=b"not a database at all")
        with pytest.raises(RestoreError):
            restore.stage_upload(settings, archive)

    def test_a_foreign_format_is_refused(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        archive = _make_archive(tmp_path / "backup.reaper", fmt="not-reaper")
        with pytest.raises(RestoreError):
            restore.stage_upload(settings, archive)

    def test_a_path_traversal_member_is_ignored(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        archive = _make_archive(
            tmp_path / "backup.reaper", extra_member=("../../escaped", b"pwned")
        )
        # The crafted member is not one of the four expected names, so it is skipped and the
        # otherwise-valid backup still stages. Nothing is written outside the staging dir.
        restore.stage_upload(settings, archive)
        assert not (tmp_path.parent / "escaped").exists()
        assert not (tmp_path / "escaped").exists()

    def test_a_bundle_claiming_a_key_it_lacks_is_refused(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        archive = _make_archive(tmp_path / "backup.reaper", key_source="file", with_key=False)
        with pytest.raises(RestoreError):
            restore.stage_upload(settings, archive)

    def test_an_env_key_backup_is_flagged(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        archive = _make_archive(tmp_path / "backup.reaper", key_source="env", with_key=False)
        summary = restore.stage_upload(settings, archive)
        assert summary.key_in_backup is False


# --- the staging nobody can reach --------------------------------------------


class TestAbandonedStaging:
    """A staging is named by the token minted for it, and that token only ever lives in the
    browser that uploaded it. So one that outlives its page can never be armed and never be
    canceled, while holding a whole database and the key material that decrypts it, and
    nothing used to reclaim it (#388). The refusal path is where it was reachable: the card
    drops the first token before it sends the second file."""

    def test_a_refused_second_upload_leaves_no_staging_behind(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        pending = settings.data_dir / restore.PENDING_DIR
        restore.stage_upload(settings, _make_archive(tmp_path / "first.reaper"))
        assert (pending / restore.TOKEN_MARKER).is_file()

        junk = tmp_path / "second.reaper"
        junk.write_bytes(b"this is not a gzip tar")
        with pytest.raises(RestoreError):
            restore.stage_upload(settings, junk)

        # Not merely un-armable: gone. The key material is the reason this is not cosmetic --
        # a copy of it nobody owns is a copy nobody rotates.
        assert not pending.exists()

    def test_an_armed_staging_survives_a_refused_upload(self, tmp_path: Path) -> None:
        # The other direction, and the one that matters more: an armed restore is one the
        # operator confirmed with their password, so a later bad file must not cancel it.
        settings = _settings(tmp_path)
        summary = restore.stage_upload(settings, _make_archive(tmp_path / "first.reaper"))
        restore.arm(settings, summary.token)
        assert restore.is_armed(settings) is True

        junk = tmp_path / "second.reaper"
        junk.write_bytes(b"this is not a gzip tar")
        with pytest.raises(RestoreError):
            restore.stage_upload(settings, junk)

        assert restore.is_armed(settings) is True

    def test_boot_clears_an_unconfirmed_staging(self, tmp_path: Path) -> None:
        # The half the client can never cover: a closed tab or a crashed browser strands the
        # staging the same way, with the token gone from memory rather than overwritten.
        settings = _settings(tmp_path)
        restore.stage_upload(settings, _make_archive(tmp_path / "backup.reaper"))

        assert restore.clear_unarmed_staging(settings) is True
        assert not (settings.data_dir / restore.PENDING_DIR).exists()
        assert restore.clear_unarmed_staging(settings) is False  # nothing left to report

    def test_boot_leaves_an_armed_staging_for_the_swap(self, tmp_path: Path) -> None:
        # This runs on the same boot as `apply_pending_restore`, so clearing an armed staging
        # here would eat the restore the operator restarted for.
        settings = _settings(tmp_path)
        summary = restore.stage_upload(settings, _make_archive(tmp_path / "backup.reaper"))
        restore.arm(settings, summary.token)

        assert restore.clear_unarmed_staging(settings) is False
        assert restore.is_armed(settings) is True


# --- arm / cancel ------------------------------------------------------------


class TestArm:
    def test_arm_writes_the_marker_and_forces_deletion_off(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        summary = restore.stage_upload(settings, _make_archive(tmp_path / "backup.reaper"))
        restore.arm(settings, summary.token)
        assert restore.is_armed(settings) is True
        staged_db = settings.data_dir / restore.PENDING_DIR / "reaper.db"
        assert _destructive_value(staged_db) == "false"

    def test_arm_without_a_staged_backup_refuses(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with pytest.raises(RestoreError):
            restore.arm(settings, None)

    def test_arm_refuses_a_token_from_a_replaced_staging(self, tmp_path: Path) -> None:
        # S-1: a second upload replaces the staging (and its token) between review and
        # confirm, so the operator's stale token no longer arms what they never saw.
        settings = _settings(tmp_path)
        first = restore.stage_upload(settings, _make_archive(tmp_path / "a.reaper"))
        restore.stage_upload(settings, _make_archive(tmp_path / "b.reaper"))
        with pytest.raises(RestoreError) as excinfo:
            restore.arm(settings, first.token)
        assert excinfo.value.status == 409
        assert restore.is_armed(settings) is False

    def test_arm_clears_inherited_auth_state(self, tmp_path: Path) -> None:
        # S-3: the backup's sessions, recovery tokens, and pending logins must not survive
        # the swap -- a restore is a wholesale credential change.
        settings = _settings(tmp_path)
        summary = restore.stage_upload(
            settings, _make_archive(tmp_path / "backup.reaper", with_auth=True)
        )
        restore.arm(settings, summary.token)
        staged_db = settings.data_dir / restore.PENDING_DIR / "reaper.db"
        con = sqlite3.connect(staged_db)
        try:
            assert con.execute("SELECT count(*) FROM auth_session").fetchone()[0] == 0
            assert con.execute("SELECT count(*) FROM recovery_token").fetchone()[0] == 0
            assert con.execute("SELECT count(*) FROM pending_plex_login").fetchone()[0] == 0
        finally:
            con.close()

    def test_arm_disarms_recovery_in_the_restored_launcher_conf(self, tmp_path: Path) -> None:
        """A backup taken while recovery mode was armed carries REAPER_RECOVERY=true. Left
        alone, restoring it would arm recovery on the TARGET at its next boot, mint a
        sign-in code, and write it to recovery.txt -- turning a backup file into a way into
        the install it was supposed to rebuild. Same obligation as the auth purge above."""
        settings = _settings(tmp_path)
        conf = b"REAPER_PORT=8421\nREAPER_RECOVERY=true\nREAPER_TRAY=false\n"
        summary = restore.stage_upload(
            settings,
            _make_archive(tmp_path / "backup.reaper", extra_member=("launcher.conf", conf)),
        )
        restore.arm(settings, summary.token)

        staged = (settings.data_dir / restore.PENDING_DIR / "launcher.conf").read_text("utf-8")
        # Commented, not deleted: the operator can still see it and turn it back on.
        assert "\nREAPER_RECOVERY=true" not in f"\n{staged}"
        assert "# REAPER_RECOVERY=true" in staged
        # ...and every other setting they had is untouched. Disarming must not cost them
        # the port their reverse proxy is pointed at.
        assert "REAPER_PORT=8421" in staged
        assert "REAPER_TRAY=false" in staged

    def test_arm_leaves_a_conf_that_never_armed_recovery_alone(self, tmp_path: Path) -> None:
        """The branch the fix does not touch, driven so the rewrite cannot start mangling
        ordinary files unnoticed (rule 145): an already-commented line is not an active one.
        Byte-for-byte, because a rewritten file that happens to mean the same thing would
        still be a file the operator did not write."""
        settings = _settings(tmp_path)
        conf = b"REAPER_PORT=8421\n# REAPER_RECOVERY=true\n"
        summary = restore.stage_upload(
            settings,
            _make_archive(tmp_path / "backup.reaper", extra_member=("launcher.conf", conf)),
        )
        restore.arm(settings, summary.token)

        assert (settings.data_dir / restore.PENDING_DIR / "launcher.conf").read_bytes() == conf

    def test_arm_is_fine_with_no_launcher_conf_at_all(self, tmp_path: Path) -> None:
        """A container's backup has none, and that must not refuse a restore."""
        settings = _settings(tmp_path)
        summary = restore.stage_upload(settings, _make_archive(tmp_path / "backup.reaper"))
        restore.arm(settings, summary.token)
        assert restore.is_armed(settings) is True

    def test_cancel_clears_the_staging(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        summary = restore.stage_upload(settings, _make_archive(tmp_path / "backup.reaper"))
        restore.arm(settings, summary.token)
        assert restore.clear_pending(settings) is True
        assert restore.is_armed(settings) is False
        assert not (settings.data_dir / restore.PENDING_DIR).exists()

    def test_cancel_with_the_staging_token_clears_it(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        summary = restore.stage_upload(settings, _make_archive(tmp_path / "backup.reaper"))
        assert restore.clear_pending(settings, summary.token) is True
        assert not (settings.data_dir / restore.PENDING_DIR).exists()

    def test_cancel_refuses_a_token_from_a_replaced_staging(self, tmp_path: Path) -> None:
        # #387, and the discard side of `test_arm_refuses_a_token_from_a_replaced_staging`
        # above: two live restore cards each hold their own reviewed summary, and only the
        # later stage survives. The earlier card's reclaim must not take the other's archive.
        settings = _settings(tmp_path)
        first = restore.stage_upload(settings, _make_archive(tmp_path / "a.reaper"))
        second = restore.stage_upload(settings, _make_archive(tmp_path / "b.reaper"))

        assert restore.clear_pending(settings, first.token) is False

        # Still `second`'s, intact and still armable by the card that staged it -- which is
        # what a bare "the directory is there" assertion would not tell apart from a staging
        # left half-removed.
        assert (settings.data_dir / restore.PENDING_DIR).exists()
        restore.arm(settings, second.token)
        assert restore.is_armed(settings) is True

    def test_cancel_with_a_token_and_nothing_staged_removes_nothing(self, tmp_path: Path) -> None:
        # The reclaim fires on an unmount that may follow a cancel from anywhere else, so
        # "no staging at all" is an ordinary arrival here, not an error.
        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        assert restore.clear_pending(settings, "a" * restore.TOKEN_MAX_LEN) is False

    def test_restored_key_and_salt_are_owner_only(self, tmp_path: Path) -> None:
        # S-4: the key and salt are 0600 from creation, before the boot swap moves them into
        # the data dir (a bind mount) where a write-then-chmod window would expose the key.
        settings = _settings(tmp_path)
        restore.stage_upload(settings, _make_archive(tmp_path / "backup.reaper"))
        pending = settings.data_dir / restore.PENDING_DIR
        assert stat.S_IMODE((pending / "secret.key").stat().st_mode) == 0o600
        assert stat.S_IMODE((pending / "secret.salt").stat().st_mode) == 0o600


# --- the boot swap -----------------------------------------------------------


class TestApplyPendingRestore:
    def _seed_live(self, settings: Settings, marker: str) -> None:
        """Write a live database (plus key and salt) carrying a distinguishing marker."""
        live = settings.data_dir / "reaper.db"
        _tiny_sqlite(live)
        con = sqlite3.connect(live)
        try:
            con.execute("INSERT INTO app_setting VALUES ('marker', ?, 0)", (json.dumps(marker),))
            con.commit()
        finally:
            con.close()
        (settings.data_dir / "secret.key").write_text("liveKey\n", encoding="utf-8")
        (settings.data_dir / "secret.salt").write_text("livesalt\n", encoding="utf-8")

    def test_the_swap_puts_the_launcher_settings_back(self, tmp_path: Path) -> None:
        """The point of carrying it: a desktop or snap install rebuilt from a backup keeps
        the port and bind address it was reachable on. Nothing in the database holds them."""
        settings = _settings(tmp_path)
        self._seed_live(settings, "live")
        (settings.data_dir / "launcher.conf").write_text("REAPER_PORT=1\n", encoding="utf-8")
        summary = restore.stage_upload(
            settings,
            _make_archive(
                tmp_path / "backup.reaper", extra_member=("launcher.conf", b"REAPER_PORT=8421\n")
            ),
        )
        restore.arm(settings, summary.token)

        assert restore.apply_pending_restore(settings) is True
        assert (settings.data_dir / "launcher.conf").read_text("utf-8") == "REAPER_PORT=8421\n"

    def test_it_swaps_an_armed_backup_and_keeps_the_old_data(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        self._seed_live(settings, "live")
        summary = restore.stage_upload(settings, _make_archive(tmp_path / "backup.reaper"))
        restore.arm(settings, summary.token)

        assert restore.apply_pending_restore(settings) is True

        # The live database is gone: the staged copy (no marker row) took its place, and it
        # boots read-only because arm forced deletion off.
        new_db = settings.data_dir / "reaper.db"
        con = sqlite3.connect(new_db)
        try:
            marker = con.execute(
                "SELECT value_json FROM app_setting WHERE key = 'marker'"
            ).fetchone()
        finally:
            con.close()
        assert marker is None
        assert _destructive_value(new_db) == "false"

        # The previous data is preserved for recovery, and the staging is gone.
        recovery = list(settings.data_dir.glob(f"{restore.PRE_RESTORE_PREFIX}*"))
        assert len(recovery) == 1
        assert (recovery[0] / "reaper.db").is_file()
        assert not (settings.data_dir / restore.PENDING_DIR).exists()

    def test_it_is_a_noop_when_not_armed(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        self._seed_live(settings, "live")
        restore.stage_upload(settings, _make_archive(tmp_path / "backup.reaper"))
        # Staged but never armed: no READY marker, so boot leaves the live data alone.
        assert restore.apply_pending_restore(settings) is False
        live = settings.data_dir / "reaper.db"
        con = sqlite3.connect(live)
        try:
            marker = con.execute(
                "SELECT value_json FROM app_setting WHERE key = 'marker'"
            ).fetchone()
        finally:
            con.close()
        assert marker == ('"live"',)

    def test_it_discards_an_unreadable_staged_db_and_keeps_live(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        self._seed_live(settings, "live")
        pending = settings.data_dir / restore.PENDING_DIR
        pending.mkdir()
        (pending / "reaper.db").write_bytes(b"not sqlite")
        (pending / restore.READY_MARKER).write_text("armed\n", encoding="utf-8")

        assert restore.apply_pending_restore(settings) is False
        assert not pending.exists()  # the bad staging was discarded
        live = settings.data_dir / "reaper.db"
        con = sqlite3.connect(live)
        try:
            marker = con.execute(
                "SELECT value_json FROM app_setting WHERE key = 'marker'"
            ).fetchone()
        finally:
            con.close()
        assert marker == ('"live"',)  # live data untouched

    def test_it_is_a_noop_with_no_staging(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        assert restore.apply_pending_restore(settings) is False


# --- the API surface ---------------------------------------------------------


class TestApi:
    def test_prepare_stages_and_confirm_arms(self, client: TestClient, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path / "up.reaper").read_bytes()
        prepared = client.post("/api/settings/backup/restore/prepare", content=archive)
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()["verdict"] in {"current", "older"}
        token = prepared.json()["token"]
        # Staged, not yet armed.
        assert client.get("/api/settings/backup").json()["restore_armed"] is False

        confirmed = client.post(
            "/api/settings/backup/restore/confirm",
            json={"password": TEST_PASSWORD, "token": token},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert client.get("/api/settings/backup").json()["restore_armed"] is True

    def test_confirm_refuses_a_token_from_a_replaced_upload(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        # S-1 end to end: the operator reviews upload A, a second upload B replaces the
        # staging, and A's token can no longer arm B even with the right password.
        first = client.post(
            "/api/settings/backup/restore/prepare",
            content=_make_archive(tmp_path / "a.reaper").read_bytes(),
        )
        token_a = first.json()["token"]
        client.post(
            "/api/settings/backup/restore/prepare",
            content=_make_archive(tmp_path / "b.reaper").read_bytes(),
        )
        response = client.post(
            "/api/settings/backup/restore/confirm",
            json={"password": TEST_PASSWORD, "token": token_a},
        )
        assert response.status_code == 409, response.text
        assert client.get("/api/settings/backup").json()["restore_armed"] is False

    def test_prepare_refuses_a_newer_backup(self, client: TestClient, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path / "up.reaper", revision=UNKNOWN_REVISION).read_bytes()
        response = client.post("/api/settings/backup/restore/prepare", content=archive)
        assert response.status_code == 409, response.text

    def test_confirm_rejects_a_wrong_password(self, client: TestClient, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path / "up.reaper").read_bytes()
        client.post("/api/settings/backup/restore/prepare", content=archive)
        response = client.post(
            "/api/settings/backup/restore/confirm", json={"password": "not-the-password"}
        )
        assert response.status_code == 403, response.text
        assert client.get("/api/settings/backup").json()["restore_armed"] is False

    def test_with_no_admin_password_set_confirm_points_at_the_password_step(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Not a 403: a Plex-only install has nothing to type, so "that didn't match" would
        send the operator to guess at a password that does not exist. The archive stays
        staged and un-armed, which is the state they can still cancel out of.

        Sibling of the same pair on arming deletion and on the watch-record reset (rule 72).
        """
        archive = _make_archive(tmp_path / "up.reaper").read_bytes()
        client.post("/api/settings/backup/restore/prepare", content=archive)
        clear_admin_password(client)

        refused = client.post("/api/settings/backup/restore/confirm", json={"password": ""})
        assert refused.status_code == 400, refused.text
        assert refused.json()["detail"] == (
            "Set an admin password first. It's what confirms a restore."
        )
        assert client.get("/api/settings/backup").json()["restore_armed"] is False

    def test_cancel_disarms_a_staged_restore(self, client: TestClient, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path / "up.reaper").read_bytes()
        prepared = client.post("/api/settings/backup/restore/prepare", content=archive)
        client.post(
            "/api/settings/backup/restore/confirm",
            json={"password": TEST_PASSWORD, "token": prepared.json()["token"]},
        )
        assert client.get("/api/settings/backup").json()["restore_armed"] is True

        canceled = client.post("/api/settings/backup/restore/cancel")
        assert canceled.status_code == 200, canceled.text
        assert canceled.json()["cleared"] is True
        assert client.get("/api/settings/backup").json()["restore_armed"] is False

    def test_cancel_scoped_to_a_replaced_staging_leaves_it(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """The route half of #387, over the wire the two cards actually use.

        The first card's reclaim arrives after a second card has staged its own archive.
        It names what it staged, the server no longer holds that, and the archive the
        other card is still showing survives -- confirmed by arming it afterwards, which
        is the operation the stranded card was left unable to perform.
        """
        first = client.post(
            "/api/settings/backup/restore/prepare",
            content=_make_archive(tmp_path / "a.reaper").read_bytes(),
        )
        second = client.post(
            "/api/settings/backup/restore/prepare",
            content=_make_archive(tmp_path / "b.reaper").read_bytes(),
        )

        reclaimed = client.post(
            "/api/settings/backup/restore/cancel", json={"token": first.json()["token"]}
        )

        assert reclaimed.status_code == 200, reclaimed.text
        assert reclaimed.json()["cleared"] is False
        armed = client.post(
            "/api/settings/backup/restore/confirm",
            json={"password": TEST_PASSWORD, "token": second.json()["token"]},
        )
        assert armed.status_code == 200, armed.text
        assert client.get("/api/settings/backup").json()["restore_armed"] is True

    def test_cancel_refuses_a_token_wider_than_one_can_be(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        # Rule 95: the field is bounded off the width the staging mints, and the staging it
        # would otherwise have to read against survives the refusal -- asserted by arming it
        # afterwards with its own token, since a 422 never reaches the handler and "the
        # directory is still there" would hold whatever the bound did.
        prepared = client.post(
            "/api/settings/backup/restore/prepare",
            content=_make_archive(tmp_path / "up.reaper").read_bytes(),
        )
        refused = client.post(
            "/api/settings/backup/restore/cancel",
            json={"token": "a" * (restore.TOKEN_MAX_LEN + 1)},
        )
        assert refused.status_code == 422, refused.text
        armed = client.post(
            "/api/settings/backup/restore/confirm",
            json={"password": TEST_PASSWORD, "token": prepared.json()["token"]},
        )
        assert armed.status_code == 200, armed.text

    def test_confirm_refuses_a_token_wider_than_one_can_be(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        # The sibling bound, and the one that matters more: this field sits on the route that
        # reaches Argon2 (rule 72, and rule 95's reason for the bound in the first place).
        client.post(
            "/api/settings/backup/restore/prepare",
            content=_make_archive(tmp_path / "up.reaper").read_bytes(),
        )
        refused = client.post(
            "/api/settings/backup/restore/confirm",
            json={"password": TEST_PASSWORD, "token": "a" * (restore.TOKEN_MAX_LEN + 1)},
        )
        assert refused.status_code == 422, refused.text
        assert client.get("/api/settings/backup").json()["restore_armed"] is False


def _arm(client: TestClient, tmp_path: Path) -> None:
    """Upload, review, and confirm a backup, leaving the swap armed -- the state the operator
    is in when the card offers Restart now."""
    prepared = client.post(
        "/api/settings/backup/restore/prepare",
        content=_make_archive(tmp_path / "up.reaper").read_bytes(),
    )
    confirmed = client.post(
        "/api/settings/backup/restore/confirm",
        json={"password": TEST_PASSWORD, "token": prepared.json()["token"]},
    )
    assert confirmed.status_code == 200, confirmed.text


@pytest.fixture
def stops(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every time the route asks this process to stop, recorded instead of sent.

    Not optional decoration: the route's own background task calls this, and ``TestClient``
    runs background tasks, so a test that armed a restore and posted without this would
    SIGTERM the pytest process.
    """
    sent: list[int] = []
    monkeypatch.setattr(backup_api, "_stop_this_process", lambda: sent.append(1))
    return sent


class TestRestartNow:
    """The last step of a restore, in the browser rather than in a shell (#386).

    A restore stages its files and stops; the swap happens on the next boot, from preflight.
    That left the operator holding an instruction ("restart the container") at the end of a
    flow that is otherwise entirely in the browser, and the first-run wizard now opens onto
    that flow. This route is that instruction, as a button.

    It cannot promise Reaper comes back -- nothing inside a container can read its own restart
    policy -- so what is pinned here is everything it CAN promise: it stops only where stopping
    is what the operator was already being asked to do, it does not stop while files are being
    deleted, and it leaves by the door that lets a run in flight record itself.
    """

    def test_with_no_restore_armed_it_refuses(self, client: TestClient, stops: list[int]) -> None:
        """Otherwise this is a general-purpose "stop the app" endpoint, reachable by anything
        holding a session, which is not a thing Reaper offers."""
        response = client.post("/api/settings/backup/restore/restart")

        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "There's no restore waiting, so nothing was stopped."
        assert stops == []

    def test_a_staged_restore_nobody_confirmed_is_not_enough(
        self, client: TestClient, tmp_path: Path, stops: list[int]
    ) -> None:
        """The password is what arms the swap, so an upload that only reached ``prepare``
        would stop the app to apply nothing at all."""
        prepared = client.post(
            "/api/settings/backup/restore/prepare",
            content=_make_archive(tmp_path / "up.reaper").read_bytes(),
        )
        assert prepared.status_code == 200, prepared.text

        response = client.post("/api/settings/backup/restore/restart")

        assert response.status_code == 409, response.text
        assert stops == []

    def test_with_a_restore_armed_it_stops(
        self, client: TestClient, tmp_path: Path, stops: list[int]
    ) -> None:
        _arm(client, tmp_path)

        response = client.post("/api/settings/backup/restore/restart")

        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True}
        # The stop rides the response's background task, so it lands after the body -- which is
        # what lets the browser render "Reaper is stopping" instead of a dead connection.
        assert stops == [1]

    def test_it_will_not_stop_the_app_while_a_reap_is_running(
        self, client: TestClient, tmp_path: Path, stops: list[int]
    ) -> None:
        """Shutdown handles a reap in flight -- it is canceled, awaited, and recorded ABORTED
        -- but handled is not a reason to interrupt the one path that deletes files. The run
        has a graceful Stop of its own, and the staged restore will wait as long as it takes.
        """
        _arm(client, tmp_path)
        client.app.state.reap_status = ReapStatus(running=True, run_id=1)

        response = client.post("/api/settings/backup/restore/restart")

        assert response.status_code == 409, response.text
        assert response.json()["detail"] == (
            "A reap is running. Let it finish or stop it, then restart Reaper."
        )
        assert stops == []
        # ...and the restore is still armed, so the operator can press it again after the run.
        assert client.get("/api/settings/backup").json()["restore_armed"] is True

    def test_the_stop_is_the_signal_a_graceful_shutdown_reads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SIGTERM to itself, which uvicorn turns into the graceful shutdown that runs
        ``main.lifespan``'s finally. ``sys.exit`` or ``os._exit`` would leave by a door that
        skips it, dropping a reap mid-step with the run row still reading EXECUTING.

        Asserts what was actually sent (rule 119) rather than that something was called: the
        signal number IS the behavior, and any other one is a different shutdown or none.
        """
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr(backup_api.os, "kill", lambda pid, sig: sent.append((pid, sig)))

        backup_api._stop_this_process()

        assert sent == [(os.getpid(), signal.SIGTERM)]

    async def test_shutdown_lets_a_reap_in_flight_finish_recording_itself(
        self, tmp_path: Path
    ) -> None:
        """The refusal above closes the door on pressing this mid-reap. It does not close the
        window between that check and the signal, and nothing can: a reap that starts in it
        meets the shutdown anyway -- exactly as it would if the operator restarted the
        container by hand, or the host rebooted.

        What makes that survivable is two lines of ``main.lifespan``'s finally: the reap task
        is canceled AND awaited, before the engines are disposed. The executor's own cancel
        branch is pinned in ``test_reap_loop.py`` (it marks the run ABORTED and defers the Plex
        purge); this pins that shutdown gives it a live database to commit that on. A bare
        ``.cancel()``, or a ``dispose()`` moved above the await, leaves the branch half-run and
        the run row reading EXECUTING forever -- with files already deleted (#327's shape).

        The task stands in for the executor rather than being one: what is under test is the
        shutdown's ordering, and the executor needs a planned run, an armed host and a gateway
        to reach the same await. It commits through the app's OWN session factory, so a
        disposal that landed too early fails here.
        """
        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        engine.dispose()
        app = create_app(settings)
        stamp = "2026-02-01T00:00:00+00:00"

        async def reap_that_commits_on_cancel() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                factory: async_sessionmaker[AsyncSession] = app.state.session_factory
                async with factory() as session:
                    await app_settings.set_last_backup_at(session, stamp)
                    await session.commit()
                raise

        async with app.router.lifespan_context(app):
            app.state.reap_task = asyncio.create_task(reap_that_commits_on_cancel())
            # One turn, so the task is actually parked in its await when shutdown arrives.
            await asyncio.sleep(0)

        # Read on a connection of our own, after the app's engines are gone: this is the row as
        # it survives the process, which is the only version of it that matters.
        read = sa_create_engine(settings.sync_database_url)
        try:
            with read.connect() as conn:
                stored = conn.execute(
                    text("SELECT value_json FROM app_setting WHERE key = :key"),
                    {"key": app_settings.BACKUP_LAST_AT_KEY},
                ).scalar_one_or_none()
        finally:
            read.dispose()
        assert stored is not None, "shutdown canceled the reap without awaiting what it wrote"
        assert stamp in stored


class TestApiKeyIsFenced:
    """A restore replaces the whole database. It stays behind the signed-in browser --
    an automation key can read, scan, and plan, but never drive a restore."""

    def test_prepare_confirm_and_cancel_are_denied_to_api_keys(self) -> None:
        assert _api_key_allowed("POST", "/api/settings/backup/restore/prepare") is False
        assert _api_key_allowed("POST", "/api/settings/backup/restore/confirm") is False
        assert _api_key_allowed("POST", "/api/settings/backup/restore/cancel") is False

    def test_restarting_is_denied_to_api_keys(self) -> None:
        """The strongest case of the four: this one stops the app. A key is for scripts that
        scan, plan, and edit the policy, and none of those needs the process to end.

        Denied by being born outside the write allowlist rather than by being listed anywhere,
        so it stays denied without anyone remembering -- but asserted here all the same,
        because "nobody added it to the allowlist" is not something a reader can see."""
        assert _api_key_allowed("POST", "/api/settings/backup/restore/restart") is False
        assert api_key_refused("POST", "/api/settings/backup/restore/restart") is True


def test_prepare_needs_a_session(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        c.headers["X-Reaper-CSRF"] = "1"  # CSRF is present; the missing piece is the session
        assert c.post("/api/settings/backup/restore/prepare", content=b"x").status_code == 401
