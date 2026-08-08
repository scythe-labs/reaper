# SPDX-License-Identifier: AGPL-3.0-or-later
"""The "requested by" join -- and never a gate.

By default the map keys on tmdb/tvdb (a loose union: every copy of a title shows everyone who
asked for the title). When the operator maps a Seerr service to a Reaper instance, a request
also files under the exact copy's media_key, so a title kept in two libraries attributes each
copy to the right person. Either way this is display-only: the worst a wrong match can do is
show the wrong name, so a loose join is acceptable here in a way it never is on the delete path.
"""

from __future__ import annotations

from reaper.clients.seerr import MediaRequest, Requester
from reaper.engine.observation import Known, Unknown
from reaper.services import requested_by
from tests._fakes import FakeSeerr


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


def _src(client: object, service_map: dict[str, int] | None = None) -> requested_by.SeerrSource:
    """Wrap a fake Seerr client as a source, optionally with a serviceId -> instance map."""
    return requested_by.SeerrSource(
        client=client,  # type: ignore[arg-type]
        service_instance_map=service_map or {},
    )


# `movie_key`/`show_key`/`season_key` take `int | None` and so are declared to return
# `str | None`: a request with no id has no key. Every call below passes a literal id, where
# None is unreachable -- but the signature cannot say so, and mypy is right that indexing a
# `dict[str, str]` with `str | None` is not allowed. These narrow it once, and the assert is
# what makes the narrowing honest rather than a cast: a helper that started returning None
# for a real id fails HERE, naming the key, instead of raising KeyError three lines later.
def _movie(tmdb_id: int) -> str:
    key = requested_by.movie_key(tmdb_id)
    assert key is not None
    return key


def _show(tvdb_id: int) -> str:
    key = requested_by.show_key(tvdb_id)
    assert key is not None
    return key


def _season(tvdb_id: int, season: int) -> str:
    key = requested_by.season_key(tvdb_id, season)
    assert key is not None
    return key


def _movie_at(instance_id: int, arr_id: int) -> str:
    key = requested_by.movie_instance_key(instance_id, arr_id)
    assert key is not None
    return key


def _show_at(instance_id: int, arr_id: int) -> str:
    key = requested_by.show_instance_key(instance_id, arr_id)
    assert key is not None
    return key


def _season_at(instance_id: int, arr_id: int, season: int) -> str:
    key = requested_by.season_instance_key(instance_id, arr_id, season)
    assert key is not None
    return key


def _rating(rating_key: object) -> str:
    key = requested_by.rating_key_key(rating_key)
    assert key is not None
    return key


