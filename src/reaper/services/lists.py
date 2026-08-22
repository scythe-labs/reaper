# SPDX-License-Identifier: AGPL-3.0-or-later
"""Curated lists as protections.

No competitor does this. Maintainerr, Janitorr and Reclaimerr all do exclusions via
tags, collections or their own database -- none of them ingest a *curated list* as a
protection source. "Never reap anything in the IMDb Top 250" is a rule you cannot
write in any of them.

Three providers, in ascending order of how much configuration they cost you:

**Plex collection** -- zero configuration, and the best of the three. You curate a
"Never Reap" collection in the Plex app you already use daily; it is editable from
your phone; there is no new screen to learn. Reaper just reads it.

***arr tag** -- also zero configuration. Tag a series `reaper-keep` in Sonarr.

**IMDb Top 250** -- one click. Served by Radarr's own list service at
``https://api.radarr.video/v1/list/imdb/top250``: 250 items, `TmdbId` and `ImdbId`,
**no auth**, verified live. (IMDb has no official API for the chart, and its
non-commercial datasets do *not* contain the ranking -- it uses an unpublished
weighted formula. Do not try to derive it, and do not scrape it. This mirror is the
right answer.)

## Rank, where a source actually has one

A list may act as a **hard gate** (never delete) or as a **soft signal** (weighted
negative pressure). Per list, not globally -- "IMDb Top 250" deserves a gate; "films I
might like" deserves a nudge.

``rank`` is stored *when the source provides one*, and left ``None`` otherwise. Note
that the IMDb Top 250 mirror does **not** provide one: its payload has no rank field
and comes back in roughly chronological order. Inferring rank from array position would
have reported "The Kid is #1 on the IMDb Top 250", which is false -- so it does not.
Being *on* the list is the signal.
"""

from __future__ import annotations

import enum
import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlsplit

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reaper.aio import per_loop_lock
from reaper.clients.arr import RadarrClient, SonarrClient
from reaper.clients.base import IntegrationError
from reaper.clients.public import PublicClient
from reaper.clock import from_epoch, utcnow
from reaper.engine import identity
from reaper.engine.observation import Absent, Known, Observation
from reaper.text import fold

if TYPE_CHECKING:
    # Annotation only. ``list_config`` imports this module at runtime, so the runtime
    # import would be circular; the definitions arrive as plain values either way.
    from reaper.services.list_config import ListDefinition

log = structlog.get_logger(__name__)

#: Radarr's own list mirror, the sanctioned source for IMDb charts and public lists (see
#: the module docstring). Path suffix per variant: a preset name maps to its chart path,
#: anything else is a public list id appended as-is. All three verified live: ``top250``,
#: ``popular``, and ``/ls<digits>`` each return the same ``TmdbId``/``ImdbId`` array.
IMDB_LIST_BASE = "https://api.radarr.video/v1/list/imdb/"
IMDB_TOP_250_URL = IMDB_LIST_BASE + "top250"

#: The shipped chart choices, keyed by the ``preset`` value a definition stores. The floor
#: is the truncation guard: a chart of known size that comes back much smaller is a broken
#: mirror, and installing it would silently stop protecting the titles that fell off. A
#: custom list has no known size, so its floor is 1 -- the empty-payload refusal in
#: ``ImdbList.fetch`` is what stands between a flaky mirror and an emptied protection.
IMDB_PRESETS: dict[str, tuple[str, int]] = {
    "top250": ("top250", 200),
    "popular": ("popular", 20),
}


class ContainerMissingError(RuntimeError):
    """A configured keep container (an *arr tag, a Plex collection) does not exist upstream.

    Distinct from "the container exists and is empty", and the distinction is what
    ``sync`` keys on: a vanished container over a POPULATED stored list keeps the previous
    membership and records the failure, while a container the owner simply has not created
    yet syncs as genuinely empty. A missing container must never read as [] -- that is how
    a renamed keep tag silently unprotects everything it used to cover.
    """


class ListMode(enum.StrEnum):
    HARD = "hard"
    """A gate. Never delete anything on this list."""

    SOFT = "soft"
    """A weighted negative signal, scaled by rank."""


class ListKind(enum.StrEnum):
    """Which protection a list feeds.

    The distinction matters in the why-panel. "Whitelisted" and "on the IMDb Top 250"
    are both reasons to keep a file, but they are *different* reasons, and collapsing
    them would tell the owner "whitelisted" about a film they never touched.
    """

    WHITELIST = "whitelist"
    """The owner said so directly -- an *arr tag, or a Plex collection they curate."""

    CURATED = "curated"
    """Somebody else's list -- the IMDb Top 250."""


#: The two kinds a stored protection row may be filed under. ``media_type`` is half of
#: every membership join key -- TMDb numbers movies and shows in separate spaces, so the
#: lookup matches on (kind, id) and never on the id alone (rule 52). A row filed under
#: anything else therefore matches nothing and protects nothing, while sitting in the
#: table looking healthy, so an entry Reaper cannot place in one of these two is never
#: stored at all.
PROTECTABLE_MEDIA_TYPES = frozenset({"movie", "tv"})


@dataclass(frozen=True, slots=True)
class ListItem:
    """One entry. Identified by whatever external ids the source gives us."""

    media_type: str  # "movie" | "tv", or whatever the source called it -- see is_protectable
    imdb_id: str | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    plex_rating_key: int | None = None
    """The Plex object's own key, where the source IS Plex. None everywhere else.

    A "Never Reap" collection can hold a title no agent ever matched -- a home video, a
    personal-media item, one whose guids stopped parsing -- and such an entry has no
    external id to be stored under, so it was dropped and protected by nothing. Plex's
    own key is the identity Plex is sure of, and it is the key the scan's Plex index is
    built on, so it binds the entry to the item the operator put on the list.

    **This is not stable identity, and it is not used as any.** A rating key moves when a
    file leaves and comes back, and it is server-scoped, so the same integer names a
    different title on a different server -- which is why `db.models` keys durable state
    on ``media_key`` and says so. What makes it safe here is that this is an *additive*
    join, rewritten from the live collection by every sync: it can only ever ADD a
    membership hit, never withdraw one an id already made, so an entry that has ids is
    unaffected either way. Both ways it can be wrong are survivable and both self-heal on
    the next sync. A key that moved matches nothing, leaving the entry exactly as
    unprotected as it was before this column existed, and :func:`_rows_to_store` keeps
    carrying its row by title in the meantime. A key that collides with another server's
    grants a keep nobody asked for, the direction this codebase fails in on purpose.
    """
    title: str = ""
    rank: int | None = None
    """1 = top of the list, WHERE THE SOURCE PROVIDES ONE.

    Left None otherwise. Never inferred from array position: the IMDb Top 250 mirror
    returns its entries roughly chronologically, so position would be a fabrication.
    """

    @property
    def has_any_id(self) -> bool:
        return bool(self.imdb_id or self.tmdb_id or self.tvdb_id)

    @property
    def is_protectable(self) -> bool:
        """Can this entry be stored as a row a membership lookup will actually find?

        Two ways it cannot. With no key of any kind -- no external id and no Plex key --
        there is nothing to match on. With a kind outside
        :data:`PROTECTABLE_MEDIA_TYPES` -- a Plex collection can hold objects that are
        neither a movie nor a show -- the keys belong to a space no lookup asks for, and
        filing them under "movie" anyway would hand a film that happens to share the
        number a keep the owner never asked for (rule 52).

        An entry that fails this stays in the fetch rather than disappearing from it, so
        ``sync`` still sees a populated container and still holds whatever the last good
        sync stored for that title.
        """
        keyed = self.has_any_id or self.plex_rating_key is not None
        return keyed and self.media_type in PROTECTABLE_MEDIA_TYPES


