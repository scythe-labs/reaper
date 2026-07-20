# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turning condemned candidates into a plan Reaper can execute.

A plan is a ``ReapRun`` and its ordered ``ActionStep`` rows. Every step is written to
the database **before** anything is sent, carrying its exact method, path and body -- so
the record of intent exists prior to the act, and a crash mid-run leaves a durable audit
trail of exactly what was in flight. That trail is not yet consumed by an automated
reconciler: a partially-completed run is re-planned from a fresh scan rather than resumed
(the executor refuses to re-run anything but a PLANNED run), so the ``idempotency_key`` on
each step is recorded for a future recovery pass, not relied on for de-duplication today.

The planner does not talk to any *arr, and it cannot delete anything. It reads condemned
candidates and produces journal rows. Whether those rows are ever executed -- and whether
execution is even permitted -- is entirely the executor's concern.

Two ordering decisions are load-bearing:

* **The canary is ordinal 0: the single smallest condemned item.** It is executed and
  verified *alone* before anything else is allowed to proceed, so that a broken path
  mapping or a misunderstanding of an API costs one worthless file, not the whole run.

* **A movie deletion is more than one step**, and the order is not arbitrary:

      1. delete the movie file, with the import-exclusion flag set
      2. re-read the exclusion list and ASSERT the id is present -- because Sonarr and
         Radarr each accept the *other's* exclusion parameter and return 200 while doing
         nothing, so the 200 is not evidence
      3. refresh the affected Plex path (a partial scan, not a full re-download)

  Plex's ``emptyTrash`` is deliberately NOT a per-item step: it is a single guarded
  operation at the very end of the run, gated by an item-count delta check, because an
  unmounted library plus a scan plus emptyTrash is how whole libraries are lost.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clock import utcnow
from reaper.db.models import ActionStep, Candidate, ReapRun, RunState, Snapshot, StepState
from reaper.services import whitelist
from reaper.services.condemned import effective_condemned

log = structlog.get_logger(__name__)


class PlanError(RuntimeError):
    """A plan could not be built -- e.g. a candidate whose media_key we cannot route."""


@dataclass(frozen=True)
class MediaRef:
    """A media_key parsed back into the coordinates a delete needs.

    A movie or a whole series is three parts (``radarr:1:42``); a *season* is four
    (``sonarr:1:42:3`` -- series 42, season 3), because season pruning acts on a
    season, not a series.
    """

    kind: str  # "radarr" | "sonarr"
    instance_id: int
    arr_id: int
    season: int | None = None

    @classmethod
    def parse(cls, media_key: str) -> MediaRef:
        """``radarr:1:42`` -> movie; ``sonarr:1:42:3`` -> season 3 of series 42.

        A media_key we cannot parse is a hard error, never a skipped item. Silently
        dropping something from a deletion plan is safe; silently *mis-routing* it is
        not, and the difference between the two is a parse we did not check.
        """
        parts = media_key.split(":")
        if len(parts) not in (3, 4) or parts[0] not in ("radarr", "sonarr"):
            raise PlanError(f'Cannot route media_key "{media_key}" to an instance.')
        try:
            season = int(parts[3]) if len(parts) == 4 else None
            ref = cls(kind=parts[0], instance_id=int(parts[1]), arr_id=int(parts[2]), season=season)
        except ValueError as exc:
            raise PlanError(f'Malformed media_key "{media_key}": {exc}') from exc

        if ref.season is not None and ref.kind != "sonarr":
            # Only TV has seasons; a four-part radarr key is a mis-built id, and routing
            # it anywhere is worse than refusing it.
            raise PlanError(f'A season media_key must be sonarr, got "{media_key}".')
        return ref


