# SPDX-License-Identifier: AGPL-3.0-or-later
"""The field and value lists the policy editor's own pickers are built from.

Read-only, and the reason it is its own module rather than part of the editor: what a
lane offers is derived from ``engine/fields.py``, so it answers the same question for the
policy editor, the keep rules and the simulator without any of them holding a copy
(rule 66).
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
    """The fields available in one lane, for one policy's media type.

    Filtered **server-side, before serialization**. ``?lane=condemn`` never returns a
    protect-only field, so the browser is not even shown one -- a dangerous condition is
    not merely rejected, it is unconstructable. ``&media_type=movie`` narrows it further:
    a TV-only field like "the show has ended" is not offered on a movie policy. Omitting
    ``media_type`` keeps every field, so older callers are unchanged.
    """
    return VocabularyOut(
        lane=lane,
        fields=[
            FieldOut(key=spec.key, type=spec.type, ops=list(spec.ops))
            for spec in vocabulary(lane, media_type)
        ],
    )


#: The fields whose seen-values are worth suggesting, and the candidate column each is
#: read from. Free-text fields only: numbers and booleans need no suggestions.
_VALUE_COLUMNS = {
    "genre": Candidate.genres_json,
    "collection": Candidate.collections_json,
    "quality": Candidate.quality,
    "library": Candidate.library_title,
}

#: How many ranked values a suggestion list carries. One shared ceiling rather than a per-field
#: table nobody would remember to extend for the next JSON-array field.
#:
#: **Measured rather than guessed, and the first guess was too low.** A real library of 5,984
#: scanned items carries 387 distinct Plex collections against tens of genres (#816, recorded in
#: `docs/LEARNINGS.md`), so the 200 this shipped with would have dropped roughly half of them
#: with nothing saying so. The list is ranked by how many titles carry the value, so a truncation
#: takes the rarest, which on a collection set is the specific shelf the operator went looking
#: for. Raised rather than making the picker type-to-search, which is frontend work this change
#: does not otherwise touch; past this a truncation still says nothing, and that is the fix the
#: next library to outgrow it should get instead of another number here.
_MAX_VALUES = 2000


@router.get("/vocabulary/values", tags=[api_tags.POLICY])
async def vocabulary_values(request: Request, field: str) -> FieldValuesOut:
    """Distinct values the latest scan actually saw for one field, most common first.

    Powers the rule editors' input suggestions ("Documentary", "Bluray-1080p", ...).
    Deliberately fail-open-to-empty: an unknown field, or no scan yet, returns an empty
    list rather than an error, because a suggestion box with nothing to suggest is still
    a working input -- typing any value remains valid either way.
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
        if raw is None:  # filtered in SQL; repeated here for the type-checker
            continue
        # Both JSON-array columns need the same parse; keyed off the COLUMN, not the field
        # name, so a third JSON-array field only ever needs a `_VALUE_COLUMNS` entry.
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
