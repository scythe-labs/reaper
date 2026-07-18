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
    MatchStatus,
    PlexFile,
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
    size: int | None = None,
    files: tuple[PlexFile, ...] | None = None,
    added: datetime | None = None,
) -> PlexItem:
    return PlexItem(
        rating_key=rk,
        title=title,
        year=year,
        added_at=added if added is not None else ADDED,
        ids=ExternalIds.of(imdb=imdb, tmdb=tmdb, tvdb=tvdb),
        file_basename=basename,
        # Mirror the production builders: both file fields derive from one media list, so
        # a single-file item's file set defaults to its one basename (+ optional size).
        files=files if files is not None else ((PlexFile(basename, size),) if basename else ()),
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
# Narrowing an ambiguous id by the *arr's own file name -- corroboration inside the
# id's candidate set, never a consult of the wider library.
# ---------------------------------------------------------------------------


class TestAnAmbiguousIdNarrowedByFileName:
    """A split library holds the same content as several Plex copies (an HD and a 4K
    section, a curated section re-listing a title), so one id names 2+ rating keys. The
    *arr item's file name may pick which copy this entry manages; anything less than
    exactly one match keeps abstaining."""

    def _split_library(self) -> PlexIndex:
        # The same movie in two sections; the file names carry the quality marker.
        return PlexIndex.build(
            [
                _item(100, year=2020, tmdb=1001, basename="example (2020) 1080p.mkv"),
                _item(200, year=2020, tmdb=1001, basename="example (2020) 2160p.mkv"),
            ]
        )

    def test_a_file_name_matching_exactly_one_copy_binds_it(self) -> None:
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="/movies/Example (2020)/example (2020) 1080p.mkv",
            index=self._split_library(),
        )
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.ID_AND_BASENAME
        assert res.status is MatchStatus.MATCHED
        # The audit detail names both signals: the id and the file name that picked the copy.
        assert "TMDB id 1001" in res.detail
        assert "example (2020) 1080p.mkv" in res.detail

    def test_two_instances_each_bind_their_own_copy(self) -> None:
        """The HD and 4K Radarr instances carry the same tmdb id but manage different
        files -- each must bind its own Plex row, never the sibling's."""
        index = self._split_library()
        hd = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="/movies-hd/Example (2020)/Example (2020) 1080p.mkv",
            index=index,
        )
        uhd = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="/movies-4k/Example (2020)/Example (2020) 2160p.mkv",
            index=index,
        )
        assert hd.rating_key == 100
        assert uhd.rating_key == 200

    def test_a_file_name_matching_none_abstains(self) -> None:
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example (2020) remux.mkv",
            index=self._split_library(),
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS
        assert "matches none" in res.detail

    def test_a_file_name_matching_both_abstains_without_sizes(self) -> None:
        """The curated-section case: the same physical file re-listed, so both copies
        carry the same leaf. With no file size on either side there is nothing left to
        corroborate with, so it must keep abstaining. (With exact sizes the twins merge:
        see TestByteIdenticalTwinListings.)"""
        index = PlexIndex.build(
            [
                _item(100, year=2020, tmdb=1001, basename="example (2020).mkv"),
                _item(200, year=2020, tmdb=1001, basename="example (2020).mkv"),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example (2020).mkv",
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS
        assert "matches 2" in res.detail

    def test_one_of_three_copies_binds_and_two_of_three_abstains(self) -> None:
        index = PlexIndex.build(
            [
                _item(100, year=2020, tmdb=1001, basename="example a.mkv"),
                _item(200, year=2020, tmdb=1001, basename="example b.mkv"),
                _item(300, year=2020, tmdb=1001, basename="example b.mkv"),
            ]
        )
        one = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example a.mkv",
            index=index,
        )
        assert one.rating_key == 100
        two = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example b.mkv",
            index=index,
        )
        assert two.rating_key is None
        assert "matches 2" in two.detail

    def test_an_item_with_no_file_name_abstains(self) -> None:
        for missing in (None, ""):
            res = resolve_movie(
                ids=ExternalIds.of(tmdb=1001),
                title="Example Movie",
                year=2020,
                file_basename=missing,
                index=self._split_library(),
            )
            assert res.rating_key is None
            assert res.status is MatchStatus.AMBIGUOUS
            assert "no file name" in res.detail

    def test_a_copy_with_unknown_files_abstains(self) -> None:
        """One candidate's files could not be seen. That copy might be the very file
        this item manages, so "could not look" never counts as "looked and it was
        different" -- even though the other copy's name matches."""
        index = PlexIndex.build(
            [
                _item(100, year=2020, tmdb=1001, basename="example (2020) 1080p.mkv"),
                _item(200, year=2020, tmdb=1001, basename=None),  # sweep saw no files
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example (2020) 1080p.mkv",
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS
        assert "unknown" in res.detail

    def test_a_merged_copy_is_compared_by_all_its_files(self) -> None:
        """One Plex row can merge several editions (several files). A re-list of its
        SECOND file must not look 'unique' just because the merged row is indexed by its
        first -- narrowing sees every file, finds two owners, and abstains."""
        index = PlexIndex.build(
            [
                _item(
                    100,
                    year=2020,
                    tmdb=1001,
                    basename="example.mkv",
                    files=(PlexFile("example.mkv"), PlexFile("example 4k.mkv")),
                ),
                _item(200, year=2020, tmdb=1001, basename="example 4k.mkv"),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example 4k.mkv",
            index=index,
        )
        assert res.rating_key is None
        assert "matches 2" in res.detail

    def test_a_merged_copy_still_binds_when_only_it_owns_the_file(self) -> None:
        index = PlexIndex.build(
            [
                _item(
                    100,
                    year=2020,
                    tmdb=1001,
                    basename="example.mkv",
                    files=(PlexFile("example.mkv"), PlexFile("example 4k.mkv")),
                ),
                _item(200, year=2020, tmdb=1001, basename="example remux.mkv"),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example 4k.mkv",
            index=index,
        )
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.ID_AND_BASENAME

    def test_a_failed_narrow_never_falls_through_to_the_wider_library(self) -> None:
        """THE no-fall-through property. The arr file name matches none of the id's
        candidates, but it WOULD uniquely match a third Plex item in the global basename
        map -- and title+year would uniquely resolve that same third item. Falling
        through would bind outside the id's candidate set: a guess. Abstain."""
        index = PlexIndex.build(
            [
                _item(100, title="Same Title", year=2020, tmdb=1001, basename="copy a.mkv"),
                _item(200, title="Same Title", year=2020, tmdb=1001, basename="copy b.mkv"),
                # The out-of-set item: unique basename, unique title+year.
                _item(300, title="Other Title", year=2020, basename="elsewhere.mkv"),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Other Title",
            year=2020,
            file_basename="elsewhere.mkv",
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS
        # And a bind produced by narrowing stays inside the candidate set.
        narrowed = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Same Title",
            year=2020,
            file_basename="copy a.mkv",
            index=index,
        )
        assert narrowed.rating_key == 100

    def test_a_narrowed_bind_is_still_vetoed_by_a_contradicting_title(self) -> None:
        """The contradiction veto survives narrowing: the file name picks one copy, but
        title+year positively resolves a DIFFERENT, third row. Two tiers disagreeing is
        still a keep."""
        index = PlexIndex.build(
            [
                _item(100, title="Split Title", year=2020, tmdb=1001, basename="copy a.mkv"),
                _item(200, title="Split Title", year=2020, tmdb=1001, basename="copy b.mkv"),
                _item(300, title="Example Movie", year=2020),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="copy a.mkv",
            index=index,
        )
        assert res.rating_key is None
        assert "disagree" in res.detail

    def test_narrowing_runs_on_the_second_id_kind_too(self) -> None:
        """tmdb names nothing in Plex -> the ladder tries imdb, which names two copies;
        narrowing applies to whichever id kind produced the candidates."""
        index = PlexIndex.build(
            [
                _item(100, year=2020, imdb="tt0000001", basename="example a.mkv"),
                _item(200, year=2020, imdb="tt0000001", basename="example b.mkv"),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=9999, imdb="tt0000001"),
            title="Example Movie",
            year=2020,
            file_basename="example a.mkv",
            index=index,
        )
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.ID_AND_BASENAME
        assert "IMDB id tt0000001" in res.detail

    def test_narrowing_normalizes_both_sides(self) -> None:
        """Candidates may carry full paths and different case; the comparison must go
        through the one shared normalizer on both sides."""
        index = PlexIndex.build(
            [
                _item(
                    100,
                    year=2020,
                    tmdb=1001,
                    basename="/media/movies/Example (2020) 1080p.mkv",
                    files=(PlexFile("/media/movies/Example (2020) 1080p.mkv"),),
                ),
                _item(
                    200,
                    year=2020,
                    tmdb=1001,
                    basename="/media/movies-4k/Example (2020) 2160p.mkv",
                    files=(PlexFile("/media/movies-4k/Example (2020) 2160p.mkv"),),
                ),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="C:\\Movies\\EXAMPLE (2020) 1080P.mkv",
            index=index,
        )
        assert res.rating_key == 100

    def test_a_show_narrows_by_its_folder_name(self) -> None:
        """Sonarr's leaf is the series folder. Split TV sections with per-instance
        folder conventions narrow the same way movies do."""
        index = PlexIndex.build(
            [
                _item(300, title="Example Show", tvdb=2001, basename="example show"),
                _item(400, title="Example Show", tvdb=2001, basename="example show (2160p)"),
            ]
        )
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/tv-4k/Example Show (2160p)",
            index=index,
        )
        assert res.rating_key == 400
        assert res.matched_by is MatchedBy.ID_AND_BASENAME

    def test_a_unique_id_bind_keeps_its_exact_provenance(self) -> None:
        """The narrowing refactor must not shift the unique-hit path by a byte: same
        rating key, same MatchedBy, same detail string as before."""
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
        assert res.detail == "Bound to Plex item by TMDB id 1001"


# ---------------------------------------------------------------------------
# A file name matching SEVERAL candidates: the exact byte size is the corroborator left.
# ---------------------------------------------------------------------------


class TestByteIdenticalTwinListings:
    """When the file name matches several of an id's candidates, the *arr's exact byte
    size decides. A size singling out one listing binds it; several listings at exactly
    that size are byte-identical twins of the *arr's own file (verified live: a curated
    section re-lists the very file under its own rating key, at another path) and bind as
    one GROUP, every listing's key kept so watch reads cover them all. Any unknown on
    either side keeps abstaining."""

    def _relisted_library(self) -> PlexIndex:
        # One file listed twice (a movie section plus a curated re-list): same name, same
        # exact size, listed years apart. Plus the 4K sibling with its own name and size.
        return PlexIndex.build(
            [
                _item(
                    100,
                    year=2020,
                    tmdb=1001,
                    basename="example (2020).mkv",
                    size=7_000,
                    added=datetime(2015, 1, 1, tzinfo=UTC),
                ),
                _item(
                    200,
                    year=2020,
                    tmdb=1001,
                    basename="example (2020).mkv",
                    size=7_000,
                    added=datetime(2021, 6, 1, tzinfo=UTC),
                ),
                _item(300, year=2020, tmdb=1001, basename="example (2020) 2160p.mkv", size=70_000),
            ]
        )

    def test_twin_listings_bind_as_a_merged_group(self) -> None:
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example (2020).mkv",
            file_size=7_000,
            index=self._relisted_library(),
        )
        assert res.status is MatchStatus.MATCHED
        assert res.matched_by is MatchedBy.MERGED_LISTINGS
        # Canonical = the earliest listing; the group carries every listing's key.
        assert res.rating_key == 100
        assert res.merged_rating_keys == (100, 200)
        assert "listed 2 times" in res.detail

    def test_the_canonical_key_is_the_earliest_listed(self) -> None:
        """Deterministic and honest: the original listing (a curated re-list comes years
        later) draws the poster and gives dormancy its floor, whatever the key order."""
        index = PlexIndex.build(
            [
                _item(
                    100,
                    year=2020,
                    tmdb=1001,
                    basename="example.mkv",
                    size=7_000,
                    added=datetime(2022, 1, 1, tzinfo=UTC),
                ),
                _item(
                    200,
                    year=2020,
                    tmdb=1001,
                    basename="example.mkv",
                    size=7_000,
                    added=datetime(2012, 1, 1, tzinfo=UTC),
                ),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example.mkv",
            file_size=7_000,
            index=index,
        )
        assert res.rating_key == 200
        assert res.merged_rating_keys == (100, 200)

    def test_a_size_singling_out_one_listing_binds_it(self) -> None:
        """Two same-name listings at DIFFERENT sizes (a re-list gone stale after an
        upgrade): the *arr's exact size picks its own file's listing, no merge."""
        index = PlexIndex.build(
            [
                _item(100, year=2020, tmdb=1001, basename="example.mkv", size=7_000),
                _item(200, year=2020, tmdb=1001, basename="example.mkv", size=5_000),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example.mkv",
            file_size=7_000,
            index=index,
        )
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.ID_AND_BASENAME
        assert res.merged_rating_keys == ()
        assert "exact file size" in res.detail

    def test_no_arr_size_keeps_abstaining(self) -> None:
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example (2020).mkv",
            index=self._relisted_library(),
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS
        assert "no file size" in res.detail

    def test_an_unknown_listing_size_keeps_abstaining(self) -> None:
        """One matching listing's size could not be seen. It might be the same file; it
        might not. "Could not look" is never "looked and it was different"."""
        index = PlexIndex.build(
            [
                _item(100, year=2020, tmdb=1001, basename="example.mkv", size=7_000),
                _item(200, year=2020, tmdb=1001, basename="example.mkv", size=None),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example.mkv",
            file_size=7_000,
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS
        assert "file size is unknown" in res.detail

    def test_a_size_matching_no_listing_keeps_abstaining(self) -> None:
        index = PlexIndex.build(
            [
                _item(100, year=2020, tmdb=1001, basename="example.mkv", size=7_000),
                _item(200, year=2020, tmdb=1001, basename="example.mkv", size=5_000),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example.mkv",
            file_size=6_000,
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS
        assert "none of those files is the same size" in res.detail

    def test_two_twins_merge_while_the_odd_size_stays_out(self) -> None:
        """Three same-name listings: two byte-identical twins and one at another size.
        The twins merge; the odd listing never enters the group."""
        index = PlexIndex.build(
            [
                _item(100, year=2020, tmdb=1001, basename="example.mkv", size=7_000),
                _item(200, year=2020, tmdb=1001, basename="example.mkv", size=7_000),
                _item(300, year=2020, tmdb=1001, basename="example.mkv", size=5_000),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example.mkv",
            file_size=7_000,
            index=index,
        )
        assert res.rating_key == 100
        assert res.merged_rating_keys == (100, 200)

    def test_a_unique_name_match_binds_without_a_size_check(self) -> None:
        """The name alone identifying one candidate binds it even when the recorded size
        disagrees (Plex metadata lags a file upgrade). Size is consulted only to break a
        name tie, never to veto the established single-match path."""
        index = PlexIndex.build(
            [
                _item(100, year=2020, tmdb=1001, basename="example a.mkv", size=5_000),
                _item(200, year=2020, tmdb=1001, basename="example b.mkv", size=9_000),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example a.mkv",
            file_size=7_000,
            index=index,
        )
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.ID_AND_BASENAME

    def test_a_title_resolving_to_the_other_twin_is_agreement(self) -> None:
        """Tier 3 resolves to the group's OTHER listing (only the newer twin carries the
        year, so title+year singles it out). A hit inside the merged group corroborates
        the bind; it must not read as a contradiction with the canonical key."""
        index = PlexIndex.build(
            [
                _item(
                    100,
                    title="Twin Title",
                    year=None,
                    tmdb=1001,
                    basename="example.mkv",
                    size=7_000,
                    added=datetime(2012, 1, 1, tzinfo=UTC),
                ),
                _item(
                    200,
                    title="Twin Title",
                    year=2020,
                    tmdb=1001,
                    basename="example.mkv",
                    size=7_000,
                    added=datetime(2022, 1, 1, tzinfo=UTC),
                ),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Twin Title",
            year=2020,
            file_basename="example.mkv",
            file_size=7_000,
            index=index,
        )
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.MERGED_LISTINGS
        assert res.merged_rating_keys == (100, 200)

    def test_a_merged_bind_is_still_vetoed_by_an_out_of_group_title(self) -> None:
        """The contradiction veto survives the merge: title+year positively resolves a
        third row OUTSIDE the group. Two tiers disagreeing is still a keep."""
        index = PlexIndex.build(
            [
                _item(100, title="Split Title", year=2020, tmdb=1001, basename="c.mkv", size=7_000),
                _item(200, title="Split Title", year=2020, tmdb=1001, basename="c.mkv", size=7_000),
                _item(300, title="Example Movie", year=2020),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="c.mkv",
            file_size=7_000,
            index=index,
        )
        assert res.rating_key is None
        assert "disagree" in res.detail

    def test_a_show_never_merges_its_folders(self) -> None:
        """A show is bound by its folder, and a folder has no one size -- so two
        same-name folder listings under one id keep abstaining, never merge."""
        index = PlexIndex.build(
            [
                _item(300, title="Example Show", tvdb=2001, basename="example show"),
                _item(400, title="Example Show", tvdb=2001, basename="example show"),
            ]
        )
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/tv/Example Show",
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS
        assert "no file size" in res.detail


# ---------------------------------------------------------------------------
# The folder corroborator -- the only thing a show has once the leaf ties.
# ---------------------------------------------------------------------------


class TestTheFolderTellsTwoListingsApart:
    """One title kept in two libraries (an HD one and a 4K one) is listed twice in Plex
    under one tvdb id, with an identical leaf folder. Only the segment above the leaf
    differs, and comparing trailing segments survives the mount-root difference.
    """

    @staticmethod
    def _two_sections() -> PlexIndex:
        return PlexIndex.build(
            [
                _item(
                    300,
                    title="Example Show",
                    tvdb=2001,
                    basename="example show",
                    files=(PlexFile("example show", None, "/media/tv/Example Show"),),
                ),
                _item(
                    400,
                    title="Example Show",
                    tvdb=2001,
                    basename="example show",
                    files=(PlexFile("example show", None, "/media/tv-4k/Example Show"),),
                ),
            ]
        )

    def test_the_parent_folder_binds_the_right_copy(self) -> None:
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/tv-4k/Example Show",
            file_path="/tv-4k/Example Show",
            index=self._two_sections(),
        )
        assert res.rating_key == 400
        assert res.matched_by is MatchedBy.ID_AND_BASENAME
        assert "folder" in res.detail

    def test_the_other_instance_binds_the_other_copy(self) -> None:
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/tv/Example Show",
            file_path="/tv/Example Show",
            index=self._two_sections(),
        )
        assert res.rating_key == 300

    def test_identical_paths_still_abstain(self) -> None:
        """Two listings of the very same folder tie at every depth, and a tie is not
        evidence for either. Fail closed, exactly as before this corroborator existed."""
        index = PlexIndex.build(
            [
                _item(
                    300,
                    title="Example Show",
                    tvdb=2001,
                    basename="example show",
                    files=(PlexFile("example show", None, "/media/tv/Example Show"),),
                ),
                _item(
                    400,
                    title="Example Show",
                    tvdb=2001,
                    basename="example show",
                    files=(PlexFile("example show", None, "/media/tv/Example Show"),),
                ),
            ]
        )
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/tv/Example Show",
            file_path="/tv/Example Show",
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS

    def test_a_listing_with_no_path_cannot_win_but_can_still_force_an_abstain(self) -> None:
        """ "Could not look" is never "looked and it was different": an unreadable path
        scores zero, so it never wins -- but it also never clears the way for the other."""
        index = PlexIndex.build(
            [
                _item(
                    300,
                    title="Example Show",
                    tvdb=2001,
                    basename="example show",
                    files=(PlexFile("example show", None, None),),
                ),
                _item(
                    400,
                    title="Example Show",
                    tvdb=2001,
                    basename="example show",
                    files=(PlexFile("example show", None, None),),
                ),
            ]
        )
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/tv-4k/Example Show",
            file_path="/tv-4k/Example Show",
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS

    def test_a_matching_leaf_alone_is_never_enough(self) -> None:
        """Depth 1 is the leaf both sides already matched on, so it is no new
        corroboration: an *arr path with no folder above the leaf cannot break the tie."""
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="Example Show",
            file_path="Example Show",
            index=self._two_sections(),
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS

    def test_a_movie_still_prefers_its_exact_size(self) -> None:
        """The folder is tried first, but where it cannot narrow, size still decides --
        the movie path keeps every corroborator it had."""
        index = PlexIndex.build(
            [
                _item(
                    100,
                    tmdb=1001,
                    basename="example.mkv",
                    files=(PlexFile("example.mkv", 111, "/media/movies/Example/example.mkv"),),
                ),
                _item(
                    200,
                    tmdb=1001,
                    basename="example.mkv",
                    files=(PlexFile("example.mkv", 222, "/media/movies/Example/example.mkv"),),
                ),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=None,
            file_basename="example.mkv",
            file_size=222,
            file_path="/movies/Example/example.mkv",
            index=index,
        )
        assert res.rating_key == 200


# ---------------------------------------------------------------------------
# The contradiction veto -- corroborate-or-silent, never contradict.
# ---------------------------------------------------------------------------


class TestTheContradictionVeto:
    def test_two_id_kinds_pointing_to_different_rows_abstains(self) -> None:
        """Tier 1's own kinds cross-check each other: the tmdb id binds one row, the
        imdb id names a different one. A mis-tagged external id with no way to know
        which is wrong -> keep."""
        index = PlexIndex.build(
            [
                _item(100, title="A Title", year=2020, tmdb=1001),
                _item(200, title="Another Title", year=1999, imdb="tt0000002"),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001, imdb="tt0000002"),
            title=None,
            year=None,
            file_basename=None,
            index=index,
        )
        assert res.rating_key is None
        assert "contradict" in res.detail.lower()

    def test_two_id_kinds_agreeing_bind_by_the_first(self) -> None:
        index = PlexIndex.build([_item(100, title="A Title", tmdb=1001, imdb="tt0000001")])
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001, imdb="tt0000001"),
            title=None,
            year=None,
            file_basename=None,
            index=index,
        )
        assert res.rating_key == 100

    def test_a_second_kind_unknown_to_plex_does_not_veto(self) -> None:
        """An id kind that names nothing in Plex is silence, not disagreement."""
        index = PlexIndex.build([_item(100, title="A Title", tmdb=1001)])
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001, imdb="tt0009999"),
            title=None,
            year=None,
            file_basename=None,
            index=index,
        )
        assert res.rating_key == 100

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