class TestBuildMap:
    async def test_no_seerr_is_an_empty_map(self) -> None:
        assert await requested_by.build_map([]) == {}

    async def test_a_movie_maps_under_its_tmdb_key(self) -> None:
        seerr = FakeSeerr([_req(media_type="movie", tmdb_id=603)])
        result = await requested_by.build_map([_src(seerr)])
        assert result[_movie(603)] == "Alice"

    async def test_a_show_maps_under_the_show_and_each_requested_season(self) -> None:
        seerr = FakeSeerr([_req(media_type="tv", tvdb_id=81189, seasons=(2, 3))])
        result = await requested_by.build_map([_src(seerr)])
        assert result[_show(81189)] == "Alice"
        assert result[_season(81189, 2)] == "Alice"
        assert result[_season(81189, 3)] == "Alice"
        assert _season(81189, 1) not in result  # season 1 was not requested

    async def test_several_requesters_are_summarised(self) -> None:
        seerr = FakeSeerr(
            [
                _req(tmdb_id=1, requester=Requester(1, 1, "a", "Alice", None)),
                _req(tmdb_id=1, requester=Requester(2, 2, "b", "Bob", None)),
                _req(tmdb_id=1, requester=Requester(3, 3, "c", "Cara", None)),
            ]
        )
        result = await requested_by.build_map([_src(seerr)])
        assert result[_movie(1)] == "Alice + 2 others"

    async def test_requests_are_merged_across_every_seerr(self) -> None:
        # The reported bug: a title requested only in the SECOND portal must still map.
        first = FakeSeerr([_req(tmdb_id=1, requester=Requester(1, 1, "a", "Alice", None))])
        second = FakeSeerr([_req(tmdb_id=2, requester=Requester(2, 2, "b", "Bob", None))])
        result = await requested_by.build_map([_src(first), _src(second)])
        assert result[_movie(1)] == "Alice"
        assert result[_movie(2)] == "Bob"

    async def test_the_same_title_in_two_portals_does_not_duplicate_a_name(self) -> None:
        # Alice asked for the same movie on both portals: one name, not "Alice + 1 other".
        first = FakeSeerr([_req(tmdb_id=1, requester=Requester(1, 1, "a", "Alice", None))])
        second = FakeSeerr([_req(tmdb_id=1, requester=Requester(1, 1, "a", "Alice", None))])
        result = await requested_by.build_map([_src(first), _src(second)])
        assert result[_movie(1)] == "Alice"

    async def test_an_unreachable_portal_is_best_effort_not_a_wipe(self) -> None:
        # Soft display map: one broken portal must not blank the reachable one's names.
        good = FakeSeerr([_req(tmdb_id=1, requester=Requester(1, 1, "a", "Alice", None))])
        result = await requested_by.build_map([_src(good), _src(FakeSeerr(unreachable=True))])
        assert result[_movie(1)] == "Alice"

    async def test_no_seerr_at_all_is_an_empty_map(self) -> None:
        assert await requested_by.build_map([_src(FakeSeerr(unreachable=True))]) == {}


