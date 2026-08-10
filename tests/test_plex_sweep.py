# SPDX-License-Identifier: AGPL-3.0-or-later
"""The GUID sweep parses listing XML directly, with no hidden per-item requests.

The trap this pins down was measured: walking ``section.all()`` objects
made plexapi silently reload any item whose accessed attribute was ``None``, one
metadata request per title, and the "single sweep" cost minutes on a large library.
The raw parser must extract every field the resolver and the review view rely on --
and a fake server here COUNTS its requests, so a regression back toward per-item
fetches fails loudly.
"""

from __future__ import annotations

from typing import Any
from xml.etree.ElementTree import fromstring as _unsafe_fromstring

import pytest

from reaper.clients.plex import SWEEP_PAGE_SIZE, PlexClient, PlexError, _parse_sweep_element
from reaper.config import RuntimeSafety
from reaper.ratings import RatingSource


def fromstring(xml: str) -> Any:
    """Parse a canned test fixture. These are literals in this file, not untrusted
    data, so the stdlib parser is fine here (S314 exists for hostile inputs)."""
    return _unsafe_fromstring(xml)  # noqa: S314


MOVIE_ROW = """
<Video ratingKey="41" guid="plex://movie/aaaa" title="Example Film" year="1999"
       addedAt="1000000" contentRating="R" duration="7260000"
       audienceRating="8.4" audienceRatingImage="rottentomatoes://image.rating.upright">
  <Guid id="imdb://tt0000041"/>
  <Guid id="tmdb://4141"/>
  <Media videoResolution="1080">
    <Part file="/media/example film (1999).mkv" size="4000000000"/>
  </Media>
</Video>
"""

BARE_ROW = """
<Video ratingKey="42" title="Bare Film"/>
"""

SHOW_ROW = """
<Directory ratingKey="90" guid="plex://show/bbbb" title="Example Show" year="2005"
           addedAt="2000000">
  <Guid id="tvdb://999"/>
</Directory>
"""


class TestParseSweepElement:
    def test_a_full_movie_row_carries_every_field(self) -> None:
        item = _parse_sweep_element(fromstring(MOVIE_ROW))
        assert item.rating_key == 41
        assert item.title == "Example Film"
        assert item.year == 1999
        assert item.added_at is not None and item.added_at.tzinfo is not None
        assert item.ids.imdb == "tt0000041"
        assert item.ids.tmdb == 4141
        assert item.file_basename == "example film (1999).mkv"
        assert item.files[0].size == 4_000_000_000
        assert item.video_resolution == "1080"
        assert item.content_rating == "R"
        assert item.runtime_minutes == 121
        # The audience slot with an RT image resolves to the audience source, and the
        # value stays on Plex's 0-10 scale exactly as the attribute carried it:
        # audienceRating="8.4" is an 84% score. Dividing it again, as Radarr's raw
        # percentages need, is the regression this pins (8.4 once became 0.84).
        assert any(
            r.source is RatingSource.ROTTEN_TOMATOES_AUDIENCE and r.value == 8.4
            for r in item.ratings
        )

    def test_missing_fields_are_none_never_a_fetch(self) -> None:
        """A bare row parses to honest Nones -- the exact opposite of the plexapi
        object walk, where a None attribute meant a hidden network request."""
        item = _parse_sweep_element(fromstring(BARE_ROW))
        assert item.rating_key == 42
        assert item.year is None
        assert item.added_at is None
        assert item.files == ()
        assert item.file_basename is None
        assert item.video_resolution is None
        assert item.content_rating is None
        assert item.runtime_minutes is None
        assert item.ratings == ()


class _FakeSection:
    def __init__(self, key: int, stype: str, title: str | None = None) -> None:
        self.key = key
        self.type = stype
        # A real Plex section carries the operator's library name; the sweep stamps it onto
        # every item as its ``library``. Defaulted from the key so existing call sites need
        # not spell one out.
        self.title = title if title is not None else f"Section {key}"
        self.locations: list[str] = []


class _FakeLibrary:
    def __init__(self, sections: list[_FakeSection]) -> None:
        self._sections = sections

    def sections(self) -> list[_FakeSection]:
        return self._sections