class ListProvider(Protocol):
    """A source of protected items.

    ``slug`` and ``display_name`` are read-only *properties*, not attributes: a
    provider like ``ArrTagRule`` derives them from its configuration, and a Protocol
    declaring a mutable attribute would not accept that.
    """

    @property
    def slug(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    @property
    def list_name(self) -> str | None: ...

    """The operator's own name for this list, from its definition on Settings -> Lists.
    ``None`` for a row derived from configuration that predates the registry. Declared
    here because :func:`rule_name` is derived from it for every source at once."""

    async def fetch(self) -> list[ListItem]: ...


def rule_name(provider: ListProvider) -> str:
    """The name a policy keep rule matches this list by, which is not its display name.

    ``ArrTagRule.display_name`` puts the server after the operator's name ("Keepers (4k)"),
    because one list is one stored row per *arr and the row has to say which. A keep rule
    stores the name the operator typed, once, and ``on_list`` matches per element and
    exactly -- so a fact built from display names matched nothing, and every tag list
    protected nothing while the Lists screen reported it healthy (#507).

    So the matched name is STORED (``protection_list.rule_name``) rather than derived from
    a display string, and derived here for both writers at once: :func:`sync` on success and
    :func:`_record_sync_error` on a first failure, which must agree or a list's protection
    would depend on whether its first check landed.
    """
    return provider.list_name or provider.display_name


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImdbList:
    """An IMDb chart or public list, via Radarr's own list service. No auth, no key.

    ``variant`` is a preset key (:data:`IMDB_PRESETS`) or a public list id (``ls…``); the
    definition on Settings -> Lists stores which. Movies only, because the mirror is
    Radarr's own service and serves the movie halves of these lists.

    **Membership is binary. There is no rank.**

    The payload carries no rank, position or order field, and the Top 250 comes back in
    roughly *chronological* order -- the first entry is from 1921. Taking the array index
    as a chart position would have told the owner "this film is #1", which is simply
    false, and the why-panel would have been confidently lying. So ``rank`` is left None.
    Being *on* the list is the signal.
    """

    variant: str = "top250"
    #: The definition this was synced for. ``None`` only in a test or a one-off refresh that
    #: has no registry to read; production always builds it from the definition row.
    list_id: int | None = None
    list_name: str | None = None

    @property
    def slug(self) -> str:
        return f"{IMDB_PREFIX}{self.variant}{list_suffix(self.list_id)}"

    @property
    def display_name(self) -> str:
        return self.list_name or f"IMDb {self.variant}"

    async def fetch(self) -> list[ListItem]:
        path, floor = IMDB_PRESETS.get(self.variant, (self.variant, 1))
        # Through clients/ like every other fetch (rule 33): the shared retry, timeout,
        # error-mapping and redirect policy, instead of a bespoke httpx use down here.
        parts = urlsplit(IMDB_LIST_BASE + path)
        async with PublicClient(f"{parts.scheme}://{parts.netloc}") as client:
            payload = await client.get_json(parts.path)

        if not isinstance(payload, list):
            raise IntegrationError(self.slug, "error.integration.unexpected_shape", path=parts.path)

        # Through identity.ExternalIds.of like every other id write: the mirror emits
        # `tt0000000` for a title it has no IMDb id for, and stored raw that row would be
        # matched by every other item carrying the same sentinel (#709). The entry still
        # becomes a ListItem either way; `is_protectable` is what decides whether an entry
        # with no usable key is stored.
        items: list[ListItem] = []
        for entry in payload:
            ids = identity.ExternalIds.of(imdb=entry.get("ImdbId"), tmdb=entry.get("TmdbId"))
            items.append(
                ListItem(
                    media_type="movie",
                    imdb_id=ids.imdb,
                    tmdb_id=ids.tmdb,
                    title=str(entry.get("Title") or ""),
                    rank=None,  # See the class docstring. The source does not carry one.
                )
            )

        if len(items) < floor:
            # A truncated or empty answer is worse than no answer: installing it would
            # silently stop protecting the titles that fell off. An IMDb chart is never
            # genuinely empty, so even the floor of 1 refuses an empty custom list.
            raise IntegrationError(
                self.slug, "error.integration.imdb_list_truncated", floor=floor, count=len(items)
            )
        return items


def _tag_key(tag: str) -> str:
    """The comparison form for an *arr tag label.

    Sonarr and Radarr lower-case and trim every label on the way in, so the operator's
    configured spelling and the stored one routinely differ. Every comparison goes
    through here, on BOTH sides, so a keep tag can never fail to match the label it
    names -- the ``plex.normalize_label`` of the *arr side.
    """
    return fold(tag)


def _name_key(name: str) -> str:
    """The comparison form for a Plex library or collection title.

    Same rule as :func:`_tag_key` and ``plex.normalize_label`` (rule 88): both sides
    case-folded, or a library the operator spells "movies" never matches the configured
    "Movies" and their keep collection reads as a missing library.
    """
    return fold(name)


@dataclass(frozen=True, slots=True)
class ArrTagRule:
    """One or more *arr tags, combined -- the configurable "keep list".

    A title matches when it carries ANY of the tags (the usual case) or ALL of them, per
    ``match``. The tag list and the library are each read ONCE for the whole rule -- the
    library is the expensive call (every movie or series in the instance), and an earlier
    version re-downloaded it once per configured tag, per scan. Protect-only, like every
    list source -- the worst a mis-configured rule can do is fail to keep something.

    ``instance_id`` is part of the slug for a reason that is easy to miss: the slug is
    the stored list's primary key, and each sync atomically REPLACES that slug's
    membership. With two same-service instances and a service-only slug, each instance's
    sync erased the other's keep-tagged titles -- whichever synced last won, and titles
    tagged only on the losing instance silently lost their whitelist protection. A
    per-instance slug makes each instance its own list, so both protect.
    """

    client: SonarrClient | RadarrClient
    tags: tuple[str, ...]
    match: Literal["any", "all"] = "any"
    instance_id: int | None = None
    instance_name: str | None = None
    #: The definition this was built from, when the operator defined it on Settings -> Lists.
    #: ``None`` for a legacy policy keep-tag row, which keeps its slug spelled as it always was.
    list_id: int | None = None
    #: What the operator called it. ``None`` falls back to describing the rule.
    list_name: str | None = None
    #: How many titles carried each configured tag, filled by :meth:`fetch` and stored by
    #: ``sync`` beside the row (see :attr:`sync_stats`). A mutable container on a frozen
    #: dataclass, deliberately: the counts are a *result* of the fetch, not configuration,
    #: and a per-tag count is independent of the ANY/ALL mode -- it answers "which tags are
    #: doing the protecting here", which the Lists screen shows per server.
    tag_counts: dict[str, int] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        instance = f"-{self.instance_id}" if self.instance_id is not None else ""
        return (
            f"{self.client.service}{instance}{KEEP_TAG_INFIX}{self.match}"
            f"{list_suffix(self.list_id)}"
        )

    @property
    def display_name(self) -> str:
        joiner = " or " if self.match == "any" else " and "
        where = f" ({self.instance_name})" if self.instance_name else ""
        if self.list_name:
            # The operator's own name leads, with the server after it: several of these rows
            # are one list to them (one per *arr instance), and the name is what says so.
            return f"{self.list_name}{where}"
        return f"{self.client.service.title()}{where} tag: {joiner.join(self.tags)}"

    async def fetch(self) -> list[ListItem]:
        if not self.tags:
            return []

        # First match wins when two tag labels collide after normalizing ('Keep' and
        # 'keep' can both exist), and a malformed tag row is skipped rather than failing
        # the whole keep-list over a row that was never the owner's keep tag.
        by_label: dict[str, int] = {}
        for row in await self.client.tags():
            tag_id = row.get("id")
            if isinstance(tag_id, int):
                by_label.setdefault(_tag_key(str(row.get("label", ""))), tag_id)

        wanted: set[int] = set()
        missing: list[str] = []
        for tag in self.tags:
            # BOTH sides normalized, or the lookup silently misses. Sonarr and Radarr
            # lower-case every label at the source, so an operator who configures the
            # natural capitalization of their own tag ("Reaper-Keep") would look up a
            # spelling the map cannot hold: the tag reads as MISSING, and on a first sync
            # that stores an empty membership and reports success -- a keep list that
            # protects nothing, forever, while the settings screen shows it as healthy.
            found = by_label.get(_tag_key(tag))
            if found is None:
                log.info("lists.tag_absent", tag=tag, service=self.client.service)
                missing.append(tag)
            else:
                wanted.add(found)
        # A missing tag is a missing CONTAINER, not an empty one: nothing can carry a
        # tag that does not exist, so returning [] here would be indistinguishable from
        # "the owner un-tagged everything" and sync() would wipe the stored membership.
        # EVERY configured tag that will not resolve is a failure (rule 27), because the
        # absence is indistinguishable from a tag the operator RENAMED upstream -- and a
        # rename withdraws the protection from every title still carrying it.
        #
        # Two failures, because the two land in different places in ``sync``:
        #
        # * Nothing resolved at all, or ANY gone under ALL (one absent tag already rules
        #   every title out): ContainerMissingError, which ``sync`` reads as genuinely
        #   empty when nothing is stored yet. That is the first-run case -- the default
        #   'reaper-keep' tag usually does not exist in a fresh *arr, and a brand-new
        #   install must not be un-scannable because of it.
        # * Some resolved and some did not, under ANY: a hard failure, whatever is
        #   stored. Reading THIS as an empty first sync would store the surviving tags'
        #   members as [] and report the keep-list healthy, so the tags that DO resolve
        #   would protect nothing (rule 90's shape, one level up). The sync fails, the
        #   stored membership survives untouched, and the scan degrades.
        if missing:
            names = ", ".join(repr(t) for t in missing)
            if not wanted or self.match == "all":
                if not wanted:
                    # No tag exists, so no title carries one: every count is a true zero,
                    # and recording them is what lets the genuinely-empty first sync say
                    # "0" instead of leaving the screen blank. Under ALL with a tag still
                    # resolved this is skipped -- the resolved tags' counts were never
                    # taken, and an untaken count is unknown, not zero (rule 96).
                    self.tag_counts.clear()
                    self.tag_counts.update(dict.fromkeys(self.tags, 0))
                raise ContainerMissingError(
                    f"keep tag {names} does not exist in {self.client.service}"
                )
            raise IntegrationError(
                self.client.service, "error.integration.keep_tag_missing", names=names
            )

        # id -> the operator's spelling, for the per-tag counts. Built from `wanted`, so a
        # tag that did not resolve counts nothing rather than raising a KeyError here.
        spelling = {by_label[_tag_key(t)]: t for t in self.tags if _tag_key(t) in by_label}
        counts: dict[str, int] = dict.fromkeys(self.tags, 0)

        def keeps(media: dict[str, Any]) -> bool:
            carried = set(media.get("tags") or [])
            for tag_id in wanted & carried:
                counts[spelling[tag_id]] += 1
            if self.match == "all":
                return wanted <= carried
            return not wanted.isdisjoint(carried)

        # Through identity.ExternalIds.of, the same door the scan's own reads of these two
        # payloads use: a raw `tt0000000` stored here is a row every other sentinel-bearing
        # item matches, and a keep tag would report protecting titles it does not (#709).
        self.tag_counts.clear()
        out: list[ListItem] = []
        if isinstance(self.client, RadarrClient):
            for m in await self.client.movies():
                if not keeps(m):
                    continue
                ids = identity.ExternalIds.of(imdb=m.get("imdbId"), tmdb=m.get("tmdbId"))
                out.append(
                    ListItem(
                        media_type="movie",
                        imdb_id=ids.imdb,
                        tmdb_id=ids.tmdb,
                        title=str(m.get("title") or ""),
                    )
                )
        else:
            for s in await self.client.series():
                if not keeps(s):
                    continue
                ids = identity.ExternalIds.of(imdb=s.get("imdbId"), tvdb=s.get("tvdbId"))
                out.append(
                    ListItem(
                        media_type="tv",
                        imdb_id=ids.imdb,
                        tvdb_id=ids.tvdb,
                        title=str(s.get("title") or ""),
                    )
                )
        self.tag_counts.update(counts)
        return out

    @property
    def server_label(self) -> str:
        """The server, named for the operator: the service with the instance after it
        ("Radarr (4k)"), so two same-named instances of different services stay apart in
        the per-server counts. The same shape :attr:`display_name` puts after a name."""
        where = f" ({self.instance_name})" if self.instance_name else ""
        return f"{self.client.service.title()}{where}"

    @property
    def sync_stats(self) -> dict[str, Any] | None:
        """What ``sync`` stores beside this row: per-tag counts, and which server they are
        from. The counts are ``None`` -- unknown, never zero -- unless a counting pass
        actually ran (:meth:`fetch` fills them, on success and on the no-tag-exists first
        sync alike)."""
        return {
            "tags": dict(self.tag_counts) if self.tag_counts else None,
            "server": self.server_label,
        }


#: What Plex calls a thing -> the kind a protection row is filed under. A collection is
#: typed by its library, so these two are what a keep collection holds. Anything else keeps
#: Plex's own spelling, which :attr:`ListItem.is_protectable` refuses: the mapping this
#: replaced read ``"tv" if item.type == "show" else "movie"``, filing every other Plex
#: object -- an episode, a music artist -- as a MOVIE and carrying whatever ids it had into
#: the movie id space (rule 52). Such a row protects nothing, or protects an unrelated film
#: that happens to share the number.
_PLEX_MEDIA_TYPES = {"movie": "movie", "show": "tv"}


@dataclass(frozen=True, slots=True)
class PlexCollection:
    """A collection you curate in the Plex app itself -- "Never Reap" by convention.

    The best of the four providers, and the one with no configuration at all. You add
    a film to the collection from your phone, in the app you already use every day.
    There is no new screen to learn and no Reaper concept to understand.

    Plex is synchronous (``plexapi`` wraps ``requests``), so it is called off the event
    loop.
    """

    server: object  # plexapi.server.PlexServer, kept loose to avoid a hard import here
    section_name: str
    collection_name: str = "Never Reap"
    #: The definition this was built from. Two collections of the same name in two libraries
    #: are two lists, and without the id in the slug each sync would atomically replace the
    #: other's membership -- the failure ``ArrTagRule`` documents for two same-service *arr
    #: instances, reached by a different route.
    list_id: int | None = None
    list_name: str | None = None

    @property
    def slug(self) -> str:
        readable = self.collection_name.lower().replace(" ", "-")
        return f"{PLEX_COLLECTION_PREFIX}{readable}{list_suffix(self.list_id)}"

    @property
    def display_name(self) -> str:
        if self.list_name:
            return self.list_name
        return f'Plex collection: "{self.collection_name}"'

    async def fetch(self) -> list[ListItem]:
        import asyncio

        return await asyncio.to_thread(self._fetch_sync)

    def _find_collection(self) -> Any:
        """The keep collection, looked for in **every** library carrying this title.

        ``library.section(title)`` returns whichever of two same-titled libraries plexapi
        saw first (rule 6/57), so on a server with an HD and a 4K library both called
        "Movies" the collection could be read from a library that does not hold it. That
        reads as "the container is gone", which over a populated stored list degrades the
        scan and, on a first sync, stores an empty keep-list. Asking each matching library
        in turn removes the ambiguity without asking the operator for a key.
        """
        from plexapi.exceptions import NotFound

        library = self.server.library  # type: ignore[attr-defined]
        # Case-folded on BOTH sides (rule 88). ``library.section(title)`` -- the call this
        # replaced -- matched case-insensitively, so an exact-match filter here silently
        # stopped finding the keep collection of an operator whose library is spelled
        # "movies": the sync then failed the whole HARD keep-list and every scan with it.
        wanted = _name_key(self.section_name)
        sections = [s for s in library.sections() if _name_key(str(s.title)) == wanted]
        if not sections:
            # Not a missing container but a missing LIBRARY: a mistyped name, or one the
            # operator removed. A hard failure, recorded against the slug, exactly as the
            # raw plexapi NotFound this replaces was.
            raise IntegrationError(
                "plex", "error.integration.library_not_found", name=self.section_name
            )
        for section in sections:
            try:
                return section.collection(self.collection_name)
            except NotFound:
                continue
        # The container is not there to ask: deleted, renamed, or simply not yet
        # created. Which of those it is depends on what is already stored, and
        # sync() decides -- raising here (rather than returning []) is what lets a
        # stored membership survive a deleted "Never Reap" collection.
        log.info("lists.plex_collection_absent", collection=self.collection_name)
        raise ContainerMissingError(
            f"Plex collection {self.collection_name!r} does not exist in section "
            f"{self.section_name!r}"
        )

    def _fetch_sync(self) -> list[ListItem]:
        collection = self._find_collection()

        items: list[ListItem] = []
        for item in collection.items():
            # The new Plex agents expose external ids as Guid children; the legacy agents
            # put a single id in `guid`. identity.parse_guids handles both (and the
            # ``?lang=`` suffix, and sentinels), so a legacy-agent library is no longer
            # silently unprotected -- the same one parser the scan's matcher uses.
            guid_ids = [str(getattr(guid, "id", "")) for guid in getattr(item, "guids", None) or []]
            legacy = getattr(item, "guid", None)
            ids = identity.parse_guids(guid_ids, str(legacy) if legacy else None)
            plex_type = str(getattr(item, "type", "") or "")
            # Plex's own key for the object, kept whether or not the guids parsed. It is
            # what protects an entry no agent ever matched, and the ids above stay the
            # join for everything else: a key means nothing on another server, and an
            # item re-added to the library comes back under a new one.
            raw_key = str(getattr(item, "ratingKey", "") or "")
            items.append(
                ListItem(
                    media_type=_PLEX_MEDIA_TYPES.get(plex_type, plex_type),
                    imdb_id=ids.imdb,
                    tmdb_id=ids.tmdb,
                    tvdb_id=ids.tvdb,
                    plex_rating_key=int(raw_key) if raw_key.isdigit() else None,
                    title=str(item.title),
                )
            )
        return items


@dataclass(frozen=True, slots=True)
class PlexWatchlist:
    """The watchlist of the Plex account Reaper is signed in with.

    The stored Plex token is the account credential itself (``services.plex_link``
    verifies the resource token matches the account's on link), so ``myPlexAccount()``
    needs no second sign-in. Watchlist entries live on plex.tv rather than the PMS, so
    unlike a collection they carry no usable server rating key; the external ids are the
    whole join, and an entry without any is held by ``_rows_to_store`` like any other.

    An **empty watchlist is genuinely empty** -- the operator cleared it from the Plex
    app -- so it empties the stored membership. A watchlist has no missing-container
    state: any failure to read it raises and leaves the stored copy protecting.

    plexapi is synchronous, so the read runs off the event loop.
    """

    server: object  # plexapi.server.PlexServer, kept loose like PlexCollection's
    list_id: int | None = None
    list_name: str | None = None

    @property
    def slug(self) -> str:
        return f"{PLEX_WATCHLIST_PREFIX}account{list_suffix(self.list_id)}"

    @property
    def display_name(self) -> str:
        return self.list_name or "Plex watchlist"

    async def fetch(self) -> list[ListItem]:
        import asyncio

        return await asyncio.to_thread(self._fetch_sync)

    def _fetch_sync(self) -> list[ListItem]:
        account = self.server.myPlexAccount()  # type: ignore[attr-defined]
        items: list[ListItem] = []
        for item in account.watchlist():
            # Discover metadata carries the external ids as Guid children, same shape as
            # a PMS item; the legacy `guid` on these is a plex:// uri parse_guids ignores.
            guid_ids = [str(getattr(g, "id", "")) for g in getattr(item, "guids", None) or []]
            legacy = getattr(item, "guid", None)
            ids = identity.parse_guids(guid_ids, str(legacy) if legacy else None)
            plex_type = str(getattr(item, "type", "") or "")
            items.append(
                ListItem(
                    media_type=_PLEX_MEDIA_TYPES.get(plex_type, plex_type),
                    imdb_id=ids.imdb,
                    tmdb_id=ids.tmdb,
                    tvdb_id=ids.tvdb,
                    title=str(getattr(item, "title", "") or ""),
                )
            )
        return items


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS protection_list (
    slug          TEXT PRIMARY KEY,
    display_name  TEXT    NOT NULL,
    rule_name     TEXT,
    mode          TEXT    NOT NULL DEFAULT 'hard',
    kind          TEXT    NOT NULL DEFAULT 'curated',
    weight        INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    item_count    INTEGER NOT NULL DEFAULT 0,
    last_synced_at INTEGER,
    last_error    TEXT
);
CREATE TABLE IF NOT EXISTS protection_list_item (
    slug       TEXT    NOT NULL,
    media_type TEXT    NOT NULL,
    imdb_id    TEXT,
    tmdb_id    INTEGER,
    tvdb_id    INTEGER,
    plex_rating_key INTEGER,
    title      TEXT,
    rank       INTEGER,
    PRIMARY KEY (slug, media_type, imdb_id, tmdb_id, tvdb_id)
);
"""

#: Created after :data:`SCHEMA`, and after the widening below, because an index over a
#: column a stored database has not been given yet cannot be created.
INDEXES = """
CREATE INDEX IF NOT EXISTS ix_pli_imdb ON protection_list_item (imdb_id);
CREATE INDEX IF NOT EXISTS ix_pli_tmdb ON protection_list_item (tmdb_id);
CREATE INDEX IF NOT EXISTS ix_pli_tvdb ON protection_list_item (tvdb_id);
CREATE INDEX IF NOT EXISTS ix_pli_plex ON protection_list_item (plex_rating_key);
"""

#: Columns added to ``protection_list_item`` after it first shipped. ``CREATE TABLE IF NOT
#: EXISTS`` leaves a table that already exists exactly as it is, so a stored ``cache.db``
#: never sees a new column from :data:`SCHEMA` alone -- and this table is not Alembic's
#: (it lives in ``cache.db``, which alembic is never pointed at), so nothing else would add
#: it either. Additive only:
#: every one is nullable, and the next sync fills it in.
_ADDED_COLUMNS = {"plex_rating_key": "INTEGER"}

#: The same widening for ``protection_list``. ``stats_json`` holds what a provider knows
#: about its own sync beyond the count -- per-tag totals and the server they came from --
#: written on success only, so like the membership it is always from the last good check.
#: ``rule_name`` is what a keep rule matches (:func:`rule_name`); a stored row that predates
#: it reads as its display name, which is what it was matched by before the column existed.
_ADDED_LIST_COLUMNS = {"stats_json": "TEXT", "rule_name": "TEXT"}

#: Serializes the widening in :func:`ensure_schema`, which is a check-then-write that SQLite
#: does not serialize for us. Whole-process, so it holds across the seven callers.
_widen_lock = per_loop_lock()


@dataclass(frozen=True, slots=True)
class Membership:
    """Why an item is protected, and by which list."""

    slug: str
    display_name: str
    mode: ListMode
    kind: ListKind
    rank: int | None
    rule_name: str = ""
    """What a keep rule naming this list spells (:func:`rule_name`), which is not always
    what the operator is shown: a tag list's display name carries the server. Defaulted so
    the fact builders' own fixtures need not restate it; every production read comes from
    the stored column, and a row written before it falls back to the display name."""

    def matched_by(self) -> str:
        """The name the ``on_list`` field compares against. Falls back to the display name
        for a row stored before ``rule_name`` existed, which is the spelling it was matched
        by then, so a widened database keeps whatever it was already protecting."""
        return self.rule_name or self.display_name

    @property
    def is_whitelist(self) -> bool:
        return self.kind is ListKind.WHITELIST

    def describe(self) -> str:
        position = f" (#{self.rank})" if self.rank else ""
        return f"{self.display_name}{position}"


def on_list_fact(memberships: Sequence[Membership]) -> Observation[str]:
    """The ``on_list`` field's input: every list holding an item, by the name its keep rule
    spells, deduplicated keeping order and comma-joined the way a multi-valued fact is read.

    One derivation for both fact builders (rule 35: same fact, same shape, every builder).
    They each carried their own copy, so the movie path and the season path could disagree
    about what a keep rule sees -- and did, because each joined DISPLAY names, which for a
    tag list carry the server and so matched no rule at all (#507).

    ``Absent``, never ``Unknown``: an item on no list is one we looked up and found on
    nothing, which is a checked miss. A membership we could not READ never reaches here --
    a failed list sync degrades the snapshot instead (rule 2), and the stored copy from the
    last good check is what these rows are.
    """
    names = tuple(dict.fromkeys(m.matched_by() for m in memberships))
    return Known(value=", ".join(names), source="lists") if names else Absent(source="lists")


async def ensure_schema(engine: AsyncEngine) -> None:
    """Create the two tables, widen a stored one, then index it -- in that order.

    The widening is what lets a ``cache.db`` written by an older version keep its
    membership. Dropping and recreating the table instead would empty every keep list
    until the next sync refilled it, and a scan in that window would reap titles the
    operator's list protects.
    """
    async with engine.begin() as conn:
        for statement in SCHEMA.strip().split(";"):
            if statement.strip():
                await conn.execute(text(statement))
    # Each shape is re-read INSIDE the lock and its ALTERs decided from THAT, never from a
    # read taken before it (rule 58). Nothing at the SQLite level serializes these: pysqlite
    # autocommits DDL, so `engine.begin()` above and below opens no transaction around it,
    # and there is no ADD COLUMN IF NOT EXISTS. Two callers reading the pre-widen shape both
    # ALTER and the second raises `duplicate column name`, which aborts a scan -- and two
    # callers is ordinary, since the Lists screen, a running scan and the nightly refresh all
    # reach here on three different job ids.
    async with _widen_lock(), engine.begin() as conn:
        for table, added in (
            ("protection_list_item", _ADDED_COLUMNS),
            ("protection_list", _ADDED_LIST_COLUMNS),
        ):
            stored = {
                str(row.name)
                for row in (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
            }
            for column, kind in added.items():
                if column not in stored:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {kind}"))
        for statement in INDEXES.strip().split(";"):
            if statement.strip():
                await conn.execute(text(statement))


async def _record_sync_error(
    engine: AsyncEngine,
    provider: ListProvider,
    *,
    mode: ListMode,
    kind: ListKind,
    weight: int,
    error: str,
) -> None:
    """Record a failed refresh on the list row, leaving its membership untouched."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO protection_list "
                "(slug, display_name, rule_name, mode, kind, weight, last_error) "
                "VALUES (:slug, :name, :rule, :mode, :kind, :weight, :err) "
                "ON CONFLICT(slug) DO UPDATE SET last_error = :err"
            ),
            {
                "slug": provider.slug,
                "name": provider.display_name,
                # Written on the INSERT, so a list whose very first check failed still
                # carries the name its keep rule matches. The conflict arm leaves it as a
                # successful sync wrote it, like ``display_name`` beside it.
                "rule": rule_name(provider),
                "mode": mode.value,
                "kind": kind.value,
                "weight": weight,
                "err": error,
            },
        )


