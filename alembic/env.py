# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alembic environment.

SQLite cannot ALTER or DROP a constraint in place, so every migration rebuilds
the table instead. ``render_as_batch=True`` turns on that rebuild, and the naming
convention in ``reaper.db.base`` is required for it to work.

``keep_ddl_in_the_transaction`` makes a failed rebuild roll back cleanly instead
of leaving a stray ``_alembic_tmp_<table>`` behind, which would block every later
migration from running.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, event, pool
from sqlalchemy.engine import Connection, Engine

from reaper.config import get_settings
from reaper.db.base import Base
from reaper.db.types import render_epoch_datetime

# Importing the models package registers every table on Base.metadata.
import reaper.db.models  # noqa: F401  # isort: skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_settings = get_settings()
_settings.ensure_data_dir()
config.set_main_option("sqlalchemy.url", _settings.sync_database_url)

target_metadata = Base.metadata

#: Columns whose ORM attribute is gone but whose column is not dropped yet. Without this
#: filter, autogenerate sees a column with no matching attribute and proposes to drop it,
#: which fails the ``alembic check`` CI gate on every commit.
#:
#: Add an entry only in the same release that removes the attribute, and delete the entry
#: once the follow-up release drops the column.
#:
#: Empty is the normal state between releases. ``test_repo_hygiene.py`` checks that this
#: set and ``RETIRED_CONSTRAINTS`` below are both empty.
RETIRED_COLUMNS: set[tuple[str, str]] = set()

#: A retired column that carried a foreign key needs its constraint listed here too, or
#: ``alembic check`` reports ``remove_fk`` and the CI gate fails. Add and remove entries on
#: the same schedule as ``RETIRED_COLUMNS`` above.
RETIRED_CONSTRAINTS: set[tuple[str, str]] = set()


def include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    """Hide the retired columns and constraints from autogenerate."""
    if type_ == "column":
        return (parent_names.get("table_name"), name) not in RETIRED_COLUMNS
    if type_ == "foreign_key_constraint":
        return (parent_names.get("table_name"), name) not in RETIRED_CONSTRAINTS
    return True


def keep_ddl_in_the_transaction(engine: Engine) -> None:
    """Make DDL roll back with the rest of a failed migration.

    pysqlite does not open a transaction for DDL. A batch table rebuild runs
    ``CREATE TABLE _alembic_tmp_X`` followed by ``INSERT INTO ... SELECT``: with
    no transaction open yet, the CREATE commits on its own and only the INSERT
    runs inside one. If the migration raises after that, the rollback restores
    the real table but leaves the temp table behind, and every later boot fails
    with ``table _alembic_tmp_candidate already exists`` until someone opens the
    database by hand.

    This function takes pysqlite's implicit transaction handling away and issues
    ``BEGIN`` explicitly, so DDL joins the same transaction as the rest of the
    migration. Only the first table rebuild in a migration is at risk this way,
    because a transaction is already open by the time a second one runs. A
    migration rebuilds the table for any ``batch_alter_table`` call, and also
    for an ``add_column`` whose ``server_default`` is a Python value rather
    than a plain string.
    """

    @event.listens_for(engine, "connect")
    def _no_implicit_transaction(dbapi_connection: Any, connection_record: Any) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _explicit_begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN")


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    keep_ddl_in_the_transaction(connectable)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
            compare_server_default=True,
            render_item=render_epoch_datetime,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()


# No offline ("--sql") mode: 11 revisions call ``op.get_bind()`` to inspect the live
# database, which an offline run has no connection for. Supporting "--sql" would mean
# removing those checks. ``tests/test_migrations.py`` keeps that count current.
run_migrations_online()
