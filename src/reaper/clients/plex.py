# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Plex Media Server client.

Wraps ``plexapi``, which is synchronous (it is ``requests`` underneath), so every call
is pushed off the event loop with ``asyncio.to_thread``.

We use ``plexapi`` rather than raw HTTP on purpose: it encodes the exact URI forms for
batch edits and hub promotion that other tools in this space have provably got wrong,
and those are the calls where being wrong means silently mangling the owner's library.

Three behaviors here were established against a live server, and each one contradicts
what you would reasonably assume:

**Labels are title-cased by Plex.** Write ``leaving-soon`` and read back
``Leaving-Soon``. Any case-sensitive comparison of label tags is therefore a latent bug
-- it will fail to find a label that is right there, and "I could not find the label I
wrote" turns into "add it again" or, worse, "this item is not marked, so it must be
safe to act on". Every comparison in this module is casefolded.

**``addLabel`` PRESERVES existing labels; it does not replace them.** Verified by adding
two labels in succession and reading back both. So Reaper's "Leaving Soon" mark does not
wipe whatever the owner had already put on their media. This was the single most
dangerous open question about the collection feature, and the answer is the safe one.

**A partial refresh takes a path.** ``section.update(path=...)`` rescans one directory;
``section.refresh()`` re-downloads metadata for the entire section. They are one word
apart and wildly different in cost.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode, urlsplit
from xml.etree.ElementTree import Element

import requests
import structlog

from reaper.clients.base import (
    SAFE_METHODS,
    SafetyViolationError,
    refuse_mutation,
    trace_call,
)
from reaper.config import RuntimeSafety
from reaper.engine.identity import PlexFile, PlexItem, parse_guids, to_basename
from reaper.ratings import Rating, from_plex
from reaper.text import fold

if TYPE_CHECKING:
    from plexapi.server import PlexServer

log = structlog.get_logger(__name__)

#: plexapi issues one PUT per chunk for a batch edit. 100 is Kometa's battle-tested size.
BATCH_SIZE = 100

#: One listing page of the GUID sweep. Big pages are strictly better here (the cost is
#: per item, not per page), bounded so a huge library never materialises in one response.
SWEEP_PAGE_SIZE = 1000

#: Hard stop on :func:`_iter_pages` (rule 56/89). Its one continuing path is a ``totalSize`` that
#: stays ahead of ``start`` on a non-empty page. ``totalSize`` is re-read from every response, so
#: the walk's length is the answering server's to choose. Nothing outside the loop bounds it. The
#: sweeps run under ``asyncio.to_thread``, which cannot be canceled, and
#: ``scan_runner._scan_running`` is cleared in a ``finally`` a loop that never returns never
#: reaches. Every later scan is then refused until the container restarts. What answers is not
#: always Plex either: a reverse proxy, an auth portal or a tunnel sits in that path.
#:
#: The bound is 1,000 PAGES, so the item count it covers falls with the page size the server
#: actually serves. A million at ``SWEEP_PAGE_SIZE`` rows, less against a server that clamps the
#: page below the request, which the loop follows to the end rather than truncating.
#:
#: Tripping it RAISES, which is where the three siblings differ. ``seerr.MAX_PAGES`` raises,
#: ``history_sync.MAX_HISTORY_PAGES`` stops short and warns, and ``library_index._SPINE_MAX_PAGES``
#: stops short, warns and degrades. Seerr is the one to match here. ``_iter_pages`` is
#: complete-or-raise, and every caller reads a protection source. Stopping short would return part
#: of a section as the whole of it, and the protection built on that read would cover only what
#: was read.
SWEEP_MAX_PAGES = 1_000

#: Rating keys per batched ``/library/metadata/{ids}`` read (Rating children + folder
#: paths). The ids ride in the URL path, so this is bounded by URL length, not by response
#: size: 400 keys is ~4 KB of comma-joined ids, comfortably under the usual ~8 KB limit,
#: and cuts a 10k-item enrichment from ~100 serial round-trips to ~25. Raise with care --
#: past a few thousand keys a server can answer 414, and the existing ``except`` maps that
#: to ``PlexError`` (the snapshot then degrades), which is safe but not free.
METADATA_BATCH_SIZE = 400

#: What :meth:`PlexClient.collection_tags` asks Plex to leave out of its metadata batch: it
#: reads one child element and the response carries every other one, which roughly halved the
#: batch where it was measured (docs/LEARNINGS.md). ``Collection`` is deliberately absent from
#: the exclusion. A server ignoring these params answers as before, and one that answered them
#: by dropping Collection children would leave every collection short of its ``childCount``,
#: which already sends the caller to the per-collection read.
_TAGS_ONLY = (
    "?excludeElements=Media,Genre,Country,Director,Writer,Producer,Role,Similar,Chapter,"
    "Marker,Extras,Related,Rating,Review,Image,UltraBlurColors,Guid"
    "&excludeFields=summary,tagline,titleSort"
)

#: Seconds Reaper waits for one Plex response, over plexapi's default of 30, which is a
#: budget for an idle server. A busy library can take longer than that to answer a section
#: sweep, and the read that times out is a protection source the scan then does without.
#: Still a bound: nothing here waits forever.
#:
#: ``PlexServer.query`` passes it on every call, so it covers the sweeps, the shelf reads and
#: the writes alike. The watchlist reads plex.tv through ``myPlexAccount()``, which plexapi
#: builds with no timeout, so that one call keeps the 30.
PLEX_READ_TIMEOUT = 60


def _parse_rating_children(el: Element) -> list[Rating]:
    """Per-provider scores from full-metadata ``Rating`` children.

    A section listing carries only two rating *slots*, so a library whose agent fills
    them with, say, IMDb can never show a Rotten Tomatoes score from the listing alone.
    The full metadata (``/library/metadata/{ids}``) carries one ``Rating`` child per
    provider score -- critic and audience separately -- each with the provenance image
    the rest of the codebase requires (measured on a live server, 2026-07-17:
    ``type="audience" image="rottentomatoes://image.rating.upright"`` is the audience
    score, ``type="critic" ... .ripe`` the Tomatometer; IMDb and TMDb arrive with
    ``type="audience"`` and their own images). Values are 0-10 like every Plex rating;
    ``from_plex`` applies the same provenance and range rules as the slot reads, so an
    unreadable child is dropped, never guessed at.
    """
    out: list[Rating] = []
    for child in el.findall("Rating"):
        rating = from_plex(
            child.get("value"),
            child.get("image"),
            audience=child.get("type") == "audience",
        )
        if rating is not None:
            out.append(rating)
    return out


def _parse_sweep_element(el: Element, *, library: str | None = None) -> PlexItem:
    """One listing row (a movie ``Video`` or show ``Directory``) as a :class:`PlexItem`.

    ``library`` is the title of the section this row was listed under -- the caller knows it
    per section and passes it in, since the listing element itself does not carry it.

    Pure attribute/child reads on already-fetched XML -- a field the server omitted is
    ``None``, never a reason for another request. Movies carry ``Media/Part`` children
    (file name + exact byte size, and the resolution); shows carry no files in a
    listing, so their ``files`` stay empty here and the folder-path batch in
    :meth:`PlexClient.library_guid_index` fills them in.
    """
    guid_ids = [str(g.get("id") or "") for g in el.findall("Guid")]
    legacy = el.get("guid")
    ids = parse_guids(guid_ids, legacy if legacy else None)

    files: list[PlexFile] = []
    video_resolution: str | None = None
    for media in el.findall("Media"):
        if video_resolution is None:
            raw_res = media.get("videoResolution")
            video_resolution = str(raw_res) if raw_res else None
        for part in media.findall("Part"):
            path = part.get("file")
            leaf = to_basename(path)
            if leaf is None:
                continue
            raw_size = part.get("size")
            size = int(raw_size) if raw_size and raw_size.isdigit() else None
            files.append(
                PlexFile(basename=leaf, size=size if size and size > 0 else None, path=path)
            )

    raw_year = el.get("year")
    raw_added = el.get("addedAt")
    raw_duration = el.get("duration")
    content_rating = el.get("contentRating")
    # Ratings with provenance: the *RatingImage tells us what each number IS (imdb, RT,
    # tmdb); a number whose image is missing is dropped by from_plex rather than guessed
    # at. The audience flag routes an RT image in the audience slot to the audience score.
    plex_ratings = [
        r
        for r in (
            from_plex(el.get("rating"), el.get("ratingImage")),
            from_plex(el.get("audienceRating"), el.get("audienceRatingImage"), audience=True),
        )
        if r is not None
    ]

    return PlexItem(
        rating_key=int(el.get("ratingKey") or 0),
        title=str(el.get("title") or ""),
        year=int(raw_year) if raw_year and raw_year.isdigit() else None,
        # addedAt in the container is a plain epoch, so the instant is exact regardless
        # of either machine's timezone.
        added_at=(
            datetime.fromtimestamp(int(raw_added), tz=UTC)
            if raw_added and raw_added.lstrip("-").isdigit() and int(raw_added) > 0
            else None
        ),
        ids=ids,
        file_basename=files[0].basename if files else None,
        files=tuple(files),
        video_resolution=video_resolution,
        content_rating=str(content_rating) if content_rating else None,
        runtime_minutes=(
            int(raw_duration) // 60_000
            if raw_duration and raw_duration.isdigit() and int(raw_duration) > 0
            else None
        ),
        ratings=tuple(plex_ratings),
        library=library,
    )


