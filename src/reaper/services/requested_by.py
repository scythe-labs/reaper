# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who asked for a title -- resolved at scan time, for display and filtering.

The review queue lets you filter to "media someone requested", and each item shows who
asked. That needs a join from Seerr's requests to the items the scan is judging.

**The join is on external ids (tmdb / tvdb) by default.** Those ids are present on both
sides and line up; Seerr's ``serviceId`` does not line up with Reaper's instance id on its
own (different numbering schemes -- Seerr indexes its own configured services, Reaper its own
rows). The tmdb/tvdb join is a *union*, though: a title kept in two libraries (a main one and
a restricted one) shares one tmdb, so every copy shows everyone who asked for the title, not
who asked for that copy.

**The operator's service map makes it per-copy.** When a Seerr portal's serviceId is mapped
to a Reaper instance (``instance.service_instance_map``, set in Settings), the request's
``externalServiceId`` -- the *arr's own item id -- rebuilds the exact copy's ``media_key``, and
the requester lands on that copy alone. Unmapped requests keep the loose union. This resolves
the multi-Seerr, multi-library case (:func:`build_map`, :class:`SeerrSource`).

**This is display-only, either way.** "Requested by" is never a gate and never condemns
anything; the worst a wrong match can do is show the wrong name on a card. So a rare
cross-edition id collision is acceptable here in a way it never would be on the delete path.
If Seerr is absent or unreachable, the map is simply empty.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import structlog

from reaper.clients.base import IntegrationError
from reaper.clients.seerr import MediaRequest, SeerrClient
from reaper.engine.observation import Known, Observation, Unknown

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SeerrSource:
    """One Seerr portal plus its operator-set serviceId -> Reaper instance map.

    ``service_instance_map`` is ``{Seerr service id (str): Reaper instance id}``. Empty when the
    operator set none, which keeps every request on the loose tmdb/tvdb union (today's behavior).
    Set, it lets :func:`build_map` bind the exact copy a person asked for when a title lives in
    more than one library/instance (a main library and a restricted one) -- see the module
    docstring on why the join is otherwise on ids alone.
    """

    client: SeerrClient
    service_instance_map: Mapping[str, int] = field(default_factory=dict)


def movie_key(tmdb_id: int | None) -> str | None:
    """The requested-map key for a movie, from its TMDb id."""
    return f"movie:tmdb:{tmdb_id}" if tmdb_id else None


def show_key(tvdb_id: int | None) -> str | None:
    """The requested-map key for a whole show, from its TVDb id."""
    return f"tv:tvdb:{tvdb_id}" if tvdb_id else None


def season_key(tvdb_id: int | None, season: int) -> str | None:
    """The requested-map key for one season of a show."""
    return f"tv:tvdb:{tvdb_id}:{season}" if tvdb_id else None


def movie_instance_key(instance_id: int, arr_id: int | None) -> str | None:
    """The precise requested-map key for a movie: its Reaper ``media_key``.

    Equal by construction to snapshot's ``radarr:{instance_id}:{movie_id}`` media_key, because a
    Seerr request's ``externalServiceId`` IS that Radarr movie id. Keyed this way, "requested by"
    lands on the exact copy a person asked for when a title is duplicated across instances."""
    return f"radarr:{instance_id}:{arr_id}" if arr_id else None


def show_instance_key(instance_id: int, arr_id: int | None) -> str | None:
    """The precise requested-map key for a whole show: its Reaper ``group_key``
    (``sonarr:{instance_id}:{series_id}``, where ``series_id`` is Seerr's ``externalServiceId``)."""
    return f"sonarr:{instance_id}:{arr_id}" if arr_id else None


def season_instance_key(instance_id: int, arr_id: int | None, season: int) -> str | None:
    """The precise requested-map key for one season: its Reaper season ``media_key``
    (``sonarr:{instance_id}:{series_id}:{season}``)."""
    return f"sonarr:{instance_id}:{arr_id}:{season}" if arr_id else None


def rating_key_key(rating_key: object) -> str | None:
    """The requested-map key for a Plex rating key: the zero-config middle tier.

    A movie request's Plex ``ratingKey`` -- or a show request's, which Seerr stores at the show
    level -- equals Reaper's ``plex_rating_key`` for the same item on the same Plex server, so a
    portal that scans only its own library gives per-copy attribution with no operator setup.
    It can only mis-point when a portal scans SEVERAL libraries and Overseerr's one non-4K slot
    kept a different copy; the operator's service map (the precise tier) overrides those. Assumes
    the portal shares Reaper's Plex server -- rating keys are unique per server, not across them,
    so a portal pointed at a *different* Plex could collide (rare, display-only, and the reason
    this sits above only the tmdb/tvdb union, never above the declared map). Takes ``object`` so
    a Seerr string and a Reaper int normalize to the one text form."""
    if rating_key is None:
        return None
    text = str(rating_key).strip()
    return f"plex:rk:{text}" if text else None


def _name(request_display: str | None, request_user: str | None) -> str:
    return (request_display or request_user or "a user").strip() or "a user"


