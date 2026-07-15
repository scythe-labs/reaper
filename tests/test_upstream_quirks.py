# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the ways upstream APIs actually behave.

Every case here was found by probing live Sonarr, Radarr, Tautulli and Seerr instances
read-only, and every one of them contradicts a reasonable assumption. A fixture written
from an OpenAPI spec would have encoded the assumption instead of the truth -- which is
the whole reason this file exists. Each test names the wrong belief it protects against.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from reaper.clients.sonarr_stats import SeasonStats, parse_season_stats, rank_seasons
from reaper.clock import from_epoch, from_iso
from reaper.ratings import Rating, RatingSource, from_plex, from_radarr, pick


class TestEpisodeCountIsNotWhatItLooksLike:
    """``episodeCount`` is Sonarr's download *intent*, not what is on disk.

    On a mature library the majority of seasons are unmonitored and complete, and every
    one of them reports ``episodeCount=0`` while holding a full ``totalEpisodeCount``.
    Reading that as "empty season" is a mistake that scales.
    """

    def test_unmonitored_season_reports_zero_wanted_but_is_not_empty(self) -> None:
        """The common case on any finished show: all episodes aired, none monitored."""
        season = parse_season_stats(
            {
                "seasonNumber": 1,
                "monitored": False,
                "statistics": {
                    "episodeCount": 0,  # <- would read as "empty season"
                    "totalEpisodeCount": 15,
                    "episodeFileCount": 0,
                    "sizeOnDisk": 0,
                },
            }
        )
        assert season is not None
        assert season.wanted_episode_count == 0
        assert season.total_episode_count == 15  # the honest length
        assert season.has_content is False  # correct: no files

    def test_a_season_with_files_is_never_reported_as_empty(self) -> None:
        """The dangerous inverse: unmonitored, reports zero wanted, but the files are
        right there on disk. ``has_content`` must depend on ``episodeFileCount``, never
        on Sonarr's intent metric."""
        season = parse_season_stats(
            {
                "seasonNumber": 3,
                "monitored": False,  # unmonitored...
                "statistics": {
                    "episodeCount": 0,  # ...and reports zero wanted...
                    "totalEpisodeCount": 22,
                    "episodeFileCount": 22,  # ...but 22 files are on disk.
                    "sizeOnDisk": 40_000_000_000,
                },
            }
        )
        assert season is not None
        assert season.has_content is True
        assert season.size_on_disk == 40_000_000_000

    def test_incomplete_season_is_detected(self) -> None:
        """A long-running show mid-download: Sonarr wants more than it has."""
        season = parse_season_stats(
            {
                "seasonNumber": 0,
                "monitored": True,
                "statistics": {
                    "episodeCount": 112,
                    "totalEpisodeCount": 112,
                    "episodeFileCount": 98,
                    "sizeOnDisk": 100_000_000_000,
                },
            }
        )
        assert season is not None
        assert season.is_incomplete is True

    def test_a_monitored_unaired_season_reports_zero_wanted(self) -> None:
        """Monitored, but nothing has aired yet -- so it also reports zero, for a
        completely different reason than the unmonitored case above. Neither may be
        read as "this season is empty, reclaim it"."""
        season = parse_season_stats(
            {
                "seasonNumber": 9,
                "monitored": True,
                "statistics": {
                    "episodeCount": 0,
                    "totalEpisodeCount": 10,
                    "episodeFileCount": 0,
                    "sizeOnDisk": 0,
                },
            }
        )
        assert season is not None
        assert season.has_content is False

    def test_missing_statistics_is_none_not_an_empty_season(self) -> None:
        """Absent data must not read as 'nothing here, safe to delete'."""
        assert parse_season_stats({"seasonNumber": 1, "monitored": True}) is None


class TestSeasonRanking:
    """What "keep the last 2 seasons" counts against."""

    def _season(self, n: int, files: int = 10) -> SeasonStats:
        return SeasonStats(
            season_number=n,
            monitored=True,
            episode_file_count=files,
            size_on_disk=files * 1_000_000_000,
            total_episode_count=files,
            wanted_episode_count=files,
        )

    def test_rank_one_is_the_newest_season(self) -> None:
        ranks = rank_seasons([self._season(n) for n in (1, 2, 3, 4, 5)])
        assert ranks[5] == 1
        assert ranks[1] == 5

    def test_specials_do_not_consume_a_rank_slot(self) -> None:
        """Season 0 is not part of the run of the show. If it took rank 1,
        'keep the last 2' would keep specials plus a single real season --
        silently deleting the most recent season the viewer actually watches."""
        ranks = rank_seasons([self._season(n) for n in (0, 1, 2, 3)])

        assert 0 not in ranks
        assert ranks[3] == 1
        assert ranks[2] == 2

    def test_keep_last_two_selects_the_right_seasons(self) -> None:
        ranks = rank_seasons([self._season(n) for n in (0, 1, 2, 3, 4, 5)])
        keep = {n for n, rank in ranks.items() if rank <= 2}
        assert keep == {4, 5}


