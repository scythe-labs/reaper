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
   match binds. An id that names *two or more* Plex items is the same content in several
   copies (split HD/4K sections, a curated section re-listing a title); the *arr item's
   own file name may then pick the copy -- compared only among that id's candidates, with
   every candidate's file names known. A name matching exactly one candidate binds it. A
   name matching *several* is first narrowed by the **operator's library map**, the one
   corroborator that is ground truth rather than inference: in Settings each *arr root folder
   is mapped to the Plex library its content lands in, so a listing in any *other* library is
   not this instance's copy and is dropped. It only narrows this id's own candidates, never
   looks wider, and stands down (changing nothing, keeping the file) wherever the partition is
   not trustworthy -- no library mapped for this item, a candidate whose own library Plex did
   not report, a byte-identical twins group the size step must merge whole, or a mapped library
   holding none of the candidates (a stale or mistaken mapping, which the scan warns about
   rather than mis-binding). It is what tells one title kept in both an HD and a 4K library
   apart when nothing in the path can: a show has no size and its folder is identical in both,
   so the map is the only discriminator a show ever gets.
   A name still matching *several* gets a second corroborator, the folder the copy sits in. The
   *arr's own root is read from that instance's root folder list and passed in, never
   guessed at a fixed depth, which turns its path into the item's path *relative to its
   library*; the one candidate whose Plex path **ends with** those exact segments is the
   copy this entry manages. That needs nothing from Plex's own root. It is deliberately not
   a "deepest shared suffix wins" ranking: two copies whose Plex roots differ in depth would
   be scored against each other as though the shallower one running out of path were
   evidence against it, and that bound the wrong copy. It declines wherever its evidence is
   not trustworthy (no root, a depth that does not match the *arr's own layout, an
   unreadable path on any candidate; see :func:`_narrow_by_path_depth`). Where its winner's
   size cannot be checked, or the folder and the exact size name *different* copies, the
   caller stands it down or abstains outright -- both live in
   :func:`_narrow_among_id_hits`. A show never gets folder evidence at all: Sonarr writes
   ``<root>/<Show>``, so below a correct root there is only the leaf already matched. It
   also stands aside when the size step below would merge twins, since it can only ever
   return one listing.
   A name still matching *several* gets the last corroborator, the exact byte size the
   *arr records for its file: a size singling out one candidate binds it, and several
   candidates carrying that name at exactly that size are byte-identical twins -- one
   file listed more than once (a curated section re-listing the very file) -- bound as a
   **group** under one canonical key, with every listing's rating key kept so watch reads
   cover all of them. That is corroboration *inside* the strongest tier's answer set, and
   it stays correct even when a shared id is an agent mis-tag: the candidate holding the
   *arr's file is the row whose watch history describes that file. What an ambiguous id
   never does is consult the wider library -- a weaker tier "resolving" the tie from
   outside the candidate set would be a guess -- so any residual ambiguity (an unknown
   name or size on either side included) **abstains**, exactly as
   ``season_scan.resolve_season_keys`` drops both rows on a duplicate season number.
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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from reaper.ratings import Rating


class MatchedBy(enum.StrEnum):
    """Which signal bound an item to its Plex row -- surfaced in the audit log."""

    TVDB = "tvdb"
    TMDB = "tmdb"
    IMDB = "imdb"
    #: An id shared by several Plex copies, narrowed to one by the *arr's own file name
    #: (and, where the name alone was not enough, its exact file size).
    ID_AND_BASENAME = "id_and_basename"
    #: One file listed several times in Plex (id, file name and exact size all equal),
    #: bound to every listing at once so its watch history is read from all of them.
    MERGED_LISTINGS = "merged_listings"
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


def to_segments(path: str | None) -> tuple[str, ...]:
    """A path split into its lowercased, non-empty segments, outermost first.

    The companion to :func:`to_basename`, and normalized the same way (either separator,
    lowercased) so the two sides of a comparison are reduced identically. Returns an empty
    tuple for a missing path, which never matches and never mismatches.
    """
    if not path:
        return ()
    return tuple(
        seg for seg in (s.strip().lower() for s in re.split(r"[\\/]", path.strip())) if seg
    )


def root_folder_paths(payload: object) -> tuple[str, ...]:
    """The usable root folder paths out of an *arr ``/rootfolder`` response body.

    Defensive by construction: a body that is not a list, an entry that is not a mapping,
    and an entry whose ``path`` is missing or blank are all skipped rather than raised on.
    A malformed body therefore yields ``()``, which stands the folder corroborator down
    (:func:`_narrow_by_path_depth` returns ``None`` with no root), and standing it down can
    only ever produce an abstain -- never a bind, and never a deletion.
    """
    if not isinstance(payload, list):
        return ()
    paths: list[str] = []
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        path = entry.get("path")
        if isinstance(path, str) and path.strip():
            paths.append(path)
    return tuple(paths)


