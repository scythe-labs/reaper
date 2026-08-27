# SPDX-License-Identifier: AGPL-3.0-or-later
"""index action_step.run_id

Every read of a run's journal filters on ``run_id``: the executor's step load
(``_load``), its reload after a journal rollback (``_revive``), and the Runs page's step
listing (``api.runs._run_steps``). SQLite does not index a foreign key on its own, so
``EXPLAIN QUERY PLAN`` returned ``SCAN action_step`` for all three.

That is a scan of a table nothing removes rows from. Retention deletes snapshots and
excludes every snapshot a run points at, so ``action_step`` and ``reap_run`` grow for the
life of the install and each journal read gets slower with every reap ever executed. The
exclusion stays as it is: ``services/retention.py`` prices the rolling delete cap off those
same pinned rows and calls it a safety interlock, so narrowing it to live or recent runs
would silently unprice the cap. The growth is paid for. The scan was not.

Additive and cheap: ``CREATE INDEX`` is plain DDL, building a new b-tree and never
rebuilding ``action_step``. Existing rows are untouched, and a database that has not run
this yet behaves exactly as before, only slower. Testers never rebuild.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-08-10 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f7a8b9c0d1e2"
down_revision: str | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_action_step_run_id", "action_step", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_action_step_run_id", table_name="action_step")
