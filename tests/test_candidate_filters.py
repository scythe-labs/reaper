# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review queue's filters: search, media type, and requested-only.

Filters narrow the frozen snapshot, they never re-decide it. Each one is a display-side
convenience over candidates that were already judged, and the queue also carries the
display fields (poster, blurb, requested-by, show grouping) captured at scan time.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session

from reaper.api.routes import _split_search_year
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Candidate, Snapshot, WhitelistEntry
from reaper.main import create_app
from reaper.services.whitelist import show_key

from ._auth import login


def _candidate(**kw: object) -> Candidate:
    base: dict[str, object] = {
        "media_type": "movie",
        "size_bytes": 1_000_000_000,
        "verdict": "condemn",
        "score": 90,
        "coverage_bp": 10_000,
        "explanation_json": "{}",
        "created_at": utcnow(),
    }
    base.update(kw)
    return Candidate(**base)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)

    now = utcnow()
    with Session(engine) as session:
        snap = Snapshot(
            created_at=now, policy_hash="a" * 64, horizon_at=now, item_count=3, degraded=False
        )
        session.add(snap)
        session.flush()
        session.add_all(
            [
                _candidate(
                    snapshot_id=snap.id,
                    media_key="radarr:1:10",
                    plex_rating_key=555,  # -> poster served from /api/poster/555
                    title="Example Alpha",
                    year=1979,
                    summary="A crew is hunted.",
                    requested_by="Alice",
                    genres_json='["Comedy", "Horror"]',
                    library_title="Movies",
                ),
                _candidate(
                    snapshot_id=snap.id,
                    media_key="radarr:1:11",
                    title="Example Zulu",
                    year=1995,
                    requested_by=None,
                    # "Comedy Special" proves the genre filter matches whole terms, not
                    # substrings; a filter for Comedy must not drag this row along.
                    genres_json='["Comedy Special"]',
                    # A distinct library whose name contains "Movies": the library filter must
                    # match the whole name, not drag this in on a filter for "Movies".
                    library_title="4K Movies",
                ),
                _candidate(
                    snapshot_id=snap.id,
                    media_key="sonarr:1:5:5",
                    title="Example Mid · Season 5",
                    media_type="season",
                    group_key="sonarr:1:5",
                    group_title="Example Mid",
                    requested_by="Bob",
                    # Malformed on purpose: the genre filter must skip it, never 500.
                    genres_json="not json",
                    library_title="TV Shows",
                ),
            ]
        )
        # Hand overrides: one item-level spare, and one show-level reap the season
        # inherits through whitelist.effective_override's own-key-beats-show precedence.
        session.add_all(
            [
                WhitelistEntry(
                    media_key="radarr:1:11", title="Example Zulu", decision="spare", created_at=now
                ),
                WhitelistEntry(
                    media_key="sonarr:1:5", title="Example Mid", decision="reap", created_at=now
                ),
            ]
        )
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


def _titles(rows: list[dict[str, object]]) -> set[str]:
    return {str(r["title"]) for r in rows}


