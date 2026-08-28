# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests the planner and the executor, the two halves of the reap loop.

This is the code that turns a verdict into a deleted file, so it gets the most
adversarial tests in the suite. Every test here is really asking whether this code can be
made to delete something it should not.

Nothing here sends a real request. The executor runs in dry-run mode, which walks the
whole plan and every interlock but sends nothing. The transport guard sits underneath as
an independent backstop, proven separately in test_guarded_transport and test_plex_guard.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest
import respx
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from reaper.api.runs import STEP_PAGE, _planned_candidates, _run_out
from reaper.clients.base import IntegrationError
from reaper.clients.plex import ActiveStream, PlexError, PlexSectionPaths
from reaper.clock import utcnow
from reaper.config import RuntimeSafety, Settings
from reaper.db.base import Base
from reaper.db.models import (
    ActionStep,
    Candidate,
    Instance,
    InstanceKind,
    Policy,
    ReapRun,
    RunState,
    SizeSource,
    Snapshot,
    StepState,
)
from reaper.db.session import create_engine, create_session_factory
from reaper.engine.policy import DEFAULT_MOVIE_POLICY, PolicyBody, ProfileSettings
from reaper.engine.reason import Reason, from_stored
from reaper.refusal import english
from reaper.services import list_config, whitelist
from reaper.services import whitelist as whitelist_module
from reaper.services.condemned import effective_condemned
from reaper.services.executor import (
    _CHECK_GREW_SINCE_APPROVED,
    _CHECK_SIZE_UNCONFIRMED,
    _NO_APPROVED_SIZE_CHECK,
    ExecutionError,
    Executor,
    ReapGateway,
    ReapProgress,
    RunReport,
    _common_parent,
    _deletable_bytes,
    _Delete,
    _grew_materially,
    _JournalRow,
    _row_timestamp,
    _Terminal,
)
from reaper.services.planner import (
    MediaRef,
    PlanError,
    build_plan,
    confirmation_phrase,
    manifest_hash,
)
from reaper.services.profiles import live_policy_hash, save_profile_settings

from ._reasons import refusal_text

GB = 1024**3


def _label_text(reason: Any) -> str:
    """Renders a ``StepCheck.label`` or ``StepOutcome.detail`` reason into English, using
    the real ``reaper.refusal.MESSAGES`` catalog. Use this in assertions that check the
    words a checklist line shows, not just which code fired."""
    return refusal_text(reason.id, **reason.params)


@pytest.fixture
async def session(async_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with async_factory() as s:
        yield s


async def _stored_run(async_factory: async_sessionmaker[AsyncSession], run_id: int) -> ReapRun:
    """Reads the run row from the database, through a session the test never wrote to.

    A fresh session has an empty identity map, so this ``get`` issues a real SELECT. A
    terminal state the executor only set in memory would otherwise read back as whatever
    is actually on disk. The returned row is detached, with every column already loaded.
    """
    async with async_factory() as fresh:
        stored = await fresh.get(ReapRun, run_id)
        assert stored is not None, f"run {run_id} is not on disk at all"
        return stored


async def _seed_instances(session: AsyncSession, media_keys: Iterable[str]) -> None:
    """Seeds the Instance rows a plan resolves each candidate's media_key against.

    A scan only condemns items from instances that exist, so a real plan reads a live row
    for every candidate, and ``build_plan`` refuses a movie whose Radarr is gone. These
    tests build candidates directly, so they seed the matching instances themselves.

    This function is safe to call more than once. A test that already seeded an instance
    to set its own exclusion setting (``_seed_radarr``) is left untouched. A freshly
    seeded row carries the exclusion on. One row per id, since the planner keys on the id
    alone, never the kind.
    """
    existing = set((await session.execute(select(Instance.id))).scalars().all())
    for instance_id in sorted({int(key.split(":")[1]) for key in media_keys}):
        if instance_id in existing:
            continue
        session.add(
            Instance(
                id=instance_id,
                kind=InstanceKind.RADARR,
                name=f"i{instance_id}",
                base_url="https://arr.test",
                api_key_enc="enc",
                add_import_exclusion=True,
                created_at=utcnow(),
            )
        )
    await session.flush()


async def _snapshot_with(session: AsyncSession, condemned: Sequence[tuple[str, int | None]]) -> int:
    """A snapshot plus a set of condemned movie candidates: (media_key, size_bytes).

    A ``None`` size is an item nothing would measure, which is not the same as a zero."""
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
        plan is safe. Silently *mis-routing* one is not, and the gap between them is a
        parse nobody checked. ``radarr:1:2:3`` is a four-part *radarr* key. Only TV
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
    inert. The order is load-bearing. Unmonitor, verify the unmonitor, then delete."""

    async def _plan_steps(self, session: AsyncSession, media_key: str) -> list[ActionStep]:
        snapshot_id = await _snapshot_with(session, [(media_key, 4 * GB)])
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="tester")
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
        It must be skipped. The run is built but carries no steps for it, so nothing is
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
        """Ordinal 0 is the item executed and verified alone before any other. It must be
        the least costly possible mistake."""
        snapshot_id = await _snapshot_with(
            session,
            [("radarr:1:1", 50 * GB), ("radarr:1:2", 1 * GB), ("radarr:1:3", 9 * GB)],
        )

        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

        steps = await _steps(session, run.id)
        canary = next(s for s in steps if s.ordinal == 0)
        assert canary.media_key == "radarr:1:2"  # the 1 GB one

    async def test_a_step_is_journalled_before_anything_is_sent(
        self, session: AsyncSession
    ) -> None:
        """The whole safety model: the row, with its exact request, exists before the
        call. Credentials are NOT in it, so it is safe to keep and to render."""
        snapshot_id = await _snapshot_with(session, [("radarr:2:42", 5 * GB)])

        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

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

        # The refusal reaches the operator as the Reap page's 422 body, so it is checked
        # as operator copy. It matches the sentence the three incomplete-scan notices lead
        # with, carries no snapshot id, and never says "degraded", which is internal
        # vocabulary. The stored reason goes last, so an unterminated one cannot run into
        # what follows.
        with pytest.raises(PlanError, match="came back incomplete, so Reaper won't act on it"):
            await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")
        with pytest.raises(PlanError, match=r"Radarr 4K unreachable$"):
            await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

    async def test_an_empty_condemned_set_is_refused(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(session, [])
        with pytest.raises(PlanError, match="othing is condemned"):
            await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")


class TestARestrictedPlanReapsOnlyTheChosenItems:
    """``only_media_keys`` is the "reap just these" path. It lets a first, single,
    hand-picked delete happen without building a plan over the whole condemned set. It
    changes only which items get steps. The manifest still binds to the whole condemned
    set.
    """

    async def test_it_plans_steps_for_only_the_chosen_key(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 1 * GB), ("radarr:1:2", 5 * GB), ("radarr:1:3", 9 * GB)]
        )
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            approved_by="admin",
            only_media_keys={"radarr:1:2"},
        )
        steps = await _steps(session, run.id)
        assert {s.media_key for s in steps} == {"radarr:1:2"}
        # The manifest still covers the whole condemned set, so any shift voids it.
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
                approved_by="admin",
                only_media_keys={"radarr:1:999"},
            )

    async def test_a_spared_key_is_refused_with_a_distinct_message(
        self, session: AsyncSession
    ) -> None:
        from reaper.services import whitelist

        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 1 * GB)])
        await whitelist.set_override(
            session, media_key="radarr:1:1", title="Kept", decision="spare", note=None
        )
        with pytest.raises(PlanError, match="spared"):
            await build_plan(
                session,
                snapshot_id=snapshot_id,
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
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

        executor = Executor(session, safety=_read_only(), settings=ProfileSettings(), dry_run=True)
        report = await executor.execute(run.id)

        assert report.state is RunState.COMPLETED
        assert report.dry_run is True
        assert report.deleted_items == 0  # nothing was actually deleted
        # Every step is recorded as what it would have done, as the raw request sequence,
        # not as an English "would" sentence.
        assert all(o.state is StepState.SKIPPED for o in report.outcomes)
        assert all(o.detail.id == "error.reap.step.dry_run" for o in report.outcomes)
        assert any(
            "DELETE /api/v3/movie/1" in str(o.detail.params["plan"]) for o in report.outcomes
        )

    async def test_the_first_outcome_is_flagged_as_the_canary(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 9 * GB), ("radarr:1:2", 1 * GB)]
        )
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

        report = await Executor(
            session, safety=_read_only(), settings=ProfileSettings(), dry_run=True
        ).execute(run.id)

        # The canary is the smallest (1 GB, movie 2) and it is executed first.
        assert report.outcomes[0].is_canary
        assert report.outcomes[0].media_key == "radarr:1:2"


class TestASeasonDryRunsAsAWholeSequence:
    """A condemned season is one delete unit with three steps, unmonitor, verify, and
    delete files. The dry run proves the whole sequence without sending any of it. The
    half-applied state "files gone, still monitored" makes Sonarr re-download what was
    removed, so the file delete comes last, and the executor treats the three steps as
    one item, not three.
    """

    async def test_all_three_steps_are_skipped_and_shown(
        self, session: AsyncSession, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        snapshot_id = await _snapshot_with(session, [("sonarr:1:42:3", 4 * GB)])
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")
        # Committed as PLANNED before the dry run, so the read below is a real check.
        # Anything the dry run consumed would have to overwrite this row to pass.
        await session.commit()

        report = await Executor(
            session, safety=_read_only(), settings=ProfileSettings(), dry_run=True
        ).execute(run.id)

        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 0
        # One outcome for the item, named by its terminal (irreversible) delete, and
        # listing the whole ordered sequence it would have sent.
        outcome = report.outcomes[0]
        assert outcome.kind == "sonarr_delete_files"
        assert outcome.detail.id == "error.reap.step.dry_run"
        plan = str(outcome.detail.params["plan"])
        assert "POST /api/v3/seasonpass" in plan
        assert "GET /api/v3/series/42" in plan
        assert "DELETE /api/v3/episodefile/bulk" in plan
        assert outcome.is_canary  # the sole item is ordinal 0

        # A dry run consumes nothing. The run stays PLANNED and every step stays PENDING,
        # so the plan can still be dry-run again, and can still be executed for real
        # afterwards. This reads through a session the executor never touched, since its
        # own session would answer both of these from its identity map without asking
        # the database.
        assert (await _stored_run(async_factory, run.id)).state is RunState.PLANNED
        async with async_factory() as fresh:
            steps = await _steps(fresh, run.id)
        assert steps and all(s.state is StepState.PENDING for s in steps)

    async def test_a_mixed_movie_and_season_plan_dry_runs_both(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 1 * GB), ("sonarr:1:42:3", 4 * GB)]
        )
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")
        report = await Executor(
            session, safety=_read_only(), settings=ProfileSettings(), dry_run=True
        ).execute(run.id)

        assert report.state is RunState.COMPLETED
        assert {o.kind for o in report.outcomes} == {"radarr_delete", "sonarr_delete_files"}


class TestTheManifestGuard:
    async def test_a_changed_condemned_set_voids_the_run(self, session: AsyncSession) -> None:
        """The stale-tab defense. Approve a plan, then let the library change. The run
        must refuse rather than execute a plan nobody approved."""
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 1 * GB), ("radarr:1:2", 5 * GB)]
        )
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

        # A new item gets condemned after approval, so the plan is now different.
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
    async def test_a_run_over_the_item_cap_aborts_entirely(
        self, session: AsyncSession, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The whole run stops rather than deleting the part that fits. Truncating would
        make which items are deleted depend on sort order."""
        condemned = [(f"radarr:1:{i}", 1 * GB) for i in range(5)]
        snapshot_id = await _snapshot_with(session, condemned)
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")
        # On disk as PLANNED before the dry run, so the read below checks the real state.
        await session.commit()

        settings = ProfileSettings(max_items_per_run=3, max_items_per_30d=100)
        report = await Executor(
            session, safety=_read_only(), settings=settings, dry_run=True
        ).execute(run.id)

        assert report.state is RunState.ABORTED
        assert report.aborted_reason is not None
        assert report.aborted_reason.id == "error.reap.items_over_run_cap"
        assert report.deleted_items == 0
        # This is a DRY run, so the abort is a *finding*, not a consumed run: the row stays
        # PLANNED and re-runnable, so raising the cap and running again just works.
        assert (await _stored_run(async_factory, run.id)).state is RunState.PLANNED

    async def test_caps_off_lets_a_run_over_the_cap_proceed(self, session: AsyncSession) -> None:
        """With the caps switched off, a plan larger than the per-run cap no longer
        aborts. Turning off the run-size ceiling is the only thing this switch does.
        Every other gate stays on, and this dry run still sends nothing."""
        condemned = [(f"radarr:1:{i}", 1 * GB) for i in range(5)]
        snapshot_id = await _snapshot_with(session, condemned)
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

        # A cap of 3 would abort five items, but caps_enabled=False turns that cap off.
        settings = ProfileSettings(caps_enabled=False, max_items_per_run=3, max_items_per_30d=100)
        report = await Executor(
            session, safety=_read_only(), settings=settings, dry_run=True
        ).execute(run.id)

        assert report.state is RunState.COMPLETED
        assert report.aborted_reason is None

    async def test_caps_off_skips_the_rolling_and_byte_caps_too(
        self, session: AsyncSession
    ) -> None:
        """The switch drops every run-size ceiling, including the per-run byte cap and
        the rolling 30-day caps, not only the per-run item cap. A plan over the per-run
        byte cap and the rolling 30-day caps still completes with caps off. Each cap here
        is set below the plan, so any cap still enforced would abort it."""
        condemned = [(f"radarr:1:{i}", 400 * GB) for i in range(5)]  # 5 items, 2000 GB
        snapshot_id = await _snapshot_with(session, condemned)
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

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
        self, session: AsyncSession, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A real run over the cap does consume the run, unlike a dry run. The row is
        marked ABORTED on disk, and still nothing is deleted."""
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
        assert (await _stored_run(async_factory, run.id)).state is RunState.ABORTED

    async def test_a_run_over_the_byte_cap_aborts(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 400 * GB), ("radarr:1:2", 400 * GB)]
        )
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

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
        With the ceiling down, the executor refuses at the top, and the transport guard
        would refuse again below it. Two layers, and neither one is trusted alone."""
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 1 * GB)])
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

        with pytest.raises(ExecutionError, match="Deletion is turned off") as excinfo:
            await Executor(
                session, safety=_read_only(), settings=ProfileSettings(), dry_run=False
            ).execute(run.id)
        assert excinfo.value.code == "error.safety.deletion_off"

    async def test_a_real_run_without_clients_is_refused(self, session: AsyncSession) -> None:
        """Being armed is not enough. A real run also needs the clients to delete
        through, and to run the streaming veto and the played-since check. With no
        gateway it refuses loudly before touching anything, rather than silently
        proceeding blind."""
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 1 * GB)])
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

        with pytest.raises(ExecutionError, match="no clients configured"):
            await Executor(
                session, safety=_armed(), settings=ProfileSettings(), dry_run=False
            ).execute(run.id)

    async def test_a_real_run_without_plex_is_refused(self, session: AsyncSession) -> None:
        """No Plex means no active-stream veto. Deleting blind to who is watching is the
        one thing that must never happen, so the run refuses."""
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 1 * GB)])
        run = await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")
        gateway = ReapGateway(radarr={1: FakeRadarr()}, plex=None, tautulli=FakeTautulli())

        with pytest.raises(ExecutionError, match="without Plex"):
            await Executor(
                session, safety=_armed(), settings=ProfileSettings(), dry_run=False, gateway=gateway
            ).execute(run.id)


class TestARunExecutesOnce:
    async def test_a_dry_run_is_repeatable_and_does_not_consume_the_plan(
        self, session: AsyncSession
    ) -> None:
        """A dry run is a simulation, so it can be run as many times as you like, and it
        leaves the plan runnable for real afterwards. A dry run must never complete the
        run itself, or "dry run, then execute for real" would break in the UI.
        """
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        dry = Executor(session, safety=_read_only(), settings=ProfileSettings(), dry_run=True)

        first = await dry.execute(run.id)
        second = await dry.execute(run.id)  # repeatable, no "executes once"
        assert first.state is RunState.COMPLETED and second.state is RunState.COMPLETED

        # The plan is still runnable for real. A real execute now succeeds.
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
# The real send, against fakes, so the whole live path is proven without a server.
# ---------------------------------------------------------------------------


class TestGrewMaterially:
    """The size-drift allowance. Growth within a tenth, or the 256 MiB floor for small
    items, is jitter. Anything past it is an upgrade the owner never approved."""

    def test_growth_past_a_tenth_is_material(self) -> None:
        assert _grew_materially(10 * GB, 12 * GB)

    def test_growth_within_a_tenth_is_tolerated(self) -> None:
        assert not _grew_materially(10 * GB, int(10.5 * GB))

    def test_small_items_get_the_floor_not_the_tenth(self) -> None:
        # A tenth of 100 MiB is 10 MiB, too small to matter. The 256 MiB floor governs.
        approved = 100 * 1024**2
        assert not _grew_materially(approved, approved + 200 * 1024**2)
        assert _grew_materially(approved, approved + 300 * 1024**2)

    def test_shrinking_is_never_material(self) -> None:
        # Deleting less than approved is the safe direction.
        assert not _grew_materially(10 * GB, 1 * GB)
        assert not _grew_materially(10 * GB, 0)


class TestTheSizeInterlocksChecklistLines:
    """The three sentences the size re-read puts on the checklist, held to their wording.

    The identity assertions on each skip below say a site reads the right CONSTANT. They do
    not say what the constant SAYS, so re-typing either string left the suite green while
    the operator read whatever was typed instead. These assertions are the second copy of
    that text, written down here on purpose so the two can be checked against each other.
    """

    def test_the_grew_line_says_what_happened_and_what_became_of_the_file(self) -> None:
        assert refusal_text(_CHECK_GREW_SINCE_APPROVED.id) == "It grew since you approved it. Kept."

    def test_the_unconfirmed_line_says_what_happened_and_what_became_of_the_file(self) -> None:
        assert (
            refusal_text(_CHECK_SIZE_UNCONFIRMED.id) == "Couldn't confirm its current size. Kept."
        )

    def test_the_never_measured_line_says_what_happened_and_what_became_of_the_file(self) -> None:
        assert (
            refusal_text(_NO_APPROVED_SIZE_CHECK.id)
            == "No size was recorded for it at scan time. Kept."
        )


