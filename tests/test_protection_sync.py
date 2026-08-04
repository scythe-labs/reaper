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
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.config import Settings
from reaper.db.session import create_engine
from reaper.services import history_sync, lists
from reaper.services.list_config import ListDefinition
from reaper.services.lists import IMDB_TOP_250_URL, ArrTagRule, ListKind, ListSource, memberships
from reaper.services.season_scan import SonarrSource
from reaper.services.snapshot import _watch_stats, sync_protection_lists

pytestmark = pytest.mark.httpx2(assert_all_called=False)

# Every list comes from the registry the operator edits on Settings -> Lists, so the sync is
# handed definitions rather than hardcoded strings. Ids are pinned here because they end up
# in the stored slug: that is what lets one definition own several stored rows (one per
# *arr) and survive being renamed.
IMDB = ListDefinition(
    id=1,
    name="IMDb Top 250",
    source=ListSource.IMDB,
    config={"preset": "top250"},
    enabled=True,
)
IMDB_SLUG = "imdb-top250-list1"


def _plex_list(collection: str = "Never Reap", *, library: str = "Movies") -> ListDefinition:
    return ListDefinition(
        id=2,
        name=collection,
        source=ListSource.PLEX_COLLECTION,
        config={"library": library, "collection": collection},
        enabled=True,
    )


def _tag_list(
    tags: tuple[str, ...] = ("keep",), match: str = "any", *, list_id: int = 3
) -> ListDefinition:
    return ListDefinition(
        id=list_id,
        name="Tagged titles",
        source=ListSource.ARR_TAG,
        config={"tags": list(tags), "match": match},
        enabled=True,
    )


TAG_SLUG = "sonarr-1-keeptags-any-list3"

WATCHLIST = ListDefinition(
    id=4,
    name="My watchlist",
    source=ListSource.PLEX_WATCHLIST,
    config={},
    enabled=True,
)
WATCHLIST_SLUG = "plex-watchlist-account-list4"


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))  # type: ignore[call-arg]
    yield eng
    await eng.dispose()


def _top250_payload(count: int = 250) -> list[dict[str, object]]:
    return [
        {"ImdbId": f"tt{i:07d}", "TmdbId": 1000 + i, "Title": f"Film {i}"} for i in range(count)
    ]


async def _enabled(engine: AsyncEngine, slug: str) -> bool | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT enabled FROM protection_list WHERE slug = :slug"), {"slug": slug}
            )
        ).one_or_none()
    return None if row is None else bool(row.enabled)


