# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Plex refresh roll-up: which folders replace the queued ones, and never a location."""

from __future__ import annotations

from reaper.services.executor import roll_up_refreshes


def _q(*paths: str) -> dict[str, set[int]]:
    return {p: {i} for i, p in enumerate(paths, start=1)}


def _movies(letter: str, total: int) -> list[str]:
    return [f"/movies/{letter}/M{i}" for i in range(total)]


def test_a_flat_movie_library_is_never_rolled_up() -> None:
    kept = [f"/movies/Kept{i}" for i in range(2)]
    gone = [f"/movies/Gone{i}" for i in range(8)]
    assert set(roll_up_refreshes(_q(*gone), ["/movies"], kept)) == set(gone)


def test_three_of_four_seasons_roll_up_to_the_show() -> None:
    seasons = [f"/tv/Show/Season {n}" for n in range(1, 5)]
    out = roll_up_refreshes(_q(*seasons[:3]), ["/tv"], [seasons[3]])
    assert set(out) == {"/tv/Show"}


def test_one_of_four_seasons_stays_a_season() -> None:
    seasons = [f"/tv/Show/Season {n}" for n in range(1, 5)]
    out = roll_up_refreshes(_q(seasons[0]), ["/tv"], seasons[1:])
    assert set(out) == {seasons[0]}


def test_six_of_ten_in_a_letter_folder_roll_up_and_four_do_not() -> None:
    titles = _movies("A", 10)
    assert set(roll_up_refreshes(_q(*titles[:6]), ["/movies"], titles[6:])) == {"/movies/A"}
    assert set(roll_up_refreshes(_q(*titles[:4]), ["/movies"], titles[4:])) == set(titles[:4])


def test_half_is_enough() -> None:
    titles = _movies("A", 10)
    assert set(roll_up_refreshes(_q(*titles[:5]), ["/movies"], titles[5:])) == {"/movies/A"}


def test_a_location_or_anything_above_it_is_never_returned() -> None:
    titles = _movies("A", 4) + [f"/media/movies/B{i}" for i in range(4)]
    out = roll_up_refreshes(_q(*titles), ["/media/movies", "/movies/"], [])
    assert not {"/movies", "/media/movies", "/media", "/"} & set(out)
    assert set(out) == {"/movies/A", *(t for t in titles if t.startswith("/media"))}


def test_seasons_roll_to_the_show_and_shows_to_a_genre_folder() -> None:
    queued = _q("/tv/Drama/A/Season 1", "/tv/Drama/A/Season 2", "/tv/Drama/B/Season 1")
    out = roll_up_refreshes(queued, ["/tv"], [])
    assert set(out) == {"/tv/Drama"}


def test_a_step_that_does_not_qualify_stops_the_climb() -> None:
    queued = _q("/tv/Drama/A/Season 1", "/tv/Drama/A/Season 2")
    others = [f"/tv/Drama/Other{i}/Season 1" for i in range(5)]
    assert set(roll_up_refreshes(queued, ["/tv"], others)) == {"/tv/Drama/A"}


def test_plex_keys_are_unioned() -> None:
    queued = {"/tv/S/Season 1": {1, 2}, "/tv/S/Season 2": {2, 3}}
    assert roll_up_refreshes(queued, ["/tv"], []) == {"/tv/S": {1, 2, 3}}


def test_a_queued_folder_counted_in_the_library_is_not_counted_twice() -> None:
    titles = _movies("A", 4)
    out = roll_up_refreshes(_q(titles[0]), ["/movies"], titles)
    assert set(out) == {titles[0]}
