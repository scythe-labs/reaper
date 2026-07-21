# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reap loop: planner and executor.

This is the code that turns a verdict into a deleted file, so it gets the most
adversarial tests in the suite. Every one of them is really asking the same question a
different way: *can this thing be made to delete something it should not?*

Nothing here sends a real request. The executor runs in dry-run, which walks the whole
plan and every interlock but sends nothing -- and the transport guard sits underneath as
the independent backstop (proven separately in test_guarded_transport / test_plex_guard).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.api.runs import _planned_candidates
from reaper.clients.base import IntegrationError
from reaper.clients.plex import ActiveStream, PlexError
from reaper.clock import utcnow
from reaper.config import RuntimeSafety, Settings
from reaper.db.base import Base
from reaper.db.models import (
    ActionStep,
    Candidate,
    ReapRun,
    RunState,
    SizeSource,
    Snapshot,
    StepState,
)
from reaper.db.session import create_engine, create_session_factory
from reaper.engine.policy import ProfileSettings
from reaper.services import whitelist
from reaper.services.condemned import effective_condemned
from reaper.services.executor import (
    ExecutionError,
    Executor,
    ReapGateway,
    RunReport,
    _grew_materially,
    _row_timestamp,
)
from reaper.services.planner import (
    MediaRef,
    PlanError,
    build_plan,
    confirmation_phrase,
    manifest_hash,
)
from reaper.services.profiles import save_profile_settings

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


async def _snapshot_with(session: AsyncSession, condemned: list[tuple[str, int | None]]) -> int:
    """A snapshot plus a set of condemned movie candidates: (media_key, size_bytes).

    A ``None`` size is an item nothing would measure, which is not the same as a zero."""
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
                size_source=SizeSource.RADARR if size is not None else None,
                verdict="condemn",
                score=90,
                coverage_bp=10_000,
                explanation_json="{}",
                created_at=now,
            )
        )
    await session.flush()
    return snapshot.id


def _armed() -> RuntimeSafety:
    return RuntimeSafety(destructive_enabled=True)


def _read_only() -> RuntimeSafety:
    return RuntimeSafety(destructive_enabled=False)


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


class TestMediaRefParsing:
    def test_a_well_formed_key_parses(self) -> None:
        ref = MediaRef.parse("radarr:2:3759")
        assert (ref.kind, ref.instance_id, ref.arr_id) == ("radarr", 2, 3759)

    @pytest.mark.parametrize("bad", ["", "radarr:2", "plex:1:2", "radarr:x:3", "radarr:1:2:3"])
    def test_an_unroutable_key_raises_rather_than_being_skipped(self, bad: str) -> None:
        """A key we cannot route is a hard error. Silently dropping an item from a delete
        plan is safe; silently *mis-routing* one is not, and the gap between them is a
        parse nobody checked. Note ``radarr:1:2:3`` is a four-part *radarr* key -- only TV
        has seasons, so a season key on the movie side is a mis-build, not a season."""
        with pytest.raises(PlanError):
            MediaRef.parse(bad)

    def test_a_season_key_parses_to_series_and_season(self) -> None:
        ref = MediaRef.parse("sonarr:1:42:3")
        assert (ref.kind, ref.instance_id, ref.arr_id, ref.season) == ("sonarr", 1, 42, 3)

    def test_a_three_part_sonarr_key_is_a_whole_series_not_a_season(self) -> None:
        ref = MediaRef.parse("sonarr:1:42")
        assert ref.season is None


class TestSeasonPruningSteps:
    """A condemned season becomes the documented three-step sequence, journalled and
    inert. The order is load-bearing: unmonitor, verify the unmonitor, then delete."""

    async def _plan_steps(self, session: AsyncSession, media_key: str) -> list[ActionStep]:
        snapshot_id = await _snapshot_with(session, [(media_key, 4 * GB)])
        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="tester"
        )
        return list(
            (
                await session.execute(
                    select(ActionStep)
                    .where(ActionStep.run_id == run.id)
                    .order_by(ActionStep.ordinal, ActionStep.id)
                )
            )
            .scalars()
            .all()
        )

    async def test_it_emits_unmonitor_then_verify_then_delete(self, session: AsyncSession) -> None:
        steps = await self._plan_steps(session, "sonarr:1:42:3")
        assert [s.kind for s in steps] == [
            "sonarr_unmonitor",
            "sonarr_verify_unmonitor",
            "sonarr_delete_files",
        ]

    async def test_the_file_delete_comes_last(self, session: AsyncSession) -> None:
        """'Files gone, still monitored' makes Sonarr re-download everything. The
        irreversible step is last, and only after the unmonitor is verified."""
        steps = await self._plan_steps(session, "sonarr:1:42:3")
        delete = next(s for s in steps if s.kind == "sonarr_delete_files")
        assert steps.index(delete) == len(steps) - 1
        assert delete.method == "DELETE"
        assert delete.path == "/api/v3/episodefile/bulk"

    async def test_the_unmonitor_targets_the_right_season(self, session: AsyncSession) -> None:
        import json

        steps = await self._plan_steps(session, "sonarr:1:42:3")
        unmonitor = next(s for s in steps if s.kind == "sonarr_unmonitor")
        body = json.loads(unmonitor.body_json or "{}")
        assert body["series"][0]["id"] == 42
        assert body["series"][0]["seasons"][0] == {"seasonNumber": 3, "monitored": False}

    async def test_a_whole_series_key_is_skipped_not_deleted(self, session: AsyncSession) -> None:
        """A three-part sonarr key has no season to prune and no series-delete path yet.
        It must be skipped -- the run is built but carries no steps for it, so nothing is
        ever turned into a delete."""
        steps = await self._plan_steps(session, "sonarr:1:42")
        assert steps == []


class TestManifestHash:
    def test_it_is_order_independent(self) -> None:
        """Approval binds to content, not to the order candidates happened to arrive in."""
        a = _fake_candidate("radarr:1:1", 5 * GB)
        b = _fake_candidate("radarr:1:2", 9 * GB)
        assert manifest_hash([a, b]) == manifest_hash([b, a])

    def test_a_size_change_changes_the_hash(self) -> None:
        """If an item was resized between approval and execution, the plan is different
        and the approval must not carry over."""
        before = manifest_hash([_fake_candidate("radarr:1:1", 5 * GB)])
        after = manifest_hash([_fake_candidate("radarr:1:1", 6 * GB)])
        assert before != after

    def test_the_confirmation_phrase_carries_count_and_size(self) -> None:
        phrase = confirmation_phrase(
            [_fake_candidate("radarr:1:1", 100 * GB), _fake_candidate("radarr:1:2", 114 * GB)]
        )
        assert phrase == "REAP 2 SOULS 214 GB"


class TestBuildPlan:
    async def test_the_canary_is_the_smallest_item(self, session: AsyncSession) -> None:
        """Ordinal 0 -- the item executed and verified alone before any other -- must be
        the least costly possible mistake."""
        snapshot_id = await _snapshot_with(
            session,
            [("radarr:1:1", 50 * GB), ("radarr:1:2", 1 * GB), ("radarr:1:3", 9 * GB)],
        )

        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )

        steps = await _steps(session, run.id)
        canary = next(s for s in steps if s.ordinal == 0)
        assert canary.media_key == "radarr:1:2"  # the 1 GB one

    async def test_a_step_is_journalled_before_anything_is_sent(
        self, session: AsyncSession
    ) -> None:
        """The whole safety model: the row, with its exact request, exists before the
        call. Credentials are NOT in it, so it is safe to keep and to render."""
        snapshot_id = await _snapshot_with(session, [("radarr:2:42", 5 * GB)])

        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )

        step = (await _steps(session, run.id))[0]
        assert step.method == "DELETE"
        assert step.path == "/api/v3/movie/42"
        assert step.state is StepState.PENDING
        assert "deleteFiles" in (step.body_json or "")
        assert "api_key" not in (step.body_json or "").lower()

    async def test_a_degraded_snapshot_cannot_be_planned(self, session: AsyncSession) -> None:
        """A degraded snapshot missed a source. Planning a deletion from a candidate list
        we already know is incomplete is exactly the mass-deletion path."""
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 5 * GB)])
        snapshot = await session.get(Snapshot, snapshot_id)
        assert snapshot is not None
        snapshot.degraded = True
        snapshot.degraded_reason = "Radarr 4K unreachable"
        await session.flush()

        with pytest.raises(PlanError, match="degraded"):
            await build_plan(
                session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
            )

    async def test_an_empty_condemned_set_is_refused(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(session, [])
        with pytest.raises(PlanError, match="othing is condemned"):
            await build_plan(
                session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
            )


class TestARestrictedPlanReapsOnlyTheChosenItems:
    """``only_media_keys`` is the 'reap just these' path -- how a first, single, hand-picked
    delete is done without building a plan over the whole condemned set. It changes only
    which items get steps; the manifest still binds to the whole set."""

    async def test_it_plans_steps_for_only_the_chosen_key(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 1 * GB), ("radarr:1:2", 5 * GB), ("radarr:1:3", 9 * GB)]
        )
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            policy_hash="p" * 64,
            approved_by="admin",
            only_media_keys={"radarr:1:2"},
        )
        steps = await _steps(session, run.id)
        assert {s.media_key for s in steps} == {"radarr:1:2"}
        # ...but the manifest still covers the WHOLE condemned set, so any shift voids it.
        from reaper.services.planner import manifest_hash

        all_three = list(
            (await session.execute(select(Candidate).where(Candidate.snapshot_id == snapshot_id)))
            .scalars()
            .all()
        )
        assert run.approved_manifest_hash == manifest_hash(all_three)

    async def test_a_key_that_is_not_condemned_is_refused(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 1 * GB)])
        with pytest.raises(PlanError, match="not condemned"):
            await build_plan(
                session,
                snapshot_id=snapshot_id,
                policy_hash="p" * 64,
                approved_by="admin",
                only_media_keys={"radarr:1:999"},
            )

    async def test_a_spared_key_is_refused_with_a_distinct_message(
        self, session: AsyncSession
    ) -> None:
        from reaper.services import whitelist

        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 1 * GB)])
        await whitelist.spare(session, media_key="radarr:1:1", title="Kept", note=None)
        with pytest.raises(PlanError, match="spared"):
            await build_plan(
                session,
                snapshot_id=snapshot_id,
                policy_hash="p" * 64,
                approved_by="admin",
                only_media_keys={"radarr:1:1"},
            )


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


class TestDryRunProvesEverythingAndDeletesNothing:
    async def test_a_dry_run_completes_and_sends_nothing(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 1 * GB), ("radarr:1:2", 5 * GB)]
        )
        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )

        executor = Executor(session, safety=_read_only(), settings=ProfileSettings(), dry_run=True)
        report = await executor.execute(run.id)

        assert report.state is RunState.COMPLETED
        assert report.dry_run is True
        assert report.deleted_items == 0  # nothing was actually deleted
        # every step is recorded as what it WOULD have done
        assert all(o.state is StepState.SKIPPED for o in report.outcomes)
        assert any("would DELETE /api/v3/movie/1" in o.detail for o in report.outcomes)

    async def test_the_first_outcome_is_flagged_as_the_canary(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 9 * GB), ("radarr:1:2", 1 * GB)]
        )
        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )

        report = await Executor(
            session, safety=_read_only(), settings=ProfileSettings(), dry_run=True
        ).execute(run.id)

        # The canary is the smallest (1 GB, movie 2) and it is executed first.
        assert "[canary]" in report.outcomes[0].detail
        assert report.outcomes[0].media_key == "radarr:1:2"


class TestASeasonDryRunsAsAWholeSequence:
    """A condemned season is one delete unit -- unmonitor, verify, delete files -- and the
    dry run proves the whole sequence without sending any of it. The half-applied state
    'files gone, still monitored' re-downloads what we removed, so the file delete is last
    and the executor treats the three steps as one item, not three."""

    async def test_all_three_steps_are_skipped_and_shown(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(session, [("sonarr:1:42:3", 4 * GB)])
        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )

        report = await Executor(
            session, safety=_read_only(), settings=ProfileSettings(), dry_run=True
        ).execute(run.id)

        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 0
        # One outcome for the item, named by its terminal (irreversible) delete, and
        # listing the whole ordered sequence it would have sent.
        outcome = report.outcomes[0]
        assert outcome.kind == "sonarr_delete_files"
        assert "would POST /api/v3/seasonpass" in outcome.detail
        assert "would GET /api/v3/series/42" in outcome.detail
        assert "would DELETE /api/v3/episodefile/bulk" in outcome.detail
        assert "[canary]" in outcome.detail  # the sole item is ordinal 0

        # A dry run consumes nothing: the run stays PLANNED and every step stays PENDING, so
        # the plan can still be dry-run again and, crucially, executed for real afterwards.
        refreshed = await session.get(ReapRun, run.id)
        assert refreshed is not None and refreshed.state is RunState.PLANNED
        steps = (
            (await session.execute(select(ActionStep).where(ActionStep.run_id == run.id)))
            .scalars()
            .all()
        )
        assert steps and all(s.state is StepState.PENDING for s in steps)

    async def test_a_mixed_movie_and_season_plan_dry_runs_both(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 1 * GB), ("sonarr:1:42:3", 4 * GB)]
        )
        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )
        report = await Executor(
            session, safety=_read_only(), settings=ProfileSettings(), dry_run=True
        ).execute(run.id)

        assert report.state is RunState.COMPLETED
        assert {o.kind for o in report.outcomes} == {"radarr_delete", "sonarr_delete_files"}


