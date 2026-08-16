# SPDX-License-Identifier: AGPL-3.0-or-later
"""add snapshot.degraded_doc

The in-app help page that explains a degradation, by its docs registry id (#809). NULL for
every scan taken before this column existed, and NULL again for a degradation with no page,
which is most of them. The two are the same thing here: no page to offer, so the notice
renders exactly as it does today.

Non-breaking by construction: one nullable column, no backfill, no table rebuild under
SQLite's batch mode. Testers never rebuild their database.

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-08-16 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c0d1e2f3a4b5"
down_revision: str | None = "b9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table for SQLite parity with the baseline: a nullable column is a plain
    # ADD COLUMN, no table copy, so existing rows are untouched.
    with op.batch_alter_table("snapshot", schema=None) as batch_op:
        batch_op.add_column(sa.Column("degraded_doc", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("snapshot", schema=None) as batch_op:
        batch_op.drop_column("degraded_doc")
