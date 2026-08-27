# SPDX-License-Identifier: AGPL-3.0-or-later
"""Curated lists as protections.

This is the differentiating feature. No competitor ingests a curated list as a protection
source. "Never reap anything in the IMDb Top 250" is a rule you cannot write in
Maintainerr, Janitorr or Reclaimerr.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reaper.clients.base import IntegrationError
from reaper.config import Settings
from reaper.db.session import create_engine
from reaper.services.list_config import ListDefinition
from reaper.services.lists import (
    IMDB_LIST_BASE,
    IMDB_TOP_250_URL,
    ArrTagRule,
    ContainerMissingError,
    ImdbList,
    ListItem,
    ListKind,
    ListMode,
    ListSource,
    PlexCollection,
    PlexWatchlist,
    adopt_legacy,
    configured,
    ensure_schema,
    load_membership_index,
    memberships,
    sync,
)
from tests._fakes import FakeSonarr

pytestmark = pytest.mark.httpx2(assert_all_called=False)


@dataclass(frozen=True)
class _Held:
    """One object in a fake keep collection, where a bare guid string will not do.

    ``guid=None`` is an item whose guids no longer parse, ``type`` is what Plex calls the
    object (a collection can hold more than movies and shows), ``title`` is what a row
    carried over from storage is matched on, and ``rating_key`` is Plex's own key for the
    object.

    A real Plex object always carries a rating key, so a test about what Plex hands back
    passes one; ``rating_key=None`` models the object Reaper can read nothing at all
    from, which is what the bare guid-string form gives.
    """

    guid: str | None
    title: str = "A title"
    type: str = "movie"
    rating_key: int | None = None


class _FakePlexServer:
    """A Plex stand-in for the keep-collection tests.

    Built from ``{library: entries or None}``: ``None`` is a library with no such
    collection (plexapi raises ``NotFound``), a list is one holding an object per entry,
    and an entry is either a guid string (a movie carrying that IMDb id, or ``None`` for
    one whose guids no longer parse) or a :class:`_Held` spelling out title and type.
    Every library reports the *same* title, so several entries model the same-title case
    the resolver has to survive.
    """

    def __init__(self, libraries: dict[str, list[str | _Held | None] | None]) -> None:
        self.library = self._Library(libraries)

    class _Library:
        def __init__(self, libraries: dict[str, list[str | _Held | None] | None]) -> None:
            self._libraries = libraries

        def sections(self) -> list[_FakePlexServer._Section]:
            return [
                _FakePlexServer._Section(name.rstrip(" *"), entries)
                for name, entries in self._libraries.items()
            ]

    class _Section:
        def __init__(self, title: str, entries: list[str | _Held | None] | None) -> None:
            self.title = title
            self._entries = entries

        def collection(self, name: str) -> object:
            from plexapi.exceptions import NotFound

            if self._entries is None:
                raise NotFound("no such collection")
            return _FakePlexServer._Collection(self._entries)

    class _Collection:
        def __init__(self, entries: list[str | _Held | None]) -> None:
            self._entries = [e if isinstance(e, _Held) else _Held(e) for e in entries]

        def items(self) -> list[object]:
            from types import SimpleNamespace

            return [
                SimpleNamespace(
                    type=e.type,
                    title=e.title,
                    guids=[SimpleNamespace(id=f"imdb://{e.guid}")] if e.guid else [],
                    guid=None,
                    **({} if e.rating_key is None else {"ratingKey": e.rating_key}),
                )
                for e in self._entries
            ]


class TestArrTagRule:
    """The configurable keep-list: several tags, combined ``any`` (union) or ``all``
    (intersection)."""

    @pytest.fixture
    def sonarr(self) -> FakeSonarr:
        tags = [{"id": 1, "label": "keep"}, {"id": 2, "label": "gold"}]
        series = [
            {"title": "A", "tvdbId": 10, "tags": [1]},  # keep only
            {"title": "B", "tvdbId": 11, "tags": [1, 2]},  # keep AND gold
            {"title": "C", "tvdbId": 12, "tags": [2]},  # gold only
        ]
        return FakeSonarr(tag_rows=tags, series_rows=series)

    async def test_any_is_the_union(self, sonarr: FakeSonarr) -> None:
        items = await ArrTagRule(sonarr, ("keep", "gold"), "any").fetch()
        assert {i.title for i in items} == {"A", "B", "C"}

    async def test_all_is_the_intersection(self, sonarr: FakeSonarr) -> None:
        items = await ArrTagRule(sonarr, ("keep", "gold"), "all").fetch()
        assert {i.title for i in items} == {"B"}  # only B has both tags

    async def test_a_single_tag_is_just_that_tag(self, sonarr: FakeSonarr) -> None:
        items = await ArrTagRule(sonarr, ("keep",), "any").fetch()
        assert {i.title for i in items} == {"A", "B"}

    async def test_no_tags_keeps_nothing(self, sonarr: FakeSonarr) -> None:
        assert await ArrTagRule(sonarr, (), "any").fetch() == []

    @pytest.mark.parametrize("configured", ["Reaper-Keep", "REAPER-KEEP", " reaper-keep "])
    async def test_the_operators_capitalization_still_matches(self, configured: str) -> None:
        """Sonarr and Radarr lower-case every label at the source, so an operator who
        configures the natural capitalization of their own tag was looking up a spelling
        the tag map cannot hold. The tag then read as *missing*, and on a first sync that
        stored an empty keep-list and reported success. Keep-tagged titles were silently
        deletable, forever, with the settings screen showing the list as healthy."""
        sonarr = FakeSonarr(
            tag_rows=[{"id": 1, "label": "reaper-keep"}],
            series_rows=[{"title": "A", "tvdbId": 10, "tags": [1]}],
        )

        items = await ArrTagRule(sonarr, (configured,), "any").fetch()

        assert [i.title for i in items] == ["A"]

    async def test_a_tag_that_really_is_absent_still_raises(self) -> None:
        """The other direction: normalizing must not turn a genuinely missing tag into a
        match, or the wipe protection above stops firing."""
        sonarr = FakeSonarr(tag_rows=[{"id": 1, "label": "other"}], series_rows=[])

        with pytest.raises(ContainerMissingError):
            await ArrTagRule(sonarr, ("reaper-keep",), "any").fetch()

    async def test_any_mode_counts_each_tag_independently(self, sonarr: FakeSonarr) -> None:
        """The per-tag counts answer "which tags are doing the protecting here", so each
        tag counts its own carriers: A and B carry keep, B and C carry gold."""
        rule = ArrTagRule(sonarr, ("keep", "gold"), "any")
        await rule.fetch()

        assert rule.tag_counts == {"keep": 2, "gold": 2}

    async def test_all_mode_counts_per_tag_not_per_match(self, sonarr: FakeSonarr) -> None:
        """Under ``all`` only B matches the rule, and the counts still say what each tag
        covers on its own. A per-tag count is independent of the combining mode."""
        rule = ArrTagRule(sonarr, ("keep", "gold"), "all")
        items = await rule.fetch()

        assert {i.title for i in items} == {"B"}
        assert rule.tag_counts == {"keep": 2, "gold": 2}

    async def test_the_counts_keep_the_operators_own_spelling(self) -> None:
        """The counts are keyed by the spelling the operator configured, which is what the
        Lists screen echoes back. Both sides of the lookup itself stay case-folded."""
        sonarr = FakeSonarr(
            tag_rows=[{"id": 1, "label": "reaper-keep"}],
            series_rows=[{"title": "A", "tvdbId": 10, "tags": [1]}],
        )
        rule = ArrTagRule(sonarr, ("Reaper-Keep",), "any")
        await rule.fetch()

        assert rule.tag_counts == {"Reaper-Keep": 1}

    async def test_sync_stats_carries_the_counts_and_the_server(self, sonarr: FakeSonarr) -> None:
        """The server is named service-first ("Sonarr (hd)"). The instance name alone is
        the operator's own label ("hd", "4k"), which two services can share, and the
        per-server fold-out on the Lists screen echoes this string as the whole row head."""
        rule = ArrTagRule(sonarr, ("keep",), "any", instance_name="hd")
        await rule.fetch()

        assert rule.sync_stats == {"tags": {"keep": 2}, "server": "Sonarr (hd)"}

    async def test_stats_before_any_counting_pass_read_as_unknown_not_zero(
        self, sonarr: FakeSonarr
    ) -> None:
        """An untaken count is unknown, never zero. Before a fetch the stats carry
        ``tags: None``, which the screen renders as bare pills."""
        rule = ArrTagRule(sonarr, ("keep",), "any", instance_name="hd")

        assert rule.sync_stats == {"tags": None, "server": "Sonarr (hd)"}

    async def test_a_wholly_missing_tag_counts_zero_for_every_tag(self) -> None:
        """No configured tag exists upstream, so no title carries one. Every count is a
        *true* zero, and recording them is what lets the genuinely-empty first sync show
        "0" on the Lists screen instead of a blank."""
        sonarr = FakeSonarr(tag_rows=[{"id": 1, "label": "other"}], series_rows=[])
        rule = ArrTagRule(sonarr, ("keep", "gold"), "any")

        with pytest.raises(ContainerMissingError):
            await rule.fetch()

        assert rule.sync_stats == {"tags": {"keep": 0, "gold": 0}, "server": "Sonarr"}

    async def test_all_mode_with_one_tag_resolved_leaves_the_counts_unknown(
        self, sonarr: FakeSonarr
    ) -> None:
        """Under ``all`` one absent tag aborts the fetch before the counting pass, so the
        resolved tags' counts were never taken. An untaken count is unknown, not zero.
        "keep" genuinely covers titles here, and storing 0 would say the opposite."""
        rule = ArrTagRule(sonarr, ("keep", "absent"), "all")

        with pytest.raises(ContainerMissingError):
            await rule.fetch()

        assert rule.sync_stats == {"tags": None, "server": "Sonarr"}


class TestSyncStatsRoundTrip:
    """``sync`` stores what a provider knows about its own check (``stats_json``), and
    ``configured`` reads it back for the Lists screen. Written on success only, so like the
    membership it is always from the last good check."""

    @staticmethod
    def _rule(instance_name: str = "hd") -> ArrTagRule:
        sonarr = FakeSonarr(
            tag_rows=[{"id": 1, "label": "keep"}],
            series_rows=[{"title": "A", "tvdbId": 10, "tags": [1]}],
        )
        return ArrTagRule(sonarr, ("keep",), "any", instance_name=instance_name)

    async def test_the_stats_round_trip_through_the_stored_row(self, engine: AsyncEngine) -> None:
        rule = self._rule()
        await sync(engine, rule, kind=ListKind.WHITELIST)

        rows = {r.slug: r for r in await configured(engine)}

        assert rows[rule.slug].stats == {"tags": {"keep": 1}, "server": "Sonarr (hd)"}

    async def test_a_provider_without_stats_stores_none(self, engine: AsyncEngine) -> None:
        provider = _StaticProvider([ListItem(media_type="movie", imdb_id="tt0000001", title="A")])
        await sync(engine, provider)

        rows = {r.slug: r for r in await configured(engine)}

        assert rows[provider.slug].stats is None

    @pytest.mark.parametrize("stored", ["not json at all", "[1, 2]", '"a string"'])
    async def test_a_malformed_stats_body_reads_as_none_not_as_a_raised_row(
        self, engine: AsyncEngine, stored: str
    ) -> None:
        """The counts are decoration on a row whose count column stands, so a body that
        will not parse (or is not an object) reads as unknown rather than raising the row
        off the screen."""
        rule = self._rule()
        await sync(engine, rule, kind=ListKind.WHITELIST)
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE protection_list SET stats_json = :s WHERE slug = :slug"),
                {"s": stored, "slug": rule.slug},
            )

        rows = {r.slug: r for r in await configured(engine)}

        assert rows[rule.slug].stats is None


class TestMediaTypesSpan:
    """``configured`` reports which media types a slug's stored members span, read back from
    ``protection_list_item``. The Lists screen compares it against the types a keep rule
    names, so a rule covering one side of a mixed list reads as partial cover, not full."""

    async def test_a_list_holding_both_kinds_spans_both(self, engine: AsyncEngine) -> None:
        provider = _StaticProvider(
            [
                ListItem(media_type="movie", imdb_id="tt0000001", title="A film"),
                ListItem(media_type="tv", tvdb_id=10, title="A show"),
            ]
        )
        await sync(engine, provider)

        rows = {r.slug: r for r in await configured(engine)}

        assert rows[provider.slug].media_types == frozenset({"movie", "tv"})

    async def test_a_single_kind_list_spans_one(self, engine: AsyncEngine) -> None:
        provider = _StaticProvider([ListItem(media_type="movie", imdb_id="tt0000001", title="A")])
        await sync(engine, provider)

        rows = {r.slug: r for r in await configured(engine)}

        assert rows[provider.slug].media_types == frozenset({"movie"})

    async def test_a_row_with_no_members_yet_spans_nothing(self, engine: AsyncEngine) -> None:
        # A defined list before its first sync holds no members, so it spans nothing. That
        # is what lets the screen say an unchecked list protects neither side, rather than
        # claiming cover it cannot confirm.
        await ensure_schema(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO protection_list (slug, display_name, mode, kind) "
                    "VALUES ('defined-no-sync', 'Defined, not checked', 'hard', 'whitelist')"
                )
            )

        rows = {r.slug: r for r in await configured(engine)}

        assert rows["defined-no-sync"].media_types == frozenset()


class TestAdoptLegacy:
    """Rows stored before their definition existed are renamed onto the definition's slug,
    so an upgrade's lists arrive rolled up and editable with their membership, instead of
    sitting as uneditable orphans until the next successful check."""

    @staticmethod
    def _definition(
        list_id: int,
        source: ListSource,
        config: dict[str, object],
        *,
        enabled: bool = True,
        name: str = "A list",
    ) -> ListDefinition:
        return ListDefinition(id=list_id, name=name, source=source, config=config, enabled=enabled)

    @staticmethod
    async def _seed_keep_tag_row(engine: AsyncEngine) -> ArrTagRule:
        """One legacy keep-tag list with a member, exactly as the policy era stored it:
        no ``-list`` suffix on the slug."""
        sonarr = FakeSonarr(
            tag_rows=[{"id": 1, "label": "keep"}],
            series_rows=[{"title": "A", "tvdbId": 10, "tags": [1]}],
        )
        rule = ArrTagRule(sonarr, ("keep",), "any", instance_id=3, instance_name="hd")
        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 1
        return rule

    async def test_a_keep_tag_row_takes_its_definitions_slug_with_its_membership(
        self, engine: AsyncEngine
    ) -> None:
        rule = await self._seed_keep_tag_row(engine)
        definition = self._definition(7, ListSource.ARR_TAG, {"tags": ["keep"], "match": "any"})

        renamed = await adopt_legacy(engine, [definition])

        assert renamed == [(rule.slug, f"{rule.slug}-list7")]
        rows = {r.slug: r for r in await configured(engine)}
        adopted = rows[f"{rule.slug}-list7"]
        assert rule.slug not in rows
        assert adopted.list_id == 7
        assert adopted.item_count == 1
        assert adopted.last_synced_at is not None  # the legacy row's history came along
        assert adopted.stats == {"tags": {"keep": 1}, "server": "Sonarr (hd)"}
        index = await load_membership_index(engine)
        assert index.lookup(media_type="tv", tvdb_id=10)  # the item rows moved with it

    async def test_two_definitions_of_the_same_match_adopt_nothing(
        self, engine: AsyncEngine
    ) -> None:
        """Which of the two the row belongs to cannot be known, and a wrong adoption files
        one list's membership under another list's name. So neither claims it, and the
        next successful sync sorts it out."""
        rule = await self._seed_keep_tag_row(engine)
        definitions = [
            self._definition(7, ListSource.ARR_TAG, {"tags": ["keep"], "match": "any"}),
            self._definition(8, ListSource.ARR_TAG, {"tags": ["gold"], "match": "any"}, name="B"),
        ]

        assert await adopt_legacy(engine, definitions) == []
        assert rule.slug in {r.slug for r in await configured(engine)}

    async def test_a_definition_of_the_other_match_mode_adopts_nothing(
        self, engine: AsyncEngine
    ) -> None:
        """The match mode is in the stored slug. A legacy ``any`` row under a definition
        since tightened to ``all`` would protect wider than the definition says."""
        rule = await self._seed_keep_tag_row(engine)
        definition = self._definition(7, ListSource.ARR_TAG, {"tags": ["keep"], "match": "all"})

        assert await adopt_legacy(engine, [definition]) == []
        assert rule.slug in {r.slug for r in await configured(engine)}

    async def test_an_occupied_target_slug_is_never_overwritten(self, engine: AsyncEngine) -> None:
        """A check already landed under the definition's slug, so that row is the living
        one. The legacy row stays for the retire sweep to stand down."""
        rule = await self._seed_keep_tag_row(engine)
        sonarr = FakeSonarr(tag_rows=[{"id": 1, "label": "keep"}], series_rows=[])
        claimed = ArrTagRule(
            sonarr,
            ("keep",),
            "any",
            instance_id=3,
            instance_name="hd",
            list_id=7,
        )
        await sync(engine, claimed, kind=ListKind.WHITELIST)
        definition = self._definition(7, ListSource.ARR_TAG, {"tags": ["keep"], "match": "any"})

        assert await adopt_legacy(engine, [definition]) == []
        slugs = {r.slug for r in await configured(engine)}
        assert {rule.slug, claimed.slug} <= slugs

    async def test_a_disabled_row_stays_where_a_sweep_put_it(self, engine: AsyncEngine) -> None:
        rule = await self._seed_keep_tag_row(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE protection_list SET enabled = 0 WHERE slug = :slug"),
                {"slug": rule.slug},
            )
        definition = self._definition(7, ListSource.ARR_TAG, {"tags": ["keep"], "match": "any"})

        assert await adopt_legacy(engine, [definition]) == []

    async def test_a_disabled_definition_claims_nothing(self, engine: AsyncEngine) -> None:
        rule = await self._seed_keep_tag_row(engine)
        definition = self._definition(
            7, ListSource.ARR_TAG, {"tags": ["keep"], "match": "any"}, enabled=False
        )

        assert await adopt_legacy(engine, [definition]) == []
        assert rule.slug in {r.slug for r in await configured(engine)}

    @staticmethod
    async def _seed_raw_row(engine: AsyncEngine, slug: str) -> None:
        """A stored row under a legacy slug, with one member, as an old version left it."""
        await ensure_schema(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO protection_list "
                    "(slug, display_name, mode, kind, weight, enabled, item_count, "
                    " last_synced_at) VALUES (:slug, :slug, 'hard', 'curated', 0, 1, 1, 100)"
                ),
                {"slug": slug},
            )
            await conn.execute(
                text(
                    "INSERT INTO protection_list_item (slug, media_type, imdb_id, title) "
                    "VALUES (:slug, 'movie', 'tt0000001', 'A')"
                ),
                {"slug": slug},
            )

    async def test_the_retired_imdb_spelling_lands_under_the_preset_definition(
        self, engine: AsyncEngine
    ) -> None:
        """``imdb-top-250`` is the chart's pre-registry slug. The definition's provider
        spells the variant ``top250``."""
        await self._seed_raw_row(engine, "imdb-top-250")
        definition = self._definition(4, ListSource.IMDB, {"preset": "top250"})

        assert await adopt_legacy(engine, [definition]) == [("imdb-top-250", "imdb-top250-list4")]
        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", imdb_id="tt0000001")

    async def test_a_plex_collection_row_lands_under_its_definition(
        self, engine: AsyncEngine
    ) -> None:
        await self._seed_raw_row(engine, "plex-collection-never-reap")
        definition = self._definition(
            2, ListSource.PLEX_COLLECTION, {"library": "Movies", "collection": "Never Reap"}
        )

        assert await adopt_legacy(engine, [definition]) == [
            ("plex-collection-never-reap", "plex-collection-never-reap-list2")
        ]

    async def test_both_imdb_spellings_stored_at_once_adopt_only_one(
        self, engine: AsyncEngine
    ) -> None:
        """Two legacy spellings of the same chart map to one target. Renaming both would
        collide on the table's primary key, so the second stays for the retire sweep."""
        await self._seed_raw_row(engine, "imdb-top-250")
        await self._seed_raw_row(engine, "imdb-top250")
        definition = self._definition(4, ListSource.IMDB, {"preset": "top250"})

        renamed = await adopt_legacy(engine, [definition])

        assert len(renamed) == 1
        assert {r.slug for r in await configured(engine)} >= {"imdb-top250-list4"}


