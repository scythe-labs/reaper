# SPDX-License-Identifier: AGPL-3.0-or-later
"""The TV/season scan path.

Season pruning deletes whole seasons, so this half of the scan is held to the same
standard as the movie half and one extra: because a season must be resolved to its own
Plex rating key before its watch history can be read, every test here is really asking
whether an *uncertain* resolution can lead to a deletion. The answer, everywhere, must be
no -- a season we cannot see is judged, at worst, ABSTAIN, never CONDEMN.

The load-bearing test is ``TestNothingUnseenIsCondemned``: it runs an unresolved season
through the real default policy and asserts the verdict cannot be "condemn".
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clients.sonarr_stats import SeasonStats
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.session import create_cache_engine
from reaper.engine import identity
from reaper.engine.gates import ABSTAIN, PROTECT, Evaluation, GateId, evaluate_all
from reaper.engine.observation import Absent, Known, Unknown
from reaper.engine.policy import DEFAULT_MOVIE_POLICY
from reaper.engine.signals import SignalConfig
from reaper.engine.signals import score as score_signals
from reaper.services import history_sync, season_scan
from reaper.services.scan_runner import build_gates
from reaper.services.season_pruning import plan_series_prune
from reaper.services.snapshot import _verdict

GB = 1024**3


def _season(
    n: int,
    *,
    files: int = 5,
    size: int = GB,
    total: int = 10,
    wanted: int = 0,
    monitored: bool = False,
) -> SeasonStats:
    return SeasonStats(
        season_number=n,
        monitored=monitored,
        episode_file_count=files,
        size_on_disk=size,
        total_episode_count=total,
        wanted_episode_count=wanted,
    )


def _season_payload(n: int, *, files: int = 5, size: int = GB, wanted: int = 0) -> dict[str, Any]:
    """A Sonarr ``seasons[]`` entry, as the API returns it."""
    return {
        "seasonNumber": n,
        "monitored": False,
        "statistics": {
            "episodeFileCount": files,
            "sizeOnDisk": size,
            "totalEpisodeCount": 10,
            "episodeCount": wanted,
        },
    }


# ---------------------------------------------------------------------------
# Keys and titles
# ---------------------------------------------------------------------------


class TestKeys:
    def test_a_season_key_is_four_parts(self) -> None:
        assert season_scan.season_media_key(1, 42, 3) == "sonarr:1:42:3"

    def test_the_title_names_the_season(self) -> None:
        assert season_scan.season_title("Example Show", 3) == "Example Show — Season 3"

    def test_specials_are_named_not_numbered(self) -> None:
        assert "Specials" in season_scan.season_title("Example Show", 0)


class TestParseSeasons:
    def test_an_entry_without_statistics_is_dropped_not_guessed(self) -> None:
        """A season Sonarr cannot describe is left out entirely rather than defaulted to
        zeros -- zeros would read as an empty season and quietly drop it from protection."""
        series = {"seasons": [_season_payload(1), {"seasonNumber": 2}]}
        seasons = season_scan.parse_seasons(series)
        assert [s.season_number for s in seasons] == [1]


# ---------------------------------------------------------------------------
# Airing detection -- conservative on purpose
# ---------------------------------------------------------------------------


class TestAiring:
    def test_a_continuing_series_protects_its_latest_season(self) -> None:
        seasons = [_season(1), _season(2), _season(3)]
        series = {"status": "continuing", "ended": False}
        assert season_scan.airing_seasons(series, seasons) == {3}

    def test_an_ended_series_marks_nothing_airing(self) -> None:
        seasons = [_season(1), _season(2)]
        series = {"status": "ended", "ended": True}
        assert season_scan.airing_seasons(series, seasons) == set()

    def test_specials_never_count_as_the_latest_airing_season(self) -> None:
        seasons = [_season(0), _season(1)]
        series = {"status": "continuing"}
        assert season_scan.airing_seasons(series, seasons) == {1}


# ---------------------------------------------------------------------------
# The show join -- the one place a wrong answer could delete the wrong thing
# ---------------------------------------------------------------------------


class TestTheShowJoin:
    """The Sonarr series -> Plex show join now runs through the one shared resolver
    (``identity.resolve_show``). These cases carry no external id, so they exercise the
    title+year backstop -- the exact behaviour the old ``match_show`` guaranteed, preserved
    now that there is a single implementation (see ``test_identity.py`` for the id tiers)."""

    def _index(self, *items: tuple[int, str, int | None]) -> identity.PlexIndex:
        return identity.PlexIndex.build(
            [
                identity.PlexItem(rating_key=rk, title=title, year=year, added_at=None)
                for rk, title, year in items
            ]
        )

    def _match(self, index: identity.PlexIndex, title: str, year: int | None) -> int | None:
        return identity.resolve_show(
            ids=identity.ExternalIds(), title=title, year=year, file_basename=None, index=index
        ).rating_key

    def test_a_unique_title_matches(self) -> None:
        assert self._match(self._index((900, "Example Show", 2010)), "Example Show", 2010) == 900

    def test_a_missing_title_is_no_match(self) -> None:
        assert self._match(self._index(), "Nowhere", None) is None

    def test_a_duplicate_title_is_disambiguated_by_year(self) -> None:
        index = self._index((1, "The Office", 2001), (2, "The Office", 2005))  # UK, US
        assert self._match(index, "The Office", 2005) == 2

    def test_a_duplicate_title_with_no_year_refuses_to_guess(self) -> None:
        """The wrong show join reads the wrong show's watch history and could condemn a
        season people are watching. With nothing to disambiguate on, refuse -> the season
        goes Unknown and abstains, rather than being matched to a coin-flip."""
        index = self._index((1, "The Office", 2001), (2, "The Office", 2005))
        assert self._match(index, "The Office", None) is None

    def test_a_lone_title_match_with_a_conflicting_year_is_refused(self) -> None:
        """The US series is scanned but the ONLY Plex show with that title is the UK one
        (the US show is indexed under a different title). A single title hit is not a safe
        join when the known years disagree -- binding would read the UK show's history."""
        index = self._index((1, "The Office", 2001))
        assert self._match(index, "The Office", 2005) is None

    def test_a_lone_title_match_with_an_agreeing_year_binds(self) -> None:
        assert self._match(self._index((1, "The Office", 2005)), "The Office", 2005) == 1

    def test_a_lone_title_match_binds_when_a_year_is_missing(self) -> None:
        """Plex often has no year; a title-only join stays as safe as the movie path's."""
        assert self._match(self._index((1, "The Office", None)), "The Office", 2005) == 1


