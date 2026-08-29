# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ratings, and the provenance that makes them meaningful.

Never assume what a rating field contains. Read its provenance instead.

Plex's published guidance says its ``rating`` and ``audience_rating`` fields hold
Rotten Tomatoes scores on a modern library. A live probe found the opposite:
every sampled movie had an empty ``rating_image`` and an
``audience_rating_image`` of ``imdb://image.rating``, so ``audience_rating``
held an IMDb score and no Rotten Tomatoes value existed at all.

Both shapes exist in the wild, depending on the Plex agent the library uses. A
rule that wired an "IMDb threshold" to a field actually holding a Tomatometer
percentage would compare 7.5 against 96 and silently protect nothing, forever.
So the source is read from the data, never inferred from the field name.

The UI never shows a bare number for the same reason: it always shows where a
score came from and when, so a wrong source is visible instead of just wrong.

Three more measured facts:

* Vote counts are mandatory. A 9.5 from 12 votes is noise. Radarr reports
  ``votes: 0`` for Rotten Tomatoes and Metacritic, because they are
  percentages, not vote averages, so a vote floor must apply only to sources
  that actually count votes.
* Radarr's ``type`` field is unreliable. It reports ``"user"`` for a Rotten
  Tomatoes value that is plainly the critic Tomatometer (96).
* Scale is a property of the provider, not the source. Plex serves every
  rating slot on a 0-10 scale no matter the source: a 96% Tomatometer arrives
  as ``9.6``, never ``96``. Radarr hands the same score through raw, as
  ``96``. So the same number needs opposite handling depending on who
  delivered it, and normalization happens per provider, never per field name.
  See ``from_plex`` and docs/LEARNINGS.md for how this was measured.
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


#: Operator-facing names for each source. Never shown as a raw enum value or an id.
#:
#: Every label is written to follow a preposition (``on {label}``, ``no rating on
#: {label}``), which is why ``UNKNOWN`` reads as "an unknown source" rather than
#: just "unknown source". Putting a label after an article like "a" or "an"
#: instead can produce a mismatch, such as "A IMDb bar" or "A an unknown source
#: bar". Placing a label after a preposition avoids that, since the preposition
#: never disagrees with the label's own grammar. ``test_engine_invariants``
#: drives every member through the sentences that render one.
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
    """Which integration provided this rating: 'radarr', 'plex', 'imdb-dataset'."""
    as_of: datetime | None = None

    @property
    def has_meaningful_vote_count(self) -> bool:
        """A vote floor only applies to sources that actually count votes."""
        return self.source not in _PERCENTAGE_SOURCES

    def short_of_vote_floor(self, min_votes: int) -> bool:
        """Whether a vote floor rules out this rating. The one place that question is
        answered.

        Two callers ask it: ``meets``, which decides whether a bar is cleared, and
        ``engine.gates.RatingFloorGate._miss_phrase``, which tells the operator why
        it was not. Both need to agree on the same edge cases: ``0`` means no floor
        at all, ``1`` is the smallest floor ``engine.policy.RatingRuleSpec`` accepts
        on a source that counts votes, and a count exactly at the floor clears it.

        A missing vote count always falls short of the floor. ``from_plex`` returns
        ``votes=None`` for every Plex-sourced rating, so on a Plex-only library
        that is the ordinary case, not an edge case, and it resolves toward not
        protecting the item.
        """
        return (
            self.has_meaningful_vote_count
            and min_votes > 0
            and (self.votes is None or self.votes < min_votes)
        )

    def meets(self, floor: float, *, min_votes: int = 0) -> bool:
        """Does this rating clear a protection threshold?

        Fails *closed* on an unknown source: we would rather keep a file than
        delete it on the strength of a number we cannot interpret.
        """
        if self.source is RatingSource.UNKNOWN:
            return False
        if self.short_of_vote_floor(min_votes):
            return False
        return self.value >= floor

    def describe(self) -> str:
        """Form for a log line or a debugger, with full provenance. No UI renders this.

        The why-panel's rating strings come from ``describe_for_user``, and the
        stored projection comes from ``services.display_meta.build_ratings_json``.
        """
        votes = describe_votes(self.votes)
        return f"{self.source.value} {self.value:.1f}/10{votes} (via {self.provider})"

    def describe_for_user(self) -> str:
        """Plain-language form for operator-facing copy, with no id, no provider name,
        and the source's native scale.

        A percentage source reads as a percentage (``Rotten Tomatoes critics
        84%``). A 0-10 source reads on its own scale with the vote count that
        makes the number mean something (``8.2 on IMDb from 120,000 votes``).
        """
        label = source_label(self.source)
        if self.source in _PERCENTAGE_SOURCES:
            return f"{label} {round(self.value * 10)}%"
        return f"{self.value:.1f} on {label}{describe_votes(self.votes)}"


