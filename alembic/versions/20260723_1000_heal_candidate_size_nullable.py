# SPDX-License-Identifier: AGPL-3.0-or-later
"""heal candidate.size_bytes nullability (and reap_run default) on pre-existing databases

A fresh install already has the corrected shape: ``candidate.size_bytes`` is nullable, and
``reap_run.held_back_unknown_size`` has no server default, matching the models. A database
created before that correction still has the old shape, with no ALTER ever applied to fix
it. This revision is that ALTER, applied only where it is still needed.

The size_bytes half is a correctness fix. The scan records an item whose size cannot be
determined as ``size_bytes = NULL``, and a NOT NULL column rejects that write with an
IntegrityError mid-scan. The held_back_unknown_size half only brings the schema in line
with the model. The column stays NOT NULL, and the app always supplies 0.

This revision is idempotent and additive. Each change is guarded by reflecting the live
column first, so a database that already has the corrected shape is left completely
untouched, with no table rebuild. An older database is reshaped in place, and its rows
are preserved. No tester database needs to be rebuilt.

Revision ID: 708192a3b4c5
Revises: 6f708192a3b4
Create Date: 2026-07-23 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "708192a3b4c5"
down_revision: str | None = "6f708192a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# This migration snapshots the database first (see ``reaper.db.schema_gate.SNAPSHOT_ATTR``).
# Its ``alter_column`` calls rebuild the table from SQLite's reflection, which can silently
# drop a constraint that reflection does not report.
needs_snapshot = True


def _column(inspector: sa.Inspector, table: str, name: str) -> dict[str, object] | None:
    """Return the reflected column dict, or None if the table lacks that column."""
    for col in inspector.get_columns(table):
        if col["name"] == name:
            return col
    return None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # Change candidate.size_bytes from NOT NULL to nullable, but only rebuild the table if
    # it is still NOT NULL. A database that already has the corrected shape is not copied
    # for nothing.
    size_bytes = _column(inspector, "candidate", "size_bytes")
    if size_bytes is not None and not size_bytes["nullable"]:
        with op.batch_alter_table("candidate", schema=None, recreate="always") as batch_op:
            batch_op.alter_column("size_bytes", existing_type=sa.Integer(), nullable=True)

    # Drop the stray DEFAULT 0 on reap_run.held_back_unknown_size. The column stays NOT NULL.
    # Only rebuild if a server default is actually present. A database that never got the
    # column at all is left alone.
    held_back = _column(inspector, "reap_run", "held_back_unknown_size")
    if held_back is not None and held_back["default"] is not None:
        with op.batch_alter_table("reap_run", schema=None, recreate="always") as batch_op:
            batch_op.alter_column(
                "held_back_unknown_size",
                existing_type=sa.Integer(),
                existing_nullable=False,
                server_default=None,
            )


def downgrade() -> None:
    # This migration is not reversed on purpose. The corrected shape is the right one.
    # Re-adding NOT NULL to candidate.size_bytes would fail on any legitimately NULL,
    # unknown size, and reintroduce the IntegrityError this migration closes. There is
    # nothing safe to undo.
    pass