def _row_key(item: ListItem) -> tuple[object, ...]:
    """What makes two entries the same stored row.

    The external ids where the entry has any, because those are what the table's primary
    key is built from and what every lookup joins on: one film listed twice under one
    TMDb id is one row, whichever Plex objects it came from. The Plex key alone where it
    has none, because then that key is the only thing telling two entries apart, and
    folding them together would drop one of the operator's titles.

    Only ever asked about an entry that is :attr:`ListItem.is_protectable`, or a stored
    row that was one when it was written, so "no ids and no Plex key" -- which would key
    every such entry alike -- cannot reach here.
    """
    if item.has_any_id:
        return (item.media_type, item.imdb_id, item.tmdb_id, item.tvdb_id)
    return (item.media_type, item.plex_rating_key)


def _row_values(slug: str, item: ListItem) -> dict[str, object]:
    return {
        "slug": slug,
        "media_type": item.media_type,
        "imdb_id": item.imdb_id,
        "tmdb_id": item.tmdb_id,
        "tvdb_id": item.tvdb_id,
        "plex_rating_key": item.plex_rating_key,
        "title": item.title,
        "rank": item.rank,
    }


async def _stored_by_title(
    conn: AsyncConnection, slug: str
) -> dict[tuple[str, str], list[ListItem]]:
    """This list's stored rows, grouped by the kind and title they were stored under.

    Both sides of the title are case-folded (rule 88), or a title Plex re-cased on a
    re-match matches nothing and the protection lapses on exactly the sync that renamed
    it. A row with no title is left out: an empty title would match every other untitled
    row, which is worse than matching none.
    """
    rows = (
        await conn.execute(
            text(
                "SELECT media_type, imdb_id, tmdb_id, tvdb_id, plex_rating_key, title, rank "
                "FROM protection_list_item WHERE slug = :slug"
            ),
            {"slug": slug},
        )
    ).all()
    out: dict[tuple[str, str], list[ListItem]] = {}
    for row in rows:
        title = str(row.title or "")
        if not title.strip():
            continue
        out.setdefault((str(row.media_type), _name_key(title)), []).append(
            ListItem(
                media_type=str(row.media_type),
                imdb_id=str(row.imdb_id) if row.imdb_id else None,
                tmdb_id=int(row.tmdb_id) if row.tmdb_id is not None else None,
                tvdb_id=int(row.tvdb_id) if row.tvdb_id is not None else None,
                plex_rating_key=(
                    int(row.plex_rating_key) if row.plex_rating_key is not None else None
                ),
                title=title,
                rank=int(row.rank) if row.rank is not None else None,
            )
        )
    return out


