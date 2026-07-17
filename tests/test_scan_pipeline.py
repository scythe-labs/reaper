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
from datetime import timedelta
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
from reaper.engine.policy import DEFAULT_MOVIE_POLICY, DEFAULT_TV_POLICY
from reaper.services import history_sync, lists, season_scan
from reaper.services.scan_runner import build_gates
from reaper.services.snapshot import Progress, RadarrSource, candidates, scan

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


class _BrokenRadarr:
    async def movies(self) -> list[dict[str, Any]]:
        raise IntegrationError("radarr", "unreachable (boom)")


class _FakeSonarr:
    def __init__(self, series: list[dict[str, Any]]) -> None:
        self._series = series

    async def series(self) -> list[dict[str, Any]]:
        return self._series

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


async def _seed_play(engine: AsyncEngine, *, row_id: int, rating_key: int) -> None:
    """One long-ago play: it anchors the data horizon ~2000 days back."""
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
            return SimpleNamespace(id=1, item_count=0)

        async def failing_sync(engine: Any, tautulli: Any) -> Any:
            raise IntegrationError("tautulli", "unreachable (boom)")

        class _CmTautulli:
            async def __aenter__(self) -> _CmTautulli:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

        async def fake_sources(factory: Any, settings: Any, box: Any) -> Any:
            return ([], [], _CmTautulli(), None, None)

        async def fake_policies(session: Any) -> Any:
            return (DEFAULT_MOVIE_POLICY, DEFAULT_TV_POLICY)

        async def fake_profile(session: Any) -> Any:
            return ProfileSettings()

        async def fake_sync_lists(engine: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        async def fake_sync_degradations(engine: Any, synced: Any) -> list[str]:
            return []

        monkeypatch.setattr(scan_runner, "build_sources", fake_sources)
        monkeypatch.setattr(scan_runner.history_sync, "sync", failing_sync)
        monkeypatch.setattr("reaper.api.routes.active_policies", fake_policies)
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