async def _merge_requests(
    seerrs: list[SeerrClient], *, filter_: str
) -> tuple[list[MediaRequest], bool]:
    """Every request from every Seerr, concatenated, plus whether all reads succeeded.

    Seerr is a genuinely multi-instance kind (two request portals is a supported setup),
    so a reader must merge them all -- picking the first would hide the second portal's
    requests entirely. ``all_ok`` is ``False`` if *any* instance could not be read, so a
    fail-closed caller can degrade rather than trust a partial view (rule 2).
    """
    merged: list[MediaRequest] = []
    all_ok = True
    for seerr in seerrs:
        try:
            merged.extend(await seerr.all_requests(filter_=filter_))
        except IntegrationError as exc:
            log.warning("requested_by.seerr_unreachable", error=str(exc))
            all_ok = False
    return merged, all_ok


async def build_map(sources: Sequence[SeerrSource]) -> dict[str, str]:
    """Build ``key -> requester name`` from every available request, across *every* Seerr.

    Each request is filed under up to **three** kinds of key, and callers read them best-first:

    1. A **precise** key -- the item's Reaper ``media_key`` -- whenever the source's
       ``service_instance_map`` resolves the request's ``serviceId`` to a Reaper instance. This
       binds the exact copy a person asked for from the *arr the request was routed to, so it is
       copy-true whatever a portal's Plex sync saw (:func:`movie_instance_key`,
       :func:`season_instance_key`, :func:`show_instance_key`).
    2. A **rating-key** key -- the request's Plex ``ratingKey`` (:func:`rating_key_key`). Zero
       setup, and copy-true whenever a portal scans only its own library; it can mis-point only
       when a portal scans several libraries (Overseerr's one non-4K slot then kept a different
       copy), which is exactly what tier 1 overrides.
    3. The **loose** key -- tmdb (movie) or tvdb (show/season). Always, as the last fallback, so a
       request the first two cannot place still names everyone who asked for the title.

    When several people requested the same thing, the map keeps a friendly "Name + N others".

    Soft and best-effort: a missing "requested by" is a blank tag, never a failed scan, so an
    unreachable Seerr simply contributes nothing and the rest still map. No Seerr at all is an
    empty map.
    """
    # key -> ordered list of distinct requester names, so we can render "+ N others".
    names: dict[str, list[str]] = {}
    request_count = 0

    def add(key: str | None, name: str) -> None:
        if key is None:
            return
        bucket = names.setdefault(key, [])
        if name not in bucket:
            bucket.append(name)

    for source in sources:
        # Per source, so each request is resolved against ITS OWN portal's service map: a
        # serviceId is numbered locally per Seerr and would collide across portals otherwise.
        try:
            requests = await source.client.all_requests(filter_="available")
        except IntegrationError as exc:
            log.warning("requested_by.seerr_unreachable", error=str(exc))
            continue
        request_count += len(requests)
        service_map = source.service_instance_map
        for req in requests:
            name = _name(req.requester.display_name, req.requester.username)
            # Tier 2, zero-config: the request's own Plex rating key. Filed for both media types
            # (Seerr stores a show's at the show level, which is what season lookups match on).
            add(rating_key_key(req.plex_rating_key), name)
            # The Reaper instance this request's *arr service adds to, if the operator mapped it.
            reaper_instance = (
                service_map.get(str(req.arr_instance_id))
                if req.arr_instance_id is not None
                else None
            )
            if req.media_type == "movie":
                add(movie_key(req.tmdb_id), name)  # loose union, always
                if reaper_instance is not None:
                    add(movie_instance_key(reaper_instance, req.arr_id), name)  # precise copy
            else:
                add(show_key(req.tvdb_id), name)
                for season in req.seasons:
                    add(season_key(req.tvdb_id, season), name)
                if reaper_instance is not None:
                    add(show_instance_key(reaper_instance, req.arr_id), name)
                    for season in req.seasons:
                        add(season_instance_key(reaper_instance, req.arr_id, season), name)

    result: dict[str, str] = {}
    for key, bucket in names.items():
        if len(bucket) == 1:
            result[key] = bucket[0]
        else:
            result[key] = f"{bucket[0]} + {len(bucket) - 1} other{'s' if len(bucket) > 2 else ''}"

    log.info("requested_by.built", keys=len(result), requests=request_count)
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


async def build_request_index(seerrs: list[SeerrClient]) -> RequestIndex:
    """Build a three-state requested-or-not index from *every* request in *every* Seerr.

    Reads ``filter_="all"`` (not just available ones), so a title that was requested but
    is still processing is not mistaken for "never requested".

    Fail-closed to the whole set (rule 2): ``available`` is ``True`` only when there is at
    least one Seerr *and* every one of them was read in full. With no Seerr configured, or
    with any one of several unreachable, ``available`` is ``False`` and every lookup returns
    ``Unknown`` -- because "requested nowhere" can only be asserted once every request store
    has actually been read. A confident ``Known(value=False)`` off a partial view would add
    delete pressure to a title that a blinded portal in fact holds a request for.
    """
    if not seerrs:
        return _EMPTY_INDEX
    requests, all_ok = await _merge_requests(seerrs, filter_="all")
    if not all_ok:
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