class TestResolveSeasonKeys:
    class _FakeChildren:
        def __init__(self, children: list[dict[str, Any]]) -> None:
            self._children = children

        async def children_metadata(self, rating_key: int) -> list[dict[str, Any]]:
            return self._children

    async def test_it_maps_number_to_key_and_arrival(self) -> None:
        client = self._FakeChildren([{"media_index": 1, "rating_key": 101, "added_at": "1000000"}])
        result = await season_scan.resolve_season_keys(client, 900)  # type: ignore[arg-type]
        assert result[1].rating_key == 101
        assert result[1].added_at is not None

    async def test_a_duplicated_season_number_is_dropped_not_guessed(self) -> None:
        """Two 'Season 2' items (a split/mis-scanned library) is ambiguous. Binding to one
        risks reading an empty duplicate's history for a watched season, so the season is
        dropped entirely -> no Plex key -> Unknown facts -> abstain."""
        client = self._FakeChildren(
            [
                {"media_index": 1, "rating_key": 101},
                {"media_index": 2, "rating_key": 201},
                {"media_index": 2, "rating_key": 202},  # duplicate season number
            ]
        )
        result = await season_scan.resolve_season_keys(client, 900)  # type: ignore[arg-type]
        assert 1 in result
        assert 2 not in result  # dropped, not bound to 201 or 202


