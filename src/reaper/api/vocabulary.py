# SPDX-License-Identifier: AGPL-3.0-or-later
"""The field and value lists the policy editor's own pickers are built from.

Read-only. This is its own module, separate from the editor, because what a lane
offers is derived from ``engine/fields.py``. It answers the same question for the
policy editor, the keep rules, and the simulator, so none of them holds its own copy.
"""

from __future__ import annotations

import json
from collections import Counter

from fastapi import APIRouter, Request
from sqlalchemy import select

from reaper.api import tags as api_tags
from reaper.api.deps import newest_snapshot, session_factory
from reaper.api.schemas import (
    FieldOut,
    FieldValuesOut,
    VocabularyOut,
)
from reaper.db.models import (
    Candidate,
)
from reaper.engine.fields import Lane, MediaType, vocabulary

router = APIRouter(prefix="/api")


@router.get("/vocabulary", tags=[api_tags.POLICY])
async def get_vocabulary(lane: Lane, media_type: MediaType | None = None) -> VocabularyOut:
    """List the fields available in one lane, for one policy's media type.

    Filtering happens server-side, before serialization. ``?lane=condemn`` never
    returns a protect-only field, so the browser cannot even build a control for one.
    ``&media_type=movie`` narrows it further, so a TV-only field like "the show has
    ended" is not offered on a movie policy. Omitting ``media_type`` keeps every field,
    so older callers see no change.
    """
    return VocabularyOut(
        lane=lane,
        fields=[
            FieldOut(key=spec.key, type=spec.type, ops=list(spec.ops))
            for spec in vocabulary(lane, media_type)
        ],
    )


#: The fields whose seen-values are worth suggesting, and the candidate column each is
#: read from. Free-text fields only. Numbers and booleans need no suggestions.
_VALUE_COLUMNS = {
    "genre": Candidate.genres_json,
    "collection": Candidate.collections_json,
    "quality": Candidate.quality,
    "library": Candidate.library_title,
}

#: How many ranked values a suggestion list carries. One shared ceiling rather than a per-field
#: table nobody would remember to extend for the next JSON-array field.
#:
#: A real library with 387 distinct Plex collections showed a cap of 200 would silently
#: drop about half of them (see ``docs/LEARNINGS.md``). The list ranks by how many
#: titles carry the value, so a truncation drops the rarest values first, which on a
#: collection field is often the specific one the operator went looking for. If a
#: library outgrows this cap too, give the picker type-to-search rather than raising
#: the number again. Past some size, a truncated list stops warning the operator that
#: it is incomplete.
_MAX_VALUES = 2000


@router.get("/vocabulary/values", tags=[api_tags.POLICY])
async def vocabulary_values(request: Request, field: str) -> FieldValuesOut:
    """List the distinct values the latest scan saw for one field, most common first.

    This powers the rule editors' input suggestions, such as "Documentary" or
    "Bluray-1080p". An unknown field, or no scan yet, returns an empty list instead of
    an error, since a suggestion box with nothing to suggest is still a working input.
    Typing any value stays valid either way.
    """
    column = _VALUE_COLUMNS.get(field)
    if column is None:
        return FieldValuesOut(field=field, values=[])

    async with session_factory(request)() as session:
        snapshot = await newest_snapshot(session)
        if snapshot is None:
            return FieldValuesOut(field=field, values=[])
        raws = (
            (
                await session.execute(
                    select(column).where(Candidate.snapshot_id == snapshot.id, column.is_not(None))
                )
            )
            .scalars()
            .all()
        )

    counts: Counter[str] = Counter()
    for raw in raws:
        if raw is None:  # Already filtered in SQL. Repeated here for the type checker.
            continue
        # Both JSON-array columns need the same parse. This checks the COLUMN itself,
        # not the field name, so adding a third JSON-array field only needs a
        # `_VALUE_COLUMNS` entry.
        if column is Candidate.genres_json or column is Candidate.collections_json:
            # A row that does not parse contributes nothing.
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                continue
            counts.update(str(g) for g in parsed if g)
        else:
            counts.update([raw])

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return FieldValuesOut(field=field, values=[value for value, _ in ranked[:_MAX_VALUES]])
