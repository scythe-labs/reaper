# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn condemned candidates into a plan Reaper can execute.

A plan is a ``ReapRun`` and its ordered ``ActionStep`` rows. Every step is written to
the database before anything is sent, with its exact method, path and body. This records
the intent before the act, so a crash mid-run leaves a full record of what was in flight.
Nothing reads this record back automatically yet: a run that stopped partway is
re-planned from a fresh scan rather than resumed (the executor refuses to run anything
but a PLANNED run). Each step's ``idempotency_key`` is stored for a future recovery pass,
not used to prevent duplicates today.

The planner never talks to Sonarr or Radarr, and it cannot delete anything. It reads
condemned candidates and writes journal rows. Whether those rows ever run is entirely the
executor's decision.

Two ordering rules matter:

* **The canary is ordinal 0: the single smallest condemned item.** Reaper deletes and
  verifies it alone before touching anything else, so a broken path mapping or a
  misunderstood API call costs one worthless file, not the whole run.

* **A movie deletion takes more than one step, in a fixed order:**

      1. delete the movie file, with the import-exclusion flag set
      2. re-read the exclusion list and check the id is present. Sonarr and Radarr each
         accept the other's exclusion parameter and return 200 while doing nothing, so a
         200 response proves nothing.
      3. refresh the affected Plex path (a partial scan, not a full re-download)

  Plex's ``emptyTrash`` never runs per item. It is one guarded call at the end of the
  run, gated by an item-count check, because emptying an unmounted library's trash can
  destroy the whole library.
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
from reaper.db.models import (
    ActionStep,
    Candidate,
    Instance,
    ReapRun,
    RunState,
    Snapshot,
    StepState,
)
from reaper.refusal import Refusal
from reaper.services import whitelist
from reaper.services.condemned import effective_condemned

log = structlog.get_logger(__name__)


class PlanError(Refusal):
    """The plan could not be built, for example when a candidate's media_key cannot be
    routed. Carries a catalog code plus raw parameters."""


@dataclass(frozen=True)
class MediaRef:
    """The coordinates a delete needs, parsed back out of a media_key.

    A movie or a whole series is three parts, for example ``radarr:1:42``. A season is
    four parts, for example ``sonarr:1:42:3`` for season 3 of series 42. Season pruning
    acts on a season, not a series, so it needs the fourth part.
    """

    kind: str  # "radarr" | "sonarr"
    instance_id: int
    arr_id: int
    season: int | None = None

    @classmethod
    def parse(cls, media_key: str) -> MediaRef:
        """Parse ``radarr:1:42`` as a movie, or ``sonarr:1:42:3`` as season 3 of series 42.

        A media_key that fails to parse raises an error instead of being skipped.
        Dropping an item from a plan is safe. Routing it to the wrong item is not, so an
        unparsed key must never be silently skipped.
        """
        parts = media_key.split(":")
        if len(parts) not in (3, 4) or parts[0] not in ("radarr", "sonarr"):
            raise PlanError("error.plan.media_key_unroutable", media_key=media_key)
        try:
            season = int(parts[3]) if len(parts) == 4 else None
            ref = cls(kind=parts[0], instance_id=int(parts[1]), arr_id=int(parts[2]), season=season)
        except ValueError as exc:
            raise PlanError(
                "error.plan.media_key_malformed", media_key=media_key, error=str(exc)
            ) from exc

        if ref.season is not None and ref.kind != "sonarr":
            # Only TV has seasons. A four-part radarr key is a malformed id, so the
            # planner refuses it instead of routing it anywhere.
            raise PlanError("error.plan.season_media_key_not_sonarr", media_key=media_key)
        return ref


