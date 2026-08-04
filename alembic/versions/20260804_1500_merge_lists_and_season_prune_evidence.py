# SPDX-License-Identifier: AGPL-3.0-or-later
"""the lists registry and the season prune evidence meet

Two chains grew from ``add_auth_session_via_recovery`` at once: the season prune evidence
on ``dev``, and the list registry on the branch that became this one. Both are additive and
neither touches the other's tables, so nothing here has anything to do -- what this revision
carries is the single head ``alembic upgrade`` needs, and the two paths into it, so a
database that took either chain reaches head without being told to skip the other.

Re-chaining one branch onto the other would have done it in one line and been wrong: a
tester already sitting on the lists chain would find ``add_season_prune_evidence`` behind
them and never apply it, ending at head with a table the code expects and the database does
not have.

Revision ID: f1a2b3c4d5e6
Revises: 0819a3b4c5d6, e5f6a7b8c9d0
Create Date: 2026-08-04 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "f1a2b3c4d5e6"
down_revision: tuple[str, ...] = ("0819a3b4c5d6", "e5f6a7b8c9d0")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to do: this revision exists to join the two chains, not to change a schema."""


def downgrade() -> None:
    """Nothing to undo, and splitting the history again is not something to offer."""
