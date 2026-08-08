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

#: Columns whose ORM attribute has been removed but whose column has not been dropped yet:
#: rule 148's one-release bridge, NOT a permanent registry. Without this filter autogenerate
#: sees a column with no attribute and proposes to DROP it, so ``alembic check`` -- a CI gate
#: -- fails on every commit until someone either drops it or silences it here (#271).
#:
#: **Every entry here is a debt with a due date.** The M+1 sweep drops all six in one
#: revision under rule 148's three obligations, and this set empties with it. An entry that
#: outlives its sweep is how a dead column becomes permanent behind a growing exclusion list,
#: which is the failure rule 148 exists to prevent -- so add to this only alongside the
#: release that removes the attribute, and never to make a red ``alembic check`` go green.
#:
#: ``candidate.poster_url`` is nullable and needed no schema change to survive its attribute
#: leaving; it still needs this arm, because autogenerate does not care why a column has no
#: attribute. The other five are the ``NOT NULL`` ones that revision ``e6f7a8b9c0d1`` gave a
#: server default or made nullable first.
RETIRED_COLUMNS = {
    ("candidate", "poster_url"),
    ("list_config", "built_in"),
    ("pending_plex_login", "pin_code"),
    ("plex_server", "owner_plex_account_id"),
    ("profile", "active_policy_id"),
    ("profile", "enabled"),
}

#: A retired column that carried a FOREIGN KEY leaves the constraint behind too, and hiding
#: the column does not hide it: ``alembic check`` reports ``remove_fk`` on the next commit and
#: the CI gate stays red. Found by running the check rather than by reading the docs, which is
#: the only reason it is here -- nothing about ``include_name``'s column arm suggests a second
#: one is needed. Empties with ``RETIRED_COLUMNS`` at the M+1 sweep, where the constraint is
#: dropped ahead of its column (rule 148's ordering).
RETIRED_CONSTRAINTS = {
    ("profile", "fk_profile_active_policy_id_policy"),
}


def include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    """Hide the retired columns from autogenerate, and nothing else.

    This used to filter six cache TABLES as well, and it could never have been doing
    anything: all six are raw DDL on ``cache.db`` (``services/{imdb_dataset,history_sync,
    lists}.py``), none is on ``Base.metadata``, and Alembic is pointed at ``reaper.db``
    (``config.sync_database_url``). The proof it was inert is that ``history_sync_state``
    was missing from the set and never once mattered.
    """
    if type_ == "column":
        return (parent_names.get("table_name"), name) not in RETIRED_COLUMNS
    if type_ == "foreign_key_constraint":
        return (parent_names.get("table_name"), name) not in RETIRED_CONSTRAINTS
    return True


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


# No offline (``--sql``) branch. There was one, with no invoker in the tree, and it could
# not have worked: 8 of the 23 revisions call ``op.get_bind()`` -- rule 81's reflection
# guards, and the shape every heal migration takes -- so ``alembic upgrade head --sql``
# exits 1 at revision 3 of 23. Restoring the capability means giving those guards up, which
# is the wrong trade for a mode nothing runs.
run_migrations_online()