class TestTheManifestGuard:
    async def test_a_changed_condemned_set_voids_the_run(self, session: AsyncSession) -> None:
        """The stale-tab defense. Approve a plan, then the library moves; the run must
        refuse rather than execute a plan nobody approved."""
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 1 * GB), ("radarr:1:2", 5 * GB)]
        )
        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )

        # A new item gets condemned after approval -- the plan is now different.
        session.add(
            Candidate(
                snapshot_id=snapshot_id,
                media_key="radarr:1:99",
                title="Sneaked In",
                media_type="movie",
                size_bytes=3 * GB,
                verdict="condemn",
                score=90,
                coverage_bp=10_000,
                explanation_json="{}",
                created_at=utcnow(),
            )
        )
        await session.flush()

        with pytest.raises(ExecutionError, match="changed since this plan was approved"):
            await Executor(
                session, safety=_read_only(), settings=ProfileSettings(), dry_run=True
            ).execute(run.id)


class TestCapsAbortNeverTruncate:
    async def test_a_run_over_the_item_cap_aborts_entirely(self, session: AsyncSession) -> None:
        """The whole run stops -- it does not delete the part that fits. Truncating would
        make *which* items die depend on sort order."""
        condemned = [(f"radarr:1:{i}", 1 * GB) for i in range(5)]
        snapshot_id = await _snapshot_with(session, condemned)
        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )

        settings = ProfileSettings(max_items_per_run=3, max_items_per_30d=100)
        report = await Executor(
            session, safety=_read_only(), settings=settings, dry_run=True
        ).execute(run.id)

        assert report.state is RunState.ABORTED
        assert report.aborted_reason is not None
        assert "over your per-run cap" in report.aborted_reason.lower()
        assert report.deleted_items == 0
        # This is a DRY run, so the abort is a *finding*, not a consumed run: the row stays
        # PLANNED and re-runnable, so raising the cap and running again just works.
        stored = await session.get(ReapRun, run.id)
        assert stored is not None
        assert stored.state is RunState.PLANNED

    async def test_caps_off_lets_a_run_over_the_cap_proceed(self, session: AsyncSession) -> None:
        """With the caps switched off, a plan larger than the per-run cap no longer aborts:
        the run-size ceiling is the one thing the switch drops. Every other gate is
        untouched, and this dry run still sends nothing."""
        condemned = [(f"radarr:1:{i}", 1 * GB) for i in range(5)]
        snapshot_id = await _snapshot_with(session, condemned)
        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )

        # A cap of 3 that WOULD abort five items -- but caps_enabled=False turns it off.
        settings = ProfileSettings(caps_enabled=False, max_items_per_run=3, max_items_per_30d=100)
        report = await Executor(
            session, safety=_read_only(), settings=settings, dry_run=True
        ).execute(run.id)

        assert report.state is RunState.COMPLETED
        assert report.aborted_reason is None

    async def test_caps_off_skips_the_rolling_and_byte_caps_too(
        self, session: AsyncSession
    ) -> None:
        """The switch drops EVERY run-size ceiling, not just the per-run item cap: a plan over
        the per-run byte cap and the rolling 30-day caps still completes with caps off (I-1).
        Each cap here is set below the plan, so any one still enforced would abort it."""
        condemned = [(f"radarr:1:{i}", 400 * GB) for i in range(5)]  # 5 items, 2000 GB
        snapshot_id = await _snapshot_with(session, condemned)
        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )

        settings = ProfileSettings(
            caps_enabled=False,
            max_items_per_run=1,
            max_bytes_per_run=100 * GB,
            max_items_per_30d=1,
            max_bytes_per_30d=100 * GB,
        )
        report = await Executor(
            session, safety=_read_only(), settings=settings, dry_run=True
        ).execute(run.id)

        assert report.state is RunState.COMPLETED
        assert report.aborted_reason is None

    async def test_a_real_run_over_the_cap_marks_the_run_aborted(
        self, session: AsyncSession
    ) -> None:
        """A REAL run over the cap, by contrast, does consume the run: the row is marked
        ABORTED (and still nothing is deleted)."""
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 1 * GB, 702)]
        )
        run = await _plan(session, snapshot_id)
        settings = ProfileSettings(max_items_per_run=1, max_items_per_30d=100)
        report = await Executor(
            session,
            safety=_armed(),
            settings=settings,
            dry_run=False,
            gateway=_gateway(radarr={1: FakeRadarr()}),
            armed_recheck=_armed_forever,
        ).execute(run.id)

        assert report.state is RunState.ABORTED
        stored = await session.get(ReapRun, run.id)
        assert stored is not None and stored.state is RunState.ABORTED

    async def test_a_run_over_the_byte_cap_aborts(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 400 * GB), ("radarr:1:2", 400 * GB)]
        )
        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )

        settings = ProfileSettings(max_bytes_per_run=500 * GB, max_bytes_per_30d=2000 * GB)
        report = await Executor(
            session, safety=_read_only(), settings=settings, dry_run=True
        ).execute(run.id)

        assert report.state is RunState.ABORTED


class TestArmingIsRequiredForARealRun:
    async def test_a_real_run_while_unarmed_is_refused_before_any_step(
        self, session: AsyncSession
    ) -> None:
        """dry_run=False is the caller opting in, but the host ceiling is independent.
        With the ceiling down, the executor refuses at the top -- and the transport guard
        would refuse again below it. Two layers, neither trusted alone."""
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 1 * GB)])
        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )

        with pytest.raises(ExecutionError, match="Refusing to execute for real"):
            await Executor(
                session, safety=_read_only(), settings=ProfileSettings(), dry_run=False
            ).execute(run.id)

    async def test_a_real_run_without_clients_is_refused(self, session: AsyncSession) -> None:
        """Armed is not enough: a real run needs the clients to delete through AND to run
        the streaming veto and the played-since check. With no gateway it refuses, loudly,
        before touching anything -- it does not silently proceed blind."""
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 1 * GB)])
        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )

        with pytest.raises(ExecutionError, match="no clients configured"):
            await Executor(
                session, safety=_armed(), settings=ProfileSettings(), dry_run=False
            ).execute(run.id)

    async def test_a_real_run_without_plex_is_refused(self, session: AsyncSession) -> None:
        """No Plex means no active-stream veto. Deleting blind to who is watching is the
        one thing that must never happen, so the run refuses."""
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 1 * GB)])
        run = await build_plan(
            session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
        )
        gateway = ReapGateway(radarr={1: FakeRadarr()}, plex=None, tautulli=FakeTautulli())

        with pytest.raises(ExecutionError, match="without Plex"):
            await Executor(
                session, safety=_armed(), settings=ProfileSettings(), dry_run=False, gateway=gateway
            ).execute(run.id)


class TestARunExecutesOnce:
    async def test_a_dry_run_is_repeatable_and_does_not_consume_the_plan(
        self, session: AsyncSession
    ) -> None:
        """A dry run is a simulation, so it can be run as many times as you like -- and,
        vitally, it leaves the plan runnable for real afterwards. This is the bug that broke
        'dry-run then execute' in the UI: the dry run used to complete the run."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        dry = Executor(session, safety=_read_only(), settings=ProfileSettings(), dry_run=True)

        first = await dry.execute(run.id)
        second = await dry.execute(run.id)  # repeatable, no "executes once"
        assert first.state is RunState.COMPLETED and second.state is RunState.COMPLETED

        # And the plan is still runnable for real: a real execute now succeeds.
        report = await _real(session, run, _gateway(radarr={1: FakeRadarr()}))
        assert report.state is RunState.COMPLETED and report.deleted_items == 1

    async def test_a_really_executed_run_cannot_be_re_executed(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)

        first = await _real(session, run, _gateway(radarr={1: FakeRadarr()}))
        assert first.state is RunState.COMPLETED

        with pytest.raises(ExecutionError, match="executes once"):
            await _real(session, run, _gateway(radarr={1: FakeRadarr()}))


# ---------------------------------------------------------------------------
# The real send -- against fakes, so the whole live path is proven without a server.
# ---------------------------------------------------------------------------


class TestGrewMaterially:
    """The size-drift allowance: growth within a tenth (or the 256 MiB floor for small
    items) is jitter; anything past it is an upgrade the owner never approved."""

    def test_growth_past_a_tenth_is_material(self) -> None:
        assert _grew_materially(10 * GB, 12 * GB)

    def test_growth_within_a_tenth_is_tolerated(self) -> None:
        assert not _grew_materially(10 * GB, int(10.5 * GB))

    def test_small_items_get_the_floor_not_the_tenth(self) -> None:
        # A tenth of 100 MiB is 10 MiB -- noise-sized. The 256 MiB floor governs.
        approved = 100 * 1024**2
        assert not _grew_materially(approved, approved + 200 * 1024**2)
        assert _grew_materially(approved, approved + 300 * 1024**2)

    def test_shrinking_is_never_material(self) -> None:
        # Deleting less than approved is the safe direction.
        assert not _grew_materially(10 * GB, 1 * GB)
        assert not _grew_materially(10 * GB, 0)


class TestSizeDriftReRead:
    """The live size is re-read immediately before anything is sent. A file that grew
    materially since approval was upgraded -- the approval, the caps and the typed phrase
    all counted the smaller file -- so the item is kept, never deleted unconfirmed."""

    async def test_an_upgraded_movie_is_kept_not_deleted(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(
            session, media_key="radarr:1:1", rating_key=700, size=2 * GB
        )
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr(size_on_disk=40 * GB)  # upgraded to a remux since approval

        report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert radarr.delete_calls == []  # nothing was sent
        assert report.deleted_items == 0
        assert report.skipped == 1
        assert report.state is RunState.COMPLETED  # a skip is a protection, not a failure
        assert "bigger now" in report.outcomes[0].detail

    async def test_a_movie_whose_size_cannot_be_read_is_kept(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr(size_on_disk=None)  # Radarr reports no sizeOnDisk at all

        report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert radarr.delete_calls == []
        assert report.skipped == 1

    async def test_a_movie_radarr_reports_as_zero_bytes_is_kept(
        self, session: AsyncSession
    ) -> None:
        """Zero is a partial payload, not a measurement, and the two sides must agree.

        The scan-side parser already reads a missing sizeOnDisk as unreadable, so the
        stored size is 0. If the executor accepted a live 0 as confirmed, zero against
        zero would be no growth and the file would be deleted on two invented numbers.
        Symmetric with the season case below."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700, size=0)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr(size_on_disk=0)

        report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert radarr.delete_calls == []
        assert report.skipped == 1

    async def test_growth_within_the_allowance_still_deletes(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(
            session, media_key="radarr:1:1", rating_key=700, size=10 * GB
        )
        run = await _plan(session, snapshot_id)
        # Within a tenth of the approved size: jitter, not an upgrade.
        radarr = FakeRadarr(size_on_disk=int(10.5 * GB))

        report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert report.deleted_items == 1
        assert radarr.delete_calls == [1]

    async def test_an_upgraded_season_is_kept_before_anything_is_sent(
        self, session: AsyncSession
    ) -> None:
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=800, media_type="season", size=1 * GB
        )
        run = await _plan(session, snapshot_id)
        sonarr = FakeSonarr(
            files=[
                {"id": 101, "seasonNumber": 3, "size": 20 * GB},  # upgraded since approval
                {"id": 900, "seasonNumber": 4, "size": 50 * 1024**2},
            ]
        )

        report = await _real(session, run, _gateway(sonarr={1: sonarr}))

        # The skip fired before even the reversible unmonitor -- the season is untouched.
        assert sonarr.unmonitor_calls == []
        assert sonarr.delete_calls == []
        assert report.skipped == 1

    async def test_a_season_with_an_unreadable_file_size_is_kept(
        self, session: AsyncSession
    ) -> None:
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=800, media_type="season"
        )
        run = await _plan(session, snapshot_id)
        sonarr = FakeSonarr(
            files=[
                {"id": 101, "seasonNumber": 3, "size": 50 * 1024**2},
                {"id": 102, "seasonNumber": 3},  # no size reported
            ]
        )

        report = await _real(session, run, _gateway(sonarr={1: sonarr}))

        assert sonarr.unmonitor_calls == []
        assert report.skipped == 1

    async def test_a_season_whose_files_all_report_zero_bytes_is_kept(
        self, session: AsyncSession
    ) -> None:
        """The hole a fabricated zero opens straight through the size interlock.

        The same partial payload that makes a season's stored size 0 at scan time makes
        its live file sizes 0 at delete time. Zero against zero is no growth at all, so
        the interlock passed and real files were deleted with BOTH numbers invented.
        `_payload_size` now reads 0 as unreadable, exactly as the scan-side parsers do.
        """
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=800, media_type="season", size=0
        )
        run = await _plan(session, snapshot_id)
        sonarr = FakeSonarr(
            files=[
                {"id": 101, "seasonNumber": 3, "size": 0},
                {"id": 102, "seasonNumber": 3, "size": 0},
            ]
        )

        report = await _real(session, run, _gateway(sonarr={1: sonarr}))

        assert sonarr.unmonitor_calls == []
        assert sonarr.delete_calls == []
        assert report.skipped == 1

    async def test_a_season_sonarr_reports_no_files_for_is_kept(
        self, session: AsyncSession
    ) -> None:
        """An empty answer is not a confirmation.

        `sum([])` is 0, which sails through the growth check and marks the step verified
        having proven nothing -- while consuming the canary, because the plan is ordered
        smallest-first and a zero-size season sorts to the front. Rule 1.
        """
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=800, media_type="season", size=0
        )
        run = await _plan(session, snapshot_id)
        sonarr = FakeSonarr(files=[{"id": 900, "seasonNumber": 4, "size": 50 * 1024**2}])

        report = await _real(session, run, _gateway(sonarr={1: sonarr}))

        assert sonarr.unmonitor_calls == []
        assert sonarr.delete_calls == []
        assert report.skipped == 1

    async def test_a_drift_skip_does_not_consume_the_canary(self, session: AsyncSession) -> None:
        """The skipped item touched no file, so the next item still gets the canary's
        halt-on-failure protection -- same rule as every other pre-send skip."""
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        run = await _plan(session, snapshot_id)

        class DriftingFirstRadarr(FakeRadarr):
            async def movie_by_id(self, movie_id: int) -> dict[str, Any]:
                movie = await super().movie_by_id(movie_id)
                if movie_id == 1:
                    movie["sizeOnDisk"] = 50 * GB  # only the first item drifted
                return movie

        radarr = DriftingFirstRadarr(fail_ids={2})  # the promoted canary then fails
        report = await _real(session, run, _gateway(radarr={1: radarr}))

        # Movie 1 was kept (drift); movie 2 became the canary, failed, and aborted the run.
        assert report.state is RunState.ABORTED
        assert radarr.delete_calls == [2]


