"""add candidate.tvdb_id

Scales rebuilds "not in the last scan" by matching Seerr requests back to the last scan's
candidates. That match used only tmdb and imdb ids, so a TV show Sonarr knows only by its
TVDb id, with no tmdb id on the candidate and no imdb id on the Seerr request, could not be
matched to its own candidate. It then read as set aside even though the scan covered it.

Storing the show's TVDb id on each season candidate lets the match use it too. The value
is already available at scan time; only the storage was missing.

The new column is nullable. Existing rows keep NULL, since they were never given a tvdb
id, so the match behaves exactly as before for them. The next scan backfills every
candidate it writes, so no tester database needs to be rebuilt. A movie candidate stays
NULL, because Radarr is tmdb-native and a movie has no TVDb id.

Revision ID: 5e6f70819203
Revises: 4d5e6f708192
Create Date: 2026-07-22 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5e6f70819203"
down_revision: str | None = "4d5e6f708192"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table matches the baseline's SQLite style. Adding a nullable column is a
    # plain ADD COLUMN with no table rebuild, so existing data is untouched.
    with op.batch_alter_table("candidate", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tvdb_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("candidate", schema=None) as batch_op:
        batch_op.drop_column("tvdb_id")
