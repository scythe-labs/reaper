# SPDX-License-Identifier: AGPL-3.0-or-later
"""Snapshots, candidates and the why-panel: what the last scan decided, and why.

Every route here reads. Nothing in this module changes a verdict, an override or a file.
The queue's decisions are written through ``api/runs.py`` and the override routes. A scan
is a frozen snapshot by the time anything here reads it.

This module also owns the readers for a stored explanation, because the JSON in
``Candidate.explanation_json`` is the one thing the panel, the chips and the simulator all
parse: ``_decode_explanation`` turns it into a dict or ``None``, ``_entries`` pulls one list
out of it, and ``_replayed_evidence`` reads the stored match evidence off a row.
``api/simulate.py`` imports all three. They live here because the panel reads them first
and the simulator reads them second.
"""

from __future__ import annotations

import enum
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import structlog
from fastapi import APIRouter, Query, Request
from sqlalchemy import and_, asc, case, desc, func, null, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement

from reaper.api import tags as api_tags
from reaper.api.deps import newest_snapshot, session_factory
from reaper.api.errors import refuse
from reaper.api.schemas import (
    CandidateDetail,
    CandidateOut,
    CandidatePageOut,
    ChipOut,
    Explanation,
    GroupOut,
    GroupRollupOut,
    GroupSeasonMarkOut,
    LinksOut,
    RatingsOut,
    SeasonShapeOut,
    SnapshotOut,
    thaw_threshold,
)
from reaper.db import KEY_CHUNK
from reaper.db.models import (
    Candidate,
    FirstFlagged,
    Instance,
    InstanceKind,
    PlexServer,
    Snapshot,
)
from reaper.engine import identity
from reaper.engine.explanation import absorb_legacy_detail, read_explanation, thaw_reason_key
from reaper.engine.gates import GateId, thaw_defers_to_owner
from reaper.engine.reason import Reason, from_wire, to_wire
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

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api")


def _replayed_evidence(row: Candidate) -> dict[str, Any]:
    """The evidence kwargs ``judge_facts`` needs, read back off the row's frozen explanation,
    so a replay rebuilds what the scan stored instead of re-deriving or dropping it.

    Two things come from one read of the stored JSON: the Plex match block, and whether the
    scan found this title's recorded plays unreadable (``watch_blind``). Both are evidence,
    not policy, so a policy edit changes neither.

    Which Plex row an item was bound to is evidence too. A policy edit cannot change that
    binding, so the replay must carry the stored answer instead of re-deriving it or leaving
    it blank. A blank match block reads as "nothing wrong with the match", which would loosen
    the one interlock that still holds a hand reap for a row Reaper cannot identify. An
    omitted match block and an explicit empty one mean different things, and this must not
    blur them.

    A row that cannot be parsed still returns a value instead of raising: an unreadable or
    unrecognized match status becomes ``UNMATCHED``, the conservative answer that holds the
    reap, matching what ``condemned.match_state`` does with the same row, where anything it
    cannot parse also becomes ``MATCH_UNREADABLE`` and holds. A row frozen before the match
    block existed carries no key at all and is left alone. ``None`` there means genuinely
    absent, since those rows were judged without it.
    """
    try:
        stored = json.loads(row.explanation_json or "{}")
        match = stored.get("match") if isinstance(stored, dict) else None
        # Read this before checking the match block below: a missing or malformed match
        # block can still sit beside a good reading, and dropping the flag along with the
        # match would remove a working protection. Anything that is not a bool becomes
        # None, never False.
        raw_blind = stored.get("watch_blind") if isinstance(stored, dict) else None
    except (ValueError, TypeError):
        match = None
        raw_blind = None
    blind: dict[str, Any] = {"watch_blind": raw_blind if isinstance(raw_blind, bool) else None}
    if match is None:
        return blind
    if not isinstance(match, dict):
        # Present but unreadable: the shape `condemned.MATCH_UNREADABLE` exists for this.
        # Report the conservative status rather than "no match block recorded".
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
        # Carried so a replay rebuilds the same match block the scan stored, instead of one
        # whose links quietly went missing. This is display only, so an unreadable value is
        # simply empty: there is no conservative choice for "which rows to offer".
        "match_candidates": tuple(k for k in candidates if isinstance(k, int))
        if isinstance(candidates, list)
        else (),
        **blind,
    }


@router.get("/snapshots/latest", tags=[api_tags.SCANS])
async def latest_snapshot(request: Request) -> SnapshotOut:
    async with session_factory(request)() as session:
        snapshot = await newest_snapshot(session)
        if snapshot is None:
            refuse(404, "error.review.no_scan")
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
    # Move each overridden item from its policy lane to its effective lane, so the headline
    # counts match the tabs and the reap ledger (condemned.effective_*). Only overridden rows
    # move, so this is the group-by above plus a handful of changes.
    for _candidate, from_lane, to_lane in await overridden_lane_shifts(
        session, snapshot.id, decisions
    ):
        counts[from_lane] = counts.get(from_lane, 0) - 1
        counts[to_lane] = counts.get(to_lane, 0) + 1

    # Reclaimable bytes are summed over the effective condemned set, the exact rows the planner
    # will act on, so a spared condemnation stops counting and a honored hand reap starts. A
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
        degraded_doc=snapshot.degraded_doc,
        condemned=int(counts.get("condemn", 0)),
        protected=int(counts.get("protect", 0)),
        abstained=int(counts.get("abstain", 0)),
        reclaimable_bytes=int(reclaimable),
        unknown_size_items=int(unknown_size),
        collection_sizes=_collection_sizes(snapshot.collection_sizes_json),
    )


@router.get("/snapshot/season-shape", tags=[api_tags.POLICY])
async def season_shape(request: Request) -> SeasonShapeOut:
    """The distribution of content-season counts across shows, for the keep-last advisory.

    A show's season count is how many season candidate rows it has in the latest snapshot.
    The editor uses this to compute, entirely on the client, how many shows have no season
    that a given keep-last-N value would leave removable. It updates live as the value
    changes, with no scan and no dependency on the current keep-last value.
    """
    async with session_factory(request)() as session:
        snapshot = await newest_snapshot(session)
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


# A release year at the end of a search term, with or without parentheses, and with whatever
# separator someone typed between it and the title. The queue prints the year in its own span
# beside the title ("Freaky Tales 2025"), Scales prints it the same way, and the operator reads
# one string and types it back, so the year must be understood, not matched literally against
# a title column that never held it. The separator before the year is stripped in code below
# instead of matched here: a `[\s,·-]*` prefix made the search run in quadratic time on a term
# that is mostly whitespace.
_TRAILING_YEAR = re.compile(r"(?P<year>(?:1[89]|20|21)\d{2})\)?\s*$")


