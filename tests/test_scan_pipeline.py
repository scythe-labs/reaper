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

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from reaper.api import routes
from reaper.api.schemas import SimStale, SimulationOut
from reaper.clients.base import IntegrationError
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import (
    Candidate,
    FirstFlagged,
    SeasonPruneEvidence,
    SizeSource,
    Snapshot,
    WatchHighWater,
    WhitelistEntry,
)
from reaper.db.session import create_cache_engine, create_engine, create_session_factory
from reaper.engine.dormancy import history_reach_days
from reaper.engine.gates import GateId
from reaper.engine.observation import Known, Unknown
from reaper.engine.policy import DEFAULT_MOVIE_POLICY, DEFAULT_TV_POLICY
from reaper.services import (
    app_settings,
    history_sync,
    lists,
    profiles,
    requested_by,
    season_evidence,
    season_pruning,
    season_scan,
    watch_evidence,
)
from reaper.services.condemned import reap_is_effective
from reaper.services.scan_runner import _allowed_sections, build_gates
from reaper.services.snapshot import Progress, RadarrSource, _release_age_days, scan


async def candidates(
    session: AsyncSession, snapshot_id: int, *, verdict: str | None = None
) -> list[Candidate]:
    """A snapshot's judged rows, ranked the way the review queue ranks them.

    A test-local read, not a production one: nothing in ``src/`` needs it, so it lived in
    ``services/snapshot`` for twelve callers that are all in this file (#320). It defends
    nothing -- it is a plain SELECT -- and a plain SELECT sitting in a deletion-path service
    reads as production reach it never had.
    """
    stmt = select(Candidate).where(Candidate.snapshot_id == snapshot_id)
    if verdict:
        stmt = stmt.where(Candidate.verdict == verdict)
    stmt = stmt.order_by(Candidate.score.desc(), Candidate.size_bytes.desc())
    return list((await session.execute(stmt)).scalars().all())


NOW = utcnow().replace(microsecond=0)
LONG_AGO = NOW - timedelta(days=2000)

#: When the fixture's show and its seasons landed in Plex.
#:
#: Inside the ~2000-day mirror ``_seed_play`` anchors, so an all-time watcher count taken
#: over that mirror is an answer for these seasons rather than a lower bound. It has to be:
#: the keep-rule conflict detector holds every prunable season whose count the reach cannot
#: support (``season_pruning._detect_conflicts``), and the placeholder epoch this replaced
#: dated the seasons to 1970 -- fifty years outside a mirror the same fixture makes 2000 days
#: deep -- so every season here was correctly un-establishable and these pipeline tests were
#: measuring that hold instead of the pipeline. Still well past the 1095-day dormancy floor,
#: so the seasons condemn on score exactly as before.
SEASONS_ADDED = NOW - timedelta(days=1500)


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
    """Which Plex libraries the scan enriches against. Turning a library off is what
    KEEPS its files, so widening on a failed read is the condemn direction and degrades."""

    async def test_no_configured_libraries_scans_everything(self, session: AsyncSession) -> None:
        assert await _allowed_sections(session) == (None, None)

    async def test_the_enabled_subset_scopes_the_scan(self, session: AsyncSession) -> None:
        await app_settings.set_plex_libraries(
            session,
            [
                {"key": 1, "title": "Movies", "kind": "movie", "enabled": True},
                {"key": 2, "title": "4K Movies", "kind": "movie", "enabled": False},
                {"key": 3, "title": "TV", "kind": "show", "enabled": True},
            ],
        )
        assert await _allowed_sections(session) == ({1, 3}, None)

    async def test_an_unreadable_setting_degrades_rather_than_widening_silently(
        self, tmp_path: Path
    ) -> None:
        """A settings-read failure must not abort a scan, and must not quietly widen it.

        Scanning every library is the more permissive branch, not the safe one: a library
        the operator turned OFF is one whose items resolve unmatched and are therefore
        kept, so re-adding it walks those files into the condemnable set. The scan still
        runs (a viewable snapshot beats an aborted one) but says so, which makes the
        snapshot un-executable.
        """
        settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
        engine = create_engine(settings)  # no create_all: the app_setting table is missing
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        async with factory() as s:
            sections, degradation = await _allowed_sections(s)
        assert sections is None
        assert degradation is not None
        assert "could not be read" in degradation
        await engine.dispose()

    async def test_every_library_turned_off_is_honored_and_does_not_degrade(
        self, session: AsyncSession
    ) -> None:
        """An explicit "none of them" is a choice, not a failure: enrich nothing, no
        degradation. It is the read FAILURE that widens, and only that one degrades."""
        await app_settings.set_plex_libraries(
            session,
            [
                {"key": 1, "title": "Movies", "kind": "movie", "enabled": False},
                {"key": 2, "title": "TV", "kind": "show", "enabled": False},
            ],
        )
        assert await _allowed_sections(session) == (set(), None)


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
    added = str(int(SEASONS_ADDED.timestamp()))
    return [{"rating_key": 900, "title": "Long Show", "year": 2005, "added_at": added}]