class TestAnApprovedSizeThatWasNeverConfirmed:
    """An item nothing would size is not deletable, and two independent layers say so.

    The growth check cannot police the approved side: it compares the LIVE size against
    the frozen number, so ``_grew_materially(None-as-0, live)`` would reduce to
    ``live > 256 MiB`` and stay silent for every smaller file. So the refusal happens
    earlier and twice over. The **planner** never puts such an item in a plan, which is
    what keeps the caps and the typed confirmation exact by construction. The **executor**
    refuses it again per item, and deliberately does not trust the plan to have done its
    job.
    """

    async def test_the_planner_holds_it_back_and_says_how_many(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 10 * GB), ("radarr:1:2", None)])
        run = await _plan(session, snapshot_id)

        steps = await _steps(session, run.id)
        assert [s.media_key for s in steps] == ["radarr:1:1"]
        assert run.held_back_unknown_size == 1

    async def test_a_named_item_is_refused_out_loud_never_dropped(
        self, session: AsyncSession
    ) -> None:
        """Silently planning fewer items than asked is the one thing a "reap just these"
        must never do, whatever the reason."""
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 10 * GB), ("radarr:1:2", None)])

        with pytest.raises(PlanError, match="couldn't measure"):
            await build_plan(
                session,
                snapshot_id=snapshot_id,
                policy_hash="p" * 64,
                approved_by="admin",
                only_media_keys={"radarr:1:2"},
            )

    async def test_a_movie_with_no_approved_size_is_kept(self, session: AsyncSession) -> None:
        """The executor's own layer, tested by planning a measured item and then taking
        its size away: the plan is wrong, and the host-side check has to hold anyway."""
        snapshot_id = await _snapshot_one(
            session, media_key="radarr:1:1", rating_key=700, size=1 * GB
        )
        run = await _plan(session, snapshot_id)
        await _unmeasure(session, "radarr:1:1", run)
        # A real, readable, ordinary-sized file: under the drift floor, so the growth check
        # would have seen no growth and let this delete through.
        radarr = FakeRadarr(size_on_disk=200 * 1024**2)

        report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert radarr.delete_calls == []  # nothing was sent
        assert report.skipped == 1
        assert report.state is RunState.COMPLETED  # a skip is a protection, not a failure
        assert "never got a size" in report.outcomes[0].detail

    async def test_a_season_with_no_approved_size_is_kept_before_the_unmonitor(
        self, session: AsyncSession
    ) -> None:
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=800, media_type="season", size=1 * GB
        )
        run = await _plan(session, snapshot_id)
        await _unmeasure(session, "sonarr:1:42:3", run)
        # Sonarr reports every file's size fine, so the live-side refusal never fires; only
        # the approved side is unconfirmed, and the season totals well under the drift floor.
        sonarr = FakeSonarr()

        report = await _real(session, run, _gateway(sonarr={1: sonarr}))

        assert sonarr.unmonitor_calls == []  # not even the reversible half ran
        assert sonarr.delete_calls == []
        assert report.skipped == 1
        assert "never got a size" in report.outcomes[0].detail

    async def test_a_size_measured_against_a_different_thing_is_kept(
        self, session: AsyncSession
    ) -> None:
        """A size alone is not enough: it has to measure what the live re-read measures.

        A movie sized from its file rather than its folder is a lower bound, so comparing
        it against the folder would read a normal folder as growth. Reaper keeps the file
        rather than comparing two different quantities.
        """
        snapshot_id = await _snapshot_one(
            session, media_key="radarr:1:1", rating_key=700, size=1 * GB
        )
        run = await _plan(session, snapshot_id)
        candidate = (
            await session.execute(select(Candidate).where(Candidate.media_key == "radarr:1:1"))
        ).scalar_one()
        candidate.size_source = SizeSource.RADARR_FILE
        await session.flush()

        report = await _real(session, run, _gateway(radarr={1: (radarr := FakeRadarr())}))

        assert radarr.delete_calls == []
        assert report.skipped == 1

    async def test_the_item_cap_counts_only_items_with_a_confirmed_size(
        self, session: AsyncSession
    ) -> None:
        """The cap counts the set that will really be acted on, not the plan's length.

        Two items can be deleted; the third is never planned, so a cap of two is not
        exceeded and the run must not abort.
        """
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 1 * GB), ("radarr:1:2", 1 * GB), ("radarr:1:3", None)]
        )
        run = await _plan(session, snapshot_id)

        settings = ProfileSettings(max_items_per_run=2, max_items_per_30d=100)
        report = await Executor(
            session, safety=_read_only(), settings=settings, dry_run=True
        ).execute(run.id)

        assert report.state is RunState.COMPLETED
        assert report.aborted_reason is None

    async def test_the_byte_cap_never_counts_an_unconfirmed_size_as_nothing(
        self, session: AsyncSession
    ) -> None:
        """The dangerous direction: counting 0 lets a run pass a cap it does not fit.

        With the unmeasured item left out, the run is 400 GB against a 500 GB cap and
        completes; the item it left out is kept, not deleted off-budget.
        """
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 400 * GB), ("radarr:1:2", None)]
        )
        run = await _plan(session, snapshot_id)

        settings = ProfileSettings(max_bytes_per_run=500 * GB, max_bytes_per_30d=2000 * GB)
        executor = Executor(session, safety=_read_only(), settings=settings, dry_run=True)
        report = await executor.execute(run.id)

        assert report.state is RunState.COMPLETED
        planned = await _planned_candidates(session, run)
        assert [c.media_key for c in planned] == ["radarr:1:1"]

    async def test_the_confirmation_total_leaves_out_what_will_not_be_deleted(
        self, session: AsyncSession
    ) -> None:
        """The count and the byte total the owner types describe the exact set acted on.

        Counting the unmeasured item would ask them to approve "2 SOULS" for a run that
        can only ever delete one.
        """
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 10 * GB), ("radarr:1:2", None)])
        run = await _plan(session, snapshot_id)

        planned = await _planned_candidates(session, run)

        assert [c.media_key for c in planned] == ["radarr:1:1"]
        assert confirmation_phrase(planned) == "REAP 1 SOUL 10 GB"

    def test_an_unmeasured_size_hashes_differently_from_a_zero(self) -> None:
        """The manifest binds what the owner approved, so the two must not collide.

        If an unknown encoded as ``0``, a size later measured as 0 would leave the hash
        unchanged and the stale approval would still execute. Encoded as JSON ``null`` it
        is a different set, which voids the approval, which is correct: the owner approved
        a set containing an item nobody could size, and it is no longer that set.
        """
        unmeasured = manifest_hash([_fake_candidate("radarr:1:1", None)])
        zero = manifest_hash([_fake_candidate("radarr:1:1", 0)])

        assert unmeasured != zero

    def test_the_hash_does_not_depend_on_the_order_it_is_given(self) -> None:
        """Sorting is on the media_key alone, which is unique per snapshot, so a None size
        is never compared against an int. Sorting on the whole tuple would raise."""
        a = _fake_candidate("radarr:1:1", None)
        b = _fake_candidate("radarr:1:2", 5)

        assert manifest_hash([a, b]) == manifest_hash([b, a])

    async def test_the_phrase_is_unchanged_for_an_all_measured_plan(
        self, session: AsyncSession
    ) -> None:
        """Regression. Both ``_run_out`` and the execute route recompute this phrase and
        compare it byte for byte, so any change of shape 409s every execute."""
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 1 * GB)])
        run = await _plan(session, snapshot_id)

        assert confirmation_phrase(await _planned_candidates(session, run)) == "REAP 1 SOUL 1 GB"


