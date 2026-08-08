# SPDX-License-Identifier: AGPL-3.0-or-later
"""Review-queue reap fixes: the destructive-planning path made symmetric and fail-closed.

Every test here is the same adversarial question the rest of the reap suite asks -- *can
this be made to delete something it should not, or refuse something it should not?* These
pin the fixes from the code review:

* a bulk "Reap now" on a TV show sends the show's group_key, which must expand to its
  condemned season media_keys (mirroring ``whitelist.effective_override``) instead of
  being rejected -- the destructive path was asymmetric with the spare/reap override path;
* an explicit *empty* selection must fail closed (plan nothing), never collapse to the
  whole condemned set the way an omitted field does;
* the per-run caps must count only what will actually be deleted, so a hand-spare during
  the grace window cannot trip a false ABORT.

Nothing here sends a real request: planning is inert, and the executor runs in dry-run.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import RuntimeSafety, Settings
from reaper.db.base import Base
from reaper.db.models import ActionStep, Candidate, Instance, InstanceKind, RunState, Snapshot
from reaper.db.session import create_engine, create_session_factory
from reaper.engine.policy import ProfileSettings
from reaper.services import list_config, whitelist
from reaper.services.executor import Executor
from reaper.services.planner import PlanError, build_plan
from reaper.services.profiles import live_policy_hash

GB = 1024**3


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


def _read_only() -> RuntimeSafety:
    return RuntimeSafety(destructive_enabled=False)


async def _seed_instances(session: AsyncSession, media_keys: Iterable[str]) -> None:
    """Seed the Instance rows a plan resolves each candidate's media_key against.

    A scan only condemns items from instances that exist, so a real plan reads a live row
    for every candidate, and ``build_plan`` now refuses a snapshot naming an instance that is
    gone. These tests fabricate candidates directly, so they seed the matching instances.
    One row per id is enough: the planner keys on the id alone (existence, and a movie's
    ``add_import_exclusion``), never on the kind, so a shared id across kinds needs one row.
    """
    for instance_id in sorted({int(key.split(":")[1]) for key in media_keys}):
        session.add(
            Instance(
                id=instance_id,
                kind=InstanceKind.RADARR,
                name=f"i{instance_id}",
                base_url="https://arr.test",
                api_key_enc="enc",
                created_at=utcnow(),
            )
        )
    await session.flush()


async def _snapshot(session: AsyncSession, condemned: list[tuple[str, int]]) -> int:
    """A snapshot with a set of condemned movie candidates: (media_key, size_bytes)."""
    now = utcnow()
    await _seed_instances(session, [media_key for media_key, _ in condemned])
    snapshot = Snapshot(
        created_at=now,
        policy_hash=await live_policy_hash(session),
        # Stamped the way a scan stamps it, so the executor's list interlock sees the
        # registry these tests actually run against rather than an unknown that refuses.
        list_config_hash=await list_config.current_fingerprint(session),
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


async def _snapshot_show(
    session: AsyncSession, *, series: int, seasons: list[tuple[int, int]], instance: int = 1
) -> tuple[int, str]:
    """A snapshot whose condemned candidates are seasons of one show.

    Every season shares the show's three-part ``group_key`` (``sonarr:{inst}:{series}``) --
    exactly as the scan writes it -- because that group_key is the only key a show can enter
    the review queue's Select mode under. Returns the snapshot id and that group_key.
    """
    now = utcnow()
    group_key = f"sonarr:{instance}:{series}"
    snapshot = Snapshot(
        created_at=now,
        policy_hash=await live_policy_hash(session),
        # Stamped the way a scan stamps it, so the executor's list interlock sees the
        # registry these tests actually run against rather than an unknown that refuses.
        list_config_hash=await list_config.current_fingerprint(session),
        scoring_hash="s" * 64,
        horizon_at=now,
        item_count=len(seasons),
    )
    session.add(snapshot)
    await session.flush()
    for season_number, size in seasons:
        session.add(
            Candidate(
                snapshot_id=snapshot.id,
                media_key=f"sonarr:{instance}:{series}:{season_number}",
                title=f"A Show - Season {season_number}",
                media_type="season",
                size_bytes=size,
                verdict="condemn",
                score=90,
                coverage_bp=10_000,
                explanation_json="{}",
                group_key=group_key,
                group_title="A Show",
                created_at=now,
            )
        )
    await session.flush()
    return snapshot.id, group_key


async def _steps(session: AsyncSession, run_id: int) -> list[ActionStep]:
    return list(
        (
            await session.execute(
                select(ActionStep).where(ActionStep.run_id == run_id).order_by(ActionStep.ordinal)
            )
        )
        .scalars()
        .all()
    )


class TestBulkReapOfAShowGroupKey:
    """A TV show is selectable only at the show level, so a bulk "Reap now" carries the
    three-part group_key -- never the four-part season keys beneath it. The reap path must
    expand it exactly as the override path resolves it, or bulk-reaping any TV title is
    impossible (it used to raise "these items are not condemned")."""

    async def test_a_show_group_key_expands_to_all_its_condemned_seasons(
        self, session: AsyncSession
    ) -> None:
        snapshot_id, group_key = await _snapshot_show(
            session, series=42, seasons=[(3, 4 * GB), (4, 5 * GB)]
        )

        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            approved_by="admin",
            only_media_keys={group_key},
        )

        # The plan covers every condemned season of the show, addressed by its four-part
        # season media_key -- not the group_key, which has no delete path of its own.
        steps = await _steps(session, run.id)
        assert {s.media_key for s in steps} == {"sonarr:1:42:3", "sonarr:1:42:4"}

    async def test_a_group_key_reap_leaves_a_hand_spared_season_behind(
        self, session: AsyncSession
    ) -> None:
        """Symmetry with the override path: a season spared by hand is kept even when the
        whole show is reaped (its own key wins over the show's), so the expansion drops it
        rather than raising a "these are spared" refusal for a season nobody named."""
        snapshot_id, group_key = await _snapshot_show(
            session, series=42, seasons=[(3, 4 * GB), (4, 5 * GB)]
        )
        await whitelist.spare(session, media_key="sonarr:1:42:4", title="Keep S4", note=None)

        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            approved_by="admin",
            only_media_keys={group_key},
        )

        steps = await _steps(session, run.id)
        assert {s.media_key for s in steps} == {"sonarr:1:42:3"}  # season 4 spared, untouched

    async def test_an_explicitly_named_season_still_works_alongside_a_group_key(
        self, session: AsyncSession
    ) -> None:
        """A mixed selection -- a show group_key plus a lone movie key -- resolves both: the
        group expands, the plain key passes through unchanged."""
        now = utcnow()
        snapshot = Snapshot(
            created_at=now,
            policy_hash=await live_policy_hash(session),
            list_config_hash=await list_config.current_fingerprint(session),
            scoring_hash="s" * 64,
            horizon_at=now,
            item_count=2,
        )
        session.add(snapshot)
        await session.flush()
        await _seed_instances(session, ["sonarr:1:42:3", "radarr:1:7"])
        session.add(
            Candidate(
                snapshot_id=snapshot.id,
                media_key="sonarr:1:42:3",
                title="A Show - Season 3",
                media_type="season",
                size_bytes=4 * GB,
                verdict="condemn",
                score=90,
                coverage_bp=10_000,
                explanation_json="{}",
                group_key="sonarr:1:42",
                group_title="A Show",
                created_at=now,
            )
        )
        session.add(
            Candidate(
                snapshot_id=snapshot.id,
                media_key="radarr:1:7",
                title="A Movie",
                media_type="movie",
                size_bytes=2 * GB,
                verdict="condemn",
                score=90,
                coverage_bp=10_000,
                explanation_json="{}",
                created_at=now,
            )
        )
        await session.flush()

        run = await build_plan(
            session,
            snapshot_id=snapshot.id,
            approved_by="admin",
            only_media_keys={"sonarr:1:42", "radarr:1:7"},
        )

        steps = await _steps(session, run.id)
        assert {s.media_key for s in steps} == {"sonarr:1:42:3", "radarr:1:7"}


class TestAnEmptySelectionFailsClosed:
    """An OMITTED field means "the whole condemned set"; an explicit EMPTY selection means
    "nothing", and must never collapse into the whole set. This is the one destructive
    planning path, so "nothing selected" becoming "everything" is the exact fail-open the
    system is built to refuse."""

    async def test_an_explicit_empty_media_key_set_raises(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot(session, [("radarr:1:1", 1 * GB), ("radarr:1:2", 5 * GB)])
        with pytest.raises(PlanError, match="No items were selected"):
            await build_plan(
                session,
                snapshot_id=snapshot_id,
                approved_by="admin",
                only_media_keys=set(),
            )

    async def test_an_omitted_selection_still_plans_the_whole_set(
        self, session: AsyncSession
    ) -> None:
        """The contrast: None (the field was omitted) still plans everything condemned, so
        the empty-set fix did not break the whole-library path."""
        snapshot_id = await _snapshot(session, [("radarr:1:1", 1 * GB), ("radarr:1:2", 5 * GB)])
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            approved_by="admin",
            only_media_keys=None,
        )
        steps = await _steps(session, run.id)
        assert {s.media_key for s in steps} == {"radarr:1:1", "radarr:1:2"}


class TestCapsCountOnlyWhatWillActuallyBeDeleted:
    async def test_a_spare_during_the_grace_window_does_not_trip_a_false_cap(
        self, session: AsyncSession
    ) -> None:
        """The plan is built at the cap; then the owner spares one item by hand during the
        grace window, intending a smaller reap that fits. The spared item still carries its
        steps (the frozen candidate row still reads ``condemn``), but it will be skipped per
        item -- so the cap must count only the deletable remainder, or it aborts a legitimate
        reduced run whose confirmation phrase already excluded the spare."""
        snapshot_id = await _snapshot(
            session,
            [("radarr:1:1", 1 * GB), ("radarr:1:2", 1 * GB), ("radarr:1:3", 1 * GB)],
        )
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")
        # Spare one AFTER the plan is built: it stays in the plan but will not be deleted.
        await whitelist.spare(session, media_key="radarr:1:3", title="Kept", note=None)

        settings = ProfileSettings(max_items_per_run=2, max_items_per_30d=100)
        report = await Executor(
            session, safety=_read_only(), settings=settings, dry_run=True
        ).execute(run.id)

        # Two deletable items fit the cap of two, so the run is NOT aborted. Before the fix
        # the cap counted all three (the spare included) and aborted this legitimate run.
        assert report.state is RunState.COMPLETED
        assert report.aborted_reason is None
        assert any("spared this by hand" in o.detail for o in report.outcomes)

    async def test_the_cap_still_aborts_when_the_deletable_set_is_over(
        self, session: AsyncSession
    ) -> None:
        """The counterpart: with no spare, three deletable items over a cap of two still
        abort. The fix narrows *what* is counted, it does not weaken the cap."""
        snapshot_id = await _snapshot(
            session,
            [("radarr:1:1", 1 * GB), ("radarr:1:2", 1 * GB), ("radarr:1:3", 1 * GB)],
        )
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")
        settings = ProfileSettings(max_items_per_run=2, max_items_per_30d=100)
        report = await Executor(
            session, safety=_read_only(), settings=settings, dry_run=True
        ).execute(run.id)

        assert report.state is RunState.ABORTED
        assert report.aborted_reason is not None
        assert "over your per-run cap" in report.aborted_reason.lower()


class TestARemovedRadarrRefusesThePlan:
    """rule 118: the orphaned-instance guard on the movie path (#471).

    A Radarr removed between the scan and the plan leaves a condemned movie whose
    per-instance exclusion setting is gone. ``build_plan`` refuses the stale snapshot rather
    than substitute the historical default into a delete the operator never configured (rule
    65/91): the movie cannot be deleted through a Radarr that is not there (the executor
    refuses it at send), and previewing it under a fabricated setting is the honesty problem
    the guard exists for."""

    async def test_a_movie_whose_radarr_was_removed_refuses_the_plan(
        self, session: AsyncSession
    ) -> None:
        snapshot_id = await _snapshot(session, [("radarr:1:7", 2 * GB)])
        # The operator deletes that Radarr after the scan froze this snapshot.
        await session.execute(delete(Instance).where(Instance.id == 1))
        await session.flush()

        with pytest.raises(PlanError, match="no longer connected"):
            await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

    async def test_a_present_radarr_freezes_its_real_exclusion_setting(
        self, session: AsyncSession
    ) -> None:
        """The control that proves the guard is not simply always-refuse, and that the body
        carries the instance's real setting rather than a substituted default. ``_snapshot``
        seeds the Radarr with the exclusion off (the model default), so the frozen body reads
        ``False`` -- the value pre-#471 quietly replaced with ``True`` on a miss."""
        snapshot_id = await _snapshot(session, [("radarr:1:7", 2 * GB)])
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

        steps = await _steps(session, run.id)
        assert json.loads(steps[0].body_json or "{}")["addImportExclusion"] is False
