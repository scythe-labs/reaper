# SPDX-License-Identifier: AGPL-3.0-or-later
"""Carrying out a plan. The most safety-critical module in Reaper.

Everything upstream of here is reversible: a snapshot can be re-scanned, a policy
re-edited, a plan re-built. This module is where a plan becomes a deleted file, and a
deleted file is not coming back. So it is built out of interlocks, and every one of them
resolves toward *not* deleting.

Two independent layers guard every mutation, on purpose:

* **This executor's ``dry_run``.** When set (and it is the default), the executor walks
  the entire plan -- the manifest re-check, the caps, the canary ordering, the spare
  check -- and records what it *would* send, but sends nothing and touches no live service.
  The two *live* per-item interlocks (the streaming veto and the played-since-approval
  check) query Plex and Tautulli, so they run only in a real send, not in a dry run; the
  dry run proves the plan's shape and the reversible checks, not those two live reads.

* **The transport guard, underneath.** ``GuardedTransport`` (and its ``GuardedSession``
  twin for Plex) refuses any mutating call unless ``REAPER_DESTRUCTIVE_ACTIONS_ENABLED``
  is set on the host AND the executor declared the intent. These two layers are not the
  same check written twice: ``dry_run`` is the executor's decision, the guard is a
  property of the host that no browser can reach. A bug that made ``dry_run`` default to
  False would still hit the guard; a bug that bypassed the guard would still be stopped
  by ``dry_run``. Neither alone is trusted.

The interlocks, in order, and why each exists:

1. **Manifest re-check -- a frozen-snapshot integrity check.** The condemned candidate
   rows are frozen per snapshot and never mutated, and this re-hashes those same rows, so
   what it actually detects is *loss or tampering of the frozen set* (e.g. a candidate row
   deleted out from under the run by retention GC), not live library drift -- the executor
   does not re-read the *arr here, so a movie deleted or resized in Radarr after approval
   would not change this hash. Live drift is caught elsewhere, by the per-item interlocks
   below (the streaming veto, the played-since-approval check, and the per-item
   existence/size re-reads at delete time); a stale tab replaying yesterday's plan is
   stopped by the route's confirmation-phrase recompute and the "executes once" guard.
2. **Manual spare re-check, per item.** The owner may spare an item by hand *after* the
   plan is built -- during the grace window this executor exists for. A spare does not
   change the frozen candidate row (still ``condemn``) or the manifest hash, so this is a
   distinct check, run for every item in dry-run and for real alike, and it wins.
3. **Caps ABORT, never truncate.** A run over its item or byte cap stops entirely.
   Truncating would make *what* gets deleted depend on sort order -- a subtle,
   order-dependent bug in the one place it must not exist.
4. **The canary.** Ordinal 0, the single smallest item, executes and verifies alone.
   Only if the world changed exactly as predicted does the rest proceed. A broken path
   mapping costs one worthless file, not the whole run.
5. **Active-stream veto, re-polled before every delete.** Not once at the start: a run
   takes minutes and someone can start watching mid-run. Fail-closed -- if Plex cannot be
   read, the item is spared.
6. **Watched-since-approval.** If anyone played the item after it was approved, it is
   spared -- the grace period existed precisely so this could still happen. Fail-closed on
   any Tautulli error or any history row we cannot precisely timestamp.
7. **Verify the world changed.** A movie: re-read the exclusion list and assert the tmdbId
   is present *and* the movie is gone -- Sonarr and Radarr each accept the *other's*
   exclusion parameter and return 200 while doing nothing, so the 200 is re-read, not
   trusted. A season: verify the unmonitor took *before* deleting any file, then verify no
   file for the season remains after.

What is deliberately NOT here yet, and why:

* **The trash interlock (``emptyTrash``).** The *arr delete is the reclamation -- the file
  is off disk once Radarr/Sonarr removes it. Purging Plex's trash is cosmetic cleanup, and
  it is the single call that turns an unmounted-library mistake into a lost library, so it
  belongs behind a per-section count-delta check that needs the path-mapping table Reaper
  does not have yet. Until then the executor does a best-effort per-item Plex *refresh*
  (safe: a wrong path silently rescans nothing) and leaves the trash for Plex's own
  scheduled maintenance. ``PlexClient.empty_trash`` exists, guarded, for when that lands.
* **Mid-run disarm.** The host arm state is read at the start of a run (and the transport
  guard enforces it on every call). Flipping deletion off *during* a multi-item run does
  not halt the items already in flight -- runs are kept small by the caps and the first-run
  ratchet, and the per-item veto still catches a new viewer, but a true mid-run kill switch
  is a follow-up.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clients.base import IntegrationError, SafetyViolationError
from reaper.clients.plex import PlexError, declared_mutation
from reaper.clock import utcnow
from reaper.config import RuntimeSafety
from reaper.db.models import (
    ActionStep,
    Candidate,
    ReapRun,
    RunState,
    StepState,
)
from reaper.engine.policy import ProfileSettings
from reaper.services import whitelist
from reaper.services.planner import MediaRef, manifest_hash

if TYPE_CHECKING:
    from reaper.clients.plex import ActiveStream

log = structlog.get_logger(__name__)

#: The irreversible step of an item's plan -- the one that removes files. A movie has a
#: ``radarr_delete``; a season's file delete is ``sonarr_delete_files``, reached only
#: after its reversible unmonitor and the verification of it. Everything else in a
#: season plan is a read or a reversible edit.
_TERMINAL_DELETE_KINDS = frozenset({"radarr_delete", "sonarr_delete_files"})

#: The two live interlocks every real send passes before it deletes -- shared labels so the
#: movie and season checklists read the same. Reaching a send means both of these are True.
_CHECK_NOT_WATCHING = "Nobody was watching it right now"
_CHECK_NOT_PLAYED_SINCE = "Not played since you approved it"


# ---------------------------------------------------------------------------
# The clients the executor drives, as narrow Protocols.
# ---------------------------------------------------------------------------
#
# The executor is given exactly the operations it needs, not whole client objects. Two
# reasons: the real-send path can be tested against fakes without a live server (the same
# way ``leaving_soon`` tests against a ``LabelTarget`` fake), and the surface a delete can
# reach is written down here in one place rather than being "any method on the client".


class MovieDeleter(Protocol):
    """Radarr, for a movie delete + its exclusion verification."""

    async def movie_by_id(self, movie_id: int) -> dict[str, Any]: ...
    async def delete_movie(
        self, movie_id: int, *, delete_files: bool = ..., add_exclusion: bool = ...
    ) -> None: ...
    async def exclusions(self) -> list[dict[str, Any]]: ...
    async def root_folders(self) -> list[dict[str, Any]]: ...


class SeasonPruner(Protocol):
    """Sonarr, for the unmonitor -> verify -> delete-files sequence of a season prune."""

    async def series_by_id(self, series_id: int) -> dict[str, Any]: ...
    async def unmonitor_season(self, series_id: int, season_number: int) -> None: ...
    async def episode_files(self, series_id: int) -> list[dict[str, Any]]: ...
    async def delete_episode_files(self, episode_file_ids: list[int]) -> None: ...
    async def root_folders(self) -> list[dict[str, Any]]: ...


class PlexOps(Protocol):
    """The Plex reads and (reversible) writes the reap loop needs.

    ``active_streams`` is the pre-delete veto; it raises rather than returning ``[]`` when
    it cannot see sessions, so the executor can fail closed. The rest is the post-delete
    cleanup: ``refresh_path`` rescans one directory so Plex notices the file is gone,
    ``is_refreshing`` lets the executor wait for that scan to settle, and ``empty_trash``
    purges the now-missing item so no stale entry lingers.
    """

    async def active_streams(self) -> list[ActiveStream]: ...
    async def section_paths(self) -> dict[str, list[str]]: ...
    async def refresh_path(self, section_title: str, path: str) -> None: ...
    async def is_refreshing(self, section_title: str) -> bool: ...
    async def empty_trash(self, section_title: str) -> None: ...


class HistorySource(Protocol):
    """Tautulli, for the 'played since approval' check."""

    async def history(
        self,
        *,
        rating_key: int | None = ...,
        parent_rating_key: int | None = ...,
        after: str | None = ...,
    ) -> dict[str, Any]: ...


@dataclass
class ReapGateway:
    """The live clients a real run drives, keyed by the instance each item belongs to.

    A separate 4K Radarr alongside the HD one is a common setup, so an item is routed to
    *its* instance by ``media_key`` -- never to "the first Radarr", which would issue a
    delete against the wrong server. Plex and Tautulli are single (there is one media
    server), and both are required for a real run: without them the streaming veto and the
    watched-since-approval check cannot run, and a delete without those is exactly the
    thing the grace period exists to prevent.
    """

    radarr: Mapping[int, MovieDeleter] = field(default_factory=dict)
    sonarr: Mapping[int, SeasonPruner] = field(default_factory=dict)
    plex: PlexOps | None = None
    tautulli: HistorySource | None = None

    def radarr_for(self, instance_id: int) -> MovieDeleter:
        client = self.radarr.get(instance_id)
        if client is None:
            raise ExecutionError(
                f"No Radarr instance {instance_id} is configured, but the plan targets it. "
                "Refusing to guess which server to delete from."
            )
        return client

    def sonarr_for(self, instance_id: int) -> SeasonPruner:
        client = self.sonarr.get(instance_id)
        if client is None:
            raise ExecutionError(
                f"No Sonarr instance {instance_id} is configured, but the plan targets it. "
                "Refusing to guess which server to delete from."
            )
        return client


class ExecutionError(RuntimeError):
    """The run could not proceed. Raised for the conditions that void a whole run
    (a changed manifest, a cap breach, a failed canary) -- never for a single item,
    which is skipped rather than aborting the run."""


@dataclass(frozen=True)
class StepCheck:
    """One line in an item's after-action checklist: a thing the reap did or verified, and
    whether it passed. Rendered like the why-panel's ticks -- ``✓`` when ``ok``, ``✗`` when
    not -- so the owner sees every step and exactly which one failed."""

    label: str
    ok: bool


@dataclass
class StepOutcome:
    media_key: str
    kind: str
    state: StepState
    detail: str
    title: str = ""
    checks: list[StepCheck] = field(default_factory=list)


@dataclass
class RunReport:
    """What a run did, or would do. The audit record and the UI's after-action view."""

    run_id: int
    dry_run: bool
    state: RunState
    outcomes: list[StepOutcome] = field(default_factory=list)
    deleted_items: int = 0
    deleted_bytes: int = 0
    skipped: int = 0
    aborted_reason: str | None = None

    def record(self, outcome: StepOutcome) -> None:
        self.outcomes.append(outcome)


