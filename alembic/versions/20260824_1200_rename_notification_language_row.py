# SPDX-License-Identifier: AGPL-3.0-or-later
"""move the app_setting row notification_language -> language

One picker in Settings -> General now sets both the language the app is shown in and the
language a notification is written in, so the row is no longer about notifications.

``app_setting`` is a key/value table, so this renames one row's key and touches no schema. An
operator who had chosen a Discord language keeps it as their app language.

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

_RENAME = "UPDATE app_setting SET key = :to_key WHERE key = :from_key"
_OLD_KEY = "notification_language"
_NEW_KEY = "language"


def upgrade() -> None:
    op.get_bind().execute(sa.text(_RENAME), {"to_key": _NEW_KEY, "from_key": _OLD_KEY})


def downgrade() -> None:
    op.get_bind().execute(sa.text(_RENAME), {"to_key": _OLD_KEY, "from_key": _NEW_KEY})
