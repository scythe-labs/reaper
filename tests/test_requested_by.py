# SPDX-License-Identifier: AGPL-3.0-or-later
"""The "requested by" join -- built on external ids, and never a gate.

The map keys on tmdb/tvdb rather than the arr instance id, because Seerr and Reaper number
their services differently. This is display-only: the worst a wrong match can do is show
the wrong name, so a loose external-id join is acceptable here in a way it never is on the
delete path.
"""

from __future__ import annotations

from reaper.clients.seerr import MediaRequest, Requester
from reaper.engine.observation import Known, Unknown
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


class _Broken:
    """A Seerr that cannot be read -- stands in for an unreachable second portal."""

    async def all_requests(self, *, filter_: str = "available") -> list[MediaRequest]:
        from reaper.clients.base import IntegrationError

        raise IntegrationError("seerr", "boom")


class TestBuildMap:
    async def test_no_seerr_is_an_empty_map(self) -> None:
        assert await requested_by.build_map([]) == {}

    async def test_a_movie_maps_under_its_tmdb_key(self) -> None:
        seerr = _FakeSeerr([_req(media_type="movie", tmdb_id=603)])
        result = await requested_by.build_map([seerr])  # type: ignore[list-item]
        assert result[requested_by.movie_key(603)] == "Alice"

    async def test_a_show_maps_under_the_show_and_each_requested_season(self) -> None:
        seerr = _FakeSeerr([_req(media_type="tv", tvdb_id=81189, seasons=(2, 3))])
        result = await requested_by.build_map([seerr])  # type: ignore[list-item]
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
        result = await requested_by.build_map([seerr])  # type: ignore[list-item]
        assert result[requested_by.movie_key(1)] == "Alice + 2 others"

    async def test_requests_are_merged_across_every_seerr(self) -> None:
        # The reported bug: a title requested only in the SECOND portal must still map.
        first = _FakeSeerr([_req(tmdb_id=1, requester=Requester(1, 1, "a", "Alice", None))])
        second = _FakeSeerr([_req(tmdb_id=2, requester=Requester(2, 2, "b", "Bob", None))])
        result = await requested_by.build_map([first, second])  # type: ignore[list-item]
        assert result[requested_by.movie_key(1)] == "Alice"
        assert result[requested_by.movie_key(2)] == "Bob"

    async def test_the_same_title_in_two_portals_does_not_duplicate_a_name(self) -> None:
        # Alice asked for the same movie on both portals: one name, not "Alice + 1 other".
        first = _FakeSeerr([_req(tmdb_id=1, requester=Requester(1, 1, "a", "Alice", None))])
        second = _FakeSeerr([_req(tmdb_id=1, requester=Requester(1, 1, "a", "Alice", None))])
        result = await requested_by.build_map([first, second])  # type: ignore[list-item]
        assert result[requested_by.movie_key(1)] == "Alice"

    async def test_an_unreachable_portal_is_best_effort_not_a_wipe(self) -> None:
        # Soft display map: one broken portal must not blank the reachable one's names.
        good = _FakeSeerr([_req(tmdb_id=1, requester=Requester(1, 1, "a", "Alice", None))])
        result = await requested_by.build_map([good, _Broken()])  # type: ignore[list-item]
        assert result[requested_by.movie_key(1)] == "Alice"

    async def test_no_seerr_at_all_is_an_empty_map(self) -> None:
        assert await requested_by.build_map([_Broken()]) == {}  # type: ignore[list-item]


class TestRequestIndex:
    """The three-state fact index -- the fail-closed side, used to score, not to display."""

    async def test_no_seerr_is_unavailable_and_answers_unknown(self) -> None:
        index = await requested_by.build_request_index([])
        assert index.available is False
        # Even a real id cannot be answered without a loaded index: fail closed to Unknown,
        # never "not requested" (which would add delete pressure).
        assert isinstance(index.movie_requested(603), Unknown)
        assert isinstance(index.season_requested(81189, 2), Unknown)

    async def test_a_requested_movie_is_known_true(self) -> None:
        seerr = _FakeSeerr([_req(media_type="movie", tmdb_id=603)])
        index = await requested_by.build_request_index([seerr])  # type: ignore[list-item]
        obs = index.movie_requested(603)
        assert isinstance(obs, Known) and obs.value is True

    async def test_a_loaded_index_answers_not_requested_as_known_false(self) -> None:
        seerr = _FakeSeerr([_req(media_type="movie", tmdb_id=603)])
        index = await requested_by.build_request_index([seerr])  # type: ignore[list-item]
        obs = index.movie_requested(999)
        assert isinstance(obs, Known) and obs.value is False

    async def test_a_movie_with_no_tmdb_id_is_unknown_even_when_loaded(self) -> None:
        seerr = _FakeSeerr([_req(media_type="movie", tmdb_id=603)])
        index = await requested_by.build_request_index([seerr])  # type: ignore[list-item]
        # No id to join on -> we cannot assert "not requested" -> Unknown, not False.
        assert isinstance(index.movie_requested(None), Unknown)

    async def test_a_season_is_requested_via_the_whole_show(self) -> None:
        # A request that names no specific season still registers the show key.
        seerr = _FakeSeerr([_req(media_type="tv", tvdb_id=81189)])
        index = await requested_by.build_request_index([seerr])  # type: ignore[list-item]
        obs = index.season_requested(81189, 5)
        assert isinstance(obs, Known) and obs.value is True

    async def test_a_season_is_requested_by_its_own_number(self) -> None:
        seerr = _FakeSeerr([_req(media_type="tv", tvdb_id=81189, seasons=(2,))])
        index = await requested_by.build_request_index([seerr])  # type: ignore[list-item]
        assert index.season_requested(81189, 2).value is True  # type: ignore[union-attr]

    async def test_a_show_that_was_never_requested_is_known_false(self) -> None:
        seerr = _FakeSeerr([_req(media_type="tv", tvdb_id=81189, seasons=(2,))])
        index = await requested_by.build_request_index([seerr])  # type: ignore[list-item]
        obs = index.season_requested(99999, 2)
        assert isinstance(obs, Known) and obs.value is False

    async def test_a_request_in_the_second_portal_is_known_true(self) -> None:
        # A movie requested only in the second Seerr must read as requested, not "not
        # requested" -- the scoring half of the reported bug.
        first = _FakeSeerr([_req(media_type="movie", tmdb_id=603)])
        second = _FakeSeerr([_req(media_type="movie", tmdb_id=1234)])
        index = await requested_by.build_request_index([first, second])  # type: ignore[list-item]
        obs = index.movie_requested(1234)
        assert isinstance(obs, Known) and obs.value is True

    async def test_an_unreachable_seerr_is_unavailable_and_answers_unknown(self) -> None:
        index = await requested_by.build_request_index([_Broken()])  # type: ignore[list-item]
        assert index.available is False
        assert isinstance(index.movie_requested(603), Unknown)

    async def test_one_unreachable_portal_degrades_the_whole_index(self) -> None:
        # The fail-closed regression: with a reachable portal AND an unreachable one, the
        # index must NOT confidently answer "not requested" (Known False) off the partial
        # view -- it degrades to Unknown so no requested title gains delete pressure.
        good = _FakeSeerr([_req(media_type="movie", tmdb_id=603)])
        index = await requested_by.build_request_index([good, _Broken()])  # type: ignore[list-item]
        assert index.available is False
        # Even the id we DID read from the good portal is Unknown, not Known(True/False):
        # the whole set degrades, because a title could be requested in the blind portal.
        assert isinstance(index.movie_requested(603), Unknown)
        assert isinstance(index.movie_requested(999), Unknown)
