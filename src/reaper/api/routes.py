# SPDX-License-Identifier: AGPL-3.0-or-later
"""The REST surface.

Read-only. Nothing here can delete anything: there is no execution route, and the
``GuardedTransport`` would refuse the call even if there were.

Two routes carry most of the product:

``GET  /api/candidates/{id}``  -- the why-panel, including the protections that were
                                 checked and did *not* fire, with their actual numbers.
``POST /api/policy/simulate``  -- re-scores the last snapshot under a candidate policy
                                 with **zero API calls**, so the owner can move a
                                 threshold and watch the blast radius move with it.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import asc, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement

from reaper.api.schemas import (
    AboutOut,
    CandidateDetail,
    CandidateOut,
    ChipOut,
    ConditionIn,
    Explanation,
    FieldOut,
    FieldValuesOut,
    GateCountOut,
    GateSettingIn,
    GroupOut,
    GroupSeasonMarkOut,
    LinksOut,
    PolicyIn,
    PolicyOut,
    PolicyWarningOut,
    RatingsOut,
    SeasonShapeOut,
    SignalSettingIn,
    SimExampleOut,
    SimulationOut,
    SnapshotOut,
    VocabularyOut,
)
from reaper.buildinfo import build_version
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.models import Candidate, FirstFlagged, Instance, InstanceKind, PlexServer, Snapshot
from reaper.db.models import Policy as PolicyModel
from reaper.engine import facts_codec
from reaper.engine.fields import Lane, vocabulary
from reaper.engine.gates import PROTECT, Evaluation, GateId, GateResult, evaluate_all
from reaper.engine.policy import (
    ConditionSpec,
    GateSetting,
    PolicyBody,
    ProfileSettings,
    SignalSetting,
    combine_hashes,
    inspect,
)
from reaper.engine.signals import SignalConfig
from reaper.engine.signals import score as score_facts
from reaper.engine.verdict import STRUCTURAL_GATES, decide_verdict
from reaper.services import app_settings, whitelist
from reaper.services.condemned import reap_is_effective, reap_override_verdict
from reaper.services.deep_links import build_links
from reaper.services.display_meta import parse_ratings_json
from reaper.services.planner import MediaRef, PlanError
from reaper.services.profiles import active_policy, active_policy_row, active_profile_settings
from reaper.services.snapshot import HAND_SPARE_DETAIL

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api")


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


async def _latest_snapshot(session: AsyncSession) -> Snapshot | None:
    return (
        await session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1))
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Snapshots and candidates
# ---------------------------------------------------------------------------


@router.get("/snapshots/latest")
async def latest_snapshot(request: Request) -> SnapshotOut:
    async with _sessions(request)() as session:
        snapshot = await _latest_snapshot(session)
        if snapshot is None:
            raise HTTPException(404, "No scan has run yet.")
        return await _snapshot_out(session, snapshot)


async def _snapshot_out(session: AsyncSession, snapshot: Snapshot) -> SnapshotOut:
    counts: dict[str, int] = {
        str(verdict): int(n)
        for verdict, n in (
            await session.execute(
                select(Candidate.verdict, func.count())
                .where(Candidate.snapshot_id == snapshot.id)
                .group_by(Candidate.verdict)
            )
        ).all()
    }
    # SUM silently skips NULL rows and COALESCE cannot tell that from a genuine zero, so
    # the count of unmeasured rows is taken in the SAME query over the SAME conditions.
    # Without it this number would go from honestly incomplete to silently wrong.
    reclaimable, unknown_size = (
        await session.execute(
            select(
                func.coalesce(func.sum(Candidate.size_bytes), 0),
                func.count().filter(Candidate.size_bytes.is_(None)),
            ).where(Candidate.snapshot_id == snapshot.id, Candidate.verdict == "condemn")
        )
    ).one()

    return SnapshotOut(
        id=snapshot.id,
        created_at=snapshot.created_at.isoformat(),
        policy_hash=snapshot.policy_hash,
        horizon_at=snapshot.horizon_at.isoformat(),
        item_count=snapshot.item_count,
        degraded=snapshot.degraded,
        degraded_reason=snapshot.degraded_reason,
        condemned=int(counts.get("condemn", 0)),
        protected=int(counts.get("protect", 0)),
        abstained=int(counts.get("abstain", 0)),
        reclaimable_bytes=int(reclaimable),
        unknown_size_items=int(unknown_size),
    )


@router.get("/snapshot/season-shape")
async def season_shape(request: Request) -> SeasonShapeOut:
    """The distribution of content-season counts across shows, for the keep-last advisory.

    A show's season count is how many season candidate rows it has in the latest snapshot.
    The editor uses this to compute, entirely client-side, how many shows have no season
    that a given keep-last-N value would leave removable -- live as the number changes,
    with no scan and no dependency on the current keep-last value.
    """
    async with _sessions(request)() as session:
        snapshot = await _latest_snapshot(session)
        if snapshot is None:
            return SeasonShapeOut(total_shows=0, season_counts={})
        rows = (
            await session.execute(
                select(Candidate.group_key, func.count())
                .where(
                    Candidate.snapshot_id == snapshot.id,
                    Candidate.media_type == "season",
                    Candidate.group_key.is_not(None),
                )
                .group_by(Candidate.group_key)
            )
        ).all()
    counts: dict[int, int] = {}
    for _group, n in rows:
        counts[int(n)] = counts.get(int(n), 0) + 1
    return SeasonShapeOut(total_shows=len(rows), season_counts=counts)


@router.get("/candidates")
async def list_candidates(
    request: Request,
    response: Response,
    verdict: str = "condemn",
    search: str | None = None,
    media_type: str | None = None,
    requested: str = "any",
    genre: str | None = None,
    library: str | None = None,
    override: str = "any",
    sort: str = "score",
    order: str = "desc",
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[CandidateOut]:
    """One page of the review queue.

    The list is **paged** -- a library runs to thousands of protected titles, and returning
    them in one payload was hiding the tail: the client fetches ``limit`` rows at ``offset``
    and asks for the next page as it scrolls. The full size of the filtered set (a count and a
    byte total, both *before* the page window) is returned in the ``X-Total-Count`` and
    ``X-Total-Bytes`` response headers, so the queue can show "[redacted] items · [redacted]" without
    having loaded them all.

    Default order is by score, then by size -- so the biggest wins among the safest
    deletions come first. Size ranks the candidates the score has already chosen; it never
    decides an item's fate (see docs/SIGNALS.md). ``sort`` (score / size / year / title) and
    ``order`` (asc / desc) let the owner re-rank; a score tiebreak keeps the order stable
    within equal keys, so a show's seasons never scatter across a page boundary.

    Filters **stack** (they are ANDed), and each only narrows the frozen snapshot, never
    re-decides it: ``search`` matches the title or the show name, ``media_type`` keeps
    movies or seasons, ``requested`` keeps only what someone asked for through Seerr
    (``yes``), only what nobody asked for (``no``), or everything (``any``), ``genre``
    keeps rows whose stored genre list contains the given term exactly, ``library`` keeps
    rows in the named Plex library (section), and ``override`` keeps rows by their
    hand-override state (``spare`` / ``reap`` / ``none`` / ``any``).
    """
    async with _sessions(request)() as session:
        snapshot = await _latest_snapshot(session)
        if snapshot is None:
            response.headers["X-Total-Count"] = "0"
            response.headers["X-Total-Bytes"] = "0"
            return []

        # The filters, built once and applied to BOTH the count and the page, so the header
        # totals describe exactly the set the rows are drawn from.
        conditions = [
            Candidate.snapshot_id == snapshot.id,
            Candidate.verdict == verdict,
        ]
        if search and search.strip():
            pattern = f"%{search.strip()}%"
            conditions.append(
                or_(Candidate.title.ilike(pattern), Candidate.group_title.ilike(pattern))
            )
        if media_type:
            conditions.append(Candidate.media_type == media_type)
        if library and library.strip():
            # Exact match on the stored library title (what the operator named the section).
            conditions.append(Candidate.library_title == library.strip())
        if requested == "yes":
            conditions.append(Candidate.requested_by.is_not(None))
        elif requested == "no":
            conditions.append(Candidate.requested_by.is_(None))
        if genre and genre.strip():
            # Exact term match inside the stored JSON genre array. json_each raises
            # mid-query on a malformed document, so invalid or missing rows are swapped
            # for an empty array inside the expression itself and simply never match.
            # Raw SQL (the season scan's precedent for json/table-valued reads), cast to
            # the boolean element type the conditions list carries.
            genre_predicate = text(
                "EXISTS (SELECT 1 FROM json_each("
                "CASE WHEN candidate.genres_json IS NOT NULL "
                "AND json_valid(candidate.genres_json) "
                "THEN candidate.genres_json ELSE '[]' END"
                ") WHERE json_each.value = :genre)"
            ).bindparams(genre=genre.strip())
            conditions.append(cast("ColumnElement[bool]", genre_predicate))
        if override in {"spare", "reap", "none"}:
            # Hand overrides resolve in Python: whitelist.effective_override is the one
            # decision function (an item's own key beats its show's), and re-stating that
            # precedence in SQL is how the copies would drift. So the candidate keys under
            # the conditions built so far are resolved through the real function, and the
            # filter becomes an IN over the ones in the asked-for state. Totals below use
            # the same final conditions, so count, bytes and page describe one set.
            decisions = await whitelist.overrides(session)
            keys = (
                (await session.execute(select(Candidate.media_key).where(*conditions)))
                .scalars()
                .all()
            )
            wanted = [
                key
                for key in keys
                if (whitelist.effective_override(key, decisions) or "none") == override
            ]
            conditions.append(Candidate.media_key.in_(wanted))

        # The byte total is a SUM, which skips NULL rows without saying so, and COALESCE
        # cannot tell that from a real zero. So the unmeasured count is taken in the same
        # query under the same conditions, and the header carries it beside the total.
        totals = (
            await session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(Candidate.size_bytes), 0),
                    func.count().filter(Candidate.size_bytes.is_(None)),
                ).where(*conditions)
            )
        ).one()
        response.headers["X-Total-Count"] = str(int(totals[0]))
        response.headers["X-Total-Bytes"] = str(int(totals[1]))
        response.headers["X-Unknown-Size-Count"] = str(int(totals[2]))

        direction = asc if order == "asc" else desc
        sort_columns = {
            "score": Candidate.score,
            "size": Candidate.size_bytes,
            "year": Candidate.year,
            "title": func.lower(func.coalesce(Candidate.group_title, Candidate.title)),
        }
        primary = direction(sort_columns.get(sort, Candidate.score))
        # Sorting BY size puts the unmeasured items last in both directions. SQLite orders
        # NULL first on ASC, so "smallest first" would otherwise open the list with every
        # item Reaper could not measure -- the ones it has the least to say about. Written
        # as an explicit is-null key rather than nullslast(), which not every backend
        # renders the same way. Only when size is the chosen key: on a title or year sort
        # the owner asked for alphabetical or chronological, not for a size grouping.
        sort_keys = [primary] if sort != "size" else [Candidate.size_bytes.is_(None), primary]
        # A score/size tiebreak after the chosen key keeps ordering deterministic -- so a
        # show's seasons stay adjacent and paging never splits or shuffles the list.
        stmt = (
            select(Candidate)
            .where(*conditions)
            .order_by(*sort_keys, Candidate.score.desc(), Candidate.size_bytes.desc())
            # limit/offset are validated at the boundary (Query ge/le above), so a
            # negative limit -- which SQLite reads as "no limit" -- can never reach here.
            .limit(limit)
            .offset(offset)
        )

        rows = (await session.execute(stmt)).scalars().all()

        flagged = {
            f.media_key: f.first_flagged_at
            for f in (
                await session.execute(
                    select(FirstFlagged).where(
                        FirstFlagged.media_key.in_([r.media_key for r in rows] or [""])
                    )
                )
            )
            .scalars()
            .all()
        }
        decisions = await whitelist.overrides(session)
        group_totals, group_marks = await _group_rollups(
            session, snapshot.id, {r.group_key for r in rows if r.group_key}, decisions
        )

        return [
            _candidate_out(
                r,
                flagged.get(r.media_key),
                decisions,
                group_condemned=group_totals.get(r.group_key) if r.group_key else None,
                group_seasons=group_marks.get(r.group_key) if r.group_key else None,
            )
            for r in rows
        ]


def _add_member(total: tuple[int, int, int], size_bytes: int | None) -> tuple[int, int, int]:
    """Fold one actable season into a show's (count, bytes, unknown) rollup.

    A season with no size raises the unknown count and nothing else. It is deliberately
    NOT counted as an item, because the planner will not plan it: the count beside "Reap
    now" has to be the number of steps the server would emit.
    """
    count, total_bytes, unknown = total
    if size_bytes is None:
        return (count, total_bytes, unknown + 1)
    return (count + 1, total_bytes + size_bytes, unknown)


async def _group_rollups(
    session: AsyncSession,
    snapshot_id: int,
    group_keys: set[str],
    decisions: dict[str, str],
) -> tuple[dict[str, tuple[int, int, int]], dict[str, list[GroupSeasonMarkOut]]]:
    """Two per-show rollups from one sweep of each group's member rows.

    **Totals** -- what "Reap now" on each show group would actually plan:
    (count, bytes, unknown) over its ACTABLE member seasons across the WHOLE snapshot:
    condemned minus hand-spares, plus hand-reaped seasons whose reap the engine honors
    (services.condemned). The show card's numbers must match the planner's expansion --
    ``build_plan`` expands a group key over the same effective set -- so the number
    beside a destructive button is derived from the set the server will act on
    (rule 30). Never derived from the fetched page, which on a long sorted list can
    hold only some of a show's seasons (B-13).

    The COUNT is filtered too, not only the bytes. A season with no size is held back by
    the planner, so counting it here would put "Reap now (8 items)" beside a plan that
    emits six. It is reported separately as ``unknown`` so the card can say what it is
    leaving out instead of quietly shrinking.

    **Marks** -- the show card's season strip: every member's (season, verdict,
    override, and whether a hand reap actually takes) across all lanes, sorted by
    season number (unnumbered rows last).

    ``IN`` chunked at 500 per the bound-variable limit.
    """
    if not group_keys:
        return {}, {}
    totals: dict[str, tuple[int, int, int]] = dict.fromkeys(group_keys, (0, 0, 0))
    marks: dict[str, list[GroupSeasonMarkOut]] = {key: [] for key in group_keys}
    # Hand-reaped members whose row is not scan-condemned: whether the reap takes needs
    # the frozen explanation, fetched in one targeted pass below rather than dragging
    # every member's JSON through the rollup query. media_key -> (mark, group, bytes).
    pending: dict[str, tuple[GroupSeasonMarkOut, str, int | None]] = {}
    keys = sorted(group_keys)
    for start in range(0, len(keys), 500):
        chunk = keys[start : start + 500]
        members = (
            await session.execute(
                select(
                    Candidate.id,
                    Candidate.media_key,
                    Candidate.group_key,
                    Candidate.size_bytes,
                    Candidate.verdict,
                ).where(
                    Candidate.snapshot_id == snapshot_id,
                    Candidate.group_key.in_(chunk),
                )
            )
        ).all()
        for candidate_id, media_key, group_key, size_bytes, verdict in members:
            override = whitelist.effective_override(media_key, decisions)
            mark = GroupSeasonMarkOut(
                id=int(candidate_id),
                season=_season_number(media_key),
                verdict=str(verdict),
                override=override,
                override_effective=(True if override == "reap" and verdict == "condemn" else None),
                size_bytes=size_bytes,
            )
            marks[group_key].append(mark)
            if override == "reap" and verdict != "condemn":
                pending[media_key] = (mark, group_key, size_bytes)
            if verdict == "condemn" and override != "spare":
                totals[group_key] = _add_member(totals[group_key], size_bytes)

    pending_keys = sorted(pending)
    for start in range(0, len(pending_keys), 500):
        chunk = pending_keys[start : start + 500]
        rows = (
            await session.execute(
                select(Candidate.media_key, Candidate.score, Candidate.explanation_json).where(
                    Candidate.snapshot_id == snapshot_id, Candidate.media_key.in_(chunk)
                )
            )
        ).all()
        for media_key, score, explanation_json in rows:
            mark, group_key, size_bytes = pending[media_key]
            effective = reap_override_verdict(explanation_json, score=int(score)) == "condemn"
            mark.override_effective = effective
            if effective:
                totals[group_key] = _add_member(totals[group_key], size_bytes)

    for members_marks in marks.values():
        members_marks.sort(
            key=lambda m: (m.season is None, m.season if m.season is not None else 0)
        )
    return totals, marks


def _primary_reason(explanation_json: str, verdict: str) -> str | None:
    """The single line the card shows: *why* Reaper judged this, not what it is about.

    A spared item leads with the protection that saved it; a reaped one with its strongest
    reason; an abstained one with what stopped it short. All of these are already plain
    English in the stored explanation -- this only picks which one to surface.
    """
    try:
        exp = json.loads(explanation_json)
    except (ValueError, TypeError):
        return None

    if verdict == "protect":
        fired = exp.get("protections_fired") or []
        return fired[0]["detail"] if fired else None
    if verdict == "condemn":
        signals = [s for s in exp.get("signals") or [] if s.get("evaluated")]
        signals.sort(key=lambda s: s.get("contribution", 0), reverse=True)
        return signals[0]["detail"] if signals else None
    # abstain: lead with the match problem when there is one -- it is the single cause
    # behind every "could not check" that follows, and the raw gate detail ("could not
    # check the watch horizon: ...") repeats it in engineer-speak. Otherwise fall back to
    # the first unchecked protection, whose detail is already a plain sentence.
    status = (exp.get("match") or {}).get("status")
    if status == "unmatched":
        return "Kept to be safe: it couldn't be found in Plex."
    if status == "ambiguous":
        return "Kept to be safe: it looks like more than one thing in Plex."
    unknown = exp.get("protections_unknown") or []
    if unknown:
        return str(unknown[0]["detail"])
    return "Scored below your threshold."


def _dormant_for(explanation_json: str) -> str | None:
    """The humanized dormancy span ("5 years, 9 months") for the card's amber pill.

    Read from the stored explanation's UNWATCHED signal, whose detail has exactly one
    producer (engine/signals.py): ``not watched in {span}``. Anything else -- the
    signal unevaluated, an older snapshot's different phrasing, a missing block --
    degrades to ``None`` and the pill is hidden. Same defensive posture as
    ``_primary_reason``: display extraction must never error a row off the queue.
    """
    prefix = "not watched in "
    try:
        exp = json.loads(explanation_json)
    except (ValueError, TypeError):
        return None
    for signal in exp.get("signals") or []:
        if not isinstance(signal, dict) or signal.get("id") != "unwatched":
            continue
        detail = signal.get("detail")
        if signal.get("evaluated") and isinstance(detail, str) and detail.startswith(prefix):
            return detail[len(prefix) :] or None
        return None
    return None


#: Parsers over our own gates' closed detail vocabularies (engine/gates.py,
#: services/season_pruning.py) -- the WhyPanel's CHECK_COPY/CAUSE_COPY precedent.
#: Anything unrecognized falls back to a static phrase, never an error.
_RATED_RE = re.compile(r"^well rated: (\d+(?:\.\d+)?) on IMDb")
_WATCHED_HERE_RE = re.compile(r"^watched here: (\d+) (?:person|people) in the last (.+)$")
_OTHERS_RE = re.compile(r"^(\d+) other")
_KEEP_LAST_RE = re.compile(r"^within the last (\d+) seasons")


def _kept_season_phrase(detail: str) -> str:
    """The chip phrase for a fired season keep rule (season_pruning's closed reasons)."""
    if detail.startswith("specials"):
        return "specials are never removed"
    if detail.startswith("Sonarr is still downloading"):
        return "still downloading"
    if detail == "currently airing":
        return "currently airing"
    if detail.startswith("the first season"):
        return "the first season stays"
    if keep_last := _KEEP_LAST_RE.match(detail):
        return f"in the last {keep_last.group(1)} seasons you keep"
    if detail.startswith("this show has only"):
        return "your keep rule keeps all its seasons"
    if detail.startswith("a viewer is part-way"):
        return "someone is partway through"
    return "your season rule keeps it"


def _kept_phrase(gate: str, detail: str) -> str:
    """The green chip's phrase for the protection that fired, worn as "Kept · {phrase}"."""
    if detail == HAND_SPARE_DETAIL:
        return "you spared it"
    if gate == "whitelisted":
        return "on your keep list"
    if gate == "streaming_now":
        return "playing right now"
    if gate == "rating_floor":
        rated = _RATED_RE.match(detail)
        return f"well rated: {rated.group(1)} on IMDb" if rated else "well rated"
    if gate == "server_popularity":
        watched = _WATCHED_HERE_RE.match(detail)
        if watched:
            count, window = int(watched.group(1)), watched.group(2)
            people = "person" if count == 1 else "people"
            return f"{count} {people} watched it in the last {window}"
        return "people here still watch it"
    if gate == "others_watching":
        others = _OTHERS_RE.match(detail)
        if others:
            count = int(others.group(1))
            return (
                "someone else is watching it" if count == 1 else f"{count} others are watching it"
            )
        return "others are watching it"
    if gate == "curated_list":
        return "on a protected list"
    if gate == "min_dormancy":
        if detail.startswith("no watch history"):
            return "no watch history, kept to be safe"
        return "watched too recently"
    if gate == "unmanaged":
        return "not managed by Sonarr or Radarr"
    if gate == "season_progression":
        return _kept_season_phrase(detail)
    if gate == "custom":
        return "by your rule"
    return "a protection applies"


def _chip(explanation_json: str, verdict: str, score: int) -> ChipOut | None:
    """The card's one short status chip -- Sanctuary and Limbo lanes only.

    Follows decide_verdict's own precedence (match trouble, then a deliberate
    left-for-you flag, then checks that couldn't run, then the coverage floor, then
    the score) so the chip names the fact that actually put the item in its lane.
    Pure display extraction from the stored explanation: never a re-decision, and
    never an error that drops a row off the queue. Condemned rows get no chip here;
    their card leads with the amber dormancy pill (``dormant_for``).
    """
    try:
        exp = json.loads(explanation_json)
    except (ValueError, TypeError):
        return None

    if verdict == "protect":
        fired = exp.get("protections_fired") or []
        first = fired[0] if fired and isinstance(fired[0], dict) else None
        if first is None:
            return None
        gate = str(first.get("gate") or "")
        detail = str(first.get("detail") or "")
        return ChipOut(tone="kept", text=f"Kept · {_kept_phrase(gate, detail)}")

    if verdict != "abstain":
        return None

    status = (exp.get("match") or {}).get("status")
    if status == "unmatched":
        return ChipOut(tone="quiet", text="Couldn't be found in Plex")
    if status == "ambiguous":
        return ChipOut(tone="quiet", text="Looks like two different things in Plex")

    unknown = [e for e in exp.get("protections_unknown") or [] if isinstance(e, dict)]
    for entry in unknown:
        detail = str(entry.get("detail") or "")
        if detail and not detail.startswith("could not check"):
            # A deliberate "decide this yourself" flag -- today, the season keep-rule
            # conflict -- not a plumbing failure. The one blocked case that wants eyes.
            if str(entry.get("gate") or "") == "season_progression":
                return ChipOut(
                    tone="look",
                    text="Needs a look · watched more than a season your rule keeps",
                )
            return ChipOut(tone="look", text="Needs a look · left for you to decide")
    if unknown:
        return ChipOut(tone="quiet", text="Some checks couldn't run")

    threshold = exp.get("threshold")
    if isinstance(threshold, int):
        if score >= threshold:
            # decide_verdict's order: past the blocked cases, an abstain at or above
            # the threshold can only be the coverage floor.
            return ChipOut(tone="quiet", text="Too little of it could be checked")
        return ChipOut(tone="quiet", text=f"Scored {score}, under your {threshold}")
    return ChipOut(tone="quiet", text="Below your threshold")


def _season_number(media_key: str) -> int | None:
    """The season a key addresses, or None (a movie, or a key that does not parse --
    display extraction never errors a row off the queue)."""
    try:
        return MediaRef.parse(media_key).season
    except PlanError:
        return None


def _candidate_out(
    r: Candidate,
    flagged_at: datetime | None = None,
    decisions: dict[str, str] | None = None,
    *,
    group_condemned: tuple[int, int, int] | None = None,
    group_seasons: list[GroupSeasonMarkOut] | None = None,
) -> CandidateOut:
    # Three views of the one whitelist: the decision in EFFECT (own, or inherited from the
    # show) colors the row; the item's OWN decision is what a control on this row can toggle;
    # the SHOW's decision is what still keeps a season the operator did not touch. Computed in
    # one place so a season row and its show card can never disagree about what is spared.
    decisions = decisions or {}
    override = whitelist.effective_override(r.media_key, decisions)
    override_own = decisions.get(r.media_key)
    _show_key = whitelist.show_key(r.media_key)
    show_override = decisions.get(_show_key) if _show_key else None
    return CandidateOut(
        id=r.id,
        media_key=r.media_key,
        title=r.title,
        media_type=r.media_type,
        size_bytes=r.size_bytes,
        verdict=r.verdict,
        score=r.score,
        coverage_bp=r.coverage_bp,
        first_flagged_at=flagged_at.isoformat() if flagged_at else None,
        year=r.year,
        summary=r.summary,
        # The poster comes from Plex, proxied by our own image route (see api/poster.py) --
        # never the *arr's stale art. For a season this is the SHOW's key (poster_rating_key),
        # since many seasons have no poster of their own; a movie falls back to its own key.
        poster_url=(
            f"/api/poster/{r.poster_rating_key or r.plex_rating_key}"
            if (r.poster_rating_key or r.plex_rating_key)
            else None
        ),
        requested_by=r.requested_by,
        group_key=r.group_key,
        group_title=r.group_title,
        group_condemned_count=group_condemned[0] if group_condemned is not None else None,
        group_condemned_bytes=group_condemned[1] if group_condemned is not None else None,
        group_unknown_size=group_condemned[2] if group_condemned is not None else None,
        video_resolution=r.video_resolution,
        library=r.library_title,
        dormant_for=_dormant_for(r.explanation_json),
        reason=_primary_reason(r.explanation_json, r.verdict),
        spared=override == "spare",
        override=override,
        override_own=override_own,
        show_override=show_override,
        # Whether a hand reap actually takes: decide_verdict honors it past cautious
        # protections but never past a safety stop or an unchecked protection. The UI
        # colors the row red only when this is True, so it never promises a removal
        # the engine will refuse.
        override_effective=reap_is_effective(r) if override == "reap" else None,
        chip=_chip(r.explanation_json, r.verdict, r.score),
        season_number=_season_number(r.media_key),
        group_seasons=group_seasons,
        show_status=r.show_status,
    )


async def _deep_links(session: AsyncSession, row: Candidate) -> LinksOut:
    """The panel's jump links, from coordinates frozen on the row plus the live
    instance/server configuration. Every lookup failure degrades that one link to
    ``None`` -- an unroutable key, a removed instance or an unlinked Plex must never
    404 the why-panel."""
    arr_base: str | None = None
    try:
        ref = MediaRef.parse(row.media_key)
    except PlanError:
        ref = None
    if ref is not None:
        # THE instance the key routes to -- by id and kind, never "the first Radarr".
        instance = await session.get(Instance, ref.instance_id)
        if instance is not None and instance.kind == ref.kind and instance.enabled:
            arr_base = instance.base_url

    async def first_enabled(kind: InstanceKind) -> Instance | None:
        # Ordered by id so "the first enabled" is deterministic: the longest-standing
        # instance, not whichever row the query planner happened to return.
        return (
            (
                await session.execute(
                    select(Instance)
                    .where(Instance.kind == kind, Instance.enabled.is_(True))
                    .order_by(Instance.id)
                )
            )
            .scalars()
            .first()
        )

    tautulli = await first_enabled(InstanceKind.TAUTULLI)
    seerr = await first_enabled(InstanceKind.SEERR)
    plex_server = (await session.execute(select(PlexServer))).scalars().first()

    links = build_links(
        row.media_key,
        plex_rating_key=row.plex_rating_key,
        tmdb_id=row.tmdb_id,
        title_slug=row.title_slug,
        arr_base_url=arr_base,
        tautulli_base_url=tautulli.base_url if tautulli else None,
        machine_identifier=plex_server.machine_identifier if plex_server else None,
        plex_web_url=await app_settings.get_plex_web_url(session),
        seerr_base_url=seerr.base_url if seerr else None,
        imdb_id=row.imdb_id,
        media_type=row.media_type,
        # A season row searches by its SHOW's title ("Example Show", not
        # "Example Show · Season 3") -- that is the page the rating describes.
        title=row.group_title or row.title,
    )
    return LinksOut(
        plex=links.plex,
        tautulli=links.tautulli,
        seerr=links.seerr,
        radarr=links.radarr,
        sonarr=links.sonarr,
        imdb=links.imdb,
        tmdb=links.tmdb,
        rotten_tomatoes=links.rotten_tomatoes,
        trakt=links.trakt,
    )


def _ratings_out(ratings_json: str | None) -> RatingsOut | None:
    """The stored int map, decoded for the wire: IMDb back to its 0-10 float, the
    percentage sources as 0-100 ints. ``None`` (row absent or empty) hides the row."""
    stored = parse_ratings_json(ratings_json)
    if not stored:
        return None
    imdb_tenths = stored.get("imdb")
    return RatingsOut(
        imdb=imdb_tenths / 10 if imdb_tenths is not None else None,
        imdb_votes=stored.get("imdb_votes"),
        rt_critic=stored.get("rotten_tomatoes_critic"),
        rt_audience=stored.get("rotten_tomatoes_audience"),
        tmdb=stored.get("tmdb"),
        trakt=stored.get("trakt"),
    )


def _genres(genres_json: str | None) -> list[str]:
    """The stored genre list, defensively: anything unexpected is an empty list."""
    if not genres_json:
        return []
    try:
        raw = json.loads(genres_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    return [str(g) for g in raw if g]


@router.get("/candidates/{candidate_id}")
async def candidate_detail(request: Request, candidate_id: int) -> CandidateDetail:
    """The why-panel.

    Renders for PROTECTED items too, showing the score it is overriding. A tool that
    only explains its deletions cannot be trusted about its keeps.
    """
    async with _sessions(request)() as session:
        row = await session.get(Candidate, candidate_id)
        if row is None:
            raise HTTPException(404, "No such candidate.")

        flagged = await session.get(FirstFlagged, row.media_key)
        decisions = await whitelist.overrides(session)

        base = _candidate_out(
            row,
            flagged.first_flagged_at if flagged else None,
            decisions,
        )
        return CandidateDetail(
            **base.model_dump(),
            explanation=Explanation(**json.loads(row.explanation_json)),
            links=await _deep_links(session, row),
            ratings=_ratings_out(row.ratings_json),
            content_rating=row.content_rating,
            runtime_minutes=row.runtime_minutes,
            genres=_genres(row.genres_json),
        )


@router.get("/groups/{group_key}")
async def group_detail(request: Request, group_key: str) -> GroupOut:
    """One show, whole: every season row in the latest snapshot, across all lanes.

    Each queue tab lists only its own lane, so this is where "which seasons stay and
    which go" is answered in one place -- the show info panel and the expanded show
    card both read it. Frozen candidate rows only; nothing here re-decides a verdict.
    """
    async with _sessions(request)() as session:
        snapshot = await _latest_snapshot(session)
        if snapshot is None:
            raise HTTPException(404, "No scan has run yet.")
        rows = (
            (
                await session.execute(
                    select(Candidate).where(
                        Candidate.snapshot_id == snapshot.id,
                        Candidate.group_key == group_key,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            raise HTTPException(404, "That show is not in the latest scan.")

        flagged = {
            f.media_key: f.first_flagged_at
            for f in (
                await session.execute(
                    select(FirstFlagged).where(
                        FirstFlagged.media_key.in_([r.media_key for r in rows])
                    )
                )
            )
            .scalars()
            .all()
        }
        decisions = await whitelist.overrides(session)

        seasons = [
            _candidate_out(
                r,
                flagged.get(r.media_key),
                decisions,
            )
            for r in rows
        ]
        seasons.sort(key=lambda c: (c.season_number is None, c.season_number or 0))

        # The show-level status line: a season deliberately left for the owner wins
        # (that is the line that wants eyes), else the highest-scoring season -- the
        # same member the collapsed card leads with.
        lead = next(
            (s for s in seasons if s.chip is not None and s.chip.tone == "look"),
            max(seasons, key=lambda c: c.score),
        )
        lead_row = next(r for r in rows if r.id == lead.id)

        return GroupOut(
            group_key=group_key,
            title=next((r.group_title for r in rows if r.group_title), rows[0].title),
            year=min((c.year for c in seasons if c.year), default=None),
            poster_url=lead.poster_url,
            summary=next((r.summary for r in rows if r.summary), None),
            size_bytes=sum(c.size_bytes for c in seasons if c.size_bytes is not None),
            unknown_size_seasons=sum(1 for c in seasons if c.size_bytes is None),
            reason=lead.reason,
            # A show-level fact: every season shares the show's library, so the first row
            # that carries one answers for the whole show (None if none do).
            library=next((r.library_title for r in rows if r.library_title), None),
            chip=lead.chip,
            # The show's own decision (the show key), which the panel's whole-show control
            # toggles. Read straight from the whitelist, never rolled up from the seasons'
            # own marks -- the control clears only this key, so lighting it from an aggregate
            # it cannot clear is the very bug this replaced.
            show_override=decisions.get(group_key),
            links=await _deep_links(session, lead_row),
            # A show-level fact, so any season carrying it answers for the whole show:
            # one reading of the series is stamped onto every one of its seasons in the
            # same scan. Skipping the rows that carry nothing keeps a snapshot taken
            # before this field existed from blanking a group whose other rows have it.
            show_status=next((c.show_status for c in seasons if c.show_status), None),
            seasons=seasons,
        )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _to_body(payload: PolicyIn) -> PolicyBody:
    """Build the domain policy, translating its refusals into a 422.

    The wire schema deliberately does NOT re-implement the domain rules -- a vote floor
    of 0, a dormancy floor under a year, a run cap above the rolling cap. Those live in
    ``engine.policy``, where they are enforced for every caller including the CLI and
    the scheduler.

    But a domain ``ValidationError`` raised inside a route is a **500**, and the owner
    would see "Internal Server Error" instead of "a vote floor of 0 makes the rating
    floor meaningless -- it would protect an 8.3 drawn from 388 votes". So it is caught
    and re-raised with the reason intact.
    """
    try:
        return PolicyBody(
            media_type=payload.media_type,
            condemn_at=payload.condemn_at,
            coverage_floor_bp=payload.coverage_floor_bp,
            keep_last_seasons=payload.keep_last_seasons,
            keep_first_season=payload.keep_first_season,
            keep_last_scope=payload.keep_last_scope,
            season_lookahead=payload.season_lookahead,
            keep_in_progress=payload.keep_in_progress,
            in_progress_hold_days=payload.in_progress_hold_days,
            keep_specials=payload.keep_specials,
            flag_keep_conflicts=payload.flag_keep_conflicts,
            gates=tuple(
                GateSetting(
                    gate=g.gate,
                    enabled=g.enabled,
                    threshold=g.threshold,
                    secondary=g.secondary,
                    window_days=g.window_days,
                )
                for g in payload.gates
            ),
            signals=tuple(
                SignalSetting(
                    signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor
                )
                for s in payload.signals
            ),
            protect_conditions=tuple(
                ConditionSpec(field=c.field, op=c.op, value=c.value)
                for c in payload.protect_conditions
            ),
            # Already engine specs (BooleanCondemnSpec / GradedCondemnSpec) -- passed through.
            custom_condemn=tuple(payload.custom_condemn),
            graded_keeps=tuple(payload.graded_keeps),
            keep_tags=tuple(t.strip() for t in payload.keep_tags if t.strip()),
            keep_tags_match=payload.keep_tags_match,
            # Already engine specs (RatingRuleSpec) -- passed through, validated on the wire.
            keep_rating_rules=tuple(payload.keep_rating_rules),
            keep_rating_match=payload.keep_rating_match,
        )
    except ValidationError as exc:
        raise HTTPException(
            422,
            detail=[
                {
                    "loc": [str(part) for part in error["loc"]],
                    "msg": error["msg"].removeprefix("Value error, "),
                    "type": error["type"],
                }
                for error in exc.errors()
            ],
        ) from exc


async def _requests_app_configured(session: AsyncSession) -> bool:
    """Whether an enabled Seerr exists, which is what lets ``inspect`` say that a
    "requested only" keep-last scope has nothing to read and is quietly doing nothing."""
    row = (
        await session.execute(
            select(Instance.id).where(
                Instance.kind == InstanceKind.SEERR, Instance.enabled.is_(True)
            )
        )
    ).first()
    return row is not None


def _policy_out(
    body: PolicyBody,
    name: str,
    *,
    requests_app_configured: bool,
    settings: ProfileSettings,
    needs_save: bool = False,
    fell_back: bool = False,
) -> PolicyOut:
    return PolicyOut(
        policy_hash=body.policy_hash(),
        name=name,
        needs_save=needs_save,
        fell_back=fell_back,
        body=PolicyIn(
            name=name,
            media_type=body.media_type,
            condemn_at=body.condemn_at,
            coverage_floor_bp=body.coverage_floor_bp,
            keep_last_seasons=body.keep_last_seasons,
            keep_first_season=body.keep_first_season,
            keep_last_scope=body.keep_last_scope,
            season_lookahead=body.season_lookahead,
            keep_in_progress=body.keep_in_progress,
            in_progress_hold_days=body.in_progress_hold_days,
            keep_specials=body.keep_specials,
            flag_keep_conflicts=body.flag_keep_conflicts,
            gates=[
                GateSettingIn(
                    gate=g.gate,
                    enabled=g.enabled,
                    threshold=g.threshold,
                    secondary=g.secondary,
                    window_days=g.window_days,
                )
                for g in body.gates
            ],
            signals=[
                SignalSettingIn(
                    signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor
                )
                for s in body.signals
            ],
            protect_conditions=[
                ConditionIn(field=c.field, op=c.op, value=c.value) for c in body.protect_conditions
            ],
            custom_condemn=list(body.custom_condemn),
            graded_keeps=list(body.graded_keeps),
            keep_tags=list(body.keep_tags),
            keep_tags_match=body.keep_tags_match,
            keep_rating_rules=list(body.keep_rating_rules),
            keep_rating_match=body.keep_rating_match,
        ),
        warnings=[
            # Only draft warnings here. The two LOAD-time recoveries (rescaled /
            # fell_back) are separate fields, not warnings: the editor renders warnings
            # from re-validating the DRAFT, so anything attached to the GET response
            # never reaches the page at all. That was a real silent drop.
            PolicyWarningOut(field=w.field, message=w.message, severity=w.severity)
            # The operator's SAVED settings, not the defaults. Passing ProfileSettings()
            # here made every settings-based warning unreachable: the caps and the
            # approval switch live on the profile, so inspecting a stand-in meant the
            # editor could never show a warning about any of them. A settings warning
            # therefore appears once the change is saved rather than as it is typed --
            # the savebar writes policy and profile together, so that is one click away.
            for w in inspect(body, settings, requests_app_configured=requests_app_configured)
        ],
    )


def _candidate_media_type(policy_media_type: str) -> str:
    """The candidate ``media_type`` a policy governs: a TV policy scores *seasons*."""
    return "season" if policy_media_type == "tv" else "movie"


@router.get("/policy")
async def get_policy(request: Request, media_type: str = "movie") -> PolicyOut:
    """Load the active policy for a media type, so the editor opens on what is in force.

    A stored body that no longer validates must never raise here. ``active_policy``
    re-parses stored JSON through ``PolicyBody``, so any rule tightened after that row was
    written turns this route into a 500 and locks the operator out of the one page that
    fixes it. Two recoveries, in order:

    1. **Rescale.** A body written before removal weights had to total 100 is repaired by
       ``policy.rebalance``, which keeps the operator's tuning. The exact rescale cannot
       move a score, but integer rounding can, by more than a point (see that function's
       docstring for the worked cases) -- which is precisely why it comes back as an
       *unsaved draft*: the operator's own tuning, in the new units, with nothing written
       until they look at it and press Save. Their approvals stay valid until they do.
    2. **Fall back.** Anything we cannot repair opens on the shipped default, saying so,
       so nobody mistakes it for what is in force.
    """
    async with _sessions(request)() as session:
        active = await active_policy(session, media_type)
        body, name = active.body, active.name
        # The two recoveries read very differently to an operator -- "your policy, in new
        # units" versus "your policy is gone" -- so they are separate flags, never inferred
        # from the name (an operator's own policy is often called "default").
        needs_save, fell_back = active.rescaled, active.fell_back
        has_requests_app = await _requests_app_configured(session)
        settings = await active_profile_settings(session)
    return _policy_out(
        body,
        name,
        requests_app_configured=has_requests_app,
        settings=settings,
        needs_save=needs_save,
        fell_back=fell_back,
    )


@router.post("/policy")
async def save_policy(request: Request, payload: PolicyIn) -> PolicyOut:
    """Save a policy. **Append-only: this never updates a row.**

    Re-saving the policy already in force is a no-op rather than a duplicate -- the hash
    is the identity, so an owner who opens the editor and saves without changing anything
    does not fork the audit trail. Only the *active* row may short-circuit like that:
    content matching an older, superseded row still appends a fresh row, because "in
    force" means "newest row for the media type". Skipping that write is how a revert
    used to vanish -- 200, reverted body in the response, old policy still active.

    Note what this does *not* do: it does not arm anything. Reaper still cannot delete,
    and a saved policy takes effect on the next scan.
    """
    body = _to_body(payload)
    policy_hash = body.policy_hash()

    async with _sessions(request)() as session:
        active = await active_policy_row(session, body.media_type)
        has_requests_app = await _requests_app_configured(session)
        settings = await active_profile_settings(session)

        if active is not None and active.policy_hash == policy_hash:
            # Content-identical to the policy in force: nothing is written and the name
            # is NOT changed. Echo the *persisted* name, not the discarded request name,
            # so the success response matches what the next GET /api/policy will show --
            # otherwise a name-only edit looks like it stuck when it silently did not.
            return _policy_out(
                body, active.name, requests_app_configured=has_requests_app, settings=settings
            )

        session.add(
            PolicyModel(
                policy_hash=policy_hash,
                body_json=body.model_dump_json(),
                media_type=body.media_type,
                name=payload.name,
                created_at=utcnow(),
            )
        )
        await session.commit()

    return _policy_out(
        body, payload.name, requests_app_configured=has_requests_app, settings=settings
    )


@router.post("/policy/validate")
async def validate_policy(request: Request, payload: PolicyIn) -> PolicyOut:
    """Validate, hash, and inspect.

    Validation refuses what is *provably* wrong. ``inspect`` warns about what is merely
    *probably* wrong -- and no validator can tell those apart, because the values are
    legal either way. The archetype: an IMDb floor of 96 is a legal 9.6, and is
    indistinguishable from a Rotten Tomatoes 96 typed into the wrong box.

    This is the route the editor calls as you type, so it is where the warnings are
    actually read. It takes a session for one reason: one warning is about the world
    outside the policy (a "requested only" scope with no Seerr to read), and a policy
    cannot see that from its own fields.
    """
    async with _sessions(request)() as session:
        has_requests_app = await _requests_app_configured(session)
        settings = await active_profile_settings(session)
    return _policy_out(
        _to_body(payload),
        payload.name,
        requests_app_configured=has_requests_app,
        settings=settings,
    )


def _replay_simulation(
    rows: list[Candidate], policy: PolicyBody, decisions: dict[str, str]
) -> SimulationOut:
    """Re-decide the governed rows by replaying the REAL engine over each row's frozen Facts.

    Reached only when the edit left the evidence hash unchanged, so the frozen Facts (and the
    season guard) still describe what a scan would gather. For each row we rebuild its Facts,
    run the same ``evaluate_all`` / ``score`` / ``decide_verdict`` the scan runs
    (services.snapshot._judge_item), and apply any live hand override -- so the counts are
    bit-identical to a fresh scan under this policy, with zero API calls. Exact for weight,
    rating-bar, custom-rule, protect-condition and threshold edits.
    """
    from reaper.services.scan_runner import build_gates

    gates = build_gates(policy)
    signals = [
        SignalConfig(signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor)
        for s in policy.signals
    ]
    custom = policy.custom_signal_configs()
    keeps = policy.keep_configs()
    window = policy.popularity_window_days()

    histogram = [0] * 10
    condemned = protected = abstained = 0
    reclaimable = 0
    unknown_size = 0
    newly = gone = 0
    # (row, re-decided score) -- the NEW score, since a weight edit moves it, not the stored one.
    newly_rows: list[tuple[Candidate, int]] = []
    spared_by: Counter[str] = Counter()

    for row in rows:
        facts, extra = facts_codec.facts_from_dict(json.loads(row.facts_json or "{}"))
        override = whitelist.effective_override(row.media_key, decisions)

        # A hand spare enters as an extra PROTECT, exactly as _judge_item merges it; the
        # frozen season guard rides in `extra`. The reap override is carried into
        # decide_verdict, which honors it only past the cautious cases.
        merged_extra = list(extra)
        if override == "spare":
            merged_extra.insert(
                0, GateResult(GateId.WHITELISTED, PROTECT, detail=HAND_SPARE_DETAIL)
            )
        evaluation = Evaluation(results=[*merged_extra, *evaluate_all(gates, facts).results])
        item_score = score_facts(
            signals, facts, custom_condemn=custom, keeps=keeps, window_days=window
        )
        score_value = round(item_score.value)
        coverage_bp = round(item_score.coverage * 10_000)
        verdict = decide_verdict(
            protected=evaluation.protected,
            blocked=evaluation.blocked,
            safety_protected=any(
                r.fired and r.gate in STRUCTURAL_GATES for r in evaluation.results
            ),
            score=score_value,
            coverage_bp=coverage_bp,
            condemn_at=policy.condemn_at,
            coverage_floor_bp=policy.coverage_floor_bp,
            override=override,
        )

        histogram[min(score_value // 10, 9)] += 1
        was_condemned = row.verdict == "condemn"
        if verdict == "condemn":
            condemned += 1
            if row.size_bytes is None:
                unknown_size += 1
            else:
                reclaimable += row.size_bytes
            if not was_condemned:
                newly += 1
                newly_rows.append((row, score_value))
        elif verdict == "protect":
            protected += 1
            spared_by.update(r.gate.value for r in evaluation.protectors)
        else:
            abstained += 1
            if was_condemned:
                gone += 1

    newly_rows.sort(key=lambda rs: rs[1], reverse=True)
    return SimulationOut(
        exact=True,
        condemned=condemned,
        protected=protected,
        abstained=abstained,
        reclaimable_bytes=reclaimable,
        unknown_size_items=unknown_size,
        newly_condemned=newly,
        no_longer_condemned=gone,
        histogram=histogram,
        examples_newly_condemned=[
            SimExampleOut(title=r.title, year=r.year, score=s) for r, s in newly_rows[:5]
        ],
        protected_by=[
            GateCountOut(gate=gate, count=n)
            for gate, n in sorted(spared_by.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    )


@router.post("/policy/simulate")
async def simulate(request: Request, payload: PolicyIn) -> SimulationOut:
    """Re-decide the last snapshot under a candidate policy. **Zero API calls.**

    This is what makes threshold-tuning honest: the knob and its blast radius sit in the
    same viewport. Move the threshold, and the count, the byte total and the histogram
    move with it -- instantly, without touching Sonarr, Radarr or Tautulli.

    **And it only works for the thresholds.** The simulator re-compares *stored* scores
    and verdicts against new numbers, through the same ``engine.verdict`` decision the
    scan uses. That is exact for ``condemn_at`` and ``coverage_floor_bp``. It is simply
    wrong for anything else: change a signal weight or a gate, and every stored score was
    produced by the old ones. There is no way to recover the new answer from the snapshot
    -- it would take re-reading the library.

    Two kinds of row are never re-decided on score: a row with a protection that could
    not be checked stays abstained at any threshold (the scan refuses to condemn on
    unchecked protections), and a row under a hand override follows the owner's decision
    whatever the threshold: a spare protects, and a reap condemns when the engine
    honors it (services.condemned).

    So rather than return a confident, stale number, it compares the candidate policy's
    ``scoring_hash`` against the snapshot's and, when they differ, **returns nothing but
    the reason**. A plausible wrong answer is worse than a blank: the owner acts on it.

    With separate movie and TV policies, the snapshot's scoring hash is the *combination* of
    both. Editing one leaves the other untouched, so we recombine the candidate policy with the
    other type's current policy and compare that -- and re-decide only the candidates the edited
    policy governs (movies, or seasons), since the other type's verdicts have not moved.
    """
    body = _to_body(payload)
    target = _candidate_media_type(body.media_type)

    async with _sessions(request)() as session:
        snapshot = await _latest_snapshot(session)
        if snapshot is None:
            raise HTTPException(404, "No scan has run yet, so there is nothing to simulate.")

        other_type = "movie" if body.media_type == "tv" else "tv"
        other = (await active_policy(session, other_type)).body

        def _combined(pick: Callable[[PolicyBody], str]) -> str:
            movie_h, tv_h = (
                (pick(body), pick(other))
                if body.media_type == "movie"
                else (pick(other), pick(body))
            )
            return combine_hashes(movie_h, tv_h)

        rows = (
            (
                await session.execute(
                    select(Candidate).where(
                        Candidate.snapshot_id == snapshot.id, Candidate.media_type == target
                    )
                )
            )
            .scalars()
            .all()
        )
        decisions = await whitelist.overrides(session)

        # Three tiers of re-decide, most exact first:
        #  1. Scoring behavior unchanged -> re-compare the STORED score against the new
        #     thresholds. Exact and cheapest (below).
        #  2. Scoring changed but the EVIDENCE is unchanged, and every governed row froze its
        #     Facts -> replay the real engine over those Facts. Exact for weight/rating/custom
        #     edits, still zero API calls.
        #  3. Otherwise the edit changed what the scan gathers (a window, a keep-tag, a
        #     season rule) -> the frozen evidence is stale, so refuse rather than guess.
        if snapshot.scoring_hash != _combined(PolicyBody.scoring_hash):
            replayable = snapshot.evidence_hash and snapshot.evidence_hash == _combined(
                PolicyBody.evidence_hash
            )
            if replayable and rows and all(r.facts_json for r in rows):
                return _replay_simulation(list(rows), body, decisions)
            kind = "movies" if body.media_type == "movie" else "TV"
            return SimulationOut(
                exact=False,
                stale_reason=(
                    "You changed how the scan reads your library (a watch window, a keep tag, or "
                    f"a season rule), so the last scan's evidence no longer fits this {kind} "
                    "policy. Run a scan to apply it, then this becomes exact again."
                ),
                condemned=0,
                protected=0,
                abstained=0,
                reclaimable_bytes=0,
                newly_condemned=0,
                no_longer_condemned=0,
                histogram=[0] * 10,
            )

    histogram = [0] * 10
    condemned = protected = abstained = 0
    reclaimable = 0
    unknown_size = 0
    newly = gone = 0
    newly_rows: list[Candidate] = []
    spared_by: Counter[str] = Counter()

    for row in rows:
        histogram[min(row.score // 10, 9)] += 1

        was_condemned = row.verdict == "condemn"

        # A hand override wins at any threshold: the owner looked and decided, and the
        # scan honors that decision, so the simulator must too. A spare protects; a
        # reap condemns exactly when the engine honors it (services.condemned) -- a
        # safety stop or an unchecked protection still keeps the file, and re-deciding
        # such a row on its score would report movement no scan will ever show.
        override = whitelist.effective_override(row.media_key, decisions)
        if override is not None:
            if override == "spare":
                protected += 1
                spared_by.update(["whitelisted"])
            elif reap_is_effective(row):
                condemned += 1
                if row.size_bytes is None:
                    unknown_size += 1
                else:
                    reclaimable += row.size_bytes
            elif row.verdict == "protect":
                protected += 1
                spared_by.update(_fired_gates(row.explanation_json))
            else:
                abstained += 1
            continue

        # A protection always wins, whatever the threshold. Only the score-based
        # verdicts can move.
        if row.verdict == "protect":
            protected += 1
            spared_by.update(_fired_gates(row.explanation_json))
            continue

        # A row with a protection that could not be checked abstains at ANY threshold:
        # the scan refuses to condemn on unchecked protections ("we could not look" is
        # not "we looked and it was fine"), so counting it as a deletion here would be
        # exactly the plausible wrong answer this route promises to refuse.
        if _has_blocked_protections(row.explanation_json):
            abstained += 1
            continue

        now_condemned = (
            decide_verdict(
                protected=False,
                blocked=False,
                score=row.score,
                coverage_bp=row.coverage_bp,
                condemn_at=body.condemn_at,
                coverage_floor_bp=body.coverage_floor_bp,
            )
            == "condemn"
        )

        if now_condemned:
            condemned += 1
            if row.size_bytes is None:
                unknown_size += 1
            else:
                reclaimable += row.size_bytes
            if not was_condemned:
                newly += 1
                newly_rows.append(row)
        else:
            abstained += 1
            if was_condemned:
                gone += 1

    # The few names the owner will actually recognize: the highest-scoring titles this
    # draft flags that the saved policy does not. A count is abstract; a familiar title
    # is what stops a bad threshold before it is saved.
    newly_rows.sort(key=lambda r: r.score, reverse=True)

    return SimulationOut(
        exact=True,
        condemned=condemned,
        protected=protected,
        abstained=abstained,
        reclaimable_bytes=reclaimable,
        unknown_size_items=unknown_size,
        newly_condemned=newly,
        no_longer_condemned=gone,
        histogram=histogram,
        examples_newly_condemned=[
            SimExampleOut(title=r.title, year=r.year, score=r.score) for r in newly_rows[:5]
        ],
        protected_by=[
            GateCountOut(gate=gate, count=n)
            for gate, n in sorted(spared_by.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    )


def _fired_gates(explanation_json: str) -> list[str]:
    """The gates that fired in one stored explanation, for the spared-by tally.

    Defensive like ``_primary_reason``: an unreadable explanation contributes nothing
    rather than failing the whole simulation.
    """
    try:
        exp = json.loads(explanation_json)
    except (ValueError, TypeError):
        return []
    return [str(entry["gate"]) for entry in exp.get("protections_fired") or [] if "gate" in entry]


def _has_blocked_protections(explanation_json: str) -> bool:
    """Did this row store any protection that could not be checked?

    Read from the stored explanation's ``protections_unknown`` block -- the same record
    the why-panel renders amber -- and defensive like ``_fired_gates``: an unreadable
    explanation reads as unblocked rather than failing the simulation.
    """
    try:
        exp = json.loads(explanation_json)
    except (ValueError, TypeError):
        return False
    return bool(exp.get("protections_unknown"))


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


@router.get("/vocabulary")
async def get_vocabulary(lane: Lane) -> VocabularyOut:
    """The fields available in one lane.

    Filtered **server-side, before serialization**. ``?lane=condemn`` never returns a
    protect-only field, so the browser is not even shown one -- a dangerous condition is
    not merely rejected, it is unconstructable.
    """
    return VocabularyOut(
        lane=lane,
        fields=[
            FieldOut(
                key=spec.key,
                label=spec.label,
                help_text=spec.help_text,
                type=spec.type,
                unit_suffix=spec.unit_suffix,
                ops=list(spec.ops),
            )
            for spec in vocabulary(lane)
        ],
    )


#: The fields whose seen-values are worth suggesting, and the candidate column each is
#: read from. Free-text fields only: numbers and booleans need no suggestions.
_VALUE_COLUMNS = {
    "genre": Candidate.genres_json,
    "quality": Candidate.quality,
    "library": Candidate.library_title,
}


@router.get("/vocabulary/values")
async def vocabulary_values(request: Request, field: str) -> FieldValuesOut:
    """Distinct values the latest scan actually saw for one field, most common first.

    Powers the rule editors' input suggestions ("Documentary", "Bluray-1080p", ...).
    Deliberately fail-open-to-empty: an unknown field, or no scan yet, returns an empty
    list rather than an error, because a suggestion box with nothing to suggest is still
    a working input -- typing any value remains valid either way.
    """
    column = _VALUE_COLUMNS.get(field)
    if column is None:
        return FieldValuesOut(field=field, values=[])

    async with _sessions(request)() as session:
        snapshot = await _latest_snapshot(session)
        if snapshot is None:
            return FieldValuesOut(field=field, values=[])
        raws = (
            (
                await session.execute(
                    select(column).where(Candidate.snapshot_id == snapshot.id, column.is_not(None))
                )
            )
            .scalars()
            .all()
        )

    counts: Counter[str] = Counter()
    for raw in raws:
        if raw is None:  # filtered in SQL; repeated here for the type-checker
            continue
        if field == "genre":
            # genres_json is a JSON array; a row that does not parse contributes nothing.
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                continue
            counts.update(str(g) for g in parsed if g)
        else:
            counts.update([raw])

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return FieldValuesOut(field=field, values=[value for value, _ in ranked[:50]])


# ---------------------------------------------------------------------------
# About
# ---------------------------------------------------------------------------


def _db_bytes(base: Path) -> int:
    """The size of one SQLite database on disk, including its live WAL.

    The -wal file holds committed writes that have not been checkpointed yet, so
    counting the bare .db alone under-reports what the operator's disk is holding.
    """
    total = 0
    for path in (base, base.with_name(base.name + "-wal")):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


@router.get("/about")
async def about(request: Request) -> AboutOut:
    """What's running and where its data lives. Read-only facts for the About page."""
    settings: Settings = request.app.state.settings
    data_dir = settings.data_dir
    return AboutOut(
        version=build_version(),
        license="AGPL-3.0",
        data_dir=str(data_dir),
        reaper_db_bytes=_db_bytes(data_dir / "reaper.db"),
        cache_db_bytes=_db_bytes(data_dir / "cache.db"),
    )
