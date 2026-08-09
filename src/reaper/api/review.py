# SPDX-License-Identifier: AGPL-3.0-or-later
"""Snapshots, candidates and the why-panel: what the last scan decided, and why.

Every route here READS. Nothing in this module changes a verdict, an override or a file --
the queue's decisions are written through ``api/runs.py`` and the override routes, and a
scan is a frozen snapshot by the time anything here reads it (rule 28).

This module also owns the readers for a stored explanation, because the JSON in
``Candidate.explanation_json`` is the one thing the panel, the chips and the simulator all
parse: ``_decode_explanation`` turns it into a dict or ``None``, ``_entries`` pulls one list
out of it, and ``_replayed_evidence`` reads the stored match evidence off a row.
``api/simulate.py`` imports all three. They live here rather than there because the panel is
their first reader and the simulator is their second -- and because the two directions were
briefly split across the pair, which does not import.
"""

from __future__ import annotations

import enum
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import and_, asc, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement

from reaper.api import tags as api_tags
from reaper.api.schemas import (
    CandidateDetail,
    CandidateLinkOut,
    CandidateOut,
    ChipOut,
    Explanation,
    GroupOut,
    GroupSeasonMarkOut,
    LinksOut,
    RatingsOut,
    SeasonShapeOut,
    SnapshotOut,
    thaw_threshold,
)
from reaper.db.models import (
    Candidate,
    FirstFlagged,
    Instance,
    InstanceKind,
    PlexServer,
    Snapshot,
)
from reaper.engine import identity
from reaper.engine.explanation import read_explanation
from reaper.engine.gates import thaw_defers_to_owner
from reaper.services import (
    app_settings,
    whitelist,
)
from reaper.services.condemned import (
    MATCH_UNREADABLE,
    effective_condemned,
    match_state,
    overridden_lane_shifts,
    reap_is_effective_decoded,
    reap_override_verdict,
)
from reaper.services.deep_links import build_links
from reaper.services.display_meta import parse_ratings_json
from reaper.services.planner import MediaRef, PlanError
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


# A release year sitting at the end of a search term, with or without parentheses and with
# whatever separator someone typed between it and the title. The queue prints the year in its own
# span beside the title ("Freaky Tales 2025"), Scales prints it the same way, and the operator
# reads one string and types it back -- so the year has to be understood, not matched literally
# against a title column that never held it. The separator run ahead of the year is
# stripped in code below rather than matched here: a `[\s,·-]*` prefix made the search
# quadratic on a term that is mostly whitespace (CodeQL alert 11).
_TRAILING_YEAR = re.compile(r"(?P<year>(?:1[89]|20|21)\d{2})\)?\s*$")


def _split_search_year(term: str) -> tuple[str, int | None]:
    """Split a trailing release year off a search term.

    Returns ``(stem, year)``, where an empty stem means the term was *only* a year. The
    caller reads the three cases apart: no year is a plain text search, a year behind a
    stem narrows that stem to it, and a year alone asks for everything released then.
    """
    match = _TRAILING_YEAR.search(term)
    if match is None:
        return term, None
    stem = term[: match.start()]
    if stem.endswith("("):
        stem = stem[:-1]
    while stem and (stem[-1].isspace() or stem[-1] in ",·-"):
        stem = stem[:-1]
    return stem.strip(), int(match["year"])


def _like_literal(text_: str) -> str:
    """Make a typed search term mean itself inside a ``LIKE`` pattern.

    ``%`` and ``_`` are the two characters ``LIKE`` reserves, so an operator typing either got
    a wildcard rather than the character: ``a_pha`` found "Example Alpha", and any ``%`` in a
    term silently widened it. It only ever over-matched, which is why nobody noticed.

    The backslash goes first and cannot be reordered: escaping ``%`` before ``\\`` would turn
    the escape it just added into a literal backslash followed by a live wildcard. Callers pair
    this with ``escape="\\\\"`` on the comparison, and the ``%`` wrappers they add around the
    result are deliberately NOT escaped -- those are the wildcards doing the substring search.
    """
    return text_.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
    ``X-Total-Bytes`` response headers, so the queue can show the whole set's count and byte
    total without having loaded them all.

    Default order is by score, then by size -- so the biggest wins among the safest
    deletions come first. Size ranks the candidates the score has already chosen; it never
    decides an item's fate (see docs/SIGNALS.md). ``sort`` (score / size / year / title) and
    ``order`` (asc / desc) let the owner re-rank; a score tiebreak keeps the order stable
    within equal keys, so a show's seasons never scatter across a page boundary.

    Filters **stack** (they are ANDed), and each only narrows the frozen snapshot, never
    re-decides it: ``search`` matches the title or the show name, and understands a release
    year on the end of either ("Example Alpha 1979", or with parentheses) -- the queue prints
    the year beside the title, so it is part of what the operator reads and types back. A
    year on its own ("1979") asks for everything released that year. Either way the term is
    still tried as plain text too, so a title whose NAME ends in a year it predates, or is a
    year outright, is never lost to the reading of the number. ``media_type`` keeps
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
            term = search.strip()

            def matches(text_: str) -> ColumnElement[bool]:
                # The term is escaped to mean itself; the wrappers are the wildcards.
                pattern = f"%{_like_literal(text_)}%"
                return or_(
                    Candidate.title.ilike(pattern, escape="\\"),
                    Candidate.group_title.ilike(pattern, escape="\\"),
                )

            stem, year = _split_search_year(term)
            if year is None:
                conditions.append(matches(term))
            elif stem:
                # Either reading of the number, never one or the other: "Blade Runner 2049" is a
                # title that ends in a year it was not released in, and "Freaky Tales 2025" is a
                # title beside its year. Trying the whole string first keeps the first kind
                # findable, and the stem-plus-year arm makes the second kind work at all.
                conditions.append(or_(matches(term), and_(Candidate.year == year, matches(stem))))
            else:
                # A year on its own is the operator asking what came out that year. It is also
                # still a string, so a title *named* after a year ("1917", "2012") comes back
                # too -- both readings, the same as the arm above. Every other search is text
                # that happens to allow a year; this is the one the year is the whole of.
                conditions.append(or_(matches(term), Candidate.year == year))
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
    """The green chip's phrase for the protection that fired, worn as "Kept, {phrase}"."""
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
        # same words with and without the "Kept, " lead.
        return ChipOut(tone="kept", text=f"Kept, {phrase}", why=phrase)

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
                # Nothing in either can tell a made comparison from a refused one. So neither
                # names a shape and both fall to the vague-but-true chip below. Recovering it
                # from the wording was tried and is wrong: it read "more than watched Season"
                # as a deferral while
                # ``condemned.reap_override_verdict`` read the absent key as a hold, so the
                # card offered a conflict to settle and then refused the reap by citing that
                # same conflict back at the operator.
                defers = thaw_defers_to_owner(entry.get("defers_to_owner"))
                if defers is True:
                    return ChipOut(
                        tone="look",
                        text="Needs a look, watched more than a season your rule keeps",
                        why="watched more than a season your rule keeps",
                    )
                if defers is False:
                    return ChipOut(
                        tone="look",
                        text="Needs a look, couldn't check who watched these seasons",
                        why="Reaper couldn't check who watched these seasons",
                    )
            return ChipOut(
                tone="look",
                text="Needs a look, left for you to decide",
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
        # "Example Show, Season 3") -- that is the page the rating describes.
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