class TestTheUnmeasuredAllowance:
    """``max_unmeasured_per_run`` above zero lets a bounded number of unmeasured items be
    reaped. Everything it does NOT relax is what these pin.

    It exists because "never" is the wrong answer for an operator with a handful of items
    their *arr will not size. It is a count and not a switch because an unmeasured item
    contributes nothing to either byte cap, so the byte caps cannot bound this population
    at all: the count is the only bound there is.
    """

    async def test_the_test_item_is_never_an_unmeasured_one(self, session: AsyncSession) -> None:
        """The single most important check here. The canary's whole purpose is a first
        mistake whose cost is known in advance, so an unmeasured canary is the original
        defect wearing a setting. No configuration may reintroduce it.

        The unmeasured item is the SMALLEST thing in the plan by any naive reading -- it
        has no size at all -- so a combined sort, or a key treating None as 0, seats it at
        ordinal 0. That is exactly the accident this asserts against.
        """
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", None), ("radarr:1:2", 5 * GB), ("radarr:1:3", 1 * GB)]
        )
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            policy_hash="p" * 64,
            approved_by="admin",
            max_unmeasured=3,
        )

        ordered = [s.media_key for s in await _steps(session, run.id)]
        # Smallest measured first, and the unmeasured one last however small it looks.
        assert ordered == ["radarr:1:3", "radarr:1:2", "radarr:1:1"]

    async def test_over_the_allowance_aborts_rather_than_trimming(
        self, session: AsyncSession
    ) -> None:
        """Planning the first N would let sort order pick which unmeasured file dies,
        which is the accident the whole design removes. Same abort-not-truncate
        discipline the byte caps keep."""
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", None), ("radarr:1:2", None), ("radarr:1:3", 1 * GB)]
        )

        with pytest.raises(PlanError, match="over your limit"):
            await build_plan(
                session,
                snapshot_id=snapshot_id,
                policy_hash="p" * 64,
                approved_by="admin",
                max_unmeasured=1,
            )

    async def test_the_phrase_names_what_the_gb_figure_does_not_cover(
        self, session: AsyncSession
    ) -> None:
        """The GB figure stays exact for the items it describes, so the owner has to type
        an acknowledgment that the run holds things it does not.

        Saved to the profile, not merely passed to ``build_plan``: the review surface
        reads the allowance live, so if only one of the two knew about it the phrase shown
        and the phrase recomputed at execute time would differ, and every execute would
        409.
        """
        await save_profile_settings(
            session, ProfileSettings(max_items_per_run=10, max_unmeasured_per_run=2)
        )
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", None), ("radarr:1:2", 10 * GB)])
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            policy_hash="p" * 64,
            approved_by="admin",
            max_unmeasured=2,
        )

        planned = await _planned_candidates(session, run)
        assert confirmation_phrase(planned) == "REAP 2 SOULS 10 GB + 1 UNSIZED"

    async def test_they_count_against_the_item_cap(self, session: AsyncSession) -> None:
        """Only the BYTE caps cannot bound them. The item caps must, or the population is
        unbounded and the setting means nothing."""
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", None), ("radarr:1:2", None), ("radarr:1:3", 1 * GB)]
        )
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            policy_hash="p" * 64,
            approved_by="admin",
            max_unmeasured=2,
        )

        settings = ProfileSettings(
            max_items_per_run=2, max_items_per_30d=100, max_unmeasured_per_run=2
        )
        report = await Executor(
            session, safety=_read_only(), settings=settings, dry_run=True
        ).execute(run.id)

        assert report.state is RunState.ABORTED
        assert "3 titles" in (report.aborted_reason or "")

    async def test_caps_off_still_enforces_the_unknown_size_limit(
        self, session: AsyncSession
    ) -> None:
        """The caps switch drops the run-size caps, never the keep-unknown-size rule. With
        caps off and the unknown-size allowance lowered after approval below what the plan
        admitted, the run still aborts on the unmeasured count: that guard is checked before
        the run-size caps and is not governed by the switch."""
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", None), ("radarr:1:2", None), ("radarr:1:3", 1 * GB)]
        )
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            policy_hash="p" * 64,
            approved_by="admin",
            max_unmeasured=2,
        )

        # Caps OFF, but the unknown-size allowance was lowered to 1 after approval.
        settings = ProfileSettings(caps_enabled=False, max_unmeasured_per_run=1)
        report = await Executor(
            session, safety=_read_only(), settings=settings, dry_run=True
        ).execute(run.id)

        assert report.state is RunState.ABORTED
        assert "couldn't measure" in (report.aborted_reason or "")

    async def test_they_never_contribute_zero_to_a_byte_cap(self, session: AsyncSession) -> None:
        """The tempting shortcut once they can be planned is to let them count as 0 bytes
        so the arithmetic keeps working. That is the original bug by the back door: the
        byte total must describe only what was actually measured."""
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", None), ("radarr:1:2", 400 * GB)]
        )
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            policy_hash="p" * 64,
            approved_by="admin",
            max_unmeasured=1,
        )

        settings = ProfileSettings(
            max_bytes_per_run=500 * GB,
            max_bytes_per_30d=2000 * GB,
            max_items_per_run=10,
            max_unmeasured_per_run=1,
        )
        report = await Executor(
            session, safety=_read_only(), settings=settings, dry_run=True
        ).execute(run.id)

        # 400 GB of measured content fits under 500. The unmeasured item is bounded by
        # count, not by pretending it is empty.
        assert report.state is RunState.COMPLETED

    async def test_lowering_it_after_approval_keeps_those_items(
        self, session: AsyncSession
    ) -> None:
        """The allowance is re-read at execute time, so an operator who changes their mind
        between approving and executing gets the safe direction. Raising it after approval
        can add nothing, because those items were never planned."""
        # The measured item is the plan's test item, and is deleted normally. Only the
        # unmeasured one is at issue here.
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", None, 701), ("radarr:1:2", 1 * GB, 702)]
        )
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            policy_hash="p" * 64,
            approved_by="admin",
            max_unmeasured=1,
        )

        radarr = FakeRadarr()
        # Approved under an allowance of 1; executed under 0.
        report = await _real(session, run, _gateway(radarr={1: radarr}))

        # The measured item went; the unmeasured one was kept by the lowered allowance.
        assert radarr.delete_calls == [2]
        assert report.skipped == 1
        assert report.deleted_unmeasured == 0

    async def test_an_unmeasured_season_still_never_rides_a_show_level_click(
        self, session: AsyncSession
    ) -> None:
        """ "Reap now" on a show expands to its measured seasons only, whatever the
        allowance says. An unmeasured season enters a plan through a deliberate whole-set
        or by-name reap, never by riding a button that was not aimed at it."""
        snapshot_id = await _snapshot_many(
            session,
            [("sonarr:1:42:1", 1 * GB, 801), ("sonarr:1:42:2", None, 802)],
            media_type="season",
            group_key="sonarr:1:42",
        )
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            policy_hash="p" * 64,
            approved_by="admin",
            only_media_keys={"sonarr:1:42"},
            max_unmeasured=5,
        )

        planned = {s.media_key for s in await _steps(session, run.id)}
        assert planned == {"sonarr:1:42:1"}


class TestUsingTheAllowanceDoesNotBrickTheNextThirtyDays:
    """The rolling 30-day window reads past VERIFIED deletions back off their frozen
    candidate rows. An allowed unmeasured item leaves a row whose size is NULL forever,
    so from the moment one is deleted the window contains one -- on every later run.

    Aborting on that would make one use of the allowance disable reaping entirely for a
    month, dry runs included, with no way out. Skipping the row would be the other
    failure: the window would read light and spend past the monthly budget. It counts as
    an item and contributes no bytes, which is the only reading that is neither.
    """

    async def test_a_past_unmeasured_deletion_does_not_abort_later_runs(
        self, session: AsyncSession
    ) -> None:
        # A measured item rides along: a plan of only unmeasured items has no safe test
        # item and is refused outright.
        past = await _snapshot_many(
            session, [("radarr:1:1", None, 701), ("radarr:1:2", 1 * GB, 702)]
        )
        first = await build_plan(
            session,
            snapshot_id=past,
            policy_hash="p" * 64,
            approved_by="admin",
            max_unmeasured=1,
        )
        allowing = ProfileSettings(max_items_per_run=10, max_unmeasured_per_run=1)
        done = await _real(session, first, _gateway(radarr={1: FakeRadarr()}), settings=allowing)
        assert done.deleted_items == 2  # both went, the unmeasured one included
        assert done.deleted_unmeasured == 1  # and is named as absent from the byte total
        assert done.deleted_bytes == 1 * GB

        # A completely ordinary second run, of a measured item, under the same settings.
        later = await _snapshot_with(session, [("radarr:1:9", 1 * GB)])
        second = await build_plan(
            session,
            snapshot_id=later,
            policy_hash="p" * 64,
            approved_by="admin",
            max_unmeasured=1,
        )
        report = await Executor(
            session, safety=_read_only(), settings=allowing, dry_run=True
        ).execute(second.id)

        assert report.state is RunState.COMPLETED, report.aborted_reason

    async def test_the_past_unmeasured_item_still_counts_against_the_monthly_item_cap(
        self, session: AsyncSession
    ) -> None:
        """It was deleted, so it spends the monthly item budget. That budget is the only
        thing bounding this population, since it contributed no bytes to spend."""
        past = await _snapshot_many(
            session, [("radarr:1:1", None, 701), ("radarr:1:2", 1 * GB, 702)]
        )
        first = await build_plan(
            session,
            snapshot_id=past,
            policy_hash="p" * 64,
            approved_by="admin",
            max_unmeasured=1,
        )
        allowing = ProfileSettings(max_items_per_run=10, max_unmeasured_per_run=1)
        await _real(session, first, _gateway(radarr={1: FakeRadarr()}), settings=allowing)

        later = await _snapshot_with(session, [("radarr:1:9", 1 * GB)])
        second = await build_plan(
            session, snapshot_id=later, policy_hash="p" * 64, approved_by="admin"
        )
        # Two already spent this month, and a cap of two.
        capped = ProfileSettings(max_items_per_run=2, max_items_per_30d=2, max_unmeasured_per_run=1)
        report = await Executor(
            session, safety=_read_only(), settings=capped, dry_run=True
        ).execute(second.id)

        assert report.state is RunState.ABORTED
        assert "already" in (report.aborted_reason or "")


class TestTheAllowanceIsACountNotASwitch:
    """The executor must enforce the NUMBER, not merely whether it is above zero.

    The whole reason this setting is safe to keep out of the policy hash is that both
    directions of a change resolve toward keeping. That only holds if a tightening is
    actually honored at execute time -- otherwise lowering 25 to 1 is silently ignored on
    the one population no byte cap can bound.
    """

    async def test_lowering_it_to_a_smaller_non_zero_value_is_enforced(
        self, session: AsyncSession
    ) -> None:
        snapshot_id = await _snapshot_with(
            session,
            [("radarr:1:1", 1 * GB), ("radarr:1:2", None), ("radarr:1:3", None)],
        )
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            policy_hash="p" * 64,
            approved_by="admin",
            max_unmeasured=2,
        )

        # Approved under 2, executed under 1. Not zero, so the old boolean read admitted
        # both; the count must refuse the run instead.
        tightened = ProfileSettings(max_items_per_run=10, max_unmeasured_per_run=1)
        report = await Executor(
            session, safety=_read_only(), settings=tightened, dry_run=True
        ).execute(run.id)

        assert report.state is RunState.ABORTED
        assert "over your limit" in (report.aborted_reason or "")


class TestTheCanaryRuleHoldsWhenNothingIsMeasured:
    """Sorting the unmeasured tail last is necessary but not sufficient. With nothing
    measured to sort ahead of it, the tail IS the plan and ordinal 0 has unknown cost."""

    async def test_a_plan_of_only_unmeasured_items_is_refused(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", None), ("radarr:1:2", None)])

        with pytest.raises(PlanError, match="nothing safe to test"):
            await build_plan(
                session,
                snapshot_id=snapshot_id,
                policy_hash="p" * 64,
                approved_by="admin",
                max_unmeasured=5,
            )


class TestTheHeldBackNoticeSurvivesTheAllowance:
    """Turning the allowance ON must not make the plan LESS honest than leaving it off."""

    async def test_a_show_reap_still_reports_the_seasons_it_dropped(
        self, session: AsyncSession
    ) -> None:
        """An unmeasured season never rides a show-level click, whatever the allowance.
        That is deliberate -- but it means the click plans fewer seasons than the queue
        showed, so the count that explains it must survive too."""
        snapshot_id = await _snapshot_many(
            session,
            [("sonarr:1:42:1", 1 * GB, 801), ("sonarr:1:42:2", None, 802)],
            media_type="season",
            group_key="sonarr:1:42",
        )
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            policy_hash="p" * 64,
            approved_by="admin",
            only_media_keys={"sonarr:1:42"},
            max_unmeasured=5,
        )

        assert {s.media_key for s in await _steps(session, run.id)} == {"sonarr:1:42:1"}
        assert run.held_back_unknown_size == 1

    async def test_a_show_with_no_measurable_season_says_which_show(
        self, session: AsyncSession
    ) -> None:
        """Not "these items are not condemned in this snapshot", which is true of the key
        and completely misleading about the show."""
        snapshot_id = await _snapshot_many(
            session,
            [("sonarr:1:42:1", None, 801)],
            media_type="season",
            group_key="sonarr:1:42",
        )

        with pytest.raises(PlanError, match="couldn't measure any of the seasons"):
            await build_plan(
                session,
                snapshot_id=snapshot_id,
                policy_hash="p" * 64,
                approved_by="admin",
                only_media_keys={"sonarr:1:42"},
            )


