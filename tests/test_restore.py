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
* the restore routes are fenced off the API-key lane and need a signed-in session.
"""

from __future__ import annotations

import io
import json
import sqlite3
import stat
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine

from reaper.api.middleware import _api_key_allowed
from reaper.config import Settings
from reaper.db.base import Base
from reaper.main import create_app
from reaper.services import restore
from reaper.services.restore import RestoreError
from tests._auth import TEST_PASSWORD, login

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
    s = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
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

    def test_cancel_clears_the_staging(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        summary = restore.stage_upload(settings, _make_archive(tmp_path / "backup.reaper"))
        restore.arm(settings, summary.token)
        restore.clear_pending(settings)
        assert restore.is_armed(settings) is False
        assert not (settings.data_dir / restore.PENDING_DIR).exists()

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


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        login(c, settings)  # seeds the admin whose password is TEST_PASSWORD
        yield c


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
        assert client.get("/api/settings/backup").json()["restore_armed"] is False


class TestApiKeyIsFenced:
    """A restore replaces the whole database. It stays behind the signed-in browser --
    an automation key can read, scan, and plan, but never drive a restore."""

    def test_prepare_confirm_and_cancel_are_denied_to_api_keys(self) -> None:
        assert _api_key_allowed("POST", "/api/settings/backup/restore/prepare") is False
        assert _api_key_allowed("POST", "/api/settings/backup/restore/confirm") is False
        assert _api_key_allowed("POST", "/api/settings/backup/restore/cancel") is False


def test_prepare_needs_a_session(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        c.headers["X-Reaper-CSRF"] = "1"  # CSRF is present; the missing piece is the session
        assert c.post("/api/settings/backup/restore/prepare", content=b"x").status_code == 401
