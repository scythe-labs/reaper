# SPDX-License-Identifier: AGPL-3.0-or-later
"""Profile persistence: the caps/grace settings a run obeys.

The fiddly bit is the foreign key -- a profile references a policy row, but a fresh
install has never saved one (it runs on the in-code default). So saving a profile has to
persist the default policy first. These prove that works, is idempotent, and that the
domain's invariants hold through the service.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Policy as PolicyModel
from reaper.db.models import Profile
from reaper.db.session import create_engine, create_session_factory
from reaper.engine.policy import DEFAULT_MOVIE_POLICY, ProfileSettings
from reaper.services.profiles import (
    active_policy,
    active_profile_settings,
    save_profile_settings,
)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


class TestActiveProfileSettings:
    async def test_defaults_before_anything_is_saved(self, session: AsyncSession) -> None:
        """A never-configured install reads the cautious built-ins -- it is not thereby
        permitted to do more than the defaults allow."""
        settings = await active_profile_settings(session)
        assert settings.max_items_per_run == 10
        assert settings.caps_enabled is True

    async def test_a_saved_profile_is_what_loads(self, session: AsyncSession) -> None:
        await save_profile_settings(session, ProfileSettings(max_items_per_run=25, grace_days=30))
        loaded = await active_profile_settings(session)
        assert loaded.max_items_per_run == 25
        assert loaded.grace_days == 30

    async def test_a_legacy_field_in_stored_settings_does_not_crash_the_read(
        self, session: AsyncSession
    ) -> None:
        """A profile written by an older build can carry a field this model has since
        removed. Under extra='forbid' that would crash EVERY read -- including the settings
        page an operator would use to fix it -- so the loader drops unknown keys and keeps
        the operator's real settings, defaulting any new field to its cautious value. This
        is the upgrade path for the removed 'require_approval' setting."""
        # Create the profile normally (sets up the policy FK), then overwrite its blob with
        # one an older build would have written: a departed 'require_approval', no
        # 'caps_enabled'.
        await save_profile_settings(session, ProfileSettings(max_items_per_run=25, grace_days=30))
        row = (
            await session.execute(select(Profile).order_by(Profile.id.asc()).limit(1))
        ).scalar_one()
        row.settings_json = (
            '{"max_items_per_run":25,"max_bytes_per_run":500000000000,'
            '"max_items_per_30d":100,"max_bytes_per_30d":2000000000000,'
            '"grace_days":30,"require_approval":false,"max_unmeasured_per_run":0}'
        )
        await session.flush()

        loaded = await active_profile_settings(session)
        assert loaded.max_items_per_run == 25  # operator's real setting preserved
        assert loaded.grace_days == 30  # preserved
        assert loaded.caps_enabled is True  # new field falls back to the cautious default

    async def test_an_unreadable_settings_blob_degrades_to_cautious_defaults(
        self, session: AsyncSession
    ) -> None:
        """A blob that is not repairable (bad JSON, or a value out of range) must not crash
        the read path either. It degrades to the built-in cautious defaults -- caps on."""
        await save_profile_settings(session, ProfileSettings(max_items_per_run=25))
        row = (
            await session.execute(select(Profile).order_by(Profile.id.asc()).limit(1))
        ).scalar_one()
        row.settings_json = "not json at all"
        await session.flush()

        loaded = await active_profile_settings(session)
        assert loaded.max_items_per_run == 10  # cautious default
        assert loaded.caps_enabled is True


class TestSavingCreatesTheBackingPolicyRow:
    async def test_the_first_save_persists_the_default_policy(self, session: AsyncSession) -> None:
        """The profile's FK needs a policy row; a fresh install has none, so saving must
        create one from the in-code default."""
        assert (await session.execute(select(func.count()).select_from(PolicyModel))).scalar() == 0

        await save_profile_settings(session, ProfileSettings())

        assert (await session.execute(select(func.count()).select_from(PolicyModel))).scalar() == 1
        profile = (await session.execute(select(Profile))).scalar_one()
        assert profile.active_policy_id is not None

    async def test_saving_twice_does_not_fork_the_profile(self, session: AsyncSession) -> None:
        await save_profile_settings(session, ProfileSettings(max_items_per_run=5))
        await save_profile_settings(session, ProfileSettings(max_items_per_run=7))

        count = (await session.execute(select(func.count()).select_from(Profile))).scalar()
        assert count == 1  # updated in place, not duplicated
        assert (await active_profile_settings(session)).max_items_per_run == 7

    async def test_a_saved_profile_ships_disabled(self, session: AsyncSession) -> None:
        """A profile that could act the moment it is created is how a starter template
        deletes a library. Saving caps must never enable acting."""
        await save_profile_settings(session, ProfileSettings())
        profile = (await session.execute(select(Profile))).scalar_one()
        assert profile.enabled is False


async def _store_policy(session: AsyncSession, body_json: str) -> None:
    """Put a raw body straight into the table, bypassing every in-app writer.

    Only an externally edited, truncated or restored row can hold something
    ``model_dump_json`` would never produce, which is exactly the row these tests are about.
    """
    session.add(
        PolicyModel(
            policy_hash="h",
            body_json=body_json,
            media_type="movie",
            name="stored",
            created_at=utcnow(),
        )
    )
    await session.flush()


class TestACorruptPolicyBodyNeverRaises:
    """``active_policy`` is read by the editor, the simulator and the scan alike, so an
    unreadable stored body has to fall back -- a raise takes out the page that fixes it.

    The two escapes this pins were both outside the handler: malformed JSON reaches the
    ``ValidationError`` branch and used to blow up in ``json.loads`` there, and valid JSON
    that is not an object used to reach ``rebalance`` and raise ``AttributeError``.
    """

    @pytest.mark.parametrize(
        "body_json",
        [
            "",
            "not json at all",
            '{"media_type": "movie", "condemn_at": 70,',  # truncated mid-write
            '["signals", "as", "a", "list"]',
            "42",
            "null",
            '"a bare string"',
            '{"signals": [7]}',
            '{"signals": "text"}',
        ],
        ids=[
            "empty",
            "not-json",
            "truncated",
            "json-array",
            "json-number",
            "json-null",
            "json-string",
            "signal-not-an-object",
            "signals-not-a-list",
        ],
    )
    async def test_it_falls_back_to_the_shipped_default(
        self, session: AsyncSession, body_json: str
    ) -> None:
        await _store_policy(session, body_json)

        active = await active_policy(session, "movie")

        assert active.fell_back is True
        assert active.rescaled is False
        assert active.repaired is True  # the scan degrades on it
        assert active.body == DEFAULT_MOVIE_POLICY
        assert active.name == "default"

    async def test_a_body_that_only_misses_the_budget_is_still_rescaled(
        self, session: AsyncSession
    ) -> None:
        """The fallback must not swallow the repairable case: a body written before removal
        weights had to total 100 keeps the operator's own tuning."""
        legacy = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
        for signal in legacy["signals"]:
            signal["weight"] *= 2

        await _store_policy(session, json.dumps(legacy))

        active = await active_policy(session, "movie")

        assert active.fell_back is False
        assert active.rescaled is True
        assert active.name == "stored"
        assert sum(s.weight for s in active.body.signals) == 100


class TestInvariantsHoldThroughTheService:
    async def test_a_run_cap_above_the_rolling_cap_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(ValueError, match="rolling cap"):
            ProfileSettings(max_items_per_run=200, max_items_per_30d=100)

    async def test_grace_under_a_week_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(ValueError):
            ProfileSettings(grace_days=3)
