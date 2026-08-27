# SPDX-License-Identifier: AGPL-3.0-or-later
"""add whitelist.spare_expires_at

Adds a bounded length to a hand-spare. The operator can spare a condemned title for a set
time, after which the next scan re-judges it. If it is still expendable, it re-enters the
review queue with a fresh grace window. An expiry alone never deletes anything.

The length is stored as an absolute instant, ``spare_expires_at``, in epoch seconds like
every other timestamp here. NULL means "kept forever", the original behavior, so every
existing spare keeps its current meaning and a value that has not been set is never read
as an early reap. A reap override never expires and leaves this column NULL.

The new column is nullable with no server default. Existing rows keep NULL, meaning
forever. The write path stamps a value when the operator sets a timed spare. The app
reads NULL as forever everywhere. No tester database needs to be rebuilt.

Revision ID: 6f708192a3b4
Revises: 6f7081920314
Create Date: 2026-07-22 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6f708192a3b4"
down_revision: str | None = "6f7081920314"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table matches the baseline's SQLite style. Adding a nullable column is a
    # plain ADD COLUMN with no table rebuild, so existing data is untouched. Timestamps are
    # stored as epoch integers (see ``reaper.db.base.EpochDateTime``), so the column type is
    # sa.Integer().
    with op.batch_alter_table("whitelist", schema=None) as batch_op:
        batch_op.add_column(sa.Column("spare_expires_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("whitelist", schema=None) as batch_op:
        batch_op.drop_column("spare_expires_at")
