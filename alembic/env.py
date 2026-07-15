# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alembic environment.

``render_as_batch=True`` is required and effectively unretrofittable: SQLite
cannot ALTER or DROP a constraint in place, so Alembic has to rebuild the table.
Together with the naming convention in ``reaper.db.base`` this is what makes any
future schema change possible at all.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

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


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
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