def library_for_path(file_path: str | None, library_map: Mapping[str, str] | None) -> str | None:
    """The Plex library the operator mapped this item's root folder to, or ``None``.

    ``library_map`` is one *arr instance's ``{root folder path: Plex library title}`` map. The
    item's root is the *longest* mapped root whose segments prefix the item's path (compared as
    normalized segments via :func:`to_segments`, so a trailing slash, doubled separator or case
    difference cannot make a real root miss), and its mapped library is returned. ``None`` when
    there is no map, the path is empty, or no mapped root prefixes it -- all of which leave the
    item unmapped, which the resolver reads as "no library declared" and keeps today's behavior.
    Longest-match, so an instance mapping both ``/tv`` and ``/tv/anime`` sends a series under the
    latter to the anime library, not the general one.
    """
    if not library_map:
        return None
    segments = to_segments(file_path)
    if not segments:
        return None
    best_root: str | None = None
    best_depth = 0
    for root in library_map:
        root_segments = to_segments(root)
        depth = len(root_segments)
        if depth == 0 or depth > len(segments):
            continue
        if segments[:depth] == root_segments and depth > best_depth:
            best_depth = depth
            best_root = root
    return library_map[best_root] if best_root is not None else None


def libraries_for_ids(ids: ExternalIds, index: PlexIndex, id_priority: Sequence[str]) -> set[str]:
    """The case-folded Plex library titles an item's ids name across *all* their hits.

    For the stale-mapping guard only: if the operator mapped an item's folder to a library that
    appears nowhere among that item's candidate listings, the scan warns the mapping is likely
    wrong or renamed (services.season_scan / snapshot) rather than let it silently mis-bind.
    Case-folded to match :func:`_same_library`. Never used to bind -- that is
    :func:`_narrow_among_id_hits`.
    """
    libs: set[str] = set()
    for kind in id_priority:
        value = ids.get(kind)
        if value is None:
            continue
        for rk in index._by_id(kind).get(value, []):
            lib = index.by_rating_key[rk].library
            if lib:
                libs.add(lib.strip().casefold())
    return libs


def _plex_size_of(rating_key: int, basename: str, index: PlexIndex) -> int | None:
    """This listing's byte size for the file with this name, or ``None`` if unknowable.

    ``None`` covers both "Plex reported no size" and "this listing carries the name more
    than once" (a merged multi-edition item), because neither yields one number to compare.
    The size branch treats exactly the same two shapes as unknown.
    """
    sizes = [
        file.size
        for file in index.by_rating_key[rating_key].files
        if to_basename(file.basename) == basename
    ]
    return sizes[0] if len(sizes) == 1 else None


def _size_contradicts(
    rating_key: int, basename: str, file_size: int | None, index: PlexIndex
) -> bool:
    """Whether this listing's own byte size positively rules it out.

    Only a *known* size on **both** sides can contradict, so this says yes exactly when both
    numbers are in hand and they disagree. Could-not-look is never "different" -- that case
    is :func:`_size_unconfirmed`, which is a different answer with a different consequence.
    """
    if file_size is None:
        return False
    size = _plex_size_of(rating_key, basename, index)
    return size is not None and size != file_size


def _size_unconfirmed(
    rating_key: int, basename: str, file_size: int | None, index: PlexIndex
) -> bool:
    """Whether the *arr knows a size but this listing gives nothing to check it against.

    Distinct from :func:`_size_contradicts`, and it must be. A contradiction means two
    corroborators name different copies, which abstains. This means the folder named a copy
    that cannot be size-checked at all, while some *other* candidate might match the size
    exactly -- so the folder must not bind on its own circumstantial evidence. It stands
    down and lets the size branch decide, which is also where a listing carrying the name
    twice is correctly read as unknown and abstains.
    """
    return file_size is not None and _plex_size_of(rating_key, basename, index) is None


def _same_library(candidate: str | None, mapped: str) -> bool:
    """Whether a Plex listing's own library is the one the operator mapped this folder to.

    Compared case- and whitespace-folded: the mapped value is a stored copy of the library's
    own title (Plex library names are unique per server, so the title is a stable key), and an
    operator may have re-cased or padded it. A listing whose library is unknown never matches
    here, but the caller requires *every* candidate's library to be known before the map is
    consulted at all -- so unknown is handled there, deliberately, never silently as
    "different".
    """
    return candidate is not None and candidate.strip().casefold() == mapped.strip().casefold()


def _ends_with(path_segments: Sequence[str], suffix: Sequence[str]) -> bool:
    """Whether a Plex path ends with the *arr's library-relative path, exactly.

    The whole folder corroborator, and the reason it needs nothing from Plex's own root.
    :func:`_below_arr_root` yields the item's path *relative to its library* -- everything
    the *arr instance knows below its own root. Any Plex listing of that same file, under
    whatever root that section is mounted at, must end with those very segments. So this
    is a proof of identity that never has to know where Plex's root ends.

    Deliberately **not** a "deepest shared suffix wins" ranking. Ranking is unsound here:
    two copies whose Plex roots differ in *depth* are scored against each other as if the
    deeper match were better evidence, when the extra segment is the shallower copy's root
    running out, not a real folder disagreeing. That comparison bound the wrong copy, and
    it is why no fixed-depth strip is applied to either side any more.
    """
    if not suffix or len(suffix) > len(path_segments):
        return False
    return tuple(path_segments[-len(suffix) :]) == tuple(suffix)


