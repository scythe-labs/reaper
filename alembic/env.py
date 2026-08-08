# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alembic environment.

``render_as_batch=True`` is required and effectively unretrofittable: SQLite
cannot ALTER or DROP a constraint in place, so Alembic has to rebuild the table.
Together with the naming convention in ``reaper.db.base`` this is what makes any
future schema change possible at all.

``keep_ddl_in_the_transaction`` is what makes a schema change that goes WRONG
survivable, and it is required for the same reason: the rebuild is what strands a
``_alembic_tmp_<table>`` behind a failed migration, and that wedges every later boot.
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

# Tables Alembic must leave alone.
#
# These are rebuildable CACHES, not schema: the IMDb dataset (millions of rows,
# reloaded nightly) and the local mirror of Tautulli's history (which grows without
# bound). They are created and swapped by their own services with raw DDL --
# imdb_rating in particular is loaded into a staging table and renamed, which no
# migration can express.
#
# Without this filter, autogenerate sees tables it did not create and helpfully
# proposes to DROP them.
CACHE_TABLES = {
    "imdb_rating",
    "imdb_rating_staging",
    "imdb_dataset_sync",
    "watch_event",
    "protection_list",
    "protection_list_item",
}


def include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    if type_ == "table":
        return name not in CACHE_TABLES
    if type_ == "index":
        return parent_names.get("table_name") not in CACHE_TABLES
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
        render_item=render_epoch_datetime,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def keep_ddl_in_the_transaction(engine: Engine) -> None:
    """Make DDL roll back with the rest of a failed migration (#564).

    pysqlite does not open a transaction for DDL. A ``CREATE TABLE`` with nothing
    started already is autocommitted on the spot, and only the first statement
    after it opens the implicit transaction. A batch recreate is ``CREATE TABLE
    _alembic_tmp_X`` followed by ``INSERT INTO ... SELECT``, so the CREATE commits,
    the INSERT opens the transaction, and a migration that raises any time later
    rolls back everything except the temp table. No data is lost -- the rollback
    restores the real table and its rows -- but every subsequent boot re-runs the
    same migration and dies on ``table _alembic_tmp_candidate already exists``.
    Migrations run at container start, so that is an install which never comes up
    again until someone opens the database by hand.

    It needs no crash and no power loss: any exception in the migration does it,
    including the ordinary authoring mistake of dropping a column before the index
    that sits on it, which is invisible against a fresh database.

    This is SQLAlchemy's documented recipe for the pysqlite dialect: take its
    implicit transaction handling away, and emit the BEGIN ourselves, so DDL joins
    the transaction like every other statement.

    Only the FIRST recreate in a migration was exposed, since after it a transaction
    is already open. But a migration does not have to LOOK like it recreates: an
    ``add_column`` carrying ``server_default=sa.false()`` rebuilds the table too,
    because the default is a ClauseElement rather than a string, and Alembic recreates
    for that. Two of the three recreates a fresh install performs are that shape.
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


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
