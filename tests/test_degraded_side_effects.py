# SPDX-License-Identifier: AGPL-3.0-or-later
"""A scan that cannot trust itself must not write protection off, and a protection
source it could not read in full must degrade it.

Three separate ways the scan pipeline used to fail quietly:

* **The keep-tag retire pass ran unconditionally.** A scan holding a policy Reaper had
  to repair carries the SHIPPED keep tags and match mode, so retiring "every keep list
  this configuration no longer produces" disabled every list the operator actually
  saved. Nothing could be deleted from that scan, but the disabling write is durable
  and outlives it (rule 115).
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
from reaper.services.lists import load_membership_index
from reaper.services.season_scan import SonarrSource
from reaper.services.snapshot import sync_protection_lists


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))  # type: ignore[call-arg]
    yield eng
    await eng.dispose()


class _TaggedSonarr:
    service = "sonarr"

    def __init__(self, tags: list[dict[str, object]], series: list[dict[str, object]]) -> None:
        self._tags = tags
        self._series = series

    async def tags(self) -> list[dict[str, object]]:
        return self._tags

    async def series(self) -> list[dict[str, object]]:
        return self._series


def _sonarr() -> SonarrSource:
    return SonarrSource(
        client=_TaggedSonarr(  # type: ignore[arg-type]
            [{"id": 1, "label": "keep"}],
            [{"title": "A", "tvdbId": 10, "tags": [1]}],
        ),
        instance_id=1,
        name="hd",
    )


class TestAPolicyReaperHadToRepairNeverRetiresAKeepList:
    """The slug carries the match mode, so a fallen-back policy (which carries the SHIPPED
    'any') retires the '-all' list an operator saved. The scan is already degraded and
    refuses to plan, so nothing is deleted -- but the keep list stays disabled afterwards,
    protecting nothing, until the next scan under a policy that loads."""

    @staticmethod
    async def _sync(engine: AsyncEngine, *, match: str, trusted: bool) -> dict[str, int | str]:
        return await sync_protection_lists(
            engine,
            sonarrs=[_sonarr()],
            tv_keep_tags=("keep",),
            tv_keep_match=match,
            keep_tags_trusted=trusted,
            include_top_250=False,
        )

    async def test_the_saved_keep_list_survives_a_fallen_back_scan(
        self, engine: AsyncEngine
    ) -> None:
        await self._sync(engine, match="all", trusted=True)
        assert (await load_membership_index(engine)).lookup(media_type="tv", tvdb_id=10)

        # The next scan holds a repaired policy: shipped tags, shipped match.
        synced = await self._sync(engine, match="any", trusted=False)

        assert "retired" not in str(synced.get("sonarr-1-keeptags-all"))
        assert (await load_membership_index(engine)).lookup(media_type="tv", tvdb_id=10)

    async def test_a_trusted_policy_still_retires_it(self, engine: AsyncEngine) -> None:
        """The control, so the test above cannot pass by the retire pass being broken: with
        the operator's own policy in hand, flipping the match still retires the old list."""
        await self._sync(engine, match="all", trusted=True)

        synced = await self._sync(engine, match="any", trusted=True)

        assert synced["sonarr-1-keeptags-all"] == "retired"


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
            tautulli,  # type: ignore[arg-type]
            _client_with(server),
            section_type="movie",
            degrade=reasons.append,
            allowed_sections=None,
        )

        assert any("ratings" in r and "nothing may be deleted" in r.lower() for r in reasons)
        assert set(index.by_rating_key) == {41, 42}  # the sweep was kept, not thrown away


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
            tautulli,  # type: ignore[arg-type]
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
            tautulli,  # type: ignore[arg-type]
            None,
            section_type="movie",
            degrade=reasons.append,
            allowed_sections=None,
        )

        assert set(index.by_rating_key) == {7}  # the good row still entered the index
        assert any("without a usable id" in r for r in reasons)
