# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared identity resolver -- the one place an *arr item binds to its Plex row.

Every case here is a way a wrong bind could delete the right file for the wrong reasons
(reading a stranger's watch history). Synthetic data throughout: generic titles, ids
``tt000000N`` / tmdb ``100N`` / tvdb ``200N`` -- never a real title.
"""

from __future__ import annotations

from datetime import UTC, datetime

from reaper.engine.identity import (
    ExternalIds,
    MatchedBy,
    PlexIndex,
    PlexItem,
    parse_guids,
    resolve_movie,
    resolve_show,
    title_year_match,
    to_basename,
)

ADDED = datetime(2020, 1, 1, tzinfo=UTC)


def _item(
    rk: int,
    *,
    title: str = "Example Movie",
    year: int | None = None,
    imdb: object = None,
    tmdb: object = None,
    tvdb: object = None,
    basename: str | None = None,
) -> PlexItem:
    return PlexItem(
        rating_key=rk,
        title=title,
        year=year,
        added_at=ADDED,
        ids=ExternalIds.of(imdb=imdb, tmdb=tmdb, tvdb=tvdb),
        file_basename=basename,
    )


# ---------------------------------------------------------------------------
# Tier 1 -- external id.
# ---------------------------------------------------------------------------


class TestTheIdTierBindsAndDisambiguates:
    def test_a_movie_binds_by_tmdb(self) -> None:
        index = PlexIndex.build([_item(100, tmdb=1001), _item(200, title="Other", tmdb=1002)])
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=None,
            file_basename=None,
            index=index,
        )
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.TMDB

    def test_a_show_binds_by_tvdb(self) -> None:
        index = PlexIndex.build([_item(300, title="Example Show", tvdb=2001)])
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Different Title",
            year=None,
            file_basename=None,
            index=index,
        )
        # Bound by id even though the title differs -- the whole point of the id tier.
        assert res.rating_key == 300
        assert res.matched_by is MatchedBy.TVDB

    def test_the_movie_ladder_falls_from_tmdb_to_imdb(self) -> None:
        """tmdb names nothing in Plex (0 hits) -> the ladder tries imdb, which binds."""
        index = PlexIndex.build([_item(100, imdb="tt0000001")])
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=9999, imdb="tt0000001"),
            title="Example Movie",
            year=None,
            file_basename=None,
            index=index,
        )
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.IMDB

    def test_a_duplicate_id_abstains_and_does_not_fall_through(self) -> None:
        """Two Plex items share one tmdb. Ambiguous -> abstain; must NOT fall through to a
        title+year match that would 'resolve' it by guessing."""
        index = PlexIndex.build(
            [
                _item(100, title="Example Movie", year=2020, tmdb=1001),
                _item(200, title="Example Movie", year=2020, tmdb=1001),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename=None,
            index=index,
        )
        assert res.rating_key is None
        assert res.matched_by is None
        assert "ambiguous" in res.detail.lower()

    def test_an_id_naming_nothing_falls_through_to_title(self) -> None:
        index = PlexIndex.build([_item(100, title="Example Movie", year=2020)])
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename=None,
            index=index,
        )
        # tmdb 1001 names nothing -> silence -> title+year binds.
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.TITLE_YEAR


# ---------------------------------------------------------------------------
# The contradiction veto -- corroborate-or-silent, never contradict.
# ---------------------------------------------------------------------------


class TestTheContradictionVeto:
    def test_id_and_title_pointing_to_different_rows_abstains(self) -> None:
        """Tier 1 resolves rk=100, Tier 3 resolves rk=200. Positive disagreement -> keep."""
        index = PlexIndex.build(
            [
                _item(100, title="A Title", year=2020, tmdb=1001),
                _item(200, title="Example Movie", year=2020),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename=None,
            index=index,
        )
        assert res.rating_key is None
        assert "disagree" in res.detail.lower()

    def test_id_and_title_agreeing_binds_by_id(self) -> None:
        """Both tiers point at the SAME row -- the normal healthy case. Bind, crediting the
        stronger (id) provenance, not title+year."""
        index = PlexIndex.build([_item(100, title="Example Movie", year=2020, tmdb=1001)])
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename=None,
            index=index,
        )
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.TMDB

    def test_a_title_mismatch_is_silence_not_contradiction(self) -> None:
        """The id row's title differs from the query title, so Tier 3 finds nothing. That
        is silence, not a contradiction -- the id still binds."""
        index = PlexIndex.build([_item(100, title="Regional Title", year=2020, tmdb=1001)])
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Original Title",
            year=2020,
            file_basename=None,
            index=index,
        )
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.TMDB


# ---------------------------------------------------------------------------
# Tier 2 -- file basename.
# ---------------------------------------------------------------------------


class TestTheBasenameTier:
    def test_a_unique_basename_binds_when_no_id(self) -> None:
        index = PlexIndex.build(
            [_item(100, title="Nope", basename="/media/movies/example (2020).mkv")]
        )
        res = resolve_movie(
            ids=ExternalIds(),
            title="Example Movie",
            year=None,
            file_basename="/movies/Example (2020).mkv",  # different mount root, same leaf
            index=index,
        )
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.FILE_BASENAME

    def test_a_duplicate_basename_abstains(self) -> None:
        index = PlexIndex.build(
            [
                _item(100, title="A", basename="example.mkv"),
                _item(200, title="B", basename="example.mkv"),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds(), title="Example", year=None, file_basename="example.mkv", index=index
        )
        assert res.rating_key is None
        assert "ambiguous" in res.detail.lower()


# ---------------------------------------------------------------------------
# Tier 3 -- title + year (migrated from the old _match_plex_movie fixtures so there is
# exactly one implementation under test).
# ---------------------------------------------------------------------------


class TestTitleYearIsTheBackstop:
    """Two films can share a title. Resolve by year, refuse on any ambiguity."""

    def _index(self) -> PlexIndex:
        return PlexIndex.build(
            [
                _item(11, title="Duplicate Title", year=1999),
                _item(22, title="Duplicate Title", year=2017),
            ]
        )

    def test_a_matching_year_binds_to_the_right_row(self) -> None:
        assert title_year_match("Duplicate Title", 1999, self._index()) == 11
        assert title_year_match("Duplicate Title", 2017, self._index()) == 22

    def test_a_duplicate_title_with_no_year_refuses(self) -> None:
        assert title_year_match("Duplicate Title", None, self._index()) is None

    def test_a_duplicate_title_with_an_unmatched_year_refuses(self) -> None:
        assert title_year_match("Duplicate Title", 1932, self._index()) is None

    def test_a_single_row_with_a_conflicting_year_refuses(self) -> None:
        index = PlexIndex.build([_item(5, title="Lone Title", year=2002)])
        assert title_year_match("Lone Title", 1972, index) is None

    def test_a_single_row_binds_when_a_year_is_missing_on_either_side(self) -> None:
        index = PlexIndex.build([_item(7, title="A Film", year=None)])
        assert title_year_match("A Film", 2010, index) == 7
        assert title_year_match("A Film", None, index) == 7

    def test_resolve_falls_to_title_when_the_item_has_no_ids(self) -> None:
        res = resolve_movie(
            ids=ExternalIds(),
            title="Duplicate Title",
            year=2017,
            file_basename=None,
            index=self._index(),
        )
        assert res.rating_key == 22
        assert res.matched_by is MatchedBy.TITLE_YEAR


# ---------------------------------------------------------------------------
# GUID parsing -- new agents, legacy agents, sentinels, non-id agents.
# ---------------------------------------------------------------------------


class TestParseGuids:
    def test_new_agent_guids(self) -> None:
        ids = parse_guids(["imdb://tt0000001", "tmdb://1001", "tvdb://2001"])
        assert ids == ExternalIds(imdb="tt0000001", tmdb=1001, tvdb=2001)

    def test_legacy_guid_with_lang_suffix(self) -> None:
        ids = parse_guids([], legacy_guid="com.plexapp.agents.imdb://tt0000001?lang=en")
        assert ids.imdb == "tt0000001"

    def test_legacy_themoviedb_and_thetvdb(self) -> None:
        assert (
            parse_guids([], legacy_guid="com.plexapp.agents.themoviedb://1001?lang=en").tmdb == 1001
        )
        # A legacy TheTVDB guid can carry a season/episode path tail.
        assert (
            parse_guids([], legacy_guid="com.plexapp.agents.thetvdb://2001/1/2?lang=en").tvdb
            == 2001
        )

    def test_plex_agent_guid_carries_no_external_id(self) -> None:
        assert parse_guids(["plex://movie/5d776b9ad"]).empty
        assert parse_guids(["com.plexapp.agents.none://0"]).empty
        assert parse_guids(["local://12345"]).empty

    def test_new_agent_wins_over_legacy_but_legacy_fills_gaps(self) -> None:
        ids = parse_guids(["tmdb://1001"], legacy_guid="com.plexapp.agents.imdb://tt0000009")
        assert ids.tmdb == 1001
        assert ids.imdb == "tt0000009"

    def test_sentinels_are_treated_as_absent(self) -> None:
        assert parse_guids(["imdb://tt0000000", "tmdb://0"]).empty
        assert ExternalIds.of(imdb="tt0", tmdb="0", tvdb="").empty
        assert ExternalIds.of(imdb="tt0000000").imdb is None


class TestToBasename:
    def test_it_reduces_and_lowercases(self) -> None:
        assert to_basename("/media/movies/Example (2020).mkv") == "example (2020).mkv"
        assert to_basename("C:\\Movies\\Example.mkv") == "example.mkv"
        assert to_basename(None) is None
        assert to_basename("") is None


# ---------------------------------------------------------------------------
# The safety invariant -- distinct ids never share a bound key; same id may.
# ---------------------------------------------------------------------------


class TestTheBindingInvariant:
    def test_two_arr_items_with_different_ids_never_share_a_key(self) -> None:
        """Across a synthetic library, no two distinct-id films resolve to the same Plex
        rating key -- the property that keeps one film from reading another's history."""
        index = PlexIndex.build(
            [
                _item(100, title="First", year=2001, tmdb=1001),
                _item(200, title="Second", year=2002, tmdb=1002),
                _item(300, title="Third", year=2003, tmdb=1003),
            ]
        )
        arr_items = [
            ExternalIds.of(tmdb=1001),
            ExternalIds.of(tmdb=1002),
            ExternalIds.of(tmdb=1003),
        ]
        bound: dict[int, ExternalIds] = {}
        for ids in arr_items:
            res = resolve_movie(ids=ids, title="", year=None, file_basename=None, index=index)
            assert res.rating_key is not None
            assert res.rating_key not in bound  # no collision across distinct ids
            bound[res.rating_key] = ids

    def test_multi_instance_same_id_shares_one_key(self) -> None:
        """The 4K + HD case: two arr items (two files) carry the SAME id and both bind the
        one Plex row. Safe -- deletes route by media_key, and both read the same (protective)
        watch history."""
        index = PlexIndex.build([_item(100, title="Example", year=2020, tmdb=1001)])
        hd = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example",
            year=2020,
            file_basename=None,
            index=index,
        )
        uhd = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example",
            year=2020,
            file_basename=None,
            index=index,
        )
        assert hd.rating_key == uhd.rating_key == 100