def manifest_hash(candidates: Sequence[Candidate]) -> str:
    """A content-bound fingerprint of the frozen condemned set a plan was built from.

    The human approves *this*: the confirmation the UI shows ("REAP 7 ITEMS 214 GB") is
    derived from it, and the executor recomputes it before acting. Both sides hash the same
    immutable, per-snapshot candidate rows, so what this actually binds is the *integrity of
    the frozen set*: it changes if a condemned candidate row is lost or tampered with under
    a run (e.g. retention GC), which voids the approval. It is NOT live drift detection --
    candidate rows are frozen at scan time and nobody re-reads the *arr here, so a title
    deleted or resized in Radarr after approval does not change this hash; that live drift
    is caught by the executor's per-item interlocks and its existence and size re-reads at
    delete time, and a stale browser tab is stopped by the route's confirmation-phrase
    recompute.

    Over the media_key and size of each item, sorted so the order candidates arrive in
    cannot change the hash. An item Reaper could not measure encodes as JSON ``null``,
    which is distinct from ``0``: a size later measured therefore voids the approval, as
    it should, because the set the owner approved is not the set they would approve now.
    Sorted explicitly on the media_key, which is unique per snapshot, so a ``None`` size
    is never compared against an ``int``.

    This hashes the WHOLE condemned set, held-back items included. It binds the frozen
    set's integrity, not the plan.
    """
    payload = sorted(((c.media_key, c.size_bytes) for c in candidates), key=lambda p: p[0])
    canonical = json.dumps(payload, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def plan_bytes(candidates: Sequence[Candidate]) -> tuple[int, int]:
    """A plan's size, as (bytes over what was measured, how many were not).

    Two numbers rather than one, and never a sum with an unknown folded into it as a zero.
    A zero under-states, an under-stated total under-states a byte cap, and a cap that does
    not fire deletes more than the owner allowed. Note the direction: rounding toward
    keeping is right on the scoring lane and exactly backwards here.
    """
    measured = [c.size_bytes for c in candidates if c.size_bytes is not None]
    return sum(measured), len(candidates) - len(measured)


def confirmation_phrase(candidates: Sequence[Candidate]) -> str:
    """The typed confirmation, bound to the content.

    Not a static "DELETE": a phrase carrying the count and the size, so muscle memory
    cannot carry someone through it and a stale plan reads as obviously different.

    The GB figure covers the items that have a size, and it is never asked to absorb the
    ones that do not. When the allowance (``ProfileSettings.max_unmeasured_per_run``) has
    admitted any, the phrase gains a ``+ N UNSIZED`` suffix, so the owner types an
    acknowledgment that the run holds things the GB figure does not describe. With the
    allowance at its default the suffix never appears and the phrase is byte-identical to
    what it has always been.

    This string is recomputed at execute time (``api.runs.execute_run``) and compared to
    what was typed, so any change of wording here 409s every execute.
    """
    total, unsized = plan_bytes(candidates)
    gib = total / 1024**3
    phrase = f"REAP {len(candidates)} ITEMS {gib:.0f} GB"
    return f"{phrase} + {unsized} UNSIZED" if unsized else phrase


def _movie_steps(
    run_id: int, candidate: Candidate, ref: MediaRef, ordinal: int
) -> list[ActionStep]:
    """The steps to remove one movie: delete-with-exclusion, then verify, then refresh.

    ``path`` and ``body`` are recorded WITHOUT credentials -- the api key is injected by
    the client at send time -- so the journal is safe to render in the UI and to keep
    forever. ``idempotency_key`` is a stable per-step identifier written for a future
    recovery pass; it is NOT yet consumed to de-duplicate a resumed step (a
    partially-completed run is re-planned, not resumed), so nothing today relies on it to
    prevent a double delete -- the "executes once" guard and the un-repeatable delete do.
    """
    now = utcnow()
    idem = f"{run_id}:{candidate.media_key}"

    # Radarr's spelling. Sonarr's differs, and each silently ignores the other's -- so
    # the planner emits the correct one per kind rather than trusting one to work.
    delete = ActionStep(
        run_id=run_id,
        media_key=candidate.media_key,
        ordinal=ordinal,
        kind="radarr_delete",
        method="DELETE",
        path=f"/api/v3/movie/{ref.arr_id}",
        body_json=json.dumps({"deleteFiles": True, "addImportExclusion": True}),
        idempotency_key=f"{idem}:delete",
        state=StepState.PENDING,
        created_at=now,
    )
    return [delete]


def _season_steps(
    run_id: int, candidate: Candidate, ref: MediaRef, ordinal: int
) -> list[ActionStep]:
    """The steps to prune one season: unmonitor, verify the unmonitor, delete the files.

    The order is not cosmetic -- the two half-applied states are asymmetric.
    "Unmonitored, files intact" is benign and resumable; "files gone, still monitored"
    makes Sonarr re-download everything we just removed. So the file delete is last, and
    only after the unmonitor is *verified* (Sonarr accepts a season-pass edit and returns
    200 whether or not it took, exactly like the exclusion footgun on the movie side).

    The final delete records the season's coordinates rather than a frozen list of
    ``episodeFileIds``: the set of files in a season changes as Sonarr downloads, so the
    ids must be resolved against the live series immediately before the delete, not
    captured at plan time. That resolution -- and the whole multi-step live send -- is the
    supervised executor work still ahead, exactly as the movie live send is.
    """
    now = utcnow()
    idem = f"{run_id}:{candidate.media_key}"

    unmonitor = ActionStep(
        run_id=run_id,
        media_key=candidate.media_key,
        ordinal=ordinal,
        kind="sonarr_unmonitor",
        method="POST",
        path="/api/v3/seasonpass",
        body_json=json.dumps(
            {
                "series": [
                    {
                        "id": ref.arr_id,
                        "seasons": [{"seasonNumber": ref.season, "monitored": False}],
                    }
                ],
                "monitoringOptions": {"monitor": "none"},
            }
        ),
        idempotency_key=f"{idem}:unmonitor",
        state=StepState.PENDING,
        created_at=now,
    )
    verify = ActionStep(
        run_id=run_id,
        media_key=candidate.media_key,
        ordinal=ordinal,
        kind="sonarr_verify_unmonitor",
        method="GET",
        path=f"/api/v3/series/{ref.arr_id}",
        body_json=None,
        idempotency_key=f"{idem}:verify",
        state=StepState.PENDING,
        created_at=now,
    )
    delete = ActionStep(
        run_id=run_id,
        media_key=candidate.media_key,
        ordinal=ordinal,
        kind="sonarr_delete_files",
        method="DELETE",
        path="/api/v3/episodefile/bulk",
        # Coordinates, not a frozen id list: the episodeFileIds are resolved live, right
        # before the delete, so a file added between plan and run is not missed or stale.
        body_json=json.dumps({"seriesId": ref.arr_id, "seasonNumber": ref.season}),
        idempotency_key=f"{idem}:delete",
        state=StepState.PENDING,
        created_at=now,
    )
    return [unmonitor, verify, delete]


def _plannable_size(candidate: Candidate) -> int:
    """The smallest-first sort key, for the measured set only.

    Never called on an unmeasured item: ``build_plan`` partitions those out first. It
    raises rather than substituting a number if that ever stops being true, because a
    stand-in zero would sort an unmeasured item to the front and make it the canary,
    which is the exact defect the partition exists to remove.
    """
    if candidate.size_bytes is None:
        raise PlanError(f"{candidate.media_key} has no measured size to order by.")
    return candidate.size_bytes


async def build_plan(
    session: AsyncSession,
    *,
    snapshot_id: int,
    policy_hash: str,
    approved_by: str,
    only_media_keys: set[str] | None = None,
    max_unmeasured: int = 0,
) -> ReapRun:
    """Build a run from the condemned candidates of a snapshot. Journal it. Send nothing.

    The run is created in ``PLANNED`` state with every step ``PENDING``. It records the
    manifest hash of what it would delete and who approved it; the executor will refuse
    to act if either the snapshot's policy or that manifest no longer matches.

    ``only_media_keys`` restricts the plan to an explicit set of items -- the "reap just
    these" path, and the safe way to do a first, single, hand-picked deletion. It changes
    only *which items get steps*: the manifest still binds to the **whole** condemned set,
    so if anything else in the snapshot shifts before execution the restricted run is voided
    too. Every requested key must be actable in this snapshot -- condemned by the scan or
    hand-reaped, and not spared -- or the build fails naming the offenders: a plan must
    never silently target fewer items than asked, or none at all.
    """
    snapshot = await session.get(Snapshot, snapshot_id)
    if snapshot is None:
        raise PlanError(f"No snapshot {snapshot_id}.")
    if snapshot.degraded:
        # A degraded snapshot missed a source or saw the history regress. Planning a
        # deletion from it means acting on a candidate list we already know is wrong.
        raise PlanError(
            f"Snapshot {snapshot_id} is degraded ({snapshot.degraded_reason}). "
            "No plan may be built from it."
        )

    all_condemned = list(
        (
            await session.execute(
                select(Candidate)
                .where(Candidate.snapshot_id == snapshot_id, Candidate.verdict == "condemn")
                # By key, not by size. This set feeds only ``manifest_hash`` (which sorts
                # internally) and a membership set, so the order here decides nothing --
                # and a size sort on a nullable column is an active trap, because SQLite
                # puts NULL FIRST on ASC. That would seat an unmeasured item at the front
                # of the very list the canary used to be drawn from. The ordering that
                # does matter is below, on the plannable set.
                .order_by(Candidate.media_key)
            )
        )
        .scalars()
        .all()
    )

    # A snapshot is frozen evidence, so the owner's overrides since it was taken are
    # applied here: a spare drops its item (no plan ever targets a file the owner told us
    # to keep), and a hand reap adds one -- when decide_verdict honors it past the
    # cautious protections (services.condemned). A decision on a whole show covers each of
    # its seasons. The executor re-derives this same set per item at execute time,
    # catching an override changed later in the grace window.
    decisions = await whitelist.overrides(session)
    effective = await effective_condemned(session, snapshot_id, decisions)

    # An item nobody would size is not plannable. A plan must be able to say what it would
    # free: a bound is not a measurement, and an unmeasured item cannot be counted against
    # a byte cap at all, so planning one means acting outside the limits the owner set.
    #
    # Smallest first among the rest, which is what makes ordinal 0 -- the canary -- the
    # least costly possible mistake, and orders the remainder so an aborting cap stops at
    # the cheapest frontier rather than a random one. The canary is why the two lists are
    # never merged and re-sorted: an unmeasured item has no size to sort by, so it could
    # only ever be seated by accident.
    measured = sorted(
        (c for c in effective.values() if c.size_bytes is not None), key=_plannable_size
    )
    held_back = sorted(
        (c for c in effective.values() if c.size_bytes is None), key=lambda c: c.media_key
    )
    # The allowance (``ProfileSettings.max_unmeasured_per_run``) lets an owner who has a
    # handful of items their *arr will not size reap them anyway. Zero by default, and
    # whatever it is set to, the unmeasured tail always sorts LAST.
    #
    # Written as concatenation rather than a combined sort deliberately. The invariant is
    # "no unmeasured item precedes a measured one", and it should be visible in the code
    # rather than emerge from sort stability or from a key that treats None as a number.
    #
    # Sorting last is necessary but NOT sufficient for the canary rule: with nothing
    # measured to sort ahead of it, the tail becomes the whole plan and ordinal 0 is an
    # item of unknown cost. So a plan with no measured item at all is refused outright.
    # The test item exists to make the first mistake one whose cost was known in advance;
    # a run that cannot offer such an item has no canary, only a first casualty.
    if max_unmeasured > 0 and held_back and not measured:
        raise PlanError(
            "Reaper couldn't measure any of these items, so it has nothing safe to test "
            "the run on. The first thing a run deletes has to be something whose size it "
            "knows. Check these in Sonarr or Radarr, then run a new scan."
        )

    plannable = measured + held_back if max_unmeasured > 0 else measured

    if only_media_keys is not None:
        # "Reap just these." Every requested key must be a condemned, non-spared item in
        # THIS snapshot -- refuse loudly on anything else, so the plan can never silently
        # cover fewer items than asked (or, worse, none). Spares are reported distinctly
        # from unknowns because the fix differs (remove the spare vs. it isn't condemned).
        requested = set(only_media_keys)
        if not requested:
            # An explicit but empty selection means "reap nothing", never "reap
            # everything". The caller distinguishes an omitted field (whole set) from an
            # empty list (this), so an empty set reaching here is a real, deliberate
            # "nothing selected" -- fail closed with a clear message rather than falling
            # through to plan the entire condemned set.
            raise PlanError("No items were selected to reap.")

        condemned_keys = {c.media_key for c in all_condemned}
        # Two sets, and the difference decides which refusal the owner reads. ``actable``
        # is everything the overrides left reapable; the held-back keys are the subset of
        # it with no size. A held-back key is actable, so checking only the narrower
        # plannable set would report it as spared and send the owner to remove a spare
        # that does not exist.
        actable_keys = set(effective)
        held_back_keys = {c.media_key for c in held_back}

        # A TV show is selectable in the review queue only at the show level, so a "reap
        # just these" request can carry a show's group_key (three-part
        # ``sonarr:{inst}:{series}``) instead of the four-part season media_keys beneath it.
        # Expand each requested group_key to its actable member seasons before the checks
        # below -- mirroring ``whitelist.effective_override``, where a decision on a whole
        # show reaches each of its seasons -- so the destructive path is symmetric with the
        # spare/reap override path (otherwise bulk-reaping any TV title is impossible). A
        # spared season is left OUT of the expansion, exactly as the override path keeps it,
        # rather than turning a show-level reap into a loud "these are spared" refusal for a
        # season the owner never named directly. An explicitly-named key is carried through
        # unchanged, so naming a spared or unknown key still fails loudly below. Members
        # come from the MEASURED set, so a hand-reaped season rides its show's bulk reap
        # and an unmeasured one is quietly left out -- exactly as a spared season is.
        # Drawing them from the wider effective set instead would pull an unmeasured season
        # into the expansion and then trip the refusal below on it, making "Reap now" fail
        # outright on any show with one unmeasured season, over an item the owner never
        # named. Measured rather than plannable even when the allowance is open: an
        # unmeasured season enters a plan through a deliberate whole-set or by-name reap,
        # never by riding a show-level click that was not aimed at it.
        members_by_group: dict[str, set[str]] = {}
        for c in measured:
            if c.group_key is not None:
                members_by_group.setdefault(c.group_key, set()).add(c.media_key)
        # Shows whose only reapable seasons are ones nothing would size. They have no
        # entry above (that map holds measured members only), so without this they would
        # fall through as an unrecognized key and be refused as "not condemned in this
        # snapshot" -- true of the key, and completely misleading about the show.
        unmeasured_groups: dict[str, set[str]] = {}
        for c in held_back:
            if c.group_key is not None and c.group_key not in members_by_group:
                unmeasured_groups.setdefault(c.group_key, set()).add(c.media_key)

        expanded: set[str] = set()
        for key in requested:
            members = members_by_group.get(key)
            if members is not None and key not in condemned_keys and key not in actable_keys:
                expanded |= members
            else:
                expanded.add(key)
        requested = expanded

        all_unmeasured = requested & set(unmeasured_groups)
        if all_unmeasured:
            raise PlanError(
                "Reaper couldn't measure any of the seasons it would remove from "
                f"{sorted(all_unmeasured)}, so there is nothing here it can reap. Check "
                "them in Sonarr, then run a new scan."
            )

        unknown = requested - (condemned_keys | actable_keys)
        if unknown:
            raise PlanError(
                "These items are not condemned in this snapshot, so they cannot be reaped: "
                f"{sorted(unknown)}."
            )
        # Named directly, so it is refused out loud rather than dropped. A key the owner
        # typed must never vanish from a plan in silence, even when the reason is safety.
        # With the allowance open these items are plannable, so there is nothing to refuse.
        named_held_back = requested & held_back_keys if max_unmeasured == 0 else set()
        if named_held_back:
            raise PlanError(
                "Reaper couldn't measure the size of these items, so it won't reap them: "
                f"{sorted(named_held_back)}. Check them in Sonarr or Radarr, then run a "
                "new scan."
            )
        spared = requested - actable_keys
        if spared:
            raise PlanError(
                f"These items are spared, so they will not be reaped: {sorted(spared)}. "
                "Remove the spare first if you really mean to delete them."
            )
        plannable = [c for c in plannable if c.media_key in requested]

    # Derived from what actually ended up in the plan, rather than decided up front by the
    # allowance. Deciding it up front made ``omitted`` empty whenever the allowance was
    # open, which silenced the very notice the allowance most needed: a show-level reap
    # still leaves its unmeasured seasons out of the expansion (they may only enter a plan
    # deliberately), so turning the allowance ON used to make the plan LESS honest than
    # leaving it off. Anything held back that did not make the plan is omitted, whatever
    # the setting says.
    planned_keys = {c.media_key for c in plannable}
    admitted = [c for c in held_back if c.media_key in planned_keys]
    omitted = [c for c in held_back if c.media_key not in planned_keys]
    if only_media_keys is not None:
        # Narrowed to what a requested show dropped from its own expansion: a held-back
        # item the owner never pointed at is not this plan's business to report.
        named = set(only_media_keys)
        omitted = [c for c in omitted if c.group_key is not None and c.group_key in named]

    # Abort, never truncate. Planning the first N would let sort order decide WHICH
    # unmeasured file dies, which is the accident this whole design removes -- and it is
    # the same abort-not-truncate discipline the byte caps already keep.
    if len(admitted) > max_unmeasured:
        raise PlanError(
            f"This plan holds {len(admitted)} items Reaper couldn't measure, over your "
            f"limit of {max_unmeasured} per run. The plan is refused rather than trimmed: "
            "which of them gets deleted must not come down to the order they were listed "
            "in. Raise the limit, or reap fewer items at once."
        )

    if admitted:
        log.info("planner.unmeasured_allowed", count=len(admitted), allowance=max_unmeasured)

    if omitted:
        log.info(
            "planner.held_back_unmeasured",
            count=len(omitted),
            media_keys=[c.media_key for c in omitted],
        )

    if not plannable:
        raise PlanError("Nothing is condemned in this snapshot; there is no plan to build.")

    now = utcnow()
    run = ReapRun(
        snapshot_id=snapshot_id,
        policy_hash=policy_hash,
        state=RunState.PLANNED,
        # The manifest binds to the WHOLE condemned set, spared or not. A spare is not a
        # change to what was condemned -- it is a decision to keep one of them -- so sparing
        # an item after approval must not change this fingerprint and void the run; the
        # executor honors the spare per item instead. Both sides hash the identical frozen
        # candidate rows for this immutable snapshot, so this fingerprint is a frozen-set
        # integrity check (it catches a condemned candidate row lost or tampered with under
        # the run), NOT live library drift -- nothing re-reads the *arr here. Live drift is
        # caught by the executor's per-item interlocks and its existence and size re-reads
        # at delete time; a stale tab is stopped by the route's confirmation-phrase
        # recompute.
        approved_manifest_hash=manifest_hash(all_condemned),
        approved_by=approved_by,
        approved_at=now,
        held_back_unknown_size=len(omitted),
    )
    session.add(run)
    await session.flush()  # assigns run.id

    ordinal = 0
    for candidate in plannable:
        ref = MediaRef.parse(candidate.media_key)
        if ref.kind == "radarr":
            steps = _movie_steps(run.id, candidate, ref, ordinal)
        elif ref.kind == "sonarr" and ref.season is not None:
            steps = _season_steps(run.id, candidate, ref, ordinal)
        else:
            # A whole-series (three-part) sonarr key is not season pruning and has no
            # delete path yet. Skip it LOUDLY -- logged, not silently dropped -- so the
            # plan never claims to cover something it does not.
            log.warning(
                "plan.no_delete_path",
                media_key=candidate.media_key,
                media_type=candidate.media_type,
            )
            continue
        for step in steps:
            session.add(step)
        ordinal += 1

    await session.flush()
    return run
