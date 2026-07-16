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

from dataclasses import dataclass

import structlog

from reaper.clients.base import IntegrationError
from reaper.clients.seerr import SeerrClient
from reaper.engine.observation import Known, Observation, Unknown

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


@dataclass(frozen=True)
class RequestIndex:
    """A three-state "was this requested?" view, for the scoring path.

    Unlike :func:`build_map` (a display name, deliberately loose), this answers a *fact*
    the score can lean on, so it is built from the fail-closed side. ``available`` is
    ``False`` whenever Seerr could not be fully read; every lookup is then ``Unknown``, so
    a missing requests app can never make a title look un-requested and add delete
    pressure. The join is on tmdb/tvdb ids, which a non-admin key does not strip.
    """

    available: bool
    movie_keys: frozenset[str]
    show_keys: frozenset[str]
    season_keys: frozenset[str]

    def movie_requested(self, tmdb_id: int | None) -> Observation[bool]:
        if not tmdb_id:
            return Unknown(reason="no TMDb id to match a request", source="seerr")
        if not self.available:
            return Unknown(reason="could not reach the requests app", source="seerr")
        return Known(value=movie_key(tmdb_id) in self.movie_keys, source="seerr")

    def season_requested(self, tvdb_id: int | None, season: int) -> Observation[bool]:
        """A season counts as requested if the season itself, or the whole show, was."""
        if not tvdb_id:
            return Unknown(reason="no TVDb id to match a request", source="seerr")
        if not self.available:
            return Unknown(reason="could not reach the requests app", source="seerr")
        hit = season_key(tvdb_id, season) in self.season_keys or show_key(tvdb_id) in self.show_keys
        return Known(value=hit, source="seerr")

    def show_requested(self, tvdb_id: int | None) -> Observation[bool]:
        """Whether the show as a whole was requested -- the show itself, or any of its seasons."""
        if not tvdb_id:
            return Unknown(reason="no TVDb id to match a request", source="seerr")
        if not self.available:
            return Unknown(reason="could not reach the requests app", source="seerr")
        prefix = f"tv:tvdb:{tvdb_id}:"
        hit = show_key(tvdb_id) in self.show_keys or any(
            k.startswith(prefix) for k in self.season_keys
        )
        return Known(value=hit, source="seerr")


_EMPTY_INDEX = RequestIndex(
    available=False,
    movie_keys=frozenset(),
    show_keys=frozenset(),
    season_keys=frozenset(),
)


async def build_request_index(seerr: SeerrClient | None) -> RequestIndex:
    """Build a three-state requested-or-not index from *every* Seerr request.

    Reads ``filter_="all"`` (not just available ones), so a title that was requested but
    is still processing is not mistaken for "never requested". When Seerr is absent or
    unreachable, ``available`` is ``False`` and every lookup returns ``Unknown``.
    """
    if seerr is None:
        return _EMPTY_INDEX

    try:
        requests = await seerr.all_requests(filter_="all")
    except IntegrationError as exc:
        log.warning("requested_by.index_unreachable", error=str(exc))
        return _EMPTY_INDEX

    movie_keys: set[str] = set()
    show_keys: set[str] = set()
    season_keys: set[str] = set()
    for req in requests:
        if req.media_type == "movie":
            if (mk := movie_key(req.tmdb_id)) is not None:
                movie_keys.add(mk)
        else:
            if (sk := show_key(req.tvdb_id)) is not None:
                show_keys.add(sk)
            for season in req.seasons:
                if (nk := season_key(req.tvdb_id, season)) is not None:
                    season_keys.add(nk)

    log.info(
        "requested_by.index_built",
        movies=len(movie_keys),
        shows=len(show_keys),
        seasons=len(season_keys),
    )
    return RequestIndex(
        available=True,
        movie_keys=frozenset(movie_keys),
        show_keys=frozenset(show_keys),
        season_keys=frozenset(season_keys),
    )
