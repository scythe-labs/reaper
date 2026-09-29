# SPDX-License-Identifier: AGPL-3.0-or-later
"""drop the instance table's three retired test-result columns

Release M+1 for ``instance.detected_version``, ``instance.last_ok_at`` and
``instance.last_error``. Release M (2026.9.1) removed their ORM attributes.

``instance`` has no index or named constraint on these columns and no foreign key
pointing at it, so one ``drop_column`` block is the whole change. The downgrade
restores the columns empty.

Revision ID: 0c7f8c5bb333
Revises: ade1f657fcfe
Create Date: 2026-09-29 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0c7f8c5bb333"
down_revision: str | None = "ade1f657fcfe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: A dropped column cannot come back with its data, and the batch rebuild copies
#: ``instance`` from reflection.
needs_snapshot = True


def upgrade() -> None:
    with op.batch_alter_table("instance", schema=None) as batch_op:
        batch_op.drop_column("detected_version")
        batch_op.drop_column("last_ok_at")
        batch_op.drop_column("last_error")


def downgrade() -> None:
    with op.batch_alter_table("instance", schema=None) as batch_op:
        batch_op.add_column(sa.Column("detected_version", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("last_ok_at", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("last_error", sa.Text(), nullable=True))
