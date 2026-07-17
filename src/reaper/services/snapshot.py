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

import asyncio
import json
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from reaper.aio import gather_reaped, reap
from reaper.clients.arr import RadarrClient
from reaper.clients.base import IntegrationError
from reaper.clients.plex import PlexClient
from reaper.clients.tautulli import TautulliClient
from reaper.clock import from_epoch, utcnow
from reaper.db.models import Candidate, FirstFlagged, Snapshot
from reaper.engine import identity
from reaper.engine.gates import PROTECT, Evaluation, Facts, Gate, GateId, GateResult, evaluate_all
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.engine.policy import PolicyBody, combine_hashes
from reaper.engine.signals import Score, SignalConfig, SignalId, score
from reaper.engine.verdict import STRUCTURAL_GATES, decide_verdict
from reaper.ratings import Rating, from_radarr
from reaper.services import (
    history_sync,
    library_index,
    lists,
    requested_by,
    season_scan,
    whitelist,
)
from reaper.services.display_meta import build_ratings_json, dataset_entry, normalize_resolution
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
    # Every Plex listing the bind covers when the file is listed more than once (includes
    # plex_rating_key). Watch reads must consult all of them; empty for a normal bind.
    merged_rating_keys: tuple[int, ...] = ()
    # The matched Plex item's imdb id -- a fallback rating key when Radarr's imdbId is
    # missing or does not resolve in the IMDb dataset.
    plex_imdb_id: str | None = None
    # Metadata authorable in custom rules (the weighting feature). Captured from the
    # Radarr payload the scan already holds -- no extra fetch.
    genres: tuple[str, ...] = ()
    quality: str | None = None
    # Display metadata frozen onto the candidate (services.display_meta). Plex first,
    # the Radarr payload as fallback; none of it touches the verdict.
    video_resolution: str | None = None
    content_rating: str | None = None
    runtime_minutes: int | None = None
    plex_ratings: tuple[Rating, ...] = ()
    arr_ratings: tuple[Rating, ...] = ()


