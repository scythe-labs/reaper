# SPDX-License-Identifier: AGPL-3.0-or-later
"""Curated lists as protections.

No competitor does this. Maintainerr, Janitorr and Reclaimerr all do exclusions via
tags, collections or their own database -- none of them ingest a *curated list* as a
protection source. "Never reap anything in the IMDb Top 250" is a rule you cannot
write in any of them.

Four providers, in ascending order of how much configuration they cost you:

**Plex collection** -- zero configuration, and the best of the four. You curate a
"Never Reap" collection in the Plex app you already use daily; it is editable from
your phone; there is no new screen to learn. Reaper just reads it.

***arr tag** -- also zero configuration. Tag a series `reaper-keep` in Sonarr.

**IMDb Top 250** -- one click. Served by Radarr's own list service at
``https://api.radarr.video/v1/list/imdb/top250``: 250 items, `TmdbId` and `ImdbId`,
**no auth**, verified live. (IMDb has no official API for the chart, and its
non-commercial datasets do *not* contain the ranking -- it uses an unpublished
weighted formula. Do not try to derive it, and do not scrape it. This mirror is the
right answer.)

***arr import lists** -- free lunch. If you already subscribe to a "Top Movies" import
list in Radarr, ``GET /api/v3/importlist/movie`` tells us which items came from it, and
membership becomes a protection with no new API key and no new configuration at all.

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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clients.arr import RadarrClient, SonarrClient
from reaper.clients.base import IntegrationError
from reaper.clock import utcnow
from reaper.engine import identity

log = structlog.get_logger(__name__)

IMDB_TOP_250_URL = "https://api.radarr.video/v1/list/imdb/top250"


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
    """Somebody else's list -- the IMDb Top 250, an import list."""


@dataclass(frozen=True, slots=True)
class ListItem:
    """One entry. Identified by whatever external ids the source gives us."""

    media_type: str  # "movie" | "tv"
    imdb_id: str | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    title: str = ""
    rank: int | None = None
    """1 = top of the list, WHERE THE SOURCE PROVIDES ONE.

    Left None otherwise. Never inferred from array position: the IMDb Top 250 mirror
    returns its entries roughly chronologically, so position would be a fabrication.
    """

    @property
    def has_any_id(self) -> bool:
        return bool(self.imdb_id or self.tmdb_id or self.tvdb_id)


class ListProvider(Protocol):
    """A source of protected items.

    ``slug`` and ``display_name`` are read-only *properties*, not attributes: a
    provider like ``ArrTag`` derives them from its configuration, and a Protocol
    declaring a mutable attribute would not accept that.
    """

    @property
    def slug(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    async def fetch(self) -> list[ListItem]: ...


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImdbTop250:
    """The IMDb Top 250, via Radarr's own list service. No auth, no key.

    **Membership is binary. There is no rank.**

    The payload carries no rank, position or order field, and the entries come back in
    roughly *chronological* order -- the first is The Kid (1921). Taking the array index
    as a chart position would have told the owner "The Kid is #1 on the IMDb Top 250",
    which is simply false, and the why-panel would have been confidently lying.

    So ``rank`` is left None. Being *on* the list is the signal; where you sit on it is
    something this source does not know.
    """

    slug: str = "imdb-top-250"
    display_name: str = "IMDb Top 250"
    url: str = IMDB_TOP_250_URL

    async def fetch(self) -> list[ListItem]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            response = await client.get(self.url)
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, list):
            raise IntegrationError(self.slug, "expected a JSON array")

        items = [
            ListItem(
                media_type="movie",
                imdb_id=entry.get("ImdbId") or None,
                tmdb_id=entry.get("TmdbId") or None,
                title=str(entry.get("Title") or ""),
                rank=None,  # See the class docstring. The source does not carry one.
            )
            for entry in payload
        ]

        if len(items) < 200:
            # A truncated list is worse than no list: it would silently stop
            # protecting the films that fell off it.
            raise IntegrationError(
                self.slug,
                f"expected ~250 entries, got {len(items)}. Refusing to install a "
                "truncated protection list.",
            )
        return items


@dataclass(frozen=True, slots=True)
class ArrTag:
    """A tag in Sonarr or Radarr -- `reaper-keep` by convention.

    Zero new configuration: you apply it in a UI you already use.
    """

    client: SonarrClient | RadarrClient
    tag_label: str = "reaper-keep"

    @property
    def slug(self) -> str:
        return f"{self.client.service}-tag-{self.tag_label}"

    @property
    def display_name(self) -> str:
        return f"{self.client.service.title()} tag: {self.tag_label}"

    async def fetch(self) -> list[ListItem]:
        tags = await self.client.tags()
        tag_id = next(
            (int(t["id"]) for t in tags if str(t.get("label", "")).lower() == self.tag_label),
            None,
        )
        if tag_id is None:
            # Not an error. The owner simply has not created the tag yet.
            log.info("lists.tag_absent", tag=self.tag_label, service=self.client.service)
            return []

        if isinstance(self.client, RadarrClient):
            media = await self.client.movies()
            return [
                ListItem(
                    media_type="movie",
                    imdb_id=m.get("imdbId") or None,
                    tmdb_id=m.get("tmdbId") or None,
                    title=str(m.get("title") or ""),
                )
                for m in media
                if tag_id in (m.get("tags") or [])
            ]

        series = await self.client.series()
        return [
            ListItem(
                media_type="tv",
                imdb_id=s.get("imdbId") or None,
                tvdb_id=s.get("tvdbId") or None,
                title=str(s.get("title") or ""),
            )
            for s in series
            if tag_id in (s.get("tags") or [])
        ]