class TestDisarmMidRun:
    """Turning deletion off mid-run stops the run before its next item. Files already
    verified deleted stay deleted; nothing further is sent."""

    async def test_disarming_between_items_halts_the_rest(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()

        answers = iter([True, False])  # armed for the first item, off before the second

        async def flipping() -> bool:
            return next(answers)

        report = await _real(session, run, _gateway(radarr={1: radarr}), armed_recheck=flipping)

        assert report.state is RunState.ABORTED
        assert "turned off" in (report.aborted_reason or "")
        assert radarr.delete_calls == [1]  # the second item was never attempted
        assert report.deleted_items == 1  # the first stays deleted and recorded

    async def test_an_unreadable_switch_fails_closed(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()

        async def broken() -> bool:
            raise RuntimeError("db unreachable")

        report = await _real(session, run, _gateway(radarr={1: radarr}), armed_recheck=broken)

        assert report.state is RunState.ABORTED
        assert radarr.delete_calls == []  # nothing at all was sent

    async def test_a_real_run_without_the_recheck_is_refused(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)

        with pytest.raises(ExecutionError, match="arm check"):
            await Executor(
                session,
                safety=_armed(),
                settings=ProfileSettings(),
                dry_run=False,
                gateway=_gateway(radarr={1: FakeRadarr()}),
            ).execute(run.id)

    async def test_a_dry_run_needs_no_recheck(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)

        report = await Executor(
            session, safety=_read_only(), settings=ProfileSettings(), dry_run=True
        ).execute(run.id)

        assert report.state is RunState.COMPLETED


class TestStopMidRun:
    """Pressing Stop halts the run gracefully before its next item -- like disarming, but it
    leaves deletion armed. Crucially, whatever was removed before the halt still has its stale
    Plex entry tidied: a stopped run cleans up Plex exactly as a completed one does."""

    async def test_stopping_between_items_halts_the_rest(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()

        answers = iter([False, True])  # running for the first item, stopped before the second

        async def stopping() -> bool:
            return next(answers)

        report = await _real(session, run, _gateway(radarr={1: radarr}), stop_recheck=stopping)

        assert report.state is RunState.ABORTED
        assert "stopped" in (report.aborted_reason or "").lower()
        assert radarr.delete_calls == [1]  # the second item was never attempted
        assert report.deleted_items == 1  # the first stays deleted and recorded

    async def test_a_stopped_run_still_tidies_plex(self, session: AsyncSession) -> None:
        """The requirement behind Stop: whatever was removed before the halt still gets its
        stale Plex entry refreshed and purged, so nothing is left showing as unavailable."""
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Movies": ["/movies"]}, item_counts={"Movies": [100, 99]})
        radarr = FakeRadarr(path="/movies/One (2001)")

        answers = iter([False, True])

        async def stopping() -> bool:
            return next(answers)

        report = await _real(
            session, run, _gateway(radarr={1: radarr}, plex=plex), stop_recheck=stopping
        )

        assert report.state is RunState.ABORTED
        assert report.deleted_items == 1
        # Refreshed for the file that WAS removed, and its stale entry purged -- on a STOPPED
        # run, exactly as on a completed one.
        assert plex.refreshed == [("Movies", "/movies/One (2001)")]
        assert plex.emptied == ["Movies"]

    async def test_an_unreadable_stop_flag_keeps_running(self, session: AsyncSession) -> None:
        """Stop is a convenience, not a fail-closed interlock (the arm-recheck is that). An
        unreadable stop flag must NOT halt a healthy run on a transient blip."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()

        async def broken() -> bool:
            raise RuntimeError("flag unreadable")

        report = await _real(session, run, _gateway(radarr={1: radarr}), stop_recheck=broken)

        assert report.state is RunState.COMPLETED
        assert radarr.delete_calls == [1]  # it ran to completion, not halted on the blip

    async def test_a_hard_cancel_still_marks_aborted_and_tidies_plex(
        self, session: AsyncSession
    ) -> None:
        """A hard cancel mid-run (the app shutting down, or a force-stop) is not the graceful
        Stop -- it arrives as CancelledError, not ExecutionError -- but the executor must still
        mark the run ABORTED and tidy Plex for what was already removed BEFORE the cancellation
        propagates, so shutdown never leaves the run EXECUTING with orphaned Plex entries. This
        exercises the separate ``except asyncio.CancelledError`` branch and its finally."""
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        run = await _plan(session, snapshot_id)

        class _CancelBeforeSecondItem(FakePlex):
            """The streaming veto is re-polled before every delete; raise CancelledError on the
            second poll to stand in for a shutdown landing between items."""

            def __init__(self, **kw: Any) -> None:
                super().__init__(**kw)
                self._polls = 0

            async def active_streams(self) -> list[ActiveStream]:
                self._polls += 1
                if self._polls >= 2:
                    raise asyncio.CancelledError
                return []

        plex = _CancelBeforeSecondItem(
            sections={"Movies": ["/movies"]}, item_counts={"Movies": [100, 99]}
        )
        radarr = FakeRadarr(path="/movies/One (2001)")

        with pytest.raises(asyncio.CancelledError):
            await _real(session, run, _gateway(radarr={1: radarr}, plex=plex))

        refreshed = await session.get(ReapRun, run.id)
        assert refreshed is not None
        assert refreshed.state is RunState.ABORTED  # not left EXECUTING
        assert plex.refreshed == [("Movies", "/movies/One (2001)")]  # the first item's path
        assert plex.emptied == ["Movies"]  # tidied on the cancel path, before it propagated

    async def test_progress_is_reported_after_every_item(self, session: AsyncSession) -> None:
        """The polled status is fed a cumulative tally after each item, so a long run shows
        movement and the app-wide bar can follow it."""
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 2 * GB, 702)]
        )
        run = await _plan(session, snapshot_id)
        seen: list[tuple[int, int, int]] = []

        executor = Executor(
            session,
            safety=_armed(),
            settings=ProfileSettings(),
            dry_run=False,
            gateway=_gateway(radarr={1: FakeRadarr()}),
            armed_recheck=_armed_forever,
            progress=lambda p: seen.append((p.done, p.total, p.deleted_items)),
            exclusion_poll_delay=0.0,
            plex_settle_delay=0.0,
        )
        report = await executor.execute(run.id)

        assert report.deleted_items == 2
        assert seen == [(1, 2, 1), (2, 2, 2)]  # one emit per item, counts cumulative


class TestRowTimestamp:
    """The played-since-approval check reads Tautulli rows through this. Tautulli
    writes ``stopped=0`` -- not a real epoch stamp -- when it has no stop time, and
    0 compared against any approval time could never spare."""

    def test_a_zero_stop_time_falls_through_to_date(self) -> None:
        assert _row_timestamp({"stopped": 0, "date": 1_700_000_000}) == 1_700_000_000

    def test_zero_everywhere_is_no_evidence(self) -> None:
        assert _row_timestamp({"stopped": 0, "date": 0}) is None

    def test_stopped_wins_when_it_is_real(self) -> None:
        assert _row_timestamp({"stopped": 1_700_000_005, "date": 1_700_000_001}) == 1_700_000_005


class TestMovieLiveSend:
    async def test_a_movie_is_deleted_and_the_exclusion_is_verified(
        self, session: AsyncSession
    ) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()
        gateway = _gateway(radarr={1: radarr})

        report = await _real(session, run, gateway)

        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 1
        assert radarr.delete_calls == [1]  # the movie was actually deleted
        assert [s.state for s in await _steps(session, run.id)] == [StepState.VERIFIED]

    async def test_a_reap_produces_a_titled_checklist(self, session: AsyncSession) -> None:
        """The after-action report carries a plain-English checklist per item: the interlocks
        that passed, the delete, and the exclusion verification -- each a ✓/✗ the UI renders
        like the why-panel."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        report = await _real(session, run, _gateway(radarr={1: FakeRadarr()}))

        outcome = report.outcomes[0]
        assert outcome.title == "Worthless 0"
        labels = {c.label: c.ok for c in outcome.checks}
        assert all(labels.values())  # every check passed
        assert any("watching" in label.lower() for label in labels)
        assert any("removed the file" in label.lower() for label in labels)
        assert any("exclusion" in label.lower() for label in labels)

    async def test_a_failed_reap_marks_the_failing_check(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        # The delete happens but the exclusion never lands.
        report = await _real(session, run, _gateway(radarr={1: FakeRadarr(land_exclusion=False)}))

        outcome = report.outcomes[0]
        failed = [c.label for c in outcome.checks if not c.ok]
        assert failed and any("exclusion" in label.lower() for label in failed)
        # ...but the "removed the file" check still passed: the file really is gone.
        removed = next(c for c in outcome.checks if "removed the file" in c.label.lower())
        assert removed.ok is True

    async def test_an_exclusion_that_lands_a_beat_late_is_still_verified(
        self, session: AsyncSession
    ) -> None:
        """The real-world bug: Radarr adds the import exclusion just *after* the delete's
        200, so an immediate single read misses it and a run whose delete actually succeeded
        aborts on the canary. The verification polls, so a slightly-late exclusion verifies."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        # The exclusion is not visible on the first two reads; it appears on the third.
        radarr = FakeRadarr(exclusion_appears_after=2)

        report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 1
        assert radarr.delete_calls == [1]

    async def test_an_exclusion_that_does_not_land_fails_the_canary(
        self, session: AsyncSession
    ) -> None:
        """Radarr returns 200 for the delete even when the exclusion silently did nothing.
        The re-read catches it, and as the sole (canary) item its failure aborts the run."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        gateway = _gateway(radarr={1: FakeRadarr(land_exclusion=False)})

        report = await _real(session, run, gateway)

        assert report.state is RunState.ABORTED
        assert report.deleted_items == 0
        assert report.aborted_reason is not None
        assert "test item" in report.aborted_reason.lower()

    async def test_a_movie_without_a_tmdb_id_is_refused_before_the_delete(
        self, session: AsyncSession
    ) -> None:
        """With no tmdbId the exclusion re-read can never verify, so the item must be
        refused BEFORE anything is sent. Discovering it afterwards would strand an
        irreversible delete behind a check that was doomed from the start -- and as
        the canary here, it would abort the run having already removed the file."""

        class TmdblessRadarr(FakeRadarr):
            async def movie_by_id(self, movie_id: int) -> dict[str, Any]:
                movie = await super().movie_by_id(movie_id)
                movie.pop("tmdbId")
                return movie

        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        radarr = TmdblessRadarr()

        report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert radarr.delete_calls == []  # nothing was sent, so nothing was deleted
        assert report.deleted_items == 0
        assert report.state is RunState.ABORTED  # the sole item is the canary

    async def test_a_movie_still_present_after_the_delete_fails(
        self, session: AsyncSession
    ) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        gateway = _gateway(radarr={1: FakeRadarr(become_gone=False)})

        report = await _real(session, run, gateway)

        assert report.state is RunState.ABORTED
        assert report.deleted_items == 0

    async def test_a_non_canary_failure_does_not_abort_the_run(self, session: AsyncSession) -> None:
        """Two movies: the smaller is the canary and succeeds; the larger fails its
        exclusion. The run completes with one deleted and one failed -- one stubborn item
        is not a reason to abandon the rest."""
        snapshot_id = await _snapshot_many(
            session,
            [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)],
        )
        run = await _plan(session, snapshot_id)
        gateway = _gateway(
            radarr={1: FakeRadarr(fail_ids={2})}  # movie 2's exclusion never lands
        )

        report = await _real(session, run, gateway)

        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 1
        states = {o.media_key: o.state for o in report.outcomes}
        assert states["radarr:1:1"] is StepState.VERIFIED
        assert states["radarr:1:2"] is StepState.FAILED

    async def test_a_missing_instance_route_fails_the_item(self, session: AsyncSession) -> None:
        """A plan targeting Radarr instance 1 with only instance 2 configured must not be
        sent to the wrong server -- it fails the item rather than guessing."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        gateway = _gateway(radarr={2: FakeRadarr()})

        report = await _real(session, run, gateway)

        assert report.state is RunState.ABORTED  # sole item is the canary
        assert report.deleted_items == 0

    async def test_the_delete_is_refused_by_the_guard_when_the_client_is_real(
        self, session: AsyncSession
    ) -> None:
        """Belt-and-suspenders: even inside a 'real' run, a genuine client refuses the
        mutation unless the host is armed. Here the executor thinks it is armed, but the
        client's own transport guard is read-only -- so the call is blocked, not sent."""
        from reaper.clients.arr import RadarrClient

        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        # A real Radarr client whose transport is read-only. movie_by_id (a GET) succeeds
        # via respx, but delete_movie must be refused by the guard.
        import respx

        with respx.mock:
            respx.get("https://radarr.test/api/v3/movie/1").mock(
                return_value=__import__("httpx").Response(
                    200, json={"id": 1, "tmdbId": 5, "sizeOnDisk": 1024**3}
                )
            )
            client = RadarrClient("https://radarr.test", "k", safety=_read_only())
            gateway = _gateway(radarr={1: client})
            async with client:
                report = await _real(session, run, gateway)

        # The guard blocked the delete; it is caught and turned into a failed canary, which
        # aborts the run cleanly. Nothing was deleted, and the process did not crash.
        assert report.deleted_items == 0
        assert report.state is RunState.ABORTED


class TestTheCanaryIsTheFirstRealDelete:
    """The canary is the first item actually deleted, not merely index 0. If the smallest is
    spared or vetoed (skipped, nothing touched), the next item inherits the halt-on-failure
    protection -- because a 'failed' delete can still have removed the file."""

    async def test_a_skipped_smallest_promotes_the_next_delete_to_canary(
        self, session: AsyncSession
    ) -> None:
        from reaper.services import whitelist

        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        run = await _plan(session, snapshot_id)
        await whitelist.spare(session, media_key="radarr:1:1", title="Spared", note=None)
        # The promoted canary (movie 2) fails its exclusion.
        radarr = FakeRadarr(fail_ids={2})

        report = await _real(session, run, _gateway(radarr={1: radarr}))

        # Movie 1 was skipped (spared); movie 2 became the canary, failed, and aborted
        # the run -- the failure was not allowed to pass as a mere per-item failure.
        assert report.state is RunState.ABORTED
        assert report.deleted_items == 0
        assert radarr.delete_calls == [2]  # only the promoted canary was attempted

    async def test_a_promoted_canary_that_succeeds_lets_a_later_failure_be_survivable(
        self, session: AsyncSession
    ) -> None:
        from reaper.services import whitelist

        snapshot_id = await _snapshot_many(
            session,
            [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 5 * GB, 702), ("radarr:1:3", 9 * GB, 703)],
        )
        run = await _plan(session, snapshot_id)
        await whitelist.spare(session, media_key="radarr:1:1", title="Spared", note=None)
        radarr = FakeRadarr(fail_ids={3})  # the last, largest item fails

        report = await _real(session, run, _gateway(radarr={1: radarr}))

        # Movie 1 skipped; movie 2 is the canary and succeeds; movie 3 fails but the run
        # completes, because the canary already proved the mechanism works.
        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 1
        assert radarr.delete_calls == [2, 3]


class TestStreamingVeto:
    async def test_an_actively_streamed_movie_is_spared(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()
        gateway = _gateway(
            radarr={1: radarr},
            plex=FakePlex(streams=[_stream(rating_key=700)]),
        )

        report = await _real(session, run, gateway)

        assert report.skipped == 1
        assert report.deleted_items == 0
        assert radarr.delete_calls == []  # never even attempted

    async def test_watching_an_episode_vetoes_its_whole_season(self, session: AsyncSession) -> None:
        """The stream is an episode (its own rating key), but the prune would take the
        season -- so the veto set includes the episode's parent (season) key, and the
        season is spared."""
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=800, media_type="season"
        )
        run = await _plan(session, snapshot_id)
        sonarr = FakeSonarr()
        gateway = _gateway(
            sonarr={1: sonarr},
            plex=FakePlex(streams=[_stream(rating_key=999, parent=800, grandparent=500)]),
        )

        report = await _real(session, run, gateway)

        assert report.skipped == 1
        assert sonarr.unmonitor_calls == []  # the season was never touched

    async def test_plex_unreadable_fails_closed(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()
        gateway = _gateway(radarr={1: radarr}, plex=FakePlex(raise_on_streams=True))

        report = await _real(session, run, gateway)

        assert report.skipped == 1
        assert radarr.delete_calls == []

    async def test_a_stream_through_the_files_other_listing_vetoes(
        self, session: AsyncSession
    ) -> None:
        """A merged bind is one file listed twice in Plex. The candidate stores the
        canonical key; someone is watching through the OTHER listing. Deleting would cut
        off that very stream, so the veto must cover every key in the group."""
        snapshot_id = await _snapshot_one(
            session, media_key="radarr:1:1", rating_key=700, merged_keys=(700, 950)
        )
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()
        gateway = _gateway(radarr={1: radarr}, plex=FakePlex(streams=[_stream(rating_key=950)]))

        report = await _real(session, run, gateway)

        assert report.skipped == 1
        assert radarr.delete_calls == []


class TestWatchedSinceApproval:
    async def test_a_play_after_approval_spares_the_item(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        after_approval = int((utcnow() + timedelta(hours=1)).timestamp())
        radarr = FakeRadarr()
        gateway = _gateway(
            radarr={1: radarr},
            tautulli=FakeTautulli(rows=[{"stopped": after_approval}]),
        )

        report = await _real(session, run, gateway)

        assert report.skipped == 1
        assert radarr.delete_calls == []

    async def test_a_play_before_approval_does_not_spare(self, session: AsyncSession) -> None:
        """The precise per-row timestamp compare is what matters: a play from before the
        approval instant is ignored even though the coarse date filter would include it."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        before_approval = int((utcnow() - timedelta(days=1)).timestamp())
        radarr = FakeRadarr()
        gateway = _gateway(
            radarr={1: radarr},
            tautulli=FakeTautulli(rows=[{"stopped": before_approval}]),
        )

        report = await _real(session, run, gateway)

        assert report.deleted_items == 1
        assert radarr.delete_calls == [1]

    async def test_tautulli_error_fails_closed(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()
        gateway = _gateway(radarr={1: radarr}, tautulli=FakeTautulli(raise_error=True))

        report = await _real(session, run, gateway)

        assert report.skipped == 1
        assert radarr.delete_calls == []

    async def test_a_present_but_unreadable_row_fails_closed(self, session: AsyncSession) -> None:
        """A history row that survived the date filter but carries no readable timestamp is
        treated as a possible late play and spares the item -- ambiguity keeps the file."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()
        gateway = _gateway(
            radarr={1: radarr},
            tautulli=FakeTautulli(rows=[{"no_timestamp_here": 1}]),
        )

        report = await _real(session, run, gateway)

        assert report.skipped == 1
        assert radarr.delete_calls == []

    async def test_a_play_through_the_files_other_listing_spares(
        self, session: AsyncSession
    ) -> None:
        """A merged bind: the post-approval play was recorded under the file's OTHER
        listing, not the stored canonical key. It is a play of the very file this delete
        would remove, so every key in the group is queried and the item is spared."""
        snapshot_id = await _snapshot_one(
            session, media_key="radarr:1:1", rating_key=700, merged_keys=(700, 950)
        )
        run = await _plan(session, snapshot_id)
        after_approval = int((utcnow() + timedelta(hours=1)).timestamp())
        radarr = FakeRadarr()
        tautulli = FakeTautulli(rows_by_key={950: [{"stopped": after_approval}]})
        gateway = _gateway(radarr={1: radarr}, tautulli=tautulli)

        report = await _real(session, run, gateway)

        assert report.skipped == 1
        assert radarr.delete_calls == []
        assert {c["rating_key"] for c in tautulli.history_calls} == {700, 950}


class TestSeasonLiveSend:
    async def test_a_season_is_unmonitored_verified_then_files_deleted(
        self, session: AsyncSession
    ) -> None:
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=800, media_type="season"
        )
        run = await _plan(session, snapshot_id)
        sonarr = FakeSonarr()  # season 3 has files 101,102; season 4 has 900
        gateway = _gateway(sonarr={1: sonarr})

        report = await _real(session, run, gateway)

        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 1
        assert sonarr.unmonitor_calls == [(42, 3)]
        # Only season 3's files, and never season 4's.
        assert sonarr.delete_calls == [[101, 102]]
        kinds = {s.kind: s.state for s in await _steps(session, run.id)}
        assert kinds["sonarr_unmonitor"] is StepState.VERIFIED
        assert kinds["sonarr_verify_unmonitor"] is StepState.VERIFIED
        assert kinds["sonarr_delete_files"] is StepState.VERIFIED

    async def test_an_unmonitor_that_does_not_take_refuses_to_delete(
        self, session: AsyncSession
    ) -> None:
        """The load-bearing asymmetry: 'files gone, still monitored' re-downloads
        everything. So when the unmonitor does not verify, the file delete never runs."""
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=800, media_type="season"
        )
        run = await _plan(session, snapshot_id)
        sonarr = FakeSonarr(monitored_after_unmonitor=True)  # the unmonitor "did not take"
        gateway = _gateway(sonarr={1: sonarr})

        report = await _real(session, run, gateway)

        assert report.state is RunState.ABORTED  # sole canary failed
        assert sonarr.unmonitor_calls == [(42, 3)]  # attempted
        assert sonarr.delete_calls == []  # but NO files deleted
        kinds = {s.kind: s.state for s in await _steps(session, run.id)}
        assert kinds["sonarr_verify_unmonitor"] is StepState.FAILED
        assert kinds["sonarr_delete_files"] is StepState.SKIPPED


class TestNoRatingKeyIsSpared:
    async def test_an_item_plex_never_matched_is_spared(self, session: AsyncSession) -> None:
        """No Plex rating key means neither the streaming veto nor the played-since check
        can address the item -- an uncertainty, so it is spared, never deleted blind."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=None)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()
        gateway = _gateway(radarr={1: radarr})

        report = await _real(session, run, gateway)

        assert report.skipped == 1
        assert radarr.delete_calls == []


class TestASpareIsHonoredAtExecuteTime:
    """The most dangerous gap the review found: a spare added *after* the plan is built --
    during the grace window this executor exists to honor -- must still stop the delete. A
    spare does not change the frozen candidate row, so neither the verdict nor the manifest
    hash can see it; the executor re-checks the override per item. Two independent reviews
    flagged the original omission, so these tests pin the fix hard."""

    async def test_a_spare_added_after_the_plan_is_not_deleted(self, session: AsyncSession) -> None:
        from reaper.services import whitelist

        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        # The owner changes their mind during grace and spares it.
        await whitelist.spare(session, media_key="radarr:1:1", title="Worthless", note=None)

        radarr = FakeRadarr()
        report = await _real(session, run, _gateway(radarr={1: radarr}))

        # Skipped, not deleted -- and the run still COMPLETES (a spare is not a library
        # change, so the manifest guard does not void the whole run).
        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 0
        assert report.skipped == 1
        assert radarr.delete_calls == []
        assert [s.state for s in await _steps(session, run.id)] == [StepState.SKIPPED]

    async def test_a_spare_at_plan_time_does_not_abort_the_run(self, session: AsyncSession) -> None:
        """The secondary half of the same bug: a spare present *before* planning must not
        make the manifest hashes disagree and abort every execution. The spared item gets
        no steps; the other is deleted normally."""
        from reaper.services import whitelist

        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        await whitelist.spare(session, media_key="radarr:1:2", title="Keep me", note=None)
        run = await _plan(session, snapshot_id)

        # Only the non-spared item has steps.
        assert {s.media_key for s in await _steps(session, run.id)} == {"radarr:1:1"}

        radarr = FakeRadarr()
        report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert report.state is RunState.COMPLETED  # NOT aborted by a manifest mismatch
        assert report.deleted_items == 1
        assert radarr.delete_calls == [1]

    async def test_sparing_a_whole_show_covers_its_condemned_season_at_execute(
        self, session: AsyncSession
    ) -> None:
        from reaper.services import whitelist

        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=800, media_type="season"
        )
        run = await _plan(session, snapshot_id)
        # Spare the SHOW; it must cover the condemned season under it.
        await whitelist.spare(session, media_key="sonarr:1:42", title="A Show", note=None)

        sonarr = FakeSonarr()
        report = await _real(session, run, _gateway(sonarr={1: sonarr}))

        assert report.skipped == 1
        assert sonarr.unmonitor_calls == []  # the season was never touched

    async def test_a_dry_run_shows_a_spared_item_as_skipped(self, session: AsyncSession) -> None:
        from reaper.services import whitelist

        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        await whitelist.spare(session, media_key="radarr:1:1", title="Worthless", note=None)

        report = await Executor(
            session, safety=_read_only(), settings=ProfileSettings(), dry_run=True
        ).execute(run.id)

        assert report.state is RunState.COMPLETED
        assert report.skipped == 1
        assert "spared this by hand" in report.outcomes[0].detail


class TestPlexCleanup:
    async def test_a_mapped_path_is_refreshed_then_trash_purged(
        self, session: AsyncSession
    ) -> None:
        """After the file is gone, Plex is refreshed for the affected path, and (mount up,
        section shrunk by exactly the one delete) the section's trash is purged so no
        stale 'unavailable' entry lingers."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Movies": ["/movies"]}, item_counts={"Movies": [100, 99]})
        gateway = _gateway(radarr={1: FakeRadarr(path="/movies/Worthless (2001)")}, plex=plex)

        report = await _real(session, run, gateway)

        assert report.deleted_items == 1
        assert plex.refreshed == [("Movies", "/movies/Worthless (2001)")]
        assert plex.emptied == ["Movies"]  # the stale entry is purged

    async def test_refresh_and_purge_fire_even_when_the_exclusion_fails(
        self, session: AsyncSession
    ) -> None:
        """The bug behind a stale Plex entry: the run failed at the exclusion check, so Plex
        was never told. The refresh must fire whenever the FILE is gone, regardless."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Movies": ["/movies"]}, item_counts={"Movies": [100, 99]})
        # Exclusion never lands -> the item FAILS, but the file is gone.
        gateway = _gateway(
            radarr={1: FakeRadarr(path="/movies/Worthless", land_exclusion=False)}, plex=plex
        )

        report = await _real(session, run, gateway)

        assert report.state is RunState.ABORTED  # the canary failed its exclusion check
        assert plex.refreshed == [("Movies", "/movies/Worthless")]  # ...but Plex was refreshed
        assert plex.emptied == ["Movies"]  # ...and the stale entry purged

    async def test_the_trash_is_not_purged_when_a_mount_is_down(
        self, session: AsyncSession
    ) -> None:
        """The mass-loss guard: if an *arr root folder is not accessible, the volume may be
        gone and the trash full of merely-unreachable items -- so we refuse to empty it."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Movies": ["/movies"]})
        gateway = _gateway(
            radarr={1: FakeRadarr(path="/movies/Worthless", root_accessible=False)}, plex=plex
        )

        report = await _real(session, run, gateway)

        assert report.deleted_items == 1  # the delete itself still happened
        assert plex.refreshed == [("Movies", "/movies/Worthless")]
        assert plex.emptied == []  # but the trash was NOT purged

    async def test_an_unmapped_path_is_skipped_without_failing(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Movies": ["/some/other/root"]})
        gateway = _gateway(radarr={1: FakeRadarr(path="/movies/Worthless")}, plex=plex)

        report = await _real(session, run, gateway)

        assert report.deleted_items == 1  # the delete still succeeded
        assert plex.refreshed == []  # refresh silently skipped, never fatal
        assert plex.emptied == []  # nothing to purge

    async def test_the_trash_is_not_purged_when_the_section_shrank_by_more_than_we_deleted(
        self, session: AsyncSession
    ) -> None:
        """The mass-loss guard the count-delta gate exists for: a mount flap on the PLEX
        host (invisible to the *arr root-folder check, a different mount) trashed hundreds
        of entries while we deleted one movie. The section shrank by far more than this
        run deleted, so purging would destroy those items' library records -- refuse."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Movies": ["/movies"]}, item_counts={"Movies": [400, 99]})
        gateway = _gateway(radarr={1: FakeRadarr(path="/movies/Worthless")}, plex=plex)

        report = await _real(session, run, gateway)

        assert report.deleted_items == 1  # the reap itself succeeded
        assert plex.refreshed == [("Movies", "/movies/Worthless")]
        assert plex.emptied == []  # but the trash was NOT purged

    async def test_the_trash_is_not_purged_when_plex_never_confirmed_the_delete(
        self, session: AsyncSession
    ) -> None:
        """No shrink at all means Plex has not confirmed OUR removals either (on some
        servers trashed items still count toward the section size). Purging without
        confirmation is refused; the stale entry is cosmetic and Plex's own maintenance
        will clear it."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Movies": ["/movies"]})  # count never changes
        gateway = _gateway(radarr={1: FakeRadarr(path="/movies/Worthless")}, plex=plex)

        report = await _real(session, run, gateway)

        assert report.deleted_items == 1
        assert plex.refreshed == [("Movies", "/movies/Worthless")]
        assert plex.emptied == []

    async def test_a_sibling_section_sharing_a_path_prefix_is_never_claimed(
        self, session: AsyncSession
    ) -> None:
        """A section rooted at /media/movies must not claim files under /media/movies-4k:
        matching on a raw string prefix would refresh -- and trash-purge -- the wrong
        section. The path must sit inside the location at a path-component boundary."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        plex = FakePlex(
            sections={"Movies": ["/media/movies"], "Movies 4K": ["/media/movies-4k"]},
            item_counts={"Movies 4K": [50, 49]},
        )
        gateway = _gateway(
            radarr={1: FakeRadarr(path="/media/movies-4k/Worthless (2001)")}, plex=plex
        )

        report = await _real(session, run, gateway)

        assert report.deleted_items == 1
        assert plex.refreshed == [("Movies 4K", "/media/movies-4k/Worthless (2001)")]
        assert plex.emptied == ["Movies 4K"]  # never "Movies"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fake_candidate(media_key: str, size: int | None) -> Candidate:
    return Candidate(
        media_key=media_key,
        title="x",
        media_type="movie",
        size_bytes=size,
        size_source=SizeSource.RADARR if size is not None else None,
        verdict="condemn",
        score=90,
        coverage_bp=10_000,
        explanation_json="{}",
    )


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


# -- real-send helpers ------------------------------------------------------


async def _unmeasure(session: AsyncSession, media_key: str, run: ReapRun) -> None:
    """Stage a plan the planner would never have produced: one holding an unmeasured item.

    The planner holds these back, so this is the only way to reach the executor's own
    per-item refusal, which exists precisely because the host-side layer must not depend
    on the plan being right.

    Re-stamping the manifest hash is part of the staging, not a workaround. Taking a size
    away legitimately voids the approval -- the hash binds the frozen set, and a resize is
    a different set -- so without this the run aborts on THAT check and never reaches the
    one under test. Re-approving says "assume the plan was built this way from the start".
    """
    candidate = (
        await session.execute(select(Candidate).where(Candidate.media_key == media_key))
    ).scalar_one()
    candidate.size_bytes = None
    candidate.size_source = None
    await session.flush()

    condemned = await effective_condemned(
        session, run.snapshot_id, await whitelist.overrides(session)
    )
    run.approved_manifest_hash = manifest_hash(
        sorted(condemned.values(), key=lambda c: c.media_key)
    )
    await session.flush()


def _source_for(media_type: str) -> SizeSource:
    """What a real scan would have stamped on this row.

    Every fixture needs one. The executor compares the frozen size against a live re-read
    and refuses to compare two different quantities, so a candidate with a size but no
    recorded source is kept, exactly as an unmeasured one is. Leaving it off would make a
    fixture look like it was reaped when the run had actually declined to touch it.
    """
    return SizeSource.SONARR if media_type == "season" else SizeSource.RADARR


async def _snapshot_one(
    session: AsyncSession,
    *,
    media_key: str,
    rating_key: int | None,
    size: int | None = 1 * GB,
    media_type: str = "movie",
    merged_keys: tuple[int, ...] = (),
) -> int:
    """A snapshot with one condemned candidate carrying a Plex rating key.

    Distinct from ``_snapshot_with`` because the real send needs ``plex_rating_key`` (the
    streaming veto and played-since checks address the item by it) and, for TV, a
    ``media_type`` of ``season``. ``merged_keys`` writes the match block a merged bind
    stores (one file listed several times in Plex), which those same checks re-read."""
    explanation = (
        json.dumps({"match": {"merged_rating_keys": list(merged_keys)}}) if merged_keys else "{}"
    )
    return await _snapshot_many(
        session, [(media_key, size, rating_key)], media_type=media_type, explanation=explanation
    )


async def _snapshot_many(
    session: AsyncSession,
    items: list[tuple[str, int | None, int | None]],
    *,
    media_type: str = "movie",
    explanation: str = "{}",
    group_key: str | None = None,
) -> int:
    now = utcnow()
    snapshot = Snapshot(
        created_at=now,
        policy_hash="p" * 64,
        scoring_hash="s" * 64,
        horizon_at=now,
        item_count=len(items),
    )
    session.add(snapshot)
    await session.flush()
    for i, (media_key, size, rating_key) in enumerate(items):
        session.add(
            Candidate(
                snapshot_id=snapshot.id,
                media_key=media_key,
                title=f"Worthless {i}",
                media_type=media_type,
                size_bytes=size,
                size_source=_source_for(media_type) if size is not None else None,
                plex_rating_key=rating_key,
                group_key=group_key,
                verdict="condemn",
                score=90,
                coverage_bp=10_000,
                explanation_json=explanation,
                created_at=now,
            )
        )
    await session.flush()
    return snapshot.id


async def _plan(session: AsyncSession, snapshot_id: int) -> ReapRun:
    return await build_plan(
        session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
    )


async def _armed_forever() -> bool:
    """The default arm re-check for tests: the switch never flips mid-run."""
    return True


async def _real(
    session: AsyncSession,
    run: ReapRun,
    gateway: ReapGateway,
    *,
    armed_recheck: Any = _armed_forever,
    stop_recheck: Any = None,
    settings: ProfileSettings | None = None,
) -> RunReport:
    """Execute a run for real (armed) against the given gateway of fakes. Zero poll delay so
    the exclusion-verification retry does not slow the suite."""
    executor = Executor(
        session,
        safety=_armed(),
        settings=settings or ProfileSettings(),
        dry_run=False,
        gateway=gateway,
        armed_recheck=armed_recheck,
        stop_recheck=stop_recheck,
        exclusion_poll_delay=0.0,
        plex_settle_delay=0.0,
    )
    return await executor.execute(run.id)


def _gateway(
    *,
    radarr: dict[int, Any] | None = None,
    sonarr: dict[int, Any] | None = None,
    plex: Any = None,
    tautulli: Any = None,
) -> ReapGateway:
    """A gateway with sensible empty-but-present Plex/Tautulli so a real run is not refused
    for missing them. Individual tests override to exercise a specific interlock."""
    return ReapGateway(
        radarr=radarr or {},
        sonarr=sonarr or {},
        plex=plex if plex is not None else FakePlex(),
        tautulli=tautulli if tautulli is not None else FakeTautulli(),
    )


def _stream(
    *, rating_key: int, parent: int | None = None, grandparent: int | None = None
) -> ActiveStream:
    return ActiveStream(
        rating_key=rating_key,
        parent_rating_key=parent,
        grandparent_rating_key=grandparent,
        user="someone",
    )


class FakeRadarr:
    """A stand-in Radarr for the movie delete path. Records what it was asked to delete and
    lets a test dictate whether the exclusion lands and whether the movie really goes."""

    def __init__(
        self,
        *,
        tmdb_id: int = 555,
        land_exclusion: bool = True,
        become_gone: bool = True,
        path: str = "/movies/Worthless",
        fail_ids: set[int] | None = None,
        exclusion_appears_after: int = 0,
        root_accessible: bool = True,
        size_on_disk: int | None = 256 * 1024**2,
    ) -> None:
        self._tmdb_id = tmdb_id
        self._land_exclusion = land_exclusion
        self._become_gone = become_gone
        self._path = path
        self._root_accessible = root_accessible
        # What sizeOnDisk reports; small by default so the drift interlock stays quiet in
        # tests about other things. None omits the field (an unreadable size).
        self._size_on_disk = size_on_disk
        self._fail_ids = fail_ids or set()  # ids whose exclusion never lands
        # Radarr adds the exclusion a beat after the delete's 200. This simulates that lag:
        # an exclusion added on read N only becomes visible from read N + this many.
        self._exclusion_appears_after = exclusion_appears_after
        self._exclusion_reads = 0
        self._exclusions: list[dict[str, Any]] = []  # each {"tmdbId", "_visible_at"}
        self._deleted: set[int] = set()
        self.delete_calls: list[int] = []

    async def movie_by_id(self, movie_id: int) -> dict[str, Any]:
        if movie_id in self._deleted and self._become_gone:
            raise IntegrationError("radarr", "movie not found", status=404)
        movie = {"id": movie_id, "tmdbId": self._tmdb_id + movie_id, "path": self._path}
        if self._size_on_disk is not None:
            movie["sizeOnDisk"] = self._size_on_disk
        return movie

    async def delete_movie(
        self, movie_id: int, *, delete_files: bool = True, add_exclusion: bool = True
    ) -> None:
        self.delete_calls.append(movie_id)
        self._deleted.add(movie_id)
        lands = self._land_exclusion and movie_id not in self._fail_ids
        if add_exclusion and lands:
            self._exclusions.append(
                {
                    "tmdbId": self._tmdb_id + movie_id,
                    "_visible_at": self._exclusion_reads + self._exclusion_appears_after,
                }
            )

    async def exclusions(self) -> list[dict[str, Any]]:
        self._exclusion_reads += 1
        return [
            {"tmdbId": e["tmdbId"]}
            for e in self._exclusions
            if e["_visible_at"] < self._exclusion_reads
        ]

    async def root_folders(self) -> list[dict[str, Any]]:
        return [{"path": "/movies", "accessible": self._root_accessible}]


class FakeSonarr:
    """A stand-in Sonarr for the season prune path."""

    def __init__(
        self,
        *,
        season: int = 3,
        monitored_after_unmonitor: bool = False,
        files: list[dict[str, Any]] | None = None,
    ) -> None:
        self._season = season
        self._monitored = True
        self._monitored_after = monitored_after_unmonitor
        # Sizes are small by default so the drift interlock stays quiet in tests about
        # other things (candidates default to 1 GB).
        self._files = files or [
            {"id": 101, "seasonNumber": season, "size": 50 * 1024**2},
            {"id": 102, "seasonNumber": season, "size": 50 * 1024**2},
            # A different season -- must be untouched.
            {"id": 900, "seasonNumber": season + 1, "size": 50 * 1024**2},
        ]
        self.unmonitor_calls: list[tuple[int, int]] = []
        self.delete_calls: list[list[int]] = []

    async def series_by_id(self, series_id: int) -> dict[str, Any]:
        return {
            "id": series_id,
            "seasons": [
                {"seasonNumber": self._season, "monitored": self._monitored},
                {"seasonNumber": self._season + 1, "monitored": True},
            ],
        }

    async def unmonitor_season(self, series_id: int, season_number: int) -> None:
        self.unmonitor_calls.append((series_id, season_number))
        self._monitored = self._monitored_after

    async def episode_files(self, series_id: int) -> list[dict[str, Any]]:
        return [dict(f) for f in self._files]

    async def delete_episode_files(self, episode_file_ids: list[int]) -> None:
        self.delete_calls.append(list(episode_file_ids))
        self._files = [f for f in self._files if f["id"] not in episode_file_ids]

    async def root_folders(self) -> list[dict[str, Any]]:
        return [{"path": "/tv", "accessible": True}]


class FakePlex:
    """A stand-in Plex: controllable streams, section paths, and a record of refreshes.

    ``item_counts`` scripts what ``item_count`` returns per section, in read order (the
    last value repeats), so a test can make a section "shrink" by exactly what was
    deleted -- or by more, to prove the purge refuses. The default never shrinks, which
    the count-delta gate treats as unconfirmed: no purge.
    """

    def __init__(
        self,
        *,
        streams: list[ActiveStream] | None = None,
        raise_on_streams: bool = False,
        sections: dict[str, list[str]] | None = None,
        item_counts: dict[str, list[int]] | None = None,
    ) -> None:
        self._streams = streams or []
        self._raise = raise_on_streams
        self._sections = sections or {}
        self._item_counts = {k: list(v) for k, v in (item_counts or {}).items()}
        self.refreshed: list[tuple[str, str]] = []
        self.emptied: list[str] = []

    async def active_streams(self) -> list[ActiveStream]:
        if self._raise:
            raise PlexError("cannot read sessions")
        return list(self._streams)

    async def section_paths(self) -> dict[str, list[str]]:
        return dict(self._sections)

    async def refresh_path(self, section_title: str, path: str) -> None:
        self.refreshed.append((section_title, path))

    async def is_refreshing(self, section_title: str) -> bool:
        return False

    async def item_count(self, section_title: str) -> int:
        scripted = self._item_counts.get(section_title)
        if not scripted:
            return 100
        return scripted.pop(0) if len(scripted) > 1 else scripted[0]

    async def empty_trash(self, section_title: str) -> None:
        self.emptied.append(section_title)


class FakeTautulli:
    """A stand-in Tautulli whose history rows and error behavior a test controls.

    ``rows`` answers every key alike; ``rows_by_key`` answers per rating key (empty for
    keys not listed), for the merged-listings tests where WHICH key was played matters.
    """

    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        rows_by_key: dict[int, list[dict[str, Any]]] | None = None,
        raise_error: bool = False,
    ) -> None:
        self._rows = rows or []
        self._rows_by_key = rows_by_key
        self._raise = raise_error
        self.history_calls: list[dict[str, Any]] = []

    async def history(
        self,
        *,
        rating_key: int | None = None,
        parent_rating_key: int | None = None,
        after: str | None = None,
    ) -> dict[str, Any]:
        self.history_calls.append(
            {"rating_key": rating_key, "parent_rating_key": parent_rating_key, "after": after}
        )
        if self._raise:
            raise IntegrationError("tautulli", "history unavailable")
        if self._rows_by_key is not None:
            key = rating_key if rating_key is not None else parent_rating_key
            return {"data": list(self._rows_by_key.get(key or 0, []))}
        return {"data": list(self._rows)}


# ---------------------------------------------------------------------------
# Journal durability and the atomic EXECUTING claim
# ---------------------------------------------------------------------------


class _DyingRadarr(FakeRadarr):
    """Succeeds on the first delete, then simulates the process dying on the second."""

    async def delete_movie(
        self, movie_id: int, *, delete_files: bool = True, add_exclusion: bool = True
    ) -> None:
        if len(self.delete_calls) >= 1:
            raise RuntimeError("process died mid-send")
        await super().delete_movie(movie_id, delete_files=delete_files, add_exclusion=add_exclusion)


class _BlockingPlex(FakePlex):
    """Parks the run inside its first live interlock until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def active_streams(self) -> list[ActiveStream]:
        self.entered.set()
        await self.release.wait()
        return []


