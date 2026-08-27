# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the ways upstream APIs actually behave.

Every case here was found by probing live Sonarr, Radarr, Tautulli, and Seerr instances
read-only, and every one of them contradicts a reasonable assumption. A fixture written
from an OpenAPI spec would have encoded the assumption instead of the truth. Each test
names the wrong belief it protects against.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from reaper.clients.base import IntegrationError
from reaper.clients.seerr import SeerrClient
from reaper.clients.sonarr_stats import SeasonStats, parse_season_stats, rank_seasons
from reaper.clock import from_epoch, from_iso
from reaper.config import RuntimeSafety
from reaper.ratings import Rating, RatingSource, from_plex, from_radarr, pick


class TestSeerrPaginationEnvelope:
    """``pageInfo.results`` is the only signal that more pages exist. Rows present with
    the total missing is an envelope-shape change. Reading it as total=0 would stop
    after one page and silently undercount every requester."""

    async def test_rows_without_a_total_refuse_rather_than_truncate(self) -> None:
        client = SeerrClient("http://seerr.local", "key", safety=RuntimeSafety())

        async def fake_get_json(path: str, **kwargs: object) -> object:
            return {
                "pageInfo": {},
                "results": [
                    {
                        "id": i,
                        "type": "movie",
                        "createdAt": "2024-01-01T00:00:00.000Z",
                        "media": {},
                        "requestedBy": {},
                    }
                    for i in range(3)
                ],
            }

        client.get_json = fake_get_json  # type: ignore[method-assign]
        try:
            with pytest.raises(IntegrationError) as exc:
                await client.all_requests()
            assert exc.value.code == "error.integration.unexpected_shape"
        finally:
            await client.aclose()


