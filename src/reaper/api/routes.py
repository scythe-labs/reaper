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

import asyncio
import enum
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import and_, asc, desc, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import load_only

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement

from reaper.api import tags as api_tags
from reaper.api.schemas import (
    AboutOut,
    CandidateDetail,
    CandidateLinkOut,
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
    PolicyValidateIn,
    PolicyWarningOut,
    RatingsOut,
    SeasonShapeOut,
    SignalSettingIn,
    SimExampleOut,
    SimulationOut,
    SnapshotOut,
    VocabularyOut,
    thaw_threshold,
)
from reaper.buildinfo import build_version
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.models import Candidate, FirstFlagged, Instance, InstanceKind, PlexServer, Snapshot
from reaper.db.models import Policy as PolicyModel
from reaper.engine import facts_codec, identity
from reaper.engine.dormancy import history_reach_days
from reaper.engine.explanation import read_explanation
from reaper.engine.fields import Lane, MediaType, vocabulary
from reaper.engine.gates import PROTECT, GateId, GateResult, thaw_defers_to_owner
from reaper.engine.observation import Known
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
from reaper.engine.verdict import decide_verdict
from reaper.services import app_settings, backup, whitelist
from reaper.services.condemned import (
    MATCH_UNREADABLE,
    effective_condemned,
    effective_verdict,
    match_state,
    overridden_lane_shifts,
    reap_is_effective,
    reap_is_effective_decoded,
    reap_override_verdict,
)
from reaper.services.deep_links import build_links
from reaper.services.display_meta import parse_ratings_json
from reaper.services.history_sync import horizon
from reaper.services.planner import MediaRef, PlanError
from reaper.services.profiles import active_policy, active_policy_row, active_profile_settings
from reaper.services.snapshot import HAND_SPARE_DETAIL, effective_fate, judge_facts

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


@router.get("/snapshots/latest", tags=[api_tags.SCANS])
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
    decisions = await whitelist.overrides(session)
    # Move each overridden item from its pure-policy lane to its EFFECTIVE lane, so the headline
    # counts agree with the tabs and the reap ledger (condemned.effective_*). Only overridden
    # rows shift, so this is the group-by above plus a handful of deltas.
    for _candidate, from_lane, to_lane in await overridden_lane_shifts(
        session, snapshot.id, decisions
    ):
        counts[from_lane] = counts.get(from_lane, 0) - 1
        counts[to_lane] = counts.get(to_lane, 0) + 1

    # Reclaimable bytes are summed over the EFFECTIVE condemned set -- the exact rows the planner
    # will act on -- so a spared condemnation stops counting and a honored hand reap starts. A
    # missing size is held out of the total and counted as unknown instead, never read as zero.
    effective = await effective_condemned(session, snapshot.id, decisions)
    reclaimable = sum(c.size_bytes or 0 for c in effective.values())
    unknown_size = sum(1 for c in effective.values() if c.size_bytes is None)

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


@router.get("/snapshot/season-shape", tags=[api_tags.POLICY])
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


