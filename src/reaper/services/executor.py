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
  The *live* per-item interlocks (the streaming veto, the played-since-approval check,
  the size re-read, and the mid-run arm re-check) query live services or fresh state, so
  they run only in a real send, not in a dry run; the dry run proves the plan's shape and
  the reversible checks, not those live reads.

* **The transport guard, underneath.** ``GuardedTransport`` (and its ``GuardedSession``
  twin for Plex) refuses any mutating call unless ``REAPER_DESTRUCTIVE_ACTIONS_ENABLED``
  is set on the host AND the executor declared the intent. These two layers are not the
  same check written twice: ``dry_run`` is the executor's decision, the guard is a
  property of the host that no browser can reach. A bug that made ``dry_run`` default to
  False would still hit the guard; a bug that bypassed the guard would still be stopped
  by ``dry_run``. Neither alone is trusted.

The interlocks, in order, and why each exists:

1. **Approval re-check -- the frozen set, and the policy that judged it.** Two halves,
   because an approval can be voided from either side.

   The *manifest* half is a frozen-snapshot integrity check. The condemned candidate rows
   are frozen per snapshot and never mutated, and this re-hashes those same rows, so what
   it actually detects is *loss or tampering of the frozen set* (e.g. a candidate row
   deleted out from under the run by retention GC), not live library drift -- the executor
   does not re-read the *arr here, so a movie deleted or resized in Radarr after approval
   would not change this hash. Live drift is caught elsewhere, by the per-item interlocks
   below (the streaming veto, the played-since-approval check, and the per-item
   existence and size re-reads at delete time); a stale tab replaying yesterday's plan is
   stopped by the route's confirmation-phrase recompute and the "executes once" guard.

   The *policy* half catches what the manifest structurally cannot: an operator who adds a
   protection or raises the condemn threshold changes no candidate row, so the fingerprint
   still matches while the plan now targets files the new policy keeps. The run carries the
   policy hash its snapshot was scored under, and this compares it against the policy in
   force right now. Anything scored under a superseded policy is refused, and the remedy is
   one scan.
2. **Manual spare re-check, per item.** The owner may spare an item by hand *after* the
   plan is built, which is what the grace countdown invites them to do (this executor never
   reads that window; see ``services.grace``). A spare does not
   change the frozen candidate row (still ``condemn``) or the manifest hash, so this is a
   distinct check, run for every item in dry-run and for real alike, and it wins.
3. **Caps ABORT, never truncate.** A run over its item or byte cap stops entirely.
   Truncating would make *what* gets deleted depend on sort order -- a subtle,
   order-dependent bug in the one place it must not exist.
4. **The canary.** Ordinal 0, the single smallest item, executes and verifies alone.
   Only if the world changed exactly as predicted does the rest proceed. A broken path
   mapping costs one worthless file, not the whole run.
