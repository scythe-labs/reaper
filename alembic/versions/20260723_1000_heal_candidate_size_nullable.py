# SPDX-License-Identifier: AGPL-3.0-or-later
"""heal candidate.size_bytes nullability (and reap_run default) on pre-existing databases

Two columns were corrected by editing the baseline's CREATE TABLE in place, before the
baseline was frozen: ``candidate.size_bytes`` went from NOT NULL to nullable, and
``reap_run.held_back_unknown_size`` lost its ``DEFAULT 0`` server default, both to match the
models. A CREATE TABLE only runs against an empty database, so a fresh install gets the
corrected shape while any database created from the earlier baseline keeps the old one, with
no ALTER to heal it. This is that missing ALTER.

The size_bytes half is correctness, not cosmetics: the scan now records an item whose size
cannot be determined with ``size_bytes = NULL`` (the held-back-unknown-size path), which a
NOT NULL column rejects with an IntegrityError mid-scan. The held_back_unknown_size half only
squares the schema with the model; the column stays NOT NULL and the app always supplies 0.

Idempotent and additive. Each change is guarded by reflecting the live column first, so a
database already carrying the corrected shape (every fresh install) is left completely
untouched: no table copy, nothing to rebuild. An old database is reshaped in place and its
rows are preserved. Testers never rebuild their database.

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

# Copy the database aside before this runs (`reaper.db.schema_gate.SNAPSHOT_ATTR`, #566). Its
# `alter_column` is a full table copy taken from SQLite's reflection, which is the shape that
# loses a constraint reflection does not report.
needs_snapshot = True


def _column(inspector: sa.Inspector, table: str, name: str) -> dict[str, object] | None:
    """The reflected column dict, or None if the table lacks that column."""
    for col in inspector.get_columns(table):
        if col["name"] == name:
            return col
    return None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # candidate.size_bytes: NOT NULL -> nullable. Only rebuild if it is still NOT NULL, so a
    # corrected database (already nullable) is not copied for nothing.
    size_bytes = _column(inspector, "candidate", "size_bytes")
    if size_bytes is not None and not size_bytes["nullable"]:
        with op.batch_alter_table("candidate", schema=None, recreate="always") as batch_op:
            batch_op.alter_column("size_bytes", existing_type=sa.Integer(), nullable=True)

    # reap_run.held_back_unknown_size: drop the stray DEFAULT 0 (the column stays NOT NULL).
    # Only rebuild if a server default is actually present. A database that never got the
    # column at all is out of scope here and is left alone.
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
    # Intentionally not reversed. The corrected shape is the right one: re-adding NOT NULL to
    # candidate.size_bytes would fail on any legitimately-NULL (unknown) size and reintroduce
    # the IntegrityError this migration exists to close. There is nothing safe to undo.
    pass
