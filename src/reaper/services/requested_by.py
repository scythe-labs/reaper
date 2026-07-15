# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who asked for a title -- resolved at scan time, for display and filtering.

The review queue lets you filter to "media someone requested", and each item shows who
asked. That needs a join from Seerr's requests to the items the scan is judging.

**The join is on external ids (tmdb / tvdb), not on the arr instance id.** It is tempting
to match Seerr's ``serviceId`` to Reaper's instance id, but they are different numbering
schemes -- Seerr indexes its own configured services, Reaper its own rows -- so they do
not line up. The tmdb/tvdb ids do, and they are present on both sides.

**This is display-only, and deliberately loose.** "Requested by" is never a gate and
never condemns anything; the worst a wrong match can do is show the wrong name on a card.
So a rare cross-edition id collision is acceptable here in a way it never would be on the
delete path. If Seerr is absent or unreachable, the map is simply empty.
"""

from __future__ import annotations

import structlog

from reaper.clients.base import IntegrationError
from reaper.clients.seerr import SeerrClient

log = structlog.get_logger(__name__)


def movie_key(tmdb_id: int | None) -> str | None:
    """The requested-map key for a movie, from its TMDb id."""
    return f"movie:tmdb:{tmdb_id}" if tmdb_id else None


def show_key(tvdb_id: int | None) -> str | None:
    """The requested-map key for a whole show, from its TVDb id."""
    return f"tv:tvdb:{tvdb_id}" if tvdb_id else None


def season_key(tvdb_id: int | None, season: int) -> str | None:
    """The requested-map key for one season of a show."""
    return f"tv:tvdb:{tvdb_id}:{season}" if tvdb_id else None


def _name(request_display: str | None, request_user: str | None) -> str:
    return (request_display or request_user or "a user").strip() or "a user"


async def build_map(seerr: SeerrClient | None) -> dict[str, str]:
    """Build ``external-id-key -> requester name`` from every available Seerr request.

    A movie maps under its tmdb key; a show maps under its show key *and* under a key per
    requested season, so a season can be matched whether the request named specific
    seasons or the whole series. When several people requested the same thing, the map
    keeps a friendly "Name + N others" so the card can say so.
    """
    if seerr is None:
        return {}

    try:
        requests = await seerr.all_requests(filter_="available")
    except IntegrationError as exc:
        # Soft: a missing "requested by" is a blank tag, never a failed scan.
        log.warning("requested_by.seerr_unreachable", error=str(exc))
        return {}

    # key -> ordered list of distinct requester names, so we can render "+ N others".
    names: dict[str, list[str]] = {}

    def add(key: str | None, name: str) -> None:
        if key is None:
            return
        bucket = names.setdefault(key, [])
        if name not in bucket:
            bucket.append(name)

    for req in requests:
        name = _name(req.requester.display_name, req.requester.username)
        if req.media_type == "movie":
            add(movie_key(req.tmdb_id), name)
        else:
            add(show_key(req.tvdb_id), name)
            for season in req.seasons:
                add(season_key(req.tvdb_id, season), name)

    result: dict[str, str] = {}
    for key, bucket in names.items():
        if len(bucket) == 1:
            result[key] = bucket[0]
        else:
            result[key] = f"{bucket[0]} + {len(bucket) - 1} other{'s' if len(bucket) > 2 else ''}"

    log.info("requested_by.built", keys=len(result), requests=len(requests))
    return result
