# SPDX-License-Identifier: AGPL-3.0-or-later
"""add action_step.file_removed_at

The rolling 30-day budget counted VERIFIED terminal delete steps only. A movie whose file
Radarr really did remove, but whose import exclusion never showed up inside the poll window,
ends FAILED -- so its bytes were reclaimed off disk and charged against nothing. Repeat that
on an intermittently slow Radarr and the operator's monthly budget is quietly overspent by
the sum of every such item, with the cap check reporting a number it knows to be short.

The step's *state* must keep telling the truth (a verification that failed is not VERIFIED),
so the file's removal is recorded separately from the verification's outcome. This column is
that record: the moment Reaper confirmed the file was gone, whatever the rest of the step
then did. ``_rolling_30d_deletions`` counts VERIFIED **or** file_removed_at IS NOT NULL, so a
removal counts once, from either signal.

Non-breaking by construction: a single nullable column. Existing rows keep NULL, which reads
exactly as before -- only their VERIFIED state counts them -- and every future removal stamps
it. Testers never rebuild their database.

Revision ID: 8192a3b4c5d6
Revises: 708192a3b4c5
Create Date: 2026-07-24 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8192a3b4c5d6"
down_revision: str | None = "708192a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table for SQLite parity with the baseline: adding a nullable column is a
    # plain ADD COLUMN, no table copy, so existing rows are untouched. Integer, like every
    # other timestamp in this schema: ``UtcTimestamp`` stores epoch seconds (see
    # ``db.types.EpochDateTime``), so a DateTime column here would read as schema drift.
    with op.batch_alter_table("action_step", schema=None) as batch_op:
        batch_op.add_column(sa.Column("file_removed_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("action_step", schema=None) as batch_op:
        batch_op.drop_column("file_removed_at")
