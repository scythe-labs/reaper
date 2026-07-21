# SPDX-License-Identifier: AGPL-3.0-or-later
"""The GUID sweep parses listing XML directly, with no hidden per-item requests.

The trap this pins down was measured, not imagined: walking ``section.all()`` objects
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

from reaper.clients.plex import PlexClient, _parse_sweep_element
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
    def __init__(self, key: int, stype: str) -> None:
        self.key = key
        self.type = stype


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
