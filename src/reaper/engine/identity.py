# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one place a media item is bound to its Plex identity.

Reaper's fate-deciding join is *arr item -> Plex item: the resulting Plex ``rating_key``
is what every downstream check reads (dormancy, popularity, the live streaming veto, the
executor's watched-since-approval interlock). Bind the wrong Plex item and Reaper does not
delete the *wrong* file -- deletes route by the arr's own ``media_key`` -- it deletes the
**right** file for the **wrong reasons**, having read a stranger's "nobody's watching,
long dormant." That is the prime-directive catastrophe, so this join is built to fail
toward *keeping the file* at every ambiguity.

## Why this module is pure

There is exactly one production join, reachable from the movie path, the season path, the
backtest and the planner without any of them importing a client (rule #3: never
reimplement the decision in a second place where it can drift). So this module holds only
data types and pure functions; the *index builders* that call Tautulli/Plex live in
``services`` and hand the frozen types in.

## The tiered ladder + contradiction veto

Both sides already carry stable ids -- Radarr ``imdbId``/``tmdbId``, Sonarr
``imdbId``/``tvdbId``, and every Plex item's ``imdb://``/``tmdb://``/``tvdb://`` GUIDs --
so an id match is precise where a title match is fragile (editions, punctuation, articles,
regional titles). The ladder, top to bottom:

1. **External id** -- shows on ``tvdb``; movies on ``tmdb`` then ``imdb``. Exactly one
   match binds; an id that names *two or more* Plex items is ambiguous and **abstains**
   (never degrades to a weaker tier), exactly as ``season_scan.resolve_season_keys`` drops
   both rows on a duplicate season number.
2. **File basename** -- the file's (movie) or folder's (show) name, compared across the
   mount-root difference Plex and the *arr disagree on.
3. **Title + year** -- the original fail-closed rule, kept verbatim as the backstop.

The reconcile is the load-bearing rule: a title *not resolving* is **silence**, and an id
still binds through silence (that is the whole point -- a renamed or regional title must
not stop a clean id match). But two tiers that both *resolve* to **different** rating keys
is a positive contradiction, and it **abstains**. Corroborate-or-silent, never contradict.
Both ``abstain`` and ``unmatched`` yield ``rating_key=None``, so the verdict path is
byte-identical to today's no-match (Unknown facts -> ABSTAIN -> the executor spares); only
``detail`` differs, which is what lets the why-panel be honest about *why* a file was kept.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class MatchedBy(enum.StrEnum):
    """Which signal bound an item to its Plex row -- surfaced in the audit log."""

    TVDB = "tvdb"
    TMDB = "tmdb"
    IMDB = "imdb"
    FILE_BASENAME = "file_basename"
    TITLE_YEAR = "title_year"


class MatchStatus(enum.StrEnum):
    """Whether the item was confidently bound to a Plex row.

    The field the UI reads: on ``MATCHED`` it stays quiet (the panel just gets on with the
    reasoning); on ``UNMATCHED`` / ``AMBIGUOUS`` it shows a plain "kept to be safe" notice,
    and which of the two picks the wording ("we couldn't find this" vs "this looks like more
    than one thing").
    """

    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"


# ---------------------------------------------------------------------------
# Identifier hygiene -- a sentinel is "no id", and "no id" never matches.
# ---------------------------------------------------------------------------

_IMDB_RE = re.compile(r"^tt\d+$")


def _clean_imdb(value: object) -> str | None:
    """A real IMDb id (``tt`` + digits, not all-zero), or ``None``.

    Some sources emit ``tt0000000`` / ``tt0`` for "unknown". Matched against every other
    item's sentinel, it would cause a mass mis-bind, so it is treated as absent.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not _IMDB_RE.match(text):
        return None
    digits = text[2:]
    if not digits or set(digits) == {"0"}:  # tt0000000 / tt0 -> "no id"
        return None
    return text


def _clean_numeric(value: object) -> int | None:
    """A positive tmdb/tvdb id, or ``None`` -- ``0``/blank/non-numeric are "no id"."""
    if value is None:
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def to_basename(path: str | None) -> str | None:
    """The lowercased final segment of a file or folder path, or ``None``.

    THE single basename normalizer, used on both the Plex side and the *arr side so a
    basename match compares like with like across the mount-root difference (Radarr says
    ``/movies/...``; Plex says ``/media/movies/...`` -- but the leaf name is the same).
    Splits on either separator (the server may be POSIX or Windows) and lowercases, since a
    case difference is not a real difference. Idempotent, so callers may pass a full path or
    an already-reduced basename.
    """
    if not path:
        return None
    segment = re.split(r"[\\/]", path.strip())[-1].strip().lower()
    return segment or None


@dataclass(frozen=True, slots=True)
class ExternalIds:
    """A media item's external ids, already sentinel-filtered. Build via :meth:`of`."""

    imdb: str | None = None
    tmdb: int | None = None
    tvdb: int | None = None

    @classmethod
    def of(cls, *, imdb: object = None, tmdb: object = None, tvdb: object = None) -> ExternalIds:
        """Construct with the sentinel filter applied to every id -- the only safe door in."""
        return cls(
            imdb=_clean_imdb(imdb),
            tmdb=_clean_numeric(tmdb),
            tvdb=_clean_numeric(tvdb),
        )

    def get(self, kind: str) -> str | int | None:
        if kind == "imdb":
            return self.imdb
        if kind == "tmdb":
            return self.tmdb
        if kind == "tvdb":
            return self.tvdb
        raise ValueError(f"unknown id kind: {kind!r}")

    @property
    def empty(self) -> bool:
        return self.imdb is None and self.tmdb is None and self.tvdb is None


def _split_guid(guid: str) -> tuple[str | None, str | int | None]:
    """``(kind, value)`` for a Plex GUID, or ``(None, None)`` if it carries no external id.

    Handles the new-agent form (``imdb://tt1234567``, ``tmdb://12345``, ``tvdb://999``) and
    the legacy single-guid form (``com.plexapp.agents.imdb://tt1234567?lang=en``,
    ``com.plexapp.agents.themoviedb://12345``, ``com.plexapp.agents.thetvdb://73141/1/2``),
    stripping any ``?query`` and path tail. Non-external agents (``plex://``,
    ``com.plexapp.agents.none``, ``local://``) legitimately carry no external id and return
    ``(None, None)`` so they never contribute a Tier-1 signal.
    """
    text = guid.strip()
    if "://" not in text:
        return None, None
    prefix, _, rest = text.partition("://")
    value = re.split(r"[?/]", rest, maxsplit=1)[0].strip()
    if not value:
        return None, None
    # The agent is the last dotted segment: 'com.plexapp.agents.imdb' -> 'imdb'; the new
    # agents are the bare scheme already.
    agent = prefix.lower().rsplit(".", 1)[-1]
    if agent == "imdb":
        return "imdb", value
    if agent in ("tmdb", "themoviedb"):
        return "tmdb", value
    if agent in ("tvdb", "thetvdb"):
        return "tvdb", value
    return None, None


def parse_guids(guids: Iterable[str], legacy_guid: str | None = None) -> ExternalIds:
    """Parse Plex GUIDs (new-agent list first, then the legacy single guid) into ids.

    The new-agent ``guids`` list wins per id kind; any kind still unset is filled from the
    legacy ``guid`` string. All values pass through :meth:`ExternalIds.of`, so sentinels
    and malformed ids drop out here rather than reaching a comparison.
    """
    imdb: str | int | None = None
    tmdb: str | int | None = None
    tvdb: str | int | None = None

    def _absorb(kind: str | None, value: str | int | None) -> None:
        nonlocal imdb, tmdb, tvdb
        if kind == "imdb" and imdb is None:
            imdb = value
        elif kind == "tmdb" and tmdb is None:
            tmdb = value
        elif kind == "tvdb" and tvdb is None:
            tvdb = value

    for raw in guids:
        _absorb(*_split_guid(str(raw)))
    if legacy_guid:
        _absorb(*_split_guid(str(legacy_guid)))
    return ExternalIds.of(imdb=imdb, tmdb=tmdb, tvdb=tvdb)


# ---------------------------------------------------------------------------
# The frozen Plex index the resolver joins against.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlexItem:
    """One Plex library item, as the resolver sees it.

    ``added_at`` is sourced from the Tautulli spine (never re-derived), so dormancy stays
    byte-identical to the title-only era; the ids and ``file_basename`` are enriched on top
    from the plexapi sweep.
    """

    rating_key: int
    title: str
    year: int | None
    added_at: datetime | None
    ids: ExternalIds = ExternalIds()
    file_basename: str | None = None


@dataclass(frozen=True, slots=True)
class PlexIndex:
    """The Plex library, inverted every way the resolver looks it up.

    Every inverted map holds a *list* of rating keys per key (never last-write-wins): a
    duplicate id or basename must be *seen* as a duplicate so the resolver can abstain, not
    silently collapsed to whichever row sorted last.
    """

    by_rating_key: dict[int, PlexItem]
    by_imdb: dict[str, list[int]]
    by_tmdb: dict[int, list[int]]
    by_tvdb: dict[int, list[int]]
    by_basename: dict[str, list[int]]
    by_title: dict[str, list[int]]

    @classmethod
    def build(cls, items: Iterable[PlexItem]) -> PlexIndex:
        by_rating_key: dict[int, PlexItem] = {}
        by_imdb: dict[str, list[int]] = {}
        by_tmdb: dict[int, list[int]] = {}
        by_tvdb: dict[int, list[int]] = {}
        by_basename: dict[str, list[int]] = {}
        by_title: dict[str, list[int]] = {}
        for item in items:
            if item.rating_key in by_rating_key:
                # A rating key is unique in Plex; a duplicate means a malformed spine. Keep
                # the first and skip -- never silently overwrite an item's identity.
                continue
            by_rating_key[item.rating_key] = item
            if item.ids.imdb is not None:
                by_imdb.setdefault(item.ids.imdb, []).append(item.rating_key)
            if item.ids.tmdb is not None:
                by_tmdb.setdefault(item.ids.tmdb, []).append(item.rating_key)
            if item.ids.tvdb is not None:
                by_tvdb.setdefault(item.ids.tvdb, []).append(item.rating_key)
            basename = to_basename(item.file_basename)
            if basename is not None:
                by_basename.setdefault(basename, []).append(item.rating_key)
            by_title.setdefault(item.title.lower(), []).append(item.rating_key)
        return cls(by_rating_key, by_imdb, by_tmdb, by_tvdb, by_basename, by_title)

    def _by_id(self, kind: str) -> dict[Any, list[int]]:
        if kind == "imdb":
            return self.by_imdb
        if kind == "tmdb":
            return self.by_tmdb
        if kind == "tvdb":
            return self.by_tvdb
        raise ValueError(f"unknown id kind: {kind!r}")


# ---------------------------------------------------------------------------
# The resolution result.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of a join. ``rating_key is None`` -- whether *unmatched* or *abstained*
    -- always keeps the file; only ``detail`` (and the presence of a ``plex_item``) differ,
    which is what the why-panel renders."""

    rating_key: int | None
    matched_by: MatchedBy | None
    detail: str
    status: MatchStatus
    plex_item: PlexItem | None = None

    @classmethod
    def bound(cls, item: PlexItem, by: MatchedBy, detail: str) -> Resolution:
        return cls(
            rating_key=item.rating_key,
            matched_by=by,
            detail=detail,
            status=MatchStatus.MATCHED,
            plex_item=item,
        )

    @classmethod
    def abstain(cls, detail: str) -> Resolution:
        # An abstain only ever comes from a duplicate id/basename or a cross-tier conflict --
        # every one of which is "more than one possible match", i.e. AMBIGUOUS.
        return cls(rating_key=None, matched_by=None, detail=detail, status=MatchStatus.AMBIGUOUS)

    @classmethod
    def unmatched(cls, detail: str = "No Plex item matched this title") -> Resolution:
        return cls(rating_key=None, matched_by=None, detail=detail, status=MatchStatus.UNMATCHED)


def title_year_match(title: str | None, year: int | None, index: PlexIndex) -> int | None:
    """The original title+year rule, relocated verbatim so there is exactly one copy.

    A single title hit binds unless the known years conflict; a duplicate title needs a
    year that singles out exactly one; any residual ambiguity returns ``None`` (fail
    closed). Accepts a match when either side has no year -- Plex often omits it -- which is
    as safe as the title-only join ever was in the unambiguous case.
    """
    rating_keys = index.by_title.get((title or "").lower())
    if not rating_keys:
        return None
    if len(rating_keys) == 1:
        only = index.by_rating_key[rating_keys[0]]
        if year is not None and only.year is not None and only.year != year:
            return None
        return only.rating_key
    if year is None:
        return None  # a duplicate title we cannot disambiguate -> refuse rather than guess
    matched = [rk for rk in rating_keys if index.by_rating_key[rk].year == year]
    return matched[0] if len(matched) == 1 else None


_MOVIE_ID_PRIORITY: tuple[str, ...] = ("tmdb", "imdb")
_SHOW_ID_PRIORITY: tuple[str, ...] = ("tvdb",)


def resolve(
    *,
    ids: ExternalIds,
    title: str | None,
    year: int | None,
    file_basename: str | None,
    index: PlexIndex,
    id_priority: Sequence[str],
) -> Resolution:
    """Bind one *arr item to its Plex rating key, failing closed on every ambiguity.

    See the module docstring for the ladder and the contradiction veto. ``id_priority`` is
    the order external ids are tried (movies: tmdb then imdb; shows: tvdb) -- callers use
    :func:`resolve_movie` / :func:`resolve_show` so they cannot pass the wrong order.
    """
    # -- Tier 1: external id -------------------------------------------------
    tier1: tuple[int, MatchedBy] | None = None
    for kind in id_priority:
        value = ids.get(kind)
        if value is None:
            continue
        hits = index._by_id(kind).get(value, [])
        if len(hits) == 1:
            tier1 = (hits[0], MatchedBy(kind))
            break
        if len(hits) >= 2:
            # A stable id that names two Plex items is ambiguous by definition. Abstain --
            # never fall through to a weaker tier that might "resolve" it by guessing.
            return Resolution.abstain(
                f"Kept: {kind.upper()} id {value} names {len(hits)} Plex items (ambiguous)"
            )
        # len 0: this id kind names nothing in Plex -> try the next kind.

    # -- Tier 2: file basename (only if no id bound) -------------------------
    tier2: int | None = None
    basename = to_basename(file_basename)
    if tier1 is None and basename is not None:
        hits = index.by_basename.get(basename, [])
        if len(hits) == 1:
            tier2 = hits[0]
        elif len(hits) >= 2:
            return Resolution.abstain(
                f"Kept: file name {basename!r} names {len(hits)} Plex items (ambiguous)"
            )

    # -- Tier 3: title + year (the backstop) ---------------------------------
    tier3 = title_year_match(title, year, index)

    # -- Reconcile -----------------------------------------------------------
    tier1_rk = tier1[0] if tier1 is not None else None
    resolved = {rk for rk in (tier1_rk, tier2, tier3) if rk is not None}
    if len(resolved) >= 2:
        parts: list[str] = []
        if tier1 is not None:
            parts.append(f"{tier1[1].value}->{tier1[0]}")
        if tier2 is not None:
            parts.append(f"basename->{tier2}")
        if tier3 is not None:
            parts.append(f"title->{tier3}")
        return Resolution.abstain("Kept: identifiers disagree (" + ", ".join(parts) + ")")
    if not resolved:
        return Resolution.unmatched()

    # Exactly one rating key: bind it, crediting the highest tier that produced it.
    if tier1 is not None:
        rk, by = tier1
        item = index.by_rating_key[rk]
        return Resolution.bound(
            item, by, f"Bound to Plex item by {by.value.upper()} id {ids.get(by.value)}"
        )
    if tier2 is not None:
        item = index.by_rating_key[tier2]
        return Resolution.bound(
            item, MatchedBy.FILE_BASENAME, f"Bound to Plex item by file name {basename!r}"
        )
    assert tier3 is not None  # the only remaining member of `resolved`
    item = index.by_rating_key[tier3]
    year_text = f" ({item.year})" if item.year is not None else ""
    return Resolution.bound(
        item, MatchedBy.TITLE_YEAR, f"Bound by title + year: {item.title!r}{year_text}"
    )


def resolve_movie(
    *,
    ids: ExternalIds,
    title: str | None,
    year: int | None,
    file_basename: str | None,
    index: PlexIndex,
) -> Resolution:
    """Bind a movie: id priority tmdb then imdb."""
    return resolve(
        ids=ids,
        title=title,
        year=year,
        file_basename=file_basename,
        index=index,
        id_priority=_MOVIE_ID_PRIORITY,
    )


def resolve_show(
    *,
    ids: ExternalIds,
    title: str | None,
    year: int | None,
    file_basename: str | None,
    index: PlexIndex,
) -> Resolution:
    """Bind a show: id priority tvdb (Sonarr's primary key)."""
    return resolve(
        ids=ids,
        title=title,
        year=year,
        file_basename=file_basename,
        index=index,
        id_priority=_SHOW_ID_PRIORITY,
    )
