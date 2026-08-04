# SPDX-License-Identifier: AGPL-3.0-or-later
"""a list name is unique without regard to case

``list_config.name`` was unique byte for byte while every reader case-folds it (rule 88),
so "Never Reap" and "never reap" were two rows to SQLite and one list to the policy: the
second never got a keep rule of its own, it shared the first's, and deleting either one
took that rule away and stopped the other protecting (#508).

The column gains ``COLLATE NOCASE``, which is what makes its UNIQUE constraint compare the
way the code does. Additive in effect and safe on a populated database: SQLite cannot alter
a collation in place, so this is a batch rebuild, and the rows carry over untouched.

Guarded twice. The stored DDL is read first, so a database already carrying the collation
(every fresh install) is left alone. Then any name that already collides is disambiguated
before the constraint could refuse it -- a suffix on the LATER row, keeping the oldest
spelling, because the older row is the one whose name the keep rule is most likely to spell.
A renamed list keeps every title it holds and shows on Settings -> Lists as not used by the
policy yet, which is what it already was: it was protecting through the other row's rule.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-04 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _disambiguate(bind: sa.Connection) -> None:
    """Give every case-colliding name its own spelling, oldest row first.

    Reads the whole table rather than a GROUP BY: it holds a handful of rows, and the
    suffixed name has to be checked against the names already taken, including ones this
    loop just handed out.
    """
    rows = bind.execute(sa.text("SELECT id, name FROM list_config ORDER BY id")).all()
    taken = set()
    for row in rows:
        name = str(row.name)
        key = name.strip().casefold()
        if key not in taken:
            taken.add(key)
            continue
        # Truncated to the column's 100 characters INCLUDING the suffix, or the rebuild
        # below refuses a name this migration itself made too long.
        suffix = 2
        while f"{name[:94]} ({suffix})".strip().casefold() in taken:
            suffix += 1
        renamed = f"{name[:94]} ({suffix})"
        taken.add(renamed.strip().casefold())
        bind.execute(
            sa.text("UPDATE list_config SET name = :new WHERE id = :id"),
            {"new": renamed, "id": row.id},
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "list_config" not in inspector.get_table_names():
        return
    # The declared DDL, because a collation is not something SQLite reflection reports:
    # `get_columns` gives the type and nothing about how it compares.
    declared = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'list_config'")
    ).scalar_one_or_none()
    if declared is None or "NOCASE" in str(declared).upper():
        return

    _disambiguate(bind)
    with op.batch_alter_table("list_config", schema=None, recreate="always") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=100),
            type_=sa.String(length=100, collation="NOCASE"),
            existing_nullable=False,
        )


def downgrade() -> None:
    # Intentionally not reversed. Putting the case-sensitive comparison back would re-open
    # the way for two lists to answer to one keep rule.
    pass