class TestRatingProvenance:
    """The published guidance says Plex's ``audienceRating`` is Rotten Tomatoes.

    On a probed server it was IMDb, on every movie sampled. Both shapes exist in the
    wild -- the field means whatever the library's metadata agent decided it means. So
    the source is read from ``ratingImage`` and never assumed from the field name.
    """

    def test_plex_audience_rating_read_as_imdb_when_the_image_says_imdb(self) -> None:
        rating = from_plex("7.0", "imdb://image.rating")

        assert rating is not None
        assert rating.source is RatingSource.IMDB
        assert rating.value == 7.0

    def test_the_same_field_read_as_rotten_tomatoes_when_the_image_says_so(self) -> None:
        """Same field, different library, different meaning. This is why we read
        provenance instead of trusting the field name."""
        rating = from_plex("96", "rottentomatoes://image.rating.ripe")

        assert rating is not None
        assert rating.source is RatingSource.ROTTEN_TOMATOES_CRITIC
        assert rating.value == 9.6  # normalised from a 0-100 percentage

    def test_a_rating_with_no_provenance_is_dropped_not_guessed(self) -> None:
        """An uninterpretable number must not justify a deletion."""
        assert from_plex("7.0", None) is None
        assert from_plex("7.0", "") is None

    def test_an_empty_rating_is_none(self) -> None:
        """Plex returns '' for absent, which must not become 0.0 -- a 0.0 rating
        would read as 'terrible film, delete it'."""
        assert from_plex("", "imdb://image.rating") is None
        assert from_plex(None, "imdb://image.rating") is None


class TestRadarrRatings:
    """Radarr hands us five rating sources for free, in a payload we already fetch.

    The shape below is representative of a well-reviewed mainstream film: a large IMDb
    vote count, a Tomatometer expressed as a bare percentage, and Metacritic with no
    vote concept at all.
    """

    SAMPLE: ClassVar[dict[str, dict[str, object]]] = {
        "imdb": {"votes": 400_000, "value": 7.7, "type": "user"},
        "tmdb": {"votes": 8_000, "value": 7.43, "type": "user"},
        "metacritic": {"votes": 0, "value": 83, "type": "user"},
        "rottenTomatoes": {"votes": 0, "value": 96, "type": "user"},
        "trakt": {"votes": 19_000, "value": 7.7455, "type": "user"},
    }

    def test_all_five_sources_are_read(self) -> None:
        ratings = from_radarr(self.SAMPLE)
        assert {r.source for r in ratings} == {
            RatingSource.IMDB,
            RatingSource.TMDB,
            RatingSource.METACRITIC,
            RatingSource.ROTTEN_TOMATOES_CRITIC,
            RatingSource.TRAKT,
        }

    def test_percentages_are_normalised_to_ten(self) -> None:
        rt = pick(from_radarr(self.SAMPLE), RatingSource.ROTTEN_TOMATOES_CRITIC)
        assert rt is not None
        assert rt.value == 9.6  # 96% -> 9.6/10

    def test_votes_zero_on_a_percentage_source_means_no_vote_concept(self) -> None:
        """Radarr reports votes: 0 for Rotten Tomatoes. Read literally, a vote
        floor of 1000 would reject every RT score in the library."""
        rt = pick(from_radarr(self.SAMPLE), RatingSource.ROTTEN_TOMATOES_CRITIC)
        assert rt is not None
        assert rt.votes is None
        assert rt.has_meaningful_vote_count is False
        assert rt.meets(9.0, min_votes=1000) is True  # the floor does not apply

    def test_the_vote_floor_does_apply_to_imdb(self) -> None:
        imdb = pick(from_radarr(self.SAMPLE), RatingSource.IMDB)
        assert imdb is not None
        assert imdb.votes == 400_000
        assert imdb.meets(7.0, min_votes=1000) is True

    def test_a_high_rating_with_too_few_votes_does_not_protect(self) -> None:
        """A 9.5 from 12 votes is noise, and protecting on it would keep junk."""
        obscure = Rating(source=RatingSource.IMDB, value=9.5, votes=12, provider="radarr")
        assert obscure.meets(7.0, min_votes=1000) is False

    def test_an_unknown_source_never_protects(self) -> None:
        """Fail closed: we would rather keep a file than delete it on a number we
        cannot interpret -- but we must not *protect* on one either, or an
        unparseable field would silently make everything immortal."""
        unknown = Rating(source=RatingSource.UNKNOWN, value=10.0, votes=999_999, provider="?")
        assert unknown.meets(1.0) is False

    def test_describe_always_states_provenance(self) -> None:
        imdb = pick(from_radarr(self.SAMPLE), RatingSource.IMDB)
        assert imdb is not None
        described = imdb.describe()
        assert "imdb" in described
        assert "400,000 votes" in described
        assert "radarr" in described


class TestUpstreamTimestampShapes:
    """The integrations disagree about how to express an instant."""

    def test_tautulli_added_at_is_a_string_but_last_played_is_an_int(self) -> None:
        """Tautulli returns the same kind of value with two different types in the same
        response: ``added_at`` as a string, ``last_played`` as an int."""
        assert from_epoch("1700000000") == from_epoch(1_700_000_000)

    def test_tautulli_never_played_is_none_not_1970(self) -> None:
        """last_played is None/'' for never-played. Coerced to epoch 0 it becomes
        1970 -- which the scoring engine reads as *maximally stale*, making
        never-watched media the top deletion candidate. Exactly backwards."""
        assert from_epoch(None) is None
        assert from_epoch("") is None
        assert from_epoch(0) is None

    def test_seerr_speaks_iso_not_epoch(self) -> None:
        """Seerr uses ISO-8601 with a trailing Z, where the others use epoch seconds."""
        parsed = from_iso("2026-01-02T15:54:47.000Z")

        assert parsed is not None
        assert parsed.year == 2026
        assert parsed.utcoffset() is not None

    @pytest.mark.parametrize("value", ["", None, "not-a-date", "2026-01-02T15:54:47"])
    def test_unparseable_or_offsetless_iso_is_none(self, value: str | None) -> None:
        """An ISO timestamp with no offset is rejected rather than assumed UTC:
        guessing is how a deletion clock ends up hours out."""
        assert from_iso(value) is None