@dataclass(frozen=True, slots=True)
class ArrTagRule:
    """One or more *arr tags, combined -- the configurable "keep list".

    A title matches when it carries ANY of the tags (the usual case) or ALL of them, per
    ``match``. Each tag is fetched via :class:`ArrTag`; the results are then combined on media
    identity, so a title carrying a tag twice is not counted twice. Protect-only, like every
    list source -- the worst a mis-configured rule can do is fail to keep something.
    """

    client: SonarrClient | RadarrClient
    tags: tuple[str, ...]
    match: Literal["any", "all"] = "any"

    @property
    def slug(self) -> str:
        return f"{self.client.service}-keeptags-{self.match}"

    @property
    def display_name(self) -> str:
        joiner = " or " if self.match == "any" else " and "
        return f"{self.client.service.title()} tag: {joiner.join(self.tags)}"

    async def fetch(self) -> list[ListItem]:
        if not self.tags:
            return []

        def key(item: ListItem) -> tuple[str, str, int, int]:
            return (item.media_type, item.imdb_id or "", item.tmdb_id or 0, item.tvdb_id or 0)

        by_key: dict[tuple[str, str, int, int], ListItem] = {}
        tag_count: dict[tuple[str, str, int, int], int] = {}
        for tag in self.tags:
            for item in await ArrTag(self.client, tag).fetch():
                k = key(item)
                by_key[k] = item
                tag_count[k] = tag_count.get(k, 0) + 1

        # ANY -> in at least one tag's set; ALL -> in every tag's set.
        need = len(self.tags) if self.match == "all" else 1
        return [by_key[k] for k, count in tag_count.items() if count >= need]


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

    @property
    def slug(self) -> str:
        return f"plex-collection-{self.collection_name.lower().replace(' ', '-')}"

    @property
    def display_name(self) -> str:
        return f'Plex collection: "{self.collection_name}"'

    async def fetch(self) -> list[ListItem]:
        import asyncio

        return await asyncio.to_thread(self._fetch_sync)

    def _fetch_sync(self) -> list[ListItem]:
        from plexapi.exceptions import NotFound

        section = self.server.library.section(self.section_name)  # type: ignore[attr-defined]
        try:
            collection = section.collection(self.collection_name)
        except NotFound:
            # Not an error. The owner simply has not made the collection yet.
            log.info("lists.plex_collection_absent", collection=self.collection_name)
            return []

        items: list[ListItem] = []
        for item in collection.items():
            # The new Plex agents expose external ids as Guid children; the legacy agents
            # put a single id in `guid`. identity.parse_guids handles both (and the
            # ``?lang=`` suffix, and sentinels), so a legacy-agent library is no longer
            # silently unprotected -- the same one parser the scan's matcher uses.
            guid_ids = [str(getattr(guid, "id", "")) for guid in getattr(item, "guids", None) or []]
            legacy = getattr(item, "guid", None)
            ids = identity.parse_guids(guid_ids, str(legacy) if legacy else None)
            items.append(
                ListItem(
                    media_type="tv" if item.type == "show" else "movie",
                    imdb_id=ids.imdb,
                    tmdb_id=ids.tmdb,
                    tvdb_id=ids.tvdb,
                    title=str(item.title),
                )
            )
        return items