class _FakeServer:
    """Serves canned containers and counts every request the sweep makes."""

    def __init__(self, sections: list[_FakeSection], responses: dict[str, str]) -> None:
        self.library = _FakeLibrary(sections)
        self._responses = responses
        self.queries: list[str] = []

    def query(self, path: str) -> Any:
        self.queries.append(path)
        for prefix, xml in self._responses.items():
            if path.startswith(prefix):
                return fromstring(xml)
        raise AssertionError(f"unexpected query: {path}")


def _client_with(server: _FakeServer) -> PlexClient:
    client = PlexClient("http://plex.local", "token", safety=RuntimeSafety())
    client._server = server  # type: ignore[assignment]
    return client


MOVIE_BATCH = """
<MediaContainer size="2">
  <Video ratingKey="41">
    <Rating image="rottentomatoes://image.rating.ripe" value="7.5" type="critic"/>
    <Rating image="rottentomatoes://image.rating.spilled" value="4.3" type="audience"/>
    <Rating image="imdb://image.rating" value="5.7" type="audience"/>
    <Rating image="themoviedb://image.rating" value="5.4" type="audience"/>
    <Rating image="metacritic://image.rating" value="5.4" type="critic"/>
  </Video>
  <Video ratingKey="42"/>
</MediaContainer>
"""


class TestLibraryGuidIndex:
    @pytest.fixture
    def movie_server(self) -> _FakeServer:
        listing = f'<MediaContainer size="2" totalSize="2">{MOVIE_ROW}{BARE_ROW}</MediaContainer>'
        return _FakeServer(
            [_FakeSection(1, "movie"), _FakeSection(2, "show")],
            {"/library/sections/1/all": listing, "/library/metadata/41,42": MOVIE_BATCH},
        )

    async def test_movies_come_from_a_listing_plus_one_batched_read(
        self, movie_server: _FakeServer
    ) -> None:
        index = await _client_with(movie_server).library_guid_index(section_type="movie")
        assert set(index) == {41, 42}
        assert index[41].ids.tmdb == 4141
        # THE point: two items, TWO requests -- one listing page plus one metadata batch
        # (100 items per call, for the Rating children). Never a per-item reload.
        assert len(movie_server.queries) == 2

    async def test_rating_children_add_sources_the_slots_did_not_carry(
        self, movie_server: _FakeServer
    ) -> None:
        """The listing's two slots cannot carry a provider's critic AND audience score;
        the batched metadata's typed Rating children fill in the rest. The slot value
        keeps precedence where both name the same source, and a child whose provenance
        we cannot read (metacritic has no prefix mapping) is dropped, never guessed."""
        index = await _client_with(movie_server).library_guid_index(section_type="movie")
        by_source = {r.source: r.value for r in index[41].ratings}
        assert by_source == {
            # The slot's 8.4 wins over the child's 4.3 for the same source.
            RatingSource.ROTTEN_TOMATOES_AUDIENCE: 8.4,
            RatingSource.ROTTEN_TOMATOES_CRITIC: 7.5,
            RatingSource.IMDB: 5.7,
            RatingSource.TMDB: 5.4,
        }
        # The bare row's empty metadata adds nothing.
        assert index[42].ratings == ()

    async def test_show_folders_arrive_from_one_batched_read(self) -> None:
        listing = f'<MediaContainer size="1" totalSize="1">{SHOW_ROW}</MediaContainer>'
        batch = (
            '<MediaContainer size="1">'
            '<Directory ratingKey="90">'
            '<Location path="/tv/example show (2005)"/>'
            '<Rating image="rottentomatoes://image.rating.upright" value="8.8" type="audience"/>'
            "</Directory>"
            "</MediaContainer>"
        )
        server = _FakeServer(
            [_FakeSection(2, "show")],
            {"/library/sections/2/all": listing, "/library/metadata/90": batch},
        )
        index = await _client_with(server).library_guid_index(section_type="show")
        assert index[90].ids.tvdb == 999
        # The folder-name tier that narrows a show listed in two sections.
        assert index[90].file_basename == "example show (2005)"
        assert [f.basename for f in index[90].files] == ["example show (2005)"]
        # The same batch carries the show's Rating children -- no extra request.
        assert [(r.source, r.value) for r in index[90].ratings] == [
            (RatingSource.ROTTEN_TOMATOES_AUDIENCE, 8.8)
        ]
        assert len(server.queries) == 2  # one listing page + one metadata batch


