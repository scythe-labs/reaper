# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every list is Arr-style now. The registry gains the keep tags and the IMDb variants.

This migration makes three additive data moves. It changes no schema.

* ``source = 'curated'`` rows become ``'imdb'`` with a ``preset`` config, the spelling the
  generalized IMDb provider reads. The operator's own name for the list survives.
* The policy keep tags become a tag list named on Settings -> Lists, seeded from the
  newest stored policy bodies. Both media types' tags are unioned and matched ANY, unless
  both bodies named the same tags under ALL. A union can only be the wider read under ANY,
  since ALL would require each title to carry both policies' tags (see
  ``_stored_keep_tags``). The other half of this move, the ``on_list`` rules that make the
  list act, is ``engine.policy_migrations.convert_list_protections``, which runs on load
  and is reviewed and saved by the operator. This migration only makes sure the list those
  rules name exists.
* ``lists_seeded`` is set whenever any definition exists, so
  ``list_config.ensure_defaults`` does not add a second, shipped copy beside an upgraded
  install's own rows.

``built_in`` clears and ``enabled`` sets on every row. The Protecting switch left the UI,
since a list now acts through its keep rules, and every list is removable since its rules
leave with it. A disabled row would otherwise render with no control that can re-enable it.
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
    """Return the keep tags the newest stored policy of each media type carries, unioned.

    ``all`` survives only when both bodies said ``all`` and named the same tags. The old
    keep tags were scoped per policy and per service: a movie carrying the movie policy's
    tag was on its keep list no matter what the tv policy said. One list now replaces both.
    Under ALL, membership means ``wanted <= carried`` (``services.lists.ArrTagRule``), so
    unioning two different tag sets under ALL would require every title to carry both
    policies' tags, and a movie carrying only the movie tag would drop off the list the
    union was meant to widen. ANY is the only union that cannot withdraw cover from a
    title either policy was keeping.

    A single stored policy is left exactly as it was, since there is no second set to
    disagree with, so its own match mode carries over. A body with no keep_tags key ran
    on the shipped default tag.
    """
    tags: list[str] = []
    per_body: list[frozenset[str]] = []
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
        own: set[str] = set()
        if isinstance(raw, list):
            for tag in raw:
                spelled = str(tag).strip()
                if not spelled:
                    continue
                own.add(spelled.casefold())
                if spelled.casefold() not in {t.casefold() for t in tags}:
                    tags.append(spelled)
        per_body.append(frozenset(own))
        matches.append("all" if body.get("keep_tags_match") == "all" else "any")
    if not spoke:
        tags = ["reaper-keep"]
    # Compared case-folded, the same comparison every reader of a tag makes, and the one
    # Sonarr and Radarr themselves make. "Keep" and "keep" are one tag there.
    agreed = len(set(per_body)) <= 1
    match = "all" if matches and all(m == "all" for m in matches) and agreed else "any"
    return tags, match


def upgrade() -> None:
    conn = op.get_bind()

    # 1. The IMDb respelling. The old config body named which shipped list. Only the Top
    # 250 ever existed, so every 'curated' row is it.
    conn.execute(
        sa.text(
            "UPDATE list_config SET source = 'imdb', config_json = :config "
            "WHERE source = 'curated'"
        ),
        {"config": json.dumps({"preset": "top250"})},
    )
    # 2. Every list is removable and always on. The switch and the built-in lock left the UI.
    conn.execute(sa.text("UPDATE list_config SET built_in = 0, enabled = 1"))

    # 3. The keep tags become a list, for installs that already have stored policies or
    # list definitions to carry forward. A brand new database leaves the flag unset, and
    # `ensure_defaults` seeds both lists on first read.
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
                        # An integer unix timestamp, EpochDateTime's storage form. Binding
                        # a Python datetime through raw SQL would land an ISO string
                        # instead, which the ORM later raises on.
                        "now": int(datetime.now(UTC).timestamp()),
                    },
                )

    # 4. The IMDb list, added when none exists. The default policy's keep rule names
    # "IMDb Top 250", and this migration sets the seeded flag below. Skipping this step
    # would leave an install whose only row is the previous migration's "Never Reap" seed
    # with a rule naming a list that does not exist.
    has_imdb = int(
        conn.execute(
            sa.text("SELECT COUNT(*) FROM list_config WHERE source = 'imdb'")
        ).scalar_one()
        or 0
    )
    if not has_imdb:
        taken = {
            str(r[0]).casefold()
            for r in conn.execute(sa.text("SELECT name FROM list_config")).fetchall()
        }
        if "imdb top 250" not in taken:
            conn.execute(
                sa.text(
                    "INSERT INTO list_config "
                    "(name, source, config_json, enabled, built_in, created_at) "
                    "VALUES ('IMDb Top 250', 'imdb', :config, 1, 0, :now)"
                ),
                {
                    "config": json.dumps({"preset": "top250"}),
                    "now": int(datetime.now(UTC).timestamp()),
                },
            )

    # 5. The defaults are now considered seeded. This install has its own rows, and the
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
    # The seeded tag list and the flag were this migration's own writes. By now, the tag
    # rows an operator authored under the new UI are indistinguishable from the seed, so
    # this conservative downgrade keeps them all and removes only the flag.
    conn.execute(sa.text("DELETE FROM app_setting WHERE key = 'lists_seeded'"))
