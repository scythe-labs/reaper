# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Plex Media Server client.

Wraps ``plexapi``, which is synchronous (it is ``requests`` underneath), so every call
is pushed off the event loop with ``asyncio.to_thread``.

We use ``plexapi`` rather than raw HTTP on purpose: it encodes the exact URI forms for
batch edits and hub promotion that other tools in this space have provably got wrong,
and those are the calls where being wrong means silently mangling the owner's library.

Three behaviours here were established against a live server, and each one contradicts
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
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

import requests
import structlog

from reaper.clients.base import SAFE_METHODS, SafetyViolationError
from reaper.config import RuntimeSafety
from reaper.engine.identity import PlexFile, PlexItem, parse_guids, to_basename
from reaper.ratings import from_plex

if TYPE_CHECKING:
    from plexapi.server import PlexServer

log = structlog.get_logger(__name__)

#: plexapi issues one PUT per chunk for a batch edit. 100 is Kometa's battle-tested size.
BATCH_SIZE = 100

#: One listing page of the GUID sweep. Big pages are strictly better here (the cost is
#: per item, not per page), bounded so a huge library never materialises in one response.
SWEEP_PAGE_SIZE = 1000

#: Rating keys per batched ``/library/metadata/{ids}`` read (show folder paths).
METADATA_BATCH_SIZE = 100