def _keep_keys(item: ListItem, stored: Sequence[ListItem]) -> ListItem:
    """``item``, given back the external ids the row it replaces was stored under.

    A fetch that comes back having lost the ids of an entry is the failure this exists
    for: the entry is still on the list, so the swap must not file it under fewer keys
    than before. Losing them is the ordinary shape of a Plex agent change, and the ids
    matter beyond Plex -- the movie lane looks a keep list up by RADARR's imdb and tmdb
    ids, which a Plex-side identity failure does not touch, so a row reduced to a Plex
    key alone stops protecting the moment the scan cannot bind the item to Plex.

    Bounded on purpose. Only an entry that came back with NO ids inherits any, because a
    source that identified the entry at all is the better authority on which title it is:
    an operator who corrects a mis-match in Plex would otherwise have the old title's ids
    welded back on at every sync. And only from a single stored row, because two rows
    under one title cannot say which ids are this entry's (rule 6).
    """
    if item.has_any_id or len(stored) != 1:
        return item
    was = stored[0]
    if not was.has_any_id:
        return item
    return replace(item, imdb_id=was.imdb_id, tmdb_id=was.tmdb_id, tvdb_id=was.tvdb_id)


async def _rows_to_store(
    conn: AsyncConnection, slug: str, fetched: Sequence[ListItem]
) -> tuple[list[ListItem], int, int]:
    """Every row this sync should leave behind, with what it recovered and what it held.

    ``sync`` refuses the swap outright when a populated container loses *every* entry.
    When it loses only some of them, the ones that survive look like a complete fetch,
    and the swap would drop the rest: the operator's keep tag is still on the title, the
    sync reports a healthy count, and nothing protects the title any more. Same failure
    as the all-or-nothing case, at a smaller scale (rule 27), and the one the read side
    cannot recover from -- a lookup finds rows by key, so a row deleted for want of one
    is simply gone.

    Two repairs, both keyed on the entry still BEING on the list, so a title the operator
    genuinely removed is still removed and a title that was never stored gains nothing:

    * an entry still identifiable, but no longer by id, is stored with the ids its row
      had (:func:`_keep_keys`);
    * an entry no longer identifiable at all keeps its stored row untouched.

    Held rather than refused, because refusing is not free: a failed ``sync`` is what
    ``snapshot.protection_sync_degradations`` reads, and past ``WHITELIST_STALE_AFTER``
    it makes every scan un-executable. One home video in a "Never Reap" collection, which
    no agent will ever give an id, would eventually stop the operator reaping anything at
    all -- and it never protected a title in the first place, so refusing would trade a
    real protection for an imaginary one.

    A title that changed spelling in the same sync it lost its keys is recoverable by
    neither, and reads as a removal.
    """
    stored = await _stored_by_title(conn, slug)
    out: list[ListItem] = []
    recovered = held = 0
    for item in fetched:
        was = stored.get((item.media_type, _name_key(item.title)), []) if item.title.strip() else []
        if not item.is_protectable:
            out.extend(was)
            held += len(was)
            continue
        kept = _keep_keys(item, was)
        recovered += kept is not item
        out.append(kept)
    return out, recovered, held


