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

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clients.base import IntegrationError
from reaper.clients.plex import ActiveStream, PlexError
from reaper.clock import utcnow
from reaper.config import RuntimeSafety, Settings
from reaper.db.base import Base
from reaper.db.models import ActionStep, Candidate, ReapRun, RunState, Snapshot, StepState
from reaper.db.session import create_engine, create_session_factory
from reaper.engine.policy import ProfileSettings
from reaper.services.executor import ExecutionError, Executor, ReapGateway, RunReport
from reaper.services.planner import (
    MediaRef,
    PlanError,
    build_plan,
    confirmation_phrase,
    manifest_hash,
)

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
    """A snapshot plus a set of condemned movie candidates: (media_key, size_bytes)."""
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
        assert phrase == "REAP 2 ITEMS 214 GB"


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
            (
                await session.execute(
                    select(Candidate).where(Candidate.snapshot_id == snapshot_id)
                )
            )
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

    async def test_a_mixed_movie_and_season_plan_dry_runs_both(
        self, session: AsyncSession
    ) -> None:
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
        """The stale-tab defence. Approve a plan, then the library moves; the run must
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
        assert "aborted, not truncated" in report.aborted_reason.lower()
        assert report.deleted_items == 0
        # This is a DRY run, so the abort is a *finding*, not a consumed run: the row stays
        # PLANNED and re-runnable, so raising the cap and running again just works.
        stored = await session.get(ReapRun, run.id)
        assert stored is not None
        assert stored.state is RunState.PLANNED

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

    async def test_a_really_executed_run_cannot_be_re_executed(
        self, session: AsyncSession
    ) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)

        first = await _real(session, run, _gateway(radarr={1: FakeRadarr()}))
        assert first.state is RunState.COMPLETED

        with pytest.raises(ExecutionError, match="executes once"):
            await _real(session, run, _gateway(radarr={1: FakeRadarr()}))


# ---------------------------------------------------------------------------
# The real send -- against fakes, so the whole live path is proven without a server.
# ---------------------------------------------------------------------------


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
        assert report.aborted_reason is not None and "canary" in report.aborted_reason.lower()

    async def test_a_movie_still_present_after_the_delete_fails(
        self, session: AsyncSession
    ) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        gateway = _gateway(radarr={1: FakeRadarr(become_gone=False)})

        report = await _real(session, run, gateway)

        assert report.state is RunState.ABORTED
        assert report.deleted_items == 0

    async def test_a_non_canary_failure_does_not_abort_the_run(
        self, session: AsyncSession
    ) -> None:
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
                return_value=__import__("httpx").Response(200, json={"id": 1, "tmdbId": 5})
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

    async def test_watching_an_episode_vetoes_its_whole_season(
        self, session: AsyncSession
    ) -> None:
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


class TestASpareIsHonouredAtExecuteTime:
    """The most dangerous gap the review found: a spare added *after* the plan is built --
    during the grace window this executor exists to honour -- must still stop the delete. A
    spare does not change the frozen candidate row, so neither the verdict nor the manifest
    hash can see it; the executor re-checks the override per item. Two independent reviews
    flagged the original omission, so these tests pin the fix hard."""

    async def test_a_spare_added_after_the_plan_is_not_deleted(
        self, session: AsyncSession
    ) -> None:
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

    async def test_a_spare_at_plan_time_does_not_abort_the_run(
        self, session: AsyncSession
    ) -> None:
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
        """After the file is gone, Plex is refreshed for the affected path, and (mount up)
        the section's trash is purged so no stale 'unavailable' entry lingers."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Movies": ["/movies"]})
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
        plex = FakePlex(sections={"Movies": ["/movies"]})
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

    async def test_an_unmapped_path_is_skipped_without_failing(
        self, session: AsyncSession
    ) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Movies": ["/some/other/root"]})
        gateway = _gateway(radarr={1: FakeRadarr(path="/movies/Worthless")}, plex=plex)

        report = await _real(session, run, gateway)

        assert report.deleted_items == 1  # the delete still succeeded
        assert plex.refreshed == []  # refresh silently skipped, never fatal
        assert plex.emptied == []  # nothing to purge


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fake_candidate(media_key: str, size: int) -> Candidate:
    return Candidate(
        media_key=media_key,
        title="x",
        media_type="movie",
        size_bytes=size,
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


async def _snapshot_one(
    session: AsyncSession,
    *,
    media_key: str,
    rating_key: int | None,
    size: int = 1 * GB,
    media_type: str = "movie",
) -> int:
    """A snapshot with one condemned candidate carrying a Plex rating key.

    Distinct from ``_snapshot_with`` because the real send needs ``plex_rating_key`` (the
    streaming veto and played-since checks address the item by it) and, for TV, a
    ``media_type`` of ``season``."""
    return await _snapshot_many(session, [(media_key, size, rating_key)], media_type=media_type)


async def _snapshot_many(
    session: AsyncSession,
    items: list[tuple[str, int, int | None]],
    *,
    media_type: str = "movie",
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
                plex_rating_key=rating_key,
                verdict="condemn",
                score=90,
                coverage_bp=10_000,
                explanation_json="{}",
                created_at=now,
            )
        )
    await session.flush()
    return snapshot.id


async def _plan(session: AsyncSession, snapshot_id: int) -> ReapRun:
    return await build_plan(
        session, snapshot_id=snapshot_id, policy_hash="p" * 64, approved_by="admin"
    )


async def _real(session: AsyncSession, run: ReapRun, gateway: ReapGateway) -> RunReport:
    """Execute a run for real (armed) against the given gateway of fakes. Zero poll delay so
    the exclusion-verification retry does not slow the suite."""
    executor = Executor(
        session,
        safety=_armed(),
        settings=ProfileSettings(),
        dry_run=False,
        gateway=gateway,
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
    ) -> None:
        self._tmdb_id = tmdb_id
        self._land_exclusion = land_exclusion
        self._become_gone = become_gone
        self._path = path
        self._root_accessible = root_accessible
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
        return {"id": movie_id, "tmdbId": self._tmdb_id + movie_id, "path": self._path}

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
        self._files = files or [
            {"id": 101, "seasonNumber": season},
            {"id": 102, "seasonNumber": season},
            {"id": 900, "seasonNumber": season + 1},  # a different season -- must be untouched
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
    """A stand-in Plex: controllable streams, section paths, and a record of refreshes."""

    def __init__(
        self,
        *,
        streams: list[ActiveStream] | None = None,
        raise_on_streams: bool = False,
        sections: dict[str, list[str]] | None = None,
    ) -> None:
        self._streams = streams or []
        self._raise = raise_on_streams
        self._sections = sections or {}
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

    async def empty_trash(self, section_title: str) -> None:
        self.emptied.append(section_title)


class FakeTautulli:
    """A stand-in Tautulli whose history rows and error behaviour a test controls."""

    def __init__(
        self, *, rows: list[dict[str, Any]] | None = None, raise_error: bool = False
    ) -> None:
        self._rows = rows or []
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
        return {"data": list(self._rows)}