def build_facts(
    item: RawItem,
    context: ScanContext,
    *,
    membership_index: lists.MembershipIndex,
    imdb: dict[str, ImdbRating],
    last_played: dict[int, datetime],
    watchers_window: dict[int, int],
    watchers_all_time: dict[int, int],
    whitelisted: set[str],
    request_index: requested_by.RequestIndex | None = None,
) -> Facts:
    """Assemble one item's evidence.

    Note how often ``Unknown`` appears. Every one of them is a place where a naive
    implementation would have written ``0``, ``[]`` or ``False`` -- and every one of
    those would have quietly condemned an item we know nothing about.
    """
    rating_key = item.plex_rating_key
    # The two no-key states are DIFFERENT stories and the why-panel must not conflate
    # them: "unmatched" means Plex has no such item as far as Reaper can tell; "ambiguous"
    # means Plex has MORE than one (a 1080p and a 4K copy sharing one TMDB id) and Reaper
    # refused to guess whose watch history to read. Both keep the file; only the words
    # shown to the owner differ.
    no_key_reason = (
        "more than one Plex item matches this title"
        if item.match_status is identity.MatchStatus.AMBIGUOUS
        else "Plex has not matched this item"
    )

    # --- dormancy -----------------------------------------------------------
    # THE derived field. "Days since last play" is null for exactly the items we care
    # about most, and coercing that null to epoch 0 reads as ~20,600 days unwatched --
    # the maximum condemnation pressure, for the item we know least about.
    dormancy: Observation[float]
    if rating_key is None:
        dormancy = Unknown(reason=no_key_reason, source="plex")
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
        recent = Unknown(reason=no_key_reason, source="plex")
        all_time = Unknown(reason=no_key_reason, source="plex")
    else:
        recent = Known(value=watchers_window.get(rating_key, 0), source="tautulli")
        all_time = Known(value=watchers_all_time.get(rating_key, 0), source="tautulli")

    # --- ratings ------------------------------------------------------------
    rating: Observation[int]
    votes: Observation[int]
    # Radarr's imdbId first, then the Plex-matched imdb id as a fallback (Radarr may lack
    # it, or carry one the IMDb dataset doesn't have). The shared helper is also what the
    # display ratings row reads, so the signal and the row can never show different numbers.
    entry = dataset_entry(imdb, item.imdb_id, item.plex_imdb_id)
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
    memberships = membership_index.lookup(imdb_id=item.imdb_id, tmdb_id=item.tmdb_id)
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
        # A merged bind covers several listings of one file; someone streaming ANY of
        # them is streaming this very file, so the veto checks every key in the group.
        watch_keys = item.merged_rating_keys or ((rating_key,) if rating_key else ())
        streaming = Known(
            value=any(key in context.active_rating_keys for key in watch_keys),
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
        # --- fields authorable in custom rules ---------------------------------
        requested=(
            request_index.movie_requested(item.tmdb_id)
            if request_index is not None
            else Unknown(reason="requests not loaded", source="seerr")
        ),
        genres=(
            Known(value=", ".join(item.genres), source="radarr")
            if item.genres
            else Absent(source="radarr")
        ),
        release_age_days=_release_age_days(item.year),
        quality=(
            Known(value=item.quality, source="radarr") if item.quality else Absent(source="radarr")
        ),
        show_ended=Absent(source="radarr"),  # a movie is not a series
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
    request_index: requested_by.RequestIndex | None = None,
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

    # Manual spares are applied via the override, not the whitelist gate, so the gate is left to
    # the *arr-tag / collection whitelists alone. An empty set keeps that path tag-only.
    tag_only_whitelist: set[str] = set()
    # Every enabled protection-list row, loaded once and shared by the movie and season
    # judges. The judge loops below ask "which lists contain this item?" for every movie
    # and every show; answering each from SQLite made the loop's runtime scale with the
    # library. One load, then dict hits.
    membership_index = await lists.load_membership_index(engine)

    # ---- fan out across the independent sources -----------------------------
    # Everything past the activity read touches DIFFERENT services -- the Plex + Tautulli
    # movie index, each Radarr's movie list, and the whole TV season gather (Sonarr, Plex,
    # Tautulli again) -- so they run concurrently and the gather takes as long as its
    # slowest source instead of the sum of all of them. The freeze-then-judge contract is
    # untouched: nothing is scored until every one of these has completed (or degraded),
    # exactly as before; only the waiting overlaps.
    emit(Progress("gathering", 2, 5, "movie and TV libraries"))

    # Every task the fan-out creates goes through _spawn, so the reap on failure below
    # covers all of them by construction -- a future branch cannot be forgotten.
    fanned_out: list[asyncio.Task[Any]] = []

    def _spawn[T](coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        task = asyncio.create_task(coro)
        fanned_out.append(task)
        return task

    # A read-only extension of the same gather. It reads Sonarr, resolves prunable
    # seasons to Plex, and reads their watch history from the same local mirror. A
    # movie-only deployment (no Sonarr) skips it entirely.
    season_task: asyncio.Task[list[season_scan.SeasonJudgement]] | None = None
    if sonarrs:
        activity_degraded = "tautulli-activity" in " ".join(context.degraded_reasons)
        season_task = _spawn(
            season_scan.gather(
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
                request_index=request_index,
                keep_last_scope=tv_policy.keep_last_scope,
                season_lookahead=tv_policy.season_lookahead,
                membership_index=membership_index,
            )
        )

    async def _movies_from(source: RadarrSource) -> list[dict[str, Any]] | None:
        try:
            movies = await source.client.movies()
        except IntegrationError as exc:
            # One instance down must not silently shrink the library. Degrade, so no run
            # may execute against a snapshot that is missing an entire *arr.
            context.degrade(f"radarr '{source.name}' unreachable: {exc}")
            return None
        log.info("snapshot.radarr", instance=source.name, movies=len(movies))
        return movies

    index_task = _spawn(build_movie_index(tautulli, plex, degrade=context.degrade))
    movie_tasks = [_spawn(_movies_from(source)) for source in radarrs]

    items: list[RawItem] = []
    season_judgements: list[season_scan.SeasonJudgement] = []
    try:
        # Awaited in the sequential code's order, so the first failure to surface is the
        # same one it would have raised then; the except below reaps every other task.
        plex_index = await index_task
        for source, movie_task in zip(radarrs, movie_tasks, strict=True):
            movies = await movie_task
            if movies is None:
                continue
            items.extend(_raw_items(movies, plex_index, source.instance_id, requested))

        emit(Progress("gathering", 4, 5, "IMDb ratings"))
        # Look up by BOTH Radarr's imdbId and the matched Plex item's imdb id, so a film
        # whose Radarr record lacks (or has a wrong) imdbId still gets its rating when
        # Plex knows it.
        imdb_ids = [x for i in items for x in (i.imdb_id, i.plex_imdb_id) if x]
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
        # A merged bind is one file listed several times in Plex; its plays are split
        # across the listings' rating keys. Fold each group's stats onto its canonical
        # key, or the item would under-count its own watching -- the direction that
        # condemns.
        await _fold_merged_watch_stats(
            engine,
            groups={
                i.plex_rating_key: i.merged_rating_keys
                for i in items
                if i.plex_rating_key is not None and i.merged_rating_keys
            },
            window_days=_popularity_window(movie_policy),
            last_played=last_played,
            watchers_window=watchers_window,
            watchers_all_time=watchers_all_time,
        )
        # The owner's manual overrides -- ``media_key -> "spare" | "reap"`` -- loaded once
        # and applied to every item's verdict. A spared file is judged PROTECT rather than
        # surfacing in "would delete" again; a reaped one is forced onto the list (short
        # of a hard safety gate). Keys may be a show's, in which case the decision applies
        # to all of its seasons.
        override_map = await whitelist.overrides(session)

        if season_task is not None:
            emit(Progress("gathering", 4, 5, "TV seasons from Sonarr"))
            season_judgements = await season_task
    except BaseException:
        # A failure on any branch aborts the scan, exactly as it did sequentially -- but
        # the surviving branches are reaped first (cancelled, drained, late failures
        # logged), so nothing keeps reading from sources after the scan is already dead
        # and no task's failure goes unobserved. Every task is in fanned_out because
        # every task was created by _spawn.
        await reap(fanned_out)
        raise

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

    condemned_keys: list[str] = []

    for index, item in enumerate(items):
        if index % 100 == 0:
            emit(Progress("scoring", index, total, item.title))
            # The judge is pure computation now (no per-item queries), so without this
            # the loop would hold the event loop for the whole scoring phase -- freezing
            # the very progress endpoint the emit above feeds.
            await asyncio.sleep(0)

        facts = build_facts(
            item,
            context,
            membership_index=membership_index,
            imdb=imdb,
            last_played=last_played,
            watchers_window=watchers_window,
            watchers_all_time=watchers_all_time,
            whitelisted=tag_only_whitelist,
            request_index=request_index,
        )
        verdict = _judge_item(
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
                tmdb_id=item.tmdb_id,
                # Radarr's id first, the Plex-matched one as fallback -- the same
                # precedence the dataset lookup uses.
                imdb_id=item.imdb_id or item.plex_imdb_id,
                video_resolution=item.video_resolution,
                content_rating=item.content_rating,
                runtime_minutes=item.runtime_minutes,
                # The same dataset entry build_facts froze into the scoring signal, so
                # the ratings row can never disagree with the signal text beside it.
                ratings_json=build_ratings_json(
                    dataset_entry(imdb, item.imdb_id, item.plex_imdb_id),
                    item.plex_ratings,
                    item.arr_ratings,
                ),
            ),
            matched_by=item.matched_by,
            match_detail=item.match_detail,
            match_status=item.match_status,
            merged_rating_keys=item.merged_rating_keys,
            override=whitelist.effective_override(item.media_key, override_map),
        )
        if verdict == "condemn":
            condemned += 1
            condemned_keys.append(item.media_key)

    # Seasons run through the SAME judge: the season-pruning guard is merged in as an
    # extra gate result, so a protected season is protected by a gate exactly as a
    # streamed movie is, and the why-panel renders both identically.
    for offset, judgement in enumerate(season_judgements):
        if offset % 100 == 0:
            emit(Progress("scoring", len(items) + offset, total, judgement.title))
            await asyncio.sleep(0)  # keep the event loop live; see the movie loop above
        verdict = _judge_item(
            session,
            snapshot_id=snapshot.id,
            media_key=judgement.media_key,
            plex_rating_key=judgement.plex_rating_key,
            # A season's poster is the SHOW's, not the season's -- shows always have one.
            poster_rating_key=judgement.poster_rating_key,
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
                tmdb_id=judgement.tmdb_id,
                imdb_id=judgement.imdb_id,
                title_slug=judgement.title_slug,
                content_rating=judgement.content_rating,
                runtime_minutes=judgement.runtime_minutes,
                ratings_json=judgement.ratings_json,
            ),
            matched_by=judgement.matched_by,
            match_detail=judgement.match_detail,
            match_status=judgement.match_status,
            extra_results=(judgement.guard_result,),
            override=whitelist.effective_override(judgement.media_key, override_map),
        )
        if verdict == "condemn":
            condemned += 1
            condemned_keys.append(judgement.media_key)

    # Grace clocks for everything condemned this run, in one batched pass -- the same
    # decision per key as _record_first_flagged, without a database round trip per item.
    await _record_first_flagged_bulk(session, condemned_keys, now, grace_days=grace_days)

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
    # Deep-link coordinates (the *arr web routes key on these, not on internal ids)
    # and the frozen display metadata. See services.display_meta and deep_links.
    tmdb_id: int | None = None
    imdb_id: str | None = None
    title_slug: str | None = None
    video_resolution: str | None = None
    content_rating: str | None = None
    runtime_minutes: int | None = None
    ratings_json: str | None = None


