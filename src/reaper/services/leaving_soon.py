# SPDX-License-Identifier: AGPL-3.0-or-later
"""The "Leaving Soon" shelf.

While an item is in its grace window, Reaper can show it on a **"Leaving Soon"
collection** in Plex -- a real shelf on the library's Recommended page, visible to your
users -- and put the matching label on it for anyone who builds smart collections or
overlay tooling on top. The shelf tracks the grace set: an item entering grace appears,
an item that leaves grace (spared, rescued, or re-judged) is taken off. That reconcile
is the whole feature, run per enabled library: movies in movie libraries, seasons in TV
libraries.

Honest limits, flagged rather than hidden:

* **Plex cannot force the shelf onto everyone.** It shows on the library's Recommended
  page and on Home for users who pinned the library. Discord, configured under
  Notifications, is the warning channel that reaches everyone else, and it works whether
  or not the shelf is on.
* **Writing the shelf is a mutation, so it is guarded** -- but a benign one. It goes
  through ``benign_shelf_write``, which permits exactly the label batch edit and the
  collection edits (see ``clients.plex._benign_shape``); by default it is gated exactly
  like a deletion (write only when armed), and the operator can turn on "Update while
  read-only" in Settings -> Plex to allow the write while read-only, so the warning can
  appear *during* the grace countdown, which is the point of it. It can never permit a
  delete: only the shelf shapes are permitted, and file deletions still require arming
  plus a journalled declaration. Reading what is already marked is a GET and works any
  time.

The reconcile itself is pure and lives at the top of this file, where it can be tested
to death without a Plex server in sight.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.aio import gather_reaped, per_loop_lock
from reaper.clients.plex import PlexClient, PlexError, benign_shelf_write
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.models import PlexServer, Snapshot
from reaper.notify.discord import DiscordNotifier, build_notifier
from reaper.services import app_settings
from reaper.services.grace import GraceReport, grace_report
from reaper.services.profiles import active_profile_settings

log = structlog.get_logger(__name__)

#: Plex title-cases this to "Leaving Soon" on the way in; every comparison in the Plex
#: client casefolds, so the display form is what we write and search for. The collection
#: and the label share the name deliberately -- one shelf, one vocabulary.
LEAVING_SOON_LABEL = "Leaving Soon"
LEAVING_SOON_COLLECTION = "Leaving Soon"

#: How many libraries reconcile at once. Libraries are independent shelves, so they run
#: concurrently rather than one after another -- but under a bound, for the same reason the
#: season fan-out is bounded: a modest self-hosted Plex should see a handful of parallel
#: reads, never one per library at once, and the client's shared requests session pools only
#: ten connections. A setup with many libraries reconciles in waves rather than a stampede.
#:
#: These tasks push reads AND writes through the one shared ``GuardedSession`` across threads
#: without taking the client's ``_sweep_lock`` (which the GUID sweeps use). Acceptable here
#: because urllib3's connection-pool checkout is atomic and each library's ``sync_section``
#: holds a DISTINCT plexapi section object, so the per-library ``batchMultiEdits`` state never
#: overlaps -- no shared mutable request state crosses threads. If that ever changes, serialize
#: the shelf writes under ``_sweep_lock`` instead (rule 24 / PR-4).
SHELF_CONCURRENCY = 4


class LeavingSoonDisabledError(RuntimeError):
    """The shelf is turned off, so there is nothing to update."""


class LeavingSoonDegradedError(RuntimeError):
    """The last scan declared itself untrustworthy, so the shelf must not act on it."""


async def _latest_scan_degraded(session: AsyncSession) -> bool:
    """Did the most recent scan declare itself untrustworthy?

    A degraded snapshot is un-plannable outright, and rule 116 says its side effects are
    gated WITH its plan. The shelf and the Discord heads-up both read the same condemned
    set the planner refuses to touch, so acting on it here is the same mistake as deleting
    on it, minus the file: an operator is told in their own library, or in a room full of
    people, that a title is about to go, on evidence Reaper has already said it cannot
    stand behind.

    ``scan_runner`` already skips the after-scan shelf on a degraded snapshot, but that
    covered only the automatic path: ``POST /api/leaving-soon/sync`` still labeled items
    and announced. Gating it here instead covers every caller of :func:`run_sync`, which
    is the one place both paths converge.

    No snapshot at all is not degraded. It is simply nothing to announce, and the grace
    report below is empty on its own.
    """
    latest = (
        await session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1))
    ).scalar_one_or_none()
    return latest is not None and bool(latest.degraded)


@dataclass(frozen=True)
class LeavingSoonPlan:
    """What a reconcile would change: keys to newly mark, and keys to unmark."""

    to_add: list[int]
    to_remove: list[int]

    @property
    def is_noop(self) -> bool:
        return not self.to_add and not self.to_remove


def reconcile(should_be_marked: set[int], currently_marked: set[int]) -> LeavingSoonPlan:
    """The marked set should exactly track the in-grace set.

    Mark in-grace items that lack it; unmark items that carry it but are no longer in
    grace (spared, rescued, or aged out). Sorted so the plan is stable and diffable, not
    dependent on set iteration order.
    """
    return LeavingSoonPlan(
        to_add=sorted(should_be_marked - currently_marked),
        to_remove=sorted(currently_marked - should_be_marked),
    )


@dataclass(frozen=True)
class ShelfOutcome:
    """What the reconcile did (or would do) in one library."""

    section_key: int
    section_title: str
    kind: str
    """``"movie"`` or ``"show"`` -- which level the shelf works at in this library."""
    added: int
    removed: int
    on_shelf: int
    """How many items belong on this library's shelf after the reconcile."""
    applied: bool
    """Whether the shelf is real in Plex after this pass: writing was allowed and the
    walk completed (a library that already matched counts -- nothing needed writing).
    False only in preview (read-only, no opt-in)."""
    error: str | None = None
    """A per-library failure. One unreachable library never blocks the others."""


