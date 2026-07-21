# SPDX-License-Identifier: AGPL-3.0-or-later
"""Display metadata and deep links: frozen presentation fields, never verdict inputs.

Covers the three pure layers the why-panel's new header rests on:

* ``normalize_resolution`` -- Plex first, *arr quality-name parse as fallback, and
  ``None`` (badge hidden) for anything unrecognizable.
* ``build_ratings_json`` / ``parse_ratings_json`` -- the frozen ratings row. The IMDb
  entry must be the dataset's (the number the score used); Plex fills the rest; the
  *arr fills what Plex did not know. Ints only, both ways.
* ``deep_links.build_links`` -- every missing coordinate degrades that ONE link to
  ``None``; a link is hidden, never guessed or broken.

Plus the sweep's reload-storm guard: ``library_guid_index`` may read only attributes a
plexapi listing row actually carries -- an attribute outside that set would trigger
plexapi's implicit per-item reload (one HTTP call per library item), which the strict
stub turns into a loud failure.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from reaper.clients.plex import PlexClient
from reaper.config import RuntimeSafety
from reaper.ratings import Rating, RatingSource
from reaper.services.deep_links import build_links
from reaper.services.display_meta import (
    build_ratings_json,
    dataset_entry,
    normalize_resolution,
    parse_ratings_json,
)
from reaper.services.imdb_dataset import ImdbRating


def _rating(source: RatingSource, value: float, votes: int | None = None) -> Rating:
    return Rating(source=source, value=value, votes=votes, provider="test")


class TestNormalizeResolution:
    def test_plex_values_win_and_4k_is_canonicalised(self) -> None:
        assert normalize_resolution("4k", "Bluray-1080p") == "2160"
        assert normalize_resolution("1080", "Bluray-2160p") == "1080"
        assert normalize_resolution("sd", None) == "sd"

    def test_quality_name_is_the_fallback(self) -> None:
        assert normalize_resolution(None, "Bluray-2160p") == "2160"
        assert normalize_resolution(None, "WEBDL-1080p") == "1080"
        assert normalize_resolution(None, "HDTV-720p") == "720"
        assert normalize_resolution(None, "DVD-576p") == "576"
        assert normalize_resolution(None, "SDTV") == "sd"
        assert normalize_resolution("", "DVD") == "sd"

    def test_unrecognizable_is_none_never_guessed(self) -> None:
        assert normalize_resolution(None, None) is None
        assert normalize_resolution(None, "Remux") is None
        assert normalize_resolution("wat", None) is None


class TestBuildRatingsJson:
    DATASET = ImdbRating(tconst="tt0000001", average_rating=5.9, num_votes=35_072)

    def test_dataset_imdb_outranks_every_other_imdb(self) -> None:
        """The row must show the number the score used -- a Plex or Radarr IMDb value
        that disagrees with the dataset must lose."""
        stored = parse_ratings_json(
            build_ratings_json(
                self.DATASET,
                plex_ratings=[_rating(RatingSource.IMDB, 7.0)],
                arr_ratings=[_rating(RatingSource.IMDB, 6.5, votes=100)],
            )
        )
        assert stored["imdb"] == 59
        assert stored["imdb_votes"] == 35_072

    def test_plex_fills_what_the_dataset_cannot_and_arr_fills_the_rest(self) -> None:
        stored = parse_ratings_json(
            build_ratings_json(
                self.DATASET,
                plex_ratings=[
                    _rating(RatingSource.ROTTEN_TOMATOES_CRITIC, 7.7),
                    _rating(RatingSource.ROTTEN_TOMATOES_AUDIENCE, 7.1),
                ],
                arr_ratings=[
                    _rating(RatingSource.TMDB, 6.1),
                    _rating(RatingSource.ROTTEN_TOMATOES_CRITIC, 9.9),  # loses to Plex
                ],
            )
        )
        assert stored == {
            "imdb": 59,
            "imdb_votes": 35_072,
            "rotten_tomatoes_critic": 77,
            "rotten_tomatoes_audience": 71,
            "tmdb": 61,
        }

    def test_without_a_dataset_row_the_fallback_imdb_serves(self) -> None:
        stored = parse_ratings_json(
            build_ratings_json(None, arr_ratings=[_rating(RatingSource.IMDB, 6.5, votes=1_200)])
        )
        assert stored["imdb"] == 65
        assert stored["imdb_votes"] == 1_200

    def test_nothing_known_stores_nothing(self) -> None:
        assert build_ratings_json(None) is None

    def test_undisplayed_sources_are_not_frozen(self) -> None:
        stored = parse_ratings_json(
            build_ratings_json(
                None,
                arr_ratings=[
                    _rating(RatingSource.METACRITIC, 8.3),
                    _rating(RatingSource.UNKNOWN, 9.9),
                ],
            ),
        )
        assert stored == {}

    def test_trakt_is_frozen_for_the_ratings_row(self) -> None:
        # Radarr's ratings object carries a Trakt score for essentially every movie
        # (measured: ~99% coverage), and the why-panel now shows it.
        stored = parse_ratings_json(
            build_ratings_json(None, arr_ratings=[_rating(RatingSource.TRAKT, 7.7)])
        )
        assert stored == {"trakt": 77}

    def test_every_stored_value_is_an_int(self) -> None:
        text = build_ratings_json(
            self.DATASET, plex_ratings=[_rating(RatingSource.ROTTEN_TOMATOES_CRITIC, 7.66)]
        )
        assert text is not None
        assert all(isinstance(v, int) for v in json.loads(text).values())

    def test_parse_degrades_garbage_to_empty(self) -> None:
        assert parse_ratings_json(None) == {}
        assert parse_ratings_json("") == {}
        assert parse_ratings_json("not json") == {}
        assert parse_ratings_json('["a", "list"]') == {}
        assert parse_ratings_json('{"imdb": "text"}') == {}


class TestDatasetEntry:
    ROW = ImdbRating(tconst="tt0000001", average_rating=7.5, num_votes=10)

    def test_first_resolving_id_wins(self) -> None:
        table = {"tt0000001": self.ROW}
        assert dataset_entry(table, None, "tt_missing", "tt0000001") is self.ROW
        assert dataset_entry(table, "tt_missing") is None
        assert dataset_entry({}, "tt0000001") is None


class TestBuildLinks:
    KWARGS: ClassVar[dict[str, Any]] = {
        "plex_rating_key": 555,
        "tmdb_id": 603,
        "title_slug": "example-show",
        "arr_base_url": "https://radarr.example/",
        "tautulli_base_url": "https://tautulli.example",
        "machine_identifier": "abc123",
        "plex_web_url": "https://app.plex.tv/",
    }

    def test_a_movie_gets_all_three_links(self) -> None:
        links = build_links("radarr:2:1542", **self.KWARGS)
        assert links.radarr == "https://radarr.example/movie/603"
        assert links.sonarr is None
        assert links.tautulli == "https://tautulli.example/info?rating_key=555"
        assert links.plex == (
            "https://app.plex.tv/desktop/#!/server/abc123/details?key=%2Flibrary%2Fmetadata%2F555"
        )

    def test_the_plex_link_uses_web_for_a_self_hosted_address(self) -> None:
        # The plex.tv-hosted app serves the client under /desktop, but a Plex Media
        # Server serves its own copy under /web and 403s on /desktop. The path follows
        # the host so an operator's own address gets a link that resolves.
        kwargs = {**self.KWARGS, "plex_web_url": "https://plex.example/"}
        assert build_links("radarr:2:1542", **kwargs).plex == (
            "https://plex.example/web#!/server/abc123/details?key=%2Flibrary%2Fmetadata%2F555"
        )
        # A plex.tv subdomain is still the hosted app, so it keeps /desktop.
        hosted = {**self.KWARGS, "plex_web_url": "https://app.plex.tv"}
        assert build_links("radarr:2:1542", **hosted).plex == (
            "https://app.plex.tv/desktop/#!/server/abc123/details?key=%2Flibrary%2Fmetadata%2F555"
        )

    def test_a_season_and_a_show_route_to_sonarr_by_slug(self) -> None:
        kwargs = {**self.KWARGS, "arr_base_url": "https://sonarr.example"}
        for key in ("sonarr:1:42:3", "sonarr:1:42"):
            links = build_links(key, **kwargs)
            assert links.sonarr == "https://sonarr.example/series/example-show"
            assert links.radarr is None

    def test_unmatched_in_plex_loses_plex_and_tautulli_only(self) -> None:
        links = build_links("radarr:2:1542", **{**self.KWARGS, "plex_rating_key": None})
        assert links.plex is None
        assert links.tautulli is None
        assert links.radarr == "https://radarr.example/movie/603"

    def test_each_missing_coordinate_degrades_only_its_link(self) -> None:
        assert build_links("radarr:2:1542", **{**self.KWARGS, "tmdb_id": None}).radarr is None
        assert build_links("sonarr:1:42", **{**self.KWARGS, "title_slug": None}).sonarr is None
        assert build_links("radarr:2:1542", **{**self.KWARGS, "arr_base_url": None}).radarr is None
        assert (
            build_links("radarr:2:1542", **{**self.KWARGS, "tautulli_base_url": None}).tautulli
            is None
        )
        no_server = build_links("radarr:2:1542", **{**self.KWARGS, "machine_identifier": None})
        assert no_server.plex is None
        no_web = build_links("radarr:2:1542", **{**self.KWARGS, "plex_web_url": None})
        assert no_web.plex is None

    def test_an_unroutable_key_still_offers_plex_and_tautulli(self) -> None:
        links = build_links("garbage", **self.KWARGS)
        assert links.radarr is None and links.sonarr is None
        assert links.plex is not None
        assert links.tautulli is not None

    def test_a_slug_needing_escaping_is_escaped(self) -> None:
        kwargs = {
            **self.KWARGS,
            "arr_base_url": "https://sonarr.example",
            "title_slug": "a b/c",
        }
        assert build_links("sonarr:1:42", **kwargs).sonarr == (
            "https://sonarr.example/series/a%20b%2Fc"
        )

    def test_seerr_routes_movies_and_tv_to_their_pages(self) -> None:
        kwargs = {**self.KWARGS, "seerr_base_url": "https://seerr.example/"}
        assert build_links("radarr:2:1542", **kwargs).seerr == "https://seerr.example/movie/603"
        assert (
            build_links("sonarr:1:42:3", **{**kwargs, "media_type": "season"}).seerr
            == "https://seerr.example/tv/603"
        )
        no_tmdb = build_links("radarr:2:1542", **{**kwargs, "tmdb_id": None})
        assert no_tmdb.seerr is None

    def test_the_rating_sites_link_from_the_frozen_ids(self) -> None:
        kwargs = {**self.KWARGS, "imdb_id": "tt0000001", "title": "Example & Movie"}
        links = build_links("radarr:2:1542", **kwargs)
        assert links.imdb == "https://www.imdb.com/title/tt0000001/"
        assert links.tmdb == "https://www.themoviedb.org/movie/603"
        # RT slugs are hand-curated and unavailable, so the link is an honest search.
        assert links.rotten_tomatoes == (
            "https://www.rottentomatoes.com/search?search=Example%20%26%20Movie"
        )
        # Trakt's id lookup lands on the title's page from the same frozen imdb id.
        assert links.trakt == "https://trakt.tv/search/imdb/tt0000001"

    def test_a_tv_row_links_tmdb_as_tv(self) -> None:
        links = build_links(
            "sonarr:1:42:3", **{**self.KWARGS, "media_type": "season", "imdb_id": "tt0000002"}
        )
        assert links.tmdb == "https://www.themoviedb.org/tv/603"
        assert links.imdb == "https://www.imdb.com/title/tt0000002/"

    def test_missing_ids_hide_the_rating_site_links(self) -> None:
        links = build_links("radarr:2:1542", **{**self.KWARGS, "tmdb_id": None})
        assert links.imdb is None  # no imdb_id passed
        assert links.tmdb is None
        assert links.rotten_tomatoes is None  # no title passed
        assert links.trakt is None  # no imdb_id passed


# --- the sweep's reload-storm guard ------------------------------------------------


_LISTING_XML = """
<MediaContainer size="1" totalSize="1">
  <Video ratingKey="100" title="Example" year="2020" contentRating="TV-14"
         duration="4800000" rating="7.7" ratingImage="rottentomatoes://image.rating.ripe"
         audienceRating="7.1"
         audienceRatingImage="rottentomatoes://image.rating.upright"/>