def _parse_sweep_element(el: Element) -> PlexItem:
    """One listing row (a movie ``Video`` or show ``Directory``) as a :class:`PlexItem`.

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
            leaf = to_basename(part.get("file"))
            if leaf is None:
                continue
            raw_size = part.get("size")
            size = int(raw_size) if raw_size and raw_size.isdigit() else None
            files.append(PlexFile(basename=leaf, size=size if size and size > 0 else None))

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
# That is not a gap we can accept. An unmounted library plus a scan plus emptyTrash is
# how people lose an entire Plex library, and it would have been the single destructive
# call in the codebase with no safety interlock in front of it.
#
# So the same rule is enforced on the requests side: a mutating call requires deletion
# to be enabled AND an explicit declaration from the executor, which writes its intent
# to the durable journal before it declares. The declaration is a context variable
# rather than a per-request flag only because plexapi gives us nowhere to hang one.

_declared: ContextVar[bool] = ContextVar("reaper_plex_mutation_declared", default=False)

#: Marks the enclosed writes as the "Leaving Soon" label reconcile -- a reversible
#: mutation that touches no files. Distinct from ``_declared`` so the two paths cannot be
#: confused: a deletion is armed + journalled; a label write is armed OR host-opted-in.
_benign_label: ContextVar[bool] = ContextVar("reaper_plex_benign_label", default=False)


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
def benign_label_write() -> Iterator[None]:
    """Mark the enclosed writes as the "Leaving Soon" label reconcile.

    Set **only** by the Leaving Soon sync path, exactly as ``declared_mutation`` is set
    only by the executor -- the narrowness is what keeps this from becoming a general
    licence to write. It tells :class:`GuardedSession` the write is the reversible,
    file-touching-nothing label rather than a deletion, so it may be permitted in
    read-only mode *if the operator opted in on the host*. It never permits a delete,
    and that is enforced structurally, not by call-site discipline: the benign branch
    matches only a PUT to the batch label-edit endpoint (``_LABEL_EDIT``); any other
    verb or path inside this block falls back to the armed-and-declared rule.
    """
    token = _benign_label.set(True)
    try:
        yield
    finally:
        _benign_label.reset(token)


#: GET-shaped mutations. Plex triggers a section scan with ``GET
#: /library/sections/{key}/refresh`` -- and on a server with "Empty trash automatically
#: after every scan" enabled, scanning a path with missing files purges those items'
#: library records. Method filtering alone would wave that straight through (the same
#: reason the Tautulli client carries a command allow-list), so the guard classifies
#: these paths as mutations regardless of verb.
_GET_SHAPED_MUTATIONS = re.compile(r"^/library/sections/[^/]+/refresh$")

#: The one write shape ``benign_label_write`` may permit: plexapi's batch label edit,
#: a PUT to ``/library/sections/{key}/all``.
_LABEL_EDIT = re.compile(r"^/library/sections/[^/]+/all$")


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
            if (
                _benign_label.get()
                and method.upper() == "PUT"
                and _LABEL_EDIT.match(path) is not None
            ):
                # The "Leaving Soon" label reconcile: reversible, touches no file, and
                # structurally confined to the batch label-edit endpoint. Gated like a
                # delete by default (needs arming); an operator may opt in to allowing
                # it while read-only so the grace-period warning can appear before
                # deletion is ever enabled. This branch can NEVER permit a deletion:
                # a DELETE, or a PUT to any other path, does not match it and falls
                # through to the armed-and-declared rule below.
                if not self._safety.leaving_soon_write_allowed:
                    raise SafetyViolationError(
                        f"Blocked {method} to Plex (Leaving Soon label). Enable deletion, "
                        "or set REAPER_ALLOW_UNARMED_LEAVING_SOON=true on the host to allow "
                        "this benign, reversible label write while read-only."
                    )
            elif not self._safety.destructive_allowed:
                raise SafetyViolationError(
                    f"Blocked {method} to Plex. {self._safety.why_blocked()}"
                )
            elif not _declared.get():
                raise SafetyViolationError(
                    f"Blocked {method} to Plex: this mutation was not declared to the "
                    "action journal. Destructive calls must go through the action "
                    "executor so that they are recorded before they are sent."
                )
        return super().request(method, url, *args, **kwargs)


def normalise_label(tag: str) -> str:
    """The comparison form for a label tag.

    Plex title-cases what you write: ``leaving-soon`` comes back as ``Leaving-Soon``.
    Comparing raw tags therefore silently fails to match a label that is present, so
    every comparison goes through here.
    """
    return tag.strip().casefold()


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
            return _PlexServer(  # type: ignore[no-untyped-call]
                self._base_url,
                self._token,
                session=GuardedSession(self._safety, verify=self._verify),
            )

        async with self._connect_lock:
            # Re-checked under the lock: a concurrent caller may have connected while
            # this one waited, and building twice would leak a session.
            connected = self._server
            if connected is not None:
                return connected
            try:
                built = await asyncio.to_thread(build)
            except Exception as exc:
                raise PlexError(f"Could not reach Plex at {self._base_url}: {exc}") from exc
            self._server = built
            return built

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

    async def section_paths(self) -> dict[str, list[str]]:
        """Every library section and the paths it covers.

        The raw material for the path-mapping table. Radarr says ``/movies``; Plex says
        ``/media/movies``. The partial-refresh path comes from the *arr, so without a
        mapping the refresh silently rescans nothing at all.
        """
        server = await self._connect()

        def read() -> dict[str, list[str]]:
            return {s.title: list(s.locations) for s in server.library.sections()}

        return await asyncio.to_thread(read)

    async def library_guid_index(self, *, section_type: str) -> dict[int, PlexItem]:
        """Every library item as the resolver sees it, keyed by Plex ``rating_key``.

        The enrichment behind id-based matching. For each library section of
        ``section_type`` (``"movie"`` or ``"show"``), page the section's ``/all``
        listing (with ``includeGuids=1``) and parse the container XML directly: the
        new-agent ``Guid`` children *and* the legacy single ``guid`` attribute, plus
        **every** file behind the listing with its name and exact byte size (the first
        location's leaf feeds the global basename tier; the full set exists so an
        ambiguous id can be narrowed against all of a candidate's files, and
        byte-identical re-lists of one file can be recognised), the display metadata
        (ratings with provenance, certification, runtime, resolution), and the
        title / year / added-at the listing already carries.

        Parsed RAW, not through ``section.all()``, and the reason is measured, not
        stylistic: plexapi reloads a partial object the first time any accessed
        attribute is ``None``, and on a listing row some attribute always is -- so the
        object walk silently issued one metadata request *per item* and the "single
        sweep" cost minutes on a large library. Reading the container XML makes a
        missing attribute honestly ``None`` with no hidden network call, and the sweep
        is a handful of page requests. Show folder paths are the one thing ``/all``
        never carries, so shows add batched ``/library/metadata/{ids}`` reads (100 per
        call) for their ``Location`` -- the folder-name tier that narrows a show listed
        in two sections. Returning full :class:`PlexItem` rows (not just the ids) lets
        the index builders union in items the Tautulli media-info cache has not listed
        yet -- a freshly added item exists here first.

        GETs only, so it runs in read-only mode through the ``GuardedSession``. It
        **raises** ``PlexError`` on any failure rather than returning a partial map, so
        the caller can fail closed and degrade the snapshot: silently falling the whole
        library back to title-only matching at the moment the id signal vanished is
        exactly the fail-open this feature exists to prevent.
        """
        server = await self._connect()

        def read() -> dict[int, PlexItem]:
            out: dict[int, PlexItem] = {}
            show_keys: list[int] = []
            for section in server.library.sections():
                if section.type != section_type:
                    continue
                start = 0
                while True:
                    container = server.query(  # type: ignore[no-untyped-call]
                        f"/library/sections/{section.key}/all"
                        f"?includeGuids=1&X-Plex-Container-Start={start}"
                        f"&X-Plex-Container-Size={SWEEP_PAGE_SIZE}"
                    )
                    elements = [el for el in container if el.get("ratingKey")]
                    for el in elements:
                        item = _parse_sweep_element(el)
                        out[item.rating_key] = item
                        if section_type == "show":
                            show_keys.append(item.rating_key)
                    start += len(elements)
                    total = int(container.get("totalSize") or container.get("size") or 0)
                    # A short page always ends the section, whether or not the server
                    # reported a total -- never trust one signal alone to terminate.
                    short_page = not elements or len(elements) < SWEEP_PAGE_SIZE
                    if short_page or (total and start >= total):
                        break

            # Show folders arrive from batched metadata reads; each Location leaf
            # becomes the show's basename exactly as the object walk produced it.
            for chunk_start in range(0, len(show_keys), METADATA_BATCH_SIZE):
                chunk = show_keys[chunk_start : chunk_start + METADATA_BATCH_SIZE]
                batch = server.query(  # type: ignore[no-untyped-call]
                    "/library/metadata/" + ",".join(str(k) for k in chunk)
                )
                for el in batch:
                    rk = el.get("ratingKey")
                    if rk is None or int(rk) not in out:
                        continue
                    paths = [loc.get("path") for loc in el.findall("Location") if loc.get("path")]
                    leaves = [leaf for leaf in (to_basename(p) for p in paths) if leaf]
                    if not leaves:
                        continue
                    item = out[int(rk)]
                    out[int(rk)] = replace(
                        item,
                        file_basename=leaves[0],
                        files=item.files or tuple(PlexFile(basename=leaf) for leaf in leaves),
                    )
            return out

        async with self._sweep_lock:
            try:
                return await asyncio.to_thread(read)
            except Exception as exc:
                raise PlexError(
                    f"Could not sweep Plex GUIDs for {section_type} sections: {exc}"
                ) from exc

    async def item_count(self, section_title: str) -> int:
        """How many items a section holds. The input to the trash interlock: the executor
        reads this before its first delete and again before purging, and refuses the purge
        unless the section shrank by no more than what the run deleted under it."""
        server = await self._connect()

        def read() -> int:
            return int(server.library.section(section_title).totalSize)

        try:
            return await asyncio.to_thread(read)
        except Exception as exc:
            raise PlexError(f"Could not count items in {section_title!r}: {exc}") from exc

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

        await asyncio.to_thread(close)

    async def __aenter__(self) -> PlexClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def is_refreshing(self, section_title: str) -> bool:
        """Is a scan currently running on this section?

        A refresh (``section.update(path=...)``) fires an asynchronous scan; the executor
        polls this so it does not empty the trash before Plex has actually noticed the
        deleted file is gone. A read. On any error it reports ``True`` (still busy), so the
        caller waits rather than racing ahead."""
        server = await self._connect()

        def read() -> bool:
            section = server.library.section(section_title)
            section.reload()
            return bool(getattr(section, "refreshing", False))

        try:
            return await asyncio.to_thread(read)
        except Exception as exc:
            log.warning("plex.refresh_state_unreadable", section=section_title, error=str(exc))
            return True

    async def movie_section_titles(self) -> list[str]:
        """The titles of the movie libraries. Leaving Soon operates on movies (the reap
        loop is movies-first), so this is where its label lives. A read."""
        server = await self._connect()

        def read() -> list[str]:
            return [s.title for s in server.library.sections() if s.type == "movie"]

        try:
            return await asyncio.to_thread(read)
        except Exception as exc:
            raise PlexError(f"Could not list Plex movie sections: {exc}") from exc

    async def items_with_label(self, section_title: str, label: str) -> set[int]:
        """The rating keys in a section currently carrying ``label``.

        A read (``section.search`` is a GET), so it works in read-only mode -- which is
        what makes the "Leaving Soon" reconcile computable without arming anything: you can
        see what is marked and what *would* change before any write is permitted. Matched
        the way Plex stored it; the search term is the label's display form.
        """
        server = await self._connect()

        def read() -> set[int]:
            section = server.library.section(section_title)
            return {int(item.ratingKey) for item in section.search(label=label)}

        try:
            return await asyncio.to_thread(read)
        except Exception as exc:
            raise PlexError(f"Could not read items labelled {label!r}: {exc}") from exc

    # -- writing -----------------------------------------------------------
    #
    # Everything below mutates Plex, so everything below requires ``declared_mutation()``
    # and an armed instance. GuardedSession enforces both; these methods cannot opt out.

    async def add_label(self, section_title: str, rating_keys: list[int], label: str) -> None:
        """Add a label to many items in one request per chunk.

        **Verified against a live server: this PRESERVES existing labels.** Adding a
        second label leaves the first in place, so Reaper's "Leaving Soon" mark does not
        wipe whatever the owner had already put on their own media. That was the single
        most dangerous open question about this feature and the answer is the safe one --
        but it is asserted here rather than assumed, because if a future Plex release
        changed it, the failure would be silent and would destroy user data.
        """
        if not rating_keys:
            return
        server = await self._connect()

        def write() -> None:
            section = server.library.section(section_title)
            for start in range(0, len(rating_keys), BATCH_SIZE):
                chunk = [section.fetchItem(k) for k in rating_keys[start : start + BATCH_SIZE]]
                section.batchMultiEdits(chunk).addLabel(label).saveMultiEdits()

        try:
            await asyncio.to_thread(write)
        except SafetyViolationError:
            raise
        except Exception as exc:
            raise PlexError(f"Could not add label {label!r}: {exc}") from exc

    async def remove_label(self, section_title: str, rating_keys: list[int], label: str) -> None:
        """Remove a label from many items.

        Matched case-insensitively against what Plex actually stored, because it will
        have title-cased the tag on the way in. A case-sensitive removal silently removes
        nothing -- which, for a "Leaving Soon" mark, means the item stays flagged to
        every user long after it was reprieved.
        """
        if not rating_keys:
            return
        server = await self._connect()
        wanted = normalise_label(label)

        def write() -> None:
            section = server.library.section(section_title)
            for start in range(0, len(rating_keys), BATCH_SIZE):
                chunk = []
                for key in rating_keys[start : start + BATCH_SIZE]:
                    item = section.fetchItem(key)
                    item.reload()
                    # Only touch items that actually carry it, and remove it under the
                    # spelling Plex is really using.
                    for existing in item.labels:
                        if normalise_label(str(existing.tag)) == wanted:
                            chunk.append((item, str(existing.tag)))
                            break

                for item, actual_tag in chunk:
                    section.batchMultiEdits([item]).removeLabel(actual_tag).saveMultiEdits()

        try:
            await asyncio.to_thread(write)
        except SafetyViolationError:
            raise
        except Exception as exc:
            raise PlexError(f"Could not remove label {label!r}: {exc}") from exc

    async def refresh_path(self, section_title: str, path: str) -> None:
        """Rescan **one directory**, not the whole section.

        ``section.update(path=...)`` is a cheap partial scan. ``section.refresh()`` is a
        full metadata re-download for every item in the library. They are one word apart
        and differ by orders of magnitude, and confusing them on a large library is an
        outage.

        plexapi issues this as a **GET**, but it is a mutation in effect: on a server set
        to empty trash after every scan, rescanning a path with missing files purges those
        items. ``GuardedSession`` therefore classifies the path as a mutation
        (``_GET_SHAPED_MUTATIONS``), so this call requires arming plus the executor's
        ``declared_mutation``, exactly like ``empty_trash``.
        """
        server = await self._connect()

        def write() -> None:
            server.library.section(section_title).update(path=path)

        try:
            await asyncio.to_thread(write)
        except SafetyViolationError:
            raise
        except Exception as exc:
            raise PlexError(f"Could not refresh {path!r}: {exc}") from exc

    async def empty_trash(self, section_title: str) -> None:
        """Purge one section's trash: the items Plex marked missing after a refresh.

        The single most dangerous call in the whole application. An unmounted library, a
        scan that finds nothing, and then this, is how a whole Plex library is lost -- so
        the executor gates it behind a count-delta check and only ever runs it *after*
        confirming the section shrank by roughly what was deleted and no more. This method
        does the purge; deciding it is safe to purge is the caller's job, deliberately kept
        out of here so the interlock cannot be bypassed by calling the client directly.

        A mutation, so it is guarded like a delete: it needs arming plus the executor's
        ``declared_mutation``. Scoped to one section, never the whole library.
        """
        server = await self._connect()

        def write() -> None:
            server.library.section(section_title).emptyTrash()

        try:
            await asyncio.to_thread(write)
        except SafetyViolationError:
            raise
        except Exception as exc:
            raise PlexError(f"Could not empty trash for {section_title!r}: {exc}") from exc