@dataclass(frozen=True)
class LeavingSoonResult:
    """One full pass over every enabled library, plus the Discord heads-up."""

    outcomes: list[ShelfOutcome]
    notified: bool
    announced: frozenset[int]
    """The updated set of rating keys that have been announced and are still in grace.
    The caller persists this so the next pass knows what was already announced -- why
    the heads-up is idempotent even when the shelf write never lands."""
    movies_on_shelves: int
    seasons_on_shelves: int

    @property
    def added(self) -> int:
        return sum(o.added for o in self.outcomes)

    @property
    def removed(self) -> int:
        return sum(o.removed for o in self.outcomes)

    @property
    def applied(self) -> bool:
        """Whether every attempted write landed. False when previewing, and false when
        any library errored -- a partial write must never report itself as complete."""
        return bool(self.outcomes) and all(o.applied and o.error is None for o in self.outcomes)

    @property
    def problems(self) -> list[str]:
        return [f"{o.section_title}: {o.error}" for o in self.outcomes if o.error]

    @property
    def no_libraries(self) -> bool:
        """Nothing is turned on, so the pass had nothing it was allowed to touch.

        One outcome is recorded per enabled library (``sync_shelves``), so an empty list is
        exactly that state, and never a library that ran and found nothing to do.
        """
        return not self.outcomes

    @property
    def ok(self) -> bool:
        """Whether the pass did what it set out to do: no library failed, and there was one
        turned on to update. Preview is not a failure, so this stays true there -- the shelf
        was computed and the heads-up went out, and only the Plex write was held back."""
        return not self.problems and not self.no_libraries

    @property
    def summary(self) -> str:
        """One plain sentence saying how this pass went.

        Every surface reporting the pass reads this one: the stored Jobs row, the response the
        "Update now" button flashes, and the Plex panel's status line. It was written twice --
        here, and again in TypeScript over the response -- and the two disagreed, because the
        route added the no-libraries case AFTER this had been stored. A pass with nothing
        turned on was stored green while the button flashed red about the same pass (#555).

        The order is the fix. Nothing turned on is reported as itself rather than as preview,
        which is what ``applied`` alone calls it (it is false whenever there are no outcomes),
        and a real per-library failure beats the benign preview caveat.
        """
        if self.no_libraries:
            return "No libraries are turned on, so no shelf was updated"
        if self.problems:
            # Named, not counted. "Some shelves didn't update" was the whole answer the
            # operator got, on every surface, while the response carried the failing library
            # per entry and nothing rendered it -- so one unreachable library out of five was
            # indistinguishable from all five, and there was nowhere to go and look. The
            # titles come from here rather than from ``problems`` because that string appends
            # ``str(exc)``, which is a stack-shaped sentence rule 21 keeps off the screen; it
            # stays the log's field, where a raw cause belongs.
            failed = ", ".join(o.section_title for o in self.outcomes if o.error)
            return f"These shelves didn't update: {failed}"
        if not self.applied:
            return "Preview only, nothing written"
        return f"{self.added:,} added, {self.removed:,} cleared"