def _split_search_year(term: str) -> tuple[str, int | None]:
    """Split a trailing release year off a search term.

    Returns ``(stem, year)``. An empty stem means the term was *only* a year. No year found
    means a plain text search. A year behind a stem narrows that stem to it. A year alone
    asks for everything released that year.
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

    ``LIKE`` treats ``%`` and ``_`` as wildcards, so a term containing either must escape
    them or it matches more than what the operator typed.

    Escape the backslash first. Escaping ``%`` before ``\\`` would turn the escape it just
    added into a literal backslash followed by a live wildcard. Callers pair this with
    ``escape="\\\\"`` on the comparison. The ``%`` wrappers callers add around the result
    stay unescaped on purpose: those are the wildcards doing the substring search.
    """
    return text_.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _safe_json_array(column: str) -> str:
    """SQL for the stored JSON-array column, guarded to a literal ``'[]'`` for a NULL or
    invalid document. ``json_each`` raises mid-query on either, so every predicate that
    walks ``genres_json`` or ``collections_json`` shares this one guard instead of copying
    it. ``column`` is never operator input. It is always one of the two literal names those
    predicates pass, so nothing untrusted reaches this string.
    """
    return (
        f"CASE WHEN candidate.{column} IS NOT NULL "
        f"AND json_valid(candidate.{column}) "
        f"THEN candidate.{column} ELSE '[]' END"
    )


def _json_array_term(column: str, term: str) -> ColumnElement[bool]:
    """EXISTS test for ``term`` inside a stored JSON-array column, exactly.

    Shared by every equality filter over one of these columns (``genres_json``,
    ``collections_json``), so a second copy cannot drift from this one.
    """
    return cast(
        "ColumnElement[bool]",
        text(
            f"EXISTS (SELECT 1 FROM json_each({_safe_json_array(column)}) "  # noqa: S608
            f"WHERE json_each.value = :term)"
        ).bindparams(term=term),
    )


def _json_array_like(column: str, pattern: str) -> ColumnElement[bool]:
    """EXISTS test for a **partial** match (SQL ``LIKE``) inside a stored JSON-array column.

    The partial sibling of ``_json_array_term`` above, sharing its NULL/malformed guard so
    the two cannot drift on that. Used only for the collection-name part of search. Genre
    stays an exact filter. Nothing else needs a substring test over a JSON array. ``pattern``
    is caller-escaped with ``_like_literal`` the same way the title search wraps it, so a
    typed ``%`` or ``_`` means itself.
    """
    return cast(
        "ColumnElement[bool]",
        text(
            f"EXISTS (SELECT 1 FROM json_each({_safe_json_array(column)}) "  # noqa: S608
            f"WHERE json_each.value LIKE :pattern ESCAPE '\\')"
        ).bindparams(pattern=pattern),
    )


def _json_array_first_like(column: str, pattern: str) -> ColumnElement[str]:
    """The first stored value in a JSON-array column matching ``pattern`` (``LIKE``), in the
    array's own stored order (the same order ``collections_json`` is written smallest-first).
    Shares the NULL/malformed guard above. ``NULL`` when nothing matches.

    Says which collection answered a search row that matched by collection name only. The
    chip's usual element 0 would show the operator's smallest collection instead of the one
    that matched, an unrelated name on a row they cannot otherwise explain. This is the one
    exception to "the chip takes element 0".
    """
    return cast(
        "ColumnElement[str]",
        text(
            f"(SELECT json_each.value FROM json_each({_safe_json_array(column)}) "  # noqa: S608
            f"WHERE json_each.value LIKE :pattern ESCAPE '\\' "
            f"ORDER BY json_each.key LIMIT 1)"
        ).bindparams(pattern=pattern),
    )


