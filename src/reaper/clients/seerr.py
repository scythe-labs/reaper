# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seerr, the service that tracks who requested what, and when it actually arrived.

This client is modeled from the live API rather than the published OpenAPI
document, which is out of date: the spec omits ``ratingKey``, ``externalServiceId``,
``mediaAddedAt`` and ``status4k``, yet the live API returns all four, because its
routes serialize the raw database entities. A client generated from that spec would
silently drop every field this app needs.

The join keys, all confirmed present on a live instance:

* ``media.ratingKey`` maps to Tautulli's ``rating_key``. It is a string, and stays
  null until Plex has matched the item.
* ``media.externalServiceId`` maps to the Sonarr/Radarr id, and ``serviceId`` names
  which instance.
* ``requestedBy.plexId`` maps to Tautulli's ``user_id``.
* ``media.mediaAddedAt`` is the clock for "requested but never watched".

That last one needs care. The countdown starts when the media arrived, not when it
was requested, because nobody can fail to watch something that does not exist yet. A
request approved in January for a film that only downloaded in June has not been
ignored for five months.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

import structlog

from reaper.clients.base import BaseClient, IntegrationError
from reaper.clock import from_iso
from reaper.config import RuntimeSafety
from reaper.engine import identity

log = structlog.get_logger(__name__)

# Seerr paginates at 10 requests by default. A caller that forgets to pass `take`
# silently reads only the first ten and treats the rest as if they do not exist.
DEFAULT_PAGE_SIZE = 100

#: Hard stop on both walks below. Their only normal exit is ``skip >= total``, and
#: ``total`` is a number the portal re-picks on every page, so the server controls how
#: long the walk can run. A total that keeps rising by a page's worth each time is
#: never caught by comparing against ``total``, so this reads the page count instead.
#: At :data:`DEFAULT_PAGE_SIZE` rows per page, this cap allows 100,000 requests, far
#: past any real Seerr, so it only fires against a server that is not actually
#: advancing through the listing. Tripping it raises rather than stopping short with a
#: warning, matching the complete-or-raise design of ``history_sync.MAX_HISTORY_PAGES``.
#: Both walks here must read every Seerr request in full, and a short return would
#: leave ``requested_by.build_request_index`` believing it did.
MAX_PAGES = 1_000


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

    # The join keys. 4K data lives in separate fields: a 4K request maps to
    # ratingKey4k and externalServiceId4k. Watch data must be summed across both, or
    # a film watched in 4K looks unwatched in HD.
    plex_rating_key: str | None
    arr_id: int | None
    arr_instance_id: int | None

    available_at: datetime | None
    """media.mediaAddedAt, the moment the media actually arrived. This is the clock
    the requester rule uses."""

    portal_key: str = ""
    """Which Seerr instance this request came from, stamped by the client that read
    it. A Seerr user id is unique only within one portal, since each instance numbers
    its own users starting from 1, so two portals can reuse the same id for two
    different people. Anything that keys a person by their Seerr id must pair it with
    this value, or two people from different portals collide."""

    seasons: tuple[int, ...] = ()

    raw: dict[str, Any] | None = None
    """The full payload, archived before this client ever calls DELETE /media/{id}.
    That delete cascades and destroys the very request history that justified it."""


@dataclass(frozen=True)
class QuotaStatus:
    """One media type's request limit, from ``GET /user/{id}/quota``.

    Movies and series have separate limits, each with its own window and unit (movies
    per N days, seasons per M days), so the window is carried per type and never
    assumed. A missing or zero ``limit`` means unlimited. ``restricted`` is Seerr's own
    live "at or over the cap right now" flag, computed inside the window. It is the
    only field that says whether a person is blocked right now, which a stored limit
    alone cannot show.
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
    show. Display only, never a join key."""

    title: str
    year: int | None


