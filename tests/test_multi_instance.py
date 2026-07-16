# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multi-instance scanning, and the cache/state separation.

Both are regression tests for bugs that shipped into a working, green, fully-tested
build and were caught only by pointing it at a real library. Neither was visible from
unit tests, because both were failures of *scope* rather than of logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reaper.clock import from_epoch, utcnow
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.models import Instance, InstanceKind
from reaper.db.session import create_engine, create_session_factory
from reaper.engine import identity
from reaper.services import scan_runner
from reaper.services.snapshot import RawItem, _raw_items

ADDED = from_epoch("1700000000")


def _plex_index(*items: tuple[int, str]) -> identity.PlexIndex:
    """A Plex movie index from ``(rating_key, title)`` pairs -- no external ids, so the
    join here exercises the title+year backstop (the multi-instance behaviour predates
    id matching and must survive it unchanged)."""
    return identity.PlexIndex.build(
        [
            identity.PlexItem(rating_key=rk, title=title, year=None, added_at=ADDED)
            for rk, title in items
        ]
    )


class TestEveryInstanceIsScanned:
    """A separate 4K Radarr alongside the HD one is a common setup.

    An early version of the scan route did ``next(r for r in rows if RADARR)`` and
    scanned whichever instance happened to come first -- a small fraction of the
    library -- while reporting a clean, confident, non-degraded result.

    A silently-partial scan is worse than a failed one: the owner reviews a candidate
    list they believe is complete, and nothing anywhere says otherwise.
    """

    def test_items_from_two_instances_are_distinct_rows(self) -> None:
        """The same film in the HD and 4K instances is two rows -- correctly, because
        they are two distinct FILES on disk, and deleting one must not delete the other."""
        movie = {
            "id": 42,
            "title": "Example Movie",
            "hasFile": True,
            "sizeOnDisk": 8_000_000_000,
            "imdbId": "tt0000001",
            "tmdbId": 1001,
        }
        plex = _plex_index((999, "Example Movie"))

        hd = _raw_items([movie], plex, instance_id=1)
        uhd = _raw_items([movie], plex, instance_id=2)

        assert hd[0].media_key != uhd[0].media_key
        assert hd[0].media_key == "radarr:1:42"
        assert uhd[0].media_key == "radarr:2:42"

    def test_the_media_key_comes_from_the_arr_not_from_plex(self) -> None:
        """Plex rating keys are NOT stable across library rebuilds or agent migrations.
        Keying candidates on one would silently orphan every grace clock the next time
        the owner rebuilt their library."""
        movie = {"id": 42, "title": "Example Movie", "hasFile": True, "sizeOnDisk": 1}
        plex = _plex_index((999, "Example Movie"))

        item = _raw_items([movie], plex, instance_id=1)[0]

        assert "999" not in item.media_key
        assert item.media_key.startswith("radarr:1:")

    def test_a_movie_with_no_file_is_skipped(self) -> None:
        """Radarr tracks films it WANTS as well as films it has. There is nothing to
        reclaim from a film that was never downloaded."""
        wanted = {"id": 1, "title": "Not Yet Downloaded", "hasFile": False, "sizeOnDisk": 0}

        assert _raw_items([wanted], _plex_index(), instance_id=1) == []

    def test_a_movie_plex_has_not_matched_still_appears(self) -> None:
        """It must not vanish. It appears with no rating key, which makes its dormancy
        Unknown -- and Unknown protects. Dropping it silently would be worse: the owner
        would never learn that Plex has failed to match it."""
        movie = {"id": 7, "title": "Unmatched By Plex", "hasFile": True, "sizeOnDisk": 1}

        items = _raw_items([movie], _plex_index(), instance_id=1)

        assert len(items) == 1
        assert items[0].plex_rating_key is None
        assert items[0].added_at is None


class TestCachesLiveInTheirOwnDatabase:
    """Tautulli's history and the IMDb dataset are large and take minutes to rebuild.
    ``reaper.db`` is small, precious, and gets migrated, reset and restored.

    Keeping them in one file means a schema reset destroys hours of sync -- which is
    exactly what happened during development, twice, before they were separated.
    """

    def test_the_two_databases_are_different_files(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]

        assert settings.database_url != settings.cache_database_url
        assert "reaper.db" in settings.database_url
        assert "cache.db" in settings.cache_database_url

    def test_alembic_never_migrates_a_cache_table(self) -> None:
        """A cache table once leaked into a migration -- autogenerate saw it in reaper.db
        and helpfully proposed to create it. Migration 2 then failed on a fresh database,
        because the table it wanted to create no longer belonged there at all."""
        import re

        versions = Path(__file__).parent.parent / "alembic" / "versions"
        cache_tables = ("imdb_rating", "imdb_dataset_sync", "watch_event", "protection_list")

        for migration in versions.glob("*.py"):
            body = migration.read_text()
            for table in cache_tables:
                assert not re.search(rf"op\.create_table\(\s*['\"]{table}['\"]", body), (
                    f"{migration.name} migrates the cache table {table!r}. Cache tables "
                    "live in cache.db and are created by raw DDL -- they must never "
                    "appear in a migration."
                )


class TestRawItemShape:
    def test_size_and_ids_survive(self) -> None:
        movie = {
            "id": 1,
            "title": "Another Movie",
            "hasFile": True,
            "sizeOnDisk": 12_000_000_000,
            "imdbId": "tt0000002",
            "tmdbId": 1002,
        }
        plex = _plex_index((5, "Another Movie"))

        item: RawItem = _raw_items([movie], plex, instance_id=3)[0]

        assert item.size_bytes == 12_000_000_000
        assert item.imdb_id == "tt0000002"
        assert item.tmdb_id == 1002
        assert item.plex_rating_key == 5
        assert item.added_at is not None
        assert item.added_at.tzinfo is not None  # aware, always


class TestScanClientsCarryTheTlsChoice:
    """``build_sources`` hands every client its own instance's ``verify_tls``. A dropped
    flag here would quietly ignore the operator's per-service certificate choice for a
    whole scan: either failing against the self-signed server they explicitly allowed,
    or, in the other direction, skipping verification somewhere they never asked."""

    async def test_build_sources_passes_each_rows_own_choice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
        engine = create_engine(settings)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        box = SecretBox("test-key")

        def row(kind: InstanceKind, name: str, url: str, verify: bool) -> Instance:
            return Instance(
                kind=kind,
                name=name,
                base_url=url,
                api_key_enc=box.encrypt("k"),
                enabled=True,
                verify_tls=verify,
                created_at=utcnow(),
            )

        async with factory() as session:
            session.add_all(
                [
                    row(InstanceKind.RADARR, "HD", "https://movies.local", False),
                    row(InstanceKind.SONARR, "HD", "https://tv.local", True),
                    row(InstanceKind.TAUTULLI, "Main", "https://history.local", True),
                    row(InstanceKind.SEERR, "Main", "https://requests.local", False),
                ]
            )
            await session.commit()

        seen: dict[str, object] = {}

        class FakeClient:
            def __init__(self, base_url: str, *args: object, **kwargs: object) -> None:
                seen[base_url] = kwargs.get("verify")

        for name in ("RadarrClient", "SonarrClient", "TautulliClient", "SeerrClient"):
            monkeypatch.setattr(scan_runner, name, FakeClient)

        try:
            await scan_runner.build_sources(factory, settings, box)
        finally:
            await engine.dispose()

        assert seen == {
            "https://movies.local": False,
            "https://tv.local": True,
            "https://history.local": True,
            "https://requests.local": False,
        }