@dataclass(frozen=True, slots=True)
class RadarrImportList:
    """Movies the owner's own Radarr import lists brought in.

    The free lunch. If they already subscribe to a "Top Movies" import list, membership
    becomes a protection with no new API key and no new configuration.
    """

    client: RadarrClient
    slug: str = "radarr-import-lists"
    display_name: str = "Radarr import lists"

    async def fetch(self) -> list[ListItem]:
        movies = await self.client.import_list_movies()
        return [
            ListItem(
                media_type="movie",
                imdb_id=m.get("imdbId") or None,
                tmdb_id=m.get("tmdbId") or None,
                title=str(m.get("title") or ""),
            )
            for m in movies
            if m.get("isExisting")  # already in the library, so it is ours to protect
        ]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS protection_list (
    slug          TEXT PRIMARY KEY,
    display_name  TEXT    NOT NULL,
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
    title      TEXT,
    rank       INTEGER,
    PRIMARY KEY (slug, media_type, imdb_id, tmdb_id, tvdb_id)
);
CREATE INDEX IF NOT EXISTS ix_pli_imdb ON protection_list_item (imdb_id);
CREATE INDEX IF NOT EXISTS ix_pli_tmdb ON protection_list_item (tmdb_id);
CREATE INDEX IF NOT EXISTS ix_pli_tvdb ON protection_list_item (tvdb_id);
"""


@dataclass(frozen=True, slots=True)
class Membership:
    """Why an item is protected, and by which list."""

    slug: str
    display_name: str
    mode: ListMode
    kind: ListKind
    rank: int | None

    @property
    def is_whitelist(self) -> bool:
        return self.kind is ListKind.WHITELIST

    def describe(self) -> str:
        position = f" (#{self.rank})" if self.rank else ""
        return f"{self.display_name}{position}"


async def ensure_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for statement in SCHEMA.strip().split(";"):
            if statement.strip():
                await conn.execute(text(statement))


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
    it would stop protecting without saying so.
    """
    await ensure_schema(engine)

    try:
        items = [i for i in await provider.fetch() if i.has_any_id]
    except Exception as exc:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO protection_list "
                    "(slug, display_name, mode, kind, weight, last_error) "
                    "VALUES (:slug, :name, :mode, :kind, :weight, :err) "
                    "ON CONFLICT(slug) DO UPDATE SET last_error = :err"
                ),
                {
                    "slug": provider.slug,
                    "name": provider.display_name,
                    "mode": mode.value,
                    "kind": kind.value,
                    "weight": weight,
                    "err": str(exc),
                },
            )
        log.warning("lists.sync_failed", slug=provider.slug, error=str(exc))
        raise

    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM protection_list_item WHERE slug = :slug"),
            {"slug": provider.slug},
        )
        if items:
            await conn.execute(
                text(
                    "INSERT OR REPLACE INTO protection_list_item "
                    "(slug, media_type, imdb_id, tmdb_id, tvdb_id, title, rank) "
                    "VALUES (:slug, :media_type, :imdb_id, :tmdb_id, :tvdb_id, :title, :rank)"
                ),
                [
                    {
                        "slug": provider.slug,
                        "media_type": i.media_type,
                        "imdb_id": i.imdb_id,
                        "tmdb_id": i.tmdb_id,
                        "tvdb_id": i.tvdb_id,
                        "title": i.title,
                        "rank": i.rank,
                    }
                    for i in items
                ],
            )
        await conn.execute(
            text(
                "INSERT INTO protection_list "
                "(slug, display_name, mode, kind, weight, enabled, item_count, "
                " last_synced_at, last_error) "
                "VALUES (:slug, :name, :mode, :kind, :weight, 1, :count, :now, NULL) "
                "ON CONFLICT(slug) DO UPDATE SET "
                "  display_name = :name, mode = :mode, kind = :kind, item_count = :count, "
                "  last_synced_at = :now, last_error = NULL"
            ),
            {
                "slug": provider.slug,
                "name": provider.display_name,
                "mode": mode.value,
                "kind": kind.value,
                "weight": weight,
                "count": len(items),
                "now": int(utcnow().timestamp()),
            },
        )

    log.info("lists.synced", slug=provider.slug, items=len(items))
    return len(items)


async def memberships(
    engine: AsyncEngine,
    *,
    imdb_id: str | None = None,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
) -> list[Membership]:
    """Which protected lists contain this item?

    Matched on any external id we hold. A film on the Top 250 is protected whether we
    know it by IMDb id or TMDb id -- requiring both would silently drop the ones where
    only one is present.
    """
    if not (imdb_id or tmdb_id or tvdb_id):
        return []

    await ensure_schema(engine)

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT l.slug, l.display_name, l.mode, l.kind, i.rank "
                    "FROM protection_list_item i "
                    "JOIN protection_list l ON l.slug = i.slug "
                    "WHERE l.enabled = 1 AND ("
                    "  (:imdb IS NOT NULL AND i.imdb_id = :imdb) OR "
                    "  (:tmdb IS NOT NULL AND i.tmdb_id = :tmdb) OR "
                    "  (:tvdb IS NOT NULL AND i.tvdb_id = :tvdb)"
                    ")"
                ),
                {"imdb": imdb_id, "tmdb": tmdb_id, "tvdb": tvdb_id},
            )
        ).all()

    return [
        Membership(
            slug=str(r.slug),
            display_name=str(r.display_name),
            mode=ListMode(r.mode),
            kind=ListKind(r.kind),
            rank=int(r.rank) if r.rank is not None else None,
        )
        for r in rows
    ]


async def configured(engine: AsyncEngine) -> Sequence[dict[str, object]]:
    """Every list, for the settings screen."""
    await ensure_schema(engine)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT slug, display_name, mode, kind, weight, enabled, item_count, "
                    "       last_synced_at, last_error FROM protection_list ORDER BY slug"
                )
            )
        ).all()
    return [dict(r._mapping) for r in rows]
