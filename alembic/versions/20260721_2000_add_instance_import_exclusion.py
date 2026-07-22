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


def upgrade() -> None:
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
