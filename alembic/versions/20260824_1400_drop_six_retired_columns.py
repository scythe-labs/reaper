# SPDX-License-Identifier: AGPL-3.0-or-later
"""drop the six retired columns

This is release M+1, following release M (``e6f7a8b9c0d1``), which gave these
columns a server default or made them nullable so their ORM attributes could
leave. That bought one release where the M and M-1 images read the same
schema, which is what makes an operator's rollback survivable. That release
has been served, so the columns go.

Six columns across five tables, plus the foreign key one of them carried::

    candidate.poster_url                plex_server.owner_plex_account_id
    list_config.built_in                profile.active_policy_id  (+ its FK)
    pending_plex_login.pin_code         profile.enabled

``alembic/env.py``'s ``RETIRED_COLUMNS`` and ``RETIRED_CONSTRAINTS`` empty in
the same change. That set is a bridge, not a registry, and an entry outliving
its sweep is how a dead column becomes permanent behind a growing exclusion
list.

One ``batch_alter_table`` block per table, with every drop for that table
inside it, because each block is another full copy of the table under
SQLite. Five tables, five rebuilds.

The foreign key is dropped before its column. Alembic reflects an index or
constraint on a column being dropped and recreates it against a column that
is gone: a two-line authoring slip, invisible against a fresh database and
fatal against a populated one. None of the six carries an index, so there is
no ``drop_index`` to order ahead of it.

``list_config.name`` is re-declared with its collation, and dropping that
line would silently un-protect the table. A batch rebuild copies from
SQLite's reflection, and reflection does not report collations, so a rebuild
that does not restate ``COLLATE NOCASE`` recreates ``name`` case-sensitive,
and "Holiday" and "holiday" become two lists answering to one keep rule.
Release M met this and kept the warning here for the same reason, and
``test_migrations.TestAListNameIsUniqueWithoutRegardToCase`` is what proves it.

``recreate`` is left at its default: SQLite already recreates for a drop, and
forcing it would rebuild on backends that need no copy.

The downgrade puts the columns back, not their data. A rollback past this
revision is the backup, never this function. It exists so the schema is
reversible and so a test can drive both directions. Every value these columns
held is gone the moment upgrade runs.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-08-24 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f3a4b5c6d7"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ``preflight`` copies the database aside before this runs. What it loses is
#: every value the six columns held: ``downgrade`` puts the columns back and
#: cannot put the data back, so the way back is the backup, never the image.
#: Five of the six held something an operator or a Plex link had chosen once,
#: so a rollback that recreated them empty would look like a working install
#: with its history quietly blanked.
needs_snapshot = True


def upgrade() -> None:
    with op.batch_alter_table("candidate", schema=None) as batch_op:
        batch_op.drop_column("poster_url")

    with op.batch_alter_table("pending_plex_login", schema=None) as batch_op:
        batch_op.drop_column("pin_code")

    with op.batch_alter_table("plex_server", schema=None) as batch_op:
        batch_op.drop_column("owner_plex_account_id")

    with op.batch_alter_table("profile", schema=None) as batch_op:
        # Constraint before column: the FK names `active_policy_id`, and
        # reflecting it onto a table that no longer has the column is the
        # failure this ordering exists to prevent.
        batch_op.drop_constraint("fk_profile_active_policy_id_policy", type_="foreignkey")
        batch_op.drop_column("active_policy_id")
        batch_op.drop_column("enabled")

    with op.batch_alter_table("list_config", schema=None) as batch_op:
        # See the docstring: reflection loses the collation, so it is restated on every rebuild
        # of this table or `name` comes back case-sensitive.
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=100),
            type_=sa.String(length=100, collation="NOCASE"),
            existing_nullable=False,
        )
        batch_op.drop_column("built_in")


def downgrade() -> None:
    # Reverse table order, so `profile` is whole again before anything references it. Each
    # column comes back in the shape release M left it: nullable, or carrying the server
    # default that stood in for the Python-side `default=` after the attribute left.
    with op.batch_alter_table("list_config", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("built_in", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=100),
            type_=sa.String(length=100, collation="NOCASE"),
            existing_nullable=False,
        )

    with op.batch_alter_table("profile", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("active_policy_id", sa.Integer(), nullable=True))
        # Column before constraint on the way back, the mirror of the upgrade's order.
        batch_op.create_foreign_key(
            "fk_profile_active_policy_id_policy", "policy", ["active_policy_id"], ["id"]
        )

    with op.batch_alter_table("plex_server", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_plex_account_id", sa.Integer(), nullable=True))

    with op.batch_alter_table("pending_plex_login", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pin_code", sa.String(length=20), nullable=True))

    with op.batch_alter_table("candidate", schema=None) as batch_op:
        batch_op.add_column(sa.Column("poster_url", sa.String(length=1000), nullable=True))
