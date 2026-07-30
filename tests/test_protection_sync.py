# SPDX-License-Identifier: AGPL-3.0-or-later
"""Protection lists must be populated BEFORE a scan reads them.

The bug this closes: the list providers and the membership tables always existed, but
nothing synced them at scan time. So the "Never Reap" collection, the reaper-keep tag
and the IMDb Top 250 were silently empty, and an empty whitelist is a whitelist that
does not protect -- a protection failing *open*, which is the worst direction.

These prove the orchestrator populates what a scan then reads, and that one failing
source neither empties the others nor aborts the scan.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.config import Settings
from reaper.db.session import create_engine
from reaper.services import history_sync
from reaper.services.lists import IMDB_TOP_250_URL, memberships
from reaper.services.season_scan import SonarrSource
from reaper.services.snapshot import _watch_stats, sync_protection_lists

pytestmark = pytest.mark.httpx2(assert_all_called=False)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))  # type: ignore[call-arg]
    yield eng
    await eng.dispose()


def _top250_payload(count: int = 250) -> list[dict[str, object]]:
    return [
        {"ImdbId": f"tt{i:07d}", "TmdbId": 1000 + i, "Title": f"Film {i}"} for i in range(count)
    ]


class TestTheTop250IsPopulatedForAScan:
    async def test_after_sync_a_top250_film_is_a_member(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """The end-to-end point: sync, then the membership a scan looks up is present.
        Before this wiring, that lookup always came back empty."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )

        synced = await sync_protection_lists(engine, include_top_250=True)

        assert synced["imdb-top-250"] == 250
        found = await memberships(engine, media_type="movie", imdb_id="tt0000005")
        assert len(found) == 1  # a scan would now see this film as protected

    async def test_it_can_be_skipped(self, engine: AsyncEngine, httpx2_mock: respx.Router) -> None:
        synced = await sync_protection_lists(engine, include_top_250=False)
        assert "imdb-top-250" not in synced


class TestAnEmptyCacheDoesNotCrashTheScan:
    """The cache database is rebuildable and can be empty on a fresh install. Reading it
    before it has ever been synced must degrade gracefully -- 'no history yet' -- never crash
    with 'no such table' a hundred frames deep in a scan. Found by clearing the cache and
    scanning.

    Reading no plays is not itself the protection: an empty mirror resolves the horizon to
    `utcnow()`, so an item with an arrival date reads Known ZERO days dormant. What holds is
    `snapshot.scan` degrading the snapshot un-plannably on that mirror."""

    async def test_watch_stats_on_a_never_synced_cache_returns_empty(
        self, engine: AsyncEngine
    ) -> None:
        # The table has never been created. This used to raise OperationalError.
        last, window, all_time = await _watch_stats(engine, rating_keys={1, 2, 3}, window_days=365)
        assert last == {} and window == {} and all_time == {}

    async def test_horizon_on_a_never_synced_cache_is_none_not_an_error(
        self, engine: AsyncEngine
    ) -> None:
        assert await history_sync.horizon(engine) is None


class TestOneFailingListDoesNotSinkTheScan:
    async def test_a_failed_fetch_is_recorded_not_raised(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """A protection source that errors must not abort the scan -- but the caller has
        to be able to SEE it failed, so the scan can treat itself as degraded rather than
        delete something the list would have saved."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(return_value=httpx.Response(503))

        synced = await sync_protection_lists(engine, include_top_250=True)

        assert isinstance(synced["imdb-top-250"], str)
        assert "error" in synced["imdb-top-250"]

    async def test_a_truncated_list_is_refused(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """A short list would silently stop protecting the films that fell off it, so
        the provider refuses it -- and the orchestrator records that refusal rather than
        installing a half-empty whitelist."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload(count=50))
        )

        synced = await sync_protection_lists(engine, include_top_250=True)

        assert isinstance(synced["imdb-top-250"], str)
        assert "error" in synced["imdb-top-250"]


