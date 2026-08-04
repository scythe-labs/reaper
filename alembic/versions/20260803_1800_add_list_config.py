# SPDX-License-Identifier: AGPL-3.0-or-later
"""add list_config: the operator's own protection lists

Until now every protection list was DERIVED. Reaper worked out which lists existed from the
keep tags on the policy, the Plex collection name it had hardcoded, and the one curated list
it ships with, then wrote the result into ``cache.db``. The operator could not name a list,
could not point one at a second Plex collection, and could not remove one. This table is
where a list they authored lives.

**It is in reaper.db, not cache.db, and that is the whole point of a separate table.**
``protection_list`` and ``protection_list_item`` are excluded from Alembic by name
(``alembic/env.py``) and excluded from backups (``services/backup.py``), because they are a
rebuildable mirror of somebody else's data: delete cache.db and the next sync refills it.
A list the operator *authored* is not rebuildable from anything. Storing it beside the
membership would mean a restore silently drops every list they configured, and the next scan
reaps what those lists were protecting. Membership stays in the cache; the definition lives
here, is migrated, and is backed up.

**Stable ids, so a list is not identified by its own configuration.** A derived list's slug
carries the settings that produced it -- the *arr instance and match mode, the collection
name -- so editing a setting mints a new slug and orphans the old row, which is why
``lists.retire_absent`` and its two LIKE families exist at all. A row here keeps its id
through every edit, so an edit is an UPDATE and there is nothing to retire.

Additive and non-breaking: a new table only. An install that upgrades and never opens the
screen has an empty table and behaves exactly as it did, because the derived lists are
unchanged and still sync. Testers never rebuild their database.

Revision ID: a1b2c3d4e5f6
Revises: f708192a3b4c
Create Date: 2026-08-03 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f708192a3b4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "list_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        # The operator's own name for it, and what the Policy screen offers them. Unique so
        # two lists cannot answer to one name in a rule -- a rule naming a list has to mean
        # exactly one list, or a protection points at whichever row was written last.
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        # The source's own settings: which collection in which library, which tags on which
        # *arr. JSON because each source needs different keys, and a column per source would
        # be mostly NULL and would need a migration for every source added. No server
        # defaults on these three: the model supplies Python-side defaults, and a stray
        # server default reads as schema drift to `alembic check` forever after.
        sa.Column("config_json", sa.Text(), nullable=False),
        # Off keeps the row and its name, so a list switched off can be switched back on with
        # its rules intact. Deleting is the other verb and it is the operator's to choose.
        sa.Column("enabled", sa.Boolean(), nullable=False),
        # A list Reaper ships with. It may be edited and switched off, never deleted, because
        # a rule shipped in the default policy names it (rule 25: copy may only reference a
        # mechanism that is wired).
        sa.Column("built_in", sa.Boolean(), nullable=False),
        # Epoch seconds, like every other timestamp in this schema: ``UtcTimestamp`` stores
        # them through ``db.types.EpochDateTime``, so a DateTime column here would read as
        # schema drift. This first shipped as DATETIME with server defaults on the three
        # columns above; 20260804_1300 heals a database created in that window.
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.UniqueConstraint("name", name="uq_list_config_name"),
    )


def downgrade() -> None:
    op.drop_table("list_config")