SEASON_LISTING = """
<MediaContainer size="4" totalSize="4">
  <Directory ratingKey="901" parentRatingKey="900" index="1" addedAt="1000000" title="Season 1"/>
  <Directory ratingKey="902" parentRatingKey="900" index="2" addedAt="1000001" title="Season 2"/>
  <Directory ratingKey="951" parentRatingKey="950" index="1" addedAt="1000002" title="Season 1"/>
  <Directory ratingKey="999" index="1" title="Orphan season with no show"/>
</MediaContainer>
"""


class TestLibrarySeasonIndex:
    async def test_seasons_group_under_their_show_with_every_field(self) -> None:
        server = _FakeServer(
            [_FakeSection(1, "movie"), _FakeSection(2, "show")],
            {"/library/sections/2/all": SEASON_LISTING},
        )
        out = await _client_with(server).library_season_index()
        # Grouped by parentRatingKey; the orphan row (no show) is dropped, never guessed.
        assert set(out) == {900, 950}
        assert {r.season_index for r in out[900]} == {1, 2}
        assert {r.rating_key for r in out[900]} == {901, 902}
        first = next(r for r in out[900] if r.season_index == 1)
        assert first.rating_key == 901
        assert first.added_at == "1000000"  # raw epoch string; from_epoch parses it later
        # Only the show section is swept -- the movie section (type=3 makes no sense there)
        # is never queried.
        assert all("/library/sections/2/all" in q for q in server.queries)
        assert all("type=3" in q for q in server.queries)

    async def test_allowed_sections_scopes_the_sweep(self) -> None:
        server = _FakeServer(
            [_FakeSection(2, "show"), _FakeSection(7, "show")],
            {"/library/sections/2/all": SEASON_LISTING, "/library/sections/7/all": SEASON_LISTING},
        )
        out = await _client_with(server).library_season_index(allowed_sections={2})
        # Section 7 is excluded, so only section 2 was read.
        assert all("/library/sections/2/all" in q for q in server.queries)
        assert set(out) == {900, 950}

    async def test_a_clamped_page_is_followed_to_totalsize(self) -> None:
        """A server may return fewer rows than the requested page while ``totalSize`` says
        more remain. The sweep follows the total to the end and never stops on the short
        page (rule 5) -- else a real season goes unread and loses its watch protection."""
        page0 = (
            '<MediaContainer size="1" totalSize="2">'
            '<Directory ratingKey="901" parentRatingKey="900" index="1"/>'
            "</MediaContainer>"
        )
        page1 = (
            '<MediaContainer size="1" totalSize="2">'
            '<Directory ratingKey="902" parentRatingKey="900" index="2"/>'
            "</MediaContainer>"
        )
        server = _FakeServer(
            [_FakeSection(2, "show")],
            {
                "/library/sections/2/all?type=3&X-Plex-Container-Start=0": page0,
                "/library/sections/2/all?type=3&X-Plex-Container-Start=1": page1,
            },
        )
        out = await _client_with(server).library_season_index()
        assert {r.rating_key for r in out[900]} == {901, 902}
        assert len(server.queries) == 2

    async def test_a_child_without_a_rating_key_raises_rather_than_truncating(self) -> None:
        """A child the paging math cannot advance over (no ratingKey) is an anomaly the
        complete-or-raise contract raises on, so the caller falls back per show (rule 5)."""
        listing = (
            '<MediaContainer size="2" totalSize="2">'
            '<Directory ratingKey="901" parentRatingKey="900" index="1"/>'
            '<Directory parentRatingKey="900" index="2"/>'  # no ratingKey
            "</MediaContainer>"
        )
        server = _FakeServer([_FakeSection(2, "show")], {"/library/sections/2/all": listing})
        with pytest.raises(PlexError):
            await _client_with(server).library_season_index()

    async def test_a_full_page_with_no_totalsize_raises(self) -> None:
        """A full page and no ``totalSize`` to bound it: we cannot tell whether more remains,
        so we fail closed rather than guess it is the last page (rule 5)."""
        rows = "".join(
            f'<Directory ratingKey="{9000 + i}" parentRatingKey="900" index="{i}"/>'
            for i in range(SWEEP_PAGE_SIZE)
        )
        listing = f'<MediaContainer size="{SWEEP_PAGE_SIZE}">{rows}</MediaContainer>'  # no total
        server = _FakeServer([_FakeSection(2, "show")], {"/library/sections/2/all": listing})
        with pytest.raises(PlexError):
            await _client_with(server).library_season_index()


