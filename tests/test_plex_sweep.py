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
from reaper.ratings import RatingSource, from_plex


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
        # value is exactly what from_plex produces for the same inputs the plexapi
        # object walk would have handed it. (from_plex divides Rotten Tomatoes values
        # by ten; whether Plex's already-0-to-10 audience number should be exempt from
        # that is a question for the ratings module, not the sweep.)
        expected = from_plex("8.4", "rottentomatoes://image.rating.upright", audience=True)
        assert expected is not None
        assert any(
            r.value == expected.value and r.source is RatingSource.ROTTEN_TOMATOES_AUDIENCE
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


class TestLibraryGuidIndex:
    @pytest.fixture
    def movie_server(self) -> _FakeServer:
        listing = f'<MediaContainer size="2" totalSize="2">{MOVIE_ROW}{BARE_ROW}</MediaContainer>'
        return _FakeServer(
            [_FakeSection(1, "movie"), _FakeSection(2, "show")],
            {"/library/sections/1/all": listing},
        )

    async def test_movies_come_from_one_listing_request(self, movie_server: _FakeServer) -> None:
        index = await _client_with(movie_server).library_guid_index(section_type="movie")
        assert set(index) == {41, 42}
        assert index[41].ids.tmdb == 4141
        # THE point: two items, ONE request. No per-item metadata calls, ever.
        assert len(movie_server.queries) == 1

    async def test_show_folders_arrive_from_one_batched_read(self) -> None:
        listing = f'<MediaContainer size="1" totalSize="1">{SHOW_ROW}</MediaContainer>'
        batch = (
            '<MediaContainer size="1">'
            '<Directory ratingKey="90"><Location path="/tv/example show (2005)"/></Directory>'
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
        assert len(server.queries) == 2  # one listing page + one metadata batch
