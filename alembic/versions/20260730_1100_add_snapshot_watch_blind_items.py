# SPDX-License-Identifier: AGPL-3.0-or-later
"""add snapshot.watch_blind_items

Settings -> Plex offers to discard Reaper's recorded watch evidence, and the one number that
tells an operator whether they need to is how many items the last scan actually held back for
that reason. This column is that count, written by the scan.

Counted at scan time rather than derived later on purpose. The alternative is to read each
stored explanation back and match the reason text, which is exactly rule 92's coupling: that
string is operator copy, it will be reworded, and the count would silently fall to zero when
it is. A typed integer written where the decision is made cannot drift from it.

Non-breaking by construction: a single nullable column. Every snapshot taken before this reads
NULL, which the API reports as "not recorded" rather than as zero -- a scan that never counted
must not be shown as a scan that counted none. Testers never rebuild their database.

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
