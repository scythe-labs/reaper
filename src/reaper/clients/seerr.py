# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seerr -- who asked for what, and when it actually arrived.

Modeled from the live API rather than the published OpenAPI document, which is
stale: the spec omits ``ratingKey``, ``externalServiceId``, ``mediaAddedAt`` and
``status4k``, yet the API returns all four (its routes serialize raw TypeORM
entities). Generating a client from that spec would silently drop every field we
actually need.

The join keys, all confirmed present on a live instance:

* ``media.ratingKey`` -> Tautulli's ``rating_key``  (a **string**, and null until
  Plex has matched the item)
* ``media.externalServiceId`` -> the Sonarr/Radarr id, with ``serviceId`` naming
  which instance
* ``requestedBy.plexId`` -> Tautulli's ``user_id``
* ``media.mediaAddedAt`` -> **the clock** for "requested but never watched"

That last one is the subtle one. The countdown starts when the media *arrived*,
not when it was requested: you cannot fail to watch something that did not exist
yet. A request approved in January for a film that only downloaded in June has not
been ignored for five months.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

import structlog

from reaper.clients.base import BaseClient, IntegrationError
from reaper.clock import from_iso
from reaper.config import RuntimeSafety

log = structlog.get_logger(__name__)

# Seerr paginates at 10 by default -- not 20, and not "everything". A caller that
# forgets to pass `take` silently reads only the first ten requests and concludes
# the rest do not exist.
DEFAULT_PAGE_SIZE = 100


class MediaStatus(enum.IntEnum):
    """Seerr's media status.

    The enum diverges between forks: ``DELETED`` is **7** on Seerr and Jellyseerr,
    but **6** on Overseerr, where 6 is ``BLOCKLISTED``. Never hardcode the integer;
    compare against this enum and detect the flavor from ``/status``.
    """

    UNKNOWN = 1
    PENDING = 2
    PROCESSING = 3
    PARTIALLY_AVAILABLE = 4
    AVAILABLE = 5
    BLOCKLISTED = 6
    DELETED = 7


@dataclass(frozen=True)
class Requester:
    """The person who asked. ``plex_id`` is the join to Tautulli."""

    seerr_user_id: int
    plex_id: int | None
    username: str | None
    display_name: str | None
    email: str | None

    @property
    def is_mappable(self) -> bool:
        """Can we tell whether this person watched the thing they asked for?

        If not, the requester rule must abstain. An unmappable requester is a
        *protection*, never evidence that nobody wanted it.
        """
        return self.plex_id is not None


@dataclass(frozen=True)
class MediaRequest:
    """One request, flattened to what the scoring engine needs."""

    request_id: int
    media_type: str  # "movie" | "tv"
    is_4k: bool
    status: int
    requested_at: datetime | None
    requester: Requester

    tmdb_id: int | None
    tvdb_id: int | None
    imdb_id: str | None

    # The join keys. Note 4K lives in parallel fields: a 4K request correlates to
    # ratingKey4k and externalServiceId4k, and watch data must be summed across
    # both, or a film watched in 4K looks unwatched in HD.
    plex_rating_key: str | None
    arr_id: int | None
    arr_instance_id: int | None

    available_at: datetime | None
    """media.mediaAddedAt -- when it actually arrived. The clock for the requester rule."""

    seasons: tuple[int, ...] = ()

    raw: dict[str, Any] | None = None
    """The full payload, archived before we ever call DELETE /media/{id}: that
    delete cascades and destroys the very request history that justified it."""

    @property
    def is_available(self) -> bool:
        return self.status in (MediaStatus.AVAILABLE, MediaStatus.PARTIALLY_AVAILABLE)


