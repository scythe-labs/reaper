# SPDX-License-Identifier: AGPL-3.0-or-later
"""A restore swap that is killed part way through, and the drift guards around it.

The swap is several renames, each atomic on its own but not together, so a host reboot or
an OOM between them leaves the database replaced and the key still staged. The next boot
then saw a staging with no ``reaper.db`` in it, read that as "the staging is broken", and
deleted the whole thing -- including ``secret.key``, the ONLY copy of the key for the
database that was by then already live -- while printing that the current data was kept
(B2-21). Everything here is about that window and the two guards beside it: the auth-purge
list matching the schema (R-3) and the two size ceilings matching each other (PR-11).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import AUTH_BEARING_TABLES, NOT_AUTH_BEARING_TABLES
from reaper.secrets import KEY_FILENAME, SALT_FILENAME
from reaper.services import restore
from reaper.services.backup import DB_ARCNAME, MAX_DB_BYTES

#: Column-name fragments that suggest a row carries a credential of some kind. Deliberately
#: broad: the test's job is to force a DECISION about every such table, not to guess right.
#: A table that holds someone else's key (``instance``) is listed as considered-and-kept,
#: the same as one that holds an account.
_AUTH_COLUMN_HINTS = ("token", "password", "session", "api_key", "secret")
#: Table-name fragments with the same meaning.
_AUTH_TABLE_HINTS = ("auth", "session", "login", "recovery", "token", "credential")


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, secret_key="k")


def _sqlite_file(path: Path) -> None:
    """A real, minimal SQLite file, so ``_looks_like_sqlite`` reads it as one."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.commit()
    con.close()


def _stage(tmp_path: Path, *, with_db: bool, swapping: str | None = None) -> Path:
    """Build an armed staging directory. ``swapping`` writes the mid-swap marker."""
    pending = tmp_path / restore.PENDING_DIR
    pending.mkdir(parents=True)
    if with_db:
        _sqlite_file(pending / DB_ARCNAME)
    (pending / KEY_FILENAME).write_text("staged-key\n")
    (pending / SALT_FILENAME).write_text("00112233\n")
    (pending / restore.READY_MARKER).write_text("2026-07-24T00:00:00Z\n")
    if swapping is not None:
        (pending / restore.SWAP_MARKER).write_text(swapping + "\n")
    return pending


class TestAnInterruptedSwapIsFinished:
    def test_the_staged_key_survives_and_lands(self, tmp_path: Path, capsys) -> None:
        """The exact interrupted state: the staged database has already been renamed into
        ``data/`` and the key has not. Discarding here deletes the key that opens the
        database now serving, so the install boots on a restored database, mints a fresh
        key, and every stored service credential silently stops decrypting."""
        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        recovery = tmp_path / "pre-restore-20260724T000000Z"
        recovery.mkdir()
        _sqlite_file(tmp_path / DB_ARCNAME)  # already the restored one
        pending = _stage(tmp_path, with_db=False, swapping=recovery.name)

        assert restore.apply_pending_restore(settings) is True

        assert (tmp_path / KEY_FILENAME).read_text() == "staged-key\n"
        assert (tmp_path / SALT_FILENAME).read_text() == "00112233\n"
        assert not pending.exists()

    def test_it_says_the_restore_already_happened(self, tmp_path: Path, capsys) -> None:
        """The old message asserted "current data kept" in a state where the current data
        had already been replaced. Copy that is wrong about a restore is worse than none:
        it is read while deciding whether to panic."""
        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        _sqlite_file(tmp_path / DB_ARCNAME)
        _stage(tmp_path, with_db=False, swapping="pre-restore-20260724T000000Z")

        restore.apply_pending_restore(settings)
        err = capsys.readouterr().err
        assert "current data kept" not in err
        assert "interrupted" in err
        assert "pre-restore-20260724T000000Z" in err