class TestTheTop250IsPopulatedForAScan:
    async def test_after_sync_a_top250_film_is_a_member(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """The end-to-end point: sync, then the membership a scan looks up is present.
        Before this wiring, that lookup always came back empty."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )

        synced = await sync_protection_lists(engine, definitions=[IMDB])

        assert synced[IMDB_SLUG] == 250
        found = await memberships(engine, media_type="movie", imdb_id="tt0000005")
        assert len(found) == 1  # a scan would now see this film as protected

    async def test_a_list_switched_off_stops_protecting(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """A disabled definition builds no provider, so its slug is outside the set the
        retire sweep is judged against and the stored membership is disabled. Without the
        sweep the switch would be decoration: the list would go on protecting every title
        it ever matched (rule 25)."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync_protection_lists(engine, definitions=[IMDB])
        assert await memberships(engine, media_type="movie", imdb_id="tt0000005")

        off = ListDefinition(
            id=IMDB.id,
            name=IMDB.name,
            source=IMDB.source,
            config=IMDB.config,
            enabled=False,
        )
        synced = await sync_protection_lists(engine, definitions=[off])

        assert synced[IMDB_SLUG] == "retired"
        assert not await memberships(engine, media_type="movie", imdb_id="tt0000005")


class TestAnUnreadableRegistryBuildsNothingAndRetiresNothing:
    """``definitions`` is three-state, and ``None`` is the one that matters: the registry
    could not be READ, which is not the same fact as an operator having no lists (rule 1,
    rule 93). Retiring on it would disable every list on the install because a table was
    briefly unavailable."""

    async def test_none_leaves_every_stored_list_exactly_as_it_was(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync_protection_lists(engine, definitions=[IMDB])

        synced = await sync_protection_lists(engine, definitions=None)

        assert synced == {}  # nothing built, nothing retired
        assert await _enabled(engine, IMDB_SLUG) is True
        assert await memberships(engine, media_type="movie", imdb_id="tt0000005")

    async def test_an_empty_registry_by_contrast_retires(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """The genuine "no lists" answer does retire -- the contrast that keeps the case
        above from passing for a sweep that never runs at all."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        await sync_protection_lists(engine, definitions=[IMDB])

        synced = await sync_protection_lists(engine, definitions=[])

        assert synced[IMDB_SLUG] == "retired"
        assert not await memberships(engine, media_type="movie", imdb_id="tt0000005")


class TestANarrowedPassNeverRetires:
    async def test_checking_one_list_cannot_switch_another_off(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """``only=`` produces one list's slugs, and a sweep reading that as the whole truth
        about a family would disable every other list in it -- including, as here, a list
        the registry no longer carries at all."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload())
        )
        popular = ListDefinition(
            id=5,
            name="Popular",
            source=ListSource.IMDB,
            config={"preset": "popular"},
            enabled=True,
        )
        httpx2_mock.get(lists.IMDB_LIST_BASE + "popular").mock(
            return_value=httpx.Response(200, json=_top250_payload(count=60))
        )
        await sync_protection_lists(engine, definitions=[IMDB, popular])
        assert await memberships(engine, media_type="movie", imdb_id="tt0000005")

        # The narrowed pass is handed a registry that has already dropped the Top 250.
        synced = await sync_protection_lists(engine, definitions=[popular], only=popular.id)

        assert synced == {"imdb-popular-list5": 60}
        assert await _enabled(engine, IMDB_SLUG) is True
        assert await memberships(engine, media_type="movie", imdb_id="tt0000005")


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

        synced = await sync_protection_lists(engine, definitions=[IMDB])

        assert isinstance(synced[IMDB_SLUG], str)
        assert "error" in synced[IMDB_SLUG]

    async def test_a_truncated_list_is_refused(
        self, engine: AsyncEngine, httpx2_mock: respx.Router
    ) -> None:
        """A short list would silently stop protecting the films that fell off it, so
        the provider refuses it -- and the orchestrator records that refusal rather than
        installing a half-empty whitelist."""
        httpx2_mock.get(IMDB_TOP_250_URL).mock(
            return_value=httpx.Response(200, json=_top250_payload(count=50))
        )

        synced = await sync_protection_lists(engine, definitions=[IMDB])

        assert isinstance(synced[IMDB_SLUG], str)
        assert "error" in synced[IMDB_SLUG]


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


class _WatchlistServer:
    """A Plex stand-in whose account watchlist holds one movie, or raises."""

    def __init__(self, imdb: str = "tt0000001", *, broken: bool = False) -> None:
        self._imdb = imdb
        self._broken = broken

    def myPlexAccount(self) -> Any:  # noqa: N802 - mirrors plexapi
        if self._broken:
            raise RuntimeError("plex.tv did not answer")
        item = SimpleNamespace(
            type="movie",
            title="A title",
            guid=None,
            guids=[SimpleNamespace(id=f"imdb://{self._imdb}")],
        )
        return SimpleNamespace(watchlist=lambda: [item])


class TestEachInstanceKeepsItsOwnKeepList:
    async def test_two_instances_of_one_service_both_protect(self, engine: AsyncEngine) -> None:
        """Two Sonarr instances, each with its own keep-tagged title, one tag definition.
        The definition builds one provider PER INSTANCE and the slug carries the instance
        id, so each instance syncs its OWN list. With a shared slug (the old shape), each
        sync atomically replaced the other's membership: whichever ran last erased the
        other instance's keep-tagged titles from the whitelist, silently -- a protection
        failing open, in whichever order the syncs happened to finish."""
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
            engine, definitions=[_tag_list()], sonarrs=[first, second]
        )

        # Two distinct lists, so neither sync can mask the other's outcome either.
        assert synced["sonarr-1-keeptags-any-list3"] == 1
        assert synced["sonarr-2-keeptags-any-list3"] == 1
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
            definitions=[_tag_list(("keep", "gold"), "any")],
            sonarrs=[self._sonarr()],
        )
        assert await memberships(engine, media_type="tv", tvdb_id=10)

        synced = await sync_protection_lists(
            engine,
            definitions=[_tag_list(("keep", "gold"), "all")],
            sonarrs=[self._sonarr()],
        )

        assert synced[TAG_SLUG] == "retired"
        assert not await memberships(engine, media_type="tv", tvdb_id=10)  # only "keep"
        assert await memberships(engine, media_type="tv", tvdb_id=11)  # both tags

    async def test_flipping_back_protects_again(self, engine: AsyncEngine) -> None:
        """Retired, never deleted: the membership stays, and the list resumes the moment
        the configuration produces it again. A row left disabled while its sync succeeds
        would be a keep-list that protects nothing."""
        for match in ("any", "all", "any"):
            await sync_protection_lists(
                engine,
                definitions=[_tag_list(("keep", "gold"), match)],
                sonarrs=[self._sonarr()],
            )

        assert await memberships(engine, media_type="tv", tvdb_id=10)

    async def test_deleting_the_tag_list_retires_the_whole_keep_list(
        self, engine: AsyncEngine
    ) -> None:
        """The stronger trigger: a deleted definition builds no provider at all, so
        without the retire pass nothing touches the stored list and every title it ever
        matched stays protected by a list the operator deleted."""
        await sync_protection_lists(engine, definitions=[_tag_list()], sonarrs=[self._sonarr()])
        assert await memberships(engine, media_type="tv", tvdb_id=10)

        await sync_protection_lists(engine, definitions=[], sonarrs=[self._sonarr()])

        assert not await memberships(engine, media_type="tv", tvdb_id=10)

    async def test_a_definition_with_no_tags_builds_no_provider_and_retires(
        self, engine: AsyncEngine
    ) -> None:
        """The save boundary refuses an empty tag list, so this row can only be a stored
        body edited by hand -- and it reads as "no tags configured", never as a sync of
        everything or of nothing that leaves the old membership protecting."""
        await sync_protection_lists(engine, definitions=[_tag_list()], sonarrs=[self._sonarr()])
        assert await memberships(engine, media_type="tv", tvdb_id=10)

        synced = await sync_protection_lists(
            engine, definitions=[_tag_list(())], sonarrs=[self._sonarr()]
        )

        assert synced[TAG_SLUG] == "retired"
        assert not await memberships(engine, media_type="tv", tvdb_id=10)

    async def test_a_failed_sync_is_never_retired(self, engine: AsyncEngine) -> None:
        """The stale copy is still the right list, just unrefreshed, and the atomic swap
        kept it deliberately. Retiring it here would unprotect every title on it because
        one *arr was briefly unreachable."""

        class _Unreachable(_TaggedSonarr):
            async def tags(self) -> list[dict[str, object]]:
                raise RuntimeError("connection refused")

        await sync_protection_lists(engine, definitions=[_tag_list()], sonarrs=[self._sonarr()])

        broken = SonarrSource(client=_Unreachable([], []), instance_id=1, name="hd")
        synced = await sync_protection_lists(engine, definitions=[_tag_list()], sonarrs=[broken])

        assert "error" in str(synced[TAG_SLUG])
        assert await _enabled(engine, TAG_SLUG) is True
        assert await memberships(engine, media_type="tv", tvdb_id=10)

    async def test_renaming_the_plex_collection_retires_the_old_one(
        self, engine: AsyncEngine
    ) -> None:
        """The collection name is in the slug too, so a rename leaves the old collection's
        membership protecting titles the operator has since taken off the new one."""
        await sync_protection_lists(
            engine, definitions=[_plex_list()], plex_server=_CollectionServer()
        )
        assert await memberships(engine, media_type="movie", imdb_id="tt0000001")

        synced = await sync_protection_lists(
            engine,
            definitions=[_plex_list("Keep Forever")],
            plex_server=_CollectionServer("tt0000002"),
        )

        assert synced["plex-collection-never-reap-list2"] == "retired"
        assert not await memberships(engine, media_type="movie", imdb_id="tt0000001")

    async def test_an_unreachable_plex_never_retires_the_collection(
        self, engine: AsyncEngine
    ) -> None:
        """With no server there is no provider and no slug, and retiring on that would
        unprotect every title on the "Never Reap" collection over a network blip. The
        scan already degrades for the same reason; the stored list must survive it."""
        await sync_protection_lists(
            engine, definitions=[_plex_list()], plex_server=_CollectionServer()
        )

        synced = await sync_protection_lists(engine, definitions=[_plex_list()], plex_server=None)

        assert synced == {}  # the collection was skipped, so nothing was checked either
        assert await memberships(engine, media_type="movie", imdb_id="tt0000001")


