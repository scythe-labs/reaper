# SPDX-License-Identifier: AGPL-3.0-or-later
"""The "Leaving Soon" mark.

While an item is in its grace window, Reaper marks it "Leaving Soon" in Plex, so the
users who share the library get a heads-up before anything disappears. The mark tracks
the grace set: an item entering grace is labelled, an item that leaves grace (spared,
rescued, or re-judged) has the label removed. That reconcile is the whole feature.

Two honest limits, both flagged rather than hidden:

* **The Plex label reaches only users who have *pinned* the library** -- no server can
  force a pinned hub. So Discord, not the label, is the real warning channel; the label
  is a bonus for the users who will see it.
* **Writing the label is a mutation, so it is guarded** -- but a benign one. It goes
  through ``GuardedSession``; by default it is gated exactly like a deletion (write only
  when armed), and an operator can set ``REAPER_ALLOW_UNARMED_LEAVING_SOON`` on the host
  to allow the write while read-only, so the warning can appear *during* the grace
  countdown, which is the point of it. It can never permit a delete: only the label is
  written, and file deletions still require arming plus a journalled declaration. Reading
  what is already marked is a GET and works any time.

The reconcile itself is pure and lives at the top of this file, where it can be tested to
death without a Plex server in sight.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import structlog

from reaper.clients.plex import PlexClient, benign_label_write
from reaper.notify.discord import DiscordNotifier
from reaper.services.grace import GraceReport

log = structlog.get_logger(__name__)

#: Plex title-cases this to "Leaving Soon" on the way in; every comparison in the Plex
#: client casefolds, so the display form is what we write and search for.
LEAVING_SOON_LABEL = "Leaving Soon"


@dataclass(frozen=True)
class LeavingSoonPlan:
    """What a reconcile would change: keys to newly label, and keys to unlabel."""

    to_add: list[int]
    to_remove: list[int]

    @property
    def is_noop(self) -> bool:
        return not self.to_add and not self.to_remove


def reconcile(should_be_labelled: set[int], currently_labelled: set[int]) -> LeavingSoonPlan:
    """The label set should exactly track the in-grace set.

    Add the label to in-grace items that lack it; remove it from items that carry it but
    are no longer in grace (spared, rescued, or aged out). Sorted so the plan is stable
    and diffable, not dependent on set iteration order.
    """
    return LeavingSoonPlan(
        to_add=sorted(should_be_labelled - currently_labelled),
        to_remove=sorted(currently_labelled - should_be_labelled),
    )


class LabelTarget(Protocol):
    """The slice of Plex the reconcile needs. A Protocol so ``sync`` is testable against a
    fake, and the real Plex adapter's live behaviour is verified separately against a
    server."""

    async def current(self) -> set[int]:
        """Rating keys currently carrying the Leaving Soon label."""
        ...

    async def apply(self, plan: LeavingSoonPlan) -> None:
        """Write the plan. May raise if Reaper is not armed -- writing a label is a
        guarded mutation, exactly like a delete."""
        ...


@dataclass(frozen=True)
class LeavingSoonResult:
    plan: LeavingSoonPlan
    applied: bool
    """Whether the label writes actually landed. False when running as a plan-only preview,
    or when the guard refused the write because Reaper is not armed."""
    notified: bool
    """Whether a Discord announce for the newly-marked titles was sent."""
    announced: frozenset[int] = frozenset()
    """The updated set of rating keys that have been announced and are still in grace. The
    caller persists this so the next sync knows what was already announced -- the whole
    reason the heads-up is idempotent even when the Plex label write never lands."""


async def sync(
    target: LabelTarget,
    grace: GraceReport,
    *,
    notifier: DiscordNotifier | None = None,
    apply: bool = False,
    already_announced: set[int] | None = None,
) -> LeavingSoonResult:
    """Bring the Leaving Soon label set in line with the grace set, and announce new ones.

    ``apply=False`` (the default) computes the plan and sends the Discord heads-up without
    touching Plex -- a preview that is safe in read-only mode. ``apply=True`` also writes
    the label, which the target's guard will refuse unless Reaper is armed; the caller
    decides how to surface that refusal.

    The Discord announce is idempotent independent of whether the label write landed.
    ``already_announced`` is the durable set of rating keys announced on previous syncs; a
    title is announced only if it is newly in grace *and* not already in that set. This
    matters because in the default read-only install the label is never written to Plex, so
    ``target.current()`` never learns about the marked items and ``plan.to_add`` would
    otherwise be the entire in-grace set on *every* call -- re-spamming the channel with the
    same titles each time an operator clicks Sync. Keying off a persisted announced set
    instead makes repeated syncs quiet, and also fixes the armed multi-section case where an
    item outside the reconciled section is invisible to ``current()``. The returned
    ``announced`` set is pruned to items still in grace, so a title that leaves grace and
    later returns is announced afresh; the caller persists it.
    """
    # Movies only. The Leaving Soon label lives in the movie library (the reap loop is
    # movies-first), and the target reads/writes a movie section. A season's rating key
    # points at a TV item, so letting it into `should` would write the label into a section
    # that can never see it again -- the reconcile could add it but never remove it, and it
    # would be re-announced every run. Seasons get their own surface when TV Leaving Soon
    # exists; until then they are excluded here rather than leaked into the movie path.
    movies = [
        i for i in grace.in_grace if i.plex_rating_key is not None and i.media_type == "movie"
    ]
    should = {i.plex_rating_key for i in movies if i.plex_rating_key is not None}
    current = await target.current()
    plan = reconcile(should, current)

    applied = False
    if apply and not plan.is_noop:
        await target.apply(plan)
        applied = True

    # Announce only what is newly in grace AND not already announced on a prior sync. The
    # Plex-label diff (plan.to_add) is not a reliable "new" signal on its own: in the
    # read-only path the label is never written, so it never shrinks.
    already = already_announced or set()
    to_announce = sorted(set(plan.to_add) - already)

    notified = False
    announced_now: set[int] = set()
    if notifier is not None and to_announce:
        keys = set(to_announce)
        titles = [i.title for i in movies if i.plex_rating_key in keys]
        notified = await notifier.announce_leaving_soon(titles, grace_days=grace.grace_days)
        # Record as announced only if the post actually landed; a failed announce must be
        # retried on the next sync, not silently marked done.
        if notified:
            announced_now = keys

    # The persisted set tracks items still in grace that have been announced. Intersecting
    # with ``should`` prunes anything that left grace, so its rating key is free to be
    # announced again if it ever returns.
    announced = frozenset((already | announced_now) & should)

    log.info(
        "leaving_soon.synced",
        to_add=len(plan.to_add),
        to_remove=len(plan.to_remove),
        applied=applied,
        notified=notified,
    )
    return LeavingSoonResult(plan=plan, applied=applied, notified=notified, announced=announced)


class PlexLabelTarget:
    """The real target: reads and writes the Leaving Soon label on one Plex section.

    A single section is a deliberate simplification for the first cut -- a 4K/HD split has
    two movie sections, and resolving each rating key to its section needs a live server
    to get right. Reading current labels and writing them are both here; the write is
    guarded and inert until Reaper is armed.
    """

    def __init__(self, plex: PlexClient, section_title: str) -> None:
        self._plex = plex
        self._section = section_title

    async def current(self) -> set[int]:
        return await self._plex.items_with_label(self._section, LEAVING_SOON_LABEL)

    async def apply(self, plan: LeavingSoonPlan) -> None:
        # ``benign_label_write`` tells the guard these are the Leaving Soon label -- a
        # reversible mutation that touches no files -- so it is gated on
        # ``leaving_soon_write_allowed`` (armed, or the host opted in) rather than on the
        # journalled-delete path. It can never permit a deletion. Removing first keeps the
        # label set from briefly over-covering if a later add fails.
        with benign_label_write():
            if plan.to_remove:
                await self._plex.remove_label(self._section, plan.to_remove, LEAVING_SOON_LABEL)
            if plan.to_add:
                await self._plex.add_label(self._section, plan.to_add, LEAVING_SOON_LABEL)
