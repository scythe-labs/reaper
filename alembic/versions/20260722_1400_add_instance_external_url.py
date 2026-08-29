"""add instance.external_url

The address the UI's jump links open for this service, when it differs from the address
Reaper connects to. An operator often reaches a service in the browser at a public address,
such as a reverse proxy or a domain, while Reaper talks to it over a LAN IP. The jump pills
should point at the address the operator can actually open. This field is display only. It
is never connected to and never sent a request, so it carries no TLS setting or key. Plex
keeps its own web_url as an AppSetting, not an Instance field, and is untouched here.

The new column is nullable, so SQLite adds it to a populated database with no rebuild.
Every existing instance reads back as NULL, meaning "no external address", which keeps
today's behavior of building links from base_url. There is no backfill. The operator fills
it in from Settings.

Revision ID: 6f7081920314
Revises: 5e6f70819203
Create Date: 2026-07-22 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "6f7081920314"
down_revision: str | None = "5e6f70819203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A nullable column needs no server default. SQLite adds it to a populated table
    # directly, and existing rows read back as NULL, which the app treats as "no external
    # address". Links then use base_url.
    with op.batch_alter_table("instance", schema=None) as batch_op:
        batch_op.add_column(sa.Column("external_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("instance", schema=None) as batch_op:
        batch_op.drop_column("external_url")
