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
from reaper.engine.reason import Reason
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

    def test_a_missing_url_says_which_variable_to_set(self) -> None:
        """A slot with no URL is the one case where the operator has clearly tried to set
        something up and Reaper never asked for the gap. The skip used to say nothing at
        all, so they got no instance and no reason (#658)."""
        with capture_logs() as logs:
            parse_instance_seeds({"REAPER_RADARR_HD_API_KEY": "key-hd"})

        assert [entry["event"] for entry in logs] == ["seed.incomplete"]
        assert "REAPER_RADARR_HD_URL" in logs[0]["detail"]

    def test_a_missing_api_key_stays_silent(self) -> None:
        """``seed.complete`` tells the operator the REAPER_*_API_KEY variables can be
        removed once the import lands, so a URL left behind with no key is the steady state
        of an install that worked. Warning there would call a running service broken on
        every boot, and seeding runs on every start."""
        with capture_logs() as logs:
            assert parse_instance_seeds({"REAPER_RADARR_HD_URL": "https://radarr.test"}) == []

        assert logs == []

    def test_a_complete_group_says_nothing(self) -> None:
        """The other arm, so the warning above cannot pass by always firing (rule 118)."""
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

    def test_the_later_spelling_of_a_field_supplies_its_value(self) -> None:
        """How an environment variable reaches this function after the dotenv file:
        ``configured_env`` merges the files first, so the environment's key is the later
        one. Pinned as the ordering rule it is, because this function is handed one mapping
        and cannot see where a key came from."""
        seeds = parse_instance_seeds(
            {
                "REAPER_SONARR_Main_URL": "https://first.example.net",
                "REAPER_SONARR_Main_API_KEY": "key-main",
                "REAPER_SONARR_MAIN_URL": "https://second.example.net",
            }
        )

        assert [s.base_url for s in seeds] == ["https://second.example.net"]

    def test_the_display_name_keeps_the_spelling_as_typed(self) -> None:
        """Folding the grouping key must not rename an instance an earlier boot already
        seeded: ``seed_instances`` matches ``Instance.name`` exactly, so a slot stored as
        ``main`` and re-read as ``MAIN`` imports a second row for one server."""
        seeds = parse_instance_seeds(
            {
                "REAPER_SONARR_main_URL": "https://sonarr.example.net",
                "REAPER_SONARR_main_API_KEY": "key-main",
            }
        )

        assert [s.name for s in seeds] == ["main"]

    def test_the_first_spelling_is_the_one_kept(self) -> None:
        """Two spellings have no third answer, so the earlier key wins, which is the one
        the file wrote before an environment override was added beside it."""
        seeds = parse_instance_seeds(
            {
                "REAPER_SONARR_Main_URL": "https://sonarr.example.net",
                "REAPER_SONARR_MAIN_API_KEY": "key-main",
            }
        )

        assert [s.name for s in seeds] == ["Main"]

    def test_an_explicit_name_is_taken_exactly_as_typed(self) -> None:
        """The fold reaches the grouping key, never the operator's chosen name."""
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

    async def test_re_reading_a_lowercase_slot_does_not_import_a_second_row(
        self, session: AsyncSession
    ) -> None:
        """The upgrade path, and why the display name keeps the spelling as typed.

        ``seed_instances`` matches ``Instance.name`` exactly and the column is
        BINARY-collated, so an instance seeded as ``hd`` and re-read as ``HD`` reads as a
        new instance. Two rows point at one server, every title on it is enumerated twice,
        and the second row carries none of the operator's per-instance settings.
        """
        box = SecretBox("test-key")
        env = {
            "REAPER_SONARR_hd_URL": "https://sonarr.example.net",
            "REAPER_SONARR_hd_API_KEY": "key-hd",
        }

        await seed_instances(session, parse_instance_seeds(env), box)
        imported, skipped = await seed_instances(session, parse_instance_seeds(env), box)

        assert (imported, skipped) == (0, 1)
        rows = (await session.execute(select(Instance))).scalars().all()
        assert [r.name for r in rows] == ["hd"]

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
        assert RuntimeSafety().why_blocked() == Reason("error.safety.deletion_off")

    def test_no_explanation_when_permitted(self) -> None:
        assert RuntimeSafety(destructive_enabled=True).why_blocked() is None

    def test_recovery_mode_names_the_switch_that_cannot_clear_it(self) -> None:
        """Recovery holds deletion off however the stored switch reads, so it takes the
        reason ahead of the plain "deletion is off" one -- naming the other switch would
        send the operator to a control the arm route also refuses."""
        blocked = RuntimeSafety(destructive_enabled=True, recovery_mode=True)
        assert blocked.why_blocked() == Reason("error.safety.recovery_mode_active")