class TestTheWatchlistFollowsThePlexRules:
    """The watchlist is account data read through the connected server, so it takes the
    collection family's fail-closed shape: no live server, no provider, and the retire
    sweep stands down with it."""

    async def test_a_watchlist_definition_syncs_and_protects(self, engine: AsyncEngine) -> None:
        synced = await sync_protection_lists(
            engine, definitions=[WATCHLIST], plex_server=_WatchlistServer()
        )

        assert synced[WATCHLIST_SLUG] == 1
        assert await memberships(engine, media_type="movie", imdb_id="tt0000001")

    async def test_an_unreachable_plex_skips_and_never_retires_the_watchlist(
        self, engine: AsyncEngine
    ) -> None:
        await sync_protection_lists(engine, definitions=[WATCHLIST], plex_server=_WatchlistServer())

        synced = await sync_protection_lists(engine, definitions=[WATCHLIST], plex_server=None)

        assert synced == {}
        assert await _enabled(engine, WATCHLIST_SLUG) is True
        assert await memberships(engine, media_type="movie", imdb_id="tt0000001")

    async def test_a_deleted_watchlist_definition_retires_when_plex_answered(
        self, engine: AsyncEngine
    ) -> None:
        """The other half, without which the stand-down above could be a sweep that never
        runs: with Plex live and the definition gone, the stored watchlist is disabled."""
        await sync_protection_lists(engine, definitions=[WATCHLIST], plex_server=_WatchlistServer())

        synced = await sync_protection_lists(engine, definitions=[], plex_server=_WatchlistServer())

        assert synced[WATCHLIST_SLUG] == "retired"
        assert not await memberships(engine, media_type="movie", imdb_id="tt0000001")