class TestSizeDriftReRead:
    """The live size is re-read immediately before anything is sent. A file that grew
    materially since approval was upgraded, and the approval, the caps, and the typed
    phrase all counted the smaller file. So the item is kept, never deleted unconfirmed.
    """

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
        assert report.outcomes[0].detail.id == "error.reap.step.grew_since_approved_movie"
        assert report.outcomes[0].checks[0].label == _CHECK_GREW_SINCE_APPROVED

    async def test_a_movie_whose_size_cannot_be_read_is_kept(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr(size_on_disk=None)  # Radarr reports no sizeOnDisk at all

        report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert radarr.delete_calls == []
        assert report.skipped == 1
        assert report.outcomes[0].checks[0].label == _CHECK_SIZE_UNCONFIRMED

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
        # Within a tenth of the approved size, so this counts as jitter, not an upgrade.
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

        # The skip fired before even the reversible unmonitor, so the season is untouched.
        assert sonarr.unmonitor_calls == []
        assert sonarr.delete_calls == []
        assert report.skipped == 1
        assert report.outcomes[0].detail.id == "error.reap.step.grew_since_approved_season"
        # The same checklist line the movie case above asserts. Only the movie's wording
        # had ever been checked elsewhere. A grep for this season's wording found it only
        # in `executor.py`.
        assert report.outcomes[0].checks[0].label == _CHECK_GREW_SINCE_APPROVED

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
        assert report.outcomes[0].checks[0].label == _CHECK_SIZE_UNCONFIRMED
        # Sonarr listed a file and would not size it, which IS a size problem. This is the
        # arm the empty-list skip above was folded into, so it is pinned from both sides.
        assert report.outcomes[0].detail.id == "error.reap.step.size_unconfirmed_season"

    async def test_a_season_whose_files_all_report_zero_bytes_is_kept(
        self, session: AsyncSession
    ) -> None:
        """Closes the path a fabricated zero would open through the size interlock.

        The same partial payload that makes a season's stored size 0 at scan time would
        also make its live file sizes read as 0 at delete time. Zero against zero looks
        like no growth at all, so a naive check would pass and delete real files based on
        two invented numbers. `_payload_size` reads 0 as unreadable instead, exactly as
        the scan-side parsers do.
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
        """An empty answer is not a confirmation, and it is not a size problem either.

        `sum([])` is 0, which sails through the growth check and marks the step verified
        having proven nothing, while still consuming the canary, because the plan is
        ordered smallest-first and a zero-size season sorts to the front.

        The wording is pinned here because this message must stay distinct from the
        unreadable-size message. Sending an operator to Sonarr to look for a file with a
        missing size, when Sonarr reported no files at all, sends them looking for
        something that does not exist. The season is kept either way, so only the wording
        tells the two cases apart.
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
        outcome = report.outcomes[0]
        # Verbatim. It says nothing about a size, which is the point of this message.
        assert outcome.detail.id == "error.reap.step.season_no_files"
        assert outcome.detail.params == {"season": 3}
        assert (
            refusal_text(outcome.detail.id, **outcome.detail.params)
            == "Sonarr lists no files for season 3, so there is nothing to delete. Kept."
        )
        # The post-unmonitor skip uses the same line without its unmonitor clause, pinned
        # verbatim there too (``TestASeasonWithNothingLeftToDelete``). Reword both
        # together, or neither.
        assert refusal_text(outcome.checks[0].label.id) == "No files left to remove. Kept."

    async def test_a_drift_skip_does_not_consume_the_canary(self, session: AsyncSession) -> None:
        """The skipped item touched no file, so the next item still gets the canary's
        halt-on-failure protection. This is the same behavior as every other pre-send
        skip."""
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

        # Movie 1 was kept because of drift. Movie 2 became the canary, failed, and
        # aborted the run.
        assert report.state is RunState.ABORTED
        assert radarr.delete_calls == [2]


class TestAnApprovedSizeThatWasNeverConfirmed:
    """An item nothing would size is not deletable, and two independent layers say so.

    The growth check cannot police the approved side. It compares the live size against
    the frozen number, so ``_grew_materially(None-as-0, live)`` would reduce to
    ``live > 256 MiB`` and stay silent for every smaller file. So the refusal happens
    earlier and twice over. The **planner** never puts such an item in a plan, which
    keeps the caps and the typed confirmation exact by construction. The **executor**
    refuses it again per item, and does not trust the plan alone to have done that job.
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
                approved_by="admin",
                only_media_keys={"radarr:1:2"},
            )

    async def test_a_movie_with_no_approved_size_is_kept(self, session: AsyncSession) -> None:
        """Tests the executor's own layer, by planning a measured item and then taking
        its size away. The plan is wrong, so the host-side check has to hold anyway."""
        snapshot_id = await _snapshot_one(
            session, media_key="radarr:1:1", rating_key=700, size=1 * GB
        )
        run = await _plan(session, snapshot_id)
        await _unmeasure(session, "radarr:1:1", run)
        # A real, readable, ordinary-sized file, under the drift floor, so the growth
        # check would have seen no growth and let this delete through.
        radarr = FakeRadarr(size_on_disk=200 * 1024**2)

        report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert radarr.delete_calls == []  # nothing was sent
        assert report.skipped == 1
        assert report.state is RunState.COMPLETED  # a skip is a protection, not a failure
        assert report.outcomes[0].detail.id == "error.reap.step.no_approved_size"
        assert report.outcomes[0].checks[0].label == _NO_APPROVED_SIZE_CHECK

    async def test_a_season_with_no_approved_size_is_kept_before_the_unmonitor(
        self, session: AsyncSession
    ) -> None:
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=800, media_type="season", size=1 * GB
        )
        run = await _plan(session, snapshot_id)
        await _unmeasure(session, "sonarr:1:42:3", run)
        # Sonarr reports every file's size fine, so the live-side refusal never fires.
        # Only the approved side is unconfirmed, and the season totals well under the
        # drift floor.
        sonarr = FakeSonarr()

        report = await _real(session, run, _gateway(sonarr={1: sonarr}))

        assert sonarr.unmonitor_calls == []  # not even the reversible half ran
        assert sonarr.delete_calls == []
        assert report.skipped == 1
        assert report.outcomes[0].detail.id == "error.reap.step.no_approved_size"
        assert report.outcomes[0].checks[0].label == _NO_APPROVED_SIZE_CHECK

    async def test_a_size_measured_against_a_different_thing_is_kept(
        self, session: AsyncSession
    ) -> None:
        """A size alone is not enough. It has to measure the same thing the live re-read
        measures.

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

        Two items can be deleted. The third is never planned, so a cap of two is not
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
        """Checks the dangerous direction, where counting 0 would let a run pass a cap it
        does not really fit.

        With the unmeasured item left out, the run is 400 GB against a 500 GB cap and
        completes. The item left out is kept, not deleted off-budget.
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
        """The manifest binds what the owner approved, so the two encodings must not
        collide.

        If an unknown size were encoded as ``0``, a size later measured as 0 would leave
        the hash unchanged, and the stale approval would still execute. Encoded as JSON
        ``null`` it is a different set, which voids the approval. That is correct. The
        owner approved a set containing an item nobody could size, and it is no longer
        that set.
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
        """Both ``_run_out`` and the execute route recompute this phrase and compare it
        byte for byte, so any change of shape 409s every execute."""
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 1 * GB)])
        run = await _plan(session, snapshot_id)

        assert confirmation_phrase(await _planned_candidates(session, run)) == "REAP 1 SOUL 1 GB"

    async def test_the_phrase_names_the_whole_plan_when_the_response_shows_a_window(
        self, session: AsyncSession
    ) -> None:
        """Pins the reason the cap sits where it does.

        ``_run_out`` serializes a WINDOW of the journal. The number the owner types must
        still describe the whole plan, so the window is applied only to the serialization,
        never to ``_run_steps`` or ``_planned_candidates``. Both of those feed the phrase,
        on this route and again inside ``execute_run``. A ``LIMIT`` added to either would
        shrink what is shown and what the server expects by the same amount, so the typed
        phrase would still match while ``services.executor`` loads its own steps and
        deletes every one of them.

        This test is driven at a size no other test in this suite reaches. The largest
        plan anywhere else is five items, and a cap of fifty is invisible below that.
        """
        keys = [(f"radarr:1:{n}", GB) for n in range(1, 61)]
        snapshot_id = await _snapshot_with(session, keys)
        run = await _plan(session, snapshot_id)

        out = await _run_out(session, run)

        assert len(out.steps) == STEP_PAGE
        assert out.step_count == 60
        assert out.item_count == 60
        assert out.confirmation_phrase == "REAP 60 SOULS 60 GB"
        # What the dangerous implementation would have produced. Asserted rather than implied,
        # because the two phrases differ only in a number and the wrong one is the plausible one.
        planned = await _planned_candidates(session, run)
        assert confirmation_phrase(planned[:STEP_PAGE]) == "REAP 50 SOULS 50 GB"
        assert out.confirmation_phrase != "REAP 50 SOULS 50 GB"

    def test_an_unmeasured_item_reaching_the_byte_sum_aborts_the_run(self) -> None:
        """The tripwire under every byte cap, tested directly because nothing can reach
        it through the normal path.

        ``_deletable`` filters unmeasured items out first, so this branch is unreachable
        through ``execute()``. That is exactly why it needs its own test. Without one,
        deleting this ``raise`` outright, or softening it to a log line, would still pass
        the rest of the suite. Note which way it fails. A stand-in zero understates the
        total, an understated total understates the cap, and a cap that does not fire
        deletes past what the owner approved. This is the one lane where rounding toward
        keeping the file is backwards.
        """
        deletable = [
            _Delete(steps=(), candidate=_fake_candidate("radarr:1:1", 10 * GB)),
            _Delete(steps=(), candidate=_fake_candidate("radarr:1:2", None)),
        ]

        with pytest.raises(ExecutionError, match="couldn't measure the size"):
            _deletable_bytes(deletable, allow_unmeasured=False)

    def test_the_allowance_totals_what_it_could_measure_instead_of_aborting(self) -> None:
        """The other branch. With the allowance open, the unmeasured item is a legitimate
        member of the set, so the sum reports the measured bytes rather than refusing the
        run.

        This test cannot tell "left out of the total" from "summed as the zero its stored
        size implies". Both produce 10 GB, so that distinction cannot be checked at this
        function's interface. What bounds the unmeasured item is the item cap, not this
        number, which is why the allowance is a count rather than a size.
        """
        deletable = [
            _Delete(steps=(), candidate=_fake_candidate("radarr:1:1", 10 * GB)),
            _Delete(steps=(), candidate=_fake_candidate("radarr:1:2", None)),
        ]

        assert _deletable_bytes(deletable, allow_unmeasured=True) == 10 * GB


class TestTheUnmeasuredAllowance:
    """``max_unmeasured_per_run`` above zero lets a bounded number of unmeasured items be
    reaped. These tests pin everything else that stays enforced.

    It exists because "never" is the wrong answer for an operator with a handful of items
    their *arr will not size. It is a count and not a switch, because an unmeasured item
    contributes nothing to either byte cap. The byte caps cannot bound this population at
    all, so the count is the only bound there is.
    """

    async def test_the_test_item_is_never_an_unmeasured_one(self, session: AsyncSession) -> None:
        """The single most important check here. The canary's whole purpose is a first
        mistake whose cost is known in advance, so an unmeasured canary brings back the
        original problem under a new setting. No configuration may reintroduce it.

        The unmeasured item looks like the smallest thing in the plan by any naive
        reading, since it has no size at all. A combined sort, or a key that treats None
        as 0, would seat it at ordinal 0. That is exactly the accident this test checks
        against.
        """
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", None), ("radarr:1:2", 5 * GB), ("radarr:1:3", 1 * GB)]
        )
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            approved_by="admin",
            max_unmeasured=3,
        )

        ordered = [s.media_key for s in await _steps(session, run.id)]
        # Smallest measured first, and the unmeasured one last however small it looks.
        assert ordered == ["radarr:1:3", "radarr:1:2", "radarr:1:1"]

    async def test_over_the_allowance_aborts_rather_than_trimming(
        self, session: AsyncSession
    ) -> None:
        """Planning the first N would let sort order pick which unmeasured file gets
        deleted, which is the accident the whole design avoids. Same abort-not-truncate
        discipline the byte caps keep."""
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", None), ("radarr:1:2", None), ("radarr:1:3", 1 * GB)]
        )

        with pytest.raises(PlanError, match="over your limit"):
            await build_plan(
                session,
                snapshot_id=snapshot_id,
                approved_by="admin",
                max_unmeasured=1,
            )

    async def test_the_phrase_names_what_the_gb_figure_does_not_cover(
        self, session: AsyncSession
    ) -> None:
        """The GB figure stays exact for the items it describes, so the owner has to type
        an acknowledgment that the run holds things it does not.

        This setting is saved to the profile, not merely passed to ``build_plan``. The
        review surface reads the allowance live, so if only one of the two knew about it,
        the phrase shown and the phrase recomputed at execute time would differ, and
        every execute would 409.
        """
        await save_profile_settings(
            session, ProfileSettings(max_items_per_run=10, max_unmeasured_per_run=2)
        )
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", None), ("radarr:1:2", 10 * GB)])
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
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
        assert report.aborted_reason is not None
        assert report.aborted_reason.id == "error.reap.items_over_run_cap"
        assert report.aborted_reason.params.get("items") == 3

    async def test_caps_off_still_enforces_the_unknown_size_limit(
        self, session: AsyncSession
    ) -> None:
        """The caps switch drops the run-size caps, never the keep-unknown-size rule. With
        caps off and the unknown-size allowance lowered after approval below what the plan
        admitted, the run still aborts on the unmeasured count. That guard is checked
        before the run-size caps, and the switch does not govern it."""
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", None), ("radarr:1:2", None), ("radarr:1:3", 1 * GB)]
        )
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            approved_by="admin",
            max_unmeasured=2,
        )

        # Caps OFF, but the unknown-size allowance was lowered to 1 after approval.
        settings = ProfileSettings(caps_enabled=False, max_unmeasured_per_run=1)
        report = await Executor(
            session, safety=_read_only(), settings=settings, dry_run=True
        ).execute(run.id)

        assert report.state is RunState.ABORTED
        assert report.aborted_reason is not None
        assert report.aborted_reason.id == "error.reap.unmeasured_over_limit"

    async def test_they_never_contribute_zero_to_a_byte_cap(self, session: AsyncSession) -> None:
        """The tempting shortcut once they can be planned is to let them count as 0 bytes
        so the arithmetic keeps working. That would reopen the same bug through a
        different path. The byte total must describe only what was actually measured."""
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", None), ("radarr:1:2", 400 * GB)]
        )
        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
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
            approved_by="admin",
            max_unmeasured=1,
        )

        radarr = FakeRadarr()
        # Approved under an allowance of 1. Executed under 0.
        report = await _real(session, run, _gateway(radarr={1: radarr}))

        # The measured item was deleted. The unmeasured one was kept by the lowered
        # allowance.
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
            approved_by="admin",
            only_media_keys={"sonarr:1:42"},
            max_unmeasured=5,
        )

        planned = {s.media_key for s in await _steps(session, run.id)}
        assert planned == {"sonarr:1:42:1"}


class TestUsingTheAllowanceDoesNotBrickTheNextThirtyDays:
    """The rolling 30-day window reads past VERIFIED deletions back off their frozen
    candidate rows. An allowed unmeasured item leaves a row whose size is NULL forever,
    so from the moment one is deleted, the window contains one on every later run.

    Aborting on that would make one use of the allowance disable reaping entirely for a
    month, dry runs included, with no way out. Skipping the row would be the other
    failure. The window would read light and spend past the monthly budget. It counts as
    an item and contributes no bytes, which is the only reading that avoids both failures.
    """

    async def test_a_past_unmeasured_deletion_does_not_abort_later_runs(
        self, session: AsyncSession
    ) -> None:
        # A measured item rides along. A plan of only unmeasured items has no safe test
        # item and is refused outright.
        past = await _snapshot_many(
            session, [("radarr:1:1", None, 701), ("radarr:1:2", 1 * GB, 702)]
        )
        first = await build_plan(
            session,
            snapshot_id=past,
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
            approved_by="admin",
            max_unmeasured=1,
        )
        allowing = ProfileSettings(max_items_per_run=10, max_unmeasured_per_run=1)
        await _real(session, first, _gateway(radarr={1: FakeRadarr()}), settings=allowing)

        later = await _snapshot_with(session, [("radarr:1:9", 1 * GB)])
        second = await build_plan(session, snapshot_id=later, approved_by="admin")
        # Two already spent this month, and a cap of two.
        capped = ProfileSettings(max_items_per_run=2, max_items_per_30d=2, max_unmeasured_per_run=1)
        report = await Executor(
            session, safety=_read_only(), settings=capped, dry_run=True
        ).execute(second.id)

        assert report.state is RunState.ABORTED
        assert report.aborted_reason is not None
        assert report.aborted_reason.id == "error.reap.items_over_rolling_cap"