def _below_arr_root(file_path: str | None, arr_roots: Sequence[str]) -> tuple[str, ...] | None:
    """The *arr path's segments strictly below its own root folder, or ``None``.

    ``arr_roots`` are the root folder paths that very *arr instance reports (Radarr and
    Sonarr both expose them at ``/rootfolder``), so the root is *known* rather than guessed
    at a fixed depth. Radarr reports a movie at ``<root>/<Title>/<file>`` and Sonarr a
    series at ``<root>/<Title>``, so the longest supplied root that prefixes the path is
    reliably this item's root. Both sides are compared as normalized segments
    (:func:`to_segments`), so a trailing slash, a doubled separator or a case difference
    cannot make a real root miss.

    ``None`` when no supplied root prefixes the path, or when none was supplied at all.
    That is deliberate and must stay: with the root unknown there is no way to tell a real
    folder from a leftover piece of somebody's mount point, so the corroborator has no
    trustworthy evidence and the caller must fall through rather than strip a fixed number
    of segments.
    """
    segments = to_segments(file_path)
    if not segments:
        return None
    longest = 0
    for root in arr_roots:
        root_segments = to_segments(root)
        depth = len(root_segments)
        # ``depth >= len(segments)``: the path is the root itself (or shorter), so nothing
        # sits below it and this root cannot be the answer. A shorter root may still match
        # on a later pass; the depth check downstream is what rejects the short root's
        # answer, not this loop.
        if depth == 0 or depth >= len(segments):
            continue
        if segments[:depth] == root_segments and depth > longest:
            longest = depth
    if longest == 0:
        return None
    return segments[longest:]


@dataclass(frozen=True, slots=True)
class PlexFile:
    """One file (movie) or folder (show) behind a Plex listing.

    ``basename`` is the location's leaf; ``size`` is the exact byte count Plex reports
    for the file, or ``None`` where there is none to report (show folders) or Plex did
    not say. An unknown size never matches and never mismatches -- it abstains.

    ``path`` is the location's *full* path as Plex reports it, kept because a show folder
    has no size and its leaf alone cannot separate the same title listed in two sections
    (an HD library and a 4K one name the folder identically). It is only ever tested for
    ending with the *arr's library-relative path, and only among one id's own candidates
    -- see :func:`_ends_with`. ``None`` where Plex did not report a path.
    """

    basename: str
    size: int | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True)
class PlexItem:
    """One Plex library item, as the resolver sees it.

    For every row the Tautulli spine lists, ``added_at`` is sourced from that spine (never
    re-derived), so dormancy stays byte-identical to the title-only era; the ids and file
    names are enriched on top from the plexapi sweep. An item the Tautulli cache has not
    listed yet (a fresh addition) enters the index from the plexapi sweep alone, carrying
    Plex's own added-at -- there is no spine value to preserve for it.

    The two file fields have one job each and are built from the same media/location
    data: ``file_basename`` is the *first* location's leaf and feeds the global Tier-2
    ``by_basename`` map (unchanged semantics); ``files`` is *every* file behind the
    listing, each with its exact byte size, and is consulted only to narrow an ambiguous
    id -- a merged multi-edition item holds several files, and narrowing must see all of
    them or a re-list of its second file would look "unique". Empty means the files are
    unknown, and narrowing treats unknown as un-narrowable, never as "different".
    """

    rating_key: int
    title: str
    year: int | None
    added_at: datetime | None
    ids: ExternalIds = ExternalIds()
    file_basename: str | None = None
    files: tuple[PlexFile, ...] = ()
    # --- display metadata riding the same sweep -----------------------------------
    # Captured because the section listing already carries it, and carried onto the
    # candidate so the review queue can show it. None of these fields participate in
    # matching or in any verdict; a row missing them matches and judges identically.
    video_resolution: str | None = None
    """Plex's ``videoResolution`` for the first media ("4k", "1080", ...). Movies only;
    show listings carry no media."""
    content_rating: str | None = None
    runtime_minutes: int | None = None
    ratings: tuple[Rating, ...] = ()
    """Plex's critic/audience ratings with provenance read from the ``*RatingImage``
    (see reaper.ratings) -- never bare numbers whose meaning was guessed."""
    library: str | None = None
    """The library (section) title this item was listed under, as the operator named it.
    Carried onto the candidate for display, and -- since the operator's library map landed --
    consulted in matching: when an id names copies in two libraries, the one whose ``library``
    equals the operator's mapping for this item's root folder is bound (:func:`_same_library`,
    :func:`_narrow_among_id_hits`). For a show it is the show's section, inherited by every
    season row. None when the section is unknown, which stands the library step down rather than
    counting as a mismatch."""


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
    merged_rating_keys: tuple[int, ...] = ()
    """Every Plex listing this bind covers, when the bound file is listed more than once
    (``matched_by is MERGED_LISTINGS``); includes ``rating_key`` itself. Empty for a
    single-listing bind. Every downstream watch read (dormancy, watchers, the streaming
    veto, the executor's played-since-approval interlock) must consult all of these keys,
    or a play made through the file's other listing would be invisible."""

    @classmethod
    def bound(
        cls, item: PlexItem, by: MatchedBy, detail: str, *, merged: tuple[int, ...] = ()
    ) -> Resolution:
        return cls(
            rating_key=item.rating_key,
            matched_by=by,
            detail=detail,
            status=MatchStatus.MATCHED,
            plex_item=item,
            merged_rating_keys=merged,
        )

    @classmethod
    def abstain(cls, detail: str) -> Resolution:
        # Every abstain is "more than one possible match", i.e. AMBIGUOUS: a duplicate id
        # the file name and size could not narrow, a duplicate basename, a cross-tier
        # conflict, two id kinds naming different rows, or the folder and the exact size
        # naming different copies.
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