@dataclass(frozen=True)
class _Delete:
    """One item's steps, resolved against its candidate.

    A movie is one step (``radarr_delete``). A season is three, in a load-bearing order
    (unmonitor -> verify -> delete files), and they travel together: the item is the unit
    of the canary, the caps and the interlocks, not the individual HTTP call.
    """

    steps: tuple[ActionStep, ...]
    candidate: Candidate

    @property
    def terminal(self) -> ActionStep:
        """The irreversible step -- the actual file delete -- which the planner always
        emits last, after any reversible unmonitor/verify."""
        return self.steps[-1]


async def _load(session: AsyncSession, run_id: int) -> tuple[ReapRun, list[ActionStep]]:
    run = await session.get(ReapRun, run_id)
    if run is None:
        raise ExecutionError(f"No run {run_id}.")
    steps = list(
        (
            await session.execute(
                select(ActionStep)
                .where(ActionStep.run_id == run_id)
                .order_by(ActionStep.ordinal, ActionStep.id)
            )
        )
        .scalars()
        .all()
    )
    return run, steps


async def _condemned(session: AsyncSession, snapshot_id: int) -> dict[str, Candidate]:
    rows = (
        (
            await session.execute(
                select(Candidate).where(
                    Candidate.snapshot_id == snapshot_id, Candidate.verdict == "condemn"
                )
            )
        )
        .scalars()
        .all()
    )
    return {c.media_key: c for c in rows}