class TestTheAllowanceIsACountNotASwitch:
    """The executor must enforce the NUMBER, not merely whether it is above zero.

    The one thing that makes this setting safe to keep out of the policy hash is that
    both directions of a change resolve toward keeping. That only holds if a tightening
    is actually honored at execute time. Otherwise lowering 25 to 1 would be silently
    ignored on the one population no byte cap can bound.
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
            approved_by="admin",
            max_unmeasured=2,
        )

        # Approved under 2, executed under 1. This is not zero, so a boolean read would
        # have admitted both. The count must refuse the run instead.
        tightened = ProfileSettings(max_items_per_run=10, max_unmeasured_per_run=1)
        report = await Executor(
            session, safety=_read_only(), settings=tightened, dry_run=True
        ).execute(run.id)

        assert report.state is RunState.ABORTED
        assert report.aborted_reason is not None
        assert report.aborted_reason.id == "error.reap.unmeasured_over_limit"


class TestTheCanaryRuleHoldsWhenNothingIsMeasured:
    """Sorting the unmeasured tail last is necessary but not sufficient. With nothing
    measured to sort ahead of it, the tail IS the plan and ordinal 0 has unknown cost."""

    async def test_a_plan_of_only_unmeasured_items_is_refused(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_with(session, [("radarr:1:1", None), ("radarr:1:2", None)])

        with pytest.raises(PlanError, match="nothing safe to test"):
            await build_plan(
                session,
                snapshot_id=snapshot_id,
                approved_by="admin",
                max_unmeasured=5,
            )

    async def test_reap_just_these_over_only_unmeasured_items_is_refused(
        self, session: AsyncSession
    ) -> None:
        """The narrowed set is the one that matters for the canary check, not the whole
        library. A library holding plenty of measured items does not help here, because
        "Reap just these" narrows the set before the canary check runs. A hand-picked
        selection made entirely of unmeasured items would still get a plan whose ordinal
        0 has unknown cost.
        """
        snapshot_id = await _snapshot_with(
            session,
            [("radarr:1:1", 1 * GB), ("radarr:1:2", 2 * GB), ("radarr:1:3", None)],
        )

        with pytest.raises(PlanError, match="nothing safe to test"):
            await build_plan(
                session,
                snapshot_id=snapshot_id,
                approved_by="admin",
                max_unmeasured=5,
                only_media_keys={"radarr:1:3"},
            )

    async def test_a_selection_keeping_one_measured_item_still_plans(
        self, session: AsyncSession
    ) -> None:
        """The control. The refusal is about having no canary, not about the selection
        containing an unmeasured item, so a mixed pick still plans, with the measured
        item first."""
        snapshot_id = await _snapshot_with(
            session,
            [("radarr:1:1", 1 * GB), ("radarr:1:2", 2 * GB), ("radarr:1:3", None)],
        )

        run = await build_plan(
            session,
            snapshot_id=snapshot_id,
            approved_by="admin",
            max_unmeasured=5,
            only_media_keys={"radarr:1:1", "radarr:1:3"},
        )

        ordered = [s.media_key for s in await _steps(session, run.id)]
        assert ordered[0] == "radarr:1:1"  # the canary has a known cost


class TestTheHeldBackNoticeSurvivesTheAllowance:
    """Turning the allowance ON must not make the plan LESS honest than leaving it off."""

    async def test_a_show_reap_still_reports_the_seasons_it_dropped(
        self, session: AsyncSession
    ) -> None:
        """An unmeasured season never rides a show-level click, whatever the allowance.
        That is deliberate. But it means the click plans fewer seasons than the queue
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
            approved_by="admin",
            only_media_keys={"sonarr:1:42"},
            max_unmeasured=5,
        )

        assert {s.media_key for s in await _steps(session, run.id)} == {"sonarr:1:42:1"}
        assert run.held_back_unknown_size == 1

    async def test_the_funnel_line_counts_the_seasons_a_show_click_selected(
        self, session: AsyncSession
    ) -> None:
        """One click on a show sends one group_key and plans its seasons, so the count the
        caller sent is not the count the plan was narrowed to. Reporting only the first
        would make `planner.built` read `requested=1, planned=3`, an inverted funnel on
        the one line that exists to explain "the queue showed 40 and my plan has 12".
        """
        snapshot_id = await _snapshot_many(
            session,
            [
                ("sonarr:1:42:1", 1 * GB, 801),
                ("sonarr:1:42:2", 2 * GB, 802),
                ("sonarr:1:42:3", 3 * GB, 803),
            ],
            media_type="season",
            group_key="sonarr:1:42",
        )

        with capture_logs() as logs:
            run = await build_plan(
                session,
                snapshot_id=snapshot_id,
                approved_by="admin",
                only_media_keys={"sonarr:1:42"},
                max_unmeasured=0,
            )

        built = next(line for line in logs if line["event"] == "planner.built")
        assert built["requested"] == 1  # what the click sent
        assert built["selected"] == 3  # what it expanded to, which is what was narrowed on
        # `planned` counts items, where a season carries several steps.
        assert built["planned"] == len({s.media_key for s in await _steps(session, run.id)}) == 3

    async def test_a_show_with_no_measurable_season_says_which_show(
        self, session: AsyncSession
    ) -> None:
        """The refusal must name the show. Saying only "these items are not condemned in
        this snapshot" is true of the key and completely misleading about the show."""
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
                approved_by="admin",
                only_media_keys={"sonarr:1:42"},
            )


class TestDisarmMidRun:
    """Turning deletion off mid-run stops the run before its next item. Files already
    verified deleted stay deleted. Nothing further is sent."""

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
        assert report.aborted_reason is not None
        assert report.aborted_reason.id == "error.reap.deletion_disabled_mid_run"
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
    """Pressing Stop halts the run gracefully before its next item, like disarming, but it
    leaves deletion armed. Whatever was removed before the halt still has its stale Plex
    entry tidied. A stopped run cleans up Plex exactly as a completed one does."""

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
        assert report.aborted_reason is not None
        assert report.aborted_reason.id == "error.reap.stopped_by_operator"
        assert radarr.delete_calls == [1]  # the second item was never attempted
        assert report.deleted_items == 1  # the first stays deleted and recorded

    async def test_a_stopped_run_still_tidies_plex(self, session: AsyncSession) -> None:
        """Whatever was removed before the halt still gets its stale Plex entry refreshed
        and purged, so nothing is left showing as unavailable."""
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
        # Refreshed for the file that was removed, and its stale entry purged, on a
        # stopped run exactly as on a completed one.
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

    async def test_a_hard_cancel_marks_aborted_and_defers_the_plex_tidy_up(
        self, session: AsyncSession, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A hard cancel mid-run, the app shutting down or a force-stop, is not the
        graceful Stop. It arrives as CancelledError, not ExecutionError. The executor
        must still mark the run ABORTED before the cancellation propagates, so shutdown
        never leaves the run EXECUTING.

        It must not tidy Plex. The purge polls each affected section for up to
        ``_PLEX_SETTLE_ATTEMPTS * _plex_settle_delay`` before it can even decide, so
        honoring it here would hold the container's shutdown open for tens of seconds per
        section, and could empty a section's trash while the process is being torn down.
        The queued rescans go with it: they are sent at the end of a run, and a run that
        was canceled has no end to send them from. The whole tidy-up is cosmetic. The
        state commit is not, so the state is made durable and Plex is left to its own
        scheduled scan, or to the next run over the same section.
        """
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        run = await _plan(session, snapshot_id)

        class _CancelBeforeSecondItem(FakePlex):
            """The streaming veto is re-polled before every delete. This raises
            CancelledError on the second poll, standing in for a shutdown landing between
            items."""

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

        # On disk, not merely on the session's copy of the row. A shutdown is exactly when
        # an in-memory terminal state buys nothing.
        assert (await _stored_run(async_factory, run.id)).state is RunState.ABORTED  # not EXECUTING
        # No Plex call at all inside the cancellation. The rescans are queued during the
        # run and sent at the end, so a shutdown that lands mid-run sends none of them
        # rather than firing one request per deleted folder into a dying process.
        assert plex.refreshed == []
        # The settle-wait and the purge do not run inside the cancellation either.
        assert plex.emptied == []

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
    writes ``stopped=0``, not a real epoch stamp, when it has no stop time, and 0
    compared against any approval time could never spare a file."""

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
        """The after-action report carries a plain-English checklist per item. It lists
        the interlocks that passed, the delete, and the exclusion verification, each
        marked ✓ or ✗ the way the UI renders the why-panel."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        report = await _real(session, run, _gateway(radarr={1: FakeRadarr()}))

        outcome = report.outcomes[0]
        assert outcome.title == "Worthless 0"
        labels = {_label_text(c.label): c.ok for c in outcome.checks}
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
        failed = [_label_text(c.label) for c in outcome.checks if not c.ok]
        assert failed and any("exclusion" in label.lower() for label in failed)
        # The "removed the file" check still passed. The file really is gone.
        removed = next(
            c for c in outcome.checks if "removed the file" in _label_text(c.label).lower()
        )
        assert removed.ok is True

    async def test_an_exclusion_that_lands_a_beat_late_is_still_verified(
        self, session: AsyncSession
    ) -> None:
        """Radarr adds the import exclusion just after the delete's 200 response. An
        immediate single read would miss it, aborting on the canary a run whose delete
        actually succeeded. The verification polls instead, so a slightly-late exclusion
        still verifies."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        # The exclusion is not visible on the first two reads. It appears on the third.
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
        assert report.aborted_reason.id == "error.reap.canary_failed"
        assert "test item" in _label_text(report.aborted_reason).lower()

    async def test_a_movie_without_a_tmdb_id_is_refused_before_the_delete(
        self, session: AsyncSession
    ) -> None:
        """With no tmdbId the exclusion re-read can never verify, so the item must be
        refused before anything is sent. Discovering it afterwards would strand an
        irreversible delete behind a check that was doomed from the start. As the canary
        here, it would abort the run having already removed the file."""

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

    async def test_a_radarr_with_the_exclusion_off_deletes_without_it(
        self, session: AsyncSession
    ) -> None:
        """When this Radarr's re-download switch is off, the plan body carries
        ``addImportExclusion: false``, the delete is sent without it, and the after-check
        says so plainly instead of trying (and failing) to verify an exclusion that was
        never asked for. The delete itself still has to be proven."""
        await _seed_radarr(session, exclusion=False)
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)

        # The plan the operator approves shows the exclusion off.
        terminal = (await _steps(session, run.id))[-1]
        assert json.loads(terminal.body_json or "{}")["addImportExclusion"] is False

        radarr = FakeRadarr()
        report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 1
        assert radarr.delete_calls == [1]
        assert radarr.exclusion_args == [False]  # the delete was sent without the exclusion
        labels = [_label_text(c.label).lower() for c in report.outcomes[0].checks]
        # The file-removed check still ran. The exclusion line reads "off", never "confirmed".
        assert any("removed the file" in label for label in labels)
        assert any("off" in label for label in labels if "exclusion" in label)
        assert not any("confirmed" in label for label in labels)

    async def test_the_exclusion_off_lets_a_movie_with_no_tmdb_id_delete(
        self, session: AsyncSession
    ) -> None:
        """The no-TMDB-id fail-closed exists only so an armed exclusion can be verified.
        With the exclusion off there is nothing to verify, so a movie Radarr lists without
        a TMDB id is deleted rather than refused."""
        await _seed_radarr(session, exclusion=False)
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr(tmdb_id=None)  # no tmdbId on the movie payload

        report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 1
        assert radarr.delete_calls == [1]

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
        """Two movies. The smaller is the canary and succeeds. The larger fails its
        exclusion. The run completes with one deleted and one failed. One stubborn item
        is not a reason to abandon the rest.
        """
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
        sent to the wrong server. It fails the item rather than guessing."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        gateway = _gateway(radarr={2: FakeRadarr()})

        report = await _real(session, run, gateway)

        assert report.state is RunState.ABORTED  # sole item is the canary
        assert report.deleted_items == 0

    @pytest.mark.httpx2(assert_all_called=False)
    async def test_the_delete_is_refused_by_the_guard_when_the_client_is_real(
        self, session: AsyncSession, httpx2_mock: respx.Router
    ) -> None:
        """Two independent layers, both must agree. Even inside a 'real' run, a genuine
        client refuses the mutation unless the host is armed. Here the executor thinks it
        is armed, but the client's own transport guard is read-only, so the call is
        blocked, not sent."""
        from reaper.clients.arr import RadarrClient

        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        # A real Radarr client whose transport is read-only. movie_by_id (a GET) succeeds
        # via the mock, but delete_movie must be refused by the guard.
        httpx2_mock.get("https://radarr.test/api/v3/movie/1").mock(
            return_value=httpx.Response(200, json={"id": 1, "tmdbId": 5, "sizeOnDisk": 1024**3})
        )
        client = RadarrClient("https://radarr.test", "k", safety=_read_only())
        gateway = _gateway(radarr={1: client})
        async with client:
            report = await _real(session, run, gateway)

        # The guard blocked the delete. It is caught and turned into a failed canary,
        # which aborts the run cleanly. Nothing was deleted, and the process did not crash.
        assert report.deleted_items == 0
        assert report.state is RunState.ABORTED


class TestTheCanaryIsTheFirstRealDelete:
    """The canary is the first item actually deleted, not merely index 0. If the smallest is
    spared or vetoed (skipped, nothing touched), the next item inherits the halt-on-failure
    protection. A 'failed' delete can still have removed the file."""

    async def test_a_skipped_smallest_promotes_the_next_delete_to_canary(
        self, session: AsyncSession
    ) -> None:
        from reaper.services import whitelist

        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        run = await _plan(session, snapshot_id)
        await whitelist.set_override(
            session, media_key="radarr:1:1", title="Spared", decision="spare", note=None
        )
        # The promoted canary (movie 2) fails its exclusion.
        radarr = FakeRadarr(fail_ids={2})

        report = await _real(session, run, _gateway(radarr={1: radarr}))

        # Movie 1 was skipped (spared). Movie 2 became the canary, failed, and aborted
        # the run. The failure was not allowed to pass as a mere per-item failure.
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
        await whitelist.set_override(
            session, media_key="radarr:1:1", title="Spared", decision="spare", note=None
        )
        radarr = FakeRadarr(fail_ids={3})  # the last, largest item fails

        report = await _real(session, run, _gateway(radarr={1: radarr}))

        # Movie 1 skipped. Movie 2 is the canary and succeeds. Movie 3 fails, but the run
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
        season. So the veto set includes the episode's parent (season) key, and the
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
        canonical key. Someone is watching through the other listing. Deleting would cut
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
        """The precise per-row timestamp compare is what matters here. A play from before
        the approval instant is ignored even though the coarse date filter would include
        it."""
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
        treated as a possible late play and spares the item. Ambiguity keeps the file."""
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

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"data": None}, id="null-data"),
            pytest.param({"data": {"rows": []}}, id="data-is-an-object"),
            pytest.param({"recordsFiltered": 0}, id="no-data-key-at-all"),
            pytest.param([], id="envelope-is-not-a-mapping"),
        ],
    )
    async def test_an_unreadable_history_body_fails_closed(
        self, session: AsyncSession, body: Any
    ) -> None:
        """A success response whose history body cannot be read must spare, not delete.

        The client raises only when the envelope reports failure, so each of these arrives
        having raised nothing. Coercing an unreadable body to an empty row list would make
        it indistinguishable from a genuine "nobody played it" and let the delete proceed,
        with the after-action checklist reporting the played-since-approval check as
        passed for a check that saw no data. Treating an unreadable body as a failure
        instead makes every case here spare.
        """
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()
        gateway = _gateway(radarr={1: radarr}, tautulli=FakeTautulli(body=body))

        report = await _real(session, run, gateway)

        assert report.skipped == 1
        assert radarr.delete_calls == []

    async def test_a_genuinely_empty_history_still_deletes(self, session: AsyncSession) -> None:
        """The other side of the previous test, so this behavior cannot be "spare on
        everything".

        A real list with no rows is Tautulli saying it looked and nobody played it. That
        is an answer, not a failure, and it must still let the delete proceed. Otherwise
        the interlock would hold every item on a library nobody has watched.
        """
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()
        gateway = _gateway(radarr={1: radarr}, tautulli=FakeTautulli(body={"data": []}))

        report = await _real(session, run, gateway)

        assert report.deleted_items == 1
        assert radarr.delete_calls == [1]

    async def test_a_play_through_the_files_other_listing_spares(
        self, session: AsyncSession
    ) -> None:
        """A merged bind. The post-approval play was recorded under the file's other
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
        sonarr = FakeSonarr()  # season 3 has files 101 and 102, season 4 has 900
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
        """This asymmetry matters. 'Files gone, still monitored' re-downloads everything,
        so when the unmonitor does not verify, the file delete never runs."""
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
        can address the item. That uncertainty means it is spared, never deleted blind."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=None)
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()
        gateway = _gateway(radarr={1: radarr})

        report = await _real(session, run, gateway)

        assert report.skipped == 1
        assert radarr.delete_calls == []


class TestASpareIsHonoredAtExecuteTime:
    """A spare added after the plan is built, during the grace window this executor
    exists to honor, must still stop the delete. A spare does not change the frozen
    candidate row, so neither the verdict nor the manifest hash can see it. The executor
    re-checks the override per item instead. These tests pin that behavior.
    """

    async def test_a_spare_added_after_the_plan_is_not_deleted(self, session: AsyncSession) -> None:
        from reaper.services import whitelist

        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        # The owner changes their mind during grace and spares it.
        await whitelist.set_override(
            session, media_key="radarr:1:1", title="Worthless", decision="spare", note=None
        )

        radarr = FakeRadarr()
        report = await _real(session, run, _gateway(radarr={1: radarr}))

        # Skipped, not deleted. The run still COMPLETES, because a spare is not a library
        # change, so the manifest guard does not void the whole run.
        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 0
        assert report.skipped == 1
        assert radarr.delete_calls == []
        assert [s.state for s in await _steps(session, run.id)] == [StepState.SKIPPED]

    async def test_a_spare_at_plan_time_does_not_abort_the_run(self, session: AsyncSession) -> None:
        """The other half of the same case. A spare present before planning must not make
        the manifest hashes disagree and abort every execution. The spared item gets no
        steps. The other is deleted normally.
        """
        from reaper.services import whitelist

        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        await whitelist.set_override(
            session, media_key="radarr:1:2", title="Keep me", decision="spare", note=None
        )
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
        # Spare the SHOW. It must cover the condemned season under it.
        await whitelist.set_override(
            session, media_key="sonarr:1:42", title="A Show", decision="spare", note=None
        )

        sonarr = FakeSonarr()
        report = await _real(session, run, _gateway(sonarr={1: sonarr}))

        assert report.skipped == 1
        assert sonarr.unmonitor_calls == []  # the season was never touched

    async def test_a_dry_run_shows_a_spared_item_as_skipped(self, session: AsyncSession) -> None:
        from reaper.services import whitelist

        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        await whitelist.set_override(
            session, media_key="radarr:1:1", title="Worthless", decision="spare", note=None
        )

        report = await Executor(
            session, safety=_read_only(), settings=ProfileSettings(), dry_run=True
        ).execute(run.id)

        assert report.state is RunState.COMPLETED
        assert report.skipped == 1
        assert report.outcomes[0].detail.id == "error.reap.step.spared_by_hand"


