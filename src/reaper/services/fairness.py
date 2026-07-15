# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fairness view: who asked for what, and who actually watched it.

This is the screen the operator will reach for most -- not "what should I delete?" but
"where is my disk going, and is it going to people who use it?" It answers per requester:
how many titles they asked for, how much disk that granted them, how much of it they ever
played, and how much of it nobody watched at all.

It is **read-only and deletes nothing.** It surfaces the requester rule's findings as
information; the actual removal of anything still goes through the reviewed scan, the
plan, and (once it exists) a supervised execution. Seeing that a user requested 400 GB
and watched none of it is a conversation to have, not a delete to fire.

The rule is evaluated **per media, not per request** -- the same title is often requested
by several people, and a file one of them watched must be protected on everyone's row.
That logic lives in ``engine/requester.py``; this module gathers its inputs, runs it over
every requested title, and rolls the findings up by person.

Every join hangs off the Plex rating key: Seerr's ``ratingKey``, Tautulli's history
``rating_key`` / ``grandparent_rating_key``, and Tautulli's media-info ``rating_key`` are
the same value. A request with no rating key (Plex has not matched it) cannot be judged
and is surfaced as such, never silently counted as unwatched.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clients.arr import RadarrClient, SonarrClient
from reaper.clients.base import IntegrationError
from reaper.clients.seerr import MediaRequest, SeerrClient
from reaper.clients.tautulli import TautulliClient
from reaper.clock import utcnow
from reaper.engine import requester
from reaper.engine.requester import CONDEMN, RequesterPolicy, WatchEvidence
from reaper.services import history_sync

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class MediaInfo:
    """What we know about a requested file, keyed by rating key."""

    title: str
    size_bytes: int


@dataclass
class RequesterRow:
    """One person's row in the leaderboard. Mutated during aggregation, then frozen into
    the report."""

    plex_id: int | None
    name: str
    requests_made: int = 0
    gb_granted_bytes: int = 0
    played_by_them: int = 0
    """Of the media they requested, how many *they personally* played at least once."""
    reclaimable_items: int = 0
    reclaimable_bytes: int = 0
    """Media they requested that the rule condemns -- nobody watched it, and it has been
    available long enough to count. Sizes may overlap across co-requesters; the report
    total dedupes, these per-person figures deliberately do not (it is 'disk you asked
    for that nobody used', per person)."""
    unwatched_titles: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FairnessReport:
    rows: list[RequesterRow]
    total_requests: int
    total_reclaimable_bytes: int
    """Deduped by media -- a title requested by three people is counted once here."""
    total_reclaimable_items: int
    unmatched_requests: int
    """Requests with no rating key or no added-at date: real, but unjudgeable. Shown so
    the operator knows the numbers describe *most* of their requests, not all."""


def _name(request: MediaRequest) -> str:
    r = request.requester
    return r.display_name or r.username or f"user:{r.seerr_user_id}"