class TestEpisodeCountIsNotWhatItLooksLike:
    """``episodeCount`` is Sonarr's download *intent*, not what is on disk.

    On a mature library the majority of seasons are unmonitored and complete, and every
    one of them reports ``episodeCount=0`` while holding a full ``totalEpisodeCount``.
    Reading that as "empty season" is a mistake that scales.
    """

    def test_unmonitored_season_reports_zero_wanted_but_is_not_empty(self) -> None:
        """On any finished show, all episodes have aired and none are monitored. This is
        the common case."""
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
        assert season.has_content is False  # correct, no files

    def test_a_season_with_files_is_never_reported_as_empty(self) -> None:
        """The dangerous inverse. Unmonitored, reporting zero wanted, but the files are
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

    def test_files_on_disk_with_no_reported_size_is_unknown_not_empty(self) -> None:
        """The partial-payload case, and the one that decides a keep.

        Sonarr says 22 files are on disk but reports no size for them. Reading that
        missing size as ``0`` would raise the deletion score as much as possible, and any
        "keep large files" rule would silently stop holding the season. It must read as
        "we could not tell" instead. ``has_content`` still says the files are there,
        because the two questions are answered by different fields for exactly this reason.
        """
        season = parse_season_stats(
            {
                "seasonNumber": 3,
                "monitored": False,
                "statistics": {
                    "episodeCount": 0,
                    "totalEpisodeCount": 22,
                    "episodeFileCount": 22,  # the files are there...
                    "sizeOnDisk": 0,  # ...but their size was not reported
                },
            }
        )
        assert season is not None
        assert season.has_content is True
        assert season.size_on_disk is None

    def test_a_genuinely_empty_season_also_reads_as_no_size(self) -> None:
        """Nothing on disk and no size. Same ``None`` result, and harmless, because
        ``has_content`` is False, so the season is never a deletion candidate in the
        first place."""
        season = parse_season_stats(
            {
                "seasonNumber": 4,
                "monitored": True,
                "statistics": {"episodeCount": 0, "totalEpisodeCount": 0, "episodeFileCount": 0},
            }
        )
        assert season is not None
        assert season.has_content is False
        assert season.size_on_disk is None

    def test_incomplete_season_is_detected(self) -> None:
        """A long-running show mid-download, where Sonarr wants more episodes than it has."""
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
        """Monitored, but nothing has aired yet, so it also reports zero, for a
        completely different reason than the unmonitored case above. Neither case means
        "this season is empty, reclaim it."
        """
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
        """Season 0 is not part of the run of the show. If it took rank 1, 'keep the
        last 2' would keep specials plus a single real season, silently deleting the
        most recent season the viewer actually watches."""
        ranks = rank_seasons([self._season(n) for n in (0, 1, 2, 3)])

        assert 0 not in ranks
        assert ranks[3] == 1
        assert ranks[2] == 2

    def test_keep_last_two_selects_the_right_seasons(self) -> None:
        ranks = rank_seasons([self._season(n) for n in (0, 1, 2, 3, 4, 5)])
        keep = {n for n, rank in ranks.items() if rank <= 2}
        assert keep == {4, 5}

    def test_a_fileless_season_does_not_consume_a_rank_slot(self) -> None:
        """An announced-but-undownloaded next season must not take rank 1. 'Keep the
        last 2' would then protect the empty shell plus one real season, leaving the
        season the rule meant to keep open to pruning. It is the same slot-shift problem
        the specials exclusion closes, showing up again through empty seasons instead.
        """
        seasons = [self._season(n) for n in (1, 2, 3, 4, 5)] + [self._season(6, files=0)]
        ranks = rank_seasons(seasons)

        assert 6 not in ranks
        assert ranks[5] == 1
        assert ranks[4] == 2


class TestRatingProvenance:
    """The published guidance says Plex's ``audienceRating`` is Rotten Tomatoes.

    On a probed server it was IMDb, on every movie sampled. Both shapes exist in the
    wild, because the field means whatever the library's metadata agent decided it means.
    So the source is read from ``ratingImage`` and never assumed from the field name.
    """

    def test_plex_audience_rating_read_as_imdb_when_the_image_says_imdb(self) -> None:
        rating = from_plex("7.0", "imdb://image.rating")

        assert rating is not None
        assert rating.source is RatingSource.IMDB
        assert rating.value == 7.0

    def test_the_same_field_read_as_rotten_tomatoes_when_the_image_says_so(self) -> None:
        """Same field, different library, different meaning. This is why we read
        provenance instead of trusting the field name."""
        rating = from_plex("9.6", "rottentomatoes://image.rating.ripe")

        assert rating is not None
        assert rating.source is RatingSource.ROTTEN_TOMATOES_CRITIC
        assert rating.value == 9.6

    def test_plex_rotten_tomatoes_is_already_on_ten_and_is_not_divided_again(self) -> None:
        """Plex serves every rating slot on 0-10 whatever the source. An 84% audience
        score arrives from Plex as "8.4". Dividing it the way Radarr's raw percentages
        need would turn it into 0.84, displayed as 8%.
        """
        audience = from_plex("8.4", "rottentomatoes://image.rating.upright", audience=True)

        assert audience is not None
        assert audience.value == 8.4

    def test_a_percentage_shaped_plex_value_is_still_read_as_a_percentage(self) -> None:
        """A percentage source above 10 can only be a raw percentage from an agent that
        skipped Plex's 0-10 normalization. The value itself proves the scale.
        """
        rating = from_plex("96", "rottentomatoes://image.rating.ripe")

        assert rating is not None
        assert rating.value == 9.6

    def test_a_value_outside_every_known_scale_is_dropped(self) -> None:
        """11 cannot be a 0-10 average, and 250 cannot be a percentage. A number we
        cannot interpret must not protect, condemn, or be displayed."""
        assert from_plex("11", "imdb://image.rating") is None
        assert from_plex("-1", "imdb://image.rating") is None
        assert from_plex("250", "rottentomatoes://image.rating.ripe") is None

    def test_the_audience_slot_routes_a_rotten_tomatoes_image_to_the_audience_score(
        self,
    ) -> None:
        """Both RT populations arrive as ``rottentomatoes://`` images. Only the slot
        tells them apart. Without the flag, the audience score would silently become the
        Tomatometer, and the panel would show two 'critic' numbers.
        """
        audience = from_plex("8.4", "rottentomatoes://image.rating.upright", audience=True)
        assert audience is not None
        assert audience.source is RatingSource.ROTTEN_TOMATOES_AUDIENCE
        assert audience.value == 8.4

        # An IMDb image in the audience slot is still just IMDb, the probed-server shape
        # from this module's docstring. The flag only disambiguates RT.
        imdb = from_plex("7.0", "imdb://image.rating", audience=True)
        assert imdb is not None
        assert imdb.source is RatingSource.IMDB

    def test_a_rating_with_no_provenance_is_dropped_not_guessed(self) -> None:
        """An uninterpretable number must not justify a deletion."""
        assert from_plex("7.0", None) is None
        assert from_plex("7.0", "") is None

    def test_an_empty_rating_is_none(self) -> None:
        """Plex returns '' for absent. That must not become 0.0, since a 0.0 rating
        would read as 'terrible film, delete it.'
        """
        assert from_plex("", "imdb://image.rating") is None
        assert from_plex(None, "imdb://image.rating") is None

    def test_both_ends_of_the_scale_are_inside_it(self) -> None:
        """``0.0 <= number <= 10.0`` is inclusive at both ends, and the test above only
        drove the outside (11, -1, 250). Dropping either edge turns a rating we read
        perfectly well into "no rating," which the why-panel prints as a source that was
        never checked, taking the protection down with it.
        """
        top = from_plex("10", "imdb://image.rating")
        assert top is not None
        assert top.value == 10.0

        bottom = from_plex("0", "imdb://image.rating")
        assert bottom is not None
        assert bottom.value == 0.0

    def test_a_percentage_source_at_exactly_ten_is_a_score_not_a_percentage(self) -> None:
        """The raw-percentage rescale triggers above 10, never at it. A Tomatometer of 10
        on Plex's 0-10 scale is already a perfect 100%. Rescaling it would file the
        best-reviewed title in the library as 10% and let the bar miss it.
        """
        on_the_scale = from_plex("10", "rottentomatoes://image.rating.ripe")
        assert on_the_scale is not None
        assert on_the_scale.value == 10.0

        # 100 is the raw-percentage shape the rescale exists for, and it lands on the
        # same 10.0. The two readings agree at the top of the scale, and only the
        # boundary itself decides which one a value of 10 gets.
        raw_percentage = from_plex("100", "rottentomatoes://image.rating.ripe")
        assert raw_percentage is not None
        assert raw_percentage.value == 10.0


class TestRadarrRatings:
    """Radarr hands us five rating sources for free, in a payload we already fetch.

    The shape below is representative of a well-reviewed mainstream film. It has a
    large IMDb vote count, a Tomatometer expressed as a bare percentage, and
    Metacritic with no vote concept at all.
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

    def test_percentages_are_normalized_to_ten(self) -> None:
        rt = pick(from_radarr(self.SAMPLE), RatingSource.ROTTEN_TOMATOES_CRITIC)
        assert rt is not None
        assert rt.value == 9.6  # 96% -> 9.6/10

    def test_a_value_outside_every_known_scale_is_dropped(self) -> None:
        """An IMDb average of 96 is not a rating we know how to read. Guessing a scale
        for it could protect or condemn a file on a fiction.
        """
        assert from_radarr({"imdb": {"votes": 1_000, "value": 96, "type": "user"}}) == []
        assert from_radarr({"rottenTomatoes": {"votes": 0, "value": 250, "type": "user"}}) == []

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
        """Failing closed here means keeping a file rather than deleting it on a number
        we cannot interpret. It also means never protecting on that number either, or
        an unparseable field would silently make everything immortal.
        """
        unknown = Rating(source=RatingSource.UNKNOWN, value=10.0, votes=999_999, provider="?")
        assert unknown.meets(1.0) is False

    def test_describe_always_states_provenance(self) -> None:
        imdb = pick(from_radarr(self.SAMPLE), RatingSource.IMDB)
        assert imdb is not None
        described = imdb.describe()
        assert "imdb" in described
        assert "400,000 votes" in described
        assert "radarr" in described

    def test_both_ends_of_the_scale_are_inside_it(self) -> None:
        """``0.0 <= value_on_ten <= 10.0`` is inclusive at both ends, and the drop test
        above only drove the outside (96 as an average, 250 as a percentage). A 100%
        Tomatometer and a 10.0 average are the best ratings a title can carry, so
        dropping them withdraws the protection from exactly what it exists to keep."""
        perfect_percentage = pick(
            from_radarr({"rottenTomatoes": {"value": 100, "type": "user"}}),
            RatingSource.ROTTEN_TOMATOES_CRITIC,
        )
        assert perfect_percentage is not None
        assert perfect_percentage.value == 10.0

        perfect_average = pick(from_radarr({"tmdb": {"value": 10}}), RatingSource.TMDB)
        assert perfect_average is not None
        assert perfect_average.value == 10.0

        # The bottom edge matters for the panel more than for a protection. A 0 that
        # gets dropped reads as "no Rotten Tomatoes rating" when there is one, and it is
        # zero.
        floor = pick(
            from_radarr({"rottenTomatoes": {"value": 0}}), RatingSource.ROTTEN_TOMATOES_CRITIC
        )
        assert floor is not None
        assert floor.value == 0.0

    def test_a_percentage_source_never_carries_a_vote_count(self) -> None:
        """The sample reports ``votes: 0`` for Rotten Tomatoes, so the test above cannot
        tell dropping the count from reading a zero. A payload carrying a real count
        distinguishes them. A percentage source has no vote concept, and storing one
        would print "from 500 votes" beside a number no votes produced, and would hand a
        vote floor a count to reject it on.
        """
        rt = pick(
            from_radarr({"rottenTomatoes": {"value": 96, "votes": 500}}),
            RatingSource.ROTTEN_TOMATOES_CRITIC,
        )
        assert rt is not None
        assert rt.votes is None
        assert rt.has_meaningful_vote_count is False
        assert rt.meets(9.0, min_votes=1000) is True

    @pytest.mark.parametrize("unreadable", ["3,000", [1], {"count": 1}, "many"])
    def test_an_unreadable_vote_count_costs_that_one_rating_and_nothing_else(
        self, unreadable: object
    ) -> None:
        """A fork or a future schema serializing votes as "3,000" must not raise out of
        the fact build. It costs that one rating its count, and never the previous
        source's count, which is still sitting in the loop variable when the conversion
        raises. A recovery that forgets to clear it would attribute IMDb's 1,200 votes
        to TMDb instead.
        """
        out = from_radarr(
            {
                "imdb": {"value": 8.2, "votes": 1_200},
                "tmdb": {"value": 7.9, "votes": unreadable},
            }
        )

        imdb = pick(out, RatingSource.IMDB)
        assert imdb is not None
        assert imdb.votes == 1_200

        tmdb = pick(out, RatingSource.TMDB)
        assert tmdb is not None
        assert tmdb.value == 7.9
        assert tmdb.votes is None


