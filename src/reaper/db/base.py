# SPDX-License-Identifier: AGPL-3.0-or-later
"""Declarative base and metadata.

The naming convention below is load-bearing and cannot be changed later.

SQLite cannot ALTER a constraint. Alembic works around this by rebuilding the table
(``render_as_batch=True``), but dropping a constraint during that rebuild requires
naming it, and SQLite auto-generates anonymous names. Without a convention fixed
before the first migration, later schema changes fail with ``ValueError: Constraint
must have a name``, and the only fix is to rewrite the entire migration history.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, mapped_column

from reaper.db.types import EpochDateTime

# Every timestamp in Reaper is timezone-aware UTC.
#
# Stored as an integer unix epoch, not DateTime(timezone=True), which is a silent
# no-op on SQLite: it stores no timezone and hands back naive datetimes on read. An
# integer cannot carry that ambiguity, since the instant is the value, and it is the
# format Tautulli and Plex already give us. See reaper.db.types.
UtcTimestamp = Annotated[datetime, mapped_column(EpochDateTime())]

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