# ---------------------------------------------------------------------------
# The guard -> gate translation
# ---------------------------------------------------------------------------


class TestGuardResult:
    def test_a_protected_season_becomes_a_protecting_gate(self) -> None:
        plan = plan_series_prune(series_title="S", seasons=[_season(1), _season(2)], keep_last=1)
        # Season 1 is the first season -> protected.
        result = season_scan.guard_result(plan, 1)
        assert result.gate is GateId.SEASON_PROGRESSION
        assert result.outcome == PROTECT

    def test_a_keep_rule_conflict_blocks_rather_than_condemns(self) -> None:
        """The old season is the good one: prunable by rank, but far more watched than a
        season the rule keeps. That is not a delete to make unattended -- it blocks, which
        forces the whole item to ABSTAIN and sends it to a human."""
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=2,
            keep_first_season=False,
            watchers_by_season={1: 40, 2: 1, 3: 1, 4: 1},
        )
        result = season_scan.guard_result(plan, 1)
        assert result.outcome == ABSTAIN
        assert result.blocked is True

    def test_a_cleanly_prunable_season_neither_protects_nor_blocks(self) -> None:
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=1,
            keep_first_season=False,
        )
        result = season_scan.guard_result(plan, 2)
        assert result.outcome == ABSTAIN
        assert result.blocked is False


# ---------------------------------------------------------------------------
# Facts assembly, and the Unknown discipline
# ---------------------------------------------------------------------------


def _facts(**over: Any) -> Any:
    base: dict[str, Any] = {
        "title": "Show — Season 3",
        "season": _season(3, size=8 * GB),
        "rank": 2,
        "plex_rating_key": 700,
        "season_added_at": utcnow() - timedelta(days=4000),
        "horizon": utcnow() - timedelta(days=4000),
        "last_played": None,
        "watchers_window": 0,
        "watchers_all_time": 0,
        "active_rating_keys": set(),
        "activity_degraded": False,
        "whitelisted": False,
        "curated": [],
    }
    base.update(over)
    return season_scan.build_season_facts(**base)


class TestBuildSeasonFacts:
    def test_an_unresolved_season_has_unknown_watch_facts(self) -> None:
        """No Plex rating key means no history to read. Dormancy, popularity and streaming
        all go Unknown -- and Unknown, through the gates, protects."""
        facts = _facts(plex_rating_key=None)
        assert isinstance(facts.days_observed_unwatched, Unknown)
        assert isinstance(facts.distinct_watchers, Unknown)
        assert isinstance(facts.is_streaming_now, Unknown)

    def test_a_resolved_season_has_known_dormancy(self) -> None:
        facts = _facts(plex_rating_key=700)
        assert isinstance(facts.days_observed_unwatched, Known)

    def test_a_season_being_streamed_is_known_streaming(self) -> None:
        facts = _facts(plex_rating_key=700, active_rating_keys={700})
        assert isinstance(facts.is_streaming_now, Known)
        assert facts.is_streaming_now.value is True

    def test_streaming_is_unknown_when_activity_could_not_be_read(self) -> None:
        """Even a resolved season goes Unknown-streaming if we could not read sessions:
        not being able to look is never the same as nobody watching."""
        facts = _facts(plex_rating_key=700, activity_degraded=True)
        assert isinstance(facts.is_streaming_now, Unknown)

    def test_a_season_has_no_imdb_rating(self) -> None:
        """There is no free per-season IMDb rating; Sonarr's ratings are flat TVDB. The
        rating is Absent, which the scorer treats as fail-safe (drags the score down)."""
        facts = _facts()
        assert isinstance(facts.imdb_rating_tenths, Absent)

    def test_a_season_is_always_managed(self) -> None:
        facts = _facts()
        assert isinstance(facts.is_managed, Known) and facts.is_managed.value is True

    def test_size_comes_from_sonarr(self) -> None:
        facts = _facts(season=_season(3, size=8 * GB))
        assert isinstance(facts.size_bytes, Known) and facts.size_bytes.value == 8 * GB

    def test_dormancy_is_measured_from_the_seasons_own_arrival(self) -> None:
        """A season backfilled into an old show arrived recently. Dormancy must count from
        the season's OWN added date, not the show's -- using the show's would read a
        just-added season as decades dormant and condemn a file nobody could have watched."""
        facts = _facts(
            season_added_at=utcnow() - timedelta(days=5),  # files landed 5 days ago
            horizon=utcnow() - timedelta(days=4000),  # mature install
            last_played=None,
        )
        assert isinstance(facts.days_observed_unwatched, Known)
        assert facts.days_observed_unwatched.value < 30

    def test_a_resolved_season_with_no_arrival_date_is_unknown_dormancy(self) -> None:
        """A season whose own arrival date could not be read has its dormancy Unknown --
        never a Known dormancy fabricated from the horizon. Mirrors the movie path's
        added_at=None branch; Unknown then forces the dormancy gates to protect."""
        facts = _facts(season_added_at=None, last_played=None)
        assert isinstance(facts.days_observed_unwatched, Unknown)