class TestPlexCleanup:
    async def test_a_mapped_path_is_refreshed_then_trash_purged(
        self, session: AsyncSession
    ) -> None:
        """After the file is gone, Plex is refreshed for the affected path. With the mount
        up and the section shrunk by exactly the one delete, the section's trash is also
        purged, so no stale 'unavailable' entry lingers."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Movies": ["/movies"]}, item_counts={"Movies": [100, 99]})
        gateway = _gateway(radarr={1: FakeRadarr(path="/movies/Worthless (2001)")}, plex=plex)

        report = await _real(session, run, gateway)

        assert report.deleted_items == 1
        assert plex.refreshed == [("Movies", "/movies/Worthless (2001)")]
        assert plex.emptied == ["Movies"]  # the stale entry is purged

    async def test_the_rescans_are_sent_once_at_the_end_not_per_item(
        self, session: AsyncSession
    ) -> None:
        """A rescan fires an asynchronous Plex scan that can take minutes. One per item
        would leave Plex scanning the folders this run is still deleting from, for the
        whole length of the run. They are queued as files go, deduplicated by path, and
        sent together once the deleting is over."""
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        run = await _plan(session, snapshot_id)

        class _CountDeletesFirst(FakePlex):
            """Records how many deletes Radarr had taken by the time each rescan arrived."""

            def __init__(self, radarr: FakeRadarr, **kw: Any) -> None:
                super().__init__(**kw)
                self._radarr = radarr
                self.deletes_before_each_refresh: list[int] = []

            async def refresh_path(self, section_key: int, path: str) -> None:
                self.deletes_before_each_refresh.append(len(self._radarr.delete_calls))
                await super().refresh_path(section_key, path)

        radarr = FakeRadarr(path="/movies/Worthless")
        plex = _CountDeletesFirst(
            radarr, sections={"Movies": ["/movies"]}, item_counts={"Movies": [100, 98]}
        )

        report = await _real(session, run, _gateway(radarr={1: radarr}, plex=plex))

        assert report.deleted_items == 2
        # Both movies report the same folder, so two deletes ask Plex for one scan.
        assert plex.refreshed == [("Movies", "/movies/Worthless")]
        # And it arrived after both deletes, never between them.
        assert plex.deletes_before_each_refresh == [2]
        # Both rating keys still reach the purge gate, so it allows a shrink of two.
        assert plex.emptied == ["Movies"]

    async def test_refresh_and_purge_fire_even_when_the_exclusion_fails(
        self, session: AsyncSession
    ) -> None:
        """A run can fail at the exclusion check without Plex ever being told, leaving a
        stale entry. The refresh must fire whenever the file is gone, regardless."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Movies": ["/movies"]}, item_counts={"Movies": [100, 99]})
        # Exclusion never lands -> the item FAILS, but the file is gone.
        gateway = _gateway(
            radarr={1: FakeRadarr(path="/movies/Worthless", land_exclusion=False)}, plex=plex
        )

        report = await _real(session, run, gateway)

        assert report.state is RunState.ABORTED  # the canary failed its exclusion check
        assert plex.refreshed == [("Movies", "/movies/Worthless")]  # Plex was refreshed anyway
        assert plex.emptied == ["Movies"]  # the stale entry was purged too

    async def test_the_trash_is_not_purged_when_a_mount_is_down(
        self, session: AsyncSession
    ) -> None:
        """This is the mass-loss guard. If an *arr root folder is not accessible, the
        volume may be gone, and the trash may be full of merely-unreachable items rather
        than deleted ones. So the purge is refused."""
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
        """This is the mass-loss guard the count-delta gate exists for. A mount flap on
        the Plex host, invisible to the *arr root-folder check because it is a different
        mount, could trash hundreds of entries while this run deletes only one movie. When
        the section shrinks by far more than this run deleted, purging would destroy
        those items' library records, so the purge is refused.
        """
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
        confirmation is refused. The stale entry is cosmetic, and Plex's own maintenance
        will clear it."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Movies": ["/movies"]})  # count never changes
        gateway = _gateway(radarr={1: FakeRadarr(path="/movies/Worthless")}, plex=plex)

        report = await _real(session, run, gateway)

        assert report.deleted_items == 1
        assert plex.refreshed == [("Movies", "/movies/Worthless")]
        assert plex.emptied == []

    async def test_two_copies_of_one_title_grant_the_purge_one_allowance_between_them(
        self, session: AsyncSession
    ) -> None:
        """Two *arr instances holding the same movie bind to ONE merged Plex listing, so
        the section's own count can only ever fall by one when both copies go. Counting
        the allowance per candidate would charge that single listing twice and let a
        shrink of two pass. A second entry lost from the section could only have come
        from something other than this run, which is the mass loss this gate exists to
        refuse. The allowance is the DISTINCT listings removed instead, so two candidates
        on rating key 700 grant one.
        """
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 700), ("radarr:2:1", 1 * GB, 700)]
        )
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Movies": ["/movies"]}, item_counts={"Movies": [100, 98]})
        gateway = _gateway(
            radarr={
                1: FakeRadarr(path="/movies/Worthless"),
                2: FakeRadarr(path="/movies/Worthless 4K"),
            },
            plex=plex,
        )

        report = await _real(session, run, gateway)

        assert report.deleted_items == 2  # both copies really went
        assert plex.emptied == []  # but the section lost one entry too many

    async def test_two_titles_of_their_own_still_earn_an_allowance_each(
        self, session: AsyncSession
    ) -> None:
        """The other side of the boundary above. Deduping the allowance is not the same
        as capping it at one. Two candidates on their own Plex listings remove two
        entries, the section falls by exactly two, and the purge goes ahead."""
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 700), ("radarr:2:1", 1 * GB, 701)]
        )
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Movies": ["/movies"]}, item_counts={"Movies": [100, 98]})
        gateway = _gateway(
            radarr={
                1: FakeRadarr(path="/movies/Worthless"),
                2: FakeRadarr(path="/movies/Worthless Two"),
            },
            plex=plex,
        )

        report = await _real(session, run, gateway)

        assert report.deleted_items == 2
        assert plex.emptied == ["Movies"]

    async def test_a_sibling_section_sharing_a_path_prefix_is_never_claimed(
        self, session: AsyncSession
    ) -> None:
        """A section rooted at /media/movies must not claim files under /media/movies-4k.
        Matching on a raw string prefix would refresh, and trash-purge, the wrong section.
        The path must sit inside the location at a path-component boundary."""
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

    async def test_two_libraries_sharing_a_title_are_told_apart(
        self, session: AsyncSession
    ) -> None:
        """Two Plex libraries may legally share a title, and a title lookup answers with
        whichever one the server listed first. Addressed by title, this run would refresh
        one library, read ITS size as the count-delta baseline, and purge ITS trash. That
        is three operations aimed at a library nothing was deleted from, on the most
        dangerous call the app makes. Only the key tells them apart."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
        run = await _plan(session, snapshot_id)
        plex = FakePlex(
            section_rows=[
                PlexSectionPaths(key=1, title="Movies", locations=("/media/hd",)),
                PlexSectionPaths(key=2, title="Movies", locations=("/media/4k",)),
            ],
            # The count-delta baseline. Only the SECOND library shrinks by the one delete.
            # Read off the first, the purge would be refused (or worse, allowed against a
            # library whose size moved for some entirely unrelated reason).
            item_counts_by_key={1: [100, 100], 2: [50, 49]},
        )
        gateway = _gateway(radarr={1: FakeRadarr(path="/media/4k/Worthless (2001)")}, plex=plex)

        report = await _real(session, run, gateway)

        assert report.deleted_items == 1
        assert plex.refreshed_keys == [2]
        assert plex.emptied_keys == [2]


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
    """Stages a plan the planner would never have produced, one holding an unmeasured item.

    The planner holds these back, so this is the only way to reach the executor's own
    per-item refusal, which exists precisely because the host-side layer must not depend
    on the plan being right.

    Re-stamping the manifest hash is part of the staging, not a workaround. Taking a size
    away legitimately voids the approval, since the hash binds the frozen set and a
    resize makes it a different set. Without this, the run would abort on that check and
    never reach the one under test. Re-approving says "assume the plan was built this way
    from the start".
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
        _clean_explanation(match={"merged_rating_keys": list(merged_keys)})
        if merged_keys
        else _clean_explanation()
    )
    return await _snapshot_many(
        session, [(media_key, size, rating_key)], media_type=media_type, explanation=explanation
    )


def _clean_explanation(**blocks: object) -> str:
    """A stored explanation carrying every block a real one carries.

    ``"{}"`` is not a row any scan writes. The why panel cannot render it, so a hand reap
    on it is refused, and an item planned only by that reap never reaches the plan. Every
    candidate below is scan-condemned, where the document is not read at all. But the
    tests that flip a verdict to ``abstain`` and lean on a hand reap depend on it, and a
    fixture that quietly stopped planning its second item would report a one-item run as
    a pass.
    """
    return json.dumps(
        {
            "score": 90,
            "coverage": 1.0,
            "signals": [],
            "protections_fired": [],
            "protections_checked": [],
            "protections_unknown": [],
            **blocks,
        }
    )


async def _snapshot_many(
    session: AsyncSession,
    items: list[tuple[str, int | None, int | None]],
    *,
    media_type: str = "movie",
    explanation: str | None = None,
    group_key: str | None = None,
) -> int:
    explanation = _clean_explanation() if explanation is None else explanation
    now = utcnow()
    await _seed_instances(session, [media_key for media_key, _, _ in items])
    snapshot = Snapshot(
        created_at=now,
        policy_hash=await live_policy_hash(session),
        # Stamped the way a scan stamps it, so the executor's list interlock sees the
        # registry these tests actually run against rather than an unknown that refuses.
        list_config_hash=await list_config.current_fingerprint(session),
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


async def _seed_radarr(session: AsyncSession, *, exclusion: bool, instance_id: int = 1) -> None:
    """Seed the Radarr instance row a plan reads its import-exclusion setting from.

    The reap-loop tests otherwise leave the instance table empty, in which case build_plan
    falls back to adding the exclusion. A test that cares about the OFF path must create the
    row so the setting is read rather than defaulted."""
    session.add(
        Instance(
            id=instance_id,
            kind=InstanceKind.RADARR,
            name=f"r{instance_id}",
            base_url="https://radarr.test",
            api_key_enc="enc",
            add_import_exclusion=exclusion,
            created_at=utcnow(),
        )
    )
    await session.flush()


async def _plan(session: AsyncSession, snapshot_id: int) -> ReapRun:
    return await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")


async def _save_policy(session: AsyncSession, body: PolicyBody, name: str = "saved") -> None:
    """Append a movie policy row, the way the editor does.

    ``active_policy`` reads the NEWEST row per media type, so appending is what changes the
    policy in force, and therefore the hash the executor compares a plan against."""
    session.add(
        Policy(
            policy_hash=body.policy_hash(),
            body_json=body.model_dump_json(),
            media_type="movie",
            name=name,
            created_at=utcnow(),
        )
    )
    await session.flush()


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
        tmdb_id: int | None = 555,
        land_exclusion: bool = True,
        become_gone: bool = True,
        path: str = "/movies/Worthless",
        fail_ids: set[int] | None = None,
        exclusion_appears_after: int = 0,
        root_accessible: bool = True,
        size_on_disk: int | None = 256 * 1024**2,
    ) -> None:
        # None models a movie Radarr lists with no TMDB id, used to prove that the
        # no-id fail-closed applies only when the exclusion is armed.
        self._tmdb_id = tmdb_id
        self._land_exclusion = land_exclusion
        self._become_gone = become_gone
        self._path = path
        self._root_accessible = root_accessible
        # What sizeOnDisk reports. Small by default so the drift interlock stays quiet in
        # tests about other things. None omits the field (an unreadable size).
        self._size_on_disk = size_on_disk
        self._fail_ids = fail_ids or set()  # ids whose exclusion never lands
        # Radarr adds the exclusion a beat after the delete's 200. This simulates that
        # lag. An exclusion added on read N only becomes visible from read N + this many.
        self._exclusion_appears_after = exclusion_appears_after
        self._exclusion_reads = 0
        self._exclusions: list[dict[str, Any]] = []  # each {"tmdbId", "_visible_at"}
        self._deleted: set[int] = set()
        self.delete_calls: list[int] = []
        self.exclusion_args: list[bool] = []  # the add_exclusion value each delete was sent

    async def movie_by_id(self, movie_id: int) -> dict[str, Any]:
        if movie_id in self._deleted and self._become_gone:
            raise IntegrationError("radarr", "movie not found", status=404)
        movie: dict[str, Any] = {"id": movie_id, "path": self._path}
        if self._tmdb_id is not None:
            movie["tmdbId"] = self._tmdb_id + movie_id
        if self._size_on_disk is not None:
            movie["sizeOnDisk"] = self._size_on_disk
        return movie

    async def delete_movie(
        self, movie_id: int, *, delete_files: bool = True, add_exclusion: bool = True
    ) -> None:
        self.delete_calls.append(movie_id)
        self.exclusion_args.append(add_exclusion)
        self._deleted.add(movie_id)
        lands = self._land_exclusion and movie_id not in self._fail_ids
        if add_exclusion and lands and self._tmdb_id is not None:
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


class UnreachableAfterDelete(FakeRadarr):
    """A movie Radarr accepted the delete for, then went unreachable for the confirming
    re-read. Nobody knows whether the movie is there. Shared by three tests in
    ``TestARemovalIsCountedEvenWhenTheStepFails`` that each drive it a different way."""

    async def movie_by_id(self, movie_id: int) -> dict[str, Any]:
        if movie_id in self._deleted:
            # A timeout carries no status at all, which is exactly the case that
            # must not collapse into "the movie is still present".
            raise IntegrationError("radarr", "timed out", status=None)
        return await super().movie_by_id(movie_id)


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
            # A different season, and it must be untouched.
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
    """A stand-in Plex, with controllable streams, section paths, and a record of
    refreshes.

    ``item_counts`` scripts what ``item_count`` returns per section, in read order (the
    last value repeats), so a test can make a section "shrink" by exactly what was
    deleted, or by more, to prove the purge refuses. The default never shrinks, which the
    count-delta gate treats as unconfirmed, so no purge happens.

    Sections are declared by title for readability, and the fake assigns each one a KEY,
    which is all the executor ever sees, because a title cannot address a library on its
    own (two may share one). ``sections`` maps title to paths. ``section_rows`` takes
    explicit rows instead, which is the only way to model two libraries of the same name.
    The recorded ``refreshed`` and ``emptied`` are titles for legibility, with
    ``refreshed_keys`` and ``emptied_keys`` beside them for when the title is deliberately
    ambiguous.
    """

    def __init__(
        self,
        *,
        streams: list[ActiveStream] | None = None,
        raise_on_streams: bool = False,
        sections: dict[str, list[str]] | None = None,
        section_rows: list[PlexSectionPaths] | None = None,
        item_counts: dict[str, list[int]] | None = None,
        item_counts_by_key: dict[int, list[int]] | None = None,
    ) -> None:
        self._streams = streams or []
        self._raise = raise_on_streams
        self._rows = section_rows or [
            PlexSectionPaths(key=100 + i, title=title, locations=tuple(paths))
            for i, (title, paths) in enumerate((sections or {}).items())
        ]
        self._titles = {row.key: row.title for row in self._rows}
        # Scripted counts are declared per title (every test that uses them has one
        # library of that name) and resolved to the key the executor asks with.
        self._item_counts = {
            row.key: list(item_counts[row.title])
            for row in self._rows
            if item_counts and row.title in item_counts
        }
        # And by key directly, for the same-title case where a title cannot say which.
        self._item_counts.update({k: list(v) for k, v in (item_counts_by_key or {}).items()})
        self.refreshed: list[tuple[str, str]] = []
        self.refreshed_keys: list[int] = []
        self.emptied: list[str] = []
        self.emptied_keys: list[int] = []

    async def active_streams(self) -> list[ActiveStream]:
        if self._raise:
            raise PlexError("cannot read sessions")
        return list(self._streams)

    async def section_paths(self) -> list[PlexSectionPaths]:
        return list(self._rows)

    async def refresh_path(self, section_key: int, path: str) -> None:
        self.refreshed.append((self._titles[section_key], path))
        self.refreshed_keys.append(section_key)

    async def is_refreshing(self, section_key: int) -> bool:
        return False

    async def item_count(self, section_key: int) -> int:
        scripted = self._item_counts.get(section_key)
        if not scripted:
            return 100
        return scripted.pop(0) if len(scripted) > 1 else scripted[0]

    async def empty_trash(self, section_key: int) -> None:
        self.emptied.append(self._titles[section_key])
        self.emptied_keys.append(section_key)


class FakeTautulli:
    """A stand-in Tautulli whose history rows and error behavior a test controls.

    ``rows`` answers every key alike. ``rows_by_key`` answers per rating key (empty for
    keys not listed), for the merged-listings tests where which key was played matters.
    ``body`` replaces the whole response, for the shapes that are not a list of rows at
    all, such as the success envelope carrying a null or unrecognized ``data``.
    """

    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        rows_by_key: dict[int, list[dict[str, Any]]] | None = None,
        raise_error: bool = False,
        body: Any = None,
    ) -> None:
        self._rows = rows or []
        self._rows_by_key = rows_by_key
        self._body = body
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
        if self._body is not None:
            body: dict[str, Any] = self._body
            return body
        if self._rows_by_key is not None:
            key = rating_key if rating_key is not None else parent_rating_key
            return {"data": list(self._rows_by_key.get(key or 0, []))}
        return {"data": list(self._rows)}


# ---------------------------------------------------------------------------
# Journal durability and the atomic EXECUTING claim
# ---------------------------------------------------------------------------


class _ProcessDied(BaseException):
    """Stands in for the process simply stopping mid-send.

    This is deliberately a ``BaseException``. The executor funnels every ordinary
    ``Exception`` through ``_fail``, so one item's surprise cannot wedge the run, which
    means an ``Exception`` here would be handled and would prove nothing about the
    journal surviving a death. This is the shape of the thing that genuinely cannot be
    handled, like the ``CancelledError`` a shutdown sends, so it escapes the same way a
    killed process does.
    """