@router.get("/candidates", tags=[api_tags.REVIEW])
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
        decisions = await whitelist.overrides(session)
        conditions = [Candidate.snapshot_id == snapshot.id]
        # Tab membership is the EFFECTIVE lane, not the raw verdict: a hand override moves an item
        # to the lane of what will actually happen -- a spared condemnation shows under Kept, a
        # honored hand reap under Condemned -- while its stored verdict stays pure policy. Only
        # overridden rows move, so raw verdict==lane stays the indexed path and the few moves are
        # spliced on; condemned.effective_verdict is the one classifier the scan summary shares.
        shifts = await overridden_lane_shifts(session, snapshot.id, decisions)
        moved_out = [c.media_key for c, from_lane, _to in shifts if from_lane == verdict]
        moved_in = [c.media_key for c, _from, to_lane in shifts if to_lane == verdict]
        lane = Candidate.verdict == verdict
        if moved_out:
            lane = and_(lane, Candidate.media_key.not_in(moved_out))
        if moved_in:
            lane = or_(lane, Candidate.media_key.in_(moved_in))
        conditions.append(lane)
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
            # precedence in SQL is how the copies would drift. Totals below use the same
            # final conditions, so count, bytes and page describe one set.
            #
            # Only the rows that could POSSIBLY carry a decision are resolved, not every
            # row in the lane. An item's effective override is non-null exactly when its
            # OWN key is in the whitelist or its SHOW's key is -- so a row that matches
            # neither is "none" without asking. That set is bounded by how many overrides
            # the operator has made, where the old form materialized every media_key in
            # the lane into Python and bound them all into one IN: on a large library
            # `override=none` bound nearly every row and could blow past SQLite's
            # bound-variable ceiling into a 500 (P-3).
            #
            # `group_key` is the show's key for a season row and null for a movie -- the
            # same value whitelist.show_key derives from a season's media_key (both are
            # `sonarr:{instance}:{series}`; pinned by a test). It is used ONLY to select
            # which rows are worth resolving; effective_override still makes the decision.
            decided = await _decided_keys(session, conditions, decisions)
            if override == "none":
                conditions.append(Candidate.media_key.notin_(decided))
            else:
                conditions.append(
                    Candidate.media_key.in_(
                        [
                            k
                            for k in decided
                            if whitelist.effective_override(k, decisions) == override
                        ]
                    )
                )

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
        # Which snapshot this page was drawn from. The queue compares it against the newest
        # completed scan (from the polled status) to notice when a scan has landed a fresher
        # snapshot underneath an open review, without re-deciding anything here.
        response.headers["X-Snapshot-Id"] = str(snapshot.id)

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
        expiries = await whitelist.spare_expiries(session)
        group_totals, group_marks = await _group_rollups(
            session, snapshot.id, {r.group_key for r in rows if r.group_key}, decisions, expiries
        )

        return [
            _candidate_out(
                r,
                flagged.get(r.media_key),
                decisions,
                group_condemned=group_totals.get(r.group_key) if r.group_key else None,
                group_seasons=group_marks.get(r.group_key) if r.group_key else None,
                expiries=expiries,
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
    expiries: dict[str, datetime | None],
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
    override, whether a hand reap actually takes, and when a hand spare stops keeping it)
    across all lanes, sorted by season number (unnumbered rows last). The expiry rides
    along because the strip square colors by the item's FATE (rule 49), and an expired
    spare is a fate of its own -- still keeping the file, but no longer a live decision.
    Without it the square could only draw the solid "you chose this" green, which is the
    one thing an expired spare is not.

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
            # The spare in EFFECT on this season, matching `override` above: its own if it has
            # one, else its show's. Read only alongside a "spare" decision, exactly as
            # `_candidate_out` reads it, so a non-spared square never carries a stray date.
            spare_exp = (
                whitelist.effective_spare_expiry(media_key, decisions, expiries)
                if override == "spare"
                else None
            )
            mark = GroupSeasonMarkOut(
                id=int(candidate_id),
                season=_season_number(media_key),
                verdict=str(verdict),
                override=override,
                override_effective=(True if override == "reap" and verdict == "condemn" else None),
                size_bytes=size_bytes,
                spare_expires_at=spare_exp.isoformat() if spare_exp is not None else None,
                spare_covers_until=_covers_until(media_key, override, decisions, expiries),
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


#: How many keys ride in one ``IN`` expansion. SQLite caps bound variables per statement,
#: so every list-shaped filter here is fed in chunks, the way ``_group_rollups`` does.
_KEY_CHUNK = 500


async def _decided_keys(
    session: AsyncSession,
    conditions: list[ColumnElement[bool]],
    decisions: dict[str, str],
) -> list[str]:
    """The media_keys under ``conditions`` that carry a hand decision, own or inherited.

    The candidate set is narrowed in SQL to rows whose own key, or whose show key, appears
    in the whitelist -- nothing else can have an effective override -- and the real
    :func:`whitelist.effective_override` then decides each one. So the expensive part
    scales with the operator's overrides rather than with the size of their library, and
    the precedence between a season's decision and its show's still lives in exactly one
    function.
    """
    if not decisions:
        return []
    keys = sorted(decisions)
    found: list[str] = []
    for start in range(0, len(keys), _KEY_CHUNK):
        chunk = keys[start : start + _KEY_CHUNK]
        rows = (
            await session.execute(
                select(Candidate.media_key).where(
                    *conditions,
                    or_(Candidate.media_key.in_(chunk), Candidate.group_key.in_(chunk)),
                )
            )
        ).scalars()
        found.extend(k for k in rows if whitelist.effective_override(k, decisions) is not None)
    # A season whose own key sits in one chunk and whose show key sits in another is found
    # twice; dedupe so the IN below carries each key once.
    return list(dict.fromkeys(found))


class _PanelExplanation(NamedTuple):
    """The why-panel's explanation block plus whether it had to be invented."""

    body: Explanation
    unreadable: bool


def _explanation_out(row: Candidate) -> _PanelExplanation:
    """The why-panel's explanation, degrading instead of erroring the panel (PR-7).

    Every sibling display extractor here treats an unreadable stored explanation as
    nothing to show; this one used to build ``Explanation(**json.loads(...))`` bare, so a
    corrupt or legacy row 500ed the panel instead. Now a row that will not decode, or
    will not validate against the current schema, falls back to what the row itself still
    knows -- its score and how much of it could be checked -- and says so.

    The fallback deliberately leaves ``threshold`` unset rather than filling a number in.
    The panel prints "your threshold is N" beside the score, and an invented N would state
    a policy setting that is not the operator's. Unset, the panel omits the clause.

    The read itself is ``engine.explanation.read_explanation`` rather than a ``try`` around
    the model here, because ``services.condemned`` must refuse a hand reap on exactly the
    rows this degrades and cannot reach into this layer to find out which they are (#142).
    """
    body = read_explanation(_decode_explanation(row.explanation_json))
    if body is not None:
        return _PanelExplanation(body, False)
    log.warning("candidate.explanation_unreadable", media_key=row.media_key)
    return _PanelExplanation(
        Explanation(
            score=float(row.score),
            coverage=row.coverage_bp / 10_000,
            signals=[],
            protections_fired=[],
            protections_checked=[],
            protections_unknown=[],
        ),
        True,
    )


def _decode_explanation(explanation_json: str) -> dict[str, Any] | None:
    """One guarded parse of a stored explanation, shared by every display extractor.

    ``_candidate_out`` decodes each row here ONCE and hands the result to
    ``_dormant_for``, ``_primary_reason``, ``_chip`` and the reap-override read, instead
    of each running its own ``json.loads`` over the same multi-KB document (P2-2).

    Returns ``None`` for anything that is not a JSON object, so a corrupted or
    hand-edited row degrades to a card with no reason line rather than 500-ing the whole
    review queue. Each extractor re-checks that it was handed a dict, so calling one
    directly is exactly as defensive as calling it through here.
    """
    try:
        exp = json.loads(explanation_json)
    except (ValueError, TypeError):
        return None
    return exp if isinstance(exp, dict) else None


def _entries(exp: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """The dict entries under ``key``, dropping anything that is not one.

    Every protection/signal list in an explanation is a list of objects. A stored row
    that says otherwise loses the malformed entries rather than raising out of a display
    extractor -- ``exp["protections_fired"][0]["detail"]`` on a string entry is a
    TypeError, and one such row would blank the entire queue (B2-13).
    """
    value = exp.get(key)
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def _match_status(exp: dict[str, Any]) -> str | None:
    """The Plex match state, straight from the one function that derives it.

    Kept as a thin alias so the queue's chips and card reasons cannot drift from the
    reap-override decision that reads the same block: an unreadable match HOLDS a reap
    (:data:`~reaper.services.condemned.MATCH_UNREADABLE`), and the copy beside it has to
    say so rather than falling through to "Scored below your threshold" (rule 61).
    """
    return match_state(exp)


def _detail_of(entry: dict[str, Any]) -> str | None:
    """One entry's plain-English detail line, or ``None`` when it has none."""
    detail = entry.get("detail")
    return str(detail) if detail else None


def _contribution(entry: dict[str, Any]) -> float:
    """One signal entry's contribution, as a number safe to sort on.

    ``_entries`` guarantees the entry is a dict; it promises nothing about the values
    inside it. Two stored entries whose contributions are a number and a string make
    ``list.sort`` raise a TypeError comparing them, which would blank the whole condemned
    lane over one hand-edited row. Anything that is not a number reads as 0.0 and sorts
    last, so a readable signal is always preferred as the card's reason.
    """
    value = entry.get("contribution")
    return float(value) if isinstance(value, int | float) else 0.0


def _managing_app(media_type: str) -> str:
    """Which *arr manages this kind of item, for copy that names it.

    Two stored media types, ``movie`` and ``season``, and Reaper only ever reaches a movie
    through Radarr and a season through Sonarr, so this is total rather than a guess. Named
    apps beat "the app that manages it" here because the operator has to go open one of
    them to fix a disagreement, and the copy may as well say which.
    """
    return "Radarr" if media_type == "movie" else "Sonarr"


def _primary_reason(
    exp: dict[str, Any] | None, verdict: str, score: int, media_type: str = "movie"
) -> str | None:
    """The single line the card shows: *why* Reaper judged this, not what it is about.

    A spared item leads with the protection that saved it; a reaped one with its strongest
    reason; an abstained one with what stopped it short. All of these are already plain
    English in the stored explanation -- this only picks which one to surface.

    Takes the DECODED explanation (``_decode_explanation``), and like every sibling
    extractor treats an unreadable one as "no reason to show", never as an error: display
    extraction must never drop a row off the queue.

    ``score`` comes from the row, not from ``exp``, so this reads the same number ``_chip``
    does (rule 104): the stored explanation carries its own copy, and two readers picking
    different sources is how the chip and the line beneath it come to disagree.
    """
    if not isinstance(exp, dict):
        return None

    if verdict == "protect":
        fired = _entries(exp, "protections_fired")
        if fired:
            return _detail_of(fired[0])
        # A protect with nothing fired is a hand reap the engine refused to honor: the item
        # was blocked (e.g. the season keep-rule conflict), so a "reap" override resolves to
        # protect. Surface that blocked reason so the held row says WHY, not a generic line.
        unknown = _entries(exp, "protections_unknown")
        return _detail_of(unknown[0]) if unknown else None
    if verdict == "condemn":
        signals = [s for s in _entries(exp, "signals") if s.get("evaluated")]
        signals.sort(key=_contribution, reverse=True)
        return _detail_of(signals[0]) if signals else None
    # abstain: lead with the match problem when there is one -- it is the single cause
    # behind every "could not check" that follows, and the raw gate detail ("could not
    # check the watch horizon: ...") repeats it in engineer-speak. Otherwise fall back to
    # the first unchecked protection, whose detail is already a plain sentence.
    status = _match_status(exp)
    if status == "unmatched":
        return "Kept to be safe: it couldn't be found in Plex."
    if status == "ambiguous":
        return "Kept to be safe: it looks like more than one thing in Plex."
    if status == "conflicted":
        app = _managing_app(media_type)
        thing = "file" if media_type == "movie" else "show"
        return f"Kept to be safe: Plex and {app} describe this {thing} differently."
    if status == MATCH_UNREADABLE:
        # The row records a match Reaper cannot read. That HOLDS a hand reap, so falling
        # through to the below-threshold line below stated the opposite of the decision in
        # force on this very row (rule 61).
        return "Kept to be safe: Reaper couldn't read what this matched in Plex."
    unknown = _entries(exp, "protections_unknown")
    if unknown:
        return _detail_of(unknown[0])
    # An abstain that got this far was stopped by the score or by the coverage floor, and
    # they are different decisions with different remedies: one says move the slider, the
    # other says fix the evidence source. ``decide_verdict``'s order settles which -- past
    # every blocked case, an abstain at or above the threshold can only be the floor. This
    # is the same branch ``_chip`` makes (see the threshold block there), and until it was
    # made here too the panel printed the chip "Too little of it could be checked" directly
    # above "Scored below your threshold." Rule 61 forced this same correction on this line
    # once already, for the unreadable-match case; the coverage arm was left behind.
    threshold = thaw_threshold(exp.get("threshold"))
    if threshold is not None and score >= threshold:
        return "Kept to be safe: too little of it could be checked."
    return "Scored below your threshold."


def _dormant_for(exp: dict[str, Any] | None) -> str | None:
    """The humanized dormancy span ("5 years, 9 months") for the card's amber pill.

    Read from the stored explanation's UNWATCHED signal, whose detail has exactly one
    producer (engine/signals.py): ``not watched in {span}``. Anything else -- the
    signal unevaluated, an older snapshot's different phrasing, a missing block --
    degrades to ``None`` and the pill is hidden. Same defensive posture as
    ``_primary_reason``: display extraction must never error a row off the queue.
    """
    prefix = "not watched in "
    if not isinstance(exp, dict):
        return None
    for signal in _entries(exp, "signals"):
        if signal.get("id") != "unwatched":
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
    """The chip phrase for a fired season keep rule (season_pruning's closed reasons).

    Three of these reasons were reworded to name what Sonarr actually reported rather than
    what it usually means (see ``_protection_reason``), so an explanation stored by an older
    scan carries the retired spelling. Those fall through to the generic phrase below, which
    is vague but true -- the same degrade every unrecognized detail takes, and the reason
    this parser has a fallback at all. The next scan restores the specific phrase.
    """
    if detail.startswith("specials"):
        return "specials are never removed"
    if detail.startswith("episodes are missing"):
        return "episodes are missing"
    if detail.startswith("the newest season of a show"):
        return "the show is still running"
    if detail.startswith("the earliest season"):
        return "the earliest season stays"
    if keep_last := _KEEP_LAST_RE.match(detail):
        return f"in the last {keep_last.group(1)} seasons you keep"
    if detail.startswith("this show has only"):
        return "your keep rule keeps all its seasons"
    if detail.startswith("a viewer is part-way"):
        return "someone is partway through"
    if detail.startswith("your watch history is too short"):
        # NOT a keep rule, so it must not fall to the generic phrase below: the lever is the
        # depth of the watch history (or the hold set against it), and naming a season rule
        # sends the operator to edit a control that will not move it.
        return "your watch history is too short to tell"
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
        # "Untouched", never "watched": this gate's clock runs from the last play only when
        # there IS one, and otherwise from the day the file arrived
        # (``engine.dormancy.reference_instant``). So the fired branch covers a title nobody
        # has ever played, and the chip this function used to return -- "watched too
        # recently" -- asserted a play that never happened, on a card whose own panel said
        # "nobody watched it in the last year" three lines above. ``MinDormancyGate`` words
        # its own detail "untouched" for exactly this reason; the chip beside it now does too.
        return "hasn't sat untouched long enough"
    if gate == "unmanaged":
        # Retired gate, kept for stored explanations only -- a snapshot taken before the
        # retirement can still be read back, and this is what renders its chip. No new scan
        # produces it (``engine.gates``, and the same reasoning as ``others_watching`` above).
        return "not managed by Sonarr or Radarr"
    if gate == "season_progression":
        return _kept_season_phrase(detail)
    if gate == "custom":
        return "by your rule"
    return "a protection applies"


def _chip(
    exp: dict[str, Any] | None, verdict: str, score: int, media_type: str = "movie"
) -> ChipOut | None:
    """The card's one short status chip -- Sanctuary and Limbo lanes only.

    Follows decide_verdict's own precedence (match trouble, then a deliberate
    left-for-you flag, then checks that couldn't run, then the coverage floor, then
    the score) so the chip names the fact that actually put the item in its lane.
    Pure display extraction from the DECODED stored explanation
    (``_decode_explanation``): never a re-decision, and never an error that drops a row
    off the queue. Condemned rows get no chip here; their card leads with the amber
    dormancy pill (``dormant_for``).

    Each chip carries its ``why`` clause (see ``ChipOut``) so the refused-reap chip can
    say the same fact mid-sentence without the frontend re-parsing ``text``. Reword a
    chip here and reword its clause in the same line.
    """
    if not isinstance(exp, dict):
        return None

    if verdict == "protect":
        fired = _entries(exp, "protections_fired")
        if not fired:
            return None
        gate = str(fired[0].get("gate") or "")
        detail = str(fired[0].get("detail") or "")
        phrase = _kept_phrase(gate, detail)
        # The kept phrase is already a lowercase clause, so the chip and its why say the
        # same words with and without the "Kept · " lead.
        return ChipOut(tone="kept", text=f"Kept · {phrase}", why=phrase)

    if verdict != "abstain":
        return None

    status = _match_status(exp)
    if status == "unmatched":
        return ChipOut(
            tone="quiet",
            text="Couldn't be found in Plex",
            why="it couldn't be found in Plex",
        )
    if status == "ambiguous":
        return ChipOut(
            tone="quiet",
            text="Looks like two different things in Plex",
            why="it looks like two different things in Plex",
        )
    if status == "conflicted":
        app = _managing_app(media_type)
        return ChipOut(
            tone="quiet",
            text=f"Plex and {app} don't agree",
            why=f"Plex and {app} don't agree about it",
        )
    if status == MATCH_UNREADABLE:
        return ChipOut(
            tone="quiet",
            text="Couldn't read its Plex match",
            why="Reaper couldn't read what it matched in Plex",
        )

    unknown = _entries(exp, "protections_unknown")
    for entry in unknown:
        detail = str(entry.get("detail") or "")
        if detail and not detail.startswith("could not check"):
            # A deliberate "decide this yourself" flag -- today, the season keep-rule
            # conflict -- not a plumbing failure. The one blocked case that wants eyes.
            if str(entry.get("gate") or "") == "season_progression":
                # Three conflict shapes reach here and only ONE of them made a comparison
                # (``services.season_pruning.PruneConflict.message``). A conflict is also
                # raised when the kept season's watcher count could not be read at all --
                # ``detect_conflicts`` treats that as a hold rather than letting an unread
                # number clear a protection -- and when the watch mirror does not reach back
                # to when one of the two seasons arrived, so the count it reports for that
                # season is a lower bound and no comparison against it settles anything.
                # Asserting the comparison there states arithmetic against a number nobody
                # took, which is the exact sentence ``detect_conflicts``'s docstring records
                # having deliberately removed from the message; the chip was still printing
                # it, one line above the panel's own denial.
                #
                # The two non-comparisons share this chip because they share the flag, so its
                # copy has to be true of both: the unestablished season may be the one being
                # removed rather than the one being kept, and naming the kept one was wrong
                # half the time once the reach arm landed.
                #
                # The producer now says which shape this is, so read the flag rather than
                # the sentence (rule 92). The flag no longer decides whether a hand reap is
                # honored -- no blocked gate holds one (see ``engine.verdict``) -- and is now
                # read purely to pick what the operator is told. Nothing about the chip
                # changes with that: "did Reaper actually make this comparison" is worth
                # telling them whether or not it gates anything.
                #
                # This chip is no longer its only reader. ``GateOutcomeOut`` serves the flag
                # to the why panel, whose verdict note branches the same three ways off it
                # (``WhyPanel.conflictNote``, #86) -- so the two agree about the CONFLICT,
                # clause for clause. A fourth shape of sentence added here wants adding there
                # too (rule 72).
                #
                # They can still lead with different stories, which is not a divergence:
                # ``_match_status`` is consulted above this loop, so a row that also has Plex
                # match trouble gets that chip ("Couldn't be found in Plex") while the panel
                # headline still reads the conflict and names the match separately in
                # ``KeptNotice``. The chip has one line and must pick; the panel has room for
                # both.
                #
                # Two rows reach here saying nothing: one frozen before the flag, which carries
                # no key, and one carrying a value that is not a bool, which carries no legible
                # answer -- `thaw_defers_to_owner` reads both to ``None`` and holds the rule.
                # Nothing in either can tell a made comparison from a refused one, the wording
                # that used to stand in for the flag being exactly what failed. So neither names
                # a shape and both fall to the vague-but-true chip below. Recovering it from
                # the wording was tried
                # and is wrong: it read "more than watched Season" as a deferral while
                # ``condemned.reap_override_verdict`` read the absent key as a hold, so the
                # card offered a conflict to settle and then refused the reap by citing that
                # same conflict back at the operator.
                defers = thaw_defers_to_owner(entry.get("defers_to_owner"))
                if defers is True:
                    return ChipOut(
                        tone="look",
                        text="Needs a look · watched more than a season your rule keeps",
                        why="watched more than a season your rule keeps",
                    )
                if defers is False:
                    return ChipOut(
                        tone="look",
                        text="Needs a look · couldn't check who watched these seasons",
                        why="Reaper couldn't check who watched these seasons",
                    )
            return ChipOut(
                tone="look",
                text="Needs a look · left for you to decide",
                why="a check on it couldn't be settled",
            )
    if unknown:
        return ChipOut(
            tone="quiet",
            text="Some checks couldn't run",
            why="some checks couldn't run",
        )

    threshold = thaw_threshold(exp.get("threshold"))
    if threshold is not None:
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


def _covers_until(
    media_key: str,
    override: str | None,
    decisions: dict[str, str],
    expiries: dict[str, datetime | None],
) -> str | None:
    """ISO of the LAST spare covering this item, for the surfaces that color it (rule 49).

    The twin of the ``spare_expires_at`` each caller computes beside it, and deliberately a
    different question: that one is the spare in force by precedence, which is what a control
    toggles (rule 50), while this one is when the item stops being kept, which is what a color
    or a sentence about its fate must read. They differ exactly when both levels spare an item
    and the higher-precedence one runs out first. Derived once here, from
    :func:`whitelist.covering_spare_expiry`, so every emitting site answers it the same way
    (rule 104).

    ``None`` covers both "forever" and "not spared", as ``spare_expires_at`` already does: read
    it only alongside a ``"spare"`` decision.
    """
    if override != "spare":
        return None
    covers = whitelist.covering_spare_expiry(media_key, decisions, expiries)
    return covers.isoformat() if covers is not None else None


def _candidate_out(
    r: Candidate,
    flagged_at: datetime | None = None,
    decisions: dict[str, str] | None = None,
    *,
    group_condemned: tuple[int, int, int] | None = None,
    group_seasons: list[GroupSeasonMarkOut] | None = None,
    expiries: dict[str, datetime | None] | None = None,
) -> CandidateOut:
    # Three views of the one whitelist: the decision in EFFECT (own, or inherited from the
    # show) colors the row; the item's OWN decision is what a control on this row can toggle;
    # the SHOW's decision is what still keeps a season the operator did not touch. Computed in
    # one place so a season row and its show card can never disagree about what is spared.
    decisions = decisions or {}
    expiries = expiries or {}
    override = whitelist.effective_override(r.media_key, decisions)
    override_own = decisions.get(r.media_key)
    _show_key = whitelist.show_key(r.media_key)
    show_override = decisions.get(_show_key) if _show_key else None
    # The expiry belongs to whichever spare is in force: the effective one colors this row's
    # countdown, the show one drives the whole-show card. Both are None for a forever spare
    # (and for no spare -- read them only alongside the matching "spare" decision above).
    spare_exp = (
        whitelist.effective_spare_expiry(r.media_key, decisions, expiries)
        if override == "spare"
        else None
    )
    show_spare_exp = expiries.get(_show_key) if (_show_key and show_override == "spare") else None
    # ONE parse of the stored explanation, shared by the pill, the reason line, the chip
    # and the reap-override read below. Each used to run its own json.loads over the same
    # multi-KB document, three or four times per row and up to 500 rows per page (P2-2).
    explanation = _decode_explanation(r.explanation_json)
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
        dormant_for=_dormant_for(explanation),
        reason=_primary_reason(explanation, r.verdict, r.score, r.media_type),
        spared=override == "spare",
        override=override,
        override_own=override_own,
        show_override=show_override,
        # Whether a hand reap actually takes: decide_verdict honors it past cautious
        # protections but never past a safety stop or an unchecked protection. The UI
        # colors the row red only when this is True, so it never promises a removal
        # the engine will refuse.
        override_effective=(
            reap_is_effective_decoded(r, explanation) if override == "reap" else None
        ),
        spare_expires_at=spare_exp.isoformat() if spare_exp is not None else None,
        spare_covers_until=_covers_until(r.media_key, override, decisions, expiries),
        show_spare_expires_at=show_spare_exp.isoformat() if show_spare_exp is not None else None,
        chip=_chip(explanation, r.verdict, r.score, r.media_type),
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
            # The operator's external address for links when set, else the connect address.
            arr_base = instance.external_url or instance.base_url

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
        # The external address for links when the operator set one, else the connect address.
        tautulli_base_url=(tautulli.external_url or tautulli.base_url) if tautulli else None,
        machine_identifier=plex_server.machine_identifier if plex_server else None,
        plex_web_url=await app_settings.get_plex_web_url(session),
        seerr_base_url=(seerr.external_url or seerr.base_url) if seerr else None,
        imdb_id=row.imdb_id,
        media_type=row.media_type,
        # A season row searches by its SHOW's title ("Example Show", not
        # "Example Show · Season 3") -- that is the page the rating describes.
        title=row.group_title or row.title,
        # The rows an abstain could not choose between. Read through the one match thaw
        # (rule 104) rather than a second copy of the same json.loads, so the links and the
        # replay can never disagree about which rows the operator was shown.
        candidate_rating_keys=_replayed_evidence(row).get("match_candidates", ()),
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
        match_candidates=[
            CandidateLinkOut(rating_key=c.rating_key, plex=c.plex, tautulli=c.tautulli)
            for c in links.match_candidates
        ],
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


@router.get("/candidates/{candidate_id}", tags=[api_tags.REVIEW])
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
        expiries = await whitelist.spare_expiries(session)

        base = _candidate_out(
            row,
            flagged.first_flagged_at if flagged else None,
            decisions,
            expiries=expiries,
        )
        explanation = _explanation_out(row)
        return CandidateDetail(
            **base.model_dump(),
            explanation=explanation.body,
            explanation_unreadable=explanation.unreadable,
            links=await _deep_links(session, row),
            ratings=_ratings_out(row.ratings_json),
            content_rating=row.content_rating,
            runtime_minutes=row.runtime_minutes,
            genres=_genres(row.genres_json),
        )


@router.get("/groups/{group_key}", tags=[api_tags.REVIEW])
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
        expiries = await whitelist.spare_expiries(session)

        seasons = [
            _candidate_out(
                r,
                flagged.get(r.media_key),
                decisions,
                expiries=expiries,
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
        # The whole-show spare's countdown, when a show-level spare is what's set. None for a
        # forever show-spare (or none at all); the panel reads it only when show_override is
        # "spare". Same key, same source as the decision below.
        _show_spare_exp = expiries.get(group_key) if decisions.get(group_key) == "spare" else None

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
            show_spare_expires_at=_show_spare_exp.isoformat() if _show_spare_exp else None,
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
    of 0, a dormancy floor under 5 days, a run cap above the rolling cap. Those live in
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
            protect_incomplete_seasons=payload.protect_incomplete_seasons,
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


async def _history_reach_days(request: Request) -> float | None:
    """How far back the watch mirror goes, for ``policy.inspect``, or ``None`` if unknown.

    The second world-fact a policy cannot see about itself. It lets ``inspect`` say that a
    popularity window longer than the mirror blocks ``gates.ServerPopularityGate``
    library-wide, so the scan condemns nothing until the window comes down or history
    accrues.

    Derived through ``dormancy.history_reach_days`` off ``history_sync.horizon``, which is
    exactly how ``services.snapshot.ScanContext`` derives the reach the gate then reads
    (rule 104). The editor must not answer this question a second way, or it could advise
    against a window the scan is perfectly happy with.

    Reading it must never cost the operator their policy editor, so a mirror that will not
    answer resolves to ``None`` -- "could not tell", which ``inspect`` treats as silence.
    That is the safe direction here and only here: the warning gates nothing destructive, so
    the worst a miss can do is withhold advice, while a guess would tell an operator their
    window is useless when it is fine. A scan reading the same horizon degrades instead
    (``services.snapshot``, rule 28); this is not the scan pipeline.
    """
    try:
        earliest = await horizon(request.app.state.cache_engine)
    except (SQLAlchemyError, OSError, AttributeError):
        log.warning("policy.history_reach_unreadable", exc_info=True)
        return None
    return None if earliest is None else float(history_reach_days(earliest, now=utcnow()))


def _policy_out(
    body: PolicyBody,
    name: str,
    *,
    requests_app_configured: bool,
    settings: ProfileSettings,
    history_reach_days: float | None = None,
    needs_save: bool = False,
    fell_back: bool = False,
    rating_rules_restored: bool = False,
) -> PolicyOut:
    return PolicyOut(
        policy_hash=body.policy_hash(),
        name=name,
        needs_save=needs_save,
        fell_back=fell_back,
        rating_rules_restored=rating_rules_restored,
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
            protect_incomplete_seasons=body.protect_incomplete_seasons,
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
            for w in inspect(
                body,
                settings,
                requests_app_configured=requests_app_configured,
                history_reach_days=history_reach_days,
            )
        ],
    )


def _candidate_media_type(policy_media_type: str) -> str:
    """The candidate ``media_type`` a policy governs: a TV policy scores *seasons*."""
    return "season" if policy_media_type == "tv" else "movie"


@router.get("/policy", tags=[api_tags.POLICY])
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

    A third recovery runs on a body that loads perfectly: a rating bar written before the
    bar moved off the gate row is restored (``policy.recover_rating_rules``), because that
    body loads cleanly while keeping nothing. It comes back as an unsaved draft too.
    """
    async with _sessions(request)() as session:
        active = await active_policy(session, media_type)
        body, name = active.body, active.name
        # The recoveries read very differently to an operator -- "your policy, in new
        # units" versus "your policy is gone" versus "a protection was put back" -- so they
        # are separate flags, never inferred from the name (an operator's own policy is
        # often called "default").
        needs_save, fell_back = active.rescaled, active.fell_back
        rating_rules_restored = active.rating_rules_recovered
        has_requests_app = await _requests_app_configured(session)
        settings = await active_profile_settings(session)
    return _policy_out(
        body,
        name,
        requests_app_configured=has_requests_app,
        settings=settings,
        history_reach_days=await _history_reach_days(request),
        needs_save=needs_save,
        fell_back=fell_back,
        rating_rules_restored=rating_rules_restored,
    )


@router.post("/policy", tags=[api_tags.POLICY])
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
    reach_days = await _history_reach_days(request)

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
                body,
                active.name,
                requests_app_configured=has_requests_app,
                settings=settings,
                history_reach_days=reach_days,
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
        body,
        payload.name,
        requests_app_configured=has_requests_app,
        settings=settings,
        history_reach_days=reach_days,
    )


@router.post("/policy/validate", tags=[api_tags.POLICY])
async def validate_policy(request: Request, payload: PolicyValidateIn) -> PolicyOut:
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
    if payload.draft_max_unmeasured_per_run is not None:
        # The editor's unknown-size box is the one control whose warning renders beneath it
        # while showing an unsaved value, so the check runs against what is on screen rather
        # than what is stored (see PolicyValidateIn). Bounds are enforced on the wire by the
        # field itself, so this cannot widen the allowance past what a save would accept.
        settings = settings.model_copy(
            update={"max_unmeasured_per_run": payload.draft_max_unmeasured_per_run}
        )
    return _policy_out(
        _to_body(payload),
        payload.name,
        requests_app_configured=has_requests_app,
        settings=settings,
        history_reach_days=await _history_reach_days(request),
    )


#: How many candidates a simulation loop walks before handing the event loop back. The
#: simulator is pure computation over rows already in memory, so without a yield it holds
#: the single loop for the whole library -- and the policy editor fires this on a 250 ms
#: debounce, so dragging a weight slider queues one full-library replay after another and
#: every other request (scan status, the review queue, auth) stalls behind them (P2-1).
_SIM_YIELD_EVERY = 500


def _replayed_evidence(row: Candidate) -> dict[str, Any]:
    """The evidence kwargs ``judge_facts`` needs, read back off the row's frozen explanation
    so a replay rebuilds what the scan stored rather than re-deriving or dropping it.

    Two things, both evidence rather than policy, and both parsed from one read of the stored
    JSON: the Plex match block, and whether the scan found this title's recorded plays
    unreadable (``watch_blind``, #275). A policy edit changes neither.

    Identity is evidence, not policy: a policy edit cannot change which Plex row an item was
    bound to, so the replay must carry the stored answer rather than re-deriving it or (as it
    did) leaving it blank. A blank match block reads as "nothing wrong with the match", which
    is the permissive direction on the one interlock that still holds a hand reap for a row
    Reaper cannot identify (rule 1: omitted is not the same as an explicit empty).

    Never raises off a stored row (rule 96). An unreadable or unrecognized value degrades to
    ``UNMATCHED``, the conservative answer: it holds the reap, matching what
    ``condemned.match_state`` does with the same row, where anything it cannot parse becomes
    ``MATCH_UNREADABLE`` and holds. A row frozen before the match block existed carries no
    key at all and is left alone -- ``None`` there is genuinely absent, and those rows were
    judged without it.
    """
    try:
        stored = json.loads(row.explanation_json or "{}")
        match = stored.get("match") if isinstance(stored, dict) else None
        # Read before the match branches below, because the two are independent: a row whose
        # match block is missing or malformed can still carry a perfectly good reading, and
        # dropping the flag with the match would silently withdraw the escape from it.
        # Anything that is not a bool thaws to None, never False (rules 96 and 142).
        raw_blind = stored.get("watch_blind") if isinstance(stored, dict) else None
    except (ValueError, TypeError):
        match = None
        raw_blind = None
    blind: dict[str, Any] = {"watch_blind": raw_blind if isinstance(raw_blind, bool) else None}
    if match is None:
        return blind
    if not isinstance(match, dict):
        # Present but unreadable -- the shape `condemned.MATCH_UNREADABLE` exists for. Assert
        # the conservative status rather than dropping to "no match block recorded".
        return {**blind, "match_status": identity.MatchStatus.UNMATCHED}

    def _enum[T: enum.StrEnum](kind: type[T], value: object) -> T | None:
        try:
            return kind(str(value)) if value is not None else None
        except ValueError:
            return None

    status = _enum(identity.MatchStatus, match.get("status"))
    if status is None and match.get("status") is not None:
        status = identity.MatchStatus.UNMATCHED
    merged = match.get("merged_rating_keys")
    candidates = match.get("candidate_rating_keys")
    return {
        "match_status": status,
        "matched_by": _enum(identity.MatchedBy, match.get("by")),
        "match_detail": match.get("detail") if isinstance(match.get("detail"), str) else None,
        "plex_rating_key": rk if isinstance(rk := match.get("rating_key"), int) else None,
        "merged_rating_keys": tuple(k for k in merged if isinstance(k, int))
        if isinstance(merged, list)
        else (),
        # Carried so a replay rebuilds the same match block the scan stored, rather than
        # one whose links quietly went missing. Display only, so an unreadable value is
        # simply empty -- there is no conservative direction for "which rows to offer".
        "match_candidates": tuple(k for k in candidates if isinstance(k, int))
        if isinstance(candidates, list)
        else (),
        **blind,
    }


async def _replay_simulation(
    rows: list[Candidate],
    policy: PolicyBody,
    decisions: dict[str, str],
    *,
    reach_days: int | None,
) -> SimulationOut:
    """Re-decide the governed rows by replaying the REAL engine over each row's frozen Facts.

    Reached only when the edit left the evidence hash unchanged, so the frozen Facts (and the
    season guard) still describe what a scan would gather. For each row we rebuild its Facts,
    hand them to the scan's own judging function (``snapshot.judge_facts``) and apply any live
    hand override through the scan's own ``snapshot.effective_fate`` -- so the counts are
    bit-identical to a fresh scan under this policy, with zero API calls. Exact for weight,
    rating-bar, custom-rule, protect-condition and threshold edits.

    ``reach_days`` is how far back the watch history reached when the snapshot was taken,
    recomputed from the snapshot's own stored ``horizon_at``. Rows frozen before
    ``Facts.history_reach_days`` existed thaw it as ``Unknown`` (rule 104), which the
    popularity gate reads as un-checkable and blocks on -- correct for a fact nobody
    recorded, but it would report every older row as held and break the bit-identical
    promise above. The snapshot row carries the horizon that a re-scan would measure the
    same way, so the gap is filled from stored evidence rather than left as a guess. It
    only ever *fills*: a row that froze its own reach keeps it.

    ``Facts.days_since_added`` is the one gap that CANNOT be filled the same way, so the
    bit-identical promise above is narrowed by exactly this much: on a row frozen before that
    field existed, a rule authored on ``watchers_all_time`` reads as un-establishable
    wherever a deeper mirror could still overturn it (``fields.reach_shortfall``'s
    ITEM_LIFETIME arm has no span to compare the reach against), until the next scan
    re-freezes it. Not every such rule -- ``fields._survives_more_history`` lets the two
    already-earned outcomes through untouched, so an "at least N" the count already clears
    and an "at most N" it already exceeds still evaluate normally. Measured over the 440 lab
    vectors: ``gte 1`` gives 415 matched against 25 blocked, ``lte 5`` gives 172 checked and
    268 blocked -- half the outcome matrix, not all of it. Unlike the reach there is no
    stored source to recover it from -- ``Candidate`` carries no arrival date, and
    ``Facts.days_since_added`` rules out deriving it from dormancy, which is clamped to the
    mirror's edge. The divergence is the keep direction: a blocked protect condition forces
    abstain and a blocked keep takes its full discount, so the preview under-counts
    removals rather than promising one the scan would refuse. Closing it properly means an
    arrival date on ``Candidate`` in an additive migration.

    Both calls are production's, never a lookalike (rules 3/22). This loop used to assemble
    the evaluate / score / round / decide pipeline itself and push the hand override straight
    through ``decide_verdict``; that is the same drift the policy lab had to be pulled back
    from, and the second half of it matters on its own: every other read-side consumer derives
    a hand reap's fate from the frozen explanation (``condemned.reap_override_verdict``), so
    deciding it live here was one edit away from the simulator promising a deletion the
    planner holds.
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

    for index, row in enumerate(rows):
        if index % _SIM_YIELD_EVERY == 0:
            await asyncio.sleep(0)
        facts, extra = facts_codec.facts_from_dict(json.loads(row.facts_json or "{}"))
        if reach_days is not None and not isinstance(facts.history_reach_days, Known):
            facts = replace(
                facts,
                history_reach_days=Known(value=reach_days, source="tautulli"),
            )
        override = whitelist.effective_override(row.media_key, decisions)

        # A hand spare enters as an extra PROTECT so the simulator applies it LIVE -- the stored
        # verdict is pure policy now, so the override is re-applied here, never read off the row.
        # The frozen season guard rides in `extra`. The reap override is carried into
        # effective_fate, which honors it only past the cautious cases.
        merged_extra = list(extra)
        if override == "spare":
            merged_extra.insert(
                0, GateResult(GateId.WHITELISTED, PROTECT, detail=HAND_SPARE_DETAIL)
            )
        judged = judge_facts(
            facts,
            gates,
            policy,
            signals=signals,
            custom_condemn=custom,
            keeps=keeps,
            window_days=window,
            extra_results=merged_extra,
            # The Plex match is REPLAYED, not re-derived: it is identity evidence the scan
            # gathered, and this path re-decides policy over frozen evidence. Omitting it
            # built an explanation whose match block read "no status recorded", which
            # ``condemned.match_state`` reads as "no bad match" -- so a hand-reaped row that
            # production holds because Reaper cannot tell WHICH file it is was previewed
            # here as a deletion, and its bytes counted toward reclaimable.
            #
            # That divergence is new. While a blocked gate still held a hand reap the two
            # sides agreed by accident: an unmatched item has no rating key, so every
            # Plex-dependent gate blocked and the reap was held on both paths for a reason
            # that had nothing to do with the match. Once the block stopped holding, the
            # match became the whole interlock on the stored path and the only one this
            # path was not carrying (rule 3/22).
            **_replayed_evidence(row),
        )
        score_value = judged.score
        verdict = effective_fate(judged, override)

        histogram[min(score_value // 10, 9)] += 1
        # The pre-edit fate is the EFFECTIVE one (override applied), matching the effective "now"
        # verdict below -- so a hand reap's condemnation is never miscounted as a change the
        # POLICY edit caused. The stored verdict alone is pure policy and would misattribute it.
        was_condemned = effective_verdict(row, decisions) == "condemn"
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
            spared_by.update(r.gate.value for r in judged.evaluation.protectors)
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


@router.post("/policy/simulate", tags=[api_tags.POLICY])
async def simulate(request: Request, payload: PolicyIn) -> SimulationOut:
    """Re-decide the last snapshot under a candidate policy. **Zero API calls.**

    This is what makes threshold-tuning honest: the knob and its blast radius sit in the
    same viewport. Move the threshold, and the count, the byte total and the histogram
    move with it -- instantly, without touching Sonarr, Radarr or Tautulli.

    **It works for whatever the frozen evidence can still answer, and refuses the rest.**
    Three tiers, most exact first, enumerated again at the branch below:

    1. ``scoring_hash`` matches -- re-compare the *stored* scores and verdicts against the
       new numbers, through the same ``engine.verdict`` decision the scan uses. Exact for
       ``condemn_at`` and ``coverage_floor_bp``, and the cheapest path.
    2. Scoring changed but ``evidence_hash`` matches, and there is at least one governed row
       and every one of them froze its Facts -- **replay** the real
       ``score``/``evaluate_all``/``decide_verdict`` over ``Candidate.facts_json`` under the
       edited policy (``_replay_simulation``). Exact for every field in
       ``PolicyBody._EVIDENCE_REPLAYABLE_FIELDS``: a weight, a rating bar, a custom condemn
       rule, a graded keep, or a protect condition. Still zero API calls.
    3. Otherwise the edit changed what a scan would *gather* (a watch window, a keep tag, a
       season rule, any gate) -- the frozen evidence is stale, so it **returns nothing but
       the reason**. A plausible wrong answer is worse than a blank: the owner acts on it.

    Tier 2 needs a snapshot that actually froze its evidence: a pre-facts-freeze snapshot
    has a null ``evidence_hash`` or rows with no ``facts_json``, and falls to tier 3.

    Two kinds of row are never re-decided on score: a row with a protection that could
    not be checked stays abstained at any threshold (the scan refuses to condemn on
    unchecked protections), and a row under a hand override follows the owner's decision
    whatever the threshold: a spare protects, and a reap condemns when the engine
    honors it (services.condemned).

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
                    select(Candidate)
                    .where(
                        Candidate.snapshot_id == snapshot.id,
                        Candidate.media_type == target,
                    )
                    # Only the columns both loops below actually read. A full entity load
                    # dragged every row's summary, genres and artwork keys through the
                    # request as well, and on a large library that materialization is a
                    # bigger share of the stall than the scoring itself (P2-1).
                    .options(
                        load_only(
                            Candidate.media_key,
                            Candidate.media_type,
                            Candidate.title,
                            Candidate.year,
                            Candidate.score,
                            Candidate.coverage_bp,
                            Candidate.verdict,
                            Candidate.size_bytes,
                            Candidate.facts_json,
                            Candidate.explanation_json,
                        )
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
                return await _replay_simulation(
                    list(rows),
                    body,
                    decisions,
                    # From the snapshot's own two stored instants, so a row frozen before
                    # the reach was a fact replays on the reach that scan actually had.
                    reach_days=history_reach_days(snapshot.horizon_at, now=snapshot.created_at),
                )
            kind = "movies" if body.media_type == "movie" else "TV"
            return SimulationOut(
                exact=False,
                # States the condition, not who caused it. An upgrade that retires a gate moves
                # both hashes exactly as an edit does (``engine.policy.RETIRED_GATES``), so
                # "you changed" can be false. Kept in step with the frontend's own copy in
                # ``PolicySimulator.tsx``, which is what the operator actually reads.
                stale_reason=(
                    "This policy doesn't match the last scan: a protection, a watch window, a "
                    f"keep tag, or a season rule reads differently from your {kind} policy now. "
                    "Run a scan to apply it, then this becomes exact again."
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

    for index, row in enumerate(rows):
        # The threshold path is cheaper per row than the replay above, but it still parses
        # an explanation per protect/abstain row over the whole library, so it yields too.
        if index % _SIM_YIELD_EVERY == 0:
            await asyncio.sleep(0)
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
            else:
                # A hand reap the engine will not honor yet is KEPT for now (a held reap), so it
                # buckets as protected -- matching condemned.effective_verdict. Its own pure-policy
                # verdict (protect or abstain) is stored raw now and no longer stands in for the
                # effective fate here.
                protected += 1
                spared_by.update(_fired_gates(row.explanation_json))
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
            # A stored condemn cannot also carry a blocking protection (decide_verdict
            # abstains on one), so this only fires for a row whose explanation is
            # unreadable -- and if that row was condemned, it genuinely is not any more.
            if was_condemned:
                gone += 1
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

    Parsed by ``_decode_explanation`` and read through ``_entries``, the same two guards
    every display extractor above goes through. It used to guard only the ``json.loads``
    and then call ``.get`` on whatever came back, so a stored top level that is a list, a
    null or a number raised an AttributeError out of the whole simulation -- one legacy row
    500-ing the policy editor's live preview on every drag of a threshold.

    The unreadable fallback is an EMPTY tally, and here that is the cautious direction: the
    caller has already counted the row in ``protected`` before asking this, so losing the
    attribution under-credits the "what saved these" list and can never move an item toward
    deletion. ``_has_blocked_protections`` reads the opposite way for exactly that reason.
    """
    exp = _decode_explanation(explanation_json)
    if exp is None:
        return []
    return [str(entry["gate"]) for entry in _entries(exp, "protections_fired") if entry.get("gate")]


def _has_blocked_protections(explanation_json: str) -> bool:
    """Did this row store any protection that could not be checked?

    Read from the stored explanation's ``protections_unknown`` block, the same record the
    why-panel renders amber, and parsed by ``_decode_explanation`` so a stored top level
    that is a list, a null or a number degrades instead of raising out of the simulation.

    The unreadable fallback HOLDS the row (rule 96). This answer is the only thing standing
    between a row and a score-based condemn in the loop below, so reading an explanation we
    could not parse as "nothing was blocking" would turn evidence we cannot see into
    evidence that nothing was wrong, and preview a deletion the scan would refuse. Present
    but unreadable is blocked; genuinely absent stays permissive, since a readable
    explanation with no ``protections_unknown`` (or an empty one) is a scan that looked and
    found nothing holding the item.

    ``_entries`` is deliberately not used to read the list: it drops entries that are not
    objects, which would read a malformed block as an empty one -- the permissive
    direction. Any entry at all holds, readable or not.
    """
    exp = _decode_explanation(explanation_json)
    if exp is None:
        return True
    unknown = exp.get("protections_unknown")
    if unknown is None:
        return False
    return bool(unknown) if isinstance(unknown, list) else True


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


@router.get("/vocabulary", tags=[api_tags.POLICY])
async def get_vocabulary(lane: Lane, media_type: MediaType | None = None) -> VocabularyOut:
    """The fields available in one lane, for one policy's media type.

    Filtered **server-side, before serialization**. ``?lane=condemn`` never returns a
    protect-only field, so the browser is not even shown one -- a dangerous condition is
    not merely rejected, it is unconstructable. ``&media_type=movie`` narrows it further:
    a TV-only field like "the show has ended" is not offered on a movie policy. Omitting
    ``media_type`` keeps every field, so older callers are unchanged.
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
            for spec in vocabulary(lane, media_type)
        ],
    )


#: The fields whose seen-values are worth suggesting, and the candidate column each is
#: read from. Free-text fields only: numbers and booleans need no suggestions.
_VALUE_COLUMNS = {
    "genre": Candidate.genres_json,
    "quality": Candidate.quality,
    "library": Candidate.library_title,
}


@router.get("/vocabulary/values", tags=[api_tags.POLICY])
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

    The one implementation lives with the backup service, which weighs the same files
    to size a download; the About page and the Backup panel must not drift apart.
    """
    return backup.db_size_on_disk(base)


@router.get("/about", tags=[api_tags.ABOUT])
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