def evaluate_fairness(
    requests: list[MediaRequest],
    evidence_by_key: dict[str, WatchEvidence],
    media_by_key: dict[str, MediaInfo],
    policy: RequesterPolicy,
    *,
    now: datetime | None = None,
) -> FairnessReport:
    """Pure aggregation: given requests, watch evidence, and sizes, roll up per requester.

    Split from the gathering so it can be tested without any live instance -- the
    correctness that matters (per-media judging, per-person roll-up, deduped totals) is
    all here.
    """
    grouped = requester.group_by_media(requests)
    rows: dict[int | None, RequesterRow] = {}
    reclaimable_keys: set[str] = set()
    total_reclaimable_bytes = 0
    unmatched = 0

    for key, group in grouped.items():
        info = media_by_key.get(key)
        evidence = evidence_by_key.get(key, WatchEvidence())
        finding = requester.evaluate(group, evidence, policy, now=now)
        size = info.size_bytes if info else 0
        title = info.title if info else (group[0].imdb_id or key)

        # An unmatched request (synthetic key, or a matched key we have no media info for)
        # is counted as seen-but-unjudgeable, not as reclaimable.
        if key.startswith("unmatched:") or info is None:
            unmatched += 1

        condemned = finding.verdict == CONDEMN
        if condemned and info is not None:
            reclaimable_keys.add(key)

        # One row per distinct requester of this title.
        seen_here: set[int | None] = set()
        for req in group:
            pid = req.requester.plex_id
            if pid in seen_here:
                continue
            seen_here.add(pid)
            row = rows.get(pid)
            if row is None:
                row = RequesterRow(plex_id=pid, name=_name(req))
                rows[pid] = row
            row.requests_made += 1
            row.gb_granted_bytes += size
            if evidence.plays_by(pid) > 0:
                row.played_by_them += 1
            if condemned and info is not None:
                row.reclaimable_items += 1
                row.reclaimable_bytes += size
                row.unwatched_titles.append(title)

    total_reclaimable_bytes = sum(
        media_by_key[k].size_bytes for k in reclaimable_keys if k in media_by_key
    )
    # Heaviest disk-holders first: the operator is looking for where the space went.
    ordered = sorted(rows.values(), key=lambda r: r.gb_granted_bytes, reverse=True)
    return FairnessReport(
        rows=ordered,
        total_requests=len(requests),
        total_reclaimable_bytes=total_reclaimable_bytes,
        total_reclaimable_items=len(reclaimable_keys),
        unmatched_requests=unmatched,
    )


async def _media_index(tautulli: TautulliClient) -> dict[str, MediaInfo]:
    """rating_key (as string) -> title + file size, across movie and TV libraries.

    Same call the scan uses for its Plex index; here we key by rating key and keep the
    size, because the fairness columns are about disk. Fetched fresh -- this is an
    on-demand analytical view, not a hot path."""
    index: dict[str, MediaInfo] = {}
    for library in await tautulli.libraries():
        if library.get("section_type") not in ("movie", "show"):
            continue
        section_id = int(library["section_id"])
        start = 0
        while True:
            page = await tautulli.library_media_info(section_id, start=start, length=1000)
            rows = page.get("data") or []
            for row in rows:
                rk = row.get("rating_key")
                if rk is None:
                    continue
                index[str(rk)] = MediaInfo(
                    title=str(row.get("title") or ""),
                    size_bytes=int(row.get("file_size") or 0),
                )
            if len(rows) < 1000:
                break
            start += 1000
    return index


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _arr_sizes(
    radarrs: Sequence[RadarrClient],
    sonarrs: Sequence[SonarrClient],
    requests: list[MediaRequest],
) -> dict[str, int]:
    """Plex rating key -> file size on disk, sourced from the *arr that manages the file.

    The *arr is the authority on file sizes -- it is what downloaded and stores the file,
    and it is where the scan itself reads them. Tautulli only reflects Plex's view, which
    lags for movies and is *zero* for show-level rows (the bytes live on the episodes), so
    sizes come from Radarr (movies) and Sonarr (TV) here too, joined to each Seerr request
    by the external id it already carries -- tmdb for a movie, tvdb for a show -- and keyed
    by the request's Plex rating key, the same value the watch join uses.

    Both are optional: a movie-only deployment passes no Sonarr, and anything an *arr does
    not cover simply keeps whatever size the Tautulli fallback had.

    When the same title exists in two instances (a 4K library beside an HD one), the larger
    size wins -- the request was for one of them, and for a "disk you asked for" view it is
    better to over-state than to silently undercount the 4K copy.
    """
    size_by_tmdb: dict[int, int] = {}
    for radarr in radarrs:
        try:
            movies = await radarr.movies()
        except IntegrationError as exc:
            log.warning("fairness.radarr_unreachable", error=str(exc))
            continue
        for movie in movies:
            tmdb = _as_int(movie.get("tmdbId"))
            size = _as_int(movie.get("sizeOnDisk")) or 0
            if tmdb and size:
                size_by_tmdb[tmdb] = max(size, size_by_tmdb.get(tmdb, 0))

    size_by_tvdb: dict[int, int] = {}
    for sonarr in sonarrs:
        try:
            series_list = await sonarr.series()
        except IntegrationError as exc:
            log.warning("fairness.sonarr_unreachable", error=str(exc))
            continue
        for series in series_list:
            tvdb = _as_int(series.get("tvdbId"))
            stats = series.get("statistics") or {}
            size = _as_int(stats.get("sizeOnDisk")) or 0
            if tvdb and size:
                size_by_tvdb[tvdb] = max(size, size_by_tvdb.get(tvdb, 0))

    out: dict[str, int] = {}
    for req in requests:
        if not req.plex_rating_key:
            continue
        if req.media_type == "movie":
            req_size = size_by_tmdb.get(req.tmdb_id) if req.tmdb_id else None
        else:
            req_size = size_by_tvdb.get(req.tvdb_id) if req.tvdb_id else None
        if req_size:
            out[str(req.plex_rating_key)] = req_size
    return out