def describe_votes(count: int | None) -> str:
    """The vote clause an operator reads, or nothing at all: `` from 1 vote``.

    The one place this phrase is built, for the two callers that render a count a
    title actually has: ``Rating.describe`` and ``Rating.describe_for_user``. It
    handles the singular case ("from 1 vote") separately from the plural ("from
    12 votes"), since a title with a single vote is ordinary, not an edge case.

    ``engine.gates.RatingRule.describe_bar`` builds its own version for a vote
    *floor* rather than a count, since a floor's clause carries a "+" (as in
    "from 1,000+ votes").

    A falsy count (``None`` or ``0``) returns the empty string, since there is
    no honest clause to print. "from 0 votes" would read as a measurement
    rather than as an absence.
    """
    if not count:
        return ""
    return f" from {count:,} vote" if count == 1 else f" from {count:,} votes"


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

    ``image`` decides everything: it says whether the number is IMDb, Rotten
    Tomatoes, or TMDb. Without it the value cannot be interpreted, and an
    uninterpretable rating must never justify a deletion, so this returns
    ``None`` instead of guessing.

    ``audience=True`` marks the value as coming from the ``audience_rating``
    slot, so a Rotten Tomatoes image resolves to the audience score instead of
    the Tomatometer. The prefix map alone cannot tell them apart, since both
    arrive as ``rottentomatoes://image.rating.*``. An IMDb or TMDb image in
    that slot is still just an IMDb or TMDb value. Only Rotten Tomatoes keeps
    two distinct populations.

    Values arrive already normalized: Plex serves every rating slot on a 0-10
    scale no matter the source (see the module docstring), so an 84% Rotten
    Tomatoes score arrives here as ``"8.4"``. Raw percentages are a Radarr
    shape, handled in :func:`from_radarr`. One exception below rescales a Plex
    value anyway, for a percentage-shaped source arriving above 10. What that
    is for, and where it is not known to be right, is written at that branch.
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
        # A number with no known source might mean anything, so it must never
        # count toward deletion. It is dropped instead of guessed at.
        return None

    # Plex already normalizes every slot to 0-10, so a value at or below 10
    # passes through unchanged. Dividing it here, the way Radarr's raw
    # percentages need, would turn an 84% audience score (already 8.4) into
    # 0.84, displayed as 8%. Above 10 the contract is already broken. This
    # branch rescales anyway, on the reasoning that a percentage-shaped source
    # reporting above 10 can only be a raw percentage from an agent that
    # skipped Plex's own normalization: the value itself reveals the scale.
    #
    # Correct for the values seen so far (84, 96, 100), and for a genuinely low
    # Tomatometer, where 11 means 11% and 1.1 is the right reading. Not
    # confirmed correct just above the boundary. 10.1 becomes 1.01 here, and
    # the panel then shows "10%" for a title Plex reported as 10.1. A
    # competing reading says a value just over 10 is really a 0-10 score with
    # a rounding error and is out of contract, the same way a non-percentage
    # source at 10.1 is already dropped a few lines down, where the panel
    # shows no rating rather than a number nothing produced. Both readings
    # withdraw the protection either way, since neither can turn into a false
    # positive. They only differ in what number the operator sees.
    #
    # Settling this needs the distribution of real values in (10, 15], which
    # nothing stored here retains: `ratings_json` keeps a display projection
    # with no provenance and no raw value. A live sweep found thousands of
    # Plex rating values with none above 10, but under 0.1% carried a
    # percentage source at all, and none carried Metacritic (see
    # docs/LEARNINGS.md), so that sweep leaves this branch unreached rather
    # than proven unnecessary. Settling it needs a server running the Rotten
    # Tomatoes or Metacritic agent. Neither answer is pinned by a test,
    # because asserting either would wrongly claim it is correct.
    if number > 10 and source in _PERCENTAGE_SOURCES:
        number /= 10.0
    if not 0.0 <= number <= 10.0:
        # Outside every scale we know how to read. A number we cannot interpret
        # must not protect, condemn, or be displayed as if it meant something.
        return None

    return Rating(source=source, value=number, votes=None, provider=provider)


def from_radarr(ratings: dict[str, Any] | None, *, provider: str = "radarr") -> list[Rating]:
    """Read Radarr's ``ratings`` object.

    Measured across a full production Radarr library: IMDb covers about 99% of
    movies, Rotten Tomatoes about 88%, and Metacritic about 85%. So for movies
    this data is close to free and close to complete. It costs no extra API
    key and no extra request, since this payload is already fetched for other
    reasons.

    Radarr's ``type`` field is ignored: it reports ``"user"`` for a Rotten
    Tomatoes value of 96, which is unambiguously the critic Tomatometer.
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
            # A 0-10 average above 10, or a percentage above 100, is outside
            # every scale this code knows how to read, so it is dropped
            # instead of guessed at.
            continue

        # Radarr's `votes` field reports 0 for percentage sources, but that
        # means "no vote concept," not "zero people voted." Treating it as an
        # actual zero would make a vote floor reject every Rotten Tomatoes
        # score.
        raw_votes = entry.get("votes")
        try:
            votes = int(raw_votes) if raw_votes and source not in _PERCENTAGE_SOURCES else None
        except (TypeError, ValueError):
            # A fork, a proxy, or a future schema might serialize votes as
            # "1,234" or as a list. That must cost this one rating, never the
            # operator's whole scan, so the failure is caught here instead of
            # raising out of the fact build. ``Rating.meets`` already treats
            # ``None`` as failing the vote floor closed, so this keeps the
            # safe reading.
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
    """One rating per source: the first one seen wins, and UNKNOWN is dropped.

    The scan holds several rating lists for one item: the IMDb dataset value,
    Radarr's ``ratings`` object, and Plex's two slots. They overlap, since a
    film can carry an IMDb score from both the dataset and Radarr. Pass the
    groups in precedence order, most authoritative first (for example the
    dataset's IMDb, then Radarr, then Plex), and the first rating seen for a
    source wins. A rating with an ``UNKNOWN`` source is never admitted, so a
    protection can never rest on a number this code cannot interpret.
    """
    out: dict[RatingSource, Rating] = {}
    for group in groups:
        for rating in group:
            if rating.source is RatingSource.UNKNOWN:
                continue
            out.setdefault(rating.source, rating)
    return tuple(out.values())