# ---------------------------------------------------------------------------
# The load-bearing safety property
# ---------------------------------------------------------------------------


def _judge(facts: Any, guard: Any = None) -> str:
    """Run one season's facts through the real default policy and return its verdict."""
    gates = build_gates(DEFAULT_MOVIE_POLICY)
    results = list(evaluate_all(gates, facts).results)
    if guard is not None:
        results = [guard, *results]
    evaluation = Evaluation(results=results)
    signals = [
        SignalConfig(signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor)
        for s in DEFAULT_MOVIE_POLICY.signals
    ]
    sc = score_signals(signals, facts)
    return _verdict(evaluation, round(sc.value), round(sc.coverage * 10_000), DEFAULT_MOVIE_POLICY)


class TestNothingUnseenIsCondemned:
    def test_an_unresolved_season_cannot_be_condemned(self) -> None:
        facts = _facts(plex_rating_key=None)
        clean = season_scan.guard_result(
            plan_series_prune(
                series_title="S", seasons=[_season(3)], keep_last=0, keep_first_season=False
            ),
            3,
        )
        assert _judge(facts, clean) == "abstain"

    def test_a_seen_dormant_unwatched_season_can_be_condemned(self) -> None:
        """The other side of the guarantee: when the evidence is real and the guards allow
        it, a season IS condemnable -- otherwise the whole path would be inert."""
        facts = _facts(plex_rating_key=700, last_played=None, watchers_window=0)
        clean = season_scan.guard_result(
            plan_series_prune(
                series_title="S",
                seasons=[_season(n) for n in range(1, 6)],
                keep_last=2,
                keep_first_season=False,
            ),
            3,
        )
        assert _judge(facts, clean) == "condemn"

    def test_a_guard_protected_season_wins_over_any_score(self) -> None:
        facts = _facts(plex_rating_key=700, last_played=None, watchers_window=0)
        protect = season_scan.guard_result(
            plan_series_prune(series_title="S", seasons=[_season(3)], keep_last=1), 3
        )
        assert protect.outcome == PROTECT
        assert _judge(facts, protect) == "protect"

    def test_a_freshly_backfilled_season_of_an_old_show_is_not_condemned(self) -> None:
        """The critical regression. A mature install (horizon ~4y ago); an old show whose
        MIDDLE season the operator just backfilled (files landed 5 days ago, never played).
        keep-last/keep-first protect the newest and first seasons by NUMBER -- not this
        middle season -- so only the dormancy discipline stands between it and a wrongful
        condemn. It must read as freshly arrived, not decades dormant."""
        facts = _facts(
            plex_rating_key=700,
            season_added_at=utcnow() - timedelta(days=5),
            horizon=utcnow() - timedelta(days=4000),
            last_played=None,
            watchers_window=0,
        )
        clean = season_scan.guard_result(
            plan_series_prune(
                series_title="S",
                seasons=[_season(n) for n in range(1, 7)],
                keep_last=2,
                keep_first_season=True,
            ),
            3,  # a middle season -> not protected by keep-last or keep-first
        )
        assert clean.outcome == ABSTAIN and clean.blocked is False  # guard does not protect it
        assert _judge(facts, clean) != "condemn"

    def test_a_resolved_season_with_unknown_arrival_abstains(self) -> None:
        """A season resolved in Plex but whose arrival date could not be read must abstain,
        not be condemned off the horizon -- the exact fail-open the movie path guards."""
        facts = _facts(
            plex_rating_key=700,
            season_added_at=None,
            horizon=utcnow() - timedelta(days=4000),
            last_played=None,
            watchers_window=0,
        )
        clean = season_scan.guard_result(
            plan_series_prune(
                series_title="S",
                seasons=[_season(n) for n in range(1, 6)],
                keep_last=2,
                keep_first_season=False,
            ),
            3,
        )
        assert _judge(facts, clean) == "abstain"


