# SPDX-License-Identifier: AGPL-3.0-or-later
"""Curated lists as protections.

The differentiating feature: no competitor ingests a curated list as a protection
source. "Never reap anything in the IMDb Top 250" is a rule you cannot write in
Maintainerr, Janitorr or Reclaimerr.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clients.base import IntegrationError
from reaper.config import Settings
from reaper.db.session import create_engine
from reaper.services.lists import (
    IMDB_TOP_250_URL,
    ArrTagRule,
    ContainerMissingError,
    ImdbTop250,
    ListItem,
    ListKind,
    ListMode,
    PlexCollection,
    ensure_schema,
    load_membership_index,
    memberships,
    sync,
)

pytestmark = pytest.mark.httpx2(assert_all_called=False)


class _FakeSonarr:
    """A Sonarr stand-in for the tag-rule test: not a RadarrClient, so ArrTagRule takes
    the series path. Carries just the two methods the tag fetch touches."""

    service = "sonarr"

    def __init__(self, tags: list[dict[str, object]], series: list[dict[str, object]]) -> None:
        self._tags = tags
        self._series = series

    async def tags(self) -> list[dict[str, object]]:
        return self._tags

    async def series(self) -> list[dict[str, object]]:
        return self._series


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
    Every library reports the SAME title, so several entries model the same-title case
    the resolver has to survive.
    """

    def __init__(self, libraries: dict[str, list[str | None | _Held] | None]) -> None:
        self.library = self._Library(libraries)

    class _Library:
        def __init__(self, libraries: dict[str, list[str | None | _Held] | None]) -> None:
            self._libraries = libraries

        def sections(self) -> list[_FakePlexServer._Section]:
            return [
                _FakePlexServer._Section(name.rstrip(" *"), entries)
                for name, entries in self._libraries.items()
            ]

    class _Section:
        def __init__(self, title: str, entries: list[str | None | _Held] | None) -> None:
            self.title = title
            self._entries = entries

        def collection(self, name: str) -> object:
            from plexapi.exceptions import NotFound

            if self._entries is None:
                raise NotFound("no such collection")
            return _FakePlexServer._Collection(self._entries)

    class _Collection:
        def __init__(self, entries: list[str | None | _Held]) -> None:
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
    """The configurable keep-list: several tags, combined ANY (union) or ALL (intersection)."""

    @pytest.fixture
    def sonarr(self) -> _FakeSonarr:
        tags = [{"id": 1, "label": "keep"}, {"id": 2, "label": "gold"}]
        series = [
            {"title": "A", "tvdbId": 10, "tags": [1]},  # keep only
            {"title": "B", "tvdbId": 11, "tags": [1, 2]},  # keep AND gold
            {"title": "C", "tvdbId": 12, "tags": [2]},  # gold only
        ]
        return _FakeSonarr(tags, series)

    async def test_any_is_the_union(self, sonarr: _FakeSonarr) -> None:
        items = await ArrTagRule(sonarr, ("keep", "gold"), "any").fetch()  # type: ignore[arg-type]
        assert {i.title for i in items} == {"A", "B", "C"}

    async def test_all_is_the_intersection(self, sonarr: _FakeSonarr) -> None:
        items = await ArrTagRule(sonarr, ("keep", "gold"), "all").fetch()  # type: ignore[arg-type]
        assert {i.title for i in items} == {"B"}  # only B has both tags

    async def test_a_single_tag_is_just_that_tag(self, sonarr: _FakeSonarr) -> None:
        items = await ArrTagRule(sonarr, ("keep",), "any").fetch()  # type: ignore[arg-type]
        assert {i.title for i in items} == {"A", "B"}

    async def test_no_tags_keeps_nothing(self, sonarr: _FakeSonarr) -> None:
        assert await ArrTagRule(sonarr, (), "any").fetch() == []  # type: ignore[arg-type]

    @pytest.mark.parametrize("configured", ["Reaper-Keep", "REAPER-KEEP", " reaper-keep "])
    async def test_the_operators_capitalization_still_matches(self, configured: str) -> None:
        """Sonarr and Radarr lower-case every label at the source, so an operator who
        configures the natural capitalization of their own tag was looking up a spelling the
        tag map cannot hold. The tag then read as MISSING, and on a first sync that stored an
        empty keep-list and reported success: keep-tagged titles silently deletable, forever,
        with the settings screen showing the list as healthy."""
        sonarr = _FakeSonarr(
            [{"id": 1, "label": "reaper-keep"}],
            [{"title": "A", "tvdbId": 10, "tags": [1]}],
        )

        items = await ArrTagRule(sonarr, (configured,), "any").fetch()  # type: ignore[arg-type]

        assert [i.title for i in items] == ["A"]

    async def test_a_tag_that_really_is_absent_still_raises(self) -> None:
        """The other direction: normalizing must not turn a genuinely missing tag into a
        match, or the wipe protection above stops firing."""
        sonarr = _FakeSonarr([{"id": 1, "label": "other"}], [])

        with pytest.raises(ContainerMissingError):
            await ArrTagRule(sonarr, ("reaper-keep",), "any").fetch()  # type: ignore[arg-type]


