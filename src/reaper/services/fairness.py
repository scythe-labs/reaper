# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scales: who asked for what, and who actually watched it.

The screen the operator reaches for when the question is not "what should I delete?" but
"where is my disk going, and is it going to people who use it?" Per requester: how many
titles they asked for, how much disk that granted, how much of it they played, and how
much of it the last scan now considers expendable.

It is **read-only and deletes nothing.** It is a report.

**Scales sits on the last scan.** Every number here is joined to the latest snapshot's
candidates -- the same rows the review queue shows -- so Scales can never disagree with
Review. A title is *reclaimable* here exactly when the effective decision reclaims it: the
scan **condemns** it and no hand spare keeps it back, or the operator hand-reaps it and the
engine will honor that (``condemned.effective_condemned``, the one production decision, merged
at load -- rule 77/61). A title the scan protects, or one a hand spare keeps, is never
reclaimable, whatever the requester did. This is deliberate: the scan resolves each title to
its real Plex copies and folds watches across all of them, so leaning on its verdict is both
correct and drift-free. Re-deriving that resolution live would be a second copy of the app's
most delicate matching -- exactly what the "one decision" rule forbids.

**Per-person figures honor the request's scope.** A season-scoped request (Seerr's default
shape) attributes only the seasons it asked for, never the whole show (rule 78); the deduped
report totals stay over the whole matched title.

A request the last scan has not seen (added since, or filtered out) is surfaced as
*not in the last scan*, never silently counted as unwatched.

The join is on external ids -- tmdb first, then imdb -- present on both the Seerr request
and the stored candidate (rule 6/29: a stable id, never the title). Watches come from the
watch mirror (``cache.db``), keyed by the candidate's own Plex rating key, so a stale key
on the Seerr side can no longer make a watched title read as never-played.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import batched

import structlog
from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from reaper.aio import gather_reaped
from reaper.clients.base import IntegrationError
from reaper.clients.seerr import MediaRequest, QuotaStatus, SeerrClient, UserQuota
from reaper.clock import utcnow
from reaper.db import KEY_CHUNK
from reaper.db.models import Candidate, Snapshot
from reaper.services import condemned, history_sync, whitelist
from reaper.services.planner import MediaRef, PlanError

log = structlog.get_logger(__name__)

#: The scan verdict that makes a requested title reclaimable. Only ``condemn`` -- the
#: fail-closed reading of "the scan does not protect it": an abstain was *kept to be safe*,
#: so it is not offered up here either.
_CONDEMN = "condemn"


@dataclass(frozen=True)
class WatchEvidence:
    """What the watch mirror knows about who played one item.

    Lived in ``engine/requester.py`` beside a second, unwired condemn/protect/abstain decision
    function -- a whole parallel verdict path that only its own tests ever called. Scales was
    rebuilt to sit on the last scan instead of re-judging requests live, which left that rule
    with no consumer at all, so it is gone (rule 38) and the evidence it carried lives here,
    with the one surface that reads it.
    """

    plays_by_user: dict[int, int] = field(default_factory=dict)
    """Tautulli user_id -> play count."""

    distinct_watchers: int = 0

    def plays_by(self, plex_user_id: int | None) -> int:
        """How many times one person played it. ``None`` (an unlinked Seerr account, whose
        history Reaper cannot see) reads as zero plays here; every surface that could act on
        that number guards the ``None`` itself rather than letting it mean "never watched"."""
        if plex_user_id is None:
            return 0
        return self.plays_by_user.get(plex_user_id, 0)


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
    override: str | None = None
    """The manual decision in EFFECT on this candidate ("spare" | "reap" | None), resolved by
    ``whitelist.effective_override`` (own key, else its show's) at load. A spare keeps the item
    off the reclaimable board even when the frozen scan condemned it (rule 77/61); None for a
    row loaded without overrides (hand-built test candidates)."""
    effective_condemn: bool = False
    """Whether this candidate is reclaimable AFTER overrides: the frozen ``condemned.effective_
    condemned`` truth (a scan condemn not spared back, or a hand reap the engine will honor),
    computed once at load so the pure roll-up never re-decides (rule 3/22). Defaults ``False`` --
    the fail-closed reading -- so a candidate is only ever counted reclaimable when a loader (or
    a test) marks it so."""
    season_number: int | None = None
    """The season a TV candidate addresses (its ``sonarr:i:series:n`` tail), for scoping a
    season-scoped request to just the seasons it asked for (rule 78). None for a movie or an
    unparsable key."""
    tvdb_id: int | None = None
    """The show's TVDb id for a season row (Sonarr's native id); None for a movie and for
    rows scanned before the column existed. Sonarr is tvdb-native, so a show often shares
    only this id with its Seerr request; the join binds on it when tmdb is absent (rule 29)."""
    year: int | None = None
    """Display only, for the per-person details drawer's title rows."""
    poster_rating_key: int | None = None
    """The Plex key to draw the title's poster from. For a season this is the SHOW's key
    (many seasons have no poster of their own); a movie falls back to its own rating key. The
    per-person panel proxies it through ``/api/poster``, exactly like the review card."""


#: Why a requested title is not in the last scan. Each is a distinct thing to tell the
#: operator, so they are separated rather than lumped under one "not in scan" count.
UNMATCHED_AFTER_SCAN = "after_scan"
"""Its media arrived, or it was asked for, AFTER the scan ran -- the scan could not have
seen it. Reassuring: the next scan includes it. Chosen only when provable from the clock."""
UNMATCHED_SET_ASIDE = "set_aside"
"""Had a joinable id at scan time, but no candidate matched it. The honest catch-all when the
clock does not prove "added since": nothing is downloaded to disk yet, the copy on the server
is a different edition than the request (a different id), it is not tracked by any enabled
Sonarr/Radarr, or the server holding it was offline. Not "a show the keep rules fully
protect" -- since de8882f such a show is scanned and shows as Kept, not set aside."""
UNMATCHED_NO_ID = "no_id"
"""The request carries no tmdb, tvdb, or imdb id at all, so it cannot be lined up."""


