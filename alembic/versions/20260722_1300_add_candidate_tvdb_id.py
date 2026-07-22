"""add candidate.tvdb_id

Scales rebuilds "not in the last scan" by re-joining Seerr requests to the last scan's
candidates. That join only ever used tmdb and imdb ids, so a TV show Sonarr knows only by
its TVDb id (no tmdb on the candidate, and a Seerr request that carries no imdb) could not
be lined up with its own candidate and read as "set aside" despite having been scanned.

Persisting the show's TVDb id onto each season candidate lets the join bind on it too
(rule 29: pass every id an item carries). The value is already in hand at scan time; only
storage was missing.

Non-breaking by construction: a single nullable column. Existing rows keep NULL (they were
never given a tvdb id, so the join behaves exactly as before for them), and the next scan
backfills every candidate it writes, so testers never rebuild their database. A movie
candidate stays NULL: Radarr is tmdb-native and a movie has no TVDb id.

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
    # batch_alter_table for SQLite parity with the baseline: adding a nullable column is a
    # plain ADD COLUMN, no table copy, so existing data is untouched.
    with op.batch_alter_table("candidate", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tvdb_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("candidate", schema=None) as batch_op:
        batch_op.drop_column("tvdb_id")
