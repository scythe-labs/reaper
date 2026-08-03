# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scan's required-instances gate.

A scan judges dormancy, and dormancy is watch history, so Tautulli is required.
The library side accepts either kind: a movie-only deployment (Radarr, no Sonarr)
and a TV-only deployment (Sonarr, no Radarr) are both real installs, and neither
may be told it cannot scan.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings, generate_secret_key
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.models import Instance, InstanceKind
from reaper.db.session import create_engine, create_session_factory
from reaper.services.scan_runner import ScanConfigError, build_sources


@pytest.fixture
async def env(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], Settings, SecretBox]]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine), settings, SecretBox(generate_secret_key())
    await engine.dispose()


async def _add(
    factory: async_sessionmaker[AsyncSession], box: SecretBox, kind: InstanceKind, name: str
) -> None:
    async with factory() as session:
        session.add(
            Instance(
                kind=kind,
                name=name,
                base_url=f"http://{name}",
                api_key_enc=box.encrypt("k"),
                enabled=True,
                created_at=utcnow(),
            )
        )
        await session.commit()


class TestBuildSourcesGate:
    async def test_sonarr_plus_tautulli_is_enough(
        self, env: tuple[async_sessionmaker[AsyncSession], Settings, SecretBox]
    ) -> None:
        factory, settings, box = env
        await _add(factory, box, InstanceKind.SONARR, "tv")
        await _add(factory, box, InstanceKind.TAUTULLI, "t")

        async with AsyncExitStack() as stack:
            radarrs, sonarrs, tautulli, seerrs, plex = await build_sources(
                factory, settings, box, stack=stack
            )
            assert radarrs == []
            assert len(sonarrs) == 1
            assert tautulli is not None
            assert seerrs == [] and plex is None

    async def test_radarr_plus_tautulli_is_still_enough(
        self, env: tuple[async_sessionmaker[AsyncSession], Settings, SecretBox]
    ) -> None:
        factory, settings, box = env
        await _add(factory, box, InstanceKind.RADARR, "hd")
        await _add(factory, box, InstanceKind.TAUTULLI, "t")

        async with AsyncExitStack() as stack:
            radarrs, sonarrs, _tautulli, _seerr, _plex = await build_sources(
                factory, settings, box, stack=stack
            )
            assert len(radarrs) == 1
            assert sonarrs == []

    async def test_every_seerr_is_built_not_just_the_first(
        self, env: tuple[async_sessionmaker[AsyncSession], Settings, SecretBox]
    ) -> None:
        """Seerr is multi-instance: two portals must both be built into the list, so the
        scan reads every request, not just the first portal's."""
        factory, settings, box = env
        await _add(factory, box, InstanceKind.RADARR, "hd")
        await _add(factory, box, InstanceKind.TAUTULLI, "t")
        await _add(factory, box, InstanceKind.SEERR, "portal-one")
        await _add(factory, box, InstanceKind.SEERR, "portal-two")

        async with AsyncExitStack() as stack:
            _radarrs, _sonarrs, _tautulli, seerrs, _plex = await build_sources(
                factory, settings, box, stack=stack
            )
            assert len(seerrs) == 2

    async def test_no_library_source_is_refused(
        self, env: tuple[async_sessionmaker[AsyncSession], Settings, SecretBox]
    ) -> None:
        factory, settings, box = env
        await _add(factory, box, InstanceKind.TAUTULLI, "t")

        with pytest.raises(ScanConfigError):
            async with AsyncExitStack() as stack:
                await build_sources(factory, settings, box, stack=stack)

    async def test_no_tautulli_is_refused(
        self, env: tuple[async_sessionmaker[AsyncSession], Settings, SecretBox]
    ) -> None:
        """Without watch history every item's dormancy is Unknown, and Unknown never
        condemns -- a scan without Tautulli would judge nothing. Refuse it plainly."""
        factory, settings, box = env
        await _add(factory, box, InstanceKind.SONARR, "tv")

        with pytest.raises(ScanConfigError):
            async with AsyncExitStack() as stack:
                await build_sources(factory, settings, box, stack=stack)