class TestFilters:
    def test_unfiltered_returns_every_condemned_item(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn").json()
        # Two, not three: Example Zulu is condemned by policy but spared by hand, so it rides the
        # Kept lane now while its stored verdict stays pure "condemn" underneath. The reaped season
        # stays here, its hand reap being effective.
        assert len(rows) == 2
        # The display fields ride along.
        alpha = next(r for r in rows if r["title"] == "Example Alpha")
        assert alpha["year"] == 1979
        # The poster is served from Plex through our proxy, keyed by the rating key.
        assert alpha["poster_url"] == "/api/poster/555"
        assert alpha["requested_by"] == "Alice"

    def test_search_matches_title(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&search=alpha").json()
        assert _titles(rows) == {"Example Alpha"}

    def test_search_understands_a_year_typed_after_the_title(self, client: TestClient) -> None:
        # The queue prints the year in its own span beside the title, so the operator reads
        # "Example Alpha 1979" as one string and types it back. It used to match nothing: the
        # year lives in its own column and was never in `title`.
        for term in ("Example Alpha 1979", "Example Alpha (1979)", "alpha 1979"):
            rows = client.get(f"/api/candidates?verdict=condemn&search={term}").json()
            assert _titles(rows) == {"Example Alpha"}, term

    def test_search_with_the_wrong_year_finds_nothing(self, client: TestClient) -> None:
        # The year narrows; it is not decoration. Example Alpha is 1979.
        rows = client.get("/api/candidates?verdict=condemn&search=Example Alpha 1980").json()
        assert rows == []

    def test_search_also_matches_the_show_name(self, client: TestClient) -> None:
        # "mid" matches the show name, carried on the season row's group_title.
        rows = client.get("/api/candidates?verdict=condemn&search=mid").json()
        assert _titles(rows) == {"Example Mid · Season 5"}

    def test_media_type_filter(self, client: TestClient) -> None:
        movies = client.get("/api/candidates?verdict=condemn&media_type=movie").json()
        # Example Zulu is a movie too, but spared by hand, so it rides the Kept lane now.
        assert _titles(movies) == {"Example Alpha"}
        kept_movies = client.get("/api/candidates?verdict=protect&media_type=movie").json()
        assert _titles(kept_movies) == {"Example Zulu"}
        seasons = client.get("/api/candidates?verdict=condemn&media_type=season").json()
        assert _titles(seasons) == {"Example Mid · Season 5"}

    def test_requested_yes_keeps_just_requested_media(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&requested=yes").json()
        assert _titles(rows) == {
            "Example Alpha",
            "Example Mid · Season 5",
        }  # Example Zulu had no requester

    def test_requested_no_keeps_just_the_unrequested(self, client: TestClient) -> None:
        # The only unrequested title, Example Zulu, is spared by hand, so it rides the Kept lane.
        rows = client.get("/api/candidates?verdict=protect&requested=no").json()
        assert _titles(rows) == {"Example Zulu"}  # the only one nobody asked for

    def test_filters_stack(self, client: TestClient) -> None:
        # media_type AND requested are ANDed, not either-or.
        rows = client.get("/api/candidates?verdict=condemn&media_type=movie&requested=yes").json()
        assert _titles(rows) == {"Example Alpha"}  # a movie AND requested; the season is excluded


class TestGenreFilter:
    def test_a_genre_matches_the_whole_term_only(self, client: TestClient) -> None:
        # "Comedy" must match ["Comedy", ...] and NOT ["Comedy Special"].
        rows = client.get("/api/candidates?verdict=condemn&genre=Comedy").json()
        assert _titles(rows) == {"Example Alpha"}

    def test_a_malformed_genre_row_is_skipped_not_an_error(self, client: TestClient) -> None:
        # The season row's genres_json does not parse; it never matches and never 500s.
        response = client.get("/api/candidates?verdict=condemn&genre=Horror")
        assert response.status_code == 200
        assert _titles(response.json()) == {"Example Alpha"}

    def test_an_unseen_genre_matches_nothing(self, client: TestClient) -> None:
        response = client.get("/api/candidates?verdict=condemn&genre=Western")
        assert response.json() == []
        assert response.headers["X-Total-Count"] == "0"


class TestLibraryFilter:
    def test_the_library_rides_along_on_every_row(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn").json()
        by_title = {str(r["title"]): r["library"] for r in rows}
        assert by_title["Example Alpha"] == "Movies"
        assert by_title["Example Mid · Season 5"] == "TV Shows"

    def test_library_keeps_only_that_section(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&library=Movies").json()
        assert _titles(rows) == {"Example Alpha"}

    def test_library_matches_the_whole_name_not_a_substring(self, client: TestClient) -> None:
        # A filter for "Movies" must not drag in the "4K Movies" library. Example Zulu (the
        # "4K Movies" title) is spared by hand, so query the Kept lane it now rides.
        rows = client.get("/api/candidates", params={"verdict": "protect", "library": "4K Movies"})
        assert _titles(rows.json()) == {"Example Zulu"}

    def test_an_unseen_library_matches_nothing(self, client: TestClient) -> None:
        response = client.get("/api/candidates?verdict=condemn&library=Anime")
        assert response.json() == []
        assert response.headers["X-Total-Count"] == "0"

    def test_it_stacks_with_media_type(self, client: TestClient) -> None:
        rows = client.get(
            "/api/candidates",
            params={"verdict": "condemn", "library": "TV Shows", "media_type": "season"},
        )
        assert _titles(rows.json()) == {"Example Mid · Season 5"}


class TestOverrideFilter:
    def test_spared_by_hand(self, client: TestClient) -> None:
        # A hand spare moves the item onto the Kept lane (its stored verdict stays pure policy);
        # the spare filter finds it there, not on the Condemned lane it left.
        response = client.get("/api/candidates?verdict=protect&override=spare")
        assert _titles(response.json()) == {"Example Zulu"}
        # The totals describe the filtered set, exactly what the page is drawn from.
        assert response.headers["X-Total-Count"] == "1"

    def test_a_show_level_reap_covers_its_season(self, client: TestClient) -> None:
        # The override sits on the SHOW key (sonarr:1:5); the season row inherits it.
        rows = client.get("/api/candidates?verdict=condemn&override=reap").json()
        assert _titles(rows) == {"Example Mid · Season 5"}

    def test_untouched_items_only(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&override=none").json()
        assert _titles(rows) == {"Example Alpha"}

    def test_it_stacks_with_the_other_filters(self, client: TestClient) -> None:
        # A spare override AND a requester: Zulu is spared but was never requested.
        rows = client.get("/api/candidates?verdict=condemn&override=spare&requested=yes").json()
        assert rows == []

    def test_a_season_spared_on_its_own_key_beats_its_shows_reap(self, client: TestClient) -> None:
        """The precedence still lives in one function, not in SQL.

        The filter now narrows in SQL to the rows that could possibly carry a decision and
        asks the real ``effective_override`` about each, rather than resolving every row in
        the lane in Python. This is the case that would break if the narrowing ever tried
        to decide as well: the season's own key must win over its show's.
        """
        assert (
            client.post(
                "/api/whitelist",
                json={"media_key": "sonarr:1:5:5", "title": "Season 5", "decision": "spare"},
            ).status_code
            < 300
        )
        spared = client.get("/api/candidates?verdict=protect&override=spare").json()
        assert "Example Mid · Season 5" in _titles(spared)
        # And it is no longer counted under its show's reap.
        reaped = client.get("/api/candidates?verdict=condemn&override=reap").json()
        assert _titles(reaped) == set()

    def test_a_row_reports_both_the_spare_it_toggles_and_the_one_that_covers_it(
        self, client: TestClient
    ) -> None:
        """Two spares, two questions, two fields -- and the wire must carry both.

        The season is spared for a day and its show forever. ``spare_expires_at`` is the spare
        in force by precedence, which is what the row's Spare control toggles and clears (rule
        50), so it must stay the season's own date. ``spare_covers_until`` is when the file
        stops being kept, which is what every color and every sentence about its fate reads
        (rules 49/61), and the show's forever spare outlasts the season's day: it must be null.

        Reading one field for both jobs is what drew "expired" over a file the show keeps and
        promised a re-judgment that changes nothing. Pinned at the route because the derivation
        being right (``test_whitelist``) does not mean it is plumbed.
        """
        for key, title, days in (
            ("sonarr:1:5", "Example Mid", 0),  # the whole show, forever
            ("sonarr:1:5:5", "Season 5", 1),  # the season, for a day
        ):
            assert (
                client.post(
                    "/api/whitelist",
                    json={
                        "media_key": key,
                        "title": title,
                        "decision": "spare",
                        "spare_days": days,
                    },
                ).status_code
                < 300
            )

        rows = client.get("/api/candidates?verdict=protect&override=spare").json()
        season = next(r for r in rows if r["media_key"] == "sonarr:1:5:5")
        assert season["spare_expires_at"] is not None, "the control still toggles the season's own"
        assert season["spare_covers_until"] is None, "the show's forever spare outlasts it"

    def test_a_season_covers_itself_when_no_show_spare_outlasts_it(
        self, client: TestClient
    ) -> None:
        """The other side of the same branch, and the one that keeps the fix honest.

        The show above this season is set to REAP, so it contributes no cover: when the
        season's own spare runs out the file really is handed back. Both fields must answer
        with the season's own date, or every expired season spare would read as covered.
        """
        assert (
            client.post(
                "/api/whitelist",
                json={
                    "media_key": "sonarr:1:5:5",
                    "title": "Season 5",
                    "decision": "spare",
                    "spare_days": 1,
                },
            ).status_code
            < 300
        )
        rows = client.get("/api/candidates?verdict=protect&override=spare").json()
        season = next(r for r in rows if r["media_key"] == "sonarr:1:5:5")
        assert season["spare_covers_until"] == season["spare_expires_at"] is not None

    def test_untouched_means_neither_its_own_key_nor_its_shows(self, client: TestClient) -> None:
        """``override=none`` is the anti-set of every effective decision.

        It used to be built by pulling every media_key in the lane into Python and binding
        them all back into one IN, which on a large library was nearly every row and could
        run past SQLite's bound-variable ceiling into a 500. It is derived from the
        operator's decisions now, so what must not change is which rows it names.
        """
        rows = client.get("/api/candidates?verdict=condemn&override=none").json()
        # Alpha has no decision at any level. The season is covered by its SHOW's reap and
        # Zulu by its own spare, so neither is untouched.
        assert _titles(rows) == {"Example Alpha"}


class TestTheShowKeyInvariantTheFilterRelieson:
    """``Candidate.group_key`` and ``whitelist.show_key`` must name the same thing.

    The override filter narrows in SQL with ``group_key IN (decided keys)`` before asking
    the real decision function about each row. That narrowing is only a superset if a
    season's stored ``group_key`` is exactly the key ``show_key`` derives from its
    media_key -- if the two ever drifted, a season kept by a whole-show spare would fall
    out of the narrowed set and be reported as untouched. The season scan builds
    ``group_key`` as ``sonarr:{instance}:{series}``; this pins that they agree.
    """

    def test_a_seasons_group_key_is_its_show_key(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=protect&media_type=season").json()
        rows += client.get("/api/candidates?verdict=condemn&media_type=season").json()
        assert rows, "the fixture must hold at least one season for this to mean anything"
        for row in rows:
            assert row["group_key"] == show_key(str(row["media_key"]))

    def test_a_movie_has_neither(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&media_type=movie").json()
        assert rows
        for row in rows:
            assert row["group_key"] is None
            assert show_key(str(row["media_key"])) is None


class TestSort:
    def test_by_title_ascending_uses_the_show_name(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&sort=title&order=asc").json()
        # The season sorts under its show name ("Example Mid"), not its own full season title.
        # Example Zulu is spared by hand, so it rides the Kept lane and is not among these.
        assert [r["title"] for r in rows] == [
            "Example Alpha",
            "Example Mid · Season 5",
        ]

    def test_by_year_descending_is_newest_first(self, client: TestClient) -> None:
        rows = client.get("/api/candidates?verdict=condemn&sort=year&order=desc").json()
        # Newest first; the season carries no year and sorts last. Example Zulu (1995) is spared by
        # hand and rides the Kept lane, so Example Alpha now leads the condemned lane.
        assert [r["title"] for r in rows] == ["Example Alpha", "Example Mid · Season 5"]


class TestYearInSearch:
    """A release year typed after a title narrows the search instead of matching nothing.

    The queue prints the year in its own span beside the title, and Scales prints it the same
    way, so the operator reads "Example Delta 2049" as one string and types it back. The year
    lives in its own column and was never inside ``title``, so every such search used to come
    back empty.

    Two rows the fixture below is built to tell apart, because a number on the end of a title
    has two readings and the predicate has to try both:

    * **Example Delta 2049**, released 2017 -- the number is part of the *name*. Only the
      whole-string arm finds it; the stem-plus-year arm asks for year 2049 and it is 2017.
    * **Example Echo**, released 2049 -- the number is the *year*. Only the stem-plus-year arm
      finds it; the whole string is not in its title.

    Either arm alone leaves one of them unfindable by the exact string the UI prints.
    """

    @pytest.fixture
    def client(self, tmp_path: Path) -> Iterator[TestClient]:
        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        now = utcnow()
        with Session(engine) as session:
            snap = Snapshot(
                created_at=now, policy_hash="b" * 64, horizon_at=now, item_count=2, degraded=False
            )
            session.add(snap)
            session.flush()
            session.add_all(
                [
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:20",
                        title="Example Delta 2049",
                        year=2017,
                    ),
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:21",
                        title="Example Echo",
                        year=2049,
                    ),
                ]
            )
            session.commit()
        engine.dispose()
        with TestClient(create_app(settings)) as c:
            login(c, settings)
            yield c

    def _found(self, client: TestClient, term: str) -> set[str]:
        return _titles(client.get("/api/candidates", params={"search": term}).json())

    def test_a_title_ending_in_a_year_it_predates_is_findable_by_its_whole_name(
        self, client: TestClient
    ) -> None:
        assert self._found(client, "Example Delta 2049") == {"Example Delta 2049"}

    def test_a_year_typed_after_a_title_narrows_to_that_year(self, client: TestClient) -> None:
        assert self._found(client, "Example Echo 2049") == {"Example Echo"}
        # Parentheses and a bare space are the two ways the year is printed across the app.
        assert self._found(client, "Example Echo (2049)") == {"Example Echo"}
        # And it narrows: the same title with the wrong year is not a near miss, it is a miss.
        assert self._found(client, "Example Echo 2017") == set()

    def test_the_year_can_come_from_either_column(self, client: TestClient) -> None:
        # "Example Delta" is 2017, so the stem-plus-year arm finds it by its real year even
        # though its title ends in a different one.
        assert self._found(client, "Example Delta 2017") == {"Example Delta 2049"}

    def test_a_year_alone_asks_for_that_year_and_still_reads_as_text(
        self, client: TestClient
    ) -> None:
        # Both readings again: Echo came out in 2049 and only the year arm finds it; Delta has
        # 2049 in its NAME and was released in 2017, so only the text arm does. Answering with
        # one half read as a broken search when it matched titles only, and would lose a title
        # named after a year if it matched the column only.
        assert self._found(client, "2049") == {"Example Delta 2049", "Example Echo"}

    def test_a_year_alone_narrows_to_that_year(self, client: TestClient) -> None:
        # Not "every row": 2017 is Delta's release year and nothing else here, so Echo stays out.
        assert self._found(client, "2017") == {"Example Delta 2049"}


class TestSearchIsLiteralText:
    """What the operator types means itself, not a SQL pattern.

    ``%`` and ``_`` are the two characters ``LIKE`` reserves, and the term went into the
    pattern unescaped, so both were live wildcards: ``a_pha`` matched "Example Alpha". It only
    ever over-matched -- a title genuinely containing ``%`` still found itself, because the
    wildcard matches the empty string -- which is why it went unnoticed. Issue #303.

    The fixture holds the pairs that tell a literal from a wildcard: two titles one underscore
    apart, and one containing a real ``%`` and a real backslash.
    """

    @pytest.fixture
    def client(self, tmp_path: Path) -> Iterator[TestClient]:
        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        now = utcnow()
        with Session(engine) as session:
            snap = Snapshot(
                created_at=now, policy_hash="c" * 64, horizon_at=now, item_count=3, degraded=False
            )
            session.add(snap)
            session.flush()
            session.add_all(
                [
                    _candidate(snapshot_id=snap.id, media_key="radarr:1:30", title="Example Alpha"),
                    _candidate(
                        snapshot_id=snap.id, media_key="radarr:1:31", title="Example Alpaca"
                    ),
                    _candidate(
                        snapshot_id=snap.id,
                        media_key="radarr:1:32",
                        title="Example 100% Complete",
                    ),
                    _candidate(
                        snapshot_id=snap.id, media_key="radarr:1:33", title="Example A\\B Sequel"
                    ),
                ]
            )
            session.commit()
        engine.dispose()
        with TestClient(create_app(settings)) as c:
            login(c, settings)
            yield c

    def _found(self, client: TestClient, term: str) -> set[str]:
        return _titles(client.get("/api/candidates", params={"search": term}).json())

    def test_an_underscore_matches_only_an_underscore(self, client: TestClient) -> None:
        # As a wildcard this found "Example Alpha". As a literal it finds nothing, because no
        # title here contains one.
        assert self._found(client, "a_pha") == set()
        # The control: the same search with the real character finds the row.
        assert self._found(client, "alpha") == {"Example Alpha"}

    def test_a_percent_matches_only_a_percent(self, client: TestClient) -> None:
        # As a wildcard this spanned the gap and matched "Example 100% Complete".
        assert self._found(client, "Example%Complete") == set()

    def test_a_title_containing_a_percent_is_found_by_typing_it(self, client: TestClient) -> None:
        # The direction the old behavior got right by accident, and the one an escape can
        # break: this also pins that the backslash is escaped BEFORE the `%`. Escaping them
        # the other way round turns the escape into a literal backslash plus a live wildcard,
        # and this row stops matching its own name.
        assert self._found(client, "100% Complete") == {"Example 100% Complete"}

    def test_a_backslash_means_a_backslash(self, client: TestClient) -> None:
        # The escape character itself has to survive being searched for.
        assert self._found(client, "A\\B") == {"Example A\\B Sequel"}

    def test_a_lone_wildcard_is_not_a_way_to_list_everything(self, client: TestClient) -> None:
        # A bare "%" used to be every row in the lane. It is now a search for one character,
        # and exactly one title here has it.
        assert self._found(client, "%") == {"Example 100% Complete"}
        assert self._found(client, "_") == set()


class TestSplitSearchYear:
    """The trailing-year split stays linear in the term, and keeps its readings.

    The separator run ahead of the year used to live in the pattern as ``[\\s,·-]*``, which
    backtracks quadratically when the term is mostly whitespace -- a string an operator can
    paste into search (CodeQL alert 11). The run is stripped in code now, and the flood case
    pins the complexity: quadratic needs minutes on that input where linear needs
    microseconds, so the bound sits a thousandfold above one and far under the other.
    """

    def test_the_readings_survive_the_rewrite(self) -> None:
        # The spellings the app itself prints beside a title, and the bare-adjacent form.
        assert _split_search_year("Example Delta 2049") == ("Example Delta", 2049)
        assert _split_search_year("Example Delta (2049)") == ("Example Delta", 2049)
        assert _split_search_year("Example Delta, 2049") == ("Example Delta", 2049)
        assert _split_search_year("2049") == ("", 2049)
        assert _split_search_year("(2049)") == ("", 2049)
        assert _split_search_year("Example Delta") == ("Example Delta", None)

    def test_a_whitespace_flood_returns_at_once(self) -> None:
        flood = "\t" * 100_000
        started = time.perf_counter()
        assert _split_search_year(flood) == (flood, None)
        assert time.perf_counter() - started < 1.0