def _show_children() -> dict[int, list[dict[str, Any]]]:
    added = str(int(SEASONS_ADDED.timestamp()))
    return {
        900: [{"media_index": n, "rating_key": 900 + n, "added_at": added} for n in range(1, 6)]
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
        # since they arrived 1500 days ago with nobody watching, condemned). That the
        # mirror covers those 1500 days is load-bearing -- see ``SEASONS_ADDED``.
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

    async def test_a_scan_keeps_the_pure_policy_verdict_under_a_hand_override(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """The scan never bakes a hand override into the stored verdict. A spared condemnation is
        still stored ``condemn`` (so undoing the spare falls back to the real policy result), a
        reaped protection is still stored ``protect``, and the grace clock tracks the EFFECTIVE
        set: the spared item loses its clock, an engine-honored reap earns one (rules 4/48-51)."""
        await _seed_play(cache_engine, row_id=1, rating_key=99)
        await _seed_imdb(cache_engine, {"tt0000001": (5.0, 5000), "tt0000042": (5.0, 5000)})
        await lists.sync(
            cache_engine,
            _StaticList([lists.ListItem(media_type="movie", imdb_id="tt0000002", title="B")]),
            mode=lists.ListMode.HARD,
            kind=lists.ListKind.WHITELIST,
        )

        # The owner's hand decisions, set BEFORE this scan: spare a soul the policy condemns
        # (radarr:1:1) and reap one the policy protects (radarr:1:2, kept by the whitelist list).
        session.add_all(
            [
                WhitelistEntry(
                    media_key="radarr:1:1", title="A", decision="spare", created_at=utcnow()
                ),
                WhitelistEntry(
                    media_key="radarr:1:2", title="B", decision="reap", created_at=utcnow()
                ),
            ]
        )
        await session.flush()

        tautulli = _FakeTautulli(
            movies=_movie_spine(), shows=_show_spine(), children=_show_children()
        )
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
        )
        await session.commit()

        rows = {c.media_key: c for c in await candidates(session, snapshot.id)}

        # The stored verdict is PURE POLICY -- the override never reached it. This is the whole
        # point: an un-spare / un-reap now falls back to the real policy result, after a rescan too.
        assert rows["radarr:1:1"].verdict == "condemn"  # spared, yet policy still condemns it
        assert rows["radarr:1:2"].verdict == "protect"  # reaped, yet policy still protects it
        # And the spare stayed OUT of the explanation: no baked "whitelisted" protection.
        assert "you spared this by hand" not in (rows["radarr:1:1"].explanation_json or "")

        # The grace clock follows the EFFECTIVE set: the spare took its item off the reap list so
        # its clock is gone, and the reap earns one exactly when the engine honors it.
        flagged = {
            f.media_key
            for f in (await session.execute(text("SELECT media_key FROM first_flagged"))).all()
        }
        assert "radarr:1:1" not in flagged  # spared -> off the list -> no grace clock
        assert ("radarr:1:2" in flagged) is reap_is_effective(rows["radarr:1:2"])
        assert {"sonarr:1:42:2", "sonarr:1:42:3"} <= flagged  # untouched condemnations keep theirs

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


class TestAStoredSizeSaysWhereItCameFrom:
    """A size nobody reported is stored as nothing, not as zero.

    ``size_source`` says which measurement the size is, and is None exactly when there is
    none. The pair is what the whole deletion lane reads: the planner holds an unmeasured
    item back, the executor refuses it again, and the scan's tally counts how often this
    happens at all.

    A fabricated ``0`` here would read as an affirmative measurement everywhere
    downstream, and the numbers beside the delete button would quietly run low.
    """

    async def test_a_movie_nobody_sized_carries_no_source(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        await _seed_play(cache_engine, row_id=1, rating_key=99)
        payloads = _movie_payloads()
        # hasFile true, no sizeOnDisk: a partial payload, which is the whole case.
        del payloads[0]["sizeOnDisk"]

        snapshot = await scan(
            cache_engine,
            session,
            radarrs=[
                RadarrSource(client=_FakeRadarr(payloads), instance_id=1, name="hd")  # type: ignore[arg-type]
            ],
            tautulli=_FakeTautulli(movies=_movie_spine()),  # type: ignore[arg-type]
            movie_policy=DEFAULT_MOVIE_POLICY,
            movie_gates=build_gates(DEFAULT_MOVIE_POLICY),
            tv_policy=DEFAULT_TV_POLICY,
            tv_gates=build_gates(DEFAULT_TV_POLICY),
        )
        await session.commit()

        rows = {c.media_key: c for c in await candidates(session, snapshot.id)}
        unsized = rows["radarr:1:1"]
        assert unsized.size_source is None
        assert unsized.size_bytes is None  # not 0: nothing measured it

        sized = rows["radarr:1:2"]
        assert sized.size_source == SizeSource.RADARR
        assert sized.size_bytes == 4_000_000_000

    async def test_a_season_sonarr_would_not_size_carries_no_source(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        await _seed_imdb(cache_engine, {"tt0000042": (5.0, 5000)})
        series = _series_payloads()
        # Season 2 is one of the prunable middle seasons, so it reaches the judge.
        for season in series[0]["seasons"]:
            if season["seasonNumber"] == 2:
                del season["statistics"]["sizeOnDisk"]

        snapshot = await scan(
            cache_engine,
            session,
            radarrs=[],
            sonarrs=[
                season_scan.SonarrSource(client=_FakeSonarr(series), instance_id=1, name="tv")
            ],
            tautulli=_FakeTautulli(  # type: ignore[arg-type]
                movies=[], shows=_show_spine(), children=_show_children()
            ),
            movie_policy=DEFAULT_MOVIE_POLICY,
            movie_gates=build_gates(DEFAULT_MOVIE_POLICY),
            tv_policy=DEFAULT_TV_POLICY,
            tv_gates=build_gates(DEFAULT_TV_POLICY),
        )
        await session.commit()

        rows = {c.media_key: c for c in await candidates(session, snapshot.id)}
        assert rows["sonarr:1:42:2"].size_source is None
        assert rows["sonarr:1:42:2"].size_bytes is None
        assert rows["sonarr:1:42:3"].size_source == SizeSource.SONARR
        assert rows["sonarr:1:42:3"].size_bytes == 1_000_000_000

    async def test_seasons_older_than_the_watch_mirror_are_held_not_condemned(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """The keep-rule conflict detector really is handed the mirror's reach by the scan.

        The same show as the full-pipeline test above, moved so it arrived 500 days before
        the mirror starts. Every play its seasons ever had is behind the horizon, so their
        all-time counts read 0 and can out-rank nothing: the detector cannot establish that
        a prunable season is watched no more than one the rule keeps, and holds it.

        Driven end to end rather than against ``plan_series_prune``, because the argument has
        a default and a unit test cannot tell a correctly-passed bound from an omitted one
        (rule 141). Seasons 2 and 3 condemn in that test on evidence identical but for the
        arrival date, so this fails if ``_judge_series`` stops passing ``shortfall_by_season``.
        """
        await _seed_play(cache_engine, row_id=1, rating_key=99)
        await _seed_imdb(cache_engine, {"tt0000042": (5.0, 5000)})

        before_the_mirror = str(int((LONG_AGO - timedelta(days=500)).timestamp()))
        children = {
            900: [
                {"media_index": n, "rating_key": 900 + n, "added_at": before_the_mirror}
                for n in range(1, 6)
            ]
        }

        snapshot = await scan(
            cache_engine,
            session,
            radarrs=[],
            sonarrs=[
                season_scan.SonarrSource(
                    client=_FakeSonarr(_series_payloads()), instance_id=1, name="tv"
                )
            ],
            tautulli=_FakeTautulli(  # type: ignore[arg-type]
                movies=[], shows=_show_spine(), children=children
            ),
            movie_policy=DEFAULT_MOVIE_POLICY,
            movie_gates=build_gates(DEFAULT_MOVIE_POLICY),
            tv_policy=DEFAULT_TV_POLICY,
            tv_gates=build_gates(DEFAULT_TV_POLICY),
        )
        await session.commit()

        rows = {c.media_key: c for c in await candidates(session, snapshot.id)}
        assert rows["sonarr:1:42:2"].verdict == "abstain"
        assert rows["sonarr:1:42:3"].verdict == "abstain"
        # And it says so in the operator's words rather than reporting a count it never
        # established.
        assert "cannot tell whether Season 2 is watched more than" in (
            rows["sonarr:1:42:2"].explanation_json or ""
        )
        # Held means held: nothing reached the grace clock, so nothing is queued to remove.
        flagged = {
            f.media_key
            for f in (await session.execute(text("SELECT media_key FROM first_flagged"))).all()
        }
        assert flagged == set()


class TestTheScanCountsHowOftenItCouldNotMeasure:
    """``scan.size_source_tally`` is the only place Reaper reports how often it could not
    measure an item, and it is a log line rather than a column, so nothing downstream goes
    red when a refactor drops it (#321).

    Two facts hold it up here. It fires exactly once per scan, and its buckets partition
    the items the scan judged -- the same set the candidate rows come from -- so a rung
    that quietly stops being counted lands as a sum that no longer reconciles. The class
    above pins the stored ``size_source`` column; this pins the measurement taken over it.

    The miss bucket is reported at zero rather than omitted, which is the half that is a
    behavior change: a ``Counter`` drops an absent key, so an operator grepping the line
    for ``unmeasured`` could not tell "none" from "never emitted."
    """

    @staticmethod
    def _tally(logs: list[dict[str, Any]]) -> dict[str, Any]:
        """The one tally event, or a failure naming how many there actually were."""
        events = [e for e in logs if e["event"] == "scan.size_source_tally"]
        assert len(events) == 1, f"one tally per scan, got {len(events)}"
        return events[0]

    async def _scan_both_lanes(
        self,
        session: AsyncSession,
        cache_engine: AsyncEngine,
        *,
        movies: list[dict[str, Any]],
        series: list[dict[str, Any]],
    ) -> tuple[Snapshot, dict[str, Any]]:
        await _seed_play(cache_engine, row_id=1, rating_key=99)
        await _seed_imdb(cache_engine, {"tt0000001": (5.0, 5000), "tt0000042": (5.0, 5000)})
        with capture_logs() as logs:
            snapshot = await scan(
                cache_engine,
                session,
                radarrs=[
                    RadarrSource(client=_FakeRadarr(movies), instance_id=1, name="hd")  # type: ignore[arg-type]
                ],
                sonarrs=[
                    season_scan.SonarrSource(client=_FakeSonarr(series), instance_id=1, name="tv")
                ],
                tautulli=_FakeTautulli(  # type: ignore[arg-type]
                    movies=_movie_spine(), shows=_show_spine(), children=_show_children()
                ),
                movie_policy=DEFAULT_MOVIE_POLICY,
                movie_gates=build_gates(DEFAULT_MOVIE_POLICY),
                tv_policy=DEFAULT_TV_POLICY,
                tv_gates=build_gates(DEFAULT_TV_POLICY),
            )
        await session.commit()
        return snapshot, self._tally(logs)

    async def test_one_tally_per_scan_partitions_everything_it_judged(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """Every movie and every content-bearing season falls in exactly one bucket."""
        snapshot, tally = await self._scan_both_lanes(
            session, cache_engine, movies=_movie_payloads(), series=_series_payloads()
        )

        judged = len(await candidates(session, snapshot.id))
        assert judged == 8  # three movies, five content-bearing seasons
        assert tally["snapshot"] == snapshot.id
        assert tally["total"] == judged
        assert sum(tally["sources"].values()) == judged

        # Every fixture item carries a size, so both rungs are full and nothing missed.
        assert tally["sources"] == {"radarr": 3, "sonarr": 5, "unmeasured": 0}
        # Stated twice on purpose: the dict above would still read as correct with the key
        # gone, and this is the assertion whose failure names the ambiguity (#321).
        assert "unmeasured" in tally["sources"], "a scan that missed nothing must say so as 0"

    async def test_the_miss_bucket_counts_an_unmeasured_item_in_either_lane(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """One movie and one season nobody sized, so a lane that stopped counting its own
        misses cannot hide behind the other lane still counting them."""
        movies = _movie_payloads()
        del movies[0]["sizeOnDisk"]  # hasFile, no size: the partial payload
        series = _series_payloads()
        for season in series[0]["seasons"]:
            if season["seasonNumber"] == 2:
                del season["statistics"]["sizeOnDisk"]

        snapshot, tally = await self._scan_both_lanes(
            session, cache_engine, movies=movies, series=series
        )

        judged = len(await candidates(session, snapshot.id))
        assert tally["sources"] == {"radarr": 2, "sonarr": 4, "unmeasured": 2}
        assert sum(tally["sources"].values()) == judged == tally["total"]


class TestAStaleMirrorDegradesTheSnapshot:
    """An ingest that quietly stopped running is invisible without a freshness check.

    ``test_a_failed_history_sync_degrades_the_snapshot`` below covers the loud case,
    where the sync raises. This is the quiet one: nothing raises, the mirror is simply
    not being refreshed, watcher counts stay frozen at their last value, and dormancy
    climbs for every item on the server at the rate of the outage.

    The clock that decides this is *when the ingest last ran*, never *when somebody last
    watched something*: those two are identical for a stalled ingest and for users
    who went away for a weekend, so reading the newest play calls a quiet library broken
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

        A single-user server, or an operator away for a long weekend, looks exactly
        like this. It has the most to reclaim, so it must scan.
        """
        await _seed_play(cache_engine, row_id=1, rating_key=99)

        snapshot = await self._scan_with(session, cache_engine)

        assert snapshot.degraded is False, snapshot.degraded_reason


class TestADegradedSnapshotDoesNotActOnItsOwnCondemnSet:
    """A degraded scan is un-plannable, but two side effects never went through the
    planner: the grace clock, and the Leaving Soon shelf / heads-up.

    Both act on a condemn set built from evidence Reaper itself declared untrustworthy.
    The clock is the one that outlives the run: because a clock restarts only after a gap
    longer than a whole grace window, a run of consecutive degraded scans keeps the
    countdown alive, and the first healthy scan finds the operator's warning window
    already spent. Skipping can only ever cause an extra restart, which is more grace.
    """

    async def _scan_with(
        self, session: AsyncSession, cache_engine: AsyncEngine, *, degrade: bool
    ) -> Any:
        await _seed_imdb(cache_engine, {"tt0000001": (5.0, 5000), "tt0000042": (5.0, 5000)})
        await _seed_play(cache_engine, row_id=1, rating_key=99)
        radarrs = [
            RadarrSource(client=_FakeRadarr(_movie_payloads()), instance_id=1, name="hd")  # type: ignore[arg-type]
        ]
        if degrade:
            # A second, unreachable Radarr: one failed source, snapshot degraded, and the
            # first instance's items still judged. The same shape as the loud-failure test.
            radarrs.append(RadarrSource(client=_BrokenRadarr(), instance_id=2, name="uhd"))  # type: ignore[arg-type]
        return await scan(
            cache_engine,
            session,
            radarrs=radarrs,
            sonarrs=[],
            tautulli=_FakeTautulli(movies=_movie_spine()),  # type: ignore[arg-type]
            movie_policy=DEFAULT_MOVIE_POLICY,
            movie_gates=build_gates(DEFAULT_MOVIE_POLICY),
            tv_policy=DEFAULT_TV_POLICY,
            tv_gates=build_gates(DEFAULT_TV_POLICY),
        )

    async def _flagged(self, session: AsyncSession) -> set[str]:
        rows = (await session.execute(text("SELECT media_key FROM first_flagged"))).all()
        return {r.media_key for r in rows}

    async def test_a_healthy_scan_still_starts_the_clock(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """The control: without this, the test below would pass on a broken scan."""
        snapshot = await self._scan_with(session, cache_engine, degrade=False)

        assert snapshot.degraded is False, snapshot.degraded_reason
        assert await self._flagged(session) == {"radarr:1:1", "radarr:1:2"}

    async def test_a_degraded_scan_starts_no_clock(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        snapshot = await self._scan_with(session, cache_engine, degrade=True)

        assert snapshot.degraded is True
        # The same item is still condemned and still viewable -- it just does not start
        # a countdown on evidence this run already called untrustworthy.
        rows = {c.media_key: c for c in await candidates(session, snapshot.id)}
        assert rows["radarr:1:1"].verdict == "condemn"
        assert await self._flagged(session) == set()

    async def test_a_degraded_scan_does_not_extend_an_existing_clock(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """The one that costs the operator their warning time.

        A clock already running must be left exactly as it was: refreshing
        last_seen_condemned_at on a degraded run is what silently keeps a spent window
        alive across a run of bad scans.
        """
        session.add(
            FirstFlagged(
                media_key="radarr:1:1",
                first_flagged_at=LONG_AGO,
                last_seen_condemned_at=LONG_AGO,
            )
        )
        await session.flush()
        before = await session.get(FirstFlagged, "radarr:1:1")
        assert before is not None
        seen_before = before.last_seen_condemned_at

        await self._scan_with(session, cache_engine, degrade=True)

        after = await session.get(FirstFlagged, "radarr:1:1")
        assert after is not None
        assert after.first_flagged_at == LONG_AGO
        assert after.last_seen_condemned_at == seen_before


class TestTheStreamingVetoNeedsAReadableAnswer:
    """ "Nobody is watching" and "we could not tell" look identical from an empty set.

    Which one it is decides whether a file somebody is streaming right now can be
    condemned, so the flag is typed and set where the read fails -- never inferred from
    the wording of a degradation reason, which is how it used to be detected: rewording
    the message would have silently turned the veto off.
    """

    async def _scan_with(
        self, session: AsyncSession, cache_engine: AsyncEngine, tautulli: Any
    ) -> Any:
        await _seed_imdb(cache_engine, {"tt0000001": (5.0, 5000), "tt0000042": (5.0, 5000)})
        await _seed_play(cache_engine, row_id=1, rating_key=99)
        return await scan(
            cache_engine,
            session,
            radarrs=[
                RadarrSource(client=_FakeRadarr(_movie_payloads()), instance_id=1, name="hd")  # type: ignore[arg-type]
            ],
            sonarrs=[],
            tautulli=tautulli,
            movie_policy=DEFAULT_MOVIE_POLICY,
            movie_gates=build_gates(DEFAULT_MOVIE_POLICY),
            tv_policy=DEFAULT_TV_POLICY,
            tv_gates=build_gates(DEFAULT_TV_POLICY),
        )

    async def test_a_genuinely_empty_session_list_is_a_measurement(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        snapshot = await self._scan_with(
            session, cache_engine, _FakeTautulli(movies=_movie_spine(), sessions=[])
        )

        assert snapshot.degraded is False, snapshot.degraded_reason

    async def test_a_null_session_list_is_not_a_measurement(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """A 200 carrying `{"sessions": null}` is a successful call that answered nothing.

        Coercing it to an empty list reads as "nothing is streaming" on every item at
        once, which is the veto silently defeated -- and the executor's live re-check is
        the only thing left between that and deleting a file mid-stream.
        """

        class _NullSessions(_FakeTautulli):
            async def activity(self) -> dict[str, Any]:
                return {"sessions": None}

        snapshot = await self._scan_with(
            session, cache_engine, _NullSessions(movies=_movie_spine())
        )

        assert snapshot.degraded is True
        assert "playing right now" in (snapshot.degraded_reason or "")

    async def test_an_unreachable_tautulli_still_degrades(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        class _BrokenActivity(_FakeTautulli):
            async def activity(self) -> dict[str, Any]:
                raise IntegrationError("tautulli", "unreachable (boom)")

        snapshot = await self._scan_with(
            session, cache_engine, _BrokenActivity(movies=_movie_spine())
        )

        assert snapshot.degraded is True
        assert "playing right now" in (snapshot.degraded_reason or "")


class TestAnUnreadableRatingsDatasetKeepsInsteadOfCondemning:
    """A stale IMDb dataset is the inverted failure: the missing evidence REMOVES a
    protection rather than adding one.

    With the dataset unreadable the lookup map is empty, which is indistinguishable from
    "every title here is genuinely unrated" unless something says otherwise. Recorded as
    Absent, that withdraws every rating-based keep across the whole library at once and
    the why-panel asserts a rating was checked and found missing, when it was never
    readable at all.
    """

    async def _scan_without_ratings(self, session: AsyncSession, cache_engine: AsyncEngine) -> Any:
        # Deliberately NO _seed_imdb: the dataset is missing, so lookup degrades.
        await _seed_play(cache_engine, row_id=1, rating_key=99)
        return await scan(
            cache_engine,
            session,
            radarrs=[
                RadarrSource(client=_FakeRadarr(_movie_payloads()), instance_id=1, name="hd")  # type: ignore[arg-type]
            ],
            sonarrs=[
                season_scan.SonarrSource(
                    client=_FakeSonarr(_series_payloads()),  # type: ignore[arg-type]
                    instance_id=1,
                    name="tv",
                )
            ],
            tautulli=_FakeTautulli(  # type: ignore[arg-type]
                movies=_movie_spine(), shows=_show_spine(), children=_show_children()
            ),
            movie_policy=DEFAULT_MOVIE_POLICY,
            movie_gates=build_gates(DEFAULT_MOVIE_POLICY),
            tv_policy=DEFAULT_TV_POLICY,
            tv_gates=build_gates(DEFAULT_TV_POLICY),
        )

    async def test_ratings_read_as_unknown_on_both_paths(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        import json as _json

        from reaper.engine.facts_codec import facts_from_dict

        snapshot = await self._scan_without_ratings(session, cache_engine)
        assert snapshot.degraded is True

        rows = {c.media_key: c for c in await candidates(session, snapshot.id)}
        # One movie and one season, so the fix is pinned on both fact builders -- a field
        # populated on one path and not the other silently moves scores on the other.
        for key in ("radarr:1:1", "sonarr:1:42:2"):
            facts, _ = facts_from_dict(_json.loads(rows[key].facts_json))
            rating = facts.imdb_rating_tenths
            assert isinstance(rating, Unknown), (key, rating)
            assert "could not be read" in rating.reason, key
            assert isinstance(facts.imdb_votes, Unknown), key

        # And nothing is condemned on ratings nobody could read: Unknown blocks the
        # rating floor into an abstain, where Absent left the item free to go.
        assert all(c.verdict != "condemn" for c in rows.values())


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
            return ([], [], _CmTautulli(), [], None)

        async def fake_policies(session: Any) -> Any:
            return (
                profiles.ActivePolicy(DEFAULT_MOVIE_POLICY, "default"),
                profiles.ActivePolicy(DEFAULT_TV_POLICY, "default"),
            )

        async def fake_profile(session: Any) -> Any:
            return profiles.ActiveProfile(ProfileSettings())

        async def fake_sync_lists(engine: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        async def fake_sync_degradations(engine: Any, synced: Any) -> list[str]:
            return []

        monkeypatch.setattr(scan_runner, "build_sources", fake_sources)
        monkeypatch.setattr(scan_runner.history_sync, "sync", failing_sync)
        monkeypatch.setattr(scan_runner.profiles, "active_policies", fake_policies)
        monkeypatch.setattr(scan_runner.profiles, "active_profile", fake_profile)
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
                [],
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
            return profiles.ActiveProfile(ProfileSettings())

        async def fake_sync_lists(engine: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        async def fake_sync_degradations(engine: Any, synced: Any) -> list[str]:
            return []

        monkeypatch.setattr(scan_runner, "build_sources", fake_sources)
        monkeypatch.setattr(scan_runner.history_sync, "sync", ok_sync)
        monkeypatch.setattr(scan_runner.profiles, "active_policies", fake_policies)
        monkeypatch.setattr(scan_runner.profiles, "active_profile", fake_profile)
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
                    approved_by="test",
                )
                assert run.id is not None
        finally:
            await engine.dispose()


class TestReleaseAgeRoundsTowardKeeping:
    """Only the release YEAR is known, so the derived age must take the bound that
    produces LESS deletion pressure: the end of the year, not the start.

    The clock is frozen for both of these. They used to re-read the wall clock in the
    assertion while production read it again inside ``_release_age_days``, so the two
    sampled different instants: straddling UTC midnight between them is an off-by-one-day
    failure, microseconds wide but real under xdist on a slow runner, arriving once at
    00:00 UTC with no obvious cause. A frozen instant also lets the expected day count be
    a literal rather than the same subtraction production does.
    """

    #: An arbitrary fixed instant. Nothing depends on the date itself, only on both sides
    #: of the comparison using the SAME one.
    FROZEN = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)

    @pytest.fixture(autouse=True)
    def _frozen_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Patched on the module under test, not on reaper.clock: snapshot.py imported the
        # name, so rebinding the source module would leave production reading the real one.
        monkeypatch.setattr("reaper.services.snapshot.utcnow", lambda: self.FROZEN)

    def test_a_year_only_date_reads_as_december_31(self) -> None:
        obs = _release_age_days(2000)

        assert isinstance(obs, Known)
        assert obs.value == 9336.0  # 2000-12-31 -> 2026-07-24

    def test_january_first_would_overstate_the_age_by_almost_a_year(self) -> None:
        """The rule 31 property, stated as the comparison it exists to prevent. A Jan-1
        reading adds 365 days of age to a title nobody can date more precisely, which
        over-matches every "older than N" rule the owner writes."""
        obs = _release_age_days(2000)

        assert isinstance(obs, Known)
        assert obs.value < 9701.0  # what 2000-01-01 -> 2026-07-24 would have given

    def test_the_current_year_clamps_to_zero_rather_than_negative(self) -> None:
        obs = _release_age_days(self.FROZEN.year)

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
            return ([], [], _CmTautulli(), [], None)

        async def fake_policies(session: Any) -> Any:
            return (
                profiles.ActivePolicy(DEFAULT_MOVIE_POLICY, "default"),
                profiles.ActivePolicy(DEFAULT_TV_POLICY, "default"),
            )

        async def fake_profile(session: Any) -> Any:
            return profiles.ActiveProfile(ProfileSettings())

        async def ok_sync(engine: Any, tautulli: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(rows=0)

        async def fake_sync_lists(engine: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        async def fake_sync_degradations(engine: Any, synced: Any) -> list[str]:
            return []

        monkeypatch.setattr(scan_runner, "build_sources", fake_sources)
        monkeypatch.setattr(scan_runner.history_sync, "sync", ok_sync)
        monkeypatch.setattr(scan_runner.profiles, "active_policies", fake_policies)
        monkeypatch.setattr(scan_runner.profiles, "active_profile", fake_profile)
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


class TestATautulliSpineFailureDegradesRatherThanKillingTheScan:
    """The library listing is the scan's spine, and it had no handler at all.

    Every other evidence source degrades: an unreachable Radarr, a failed Plex sweep, a
    stalled mirror. A Tautulli library-list timeout raised straight through the gather
    and aborted the run with no snapshot to look at -- so a transient hiccup cost the
    operator their scan instead of costing them a plan. With no spine rows, nothing
    matches, which abstains and keeps.
    """

    async def test_a_failed_library_list_degrades_and_still_produces_a_snapshot(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        await _seed_imdb(cache_engine, {"tt0000001": (5.0, 5000), "tt0000042": (5.0, 5000)})
        await _seed_play(cache_engine, row_id=1, rating_key=99)

        class _BrokenSpine(_FakeTautulli):
            async def libraries(self) -> list[dict[str, Any]]:
                raise IntegrationError("tautulli", "libraries timed out")

        snapshot = await scan(
            cache_engine,
            session,
            radarrs=[
                RadarrSource(client=_FakeRadarr(_movie_payloads()), instance_id=1, name="hd")  # type: ignore[arg-type]
            ],
            sonarrs=[],
            tautulli=_BrokenSpine(movies=_movie_spine()),  # type: ignore[arg-type]
            movie_policy=DEFAULT_MOVIE_POLICY,
            movie_gates=build_gates(DEFAULT_MOVIE_POLICY),
            tv_policy=DEFAULT_TV_POLICY,
            tv_gates=build_gates(DEFAULT_TV_POLICY),
        )

        assert snapshot.degraded is True
        assert "could not be read" in (snapshot.degraded_reason or "")
        # And the run survived: items are present, unmatched, and therefore kept.
        rows = {c.media_key: c for c in await candidates(session, snapshot.id)}
        assert rows
        assert all(c.verdict != "condemn" for c in rows.values())


class TestKeepHistoryCoverage:
    """A user with Tautulli's per-user Keep History off is invisible in the
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

    async def test_an_unparseable_flag_is_unreadable_not_true(self) -> None:
        """`bool("false")` is True, and that was the whole bug.

        The docstring on `_flag` promises None for anything unreadable, and
        `_keep_history_degradations` is built on that promise -- an unreadable value goes
        in the cannot-confirm bucket and degrades. Falling back to plain truthiness read
        every non-empty string as "yes, recording": no degradation, the scan stays
        executable, and every title only that user watches scores as never-played. Which
        is the one case this whole function exists to catch.
        """
        from reaper.services.scan_runner import _flag

        for junk in ("false", "N", "off", "no", {"nested": 1}, ["x"], "maybe"):
            row = {"username": "user-one", "keep_history": junk}
            assert _flag(row, "keep_history") in (False, None), junk
            assert _flag(row, "keep_history") is not True, junk

    async def test_the_flags_it_does_understand(self) -> None:
        """Tautulli stores these as integers, so 1/0 and "1"/"0" are the real path."""
        from reaper.services.scan_runner import _flag

        for truthy in (1, "1", True, 2, "true", "Yes", " ON "):
            assert _flag({"f": truthy}, "f") is True, truthy
        for falsy in (0, "0", False, "false", "No", "off"):
            assert _flag({"f": falsy}, "f") is False, falsy
        assert _flag({"f": None}, "f") is None
        assert _flag({}, "f") is None
        assert _flag("not a row", "f") is None

    async def test_a_truthy_unparseable_flag_degrades_end_to_end(self) -> None:
        """The contract, through the caller: an unreadable value degrades the scan.

        It used to pass straight through as "recording", because the value is a non-empty
        string and every non-empty string is truthy.
        """
        from reaper.services.scan_runner import _keep_history_degradations

        reasons = await _keep_history_degradations(
            _UsersTautulli(
                [
                    {"username": "user-one", "is_active": 1, "keep_history": 1},
                    {"username": "user-two", "is_active": 1, "keep_history": "maybe"},
                ]
            )  # type: ignore[arg-type]
        )

        assert any("cannot confirm" in r for r in reasons)

    async def test_a_spelled_out_false_is_read_as_off_not_as_unreadable(self) -> None:
        """ "false" is a value we DO understand, so it names the user rather than landing
        in the vaguer cannot-confirm bucket. Both degrade; only the wording differs."""
        from reaper.services.scan_runner import _keep_history_degradations

        reasons = await _keep_history_degradations(
            _UsersTautulli([{"username": "user-two", "is_active": 1, "keep_history": "false"}])  # type: ignore[arg-type]
        )

        assert any("user-two" in r for r in reasons)

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
            return ([], [], _OffTautulli(), [], None)

        async def ok_sync(engine: Any, tautulli: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(rows=0)

        async def fake_policies(session: Any) -> Any:
            return (
                profiles.ActivePolicy(DEFAULT_MOVIE_POLICY, "default"),
                profiles.ActivePolicy(DEFAULT_TV_POLICY, "default"),
            )

        async def fake_profile(session: Any) -> Any:
            return profiles.ActiveProfile(ProfileSettings())

        async def fake_sync_lists(engine: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        async def fake_sync_degradations(engine: Any, synced: Any) -> list[str]:
            return []

        monkeypatch.setattr(scan_runner, "build_sources", fake_sources)
        monkeypatch.setattr(scan_runner.history_sync, "sync", ok_sync)
        monkeypatch.setattr(scan_runner.profiles, "active_policies", fake_policies)
        monkeypatch.setattr(scan_runner.profiles, "active_profile", fake_profile)
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


class TestTheWatchBlindnessGuardThroughAWholeScan:
    """The guard end to end, because every other test for it calls a function directly.

    Deleting all four of the scan's handoffs -- ``watch_marks`` into ``season_scan.gather``,
    ``watch_blind_reason`` into ``build_facts``, the ``watch_evidence.record`` call and the
    ``watch_blind_items`` assignment -- once left the whole suite green, so the guard was
    proven as a function and unproven as a feature (rule 118). ``mypy`` now refuses the first
    one; these pin the rest, on both lanes, by the only thing an operator would notice: an item
    that condemned on a false zero stops condemning.

    The two items are the ones ``TestScanPipelineEndToEnd`` asserts condemn, so the flip is
    against a verdict this suite already pins in the other direction.
    """

    async def _scan(
        self,
        session: AsyncSession,
        cache_engine: AsyncEngine,
        *,
        degrade: Sequence[str] | None = None,
    ) -> Any:
        return await scan(
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
            tautulli=_FakeTautulli(  # type: ignore[arg-type]
                movies=_movie_spine(), shows=_show_spine(), children=_show_children()
            ),
            movie_policy=DEFAULT_MOVIE_POLICY,
            movie_gates=build_gates(DEFAULT_MOVIE_POLICY),
            tv_policy=DEFAULT_TV_POLICY,
            tv_gates=build_gates(DEFAULT_TV_POLICY),
            extra_degrade_reasons=degrade,
        )

    async def _prepare(self, cache_engine: AsyncEngine) -> None:
        await _seed_play(cache_engine, row_id=1, rating_key=99)
        await _seed_imdb(cache_engine, {"tt0000001": (5.0, 5000), "tt0000042": (5.0, 5000)})

    async def test_a_mark_the_mirror_can_no_longer_support_stops_the_condemn(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        # An earlier scan measured three watchers for the dormant movie and two for the
        # dormant season. The mirror holds no plays under either item's current Plex key, so
        # both read zero now -- a fall no library can perform.
        await self._prepare(cache_engine)
        session.add_all(
            [
                WatchHighWater(media_key="radarr:1:1", watchers_all_time=3, updated_at=utcnow()),
                WatchHighWater(media_key="sonarr:1:42:2", watchers_all_time=2, updated_at=utcnow()),
            ]
        )
        await session.flush()

        snapshot = await self._scan(session, cache_engine)
        await session.commit()
        rows = {c.media_key: c for c in await candidates(session, snapshot.id)}

        # Both condemn in TestScanPipelineEndToEnd, on the same fixtures, with no mark.
        assert rows["radarr:1:1"].verdict == "abstain"
        assert rows["sonarr:1:42:2"].verdict != "condemn"
        # The operator is told why, in the stored explanation the why-panel reads.
        assert watch_evidence.BLIND_REASON in (rows["radarr:1:1"].explanation_json or "")
        assert watch_evidence.BLIND_REASON in (rows["sonarr:1:42:2"].explanation_json or "")
        # And the count Settings reads is the count of what actually happened. Two items
        # went blind; the third season held below is collateral and is NOT counted, because
        # its own plays read fine.
        assert snapshot.watch_blind_items == 2

    async def test_a_blind_season_holds_the_sibling_a_viewer_is_about_to_watch(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """The sharp end, and the reason the blind reason is decided before the roll-up.

        Season 3's own plays are perfectly readable, so it takes no ``Unknown`` of its own and
        scores and condemns at full confidence. What it loses when season 2 goes blind is the
        mid-binge guard: the viewer's place in the show is read from the same mirror, so a
        viewer who finished season 2 leaves no trace, ``active_progress`` expires them, and the
        season they were about to watch becomes prunable. A protection that fires and does not
        protect (rule 140).
        """
        await self._prepare(cache_engine)
        session.add(
            WatchHighWater(media_key="sonarr:1:42:2", watchers_all_time=2, updated_at=utcnow())
        )
        await session.flush()

        snapshot = await self._scan(session, cache_engine)
        await session.commit()
        rows = {c.media_key: c for c in await candidates(session, snapshot.id)}

        # Season 3 condemns in TestScanPipelineEndToEnd on these same fixtures.
        assert rows["sonarr:1:42:3"].verdict != "condemn"
        # Held for the honest reason, which names neither a short mirror nor a cause it did
        # not establish: the two unanswerable reasons are separate strings for that reason.
        explanation = rows["sonarr:1:42:3"].explanation_json or ""
        assert season_pruning.PROGRESS_UNREADABLE_REASON in explanation
        assert season_pruning.PROGRESS_UNESTABLISHABLE_REASON not in explanation
        # Collateral, not blind: only the season whose own plays vanished is counted.
        assert snapshot.watch_blind_items == 1

    async def test_the_blind_scan_leaves_the_mark_standing(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        # The property that makes the guard survive its own first firing. If this scan wrote
        # its zero over the mark, the NEXT scan would see 0 -> 0, call it honest, and condemn
        # on the same false evidence -- with nothing left anywhere to notice.
        await self._prepare(cache_engine)
        session.add(
            WatchHighWater(media_key="radarr:1:1", watchers_all_time=3, updated_at=utcnow())
        )
        await session.flush()

        await self._scan(session, cache_engine)
        await session.commit()

        marks = await watch_evidence.recall_all(session)
        assert marks["radarr:1:1"].watchers_all_time == 3

    async def test_a_degraded_scan_still_records_what_it_measured(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """The mark write is deliberately NOT gated on ``degraded``, and this pins it (#276).

        Rule 116 gates a degraded scan's side effects because they act on the condemned set:
        a grace clock, the shelf and a Discord post each push an item toward deletion, so
        skipping them is the keep direction. This write is the opposite kind. The mark is what
        a LATER scan reads to WITHHOLD pressure, so skipping it removes a reason to keep, and
        ``degraded`` is snapshot-global -- it fires on one *arr being unreachable, on sessions
        or ratings being unreadable, none of which say anything about this item's plays.

        So: a title watched by one person, on a scan degraded for an unrelated reason. Gate the
        write and no mark is stored; when this item's Plex key later churns, the fall has
        nothing to fall from and it reads Known(0) with maximum dormancy -- the exact defect
        ``watch_evidence`` exists to prevent, arriving through the guard meant to be careful.
        """
        await self._prepare(cache_engine)
        # radarr:1:1 carries Plex rating key 11, so this play is ITS play -- unlike the
        # anchor play in `_prepare`, whose key no candidate here holds.
        await _seed_play(cache_engine, row_id=2, rating_key=11)

        snapshot = await self._scan(
            session, cache_engine, degrade=["could not read what is playing right now"]
        )
        await session.commit()

        assert snapshot.degraded is True
        marks = await watch_evidence.recall_all(session)
        assert marks["radarr:1:1"].watchers_all_time == 1

    async def test_a_scan_with_no_marks_condemns_and_counts_none(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        # The other side, and the one that keeps the feature usable: with nothing to fall
        # from, every item reads honestly and the count is a real zero, not a NULL.
        await self._prepare(cache_engine)

        snapshot = await self._scan(session, cache_engine)
        await session.commit()
        rows = {c.media_key: c for c in await candidates(session, snapshot.id)}

        assert rows["radarr:1:1"].verdict == "condemn"
        assert rows["sonarr:1:42:2"].verdict == "condemn"
        assert snapshot.watch_blind_items == 0


class TestAProtectionSwitchDoesNotMoveTheEvidence:
    """The claim the policy simulator's replay tier rests on, driven through a real scan.

    ``PolicyBody.evidence_hash`` lets a gate edit replay off the frozen Facts instead of
    demanding a scan, and that is only honest if a scan under the edited policy would have
    frozen the same bytes. Argued from the code it is nearly obvious -- no fact builder
    branches on the gate list, and ``evaluate_all`` reads nothing but ``Facts`` -- but the
    hash is what stands between an operator and a confident wrong preview, so it is
    measured here rather than reasoned about. A gather-phase read of a gate setting added
    later fails this without anyone having to remember why it matters.
    """

    #: Nothing depends on the instant, only on both scans using the same one: the frozen
    #: Facts carry ages in days, and a suite straddling midnight UTC would otherwise
    #: produce two honest answers that differ.
    FROZEN = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)

    @pytest.fixture(autouse=True)
    def _frozen_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("reaper.services.snapshot.utcnow", lambda: self.FROZEN)

    async def _scan_under(
        self, session: AsyncSession, cache_engine: AsyncEngine, movie_policy: Any
    ) -> dict[str, Candidate]:
        tautulli = _FakeTautulli(
            movies=_movie_spine(), shows=_show_spine(), children=_show_children()
        )
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
            movie_policy=movie_policy,
            movie_gates=build_gates(movie_policy),
            tv_policy=DEFAULT_TV_POLICY,
            tv_gates=build_gates(DEFAULT_TV_POLICY),
        )
        await session.commit()
        return {c.media_key: c for c in await candidates(session, snapshot.id)}

    async def test_a_scan_freezes_the_same_facts_whether_a_protection_is_on_or_off(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        await _seed_play(cache_engine, row_id=1, rating_key=99)
        await _seed_imdb(cache_engine, {"tt0000001": (8.5, 50_000), "tt0000042": (8.5, 50_000)})

        on = DEFAULT_MOVIE_POLICY
        off = DEFAULT_MOVIE_POLICY.model_copy(
            update={
                "gates": tuple(
                    g.model_copy(update={"enabled": False}) if g.gate is GateId.RATING_FLOOR else g
                    for g in DEFAULT_MOVIE_POLICY.gates
                )
            }
        )
        # The switch has to matter, or identical evidence proves nothing: a well-rated movie
        # is kept by the bar while it is on, and judged on its score once it is off.
        assert on.scoring_hash() != off.scoring_hash()

        with_gate = await self._scan_under(session, cache_engine, on)
        without_gate = await self._scan_under(session, cache_engine, off)

        assert with_gate.keys() == without_gate.keys()
        for key, before in with_gate.items():
            assert before.facts_json == without_gate[key].facts_json, (
                f"{key} froze different evidence under a gate toggle, so the simulator's "
                "replay of that edit is not exact"
            )

        # ...and the toggle really did reach a verdict, so the evidence held still across a
        # change that moved the answer rather than across no change at all.
        assert any(
            before.verdict != without_gate[key].verdict for key, before in with_gate.items()
        ), "the gate toggle changed no verdict, so this proves nothing"

    async def test_a_scan_freezes_the_same_facts_when_a_protection_threshold_moves(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """The same claim for the other tunable an operator drags. Dormancy is measured
        from the item's own dates, never from the floor it is compared against."""
        await _seed_play(cache_engine, row_id=1, rating_key=99)
        await _seed_imdb(cache_engine, {"tt0000001": (5.0, 5000), "tt0000042": (5.0, 5000)})

        def _at(days: int) -> Any:
            return DEFAULT_MOVIE_POLICY.model_copy(
                update={
                    "gates": tuple(
                        g.model_copy(update={"threshold": days})
                        if g.gate is GateId.MIN_DORMANCY
                        else g
                        for g in DEFAULT_MOVIE_POLICY.gates
                    )
                }
            )

        low, high = _at(30), _at(100_000)

        lenient = await self._scan_under(session, cache_engine, low)
        strict = await self._scan_under(session, cache_engine, high)

        for key, before in lenient.items():
            assert before.facts_json == strict[key].facts_json, (
                f"{key} froze different evidence when only a dormancy floor moved"
            )
        # A floor nothing can clear keeps everything, which is what makes the pair a test.
        assert any(before.verdict != strict[key].verdict for key, before in lenient.items())

    async def test_the_popularity_window_does_move_the_frozen_evidence(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """The negative control, and the reason the window is the one gate setting
        ``evidence_hash`` still folds in.

        Without this the pair above would pass just as happily if ``facts_json`` were
        constant, or if the comparison could not see a difference at all. It also pins the
        exclusion itself: the frozen watcher count IS the window, so an operator who
        shortens it gets the refusal rather than a count taken over a span they no longer
        asked about.
        """
        # Rating key 11 is the one a candidate actually carries, and the play lands 2000
        # days back: inside the wide window, outside the narrow one. Seeded against a key
        # no candidate holds, this test passes while comparing nothing, which is how the
        # first draft of it read.
        await _seed_play(cache_engine, row_id=1, rating_key=11)
        await _seed_imdb(cache_engine, {"tt0000001": (5.0, 5000), "tt0000042": (5.0, 5000)})

        def _window(days: int) -> Any:
            return DEFAULT_MOVIE_POLICY.model_copy(
                update={
                    "gates": tuple(
                        g.model_copy(update={"window_days": days})
                        if g.gate is GateId.SERVER_POPULARITY
                        else g
                        for g in DEFAULT_MOVIE_POLICY.gates
                    )
                }
            )

        wide, narrow = _window(3650), _window(1)
        assert wide.evidence_hash() != narrow.evidence_hash()

        long_view = await self._scan_under(session, cache_engine, wide)
        short_view = await self._scan_under(session, cache_engine, narrow)

        assert any(
            before.facts_json != short_view[key].facts_json for key, before in long_view.items()
        ), "the window changed no frozen evidence, so the tests above compare nothing"


class TestASeasonRuleReplaysExactlyOffTheFrozenBundle:
    """The claim #491 asks for, driven through two real scans rather than argued.

    A season's guard result is decided per SHOW, from inputs that never reached ``Facts``,
    so freezing the guard's *output* explained a scan and could not re-decide one, and every
    season rule blanked the simulator. The scan now freezes the plan's inputs
    (``db.models.SeasonPruneEvidence``) and the replay re-derives the guard from them.

    The bar is exactness, not plausibility (``docs/LEARNINGS.md`` §8): a replayed answer must
    equal what a real scan under that policy would produce. So every test here compares
    against a **second real scan**, never against an expectation typed by hand.

    **It compares the guard per season, not the verdict tally.** The tally was the first
    draft and it was too coarse to see four of the nine settings: a season already held by
    keep-last is held whatever ``protect_incomplete_seasons`` says, so the headline sits
    still while the reason under it changes. Reason-level, every setting is visible, which
    is what lets the sweep below cover all nine rather than the three a tally could
    discriminate (rule 145: prove the guard against the population it claims).
    """

    #: Both scans and the replay share one instant. The frozen bundle carries ages in days
    #: and the mid-binge expiry compares against a stored clock read, so a suite straddling
    #: midnight UTC would otherwise produce two honest answers that differ.
    FROZEN = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)

    #: The show's tvdb id. Present so ``keep_last_scope`` is answerable at all: without one
    #: every request lookup is ``Unknown``, the scope stays fail-closed in both directions,
    #: and a test sweeping it would compare two identical answers.
    TVDB = 4242

    @pytest.fixture(autouse=True)
    def _frozen_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("reaper.services.snapshot.utcnow", lambda: self.FROZEN)
        monkeypatch.setattr("reaper.services.season_scan.utcnow", lambda: self.FROZEN)

    def _tv(self, **edits: Any) -> Any:
        return DEFAULT_TV_POLICY.model_copy(update=edits)

    @classmethod
    def _series(cls) -> list[dict[str, Any]]:
        """Seasons 0 to 6, with one special and one Sonarr is still filling.

        Shaped so each of the nine settings can *change a reason*. On the file-wide fixture
        -- five ordinary seasons, all complete, no specials -- ``keep_specials`` and
        ``protect_incomplete_seasons`` change nothing whatever they are set to, so a replay
        that ignored either matched the scan exactly and this class reported a proof it did
        not have. Season 4 wants ten episodes and holds five (``is_incomplete``); it sits
        outside the default keep-last-2 window so the switch is the only thing holding it.
        """
        series = dict(_series_payloads()[0])
        series["tvdbId"] = cls.TVDB
        seasons = [_season_payload(n) for n in range(0, 7)]
        partial = seasons[4]
        partial["statistics"] = {**partial["statistics"], "episodeCount": 10}
        series["seasons"] = seasons
        return [series]

    @staticmethod
    def _children() -> dict[int, list[dict[str, Any]]]:
        added = str(int(SEASONS_ADDED.timestamp()))
        return {
            900: [{"media_index": n, "rating_key": 900 + n, "added_at": added} for n in range(0, 7)]
        }

    async def _seed_episodes(self, engine: AsyncEngine) -> None:
        """Two viewers, so the mid-binge guard and the conflict detector have something to say.

        Viewer 2 is three episodes into season 2 of five, sixty days ago: inside the default
        180-day hold and outside a 30-day one, which is the only way ``in_progress_hold_days``
        is observable. Viewer 3 finished season 3 long ago, which gives season 3 a watcher
        count for the keep-rule conflict to compare against a kept season's.
        """
        rows: list[dict[str, Any]] = []
        for episode in (1, 2, 3):
            rows.append(
                {
                    "row_id": 5000 + episode,
                    "rating_key": 9020 + episode,
                    "parent": 902,
                    "user_id": 2,
                    "at": int((self.FROZEN - timedelta(days=60)).timestamp()),
                    "index": episode,
                }
            )
        for episode in range(1, 6):
            rows.append(
                {
                    "row_id": 6000 + episode,
                    "rating_key": 9030 + episode,
                    "parent": 903,
                    "user_id": 3,
                    "at": int((self.FROZEN - timedelta(days=400)).timestamp()),
                    "index": episode,
                }
            )
        async with engine.begin() as conn:
            for row in rows:
                await conn.execute(
                    text(
                        "INSERT INTO watch_event (row_id, rating_key, parent_rating_key, "
                        " user_id, watched_at, watched_status, percent_complete, media_type, "
                        " media_index) "
                        "VALUES (:row_id, :rating_key, :parent, :user_id, :at, 1, 100, "
                        " 'episode', :index)"
                    ),
                    row,
                )

    async def _seed(self, cache_engine: AsyncEngine) -> None:
        await _seed_play(cache_engine, row_id=1, rating_key=901)
        await _seed_imdb(cache_engine, {"tt0000001": (5.0, 5000), "tt0000042": (5.0, 5000)})
        await self._seed_episodes(cache_engine)

    async def _scan_under(
        self, session: AsyncSession, cache_engine: AsyncEngine, tv_policy: Any
    ) -> tuple[Snapshot, dict[str, Candidate]]:
        tautulli = _FakeTautulli(
            movies=_movie_spine(), shows=_show_spine(), children=self._children()
        )
        snapshot = await scan(
            cache_engine,
            session,
            radarrs=[
                RadarrSource(client=_FakeRadarr(_movie_payloads()), instance_id=1, name="hd")  # type: ignore[arg-type]
            ],
            sonarrs=[
                season_scan.SonarrSource(
                    client=_FakeSonarr(self._series()), instance_id=1, name="tv"
                )
            ],
            tautulli=tautulli,  # type: ignore[arg-type]
            movie_policy=DEFAULT_MOVIE_POLICY,
            movie_gates=build_gates(DEFAULT_MOVIE_POLICY),
            tv_policy=tv_policy,
            tv_gates=build_gates(tv_policy),
            # Available and holding nothing, so the show is KNOWN not to have been requested.
            # Omitting it leaves every lookup Unknown, under which `keep_last_scope` is
            # fail-closed both ways and unobservable.
            request_index=requested_by.RequestIndex(
                available=True,
                movie_keys=frozenset(),
                show_keys=frozenset(),
                season_keys=frozenset(),
            ),
        )
        await session.commit()
        rows = {
            c.media_key: c
            for c in await candidates(session, snapshot.id)
            if c.media_type == "season"
        }
        return snapshot, rows

    @staticmethod
    def _frozen_guard(row: Candidate) -> tuple[Any, ...]:
        """One season's stored guard result, as the tuple of everything it asserts."""
        from reaper.engine.facts_codec import facts_from_dict

        _, extra = facts_from_dict(json.loads(row.facts_json or "{}"))
        guard = next(e for e in extra if e.gate is GateId.SEASON_PROGRESSION)
        return (guard.outcome, guard.detail, guard.blocked, guard.unestablishable)

    def _scanned_guards(self, rows: dict[str, Candidate]) -> dict[str, tuple[Any, ...]]:
        return {key: self._frozen_guard(row) for key, row in rows.items()}

    async def _replayed_guards(
        self,
        session: AsyncSession,
        snapshot: Snapshot,
        rows: dict[str, Candidate],
        tv_policy: Any,
    ) -> dict[str, tuple[Any, ...]]:
        """The production replay's guard for each season, through the route's own helper."""
        from reaper.engine.facts_codec import facts_from_dict

        bundles = await routes._season_bundles(session, snapshot_id=snapshot.id)
        policy = season_evidence.SeasonPolicy.from_body(tv_policy)
        # The same per-show memo the production loop keeps, so a plan cached for one season
        # of a show is the plan its siblings are judged against here too.
        plans: dict[str, Any] = {}
        out: dict[str, tuple[Any, ...]] = {}
        for key, row in rows.items():
            _, extra = facts_from_dict(json.loads(row.facts_json or "{}"))
            replayed = routes._season_guard_replay(
                row, extra, bundles=bundles, policy=policy, plans=plans
            )
            guard = next(e for e in replayed if e.gate is GateId.SEASON_PROGRESSION)
            out[key] = (guard.outcome, guard.detail, guard.blocked, guard.unestablishable)
        return out

    @staticmethod
    def _tally(rows: dict[str, Candidate]) -> dict[str, int]:
        """The panel's own four figures, off a real scan's stored rows."""
        return {
            "condemned": sum(1 for r in rows.values() if r.verdict == "condemn"),
            "protected": sum(1 for r in rows.values() if r.verdict == "protect"),
            "abstained": sum(1 for r in rows.values() if r.verdict == "abstain"),
            "reclaimable": sum(r.size_bytes or 0 for r in rows.values() if r.verdict == "condemn"),
        }

    @staticmethod
    def _shown(out: SimulationOut) -> dict[str, int]:
        return {
            "condemned": out.condemned,
            "protected": out.protected,
            "abstained": out.abstained,
            "reclaimable": out.reclaimable_bytes,
        }

    #: One edit per season setting, each away from its shipped default and each shaped to
    #: change a guard reason on the fixture above. Swept individually rather than all at
    #: once because the settings MASK each other: with keep-last raised to 4 the incomplete
    #: season is held by rank anyway, so a combined edit hides whichever setting the others
    #: already cover (rule 145).
    SETTINGS: ClassVar[list[dict[str, Any]]] = [
        {"keep_last_seasons": 4},
        {"keep_first_season": False},
        {"keep_last_scope": "requested"},
        {"season_lookahead": 2},
        {"keep_in_progress": False},
        {"in_progress_hold_days": 30},
        {"keep_specials": False},
        {"protect_incomplete_seasons": False},
        {"flag_keep_conflicts": False},
    ]

    @pytest.mark.parametrize("edit", SETTINGS, ids=lambda e: next(iter(e)))
    async def test_each_season_setting_replays_to_what_a_scan_under_it_decided(
        self,
        session: AsyncSession,
        cache_engine: AsyncEngine,
        edit: dict[str, Any],
    ) -> None:
        """The load-bearing sweep, one setting at a time.

        Two roads carry a ``PolicyBody`` to the planner: ``snapshot.scan`` unpacks it into
        ``season_scan.gather``, and the simulator builds a ``SeasonPolicy`` off the draft. A
        value that reaches one road and not the other is invisible until the two answers are
        compared under a body where it differs from the default (rule 141, rule 144), and
        invisible per-setting until each is swept alone.
        """
        await self._seed(cache_engine)
        edited = self._tv(**edit)
        # The edit must be replayable at all, or this measures the refusal path instead.
        assert self._tv().evidence_hash() == edited.evidence_hash()

        first, before = await self._scan_under(session, cache_engine, self._tv())
        _, after = await self._scan_under(session, cache_engine, edited)

        assert before.keys() == after.keys()
        scanned_before = self._scanned_guards(before)
        scanned_after = self._scanned_guards(after)
        assert scanned_before != scanned_after, (
            f"{next(iter(edit))} changed no season's guard on this fixture, so comparing a "
            "replay against it proves nothing about whether the setting reaches the planner"
        )

        replayed = await self._replayed_guards(session, first, before, edited)
        assert replayed == scanned_after, (
            f"replaying the first scan's frozen bundle under {edit} did not reproduce the "
            "guard a real scan under that policy actually decided"
        )

    async def test_the_replay_is_not_answering_the_old_policy(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """The control, and the shape #490's own control was added for.

        A replay that ignored the draft and re-derived under the scan's policy would return
        the first scan's guards, which the sweep above cannot distinguish from a correct
        answer on its own. So this pins the disagreement directly.
        """
        await self._seed(cache_engine)
        first, before = await self._scan_under(session, cache_engine, self._tv())

        replayed = await self._replayed_guards(
            session, first, before, self._tv(keep_last_seasons=4)
        )

        assert replayed != self._scanned_guards(before), (
            "the replay returned the scan's own guards under a different policy, so it is "
            "reading the stored one rather than re-deriving it"
        )

    async def test_the_panel_figures_equal_a_real_scan_under_the_edit(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """The same claim at the granularity the operator actually reads.

        The sweep above compares guards, which is sharper; this compares the four numbers on
        the panel, through the real ``_replay_simulation``, so the thing being promised to
        the operator is the thing that was measured.
        """
        await self._seed(cache_engine)
        edited = self._tv(keep_last_seasons=4, keep_first_season=False)

        first, before = await self._scan_under(session, cache_engine, self._tv())
        _, after = await self._scan_under(session, cache_engine, edited)
        assert self._tally(before) != self._tally(after), (
            "the edit moved no figure, so comparing the replay against it proves nothing"
        )

        bundles = await routes._season_bundles(session, snapshot_id=first.id)
        out = await routes._replay_simulation(
            list(before.values()),
            edited,
            {},
            reach_days=history_reach_days(first.horizon_at, now=first.created_at),
            season_bundles=bundles,
        )

        assert out.exact is True
        assert self._shown(out) == self._tally(after)

    async def test_a_season_rule_does_not_move_the_frozen_bundle(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """The evidence half: a season rule decides, it does not gather.

        Byte-identical payloads, which is what makes replaying one scan's bundle under
        another policy legitimate rather than merely convenient. ``facts_json`` is
        deliberately NOT compared: it carries the guard's output, which is exactly what a
        season rule is supposed to move.
        """
        await self._seed(cache_engine)
        first, _ = await self._scan_under(session, cache_engine, self._tv())
        second, _ = await self._scan_under(
            session, cache_engine, self._tv(keep_last_seasons=4, keep_specials=False)
        )

        one = await self._bundles_json(session, first.id)
        two = await self._bundles_json(session, second.id)
        assert one and one.keys() == two.keys()
        for show, payload in one.items():
            assert payload == two[show], (
                f"{show} froze different season evidence under a season-rule edit, so the "
                "simulator's replay of that edit is not exact"
            )

    async def _bundles_json(self, session: AsyncSession, snapshot_id: int) -> dict[str, str]:
        rows = (
            (
                await session.execute(
                    select(SeasonPruneEvidence.group_key, SeasonPruneEvidence.payload_json).where(
                        SeasonPruneEvidence.snapshot_id == snapshot_id
                    )
                )
            )
            .tuples()
            .all()
        )
        return dict(rows)

    async def test_turning_the_mid_binge_hold_on_refuses(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """The one direction that cannot be answered, and why the bundle is three-state.

        With the guard off the scan skips the Sonarr episode fan-out entirely, so the map the
        sequential guard reads was never gathered. Replaying anyway would fall back to
        whole-season protection and show a preview no scan will produce, which is worse than
        a blank. Turning it OFF is the answerable direction and is covered by the sweep.
        """
        await self._seed(cache_engine)
        first, before = await self._scan_under(
            session, cache_engine, self._tv(keep_in_progress=False)
        )

        with pytest.raises(routes._SeasonEvidenceMissingError) as caught:
            await self._replayed_guards(session, first, before, self._tv(keep_in_progress=True))

        assert caught.value.kind is SimStale.IN_PROGRESS_NOT_READ

    async def test_a_show_whose_every_season_is_kept_is_still_recorded(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """The skip this change removed, as a property rather than as a diff.

        The episodes read used to be spared for a show with no prunable season, which is
        exactly the show a lowered keep-last makes prunable. Its bundle would then carry no
        episode map and the whole lane would refuse, on the edit the season card exists for.
        """
        await self._seed(cache_engine)
        held, rows = await self._scan_under(session, cache_engine, self._tv(keep_last_seasons=6))
        assert rows, "the fixture show produced no season rows"

        bundles = await routes._season_bundles(session, snapshot_id=held.id)
        assert bundles, "a fully kept show recorded no season evidence at all"
        for show, bundle in bundles.items():
            assert bundle.season_final_episode is not None, (
                f"{show} is fully kept and its episode map was skipped, so lowering "
                "keep-last cannot be previewed for it"
            )

    async def test_a_snapshot_with_no_bundle_refuses_rather_than_replaying_empty(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """Every scan taken before this table existed, and why it is not a hash question.

        The evidence hash matches perfectly for such a snapshot -- the policy gathers exactly
        the same things -- so nothing in ``PolicyBody`` can tell the simulator to stand down.
        The stored evidence has to be asked directly.
        """
        await self._seed(cache_engine)
        first, before = await self._scan_under(session, cache_engine, self._tv())
        await session.execute(
            delete(SeasonPruneEvidence).where(SeasonPruneEvidence.snapshot_id == first.id)
        )
        await session.commit()

        with pytest.raises(routes._SeasonEvidenceMissingError) as caught:
            await self._replayed_guards(session, first, before, self._tv(keep_last_seasons=4))

        assert caught.value.kind is SimStale.SEASONS_NOT_RECORDED

    async def test_an_unreadable_bundle_refuses_rather_than_half_parsing(
        self, session: AsyncSession, cache_engine: AsyncEngine
    ) -> None:
        """Rule 96, on the one decode this path adds: the fallback resolves toward keeping."""
        await self._seed(cache_engine)
        first, _ = await self._scan_under(session, cache_engine, self._tv())
        await session.execute(
            update(SeasonPruneEvidence)
            .where(SeasonPruneEvidence.snapshot_id == first.id)
            .values(payload_json='{"title": "half a bundle"}')
        )
        await session.commit()

        with pytest.raises(routes._SeasonEvidenceMissingError) as caught:
            await routes._season_bundles(session, snapshot_id=first.id)

        assert caught.value.kind is SimStale.SEASONS_NOT_RECORDED