class TestBuildMapPrecisePerCopy:
    """The service map: a request also files under the exact copy's media_key.

    The multi-Seerr, multi-library case -- a title kept in a main library (added by one Sonarr)
    and a restricted one (added by another) -- so each copy attributes to the person who asked
    for THAT copy, not the union of everyone who asked for the title.
    """

    async def test_a_mapped_movie_files_under_its_exact_media_key(self) -> None:
        # serviceId 2 on this portal adds to Reaper instance 7 (a Radarr). The request's
        # externalServiceId (arr_id) is that Radarr's movie id, so the precise key is the
        # candidate's own media_key radarr:7:55.
        seerr = FakeSeerr([_req(media_type="movie", tmdb_id=603, arr_id=55, arr_instance_id=2)])
        # A movie request reads a Radarr service, so the map key is "radarr:{serviceId}".
        result = await requested_by.build_map([_src(seerr, {"radarr:2": 7})])
        # The precise key IS the candidate's media_key by construction (radarr:{inst}:{arr id}).
        assert _movie_at(7, 55) == "radarr:7:55"
        assert result["radarr:7:55"] == "Alice"
        # The loose tmdb union still exists as the fallback for un-mapped copies.
        assert result[_movie(603)] == "Alice"

    async def test_two_people_two_copies_attribute_to_their_own_copy(self) -> None:
        # Alice asked on the primary portal (serviceId 1 -> instance 7, the main library);
        # Bob on the secondary (serviceId 1 -> instance 8, the restricted library). Same tmdb,
        # different Radarr instances, so different precise media_keys.
        primary = FakeSeerr(
            [
                _req(
                    tmdb_id=603,
                    arr_id=55,
                    arr_instance_id=1,
                    requester=Requester(1, 1, "a", "Alice", None),
                )
            ]
        )
        secondary = FakeSeerr(
            [
                _req(
                    tmdb_id=603,
                    arr_id=99,
                    arr_instance_id=1,
                    requester=Requester(2, 2, "b", "Bob", None),
                )
            ]
        )
        result = await requested_by.build_map(
            [_src(primary, {"radarr:1": 7}), _src(secondary, {"radarr:1": 8})]
        )
        # Each copy attributes to its own requester...
        assert result[_movie_at(7, 55)] == "Alice"
        assert result[_movie_at(8, 99)] == "Bob"
        # ...while the loose union still lists both (the fallback for any un-mapped copy).
        assert result[_movie(603)] == "Alice + 1 other"

    async def test_a_mapped_show_files_under_group_key_and_each_season_key(self) -> None:
        seerr = FakeSeerr(
            [_req(media_type="tv", tvdb_id=81189, seasons=(2, 3), arr_id=42, arr_instance_id=5)]
        )
        # A tv request reads a Sonarr service, so the map key is "sonarr:{serviceId}".
        result = await requested_by.build_map([_src(seerr, {"sonarr:5": 9})])
        assert result[_show_at(9, 42)] == "Alice"
        assert result[_season_at(9, 42, 2)] == "Alice"
        assert result[_season_at(9, 42, 3)] == "Alice"
        assert _season_at(9, 42, 1) not in result

    async def test_an_unmapped_service_keeps_only_the_loose_key(self) -> None:
        # radarr serviceId 3 is not in the map, so no precise key is filed -- today's behavior.
        seerr = FakeSeerr([_req(media_type="movie", tmdb_id=603, arr_id=55, arr_instance_id=3)])
        result = await requested_by.build_map([_src(seerr, {"radarr:2": 7})])
        assert result[_movie(603)] == "Alice"
        assert _movie_at(7, 55) not in result
        assert _movie_at(3, 55) not in result

    async def test_a_request_with_no_arr_id_files_only_the_loose_key(self) -> None:
        # A manual add / dedup case: mapped service, but no externalServiceId to pin the copy.
        seerr = FakeSeerr([_req(media_type="movie", tmdb_id=603, arr_id=None, arr_instance_id=2)])
        result = await requested_by.build_map([_src(seerr, {"radarr:2": 7})])
        assert result[_movie(603)] == "Alice"
        assert not any(k.startswith("radarr:") for k in result)

    async def test_sonarr_and_radarr_service_ids_do_not_collide(self) -> None:
        # THE bug this keying prevents: Seerr numbers Sonarr and Radarr services separately, so
        # both have a serviceId 0. A movie request (radarr 0 -> instance 7) and a tv request
        # (sonarr 0 -> instance 9) must resolve to their OWN instance, not clobber each other.
        seerr = FakeSeerr(
            [
                _req(media_type="movie", tmdb_id=603, arr_id=55, arr_instance_id=0),
                _req(media_type="tv", tvdb_id=81189, seasons=(1,), arr_id=42, arr_instance_id=0),
            ]
        )
        result = await requested_by.build_map([_src(seerr, {"radarr:0": 7, "sonarr:0": 9})])
        assert result[_movie_at(7, 55)] == "Alice"  # radarr 0 -> 7
        assert result[_season_at(9, 42, 1)] == "Alice"  # sonarr 0 -> 9
        # Never crossed: no movie key under the sonarr instance, no season under the radarr one.
        assert _movie_at(9, 55) not in result
        assert _season_at(7, 42, 1) not in result


