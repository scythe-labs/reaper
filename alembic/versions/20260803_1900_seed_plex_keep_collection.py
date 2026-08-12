# SPDX-License-Identifier: AGPL-3.0-or-later
"""seed the "Never Reap" definition an upgrade would otherwise lose

Every Plex keep collection now comes from ``list_config``: ``sync_protection_lists`` builds
its provider from a definition row rather than from the two hardcoded strings it used to
carry (``section_name="Movies"``, ``collection_name="Never Reap"``).

**That change withdraws a protection unless this seed runs.** An install upgrading into it
has a populated ``plex-collection-never-reap`` list in the cache and an empty registry, so
the next scan would build no Plex provider, sync nothing, and the retire sweep would disable
the stored membership -- the operator's keep collection silently stops protecting, on a
release that shipped a screen promising to show them exactly that. Seeding the definition
Reaper had hardcoded means the first scan after the upgrade produces the same collection
from the same library and simply re-homes it under a slug carrying the definition's id.

The row carries no special standing. An operator with no Plex, or one who keeps nothing this
way, deletes it, and a startup fixup that re-seeded the shipped lists would put a deleted row
straight back -- which is why the seed is a one-time migration and why no such fixup exists.
(This paragraph named one, ``ensure_built_ins``, which never did: rule 7/24.)

``"Movies"`` is not a guess Reaper is entitled to keep making -- it is the bug (#483), and
carrying it here is only what makes the upgrade a no-op for the installs it happened to be
right about. The screen this ships with is where it stops being hardcoded: the definition
names the library, and an operator whose library is called anything else can now say so.

Skipped when a definition of that source already exists, so an install that reaches this
revision having already made one keeps theirs and gets no duplicate beside it.

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

#: What the code hardcoded before the registry existed. Both halves, so the seeded row
#: reproduces the old behavior exactly rather than approximately.
_LIBRARY = "Movies"
_COLLECTION = "Never Reap"


def upgrade() -> None:
    conn = op.get_bind()
    existing = conn.execute(
        sa.text("SELECT COUNT(*) FROM list_config WHERE source = 'plex_collection'")
    ).scalar_one()
    if int(existing or 0):
        return
    # A name clash is possible: `name` is unique, and an operator could have already made a
    # list called "Never Reap" from an *arr tag. Insert under a name that cannot collide with
    # what they chose, rather than failing the migration and leaving them unable to start.
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
            # An INTEGER unix timestamp, because that is what the column holds:
            # ``db.types.EpochDateTime`` stores every instant as one, and its read side calls
            # ``datetime.fromtimestamp`` on whatever is there. Binding a datetime through raw
            # SQL goes around the type and lands an ISO STRING, which SQLite accepts happily
            # and the ORM then raises on -- so every read of the table 500s, on the first page
            # load after upgrading. Found by driving it; the shipped list's own row is written
            # through the ORM, so the two rows disagreed and only this one broke.
            "now": int(datetime.now(UTC).timestamp()),
        },
    )


def downgrade() -> None:
    # Only the row this seeded, matched on its whole configuration: an operator who edited it
    # to point at their real library owns it now, and a downgrade must not take that away.
    op.get_bind().execute(
        sa.text(
            "DELETE FROM list_config WHERE source = 'plex_collection' AND built_in = 0 "
            "AND config_json = :config"
        ),
        {"config": json.dumps({"library": _LIBRARY, "collection": _COLLECTION})},
    )
