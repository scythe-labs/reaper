# SPDX-License-Identifier: AGPL-3.0-or-later
"""add snapshot.list_config_hash

Which titles a keep rule protects is decided by the protection lists, and the lists are not
policy. While the tags lived on the policy body as ``keep_tags`` they sat inside
``PolicyBody.evidence_hash``, so retagging them made the simulator refuse and ask for a scan.
Moving membership into gathered evidence (``Facts.on_lists``) moved them out from under that
hash, and nothing replaced it: an operator could retag, repoint, rename or switch off a list
and the panel went on reporting the membership the last scan froze, labelled exact (#512).

This column is the missing half. The scan records what its lists looked like
(``services.list_config.fingerprint``) and ``api.routes.simulate`` compares it against the
registry as it stands, above the tier split -- both tiers read evidence derived from that
membership, the replay through the frozen fact and the threshold path through the stored
verdict the fact produced.

Non-breaking by construction: a single nullable column. Existing rows keep NULL, which reads
as *unknown* and matches no fingerprint, so the panel refuses until the next scan rather than
asserting those snapshots were taken under today's lists. That is the one-scan cost
``PolicyBody.evidence_hash`` already documents, and it heals on the same scan. Testers never
rebuild their database.

Revision ID: a1b2c3d4e5f7
Revises: f1a2b3c4d5e6
Create Date: 2026-08-04 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f7"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table for SQLite parity with the baseline: a nullable column is a plain
    # ADD COLUMN, no table copy, so existing rows are untouched.
    with op.batch_alter_table("snapshot", schema=None) as batch_op:
        batch_op.add_column(sa.Column("list_config_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("snapshot", schema=None) as batch_op:
        batch_op.drop_column("list_config_hash")