class TestJournalDurability:
    """The action journal is committed at every state change, never held inside one
    run-long transaction. Kill the process after item 1 of 2 has deleted: the run must
    still read EXECUTING with item 1 VERIFIED and item 2 SENT from a fresh session --
    never roll back to PLANNED as if no file were gone."""

    async def test_a_crash_mid_run_leaves_a_durable_journal(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
        engine = create_engine(settings)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)

        try:
            async with factory() as session:
                snapshot_id = await _snapshot_many(
                    session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
                )
                run = await _plan(session, snapshot_id)
                run_id = run.id
                await session.commit()

            async with factory() as run_session:
                executor = Executor(
                    run_session,
                    safety=_armed(),
                    settings=ProfileSettings(),
                    dry_run=False,
                    gateway=_gateway(radarr={1: _DyingRadarr()}),
                    armed_recheck=_armed_forever,
                    exclusion_poll_delay=0.0,
                    plex_settle_delay=0.0,
                )
                with pytest.raises(RuntimeError, match="process died"):
                    await executor.execute(run_id)
                # Deliberately no commit: the process "died" here.

            # A fresh session -- a restart -- must see the durable truth.
            async with factory() as fresh:
                run_row = await fresh.get(ReapRun, run_id)
                assert run_row is not None
                assert run_row.state is RunState.EXECUTING, (
                    "a crashed run must stay EXECUTING, not roll back to PLANNED"
                )
                steps = {s.media_key: s for s in await _steps(fresh, run_id)}
                # The canary (smallest, first) really deleted and verified.
                assert steps["radarr:1:1"].state is StepState.VERIFIED
                # The second item was declared in flight before the send.
                assert steps["radarr:1:2"].state is StepState.SENT
        finally:
            await engine.dispose()


