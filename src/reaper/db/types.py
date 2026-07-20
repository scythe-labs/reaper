# SPDX-License-Identifier: AGPL-3.0-or-later
"""Custom column types.

``EpochDateTime`` stores every timestamp as an **INTEGER unix epoch (UTC seconds)**
and presents it to Python as a timezone-aware ``datetime``.

Storing epoch rather than a DATETIME column is not a micro-optimization; it
removes a bug class rather than guarding against it. SQLite has no native date
type and stores no timezone, so ``DateTime(timezone=True)`` is silently a no-op
there: an aware datetime goes in and a *naive* one comes back. Comparing that
against ``datetime.now(UTC)`` raises TypeError at best, and at worst -- if both
sides happen to be naive -- compares a UTC instant against a local one and is
quietly wrong by the size of the UTC offset.

For Reaper that is not cosmetic. Every deletion decision rests on "when was this
last watched", so a timezone slip silently condemns media that was watched
recently, or spares media that was not.

An integer cannot carry that ambiguity. There is no tzinfo to lose, no dialect
that can drop it, and no guard that has to stay correct. The instant is the
value.

It also matches our inputs: Tautulli's ``date`` / ``started`` / ``stopped`` /
``last_played`` / ``added_at`` and Plex's ``addedAt`` / ``lastViewedAt`` are all
unix ints already, so this is the format the data arrives in.

Trade-off, stated plainly: raw SQL is less readable. Use SQLite's own conversion
when poking at the database by hand::

    SELECT datetime(first_flagged_at, 'unixepoch') FROM first_flagged;

Precision is whole seconds. Nothing in Reaper is sub-second -- watch history is
recorded in seconds -- and rows that need a stable tiebreak order by their
autoincrement id.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Integer
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class EpochDateTime(TypeDecorator[datetime]):
    """A UTC instant, stored as an integer unix timestamp."""

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> int | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # We cannot know which instant a naive datetime means, and guessing
            # is how "last played 612 days ago" ends up wrong by hours.
            raise ValueError(
                "Refusing to store a naive datetime. Reaper's deletion decisions "
                "depend on watch-history recency, so an ambiguous instant is a "
                "correctness bug. Use reaper.clock.utcnow()."
            )
        return int(value.timestamp())

    def process_result_value(self, value: int | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return datetime.fromtimestamp(value, tz=UTC)

    def process_literal_param(self, value: datetime | None, dialect: Dialect) -> str:
        return "NULL" if value is None else str(int(value.timestamp()))

    @property
    def python_type(self) -> type[datetime]:
        return datetime


def render_epoch_datetime(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Alembic hook: render ``EpochDateTime`` as a plain ``sa.Integer()``.

    The emitted DDL is identical -- ``EpochDateTime`` only changes Python-side
    conversion -- so migrations need not import Reaper's own modules.
    (Autogenerate otherwise writes ``reaper.db.types.EpochDateTime()`` *without*
    emitting the import, producing a migration that fails at runtime with a
    NameError: it reports success and creates no tables.)
    """
    if type_ == "type" and isinstance(obj, EpochDateTime):
        autogen_context.imports.add("import sqlalchemy as sa")
        return "sa.Integer()"
    return False
