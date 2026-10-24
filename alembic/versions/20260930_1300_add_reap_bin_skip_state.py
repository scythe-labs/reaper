# SPDX-License-Identifier: AGPL-3.0-or-later
"""add reap_bin.skip, state and announced

A reap can turn an instance's recycle bin off and put it back when it ends. These columns
record that choice and where each bin stands, so a bin still off after a crash is put back
at the next startup.

Revision ID: 8a1c4e7f2d90
Revises: 3d6e1f0a9b24
Create Date: 2026-09-30 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8a1c4e7f2d90"
down_revision: str | None = "3d6e1f0a9b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reap_bin", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("skip", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("state", sa.String(length=20), nullable=True))
        batch_op.add_column(
            sa.Column("announced", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("reap_bin", schema=None) as batch_op:
        batch_op.drop_column("announced")
        batch_op.drop_column("state")
        batch_op.drop_column("skip")