#: The "no display fields" default, as a singleton so it is not constructed per call.
_NO_DISPLAY = Display()


def _judge_item(
    session: AsyncSession,
    *,
    snapshot_id: int,
    media_key: str,
    plex_rating_key: int | None,
    poster_rating_key: int | None = None,
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
    merged_rating_keys: tuple[int, ...] = (),
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
    item_score = score(
        signals,
        facts,
        custom_condemn=policy.custom_signal_configs(),
        keeps=policy.keep_configs(),
        window_days=window_days,
    )

    score_value = round(item_score.value)
    coverage_bp = round(item_score.coverage * 10_000)

    verdict = _verdict(evaluation, score_value, coverage_bp, policy, override=override)
    # The grace clock for a condemned item is set by the CALLER, batched across the whole
    # run (_record_first_flagged_bulk) -- one query for every condemned key instead of a
    # read per item. The decision per key is unchanged: see _apply_first_flag.

    session.add(
        Candidate(
            snapshot_id=snapshot_id,
            media_key=media_key,
            plex_rating_key=plex_rating_key,
            poster_rating_key=poster_rating_key,
            title=title,
            media_type=media_type,
            size_bytes=size_bytes,
            year=display.year,
            summary=display.summary,
            poster_url=display.poster_url,
            requested_by=display.requested_by,
            # Suggestion fields for the rule editors' datalists, from evidence already in
            # hand. Facts carries genres comma-joined (genre names never contain ", ").
            genres_json=(
                json.dumps(facts.genres.value.split(", "))
                if isinstance(facts.genres, Known)
                else None
            ),
            quality=(facts.quality.value if isinstance(facts.quality, Known) else None),
            group_key=display.group_key,
            group_title=display.group_title,
            tmdb_id=display.tmdb_id,
            imdb_id=display.imdb_id,
            title_slug=display.title_slug,
            video_resolution=display.video_resolution,
            content_rating=display.content_rating,
            runtime_minutes=display.runtime_minutes,
            ratings_json=display.ratings_json,
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
                merged_rating_keys=merged_rating_keys,
            ),
            created_at=now,
        )
    )
    return verdict