async def sync(
    engine: AsyncEngine,
    provider: ListProvider,
    *,
    mode: ListMode = ListMode.HARD,
    kind: ListKind = ListKind.CURATED,
    weight: int = 0,
) -> int:
    """Refresh one list.

    The swap is atomic per list: a failed fetch leaves the previous membership intact.
    A protection that silently empties itself is worse than one that is out of date --
    it would stop protecting without saying so. A missing CONTAINER (a renamed keep
    tag, a deleted "Never Reap" collection) counts as a failure whenever members are
    stored, for exactly that reason; it counts as genuinely empty only when there was
    never anything to protect.

    A **populated** container whose every entry became unusable is the same failure
    wearing a different coat, and it is treated the same way (rule 27). Only a container
    that genuinely came back empty may empty the list.

    Short of that, the swap runs over what :func:`_rows_to_store` makes of the fetch,
    which never files a title the container still lists under fewer keys than the row it
    replaces. Whatever is left unidentifiable is on the list and protected by nothing,
    and is logged as such.
    """
    await ensure_schema(engine)

    fetched: list[ListItem] = []
    unusable: list[ListItem] = []
    try:
        fetched = list(await provider.fetch())
        items = [i for i in fetched if i.is_protectable]
        unusable = [i for i in fetched if not i.is_protectable]
        if fetched and not items:
            raise ContainerMissingError(
                f"None of the {len(fetched)} titles on {provider.display_name} could be "
                "identified, so the list was not read as empty."
            )
    except ContainerMissingError as exc:
        async with engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM protection_list_item WHERE slug = :slug"),
                    {"slug": provider.slug},
                )
            ).scalar_one()
        if stored:
            # The container vanished from under a populated list. Fail, so the atomic
            # swap below never runs and the previous membership keeps protecting;
            # succeeding-with-[] would unprotect every stored title on this very scan.
            await _record_sync_error(
                engine, provider, mode=mode, kind=kind, weight=weight, error=str(exc)
            )
            log.warning("lists.container_missing", slug=provider.slug, error=str(exc))
            raise
        # Nothing stored and no container to read: the owner has not created it yet.
        # A genuinely empty first sync, not a failure.
        fetched = []
    except Exception as exc:
        await _record_sync_error(
            engine, provider, mode=mode, kind=kind, weight=weight, error=str(exc)
        )
        log.warning("lists.sync_failed", slug=provider.slug, error=str(exc))
        raise

    async with engine.begin() as conn:
        # Read the stored rows inside the write transaction (rule 58): what is carried
        # over is decided from the very rows this DELETE is about to remove, not from an
        # earlier snapshot of them.
        keeping, recovered, held = await _rows_to_store(conn, provider.slug, fetched)
        await conn.execute(
            text("DELETE FROM protection_list_item WHERE slug = :slug"),
            {"slug": provider.slug},
        )
        # Deduplicated here, because SQLite will not do it: it counts NULLs in a primary
        # key as distinct, and every one of these rows has a NULL somewhere in that key,
        # so INSERT OR REPLACE leaves an entry the container lists twice sitting in the
        # table twice and every lookup reports the same list protecting it twice.
        rows = {_row_key(i): _row_values(provider.slug, i) for i in keeping}
        if rows:
            await conn.execute(
                text(
                    "INSERT OR REPLACE INTO protection_list_item "
                    "(slug, media_type, imdb_id, tmdb_id, tvdb_id, plex_rating_key, title, rank) "
                    "VALUES (:slug, :media_type, :imdb_id, :tmdb_id, :tvdb_id, :plex_rating_key, "
                    ":title, :rank)"
                ),
                list(rows.values()),
            )
        stats = getattr(provider, "sync_stats", None)
        await conn.execute(
            text(
                "INSERT INTO protection_list "
                "(slug, display_name, rule_name, mode, kind, weight, enabled, item_count, "
                " last_synced_at, last_error, stats_json) "
                "VALUES (:slug, :name, :rule, :mode, :kind, :weight, 1, :count, :now, NULL, "
                "        :stats) "
                # ``enabled = 1`` on the conflict too, not just on the insert: a slug
                # ``retire_absent`` switched off is a live list again the moment this
                # configuration produces it, and a keep-list that syncs while disabled
                # protects nothing (``load_membership_index`` joins ``WHERE enabled = 1``).
                "ON CONFLICT(slug) DO UPDATE SET "
                "  display_name = :name, rule_name = :rule, mode = :mode, kind = :kind, "
                "  item_count = :count, enabled = 1, last_synced_at = :now, "
                "  last_error = NULL, stats_json = :stats"
            ),
            {
                "slug": provider.slug,
                "name": provider.display_name,
                "rule": rule_name(provider),
                "mode": mode.value,
                "kind": kind.value,
                "weight": weight,
                "count": len(rows),
                "now": int(utcnow().timestamp()),
                "stats": json.dumps(stats) if stats is not None else None,
            },
        )

    if unusable:
        # Named, because a count alone leaves the operator nothing to go and look at, and
        # the titles are the operator's own (``scan.plex_unmatched`` logs them the same
        # way). Capped: a whole list going unusable raises above instead of landing here,
        # but a large one could still fill a line.
        log.warning(
            "lists.unidentified",
            slug=provider.slug,
            unidentified=len(unusable),
            held=held,
            titles=sorted({i.title for i in unusable if i.title.strip()})[:20],
        )
    log.info("lists.synced", slug=provider.slug, items=len(rows), recovered=recovered, held=held)
    return len(rows)


