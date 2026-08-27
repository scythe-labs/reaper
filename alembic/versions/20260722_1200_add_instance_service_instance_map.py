"""add instance.service_instance_map

Adds the multi-Seerr requester-attribution map. It is a JSON object mapping each of a
Seerr portal's own services to the Reaper Sonarr/Radarr instance it adds media to, keyed
"{kind}:{serviceId}" since Seerr numbers Sonarr and Radarr services separately, for
example {"sonarr:0": 7, "radarr:0": 8}.

A Seerr request carries the *arr's own item id (externalServiceId) and the portal-local
serviceId. This map resolves serviceId to a Reaper instance, so "requested by" can bind
the exact copy a person asked for, such as the main library versus a restricted one,
instead of the looser tmdb/tvdb match across every copy. It is display only. It never
gates a decision. See ``services.requested_by.build_map``.

The new column is nullable, so SQLite adds it to a populated database with no rebuild.
Every existing instance reads back as NULL, meaning "no map", which keeps today's
tmdb/tvdb match behavior. There is no backfill. The operator fills the map in from the
Seerr instance's edit form in Settings.

Revision ID: 4d5e6f708192
Revises: 3c4d5e6f7081
Create Date: 2026-07-22 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "4d5e6f708192"
down_revision: str | None = "3c4d5e6f7081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A nullable column needs no server default. SQLite adds it to a populated table
    # directly, and existing rows read back as NULL, which the app treats as "no service map".
    with op.batch_alter_table("instance", schema=None) as batch_op:
        batch_op.add_column(sa.Column("service_instance_map", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("instance", schema=None) as batch_op:
        batch_op.drop_column("service_instance_map")
