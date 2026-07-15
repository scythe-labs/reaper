"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
## `imports` arrives as a pre-joined string of import lines contributed by the
## render_item hooks in reaper.db.types. `import sqlalchemy as sa` is emitted above
## unconditionally, so drop it here rather than importing it twice.
${"\n".join(line for line in imports.splitlines() if line.strip() != "import sqlalchemy as sa") if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