class TestAVanishedContainerNeverWipesTheList:
    """A renamed keep tag or a deleted "Never Reap" collection is a missing *container*,
    not an empty membership. With members stored, the sync must fail so the stale list
    keeps protecting. With nothing stored, it is a quiet first sync."""

    async def test_a_vanished_tag_with_stored_members_keeps_the_membership(
        self, engine: AsyncEngine
    ) -> None:
        good = FakeSonarr(
            tag_rows=[{"id": 1, "label": "keep"}],
            series_rows=[{"title": "A", "tvdbId": 10, "tags": [1]}],
        )
        rule = ArrTagRule(good, ("keep",), "any")
        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 1

        renamed = FakeSonarr(tag_rows=[{"id": 1, "label": "hold"}], series_rows=[])
        stale = ArrTagRule(renamed, ("keep",), "any")
        with pytest.raises(ContainerMissingError):
            await sync(engine, stale, kind=ListKind.WHITELIST)

        index = await load_membership_index(engine)
        assert index.lookup(media_type="tv", tvdb_id=10)  # the stored membership survived the wipe

    async def test_a_missing_tag_with_nothing_stored_is_an_empty_first_sync(
        self, engine: AsyncEngine
    ) -> None:
        sonarr = FakeSonarr(tag_rows=[{"id": 1, "label": "other"}], series_rows=[])
        rule = ArrTagRule(sonarr, ("keep",), "any")
        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 0

    async def test_under_all_one_missing_tag_raises_when_members_are_stored(
        self, engine: AsyncEngine
    ) -> None:
        """Under ``all``, one absent tag structurally rules every title out, which is the
        same wipe wearing a different hat, so it gets the same treatment."""
        both = FakeSonarr(
            tag_rows=[{"id": 1, "label": "keep"}, {"id": 2, "label": "gold"}],
            series_rows=[{"title": "B", "tvdbId": 11, "tags": [1, 2]}],
        )
        rule = ArrTagRule(both, ("keep", "gold"), "all")
        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 1

        one_gone = FakeSonarr(
            tag_rows=[{"id": 1, "label": "keep"}],
            series_rows=[{"title": "B", "tvdbId": 11, "tags": [1]}],
        )
        stale = ArrTagRule(one_gone, ("keep", "gold"), "all")
        with pytest.raises(ContainerMissingError):
            await sync(engine, stale, kind=ListKind.WHITELIST)

    async def test_a_deleted_plex_collection_is_a_missing_container(self) -> None:
        provider = PlexCollection(server=_FakePlexServer({"Movies": None}), section_name="Movies")
        with pytest.raises(ContainerMissingError):
            await provider.fetch()

    async def test_the_collection_is_found_in_the_second_library_of_that_name(self) -> None:
        """Two libraries may share a title, and ``library.section(title)`` answers with only
        one of them. Reading the keep collection off the wrong twin looks exactly like the
        collection having been deleted, which degrades the scan and, on a first sync, stores
        an empty keep-list."""
        provider = PlexCollection(
            server=_FakePlexServer({"Movies": None, "Movies ": None, "Movies*": ["tt0000001"]}),
            section_name="Movies",
        )
        # The third library is titled "Movies" too. The fake spells them apart only so
        # the dict can hold three, and reports every one of them under the same title.
        items = await provider.fetch()

        assert [i.imdb_id for i in items] == ["tt0000001"]

    async def test_a_library_that_does_not_exist_is_a_hard_failure(self) -> None:
        """This is not a missing container but a missing *library*. A name nothing
        matches is a configuration error, reported, never synced as an empty list."""
        provider = PlexCollection(server=_FakePlexServer({}), section_name="Movies")
        with pytest.raises(IntegrationError):
            await provider.fetch()

    async def test_a_populated_collection_with_nothing_readable_never_wipes_the_list(
        self, engine: AsyncEngine
    ) -> None:
        """A container that comes back populated and yields not one usable entry is a
        failure, not an empty list. Reading it as empty would wipe the stored membership
        and unprotect every title on it. The entries here carry no key of any kind, which
        is what every non-Plex source looks like once its ids stop parsing."""
        good = PlexCollection(
            server=_FakePlexServer({"Movies": ["tt0000001", "tt0000002"]}), section_name="Movies"
        )
        assert await sync(engine, good, kind=ListKind.WHITELIST) == 2

        unparseable = PlexCollection(
            server=_FakePlexServer({"Movies": [None, None]}), section_name="Movies"
        )
        with pytest.raises(ContainerMissingError):
            await sync(engine, unparseable, kind=ListKind.WHITELIST)

        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", imdb_id="tt0000001")


