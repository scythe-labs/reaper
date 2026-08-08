# SPDX-License-Identifier: AGPL-3.0-or-later
"""let six write-only columns survive their ORM attributes leaving

Rule 148's **release M**. Six columns are write-only across ``src/``, ``tests/`` and
``frontend/src/``, and this release removes their ORM attributes. Five of them are
``NOT NULL`` with no server default, so the Python-side ``default=`` is what has been
filling them -- and that dies with the attribute, leaving the next ``INSERT`` omitting a
column SQLite will refuse. That is a fresh install failing its first Plex link or its
first settings save, which is why the schema moves BEFORE the attributes and not with
them.

**Nothing is dropped here, deliberately.** One release where both images work is what
makes the operator's rollback survivable: release M-1 still carries every attribute and
still writes every column, and it keeps working against this database unchanged. Release
M+1 drops the six, under rule 148's three obligations.

**Two shapes, chosen per column rather than uniformly.**

*A server default*, where the value the Python side was writing is a real answer this
release still means: ``profile.enabled`` and ``list_config.built_in`` both carried
``default=False``, so ``sa.false()`` writes exactly what the retiring code wrote.

*Nullable*, where there is no honest default: ``pending_plex_login.pin_code``,
``plex_server.owner_plex_account_id`` and ``profile.active_policy_id``. Inventing ``""``
or ``0`` would put a wrong definite value in a column an older image can still read,
where NULL says the only true thing -- this image did not write it. ``active_policy_id``
forces the question rather than merely inviting it: it is a ``FOREIGN KEY`` to
``policy.id`` and ``PRAGMA foreign_keys`` is ON (``db/session.py``), so a
``server_default`` of ``0`` would point every new profile at a policy row that does not
exist and the insert would fail -- the exact first-save break this revision exists to
prevent. A NULL foreign key is permitted and checks clean.

``candidate.poster_url`` needs nothing here. It is already nullable with a Python-side
``default=None``, so the omitted column simply reads NULL; it appears in this docstring
only because it is the sixth attribute leaving, and it still needs its ``include_name``
arm in ``alembic/env.py`` or ``alembic check`` reports a pending ``drop_column`` forever.

**One ``batch_alter_table`` block per table.** Each block is a full copy of the table
under ``render_as_batch``, so ``profile``'s two columns move together; four blocks, four
rebuilds. ``recreate`` is left at its default, which already recreates for an
``alter_column`` on SQLite.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-08 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("profile", schema=None) as batch_op:
        batch_op.alter_column(
            "enabled",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.false(),
        )
        batch_op.alter_column(
            "active_policy_id",
            existing_type=sa.Integer(),
            nullable=True,
        )

    with op.batch_alter_table("pending_plex_login", schema=None) as batch_op:
        batch_op.alter_column(
            "pin_code",
            existing_type=sa.String(length=20),
            nullable=True,
        )

    with op.batch_alter_table("plex_server", schema=None) as batch_op:
        batch_op.alter_column(
            "owner_plex_account_id",
            existing_type=sa.Integer(),
            nullable=True,
        )

    # `name` is re-declared with its collation on purpose, and dropping this line silently
    # un-protects the table. A batch rebuild copies the table from SQLite's REFLECTION, and
    # reflection does not report collations (`20260804_1400`'s own comment says so, having
    # learned it) -- so a rebuild that does not restate `COLLATE NOCASE` recreates `name` as
    # a case-SENSITIVE unique column, and "Holiday" and "holiday" become two lists answering
    # to one keep rule. Nothing in the shape of this migration hints at that; the behavioral
    # test is what caught it (`test_migrations.TestAListNameIsUniqueWithoutRegardToCase`), and
    # it is why rule 148 asks for surviving constraints to be asserted rather than eyeballed.
    with op.batch_alter_table("list_config", schema=None) as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=100),
            type_=sa.String(length=100, collation="NOCASE"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "built_in",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.false(),
        )


def downgrade() -> None:
    """Restores the old shape over a backfill, because a row this release wrote may hold a
    NULL the old shape forbids.

    Reversible, unlike the M+1 drop this precedes: nothing was removed, so nothing has to
    come back. The backfilled values are the ones the retiring Python defaults would have
    written. ``active_policy_id`` is the exception and has no correct value to invent, so
    it is pointed at the lowest real ``policy.id`` -- a live row beats both a dangling
    foreign key and a failed constraint. A database holding a profile and no policy at all
    cannot be reversed honestly, and says so rather than guessing.
    """
    with op.batch_alter_table("list_config", schema=None) as batch_op:
        batch_op.alter_column(
            "built_in",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=None,
        )

    op.execute(
        sa.text("UPDATE plex_server SET owner_plex_account_id = 0 WHERE owner_plex_account_id IS NULL")
    )
    with op.batch_alter_table("plex_server", schema=None) as batch_op:
        batch_op.alter_column(
            "owner_plex_account_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

    op.execute(sa.text("UPDATE pending_plex_login SET pin_code = '' WHERE pin_code IS NULL"))
    with op.batch_alter_table("pending_plex_login", schema=None) as batch_op:
        batch_op.alter_column(
            "pin_code",
            existing_type=sa.String(length=20),
            nullable=False,
        )

    bind = op.get_bind()
    orphaned = bind.execute(
        sa.text("SELECT COUNT(*) FROM profile WHERE active_policy_id IS NULL")
    ).scalar_one()
    if orphaned:
        oldest = bind.execute(sa.text("SELECT MIN(id) FROM policy")).scalar_one_or_none()
        if oldest is None:
            raise RuntimeError(
                "cannot restore profile.active_policy_id NOT NULL: a profile has no policy to "
                "point at and this database holds none. Upgrade again, or add a policy first."
            )
        bind.execute(
            sa.text("UPDATE profile SET active_policy_id = :oldest WHERE active_policy_id IS NULL"),
            {"oldest": oldest},
        )
    with op.batch_alter_table("profile", schema=None) as batch_op:
        batch_op.alter_column(
            "active_policy_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.alter_column(
            "enabled",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=None,
        )
