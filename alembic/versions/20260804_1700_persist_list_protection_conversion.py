# SPDX-License-Identifier: AGPL-3.0-or-later
"""persist the list-protection conversion, so the upgrade needs no visit to the policy page

``c3d4e5f6a7b8`` moved the keep tags into the list registry and left the policy-body half to
``engine.policy.convert_list_protections``, which runs **on load** and is never written back.
That is the shape the shim was designed for -- a protection moving between surfaces is a
policy edit nobody has saved, so the editor opens on it as a draft and the scan degrades
until it is saved (rule 65). What it misses is that the load shim can never *finish*: the
stored row stays legacy-shaped however many times the operator saves nothing, so every scan
degrades, forever, and the incomplete-scan notice on the review screen never clears (#516).

So the conversion is done once, here, where an upgrade can carry it: for each media type the
newest policy row is converted and appended as a NEW row (the table is append-only, and
snapshots and approvals point at the old one by hash). The next load finds nothing legacy,
reports no repair, and the scan is clean.

**Writing it is safe because the conversion is verdict-preserving.** An enabled ``whitelisted``
or ``curated_list`` gate becomes an ``on_list`` protect condition, which ``CustomProtectGate``
answers with PROTECT and ``decide_verdict`` honors before score or coverage are consulted --
the same branch the retired gates fired into. Nothing moves from kept to condemnable.

Three refusals, each leaving the row alone for the load shim to handle exactly as it does
today, because a migration that guesses is worse than one that declines:

* the conversion does not fully clear the legacy shape. It declines to strip an enabled gate
  whose replacement list is missing, since an ``on_list`` rule naming no list reads as a green
  "checked, did not fire" -- so the body it hands back still carries the gate on purpose, and
  writing that would persist a body ``build_gates`` refuses to scan.
* the result does not validate as a ``PolicyBody``.
* anything raises. A failed conversion must not fail the upgrade: the operator's exit is the
  editor, and it now offers a Save whether or not this ran (``PolicyRepair.LISTS_MIGRATED``).

The shim stays for what this cannot reach: a restored backup, a hand-edited body, a database
that never passed through this revision.

Revision ID: d5e6f7a8b9c0
Revises: a1b2c3d4e5f7
Create Date: 2026-08-04 17:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "a1b2c3d4e5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _list_names(conn: sa.Connection) -> tuple[str | None, str | None, tuple[str, ...]]:
    """The registry names the converted rules must point at, resolved the way
    ``services.profiles._conversion_list_names`` resolves them: by source and by age, never by
    spelling, because every one of these names is the operator's to change."""
    rows = conn.execute(sa.text("SELECT source, name FROM list_config ORDER BY id")).fetchall()
    tag = next((str(r[1]) for r in rows if r[0] == "arr_tag"), None)
    imdb = next((str(r[1]) for r in rows if r[0] == "imdb"), None)
    own = tuple(str(r[1]) for r in rows if r[0] in ("plex_collection", "plex_watchlist"))
    return tag, imdb, own


def upgrade() -> None:
    # Imported inside the function, so a module that moves or fails to import cannot stop an
    # upgrade whose other revisions are unrelated to policy bodies.
    from reaper.engine.policy import (
        PolicyBody,
        convert_list_protections,
        has_legacy_list_protections,
    )

    conn = op.get_bind()
    tag_name, imdb_name, own_names = _list_names(conn)
    now = int(datetime.now(UTC).timestamp())

    for media_type in ("movie", "tv"):
        row = conn.execute(
            sa.text("SELECT body_json, name FROM policy WHERE media_type = :mt ORDER BY id DESC LIMIT 1"),
            {"mt": media_type},
        ).first()
        if row is None:
            continue
        try:
            raw = json.loads(row[0])
        except ValueError:
            continue
        if not has_legacy_list_protections(raw):
            continue
        try:
            converted = convert_list_protections(
                raw,
                tag_list_name=tag_name,
                imdb_list_name=imdb_name,
                collection_list_names=own_names,
            )
            # A conversion that left any legacy shape behind did so deliberately, to keep a
            # protection whose replacement list does not exist. Leave the stored row alone and
            # let the load shim reach the same answer, where the editor can say so.
            if converted is None or has_legacy_list_protections(converted):
                continue
            body = PolicyBody.model_validate(converted)
        except Exception:  # noqa: BLE001 - see the module docstring's third refusal
            continue
        conn.execute(
            sa.text(
                "INSERT INTO policy (policy_hash, body_json, media_type, name, created_at) "
                "VALUES (:hash, :body, :mt, :name, :now)"
            ),
            {
                "hash": body.policy_hash(),
                "body": body.model_dump_json(),
                "mt": media_type,
                # The operator's own name for their policy, carried across. This is the same
                # policy; only where its list protections are written has changed.
                "name": str(row[1]),
                # An INTEGER unix timestamp: `db.types.EpochDateTime` stores every instant as
                # one, and raw SQL goes around the type. An ISO string here would land a row
                # the ORM raises on for every later read (b2c3d4e5f6a7 found this the hard way).
                "now": now,
            },
        )


def downgrade() -> None:
    # Nothing. The rows this appended are ordinary policy rows an operator may have since
    # edited or scanned against, and the table is append-only by design: deleting the newest
    # row would silently put a DIFFERENT policy in force, which is the substitution every
    # guard in this codebase exists to prevent. A downgrade re-reads the legacy row through
    # the load shim, which is exactly what it did before this revision.
    pass