@dataclass(frozen=True)
class QuotaStatus:
    """One media type's request limit, from ``GET /user/{id}/quota``.

    Movies and series are **separate** limits with their own window and unit (movies per
    N days, seasons per M days), so the window is carried per type and never assumed. A
    missing or zero ``limit`` is unlimited. ``restricted`` is Seerr's own live "at or over
    the cap right now" flag, computed inside the window -- the one field that says whether
    a person is currently blocked, which a stored limit alone cannot.
    """

    limit: int | None
    """Requests allowed in the window. ``None`` means unlimited."""
    days: int | None
    """The rolling window, in days. ``None`` when unlimited."""
    used: int
    remaining: int | None
    restricted: bool
    """At or over the cap right now."""

    @property
    def unlimited(self) -> bool:
        return self.limit is None


@dataclass(frozen=True)
class UserQuota:
    """A user's live request limits, both types, from ``GET /user/{id}/quota``."""

    movie: QuotaStatus
    tv: QuotaStatus


@dataclass(frozen=True)
class SeerrUser:
    """One Seerr account. ``plex_id`` is the join to a requester and to Tautulli; the
    ``seerr_user_id`` is what the quota endpoint is keyed by (per instance)."""

    seerr_user_id: int
    plex_id: int | None
    username: str | None
    display_name: str | None
    email: str | None
    request_count: int
    """Lifetime requests on THIS instance, all statuses -- Seerr's own ``requestCount``.
    Distinct from the in-scan count Scales computes: this includes titles the scan no
    longer has (deleted, unavailable, filtered out)."""


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_request(payload: dict[str, Any]) -> MediaRequest:
    media = payload.get("media") or {}
    user = payload.get("requestedBy") or {}
    is_4k = bool(payload.get("is4k"))

    # A 4K request correlates to the parallel *4k fields. Reading the HD ones for a
    # 4K request would check watch history against the wrong file entirely.
    status = _as_int(media.get("status4k") if is_4k else media.get("status")) or 0

    return MediaRequest(
        request_id=_as_int(payload.get("id")) or 0,
        media_type=str(payload.get("type") or media.get("mediaType") or "movie"),
        is_4k=is_4k,
        status=status,
        requested_at=from_iso(payload.get("createdAt")),
        requester=Requester(
            seerr_user_id=_as_int(user.get("id")) or 0,
            plex_id=_as_int(user.get("plexId")),
            username=user.get("plexUsername") or user.get("username"),
            display_name=user.get("displayName"),
            email=user.get("email"),
        ),
        tmdb_id=_as_int(media.get("tmdbId")),
        tvdb_id=_as_int(media.get("tvdbId")),
        imdb_id=media.get("imdbId") or None,
        # ratingKey is a string in the API, and null until Plex matches the item.
        plex_rating_key=(media.get("ratingKey4k") if is_4k else media.get("ratingKey")) or None,
        arr_id=_as_int(
            media.get("externalServiceId4k") if is_4k else media.get("externalServiceId")
        ),
        arr_instance_id=_as_int(media.get("serviceId4k") if is_4k else media.get("serviceId")),
        available_at=from_iso(media.get("mediaAddedAt")),
        seasons=tuple(
            n
            for s in (payload.get("seasons") or [])
            if (n := _as_int(s.get("seasonNumber"))) is not None
        ),
        raw=payload,
    )


def _parse_quota(node: Any) -> QuotaStatus:
    """One media type's quota block. A zero limit is normalized to unlimited (``None``):
    Overseerr uses 0 and absent interchangeably for 'no limit', and a false 'zero allowed'
    would read as an at-cap block that isn't real."""
    node = node if isinstance(node, dict) else {}
    limit = _as_int(node.get("limit")) or None
    return QuotaStatus(
        limit=limit,
        days=_as_int(node.get("days")) if limit is not None else None,
        used=_as_int(node.get("used")) or 0,
        remaining=_as_int(node.get("remaining")) if limit is not None else None,
        # Never restricted when there is no limit, whatever the payload says.
        restricted=bool(node.get("restricted")) and limit is not None,
    )


def _parse_user(payload: dict[str, Any]) -> SeerrUser:
    return SeerrUser(
        seerr_user_id=_as_int(payload.get("id")) or 0,
        plex_id=_as_int(payload.get("plexId")),
        username=payload.get("plexUsername") or payload.get("username") or None,
        display_name=payload.get("displayName") or None,
        email=payload.get("email") or None,
        request_count=_as_int(payload.get("requestCount")) or 0,
    )


