# SPDX-License-Identifier: AGPL-3.0-or-later
"""persist the list-protection conversion, so the upgrade needs no visit to the policy page

``c3d4e5f6a7b8`` moved the keep tags into the list registry and left the
policy-body half to ``engine.policy_migrations.convert_list_protections``, which
runs on load and is never written back. That shim exists because a protection
moving between surfaces is a policy edit nobody has saved, so the editor opens on
it as a draft and the scan degrades until it is saved. But the load shim can never
finish: the stored row stays legacy-shaped no matter how many times the operator
saves nothing, so every scan degrades forever, and the incomplete-scan notice on
the review screen never clears.

So the conversion runs once, here, where an upgrade can carry it: for each media
type, the newest policy row is converted and appended as a new row (the table is
append-only, and snapshots and approvals point at the old one by hash). The next
load finds nothing legacy, reports no repair, and the scan is clean.

Writing it is safe because the conversion preserves the verdict. An enabled
``whitelisted`` or ``curated_list`` gate becomes an ``on_list`` protect condition,
which ``CustomProtectGate`` answers with PROTECT and ``decide_verdict`` honors
before score or coverage are consulted, the same branch the retired gates fired
into. Nothing moves from kept to condemnable.

Three refusals leave the row alone for the load shim to handle exactly as it does
today, because a migration that guesses is worse than one that declines:

* the conversion does not fully clear the legacy shape. It declines to strip an
  enabled gate whose replacement list is missing, since an ``on_list`` rule naming
  no list reads as a green "checked, did not fire." So the body it hands back
  still carries the gate on purpose, and writing that would persist a body
  ``build_gates`` refuses to scan.
* the result does not validate as a ``PolicyBody``.
* anything raises. A failed conversion must not fail the upgrade: the operator's
  exit is the editor, and it now offers a Save whether or not this ran
  (``PolicyRepair.LISTS_MIGRATED``).

The shim stays for what this cannot reach: a restored backup, a hand-edited body,
a database that never passed through this revision.

Each candidate list is resolved by ``policy_migrations.conversion_list_names``,
which identifies a list by what it holds rather than by when it was created.
Picking the oldest list of a given kind is fragile: if the operator deletes the
tag list this conversion expects and later adds an unrelated list from the same
source, that new list becomes the oldest one of its kind, and resolving by age
would write its name into the policy permanently instead. Comparing content
avoids that. ``media_type`` also scopes the IMDb rule to the movie policy, since
that chart is movies only, and a TV row converts to the tag list alone.

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


def _list_rows(conn: sa.Connection) -> list[tuple[str, str, str | None]]:
    """Every registry row as ``policy_migrations.conversion_list_names`` takes them,
    oldest first. That function decides which row answers each half of the
    conversion, here and on the load path alike."""
    rows = conn.execute(
        sa.text("SELECT source, name, config_json FROM list_config ORDER BY id")
    ).fetchall()
    return [(str(r[0]), str(r[1]), r[2]) for r in rows]


def _library_media_types(conn: sa.Connection) -> dict[str, frozenset[str]]:
    """Casefolded Plex library title -> the media types it spans, from the synced
    ``plex_libraries`` setting. Read best-effort: this only ever narrows a
    collection's rule to one policy, so an absent, unparseable, or unreadable
    setting leaves every collection covered by both policies. That is the wider
    protection, and it matches what the load path does when it cannot tell either."""
    from reaper.engine.policy_migrations import library_media_types

    try:
        row = conn.execute(
            sa.text("SELECT value_json FROM app_setting WHERE key = 'plex_libraries'")
        ).first()
    except sa.exc.SQLAlchemyError:
        return {}
    if row is None:
        return {}
    try:
        libraries = json.loads(row[0])
    except (ValueError, TypeError):
        return {}
    if not isinstance(libraries, list):
        return {}
    return library_media_types([lib for lib in libraries if isinstance(lib, dict)])


def upgrade() -> None:
    # Imported inside the function, so a module that moves or fails to import cannot stop an
    # upgrade whose other revisions are unrelated to policy bodies.
    from reaper.engine.policy import PolicyBody
    from reaper.engine.policy_migrations import (
        conversion_list_names,
        convert_list_protections,
        has_legacy_list_protections,
        legacy_keep_tags,
        own_list_media_scope,
    )

    conn = op.get_bind()
    rows = _list_rows(conn)
    # One derivation of a collection's media scope for both bodies: a
    # single-library collection's rule lands only on the policy for its
    # library's type.
    collection_scope = own_list_media_scope(rows, _library_media_types(conn))
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
            # Resolved per media type, because which list answers the tag half depends
            # on the tags this body was protecting on, and the two policies are tuned
            # separately.
            tag_name, imdb_name, own_names = conversion_list_names(
                rows, keep_tags=legacy_keep_tags(raw)
            )
            converted = convert_list_protections(
                raw,
                media_type=media_type,
                tag_list_name=tag_name,
                imdb_list_name=imdb_name,
                collection_list_names=own_names,
                collection_media_scope=collection_scope,
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
                # The operator's own name for their policy, carried across.
                # This is the same policy. Only where its list protections
                # are written has changed.
                "name": str(row[1]),
                # An INTEGER unix timestamp: `db.types.EpochDateTime` stores
                # every instant as one, and raw SQL goes around that type. An
                # ISO string here would produce a row the ORM cannot read back.
                "now": now,
            },
        )


def downgrade() -> None:
    # Nothing. The rows this appended are ordinary policy rows an operator may
    # have since edited or scanned against, and the table is append-only by
    # design: deleting the newest row would silently put a different policy in
    # force, which is the substitution every guard in this codebase exists to
    # prevent. A downgrade re-reads the legacy row through the load shim,
    # exactly as it did before this revision.
    pass
