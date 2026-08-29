# SPDX-License-Identifier: AGPL-3.0-or-later
"""seed the "Never Reap" definition an upgrade would otherwise lose

Every Plex keep collection now comes from ``list_config``. ``sync_protection_lists``
builds its provider from a definition row instead of from the two hardcoded strings it
used to carry, ``section_name="Movies"`` and ``collection_name="Never Reap"``.

That change withdraws a protection unless this seed runs. An install upgrading into it
has a populated ``plex-collection-never-reap`` list in the cache, but an empty registry.
The next scan would build no Plex provider, sync nothing, and the retire sweep would
disable the stored membership: the operator's keep collection would silently stop
protecting anything. Seeding the definition Reaper had hardcoded means the first scan
after the upgrade produces the same collection from the same library, and simply
re-homes it under a slug carrying the definition's id.

The seeded row carries no special standing. An operator with no Plex, or one who does
not keep titles this way, can delete it. There is no startup step that re-seeds shipped
lists, because that would put a deleted row straight back. That is why this seed runs
once, as a migration, instead.

``"Movies"`` was a guess baked into the old code, not a fact about every library. Seeding
it here only makes the upgrade a no-op for the installs it happened to be right about.
The screen this migration ships with is where the guess stops: the definition names the
library explicitly, and an operator whose library is called anything else can now say so.

This seed is skipped when a definition of that source already exists, so an install that
reaches this revision having already made one keeps theirs and gets no duplicate beside
it.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-03 19:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: What the code hardcoded before the registry existed. Both values are kept, so the
#: seeded row reproduces the old behavior exactly.
_LIBRARY = "Movies"
_COLLECTION = "Never Reap"


def upgrade() -> None:
    conn = op.get_bind()
    existing = conn.execute(
        sa.text("SELECT COUNT(*) FROM list_config WHERE source = 'plex_collection'")
    ).scalar_one()
    if int(existing or 0):
        return
    # A name clash is possible. `name` is unique, and an operator could have already made a
    # list called "Never Reap" from an *arr tag. Insert under a name that cannot collide
    # with what they chose, instead of failing the migration and leaving them unable to
    # start.
    taken = {
        str(row[0]) for row in conn.execute(sa.text("SELECT name FROM list_config")).fetchall()
    }
    name = _COLLECTION if _COLLECTION not in taken else f"{_COLLECTION} (Plex)"
    if name in taken:
        return
    conn.execute(
        sa.text(
            "INSERT INTO list_config (name, source, config_json, enabled, built_in, created_at) "
            "VALUES (:name, 'plex_collection', :config, 1, 0, :now)"
        ),
        {
            "name": name,
            "config": json.dumps({"library": _LIBRARY, "collection": _COLLECTION}),
            # This column holds an integer unix timestamp. ``db.types.EpochDateTime`` stores
            # every instant that way, and its read side calls ``datetime.fromtimestamp`` on
            # whatever is there. Binding a Python datetime through raw SQL bypasses that type
            # and stores an ISO string instead, which SQLite accepts but the ORM later
            # raises on, so every read of the table would fail after the upgrade.
            "now": int(datetime.now(UTC).timestamp()),
        },
    )


def downgrade() -> None:
    # This deletes only the row this migration seeded, matched on its whole configuration.
    # An operator who edited it to point at their real library owns it now, and a downgrade
    # must not take that away.
    op.get_bind().execute(
        sa.text(
            "DELETE FROM list_config WHERE source = 'plex_collection' AND built_in = 0 "
            "AND config_json = :config"
        ),
        {"config": json.dumps({"library": _LIBRARY, "collection": _COLLECTION})},
    )
