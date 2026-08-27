"""add instance.plex_library_map

Adds the per-instance HD/4K split-library map. It is a JSON object mapping each of this
*arr instance's root folder paths to the Plex library it lands in, for example
{"/tv": "TV", "/tv-4k": "TV 4K"}. When one id names the same title in two Plex libraries,
the copy in the mapped library is the one Reaper binds to, which is how it tells apart a
show kept in both an HD and a 4K library. The map only narrows an already-ambiguous id.
It never turns an unambiguous match into an ambiguous one.

The new column is nullable, so SQLite adds it to a populated database with no rebuild.
Every existing instance reads back as NULL, meaning "no map", which keeps today's
behavior of abstaining on a duplicated title. The next scan needs no backfill. The
operator fills the map in from Settings.

Revision ID: 3c4d5e6f7081
Revises: 2b3c4d5e6f70
Create Date: 2026-07-21 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "3c4d5e6f7081"
down_revision: str | None = "2b3c4d5e6f70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A nullable column needs no server default. SQLite adds it to a populated table
    # directly, and existing rows read back as NULL, which the app treats as "no library map".
    with op.batch_alter_table("instance", schema=None) as batch_op:
        batch_op.add_column(sa.Column("plex_library_map", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("instance", schema=None) as batch_op:
        batch_op.drop_column("plex_library_map")
