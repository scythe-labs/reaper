# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scales: who asked for what, and who actually watched it.

The screen the operator reaches for when the question is not "what should I delete?" but
"where is my disk going, and is it going to people who use it?" Per requester: how many
titles they asked for, how much disk that granted, how much of it they played, and how
much of it the last scan now considers expendable.

It is **read-only and deletes nothing.** It is a report.

**Scales sits on the last scan.** Every number here is joined to the latest snapshot's
candidates -- the same rows the review queue shows -- so Scales can never disagree with
Review. In particular a title is only ever called *reclaimable* when the scan itself
**condemns** it; a title the scan protects (watched too recently, on a keep list, ...) is
never reclaimable here, whatever the requester did. This is deliberate: the scan resolves
each title to its real Plex copies and folds watches across all of them, so leaning on its
verdict is both correct and drift-free. Re-deriving that resolution live would be a second
copy of the app's most delicate matching -- exactly what the "one decision" rule forbids.

A request the last scan has not seen (added since, or filtered out) is surfaced as
*not in the last scan*, never silently counted as unwatched.

The join is on external ids -- tmdb first, then imdb -- present on both the Seerr request
and the stored candidate (rule 6/29: a stable id, never the title). Watches come from the
watch mirror (``cache.db``), keyed by the candidate's own Plex rating key, so a stale key
on the Seerr side can no longer make a watched title read as never-played.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from itertools import batched

import structlog
from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from reaper.clients.seerr import MediaRequest, SeerrClient
from reaper.db.models import Candidate, Snapshot
from reaper.engine.requester import WatchEvidence
from reaper.services import history_sync

log = structlog.get_logger(__name__)

#: The scan verdict that makes a requested title reclaimable. Only ``condemn`` -- the
#: fail-closed reading of "the scan does not protect it": an abstain was *kept to be safe*,
#: so it is not offered up here either.
_CONDEMN = "condemn"


@dataclass(frozen=True)
class CandidateInfo:
    """The slice of a scanned candidate that Scales needs, lifted out of the ORM row so the
    roll-up is a pure function testable without a database."""

    candidate_id: int
    plex_rating_key: int | None
    verdict: str  # condemn | protect | abstain
    size_bytes: int | None
    """NULL when the arr would not report a size. Kept as ``None`` (not coerced to 0) so an
    unmeasured title reads as *size unknown* on the chip rather than a false ``0 B``; sums
    treat it as 0 (an unmeasured item cannot be weighed), the reap path is what refuses it."""
    title: str
    media_type: str  # movie | season (a season carries its show's TV-namespace ids)
    group_key: str | None
    group_title: str | None
    tmdb_id: int | None
    imdb_id: str | None


@dataclass(frozen=True)
class ReclaimableTitle:
    """One reclaimable title on a requester's row: what it is, the disk it holds, and how to
    open it. Every entry is condemned by the last scan, so the verdict is implicit.

    Exactly one of ``item_id`` / ``group_key`` is set: a movie or a single season opens its
    own card (``item_id``); a show with condemned seasons opens the show (``group_key``)."""

    title: str
    size_bytes: int | None
    """The condemned disk this title holds, or ``None`` when nothing about it is measured --
    the chip then says *size unknown* rather than a false ``0 B``."""
    item_id: int | None
    group_key: str | None


@dataclass
class RequesterRow:
    """One person's row. Mutated during aggregation, then frozen into the report."""

    user_id: int
    """The Seerr user id: always present, and the stable identity every roll-up keys on. A
    Seerr local user not linked to Plex has no ``plex_id``, so keying on ``plex_id`` merged
    every such person into one row -- this is the id that keeps them apart (rule 6/12)."""
    plex_id: int | None
    """Only the watch join uses this (plays are keyed by Plex id); never the row identity."""
    name: str
    requests_made: int = 0
    gb_granted_bytes: int = 0
    played_by_them: int = 0
    """Of the titles they requested (and the scan has), how many they personally played."""
    reclaimable_items: int = 0
    reclaimable_bytes: int = 0
    """Titles they requested that the last scan condemns. Sizes may overlap across
    co-requesters; the report total dedupes, these per-person figures deliberately do not
    (it is 'disk you asked for that is now expendable', per person)."""
    reclaimable: list[ReclaimableTitle] = field(default_factory=list)


