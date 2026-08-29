# SPDX-License-Identifier: AGPL-3.0-or-later
"""add watch_high_water

A Plex rating key is not stable. If a file is removed and comes back, Plex assigns it a
new key, but Tautulli still files every earlier play under the old one. Reaper reads its
Tautulli mirror by the item's current key, finds nothing, and reports ``Known(0)``
watchers and maximum dormancy. That reads as an affirmative "nobody ever watched this"
for a title somebody did watch, which adds deletion pressure.

The read path cannot tell that case apart from a genuinely unwatched item, since both
show "no rows for this key". This table is the evidence that can. It stores the most
watch activity ever measured for an item, keyed on the stable ``media_key`` rather than
the rating key that moved. All-time watch evidence only grows, so a fall to zero from a
positive mark is a change no real library makes. When the scan sees that fall, it reports
those facts as ``Unknown`` instead of zero, which blocks the gate and keeps the discount
for uncertain evidence.

This table has to outlive individual snapshots. Comparing only against the previous
snapshot would let the first blind scan record zero as a new baseline, after which a
drop from zero to zero is invisible and the check never fires again.

This is a new table, so nothing existing is altered. Every current database gains an
empty one, and the next scan fills it in. An item with no recorded mark yet simply never
triggers the check. No tester database needs to be rebuilt.

Revision ID: c4d5e6f70819
Revises: 8192a3b4c5d6
Create Date: 2026-07-30 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f70819"
down_revision: str | None = "8192a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Integer timestamps, like every other one in this schema. ``EpochDateTime`` (see
    # ``db.types.EpochDateTime``) stores epoch seconds, so a DateTime column here would
    # read as schema drift against the model and fail ``alembic check``.
    op.create_table(
        "watch_high_water",
        sa.Column("media_key", sa.String(length=100), nullable=False),
        sa.Column("watchers_all_time", sa.Integer(), nullable=False),
        sa.Column("last_played_at", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("media_key"),
    )


def downgrade() -> None:
    op.drop_table("watch_high_water")
