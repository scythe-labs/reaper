# SPDX-License-Identifier: AGPL-3.0-or-later
"""add run journal reason json columns

Phase 11b of #885. `reap_run.aborted_reason` and `action_step.error` carry the run
journal's English, read back by SQL and by an API client with no catalog -- so the prose
stays, and a typed twin rides beside it: `aborted_reason_json` and `error_json`, the wire
shape `engine.reason.to_wire` writes for a `Reason`, so the browser can translate the
sentence.

NULL on every row written before this migration, and NULL again on a run that never
aborted or a step that never failed or skipped. A row with prose and no JSON twin thaws as
a `legacy` reason and still renders (rule 96).

Non-breaking by construction: two nullable columns, no backfill, no table rebuild under
SQLite's batch mode. Testers never rebuild their database.

Revision ID: 2d4697e6c221
Revises: c0d1e2f3a4b5
Create Date: 2026-08-22 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2d4697e6c221"
down_revision: str | None = "c0d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table for SQLite parity with the baseline: a nullable column is a plain
    # ADD COLUMN, no table copy, so existing rows are untouched.
    with op.batch_alter_table("reap_run", schema=None) as batch_op:
        batch_op.add_column(sa.Column("aborted_reason_json", sa.Text(), nullable=True))
    with op.batch_alter_table("action_step", schema=None) as batch_op:
        batch_op.add_column(sa.Column("error_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("action_step", schema=None) as batch_op:
        batch_op.drop_column("error_json")
    with op.batch_alter_table("reap_run", schema=None) as batch_op:
        batch_op.drop_column("aborted_reason_json")
