# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ratings, and the provenance that makes them meaningful.

**Never assume what a rating field contains. Read its provenance.**

The published guidance says Plex's ``rating`` / ``audience_rating`` on a modern
library are Rotten Tomatoes scores. Probing a real server on 2026-07-14 found the
opposite: across every movie sampled, ``rating_image`` was *empty* and
``audience_rating_image`` was ``imdb://image.rating`` -- so ``audience_rating`` was
an **IMDb** score, and there was no Rotten Tomatoes value at all.

Both shapes exist in the wild; it depends on the Plex agent the library uses. A
rule that wired an "IMDb threshold" to a field holding a Tomatometer percentage
would compare 7.5 against 96 and protect nothing, silently, forever. So the source
is *read from the data*, never inferred from the field name.

The corollary for the UI: a score is never displayed as a bare number. It is
displayed with where it came from and when, so a wrong source is visible rather
than merely wrong.

Two further facts, both measured:

* **Vote counts are mandatory.** A 9.5 from 12 votes is noise. Radarr reports
  ``votes: 0`` for Rotten Tomatoes and Metacritic (they are percentages, not vote
  averages), so a vote floor must apply only to sources that actually have votes.
* **Radarr's ``type`` field lies.** It reports ``"user"`` for Rotten Tomatoes on a
  value that is plainly the critic Tomatometer (96). Do not trust it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class RatingSource(enum.StrEnum):
    IMDB = "imdb"
    TMDB = "tmdb"
    ROTTEN_TOMATOES_CRITIC = "rotten_tomatoes_critic"
    ROTTEN_TOMATOES_AUDIENCE = "rotten_tomatoes_audience"
    METACRITIC = "metacritic"
    TRAKT = "trakt"
    TVDB = "tvdb"
    UNKNOWN = "unknown"


# Plex encodes provenance in *_rating_image, e.g. "imdb://image.rating" or
# "rottentomatoes://image.rating.ripe". This is the only reliable signal.
_PLEX_IMAGE_PREFIXES: dict[str, RatingSource] = {
    "imdb": RatingSource.IMDB,
    "rottentomatoes": RatingSource.ROTTEN_TOMATOES_CRITIC,
    "themoviedb": RatingSource.TMDB,
    "tmdb": RatingSource.TMDB,
}

# Sources that are percentages (0-100), not 0-10 averages, and carry no vote count.
_PERCENTAGE_SOURCES = frozenset(
    {
        RatingSource.ROTTEN_TOMATOES_CRITIC,
        RatingSource.ROTTEN_TOMATOES_AUDIENCE,
        RatingSource.METACRITIC,
    }
)


@dataclass(frozen=True)
class Rating:
    """A rating, with the provenance required to interpret it."""

    source: RatingSource
    value: float
    """Normalised to 0-10, whatever scale the source natively uses."""
    votes: int | None
    """None where the source has no vote concept (Rotten Tomatoes, Metacritic)."""
    provider: str
    """Which integration handed us this -- 'radarr', 'plex', 'imdb-dataset'."""
    as_of: datetime | None = None

    @property
    def has_meaningful_vote_count(self) -> bool:
        """A vote floor only applies to sources that actually count votes."""
        return self.source not in _PERCENTAGE_SOURCES

    def meets(self, floor: float, *, min_votes: int = 0) -> bool:
        """Does this rating clear a protection threshold?

        Fails *closed* on an unknown source: we would rather keep a file than
        delete it on the strength of a number we cannot interpret.
        """
        if self.source is RatingSource.UNKNOWN:
            return False
        if (
            self.has_meaningful_vote_count
            and min_votes > 0
            and (self.votes is None or self.votes < min_votes)
        ):
            return False
        return self.value >= floor

    def describe(self) -> str:
        """The string the why-panel shows. Provenance is not optional."""
        votes = f" from {self.votes:,} votes" if self.votes else ""
        return f"{self.source.value} {self.value:.1f}/10{votes} (via {self.provider})"


def _to_ten(value: float, source: RatingSource) -> float:
    return value / 10.0 if source in _PERCENTAGE_SOURCES else value


def from_plex(
    value: str | float | None,
    image: str | None,
    *,
    provider: str = "plex",
    audience: bool = False,
) -> Rating | None:
    """Read a Plex ``rating`` / ``audience_rating`` pair.

    ``image`` is load-bearing: it is what tells us whether the number is IMDb,
    Rotten Tomatoes or TMDb. Without it the value is uninterpretable, and an
    uninterpretable rating must not be used to justify a deletion -- so we return
    None rather than guessing.

    ``audience=True`` marks the value as coming from the ``audience_rating`` slot, so
    a Rotten Tomatoes image resolves to the audience score, not the Tomatometer --
    the prefix map alone cannot tell them apart, since both arrive as
    ``rottentomatoes://image.rating.*``. (An IMDb or TMDb image in that slot is
    still just an IMDb/TMDb value; only RT keeps two distinct populations.)
    """
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    source = RatingSource.UNKNOWN
    if image:
        scheme = str(image).split("://", 1)[0].lower()
        source = _PLEX_IMAGE_PREFIXES.get(scheme, RatingSource.UNKNOWN)
        if audience and source is RatingSource.ROTTEN_TOMATOES_CRITIC:
            source = RatingSource.ROTTEN_TOMATOES_AUDIENCE

    if source is RatingSource.UNKNOWN:
        # We know a number but not what it means. Unknown may only protect,
        # never condemn -- so it is dropped rather than guessed at.
        return None

    return Rating(source=source, value=_to_ten(number, source), votes=None, provider=provider)


def from_radarr(ratings: dict[str, Any] | None, *, provider: str = "radarr") -> list[Rating]:
    """Read Radarr's ``ratings`` object.

    Measured coverage over a full production Radarr library: IMDb ~99%, Rotten
    Tomatoes ~88%, Metacritic ~85%. So for movies these are essentially free and
    essentially complete -- no extra API key and no extra request, since we already
    fetch this payload for other reasons.

    Radarr's ``type`` field is ignored: it reports ``"user"`` for a Rotten Tomatoes
    value of 96, which is unambiguously the critic Tomatometer.
    """
    if not isinstance(ratings, dict):
        return []

    mapping: dict[str, RatingSource] = {
        "imdb": RatingSource.IMDB,
        "tmdb": RatingSource.TMDB,
        "metacritic": RatingSource.METACRITIC,
        "rottenTomatoes": RatingSource.ROTTEN_TOMATOES_CRITIC,
        "trakt": RatingSource.TRAKT,
    }

    out: list[Rating] = []
    for key, source in mapping.items():
        entry = ratings.get(key)
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue

        # Radarr reports votes: 0 for percentage sources. Zero is "no vote
        # concept", not "zero people voted" -- conflating them would make a vote
        # floor reject every Rotten Tomatoes score.
        raw_votes = entry.get("votes")
        votes = int(raw_votes) if raw_votes and source not in _PERCENTAGE_SOURCES else None

        out.append(
            Rating(
                source=source,
                value=_to_ten(number, source),
                votes=votes,
                provider=provider,
            )
        )
    return out


def pick(ratings: list[Rating], source: RatingSource) -> Rating | None:
    return next((r for r in ratings if r.source is source), None)