async def _evidence_index(
    cache_engine: AsyncEngine, rating_keys: set[str]
) -> dict[str, WatchEvidence]:
    """For each requested rating key, who played it and how many times.

    Matches the key against both ``rating_key`` (movies) and ``grandparent_rating_key``
    (a show's episodes roll up to the show), so one query serves both media types without
    the caller having to know which it is.
    """
    if not rating_keys:
        return {}
    await history_sync.ensure_schema(cache_engine)

    int_keys = [int(k) for k in rating_keys if k.isdigit()]
    if not int_keys:
        return {}

    # Match against the movie key and the show key in one pass, then sum -- so a caller
    # never has to know whether a request points at a film or a series. SQLite has no
    # array type, so the list is expanded by an ``expanding`` bindparam.
    stmt = text(
        "SELECT key, user_id, SUM(plays) AS plays FROM ("
        "  SELECT rating_key AS key, user_id, COUNT(*) AS plays "
        "  FROM watch_event WHERE rating_key IN :keys GROUP BY rating_key, user_id "
        "  UNION ALL "
        "  SELECT grandparent_rating_key AS key, user_id, COUNT(*) AS plays "
        "  FROM watch_event WHERE grandparent_rating_key IN :keys "
        "  GROUP BY grandparent_rating_key, user_id "
        ") GROUP BY key, user_id"
    ).bindparams(bindparam("keys", expanding=True))

    result: dict[str, dict[int, int]] = {}
    async with cache_engine.connect() as conn:
        rows = (await conn.execute(stmt, {"keys": int_keys})).all()
    for key, user_id, plays in rows:
        result.setdefault(str(key), {})[int(user_id)] = int(plays)

    return {
        k: WatchEvidence(plays_by_user=plays, distinct_watchers=len(plays))
        for k, plays in result.items()
    }


async def build_report(
    *,
    seerr: SeerrClient,
    tautulli: TautulliClient,
    cache_engine: AsyncEngine,
    radarrs: Sequence[RadarrClient] = (),
    sonarrs: Sequence[SonarrClient] = (),
    policy: RequesterPolicy | None = None,
) -> FairnessReport:
    """Gather everything the leaderboard needs from live instances, then aggregate."""
    policy = policy or RequesterPolicy()
    requests = await seerr.all_requests(filter_="available")
    keys = {r.plex_rating_key for r in requests if r.plex_rating_key}

    # Titles come from Tautulli's sweep (keyed by rating key). Sizes come from the *arr --
    # the authority on what is actually on disk -- overriding Tautulli's, which lags for
    # movies and is zero for shows. Tautulli's size survives only where no *arr covers it.
    media = await _media_index(tautulli)
    for rating_key, size in (await _arr_sizes(radarrs, sonarrs, requests)).items():
        existing = media.get(rating_key)
        media[rating_key] = MediaInfo(title=existing.title if existing else "", size_bytes=size)

    evidence = await _evidence_index(cache_engine, keys)
    log.info(
        "fairness.built",
        requests=len(requests),
        media_indexed=len(media),
        keys_with_history=len(evidence),
    )
    return evaluate_fairness(requests, evidence, media, policy, now=utcnow())