@dataclass
class UnmatchedTitle:
    """One requested title the last scan did not include, for the "not in the last scan"
    panel. Mutated only to fill in the display name once (see :func:`_enrich_titles`), which
    the request payload never carries; everything else is set at construction.

    Merged by content across co-requesters -- one row per title, ``request_count`` requests
    behind it -- so the panel reads as titles while the card still counts requests. A no-id
    request cannot be merged (no key) and stands alone."""

    title: str | None
    """The display name, filled from Seerr's TMDB proxy. ``None`` until enriched, and left
    ``None`` when the lookup is impossible (no tmdb id) or fails; the row then shows a generic
    label built from the type and date, never an id."""
    year: int | None
    media_type: str  # "movie" | "tv"
    is_4k: bool
    """True when ANY request behind this title asked for the 4K copy."""
    requested_at: datetime | None
    """Earliest request across the co-requesters."""
    available_at: datetime | None
    """When the media arrived, if Seerr reported it -- the clock the reason is judged on."""
    reason: str  # UNMATCHED_AFTER_SCAN | UNMATCHED_SET_ASIDE | UNMATCHED_NO_ID
    requested_by: list[str]
    """Distinct requester display names behind this title, sorted."""
    request_count: int
    """How many requests this one row stands for, so the panel and the card's count agree."""
    tmdb_id: int | None
    """Internal, for the title lookup; never serialized to the client (no ids in the UI)."""
    portal_key: str
    """Internal: which portal to resolve the title on. Never serialized."""


@dataclass
class RequesterRow:
    """One person's row. Mutated during aggregation, then frozen into the report."""

    identity: str
    """The stable person key (see :func:`_identity`): ``plex:{id}`` for a Plex-linked
    account, ``local:{portal}:{seerr_id}`` for an unlinked one. Unique across portals, so two
    people who share a Seerr id on two portals never merge (rule 6/12). The frontend keys
    cards on it and opens the drawer by it."""
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


@dataclass(frozen=True)
class FairnessReport:
    rows: list[RequesterRow]
    total_requests: int
    total_reclaimable_bytes: int
    """Deduped by candidate -- a title requested by three people is counted once here."""
    total_reclaimable_items: int
    not_in_scan: int
    """Requests the last scan has not seen, counted PER REQUEST: no external id to join on,
    or no matching candidate. Shown so the numbers read as *most* of the requests, not all.
    Kept exactly equal to ``sum(u.request_count for u in unmatched)`` so the card's count and
    the panel's list can never disagree (rule 30)."""
    unmatched: list[UnmatchedTitle] = field(default_factory=list)
    """The not-in-scan requests themselves, merged by title and classified by reason, so the
    panel can name each one and say why. Titles are filled in by :func:`build_report`."""
    no_snapshot: bool = False
    """True when no scan has ever run: Scales has nothing to sit on, and says so."""
    horizon_at: datetime | None = None
    """How far back the watch mirror reaches. Older plays are invisible, so the watched
    figures are read against the right window."""


def _name(request: MediaRequest) -> str:
    r = request.requester
    return r.display_name or r.username or f"user:{r.seerr_user_id}"


def _identity(request: MediaRequest) -> str:
    """The stable person key every roll-up keys on, unique across portals.

    A Seerr user id is unique only *within* one portal, so keying on it alone merges two
    different people who share an id on two portals (the exact bug this exists to prevent).

    A Plex-linked account is one human across every portal -- their watches and quota already
    fold by Plex id -- so it keys on that. An unlinked local account has no Plex id (keying
    those on Plex id folded them all into one row, rule 6/12), and its Seerr id is unique only
    on its own portal, so it carries the portal too."""
    r = request.requester
    if r.plex_id is not None:
        return f"plex:{r.plex_id}"
    return f"local:{request.portal_key}:{r.seerr_user_id}"


def _profile_url(seerrs: Sequence[SeerrClient], request: MediaRequest) -> str | None:
    """The requester's page on the portal their request came through
    (``{link_base}/users/{seerr_user_id}``). Built from the request itself, so it works for a
    Plex-linked and an unlinked local account alike and needs no extra Seerr read. The link
    opens the operator's external address for the portal when they set one, else its connect
    address -- the same fallback the why-panel jump links use. ``None`` when the request
    carries no user id, or its portal is not among the open clients -- the panel then shows
    the name as plain text, never a dead link."""
    uid = request.requester.seerr_user_id
    if not uid:
        return None
    client = next((c for c in seerrs if c.instance_key == request.portal_key), None)
    if client is None:
        return None
    return f"{client.link_base_url or client.base_url}/users/{uid}"


ContentKey = tuple[str, str | int]


def _kind(media_type: str) -> str:
    """Which id space a row lives in: ``movie`` or ``tv``. TMDB movie ids and TMDB TV ids
    are separate, numerically overlapping spaces (a movie and a show can share the integer),
    so a tmdb join key must carry the kind or a TV request binds a same-numbered movie
    (rule 6/29). A candidate is stored as ``movie`` or ``season``; a season is a show, so it
    keys as ``tv``."""
    return "movie" if media_type == "movie" else "tv"


def _content_key(
    media_type: str, tmdb_id: int | None, imdb_id: str | None, tvdb_id: int | None
) -> ContentKey | None:
    """The id a request groups under: tmdb first, then tvdb, then imdb. ``None`` when the item
    carries none -- unjoinable, and surfaced as not-in-scan rather than guessed.

    The tmdb key is namespaced by media kind (``tmdb-movie`` / ``tmdb-tv``) so the movie and
    TV id spaces cannot collide. TVDb ids are a TV-only space and IMDb ids are globally
    unique, so neither needs a kind (rule 6/29). This only groups co-requesters of one title;
    binding a request to the scan's candidates is _match_candidates, which tries every id."""
    if tmdb_id:
        return (f"tmdb-{_kind(media_type)}", tmdb_id)
    if tvdb_id:
        return ("tvdb", tvdb_id)
    if imdb_id:
        return ("imdb", imdb_id)
    return None


@dataclass(frozen=True)
class _CandidateIndex:
    """The last scan's candidates indexed by every id kind they carry, so a request keyed by
    any one of tmdb / tvdb / imdb finds them. Built once per join site and shared by the board
    roll-up, the not-in-scan classifier, and the person drawer, so the three can never drift
    on what "the scan has this title" means (rule 3/22)."""

    by_tmdb: dict[ContentKey, list[CandidateInfo]]
    by_tvdb: dict[int, list[CandidateInfo]]
    by_imdb: dict[str, list[CandidateInfo]]