@router.get("/candidates", tags=[api_tags.REVIEW])
async def list_candidates(
    request: Request,
    verdict: str = "condemn",
    search: str | None = None,
    media_type: str | None = None,
    requested: str = "any",
    genre: str | None = None,
    collection: str | None = None,
    library: str | None = None,
    override: str = "any",
    sort: str = "score",
    order: str = "desc",
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> CandidatePageOut:
    """One page of the review queue.

    The list is paged: a library can hold thousands of protected titles, so returning them
    all in one payload hid the tail. The client fetches ``limit`` rows at ``offset`` and asks
    for the next page as it scrolls. The envelope also carries the filtered set's full size
    (a count, a byte total, and how many rows have no size, all before the page window), so
    the queue can show the whole set's count and byte total without loading every row.

    The default order is by score, then by size, so the biggest wins among the safest
    deletions come first. Size only ranks candidates the score has already chosen. It never
    decides an item's fate (see docs/SIGNALS.md). ``sort`` (score, size, year, or title) and
    ``order`` (asc or desc) let the owner re-rank. A score tiebreak keeps the order stable
    within equal keys, so a show's seasons never scatter across a page boundary.

    ``verdict`` selects the lane: ``condemn``, ``protect``, or ``abstain``. ``any`` returns
    every stored lane at once, which is what makes the collection screen show every fate a
    title's siblings got. Hand overrides can only move an item into or out of one named lane,
    so ``any`` skips that step: nothing is excluded from a lane, so there is nothing to move.

    Filters stack (they are ANDed), and each one only narrows the frozen snapshot. None of
    them re-decides it. ``search`` matches the title or the show name, and reads a release
    year at the end of either ("Example Alpha 1979", with or without parentheses), since the
    queue prints the year beside the title and the operator types it back. A year on its own
    ("1979") asks for everything released that year. Either way, the term is also tried as
    plain text, so a title whose name ends in a year it predates, or that is a year outright,
    is never lost to the year reading. ``search`` also matches a Plex collection name
    partially, so typing a franchise finds its members. This is navigation only, like
    ``collection`` below, and never re-decides a verdict. Each row carries a ``search_rank``
    (0 exact title, 1 partial title or show name, 2 collection name only), so the client can
    show titles before collections without imposing a relevance order within either group. A
    collection-only row also carries ``matched_collection``, the name that actually matched,
    since the chip's usual smallest-first choice would otherwise show an unrelated name.

    ``media_type`` keeps movies or seasons. ``requested`` keeps only titles someone asked for
    through Seerr (``yes``), only titles nobody asked for (``no``), or everything (``any``).
    ``genre`` keeps rows whose stored genre list contains the given term exactly.
    ``collection`` does the same over the stored Plex collection list. Like ``search``, this
    is navigation only and never re-decides a verdict. ``library`` keeps rows in the named
    Plex library (section). ``override`` keeps rows by their hand-override state (``spare``,
    ``reap``, ``none``, or ``any``).
    """
    async with session_factory(request)() as session:
        snapshot = await newest_snapshot(session)
        if snapshot is None:
            return CandidatePageOut(
                items=[],
                groups=[],
                total=0,
                total_bytes=0,
                unknown_size=0,
                offset=offset,
                snapshot_id=None,
            )

        # The filters, built once and applied to BOTH the count and the page, so the envelope's
        # totals describe exactly the set the rows are drawn from.
        decisions = await whitelist.overrides(session)
        conditions = [Candidate.snapshot_id == snapshot.id]
        if verdict != "any":
            # Tab membership is the effective lane, not the raw verdict. A hand override moves
            # an item to the lane of what will actually happen: a spared condemnation shows
            # under Kept, and a honored hand reap under Condemned, while its stored verdict
            # stays pure policy. Only overridden rows move, so raw verdict == lane stays the
            # indexed path and the few moves are spliced on. condemned.effective_verdict is
            # the one classifier the scan summary shares. ``any`` (the collection screen)
            # wants every lane, so this whole step is skipped: nothing is excluded, so
            # nothing moves.
            shifts = await overridden_lane_shifts(session, snapshot.id, decisions)
            moved_out = [c.media_key for c, from_lane, _to in shifts if from_lane == verdict]
            moved_in = [c.media_key for c, _from, to_lane in shifts if to_lane == verdict]
            lane = Candidate.verdict == verdict
            if moved_out:
                lane = and_(lane, Candidate.media_key.not_in(moved_out))
            if moved_in:
                lane = or_(lane, Candidate.media_key.in_(moved_in))
            conditions.append(lane)
        search_rank: ColumnElement[int] | None = None
        matched_collection: ColumnElement[str] | None = None
        if search and search.strip():
            term = search.strip()

            def matches(text_: str) -> ColumnElement[bool]:
                # The term is escaped to mean itself. The wrappers are the wildcards.
                pattern = f"%{_like_literal(text_)}%"
                return or_(
                    Candidate.title.ilike(pattern, escape="\\"),
                    Candidate.group_title.ilike(pattern, escape="\\"),
                )

            def exact(text_: str) -> ColumnElement[bool]:
                # Whole-string equality, not a LIKE. Block 0 is "typed the exact title".
                # `matches` above already covers "typed something inside it" (block 1).
                return or_(
                    func.lower(Candidate.title) == text_.lower(),
                    func.lower(Candidate.group_title) == text_.lower(),
                )

            def collection_pattern(text_: str) -> str:
                return f"%{_like_literal(text_)}%"

            stem, year = _split_search_year(term)
            if year is None:
                title_hit = matches(term)
                exact_hit = exact(term)
                collection_terms = [term]
            elif stem:
                # Try both readings of the number. "Blade Runner 2049" is a title that ends
                # in a year it was not released in. "Freaky Tales 2025" is a title beside its
                # own year. Trying the whole string first keeps the first kind findable, and
                # the stem-plus-year arm makes the second kind work at all.
                title_hit = or_(matches(term), and_(Candidate.year == year, matches(stem)))
                exact_hit = or_(exact(term), and_(Candidate.year == year, exact(stem)))
                collection_terms = [term, stem]
            else:
                # A year on its own asks what came out that year. It is also still a string,
                # so a title named after a year ("1917", "2012") comes back too, the same two
                # readings as the arm above. Every other search is text that happens to allow
                # a year. Here the year is the whole term. A bare year never counts as an
                # exact title match.
                title_hit = or_(matches(term), Candidate.year == year)
                exact_hit = exact(term)
                collection_terms = [term]

            # A collection name matched partially, so typing a franchise finds its members. It
            # joins the same OR the title reading uses, so a title-only match and a
            # collection-only match land on the same page. Collections stay navigation:
            # nothing here re-decides a verdict.
            collection_hit = or_(
                *(
                    _json_array_like("collections_json", collection_pattern(t))
                    for t in collection_terms
                )
            )
            conditions.append(or_(title_hit, collection_hit))

            # The queue has no relevance order of its own: search filters, and the operator's
            # chosen sort orders what is left. "Titles first, collections after" is a second
            # ordering on top of that one, and the two cannot both apply to the same sort. So
            # the server reports which of three blocks a row landed in (0 exact title, 1
            # partial title or show, 2 collection name only), and the client sorts within
            # each block by the operator's own order, with a labeled divider where block 2
            # starts.
            search_rank = case((exact_hit, 0), (title_hit, 1), else_=2)
            # Which collection matched, for a row that matched by collection only. Not the
            # chip's usual smallest-first element 0, which would put an unrelated name on a
            # row the operator cannot otherwise explain. NULL for a row that matched by title.
            # `coalesce` needs 2 or more arguments in SQLite. The trailing NULL is a no-op
            # when there are already two terms, and the only way to call it at all with one.
            matched_collection = func.coalesce(
                *(
                    _json_array_first_like("collections_json", collection_pattern(t))
                    for t in collection_terms
                ),
                null(),
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
            # Exact term match inside the stored JSON genre array.
            conditions.append(_json_array_term("genres_json", genre.strip()))
        if collection and collection.strip():
            # Exact name match inside the stored JSON collection array. The same predicate
            # as genre above, over collections_json instead.
            conditions.append(_json_array_term("collections_json", collection.strip()))
        if override in {"spare", "reap", "none"}:
            # Hand overrides resolve in Python: whitelist.effective_override is the one
            # decision function (an item's own key beats its show's), so re-stating that
            # precedence in SQL would let the two drift. Totals below use the same final
            # conditions, so count, bytes and page describe one set.
            #
            # Only the rows that could possibly carry a decision are resolved, not every row
            # in the lane. An item's effective override is non-null exactly when its own key
            # is in the whitelist or its show's key is, so a row matching neither is "none"
            # without asking. That set is bounded by how many overrides the operator has
            # made. Resolving every row in the lane instead would bind nearly all of a large
            # library into one IN clause and risk exceeding SQLite's bound-variable ceiling.
            #
            # `group_key` is the show's key for a season row and null for a movie, the same
            # value whitelist.show_key derives from a season's media_key (both are
            # `sonarr:{instance}:{series}`, pinned by a test). It is used only to select
            # which rows are worth resolving. `effective_override` still makes the decision.
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

        # SUM skips NULL rows without saying so, and COALESCE cannot tell that from a real
        # zero. So the unmeasured count is taken in the same query under the same conditions,
        # and the envelope carries it beside the total.
        totals = (
            await session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(Candidate.size_bytes), 0),
                    func.count().filter(Candidate.size_bytes.is_(None)),
                ).where(*conditions)
            )
        ).one()

        direction = asc if order == "asc" else desc
        sort_columns = {
            "score": Candidate.score,
            "size": Candidate.size_bytes,
            "year": Candidate.year,
            "title": func.lower(func.coalesce(Candidate.group_title, Candidate.title)),
        }
        primary = direction(sort_columns.get(sort, Candidate.score))
        # Sorting by size puts the unmeasured items last in both directions. SQLite orders
        # NULL first on ASC, so "smallest first" would otherwise open the list with every
        # item Reaper could not measure, the ones it has the least to say about. This is
        # written as an explicit is-null key instead of nullslast(), since not every backend
        # renders that the same way. It only applies when size is the chosen key: a title or
        # year sort is what the owner asked for, alphabetical or chronological, not a size
        # grouping.
        sort_keys = [primary] if sort != "size" else [Candidate.size_bytes.is_(None), primary]
        # A search's block order (0/1/2) sorts above the operator's chosen key, and the two
        # never mix: within a block, the owner's sort still holds. With no search, there is
        # no block and this prefix is empty.
        rank_prefix = [search_rank] if search_rank is not None else []
        # A score/size tiebreak after the chosen key keeps ordering deterministic, so a
        # show's seasons stay adjacent and paging never splits or shuffles the list.
        stmt = (
            select(
                Candidate,
                search_rank if search_rank is not None else null(),
                matched_collection if matched_collection is not None else null(),
            )
            .where(*conditions)
            .order_by(*rank_prefix, *sort_keys, Candidate.score.desc(), Candidate.size_bytes.desc())
            # limit/offset are validated at the boundary (Query ge/le above), so a negative
            # limit, which SQLite reads as "no limit", can never reach here.
            .limit(limit)
            .offset(offset)
        )

        page = (await session.execute(stmt)).all()
        rows = [p[0] for p in page]
        # media_key is already relied on as a unique per-snapshot key elsewhere on this page
        # (the `flagged` map below). block and matched-collection are display-only extras
        # riding the same query, read back the same way.
        ranks = {p[0].media_key: p[1] for p in page if p[1] is not None}
        # Only a collection-only row carries a matched collection. A title match's own
        # collections can independently contain the term too, and that must not leak a name
        # onto a row that did not need one to find it.
        matched_names = {p[0].media_key: p[2] for p in page if p[2] is not None and p[1] == 2}

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
        group_keys = {r.group_key for r in rows if r.group_key}
        group_totals, group_marks = await _group_rollups(
            session, snapshot.id, group_keys, decisions, expiries
        )

        return CandidatePageOut(
            items=[
                _candidate_out(
                    r,
                    flagged.get(r.media_key),
                    decisions,
                    expiries=expiries,
                    search_rank=ranks.get(r.media_key),
                    matched_collection=matched_names.get(r.media_key),
                )
                for r in rows
            ],
            # One entry per show on the page, in place of the same four values stamped onto
            # each of its season rows. Sorted so a page's shape does not depend on set order.
            groups=[
                GroupRollupOut(
                    group_key=key,
                    condemned_count=group_totals[key][0],
                    condemned_bytes=group_totals[key][1],
                    unknown_size=group_totals[key][2],
                    seasons=group_marks[key],
                )
                for key in sorted(group_keys)
            ],
            total=int(totals[0]),
            total_bytes=int(totals[1]),
            unknown_size=int(totals[2]),
            offset=offset,
            # Which snapshot this page was drawn from. The queue compares it against the newest
            # completed scan (from the polled status) to notice when a scan has landed a fresher
            # snapshot underneath an open review, without re-deciding anything here.
            snapshot_id=snapshot.id,
        )


