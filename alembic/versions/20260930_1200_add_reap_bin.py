# SPDX-License-Identifier: AGPL-3.0-or-later
"""add reap_bin and reap_run.binned_bytes

``reap_bin`` records each Sonarr and Radarr instance's recycle bin as a reap starts.
``reap_run.binned_bytes`` is the part of a run's deleted bytes still held in a bin. Both are
new, so an older run reads as having no bin information.

Revision ID: 3d6e1f0a9b24
Revises: 0c7f8c5bb333
Create Date: 2026-09-30 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3d6e1f0a9b24"
down_revision: str | None = "0c7f8c5bb333"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reap_bin",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("reap_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("instance_id", sa.Integer(), nullable=False),
        sa.Column("instance_name", sa.String(length=100), nullable=False),
        sa.Column("bin_path", sa.String(length=500), nullable=True),
        sa.Column("cleanup_days", sa.Integer(), nullable=True),
        sa.UniqueConstraint("run_id", "kind", "instance_id"),
    )
    with op.batch_alter_table("reap_run", schema=None) as batch_op:
        batch_op.add_column(sa.Column("binned_bytes", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("reap_run", schema=None) as batch_op:
        batch_op.drop_column("binned_bytes")
    op.drop_table("reap_bin")
