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
) -> identity.PlexIndex:
    """The Plex library of one ``section_type``, inverted for id / basename / title
    matching. See the module docstring for the spine + sweep design."""

    async def _sweep() -> dict[int, identity.PlexItem]:
        if plex is None:
            return {}
        try:
            return await plex.library_guid_index(section_type=section_type)
        except PlexError as exc:
            degrade(
                f"Plex GUID sweep failed ({exc}) -- id matching unavailable, snapshot un-executable"
            )
            return {}

    async def _spine() -> list[Mapping[str, Any]]:
        collected: list[Mapping[str, Any]] = []
        for library in await tautulli.libraries():
            if library.get("section_type") != section_type:
                continue
            section_id = int(library["section_id"])
            start = 0
            while True:
                page = await tautulli.library_media_info(section_id, start=start, length=1000)
                rows = page.get("data") or []
                collected.extend(rows)
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
            )
        )

    # Items Plex has that the Tautulli cache has not listed yet (fresh additions).
    spine_keys = {item.rating_key for item in items}
    items.extend(row for rk, row in plex_items.items() if rk not in spine_keys)
    return identity.PlexIndex.build(items)
