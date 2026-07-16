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
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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
    read-only mode *if the operator opted in on the host*. It never permits a delete.
    """
    token = _benign_label.set(True)
    try:
        yield
    finally:
        _benign_label.reset(token)


class GuardedSession(requests.Session):
    """``GuardedTransport``'s twin, for the one library that does not use httpx."""

    def __init__(self, safety: RuntimeSafety) -> None:
        super().__init__()
        self._safety = safety

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        if method.upper() not in SAFE_METHODS:
            if _benign_label.get():
                # A "Leaving Soon" label write: reversible, touches no file. Gated like a
                # delete by default (needs arming); an operator may opt in to allowing it
                # while read-only so the grace-period warning can appear before deletion
                # is ever enabled. This branch can NEVER permit a deletion -- only the
                # Leaving Soon sync sets the flag, and it writes only labels.
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

    def __init__(self, base_url: str, token: str, *, safety: RuntimeSafety) -> None:
        self._base_url = base_url
        self._token = token
        self._safety = safety
        self._server: PlexServer | None = None

    async def connect(self) -> PlexServer:
        """The underlying plexapi server, connected once and reused.

        Exposed for the list providers, which speak plexapi directly. Reads through it
        still pass the GuardedSession -- which permits GETs and refuses writes -- so even
        a list refresh cannot mutate Plex.
        """
        return await self._connect()

    async def _connect(self) -> PlexServer:
        if self._server is not None:
            return self._server

        def build() -> PlexServer:
            from plexapi.server import PlexServer as _PlexServer

            # The guarded session, not plexapi's default. Every write plexapi makes
            # goes through it, so Plex is held to the same rule as everything else.
            return _PlexServer(  # type: ignore[no-untyped-call]
                self._base_url, self._token, session=GuardedSession(self._safety)
            )

        try:
            self._server = await asyncio.to_thread(build)
        except Exception as exc:
            raise PlexError(f"Could not reach Plex at {self._base_url}: {exc}") from exc
        return self._server

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
        ``section_type`` (``"movie"`` or ``"show"``), sweep every item once via
        ``section.all()`` and read its GUIDs -- the new-agent ``guids`` list *and* the
        legacy single ``guid`` string -- plus **every** file behind the listing with its
        name and exact byte size (the first location's leaf feeds the global basename
        tier; the full set exists so an ambiguous id can be narrowed against all of a
        candidate's files, and byte-identical re-lists of one file can be recognised),
        and the title / year / added-at the listing already carries. One sweep per
        section, never a per-item metadata call. Returning full :class:`PlexItem` rows
        (not just the ids) lets the index builders union in items the Tautulli media-info
        cache has not listed yet -- a freshly added item exists here first.

        A GET, so it runs in read-only mode through the ``GuardedSession``. It **raises**
        ``PlexError`` on any failure rather than returning a partial map, so the caller can
        fail closed and degrade the snapshot: silently falling the whole library back to
        title-only matching at the moment the id signal vanished is exactly the fail-open
        this feature exists to prevent.
        """
        server = await self._connect()

        def read() -> dict[int, PlexItem]:
            from datetime import UTC, datetime

            out: dict[int, PlexItem] = {}
            for section in server.library.sections():
                if section.type != section_type:
                    continue
                for item in section.all():
                    rating_key = getattr(item, "ratingKey", None)
                    if rating_key is None:
                        continue
                    guid_ids = [
                        str(getattr(guid, "id", "")) for guid in getattr(item, "guids", None) or []
                    ]
                    legacy = getattr(item, "guid", None)
                    ids = parse_guids(guid_ids, str(legacy) if legacy else None)
                    locations = list(getattr(item, "locations", None) or [])
                    basename = to_basename(locations[0]) if locations else None
                    # Movies carry media parts (file path + exact byte size); shows carry
                    # folder locations only, which have no size. A missing or zero size is
                    # recorded as None (unknown), never as a comparable number.
                    #
                    # Display metadata rides the same listing XML, so reading it here costs
                    # nothing. All of these attributes are set by plexapi's _loadData for
                    # listing rows (None when the server omits one), so plain attribute
                    # reads never trigger the implicit per-item reload plexapi fires for
                    # attributes it has never seen.
                    files: list[PlexFile] = []
                    video_resolution: str | None = None
                    for media in getattr(item, "media", None) or []:
                        if video_resolution is None:
                            raw_res = getattr(media, "videoResolution", None)
                            video_resolution = str(raw_res) if raw_res else None
                        for part in getattr(media, "parts", None) or []:
                            leaf = to_basename(getattr(part, "file", None))
                            if leaf is None:
                                continue
                            size = getattr(part, "size", None)
                            files.append(
                                PlexFile(
                                    basename=leaf,
                                    size=int(size) if isinstance(size, int) and size > 0 else None,
                                )
                            )
                    if not files:
                        files = [
                            PlexFile(basename=leaf)
                            for leaf in (to_basename(loc) for loc in locations)
                            if leaf is not None
                        ]
                    year = getattr(item, "year", None)
                    # plexapi parses addedAt with fromtimestamp() in *this* process, so a
                    # naive value means "this host's local time"; astimezone(UTC) recovers
                    # the exact instant regardless of the Plex server's own timezone.
                    added = getattr(item, "addedAt", None)
                    added_at = added.astimezone(UTC) if isinstance(added, datetime) else None
                    # Ratings with provenance: the *RatingImage tells us what each number
                    # IS (imdb, RT, tmdb); a number whose image is missing is dropped by
                    # from_plex rather than guessed at. The audience flag routes an RT
                    # image in the audience slot to the audience score.
                    plex_ratings = [
                        r
                        for r in (
                            from_plex(
                                getattr(item, "rating", None),
                                getattr(item, "ratingImage", None),
                            ),
                            from_plex(
                                getattr(item, "audienceRating", None),
                                getattr(item, "audienceRatingImage", None),
                                audience=True,
                            ),
                        )
                        if r is not None
                    ]
                    content_rating = getattr(item, "contentRating", None)
                    duration = getattr(item, "duration", None)
                    out[int(rating_key)] = PlexItem(
                        rating_key=int(rating_key),
                        title=str(getattr(item, "title", "") or ""),
                        year=int(year) if isinstance(year, int) and year > 0 else None,
                        added_at=added_at,
                        ids=ids,
                        file_basename=basename,
                        files=tuple(files),
                        video_resolution=video_resolution,
                        content_rating=str(content_rating) if content_rating else None,
                        runtime_minutes=(
                            duration // 60_000
                            if isinstance(duration, int) and duration > 0
                            else None
                        ),
                        ratings=tuple(plex_ratings),
                    )
            return out

        try:
            return await asyncio.to_thread(read)
        except Exception as exc:
            raise PlexError(
                f"Could not sweep Plex GUIDs for {section_type} sections: {exc}"
            ) from exc

    async def item_count(self, section_title: str) -> int:
        """How many items a section holds. The input to the trash interlock."""
        server = await self._connect()

        def read() -> int:
            return int(server.library.section(section_title).totalSize)

        return await asyncio.to_thread(read)

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

    async def labels(self, section_title: str, rating_key: int) -> list[str]:
        """The labels currently on an item, as Plex actually stores them.

        Returned verbatim (so the UI shows what is really there), but compare them with
        ``normalise_label`` -- Plex title-cases what you write.
        """
        server = await self._connect()

        def read() -> list[str]:
            item = server.library.section(section_title).fetchItem(rating_key)
            item.reload()
            return [str(label.tag) for label in item.labels]

        try:
            return await asyncio.to_thread(read)
        except Exception as exc:
            raise PlexError(f"Could not read labels for {rating_key}: {exc}") from exc

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
