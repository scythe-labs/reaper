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
from datetime import datetime

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.api.schemas import (
    CandidateDetail,
    CandidateOut,
    ConditionIn,
    Explanation,
    FieldOut,
    GateSettingIn,
    PolicyIn,
    PolicyOut,
    PolicyWarningOut,
    SignalSettingIn,
    SimulationOut,
    SnapshotOut,
    VocabularyOut,
)
from reaper.clock import utcnow
from reaper.db.models import Candidate, FirstFlagged, Snapshot
from reaper.db.models import Policy as PolicyModel
from reaper.engine.fields import Lane, vocabulary
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    ConditionSpec,
    GateSetting,
    PolicyBody,
    ProfileSettings,
    SignalSetting,
    combine_hashes,
    inspect,
)
from reaper.services import whitelist

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
    reclaimable = (
        await session.execute(
            select(func.coalesce(func.sum(Candidate.size_bytes), 0)).where(
                Candidate.snapshot_id == snapshot.id, Candidate.verdict == "condemn"
            )
        )
    ).scalar_one()

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
    )


@router.get("/candidates")
async def list_candidates(
    request: Request,
    response: Response,
    verdict: str = "condemn",
    search: str | None = None,
    media_type: str | None = None,
    requested: str = "any",
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
    movies or seasons, and ``requested`` keeps only what someone asked for through Seerr
    (``yes``), only what nobody asked for (``no``), or everything (``any``).
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
        if requested == "yes":
            conditions.append(Candidate.requested_by.is_not(None))
        elif requested == "no":
            conditions.append(Candidate.requested_by.is_(None))

        totals = (
            await session.execute(
                select(func.count(), func.coalesce(func.sum(Candidate.size_bytes), 0)).where(
                    *conditions
                )
            )
        ).one()
        response.headers["X-Total-Count"] = str(int(totals[0]))
        response.headers["X-Total-Bytes"] = str(int(totals[1]))

        direction = asc if order == "asc" else desc
        sort_columns = {
            "score": Candidate.score,
            "size": Candidate.size_bytes,
            "year": Candidate.year,
            "title": func.lower(func.coalesce(Candidate.group_title, Candidate.title)),
        }
        primary = direction(sort_columns.get(sort, Candidate.score))
        # A score/size tiebreak after the chosen key keeps ordering deterministic -- so a
        # show's seasons stay adjacent and paging never splits or shuffles the list.
        stmt = (
            select(Candidate)
            .where(*conditions)
            .order_by(primary, Candidate.score.desc(), Candidate.size_bytes.desc())
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

        return [
            _candidate_out(
                r, flagged.get(r.media_key), whitelist.effective_override(r.media_key, decisions)
            )
            for r in rows
        ]


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
    # abstain: a protection that could not be checked, else "scored too low".
    unknown = exp.get("protections_unknown") or []
    if unknown:
        return f"Couldn't check: {unknown[0]['detail']}"
    return "Scored below your threshold."


def _candidate_out(
    r: Candidate, flagged_at: datetime | None = None, override: str | None = None
) -> CandidateOut:
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
        # never the *arr's stale art. Only items Plex has matched (a rating key) have one.
        poster_url=(f"/api/poster/{r.plex_rating_key}" if r.plex_rating_key else None),
        requested_by=r.requested_by,
        group_key=r.group_key,
        group_title=r.group_title,
        reason=_primary_reason(r.explanation_json, r.verdict),
        spared=override == "spare",
        override=override,
    )


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
            whitelist.effective_override(row.media_key, decisions),
        )
        return CandidateDetail(
            **base.model_dump(),
            explanation=Explanation(**json.loads(row.explanation_json)),
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
            keep_tags=tuple(t.strip() for t in payload.keep_tags if t.strip()),
            keep_tags_match=payload.keep_tags_match,
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


def _policy_out(body: PolicyBody, name: str) -> PolicyOut:
    return PolicyOut(
        policy_hash=body.policy_hash(),
        name=name,
        body=PolicyIn(
            name=name,
            media_type=body.media_type,
            condemn_at=body.condemn_at,
            coverage_floor_bp=body.coverage_floor_bp,
            keep_last_seasons=body.keep_last_seasons,
            keep_first_season=body.keep_first_season,
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
            keep_tags=list(body.keep_tags),
            keep_tags_match=body.keep_tags_match,
        ),
        warnings=[
            PolicyWarningOut(field=w.field, message=w.message, severity=w.severity)
            for w in inspect(body, ProfileSettings())
        ],
    )


async def active_policy(session: AsyncSession, media_type: str = "movie") -> tuple[PolicyBody, str]:
    """The policy Reaper is currently working to, for one media type.

    Movies and TV are tuned separately -- keep-last-N seasons and the season-rank signal only
    make sense for TV, and a library often wants a gentler hand on one than the other -- so
    there are two policies, chosen here by ``media_type`` ("movie" or "tv").

    The most recently saved one for that type, or the built-in default if none has been saved.
    Policy rows are **immutable and append-only** -- editing writes a new row with a new hash
    rather than mutating the old one, because snapshots, approvals and audit entries point at
    that hash and must stay interpretable years later.
    """
    row = (
        await session.execute(
            select(PolicyModel)
            .where(PolicyModel.media_type == media_type)
            .order_by(PolicyModel.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if row is None:
        return (DEFAULT_TV_POLICY if media_type == "tv" else DEFAULT_MOVIE_POLICY), "default"
    return PolicyBody.model_validate_json(row.body_json), row.name


async def active_policies(session: AsyncSession) -> tuple[PolicyBody, PolicyBody]:
    """The (movie, tv) policies in force, in that fixed order -- the pair a scan runs to."""
    movie, _ = await active_policy(session, "movie")
    tv, _ = await active_policy(session, "tv")
    return movie, tv


def _candidate_media_type(policy_media_type: str) -> str:
    """The candidate ``media_type`` a policy governs: a TV policy scores *seasons*."""
    return "season" if policy_media_type == "tv" else "movie"


@router.get("/policy")
async def get_policy(request: Request, media_type: str = "movie") -> PolicyOut:
    """Load the active policy for a media type, so the editor opens on what is in force."""
    async with _sessions(request)() as session:
        body, name = await active_policy(session, media_type)
    return _policy_out(body, name)


@router.post("/policy")
async def save_policy(request: Request, payload: PolicyIn) -> PolicyOut:
    """Save a policy. **Append-only: this never updates a row.**

    Saving the same policy twice is a no-op rather than a duplicate -- the hash is the
    identity, so an owner who opens the editor and saves without changing anything does
    not fork the audit trail.

    Note what this does *not* do: it does not arm anything. Reaper still cannot delete,
    and a saved policy takes effect on the next scan.
    """
    body = _to_body(payload)
    policy_hash = body.policy_hash()

    async with _sessions(request)() as session:
        existing = (
            await session.execute(select(PolicyModel).where(PolicyModel.policy_hash == policy_hash))
        ).scalar_one_or_none()

        if existing is None:
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
        else:
            # Content-identical save: append-only, so nothing is written and the name is
            # NOT changed. Echo the *persisted* name, not the discarded request name, so
            # the success response matches what the next GET /api/policy will show --
            # otherwise a name-only edit looks like it stuck when it silently did not.
            return _policy_out(body, existing.name)

    return _policy_out(body, payload.name)


@router.post("/policy/validate")
async def validate_policy(payload: PolicyIn) -> PolicyOut:
    """Validate, hash, and inspect.

    Validation refuses what is *provably* wrong. ``inspect`` warns about what is merely
    *probably* wrong -- and no validator can tell those apart, because the values are
    legal either way. The archetype: an IMDb floor of 96 is a legal 9.6, and is
    indistinguishable from a Rotten Tomatoes 96 typed into the wrong box.
    """
    return _policy_out(_to_body(payload), payload.name)


@router.post("/policy/simulate")
async def simulate(request: Request, payload: PolicyIn) -> SimulationOut:
    """Re-decide the last snapshot under a candidate policy. **Zero API calls.**

    This is what makes threshold-tuning honest: the knob and its blast radius sit in the
    same viewport. Move the threshold, and the count, the byte total and the histogram
    move with it -- instantly, without touching Sonarr, Radarr or Tautulli.

    **And it only works for the thresholds.** The simulator re-compares *stored* scores
    and verdicts against new numbers. That is exact for ``condemn_at`` and
    ``coverage_floor_bp``. It is simply wrong for anything else: change a signal weight
    or a gate, and every stored score was produced by the old ones. There is no way to
    recover the new answer from the snapshot -- it would take re-reading the library.

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
        other, _ = await active_policy(session, other_type)
        movie_scoring, tv_scoring = (
            (body.scoring_hash(), other.scoring_hash())
            if body.media_type == "movie"
            else (other.scoring_hash(), body.scoring_hash())
        )

        if snapshot.scoring_hash != combine_hashes(movie_scoring, tv_scoring):
            kind = "movies" if body.media_type == "movie" else "TV"
            return SimulationOut(
                exact=False,
                stale_reason=(
                    "You changed a signal weight or a protection, so the last scan's scores no "
                    f"longer describe this {kind} policy. Run a scan to score the library under "
                    "it, then this becomes exact again."
                ),
                condemned=0,
                protected=0,
                abstained=0,
                reclaimable_bytes=0,
                newly_condemned=0,
                no_longer_condemned=0,
                histogram=[0] * 10,
            )

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

    histogram = [0] * 10
    condemned = protected = abstained = 0
    reclaimable = 0
    newly = gone = 0

    for row in rows:
        histogram[min(row.score // 10, 9)] += 1

        was_condemned = row.verdict == "condemn"

        # A protection always wins, whatever the threshold. Only the score-based
        # verdicts can move.
        if row.verdict == "protect":
            protected += 1
            continue

        eligible = row.coverage_bp >= body.coverage_floor_bp
        now_condemned = eligible and row.score >= body.condemn_at

        if now_condemned:
            condemned += 1
            reclaimable += row.size_bytes
            if not was_condemned:
                newly += 1
        else:
            abstained += 1
            if was_condemned:
                gone += 1

    return SimulationOut(
        exact=True,
        condemned=condemned,
        protected=protected,
        abstained=abstained,
        reclaimable_bytes=reclaimable,
        newly_condemned=newly,
        no_longer_condemned=gone,
        histogram=histogram,
    )


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


@router.get("/vocabulary")
async def get_vocabulary(lane: Lane) -> VocabularyOut:
    """The fields available in one lane.

    Filtered **server-side, before serialisation**. ``?lane=condemn`` never returns a
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