def _twin_group(
    matched: Sequence[int], basename: str, file_size: int | None, index: PlexIndex
) -> tuple[int, ...]:
    """The listings the size corroborator would merge: byte-identical twins, or ``()``.

    Mirrors the qualifying test in the size branch of :func:`_narrow_among_id_hits`: a
    listing qualifies when it carries this basename once, at a size Plex reported, equal to
    the *arr's own byte count. Two or more such listings are one file listed more than once,
    and reading only one of them under-counts watching -- the direction that condemns.

    Returns the members rather than a bare "yes", because the caller has to ask whether the
    folder corroborator's own answer is *inside* this group. It is only safe for the folder
    step to stand aside for a group it agrees with: a folder winner from outside the group
    is two corroborators naming different copies, which abstains.

    Deliberately *not* gated on that branch's abstain checks (an unknown size on some other
    matching listing). Those make the size branch abstain, and standing aside to reach an
    abstain keeps the file too.
    """
    if file_size is None:
        return ()
    twins = [
        rk
        for rk in matched
        if _plex_size_of(rk, basename, index) is not None
        and _plex_size_of(rk, basename, index) == file_size
    ]
    return tuple(twins) if len(twins) >= 2 else ()


def _narrow_by_path_depth(
    matched: Sequence[int],
    basename: str,
    file_path: str | None,
    file_size: int | None,
    index: PlexIndex,
    arr_roots: Sequence[str] = (),
    arr_layout_depth: int = 2,
) -> int | None:
    """The one listing that holds this *arr item's library-relative path, or ``None``.

    Every listing in ``matched`` already holds a file with this leaf name, so the leaf is
    spent as a discriminator and only the segments *above* it can still speak.
    :func:`_below_arr_root` turns the *arr's path into the item's path *relative to its own
    library* (given ``arr_roots`` from that instance's ``/rootfolder``, so the root is known
    rather than guessed). A Plex listing of that same file, under whatever root its section
    is mounted at, must end with exactly those segments -- so :func:`_ends_with` decides it,
    and Plex's own root never has to be known. Exactly one holder binds; several or none
    fall through.

    With no root supplied, or a path under none of the supplied roots, this returns
    ``None``: the corroborator stands down rather than guess where the root ends.

    **No ranking.** An earlier version scored candidates by deepest shared suffix and let
    the deepest strictly win. That is unsound when two copies' Plex roots differ in *depth*:
    the shallower copy simply runs out of path, and losing that comparison reads as evidence
    against it when it is nothing of the kind. It bound the wrong copy. An exact suffix
    match has no such failure mode -- a copy either carries the relative path or it does not.

    **What the root requirement costs, plainly.** Measuring strictly below the root removes
    two binds this step used to make, and both of them were wrong:

    * Two instances that each map their own host directory to the same in-container root
      (the standard single-mount layout) both report ``<root>/<Title>/<file>``. Below the
      root that is ``(title, file)`` for both, which ties against every copy, so this
      abstains. A movie then recovers through its exact byte size; a show has no size and
      keeps both copies. Previously the leftover root segment broke the tie and could bind
      the *wrong* copy, reading a stranger's watch history and added-at.
    * A library distinction that lives in the root itself (one instance rooted at one path,
      another at a different one, each series being just root-plus-show) leaves only the
      show name below the root, identical in both libraries, so this abstains too. That
      information genuinely is not in the path. Comparing the roots' own leaf names would
      not recover it either: in the two-instance case above both instances carry the *same*
      root leaf, so such a rule would bind both to one copy.

    An abstain keeps the file, so both losses resolve the safe way.

    Knows nothing about byte-identical twins: the caller arbitrates that, because only the
    caller can ask whether this step's winner is *inside* the group the size branch would
    merge (:func:`_twin_group`). Gating it here instead returned ``None`` before a winner
    existed, which silently discarded a definite answer that contradicted the group.

    ``None`` means "could not narrow", never "no match" -- the caller falls through to the
    size corroborator and, failing that, abstains. Unknown is never "different".
    """
    arr_segments = _below_arr_root(file_path, arr_roots)
    # The *arr's own layout pins how deep this must be: Radarr writes <root>/<Title>/<file>
    # and Sonarr <root>/<Show>, so anything else means the reported root is not this item's
    # real root (a stale root after the operator reconfigured them -- Radarr does not move
    # files on a root change -- a manual import into a nested folder, a container mounted
    # above the library). The leftover mount segments would then be compared as if they
    # were real folders, which is the whole class of bug this step keeps regenerating, so
    # an unexpected depth stands the step down rather than guessing.
    if arr_segments is None or len(arr_segments) != arr_layout_depth:
        return None
    if len(arr_segments) < 2:
        return None  # only the leaf both sides already matched on: no new evidence

    # A candidate whose path Plex did not report cannot be tested, and dropping it from the
    # holder set would silently turn a tie into a strict win for someone else -- exactly the
    # inversion this step was rebuilt to remove. Could-not-look is never "different".
    if any(
        file.path is None
        for rk in matched
        for file in index.by_rating_key[rk].files
        if to_basename(file.basename) == basename
    ):
        return None

    holders = [
        rk
        for rk in matched
        if any(
            to_basename(file.basename) == basename
            and file.path is not None
            and _ends_with(to_segments(file.path), arr_segments)
            for file in index.by_rating_key[rk].files
        )
    ]
    if len(holders) != 1:
        # Several carrying it is a genuine tie (the same relative path under two different
        # Plex roots); none carrying it is no evidence at all. Both fall through to size.
        return None
    return holders[0]