class TestATitleTheContainerStillListsIsNeverDropped:
    """The same loss at a smaller scale. *Some* of the entries stop parsing, not all.

    The survivors then look like a complete fetch, so the swap replaces the membership
    with them and everything else silently stops being protected. The keep tag is still
    on the title and nothing says a word. A title the container still lists keeps the
    keys its row was stored under instead.
    """

    @staticmethod
    def _collection(*entries: str | _Held | None) -> PlexCollection:
        return PlexCollection(
            server=_FakePlexServer({"Movies": list(entries)}), section_name="Movies"
        )

    async def test_a_title_whose_guids_stopped_parsing_keeps_its_ids(
        self, engine: AsyncEngine
    ) -> None:
        """The real shape of a Plex agent change. The object is still there, still
        carrying Plex's own key, and only the guids stopped resolving. Storing what came
        back would file it under that key alone, and the movie lane looks a keep list up
        by Radarr's ids, which the agent change never touched. So the row keeps both."""
        good = self._collection(
            _Held("tt0000001", title="First", rating_key=11),
            _Held("tt0000002", title="Second", rating_key=22),
        )
        assert await sync(engine, good, kind=ListKind.WHITELIST) == 2

        partial = self._collection(
            _Held("tt0000001", title="First", rating_key=11),
            _Held(None, title="Second", rating_key=22),
        )
        assert await sync(engine, partial, kind=ListKind.WHITELIST) == 2

        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", imdb_id="tt0000002")
        assert index.lookup(media_type="movie", plex_rating_keys=(22,))

    async def test_a_title_with_nothing_left_to_read_keeps_its_stored_row(
        self, engine: AsyncEngine
    ) -> None:
        """The floor of the same guarantee, for an entry Reaper can read nothing from, no
        guids and no key, which is every non-Plex source's shape too."""
        good = self._collection(
            _Held("tt0000001", title="First"), _Held("tt0000002", title="Second")
        )
        assert await sync(engine, good, kind=ListKind.WHITELIST) == 2

        partial = self._collection(_Held("tt0000001", title="First"), _Held(None, title="Second"))
        assert await sync(engine, partial, kind=ListKind.WHITELIST) == 2

        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", imdb_id="tt0000002")

    async def test_the_operators_own_capitalization_still_carries_it(
        self, engine: AsyncEngine
    ) -> None:
        """Both sides are case-folded, or a title Plex re-cased on a re-match is carried
        by nothing and the protection lapses on exactly the sync that renamed it."""
        good = self._collection(
            _Held("tt0000001", title="First"), _Held("tt0000002", title="Second")
        )
        assert await sync(engine, good, kind=ListKind.WHITELIST) == 2

        recased = self._collection(_Held("tt0000001", title="First"), _Held(None, title=" SECOND "))
        assert await sync(engine, recased, kind=ListKind.WHITELIST) == 2

        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", imdb_id="tt0000002")

    async def test_two_stored_rows_under_one_title_give_back_no_ids(
        self, engine: AsyncEngine
    ) -> None:
        """Which of them is this entry cannot be answered, so it is not guessed. Both
        rows are still carried, which keeps every protection that existed."""
        good = self._collection(
            _Held("tt0000001", title="Twin", rating_key=11),
            _Held("tt0000002", title="Twin", rating_key=22),
        )
        assert await sync(engine, good, kind=ListKind.WHITELIST) == 2

        lost = self._collection(
            _Held(None, title="Twin", rating_key=11), _Held(None, title="Twin", rating_key=22)
        )
        assert await sync(engine, lost, kind=ListKind.WHITELIST) == 2

        index = await load_membership_index(engine)
        # Stored under their keys alone this time: no id was invented for either.
        assert index.lookup(media_type="movie", plex_rating_keys=(11,))
        assert not index.lookup(media_type="movie", imdb_id="tt0000001")

    async def test_a_title_the_operator_took_off_the_list_is_still_removed(
        self, engine: AsyncEngine
    ) -> None:
        """The other direction, and the reason this holds over rather than refusing the
        swap: a removal the operator meant must still take effect, or the keep list can
        only ever grow."""
        good = self._collection(
            _Held("tt0000001", title="First"), _Held("tt0000002", title="Second")
        )
        assert await sync(engine, good, kind=ListKind.WHITELIST) == 2

        assert await sync(engine, self._collection(_Held("tt0000001", title="First"))) == 1

        index = await load_membership_index(engine)
        assert not index.lookup(media_type="movie", imdb_id="tt0000002")

    async def test_an_untitled_entry_holds_nothing(self, engine: AsyncEngine) -> None:
        """An empty title matches every untitled row, which would hold rows at random.
        It holds none of them instead."""
        good = self._collection(_Held("tt0000001", title="First"), _Held("tt0000002", title=""))
        assert await sync(engine, good, kind=ListKind.WHITELIST) == 2

        untitled = self._collection(_Held("tt0000001", title="First"), _Held(None, title=""))
        assert await sync(engine, untitled, kind=ListKind.WHITELIST) == 1

        index = await load_membership_index(engine)
        assert not index.lookup(media_type="movie", imdb_id="tt0000002")

    async def test_a_title_that_was_never_stored_is_reported_not_invented(
        self, engine: AsyncEngine
    ) -> None:
        """An entry with no key of any kind, never stored before, is on the list and
        protected by nothing. There is nothing to protect it by, and inventing something
        is worse. So the sync says so and carries on. Refusing instead would fail this
        list on every scan from now on, and past the staleness bound that stops the
        operator reaping anything at all."""
        stored = await sync(
            engine,
            self._collection(_Held("tt0000001", title="First"), _Held(None, title="Unreadable")),
            kind=ListKind.WHITELIST,
        )

        assert stored == 1
        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", imdb_id="tt0000001")