class TestTwinsHardenedPaging:
    """B-3: the GUID sweep and the two section-listing twins page exactly like the season
    sweep -- raw-count advance, ``totalSize`` the sole authority, a truncated page or an
    unbounded full page raised on. Before the fix these three still fell ``totalSize`` -> ``size``
    and ended on a short page, so a clamped or unbounded section returned a silently partial map.
    """

    async def test_guid_sweep_follows_a_clamped_page_to_totalsize(self) -> None:
        # size=1 while totalSize=2: the server clamped the page below the request. The sweep
        # must follow the total to the second page, not stop on the short first one.
        page0 = f'<MediaContainer size="1" totalSize="2">{MOVIE_ROW}</MediaContainer>'
        page1 = f'<MediaContainer size="1" totalSize="2">{BARE_ROW}</MediaContainer>'
        server = _FakeServer(
            [_FakeSection(1, "movie")],
            {
                "/library/sections/1/all?includeGuids=1&X-Plex-Container-Start=0": page0,
                "/library/sections/1/all?includeGuids=1&X-Plex-Container-Start=1": page1,
                "/library/metadata/": "<MediaContainer/>",  # the batched Rating read, empty
            },
        )
        index = await _client_with(server).library_guid_index(section_type="movie")
        assert set(index) == {41, 42}

    async def test_guid_sweep_raises_on_a_full_page_with_no_totalsize(self) -> None:
        rows = "".join(
            f'<Video ratingKey="{1000 + i}" title="F{i}"/>' for i in range(SWEEP_PAGE_SIZE)
        )
        listing = f'<MediaContainer size="{SWEEP_PAGE_SIZE}">{rows}</MediaContainer>'  # no total
        server = _FakeServer([_FakeSection(1, "movie")], {"/library/sections/1/all": listing})
        with pytest.raises(PlexError):
            await _client_with(server).library_guid_index(section_type="movie")

    async def test_label_read_raises_on_a_child_with_no_rating_key(self) -> None:
        listing = (
            '<MediaContainer size="2" totalSize="2">'
            '<Video ratingKey="1"/>'
            "<Video/>"  # no ratingKey: the paging math cannot advance over it
            "</MediaContainer>"
        )
        server = _FakeServer([_FakeSection(1, "movie")], {"/library/sections/1/all": listing})
        with pytest.raises(PlexError):
            await _client_with(server).labeled_in_section(1, kind="movie", label="Leaving Soon")

    async def test_section_listing_follows_a_clamped_page_to_totalsize(self) -> None:
        page0 = '<MediaContainer size="1" totalSize="2"><Video ratingKey="1"/></MediaContainer>'
        page1 = '<MediaContainer size="1" totalSize="2"><Video ratingKey="2"/></MediaContainer>'
        server = _FakeServer(
            [_FakeSection(1, "movie")],
            {
                "/library/sections/1/all?type=1&X-Plex-Container-Start=0": page0,
                "/library/sections/1/all?type=1&X-Plex-Container-Start=1": page1,
            },
        )
        keys = await _client_with(server).section_rating_keys(1, kind="movie")
        assert keys == {1, 2}


class TestTheShelfReadsPageToo:
    """B-4: the two collection reads were issued raw, one request, iterate what comes back.
    Windowed by the server, each one fails a different way and neither says so."""

    async def test_a_collection_past_the_first_window_is_still_found(self) -> None:
        """Read unpaged, a shelf sitting past the first window reads as ABSENT, and the
        caller then creates a SECOND "Leaving Soon" collection: the reconcile splits across
        two shelves and neither one is right."""
        page0 = (
            '<MediaContainer size="1" totalSize="2">'
            '<Directory ratingKey="10" title="Other"/>'
            "</MediaContainer>"
        )
        page1 = (
            '<MediaContainer size="1" totalSize="2">'
            '<Directory ratingKey="11" title="Leaving Soon"/>'
            "</MediaContainer>"
        )
        server = _FakeServer(
            [_FakeSection(1, "movie")],
            {
                "/library/sections/1/collections?X-Plex-Container-Start=0": page0,
                "/library/sections/1/collections?X-Plex-Container-Start=1": page1,
            },
        )

        assert await _client_with(server).find_collection(1, "leaving soon") == 11

    async def test_a_truncated_member_list_is_never_read_as_the_whole_shelf(self) -> None:
        """The reconcile detaches ``current - wanted``, so a short read leaves titles marked
        "Leaving Soon" long after they were reprieved."""
        page0 = '<MediaContainer size="1" totalSize="2"><Video ratingKey="1"/></MediaContainer>'
        page1 = '<MediaContainer size="1" totalSize="2"><Video ratingKey="2"/></MediaContainer>'
        server = _FakeServer(
            [_FakeSection(1, "movie")],
            {
                "/library/collections/9/children?X-Plex-Container-Start=0": page0,
                "/library/collections/9/children?X-Plex-Container-Start=1": page1,
            },
        )

        assert await _client_with(server).collection_children(9) == {1, 2}

    async def test_an_unbounded_full_page_of_members_raises(self) -> None:
        """The same fail-closed rule the sweeps run on: a full page with no ``totalSize``
        cannot be told from a truncated one, so it is never guessed to be the last."""
        rows = "".join(f'<Video ratingKey="{i}"/>' for i in range(SWEEP_PAGE_SIZE))
        server = _FakeServer(
            [_FakeSection(1, "movie")],
            {"/library/collections/9/children": f"<MediaContainer>{rows}</MediaContainer>"},
        )

        with pytest.raises(PlexError):
            await _client_with(server).collection_children(9)