class _DyingRadarr(FakeRadarr):
    """Succeeds on the first delete, then simulates the process dying on the second."""

    async def delete_movie(
        self, movie_id: int, *, delete_files: bool = True, add_exclusion: bool = True
    ) -> None:
        if len(self.delete_calls) >= 1:
            raise _ProcessDied("process died mid-send")
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
    run-long transaction. Kill the process after item 1 of 2 has deleted, and the run
    must still read EXECUTING with item 1 VERIFIED and item 2 SENT from a fresh session.
    It must never roll back to PLANNED as if no file were gone."""

    async def test_a_crash_mid_run_leaves_a_durable_journal(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="test-key")
        engine = create_engine(settings)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)

        try:
            async with async_factory() as session:
                snapshot_id = await _snapshot_many(
                    session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
                )
                run = await _plan(session, snapshot_id)
                run_id = run.id
                await session.commit()

            async with async_factory() as run_session:
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
                with pytest.raises(_ProcessDied, match="process died"):
                    await executor.execute(run_id)
                # Deliberately no commit. The process "died" here.

            # A fresh session, standing in for a restart, must see the durable truth.
            async with async_factory() as fresh:
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
    committed before the first send. A second execute arriving while the first is
    mid-run is refused, instead of re-running the plan over the first one's journal."""

    async def test_a_second_execute_while_one_is_in_flight_is_refused(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="test-key")
        engine = create_engine(settings)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)

        try:
            async with async_factory() as session:
                snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=700)
                run = await _plan(session, snapshot_id)
                run_id = run.id
                await session.commit()

            plex = _BlockingPlex()
            radarr = FakeRadarr()

            async with async_factory() as first_session, async_factory() as second_session:
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
    """The 30-day budget is enforced at execute time, over verified deletions. No
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
        return ProfileSettings(**base)

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
        assert report.aborted_reason is not None
        assert report.aborted_reason.id == "error.reap.bytes_over_rolling_cap"
        assert radarr.delete_calls == []  # nothing was deleted. Abort, never truncate

    async def test_a_dry_run_reports_the_same_rolling_refusal(
        self, session: AsyncSession, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
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
        # On disk as PLANNED before the dry run, so the read below checks the real state.
        await session.commit()
        report = await self._execute(session, second.id, settings, dry=True)

        assert report.state is RunState.ABORTED
        assert report.aborted_reason is not None
        assert report.aborted_reason.id == "error.reap.bytes_over_rolling_cap"
        # The dry run did not consume the plan. On disk, not in the session's copy.
        assert (await _stored_run(async_factory, second.id)).state is RunState.PLANNED

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
        assert report.aborted_reason is not None
        assert report.aborted_reason.id == "error.reap.items_over_rolling_cap"
        assert report.aborted_reason.params.get("cap") == 3

    async def test_deletions_older_than_thirty_days_fall_out_of_the_window(
        self, session: AsyncSession
    ) -> None:
        settings = self._settings()
        first_snapshot = await _snapshot_one(
            session, media_key="radarr:1:1", rating_key=701, size=300 * 10**9
        )
        first = await _plan(session, first_snapshot)
        assert (await self._execute(session, first.id, settings)).deleted_items == 1

        # Age the verified deletion out of the window. Age both stamps. A deletion carries
        # a verified_at and a file_removed_at, and the window reads either, so aging only
        # one of them leaves the deletion inside the window through the other.
        for step in await _steps(session, first.id):
            step.verified_at = utcnow() - timedelta(days=40)
            step.file_removed_at = utcnow() - timedelta(days=40)
        await session.commit()

        second_snapshot = await _snapshot_one(
            session, media_key="radarr:1:2", rating_key=702, size=300 * 10**9
        )
        second = await _plan(session, second_snapshot)
        report = await self._execute(session, second.id, settings)

        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 1


# ---------------------------------------------------------------------------
# The approval's second half: the policy that judged the plan
# ---------------------------------------------------------------------------


class TestAPolicyEditVoidsAPendingPlan:
    """The manifest hash cannot see a policy edit. It hashes frozen candidate rows, and
    editing a policy touches none of them. So a plan approved under a looser policy could
    sail through every gate and delete the very items a freshly-added protection was meant
    to keep. The run carries the policy hash its snapshot was scored under. The executor
    compares it against the policy in force now.
    """

    async def _tighten(self, session: AsyncSession) -> None:
        """Save a movie policy that differs from the shipped default, so the live hash moves."""
        await _save_policy(session, DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 95}))

    async def test_a_plan_under_the_policy_in_force_still_runs(self, session: AsyncSession) -> None:
        """The control. Nothing edited, so nothing is refused."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)

        report = await _real(session, run, _gateway(radarr={1: FakeRadarr()}))

        assert report.state is RunState.COMPLETED

    async def test_tightening_the_policy_after_approval_refuses_the_run(
        self, session: AsyncSession, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)
        await self._tighten(session)
        # On disk as PLANNED before the refusal, so the read below checks the real state.
        await session.commit()

        radarr = FakeRadarr()
        # This is a refusal, not an abort. Like the manifest re-check, this runs before
        # the run is claimed, so the route can answer it immediately instead of through
        # the poll.
        with pytest.raises(ExecutionError, match="policy changed"):
            await _real(session, run, _gateway(radarr={1: radarr}))

        assert radarr.delete_calls == []  # nothing was sent
        # On disk. A refused run that claimed EXECUTING and could not roll it back is
        # exactly the state that would make "still runnable after a re-scan" false.
        assert (await _stored_run(async_factory, run.id)).state is RunState.PLANNED

    async def test_the_dry_run_proves_the_same_refusal(self, session: AsyncSession) -> None:
        """A simulation that still said "would delete" would send the operator to the real
        run to discover the refusal, after they had typed the phrase."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)
        await self._tighten(session)

        executor = Executor(session, safety=_read_only(), settings=ProfileSettings(), dry_run=True)
        with pytest.raises(ExecutionError, match="policy changed"):
            await executor.execute(run.id)

    async def test_a_run_is_still_refused_after_the_policy_is_put_back(
        self, session: AsyncSession
    ) -> None:
        """Putting the old numbers back into a NEW policy row restores the hash, because the
        hash is over the body, not the row. This pins that the check is content-addressed.
        An operator who edits and undoes has not been locked out of their own plan."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)
        await self._tighten(session)
        await _save_policy(session, DEFAULT_MOVIE_POLICY)

        report = await _real(session, run, _gateway(radarr={1: FakeRadarr()}))

        assert report.state is RunState.COMPLETED


class TestAListEditVoidsAPendingPlan:
    """The same gap one layer out, opened by keeping tags off the policy body.

    Keep tags live on Settings, Lists, not in the policy body, so a config-only edit
    (retag it, repoint it at another Plex collection) rewrites no policy rule at all.
    Every body stays byte for byte the same, so both policy-hash checks above still
    match. The per-item interlocks never re-read membership either, so an operator who
    narrowed a keep list after approving a plan could come back and delete the titles it
    had been keeping.

    ``Snapshot.list_config_hash`` is what closes this gap, since nothing else records
    what list membership a plan was built under.
    """

    @staticmethod
    async def _retag(session: AsyncSession) -> None:
        """Change what one list matches, and nothing else. No policy row is written."""
        rows = await list_config.all_lists(session)
        tag_list = next((r for r in rows if r.source == "arr_tag"), None)
        assert tag_list is not None, "the seeded registry has no tag list to retag"
        await list_config.update(
            session, tag_list.id, config={"tags": ["a-different-tag"], "match": "any"}
        )

    async def test_a_plan_under_the_lists_in_force_still_runs(self, session: AsyncSession) -> None:
        """The control. Nothing edited, so nothing is refused."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)

        report = await _real(session, run, _gateway(radarr={1: FakeRadarr()}))

        assert report.state is RunState.COMPLETED

    async def test_retagging_a_list_after_approval_refuses_the_run(
        self, session: AsyncSession, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The edit that moves neither hash above, so this is the single variable."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)
        policy_before = await live_policy_hash(session)
        await self._retag(session)
        assert await live_policy_hash(session) == policy_before, (
            "the retag moved the policy hash, so this test would pass with the list "
            "interlock deleted (rule 118)"
        )
        await session.commit()

        radarr = FakeRadarr()
        with pytest.raises(ExecutionError, match="protection lists changed"):
            await _real(session, run, _gateway(radarr={1: radarr}))

        assert radarr.delete_calls == []
        assert (await _stored_run(async_factory, run.id)).state is RunState.PLANNED

    async def test_the_dry_run_proves_the_same_refusal(self, session: AsyncSession) -> None:
        """Or the operator meets the refusal only after typing the confirmation phrase."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)
        await self._retag(session)

        executor = Executor(session, safety=_read_only(), settings=ProfileSettings(), dry_run=True)
        with pytest.raises(ExecutionError, match="protection lists changed"):
            await executor.execute(run.id)

    async def test_a_snapshot_that_never_recorded_its_lists_refuses(
        self, session: AsyncSession
    ) -> None:
        """The pre-upgrade shape, and a scan that degraded for a registry it could not
        read, both stamp ``None``. That means unknown, never "no lists". A plan built
        under lists nobody can name is exactly what must not execute."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)
        snapshot = await session.get(Snapshot, snapshot_id)
        assert snapshot is not None
        snapshot.list_config_hash = None
        await session.flush()

        with pytest.raises(ExecutionError, match="protection lists changed"):
            await _real(session, run, _gateway(radarr={1: FakeRadarr()}))

    async def test_a_registry_unreadable_at_execute_refuses(self, session: AsyncSession) -> None:
        """The live side unknown. Both sides are driven to ``None`` at once, because that is
        the pairing an equality test reads as agreement and lets through."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)
        snapshot = await session.get(Snapshot, snapshot_id)
        assert snapshot is not None
        snapshot.list_config_hash = None
        rows = await list_config.all_lists(session)
        rows[0].config_json = "{not json"
        await session.flush()

        with pytest.raises(ExecutionError, match="protection lists changed"):
            await _real(session, run, _gateway(radarr={1: FakeRadarr()}))


# ---------------------------------------------------------------------------
# Overrides reach a run already in flight
# ---------------------------------------------------------------------------


class TestAnOverrideChangedMidRun:
    """A 200-item reap takes minutes, and the grace window exists so the owner can change
    their mind inside it. Reading the decisions only once, before the first item, would
    make a Spare clicked while the run is in flight invisible to it, and the file would go
    anyway.

    Each test hangs its change off the ARM re-check, which the executor runs at the top of
    every item, immediately before it re-reads the overrides. That is the moment a decision
    committed from another screen has to land.

    A Spare arrives on **another connection**. It is an API request, handled by its own
    session, while this run holds one of its own. So the change is committed from a
    separate session here, which is the shape that proves the run session actually sees
    it: the run session commits after every item, so its next read starts a fresh
    transaction and sees what the other one wrote. Committing on the run's own session
    instead would test the executor against a decision it could never have missed.
    """

    @staticmethod
    def _on_second_item(action: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[bool]]:
        """An arm re-check that stays armed and fires ``action`` as item two begins."""
        calls = {"n": 0}

        async def recheck() -> bool:
            calls["n"] += 1
            if calls["n"] == 2:
                await action()
            return True

        return recheck

    @staticmethod
    async def _decide_elsewhere(
        session: AsyncSession, *, media_key: str, decision: str | None
    ) -> None:
        """Record (or withdraw) a decision on a SECOND session against the same database,
        the way the review queue's own request does, and commit it there."""
        async_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            session.bind, expire_on_commit=False, autoflush=False
        )
        async with async_factory() as other:
            if decision is None:
                await whitelist.remove_override(other, media_key=media_key)
            else:
                await whitelist.set_override(
                    other,
                    media_key=media_key,
                    title="Worthless 1",
                    decision=decision,
                    note=None,
                )
            await other.commit()

    async def test_a_spare_committed_mid_run_keeps_the_file(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        run = await _plan(session, snapshot_id)

        async def spare_it() -> None:
            await self._decide_elsewhere(session, media_key="radarr:1:2", decision="spare")

        radarr = FakeRadarr()
        report = await _real(
            session,
            run,
            _gateway(radarr={1: radarr}),
            armed_recheck=self._on_second_item(spare_it),
        )

        assert report.state is RunState.COMPLETED
        assert radarr.delete_calls == [1]  # the second item was never sent
        assert report.deleted_items == 1
        assert report.skipped == 1
        kept = next(o for o in report.outcomes if o.media_key == "radarr:1:2")
        assert kept.state is StepState.SKIPPED
        assert kept.detail.id == "error.reap.step.spared_by_hand"

    async def test_withdrawing_a_hand_reap_mid_run_keeps_the_file(
        self, session: AsyncSession
    ) -> None:
        """The mirror case. The check consults the whole effective verdict, not just the
        spare map, because an item planned only because it was hand-reaped drops out the
        moment that reap is withdrawn."""
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        # Item two is not scan-condemned. A hand reap is what puts it in the plan.
        second = (
            await session.execute(select(Candidate).where(Candidate.media_key == "radarr:1:2"))
        ).scalar_one()
        second.verdict = "abstain"
        await whitelist.set_override(
            session, media_key="radarr:1:2", title="Worthless 1", decision="reap", note=None
        )
        await session.flush()
        run = await _plan(session, snapshot_id)

        async def unreap_it() -> None:
            await self._decide_elsewhere(session, media_key="radarr:1:2", decision=None)

        radarr = FakeRadarr()
        report = await _real(
            session,
            run,
            _gateway(radarr={1: radarr}),
            armed_recheck=self._on_second_item(unreap_it),
        )

        assert report.state is RunState.COMPLETED
        assert radarr.delete_calls == [1]
        kept = next(o for o in report.outcomes if o.media_key == "radarr:1:2")
        assert kept.state is StepState.SKIPPED
        # Distinct ids. The run-start-set skip below covers a different case that could
        # read the same way. Both are kept, so only the id tells them apart, and merging
        # either into the other puts the two facts back under one sentence.
        assert kept.detail.id == "error.reap.step.hand_reap_removed"
        assert kept.checks[0].label.id == "error.reap.check.hand_reap_removed"

    async def test_a_spare_withdrawn_mid_run_does_not_name_a_hand_reap(
        self, session: AsyncSession
    ) -> None:
        """The run-start set is what keeps this item, and it is not a hand reap.

        The item is scan-condemned, so it never had a hand reap to remove. It is spared
        before the run is claimed, which drops it from ``_effective_keys``, and the spare is
        withdrawn while the run walks, so the freshly re-read decisions condemn it again.
        The first half of the guard fires alone and the second does not. It is kept either
        way, so only the wording can tell the two halves apart.
        """
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        run = await _plan(session, snapshot_id)
        # Spared before the claim, so the run's ceiling is item one alone.
        await whitelist.set_override(
            session, media_key="radarr:1:2", title="Worthless 1", decision="spare", note=None
        )
        await session.flush()

        async def unspare_it() -> None:
            await self._decide_elsewhere(session, media_key="radarr:1:2", decision=None)

        radarr = FakeRadarr()
        report = await _real(
            session,
            run,
            _gateway(radarr={1: radarr}),
            armed_recheck=self._on_second_item(unspare_it),
        )

        assert radarr.delete_calls == [1]  # still only what the operator confirmed
        kept = next(o for o in report.outcomes if o.media_key == "radarr:1:2")
        assert kept.state is StepState.SKIPPED
        assert kept.detail.id == "error.reap.step.not_in_confirmed_run"
        assert kept.checks[0].label.id == "error.reap.check.not_in_confirmed_run"
        # The id that belongs to the other half, pinned in
        # ``test_withdrawing_a_hand_reap_mid_run_keeps_the_file``.
        assert kept.detail.id != "error.reap.step.hand_reap_removed"

    async def test_a_reap_added_mid_run_cannot_smuggle_an_item_in(
        self, session: AsyncSession
    ) -> None:
        """The refresh may only ever REMOVE items. The run-start effective set is the ceiling,
        because it is what the caps counted and what the operator's typed phrase described.
        So an item they un-reaped before starting stays out even if the decision flips back
        while the run is walking."""
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        second = (
            await session.execute(select(Candidate).where(Candidate.media_key == "radarr:1:2"))
        ).scalar_one()
        second.verdict = "abstain"
        await whitelist.set_override(
            session, media_key="radarr:1:2", title="Worthless 1", decision="reap", note=None
        )
        await session.flush()
        run = await _plan(session, snapshot_id)
        # Withdrawn again before the run starts, so it is outside the run-start set.
        await whitelist.remove_override(session, media_key="radarr:1:2")
        await session.flush()

        async def re_reap_it() -> None:
            await self._decide_elsewhere(session, media_key="radarr:1:2", decision="reap")

        radarr = FakeRadarr()
        report = await _real(
            session,
            run,
            _gateway(radarr={1: radarr}),
            armed_recheck=self._on_second_item(re_reap_it),
        )

        assert radarr.delete_calls == [1]  # still only the item the operator confirmed
        # It also does not tell them their reap was removed at the moment they added one
        # back, which is the other case the run-start-set skip covers.
        kept = next(o for o in report.outcomes if o.media_key == "radarr:1:2")
        assert kept.detail.id == "error.reap.step.not_in_confirmed_run"

    async def test_an_unreadable_override_read_stops_the_run(self, session: AsyncSession) -> None:
        """Fail-closed. If the decisions cannot be re-read we cannot prove the next file is
        still one the owner wants gone, so the run halts rather than falling back on a map
        that may be minutes old."""
        snapshot_id = await _snapshot_many(
            session, [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 9 * GB, 702)]
        )
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()

        real_overrides = whitelist.overrides
        calls = {"n": 0}

        async def flaky(sess: AsyncSession) -> dict[str, str]:
            # Reads 1 and 2 are the run-start load and item one's refresh. The database
            # "goes away" before item two is decided.
            calls["n"] += 1
            if calls["n"] > 2:
                raise RuntimeError("the database went away")
            return await real_overrides(sess)

        with mock.patch.object(whitelist_module, "overrides", flaky):
            report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert report.state is RunState.ABORTED
        assert report.aborted_reason is not None
        assert report.aborted_reason.id == "error.reap.overrides_unreadable"
        assert radarr.delete_calls == [1]  # the first item went. The second was never sent

    async def test_the_decisions_are_re_read_before_every_item(self, session: AsyncSession) -> None:
        """This interlock's own tripwire. It pins one arm deliberately.

        The behavioral tests above cannot tell this interlock's two arms apart. Each
        fails whether the per-item re-read is deleted or the per-item commit that lets
        another session's write become visible is deleted. This one fails only for the
        re-read, by counting the reads: once before the first item, then once more before
        each. Hoisting the re-read back out of the loop would drop the count to one, and
        would make a Spare clicked during a long reap invisible again.
        """
        snapshot_id = await _snapshot_many(
            session,
            [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 1 * GB, 702), ("radarr:1:3", 1 * GB, 703)],
        )
        run = await _plan(session, snapshot_id)
        radarr = FakeRadarr()

        real_overrides = whitelist.overrides
        reads = {"n": 0}

        async def counted(sess: AsyncSession) -> dict[str, str]:
            # Delegates to the real read. A test must never re-implement what it checks.
            reads["n"] += 1
            return await real_overrides(sess)

        with mock.patch.object(whitelist_module, "overrides", counted):
            report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert report.state is RunState.COMPLETED
        assert len(radarr.delete_calls) == 3
        assert reads["n"] == 4  # the run-start load, then one before each of the three items


# ---------------------------------------------------------------------------
# A file that is gone is charged, whatever the verification said
# ---------------------------------------------------------------------------