@dataclass(frozen=True)
class SeerrUser:
    """One Seerr account. ``plex_id`` is the join to a requester and to Tautulli.
    ``seerr_user_id`` is what the quota endpoint keys on, per instance."""

    seerr_user_id: int
    plex_id: int | None
    username: str | None
    display_name: str | None
    email: str | None
    request_count: int
    """Lifetime requests on this instance, across all statuses. This is Seerr's own
    ``requestCount``, and it differs from the count Scales computes during a scan.
    It includes titles the scan no longer has, such as ones deleted, unavailable, or
    filtered out."""


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
    # The external ids go through identity.ExternalIds.of, which filters out
    # placeholder ids, exactly as the scan's own reads of Radarr/Sonarr/Plex do. Seerr
    # fills `imdbId` from TMDB, which returns "tt0000000" for a title with no IMDb
    # entry. Carried raw, every such request and candidate would join to each other in
    # Scales (services.fairness._index_candidates) and credit one person with another
    # person's disk space. `_as_int` still handles the other, non-id integers.
    ids = identity.ExternalIds.of(
        imdb=media.get("imdbId"), tmdb=media.get("tmdbId"), tvdb=media.get("tvdbId")
    )

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
        tmdb_id=ids.tmdb,
        tvdb_id=ids.tvdb,
        imdb_id=ids.imdb,
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
    """One media type's quota block. A zero limit is normalized to unlimited (``None``).
    Overseerr uses 0 and a missing value interchangeably to mean "no limit", and
    reading a zero literally would show a false "zero allowed" block that does not
    really exist."""
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

    ``service_id`` is the portal-local id that a request's ``serviceId`` refers to. It
    is the join the operator maps to a Reaper instance. ``hostname``, ``port``,
    ``use_ssl`` and ``base_url`` describe how this Seerr reaches the *arr, and are
    used only to suggest a matching Reaper instance. The operator still confirms the
    match, because Seerr and Reaper may reach the same server at different
    addresses."""

    service_id: int
    kind: str  # "sonarr" | "radarr"
    name: str
    is_4k: bool
    hostname: str | None
    port: int | None
    use_ssl: bool
    base_url: str  # the *arr's own base path (e.g. "/sonarr"), NOT a full URL


def _parse_service(payload: dict[str, Any], kind: str) -> SeerrService | None:
    """One service row, or ``None`` when it carries no usable id. Never raises."""
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
        # A stable id for this portal, stamped onto every request so a per-portal
        # Seerr user id can be paired with it. Each instance numbers its users
        # independently, so the id alone collides across portals. Defaults to the
        # base url when a caller does not pass one, since any value distinct per
        # portal is enough for the pairing.
        self.instance_key = instance_key or self.base_url
        # The operator's external address for browser links (the instance's
        # external_url), for when they reach Seerr at a public address while Reaper
        # connects over a LAN ip. Never used for API calls, which always use
        # ``base_url``, the connect address. ``None`` falls back to ``base_url`` at
        # the link, so nothing changes when it is unset.
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
        """One page of requests, plus the total count.

        ``take`` is always sent explicitly. The server default is 10, and relying on
        it would quietly analyze only the ten most recent requests and report the
        rest as if they did not exist.
        """
        payload = await self.get_dict(
            "/api/v1/request",
            params={"take": take, "skip": skip, "filter": filter_, "sort": sort},
        )
        total = int((payload.get("pageInfo") or {}).get("results") or 0)
        rows = payload.get("results")
        if not isinstance(rows, list):
            # A page whose rows cannot be read is not a page with no rows. Coercing it
            # to [] would end the walk below as though it had finished reading
            # everything.
            raise IntegrationError(
                self.service, "error.integration.unexpected_shape", path="/request"
            )
        results = [_parse_request(r, self.instance_key) for r in rows]
        if results and total <= 0:
            # Rows came back but no total did. The envelope shape has likely changed,
            # such as pageInfo moving or being renamed. Treating that as total=0 would
            # stop after one page and silently undercount every requester, so this
            # refuses instead.
            raise IntegrationError(
                self.service, "error.integration.unexpected_shape", path="/request"
            )
        return results, total

    async def all_requests(self, *, filter_: str = "available") -> list[MediaRequest]:
        """Every request, paged through to the end. Either this reads everything, or
        it raises.

        Completeness matters more here than the count suggests. ``build_request_index``
        sets ``available=True`` only when every Seerr portal was read in full, and its
        own docstring explains why: a confident "no request exists" conclusion drawn
        from a partial read would raise a title's deletion score, even though a portal
        Reaper could not fully read might in fact hold a request for it. Returning a
        short, silent result would reach that false conclusion without anything
        noticing.

        Bounded by :data:`MAX_PAGES`, which raises rather than returning a short
        result.
        """
        out: list[MediaRequest] = []
        skip = 0
        pages = 0
        while True:
            page, total = await self.requests(take=DEFAULT_PAGE_SIZE, skip=skip, filter_=filter_)
            pages += 1
            out.extend(page)
            skip += DEFAULT_PAGE_SIZE
            if skip >= total:
                break
            if not page:
                # The server says more requests exist but returned none. Refusing
                # costs the requester index for this scan, since every lookup then
                # becomes Unknown, which keeps the item. Continuing instead would
                # report a partial set as the whole truth.
                raise IntegrationError(
                    self.service,
                    "error.integration.seerr_list_incomplete",
                    seen=len(out),
                    total=total,
                )
            if pages >= MAX_PAGES:
                # Only reachable while the reported total keeps outrunning ``skip``, which is a
                # portal that is not advancing through the listing.
                raise IntegrationError(
                    self.service, "error.integration.seerr_list_unbounded", count=len(out)
                )
        log.info("seerr.requests_loaded", count=len(out), filter=filter_)
        return out

    async def users(self, *, take: int = DEFAULT_PAGE_SIZE) -> list[SeerrUser]:
        """Every Seerr account, paged to the end.

        ``take`` is sent explicitly for the same reason as :meth:`requests`: the server
        default is small, and relying on it would read only the first page of users and
        report the rest as if they did not exist.

        Bounded by :data:`MAX_PAGES`, which raises rather than returning a short result.
        """
        out: list[SeerrUser] = []
        skip = 0
        pages = 0
        while True:
            payload = await self.get_dict("/api/v1/user", params={"take": take, "skip": skip})
            pages += 1
            results = payload.get("results")
            if not isinstance(results, list):
                # Unreadable rows are not zero rows. This is the same failure as
                # :meth:`requests`, fixed the same way.
                raise IntegrationError(
                    self.service, "error.integration.unexpected_shape", path="/user"
                )
            total = int((payload.get("pageInfo") or {}).get("results") or 0)
            if results and total <= 0:
                # Rows came back but no total did, meaning the envelope shape changed.
                # This refuses rather than stopping after one page and silently
                # undercounting, exactly as :meth:`requests` does.
                raise IntegrationError(
                    self.service, "error.integration.unexpected_shape", path="/user"
                )
            out.extend(_parse_user(r) for r in results)
            skip += take
            if skip >= total:
                break
            if not results:
                raise IntegrationError(
                    self.service,
                    "error.integration.seerr_list_incomplete",
                    seen=len(out),
                    total=total,
                )
            if pages >= MAX_PAGES:
                # The same page-bound check as :meth:`requests`. It is also the only
                # bound when a caller passes ``take=0``, where ``skip`` never advances
                # at all.
                raise IntegrationError(
                    self.service, "error.integration.seerr_list_unbounded", count=len(out)
                )
        log.info("seerr.users_loaded", count=len(out))
        return out

    async def quota(self, user_id: int) -> UserQuota:
        """One user's live request limits, for both media types, from
        ``GET /user/{id}/quota``.

        Read with the admin API key, so it resolves any user's effective quota,
        whether that is their own override or the global default. The ``restricted``
        flag is the only source of truth for whether they are at their cap right now,
        computed live inside each type's window."""
        payload = await self.get_dict(f"/api/v1/user/{user_id}/quota")
        return UserQuota(
            movie=_parse_quota(payload.get("movie")), tv=_parse_quota(payload.get("tv"))
        )

    async def title(self, *, tmdb_id: int, media_type: str) -> TitleInfo:
        """The human title and year for a tmdb id, from Seerr's TMDB proxy
        (``/movie/{id}`` or ``/tv/{id}``). A request carries only ids, so this is how
        Scales names a requested item the last scan never saw.

        Best-effort by design: the caller gathers these and lets a failed lookup fall
        back to a generic label instead of blocking the page. The kind (movie or tv)
        picks the endpoint and the title field, since the two id spaces overlap and
        each endpoint names the title field differently."""
        kind = "movie" if media_type == "movie" else "tv"
        payload = await self.get_dict(f"/api/v1/{kind}/{tmdb_id}")
        if kind == "movie":
            name = payload.get("title") or payload.get("originalTitle")
            released = payload.get("releaseDate")
        else:
            name = payload.get("name") or payload.get("originalName")
            released = payload.get("firstAirDate")
        if not name:
            raise IntegrationError(
                self.service, "error.integration.unexpected_shape", path=f"/{kind}/{tmdb_id}"
            )
        # A TMDB date reads as "YYYY-MM-DD". The year is its first four characters. A
        # missing or malformed date leaves the year unknown rather than guessed.
        year = _as_int(str(released)[:4]) if released else None
        return TitleInfo(title=str(name), year=year)

    async def services(self) -> list[SeerrService]:
        """Every Sonarr and Radarr service this portal has configured.

        Reads ``/settings/sonarr`` and ``/settings/radarr``, each a plain JSON array.
        Used only to build the serviceId -> Reaper-instance mapping UI, since a
        request already carries the ``serviceId`` this list's ids match against.
        Requires the portal's admin API key, because settings are admin-scoped. A
        non-object array element is skipped rather than crashing the whole list.
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
        ``/settings/plex``, or ``None`` if it cannot be read.

        A rating key is unique only within one Plex server. A portal synced to a
        different Plex server than Reaper's would report rating keys that can
        numerically collide with Reaper's own candidates. Knowing the portal's
        machine id lets the requested-by join skip that portal's rating-key tier
        instead of matching on a false collision (``requested_by.build_map``).
        Requires the portal's admin API key, like :meth:`services`, because settings
        are admin-scoped. Best-effort: ``None`` on any failure or a missing field
        keeps the caller's current behavior, which is to keep using the rating-key
        tier."""
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