class TestASwapKilledMidLoopDoesNotStrandAWal:
    """The other half of the same window, one rename earlier.

    The move-aside walks ``reaper.db`` first and its ``-wal``/``-shm`` after. A kill
    between those two renames left the previous database's write-ahead log sitting in
    ``data/``, and the resume path only ever moved the STAGED files in -- so the restored
    database landed beside a log written for a different database. SQLite validates a WAL
    by its own header and frame checksums, never by any binding to the database file, so
    it replays. Neither copy survives it: the restored database takes writes it never
    made, and the pre-restore copy is missing the log those writes were in.
    """

    def test_a_stale_wal_follows_its_own_database_into_recovery(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        recovery = tmp_path / "pre-restore-20260724T000000Z"
        recovery.mkdir()
        # The interrupted state: the live database made it across, its sidecars did not.
        _sqlite_file(recovery / DB_ARCNAME)
        (tmp_path / f"{DB_ARCNAME}-wal").write_bytes(b"previous-database-wal")
        (tmp_path / f"{DB_ARCNAME}-shm").write_bytes(b"previous-database-shm")
        _stage(tmp_path, with_db=True, swapping=recovery.name)

        assert restore.apply_pending_restore(settings) is True

        assert not (tmp_path / f"{DB_ARCNAME}-wal").exists(), (
            "the previous database's write-ahead log was left beside the restored "
            "database, where SQLite will replay it into a database it never belonged to"
        )
        assert not (tmp_path / f"{DB_ARCNAME}-shm").exists()
        assert (recovery / f"{DB_ARCNAME}-wal").read_bytes() == b"previous-database-wal"
        assert (recovery / f"{DB_ARCNAME}-shm").read_bytes() == b"previous-database-shm"
        assert (tmp_path / DB_ARCNAME).is_file()

    def test_a_kill_before_any_rename_still_saves_the_live_database(self, tmp_path: Path) -> None:
        """Killed after the marker but before the first move, everything is still live.
        The resume path must move it aside, not let the staged copy overwrite it."""
        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        recovery = tmp_path / "pre-restore-20260724T000000Z"
        recovery.mkdir()
        _sqlite_file(tmp_path / DB_ARCNAME)
        live = (tmp_path / DB_ARCNAME).read_bytes()
        (tmp_path / KEY_FILENAME).write_text("live-key\n")
        _stage(tmp_path, with_db=True, swapping=recovery.name)

        assert restore.apply_pending_restore(settings) is True

        assert (recovery / DB_ARCNAME).read_bytes() == live
        assert (recovery / KEY_FILENAME).read_text() == "live-key\n"
        assert (tmp_path / KEY_FILENAME).read_text() == "staged-key\n"

    def test_an_already_restored_database_is_not_moved_aside(self, tmp_path: Path) -> None:
        """Once the staged database has been moved in, ``data/reaper.db`` is the RESTORED
        one. Sweeping it into recovery on the next boot would undo the restore and leave
        the install with no database at all, which is why the sweep is scoped by whether
        the staged copy is still staged."""
        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        recovery = tmp_path / "pre-restore-20260724T000000Z"
        recovery.mkdir()
        _sqlite_file(tmp_path / DB_ARCNAME)
        restored = (tmp_path / DB_ARCNAME).read_bytes()
        _stage(tmp_path, with_db=False, swapping=recovery.name)

        assert restore.apply_pending_restore(settings) is True

        assert (tmp_path / DB_ARCNAME).read_bytes() == restored
        assert not (recovery / DB_ARCNAME).exists()

    def test_the_restored_databases_own_wal_is_left_where_it_is(self, tmp_path: Path) -> None:
        """Once the staged database is in place, data/reaper.db-wal is ITS log.

        _move_staged_in ends in an rmtree that swallows its own failure, so a completed
        swap can still leave the marker behind: a read-only directory, or a held file on a
        network mount. Every later boot then re-enters this branch with the app's own live
        WAL sitting beside the restored database. Sweeping the sidecars "just in case"
        looked harmless because the branch looked unreachable, and it is neither: it loses
        every transaction still in that log, and drops it on top of the recovery copy's own
        log, which is precisely the mismatched-WAL corruption the sweep exists to prevent.
        """
        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        recovery = tmp_path / "pre-restore-20260724T000000Z"
        recovery.mkdir()
        _sqlite_file(recovery / DB_ARCNAME)
        (recovery / f"{DB_ARCNAME}-wal").write_bytes(b"the-previous-databases-wal")
        # The swap finished; only the staging cleanup did not.
        _sqlite_file(tmp_path / DB_ARCNAME)
        (tmp_path / f"{DB_ARCNAME}-wal").write_bytes(b"the-restored-databases-own-wal")
        _stage(tmp_path, with_db=False, swapping=recovery.name)

        assert restore.apply_pending_restore(settings) is True

        assert (tmp_path / f"{DB_ARCNAME}-wal").read_bytes() == b"the-restored-databases-own-wal"
        assert (recovery / f"{DB_ARCNAME}-wal").read_bytes() == b"the-previous-databases-wal"

    def test_a_marker_naming_a_plain_file_still_finishes_the_restore(self, tmp_path: Path) -> None:
        """A hand-edited marker must not be able to stop the restore it is describing."""
        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        (tmp_path / "pre-restore-not-a-directory").write_text("x\n")
        _sqlite_file(tmp_path / DB_ARCNAME)
        _stage(tmp_path, with_db=True, swapping="pre-restore-not-a-directory")

        assert restore.apply_pending_restore(settings) is True
        assert (tmp_path / KEY_FILENAME).read_text() == "staged-key\n"

    def test_an_unusable_marker_name_still_lands_inside_the_data_dir(self, tmp_path: Path) -> None:
        """The marker is written by this process, but it is read back off disk, so it is
        treated as untrusted: a name that is not one of ours gets a fresh folder rather
        than being joined onto the data directory."""
        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        _sqlite_file(tmp_path / DB_ARCNAME)
        _stage(tmp_path, with_db=True, swapping="../escaped")

        assert restore.apply_pending_restore(settings) is True

        assert not (tmp_path.parent / "escaped").exists()
        made = [p for p in tmp_path.iterdir() if p.name.startswith(restore.PRE_RESTORE_PREFIX)]
        assert len(made) == 1
        assert (made[0] / DB_ARCNAME).is_file()


class TestAnUnusableStagingIsStillDiscarded:
    def test_the_live_data_is_untouched_and_the_message_is_true(
        self,
        tmp_path: Path,
        capsys,
    ) -> None:
        """With no swap marker nothing has been moved, so "current data kept" IS true."""
        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        _sqlite_file(tmp_path / DB_ARCNAME)
        live = (tmp_path / DB_ARCNAME).read_bytes()
        pending = tmp_path / restore.PENDING_DIR
        pending.mkdir()
        (pending / DB_ARCNAME).write_text("not a database")
        (pending / restore.READY_MARKER).write_text("x\n")

        assert restore.apply_pending_restore(settings) is False
        assert (tmp_path / DB_ARCNAME).read_bytes() == live
        assert not pending.exists()
        assert "current data kept" in capsys.readouterr().err

    def test_the_key_material_is_parked_rather_than_deleted(self, tmp_path: Path) -> None:
        """``rmtree`` took the key and salt with it. They are tiny, they are the only copy
        of what opens that backup's credentials, and an operator who staged the wrong file
        may well want the right one from the same source."""
        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        pending = tmp_path / restore.PENDING_DIR
        pending.mkdir()
        (pending / DB_ARCNAME).write_text("not a database")
        (pending / KEY_FILENAME).write_text("staged-key\n")
        (pending / SALT_FILENAME).write_text("00112233\n")
        (pending / restore.READY_MARKER).write_text("x\n")

        assert restore.apply_pending_restore(settings) is False

        parked = [p for p in tmp_path.iterdir() if p.name.startswith(restore.PRE_RESTORE_PREFIX)]
        assert len(parked) == 1
        assert (parked[0] / KEY_FILENAME).read_text() == "staged-key\n"
        assert (parked[0] / SALT_FILENAME).read_text() == "00112233\n"


class TestACleanSwapLeavesNoMarker:
    def test_the_marker_is_written_then_removed_with_the_staging(self, tmp_path: Path) -> None:
        """A marker left behind would make the NEXT armed restore look interrupted."""
        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        _sqlite_file(tmp_path / DB_ARCNAME)
        _stage(tmp_path, with_db=True)

        assert restore.apply_pending_restore(settings) is True
        assert not (tmp_path / restore.PENDING_DIR).exists()
        assert (tmp_path / KEY_FILENAME).read_text() == "staged-key\n"
        # And the old data is recoverable, which is the point of moving it aside.
        recovery = [p for p in tmp_path.iterdir() if p.name.startswith(restore.PRE_RESTORE_PREFIX)]
        assert len(recovery) == 1
        assert (recovery[0] / DB_ARCNAME).is_file()

    def test_nothing_happens_without_the_ready_marker(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        pending = tmp_path / restore.PENDING_DIR
        pending.mkdir()
        _sqlite_file(pending / DB_ARCNAME)

        assert restore.apply_pending_restore(settings) is False
        assert (pending / DB_ARCNAME).is_file()


class TestTheAuthPurgeListTracksTheSchema:
    """The purge list was a literal inside the restore code, tied to nothing. A new
    auth-bearing table would have ridden a restore through in silence, so a session or
    reset link valid when the backup was taken would work again after the swap -- exactly
    what the purge exists to prevent (R-3)."""

    def test_every_listed_table_exists(self) -> None:
        known = set(Base.metadata.tables)
        assert set(AUTH_BEARING_TABLES) <= known
        assert set(NOT_AUTH_BEARING_TABLES) <= known

    def test_no_auth_bearing_table_is_unaccounted_for(self) -> None:
        accounted = set(AUTH_BEARING_TABLES) | set(NOT_AUTH_BEARING_TABLES)
        missed = []
        for name, table in Base.metadata.tables.items():
            if name in accounted:
                continue
            columns = [c.name.lower() for c in table.columns]
            looks_auth = any(hint in name.lower() for hint in _AUTH_TABLE_HINTS) or any(
                hint in column for hint in _AUTH_COLUMN_HINTS for column in columns
            )
            if looks_auth:
                missed.append(name)
        assert not missed, (
            f"{missed} look auth-bearing but are in neither AUTH_BEARING_TABLES nor "
            "NOT_AUTH_BEARING_TABLES. Add each to one of them (db/models.py): the first "
            "purges it from a restored database, the second records that it was considered."
        )

    def test_the_purge_empties_them(self, tmp_path: Path) -> None:
        db = tmp_path / "staged.db"
        con = sqlite3.connect(db)
        for table in AUTH_BEARING_TABLES:
            con.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
            con.execute(f"INSERT INTO {table} (id) VALUES (1)")  # noqa: S608
        con.commit()
        con.close()

        restore._purge_auth_state(db)

        con = sqlite3.connect(db)
        try:
            for table in AUTH_BEARING_TABLES:
                assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
        finally:
            con.close()

    def test_a_backup_predating_a_table_still_purges(self, tmp_path: Path) -> None:
        """An older backup may not have every table yet; that is nothing to purge, not an
        error that would block the restore."""
        db = tmp_path / "old.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE app_user (id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()

        restore._purge_auth_state(db)  # must not raise


class TestTheSizeCeilingsAgree:
    def test_the_restore_cap_is_the_backup_cap(self) -> None:
        """A backup its own restore rejects is worse than no backup: the operator believes
        they are covered right up until they are not (PR-11)."""
        assert restore._MEMBER_CAPS[DB_ARCNAME] == MAX_DB_BYTES

    def test_the_backup_refuses_to_write_one_it_could_not_restore(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from reaper.services import backup

        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        _sqlite_file(tmp_path / "reaper.db")
        monkeypatch.setattr(backup, "MAX_DB_BYTES", 1)

        with pytest.raises(backup.BackupTooLargeError):
            backup._build_sync(settings, "2026-07-24T00:00:00Z")

        # And it cleaned up after itself rather than stranding a partial snapshot.
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(backup.BACKUP_TMP_PREFIX)]