def _verdict(
    evaluation: Evaluation,
    score_value: int,
    coverage_bp: int,
    policy: PolicyBody,
    *,
    override: str | None = None,
) -> str:
    """The scan's adapter onto the ONE decision function, ``engine.verdict``.

    Takes the **stored** integers, not the underlying floats, so that this path and the
    simulator -- which has only the stored integers to work with -- cannot reach
    different verdicts for the same item under the same policy. Two code paths that
    answer the same question must answer it the same way, and the cheapest way to
    guarantee that is to give them the same function and the same inputs.

    A manual ``"reap"`` override forces CONDEMN -- the owner looked and decided -- but never
    past a hard safety gate (streaming now, unmanaged) or a protection that could not be
    checked; those still protect. A ``"spare"`` override arrives as an extra PROTECT result and
    so is already handled by ``evaluation.protected``.
    """
    return decide_verdict(
        protected=evaluation.protected,
        blocked=evaluation.blocked,
        safety_protected=any(r.fired and r.gate in STRUCTURAL_GATES for r in evaluation.results),
        score=score_value,
        coverage_bp=coverage_bp,
        condemn_at=policy.condemn_at,
        coverage_floor_bp=policy.coverage_floor_bp,
        override=override,
    )


def _explain(
    evaluation: Evaluation,
    item_score: Score,
    policy: PolicyBody,
    *,
    plex_rating_key: int | None = None,
    matched_by: identity.MatchedBy | None = None,
    match_detail: str | None = None,
    match_status: identity.MatchStatus | None = None,
    merged_rating_keys: tuple[int, ...] = (),
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
            # The condemnation subtotal before any keep discount -- so the panel can show
            # "condemnation 67, keep -15, final 52" the way the operator expects from Radarr.
            "base_score": round(item_score.base_value, 1),
            "keep_discount": round(item_score.keep_discount, 1),
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
                # Every listing a merged bind covers (one file listed several times in
                # Plex). The executor's live interlocks re-read THIS list, so the keys
                # they protect are exactly the keys the owner was shown.
                "merged_rating_keys": (list(merged_rating_keys) if merged_rating_keys else None),
            },
            "signals": [
                {
                    # Built-in signals carry a SignalId; a custom rule carries its own name.
                    "id": r.signal.value if isinstance(r.signal, SignalId) else r.signal,
                    "contribution": round(r.pressure, 1),
                    "weight": r.weight,
                    "detail": r.detail,
                    "evaluated": r.evaluated,
                }
                for r in item_score.results
            ],
            "keeps": [
                {
                    "name": k.name,
                    "discount": round(k.discount, 1),
                    "max_discount": k.max_discount,
                    "detail": k.detail,
                    "evaluated": k.evaluated,
                }
                for k in item_score.keep_results
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


def _apply_first_flag(
    session: AsyncSession,
    existing: FirstFlagged | None,
    media_key: str,
    now: datetime,
    *,
    grace_days: int,
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

    This is THE decision, shared by the one-key and batched recorders below so the two
    can never drift apart on a safety window.
    """
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


async def _record_first_flagged_bulk(
    session: AsyncSession, media_keys: Sequence[str], now: datetime, *, grace_days: int
) -> None:
    """Grace bookkeeping for every key condemned in one run, in one read.

    The ONLY write path to the grace clock, applying :func:`_apply_first_flag` per key;
    the existing rows arrive in chunked ``IN`` queries instead of a ``session.get`` (and
    its autoflush) per condemned item.
    """
    keys = list(dict.fromkeys(media_keys))
    if not keys:
        return
    existing: dict[str, FirstFlagged] = {}
    for start in range(0, len(keys), 500):
        chunk = keys[start : start + 500]
        rows = (
            await session.execute(select(FirstFlagged).where(FirstFlagged.media_key.in_(chunk)))
        ).scalars()
        for row in rows:
            existing[row.media_key] = row
    for key in keys:
        _apply_first_flag(session, existing.get(key), key, now, grace_days=grace_days)


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

    One shared implementation with ``season_scan.build_tv_index`` -- see
    ``services.library_index`` for the spine + sweep design and its failure
    semantics. A movie-only deployment with no Plex configured simply gets no
    enrichment; its snapshot was already un-executable, since a real reap refuses
    without Plex.
    """
    return await library_index.build_index(tautulli, plex, section_type="movie", degrade=degrade)


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


def _movie_file_size(movie: Mapping[str, Any]) -> int | None:
    """The exact byte count Radarr records for the movie's file, or ``None``.

    The corroborator that tells apart several Plex listings carrying the same file name:
    an exact byte match is the same file (or a bit-identical copy of it); a mismatch is a
    different file. Deliberately ``movieFile.size`` and not ``sizeOnDisk``, which can
    include extras -- the comparison must be file-to-file. Zero or missing is unknown.
    """
    movie_file = movie.get("movieFile")
    if not isinstance(movie_file, dict):
        return None
    size = movie_file.get("size")
    return int(size) if isinstance(size, int) and size > 0 else None


def _summary(text: Any) -> str | None:
    """A trimmed overview. Kept short -- the card shows a couple of lines, not an essay."""
    if not isinstance(text, str):
        return None
    trimmed = text.strip()
    if not trimmed:
        return None
    return trimmed[:600]


def _movie_quality(movie: Mapping[str, Any]) -> str | None:
    """The file's quality name (e.g. "Bluray-1080p"), for the ``quality`` rule field."""
    movie_file = movie.get("movieFile")
    if not isinstance(movie_file, dict):
        return None
    name = (((movie_file.get("quality") or {}).get("quality")) or {}).get("name")
    return str(name) if name else None


def _release_age_days(year: int | None) -> Observation[float]:
    """Days since release, from the release year. ``Absent`` when the year is unknown.

    Derived (not the raw year) because "how old" composes with "how long unwatched"; the
    year-granularity is deliberate -- a finer release date is not worth a second fetch.
    """
    if not year:
        return Absent(source="radarr")
    try:
        age = (utcnow().date() - date(year, 1, 1)).days
    except (ValueError, OverflowError):
        return Absent(source="radarr")
    return Known(value=float(max(0, age)), source="radarr")


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
            file_size=_movie_file_size(movie),
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
                merged_rating_keys=resolution.merged_rating_keys,
                plex_imdb_id=matched.ids.imdb if matched is not None else None,
                genres=tuple(str(g) for g in (movie.get("genres") or []) if g),
                quality=_movie_quality(movie),
                # Display metadata: Plex's answer first (it describes the file Plex
                # serves), the Radarr payload filling the gaps. Never a verdict input.
                video_resolution=normalize_resolution(
                    matched.video_resolution if matched is not None else None,
                    _movie_quality(movie),
                ),
                content_rating=matched.content_rating if matched is not None else None,
                runtime_minutes=matched.runtime_minutes if matched is not None else None,
                plex_ratings=matched.ratings if matched is not None else (),
                arr_ratings=tuple(from_radarr(movie.get("ratings"))),
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


async def _fold_merged_watch_stats(
    engine: AsyncEngine,
    *,
    groups: Mapping[int, tuple[int, ...]],
    window_days: int,
    last_played: dict[int, datetime],
    watchers_window: dict[int, int],
    watchers_all_time: dict[int, int],
) -> None:
    """Fold each merged listing group's watch stats onto its canonical rating key.

    A merged group is one file listed several times in Plex (see
    ``identity.MatchedBy.MERGED_LISTINGS``); its plays are split across the listings'
    rating keys. Exact, not additive: distinct watchers are counted over the union of the
    group's events, so one person who played the file through two listings still counts
    once, and last-played is the latest play through any listing. Only the canonical
    keys' entries are rewritten; every other item's stats are untouched.
    """
    all_keys = sorted({key for group in groups.values() for key in group})
    if not all_keys:
        return
    window_start = int((utcnow() - timedelta(days=window_days)).timestamp())
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT rating_key, user_id, MAX(watched_at) AS last FROM watch_event "
                    "WHERE media_type = 'movie' AND rating_key IN :keys "
                    "GROUP BY rating_key, user_id"
                ).bindparams(bindparam("keys", expanding=True)),
                {"keys": all_keys},
            )
        ).all()
    per_key: dict[int, list[Any]] = {}
    for row in rows:
        per_key.setdefault(int(row.rating_key), []).append(row)
    for canonical, group in groups.items():
        group_rows = [row for key in group for row in per_key.get(key, [])]
        if not group_rows:
            continue  # no plays anywhere in the group: leave the base (empty) stats be
        played = from_epoch(max(int(row.last) for row in group_rows))
        if played is not None:
            last_played[canonical] = played
        # A user's latest play being inside the window is the same as having any play
        # inside it. Rows with no user still move last-played but never count a watcher,
        # matching COUNT(DISTINCT user_id) in the base queries.
        watchers_window[canonical] = len(
            {
                row.user_id
                for row in group_rows
                if row.user_id is not None and int(row.last) >= window_start
            }
        )
        watchers_all_time[canonical] = len(
            {row.user_id for row in group_rows if row.user_id is not None}
        )


