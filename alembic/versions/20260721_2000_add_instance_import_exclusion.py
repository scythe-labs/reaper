"""add instance.add_import_exclusion

The per-instance "Block re-download after delete" switch (Radarr movie deletes ask the *arr
to add an import/list exclusion so a list cannot re-add and re-download the title). Off by
default; the operator opts in per instance.

Split out of the frozen baseline into its own additive revision so an existing tester
database gets the column instead of silently missing it: the baseline already ran on those
databases, so a column added *inside* the baseline is never created, and every query that
loads an Instance row then fails. Non-breaking by construction -- a single NOT NULL column
with a server default of false, so existing rows backfill to "off" and no database is rebuilt.

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
    """Whether ``table`` already carries ``name`` in the live database."""
    return any(col["name"] == name for col in inspector.get_columns(table))


def upgrade() -> None:
    # For a brief window this column lived in the frozen baseline's CREATE TABLE in place
    # (later reverted), so a database created fresh during that ~30 minutes already carries
    # it. A plain add_column then raises "duplicate column name" and boot-loops the container
    # -- exactly the rebuild the frozen-baseline rule forbids. Reflect first and skip the add
    # when it is already present, the same reflection guard the sibling heal migration
    # (20260723_1000) uses (rule 81). A database that never had the column still gets it, and
    # one that already ran this migration never re-runs it, so editing the shipped file is safe.
    if _has_column(sa.inspect(op.get_bind()), "instance", "add_import_exclusion"):
        return
    # batch_alter_table for SQLite parity with the baseline. The server default lets SQLite
    # add a NOT NULL column to a populated table (it cannot otherwise) and backfills every
    # existing instance to "off"; new rows still take the model's default.
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
