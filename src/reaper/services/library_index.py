# SPDX-License-Identifier: AGPL-3.0-or-later
"""Building a Plex library index for the scan's identity matching.

One implementation for both media types -- ``snapshot.build_movie_index`` and
``season_scan.build_tv_index`` are thin wrappers -- so a fix to the pagination, the
staleness handling, or the failure semantics cannot land on one path and miss the
other. The two indexes feed the same resolver, and the movie and TV libraries
silently diverging in what enters the frozen snapshot is exactly the kind of drift
rule 3 exists to prevent.

The Tautulli ``get_library_media_info`` sweep is the **spine** -- it alone gives
rating_key / title / year / added_at cheaply, and for every row it lists,
``added_at`` keeps coming from there so dormancy stays byte-identical to the
title-only era. The plexapi sweep enriches each spine row with external ids + file
basenames, joined by rating key.

The spine is a Tautulli-side *cache*, though, and it lags: an item added to Plex
since Tautulli's last library refresh is absent from the listing (verified live: a
day-old item missing from the media-info listing while Tautulli's own get_metadata
served it fine). Spine-only, that item never enters the index, so the resolver
reports it unmatched -- kept, but with a false "Plex has not matched this item"
explanation. The plexapi sweep walks the same sections directly, so any rating key
it returns that the spine did not list is appended as its own row, carrying Plex's
own added-at (there is no Tautulli value to preserve for a row Tautulli has not
listed yet).

A plexapi sweep that fails **degrades** the snapshot (rule 2: never let the id
signal vanish and silently fall the whole library back to title-only) and leaves
ids empty, so items still match by title+year but no run may execute against the
result. A deployment with no Plex configured simply gets no enrichment.

The sweep and the spine read different services, so they run concurrently and are
joined only afterwards; the pairing goes through ``aio.gather_reaped`` so a spine
failure aborts the scan exactly as it did when the reads were sequential, with the
sweep reaped rather than left running. The sweep itself never raises -- it degrades
and returns empty.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import structlog

from reaper.aio import gather_reaped
from reaper.clients.plex import PlexClient, PlexError
from reaper.clients.tautulli import TautulliClient
from reaper.clock import from_epoch
from reaper.engine import identity

log = structlog.get_logger(__name__)

#: Where ``_spine`` stashes each row's library (section) title, for the item build loop to
#: read as the fallback when the plexapi sweep did not enrich the row. Underscore-prefixed so
#: it cannot collide with a real Tautulli field.
_SPINE_LIBRARY = "_reaper_library"


def _as_year(value: Any) -> int | None:
    """A row's release year, or ``None`` -- used only to disambiguate duplicate titles.

    Tautulli returns years as ints or numeric strings; anything else reads as unknown.
    """
    if isinstance(value, int | str) and str(value).isdigit():
        return int(value)
    return None


async def build_index(
    tautulli: TautulliClient,
    plex: PlexClient | None,
    *,
    section_type: str,
    degrade: Callable[[str], None],
    allowed_sections: set[int] | None = None,
) -> identity.PlexIndex:
    """The Plex library of one ``section_type``, inverted for id / basename / title
    matching. See the module docstring for the spine + sweep design.

    ``allowed_sections`` scopes both reads to the libraries the operator included in scans
    (Settings -> Plex): ``None`` means every library of this type, a set restricts to those
    section keys. The **spine and the sweep are filtered on the same set** -- filtering only
    one would leave the rating-key join reading a section the other never listed, and an
    inconsistent join is exactly the drift rule 3 forbids. Scoping only ever removes an
    item's enrichment (it then resolves unmatched and is kept), never adds condemnation.
    """

    async def _sweep() -> dict[int, identity.PlexItem]:
        if plex is None:
            return {}
        try:
            return await plex.library_guid_index(
                section_type=section_type, allowed_sections=allowed_sections
            )
        except PlexError as exc:
            degrade(
                f"Plex GUID sweep failed ({exc}): id matching unavailable, snapshot un-executable"
            )
            return {}

    async def _spine() -> list[Mapping[str, Any]]:
        collected: list[Mapping[str, Any]] = []
        for library in await tautulli.libraries():
            if library.get("section_type") != section_type:
                continue
            section_id = int(library["section_id"])
            if allowed_sections is not None and section_id not in allowed_sections:
                continue
            # The library title, stamped onto each of its rows so the item build loop has a
            # library even for a row the plexapi sweep did not (or could not) enrich.
            section_name = str(library.get("section_name") or "") or None
            start = 0
            while True:
                page = await tautulli.library_media_info(section_id, start=start, length=1000)
                rows = page.get("data") or []
                collected.extend({**row, _SPINE_LIBRARY: section_name} for row in rows)
                if len(rows) < 1000:
                    break
                start += 1000
        return collected

    plex_items, spine_rows = await gather_reaped(_sweep(), _spine())

    items: list[identity.PlexItem] = []
    for row in spine_rows:
        # A row with no rating key cannot become a candidate's join (its rating_key
        # read would fail), so it is dropped -- identically for movies and shows.
        rk = row.get("rating_key")
        if rk is None:
            continue
        rating_key = int(rk)
        enriched = plex_items.get(rating_key)
        items.append(
            identity.PlexItem(
                rating_key=rating_key,
                title=str(row.get("title") or ""),
                year=_as_year(row.get("year")),
                added_at=from_epoch(row.get("added_at")),
                ids=enriched.ids if enriched is not None else identity.ExternalIds(),
                file_basename=enriched.file_basename if enriched is not None else None,
                files=enriched.files if enriched is not None else (),
                # Display metadata from the plexapi sweep; rows the sweep did not list
                # (or a failed sweep) simply carry none of it. Shows carry no media, so
                # video_resolution stays None for them by construction.
                video_resolution=(enriched.video_resolution if enriched is not None else None),
                content_rating=enriched.content_rating if enriched is not None else None,
                runtime_minutes=(enriched.runtime_minutes if enriched is not None else None),
                ratings=enriched.ratings if enriched is not None else (),
                # The sweep's section title when it enriched this row, else the one the spine
                # stamped from Tautulli's own library listing -- so a row the sweep missed (or
                # a failed sweep) still carries its library.
                library=(
                    enriched.library
                    if (enriched is not None and enriched.library)
                    else row.get(_SPINE_LIBRARY)
                ),
            )
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
        )
    else:
        # The denominator for every "why didn't my item match" question, and per scan (once
        # or twice), not per item, so it is safe at info. A large ``fresh`` count means the
        # Tautulli spine is lagging Plex -- items present in Plex it has not listed yet.
        log.info(
            "library_index.built",
            section_type=section_type,
            spine_rows=len(spine_rows),
            swept=len(plex_items),
            items=len(items),
            fresh=len(fresh),
        )
    return identity.PlexIndex.build(items)