class _NeverAdvancing(_FakeServer):
    """Serves a full page whose ``totalSize`` sits far ahead of ``start``, forever.

    None of the loop's existing exits fire: the page is never empty, never short, and the
    reported total is always ahead. Refuses once asked past the cap, so deleting the cap fails
    this test instead of hanging the suite (rule 118).
    """

    def __init__(self, limit: int) -> None:
        super().__init__([_FakeSection(1, "movie")], {})
        self._limit = limit

    def query(self, path: str) -> Any:
        self.queries.append(path)
        if len(self.queries) > self._limit:
            raise AssertionError("asked for a page past the cap")
        return fromstring(
            '<MediaContainer size="1" totalSize="10000">'
            f'<Video ratingKey="{len(self.queries)}"/>'
            "</MediaContainer>"
        )


class TestASweepThatNeverFinishesIsBounded:
    """Rule 56/89's page backstop, on the one paged read of four that lacked one.

    ``plex.SWEEP_MAX_PAGES`` carries why it raises and what a runaway sweep costs.
    """

    async def test_a_section_sweep_stops_and_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The page count is what stops it, never the total: the fixture's 10,000 would end on
        its own after 10,000 pages. It raises rather than returning short, matching
        ``seerr.MAX_PAGES`` and not ``history_sync.MAX_HISTORY_PAGES``, because ``_iter_pages``
        is complete-or-raise and every caller reads a protection source."""
        monkeypatch.setattr("reaper.clients.plex.SWEEP_MAX_PAGES", 3)
        server = _NeverAdvancing(limit=4)

        with pytest.raises(PlexError, match="never finished, after 3 items"):
            await _client_with(server).section_rating_keys(1, kind="movie")

        assert len(server.queries) == 3

    async def test_the_shelf_read_is_bounded_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The two shelf reads enter ``_iter_pages`` directly rather than through
        ``_iter_section_pages``, and a different cap value from the case above so neither test
        rests on one number (rule 141). Production is 1,000."""
        monkeypatch.setattr("reaper.clients.plex.SWEEP_MAX_PAGES", 2)
        server = _NeverAdvancing(limit=3)

        with pytest.raises(PlexError, match="never finished, after 2 items"):
            await _client_with(server).collection_children(9)

        assert len(server.queries) == 2

    async def test_a_listing_that_ends_on_the_cap_still_reads_in_full(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The trip sits after the ``start >= totalSize`` exit, so a listing whose last page is
        the cap'th page returns everything. Off by one the other way and the cap would refuse
        reads that finished."""
        monkeypatch.setattr("reaper.clients.plex.SWEEP_MAX_PAGES", 2)
        page0 = '<MediaContainer size="1" totalSize="2"><Video ratingKey="1"/></MediaContainer>'
        page1 = '<MediaContainer size="1" totalSize="2"><Video ratingKey="2"/></MediaContainer>'
        server = _FakeServer(
            [_FakeSection(1, "movie")],
            {
                "/library/sections/1/all?type=1&X-Plex-Container-Start=0": page0,
                "/library/sections/1/all?type=1&X-Plex-Container-Start=1": page1,
            },
        )

        assert await _client_with(server).section_rating_keys(1, kind="movie") == {1, 2}


class TestSectionPaths:
    """B-2/B2-2: the path table addresses sections by KEY, and its failures are PlexError."""

    async def test_two_libraries_sharing_a_title_both_survive(self) -> None:
        """Keyed by title, one of these silently overwrote the other -- and the dropped
        library's post-reap refresh then mapped to nothing at all."""
        hd, four_k = _FakeSection(1, "movie", "Movies"), _FakeSection(2, "movie", "Movies")
        hd.locations, four_k.locations = ["/media/hd"], ["/media/4k"]

        rows = await _client_with(_FakeServer([hd, four_k], {})).section_paths()

        assert [(r.key, r.title, r.locations) for r in rows] == [
            (1, "Movies", ("/media/hd",)),
            (2, "Movies", ("/media/4k",)),
        ]

    async def test_a_failing_read_surfaces_as_plex_error(self) -> None:
        """The only Plex read that did not map its failures. Plex can answer the connect
        handshake and still fail this one path, and the raw exception escaped the executor's
        ``except PlexError`` mid-run: the file already deleted, its journal step stuck at
        SENT, the run stuck EXECUTING, and every remaining approved deletion never tried."""

        class _Boom:
            def sections(self) -> list[_FakeSection]:
                raise RuntimeError("Plex restarted")

        server = _FakeServer([], {})
        server.library = _Boom()  # type: ignore[assignment]

        with pytest.raises(PlexError):
            await _client_with(server).section_paths()


class TestTrashCount:
    """What the operator is warned about before a reap.

    ``empty_trash`` is section-wide, so it destroys the library records of everything
    already in the trash, not just what the run deleted. Those items sit on both sides of
    the executor's before/after count and cancel out of its gate, so this read is the only
    thing that can see them. Every branch here therefore resolves toward telling the
    operator something rather than reporting a reassuring zero.
    """

    def _server(self, xml: str) -> _FakeServer:
        return _FakeServer([], {"/library/sections/7/all?trash=1": xml})

    async def test_it_reads_the_total_the_container_reports(self) -> None:
        server = self._server('<MediaContainer size="0" totalSize="12"/>')
        assert await _client_with(server).trash_count(7) == 12
        # Zero-sized window: a count read must never pull the listing itself.
        assert "X-Plex-Container-Size=0" in server.queries[0]

    async def test_a_genuinely_empty_trash_is_zero(self) -> None:
        server = self._server('<MediaContainer size="0" totalSize="0"/>')
        assert await _client_with(server).trash_count(7) == 0

    async def test_a_container_with_no_total_fails_closed(self) -> None:
        """No ``totalSize`` means the answer is not a count. Falling back to ``size`` (rule
        56) would read a zero-sized window as an empty trash and print a fabricated number
        beside the operator's most dangerous button."""
        server = self._server('<MediaContainer size="0"/>')
        with pytest.raises(PlexError, match="totalSize"):
            await _client_with(server).trash_count(7)

    async def test_a_failing_read_surfaces_as_plex_error(self) -> None:
        """Rule 110: the caller catches ``PlexError`` and warns. A raw transport exception
        would escape that and take the reap page down with it."""

        class _Boom(_FakeServer):
            def query(self, path: str) -> Any:
                raise RuntimeError("Plex restarted")

        with pytest.raises(PlexError):
            await _client_with(_Boom([], {})).trash_count(7)


class TestEmptiesTrashAfterScan:
    """Plex's own ``autoEmptyTrash``, which is server-wide and ships ON. When it is on,
    Plex purges the trash itself after every scan Reaper's refresh triggers, so the
    executor's trash interlock never gets a say."""

    class _Settings:
        def __init__(self, value: Any) -> None:
            self._value = value

        def get(self, key: str) -> Any:
            assert key == "autoEmptyTrash"
            return type("Pref", (), {"value": self._value})()

    async def test_it_reads_the_preference(self) -> None:
        server = _FakeServer([], {})
        server.settings = self._Settings(True)  # type: ignore[attr-defined]
        assert await _client_with(server).empties_trash_after_scan() is True

    async def test_an_unreadable_preference_is_an_error_not_a_no(self) -> None:
        """Reporting "no" for a setting we could not read would tell the operator Reaper's
        interlock is in force when it may not be."""

        class _Boom:
            def get(self, key: str) -> Any:
                raise RuntimeError("no such setting")

        server = _FakeServer([], {})
        server.settings = _Boom()  # type: ignore[attr-defined]
        with pytest.raises(PlexError):
            await _client_with(server).empties_trash_after_scan()
