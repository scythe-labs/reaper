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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clients.arr import RadarrClient, SonarrClient
from reaper.clients.base import IntegrationError
from reaper.clients.public import PublicClient
from reaper.clock import utcnow
from reaper.engine import identity

log = structlog.get_logger(__name__)

IMDB_TOP_250_URL = "https://api.radarr.video/v1/list/imdb/top250"


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
    provider like ``ArrTagRule`` derives them from its configuration, and a Protocol
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
        # Through clients/ like every other fetch (rule 33): the shared retry, timeout,
        # error-mapping and redirect policy, instead of a bespoke httpx use down here.
        parts = urlsplit(self.url)
        async with PublicClient(f"{parts.scheme}://{parts.netloc}") as client:
            payload = await client.get_json(parts.path)

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

    @property
    def slug(self) -> str:
        instance = f"-{self.instance_id}" if self.instance_id is not None else ""
        return f"{self.client.service}{instance}-keeptags-{self.match}"

    @property
    def display_name(self) -> str:
        joiner = " or " if self.match == "any" else " and "
        where = f" ({self.instance_name})" if self.instance_name else ""
        return f"{self.client.service.title()}{where} tag: {joiner.join(self.tags)}"

    async def fetch(self) -> list[ListItem]:
        if not self.tags:
            return []

        # First match wins when two tag labels collide after lowercasing ('Keep' and
        # 'keep' can both exist), and a malformed tag row is skipped rather than failing
        # the whole keep-list over a row that was never the owner's keep tag.
        by_label: dict[str, int] = {}
        for row in await self.client.tags():
            tag_id = row.get("id")
            if isinstance(tag_id, int):
                by_label.setdefault(str(row.get("label", "")).lower(), tag_id)

        wanted: set[int] = set()
        missing: list[str] = []
        for tag in self.tags:
            found = by_label.get(tag)
            if found is None:
                log.info("lists.tag_absent", tag=tag, service=self.client.service)
                missing.append(tag)
            else:
                wanted.add(found)
        # A missing tag is a missing CONTAINER, not an empty one: nothing can carry a
        # tag that does not exist, so returning [] here would be indistinguishable from
        # "the owner un-tagged everything" and sync() would wipe the stored membership.
        # Raise whenever the absence decides the outcome -- every tag gone, or any gone
        # under ALL (one absent tag already rules every title out). Under ANY with some
        # tags still present, the present tags' members still sync.
        if missing and (not wanted or self.match == "all"):
            raise ContainerMissingError(
                f"keep tag {', '.join(repr(t) for t in missing)} does not exist in "
                f"{self.client.service}"
            )

        def keeps(media: dict[str, Any]) -> bool:
            carried = set(media.get("tags") or [])
            if self.match == "all":
                return wanted <= carried
            return not wanted.isdisjoint(carried)

        if isinstance(self.client, RadarrClient):
            movies = await self.client.movies()
            return [
                ListItem(
                    media_type="movie",
                    imdb_id=m.get("imdbId") or None,
                    tmdb_id=m.get("tmdbId") or None,
                    title=str(m.get("title") or ""),
                )
                for m in movies
                if keeps(m)
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
            if keeps(s)
        ]


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
            # The container is not there to ask: deleted, renamed, or simply not yet
            # created. Which of those it is depends on what is already stored, and
            # sync() decides -- raising here (rather than returning []) is what lets a
            # stored membership survive a deleted "Never Reap" collection.
            log.info("lists.plex_collection_absent", collection=self.collection_name)
            raise ContainerMissingError(
                f"Plex collection {self.collection_name!r} does not exist in section "
                f"{self.section_name!r}"
            ) from None

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
                "err": error,
            },
        )


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
    """
    await ensure_schema(engine)

    try:
        items = [i for i in await provider.fetch() if i.has_any_id]
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
        items = []
    except Exception as exc:
        await _record_sync_error(
            engine, provider, mode=mode, kind=kind, weight=weight, error=str(exc)
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
    applied to every id kind so the join key is always (media_type, id).
    """

    _by_imdb: Mapping[str, tuple[tuple[int, str, Membership], ...]]
    _by_tmdb: Mapping[int, tuple[tuple[int, str, Membership], ...]]
    _by_tvdb: Mapping[int, tuple[tuple[int, str, Membership], ...]]

    def lookup(
        self,
        *,
        media_type: str,
        imdb_id: str | None = None,
        tmdb_id: int | None = None,
        tvdb_id: int | None = None,
    ) -> list[Membership]:
        """Which protected lists contain this item? Same answer as :func:`memberships`.

        ``media_type`` ("movie" | "tv") is the item's own type; only rows of that type
        can match, so a movie id space and a show id space never cross.
        """
        if not (imdb_id or tmdb_id or tvdb_id):
            return []
        entries: list[tuple[int, str, Membership]] = []
        if imdb_id is not None:
            entries += self._by_imdb.get(imdb_id, ())
        if tmdb_id is not None:
            entries += self._by_tmdb.get(tmdb_id, ())
        if tvdb_id is not None:
            entries += self._by_tvdb.get(tvdb_id, ())
        seen: set[int] = set()
        out: list[Membership] = []
        for seq, row_media_type, membership in sorted(entries, key=lambda entry: entry[0]):
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
                    "SELECT i.imdb_id, i.tmdb_id, i.tvdb_id, i.media_type, "
                    "       l.slug, l.display_name, l.mode, l.kind, i.rank "
                    "FROM protection_list_item i "
                    "JOIN protection_list l ON l.slug = i.slug "
                    "WHERE l.enabled = 1"
                )
            )
        ).all()

    by_imdb: dict[str, list[tuple[int, str, Membership]]] = {}
    by_tmdb: dict[int, list[tuple[int, str, Membership]]] = {}
    by_tvdb: dict[int, list[tuple[int, str, Membership]]] = {}
    for seq, row in enumerate(rows):
        membership = Membership(
            slug=str(row.slug),
            display_name=str(row.display_name),
            mode=ListMode(row.mode),
            kind=ListKind(row.kind),
            rank=int(row.rank) if row.rank is not None else None,
        )
        media_type = str(row.media_type)
        if row.imdb_id:
            by_imdb.setdefault(str(row.imdb_id), []).append((seq, media_type, membership))
        if row.tmdb_id is not None:
            by_tmdb.setdefault(int(row.tmdb_id), []).append((seq, media_type, membership))
        if row.tvdb_id is not None:
            by_tvdb.setdefault(int(row.tvdb_id), []).append((seq, media_type, membership))

    return MembershipIndex(
        _by_imdb={k: tuple(v) for k, v in by_imdb.items()},
        _by_tmdb={k: tuple(v) for k, v in by_tmdb.items()},
        _by_tvdb={k: tuple(v) for k, v in by_tvdb.items()},
    )


async def memberships(
    engine: AsyncEngine,
    *,
    media_type: str,
    imdb_id: str | None = None,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
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
    return index.lookup(media_type=media_type, imdb_id=imdb_id, tmdb_id=tmdb_id, tvdb_id=tvdb_id)


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
