# SPDX-License-Identifier: AGPL-3.0-or-later
"""add action_step.file_removed_at

The rolling 30-day budget counts only VERIFIED terminal delete steps. A movie whose file
Radarr did remove, but whose import exclusion never showed up inside the poll window, ends
FAILED instead. Its bytes came off disk, but nothing counts them, so the operator's monthly
budget can run over without the cap check noticing.

A step's state must keep telling the truth. A verification that failed is not VERIFIED. So
the file's removal is recorded separately from the verification's outcome. This column
holds that record. It is the moment Reaper confirmed the file was gone, no matter what the
rest of the step did next. ``_rolling_30d_deletions`` counts a step as VERIFIED or as
having file_removed_at set, so a removal counts once from either signal.

The new column is nullable. Existing rows keep NULL, which reads exactly as before since
only their VERIFIED state counted them, and every future removal stamps it. No tester
database needs to be rebuilt.

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
    # batch_alter_table matches the baseline's SQLite style. Adding a nullable column is a
    # plain ADD COLUMN with no table rebuild, so existing rows are untouched. The column
    # type is Integer, like every other timestamp in this schema, since ``EpochDateTime``
    # (see ``db.types.EpochDateTime``) stores epoch seconds. A DateTime column here would
    # read as schema drift.
    with op.batch_alter_table("action_step", schema=None) as batch_op:
        batch_op.add_column(sa.Column("file_removed_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("action_step", schema=None) as batch_op:
        batch_op.drop_column("file_removed_at")
