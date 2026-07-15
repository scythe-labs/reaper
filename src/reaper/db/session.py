# SPDX-License-Identifier: AGPL-3.0-or-later
"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry

from reaper.config import Settings


def _configure_sqlite(dbapi_conn: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    """SQLite pragmas.

    WAL matters here: a library scan holds a long read while the UI is still
    serving requests, and the default rollback journal would block writers for
    the duration. ``foreign_keys`` is OFF by default in SQLite -- without it our
    cascade rules are decorative.
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def create_engine(settings: Settings) -> AsyncEngine:
    """Reaper's own state. Small, precious, migrated."""
    settings.ensure_data_dir()
    engine = create_async_engine(settings.database_url, echo=False)
    event.listen(engine.sync_engine, "connect", _configure_sqlite)
    return engine


def create_cache_engine(settings: Settings) -> AsyncEngine:
    """The local mirror of other people's data.

    A SEPARATE FILE, deliberately: Tautulli's history and the IMDb dataset are large
    and take minutes to rebuild, while reaper.db is small and gets migrated, reset and
    restored. Keeping them in one file means a schema reset destroys hours of sync
    along with it -- which is exactly what happened during development, twice.

    Nothing in here is a source of truth. It can be deleted at any time and rebuilt.
    """
    settings.ensure_data_dir()
    engine = create_async_engine(settings.cache_database_url, echo=False)
    event.listen(engine.sync_engine, "connect", _configure_sqlite)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
