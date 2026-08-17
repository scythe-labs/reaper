# SPDX-License-Identifier: AGPL-3.0-or-later
"""add library_seen

The ledger behind #553, "a title that came back". One row per external id, holding every
Plex rating key Reaper has ever seen bound to it, when it was last seen, and whether it has
since come back. A file that leaves the library and returns gets a new Plex key while the
old one stops existing, and without this table a fresh key is indistinguishable from the
only key an item ever had.

A new table, so nothing existing is touched and no tester rebuilds. It starts empty on every
install, including one upgrading, which is the feature's stated property: with nothing to
compare against, no title can look returned until Reaper has watched the library long enough
to have seen the thing that later leaves.

``rating_keys_json`` holds a JSON array rather than a second table. The set is one to a
handful of ints per row, nothing joins on a member, and the only two operations are "is this
key in the set" and "add one", both of which the reader does in Python over a row it already
has.

No ``needs_snapshot``: ``create_table`` loses nothing, and ``downgrade()`` drops a table this
revision created.

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-08-14 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8b9c0d1e2f3"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "library_seen",
        sa.Column("id_key", sa.String(length=100), primary_key=True),
        sa.Column("rating_keys_json", sa.Text(), nullable=False, server_default="[]"),
        # Integer timestamps, like every other one in this schema: ``UtcTimestamp`` stores
        # epoch seconds (``db.types.EpochDateTime``, whose ``impl`` is ``Integer``). A
        # ``DateTime`` column here reads as schema drift against the model and fails
        # ``alembic check``, which CI runs as a required step.
        sa.Column("first_seen_at", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.Integer(), nullable=False),
        sa.Column("returned_at", sa.Integer(), nullable=True),
        sa.Column("returned_by_reaper", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("library_seen")