class TestARemovalIsCountedEvenWhenTheStepFails:
    """Radarr honors the delete, the file goes, and the import exclusion never appears
    inside the poll window. The step is FAILED, correctly, since the verification failed.
    But the bytes are off disk, so counting only VERIFIED steps would charge them against
    nothing. Repeat that on an intermittently slow Radarr and the monthly budget would be
    spent past, with the cap check reporting a number it knows to be short.
    """

    @staticmethod
    def _settings() -> ProfileSettings:
        return ProfileSettings(
            max_items_per_run=1,
            max_bytes_per_run=10_000 * GB,
            max_items_per_30d=2,
            max_bytes_per_30d=10_000 * GB,
        )

    async def test_the_step_stays_failed_but_the_removal_is_stamped(
        self, session: AsyncSession
    ) -> None:
        """The state must keep telling the truth. Marking it VERIFIED would make the
        journal and the after-action report claim an exclusion that never landed."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)

        report = await _real(session, run, _gateway(radarr={1: FakeRadarr(land_exclusion=False)}))

        # The sole item is the canary, so an unconfirmed delete halts the run. This is by
        # design.
        assert report.state is RunState.ABORTED
        step = (await _steps(session, run.id))[0]
        assert step.state is StepState.FAILED  # the verification really did fail
        assert step.file_removed_at is not None  # and the file really did go

    async def test_it_is_charged_against_the_rolling_budget(self, session: AsyncSession) -> None:
        """The point of the stamp. Two such deletions fill a cap of two, and the third
        run is refused. Without the stamp, an unlimited number of them would spend
        nothing."""
        settings = self._settings()
        for i in (1, 2):
            snapshot_id = await _snapshot_one(
                session, media_key=f"radarr:1:{i}", rating_key=700 + i
            )
            run = await _plan(session, snapshot_id)
            await _real(
                session,
                run,
                _gateway(radarr={1: FakeRadarr(land_exclusion=False)}),
                settings=settings,
            )

        third_snapshot = await _snapshot_one(session, media_key="radarr:1:3", rating_key=703)
        third = await _plan(session, third_snapshot)
        radarr = FakeRadarr()
        report = await _real(session, third, _gateway(radarr={1: radarr}), settings=settings)

        assert report.state is RunState.ABORTED
        assert report.aborted_reason is not None
        assert report.aborted_reason.id == "error.reap.items_over_rolling_cap"
        assert report.aborted_reason.params.get("cap") == 2
        assert radarr.delete_calls == []

    async def test_the_run_reports_that_the_library_changed(self, session: AsyncSession) -> None:
        """The rescan trigger. Reading the confirmed count alone would leave the queue
        offering files that were already gone."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)

        report = await _real(session, run, _gateway(radarr={1: FakeRadarr(land_exclusion=False)}))

        assert report.deleted_items == 0  # nothing is CONFIRMED deleted
        assert report.removed_unconfirmed == 1
        assert report.library_changed is True

    async def test_a_failure_before_the_delete_does_not_claim_a_removal(
        self, session: AsyncSession
    ) -> None:
        """The mirror case. A movie that would not delete at all changed nothing, so
        nothing is stamped and no rescan is owed."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)

        report = await _real(session, run, _gateway(radarr={1: FakeRadarr(become_gone=False)}))

        assert report.removed_unconfirmed == 0
        assert report.library_changed is False
        assert (await _steps(session, run.id))[0].file_removed_at is None

    async def test_a_delete_that_cannot_be_re_read_is_still_charged(
        self, session: AsyncSession
    ) -> None:
        """The third answer. Radarr accepted the delete, then went unreachable for the
        confirming re-read, so nobody knows whether the movie is there. That is different
        from the mirror above, where a clean read proved it was. The file almost certainly
        went, and leaving it uncharged would let an intermittently slow Radarr buy
        unlimited deletions past the monthly budget.
        """
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)

        report = await _real(session, run, _gateway(radarr={1: UnreachableAfterDelete()}))

        step = (await _steps(session, run.id))[0]
        assert step.state is StepState.FAILED  # nothing was confirmed, so nothing claims it was
        assert step.file_removed_at is not None  # but the bytes are charged
        assert report.removed_unconfirmed == 1
        assert report.library_changed is True

    async def test_the_operator_is_not_told_the_movie_is_still_there(
        self, session: AsyncSession
    ) -> None:
        """The copy has to distinguish the two failures, because they need opposite
        responses. A movie Radarr refused to delete is still on disk and can be retried.
        A movie it could not re-read is gone. Printing "the movie is still there" for the
        second would send the operator looking for a file nobody can find.
        """
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)

        await _real(session, run, _gateway(radarr={1: UnreachableAfterDelete()}))

        # `error` stores the reason's code, not its English, so decode it through the
        # real function before composing. This is the same `from_stored` the API route
        # reads the column with.
        reason = from_stored((await _steps(session, run.id))[0].error)
        assert reason is not None
        error = refusal_text(reason.id, **reason.params).lower()
        assert "could not reach it again" in error
        assert "still there" not in error

    async def test_the_reason_reaches_the_browser_from_the_journal(
        self, session: AsyncSession
    ) -> None:
        """A reopened run carries why the step failed, not just that it did.

        The executor writes one sentence twice, durably to ``action_step.error``, and
        into the in-memory ``StepOutcome`` the run report is built from. That report
        lives on ``app.state`` and is gone after a restart or the next run, so the
        durable copy is the only one a reopened run can read back from. It must be on the
        response schema, or a run reopened from history would show a failed step with no
        reason. This test drives the same failure as the test above, then reads it back
        through the detail route's own builder.

        The HTTP half of this pair is ``test_api``'s
        ``test_a_failed_step_reads_back_why_it_failed``, which drives the route rather
        than the builder. This test proves a real executor failure populates the reason,
        where that one writes the row itself.
        """
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)

        # Planned and not yet sent. Nothing has failed, so every step carries no reason at
        # all, rather than one the browser would have to tell apart from a real one.
        planned = await _run_out(session, run)
        assert planned.steps and all(s.error_reason is None for s in planned.steps)

        await _real(session, run, _gateway(radarr={1: UnreachableAfterDelete()}))

        out = await _run_out(session, run)
        failed = [s for s in out.steps if s.state == StepState.FAILED.value]
        assert failed, "the scenario stopped failing a step, so this proves nothing"
        assert failed[0].error_reason is not None
        assert failed[0].error_reason.k == "error.reap.step.movie_removal_unconfirmed"

    async def test_a_season_delete_that_cannot_be_re_read_is_charged_too(
        self, session: AsyncSession
    ) -> None:
        """The same risk in a different shape as the movie path's version of this test.
        There, an unreadable re-read could collapse into a return value. Here, the season
        path lets it raise instead, which could unwind past the stamp entirely. Either
        way, the risk is reclaimed bytes charged to nothing, which this test proves does
        not happen.
        """

        class UnreachableAfterDelete(FakeSonarr):
            async def episode_files(self, series_id: int) -> list[dict[str, Any]]:
                if self.delete_calls:
                    raise IntegrationError("sonarr", "timed out", status=None)
                return await super().episode_files(series_id)

        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=800, media_type="season"
        )
        run = await _plan(session, snapshot_id)
        sonarr = UnreachableAfterDelete()

        report = await _real(session, run, _gateway(sonarr={1: sonarr}))

        assert sonarr.delete_calls == [[101, 102]]  # the files really were deleted
        steps = {s.kind: s for s in await _steps(session, run.id)}
        delete_step = steps["sonarr_delete_files"]
        assert delete_step.state is StepState.FAILED  # nothing confirmed it
        assert delete_step.file_removed_at is not None  # but the bytes are charged
        assert report.library_changed is True
        reason = from_stored(delete_step.error)
        assert reason is not None
        assert "could not reach it again" in refusal_text(reason.id, **reason.params).lower()


# ---------------------------------------------------------------------------
# The progress bar counts the set the operator authorized
# ---------------------------------------------------------------------------


class TestProgressIsDenominatedInTheActedOnSet:
    """``deletes`` deliberately keeps items spared after the plan was built, so the report
    can say they were kept. Counting those in the denominator would flip the bar mid-run
    to a number LARGER than the one the operator typed to authorize, and would disagree
    with the header in the same window.
    """

    async def test_a_spare_after_planning_does_not_inflate_the_total(
        self, session: AsyncSession
    ) -> None:
        snapshot_id = await _snapshot_many(
            session,
            [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 2 * GB, 702), ("radarr:1:3", 3 * GB, 703)],
        )
        run = await _plan(session, snapshot_id)
        await whitelist.set_override(
            session, media_key="radarr:1:3", title="Worthless 2", decision="spare", note=None
        )
        await session.flush()

        seen: list[ReapProgress] = []
        executor = Executor(
            session,
            safety=_armed(),
            settings=ProfileSettings(),
            dry_run=False,
            gateway=_gateway(radarr={1: FakeRadarr()}),
            armed_recheck=_armed_forever,
            progress=seen.append,
            exclusion_poll_delay=0.0,
            plex_settle_delay=0.0,
        )
        report = await executor.execute(run.id)

        # Two items are acted on, and that is what the operator's phrase counts too.
        assert len(await _planned_candidates(session, run)) == 2
        assert {p.total for p in seen} == {2}
        assert seen[-1].done == 2  # and the bar still finishes
        assert report.skipped == 1  # the spared one is reported, just not denominated


# ---------------------------------------------------------------------------
# The season path: Plex, and a season with nothing left to delete
# ---------------------------------------------------------------------------


class TestASeasonPruneTidiesPlexToo:
    """The trash interlock must cover every deletion routed through an *arr, both movies
    and seasons. Without that, a TV section would never join the affected set and its
    trash would never be purged, leaving stale "unavailable" episodes until Plex's own
    scheduled scan.
    """

    async def test_a_pruned_season_refreshes_its_series_folder(self, session: AsyncSession) -> None:
        """The refresh is the point. The purge deliberately is not.

        A Plex TV section counts SHOWS, so pruning one season of a multi-season show
        moves that count by zero. The prune therefore claims no section entry, the
        count-delta gate has no allowance to spend, and it declines. Claiming one per
        season instead would authorize a shrink of up to N shows on the most dangerous
        call in the application, and a shrink of that size could only have come from
        something other than this run, since our own prunes move the count by nothing.
        The cost is a lingering "unavailable" entry until Plex's own scan, which is
        cosmetic.
        """
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=701, media_type="season"
        )
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Shows": ["/tv"]}, item_counts={"Shows": [50, 49]})

        class _PathedSonarr(FakeSonarr):
            async def series_by_id(self, series_id: int) -> dict[str, Any]:
                return {**await super().series_by_id(series_id), "path": "/tv/A Show"}

        report = await _real(session, run, _gateway(sonarr={1: _PathedSonarr()}, plex=plex))

        assert report.state is RunState.COMPLETED
        # Scoped to the series' own folder, never the whole library.
        assert plex.refreshed == [("Shows", "/tv/A Show")]
        # The section is left for Plex to tidy. A shrink this run cannot account for is
        # never ours to purge, even when the numbers happen to look right.
        assert plex.emptied == []

    async def test_a_movie_delete_still_purges_its_section(self, session: AsyncSession) -> None:
        """This is the scope check on the test above. TV claims no entry, movies still
        claim one, so the count-delta gate still confirms and purges for the movie path.
        Without this, the assertion above would pass just as well if purging had been
        disabled everywhere."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Films": ["/movies"]}, item_counts={"Films": [50, 49]})

        report = await _real(
            session,
            run,
            _gateway(radarr={1: FakeRadarr(path="/movies/One")}, plex=plex),
        )

        assert report.state is RunState.COMPLETED
        assert plex.refreshed == [("Films", "/movies/One")]
        assert plex.emptied == ["Films"]

    async def test_a_series_with_no_path_simply_does_not_refresh(
        self, session: AsyncSession
    ) -> None:
        """This is best-effort, exactly like the movie path. The files are gone either
        way, and an unmappable path costs a lingering entry, never a lost file."""
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=701, media_type="season"
        )
        run = await _plan(session, snapshot_id)
        plex = FakePlex(sections={"Shows": ["/tv"]})

        report = await _real(session, run, _gateway(sonarr={1: FakeSonarr()}, plex=plex))

        assert report.state is RunState.COMPLETED
        assert plex.refreshed == []
        assert plex.emptied == []


class TestTheCommonParentOfTheDeletedFiles:
    """``_common_parent`` decides how much of the library the post-prune rescan reaches.

    A rescan is a mutation in effect on a Plex server that empties its trash after every
    scan, so every segment this keeps is a folder whose other items Plex is not invited to
    trash and purge. The refusals matter as much as the successes. Returning the disk
    root would scope the rescan to everything.
    """

    def test_it_finds_the_folder_the_files_share(self) -> None:
        assert (
            _common_parent(["/tv/Show/Season 03/a.mkv", "/tv/Show/Season 03/b.mkv"])
            == "/tv/Show/Season 03"
        )

    def test_one_file_still_yields_its_own_folder(self) -> None:
        assert _common_parent(["/tv/Show/Season 03/a.mkv"]) == "/tv/Show/Season 03"

    def test_it_climbs_only_as_far_as_it_must(self) -> None:
        """Files split across season folders share the show, and nothing narrower."""
        assert (
            _common_parent(["/tv/Show/Season 03/a.mkv", "/tv/Show/Season 04/b.mkv"]) == "/tv/Show"
        )

    def test_it_refuses_the_disk_root(self) -> None:
        """Sharing nothing but ``/`` is not a scope. The caller falls back instead."""
        assert _common_parent(["/one/a.mkv", "/two/b.mkv"]) == ""

    def test_it_ignores_paths_it_cannot_read(self) -> None:
        assert _common_parent(["", "not/absolute.mkv", "/atroot.mkv"]) == ""


class TestTheSeasonRescanIsScopedToTheSeasonsOwnFolder:
    """Pruning one season must not hand Plex the whole series to rescan.

    Passing ``series["path"]`` instead of the season's own folder would make a prune of
    S03 rescan every season of the show. On a server set to empty its trash after every
    scan, that is a purge. Any other season whose files had gone missing out of band
    would lose its library records, watch state, ratings, collections, inside Plex,
    where none of ``_finalize_plex``'s interlocks can see it.
    """

    class _PathfulSonarr(FakeSonarr):
        """Reports file paths and a series root, the way a real Sonarr does."""

        async def series_by_id(self, series_id: int) -> dict[str, Any]:
            series = await super().series_by_id(series_id)
            series["path"] = "/tv/Show"
            return series

    async def test_only_the_pruned_seasons_folder_is_rescanned(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=701, media_type="season"
        )
        run = await _plan(session, snapshot_id)
        sonarr = self._PathfulSonarr(
            files=[
                {
                    "id": 101,
                    "seasonNumber": 3,
                    "size": 50 * 1024**2,
                    "path": "/tv/Show/Season 03/e01.mkv",
                },
                {
                    "id": 102,
                    "seasonNumber": 3,
                    "size": 50 * 1024**2,
                    "path": "/tv/Show/Season 03/e02.mkv",
                },
                # A season this run never approved. Its folder must not be rescanned.
                {
                    "id": 900,
                    "seasonNumber": 4,
                    "size": 50 * 1024**2,
                    "path": "/tv/Show/Season 04/e01.mkv",
                },
            ]
        )
        plex = FakePlex(sections={"TV": ["/tv"]})

        report = await _real(session, run, _gateway(sonarr={1: sonarr}, plex=plex))

        assert report.deleted_items == 1
        assert plex.refreshed == [("TV", "/tv/Show/Season 03")]


class TestAFileThatLandsWhileTheSeasonIsBeingPruned:
    """The size gate weighs one read of the season's files. The delete resolves a second.

    Without something tying the two reads together, a file that arrives in the window
    could be deleted having never been weighed. ``_grew_materially`` cannot catch it
    either. It only refuses growth past ``max(10%, 256 MiB)``, and an arrival in this
    window is far under that floor, so the gate would pass and the file would be removed
    anyway. Worse, the run would charge the frozen approved size, so those bytes would be
    deleted and charged to nothing. The season is kept instead.
    """

    class _ImportingSonarr(FakeSonarr):
        """A Sonarr import completes between the size re-read and the live resolve."""

        def __init__(self, **kw: Any) -> None:
            super().__init__(**kw)
            self._reads = 0

        async def episode_files(self, series_id: int) -> list[dict[str, Any]]:
            self._reads += 1
            files = await super().episode_files(series_id)
            if self._reads >= 2:
                # 10 MiB, nowhere near the growth interlock's 256 MiB floor.
                files.append({"id": 103, "seasonNumber": self._season, "size": 10 * 1024**2})
            return files

    async def test_the_season_is_kept_and_nothing_is_deleted(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=701, media_type="season"
        )
        run = await _plan(session, snapshot_id)
        sonarr = self._ImportingSonarr()

        report = await _real(session, run, _gateway(sonarr={1: sonarr}))

        assert report.deleted_items == 0
        assert report.deleted_bytes == 0
        assert sonarr.delete_calls == []
        assert report.outcomes[0].detail.id == "error.reap.step.season_files_arrived"

    async def test_the_operator_is_told_the_season_was_left_unmonitored(
        self, session: AsyncSession
    ) -> None:
        """This skip is reached after the unmonitor took and was verified, so the season
        is spared but not untouched. Sonarr has stopped grabbing for it. Told only
        "Kept", an operator would never think to turn monitoring back on, so this message
        must also say the season was left unmonitored, the same way the files-vanished
        skip below already does.
        """
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=701, media_type="season"
        )
        run = await _plan(session, snapshot_id)
        sonarr = self._ImportingSonarr()

        report = await _real(session, run, _gateway(sonarr={1: sonarr}))

        assert sonarr.unmonitor_calls == [(42, 3)]  # it really did happen
        outcome = report.outcomes[0]
        said = (
            _label_text(outcome.detail)
            + " "
            + " ".join(_label_text(c.label) for c in outcome.checks)
        ).lower()
        assert "left unmonitored" in said


