# SPDX-License-Identifier: AGPL-3.0-or-later
"""A scan that cannot trust itself must not write protection off, and a protection
source it could not read in full must degrade it.

Three separate ways the scan pipeline used to fail quietly:

* **The retire pass ran on configuration the scan could not vouch for.** The original
  case was a repaired policy carrying the SHIPPED keep tags, which retired every keep
  list the operator actually saved; the keep tags have since moved to the list registry,
  so the policy no longer feeds the sync at all and that trigger is gone structurally.
  The registry itself is the surviving trigger: a read failure handed to the sync as "no
  lists" would retire every list on the install. Nothing could be deleted from that scan,
  but the disabling write is durable and outlives it (rule 115).
* **A short batched metadata read only logged.** Those ``Rating`` children are the one
  source of the per-provider scores, so a windowed response takes the keep bar off the
  tail of every chunk: a protection WITHDRAWN, which rule 28 never lets pass as a log
  line.
* **A malformed row from Tautulli raised out of a read documented as never raising**,
  which costs the operator the whole scan rather than a plan.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import fromstring as _unsafe_fromstring

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clients.base import IntegrationError
from reaper.clients.plex import PlexClient, collecting_incomplete_reads
from reaper.clients.tautulli import TautulliClient
from reaper.config import RuntimeSafety, Settings
from reaper.db.session import create_engine
from reaper.services import library_index
from reaper.services.list_config import ListDefinition
from reaper.services.lists import ListSource, load_membership_index
from reaper.services.season_scan import SonarrSource
from reaper.services.snapshot import sync_protection_lists
from tests._fakes import FakeSonarr


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))
    yield eng
    await eng.dispose()


def _sonarr() -> SonarrSource:
    return SonarrSource(
        client=FakeSonarr(
            tag_rows=[{"id": 1, "label": "keep"}],
            series_rows=[{"title": "A", "tvdbId": 10, "tags": [1]}],
        ),
        instance_id=1,
        name="hd",
    )


class TestAConfigurationTheScanCannotVouchForNeverRetiresAKeepList:
    """Rule 115's durable half, from the degraded scan's side. A registry that could not
    be READ arrives as ``definitions=None``, and the sweep must stand down whole: reading
    it as "no lists" would disable every list on the install, and the disabling write
    outlives the (already un-plannable) scan that made it. The keep-tags version of this
    trigger -- a repaired policy carrying the SHIPPED tags -- is gone structurally, because
    the policy no longer feeds the sync anything at all; ``services.list_rules`` carries
    the repaired-policy skip now, pinned in ``tests/test_list_rules.py``."""

    @staticmethod
    def _tag_list(match: str = "all") -> ListDefinition:
        return ListDefinition(
            id=3,
            name="Tagged",
            source=ListSource.ARR_TAG,
            config={"tags": ["keep"], "match": match},
            enabled=True,
        )

    async def test_the_saved_keep_list_survives_an_unreadable_registry(
        self, engine: AsyncEngine
    ) -> None:
        await sync_protection_lists(engine, definitions=[self._tag_list()], sonarrs=[_sonarr()])
        assert (await load_membership_index(engine)).lookup(media_type="tv", tvdb_id=10)

        synced = await sync_protection_lists(engine, definitions=None, sonarrs=[_sonarr()])

        assert "retired" not in str(synced.get("sonarr-1-keeptags-all-list3"))
        assert (await load_membership_index(engine)).lookup(media_type="tv", tvdb_id=10)

    async def test_a_readable_registry_still_retires_it(self, engine: AsyncEngine) -> None:
        """The control, so the test above cannot pass by the retire pass being broken: with
        the registry in hand, flipping the match still retires the old list."""
        await sync_protection_lists(engine, definitions=[self._tag_list()], sonarrs=[_sonarr()])

        synced = await sync_protection_lists(
            engine, definitions=[self._tag_list("any")], sonarrs=[_sonarr()]
        )

        assert synced["sonarr-1-keeptags-all-list3"] == "retired"


def _fromstring(xml: str) -> Any:
    """Canned fixtures, literals in this file rather than untrusted input."""
    return _unsafe_fromstring(xml)  # noqa: S314


class _FakeSection:
    def __init__(self, key: int, stype: str) -> None:
        self.key = key
        self.type = stype
        self.title = f"Section {key}"


class _FakeServer:
    """Serves canned containers by path prefix."""

    def __init__(self, responses: dict[str, str]) -> None:
        self.library = self._Library()
        self._responses = responses

    class _Library:
        def sections(self) -> list[_FakeSection]:
            return [_FakeSection(1, "movie")]

    def query(self, path: str) -> Any:
        for prefix, xml in self._responses.items():
            if path.startswith(prefix):
                return _fromstring(xml)
        raise AssertionError(f"unexpected query: {path}")


_LISTING = (
    '<MediaContainer size="2" totalSize="2">'
    '<Video ratingKey="41" title="One"><Guid id="imdb://tt0000041"/></Video>'
    '<Video ratingKey="42" title="Two"><Guid id="imdb://tt0000042"/></Video>'
    "</MediaContainer>"
)


def _client_with(server: _FakeServer) -> PlexClient:
    client = PlexClient("http://plex.local", "token", safety=RuntimeSafety())
    client._server = server  # type: ignore[assignment]
    return client


class TestAShortRatingsReadDegradesTheScan:
    """The enrichment batch carries the per-provider scores, and for shows it is the only
    rating source there is. A server that windows the multi-id response drops the tail of
    the chunk, and every title in that tail is judged with no rating: the rating bar cannot
    keep what it cannot see. Silence there was a protection withdrawn without a word."""

    async def test_a_windowed_batch_reports_a_reason(self) -> None:
        server = _FakeServer(
            {
                "/library/sections/1/all": _LISTING,
                # Two keys asked for, one answered: the tail of the chunk is gone.
                "/library/metadata/": '<MediaContainer size="1"><Video ratingKey="41"/>'
                "</MediaContainer>",
            }
        )

        with collecting_incomplete_reads() as reasons:
            index = await _client_with(server).library_guid_index(section_type="movie")

        assert len(reasons) == 1
        assert "1 of 2" in reasons[0]
        # ...and the ids are still complete. Raising instead would have thrown a good sweep
        # away and dropped the whole library to title-only matching on top of degrading.
        assert set(index) == {41, 42}

    async def test_a_complete_batch_reports_nothing(self) -> None:
        server = _FakeServer(
            {
                "/library/sections/1/all": _LISTING,
                "/library/metadata/": '<MediaContainer size="2"><Video ratingKey="41"/>'
                '<Video ratingKey="42"/></MediaContainer>',
            }
        )

        with collecting_incomplete_reads() as reasons:
            await _client_with(server).library_guid_index(section_type="movie")

        assert reasons == []

    async def test_the_scan_degrades_on_it_end_to_end(self) -> None:
        """The reason has to reach the snapshot, not just the collector: ``build_index``
        opens the collector around its gather, so a windowed enrichment read leaves the
        scan viewable and un-executable rather than quietly under-protected."""
        server = _FakeServer(
            {
                "/library/sections/1/all": _LISTING,
                "/library/metadata/": '<MediaContainer size="1"><Video ratingKey="41"/>'
                "</MediaContainer>",
            }
        )
        tautulli = _RawLibraries(
            [{"section_id": "1", "section_type": "movie", "section_name": "Movies"}],
            [{"rating_key": "41", "title": "One", "year": 1999}],
        )
        reasons: list[str] = []

        index = await library_index.build_index(
            tautulli,
            _client_with(server),
            section_type="movie",
            degrade=reasons.append,
            allowed_sections=None,
        )

        assert any("ratings" in r and "nothing may be deleted" in r.lower() for r in reasons)
        assert set(index.by_rating_key) == {41, 42}  # the sweep was kept


class _RawLibraries(TautulliClient):
    """Serves a library list exactly as written, malformed rows included.

    Not `tests._fakes.FakeTautulli`: that one builds its `libraries()` from the keys of a
    section map, so it can only ever emit a well-formed integer `section_id`. These cases
    need the shapes it cannot produce, which is the whole subject here -- a section with a
    string id, and a section carrying no id at all.

    Two knobs cover the paging shapes. `page` caps how many rows one call serves whatever
    `length` asked for, which is ordinary API behavior and the shape #559 turned on; `count`
    is what the envelope reports, with `None` for a server that reports no count at all.
    Neither defaults to the production page size, so no case can pass by accident (rule 141).
    """

    def __init__(
        self,
        libraries: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        *,
        page: int | None = None,
        count: int | None = None,
    ) -> None:
        self._libraries = libraries
        self._rows = rows
        self._page = page
        self._count = count
        #: Every `start` the walk asked for, so a test can pin what it advanced by.
        self.starts: list[int] = []

    async def libraries(self) -> list[dict[str, Any]]:
        return self._libraries

    async def library_media_info(
        self,
        section_id: int,
        *,
        start: int = 0,
        length: int = 100,
        order_column: str = "added_at",
        order_dir: str = "desc",
    ) -> dict[str, Any]:
        self.starts.append(start)
        served = min(length, self._page) if self._page is not None else length
        page: dict[str, Any] = {"data": self._rows[start : start + served]}
        if self._count is not None:
            page["recordsFiltered"] = self._count
        return page


class TestAMalformedLibraryRowDegradesInsteadOfRaising:
    """``services.library_index`` promises neither read raises: a failure costs the operator
    a plan, never the whole scan. A library row with no section id, or an item row whose
    rating key is not a number, used to raise straight out of the spine read."""

    async def test_a_library_with_no_id_is_skipped_and_degrades(self) -> None:
        tautulli = _RawLibraries([{"section_type": "movie", "section_name": "Movies"}], [])
        reasons: list[str] = []

        index = await library_index.build_index(
            tautulli,
            None,
            section_type="movie",
            degrade=reasons.append,
            allowed_sections=None,
        )

        assert index.by_rating_key == {}
        assert any("without an id" in r for r in reasons)

    async def test_an_item_with_an_unusable_rating_key_is_skipped_and_degrades(self) -> None:
        tautulli = _RawLibraries(
            [{"section_id": "1", "section_type": "movie", "section_name": "Movies"}],
            [
                {"rating_key": "not-a-number", "title": "Bad", "year": 1999},
                {"rating_key": "7", "title": "Good", "year": 2001},
            ],
        )
        reasons: list[str] = []

        index = await library_index.build_index(
            tautulli,
            None,
            section_type="movie",
            degrade=reasons.append,
            allowed_sections=None,
        )

        assert set(index.by_rating_key) == {7}  # the good row still entered the index
        assert any("without a usable id" in r for r in reasons)


class TestAShortPageDoesNotEndTheLibraryWalk:
    """The spine walk used to exit on ``len(rows) < 1000``. A server is free to clamp a page
    below the length asked for, so that read part of a library as the whole of it and degraded
    nothing: every item never listed resolves unmatched, which keeps the file but explains a
    live one as something Plex has not matched (#559). Tautulli's own reported count is the
    paging authority now, the way ``history_sync`` reads it off the same API."""

    @staticmethod
    def _rows(n: int) -> list[dict[str, Any]]:
        return [{"rating_key": str(k), "title": f"Item {k}", "year": 2001} for k in range(1, n + 1)]

    @staticmethod
    async def _build(tautulli: TautulliClient, reasons: list[str]) -> Any:
        return await library_index.build_index(
            tautulli,
            None,
            section_type="movie",
            degrade=reasons.append,
            allowed_sections=None,
        )

    async def test_a_clamped_page_is_followed_to_the_end(self) -> None:
        """Three rows a call against a count of seven: the walk keeps going, and it advances
        by what each page held rather than by the length it asked for."""
        tautulli = _RawLibraries(
            [{"section_id": "1", "section_type": "movie", "section_name": "Movies"}],
            self._rows(7),
            page=3,
            count=7,
        )
        reasons: list[str] = []

        index = await self._build(tautulli, reasons)

        assert set(index.by_rating_key) == set(range(1, 8))
        assert reasons == []  # a complete read is not an anomaly
        assert tautulli.starts == [0, 3, 6]

    async def test_a_walk_that_ends_before_the_count_degrades(self) -> None:
        """Tautulli says nine and serves four. What was read is kept, and the operator is told
        rather than shown a scan that quietly judged a fraction of a library (rule 28)."""
        tautulli = _RawLibraries(
            [{"section_id": "1", "section_type": "movie", "section_name": "Movies"}],
            self._rows(4),
            page=2,
            count=9,
        )
        reasons: list[str] = []

        index = await self._build(tautulli, reasons)

        assert set(index.by_rating_key) == {1, 2, 3, 4}
        assert any("4 of 9" in r and "nothing may be deleted" in r for r in reasons)

    @pytest.mark.parametrize("count", [None, 0])
    async def test_a_source_that_reports_no_count_is_still_paged_to_the_end(
        self, count: int | None
    ) -> None:
        """ "We were not told how many" must never read as "none", in either spelling: an
        omitted field and a reported zero both mean the count cannot end the walk. Folding
        them to zero ends it after page one and calls the truncation complete."""
        tautulli = _RawLibraries(
            [{"section_id": "1", "section_type": "movie", "section_name": "Movies"}],
            self._rows(5),
            page=2,
            count=count,
        )
        reasons: list[str] = []

        index = await self._build(tautulli, reasons)

        assert set(index.by_rating_key) == {1, 2, 3, 4, 5}
        assert reasons == []

    async def test_a_server_that_never_advances_is_stopped_and_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The count bounds every walk that is given one. A server that reports no count AND
        ignores ``start`` is bounded by nothing else once the short-page exit is gone, so the
        page cap is what stops it, and stopping early is a partial read like any other
        (rule 56)."""
        monkeypatch.setattr(library_index, "_SPINE_MAX_PAGES", 3)

        class _IgnoresStart(_RawLibraries):
            """Serves page one forever, and refuses once asked past the cap so that deleting
            the cap fails this test rather than hanging the suite (rule 118)."""

            async def library_media_info(self, section_id: int, **kwargs: Any) -> dict[str, Any]:
                if len(self.starts) >= 3:
                    raise IntegrationError("tautulli", "asked for a page past the cap")
                return await super().library_media_info(section_id, **{**kwargs, "start": 0})

        tautulli = _IgnoresStart(
            [{"section_id": "1", "section_type": "movie", "section_name": "Movies"}],
            self._rows(2),
            page=2,
            count=None,
        )
        reasons: list[str] = []

        index = await self._build(tautulli, reasons)

        assert set(index.by_rating_key) == {1, 2}  # it stopped, and kept what it read
        assert any("only 6 items" in r and "nothing may be deleted" in r for r in reasons)
