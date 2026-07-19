# SPDX-License-Identifier: AGPL-3.0-or-later
"""The whole ``scan()`` pipeline, end to end with fakes.

The gather fans out across services concurrently -- the movie index, each Radarr's
movie list, and the TV season gather all overlap -- so this file proves the pipeline
still produces the snapshot a sequential gather would have:

* every source lands in the freeze (movies from Radarr, seasons from Sonarr);
* verdicts come out whole -- a dormant movie condemns, a whitelisted one protects with
  the protection named in its why-panel, an unmatched one abstains;
* the grace clock is recorded for exactly the condemned keys (the batched path);
* one unreachable Radarr degrades the snapshot loudly while the others' items survive.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from reaper.clients.base import IntegrationError
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import FirstFlagged
from reaper.db.session import create_cache_engine, create_engine, create_session_factory
from reaper.engine.observation import Known
from reaper.engine.policy import DEFAULT_MOVIE_POLICY, DEFAULT_TV_POLICY
from reaper.services import app_settings, history_sync, lists, profiles, season_scan
from reaper.services.scan_runner import _allowed_sections, build_gates
from reaper.services.snapshot import Progress, RadarrSource, _release_age_days, candidates, scan

NOW = utcnow().replace(microsecond=0)
LONG_AGO = NOW - timedelta(days=2000)


# ---------------------------------------------------------------------------
# Fakes -- just the surface the scan touches, all stateless and concurrency-safe.
# ---------------------------------------------------------------------------


class _FakeTautulli:
    def __init__(
        self,
        *,
        movies: list[dict[str, Any]],
        shows: list[dict[str, Any]] | None = None,
        children: dict[int, list[dict[str, Any]]] | None = None,
        sessions: list[dict[str, Any]] | None = None,
    ) -> None:
        self._movies = movies
        self._shows = shows or []
        self._children = children or {}
        self._sessions = sessions or []

    async def activity(self) -> dict[str, Any]:
        return {"sessions": self._sessions}

    async def libraries(self) -> list[dict[str, Any]]:
        return [
            {"section_id": 1, "section_type": "movie"},
            {"section_id": 3, "section_type": "show"},
        ]

    async def library_media_info(
        self, section_id: int, *, start: int = 0, length: int = 1000
    ) -> dict[str, Any]:
        rows = self._movies if section_id == 1 else self._shows
        return {"data": rows if start == 0 else []}

    async def children_metadata(self, rating_key: int) -> list[dict[str, Any]]:
        return self._children.get(rating_key, [])


class _FakeRadarr:
    def __init__(self, movies: list[dict[str, Any]]) -> None:
        self._movies = movies

    async def movies(self) -> list[dict[str, Any]]:
        return self._movies

    async def root_folders(self) -> list[dict[str, Any]]:
        return [{"path": "/data/movies", "accessible": True}]


class _BrokenRadarr:
    async def movies(self) -> list[dict[str, Any]]:
        raise IntegrationError("radarr", "unreachable (boom)")

    async def root_folders(self) -> list[dict[str, Any]]:
        raise IntegrationError("radarr", "unreachable (boom)")


class _FakeSonarr:
    def __init__(self, series: list[dict[str, Any]]) -> None:
        self._series = series

    async def series(self) -> list[dict[str, Any]]:
        return self._series

    async def root_folders(self) -> list[dict[str, Any]]:
        return [{"path": "/data/tv", "accessible": True}]

    async def episodes(self, series_id: int) -> list[dict[str, Any]]:
        return [
            {"seasonNumber": season, "episodeNumber": ep, "hasFile": True}
            for season in range(1, 6)
            for ep in range(1, 6)
        ]


class _StaticList:
    """A protection-list provider with fixed members, for seeding the whitelist."""

    slug = "keep-list"
    display_name = "Keep list"

    def __init__(self, items: list[lists.ListItem]) -> None:
        self._items = items

    async def fetch(self) -> list[lists.ListItem]:
        return self._items


# ---------------------------------------------------------------------------
# Fixtures and seed helpers
# ---------------------------------------------------------------------------


class TestLibraryScope:
    """Which Plex libraries the scan enriches against. Scoping only ever removes a
    library's enrichment, so an absent or unreadable selection must widen to all."""

    async def test_no_configured_libraries_scans_everything(self, session: AsyncSession) -> None:
        assert await _allowed_sections(session) is None

    async def test_the_enabled_subset_scopes_the_scan(self, session: AsyncSession) -> None:
        await app_settings.set_plex_libraries(
            session,
            [
                {"key": 1, "title": "Movies", "kind": "movie", "enabled": True},
                {"key": 2, "title": "4K Movies", "kind": "movie", "enabled": False},
                {"key": 3, "title": "TV", "kind": "show", "enabled": True},
            ],
        )
        assert await _allowed_sections(session) == {1, 3}

    async def test_an_unreadable_setting_falls_back_to_all(self, tmp_path: Path) -> None:
        """A settings-read failure must not abort a scan: it defaults to scanning every
        library (full coverage), never to scanning none."""
        settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
        engine = create_engine(settings)  # no create_all: the app_setting table is missing
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        async with factory() as s:
            assert await _allowed_sections(s) is None
        await engine.dispose()


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
async def cache_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_cache_engine(Settings(data_dir=tmp_path, secret_key="k"))  # type: ignore[call-arg]
    await history_sync.ensure_schema(eng)
    yield eng
    await eng.dispose()