def _grace_keys(report: GraceReport) -> tuple[set[int], set[int], dict[int, str]]:
    """The in-grace rating keys by kind, plus rating key -> title for the announce.

    Items Plex never matched (``plex_rating_key is None``) cannot be addressed on a
    shelf and are excluded here. They get no heads-up on any channel: the Discord
    announce in :func:`announce_new` is built from this same set and dedupes on the
    integer rating key, which an unmatched item does not have. Warning them would need a
    separate per-item key (their ``media_key``); until then, this is a known gap, not a
    guarantee.
    """
    movies: set[int] = set()
    seasons: set[int] = set()
    titles: dict[int, str] = {}
    for item in report.in_grace:
        if item.plex_rating_key is None:
            continue
        if item.media_type == "movie":
            movies.add(item.plex_rating_key)
        elif item.media_type == "season":
            seasons.add(item.plex_rating_key)
        else:
            # A kind the shelf does not know how to place. Excluded rather than guessed
            # into some section where it could be added but never removed.
            continue
        titles[item.plex_rating_key] = item.title
    return movies, seasons, titles


async def sync_section(
    plex: PlexClient,
    *,
    section_key: int,
    section_title: str,
    kind: str,
    in_grace: set[int],
    apply: bool,
) -> ShelfOutcome:
    """Reconcile one library's shelf: the collection and the label together.

    The target set is the in-grace keys that actually live in this section -- the
    intersection scopes every mark to the library that holds the item, which is what
    lets a 4K/HD split or a movie/TV split each carry their own shelf without
    cross-contamination.

    Removals run before adds, so the marked set never briefly over-covers if a later
    add fails partway. A failure anywhere in this section is caught by the caller;
    one unreachable library must not block the rest.
    """

    # The reads run one after another rather than concurrently: the whole-section key dump
    # dominates a section's time, so overlapping the two small reads with it saves a second
    # or two while tripling this section's live Plex connections. The libraries run
    # concurrently instead (sync_shelves), which is where the real overlap is -- and keeping
    # one section to one connection at a time is what keeps the bounded fan-out there under
    # the requests session's connection pool.
    section_keys = await plex.section_rating_keys(section_key, kind=kind)
    target = in_grace & section_keys

    collection_key = await plex.find_collection(section_key, LEAVING_SOON_COLLECTION)
    on_collection = (
        await plex.collection_children(collection_key) if collection_key is not None else set()
    )
    collection_plan = reconcile(target, on_collection)

    labeled = await plex.labeled_in_section(section_key, kind=kind, label=LEAVING_SOON_LABEL)
    label_plan = reconcile(target, labeled)

    if apply and not (collection_plan.is_noop and label_plan.is_noop):
        with benign_shelf_write():
            # Removals first: the shelf must never briefly claim more than is true. When
            # EVERY current member is leaving (a plain clear, or a total list swap where
            # nothing carries over), drop the whole collection in ONE request rather than
            # detaching members one at a time; otherwise detach just the leavers by batch
            # tag-edit.
            clears_collection = (
                bool(on_collection) and set(collection_plan.to_remove) == on_collection
            )
            if collection_key is not None and clears_collection:
                await plex.delete_collection(collection_key)
                collection_key = None  # gone now; a re-add below recreates it from target
            elif collection_key is not None and collection_plan.to_remove:
                await plex.remove_collection_members(
                    section_key,
                    name=LEAVING_SOON_COLLECTION,
                    rating_keys=collection_plan.to_remove,
                )
            if label_plan.to_remove:
                await plex.remove_label(section_key, label_plan.to_remove, LEAVING_SOON_LABEL)
            if collection_plan.to_add:
                if collection_key is None:
                    # No collection (never had one, or the clear above dropped it): Plex
                    # refuses an empty collection, so it is born already holding the full
                    # target set.
                    await plex.create_collection(
                        section_key,
                        kind=kind,
                        name=LEAVING_SOON_COLLECTION,
                        rating_keys=sorted(target),
                    )
                else:
                    await plex.add_to_collection(collection_key, collection_plan.to_add)
            if label_plan.to_add:
                await plex.add_label(section_key, label_plan.to_add, LEAVING_SOON_LABEL)

    # A section whose shelf already matches is APPLIED when writing was allowed: there
    # was nothing to write and nothing failed. Only a preview (apply=False) reports
    # false -- otherwise one quiet library would make a fully-written pass claim
    # "preview only", the exact dishonesty the applied flag exists to prevent.
    return ShelfOutcome(
        section_key=section_key,
        section_title=section_title,
        kind=kind,
        added=len(collection_plan.to_add),
        removed=len(collection_plan.to_remove),
        on_shelf=len(target),
        applied=apply,
    )


