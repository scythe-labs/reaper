# SPDX-License-Identifier: AGPL-3.0-or-later
"""The contributor dump tool, and the one property it exists to hold.

``test_no_identifying_field_survives`` is the reason this file exists. The tool asks a
stranger to send their watch history to people they have never met, and the whole basis for
that is a promise about what is in the file. Tautulli hands out far more than the numbers
Reaper scores on: a history row carries ``ip_address``, ``user``, ``friendly_name``,
``machine_id``, ``platform`` and ``player``, a user row carries ``email`` and ``username``,
and an episode's ``media_info`` carries the file path. So the fake server here answers every
endpoint with those fields populated, and the test greps the serialized dump for each value.

That shape is deliberate. A test asserting the dump has the RIGHT keys passes just as well
when a new Tautulli version adds a field nobody has thought about, which is the failure this
tool cannot afford. Asserting the wrong values are absent fails instead.
"""

from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
from typing import Any

import pytest

from tests._generators import load_script

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tautulli_anon_dump.py"


@pytest.fixture(scope="module")
def dump_tool() -> Any:
    return load_script(SCRIPT)


#: Values the fake server volunteers and the dump must never carry. Every one of these is a
#: field Tautulli genuinely returns, checked against a live server.
POISON = (
    "A Movie Nobody Named",
    "A Show Nobody Named",
    "Episode One",
    "/media/movies/a-movie/a-movie.mkv",
    "203.0.113.44",
    "someone@example.invalid",
    "someone",
    "Someone Friendly",
    "machine-abcdef",
    "Chrome",
    "Living Room",
    "Movies On A Disk",
    "tt0000001",
    "plex://movie/abcdef",
)