def _add_member(total: tuple[int, int, int], size_bytes: int | None) -> tuple[int, int, int]:
    """Add one actable season to a show's (count, bytes, unknown) rollup.

    A season with no size raises only the unknown count. The item count must match the
    number of steps the planner will actually emit, and the planner skips a season with no
    size, so this excludes it too.
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

    Totals: what "Reap now" on each show group would actually plan. That is
    (count, bytes, unknown) over its actable member seasons across the whole snapshot:
    condemned minus hand-spares, plus hand-reaped seasons the engine honors
    (services.condemned). The show card's numbers must match the planner's expansion, since
    ``build_plan`` expands a group key over the same effective set, so the number beside a
    destructive button is derived from the set the server will act on. It is never derived
    from the fetched page, which on a long sorted list can hold only some of a show's
    seasons.

    The count is filtered too, not only the bytes. A season with no size is held back by
    the planner, so counting it here would put "Reap now (8 items)" beside a plan that
    emits six. It is reported separately as ``unknown``, so the card can say what it is
    leaving out instead of quietly shrinking.

    Marks: the show card's season strip. Every member's season number, verdict, override,
    whether a hand reap actually takes, and when a hand spare stops keeping it, across all
    lanes, sorted by season number (unnumbered rows last). The expiry rides along because
    the strip square colors by the item's fate, and an expired spare is a fate of its own:
    it still keeps the file, but is no longer a live decision. Without it the square could
    only draw the solid "you chose this" green, which an expired spare is not.

    ``IN`` is chunked at ``KEY_CHUNK`` per the bound-variable limit.
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
    for start in range(0, len(keys), KEY_CHUNK):
        chunk = keys[start : start + KEY_CHUNK]
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
            # The spare in effect on this season, matching `override` above: its own if it
            # has one, else its show's. Read only alongside a "spare" decision, exactly as
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
    for start in range(0, len(pending_keys), KEY_CHUNK):
        chunk = pending_keys[start : start + KEY_CHUNK]
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


async def _decided_keys(
    session: AsyncSession,
    conditions: list[ColumnElement[bool]],
    decisions: dict[str, str],
) -> list[str]:
    """The media_keys under ``conditions`` that carry a hand decision, own or inherited.

    The candidate set is narrowed in SQL to rows whose own key, or whose show key, appears
    in the whitelist, since nothing else can have an effective override. The real
    :func:`whitelist.effective_override` then decides each one. So the expensive part scales
    with the operator's overrides rather than with the size of their library, and the
    precedence between a season's decision and its show's still lives in exactly one
    function.
    """
    if not decisions:
        return []
    keys = sorted(decisions)
    found: list[str] = []
    for start in range(0, len(keys), KEY_CHUNK):
        chunk = keys[start : start + KEY_CHUNK]
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
    # twice. Dedupe so the IN below carries each key once.
    return list(dict.fromkeys(found))


class _PanelExplanation(NamedTuple):
    """The why-panel's explanation block plus whether it had to be invented."""

    body: Explanation
    unreadable: bool


