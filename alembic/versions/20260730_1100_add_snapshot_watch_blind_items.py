# SPDX-License-Identifier: AGPL-3.0-or-later
"""add snapshot.watch_blind_items

Settings > Plex offers to discard Reaper's recorded watch evidence. The number an operator
needs before doing that is how many items the last scan found had plays it could no longer
read. This column is that count, written by the scan.

It counts what was measured, not what was decided. Such an item is normally held back, but
by gates the operator can turn off, and this count does not consult the final verdict. So no
copy may describe this figure as items held back or kept.

The scan counts this directly rather than deriving it later from stored explanations. Those
explanation strings are operator-facing copy that gets reworded, and matching against their
text would let the count silently fall to zero when the wording changes. A typed integer
written at the moment of the decision cannot drift from it.

The new column is nullable. Every snapshot taken before this migration reads NULL, which the
API reports as "not recorded" rather than as zero. A scan that never counted this must not
look like a scan that counted zero. No tester database needs to be rebuilt.

Revision ID: d5e6f708192a
Revises: c4d5e6f70819
Create Date: 2026-07-30 11:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f708192a"
down_revision: str | None = "c4d5e6f70819"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("snapshot", schema=None) as batch_op:
        batch_op.add_column(sa.Column("watch_blind_items", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("snapshot", schema=None) as batch_op:
        batch_op.drop_column("watch_blind_items")