def _index_candidates(candidates: list[CandidateInfo]) -> _CandidateIndex:
    """Index candidates by every id they carry. A show contributes several season rows under
    one id. The tmdb index is namespaced by media kind (movie/tv ids overlap); tvdb is a
    TV-only space and imdb is globally unique, so neither is namespaced. Passing every id is
    what lets a show Sonarr knows only by tvdb still bind its tmdb-keyed request (rule 29)."""
    by_tmdb: dict[ContentKey, list[CandidateInfo]] = defaultdict(list)
    by_tvdb: dict[int, list[CandidateInfo]] = defaultdict(list)
    by_imdb: dict[str, list[CandidateInfo]] = defaultdict(list)
    for c in candidates:
        if c.tmdb_id:
            by_tmdb[(f"tmdb-{_kind(c.media_type)}", c.tmdb_id)].append(c)
        if c.tvdb_id:
            by_tvdb[c.tvdb_id].append(c)
        if c.imdb_id:
            by_imdb[c.imdb_id].append(c)
    return _CandidateIndex(by_tmdb, by_tvdb, by_imdb)


def _match_candidates(index: _CandidateIndex, req: MediaRequest) -> list[CandidateInfo]:
    """The candidates a request binds, trying every id it carries in turn: tmdb (kind-
    namespaced), then tvdb, then imdb. The first id that resolves wins; a candidate is indexed
    under all of its own ids, so the sets agree and the order only picks which non-empty one
    is read. Empty exactly when the scan has no candidate for this request."""
    tmdb_key: ContentKey | None = (
        (f"tmdb-{_kind(req.media_type)}", req.tmdb_id) if req.tmdb_id else None
    )
    return (
        (index.by_tmdb.get(tmdb_key) if tmdb_key else None)
        or (index.by_tvdb.get(req.tvdb_id) if req.tvdb_id else None)
        or index.by_imdb.get(req.imdb_id or "")
        or []
    )


def _season_number(media_key: str) -> int | None:
    """The season a candidate's key addresses, or ``None`` (a movie, or a key that does not
    parse). Reuses the one ``MediaRef`` parser -- never a second season-tail reader (rule 6/29) --
    and, like the review-queue extractor, treats a parse failure as unknown rather than erroring
    a row out of the report."""
    try:
        return MediaRef.parse(media_key).season
    except PlanError:
        return None


def _scope_to_request(cands: list[CandidateInfo], req: MediaRequest) -> list[CandidateInfo]:
    """The candidates one request actually asked for. A season-scoped TV request (``req.seasons``
    non-empty) binds ONLY those season numbers; a movie, or a whole-show request (empty seasons),
    binds the whole matched set (rule 78).

    Charging a person the whole show for a single-season request is the exact over-attribution
    B-6 fixes, so a season whose number is unknown (``None``) is treated as out of a specific
    scope rather than swept in -- fail toward under-, never over-, attributing disk on a
    report about who is holding it. A movie never scopes (movies have no seasons)."""
    if req.media_type == "movie" or not req.seasons:
        return cands
    wanted = set(req.seasons)
    return [c for c in cands if c.season_number in wanted]


def _unmatched_reason(group: list[MediaRequest], snapshot_at: datetime | None) -> str:
    """Why a joinable title produced no candidate: added since the scan, or set aside. Rounds
    toward SET-ASIDE whenever the clock does not *prove* "added since," so a title is never
    reassuringly labeled "new" when it might in fact be present and skipped.

    ``available_at`` (when the media arrived) is the honest clock: media that arrived after
    the scan ran cannot have been in it. Only when arrival is entirely unknown does it fall
    back to ``requested_at`` -- if it is available now and was asked for after the scan, it
    must have arrived after the scan too."""
    if snapshot_at is None:
        return UNMATCHED_SET_ASIDE
    if any(r.available_at and r.available_at > snapshot_at for r in group):
        return UNMATCHED_AFTER_SCAN
    if all(r.available_at is None for r in group) and any(
        r.requested_at and r.requested_at > snapshot_at for r in group
    ):
        return UNMATCHED_AFTER_SCAN
    return UNMATCHED_SET_ASIDE


def _unmatched_row(
    group: list[MediaRequest], reason: str, *, identity: str | None = None
) -> UnmatchedTitle:
    """One panel row from a group of co-requests for the same not-in-scan title.

    ``request_count`` counts every request behind the title for the board (``identity`` None),
    matching the card's per-request count; for one person's drawer (``identity`` set) it counts
    only that person's own requests, matching "this person's requests the scan has not seen.\""""
    rep = group[0]
    count = (
        sum(1 for r in group if _identity(r) == identity) if identity is not None else len(group)
    )
    return UnmatchedTitle(
        title=None,
        year=None,
        media_type=_kind(rep.media_type),
        is_4k=any(r.is_4k for r in group),
        requested_at=min((r.requested_at for r in group if r.requested_at), default=None),
        available_at=next((r.available_at for r in group if r.available_at), None),
        reason=reason,
        requested_by=sorted({_name(r) for r in group}),
        request_count=count,
        tmdb_id=rep.tmdb_id,
        portal_key=rep.portal_key,
    )


def _collect_unmatched(
    requests: list[MediaRequest],
    candidates: list[CandidateInfo],
    snapshot_at: datetime | None,
    *,
    identity: str | None = None,
) -> list[UnmatchedTitle]:
    """Every available request the last scan did not include, merged by title and classified
    by why. The one place that decision lives, shared by the board (``identity`` None -- all
    people) and the person drawer (``identity`` set -- only titles that person asked for), so
    the two surfaces can never disagree on what "not in scan" means.

    A request is unmatched when it carries no joinable id (NO-ID, and unmergeable so it stands
    alone) or when its title produced no candidate (ADDED-since vs SET-aside, by the clock)."""
    index = _index_candidates(candidates)

    out: list[UnmatchedTitle] = []
    groups: dict[ContentKey, list[MediaRequest]] = defaultdict(list)
    for req in requests:
        key = _content_key(req.media_type, req.tmdb_id, req.imdb_id, req.tvdb_id)
        if key is None:
            if identity is not None and _identity(req) != identity:
                continue
            out.append(_unmatched_row([req], UNMATCHED_NO_ID, identity=identity))
            continue
        groups[key].append(req)

    for group in groups.values():
        rep = group[0]
        cands = _match_candidates(index, rep)
        if cands:
            # The scan has the TITLE, which is not the same as having what was asked for: a
            # request for season 5 of a show the scan holds seasons 1-3 of scopes to nothing
            # (rule 30). It used to fall between every surface -- it counted toward the
            # board's "N requests" and the watch-rate denominator, but the drawer's
            # in-scan list skipped it and this classifier called the group matched, so the
            # operator saw a request that was findable nowhere. Classified per request, not
            # per group: its co-requesters may well have asked for seasons the scan does
            # hold, and they stay matched.
            group = [r for r in group if not _scope_to_request(cands, r)]
            if not group:
                continue
        if identity is not None and not any(_identity(r) == identity for r in group):
            continue
        out.append(_unmatched_row(group, _unmatched_reason(group, snapshot_at), identity=identity))

    return out


