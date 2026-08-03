"""add instance.external_url

The address the UI's jump links open for this service, when it differs from the one Reaper
connects to. An operator often reaches a service in the browser at a public address (a reverse
proxy or a domain) while Reaper talks to it over a LAN IP; the jump pills should point at the
address the operator can actually open. Display only: never connected to, never sent a request,
so it carries no TLS or key. Plex keeps its own web_url (an AppSetting, not an Instance) and is
untouched.

Additive and non-breaking by construction: a single NULLABLE column, so SQLite adds it to a
populated tester database with no default and no rebuild, and every existing instance reads as
NULL -- "no external address" -- which keeps today's behavior, links built from base_url. No
backfill; the operator fills it in from Settings.

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
    # A nullable column needs no server default: SQLite adds it to a populated table directly,
    # and existing rows read NULL, which the app treats as "no external address" (links use
    # base_url).
    with op.batch_alter_table("instance", schema=None) as batch_op:
        batch_op.add_column(sa.Column("external_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("instance", schema=None) as batch_op:
        batch_op.drop_column("external_url")
