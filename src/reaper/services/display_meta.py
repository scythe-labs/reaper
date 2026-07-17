# SPDX-License-Identifier: AGPL-3.0-or-later
"""Display metadata frozen onto a candidate at scan time.

Resolution, certification, runtime and external ratings are **presentation** fields:
the review queue and the why-panel draw them around a verdict, and none of them ever
participates in one. They still follow the codebase's provenance rules -- a rating is
stored only when its source is known (see ``reaper.ratings``), and the IMDb number
stored here is the *same dataset entry* the scoring signal froze into ``Facts``, so
the panel can never show one IMDb value beside a signal that used another.

Source priority is Plex first, then the *arrs -- Plex is the server the operator
actually fronts, and its section listing carries all of this for free during the
sweep the scan already runs.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from reaper.ratings import Rating, RatingSource

if TYPE_CHECKING:
    from reaper.services.imdb_dataset import ImdbRating

#: The sources the ratings row displays, and therefore the ones worth freezing.
_STORED_SOURCES = frozenset(
    {
        RatingSource.IMDB,
        RatingSource.TMDB,
        RatingSource.ROTTEN_TOMATOES_CRITIC,
        RatingSource.ROTTEN_TOMATOES_AUDIENCE,
        RatingSource.TRAKT,
    }
)

#: Substring -> canonical resolution, checked in order so "2160" wins over "160".
_QUALITY_MARKERS: tuple[tuple[str, str], ...] = (
    ("2160", "2160"),
    ("1080", "1080"),
    ("720", "720"),
    ("576", "576"),
    ("480", "480"),
    ("sdtv", "sd"),
    ("dvd", "sd"),
)


def normalize_resolution(plex_value: str | None, quality_name: str | None) -> str | None:
    """One canonical resolution string ("2160", "1080", ..., "sd") or ``None``.

    Plex's ``videoResolution`` first (it describes the file Plex actually serves),
    falling back to parsing the *arr's quality name (e.g. "Bluray-1080p"). Anything
    unrecognisable is ``None`` -- the badge is hidden, never guessed.
    """
    if plex_value:
        value = plex_value.strip().lower()
        if value == "4k":
            return "2160"
        if value in {"2160", "1080", "720", "576", "480", "sd"}:
            return value

    if quality_name:
        lowered = quality_name.lower()
        for marker, canonical in _QUALITY_MARKERS:
            if marker in lowered:
                return canonical

    return None


def dataset_entry(imdb: dict[str, ImdbRating], *imdb_ids: str | None) -> ImdbRating | None:
    """The IMDb dataset row for the first id that resolves, or ``None``.

    The single lookup shared by ``build_facts`` (which freezes it into the scoring
    signal) and ``build_ratings_json`` (which freezes it into the display row), so
    the two can never drift apart.
    """
    for imdb_id in imdb_ids:
        if imdb_id:
            entry = imdb.get(imdb_id)
            if entry is not None:
                return entry
    return None


def build_ratings_json(
    dataset: ImdbRating | None,
    plex_ratings: Sequence[Rating] = (),
    arr_ratings: Sequence[Rating] = (),
) -> str | None:
    """The frozen ratings row, as canonical JSON, or ``None`` when nothing is known.

    Integers only (floats do not canonicalise): every value is stored as
    ``round(value_on_the_0_to_10_scale * 10)`` -- IMDb 5.9 -> 59, an RT 77% -> 77,
    TMDb 6.1 -> 61 -- plus ``imdb_votes`` when the IMDb source counts votes.

    Priority per source: the IMDb dataset entry outranks everything (it is what the
    score used); then Plex; then the *arr fills what is still missing.
    """
    out: dict[str, int] = {}

    if dataset is not None:
        out[RatingSource.IMDB.value] = round(dataset.average_rating * 10)
        out["imdb_votes"] = int(dataset.num_votes)

    for rating in [*plex_ratings, *arr_ratings]:
        if rating.source not in _STORED_SOURCES or rating.source.value in out:
            continue
        out[rating.source.value] = round(rating.value * 10)
        if rating.source is RatingSource.IMDB and rating.votes:
            out["imdb_votes"] = int(rating.votes)

    if not out:
        return None
    return json.dumps(out, sort_keys=True)


def parse_ratings_json(text: str | None) -> dict[str, int]:
    """Decode a stored ratings row back to its int map. Defensive: anything that is
    not the dict of ints :func:`build_ratings_json` writes comes back empty, so an
    old or hand-edited row degrades to "no ratings shown", never an error."""
    if not text:
        return {}
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): int(v) for k, v in raw.items() if isinstance(v, int | float)}