class _TaggedSonarr:
    """A Sonarr stand-in carrying exactly what the keep-tag rule reads."""

    service = "sonarr"

    def __init__(self, tags: list[dict[str, object]], series: list[dict[str, object]]) -> None:
        self._tags = tags
        self._series = series

    async def tags(self) -> list[dict[str, object]]:
        return self._tags

    async def series(self) -> list[dict[str, object]]:
        return self._series


class _CollectionServer:
    """A Plex stand-in holding one library with one collection, whatever it is called.

    ``imdb`` is the single title on that collection, so a rename can be told apart from
    the same list under a new name.
    """

    class _Item:
        type = "movie"
        title = "A title"
        guid = None

        def __init__(self, imdb: str) -> None:
            from types import SimpleNamespace

            self.guids = [SimpleNamespace(id=f"imdb://{imdb}")]

    class _Collection:
        def __init__(self, imdb: str) -> None:
            self._imdb = imdb

        def items(self) -> list[object]:
            return [_CollectionServer._Item(self._imdb)]

    class _Section:
        title = "Movies"

        def __init__(self, imdb: str) -> None:
            self._imdb = imdb

        def collection(self, name: str) -> object:
            return _CollectionServer._Collection(self._imdb)

    class _Library:
        def __init__(self, imdb: str) -> None:
            self._imdb = imdb

        def sections(self) -> list[object]:
            return [_CollectionServer._Section(self._imdb)]

    def __init__(self, imdb: str = "tt0000001") -> None:
        self.library = self._Library(imdb)


class TestEachInstanceKeepsItsOwnKeepList:
    async def test_two_instances_of_one_service_both_protect(self, engine: AsyncEngine) -> None:
        """Two Sonarr instances, each with its own keep-tagged title. The slug carries
        the instance id, so each instance syncs its OWN list. With a shared slug (the
        old shape), each sync atomically replaced the other's membership: whichever ran
        last erased the other instance's keep-tagged titles from the whitelist, silently
        -- a protection failing open, in whichever order the syncs happened to finish."""
        first = SonarrSource(
            client=_TaggedSonarr(
                [{"id": 1, "label": "keep"}],
                [{"title": "A", "tvdbId": 10, "tags": [1]}],
            ),
            instance_id=1,
            name="hd",
        )
        second = SonarrSource(
            client=_TaggedSonarr(
                [{"id": 9, "label": "keep"}],
                [{"title": "B", "tvdbId": 20, "tags": [9]}],
            ),
            instance_id=2,
            name="uhd",
        )

        synced = await sync_protection_lists(
            engine, sonarrs=[first, second], tv_keep_tags=("keep",), include_top_250=False
        )

        # Two distinct lists, so neither sync can mask the other's outcome either.
        assert synced["sonarr-1-keeptags-any"] == 1
        assert synced["sonarr-2-keeptags-any"] == 1
        # And BOTH instances' keep-tagged titles are protected at the same time.
        assert await memberships(engine, media_type="tv", tvdb_id=10)
        assert await memberships(engine, media_type="tv", tvdb_id=20)


