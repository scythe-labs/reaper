# SPDX-License-Identifier: AGPL-3.0-or-later
"""add auth_session.via_recovery

Recovery mode signs an operator in when both Plex and the local password have failed them,
and the screen it lands on promised they could "reset a password" from there. They could
not: ``POST /api/settings/admin-password`` asks for the current password whenever one is
set, and a forgotten password is the reason recovery was used at all. The way out was the
``reaper-admin`` CLI, which the desktop bundles do not ship.

This column is how the server tells one session from the other. A session opened by
redeeming a recovery code carries it; every other session does not, and may still only
change the password by proving the old one. It is spent on first use, so the elevated
permission never outlives the reset it existed for.

Non-breaking by construction: one NOT NULL boolean with a server default of false, which is
also the fail-closed reading. Every session already issued backfills to "not a recovery
session" and keeps behaving exactly as it did. Testers never rebuild their database.

Revision ID: f708192a3b4c
Revises: e6f708192a3b
Create Date: 2026-08-03 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f708192a3b4c"
down_revision: str | None = "e6f708192a3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table for SQLite parity with the baseline. The server default lets SQLite
    # add a NOT NULL column to a populated table (it cannot otherwise) and backfills every
    # live session to "false"; new rows still take the model's default.
    with op.batch_alter_table("auth_session", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("via_recovery", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("auth_session", schema=None) as batch_op:
        batch_op.drop_column("via_recovery")