def _check_caps(
    deletes: Sequence[_Delete], settings: ProfileSettings, decisions: dict[str, str]
) -> None:
    """A run over cap ABORTS before it starts. It never runs the part that fits.

    Only the per-run caps are enforced here; the rolling 30-day caps belong to the
    scheduler, which knows what other runs have happened. Enforcing a partial here would
    be worse than not enforcing it, because it would delete *some* items and call the run
    a success.

    The cap is measured over only the items that will *actually* be deleted -- an item the
    owner spared by hand after the plan was built still carries delete steps (the frozen
    candidate row still reads ``condemn``) but is skipped per item in ``_one_delete``, so
    counting it here would abort a legitimate reduced run and quote a count that no longer
    matches the confirmation phrase the owner approved (which also excludes spares, see
    runs.py ``_planned_candidates``). This mirrors that surface so enforcement and approval
    agree. It still fails safe: a spare only ever removes items from the count.
    """
    deletable = [
        d
        for d in deletes
        if whitelist.effective_override(d.candidate.media_key, decisions) != "spare"
    ]
    items = len(deletable)
    total_bytes = sum(int(d.candidate.size_bytes) for d in deletable)

    if items > settings.max_items_per_run:
        raise ExecutionError(
            f"This plan would delete {items} items, over the per-run cap of "
            f"{settings.max_items_per_run}. The run is aborted, not truncated: which "
            "items got deleted must never depend on sort order. Raise the cap or "
            "reduce the plan."
        )
    if total_bytes > settings.max_bytes_per_run:
        raise ExecutionError(
            f"This plan would delete {total_bytes / 1024**3:.0f} GB, over the per-run cap "
            f"of {settings.max_bytes_per_run / 1024**3:.0f} GB. Aborted, not truncated."
        )


