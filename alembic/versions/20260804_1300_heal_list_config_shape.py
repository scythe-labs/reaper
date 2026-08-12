# SPDX-License-Identifier: AGPL-3.0-or-later
"""heal list_config's column shapes on databases created in the branch window

``add_list_config`` first shipped creating ``created_at`` as DATETIME and putting server
defaults on ``config_json``/``enabled``/``built_in``, while the model declares
``EpochDateTime`` (an INTEGER unix timestamp) and Python-side defaults. That migration is
corrected in place, so a fresh database gets the model's shape -- but a CREATE TABLE only
runs once, so any database that upgraded through the earlier spelling keeps the old shape
and reads as drift to ``alembic check`` forever. This is the missing rebuild, the same
obligation rule 81 states for a baseline edit.

Idempotent and additive. Guarded by reflecting the live column first, so a database
already carrying the corrected shape (every fresh install) is left completely untouched.
An old database is reshaped in place and its rows are preserved: the stored values are
already integer epochs (the ORM's ``EpochDateTime`` wrote them), so only the declared
type and the stray defaults move. Testers never rebuild their database.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-04 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copy the database aside before this runs (`reaper.db.schema_gate.SNAPSHOT_ATTR`, #566). Its
# `alter_column` is a full table copy taken from SQLite's reflection, and the table it rebuilds
# is `list_config`, which is the one carrying a collation reflection does not report.
needs_snapshot = True


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "list_config" not in inspector.get_table_names():
        return
    columns = {col["name"]: col for col in inspector.get_columns("list_config")}
    created_at = columns.get("created_at")
    stray_defaults = [
        name
        for name in ("config_json", "enabled", "built_in")
        if columns.get(name) is not None and columns[name]["default"] is not None
    ]
    dated = created_at is not None and not isinstance(created_at["type"], sa.Integer)
    if not dated and not stray_defaults:
        return

    with op.batch_alter_table("list_config", schema=None, recreate="always") as batch_op:
        if dated:
            batch_op.alter_column(
                "created_at",
                existing_type=sa.DateTime(timezone=True),
                type_=sa.Integer(),
                existing_nullable=False,
            )
        for name in stray_defaults:
            batch_op.alter_column(
                name,
                existing_type=(sa.Text() if name == "config_json" else sa.Boolean()),
                existing_nullable=False,
                server_default=None,
            )


def downgrade() -> None:
    # Intentionally not reversed. The corrected shape is the model's; putting the DATETIME
    # spelling back would only recreate the drift this exists to close.
    pass
