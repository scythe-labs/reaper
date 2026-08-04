# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every list is Arr-style now: the registry gains the keep tags and the IMDb variants.

Three additive data moves, no schema change:

* ``source = 'curated'`` rows become ``'imdb'`` with a ``preset`` config, the spelling the
  generalized IMDb provider reads. The operator's name survives.
* The policy keep tags become a tag list named on Settings -> Lists, seeded from the newest
  stored policy bodies (both media types, tags unioned -- the wider, keep-safe read). The
  policy-body half of the same move -- the ``on_list`` rules that make the list act -- is
  ``engine.policy.convert_list_protections``, which runs on load and is reviewed and saved
  by the operator; this migration only makes sure the list those rules name exists.
* ``lists_seeded`` is set whenever any definition exists, so ``list_config.ensure_defaults``
  does not add a second, shipped copy beside an upgraded install's own rows.

``built_in`` clears and ``enabled`` sets on every row: the Protecting switch left the UI
(a list acts through its keep rules now), and every list is removable since its rules leave
with it. A disabled row would otherwise render with no control that can re-enable it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | None = None
depends_on: str | None = None

_TAG_LIST_NAME = "Titles you've tagged"


def _stored_keep_tags(conn: sa.Connection) -> tuple[list[str], str]:
    """The keep tags the newest stored policy of each media type carries, unioned, with
    ``all`` only when every body that spoke said ``all`` -- ``any`` is the wider net. A
    body that carries no key ran on the shipped default tag."""
    tags: list[str] = []
    matches: list[str] = []
    spoke = False
    for media_type in ("movie", "tv"):
        row = conn.execute(
            sa.text(
                "SELECT body_json FROM policy WHERE media_type = :mt ORDER BY id DESC LIMIT 1"
            ),
            {"mt": media_type},
        ).first()
        if row is None:
            continue
        try:
            body = json.loads(row[0])
        except ValueError:
            continue
        if not isinstance(body, dict):
            continue
        spoke = True
        raw = body.get("keep_tags", ["reaper-keep"])
        if isinstance(raw, list):
            for tag in raw:
                spelled = str(tag).strip()
                if spelled and spelled.casefold() not in {t.casefold() for t in tags}:
                    tags.append(spelled)
        matches.append("all" if body.get("keep_tags_match") == "all" else "any")
    if not spoke:
        tags = ["reaper-keep"]
    match = "all" if matches and all(m == "all" for m in matches) else "any"
    return tags, match


def upgrade() -> None:
    conn = op.get_bind()

    # 1. The IMDb respelling. The old config body named which shipped list; only the Top 250
    # ever existed, so every 'curated' row is it.
    conn.execute(
        sa.text(
            "UPDATE list_config SET source = 'imdb', config_json = :config "
            "WHERE source = 'curated'"
        ),
        {"config": json.dumps({"preset": "top250"})},
    )
    # 2. Every list is removable and always on; the switch and the built-in lock left the UI.
    conn.execute(sa.text("UPDATE list_config SET built_in = 0, enabled = 1"))

    # 3. The keep tags become a list, for installs that have any state to carry: stored
    # policies (their tags), or definitions (the branch's earlier builds). A truly fresh
    # database leaves the flag unset and `ensure_defaults` seeds both lists on first read.
    rows = int(
        conn.execute(sa.text("SELECT COUNT(*) FROM list_config")).scalar_one() or 0
    )
    policies = int(conn.execute(sa.text("SELECT COUNT(*) FROM policy")).scalar_one() or 0)
    if not rows and not policies:
        return

    has_tag_list = int(
        conn.execute(
            sa.text("SELECT COUNT(*) FROM list_config WHERE source = 'arr_tag'")
        ).scalar_one()
        or 0
    )
    if not has_tag_list:
        tags, match = _stored_keep_tags(conn)
        if tags:
            taken = {
                str(r[0]).casefold()
                for r in conn.execute(sa.text("SELECT name FROM list_config")).fetchall()
            }
            name = _TAG_LIST_NAME
            if name.casefold() in taken:
                name = f"{_TAG_LIST_NAME} (tags)"
            if name.casefold() not in taken:
                conn.execute(
                    sa.text(
                        "INSERT INTO list_config "
                        "(name, source, config_json, enabled, built_in, created_at) "
                        "VALUES (:name, 'arr_tag', :config, 1, 0, :now)"
                    ),
                    {
                        "name": name,
                        "config": json.dumps({"tags": tags, "match": match}),
                        # An INTEGER unix timestamp: EpochDateTime's storage form. Binding a
                        # datetime through raw SQL lands an ISO string the ORM raises on --
                        # the defect the previous seed migration shipped and then pinned.
                        "now": int(datetime.now(UTC).timestamp()),
                    },
                )

    # 4. The defaults are considered seeded: this install has its own rows now, and the
    # first read must not add a second shipped copy beside them.
    now = int(datetime.now(UTC).timestamp())
    existing = conn.execute(
        sa.text("SELECT COUNT(*) FROM app_setting WHERE key = 'lists_seeded'")
    ).scalar_one()
    if int(existing or 0):
        conn.execute(
            sa.text("UPDATE app_setting SET value_json = 'true' WHERE key = 'lists_seeded'")
        )
    else:
        conn.execute(
            sa.text(
                "INSERT INTO app_setting (key, value_json, updated_at) "
                "VALUES ('lists_seeded', 'true', :now)"
            ),
            {"now": now},
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE list_config SET source = 'curated', config_json = :config "
            "WHERE source = 'imdb'"
        ),
        {"config": json.dumps({"list": "imdb-top-250"})},
    )
    # The seeded tag list and the flag were this migration's own writes; the tag rows an
    # operator authored under the new UI are indistinguishable from the seed by now, so the
    # conservative downgrade keeps them all and removes only the flag.
    conn.execute(sa.text("DELETE FROM app_setting WHERE key = 'lists_seeded'"))