class Executor:
    """Runs one plan, once.

    The ``gateway`` of live clients is injected so the real-send path can be exercised end
    to end against fakes without a live server -- but the transport guard is always real,
    so a mutation attempted while unarmed is refused whether the client is real or a fake.

    A **dry run needs no gateway**: it proves the plan and sends nothing, so it never
    touches a live service. A **real run requires one**, with Plex and Tautulli present --
    the streaming veto and the played-since-approval check are not optional before an
    irreversible delete.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        safety: RuntimeSafety,
        settings: ProfileSettings,
        dry_run: bool = True,
        gateway: ReapGateway | None = None,
        exclusion_poll_attempts: int = 5,
        exclusion_poll_delay: float = 1.0,
        plex_settle_attempts: int = 10,
        plex_settle_delay: float = 2.0,
    ) -> None:
        self._session = session
        self._safety = safety
        self._settings = settings
        # dry_run defaults True. Deleting requires opting *in*, at the call site and
        # again at the host via the guard. Nothing deletes by omission.
        self._dry_run = dry_run
        self._gateway = gateway
        # Radarr adds the import exclusion a beat *after* the delete returns 200, so the
        # verification re-reads the exclusion list a few times before concluding it did not
        # land. Tests pass a zero delay to stay fast.
        self._exclusion_poll_attempts = max(1, exclusion_poll_attempts)
        self._exclusion_poll_delay = exclusion_poll_delay
        # Plex scans are asynchronous, so the trash purge waits for the refresh to settle.
        self._plex_settle_attempts = max(1, plex_settle_attempts)
        self._plex_settle_delay = plex_settle_delay
        # Plex movie sections whose path we refreshed this run -- the ones to purge trash
        # from at the end, once, if the mount is confirmed up.
        self._affected_sections: set[str] = set()
        # The manual spare/reap overrides, loaded fresh at the top of execute(). An item
        # spared by hand AFTER the plan was built must not be deleted -- the planner filters
        # spares at plan time, but the owner can spare during the grace window too, so the
        # executor re-checks per item. Loaded per run, not per item, so one query serves all.
        self._decisions: dict[str, str] = {}

    async def execute(self, run_id: int) -> RunReport:
        run, steps = await _load(self._session, run_id)
        report = RunReport(run_id=run_id, dry_run=self._dry_run, state=run.state)

        # Only a PLANNED run may be executed. A run left in EXECUTING is one whose process
        # died mid-flight; it is deliberately NOT resumable here. Re-entering it cannot
        # converge -- an already-deleted movie 404s on its up-front read and would abort as
        # the canary -- and there is no reconciler consuming the SENT journal or the
        # per-step idempotency keys to de-duplicate a resumed step. So a partially-completed
        # run is re-planned from a fresh scan, not re-executed; refusing here makes that
        # explicit rather than letting a re-trigger fail confusingly at the first item.
        if run.state != RunState.PLANNED:
            raise ExecutionError(
                f"Run {run_id} is {run.state.value}, not runnable. A run executes once."
            )

        # A real (non-dry) run requires the host ceiling to be up. This is the
        # executor's own check; the transport enforces it again independently.
        if not self._dry_run and not self._safety.destructive_allowed:
            raise ExecutionError(f"Refusing to execute for real: {self._safety.why_blocked()}")

        # ...and it requires the clients to drive the delete AND to run the two live
        # pre-delete interlocks. No Plex means no streaming veto; no Tautulli means no
        # played-since-approval check. Refusing here is the safe failure -- the alternative
        # is deleting without being able to see who is watching.
        if not self._dry_run:
            if self._gateway is None:
                raise ExecutionError(
                    "Refusing a real run with no clients configured: there is nothing to "
                    "issue the delete through, and no way to check who is watching."
                )
            if self._gateway.plex is None:
                raise ExecutionError(
                    "Refusing a real run without Plex: the active-stream veto -- re-polled "
                    "before every delete -- cannot run, and deleting blind to who is "
                    "watching is exactly what must never happen."
                )
            if self._gateway.tautulli is None:
                raise ExecutionError(
                    "Refusing a real run without Tautulli: the played-since-approval check "
                    "cannot run, and the grace period exists precisely so a late view can "
                    "still spare an item."
                )

        condemned = await _condemned(self._session, run.snapshot_id)

        # The owner's manual overrides, read fresh: a spare added since the plan was built
        # must still stop the delete, and this is the layer that enforces it at the point of
        # no return (see the per-item check in ``_one_delete``).
        self._decisions = await whitelist.overrides(self._session)
        self._affected_sections = set()

        # -- interlock 1: the frozen condemned set must still be intact ----
        # Hashed over the WHOLE condemned set -- spared or not -- so that sparing an item
        # after approval does not change the fingerprint and void the run. The planner hashes
        # the same whole set for exactly this reason; a spare is not a change to *what was
        # condemned*, it is a decision to keep one of them, which the per-item check honours.
        # Both sides hash the identical frozen candidate rows for this immutable snapshot, so
        # this is a snapshot-integrity check: it fires if a condemned candidate row is lost or
        # tampered with under the run (e.g. retention GC), NOT if the live library moved --
        # nothing re-reads the *arr here. Real live drift (a movie deleted or resized in
        # Radarr since approval) is caught by the per-item interlocks and the existence/size
        # re-reads at delete time, and a stale tab is stopped by the route's confirmation
        # recompute and the "executes once" guard above.
        current_hash = manifest_hash(sorted(condemned.values(), key=lambda c: c.media_key))
        if current_hash != run.approved_manifest_hash:
            raise ExecutionError(
                "The condemned set changed since this plan was approved -- an item was "
                "added, removed, or resized. The approval was for a different plan and "
                "is void. Re-scan, re-review, and approve the new plan."
            )

        # Group every step by the item it belongs to, keeping only condemned items that
        # actually carry an irreversible delete step. An item is one delete unit -- a
        # movie's single call, or a season's unmonitor -> verify -> delete triple -- and
        # the canary, caps and interlocks all act on the item, never on a lone step.
        # Ordered by the item's ordinal so the canary (the smallest condemned item,
        # ordinal 0) is first.
        deletable_keys = {s.media_key for s in steps if s.kind in _TERMINAL_DELETE_KINDS}
        by_item: dict[str, list[ActionStep]] = {}
        for s in steps:
            if s.media_key in condemned and s.media_key in deletable_keys:
                by_item.setdefault(s.media_key, []).append(s)
        deletes = sorted(
            (
                _Delete(
                    steps=tuple(sorted(item_steps, key=lambda st: (st.ordinal, st.id))),
                    candidate=condemned[key],
                )
                for key, item_steps in by_item.items()
            ),
            key=lambda d: (d.steps[0].ordinal, d.candidate.media_key),
        )

        # A dry run is a repeatable *simulation*: it must not consume the plan. Only a real
        # run transitions the run row (PLANNED -> EXECUTING -> COMPLETED/ABORTED); a dry run
        # leaves it PLANNED so it can still be dry-run again and, crucially, still executed
        # for real afterwards. The report always carries the outcome for display.
        if not self._dry_run:
            run.state = RunState.EXECUTING
            run.started_at = utcnow()
            await self._session.flush()

        try:
            # -- interlock 3: caps abort the whole run ----------------------
            # Inside the guarded block, so a breach becomes a visible ABORTED report the
            # owner can read -- the same shape as a canary failure -- rather than an
            # exception that escapes. Either way the run deletes nothing.
            _check_caps(deletes, self._settings, self._decisions)
            await self._run_deletes(deletes, report, run.approved_at)
            report.state = RunState.COMPLETED
            if not self._dry_run:
                run.state = RunState.COMPLETED
                run.finished_at = utcnow()
        except ExecutionError as exc:
            report.state = RunState.ABORTED
            report.aborted_reason = str(exc)
            if not self._dry_run:
                run.state = RunState.ABORTED
                run.aborted_reason = str(exc)

        # Purge stale Plex entries for whatever was actually removed -- on a COMPLETED or an
        # ABORTED run alike, because a canary can fail its exclusion check *after* its file is
        # already gone (that is the bug that left a stale entry). Post-processing, never fatal:
        # the files are gone; this only keeps Plex's view honest. Gated on a section actually
        # having been refreshed (i.e. a file really was removed).
        if not self._dry_run and self._affected_sections:
            await self._finalize_plex()

        if not self._dry_run:
            await self._session.flush()
        return report

    async def _run_deletes(
        self, deletes: Sequence[_Delete], report: RunReport, approved_at: datetime
    ) -> None:
        # The canary is the first item we actually *attempt to delete* -- not merely index
        # 0. If the smallest item is spared, vetoed, or watched-since (skipped, no file
        # touched), the next item becomes the first real delete and must earn the canary's
        # halt-on-failure protection. A "failed" delete can still mean the file was removed
        # (deleted, but the exclusion did not land), so plowing on after the first surprise
        # is exactly what the canary exists to prevent.
        real_attempt_made = False
        for index, delete in enumerate(deletes):
            outcome = await self._one_delete(delete, is_canary=index == 0, approved_at=approved_at)
            report.record(outcome)

            if outcome.state == StepState.SKIPPED:
                report.skipped += 1
                continue  # a skip touched no file, so it does not consume the canary

            # From here the item was really acted on (verified or failed).
            first_real_attempt = not real_attempt_made
            real_attempt_made = True

            if outcome.state == StepState.VERIFIED:
                report.deleted_items += 1
                report.deleted_bytes += int(delete.candidate.size_bytes)
            elif outcome.state == StepState.FAILED and first_real_attempt:
                # The canary -- the first real deletion -- misbehaved. Halt the whole run:
                # a plan whose first, smallest, safest delete does not behave as predicted
                # is a plan we do not understand.
                raise ExecutionError(
                    f"The canary ({delete.candidate.title!r}) did not complete as expected: "
                    f"{outcome.detail}. Halting before touching anything else."
                )
            # A later item failing is recorded and survivable: one stubborn item is not a
            # reason to abandon the rest, and the canary already proved the mechanism works.

    async def _one_delete(
        self, delete: _Delete, *, is_canary: bool, approved_at: datetime
    ) -> StepOutcome:
        """The interlocks that guard a single deletion, then the deletion itself.

        In dry-run no mutating call is sent, no live service is touched, and **no row is
        mutated** -- the outcome records the exact request each step would have made, so a
        season's whole unmonitor -> verify -> delete sequence is proven and inspectable
        without a file being touched *and without consuming the plan*, which stays PLANNED
        and PENDING so it can still be executed for real. The two live pre-delete interlocks
        below run **only for a real send**, and each spares the item on any uncertainty
        (fail-closed).
        """
        candidate = delete.candidate

        # A spare wins over everything, in dry-run and for real alike. The owner may spare an
        # item by hand after the plan was built -- during the grace window this executor
        # exists to honour -- and a frozen candidate row still reads ``condemn``, so this is
        # the check (not the verdict, not the manifest hash) that keeps a hand-spared file.
        if whitelist.effective_override(candidate.media_key, self._decisions) == "spare":
            return self._mark_skipped(
                delete,
                "you spared this by hand, so it is kept even though it was in the plan",
                check="You spared this by hand. Kept.",
            )

        if not self._dry_run:
            # An item Plex never matched has no rating key, so neither the streaming veto
            # nor the played-since check can even address it. That is an uncertainty, and
            # uncertainty spares -- we will not delete something we cannot prove is idle.
            if candidate.plex_rating_key is None:
                return self._mark_skipped(
                    delete,
                    "Plex has no rating key for this item, so Reaper cannot confirm nobody "
                    "is watching it. Spared.",
                    check="No Plex match, so we can't confirm it's idle. Kept.",
                )
            if await self._being_watched_now(candidate):
                return self._mark_skipped(
                    delete,
                    "someone is watching it right now",
                    check="Someone is watching it right now. Kept.",
                )
            if await self._watched_since_approval(candidate, approved_at):
                return self._mark_skipped(
                    delete,
                    "played since the plan was approved",
                    check="It was played since you approved the plan. Kept.",
                )

        if self._dry_run:
            # The heart of the dry run: prove everything, send nothing, mutate nothing. The
            # full sequence is shown in the detail, but the step rows are left PENDING so the
            # plan is still runnable for real afterwards.
            parts: list[str] = []
            for step in delete.steps:
                body = json.loads(step.body_json) if step.body_json else {}
                parts.append(f"would {step.method} {step.path} {body}".rstrip())
            return StepOutcome(
                media_key=delete.terminal.media_key,
                kind=delete.terminal.kind,
                state=StepState.SKIPPED,
                detail=" -> ".join(parts) + (" [canary]" if is_canary else ""),
                title=candidate.title,
            )

        # A real send. Each step is marked SENT (journalled) *before* its guarded call and
        # VERIFIED only after the world is re-read -- so a crash mid-item leaves a durable
        # audit record of exactly what was in flight, and a 200 is never mistaken for proof.
        # That record is not yet auto-reconciled: a crashed (EXECUTING) run is re-planned
        # from a fresh scan, not resumed -- execute() refuses anything but a PLANNED run.
        return await self._send_for_real(delete, is_canary=is_canary)

    # -- live pre-delete interlocks: read-only, fail-closed ----------------

    def _equivalent_keys(self, candidate: Candidate) -> list[int]:
        """Every Plex rating key this candidate's watch evidence lives under.

        Usually just ``plex_rating_key``. A merged bind (one file listed several times in
        Plex, ``matched_by = merged_listings``) recorded every listing's key in its
        explanation's match block, and the live interlocks must consult all of them: a
        play or an active stream through the file's other listing is a play of the very
        file this delete would remove. Read back from the stored explanation, so the keys
        the interlocks protect are exactly the keys the owner was shown. An explanation
        without the list (every non-merged bind, and every snapshot from before merging
        existed) falls back to the single key, which is the pre-merge behaviour.
        """
        keys: list[int] = []
        if candidate.plex_rating_key is not None:
            keys.append(candidate.plex_rating_key)
        try:
            match = json.loads(candidate.explanation_json).get("match") or {}
            merged = match.get("merged_rating_keys") or []
        except (ValueError, AttributeError):
            merged = []
        for value in merged:
            if isinstance(value, int) and value not in keys:
                keys.append(value)
        return keys

    async def _being_watched_now(self, candidate: Candidate) -> bool:
        """Is anyone watching this item -- or a child of it -- right now?

        Re-polled per item, never once at the start: a run takes minutes and someone can
        start playing mid-run. The veto set unions each stream's episode / season / show
        rating keys, so watching one episode protects the whole season a prune would take.

        Fail-closed: if Plex cannot be read, we cannot conclude nobody is watching, so the
        item is treated as watched and spared. ``active_streams`` raises rather than
        returning ``[]`` for exactly this reason.
        """
        gateway = self._gateway
        if gateway is None or gateway.plex is None:  # pragma: no cover - execute() guards this
            return True
        try:
            streams = await gateway.plex.active_streams()
        except Exception as exc:
            # Any failure to read who is watching -- not just the expected PlexError -- means
            # we cannot conclude nobody is, so we spare. Catching broadly here is deliberate:
            # the last check before an irreversible act must never let an unexpected error
            # become "nobody is watching".
            log.warning(
                "reap.stream_veto_unreadable", media_key=candidate.media_key, error=str(exc)
            )
            return True
        veto: set[int] = set()
        for stream in streams:
            veto |= stream.veto_keys
        # Every listing of a merged bind is checked: a stream through the file's second
        # listing is someone watching the very file this delete would remove.
        return any(key in veto for key in self._equivalent_keys(candidate))

    async def _watched_since_approval(self, candidate: Candidate, approved_at: datetime) -> bool:
        """Has anyone played this item since the plan was approved?

        The grace period exists precisely so a late view can still rescue an item, so this
        is checked immediately before the delete, not at approval time. ``after`` is a
        coarse date filter with a **one-day margin**: Tautulli applies it against its own
        (possibly non-UTC) day boundary, so querying from the day before approval keeps a
        play made just after a near-midnight approval from being filtered out before we can
        see it. The precise unix ``stopped``/``date`` on each returned row is then compared
        against the exact approval instant, so a play genuinely before approval does not
        count and one after it does.

        Fail-closed at every step: a Tautulli error, or a returned row we cannot read a
        timestamp from, both spare the item. A row present but unreadable survived the
        ``after`` filter, so it may well be a post-approval play -- we do not delete on the
        assumption that it was not.
        """
        gateway = self._gateway
        if gateway is None or gateway.tautulli is None:  # pragma: no cover - execute() guards
            return True
        # The rating-key skip in _one_delete precedes this, so this is belt-and-suspenders.
        if candidate.plex_rating_key is None:  # pragma: no cover
            return True

        # One-day margin (see docstring): guard against Tautulli's local-day boundary
        # dropping a real post-approval play near a UTC-midnight approval.
        after = (approved_at - timedelta(days=1)).strftime("%Y-%m-%d")
        approved_ts = int(approved_at.timestamp())
        # A merged bind is one file listed several times in Plex; a play recorded under
        # ANY of its listings is a play of the file, so every key is queried.
        for rating_key in self._equivalent_keys(candidate):
            try:
                # A season's episodes are children of its rating key; a movie is the key
                # itself. Querying the season by parent_rating_key catches an episode play
                # the movie-style rating_key query would miss.
                if candidate.media_type == "season":
                    data = await gateway.tautulli.history(parent_rating_key=rating_key, after=after)
                else:
                    data = await gateway.tautulli.history(rating_key=rating_key, after=after)
            except Exception as exc:
                # Broad on purpose: any failure to read history -- not just
                # IntegrationError -- means we cannot prove it was not watched, so we spare.
                log.warning(
                    "reap.watched_since_unreadable", media_key=candidate.media_key, error=str(exc)
                )
                return True

            rows = data.get("data") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                rows = []
            for row in rows:
                played_ts = _row_timestamp(row)
                # An unreadable timestamp (None) spares: the row is present and passed the
                # date filter, so we treat it as a possible late play rather than assume it
                # is old.
                if played_ts is None or played_ts >= approved_ts:
                    return True
        return False

    # -- the real send -----------------------------------------------------

    async def _send_for_real(self, delete: _Delete, *, is_canary: bool) -> StepOutcome:
        """Issue one item's deletion for real, then verify the world changed as intended.

        Reached only when dry_run is False, the host is armed, the gateway is present, and
        every live interlock passed. Dispatched by the item's own coordinates -- a movie is
        one guarded call plus an exclusion re-read; a season is the load-bearing
        unmonitor -> verify -> delete-files sequence.

        The execution is driven from the item's ``media_key`` through the *typed* client
        methods, **not** by replaying the journalled ``method``/``path`` strings. Replaying
        the raw path would bypass the version gate and, worse, re-open the exclusion-param
        footgun -- Sonarr and Radarr each accept the other's exclusion parameter and return
        200 while doing nothing. The journal stays the audit record; the typed method is
        what actually runs.
        """
        candidate = delete.candidate
        try:
            ref = MediaRef.parse(candidate.media_key)
        except Exception as exc:  # a key that parsed at plan time should still parse now
            return self._fail(delete, f"could not route {candidate.media_key!r}: {exc}")

        try:
            if ref.kind == "radarr":
                return await self._send_movie(delete, ref, is_canary=is_canary)
            if ref.kind == "sonarr" and ref.season is not None:
                return await self._send_season(delete, ref, is_canary=is_canary)
        except IntegrationError as exc:
            # A hard client/transport error on this item. Recorded and (unless it is the
            # canary) survivable -- the run continues with the others.
            return self._fail(delete, f"the *arr call failed: {exc}")
        except SafetyViolationError as exc:
            # The transport guard refused the mutation mid-send. In production the executor
            # and clients share one RuntimeSafety, so this cannot happen -- but if it ever
            # did, a clean failed item (the canary aborts the run) is far better than a
            # crash that leaves the run in an unknown state.
            return self._fail(delete, f"the transport guard blocked the delete: {exc}")
        except ExecutionError as exc:
            # A missing instance route. Same treatment: fail this item, not the world.
            return self._fail(delete, str(exc))

        return self._fail(
            delete, f"no live delete path for {candidate.media_key!r} ({candidate.media_type})"
        )

    async def _send_movie(self, delete: _Delete, ref: MediaRef, *, is_canary: bool) -> StepOutcome:
        """Delete a movie with an import exclusion, then PROVE the exclusion landed.

        The tmdbId is read *before* the delete -- it cannot be read after, the movie is
        gone -- and the exclusion list is re-read after, because Radarr returns 200 for the
        delete whether or not the exclusion took. A missing exclusion is a verification
        failure, not a success: re-requesting the title would silently re-download it.
        """
        radarr = self._gateway.radarr_for(ref.instance_id)  # type: ignore[union-attr]
        step = delete.terminal  # the sole radarr_delete step

        # Reaching here means the two live interlocks already passed for this item.
        checks = [StepCheck(_CHECK_NOT_WATCHING, True), StepCheck(_CHECK_NOT_PLAYED_SINCE, True)]

        # Read the tmdbId now, while the movie still exists, for the exclusion re-read.
        movie = await radarr.movie_by_id(ref.arr_id)
        tmdb_id = int(movie.get("tmdbId") or 0)

        await self._mark_sent(step)
        await radarr.delete_movie(ref.arr_id, delete_files=True, add_exclusion=True)

        # Verify: the movie is actually gone AND the exclusion is present. The gone check is
        # immediate; the exclusion is polled, because Radarr adds it a moment after the
        # delete returns 200 -- a single immediate read is a false negative.
        gone = await self._movie_is_gone(radarr, ref.arr_id)
        excluded = await self._exclusion_landed(radarr, tmdb_id)
        checks.append(StepCheck("Removed the file through Radarr", gone))
        checks.append(StepCheck("Import exclusion confirmed. It won't re-download", excluded))

        # Once the file is gone, tell Plex -- whatever the exclusion result. This is what
        # stops a stale entry lingering, and it must fire even when the exclusion check
        # failed (the file is still gone). Best-effort: never affects the item's verdict.
        if gone:
            await self._best_effort_refresh(str(movie.get("path") or movie.get("folderName") or ""))

        if not (excluded and gone):
            # The file is already gone once ``gone`` is True; a missing exclusion is the
            # remaining risk (a re-request could re-download), so say which failed and that
            # the file itself is removed either way.
            return self._fail(
                delete,
                f"delete not fully confirmed (gone={gone}, exclusion_verified={excluded}). "
                + (
                    "The file was removed, but the import exclusion could not be verified "
                    "after polling -- a re-request could re-download it."
                    if gone
                    else "Radarr returned 200 but the movie is still present."
                ),
                checks=checks,
            )

        await self._mark_verified(step, {"tmdb_id": tmdb_id, "excluded": True, "gone": True})
        return StepOutcome(
            media_key=step.media_key,
            kind=step.kind,
            state=StepState.VERIFIED,
            detail="deleted; import exclusion verified present"
            + (" [canary]" if is_canary else ""),
            title=delete.candidate.title,
            checks=checks,
        )

    async def _movie_is_gone(self, radarr: MovieDeleter, movie_id: int) -> bool:
        """A deleted movie 404s. Any other error is treated as 'not proven gone'."""
        try:
            await radarr.movie_by_id(movie_id)
        except IntegrationError as exc:
            return exc.status == 404
        return False

    async def _exclusion_landed(self, radarr: MovieDeleter, tmdb_id: int) -> bool:
        """Was the import exclusion for ``tmdb_id`` added? Polled, not read once.

        Radarr adds the exclusion just after the delete's 200, so an immediate single read
        misses it and reports a false negative -- which, on the canary, aborts a run whose
        delete actually succeeded. Re-read a few times with a short delay; return as soon as
        it appears, and give up only after the whole window. ``tmdb_id == 0`` (no id to match
        on) can never be verified, so it is False.
        """
        if tmdb_id == 0:
            return False
        for attempt in range(self._exclusion_poll_attempts):
            exclusions = await radarr.exclusions()
            if any(int(e.get("tmdbId") or 0) == tmdb_id for e in exclusions):
                return True
            if attempt < self._exclusion_poll_attempts - 1 and self._exclusion_poll_delay > 0:
                await asyncio.sleep(self._exclusion_poll_delay)
        return False

    async def _send_season(self, delete: _Delete, ref: MediaRef, *, is_canary: bool) -> StepOutcome:
        """Prune one season: unmonitor -> VERIFY the unmonitor -> delete files -> verify gone.

        The order is load-bearing and the verify between unmonitor and delete is not
        optional: 'files gone, still monitored' makes Sonarr re-download everything just
        removed, so the file delete never runs until the unmonitor is confirmed. The
        episode-file ids are resolved live here, immediately before the delete, from the
        current series -- never frozen at plan time.
        """
        assert ref.season is not None
        sonarr = self._gateway.sonarr_for(ref.instance_id)  # type: ignore[union-attr]
        by_kind = {s.kind: s for s in delete.steps}
        unmonitor = by_kind["sonarr_unmonitor"]
        verify = by_kind["sonarr_verify_unmonitor"]
        delete_step = by_kind["sonarr_delete_files"]

        # Reaching here means the two live interlocks already passed for this item.
        checks = [StepCheck(_CHECK_NOT_WATCHING, True), StepCheck(_CHECK_NOT_PLAYED_SINCE, True)]

        # 1. Unmonitor (reversible), then VERIFY it actually took before any file is touched.
        await self._mark_sent(unmonitor)
        await sonarr.unmonitor_season(ref.arr_id, ref.season)
        series = await sonarr.series_by_id(ref.arr_id)
        monitored_off = self._season_monitored(series, ref.season) is False
        checks.append(StepCheck(f"Unmonitored season {ref.season} in Sonarr", True))
        checks.append(StepCheck("Confirmed the season is no longer monitored", monitored_off))
        if not monitored_off:
            # Do NOT proceed to the delete: the dangerous half-state is files-gone-while-
            # -monitored. Mark the unmonitor sent, the verify failed, the delete un-run.
            await self._mark_verified(unmonitor, {"unmonitor_sent": True})
            self._mark_step_failed(
                verify, "the season is still monitored after the unmonitor; not deleting files"
            )
            self._mark_step_skipped(delete_step, "unmonitor unverified")
            await self._session.flush()
            checks.append(StepCheck("Deleted the season's episode files", False))
            return StepOutcome(
                media_key=delete_step.media_key,
                kind=delete_step.kind,
                state=StepState.FAILED,
                detail="unmonitor did not take; refused to delete files while still monitored",
                title=delete.candidate.title,
                checks=checks,
            )
        await self._mark_verified(unmonitor, {"unmonitor_sent": True})
        await self._mark_verified(verify, {"monitored": False})

        # 2. Resolve this season's episode files LIVE, then delete them.
        files = await sonarr.episode_files(ref.arr_id)
        file_ids = [
            int(f["id"])
            for f in files
            if f.get("id") is not None and int(f.get("seasonNumber", -1)) == ref.season
        ]
        await self._mark_sent(delete_step)
        await sonarr.delete_episode_files(file_ids)

        # 3. Verify no file for this season remains.
        remaining = await sonarr.episode_files(ref.arr_id)
        still_there = [f for f in remaining if int(f.get("seasonNumber", -1)) == ref.season]
        checks.append(
            StepCheck(f"Deleted the season's {len(file_ids)} episode file(s)", not still_there)
        )
        if still_there:
            return self._fail(
                delete,
                f"{len(still_there)} episode file(s) for season {ref.season} remain after "
                "the delete; not confirmed.",
                checks=checks,
            )

        await self._mark_verified(delete_step, {"deleted_file_ids": file_ids, "remaining": 0})
        return StepOutcome(
            media_key=delete_step.media_key,
            kind=delete_step.kind,
            state=StepState.VERIFIED,
            detail=f"season {ref.season} pruned: {len(file_ids)} file(s) deleted, unmonitor "
            "verified" + (" [canary]" if is_canary else ""),
            title=delete.candidate.title,
            checks=checks,
        )

    @staticmethod
    def _season_monitored(series: dict[str, Any], season_number: int) -> bool | None:
        """Is the given season monitored, per a freshly-read series? None if not found."""
        for season in series.get("seasons") or []:
            if int(season.get("seasonNumber", -1)) == season_number:
                monitored = season.get("monitored")
                return bool(monitored) if monitored is not None else None
        return None

    async def _best_effort_refresh(self, arr_path: str) -> None:
        """Nudge Plex to rescan the deleted item's directory. Never fatal.

        Fires whenever the file is gone, so Plex learns the item is missing. When a Plex
        section location is a prefix of the *arr path, the refresh is path-scoped -- it can
        only affect items *under that path*, never the whole library, which is what makes the
        end-of-run trash purge safe. The refreshed section is remembered for that purge. When
        the path cannot be mapped it does nothing and says so; the file is already gone and
        Plex will notice on its next scheduled scan regardless.
        """
        gateway = self._gateway
        if gateway is None or gateway.plex is None or not arr_path:
            return
        try:
            sections = await gateway.plex.section_paths()
            for title, locations in sections.items():
                for location in locations:
                    if arr_path.startswith(location):
                        with declared_mutation():
                            await gateway.plex.refresh_path(title, arr_path)
                        self._affected_sections.add(title)
                        return
            log.info("reap.refresh_unmapped", arr_path=arr_path)
        except PlexError as exc:
            log.warning("reap.refresh_failed", arr_path=arr_path, error=str(exc))

    async def _finalize_plex(self) -> None:
        """Purge the stale entries for the files we removed, so Plex's view stays honest.

        The single most dangerous call in the app (an unmounted library + a scan + an empty
        trash is how whole libraries vanish), so it is doubly interlocked:

        * **The mount must be up.** Every deletion routed through an *arr whose root folder
          reports ``accessible``; if any does not, the volume may be gone and the trash is
          full of items that are merely *unreachable*, not deleted -- so we refuse to purge.
        * **Only path-scoped scans ran.** ``_best_effort_refresh`` rescans one directory at
          a time, so the only items Plex could have freshly trashed are the ones under the
          paths we deleted. Reaper never triggers a whole-section scan.

        Either check failing skips the purge and logs it -- the reap already succeeded, and a
        lingering "unavailable" entry is a cosmetic problem, never a lost file. Never raises.
        """
        gateway = self._gateway
        if gateway is None or gateway.plex is None or not self._affected_sections:
            return

        if not await self._mount_is_up():
            log.warning("reap.trash_purge_skipped", reason="a root folder is not accessible")
            return

        for section in sorted(self._affected_sections):
            try:
                await self._wait_for_scan(gateway.plex, section)
                with declared_mutation():
                    await gateway.plex.empty_trash(section)
                log.info("reap.trash_purged", section=section)
            except PlexError as exc:
                log.warning("reap.trash_purge_failed", section=section, error=str(exc))

    async def _mount_is_up(self) -> bool:
        """Do all the *arr instances we deleted through report their root folders accessible?

        The proxy for "the volume is really mounted". If we cannot read an *arr, or any root
        folder is inaccessible, we assume the worst and return False -- the caller then skips
        the trash purge. Plex shares these paths, so an accessible *arr root means Plex's
        trashed items are genuinely deleted, not a transient unmount.
        """
        gateway = self._gateway
        if gateway is None:  # pragma: no cover - _finalize_plex guards this
            return False
        clients: list[MovieDeleter | SeasonPruner] = [
            *gateway.radarr.values(),
            *gateway.sonarr.values(),
        ]
        for client in clients:
            try:
                folders = await client.root_folders()
            except IntegrationError as exc:
                log.warning("reap.rootfolder_unreadable", error=str(exc))
                return False
            if not folders or not all(f.get("accessible") is True for f in folders):
                return False
        return True

    async def _wait_for_scan(self, plex: PlexOps, section: str) -> None:
        """Wait (bounded) for a section's scan to settle before emptying its trash.

        A refresh fires an asynchronous scan; emptying the trash before Plex has noticed the
        deleted file would purge nothing. Polls ``is_refreshing`` a few times; gives up after
        the window either way, since the purge is best-effort.
        """
        for attempt in range(self._plex_settle_attempts):
            if not await plex.is_refreshing(section):
                return
            if attempt < self._plex_settle_attempts - 1 and self._plex_settle_delay > 0:
                await asyncio.sleep(self._plex_settle_delay)

    # -- journal state transitions -----------------------------------------

    async def _mark_sent(self, step: ActionStep) -> None:
        step.state = StepState.SENT
        step.sent_at = utcnow()
        await self._session.flush()

    async def _mark_verified(self, step: ActionStep, verification: dict[str, Any]) -> None:
        step.state = StepState.VERIFIED
        step.verified_at = utcnow()
        step.verification_json = json.dumps(verification)
        await self._session.flush()

    def _mark_step_failed(self, step: ActionStep, reason: str) -> None:
        step.state = StepState.FAILED
        step.error = reason

    def _mark_step_skipped(self, step: ActionStep, reason: str) -> None:
        step.state = StepState.SKIPPED
        step.error = reason

    def _fail(
        self, delete: _Delete, reason: str, checks: list[StepCheck] | None = None
    ) -> StepOutcome:
        """Fail this item: mark any not-yet-terminal step FAILED, and record why.

        A step already VERIFIED (an unmonitor that took, say) keeps its state -- it really
        did happen. Only the steps that did not reach a terminal state are marked FAILED,
        so the journal reflects reality for a future reconciler. ``checks`` carries the
        after-action checklist (what got done, and which check failed); when absent, the
        reason itself is the single failed line.
        """
        for step in delete.steps:
            if step.state not in (StepState.VERIFIED, StepState.SKIPPED):
                step.state = StepState.FAILED
                step.error = reason
        return StepOutcome(
            media_key=delete.terminal.media_key,
            kind=delete.terminal.kind,
            state=StepState.FAILED,
            detail=reason,
            title=delete.candidate.title,
            checks=checks if checks is not None else [StepCheck(reason, False)],
        )

    def _mark_skipped(self, delete: _Delete, reason: str, check: str | None = None) -> StepOutcome:
        """Spare the whole item. In a REAL run, mark every one of its steps SKIPPED (not
        just the last): a season sparing that left its unmonitor step PENDING would read to
        a future reconciler as an interrupted run with work still to do. In a dry run, mutate
        nothing -- the simulation must leave the plan runnable -- and only report the skip.

        The checklist gets one ``✓`` line for the protection that fired: a spare is not a
        failure, it is a protection working, so it reads as a pass, not a cross.
        """
        if not self._dry_run:
            for step in delete.steps:
                step.state = StepState.SKIPPED
                step.error = reason
        return StepOutcome(
            media_key=delete.terminal.media_key,
            kind=delete.terminal.kind,
            state=StepState.SKIPPED,
            detail=reason,
            title=delete.candidate.title,
            checks=[StepCheck(check or reason, True)],
        )


def _row_timestamp(row: object) -> int | None:
    """The unix time a history row was played, from ``stopped`` (preferred) or ``date``.

    Tautulli rows carry both; ``stopped`` is when the view ended, which is the most
    conservative 'was this watched' signal. Returns None for a row we cannot read a time
    from, which the caller treats as 'no evidence of a play' rather than crashing.
    """
    if not isinstance(row, dict):
        return None
    for field_name in ("stopped", "date"):
        value = row.get(field_name)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None
