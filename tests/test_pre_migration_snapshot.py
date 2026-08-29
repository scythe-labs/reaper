# SPDX-License-Identifier: AGPL-3.0-or-later
"""The copy of the database a destructive migration falls back to.

Nothing sits between preflight and ``alembic upgrade head``. Most migrations only ever add
columns, so that cost little, but some releases drop columns, and a ``downgrade()``
recreates a dropped column without its data. The file written here is the only way back
from a migration that goes wrong.

Four subjects, in the order a boot runs them: which pending revisions ask for a snapshot,
what the snapshot is and how many are kept, preflight refusing rather than migrating
without one, and a restore that really does bring a dropped column back.

The last of those is the point of the whole file. A snapshot nobody has restored from is
just a file, not a proven recovery path. ``TestRestoringFromTheSnapshot`` breaks a
database the same way a bad migration would (a column dropped, and a batch rebuild that
forgot to restate ``COLLATE NOCASE``) and puts it back through the restore an operator
would actually use.
"""

from __future__ import annotations

import errno
import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import Boolean, create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from reaper import config as reaper_config
from reaper import preflight
from reaper.config import Settings
from reaper.db import schema_gate
from reaper.services import backup, restore
from tests.test_migrations import PROJECT_ROOT, _alembic_config

#: Release M, the first revision to carry the marker. Its four ``batch_alter_table``
#: blocks are four full table copies taken from reflection. Its predecessor sits below
#: it, so a walk starting there finds a marked revision.
#:
#: A walk starting above release M finds one too. ``e2f3a4b5c6d7`` is the M+1 sweep, and
#: it carries the marker for a stronger reason. A dropped column's data is gone rather
#: than merely at risk. Only a database already at head finds nothing pending.
_RELEASE_M = "e6f7a8b9c0d1"
_BEFORE_RELEASE_M = "d5e6f7a8b9c0"

_LIST_BODY = json.dumps({"tags": ["keep"], "match": "any"})


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Settings, Config]:
    """A throwaway install at ``tmp_path`` and the alembic Config that migrates it.

    ``_alembic_config`` pins the settings object env.py resolves the database URL from, so
    it is read back rather than built a second time, keeping one object and one ``reaper.db``.
    """
    config = _alembic_config(tmp_path, monkeypatch)
    return reaper_config.get_settings(), config


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))