# ---------------------------------------------------------------------------
# Per-season watch statistics, against a real mirror
# ---------------------------------------------------------------------------


@pytest.fixture
async def cache_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")
    engine = create_cache_engine(settings)
    await history_sync.ensure_schema(engine)
    yield engine
    await engine.dispose()


async def _episode(
    engine: AsyncEngine,
    *,
    season_key: int,
    user_id: int,
    show_key: int = 42,
    days_ago: int = 1,
) -> None:
    when = int((utcnow() - timedelta(days=days_ago)).timestamp())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO watch_event (rating_key, parent_rating_key, "
                "grandparent_rating_key, user_id, watched_at, watched_status, "
                "percent_complete, media_type) "
                "VALUES (:rk, :season, :show, :uid, :ts, 1, 100, 'episode')"
            ),
            {
                "rk": season_key * 1000 + user_id,
                "season": season_key,
                "show": show_key,
                "uid": user_id,
                "ts": when,
            },
        )


class TestSeasonWatchStats:
    async def test_plays_aggregate_by_season(self, cache_engine: AsyncEngine) -> None:
        await _episode(cache_engine, season_key=701, user_id=1)
        await _episode(cache_engine, season_key=701, user_id=2)
        await _episode(cache_engine, season_key=702, user_id=1)

        stats = await season_scan.season_watch_stats(cache_engine, {701, 702}, window_days=365)
        assert stats.watchers_all_time[701] == 2
        assert stats.watchers_all_time[702] == 1
        assert 701 in stats.last_played

    async def test_the_window_excludes_old_plays(self, cache_engine: AsyncEngine) -> None:
        await _episode(cache_engine, season_key=703, user_id=1, days_ago=2000)
        stats = await season_scan.season_watch_stats(cache_engine, {703}, window_days=365)
        assert stats.watchers_all_time[703] == 1
        assert stats.watchers_window.get(703, 0) == 0

    async def test_user_season_keys_are_collected(self, cache_engine: AsyncEngine) -> None:
        await _episode(cache_engine, season_key=704, user_id=1)
        await _episode(cache_engine, season_key=705, user_id=1)
        stats = await season_scan.season_watch_stats(cache_engine, {704, 705}, window_days=365)
        assert stats.user_season_keys[1] == {704, 705}


class TestWatchedMaxByUser:
    def test_a_users_highest_season_is_scoped_to_this_show(self) -> None:
        """A user's progress in another series must not leak in: only this show's season
        keys are consulted."""
        stats = season_scan.SeasonWatchStats(user_season_keys={7: {701, 702, 999}})
        # 701 -> season 2, 702 -> season 3; 999 belongs to another show and is ignored.
        this_show = {701: 2, 702: 3}
        assert season_scan._watched_max_by_user(stats, this_show) == {"7": 3}


# ---------------------------------------------------------------------------
# End to end, with fake clients
# ---------------------------------------------------------------------------


class _FakeSonarr:
    def __init__(self, series: list[dict[str, Any]]) -> None:
        self._series = series

    async def series(self) -> list[dict[str, Any]]:
        return self._series