def _narrow_among_id_hits(
    hits: Sequence[int],
    basename: str | None,
    file_size: int | None,
    index: PlexIndex,
    file_path: str | None = None,
    arr_roots: Sequence[str] | None = (),
    arr_layout_depth: int = 2,
    plex_library: str | None = None,
) -> tuple[tuple[int, ...], str]:
    """The Tier-1 candidates provably holding this *arr item's file, or why none could be.

    All ``hits`` share one external id, so they are the same *content* in several copies
    (split HD/4K sections, a curated section re-listing a title). The *arr's file name is
    causally tied to the physical copy it manages, so a name matching exactly one
    candidate's files identifies *which copy this entry is* -- corroboration inside the
    id's own answer set, never a lookup in the wider library. Every candidate's file
    names must be known and the comparison covers **all** of a candidate's files: a copy
    whose files we could not see might be the very file this item manages, so "could not
    look" abstains rather than counting as "looked and it was different".

    A name matching SEVERAL candidates is first narrowed by ``plex_library`` when the operator
    set one: the Plex library this item's root folder is mapped to (Settings). Unlike the
    folder and size corroborators, which infer the copy from evidence, this is the operator's
    own declaration of where an instance's content lives, so a candidate in any *other* library
    is not this instance's copy and is dropped. It only ever narrows this id's candidates and
    stands down (changing nothing) wherever the partition is untrustworthy: any candidate whose
    own library Plex did not report (``_same_library`` never treats unknown as different), or a
    byte-identical twins group among the matches (the size branch must merge those whole rather
    than fragment one file by library). A single survivor binds it, unless its own size cannot
    be checked (stand down, let size decide) or its size positively contradicts the *arr's byte
    count (abstain, corroborate-or-silent). A mapped library holding *none* of the candidates is
    a stale or mistaken mapping: the map narrows to nothing and is simply ignored here, and the
    caller warns separately -- never a silent mis-bind. A show has no size and no usable folder,
    so this is the only thing that ever tells two same-name show listings apart.

    A name still matching SEVERAL candidates gets one more corroborator before size: the folder
    the copy lives *under* (:func:`_narrow_by_path_depth`). Comparing whole paths would be
    wrong -- the mount roots differ (:func:`to_basename` exists for that reason) -- so the
    *arr side is cut back to what sits below **that instance's own reported root folder**
    (``arr_roots``, from its ``/rootfolder``), which is the item's path relative to its
    library. The one candidate whose Plex path *ends with* those exact segments is the copy
    this entry manages (:func:`_ends_with`); that needs nothing from Plex's own root, so no
    fixed-depth strip is applied to either side. It is not a depth ranking, and must not
    become one: two copies whose Plex roots differ in depth would be scored against each
    other as though the shallower one running out of path were evidence against it, which
    bound the wrong copy.

    The step declines, falling through to size, in every shape where its evidence is not
    trustworthy: no root supplied or none prefixing the path; a below-root depth that does
    not match the *arr's own layout (``arr_layout_depth``, so a stale or over-broad root
    cannot pass mount segments off as folders); any candidate whose path Plex did not
    report, since dropping it would turn a tie into a strict win for someone else; and a
    winner whose size cannot be checked while another candidate might match it exactly
    (:func:`_size_unconfirmed`). Where the folder names one copy and the exact byte size
    names another, that is a positive contradiction and the whole narrowing abstains
    (:func:`_size_contradicts`) rather than letting either overrule the other.

    A show therefore never gets folder evidence: Sonarr writes ``<root>/<Show>``, so below
    a correct root there is only the leaf both sides already matched on. Two same-leaf show
    listings under one id are kept, not guessed between (recorded in docs/LEARNINGS.md).
    Where the size branch below would merge byte-identical twins (:func:`_twin_group`) the
    folder step stands aside, because it returns one listing and one listing cannot carry a
    group -- but only when its winner is *in* that group. A winner from outside it is the
    same contradiction as above and abstains, rather than being discarded behind the group.

    A name still matching SEVERAL candidates gets the last corroborator: the exact byte size
    the *arr records for its file. A size singling out one candidate binds it. Several
    candidates carrying that name at exactly that size are byte-identical twins -- one
    file listed more than once in Plex (verified live: a curated section re-lists the
    very same file under its own rating key, at a different path) -- and *all* of them
    are returned, because the file's plays are split across those listings and reading
    only one would under-count watching, which is the direction that condemns. The folder
    step above never preempts this, whatever paths the twins sit at. Any unknown (a missing
    name or size on either side, or a candidate holding two same-name files) abstains:
    unknown is never "different", and it is never "the same" either.

    Returns ``(rating_keys, text)``: on success one key (a clean single bind) or several
    (byte-identical twins) plus the corroborator wording for the bind detail; on failure
    ``()`` plus the reason phrased for the audit detail.
    """
    if arr_roots is None:
        # The instance's root folder list could not be read at all. That is NOT the same as
        # "no roots reported": without it the folder step cannot run, and the folder step is
        # the only thing that produces the folder-vs-size contradiction veto below. Falling
        # through to size alone would let a stale Plex size bind a copy the folder would have
        # disputed, so a read failure removes a safeguard rather than merely a bind. Refuse
        # the whole narrowing instead, which keeps the file.
        return (), (
            "Reaper couldn't read the folder list from Sonarr or Radarr, "
            "so it can't tell the copies apart"
        )
    if basename is None:
        return (), "this item has no file name to tell the copies apart"
    matched: list[int] = []
    sizes_by_key: dict[int, list[int | None]] = {}
    for rk in hits:
        files = index.by_rating_key[rk].files
        leaves = {
            leaf for leaf in (to_basename(file.basename) for file in files) if leaf is not None
        }
        if not leaves:
            return (), "a copy's file name is unknown, so the copies cannot be told apart"
        if basename in leaves:
            matched.append(rk)
            sizes_by_key[rk] = [
                file.size for file in files if to_basename(file.basename) == basename
            ]
    if len(matched) == 1:
        return (matched[0],), f"file name {basename!r}"
    if not matched:
        return (), "this item's file name matches none of them"

    # The operator's library map is ground truth about which library this instance's copies
    # live in, so it is applied before the inferred corroborators (folder, then size). It only
    # ever NARROWS this id's own name-matched candidates -- a listing in another library is not
    # this instance's copy -- and never looks wider. It stands down in every shape where the
    # partition cannot be trusted: any candidate whose own library is unknown (could-not-look
    # is never "different"), or a byte-identical twins group the size branch must merge whole.
    # A mapped library holding none of the candidates narrows to nothing and is ignored here;
    # the scan warns about that stale mapping separately (season_scan / snapshot), never a
    # silent mis-bind.
    if (
        plex_library is not None
        and len(matched) > 1
        and all(index.by_rating_key[rk].library is not None for rk in matched)
        and not _twin_group(matched, basename, file_size, index)
    ):
        in_library = [
            rk for rk in matched if _same_library(index.by_rating_key[rk].library, plex_library)
        ]
        if len(in_library) == 1:
            winner = in_library[0]
            if not _size_unconfirmed(winner, basename, file_size, index):
                # The library named one copy; where a byte size can be checked it must agree.
                if _size_contradicts(winner, basename, file_size, index):
                    # Library names one copy, the exact size names another: a positive
                    # contradiction. Corroborate-or-silent, never contradict -- keep the file.
                    return (), (
                        "the Plex library set for its folder and its file size "
                        "point at different copies"
                    )
                return (winner,), f"file name {basename!r} and the Plex library set for its folder"
            # else: the winner's size cannot be checked while another copy might match the byte
            # count exactly. Stand the library step down and let size decide, exactly as the
            # folder step does -- a show, having no size, is never unconfirmed and binds above.
        elif len(in_library) >= 2:
            # Several same-name copies in the one mapped library: the library cannot split them.
            # Narrow to just those and let the folder/size ladder try. A show has neither, so
            # two same-name listings in one mapped library abstain and are kept.
            matched = in_library

    # Several listings hold a file with this very name. Before size, try the folder each
    # copy lives under: the one listing whose path ends with this item's library-relative
    # path, taken from the *arr's own reported root.
    count = len(matched)
    disagree = (
        f"this item's file name matches {count} of them, and the folder it sits in "
        "and its file size point at different copies"
    )
    narrowed_by_path = _narrow_by_path_depth(
        matched, basename, file_path, file_size, index, arr_roots, arr_layout_depth
    )
    if narrowed_by_path is not None and _size_unconfirmed(
        narrowed_by_path, basename, file_size, index
    ):
        # The folder named a copy whose size cannot be checked, while another candidate may
        # match the *arr's byte count exactly. Circumstantial evidence must not bind over
        # evidence that has not been consulted yet, so stand down and let size decide.
        narrowed_by_path = None
    if narrowed_by_path is not None:
        if _size_contradicts(narrowed_by_path, basename, file_size, index):
            # The folder names one copy and the exact byte size names a different one.
            # That is a positive contradiction, and this module's rule is
            # corroborate-or-silent, NEVER contradict (see the module docstring). It must
            # abstain here rather than fall through: falling through would let the size
            # branch bind the other copy on evidence the folder just disputed, and a stale
            # Plex size (an *arr upgraded the file in place, Plex has not rescanned) makes
            # that the wrong copy. Keeping the file is the only answer both corroborators
            # can live with.
            return (), disagree
        twins = _twin_group(matched, basename, file_size, index)
        # The size branch below would merge a twins group, and a single winner cannot carry
        # one. Standing aside is right only when the folder AGREES with that group: if its
        # winner is one of the twins, fall through so every listing's plays are read. A
        # winner from OUTSIDE the group is the same contradiction as above, and deferring
        # would silently discard a definite folder answer and bind a stranger's listings.
        if twins and narrowed_by_path not in twins:
            return (), disagree
        if not twins:
            return (narrowed_by_path,), f"file name {basename!r} and the folder it sits in"

    # The last corroborator is size.
    if file_size is None:
        return (), (
            f"this item's file name matches {count} of them, "
            "and it has no file size to tell them apart"
        )
    for rk in matched:
        rk_sizes = sizes_by_key[rk]
        if len(rk_sizes) != 1 or rk_sizes[0] is None:
            return (), (
                f"this item's file name matches {count} of them, and a matching copy's "
                "file size is unknown, so they cannot be told apart"
            )
    same_size = [rk for rk in matched if sizes_by_key[rk][0] == file_size]
    if len(same_size) == 1:
        return (same_size[0],), f"file name {basename!r} and its exact file size"
    if not same_size:
        return (), (
            f"this item's file name matches {count} of them, "
            "but none of those files is the same size as its file"
        )
    # Byte-identical twins: the same file, listed several times.
    return tuple(same_size), f"file name {basename!r} and its exact file size"