class TestWhichPendingRevisionsAskForASnapshot:
    """The marker is on the revision because the revision is the only thing that knows.

    An ordinary ``ADD COLUMN`` costs nothing here and asks for nothing. Everything that
    cannot be read is answered ``True``, because an unnecessary snapshot costs a file and a
    missing one costs the database.
    """

    def test_a_revision_below_a_marked_one_asks(self) -> None:
        assert schema_gate.needs_snapshot(_BEFORE_RELEASE_M) is True

    def test_only_the_revisions_that_lose_data_carry_the_marker(self) -> None:
        """Every revision pending from release M, and which of them asks for the copy.

        ``e2f3a4b5c6d7`` is the M+1 sweep, and it drops six columns, so the honest answer
        from release M is True. Asserting *which* revision made it True is the stronger
        check: a marker landing on one of the four additive revisions beside it would fail
        here.
        """
        pending = list(_script().iterate_revisions("head", _RELEASE_M))
        assert pending, "release M is head; this case needs a revision shipped after it"

        marked = {
            r.revision for r in pending if getattr(r.module, schema_gate.SNAPSHOT_ATTR, False)
        }
        assert marked == {"e2f3a4b5c6d7"}, (
            "the revisions asking for a pre-migration copy are not the ones that lose data. "
            "A snapshot nobody needs costs a file; a missing one costs the database."
        )
        # So an operator sitting on v2026.8.4 gets their database copied aside on the boot
        # that crosses the sweep, which is the whole reason the marker is on the revision.
        assert schema_gate.needs_snapshot(_RELEASE_M) is True

    def test_a_database_at_head_asks_for_nothing(self) -> None:
        head = _script().get_current_head()
        assert head is not None
        assert schema_gate.needs_snapshot(head) is False

    def test_nothing_on_disk_asks_for_nothing(self) -> None:
        """No ``alembic_version`` row means a first boot, or a database a test built
        straight from the models. There is nothing to lose, so there is nothing to copy.
        """
        assert schema_gate.needs_snapshot(None) is False

    def test_at_least_one_shipped_revision_carries_the_marker(self) -> None:
        """Proves the marker guard can actually fire, by naming a revision that carries it.

        Release M is the one that carries it today. Naming it here catches the attribute
        being renamed on only one side. Without this test, the walk would go on reading
        ``False`` for every revision, and nothing would notice, because "no snapshot
        needed" is exactly what a green additive upgrade looks like.
        """
        marked = [
            revision.revision
            for revision in _script().walk_revisions()
            if getattr(revision.module, schema_gate.SNAPSHOT_ATTR, False)
        ]
        assert _RELEASE_M in marked

    def test_a_script_directory_that_will_not_read_asks_anyway(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken() -> ScriptDirectory:
            raise RuntimeError("no migrations here")

        monkeypatch.setattr(schema_gate, "_script", _broken)
        assert schema_gate.needs_snapshot(_BEFORE_RELEASE_M) is True

    def test_a_revision_that_is_not_on_the_way_to_head_asks_anyway(self) -> None:
        """Alembic raises on a revision it cannot place, and "I could not tell" must count
        as "needs a copy," not as certainty that nothing does.
        """
        assert schema_gate.needs_snapshot("not-a-revision") is True


class TestTheSnapshotAndWhatIsKept:
    """What lands in ``data/pre-migration/``.

    An ordinary ``.reaper`` archive, owner-only, named for the revision the database sat
    at, and pruned to the newest few.
    """

    def test_it_writes_an_archive_the_restore_side_accepts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings, config = _install(tmp_path, monkeypatch)
        command.upgrade(config, "head")
        revision = schema_gate.stored_revision(settings.database_path)

        path = backup.snapshot_before_migration(settings, revision)

        assert path.parent == tmp_path / backup.PRE_MIGRATION_DIR
        assert revision is not None and revision in path.name
        # The restore side is the recovery path, so the artifact is proved against it
        # rather than against a shape this test believes it has.
        summary = restore.stage_upload(settings, path)
        assert summary.revision == revision

    def test_the_folder_and_the_file_are_owner_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each archive carries ``secret.key``, so the folder is as sensitive as the key
        itself."""
        settings, config = _install(tmp_path, monkeypatch)
        command.upgrade(config, "head")

        path = backup.snapshot_before_migration(settings, "any")

        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600

    def test_only_the_newest_few_are_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Written one per destructive release, so the bound is small on purpose. The
        stamp is second-resolution, so the revision in the name is what separates these."""
        settings, config = _install(tmp_path, monkeypatch)
        command.upgrade(config, "head")
        directory = tmp_path / backup.PRE_MIGRATION_DIR
        directory.mkdir(mode=0o700)
        older = [
            directory / f"{backup.PRE_MIGRATION_PREFIX}2026010{n}T000000-old{n}.reaper"
            for n in range(1, 5)
        ]
        for stale in older:
            stale.write_bytes(b"an earlier release's snapshot")

        kept = backup.snapshot_before_migration(settings, "new")

        names = sorted(p.name for p in directory.iterdir())
        assert len(names) == backup.KEEP_PRE_MIGRATION
        assert kept.name in names
        assert older[0].name not in names and older[1].name not in names
        assert older[-1].name in names  # the newest of the old ones survives

    def test_a_snapshot_that_cannot_be_written_raises_and_strands_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A full disk is the case this is written for. The half-written copy must not be
        left behind, because a stranded multi-gigabyte file makes a full disk worse.
        """
        settings, config = _install(tmp_path, monkeypatch)
        command.upgrade(config, "head")

        def _full(*_a: object, **_k: object) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(backup, "_build_into", _full)

        with pytest.raises(OSError, match="No space left"):
            backup.snapshot_before_migration(settings, "any")

        assert not [p for p in tmp_path.iterdir() if p.name.startswith(backup.BACKUP_TMP_PREFIX)]
        assert not (tmp_path / backup.PRE_MIGRATION_DIR).exists()


class TestPreflightRefusesRatherThanMigrateUnprotected:
    """Preflight is the one thing every launcher runs immediately before ``alembic upgrade
    head``. The container entrypoint, ``scripts/dev-local.sh``, and ``launcher.main`` all
    call it, and the wiring is pinned here so the three callers carry no copy of it to drift.

    These drive the real walk against a real database migrated to a real revision.
    Nothing is stubbed except the disk failure, which has no other way to happen.
    """

    @staticmethod
    def _at(revision: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
        settings, config = _install(tmp_path, monkeypatch)
        command.upgrade(config, revision)
        monkeypatch.setattr(preflight, "get_settings", lambda: settings)
        return settings

    def test_a_pending_destructive_revision_gets_its_snapshot(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._at(_BEFORE_RELEASE_M, tmp_path, monkeypatch)

        assert preflight.main() == 0

        written = list((tmp_path / backup.PRE_MIGRATION_DIR).iterdir())
        assert len(written) == 1
        assert written[0].name in capsys.readouterr().err

    def test_a_database_that_will_not_say_what_it_is_is_copied_rather_than_migrated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``stored_revision`` answers ``None`` for three cases, and only two of them mean
        "nothing to lose." The third is a database it could not read. Treating that as "no
        snapshot needed" would be a fail-open: a lock released between preflight's read and
        alembic's open could let a migration that ``downgrade()`` cannot undo run unprotected.

        A file that exists and carries no ``alembic_version`` is that third case, driven
        without a race. This also asserts ``needs_snapshot(None)`` is ``False``, because
        that is the value the guard has to survive. The branch lives at the call site, so a
        test that only drove ``needs_snapshot`` could not see it.
        """
        settings = Settings(data_dir=tmp_path, secret_key="k")
        settings.ensure_data_dir()
        con = sqlite3.connect(settings.database_path)
        con.execute("CREATE TABLE something (a)")
        con.close()
        monkeypatch.setattr(preflight, "get_settings", lambda: settings)
        assert schema_gate.stored_revision(settings.database_path) is None
        assert schema_gate.needs_snapshot(None) is False

        assert preflight.main() == 0

        written = list((tmp_path / backup.PRE_MIGRATION_DIR).iterdir())
        assert len(written) == 1
        assert "unknown" in written[0].name  # the revision it could not read

    def test_a_database_held_open_by_something_else_stops_the_boot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same guard at its other outcome, and the one an operator can actually hit.

        A second instance on a shared data dir holds a write lock, so the database can
        neither answer its revision nor be copied. Refusing to boot is the fail-closed
        response, and it is what the guard buys. Without it, this would boot and migrate
        anyway.
        """
        settings, config = _install(tmp_path, monkeypatch)
        command.upgrade(config, "head")
        monkeypatch.setattr(preflight, "get_settings", lambda: settings)

        holder = sqlite3.connect(settings.database_path, isolation_level=None)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            seen: list[str] = []

            assert preflight.main(seen.append) == 1
        finally:
            holder.close()

        assert len(seen) == 1
        assert backup.SNAPSHOT_FAILED in seen[0]

    def test_a_first_boot_with_no_database_still_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other side of the guard above. ``None`` with no file is the ordinary first
        boot, and copying a database that does not exist yet is work for nothing.
        """
        settings = Settings(data_dir=tmp_path, secret_key="k")
        monkeypatch.setattr(preflight, "get_settings", lambda: settings)
        assert not settings.database_path.exists()

        assert preflight.main() == 0

        assert not (tmp_path / backup.PRE_MIGRATION_DIR).exists()

    def test_an_upgrade_crossing_the_column_sweep_copies_the_database_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The case an operator on v2026.8.4 actually hits.

        ``e2f3a4b5c6d7`` drops six columns, so booting from release M crosses it and the
        copy is taken. The no-copy side is pinned separately, by the two cases above: a
        database already at head, and a first boot with no database at all.
        """
        self._at(_RELEASE_M, tmp_path, monkeypatch)

        assert preflight.main() == 0

        assert (tmp_path / backup.PRE_MIGRATION_DIR).exists()

    def test_a_snapshot_that_cannot_be_written_stops_the_boot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fatal, unlike the two housekeeping sweeps above it in ``preflight.main``.

        A migration whose own ``downgrade()`` cannot undo it must not run with nothing to
        fall back to, so the boot ends here and ``alembic upgrade head`` never starts.

        The message reaches ``refuse`` rather than only stderr, because a frozen desktop
        build's stderr is the null device.
        """
        self._at(_BEFORE_RELEASE_M, tmp_path, monkeypatch)

        def _full(*_a: object, **_k: object) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(backup, "_build_into", _full)
        seen: list[str] = []

        assert preflight.main(seen.append) == 1

        assert len(seen) == 1
        assert backup.SNAPSHOT_FAILED in seen[0]
        assert "No space left" in seen[0]


class TestRestoringFromTheSnapshot:
    """The whole file exists for this. A snapshot nobody has restored from is just a file.

    ``list_config`` carries both halves of what has to come back. ``config_json`` is the
    data a ``drop_column`` takes with it. ``name`` is the ``COLLATE NOCASE`` unique column
    that a batch rebuild can silently drop, because reflection does not report a collation.
    Without it, the rebuilt table looks fine, but two names differing only in case can both
    exist even though the keep rule expects one.
    """

    @staticmethod
    def _add(engine: Engine, name: str) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO list_config "
                    "(name, source, config_json, enabled, created_at) "
                    "VALUES (:name, 'arr_tag', :body, 1, 1750000000)"
                ),
                {"name": name, "body": _LIST_BODY},
            )

    @staticmethod
    def _damage(settings: Settings) -> None:
        """Break the database the way a migration that went wrong would.

        This uses Alembic's own batch mode, so the rebuild is the real one. It omits
        ``collation=`` on the ``alter_column`` call, the same two-line authoring slip a
        real migration could make, then drops a column the way no ``downgrade()`` can undo.

        The rebuild is triggered on ``enabled``, but any ``Boolean`` column on this table
        reproduces the hazard, because what loses the collation is the rebuild itself, not
        which column asked for one.
        """
        engine = create_engine(settings.sync_database_url)
        try:
            with engine.connect() as conn:
                ops = Operations(MigrationContext.configure(conn, opts={"as_batch": True}))
                with ops.batch_alter_table("list_config") as batch:
                    batch.alter_column("enabled", existing_type=Boolean(), existing_nullable=False)
                conn.exec_driver_sql("ALTER TABLE list_config DROP COLUMN config_json")
                conn.commit()
        finally:
            engine.dispose()

    def test_a_dropped_column_and_a_dropped_collation_both_come_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings, config = _install(tmp_path, monkeypatch)
        command.upgrade(config, "head")
        engine = create_engine(settings.sync_database_url)
        self._add(engine, "Keepers")
        engine.dispose()

        snapshot = backup.snapshot_before_migration(
            settings, schema_gate.stored_revision(settings.database_path)
        )
        self._damage(settings)

        # The damage is real, or the recovery below proves nothing.
        engine = create_engine(settings.sync_database_url)
        assert "config_json" not in [c["name"] for c in inspect(engine).get_columns("list_config")]
        with engine.begin() as conn:  # the collation is gone, so the collision is accepted
            conn.execute(
                text(
                    "INSERT INTO list_config (name, source, enabled, created_at) "
                    "VALUES ('keepers', 'arr_tag', 1, 1750000000)"
                )
            )
        engine.dispose()

        summary = restore.stage_upload(settings, snapshot)
        restore.arm(settings, summary.token)
        assert restore.apply_pending_restore(settings) is True

        engine = create_engine(settings.sync_database_url)
        try:
            assert "config_json" in [c["name"] for c in inspect(engine).get_columns("list_config")]
            with engine.connect() as conn:
                stored = conn.execute(
                    text("SELECT config_json FROM list_config WHERE name = 'Keepers'")
                ).scalar_one()
                assert json.loads(stored) == json.loads(_LIST_BODY)
                names = [
                    str(row.name) for row in conn.execute(text("SELECT name FROM list_config"))
                ]
            assert "keepers" not in names  # the row written after the snapshot is not here
            with pytest.raises(IntegrityError):
                self._add(engine, "keepers")  # and the collation refuses it again
        finally:
            engine.dispose()