def roll_up(
    requests: list[MediaRequest],
    candidates: list[CandidateInfo],
    evidence_by_key: dict[str, WatchEvidence],
    *,
    horizon: datetime | None = None,
    snapshot_at: datetime | None = None,
) -> FairnessReport:
    """Pure aggregation: given available requests, the scan's candidates, and who played
    what, roll up per requester. Split from the gathering so the correctness that matters --
    the id join, the override-aware reclaimable gate (``CandidateInfo.effective_condemn``), the
    per-person season scope (rule 78), the deduped totals -- is testable with no live instance
    and no database.

    ``snapshot_at`` (the scan's own timestamp) only classifies the not-in-scan requests into
    "added since" vs "set aside"; it never touches a score or a verdict.
    """
    # Candidates indexed by every id they carry, so a request keyed by tmdb, tvdb OR imdb
    # finds them (see _index_candidates; rule 29: pass every id).
    index = _index_candidates(candidates)

    # Group requests by the content they point at, so co-requesters of one title are judged
    # together. A request with no joinable id is not-in-scan straight away.
    groups: dict[ContentKey, list[MediaRequest]] = defaultdict(list)
    for req in requests:
        key = _content_key(req.media_type, req.tmdb_id, req.imdb_id, req.tvdb_id)
        if key is None:
            continue
        groups[key].append(req)

    # Keyed on the cross-portal person identity (see _identity), never a bare Seerr user id:
    # that id is unique only within one portal, so keying on it merged two different people
    # who share an id across two portals (rule 6/12).
    rows: dict[str, RequesterRow] = {}
    # Deduped by the condemned candidate set, so the same title reached via a tmdb group and
    # an imdb group counts once -- the way the bytes total already dedupes by candidate id.
    reclaimable_titles: set[frozenset[int]] = set()
    condemned_size_by_candidate: dict[int, int] = {}
    #: (person, the candidate set their request matched) pairs already attributed. Spans
    #: every group, because one title can be reached through more than one of them.
    seen: set[tuple[str, frozenset[int]]] = set()

    def _row(req: MediaRequest) -> RequesterRow:
        ident = _identity(req)
        row = rows.get(ident)
        if row is None:
            row = RequesterRow(identity=ident, plex_id=req.requester.plex_id, name=_name(req))
            rows[ident] = row
        return row

    for group in groups.values():
        rep = group[0]
        cands = _match_candidates(index, rep)
        if not cands:
            # The scan has not seen this title; it is collected (and counted) below by
            # _collect_unmatched, the one place that classifies not-in-scan.
            continue

        # The report-level reclaimable set (deduped across people) is the whole matched title's
        # EFFECTIVELY-condemned candidates: a hand spare keeps its item off the board even though
        # the frozen scan condemned it, and a hand reap the engine honors adds one. That truth is
        # condemned.effective_condemned, frozen onto ``effective_condemn`` at load, so this pure
        # roll-up never re-decides a verdict (rule 3/22/77). The per-PERSON figures below scope
        # to each requester's own seasons; this report total stays over the whole matched title.
        condemned = [c for c in cands if c.effective_condemn]
        if condemned:
            reclaimable_titles.add(frozenset(c.candidate_id for c in condemned))
            for c in condemned:
                condemned_size_by_candidate[c.candidate_id] = c.size_bytes or 0

        # One row per distinct requester (by cross-portal identity), each attributed ONLY the
        # seasons they asked for: a season-scoped request never charges the whole show (B-6,
        # rule 78). Co-requesters of one show can scope different seasons, so this is per request.
        matched_key = frozenset(c.candidate_id for c in cands)
        for req in group:
            scoped = _scope_to_request(cands, req)
            if not scoped:
                # The title is in the scan but none of the seasons THEY asked for are, so
                # there is nothing of theirs to attribute here, and counting the request
                # anyway inflated both the board's request count and the denominator of
                # their watch rate with something that could never move the numerator
                # (B-28, rule 30). Skipped exactly as the person drawer skips it
                # (``build_person_detail``), which is the point: counting it here and not
                # there made the card say "4 requests, 50% they watched" while the panel it
                # opens said "3 requests in the last scan, 67% They watched" for the same
                # person and the same scan. A person with only such requests gets no row at
                # all, matching the drawer, which has no detail to show them. It is not lost
                # either: _collect_unmatched classifies exactly this case, so it surfaces in
                # the not-in-scan panel and the two surfaces agree on the count.
                continue
            ident = _identity(req)
            # Deduped across GROUPS, not just within one. A single title splits into two
            # content-key groups when some co-requests carry tmdb and others only imdb, and
            # both resolve to the SAME candidates -- every candidate is indexed under every
            # id it carries. A person in both groups was charged twice: requests, granted
            # disk and reclaimable items all doubled on their row while the report totals
            # (deduped by candidate set) stayed right, so the board disagreed with itself.
            if (ident, matched_key) in seen:
                continue
            seen.add((ident, matched_key))
            row = _row(req)
            row.requests_made += 1
            row.gb_granted_bytes += sum(c.size_bytes or 0 for c in scoped)

            pid = req.requester.plex_id
            if pid is not None:
                # Did THEY play any of the seasons they asked for (not the whole show)?
                scoped_plays = 0
                for c in scoped:
                    ev = evidence_by_key.get(str(c.plex_rating_key)) if c.plex_rating_key else None
                    if ev:
                        scoped_plays += ev.plays_by(pid)
                if scoped_plays > 0:
                    row.played_by_them += 1

            scoped_condemned = [c for c in scoped if c.effective_condemn]
            if scoped_condemned:
                # Sum only what is measured, so an unsizable item adds nothing to the byte
                # total while still counting toward the item count.
                row.reclaimable_items += 1
                row.reclaimable_bytes += sum(
                    c.size_bytes for c in scoped_condemned if c.size_bytes is not None
                )

    ordered = sorted(rows.values(), key=lambda r: r.gb_granted_bytes, reverse=True)

    # The not-in-scan list and its count come from the one shared classifier, so the card's
    # count is exactly the requests behind the panel's rows (rule 30).
    unmatched = _collect_unmatched(requests, candidates, snapshot_at)
    not_in_scan = sum(u.request_count for u in unmatched)

    return FairnessReport(
        rows=ordered,
        total_requests=len(requests),
        total_reclaimable_bytes=sum(condemned_size_by_candidate.values()),
        total_reclaimable_items=len(reclaimable_titles),
        not_in_scan=not_in_scan,
        unmatched=unmatched,
        horizon_at=horizon,
    )


