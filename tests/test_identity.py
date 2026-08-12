# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared identity resolver -- the one place an *arr item binds to its Plex row.

Every case here is a way a wrong bind could delete the right file for the wrong reasons
(reading a stranger's watch history). Synthetic data throughout: generic titles, ids
``tt000000N`` / tmdb ``100N`` / tvdb ``200N`` -- never a real title.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from reaper.engine.identity import (
    ExternalIds,
    MatchedBy,
    MatchStatus,
    PlexFile,
    PlexIndex,
    PlexItem,
    candidate_libraries,
    libraries_for_ids,
    library_for_path,
    parse_guids,
    resolve_movie,
    resolve_show,
    root_folder_paths,
    title_year_match,
    to_basename,
)
from reaper.text import fold

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
    library: str | None = None,
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
        library=library,
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
        # Bound by id even though the title differs -- exactly what the id tier is for.
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
        # Genuine multiplicity keeps the wording it always had -- this is what CONFLICTED
        # was split off FROM, so a change that reworded both would have said nothing new.
        assert res.candidate_rating_keys == (100, 200)

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

    def _relisted_library_with_paths(self) -> PlexIndex:
        # The same one-file-listed-twice shape, with the paths the live check found: the
        # curated re-list sits in another folder entirely, so the folder corroborator
        # would happily pick the main listing alone if it were allowed to run.
        return PlexIndex.build(
            [
                _item(
                    100,
                    year=2020,
                    tmdb=1001,
                    added=datetime(2015, 1, 1, tzinfo=UTC),
                    files=(
                        PlexFile("example (2020).mkv", 7_000, "/data/movies/Example/example.mkv"),
                    ),
                ),
                _item(
                    200,
                    year=2020,
                    tmdb=1001,
                    added=datetime(2021, 6, 1, tzinfo=UTC),
                    files=(
                        PlexFile(
                            "example (2020).mkv", 7_000, "/data/curated/Example (2020)/example.mkv"
                        ),
                    ),
                ),
            ]
        )

    def test_divergent_paths_never_break_the_twin_group_up(self) -> None:
        """The two listings of one file sit in different folders, and the *arr's path is
        deeper under one of them. The folder corroborator must stand aside: it returns a
        single listing, and binding one twin alone hides every play made through the
        other, which under-counts watching and condemns a file people watch."""
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename="example (2020).mkv",
            file_size=7_000,
            file_path="/movies/Example/example.mkv",
            index=self._relisted_library_with_paths(),
        )
        assert res.status is MatchStatus.MATCHED
        assert res.matched_by is MatchedBy.MERGED_LISTINGS
        assert res.rating_key == 100
        assert res.merged_rating_keys == (100, 200)

    def test_twins_merge_even_when_the_root_makes_the_folder_evidence_readable(self) -> None:
        """The ordering check in its strongest form. Roots ARE supplied and the folder
        evidence genuinely singles out one twin, so the folder step could bind one listing
        alone if it ran first. _would_merge_as_twins runs before it and stands it down, so
        the group forms and every play through either listing is still counted."""
        index = PlexIndex.build(
            [
                _item(
                    100,
                    tmdb=1001,
                    added=datetime(2015, 1, 1, tzinfo=UTC),
                    files=(PlexFile("example.mkv", 7_000, "/data/movies/Example/example.mkv"),),
                ),
                _item(
                    200,
                    tmdb=1001,
                    added=datetime(2021, 6, 1, tzinfo=UTC),
                    files=(PlexFile("example.mkv", 7_000, "/data/curated/Other/example.mkv"),),
                ),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=None,
            file_basename="example.mkv",
            file_size=7_000,
            file_path="/srv/movies/Example/example.mkv",
            root_folders=("/srv/movies",),
            index=index,
        )
        assert res.matched_by is MatchedBy.MERGED_LISTINGS
        assert res.rating_key == 100
        assert res.merged_rating_keys == (100, 200)

    def test_a_twin_group_survives_even_when_a_third_copy_is_a_different_size(self) -> None:
        """Two twins plus one listing at another size: the folder step still stands aside,
        so the group forms and the odd copy stays out of it."""
        index = PlexIndex.build(
            [
                _item(
                    100,
                    tmdb=1001,
                    files=(PlexFile("example.mkv", 7_000, "/data/movies/Example/example.mkv"),),
                ),
                _item(
                    200,
                    tmdb=1001,
                    files=(PlexFile("example.mkv", 7_000, "/data/curated/Other/example.mkv"),),
                ),
                _item(
                    300,
                    tmdb=1001,
                    files=(PlexFile("example.mkv", 5_000, "/data/movies-4k/Example/example.mkv"),),
                ),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=None,
            file_basename="example.mkv",
            file_size=7_000,
            file_path="/movies/Example/example.mkv",
            index=index,
        )
        assert res.matched_by is MatchedBy.MERGED_LISTINGS
        assert res.merged_rating_keys == (100, 200)

    def test_an_unknown_size_beside_a_twin_pair_abstains_rather_than_binding_one(self) -> None:
        """Two twins plus a third matching listing whose size Plex never reported. The
        folder step still stands aside, so this abstains instead of binding one twin and
        hiding the other's plays. Both ways of resolving it keep the file."""
        index = PlexIndex.build(
            [
                _item(
                    100,
                    tmdb=1001,
                    files=(PlexFile("example.mkv", 7_000, "/data/movies/Example/example.mkv"),),
                ),
                _item(
                    200,
                    tmdb=1001,
                    files=(PlexFile("example.mkv", 7_000, "/data/curated/Other/example.mkv"),),
                ),
                _item(
                    300,
                    tmdb=1001,
                    files=(PlexFile("example.mkv", None, "/data/movies-4k/Example/example.mkv"),),
                ),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=None,
            file_basename="example.mkv",
            file_size=7_000,
            file_path="/movies/Example/example.mkv",
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS

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
    """One title kept in two libraries is listed twice in Plex under one id, with an
    identical leaf name. A folder ABOVE the leaf can still tell the copies apart, but only
    where it sits strictly below the *arr instance's own root folder, which the instance
    reports and the resolver is handed.

    The *arr root is never inferred from the path's shape: a container root may be one
    segment or three, so a fixed strip leaves root pieces to be compared as if they were
    real folders. With no root supplied the corroborator stands down entirely.

    **Movies only, and that is not an oversight.** Radarr writes ``<root>/<Title>/<file>``,
    so a real title folder sits above the leaf. Sonarr writes ``<root>/<Show>``: below a
    correct root there is only the show folder, which IS the leaf both sides already
    matched on, so there is no new evidence and the step stands down for every show. See
    TestAShowNeverGetsFolderEvidence, which pins that.
    """

    #: Radarr's own layout: the title folder is what sits below the root.
    _ARR_ROOTS = ("/data/movies",)

    @staticmethod
    def _two_sections() -> PlexIndex:
        return PlexIndex.build(
            [
                _item(
                    300,
                    title="Example Movie",
                    tmdb=2001,
                    files=(PlexFile("film.mkv", 7_000, "/media/movies/Example Movie/film.mkv"),),
                ),
                _item(
                    400,
                    title="Example Movie",
                    tmdb=2001,
                    files=(PlexFile("film.mkv", 70_000, "/media/movies-4k/Other Folder/film.mkv"),),
                ),
            ]
        )

    def test_a_real_folder_below_the_root_binds_the_right_copy(self) -> None:
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=2001),
            title="Example Movie",
            year=None,
            file_basename="film.mkv",
            file_size=7_000,
            file_path="/data/movies/Example Movie/film.mkv",
            root_folders=self._ARR_ROOTS,
            index=self._two_sections(),
        )
        assert res.rating_key == 300
        assert res.matched_by is MatchedBy.ID_AND_BASENAME
        assert "folder" in res.detail

    def test_the_other_instance_binds_the_other_copy(self) -> None:
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=2001),
            title="Example Movie",
            year=None,
            file_basename="film.mkv",
            file_size=70_000,
            file_path="/data/movies/Other Folder/film.mkv",
            root_folders=self._ARR_ROOTS,
            index=self._two_sections(),
        )
        assert res.rating_key == 400

    def test_the_longest_matching_root_is_the_one_used(self) -> None:
        """An instance may report nested roots. The longest one that prefixes the path is
        this item's root, so only what sits below THAT is evidence. Choosing the shorter
        root would leave an extra segment to be counted as a folder, and would also make
        the path too deep for Radarr's layout, which stands the step down."""
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=2001),
            title="Example Movie",
            year=None,
            file_basename="film.mkv",
            file_size=7_000,
            file_path="/data/movies/Example Movie/film.mkv",
            root_folders=("/data", "/data/movies"),
            index=self._two_sections(),
        )
        assert res.rating_key == 300

    def test_a_trailing_slash_or_case_difference_in_a_root_still_matches(self) -> None:
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=2001),
            title="Example Movie",
            year=None,
            file_basename="film.mkv",
            file_size=7_000,
            file_path="/data/movies/Example Movie/film.mkv",
            root_folders=("/Data/Movies/",),
            index=self._two_sections(),
        )
        assert res.rating_key == 300

    def test_no_roots_supplied_stands_the_corroborator_down(self) -> None:
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=2001),
            title="Example Movie",
            year=None,
            file_basename="film.mkv",
            file_size=None,
            file_path="/data/movies/Example Movie/film.mkv",
            index=self._two_sections(),
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS

    def test_a_path_under_none_of_the_supplied_roots_stands_it_down(self) -> None:
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=2001),
            title="Example Movie",
            year=None,
            file_basename="film.mkv",
            file_size=None,
            file_path="/somewhere/else/Example Movie/film.mkv",
            root_folders=self._ARR_ROOTS,
            index=self._two_sections(),
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS

    def test_a_root_broader_than_the_items_real_parent_stands_it_down(self) -> None:
        """The reported root is an ancestor, not this item's real root (a stale root after
        the operator reconfigured them, or a manual import into a nested folder). The
        leftover mount segments would be compared as if they were real folders, so the
        depth no longer matches Radarr's layout and the step declines rather than guess."""
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=2001),
            title="Example Movie",
            year=None,
            file_basename="film.mkv",
            file_size=None,
            file_path="/data/movies/Example Movie/film.mkv",
            root_folders=("/data",),
            index=self._two_sections(),
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS

    def test_an_unreadable_path_on_any_candidate_stands_it_down(self) -> None:
        """Could-not-look is never "different". Dropping the unreadable candidate would
        turn a tie into a strict win for the other copy."""
        index = PlexIndex.build(
            [
                _item(
                    300,
                    title="Example Movie",
                    tmdb=2001,
                    files=(PlexFile("film.mkv", 7_000, "/media/movies/Example Movie/film.mkv"),),
                ),
                _item(400, title="Example Movie", tmdb=2001, files=(PlexFile("film.mkv", None),)),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=2001),
            title="Example Movie",
            year=None,
            file_basename="film.mkv",
            file_size=None,
            file_path="/data/movies/Example Movie/film.mkv",
            root_folders=self._ARR_ROOTS,
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS


class TestTheFolderNeverBindsOnUncheckableEvidence:
    """The folder is circumstantial and the byte count is exact, so the folder must never
    bind a copy whose size cannot be checked while another candidate might match it."""

    def test_a_listing_holding_the_name_twice_is_not_bound_on_its_folder(self) -> None:
        """A merged multi-edition Plex item carries the name more than once, so it yields
        no single number to compare. The folder points at it; another copy matches the
        *arr's byte count exactly. Binding the folder's pick would ignore that."""
        index = PlexIndex.build(
            [
                _item(
                    21,
                    tmdb=4001,
                    files=(
                        PlexFile("film.mkv", 111, "/media/movies/Title/film.mkv"),
                        PlexFile("film.mkv", 222, "/media/movies/Title Two/film.mkv"),
                    ),
                ),
                _item(
                    22,
                    tmdb=4001,
                    files=(PlexFile("film.mkv", 500, "/media/movies-4k/Other/film.mkv"),),
                ),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=4001),
            title="Example Movie",
            year=None,
            file_basename="film.mkv",
            file_size=500,
            file_path="/data/movies/Title/film.mkv",
            root_folders=("/data/movies",),
            index=index,
        )
        assert res.rating_key != 21, "bound a copy whose size could not be checked"
        assert res.status is MatchStatus.AMBIGUOUS

    def test_the_folder_and_the_size_naming_different_copies_abstains(self) -> None:
        """A positive contradiction between two corroborators. Neither overrules the
        other: the file is kept."""
        index = PlexIndex.build(
            [
                _item(
                    100,
                    tmdb=4002,
                    files=(PlexFile("film.mkv", 7_000, "/media/movies/Title/film.mkv"),),
                ),
                _item(
                    200,
                    tmdb=4002,
                    files=(PlexFile("film.mkv", 70_000, "/media/movies-4k/Other/film.mkv"),),
                ),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=4002),
            title="Example Movie",
            year=None,
            file_basename="film.mkv",
            file_size=70_000,
            file_path="/data/movies/Title/film.mkv",
            root_folders=("/data/movies",),
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS
        assert "point at different copies" in res.detail


class TestARootFolderPayloadIsReadDefensively:
    """The roots arrive as an *arr ``/rootfolder`` body. A malformed one must yield no
    roots (which only stands the corroborator down), never raise out of a scan."""

    def test_a_well_formed_body_yields_its_paths(self) -> None:
        assert root_folder_paths(
            [{"path": "/data/movies", "accessible": True}, {"path": "/data/movies-4k"}]
        ) == ("/data/movies", "/data/movies-4k")

    def test_a_body_that_is_not_a_list_yields_nothing(self) -> None:
        assert root_folder_paths({"error": "not found"}) == ()
        assert root_folder_paths(None) == ()

    def test_entries_without_a_usable_path_are_skipped(self) -> None:
        assert root_folder_paths(["/data/movies", {"id": 1}, {"path": ""}, {"path": 7}]) == ()


class TestRootFoldersThatCouldNotBeRead:
    """``None`` roots mean the ``/rootfolder`` read failed; ``()`` means the instance
    answered and reported none. They must not behave the same.

    Losing the roots does not merely cost a bind: the folder step is the only thing that
    produces the folder-vs-size contradiction veto, so without it a stale Plex size can
    bind a copy the folder would have disputed. A failed read therefore refuses the whole
    narrowing rather than falling through to size alone.
    """

    @staticmethod
    def _stale_size_index() -> PlexIndex:
        # rk 100 is the copy this Radarr manages; its file was upgraded in place and Plex
        # still reports the old byte count. rk 200 happens to carry the new count.
        return PlexIndex.build(
            [
                _item(
                    100,
                    tmdb=5001,
                    files=(PlexFile("film.mkv", 111, "/media/movies/Title/film.mkv"),),
                ),
                _item(
                    200,
                    tmdb=5001,
                    files=(PlexFile("film.mkv", 900, "/media/movies-4k/Other/film.mkv"),),
                ),
            ]
        )

    def _resolve(self, roots: tuple[str, ...] | None) -> object:
        return resolve_movie(
            ids=ExternalIds.of(tmdb=5001),
            title="Example Movie",
            year=None,
            file_basename="film.mkv",
            file_size=900,
            file_path="/data/movies/Title/film.mkv",
            root_folders=roots,
            index=self._stale_size_index(),
        )

    def test_roots_in_hand_catch_the_disagreement_and_keep_the_file(self) -> None:
        res = self._resolve(("/data/movies",))
        assert res.rating_key is None  # type: ignore[attr-defined]
        assert "point at different copies" in res.detail  # type: ignore[attr-defined]

    def test_a_failed_read_refuses_to_narrow_rather_than_binding_on_size_alone(self) -> None:
        res = self._resolve(None)
        assert res.rating_key != 200, "bound the copy the folder would have disputed"  # type: ignore[attr-defined]
        assert res.rating_key is None  # type: ignore[attr-defined]
        assert res.status is MatchStatus.AMBIGUOUS  # type: ignore[attr-defined]
        assert "couldn't read the folder list" in res.detail  # type: ignore[attr-defined]


class TestAShowNeverGetsFolderEvidence:
    """Sonarr writes ``<root>/<Show>``. Below a correct root that is one segment -- the
    show folder, which is the very leaf both sides already matched on -- so the folder
    corroborator has nothing new to say about a show and always stands down.

    This is worth pinning because the step's reach for shows used to be co-extensive with
    its failure mode: it could only fire when the path was DEEPER than Sonarr's layout,
    which means the reported root was wrong, and it then bound on leftover mount segments.
    A show with two listings under one id has no size to fall back on either, so it
    abstains, and an abstain keeps the file.
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

    def test_a_correct_sonarr_root_leaves_no_evidence_and_abstains(self) -> None:
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/data/tv/Example Show",
            file_path="/data/tv/Example Show",
            root_folders=("/data/tv",),
            index=self._two_sections(),
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS

    def test_a_root_that_makes_the_path_too_deep_still_abstains(self) -> None:
        """The reported root is an ancestor of the real one, so ``tv`` is left in the
        path. That segment names one Plex library's folder, and before the layout guard it
        bound the wrong copy with no size to recover with."""
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/data/tv/Example Show",
            file_path="/data/tv/Example Show",
            root_folders=("/data",),
            index=self._two_sections(),
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS


class TestTwoInstancesSharingAMultiSegmentRoot:
    """The same two-instance layout, with the root the standard single-mount guide gives:
    each container maps its own host directory to a TWO-segment in-container root, so both
    instances report the identical path under it.

    This is the case a fixed one-segment strip could not survive. It left the second root
    segment in place, that segment happened to name one library's folder, and the strict
    margin fired on it -- binding the 4K entry to the HD listing and reading the HD copy's
    watch history and added-at. The exact byte size that separates the two was never even
    reached. Measured strictly below the reported root, both copies tie and the folder step
    stands aside, which is what lets size do its job.
    """

    _ARR_ROOTS = ("/data/movies",)
    _ARR_PATH = "/data/movies/Title/file.mkv"

    @staticmethod
    def _hd_and_4k_movies() -> PlexIndex:
        return PlexIndex.build(
            [
                _item(
                    100,
                    tmdb=1001,
                    files=(PlexFile("file.mkv", 7_000, "/srv/media/movies/Title/file.mkv"),),
                ),
                _item(
                    200,
                    tmdb=1001,
                    files=(PlexFile("file.mkv", 70_000, "/srv/media/movies-4k/Title/file.mkv"),),
                ),
            ]
        )

    def test_the_4k_instance_is_not_bound_to_the_hd_listing(self) -> None:
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=None,
            file_basename="file.mkv",
            file_size=70_000,
            file_path=self._ARR_PATH,
            root_folders=self._ARR_ROOTS,
            index=self._hd_and_4k_movies(),
        )
        assert res.rating_key != 100, "bound to the other library's copy"
        assert res.rating_key == 200
        assert "exact file size" in res.detail

    def test_the_hd_instance_binds_its_own_copy_by_size(self) -> None:
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=None,
            file_basename="file.mkv",
            file_size=7_000,
            file_path=self._ARR_PATH,
            root_folders=self._ARR_ROOTS,
            index=self._hd_and_4k_movies(),
        )
        assert res.rating_key == 100

    def test_the_show_half_has_no_size_to_recover_with_and_abstains(self) -> None:
        """The same layout on the TV side. A show has no byte size, so once the folder
        step stands aside there is nothing left. Both copies are kept, which is the honest
        answer: which library this series belongs to is not written anywhere below the
        root."""
        index = PlexIndex.build(
            [
                _item(
                    300,
                    title="Example Show",
                    tvdb=2001,
                    files=(PlexFile("title", None, "/srv/media/tv/Title"),),
                ),
                _item(
                    400,
                    title="Example Show",
                    tvdb=2001,
                    files=(PlexFile("title", None, "/srv/media/tv-4k/Title"),),
                ),
            ]
        )
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/data/tv/Title",
            file_path="/data/tv/Title",
            root_folders=("/data/tv",),
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS


class TestTheFolderStepNeverOutranksTheExactSize:
    """Two Plex copies whose own library roots sit at DIFFERENT depths.

    The folder step used to rank candidates by deepest shared suffix and let the deepest
    strictly win. That is unsound here: the shallower copy simply runs out of path, and
    losing the comparison reads as evidence against it when it is nothing of the kind. An
    exact suffix match on the item's library-relative path has no such failure mode, and
    where the folder still points somewhere the exact byte size contradicts, the folder
    yields. Radarr's byte count is exact; a folder name is circumstantial.

    Both shapes below bound the WRONG copy before this change, reading a stranger's watch
    history and added-at onto a file somebody watches.
    """

    @staticmethod
    def _two_plex_roots_of_different_depth() -> PlexIndex:
        return PlexIndex.build(
            [
                _item(
                    100, tmdb=1101, files=(PlexFile("f.mkv", 7_000, "/srv/media/movies/T/f.mkv"),)
                ),
                _item(200, tmdb=1101, files=(PlexFile("f.mkv", 70_000, "/movies/T/f.mkv"),)),
            ]
        )

    def test_a_shallower_plex_root_does_not_lose_on_depth_alone(self) -> None:
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1101),
            title="Example Movie",
            year=None,
            file_basename="f.mkv",
            file_size=70_000,
            file_path="/data/movies/T/f.mkv",
            root_folders=("/data",),
            index=self._two_plex_roots_of_different_depth(),
        )
        assert res.rating_key == 200, "the copy whose exact byte size matches"
        assert "exact file size" in res.detail

    def test_the_folder_yields_to_a_contradicting_exact_size(self) -> None:
        """The reported root is broader than the item's real parent, so a leftover root
        segment names a real folder in one library and the folder step points at it. The
        byte size says otherwise, and the byte size is the one that is exact."""
        index = PlexIndex.build(
            [
                _item(
                    100,
                    tmdb=1102,
                    files=(PlexFile("file.mkv", 7_000, "/srv/media/movies/Title/file.mkv"),),
                ),
                _item(
                    200,
                    tmdb=1102,
                    files=(PlexFile("file.mkv", 70_000, "/srv/media/movies-4k/Title/file.mkv"),),
                ),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1102),
            title="Example Movie",
            year=None,
            file_basename="file.mkv",
            file_size=70_000,
            file_path="/data/movies/Title/file.mkv",
            root_folders=("/data",),
            index=index,
        )
        assert res.rating_key != 100, "bound to the other library's copy"
        assert res.rating_key == 200
        assert "exact file size" in res.detail

    def test_an_unknown_arr_size_never_contradicts(self) -> None:
        """Could-not-look is never "different": with no *arr size there is nothing for the
        folder to disagree with, so the folder still decides."""
        index = PlexIndex.build(
            [
                _item(
                    300,
                    title="Example Movie",
                    tmdb=2101,
                    files=(PlexFile("film.mkv", 7_000, "/media/movies/Example Movie/film.mkv"),),
                ),
                _item(
                    400,
                    title="Example Movie",
                    tmdb=2101,
                    files=(PlexFile("film.mkv", 70_000, "/media/movies-4k/Other Folder/film.mkv"),),
                ),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=2101),
            title="Example Movie",
            year=None,
            file_basename="film.mkv",
            file_size=None,
            file_path="/data/movies/Example Movie/film.mkv",
            root_folders=("/data/movies",),
            index=index,
        )
        assert res.rating_key == 300
        assert "folder it sits in" in res.detail


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
        # The TYPE, not the sentence (rule 142). This branch is a disagreement -- one row
        # per id kind, different rows -- so it must not report as several copies in Plex,
        # which is what the owner used to be told. The keys go with it so the panel can
        # offer a link to each of the two rows.
        assert res.status is MatchStatus.CONFLICTED
        assert res.candidate_rating_keys == (100, 200)

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

    def test_an_id_pointing_away_from_the_row_holding_the_file_abstains(self) -> None:
        """The catastrophe the module docstring names. Radarr manages one file; Plex lists
        it twice -- rk=200 is the row whose Part IS that file (a local agent, so it carries
        no ids at all) and rk=100 is a second listing of the same content that DID match
        and holds a different file. The id binds rk=100, but the file name names rk=200.
        Judging rk=100's watch history while deleting rk=200's file is exactly what the
        cross-tier veto exists to stop -> keep."""
        index = PlexIndex.build(
            [
                _item(100, title="Example Movie", year=2020, tmdb=1001, basename="other copy.mkv"),
                _item(200, title="Example Movie", year=2020, basename="the managed file.mkv"),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title=None,
            year=None,
            file_basename="/movies/Example Movie (2020)/the managed file.mkv",
            file_size=99,
            index=index,
        )
        assert res.rating_key is None
        assert res.detail == "Kept: identifiers disagree (tmdb->100, basename->200)"
        assert res.status is MatchStatus.CONFLICTED
        assert res.candidate_rating_keys == (100, 200)

    def test_a_file_name_naming_the_bound_row_confirms_the_id(self) -> None:
        """The healthy shape: the id and the file name name the same listing. The
        cross-check corroborates, and the bind keeps its id provenance."""
        index = PlexIndex.build(
            [
                _item(100, title="Example Movie", tmdb=1001, basename="the managed file.mkv"),
                _item(200, title="Other", tmdb=1002, basename="something else.mkv"),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title=None,
            year=None,
            file_basename="/movies/the managed file.mkv",
            index=index,
        )
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.TMDB

    def test_a_file_name_naming_several_listings_is_silence_for_a_bound_id(self) -> None:
        """A name naming more than one listing says nothing about WHICH -- and under a
        shared id it is the merged-twins shape tier 1 has already narrowed. Silence, so
        the id's own answer stands; only a name naming exactly one listing can veto."""
        index = PlexIndex.build(
            [
                _item(100, title="Example Movie", tmdb=1001, basename="example.mkv"),
                _item(300, title="Elsewhere", basename="example.mkv"),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title=None,
            year=None,
            file_basename="example.mkv",
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


class TestAShowsImdbIdCrossChecksItsTvdbBind:
    """imdb never binds a show, but it always checks the one that did.

    A movie in this exact shape has always abstained; a show used to bind whatever its
    tvdb id named, because the resolver never consulted the imdb id both sides carry.
    """

    def test_a_stale_tvdb_id_naming_another_show_abstains(self) -> None:
        """Sonarr's tvdb id is stale (a series split, or a bad match) and names a
        different Plex show. The imdb id names the real one. The ids contradict each
        other and there is no way to know which is wrong -> keep the whole series."""
        index = PlexIndex.build(
            [
                _item(100, title="Some Other Show", tvdb=2001, imdb="tt0000001"),
                _item(200, title="Example Show", tvdb=9999, imdb="tt0000042"),
            ]
        )
        res = resolve_show(
            ids=ExternalIds.of(imdb="tt0000042", tvdb=2001),
            title=None,
            year=None,
            file_basename=None,
            index=index,
        )
        assert res.rating_key is None
        assert "contradict" in res.detail.lower()
        # rule 72: the show ladder reaches the same branch, so it carries the same status
        # and the same candidates.
        assert res.status is MatchStatus.CONFLICTED
        assert res.candidate_rating_keys == (100, 200)

    def test_an_agreeing_imdb_id_confirms_the_tvdb_bind(self) -> None:
        index = PlexIndex.build([_item(300, title="Example Show", tvdb=2001, imdb="tt0000042")])
        res = resolve_show(
            ids=ExternalIds.of(imdb="tt0000042", tvdb=2001),
            title=None,
            year=None,
            file_basename=None,
            index=index,
        )
        assert res.rating_key == 300
        assert res.matched_by is MatchedBy.TVDB

    def test_imdb_alone_never_binds_a_show(self) -> None:
        """The cross-check may only ever ADD abstains. With no tvdb hit to check, an imdb
        id that names a Plex show does NOT bind it -- that would make shows deletable that
        are kept today. The weaker tiers decide, exactly as before."""
        index = PlexIndex.build([_item(300, title="Example Show", imdb="tt0000042")])
        res = resolve_show(
            ids=ExternalIds.of(imdb="tt0000042", tvdb=2001),
            title=None,
            year=None,
            file_basename=None,
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.UNMATCHED

    def test_imdb_alone_still_leaves_the_title_backstop_intact(self) -> None:
        """Standing down is not a veto: the title+year tier still binds the show it always
        did, so the imdb cross-check costs no existing match."""
        index = PlexIndex.build([_item(300, title="Example Show", year=2020, imdb="tt0000042")])
        res = resolve_show(
            ids=ExternalIds.of(imdb="tt0000042"),
            title="Example Show",
            year=2020,
            file_basename=None,
            index=index,
        )
        assert res.rating_key == 300
        assert res.matched_by is MatchedBy.TITLE_YEAR


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


class TestTheBasenameCrossChecksAnIdBind:
    """The basename tier runs even when an id already bound, as a cross-check only:
    promoted from "consulted only if nothing bound" to always-on so a corroborating kind
    can raise an abstain it was otherwise structurally unable to raise (rule 109). The
    cost is real: after an *arr renames a file and before Plex rescans, the candidate's
    basename and Plex's stored one disagree. So disagreement abstains only when the new
    name positively names a DIFFERENT row; a name Plex has simply never heard of is
    silence and leaves the id bind standing, or every rename would cost a scan's worth
    of deletability.
    """

    def test_a_renamed_file_plex_has_not_rescanned_still_binds_by_id(self) -> None:
        """The ordinary rename window: Plex still lists the old leaf, and the new one
        matches nothing. The corroborator stands down rather than veto."""
        index = PlexIndex.build(
            [_item(100, title="Example Movie", tmdb=1001, basename="old name (2020).mkv")]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title=None,
            year=None,
            file_basename="/movies/new name (2020).mkv",
            index=index,
        )
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.TMDB

    def test_a_basename_naming_a_different_row_vetoes_the_id_bind(self) -> None:
        """Positive disagreement, which is the abstain this tier was promoted to catch:
        the id says one row and the file on disk says another. No way to know which is
        wrong, so keep the file."""
        index = PlexIndex.build(
            [
                _item(100, title="Example Movie", tmdb=1001, basename="old name (2020).mkv"),
                _item(200, title="Something Else", basename="new name (2020).mkv"),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title=None,
            year=None,
            file_basename="/movies/new name (2020).mkv",
            index=index,
        )
        assert res.rating_key is None
        assert "disagree" in res.detail.lower()

    def test_the_cross_check_never_originates_a_bind(self) -> None:
        """The corroborator may add an abstain; it may never be the thing that binds. With
        the id agreeing, the bind is still credited to the id, not to the file name."""
        index = PlexIndex.build(
            [_item(100, title="Example Movie", tmdb=1001, basename="same (2020).mkv")]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title=None,
            year=None,
            file_basename="/movies/same (2020).mkv",
            index=index,
        )
        assert res.rating_key == 100
        assert res.matched_by is MatchedBy.TMDB


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


# ---------------------------------------------------------------------------
# The operator's library map -- ground truth that tells two libraries apart, and
# the only discriminator a show ever gets. Every case is a way a wrong bind could
# read a stranger's watch history, so each ambiguity resolves toward keeping the file.
# ---------------------------------------------------------------------------


class TestCandidateLibrariesForDiagnostics:
    """The raw library titles a duplicated title's copies live in -- printed in the unmatched
    warning so an operator sees, from the log, whether to map the folder or fix its spelling."""

    def test_lists_each_distinct_library_raw_cased_and_sorted(self) -> None:
        index = PlexIndex.build(
            [
                _item(300, title="Example Show", tvdb=2001, basename="s", library="TV 4K"),
                _item(400, title="Example Show", tvdb=2001, basename="s", library="TV"),
            ]
        )
        assert candidate_libraries(ExternalIds.of(tvdb=2001), index, ("tvdb", "imdb")) == [
            "TV",
            "TV 4K",
        ]

    def test_collapses_two_copies_in_one_library_to_a_single_entry(self) -> None:
        """Both copies in one library is the genuine can't-split case; the operator sees one
        library, not two, so the log tells that apart from an unmapped-but-splittable title."""
        index = PlexIndex.build(
            [
                _item(300, title="Example Show", tvdb=2001, basename="s", library="TV"),
                _item(400, title="Example Show", tvdb=2001, basename="s", library="TV"),
            ]
        )
        assert candidate_libraries(ExternalIds.of(tvdb=2001), index, ("tvdb", "imdb")) == ["TV"]

    def test_an_unknown_library_is_skipped_not_rendered_as_blank(self) -> None:
        index = PlexIndex.build(
            [
                _item(300, title="Example Show", tvdb=2001, basename="s", library="TV"),
                _item(400, title="Example Show", tvdb=2001, basename="s", library=None),
            ]
        )
        assert candidate_libraries(ExternalIds.of(tvdb=2001), index, ("tvdb", "imdb")) == ["TV"]

    def test_no_hits_is_an_empty_list(self) -> None:
        index = PlexIndex.build([_item(300, title="Example Show", tvdb=2001, library="TV")])
        assert candidate_libraries(ExternalIds.of(tvdb=9999), index, ("tvdb", "imdb")) == []


class TestTheLibraryMapTellsTwoListingsApart:
    """One title kept in an HD library and a 4K one is listed twice in Plex under one tvdb id
    with an identical show folder. The path cannot split them (measured), but the operator's
    root-folder -> library map can: it is a declaration, not an inference."""

    @staticmethod
    def _two_shows() -> PlexIndex:
        return PlexIndex.build(
            [
                _item(300, title="Example Show", tvdb=2001, basename="example show", library="TV"),
                _item(
                    400, title="Example Show", tvdb=2001, basename="example show", library="TV 4K"
                ),
            ]
        )

    def test_the_mapped_library_binds_the_right_show(self) -> None:
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/tv/Example Show",
            file_path="/tv/Example Show",
            plex_library="TV",
            index=self._two_shows(),
        )
        assert res.rating_key == 300
        assert res.matched_by is MatchedBy.ID_AND_BASENAME
        assert "Plex library" in res.detail

    def test_the_other_instance_binds_the_other_show(self) -> None:
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/tv-4k/Example Show",
            file_path="/tv-4k/Example Show",
            plex_library="TV 4K",
            index=self._two_shows(),
        )
        assert res.rating_key == 400

    def test_the_stale_mapping_guard_folds_both_sides(self) -> None:
        """``libraries_for_ids`` is the guard's whole input and had no test at all. Both
        callers ask ``fold(plex_library) in libraries_for_ids(...)``, so if only one side
        were folded a correctly mapped library would look absent from the item's own
        listings and the scan would warn the operator their mapping is wrong (rule 88)."""
        folded = libraries_for_ids(ExternalIds.of(tvdb=2001), self._two_shows(), ("tvdb", "imdb"))

        assert folded == {"tv", "tv 4k"}
        assert fold("  TV 4K  ") in folded

    def test_library_match_is_case_and_whitespace_folded(self) -> None:
        """The mapped value is a stored copy of the title; a re-cased or padded copy still
        binds, because the operator picked the same library."""
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/tv/Example Show",
            file_path="/tv/Example Show",
            plex_library="  tv 4k  ",
            index=self._two_shows(),
        )
        assert res.rating_key == 400

    def test_no_map_keeps_the_old_abstain(self) -> None:
        """Without a mapping a show has no discriminator at all, so two same-name listings
        abstain and are kept -- exactly the behavior before the map existed."""
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/tv/Example Show",
            file_path="/tv/Example Show",
            plex_library=None,
            index=self._two_shows(),
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS

    def test_two_copies_in_the_one_mapped_library_abstain(self) -> None:
        """Two instances feeding one Plex library: both listings live in it, so the library
        cannot split them. A show has no size to fall back on -> abstain, keep both."""
        index = PlexIndex.build(
            [
                _item(300, title="Example Show", tvdb=2001, basename="example show", library="TV"),
                _item(400, title="Example Show", tvdb=2001, basename="example show", library="TV"),
            ]
        )
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/tv/Example Show",
            file_path="/tv/Example Show",
            plex_library="TV",
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS

    def test_mapped_library_holds_neither_is_ignored_and_abstains(self) -> None:
        """A stale or mistaken mapping (points at a library holding none of the copies)
        narrows to nothing, is ignored, and the show abstains as if unmapped -- never a
        silent mis-bind into a library the copy is not in."""
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/tv/Example Show",
            file_path="/tv/Example Show",
            plex_library="Anime",
            index=self._two_shows(),
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS

    def test_an_unknown_library_on_any_candidate_stands_the_step_down(self) -> None:
        """Could-not-look is never 'different': if any candidate's own library is unknown the
        partition is untrustworthy, so the map stands down and the show abstains."""
        index = PlexIndex.build(
            [
                _item(300, title="Example Show", tvdb=2001, basename="example show", library="TV"),
                _item(400, title="Example Show", tvdb=2001, basename="example show", library=None),
            ]
        )
        res = resolve_show(
            ids=ExternalIds.of(tvdb=2001),
            title="Example Show",
            year=None,
            file_basename="/tv/Example Show",
            file_path="/tv/Example Show",
            plex_library="TV",
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS


class TestTheLibraryMapWithMovies:
    """A movie keeps every corroborator it had; the library is tried first, but a positive
    size contradiction still vetoes it, and an unconfirmed winner still defers to size."""

    def test_library_and_name_bind_a_movie(self) -> None:
        index = PlexIndex.build(
            [
                _item(100, tmdb=1001, basename="example.mkv", size=111, library="Movies"),
                _item(200, tmdb=1001, basename="example.mkv", size=222, library="Movies 4K"),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=None,
            file_basename="example.mkv",
            file_size=111,
            file_path="/movies/Example/example.mkv",
            plex_library="Movies",
            index=index,
        )
        assert res.rating_key == 100

    def test_library_names_one_but_size_contradicts_abstains(self) -> None:
        """The mapped library names copy 100, but the arr's exact byte count matches copy 200
        instead. Two corroborators naming different copies is a positive contradiction ->
        abstain, keep the file."""
        index = PlexIndex.build(
            [
                _item(100, tmdb=1001, basename="example.mkv", size=111, library="Movies"),
                _item(200, tmdb=1001, basename="example.mkv", size=222, library="Movies 4K"),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=None,
            file_basename="example.mkv",
            file_size=222,
            file_path="/movies/Example/example.mkv",
            plex_library="Movies",
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS

    def test_unconfirmed_library_winner_defers_to_size_which_keeps_the_file(self) -> None:
        """The mapped library's copy has no checkable size, while the OTHER copy matches the
        arr's byte count exactly. The library step will not bind on the map alone when its
        winner's size cannot be checked and another copy might match exactly; it stands down
        and lets size decide. Size, seeing an unknown size in the set, abstains -- so the
        operator's map and the physical byte count disagreeing keeps the file, never a
        mis-bind into the mapped library over contradictory bytes."""
        index = PlexIndex.build(
            [
                _item(100, tmdb=1001, basename="example.mkv", size=None, library="Movies"),
                _item(200, tmdb=1001, basename="example.mkv", size=222, library="Movies 4K"),
            ]
        )
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=None,
            file_basename="example.mkv",
            file_size=222,
            file_path="/movies/Example/example.mkv",
            plex_library="Movies",
            index=index,
        )
        assert res.rating_key is None
        assert res.status is MatchStatus.AMBIGUOUS


class TestLibraryForPath:
    """The service-layer helper that turns an item's path + its instance's map into a library."""

    def test_longest_matching_root_wins(self) -> None:
        m = {"/tv": "TV", "/tv/anime": "Anime"}
        assert library_for_path("/tv/anime/Example Show", m) == "Anime"
        assert library_for_path("/tv/Example Show", m) == "TV"

    def test_no_map_or_no_prefix_is_none(self) -> None:
        assert library_for_path("/tv/Example Show", None) is None
        assert library_for_path("/tv/Example Show", {}) is None
        assert library_for_path("/movies/Example", {"/tv": "TV"}) is None
        assert library_for_path(None, {"/tv": "TV"}) is None

    def test_segment_comparison_ignores_separators_and_case(self) -> None:
        assert library_for_path("/TV/Example", {"/tv/": "TV"}) == "TV"


class TestTheAbstainVocabulary:
    """Every ``abstain`` in :func:`resolve` is one of two stories, and they are not the same.

    MULTIPLICITY: several Plex rows answer to this item, so the library really does hold
    more than one copy. DISAGREEMENT: each kind of evidence found ONE row and they were
    different rows, which is Plex and the *arr describing one file differently over a
    library that may hold exactly one copy. Both keep the file. Only one of them is true
    about the number of copies, and for a year every surface said that one.

    Written as a table from the four call sites rather than transcribed off the branch
    structure (rule 119). The table alone cannot see a FIFTH call site, though -- it drives
    the four it names and passes whatever else exists -- so
    :meth:`test_the_table_covers_every_abstain_in_resolve` pins the count of the population
    it claims to cover (rule 145).
    """

    #: The two spellings the count below scans for. Both constructors are called as
    #: ``Resolution.<name>(`` inside :func:`resolve` and nowhere else in the module; the
    #: ``cls(...)`` bodies of the classmethods themselves do not match, which is what keeps
    #: the count equal to the number of call sites rather than call sites plus definitions
    #: (rule 147 -- a source scan is bounded by the spellings it accepts, so they are written
    #: down here and asserted against the tree below).
    _ABSTAIN_CALLS: ClassVar[tuple[str, ...]] = ("Resolution.abstain(", "Resolution.conflicted(")

    def test_the_table_covers_every_abstain_in_resolve(self) -> None:
        """A fifth abstain arm added to ``resolve`` fails HERE, which the table cannot do.

        The table drives four resolutions and asserts the status of each; a new arm calling
        ``Resolution.abstain`` where it should call ``conflicted`` adds a fifth case nothing
        drives, and every operator surface then tells the owner their library holds a
        duplicate that is not there -- the defect this whole class exists over. Counting the
        call sites is what makes the omission visible: a member the table never collected is
        otherwise missing from both the guard and its proof.
        """
        source = (
            Path(__file__).resolve().parents[1] / "src" / "reaper" / "engine" / "identity.py"
        ).read_text(encoding="utf-8")
        found = sum(source.count(call) for call in self._ABSTAIN_CALLS)
        cases = len(
            self.test_each_abstain_reports_its_own_kind.pytestmark[0].args[1]  # type: ignore[attr-defined]
        )
        assert found == cases, (
            f"identity.resolve has {found} abstain/conflicted call sites and the table above "
            f"drives {cases}. Add the new one to the table with the status it must report, or "
            "an abstain wording no test covers ships to the why-panel, the card reason and "
            "the chip at once. See tests/test_identity.py::TestTheAbstainVocabulary."
        )

    @pytest.mark.parametrize(
        ("name", "resolve", "expected", "candidates"),
        [
            (
                "an id naming several rows nothing narrowed",
                lambda index: resolve_movie(
                    ids=ExternalIds.of(tmdb=1001),
                    title=None,
                    year=None,
                    file_basename=None,
                    index=index,
                ),
                MatchStatus.AMBIGUOUS,
                (100, 200),
            ),
            (
                "a file name naming several rows",
                lambda index: resolve_movie(
                    ids=ExternalIds(),
                    title=None,
                    year=None,
                    file_basename="shared.mkv",
                    index=index,
                ),
                MatchStatus.AMBIGUOUS,
                (300, 400),
            ),
            (
                "two id kinds naming different rows",
                lambda index: resolve_movie(
                    ids=ExternalIds.of(tmdb=2002, imdb="tt0000500"),
                    title=None,
                    year=None,
                    file_basename=None,
                    index=index,
                ),
                MatchStatus.CONFLICTED,
                (500, 600),
            ),
            (
                "the id and the file name naming different rows",
                lambda index: resolve_movie(
                    ids=ExternalIds.of(tmdb=3003),
                    title=None,
                    year=None,
                    file_basename="seven hundred.mkv",
                    index=index,
                ),
                MatchStatus.CONFLICTED,
                (700, 800),
            ),
        ],
        ids=["dup-id", "dup-basename", "id-vs-id", "id-vs-name"],
    )
    def test_each_abstain_reports_its_own_kind(
        self,
        name: str,
        resolve: object,
        expected: MatchStatus,
        candidates: tuple[int, ...],
    ) -> None:
        index = PlexIndex.build(
            [
                # Multiplicity: one id over two rows, and one file name over two others.
                _item(100, title="Dup Id A", year=2020, tmdb=1001),
                _item(200, title="Dup Id B", year=2020, tmdb=1001),
                _item(300, title="Dup Name A", year=2020, basename="shared.mkv"),
                _item(400, title="Dup Name B", year=2020, basename="shared.mkv"),
                # Disagreement: two id kinds apart, then an id and a file name apart.
                _item(500, title="Id Clash A", year=2020, tmdb=2002),
                _item(600, title="Id Clash B", year=2020, imdb="tt0000500"),
                _item(700, title="Tier Clash A", year=2020, tmdb=3003),
                _item(800, title="Tier Clash B", year=2020, basename="seven hundred.mkv"),
            ]
        )
        res = resolve(index)  # type: ignore[operator]
        assert res.rating_key is None, name
        assert res.status is expected, name
        assert res.candidate_rating_keys == candidates, name

    def test_a_bind_carries_no_candidates(self) -> None:
        """The field is set only where Reaper refused to choose. A clean bind offering
        "possible matches" would put a links row under a notice that never renders."""
        index = PlexIndex.build([_item(100, title="Example Movie", year=2020, tmdb=1001)])
        res = resolve_movie(
            ids=ExternalIds.of(tmdb=1001),
            title="Example Movie",
            year=2020,
            file_basename=None,
            index=index,
        )
        assert res.status is MatchStatus.MATCHED
        assert res.candidate_rating_keys == ()