def _earliest_listed(rating_keys: Sequence[int], index: PlexIndex) -> int:
    """The listing a merged group binds under: earliest-added, ties to the lowest key.

    A curated section re-lists a file long after its original listing, so the earliest
    added-at is the original row -- the one whose poster the queue should draw and whose
    added-at gives dormancy its honest floor (the file has been observable since then; the
    history horizon still caps the claim). A listing with no added-at never outranks one
    that has a date. Deterministic, so the same scan input always binds the same key.
    """

    def sort_key(rk: int) -> tuple[bool, float, int]:
        added = index.by_rating_key[rk].added_at
        return (added is None, added.timestamp() if added is not None else 0.0, rk)

    return min(rating_keys, key=sort_key)


_MOVIE_ID_PRIORITY: tuple[str, ...] = ("tmdb", "imdb")
_SHOW_ID_PRIORITY: tuple[str, ...] = ("tvdb",)


def resolve(
    *,
    ids: ExternalIds,
    title: str | None,
    year: int | None,
    file_basename: str | None,
    file_size: int | None = None,
    file_path: str | None = None,
    root_folders: Sequence[str] | None = (),
    arr_layout_depth: int,
    plex_library: str | None = None,
    index: PlexIndex,
    id_priority: Sequence[str],
) -> Resolution:
    """Bind one *arr item to its Plex rating key, failing closed on every ambiguity.

    See the module docstring for the ladder and the contradiction veto. ``id_priority`` is
    the order external ids are tried (movies: tmdb then imdb; shows: tvdb) -- callers use
    :func:`resolve_movie` / :func:`resolve_show` so they cannot pass the wrong order.
    ``file_size`` is the exact byte count the *arr records for its file (movies only;
    a show is bound by its folder, which has no size) and is consulted only when the file
    name alone cannot narrow an ambiguous id. ``file_path`` is that file or folder's full
    path on the *arr side, consulted in the same place and only for the trailing segments
    strictly below the instance's own root folder. ``root_folders`` is that instance's real
    root folder paths, read from its ``/rootfolder`` list; without them the path evidence
    is unusable (nothing in a path says where a mount root ends) and the folder step stands
    down, leaving size, and failing that an abstain. ``plex_library`` is the Plex library the
    operator mapped this item's root folder to (Settings); when set it narrows an ambiguous id
    to that library *before* the folder and size corroborators, since it is the operator's own
    declaration rather than inference (see :func:`_narrow_among_id_hits`).
    All of these are corroborators *inside* an id's own answer set; none is ever a lookup
    into the wider library.
    """
    basename = to_basename(file_basename)

    # -- Tier 1: external id -------------------------------------------------
    tier1: tuple[int, MatchedBy, str, tuple[int, ...]] | None = None
    tier1_kind: str | None = None
    for kind in id_priority:
        value = ids.get(kind)
        if value is None:
            continue
        hits = index._by_id(kind).get(value, [])
        if not hits:
            # This id kind names nothing in Plex -> consult the next kind.
            continue

        if tier1 is not None:
            # A kind already bound; later kinds are consulted as a CROSS-CHECK. A second
            # id kind resolving away from the bound row means the item's own external
            # ids contradict each other (one of them is mis-tagged, and there is no way
            # to know which), so the contradiction veto the tiers already apply to each
            # other applies within tier 1 as well: abstain. Agreement -- the bound key,
            # or any listing of its merged group, among this kind's hits -- simply
            # confirms the bind.
            assert tier1_kind is not None
            if {tier1[0], *tier1[3]}.isdisjoint(hits):
                return Resolution.abstain(
                    f"Kept: the {tier1_kind.upper()} id and the {kind.upper()} id "
                    f"{value} name different Plex items; the ids contradict each "
                    "other, so neither is trusted"
                )
            continue

        if len(hits) == 1:
            tier1 = (
                hits[0],
                MatchedBy(kind),
                f"Bound to Plex item by {kind.upper()} id {value}",
                (),
            )
            tier1_kind = kind
            continue  # keep going: the remaining kinds cross-check this bind
        if len(hits) >= 2:
            # Two or more Plex items share this id: the same content in several copies.
            # The *arr item's own file name (and, if several listings carry that name,
            # its exact file size) may single out which copy this entry manages -- or
            # prove that several listings are the same file, bound together (see
            # _narrow_among_id_hits). The wider library is never consulted to break the
            # tie, and any residual ambiguity abstains.
            narrowed, text = _narrow_among_id_hits(
                hits,
                basename,
                file_size,
                index,
                file_path,
                root_folders,
                arr_layout_depth,
                plex_library,
            )
            if not narrowed:
                return Resolution.abstain(
                    f"Kept: {kind.upper()} id {value} names {len(hits)} Plex items "
                    f"(ambiguous), and {text}"
                )
            if len(narrowed) == 1:
                tier1 = (
                    narrowed[0],
                    MatchedBy.ID_AND_BASENAME,
                    f"Bound to Plex item by {kind.upper()} id {value} plus {text} "
                    f"({len(hits)} Plex items share the id)",
                    (),
                )
            else:
                # Byte-identical twins: one file, several listings. Bind the group under
                # the earliest listing; every key is kept so watch reads cover them all.
                tier1 = (
                    _earliest_listed(narrowed, index),
                    MatchedBy.MERGED_LISTINGS,
                    f"Bound by {kind.upper()} id {value} plus {text}: the same file is "
                    f"listed {len(narrowed)} times in Plex, so its watch history is read "
                    f"from all {len(narrowed)} listings",
                    tuple(sorted(narrowed)),
                )
            tier1_kind = kind
            # Keep going: the remaining kinds cross-check this bind.

    # -- Tier 2: file basename (only if no id bound) -------------------------
    tier2: int | None = None
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
    if tier1 is not None and tier3 in tier1[3]:
        # The title resolved to another listing of the very file the merged bind covers.
        # That is agreement with the group, not a contradiction with its canonical key.
        tier3 = tier1_rk
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
        rk, by, bind_detail, merged = tier1
        return Resolution.bound(index.by_rating_key[rk], by, bind_detail, merged=merged)
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
    file_size: int | None = None,
    file_path: str | None = None,
    root_folders: Sequence[str] | None = (),
    plex_library: str | None = None,
    index: PlexIndex,
) -> Resolution:
    """Bind a movie: id priority tmdb then imdb.

    ``file_size`` is Radarr's exact byte count for the movie's file, consulted only when
    several Plex listings carry the same file name under one shared id; ``file_path`` is
    that file's full path, consulted in the same place.

    ``root_folders`` is this Radarr instance's own root folder paths. Radarr reports a
    movie at ``<root>/<Title>/<file>``, so the root is what tells the real folder apart
    from the container's mount point. Supply none and the folder step stands down: a movie
    then leans on its exact byte size, and where that cannot separate the copies either,
    the item abstains and is kept. ``plex_library`` is the Plex library the operator mapped
    this movie's root folder to; when set it narrows an ambiguous id to that library ahead of
    folder and size, but a positive size contradiction still vetoes it (a movie keeps every
    corroborator it had).
    """
    return resolve(
        ids=ids,
        title=title,
        year=year,
        file_basename=file_basename,
        file_size=file_size,
        file_path=file_path,
        root_folders=root_folders,
        plex_library=plex_library,
        # Radarr writes <root>/<Title>/<file>: two segments below the root.
        arr_layout_depth=2,
        index=index,
        id_priority=_MOVIE_ID_PRIORITY,
    )


