# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config layering and environment seeding."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.config import RuntimeSafety, Settings, parse_instance_seeds
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.models import Instance, InstanceKind
from reaper.db.session import create_engine, create_session_factory
from reaper.services.seeding import seed_instances


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


ENV = {
    "REAPER_SONARR_HD_URL": "https://sonarr.example.net/",
    "REAPER_SONARR_HD_API_KEY": "key-hd",
    "REAPER_SONARR_4K_URL": "https://sonarr-4k.example.net",
    "REAPER_SONARR_4K_API_KEY": "key-4k",
    "REAPER_TAUTULLI_MAIN_URL": "https://tautulli.example.net",
    "REAPER_TAUTULLI_MAIN_API_KEY": "key-taut",
    "UNRELATED_VAR": "ignored",
}


class TestSeedParsing:
    def test_parses_multiple_instances_of_the_same_kind(self) -> None:
        seeds = parse_instance_seeds(ENV)
        sonarr = [s for s in seeds if s.kind == "sonarr"]
        assert {s.name for s in sonarr} == {"HD", "4K"}

    def test_trailing_slash_is_stripped(self) -> None:
        """Every client joins paths onto base_url; a trailing slash yields '//api/v3'."""
        seed = next(s for s in parse_instance_seeds(ENV) if s.name == "HD")
        assert seed.base_url == "https://sonarr.example.net"

    def test_a_group_missing_its_api_key_is_skipped_not_half_imported(self) -> None:
        seeds = parse_instance_seeds({"REAPER_RADARR_HD_URL": "https://radarr.example.net"})
        assert seeds == []

    def test_unrelated_env_vars_are_ignored(self) -> None:
        kinds = {s.kind for s in parse_instance_seeds(ENV)}
        assert kinds <= {"sonarr", "radarr", "tautulli", "seerr"}


class TestSeeding:
    async def test_imports_and_encrypts(self, session: AsyncSession) -> None:
        box = SecretBox("test-key")
        imported, skipped = await seed_instances(session, parse_instance_seeds(ENV), box)

        assert (imported, skipped) == (3, 0)

        row = await session.scalar(
            select(Instance).where(Instance.kind == InstanceKind.SONARR, Instance.name == "HD")
        )
        assert row is not None
        # Stored encrypted, and decrypts back to the original.
        assert "key-hd" not in row.api_key_enc
        assert box.decrypt(row.api_key_enc) == "key-hd"

    async def test_seeding_is_idempotent(self, session: AsyncSession) -> None:
        box = SecretBox("test-key")
        seeds = parse_instance_seeds(ENV)

        await seed_instances(session, seeds, box)
        imported, skipped = await seed_instances(session, seeds, box)

        assert (imported, skipped) == (0, 3)
        assert len((await session.execute(select(Instance))).scalars().all()) == 3

    async def test_env_never_overwrites_a_key_changed_in_the_ui(
        self, session: AsyncSession
    ) -> None:
        """The database is the source of truth. If you rotate a key in the UI and
        forget to update .env, the stale env value must not silently clobber it."""
        box = SecretBox("test-key")
        await seed_instances(session, parse_instance_seeds(ENV), box)

        row = await session.scalar(
            select(Instance).where(Instance.kind == InstanceKind.SONARR, Instance.name == "HD")
        )
        assert row is not None
        row.api_key_enc = box.encrypt("rotated-in-ui")
        await session.flush()

        await seed_instances(session, parse_instance_seeds(ENV), box)

        refreshed = await session.scalar(
            select(Instance).where(Instance.kind == InstanceKind.SONARR, Instance.name == "HD")
        )
        assert refreshed is not None
        assert box.decrypt(refreshed.api_key_enc) == "rotated-in-ui"


class TestRuntimeSafety:
    def test_the_toggle_decides(self) -> None:
        assert RuntimeSafety(destructive_enabled=True).destructive_allowed is True
        assert RuntimeSafety(destructive_enabled=False).destructive_allowed is False

    def test_it_ships_read_only_by_default(self) -> None:
        """The default must be safe: an unconfigured RuntimeSafety cannot delete."""
        assert RuntimeSafety().destructive_allowed is False
        assert "turned off" in (RuntimeSafety().why_blocked() or "")

    def test_no_explanation_when_permitted(self) -> None:
        assert RuntimeSafety(destructive_enabled=True).why_blocked() is None