class TestASeasonWithNothingLeftToDelete:
    """The episode file ids are resolved live, immediately before the delete. If that
    resolve comes back empty, the files went out of band between the size re-read and
    here. ``delete_episode_files([])`` is a documented no-op, so nothing remains to
    delete. Marking the season VERIFIED anyway would assert a deletion that never
    happened, charged in full against the rolling budget. "No files" is a skip instead.
    """

    class _VanishingSonarr(FakeSonarr):
        """Reports files for the size re-read, then none for the live resolve."""

        def __init__(self, **kw: Any) -> None:
            super().__init__(**kw)
            self._reads = 0

        async def episode_files(self, series_id: int) -> list[dict[str, Any]]:
            self._reads += 1
            if self._reads >= 2:
                return []
            return await super().episode_files(series_id)

    async def test_it_is_skipped_not_verified(self, session: AsyncSession) -> None:
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=701, media_type="season"
        )
        run = await _plan(session, snapshot_id)
        sonarr = self._VanishingSonarr()

        report = await _real(session, run, _gateway(sonarr={1: sonarr}))

        assert report.deleted_items == 0
        assert report.deleted_bytes == 0
        assert report.skipped == 1
        assert sonarr.delete_calls == []  # no empty delete was even sent
        outcome = report.outcomes[0]
        assert outcome.state is StepState.SKIPPED
        assert outcome.detail.id == "error.reap.step.season_files_vanished"
        detail = _label_text(outcome.detail)
        assert "nothing was deleted" in detail
        # The copy says what did happen to the season. The unmonitor was sent, took, and
        # is still in force (the next test), so a line reading "nothing was sent" would
        # tell the operator the season was untouched when it was not.
        assert "left unmonitored" in detail
        assert "nothing was sent" not in detail
        # Verbatim, because the pre-unmonitor skip's line is this one without the unmonitor
        # clause. ``test_a_season_sonarr_reports_no_files_for_is_kept`` pins that one.
        # Reword both together, or neither.
        assert (
            _label_text(outcome.checks[0].label)
            == "No files left to remove. The season was left unmonitored."
        )

    async def test_the_verified_unmonitor_keeps_its_state(self, session: AsyncSession) -> None:
        """The unmonitor already took, and it is still in force. Overwriting its VERIFIED mark
        with SKIPPED would make the journal deny a reversible edit that really happened."""
        snapshot_id = await _snapshot_one(
            session, media_key="sonarr:1:42:3", rating_key=701, media_type="season"
        )
        run = await _plan(session, snapshot_id)

        await _real(session, run, _gateway(sonarr={1: self._VanishingSonarr()}))

        by_kind = {s.kind: s.state for s in await _steps(session, run.id)}
        assert by_kind["sonarr_unmonitor"] is StepState.VERIFIED
        assert by_kind["sonarr_verify_unmonitor"] is StepState.VERIFIED
        assert by_kind["sonarr_delete_files"] is StepState.SKIPPED


# ---------------------------------------------------------------------------
# One item's surprise cannot wedge the run
# ---------------------------------------------------------------------------


