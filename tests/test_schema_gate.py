# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests the boot gate that refuses to serve a database this build cannot read.

An older Reaper pointed at a database a newer one already migrated sees no pending
migrations, so nothing in the boot sequence checks whether it actually understands the
schema on disk. The refusal is one function, ``reaper.db.schema_gate.refusal``, with two
callers. Both callers are driven here for real, not just asserted about: preflight, which
every launcher runs before ``alembic upgrade head``, and the app's own lifespan, which
covers a process started without preflight.

Three other files could have held pieces of this, but none holds all of it:
``test_data_dir_preflight.py`` is about the data folder, ``test_app.py`` boots a healthy
app, and ``test_restore.py`` asks the same question about a backup file. The verdict and
its two call sites are one subject, so they get one file here.

Every test in ``TestARefusal`` fails if the gate is removed. The tests in
``TestWhatMustStillBoot`` prove the opposite and cannot fail that way. They exist because a
gate that fails closed too eagerly would lock out a fresh install and every test database
in the suite, which is the most likely way this kind of gate breaks.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from reaper import preflight
from reaper.config import Settings
from reaper.db import schema_gate
from reaper.main import create_app
from tests.conftest import uncache_module_loggers

#: A revision this build actually ships. Boot can serve any revision this build knows
#: about, so it does not matter which one the frozenset happens to yield first.
SHIPPED_REVISION = next(iter(schema_gate.known_revisions()))

#: A revision this build has never heard of, standing in for what a newer Reaper's
#: migration id looks like from here. It is deliberately not a plausible-looking hash,
#: since a test that only fails for a correctly-shaped id would be pinning the shape, not
#: the membership check.
UNKNOWN_REVISION = "0000fromanewerreaper"


def _stamp(db_path: Path, revision: str) -> None:
    """Write an ``alembic_version`` row the way a real migrated database carries one."""
    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32))")
        con.execute("DELETE FROM alembic_version")
        con.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        con.commit()
    finally:
        con.close()


