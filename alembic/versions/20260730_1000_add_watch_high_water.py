# SPDX-License-Identifier: AGPL-3.0-or-later
"""add watch_high_water

A Plex rating key is not stable. Remove a file, let it come back, and Plex issues a new
one -- while Tautulli keeps every earlier play filed under the OLD one. That is not a
guess: across Tautulli's whole source the only thing that rewrites a historical rating key
is ``datafactory.update_metadata_details``, reachable from the "Fix Metadata" button and
the API command of the same name, and no scheduled task calls it. Reaper reads its mirror
by the key the item carries now, finds nothing, and reports ``Known(0)`` watchers plus
maximum dormancy -- an affirmative "nobody ever watched this" about a title somebody
watched, which is pressure in the condemn direction.

The read path cannot tell that apart from a genuinely unwatched item, because both are
"no rows for this key". This table is the outside evidence that can: the most watch
evidence ever measured for an item, under the stable ``media_key`` rather than the rating
key that moved. All-time watch evidence only grows (the mirror never deletes, and a
Tautulli prune is caught by ``history_sync._check_regression``), so a fall to zero from a
positive mark is a transition no library can make, and the scan then reports those facts
``Unknown`` instead of zero -- which blocks the gate and takes the keep discount.

It has to outlive the snapshots for the check to keep working. Comparing against only the
previous snapshot would let the first blind scan write zero as the new baseline, and after
that 0 -> 0 is not a fall and nothing ever notices again.

Non-breaking by construction: a brand new table, nothing altered. Existing databases gain
an empty one, the next scan fills it, and an item with no mark yet simply never fires the
check -- so no in-flight database is judged differently on the strength of a row that is
not there. Testers never rebuild.

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
    # Integer timestamps, like every other one in this schema: ``UtcTimestamp`` stores
    # epoch seconds (``db.types.EpochDateTime``), so a DateTime column here would read as
    # schema drift against the model and fail `alembic check`.
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
