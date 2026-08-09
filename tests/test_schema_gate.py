# SPDX-License-Identifier: AGPL-3.0-or-later
"""The boot gate that refuses a database this build cannot serve (#565).

An older Reaper pointed at a database a newer one migrated has no pending migrations, so
nothing in the boot sequence is asked whether the schema on disk is one it understands.
The refusal is one verdict (``reaper.db.schema_gate.refusal``) with two callers, and both
of them are driven here for real rather than asserted about: preflight, which every
launcher runs before ``alembic upgrade head``, and the app's own lifespan, which covers a
process started without preflight.

Three files could have held this and none holds all of it: ``test_data_dir_preflight.py``
is about the data folder, ``test_app.py`` boots a healthy app, and ``test_restore.py``
owns the same question asked about a backup file. The verdict and its two call sites are
one subject, so they are one file.

Rule 118: every test in ``TestARefusal`` fails with the gate removed. The ones in
``TestWhatMustStillBoot`` are the opposite proof and cannot -- they exist because a gate
that fails closed too eagerly locks out a fresh install and every test database in the
suite, which is the failure this shape is most likely to have.
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

#: A revision this build actually ships. Any of them is a schema boot can serve, so which
#: one the frozenset yields first does not matter.
SHIPPED_REVISION = next(iter(schema_gate.known_revisions()))

#: What a newer Reaper's migration id looks like from here: a revision this build has never
#: heard of. Deliberately not a plausible-looking hash -- a test that fails only for a
#: correctly-shaped id would be pinning the shape rather than the membership test.
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
    """``refusal`` alone: what each of the three answers is, and why."""

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

        "I could not tell" is not "it is fine" (rule 93). The database still holds a
        revision, and this build has no way to learn whether it is one it can read, so the
        boot stops. Note which revision is passed: a *shipped* one, so the refusal cannot
        be coming from the membership test.
        """

        def _no_migrations() -> Path:
            raise schema_gate.SchemaRefusedError(schema_gate.MIGRATIONS_UNREADABLE)

        monkeypatch.setattr(schema_gate, "alembic_dir", _no_migrations)
        assert schema_gate.refusal(SHIPPED_REVISION) == schema_gate.MIGRATIONS_UNREADABLE

    def test_the_operator_copy_carries_no_revision_and_no_em_dash(self) -> None:
        """Rule 21, on the two sentences an operator reads at a refused boot.

        A hash is not an explanation. The app logs it as its own field instead, which
        ``TestARefusal.test_the_app_refuses_to_serve_a_newer_database`` reads back.
        """
        for message in (schema_gate.DATABASE_IS_NEWER, schema_gate.MIGRATIONS_UNREADABLE):
            assert "—" not in message
            assert UNKNOWN_REVISION not in message
            assert SHIPPED_REVISION not in message
        # Both ways out of a rollback, named where the operator is standing.
        assert "newer version" in schema_gate.DATABASE_IS_NEWER
        assert "restore a backup" in schema_gate.DATABASE_IS_NEWER


class TestReadingTheRevision:
    """``stored_revision``: what it reads, and what it must not create."""

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

    Each of these goes red when the gate is removed from the module it exercises, which is
    the point of having two: preflight and the lifespan are different processes on a real
    install, and the container reaches one of them before the other.
    """

    def test_preflight_refuses_before_migrations_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The gate every launcher reaches: the container entrypoint, dev-local, launcher.

        Preflight returns non-zero *before* ``alembic upgrade head`` is attempted, so the
        operator gets the plain sentence rather than Alembic's own "Can't locate revision
        identified by ..." traceback, which names a hash and nothing else.
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
        """The hash the message deliberately leaves out has to be somewhere.

        It rides as its own log field beside the plain sentence, so a support request that
        arrives as "it says put the newer version back" can still be answered.
        """
        _stamp(settings.database_path, UNKNOWN_REVISION)
        # Built before the capture is installed, and thawed after: ``create_app`` calls
        # ``configure_logging``, which replaces the processor list ``capture_logs`` mutates
        # and freezes any logger used under it (conftest's ``_capturable_logs``). Entering
        # the capture first leaves it watching a list nothing writes to.
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

    A gate on the boot path that refuses too much is worse than the bug it fixes: it locks
    an operator out of an install that was working. These cannot fail when the gate is
    deleted, and they are not trying to (rule 118).
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
        """No ``alembic_version`` row at all: every test database in this suite.

        The whole suite would go red with this broken, which is exactly why it is stated
        once here rather than left as a property nobody named.
        """
        with TestClient(create_app(settings)) as booted:
            assert booted.get("/api/health").status_code == 200