async def sync_protection_lists(
    engine: AsyncEngine,
    *,
    radarrs: Sequence[RadarrSource] = (),
    sonarrs: Sequence[season_scan.SonarrSource] = (),
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

    # Every provider reads a different service, and each one already fails soft on its
    # own, so they refresh concurrently -- the whole pass takes as long as the slowest
    # provider instead of the sum. The database writes inside lists.sync stay atomic per
    # list; SQLite allows one writer at a time, and the busy_timeout pragma (see
    # db/session.py) queues the brief overlapping writes -- each provider's write is a
    # few hundred rows, far inside that budget.
    runs: list[Coroutine[Any, Any, None]] = []
    if include_top_250:
        runs.append(_run(lists.ImdbTop250(), kind=lists.ListKind.CURATED))

    # The keep-list, configurable per media type: movies read the owner's Radarr keep-tags,
    # TV reads their Sonarr keep-tags, each matched ANY/ALL. A title carrying a keep-tag is
    # spared outright. Empty tag list -> nothing synced (the protection simply does not fire).
    # Each instance is its OWN list (the instance id is part of the slug): with a shared
    # slug, two same-service instances would take turns erasing each other's keep-tagged
    # titles, since a sync atomically replaces its slug's whole membership.
    movie_match: Literal["any", "all"] = "all" if movie_keep_match == "all" else "any"
    tv_match: Literal["any", "all"] = "all" if tv_keep_match == "all" else "any"
    for radarr in radarrs:
        if movie_keep_tags:
            runs.append(
                _run(
                    lists.ArrTagRule(
                        radarr.client,
                        tuple(movie_keep_tags),
                        movie_match,
                        instance_id=radarr.instance_id,
                        instance_name=radarr.name,
                    ),
                    kind=lists.ListKind.WHITELIST,
                )
            )
    for sonarr in sonarrs:
        if tv_keep_tags:
            runs.append(
                _run(
                    lists.ArrTagRule(
                        sonarr.client,
                        tuple(tv_keep_tags),
                        tv_match,
                        instance_id=sonarr.instance_id,
                        instance_name=sonarr.name,
                    ),
                    kind=lists.ListKind.WHITELIST,
                )
            )

    if plex_server is not None:
        runs.append(
            _run(
                lists.PlexCollection(
                    server=plex_server, section_name=section_name, collection_name=collection_name
                ),
                kind=lists.ListKind.WHITELIST,
            )
        )

    # gather_reaped, not bare gather: _run swallows every per-provider failure, so only
    # something unexpected (a cache-database fault) can raise here -- and when it does,
    # the surviving providers are cancelled and drained rather than left refreshing
    # lists for a scan that is already dead.
    await gather_reaped(*runs)
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