@dataclass(frozen=True)
class FairnessReport:
    rows: list[RequesterRow]
    total_requests: int
    total_reclaimable_bytes: int
    """Deduped by candidate -- a title requested by three people is counted once here."""
    total_reclaimable_items: int
    not_in_scan: int
    """Requests the last scan has not seen, counted PER REQUEST: no external id to join on,
    or no matching candidate. Shown so the numbers read as *most* of the requests, not all."""
    no_snapshot: bool = False
    """True when no scan has ever run: Scales has nothing to sit on, and says so."""
    horizon_at: datetime | None = None
    """How far back the watch mirror reaches. Older plays are invisible, so the watched
    figures are read against the right window."""


def _name(request: MediaRequest) -> str:
    r = request.requester
    return r.display_name or r.username or f"user:{r.seerr_user_id}"


ContentKey = tuple[str, str | int]


def _kind(media_type: str) -> str:
    """Which id space a row lives in: ``movie`` or ``tv``. TMDB movie ids and TMDB TV ids
    are separate, numerically overlapping spaces (a movie and a show can share the integer),
    so a tmdb join key must carry the kind or a TV request binds a same-numbered movie
    (rule 6/29). A candidate is stored as ``movie`` or ``season``; a season is a show, so it
    keys as ``tv``."""
    return "movie" if media_type == "movie" else "tv"


def _content_key(media_type: str, tmdb_id: int | None, imdb_id: str | None) -> ContentKey | None:
    """The id Scales joins a request and a candidate on: tmdb first, then imdb. ``None`` when
    the item carries neither -- unjoinable, and surfaced as not-in-scan rather than guessed.

    The tmdb key is namespaced by media kind (``tmdb-movie`` / ``tmdb-tv``) so the movie and
    TV id spaces cannot collide; imdb ids are globally unique, so that branch needs no kind."""
    if tmdb_id:
        return (f"tmdb-{_kind(media_type)}", tmdb_id)
    if imdb_id:
        return ("imdb", imdb_id)
    return None


