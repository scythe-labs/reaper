# SPDX-License-Identifier: AGPL-3.0-or-later
"""The manual whitelist.

The one guarantee that matters: an item the owner spared is never reaped -- and that has
to hold even in the window between "spare" and the next scan, when a frozen snapshot's
candidate row still reads ``condemn``. So the planner is tested for the exclusion
directly, not just the service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import ActionStep, Candidate, Snapshot
from reaper.db.session import create_engine, create_session_factory
from reaper.services import whitelist
from reaper.services.planner import build_plan

GB = 1024**3


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


async def _snapshot_with(session: AsyncSession, condemned: list[tuple[str, int]]) -> int:
    now = utcnow()
    snapshot = Snapshot(
        created_at=now,
        policy_hash="p" * 64,
        scoring_hash="s" * 64,
        horizon_at=now,
        item_count=len(condemned),
    )
    session.add(snapshot)
    await session.flush()
    for i, (media_key, size) in enumerate(condemned):
        session.add(
            Candidate(
                snapshot_id=snapshot.id,
                media_key=media_key,
                title=f"Movie {i}",
                media_type="movie",
                size_bytes=size,
                verdict="condemn",
                score=90,
                coverage_bp=10_000,
                explanation_json="{}",
                created_at=now,
            )
        )
    await session.flush()
    return snapshot.id


class TestService:
    async def test_spare_then_read_back(self, session: AsyncSession) -> None:
        await whitelist.spare(session, media_key="radarr:1:7", title="Kept", note="a favorite")
        assert await whitelist.overrides(session) == {"radarr:1:7": "spare"}
        spared = await whitelist.list_spared(session)
        assert (spared[0].title, spared[0].note) == ("Kept", "a favorite")

    async def test_spare_is_idempotent_and_updates_the_note(self, session: AsyncSession) -> None:
        await whitelist.spare(session, media_key="radarr:1:7", title="Kept", note="one")
        await whitelist.spare(session, media_key="radarr:1:7", title="Kept", note="two")
        spared = await whitelist.list_spared(session)
        assert len(spared) == 1
        assert spared[0].note == "two"

    async def test_unspare_removes_and_reports(self, session: AsyncSession) -> None:
        await whitelist.spare(session, media_key="radarr:1:7", title="Kept", note=None)
        assert await whitelist.unspare(session, media_key="radarr:1:7") is True
        assert await whitelist.unspare(session, media_key="radarr:1:7") is False
        assert await whitelist.overrides(session) == {}


class TestOverrideService:
    async def test_reap_override_reads_back_separately_from_spare(
        self, session: AsyncSession
    ) -> None:
        await whitelist.spare(session, media_key="radarr:1:7", title="Kept", note=None)
        await whitelist.set_override(
            session, media_key="radarr:1:9", title="Gone", decision="reap", note="done with it"
        )
        assert await whitelist.overrides(session) == {"radarr:1:7": "spare", "radarr:1:9": "reap"}
        assert await whitelist.override_for(session, "radarr:1:9") == "reap"
        assert await whitelist.override_for(session, "radarr:1:404") is None

    async def test_setting_reap_flips_a_spare_in_place(self, session: AsyncSession) -> None:
        await whitelist.spare(session, media_key="radarr:1:7", title="Kept", note=None)
        await whitelist.set_override(
            session, media_key="radarr:1:7", title="Kept", decision="reap", note=None
        )
        assert await whitelist.overrides(session) == {"radarr:1:7": "reap"}
        # is_spared reflects the decision, not mere presence.
        assert await whitelist.is_spared(session, "radarr:1:7") is False

    async def test_remove_override_clears_either_decision(self, session: AsyncSession) -> None:
        await whitelist.set_override(
            session, media_key="radarr:1:9", title="Gone", decision="reap", note=None
        )
        assert await whitelist.remove_override(session, media_key="radarr:1:9") is True
        assert await whitelist.overrides(session) == {}


class TestEffectiveOverride:
    def test_a_show_override_covers_its_seasons(self) -> None:
        decisions = {"sonarr:1:88": "reap"}
        assert whitelist.effective_override("sonarr:1:88:3", decisions) == "reap"
        assert whitelist.effective_override("sonarr:1:88", decisions) == "reap"

    def test_a_season_override_wins_over_its_show(self) -> None:
        decisions = {"sonarr:1:88": "reap", "sonarr:1:88:3": "spare"}
        assert whitelist.effective_override("sonarr:1:88:3", decisions) == "spare"

    def test_a_movie_has_no_show_and_matches_only_itself(self) -> None:
        assert whitelist.effective_override("radarr:1:7", {"radarr:1:8": "reap"}) is None

    def test_no_override_returns_none(self) -> None:
        assert whitelist.effective_override("sonarr:1:88:3", {}) is None


class TestPlannerExcludesSparedItems:
    async def test_a_spared_condemned_item_never_enters_a_plan(self, session: AsyncSession) -> None:
        """The heart of it: the snapshot still says condemn, but the plan must not touch a
        file spared after that snapshot was frozen."""
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 1 * GB), ("radarr:1:2", 5 * GB), ("radarr:1:3", 9 * GB)]
        )
        await whitelist.spare(session, media_key="radarr:1:2", title="Movie 1", note=None)

        await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )

        planned_keys = {s.media_key for s in (await session.execute(select(ActionStep))).scalars()}
        assert "radarr:1:2" not in planned_keys
        assert planned_keys == {"radarr:1:1", "radarr:1:3"}

    async def test_sparing_a_whole_show_excludes_its_condemned_seasons(
        self, session: AsyncSession
    ) -> None:
        """A spare on the show key must reach every one of its condemned seasons -- even
        those the frozen snapshot still reads ``condemn`` -- exactly as an item spare does."""
        snapshot_id = await _snapshot_with(
            session, [("sonarr:1:88:2", 3 * GB), ("sonarr:1:88:3", 4 * GB), ("radarr:1:1", 1 * GB)]
        )
        await whitelist.spare(session, media_key="sonarr:1:88", title="A Show", note=None)

        await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )

        planned_keys = {s.media_key for s in (await session.execute(select(ActionStep))).scalars()}
        assert planned_keys == {"radarr:1:1"}

    async def test_sparing_every_condemned_item_leaves_no_plan(self, session: AsyncSession) -> None:
        """If everything condemned is spared, there is nothing to build -- and that is a
        refusal to plan, not an empty run that looks executable."""
        from reaper.services.planner import PlanError

        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 1 * GB)])
        await whitelist.spare(session, media_key="radarr:1:1", title="Movie 0", note=None)

        with pytest.raises(PlanError):
            await build_plan(
                session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
            )
