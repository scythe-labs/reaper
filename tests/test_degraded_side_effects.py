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

from reaper.clients.plex import PlexClient, collecting_incomplete_reads
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
        tautulli = _FakeTautulli(
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


class _FakeTautulli:
    def __init__(self, libraries: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
        self._libraries = libraries
        self._rows = rows

    async def libraries(self) -> list[dict[str, Any]]:
        return self._libraries

    async def library_media_info(
        self, section_id: int, *, start: int = 0, length: int = 100
    ) -> dict[str, Any]:
        return {"data": self._rows if start == 0 else []}


class TestAMalformedLibraryRowDegradesInsteadOfRaising:
    """``services.library_index`` promises neither read raises: a failure costs the operator
    a plan, never the whole scan. A library row with no section id, or an item row whose
    rating key is not a number, used to raise straight out of the spine read."""

    async def test_a_library_with_no_id_is_skipped_and_degrades(self) -> None:
        tautulli = _FakeTautulli([{"section_type": "movie", "section_name": "Movies"}], [])
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
        tautulli = _FakeTautulli(
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