def resolve_show(
    *,
    ids: ExternalIds,
    title: str | None,
    year: int | None,
    file_basename: str | None,
    file_path: str | None = None,
    root_folders: Sequence[str] | None = (),
    plex_library: str | None = None,
    index: PlexIndex,
) -> Resolution:
    """Bind a show: id priority tvdb (Sonarr's primary key).

    No ``file_size``: a show is bound by its folder, and a folder has no one size -- so
    two same-name folder listings under one id can never be told apart by size, and never
    merge. ``file_path`` is the folder's full path; ``root_folders`` is this Sonarr instance's
    own root folder paths, without which that path cannot be read (:func:`_below_arr_root`).

    The *path* leaves a show no reach, and that is deliberate: Sonarr reports a series at
    ``<root>/<Show>``, so below the root there is only the show name -- the leaf both copies
    already matched on -- and a *deeper* path means the reported root is not this item's real
    root, which stands the folder step down too (``arr_layout_depth`` is 1 here). A show
    narrows by path in no layout, nested or flat, and inferring the copy from the roots' own
    names would mis-bind two instances that mount alike (see :func:`_narrow_by_path_depth`).

    ``plex_library`` is what changes that. It is not inference from the path but the operator's
    own declaration -- each root folder mapped, in Settings, to the Plex library its content
    lands in -- so it can tell one title kept in both an HD and a 4K library apart where the
    path never could. When it is not set, a show falls back to the old behavior: every
    ambiguous show abstains and is kept.
    """
    return resolve(
        ids=ids,
        title=title,
        year=year,
        file_basename=file_basename,
        file_size=None,
        file_path=file_path,
        root_folders=root_folders,
        plex_library=plex_library,
        # Sonarr writes <root>/<Show>: one segment below the root, which is the folder leaf
        # both sides already matched on. So the folder step has no new evidence for a show
        # and always stands down -- see _narrow_by_path_depth. The library map (plex_library)
        # is the one thing that can narrow a show, and it is the operator's declaration, not
        # the path.
        arr_layout_depth=1,
        index=index,
        id_priority=_SHOW_ID_PRIORITY,
    )