class TestBuildMapRatingKey:
    """Tier 2, zero-config: a request also files under its Plex rating key, which equals the
    candidate's plex_rating_key on the same Plex server. A portal scanning only its own library
    gets per-copy attribution with no service map; the union still lists everyone as the last
    fallback."""

    def test_rating_key_key_normalizes_str_and_int(self) -> None:
        # The real function, not `_rating`: this is the one case that WANTS the None arm, and
        # the narrowing helper exists to assert it away.
        assert requested_by.rating_key_key("100") == "plex:rk:100"
        assert requested_by.rating_key_key(100) == "plex:rk:100"  # Seerr sends str, Reaper int
        assert requested_by.rating_key_key(None) is None
        assert requested_by.rating_key_key("  ") is None

    async def test_a_movie_files_under_its_rating_key(self) -> None:
        seerr = FakeSeerr([_req(media_type="movie", tmdb_id=603, plex_rating_key="100")])
        result = await requested_by.build_map([_src(seerr)])
        assert result[_rating("100")] == "Alice"
        assert result[_movie(603)] == "Alice"  # union still there

    async def test_two_copies_two_rating_keys_attribute_apart(self) -> None:
        # Two portals, each scanning its own library, so each request carries its own copy's key.
        primary = FakeSeerr(
            [
                _req(
                    tmdb_id=603,
                    plex_rating_key="100",
                    requester=Requester(1, 1, "a", "Alice", None),
                )
            ]
        )
        secondary = FakeSeerr(
            [_req(tmdb_id=603, plex_rating_key="200", requester=Requester(2, 2, "b", "Bob", None))]
        )
        result = await requested_by.build_map([_src(primary), _src(secondary)])
        assert result[_rating(100)] == "Alice"
        assert result[_rating(200)] == "Bob"
        assert result[_movie(603)] == "Alice + 1 other"  # union lists both

    async def test_a_show_files_under_its_show_rating_key(self) -> None:
        # Seerr stores a TV request's ratingKey at the show level, so season lookups match on it.
        seerr = FakeSeerr(
            [_req(media_type="tv", tvdb_id=81189, seasons=(2,), plex_rating_key="500")]
        )
        result = await requested_by.build_map([_src(seerr)])
        assert result[_rating("500")] == "Alice"

    async def test_a_request_with_no_rating_key_files_none(self) -> None:
        seerr = FakeSeerr([_req(media_type="movie", tmdb_id=603, plex_rating_key=None)])
        result = await requested_by.build_map([_src(seerr)])
        assert not any(k.startswith("plex:rk:") for k in result)


class _PlexAwareSeerr(FakeSeerr):
    """A fake Seerr that also answers ``plex_machine_id`` -- for the I-3 namespace guard."""

    def __init__(self, requests: list[MediaRequest], machine_id: str | None) -> None:
        super().__init__(requests)
        self._machine_id = machine_id

    async def plex_machine_id(self) -> str | None:
        return self._machine_id