class TestTheExecutingClaimIsAtomic:
    """The 'a run executes once' guard is an atomic UPDATE ... WHERE state='planned',
    committed before the first send -- so a second execute arriving while the first is
    mid-run is refused, instead of re-running the plan over the first one's journal."""

    async def test_a_second_execute_while_one_is_in_flight_is_refused(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
        engine = create_engine(settings)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)

        try:
            async with factory() as session:
                snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
                run = await _plan(session, snapshot_id)
                run_id = run.id
                await session.commit()

            plex = _BlockingPlex()
            radarr = FakeRadarr()

            async with factory() as first_session, factory() as second_session:
                first = Executor(
                    first_session,
                    safety=_armed(),
                    settings=ProfileSettings(),
                    dry_run=False,
                    gateway=_gateway(radarr={1: radarr}, plex=plex),
                    armed_recheck=_armed_forever,
                    exclusion_poll_delay=0.0,
                    plex_settle_delay=0.0,
                )
                task = asyncio.create_task(first.execute(run_id))
                # The first run is now parked mid-run, AFTER committing its claim.
                await asyncio.wait_for(plex.entered.wait(), timeout=5)

                second = Executor(
                    second_session,
                    safety=_armed(),
                    settings=ProfileSettings(),
                    dry_run=False,
                    gateway=_gateway(radarr={1: radarr}, plex=plex),
                    armed_recheck=_armed_forever,
                    exclusion_poll_delay=0.0,
                    plex_settle_delay=0.0,
                )
                with pytest.raises(ExecutionError, match="executing"):
                    await second.execute(run_id)

                plex.release.set()
                report = await asyncio.wait_for(task, timeout=5)
                await first_session.commit()

            assert report.state is RunState.COMPLETED
            # Exactly one delete was ever issued.
            assert radarr.delete_calls == [1]
        finally:
            await engine.dispose()


