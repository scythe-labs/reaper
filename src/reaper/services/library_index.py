# SPDX-License-Identifier: AGPL-3.0-or-later
"""Building a Plex library index for the scan's identity matching.

One implementation serves both media types: ``snapshot.build_movie_index`` and
``season_scan.build_tv_index`` are thin wrappers around it, so a fix to the pagination,
the staleness handling, or the failure semantics lands on both paths at once instead of
missing one. The two indexes feed the same resolver, and letting the movie and TV
libraries silently diverge in what enters the frozen snapshot is exactly the kind of
drift this module exists to prevent.

The Tautulli ``get_library_media_info`` sweep is the **spine**. It alone gives
rating_key, title, year and added_at cheaply, and for every row it lists, ``added_at``
keeps coming from there so dormancy stays byte-identical to the title-only era. The
plexapi sweep enriches each spine row with external ids and file basenames, joined by
rating key.

**The spine walk ends on Tautulli's own reported count, never on a page coming back
short.** A server may clamp a page below the length asked for, so a short page says
nothing about whether the library ended. Reading a short page as the end would
silently admit only a fraction of a section. Every item the walk never listed resolves
unmatched, which keeps it but explains it as "Plex has not matched this", so a walk
that ends before the count degrades the snapshot like any other read anomaly here. A
server that serves rows but reports no count at all is paged until a page comes back
empty, bounded by ``_SPINE_MAX_PAGES``.

The spine is a Tautulli-side cache, and it lags: an item added to Plex since
Tautulli's last library refresh is absent from the listing. Verified live: a day-old
item was missing from the media-info listing while Tautulli's own get_metadata served
it fine. Read from the spine alone, that item would never enter the index, and the
resolver would report it unmatched, kept, but with a false "Plex has not matched this
item" explanation. The plexapi sweep walks the same sections directly, so any rating
key it returns that the spine did not list is appended as its own row, carrying
Plex's own added-at (there is no Tautulli value to preserve for a row Tautulli has
not listed yet).

**The cache lags in both directions, and the other direction is the dangerous one.**
A rating key the spine still lists that the sweep did not return names an item Plex
no longer has: re-matched, replaced, or deleted. Admitting it builds a phantom row
into the index: a stale title, year and added-at, with no ids and no file name,
because there was nothing left to enrich it from. Carrying no ids and no basename, it
can only ever match through the resolver's weakest tier, title plus year, where it
causes real harm twice. It can block a good match, by naming a different row than the
file name did, which makes the item abstain and tells the operator Plex holds several
copies of a file it holds one copy of. Worse, it can create a false match on its own:
with no id hit and a file name the real row does not carry (an ordinary *arr rename),
title plus year is the last tier standing, and the item binds to a rating key Plex
returns 404 for. That reads as matched, so the fact layer takes its affirmative
branch: ``watchers_window.get(rating_key, 0)`` becomes ``Known(0)``, a measurement
rather than ``Unknown``, and dormancy anchors on the phantom's stale added-at. Nobody
can have watched a row that does not exist, so a live file collects maximum deletion
pressure at full confidence from an item that is gone. So a spine row the sweep did
not return is dropped instead, and the item resolves unmatched, which keeps it.

That drop only happens once the sweep has actually run. A failed sweep and an
unconfigured Plex both return an empty map, and reading "not in the sweep" as "not
in Plex" there would retire the whole library on the strength of a read that never
happened. Both cases leave the spine's rows in place, and the snapshot is degraded
and unexecutable anyway in the failure case. Both reads are filtered on the same
``allowed_sections`` set, but that is where the guarantee stops: the spine enumerates
Tautulli's cached library list and the sweep enumerates Plex's live sections, so the
two can still disagree about which libraries exist. A large gap means they disagree
about scope rather than about a handful of retired items, so that degrades the
snapshot too. It is measured per library, since a whole section vanishing is the case
worth catching, and an overall share would hide it behind the healthy libraries.

A plexapi sweep that fails degrades the snapshot: the id signal must never vanish and
silently fall the whole library back to matching by title and year alone. Items still
match by title and year, but no run may execute against the result. A sweep that
succeeds but could not read every item's ratings degrades too, without discarding the
ids it did read (``plex.collecting_incomplete_reads``, opened around the gather
below): a title whose ratings went missing is a title the rating bar can no longer
keep. A deployment with no Plex configured simply gets no enrichment.

The sweep and the spine read different services, so they run concurrently and are
joined only afterwards. The pairing goes through ``aio.gather_reaped``, so a spine
failure aborts the scan exactly as it did when the reads were sequential, with the
sweep reaped rather than left running. Neither read raises on its own: a failure, and
every malformed shape either can produce, degrades the snapshot and is skipped
instead. That covers a listing entry that is not an object, a library with no usable
id, a media row that is not an object, an item with no usable rating key, and a page
count that is not a number, the five shapes a response Reaper did not write could
otherwise throw straight through ``gather_reaped``. So a bad source costs the
operator a plan, never the whole scan.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from typing import Any

import structlog

from reaper.aio import gather_reaped
from reaper.clients.base import IntegrationError
from reaper.clients.plex import PlexClient, PlexError, collecting_incomplete_reads
from reaper.clients.tautulli import TautulliClient
from reaper.clock import from_epoch
from reaper.engine import identity

log = structlog.get_logger(__name__)

#: Where ``_spine`` stashes each row's library (section) title, for the item build loop to
#: read as the fallback when the plexapi sweep did not enrich the row. Underscore-prefixed so
#: it cannot collide with a real Tautulli field.
_SPINE_LIBRARY = "_reaper_library"

#: The same stamp for the section's *id*, which is what the retirement share is bucketed on.
#: Keyed on the id rather than on ``_SPINE_LIBRARY``'s title, because two libraries may
#: share a display name, and merging their buckets would hide a whole vanished section
#: inside a healthy same-named one, the very failure the per-section share exists to catch.
_SPINE_SECTION = "_reaper_section_id"

#: How much of the spine may be retired as "Plex no longer has this" before the scan stops
#: believing the two reads covered the same libraries. A stale Tautulli cache retires a
#: handful of items. A section the sweep never walked retires all of it, and every item in
#: that library would then resolve unmatched with nothing announcing why. Both bounds must
#: be passed, so a tiny library cannot degrade the snapshot on a single retired row.
#:
#: **The share is measured per section, against that section's own spine rows.** Measured
#: across every section of the type instead, it could not catch the case it exists for: a
#: 150-row library beside a 2,000-row one could vanish whole (150 is above the floor, but
#: below 10% of 2,150) and report nothing, exactly the "section the sweep never walked" case
#: going unannounced. The floor stays global, because one stale row in each of twenty
#: libraries really is a lagging cache, not a scope mismatch.
_RETIRED_DEGRADE_SHARE = 0.1
_RETIRED_DEGRADE_FLOOR = 20

#: How many rows the spine asks for per page, and how many pages one library may take. The
#: client's own default is 100, and a whole library is read on every scan, so the size is
#: set here rather than inherited. The page cap bounds one library at a million rows, orders
#: of magnitude past a real one, so it can only ever trigger on a server that is not
#: advancing through the listing at all. That is exactly what this walk needs to catch,
#: since ending on a short page was the bug being fixed, and the reported count is the only
#: other thing that ends the walk (``history_sync.MAX_HISTORY_PAGES`` follows the same
#: pattern).
_SPINE_PAGE_SIZE = 1_000
_SPINE_MAX_PAGES = 1_000


def _as_year(value: Any) -> int | None:
    """A row's release year, or ``None``. Used only to disambiguate duplicate titles.

    Tautulli returns years as ints or numeric strings. Anything else reads as unknown.
    """
    if isinstance(value, int | str) and str(value).isdigit():
        return int(value)
    return None


def _as_count(value: Any) -> int | None:
    """A page envelope's row count, or ``None`` for "the server did not tell us".

    Zero folds into ``None`` deliberately. ``int(value or 0)`` would make a Tautulli that
    omits the field indistinguishable from one reporting an empty library, and the walk
    would then stop after page one having read a library it never finished. ``history_sync``
    carries the same note over the same field. Anything that is not a plain number reads as
    not-told too, because this module's contract is that a shape Reaper did not write
    degrades the snapshot rather than raising out of the scan.
    """
    if isinstance(value, int | str) and str(value).isdigit():
        return int(value) or None
    return None


async def build_index(
    tautulli: TautulliClient,
    plex: PlexClient | None,
    *,
    section_type: str,
    degrade: Callable[[str], None],
    allowed_sections: set[int] | None = None,
) -> identity.PlexIndex:
    """The Plex library of one ``section_type``, inverted for id, basename and title
    matching. See the module docstring for the spine and sweep design.

    ``allowed_sections`` scopes both reads to the libraries the operator included in scans
    (Settings, Plex): ``None`` means every library of this type, and a set restricts to
    those section keys. The spine and the sweep are filtered on the same set. Filtering
    only one would leave the rating-key join reading a section the other never listed,
    which is exactly the kind of drift this module exists to prevent. Scoping only ever
    removes an item's enrichment, so it resolves unmatched and is kept, never adds
    deletion pressure.
    """
    # This function runs twice per scan: movies from `snapshot.build_movie_index`, shows
    # from `season_scan.build_tv_index`, against one shared `degraded_reasons` list. A
    # single outage reaches every `degrade` call below twice, so without a lane name it
    # appended the identical sentence twice and read as two separate failures. Naming the
    # lane tells the two apart. It rides every message from here rather than being written
    # into each one by hand, so a message added later cannot forget it.
    lane = "Movies" if section_type == "movie" else "TV shows"
    _shared_degrade = degrade

    def _lane_degrade(reason: str) -> None:
        _shared_degrade(f"{lane}: {reason}")

    # Rebound, rather than introduced under a second name, so `degrade` is the only one in
    # scope below and a message added later cannot reach the unprefixed callback by habit.
    degrade = _lane_degrade

    async def _sweep() -> tuple[dict[int, identity.PlexItem], bool]:
        """The sweep's items, and whether it actually ran for the scoped sections.

        The flag is not ``bool(items)``. A genuinely empty library and a sweep that never
        ran both return an empty map, and only the first of them lets the caller read
        "absent from the sweep" as "Plex does not have this" (see the module docstring). A
        failed or unconfigured sweep says nothing, so it retires nothing.
        """
        if plex is None:
            return {}, False
        try:
            return (
                await plex.library_guid_index(
                    section_type=section_type, allowed_sections=allowed_sections
                ),
                True,
            )
        except PlexError as exc:
            # Plain language, because this text reaches the operator directly, in the
            # incomplete-scan notice shown on three screens. It says what happened and what
            # it means for their files, not the internal name of the read that failed.
            degrade(
                f"Reaper couldn't read your Plex libraries ({exc}), so nothing in this scan "
                "could be matched to your libraries and nothing may be deleted from it"
            )
            return {}, False

    async def _spine() -> list[Mapping[str, Any]]:
        try:
            return await _spine_rows()
        except IntegrationError as exc:
            # Same treatment the plexapi sweep already gets: degrade, return nothing, and
            # let the scan finish. A raise here would propagate through gather_reaped and
            # kill the whole run with no viewable snapshot, so a Tautulli hiccup would cost
            # the operator their scan instead of costing them a plan. With no spine rows,
            # every item resolves unmatched, which keeps it.
            degrade(
                f"the Plex library listing could not be read ({exc}), so nothing in this "
                "scan could be matched to your libraries"
            )
            return []

    async def _spine_rows() -> list[Mapping[str, Any]]:
        collected: list[Mapping[str, Any]] = []
        malformed = 0
        # Widened to Any deliberately: the client types this as a list of dicts, but the
        # value is whatever the remote returned, so the type is a claim rather than a
        # guarantee and the guard below has to stay reachable.
        listing: list[Any] = list(await tautulli.libraries())
        for library in listing:
            if not isinstance(library, Mapping):
                # A listing entry that is not an object at all. Counted rather than read,
                # because ``library.get(...)`` on it raises AttributeError straight out of
                # ``gather_reaped`` and kills the scan, the one outcome this module's
                # contract promises cannot happen.
                malformed += 1
                continue
            if library.get("section_type") != section_type:
                continue
            try:
                section_id = int(library["section_id"])
            except (KeyError, TypeError, ValueError):
                # A library row with no usable section id cannot be listed, so everything
                # in that library would quietly fall out of the index and read as "Plex
                # has not matched this", a whole library judged on nothing. Skip it and
                # degrade the snapshot, rather than raising out of a read the module
                # contract says never raises.
                degrade(
                    "one of your libraries came back from Tautulli without an id, so "
                    "nothing in it could be matched and nothing may be deleted from "
                    "this scan"
                )
                continue
            if allowed_sections is not None and section_id not in allowed_sections:
                continue
            # The library title, stamped onto each of its rows so the item build loop has a
            # library even for a row the plexapi sweep did not (or could not) enrich.
            section_name = str(library.get("section_name") or "") or None
            start = 0
            pages = 0
            total: int | None = None
            capped = False
            while True:
                page = await tautulli.library_media_info(
                    section_id, start=start, length=_SPINE_PAGE_SIZE
                )
                pages += 1
                rows = page.get("data") or []
                if total is None:
                    # Tautulli's own count for this section, read the way ``history_sync``
                    # reads it off the same API. No search filter is sent, so
                    # ``recordsFiltered`` counts the whole section. ``recordsTotal`` is the
                    # same number, and stands in for it where only ``recordsTotal`` is
                    # served.
                    total = _as_count(page.get("recordsFiltered")) or _as_count(
                        page.get("recordsTotal")
                    )
                if not rows:
                    break
                # Paging always advances on the RAW page length, never the filtered one: a
                # malformed row must not shorten a page and end the walk early, which would
                # silently truncate the library.
                usable = [row for row in rows if isinstance(row, Mapping)]
                malformed += len(rows) - len(usable)
                collected.extend(
                    {**row, _SPINE_LIBRARY: section_name, _SPINE_SECTION: section_id}
                    for row in usable
                )
                # By what the page actually held, never by the constant: a server that clamps
                # the page would otherwise have `start` step over the rows it did not serve.
                start += len(rows)
                if total is not None and start >= total:
                    break
                if pages >= _SPINE_MAX_PAGES:
                    # A reported count ends the walk long before this at any library size
                    # that exists, so reaching it means the server is serving rows,
                    # reporting no count, and ignoring `start`. Stop rather than spin.
                    log.warning("library_index.page_cap", section_id=section_id, fetched=start)
                    capped = True
                    break
            if capped or (total is not None and start < total):
                # Without this warning, a library read only in part looks read in full:
                # every item the walk never listed resolves unmatched, which keeps it, and
                # the why-panel then explains a live file as one Plex has not matched.
                counted = f"{start} of {total}" if total is not None else str(start)
                degrade(
                    f"Tautulli listed only {counted} items in one of your libraries, so the "
                    "rest could not be matched and nothing may be deleted from this scan"
                )
        if malformed:
            degrade(
                f"{malformed} row(s) in your Plex library listing could not be read, so "
                "nothing may be deleted from this scan"
            )
        return collected

    # The collector is opened around the gather so it is in place before the sweep task is
    # created (the task copies this context). A sweep that succeeded but could not read
    # every item's ratings files its reason here rather than raising. The ids it returned
    # are complete, so matching is unharmed, but a title whose ratings went missing is one
    # the rating bar can no longer keep, and a withdrawn protection degrades the snapshot.
    with collecting_incomplete_reads() as incomplete:
        (plex_items, swept), spine_rows = await gather_reaped(_sweep(), _spine())
    for reason in incomplete:
        degrade(f"{reason}. Nothing may be deleted from this scan.")

    items: list[identity.PlexItem] = []
    unusable = 0
    retired = 0
    # Retired and considered rows per section, so the share below is measured against the
    # library the rows came from. ``considered`` counts only rows that reached the enrichment
    # look-up, so rows already dropped for a missing or unusable id cannot inflate the
    # denominator and hold the degrade back.
    retired_by_section: dict[object, int] = defaultdict(int)
    considered_by_section: dict[object, int] = defaultdict(int)
    for row in spine_rows:
        # A row with no rating key, or one that is not a number, cannot become a
        # candidate's join (its rating_key read would fail), so it is dropped, identically
        # for movies and shows. An item missing from the index resolves unmatched, which
        # keeps it.
        #
        # Only the malformed case is counted and degrades the snapshot. A row with no
        # rating_key at all is a row Tautulli has not tied to Plex yet, which is an
        # ordinary state on a library mid-scan. A rating_key that is present and not a
        # number is a row that should have joined and could not, which is evidence this
        # scan lost.
        rk = row.get("rating_key")
        if rk is None:
            continue
        try:
            rating_key = int(rk)
        except (TypeError, ValueError):
            unusable += 1
            continue
        considered_by_section[row.get(_SPINE_SECTION)] += 1
        enriched = plex_items.get(rating_key)
        if enriched is None and swept:
            # Tautulli still lists it, but Plex, asked directly over the same sections,
            # does not. The item is gone, and a row for it would be a phantom the title
            # tier can bind a live file to (see the module docstring). Dropping it
            # resolves that file unmatched, which keeps it.
            retired += 1
            retired_by_section[row.get(_SPINE_SECTION)] += 1
            continue
        items.append(
            identity.PlexItem(
                rating_key=rating_key,
                title=str(row.get("title") or ""),
                year=_as_year(row.get("year")),
                added_at=from_epoch(row.get("added_at")),
                ids=enriched.ids if enriched is not None else identity.ExternalIds(),
                file_basename=enriched.file_basename if enriched is not None else None,
                files=enriched.files if enriched is not None else (),
                # Display metadata from the plexapi sweep. Rows the sweep did not list, or
                # a failed sweep, simply carry none of it. Shows carry no media, so
                # video_resolution stays None for them by construction.
                video_resolution=(enriched.video_resolution if enriched is not None else None),
                content_rating=enriched.content_rating if enriched is not None else None,
                runtime_minutes=(enriched.runtime_minutes if enriched is not None else None),
                ratings=enriched.ratings if enriched is not None else (),
                # The sweep's section title when it enriched this row, or else the one the
                # spine stamped from Tautulli's own library listing, so a row the sweep
                # missed, or a failed sweep, still carries its library.
                library=(
                    enriched.library
                    if (enriched is not None and enriched.library)
                    else row.get(_SPINE_LIBRARY)
                ),
            )
        )

    if unusable:
        degrade(
            f"{unusable} items in your libraries came back from Tautulli without a usable "
            "id, so they could not be matched and nothing may be deleted from this scan"
        )

    # Past the floor and past the share of any one library, "Tautulli is a little behind"
    # stops explaining it. The likelier story is a library the sweep never walked, and
    # every item in it just became impossible to judge. Keeping the file is still the
    # outcome, but a scan that quietly decided it can see nothing is worse than one that
    # says so. This checks any one section rather than the overall share, so a small
    # library vanishing whole beside a large healthy one is announced instead of being
    # averaged away.
    if retired > _RETIRED_DEGRADE_FLOOR and any(
        count > _RETIRED_DEGRADE_SHARE * considered_by_section[section]
        for section, count in retired_by_section.items()
    ):
        degrade(
            f"{retired} items in Tautulli's library list are no longer in Plex, so the two "
            "don't line up and nothing may be deleted from this scan. Refreshing the "
            "libraries in Tautulli usually fixes it."
        )

    # Items Plex has that the Tautulli cache has not listed yet (fresh additions).
    spine_keys = {item.rating_key for item in items}
    fresh = [row for rk, row in plex_items.items() if rk not in spine_keys]
    items.extend(fresh)

    if not items:
        # An empty index makes every downstream item resolve unmatched, flooding the log
        # with per-item "Plex has not matched this" warnings and no cause. Almost always a
        # section-scope that excluded every library of this type, or a wrong section_type.
        # One line here names the real reason.
        log.warning(
            "library_index.empty",
            section_type=section_type,
            allowed_sections=sorted(allowed_sections) if allowed_sections else None,
            spine_rows=len(spine_rows),
            swept=len(plex_items),
            retired=retired,
        )
    else:
        # The denominator for every "why didn't my item match" question, and per scan (once
        # or twice), not per item, so it is safe at info. The spine lags Plex in both
        # directions: ``fresh`` counts items Plex has that it has not listed yet, ``retired``
        # items it still lists that Plex no longer has.
        log.info(
            "library_index.built",
            section_type=section_type,
            spine_rows=len(spine_rows),
            swept=len(plex_items),
            items=len(items),
            fresh=len(fresh),
            retired=retired,
        )
    return identity.PlexIndex.build(items)