</MediaContainer>
"""

_METADATA_XML = """
<MediaContainer size="1">
  <Video ratingKey="100"/>
</MediaContainer>
"""


class _StrictSection:
    type = "movie"
    key = 1
    title = "Movies"


class _StrictServer:
    """Serves canned containers and counts every request.

    The sweep parses the container XML directly, so listing metadata can only come from
    what the listing carried, plus ONE deliberate batched metadata read per 100 items
    (for the Rating children and show folders). Any regression back toward per-item
    fetches (the reload storm plexapi's object walk silently produced, measured at one
    HTTP request per title on a real library) shows up here as extra queries."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    class library:  # noqa: N801 - mirrors the plexapi attribute
        @staticmethod
        def sections() -> list[_StrictSection]:
            return [_StrictSection()]

    def query(self, path: str) -> Any:
        self.queries.append(path)
        from xml.etree.ElementTree import fromstring

        if path.startswith("/library/metadata/"):
            return fromstring(_METADATA_XML)  # noqa: S314 - canned literal
        return fromstring(_LISTING_XML)  # noqa: S314 - canned literal, not untrusted data


async def test_the_sweep_reads_only_listing_attributes() -> None:
    client = PlexClient(
        "http://plex.local:32400",
        "token",
        safety=RuntimeSafety(destructive_enabled=False),
    )
    server = _StrictServer()
    client._server = server  # type: ignore[assignment]

    swept = await client.library_guid_index(section_type="movie")

    item = swept[100]
    assert item.content_rating == "TV-14"
    assert item.runtime_minutes == 80
    # The section title rides onto every swept item as its library (read once per section,
    # not a per-item fetch, so it does not add to the query count asserted below).
    assert item.library == "Movies"
    # Provenance-parsed, with the audience slot routed to the audience source.
    assert {r.source for r in item.ratings} == {
        RatingSource.ROTTEN_TOMATOES_CRITIC,
        RatingSource.ROTTEN_TOMATOES_AUDIENCE,
    }
    # No media on this listing -> no resolution, and the badge stays hidden.
    assert item.video_resolution is None
    # One item, TWO requests: the listing plus one batched metadata read (Rating
    # children ride it at 100 items per call). Never a hidden per-item reload.
    assert len(server.queries) == 2