#: The infix and prefix the families below are matched on, as the SQL patterns' one source.
#: The patterns are built from these rather than the other way round, so a family that is
#: respelled moves the retire sweep and every reader of it together (rule 144).
KEEP_TAG_INFIX = "-keeptags-"
PLEX_COLLECTION_PREFIX = "plex-collection-"
PLEX_WATCHLIST_PREFIX = "plex-watchlist-"
IMDB_PREFIX = "imdb-"

#: The slug families Reaper *derives* from configuration rather than being told, and so is
#: responsible for retiring. Each changes spelling when the configuration behind it changes
#: -- ``ArrTagRule.slug`` carries the any/all match and the instance id, ``PlexCollection.slug``
#: carries the collection name, and every list the operator defined carries its definition's id
#: (see :func:`list_suffix`) -- so the old row would otherwise sit there enabled, still
#: protecting from a definition the operator has already replaced or removed. The IMDb family
#: pattern also matches the retired ``imdb-top-250`` spelling an upgrade leaves behind, which
#: is what re-homes it.
KEEP_TAG_SLUGS = f"%{KEEP_TAG_INFIX}%"
PLEX_COLLECTION_SLUGS = f"{PLEX_COLLECTION_PREFIX}%"
PLEX_WATCHLIST_SLUGS = f"{PLEX_WATCHLIST_PREFIX}%"
IMDB_SLUGS = f"{IMDB_PREFIX}%"

#: How a slug says which definition it was synced for. Appended, so the readable part of a
#: slug stays at the front and the existing family patterns above go on matching unchanged.
_LIST_SUFFIX = "-list"


def list_slugs(list_id: int) -> str:
    """The ``LIKE`` pattern matching every stored slug synced for ONE definition.

    A narrower ``family`` for :func:`retire_absent`, for the pass that checks a single row of
    the Lists screen: that pass produces one definition's slugs, so one definition is the most
    it may sweep. Anchored at the end with no trailing wildcard, or ``-list1`` would also
    match ``-list10`` and a sweep of one list would retire another's rows.
    """
    return f"%{_LIST_SUFFIX}{list_id}"


def list_suffix(list_id: int | None) -> str:
    """The ``-list<id>`` tail every list the operator DEFINED carries.

    The definition's id, not its name: the name is the operator's to change, and a slug that
    moved when they renamed a list would write a second row and leave the first one enabled
    and still protecting (the failure :func:`retire_absent` exists to prevent).

    Empty for the keep tags, which are configured on Policy and have no definition row. That
    is what keeps their stored slugs spelled exactly as they were before the registry existed,
    so an upgrade does not orphan the membership protecting an operator's tagged titles.
    """
    return f"{_LIST_SUFFIX}{list_id}" if list_id is not None else ""


def list_id_of(slug: str) -> int | None:
    """Which definition a stored slug was synced for, or ``None`` for the keep tags.

    The inverse of :func:`list_suffix`, and the join the Lists screen renders one row per
    definition from. It lives here, beside the spellings, because a surface that parsed a slug
    for itself would be a second copy of this to keep in step (:class:`ListSource` says the
    same about grouping).
    """
    head, sep, tail = slug.rpartition(_LIST_SUFFIX)
    return int(tail) if sep and head and tail.isdigit() else None


class ListSource(enum.StrEnum):
    """Which kind of thing a list comes from: the registry's ``source`` column, and the
    grouping key for stored membership rows (derived from the slug there, since no cache
    column records the provider).

    A surface that groups MUST NOT parse the slug itself. One tag list exists per *arr
    instance, so an operator with two Radarrs and two Sonarrs has four stored rows for the
    single protection "titles I tagged reaper-keep". Grouping is how that stays readable,
    and the grouping key belongs wherever the slugs are spelled, not in the component.
    """

    ARR_TAG = "arr_tag"
    """One *arr instance's tags. Several stored rows are one protection to the operator."""

    PLEX_COLLECTION = "plex_collection"
    """A collection curated in the Plex app."""

    PLEX_WATCHLIST = "plex_watchlist"
    """The watchlist of the Plex account Reaper is signed in with."""

    IMDB = "imdb"
    """An IMDb chart or public list, via Radarr's mirror."""


async def retire_absent(engine: AsyncEngine, *, family: str, current: Collection[str]) -> list[str]:
    """Disable every enabled list in ``family`` that this configuration no longer produces.

    A stored list outlives the setting that created it. Flip "Keep tags: match ANY" to
    "match ALL" and the slug changes, so the sync writes a NEW row while the old one stays
    enabled: the tightening the operator saved never takes effect, because everything that
    matched ANY tag is still whitelisted by a list nothing updates. Clearing the keep tags
    entirely, renaming the "Never Reap" collection, and deleting an *arr instance all land
    the same way. So each run declares the slugs it is responsible for and this retires the
    rest of that family.

    Disabled, never deleted: the membership is left in place so the row still explains
    itself on the settings screen, and ``sync`` re-enables a slug the moment it comes back
    (flip ALL back to ANY and the old list resumes protecting). Returns what it retired.

    **Only call this for a family whose inputs were actually readable this run.** The
    caller decides: a Plex that could not be reached produces no collection provider, and
    retiring on that would unprotect every title on the operator's "Never Reap" collection
    because of a network blip -- the fail-open this codebase exists to avoid.
    """
    await ensure_schema(engine)
    keep = set(current)
    async with engine.begin() as conn:
        # Read and write inside ONE transaction (rule 58): the set being retired is
        # decided from the rows this statement will update, not from an earlier snapshot.
        rows = (
            await conn.execute(
                text("SELECT slug FROM protection_list WHERE enabled = 1 AND slug LIKE :family"),
                {"family": family},
            )
        ).scalars()
        stale = sorted({str(slug) for slug in rows} - keep)
        for slug in stale:
            await conn.execute(
                text("UPDATE protection_list SET enabled = 0 WHERE slug = :slug"),
                {"slug": slug},
            )
    for slug in stale:
        log.info("lists.retired", slug=slug, family=family)
    return stale


async def sync_rule_names(engine: AsyncEngine, definitions: Sequence[ListDefinition]) -> int:
    """Write each definition's current name onto every row synced for it. Returns how many
    rows moved.

    A stored row learns the name its keep rule matches from the sync that wrote it
    (:func:`rule_name`), so between a rename and the next successful check the row still
    carries the old spelling while ``list_rules.rename_list`` has already re-spelled the
    rules. That gap is a live protection switched off, silently, by an edit that reads as
    cosmetic -- so the two surfaces are brought back into step the moment the rename lands,
    and again whenever the Lists screen is read, which is also what gives an adopted legacy
    row its name before anything has been checked.

    Rows are found by the definition id in their slug, never by the old name, so a row is
    re-homed however many renames it slept through.
    """
    await ensure_schema(engine)
    by_id = {d.id: d.name for d in definitions}
    moved = 0
    async with engine.begin() as conn:
        # Read inside the write transaction (rule 58).
        rows = (await conn.execute(text("SELECT slug, rule_name FROM protection_list"))).all()
        for row in rows:
            wanted = by_id.get(list_id_of(str(row.slug)) or -1)
            if wanted is None or str(row.rule_name or "") == wanted:
                continue
            await conn.execute(
                text("UPDATE protection_list SET rule_name = :name WHERE slug = :slug"),
                {"name": wanted, "slug": str(row.slug)},
            )
            moved += 1
    if moved:
        log.info("lists.rule_names_synced", rows=moved)
    return moved


