# SPDX-License-Identifier: AGPL-3.0-or-later
"""Building a snapshot: gather, freeze, then judge.

The order matters. **Everything is gathered and frozen before anything is scored**, so
that every item in a run is judged against the same evidence. A Sonarr timeout halfway
through must not be able to flip the fate of the items that come after it.

That is not hypothetical. Maintainerr #3125: *"collection items flip in/out when Sonarr
API lookups fail transiently during rule runs."* An item's fate should not depend on
network luck.

## Degradation is loud, and it is a gate

If a source is unreachable, the snapshot is marked **degraded**. A degraded snapshot may
still be *viewed* -- the owner should be able to see exactly what went wrong -- but
nothing may be executed against it. Partial evidence is how you delete a beloved film
during an API outage.

## Every fact carries its provenance

A `Fact` is `Known`, `Absent`, or `Unknown`. An empty result from a *failed* call is
``Unknown``, never ``Absent`` and never ``[]``. That distinction is the difference
between "nobody watched this" (evidence, may condemn) and "we could not find out"
(never evidence, may only protect), and every adapter below has a test asserting which
one it produces.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from reaper.clients.arr import RadarrClient
from reaper.clients.base import IntegrationError
from reaper.clients.plex import PlexClient, PlexError
from reaper.clients.tautulli import TautulliClient
from reaper.clock import from_epoch, utcnow
from reaper.db.models import Candidate, FirstFlagged, Snapshot
from reaper.engine import identity
from reaper.engine.gates import PROTECT, Evaluation, Facts, Gate, GateId, GateResult, evaluate_all
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.engine.policy import PolicyBody, combine_hashes
from reaper.engine.signals import Score, SignalConfig, score
from reaper.services import history_sync, lists, requested_by, season_scan, whitelist
from reaper.services.imdb_dataset import DatasetDegradedError, ImdbRating, ImdbRatings

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Progress:
    """One step of a scan, streamed to the browser over SSE."""

    phase: str
    done: int
    total: int
    detail: str = ""


ProgressFn = Callable[[Progress], None]


@dataclass
class ScanContext:
    """Everything gathered, before anything is judged."""

    horizon: datetime
    active_rating_keys: set[int] = field(default_factory=set)
    degraded_reasons: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return bool(self.degraded_reasons)

    def degrade(self, reason: str) -> None:
        log.warning("snapshot.degraded", reason=reason)
        self.degraded_reasons.append(reason)


@dataclass(frozen=True, slots=True)
class RawItem:
    """One movie, as the *arr sees it, before any judgement."""

    media_key: str
    title: str
    media_type: str
    size_bytes: int
    imdb_id: str | None
    tmdb_id: int | None
    plex_rating_key: int | None
    added_at: datetime | None
    has_file: bool
    # Display fields, carried onto the candidate so the review queue can show a poster and
    # a blurb without a second data source. None of them influence the verdict.
    year: int | None = None
    summary: str | None = None
    poster_url: str | None = None
    requested_by: str | None = None
    # How this item was bound to its Plex row (and why, if it was not) -- for the why-panel.
    matched_by: identity.MatchedBy | None = None
    match_detail: str | None = None
    match_status: identity.MatchStatus | None = None


async def build_facts(
    engine: AsyncEngine,
    item: RawItem,
    context: ScanContext,
    *,
    imdb: dict[str, ImdbRating],
    last_played: dict[int, datetime],
    watchers_window: dict[int, int],
    watchers_all_time: dict[int, int],
    whitelisted: set[str],
) -> Facts:
    """Assemble one item's evidence.

    Note how often ``Unknown`` appears. Every one of them is a place where a naive
    implementation would have written ``0``, ``[]`` or ``False`` -- and every one of
    those would have quietly condemned an item we know nothing about.
    """
    rating_key = item.plex_rating_key

    # --- dormancy -----------------------------------------------------------
    # THE derived field. "Days since last play" is null for exactly the items we care
    # about most, and coercing that null to epoch 0 reads as ~20,600 days unwatched --
    # the maximum condemnation pressure, for the item we know least about.
    dormancy: Observation[float]
    if rating_key is None:
        dormancy = Unknown(reason="Plex has not matched this item", source="plex")
    elif item.added_at is None:
        dormancy = Unknown(reason="no added-at date", source="tautulli")
    else:
        played = last_played.get(rating_key)
        reference = played or max(item.added_at, context.horizon)
        dormancy = Known(value=(utcnow() - reference).days, source="tautulli")

    # --- popularity ---------------------------------------------------------
    recent: Observation[int]
    all_time: Observation[int]
    if rating_key is None:
        recent = Unknown(reason="Plex has not matched this item", source="plex")
        all_time = Unknown(reason="Plex has not matched this item", source="plex")
    else:
        recent = Known(value=watchers_window.get(rating_key, 0), source="tautulli")
        all_time = Known(value=watchers_all_time.get(rating_key, 0), source="tautulli")

    # --- ratings ------------------------------------------------------------
    rating: Observation[int]
    votes: Observation[int]
    entry = imdb.get(item.imdb_id or "")
    if entry is not None:
        rating = Known(value=int(entry.average_rating * 10), source="imdb")
        votes = Known(value=int(entry.num_votes), source="imdb")
    else:
        # Absent, not Unknown: we looked and this title genuinely has no IMDb rating.
        # (A *degraded* dataset is different, and is caught upstream -- it degrades the
        # whole snapshot rather than silently unprotecting every film.)
        rating = Absent(source="imdb")
        votes = Absent(source="imdb")

    # --- lists --------------------------------------------------------------
    # Whitelist and curated are DIFFERENT reasons to keep a file, and collapsing them
    # would tell the owner "whitelisted" about a film they never touched. The why-panel
    # must be able to say which.
    memberships = await lists.memberships(engine, imdb_id=item.imdb_id, tmdb_id=item.tmdb_id)
    hard = [m for m in memberships if m.mode is lists.ListMode.HARD]

    whitelists = [m for m in hard if m.is_whitelist]
    curated_lists = [m for m in hard if not m.is_whitelist]

    curated: Observation[str] = (
        Known(value=", ".join(m.describe() for m in curated_lists), source="lists")
        if curated_lists
        else Absent(source="lists")
    )
    is_whitelisted: Observation[bool] = Known(
        value=bool(whitelists) or item.media_key in whitelisted,
        source=whitelists[0].display_name if whitelists else "lists",
    )

    # --- streaming right now ------------------------------------------------
    streaming: Observation[bool]
    if "tautulli-activity" in " ".join(context.degraded_reasons):
        # We could not check. Never assume False -- that is how a tool deletes a file
        # somebody is watching.
        streaming = Unknown(reason="could not read active sessions", source="tautulli")
    else:
        streaming = Known(
            value=rating_key in context.active_rating_keys if rating_key else False,
            source="tautulli",
        )

    return Facts(
        title=item.title,
        days_observed_unwatched=dormancy,
        distinct_watchers=recent,
        distinct_watchers_all_time=all_time,
        size_bytes=Known(value=item.size_bytes, source="radarr"),
        imdb_rating_tenths=rating,
        imdb_votes=votes,
        season_rank=Absent(source="radarr"),  # movies have no season
        is_streaming_now=streaming,
        is_managed=Known(value=True, source="radarr"),  # it came FROM radarr
        in_curated_list=curated,
        is_whitelisted=is_whitelisted,
        # Not applicable outside the requester rule: with no requester, "others" is
        # everyone, and the gate would protect anything ever played.
        others_watching=Absent(source="tautulli"),
    )


@dataclass(frozen=True, slots=True)
class RadarrSource:
    """One Radarr instance, and the id it is known by."""

    client: RadarrClient
    instance_id: int
    name: str


#: Re-exported so callers wire both media types from one module. The TV gather itself
#: lives in ``season_scan`` -- it is a large, self-contained read path -- but a Sonarr
#: instance is a scan source exactly as a Radarr one is.
SonarrSource = season_scan.SonarrSource


async def scan(
    engine: AsyncEngine,
    session: AsyncSession,
    *,
    radarrs: list[RadarrSource],
    tautulli: TautulliClient,
    movie_policy: PolicyBody,
    movie_gates: list[Gate],
    tv_policy: PolicyBody,
    tv_gates: list[Gate],
    plex: PlexClient | None = None,
    sonarrs: list[season_scan.SonarrSource] | None = None,
    requested: dict[str, str] | None = None,
    grace_days: int = 14,
    extra_degrade_reasons: Sequence[str] | None = None,
    on_progress: ProgressFn | None = None,
) -> Snapshot:
    """Gather, freeze, judge, persist. Read-only throughout.

    Movies are judged under ``movie_policy`` and seasons under ``tv_policy`` -- the two are
    tuned separately -- so the snapshot's ``policy_hash`` / ``scoring_hash`` are the
    *combination* of both (see ``policy.combine_hashes``), and the simulator recombines to stay
    honest per media type.

    **Every** Radarr instance is scanned, not one of them. A separate 4K instance
    alongside the HD one is a common setup, and scanning whichever happened to be first
    would silently ignore an entire library -- while reporting a clean, confident,
    non-degraded result. (This is exactly what happened: an early version picked a
    single arbitrary instance and scanned a small fraction of the library, with no
    indication that anything was missing.)

    ``media_key`` already carries the instance id, so the same film in the HD and 4K
    instances is two distinct rows -- which is correct: they are two distinct files.
    """
    emit = on_progress or (lambda _p: None)
    requested = requested or {}

    context = ScanContext(horizon=utcnow())

    # ---- gather ------------------------------------------------------------
    emit(Progress("gathering", 0, 5, "watch history"))

    horizon = await history_sync.horizon(engine)
    no_history = horizon is None
    if horizon is None:
        horizon = utcnow()
    context = ScanContext(horizon=horizon)
    if no_history:
        # Degrade AFTER the context is rebuilt with the resolved horizon. Degrading the
        # earlier throwaway context (then replacing it here) silently dropped the reason, so
        # a scan with no watch history at all -- which can judge nothing safely -- looked
        # non-degraded and executable. Fail closed instead.
        context.degrade("no watch history at all -- nothing can be judged")

    # Failures the caller detected BEFORE the gather (an unreachable Plex, a protection list
    # that failed to sync with an empty keep-list) degrade this snapshot the same loud,
    # un-executable way an in-gather failure does. A whitelist that could not refresh must
    # never let a reap run against an empty keep-list -- fail closed, exactly as the source
    # failures below do.
    for reason in extra_degrade_reasons or ():
        context.degrade(reason)

    emit(Progress("gathering", 1, 5, "active streams"))
    try:
        activity = await tautulli.activity()
        for session_data in activity.get("sessions") or []:
            for key in ("rating_key", "parent_rating_key", "grandparent_rating_key"):
                value = session_data.get(key)
                if value:
                    context.active_rating_keys.add(int(value))
    except IntegrationError as exc:
        # Do NOT assume nothing is playing. That is how you delete a file mid-stream.
        context.degrade(f"tautulli-activity unreachable: {exc}")

    emit(Progress("gathering", 3, 5, "the Plex library index"))
    plex_index = await build_movie_index(tautulli, plex, degrade=context.degrade)

    items: list[RawItem] = []
    all_movies: list[dict[str, Any]] = []
    for source in radarrs:
        emit(Progress("gathering", 2, 5, f"movies from Radarr ({source.name})"))
        try:
            movies = await source.client.movies()
        except IntegrationError as exc:
            # One instance down must not silently shrink the library. Degrade, so no run
            # may execute against a snapshot that is missing an entire *arr.
            context.degrade(f"radarr '{source.name}' unreachable: {exc}")
            continue
        all_movies.extend(movies)
        items.extend(_raw_items(movies, plex_index, source.instance_id, requested))
        log.info("snapshot.radarr", instance=source.name, movies=len(movies))

    emit(Progress("gathering", 4, 5, "IMDb ratings"))
    imdb_ids = [m["imdbId"] for m in all_movies if m.get("imdbId")]
    try:
        imdb = await ImdbRatings(engine).lookup(imdb_ids)
    except DatasetDegradedError as exc:
        # The inverted failure: a missing rating REMOVES protection. Degrade loudly.
        context.degrade(str(exc))
        imdb = {}
    last_played, watchers_window, watchers_all_time = await _watch_stats(
        engine,
        rating_keys={i.plex_rating_key for i in items if i.plex_rating_key},
        window_days=_popularity_window(movie_policy),
    )
    # The owner's manual overrides -- ``media_key -> "spare" | "reap"`` -- loaded once and
    # applied to every item's verdict. A spared file is judged PROTECT rather than surfacing in
    # "would delete" again; a reaped one is forced onto the list (short of a hard safety gate).
    # Keys may be a show's, in which case the decision applies to all of its seasons.
    override_map = await whitelist.overrides(session)
    # Manual spares are applied via the override, not the whitelist gate, so the gate is left to
    # the *arr-tag / collection whitelists alone. An empty set keeps that path tag-only.
    tag_only_whitelist: set[str] = set()

    # ---- gather TV seasons -------------------------------------------------
    # A read-only extension of the same gather. It reads Sonarr, resolves prunable
    # seasons to Plex, and reads their watch history from the same local mirror. A
    # movie-only deployment (no Sonarr) passes an empty list and this is a no-op.
    season_judgements: list[season_scan.SeasonJudgement] = []
    if sonarrs:
        emit(Progress("gathering", 4, 5, "TV seasons from Sonarr"))
        activity_degraded = "tautulli-activity" in " ".join(context.degraded_reasons)
        season_judgements = await season_scan.gather(
            engine,
            sonarrs=sonarrs,
            tautulli=tautulli,
            plex=plex,
            horizon=context.horizon,
            active_rating_keys=context.active_rating_keys,
            activity_degraded=activity_degraded,
            keep_last_seasons=tv_policy.keep_last_seasons,
            keep_first_season=tv_policy.keep_first_season,
            window_days=_popularity_window(tv_policy),
            whitelisted=tag_only_whitelist,
            degrade=context.degrade,
            requested=requested,
        )

    # ---- freeze ------------------------------------------------------------
    snapshot = Snapshot(
        created_at=utcnow(),
        # Movies and seasons are judged under different policies, so the snapshot records the
        # combination of both -- movie first, TV second. See policy.combine_hashes.
        policy_hash=combine_hashes(movie_policy.policy_hash(), tv_policy.policy_hash()),
        scoring_hash=combine_hashes(movie_policy.scoring_hash(), tv_policy.scoring_hash()),
        horizon_at=context.horizon,
        item_count=len(items) + len(season_judgements),
        degraded=context.degraded,
        degraded_reason="; ".join(context.degraded_reasons) or None,
    )
    session.add(snapshot)
    await session.flush()

    # ---- judge -------------------------------------------------------------
    def _signals(policy: PolicyBody) -> list[SignalConfig]:
        return [
            SignalConfig(signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor)
            for s in policy.signals
        ]

    movie_signals = _signals(movie_policy)
    tv_signals = _signals(tv_policy)
    movie_window = _popularity_window(movie_policy)
    tv_window = _popularity_window(tv_policy)
    now = utcnow()
    condemned = 0
    total = len(items) + len(season_judgements)

    for index, item in enumerate(items):
        if index % 100 == 0:
            emit(Progress("scoring", index, total, item.title))

        facts = await build_facts(
            engine,
            item,
            context,
            imdb=imdb,
            last_played=last_played,
            watchers_window=watchers_window,
            watchers_all_time=watchers_all_time,
            whitelisted=tag_only_whitelist,
        )
        verdict = await _judge_item(
            session,
            snapshot_id=snapshot.id,
            media_key=item.media_key,
            plex_rating_key=item.plex_rating_key,
            title=item.title,
            media_type=item.media_type,
            size_bytes=item.size_bytes,
            facts=facts,
            gates=movie_gates,
            signals=movie_signals,
            policy=movie_policy,
            now=now,
            window_days=movie_window,
            grace_days=grace_days,
            display=Display(
                year=item.year,
                summary=item.summary,
                poster_url=item.poster_url,
                requested_by=item.requested_by,
            ),
            matched_by=item.matched_by,
            match_detail=item.match_detail,
            match_status=item.match_status,
            override=whitelist.effective_override(item.media_key, override_map),
        )
        if verdict == "condemn":
            condemned += 1

    # Seasons run through the SAME judge: the season-pruning guard is merged in as an
    # extra gate result, so a protected season is protected by a gate exactly as a
    # streamed movie is, and the why-panel renders both identically.
    for offset, judgement in enumerate(season_judgements):
        if offset % 100 == 0:
            emit(Progress("scoring", len(items) + offset, total, judgement.title))
        verdict = await _judge_item(
            session,
            snapshot_id=snapshot.id,
            media_key=judgement.media_key,
            plex_rating_key=judgement.plex_rating_key,
            title=judgement.title,
            media_type="season",
            size_bytes=judgement.size_bytes,
            facts=judgement.facts,
            gates=tv_gates,
            signals=tv_signals,
            policy=tv_policy,
            now=now,
            window_days=tv_window,
            grace_days=grace_days,
            display=Display(
                year=judgement.year,
                summary=judgement.summary,
                poster_url=judgement.poster_url,
                requested_by=judgement.requested_by,
                group_key=judgement.group_key,
                group_title=judgement.group_title,
            ),
            matched_by=judgement.matched_by,
            match_detail=judgement.match_detail,
            match_status=judgement.match_status,
            extra_results=(judgement.guard_result,),
            override=whitelist.effective_override(judgement.media_key, override_map),
        )
        if verdict == "condemn":
            condemned += 1

    await session.flush()
    emit(Progress("done", total, total, f"{condemned} candidates"))

    log.info(
        "snapshot.built",
        snapshot=snapshot.id,
        items=len(items),
        seasons=len(season_judgements),
        condemned=condemned,
        degraded=context.degraded,
    )
    return snapshot


@dataclass(frozen=True, slots=True)
class Display:
    """The presentation fields carried onto a candidate. None of them decide anything --
    they are what the review queue draws around the verdict."""

    year: int | None = None
    summary: str | None = None
    poster_url: str | None = None
    requested_by: str | None = None
    group_key: str | None = None
    group_title: str | None = None


#: The "no display fields" default, as a singleton so it is not constructed per call.
_NO_DISPLAY = Display()


async def _judge_item(
    session: AsyncSession,
    *,
    snapshot_id: int,
    media_key: str,
    plex_rating_key: int | None,
    title: str,
    media_type: str,
    size_bytes: int,
    facts: Facts,
    gates: list[Gate],
    signals: list[SignalConfig],
    policy: PolicyBody,
    now: datetime,
    window_days: int = 365,
    grace_days: int = 14,
    display: Display = _NO_DISPLAY,
    matched_by: identity.MatchedBy | None = None,
    match_detail: str | None = None,
    match_status: identity.MatchStatus | None = None,
    extra_results: Sequence[GateResult] = (),
    override: str | None = None,
) -> str:
    """Evaluate one item's gates and signals, store its candidate, return its verdict.

    Shared by the movie and season paths so both reach a verdict the same way. Seasons
    pass ``extra_results`` -- the season-pruning guard's outcome -- which is merged ahead
    of the ordinary gates: a guard PROTECT wins like any protection, and a guard *blocked*
    ABSTAIN (a keep-rule conflict) forces the item to abstain for a human to look at.

    Round FIRST, then decide -- and store exactly what decided. The stored integers are
    what the table shows, what the why-panel explains, and what the simulator re-decides;
    if the verdict were taken from the underlying float instead, an item scoring 69.7
    against a threshold of 70 would abstain while storing a 70, and the simulator would
    later condemn the very item the queue said it was sparing. There must be exactly one
    number, and everything must decide on it.
    """
    # A hand "spare" enters as an extra PROTECT result, so it wins like any protection and
    # shows in the why-panel as a reason; a hand "reap" is carried into _verdict, which forces
    # CONDEMN unless a hard safety gate still stands.
    merged_extra = list(extra_results)
    if override == "spare":
        merged_extra.insert(
            0, GateResult(GateId.WHITELISTED, PROTECT, detail="You spared this by hand.")
        )
    evaluation = Evaluation(results=[*merged_extra, *evaluate_all(gates, facts).results])
    item_score = score(signals, facts, window_days=window_days)

    score_value = round(item_score.value)
    coverage_bp = round(item_score.coverage * 10_000)

    verdict = _verdict(evaluation, score_value, coverage_bp, policy, override=override)
    if verdict == "condemn":
        await _record_first_flagged(session, media_key, now, grace_days=grace_days)

    session.add(
        Candidate(
            snapshot_id=snapshot_id,
            media_key=media_key,
            plex_rating_key=plex_rating_key,
            title=title,
            media_type=media_type,
            size_bytes=size_bytes,
            year=display.year,
            summary=display.summary,
            poster_url=display.poster_url,
            requested_by=display.requested_by,
            group_key=display.group_key,
            group_title=display.group_title,
            verdict=verdict,
            score=score_value,
            coverage_bp=coverage_bp,
            explanation_json=_explain(
                evaluation,
                item_score,
                policy,
                plex_rating_key=plex_rating_key,
                matched_by=matched_by,
                match_detail=match_detail,
                match_status=match_status,
            ),
            created_at=now,
        )
    )
    return verdict


#: The protections a manual "reap" override may NOT overrule -- a file that is streaming right
#: now must not be deleted, and an unmanaged file has no path to delete through. Everything
#: else (dormancy, rating, popularity, a curated list, the keep list) is a *cautious* judgement
#: the owner is entitled to overrule by hand.
_STRUCTURAL_GATES = frozenset({GateId.STREAMING_NOW, GateId.UNMANAGED})


def _verdict(
    evaluation: Evaluation,
    score_value: int,
    coverage_bp: int,
    policy: PolicyBody,
    *,
    override: str | None = None,
) -> str:
    """PROTECT beats everything. Then coverage. Then the score.

    Takes the **stored** integers, not the underlying floats, so that this function and
    the simulator -- which has only the stored integers to work with -- cannot reach
    different verdicts for the same item under the same policy. Two code paths that
    answer the same question must answer it the same way, and the cheapest way to
    guarantee that is to give them the same inputs.

    A manual ``"reap"`` override forces CONDEMN -- the owner looked and decided -- but never
    past a hard safety gate (streaming now, unmanaged) or a protection that could not be
    checked; those still protect. A ``"spare"`` override arrives as an extra PROTECT result and
    so is already handled by ``evaluation.protected``.
    """
    if override == "reap":
        blocked_by_safety = evaluation.blocked or any(
            r.fired and r.gate in _STRUCTURAL_GATES for r in evaluation.results
        )
        return "protect" if blocked_by_safety else "condemn"
    if evaluation.protected:
        return "protect"
    if evaluation.blocked:
        # A protection could not be checked. Not being able to look is not the same as
        # looking and finding nothing.
        return "abstain"
    if coverage_bp < policy.coverage_floor_bp:
        return "abstain"
    if score_value >= policy.condemn_at:
        return "condemn"
    return "abstain"


def _explain(
    evaluation: Evaluation,
    item_score: Score,
    policy: PolicyBody,
    *,
    plex_rating_key: int | None = None,
    matched_by: identity.MatchedBy | None = None,
    match_detail: str | None = None,
    match_status: identity.MatchStatus | None = None,
) -> str:
    """The why-panel.

    One dict, two sinks -- the UI and the audit log -- so what the owner was shown and
    what actually happened cannot drift apart.

    Three blocks, and the middle two are what make a verdict trustworthy:

    * protections that FIRED
    * protections CHECKED that did not fire, **with the actual numbers**
    * protections that COULD NOT BE CHECKED -- rendered amber, not green, because "we
      could not look" is not "we looked and it was fine"

    Plus a ``match`` block that says how (or whether) the item was bound to its Plex row --
    "bound by TMDB id 12345", or "kept: two Plex items share this id" -- so a file that was
    spared for a *matching* reason is not mistaken for one nobody looked at.
    """
    return json.dumps(
        {
            "score": round(item_score.value, 1),
            "threshold": policy.condemn_at,
            "coverage": round(item_score.coverage, 3),
            "match": {
                # status is what the UI reads: "matched" -> stay quiet, "unmatched" /
                # "ambiguous" -> a plain "kept to be safe" notice (the two differ only in
                # wording). by/detail are kept for the audit log, not shown to the owner.
                "status": match_status.value if match_status is not None else None,
                "by": matched_by.value if matched_by is not None else None,
                "detail": match_detail,
                "rating_key": plex_rating_key,
            },
            "signals": [
                {
                    "id": r.signal.value,
                    "contribution": round(r.pressure, 1),
                    "weight": r.weight,
                    "detail": r.detail,
                    "evaluated": r.evaluated,
                }
                for r in item_score.results
            ],
            "protections_fired": [
                {"gate": r.gate.value, "detail": r.detail} for r in evaluation.protectors
            ],
            "protections_checked": [
                {"gate": r.gate.value, "detail": r.detail}
                for r in evaluation.checked_and_did_not_fire
            ],
            "protections_unknown": [
                {"gate": r.gate.value, "detail": r.detail} for r in evaluation.could_not_be_checked
            ],
        }
    )


async def _record_first_flagged(
    session: AsyncSession, media_key: str, now: datetime, *, grace_days: int
) -> None:
    """Set the grace clock once, and never move it while the item stays condemned --
    but DO restart it when an item that had left the condemned set comes back.

    A transient Sonarr timeout that drops an item from one snapshot must not reset the
    clock -- otherwise the item can never age out and the grace period becomes unreachable.
    That is why ``first_flagged_at`` is not touched on an ordinary re-condemn.

    The other direction is just as important, and was the actual gap: an item condemned
    long ago, then *rescued* (watched, spared, or re-judged as protect) and later condemned
    again a full dormancy period afterwards, must serve a FRESH grace window. Its old
    ``first_flagged_at`` is far in the past, so grace_report would drop it straight into
    ``ready`` with no countdown and no Leaving Soon warning -- deleting on the second
    condemnation with zero grace. We detect the return by the gap since it was last seen
    condemned: when that gap exceeds the grace window (so it genuinely left, not just
    missed a snapshot to an outage), the clock restarts. ``last_seen_condemned_at`` exists
    for exactly this reset.
    """
    existing = await session.get(FirstFlagged, media_key)
    if existing is None:
        session.add(
            FirstFlagged(media_key=media_key, first_flagged_at=now, last_seen_condemned_at=now)
        )
        return

    last_seen = existing.last_seen_condemned_at
    gap = timedelta(days=grace_days)
    if last_seen is None or (now - last_seen) > gap:
        # It left the condemned set for longer than a whole grace window and has returned:
        # this is a new condemnation, so it earns a new window. Keying on the gap exceeding
        # the window (not a single missed snapshot) keeps a transient outage from resetting
        # a clock that was legitimately still running.
        existing.first_flagged_at = now
    existing.last_seen_condemned_at = now


# ---------------------------------------------------------------------------
# Gathering helpers
# ---------------------------------------------------------------------------


def _popularity_window(policy: PolicyBody) -> int:
    return next(
        (g.window_days for g in policy.gates if g.gate is GateId.SERVER_POPULARITY),
        365,
    )


def _as_year(value: Any) -> int | None:
    """A Plex row's release year, or ``None`` -- used only to disambiguate duplicate titles.

    Mirrors ``season_scan._as_year`` so the movie join reads years exactly as the show join
    does (Tautulli returns them as ints or numeric strings).
    """
    if isinstance(value, int | str) and str(value).isdigit():
        return int(value)
    return None


async def build_movie_index(
    tautulli: TautulliClient,
    plex: PlexClient | None,
    *,
    degrade: Callable[[str], None],
) -> identity.PlexIndex:
    """The Plex movie library, inverted for id / basename / title matching.

    The Tautulli ``get_library_media_info`` sweep is the **spine** -- it alone gives
    rating_key / title / year / added_at cheaply, and ``added_at`` must keep coming from
    there so dormancy stays byte-identical to the title-only era. The plexapi sweep then
    enriches each spine row with external ids + file basename, joined by rating key.

    A plexapi sweep that fails **degrades** the snapshot (rule #2: never let the id signal
    vanish and silently fall the whole library back to title-only) and leaves ids empty, so
    items still match by title+year but no run may execute against the result. A movie-only
    deployment with no Plex configured simply gets no enrichment -- its snapshot was already
    un-executable, since a real reap refuses without Plex.
    """
    guids: dict[int, tuple[identity.ExternalIds, str | None]] = {}
    if plex is not None:
        try:
            guids = await plex.library_guid_index(section_type="movie")
        except PlexError as exc:
            degrade(
                f"Plex GUID sweep failed ({exc}) -- id matching unavailable, snapshot un-executable"
            )

    items: list[identity.PlexItem] = []
    for library in await tautulli.libraries():
        if library.get("section_type") != "movie":
            continue
        section_id = int(library["section_id"])
        start = 0
        while True:
            page = await tautulli.library_media_info(section_id, start=start, length=1000)
            rows = page.get("data") or []
            for row in rows:
                # A row with no rating key cannot become a candidate's join (its rating_key
                # read would fail), so drop it here exactly as build_tv_index does.
                rk = row.get("rating_key")
                if rk is None:
                    continue
                rating_key = int(rk)
                ids, basename = guids.get(rating_key, (identity.ExternalIds(), None))
                items.append(
                    identity.PlexItem(
                        rating_key=rating_key,
                        title=str(row.get("title") or ""),
                        year=_as_year(row.get("year")),
                        added_at=from_epoch(row.get("added_at")),
                        ids=ids,
                        file_basename=basename,
                    )
                )
            if len(rows) < 1000:
                break
            start += 1000
    return identity.PlexIndex.build(items)


def _movie_file_basename(movie: Mapping[str, Any]) -> str | None:
    """The movie's file name, for the basename match tier.

    Radarr nests the file under ``movieFile``; the relative path is just the file name,
    which is what Plex's ``locations[0]`` basename also reduces to -- so the two compare
    equal across the mount-root difference. Falls back to the full path (still basenamed).
    """
    movie_file = movie.get("movieFile")
    if not isinstance(movie_file, dict):
        return None
    return identity.to_basename(movie_file.get("relativePath") or movie_file.get("path"))


def _summary(text: Any) -> str | None:
    """A trimmed overview. Kept short -- the card shows a couple of lines, not an essay."""
    if not isinstance(text, str):
        return None
    trimmed = text.strip()
    if not trimmed:
        return None
    return trimmed[:600]


def _raw_items(
    movies: list[dict[str, Any]],
    plex_index: identity.PlexIndex,
    instance_id: int,
    requested: dict[str, str] | None = None,
) -> list[RawItem]:
    requested = requested or {}
    items: list[RawItem] = []
    for movie in movies:
        if not movie.get("hasFile"):
            continue
        tmdb_id = int(movie["tmdbId"]) if movie.get("tmdbId") else None
        # Bind to Plex through the one shared resolver: external id (tmdb, then imdb) ->
        # file basename -> title+year, abstaining on any ambiguity or cross-tier conflict.
        # An abstain/unmatched leaves plex_rating_key None, which makes the item's facts
        # Unknown -> ABSTAIN, and the executor spares a keyless item.
        resolution = identity.resolve_movie(
            ids=identity.ExternalIds.of(imdb=movie.get("imdbId"), tmdb=movie.get("tmdbId")),
            title=str(movie.get("title") or ""),
            year=int(movie["year"]) if movie.get("year") else None,
            file_basename=_movie_file_basename(movie),
            index=plex_index,
        )
        matched = resolution.plex_item
        items.append(
            RawItem(
                # Identity is the *arr's, not Plex's. Plex rating keys are not stable
                # across library rebuilds or agent migrations.
                media_key=f"radarr:{instance_id}:{movie['id']}",
                title=str(movie.get("title") or ""),
                media_type="movie",
                size_bytes=int(movie.get("sizeOnDisk") or 0),
                imdb_id=movie.get("imdbId") or None,
                tmdb_id=tmdb_id,
                # added_at comes from the matched Plex item (Tautulli spine), preserving the
                # dormancy floor exactly as before.
                plex_rating_key=resolution.rating_key,
                added_at=matched.added_at if matched is not None else None,
                has_file=True,
                year=int(movie["year"]) if movie.get("year") else None,
                summary=_summary(movie.get("overview")),
                # poster_url is derived from the Plex rating key at read time (api/poster.py),
                # not stored -- the *arr's art is stale. See routes._candidate_out.
                requested_by=requested.get(requested_by.movie_key(tmdb_id) or ""),
                matched_by=resolution.matched_by,
                match_detail=resolution.detail,
                match_status=resolution.status,
            )
        )
    return items


async def _watch_stats(
    engine: AsyncEngine, *, rating_keys: set[int], window_days: int
) -> tuple[dict[int, datetime], dict[int, int], dict[int, int]]:
    """Last-played and distinct-watcher counts, from the local history mirror."""
    if not rating_keys:
        return {}, {}, {}

    # The cache is rebuildable and may be empty on a fresh install. Ensure the table
    # exists so a never-synced cache reads as "no plays" (which leaves dormancy Unknown,
    # and Unknown protects) rather than crashing the scan with 'no such table'.
    await history_sync.ensure_schema(engine)

    window_start = int((utcnow() - timedelta(days=window_days)).timestamp())

    async with engine.connect() as conn:
        last = {
            int(r.rating_key): from_epoch(r.last)
            for r in (
                await conn.execute(
                    text(
                        "SELECT rating_key, MAX(watched_at) AS last FROM watch_event "
                        "WHERE media_type = 'movie' GROUP BY rating_key"
                    )
                )
            ).all()
        }
        window = {
            int(r.rating_key): int(r.n)
            for r in (
                await conn.execute(
                    text(
                        "SELECT rating_key, COUNT(DISTINCT user_id) AS n FROM watch_event "
                        "WHERE media_type = 'movie' AND watched_at >= :since "
                        "GROUP BY rating_key"
                    ),
                    {"since": window_start},
                )
            ).all()
        }
        ever = {
            int(r.rating_key): int(r.n)
            for r in (
                await conn.execute(
                    text(
                        "SELECT rating_key, COUNT(DISTINCT user_id) AS n FROM watch_event "
                        "WHERE media_type = 'movie' GROUP BY rating_key"
                    )
                )
            ).all()
        }

    return (
        {k: v for k, v in last.items() if v is not None},
        window,
        ever,
    )


async def sync_protection_lists(
    engine: AsyncEngine,
    *,
    radarrs: Sequence[RadarrClient] = (),
    sonarrs: Sequence[Any] = (),
    movie_keep_tags: tuple[str, ...] = ("reaper-keep",),
    movie_keep_match: str = "any",
    tv_keep_tags: tuple[str, ...] = ("reaper-keep",),
    tv_keep_match: str = "any",
    plex_server: object | None = None,
    section_name: str = "Movies",
    collection_name: str = "Never Reap",
    include_top_250: bool = True,
) -> dict[str, int | str]:
    """Refresh every protection list, **before a scan reads them.**

    This is the wiring that makes the list-based protections actually fire. The
    providers and the membership tables have always existed, but nothing populated them
    at scan time -- so a "Never Reap" collection, a ``reaper-keep`` tag, and the IMDb
    Top 250 were all silently empty, and a protection that is empty is a protection that
    does not protect. A whitelist that quietly fails open is the worst kind of bug this
    tool can have.

    Three sources, each optional and each failing *soft*:

    * **IMDb Top 250** -- no auth, always available. A curated (soft) hard-gate list.
    * **``reaper-keep`` tag** -- one per Radarr instance. A whitelist.
    * **"Never Reap" Plex collection** -- curated in the Plex app itself. A whitelist.

    A provider that finds nothing is not an error (the owner may not have made the tag
    or collection yet). A provider that *fails* is recorded against its slug and does not
    abort the others -- but the caller can see which lists are stale, and a scan that
    relied on a failed whitelist should treat itself as degraded rather than delete
    something the list would have saved. The atomic-swap in ``lists.sync`` guarantees a
    failed refresh leaves the previous membership intact rather than emptying it.
    """
    synced: dict[str, int | str] = {}

    async def _run(provider: lists.ListProvider, *, kind: lists.ListKind) -> None:
        try:
            synced[provider.slug] = await lists.sync(
                engine, provider, mode=lists.ListMode.HARD, kind=kind
            )
        except Exception as exc:
            synced[provider.slug] = f"error: {exc}"
            log.warning("lists.sync_failed", slug=provider.slug, error=str(exc))

    if include_top_250:
        await _run(lists.ImdbTop250(), kind=lists.ListKind.CURATED)

    # The keep-list, configurable per media type: movies read the owner's Radarr keep-tags,
    # TV reads their Sonarr keep-tags, each matched ANY/ALL. A title carrying a keep-tag is
    # spared outright. Empty tag list -> nothing synced (the protection simply does not fire).
    movie_match: Literal["any", "all"] = "all" if movie_keep_match == "all" else "any"
    tv_match: Literal["any", "all"] = "all" if tv_keep_match == "all" else "any"
    for radarr in radarrs:
        if movie_keep_tags:
            await _run(
                lists.ArrTagRule(radarr, tuple(movie_keep_tags), movie_match),
                kind=lists.ListKind.WHITELIST,
            )
    for sonarr in sonarrs:
        if tv_keep_tags:
            await _run(
                lists.ArrTagRule(sonarr, tuple(tv_keep_tags), tv_match),
                kind=lists.ListKind.WHITELIST,
            )

    if plex_server is not None:
        await _run(
            lists.PlexCollection(
                server=plex_server, section_name=section_name, collection_name=collection_name
            ),
            kind=lists.ListKind.WHITELIST,
        )

    return synced


async def protection_sync_degradations(
    engine: AsyncEngine, synced: Mapping[str, int | str]
) -> list[str]:
    """Which failed protection-list syncs must degrade the snapshot.

    ``sync_protection_lists`` records a failed provider as ``"error: ..."`` and then only
    the caller can decide what to do about it. The atomic swap in ``lists.sync`` keeps the
    PRIOR membership on a failed refresh, so a transient failure of a list that already has
    members is bounded -- the previous keep-list still protects -- and need not degrade.

    The dangerous case, and the one this guards, is a failed **whitelist** with NO
    membership to fall back on: a first scan, or a newly-added keep-list that has never
    synced once. The WhitelistGate then reads an empty keep-list, fails to fire, and the
    snapshot -- unless we degrade it here -- is fully executable against a keep-list that
    was meant to save those very titles. A whitelist failing open is the worst kind of bug
    this tool can have, so only those slugs degrade (a curated soft-list that fails merely
    loses a scoring nudge; it never unprotects a kept title).
    """
    await lists.ensure_schema(engine)
    reasons: list[str] = []
    async with engine.connect() as conn:
        for slug, outcome in synced.items():
            if not (isinstance(outcome, str) and outcome.startswith("error:")):
                continue
            kind = (
                await conn.execute(
                    text("SELECT kind FROM protection_list WHERE slug = :slug"),
                    {"slug": slug},
                )
            ).scalar_one_or_none()
            # Only whitelist-kind lists fail *open* when empty. A missing row (never synced
            # even once) is treated as whitelist-shaped -- fail closed rather than guess.
            if kind is not None and str(kind) != lists.ListKind.WHITELIST.value:
                continue
            members = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM protection_list_item WHERE slug = :slug"),
                    {"slug": slug},
                )
            ).scalar_one()
            if int(members or 0) == 0:
                reasons.append(
                    f"protection list '{slug}' failed to sync and its keep-list is empty -- "
                    "a scan must not reap titles the list would have saved"
                )
    return reasons


async def candidates(
    session: AsyncSession, snapshot_id: int, *, verdict: str | None = None
) -> list[Candidate]:
    stmt = select(Candidate).where(Candidate.snapshot_id == snapshot_id)
    if verdict:
        stmt = stmt.where(Candidate.verdict == verdict)
    stmt = stmt.order_by(Candidate.score.desc(), Candidate.size_bytes.desc())
    return list((await session.execute(stmt)).scalars().all())
