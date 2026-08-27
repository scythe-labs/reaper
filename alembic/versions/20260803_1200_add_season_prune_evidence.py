# SPDX-License-Identifier: AGPL-3.0-or-later
"""add season_prune_evidence

The policy simulator answers a policy edit by replaying the real engine over each item's
frozen ``Candidate.facts_json``. That works for every movie control, but not for the
season controls, because ``facts_json`` freezes the season guard's output, while
``season_pruning.plan_series_prune``'s inputs are per-show and never reach ``Facts`` at
all. This table holds those inputs, one row per show per snapshot.

This is a new table, so nothing existing is altered. A snapshot with no row for a show
reads as "this scan did not record it", and the simulator refuses the season card rather
than replay a plan from an empty bundle. A plausible but wrong number on the screen where
someone picks a deletion threshold is worse than a blank one. A snapshot taken before this
migration also refuses, for a second reason: the same change re-scopes
``PolicyBody.evidence_hash``, so no earlier snapshot can match it, and every edit falls
back to the generic refusal until the next scan. Both cases heal on that next scan. No
tester database needs to be rebuilt.

This table is cascade-deleted with its snapshot, so ``services.retention`` needs no
change. Deleting a ``Snapshot`` row removes these with it, exactly as it already does for
``candidate``.

Revision ID: 0819a3b4c5d6
Revises: f708192a3b4c
Create Date: 2026-08-03 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0819a3b4c5d6"
down_revision: str | None = "f708192a3b4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "season_prune_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("group_key", sa.String(length=100), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["snapshot.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id", "group_key"),
    )
    # There is no standalone index on `group_key`. The unique constraint above already serves
    # the one read (`simulate._season_payloads`, `WHERE snapshot_id = ?`) on its leading
    # column, and no query filters a show key across snapshots.


def downgrade() -> None:
    op.drop_table("season_prune_evidence")