class _FakeTautulli:
    def __init__(
        self,
        *,
        shows: list[dict[str, Any]],
        children: dict[int, list[dict[str, Any]]],
    ) -> None:
        self._shows = shows
        self._children = children

    async def libraries(self) -> list[dict[str, Any]]:
        return [{"section_id": 3, "section_type": "show"}]

    async def library_media_info(
        self, section_id: int, *, start: int = 0, length: int = 1000
    ) -> dict[str, Any]:
        return {"data": self._shows if start == 0 else []}

    async def children_metadata(self, rating_key: int) -> list[dict[str, Any]]:
        return self._children.get(rating_key, [])


def _degrade_sink() -> tuple[list[str], Any]:
    reasons: list[str] = []
    return reasons, reasons.append


def _source(client: Any) -> season_scan.SonarrSource:
    return season_scan.SonarrSource(client=client, instance_id=1, name="hd")


class _FakePlexGuids:
    """A stand-in for the plexapi GUID sweep: rating_key -> (ExternalIds, basename)."""

    def __init__(self, guids: dict[int, tuple[identity.ExternalIds, str | None]]) -> None:
        self._guids = guids

    async def library_guid_index(
        self, *, section_type: str
    ) -> dict[int, tuple[identity.ExternalIds, str | None]]:
        return self._guids


