# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration tests.

These exist because the two settings they exercise -- the metadata naming
convention and Alembic's ``render_as_batch`` -- cannot be added later. If a
future schema change is the first thing to discover they were misconfigured, the
fix is rewriting the entire migration history.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alembic import op  # noqa: F401  # imported so op.f is resolvable in migrations
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, MetaData, String, Table, UniqueConstraint, create_engine
from sqlalchemy.engine import Engine

from reaper.config import Settings
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


class _ConfigureCalled(Exception):  # noqa: N818 -- a control-flow signal, not an error
    """Carries env.py's real ``context.configure()`` keyword arguments back to the test."""

    def __init__(self, kwargs: dict[str, Any]) -> None:
        super().__init__("env.py called context.configure()")
        self.kwargs = kwargs


def _env_py_configure_kwargs(
    *, as_sql: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Run the real ``alembic/env.py`` and return what it passed to ``context.configure()``.

    env.py cannot be imported -- Alembic execs it by path -- so this drives it the
    way Alembic itself does and intercepts the one call that matters. ``as_sql``
    picks the branch: True runs ``run_migrations_offline``, False the online one.

    Nothing is migrated: ``configure`` raises as soon as it has the keyword
    arguments, which also means a future env.py that never calls it fails here
    rather than passing on an empty result.
    """
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    monkeypatch.setattr("reaper.config.get_settings", lambda: settings)

    # env.py replays alembic.ini's logging config, and fileConfig() disables every
    # logger it does not name -- for the rest of the session, not just this test.
    monkeypatch.setattr("logging.config.fileConfig", lambda *a, **kw: None)

    def _capture(**kwargs: Any) -> None:
        raise _ConfigureCalled(kwargs)

    # env.py holds the alembic.context *module*, so it resolves configure at call time.
    monkeypatch.setattr("alembic.context.configure", _capture)

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    with (
        EnvironmentContext(config, script, as_sql=as_sql),
        pytest.raises(_ConfigureCalled) as caught,
    ):
        script.run_env()
    return caught.value.kwargs


@pytest.mark.parametrize("as_sql", [True, False], ids=["offline", "online"])
def test_env_py_configures_batch_mode(
    as_sql: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What alembic/env.py actually passes -- not what a test hand-configures.

    ``test_batch_mode_can_drop_a_named_constraint`` proves batch mode plus the
    naming convention can drop a constraint, but it configures batch mode itself.
    This one reads the shipped env.py, so flipping ``render_as_batch`` to False
    fails here instead of years later, in the first migration that needs it.

    Both call sites are covered: env.py configures once per branch, and only the
    branch under test runs.
    """
    kwargs = _env_py_configure_kwargs(as_sql=as_sql, tmp_path=tmp_path, monkeypatch=monkeypatch)

    assert kwargs.get("render_as_batch") is True, (
        "alembic/env.py must pass render_as_batch=True. Without it SQLite cannot "
        "drop a constraint, and the fix is rewriting the migration history."
    )
    # The other half of the pair: the convention only helps if the metadata carrying
    # it is the metadata env.py hands to Alembic.
    assert kwargs["target_metadata"].naming_convention == NAMING_CONVENTION
