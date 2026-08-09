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


@dataclass(frozen=True)
class Requester:
    """The person who asked. ``plex_id`` is the join to Tautulli."""

    seerr_user_id: int
    plex_id: int | None
    username: str | None
    display_name: str | None
    email: str | None


@dataclass(frozen=True)
class MediaRequest:
    """One request, flattened to what the scoring engine needs."""

    request_id: int
    media_type: str  # "movie" | "tv"
    is_4k: bool
    requested_at: datetime | None
    requester: Requester

    tmdb_id: int | None
    tvdb_id: int | None
    imdb_id: str | None

    # The join keys. 4K lives in parallel fields: a 4K request correlates to
    # ratingKey4k and externalServiceId4k, and watch data must be summed across
    # both, or a film watched in 4K looks unwatched in HD.
    plex_rating_key: str | None
    arr_id: int | None
    arr_instance_id: int | None

    available_at: datetime | None
    """media.mediaAddedAt -- when it actually arrived. The clock for the requester rule."""

    portal_key: str = ""
    """Which Seerr instance this request came from -- stamped by the client that read it. A
    Seerr user id is unique only *within* one portal (each instance numbers its own users
    from 1), so two portals reuse the same ids for different people. Anything that keys a
    person on their Seerr id must pair it with this, or two people collide across portals."""

    seasons: tuple[int, ...] = ()

    raw: dict[str, Any] | None = None
    """The full payload, archived before we ever call DELETE /media/{id}: that
    delete cascades and destroys the very request history that justified it."""


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
class TitleInfo:
    """The human name and year for a tmdb id, from Seerr's TMDB proxy. A request payload
    carries only ids, so this is how a requested item the scan never saw gets a name to
    show. Display only -- never a join key."""

    title: str
    year: int | None


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