# ---------------------------------------------------------------------------
# Per-person detail (the Scales drawer) and Seerr request quota.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuotaLine:
    """One media type's request limit for a person, aggregated across portals.

    Movies and series stay separate: their windows and units differ (movies per N days,
    seasons per M days), so they are never merged into a single 'quota'. When a person has
    accounts on several portals the *tightest* finite limit is shown, and ``at_limit`` is
    true if they are capped on any of them -- the honest 'most constrained' reading.
    ``limit is None`` is unlimited.
    """

    limit: int | None
    days: int | None
    at_limit: bool

    @property
    def unlimited(self) -> bool:
        return self.limit is None


@dataclass(frozen=True)
class PersonQuota:
    seerr_total: int
    movie: QuotaLine
    tv: QuotaLine


@dataclass(frozen=True)
class PersonTitle:
    """One title a person requested that the scan still has, for the drawer's list."""

    title: str
    year: int | None
    media_type: str
    is_4k: bool
    size_bytes: int | None
    """None when nothing about the title is measured; the row says "size unknown"."""
    requested_at: datetime | None
    available_at: datetime | None
    watched_by_them: int
    """How much of it they watched: a movie's raw plays, but a series' DISTINCT episodes
    watched (a resumed episode counts once). The row's wording follows ``media_type``."""
    verdict: str  # condemn (reclaimable) | protect | abstain
    item_id: int | None
    group_key: str | None
    co_requesters: tuple[str, ...]
    poster_url: str | None
    """A ``/api/poster/{key}`` URL, or ``None`` when the title carries no poster key. The
    panel shows a film-strip placeholder in that case rather than a broken image."""


@dataclass(frozen=True)
class PersonDetail:
    plex_id: int | None
    name: str
    seerr_total: int | None
    requests_in_scan: int
    gb_granted_bytes: int
    played_by_them: int
    reclaimable_items: int
    reclaimable_bytes: int
    quota: PersonQuota | None
    titles: list[PersonTitle]
    not_in_scan: int
    """This person's requests the scan has not seen -- shown so the list reads as most of
    what they asked for, not all. Equals ``sum(u.request_count for u in unmatched)``."""
    unmatched: list[UnmatchedTitle] = field(default_factory=list)
    """This person's not-in-scan requests, merged by title and classified by reason, with
    names filled in -- the same panel the board shows, scoped to them."""
    profile_url: str | None = None
    """The requester's page on the portal their request came through
    (``{base_url}/users/{seerr_user_id}``), or ``None`` when it cannot be built. Display only;
    the panel opens it in a new tab, and shows the name as plain text when it is ``None``."""
    horizon_at: datetime | None = None
    """How far back the watch mirror reaches, the same span :class:`FairnessReport` carries for
    the board. Every watch figure on this shape -- ``played_by_them`` and each title's
    ``watched_by_them`` -- is counted with no lower time bound, so a person whose plays all
    predate this reads as a LOWER BOUND, never a measured zero. The panel prints the span
    beside those figures and qualifies a zero with it; ``None`` (an empty mirror) is not a
    deeper span but no span at all, and the panel then asserts no figure."""


def _fold_quota(statuses: Iterable[QuotaStatus]) -> QuotaLine:
    """Aggregate one media type across a person's portals: the tightest finite limit, and
    at_limit if capped on any. An empty iterable (nothing readable) reads as unlimited and
    not-at-limit -- the safe display default, never a made-up cap."""
    limit: int | None = None
    days: int | None = None
    at_limit = False
    for s in statuses:
        at_limit = at_limit or s.restricted
        if s.limit is not None and (limit is None or s.limit < limit):
            limit, days = s.limit, s.days
    return QuotaLine(limit=limit, days=days, at_limit=at_limit)


async def _enrich_accounts(
    seerrs: Sequence[SeerrClient], targets: set[int | None]
) -> dict[int, PersonQuota]:
    """Best-effort Seerr account data for the given people: lifetime request counts and the
    live per-type caps, aggregated across portals.

    Display-only, so best-effort by design: a portal whose user list or quota cannot be
    read simply contributes nothing, and the core report (who requested what) is never
    blocked by it. Only real plex ids are enriched -- an unmatched requester carries no
    Seerr account to look up.
    """
    wanted = {t for t in targets if t is not None}
    if not wanted or not seerrs:
        return {}

    # Resolve each wanted person to their (client, seerr_user_id) on every reachable portal,
    # summing lifetime request counts as we go.
    resolved: dict[int, list[tuple[SeerrClient, int]]] = defaultdict(list)
    totals: dict[int, int] = defaultdict(int)
    for client in seerrs:
        try:
            users = await client.users()
        except IntegrationError as exc:
            log.warning("fairness.users_unreadable", error=str(exc))
            continue
        for u in users:
            if u.plex_id in wanted:
                resolved[u.plex_id].append((client, u.seerr_user_id))
                totals[u.plex_id] += u.request_count

    if not resolved:
        return {}

    # Fetch every needed quota concurrently; a failed one contributes nothing.
    calls = [(pid, client, uid) for pid, es in resolved.items() for (client, uid) in es]
    results = await asyncio.gather(
        *(client.quota(uid) for _, client, uid in calls), return_exceptions=True
    )
    movie_by: dict[int, list[QuotaStatus]] = defaultdict(list)
    tv_by: dict[int, list[QuotaStatus]] = defaultdict(list)
    for (pid, _, _), res in zip(calls, results, strict=True):
        if isinstance(res, UserQuota):
            movie_by[pid].append(res.movie)
            tv_by[pid].append(res.tv)
        elif isinstance(res, IntegrationError):
            log.warning("fairness.quota_unreadable", error=str(res))
        elif isinstance(res, BaseException):
            raise res  # a real bug (or cancellation), never swallowed as "no quota"

    return {
        pid: PersonQuota(
            seerr_total=totals[pid],
            movie=_fold_quota(movie_by.get(pid, [])),
            tv=_fold_quota(tv_by.get(pid, [])),
        )
        for pid in resolved
    }