class PlexError(RuntimeError):
    """Plex could not be reached, or refused the operation."""


# ---------------------------------------------------------------------------
# The guard, again -- because plexapi does not speak httpx.
# ---------------------------------------------------------------------------
#
# ``GuardedTransport`` protects every integration that goes through our httpx clients.
# plexapi is ``requests``, so it would sail straight past it: label writes, collection
# edits and -- the one that matters -- ``emptyTrash`` would all be unguarded.
#
# An unmounted library plus a scan plus emptyTrash is
# how people lose an entire Plex library, and it would have been the single destructive
# call in the codebase with no safety interlock in front of it.
#
# So the same rule is enforced on the requests side: a mutating call requires deletion
# to be enabled AND an explicit declaration from the executor, which writes its intent
# to the durable journal before it declares. The declaration is a context variable
# rather than a per-request flag only because plexapi gives us nowhere to hang one.

_declared: ContextVar[bool] = ContextVar("reaper_plex_mutation_declared", default=False)

#: Marks the enclosed writes as the "Leaving Soon" shelf reconcile -- reversible
#: mutations that touch no files. Distinct from ``_declared`` so the two paths cannot be
#: confused: a deletion is armed + journalled; a shelf write is armed OR opted-in.
_benign_shelf: ContextVar[bool] = ContextVar("reaper_plex_benign_shelf", default=False)


#: Where a read that came back INCOMPLETE reports itself, without throwing away the part
#: it did read. The GUID sweep's batched metadata enrichment can be windowed by the server:
#: the ids stay complete, the ratings do not, and a title with no rating is one the rating
#: bar can no longer keep. That is a protection withdrawn, so it has to degrade the snapshot
#: (rule 28) -- but raising would discard a perfectly good id sweep on top of it and drop the
#: whole library to title-only matching, which is a much wider blast radius than the loss.
#:
#: A context variable rather than a parameter, for the same reason ``_declared`` is one: the
#: movie and show sweeps run as concurrent tasks over one client, and each task's copy of the
#: context keeps its own collector. The list is bound before those tasks are created, so
#: appends from inside them land in the collector that opened it.
_incomplete: ContextVar[list[str] | None] = ContextVar("reaper_plex_incomplete_reads", default=None)


@contextlib.contextmanager
def collecting_incomplete_reads() -> Iterator[list[str]]:
    """Collect, for this task, the reasons any read here came back short.

    Opened by ``services.library_index.build_index``, which degrades the snapshot for
    every reason collected. Outside a collector the reasons are logged and dropped, which
    is right for a one-off diagnostic read and never for a scan.
    """
    problems: list[str] = []
    token = _incomplete.set(problems)
    try:
        yield problems
    finally:
        _incomplete.reset(token)


def _report_incomplete(reason: str) -> None:
    sink = _incomplete.get()
    if sink is not None:
        sink.append(reason)


@contextlib.contextmanager
def declared_mutation() -> Iterator[None]:
    """Permit mutating Plex calls on this task, having journalled the intent first.

    Set **only** by the action executor. Anything else calling this is a bug, and the
    narrowness is the point: the window is one context manager wide, so a stray write
    from some future code path fails loudly instead of quietly succeeding.
    """
    token = _declared.set(True)
    try:
        yield
    finally:
        _declared.reset(token)


@contextlib.contextmanager
def benign_shelf_write() -> Iterator[None]:
    """Mark the enclosed writes as the "Leaving Soon" shelf reconcile.

    Set **only** by the Leaving Soon sync path, exactly as ``declared_mutation`` is set
    only by the executor -- the narrowness is what keeps this from becoming a general
    license to write. It tells :class:`GuardedSession` the write is the reversible,
    file-touching-nothing shelf (the label batch edit and the collection edits) rather
    than a deletion, so it may be permitted in read-only mode *if the operator turned on
    "Update while read-only" in Settings -> Plex*. It never permits a delete, and that is
    enforced structurally, not by call-site discipline: the benign branch matches only
    the exact shapes in ``_benign_shape``; any other verb or path inside this block
    falls back to the armed-and-declared rule. In particular ``DELETE
    /library/metadata/{key}`` -- the request that removes an item AND its files on a
    server allowing media deletion -- matches no benign shape and never can.
    """
    token = _benign_shelf.set(True)
    try:
        yield
    finally:
        _benign_shelf.reset(token)


#: GET-shaped mutations. Plex triggers a section scan with ``GET
#: /library/sections/{key}/refresh`` -- and on a server with "Empty trash automatically
#: after every scan" enabled, scanning a path with missing files purges those items'
#: library records. Method filtering alone would wave that straight through (the same
#: reason the Tautulli client carries a command allow-list), so the guard classifies
#: these paths as mutations regardless of verb.
_GET_SHAPED_MUTATIONS = re.compile(r"^/library/sections/[^/]+/refresh$")

#: The write shapes ``benign_shelf_write`` may permit, each matched by exact method AND
#: path so the benign branch can never widen:
#:
#: * the batch tag edit -- ``PUT /library/sections/{key}/all`` -- which carries BOTH the
#:   label add/remove and the collection-membership remove (a collection is a tag, so
#:   detaching a member is ``collection[].tag.tag-={name}`` on this same endpoint);
#: * creating a collection -- ``POST /library/collections``;
#: * adding items to one -- ``PUT /library/collections/{key}/items``;
#: * deleting a whole (emptied) collection -- ``DELETE /library/collections/{key}``.
#:
#: The whole-collection delete is how an emptied shelf disappears in ONE request instead of
#: one delete per member. ``DELETE /library/metadata/{key}`` -- the shape that deletes an
#: item (and, on a permissive server, its files) -- is a DIFFERENT path (``.../metadata/``,
#: not ``.../collections/``) and is deliberately NOT on this list and must never be added.
_LABEL_EDIT = re.compile(r"^/library/sections/[^/]+/all$")
_COLLECTION_CREATE = re.compile(r"^/library/collections$")
_COLLECTION_ADD = re.compile(r"^/library/collections/[^/]+/items$")
_COLLECTION_DELETE = re.compile(r"^/library/collections/[^/]+$")


def _benign_shape(method: str, path: str) -> bool:
    """Whether this request is one of the exact shelf-write shapes. Anything else --
    any other verb, any other path -- is not benign, whatever context it runs in."""
    verb = method.upper()
    if verb == "PUT":
        return bool(_LABEL_EDIT.match(path)) or bool(_COLLECTION_ADD.match(path))
    if verb == "POST":
        return bool(_COLLECTION_CREATE.match(path))
    if verb == "DELETE":
        return bool(_COLLECTION_DELETE.match(path))
    return False


class GuardedSession(requests.Session):
    """``GuardedTransport``'s twin, for the one library that does not use httpx."""

    def __init__(self, safety: RuntimeSafety, *, verify: bool = True) -> None:
        super().__init__()
        self._safety = safety
        # requests reads TLS verification off the session. Off is the operator's
        # explicit per-server choice (PlexServer.verify_tls), mirroring the *arr
        # clients' opt-out; the default stays on.
        self.verify = verify

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        path = urlsplit(url).path
        mutates = method.upper() not in SAFE_METHODS or bool(_GET_SHAPED_MUTATIONS.match(path))
        if mutates:
            if _benign_shelf.get() and _benign_shape(method, path):
                # The "Leaving Soon" shelf reconcile: reversible, touches no file, and
                # structurally confined to the label batch edit and the collection
                # edits (see _benign_shape). Gated like a delete by default (needs
                # arming); an operator may opt in to allowing it while read-only so the
                # grace-period warning can appear before deletion is ever enabled. This
                # branch can NEVER permit a deletion: a verb+path outside _benign_shape
                # does not match it and falls through to the armed-and-declared rule.
                if not self._safety.leaving_soon_write_allowed:
                    refuse_mutation(
                        "plex.write_blocked",
                        method,
                        path,
                        reason="shelf_not_allowed",
                        message=(
                            f"Blocked {method} to Plex (Leaving Soon shelf). Turn deletion "
                            'on, or turn on "Update while read-only" under Settings, Plex, '
                            "Leaving Soon to allow this reversible shelf write while "
                            "Reaper is read-only."
                        ),
                    )
            elif not self._safety.destructive_allowed:
                refuse_mutation(
                    "plex.write_blocked",
                    method,
                    path,
                    reason="not_armed",
                    message=f"Blocked {method} to Plex. {self._safety.why_blocked()}",
                )
            elif not _declared.get():
                refuse_mutation(
                    "plex.write_blocked",
                    method,
                    path,
                    reason="not_declared",
                    message=(
                        f"Blocked {method} to Plex: this mutation was not declared to the "
                        "action journal. Destructive calls must go through the action "
                        "executor so that they are recorded before they are sent."
                    ),
                )
        # Trace only what passed the guard and reached the wire; a blocked mutation raised
        # above and never happened. plexapi puts X-Plex-Token in the query string (rule 13),
        # so `path` -- already the token-free split above -- is what is logged, never `url`.
        started = time.monotonic()
        status: int | None = None
        try:
            response = super().request(method, url, *args, **kwargs)
            status = response.status_code
            return response
        finally:
            trace_call("plex", method, path, status, started, mutation=mutates)


