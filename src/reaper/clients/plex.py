# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Plex Media Server client.

Wraps ``plexapi``, which is synchronous (it is ``requests`` underneath), so every
call is pushed off the event loop with ``asyncio.to_thread``.

This client uses ``plexapi`` rather than raw HTTP on purpose. It encodes the exact
URI forms for batch edits and hub promotion that other tools in this space have
gotten wrong, and those are the calls where being wrong means silently mangling the
owner's library.

Three behaviors here were confirmed against a live server, and each one contradicts
what you would reasonably assume:

**Labels are title-cased by Plex.** Write ``leaving-soon`` and read back
``Leaving-Soon``. Comparing label tags case-sensitively is therefore a latent bug.
It fails to find a label that is right there, and "I could not find the label I
wrote" turns into "add it again", or worse, "this item is not marked, so it must be
safe to act on". Every comparison in this module is casefolded.

**``addLabel`` preserves existing labels. It does not replace them.** Verified by
adding two labels in succession and reading back both. So Reaper's "Leaving Soon"
mark does not wipe whatever the owner had already put on their media.

**A partial refresh takes a path.** ``section.update(path=...)`` rescans one
directory. ``section.refresh()`` re-downloads metadata for the entire section. They
are one word apart and differ by orders of magnitude in cost.
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
from reaper.engine.reason import Reason
from reaper.ratings import Rating, from_plex
from reaper.refusal import MESSAGES
from reaper.text import fold

if TYPE_CHECKING:
    from plexapi.server import PlexServer

log = structlog.get_logger(__name__)

#: plexapi issues one PUT per chunk in a batch edit. 100 matches the size Kometa
#: uses for the same kind of edit.
BATCH_SIZE = 100

#: One listing page of the GUID sweep. A bigger page is always better here, since
#: the cost is per item rather than per page. This is still bounded, so a huge
#: library never has to arrive in one response.
SWEEP_PAGE_SIZE = 1000

#: Hard stop on :func:`_iter_pages`. Its only path to keep going is a ``totalSize``
#: that stays ahead of ``start`` on a non-empty page. ``totalSize`` is re-read from
#: every response, so the server answering the request controls how long the walk
#: runs. Nothing outside the loop bounds it. The sweeps run under
#: ``asyncio.to_thread``, which cannot be canceled, so a loop that never returns
#: also never reaches the ``finally`` that clears ``scan_runner._scan_running``.
#: Every later scan is then refused until the container restarts. What answers is
#: not always Plex either: a reverse proxy, an auth portal, or a tunnel can sit in
#: that path.
#:
#: The bound is 1,000 pages, so the number of items it covers depends on the page
#: size the server actually serves: about a million rows at
#: :data:`SWEEP_PAGE_SIZE`, fewer against a server that clamps the page below what
#: was requested, which this loop follows to the end rather than treating as
#: truncated.
#:
#: Tripping this bound raises. Two similar loops elsewhere choose differently:
#: ``seerr.MAX_PAGES`` also raises, but ``history_sync.MAX_HISTORY_PAGES`` and
#: ``library_index._SPINE_MAX_PAGES`` stop short and warn instead, and the latter
#: also marks the scan untrusted so nothing gets deleted from it. This function
#: matches Seerr's choice: ``_iter_pages`` reads everything or raises, because
#: every caller is reading a protection source, and stopping short would report
#: part of a section as the whole of it, covering only what was actually read.
SWEEP_MAX_PAGES = 1_000

#: Rating keys per batched ``/library/metadata/{ids}`` read (Rating children and
#: folder paths). The ids ride in the URL path, so this is bounded by URL length,
#: not by response size. 400 keys is about 4 KB of comma-joined ids, comfortably
#: under the usual 8 KB limit, and cuts a 10,000-item enrichment from about 100
#: serial round-trips down to about 25. Raise this with care: past a few thousand
#: keys, a server can answer 414, which the existing ``except`` maps to
#: ``PlexError``. The snapshot then marks itself untrusted, which is safe, but not
#: free.
METADATA_BATCH_SIZE = 400

#: What :meth:`PlexClient.collection_tags` asks Plex to leave out of its metadata
#: batch. It reads one child element, but the response otherwise carries every
#: other one, and excluding them roughly halved the batch size where this was
#: measured (docs/LEARNINGS.md). ``Collection`` is deliberately left out of this
#: exclusion list. A server that ignores these parameters answers as before, and
#: one that dropped Collection children in response to them would leave every
#: collection short of its ``childCount``, which already sends the caller to the
#: per-collection read.
_TAGS_ONLY = (
    "?excludeElements=Media,Genre,Country,Director,Writer,Producer,Role,Similar,Chapter,"
    "Marker,Extras,Related,Rating,Review,Image,UltraBlurColors,Guid"
    "&excludeFields=summary,tagline,titleSort"
)

#: Seconds Reaper waits for one Plex response, longer than plexapi's default of
#: 30, which is a budget for an idle server. A busy library can take longer than
#: that to answer a section sweep, and a read that times out is a protection
#: source the scan then has to do without. This is still a bound: nothing here
#: waits forever.
#:
#: ``PlexServer.query`` passes this on every call, so it covers the sweeps, the
#: shelf reads, and the writes alike. The watchlist reads plex.tv through
#: ``myPlexAccount()``, which plexapi builds with no timeout, so that one call
#: keeps the default of 30.
PLEX_READ_TIMEOUT = 60