#: Cap on how many not-in-scan titles get a live name lookup per report, so a library with a
#: huge backlog cannot turn one Scales load into hundreds of Seerr calls. Rows past the cap
#: still appear, named by their type and date instead -- the count is never truncated.
_TITLE_LOOKUP_CAP = 80


async def _enrich_titles(
    seerrs: Sequence[SeerrClient], unmatched: Sequence[UnmatchedTitle]
) -> None:
    """Fill in each not-in-scan title's display name from Seerr's TMDB proxy, in place.

    Best-effort, exactly like :func:`_enrich_accounts`: a request payload carries only ids, so
    the name is looked up live; a lookup that fails (or a title with no tmdb id, or one past
    the per-report cap) simply keeps ``title=None`` and the row shows a generic label. Titles
    are resolved on the portal the request came from, since that portal certainly has TMDB
    access configured; any other reachable portal is an acceptable fallback (all proxy the
    same TMDB). Bounded by the not-in-scan count and the cap, so the added calls are bounded."""
    targets = [u for u in unmatched if u.tmdb_id is not None][:_TITLE_LOOKUP_CAP]
    if not targets or not seerrs:
        return
    by_portal = {c.instance_key: c for c in seerrs}

    async def _one(u: UnmatchedTitle) -> None:
        client = by_portal.get(u.portal_key) or seerrs[0]
        try:
            info = await client.title(tmdb_id=u.tmdb_id or 0, media_type=u.media_type)
        except IntegrationError as exc:
            log.warning("fairness.title_unreadable", tmdb=u.tmdb_id, error=str(exc))
            return
        u.title = info.title
        # Prefer the looked-up year; the request itself never carried one.
        u.year = info.year

    # ``_one`` swallows every ordinary failure itself, so nothing normally escapes to detach
    # the siblings here. Reaped anyway, for the case that is not ordinary: up to 80 lookups
    # ride the same clients, and a cancellation or a genuine bug in one must not leave the
    # other 79 reading against a client the route is unwinding past (rule 34).
    await gather_reaped(*(_one(u) for u in targets))