class SeerrClient(BaseClient):
    service: ClassVar[str] = "seerr"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        safety: RuntimeSafety,
        verify: bool = True,
    ) -> None:
        super().__init__(
            base_url,
            safety=safety,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            verify=verify,
        )

    async def status(self) -> dict[str, Any]:
        data = await self.get_json("/api/v1/status")
        if not isinstance(data, dict):
            raise IntegrationError(self.service, "/status did not return an object")
        return data

    async def requests(
        self,
        *,
        take: int = DEFAULT_PAGE_SIZE,
        skip: int = 0,
        filter_: str = "available",
        sort: str = "added",
    ) -> tuple[list[MediaRequest], int]:
        """One page of requests, plus the total.

        ``take`` is always sent explicitly: the server default is 10, and relying
        on it would quietly analyze the ten most recent requests and report the
        rest as non-existent.
        """
        payload = await self.get_json(
            "/api/v1/request",
            params={"take": take, "skip": skip, "filter": filter_, "sort": sort},
        )
        if not isinstance(payload, dict):
            raise IntegrationError(self.service, "/request did not return an object")

        total = int((payload.get("pageInfo") or {}).get("results") or 0)
        results = [_parse_request(r) for r in (payload.get("results") or [])]
        if results and total <= 0:
            # Rows came back but no total did: the envelope shape changed (pageInfo moved
            # or was renamed). Treating that as total=0 would stop after one page and
            # silently undercount every requester, so refuse instead.
            raise IntegrationError(self.service, "/request returned rows but no pageInfo total")
        return results, total

    async def all_requests(self, *, filter_: str = "available") -> list[MediaRequest]:
        """Every request, paged through to the end."""
        out: list[MediaRequest] = []
        skip = 0
        while True:
            page, total = await self.requests(take=DEFAULT_PAGE_SIZE, skip=skip, filter_=filter_)
            out.extend(page)
            skip += DEFAULT_PAGE_SIZE
            if not page or skip >= total:
                break
        log.info("seerr.requests_loaded", count=len(out), filter=filter_)
        return out

    async def users(self, *, take: int = DEFAULT_PAGE_SIZE) -> list[SeerrUser]:
        """Every Seerr account, paged to the end.

        ``take`` is sent explicitly for the same reason as :meth:`requests`: the server
        default is small, and relying on it would read only the first page of users and
        report the rest as absent.
        """
        out: list[SeerrUser] = []
        skip = 0
        while True:
            payload = await self.get_json("/api/v1/user", params={"take": take, "skip": skip})
            if not isinstance(payload, dict):
                raise IntegrationError(self.service, "/user did not return an object")
            results = payload.get("results") or []
            total = int((payload.get("pageInfo") or {}).get("results") or 0)
            if results and total <= 0:
                # Rows but no total: the envelope shape changed. Refuse rather than stop
                # after one page and silently undercount, exactly as :meth:`requests` does.
                raise IntegrationError(self.service, "/user returned rows but no pageInfo total")
            out.extend(_parse_user(r) for r in results)
            skip += take
            if not results or skip >= total:
                break
        log.info("seerr.users_loaded", count=len(out))
        return out

    async def quota(self, user_id: int) -> UserQuota:
        """One user's live request limits, both types, from ``GET /user/{id}/quota``.

        Read with the admin API key, so it resolves any user's effective quota (their own
        override, or the global default). The ``restricted`` flag it carries is the only
        source of truth for "at their cap right now", computed live inside each type's
        window."""
        payload = await self.get_json(f"/api/v1/user/{user_id}/quota")
        if not isinstance(payload, dict):
            raise IntegrationError(self.service, "/quota did not return an object")
        return UserQuota(
            movie=_parse_quota(payload.get("movie")), tv=_parse_quota(payload.get("tv"))
        )