class TestLegacySlugsAreRehomedOnUpgrade:
    """The upgrade path. Policy keep tags wrote slugs with no ``-list`` suffix. A definition
    that alone claims such a row ADOPTS it before the pass (``lists.adopt_legacy``): the row
    is renamed onto the definition's slug with its membership, so the operator's tagged
    titles are one editable list before anything has been checked. Where adoption must stand
    down -- two definitions could claim the row -- the sweep's contract holds: the legacy row
    is retired exactly when its replacements actually synced (rule 115), never on a failed
    sync that would withdraw the only membership still protecting."""

    @staticmethod
    def _sonarr_client() -> _TaggedSonarr:
        return _TaggedSonarr(
            [{"id": 1, "label": "keep"}],
            [{"title": "A", "tvdbId": 10, "tags": [1]}],
        )

    async def _store_legacy_row(self, engine: AsyncEngine) -> str:
        """A keep-tag list as the pre-registry code stored it: no definition id."""
        rule = ArrTagRule(self._sonarr_client(), ("keep",), "any", instance_id=1)  # type: ignore[arg-type]
        await lists.sync(engine, rule, kind=ListKind.WHITELIST)
        return rule.slug

    async def test_a_claimable_legacy_row_is_adopted_before_the_sync(
        self, engine: AsyncEngine
    ) -> None:
        legacy = await self._store_legacy_row(engine)
        assert legacy == "sonarr-1-keeptags-any"

        source = SonarrSource(client=self._sonarr_client(), instance_id=1, name="hd")
        synced = await sync_protection_lists(engine, definitions=[_tag_list()], sonarrs=[source])

        assert synced[TAG_SLUG] == 1
        assert legacy not in synced  # renamed before the pass, so there was nothing to retire
        assert await _enabled(engine, legacy) is None  # the old spelling is gone entirely
        assert await memberships(engine, media_type="tv", tvdb_id=10)

    async def test_a_failed_sync_after_adoption_keeps_the_stored_membership(
        self, engine: AsyncEngine
    ) -> None:
        """The adopted row already holds the legacy membership, and the atomic swap in
        ``lists.sync`` leaves it exactly as the last good check left it -- so a failed
        refresh right after the upgrade still protects every tagged title."""
        await self._store_legacy_row(engine)

        class _Unreachable(_TaggedSonarr):
            async def tags(self) -> list[dict[str, object]]:
                raise RuntimeError("connection refused")

        broken = SonarrSource(client=_Unreachable([], []), instance_id=1, name="hd")
        synced = await sync_protection_lists(engine, definitions=[_tag_list()], sonarrs=[broken])

        assert "error" in str(synced[TAG_SLUG])
        assert await _enabled(engine, TAG_SLUG) is True
        assert await memberships(engine, media_type="tv", tvdb_id=10)

    async def test_a_coasting_adopted_row_is_matched_by_the_name_its_rule_spells(
        self, engine: AsyncEngine
    ) -> None:
        """Membership surviving the failed refresh above is only half of the protection: the
        keep rule spells the DEFINITION's name, and a legacy row carries no ``rule_name`` at
        all, so ``matched_by`` falls back to a display name naming the server. The rule then
        matches nothing while the row reads healthy and the scan stays executable -- every
        tagged title unprotected, unannounced.

        The scan path pairs ``adopt_legacy`` with ``lists.sync_rule_names`` for that reason,
        as both of ``api/lists.py``'s paths already did (rule 72). Driven through the failing
        sync, because that is the branch where the fallback is what answers.
        """
        await self._store_legacy_row(engine)

        class _Unreachable(_TaggedSonarr):
            async def tags(self) -> list[dict[str, object]]:
                raise RuntimeError("connection refused")

        broken = SonarrSource(client=_Unreachable([], []), instance_id=1, name="hd")
        await sync_protection_lists(engine, definitions=[_tag_list()], sonarrs=[broken])

        held = await memberships(engine, media_type="tv", tvdb_id=10)

        # "Tagged titles", never "Sonarr (hd) tag: keep" -- the spelling `attach_list` wrote.
        assert [m.matched_by() for m in held] == [_tag_list().name]

    async def test_an_unclaimable_legacy_row_retires_once_the_syncs_land(
        self, engine: AsyncEngine
    ) -> None:
        """Two same-match definitions could each own the row, so adoption stands down and
        the sweep takes over -- retired here, where both replacements landed, and the new
        row carries the title on."""
        legacy = await self._store_legacy_row(engine)
        source = SonarrSource(client=self._sonarr_client(), instance_id=1, name="hd")
        two = [_tag_list(), _tag_list(("gold",), "any", list_id=4)]

        synced = await sync_protection_lists(engine, definitions=two, sonarrs=[source])

        assert synced[TAG_SLUG] == 1
        assert synced[legacy] == "retired"
        assert await memberships(engine, media_type="tv", tvdb_id=10)

    async def test_an_unclaimable_row_survives_a_failed_replacement_sync(
        self, engine: AsyncEngine
    ) -> None:
        """Rule 115's second half. The failed slug itself is in ``current`` and safe from
        the sweep; the row it was meant to REPLACE is not, and disabling that one on the
        strength of a sync that did not land is the fail-open this stands against."""
        legacy = await self._store_legacy_row(engine)

        class _Unreachable(_TaggedSonarr):
            async def tags(self) -> list[dict[str, object]]:
                raise RuntimeError("connection refused")

        broken = SonarrSource(client=_Unreachable([], []), instance_id=1, name="hd")
        two = [_tag_list(), _tag_list(("gold",), "any", list_id=4)]
        synced = await sync_protection_lists(engine, definitions=two, sonarrs=[broken])

        assert "error" in str(synced[TAG_SLUG])
        assert legacy not in synced  # not retired
        assert await _enabled(engine, legacy) is True
        assert await memberships(engine, media_type="tv", tvdb_id=10)
