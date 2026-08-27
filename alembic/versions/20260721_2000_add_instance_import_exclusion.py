"""add instance.add_import_exclusion

Adds the per-instance "Block re-download after delete" switch. When it is on, a Radarr
movie delete also asks Radarr to add an import/list exclusion, so a list cannot re-add
and re-download the title. It defaults to off, and the operator turns it on per instance.

This revision is separate from the frozen baseline because an existing tester database
already ran the baseline: a column added inside it would never reach that database, and
every query loading an Instance row would fail. The new column is NOT NULL with a server
default of false, so existing rows backfill to "off" and no database needs a rebuild.

Revision ID: 2b3c4d5e6f70
Revises: 1f2a3b4c5d6e
Create Date: 2026-07-21 20:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "2b3c4d5e6f70"
down_revision: str | None = "1f2a3b4c5d6e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(inspector: sa.Inspector, table: str, name: str) -> bool:
    """Check whether ``table`` already has a column named ``name``."""
    return any(col["name"] == name for col in inspector.get_columns(table))


def upgrade() -> None:
    # A database that already has this column fails a plain add_column with "duplicate
    # column name", and the container does not start. Check first, and skip the add when
    # the column is already present. The heal migration in
    # 20260723_1000_heal_candidate_size_nullable.py uses the same guard.
    if _has_column(sa.inspect(op.get_bind()), "instance", "add_import_exclusion"):
        return
    # batch_alter_table matches the baseline's SQLite style. The server default lets SQLite
    # add a NOT NULL column to a populated table, and it backfills every existing instance
    # to "off". New rows still take the model's own default.
    with op.batch_alter_table("instance", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "add_import_exclusion",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("instance", schema=None) as batch_op:
        batch_op.drop_column("add_import_exclusion")