async def sync_shelves(
    plex: PlexClient,
    libraries: list[dict[str, Any]],
    *,
    movie_keys: set[int],
    season_keys: set[int],
    apply: bool,
) -> list[ShelfOutcome]:
    """Reconcile every enabled library, movies in movie libraries and seasons in TV
    libraries. A library that fails records its error and the pass continues; partial
    honesty beats all-or-nothing silence for a warning feature. The libraries reconcile
    concurrently -- each is an independent shelf, so nothing is shared but the client, and
    the whole-section read that dominates one library's time overlaps the others' instead of
    queuing behind them. Under SHELF_CONCURRENCY, so many libraries reconcile in waves rather
    than opening one Plex connection each at once. A library's failure is caught inside its
    own task so it can never cancel a sibling; the results stay in library order."""
    bound = asyncio.Semaphore(SHELF_CONCURRENCY)

    async def _reconcile(lib: dict[str, Any]) -> ShelfOutcome:
        section_key = int(lib["key"])
        section_title = str(lib["title"])
        kind = str(lib["kind"])
        in_grace = movie_keys if kind == "movie" else season_keys
        try:
            async with bound:
                return await sync_section(
                    plex,
                    section_key=section_key,
                    section_title=section_title,
                    kind=kind,
                    in_grace=in_grace,
                    apply=apply,
                )
        except PlexError as exc:
            # One unreachable library records its error and the pass carries on; caught
            # here rather than at the gather so a sibling reconcile is never canceled.
            return ShelfOutcome(
                section_key=section_key,
                section_title=section_title,
                kind=kind,
                added=0,
                removed=0,
                on_shelf=0,
                applied=False,
                error=str(exc),
            )

    return list(await gather_reaped(*(_reconcile(lib) for lib in libraries)))


async def announce_new(
    notifier: DiscordNotifier | None,
    report: GraceReport,
    *,
    already_announced: set[int],
) -> tuple[bool, frozenset[int]]:
    """Send the Discord heads-up for anything newly in grace, idempotently.

    Independent of the shelf on purpose: the webhook is the channel that reaches people
    who never open Plex, so it fires for every newly-in-grace item whether or not any
    library carries a shelf. ``already_announced`` is the durable set of rating keys
    announced on previous passes; a title is announced only once per stay in grace. The
    returned set is pruned to items still in grace, so a title that leaves grace and
    later returns is announced afresh; the caller persists it.
    """
    movies, seasons, titles = _grace_keys(report)
    in_grace = movies | seasons
    to_announce = sorted(in_grace - already_announced)

    notified = False
    announced_now: set[int] = set()
    if notifier is not None and to_announce:
        names = [titles[k] for k in to_announce]
        notified = await notifier.announce_leaving_soon(names, grace_days=report.grace_days)
        # Record as announced only if the post actually landed; a failed announce must
        # be retried on the next pass, not silently marked done.
        if notified:
            announced_now = set(to_announce)

    return notified, frozenset((already_announced | announced_now) & in_grace)