async def _load_candidates(
    session: AsyncSession,
) -> tuple[datetime | None, list[CandidateInfo]]:
    """The latest snapshot's timestamp and its candidates as ``CandidateInfo``. Returns
    ``(snapshot_at, candidates)``; ``snapshot_at`` is ``None`` only when no scan has ever run
    (a snapshot always carries a ``created_at``), which tells a never-scanned install apart
    from a scan that legitimately found nothing. The timestamp classifies not-in-scan
    requests into "added since" vs "set aside".

    Live hand overrides are merged in the same breath, exactly as the review routes do (rule
    77): ``effective_override`` colors each candidate, and ``condemned.effective_condemned`` --
    the ONE production reclaimable decision (rule 3/22) -- says which candidates are reclaimable
    after a spare keeps one back or an engine-honored hand reap adds one. So Scales and Review
    can never disagree about a hand-decided title."""
    snapshot = (
        await session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1))
    ).scalar_one_or_none()
    if snapshot is None:
        return None, []
    rows = (
        (await session.execute(select(Candidate).where(Candidate.snapshot_id == snapshot.id)))
        .scalars()
        .all()
    )
    decisions = await whitelist.overrides(session)
    reclaimable_keys = set(await condemned.effective_condemned(session, snapshot.id, decisions))
    return snapshot.created_at, [
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
            tvdb_id=c.tvdb_id,
            year=c.year,
            poster_rating_key=c.poster_rating_key,
            override=whitelist.effective_override(c.media_key, decisions),
            effective_condemn=c.media_key in reclaimable_keys,
            season_number=_season_number(c.media_key),
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
        for chunk in batched(sorted(rating_keys), KEY_CHUNK, strict=False):
            for key, user_id, plays in (await conn.execute(stmt, {"keys": list(chunk)})).all():
                result.setdefault(str(key), {})[int(user_id)] = int(plays)

    return {
        k: WatchEvidence(plays_by_user=plays, distinct_watchers=len(plays))
        for k, plays in result.items()
    }


async def _distinct_episodes(
    cache_engine: AsyncEngine, plex_id: int | None, season_keys: set[int]
) -> dict[int, int]:
    """For one person, the number of DISTINCT episodes they watched under each season (an
    episode's ``parent_rating_key`` is its season). Summed across a show's seasons this is the
    show's distinct-episodes-watched, which is what the panel shows for a series -- a resumed
    or rewatched episode counts once, unlike raw plays. Movies never come here (they show
    plays); this is keyed only on season parents."""
    if not season_keys or plex_id is None:
        return {}
    stmt = text(
        "SELECT parent_rating_key AS k, COUNT(DISTINCT rating_key) AS eps "
        "FROM watch_event WHERE user_id = :pid AND parent_rating_key IN :keys "
        "GROUP BY parent_rating_key"
    ).bindparams(bindparam("keys", expanding=True))

    out: dict[int, int] = {}
    async with cache_engine.connect() as conn:
        for chunk in batched(sorted(season_keys), KEY_CHUNK, strict=False):
            rows = (await conn.execute(stmt, {"pid": plex_id, "keys": list(chunk)})).all()
            for k, eps in rows:
                out[int(k)] = int(eps)
    return out


_REQUEST_CACHE_TTL = timedelta(seconds=30)


async def _fetch_available(seerrs: Sequence[SeerrClient]) -> list[MediaRequest]:
    """Every "available" request across every portal, fetched CONCURRENTLY. The portal reads are
    independent, and paging them serially was the drawer's seconds-slow cost (P-1). Fail-hard:
    the first portal error propagates exactly as the old serial loop did, so an unreachable
    portal still 502s rather than a partial board.

    Through ``gather_reaped``, like every other fan-out against an operator's live services. A
    bare ``asyncio.gather`` re-raises the first failure and leaves its siblings *running* --
    against clients the route is about to close as its exit stack unwinds, so a portal still
    paging several thousand requests dies mid-read on "the client has been closed", inside a
    task nobody is awaiting. Reaping cancels and drains them first, so the failure the caller
    sees is the only thing still in flight."""
    results = await gather_reaped(*(s.all_requests(filter_="available") for s in seerrs))
    return [r for sub in results for r in sub]


class RequestCache:
    """A tiny TTL cache for the merged "available" request list, shared by the board and the
    drawer. It lives on ``app.state`` (one per app), never module-global, so tests stay hermetic
    (rule 37). Opening a person right after a board load -- or reloading the board -- then re-pages
    no portal within the window, which is where P-1's repeated full re-reads went. Fail-hard is
    preserved: a portal error propagates out of :meth:`available` before anything is stored, so a
    partial view is never cached; and the cache keys on the exact portal set, so a config change
    never serves the old portals' requests."""

    def __init__(self, ttl: timedelta = _REQUEST_CACHE_TTL) -> None:
        self._ttl = ttl
        self._at: datetime | None = None
        self._key: str | None = None
        self._requests: list[MediaRequest] | None = None

    async def available(self, seerrs: Sequence[SeerrClient]) -> list[MediaRequest]:
        key = ",".join(sorted(c.instance_key for c in seerrs))
        now = utcnow()
        if (
            self._requests is not None
            and self._key == key
            and self._at is not None
            and now - self._at < self._ttl
        ):
            return self._requests
        merged = await _fetch_available(seerrs)
        self._at, self._key, self._requests = now, key, merged
        return merged


async def _available_requests(
    seerrs: Sequence[SeerrClient], cache: RequestCache | None
) -> list[MediaRequest]:
    """The merged available requests, through the shared cache when one is provided (the API
    routes pass the app's), else a fresh concurrent fetch (the service tests call this way)."""
    return await cache.available(seerrs) if cache is not None else await _fetch_available(seerrs)


async def build_report(
    *,
    session_factory: Callable[[], AsyncSession],
    seerrs: list[SeerrClient],
    cache_engine: AsyncEngine,
    cache: RequestCache | None = None,
) -> FairnessReport:
    """Gather what the roll-up needs -- the last scan's candidates, the available requests
    across *every* Seerr, and who played what -- then aggregate.

    Seerr is multi-instance: every enabled portal is read and its requests merged, so a
    person who only ever asked through the second portal still appears. Fail-hard: if any
    Seerr is unreachable the ``IntegrationError`` propagates and the endpoint answers 502,
    never a leaderboard that looks complete while a portal was silently skipped. ``cache`` is
    the app-scoped :class:`RequestCache` the board shares with the drawer (P-1); ``None`` fetches
    fresh."""
    async with session_factory() as session:
        snapshot_at, candidates = await _load_candidates(session)
    if snapshot_at is None:
        return FairnessReport(
            rows=[],
            total_requests=0,
            total_reclaimable_bytes=0,
            total_reclaimable_items=0,
            not_in_scan=0,
            no_snapshot=True,
        )

    requests = await _available_requests(seerrs, cache)
    keys = {c.plex_rating_key for c in candidates if c.plex_rating_key is not None}
    evidence = await _evidence_index(cache_engine, keys)
    horizon = await history_sync.horizon(cache_engine)

    report = roll_up(requests, candidates, evidence, horizon=horizon, snapshot_at=snapshot_at)

    # The board deliberately reads no Seerr *account* data. It used to fold in lifetime
    # request counts and live per-type caps for every person shown, which cost one user list
    # plus one quota call per person per portal, uncached, on every load -- and nothing on the
    # card has rendered those numbers since the chips came off. The drawer still shows them
    # for the one person it opens (``build_person_detail`` calls ``_enrich_accounts``), which
    # is where the cost is worth paying.

    # Name the not-in-scan titles for the panel. Bounded by the not-in-scan count, and only
    # paid on a report that has some -- most loads pay nothing.
    await _enrich_titles(seerrs, report.unmatched)

    log.info(
        "fairness.built",
        requests=len(requests),
        candidates=len(candidates),
        keys_with_history=len(evidence),
        not_in_scan=report.not_in_scan,
    )
    return report


async def build_person_detail(
    *,
    session_factory: Callable[[], AsyncSession],
    seerrs: Sequence[SeerrClient],
    cache_engine: AsyncEngine,
    identity: str,
    cache: RequestCache | None = None,
) -> PersonDetail | None:
    """One person's full request breakdown for the Scales drawer: every title they asked
    for that the scan still has, each with when it was requested and when it arrived,
    whether they watched it, its fate, and who else asked. Plus their Seerr account totals
    and caps.

    Keyed on the cross-portal ``identity`` the roll-up assigns each row (see :func:`_identity`)
    -- never a bare Seerr user id, which collides across portals (rule 6/12), nor the name
    (shared). Returns ``None`` when no one by that identity is in the current scan. Fail-hard
    on an unreachable Seerr, exactly like :func:`build_report`: a partial breakdown that looks
    complete is worse than an error. ``cache`` is the app-scoped :class:`RequestCache` the drawer
    shares with the board (P-1), so opening a person right after a board load re-pages no portal.
    """
    async with session_factory() as session:
        snapshot_at, candidates = await _load_candidates(session)
    if snapshot_at is None:
        return None

    requests = await _available_requests(seerrs, cache)
    evidence = await _evidence_index(
        cache_engine, {c.plex_rating_key for c in candidates if c.plex_rating_key is not None}
    )
    # Distinct episodes this person watched, per season, so a series row can show episodes
    # watched (not inflated raw plays). Keyed on the target's plex id, gathered once.
    target_plex_id = next((r.requester.plex_id for r in requests if _identity(r) == identity), None)
    season_keys = {
        c.plex_rating_key
        for c in candidates
        if c.plex_rating_key is not None and c.media_type == "season"
    }
    episodes_by_season = await _distinct_episodes(cache_engine, target_plex_id, season_keys)

    # The same index the roll-up and classifier use, so all three agree on what the scan has
    # (rule 3/22): a request binds by tmdb, tvdb OR imdb (rule 6/29).
    index = _index_candidates(candidates)

    # Group by content so co-requesters are known and the target's request for a title is
    # judged with everyone else's.
    groups: dict[ContentKey, list[MediaRequest]] = defaultdict(list)
    for req in requests:
        ck = _content_key(req.media_type, req.tmdb_id, req.imdb_id, req.tvdb_id)
        if ck is not None:
            groups[ck].append(req)

    titles: list[PersonTitle] = []
    #: Candidate sets already listed, so one title reached through two id-split groups is
    #: one row here rather than two (see the roll-up's ``seen``).
    seen_titles: set[frozenset[int]] = set()
    name = ""
    plex_id: int | None = None
    granted = played = recl_items = recl_bytes = 0
    matched_any = False
    for group in groups.values():
        mine = next((r for r in group if _identity(r) == identity), None)
        if mine is None:
            continue
        name = _name(mine)
        plex_id = mine.requester.plex_id
        rep = group[0]
        cands = _match_candidates(index, rep)
        if not cands:
            # Not in the scan: collected (and named) below by the shared classifier.
            continue
        # Only the seasons THIS person asked for (rule 78). A season-scoped request whose
        # seasons are all absent from the scan scopes to nothing -- there is nothing of theirs
        # to attribute here, so it is skipped rather than shown as a 0-byte matched title.
        # (The board skips it identically, and the shared classifier below lists it under
        # not-in-scan, so the two surfaces agree on where it went.)
        scoped = _scope_to_request(cands, mine)
        if not scoped:
            continue
        # The board's cross-group dedup, on the drawer's twin of the same loop (rule 72): a
        # title reached through both a tmdb group and an imdb group resolves to one candidate
        # set, and counting it once per group listed it twice and doubled this person's
        # granted and reclaimable figures.
        matched_key = frozenset(c.candidate_id for c in cands)
        if matched_key in seen_titles:
            continue
        seen_titles.add(matched_key)
        matched_any = True

        granted += sum(c.size_bytes or 0 for c in scoped)
        # Nullable, matching the roll-up: None when nothing about the set is measured, so the
        # row says "size unknown" rather than a false 0 B.
        measured = [c.size_bytes for c in scoped if c.size_bytes is not None]
        title_size: int | None = sum(measured) if measured else None
        # Reclaimable after overrides, not just the frozen verdict (rule 77/61): a hand spare
        # keeps its season off this set, an engine-honored hand reap adds one.
        eff_condemned = [c for c in scoped if c.effective_condemn]

        plays = 0
        for c in scoped:
            ev = evidence.get(str(c.plex_rating_key)) if c.plex_rating_key else None
            if ev:
                plays += ev.plays_by(plex_id)
        if plays > 0:
            played += 1
        # What the row shows: a movie's raw plays ("watched 3x"), but a series' DISTINCT
        # episodes watched ("62 episodes watched") -- a resumed episode is one episode, so the
        # figure reads naturally and never inflates the way summed plays would.
        if scoped[0].media_type == "movie":
            watched_shown = plays
        else:
            watched_shown = sum(
                episodes_by_season.get(c.plex_rating_key, 0)
                for c in scoped
                if c.plex_rating_key is not None
            )

        # Title-level fate on the seasons they asked for: reclaimable if ANY is effectively
        # condemned (a show is on the reap lane if any season is, rule 48); else abstain if any
        # is still UNDECIDED; else protect (a hand-kept item reads as kept, rule 61).
        #
        # "Undecided" is no hand override at all, which is what ``condemned.effective_verdict``
        # -- the queue's classifier and the one this label must agree with -- leaves at its scan
        # verdict. Every other case it answers itself: a spare keeps the file, a reap the engine
        # WILL honor took the branch above, and a reap it will NOT honor (streaming right now, a
        # file no *arr manages, a bad Plex match, an unreadable explanation) also keeps it. That
        # last one is what this branch used to miss: it asked "not spared" rather than "not
        # decided", so a held reap read as "Left to decide" beside a file Reaper had settled on
        # keeping, and the row's jump opened the one lane the title is not in.
        if eff_condemned:
            verdict = _CONDEMN
            recl_items += 1
            recl_bytes += sum(c.size_bytes or 0 for c in eff_condemned)
        elif any(c.verdict == "abstain" and c.override is None for c in scoped):
            verdict = "abstain"
        else:
            verdict = "protect"

        display = scoped[0].group_title or scoped[0].title
        year = next((c.year for c in scoped if c.year), None)
        # The poster comes from Plex, proxied by our own image route -- the show's key for a
        # season (many have no poster of their own), the item's own key otherwise.
        poster_key = scoped[0].poster_rating_key or scoped[0].plex_rating_key
        poster_url = f"/api/poster/{poster_key}" if poster_key else None
        if len(scoped) == 1:
            item_id: int | None = scoped[0].candidate_id
            group_key: str | None = None
        else:
            item_id = None
            group_key = next((c.group_key for c in eff_condemned if c.group_key), None) or next(
                (c.group_key for c in scoped if c.group_key), None
            )

        titles.append(
            PersonTitle(
                title=display,
                year=year,
                media_type=scoped[0].media_type,
                is_4k=mine.is_4k,
                size_bytes=title_size,
                requested_at=mine.requested_at,
                available_at=mine.available_at,
                watched_by_them=watched_shown,
                verdict=verdict,
                item_id=item_id,
                group_key=group_key,
                # Distinct co-requesters, by cross-portal identity so two people who share a
                # name (or a Seerr id across portals) stay apart; the target's own are excluded.
                co_requesters=tuple(sorted({_name(r) for r in group if _identity(r) != identity})),
                poster_url=poster_url,
            )
        )

    if not matched_any:
        return None

    # Reclaimable first (most actionable), then abstain, then kept; heaviest first inside each.
    order = {"condemn": 0, "abstain": 1, "protect": 2}
    titles.sort(key=lambda t: (order.get(t.verdict, 3), -(t.size_bytes or 0)))

    # This person's not-in-scan requests, from the one shared classifier and named for the
    # panel -- the same list the board shows, scoped to them.
    unmatched = _collect_unmatched(requests, candidates, snapshot_at, identity=identity)
    await _enrich_titles(seerrs, unmatched)
    not_in_scan = sum(u.request_count for u in unmatched)

    accounts = await _enrich_accounts(seerrs, {plex_id})
    quota = accounts.get(plex_id) if plex_id is not None else None

    # A link to this person on the portal they requested through. Built from any one of their
    # requests (all carry their user id and portal), so it does not depend on the best-effort
    # account read above and survives an unreadable user list.
    own_request = next((r for r in requests if _identity(r) == identity), None)
    profile_url = _profile_url(seerrs, own_request) if own_request is not None else None

    return PersonDetail(
        plex_id=plex_id,
        name=name,
        seerr_total=quota.seerr_total if quota else None,
        requests_in_scan=len(titles),
        gb_granted_bytes=granted,
        played_by_them=played,
        reclaimable_items=recl_items,
        reclaimable_bytes=recl_bytes,
        quota=quota,
        titles=titles,
        not_in_scan=not_in_scan,
        unmatched=unmatched,
        profile_url=profile_url,
        # The span every watch figure above was counted over, carried so the drawer can bound
        # them exactly as the board does. Read here rather than derived from the numbers: a
        # zero cannot tell "nobody watched it" from "their plays are behind the horizon".
        horizon_at=await history_sync.horizon(cache_engine),
    )
