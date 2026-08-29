# SPDX-License-Identifier: AGPL-3.0-or-later
"""add list_config: the operator's own protection lists

Reaper derives some protection lists on its own, from the keep tags on the policy, a
hardcoded Plex collection name, and one curated list it ships with, then writes the
result into ``cache.db``. This table adds a second kind: a list the operator names,
points at their own Plex collection or *arr tag, and can remove.

This table lives in ``reaper.db``, not ``cache.db``, and that is the whole point of a
separate table. ``protection_list`` and ``protection_list_item`` are excluded from
Alembic (``alembic/env.py``) and from backups (``services/backup.py``), because they
are a rebuildable mirror of somebody else's data. Deleting ``cache.db`` and letting the
next sync refill it loses nothing. A list the operator authored is not rebuildable from
anything else, so storing it beside the membership would mean a restore silently drops
every list they configured, and the next scan would reap what those lists were
protecting. Membership stays in the cache. The definition lives here, is migrated, and
is backed up.

A row here keeps a stable id, so a list is never identified by its own configuration.
A derived list's slug carries the settings that produced it, such as the *arr instance,
the match mode, or the collection name, so editing a setting mints a new slug and orphans
the old row. That is why ``lists.retire_absent`` exists, along with the two matching
patterns it uses to catch a slug that changed. A row in this table keeps its id through
every edit, so an edit is a plain UPDATE and there is nothing to retire.

This is a new table only, so nothing existing changes. An install that upgrades and
never opens the screen has an empty table and behaves exactly as before, because the
derived lists are unchanged and still sync. No tester database needs to be rebuilt.

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
        # The operator's own name for the list, shown on the Policy screen. Unique, so a
        # rule naming a list always means exactly one list. Without that, a protection
        # would point at whichever row was written last.
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        # The source's own settings, such as which collection in which library, or which
        # tags on which *arr. This is JSON because each source needs different keys. A
        # column per source would be mostly NULL and would need a migration for every
        # source added. These three columns have no server default: the model supplies
        # Python-side defaults, and a stray server default would read as schema drift to
        # `alembic check` forever after.
        sa.Column("config_json", sa.Text(), nullable=False),
        # Turning a list off keeps its row and name, so it can be switched back on with
        # its rules intact. Deleting is a separate action, and it is the operator's choice.
        sa.Column("enabled", sa.Boolean(), nullable=False),
        # A list Reaper ships with. It can be edited and switched off, but never deleted,
        # because a rule in the default policy names it.
        sa.Column("built_in", sa.Boolean(), nullable=False),
        # Epoch seconds, like every other timestamp in this schema. ``EpochDateTime`` (see
        # ``db.types.EpochDateTime``) stores them this way, so a DateTime column here would
        # read as schema drift. This column first shipped as DATETIME with server defaults
        # on the three columns above. 20260804_1300_heal_list_config_shape.py heals a
        # database created in that window.
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.UniqueConstraint("name", name="uq_list_config_name"),
    )


def downgrade() -> None:
    op.drop_table("list_config")