class TestWhereTheRatingBarTurns:
    """``Rating.meets`` decides whether one rating bar cleared. A bar that stops
    clearing does not refuse a save. It withdraws a protection ``RatingFloorGate`` was
    carrying and hands the file to the reap list.

    Its three comparisons are all inclusive. The value floor (``value >= floor``) was
    already covered by earlier tests. The two comparisons governing the vote floor were
    not, because every case above drove a four-figure count or a dozen, never the
    numbers these comparisons actually turn on.
    """

    @staticmethod
    def imdb(value: float, votes: int | None) -> Rating:
        return Rating(source=RatingSource.IMDB, value=value, votes=votes, provider="radarr")

    def test_a_rating_with_no_vote_count_clears_a_bar_that_asked_for_no_vote_floor(self) -> None:
        """``min_votes > 0`` is the whole of what makes a vote floor optional, and
        ``from_plex`` returns ``votes=None`` for every rating it reads. Applying the vote
        check at ``min_votes=0`` therefore drops the protection off every Plex-sourced
        rating in the library at once."""
        assert self.imdb(8.0, None).meets(7.5) is True
        assert self.imdb(8.0, 0).meets(7.5) is True

    def test_a_vote_floor_of_one_still_applies(self) -> None:
        """The other end of the same comparison. 1 is a legal vote floor an operator can
        set, and it has to take effect. A 9.5 from a single vote is noise, and protecting
        on it keeps junk, so it clears at one vote and not at none.
        """
        assert self.imdb(9.5, 1).meets(7.5, min_votes=1) is True
        assert self.imdb(9.5, 0).meets(7.5, min_votes=1) is False
        assert self.imdb(9.5, None).meets(7.5, min_votes=1) is False

    def test_a_count_exactly_at_the_vote_floor_is_trusted(self) -> None:
        """``votes < min_votes`` refuses, so the floor is inclusive. An operator asking
        for 1,000 votes is asking for a title with exactly 1,000 to count.
        """
        assert self.imdb(8.0, 1_000).meets(7.5, min_votes=1_000) is True
        assert self.imdb(8.0, 999).meets(7.5, min_votes=1_000) is False


class TestUpstreamTimestampShapes:
    """The integrations disagree about how to express an instant."""

    def test_tautulli_added_at_is_a_string_but_last_played_is_an_int(self) -> None:
        """Tautulli returns the same kind of value with two different types in the same
        response. ``added_at`` arrives as a string, ``last_played`` as an int.
        """
        assert from_epoch("1700000000") == from_epoch(1_700_000_000)

    def test_tautulli_never_played_is_none_not_1970(self) -> None:
        """last_played is None or '' for never-played. Coerced to epoch 0, it becomes
        1970, which the scoring engine reads as maximally stale, making never-watched
        media the top deletion candidate. That is exactly backwards.
        """
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
        """An ISO timestamp with no offset is rejected rather than assumed UTC. Guessing
        is how a deletion clock ends up hours off.
        """
        assert from_iso(value) is None
