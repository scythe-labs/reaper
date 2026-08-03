"""add candidate.library_title

The first additive revision after the frozen baseline (see the baseline's note). Adds
the Plex library (section) title captured onto each candidate at scan time, for the
review queue's library chip and the library filter.

Non-breaking by construction: a single nullable column. Existing rows keep NULL (the UI
shows no library chip for them), and the next scan backfills every candidate it writes,
so testers never rebuild their database.

Revision ID: 1f2a3b4c5d6e
Revises: 22777b2b5015
Create Date: 2026-07-21 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '1f2a3b4c5d6e'
down_revision: str | None = '22777b2b5015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table for SQLite parity with the baseline: adding a nullable column is a
    # plain ADD COLUMN, no table copy, so existing data is untouched.
    with op.batch_alter_table('candidate', schema=None) as batch_op:
        batch_op.add_column(sa.Column('library_title', sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('candidate', schema=None) as batch_op:
        batch_op.drop_column('library_title')
