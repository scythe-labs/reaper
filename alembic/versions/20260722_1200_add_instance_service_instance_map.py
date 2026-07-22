"""add instance.service_instance_map

The multi-Seerr requester-attribution map: a JSON object mapping a Seerr portal's own service
ids to the Reaper Sonarr/Radarr instance each one adds media to, e.g. {"2": 7, "3": 8}. A Seerr
request carries the *arr's own item id (externalServiceId) plus the portal-local serviceId, so
this map resolves serviceId -> Reaper instance and lets "requested by" bind the exact copy a
person asked for (the main vs a restricted library) instead of the loose tmdb/tvdb union across
every copy. Display only, never a gate (services.requested_by.build_map).

Additive and non-breaking by construction: a single NULLABLE column, so SQLite adds it to a
populated tester database with no default and no rebuild, and every existing instance reads as
NULL -- "no map" -- which keeps today's tmdb/tvdb union behavior. No backfill; the operator
fills the map in from the Seerr instance's edit form in Settings.

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
    # A nullable column needs no server default: SQLite adds it to a populated table directly,
    # and existing rows read NULL, which the app treats as "no service map".
    with op.batch_alter_table("instance", schema=None) as batch_op:
        batch_op.add_column(sa.Column("service_instance_map", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("instance", schema=None) as batch_op:
        batch_op.drop_column("service_instance_map")
