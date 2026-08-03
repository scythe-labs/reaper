# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings -> Backup: the download half of Backup & Restore.

What is pinned here:

* the download is one gzip tar carrying the database AND the key material that
  decrypts it, because a database-only backup silently loses every credential;
* the cache database is never in it (it rebuilds itself, and is not a source of truth);
* the snapshot inside is a real, openable SQLite file, not the torn live one;
* downloading records "last backup" so the panel can report it;
* the download is fenced off the API-key lane -- it is the master key in a file, and
  a leaked automation key must not be able to pull it.
"""

from __future__ import annotations

import io
import json
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
from reaper.services import backup
from tests._auth import login


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A logged-in client over an empty database: exactly a fresh install."""
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


def _members(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        out: dict[str, bytes] = {}
        for member in tar.getmembers():
            handle = tar.extractfile(member)
            out[member.name] = handle.read() if handle else b""
        return out


class TestDownload:
    def test_it_returns_a_gzip_attachment_named_reaper(self, client: TestClient) -> None:
        response = client.get("/api/settings/backup/download")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/gzip"
        disposition = response.headers["content-disposition"]
        assert "attachment" in disposition
        assert ".reaper" in disposition

    def test_the_archive_carries_the_db_the_salt_and_the_manifest(self, client: TestClient) -> None:
        members = _members(client.get("/api/settings/backup/download").content)
        # The database and the salt that keys it travel together; the env-supplied key
        # ("k") is never written to a file, so there is no secret.key member to bundle.
        assert "reaper.db" in members
        assert "secret.salt" in members
        assert "secret.key" not in members
        assert "manifest.json" in members

    def test_the_db_member_is_a_real_sqlite_file(self, client: TestClient) -> None:
        members = _members(client.get("/api/settings/backup/download").content)
        assert members["reaper.db"].startswith(b"SQLite format 3\x00")

    def test_the_cache_db_is_never_in_the_backup(self, client: TestClient) -> None:
        members = _members(client.get("/api/settings/backup/download").content)
        assert "cache.db" not in members

    def test_the_manifest_describes_the_archive(self, client: TestClient) -> None:
        members = _members(client.get("/api/settings/backup/download").content)
        manifest = json.loads(members["manifest.json"])
        assert manifest["format"] == "reaper-backup"
        assert manifest["format_version"] == 1
        assert manifest["key_source"] == "env"  # the key came from REAPER_SECRET_KEY
        assert manifest["contents"] == {
            "reaper_db": True,
            "secret_key": False,
            "secret_salt": True,
            "launcher_conf": False,  # this fixture's data folder has no launcher.conf
            "cache_db": False,
        }
        # A model-built test DB has no alembic_version row; production always does. Either
        # way the field is present so the restore side can decide.
        assert "alembic_revision" in manifest

    def test_launcher_conf_travels_when_the_install_has_one(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """On Windows, macOS and the snap this file IS the settings: the port, the bind
        address and the icons live nowhere else, and the database carries none of them. A
        backup without it rebuilds an install that has forgotten all of them."""
        (tmp_path / "launcher.conf").write_text("REAPER_PORT=8421\n", encoding="utf-8")

        members = _members(client.get("/api/settings/backup/download").content)
        assert members["launcher.conf"] == b"REAPER_PORT=8421\n"
        manifest = json.loads(members["manifest.json"])
        assert manifest["contents"]["launcher_conf"] is True

    def test_an_install_without_one_carries_no_empty_member(self, client: TestClient) -> None:
        """A container has no launcher.conf and never reads one, so its backup must not
        gain a member -- an empty file restored onto a desktop install would replace the
        real settings with nothing."""
        members = _members(client.get("/api/settings/backup/download").content)
        assert "launcher.conf" not in members

    def test_a_stale_key_file_is_not_bundled_when_the_env_key_wins(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        # B-4: the env key ("k") wins over any file, so a lingering secret.key is inactive.
        # It must not travel (bundling it would silently break decrypt on a target without
        # the env var), and the manifest must say "env" so the operator sets it there.
        (tmp_path / "secret.key").write_text("staleoldkey\n", encoding="utf-8")
        members = _members(client.get("/api/settings/backup/download").content)
        assert "secret.key" not in members
        manifest = json.loads(members["manifest.json"])
        assert manifest["key_source"] == "env"
        assert manifest["contents"]["secret_key"] is False
        assert client.get("/api/settings/backup").json()["key_in_backup"] is False


class TestInfo:
    def test_fresh_install_has_never_backed_up(self, client: TestClient) -> None:
        data = client.get("/api/settings/backup").json()
        assert data["last_backup_at"] is None
        assert data["key_in_backup"] is False  # env-supplied key, no file to bundle
        assert data["reaper_db_bytes"] > 0

    def test_downloading_records_the_last_backup_time(self, client: TestClient) -> None:
        assert client.get("/api/settings/backup").json()["last_backup_at"] is None
        client.get("/api/settings/backup/download")
        assert client.get("/api/settings/backup").json()["last_backup_at"] is not None


class TestSweepStaleTemp:
    """PR-3: crash-leftover backup/restore temp is swept at boot, real state is spared."""

    def test_it_clears_leftover_temp_and_spares_real_state(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        settings.ensure_data_dir()
        # Crash debris: a backup temp dir with a partial snapshot, a restore staging temp,
        # and an upload spool file.
        (tmp_path / ".backup-tmp-abc").mkdir()
        (tmp_path / ".backup-tmp-abc" / "reaper.db").write_bytes(b"partial")
        (tmp_path / ".restore-tmp-xyz").mkdir()
        (tmp_path / ".restore-upload-123").write_bytes(b"upload")
        # Real state that must survive: the armed staging and a recovery copy carry no
        # leading dot, so the prefix match never touches them.
        (tmp_path / "pending-restore").mkdir()
        (tmp_path / "pre-restore-20260101T000000Z").mkdir()
        (tmp_path / "reaper.db").write_bytes(b"live")

        assert backup.sweep_stale_temp(settings) == 3
        assert not (tmp_path / ".backup-tmp-abc").exists()
        assert not (tmp_path / ".restore-tmp-xyz").exists()
        assert not (tmp_path / ".restore-upload-123").exists()
        assert (tmp_path / "pending-restore").exists()
        assert (tmp_path / "pre-restore-20260101T000000Z").exists()
        assert (tmp_path / "reaper.db").exists()


class TestApiKeyIsFenced:
    """The download is the whole database plus the master key. A leaked automation key
    reads plenty, but never this: it would hand over everything the key protects."""

    def test_an_api_key_may_not_download_the_backup(self) -> None:
        assert _api_key_allowed("GET", "/api/settings/backup/download") is False

    def test_but_the_metadata_read_stays_open(self) -> None:
        # The info route reveals only sizes and a timestamp, nothing secret.
        assert _api_key_allowed("GET", "/api/settings/backup") is True


def test_the_backup_download_needs_a_session(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        # No login, no cookie: the auth gate turns it away like every other /api route.
        assert c.get("/api/settings/backup/download").status_code == 401