class TestATitlePlexNeverMatchedIsStillProtected:
    """A "Never Reap" collection can hold a title no agent ever gave an id: a home video,
    a personal-media item. It has nothing to be stored under but the key Plex itself uses,
    and without that it is on the operator's keep list and Reaper reaps it anyway."""

    @staticmethod
    def _collection(*entries: str | _Held | None) -> PlexCollection:
        return PlexCollection(
            server=_FakePlexServer({"Movies": list(entries)}), section_name="Movies"
        )

    async def test_a_home_video_is_protected_by_the_key_plex_uses(
        self, engine: AsyncEngine
    ) -> None:
        stored = await sync(
            engine,
            self._collection(_Held(None, title="Home video", rating_key=77)),
            kind=ListKind.WHITELIST,
        )

        assert stored == 1
        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", plex_rating_keys=(77,))

    async def test_a_collection_that_lost_every_guid_still_protects_every_title(
        self, engine: AsyncEngine
    ) -> None:
        """The whole-collection version of the agent change, which used to be refused
        outright because nothing on it could be identified. Every object still carries
        its key, so the list syncs and keeps protecting instead of going stale."""
        good = self._collection(
            _Held("tt0000001", title="First", rating_key=11),
            _Held("tt0000002", title="Second", rating_key=22),
        )
        assert await sync(engine, good, kind=ListKind.WHITELIST) == 2

        unparseable = self._collection(
            _Held(None, title="First", rating_key=11), _Held(None, title="Second", rating_key=22)
        )
        assert await sync(engine, unparseable, kind=ListKind.WHITELIST) == 2

        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", plex_rating_keys=(11,))
        assert index.lookup(media_type="movie", plex_rating_keys=(22,))

    async def test_any_of_a_merged_binds_listings_finds_it(self, engine: AsyncEngine) -> None:
        """One file listed twice is bound as a group, and the operator put *one* of those
        listings on the list. Every key the item carries is passed, so it does not matter
        which."""
        await sync(
            engine,
            self._collection(_Held(None, title="Home video", rating_key=77)),
            kind=ListKind.WHITELIST,
        )

        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", plex_rating_keys=(41, 77))
        assert not index.lookup(media_type="movie", plex_rating_keys=(41, 42))

    async def test_a_key_never_matches_across_kinds(self, engine: AsyncEngine) -> None:
        """The join key stays (kind, key) for a Plex key exactly as for an id. Keys are
        one integer space per server, so a show could otherwise inherit a film's keep."""
        await sync(
            engine,
            self._collection(_Held(None, title="Home video", rating_key=77)),
            kind=ListKind.WHITELIST,
        )

        index = await load_membership_index(engine)
        assert not index.lookup(media_type="tv", plex_rating_keys=(77,))

    async def test_an_item_carrying_no_keys_at_all_is_still_protected_by_nothing(
        self, engine: AsyncEngine
    ) -> None:
        """The lookup's short circuit: no ids and no keys can never mean "everything"."""
        await sync(
            engine,
            self._collection(_Held("tt0000001", title="First", rating_key=11)),
            kind=ListKind.WHITELIST,
        )

        index = await load_membership_index(engine)
        assert not index.lookup(media_type="movie")


