# SPDX-License-Identifier: AGPL-3.0-or-later
"""The "requested by" join -- built on external ids, and never a gate.

The map keys on tmdb/tvdb rather than the arr instance id, because Seerr and Reaper number
their services differently. This is display-only: the worst a wrong match can do is show
the wrong name, so a loose external-id join is acceptable here in a way it never is on the
delete path.
"""

from __future__ import annotations

from reaper.clients.seerr import MediaRequest, Requester
from reaper.services import requested_by


def _req(**kw: object) -> MediaRequest:
    base: dict[str, object] = {
        "request_id": 1,
        "media_type": "movie",
        "is_4k": False,
        "status": 5,
        "requested_at": None,
        "requester": Requester(
            seerr_user_id=1, plex_id=1, username="alice", display_name="Alice", email=None
        ),
        "tmdb_id": None,
        "tvdb_id": None,
        "imdb_id": None,
        "plex_rating_key": None,
        "arr_id": None,
        "arr_instance_id": None,
        "available_at": None,
        "seasons": (),
    }
    base.update(kw)
    return MediaRequest(**base)  # type: ignore[arg-type]


class _FakeSeerr:
    def __init__(self, requests: list[MediaRequest]) -> None:
        self._requests = requests

    async def all_requests(self, *, filter_: str = "available") -> list[MediaRequest]:
        return self._requests


class TestBuildMap:
    async def test_none_seerr_is_an_empty_map(self) -> None:
        assert await requested_by.build_map(None) == {}

    async def test_a_movie_maps_under_its_tmdb_key(self) -> None:
        seerr = _FakeSeerr([_req(media_type="movie", tmdb_id=603)])
        result = await requested_by.build_map(seerr)  # type: ignore[arg-type]
        assert result[requested_by.movie_key(603)] == "Alice"

    async def test_a_show_maps_under_the_show_and_each_requested_season(self) -> None:
        seerr = _FakeSeerr([_req(media_type="tv", tvdb_id=81189, seasons=(2, 3))])
        result = await requested_by.build_map(seerr)  # type: ignore[arg-type]
        assert result[requested_by.show_key(81189)] == "Alice"
        assert result[requested_by.season_key(81189, 2)] == "Alice"
        assert result[requested_by.season_key(81189, 3)] == "Alice"
        assert requested_by.season_key(81189, 1) not in result  # season 1 was not requested

    async def test_several_requesters_are_summarised(self) -> None:
        seerr = _FakeSeerr(
            [
                _req(tmdb_id=1, requester=Requester(1, 1, "a", "Alice", None)),
                _req(tmdb_id=1, requester=Requester(2, 2, "b", "Bob", None)),
                _req(tmdb_id=1, requester=Requester(3, 3, "c", "Cara", None)),
            ]
        )
        result = await requested_by.build_map(seerr)  # type: ignore[arg-type]
        assert result[requested_by.movie_key(1)] == "Alice + 2 others"

    async def test_an_unreachable_seerr_degrades_to_empty_not_an_error(self) -> None:
        class _Broken:
            async def all_requests(self, *, filter_: str = "available") -> list[MediaRequest]:
                from reaper.clients.base import IntegrationError

                raise IntegrationError("seerr", "boom")

        assert await requested_by.build_map(_Broken()) == {}  # type: ignore[arg-type]