class TestAVanishedContainerNeverWipesTheList:
    """A renamed keep tag or a deleted "Never Reap" collection is a missing CONTAINER,
    not an empty membership. With members stored, the sync must fail so the stale list
    keeps protecting; with nothing stored, it is a quiet first sync."""

    async def test_a_vanished_tag_with_stored_members_keeps_the_membership(
        self, engine: AsyncEngine
    ) -> None:
        good = _FakeSonarr(
            [{"id": 1, "label": "keep"}],
            [{"title": "A", "tvdbId": 10, "tags": [1]}],
        )
        rule = ArrTagRule(good, ("keep",), "any")  # type: ignore[arg-type]
        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 1

        renamed = _FakeSonarr([{"id": 1, "label": "hold"}], [])
        stale = ArrTagRule(renamed, ("keep",), "any")  # type: ignore[arg-type]
        with pytest.raises(ContainerMissingError):
            await sync(engine, stale, kind=ListKind.WHITELIST)

        index = await load_membership_index(engine)
        assert index.lookup(media_type="tv", tvdb_id=10)  # the stored membership survived the wipe

    async def test_a_missing_tag_with_nothing_stored_is_an_empty_first_sync(
        self, engine: AsyncEngine
    ) -> None:
        sonarr = _FakeSonarr([{"id": 1, "label": "other"}], [])
        rule = ArrTagRule(sonarr, ("keep",), "any")  # type: ignore[arg-type]
        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 0

    async def test_under_all_one_missing_tag_raises_when_members_are_stored(
        self, engine: AsyncEngine
    ) -> None:
        """Under ALL, one absent tag structurally rules every title out -- which is the
        same wipe wearing a different hat, so it gets the same treatment."""
        both = _FakeSonarr(
            [{"id": 1, "label": "keep"}, {"id": 2, "label": "gold"}],
            [{"title": "B", "tvdbId": 11, "tags": [1, 2]}],
        )
        rule = ArrTagRule(both, ("keep", "gold"), "all")  # type: ignore[arg-type]
        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 1

        one_gone = _FakeSonarr(
            [{"id": 1, "label": "keep"}],
            [{"title": "B", "tvdbId": 11, "tags": [1]}],
        )
        stale = ArrTagRule(one_gone, ("keep", "gold"), "all")  # type: ignore[arg-type]
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
        # The third library is titled "Movies" too; the fake spells them apart only so the
        # dict can hold three, and reports every one of them under the same title.
        items = await provider.fetch()

        assert [i.imdb_id for i in items] == ["tt0000001"]

    async def test_a_library_that_does_not_exist_is_a_hard_failure(self) -> None:
        """Not a missing container but a missing LIBRARY: a name nothing matches is a
        configuration error, reported, never synced as an empty list."""
        provider = PlexCollection(server=_FakePlexServer({}), section_name="Movies")
        with pytest.raises(IntegrationError):
            await provider.fetch()

    async def test_a_populated_collection_with_nothing_readable_never_wipes_the_list(
        self, engine: AsyncEngine
    ) -> None:
        """A container that comes back populated and yields not one usable entry is a
        failure, not an empty list: reading it as empty would wipe the stored membership
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
    """The same loss at a smaller scale: SOME of the entries stop parsing, not all.

    The survivors then look like a complete fetch, so the swap replaces the membership
    with them and everything else silently stops being protected -- the keep tag is still
    on the title and nothing says a word. A title the container still lists keeps the keys
    its row was stored under instead (rule 27).
    """

    @staticmethod
    def _collection(*entries: str | None | _Held) -> PlexCollection:
        return PlexCollection(
            server=_FakePlexServer({"Movies": list(entries)}), section_name="Movies"
        )

    async def test_a_title_whose_guids_stopped_parsing_keeps_its_ids(
        self, engine: AsyncEngine
    ) -> None:
        """The real shape of a Plex agent change: the object is still there, still
        carrying Plex's own key, and only the guids stopped resolving. Storing what came
        back would file it under that key alone, and the movie lane looks a keep list up
        by RADARR's ids, which the agent change never touched. So the row keeps both."""
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
        """The floor of the same guarantee, for an entry Reaper can read nothing from --
        no guids and no key, which is every non-Plex source's shape too."""
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
        """Both sides case-folded (rule 88), or a title Plex re-cased on a re-match is
        carried by nothing and the protection lapses on exactly the sync that renamed it."""
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
        """Which of them is this entry cannot be answered, so it is not guessed (rule 6).
        Both rows are still carried, which keeps every protection that existed."""
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
        protected by nothing: there is nothing to protect it by, and inventing something
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
    def _collection(*entries: str | None | _Held) -> PlexCollection:
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
        """One file listed twice is bound as a group, and the operator put ONE of those
        listings on the list. Every key the item carries is passed, so it does not matter
        which (rule 29)."""
        await sync(
            engine,
            self._collection(_Held(None, title="Home video", rating_key=77)),
            kind=ListKind.WHITELIST,
        )

        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", plex_rating_keys=(41, 77))
        assert not index.lookup(media_type="movie", plex_rating_keys=(41, 42))

    async def test_a_key_never_matches_across_kinds(self, engine: AsyncEngine) -> None:
        """The join key stays (kind, key) for a Plex key exactly as for an id: keys are
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
    kind matches nothing while looking healthy in the table -- or matches an unrelated
    title, since TMDb numbers movies and shows in separate spaces (rule 52)."""

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
        every non-show object as a MOVIE. An episode carries episode ids, so the row
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
        """Unusable for want of a kind is unusable for want of an id: a populated
        container none of whose entries can be stored is refused, so the stored
        membership survives it (rule 27)."""
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
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))  # type: ignore[call-arg]
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

    async def test_widening_twice_is_the_same_as_widening_once(self, engine: AsyncEngine) -> None:
        """It runs on every call, several times per scan."""
        async with engine.begin() as conn:
            await conn.execute(text(_SCHEMA_BEFORE_THE_PLEX_KEY))

        await ensure_schema(engine)
        await ensure_schema(engine)

        provider = _StaticProvider([ListItem(media_type="movie", plex_rating_key=5, title="A")])
        assert await sync(engine, provider) == 1