async def _seed_sync(engine: AsyncEngine, *, ago: timedelta) -> None:
    """Record when the watch-history ingest last ran, the way a real sync does.

    This clock, not the newest play, is what the scan checks for a stalled ingest
    (``snapshot.MIRROR_STALE_AFTER``); with no row at all the scan has no evidence the
    ingest ever ran and degrades.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR REPLACE INTO history_sync_state (id, tautulli_total, synced_at) "
                "VALUES (1, 1, :ts)"
            ),
            {"ts": int((NOW - ago).timestamp())},
        )


async def _seed_play(
    engine: AsyncEngine,
    *,
    row_id: int,
    rating_key: int,
    synced_ago: timedelta = timedelta(hours=1),
    recent_play: bool = False,
) -> None:
    """One long-ago play: it anchors the data horizon ~2000 days back.

    Plus a successful sync ``synced_ago`` back, because a scan whose ingest has not run
    recently degrades and can judge nothing. Pass ``recent_play=True`` to add a fresh play
    on an unrelated item; it uses a rating key no candidate here carries, so no item's
    watcher counts move.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO watch_event (row_id, rating_key, user_id, watched_at, "
                " watched_status, percent_complete, media_type) "
                "VALUES (:row_id, :rating_key, 1, :watched_at, 1, 100, 'movie')"
            ),
            {
                "row_id": row_id,
                "rating_key": rating_key,
                "watched_at": int(LONG_AGO.timestamp()),
            },
        )
        if recent_play:
            await conn.execute(
                text(
                    "INSERT INTO watch_event (row_id, rating_key, user_id, watched_at, "
                    " watched_status, percent_complete, media_type) "
                    "VALUES (:row_id, 8675309, 1, :watched_at, 1, 100, 'movie')"
                ),
                {
                    "row_id": row_id + 1_000_000,
                    "watched_at": int((NOW - timedelta(hours=1)).timestamp()),
                },
            )
    await _seed_sync(engine, ago=synced_ago)