class FakeTautulli:
    """Answers every endpoint the tool calls, with the identifying fields filled in."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, cmd: str, **params: Any) -> Any:
        self.calls += 1
        if cmd == "get_libraries":
            return [
                {
                    "section_id": "1",
                    "section_type": "movie",
                    "section_name": "Movies On A Disk",
                    "count": "1",
                },
                {
                    "section_id": "2",
                    "section_type": "show",
                    "section_name": "Movies On A Disk",
                    "count": "1",
                },
            ]
        if cmd == "get_library_media_info":
            kind = "movie" if str(params.get("section_id")) == "1" else "show"
            if params.get("start", 0):
                return {"data": []}
            return {
                "data": [
                    {
                        "rating_key": "101" if kind == "movie" else "201",
                        "media_type": kind,
                        "title": "A Movie Nobody Named"
                        if kind == "movie"
                        else "A Show Nobody Named",
                        "sort_title": "A Movie Nobody Named",
                        "year": "2011",
                        "file_size": "1449551462" if kind == "movie" else "",
                        "added_at": "1600000000",
                        "last_played": 1700000000,
                        "play_count": 3,
                        "video_resolution": "1080",
                        "section_id": params.get("section_id"),
                    }
                ]
            }
        if cmd == "get_metadata":
            return {
                "rating_key": params["rating_key"],
                "title": "A Movie Nobody Named",
                "full_title": "A Movie Nobody Named",
                "summary": "Something happens to someone.",
                "guid": "plex://movie/abcdef",
                "guids": ["imdb://tt0000001", "tmdb://99", "tvdb://5"],
                "genres": ["Drama", "Comedy"],
                "originally_available_at": "2011-04-02",
                "library_name": "Movies On A Disk",
                "media_info": [{"parts": [{"file": "/media/movies/a-movie/a-movie.mkv"}]}],
            }
        if cmd == "get_history":
            if params.get("start", 0):
                return {"data": []}
            return {
                "data": [
                    {
                        "row_id": 1,
                        "rating_key": "301",
                        "parent_rating_key": "202",
                        "grandparent_rating_key": "201",
                        "user_id": 77,
                        "user": "someone",
                        "friendly_name": "Someone Friendly",
                        "ip_address": "203.0.113.44",
                        "machine_id": "machine-abcdef",
                        "platform": "Chrome",
                        "player": "Chrome",
                        "location": "Living Room",
                        "guid": "plex://movie/abcdef",
                        "title": "Episode One",
                        "full_title": "A Show Nobody Named - Episode One",
                        "date": 1700000000,
                        "media_type": "episode",
                        "media_index": 4,
                        "parent_media_index": 2,
                        "percent_complete": 97,
                        "watched_status": 1,
                    },
                    {
                        "row_id": 2,
                        "rating_key": "302",
                        "user_id": 77,
                        "ip_address": "203.0.113.44",
                        "date": 1700100000,
                        "media_type": "movie",
                        "percent_complete": 12,
                        # Tautulli did not say. This must survive as null, never as 0.0.
                        "watched_status": "",
                    },
                    {
                        # Still playing. Tautulli gives an in-progress session no row_id,
                        # and history_sync drops it, so this dump must drop it too.
                        "row_id": None,
                        "rating_key": "303",
                        "user_id": 77,
                        "date": 1700200000,
                        "media_type": "movie",
                        "percent_complete": 4,
                        "watched_status": 0,
                    },
                ]
            }
        if cmd == "get_users":
            return [
                {
                    "user_id": 77,
                    "username": "someone",
                    "email": "someone@example.invalid",
                    "friendly_name": "Someone Friendly",
                    "keep_history": 1,
                    "is_active": 1,
                    "is_home_user": 0,
                }
            ]
        raise AssertionError(f"unexpected command {cmd}")

    def paged(
        self,
        cmd: str,
        *,
        cap: int | None = None,
        page: int = 1000,
        note: Any = None,
        **params: Any,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            data = self(cmd, start=start, length=page, **params)
            batch = (data or {}).get("data") or []
            rows.extend(batch)
            if not batch:
                return rows
            start += len(batch)

    def spread(self, work: Any, over: Any, *, jobs: int, note: Any = None) -> list[Any]:
        """Serial, so a test's ordering is the code's ordering and not the pool's."""
        return [work(item) for item in over]

    def children(self, rating_key: int | str) -> list[dict[str, Any]]:
        if int(rating_key) == 201:
            return [
                {
                    "rating_key": "202",
                    "media_index": "2",
                    "title": "Season 2",
                    "added_at": "1600000000",
                    "last_viewed_at": "1700000000",
                }
            ]
        return [
            {"rating_key": "301", "media_index": "1", "title": "Episode One"},
            {"rating_key": "302", "media_index": "2", "title": "Episode Two"},
        ]