5. **Mid-run disarm halts the run.** The arm switch is re-read before every item
   (``_still_armed``, through the ``armed_recheck`` the execute route injects -- a fresh
   read of the same database switch the UI toggles, never this run's cached snapshot).
   The moment it reads off -- or cannot be read at all -- the run ABORTS before its next
   item. Files already verified deleted stay deleted (they are not coming back), but
   nothing further is sent. The transport guard still enforces arming underneath on
   every single call.
6. **Active-stream veto, re-polled before every delete.** Not once at the start: a run
   takes minutes and someone can start watching mid-run. Fail-closed -- if Plex cannot be
   read, the item is spared.
7. **Watched-since-approval.** If anyone played the item after it was approved, it is
   spared -- the grace period existed precisely so this could still happen. Fail-closed on
   any Tautulli error or any history row we cannot precisely timestamp.
8. **Size re-read at delete time.** The item's live size is re-read immediately before
   anything is sent (``sizeOnDisk`` on the movie in ``_send_movie``; the season's episode
   files in ``_send_season``) and compared against the frozen size the owner approved.
   A file that grew beyond the allowance (``_grew_materially``) was upgraded since the
   scan -- the approval, the caps and the typed phrase all counted a smaller file -- so
   the item is skipped (kept), and so is one whose current size cannot be read at all.
   That comparison needs two CONFIRMED numbers, and it does not police the *approved*
   side: ``_grew_materially(0, live)`` reduces to ``live > 256 MiB``, so it stays silent
   for anything smaller. The approved side is policed instead by
   ``_may_send_unmeasured``, which both send paths call before the growth check, and by
   ``_deletable``, which keeps it out of the caps and out of the phrase the owner types.
   Neither refusal is unconditional: an owner who raises
   ``ProfileSettings.max_unmeasured_per_run`` admits a bounded number of unmeasured items
   deliberately, and each is sent with this growth comparison unavailable.
9. **Verify the world changed.** A movie: re-read the exclusion list and assert the tmdbId
   is present *and* the movie is gone -- Sonarr and Radarr each accept the *other's*
   exclusion parameter and return 200 while doing nothing, so the 200 is re-read, not
   trusted. A season: verify the unmonitor took *before* deleting any file, then verify no
   file for the season remains after.
10. **The trash interlock (``emptyTrash``), at the very end of a real run.** The *arr
    delete is the reclamation -- the file is off disk once Radarr/Sonarr removes it.
    Purging Plex's stale entry is cosmetic cleanup, and it is the single call that turns
    an unmounted-library mistake into a lost library, so ``_finalize_plex`` runs it only
    per affected section and behind three checks: every *arr root folder reports
    accessible, only path-scoped refreshes were issued, and the section's item count --
    captured before the first delete -- shrank by no more than what this run deleted under
    it (``_trash_delta_is_ours``). Any doubt skips the purge: a stale "unavailable" entry
    is cosmetic, a wrong purge destroys other items' library records.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import batched
from typing import Any, Protocol, cast

import structlog
from sqlalchemy import Update, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clients.base import IntegrationError, SafetyViolationError
from reaper.clients.plex import ActiveStream, PlexSectionPaths, declared_mutation
from reaper.clock import utcnow
from reaper.config import RuntimeSafety
from reaper.db import KEY_CHUNK
from reaper.db.models import (
    ActionStep,
    Candidate,
    ReapRun,
    RunState,
    SizeSource,
    Snapshot,
    StepState,
)
from reaper.engine.policy import ProfileSettings
from reaper.services import list_config, whitelist
from reaper.services.condemned import effective_condemned, effective_verdict
from reaper.services.planner import MediaRef, manifest_hash
from reaper.services.profiles import live_policy_hash

log = structlog.get_logger(__name__)

#: The irreversible step of an item's plan -- the one that removes files. A movie has a
#: ``radarr_delete``; a season's file delete is ``sonarr_delete_files``, reached only
#: after its reversible unmonitor and the verification of it. Everything else in a
#: season plan is a read or a reversible edit.
_TERMINAL_DELETE_KINDS = frozenset({"radarr_delete", "sonarr_delete_files"})

#: How many times each post-delete settle re-reads before concluding it did not land. Fixed,
#: not injected: no caller ever passed a count, so both were a constructor argument clamped
#: by ``max(1, ...)`` against an input that could not arrive. The paired *delays* stay
#: injectable, which is what a test needs to run these loops at full speed.
_EXCLUSION_POLL_ATTEMPTS = 5
_PLEX_SETTLE_ATTEMPTS = 10

#: The two live interlocks every real send passes before it deletes -- shared labels so the
#: movie and season checklists read the same. Reaching a send means both of these are True.
_CHECK_NOT_WATCHING = "Nobody was watching it right now"
_CHECK_NOT_PLAYED_SINCE = "Not played since you approved it"

#: The size re-read's allowance floor. A file may read a little larger than the frozen
#: candidate (a rescan rounding differently, a subtitle landing beside a season) without
#: meaning an upgrade, so growth within one tenth of the approved size -- or within this
#: floor for small items, where a tenth is noise-sized -- is tolerated. A real quality
#: upgrade is a step change (a 2 GB web-dl becoming a 40 GB remux) and clears both.
_SIZE_DRIFT_FLOOR = 256 * 1024**2


def _grew_materially(approved_bytes: int, live_bytes: int) -> bool:
    """Did the item grow past the allowance since it was approved?

    Only growth counts: an item that SHRANK deletes less than the owner approved, which
    is the safe direction. Growth beyond the allowance means the file was upgraded after
    the scan -- the approval, the caps and the typed confirmation all counted the smaller
    file -- so the caller keeps it (rule: the count beside the button must match what is
    acted on).
    """
    return live_bytes > approved_bytes + max(approved_bytes // 10, _SIZE_DRIFT_FLOOR)


def _payload_size(value: object) -> int | None:
    """A byte size read back from a live *arr payload, or None when unreadable.

    None (missing field, junk type, negative, **zero**) means the size cannot be
    confirmed, and the caller treats that as drift it cannot rule out: the item is kept.

    Zero counts as unreadable, and that is deliberate. The scan-side parsers already
    treat it that way (``snapshot._reported_size``, ``sonarr_stats._reported_size``),
    because an *arr that reports a file it holds with no size is sending a partial
    payload, not describing an empty file. This function used to disagree -- it accepted
    0 as a measurement -- and the disagreement was a hole straight through the size
    interlock: a stored 0 (the same partial payload, frozen at scan time) against a live
    0 is no growth at all, so the check passed and real files were deleted with both
    numbers fabricated. A genuinely 0-byte file is pathological and reclaims nothing, so
    keeping it costs the owner nothing either.
    """
    try:
        size = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


#: What the operator is told when the scan never got a size for an item. Plain, and it
#: says what happened to the file, because this is the only place the refusal surfaces.
_NO_APPROVED_SIZE_REASON = (
    "Reaper never got a size for this when it was scanned, so it cannot confirm this is "
    "what you approved. Kept."
)
_NO_APPROVED_SIZE_CHECK = "No size was recorded for it at scan time. Kept."

#: The size re-read's two checklist lines. Both send paths reach both, and the sentence an
#: operator reads must not depend on which one they are looking at, so each is written once
#: here rather than at each branch (rule 144). Four branches share them, not two: the season's
#: unconfirmed guard fires on a file list that is empty AND on one carrying a size it could not
#: read. The *reason* beside each stays per-path and inline, naming the item and its numbers,
#: which is what a checklist line this short cannot carry (rule 21).
_CHECK_SIZE_UNCONFIRMED = "Couldn't confirm its current size. Kept."
_CHECK_GREW_SINCE_APPROVED = "It grew since you approved it. Kept."


def size_confirmed(candidate: Candidate) -> bool:
    """Did the scan actually get a size for this item?

    ``Candidate.size_bytes`` is NULL when nothing would report one, which is the whole
    answer. A stored ``0`` used to mean the same thing, and reading it that way worked
    only for as long as every call site remembered to ask; a NULL makes the sites that
    forget raise instead of quietly summing.

    An unconfirmed size cannot be caught downstream by the growth check --
    ``_grew_materially(0, live)`` reduces to ``live > _SIZE_DRIFT_FLOOR`` -- so it is
    refused directly. This predicate has exactly two callers, both on the counting side:
    ``_deletable``, which keeps the item out of the caps and out of the byte total behind
    the confirmation phrase, and ``api.runs._planned_candidates``, which runs the same
    filter so both numbers describe one set. The per-item refusal before anything is sent
    is a *stronger*, separate check,
    ``_may_send_unmeasured``: it asks this question and also requires the frozen size to
    measure the same quantity the live re-read will (``_measures``). ``_send_movie`` /
    ``_send_season`` call that one, never this one -- collapsing them back to this
    predicate would silently drop the comparable-quantity half.

    Neither refusal is unconditional. Both yield to the owner's allowance
    (``ProfileSettings.max_unmeasured_per_run``): above zero, a bounded number of
    unmeasured items are planned, ARE counted against the item caps, and are sent with the
    growth comparison unavailable.

    The planner holds these items back before a plan exists at all
    (``planner.build_plan``). This is the independent second layer, and it deliberately
    does not trust that one: the host-side checks must hold even if the plan is wrong.
    """
    return candidate.size_bytes is not None


def _season_number(obj: dict[str, Any]) -> int:
    """The season an *arr object belongs to, or -1 when it cannot be read.

    Sonarr always sets ``seasonNumber``, but a null or malformed value must not crash a
    run mid-reap: -1 is a sentinel that matches no real season (0 is Specials, and is
    preserved), so an unreadable value simply excludes the object from a season match
    the way a missing field already did -- it never raises out of the executor.
    """
    value = obj.get("seasonNumber")
    if value is None:
        return -1
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _gb(value: int) -> str:
    return f"{value / 1024**3:.1f} GB"


def _planned_add_exclusion(step: ActionStep) -> bool:
    """The import-exclusion decision the planner froze into this delete step, read back
    from the approved journal so the send honors exactly what the operator previewed --
    never a setting re-read that could have changed since approval.

    Defaults to True (add the exclusion and verify it) when the body is missing or
    unparseable: the exclusion is protective, not destructive, so an unreadable plan
    resolves toward keeping a deleted title from re-downloading. Fails closed.
    """
    raw = step.body_json
    if not raw:
        return True
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return True
    if not isinstance(body, dict):
        return True
    value = body.get("addImportExclusion")
    return True if value is None else bool(value)


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
    ``is_refreshing`` lets the executor wait for that scan to settle, ``item_count`` feeds
    the trash interlock's count-delta gate, and ``empty_trash`` purges the now-missing
    item so no stale entry lingers.

    Every section is addressed by its **key**, never its title: two Plex libraries may
    share a title, and the count-delta interlock comparing the wrong library's size is
    exactly how a purge would be approved that this run never earned (rule 6/57).
    """

    async def active_streams(self) -> list[ActiveStream]: ...
    async def section_paths(self) -> list[PlexSectionPaths]: ...
    async def refresh_path(self, section_key: int, path: str) -> None: ...
    async def is_refreshing(self, section_key: int) -> bool: ...
    async def item_count(self, section_key: int) -> int: ...
    async def empty_trash(self, section_key: int) -> None: ...


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

    file_removed: bool = False
    """Set on a FAILED outcome whose file is nonetheless really off disk.

    A movie Radarr deleted whose import exclusion never landed is a genuine failure -- the
    verification did not pass -- and it is also a file that is gone. The run's bookkeeping
    needs both facts: the state drives the canary and the checklist, this drives the post-run
    rescan, which exists because removed files leave the queue stale. A VERIFIED outcome
    needs no flag (``deleted_items`` already counts it), so this defaults False and is set
    only where a removal is proven under a failure."""

    halts_run: bool = False
    """Set on a FAILED outcome the executor could not classify, which stops the whole run.

    A *mapped* failure is a known shape and one item's problem: an ``IntegrationError``, a
    guard refusal, a missing instance route. The run records it and carries on, because the
    canary already proved the mechanism works and one stubborn title is no reason to abandon
    the rest.

    An UNMAPPED exception is a different thing. Reaper does not know what just happened, nor
    how far it got, and the only honest reading of "I do not understand this" on a deletion
    path is to stop touching files. Funnelling it through :meth:`Executor._fail` alone
    (so the journal records it and the run does not wedge in EXECUTING) let the run continue
    into items 3..N after an unclassifiable failure at item 2 -- the wedge was fixed by
    trading away the halt. This flag keeps both: the item is journalled FAILED and recorded
    in the report, and then :meth:`Executor._run_deletes` aborts the run."""


@dataclass
class RunReport:
    """What a run did, or would do. The audit record and the UI's after-action view."""

    run_id: int
    dry_run: bool
    state: RunState
    outcomes: list[StepOutcome] = field(default_factory=list)
    deleted_items: int = 0
    deleted_bytes: int = 0
    deleted_unmeasured: int = 0
    """How many of ``deleted_items`` had no size, so contributed nothing to
    ``deleted_bytes``. Only ever above zero when the operator raised
    ``ProfileSettings.max_unmeasured_per_run``. Carried so the report can say why its two
    numbers describe different sets, rather than letting the byte figure read as the whole
    story."""

    skipped: int = 0

    removed_unconfirmed: int = 0
    """Items whose file is gone but whose step ended FAILED -- a delete Radarr honored whose
    import exclusion could not be verified. They are not ``deleted_items`` (nothing about
    them is confirmed) but they DID change the library, so the post-run rescan has to fire
    on them or the queue keeps offering files that no longer exist."""

    aborted_reason: str | None = None

    @property
    def library_changed(self) -> bool:
        """Did this run remove anything at all? The rescan trigger.

        Confirmed deletions plus the ones whose removal is proven under a failed
        verification. Reading ``deleted_items`` alone skipped the rescan for a run whose
        every item deleted but failed its exclusion check, leaving the queue showing files
        that were already gone."""
        return self.deleted_items > 0 or self.removed_unconfirmed > 0

    def record(self, outcome: StepOutcome) -> None:
        self.outcomes.append(outcome)
        if outcome.state == StepState.FAILED and outcome.file_removed:
            self.removed_unconfirmed += 1


@dataclass(frozen=True)
class ReapProgress:
    """A live snapshot of a running reap, emitted after every item.

    A real reap is one item at a time and a large one takes a while, so the executor hands
    the API layer this after each item to fill a status the browser polls -- the same shape
    the scan uses (``snapshot.Progress``). Cumulative counts, not deltas, so a dropped poll
    never loses ground. ``done``/``total`` count the *plan's* delete units (a whole season
    is one), matching the number the owner confirmed.
    """

    done: int
    total: int
    deleted_items: int
    deleted_bytes: int
    skipped: int
    title: str


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


#: Why a run stops when it could not write its own journal. The database is the only record
#: of what was removed, so a run that cannot write it stops rather than deleting more that it
#: also could not record.
#: It stops at what the operator must go and do. It deliberately does NOT tell them to scan
#: again: the report panel appends "a fresh scan is running" to this very paragraph whenever
#: the run removed anything, so the two sentences told them to start a scan and that one was
#: already going, in the same breath (rule 144). The surface that knows whether a scan was
#: launched is the one that says so.
_JOURNAL_HALT = (
    "Reaper could not save its record of what it just did, so it stopped before touching "
    "anything else. Anything already removed stays removed. Check the free space and "
    "permissions on Reaper's data folder."
)


class _JournalWriteError(RuntimeError):
    """A journal write did not land, even after a rollback and a retry.

    Deliberately not an :class:`ExecutionError` and deliberately caught nowhere in the item
    path: every handler down there turns an exception into a *recorded* item outcome, and
    recording is precisely what is not working. Two of them would make it worse -- the
    ``ExecutionError`` handler in ``_send_for_real`` treats it as one item's problem and lets
    the run walk on, and any handler that builds an outcome reads the item's rows, which a
    rollback has just expired.

    So it unwinds to ``execute()``, which records the run's terminal state through a path
    that touches no ORM row at all.
    """


@dataclass(frozen=True)
class _Terminal:
    """A run's final row, as plain values rather than attributes on the ORM object.

    The terminal write is the one that has to land even when the session's transaction has
    already died, and recovering from that means ``rollback()`` -- which expires every ORM
    object in the session. So these travel where a rollback cannot reach them; see
    :meth:`Executor._commit_journal`.
    """

    state: RunState
    aborted_reason: str | None
    finished_at: datetime


@dataclass(frozen=True)
class _JournalRow:
    """One step's journal columns, read off the ORM object *before* a commit that may fail.

    Same reason as :class:`_Terminal`, and the same hazard: after a rollback these attributes
    are expired, so reading one raises ``MissingGreenlet`` rather than quietly reloading it.
    Captured first, replayed as an ``UPDATE`` after.

    It carries ``file_removed_at`` for a specific reason. That stamp is committed by
    :meth:`Executor._mark_file_removed` the moment a delete is confirmed, and that commit is
    itself one of the ones that can fail -- which loses the record that a file is gone while
    the file stays gone. Those bytes then drop out of
    :meth:`Executor._rolling_30d_deletions`, the rolling delete budget reads light, and a
    later run spends past what the operator set. That is rule 5/30 failing open, and it is
    the part of a failed commit that outlives the run.
    """

    id: int
    state: StepState
    error: str | None
    sent_at: datetime | None
    verified_at: datetime | None
    verification_json: str | None
    file_removed_at: datetime | None

    @classmethod
    def of(cls, step: ActionStep) -> _JournalRow:
        return cls(
            id=step.id,
            state=step.state,
            error=step.error,
            sent_at=step.sent_at,
            verified_at=step.verified_at,
            verification_json=step.verification_json,
            file_removed_at=step.file_removed_at,
        )

    def replay(self) -> Update:
        return (
            update(ActionStep)
            .where(ActionStep.id == self.id)
            .values(
                state=self.state,
                error=self.error,
                sent_at=self.sent_at,
                verified_at=self.verified_at,
                verification_json=self.verification_json,
                file_removed_at=self.file_removed_at,
            )
            .execution_options(synchronize_session=False)
        )


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


def _deletable(
    deletes: Sequence[_Delete], effective_keys: set[str], *, allow_unmeasured: bool = False
) -> list[_Delete]:
    """The exact set a run will act on: the items in the effective condemned set.

    ``effective_keys`` is the membership of ``services.condemned.effective_condemned``,
    read fresh for this execution: scan-condemned minus hand-spares plus effective hand
    reaps. Every cap -- per-run and rolling -- counts THIS set, the same one the
    confirmation phrase counts (runs.py ``_planned_candidates``), so enforcement and
    approval agree. An item the owner spared (or un-reaped) after the plan was built
    still carries delete steps but is skipped per item in ``_one_delete``; counting it
    would abort a legitimate reduced run. Fails safe: an override change only ever
    removes items from the count of what THIS run sends.

    An item whose approved size was never confirmed (``size_confirmed``) drops out for
    the same reason, wherever the allowance below is shut: ``_may_send_unmeasured`` refuses
    it in the send paths, so it is not part of what this run acts on, and counting it as 0
    bytes -- which is what its stored size says -- would let a run delete materially more
    than the cap and the typed total the owner confirmed.

    ``allow_unmeasured`` is the operator's allowance
    (``ProfileSettings.max_unmeasured_per_run``) above zero. Those items ARE acted on, so
    they belong in this set: they count against the item caps, and they are excluded from
    the byte sums by ``_deletable_bytes`` rather than by being missing here. Letting them
    contribute a zero to a byte cap instead would be the original bug restored by the
    back door.
    """
    return [
        d
        for d in deletes
        if d.candidate.media_key in effective_keys
        and (allow_unmeasured or size_confirmed(d.candidate))
    ]


#: Which stored measurements the movie send path may compare against its live re-read.
#: It reads Radarr's ``sizeOnDisk``, which sums the movie's tracked file rows, and the
#: live side reads the same field, so ``RADARR`` is like-for-like. ``RADARR_FILE`` and
#: ``PLEX`` read a single file where that sums every row, so they can measure less; if one
#: arrives here anyway it is kept rather than compared against a different quantity.
_MOVIE_COMPARABLE = frozenset({SizeSource.RADARR})

#: And for seasons, which delete file by file and re-read the summed episode files. Both
#: members are that same quantity: Sonarr's ``seasons[].statistics.sizeOnDisk`` is not a
#: folder walk but a ``SUM`` over the ``EpisodeFiles`` table grouped by series and season,
#: which is the same table and column ``GET /api/v3/episodefile?seriesId=`` returns row by
#: row. So the frozen and live sides already match, and the interlock's blind band is only
#: the tolerance ``_grew_materially`` declares.
_SEASON_COMPARABLE = frozenset({SizeSource.SONARR_FILES, SizeSource.SONARR})


def _measures(candidate: Candidate, comparable: frozenset[SizeSource]) -> bool:
    """Does the frozen size measure the same thing the live re-read will?

    An exhaustive allow-list with a fail-closed default: a source this build does not
    recognize, or none at all, answers False and the item is kept. The alternative is an
    unwritten ``else`` branch that either skips every such item or, worse, weighs a single
    file against a sum of every file and calls the difference growth.
    """
    return candidate.size_source in comparable


def _deletable_bytes(deletable: Sequence[_Delete], *, allow_unmeasured: bool = False) -> int:
    """Total bytes over the exact set a run will act on.

    Asserts rather than coercing. ``_deletable`` has already dropped every item whose size
    was never confirmed, so an unknown reaching here means that filter regressed. Aborting
    is the only safe answer: a stand-in zero under-states the total, an under-stated total
    under-states a byte cap, and a cap that does not fire deletes more than the owner
    allowed. Note the direction: rounding toward keeping is right on the scoring lane and
    exactly backwards here.

    This should never fire, and must not be deleted as unreachable. It is the only thing
    standing between a future regression in the planner's filter and a cap that silently
    stops working.

    With the operator's allowance open, an unmeasured item is a legitimate member of the
    set, so it is simply left OUT of the byte total. It is not counted as zero: the item
    caps bound it instead, which is why the allowance is a count.
    """
    unknown = sorted(d.candidate.media_key for d in deletable if d.candidate.size_bytes is None)
    if unknown and not allow_unmeasured:
        raise ExecutionError(
            "Reaper couldn't measure the size of these items, so it can't check the run "
            f"against your limits: {unknown}. The run is aborted."
        )
    return sum(d.candidate.size_bytes for d in deletable if d.candidate.size_bytes is not None)


def _check_caps(
    deletes: Sequence[_Delete], settings: ProfileSettings, effective_keys: set[str]
) -> None:
    """A run over its per-run cap ABORTS before it starts. It never runs the part that
    fits: truncating would make *what* gets deleted depend on sort order. The rolling
    30-day caps are enforced separately, by ``Executor._check_rolling_caps``, which also
    aborts rather than truncates.

    The unknown-size limit is always enforced; the four run-size caps are enforced only
    while ``caps_enabled`` is on. Turning the caps off is a deliberate choice (a big first
    cleanup), and it drops only the run-size ceilings -- never the unknown-size rule, which
    is a separate keep-what-we-cannot-measure guard, and never any other interlock."""
    allow_unmeasured = settings.max_unmeasured_per_run > 0
    deletable = _deletable(deletes, effective_keys, allow_unmeasured=allow_unmeasured)

    # The allowance is a COUNT, and this is where the host enforces it as one. The planner
    # checks it too, at plan time, but the settings can move between approving a plan and
    # running it -- and lowering a limit that is only ever read as "above zero or not"
    # would be silently ignored on the one population no byte cap can bound. Enforced
    # whether or not the run-size caps are on: it is not a run-size cap.
    unmeasured = sum(1 for d in deletable if d.candidate.size_bytes is None)
    if unmeasured > settings.max_unmeasured_per_run:
        raise ExecutionError(
            f"This run would delete {unmeasured} items Reaper couldn't measure, over your "
            f"limit of {settings.max_unmeasured_per_run} per run. It stops rather than "
            "deleting just part. Raise the unknown-size allowance in Policy, under Pace and "
            "limits."
        )

    # The four run-size caps are the optional second layer, on top of the deletion
    # password and the typed confirmation. Off, they stop bounding the run.
    if not settings.caps_enabled:
        return

    items = len(deletable)
    total_bytes = _deletable_bytes(deletable, allow_unmeasured=allow_unmeasured)
    if items > settings.max_items_per_run:
        raise ExecutionError(
            f"This plan would remove {items} titles, over your per-run cap of "
            f"{settings.max_items_per_run}. It stops rather than deleting just part, "
            "because which titles go must never come down to sort order. Raise the cap "
            "or turn limits off in Policy, under Pace and limits."
        )
    if total_bytes > settings.max_bytes_per_run:
        raise ExecutionError(
            f"This plan would remove {total_bytes / 1024**3:.0f} GB, over your per-run "
            f"cap of {settings.max_bytes_per_run / 1024**3:.0f} GB. It stops rather than "
            "deleting just part. Raise the cap or turn limits off in Policy, under Pace "
            "and limits."
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
        armed_recheck: Callable[[], Awaitable[bool]] | None = None,
        stop_recheck: Callable[[], Awaitable[bool]] | None = None,
        progress: Callable[[ReapProgress], None] | None = None,
        exclusion_poll_delay: float = 1.0,
        plex_settle_delay: float = 2.0,
    ) -> None:
        self._session = session
        self._safety = safety
        self._settings = settings
        # The mid-run kill switch: re-read before every item of a real run, so turning
        # deletion off in the UI halts a run already in flight. Injected (the route reads
        # the database switch through a FRESH session -- this run's own session caches
        # rows across its per-item commits and would keep answering with the stale
        # value). Required for a real run; a dry run sends nothing and needs none.
        self._armed_recheck = armed_recheck
        # The explicit Stop, from the reap window or the follow-you bar: a separate signal
        # from disarming, so stopping one run leaves deletion armed for the next. Re-read
        # before every item, like the arm switch. A convenience layer, NOT a safety
        # interlock -- the armed-recheck and the caps stay the fail-closed guards -- so an
        # unreadable stop flag resolves toward continuing, never toward a spurious halt.
        self._stop_recheck = stop_recheck
        # A sink for per-item progress (the API's polled status). None for a dry run and any
        # headless run; the reap still completes, it just reports nowhere.
        self._progress = progress
        # dry_run defaults True. Deleting requires opting *in*, at the call site and
        # again at the host via the guard. Nothing deletes by omission.
        self._dry_run = dry_run
        self._gateway = gateway
        # Radarr adds the import exclusion a beat *after* the delete returns 200, so the
        # verification re-reads the exclusion list a few times before concluding it did not
        # land. Tests pass a zero delay to stay fast.
        self._exclusion_poll_delay = exclusion_poll_delay
        # Plex scans are asynchronous, so the trash purge waits for the refresh to settle.
        self._plex_settle_delay = plex_settle_delay
        # Plex movie sections whose path we refreshed this run -- the ones to purge trash
        # from at the end, once, if the mount is confirmed up. Keyed by section KEY, not
        # title: two libraries may share a title, and the interlock below must not read one
        # library's size while purging another's trash (rule 6/57).
        self._affected_sections: set[int] = set()
        # The trash interlock's inputs: each section's item count BEFORE anything was
        # deleted, and WHICH Plex listings this run removed under each section. The
        # purge runs only when a section shrank by no more than what we deleted there.
        #
        # The listings are held as a set of rating keys rather than a running count,
        # because two candidates can name the SAME listing: two *arr instances holding one
        # tmdb id each bind to a single merged Plex item (engine/identity.py binds on one
        # unambiguous hit), and nothing dedups candidates by rating key. Summing per
        # candidate charged that one listing twice, so the allowance grew by one per
        # collision while the section's own count could only ever fall by one -- widening
        # the single bound on how much of a section-wide purge may be someone else's.
        self._section_pre_counts: dict[int, int] = {}
        self._deleted_by_section: dict[int, set[int]] = {}
        # Section key -> title, filled as sections are read, so the log lines an operator
        # reads still name the library rather than a number.
        self._section_titles: dict[int, str] = {}
        # The manual spare/reap overrides. Loaded at the top of execute() and RE-READ before
        # every item of a real run (``_refresh_overrides``): an item spared by hand after the
        # plan was built must not be deleted, and the owner can spare at any point in the
        # grace window -- including while the reap is in flight, which is exactly when they
        # are watching a title they want back go past. Per item, not per run, because a run
        # takes minutes and a decision made in minute two must reach minute three.
        self._decisions: dict[str, str] = {}
        # The effective condemned set as it stood when the run was claimed. Deliberately NOT
        # refreshed: it is the ceiling on what this run may send (the caps and the operator's
        # typed confirmation both counted it), so the per-item checks intersect against it
        # and can only ever remove items, never add one that was never authorized.
        self._effective_keys: set[str] = set()
        # What ``_revive`` re-reads. Set once the run is loaded; None before that, when there
        # is nothing loaded to revive.
        self._run_id: int | None = None
        self._snapshot_id: int | None = None
        # Journal writes whose own commit did not land, kept as statements so the end of the
        # run can try them once more. A fault that broke one commit is usually gone seconds
        # later -- the vacuum finished, the disk was freed -- and the write it took down is
        # the record that a file is off disk. See ``_commit_and_finalize``.
        self._unwritten: list[Update] = []
        # Whether the item being walked right now has really had its file removed. Tracked
        # here, off the ORM, because the one path that needs to read it is the one where the
        # journal cannot be written and every row is expired. Reset per item by
        # ``_run_deletes``.
        self._file_is_gone = False

    async def execute(self, run_id: int) -> RunReport:
        run, steps = await _load(self._session, run_id)
        self._run_id = run_id
        self._snapshot_id = run.snapshot_id
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
                    "Refusing a real run without Plex: the active-stream veto (re-polled "
                    "before every delete) cannot run, and deleting blind to who is "
                    "watching is exactly what must never happen."
                )
            if self._gateway.tautulli is None:
                raise ExecutionError(
                    "Refusing a real run without Tautulli: the played-since-approval check "
                    "cannot run, and the grace period exists precisely so a late view can "
                    "still spare an item."
                )
            if self._armed_recheck is None:
                raise ExecutionError(
                    "Refusing a real run without a live arm check: turning deletion off "
                    "could not stop a run already in progress."
                )

        condemned = await _condemned(self._session, run.snapshot_id)

        # The owner's manual overrides, read fresh: a spare added since the plan was built
        # must still stop the delete, and a hand reap withdrawn since must stop its item
        # too. This is the layer that enforces both at the point of no return (see the
        # per-item checks in ``_one_delete``, which re-read this map before every item of a
        # real run). The effective set is what THIS run may send, fixed here as the ceiling;
        # the frozen ``condemned`` dict above stays untouched for the manifest.
        self._decisions = await whitelist.overrides(self._session)
        effective = await effective_condemned(self._session, run.snapshot_id, self._decisions)
        self._effective_keys = set(effective)
        self._affected_sections = set()
        self._section_pre_counts = {}
        self._deleted_by_section = {}
        self._section_titles = {}

        # -- interlock 1: the frozen condemned set must still be intact ----
        # Hashed over the WHOLE condemned set -- spared or not -- so that sparing an item
        # after approval does not change the fingerprint and void the run. The planner hashes
        # the same whole set for exactly this reason; a spare is not a change to *what was
        # condemned*, it is a decision to keep one of them, which the per-item check honors.
        # Both sides hash the identical frozen candidate rows for this immutable snapshot, so
        # this is a snapshot-integrity check: it fires if a condemned candidate row is lost or
        # tampered with under the run (e.g. retention GC), NOT if the live library moved --
        # nothing re-reads the *arr here. Real live drift (a movie deleted or resized in
        # Radarr since approval) is caught by the per-item interlocks and the existence
        # re-reads at delete time, and a stale tab is stopped by the route's confirmation
        # recompute and the "executes once" guard above.
        current_hash = manifest_hash(sorted(condemned.values(), key=lambda c: c.media_key))
        if current_hash != run.approved_manifest_hash:
            raise ExecutionError(
                "The condemned set changed since this plan was approved: an item was "
                "added, removed, or resized. The approval was for a different plan and "
                "is void. Re-scan, re-review, and approve the new plan."
            )

        # -- interlock 1, second half: the policy must still be the one in force ----
        # The manifest above cannot catch a policy edit: it hashes frozen candidate rows,
        # which a policy change never touches. So an operator who adds a protection --
        # exactly the act that should void a pending approval -- would otherwise come back
        # to the Reap page and delete the very items they just protected.
        #
        # A plan carries the policy hash of the snapshot it was built from, and a policy
        # edit does NOT trigger a rescan, so this refuses a plan built AFTER the edit as
        # well as one built before it. That is deliberate, not collateral: both were
        # scored under the old policy, and the Policy page already says a saved policy
        # takes effect on the next scan. The remedy is one scan, and it is the same
        # remedy either way.
        #
        # Checked in the dry run too, so the simulation proves the same refusal. A policy
        # Reaper had to recover reads as the shipped default here (``active_policy`` never
        # raises), which will not match and so refuses -- the same direction rule 65 wants.
        if run.policy_hash != await live_policy_hash(self._session):
            raise ExecutionError(
                "Your policy changed after this plan was approved, so the plan is out of "
                "date and nothing was deleted. Run a new scan, then review and plan again."
            )

        # -- interlock 1, third half: the protection lists must still be the ones scanned ----
        # The two hashes above cannot see a list edit. A keep list is not policy -- retagging
        # one, repointing it at another Plex collection, or switching it off changes which
        # titles the `on_list` rules protect while every policy body stays byte for byte the
        # same -- and the per-item interlocks below never re-read membership, so an operator
        # who narrows a keep list after approving a plan would come back and delete the very
        # titles it had been keeping. Before this branch the same knob was `keep_tags` ON the
        # policy body, so the policy hash moved and this refused; moving it to the registry
        # took that cover away with it (rule 140: the same value, re-qualified).
        #
        # `None` on EITHER side refuses and the two are never compared: the snapshot's is a
        # scan that degraded for a registry it could not read, the live one is a registry that
        # cannot be read right now, and they are different unknowns (rules 93, 104). A
        # snapshot predating the column carries `None` too, and a plan built under lists
        # nobody can name is exactly what must not execute.
        #
        # Checked in the dry run too, so the simulation proves the same refusal.
        # `api.simulate.simulate` makes the same three-way test for the preview panel.
        snapshot = await self._session.get(Snapshot, run.snapshot_id)
        live_lists = await list_config.current_fingerprint(self._session)
        stored_lists = snapshot.list_config_hash if snapshot is not None else None
        if stored_lists is None or live_lists is None or stored_lists != live_lists:
            raise ExecutionError(
                "Your protection lists changed after this plan was approved, so the plan is "
                "out of date and nothing was deleted. Run a new scan, then review and plan "
                "again."
            )

        # Group every step by the item it belongs to, keeping every planned item that
        # actually carries an irreversible delete step. An item is one delete unit -- a
        # movie's single call, or a season's unmonitor -> verify -> delete triple -- and
        # the canary, caps and interlocks all act on the item, never on a lone step.
        # Ordered by the item's ordinal so the canary (the smallest condemned item,
        # ordinal 0) is first. Planned items OUTSIDE today's effective set (spared, or a
        # withdrawn hand reap) stay in the walk on purpose: ``_one_delete`` skips each
        # one visibly, so the report says why an item was kept instead of losing it.
        candidates_by_key = dict(condemned)
        planned_keys = {s.media_key for s in steps}
        missing = sorted(planned_keys - set(candidates_by_key))
        # Chunked (rule 94). `condemned` is the FROZEN scan-condemned set while the steps were
        # planned from the effective one, so `missing` is exactly the honored hand reaps --
        # bounded by whitelist rows, one per hand click, the same bound
        # `condemned._reap_overridden_rows` carries. Chunked anyway because nothing in the type
        # says so, and a later planner that admits a wider set would find this read already
        # safe. Merged into a map keyed by media_key, so the chunks cannot reorder it.
        for chunk in batched(missing, KEY_CHUNK, strict=False):
            extra = (
                (
                    await self._session.execute(
                        select(Candidate).where(
                            Candidate.snapshot_id == run.snapshot_id,
                            Candidate.media_key.in_(chunk),
                        )
                    )
                )
                .scalars()
                .all()
            )
            candidates_by_key.update({c.media_key: c for c in extra})
        deletable_keys = {s.media_key for s in steps if s.kind in _TERMINAL_DELETE_KINDS}
        by_item: dict[str, list[ActionStep]] = {}
        for s in steps:
            if s.media_key in candidates_by_key and s.media_key in deletable_keys:
                by_item.setdefault(s.media_key, []).append(s)
        deletes = sorted(
            (
                _Delete(
                    steps=tuple(sorted(item_steps, key=lambda st: (st.ordinal, st.id))),
                    candidate=candidates_by_key[key],
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
            # The claim is one atomic UPDATE ... WHERE state = 'planned', COMMITTED before
            # anything is sent. The read-check above gives the friendly refusal; this is
            # the enforcement: two concurrent execute calls can both read PLANNED, but
            # only one row-update wins, and the loser refuses rather than re-running the
            # plan over the winner's journal. The commit also makes EXECUTING durable, so
            # a process that dies mid-run leaves a run that says so instead of rolling
            # back to PLANNED as if nothing had been deleted.
            #
            # The one commit on this path deliberately NOT routed through _commit_journal
            # (rule 72's sweep). It sits ahead of the guarded block, so a failure here raises
            # out of execute() with the claim rolled back, the run still PLANNED, and nothing
            # sent -- already the keep-the-file answer, and a retry would only re-attempt a
            # claim the operator can simply make again. Everything after this point has files
            # at stake and gets the recovery.
            started = utcnow()
            claimed = await self._session.execute(
                update(ReapRun)
                .where(ReapRun.id == run.id, ReapRun.state == RunState.PLANNED)
                .values(state=RunState.EXECUTING, started_at=started)
                .execution_options(synchronize_session=False)
            )
            # execute() is typed as the base Result; a DML statement always yields a
            # CursorResult, which is what carries rowcount.
            if cast("CursorResult[Any]", claimed).rowcount != 1:
                raise ExecutionError(
                    f"Run {run.id} was already claimed by another request. A run executes once."
                )
            run.state = RunState.EXECUTING
            run.started_at = started
            await self._session.commit()

            # The trash interlock's baseline: every section's item count, read BEFORE
            # anything is deleted. Best-effort -- a section with no baseline simply never
            # gets its trash purged (see _trash_delta_is_ours), which is the safe failure.
            await self._capture_section_counts()

        # Set by the cancel path so the finally below commits the run's state but does NOT
        # sit in Plex's settle-wait. See _commit_and_finalize.
        canceled = False
        # The row this run must leave behind, decided by whichever handler below runs and
        # written by the finally. Held as plain values, NOT as attributes on ``run``: if a
        # journal commit has already failed, recording the terminal state means rolling the
        # session back first, and a rollback expires every ORM object in it. The old code
        # set ``run.state`` in each handler and SQLAlchemy discarded every one of those
        # writes on a dead transaction, which is how a run stayed EXECUTING forever (#327).
        terminal: _Terminal | None = None
        try:
            # -- interlock 3: caps abort the whole run ----------------------
            # Inside the guarded block, so a breach becomes a visible ABORTED report the
            # owner can read -- the same shape as a canary failure -- rather than an
            # exception that escapes. Either way the run deletes nothing. Both the
            # per-run caps and the rolling 30-day budget are checked, dry run included.
            _check_caps(deletes, self._settings, self._effective_keys)
            await self._check_rolling_caps(deletes)
            await self._run_deletes(deletes, report, run.approved_at)
            report.state = RunState.COMPLETED
            terminal = _Terminal(RunState.COMPLETED, None, utcnow())
        except ExecutionError as exc:
            report.state = RunState.ABORTED
            report.aborted_reason = str(exc)
            terminal = _Terminal(RunState.ABORTED, str(exc), utcnow())
        except _JournalWriteError:
            # A step's own journal write did not land even after the retry, so the run stopped
            # where it stood. Handled apart from the catch-all below only for the copy: this
            # is a known shape with a known remedy, not an unexpected error, and the operator
            # is told what to check. The terminal write below does not depend on the session
            # being healthy, which is why it goes through _commit_journal.
            log.warning("reap.journal_halt", run_id=run_id)
            report.state = RunState.ABORTED
            report.aborted_reason = _JOURNAL_HALT
            terminal = _Terminal(RunState.ABORTED, _JOURNAL_HALT, utcnow())
            return report
        except asyncio.CancelledError:
            # A hard cancel -- the app shutting down, or a force-stop that did not go
            # through the graceful Stop (which raises ExecutionError above). Record it as an
            # abort so the run does not linger in EXECUTING, then let the finally make that
            # durable before the cancellation propagates. Every removed file is already
            # journalled per item, so nothing is lost by aborting here. The Plex tidy-up is
            # deliberately NOT run: see _commit_and_finalize.
            canceled = True
            report.state = RunState.ABORTED
            report.aborted_reason = "The run was stopped before it finished."
            terminal = _Terminal(RunState.ABORTED, report.aborted_reason, utcnow())
            raise
        except Exception as exc:
            # The catch-all, and the last thing standing between a surprise and a wedged
            # run. Anything not mapped above -- a raw transport error out of a client, a bug
            # -- used to escape with run.state left EXECUTING, which execute() refuses to
            # re-enter and nothing in the app reconciles: the run row stuck forever with no
            # reason recorded, and the operator shown a bare error string with no way to see
            # which files had actually been removed.
            #
            # So it becomes an ordinary abort, the same shape a cap breach produces, and the
            # report is RETURNED rather than re-raised -- the report is the only record of
            # the per-item outcomes, and a run that deleted files before something surprising
            # happened is exactly when the operator most needs to read it. Nothing is
            # swallowed: the reason carries the error text into the UI, and the traceback is
            # logged here. Files already deleted keep their committed marks.
            log.warning("reap.unexpected_error", run_id=run_id, error=str(exc), exc_info=True)
            report.state = RunState.ABORTED
            report.aborted_reason = f"The run stopped on an unexpected error: {exc}"
            terminal = _Terminal(RunState.ABORTED, report.aborted_reason, utcnow())
            return report
        finally:
            # Runs on EVERY exit -- COMPLETED, a graceful abort, and a hard cancel alike --
            # because whatever ended the run, the files already removed must have their
            # outcome made durable and their stale Plex entries cleared. Previously this sat
            # after the try/except and an unexpected exception (or a cancel from
            # backgrounding the run) skipped it, orphaning Plex entries and leaving the run
            # EXECUTING. See _commit_and_finalize for why the purge is best-effort.
            #
            # ``run_id``, never ``run.id``: by here the session may have been rolled back to
            # recover from a failed journal write, which expires every ORM object, and
            # reading an expired attribute raises MissingGreenlet instead of reloading it.
            if not self._dry_run:
                await self._commit_and_finalize(run_id, terminal, canceled=canceled)
                if terminal is not None:
                    # The row was written as a Core UPDATE, which goes around the identity
                    # map, and this session is ``expire_on_commit=False`` -- so a caller
                    # still holding ``run`` would otherwise keep reading the state it had
                    # while the run was going. Set AFTER the write, never before: an ORM
                    # mutation ahead of it would be the discarded write this whole change
                    # exists to stop depending on. Setting a column on an expired instance
                    # loads nothing, so this is safe even after a recovery rollback.
                    run.state = terminal.state
                    run.aborted_reason = terminal.aborted_reason
                    run.finished_at = terminal.finished_at

        return report

    async def _commit_journal(self, *, what: str, write: Sequence[Update]) -> bool:
        """Land one set of journal writes, surviving a transaction that has already failed.

        Rule 26 wants every journal and state-transition write on the deletion path durable
        at each step, and each is its own commit. What that left uncovered is a commit that
        *raises*: the session is then holding a transaction neither committed nor rolled
        back, so every later commit on it raises ``PendingRollbackError`` whether or not the
        original fault has cleared, and SQLAlchemy discards the in-memory attribute writes
        that were riding on it ("Session's state has been changed on a non-active
        transaction - this state will be discarded"). One step commit failing under a held
        write lock therefore used to take the run's own terminal write down with it, and the
        run stayed EXECUTING on disk with a step still SENT, files already gone, and nothing
        in the app able to reconcile it (#327).

        ``rollback()`` is what makes the session usable again, so the retry does that first.
        It also EXPIRES every ORM object in the session, which is why the values travel in
        ``write`` as statements rather than as mutations of rows: after the rollback, reading
        one of those attributes would raise ``MissingGreenlet`` rather than reload it. The
        same statements run on both attempts, so there is one spelling of the write, not a
        durable one and a recovery one that can drift apart.

        Two attempts, no sleep between them. SQLite's ``busy_timeout`` (5s, see
        ``db.session._configure_sqlite``) already waits inside each, and a caller with a file
        to keep safe should not be held longer than that on a database that is not answering.

        Returns whether the writes are on disk. Never raises: every caller has a
        keep-the-file answer to a journal it cannot write, and none of them is to wedge the
        run.
        """
        for attempt in (1, 2):
            try:
                # Pending ORM writes go FIRST, and this flush is not optional. The session is
                # built ``autoflush=False`` (``db.session.create_session_factory``), so
                # ``execute()`` does not flush on its own -- and ``commit()`` does, at the end.
                # Without this, a Core UPDATE below lands and is then overwritten inside the
                # same transaction by the flush that ``commit()`` runs, carrying whatever the
                # PREVIOUS ``_mark`` had set on the row. That is how a step committed as
                # VERIFIED came to sit on disk reading SENT with a ``verified_at`` beside it.
                await self._session.flush()
                for statement in write:
                    await self._session.execute(statement)
                await self._session.commit()
            except Exception as exc:
                log.warning(
                    "reap.journal_commit_failed", what=what, attempt=attempt, error=str(exc)
                )
            else:
                if attempt == 2:
                    log.info("reap.journal_recovered", what=what)
                return True
            if attempt == 2:
                break
            try:
                await self._session.rollback()
            except Exception as exc:
                log.error("reap.journal_rollback_failed", what=what, error=str(exc))
                break
            try:
                await self._revive()
            except Exception as exc:
                # Its own handler purely so the log names the call that failed. Sharing the
                # rollback's meant a `_revive` fault was reported as `journal_rollback_failed`,
                # naming the one call that had just returned -- on the deletion path, where
                # that line is what someone reads to work out why a run wedged (#343).
                # Still breaks: the rollback expired every row, and a run that walks on
                # without them is not a trade to make here.
                log.error("reap.journal_revive_failed", what=what, error=str(exc))
                break
        # The write that would have recorded the trouble is itself the write that failed, so
        # the log is the only place this fact can still be put.
        log.error("reap.journal_unrecoverable", what=what)
        # Kept for one last try at the end of the run. Both attempts happened inside the
        # seconds the fault was live; by the time the run winds up, a vacuum has finished or
        # a disk has been freed, and the same statements land. Without this the values were
        # simply dropped -- and the terminal write, which is retried, was observed to succeed
        # moments later on the very same session, proving the database had come back while
        # the record that a file was removed stayed lost (rule 5/30).
        self._unwritten.extend(write)
        return False

    async def _revive(self) -> None:
        """Reload every row this run still works from, after a rollback expired them all.

        A rollback is the only thing that makes a session live again after a failed commit,
        and it expires every object in the identity map. Under asyncio that is not a
        reloadable state: the next attribute read anywhere in the run raises
        ``MissingGreenlet`` (or ``PendingRollbackError`` before the rollback) instead of
        quietly fetching, because a lazy load cannot be awaited from an attribute access. It
        is not confined to the row that failed either -- one rollback expires the whole
        identity map, so the items still ahead in the walk are just as dead as the current
        one.

        Two queries put it all back, because a ``SELECT`` repopulates the expired attributes
        of the rows it returns. Cheap, and only ever reached on the recovery path.
        """
        if self._run_id is None or self._snapshot_id is None:
            return
        await self._session.execute(select(ActionStep).where(ActionStep.run_id == self._run_id))
        await self._session.execute(
            select(Candidate).where(Candidate.snapshot_id == self._snapshot_id)
        )

    async def _commit_and_finalize(
        self, run_id: int, terminal: _Terminal | None, *, canceled: bool = False
    ) -> None:
        """Make the run's final state durable, then purge Plex's now-stale entries.

        The final state is committed BEFORE the purge: the purge can wait on Plex scans for
        a while, and the outcome must already be durable by then. The per-item marks are
        already committed durably in the delete loop, so this only persists the terminal row.

        It is written as an explicit ``UPDATE`` carrying plain values, and retried through
        :meth:`_commit_journal`, because this is the write whose failure has no second
        chance. Nothing reconciles a run left EXECUTING: ``execute()`` refuses any non-PLANNED
        run, no startup path touches the state, and retention never sweeps a run-bound
        snapshot. It used to be an ORM mutation under ``except Exception: log.warning``, so a
        failed commit was swallowed and the run wedged (#327).

        Keyed on the run id alone, with no ``WHERE state = ...`` guard. Rule 26 wants state
        transitions guarded, and the EXECUTING claim in ``execute()`` is that guard: it is
        what proves this executor owns the row, and there is no second writer to race. A
        condition here could only ever *decline* to record a terminal state, which is the
        wedge itself.

        ``terminal`` is None only when something that is not an ``Exception`` escaped the run
        and no handler above ran -- the process going down under it. That case deliberately
        writes no terminal state: EXECUTING is the *correct* durable record of a run that was
        interrupted with a step possibly still in flight, and it is what a reconciler would
        need to see. It is also not the wedge this method exists to prevent, which is a run
        that finished and could not say so. The pending journal writes are still committed.

        Purge runs on a COMPLETED or an ABORTED run alike, because a canary can fail its
        exclusion check *after* its file is already gone; it is gated on a section actually
        having been refreshed (a file really was removed), and _finalize_plex never raises.
        A failed purge leaves a cosmetic lingering Plex entry, never a lost file.

        ``canceled`` is the one case the purge is skipped. A hard cancel is the container
        going down, and the purge polls ``is_refreshing`` for up to
        ``_PLEX_SETTLE_ATTEMPTS * _plex_settle_delay`` PER affected section before it can
        even decide -- so honoring it here would hold shutdown open for tens of seconds
        inside the cancellation, and might empty a section's trash while the process is
        being torn down. The state commit above still runs, so the run ends ABORTED and
        recorded; only the cosmetic tidy-up is deferred, to Plex's own scheduled scan or to
        the next run over the same section.
        """
        # Drained BEFORE the terminal write and sent in a commit of their own, never bundled
        # with it. The terminal write is the one with no second chance, and a replay statement
        # that failed for its own reasons would otherwise take it down and re-open the wedge.
        pending = list(self._unwritten)
        self._unwritten.clear()
        await self._commit_journal(
            what="the run's final state",
            write=[
                update(ReapRun)
                .where(ReapRun.id == run_id)
                .values(
                    state=terminal.state,
                    aborted_reason=terminal.aborted_reason,
                    finished_at=terminal.finished_at,
                )
                .execution_options(synchronize_session=False)
            ]
            if terminal is not None
            else [],
        )
        if pending:
            # Second and last try for the journal rows whose own commit did not land. The run
            # is over, so nothing else writes these rows and replaying them cannot race; what
            # it buys is a ``file_removed_at`` that survives, because a file gone with no
            # stamp is bytes the rolling 30-day budget never charges and a later run spends
            # past what the operator set (rule 5/30).
            await self._commit_journal(what="the journal writes that did not land", write=pending)
        if canceled:
            if self._affected_sections:
                log.info("reap.trash_purge_deferred", sections=len(self._affected_sections))
            return
        if self._affected_sections:
            await self._finalize_plex()

    async def _check_rolling_caps(self, deletes: Sequence[_Delete]) -> None:
        """The 30-day budget: past verified deletions plus THIS run must fit both rolling
        caps, or the run ABORTS before it starts.

        Abort, never truncate, for the same reason as the per-run caps: a truncated run
        makes what gets deleted depend on sort order. This is the enforcement that keeps
        the configured monthly budget out of reach -- no sequence of runs is admitted past
        it, because each run is admitted only if the whole of it still fits. Exact in
        ITEMS. In BYTES it is a close bound and not an equality, because the sizes summed
        here are what the *arr tracks and a movie delete removes the whole folder
        (``ProfileSettings``'s caps comment carries the measured margin, #317).
        Checked in dry run too, so the simulation proves the same refusal a real run
        would hit. Skipped entirely while ``caps_enabled`` is off: the rolling budget is a
        run-size cap like the others, so the one switch governs it too.
        """
        if not self._settings.caps_enabled:
            return
        allow_unmeasured = self._settings.max_unmeasured_per_run > 0
        deletable = _deletable(deletes, self._effective_keys, allow_unmeasured=allow_unmeasured)
        items = len(deletable)
        total_bytes = _deletable_bytes(deletable, allow_unmeasured=allow_unmeasured)
        past_items, past_bytes = await self._rolling_30d_deletions()

        if past_items + items > self._settings.max_items_per_30d:
            raise ExecutionError(
                f"This run would delete {items} items on top of the {past_items} already "
                f"deleted in the last 30 days, over the rolling cap of "
                f"{self._settings.max_items_per_30d}. It stops rather than deleting just part. "
                "Wait for the window to pass, raise the cap, or turn limits off in Policy, "
                "under Pace and limits."
            )
        if past_bytes + total_bytes > self._settings.max_bytes_per_30d:
            raise ExecutionError(
                f"This run would delete {total_bytes / 1024**3:.0f} GB on top of the "
                f"{past_bytes / 1024**3:.0f} GB already deleted in the last 30 days, over "
                f"the rolling cap of {self._settings.max_bytes_per_30d / 1024**3:.0f} GB. It "
                "stops rather than deleting just part. Wait for the window to pass, raise the "
                "cap, or turn limits off in Policy, under Pace and limits."
            )

    async def _rolling_30d_deletions(self) -> tuple[int, int]:
        """What real runs verifiably deleted in the trailing 30 days: (items, bytes).

        Counted over terminal delete steps whose file is gone -- joined back to their
        frozen candidate rows for sizes, whatever state their run ended in: a crashed or
        aborted run's verified deletions are still deletions, and leaving them out would
        let repeated aborts spend past the budget.

        "Gone" is VERIFIED **or** ``file_removed_at`` set, and the two are deliberately not
        the same question. A movie Radarr really did delete, whose import exclusion never
        appeared inside the poll window, ends FAILED -- correctly, the verification failed
        -- but its bytes are off disk all the same. Counting state alone let every such
        item spend nothing, so an intermittently slow Radarr bought unlimited deletions
        while the cap check reported a number it knew to be short. The stamp records the
        removal independently of the verification's outcome; the ``OR`` counts each row
        once, whichever signal it carries (rule 5/30).

        An unmeasured past deletion is a normal outcome once the operator's allowance is
        above zero, and it is permanent: the candidate row keeps its NULL forever, so
        every later run sees it again. It therefore counts as an ITEM -- it was deleted,
        and the rolling item cap is what bounds that population -- and contributes no
        bytes, because there are none to contribute.

        Aborting on it instead would be the wrong kind of fail-closed: one use of the
        allowance would make every subsequent run, dry runs included, refuse for thirty
        days with no way out. Skipping the ROW would be the dangerous kind, since the
        trailing window would read light and spend past the budget. Counting the item and
        omitting its bytes is the only reading that is neither.
        """
        cutoff = utcnow() - timedelta(days=30)
        sizes = (
            (
                await self._session.execute(
                    select(Candidate.size_bytes)
                    .select_from(ActionStep)
                    .join(ReapRun, ReapRun.id == ActionStep.run_id)
                    .join(
                        Candidate,
                        (Candidate.snapshot_id == ReapRun.snapshot_id)
                        & (Candidate.media_key == ActionStep.media_key),
                    )
                    .where(
                        ActionStep.kind.in_(_TERMINAL_DELETE_KINDS),
                        or_(
                            (ActionStep.state == StepState.VERIFIED)
                            & (ActionStep.verified_at >= cutoff),
                            ActionStep.file_removed_at >= cutoff,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        unmeasured = sum(1 for size in sizes if size is None)
        if unmeasured:
            # Not an alarm on its own -- the allowance produces these deliberately -- but
            # the monthly BYTE budget is knowingly incomplete while they are in the window,
            # so it is recorded rather than passed over in silence.
            log.info("executor.rolling_window_unmeasured", items=unmeasured, window_days=30)
        return len(sizes), sum(size for size in sizes if size is not None)

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
        # The progress denominator is the set this run will ACT on -- the same set
        # ``_planned_candidates`` counts for the confirmation phrase and the caps count for
        # enforcement (rule 30/62). ``deletes`` is deliberately wider: an item spared after
        # the plan was built keeps its steps so the report can say it was kept, and counting
        # those would flip the bar mid-run to a number LARGER than the one the operator typed
        # to authorize, disagreeing with the header in the same window. They are still walked
        # and still reported; they just land in ``report.skipped`` (which the UI shows beside
        # the bar as "spared") rather than in the denominator.
        acted_on = {
            d.candidate.media_key
            for d in _deletable(
                deletes,
                self._effective_keys,
                allow_unmeasured=self._settings.max_unmeasured_per_run > 0,
            )
        }
        total = len(acted_on)
        done = 0
        for index, delete in enumerate(deletes):
            # The mid-run kill switch, re-read before EVERY item (the first included --
            # the route's own check is seconds stale by now). Aborting mid-list is safe
            # and honest: every earlier item's marks are already committed, and the
            # abort reason tells the owner exactly why the rest were not touched.
            if not self._dry_run and not await self._still_armed():
                raise ExecutionError(
                    "Deletion was turned off while this run was in progress, so the run "
                    "stopped here. Anything already deleted stays deleted; nothing "
                    "further was sent."
                )
            # The explicit Stop, checked in the same breath and the same fail-graceful way:
            # a graceful halt through an ExecutionError, so execute()'s abort path runs and
            # -- crucially -- still tidies Plex for whatever was already removed.
            if not self._dry_run and await self._stop_requested():
                raise ExecutionError(
                    "You stopped this run, so it halted here. Anything already removed "
                    "stays removed; nothing further was sent."
                )
            # The owner's overrides, re-read before EVERY item of a real run. Loading them
            # once at run start meant a Spare clicked while a long reap was in flight was
            # invisible to it and the file was deleted anyway -- with Stop the only mid-run
            # control that actually worked. Used ONLY by the two per-item checks below; the
            # caps stay on the run-start set (they must be fixed), and the run-start
            # effective set stays the ceiling, so a refresh can only ever REMOVE items from
            # what is sent, never add one the operator never authorized.
            if not self._dry_run:
                await self._refresh_overrides()
            # Read while the rows still answer, for the handler below: a journal halt unwinds
            # through a session whose objects a rollback has expired, so nothing down there
            # may touch the ORM. ``_file_is_gone`` is per item and belongs to this one.
            media_key, kind = delete.terminal.media_key, delete.terminal.kind
            title = delete.candidate.title
            self._file_is_gone = False
            try:
                outcome = await self._one_delete(
                    delete, is_canary=index == 0, approved_at=approved_at
                )
            except _JournalWriteError:
                # The item is recorded even though its journal is not, and this is the only
                # place that can do it: the halt fires from inside the send, so no
                # ``StepOutcome`` was ever built and the walk below never runs. Leaving it out
                # showed the operator "0 souls reclaimed" beside an abort reading "Anything
                # already removed stays removed", with nothing naming the file that went; and
                # a removal missing from the report leaves ``library_changed`` False, so the
                # post-run rescan never fires and the queue keeps offering a file that is gone
                # (rule 111).
                report.record(
                    StepOutcome(
                        media_key=media_key,
                        kind=kind,
                        state=StepState.FAILED,
                        detail=_JOURNAL_HALT,
                        title=title,
                        checks=[StepCheck(_JOURNAL_HALT, False)],
                        file_removed=self._file_is_gone,
                    )
                )
                raise
            # The rest of this iteration works off ``outcome`` and this local, never off
            # ``delete.candidate`` or a step, because the commit below may have to roll the
            # session back to recover -- and a rollback expires every ORM object, turning the
            # next attribute read into a MissingGreenlet rather than a reload. ``outcome``
            # already carries the media key and the title as plain strings; the approved size
            # is the one figure it does not, so it is read here while the row still answers.
            approved_size = delete.candidate.size_bytes
            journalled = True
            if not self._dry_run:
                # Every item's step marks (VERIFIED, FAILED, SKIPPED) are made durable
                # before the next item is attempted. A crash mid-run must never roll back
                # the record of files that are already gone.
                #
                # Read off the rows FIRST, as plain values. Recovering from a failed commit
                # means a rollback, and a rollback expires every one of these ORM objects --
                # so what gets replayed has to be captured while they can still be read. The
                # capture is also what carries a ``file_removed_at`` whose own commit was the
                # one that failed: without it the record that a file is gone is lost while
                # the file stays gone, those bytes drop out of ``_rolling_30d_deletions``,
                # and a later run spends past the operator's budget (rule 5/30).
                rows = [_JournalRow.of(step) for step in delete.steps]
                journalled = await self._commit_journal(
                    what=f"the journal for {outcome.media_key}",
                    write=[row.replay() for row in rows],
                )
            # Recorded BEFORE any halt below. This used to be an unguarded commit, so a
            # failed one raised past here and the item it had just acted on was missing from
            # the report entirely -- the operator was shown a bare error instead of the
            # account of what happened, which is exactly the record rule 111 exists to keep.
            report.record(outcome)
            if outcome.media_key in acted_on:
                done += 1
            # A per-item trace of the run's decisions -- verified, skipped or failed -- so a
            # reap can be followed line by line with Debug on. At debug, not info: a capped
            # run is bounded, but a big first cleanup still lists many items, and the failures
            # and the run summary carry their own louder lines.
            log.debug(
                "reap.item",
                media_key=outcome.media_key,
                title=outcome.title,
                state=outcome.state.value,
                dry_run=self._dry_run,
            )

            if outcome.state == StepState.SKIPPED:
                report.skipped += 1
                self._emit_progress(done, total, report, outcome.title)
                if not journalled:
                    raise ExecutionError(_JOURNAL_HALT)
                continue  # a skip touched no file, so it does not consume the canary

            # From here the item was really acted on (verified or failed).
            first_real_attempt = not real_attempt_made
            real_attempt_made = True

            if outcome.state == StepState.VERIFIED:
                report.deleted_items += 1
                # An item the allowance admitted is deleted with no size to add, so the
                # reclaim figure covers what was measured and the count beside it covers
                # everything. They describe different sets by necessity, and the report
                # carries the difference rather than letting the byte figure imply it is
                # the whole story.
                if (freed := approved_size) is not None:
                    report.deleted_bytes += freed
                else:
                    report.deleted_unmeasured += 1

            # Emit AFTER this item's counters update, so the polled status shows the real
            # running tally, and BEFORE the canary halt below, so a failed test item's count
            # is reported before the run aborts.
            self._emit_progress(done, total, report, outcome.title)

            # A journal Reaper could not write stops the run wherever it happens, ahead of
            # the canary and unclassified-failure halts below, because it is the more basic
            # failure: those two describe what a delete did, and this one says the record of
            # what it did is not on disk. Carrying on would delete more files that also could
            # not be recorded, and an unrecorded removal is one the rolling budget never
            # charges (rule 5/30).
            if not journalled:
                raise ExecutionError(_JOURNAL_HALT)

            if outcome.state == StepState.FAILED and first_real_attempt:
                # The canary -- the first real deletion -- misbehaved. Halt the whole run:
                # a plan whose first, smallest, safest delete does not behave as predicted
                # is a plan we do not understand.
                log.warning(
                    "reap.canary_failed",
                    media_key=outcome.media_key,
                    title=outcome.title,
                    detail=outcome.detail,
                )
                raise ExecutionError(
                    f'The first item, the test item ("{outcome.title}"), did not '
                    f"finish the way Reaper expected: {outcome.detail}. Stopping now, "
                    "before anything else is touched."
                )
            # A later item failing is recorded and survivable: one stubborn item is not a
            # reason to abandon the rest, and the canary already proved the mechanism works.
            # Unless Reaper could not tell what the failure WAS. Raised here rather than out
            # of the send so this item is journalled, recorded in the report and counted in
            # the progress tally first: the operator has to be able to read what happened up
            # to the surprise, and an item whose file went before it failed still has to
            # reach ``removed_unconfirmed`` and fire the post-run rescan.
            if outcome.halts_run:
                log.warning(
                    "reap.unclassified_item_failure",
                    media_key=outcome.media_key,
                    title=outcome.title,
                    detail=outcome.detail,
                )
                raise ExecutionError(
                    f'The run stopped at "{outcome.title}". Reaper could not tell '
                    "what went wrong there, so it did not touch anything after it. Anything "
                    "already removed stays removed. The details are in the run's own list "
                    "and in the log."
                )

    async def _refresh_overrides(self) -> None:
        """Re-read the owner's spare/reap decisions, mid-run, before the next item.

        A 200-item reap takes minutes, and the grace window is there so the owner may
        change their mind inside it. They watch the bar, see a title they want to keep,
        click Spare in the review queue -- and that decision has to reach this run.

        A plain re-query is enough: ``whitelist.overrides`` is a two-column Core select, not
        an ORM entity load, so the identity-map staleness that forced ``armed_recheck`` onto
        a fresh session does not apply here, and the run session commits after every item,
        so it sees another session's committed rows.

        Fail-closed. If the decisions cannot be read we cannot prove the next file is still
        one the owner wants gone, so the run stops rather than falling back on a map that
        may be minutes old. (Everything already deleted keeps its committed marks; this only
        refuses what has not been sent.)
        """
        try:
            self._decisions = await whitelist.overrides(self._session)
        except Exception as exc:
            log.warning("reap.override_recheck_unreadable", error=str(exc))
            raise ExecutionError(
                "Reaper could not re-check your keep and remove decisions, so the run "
                "stopped here rather than risk deleting something you just kept. Anything "
                "already deleted stays deleted; nothing further was sent."
            ) from exc

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
        # item by hand after the plan was built, which is what the grace countdown invites
        # (this executor never reads that window -- see services.grace), and a frozen candidate
        # row still reads ``condemn``, so this is the check (not the verdict, not the manifest
        # hash) that keeps a hand-spared file.
        if whitelist.effective_override(candidate.media_key, self._decisions) == "spare":
            return self._mark_skipped(
                delete,
                "you spared this by hand, so it is kept even though it was in the plan",
                check="You spared this by hand. Kept.",
            )

        # The mirror case: an item that was in the plan only because of a hand reap, whose
        # override has since been removed. It is no longer in the effective set, so it is
        # kept -- visibly, not silently dropped from the report.
        #
        # Both halves are consulted, and an item must pass BOTH. ``_effective_keys`` is the
        # set as it stood when the run was claimed -- the ceiling, so a reap added mid-run
        # cannot smuggle in an item outside what the operator confirmed. ``effective_verdict``
        # is the same production function ``effective_condemned`` decides membership with
        # (rule 3/22), applied to the freshly re-read decisions, so a reap withdrawn DURING
        # the run also drops its item. Intersecting the two makes the set only ever shrink.
        if candidate.media_key not in self._effective_keys or (
            effective_verdict(candidate, self._decisions) != "condemn"
        ):
            return self._mark_skipped(
                delete,
                "the hand reap on this was removed, so it is kept",
                check="No longer marked to reap by hand. Kept.",
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

        # A real send. Each step is marked SENT and COMMITTED *before* its guarded call,
        # and VERIFIED (committed again) only after the world is re-read -- so a crash
        # mid-item leaves a durable audit record of exactly what was in flight, and a 200
        # is never mistaken for proof. That record is not yet auto-reconciled: a crashed
        # (EXECUTING) run is re-planned from a fresh scan, not resumed -- execute() refuses
        # anything but a PLANNED run.
        return await self._send_for_real(delete, is_canary=is_canary)

    # -- live pre-delete interlocks: read-only, fail-closed ----------------

    async def _still_armed(self) -> bool:
        """Is deletion still turned on, right now?

        Read through the injected ``armed_recheck`` -- a fresh look at the same switch
        the UI toggles -- never this run's cached snapshot. Fail-closed: if the switch
        cannot be read, we cannot claim permission to continue, so the answer is no and
        the run stops. (The files already deleted are not affected; this only refuses
        the next one.)
        """
        if self._armed_recheck is None:  # pragma: no cover - execute() guards this
            return False
        try:
            return bool(await self._armed_recheck())
        except Exception as exc:
            log.warning("reap.arm_recheck_unreadable", error=str(exc))
            return False

    async def _stop_requested(self) -> bool:
        """Did the operator press Stop, from the reap window or the follow-you bar?

        Re-read before every item, like the arm switch. This is a convenience, not a safety
        interlock: the arm-recheck above is the fail-closed guard, so an unreadable stop flag
        resolves toward *continuing* (return False) rather than halting a healthy run on a
        transient blip. Stopping still keeps every file it has not reached, and a genuine
        Stop sets a durable flag that the next read sees.
        """
        if self._stop_recheck is None:
            return False
        try:
            return bool(await self._stop_recheck())
        except Exception as exc:  # pragma: no cover - the injected read is an in-memory flag
            log.warning("reap.stop_recheck_unreadable", error=str(exc))
            return False

    def _emit_progress(self, done: int, total: int, report: RunReport, title: str) -> None:
        """Hand the running tally to the injected progress sink, after each item.

        No-op unless a sink was injected (the API's polled status). A failure in the sink
        must never derail a deletion, so it is caught and logged -- reporting is strictly
        secondary to the run.
        """
        if self._progress is None:
            return
        try:
            self._progress(
                ReapProgress(
                    done=done,
                    total=total,
                    deleted_items=report.deleted_items,
                    deleted_bytes=report.deleted_bytes,
                    skipped=report.skipped,
                    title=title,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive; progress is never load-bearing
            log.warning("reap.progress_emit_failed", error=str(exc))

    def _equivalent_keys(self, candidate: Candidate) -> list[int]:
        """Every Plex rating key this candidate's watch evidence lives under.

        Usually just ``plex_rating_key``. A merged bind (one file listed several times in
        Plex, ``matched_by = merged_listings``) recorded every listing's key in its
        explanation's match block, and the live interlocks must consult all of them: a
        play or an active stream through the file's other listing is a play of the very
        file this delete would remove. Read back from the stored explanation, so the keys
        the interlocks protect are exactly the keys the owner was shown. An explanation
        without the list (every non-merged bind, and every snapshot from before merging
        existed) falls back to the single key, which is the pre-merge behavior.
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

        Fail-closed at every step: a Tautulli error, a success response whose history body
        cannot be read, and a returned row we cannot read a timestamp from all spare the
        item. A row present but unreadable survived the ``after`` filter, so it may well be
        a post-approval play -- we do not delete on the assumption that it was not. Only a
        genuine list of rows, none of them at or after the approval instant, returns False.
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
                # A body we cannot read is NOT "nobody played it". The client only raises
                # when the envelope reports failure, so a success response carrying a null
                # or unrecognized ``data`` arrives here having raised nothing -- and
                # coercing it to ``[]`` fell straight through to ``return False``, the
                # value that proceeds with the delete. That made a genuine empty, a null
                # body and an unknown shape indistinguishable, and the after-action
                # checklist then printed _CHECK_NOT_PLAYED_SINCE for a check that saw no
                # data at all. Rules 1, 28 and 93: unreadable is Unknown, never a definite
                # empty. Spare, exactly as the ``except`` above does.
                log.warning(
                    "reap.watched_since_unreadable",
                    media_key=candidate.media_key,
                    error="Tautulli returned a history body Reaper could not read",
                )
                return True
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
            return self._fail(delete, f'could not route "{candidate.media_key}": {exc}')

        def failed(reason: str, *, halts_run: bool = False) -> StepOutcome:
            """Fail this item, reading whether its file is already gone off the journal.

            The ``file_removed_at`` stamp is committed the moment a delete is confirmed
            (:meth:`_mark_file_removed`), which is BEFORE the exclusion poll and the Plex
            refresh that can each end the item FAILED. None of the handlers below used to
            consult it, so a run whose items all deleted but failed afterwards reported
            ``library_changed`` False, skipped the post-run rescan, and left the queue
            offering files that were no longer on disk.
            """
            return self._fail(
                delete,
                reason,
                file_removed=delete.terminal.file_removed_at is not None,
                halts_run=halts_run,
            )

        try:
            if ref.kind == "radarr":
                return await self._send_movie(delete, ref, is_canary=is_canary)
            if ref.kind == "sonarr" and ref.season is not None:
                return await self._send_season(delete, ref, is_canary=is_canary)
        except IntegrationError as exc:
            # A hard client/transport error on this item. Recorded and (unless it is the
            # canary) survivable -- the run continues with the others.
            return failed(f"the *arr call failed: {exc}")
        except SafetyViolationError as exc:
            # The transport guard refused the mutation mid-send. The execute route hands
            # its own RuntimeSafety snapshot to build_reap_gateway, so the guard and this
            # executor decide from one switch state -- but if they ever disagreed, a clean
            # failed item (the canary aborts the run) is far better than a crash that
            # leaves the run in an unknown state.
            return failed(f"the transport guard blocked the delete: {exc}")
        except ExecutionError as exc:
            # A missing instance route. Same treatment: fail this item, not the world.
            return failed(str(exc))
        except _JournalWriteError:
            # Straight past every handler here, including the catch-all below. See the class:
            # they all answer by recording an outcome, and this is the failure to record.
            raise
        except Exception as exc:
            # The catch-all, one level below execute()'s. A surprise here can land AFTER the
            # file is already gone (a raw transport error out of a client on the re-read that
            # follows the delete), and letting it escape left the terminal step SENT forever
            # and the whole run wedged in EXECUTING with no report -- so the operator could
            # not even see which files had been removed. Funnelling through ``_fail`` marks
            # the step FAILED with the reason. Whatever was already stamped
            # ``file_removed_at`` still counts against the rolling budget.
            #
            # It also HALTS the run (``halts_run``), which the wedge fix originally traded
            # away: with only the canary rule left, an unclassifiable failure at item 2 of 50
            # let items 3..50 delete anyway. The three handlers above are known shapes and
            # stay survivable; this one is by definition a shape Reaper does not recognize,
            # and on the deletion path that resolves toward stopping.
            log.warning("reap.item_unexpected_error", media_key=candidate.media_key, error=str(exc))
            return failed(f"an unexpected error stopped this item: {exc}", halts_run=True)

        return failed(f'no live delete path for "{candidate.media_key}" ({candidate.media_type})')

    async def _send_movie(self, delete: _Delete, ref: MediaRef, *, is_canary: bool) -> StepOutcome:
        """Delete a movie and, when the operator armed it, PROVE the import exclusion landed.

        Whether to add the exclusion is the target Radarr's own setting, frozen into the
        plan the operator approved and read back here (:func:`_planned_add_exclusion`) so the
        send matches the preview. When it is ON, the tmdbId is read *before* the delete (it
        cannot be read after, the movie is gone) and the exclusion list is re-read after,
        because Radarr returns 200 for the delete whether or not the exclusion took -- a
        missing exclusion is a verification failure, not a success, since re-requesting the
        title would silently re-download it. When it is OFF, there is no exclusion to add or
        prove: the delete needs no tmdbId and the item is done once the file is gone.
        """
        radarr = self._gateway.radarr_for(ref.instance_id)  # type: ignore[union-attr]
        step = delete.terminal  # the sole radarr_delete step

        # Reaching here means the two live interlocks already passed for this item.
        checks = [StepCheck(_CHECK_NOT_WATCHING, True), StepCheck(_CHECK_NOT_PLAYED_SINCE, True)]

        # Fail CLOSED on an approved size that was never confirmed, BEFORE the growth
        # check below: the growth check compares against the approved number and cannot
        # police it (``_grew_materially(0, live)`` is just ``live > 256 MiB``).
        # ``_may_send_unmeasured`` is STRICTLY STRONGER than ``size_confirmed`` -- it also
        # requires the frozen size to be one ``_MOVIE_COMPARABLE`` admits (``_measures``) --
        # and it is a shared method, not inline. Do not collapse it to ``size_confirmed``:
        # that drops the comparable-quantity check, which is what stops a file-only bound
        # (``RADARR_FILE``, ``PLEX``) from being weighed against Radarr's folder read.
        candidate = delete.candidate
        approved_size = candidate.size_bytes
        if not self._may_send_unmeasured(candidate, _MOVIE_COMPARABLE):
            return self._mark_skipped(
                delete, _NO_APPROVED_SIZE_REASON, check=_NO_APPROVED_SIZE_CHECK
            )

        # Read the tmdbId now, while the movie still exists, for the exclusion re-read.
        movie = await radarr.movie_by_id(ref.arr_id)

        # The size re-read: the approval, the caps and the typed phrase all counted the
        # frozen size, so a file that has grown materially since then (a quality upgrade)
        # is not the file the owner approved deleting. Skipped, kept, re-reviewable at
        # its real size after the next scan. An unreadable size is drift we cannot rule
        # out, so it is kept too.
        live_size = _payload_size(movie.get("sizeOnDisk"))
        # An item the allowance admitted has no frozen size to compare against, so the
        # growth interlock simply cannot run for it. That is exactly the protection the
        # owner traded away by raising the limit, and it is why the limit is a count: the
        # byte caps cannot bound this item either.
        if live_size is None and approved_size is not None:
            return self._mark_skipped(
                delete,
                "Radarr did not report this movie's current size, so Reaper cannot "
                "confirm it is still the file that was approved. Kept.",
                check=_CHECK_SIZE_UNCONFIRMED,
            )
        if (
            approved_size is not None
            and live_size is not None
            and _grew_materially(approved_size, live_size)
        ):
            return self._mark_skipped(
                delete,
                f"the file is bigger now ({_gb(live_size)}) than when it was approved "
                f"({_gb(approved_size)}), so it was likely upgraded since the "
                "scan. Kept. Run a new scan to review it at its current size.",
                check=_CHECK_GREW_SINCE_APPROVED,
            )

        # The exclusion decision was frozen into the plan the operator approved
        # (planner._movie_steps) and is read back from the journal here, so the send matches
        # the preview even if this Radarr's setting changed since approval. OFF skips both
        # the exclusion itself and the checks that police it.
        add_exclusion = _planned_add_exclusion(step)

        tmdb_id = int(movie.get("tmdbId") or 0)
        if add_exclusion and tmdb_id == 0:
            # Fail CLOSED before anything is sent: with the exclusion armed but no tmdbId,
            # _exclusion_landed can never verify, so the delete would always end "not fully
            # confirmed" -- and by then the file would already be gone (worse when this item
            # is the canary). Only a concern when the exclusion is on; off, there is nothing
            # to verify, so a missing id does not block the delete.
            return self._fail(
                delete,
                "Radarr lists no TMDB id for this movie, so the import exclusion could "
                "never be verified after deleting. The file was kept. Fix the movie's "
                "identification in Radarr, then plan again.",
                checks=checks,
            )

        await self._mark_sent(step)
        await radarr.delete_movie(ref.arr_id, delete_files=True, add_exclusion=add_exclusion)

        # Verify the movie is actually gone -- always, and immediately (a deleted movie 404s).
        gone = await self._movie_is_gone(radarr, ref.arr_id)
        # Only a real 404 passes the check. An unreadable re-read (``None``) is not a pass:
        # nobody confirmed anything, and the checklist must not say they did (rule 7/24).
        proven_gone = gone is True
        # ``None`` means the re-read failed, so the file's fate is unknown. For everything
        # below that is the KEEP direction (fail the item, do not claim a verification), but
        # for the budget it is the opposite: an uncharged delete buys the next run more
        # room. So "not proven still present" is what charges, not "proven gone".
        assume_removed = gone is not False
        checks.append(StepCheck("Removed the file through Radarr", proven_gone))

        # Stamp the removal the moment it is proven, and BEFORE anything else that could
        # fail. Everything below -- the exclusion poll, the Plex refresh -- can end this item
        # FAILED or raise, and the file is off disk either way. The stamp is what charges
        # those bytes to the rolling 30-day budget regardless of how the step's state ends
        # up, so a Radarr that is slow to add exclusions cannot quietly buy unlimited
        # deletions (rule 5/30). Committed here, not at the end: an audit record of a
        # deleted file is not something to hold in a transaction (rule 26).
        if assume_removed:
            await self._mark_file_removed(step)

        # The exclusion is polled only when it was armed: Radarr adds it a moment after the
        # delete returns 200, so a single immediate read is a false negative. With it off,
        # there is no exclusion to prove and the item is done once the file is gone -- so
        # ``excluded`` is vacuously True and the after-check says the exclusion was skipped
        # by the operator's own choice rather than leaving a silent gap.
        if add_exclusion:
            excluded = await self._exclusion_landed(radarr, tmdb_id)
            checks.append(StepCheck("Import exclusion confirmed. It won't re-download", excluded))
        else:
            excluded = True
            checks.append(
                StepCheck("Import exclusion off for this Radarr, so none was added", True)
            )

        # Once the file is gone, tell Plex -- whatever the exclusion result. This is what
        # stops a stale entry lingering, and it must fire even when the exclusion check
        # failed (the file is still gone). Best-effort: never affects the item's verdict.
        # A merged bind lists this one file under several rating keys, so the purge's
        # count-delta gate is told WHICH Plex listings this delete removes. Handing over
        # the keys rather than their count is what lets the gate dedup a listing two
        # candidates both bound to (see ``_deleted_by_section``).
        if assume_removed:
            await self._best_effort_refresh(
                str(movie.get("path") or movie.get("folderName") or ""),
                plex_keys=self._equivalent_keys(delete.candidate),
            )

        if not (excluded and proven_gone):
            # Three different things went wrong here and they read differently to an
            # operator, so each says what it actually knows rather than printing a tuple of
            # internal flags. With the exclusion off ``excluded`` is always True, so the
            # first branch only fires on a movie that would not delete.
            if gone is False:
                reason = (
                    "Radarr accepted the delete but the movie is still there. Nothing was removed."
                )
            elif gone is None:
                reason = (
                    "Radarr accepted the delete, but Reaper could not reach it again to "
                    "confirm the file is gone. It is counted against your limits as removed."
                )
            else:
                reason = (
                    "The file was removed, but Reaper could not confirm the import "
                    "exclusion, so a re-request could download it again."
                )
            return self._fail(
                delete,
                reason,
                checks=checks,
                # The file is gone even though this item failed, so the library changed and
                # the queue is now stale: the post-run rescan has to fire.
                file_removed=assume_removed,
            )

        await self._mark_verified(step, {"tmdb_id": tmdb_id, "excluded": excluded, "gone": True})
        detail = (
            "deleted; import exclusion verified present"
            if add_exclusion
            else "deleted; import exclusion off for this Radarr"
        )
        return StepOutcome(
            media_key=step.media_key,
            kind=step.kind,
            state=StepState.VERIFIED,
            detail=detail + (" [canary]" if is_canary else ""),
            title=delete.candidate.title,
            checks=checks,
        )

    async def _movie_is_gone(self, radarr: MovieDeleter, movie_id: int) -> bool | None:
        """Did the movie really go? ``True`` gone, ``False`` still there, ``None`` unknown.

        Three answers, not two, because the two ends of "not True" want opposite handling.
        A 404 proves the delete took. A clean read that still finds the movie proves it did
        NOT: Radarr returned 200 and did nothing. But a timeout or a 502 on this re-read
        proves neither, and collapsing it into ``False`` claimed the movie was still present
        when nobody had looked -- and, worse, skipped the ``file_removed_at`` stamp, so bytes
        that really were reclaimed never reached the rolling 30-day budget (rules 97, 5/30).
        ``delete_movie`` already returned without raising by the time this runs, so the
        fail-closed reading of an unreadable verification is that the file went.
        """
        try:
            await radarr.movie_by_id(movie_id)
        except IntegrationError as exc:
            return True if exc.status == 404 else None
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
        for attempt in range(_EXCLUSION_POLL_ATTEMPTS):
            exclusions = await radarr.exclusions()
            if any(int(e.get("tmdbId") or 0) == tmdb_id for e in exclusions):
                return True
            if attempt < _EXCLUSION_POLL_ATTEMPTS - 1 and self._exclusion_poll_delay > 0:
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

        # The size re-read, BEFORE the unmonitor so a skip leaves the season untouched:
        # the season's live episode files, summed, against the frozen size the owner
        # approved. A season that grew materially (episodes upgraded, or new episodes
        # landed since the scan) is not what was approved, so it is kept; so is one
        # whose file sizes cannot all be read.
        candidate = delete.candidate

        # The approved side first, and fail CLOSED on it. The live-side refusal below
        # only fires when Sonarr cannot report a size NOW; it says nothing about the
        # frozen number, and the growth check cannot police that either
        # (``_grew_materially(0, live)`` is just ``live > 256 MiB``).
        # ``_may_send_unmeasured`` is STRICTLY STRONGER than ``size_confirmed`` -- it also
        # requires the frozen size to be one ``_SEASON_COMPARABLE`` admits (``_measures``)
        # -- and it is a shared method, not inline. Do not collapse it to ``size_confirmed``:
        # that drops the comparable-quantity check, which is what keeps a Plex or Radarr
        # measurement that reached a season row from being weighed against a sum of Sonarr
        # episode files. Both members the set DOES admit are that same sum -- see the note
        # on ``_SEASON_COMPARABLE``, and learning 14 for the measurement behind it.
        approved_size = candidate.size_bytes
        if not self._may_send_unmeasured(candidate, _SEASON_COMPARABLE):
            return self._mark_skipped(
                delete, _NO_APPROVED_SIZE_REASON, check=_NO_APPROVED_SIZE_CHECK
            )

        # Keyed by file id, never a bare list of sizes. The delete below re-resolves this
        # season's files from a SECOND, independent read, and the two have to describe the
        # same files for the growth check to mean anything. Summing an anonymous list let a
        # file that landed between the reads be deleted having never been weighed.
        measured: dict[int, int | None] = {
            int(f["id"]): _payload_size(f.get("size"))
            for f in await sonarr.episode_files(ref.arr_id)
            if f.get("id") is not None and _season_number(f) == ref.season
        }
        live_sizes = list(measured.values())
        # An EMPTY list is not a confirmation. `sum([])` is 0, which sails through the
        # growth check below and marks the step verified having proven nothing -- and
        # since the plan is ordered smallest-first, a zero-size season is exactly what
        # the canary lands on. Rule 1: an omitted answer is not an explicit empty one.
        # An item the allowance admitted has no frozen size to compare against, so the
        # growth interlock cannot run for it at all. The empty-list guard still applies:
        # a season with no files is not a season worth sending a delete for.
        if not live_sizes or (approved_size is not None and any(s is None for s in live_sizes)):
            return self._mark_skipped(
                delete,
                "Sonarr did not report a size for every file in this season, so Reaper "
                "cannot confirm it is still what was approved. Kept.",
                check=_CHECK_SIZE_UNCONFIRMED,
            )
        live_total = sum(size for size in live_sizes if size is not None)
        if approved_size is not None and _grew_materially(approved_size, live_total):
            return self._mark_skipped(
                delete,
                f"this season is bigger now ({_gb(live_total)}) than when it was approved "
                f"({_gb(approved_size)}), so its files likely changed since "
                "the scan. Kept. Run a new scan to review it at its current size.",
                check=_CHECK_GREW_SINCE_APPROVED,
            )

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
            # Warn: Sonarr returned 200 for the season-pass edit but it did not take -- the
            # exact 200-means-nothing footgun this sequence exists to catch -- so the season
            # is kept. This path returns its own outcome (not via _fail), so it warns here.
            log.warning(
                "reap.unmonitor_unverified",
                media_key=candidate.media_key,
                series_id=ref.arr_id,
                season=ref.season,
            )
            await self._mark_verified(unmonitor, {"unmonitor_sent": True})
            self._stage_step_failed(
                verify, "the season is still monitored after the unmonitor; not deleting files"
            )
            self._stage_step_skipped(delete_step, "unmonitor unverified")
            # The two staged writes above are left to ``_run_deletes``'s per-item commit,
            # which runs before the next item is attempted and can recover a failed one.
            # A bare commit here could not: nothing was deleted on this path, but a raise
            # out of it still killed the session's transaction and took the run's terminal
            # write with it.
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
        resolved = [
            int(f["id"])
            for f in files
            if f.get("id") is not None and _season_number(f) == ref.season
        ]
        # A file here that the size re-read never weighed arrived in the window between the
        # two reads -- a Sonarr import completing while the unmonitor round-tripped. The
        # growth check above cannot catch it: it only refuses growth PAST
        # ``_grew_materially``'s tolerance, so anything under that band used to pass the gate
        # and then be deleted anyway. That deletes a file the owner never approved and never
        # saw counted, and the run charges the frozen approved size, so its bytes are removed
        # and charged to nothing (rules 5/30/97). Fail closed on the whole season -- the same
        # answer the grew-since-approved branch gives, and what this method's docstring
        # already promises.
        unmeasured = [fid for fid in resolved if fid not in measured]
        if unmeasured:
            log.warning(
                "reap.season_files_arrived_after_sizing",
                media_key=candidate.media_key,
                series_id=ref.arr_id,
                season=ref.season,
                arrived=len(unmeasured),
            )
            # Says the season was left unmonitored, exactly as the files-vanished skip below
            # does. Both are reached AFTER the unmonitor took and was verified, so the season
            # is spared but not untouched: Sonarr has stopped grabbing for it, and an operator
            # told only "Kept" would never think to turn it back on. ``_mark_skipped`` rebuilds
            # the checklist as one line, so the verified unmonitor is not visible there either.
            return self._mark_skipped(
                delete,
                f"{len(unmeasured)} more file(s) landed in season {ref.season} while Reaper "
                "was working, so it is no longer what was approved. Nothing was deleted, and "
                "the season was left unmonitored. Run a new scan to review it as it is now.",
                check="It changed while Reaper was working. The season was left unmonitored.",
            )
        file_ids = resolved
        if not file_ids:
            # Nothing resolved. ``delete_episode_files([])`` is a documented no-op, so the
            # verification below would find nothing remaining and mark the season VERIFIED
            # -- claiming a deletion that never happened, and charging the whole approved
            # size against the rolling budget. The files went somewhere between the size
            # re-read above and this resolve (removed out of band, or re-numbered), and the
            # fail-closed reading of "no files" is a skip, not a confirmed delete. The
            # unmonitor already took and keeps its VERIFIED mark; only the delete is skipped.
            # Which is also why the copy below does not say "nothing was sent": the
            # unmonitor was sent, was verified, and is still in force. The skip is right;
            # telling the operator their season is untouched when Sonarr has stopped
            # tracking it is not.
            log.warning(
                "reap.season_files_vanished",
                media_key=candidate.media_key,
                series_id=ref.arr_id,
                season=ref.season,
            )
            return self._mark_skipped(
                delete,
                f"Sonarr no longer lists any file for season {ref.season}, so nothing was "
                "deleted. The season was left unmonitored.",
                check="No files left to remove. The season was left unmonitored.",
            )
        await self._mark_sent(delete_step)
        await sonarr.delete_episode_files(file_ids)

        # 3. Verify no file for this season remains.
        try:
            remaining = await sonarr.episode_files(ref.arr_id)
        except IntegrationError:
            # The twin of the movie path's unreadable re-read (``_movie_is_gone`` returning
            # ``None``), reached by a raise rather than a return because there is no 404 to
            # distinguish here. ``delete_episode_files`` already returned without raising, so
            # the files went; only the confirmation failed. Charge the bytes before failing
            # the item, or a Sonarr that is unreachable for the re-read buys the next run
            # unlimited room (rules 97, 5/30, and 72 for the twin).
            await self._mark_file_removed(delete_step)
            return self._fail(
                delete,
                f"Sonarr accepted the delete for season {ref.season}, but Reaper could not "
                "reach it again to confirm the files are gone. They are counted against "
                "your limits as removed.",
                checks=checks,
                file_removed=True,
            )
        still_there = [f for f in remaining if _season_number(f) == ref.season]
        checks.append(
            StepCheck(f"Deleted the season's {len(file_ids)} episode file(s)", not still_there)
        )
        if len(still_there) < len(file_ids):
            # At least one file really went, so the bytes are reclaimed and the rolling
            # budget must be charged whichever way the step below ends. Same reasoning as
            # the movie path: the removal is recorded independently of the verification.
            await self._mark_file_removed(delete_step)
        if still_there:
            return self._fail(
                delete,
                f"{len(still_there)} episode file(s) for season {ref.season} remain after "
                "the delete; not confirmed.",
                checks=checks,
                file_removed=len(still_there) < len(file_ids),
            )

        # The season's files are gone, so tell Plex -- the same nudge the movie path sends.
        # Scoped to the folder the deleted files actually sat in, NOT the series root.
        #
        # This used to pass ``series["path"]``, which rescans every season of the show. That
        # is a mutation in effect: ``refresh_path``'s docstring says why -- on a server set
        # to empty its trash after every scan, rescanning a path with missing files purges
        # them, inside Plex, where none of ``_finalize_plex``'s interlocks can see it. So
        # pruning one season made Plex destroy the library records (watch state, ratings,
        # collections) of any OTHER season of that show whose files had gone missing out of
        # band -- items this run never measured, never planned, and never showed the owner.
        # Rescanning only the pruned season's own folder keeps the blast radius equal to
        # what was approved. When the paths cannot be read we fall back to the old
        # whole-series scope rather than skipping the refresh, since a stale entry is
        # cosmetic and Plex will notice on its own scheduled scan either way.
        #
        # No ``plex_keys``, deliberately: a TV section counts SHOWS, and pruning a season
        # of a multi-season show removes no show from it. Passing one (what this did)
        # granted the trash purge an allowance of one section entry per season pruned, so a
        # run pruning N seasons authorized a shrink of up to N shows on the most dangerous
        # call in the application -- a shrink that could only have come from something OTHER
        # than this run, since our own prunes move that count by zero. The comment here
        # already said the gate should decline; now it does, because an empty key set fails
        # the ``expected <= 0`` guard in ``_trash_delta_is_ours``.
        #
        # Pruning a show's LAST season may well drop the show, and that purge is declined
        # too. That is the cost: a lingering "unavailable"
        # entry is cosmetic, and purging someone else's trashed items is not.
        series_path = str(series.get("path") or "")
        deleted = set(file_ids)
        scoped = _common_parent(
            [
                str(f.get("path") or "")
                for f in files
                if f.get("id") is not None and int(f["id"]) in deleted
            ]
        )
        # An odd path set must never WIDEN the rescan past the show it belongs to, so a
        # common parent that escapes the series folder is discarded rather than used.
        if scoped and series_path and not _path_within(scoped, series_path):
            scoped = ""
        await self._best_effort_refresh(scoped or series_path, plex_keys=())

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
            if _season_number(season) == season_number:
                monitored = season.get("monitored")
                return bool(monitored) if monitored is not None else None
        return None

    async def _capture_section_counts(self) -> None:
        """Record every Plex section's item count before anything is deleted.

        The baseline of the trash interlock: ``_finalize_plex`` refuses to purge a section
        that shrank by more than this run deleted under it, and it can only know that with
        a before-count. Best-effort and never fatal -- an unreadable count leaves no
        baseline, and no baseline means the purge is refused for that section. The purge
        is cosmetic; a wrong purge is a lost library, so any doubt lands on "do not purge".
        """
        gateway = self._gateway
        if gateway is None or gateway.plex is None:
            return
        try:
            sections = await gateway.plex.section_paths()
        except Exception as exc:
            log.warning("reap.section_counts_unreadable", error=str(exc))
            return
        for section in sections:
            self._section_titles[section.key] = section.title
            try:
                self._section_pre_counts[section.key] = await gateway.plex.item_count(section.key)
            except Exception as exc:
                log.warning("reap.section_count_unreadable", section=section.title, error=str(exc))

    async def _best_effort_refresh(self, arr_path: str, *, plex_keys: Sequence[int]) -> None:
        """Nudge Plex to rescan the deleted item's directory. Never fatal.

        Fires whenever the file is gone, so Plex learns the item is missing. When the
        *arr path sits inside a Plex section location, the refresh is path-scoped -- it can
        only affect items *under that path*, never the whole library, which is what makes
        the end-of-run trash purge safe. The refreshed section is remembered for that
        purge, along with ``plex_keys`` -- WHICH Plex listings this delete removes (a
        merged bind lists one file under several) -- which feeds the purge's count-delta
        gate. When the path cannot be mapped it does nothing and says so; the file is
        already gone and Plex will notice on its next scheduled scan regardless.

        The keys are unioned into the section's set rather than counted, so a listing two
        candidates both name contributes one allowance and not two (see
        ``_deleted_by_section``). Under-counting is impossible in the other direction: one
        rating key is one section entry.

        An empty ``plex_keys`` is a real and meaningful value: this delete removes no entry
        from the section's own count, so it grants the purge NO allowance and the
        count-delta gate declines for that section. A season prune of a show passes nothing
        for exactly that reason (a TV section counts shows). There is no default, so a new
        caller must state which listings it removes rather than inheriting an allowance.
        """
        gateway = self._gateway
        if gateway is None or gateway.plex is None or not arr_path:
            return
        try:
            sections = await gateway.plex.section_paths()
            for section in sections:
                self._section_titles.setdefault(section.key, section.title)
                for location in section.locations:
                    if _path_within(arr_path, location):
                        with declared_mutation():
                            await gateway.plex.refresh_path(section.key, arr_path)
                        self._affected_sections.add(section.key)
                        self._deleted_by_section.setdefault(section.key, set()).update(plex_keys)
                        return
            log.info("reap.refresh_unmapped", arr_path=arr_path)
        except Exception as exc:
            # Broad, because "never fatal" has to mean it. This runs immediately after a
            # file has been deleted, and a raw transport error out of plexapi (a Plex
            # restart between the connect and the read) used to escape a PlexError-only
            # handler, past _send_for_real and past execute(), leaving the terminal step
            # SENT and the whole run wedged in EXECUTING. Nothing here can affect the
            # item's verdict: the file is already gone, and a missed refresh costs a
            # lingering "unavailable" entry until Plex's own scheduled scan.
            log.warning("reap.refresh_failed", arr_path=arr_path, error=str(exc))

    async def _finalize_plex(self) -> None:
        """Purge the stale entries for the files we removed, so Plex's view stays honest.

        The single most dangerous call in the app (an unmounted library + a scan + an empty
        trash is how whole libraries vanish), so it is triply interlocked:

        * **The mount must be up.** Every deletion routed through an *arr whose root folder
          reports ``accessible``; if any does not, the volume may be gone and the trash is
          full of items that are merely *unreachable*, not deleted -- so we refuse to purge.
        * **Only path-scoped scans ran.** ``_best_effort_refresh`` rescans one directory at
          a time -- for a movie its own folder, for a season prune the folder its files sat
          in (``_common_parent``) -- so the only items Plex could have *freshly* trashed are
          the ones under the paths we deleted. Reaper never triggers a whole-section scan.
        * **The count-delta gate** (``_trash_delta_is_ours``). The section must have shrunk
          since the pre-delete baseline, and by no more than the distinct Plex listings this
          run deleted under it. A larger shrink means something else -- a mount flap on the
          Plex host, a nightly scan -- trashed items that are NOT ours, and purging would
          destroy their library records (watch states, collection membership).

        **What these do NOT cover, stated plainly because the purge is section-wide.**
        ``empty_trash`` is ``PUT /library/sections/{key}/emptyTrash``: it empties that
        section's WHOLE trash, not the subset this run caused. Items already sitting in the
        trash when ``_capture_section_counts`` read the baseline are in the same state
        before and after, so they cancel out of ``pre - post`` and the count-delta gate
        cannot see them -- and this call then destroys their library records too. The gate
        bounds the *magnitude* of the shrink; it cannot attribute WHICH items account for
        it. So the honest claim is "one section, on a run whose shrink looked like ours",
        not "only our items". Scoping the purge to the items we actually deleted (per-item
        ``DELETE`` rather than ``emptyTrash``) is the real fix and is not done here.

        Any check failing skips the purge and logs it -- the reap already succeeded, and a
        lingering "unavailable" entry is a cosmetic problem, never a lost file. Never raises.
        """
        gateway = self._gateway
        if gateway is None or gateway.plex is None or not self._affected_sections:
            return

        if not await self._mount_is_up():
            log.warning("reap.trash_purge_skipped", reason="a root folder is not accessible")
            return

        for section_key in sorted(self._affected_sections):
            title = self._section_title(section_key)
            if not self._deleted_by_section.get(section_key):
                # Declined here rather than inside the gate, so a section whose count this
                # run cannot move does not first pay the settle wait. A TV-only run reaches
                # this for every section it touched (a TV section counts shows), and waiting
                # up to _PLEX_SETTLE_ATTEMPTS x _plex_settle_delay apiece for a purge that
                # can never pass the gate is time the operator waits for nothing.
                log.info(
                    "reap.trash_purge_declined",
                    section=title,
                    reason="this run removed no entries from this library's own count",
                )
                continue
            try:
                await self._wait_for_scan(gateway.plex, section_key)
                if not await self._trash_delta_is_ours(gateway.plex, section_key):
                    continue
                with declared_mutation():
                    await gateway.plex.empty_trash(section_key)
                log.info("reap.trash_purged", section=title)
            except Exception as exc:
                # Same reasoning as _best_effort_refresh: the docstring above promises this
                # never raises, and a PlexError-only handler did not deliver that. This runs
                # in execute()'s finally, where an escape would replace the run's real
                # outcome with a Plex error the operator can do nothing about.
                log.warning("reap.trash_purge_failed", section=title, error=str(exc))

    def _section_title(self, section_key: int) -> str:
        """What to call a section in a log line. The key is the address; the title is for
        the operator reading the line, and falls back to the key when we never saw one."""
        return self._section_titles.get(section_key) or f"section {section_key}"

    async def _trash_delta_is_ours(self, plex: PlexOps, section_key: int) -> bool:
        """Did this section shrink by what this run deleted under it, and no more?

        The confirmation the purge requires: ``pre - post`` must be at least 1 (Plex has
        actually noticed a removal) and at most the number of DISTINCT Plex listings we
        deleted there -- distinct because one listing two candidates both bound to can only
        move the section's count by one, however many candidates named it
        (``_deleted_by_section``). A shrink of zero means Plex has not confirmed OUR
        deletes either -- perhaps trashed
        items still count toward the section size on this server -- and purging without
        confirmation is exactly the mistake this gate exists to prevent, so it refuses and
        the trash is left to the owner or to Plex's own maintenance. Never raises.
        """
        title = self._section_title(section_key)
        pre = self._section_pre_counts.get(section_key)
        expected = len(self._deleted_by_section.get(section_key, ()))
        if expected <= 0:
            # Nothing this run deleted changes this section's own item count, so there is no
            # allowance to compare a shrink against and the purge is declined. The ordinary
            # case is a TV section: it counts shows, and pruning seasons of a show removes
            # none (see _send_season). Reported at INFO and in its own words, because it is
            # a normal outcome, not the "we could not read the baseline" failure below --
            # which is what this branch used to claim for every season prune.
            log.info(
                "reap.trash_purge_declined",
                section=title,
                reason="this run removed no entries from this library's own count",
            )
            return False
        if pre is None:
            log.warning(
                "reap.trash_purge_skipped",
                section=title,
                reason="no pre-delete item count to compare against",
            )
            return False
        try:
            post = await plex.item_count(section_key)
        except Exception as exc:
            log.warning(
                "reap.trash_purge_skipped",
                section=title,
                reason=f"could not re-read the item count: {exc}",
            )
            return False
        shrink = pre - post
        if shrink < 1 or shrink > expected:
            log.warning(
                "reap.trash_purge_skipped",
                section=title,
                pre=pre,
                post=post,
                deleted_here=expected,
                reason="the section did not shrink by exactly what this run deleted",
            )
            return False
        return True

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
            except Exception as exc:
                # Any failure at all, not just a mapped IntegrationError: this is the "is
                # the volume really mounted" proxy, so anything we cannot read is assumed
                # to be the worst case and the purge is refused.
                log.warning("reap.rootfolder_unreadable", error=str(exc))
                return False
            if not folders or not all(f.get("accessible") is True for f in folders):
                return False
        return True

    async def _wait_for_scan(self, plex: PlexOps, section_key: int) -> None:
        """Wait (bounded) for a section's scan to settle before emptying its trash.

        A refresh fires an asynchronous scan; emptying the trash before Plex has noticed the
        deleted file would purge nothing. Polls ``is_refreshing`` a few times; gives up after
        the window either way, since the purge is best-effort.
        """
        for attempt in range(_PLEX_SETTLE_ATTEMPTS):
            if not await plex.is_refreshing(section_key):
                return
            if attempt < _PLEX_SETTLE_ATTEMPTS - 1 and self._plex_settle_delay > 0:
                await asyncio.sleep(self._plex_settle_delay)

    # -- journal state transitions -----------------------------------------
    #
    # The three ``_mark_*`` helpers below COMMIT (rule 26). The two ``_stage_*`` ones after
    # them, and ``_fail`` and ``_mark_skipped``, only write attributes in memory; the
    # per-item ``_commit_journal`` in ``_run_deletes`` is what makes those durable. The line
    # matters, and it used to be invisible: every one of these was called a mark, so a
    # reader checking rule 26 against the group found durable-looking names over writes that
    # a rollback discards.

    async def _mark(self, step: ActionStep, what: str, **values: object) -> None:
        """Apply one step-state transition to the row and to the database, durably (rule 26).

        The values are set on the ORM object *and* carried in an explicit ``UPDATE``, out of
        one dict, so the in-memory row and the durable one cannot come to say different
        things. The statement is the half that survives: recovering from a failed commit
        means a rollback, and a rollback discards what was set on the row.

        Raises :class:`_JournalWriteError` when the write does not land even after that
        retry, which is the fail-closed direction at every call site -- see the class.
        """
        step_id = step.id
        landed = await self._commit_journal(
            what=what,
            write=[
                update(ActionStep)
                .where(ActionStep.id == step_id)
                .values(**values)
                .execution_options(synchronize_session=False)
            ],
        )
        if not landed:
            raise _JournalWriteError(_JOURNAL_HALT)
        # Applied to the row only once it is on disk, and never before: a rollback inside the
        # recovery would have discarded an earlier assignment, leaving the row disagreeing
        # with the database it just wrote. That disagreement is not cosmetic -- ``_JournalRow``
        # captures these rows and replays them, so a stale value here is written back OVER the
        # good one, which is how a recovered ``file_removed_at`` came out NULL. Any future
        # write that goes around the identity map owes the row the same courtesy.
        for key, value in values.items():
            setattr(step, key, value)

    async def _mark_sent(self, step: ActionStep) -> None:
        # COMMITTED, not merely flushed: the SENT mark is the durable declaration of
        # intent, written before the request leaves the process. If the process dies
        # between this commit and the verify, the journal still shows exactly which call
        # was in flight -- a flush inside a run-long transaction would roll back with it.
        #
        # It is also the one mark whose failure must stop the send: the caller dispatches
        # the delete on the next line, and ``_JournalWriteError`` unwinds past it, so an
        # intent that could not be journalled is never carried out.
        await self._mark(step, "sent", state=StepState.SENT, sent_at=utcnow())

    async def _mark_verified(self, step: ActionStep, verification: dict[str, Any]) -> None:
        await self._mark(
            step,
            "verified",
            state=StepState.VERIFIED,
            verified_at=utcnow(),
            verification_json=json.dumps(verification),
        )

    async def _mark_file_removed(self, step: ActionStep) -> None:
        """Record that this step's file is really gone, whatever the step's state ends up.

        Separate from ``_mark_verified`` on purpose: a movie whose delete succeeded but
        whose import exclusion never landed ends FAILED, and that state must keep saying so
        -- marking it VERIFIED would make the journal and the after-action report claim a
        verification that explicitly failed. The bytes are still reclaimed, so they are
        still charged: ``_rolling_30d_deletions`` reads this alongside VERIFIED.

        Committed immediately, like ``_mark_sent``: this is the durable record that a file
        is gone, and a flush inside the run-long transaction would roll back with a crash
        that happened after the file was already deleted (rule 26). It is also the write
        whose loss outlives the run -- a file gone with no stamp is bytes the rolling 30-day
        budget never charges (rule 5/30) -- so it gets the same rollback-and-replay as the
        rest rather than being left to a commit that may never come.
        """
        # Set BEFORE the write, and never conditional on it landing. Reaching this method at
        # all means the delete is confirmed and the file is off disk; that fact is true
        # whether or not it can be written down, and the journal-halt handler in
        # ``_run_deletes`` has no other way to learn it once every row is expired.
        self._file_is_gone = True
        await self._mark(step, "file removed", file_removed_at=utcnow())

    def _stage_step_failed(self, step: ActionStep, reason: str) -> None:
        """In memory only. Durable once ``_run_deletes`` commits this item's journal."""
        step.state = StepState.FAILED
        step.error = reason

    def _stage_step_skipped(self, step: ActionStep, reason: str) -> None:
        """In memory only. Durable once ``_run_deletes`` commits this item's journal."""
        step.state = StepState.SKIPPED
        step.error = reason

    def _fail(
        self,
        delete: _Delete,
        reason: str,
        checks: list[StepCheck] | None = None,
        *,
        file_removed: bool = False,
        halts_run: bool = False,
    ) -> StepOutcome:
        """Fail this item: set any not-yet-terminal step FAILED, and record why.

        Written in memory, like ``_mark_skipped`` and the two ``_stage_*`` helpers, and made
        durable by ``_run_deletes``'s per-item ``_commit_journal`` before the next item is
        attempted. Not a rule 26 exemption: rule 26 asks that the write be committed at each
        step, not that this function be the one to do it. What matters is that a rollback
        discards what is set here, which is why the per-item commit reads these rows into
        ``_JournalRow`` before committing them and can replay them if it has to.

        A step already VERIFIED (an unmonitor that took, say) keeps its state -- it really
        did happen. Only the steps that did not reach a terminal state are marked FAILED,
        so the journal reflects reality for a future reconciler. ``checks`` carries the
        after-action checklist (what got done, and which check failed); when absent, the
        reason itself is the single failed line.

        ``file_removed`` says this failure happened AFTER the file was already gone, which
        is a different thing from the failure itself: the step stays FAILED (its
        verification really did fail), but the run must still count the library as changed
        and kick the post-run rescan.

        ``halts_run`` says the failure was one Reaper could not classify, so the run stops
        after this item rather than carrying on to the next (see :class:`StepOutcome`).
        """
        # Every hard item failure -- a delete that could not be confirmed, a routing error,
        # an *arr call that raised -- funnels through here, so this is the one place to
        # surface it. At warning: during a real reap, a delete Reaper cannot confirm is
        # operator-actionable ("did it delete or not?"). The reason is already plain copy.
        log.warning(
            "reap.item_failed",
            media_key=delete.candidate.media_key,
            title=delete.candidate.title,
            detail=reason,
        )
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
            file_removed=file_removed,
            halts_run=halts_run,
        )

    def _may_send_unmeasured(self, candidate: Candidate, comparable: frozenset[SizeSource]) -> bool:
        """May this item be sent at all, on the evidence of its frozen size?

        Three answers, and only one of them is yes-with-a-comparison:

        * a size that measures the same thing the live re-read will -> send, and the
          growth interlock runs normally;
        * no size at all, with the operator's allowance open -> send, with the growth
          interlock unavailable. That is the protection they traded away deliberately, and
          it is why the allowance is a count: the byte caps cannot bound this item either;
        * anything else -> keep. No size and no allowance, or a size that measures a
          different quantity (one file where the live read sums every file), which would
          make an ordinary read look like growth.

        Read from settings HERE, at execute time, not from the plan. An operator who
        lowers the allowance to 0 between approval and execute gets those items kept;
        raising it after approval can add nothing, because they were never planned. Both
        directions resolve toward keeping the file, which is what makes a late read safe.
        """
        if candidate.size_bytes is not None:
            return _measures(candidate, comparable)
        if self._settings.max_unmeasured_per_run > 0:
            return True
        # A real alarm, not routine noise: the planner should have held this back, so this
        # firing means the two layers disagree and the first one is wrong.
        log.warning("executor.skipped_unmeasured", media_key=candidate.media_key)
        return False

    def _mark_skipped(self, delete: _Delete, reason: str, check: str | None = None) -> StepOutcome:
        """Spare the whole item. In a REAL run, set every one of its not-yet-terminal steps
        SKIPPED (not just the last) -- in memory, durable at the per-item commit, exactly as
        in ``_fail``: a season sparing that left its unmonitor step PENDING
        would read to a future reconciler as an interrupted run with work still to do. In a
        dry run, mutate nothing -- the simulation must leave the plan runnable -- and only
        report the skip.

        A step that already reached VERIFIED keeps its state, exactly as in ``_fail``: it
        really did happen, and overwriting it would make the journal deny a reversible edit
        that is still in force. Every skip decided before anything is sent leaves no such
        step, so this only bites where a season is spared part-way -- its unmonitor took,
        then Sonarr listed no file to delete.

        The checklist gets one ``✓`` line for the protection that fired: a spare is not a
        failure, it is a protection working, so it reads as a pass, not a cross.
        """
        if not self._dry_run:
            for step in delete.steps:
                if step.state == StepState.VERIFIED:
                    continue
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


def _path_within(path: str, location: str) -> bool:
    """Is ``path`` the section location itself, or genuinely inside it?

    A raw prefix test is not enough: a section rooted at ``/media/movies`` must never
    claim files under ``/media/movies-4k/``, or the refresh -- and worse, the end-of-run
    trash purge -- lands on the wrong section.
    """
    root = location.rstrip("/")
    return path in (root, location) or path.startswith(root + "/")


def _common_parent(paths: Sequence[str]) -> str:
    """The deepest directory holding every one of ``paths``. Empty string when there is none.

    Scopes the post-prune Plex rescan (:meth:`Executor._send_season`) to the season folder
    the deleted files actually sat in, instead of the series root. Rescanning a path is a
    mutation in effect on a server that empties its trash after every scan, so the narrower
    the path, the smaller the set of other people's library records Plex can destroy.

    Splits on ``/`` like :func:`_path_within`, which is the separator every *arr root folder
    in this codebase is expressed in. Returns "" rather than ``/`` when the paths share no
    real directory: the whole disk is not a scope, and the caller falls back instead.
    """
    dirs = [p.rsplit("/", 1)[0] for p in paths if p.startswith("/") and p.count("/") > 1]
    if not dirs:
        return ""
    parts = dirs[0].split("/")
    for other in (d.split("/") for d in dirs[1:]):
        keep = 0
        while keep < min(len(parts), len(other)) and parts[keep] == other[keep]:
            keep += 1
        parts = parts[:keep]
    common = "/".join(parts)
    # ``parts == [""]`` joins to "" and anything shallower than one real segment is root.
    return common if common.count("/") >= 1 else ""


def _row_timestamp(row: object) -> int | None:
    """The unix time a history row was played, from ``stopped`` (preferred) or ``date``.

    Tautulli rows carry both; ``stopped`` is when the view ended, which is the most
    conservative 'was this watched' signal.

    None means "no readable time", NOT "no play". The caller
    (:meth:`Executor._watched_since_approval`) treats it as a possible post-approval play
    and SPARES the item: the row was returned by a query already filtered to on-or-after
    the approval date, so a time we cannot read is a play we cannot rule out. Do not
    "repair" the caller to skip such rows -- that would silently disable the
    played-since-approval interlock for every Tautulli row with an unreadable timestamp.
    """
    if not isinstance(row, dict):
        return None
    for field_name in ("stopped", "date"):
        value = row.get(field_name)
        if not value:
            # None, 0 and "" all mean "no time recorded" -- Tautulli writes 0, not a real
            # epoch-1970 stamp, for a missing stop time. Fall through to the next field
            # rather than compare 0 against the approval time (which could never spare).
            continue
        try:
            ts = int(value)
        except (TypeError, ValueError):
            continue
        if ts > 0:
            return ts
    return None