class TestARowIsNeverFiledUnderAKindItsIdsDoNotBelongTo:
    """``media_type`` is half of every lookup's join key, so a row filed under the wrong
    kind matches nothing while looking healthy in the table, or matches an unrelated
    title, since TMDb numbers movies and shows in separate spaces."""

    async def test_a_stored_row_under_the_wrong_kind_protects_nothing(
        self, engine: AsyncEngine
    ) -> None:
        """The consequence, pinned at the read side: this is why no write path may
        produce one."""
        await sync(
            engine,
            _StaticProvider([ListItem(media_type="tv", imdb_id="tt0000001", title="A film")]),
        )

        index = await load_membership_index(engine)
        assert not index.lookup(media_type="movie", imdb_id="tt0000001")
        assert index.lookup(media_type="tv", imdb_id="tt0000001")

    async def test_a_plex_object_that_is_neither_a_movie_nor_a_show_is_not_stored(
        self, engine: AsyncEngine
    ) -> None:
        """A collection is typed by its library, but the mapping this replaced filed
        every non-show object as a *movie*. An episode carries episode ids, so the row
        landed in the movie id space and could hand its keep to whichever film shared
        the number."""
        collection = PlexCollection(
            server=_FakePlexServer(
                {
                    "Movies": [
                        _Held("tt0000001", title="A film"),
                        _Held("tt0000002", title="An episode", type="episode"),
                    ]
                }
            ),
            section_name="Movies",
        )

        assert await sync(engine, collection, kind=ListKind.WHITELIST) == 1

        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", imdb_id="tt0000001")
        assert not index.lookup(media_type="movie", imdb_id="tt0000002")
        assert not index.lookup(media_type="tv", imdb_id="tt0000002")

    async def test_a_collection_of_nothing_but_those_never_wipes_the_list(
        self, engine: AsyncEngine
    ) -> None:
        """Unusable for want of a kind is unusable for want of an id. A populated
        container none of whose entries can be stored is refused, so the stored
        membership survives it."""
        good = PlexCollection(
            server=_FakePlexServer({"Movies": ["tt0000001"]}), section_name="Movies"
        )
        assert await sync(engine, good, kind=ListKind.WHITELIST) == 1

        episodes = PlexCollection(
            server=_FakePlexServer(
                {"Movies": [_Held("tt0000009", title="An episode", type="episode")]}
            ),
            section_name="Movies",
        )
        with pytest.raises(ContainerMissingError):
            await sync(engine, episodes, kind=ListKind.WHITELIST)

        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", imdb_id="tt0000001")

    async def test_a_show_in_the_collection_is_still_stored_as_one(
        self, engine: AsyncEngine
    ) -> None:
        """The mapping still has to place the two kinds it does know."""
        collection = PlexCollection(
            server=_FakePlexServer(
                {
                    "Shows": [
                        _Held("tt0000001", title="A show", type="show"),
                        _Held("tt0000002", title="A film", type="movie"),
                    ]
                }
            ),
            section_name="Shows",
        )

        assert await sync(engine, collection, kind=ListKind.WHITELIST) == 2

        index = await load_membership_index(engine)
        assert index.lookup(media_type="tv", imdb_id="tt0000001")
        assert index.lookup(media_type="movie", imdb_id="tt0000002")


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))
    yield eng
    await eng.dispose()


