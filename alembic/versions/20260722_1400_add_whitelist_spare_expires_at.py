# SPDX-License-Identifier: AGPL-3.0-or-later
"""add whitelist.spare_expires_at

A hand-spare was permanent: once spared, an item was kept forever. This adds an optional
length -- the operator can spare a condemned title for a bounded time, after which the next
scan re-judges it and, if still expendable, it re-enters the review queue on a fresh grace
window. Nothing is ever deleted by an expiry itself.

The length is stored as an absolute instant, ``spare_expires_at`` (epoch seconds, like every
other timestamp here). NULL means "kept forever" -- the original behavior -- so every existing
spare keeps its exact meaning and a not-yet-set value is never read as an early reap. A reap
override never expires and leaves this NULL.

Non-breaking by construction: a single nullable column, no server default. Existing rows keep
NULL (forever), the write path stamps it when a timed spare is set, and the app reads NULL as
"forever" everywhere. Testers never rebuild their database.

Revision ID: 6f708192a3b4
Revises: 5e6f70819203
Create Date: 2026-07-22 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6f708192a3b4"
down_revision: str | None = "5e6f70819203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table for SQLite parity with the baseline: adding a nullable column is a
    # plain ADD COLUMN, no table copy, so existing data is untouched. Timestamps are stored
    # as epoch integers (reaper.db.base.EpochDateTime), so the column is sa.Integer().
    with op.batch_alter_table("whitelist", schema=None) as batch_op:
        batch_op.add_column(sa.Column("spare_expires_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("whitelist", schema=None) as batch_op:
        batch_op.drop_column("spare_expires_at")
