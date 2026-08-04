# SPDX-License-Identifier: AGPL-3.0-or-later
"""a list name is unique without regard to case

``list_config.name`` was unique byte for byte while every reader case-folds it (rule 88),
so "Never Reap" and "never reap" were two rows to SQLite and one list to the policy: the
second never got a keep rule of its own, it shared the first's, and deleting either one
took that rule away and stopped the other protecting (#508).

The column gains ``COLLATE NOCASE``, which is what makes its UNIQUE constraint compare the
way the code does. Additive in effect and safe on a populated database: SQLite cannot alter
a collation in place, so this is a batch rebuild, and the rows carry over untouched.

Guarded twice. The stored DDL is read first, so a database already carrying the collation
(every fresh install) is left alone. Then any name that already collides is disambiguated
before the constraint could refuse it -- a suffix on the LATER row, keeping the oldest
spelling, because the older row is the one whose name the keep rule is most likely to spell.

**A rename takes the renamed row's protection with it, so the rule is re-spelled in the same
transaction.** That is the whole defect this revision is about, seen from the other end: one
rule was covering both rows precisely because ``on_list`` case-folds both sides
(``engine.fields._compare``), so suffixing the later row leaves it matching nothing, and the
next ``sync_rule_names`` rewrites its stored membership to a spelling no rule names. The keep
rule naming the shared spelling is DUPLICATED for the new one, never moved: the older row
still answers to the original, and both lists go on keeping exactly what they kept before the
upgrade. ``services.list_rules.rename_list`` is the same act at runtime.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-04 14:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _disambiguate(bind: sa.Connection) -> list[tuple[str, str]]:
    """Give every case-colliding name its own spelling, oldest row first.

    Returns ``(spelling it collided under, new spelling)`` per renamed row, for the keep rules
    that have to follow it.

    Reads the whole table rather than a GROUP BY: it holds a handful of rows, and the
    suffixed name has to be checked against the names already taken, including ones this
    loop just handed out.
    """
    rows = bind.execute(sa.text("SELECT id, name FROM list_config ORDER BY id")).all()
    taken = set()
    renames: list[tuple[str, str]] = []
    for row in rows:
        name = str(row.name)
        key = name.strip().casefold()
        if key not in taken:
            taken.add(key)
            continue
        # Truncated to the column's 100 characters INCLUDING the suffix, or the rebuild
        # below refuses a name this migration itself made too long.
        suffix = 2
        while f"{name[:94]} ({suffix})".strip().casefold() in taken:
            suffix += 1
        renamed = f"{name[:94]} ({suffix})"
        taken.add(renamed.strip().casefold())
        bind.execute(
            sa.text("UPDATE list_config SET name = :new WHERE id = :id"),
            {"new": renamed, "id": row.id},
        )
        renames.append((name, renamed))
    return renames


def _respell_keep_rules(bind: sa.Connection, renames: Sequence[tuple[str, str]]) -> None:
    """Give each renamed list a keep rule of its own, appended to the newest policy per type.

    Additive by construction, which is what makes writing a policy body from a migration
    safe here: the rule naming the shared spelling is COPIED, never edited, so the row that
    kept its name keeps its cover and the renamed row regains the cover the suffix took. No
    title moves from kept toward condemnable.

    Appended as a new row, because ``db.models.Policy`` is append-only by contract: snapshots
    and approvals point at the parent by hash and must stay readable. A pending approval is
    voided by the hash moving, exactly as after any policy edit (rule 113), and an upgrade is
    already a moment no plan survives.

    Declines rather than guesses, on the same three grounds as ``d5e6f7a8b9c0``: a body that
    is not JSON, one that carries no rule naming a renamed list, and one the model will not
    validate. A legacy-shaped body takes the second exit on its own, since the rules only
    exist after the list conversion.
    """
    if not renames:
        return
    # Imported inside the function, so a module that moves or fails to import cannot stop an
    # upgrade whose other revisions have nothing to do with policy bodies.
    from reaper.engine.policy import PolicyBody

    now = int(datetime.now(UTC).timestamp())
    folded = [(old.strip().casefold(), new) for old, new in renames]
    for media_type in ("movie", "tv"):
        row = bind.execute(
            sa.text(
                "SELECT body_json, name FROM policy WHERE media_type = :mt ORDER BY id DESC LIMIT 1"
            ),
            {"mt": media_type},
        ).first()
        if row is None:
            continue
        try:
            raw = json.loads(row[0])
        except ValueError:
            continue
        if not isinstance(raw, dict):
            continue
        changed = False
        # Both strengths, the way every other list-rule sweep walks them: an outright keep
        # and a graded lean are the same protection at two weights (``list_rules``).
        for key in ("protect_conditions", "graded_keeps"):
            rules = raw.get(key)
            if not isinstance(rules, list):
                continue
            extra: list[dict[str, object]] = []
            for rule in rules:
                if not isinstance(rule, dict) or rule.get("field") != "on_list":
                    continue
                value = rule.get("value")
                if not isinstance(value, str):
                    continue
                for old_key, new_name in folded:
                    if value.strip().casefold() == old_key:
                        extra.append({**rule, "value": new_name})
            if extra:
                raw[key] = [*rules, *extra]
                changed = True
        if not changed:
            continue
        try:
            body = PolicyBody.model_validate(raw)
        except Exception:  # noqa: BLE001 - see the docstring's third refusal
            continue
        bind.execute(
            sa.text(
                "INSERT INTO policy (policy_hash, body_json, media_type, name, created_at) "
                "VALUES (:hash, :body, :mt, :name, :now)"
            ),
            {
                "hash": body.policy_hash(),
                "body": body.model_dump_json(),
                "mt": media_type,
                # The operator's own name for their policy, carried across: this is the same
                # policy, and only the spelling of a list it names has changed.
                "name": str(row[1]),
                # An INTEGER unix timestamp: `db.types.EpochDateTime` stores every instant as
                # one, and raw SQL goes around the type. An ISO string lands a row the ORM
                # raises on for every later read (b2c3d4e5f6a7 found this the hard way).
                "now": now,
            },
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "list_config" not in inspector.get_table_names():
        return
    # The declared DDL, because a collation is not something SQLite reflection reports:
    # `get_columns` gives the type and nothing about how it compares.
    declared = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'list_config'")
    ).scalar_one_or_none()
    if declared is None or "NOCASE" in str(declared).upper():
        return

    _respell_keep_rules(bind, _disambiguate(bind))
    with op.batch_alter_table("list_config", schema=None, recreate="always") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=100),
            type_=sa.String(length=100, collation="NOCASE"),
            existing_nullable=False,
        )


def downgrade() -> None:
    # Intentionally not reversed. Putting the case-sensitive comparison back would re-open
    # the way for two lists to answer to one keep rule.
    pass