#: A keep-tag row as the policy era spelled it: service, optional instance id, match mode --
#: and no ``-list`` tail, which is what marks it as predating the registry.
_LEGACY_KEEP_TAG = re.compile(rf"^(?:radarr|sonarr)(?:-\d+)?{re.escape(KEEP_TAG_INFIX)}(any|all)$")


async def adopt_legacy(
    engine: AsyncEngine, definitions: Sequence[ListDefinition]
) -> list[tuple[str, str]]:
    """Re-home rows stored before their definition existed onto the definition's slug.

    The upgrade migrations seed a definition for everything the old code derived -- the keep
    tags, the keep collection, the shipped IMDb chart -- but the stored membership keeps its
    legacy slug until a successful check rewrites it. Until then the screen shows one
    protection twice: an editable definition protecting nothing, beside an uneditable row
    holding everything. Renaming the stored slug in place hands the definition the
    membership, stats and timestamps the legacy row earned, so an upgrade's lists arrive
    rolled up and editable before anything has been checked.

    Conservative on every edge, because a wrong adoption files one list's membership under
    another list's name. A legacy row moves only when exactly ONE enabled definition claims
    its spelling; never onto a slug that already has a stored row (a check landed there
    first, and the retire sweep stands the legacy row down on its own); and a disabled row,
    one a sweep already retired, stays where it is.
    """
    claims: dict[str, list[str]] = {}
    tag_defs: dict[str, list[int]] = {}
    for definition in definitions:
        if not definition.enabled:
            continue
        if definition.source is ListSource.ARR_TAG:
            # Keyed by match mode, because the mode is in the stored slug: a legacy ANY row
            # must not land under a definition the operator has since tightened to ALL.
            tag_defs.setdefault(definition.match, []).append(definition.id)
        elif definition.source is ListSource.IMDB:
            variant = definition.imdb_variant
            new = ImdbList(variant=variant, list_id=definition.id).slug
            claims.setdefault(ImdbList(variant=variant).slug, []).append(new)
            if variant == "top250":
                # The chart's pre-registry spelling, which an upgraded cache still holds.
                claims.setdefault("imdb-top-250", []).append(new)
        elif definition.source is ListSource.PLEX_COLLECTION:
            collection = str(definition.config.get("collection", "")).strip()
            if not collection:
                continue
            # The providers spell their own slugs, so adoption cannot drift from the sync
            # (rule 144). Only the slug is read; no server is needed to derive it.
            claims.setdefault(
                PlexCollection(server=None, section_name="", collection_name=collection).slug,
                [],
            ).append(
                PlexCollection(
                    server=None,
                    section_name="",
                    collection_name=collection,
                    list_id=definition.id,
                ).slug
            )
        elif definition.source is ListSource.PLEX_WATCHLIST:
            claims.setdefault(PlexWatchlist(server=None).slug, []).append(
                PlexWatchlist(server=None, list_id=definition.id).slug
            )

    await ensure_schema(engine)
    renames: list[tuple[str, str]] = []
    async with engine.begin() as conn:
        # Read inside the write transaction (rule 58): the rows being renamed and the
        # occupancy check on their targets are decided from what this transaction sees.
        rows = (await conn.execute(text("SELECT slug, enabled FROM protection_list"))).all()
        stored = {str(r.slug) for r in rows}
        for row in rows:
            slug = str(row.slug)
            if not row.enabled or list_id_of(slug) is not None:
                continue
            wanted = claims.get(slug, [])
            matched = _LEGACY_KEEP_TAG.match(slug)
            if matched:
                mine = tag_defs.get(matched.group(1), [])
                wanted = [slug + list_suffix(mine[0])] if len(mine) == 1 else []
            if len(wanted) != 1 or wanted[0] in stored:
                continue
            renames.append((slug, wanted[0]))
            # Claimed: a second legacy spelling of the same list must not rename onto it
            # too, which would collide on the table's primary key.
            stored.add(wanted[0])
        for old, new in renames:
            await conn.execute(
                text("UPDATE protection_list SET slug = :new WHERE slug = :old"),
                {"old": old, "new": new},
            )
            await conn.execute(
                text("UPDATE protection_list_item SET slug = :new WHERE slug = :old"),
                {"old": old, "new": new},
            )
    for old, new in renames:
        log.info("lists.adopted", old=old, new=new)
    return renames


@dataclass(frozen=True, slots=True)
class MembershipIndex:
    """Every enabled protection-list row, loaded once and looked up in memory.

    A scan asks "which lists contain this item?" once per movie and once per show.
    Answering each of those with its own SQLite round trip (plus the ensure-schema DDL
    :func:`memberships` runs first) dominated the judge loop on large libraries, so the
    scan loads this index once and every per-item lookup becomes a dict hit.

    Parity with :func:`memberships` is the contract: a lookup returns one
    :class:`Membership` per matching *stored row* -- a row reachable through more than
    one of its ids still counts once, and two distinct rows of the same list still count
    twice -- so the two paths cannot disagree about what protects an item. Entries carry
    their load order so results are deterministic.

    Every entry also carries its row's ``media_type`` and a lookup only matches rows of
    the *same* type. TMDb numbers movies and shows in separate id spaces (movie #1399 and
    show #1399 are unrelated titles), so without this a show whose TMDb id coincides with
    a Top 250 film would be reported "on the IMDb Top 250" -- a keep the owner never
    asked for and a why-panel that lies. IMDb ids are globally unique, but the filter is
    applied to every key kind so the join key is always (media_type, key).

    Four maps, not three: a keep-collection entry Plex never matched to an agent is
    stored under :attr:`ListItem.plex_rating_key` alone, and is reachable only through
    that map.
    """

    _by_imdb: Mapping[str, tuple[tuple[int, str, Membership], ...]]
    _by_tmdb: Mapping[int, tuple[tuple[int, str, Membership], ...]]
    _by_tvdb: Mapping[int, tuple[tuple[int, str, Membership], ...]]
    _by_plex_key: Mapping[int, tuple[tuple[int, str, Membership], ...]]

    def lookup(
        self,
        *,
        media_type: str,
        imdb_id: str | None = None,
        tmdb_id: int | None = None,
        tvdb_id: int | None = None,
        plex_rating_keys: Collection[int] = (),
    ) -> list[Membership]:
        """Which protected lists contain this item? Same answer as :func:`memberships`.

        ``media_type`` ("movie" | "tv") is the item's own type; only rows of that type
        can match, so a movie id space and a show id space never cross.

        Every key the item carries is passed together, never the first one that is set
        (rule 29): a "Never Reap" entry no agent matched is stored under its Plex key
        alone, and an item bound to a merged group of byte-identical twins carries one
        key per listing, any of which the operator may have put on the list.
        """
        if not (imdb_id or tmdb_id or tvdb_id or plex_rating_keys):
            return []
        entries: list[tuple[int, str, Membership]] = []
        if imdb_id is not None:
            entries += self._by_imdb.get(imdb_id, ())
        if tmdb_id is not None:
            entries += self._by_tmdb.get(tmdb_id, ())
        if tvdb_id is not None:
            entries += self._by_tvdb.get(tvdb_id, ())
        for key in plex_rating_keys:
            entries += self._by_plex_key.get(key, ())
        seen: set[int] = set()
        out: list[Membership] = []
        for seq, row_media_type, membership in sorted(entries, key=lambda entry: entry[0]):
            # A row whose kind disagrees with the item is skipped, so it protects nothing.
            # That is the right arm -- matching across kinds is how a show inherits a
            # film's keep -- which puts the whole burden on the write side: every insert
            # goes through ``sync``, which stores only ``ListItem.is_protectable`` entries,
            # so no current path can file a row under a kind its ids do not belong to.
            # This still fires for a row an older version wrote, when a Plex collection
            # holding anything other than a movie or a show was filed as a movie.
            if row_media_type != media_type:
                continue
            if seq not in seen:
                seen.add(seq)
                out.append(membership)
        return out