# ---------------------------------------------------------------------------
# Orchestration: the one pass the button, the scan hook, and the cleanup share
# ---------------------------------------------------------------------------

#: Serializes a whole Leaving Soon pass against every other one in this process.
#:
#: The announced set is read at the start of a pass and written at the end, with a
#: whole-library Plex reconcile and a Discord post in between -- minutes of network I/O
#: across which nothing else was held back. Two passes overlap easily: the operator presses
#: "Update now" while a scheduled scan is landing, and its after-scan hook fires. Both read
#: the same "already announced", both decide the same title is new, and your users get
#: the same heads-up twice. Worse, the later writer persists a set derived from ITS pre-I/O
#: read, dropping whatever the first pass recorded -- so that title is announced a third
#: time on the next pass. Rule 8 wants the announcement idempotent on the durable set; the
#: set was durable, the read-modify-write around it was not.
#:
#: Held across the reconcile too, not just the announce: a second concurrent whole-section
#: reconcile against the same libraries is wasted work at best.
_pass_lock = per_loop_lock()


async def _plex_client(
    session: AsyncSession, box: SecretBox, settings: Settings
) -> PlexClient | None:
    safety = await app_settings.runtime_safety(session, settings)
    server = (await session.execute(select(PlexServer))).scalars().first()
    if server is None:
        return None
    return PlexClient(
        server.connection_uri,
        box.decrypt(server.token_enc),
        safety=safety,
        verify=server.verify_tls,
    )


async def run_sync(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    box: SecretBox,
) -> LeavingSoonResult:
    """One full Leaving Soon pass: reconcile every enabled library and announce.

    The single implementation behind the Reap-page button and the after-scan hook, so
    the two can never drift. Raises :class:`LeavingSoonDisabledError` when the shelf is
    off, and :class:`PlexError` when no server is linked or none of it is reachable.

    Serialized against every other pass in this process (see :data:`_pass_lock`), because
    the announced set is read at the top and written at the bottom with minutes of network
    I/O in between.
    """
    async with _pass_lock():
        return await _run_pass(session_factory, settings, box)


async def _run_pass(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    box: SecretBox,
) -> LeavingSoonResult:
    """The pass itself. Split from :func:`run_sync` only so the lock wraps it whole."""
    async with session_factory() as session:
        if not await app_settings.leaving_soon_enabled(session):
            raise LeavingSoonDisabledError(
                "Leaving Soon is off. Turn it on in Settings, under Plex, "
                "and Reaper will keep the shelf up to date."
            )
        if await _latest_scan_degraded(session):
            raise LeavingSoonDegradedError(
                "The last scan couldn't be trusted, so the shelf wasn't updated. "
                "Run a scan that finishes cleanly first."
            )
        safety = await app_settings.runtime_safety(session, settings)
        libraries = await app_settings.enabled_plex_libraries(session)
        profile = await active_profile_settings(session)
        report = await grace_report(session, grace_days=profile.grace_days)
        notifier = await build_notifier(session, box, settings)
        already = await app_settings.get_leaving_soon_announced(session)
        # build_notifier may have seeded the webhook from the environment on first read.
        await session.commit()
        # Built LAST of everything this pass gathers. This pass owns the client (rule 34),
        # and its close lives in the try/finally below; building it FIRST, as this used to,
        # put four awaited reads between the construction and the only thing that closes it,
        # so any one of them raising leaked the client and its pooled connections.
        plex = await _plex_client(session, box, settings)

    if plex is None:
        raise PlexError("Leaving Soon needs a linked Plex server. Link one in Settings first.")

    try:
        movie_keys, season_keys, _titles = _grace_keys(report)
        outcomes = await sync_shelves(
            plex,
            libraries,
            movie_keys=movie_keys,
            season_keys=season_keys,
            apply=safety.leaving_soon_write_allowed,
        )
        notified, announced = await announce_new(notifier, report, already_announced=already)
    finally:
        await plex.aclose()

    result = LeavingSoonResult(
        outcomes=outcomes,
        notified=notified,
        announced=announced,
        movies_on_shelves=sum(o.on_shelf for o in outcomes if o.kind == "movie"),
        seasons_on_shelves=sum(o.on_shelf for o in outcomes if o.kind == "show"),
    )

    # The row stores the same two answers the route hands the browser, off one derivation
    # (``ok`` and ``summary``). Nothing downstream re-words this pass.
    async with session_factory() as session:
        await app_settings.set_leaving_soon_announced(session, set(result.announced))
        await app_settings.set_leaving_soon_last(
            session,
            at=utcnow().isoformat(),
            movies=result.movies_on_shelves,
            seasons=result.seasons_on_shelves,
            applied=result.applied,
            ok=result.ok,
            result=result.summary,
        )
        await session.commit()

    log.info(
        "leaving_soon.synced",
        libraries=len(outcomes),
        added=result.added,
        removed=result.removed,
        applied=result.applied,
        notified=result.notified,
        problems=len(result.problems),
    )
    # A library that could not be reached is surfaced here rather than in the UI: the sync
    # is fire-and-forget from Settings, and the operator finds a failure in the logs.
    if result.problems:
        log.warning("leaving_soon.problems", problems=result.problems)
    return result


