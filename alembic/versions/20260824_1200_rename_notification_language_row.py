# SPDX-License-Identifier: AGPL-3.0-or-later
"""move the app_setting row notification_language -> language

The Discord picker in Settings -> Notifications is gone. One picker in Settings -> General now
sets both the language the app is shown in and the language a notification is written in, so
the row it writes is no longer about notifications and is renamed to say so.

No schema change at all: ``app_setting`` is a key/value table, so this moves one row's key and
leaves every column as it was. An operator who had chosen a Discord language keeps it, now as
their app language too; one who never touched it has no row and still has none.

The move is skipped when a ``language`` row already exists, so the newer value wins rather than
being overwritten by a stale one, and so re-running the migration cannot resurrect the old key.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-08-24 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "c0d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KEY = "notification_language"
_NEW_KEY = "language"


def _move(from_key: str, to_key: str) -> None:
    """Rename one ``app_setting`` row, leaving the destination alone if it is already there."""
    bind = op.get_bind()
    exists = sa.text("SELECT 1 FROM app_setting WHERE key = :key")
    if bind.execute(exists, {"key": to_key}).first() is not None:
        return
    bind.execute(
        sa.text("UPDATE app_setting SET key = :to_key WHERE key = :from_key"),
        {"to_key": to_key, "from_key": from_key},
    )


def upgrade() -> None:
    _move(_OLD_KEY, _NEW_KEY)


def downgrade() -> None:
    _move(_NEW_KEY, _OLD_KEY)