@pytest.fixture
def built(dump_tool: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A full dump off the fake server, with the ratings dataset served from memory."""
    tsv = b"tconst\taverageRating\tnumVotes\ntt0000001\t7.4\t1847392\n"
    payload = io.BytesIO()
    with gzip.GzipFile(fileobj=payload, mode="wb") as fh:
        fh.write(tsv)

    def fake_urlopen(request: Any, **kwargs: Any) -> io.BytesIO:
        return io.BytesIO(payload.getvalue())

    monkeypatch.setattr(dump_tool.urllib.request, "urlopen", fake_urlopen)
    mask = dump_tool.Mask(tmp_path / "salt.json")
    api = FakeTautulli()
    dump: dict[str, Any] = dump_tool.build(api, mask, cap=None, quick=False, note=lambda _: None)
    return dump


class TestWhatLeaves:
    def test_no_identifying_field_survives(self, built: dict[str, Any]) -> None:
        serialized = json.dumps(built)
        leaked = [value for value in POISON if value in serialized]
        assert leaked == [], f"the dump carried identifying values: {leaked}"

    def test_the_imdb_id_does_not_survive_its_own_lookup(self, built: dict[str, Any]) -> None:
        movie = next(i for i in built["items"] if i["type"] == "movie")
        assert movie["imdb_rating_tenths"] == 74
        assert "tt0000001" not in json.dumps(built)

    def test_votes_are_rounded_away_from_the_exact_count(self, built: dict[str, Any]) -> None:
        movie = next(i for i in built["items"] if i["type"] == "movie")
        assert movie["imdb_votes"] == 1_800_000

    def test_size_is_rounded_to_a_hundred_megabytes(self, built: dict[str, Any]) -> None:
        movie = next(i for i in built["items"] if i["type"] == "movie")
        assert movie["size_bytes"] == 1_400_000_000

    def test_genres_and_structure_do_survive(self, built: dict[str, Any]) -> None:
        movie = next(i for i in built["items"] if i["type"] == "movie")
        assert movie["genres"] == ["Drama", "Comedy"]
        assert built["seasons"][0]["seasons"][0]["number"] == 2
        assert built["seasons"][0]["seasons"][0]["episodes"] == 2

    def test_an_unreported_watched_status_stays_null(self, built: dict[str, Any]) -> None:
        movie_play = next(p for p in built["plays"] if p["type"] == "movie")
        assert movie_play["watched_status"] is None, "an absent status must not become 0.0"


class TestAgreementWithHistorySync:
    """The dump has to hold what ``services.history_sync`` would have stored, or a replay
    against it produces verdicts a real scan never would, and the gap reads as a finding."""

    def test_a_session_still_playing_is_not_history(self, built: dict[str, Any]) -> None:
        # A null row_id is an in-progress session. history_sync skips those, so a dump that
        # kept them would carry plays Reaper's own mirror never holds.
        assert len(built["plays"]) == 2
        assert all(p["item"] != "303" for p in built["plays"])

    def test_history_is_asked_for_ungrouped(self, dump_tool: Any, tmp_path: Path) -> None:
        # Tautulli groups consecutive plays of the same item unless told not to, and the
        # default is what a caller that says nothing gets. Measured against a live instance:
        # 309,013 rows came back where it held 425,983. Those folded rows ARE the rewatches
        # (``services.rewatch.viewing_count`` clusters plays into viewings itself), so a
        # grouped dump reports a habitual rewatcher as a single viewing.
        sent: list[Any] = []

        class Recording(FakeTautulli):
            def __call__(self, cmd: str, **params: Any) -> Any:
                if cmd == "get_history":
                    sent.append(params.get("grouping"))
                return super().__call__(cmd, **params)

        mask = dump_tool.Mask(tmp_path / "salt.json")
        dump_tool.collect_plays(Recording(), mask, cap=None, note=lambda _: None)
        assert sent, "history was never fetched"
        assert all(g == 0 for g in sent), f"history was asked for with grouping={sent}"

    def test_percent_complete_is_coerced_the_way_the_column_demands(
        self, built: dict[str, Any]
    ) -> None:
        # history_sync writes `int(percent_complete or 0)` into a NOT NULL column. A null
        # here would be more honest and would disagree with the mirror, which is worse.
        assert all(isinstance(p["percent_complete"], int) for p in built["plays"])


class TestWhatTheLibraryNoLongerHolds:
    def test_a_play_of_a_vanished_item_is_counted(self, dump_tool: Any) -> None:
        # 39% of watched movies and 46% of watched shows were already gone from the first
        # real library this ran against. A dump that did not say so would read as though
        # every play it carries is about something still on disk.
        items = [{"token": "kept", "type": "movie"}, {"token": "show_kept", "type": "show"}]
        plays = [
            {"item": "kept", "type": "movie", "show": None},
            {"item": "gone", "type": "movie", "show": None},
            {"item": "ep1", "type": "episode", "show": "show_kept"},
            {"item": "ep2", "type": "episode", "show": "show_gone"},
        ]
        assert dump_tool.orphans(items, plays) == {
            "movies": 1,
            "movies_played": 2,
            "shows": 1,
            "shows_played": 2,
        }

    def test_a_library_that_lost_nothing_reports_none_gone(self, dump_tool: Any) -> None:
        items = [{"token": "kept", "type": "movie"}]
        plays = [{"item": "kept", "type": "movie", "show": None}]
        counted = dump_tool.orphans(items, plays)
        assert counted["movies"] == 0 and counted["movies_played"] == 1


class TestTheClock:
    def test_intervals_survive_the_shift(self, dump_tool: Any, tmp_path: Path) -> None:
        mask = dump_tool.Mask(tmp_path / "salt.json")
        first, second = mask.when(1_700_000_000), mask.when(1_700_100_000)
        assert second - first == 100_000

    def test_the_reference_now_moves_with_the_plays(self, built: dict[str, Any]) -> None:
        # "Days since last play" is computed against reference_now, so a shift applied to
        # one and not the other would move every dormancy figure in the dump.
        latest = max(p["at"] for p in built["plays"])
        assert built["reference_now"] > latest

    def test_absent_and_zero_timestamps_stay_absent(self, dump_tool: Any, tmp_path: Path) -> None:
        mask = dump_tool.Mask(tmp_path / "salt.json")
        assert mask.when("") is None
        assert mask.when(0) is None
        assert mask.when(None) is None


class TestTheSalt:
    def test_a_reused_salt_reproduces_every_token(self, dump_tool: Any, tmp_path: Path) -> None:
        path = tmp_path / "salt.json"
        first = dump_tool.Mask(path)
        again = dump_tool.Mask(path)
        assert first.fresh and not again.fresh
        assert first.token("movie", 101) == again.token("movie", 101)
        assert first.shift_days == again.shift_days

    def test_a_different_salt_produces_different_tokens(
        self, dump_tool: Any, tmp_path: Path
    ) -> None:
        one = dump_tool.Mask(tmp_path / "a.json")
        two = dump_tool.Mask(tmp_path / "b.json")
        assert one.token("movie", 101) != two.token("movie", 101)

    def test_the_rating_key_is_not_recoverable_by_enumeration(
        self, dump_tool: Any, tmp_path: Path
    ) -> None:
        # A bare hash of a small integer is undone by counting to it, which is why the
        # token is an HMAC under a secret salt rather than a digest of the key.
        mask = dump_tool.Mask(tmp_path / "salt.json")
        import hashlib

        assert mask.token("movie", 101) != hashlib.sha256(b"101").hexdigest()[:12]

    def test_the_salt_file_is_owner_only(self, dump_tool: Any, tmp_path: Path) -> None:
        path = tmp_path / "salt.json"
        dump_tool.Mask(path)
        assert path.stat().st_mode & 0o077 == 0

    def test_a_kind_prefix_keeps_namespaces_apart(self, dump_tool: Any, tmp_path: Path) -> None:
        # A season and a movie can hold the same rating key on different servers, and a
        # collision would merge two unrelated items in the dump.
        mask = dump_tool.Mask(tmp_path / "salt.json")
        assert mask.token("movie", 1) != mask.token("season", 1)


class TestTheAddress:
    @pytest.mark.parametrize("scheme", ["file", "ftp", "data", "gopher"])
    def test_a_non_http_address_is_refused(self, dump_tool: Any, scheme: str) -> None:
        # urlopen honors file: and reads whatever is behind it. The address is typed by
        # whoever runs the tool, so it is checked before the opener sees it.
        with pytest.raises(ValueError, match="only http and https"):
            dump_tool.http_open(f"{scheme}:///etc/passwd")

    def test_a_refused_address_never_reaches_urlopen(
        self, dump_tool: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("urlopen was called with an unchecked scheme")

        monkeypatch.setattr(dump_tool.urllib.request, "urlopen", explode)
        with pytest.raises(ValueError, match="only http and https"):
            dump_tool.http_open("file:///etc/passwd")


class TestPaging:
    def test_a_cap_stops_the_walk(self, dump_tool: Any) -> None:
        asked: list[int] = []

        def fake(cmd: str, **params: Any) -> Any:
            asked.append(params["length"])
            return {"data": [{"n": i} for i in range(params["length"])]}

        rows = dump_tool.Tautulli.paged(_Stub(fake), "get_history", cap=1500)
        assert len(rows) == 1500
        # The second page asks for the remainder, never a full page it would discard.
        assert asked == [1000, 500]

    def test_an_over_long_page_still_advances_the_cursor(self, dump_tool: Any) -> None:
        # A server answering with more rows than the page asked for would leave a cursor
        # advanced by the requested length behind the data, and re-read the overlap forever.
        starts: list[int] = []

        def fake(cmd: str, **params: Any) -> Any:
            starts.append(params["start"])
            if len(starts) > 3:
                return {"data": []}
            return {"data": [{"n": i} for i in range(1500)]}

        rows = dump_tool.Tautulli.paged(_Stub(fake), "get_history")
        assert starts == [0, 1500, 3000, 4500]
        assert len(rows) == 4500

    def test_a_slow_page_is_retried_at_half_the_size(self, dump_tool: Any) -> None:
        # A history page's cost is nearly all fixed per request, so the page size sets the
        # runtime. A server slower than the one it was measured on has to degrade to a
        # longer run rather than to no dump at all.
        asked: list[int] = []

        def fake(cmd: str, **params: Any) -> Any:
            asked.append(params["length"])
            if params["length"] > 6250:
                raise RuntimeError("get_history failed after 3 tries: timed out")
            return {"data": [{"n": i} for i in range(params["length"] - 1)]}

        rows = dump_tool.Tautulli.paged(_Stub(fake), "get_history", page=25000)
        assert asked == [25000, 12500, 6250]
        assert len(rows) == 6249

    def test_a_page_that_will_not_shrink_further_raises(self, dump_tool: Any) -> None:
        def fake(cmd: str, **params: Any) -> Any:
            raise RuntimeError("get_history failed after 3 tries: timed out")

        with pytest.raises(RuntimeError, match="timed out"):
            dump_tool.Tautulli.paged(_Stub(fake), "get_history", page=1000)


class _Stub:
    """Just enough of ``Tautulli`` for ``paged`` to be exercised without a server."""

    def __init__(self, responder: Any) -> None:
        self._responder = responder

    def __call__(self, cmd: str, **params: Any) -> Any:
        return self._responder(cmd, **params)


class TestParsing:
    @pytest.mark.parametrize(
        ("meta", "expected"),
        [
            ({"guids": ["imdb://tt0000001", "tmdb://9"]}, "tt0000001"),
            ({"guids": [], "guid": "com.plexapp.agents.imdb://tt0000002?lang=en"}, "tt0000002"),
            ({"guids": ["tmdb://9"], "guid": "plex://movie/abc"}, None),
            ({}, None),
        ],
    )
    def test_imdb_id_reads_both_agent_generations(
        self, dump_tool: Any, meta: dict[str, Any], expected: str | None
    ) -> None:
        assert dump_tool.imdb_id(meta) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"), [("", None), (None, None), ("12", 12), ("12.7", 12), ("nope", None)]
    )
    def test_as_int_treats_the_empty_string_as_absent(
        self, dump_tool: Any, raw: Any, expected: int | None
    ) -> None:
        assert dump_tool.as_int(raw) == expected

    @pytest.mark.parametrize(
        ("votes", "expected"), [(5, 5), (99, 99), (100, 100), (1847392, 1800000), (12345, 12000)]
    )
    def test_votes_round_to_two_significant_figures(
        self, dump_tool: Any, votes: int, expected: int
    ) -> None:
        assert dump_tool.round_votes(votes) == expected