async def load_membership_index(engine: AsyncEngine) -> MembershipIndex:
    """Materialise the :func:`memberships` join once, for a whole scan's lookups."""
    await ensure_schema(engine)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT i.imdb_id, i.tmdb_id, i.tvdb_id, i.plex_rating_key, i.media_type, "
                    "       l.slug, l.display_name, l.rule_name, l.mode, l.kind, i.rank "
                    "FROM protection_list_item i "
                    "JOIN protection_list l ON l.slug = i.slug "
                    "WHERE l.enabled = 1"
                )
            )
        ).all()

    by_imdb: dict[str, list[tuple[int, str, Membership]]] = {}
    by_tmdb: dict[int, list[tuple[int, str, Membership]]] = {}
    by_tvdb: dict[int, list[tuple[int, str, Membership]]] = {}
    by_plex_key: dict[int, list[tuple[int, str, Membership]]] = {}
    for seq, row in enumerate(rows):
        membership = Membership(
            slug=str(row.slug),
            display_name=str(row.display_name),
            mode=ListMode(row.mode),
            kind=ListKind(row.kind),
            rank=int(row.rank) if row.rank is not None else None,
            rule_name=str(row.rule_name) if row.rule_name else "",
        )
        media_type = str(row.media_type)
        if row.imdb_id:
            by_imdb.setdefault(str(row.imdb_id), []).append((seq, media_type, membership))
        if row.tmdb_id is not None:
            by_tmdb.setdefault(int(row.tmdb_id), []).append((seq, media_type, membership))
        if row.tvdb_id is not None:
            by_tvdb.setdefault(int(row.tvdb_id), []).append((seq, media_type, membership))
        if row.plex_rating_key is not None:
            by_plex_key.setdefault(int(row.plex_rating_key), []).append(
                (seq, media_type, membership)
            )

    return MembershipIndex(
        _by_imdb={k: tuple(v) for k, v in by_imdb.items()},
        _by_tmdb={k: tuple(v) for k, v in by_tmdb.items()},
        _by_tvdb={k: tuple(v) for k, v in by_tvdb.items()},
        _by_plex_key={k: tuple(v) for k, v in by_plex_key.items()},
    )


async def memberships(
    engine: AsyncEngine,
    *,
    media_type: str,
    imdb_id: str | None = None,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
    plex_rating_keys: Collection[int] = (),
) -> list[Membership]:
    """Which protected lists contain this item?

    Matched on any external id we hold, within the item's own ``media_type``. A film on
    the Top 250 is protected whether we know it by IMDb id or TMDb id -- requiring both
    would silently drop the ones where only one is present -- but a show is never matched
    against a movie row, so a show whose TMDb id (a separate id space) coincides with a
    Top 250 film is not falsely protected.

    Implemented AS a one-item view over :func:`load_membership_index`, so there is
    exactly one place that decides what protects an item -- a second hand-written query
    here could drift from the one the scan actually uses (rule 3). The scan never calls
    this per item; it loads the index once. This form exists for one-off callers, where
    loading the (small) list tables per call is fine.
    """
    index = await load_membership_index(engine)
    return index.lookup(
        media_type=media_type,
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        plex_rating_keys=plex_rating_keys,
    )


class ListHealth(enum.StrEnum):
    """What a stored row says about whether this list is protecting anything right now.

    One derivation, because ``last_error`` and ``last_synced_at`` are two columns that mean
    four different things together, and a surface deciding for itself gets a different answer
    than the degradation check does (rule 144). ``snapshot.protection_sync_degradations`` is
    the enforcement; this is what the operator is shown, and both read
    ``snapshot.WHITELIST_STALE_AFTER`` rather than each carrying a bound.
    """

    WORKING = "working"
    """The last check succeeded and is recent enough to rely on."""

    STALE = "stale"
    """It succeeded, but too long ago. A keep list only: a title added since the last good
    check is not on the stored copy, so it is unprotected until the next one lands. A curated
    list churns slowly and keeps protecting from its stored copy, which is why the staleness
    bound is not applied to one."""

    FAILING = "failing"
    """The last check failed. Whether anything is still protected depends on ``item_count``:
    the atomic swap in :func:`sync` leaves the previous membership in place, so a populated
    list still covers what it covered before."""

    NEVER_CHECKED = "never_checked"
    """No successful check on record. Nothing is stored, so this list protects nothing."""


@dataclass(frozen=True, slots=True)
class ConfiguredList:
    """One stored row, typed. What the Lists screen renders and nothing else reads.

    Typed rather than the raw mapping it used to be, because every column arrives from
    SQLite as ``object`` and each of the six readers would otherwise re-narrow it -- the
    kind of per-caller coercion rule 104 exists to stop. ``health`` is on the row for the
    same reason: the verdict is derived once, here, beside the columns it reads.
    """

    slug: str
    display_name: str
    mode: str
    kind: str
    enabled: bool
    item_count: int
    last_synced_at: int | None
    last_error: str | None
    stats: dict[str, Any] | None = None
    """What the last good sync recorded beyond the count (``stats_json``): per-tag totals
    and the server name for a tag list, ``None`` for every other source and for rows
    written before the column existed, which read as "unknown", never as zero."""

    media_types: frozenset[str] = frozenset()
    """The protectable media types this slug's stored members actually span -- ``movie``,
    ``tv``, or both. Empty until a sync has run, which is what lets the Lists screen say a
    keep rule covers one side of a mixed list and leaves the other still deletable (#533):
    a rule protects a media type, and a list holds whichever types its members do. Filled
    from ``protection_list_item`` in :func:`configured`, not stored on the row."""

    @property
    def source(self) -> ListSource:
        """Which family this row belongs to. See :class:`ListSource` for why it is derived."""
        if KEEP_TAG_INFIX in self.slug:
            return ListSource.ARR_TAG
        if self.slug.startswith(PLEX_COLLECTION_PREFIX):
            return ListSource.PLEX_COLLECTION
        if self.slug.startswith(PLEX_WATCHLIST_PREFIX):
            return ListSource.PLEX_WATCHLIST
        return ListSource.IMDB

    @property
    def list_id(self) -> int | None:
        """Which definition this membership was synced for. See :func:`list_id_of`.

        ``None`` for the keep tags, whose definition is the policy rather than a row on
        Settings -> Lists -- and for a row stored before the registry existed, which is
        retired the moment the definition that replaced it syncs.
        """
        return list_id_of(self.slug)

    @property
    def last_success(self) -> datetime | None:
        """When the last check that actually landed was. ``last_synced_at`` is written only
        on success (:func:`sync`), so the column IS the last good check, not the last attempt."""
        return from_epoch(self.last_synced_at)

    def health(self, *, stale_after: timedelta, now: datetime) -> ListHealth:
        """This row's state, in the order the states override each other.

        A failure outranks "never checked" even though a first sync that failed leaves
        ``last_synced_at`` NULL: the list *was* checked, and saying it has not been hides the
        error that is the whole reason to look. ``item_count`` is deliberately not folded in
        -- a failing list with members and one without call for different action, and the
        count is on the row beside this.
        """
        if self.last_error:
            return ListHealth.FAILING
        last_success = self.last_success
        if last_success is None:
            return ListHealth.NEVER_CHECKED
        if self.kind == ListKind.WHITELIST.value and now - last_success > stale_after:
            return ListHealth.STALE
        return ListHealth.WORKING


async def configured(engine: AsyncEngine) -> Sequence[ConfiguredList]:
    """Every list, for the settings screen."""
    await ensure_schema(engine)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT slug, display_name, mode, kind, enabled, item_count, "
                    "       last_synced_at, last_error, stats_json "
                    "FROM protection_list ORDER BY slug"
                )
            )
        ).all()
        # Which media types each slug's members span, in one grouped pass over the small
        # membership table. A tag list is one slug per *arr instance -- a Radarr slug is all
        # movies, a Sonarr slug all shows -- so a family that reaches the screen across
        # several slugs carries each type on the slug that holds it (#533).
        spans = (
            await conn.execute(
                text("SELECT slug, media_type FROM protection_list_item GROUP BY slug, media_type")
            )
        ).all()
    types_by_slug: dict[str, set[str]] = {}
    for span in spans:
        media_type = str(span.media_type)
        if media_type in PROTECTABLE_MEDIA_TYPES:
            types_by_slug.setdefault(str(span.slug), set()).add(media_type)

    def _stats(raw: object) -> dict[str, Any] | None:
        """A stats body that will not parse reads as absent, never as a raised row
        (rule 96's shape): the counts are decoration on a row whose count column stands."""
        if not raw:
            return None
        try:
            body = json.loads(str(raw))
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    return [
        ConfiguredList(
            slug=str(r.slug),
            display_name=str(r.display_name),
            mode=str(r.mode),
            kind=str(r.kind),
            enabled=bool(r.enabled),
            item_count=int(r.item_count or 0),
            last_synced_at=None if r.last_synced_at is None else int(r.last_synced_at),
            last_error=None if r.last_error is None else str(r.last_error),
            stats=_stats(r.stats_json),
            media_types=frozenset(types_by_slug.get(str(r.slug), set())),
        )
        for r in rows
    ]