def normalize_label(tag: str) -> str:
    """The comparison form for a label tag.

    Plex title-cases what you write: ``leaving-soon`` comes back as ``Leaving-Soon``.
    Comparing raw tags therefore silently fails to match a label that is present, so
    every comparison goes through here.
    """
    return fold(tag)


@dataclass(frozen=True)
class PlexSection:
    """One video library, as the pickers and the Leaving Soon reconcile see it."""

    key: int
    title: str
    kind: str
    """``"movie"`` or ``"show"`` -- Plex's own section types. Music and photo sections
    are never surfaced: Reaper has no business in them."""


@dataclass(frozen=True)
class PlexSectionPaths:
    """One library section and the folders it covers, addressed by its own key.

    The key rides along because everything downstream of this table -- the partial
    refresh, the item count, the trash purge -- addresses a section by key, never by
    title. Two libraries may share a title; only the key is unique.
    """

    key: int
    title: str
    locations: tuple[str, ...]


@dataclass(frozen=True)
class PlexSeasonRow:
    """One season as a listing sweep sees it: its number, its own rating key, its added-at.

    Grouped under its show's rating key by :meth:`PlexClient.library_season_index`. The
    added-at is carried raw as Plex reports it (an epoch string); ``from_epoch`` parses it.
    The season-number ambiguity policy (a number that appears twice under one show is
    dropped) is applied by the caller, in the one place it lives for both this sweep and the
    per-show fallback.
    """

    season_index: int | None
    rating_key: int
    added_at: str | None


@dataclass(frozen=True)
class PlexCollectionRow:
    """One collection as the section-level listing sees it: its rating key, title and
    Plex's own member count. Membership -- which items actually sit inside it -- is a
    separate read, :meth:`PlexClient.collection_children`, which already exists (rule 72):
    this row is the shelf's identity, not its contents.

    ``child_count`` is also what tells a caller that :meth:`PlexClient.collection_tags`
    did not see all of this collection, since Plex counts members it stores no tag for.
    """

    rating_key: int
    title: str
    child_count: int | None


#: Plex's numeric metadata types. The two the shelf works on -- movies in movie sections,
#: seasons in show sections -- plus the show itself, which is the level a TV collection
#: lists and therefore the level ``collection_tags`` reads a show library at.
_PLEX_TYPE_CODES = {"movie": 1, "show": 2, "season": 3}


def _iter_pages(server: Any, path: str, query: str, *, what: str) -> Iterator[list[Any]]:
    """Yield each raw page of one Plex listing, hardened against silent truncation.

    The single paging loop every listing read runs on, so none of them can drift (rule 72):
    the four section sweeps (``library_guid_index``, ``library_season_index``,
    ``labeled_in_section``, ``section_rating_keys``) through :func:`_iter_section_pages`, and
    the two shelf reads (``list_collections``, ``collection_children``) directly --
    ``find_collection`` searches ``list_collections``'s result rather than reading this a
    second time. ``query`` is the listing's own query string BEFORE the container-window
    params (``"?includeGuids=1"``, ``"?type=3&label=..."``, or ``""``); this appends the
    start/size.

    Pages and terminates on the RAW child count, never a filtered one: advancing or ending a
    listing on the count of children that survived a ``ratingKey`` filter would let one dropped
    child end it a page early. A child without a ``ratingKey`` is an anomaly the contract
    raises on (so the caller degrades), not a shorter page. ``totalSize`` is the sole paging
    authority -- a server that clamps a page below the requested size is still followed to the
    end, and a full page with no ``totalSize`` to bound it is failed closed rather than guessed
    to be the last. Never falls back ``totalSize`` -> ``size`` (rule 56).

    Bounded by :data:`SWEEP_MAX_PAGES`, which raises rather than returning short.
    """
    start = 0
    pages = 0
    joiner = "&" if query else "?"
    while True:
        container = server.query(
            f"{path}{query}"
            f"{joiner}X-Plex-Container-Start={start}&X-Plex-Container-Size={SWEEP_PAGE_SIZE}"
        )
        raw = list(container)
        pages += 1
        if any(el.get("ratingKey") is None for el in raw):
            # A child the paging math cannot advance over: not the container shape the loop
            # assumes. Raise so the caller falls back / degrades, never end on a filtered page.
            raise PlexError(
                f"{what} returned a child with no ratingKey; refusing to read the page as complete"
            )
        yield raw
        start += len(raw)
        total_attr = container.get("totalSize")
        if total_attr is not None:
            # totalSize is the authority: page until we have reached it, even if a page came
            # back short (a clamped page is followed, never truncated).
            if start >= int(total_attr):
                return
            if not raw:
                # start < total but the page was empty: no progress. Fail closed.
                raise PlexError(f"{what} stalled at {start} of {int(total_attr)}")
            if pages >= SWEEP_MAX_PAGES:
                # Only reachable while the reported total keeps outrunning ``start`` on full
                # pages, which is a server that is not advancing through the listing.
                raise PlexError(f"{what} never finished, after {start} items")
        elif len(raw) < SWEEP_PAGE_SIZE:
            # No totalSize to lean on: a short raw page is the last page.
            return
        else:
            # A full page with no totalSize -- we cannot tell whether more remains.
            raise PlexError(
                f"{what} returned a full page with no totalSize; "
                "refusing to guess it is the last page"
            )


def _iter_section_pages(
    server: Any, section_key: int, query: str, *, what: str
) -> Iterator[list[Any]]:
    """The section-sweep form of :func:`_iter_pages`: one section's ``/all`` listing."""
    yield from _iter_pages(
        server,
        f"/library/sections/{section_key}/all",
        query,
        what=f"{what} of section {section_key}",
    )


@dataclass(frozen=True)
class ActiveStream:
    """Something being watched *right now*."""

    rating_key: int
    parent_rating_key: int | None
    grandparent_rating_key: int | None
    user: str

    @property
    def veto_keys(self) -> set[int]:
        """Every key this stream should protect.

        An episode is playing, but the thing on disk that a season-prune would remove is
        its *season*, and the thing a show-delete would remove is its *show*. Vetoing
        only the episode's own rating key would let Reaper delete the season out from
        under someone who is watching it.
        """
        keys = {self.rating_key}
        if self.parent_rating_key:
            keys.add(self.parent_rating_key)
        if self.grandparent_rating_key:
            keys.add(self.grandparent_rating_key)
        return keys


