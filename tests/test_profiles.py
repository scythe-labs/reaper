# SPDX-License-Identifier: AGPL-3.0-or-later
"""Profile persistence: the caps/grace settings a run obeys.

The fiddly bit is the foreign key -- a profile references a policy row, but a fresh
install has never saved one (it runs on the in-code default). So saving a profile has to
persist the default policy first. These prove that works, is idempotent, and that the
domain's invariants hold through the service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Policy as PolicyModel
from reaper.db.models import Profile
from reaper.db.session import create_engine, create_session_factory
from reaper.engine.policy import ProfileSettings
from reaper.services.profiles import active_profile_settings, save_profile_settings


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
        assert settings.require_approval is True

    async def test_a_saved_profile_is_what_loads(self, session: AsyncSession) -> None:
        await save_profile_settings(session, ProfileSettings(max_items_per_run=25, grace_days=30))
        loaded = await active_profile_settings(session)
        assert loaded.max_items_per_run == 25
        assert loaded.grace_days == 30


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


class TestInvariantsHoldThroughTheService:
    async def test_a_run_cap_above_the_rolling_cap_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(ValueError, match="rolling cap"):
            ProfileSettings(max_items_per_run=200, max_items_per_30d=100)

    async def test_grace_under_a_week_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(ValueError):
            ProfileSettings(grace_days=3)