def roll_up(
    requests: list[MediaRequest],
    candidates: list[CandidateInfo],
    evidence_by_key: dict[str, WatchEvidence],
    *,
    horizon: datetime | None = None,
) -> FairnessReport:
    """Pure aggregation: given available requests, the scan's candidates, and who played
    what, roll up per requester. Split from the gathering so the correctness that matters --
    the id join, the condemn-only reclaimable gate, the deduped totals -- is testable with
    no live instance and no database.
    """
    # Candidates indexed by every id they carry, so a request keyed by tmdb OR imdb finds
    # them. A show contributes several season rows under one id (rule 29: pass every id). The
    # tmdb index is namespaced by media kind, matching the request's content key, so a TV
    # request cannot bind a same-numbered movie candidate (rule 6/29).
    by_tmdb: dict[ContentKey, list[CandidateInfo]] = defaultdict(list)
    by_imdb: dict[str, list[CandidateInfo]] = defaultdict(list)
    for c in candidates:
        if c.tmdb_id:
            by_tmdb[(f"tmdb-{_kind(c.media_type)}", c.tmdb_id)].append(c)
        if c.imdb_id:
            by_imdb[c.imdb_id].append(c)

    # Group requests by the content they point at, so co-requesters of one title are judged
    # together. A request with no joinable id is not-in-scan straight away.
    groups: dict[ContentKey, list[MediaRequest]] = defaultdict(list)
    not_in_scan = 0
    for req in requests:
        key = _content_key(req.media_type, req.tmdb_id, req.imdb_id)
        if key is None:
            not_in_scan += 1
            continue
        groups[key].append(req)

    # Keyed on the Seerr user id, always present: an unlinked local user has no plex_id, and
    # keying on plex_id folded every such person into one row under the first name (rule 12).
    rows: dict[int, RequesterRow] = {}
    # Deduped by the condemned candidate set, so the same title reached via a tmdb group and
    # an imdb group counts once -- the way the bytes total already dedupes by candidate id.
    reclaimable_titles: set[frozenset[int]] = set()
    condemned_size_by_candidate: dict[int, int] = {}

    def _row(req: MediaRequest) -> RequesterRow:
        r = req.requester
        row = rows.get(r.seerr_user_id)
        if row is None:
            row = RequesterRow(user_id=r.seerr_user_id, plex_id=r.plex_id, name=_name(req))
            rows[r.seerr_user_id] = row
        return row

    for group in groups.values():
        rep = group[0]
        tmdb_key: ContentKey | None = (
            (f"tmdb-{_kind(rep.media_type)}", rep.tmdb_id) if rep.tmdb_id else None
        )
        cands = (
            (by_tmdb.get(tmdb_key) if tmdb_key else None) or by_imdb.get(rep.imdb_id or "") or []
        )
        if not cands:
            # The scan has not seen this title (added since the scan, or filtered out).
            not_in_scan += len(group)
            continue

        title_size = sum(c.size_bytes or 0 for c in cands)
        condemned = [c for c in cands if c.verdict == _CONDEMN]
        # Sum only what is measured; ``None`` when nothing about the condemned set is measured
        # (so the chip says "size unknown"), matching how the byte total treats unmeasured.
        measured = [c.size_bytes for c in condemned if c.size_bytes is not None]
        reclaimable_size: int | None = sum(measured) if measured else None

        link: ReclaimableTitle | None = None
        if condemned:
            reclaimable_titles.add(frozenset(c.candidate_id for c in condemned))
            for c in condemned:
                condemned_size_by_candidate[c.candidate_id] = c.size_bytes or 0
            display = condemned[0].group_title or condemned[0].title
            if len(cands) == 1:
                # A movie or a lone season: open its own card.
                link = ReclaimableTitle(display, reclaimable_size, condemned[0].candidate_id, None)
            else:
                # A show with condemned seasons: open the show, whose group carries them.
                gk = next((c.group_key for c in condemned if c.group_key), None)
                link = ReclaimableTitle(display, reclaimable_size, None, gk)

        # Who played any copy of this title, merged across its candidates' keys.
        plays_here: dict[int, int] = defaultdict(int)
        for c in cands:
            ev = evidence_by_key.get(str(c.plex_rating_key)) if c.plex_rating_key else None
            if ev:
                for uid, n in ev.plays_by_user.items():
                    plays_here[uid] += n

        # One row per distinct requester of this title (distinct by Seerr id, not plex_id).
        seen: set[int] = set()
        for req in group:
            uid = req.requester.seerr_user_id
            if uid in seen:
                continue
            seen.add(uid)
            row = _row(req)
            row.requests_made += 1
            row.gb_granted_bytes += title_size
            pid = req.requester.plex_id
            if pid is not None and plays_here.get(pid, 0) > 0:
                row.played_by_them += 1
            if link is not None:
                row.reclaimable_items += 1
                row.reclaimable_bytes += reclaimable_size or 0
                row.reclaimable.append(link)

    ordered = sorted(rows.values(), key=lambda r: r.gb_granted_bytes, reverse=True)
    for row in ordered:
        row.reclaimable.sort(key=lambda t: t.size_bytes or 0, reverse=True)

    return FairnessReport(
        rows=ordered,
        total_requests=len(requests),
        total_reclaimable_bytes=sum(condemned_size_by_candidate.values()),
        total_reclaimable_items=len(reclaimable_titles),
        not_in_scan=not_in_scan,
        horizon_at=horizon,
    )


