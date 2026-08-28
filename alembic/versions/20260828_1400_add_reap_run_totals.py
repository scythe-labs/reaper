# SPDX-License-Identifier: AGPL-3.0-or-later
"""add reap_run's terminal totals, backfilled from the journal

A run's totals (items deleted, bytes reclaimed, how many were unmeasured, how many were
skipped) exist today only on the in-memory ``RunReport`` the executor returns: gone the
moment the process restarts or a later run replaces it on ``app.state``. Four nullable
columns on ``reap_run`` hold them instead, written once by
``Executor._write_run_totals`` when a real run reaches COMPLETED or ABORTED. NULL means
"has not reached a terminal state yet" (PLANNED, EXECUTING, or a dry run, which never
writes them), read as unknown, never as zero.

"Deleted" counts a step from its file's actual removal, VERIFIED or ``file_removed_at``
set, never from ``state`` alone: a movie Radarr really deleted whose import exclusion
never landed ends FAILED and stays FAILED, but its bytes are off disk, so it counts. This
is the same discipline ``Executor._rolling_30d_deletions`` already uses for the rolling
budget. ``services.run_totals.totals_query``/``aggregate_rows`` are the one query and the
one aggregation both the executor's terminal write and this backfill run, so the two can
never drift apart.

Backfilled here for every run already COMPLETED or ABORTED, so an existing install reads
identically to one that scanned after this landed. A run still PLANNED or EXECUTING (a
crash mid-flight, or one nobody ever ran) is left NULL, exactly as a fresh terminal write
would leave it.

Revision ID: ade1f657fcfe
Revises: e2f3a4b5c6d7
Create Date: 2026-08-28 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ade1f657fcfe"
down_revision: str | None = "e2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reap_run", schema=None) as batch_op:
        batch_op.add_column(sa.Column("deleted_items", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("deleted_bytes", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("deleted_unmeasured", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("skipped", sa.Integer(), nullable=True))

    # Imported inside the function, so a module that moves or fails to import cannot stop
    # an upgrade whose other revisions are unrelated to the reap loop.
    from reaper.services.run_totals import aggregate_rows, totals_query

    conn = op.get_bind()
    # The state column stores the enum MEMBER NAME ('COMPLETED'), not the lowercase wire
    # value: raw SQL here must match that spelling or the backfill silently touches
    # nothing. tests/test_migrations.py seeds rows in the stored spelling to hold this.
    run_ids = [
        row[0]
        for row in conn.execute(
            sa.text("SELECT id FROM reap_run WHERE state IN ('COMPLETED', 'ABORTED')")
        ).fetchall()
    ]
    for run_id in run_ids:
        totals = aggregate_rows(conn.execute(totals_query(run_id)).all())
        conn.execute(
            sa.text(
                "UPDATE reap_run SET deleted_items = :items, deleted_bytes = :bytes, "
                "deleted_unmeasured = :unmeasured, skipped = :skipped WHERE id = :id"
            ),
            {
                "items": totals.deleted_items,
                "bytes": totals.deleted_bytes,
                "unmeasured": totals.deleted_unmeasured,
                "skipped": totals.skipped,
                "id": run_id,
            },
        )


def downgrade() -> None:
    with op.batch_alter_table("reap_run", schema=None) as batch_op:
        batch_op.drop_column("skipped")
        batch_op.drop_column("deleted_unmeasured")
        batch_op.drop_column("deleted_bytes")
        batch_op.drop_column("deleted_items")