class TestAnUnmappedErrorStopsTheRunWithoutWedgingIt:
    """A raw transport error out of a client, say Plex restarting between the connect and
    the read, must not escape ``_send_for_real`` and ``execute()`` after a file has
    already been deleted. Left unhandled, it would leave the terminal step stuck SENT and
    the run stuck EXECUTING, which ``execute()`` refuses to re-enter and nothing
    reconciles, with no report at all, so the operator could not even see which files had
    gone.

    Journalling the item is not enough on its own either: the run must still halt rather
    than walking on into items 3..N after a failure nobody could classify. Both
    properties are required, and they are what these tests pin apart. An unmapped
    exception means Reaper does not know what just happened or how far it got, and the
    only honest reading of that on a deletion path is to stop touching files. A mapped
    one (an ``IntegrationError``) is a known shape and one item's problem, so the run
    carries on. The canary already proved the mechanism works.
    """

    @staticmethod
    async def _three(session: AsyncSession) -> ReapRun:
        snapshot_id = await _snapshot_many(
            session,
            [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 2 * GB, 702), ("radarr:1:3", 3 * GB, 703)],
        )
        return await _plan(session, snapshot_id)

    async def test_a_raw_error_journals_the_item_and_halts_the_run(
        self, session: AsyncSession, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run = await self._three(session)

        class _SurprisingRadarr(FakeRadarr):
            async def exclusions(self) -> list[dict[str, Any]]:
                # Not an IntegrationError, not a PlexError. This is the shape nothing maps.
                if self.delete_calls and self.delete_calls[-1] == 2:
                    raise ValueError("something nobody anticipated")
                return await super().exclusions()

        radarr = _SurprisingRadarr()
        report = await _real(session, run, _gateway(radarr={1: radarr}))

        # Stopped at the surprise: item three was never sent.
        assert report.state is RunState.ABORTED
        assert radarr.delete_calls == [1, 2]
        assert report.aborted_reason is not None
        assert report.aborted_reason.id == "error.reap.item_failed_unexplained"
        assert report.aborted_reason.params.get("title") == "Worthless 1"  # the item is named
        # The item is journalled, the report exists, and the run is in a terminal state
        # rather than stuck EXECUTING with nothing to reconcile.
        hurt = next(o for o in report.outcomes if o.media_key == "radarr:1:2")
        assert hurt.state is StepState.FAILED
        assert hurt.detail.id == "error.reap.step.item_unexpected"
        steps = {s.media_key: s for s in await _steps(session, run.id)}
        assert steps["radarr:1:2"].state is StepState.FAILED  # not left SENT forever
        assert steps["radarr:1:2"].file_removed_at is not None  # and the removal is charged
        # The terminal state on disk, which is the half of "not wedged" that outlives the
        # process holding the session.
        assert (await _stored_run(async_factory, run.id)).state is RunState.ABORTED

    async def test_a_mapped_failure_still_lets_the_run_carry_on(
        self, session: AsyncSession
    ) -> None:
        """The contrast that makes the halt meaningful. The same failure point, raised as
        the shape the executor does map. One stubborn title is recorded and the rest of
        the run still happens."""
        run = await self._three(session)

        class _UnreachableAtItemTwo(FakeRadarr):
            async def exclusions(self) -> list[dict[str, Any]]:
                if self.delete_calls and self.delete_calls[-1] == 2:
                    raise IntegrationError("radarr", "connection reset")
                return await super().exclusions()

        radarr = _UnreachableAtItemTwo()
        report = await _real(session, run, _gateway(radarr={1: radarr}))

        assert report.state is RunState.COMPLETED
        assert radarr.delete_calls == [1, 2, 3]  # it reached the third item
        hurt = next(o for o in report.outcomes if o.media_key == "radarr:1:2")
        assert hurt.state is StepState.FAILED
        assert hurt.detail.id == "error.reap.step.arr_call_failed"
        # `{error}` is a nested reason (the client's own coded failure via `as_reason()`),
        # not a raw string, so read it through the real composer rather than the dataclass repr.
        nested = hurt.detail.params.get("error")
        assert isinstance(nested, Reason)
        assert "connection reset" in english(nested)

    async def test_a_plex_surprise_during_the_refresh_is_swallowed(
        self, session: AsyncSession
    ) -> None:
        """``_flush_refreshes`` is documented as never fatal, so a handler that only
        catches PlexError is not enough. It runs in the same block that records the run's
        outcome, so anything escaping it replaces that outcome with a Plex error."""
        snapshot_id = await _snapshot_one(session, media_key="radarr:1:1", rating_key=701)
        run = await _plan(session, snapshot_id)

        class _DyingPlex(FakePlex):
            async def section_paths(self) -> list[PlexSectionPaths]:
                raise RuntimeError("the connection went away")

        report = await _real(
            session,
            run,
            _gateway(radarr={1: FakeRadarr(path="/movies/One")}, plex=_DyingPlex()),
        )

        assert report.state is RunState.COMPLETED
        assert report.deleted_items == 1


class _CommitsThatFail:
    """Makes a bounded number of one session's commits fail the way a held write lock does.

    The failure is a REAL flush error (``path`` is NOT NULL), not a raise in front of a
    healthy commit, because what is being pinned is what the session does *after* a commit
    fails. SQLAlchemy leaves a transaction that is neither committed nor rolled back, every
    later commit on it raises ``PendingRollbackError`` whether or not the original fault has
    cleared, and the in-memory writes riding on it are discarded. A raise in front of a live
    transaction reproduces none of that and would pass against the wedged code.

    Armed by the *arr fake mid-item rather than by counting commits, so each test names the
    moment it is aiming at and does not drift when a commit is added or removed upstream.
    """

    def __init__(self, session: AsyncSession, run_id: int, *, times: int = 1) -> None:
        self._session = session
        self._run_id = run_id
        self._remaining = times
        self._armed = False
        self.fired = 0
        self._real = session.commit
        session.commit = self._commit  # type: ignore[method-assign]

    def arm(self) -> None:
        self._armed = True

    async def _commit(self) -> None:
        if self._armed and self._remaining > 0:
            self._remaining -= 1
            self.fired += 1
            self._session.add(
                ActionStep(
                    run_id=self._run_id,
                    media_key="poison",
                    ordinal=99,
                    kind="radarr_delete",
                    method="DELETE",
                    path=None,  # NOT NULL: the flush inside commit() raises
                    idempotency_key=f"poison-{self.fired}",
                    created_at=utcnow(),
                )
            )
        await self._real()


class _RadarrThatLosesTheDatabase(FakeRadarr):
    """Arms the commit failure the instant a file is really gone.

    ``delete_movie`` has returned, ``_movie_is_gone`` reads no database, so the very next
    commit is the one stamping ``file_removed_at``, the worst moment for one to fail, and
    the one the report names as outliving the run.
    """

    def __init__(self, lock: _CommitsThatFail, *, on_movie: int) -> None:
        super().__init__()
        self._lock = lock
        self._on_movie = on_movie

    async def delete_movie(
        self, movie_id: int, *, delete_files: bool = True, add_exclusion: bool = True
    ) -> None:
        await super().delete_movie(movie_id, delete_files=delete_files, add_exclusion=add_exclusion)
        if movie_id == self._on_movie:
            self._lock.arm()


class _RadarrThatFailsTheItemAndTheDatabase(FakeRadarr):
    """Fails the item in a way the run normally survives, and breaks the next commit with it.

    The point is WHICH commit then fails. Every ``_mark_*`` of this item is already
    committed, since ``_mark_sent`` went before the call, and an ``IntegrationError`` is
    a mapped failure, so ``_fail`` only stages FAILED in memory. That leaves the per-item
    commit in ``_run_deletes`` as the one that breaks, on an item the run would otherwise
    walk straight past.
    """

    def __init__(self, lock: _CommitsThatFail, *, on_movie: int) -> None:
        super().__init__()
        self._lock = lock
        self._on_movie = on_movie

    async def delete_movie(
        self, movie_id: int, *, delete_files: bool = True, add_exclusion: bool = True
    ) -> None:
        if movie_id != self._on_movie:
            await super().delete_movie(
                movie_id, delete_files=delete_files, add_exclusion=add_exclusion
            )
            return
        # The call really was made, so the test can say how far the run got.
        self.delete_calls.append(movie_id)
        self._lock.arm()
        raise IntegrationError("radarr", "the delete could not be confirmed")


class _PlexThatLosesTheDatabase(FakePlex):
    """Arms the commit failure from the streaming veto, which spares the item.

    A spared item commits no marks of its own, so the very next commit is the per-item one
    in ``_run_deletes``, the other place a journal write can fail, and the one that carries
    the SKIPPED marks.
    """

    def __init__(self, lock: _CommitsThatFail, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._lock = lock

    async def active_streams(self) -> list[ActiveStream]:
        streams = await super().active_streams()
        self._lock.arm()
        return streams


async def _fresh_engine(tmp_path: Path) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    """An engine of this test's own, so durable state can be read back through a session the
    executor never touched. The run's own session answers from its identity map
    (``expire_on_commit=False``) and cannot tell a durable write from a discarded one."""
    settings = Settings(data_dir=tmp_path, secret_key="test-key")
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, create_session_factory(engine)


class TestAFailedJournalCommitDoesNotWedgeTheRun:
    """A commit on the deletion path can fail. The housekeeping vacuum holding SQLite's
    write lock, a full disk, any transient error. Left unhandled, that would leave the
    run EXECUTING on disk permanently, with a step still SENT for a file that was already
    gone, and nothing in the app able to reconcile it. ``execute()`` refuses any
    non-PLANNED run, no startup path reads EXECUTING, and retention never sweeps a
    run-bound snapshot.

    This is not a race on how long the fault lasted. A failed commit leaves the session
    holding a transaction that is neither committed nor rolled back. Every later commit
    on it raises whether or not the lock has been released, every row it loaded is
    expired, and the in-memory writes riding on it are discarded. A statement that would
    record the terminal state must never sit under a bare ``except Exception:
    log.warning`` that swallows it.

    So these tests break the transaction for real rather than raising in front of a
    healthy one. That distinction matters: a bare raise leaves a working session, which
    recovers by itself, and would pass against wedged code.
    """

    @staticmethod
    async def _three(session: AsyncSession) -> ReapRun:
        snapshot_id = await _snapshot_many(
            session,
            [("radarr:1:1", 1 * GB, 701), ("radarr:1:2", 2 * GB, 702), ("radarr:1:3", 3 * GB, 703)],
        )
        return await _plan(session, snapshot_id)

    async def test_one_failed_commit_is_recovered_and_the_run_finishes(
        self, tmp_path: Path
    ) -> None:
        """The fault clears. The run rolls back, replays the write, and carries on to a
        normal COMPLETED finish.

        The stamp is the assertion that matters. ``_mark_file_removed`` is the commit
        being broken here, and losing it would leave a file gone with nothing on disk
        saying so. Those bytes would drop out of ``_rolling_30d_deletions``, the rolling
        budget would read light, and a later run could spend past what the operator set.
        That is the part of a failed commit that would outlive the run.
        """
        engine, async_factory = await _fresh_engine(tmp_path)
        try:
            async with async_factory() as setup:
                run = await self._three(setup)
                run_id = run.id
                await setup.commit()

            async with async_factory() as run_session:
                lock = _CommitsThatFail(run_session, run_id)
                radarr = _RadarrThatLosesTheDatabase(lock, on_movie=2)
                report = await Executor(
                    run_session,
                    safety=_armed(),
                    settings=ProfileSettings(),
                    dry_run=False,
                    gateway=_gateway(radarr={1: radarr}),
                    armed_recheck=_armed_forever,
                    exclusion_poll_delay=0.0,
                    plex_settle_delay=0.0,
                ).execute(run_id)

            assert lock.fired == 1, "the test did not actually break a commit"
            assert report.state is RunState.COMPLETED
            assert report.deleted_items == 3
            assert radarr.delete_calls == [1, 2, 3]

            async with async_factory() as fresh:
                stored = await fresh.get(ReapRun, run_id)
                assert stored is not None
                assert stored.state is RunState.COMPLETED
                steps = {s.media_key: s for s in await _steps(fresh, run_id)}
                assert [s.state for s in steps.values()] == [StepState.VERIFIED] * 3
                # The write that failed, on disk anyway.
                assert steps["radarr:1:2"].file_removed_at is not None
        finally:
            await engine.dispose()

    async def test_a_journal_that_stays_unwritable_ends_the_run_aborted_on_disk(
        self, tmp_path: Path
    ) -> None:
        """The wedge itself. One retry is what the executor offers, not a promise, so
        when the write still will not land the run stops. The run row says ABORTED on
        disk, rather than EXECUTING forever."""
        engine, async_factory = await _fresh_engine(tmp_path)
        try:
            async with async_factory() as setup:
                run = await self._three(setup)
                run_id = run.id
                await setup.commit()

            async with async_factory() as run_session:
                lock = _CommitsThatFail(run_session, run_id, times=2)
                radarr = _RadarrThatLosesTheDatabase(lock, on_movie=1)
                report = await Executor(
                    run_session,
                    safety=_armed(),
                    settings=ProfileSettings(),
                    dry_run=False,
                    gateway=_gateway(radarr={1: radarr}),
                    armed_recheck=_armed_forever,
                    exclusion_poll_delay=0.0,
                    plex_settle_delay=0.0,
                ).execute(run_id)

            assert lock.fired == 2, "both the commit and its retry must have failed"
            assert report.state is RunState.ABORTED
            # It stopped rather than deleting more it also could not record.
            assert radarr.delete_calls == [1]
            assert report.aborted_reason is not None
            assert report.aborted_reason.id == "error.reap.journal_halt"
            reason = refusal_text(report.aborted_reason.id)
            assert "could not save its record" in reason
            assert "stays removed" in reason

            async with async_factory() as fresh:
                stored = await fresh.get(ReapRun, run_id)
                assert stored is not None
                assert stored.state is RunState.ABORTED, "the run was left wedged in EXECUTING"
                # `aborted_reason` stores the reason's code, not its English, so decode it
                # through the real function rather than a transcribed comparison.
                assert from_stored(stored.aborted_reason) == report.aborted_reason
                assert stored.finished_at is not None
        finally:
            await engine.dispose()

    async def test_a_halted_item_whose_file_is_gone_is_still_named_and_still_stamped(
        self, tmp_path: Path
    ) -> None:
        """The same halt as above, read for what it leaves behind rather than for the run row.

        The halt fires from inside the send, so no ``StepOutcome`` is ever built, and the
        item could fall out of the report entirely. Without this fix, the sheet would say
        "0 souls reclaimed" beside an abort reading "Anything already removed stays
        removed", naming no file at all. ``library_changed`` would stay False with it, so
        the post-run rescan would never fire and the queue would keep offering a file
        that is gone.

        The stamp is the other half. Both attempts fail inside the seconds the fault is
        live, and the database is writable again by the time the run winds up. The
        terminal write proves it, landing on the very same session, so the write that was
        taken down is tried once more there. Losing it would leave a file gone with
        nothing on disk saying so, and those bytes would never charge the rolling 30-day
        budget.
        """
        engine, async_factory = await _fresh_engine(tmp_path)
        try:
            async with async_factory() as setup:
                run = await self._three(setup)
                run_id = run.id
                await setup.commit()

            async with async_factory() as run_session:
                lock = _CommitsThatFail(run_session, run_id, times=2)
                radarr = _RadarrThatLosesTheDatabase(lock, on_movie=1)
                report = await Executor(
                    run_session,
                    safety=_armed(),
                    settings=ProfileSettings(),
                    dry_run=False,
                    gateway=_gateway(radarr={1: radarr}),
                    armed_recheck=_armed_forever,
                    exclusion_poll_delay=0.0,
                    plex_settle_delay=0.0,
                ).execute(run_id)

            assert lock.fired == 2, "both the commit and its retry must have failed"
            assert radarr.delete_calls == [1], "the file really did go"

            # The operator is told which file went, not handed an empty sheet.
            assert [o.media_key for o in report.outcomes] == ["radarr:1:1"]
            assert report.outcomes[0].state is StepState.FAILED
            assert report.outcomes[0].file_removed is True
            # The rescan fires, so the queue stops offering it.
            assert report.removed_unconfirmed == 1
            assert report.library_changed is True

            async with async_factory() as fresh:
                steps = {s.media_key: s for s in await _steps(fresh, run_id)}
                assert steps["radarr:1:1"].file_removed_at is not None, (
                    "the stamp was dropped, so the rolling budget never charges these bytes"
                )
        finally:
            await engine.dispose()

    async def test_an_acted_on_item_whose_journal_will_not_write_stops_the_run_too(
        self, tmp_path: Path
    ) -> None:
        """The halt on the real-delete branch, which no other test here reaches.

        Its sibling on the SKIPPED branch has a test. This branch does not, because every
        other test here arms at ``_mark_file_removed`` and so halts through
        ``_JournalWriteError`` out of ``_mark``, never reaching the ``journalled`` check
        at all. So this arms the one commit the others do not, a mapped failure the run
        would normally walk past, whose per-item commit is the one that breaks.

        The reason is the assertion that discriminates, not the delete count. Deleting
        the two lines that check the reason would still leave this run stopping before
        the third file, because the transaction is dead by then and the next item's own
        re-reads fail one item later, on an error that says nothing about the journal,
        and only because both attempts failed here. Where the session survives (a
        rollback that works and a ``_revive`` that does not), nothing stops the walk at
        all. What this test pins is that the run halts at the unwritten journal and says
        so, rather than wandering on to fail for some unrelated reason.
        """
        engine, async_factory = await _fresh_engine(tmp_path)
        try:
            async with async_factory() as setup:
                run = await self._three(setup)
                run_id = run.id
                await setup.commit()

            async with async_factory() as run_session:
                lock = _CommitsThatFail(run_session, run_id, times=2)
                radarr = _RadarrThatFailsTheItemAndTheDatabase(lock, on_movie=2)
                report = await Executor(
                    run_session,
                    safety=_armed(),
                    settings=ProfileSettings(),
                    dry_run=False,
                    gateway=_gateway(radarr={1: radarr}),
                    armed_recheck=_armed_forever,
                    exclusion_poll_delay=0.0,
                    plex_settle_delay=0.0,
                ).execute(run_id)

            assert lock.fired == 2, "both the commit and its retry must have failed"
            # The third file is the assertion. The run stopped rather than deleting more
            # it also could not record.
            assert radarr.delete_calls == [1, 2]
            assert report.state is RunState.ABORTED
            assert report.aborted_reason is not None
            assert report.aborted_reason.id == "error.reap.journal_halt"

            async with async_factory() as fresh:
                stored = await fresh.get(ReapRun, run_id)
                assert stored is not None
                assert stored.state is RunState.ABORTED
        finally:
            await engine.dispose()

    async def test_a_recovered_per_item_commit_still_journals_the_item(
        self, tmp_path: Path
    ) -> None:
        """The other commit on the path, recovered rather than halted.

        A spared item writes no marks of its own, since ``_mark_skipped`` only sets
        attributes, so the per-item commit in ``_run_deletes`` is the one that fails
        here, and the rollback that revives the session discards exactly those
        attributes. Only the row capture taken before the commit can put them back.
        Without it the commit would succeed writing nothing, and the step would be left
        PENDING on disk, a journal that reads as an interrupted run with work still to
        do, for an item that was deliberately kept.
        """
        engine, async_factory = await _fresh_engine(tmp_path)
        try:
            async with async_factory() as setup:
                run = await self._three(setup)
                run_id = run.id
                await setup.commit()

            async with async_factory() as run_session:
                lock = _CommitsThatFail(run_session, run_id)
                radarr = FakeRadarr()
                plex = _PlexThatLosesTheDatabase(lock, streams=[_stream(rating_key=701)])
                report = await Executor(
                    run_session,
                    safety=_armed(),
                    settings=ProfileSettings(),
                    dry_run=False,
                    gateway=_gateway(radarr={1: radarr}, plex=plex),
                    armed_recheck=_armed_forever,
                    exclusion_poll_delay=0.0,
                    plex_settle_delay=0.0,
                ).execute(run_id)

            assert lock.fired == 1
            assert report.state is RunState.COMPLETED
            assert report.skipped == 1
            assert radarr.delete_calls == [2, 3]  # the run carried on past the recovery

            async with async_factory() as fresh:
                steps = {s.media_key: s for s in await _steps(fresh, run_id)}
                assert steps["radarr:1:1"].state is StepState.SKIPPED, (
                    "the spare was discarded by the recovery rollback and never replayed"
                )
                assert steps["radarr:1:1"].error
                assert steps["radarr:1:2"].state is StepState.VERIFIED
        finally:
            await engine.dispose()

    async def test_a_spared_item_whose_journal_will_not_write_stops_the_run_too(
        self, tmp_path: Path
    ) -> None:
        """The other commit on the path, and the other halt. An item spared by the streaming
        veto writes no marks of its own, so the per-item commit in ``_run_deletes`` is the
        one that fails. A skip returns to the top of the loop, so it needs its own check
        or the run walks on with an unwritable database."""
        engine, async_factory = await _fresh_engine(tmp_path)
        try:
            async with async_factory() as setup:
                run = await self._three(setup)
                run_id = run.id
                await setup.commit()

            async with async_factory() as run_session:
                lock = _CommitsThatFail(run_session, run_id, times=2)
                radarr = FakeRadarr()
                plex = _PlexThatLosesTheDatabase(lock, streams=[_stream(rating_key=701)])
                report = await Executor(
                    run_session,
                    safety=_armed(),
                    settings=ProfileSettings(),
                    dry_run=False,
                    gateway=_gateway(radarr={1: radarr}, plex=plex),
                    armed_recheck=_armed_forever,
                    exclusion_poll_delay=0.0,
                    plex_settle_delay=0.0,
                ).execute(run_id)

            assert lock.fired == 2
            assert report.state is RunState.ABORTED
            assert radarr.delete_calls == []  # nothing was ever sent
            assert report.aborted_reason is not None
            assert report.aborted_reason.id == "error.reap.journal_halt"

            async with async_factory() as fresh:
                stored = await fresh.get(ReapRun, run_id)
                assert stored is not None and stored.state is RunState.ABORTED
        finally:
            await engine.dispose()

    async def test_the_terminal_write_lands_on_a_transaction_that_already_failed(
        self, tmp_path: Path
    ) -> None:
        """The failure shape in one place, with no run around it. Kill the transaction,
        then ask the executor to record the terminal state.

        Driven through ``_commit_and_finalize`` directly because the tests above cannot
        say which write recovered, and this is the write with no second chance. Nothing
        reconciles a run left EXECUTING. A bare ``await self._session.commit()`` under
        ``except Exception: log.warning`` would raise ``PendingRollbackError`` here, and
        the warning would swallow it.
        """
        engine, async_factory = await _fresh_engine(tmp_path)
        try:
            async with async_factory() as setup:
                snapshot_id = await _snapshot_one(setup, media_key="radarr:1:1", rating_key=701)
                run = await _plan(setup, snapshot_id)
                run_id = run.id
                run.state = RunState.EXECUTING
                await setup.commit()

            async with async_factory() as run_session:
                executor = Executor(
                    run_session,
                    safety=_armed(),
                    settings=ProfileSettings(),
                    dry_run=False,
                    gateway=_gateway(radarr={1: FakeRadarr()}),
                    armed_recheck=_armed_forever,
                )
                # Break the transaction exactly as a failed step commit does.
                run_session.add(
                    ActionStep(
                        run_id=run_id,
                        media_key="poison",
                        ordinal=99,
                        kind="radarr_delete",
                        method="DELETE",
                        path=None,  # NOT NULL
                        idempotency_key="poison",
                        created_at=utcnow(),
                    )
                )
                with pytest.raises(IntegrityError):
                    await run_session.commit()
                # This is the state the wedge is made of. Nothing can be committed on
                # this session any more, however long ago the original fault cleared.
                with pytest.raises(PendingRollbackError):
                    await run_session.commit()

                await executor._commit_and_finalize(
                    run_id, _Terminal(RunState.ABORTED, "the run stopped early", utcnow())
                )

            async with async_factory() as fresh:
                stored = await fresh.get(ReapRun, run_id)
                assert stored is not None
                assert stored.state is RunState.ABORTED
                assert stored.aborted_reason == "the run stopped early"
                assert stored.finished_at is not None
        finally:
            await engine.dispose()

    async def test_a_revive_failure_is_not_reported_as_a_rollback_failure(
        self, tmp_path: Path
    ) -> None:
        """Each call in the recovery pair must name itself in the log.

        ``rollback()`` is what makes the session usable again. ``_revive()`` only
        repopulates the rows that read afterwards. If they shared one ``except``, a
        ``_revive`` fault would log as ``reap.journal_rollback_failed``, naming the call
        that had just returned successfully, on the path whose log is what gets read
        after a run wedges.

        This is driven at ``_commit_journal`` directly. Reaching this pair through a real
        run needs a fault that breaks a SELECT while leaving a rollback working, and none
        has been shown (WAL readers do not block on a writer). The handler is live
        either way, and an unreachable branch with no test is one refactor from silently
        gone.
        """
        engine, async_factory = await _fresh_engine(tmp_path)
        try:
            async with async_factory() as session:
                executor = Executor(
                    session, safety=_armed(), settings=ProfileSettings(), dry_run=False
                )

                rolled_back = False
                real_rollback = session.rollback

                async def rollback_that_works() -> None:
                    nonlocal rolled_back
                    rolled_back = True
                    await real_rollback()

                async def commit_that_fails() -> None:
                    raise RuntimeError("the write lock is held")

                async def revive_that_fails() -> None:
                    raise RuntimeError("the select could not run")

                session.commit = commit_that_fails  # type: ignore[method-assign]
                session.rollback = rollback_that_works  # type: ignore[method-assign]
                executor._revive = revive_that_fails  # type: ignore[method-assign]

                with capture_logs() as logs:
                    wrote = await executor._commit_journal(what="a step", write=[])

                assert wrote is False
                assert rolled_back, "the rollback must have run and returned for this to be it"
                events = [entry["event"] for entry in logs]
                assert "reap.journal_revive_failed" in events
                assert "reap.journal_rollback_failed" not in events, (
                    "the rollback worked, so blaming it points the reader at the wrong call"
                )
        finally:
            await engine.dispose()


class TestTwoMarksInARowBothReachTheDisk:
    """Each ``_mark_*`` is its own commit, so the row it wrote must be on disk before the
    next one runs, not merely in the identity map.

    The two halves of a mark disagree about when they happen. The Core ``UPDATE`` runs
    when it is executed. The ``setattr`` that mirrors it onto the ORM row leaves that row
    DIRTY, and the session is built ``autoflush=False``, so nothing flushes it until some
    later ``commit()`` does. The next mark's ``commit()`` is that later one, and its
    flush would run after its own Core ``UPDATE``, overwriting the column just written
    with the previous mark's values, inside the same transaction.

    This is driven through ``_mark_sent`` and ``_mark_verified`` directly. The corruption
    lasts exactly one commit, and every public path takes a third commit that repairs it
    before a test could look. The read-back goes through an engine of this test's own,
    because the run's session answers from memory, where the row is always right.
    """

    async def test_the_second_mark_does_not_write_the_first_ones_values_back(
        self, tmp_path: Path
    ) -> None:
        engine, async_factory = await _fresh_engine(tmp_path)
        try:
            async with async_factory() as setup:
                snapshot_id = await _snapshot_one(setup, media_key="radarr:1:1", rating_key=701)
                run = await _plan(setup, snapshot_id)
                run_id = run.id
                await setup.commit()

            async with async_factory() as run_session:
                executor = Executor(
                    run_session,
                    safety=_armed(),
                    settings=ProfileSettings(),
                    dry_run=False,
                    gateway=_gateway(radarr={1: FakeRadarr()}),
                    armed_recheck=_armed_forever,
                )
                step = (await _steps(run_session, run_id))[0]
                step_id = step.id
                # The season path's real sequence, sent, then verified, with no commit of
                # anyone else's in between to paper over the ordering.
                await executor._mark_sent(step)
                await executor._mark_verified(step, {"gone": True})

            async with async_factory() as fresh:
                stored = await fresh.get(ActionStep, step_id)
                assert stored is not None
                # Not SENT with a verified_at beside it, which is what the flush wrote back.
                assert stored.state is StepState.VERIFIED
                assert stored.verified_at is not None
                assert stored.verification_json == '{"gone": true}'
        finally:
            await engine.dispose()


#: A moment nothing else on this path produces, so a column that fails to make the round trip
#: below reads as a wrong value rather than as a coincidence.
_SENTINEL_INSTANT = datetime(2021, 3, 4, 5, 6, 7, tzinfo=UTC)

#: Every ``ActionStep`` column the executor writes while a run is in flight. The journal
#: writes ``_JournalRow`` captures ahead of a commit that may fail, and replays after the
#: rollback that recovering from it needs. Each carries a distinct value, so the replay can be
#: driven for real rather than compared by name.
_REPLAYED_STEP_COLUMNS: dict[str, Any] = {
    "state": StepState.FAILED,
    "error": "sentinel: the error a recovered write has to carry",
    "sent_at": _SENTINEL_INSTANT,
    "verified_at": _SENTINEL_INSTANT + timedelta(seconds=1),
    "verification_json": '{"sentinel": "the verification a recovered write has to carry"}',
    "file_removed_at": _SENTINEL_INSTANT + timedelta(seconds=2),
}

#: And the ones the planner writes once, before the run starts. They are durable long before
#: anything is sent, so a rollback on the delete path cannot discard them and the replay has
#: nothing to carry.
_WRITE_ONCE_STEP_COLUMNS = frozenset(
    {
        "id",
        "run_id",
        "media_key",
        "ordinal",
        "kind",
        "method",
        "path",
        "body_json",
        "idempotency_key",
        "created_at",
    }
)

#: ``ReapRun``'s half of the same split. The columns ``_Terminal`` carries and
#: ``_commit_and_finalize`` writes as plain values.
_TERMINAL_RUN_COLUMNS = frozenset({"state", "aborted_reason", "finished_at"})

#: Everything else on the run row is durable before the first file is touched.
#: ``started_at`` belongs here rather than above, since it is committed with the
#: EXECUTING claim, which sits ahead of the guarded block precisely so nothing with a
#: file at stake rides on it.
_BEFORE_ANY_DELETE_RUN_COLUMNS = frozenset(
    {
        "id",
        "snapshot_id",
        "policy_hash",
        "approved_manifest_hash",
        "approved_by",
        "approved_at",
        "started_at",
        "held_back_unknown_size",
    }
)


class TestARecoveredWriteCarriesEveryColumn:
    """``_JournalRow`` and ``_Terminal`` each mirror a model's columns by hand, and the
    replay and the terminal ``UPDATE`` restate that list a second and third time.

    This list is right today, with nothing keeping it right on its own. Drift costs
    something specific and silent. Add a mutable column to ``ActionStep``, write it from
    ``_fail`` or a ``_stage_*`` helper, and it lands on the ordinary path. On the
    recovery path, the rollback discards it and the replay does not carry it, so it
    comes back NULL. docs/LEARNINGS.md records the ``file_removed_at`` clobber this
    caused, invisible to a suite that never wrote the column in the first place.

    So every column of both models is classified here, and the replay is then driven for
    real. It is captured, the transaction is lost, it is replayed, and it is read back
    through a session the write never touched. A new column fails the classification
    until someone decides which side it is on. A column classified as replayed but
    missing from the dataclass, from ``of``, or from ``replay`` fails the round trip.
    """

    def test_every_action_step_column_is_classified_as_replayed_or_write_once(self) -> None:
        """A column added to ``ActionStep`` is on one side or the other, and someone has
        to say which. A mutable one that nobody classifies is one the replay silently
        drops."""
        assert {c.key for c in sa_inspect(ActionStep).column_attrs} == (
            set(_REPLAYED_STEP_COLUMNS) | _WRITE_ONCE_STEP_COLUMNS
        )

    def test_every_reap_run_column_is_classified_as_terminal_or_already_durable(self) -> None:
        """The sibling of the test above. ``_Terminal`` is the same hand-written mirror
        over the same hazard, one class above ``_JournalRow`` in the same file."""
        assert {c.key for c in sa_inspect(ReapRun).column_attrs} == (
            _TERMINAL_RUN_COLUMNS | _BEFORE_ANY_DELETE_RUN_COLUMNS
        )

    def test_the_journal_row_carries_every_replayed_column(self) -> None:
        """``id`` is the ``WHERE``, not a value the replay restores."""
        assert {f.name for f in dataclasses.fields(_JournalRow)} - {"id"} == set(
            _REPLAYED_STEP_COLUMNS
        )

    def test_the_terminal_row_carries_every_terminal_column(self) -> None:
        assert {f.name for f in dataclasses.fields(_Terminal)} == _TERMINAL_RUN_COLUMNS

    async def test_a_rolled_back_journal_write_is_replayed_whole(self, tmp_path: Path) -> None:
        """The round trip recovery actually makes. Journal the step, capture it, lose the
        transaction, replay. Every classified column has to arrive.

        Read back through an engine of its own. The writing session answers a ``get`` out
        of its identity map (``expire_on_commit=False``), and would report a discarded
        write as a durable one, which would make this test unable to fail for the reason
        it names.
        """
        engine, async_factory = await _fresh_engine(tmp_path)
        try:
            async with async_factory() as setup:
                snapshot_id = await _snapshot_many(setup, [("radarr:1:1", 1 * GB, 701)])
                run = await _plan(setup, snapshot_id)
                run_id = run.id
                await setup.commit()

            async with async_factory() as writer:
                step = (await _steps(writer, run_id))[0]
                step_id = step.id
                for column, sentinel in _REPLAYED_STEP_COLUMNS.items():
                    setattr(step, column, sentinel)
                captured = _JournalRow.of(step)
                # This is what recovery starts from. The commit failed, so the transaction
                # is dead, every write above is discarded, and every attribute on
                # ``step`` is expired.
                await writer.rollback()
                await writer.execute(captured.replay())
                await writer.commit()

            async with async_factory() as fresh:
                stored = await fresh.get(ActionStep, step_id)
                assert stored is not None
                assert {
                    column: getattr(stored, column) for column in _REPLAYED_STEP_COLUMNS
                } == _REPLAYED_STEP_COLUMNS
        finally:
            await engine.dispose()