class TestGatherEndToEnd:
    async def test_a_matched_prunable_season_is_gathered_with_its_plex_key(
        self, cache_engine: AsyncEngine
    ) -> None:
        series = [
            {
                "id": 42,
                "title": "Long Show",
                "year": 2005,
                "status": "ended",
                "ended": True,
                "imdbId": "tt0001",
                "seasons": [_season_payload(n) for n in range(1, 6)],  # 1..5
            }
        ]
        tautulli = _FakeTautulli(
            shows=[{"rating_key": 900, "title": "Long Show", "year": 2005, "added_at": "1000000"}],
            children={900: [{"media_index": n, "rating_key": 900 + n} for n in range(1, 6)]},
        )
        _reasons, degrade = _degrade_sink()

        judgements = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(_FakeSonarr(series))],
            tautulli=tautulli,  # type: ignore[arg-type]
            horizon=utcnow() - timedelta(days=4000),
            active_rating_keys=set(),
            activity_degraded=False,
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
        )

        by_key = {j.media_key: j for j in judgements}
        # Season 3 is prunable (outside keep-last 2, not the first): resolved to its Plex key.
        assert "sonarr:1:42:3" in by_key
        assert by_key["sonarr:1:42:3"].plex_rating_key == 903
        # The card poster comes from the SHOW's key (900), never the season's -- a season
        # often has no poster of its own, so the season key would 404 to a placeholder.
        assert by_key["sonarr:1:42:3"].poster_rating_key == 900
        # The first and last-two seasons are protected, and emitted so the panel shows why.
        assert by_key["sonarr:1:42:1"].guard_result.outcome == PROTECT
        assert by_key["sonarr:1:42:5"].guard_result.outcome == PROTECT

    async def test_plex_supplies_the_rating_and_poster_when_sonarr_cannot(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Sonarr has no imdbId (common for reality/recent shows), but the show matches Plex
        by tvdb and Plex carries the imdb id. The rating comes through on the Plex id, and
        the card poster uses the show's key -- so neither the rating nor the poster is lost
        to a Sonarr/TVDB metadata gap."""
        async with cache_engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS imdb_rating "
                    "(tconst TEXT PRIMARY KEY, average_rating REAL, num_votes INTEGER)"
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS imdb_dataset_sync (id INTEGER PRIMARY KEY, "
                    "synced_at INTEGER NOT NULL, row_count INTEGER NOT NULL)"
                )
            )
            await conn.execute(
                text(
                    "INSERT OR REPLACE INTO imdb_dataset_sync (id, synced_at, row_count) "
                    "VALUES (1, :ts, :n)"
                ),
                {"ts": int(utcnow().timestamp()), "n": 1_000_000},
            )
            await conn.execute(
                text(
                    "INSERT OR REPLACE INTO imdb_rating (tconst, average_rating, num_votes) "
                    "VALUES ('tt7777', 7.1, 38)"
                )
            )
        series = [
            {
                "id": 55,
                "title": "Reality Show",
                "year": 2020,
                "status": "ended",
                "ended": True,
                "tvdbId": 4242,  # matches Plex by tvdb; NO imdbId from Sonarr
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]
        tautulli = _FakeTautulli(
            shows=[
                {"rating_key": 800, "title": "Reality Show", "year": 2020, "added_at": "1000000"}
            ],
            children={800: [{"media_index": n, "rating_key": 800 + n} for n in range(1, 6)]},
        )
        plex = _FakePlexGuids({800: (identity.ExternalIds.of(tvdb=4242, imdb="tt7777"), None)})
        _reasons, degrade = _degrade_sink()

        judgements = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(_FakeSonarr(series))],
            tautulli=tautulli,  # type: ignore[arg-type]
            plex=plex,  # type: ignore[arg-type]
            horizon=utcnow() - timedelta(days=4000),
            active_rating_keys=set(),
            activity_degraded=False,
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
        )

        pruned = next(j for j in judgements if j.media_key == "sonarr:1:55:3")
        # Rating resolved via the Plex-supplied imdb id, even though Sonarr had none.
        assert isinstance(pruned.facts.imdb_rating_tenths, Known)
        assert pruned.facts.imdb_rating_tenths.value == 71
        # Poster uses the show's key (800), not the season's (803).
        assert pruned.poster_rating_key == 800
        assert pruned.plex_rating_key == 803

    async def test_an_unmatched_series_yields_unresolved_seasons(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Plex has not matched the show. Its prunable seasons still appear -- so the owner
        learns Plex failed to match them -- but with no Plex key and Unknown facts, so they
        can only abstain."""
        series = [
            {
                "id": 7,
                "title": "Unmatched Show",
                "status": "ended",
                "ended": True,
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]
        tautulli = _FakeTautulli(shows=[], children={})
        _reasons, degrade = _degrade_sink()

        judgements = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(_FakeSonarr(series))],
            tautulli=tautulli,  # type: ignore[arg-type]
            horizon=utcnow() - timedelta(days=4000),
            active_rating_keys=set(),
            activity_degraded=False,
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
        )
        pruned = next(j for j in judgements if j.media_key == "sonarr:1:7:3")
        assert pruned.plex_rating_key is None
        assert isinstance(pruned.facts.days_observed_unwatched, Unknown)

    async def test_a_fully_protected_short_show_yields_nothing(
        self, cache_engine: AsyncEngine
    ) -> None:
        series = [
            {
                "id": 9,
                "title": "Two Seasons",
                "status": "ended",
                "ended": True,
                "seasons": [_season_payload(1), _season_payload(2)],
            }
        ]
        _reasons, degrade = _degrade_sink()
        judgements = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(_FakeSonarr(series))],
            tautulli=_FakeTautulli(shows=[], children={}),  # type: ignore[arg-type]
            horizon=utcnow(),
            active_rating_keys=set(),
            activity_degraded=False,
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
        )
        assert judgements == []

    async def test_an_unreachable_sonarr_degrades_the_snapshot(
        self, cache_engine: AsyncEngine
    ) -> None:
        from reaper.clients.base import IntegrationError

        class _DeadSonarr:
            async def series(self) -> list[dict[str, Any]]:
                raise IntegrationError("sonarr", "connection refused")

        reasons, degrade = _degrade_sink()
        judgements = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(_DeadSonarr())],
            tautulli=_FakeTautulli(shows=[], children={}),  # type: ignore[arg-type]
            horizon=utcnow(),
            active_rating_keys=set(),
            activity_degraded=False,
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
        )
        assert judgements == []
        assert any("sonarr" in r and "unreachable" in r for r in reasons)
