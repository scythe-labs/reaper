# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration tests.

These exist because the two settings they exercise -- the metadata naming
convention and Alembic's ``render_as_batch`` -- cannot be added later. If a
future schema change is the first thing to discover they were misconfigured, the
fix is rewriting the entire migration history.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import op  # noqa: F401  # imported so op.f is resolvable in migrations
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, MetaData, String, Table, UniqueConstraint, create_engine
from sqlalchemy.engine import Engine

from reaper.db.base import NAMING_CONVENTION

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Engine:
    return create_engine(f"sqlite:///{tmp_path / 'test.db'}")


def test_exactly_one_migration_head() -> None:
    """A branched history is a merge conflict waiting to corrupt someone's database."""
    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, f"Expected a single head, found {heads}"


def test_batch_mode_can_drop_a_named_constraint(sqlite_engine: Engine) -> None:
    """The point of the whole arrangement.

    SQLite cannot ``ALTER TABLE ... DROP CONSTRAINT``. Alembic's batch mode
    rebuilds the table instead -- but it can only drop a constraint it can *name*,
    and SQLite auto-generates anonymous names. The naming convention is what makes
    the name predictable, and this test proves the two work together.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    md = MetaData(naming_convention=NAMING_CONVENTION)
    table = Table(
        "thing",
        md,
        Column("id", Integer, primary_key=True),
        Column("code", String(20)),
        UniqueConstraint("code"),  # anonymous -- the convention names it
    )
    md.create_all(sqlite_engine)

    with sqlite_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"as_batch": True})
        ops = Operations(ctx)

        # Without the convention this raises ValueError: Constraint must have a name
        with ops.batch_alter_table("thing", copy_from=table) as batch:
            batch.drop_constraint("uq_thing_code", type_="unique")

        conn.commit()

        # The constraint is gone: the duplicate below would previously have failed.
        conn.exec_driver_sql("INSERT INTO thing (id, code) VALUES (1, 'dup')")
        conn.exec_driver_sql("INSERT INTO thing (id, code) VALUES (2, 'dup')")
        conn.commit()

        count = conn.exec_driver_sql("SELECT COUNT(*) FROM thing").scalar_one()
        assert count == 2