#: ``protection_list_item`` as it shipped, before it carried a Plex key. What an operator
#: who has been running Reaper actually has on disk.
_SCHEMA_BEFORE_THE_PLEX_KEY = """
CREATE TABLE protection_list_item (
    slug       TEXT    NOT NULL,
    media_type TEXT    NOT NULL,
    imdb_id    TEXT,
    tmdb_id    INTEGER,
    tvdb_id    INTEGER,
    title      TEXT,
    rank       INTEGER,
    PRIMARY KEY (slug, media_type, imdb_id, tmdb_id, tvdb_id)
)
"""


class TestAStoredCacheIsWidenedNeverRebuilt:
    """``CREATE TABLE IF NOT EXISTS`` leaves a table that exists exactly as it is, so a
    cache written before the Plex key would never get the column. Dropping the table to
    get it would empty every keep list until the next successful sync refilled it, and a
    scan in that window reaps titles the operator's list protects."""

    async def test_the_column_arrives_and_the_membership_survives(
        self, engine: AsyncEngine
    ) -> None:
        async with engine.begin() as conn:
            await conn.execute(text(_SCHEMA_BEFORE_THE_PLEX_KEY))
            await conn.execute(
                text(
                    "INSERT INTO protection_list_item (slug, media_type, imdb_id, title) "
                    "VALUES ('keep', 'movie', 'tt0000001', 'First')"
                )
            )

        await ensure_schema(engine)

        async with engine.connect() as conn:
            columns = {
                str(row.name)
                for row in (
                    await conn.execute(text("PRAGMA table_info(protection_list_item)"))
                ).all()
            }
            rows = (await conn.execute(text("SELECT title FROM protection_list_item"))).all()
        assert "plex_rating_key" in columns
        assert [str(r.title) for r in rows] == ["First"]

    async def test_two_callers_widening_at_once_leave_one_of_each_column(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The PRAGMA guarding each ``ALTER`` is not inside a transaction. pysqlite
        autocommits DDL, so ``engine.begin()`` opens nothing around it, and SQLite has no
        ``ADD COLUMN IF NOT EXISTS``. Without ``_widen_lock`` both callers read the
        pre-widen shape and the second raises ``duplicate column name``, which aborts a scan.

        Two callers overlap on an ordinary install. The Lists screen calls ``configured``
        while a scan calls ``load_membership_index``, and the nightly ``refresh_curated_lists``
        calls ``sync``. Three job ids, so APScheduler's ``max_instances`` does not separate
        them.

        The interleave is probabilistic, and this test says so rather than reading as a
        proof. Every shape read yields once, which widens the window. Measured against the
        unlocked function, which raises in about a third of rounds, so it runs twelve
        rounds against twelve fresh databases and fails on any one of them. With the lock
        it cannot fail at all. The second caller reads the widened shape and skips.
        """
        real_execute = AsyncConnection.execute

        async def yielding_execute(
            self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
        ) -> Any:
            result = await real_execute(self, statement, *args, **kwargs)
            if "PRAGMA table_info(" in str(statement):
                await asyncio.sleep(0)
            return result

        monkeypatch.setattr(AsyncConnection, "execute", yielding_execute)

        for round_number in range(12):
            data_dir = tmp_path / f"round-{round_number}"
            engine = create_engine(Settings(data_dir=data_dir, secret_key="k"))
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(_SCHEMA_BEFORE_THE_PLEX_KEY))

                await asyncio.gather(ensure_schema(engine), ensure_schema(engine))

                async with engine.connect() as conn:
                    columns = [
                        str(row.name)
                        for row in (
                            await conn.execute(text("PRAGMA table_info(protection_list_item)"))
                        ).all()
                    ]
                assert columns.count("plex_rating_key") == 1, round_number
            finally:
                await engine.dispose()

    async def test_widening_twice_is_the_same_as_widening_once(self, engine: AsyncEngine) -> None:
        """It runs on every call, several times per scan."""
        async with engine.begin() as conn:
            await conn.execute(text(_SCHEMA_BEFORE_THE_PLEX_KEY))

        await ensure_schema(engine)
        await ensure_schema(engine)

        provider = _StaticProvider([ListItem(media_type="movie", plex_rating_key=5, title="A")])
        assert await sync(engine, provider) == 1


def _top250_payload(count: int = 250) -> list[dict[str, object]]:
    """Ids from 1, never 0. ``tt0000000`` is the "unknown" sentinel ``identity._clean_imdb``
    drops, so an entry numbered from zero is stored under no imdb id at all."""
    return [
        {"ImdbId": f"tt{i:07d}", "TmdbId": 1000 + i, "Title": f"Film {i}", "Year": 1920 + i}
        for i in range(1, count + 1)
    ]


class TestImdbList:
    async def test_a_mirror_redirecting_to_a_cdn_still_fetches(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """The public fetchers carry no credentials, so a cross-origin CDN hop is safe
        and must be followed, unlike the credentialed clients, which refuse it."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(
                302, headers={"location": "https://cdn.example.test/top250.json"}
            )
        )
        httpx2_mock.get("https://cdn.example.test/top250.json").mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        assert await sync(engine, ImdbList()) == 250

    async def test_it_fetches_and_stores(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )

        count = await sync(engine, ImdbList(), mode=ListMode.HARD)

        assert count == 250

    async def test_the_popular_preset_fetches_its_own_chart_path(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        route = httpx2_mock.get(IMDB_LIST_BASE + "popular").mock(
            return_value=httpx.Response(200, json=_top250_payload(count=60))
        )

        assert await sync(engine, ImdbList(variant="popular")) == 60
        assert route.called

    async def test_a_custom_list_id_is_appended_to_the_mirror_path(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        route = httpx2_mock.get(IMDB_LIST_BASE + "ls005421403").mock(
            return_value=httpx.Response(200, json=_top250_payload(count=3))
        )

        assert await sync(engine, ImdbList(variant="ls005421403")) == 3
        assert route.called

    def test_the_slug_carries_the_variant_and_the_definition(self) -> None:
        """The variant tells two IMDb lists apart, and the definition id is what lets a
        rename keep its stored membership (``list_suffix``). The no-definition spelling is
        what a one-off refresh writes."""
        assert ImdbList().slug == "imdb-top250"
        assert ImdbList(variant="popular", list_id=3).slug == "imdb-popular-list3"
        assert ImdbList(variant="ls005421403", list_id=9).slug == "imdb-ls005421403-list9"

    async def test_membership_is_binary_because_the_source_has_no_rank(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """The payload carries *no* rank field, and the entries come back in roughly
        chronological order, the first is The Kid (1921).

        Taking the array index as a chart position would tell the owner "The Kid is #1
        on the IMDb Top 250", which is false. The why-panel would be confidently lying.
        So membership is binary and rank stays None."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync(engine, ImdbList(list_name="IMDb Top 250"))

        found = await memberships(engine, media_type="movie", imdb_id="tt0000001")

        assert len(found) == 1
        assert found[0].rank is None
        assert found[0].describe() == "IMDb Top 250"  # no fabricated "#1"

    async def test_an_unknown_id_sentinel_is_stored_under_no_imdb_id(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """The mirror emits ``tt0000000`` for a chart entry it has no IMDb id for.

        Stored raw, that row answers every *other* item whose own imdbId is the same
        sentinel, and the why-panel then names a list the film is not on. The entry keeps
        protecting by its tmdb id, which is the id it is actually identified by.
        """
        payload = _top250_payload()
        payload[0]["ImdbId"] = "tt0000000"
        httpx2_mock.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(200, json=payload))

        await sync(engine, ImdbList(list_name="IMDb Top 250"))

        assert await memberships(engine, media_type="movie", imdb_id="tt0000000") == []
        assert len(await memberships(engine, media_type="movie", tmdb_id=1001)) == 1

    @pytest.mark.parametrize(
        ("variant", "floor"),
        [("top250", 200), ("popular", 20)],
        ids=["top250-floor-200", "popular-floor-20"],
    )
    async def test_each_preset_refuses_a_payload_under_its_own_floor(
        self, engine: AsyncEngine, httpx2_mock: respx.Router, variant: str, floor: int
    ) -> None:
        """A chart of known size that comes back much smaller is a broken mirror, and
        installing it would silently stop protecting the titles that fell off. The floor
        is per preset, so each is pinned one entry either side of its own."""
        url = IMDB_LIST_BASE + variant
        httpx2_mock.get(url).mock(
            return_value=httpx.Response(200, json=_top250_payload(count=floor - 1))
        )
        with pytest.raises(IntegrationError, match="too short to trust"):
            await sync(engine, ImdbList(variant=variant))

        httpx2_mock.get(url).mock(
            return_value=httpx.Response(200, json=_top250_payload(count=floor))
        )
        assert await sync(engine, ImdbList(variant=variant)) == floor

    async def test_a_custom_list_refuses_only_an_empty_payload(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """A custom list has no known size, so its floor is 1: a single-entry list is the
        operator's to keep, and only an empty answer reads as a broken mirror."""
        url = IMDB_LIST_BASE + "ls005421403"
        httpx2_mock.get(url).mock(return_value=httpx.Response(200, json=[]))
        with pytest.raises(IntegrationError, match="too short to trust"):
            await sync(engine, ImdbList(variant="ls005421403"))

        httpx2_mock.get(url).mock(return_value=httpx.Response(200, json=_top250_payload(count=1)))
        assert await sync(engine, ImdbList(variant="ls005421403")) == 1

    async def test_a_failed_fetch_leaves_the_previous_list_intact(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """The atomic swap is there so a protection never silently empties itself
        because a third-party service had a bad minute.

        Both of these name the error the sync really raises. A bare
        ``pytest.raises(Exception)`` is satisfied by one raised *before* the swap is
        reached, so the assertions below would be covering a path that never ran, on the
        one surface where an empty result is the failure being guarded.
        """
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync(engine, ImdbList())

        httpx2_mock.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(IntegrationError, match="503"):
            await sync(engine, ImdbList())

        # Still protected.
        assert await memberships(engine, media_type="movie", imdb_id="tt0000001")

    async def test_the_error_is_recorded_for_the_settings_screen(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(503))

        with pytest.raises(IntegrationError, match="503"):
            await sync(engine, ImdbList())

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT last_error FROM protection_list WHERE slug = 'imdb-top250'")
                )
            ).one()

        assert row.last_error


class _FakeWatchlistServer:
    """A Plex stand-in whose account watchlist is a fixed set of entries, or raises."""

    def __init__(self, entries: list[object] | None = None, *, broken: bool = False) -> None:
        self._entries = entries or []
        self._broken = broken

    def myPlexAccount(self) -> object:  # noqa: N802 - mirrors plexapi
        from types import SimpleNamespace

        if self._broken:
            raise RuntimeError("plex.tv did not answer")
        return SimpleNamespace(watchlist=lambda: list(self._entries))


def _watchlist_entry(
    *, imdb: str | None = None, tmdb: int | None = None, kind: str = "movie", title: str = "A"
) -> object:
    from types import SimpleNamespace

    guids = []
    if imdb:
        guids.append(SimpleNamespace(id=f"imdb://{imdb}"))
    if tmdb:
        guids.append(SimpleNamespace(id=f"tmdb://{tmdb}"))
    # The legacy guid on discover metadata is a plex:// uri, which parse_guids ignores.
    return SimpleNamespace(type=kind, title=title, guid="plex://movie/abc", guids=guids)


class TestPlexWatchlist:
    async def test_entries_are_parsed_through_the_shared_guid_parser(self) -> None:
        """Both Guid children land, the plex:// legacy guid is ignored, and a show files
        under tv, the same one parser the scan's matcher uses."""
        provider = PlexWatchlist(
            server=_FakeWatchlistServer(
                [
                    _watchlist_entry(imdb="tt0000001", tmdb=550, title="A film"),
                    _watchlist_entry(imdb="tt0000002", kind="show", title="A show"),
                ]
            )
        )

        items = await provider.fetch()

        assert items[0].imdb_id == "tt0000001"
        assert items[0].tmdb_id == 550
        assert items[0].media_type == "movie"
        assert items[1].media_type == "tv"

    def test_the_slug_carries_the_definition(self) -> None:
        assert PlexWatchlist(server=object(), list_id=4).slug == "plex-watchlist-account-list4"

    async def test_an_empty_watchlist_is_genuinely_empty(self, engine: AsyncEngine) -> None:
        """The operator cleared it from the Plex app, so it empties the stored membership.
        A watchlist has no missing-container state. Only a raising read keeps the copy."""
        good = PlexWatchlist(server=_FakeWatchlistServer([_watchlist_entry(imdb="tt0000001")]))
        assert await sync(engine, good, kind=ListKind.WHITELIST) == 1

        empty = PlexWatchlist(server=_FakeWatchlistServer([]))
        assert await sync(engine, empty, kind=ListKind.WHITELIST) == 0

        index = await load_membership_index(engine)
        assert not index.lookup(media_type="movie", imdb_id="tt0000001")

    async def test_a_failed_read_records_the_error_and_keeps_the_membership(
        self, engine: AsyncEngine
    ) -> None:
        """Any failure to read plex.tv raises and leaves the stored copy protecting, the
        atomic-swap guarantee, plus the error the Lists screen shows."""
        good = PlexWatchlist(server=_FakeWatchlistServer([_watchlist_entry(imdb="tt0000001")]))
        assert await sync(engine, good, kind=ListKind.WHITELIST) == 1

        broken = PlexWatchlist(server=_FakeWatchlistServer(broken=True))
        with pytest.raises(RuntimeError, match="did not answer"):
            await sync(engine, broken, kind=ListKind.WHITELIST)

        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", imdb_id="tt0000001")
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT last_error FROM protection_list WHERE slug = :slug"),
                    {"slug": broken.slug},
                )
            ).one()
        assert "did not answer" in str(row.last_error)