def _explanation_out(row: Candidate) -> _PanelExplanation:
    """The why-panel's explanation, degrading instead of erroring the panel.

    Every sibling display extractor here treats an unreadable stored explanation as nothing
    to show. A row that will not decode, or will not validate against the current schema,
    falls back to what the row itself still knows, its score and how much of it could be
    checked, and says so.

    The fallback leaves ``threshold`` unset rather than filling in a number. The panel
    prints "your threshold is N" beside the score, and an invented N would state a policy
    setting that is not the operator's. Left unset, the panel omits that clause.

    The read goes through ``engine.explanation.read_explanation`` rather than a ``try``
    around the model here, because ``services.condemned`` must refuse a hand reap on
    exactly the rows this degrades, and it cannot reach into this layer to find out which
    they are.
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

    ``_candidate_out`` decodes each row here once and hands the result to
    ``_dormant_days``, ``_primary_reason``, ``_chip`` and the reap-override read, instead of
    each running its own ``json.loads`` over the same multi-KB document.

    Returns ``None`` for anything that is not a JSON object, so a corrupted or hand-edited
    row degrades to a card with no reason line instead of failing the whole review queue.
    Each extractor re-checks that it was handed a dict, so calling one directly is exactly
    as defensive as calling it through here.
    """
    try:
        exp = json.loads(explanation_json)
    except (ValueError, TypeError):
        return None
    return exp if isinstance(exp, dict) else None