class TestTheVerdict:
    """Tests what each of ``refusal``'s three possible answers means, and why."""

    def test_no_revision_row_is_allowed(self) -> None:
        """A database built straight from the models carries no ``alembic_version`` row.

        That is every test database in this suite and a first boot before its own
        migrations run, so this is the case a gate written to fail closed breaks first.
        """
        assert schema_gate.refusal(None) is None

    def test_a_revision_this_build_ships_is_allowed(self) -> None:
        assert schema_gate.refusal(SHIPPED_REVISION) is None

    def test_a_revision_this_build_never_shipped_is_refused(self) -> None:
        assert schema_gate.refusal(UNKNOWN_REVISION) == schema_gate.DATABASE_IS_NEWER

    def test_unreadable_migrations_refuse_rather_than_wave_it_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no migrations to compare against, the question is unanswerable.

        Not knowing whether the database is safe is treated as unsafe, not as fine. The
        database still holds a revision, but this build has no way to check it, so the boot
        stops. The revision passed in this test is a *shipped* one, so the refusal cannot be
        coming from the membership check.
        """

        def _no_migrations() -> Path:
            raise schema_gate.SchemaRefusedError(schema_gate.MIGRATIONS_UNREADABLE)

        monkeypatch.setattr(schema_gate, "alembic_dir", _no_migrations)
        assert schema_gate.refusal(SHIPPED_REVISION) == schema_gate.MIGRATIONS_UNREADABLE

    def test_the_operator_copy_carries_no_revision_and_no_em_dash(self) -> None:
        """Checks the two sentences an operator reads at a refused boot.

        The message omits the hash, since a hash alone would not explain anything to a
        normal person. The app logs the hash separately, as its own field, which
        ``TestARefusal.test_the_app_refuses_to_serve_a_newer_database`` reads back.
        """
        for message in (schema_gate.DATABASE_IS_NEWER, schema_gate.MIGRATIONS_UNREADABLE):
            assert "—" not in message
            assert UNKNOWN_REVISION not in message
            assert SHIPPED_REVISION not in message
        # States both ways to recover: upgrade Reaper, or restore an older backup.
        assert "newer version" in schema_gate.DATABASE_IS_NEWER
        assert "restore a backup" in schema_gate.DATABASE_IS_NEWER


class TestReadingTheRevision:
    """Tests what ``stored_revision`` reads, and what it must never create."""

    def test_a_database_that_is_not_there_yet_is_not_conjured(self, tmp_path: Path) -> None:
        """The gate runs before migrations, so on a first boot there is no file at all.

        ``sqlite3.connect`` creates whatever path it is handed, so without the existence
        check this would leave Alembic migrating a file the gate invented. The second
        assertion is the one that goes red.
        """
        missing = tmp_path / "reaper.db"

        assert schema_gate.stored_revision(missing) is None
        assert not missing.exists()

    def test_a_stamped_database_reads_back_its_revision(self, tmp_path: Path) -> None:
        db_path = tmp_path / "reaper.db"
        _stamp(db_path, UNKNOWN_REVISION)

        assert schema_gate.stored_revision(db_path) == UNKNOWN_REVISION

    def test_a_database_with_no_version_table_reads_as_none(self, tmp_path: Path) -> None:
        db_path = tmp_path / "reaper.db"
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE candidate (id INTEGER)")
        con.commit()
        con.close()

        assert schema_gate.stored_revision(db_path) is None


class TestARefusal:
    """Both call sites, driven against a database from a newer Reaper.

    Each test fails if the gate is removed from the module it exercises. That is why there
    are two: preflight and the app's lifespan run as different processes on a real install,
    and the container reaches one before the other.
    """

    def test_preflight_refuses_before_migrations_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The gate every launcher reaches, whether the container entrypoint, dev-local, or
        the packaged launcher.

        Preflight returns non-zero before ``alembic upgrade head`` runs, so the operator
        sees the plain sentence instead of Alembic's own "Can't locate revision identified
        by ..." traceback, which names only a hash.
        """
        settings = Settings(data_dir=tmp_path, secret_key="test-key")
        _stamp(settings.database_path, UNKNOWN_REVISION)
        monkeypatch.setattr(preflight, "get_settings", lambda: settings)

        assert preflight.main() == 1
        assert schema_gate.DATABASE_IS_NEWER in capsys.readouterr().err

    def test_the_app_refuses_to_serve_a_newer_database(self, settings: Settings) -> None:
        """The backstop, for a process started without preflight.

        The refusal is raised out of the lifespan, which is what uvicorn turns into
        "Application startup failed" and a non-zero exit, so no request is ever answered.
        """
        _stamp(settings.database_path, UNKNOWN_REVISION)

        # ``pytest.raises`` is entered first, so it is what catches the lifespan raising
        # out of ``TestClient.__enter__``. Nothing inside the body ever runs.
        with (
            pytest.raises(schema_gate.SchemaRefusedError) as excinfo,
            TestClient(create_app(settings)),
        ):
            pass

        assert str(excinfo.value) == schema_gate.DATABASE_IS_NEWER

    def test_the_refused_boot_logs_the_revision_for_support(self, settings: Settings) -> None:
        """The hash the operator-facing message leaves out still has to be recorded somewhere.

        It appears as its own log field next to the plain sentence, so a support request
        that only says "it told me to put the newer version back" can still be answered.
        """
        _stamp(settings.database_path, UNKNOWN_REVISION)
        # ``create_app`` must run, and ``uncache_module_loggers`` after it, before
        # ``capture_logs`` starts. ``create_app`` calls ``configure_logging``, which
        # replaces the processor list ``capture_logs`` reads from and caches any logger
        # already built (conftest's ``_capturable_logs``). Starting the capture first would
        # leave it watching a list nothing ever writes to.
        app = create_app(settings)
        uncache_module_loggers()

        with capture_logs() as logs, pytest.raises(schema_gate.SchemaRefusedError), TestClient(app):
            pass

        refusals = [entry for entry in logs if entry.get("event") == "db.schema_refused"]
        assert len(refusals) == 1
        assert refusals[0]["revision"] == UNKNOWN_REVISION
        assert refusals[0]["detail"] == schema_gate.DATABASE_IS_NEWER


class TestWhatMustStillBoot:
    """The other direction, and the reason it needs its own class.

    A gate on the boot path that refuses too much is worse than the bug it fixes. It would
    lock an operator out of an install that was working. These tests cannot fail when the
    gate is deleted, and they are not trying to.
    """

    def test_a_first_boot_with_no_database_at_all_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="test-key")
        monkeypatch.setattr(preflight, "get_settings", lambda: settings)

        assert preflight.main() == 0
        assert not settings.database_path.exists()

    def test_a_database_at_a_shipped_revision_passes_preflight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="test-key")
        _stamp(settings.database_path, SHIPPED_REVISION)
        monkeypatch.setattr(preflight, "get_settings", lambda: settings)

        assert preflight.main() == 0

    def test_a_database_at_a_shipped_revision_serves(self, settings: Settings) -> None:
        """What a real install looks like the moment after ``alembic upgrade head``."""
        _stamp(settings.database_path, SHIPPED_REVISION)

        with TestClient(create_app(settings)) as booted:
            assert booted.get("/api/health").status_code == 200

    def test_a_model_built_database_serves(self, settings: Settings) -> None:
        """Every test database in this suite has no ``alembic_version`` row at all.

        The whole suite would go red if this broke, which is why the property is stated
        once here instead of being left as something nobody names.
        """
        with TestClient(create_app(settings)) as booted:
            assert booted.get("/api/health").status_code == 200