def _parse_rating_children(el: Element) -> list[Rating]:
    """Per-provider scores from full-metadata ``Rating`` children.

    A section listing carries only two rating slots, so a library whose agent fills
    them with, say, IMDb can never show a Rotten Tomatoes score from the listing
    alone. The full metadata (``/library/metadata/{ids}``) carries one ``Rating``
    child per provider score, critic and audience kept separate, each with the
    provenance image the rest of the codebase requires. Confirmed on a live server:
    ``type="audience" image="rottentomatoes://image.rating.upright"`` is the
    audience score, and ``type="critic" ... .ripe`` is the Tomatometer. IMDb and
    TMDb both arrive with ``type="audience"`` and their own images. Values are 0-10
    like every Plex rating. ``from_plex`` applies the same provenance and range
    rules the slot reads use, so an unreadable child is dropped rather than guessed
    at.
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

    ``library`` is the title of the section this row was listed under. The caller
    knows it per section and passes it in, since the listing element itself does
    not carry it.

    This only reads attributes and children already present on the fetched XML. A
    field the server omitted becomes ``None``, never a reason for another request.
    Movies carry ``Media/Part`` children (file name, exact byte size, and
    resolution). Shows carry no files in a listing, so their ``files`` stay empty
    here, and the folder-path batch in :meth:`PlexClient.library_guid_index` fills
    them in.
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
    # Ratings with provenance. The *RatingImage field says what each number is
    # (imdb, RT, tmdb). A number whose image is missing is dropped by from_plex
    # rather than guessed at. The audience flag routes an RT image in the audience
    # slot to the audience score.
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
    """Plex could not be reached, or refused the operation.

    Carries a catalog code and raw params the same way
    ``clients.base.IntegrationError`` does (``error.plexclient.*`` in
    ``reaper.refusal.MESSAGES``). There is no ``service`` param, since there is
    always exactly one Plex.
    """

    def __init__(self, code: str, /, **params: str | int | float | bool) -> None:
        self.code = code
        self.params: dict[str, str | int | float | bool] = params
        super().__init__(str(self))

    def __str__(self) -> str:
        template = MESSAGES.get(self.code, self.code)
        try:
            return template.format(**self.params)
        except (KeyError, IndexError):
            return template

    def as_reason(self) -> Reason:
        return Reason(self.code, dict(self.params))


# ---------------------------------------------------------------------------
# The guard, again, because plexapi does not speak httpx.
# ---------------------------------------------------------------------------
#
# ``GuardedTransport`` protects every integration that goes through Reaper's httpx
# clients. plexapi is built on ``requests``, so it would bypass that guard
# entirely: label writes, collection edits, and, the one that matters most,
# ``emptyTrash`` would all go unguarded.
#
# An unmounted library, plus a scan, plus emptyTrash, is how people lose an entire
# Plex library. Without a guard here, emptyTrash would be the one destructive call
# in the codebase with no safety interlock in front of it.
#
# So this file enforces the same rule on the requests side: a mutating call
# requires deletion to be enabled and an explicit declaration from the executor,
# which writes its intent to the durable journal before declaring it. The
# declaration is a context variable rather than a per-request flag only because
# plexapi gives this code nowhere else to hang one.

_declared: ContextVar[bool] = ContextVar("reaper_plex_mutation_declared", default=False)

#: Marks the enclosed writes as the "Leaving Soon" shelf reconcile, a set of
#: reversible mutations that touch no files. This stays distinct from
#: ``_declared`` so the two paths cannot be confused. A deletion needs the host
#: armed and the write journalled. A shelf write needs the host armed, or the
#: operator opted in separately.
_benign_shelf: ContextVar[bool] = ContextVar("reaper_plex_benign_shelf", default=False)


#: Where a read that came back incomplete reports itself, without throwing away
#: the part it did read. The GUID sweep's batched metadata enrichment can be
#: windowed by the server: the ids stay complete, but the ratings do not, and a
#: title with no rating is one the rating bar can no longer protect. Losing a
#: protection like that means the scan must mark itself untrusted so nothing gets
#: deleted from it. But raising here would also throw away a perfectly good id
#: sweep, dropping the whole library to title-only matching, which is a much
#: wider loss than the missing ratings alone.
#:
#: A context variable rather than a parameter, for the same reason ``_declared``
#: is one: the movie and show sweeps run as concurrent tasks over one client, and
#: each task's own copy of the context keeps its own collector. The list is bound
#: before those tasks are created, so appends from inside them land in the
#: collector that opened it.
_incomplete: ContextVar[list[str] | None] = ContextVar("reaper_plex_incomplete_reads", default=None)


@contextlib.contextmanager
def collecting_incomplete_reads() -> Iterator[list[str]]:
    """Collect, for this task, the reasons any read here came back short.

    Opened by ``services.library_index.build_index``, which marks the snapshot
    untrusted for every reason collected. Outside a collector, the reasons are
    logged and dropped, which is correct for a one-off diagnostic read, but never
    for a scan.
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
    """Permit mutating Plex calls on this task, after journalling the intent first.

    Set only by the action executor. Anything else calling this is a bug. That
    narrowness is the point: the window is exactly one context manager wide, so a
    stray write from some future code path fails loudly instead of quietly
    succeeding.
    """
    token = _declared.set(True)
    try:
        yield
    finally:
        _declared.reset(token)


@contextlib.contextmanager
def benign_shelf_write() -> Iterator[None]:
    """Mark the enclosed writes as the "Leaving Soon" shelf reconcile.

    Set only by the Leaving Soon sync path, exactly as ``declared_mutation`` is set
    only by the executor. That narrowness keeps this from becoming a general
    license to write. It tells :class:`GuardedSession` that the write is the
    reversible, file-touching-nothing shelf work (the label batch edit and the
    collection edits), not a deletion, so it may be permitted in read-only mode if
    the operator turned on "Update while read-only" in Settings -> Plex.

    This can never permit a delete, and that is enforced structurally, not by
    call-site discipline. The benign branch matches only the exact shapes in
    ``_benign_shape``. Any other verb or path inside this block falls back to the
    armed-and-declared rule. In particular, ``DELETE /library/metadata/{key}``, the
    request that removes an item and its files on a server that allows media
    deletion, matches no benign shape and never can.
    """
    token = _benign_shelf.set(True)
    try:
        yield
    finally:
        _benign_shelf.reset(token)


#: GET-shaped mutations. Plex triggers a section scan with
#: ``GET /library/sections/{key}/refresh``. On a server with "Empty trash
#: automatically after every scan" enabled, scanning a path with missing files
#: purges those items' library records. Filtering on method alone would let that
#: straight through, the same reason the Tautulli client carries a command
#: allow-list, so this guard classifies these paths as mutations regardless of
#: verb.
_GET_SHAPED_MUTATIONS = re.compile(r"^/library/sections/[^/]+/refresh$")

#: The write shapes ``benign_shelf_write`` may permit. Each is matched by its
#: exact method and path, so this list can never quietly widen:
#:
#: * the batch tag edit, ``PUT /library/sections/{key}/all``, which carries both
#:   the label add/remove and the collection-membership remove (a collection is a
#:   tag, so detaching a member is ``collection[].tag.tag-={name}`` on this same
#:   endpoint)
#: * creating a collection, ``POST /library/collections``
#: * adding items to one, ``PUT /library/collections/{key}/items``
#: * deleting a whole, emptied collection, ``DELETE /library/collections/{key}``
#:
#: The whole-collection delete is how an emptied shelf disappears in one request
#: instead of one delete per member. ``DELETE /library/metadata/{key}``, the shape
#: that deletes an item and, on a permissive server, its files, is a different path
#: (``.../metadata/``, not ``.../collections/``) and is deliberately not on this
#: list. It must never be added.
_LABEL_EDIT = re.compile(r"^/library/sections/[^/]+/all$")
_COLLECTION_CREATE = re.compile(r"^/library/collections$")
_COLLECTION_ADD = re.compile(r"^/library/collections/[^/]+/items$")
_COLLECTION_DELETE = re.compile(r"^/library/collections/[^/]+$")


