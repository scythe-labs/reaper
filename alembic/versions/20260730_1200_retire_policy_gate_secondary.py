# SPDX-License-Identifier: AGPL-3.0-or-later
"""drop the dead vote floor from stored policy bodies

``GateSetting.secondary`` held the rating gate's vote floor until that bar moved to
``PolicyBody.keep_rating_rules``. The only remaining reader is the loader shim
``engine.policy_migrations.recover_rating_rules``, which reads it off the raw stored dict.
The field cannot simply be deleted from the model, because ``PolicyBody`` rejects unknown
keys and every stored body was serialized with this one. A bare deletion would turn each
saved policy into a validation error, and the app would silently fall back to shipped
defaults, replacing the operator's own safety values. This migration makes the stored data
agree with the model first.

This migration does not synthesize the rating bar. A body that still carries a recoverable
bar (``keep_rating_rules`` absent, an enabled ``rating_floor`` gate, and numbers the old
validator would have accepted) is left completely untouched, because ``secondary`` is the
only surviving copy of that operator's protection. Writing the recovered bar here would
persist a safety value the operator never approved, with no flag, no degraded scan, and no
editor draft to review it in. That is why the recovery happens at load time instead. Those
rows keep the ``secondary`` key, and the shim keeps reading it, though it now strips the key
from the body it hands back so removing the field elsewhere does not break that path. Those
rows migrate themselves the first time the operator saves the draft the editor opens for
them.

Every other row, where the key is present but inert, is rewritten without it.

This migration appends. It never updates a row in place. ``db.models.Policy`` is
append-only by contract. Approvals, candidates, and audit entries point at a row's
``policy_hash`` and must stay interpretable years later, so rewriting a body in place
would leave every one of those references pointing at content that no longer hashes to
it. Instead, the newest row per media type, the one ``services.profiles.active_policy_row``
treats as in force, gets a fresh row carrying the stripped body. History stays untouched
and readable. The new row simply becomes the one in force. Older rows keep the key and are
never model-validated, since only the newest row is ever loaded.

The new row's hash differs from its parent's, and that is intentional. ``policy_hash`` is
what an approval is bound to, so after this migration the policy in force is no longer the
one any pending plan was approved under. Those plans are refused at execute time, and the
operator re-scans. That is why this revision lands on its own rather than riding along
with unrelated work.

This migration is idempotent. A newest row whose gates carry no ``secondary`` key is
skipped, so a database that already went through this, including every fresh install, is
untouched. Re-running it costs one read per media type.

The predicate below is a hand copy of ``recover_rating_rules``'s trigger. A migration must
mean the same thing forever, and importing application code would let a later edit change
what an old revision did. ``tests/test_migrations.py`` checks the two against each other,
so the copy cannot drift without a test failing.

Revision ID: e6f708192a3b
Revises: d5e6f708192a
Create Date: 2026-07-30 12:00:00.000000
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "e6f708192a3b"
down_revision: str | None = "d5e6f708192a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out rather than imported from ``engine.gates``. See the module docstring for why.
_RATING_FLOOR = "rating_floor"
_FIELD = "secondary"


def bar_is_still_recoverable(raw: object) -> bool:
    """Check whether ``secondary`` is the last copy of a rating bar nothing else records.

    A hand copy of ``engine.policy_migrations.recover_rating_rules``'s trigger, kept in sync
    by ``tests.test_migrations.test_the_migration_reads_a_recoverable_bar_exactly_as_the_shim_does``.
    True means leave the row alone.
    """
    if not isinstance(raw, dict) or "keep_rating_rules" in raw:
        return False
    gates = raw.get("gates")
    if not isinstance(gates, list):
        return False
    for gate in gates:
        if not isinstance(gate, dict) or gate.get("gate") != _RATING_FLOOR:
            continue
        if not gate.get("enabled", True):
            return False
        floor, min_votes = gate.get("threshold"), gate.get(_FIELD)
        # bool is a subclass of int in Python, so a stored `true` must not be read as 1.
        if isinstance(floor, bool) or isinstance(min_votes, bool):
            return False
        if not isinstance(floor, int) or not isinstance(min_votes, int):
            return False
        return 1 <= floor <= 100 and min_votes >= 1
    return False


def _strip(body: dict[str, Any]) -> bool:
    """Remove the key from every gate row. True if anything was there to remove."""
    gates = body.get("gates")
    if not isinstance(gates, list):
        return False
    removed = False
    for gate in gates:
        if isinstance(gate, dict) and _FIELD in gate:
            del gate[_FIELD]
            removed = True
    return removed


def _canonical(body: dict[str, Any]) -> str:
    """Serialize ``body`` to byte-stable JSON, matching
    ``engine.policy.PolicyBody.canonical_json`` exactly.

    The body holds only integers. Sorted keys and tight separators make sure the same
    policy always hashes to the same digest on any machine.
    """
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def upgrade() -> None:
    bind = op.get_bind()
    # Only the newest row per media type is ever loaded (see ``profiles.active_policy_row``),
    # so only that row has to agree with the model. This reads them by hand instead of with a
    # window function, because the table has one row per save and SQLite's window-function
    # support varies by build.
    media_types = [
        row[0] for row in bind.execute(sa.text("SELECT DISTINCT media_type FROM policy"))
    ]
    for media_type in media_types:
        row = bind.execute(
            sa.text(
                "SELECT id, body_json, name, created_at FROM policy"
                " WHERE media_type = :mt ORDER BY id DESC LIMIT 1"
            ),
            {"mt": media_type},
        ).fetchone()
        if row is None:
            continue
        _, body_json, name, created_at = row
        try:
            body = json.loads(body_json)
        except (TypeError, ValueError):
            # Not JSON. `active_policy` already reads such a row as unreadable and falls back
            # with a flag. There is nothing here to carry forward, and no reason to raise
            # mid-upgrade over a row the application already handles.
            continue
        if not isinstance(body, dict) or bar_is_still_recoverable(body):
            continue
        if not _strip(body):
            continue
        canonical = _canonical(body)
        bind.execute(
            sa.text(
                "INSERT INTO policy (policy_hash, body_json, media_type, name, created_at)"
                " VALUES (:hash, :body, :mt, :name, :created_at)"
            ),
            {
                "hash": hashlib.sha256(canonical.encode("ascii")).hexdigest(),
                "body": canonical,
                "mt": media_type,
                "name": name,
                # The parent row's own timestamp, not the current clock. This is the same
                # policy the operator already saved with one dead number taken out, so dating
                # it now would put a save they never made at the top of their history.
                "created_at": created_at,
            },
        )


def downgrade() -> None:
    """Nothing to undo.

    The rows this migration appended are ordinary policy rows. Older code reads them fine,
    since it simply defaults the missing key to 0, which is what the number meant. Deleting
    them would drop back to a body whose only difference is inert, and would pull the newest
    row out from under anything already bound to its hash.
    """
