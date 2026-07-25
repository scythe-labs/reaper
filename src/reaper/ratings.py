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

Three further facts, all measured:

* **Vote counts are mandatory.** A 9.5 from 12 votes is noise. Radarr reports
  ``votes: 0`` for Rotten Tomatoes and Metacritic (they are percentages, not vote
  averages), so a vote floor must apply only to sources that actually have votes.
* **Radarr's ``type`` field lies.** It reports ``"user"`` for Rotten Tomatoes on a
  value that is plainly the critic Tomatometer (96). Do not trust it.
* **Scale is a property of the provider, not the source.** Plex serves *every*
  rating slot on a 0-10 scale whatever the source: a 96% Tomatometer arrives as
  ``9.6``, never ``96``. A follow-up probe of a live server (2026-07-16) swept
  every movie and show section and found thousands of rating values across
  ``imdb://``, ``themoviedb://`` and ``rottentomatoes://`` images with **not one
  above 10** -- including Rotten Tomatoes values that Plex's own UI displays as
  percentages. Radarr hands the very same score through raw (``96``). The same
  number therefore needs opposite handling depending on who delivered it, so
  normalization happens per provider, never per field name.
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


def is_percentage_source(source: RatingSource) -> bool:
    """Whether a source is a 0-100 percentage (Rotten Tomatoes, Metacritic) rather than a
    0-10 average. The UI reads this to show a ``%`` box with no vote floor for these, and a
    ``/10`` box with a vote floor for IMDb/TMDb."""
    return source in _PERCENTAGE_SOURCES


#: Operator-facing names for each source. Never a raw enum value or an id in the UI.
SOURCE_LABELS: dict[RatingSource, str] = {
    RatingSource.IMDB: "IMDb",
    RatingSource.TMDB: "TMDb",
    RatingSource.ROTTEN_TOMATOES_CRITIC: "Rotten Tomatoes critics",
    RatingSource.ROTTEN_TOMATOES_AUDIENCE: "Rotten Tomatoes audience",
    RatingSource.METACRITIC: "Metacritic",
    RatingSource.TRAKT: "Trakt",
    RatingSource.TVDB: "TVDB",
    RatingSource.UNKNOWN: "an unknown source",
}


def source_label(source: RatingSource) -> str:
    return SOURCE_LABELS.get(source, source.value)


@dataclass(frozen=True)
class Rating:
    """A rating, with the provenance required to interpret it."""

    source: RatingSource
    value: float
    """Normalized to 0-10, whatever scale the source natively uses."""
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

    def describe_for_user(self) -> str:
        """Plain-language form for operator-facing copy: no id, no provider, native scale.

        Percentage sources read as a percentage (``Rotten Tomatoes critics 84%``); the 0-10
        sources read on their own scale with the vote count that makes the number mean
        something (``8.2 on IMDb from 120,000 votes``).
        """
        label = source_label(self.source)
        if self.source in _PERCENTAGE_SOURCES:
            return f"{label} {round(self.value * 10)}%"
        votes = f" from {self.votes:,} votes" if self.votes else ""
        return f"{self.value:.1f} on {label}{votes}"


def _to_ten(value: float, source: RatingSource) -> float:
    """Radarr-shaped normalization: its percentage sources arrive raw (96, not 9.6)."""
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

    Values arrive already normalized: Plex serves every rating slot on a 0-10
    scale whatever the source (measured -- see the module docstring), so an 84%
    Rotten Tomatoes score is ``"8.4"`` here. Raw percentages are a Radarr shape,
    handled in :func:`from_radarr`, never divided out of a Plex value.
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

    # Plex's contract is 0-10 for every slot, so the value passes through
    # undivided: dividing a Tomatometer here (as Radarr's raw percentages need)
    # turned an 84% audience score into 0.84, displayed as 8%. Defensively, a
    # percentage-shaped source above 10 can only be a raw percentage from an
    # agent that skipped Plex's normalization -- the value itself proves the scale.
    if number > 10 and source in _PERCENTAGE_SOURCES:
        number /= 10.0
    if not 0.0 <= number <= 10.0:
        # Outside every scale we know how to read. A number we cannot interpret
        # must not protect, condemn, or be displayed as if it meant something.
        return None

    return Rating(source=source, value=number, votes=None, provider=provider)


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

        value_on_ten = _to_ten(number, source)
        if not 0.0 <= value_on_ten <= 10.0:
            # A 0-10 average above 10, a percentage above 100: outside every
            # scale we know how to read, so it is dropped rather than guessed at.
            continue

        # Radarr reports votes: 0 for percentage sources. Zero is "no vote
        # concept", not "zero people voted" -- conflating them would make a vote
        # floor reject every Rotten Tomatoes score.
        raw_votes = entry.get("votes")
        try:
            votes = int(raw_votes) if raw_votes and source not in _PERCENTAGE_SOURCES else None
        except (TypeError, ValueError):
            # A fork, a proxy, or a future schema serializing votes as "1,234" or a list
            # must cost this one rating, never the operator's whole scan: a bare int() here
            # raised straight out of the fact build (rule 32). None already fails a vote
            # floor closed in ``Rating.meets``, so the safe reading is preserved.
            votes = None

        out.append(
            Rating(
                source=source,
                value=value_on_ten,
                votes=votes,
                provider=provider,
            )
        )
    return out


def pick(ratings: list[Rating], source: RatingSource) -> Rating | None:
    return next((r for r in ratings if r.source is source), None)


def merge_by_source(*groups: list[Rating] | tuple[Rating, ...]) -> tuple[Rating, ...]:
    """One rating per source, first writer wins, UNKNOWN dropped.

    The scan holds several rating lists for one item (the IMDb dataset value, Radarr's
    ``ratings`` object, Plex's two slots). They overlap: a film can carry an IMDb score
    from both the dataset and Radarr. Pass the groups in **precedence order** (most
    authoritative first, e.g. the dataset's IMDb, then Radarr, then Plex) and the first
    rating seen for a source wins. An ``UNKNOWN``-source rating is never admitted, so a
    protection can never be decided on a number we cannot interpret.
    """
    out: dict[RatingSource, Rating] = {}
    for group in groups:
        for rating in group:
            if rating.source is RatingSource.UNKNOWN:
                continue
            out.setdefault(rating.source, rating)
    return tuple(out.values())