async def after_scan(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    box: SecretBox,
) -> None:
    """The automatic pass that runs when a scan lands. Best-effort by design.

    A scan changes the grace set, so this is the moment the shelf goes stale. When the
    shelf is on, run the full pass; when it is off but a Discord webhook is set, send
    the heads-up alone (announcing is a read plus a webhook post -- no Plex write). A
    failure here is logged and swallowed: the warning layer must never fail a scan that
    already committed.

    Every skip below is written down (:func:`_record_skip`), because none of them reach the
    row ``_run_pass`` writes: the Jobs page then re-read the last COMPLETED pass and showed
    its green dot, its timestamp and its counts as the answer for a scan that never touched
    the shelf, under a heading reading "Runs after every scan".
    """
    # The shelf is on until the pass says otherwise. Off is the one skip not worth writing
    # down: the Jobs row renders its "Off" branch from that same setting and shows no
    # last-run line at all, so there is nothing on screen for a record to correct.
    shelf_on = True
    # Whether a specific reason is already stored for this call, so the catch-all below
    # cannot overwrite it with the vague one.
    recorded = False
    try:
        try:
            await run_sync(session_factory, settings, box)
            return
        except LeavingSoonDisabledError:
            shelf_on = False
        except LeavingSoonDegradedError:
            # The scan that triggered this could not be trusted. There is deliberately NO
            # fall-through to the heads-up below: it reads the same condemned set (rule
            # 116), so "shelf off" and "scan untrustworthy" are not the same skip.
            log.info("leaving_soon.skipped_degraded")
            await _record_skip(session_factory, "the scan didn't finish cleanly")
            return
        except PlexError as exc:
            # Shelf on, but Plex is unlinked or unreachable. Fall through to the Discord
            # heads-up below, but do not swallow it silently: an operator who enabled the
            # shelf and sees it not updating has no other trail to this cause.
            log.warning("leaving_soon.shelf_unreachable", error=str(exc))
            await _record_skip(session_factory, "Reaper couldn't reach Plex")
            recorded = True

        # Shelf off, or on with no reachable server: the Discord heads-up still runs
        # whenever a webhook is set. Announcing is a read plus a webhook post, no Plex
        # write, so it does not depend on the shelf reconcile having succeeded. run_sync
        # raises PlexError before it announces, so nothing here double-announces.
        #
        # Under the same lock as the full pass, and for the same reason: this is the read-
        # announce-write in its barest form, and for an operator running Leaving Soon with
        # the shelf off it is not an edge case, it is the ONLY path that ever announces.
        # run_sync has already released the lock by the time it raises its way to here.
        async with _pass_lock():
            async with session_factory() as session:
                # Checked again on this path, because it is reached when the SHELF is off,
                # which returns before the guard in _run_pass ever runs. Announcing is the
                # only thing this branch does, and an announcement of a title about to be
                # removed is exactly a side effect of the condemned set (rule 116).
                if await _latest_scan_degraded(session):
                    log.info("leaving_soon.announce_skipped_degraded")
                    return
                notifier = await build_notifier(session, box, settings)
                if notifier is None:
                    return
                profile = await active_profile_settings(session)
                report = await grace_report(session, grace_days=profile.grace_days)
                already = await app_settings.get_leaving_soon_announced(session)
                await session.commit()

            notified, announced = await announce_new(notifier, report, already_announced=already)
            async with session_factory() as session:
                await app_settings.set_leaving_soon_announced(session, set(announced))
                await session.commit()
        if notified:
            log.info("leaving_soon.announced_without_shelf", count=len(announced))
    except Exception as exc:
        log.warning("leaving_soon.after_scan_failed", error=str(exc))
        # Not on the shelf-off path, which has no line on screen to correct, and not over a
        # reason already written above. What is left is a surprise nobody has a name for,
        # and the row still has to say the shelf did not move.
        if shelf_on and not recorded:
            await _record_skip(session_factory, "the update didn't finish")