class TestRollingThirtyDayCaps:
    """The 30-day budget is enforced at execute time, over verified deletions: no
    sequence of runs may exceed the rolling caps. Abort, never truncate."""

    @staticmethod
    def _settings(**overrides: int) -> ProfileSettings:
        base: dict[str, int] = {
            "max_items_per_run": 10,
            "max_bytes_per_run": 400 * 10**9,
            "max_items_per_30d": 100,
            "max_bytes_per_30d": 500 * 10**9,
        }
        base.update(overrides)
        return ProfileSettings(**base)  # type: ignore[arg-type]

    async def _execute(
        self, session: AsyncSession, run_id: int, settings: ProfileSettings, *, dry: bool = False
    ) -> RunReport:
        executor = Executor(
            session,
            safety=_read_only() if dry else _armed(),
            settings=settings,
            dry_run=dry,
            gateway=None if dry else _gateway(radarr={1: FakeRadarr()}),
            armed_recheck=None if dry else _armed_forever,
            exclusion_poll_delay=0.0,
            plex_settle_delay=0.0,
        )
        return await executor.execute(run_id)

    async def test_a_run_that_would_exceed_the_rolling_byte_cap_aborts(
        self, session: AsyncSession
    ) -> None:
        settings = self._settings()

        # Run 1: 300 GB, well within the per-run cap and the 30-day budget.
        first_snapshot = await _snapshot_one(
            session, media_key="radarr:1:1", rating_key=701, size=300 * 10**9
        )
        first = await _plan(session, first_snapshot)
        report = await self._execute(session, first.id, settings)
        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 1

        # Run 2: another 300 GB fits the per-run cap but blows the 500 GB rolling budget.
        second_snapshot = await _snapshot_one(
            session, media_key="radarr:1:2", rating_key=702, size=300 * 10**9
        )
        second = await _plan(session, second_snapshot)
        radarr = FakeRadarr()
        executor = Executor(
            session,
            safety=_armed(),
            settings=settings,
            dry_run=False,
            gateway=_gateway(radarr={1: radarr}),
            armed_recheck=_armed_forever,
            exclusion_poll_delay=0.0,
        )
        report = await executor.execute(second.id)

        assert report.state is RunState.ABORTED
        assert "30 days" in (report.aborted_reason or "")
        assert radarr.delete_calls == []  # nothing was deleted: abort, never truncate

    async def test_a_dry_run_reports_the_same_rolling_refusal(self, session: AsyncSession) -> None:
        settings = self._settings()
        first_snapshot = await _snapshot_one(
            session, media_key="radarr:1:1", rating_key=701, size=300 * 10**9
        )
        first = await _plan(session, first_snapshot)
        assert (await self._execute(session, first.id, settings)).deleted_items == 1

        second_snapshot = await _snapshot_one(
            session, media_key="radarr:1:2", rating_key=702, size=300 * 10**9
        )
        second = await _plan(session, second_snapshot)
        report = await self._execute(session, second.id, settings, dry=True)

        assert report.state is RunState.ABORTED
        assert "30 days" in (report.aborted_reason or "")
        # And the dry run did not consume the plan.
        assert (await session.get(ReapRun, second.id)).state is RunState.PLANNED  # type: ignore[union-attr]

    async def test_the_rolling_item_cap_counts_past_verified_items(
        self, session: AsyncSession
    ) -> None:
        settings = self._settings(max_items_per_run=2, max_items_per_30d=3)

        first_snapshot = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 2 * GB, 702)]
        )
        first = await _plan(session, first_snapshot)
        assert (await self._execute(session, first.id, settings)).deleted_items == 2

        second_snapshot = await _snapshot_many(
            session, [("radarr:1:3", 1 * GB, 703), ("radarr:1:4", 2 * GB, 704)]
        )
        second = await _plan(session, second_snapshot)
        report = await self._execute(session, second.id, settings)

        assert report.state is RunState.ABORTED
        assert "rolling cap of 3" in (report.aborted_reason or "")

    async def test_deletions_older_than_thirty_days_fall_out_of_the_window(
        self, session: AsyncSession
    ) -> None:
        settings = self._settings()
        first_snapshot = await _snapshot_one(
            session, media_key="radarr:1:1", rating_key=701, size=300 * 10**9
        )
        first = await _plan(session, first_snapshot)
        assert (await self._execute(session, first.id, settings)).deleted_items == 1

        # Age the verified deletion out of the window.
        for step in await _steps(session, first.id):
            step.verified_at = utcnow() - timedelta(days=40)
        await session.commit()

        second_snapshot = await _snapshot_one(
            session, media_key="radarr:1:2", rating_key=702, size=300 * 10**9
        )
        second = await _plan(session, second_snapshot)
        report = await self._execute(session, second.id, settings)

        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 1