class TestAReplacedKeepListStopsProtecting:
    """A stored list outlives the setting that created it: the slug carries the any/all
    match, the instance id and the collection name, so changing any of them writes a NEW
    list and leaves the old one enabled. Everything the old rule ever matched stayed
    whitelisted forever, which means the tightening the operator saved never took effect
    and the why-panel cited a keep rule that no longer exists."""

    @staticmethod
    def _sonarr() -> SonarrSource:
        return SonarrSource(
            client=_TaggedSonarr(
                [{"id": 1, "label": "keep"}, {"id": 2, "label": "gold"}],
                [
                    {"title": "A", "tvdbId": 10, "tags": [1]},  # keep only
                    {"title": "B", "tvdbId": 11, "tags": [1, 2]},  # both
                ],
            ),
            instance_id=1,
            name="hd",
        )

    async def test_flipping_any_to_all_stops_protecting_the_any_matches(
        self, engine: AsyncEngine
    ) -> None:
        await sync_protection_lists(
            engine,
            sonarrs=[self._sonarr()],
            tv_keep_tags=("keep", "gold"),
            tv_keep_match="any",
            include_top_250=False,
        )
        assert await memberships(engine, media_type="tv", tvdb_id=10)

        synced = await sync_protection_lists(
            engine,
            sonarrs=[self._sonarr()],
            tv_keep_tags=("keep", "gold"),
            tv_keep_match="all",
            include_top_250=False,
        )

        assert synced["sonarr-1-keeptags-any"] == "retired"
        assert not await memberships(engine, media_type="tv", tvdb_id=10)  # only "keep"
        assert await memberships(engine, media_type="tv", tvdb_id=11)  # both tags

    async def test_flipping_back_protects_again(self, engine: AsyncEngine) -> None:
        """Retired, never deleted: the membership stays, and the list resumes the moment
        the configuration produces it again. A row left disabled while its sync succeeds
        would be a keep-list that protects nothing."""
        for match in ("any", "all", "any"):
            await sync_protection_lists(
                engine,
                sonarrs=[self._sonarr()],
                tv_keep_tags=("keep", "gold"),
                tv_keep_match=match,
                include_top_250=False,
            )

        assert await memberships(engine, media_type="tv", tvdb_id=10)

    async def test_clearing_the_tags_retires_the_whole_keep_list(self, engine: AsyncEngine) -> None:
        """The stronger trigger: with no tags configured no provider is built at all, so
        without the retire pass nothing touches the stored list and every title it ever
        matched stays protected by a rule the operator deleted."""
        await sync_protection_lists(
            engine, sonarrs=[self._sonarr()], tv_keep_tags=("keep",), include_top_250=False
        )
        assert await memberships(engine, media_type="tv", tvdb_id=10)

        await sync_protection_lists(
            engine, sonarrs=[self._sonarr()], tv_keep_tags=(), include_top_250=False
        )

        assert not await memberships(engine, media_type="tv", tvdb_id=10)

    async def test_a_failed_sync_is_never_retired(self, engine: AsyncEngine) -> None:
        """The stale copy is still the right list, just unrefreshed, and the atomic swap
        kept it deliberately. Retiring it here would unprotect every title on it because
        one *arr was briefly unreachable."""

        class _Unreachable(_TaggedSonarr):
            async def tags(self) -> list[dict[str, object]]:
                raise RuntimeError("connection refused")

        await sync_protection_lists(
            engine, sonarrs=[self._sonarr()], tv_keep_tags=("keep",), include_top_250=False
        )

        broken = SonarrSource(client=_Unreachable([], []), instance_id=1, name="hd")
        synced = await sync_protection_lists(
            engine, sonarrs=[broken], tv_keep_tags=("keep",), include_top_250=False
        )

        assert "error" in str(synced["sonarr-1-keeptags-any"])
        assert await memberships(engine, media_type="tv", tvdb_id=10)

    async def test_renaming_the_plex_collection_retires_the_old_one(
        self, engine: AsyncEngine
    ) -> None:
        """The collection name is in the slug too, so a rename leaves the old collection's
        membership protecting titles the operator has since taken off the new one."""
        await sync_protection_lists(
            engine,
            plex_server=_CollectionServer(),
            collection_name="Never Reap",
            include_top_250=False,
        )
        assert await memberships(engine, media_type="movie", imdb_id="tt0000001")

        synced = await sync_protection_lists(
            engine,
            plex_server=_CollectionServer("tt0000002"),
            collection_name="Keep Forever",
            include_top_250=False,
        )

        assert synced["plex-collection-never-reap"] == "retired"
        assert not await memberships(engine, media_type="movie", imdb_id="tt0000001")

    async def test_an_unreachable_plex_never_retires_the_collection(
        self, engine: AsyncEngine
    ) -> None:
        """With no server there is no provider and no slug, and retiring on that would
        unprotect every title on the "Never Reap" collection over a network blip. The
        scan already degrades for the same reason; the stored list must survive it."""
        await sync_protection_lists(engine, plex_server=_CollectionServer(), include_top_250=False)

        await sync_protection_lists(engine, plex_server=None, include_top_250=False)

        assert await memberships(engine, media_type="movie", imdb_id="tt0000001")
