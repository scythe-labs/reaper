# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config layering and environment seeding."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from reaper.config import RuntimeSafety, Settings, parse_instance_seeds
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.models import Instance, InstanceKind
from reaper.db.session import create_engine, create_session_factory
from reaper.services.seeding import seed_instances


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")
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

    def test_the_skip_names_the_variable_that_is_missing(self) -> None:
        """A half-configured slot is the one case where the operator has clearly tried to
        set something up, and the skip used to say nothing at all. They got no instance and
        no reason (#658)."""
        with capture_logs() as logs:
            parse_instance_seeds({"REAPER_RADARR_HD_URL": "https://radarr.example.net"})

        assert [entry["event"] for entry in logs] == ["seed.incomplete"]
        assert "REAPER_RADARR_HD_API_KEY" in logs[0]["detail"]
        assert "REAPER_RADARR_HD_URL" not in logs[0]["detail"]

    def test_a_complete_group_says_nothing(self) -> None:
        """The other arm, so the assertion above cannot pass on a warning that always
        fires (rule 118)."""
        with capture_logs() as logs:
            assert parse_instance_seeds(ENV)

        assert logs == []

    def test_two_spellings_of_one_slot_are_one_instance(self) -> None:
        """``_SEED_PATTERN`` is IGNORECASE, so the grouping key has to absorb case the way
        ``kind`` and ``field`` already do. Grouped as typed, these are two half-configured
        instances and both are skipped silently: the operator gets nothing (#658)."""
        seeds = parse_instance_seeds(
            {
                "REAPER_SONARR_Main_URL": "https://sonarr.example.net",
                "REAPER_SONARR_main_API_KEY": "key-main",
            }
        )

        assert len(seeds) == 1
        assert seeds[0].api_key.get_secret_value() == "key-main"

    def test_an_environment_override_spelled_differently_wins_over_the_file(self) -> None:
        """The reachable shape, and the reason the fold matters. ``configured_env`` merges
        the dotenv file and ``os.environ`` on the exact string and puts the environment
        last, so a compose ``environment:`` block and a ``.env`` file that disagree about
        the slot's case arrive as two keys. Ungrouped, the override was dropped and the
        file's URL was seeded."""
        merged = {
            "REAPER_SONARR_Main_URL": "https://from-the-file.example.net",
            "REAPER_SONARR_Main_API_KEY": "key-from-the-file",
            "REAPER_SONARR_MAIN_URL": "https://from-the-environment.example.net",
        }

        seeds = parse_instance_seeds(merged)

        assert len(seeds) == 1
        assert seeds[0].base_url == "https://from-the-environment.example.net"
        assert seeds[0].api_key.get_secret_value() == "key-from-the-file"

    def test_the_display_name_of_a_folded_slot_is_the_folded_one(self) -> None:
        """Folding is upward, and the slot is the display name when no ``_NAME`` is given,
        so a lowercase slot now reads uppercase. Recorded rather than left to be noticed:
        every example Reaper ships spells the slot in caps, and two spellings of one slot
        have no third answer to pick between them."""
        seeds = parse_instance_seeds(
            {
                "REAPER_SONARR_main_URL": "https://sonarr.example.net",
                "REAPER_SONARR_main_API_KEY": "key-main",
            }
        )

        assert [s.name for s in seeds] == ["MAIN"]

    def test_an_explicit_name_is_taken_exactly_as_typed(self) -> None:
        """The fold reaches the grouping key, not the operator's chosen name."""
        seeds = parse_instance_seeds(
            {
                "REAPER_SONARR_main_URL": "https://sonarr.example.net",
                "REAPER_SONARR_main_API_KEY": "key-main",
                "REAPER_SONARR_MAIN_NAME": "Main library",
            }
        )

        assert [s.name for s in seeds] == ["Main library"]

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

    async def test_a_second_tautulli_seed_is_skipped_not_imported(
        self, session: AsyncSession
    ) -> None:
        """Tautulli is a singleton even from the environment (it mirrors one Plex, and
        Reaper connects to one Plex), matching the UI. A second declared Tautulli is
        skipped, never seeded as a row the scan would silently ignore."""
        box = SecretBox("test-key")
        env = {
            "REAPER_TAUTULLI_MAIN_URL": "https://t1.example.net",
            "REAPER_TAUTULLI_MAIN_API_KEY": "key-1",
            "REAPER_TAUTULLI_BACKUP_URL": "https://t2.example.net",
            "REAPER_TAUTULLI_BACKUP_API_KEY": "key-2",
        }
        imported, skipped = await seed_instances(session, parse_instance_seeds(env), box)

        assert imported == 1 and skipped == 1
        tautullis = (
            (await session.execute(select(Instance).where(Instance.kind == InstanceKind.TAUTULLI)))
            .scalars()
            .all()
        )
        assert len(tautullis) == 1

    async def test_the_same_instance_declared_twice_is_seeded_once(
        self, session: AsyncSession
    ) -> None:
        """Two seeds naming the same (kind, name) are one instance, not two rows.

        The duplicate check is a query, and nothing is flushed until the batch ends, so
        both reads used to answer "not there yet" and both rows got added. The singleton
        path has always had an in-batch set; this is the same guard everywhere else.
        """
        box = SecretBox("test-key")
        env = {
            "REAPER_SONARR_HD_URL": "https://s1.example.net",
            "REAPER_SONARR_HD_API_KEY": "key-1",
        }
        seeds = parse_instance_seeds(env)
        # The same instance declared twice inside ONE batch, before anything is flushed.
        imported, skipped = await seed_instances(session, [*seeds, *seeds], box)

        assert (imported, skipped) == (1, 1)
        rows = (
            (await session.execute(select(Instance).where(Instance.kind == InstanceKind.SONARR)))
            .scalars()
            .all()
        )
        assert len(rows) == 1

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