def _entries(exp: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """The dict entries under ``key``, dropping anything that is not one.

    Every protection or signal list in an explanation is a list of objects. A stored row
    that says otherwise loses the malformed entries instead of raising out of a display
    extractor: ``exp["protections_fired"][0]["detail"]`` on a string entry raises a
    TypeError, and one such row would blank the entire queue.
    """
    value = exp.get(key)
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def _match_status(exp: dict[str, Any]) -> str | None:
    """The Plex match state, straight from the one function that derives it.

    Kept as a thin alias so the queue's chips and card reasons cannot drift from the
    reap-override decision that reads the same block. An unreadable match holds a reap
    (:data:`~reaper.services.condemned.MATCH_UNREADABLE`), and the copy beside it must say
    so instead of falling through to "Scored below your threshold".
    """
    return match_state(exp)


def _detail_reason(entry: dict[str, Any]) -> Reason | None:
    """One stored row's typed detail, however old the row is.

    A fresh row carries ``detail_key`` and comes back as the reason the engine wrote. A row
    frozen before details were typed carries prose ``detail`` and no ``detail_key``.
    ``absorb_legacy_detail`` converts it into a legacy reason before this reads it, so both
    ages take the same code path downstream. This is the same conversion
    ``engine.explanation``'s models apply on their own read, for a reader that takes the
    stored row as a raw dict and never builds those models at all. Returns ``None`` when
    the row carries neither.

    Whether the resulting ``detail_key`` is legible is ``thaw_reason_key``'s call, the same
    one the panel's models make. A malformed dict here must degrade exactly as it does
    there, to ``None``, never through ``from_wire``'s wrap-anything arm, which would print
    the dict's raw contents at the operator."""
    folded = absorb_legacy_detail(entry)
    key = folded.get("detail_key") if isinstance(folded, dict) else None
    if isinstance(key, dict) and thaw_reason_key(key) is not None:
        return from_wire(key)
    return None


def _is_hand_spare(reason: Reason | None) -> bool:
    """Whether this fired row is the added hand spare.

    A fresh row carries the typed id. A row frozen before reasons were typed carries the
    sentence as a ``legacy`` reason instead, and is not matched here. It falls through to
    the same generic ``kept.whitelisted`` chip any other whitelisted keep takes, which is
    still true, just not as specific."""
    return reason is not None and reason.id == "hand_spare"


def _contribution(entry: dict[str, Any]) -> float:
    """One signal entry's contribution, as a number safe to sort on.

    ``_entries`` guarantees the entry is a dict, but promises nothing about the values
    inside it. Two stored entries whose contributions are a number and a string make
    ``list.sort`` raise a TypeError comparing them, which would blank the whole condemned
    list over one hand-edited row. Anything that is not a number reads as 0.0 and sorts
    last, so a readable signal is always preferred as the card's reason.
    """
    value = entry.get("contribution")
    return float(value) if isinstance(value, int | float) else 0.0


def _primary_reason(
    exp: dict[str, Any] | None, verdict: str, score: int, media_type: str = "movie"
) -> Reason | None:
    """The single line the card shows: why Reaper judged this, not what it is about.

    A spared item leads with the protection that saved it. A reaped one leads with its
    strongest reason. An abstained one leads with what stopped it short. All of these are
    already plain English in the stored explanation. This only picks which one to surface.

    Takes the decoded explanation (``_decode_explanation``), and like every sibling
    extractor treats an unreadable one as "no reason to show", never as an error. Display
    extraction must never drop a row off the queue.

    ``score`` comes from the row, not from ``exp``, so this reads the same number ``_chip``
    does. The stored explanation carries its own copy of the score, and two readers picking
    different sources is how the chip and the line beneath it could come to disagree.
    """
    if not isinstance(exp, dict):
        return None

    if verdict == "protect":
        fired = _entries(exp, "protections_fired")
        if fired:
            return _detail_reason(fired[0])
        # A protect with nothing fired is a hand reap the engine refused to honor: the item
        # was blocked (e.g. the season keep-rule conflict), so a "reap" override resolves to
        # protect. Surface that blocked reason so the held row says WHY, not a generic line.
        unknown = _entries(exp, "protections_unknown")
        return _detail_reason(unknown[0]) if unknown else None
    if verdict == "condemn":
        signals = [s for s in _entries(exp, "signals") if s.get("evaluated")]
        signals.sort(key=_contribution, reverse=True)
        return _detail_reason(signals[0]) if signals else None
    # abstain: lead with the match problem when there is one. It is the single cause
    # behind every "could not check" that follows, and the raw gate detail ("could not
    # check the watch horizon: ...") repeats it in technical terms. Otherwise fall back
    # to the first unchecked protection, whose detail is already a plain sentence.
    status = _match_status(exp)
    if status == "unmatched":
        return Reason("kept_safe.unmatched")
    if status == "ambiguous":
        return Reason("kept_safe.ambiguous")
    if status == "conflicted":
        return Reason("kept_safe.conflicted", {"media": media_type})
    if status == MATCH_UNREADABLE:
        # The row records a match Reaper cannot read. That holds a hand reap, so this must
        # return here rather than fall through to the below-threshold line, which would
        # state the opposite of the decision in force on this row.
        return Reason("kept_safe.match_unreadable")
    unknown = _entries(exp, "protections_unknown")
    if unknown:
        return _detail_reason(unknown[0])
    # An abstain that got this far was stopped by the score or by the coverage floor. These
    # are different decisions with different remedies: one says move the slider, the other
    # says fix the evidence source. ``decide_verdict``'s order settles which: past every
    # blocked case, an abstain at or above the threshold can only be the coverage floor.
    # This is the same branch ``_chip`` makes below, and the two must agree, or the chip
    # and the line beneath it would name different causes for the same row.
    threshold = thaw_threshold(exp.get("threshold"))
    if threshold is not None and score >= threshold:
        return Reason("kept_safe.coverage")
    return Reason("below_threshold")


def _dormant_days(exp: dict[str, Any] | None) -> float | None:
    """The card's amber dormancy pill: the raw day count off a fresh row's typed detail.

    Read from the stored explanation's unwatched signal. A fresh row's detail key carries
    the day count (``signal_unwatched``), and the frontend composes the humanized span in
    the active locale. A row frozen before details were typed carried the span inside its
    own prose sentence instead, and that string is not parsed back out of it: a legacy
    unwatched signal degrades to ``None`` and hides the pill, the same as an unevaluated
    signal, a missing block, or an unrecognized shape. Same defensive posture as
    ``_primary_reason``: display extraction must never fail a row off the queue.
    """
    if not isinstance(exp, dict):
        return None
    for signal in _entries(exp, "signals"):
        if signal.get("id") != "unwatched":
            continue
        if not signal.get("evaluated"):
            return None
        reason = _detail_reason(signal)
        if reason is not None and reason.id == "signal_unwatched":
            days = reason.params.get("days")
            return float(days) if isinstance(days, int | float) else None
        return None
    return None


#: The chip id per fresh season-keep reason id (``season_pruning._protection_reason``).
#: ``cause.progress_history_short`` is not a keep rule and must not fall to the generic id.
#: The lever here is the depth of the watch history, and naming a season rule would send
#: the operator to edit a control that will not move it. Its three siblings keep the
#: generic id.
_KEPT_SEASON_IDS: dict[str, str] = {
    "season_keep.specials": "kept.season.specials",
    "season_keep.incomplete": "kept.season.incomplete",
    "season_keep.airing": "kept.season.airing",
    "season_keep.first": "kept.season.first",
    "season_keep.keep_all": "kept.season.keep_all",
    "season_keep.midbinge": "kept.season.midbinge",
    "cause.progress_history_short": "kept.season.progress_history_short",
}

#: Every id ``_chip``/``_kept_reason``/``_came_back_chip`` can emit, under the ``chip``
#: catalog namespace (``chip.text.<id>`` / ``chip.sentence.<id>``). Hand-maintained and
#: reconciled against the catalog by the two-way walk in ``test_review_chips.py``. Chip
#: ids are inline literals across many branches here, not each a named module constant, so
#: this list is scanned by hand rather than discovered from the code the way ``*_REASON``
#: constants are.
_CHIP_IDS: frozenset[str] = frozenset(
    {
        "kept.hand_spare",
        "kept.whitelisted",
        "kept.streaming_now",
        "kept.rating",
        "kept.rating_plain",
        "kept.popularity",
        "kept.popularity_plain",
        "kept.curated_list",
        "kept.no_history",
        "kept.dormancy",
        "kept.season.keep_last",
        "kept.season.rule",
        "kept.returned",
        "kept.custom",
        "kept.unknown",
        "came_back",
        "came_back_unknown",
        "match.unmatched",
        "match.ambiguous",
        "match.conflicted",
        "match.unreadable",
        "look.comparable",
        "look.unknowable",
        "look.unsettled",
        "unknown_checks",
        "coverage",
        "below_threshold",
        "below",
    }
) | frozenset(_KEPT_SEASON_IDS.values())


def _kept_season_reason(reason: Reason) -> Reason:
    """The chip's typed reason for a fresh season row, keyed on the reason id."""
    if reason.id == "season_keep.keep_last":
        keep_last = reason.params.get("keep_last")
        if isinstance(keep_last, int | float):
            return Reason("kept.season.keep_last", {"keep_last": int(keep_last)})
    chip_id = _KEPT_SEASON_IDS.get(reason.id)
    return Reason(chip_id) if chip_id is not None else Reason("kept.season.rule")


def _kept_reason(gate: str, reason: Reason | None) -> Reason:
    """The green chip's typed reason for the protection that fired.

    A fresh row's numbers come off the reason's params. A legacy row's id is ``"legacy"``,
    which matches none of the ids or gates checked below, so it falls through to whichever
    static id its gate returns, the same generic-but-true fallback every unrecognized shape
    takes here. The why panel beneath the chip still shows the row's actual stored sentence,
    verbatim, through ``detail_key``: display extraction never invents a sentence, it only
    degrades to a plainer one.
    """
    if _is_hand_spare(reason):
        return Reason("kept.hand_spare")
    if gate == "whitelisted":
        return Reason("kept.whitelisted")
    if gate == "streaming_now":
        return Reason("kept.streaming_now")
    if gate == "rating_floor":
        if reason is not None and reason.id == "rating_cleared":
            clauses = reason.params.get("clauses")
            if isinstance(clauses, tuple):
                for clause in clauses:
                    value = clause.params.get("value")
                    if clause.params.get("source") == "imdb" and isinstance(value, int | float):
                        return Reason("kept.rating", {"value": value, "source": "imdb"})
        return Reason("kept.rating_plain")
    if gate == "server_popularity":
        if reason is not None and reason.id == "popularity_watched":
            count = reason.params.get("count")
            window_days = reason.params.get("window_days")
            if isinstance(count, int | float) and isinstance(window_days, int | float):
                return Reason(
                    "kept.popularity", {"count": int(count), "window_days": int(window_days)}
                )
        return Reason("kept.popularity_plain")
    if gate == "curated_list":
        return Reason("kept.curated_list")
    if gate == "min_dormancy":
        if reason is not None and reason.id == "dormancy_unestablished":
            return Reason("kept.no_history")
        # This gate's clock runs from the last play when there is one, and otherwise from
        # the day the file arrived (``engine.dormancy.reference_instant``). So the fired
        # branch covers a title nobody has ever played, and the chip must say "untouched",
        # never "watched": a "watched too recently" chip would assert a play that never
        # happened, on a card whose own panel says "nobody watched it in the last year"
        # three lines above. ``MinDormancyGate`` words its own detail "untouched" for the
        # same reason.
        return Reason("kept.dormancy")
    if gate == "season_progression":
        return _kept_season_reason(reason) if reason is not None else Reason("kept.season.rule")
    if gate == "returned":
        # This arm is never reached: the one caller asks `_came_back_chip` first, and that
        # never returns None for a `returned` entry, so an unparseable detail costs it the
        # countdown and it still answers. This arm exists anyway because the generic
        # fallback below would let a missing member pass silently, which is what
        # `gateMeta`'s guard on the frontend is written to catch.
        return Reason("kept.returned")
    if gate == "custom":
        return Reason("kept.custom")
    return Reason("kept.unknown")


def _came_back_chip(fired: list[dict[str, Any]]) -> ChipOut | None:
    """The came-back hold's chip, or ``None`` when that protection did not fire.

    This chip wins whenever it fires, whatever else fired too, and it is the only
    protection that does. Every other protection on the list is re-decided from scratch at
    the next scan, so "why is this kept" is answered by conditions the operator can go look
    at. This one is a countdown against a date they cannot see, on evidence from a scan that
    may be a year old. Left unstated, the honest question is not "why" but "is it stuck
    forever". So the chip says how much time is left, without needing anything opened.

    A chip goes only to a protection with an expiry, so the next one added here is not
    argued from scratch. "Someone is watching it right now" and "well rated" have nothing
    to count down, so a chip for them would add noise and no information.

    A hand spare still wins, because that is the owner's decision and it carries its own
    countdown already (``OverrideChip``). The caller checks it before asking here.
    """
    entry = next((e for e in fired if str(e.get("gate") or "") == GateId.RETURNED.value), None)
    if entry is None:
        return None
    reason = _detail_reason(entry)
    days_left = reason.params.get("days_left") if reason is not None else None
    # A legacy row's id is "legacy", matching neither id below, so it takes the same
    # unknown-countdown fallback any unrecognized shape does: the number is lost, never the
    # chip, and the next scan restores it.
    chip_reason = (
        Reason("came_back", {"days_left": int(days_left)})
        if reason is not None
        and reason.id in {"returned_came_back", "returned_came_back_ours"}
        and isinstance(days_left, int | float)
        else Reason("came_back_unknown")
    )
    return ChipOut(tone="held", reason=to_wire(chip_reason))


def _chip(
    exp: dict[str, Any] | None, verdict: str, score: int, media_type: str = "movie"
) -> ChipOut | None:
    """The card's one short status chip: Sanctuary and Limbo lanes only.

    Follows decide_verdict's own precedence (match trouble, then a deliberate
    left-for-you flag, then checks that could not run, then the coverage floor, then the
    score), so the chip names the fact that actually put the item in its lane. This is pure
    display extraction from the decoded stored explanation (``_decode_explanation``): never
    a re-decision, and never an error that drops a row off the queue. Condemned rows get no
    chip here. Their card leads with the amber dormancy pill (``dormant_days``).

    Each chip carries a typed ``reason`` (id plus params, see ``ChipOut``) rather than
    rendered English, so the frontend composes both the chip and its standalone sentence
    from one id, with nothing to keep in sync by hand.
    """
    if not isinstance(exp, dict):
        return None

    if verdict == "protect":
        fired = _entries(exp, "protections_fired")
        if not fired:
            return None
        gate = str(fired[0].get("gate") or "")
        lead_reason = _detail_reason(fired[0])
        if not _is_hand_spare(lead_reason) and (came_back := _came_back_chip(fired)) is not None:
            return came_back
        return ChipOut(tone="kept", reason=to_wire(_kept_reason(gate, lead_reason)))

    if verdict != "abstain":
        return None

    status = _match_status(exp)
    if status == "unmatched":
        return ChipOut(tone="quiet", reason=to_wire(Reason("match.unmatched")))
    if status == "ambiguous":
        return ChipOut(tone="quiet", reason=to_wire(Reason("match.ambiguous")))
    if status == "conflicted":
        return ChipOut(
            tone="quiet",
            reason=to_wire(Reason("match.conflicted", {"media_type": media_type})),
        )
    if status == MATCH_UNREADABLE:
        return ChipOut(tone="quiet", reason=to_wire(Reason("match.unreadable")))

    unknown = _entries(exp, "protections_unknown")
    for entry in unknown:
        entry_reason = _detail_reason(entry)
        deliberate = entry_reason is not None and entry_reason.id not in ("blocked", "legacy")
        if deliberate:
            # A deliberate "decide this yourself" flag, today the season keep-rule conflict,
            # not a plumbing failure. The one blocked case that wants eyes.
            if str(entry.get("gate") or "") == "season_progression":
                # Three conflict shapes reach here, and only one actually compares watcher
                # counts between the two seasons (``services.season_pruning.PruneConflict``).
                # The other two are: the kept season's watcher count could not be read at
                # all, or the watch mirror does not reach back far enough for the comparison
                # to mean anything. All three share this flag, so its copy must stay true
                # for all three, and the season named as "kept" may be the one being removed.
                #
                # Read the ``defers_to_owner`` flag, not the free-text message: two different
                # conflict messages can share the same wording, so parsing text cannot tell
                # the three shapes apart.
                #
                # ``GateOutcomeOut`` serves this same flag to the why panel
                # (``WhyPanel.conflictNote``), which branches the same three ways. Add a
                # fourth shape here and add it there too.
                #
                # ``_match_status`` runs earlier in this function, so a row with Plex match
                # trouble shows that chip instead. The panel can still show both the match
                # problem and the conflict.
                #
                # A row frozen before this flag existed, or one with a non-bool value, gives
                # no legible answer. ``thaw_defers_to_owner`` reads both as ``None``, which
                # falls through to the plain conflict chip below.
                defers = thaw_defers_to_owner(entry.get("defers_to_owner"))
                if defers is True:
                    return ChipOut(tone="look", reason=to_wire(Reason("look.comparable")))
                if defers is False:
                    return ChipOut(tone="look", reason=to_wire(Reason("look.unknowable")))
            return ChipOut(tone="look", reason=to_wire(Reason("look.unsettled")))
    if unknown:
        return ChipOut(tone="quiet", reason=to_wire(Reason("unknown_checks")))

    threshold = thaw_threshold(exp.get("threshold"))
    if threshold is not None:
        if score >= threshold:
            # decide_verdict's order: past the blocked cases, an abstain at or above
            # the threshold can only be the coverage floor.
            return ChipOut(tone="quiet", reason=to_wire(Reason("coverage")))
        return ChipOut(
            tone="quiet",
            reason=to_wire(Reason("below_threshold", {"score": score, "threshold": threshold})),
        )
    return ChipOut(tone="quiet", reason=to_wire(Reason("below")))


def _season_number(media_key: str) -> int | None:
    """The season a key addresses, or None for a movie, or for a key that does not parse.
    Display extraction never errors a row off the queue."""
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
    """ISO of the last spare covering this item, for the surfaces that color it.

    The twin of the ``spare_expires_at`` each caller computes beside it, and a deliberately
    different question. That one is the spare in force by precedence, which is what a
    control toggles. This one is when the item stops being kept, which is what a color or a
    sentence about its fate must read. They differ exactly when both levels spare an item
    and the higher-precedence one runs out first. Derived once here, from
    :func:`whitelist.covering_spare_expiry`, so every site that reports it answers the same
    way.

    ``None`` covers both "forever" and "not spared", the same as ``spare_expires_at``. Read
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
    expiries: dict[str, datetime | None] | None = None,
    search_rank: int | None = None,
    matched_collection: str | None = None,
) -> CandidateOut:
    # Three views of the one whitelist. The decision in effect (own, or inherited from the
    # show) colors the row. The item's own decision is what a control on this row can toggle.
    # The show's decision is what still keeps a season the operator did not touch. Computed
    # in one place so a season row and its show card can never disagree about what is spared.
    decisions = decisions or {}
    expiries = expiries or {}
    override = whitelist.effective_override(r.media_key, decisions)
    override_own = decisions.get(r.media_key)
    _show_key = whitelist.show_key(r.media_key)
    show_override = decisions.get(_show_key) if _show_key else None
    # The expiry belongs to whichever spare is in force: the effective one colors this row's
    # countdown, the show one drives the whole-show card. Both are None for a forever spare
    # and for no spare at all. Read them only alongside the matching "spare" decision above.
    spare_exp = (
        whitelist.effective_spare_expiry(r.media_key, decisions, expiries)
        if override == "spare"
        else None
    )
    show_spare_exp = expiries.get(_show_key) if (_show_key and show_override == "spare") else None
    # One parse of the stored explanation, shared by the pill, the reason line, the chip
    # and the reap-override read below, instead of each running its own json.loads over
    # the same multi-KB document.
    explanation = _decode_explanation(r.explanation_json)
    dormant_days = _dormant_days(explanation)
    primary_reason = _primary_reason(explanation, r.verdict, r.score, r.media_type)
    reason_key = to_wire(primary_reason) if primary_reason is not None else None
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
        # The poster comes from Plex, proxied by our own image route (see api/poster.py),
        # never the *arr's stale art. For a season this is the show's key
        # (poster_rating_key), since many seasons have no poster of their own. A movie
        # falls back to its own key.
        poster_url=(
            f"/api/poster/{r.poster_rating_key or r.plex_rating_key}"
            if (r.poster_rating_key or r.plex_rating_key)
            else None
        ),
        requested_by=r.requested_by,
        group_key=r.group_key,
        group_title=r.group_title,
        video_resolution=r.video_resolution,
        library=r.library_title,
        dormant_days=dormant_days,
        reason_key=reason_key,
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
        show_status=r.show_status,
        collections=_collections(r.collections_json),
        search_rank=search_rank,
        matched_collection=matched_collection,
    )


async def _deep_links(session: AsyncSession, row: Candidate) -> LinksOut:
    """The panel's jump links, from coordinates frozen on the row plus the live
    instance and server configuration. Every lookup failure degrades that one link to
    ``None``. An unroutable key, a removed instance, or an unlinked Plex account must
    never break the why-panel."""
    arr_base: str | None = None
    try:
        ref = MediaRef.parse(row.media_key)
    except PlanError:
        ref = None
    if ref is not None:
        # The instance the key routes to, by id and kind, never "the first Radarr".
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
        # A season row searches by its show's title ("Example Show", not
        # "Example Show, Season 3"), since that is the page the rating describes.
        title=row.group_title or row.title,
        # The rows an abstain could not choose between, read through `_replayed_evidence`
        # rather than a second copy of the same json.loads, so the links and the replay
        # can never disagree about which rows the operator was shown.
        candidate_rating_keys=_replayed_evidence(row).get("match_candidates", ()),
    )
    # Field for field off `DeepLinks`, including the nested `match_candidates`.
    return LinksOut.model_validate(links, from_attributes=True)


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


def _collection_sizes(collection_sizes_json: str | None) -> dict[str, int] | None:
    """The stored collection-size map, decoded the same defensive way ``_collections`` reads
    the per-item list: unparseable or absent both read as ``None`` (not recorded), and a
    non-integer count is dropped rather than guessed at, so the header omits a number
    rather than showing a wrong one."""
    if not collection_sizes_json:
        return None
    try:
        raw = json.loads(collection_sizes_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    return {str(k): v for k, v in raw.items() if isinstance(v, int)}


def _collections(collections_json: str | None) -> list[str] | None:
    """The stored collection list, defensively. Unlike ``_genres``, ``None`` survives
    instead of collapsing to an empty list.

    ``None`` means "not recorded for this scan" (no Plex configured, a section read that
    failed, a row from before this shipped), which the UI must render differently from "in
    no collection". Collections are navigation, never protection, so nothing here degrades
    or re-decides. A read failure just costs the chip. Anything unparseable reads the same
    as absent, the same conservative default ``_genres`` uses."""
    if not collections_json:
        return None
    try:
        raw = json.loads(collections_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, list):
        return None
    return [str(c) for c in raw if c]


@router.get("/candidates/{candidate_id}", tags=[api_tags.REVIEW])
async def candidate_detail(request: Request, candidate_id: int) -> CandidateDetail:
    """The why-panel.

    Renders for PROTECTED items too, showing the score it is overriding. A tool that
    only explains its deletions cannot be trusted about its keeps.
    """
    async with session_factory(request)() as session:
        row = await session.get(Candidate, candidate_id)
        if row is None:
            refuse(404, "error.review.candidate_not_found")

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
    which go" is answered in one place. The show info panel and the expanded show card
    both read it. These are frozen candidate rows only. Nothing here re-decides a verdict.
    """
    async with session_factory(request)() as session:
        snapshot = await newest_snapshot(session)
        if snapshot is None:
            refuse(404, "error.review.no_scan")
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
            refuse(404, "error.review.show_not_in_scan")

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

        # The show-level status line: a season deliberately left for the owner wins (that
        # is the line that wants eyes), else the highest-scoring season, the same member
        # the collapsed card leads with.
        lead = next(
            (s for s in seasons if s.chip is not None and s.chip.tone == "look"),
            max(seasons, key=lambda c: c.score),
        )
        lead_row = next(r for r in rows if r.id == lead.id)
        # The whole-show spare's countdown, when a show-level spare is set. None for a
        # forever show-spare, or none at all. The panel reads it only when show_override is
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
            reason_key=lead.reason_key,
            # A show-level fact: every season shares the show's library, so the first row
            # that carries one answers for the whole show (None if none do).
            library=next((r.library_title for r in rows if r.library_title), None),
            chip=lead.chip,
            # The show's own decision (the show key), which the panel's whole-show control
            # toggles. Read straight from the whitelist, never rolled up from the seasons'
            # own marks. The control clears only this key, so lighting it from an aggregate
            # it cannot clear would show a state the control has no way to undo.
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