def manifest_hash(candidates: Sequence[Candidate]) -> str:
    """A fingerprint of the frozen condemned set a plan was built from.

    The confirmation phrase the UI shows (for example "REAP 7 SOULS 214 GB") comes from
    this hash, and the executor recomputes it before acting. Both sides hash the same
    frozen, per-snapshot candidate rows, so this checks the integrity of that frozen set:
    it changes if a condemned candidate row is lost or altered while a run is pending,
    which voids the approval.

    It does not detect drift in the *arr library. Candidate rows are frozen at scan time
    and nothing here re-reads Radarr or Sonarr, so a title deleted or resized in Radarr
    after approval leaves this hash unchanged. The executor's own per-item checks catch
    that kind of drift, re-reading each item's existence and size right before it
    deletes. A stale browser tab is caught separately, by the route recomputing the
    confirmation phrase.

    The hash covers each item's media_key and size, sorted by media_key so the order
    items arrive in cannot change it. An item Reaper could not measure encodes its size
    as JSON ``null``, distinct from ``0``, so measuring it later changes the hash and
    voids the approval, as it should: the owner approved a different set than the one
    now on screen.

    The hash covers the whole condemned set, including items held back for having no
    size. It checks the integrity of that frozen set, not the plan built from it.
    """
    payload = sorted(((c.media_key, c.size_bytes) for c in candidates), key=lambda p: p[0])
    canonical = json.dumps(payload, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def plan_bytes(candidates: Sequence[Candidate]) -> tuple[int, int]:
    """The plan's size: total measured bytes, and how many items have no size.

    Returns two numbers, never a sum that folds an unmeasured item in as zero. A zero
    would understate the total, understating the byte cap, and a cap that does not fire
    lets the run delete more than the owner allowed. Rounding toward keeping is correct
    for scoring, but the wrong direction here.
    """
    measured = [c.size_bytes for c in candidates if c.size_bytes is not None]
    return sum(measured), len(candidates) - len(measured)


def confirmation_phrase(candidates: Sequence[Candidate]) -> str:
    """The confirmation phrase the operator must type, bound to what the plan will delete.

    It carries the item count and total size instead of a static "DELETE", so it cannot
    be typed from memory and a stale plan looks obviously different.

    The GB figure covers only items with a known size. When the allowance
    (``ProfileSettings.max_unmeasured_per_run``) admits unsized items, the phrase adds a
    ``+ N UNSIZED`` suffix, so the operator acknowledges items the GB figure leaves out.
    With the allowance at its default, the suffix never appears.

    ``api.runs.execute_run`` recomputes this string at execute time and compares it to
    what was typed, so any wording change here fails every pending execute with a 409.
    """
    total, unsized = plan_bytes(candidates)
    gib = total / 1024**3
    n = len(candidates)
    noun = "SOUL" if n == 1 else "SOULS"
    phrase = f"REAP {n} {noun} {gib:.0f} GB"
    return f"{phrase} + {unsized} UNSIZED" if unsized else phrase


def _movie_steps(
    run_id: int, candidate: Candidate, ref: MediaRef, ordinal: int, *, add_exclusion: bool
) -> list[ActionStep]:
    """Build the one journal step that deletes a movie, with its import exclusion.

    A season prunes in three journalled steps because two of them are reversible and
    their order matters (see :func:`_season_steps`). A movie has one irreversible call,
    so the exclusion poll and the Plex refresh that follow it belong to the executor
    (``executor._send_movie``) and are never journalled as their own steps. A journal for
    a movie never shows a verify step.

    ``path`` and ``body`` carry no credentials. The client injects the api key at send
    time, so the journal is safe to show in the UI and keep forever. ``idempotency_key``
    is a stable per-step id, stored for a future recovery pass; nothing reads it today to
    prevent a double delete. The "executes once" guard and the fact that a delete cannot
    repeat do that job instead.

    ``add_exclusion`` freezes the target Radarr's ``add_import_exclusion`` setting into
    the body, so what the operator approves is exactly what gets sent. The executor
    reads this same value back from the journal (``executor._send_movie``) rather than
    re-reading a setting that may have changed since approval.
    """
    now = utcnow()
    idem = f"{run_id}:{candidate.media_key}"

    # Radarr's own parameter spelling. Sonarr uses a different one and each ignores the
    # other's silently, so the planner sends the correct spelling for each kind.
    delete = ActionStep(
        run_id=run_id,
        media_key=candidate.media_key,
        ordinal=ordinal,
        kind="radarr_delete",
        method="DELETE",
        path=f"/api/v3/movie/{ref.arr_id}",
        body_json=json.dumps({"deleteFiles": True, "addImportExclusion": add_exclusion}),
        idempotency_key=f"{idem}:delete",
        state=StepState.PENDING,
        created_at=now,
    )
    return [delete]


def _season_steps(
    run_id: int, candidate: Candidate, ref: MediaRef, ordinal: int
) -> list[ActionStep]:
    """Build the three journal steps that prune one season: unmonitor, verify, delete files.

    The order matters, because the two half-applied states are not equally safe.
    "Unmonitored, files intact" is safe and can be resumed. "Files gone, still monitored"
    makes Sonarr re-download everything just removed. So the file delete runs last, and
    only after the unmonitor is verified. Sonarr returns 200 for a season-pass edit
    whether or not it actually took, the same trap the movie exclusion has.

    The delete step records the season's coordinates, not a frozen list of
    ``episodeFileIds``. A season's files change as Sonarr downloads, so the executor
    resolves the ids against the live series immediately before deleting, never at plan
    time.
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
    """The sort key that orders measured candidates smallest first.

    ``build_plan`` partitions out unmeasured items before calling this, so it should
    never see one. It raises rather than returning a stand-in zero, because a zero would
    sort an unmeasured item to the front and make it the canary, the exact mistake the
    partition exists to prevent.
    """
    if candidate.size_bytes is None:
        raise PlanError("error.plan.unmeasured_sort_key", media_key=candidate.media_key)
    return candidate.size_bytes


def _refuse_without_a_canary(plannable: list[Candidate], max_unmeasured: int) -> None:
    """Refuse a plan whose first item has no measured size: it has no canary, only a
    first casualty.

    Call this on the final plannable list, after every narrowing. Checking it against an
    earlier, wider list would let a "reap just these" request over only unmeasured items
    pass a check that measured items later dropped from the plan satisfied, then delete
    an item of unknown cost first. The check must see the exact set the run will act on.

    This never fires when the allowance is off, since ``build_plan`` plans measured
    items only in that case. An empty plannable list is not this function's refusal to
    raise: the caller reports "nothing is condemned" for that case instead.
    """
    if max_unmeasured <= 0 or not plannable:
        return
    if any(c.size_bytes is not None for c in plannable):
        return
    raise PlanError("error.plan.no_canary")


async def build_plan(
    session: AsyncSession,
    *,
    snapshot_id: int,
    approved_by: str,
    only_media_keys: set[str] | None = None,
    max_unmeasured: int = 0,
) -> ReapRun:
    """Build a run from a snapshot's condemned candidates, journal it, and send nothing.

    The run starts in ``PLANNED`` state with every step ``PENDING``. It records the
    manifest hash of what it would delete and who approved it. The executor refuses to
    act if either the snapshot's policy or that manifest no longer matches.

    This reads the policy hash off the snapshot rather than taking it as an argument, so
    a caller cannot pass a value other than ``snapshot.policy_hash`` and hand the
    executor's policy check the wrong number.

    ``only_media_keys`` restricts the plan to an explicit set of items, the "reap just
    these" path for a first, single, hand-picked deletion. It changes only which items
    get steps: the manifest still binds to the whole condemned set, so a later change
    anywhere in the snapshot still voids the restricted run. Every requested key must be
    an actable item in this snapshot, condemned by the scan or hand-reaped, and not
    spared. Otherwise the build fails and names the offending keys: a plan must never
    target fewer items than requested, or none at all, without saying so.
    """
    snapshot = await session.get(Snapshot, snapshot_id)
    if snapshot is None:
        raise PlanError("error.plan.no_snapshot", snapshot_id=snapshot_id)
    if snapshot.degraded:
        # A degraded snapshot missed a source or saw its watch history go backward.
        # Planning a deletion from it acts on a candidate list already known to be
        # wrong. This reaches the operator as an HTTP 422 body on the Reap page
        # (`api/runs.py`), written in plain language for them: no snapshot id, and not
        # the word "degraded". It matches the wording the incomplete-scan notices use
        # elsewhere in the app.
        # The reason must go last, with nothing appended after it. `ScanContext.degrade`
        # always ends a reason with a period, but this reads a stored column that
        # something else may have written without one, so appending text here could run
        # two sentences together.
        raise PlanError("error.plan.snapshot_degraded", reason=snapshot.degraded_reason or "")

    all_condemned = list(
        (
            await session.execute(
                select(Candidate)
                .where(Candidate.snapshot_id == snapshot_id, Candidate.verdict == "condemn")
                # Ordered by key, not by size. This set feeds only ``manifest_hash``
                # (which sorts internally) and a membership check, so this order decides
                # nothing on its own. Sorting by a nullable size column is a trap:
                # SQLite puts NULL first on ASC, which would put an unmeasured item
                # first in a list read for the canary. The order that matters is below,
                # on the plannable set.
                .order_by(Candidate.media_key)
            )
        )
        .scalars()
        .all()
    )

    # A snapshot freezes its evidence, so this applies the owner's overrides made since
    # the scan ran. A spare removes its item: no plan ever targets a file the owner said
    # to keep. A hand reap adds an item when :func:`services.condemned.effective_condemned`
    # honors it past the built-in protections. A decision on a whole show covers each of
    # its seasons. The executor re-derives this same set per item at execute time, so an
    # override changed later in the grace window still takes effect.
    decisions = await whitelist.overrides(session)
    effective = await effective_condemned(session, snapshot_id, decisions)

    # An item with no known size is not plannable on its own. A plan must be able to say
    # what it will free, and an unmeasured item cannot count against a byte cap.
    #
    # The rest sort smallest first, so ordinal 0, the canary, is the least costly
    # possible mistake, and an aborting cap stops at the cheapest items rather than a
    # random set. The two lists stay separate rather than merging and re-sorting,
    # because an unmeasured item has no size to sort by and could only land first by
    # accident.
    measured = sorted(
        (c for c in effective.values() if c.size_bytes is not None), key=_plannable_size
    )
    held_back = sorted(
        (c for c in effective.values() if c.size_bytes is None), key=lambda c: c.media_key
    )
    # The allowance (``ProfileSettings.max_unmeasured_per_run``) lets an owner reap a
    # handful of items their *arr will not report a size for. It is zero by default, and
    # whatever it is set to, unmeasured items always sort last.
    #
    # Concatenated rather than sorted together on purpose, so the rule "no unmeasured
    # item precedes a measured one" is visible in the code itself, not left to sort
    # stability or a key that treats None as a number.
    #
    # Sorting unmeasured items last is not enough on its own: with no measured item
    # ahead of it, the whole plan is unmeasured and ordinal 0 is an item of unknown
    # cost. :func:`_refuse_without_a_canary` checks the final list below and refuses
    # that case outright.
    plannable = measured + held_back if max_unmeasured > 0 else measured

    #: The requested selection after group_key expansion, used in the summary log below.
    #: Set outside the block below because a show-level reap sends one key but plans
    #: several seasons, so the caller's count differs from the count the plan actually
    #: used.
    selected: set[str] | None = None

    if only_media_keys is not None:
        # "Reap just these." Every requested key must be a condemned, non-spared item in
        # this snapshot. Anything else raises, so a plan can never silently cover fewer
        # items than asked, or none. Spares are reported separately from unknown keys,
        # since the fix differs: remove the spare, or the item was never condemned.
        requested = set(only_media_keys)
        if not requested:
            # An explicit but empty selection means "reap nothing," not "reap
            # everything." The caller distinguishes an omitted field, which means the
            # whole set, from an empty list, which means this. So an empty set reaching
            # here is a deliberate "nothing selected," and it raises a clear error
            # instead of planning the entire condemned set.
            raise PlanError("error.plan.selection_empty")

        condemned_keys = {c.media_key for c in all_condemned}
        # Two sets decide which error the owner sees. ``actable_keys`` is everything the
        # overrides left reapable; ``held_back_keys`` is the subset of it with no size. A
        # held-back key is still actable, so checking only the narrower plannable set
        # would wrongly report it as spared and send the owner to remove a spare that
        # does not exist.
        actable_keys = set(effective)
        held_back_keys = {c.media_key for c in held_back}

        # The review queue lets an operator select a TV show only at the show level, so a
        # "reap just these" request can carry a show's group_key
        # (``sonarr:{inst}:{series}``) instead of the four-part season keys beneath it.
        # Expand each requested group_key to its actable member seasons before the
        # checks below, the same way ``whitelist.effective_override`` reaches every
        # season of a show from one decision on it. A spared season stays out of the
        # expansion, so a show-level reap does not fail loudly over a season the owner
        # never named. A key named directly is carried through unchanged, so naming a
        # spared or unknown key still fails below.
        #
        # Members come from the measured set only. A hand-reaped season rides along with
        # its show's bulk reap, and an unmeasured one is left out, the same as a spared
        # season. Using the wider effective set instead would pull an unmeasured season
        # into the expansion and then fail the whole show's reap over an item the owner
        # never named. An unmeasured season enters a plan only through a deliberate
        # whole-set or by-name reap, never by riding a show-level click aimed at other
        # seasons.
        members_by_group: dict[str, set[str]] = {}
        for c in measured:
            if c.group_key is not None:
                members_by_group.setdefault(c.group_key, set()).add(c.media_key)
        # Shows whose only reapable seasons have no known size. They have no entry
        # above, since that map holds measured members only. Without this, such a
        # show's key would look unrecognized and get refused as "not condemned in this
        # snapshot," which is technically true but misleading about the show.
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
            raise PlanError("error.plan.unmeasured_seasons", keys=", ".join(sorted(all_unmeasured)))

        unknown = requested - (condemned_keys | actable_keys)
        if unknown:
            raise PlanError("error.plan.items_not_condemned", keys=", ".join(sorted(unknown)))
        # Refused out loud rather than dropped, since a key the owner named must never
        # vanish from a plan silently, even for a safety reason. With the allowance
        # open, these items are plannable, so there is nothing to refuse.
        named_held_back = requested & held_back_keys if max_unmeasured == 0 else set()
        if named_held_back:
            raise PlanError("error.plan.items_unmeasured", keys=", ".join(sorted(named_held_back)))
        spared = requested - actable_keys
        if spared:
            raise PlanError("error.plan.items_spared", keys=", ".join(sorted(spared)))
        plannable = [c for c in plannable if c.media_key in requested]
        selected = requested

    # Every narrowing above is done, so this is the exact set the run will act on, the
    # only set the canary check means anything over.
    _refuse_without_a_canary(plannable, max_unmeasured)

    # Derived from what actually ended up in the plan, not decided up front from the
    # allowance. A show-level reap still leaves its unmeasured seasons out of the
    # expansion, since they can only enter a plan deliberately, so deciding this from
    # the allowance alone would report it as empty even when items were held back.
    # Anything held back that did not make the plan is omitted, whatever the allowance
    # is set to.
    planned_keys = {c.media_key for c in plannable}
    admitted = [c for c in held_back if c.media_key in planned_keys]
    omitted = [c for c in held_back if c.media_key not in planned_keys]
    if only_media_keys is not None:
        # Narrowed to what a requested show dropped from its own expansion. A held-back
        # item the owner never pointed at is not this plan's business to report.
        named = set(only_media_keys)
        omitted = [c for c in omitted if c.group_key is not None and c.group_key in named]

    # Abort rather than truncate. Planning only the first N would let sort order decide
    # which unmeasured file gets deleted, which is exactly what this design prevents.
    # The byte caps use the same abort-not-truncate rule.
    if len(admitted) > max_unmeasured:
        raise PlanError(
            "error.plan.unmeasured_over_limit", count=len(admitted), limit=max_unmeasured
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
        raise PlanError("error.plan.nothing_condemned")

    # Read every instance's row once. The keys are the instance ids configured right
    # now, which the guard below checks against. The values are each Radarr's own
    # import-exclusion setting, frozen into the movie delete body below so the preview
    # matches what actually gets sent, and so the executor reads this same approved
    # value back from the journal (``executor._send_movie``) instead of a setting that
    # may have changed since approval. ``add_import_exclusion`` is NOT NULL, so every
    # instance has a row and this key set is complete.
    exclusion_by_instance: dict[int, bool] = {
        row.id: row.add_import_exclusion
        for row in (await session.execute(select(Instance.id, Instance.add_import_exclusion))).all()
    }
    # A movie candidate froze its instance id at scan time, but this map holds only the
    # instances that exist now. A Radarr removed between the scan and the plan is
    # missing here, and that miss matters: the movie cannot be deleted
    # (``executor.radarr_for`` refuses it), and its exclusion setting is gone, so a plan
    # built on a default would preview a delete under a setting the operator never
    # chose. The snapshot is stale, so this refuses the plan outright instead of
    # substituting a default, the same as the refusals above. A re-scan drops the item,
    # since a removed instance is never fetched again.
    #
    # The Sonarr season path freezes no per-instance setting, so it has nothing to
    # substitute; the executor refuses a season targeting a removed Sonarr per item
    # instead.
    orphaned = sorted(
        {
            ref.instance_id
            for c in plannable
            if (ref := MediaRef.parse(c.media_key)).kind == "radarr"
            and ref.instance_id not in exclusion_by_instance
        }
    )
    if orphaned:
        raise PlanError("error.plan.instance_orphaned")

    now = utcnow()
    run = ReapRun(
        snapshot_id=snapshot_id,
        # The policy this snapshot was scored under. The executor compares it to the
        # policy in force at execute time and refuses to run a plan judged under a
        # policy since replaced.
        policy_hash=snapshot.policy_hash,
        state=RunState.PLANNED,
        # The manifest binds to the whole condemned set, spared or not. Sparing an item
        # is a decision to keep it, not a change to what was condemned, so sparing it
        # after approval must not change this hash and void the run. The executor
        # honors the spare per item instead. Both sides hash the same frozen candidate
        # rows for this snapshot, so this checks the integrity of that frozen set. It
        # catches a condemned candidate row lost or altered while the run is pending.
        # It does not catch drift in the *arr library, since nothing here re-reads
        # Radarr or Sonarr: the executor's per-item checks catch that, re-reading each
        # item's existence and size right before deleting it, and the route
        # recomputing the confirmation phrase catches a stale browser tab.
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
            # Guaranteed present: the orphaned-instance guard above already refused the
            # plan if any movie named a Radarr missing from this map. A KeyError here
            # means that guard failed, not that the operator removed an instance.
            add_exclusion = exclusion_by_instance[ref.instance_id]
            steps = _movie_steps(run.id, candidate, ref, ordinal, add_exclusion=add_exclusion)
        elif ref.kind == "sonarr" and ref.season is not None:
            steps = _season_steps(run.id, candidate, ref, ordinal)
        else:
            # A whole-series (three-part) sonarr key is not season pruning, and has no
            # delete path yet. Log it and skip it, so the plan never claims to cover
            # something it does not.
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
    # The set narrows four times between the review queue and the plan: overrides, the
    # measured/held-back split, the requested selection, and items with no delete path.
    # This line logs every stage, so a mismatch between what the queue showed and what
    # the plan built is answerable from the log alone.
    #
    # Every value here is already computed above, so logging it costs nothing at INFO.
    # The per-item detail behind the first two stages is logged at DEBUG below. The
    # requested selection has no per-item detail here on purpose: naming one title out
    # of three hundred would log the other two hundred ninety-nine as not picked, and
    # the operator already knows which one they clicked. The no-delete-path drop names
    # its item at WARNING above.
    #
    # `requested` and `selected` answer different questions, and a show-level reap
    # makes them differ: `requested` is what the caller sent, `selected` is that set
    # after each group_key expanded to its member seasons, the set the plan was
    # actually narrowed to. Reporting only `requested` would misrepresent a single click
    # on a five-season show as `requested=1, planned=5`.
    log.info(
        "planner.built",
        run_id=run.id,
        snapshot_id=snapshot_id,
        condemned=len(all_condemned),
        effective=len(effective),
        measured=len(measured),
        held_back=len(held_back),
        requested=len(only_media_keys) if only_media_keys is not None else None,
        selected=len(selected) if selected is not None else None,
        planned=ordinal,
    )
    # Named rather than counted, because the operator's question is always about one title.
    for media_key in sorted({c.media_key for c in all_condemned} - set(effective)):
        log.debug("planner.dropped", media_key=media_key, reason="spared by hand")
    for candidate in held_back:
        log.debug(
            "planner.dropped" if candidate.media_key not in planned_keys else "planner.admitted",
            media_key=candidate.media_key,
            reason="no measured size",
        )
    return run
