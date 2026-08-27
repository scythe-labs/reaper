# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multi-instance scanning, and the cache/state separation.

Both groups of tests guard against bugs that ordinary unit tests cannot see, because both
are failures of *scope* rather than of logic: the arithmetic is right, but it runs over the
wrong instance or the wrong database.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from structlog.testing import capture_logs

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
    """A Plex movie index from ``(rating_key, title)`` pairs. It carries no external ids,
    so the join exercises the title-and-year backstop match, which the multi-instance
    behavior must keep working alongside id matching."""
    return identity.PlexIndex.build(
        [
            identity.PlexItem(rating_key=rk, title=title, year=None, added_at=ADDED)
            for rk, title in items
        ]
    )


class TestEveryInstanceIsScanned:
    """A separate 4K Radarr alongside an HD one is a common setup, and every instance of
    a kind must be scanned, not just one.

    Picking the first matching row instead (``next(r for r in rows if RADARR)``) would
    scan only a small fraction of the library, while still reporting a clean, confident,
    non-degraded result.

    A silently partial scan is worse than a failed one. The owner would review a
    candidate list they believe is complete, with nothing anywhere saying otherwise.
    """

    def test_items_from_two_instances_are_distinct_rows(self) -> None:
        """The same film in the HD and 4K instances is two separate rows, because they
        are two distinct files on disk, and deleting one must not delete the other."""
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
        """Plex rating keys change across a library rebuild or an agent migration.
        Keying candidates on one would silently orphan every grace clock the next time
        the owner rebuilt their library."""
        movie = {"id": 42, "title": "Example Movie", "hasFile": True, "sizeOnDisk": 1}
        plex = _plex_index((999, "Example Movie"))

        item = _raw_items([movie], plex, instance_id=1)[0]

        assert "999" not in item.media_key
        assert item.media_key.startswith("radarr:1:")

    def test_a_movie_with_no_file_is_skipped(self) -> None:
        """Radarr tracks films it wants as well as films it has. There is nothing to
        reclaim from a film that was never downloaded."""
        wanted = {"id": 1, "title": "Not Yet Downloaded", "hasFile": False, "sizeOnDisk": 0}

        assert _raw_items([wanted], _plex_index(), instance_id=1) == []

    def test_a_movie_plex_has_not_matched_still_appears(self) -> None:
        """This item must still appear, even though Plex has not matched it. It appears
        with no rating key, which makes its dormancy Unknown. An Unknown dormancy blocks
        both dormancy gates, so the item abstains and is kept as a blocked hold
        (``gates._blocked``), not as a PROTECT verdict. Dropping it silently would be
        worse, since the owner would never learn that Plex failed to match it.
        """
        movie = {"id": 7, "title": "Unmatched By Plex", "hasFile": True, "sizeOnDisk": 1}

        items = _raw_items([movie], _plex_index(), instance_id=1)

        assert len(items) == 1
        assert items[0].plex_rating_key is None
        assert items[0].added_at is None

    def test_a_movie_plex_has_not_matched_is_warned(self) -> None:
        """When the owner asks "why isn't this in review," the answer must be in the log,
        not only on the row's why panel. This item appears, but only as kept-to-be-safe,
        so a warning names it and says Plex could not match it.
        """
        movie = {"id": 7, "title": "Unmatched By Plex", "hasFile": True, "sizeOnDisk": 1}

        with capture_logs() as logs:
            _raw_items([movie], _plex_index(), instance_id=1)

        warned = [e for e in logs if e["event"] == "scan.plex_unmatched"]
        assert len(warned) == 1
        assert warned[0]["log_level"] == "warning"
        assert warned[0]["media_type"] == "movie"
        assert warned[0]["match_status"] == "unmatched"

    def test_a_matched_movie_is_not_warned(self) -> None:
        """A clean match stays quiet. The warning must fire only on a real match failure."""
        movie = {"id": 42, "title": "Example Movie", "hasFile": True, "sizeOnDisk": 1}

        with capture_logs() as logs:
            _raw_items([movie], _plex_index((999, "Example Movie")), instance_id=1)

        assert [e for e in logs if e["event"] == "scan.plex_unmatched"] == []


class TestCachesLiveInTheirOwnDatabase:
    """Tautulli's history and the IMDb dataset are large and take minutes to rebuild.
    ``reaper.db`` is small, and it gets migrated, reset, and restored often.

    Keeping them in one file would mean a schema reset destroys hours of synced data,
    which is why the two live in separate database files.
    """

    def test_the_two_databases_are_different_files(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="k")

        assert settings.database_url != settings.cache_database_url
        assert "reaper.db" in settings.database_url
        assert "cache.db" in settings.cache_database_url

    def test_alembic_never_migrates_a_cache_table(self) -> None:
        """Autogenerate can see a cache table in reaper.db and propose creating it in a
        migration. That would fail against a fresh database, because the table it wants
        to create does not belong in that database at all.
        """
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
    whole scan. That means either failing against a self-signed server they explicitly
    allowed, or skipping verification somewhere they never asked for it.
    """

    async def test_build_sources_passes_each_rows_own_choice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="test-key")
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

            async def __aenter__(self) -> object:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

        for name in ("RadarrClient", "SonarrClient", "TautulliClient", "SeerrClient"):
            monkeypatch.setattr(scan_runner, name, FakeClient)

        try:
            async with AsyncExitStack() as stack:
                await scan_runner.build_sources(factory, settings, box, stack=stack)
        finally:
            await engine.dispose()

        assert seen == {
            "https://movies.local": False,
            "https://tv.local": True,
            "https://history.local": True,
            "https://requests.local": False,
        }
