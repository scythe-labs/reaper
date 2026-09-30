# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Plex refresh roll-up: which folders replace the queued ones, and never a root."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clients.base import IntegrationError
from reaper.engine.policy import ProfileSettings
from reaper.services.executor import Executor, roll_up_refreshes
from tests.test_reap_loop import FakePlex, _armed, _gateway


@pytest.fixture
async def session(async_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with async_factory() as s:
        yield s


def _q(*paths: str) -> dict[str, set[int]]:
    return {p: {i} for i, p in enumerate(paths, start=1)}


class _Disk:
    """A disk listing per parent, recording every listing asked for."""

    def __init__(self, disk: Mapping[str, list[str]]) -> None:
        self.disk = disk
        self.asked: list[str] = []

    async def __call__(self, parent: str) -> list[str] | None:
        self.asked.append(parent)
        return self.disk.get(parent)


def _kids(parent: str, names: list[str]) -> list[str]:
    return [f"{parent}/{n}" for n in names]


async def test_a_flat_movie_library_is_never_rolled_up() -> None:
    gone = _kids("/movies", [f"G{i}" for i in range(8)])
    disk = _Disk({"/movies": _kids("/movies", ["K1", "K2"])})
    out = await roll_up_refreshes(_q(*gone), ["/movies"], ["/movies"], disk)
    assert set(out) == set(gone)
    assert disk.asked == []


async def test_three_of_four_seasons_roll_up_to_the_show() -> None:
    seasons = _kids("/tv/Show", [f"Season {n}" for n in range(1, 5)])
    disk = _Disk({"/tv/Show": seasons[3:]})
    out = await roll_up_refreshes(_q(*seasons[:3]), ["/tv"], ["/tv"], disk)
    assert set(out) == {"/tv/Show"}


async def test_one_of_four_seasons_stays_a_season() -> None:
    seasons = _kids("/tv/Show", [f"Season {n}" for n in range(1, 5)])
    disk = _Disk({"/tv/Show": seasons[1:]})
    assert set(await roll_up_refreshes(_q(seasons[0]), ["/tv"], ["/tv"], disk)) == {seasons[0]}


async def test_half_rolls_up_and_less_does_not() -> None:
    titles = _kids("/movies/A", [f"M{i}" for i in range(10)])
    disk = _Disk({"/movies/A": titles[5:]})
    args = (["/movies"], ["/movies"], disk)
    assert set(await roll_up_refreshes(_q(*titles[:5]), *args)) == {"/movies/A"}
    disk = _Disk({"/movies/A": titles[4:]})
    assert set(await roll_up_refreshes(_q(*titles[:4]), ["/movies"], ["/movies"], disk)) == set(
        titles[:4]
    )


async def test_unmanaged_entries_count_as_kept() -> None:
    managed = _kids("/movies/A", ["M1", "M2", "M3", "M4"])
    stray = _kids("/movies/A", [f"Stray{i}" for i in range(10)])
    disk = _Disk({"/movies/A": managed[2:] + stray})
    out = await roll_up_refreshes(_q(*managed[:2]), ["/movies"], ["/movies"], disk)
    assert set(out) == set(managed[:2])


async def test_a_plex_location_above_the_arr_root_never_rolls_to_the_root() -> None:
    gone = _kids("/data/movies", [f"G{i}" for i in range(4)])
    disk = _Disk({"/data/movies": gone[:1]})
    out = await roll_up_refreshes(_q(*gone), ["/data"], ["/data/movies"], disk)
    assert set(out) == set(gone)
    assert disk.asked == []


async def test_an_arr_root_above_the_plex_location_never_rolls_to_the_location() -> None:
    gone = _kids("/data/movies", [f"G{i}" for i in range(4)])
    out = await roll_up_refreshes(_q(*gone), ["/data/movies"], ["/data"], _Disk({}))
    assert set(out) == set(gone)


async def test_a_path_in_no_location_or_no_root_never_rolls_up() -> None:
    other = _kids("/other/A", ["M1", "M2"])
    disk = _Disk({"/other/A": []})
    assert set(await roll_up_refreshes(_q(*other), ["/movies"], ["/other"], disk)) == set(other)
    assert set(await roll_up_refreshes(_q(*other), ["/other"], ["/movies"], disk)) == set(other)
    assert disk.asked == []


async def test_seasons_roll_to_the_show_and_shows_to_a_genre_folder() -> None:
    queued = _q("/tv/Drama/A/Season 1", "/tv/Drama/A/Season 2", "/tv/Drama/B/Season 1")
    disk = _Disk(
        {
            "/tv/Drama/A": _kids("/tv/Drama/A", ["Season 3"]),
            "/tv/Drama/B": [],
            "/tv/Drama": _kids("/tv/Drama", ["A", "B"]),
        }
    )
    out = await roll_up_refreshes(queued, ["/tv"], ["/tv"], disk)
    assert set(out) == {"/tv/Drama"}


async def test_a_step_that_does_not_qualify_stops_the_climb() -> None:
    queued = _q("/tv/Drama/A/Season 1", "/tv/Drama/A/Season 2")
    disk = _Disk(
        {
            "/tv/Drama/A": [],
            "/tv/Drama": _kids("/tv/Drama", ["A", "B", "C", "D"]),
        }
    )
    assert set(await roll_up_refreshes(queued, ["/tv"], ["/tv"], disk)) == {"/tv/Drama/A"}


async def test_plex_keys_are_unioned_including_the_parents_own() -> None:
    queued = {"/tv/S/Season 1": {1, 2}, "/tv/S/Season 2": {2, 3}, "/tv/S": {9}}
    disk = _Disk({"/tv/S": []})
    assert await roll_up_refreshes(queued, ["/tv"], ["/tv"], disk) == {"/tv/S": {1, 2, 3, 9}}


async def test_a_failed_listing_sends_the_folders_as_queued() -> None:
    queued = _q("/tv/S/Season 1", "/tv/S/Season 2")
    assert set(await roll_up_refreshes(queued, ["/tv"], ["/tv"], _Disk({}))) == set(queued)


async def test_each_parent_is_listed_once() -> None:
    seasons = _kids("/tv/S", [f"Season {n}" for n in range(1, 5)])
    disk = _Disk({"/tv/S": seasons[3:], "/tv": ["/tv/S", "/tv/T", "/tv/U"]})
    await roll_up_refreshes(_q(*seasons[:3]), ["/tv"], ["/tv"], disk)
    assert sorted(disk.asked) == ["/tv/S"]


class _Arr:
    def __init__(self, listing: dict[str, Any] | None, root: str = "/movies") -> None:
        self._listing, self._root = listing, root
        self.listed: list[str] = []

    async def root_folders(self) -> list[dict[str, Any]]:
        return [{"path": self._root, "accessible": True}]

    async def filesystem(self, path: str) -> dict[str, Any]:
        self.listed.append(path)
        if self._listing is None:
            raise IntegrationError("radarr", "unreachable")
        return self._listing


async def _flush(session: AsyncSession, arr: _Arr, plex: FakePlex) -> None:
    executor = Executor(
        session,
        safety=_armed(),
        settings=ProfileSettings(),
        dry_run=False,
        gateway=_gateway(radarr={1: arr}, plex=plex),
    )
    for i in range(3):
        executor._queue_refresh(f"/movies/A/M{i}", plex_keys=(i,))
    await executor._flush_refreshes()


async def test_the_flush_rolls_up_from_the_arrs_listing(session: AsyncSession) -> None:
    plex = FakePlex(sections={"Films": ["/movies"]})
    arr = _Arr({"directories": [{"path": "/movies/A/Keep"}], "files": []})
    await _flush(session, arr, plex)
    assert plex.refreshed == [("Films", "/movies/A")]
    assert arr.listed == ["/movies/A/"]


async def test_a_failed_listing_in_the_flush_sends_the_folders_as_queued(
    session: AsyncSession,
) -> None:
    plex = FakePlex(sections={"Films": ["/movies"]})
    await _flush(session, _Arr(None), plex)
    assert sorted(p for _, p in plex.refreshed) == [f"/movies/A/M{i}" for i in range(3)]
