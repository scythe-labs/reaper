# SPDX-License-Identifier: AGPL-3.0-or-later
"""Curated lists as protections.

The differentiating feature: no competitor ingests a curated list as a protection
source. "Never reap anything in the IMDb Top 250" is a rule you cannot write in
Maintainerr, Janitorr or Reclaimerr.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

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
        from plexapi.exceptions import NotFound

        class _Section:
            def collection(self, name: str) -> object:
                raise NotFound("gone")

        class _Library:
            def section(self, name: str) -> _Section:
                return _Section()

        class _Server:
            library = _Library()

        provider = PlexCollection(server=_Server(), section_name="Movies")
        with pytest.raises(ContainerMissingError):
            await provider.fetch()


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))  # type: ignore[call-arg]
    yield eng
    await eng.dispose()


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

        with pytest.raises(Exception, match="truncated"):
            await sync(engine, ImdbTop250())

    async def test_a_failed_fetch_leaves_the_previous_list_intact(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """The whole point of the atomic swap. A protection must never silently empty
        itself because a third-party service had a bad minute."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync(engine, ImdbTop250())

        httpx2_mock.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(Exception):  # noqa: B017
            await sync(engine, ImdbTop250())

        # Still protected.
        assert await memberships(engine, media_type="movie", imdb_id="tt0000001")

    async def test_the_error_is_recorded_for_the_settings_screen(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(503))

        with pytest.raises(Exception):  # noqa: B017
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