async def _record_skip(
    session_factory: async_sessionmaker[AsyncSession],
    result: str,
) -> None:
    """Write down that a scan finished without updating the shelf, and why in one clause.

    Read beside the last completed pass and preferred only while it is newer, so a pass that
    later completes wins on its own timestamp and this is never cleared -- the arrangement
    ``ScanRow`` already uses for a scheduled scan that crashed and wrote no snapshot.

    Never lets this bookkeeping break the scan it follows: a failed write is logged and
    swallowed, exactly as :func:`after_scan` handles its own errors.
    """
    try:
        async with session_factory() as session:
            await app_settings.set_leaving_soon_last_skip(
                session, at=utcnow().isoformat(), result=result
            )
            await session.commit()
    except Exception as exc:
        log.warning("leaving_soon.record_skip_failed", error=str(exc))


async def cleanup_sections(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    box: SecretBox,
    *,
    sections: list[dict[str, Any]],
) -> bool:
    """Take everything off the given libraries' shelves, so nothing stale lingers.

    The same per-library reconcile with an empty target set -- every current member is
    leaving, so each library drops its whole collection in one request (delete_collection)
    and the labels come off with it. Writes only when the guard allows (armed, or the
    read-only opt-in); otherwise this is a no-op and the marks stay until Reaper is next
    allowed to write. Returns whether the cleanup actually ran. Best-effort: a failure is
    logged, never raised, because turning a warning off must always succeed.
    """
    if not sections:
        return False
    try:
        async with session_factory() as session:
            safety = await app_settings.runtime_safety(session, settings)
            # The guard first, the client second (rule 34). Read-only is the DEFAULT state,
            # so building the client before checking it leaked one -- with its pooled
            # connections -- every time a library or the whole shelf was switched off while
            # deletion was not armed, which is the common way this is reached.
            if not safety.leaving_soon_write_allowed:
                return False
            plex = await _plex_client(session, box, settings)

        if plex is None:
            return False

        try:
            outcomes = await sync_shelves(
                plex, sections, movie_keys=set(), season_keys=set(), apply=True
            )
        finally:
            await plex.aclose()

        removed = sum(o.removed for o in outcomes)
        failed = sum(1 for o in outcomes if o.error)
        log.info(
            "leaving_soon.cleaned_up", sections=len(sections), removed=removed, problems=failed
        )
        return True
    except Exception as exc:
        log.warning("leaving_soon.cleanup_failed", error=str(exc))
        return False


async def cleanup_shelves(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    box: SecretBox,
) -> bool:
    """Take everything off every enabled library's shelf: the last pass when the whole
    feature is turned off. (A single library toggled off gets the same treatment through
    ``cleanup_sections`` -- see the libraries route -- so neither path strands a shelf.)
    """
    async with session_factory() as session:
        libraries = await app_settings.enabled_plex_libraries(session)
    return await cleanup_sections(session_factory, settings, box, sections=libraries)