def _parse_request(payload: dict[str, Any], portal_key: str = "") -> MediaRequest:
    media = payload.get("media") or {}
    user = payload.get("requestedBy") or {}
    is_4k = bool(payload.get("is4k"))

    return MediaRequest(
        request_id=_as_int(payload.get("id")) or 0,
        media_type=str(payload.get("type") or media.get("mediaType") or "movie"),
        is_4k=is_4k,
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
        portal_key=portal_key,
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


@dataclass(frozen=True)
class SeerrService:
    """One Sonarr/Radarr service configured on this Seerr portal (from ``/settings/*``).

    ``service_id`` is the portal-local id a request's ``serviceId`` refers to (the join the
    operator maps to a Reaper instance). ``hostname``/``port``/``use_ssl``/``base_url`` are how
    THIS Seerr reaches the *arr, used only to *suggest* a matching Reaper instance -- the operator
    confirms, because Seerr and Reaper may reach the same server at different addresses."""

    service_id: int
    kind: str  # "sonarr" | "radarr"
    name: str
    is_4k: bool
    hostname: str | None
    port: int | None
    use_ssl: bool
    base_url: str  # the *arr's own base path (e.g. "/sonarr"), NOT a full URL


def _parse_service(payload: dict[str, Any], kind: str) -> SeerrService | None:
    """One service row, or ``None`` when it carries no usable id (never a crash)."""
    service_id = _as_int(payload.get("id"))
    if service_id is None:
        return None
    return SeerrService(
        service_id=service_id,
        kind=kind,
        name=str(payload.get("name") or "").strip() or f"{kind.capitalize()} {service_id}",
        is_4k=bool(payload.get("is4k")),
        hostname=(str(payload.get("hostname")).strip() or None)
        if payload.get("hostname")
        else None,
        port=_as_int(payload.get("port")),
        use_ssl=bool(payload.get("useSsl")),
        base_url=str(payload.get("baseUrl") or "").strip(),
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
        instance_key: str | None = None,
        link_base_url: str | None = None,
    ) -> None:
        super().__init__(
            base_url,
            safety=safety,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            verify=verify,
        )
        # A stable id for this portal, stamped onto every request so a per-portal Seerr user
        # id can be paired with it (each instance numbers its users independently, so the id
        # alone collides across portals). Defaults to the base url when a caller does not pass
        # one -- distinct per portal, which is all the pairing needs.
        self.instance_key = instance_key or self.base_url
        # The operator's external address for BROWSER links (the instance's external_url), when
        # they reach Seerr at a public address while Reaper connects over a LAN ip. Never used
        # for API calls -- those always ride ``base_url``, the connect address. ``None`` falls
        # back to ``base_url`` at the link, so nothing changes when it is unset.
        self.link_base_url = link_base_url

    async def status(self) -> dict[str, Any]:
        return await self.get_dict("/api/v1/status")

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
        payload = await self.get_dict(
            "/api/v1/request",
            params={"take": take, "skip": skip, "filter": filter_, "sort": sort},
        )
        total = int((payload.get("pageInfo") or {}).get("results") or 0)
        rows = payload.get("results")
        if not isinstance(rows, list):
            # A page whose rows cannot be read is not a page with no rows. Coerced to [],
            # it ended the walk below as though the walk were finished (rule 56/89).
            raise IntegrationError(self.service, "/request did not return a list of results")
        results = [_parse_request(r, self.instance_key) for r in rows]
        if results and total <= 0:
            # Rows came back but no total did: the envelope shape changed (pageInfo moved
            # or was renamed). Treating that as total=0 would stop after one page and
            # silently undercount every requester, so refuse instead.
            raise IntegrationError(self.service, "/request returned rows but no pageInfo total")
        return results, total

    async def all_requests(self, *, filter_: str = "available") -> list[MediaRequest]:
        """Every request, paged through to the end. Complete, or it raises.

        The completeness matters more here than the count suggests. ``build_request_index``
        sets ``available=True`` when every Seerr "was read in full", and its own docstring
        says why: a confident ``Known(value=False)`` off a partial view adds delete pressure
        to a title a blinded portal in fact holds a request for. Returning short and normal
        made that claim false without anything noticing (rules 56/89 and 7/24).
        """
        out: list[MediaRequest] = []
        skip = 0
        while True:
            page, total = await self.requests(take=DEFAULT_PAGE_SIZE, skip=skip, filter_=filter_)
            out.extend(page)
            skip += DEFAULT_PAGE_SIZE
            if skip >= total:
                break
            if not page:
                # The server says there are more and handed back none. Refusing costs the
                # requester index for this scan (every lookup becomes Unknown, which keeps);
                # continuing would report a partial set as the whole truth.
                raise IntegrationError(
                    self.service, f"/request stopped at {len(out)} of {total} requests"
                )
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
            payload = await self.get_dict("/api/v1/user", params={"take": take, "skip": skip})
            results = payload.get("results")
            if not isinstance(results, list):
                # Unreadable rows are not zero rows. Same defect as :meth:`requests`, and
                # fixed the same way (rules 56/89 and 72).
                raise IntegrationError(self.service, "/user did not return a list of results")
            total = int((payload.get("pageInfo") or {}).get("results") or 0)
            if results and total <= 0:
                # Rows but no total: the envelope shape changed. Refuse rather than stop
                # after one page and silently undercount, exactly as :meth:`requests` does.
                raise IntegrationError(self.service, "/user returned rows but no pageInfo total")
            out.extend(_parse_user(r) for r in results)
            skip += take
            if skip >= total:
                break
            if not results:
                raise IntegrationError(
                    self.service, f"/user stopped at {len(out)} of {total} accounts"
                )
        log.info("seerr.users_loaded", count=len(out))
        return out

    async def quota(self, user_id: int) -> UserQuota:
        """One user's live request limits, both types, from ``GET /user/{id}/quota``.

        Read with the admin API key, so it resolves any user's effective quota (their own
        override, or the global default). The ``restricted`` flag it carries is the only
        source of truth for "at their cap right now", computed live inside each type's
        window."""
        payload = await self.get_dict(f"/api/v1/user/{user_id}/quota")
        return UserQuota(
            movie=_parse_quota(payload.get("movie")), tv=_parse_quota(payload.get("tv"))
        )

    async def title(self, *, tmdb_id: int, media_type: str) -> TitleInfo:
        """The human title and year for a tmdb id, from Seerr's TMDB proxy
        (``/movie/{id}`` or ``/tv/{id}``). A request carries only ids, so this is how Scales
        names a requested item the last scan never saw. Best-effort by design: the caller
        gathers these and lets a failed lookup fall back to a generic label, never blocking
        the page. The kind (movie vs tv) picks the endpoint and the title field, since the
        two id spaces overlap and each endpoint names the field differently."""
        kind = "movie" if media_type == "movie" else "tv"
        payload = await self.get_dict(f"/api/v1/{kind}/{tmdb_id}")
        if kind == "movie":
            name = payload.get("title") or payload.get("originalTitle")
            released = payload.get("releaseDate")
        else:
            name = payload.get("name") or payload.get("originalName")
            released = payload.get("firstAirDate")
        if not name:
            raise IntegrationError(self.service, f"/{kind}/{tmdb_id} carried no title")
        # A TMDB date is "YYYY-MM-DD"; the year is its first four chars. Absent or malformed
        # dates leave the year unknown rather than guessed.
        year = _as_int(str(released)[:4]) if released else None
        return TitleInfo(title=str(name), year=year)

    async def services(self) -> list[SeerrService]:
        """Every Sonarr and Radarr service this portal has configured.

        Reads ``/settings/sonarr`` and ``/settings/radarr`` (each a plain JSON array). Used only
        to build the serviceId -> Reaper-instance mapping UI; a request already carries the
        ``serviceId`` this list's ids match. Requires the portal's admin API key (settings are
        admin-scoped); a non-object array element is skipped rather than crashing the list.
        """
        out: list[SeerrService] = []
        for path, kind in (
            ("/api/v1/settings/sonarr", "sonarr"),
            ("/api/v1/settings/radarr", "radarr"),
        ):
            payload = await self.get_list(path)
            for row in payload:
                if isinstance(row, dict) and (svc := _parse_service(row, kind)) is not None:
                    out.append(svc)
        log.info("seerr.services_loaded", count=len(out))
        return out

    async def plex_machine_id(self) -> str | None:
        """The machine identifier of the Plex server this portal is synced to, from
        ``/settings/plex`` -- or ``None`` if it cannot be read.

        Rating keys are unique per Plex server only, so a portal synced to a DIFFERENT Plex than
        Reaper's files ratingKeys that can numerically collide with Reaper's candidates. This lets
        the requested-by join skip such a portal's rating-key tier (requested_by.build_map, I-3).
        Requires the portal's admin API key (settings are admin-scoped), like :meth:`services`.
        Best-effort: ``None`` on any failure or a missing field keeps the caller's current
        behavior, which is to file the rating-key tier."""
        try:
            payload = await self.get_json("/api/v1/settings/plex")
        except IntegrationError as exc:
            log.warning("seerr.plex_settings_unreadable", error=str(exc))
            return None
        if not isinstance(payload, dict):
            return None
        machine = payload.get("machineId") or payload.get("machineIdentifier")
        text = str(machine).strip() if machine is not None else ""
        return text or None
