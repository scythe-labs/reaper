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
    ImdbTop250,
    ListItem,
    ListMode,
    memberships,
    sync,
)


class _FakeSonarr:
    """A Sonarr stand-in for the tag-rule test: not a RadarrClient, so ArrTag takes the
    series path. Carries just the two methods the tag fetch touches."""

    service = "sonarr"

    def __init__(
        self, tags: list[dict[str, object]], series: list[dict[str, object]]
    ) -> None:
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
    @respx.mock
    async def test_it_fetches_and_stores(self, engine: AsyncEngine) -> None:
        respx.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(200, json=_top250_payload()))

        count = await sync(engine, ImdbTop250(), mode=ListMode.HARD)

        assert count == 250

    @respx.mock
    async def test_membership_is_binary_because_the_source_has_no_rank(
        self, engine: AsyncEngine
    ) -> None:
        """The payload carries NO rank field, and the entries come back in roughly
        CHRONOLOGICAL order -- the first is The Kid (1921).

        Taking the array index as a chart position would tell the owner "The Kid is #1
        on the IMDb Top 250", which is false. The why-panel would be confidently lying.
        So membership is binary and rank stays None."""
        respx.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(200, json=_top250_payload()))
        await sync(engine, ImdbTop250())

        found = await memberships(engine, imdb_id="tt0000000")

        assert len(found) == 1
        assert found[0].rank is None
        assert found[0].describe() == "IMDb Top 250"  # no fabricated "#1"

    @respx.mock
    async def test_a_truncated_list_is_refused(self, engine: AsyncEngine) -> None:
        """A protection that silently shrinks is worse than one that is out of date --
        it stops protecting the films that fell off it, and says nothing."""
        respx.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload(count=12))
        )

        with pytest.raises(Exception, match="truncated"):
            await sync(engine, ImdbTop250())

    @respx.mock
    async def test_a_failed_fetch_leaves_the_previous_list_intact(
        self, engine: AsyncEngine
    ) -> None:
        """The whole point of the atomic swap. A protection must never silently empty
        itself because a third-party service had a bad minute."""
        respx.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(200, json=_top250_payload()))
        await sync(engine, ImdbTop250())

        respx.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(Exception):  # noqa: B017
            await sync(engine, ImdbTop250())

        # Still protected.
        assert await memberships(engine, imdb_id="tt0000001")

    @respx.mock
    async def test_the_error_is_recorded_for_the_settings_screen(self, engine: AsyncEngine) -> None:
        respx.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(503))

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

    @respx.mock
    async def test_matched_by_tmdb_when_imdb_is_missing(self, engine: AsyncEngine) -> None:
        """Requiring both ids would silently drop every item where only one is present."""
        respx.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(200, json=_top250_payload()))
        await sync(engine, ImdbTop250())

        assert await memberships(engine, tmdb_id=1005)

    @respx.mock
    async def test_an_unmatched_item_is_not_protected(self, engine: AsyncEngine) -> None:
        respx.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(200, json=_top250_payload()))
        await sync(engine, ImdbTop250())

        assert await memberships(engine, imdb_id="tt9999999") == []

    async def test_an_item_with_no_ids_at_all_is_not_protected(self, engine: AsyncEngine) -> None:
        """And does not blow up. An item Plex has not matched has no ids."""
        assert await memberships(engine) == []


class TestListItem:
    def test_an_item_with_no_ids_is_unusable(self) -> None:
        """It cannot be matched to anything, so it is dropped at sync time rather than
        silently bloating the table."""
        assert ListItem(media_type="movie").has_any_id is False

    def test_any_single_id_is_enough(self) -> None:
        assert ListItem(media_type="movie", tmdb_id=550).has_any_id is True
