# SPDX-License-Identifier: AGPL-3.0-or-later
"""add candidate.collections_json, snapshot.collection_sizes_json

Phase 2 of the collections plan. Two nullable columns, mirroring how genres
already ride along. ``candidate.collections_json`` holds the item's own
collection names (sorted smallest-first, ties alphabetical).
``snapshot.collection_sizes_json`` holds every collection this scan saw, name
to Plex's own member count, for the picker and the header.

Both are NULL on every row scanned before this column existed, and NULL again
whenever a scan's Plex collection read failed. The two are indistinguishable
on purpose, because collections are navigation, never protection
(docs/COLLECTIONS_PLAN.md's fence): the worst a bad read costs is a missing
chip, never a degraded snapshot.

Non-breaking by construction: two nullable columns, no backfill, no table
rebuild under SQLite's batch mode. Testers never rebuild their database.

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-08-15 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9c0d1e2f3a4"
down_revision: str | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table for SQLite parity with the baseline: a nullable column is a plain
    # ADD COLUMN, no table copy, so existing rows are untouched.
    with op.batch_alter_table("candidate", schema=None) as batch_op:
        batch_op.add_column(sa.Column("collections_json", sa.Text(), nullable=True))
    with op.batch_alter_table("snapshot", schema=None) as batch_op:
        batch_op.add_column(sa.Column("collection_sizes_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("snapshot", schema=None) as batch_op:
        batch_op.drop_column("collection_sizes_json")
    with op.batch_alter_table("candidate", schema=None) as batch_op:
        batch_op.drop_column("collections_json")
