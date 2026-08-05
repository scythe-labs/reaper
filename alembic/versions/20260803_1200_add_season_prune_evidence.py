# SPDX-License-Identifier: AGPL-3.0-or-later
"""add season_prune_evidence

The policy simulator answers a policy edit by replaying the real engine over each item's
frozen ``Candidate.facts_json``. That works for every movie control and blanked on every
season control, because ``facts_json`` freezes the season guard's *output* while
``season_pruning.plan_series_prune``'s inputs are per-show and never reached ``Facts`` at all.
Nine controls, the densest card in the editor, and the lane where most bytes usually sit
(#491). This table is those inputs, one row per show per snapshot.

Non-breaking by construction: a new table, nothing altered. A snapshot with no row for a show
reads as "this scan did not record it" and the simulator refuses the season card, never as an
empty bundle to replay a plan from -- the whole point being that a plausible wrong number on
the screen where someone chooses a deletion threshold is worse than a blank. A snapshot taken
before this migration refuses for a second reason as well: the same change re-scoped
``PolicyBody.evidence_hash``, so no earlier snapshot can match it and every edit falls to the
generic refusal until the next scan. Both heal on that scan. Testers never rebuild their
database.

Swept with its snapshot through the CASCADE below, so ``services.retention`` needs no change:
it deletes ``Snapshot`` rows and the database removes these with them, exactly as it already
does for ``candidate``.

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
    # No standalone index on `group_key`: the unique constraint above already serves the one
    # read (`routes._season_payloads`, `WHERE snapshot_id = ?`) on its leading column, and no
    # query filters a show key across snapshots.


def downgrade() -> None:
    op.drop_table("season_prune_evidence")