async def _load_candidates(session: AsyncSession) -> tuple[bool, list[CandidateInfo]]:
    """The latest snapshot's candidates as ``CandidateInfo``. Returns ``(has_snapshot,
    candidates)`` so a never-scanned install (no snapshot at all) is told apart from a scan
    that legitimately found nothing."""
    snapshot = (
        await session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1))
    ).scalar_one_or_none()
    if snapshot is None:
        return False, []
    rows = (
        (await session.execute(select(Candidate).where(Candidate.snapshot_id == snapshot.id)))
        .scalars()
        .all()
    )
    return True, [
        CandidateInfo(
            candidate_id=c.id,
            plex_rating_key=c.plex_rating_key,
            verdict=c.verdict,
            # Kept as-is (NULL stays None, not 0): sums treat it as 0, but a title with no
            # measured size reads as "size unknown" on its chip rather than a false 0 B. The
            # reap path (not Scales) is what refuses to delete an unmeasured item.
            size_bytes=c.size_bytes,
            title=c.title,
            media_type=c.media_type,
            group_key=c.group_key,
            group_title=c.group_title,
            tmdb_id=c.tmdb_id,
            imdb_id=c.imdb_id,
        )
        for c in rows
    ]


async def _evidence_index(
    cache_engine: AsyncEngine, rating_keys: set[int]
) -> dict[str, WatchEvidence]:
    """For each candidate rating key, who played it and how many times.

    Matches the key against ``rating_key`` (a movie or episode), ``parent_rating_key`` (a
    season, whose episodes carry it), and ``grandparent_rating_key`` (a show), so one query
    serves a movie, a season and a show without the caller having to know which it is."""
    if not rating_keys:
        return {}
    await history_sync.ensure_schema(cache_engine)

    stmt = text(
        "SELECT key, user_id, SUM(plays) AS plays FROM ("
        "  SELECT rating_key AS key, user_id, COUNT(*) AS plays "
        "  FROM watch_event WHERE rating_key IN :keys GROUP BY rating_key, user_id "
        "  UNION ALL "
        "  SELECT parent_rating_key AS key, user_id, COUNT(*) AS plays "
        "  FROM watch_event WHERE parent_rating_key IN :keys GROUP BY parent_rating_key, user_id "
        "  UNION ALL "
        "  SELECT grandparent_rating_key AS key, user_id, COUNT(*) AS plays "
        "  FROM watch_event WHERE grandparent_rating_key IN :keys "
        "  GROUP BY grandparent_rating_key, user_id "
        ") GROUP BY key, user_id"
    ).bindparams(bindparam("keys", expanding=True))

    result: dict[str, dict[int, int]] = {}
    async with cache_engine.connect() as conn:
        # Chunked so a very large candidate set cannot exceed SQLite's bound-variable limit;
        # chunks are disjoint keys, so plain merging is exact.
        for chunk in batched(sorted(rating_keys), 500, strict=False):
            for key, user_id, plays in (await conn.execute(stmt, {"keys": list(chunk)})).all():
                result.setdefault(str(key), {})[int(user_id)] = int(plays)

    return {
        k: WatchEvidence(plays_by_user=plays, distinct_watchers=len(plays))
        for k, plays in result.items()
    }


async def build_report(
    *,
    session_factory: Callable[[], AsyncSession],
    seerr: SeerrClient,
    cache_engine: AsyncEngine,
) -> FairnessReport:
    """Gather what the roll-up needs -- the last scan's candidates, the available requests,
    and who played what -- then aggregate."""
    async with session_factory() as session:
        has_snapshot, candidates = await _load_candidates(session)
    if not has_snapshot:
        return FairnessReport(
            rows=[],
            total_requests=0,
            total_reclaimable_bytes=0,
            total_reclaimable_items=0,
            not_in_scan=0,
            no_snapshot=True,
        )

    requests = await seerr.all_requests(filter_="available")
    keys = {c.plex_rating_key for c in candidates if c.plex_rating_key is not None}
    evidence = await _evidence_index(cache_engine, keys)
    horizon = await history_sync.horizon(cache_engine)

    log.info(
        "fairness.built",
        requests=len(requests),
        candidates=len(candidates),
        keys_with_history=len(evidence),
    )
    return roll_up(requests, candidates, evidence, horizon=horizon)