class TestBuildMapRatingKeyNamespace:
    """I-3: the rating-key tier is filed only when the portal is on the SAME Plex as Reaper.
    Rating keys are unique per server, so a portal on a different Plex would file keys that
    collide with Reaper's candidates and name a requester on an unrelated item."""

    async def test_a_matching_portal_still_files_the_rating_key(self) -> None:
        seerr = _PlexAwareSeerr(
            [_req(media_type="movie", tmdb_id=603, plex_rating_key="100")], machine_id="SAME"
        )
        result = await requested_by.build_map([_src(seerr)], reaper_plex_machine_id="SAME")
        assert result[_rating("100")] == "Alice"

    async def test_a_foreign_portal_skips_the_rating_key_but_keeps_the_loose_union(self) -> None:
        seerr = _PlexAwareSeerr(
            [_req(media_type="movie", tmdb_id=603, plex_rating_key="100")], machine_id="OTHER"
        )
        result = await requested_by.build_map([_src(seerr)], reaper_plex_machine_id="SAME")
        # The colliding rating-key tier is dropped; the server-agnostic tmdb union survives.
        assert not any(k.startswith("plex:rk:") for k in result)
        assert result[_movie(603)] == "Alice"

    async def test_an_unknown_reaper_id_keeps_todays_behavior(self) -> None:
        # No Reaper machine id (no Plex, or unreadable): the tier is filed exactly as before,
        # and the portal's own machine id is never even read.
        seerr = _PlexAwareSeerr(
            [_req(media_type="movie", tmdb_id=603, plex_rating_key="100")], machine_id="OTHER"
        )
        result = await requested_by.build_map([_src(seerr)], reaper_plex_machine_id=None)
        assert result[_rating("100")] == "Alice"

    async def test_an_unknown_portal_id_keeps_the_tier(self) -> None:
        # Reaper's id is known, but the portal's /settings/plex could not be read (None): keep
        # the tier rather than dropping a requester on an unproven mismatch.
        seerr = _PlexAwareSeerr(
            [_req(media_type="movie", tmdb_id=603, plex_rating_key="100")], machine_id=None
        )
        result = await requested_by.build_map([_src(seerr)], reaper_plex_machine_id="SAME")
        assert result[_rating("100")] == "Alice"


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
        seerr = FakeSeerr([_req(media_type="movie", tmdb_id=603)])
        index = await requested_by.build_request_index([seerr])
        obs = index.movie_requested(603)
        assert isinstance(obs, Known) and obs.value is True

    async def test_a_loaded_index_answers_not_requested_as_known_false(self) -> None:
        seerr = FakeSeerr([_req(media_type="movie", tmdb_id=603)])
        index = await requested_by.build_request_index([seerr])
        obs = index.movie_requested(999)
        assert isinstance(obs, Known) and obs.value is False

    async def test_a_movie_with_no_tmdb_id_is_unknown_even_when_loaded(self) -> None:
        seerr = FakeSeerr([_req(media_type="movie", tmdb_id=603)])
        index = await requested_by.build_request_index([seerr])
        # No id to join on -> we cannot assert "not requested" -> Unknown, not False.
        assert isinstance(index.movie_requested(None), Unknown)

    async def test_a_season_is_requested_via_the_whole_show(self) -> None:
        # A request that names no specific season still registers the show key.
        seerr = FakeSeerr([_req(media_type="tv", tvdb_id=81189)])
        index = await requested_by.build_request_index([seerr])
        obs = index.season_requested(81189, 5)
        assert isinstance(obs, Known) and obs.value is True

    async def test_a_season_is_requested_by_its_own_number(self) -> None:
        seerr = FakeSeerr([_req(media_type="tv", tvdb_id=81189, seasons=(2,))])
        index = await requested_by.build_request_index([seerr])
        assert index.season_requested(81189, 2).value is True  # type: ignore[union-attr]

    async def test_a_show_that_was_never_requested_is_known_false(self) -> None:
        seerr = FakeSeerr([_req(media_type="tv", tvdb_id=81189, seasons=(2,))])
        index = await requested_by.build_request_index([seerr])
        obs = index.season_requested(99999, 2)
        assert isinstance(obs, Known) and obs.value is False

    async def test_a_request_in_the_second_portal_is_known_true(self) -> None:
        # A movie requested only in the second Seerr must read as requested, not "not
        # requested" -- the scoring half of the reported bug.
        first = FakeSeerr([_req(media_type="movie", tmdb_id=603)])
        second = FakeSeerr([_req(media_type="movie", tmdb_id=1234)])
        index = await requested_by.build_request_index([first, second])
        obs = index.movie_requested(1234)
        assert isinstance(obs, Known) and obs.value is True

    async def test_an_unreachable_seerr_is_unavailable_and_answers_unknown(self) -> None:
        index = await requested_by.build_request_index([FakeSeerr(unreachable=True)])
        assert index.available is False
        assert isinstance(index.movie_requested(603), Unknown)

    async def test_one_unreachable_portal_degrades_the_whole_index(self) -> None:
        # The fail-closed regression: with a reachable portal AND an unreachable one, the
        # index must NOT confidently answer "not requested" (Known False) off the partial
        # view -- it degrades to Unknown so no requested title gains delete pressure.
        good = FakeSeerr([_req(media_type="movie", tmdb_id=603)])
        index = await requested_by.build_request_index([good, FakeSeerr(unreachable=True)])
        assert index.available is False
        # Even the id we DID read from the good portal is Unknown, not Known(True/False):
        # the whole set degrades, because a title could be requested in the blind portal.
        assert isinstance(index.movie_requested(603), Unknown)
        assert isinstance(index.movie_requested(999), Unknown)