class PlexClient:
    """Everything Reaper does to the media server itself."""

    def __init__(
        self, base_url: str, token: str, *, safety: RuntimeSafety, verify: bool = True
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._safety = safety
        self._verify = verify
        self._server: PlexServer | None = None
        # The scan runs the movie and show GUID sweeps concurrently with its other
        # gathers. Both sweeps ride ONE plexapi server (one requests.Session), and
        # requests does not promise a Session is safe to share across threads -- so the
        # sweeps take this lock and run one at a time, while still overlapping with the
        # Tautulli and *arr reads that dominate the wall clock.
        #
        # SCOPE, so nobody reads more into it than it does: it serializes the two SWEEPS
        # (``library_guid_index``, ``library_season_index``), the long multi-page walks a
        # scan issues in parallel. It is NOT a whole-client mutex. The Leaving Soon
        # reconcile deliberately runs its per-library passes concurrently
        # (``leaving_soon.sync_shelves``, bounded by ``SHELF_CONCURRENCY``), so several of
        # its shelf reads and writes can be in flight on this same session; that pass owns
        # its own client, touches no file, and taking this lock on its reads alone would
        # look like a fix while leaving its writes exactly as concurrent as before.
        self._sweep_lock = asyncio.Lock()
        # And the "one server" premise itself needs a lock: two concurrent callers both
        # seeing _server is None would each build a connection, leaving one session
        # leaked and the sweeps split across two sessions -- exactly what _sweep_lock
        # claims cannot happen.
        self._connect_lock = asyncio.Lock()

    async def connect(self) -> PlexServer:
        """The underlying plexapi server, connected once and reused.

        Exposed for the list providers, which speak plexapi directly. Reads through it
        still pass the GuardedSession -- which permits GETs and refuses writes -- so even
        a list refresh cannot mutate Plex.
        """
        return await self._connect()

    async def _connect(self) -> PlexServer:
        connected = self._server
        if connected is not None:
            return connected

        def build() -> PlexServer:
            from plexapi.server import PlexServer as _PlexServer

            # The guarded session, not plexapi's default. Every write plexapi makes
            # goes through it, so Plex is held to the same rule as everything else.
            # The timeout rides the server object, not the session: query passes its own
            # value explicitly, so a session default would be overridden.
            return _PlexServer(  # type: ignore[no-untyped-call]
                self._base_url,
                self._token,
                session=GuardedSession(self._safety, verify=self._verify),
                timeout=PLEX_READ_TIMEOUT,
            )

        async with self._connect_lock:
            # Re-checked under the lock: a concurrent caller may have connected while
            # this one waited, and building twice would leak a session.
            connected = self._server
            if connected is not None:
                return connected
            # Not through `_call`: its message would in fact be reproduced byte for byte by
            # `what=f"reach Plex at {self._base_url}"`, so this is a contract boundary and not
            # a mapping difference. `_call` describes one call against the connected server;
            # this is the call that runs before there is one.
            try:
                built = await asyncio.to_thread(build)
            except Exception as exc:
                raise PlexError(f"Could not reach Plex at {self._base_url}: {exc}") from exc
            self._server = built
            return built

    async def _call[T](self, work: Callable[[], T], *, what: str) -> T:
        """Run one synchronous plexapi call off the event loop, mapping its failures.

        plexapi is synchronous, so every call in this client is a closure handed to a worker
        thread. The failure mapping around it was written out at each one: a catch-all
        converting whatever plexapi raised into a :class:`PlexError` naming what was being
        attempted (rule 9/110), and, on every mutating method, an arm ahead of it letting the
        transport guard's refusal through unchanged.

        **The refusal arm is the reason this is a helper rather than a tidy-up.** A guard
        refusal is not a Plex failure: it means a mutating call was attempted without the host
        armed or without the intent journalled first, and re-labeling it ``PlexError`` tells the
        caller Plex is unwell and invites it to degrade. Eight write methods each carried that
        arm by hand, so a ninth written later inherits nothing. Here it is one declaration and a
        new write cannot arrive without it.

        ``asyncio.to_thread`` rather than a shared executor, and that is load-bearing: it copies
        the context, and the journalled-intent flag the guard reads is a
        :class:`~contextvars.ContextVar`. Under ``loop.run_in_executor`` with a bare executor
        the worker reads it as unset and every journalled write is refused. Fail-closed in both
        directions, and both executor call sites swallow the refusal into a warning, so it would
        break quietly.

        ``what`` completes the sentence ``Could not {what}: {exc}``, so it is a verb phrase and
        not a noun. ``_iter_pages`` in this same file takes a ``what`` that IS a noun, which is
        the one way to get ``Could not GUID sweep: ...`` out of this.

        **Five methods do not read through this and each says why in place**: ``_connect`` (the
        one call that runs before there is a server, so this contract does not describe it),
        ``active_streams`` (its message is the fail-closed reasoning, not a verb phrase),
        ``is_refreshing`` (it degrades to a warning rather than raising), ``aclose`` (no mapping
        at all, it is teardown), and ``trash_count`` (its own read raises ``PlexError``, which
        this would wrap a second time).
        """
        try:
            return await asyncio.to_thread(work)
        # Ahead of the catch-all, deliberately. `SafetyViolationError` and `PlexError` are
        # siblings off `RuntimeError`, neither derived from the other, so an `except PlexError`
        # downstream does NOT catch a refusal -- and this arm is what keeps it that way. It must
        # reach the caller as itself; `clients/base.refuse_mutation` has already logged it at
        # the point of refusal.
        except SafetyViolationError:
            raise
        except Exception as exc:
            raise PlexError(f"Could not {what}: {exc}") from exc

    # -- reading -----------------------------------------------------------

    async def active_streams(self) -> list[ActiveStream]:
        """What is playing right now.

        Re-polled immediately before **every** delete, not once at the start of a run: a
        run takes minutes, and somebody can start watching in the middle of it. No
        competitor does this, and it is the cheapest possible protection against the
        worst possible outcome -- deleting a file out from under a person watching it.
        """
        server = await self._connect()

        def read() -> list[ActiveStream]:
            streams: list[ActiveStream] = []
            for session in server.sessions():  # type: ignore[no-untyped-call]
                usernames = list(getattr(session, "usernames", None) or [])
                streams.append(
                    ActiveStream(
                        rating_key=int(session.ratingKey),
                        parent_rating_key=(
                            int(session.parentRatingKey)
                            if getattr(session, "parentRatingKey", None)
                            else None
                        ),
                        grandparent_rating_key=(
                            int(session.grandparentRatingKey)
                            if getattr(session, "grandparentRatingKey", None)
                            else None
                        ),
                        user=str(usernames[0]) if usernames else "unknown",
                    )
                )
            return streams

        # Not through `_call`: the message below is the fail-closed reasoning itself, not a
        # verb phrase, and this is the last read before an irreversible act.
        try:
            return await asyncio.to_thread(read)
        except Exception as exc:
            # Fail CLOSED. If we cannot tell whether anyone is watching, we must not
            # conclude that nobody is -- that is precisely the Unknown-vs-Absent
            # distinction the whole engine is built on, applied to the last check
            # before an irreversible act.
            raise PlexError(
                f"Could not read active sessions from Plex ({exc}). Refusing to delete: "
                "not being able to see who is watching is not the same as nobody watching."
            ) from exc

    async def section_paths(self) -> list[PlexSectionPaths]:
        """Every library section and the paths it covers, each carrying its own key.

        The raw material for the path-mapping table. Radarr says ``/movies``; Plex says
        ``/media/movies``. The partial-refresh path comes from the *arr, so without a
        mapping the refresh silently rescans nothing at all.

        A **list keyed by section key**, not a ``{title: paths}`` dict: two Plex libraries
        may legally share a title, and a title-keyed map silently drops one of them
        (rule 6/57). The dropped library's post-reap refresh would then map to nothing --
        the exact "silently rescans nothing at all" this table exists to prevent -- and the
        trash interlock behind it would compare against a library nobody deleted from.

        Failures map to ``PlexError`` like every other read here. Plex can answer the
        connect handshake and still fail this one path (a restart between the two calls, a
        proxy 502), and the raw plexapi exception would escape the executor's ``except
        PlexError`` mid-run: the file already deleted, its journal step stuck at SENT, the
        run stuck EXECUTING, and every remaining approved deletion never attempted.
        """
        server = await self._connect()

        def read() -> list[PlexSectionPaths]:
            return [
                PlexSectionPaths(
                    key=int(s.key), title=str(s.title or ""), locations=tuple(s.locations)
                )
                for s in server.library.sections()
            ]

        return await self._call(read, what="read Plex section paths")

    async def library_guid_index(
        self, *, section_type: str, allowed_sections: set[int] | None = None
    ) -> dict[int, PlexItem]:
        """Every library item as the resolver sees it, keyed by Plex ``rating_key``.

        The enrichment behind id-based matching. For each library section of
        ``section_type`` (``"movie"`` or ``"show"``), page the section's ``/all``
        listing (with ``includeGuids=1``) and parse the container XML directly: the
        new-agent ``Guid`` children *and* the legacy single ``guid`` attribute, plus
        **every** file behind the listing with its name and exact byte size (the first
        location's leaf feeds the global basename tier; the full set exists so an
        ambiguous id can be narrowed against all of a candidate's files, and
        byte-identical re-lists of one file can be recognized), the display metadata
        (ratings with provenance, certification, runtime, resolution), and the
        title / year / added-at the listing already carries.

        Parsed RAW, not through ``section.all()``, and the reason is measured, not
        stylistic: plexapi reloads a partial object the first time any accessed
        attribute is ``None``, and on a listing row some attribute always is -- so the
        object walk silently issued one metadata request *per item* and the "single
        sweep" cost minutes on a large library. Reading the container XML makes a
        missing attribute honestly ``None`` with no hidden network call, and the sweep
        is a handful of page requests. Two things ``/all`` never carries are filled in
        by batched ``/library/metadata/{ids}`` reads (100 per call, so ~1 request per
        100 items): show ``Location`` folder paths -- the folder-name tier that narrows
        a show listed in two sections -- and, for movies and shows alike, the
        per-provider ``Rating`` children (``includeRatings=1`` on a listing returns
        nothing; measured). The listing's two rating slots cannot carry a provider's
        critic AND audience score, so without the children a library whose slots hold
        IMDb would never surface a Rotten Tomatoes number at all. Returning full
        :class:`PlexItem` rows (not just the ids) lets the index builders union in
        items the Tautulli media-info cache has not listed yet -- a freshly added item
        exists here first.

        GETs only, so it runs in read-only mode through the ``GuardedSession``. It
        **raises** ``PlexError`` on any failure rather than returning a partial map, so
        the caller can fail closed and degrade the snapshot: silently falling the whole
        library back to title-only matching at the moment the id signal vanished is
        exactly the fail-open this feature exists to prevent. The paging runs through
        the one hardened ``_iter_section_pages`` loop (raw-count advance, ``totalSize`` the
        sole authority, a truncated or unbounded page raised on), so a section can never
        end early with a silently partial map.

        The *enrichment* read reports the same class of loss without throwing the whole
        sweep away: a batched metadata read that comes back short files one reason with
        :func:`collecting_incomplete_reads`, which ``services.library_index.build_index``
        opens and degrades the snapshot from. The ratings that read carries are a
        PROTECTION, and losing them quietly makes titles more deletable, not less
        (rule 28).
        """
        server = await self._connect()
        # Filled inside the worker thread, read back on the event loop below: a short
        # metadata batch is a lost protection source, so the caller has to hear about it.
        short_batches: list[tuple[int, int]] = []

        def read() -> dict[int, PlexItem]:
            out: dict[int, PlexItem] = {}
            batch_keys: list[int] = []
            for section in server.library.sections():
                if section.type != section_type:
                    continue
                # Only the libraries the operator included in scans (Settings -> Plex). None
                # means every library of this type; a set scopes the sweep. Filtered on the
                # SAME section keys the Tautulli spine uses (services.library_index), so the
                # two never disagree about which sections were read.
                if allowed_sections is not None and int(section.key) not in allowed_sections:
                    continue
                section_title = str(section.title) if section.title else None
                # Hardened, complete-or-raise paging (see _iter_section_pages): every child in
                # every page carries a ratingKey, so the loop reads them straight off ``raw``.
                for raw in _iter_section_pages(
                    server, int(section.key), "?includeGuids=1", what="GUID sweep"
                ):
                    for el in raw:
                        item = _parse_sweep_element(el, library=section_title)
                        out[item.rating_key] = item
                        batch_keys.append(item.rating_key)

            # The batched metadata reads: show Location folders (each leaf becomes the
            # show's basename exactly as the object walk produced it), and the Rating
            # children for every item. The slot ratings from the listing keep precedence;
            # children only add sources the slots did not carry, so a server whose slots
            # and children disagree keeps the value the rest of the scan already froze.
            for chunk_start in range(0, len(batch_keys), METADATA_BATCH_SIZE):
                chunk = batch_keys[chunk_start : chunk_start + METADATA_BATCH_SIZE]
                batch = list(
                    server.query(  # type: ignore[no-untyped-call]
                        "/library/metadata/" + ",".join(str(k) for k in chunk)
                    )
                )
                if len(batch) < len(chunk):
                    # A server that windows the multi-id response drops the tail of the
                    # chunk, and those Rating children are the ONLY source of the
                    # per-provider scores (for shows, of any score at all). Losing them
                    # takes the keep bar off every title in the tail, which is a
                    # protection WITHDRAWN -- so this is one of rule 28's source failures
                    # and it degrades, through ``_report_incomplete`` below, which reaches the scan
                    # via the ``collecting_incomplete_reads`` sink the caller opens.
                    #
                    # Reported rather than raised, deliberately, and this is the narrower
                    # control: raising here would throw away a complete id sweep as well,
                    # dropping the whole library to title-only matching on top of the
                    # degradation. The map returned is exactly as complete as it was; the
                    # snapshot simply cannot be executed.
                    log.warning(
                        "plex.metadata_batch_short",
                        section_type=section_type,
                        requested=len(chunk),
                        returned=len(batch),
                    )
                    short_batches.append((len(chunk), len(batch)))
                for el in batch:
                    rk = el.get("ratingKey")
                    if rk is None or int(rk) not in out:
                        continue
                    item = out[int(rk)]

                    known = {r.source for r in item.ratings}
                    extra: list[Rating] = []
                    for rating in _parse_rating_children(el):
                        if rating.source not in known:
                            known.add(rating.source)
                            extra.append(rating)
                    if extra:
                        item = replace(item, ratings=item.ratings + tuple(extra))

                    paths = [loc.get("path") for loc in el.findall("Location") if loc.get("path")]
                    # The leaf feeds the global by_basename map; the FULL path rides along
                    # on each PlexFile because a show folder has no size, so the segments
                    # above the leaf are the only thing that can separate the same title
                    # listed in two sections (identity._narrow_by_path_depth).
                    located = [(to_basename(p), p) for p in paths]
                    leaves = [(leaf, p) for leaf, p in located if leaf]
                    if leaves:
                        item = replace(
                            item,
                            file_basename=leaves[0][0],
                            files=item.files
                            or tuple(PlexFile(basename=leaf, path=p) for leaf, p in leaves),
                        )
                    out[int(rk)] = item
            return out

        async with self._sweep_lock:
            out = await self._call(read, what=f"sweep Plex GUIDs for {section_type} sections")

        # Back on the event loop, with the worker thread joined, so the collector is
        # appended to from one place. One reason for the whole sweep, however many chunks
        # came back short.
        if short_batches:
            requested = sum(size for size, _ in short_batches)
            returned = sum(got for _, got in short_batches)
            what = "movie" if section_type == "movie" else "TV"
            _report_incomplete(
                f"Plex sent back ratings for only {returned} of {requested} {what} titles, so "
                "some were judged without a rating"
            )
        return out

    async def item_count(self, section_key: int) -> int:
        """How many items a section holds. The input to the trash interlock: the executor
        reads this before its first delete and again before purging, and refuses the purge
        unless the section shrank by no more than what the run deleted under it.

        Addressed by **key** via ``sectionByID`` (rule 6/57). A by-title lookup returns
        whichever of two same-titled libraries plexapi saw first, so the interlock that is
        meant to catch an over-large trash purge would be comparing the wrong library's
        size against this run's deletions.
        """
        server = await self._connect()

        def read() -> int:
            return int(server.library.sectionByID(section_key).totalSize)

        return await self._call(read, what=f"count items in section {section_key}")

    async def trash_count(self, section_key: int) -> int:
        """How many items are sitting in one section's trash, waiting to be purged.

        What the operator is warned about before a reap. ``empty_trash`` is section-wide
        (``PUT /library/sections/{key}/emptyTrash``), so it destroys the library records --
        watch history, ratings, collection membership -- of everything already in there,
        not just what this run deleted. Items trashed BEFORE a run are in the same state
        before and after it, so they cancel out of the executor's count-delta gate and it
        cannot see them. This read is the only thing that can.

        A **count** read, not a listing read: the container window is zero-sized and only
        ``totalSize`` is taken, so rule 89's complete-or-raise paging does not apply (same
        shape as :meth:`item_count`, which reads ``totalSize`` off the section).

        ``trash=1`` is a real Plex filter, confirmed against a live server against a
        control: an unknown parameter comes back with the FULL library count, while
        ``trash=1`` narrows. That control is why the caller can read 0 as "genuinely
        nothing in the trash" rather than "the server ignored me". A server that DOES
        ignore it returns the whole library, which the caller detects by comparing against
        :meth:`item_count` and treats as unreadable rather than as a real count.

        Raises ``PlexError`` on any failure (rule 110). The caller treats that as Unknown
        and warns, never as "nothing there" (rule 93): an unreadable trash is exactly the
        case where the operator most needs telling.
        """
        server = await self._connect()
        # ``query`` is untyped in plexapi, same as it is for ``_iter_pages``.
        raw_server: Any = server

        def read() -> int:
            container = raw_server.query(
                f"/library/sections/{section_key}/all?trash=1"
                "&X-Plex-Container-Start=0&X-Plex-Container-Size=0"
            )
            raw = container.get("totalSize")
            if raw is None:
                # No totalSize to read means the answer is not a count. Fail closed rather
                # than fall back to ``size`` (rule 56) or guess at zero.
                raise PlexError(f"trash listing for section {section_key} reported no totalSize")
            return int(raw)

        # Not through `_call`: `read` raises `PlexError` itself on a missing totalSize, and
        # `_call` would wrap that into a second one naming the wrong thing.
        try:
            return await asyncio.to_thread(read)
        except PlexError:
            raise
        except Exception as exc:
            raise PlexError(f"Could not count the trash in section {section_key}: {exc}") from exc

    async def empties_trash_after_scan(self) -> bool:
        """Does this server empty a section's trash by itself after every scan?

        Plex's ``autoEmptyTrash`` preference, and it is server-wide, not per-library. It
        ships **on by default**, which is why this is worth surfacing: when it is on, Plex
        purges the trash itself after each scan Reaper's path refresh triggers, so the
        executor's own trash interlock (the count-delta gate, the mount check, the settle
        wait) never gets a say. The purge has already happened, inside Plex.

        Raises ``PlexError`` if the preference cannot be read (rule 110), which the caller
        reports as unknown rather than as "no".
        """
        server = await self._connect()

        def read() -> bool:
            return bool(server.settings.get("autoEmptyTrash").value)

        return await self._call(read, what="read the empty-trash-after-scan setting")

    async def aclose(self) -> None:
        """Close the underlying plexapi session, if one was ever built.

        plexapi rides one ``requests.Session`` (built in ``_connect``); nothing but GC
        reclaims its pooled sockets unless this is called. Safe when never connected,
        and safe to call twice.
        """
        server, self._server = self._server, None
        if server is None:
            return

        def close() -> None:
            session = getattr(server, "_session", None)
            if session is not None:
                session.close()

        # Not through `_call`: teardown maps nothing, because there is no caller left to tell.
        await asyncio.to_thread(close)

    async def __aenter__(self) -> PlexClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def is_refreshing(self, section_key: int) -> bool:
        """Is a scan currently running on this section?

        A refresh (``section.update(path=...)``) fires an asynchronous scan; the executor
        polls this so it does not empty the trash before Plex has actually noticed the
        deleted file is gone. A read, addressed by **key** via ``sectionByID`` (rule 6/57)
        so it cannot report on the wrong one of two same-titled libraries. On any error it
        reports ``True`` (still busy), so the caller waits rather than racing ahead."""
        server = await self._connect()

        def read() -> bool:
            section = server.library.sectionByID(section_key)
            section.reload()
            return bool(getattr(section, "refreshing", False))

        # Not through `_call`: this one degrades to a warning and a conservative True rather
        # than raising, so there is no `PlexError` for a shared mapping to build.
        try:
            return await asyncio.to_thread(read)
        except Exception as exc:
            log.warning("plex.refresh_state_unreadable", section=section_key, error=str(exc))
            return True

    async def labeled_in_section(self, section_key: int, *, kind: str, label: str) -> set[int]:
        """The rating keys in one section carrying ``label``, at the level the shelf
        works on: movies in a movie section, seasons in a show section.

        A raw container read rather than ``section.search`` so a show section filters at
        the season level (``type=3``): the object walk searches shows by default, and a
        label sitting on a season would be invisible to it -- which would re-add the
        label forever. GETs only. Paged through the one hardened ``_iter_section_pages`` loop
        (raw-count advance, ``totalSize`` the sole authority), so a partial page can never
        under-report the labeled set and re-add a label the item already carries.
        """
        code = _PLEX_TYPE_CODES["movie" if kind == "movie" else "season"]
        server = await self._connect()

        def read() -> set[int]:
            keys: set[int] = set()
            for raw in _iter_section_pages(
                server, section_key, f"?type={code}&label={quote(label)}", what="label read"
            ):
                keys.update(int(el.get("ratingKey") or 0) for el in raw)
            return keys

        return await self._call(read, what=f"read items labeled {label!r} in section {section_key}")

    async def video_sections(self) -> list[PlexSection]:
        """Every movie and show library on the server, for the library picker.

        A read. Music, photo and other section types are filtered out here, not in the
        UI: Reaper has no feature that touches them, so they are never even offered.
        """
        server = await self._connect()

        def read() -> list[PlexSection]:
            return [
                PlexSection(key=int(s.key), title=str(s.title), kind=str(s.type))
                for s in server.library.sections()
                if s.type in ("movie", "show")
            ]

        return await self._call(read, what="list Plex libraries")

    async def section_rating_keys(self, section_key: int, *, kind: str) -> set[int]:
        """Every rating key in one section, at the level the shelf works on: movies in a
        movie section (``type=1``), seasons in a show section (``type=3``).

        This is what scopes the reconcile per library: the grace list's rating keys are
        intersected with this set, so an item is only ever marked in the section it
        actually lives in. A read, paged through the one hardened ``_iter_section_pages`` loop
        (raw-count advance, ``totalSize`` the sole authority) so a partial page can never
        shrink the section and leave a marked item unmatched by the reconcile.
        """
        code = _PLEX_TYPE_CODES["movie" if kind == "movie" else "season"]
        server = await self._connect()

        def read() -> set[int]:
            keys: set[int] = set()
            for raw in _iter_section_pages(
                server, section_key, f"?type={code}", what="section listing"
            ):
                keys.update(int(el.get("ratingKey") or 0) for el in raw)
            return keys

        return await self._call(read, what=f"list section {section_key}")

    async def library_season_index(
        self, *, allowed_sections: set[int] | None = None
    ) -> dict[int, list[PlexSeasonRow]]:
        """Every season in the show libraries, grouped under its show's rating key.

        The bulk replacement for a per-show ``get_children_metadata`` call: one paged
        ``type=3`` sweep of each show section yields every season at once, each carrying its
        number (``index``), its own rating key, its show (``parentRatingKey``) and its own
        added-at. Reading seasons this way is possible straight from Plex even though
        Tautulli has no season-sweep command, and the keys are identical: both address the
        same linked server, so the season key a sweep returns equals the one the per-show
        call returns and joins the same watch history.

        ``allowed_sections`` scopes the sweep to the show libraries the operator included in
        scans, the SAME set the GUID sweep and the Tautulli spine filter on, so the season
        keys join the same items those listed. Read RAW like the GUID sweep -- container XML,
        no object walk, GETs only -- and under the same ``_sweep_lock``, since it shares the
        one requests session with the movie and show GUID sweeps.

        **Raises** ``PlexError`` on any failure rather than returning a partial map: a caller
        reading a truncated season sweep as complete would let a real season's watch history
        go unread and unprotect it. The caller falls back per show for anything absent here.

        Paged through the one hardened ``_iter_section_pages`` loop: the RAW child count drives
        the paging, ``totalSize`` is the sole authority (a clamped page is followed to the end),
        and a child without a ``ratingKey`` or a full page with no ``totalSize`` raises so every
        show falls back per show rather than a section ending a page early.
        """
        server = await self._connect()
        code = _PLEX_TYPE_CODES["season"]

        def read() -> dict[int, list[PlexSeasonRow]]:
            out: dict[int, list[PlexSeasonRow]] = {}
            for section in server.library.sections():
                if section.type != "show":
                    continue
                # The same scope filter the GUID sweep uses (services.library_index), so the
                # two never disagree about which sections were read.
                if allowed_sections is not None and int(section.key) not in allowed_sections:
                    continue
                for raw in _iter_section_pages(
                    server, int(section.key), f"?type={code}", what="season sweep"
                ):
                    for el in raw:
                        parent = el.get("parentRatingKey")
                        if parent is None:
                            # A season with no show cannot be attributed to one. Skipped, so
                            # it abstains, never guessed onto the wrong show.
                            continue
                        try:
                            show_rk = int(parent)
                            rk = int(el.get("ratingKey") or 0)
                        except (TypeError, ValueError):
                            continue
                        raw_index = el.get("index")
                        try:
                            index = int(raw_index) if raw_index is not None else None
                        except (TypeError, ValueError):
                            index = None
                        out.setdefault(show_rk, []).append(
                            PlexSeasonRow(
                                season_index=index,
                                rating_key=rk,
                                added_at=el.get("addedAt"),
                            )
                        )
            return out

        async with self._sweep_lock:
            return await self._call(read, what="sweep Plex seasons")

    async def list_collections(self, section_key: int) -> list[PlexCollectionRow]:
        """Every collection in one section: its rating key, title and Plex's own child count.

        Paged through the one hardened ``_iter_pages`` loop over
        ``/library/sections/{key}/collections``. :meth:`find_collection` searches this
        method's result rather than running its own second loop over the same path (rule
        72). Complete-or-raise like every other listing (rule 56): a truncated page is
        never read as the whole shelf.

        Collections are navigation, never protection (the fence in
        ``docs/history/COLLECTIONS_PLAN.md``), so this method does not degrade anything itself --
        it either reports the section honestly or raises. A caller that wants "missing
        chip" rather than "broken scan" out of a Plex hiccup catches the raise itself.
        """
        server = await self._connect()

        def read() -> list[PlexCollectionRow]:
            rows: list[PlexCollectionRow] = []
            for raw in _iter_pages(
                server,
                f"/library/sections/{section_key}/collections",
                "",
                what=f"collection list of section {section_key}",
            ):
                for el in raw:
                    raw_count = el.get("childCount")
                    rows.append(
                        PlexCollectionRow(
                            rating_key=int(el.get("ratingKey")),
                            title=str(el.get("title") or ""),
                            child_count=int(raw_count) if raw_count is not None else None,
                        )
                    )
            return rows

        return await self._call(read, what=f"list collections in section {section_key}")

    async def find_collection(self, section_key: int, name: str) -> int | None:
        """The rating key of the collection called ``name`` in one section, or ``None``.

        Searches :meth:`list_collections`'s result rather than paging
        ``/library/sections/{key}/collections`` a second time (rule 72), so it inherits
        that method's paging and completeness guarantees. Matched casefolded, because
        Plex title-cases tags and titles on the way in.

        A row ``list_collections`` cannot assign a rating key to now raises instead of
        being silently skipped as it once was: returning ``None`` here reads to the
        caller as "no such collection," and the caller then CREATES a second "Leaving
        Soon" collection, splitting the shelf. Raising degrades honestly instead.
        """
        wanted = normalize_label(name)
        for row in await self.list_collections(section_key):
            if normalize_label(row.title) == wanted:
                return row.rating_key
        return None

    async def collection_children(self, collection_key: int) -> set[int]:
        """The rating keys currently on one collection. A read.

        Paged through the one hardened ``_iter_pages`` loop (rule 72), so a truncated
        response can never be read as the whole membership: the reconcile computes the
        items to DETACH as ``current - wanted``, so a short read leaves stale titles
        marked "Leaving Soon" long after they were reprieved.
        """
        server = await self._connect()

        def read() -> set[int]:
            keys: set[int] = set()
            for raw in _iter_pages(
                server,
                f"/library/collections/{collection_key}/children",
                "",
                what=f"members of collection {collection_key}",
            ):
                keys.update(int(el.get("ratingKey")) for el in raw)
            return keys

        return await self._call(read, what=f"read collection {collection_key}")

    async def collection_tags(self, section_key: int, *, kind: str) -> dict[int, tuple[str, ...]]:
        """Every item's collection names in one section, keyed by rating key.

        One request per 400 items, where asking each collection for its children costs one
        per COLLECTION. A Plex read costs about the same whatever it returns, so a library
        holding hundreds of collections spends the difference on per-request overhead alone
        (measured in docs/LEARNINGS.md).

        A "dumb" collection's membership IS each member's ``collection`` tag, which is what
        :meth:`remove_collection_members` already writes through, and the full metadata read
        carries those tags. **The section LISTING does not**, which is why this pays for a
        second read rather than parsing the sweep's pages: it reports some of an item's tags
        and drops the rest. Never a wrong tag, only a missing one, which would have shipped
        as titles quietly losing their chip.

        A collection Plex reports MORE members for than this returns is one whose membership
        is not tags: a smart collection is a saved filter, and a collection of seasons or
        episodes holds objects this section-level listing never lists. The caller compares
        against ``child_count`` and reads those the per-collection way
        (``services.snapshot._collection_membership``); this method reports what the tags say
        and nothing more.

        Collections are navigation, never protection (the fence in
        ``docs/history/COLLECTIONS_PLAN.md``), so a short metadata chunk is logged and the
        rest is returned rather than raised on -- the cost is a missing chip, and rule 28
        binds evidence sources. The key LISTING is still complete-or-raise through
        ``_iter_section_pages``, so a truncated page can never quietly shrink the section.
        """
        code = _PLEX_TYPE_CODES["movie" if kind == "movie" else "show"]
        server = await self._connect()

        def read() -> dict[int, tuple[str, ...]]:
            keys = [
                int(el.get("ratingKey") or 0)
                for raw in _iter_section_pages(
                    server, section_key, f"?type={code}", what="collection tag listing"
                )
                for el in raw
            ]
            out: dict[int, tuple[str, ...]] = {}
            for start in range(0, len(keys), METADATA_BATCH_SIZE):
                chunk = keys[start : start + METADATA_BATCH_SIZE]
                batch = list(
                    server.query(  # type: ignore[no-untyped-call]
                        "/library/metadata/" + ",".join(str(k) for k in chunk) + _TAGS_ONLY
                    )
                )
                if len(batch) < len(chunk):
                    log.warning(
                        "plex.collection_tags_short",
                        section=section_key,
                        requested=len(chunk),
                        returned=len(batch),
                    )
                for el in batch:
                    rk = el.get("ratingKey")
                    names = tuple(
                        str(c.get("tag") or "") for c in el.findall("Collection") if c.get("tag")
                    )
                    if rk is not None and names:
                        out[int(rk)] = names
            return out

        # Deliberately NOT under ``_sweep_lock``, which the two GUID sweeps take: this is one
        # request at a time where the fan-out it replaces was eight, so it adds a single
        # reader beside them, and serializing it would add its whole cost to the index phase
        # rather than overlapping it.
        return await self._call(read, what=f"read collection tags in section {section_key}")

    # -- writing -----------------------------------------------------------
    #
    # Everything below mutates Plex, so everything below requires ``declared_mutation()``
    # and an armed instance. GuardedSession enforces both; these methods cannot opt out.

    async def add_label(self, section_key: int, rating_keys: list[int], label: str) -> None:
        """Add a label to many items in one read plus one edit per chunk.

        The section is addressed by **key** via ``sectionByID``, never by title: two libraries
        can share a title and plexapi's title lookup returns the last match, so a title-addressed
        write would target the wrong library every pass (rule 6/57, mirroring the collection
        detach). The caller has the key already.

        **Verified against a live server: this PRESERVES existing labels.** Adding a
        second label leaves the first in place, so Reaper's "Leaving Soon" mark does not
        wipe whatever the owner had already put on their own media. That was the single
        most dangerous open question about this feature and the answer is the safe one.

        It is **assumed at runtime, not re-checked**: this method adds the label and does
        not read the item back. Said plainly because the docstring used to claim an
        assertion that was never written (rules 7/24), which is worse than no check at all
        -- a reader would have believed a future Plex release could not silently take the
        owner's own labels away. Re-reading every edited item to prove it is a cost the
        shelf write does not carry today; if that property is ever to be enforced rather
        than trusted, the read-back belongs here.
        """
        if not rating_keys:
            return
        server = await self._connect()

        def write() -> None:
            section = server.library.sectionByID(section_key)
            for start in range(0, len(rating_keys), BATCH_SIZE):
                keys = rating_keys[start : start + BATCH_SIZE]
                # ONE /library/metadata/<id,id,...> read for the whole chunk, not one GET
                # per item: fetchItem and fetchItems hit the SAME endpoint, and a list of
                # ids becomes a single multi-id path. The batch edit itself is byte-identical,
                # so Plex's additive label write -- which PRESERVES existing labels, the
                # verified property above -- is exactly as before. An item removed from Plex
                # since the scan simply does not come back from the read and is skipped, never
                # a failed reconcile.
                items = section.fetchItems(keys)
                if items:
                    section.batchMultiEdits(items).addLabel(label).saveMultiEdits()

        await self._call(write, what=f"add label {label!r}")

    async def remove_label(self, section_key: int, rating_keys: list[int], label: str) -> None:
        """Remove a label from many items.

        The section is addressed by **key** via ``sectionByID``, never by title (rule 6/57),
        exactly as :meth:`add_label` and the collection detach do -- two libraries can share a
        title and the title lookup returns only the last match.

        Matched case-insensitively against what Plex actually stored, because it will
        have title-cased the tag on the way in. A case-sensitive removal silently removes
        nothing -- which, for a "Leaving Soon" mark, means the item stays flagged to
        every user long after it was reprieved.
        """
        if not rating_keys:
            return
        server = await self._connect()
        wanted = normalize_label(label)

        def write() -> None:
            section = server.library.sectionByID(section_key)
            for start in range(0, len(rating_keys), BATCH_SIZE):
                keys = rating_keys[start : start + BATCH_SIZE]
                # ONE multi-id read for the chunk -- the metadata carries each item's labels,
                # so there is no per-item fetch and no per-item reload. Only items that
                # actually carry the label are edited, grouped by the exact spelling Plex
                # stored (so removal stays case-correct against Plex's title-casing), which
                # collapses to one write per spelling per chunk -- in practice one -- instead
                # of one write per item.
                by_spelling: dict[str, list[Any]] = {}
                for item in section.fetchItems(keys):
                    for existing in item.labels:
                        if normalize_label(str(existing.tag)) == wanted:
                            by_spelling.setdefault(str(existing.tag), []).append(item)
                            break

                for actual_tag, group in by_spelling.items():
                    section.batchMultiEdits(group).removeLabel(actual_tag).saveMultiEdits()

        await self._call(write, what=f"remove label {label!r}")

    @staticmethod
    def _metadata_uri(machine_identifier: str, rating_keys: list[int]) -> str:
        """The ``server://`` URI form Plex's collection endpoints take items in."""
        ids = ",".join(str(k) for k in rating_keys)
        return f"server://{machine_identifier}/com.plexapp.plugins.library/library/metadata/{ids}"

    async def create_collection(
        self, section_key: int, *, kind: str, name: str, rating_keys: list[int]
    ) -> int | None:
        """Create a collection holding ``rating_keys`` and return its rating key.

        The write behind the first shelf in a library. ``POST /library/collections`` --
        one of the exact shapes ``benign_shelf_write`` permits, so it is writable while
        read-only only when the operator opted in. Creating with the items inline (the
        ``uri`` form plexapi itself uses) rather than create-then-add, because Plex
        refuses an empty collection. Items beyond the first batch are added by the
        caller through :meth:`add_to_collection`.
        """
        if not rating_keys:
            return None
        code = _PLEX_TYPE_CODES["movie" if kind == "movie" else "season"]
        server = await self._connect()
        first = rating_keys[:BATCH_SIZE]

        def write() -> int | None:
            params = urlencode(
                {
                    "type": code,
                    "title": name,
                    "smart": 0,
                    "sectionId": section_key,
                    "uri": self._metadata_uri(str(server.machineIdentifier), first),
                }
            )
            container = server.query(  # type: ignore[no-untyped-call]
                f"/library/collections?{params}", method=server._session.post
            )
            for el in container if container is not None else []:
                key = el.get("ratingKey")
                if key:
                    return int(key)
            return None

        created = await self._call(write, what=f"create the {name!r} collection")

        if created is not None and len(rating_keys) > BATCH_SIZE:
            await self.add_to_collection(created, rating_keys[BATCH_SIZE:])
        return created

    async def add_to_collection(self, collection_key: int, rating_keys: list[int]) -> None:
        """Put items on an existing collection, in batches.

        ``PUT /library/collections/{key}/items`` -- a benign shelf shape. Items already
        on the collection are left as they are by Plex, so re-adding is harmless.
        """
        if not rating_keys:
            return
        server = await self._connect()

        def write() -> None:
            machine = str(server.machineIdentifier)
            for start in range(0, len(rating_keys), BATCH_SIZE):
                chunk = rating_keys[start : start + BATCH_SIZE]
                params = urlencode({"uri": self._metadata_uri(machine, chunk)})
                server.query(  # type: ignore[no-untyped-call]
                    f"/library/collections/{collection_key}/items?{params}",
                    method=server._session.put,
                )

        await self._call(write, what=f"add items to collection {collection_key}")

    async def remove_collection_members(
        self, section_key: int, *, name: str, rating_keys: list[int]
    ) -> None:
        """Take items off the named collection in one request per chunk.

        A "dumb" collection's membership IS the ``collection`` tag (verified against a live
        server), so a batch tag-edit -- ``collection[].tag.tag-={name}`` on
        ``PUT /library/sections/{key}/all``, the same endpoint the label edit uses --
        detaches many items at once, where the old per-item
        ``DELETE .../children/{ratingKey}`` cost one round-trip each (minutes on a large
        shelf). The ``-`` form removes ONLY the named collection, so an item's other
        collections are left in place. The reads batch too: one ``/library/metadata/<ids>``
        per chunk, never one fetch per item.

        The section is addressed by **key** via ``sectionByID``, never by title: two
        libraries can share a title and plexapi's title lookup returns the last match, so a
        title-addressed detach would fail against the first twin every pass (rule 6).

        The collection is removed by the **exact spelling Plex stored**, grouped the way
        :meth:`remove_label` groups labels. A "dumb" collection is a tag, and a
        case-sensitive removal of a case-variant spelling silently removes nothing -- an item
        that left grace would then stay on the shelf forever while the outcome claimed it was
        detached. ``find_collection`` deliberately adopts a case-variant collection, so this
        path must be ready to remove one.

        plexapi's ``removeCollection`` locks the collection field on every edited item, and
        that is chosen deliberately here (``locked=True``, stated explicitly): ``locked=False``
        would not leave the field alone but actively CLEAR an operator's own collection locks,
        so locking is the smaller change. This is a known delta from the old per-child
        ``DELETE``, which left the field unlocked.

        Only ever called when the collection keeps at least one member afterward; a full
        clear goes through :meth:`delete_collection`, so batch-removing is never asked to
        empty a collection and never depends on Plex's empty-collection cleanup.
        """
        if not rating_keys:
            return
        server = await self._connect()
        wanted = normalize_label(name)

        def write() -> None:
            section = server.library.sectionByID(section_key)
            for start in range(0, len(rating_keys), BATCH_SIZE):
                keys = rating_keys[start : start + BATCH_SIZE]
                # ONE multi-id read for the chunk; the metadata carries each item's collection
                # tags. Group by the exact stored spelling that casefold-matches the shelf
                # name, so a case-variant collection is still detached (mirrors remove_label).
                # An item removed from Plex since the scan does not come back and is skipped.
                by_spelling: dict[str, list[Any]] = {}
                for item in section.fetchItems(keys):
                    for existing in item.collections:
                        if normalize_label(str(existing.tag)) == wanted:
                            by_spelling.setdefault(str(existing.tag), []).append(item)
                            break
                for actual_tag, group in by_spelling.items():
                    section.batchMultiEdits(group).removeCollection(
                        actual_tag, locked=True
                    ).saveMultiEdits()

        await self._call(write, what=f"remove items from the {name!r} collection")

    async def rename_collection(self, collection_key: int, name: str) -> None:
        """Re-title a collection in place, keeping its rating key.

        **Verified against a live server:** ``editTitle`` issues ``PUT
        /library/sections/{key}/all?type=18&id={key}&title.value=...`` -- the SAME batch edit
        shape the label writes use, so ``_benign_shape`` already permits it and renaming adds
        no new write shape to the guard. The collection is read by rating key first, which is
        a GET.

        In place, rather than dropping the shelf and rebuilding it under the new name: the
        rating key survives, so a poster, a sort title, or a pin on someone's Plex Home
        screen survives with it. That is what an operator renaming their shelf expects (#869).

        plexapi locks the title field, as a rename in Plex Web does. Nothing re-titles a
        collection behind Reaper's back, so the lock costs nothing and stops an agent
        refresh undoing the operator's name.
        """
        server = await self._connect()

        def write() -> None:
            collection = server.fetchItem(  # type: ignore[no-untyped-call]
                f"/library/collections/{collection_key}"
            )
            collection.editTitle(name)

        await self._call(write, what=f"rename collection {collection_key} to {name!r}")

    async def delete_collection(self, collection_key: int) -> None:
        """Delete a whole collection in one request.

        ``DELETE /library/collections/{key}`` -- a benign shelf shape. This is how an
        emptied "Leaving Soon" shelf disappears: one request, instead of one
        ``DELETE .../children/{ratingKey}`` per member (minutes of serial round-trips on a
        large shelf). It removes only the collection object; the items and their files are
        untouched -- that is a different path, ``/library/metadata/{key}``, which the guard
        never permits under the benign branch.
        """
        server = await self._connect()

        def write() -> None:
            server.query(  # type: ignore[no-untyped-call]
                f"/library/collections/{collection_key}",
                method=server._session.delete,
            )

        await self._call(write, what=f"delete collection {collection_key}")

    async def refresh_path(self, section_key: int, path: str) -> None:
        """Rescan **one directory**, not the whole section.

        ``section.update(path=...)`` is a cheap partial scan. ``section.refresh()`` is a
        full metadata re-download for every item in the library. They are one word apart
        and differ by orders of magnitude, and confusing them on a large library is an
        outage.

        Addressed by **key** via ``sectionByID`` (rule 6/57), like every other write here:
        a title lookup with two same-titled libraries scans the wrong one, which both
        misses the deleted file and leaves the trash interlock reading a library this run
        never touched.

        plexapi issues this as a **GET**, but it is a mutation in effect: on a server set
        to empty trash after every scan, rescanning a path with missing files purges those
        items. ``GuardedSession`` therefore classifies the path as a mutation
        (``_GET_SHAPED_MUTATIONS``), so this call requires arming plus the executor's
        ``declared_mutation``, exactly like ``empty_trash``.
        """
        server = await self._connect()

        def write() -> None:
            server.library.sectionByID(section_key).update(path=path)

        await self._call(write, what=f"refresh {path!r}")

    async def empty_trash(self, section_key: int) -> None:
        """Purge one section's trash: the items Plex marked missing after a refresh.

        The single most dangerous call in the whole application. An unmounted library, a
        scan that finds nothing, and then this, is how a whole Plex library is lost -- so
        the executor gates it behind a count-delta check and only ever runs it *after*
        confirming the section shrank by roughly what was deleted and no more. This method
        does the purge; deciding it is safe to purge is the caller's job, deliberately kept
        out of here so the interlock cannot be bypassed by calling the client directly.

        A mutation, so it is guarded like a delete: it needs arming plus the executor's
        ``declared_mutation``. Scoped to one section, never the whole library, and that
        section is addressed by **key** via ``sectionByID`` (rule 6/57): purging the trash
        of whichever same-titled library plexapi happened to return first is a whole
        library's worth of blast radius on the app's single most dangerous call.
        """
        server = await self._connect()

        def write() -> None:
            server.library.sectionByID(section_key).emptyTrash()

        await self._call(write, what=f"empty trash for section {section_key}")