async def _seed_imdb(engine: AsyncEngine, ratings: dict[str, tuple[float, int]]) -> None:
    """A fresh IMDb dataset, so rating protections evaluate instead of degrading."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS imdb_rating ("
                " tconst TEXT PRIMARY KEY, average_rating REAL NOT NULL, "
                " num_votes INTEGER NOT NULL)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS imdb_dataset_sync ("
                " id INTEGER PRIMARY KEY CHECK (id = 1), synced_at INTEGER NOT NULL, "
                " row_count INTEGER NOT NULL)"
            )
        )
        for tconst, (rating, votes) in ratings.items():
            await conn.execute(
                text(
                    "INSERT OR REPLACE INTO imdb_rating (tconst, average_rating, num_votes) "
                    "VALUES (:tconst, :rating, :votes)"
                ),
                {"tconst": tconst, "rating": rating, "votes": votes},
            )
        await conn.execute(
            text(
                "INSERT OR REPLACE INTO imdb_dataset_sync (id, synced_at, row_count) "
                "VALUES (1, :now, :count)"
            ),
            {"now": int(NOW.timestamp()), "count": max(1, len(ratings))},
        )


def _movie_payloads() -> list[dict[str, Any]]:
    return [
        # Dormant for ~2000 days, poorly rated, nobody watching: condemned.
        {
            "id": 1,
            "title": "Dust",
            "year": 1990,
            "tmdbId": 101,
            "imdbId": "tt0000001",
            "hasFile": True,
            "sizeOnDisk": 5_000_000_000,
        },
        # Exactly as dormant, but on the owner's keep list: protected, and the
        # why-panel must say so.
        {
            "id": 2,
            "title": "Beloved",
            "year": 1988,
            "tmdbId": 102,
            "imdbId": "tt0000002",
            "hasFile": True,
            "sizeOnDisk": 4_000_000_000,
        },
        # Nowhere in Plex and carrying no ids: unmatched, so it must abstain.
        {
            "id": 3,
            "title": "Ghost Reel",
            "year": 2001,
            "hasFile": True,
            "sizeOnDisk": 1_000_000_000,
        },
    ]


def _movie_spine() -> list[dict[str, Any]]:
    return [
        {"rating_key": 11, "title": "Dust", "year": 1990, "added_at": "1000000"},
        {"rating_key": 22, "title": "Beloved", "year": 1988, "added_at": "1000000"},
    ]


def _season_payload(n: int) -> dict[str, Any]:
    return {
        "seasonNumber": n,
        "monitored": False,
        "statistics": {
            "episodeFileCount": 5,
            "sizeOnDisk": 1_000_000_000,
            "totalEpisodeCount": 10,
            "episodeCount": 0,
        },
    }


def _series_payloads() -> list[dict[str, Any]]:
    return [
        {
            "id": 42,
            "title": "Long Show",
            "year": 2005,
            "status": "ended",
            "ended": True,
            "imdbId": "tt0000042",
            "seasons": [_season_payload(n) for n in range(1, 6)],
        }
    ]


def _show_spine() -> list[dict[str, Any]]:
    return [{"rating_key": 900, "title": "Long Show", "year": 2005, "added_at": "1000000"}]


def _show_children() -> dict[int, list[dict[str, Any]]]:
    return {
        900: [{"media_index": n, "rating_key": 900 + n, "added_at": "1000000"} for n in range(1, 6)]
    }


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


class TestScanPipelineEndToEnd:
    async def test_a_full_scan_freezes_judges_and_records_grace(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        await _seed_play(cache_engine, row_id=1, rating_key=99)
        await _seed_imdb(cache_engine, {"tt0000001": (5.0, 5000), "tt0000042": (5.0, 5000)})
        await lists.sync(
            cache_engine,
            _StaticList([lists.ListItem(media_type="movie", imdb_id="tt0000002", title="B")]),
            mode=lists.ListMode.HARD,
            kind=lists.ListKind.WHITELIST,
        )

        tautulli = _FakeTautulli(
            movies=_movie_spine(), shows=_show_spine(), children=_show_children()
        )
        seen: list[Progress] = []

        snapshot = await scan(
            cache_engine,
            session,
            radarrs=[
                RadarrSource(client=_FakeRadarr(_movie_payloads()), instance_id=1, name="hd")  # type: ignore[arg-type]
            ],
            sonarrs=[
                season_scan.SonarrSource(
                    client=_FakeSonarr(_series_payloads()), instance_id=1, name="tv"
                )
            ],
            tautulli=tautulli,  # type: ignore[arg-type]
            movie_policy=DEFAULT_MOVIE_POLICY,
            movie_gates=build_gates(DEFAULT_MOVIE_POLICY),
            tv_policy=DEFAULT_TV_POLICY,
            tv_gates=build_gates(DEFAULT_TV_POLICY),
            on_progress=seen.append,
        )
        await session.commit()

        # Nothing failed, so nothing may claim otherwise: a clean concurrent gather must
        # produce a clean snapshot. Three movies plus five content-bearing seasons.
        assert snapshot.degraded is False, snapshot.degraded_reason
        assert snapshot.item_count == 8

        rows = {c.media_key: c for c in await candidates(session, snapshot.id)}
        assert len(rows) == 8

        # The dormant movie condemns; the whitelisted one protects AND names the
        # protection; the unmatched one abstains.
        assert rows["radarr:1:1"].verdict == "condemn"
        assert rows["radarr:1:2"].verdict == "protect"
        assert "whitelisted" in (rows["radarr:1:2"].explanation_json or "")
        assert rows["radarr:1:3"].verdict == "abstain"

        # Seasons rode the same judge: the keep-last/keep-first guards protected
        # seasons 1, 4 and 5; the prunable middle seasons were scored (and, dormant
        # for ~2000 days with nobody watching, condemned).
        assert rows["sonarr:1:42:1"].verdict == "protect"
        assert rows["sonarr:1:42:4"].verdict == "protect"
        assert rows["sonarr:1:42:5"].verdict == "protect"
        assert rows["sonarr:1:42:2"].verdict == "condemn"
        assert rows["sonarr:1:42:3"].verdict == "condemn"
        assert rows["sonarr:1:42:3"].media_type == "season"

        # The batched grace pass recorded exactly the condemned keys.
        flagged = {
            f.media_key
            for f in (await session.execute(text("SELECT media_key FROM first_flagged"))).all()
        }
        assert flagged == {"radarr:1:1", "sonarr:1:42:2", "sonarr:1:42:3"}
        first = await session.get(FirstFlagged, "radarr:1:1")
        assert first is not None and first.first_flagged_at is not None

        # Progress streamed through every phase.
        phases = [p.phase for p in seen]
        assert "gathering" in phases
        assert "scoring" in phases
        assert phases[-1] == "done"

        # THE freeze-replay invariant: every candidate froze its Facts, and replaying the
        # real engine over those frozen Facts under the SAME policy reproduces the stored
        # verdict, score and coverage EXACTLY. If the codec dropped or flipped any field --
        # or the replay diverged from _judge_item -- this fails, which is the guard against
        # the simulator ever showing a wrong preview (a fail-open the moment a weight moves).
        import json as _json

        from reaper.engine.facts_codec import facts_from_dict
        from reaper.engine.gates import Evaluation, evaluate_all
        from reaper.engine.signals import SignalConfig
        from reaper.engine.signals import score as _score
        from reaper.engine.verdict import STRUCTURAL_GATES as _STRUCTURAL
        from reaper.engine.verdict import decide_verdict

        by_type = {
            "movie": (DEFAULT_MOVIE_POLICY, build_gates(DEFAULT_MOVIE_POLICY)),
            "season": (DEFAULT_TV_POLICY, build_gates(DEFAULT_TV_POLICY)),
        }
        for cand in rows.values():
            assert cand.facts_json, f"{cand.media_key} froze no facts"
            policy, gates = by_type[cand.media_type]
            facts, extra = facts_from_dict(_json.loads(cand.facts_json))
            evaluation = Evaluation(results=[*extra, *evaluate_all(gates, facts).results])
            s = _score(
                [
                    SignalConfig(
                        signal=x.signal, weight=x.weight, saturate_at=x.saturate_at, floor=x.floor
                    )
                    for x in policy.signals
                ],
                facts,
                custom_condemn=policy.custom_signal_configs(),
                keeps=policy.keep_configs(),
                window_days=policy.popularity_window_days(),
            )
            score_value = round(s.value)
            coverage_bp = round(s.coverage * 10_000)
            verdict = decide_verdict(
                protected=evaluation.protected,
                blocked=evaluation.blocked,
                safety_protected=any(r.fired and r.gate in _STRUCTURAL for r in evaluation.results),
                score=score_value,
                coverage_bp=coverage_bp,
                condemn_at=policy.condemn_at,
                coverage_floor_bp=policy.coverage_floor_bp,
            )
            assert (verdict, score_value, coverage_bp) == (
                cand.verdict,
                cand.score,
                cand.coverage_bp,
            ), f"replay diverged for {cand.media_key}"

    async def test_one_unreachable_radarr_degrades_but_keeps_the_rest(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        await _seed_play(cache_engine, row_id=1, rating_key=99)
        await _seed_imdb(cache_engine, {"tt0000001": (5.0, 5000)})

        tautulli = _FakeTautulli(movies=_movie_spine())
        snapshot = await scan(
            cache_engine,
            session,
            radarrs=[
                RadarrSource(client=_FakeRadarr(_movie_payloads()), instance_id=1, name="hd"),  # type: ignore[arg-type]
                RadarrSource(client=_BrokenRadarr(), instance_id=2, name="uhd"),  # type: ignore[arg-type]
            ],
            tautulli=tautulli,  # type: ignore[arg-type]
            movie_policy=DEFAULT_MOVIE_POLICY,
            movie_gates=build_gates(DEFAULT_MOVIE_POLICY),
            tv_policy=DEFAULT_TV_POLICY,
            tv_gates=build_gates(DEFAULT_TV_POLICY),
        )
        await session.commit()

        # Loud, and fail-closed: the snapshot says which instance is missing, and the
        # reachable instance's items are all still there.
        assert snapshot.degraded is True
        assert "radarr 'uhd' unreachable" in (snapshot.degraded_reason or "")
        rows = {c.media_key: c for c in await candidates(session, snapshot.id)}
        assert set(rows) == {"radarr:1:1", "radarr:1:2", "radarr:1:3"}


class TestAStaleMirrorDegradesTheSnapshot:
    """An ingest that quietly stopped running is invisible without a freshness check.

    ``test_a_failed_history_sync_degrades_the_snapshot`` below covers the loud case,
    where the sync raises. This is the quiet one: nothing raises, the mirror is simply
    not being refreshed, watcher counts stay frozen at their last value, and dormancy
    climbs for every item on the server at the rate of the outage.

    The clock that decides this is *when the ingest last ran*, never *when somebody last
    watched something*: those two are identical for a stalled ingest and for a household
    that went away for a weekend, so reading the newest play calls a quiet library broken
    and blocks every deletion until somebody watches something.
    """

    async def _scan_with(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> Any:  # the Snapshot row
        await _seed_imdb(cache_engine, {"tt0000001": (5.0, 5000), "tt0000042": (5.0, 5000)})
        return await scan(
            cache_engine,
            session,
            radarrs=[
                RadarrSource(client=_FakeRadarr(_movie_payloads()), instance_id=1, name="hd")  # type: ignore[arg-type]
            ],
            sonarrs=[],
            tautulli=_FakeTautulli(  # type: ignore[arg-type]
                movies=_movie_spine(), shows=_show_spine(), children=_show_children()
            ),
            movie_policy=DEFAULT_MOVIE_POLICY,
            movie_gates=build_gates(DEFAULT_MOVIE_POLICY),
            tv_policy=DEFAULT_TV_POLICY,
            tv_gates=build_gates(DEFAULT_TV_POLICY),
        )

    async def test_an_ingest_that_has_not_run_recently_degrades(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """Plays are still landing, but the last sync was five days ago: a stalled ingest.

        The recent play is what makes this the real test: reading the newest play instead
        of the sync clock would call this mirror healthy.
        """
        await _seed_play(
            cache_engine,
            row_id=1,
            rating_key=99,
            synced_ago=timedelta(days=5),
            recent_play=True,
        )

        snapshot = await self._scan_with(session, cache_engine)

        assert snapshot.degraded is True
        assert "watch history has not updated recently" in (snapshot.degraded_reason or "")

    async def test_a_mirror_that_never_synced_degrades(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """No record of the ingest ever running is no evidence it did. Fail closed."""
        await _seed_play(cache_engine, row_id=1, rating_key=99, recent_play=True)
        async with cache_engine.begin() as conn:
            await conn.execute(text("DELETE FROM history_sync_state"))

        snapshot = await self._scan_with(session, cache_engine)

        assert snapshot.degraded is True
        assert "watch history has not updated recently" in (snapshot.degraded_reason or "")

    async def test_a_quiet_library_that_synced_recently_does_not_degrade(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """Nobody has watched anything for 2000 days, and the ingest ran an hour ago.

        A single-household server, or an operator away for a long weekend, looks exactly
        like this. It has the most to reclaim, so it must scan.
        """
        await _seed_play(cache_engine, row_id=1, rating_key=99)

        snapshot = await self._scan_with(session, cache_engine)

        assert snapshot.degraded is False, snapshot.degraded_reason


class TestRunScanHistorySync:
    """The orchestrator's handling of the watch-history mirror sync.

    The mirror is the primary condemning evidence -- dormancy and watcher counts are
    read from it -- so a failed sync must degrade the snapshot (loud, viewable,
    un-executable), never let the scan score quietly on a stale mirror. A play that
    landed after the last successful sync is invisible to scoring; only degradation
    keeps that item deletable-looking snapshot from being executed.
    """

    async def test_a_failed_history_sync_degrades_the_snapshot(
        self, tmp_path: Path, cache_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from reaper.engine.policy import ProfileSettings
        from reaper.services import scan_runner

        captured: dict[str, Any] = {}

        async def fake_scan(engine: Any, session: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(id=1, item_count=0, degraded=False)

        async def failing_sync(engine: Any, tautulli: Any) -> Any:
            raise IntegrationError("tautulli", "unreachable (boom)")

        class _CmTautulli:
            async def __aenter__(self) -> _CmTautulli:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def users(self) -> list[dict[str, Any]]:
                return []

        async def fake_sources(factory: Any, settings: Any, box: Any, **kwargs: Any) -> Any:
            return ([], [], _CmTautulli(), None, None)

        async def fake_policies(session: Any) -> Any:
            return (
                profiles.ActivePolicy(DEFAULT_MOVIE_POLICY, "default"),
                profiles.ActivePolicy(DEFAULT_TV_POLICY, "default"),
            )

        async def fake_profile(session: Any) -> Any:
            return ProfileSettings()

        async def fake_sync_lists(engine: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        async def fake_sync_degradations(engine: Any, synced: Any) -> list[str]:
            return []

        monkeypatch.setattr(scan_runner, "build_sources", fake_sources)
        monkeypatch.setattr(scan_runner.history_sync, "sync", failing_sync)
        monkeypatch.setattr(scan_runner.profiles, "active_policies", fake_policies)
        monkeypatch.setattr(scan_runner.profiles, "active_profile_settings", fake_profile)
        monkeypatch.setattr(scan_runner.snapshot_service, "scan", fake_scan)
        monkeypatch.setattr(scan_runner.snapshot_service, "sync_protection_lists", fake_sync_lists)
        monkeypatch.setattr(
            scan_runner.snapshot_service, "protection_sync_degradations", fake_sync_degradations
        )

        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        engine = create_engine(settings)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        try:
            await scan_runner.run_scan(
                settings=settings,
                session_factory=factory,
                cache_engine=cache_engine,
                box=None,  # type: ignore[arg-type]  # build_sources is stubbed; never read
            )
        finally:
            await engine.dispose()

        reasons = captured.get("extra_degrade_reasons")
        assert reasons, "a failed history sync must hand the scan a degradation reason"
        assert any("Watch history could not be refreshed" in r for r in reasons)
        assert any("nothing may be deleted" in r for r in reasons)


class TestARepairedPolicyCannotBeReapedFrom:
    """A policy Reaper had to repair to load it is safe to SCAN on and unsafe to DELETE on.

    The rescale cannot move a score, so the numbers this scan produces are right. But the
    body it ran was never saved by anyone: it is Reaper's repair of a stored row, and an
    approval names a policy hash. So the scan degrades (``scan_runner._run_scan_locked``
    appends the reason to ``pre_scan_degradations``), and ``planner.build_plan`` -- the one
    call ``POST /api/runs`` makes before anything can be executed -- refuses a degraded
    snapshot outright, so no run ever exists to execute.

    Driven through the real ``snapshot_service.scan`` and the real planner, so the chain
    is pinned end to end: the branch, the reason reaching ``snapshot.degraded``, and
    degraded blocking the plan. ``tests/test_fact_layer_states.py`` pins the flags this
    reads.
    """

    async def _run(
        self,
        tmp_path: Path,
        cache_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
        *,
        rescaled: bool,
    ) -> tuple[Any, async_sessionmaker[AsyncSession], AsyncEngine]:
        from types import SimpleNamespace

        from reaper.engine.policy import ProfileSettings
        from reaper.services import scan_runner

        class _ScanTautulli(_FakeTautulli):
            """The scan's Tautulli surface plus the user list run_scan checks."""

            async def users(self) -> list[dict[str, Any]]:
                return [{"username": "user-one", "is_active": 1, "keep_history": 1}]

        async def fake_sources(factory: Any, settings: Any, box: Any, **kwargs: Any) -> Any:
            return (
                [RadarrSource(client=_FakeRadarr(_movie_payloads()), instance_id=1, name="hd")],
                [],
                _ScanTautulli(movies=_movie_spine()),
                None,
                None,
            )

        async def ok_sync(engine: Any, tautulli: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(rows=0)

        async def fake_policies(session: Any) -> Any:
            return (
                profiles.ActivePolicy(DEFAULT_MOVIE_POLICY, "default", rescaled=rescaled),
                profiles.ActivePolicy(DEFAULT_TV_POLICY, "default"),
            )

        async def fake_profile(session: Any) -> Any:
            return ProfileSettings()

        async def fake_sync_lists(engine: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        async def fake_sync_degradations(engine: Any, synced: Any) -> list[str]:
            return []

        monkeypatch.setattr(scan_runner, "build_sources", fake_sources)
        monkeypatch.setattr(scan_runner.history_sync, "sync", ok_sync)
        monkeypatch.setattr(scan_runner.profiles, "active_policies", fake_policies)
        monkeypatch.setattr(scan_runner.profiles, "active_profile_settings", fake_profile)
        monkeypatch.setattr(scan_runner.snapshot_service, "sync_protection_lists", fake_sync_lists)
        monkeypatch.setattr(
            scan_runner.snapshot_service, "protection_sync_degradations", fake_sync_degradations
        )

        await _seed_play(cache_engine, row_id=1, rating_key=99)
        await _seed_imdb(cache_engine, {"tt0000001": (5.0, 5000)})

        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        engine = create_engine(settings)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        snapshot = await scan_runner.run_scan(
            settings=settings,
            session_factory=factory,
            cache_engine=cache_engine,
            box=None,  # type: ignore[arg-type]  # build_sources is stubbed; never read
        )
        return snapshot, factory, engine

    async def test_a_repaired_policy_degrades_the_scan_and_blocks_the_plan(
        self, tmp_path: Path, cache_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from reaper.services.planner import PlanError, build_plan

        snapshot, factory, engine = await self._run(
            tmp_path, cache_engine, monkeypatch, rescaled=True
        )
        try:
            assert snapshot.degraded is True
            reason = snapshot.degraded_reason or ""
            assert "movie policy needs saving again" in reason, reason

            async with factory() as s:
                with pytest.raises(PlanError, match="degraded"):
                    await build_plan(
                        s,
                        snapshot_id=snapshot.id,
                        policy_hash=snapshot.policy_hash,
                        approved_by="test",
                    )
        finally:
            await engine.dispose()

    async def test_the_same_scan_on_a_saved_policy_can_be_planned(
        self, tmp_path: Path, cache_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control. Identical evidence, identical condemned item; only the repair flag
        differs. Without this the test above would still pass if planning were broken for
        some unrelated reason."""
        from reaper.services.planner import build_plan

        snapshot, factory, engine = await self._run(
            tmp_path, cache_engine, monkeypatch, rescaled=False
        )
        try:
            assert snapshot.degraded is False, snapshot.degraded_reason

            async with factory() as s:
                run = await build_plan(
                    s,
                    snapshot_id=snapshot.id,
                    policy_hash=snapshot.policy_hash,
                    approved_by="test",
                )
                assert run.id is not None
        finally:
            await engine.dispose()


class TestReleaseAgeRoundsTowardKeeping:
    """Only the release YEAR is known, so the derived age must take the bound that
    produces LESS deletion pressure: the end of the year, not the start."""

    def test_a_year_only_date_reads_as_december_31(self) -> None:
        obs = _release_age_days(2000)
        assert isinstance(obs, Known)
        assert obs.value == float((utcnow().date() - date(2000, 12, 31)).days)

    def test_the_current_year_clamps_to_zero_rather_than_negative(self) -> None:
        obs = _release_age_days(utcnow().year)
        assert isinstance(obs, Known)
        assert obs.value == 0.0


# ---------------------------------------------------------------------------
# One scan at a time -- the claim lives inside run_scan, shared by every caller.
# ---------------------------------------------------------------------------


class TestOneScanAtATime:
    """The browser's scan button and the scheduler's cron job both come through
    ``run_scan``, and the one-at-a-time claim lives inside it -- so the two can never
    overlap and race each other's grace-clock writes, whoever started first."""

    def _stub_pipeline(self, monkeypatch: pytest.MonkeyPatch, parked_scan: Any) -> None:
        """A run_scan whose body is fully stubbed, with the snapshot step scriptable."""
        from types import SimpleNamespace

        from reaper.engine.policy import ProfileSettings
        from reaper.services import scan_runner

        class _CmTautulli:
            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def users(self) -> list[dict[str, Any]]:
                return []

        async def fake_sources(factory: Any, settings: Any, box: Any, **kwargs: Any) -> Any:
            return ([], [], _CmTautulli(), None, None)

        async def fake_policies(session: Any) -> Any:
            return (
                profiles.ActivePolicy(DEFAULT_MOVIE_POLICY, "default"),
                profiles.ActivePolicy(DEFAULT_TV_POLICY, "default"),
            )

        async def fake_profile(session: Any) -> Any:
            return ProfileSettings()

        async def ok_sync(engine: Any, tautulli: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(rows=0)

        async def fake_sync_lists(engine: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        async def fake_sync_degradations(engine: Any, synced: Any) -> list[str]:
            return []

        monkeypatch.setattr(scan_runner, "build_sources", fake_sources)
        monkeypatch.setattr(scan_runner.history_sync, "sync", ok_sync)
        monkeypatch.setattr(scan_runner.profiles, "active_policies", fake_policies)
        monkeypatch.setattr(scan_runner.profiles, "active_profile_settings", fake_profile)
        monkeypatch.setattr(scan_runner.snapshot_service, "scan", parked_scan)
        monkeypatch.setattr(scan_runner.snapshot_service, "sync_protection_lists", fake_sync_lists)
        monkeypatch.setattr(
            scan_runner.snapshot_service, "protection_sync_degradations", fake_sync_degradations
        )

    async def test_a_second_scan_is_refused_while_one_runs(
        self, tmp_path: Path, cache_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        from types import SimpleNamespace

        from reaper.services import scan_runner

        started = asyncio.Event()
        release = asyncio.Event()

        async def parked_scan(engine: Any, session: Any, **kwargs: Any) -> Any:
            started.set()
            await release.wait()
            return SimpleNamespace(id=1, item_count=0, degraded=False)

        self._stub_pipeline(monkeypatch, parked_scan)
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        engine = create_engine(settings)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        try:
            kwargs: dict[str, Any] = {
                "settings": settings,
                "session_factory": factory,
                "cache_engine": cache_engine,
                "box": None,  # build_sources is stubbed; never read
            }
            task = asyncio.create_task(scan_runner.run_scan(**kwargs))
            await asyncio.wait_for(started.wait(), timeout=5)

            with pytest.raises(scan_runner.ScanInProgressError, match="already running"):
                await scan_runner.run_scan(**kwargs)

            release.set()
            snapshot = await asyncio.wait_for(task, timeout=5)
            assert snapshot.id == 1
        finally:
            release.set()
            await engine.dispose()

    async def test_the_claim_is_released_after_a_failed_scan(
        self, tmp_path: Path, cache_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scan that dies must not leave the claim held, or no scan could ever run
        again without a restart."""
        from types import SimpleNamespace

        from reaper.services import scan_runner

        calls = {"n": 0}

        async def failing_then_fine(engine: Any, session: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrationError("radarr", "unreachable (boom)")
            return SimpleNamespace(id=2, item_count=0, degraded=False)

        self._stub_pipeline(monkeypatch, failing_then_fine)
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        engine = create_engine(settings)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        try:
            kwargs: dict[str, Any] = {
                "settings": settings,
                "session_factory": factory,
                "cache_engine": cache_engine,
                "box": None,
            }
            with pytest.raises(IntegrationError):
                await scan_runner.run_scan(**kwargs)

            snapshot = await scan_runner.run_scan(**kwargs)  # the claim was released
            assert snapshot.id == 2
        finally:
            await engine.dispose()

    async def test_the_scheduler_skips_quietly_when_a_scan_is_running(
        self, tmp_path: Path, cache_engine: AsyncEngine
    ) -> None:
        """The cron path must not error, retry, or stack: an in-flight scan means this
        firing is skipped and the next one runs normally."""
        from reaper.services import scan_runner, scheduler

        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        engine = create_engine(settings)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        assert scan_runner._claim_scan()  # a scan is "running"
        try:
            # Must return without raising -- the refusal is caught and logged.
            await scheduler.scheduled_scan(settings, factory, cache_engine, None)  # type: ignore[arg-type]
        finally:
            scan_runner._release_scan()
            await engine.dispose()


# ---------------------------------------------------------------------------
# Keep History coverage -- a user Tautulli is not recording makes "nobody watched
# it" untrustworthy, so the scan degrades.
# ---------------------------------------------------------------------------


class _UsersTautulli:
    def __init__(self, rows: list[dict[str, Any]] | None = None, *, raise_error: bool = False):
        self._rows = rows or []
        self._raise = raise_error

    async def users(self) -> list[dict[str, Any]]:
        if self._raise:
            raise IntegrationError("tautulli", "users unavailable")
        return list(self._rows)


class TestKeepHistoryCoverage:
    """A household member with Tautulli's per-user Keep History off is invisible in the
    history table: everything only they watch looks never-played. The scan must degrade
    while that is true -- and while it cannot even check."""

    async def test_an_active_user_with_recording_off_degrades(self) -> None:
        from reaper.services.scan_runner import _keep_history_degradations

        reasons = await _keep_history_degradations(
            _UsersTautulli(
                [
                    {"username": "user-one", "is_active": 1, "keep_history": 1},
                    {"username": "user-two", "is_active": 1, "keep_history": 0},
                ]
            )  # type: ignore[arg-type]
        )

        assert any("user-two" in r for r in reasons)
        assert any("nothing may be deleted" in r for r in reasons)

    async def test_everyone_recording_is_clean(self) -> None:
        from reaper.services.scan_runner import _keep_history_degradations

        reasons = await _keep_history_degradations(
            _UsersTautulli(
                [
                    {"username": "user-one", "is_active": 1, "keep_history": 1},
                    {"username": "user-two", "is_active": True, "keep_history": "1"},
                ]
            )  # type: ignore[arg-type]
        )

        assert reasons == []

    async def test_an_inactive_users_setting_does_not_matter(self) -> None:
        """A user removed from the server cannot play anything, so their old Keep
        History setting cannot hide a play."""
        from reaper.services.scan_runner import _keep_history_degradations

        reasons = await _keep_history_degradations(
            _UsersTautulli(
                [
                    {"username": "user-one", "is_active": 1, "keep_history": 1},
                    {"username": "departed", "is_active": 0, "keep_history": 0},
                ]
            )  # type: ignore[arg-type]
        )

        assert reasons == []

    async def test_an_unreadable_user_list_degrades(self) -> None:
        from reaper.services.scan_runner import _keep_history_degradations

        reasons = await _keep_history_degradations(
            _UsersTautulli(raise_error=True)  # type: ignore[arg-type]
        )

        assert any("could not check" in r for r in reasons)

    async def test_a_row_missing_the_flag_degrades_as_unconfirmed(self) -> None:
        """A payload shape we cannot read is not assumed safe: fail closed, loudly and
        accurately (could-not-confirm, not user-has-it-off)."""
        from reaper.services.scan_runner import _keep_history_degradations

        reasons = await _keep_history_degradations(
            _UsersTautulli([{"username": "user-one", "is_active": 1}])  # type: ignore[arg-type]
        )

        assert any("cannot confirm" in r for r in reasons)

    async def test_the_degradation_reaches_the_snapshot(
        self, tmp_path: Path, cache_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end through run_scan: the reason lands in extra_degrade_reasons, so the
        snapshot is marked degraded exactly like any other failed source."""
        from types import SimpleNamespace

        from reaper.engine.policy import ProfileSettings
        from reaper.services import scan_runner

        captured: dict[str, Any] = {}

        async def fake_scan(engine: Any, session: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(id=1, item_count=0, degraded=False)

        class _OffTautulli(_UsersTautulli):
            def __init__(self) -> None:
                super().__init__([{"username": "user-two", "is_active": 1, "keep_history": 0}])

            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

        async def fake_sources(factory: Any, settings: Any, box: Any, **kwargs: Any) -> Any:
            return ([], [], _OffTautulli(), None, None)

        async def ok_sync(engine: Any, tautulli: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(rows=0)

        async def fake_policies(session: Any) -> Any:
            return (
                profiles.ActivePolicy(DEFAULT_MOVIE_POLICY, "default"),
                profiles.ActivePolicy(DEFAULT_TV_POLICY, "default"),
            )

        async def fake_profile(session: Any) -> Any:
            return ProfileSettings()

        async def fake_sync_lists(engine: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        async def fake_sync_degradations(engine: Any, synced: Any) -> list[str]:
            return []

        monkeypatch.setattr(scan_runner, "build_sources", fake_sources)
        monkeypatch.setattr(scan_runner.history_sync, "sync", ok_sync)
        monkeypatch.setattr(scan_runner.profiles, "active_policies", fake_policies)
        monkeypatch.setattr(scan_runner.profiles, "active_profile_settings", fake_profile)
        monkeypatch.setattr(scan_runner.snapshot_service, "scan", fake_scan)
        monkeypatch.setattr(scan_runner.snapshot_service, "sync_protection_lists", fake_sync_lists)
        monkeypatch.setattr(
            scan_runner.snapshot_service, "protection_sync_degradations", fake_sync_degradations
        )

        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        engine = create_engine(settings)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        try:
            await scan_runner.run_scan(
                settings=settings,
                session_factory=factory,
                cache_engine=cache_engine,
                box=None,  # type: ignore[arg-type]  # build_sources is stubbed; never read
            )
        finally:
            await engine.dispose()

        reasons = captured.get("extra_degrade_reasons")
        assert reasons, "an unrecorded user must hand the scan a degradation reason"
        assert any("user-two" in r for r in reasons)