class TestMatching:
    """An item is protected if *any* of its external ids matches."""

    async def test_matched_by_tmdb_when_imdb_is_missing(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """Requiring both ids would silently drop every item where only one is present."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync(engine, ImdbList())

        assert await memberships(engine, media_type="movie", tmdb_id=1005)

    async def test_an_unmatched_item_is_not_protected(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync(engine, ImdbList())

        assert await memberships(engine, media_type="movie", imdb_id="tt9999999") == []

    async def test_an_item_with_no_ids_at_all_is_not_protected(self, engine: AsyncEngine) -> None:
        """And does not blow up. An item Plex has not matched has no ids."""
        assert await memberships(engine, media_type="movie") == []


class TestAShowIsNeverMatchedAgainstAMovieList:
    """TMDb numbers movies and shows in *separate* id spaces. Movie #1005 and show #1005
    are unrelated titles. The join key is therefore (media_type, id), not id alone. Without
    that, a show whose TMDb id happens to equal a Top 250 film's is reported "on the IMDb
    Top 250", kept for a reason its owner never gave, and the why-panel says something
    false. A live instance showed exactly this on TV shows."""

    async def test_a_show_sharing_a_top250_films_tmdb_id_is_not_protected(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync(engine, ImdbList())

        # Film 5 is stored as a *movie* with TMDb id 1005.
        assert await memberships(engine, media_type="movie", tmdb_id=1005)  # the film is on it
        assert await memberships(engine, media_type="tv", tmdb_id=1005) == []  # a show is not

        # And the in-memory index the scan actually uses agrees with the query.
        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", tmdb_id=1005)
        assert index.lookup(media_type="tv", tmdb_id=1005) == []


class TestMembershipIndexParity:
    """The scan answers "which lists contain this item?" from an in-memory index loaded
    once per run. The index must agree with :func:`memberships`, the per-item SQL it
    replaced, on every lookup, or the two paths could protect different items."""

    async def test_the_index_answers_exactly_like_the_query(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync(engine, ImdbList())
        # A second, whitelist-kind list overlapping one title, so an item can belong to
        # two lists at once and the parity check covers the multi-row answer.
        await sync(
            engine,
            _StaticProvider(
                [
                    ListItem(media_type="movie", imdb_id="tt0000005", title="Overlap"),
                    ListItem(media_type="tv", tvdb_id=777, title="A show"),
                ]
            ),
            mode=ListMode.HARD,
            kind=ListKind.WHITELIST,
        )

        index = await load_membership_index(engine)
        probes: list[dict[str, object]] = [
            {"media_type": "movie", "imdb_id": "tt0000005"},  # on both lists
            {"media_type": "movie", "imdb_id": "tt0000001"},  # top-250 only
            {"media_type": "movie", "tmdb_id": 1005},  # matched through the other id
            {"media_type": "movie", "imdb_id": "tt0000005", "tmdb_id": 1005},  # one row, both ways
            {"media_type": "tv", "tvdb_id": 777},  # tv, whitelist only
            {"media_type": "tv", "tmdb_id": 1005},  # a show colliding with a film: no match
            {"media_type": "movie", "imdb_id": "tt9999999"},  # on nothing
            {"media_type": "movie"},  # no ids at all
        ]
        for probe in probes:
            expected = await memberships(engine, **probe)  # type: ignore[arg-type]
            assert sorted(index.lookup(**probe), key=lambda m: m.slug) == sorted(  # type: ignore[arg-type]
                expected, key=lambda m: m.slug
            ), probe

    async def test_a_disabled_list_drops_out_of_the_index(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync(engine, ImdbList())
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE protection_list SET enabled = 0"))

        index = await load_membership_index(engine)

        assert index.lookup(media_type="movie", imdb_id="tt0000001") == []
        assert await memberships(engine, media_type="movie", imdb_id="tt0000001") == []


class _StaticProvider:
    slug = "static-keep"
    display_name = "Static keep"
    # A row the operator never named, so its keep rule matches the display name. The
    # registry-defined providers carry a name here and are covered in TestTheNameAKeepRuleMatches.
    list_name = None

    def __init__(self, items: list[ListItem]) -> None:
        self._items = items

    async def fetch(self) -> list[ListItem]:
        return self._items


class TestListItem:
    def test_an_item_with_no_ids_is_unusable(self) -> None:
        """It cannot be matched to anything, so it is dropped at sync time rather than
        silently bloating the table."""
        assert ListItem(media_type="movie").has_any_id is False

    def test_any_single_id_is_enough(self) -> None:
        assert ListItem(media_type="movie", tmdb_id=550).has_any_id is True