def _benign_shape(method: str, path: str) -> bool:
    """Whether this request is one of the exact shelf-write shapes. Any other verb,
    or any other path, is not benign, whatever context it runs in."""
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
        # requests reads TLS verification off the session. Turning it off is the
        # operator's explicit per-server choice (PlexServer.verify_tls), mirroring
        # the *arr clients' opt-out. The default stays on.
        self.verify = verify

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        path = urlsplit(url).path
        mutates = method.upper() not in SAFE_METHODS or bool(_GET_SHAPED_MUTATIONS.match(path))
        if mutates:
            if _benign_shelf.get() and _benign_shape(method, path):
                # The "Leaving Soon" shelf reconcile. This is reversible, touches no
                # file, and is structurally confined to the label batch edit and the
                # collection edits (see _benign_shape). It is gated like a delete by
                # default, needing the host armed, but an operator may opt in to
                # allowing it while read-only, so the grace-period warning can appear
                # before deletion is ever enabled. This branch can never permit a
                # deletion: a verb and path outside _benign_shape does not match it
                # and falls through to the armed-and-declared rule.
                if not self._safety.leaving_soon_write_allowed:
                    refuse_mutation(
                        "plex.write_blocked",
                        method,
                        path,
                        reason="shelf_not_allowed",
                        code="error.integration.write_shelf_blocked",
                    )
            elif not self._safety.destructive_allowed:
                refuse_mutation(
                    "plex.write_blocked",
                    method,
                    path,
                    reason="not_armed",
                    code="error.integration.write_not_armed",
                    why=self._safety.why_blocked() or "",
                )
            elif not _declared.get():
                refuse_mutation(
                    "plex.write_blocked",
                    method,
                    path,
                    reason="not_declared",
                    code="error.integration.write_not_declared",
                )
        # Trace only what passed the guard and reached the wire. A blocked mutation
        # raised above and never happened. plexapi puts X-Plex-Token in the query
        # string, so `path`, already the token-free split above, is what gets
        # logged, never `url`.
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

    Plex title-cases what you write. Write ``leaving-soon`` and it comes back as
    ``Leaving-Soon``. Comparing raw tags therefore silently fails to match a label
    that is present, so every comparison goes through here.
    """
    return fold(tag)


@dataclass(frozen=True)
class PlexSection:
    """One video library, as the pickers and the Leaving Soon reconcile see it."""

    key: int
    title: str
    kind: str
    """``"movie"`` or ``"show"``, Plex's own section types. Music and photo sections
    are never surfaced, because Reaper has no business in them."""


@dataclass(frozen=True)
class PlexSectionPaths:
    """One library section and the folders it covers, addressed by its own key.

    The key rides along because everything downstream of this table, the partial
    refresh, the item count, the trash purge, addresses a section by key, never by
    title. Two libraries may share a title. Only the key is unique.
    """

    key: int
    title: str
    locations: tuple[str, ...]


@dataclass(frozen=True)
class PlexSeasonRow:
    """One season as a listing sweep sees it: its number, its own rating key, and
    its added-at.

    Grouped under its show's rating key by :meth:`PlexClient.library_season_index`.
    The added-at is carried raw as Plex reports it, an epoch string, and
    ``from_epoch`` parses it. The season-number ambiguity policy, dropping a number
    that appears twice under one show, is applied by the caller, in the one place
    that policy lives for both this sweep and the per-show fallback.
    """

    season_index: int | None
    rating_key: int
    added_at: str | None


@dataclass(frozen=True)
class PlexCollectionRow:
    """One collection as the section-level listing sees it: its rating key, title,
    and Plex's own member count. Membership, which items actually sit inside it, is
    a separate read, :meth:`PlexClient.collection_children`. This row is the
    shelf's identity, not its contents.

    ``child_count`` is also what tells a caller that :meth:`PlexClient.collection_tags`
    did not see all of this collection, since Plex counts members it stores no tag
    for.
    """

    rating_key: int
    title: str
    child_count: int | None


#: Plex's numeric metadata types. This covers the two the shelf works on, movies in
#: movie sections and seasons in show sections, plus the show itself, which is the
#: level a TV collection lists at and therefore the level ``collection_tags`` reads
#: a show library at.
_PLEX_TYPE_CODES = {"movie": 1, "show": 2, "season": 3}


def _iter_pages(server: Any, path: str, query: str, *, what: str) -> Iterator[list[Any]]:
    """Yield each raw page of one Plex listing, hardened against silent truncation.

    This is the single paging loop every listing read in this module runs on, so
    none of them can drift apart. The four section sweeps (``library_guid_index``,
    ``library_season_index``, ``labeled_in_section``, ``section_rating_keys``) go
    through :func:`_iter_section_pages`, and the two shelf reads
    (``list_collections``, ``collection_children``) call this directly.
    ``find_collection`` searches ``list_collections``'s result rather than paging a
    second time. ``query`` is the listing's own query string before the
    container-window params (``"?includeGuids=1"``, ``"?type=3&label=..."``, or
    ``""``). This function appends the start and size.

    This pages, and stops, based on the raw child count, never a filtered one.
    Advancing or ending a listing on the count of children that survived a
    ``ratingKey`` filter would let one dropped child end a page early. A child with
    no ``ratingKey`` is an anomaly this function raises on, rather than treating it
    as a shorter page, so the caller can mark the scan untrusted instead.
    ``totalSize`` is the sole paging authority: a server that clamps a page below
    the requested size is still followed to the end, and a full page with no
    ``totalSize`` to bound it fails closed rather than being guessed to be the
    last. This never falls back from ``totalSize`` to the requested ``size``.

    Bounded by :data:`SWEEP_MAX_PAGES`, which raises rather than returning a short
    result.
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
            # A child the paging math cannot advance over. This is not the
            # container shape the loop assumes, so it raises, letting the caller
            # fall back or mark the scan untrusted, rather than ending on a
            # filtered page.
            raise PlexError("error.plexclient.paging_failed", what=what)
        yield raw
        start += len(raw)
        total_attr = container.get("totalSize")
        if total_attr is not None:
            # totalSize is the authority. This pages until it is reached, even if
            # a page came back short. A clamped page is followed, never treated
            # as truncated.
            if start >= int(total_attr):
                return
            if not raw:
                # start < total but the page was empty. No progress, so this
                # fails closed.
                raise PlexError("error.plexclient.paging_failed", what=what)
            if pages >= SWEEP_MAX_PAGES:
                # Only reachable while the reported total keeps outrunning
                # ``start`` on full pages, which is a server that is not
                # advancing through the listing.
                raise PlexError("error.plexclient.paging_failed", what=what)
        elif len(raw) < SWEEP_PAGE_SIZE:
            # No totalSize to lean on. A short raw page is the last page.
            return
        else:
            # A full page with no totalSize. This cannot tell whether more
            # remains, so it fails closed.
            raise PlexError("error.plexclient.paging_failed", what=what)


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
    """Something being watched right now."""

    rating_key: int
    parent_rating_key: int | None
    grandparent_rating_key: int | None
    user: str

    @property
    def veto_keys(self) -> set[int]:
        """Every key this stream should protect.

        An episode might be playing, but the thing on disk that a season prune
        would remove is its season, and the thing a show delete would remove is
        its show. Vetoing only the episode's own rating key would let Reaper
        delete the season out from under someone who is watching it.
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
        # gathers. Both sweeps ride one plexapi server (one requests.Session), and
        # requests does not promise a Session is safe to share across threads. So
        # the sweeps take this lock and run one at a time, while still overlapping
        # with the Tautulli and *arr reads that dominate the wall clock.
        #
        # This lock's scope is narrow: it serializes only the two sweeps
        # (``library_guid_index``, ``library_season_index``), the long multi-page
        # walks a scan issues in parallel. It is not a whole-client mutex. The
        # Leaving Soon reconcile deliberately runs its per-library passes
        # concurrently (``leaving_soon.sync_shelves``, bounded by
        # ``SHELF_CONCURRENCY``), so several of its shelf reads and writes can be in
        # flight on this same session. That pass owns its own client, touches no
        # file, and taking this lock on its reads alone would look like a fix while
        # leaving its writes exactly as concurrent as before.
        self._sweep_lock = asyncio.Lock()
        # The "one server" assumption itself needs a lock too: two concurrent
        # callers that both see _server as None would each build a connection,
        # leaking one session and splitting the sweeps across two sessions, exactly
        # what _sweep_lock is meant to prevent.
        self._connect_lock = asyncio.Lock()

    async def connect(self) -> PlexServer:
        """The underlying plexapi server, connected once and reused.

        Exposed for the list providers, which speak plexapi directly. Reads through
        it still pass through :class:`GuardedSession`, which permits GETs and
        refuses writes, so even a list refresh cannot mutate Plex.
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
            # The timeout rides the server object, not the session, because query
            # passes its own value explicitly, which would override a session
            # default anyway.
            return _PlexServer(  # type: ignore[no-untyped-call]
                self._base_url,
                self._token,
                session=GuardedSession(self._safety, verify=self._verify),
                timeout=PLEX_READ_TIMEOUT,
            )

        async with self._connect_lock:
            # Re-checked under the lock. A concurrent caller may have connected
            # while this one waited, and building twice would leak a session.
            connected = self._server
            if connected is not None:
                return connected
            # Not routed through `_call`. Its message would end up byte-for-byte
            # the same as `what=f"reach Plex at {self._base_url}"`, so this is a
            # deliberate boundary, not a missed mapping: `_call` describes one call
            # against an already-connected server, and this is the call that runs
            # before a server exists.
            try:
                built = await asyncio.to_thread(build)
            except Exception as exc:
                raise PlexError(
                    "error.plexclient.connect_failed", base_url=self._base_url, detail=str(exc)
                ) from exc
            self._server = built
            return built

    async def _call[T](self, work: Callable[[], T], *, what: str) -> T:
        """Run one synchronous plexapi call off the event loop, mapping its failures.

        plexapi is synchronous, so every call in this client is a closure handed to
        a worker thread. This centralizes the failure mapping that would otherwise
        be written out at each call site: a catch-all that converts whatever
        plexapi raised into a :class:`PlexError` naming what was being attempted,
        and, on every mutating method, an arm ahead of it that lets the transport
        guard's refusal through unchanged.

        That refusal arm is the reason this is a helper rather than a tidy-up. A
        guard refusal is not a Plex failure. It means a mutating call was attempted
        without the host armed, or without the intent journalled first, and
        relabeling it ``PlexError`` would tell the caller Plex is unwell and invite
        it to mark the scan untrusted instead. Centralizing this arm here means a
        new write method inherits it automatically, rather than needing to copy it
        by hand.

        This uses ``asyncio.to_thread`` rather than a shared executor, and that
        choice matters: it copies the current context, and the journalled-intent
        flag the guard reads is a :class:`~contextvars.ContextVar`. Under
        ``loop.run_in_executor`` with a bare executor, the worker would read that
        flag as unset, and every journalled write would be refused. This fails
        closed in both directions, and both executor call sites turn a refusal
        into a warning rather than a crash, so a break here would happen quietly.

        ``what`` completes the sentence ``Could not {what}: {exc}``, so it must be
        a verb phrase, not a noun. ``_iter_pages`` in this same file takes a
        ``what`` that is a noun, which is the one way to get a mismatched sentence
        like ``Could not GUID sweep: ...`` out of this.

        Five methods do not read through this, and each explains why in place:
        ``_connect`` (the one call that runs before there is a server, so this
        contract does not describe it), ``active_streams`` (its message is the
        fail-closed reasoning itself, not a verb phrase), ``is_refreshing`` (it
        falls back to a warning rather than raising), ``aclose`` (no mapping at
        all, since it is teardown), and ``trash_count`` (its own read already
        raises ``PlexError``, which this would wrap a second time).
        """
        try:
            return await asyncio.to_thread(work)
        # Ahead of the catch-all, deliberately. `SafetyViolationError` and
        # `PlexError` are siblings off `RuntimeError`, neither derived from the
        # other, so a downstream `except PlexError` does not catch a refusal. This
        # arm is what keeps it that way: the refusal must reach the caller
        # unchanged, since `clients/base.refuse_mutation` has already logged it at
        # the point of refusal.
        except SafetyViolationError:
            raise
        except Exception as exc:
            raise PlexError("error.plexclient.call_failed", what=what, detail=str(exc)) from exc

    # -- reading -----------------------------------------------------------

    async def active_streams(self) -> list[ActiveStream]:
        """What is playing right now.

        Checked again immediately before every delete, not just once at the start
        of a run: a run takes minutes, and somebody can start watching partway
        through it. No other tool in this space does this, and it is a cheap
        protection against the worst possible outcome, deleting a file out from
        under a person watching it.
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

        # Not routed through `_call`. The message below is the fail-closed
        # reasoning itself, not a verb phrase, and this is the last read before an
        # irreversible act.
        try:
            return await asyncio.to_thread(read)
        except Exception as exc:
            # Fail closed. If this cannot tell whether anyone is watching, it must
            # not conclude that nobody is. That distinction, between not knowing
            # and knowing something is absent, is what the whole engine is built
            # on, applied here to the last check before an irreversible act.
            raise PlexError("error.plexclient.streams_unreadable", detail=str(exc)) from exc

    async def section_paths(self) -> list[PlexSectionPaths]:
        """Every library section and the paths it covers, each carrying its own key.

        This is the raw material for the path-mapping table. Radarr says
        ``/movies``. Plex says ``/media/movies``. The partial-refresh path comes
        from the *arr, so without this mapping the refresh would silently rescan
        nothing at all.

        This is a list keyed by section key, not a ``{title: paths}`` dict, because
        two Plex libraries may legally share a title, and a title-keyed map would
        silently drop one of them. The dropped library's post-reap refresh would
        then map to nothing, the exact "silently rescans nothing at all" failure
        this table exists to prevent, and the trash interlock behind it would
        compare against a library nobody deleted from.

        Failures map to ``PlexError`` like every other read here. Plex can answer
        the connect handshake and still fail this one call, such as a restart
        between the two calls or a proxy 502, and an unmapped plexapi exception
        would escape the executor's ``except PlexError`` mid-run. That would leave
        the file already deleted, its journal step stuck at SENT, the run stuck
        EXECUTING, and every remaining approved deletion never attempted.
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

        This is the enrichment behind id-based matching. For each library section
        of ``section_type`` (``"movie"`` or ``"show"``), this pages the section's
        ``/all`` listing (with ``includeGuids=1``) and parses the container XML
        directly: the new-agent ``Guid`` children and the legacy single ``guid``
        attribute, plus every file behind the listing with its name and exact byte
        size (the first location's leaf feeds the global basename tier, and the
        full set exists so an ambiguous id can be narrowed against all of a
        candidate's files, and byte-identical re-lists of one file can be
        recognized), the display metadata (ratings with provenance, certification,
        runtime, resolution), and the title, year, and added-at the listing already
        carries.

        This parses the raw XML rather than going through ``section.all()``, and
        the reason is measured, not stylistic. plexapi reloads a partial object the
        first time any accessed attribute is ``None``, and on a listing row some
        attribute always is, so the object walk would silently issue one metadata
        request per item, and the "single sweep" would cost minutes on a large
        library. Reading the container XML makes a missing attribute honestly
        ``None`` with no hidden network call, so the sweep stays a handful of page
        requests. Two things ``/all`` never carries are filled in separately, by
        batched ``/library/metadata/{ids}`` reads (100 per call, about one request
        per 100 items): show ``Location`` folder paths, the folder-name tier that
        narrows a show listed in two sections, and, for movies and shows alike, the
        per-provider ``Rating`` children (``includeRatings=1`` on a listing returns
        nothing, confirmed by testing). The listing's two rating slots cannot carry
        a provider's critic and audience score at once, so without these children a
        library whose slots hold IMDb would never surface a Rotten Tomatoes number
        at all. Returning full :class:`PlexItem` rows, not just the ids, lets the
        index builders merge in items the Tautulli media-info cache has not listed
        yet. A freshly added item exists here first.

        This issues GETs only, so it runs in read-only mode through
        :class:`GuardedSession`. It raises ``PlexError`` on any failure rather than
        returning a partial map, so the caller can fail closed and mark the
        snapshot untrusted. Silently falling the whole library back to title-only
        matching at the moment the id signal vanished is exactly the failure this
        feature exists to prevent. The paging runs through the one hardened
        ``_iter_section_pages`` loop (raw-count advance, ``totalSize`` the sole
        authority, a truncated or unbounded page raised on), so a section can never
        end early with a silently partial map.

        The enrichment read, though, reports the same class of loss without
        throwing the whole sweep away: a batched metadata read that comes back
        short files one reason with :func:`collecting_incomplete_reads`, which
        ``services.library_index.build_index`` opens and marks the snapshot
        untrusted from. The ratings that read carries are a protection, and
        losing them quietly makes titles look more deletable, not less.
        """
        server = await self._connect()
        # Filled inside the worker thread, read back on the event loop below. A
        # short metadata batch is a lost protection source, so the caller has to
        # hear about it.
        short_batches: list[tuple[int, int]] = []

        def read() -> dict[int, PlexItem]:
            out: dict[int, PlexItem] = {}
            batch_keys: list[int] = []
            for section in server.library.sections():
                if section.type != section_type:
                    continue
                # Only the libraries the operator included in scans (Settings ->
                # Plex). None means every library of this type. A set scopes the
                # sweep to just those sections, the same section keys the Tautulli
                # spine uses (services.library_index), so the two never disagree
                # about which sections were read.
                if allowed_sections is not None and int(section.key) not in allowed_sections:
                    continue
                section_title = str(section.title) if section.title else None
                # Hardened, complete-or-raise paging (see _iter_section_pages).
                # Every child in every page carries a ratingKey, so this loop reads
                # them straight off ``raw``.
                for raw in _iter_section_pages(
                    server, int(section.key), "?includeGuids=1", what="GUID sweep"
                ):
                    for el in raw:
                        item = _parse_sweep_element(el, library=section_title)
                        out[item.rating_key] = item
                        batch_keys.append(item.rating_key)

            # The batched metadata reads cover show Location folders (each leaf
            # becomes the show's basename exactly as the object walk produced it)
            # and the Rating children for every item. The slot ratings from the
            # listing keep precedence. Children only add sources the slots did not
            # carry, so a server whose slots and children disagree keeps the value
            # the rest of the scan already froze.
            for chunk_start in range(0, len(batch_keys), METADATA_BATCH_SIZE):
                chunk = batch_keys[chunk_start : chunk_start + METADATA_BATCH_SIZE]
                batch = list(
                    server.query(  # type: ignore[no-untyped-call]
                        "/library/metadata/" + ",".join(str(k) for k in chunk)
                    )
                )
                if len(batch) < len(chunk):
                    # A server that windows the multi-id response drops the tail of
                    # the chunk, and those Rating children are the only source of
                    # the per-provider scores (for shows, of any score at all).
                    # Losing them removes the rating protection from every title in
                    # the tail, so this marks the snapshot untrusted through
                    # ``_report_incomplete`` below, which reaches the scan via the
                    # ``collecting_incomplete_reads`` sink the caller opens.
                    #
                    # This is reported rather than raised, deliberately, as the
                    # narrower response: raising here would also throw away a
                    # complete id sweep, dropping the whole library to title-only
                    # matching on top of the lost ratings. The map returned is
                    # exactly as complete as it was. The snapshot simply cannot be
                    # executed as is.
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
                    # The leaf feeds the global by_basename map. The full path rides
                    # along on each PlexFile because a show folder has no size, so
                    # the segments above the leaf are the only thing that can
                    # separate the same title listed in two sections
                    # (identity._narrow_by_path_depth).
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
        """How many items a section holds. This is the input to the trash
        interlock: the executor reads it before its first delete and again before
        purging, and refuses the purge unless the section shrank by no more than
        what the run deleted under it.

        Addressed by key, through ``sectionByID``. A by-title lookup would return
        whichever of two same-titled libraries plexapi saw first, so the interlock
        meant to catch an over-large trash purge would end up comparing the wrong
        library's size against this run's deletions.
        """
        server = await self._connect()

        def read() -> int:
            return int(server.library.sectionByID(section_key).totalSize)

        return await self._call(read, what=f"count items in section {section_key}")

    async def trash_count(self, section_key: int) -> int:
        """How many items are sitting in one section's trash, waiting to be purged.

        This is what the operator is warned about before a reap. ``empty_trash`` is
        section-wide (``PUT /library/sections/{key}/emptyTrash``), so it destroys
        the library records, watch history, ratings, collection membership, of
        everything already in the trash, not just what this run deleted. Items
        trashed before a run are in the same state before and after it, so they
        cancel out of the executor's count-delta gate, and that gate cannot see
        them. This read is the only thing that can.

        This is a count read, not a listing read: the container window is
        zero-sized and only ``totalSize`` is taken, so the complete-or-raise
        paging rule does not apply here (the same shape as :meth:`item_count`,
        which also reads ``totalSize`` off the section).

        ``trash=1`` is a real Plex filter, confirmed against a live server with a
        control: an unknown parameter comes back with the full library count,
        while ``trash=1`` narrows it. That control is why the caller can read 0 as
        "genuinely nothing in the trash" rather than "the server ignored me". A
        server that does ignore it returns the whole library, which the caller
        detects by comparing against :meth:`item_count` and treats as unreadable
        rather than as a real count.

        Raises ``PlexError`` on any failure. The caller treats that as unknown and
        warns, never as "nothing there". An unreadable trash is exactly the case
        where the operator most needs telling.
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
                # No totalSize to read means the answer is not a count. Fail
                # closed rather than falling back to a guessed size or zero.
                raise PlexError("error.plexclient.trash_count_failed", section=section_key)
            return int(raw)

        # Not routed through `_call`. `read` already raises `PlexError` on a
        # missing totalSize, and `_call` would wrap that into a second error
        # naming the wrong thing.
        try:
            return await asyncio.to_thread(read)
        except PlexError:
            raise
        except Exception as exc:
            raise PlexError("error.plexclient.trash_count_failed", section=section_key) from exc

    async def empties_trash_after_scan(self) -> bool:
        """Does this server empty a section's trash by itself after every scan?

        This reads Plex's ``autoEmptyTrash`` preference, which is server-wide, not
        per-library. It ships on by default, which is why this is worth
        surfacing: when it is on, Plex purges the trash itself after each scan
        Reaper's path refresh triggers, so the executor's own trash interlock (the
        count-delta gate, the mount check, the settle wait) never gets a say. The
        purge has already happened, inside Plex.

        Raises ``PlexError`` if the preference cannot be read, which the caller
        reports as unknown rather than as "no".
        """
        server = await self._connect()

        def read() -> bool:
            return bool(server.settings.get("autoEmptyTrash").value)

        return await self._call(read, what="read the empty-trash-after-scan setting")

    async def aclose(self) -> None:
        """Close the underlying plexapi session, if one was ever built.

        plexapi rides one ``requests.Session``, built in ``_connect``. Nothing but
        garbage collection reclaims its pooled sockets unless this is called. Safe
        when never connected, and safe to call twice.
        """
        server, self._server = self._server, None
        if server is None:
            return

        def close() -> None:
            session = getattr(server, "_session", None)
            if session is not None:
                session.close()

        # Not routed through `_call`. Teardown maps nothing, because there is no
        # caller left to tell.
        await asyncio.to_thread(close)

    async def __aenter__(self) -> PlexClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def is_refreshing(self, section_key: int) -> bool:
        """Is a scan currently running on this section?

        A refresh (``section.update(path=...)``) fires an asynchronous scan. The
        executor polls this so it does not empty the trash before Plex has
        actually noticed the deleted file is gone. A read, addressed by key
        through ``sectionByID``, so it cannot report on the wrong one of two
        same-titled libraries. On any error, it reports ``True`` (still busy), so
        the caller waits rather than racing ahead."""
        server = await self._connect()

        def read() -> bool:
            section = server.library.sectionByID(section_key)
            section.reload()
            return bool(getattr(section, "refreshing", False))

        # Not routed through `_call`. This falls back to a warning and a
        # conservative True rather than raising, so there is no `PlexError` for a
        # shared mapping to build.
        try:
            return await asyncio.to_thread(read)
        except Exception as exc:
            log.warning("plex.refresh_state_unreadable", section=section_key, error=str(exc))
            return True

    async def labeled_in_section(self, section_key: int, *, kind: str, label: str) -> set[int]:
        """The rating keys in one section carrying ``label``, at the level the
        shelf works on: movies in a movie section, seasons in a show section.

        A raw container read rather than ``section.search``, so a show section
        filters at the season level (``type=3``). The object walk searches shows
        by default, and a label sitting on a season would be invisible to it,
        which would cause the label to be re-added forever. GETs only. Paged
        through the one hardened ``_iter_section_pages`` loop (raw-count advance,
        ``totalSize`` the sole authority), so a partial page can never
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

        A read. Music, photo, and other section types are filtered out here, not
        in the UI, because Reaper has no feature that touches them, so they are
        never even offered.
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
        """Every rating key in one section, at the level the shelf works on,
        movies in a movie section (``type=1``), seasons in a show section
        (``type=3``).

        This is what scopes the reconcile per library: the grace list's rating
        keys are intersected with this set, so an item is only ever marked in the
        section it actually lives in. A read, paged through the one hardened
        ``_iter_section_pages`` loop (raw-count advance, ``totalSize`` the sole
        authority) so a partial page can never shrink the section and leave a
        marked item unmatched by the reconcile.
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

        This is the bulk replacement for a per-show ``get_children_metadata`` call.
        One paged ``type=3`` sweep of each show section yields every season at
        once, each carrying its number (``index``), its own rating key, its show
        (``parentRatingKey``), and its own added-at. Reading seasons this way is
        possible straight from Plex even though Tautulli has no season-sweep
        command, and the keys match: both address the same linked server, so the
        season key this sweep returns equals the one the per-show call returns,
        and joins the same watch history.

        ``allowed_sections`` scopes the sweep to the show libraries the operator
        included in scans, the same set the GUID sweep and the Tautulli spine
        filter on, so the season keys join the same items those listed. This reads
        raw XML like the GUID sweep, no object walk, GETs only, and under the same
        ``_sweep_lock``, since it shares the one requests session with the movie
        and show GUID sweeps.

        This raises ``PlexError`` on any failure rather than returning a partial
        map. A caller reading a truncated season sweep as complete would let a
        real season's watch history go unread and unprotected. The caller falls
        back to reading per show for anything absent here.

        Paged through the one hardened ``_iter_section_pages`` loop: the raw child
        count drives the paging, ``totalSize`` is the sole authority (a clamped
        page is followed to the end), and a child with no ``ratingKey``, or a full
        page with no ``totalSize``, raises. That way every show falls back to a
        per-show read instead of a section silently ending a page early.
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
                            # A season with no show cannot be attributed to one.
                            # This skips it rather than guessing it onto the wrong
                            # show.
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
        """Every collection in one section: its rating key, title, and Plex's own
        child count.

        Paged through the one hardened ``_iter_pages`` loop over
        ``/library/sections/{key}/collections``. :meth:`find_collection` searches
        this method's result rather than running its own second loop over the same
        path. Complete-or-raise like every other listing here: a truncated page is
        never read as the whole shelf.

        Collections are navigation, never protection (see
        ``docs/history/COLLECTIONS_PLAN.md``), so this method does not mark
        anything untrusted itself. It either reports the section honestly or
        raises. A caller that wants "missing chip" rather than "broken scan" out
        of a Plex hiccup catches the raise itself.
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
        ``/library/sections/{key}/collections`` a second time, so it inherits that
        method's paging and completeness guarantees. Matched casefolded, because
        Plex title-cases tags and titles on the way in.

        A row ``list_collections`` cannot assign a rating key to raises here,
        rather than being silently skipped. Returning ``None`` reads to the caller
        as "no such collection," and the caller then creates a second "Leaving
        Soon" collection, splitting the shelf. Raising instead reports the problem
        honestly.
        """
        wanted = normalize_label(name)
        for row in await self.list_collections(section_key):
            if normalize_label(row.title) == wanted:
                return row.rating_key
        return None

    async def collection_children(self, collection_key: int) -> set[int]:
        """The rating keys currently on one collection. A read.

        Paged through the one hardened ``_iter_pages`` loop, so a truncated
        response can never be read as the whole membership. The reconcile computes
        the items to detach as ``current - wanted``, so a short read would leave
        stale titles marked "Leaving Soon" long after they were reprieved.
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

        This costs one request per 400 items, where asking each collection for its
        children would cost one request per collection. A Plex read costs about
        the same whatever it returns, so a library holding hundreds of collections
        would spend the difference on per-request overhead alone (measured in
        docs/LEARNINGS.md).

        A "dumb" collection's membership is each member's ``collection`` tag,
        which is what :meth:`remove_collection_members` already writes through,
        and the full metadata read carries those tags. The section listing does
        not, which is why this pays for a second read rather than parsing the
        sweep's pages. That listing reports some of an item's tags and drops the
        rest, never a wrong tag, only a missing one, which would otherwise ship as
        titles quietly losing their chip.

        A collection Plex reports more members for than this returns is one whose
        membership is not tags: a smart collection is a saved filter, and a
        collection of seasons or episodes holds objects this section-level listing
        never lists. The caller compares against ``child_count`` and reads those
        the per-collection way (``services.snapshot._collection_membership``).
        This method reports what the tags say, and nothing more.

        Collections are navigation, never protection (see
        ``docs/history/COLLECTIONS_PLAN.md``), so a short metadata chunk is logged
        and the rest is returned rather than raised on. The cost of that choice is
        a missing chip, never a wrong evidence reading. The key listing is still
        complete-or-raise through ``_iter_section_pages``, so a truncated page can
        never quietly shrink the section.
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

        # Deliberately not under ``_sweep_lock``, which the two GUID sweeps take.
        # This issues one request at a time, where the fan-out it replaces would
        # be eight, so it adds a single reader beside the sweeps. Serializing it
        # too would add its whole cost to the index phase instead of letting it
        # overlap.
        return await self._call(read, what=f"read collection tags in section {section_key}")

    # -- writing -----------------------------------------------------------
    #
    # Everything below mutates Plex, so everything below requires
    # ``declared_mutation()`` and an armed instance. GuardedSession enforces both.
    # These methods cannot opt out.

    async def add_label(self, section_key: int, rating_keys: list[int], label: str) -> None:
        """Add a label to many items in one read plus one edit per chunk.

        The section is addressed by key, through ``sectionByID``, never by title.
        Two libraries can share a title, and plexapi's title lookup returns only
        the last match, so a title-addressed write would target the wrong library
        every pass (mirroring the collection detach). The caller has the key
        already.

        Verified against a live server: this preserves existing labels. Adding a
        second label leaves the first in place, so Reaper's "Leaving Soon" mark
        does not wipe whatever the owner had already put on their own media.

        This is assumed at runtime, not re-checked: the method adds the label and
        does not read the item back to confirm it landed. A future Plex release
        could in principle take the owner's own labels away silently, and nothing
        here would catch that. Re-reading every edited item to prove it stayed
        would cost more than the shelf write does today. If that property is ever
        enforced rather than trusted, the read-back belongs here.
        """
        if not rating_keys:
            return
        server = await self._connect()

        def write() -> None:
            section = server.library.sectionByID(section_key)
            for start in range(0, len(rating_keys), BATCH_SIZE):
                keys = rating_keys[start : start + BATCH_SIZE]
                # One /library/metadata/<id,id,...> read covers the whole chunk,
                # not one GET per item. fetchItem and fetchItems hit the same
                # endpoint, so a list of ids becomes a single multi-id path. The
                # batch edit itself is unchanged, so Plex's additive label write
                # still preserves existing labels, the verified property above. An
                # item removed from Plex since the scan simply does not come back
                # from the read and is skipped, never treated as a failed
                # reconcile.
                items = section.fetchItems(keys)
                if items:
                    section.batchMultiEdits(items).addLabel(label).saveMultiEdits()

        await self._call(write, what=f"add label {label!r}")

    async def remove_label(self, section_key: int, rating_keys: list[int], label: str) -> None:
        """Remove a label from many items.

        The section is addressed by key, through ``sectionByID``, never by title,
        exactly as :meth:`add_label` and the collection detach do. Two libraries
        can share a title, and the title lookup returns only the last match.

        Matched case-insensitively against what Plex actually stored, because
        Plex title-cases the tag on the way in. A case-sensitive removal would
        silently remove nothing, which, for a "Leaving Soon" mark, means the item
        stays flagged to every user long after it was reprieved.
        """
        if not rating_keys:
            return
        server = await self._connect()
        wanted = normalize_label(label)

        def write() -> None:
            section = server.library.sectionByID(section_key)
            for start in range(0, len(rating_keys), BATCH_SIZE):
                keys = rating_keys[start : start + BATCH_SIZE]
                # One multi-id read covers the chunk, since the metadata carries
                # each item's labels, so there is no per-item fetch and no
                # per-item reload. Only items that actually carry the label are
                # edited, grouped by the exact spelling Plex stored, so removal
                # stays case-correct against Plex's title-casing. This collapses
                # to one write per spelling per chunk, in practice one, instead of
                # one write per item.
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

        This is the write behind the first shelf in a library. ``POST
        /library/collections`` is one of the exact shapes ``benign_shelf_write``
        permits, so it is writable while read-only only when the operator opted
        in. This creates the collection with its items inline, the same ``uri``
        form plexapi itself uses, rather than create-then-add, because Plex
        refuses to create an empty collection. Items beyond the first batch are
        added by the caller through :meth:`add_to_collection`.
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

        ``PUT /library/collections/{key}/items`` is a benign shelf shape. Items
        already on the collection are left as they are by Plex, so re-adding is
        harmless.
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

        A "dumb" collection's membership is the ``collection`` tag, confirmed
        against a live server, so a batch tag edit, ``collection[].tag.tag-={name}``
        on ``PUT /library/sections/{key}/all``, the same endpoint the label edit
        uses, detaches many items at once. A per-item ``DELETE
        .../children/{ratingKey}`` would instead cost one round-trip per item,
        minutes on a large shelf. The ``-`` form removes only the named
        collection, so an item's other collections are left in place. The reads
        batch too: one ``/library/metadata/<ids>`` read per chunk, never one fetch
        per item.

        The section is addressed by key, through ``sectionByID``, never by title.
        Two libraries can share a title, and plexapi's title lookup returns the
        last match, so a title-addressed detach would fail against the first twin
        every pass.

        The collection is removed by the exact spelling Plex stored, grouped the
        way :meth:`remove_label` groups labels. A "dumb" collection is a tag, and
        a case-sensitive removal of a case-variant spelling would silently remove
        nothing. An item that left grace would then stay on the shelf forever
        while the outcome claimed it was detached. ``find_collection``
        deliberately adopts a case-variant collection, so this path must be ready
        to remove one.

        plexapi's ``removeCollection`` locks the collection field on every edited
        item, and that is a deliberate choice here (``locked=True``, stated
        explicitly). ``locked=False`` would not just leave the field alone, it
        would actively clear an operator's own collection locks, so locking is
        the smaller change.

        Only ever called when the collection keeps at least one member
        afterward. A full clear goes through :meth:`delete_collection`, so
        batch-removing is never asked to empty a collection and never depends on
        Plex's empty-collection cleanup.
        """
        if not rating_keys:
            return
        server = await self._connect()
        wanted = normalize_label(name)

        def write() -> None:
            section = server.library.sectionByID(section_key)
            for start in range(0, len(rating_keys), BATCH_SIZE):
                keys = rating_keys[start : start + BATCH_SIZE]
                # One multi-id read covers the chunk, since the metadata carries
                # each item's collection tags. Group by the exact stored spelling
                # that casefold-matches the shelf name, so a case-variant
                # collection is still detached (mirrors remove_label). An item
                # removed from Plex since the scan does not come back and is
                # skipped.
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

        Verified against a live server: ``editTitle`` issues ``PUT
        /library/sections/{key}/all?type=18&id={key}&title.value=...``, the same
        batch edit shape the label writes use, so ``_benign_shape`` already
        permits it and renaming adds no new write shape to the guard. The
        collection is read by rating key first, which is a GET.

        This renames in place rather than dropping the shelf and rebuilding it
        under the new name. The rating key survives, so a poster, a sort title, or
        a pin on someone's Plex Home screen survives with it, which is what an
        operator renaming their shelf expects.

        plexapi locks the title field, as a rename in Plex Web does. Nothing else
        re-titles a collection behind Reaper's back, so the lock costs nothing and
        stops an agent refresh from undoing the operator's name.
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

        ``DELETE /library/collections/{key}`` is a benign shelf shape. This is how
        an emptied "Leaving Soon" shelf disappears in one request, instead of one
        ``DELETE .../children/{ratingKey}`` per member, minutes of serial
        round-trips on a large shelf. It removes only the collection object. The
        items and their files are untouched, since that is a different path,
        ``/library/metadata/{key}``, which the guard never permits under the
        benign branch.
        """
        server = await self._connect()

        def write() -> None:
            server.query(  # type: ignore[no-untyped-call]
                f"/library/collections/{collection_key}",
                method=server._session.delete,
            )

        await self._call(write, what=f"delete collection {collection_key}")

    async def refresh_path(self, section_key: int, path: str) -> None:
        """Rescan one directory, not the whole section.

        ``section.update(path=...)`` is a cheap partial scan. ``section.refresh()``
        is a full metadata re-download for every item in the library. They are one
        word apart and differ by orders of magnitude in cost, and confusing them
        on a large library causes an outage.

        Addressed by key, through ``sectionByID``, like every other write here. A
        title lookup with two same-titled libraries would scan the wrong one,
        which both misses the deleted file and leaves the trash interlock reading
        a library this run never touched.

        plexapi issues this as a GET, but it acts as a mutation: on a server set
        to empty trash after every scan, rescanning a path with missing files
        purges those items. ``GuardedSession`` therefore classifies this path as a
        mutation (``_GET_SHAPED_MUTATIONS``), so this call requires arming plus the
        executor's ``declared_mutation``, exactly like ``empty_trash``.
        """
        server = await self._connect()

        def write() -> None:
            server.library.sectionByID(section_key).update(path=path)

        await self._call(write, what=f"refresh {path!r}")

    async def empty_trash(self, section_key: int) -> None:
        """Purge one section's trash, the items Plex marked missing after a refresh.

        This is the most dangerous call in the whole application. An unmounted
        library, a scan that finds nothing, and then this, is how a whole Plex
        library is lost. So the executor gates it behind a count-delta check and
        only ever runs it after confirming the section shrank by roughly what was
        deleted and no more. This method does the purge. Deciding it is safe to
        purge is the caller's job, deliberately kept out of here so the interlock
        cannot be bypassed by calling the client directly.

        A mutation, so it is guarded like a delete: it needs arming plus the
        executor's ``declared_mutation``. Scoped to one section, never the whole
        library, and that section is addressed by key, through ``sectionByID``.
        Purging the trash of whichever same-titled library plexapi happened to
        return first would be a whole library's worth of blast radius on the
        app's most dangerous call.
        """
        server = await self._connect()

        def write() -> None:
            server.library.sectionByID(section_key).emptyTrash()

        await self._call(write, what=f"empty trash for section {section_key}")