def _top250_payload(count: int = 250) -> list[dict[str, object]]:
    return [
        {"ImdbId": f"tt{i:07d}", "TmdbId": 1000 + i, "Title": f"Film {i}", "Year": 1920 + i}
        for i in range(count)
    ]


class TestImdbTop250:
    async def test_a_mirror_redirecting_to_a_cdn_still_fetches(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """The public fetchers carry no credentials, so a cross-origin CDN hop is safe
        and must be followed -- unlike the credentialed clients, which refuse it."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(
                302, headers={"location": "https://cdn.example.test/top250.json"}
            )
        )
        httpx2_mock.get("https://cdn.example.test/top250.json").mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        assert await sync(engine, ImdbTop250()) == 250

    async def test_it_fetches_and_stores(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )

        count = await sync(engine, ImdbTop250(), mode=ListMode.HARD)

        assert count == 250

    async def test_membership_is_binary_because_the_source_has_no_rank(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """The payload carries NO rank field, and the entries come back in roughly
        CHRONOLOGICAL order -- the first is The Kid (1921).

        Taking the array index as a chart position would tell the owner "The Kid is #1
        on the IMDb Top 250", which is false. The why-panel would be confidently lying.
        So membership is binary and rank stays None."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync(engine, ImdbTop250())

        found = await memberships(engine, media_type="movie", imdb_id="tt0000000")

        assert len(found) == 1
        assert found[0].rank is None
        assert found[0].describe() == "IMDb Top 250"  # no fabricated "#1"

    async def test_a_truncated_list_is_refused(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """A protection that silently shrinks is worse than one that is out of date --
        it stops protecting the films that fell off it, and says nothing."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload(count=12))
        )

        with pytest.raises(IntegrationError, match="truncated"):
            await sync(engine, ImdbTop250())

    async def test_a_failed_fetch_leaves_the_previous_list_intact(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """The atomic swap is there so a protection never silently empties itself
        because a third-party service had a bad minute.

        Both of these name the error the sync really raises. A bare
        ``pytest.raises(Exception)`` is satisfied by one raised BEFORE the swap is reached
        -- so the assertions below would be covering a path that never ran, on the one
        rule 27 / rule 2 surface where an empty result is the failure being guarded.
        """
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync(engine, ImdbTop250())

        httpx2_mock.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(IntegrationError, match="503"):
            await sync(engine, ImdbTop250())

        # Still protected.
        assert await memberships(engine, media_type="movie", imdb_id="tt0000001")

    async def test_the_error_is_recorded_for_the_settings_screen(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(503))

        with pytest.raises(IntegrationError, match="503"):
            await sync(engine, ImdbTop250())

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT last_error FROM protection_list WHERE slug = 'imdb-top-250'")
                )
            ).one()

        assert row.last_error


class TestMatching:
    """An item is protected if ANY of its external ids matches."""

    async def test_matched_by_tmdb_when_imdb_is_missing(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """Requiring both ids would silently drop every item where only one is present."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync(engine, ImdbTop250())

        assert await memberships(engine, media_type="movie", tmdb_id=1005)

    async def test_an_unmatched_item_is_not_protected(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync(engine, ImdbTop250())

        assert await memberships(engine, media_type="movie", imdb_id="tt9999999") == []

    async def test_an_item_with_no_ids_at_all_is_not_protected(self, engine: AsyncEngine) -> None:
        """And does not blow up. An item Plex has not matched has no ids."""
        assert await memberships(engine, media_type="movie") == []


class TestAShowIsNeverMatchedAgainstAMovieList:
    """TMDb numbers movies and shows in SEPARATE id spaces: movie #1005 and show #1005
    are unrelated titles. The join key is therefore (media_type, id), not id alone. Without
    that, a show whose TMDb id happens to equal a Top 250 film's is reported "on the IMDb
    Top 250", kept for a reason its owner never gave, and the why-panel says something false.
    A live instance showed exactly this on TV shows."""

    async def test_a_show_sharing_a_top250_films_tmdb_id_is_not_protected(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync(engine, ImdbTop250())

        # Film 5 is stored as a MOVIE with TMDb id 1005.
        assert await memberships(engine, media_type="movie", tmdb_id=1005)  # the film is on it
        assert await memberships(engine, media_type="tv", tmdb_id=1005) == []  # a show is not

        # And the in-memory index the scan actually uses agrees with the query.
        index = await load_membership_index(engine)
        assert index.lookup(media_type="movie", tmdb_id=1005)
        assert index.lookup(media_type="tv", tmdb_id=1005) == []


class TestMembershipIndexParity:
    """The scan answers "which lists contain this item?" from an in-memory index loaded
    once per run. The index must agree with :func:`memberships` -- the per-item SQL it
    replaced -- on every lookup, or the two paths could protect different items."""

    async def test_the_index_answers_exactly_like_the_query(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync(engine, ImdbTop250())
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
        await sync(engine, ImdbTop250())
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE protection_list SET enabled = 0"))

        index = await load_membership_index(engine)

        assert index.lookup(media_type="movie", imdb_id="tt0000001") == []
        assert await memberships(engine, media_type="movie", imdb_id="tt0000001") == []


class _StaticProvider:
    slug = "static-keep"
    display_name = "Static keep"

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
