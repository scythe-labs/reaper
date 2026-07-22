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
import time
from collections import Counter
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from reaper.aio import gather_reaped, reap
from reaper.clients.arr import RadarrClient
from reaper.clients.base import IntegrationError
from reaper.clients.plex import PlexClient
from reaper.clients.tautulli import TautulliClient
from reaper.clock import from_epoch, utcnow
from reaper.db.models import Candidate, FirstFlagged, SizeSource, Snapshot
from reaper.engine import facts_codec, identity
from reaper.engine.gates import PROTECT, Evaluation, Facts, Gate, GateId, GateResult, evaluate_all
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.engine.policy import PolicyBody, combine_hashes
from reaper.engine.signals import (
    CustomSignalConfig,
    KeepConfig,
    Score,
    SignalConfig,
    SignalId,
    score,
)
from reaper.engine.verdict import STRUCTURAL_GATES, decide_verdict
from reaper.ratings import Rating, RatingSource, from_radarr, merge_by_source
from reaper.services import (
    history_sync,
    library_index,
    lists,
    requested_by,
    season_scan,
    whitelist,
)
from reaper.services.display_meta import (
    build_ratings_json,
    dataset_entry,
    dataset_lookup,
    normalize_resolution,
)
from reaper.services.imdb_dataset import DatasetDegradedError, ImdbRating, ImdbRatings

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Progress:
    """One step of a scan's progress, polled by the browser via ``GET /api/scan/status``."""

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
    """One movie, as the *arr sees it, before any judgment."""

    media_key: str
    title: str
    media_type: str
    size_bytes: int | None
    """Bytes on disk, or ``None`` when the *arr reported a file but no size for it.

    ``None`` is not zero. A partial payload (``hasFile`` true, ``sizeOnDisk`` missing)
    carried as ``0`` becomes an affirmative measurement: it reads as maximum pressure on
    a size signal, and it silently withdraws any "keep large files" rule. See
    ``tests/test_fact_layer_states.py``.
    """

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
    # The Plex library (section) the matched item lives in. Display/filter only.
    library: str | None = None
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
    entry, looked_up = dataset_lookup(imdb, item.imdb_id, item.plex_imdb_id)
    if entry is not None:
        rating = Known(value=int(entry.average_rating * 10), source="imdb")
        votes = Known(value=int(entry.num_votes), source="imdb")
    elif looked_up:
        # Absent, not Unknown: we looked and this title genuinely has no IMDb rating.
        # (A *degraded* dataset is different, and is caught upstream -- it degrades the
        # whole snapshot rather than silently unprotecting every film.)
        rating = Absent(source="imdb")
        votes = Absent(source="imdb")
    else:
        # We never got to ask: no imdbId from Radarr and no Plex match to borrow one from.
        # Absent here would tell the keep lane "this title has no IMDb rating", withdrawing
        # every rating-based keep while coverage still read 100%. See dataset_lookup.
        no_id = Unknown(reason="no IMDb id to look up", source="imdb")
        rating = no_id
        votes = no_id

    # The multi-source keep gate reads this. The IMDb dataset value goes first (it carries
    # the authoritative vote count the score already uses), then Radarr's ratings object
    # (IMDb/TMDb/RT-critic/Metacritic), then Plex's two slots (which can add the RT
    # audience score). merge_by_source keeps one per source, first writer winning, and
    # drops any UNKNOWN-source value -- so a protection is never decided on a number we
    # cannot interpret. No extra fetch: all three are already frozen on the item.
    dataset_rating = (
        [
            Rating(
                source=RatingSource.IMDB,
                value=entry.average_rating,
                votes=int(entry.num_votes),
                provider="imdb-dataset",
            )
        ]
        if entry is not None
        else []
    )
    rating_set = merge_by_source(dataset_rating, list(item.arr_ratings), list(item.plex_ratings))

    # --- lists --------------------------------------------------------------
    # Whitelist and curated are DIFFERENT reasons to keep a file, and collapsing them
    # would tell the owner "whitelisted" about a film they never touched. The why-panel
    # must be able to say which.
    memberships = membership_index.lookup(
        media_type="movie", imdb_id=item.imdb_id, tmdb_id=item.tmdb_id
    )
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
        size_bytes=(
            Known(value=item.size_bytes, source="radarr")
            if item.size_bytes is not None
            else Unknown(reason="the file's size was not reported", source="radarr")
        ),
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
        ratings=rating_set,
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


#: The size-tally bucket for an item no source would size. Not a ``SizeSource`` value,
#: because it is the absence of one -- kept distinct so the log line reads as counts of
#: rungs plus a miss count, rather than inventing a rung that means "none".
_UNMEASURED = "unmeasured"


def _size_bucket(source: str | None) -> str:
    """Which tally bucket one item falls in: the rung that fired, or the miss bucket.

    Returns a plain ``str`` rather than the enum member, so the log line reads
    ``{'radarr': 900, 'unmeasured': 3}`` instead of a row of enum reprs. This exists to
    be read by an operator pasting a log into an issue.
    """
    return str(source) if source else _UNMEASURED


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
    allowed_sections: set[int] | None = None,
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

    # One read of the mirror for both questions below: `state` returns the row count and
    # both ends of the window in a single pass, where `horizon`/`latest` are two thin
    # wrappers that would each call ensure_schema and open their own read of the same table.
    mirror = await history_sync.state(engine)
    horizon = mirror.earliest
    no_history = horizon is None
    if horizon is None:
        horizon = utcnow()
    context = ScanContext(horizon=horizon)
    if no_history:
        # Degrade AFTER the context is rebuilt with the resolved horizon. Degrading the
        # earlier throwaway context (then replacing it here) silently dropped the reason, so
        # a scan with no watch history at all -- which can judge nothing safely -- looked
        # non-degraded and executable. Fail closed instead.
        context.degrade("no watch history at all: nothing can be judged")
    else:
        # An empty mirror is caught above; a STALE one is invisible without this. Watch
        # stats come from the local mirror, not a live call, so a stopped ingest raises
        # nothing: watcher counts stay frozen at their last value while dormancy keeps
        # climbing, and every item drifts toward condemnation at the rate of the outage.
        #
        # Ask when the INGEST last ran (`history_sync.last_synced_at`), never when somebody
        # last watched something (`mirror.latest`). The two are identical for a stalled
        # ingest and for a quiet library, so gating on the newest play tells a household
        # that went away for a weekend that its watch history is broken, and blocks every
        # deletion until somebody watches something.
        synced = await history_sync.last_synced_at(engine)
        if synced is None or utcnow() - synced > MIRROR_STALE_AFTER:
            context.degrade(
                "watch history has not updated recently, so nothing can be judged on how "
                "long it has gone unwatched"
            )

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

    # Wall clock of the whole concurrent gather (fan-out to last await). The per-source
    # self-times above tell which source dominates this wall; this is the wall itself.
    gather_wall_started = time.monotonic()

    # Every task the fan-out creates goes through _spawn, so the reap on failure below
    # covers all of them by construction -- a future branch cannot be forgotten.
    fanned_out: list[asyncio.Task[Any]] = []

    # Per-source wall time, so a slow scan can be pinned to ONE source instead of guessing.
    # Each named task times ITSELF (start when it begins, stop when it finishes), which is
    # the only honest measure when they all run concurrently: awaiting one that already
    # finished would read as instant. `radarr` accumulates across instances (they overlap,
    # so the sum is an upper bound, not the wall). Logged as snapshot.gather_timing below.
    source_ms: dict[str, int] = {}

    def _spawn[T](coro: Coroutine[Any, Any, T], *, name: str | None = None) -> asyncio.Task[T]:
        async def _timed() -> T:
            started = time.monotonic()
            try:
                return await coro
            finally:
                if name is not None:
                    source_ms[name] = source_ms.get(name, 0) + round(
                        (time.monotonic() - started) * 1000
                    )

        task = asyncio.create_task(_timed())
        fanned_out.append(task)
        return task

    # A read-only extension of the same gather. It reads Sonarr, resolves prunable
    # seasons to Plex, and reads their watch history from the same local mirror. A
    # movie-only deployment (no Sonarr) skips it entirely.
    season_task: asyncio.Task[list[season_scan.SeasonJudgment]] | None = None
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
                window_days=tv_policy.popularity_window_days(),
                whitelisted=tag_only_whitelist,
                degrade=context.degrade,
                requested=requested,
                request_index=request_index,
                keep_last_scope=tv_policy.keep_last_scope,
                season_lookahead=tv_policy.season_lookahead,
                keep_in_progress=tv_policy.keep_in_progress,
                in_progress_hold_days=tv_policy.in_progress_hold_days,
                keep_specials=tv_policy.keep_specials,
                flag_keep_conflicts=tv_policy.flag_keep_conflicts,
                membership_index=membership_index,
                allowed_sections=allowed_sections,
            ),
            name="season",
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

    async def _roots_from(source: RadarrSource) -> tuple[str, ...] | None:
        """This instance's root folder paths, or ``None`` if they could not be read.

        Fetched once per instance, never per item. ``None`` is not ``()``: an instance that
        reports no roots is answering, while a failed read is not, and
        :func:`identity._narrow_among_id_hits` refuses to narrow an ambiguous id at all on
        ``None``. That refusal exists because losing the roots does not merely cost a bind,
        it removes the folder-vs-size contradiction veto, and without the veto a stale Plex
        size can bind a copy the folder would have disputed.

        A failure here does NOT degrade the snapshot, which is a deliberate exception to
        rule 28: rule 28 degrades on evidence that can *condemn*, and the compensating
        control is the refusal above, which keeps every affected file.
        """
        try:
            folders = await source.client.root_folders()
        except IntegrationError as exc:
            log.warning("snapshot.radarr_rootfolders", instance=source.name, error=str(exc))
            return None
        return identity.root_folder_paths(folders)

    index_task = _spawn(
        build_movie_index(
            tautulli, plex, degrade=context.degrade, allowed_sections=allowed_sections
        ),
        name="plex_index",
    )
    movie_tasks = [_spawn(_movies_from(source), name="radarr") for source in radarrs]
    roots_tasks = [_spawn(_roots_from(source)) for source in radarrs]

    items: list[RawItem] = []
    season_judgments: list[season_scan.SeasonJudgment] = []
    try:
        # Awaited in the sequential code's order, so the first failure to surface is the
        # same one it would have raised then; the except below reaps every other task.
        plex_index = await index_task
        for source, movie_task, roots_task in zip(radarrs, movie_tasks, roots_tasks, strict=True):
            movies = await movie_task
            roots = await roots_task
            if movies is None:
                continue
            items.extend(
                _raw_items(movies, plex_index, source.instance_id, requested, root_folders=roots)
            )

        emit(Progress("gathering", 4, 5, "IMDb ratings"))
        # Look up by BOTH Radarr's imdbId and the matched Plex item's imdb id, so a film
        # whose Radarr record lacks (or has a wrong) imdbId still gets its rating when
        # Plex knows it.
        # Deduped: an item's Radarr imdbId and its Plex-matched imdb id are usually the
        # same string, and the dataset lookup returns a keyed map, so passing each id once
        # changes nothing but the chunk count.
        imdb_ids = list(dict.fromkeys(x for i in items for x in (i.imdb_id, i.plex_imdb_id) if x))
        try:
            imdb = await ImdbRatings(engine).lookup(imdb_ids)
        except DatasetDegradedError as exc:
            # The inverted failure: a missing rating REMOVES protection. Degrade loudly.
            context.degrade(str(exc))
            imdb = {}
        last_played, watchers_window, watchers_all_time = await _watch_stats(
            engine,
            rating_keys={i.plex_rating_key for i in items if i.plex_rating_key},
            window_days=movie_policy.popularity_window_days(),
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
            window_days=movie_policy.popularity_window_days(),
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
            season_judgments = await season_task
    except BaseException:
        # A failure on any branch aborts the scan, exactly as it did sequentially -- but
        # the surviving branches are reaped first (canceled, drained, late failures
        # logged), so nothing keeps reading from sources after the scan is already dead
        # and no task's failure goes unobserved. Every task is in fanned_out because
        # every task was created by _spawn.
        await reap(fanned_out)
        raise

    gather_wall_ms = round((time.monotonic() - gather_wall_started) * 1000)

    # ---- freeze ------------------------------------------------------------
    snapshot = Snapshot(
        created_at=utcnow(),
        # Movies and seasons are judged under different policies, so the snapshot records the
        # combination of both -- movie first, TV second. See policy.combine_hashes.
        policy_hash=combine_hashes(movie_policy.policy_hash(), tv_policy.policy_hash()),
        scoring_hash=combine_hashes(movie_policy.scoring_hash(), tv_policy.scoring_hash()),
        # What was gathered and frozen: lets the simulator replay a weight/rating edit from
        # each Candidate's facts_json without a re-scan (services.snapshot._judge_item freezes
        # them). Movie first, TV second, exactly like the other combined hashes.
        evidence_hash=combine_hashes(movie_policy.evidence_hash(), tv_policy.evidence_hash()),
        horizon_at=context.horizon,
        item_count=len(items) + len(season_judgments),
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
    movie_window = movie_policy.popularity_window_days()
    tv_window = tv_policy.popularity_window_days()
    # Scoring configs are pure functions of the frozen policies -- identical for every
    # item -- so build them once here instead of once per item inside the judge loops.
    movie_custom = movie_policy.custom_signal_configs()
    movie_keeps = movie_policy.keep_configs()
    tv_custom = tv_policy.custom_signal_configs()
    tv_keeps = tv_policy.keep_configs()
    now = utcnow()
    condemned = 0
    total = len(items) + len(season_judgments)

    condemned_keys: list[str] = []
    # Which rung of the size ladder actually fired, counted across the whole scan. This
    # answers a question nothing in Reaper has ever measured: how often is a size simply
    # not reported? Counts only, never a title or a path.
    size_sources: Counter[str] = Counter()

    # Scoring is pure in-memory now (no per-item I/O), so this measures the CPU cost of
    # judging every movie and season -- kept apart from the source-read wall above so a
    # slow scan is attributable to a source or to scoring, never lumped into one number.
    score_started = time.monotonic()
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
        movie_size_source = SizeSource.RADARR if item.size_bytes is not None else None
        size_sources[_size_bucket(movie_size_source)] += 1
        if movie_size_source is None:
            # Per item, because a count alone cannot be chased down. The media_key is an
            # internal coordinate, never a title or a path.
            log.info(
                "scan.size_unmeasured",
                media_key=item.media_key,
                media_type=item.media_type,
                reason="radarr reported no sizeOnDisk",
            )
        verdict = _judge_item(
            session,
            snapshot_id=snapshot.id,
            media_key=item.media_key,
            plex_rating_key=item.plex_rating_key,
            title=item.title,
            media_type=item.media_type,
            # The scoring lane reads the honest Observation off `facts`; this is the
            # display and reclaim-accounting column. None means Radarr reported a file it
            # holds without a size, and it stays None: no file worth deleting is genuinely
            # 0 bytes, so a stored 0 would be a measurement Reaper never took.
            #
            # What that costs the item is deletion. `planner.build_plan` holds it back,
            # `executor.size_confirmed` refuses it again per item, and both caps and the
            # byte total the owner confirms leave it out. It still scores, still shows in
            # the queue, and says "Size unknown" wherever its size would appear.
            # Do not "fix" this by inventing a size here.
            size_bytes=item.size_bytes,
            size_source=movie_size_source,
            facts=facts,
            gates=movie_gates,
            signals=movie_signals,
            custom_condemn=movie_custom,
            keeps=movie_keeps,
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
                library=item.library,
                show_status=None,  # a movie is not a series, so the question does not apply
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
    for offset, judgment in enumerate(season_judgments):
        if offset % 100 == 0:
            emit(Progress("scoring", len(items) + offset, total, judgment.title))
            await asyncio.sleep(0)  # keep the event loop live; see the movie loop above
        size_sources[_size_bucket(judgment.size_source)] += 1
        if judgment.size_source is None:
            log.info(
                "scan.size_unmeasured",
                media_key=judgment.media_key,
                media_type="season",
                reason="sonarr reported no size for the season",
            )
        verdict = _judge_item(
            session,
            snapshot_id=snapshot.id,
            media_key=judgment.media_key,
            plex_rating_key=judgment.plex_rating_key,
            # A season's poster is the SHOW's, not the season's -- shows always have one.
            poster_rating_key=judgment.poster_rating_key,
            title=judgment.title,
            media_type="season",
            size_bytes=judgment.size_bytes,
            size_source=judgment.size_source,
            facts=judgment.facts,
            gates=tv_gates,
            signals=tv_signals,
            custom_condemn=tv_custom,
            keeps=tv_keeps,
            policy=tv_policy,
            now=now,
            window_days=tv_window,
            grace_days=grace_days,
            display=Display(
                year=judgment.year,
                summary=judgment.summary,
                poster_url=judgment.poster_url,
                requested_by=judgment.requested_by,
                group_key=judgment.group_key,
                group_title=judgment.group_title,
                tmdb_id=judgment.tmdb_id,
                imdb_id=judgment.imdb_id,
                title_slug=judgment.title_slug,
                content_rating=judgment.content_rating,
                runtime_minutes=judgment.runtime_minutes,
                library=judgment.library,
                ratings_json=judgment.ratings_json,
                show_status=judgment.show_status,
            ),
            matched_by=judgment.matched_by,
            match_detail=judgment.match_detail,
            match_status=judgment.match_status,
            extra_results=(judgment.guard_result,),
            override=whitelist.effective_override(judgment.media_key, override_map),
        )
        if verdict == "condemn":
            condemned += 1
            condemned_keys.append(judgment.media_key)

    # Grace clocks for everything condemned this run, in one batched pass -- the
    # _apply_first_flag decision per key, without a database round trip per item.
    await record_first_flagged_bulk(session, condemned_keys, now, grace_days=grace_days)

    await session.flush()
    score_ms = round((time.monotonic() - score_started) * 1000)
    emit(Progress("done", total, total, f"{condemned} candidates"))

    log.info(
        "scan.size_source_tally",
        snapshot=snapshot.id,
        total=total,
        sources=dict(sorted(size_sources.items())),
    )
    log.info(
        "snapshot.built",
        snapshot=snapshot.id,
        items=len(items),
        seasons=len(season_judgments),
        condemned=condemned,
        degraded=context.degraded,
    )
    # The intra-gather split scan_runner's scan.completed points at: which source owns the
    # gather wall (plex_index / radarr / season), and how much is pure scoring. A None means
    # that source did not run (no Sonarr -> no season_ms). Read this to decide which
    # structural optimization is worth building before writing one.
    log.info(
        "snapshot.gather_timing",
        snapshot=snapshot.id,
        gather_wall_ms=gather_wall_ms,
        plex_index_ms=source_ms.get("plex_index"),
        radarr_ms=source_ms.get("radarr"),
        season_ms=source_ms.get("season"),
        score_ms=score_ms,
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
    # The Plex library (section) title -- the show's for a season, its own for a movie.
    library: str | None = None
    ratings_json: str | None = None
    # "ended" / "continuing" / "unknown" for a season row, None for a movie. Built once,
    # by season_scan.show_status_key.
    show_status: str | None = None


#: The "no display fields" default, as a singleton so it is not constructed per call.
_NO_DISPLAY = Display()

#: What a hand spare reads as in the why-panel's "Protections that fired" list. A lowercase
#: fragment with no trailing period, matching every gate protection ("someone is watching it
#: right now", "on your keep list, never reaped"). A hand spare wears the whitelist gate id,
#: so the review chip (``api.routes._kept_phrase``) tells it apart from a real keep-list
#: entry by this exact string. Every producer and that one reader import this constant;
#: never re-type the literal.
HAND_SPARE_DETAIL = "you spared this by hand"


def _judge_item(
    session: AsyncSession,
    *,
    snapshot_id: int,
    media_key: str,
    plex_rating_key: int | None,
    poster_rating_key: int | None = None,
    title: str,
    media_type: str,
    size_bytes: int | None,
    size_source: str | None,
    facts: Facts,
    gates: list[Gate],
    signals: list[SignalConfig],
    custom_condemn: list[CustomSignalConfig],
    keeps: list[KeepConfig],
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
        merged_extra.insert(0, GateResult(GateId.WHITELISTED, PROTECT, detail=HAND_SPARE_DETAIL))
    evaluation = Evaluation(results=[*merged_extra, *evaluate_all(gates, facts).results])
    item_score = score(
        signals,
        facts,
        custom_condemn=custom_condemn,
        keeps=keeps,
        window_days=window_days,
    )

    score_value = round(item_score.value)
    coverage_bp = round(item_score.coverage * 10_000)

    verdict = _verdict(evaluation, score_value, coverage_bp, policy, override=override)
    # The grace clock for a condemned item is set by the CALLER, batched across the whole
    # run (record_first_flagged_bulk) -- one query for every condemned key instead of a
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
            size_source=size_source,
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
            library_title=display.library,
            ratings_json=display.ratings_json,
            show_status=display.show_status,
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
            # The frozen scoring inputs: the Facts plus the season-pruning guard (extra_results,
            # NOT the hand-override, which is re-applied live at replay time from the override
            # map). This is what the simulator replays under an edited policy. See facts_codec.
            facts_json=json.dumps(
                facts_codec.facts_to_dict(facts, extra_results=tuple(extra_results))
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
            # The same whole number stored on the row and compared by decide_verdict, so
            # the panel and the decision can never show two different scores.
            "score": round(item_score.value),
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
                    # What the zero means. Four situations all land on a contribution of
                    # 0 and are otherwise identical on the wire; only the engine branch
                    # that produced the result can tell them apart. See SignalState.
                    "state": r.state.value,
                }
                # A weight of 0 is a turned-off rule: out of the denominator, worth no
                # points, and nothing an owner needs to read. Its detail is engine
                # shorthand ("disabled"), so it is dropped here rather than rendered.
                for r in item_score.results
                if r.weight > 0
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
    existing: FirstFlagged | None,
    media_key: str,
    now: datetime,
    *,
    grace_days: int,
) -> FirstFlagged | None:
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

    This is THE decision, applied per key by :func:`record_first_flagged_bulk` (the only
    write path to the grace clock). A key with no row yet is RETURNED as a new row rather
    than inserted here, so the recorder can insert it conflict-tolerantly.
    """
    if existing is None:
        return FirstFlagged(media_key=media_key, first_flagged_at=now, last_seen_condemned_at=now)

    last_seen = existing.last_seen_condemned_at
    gap = timedelta(days=grace_days)
    if last_seen is None or (now - last_seen) > gap:
        # It left the condemned set for longer than a whole grace window and has returned:
        # this is a new condemnation, so it earns a new window. Keying on the gap exceeding
        # the window (not a single missed snapshot) keeps a transient outage from resetting
        # a clock that was legitimately still running.
        existing.first_flagged_at = now
    existing.last_seen_condemned_at = now
    return None


async def _insert_first_flags(session: AsyncSession, rows: Sequence[FirstFlagged]) -> None:
    """Insert new grace-clock rows, tolerating a competing writer.

    ``ON CONFLICT DO NOTHING`` on the key: if another writer landed the same key between
    our read and this write, its row already says "condemned around now" and keeping it
    is correct -- a primary-key collision must never abort a scan. Chunked at 300 rows
    (three bound values each) to stay under SQLite's historical 999-variable limit.
    """
    for start in range(0, len(rows), 300):
        chunk = rows[start : start + 300]
        await session.execute(
            sqlite_insert(FirstFlagged)
            .values(
                [
                    {
                        "media_key": row.media_key,
                        "first_flagged_at": row.first_flagged_at,
                        "last_seen_condemned_at": row.last_seen_condemned_at,
                    }
                    for row in chunk
                ]
            )
            .on_conflict_do_nothing(index_elements=["media_key"])
        )


async def record_first_flagged_bulk(
    session: AsyncSession, media_keys: Sequence[str], now: datetime, *, grace_days: int
) -> None:
    """Grace bookkeeping for every key condemned in one run, in one read.

    The ONLY write path to the grace clock, applying :func:`_apply_first_flag` per key;
    the existing rows arrive in chunked ``IN`` queries instead of a ``session.get`` (and
    its autoflush) per condemned item, and brand-new keys are inserted conflict-tolerantly
    (:func:`_insert_first_flags`) so two writers racing on one key cannot abort the scan.
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
    new_rows = [
        new_row
        for key in keys
        if (new_row := _apply_first_flag(existing.get(key), key, now, grace_days=grace_days))
        is not None
    ]
    await _insert_first_flags(session, new_rows)


# ---------------------------------------------------------------------------
# Gathering helpers
# ---------------------------------------------------------------------------


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
    allowed_sections: set[int] | None = None,
) -> identity.PlexIndex:
    """The Plex movie library, inverted for id / basename / title matching.

    One shared implementation with ``season_scan.build_tv_index`` -- see
    ``services.library_index`` for the spine + sweep design and its failure
    semantics. ``allowed_sections`` scopes the read to the movie libraries the operator
    included in scans (``None`` = all). A movie-only deployment with no Plex configured
    simply gets no enrichment; its snapshot was already un-executable, since a real reap
    refuses without Plex.
    """
    return await library_index.build_index(
        tautulli, plex, section_type="movie", degrade=degrade, allowed_sections=allowed_sections
    )


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


def _movie_file_path(movie: Mapping[str, Any]) -> str | None:
    """The movie file's full path, for the folder corroborator.

    Always the absolute ``path`` (never ``relativePath``, which is the bare file name and
    carries no folder to compare). Only its trailing segments are ever read, so the
    mount-root difference against Plex does not matter.
    """
    movie_file = movie.get("movieFile")
    if not isinstance(movie_file, dict):
        return None
    path = movie_file.get("path")
    return str(path) if path else None


def _reported_size(movie: Mapping[str, Any]) -> int | None:
    """Radarr's ``sizeOnDisk`` for a movie it says it holds, or ``None``.

    ``sizeOnDisk`` covers the movie's folder (file plus extras), which is the number the
    reclaim estimate and the byte cap want. Distinct from :func:`_movie_file_size`, which
    reads ``movieFile.size`` for file-to-file identity comparison.

    Missing or zero is ``None``, never ``0``: see ``RawItem.size_bytes``.
    """
    size = movie.get("sizeOnDisk")
    return int(size) if isinstance(size, int | float) and size > 0 else None


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
        # Dec 31, not Jan 1: only the year is known, so resolve the ambiguity toward
        # keeping. Jan 1 would OVERSTATE age by up to ~364 days on a condemn-lane
        # field, over-matching every `release_age >= N` rule the owner writes.
        age = (utcnow().date() - date(year, 12, 31)).days
    except (ValueError, OverflowError):
        return Absent(source="radarr")
    return Known(value=float(max(0, age)), source="radarr")


def _raw_items(
    movies: list[dict[str, Any]],
    plex_index: identity.PlexIndex,
    instance_id: int,
    requested: dict[str, str] | None = None,
    root_folders: Sequence[str] | None = (),
) -> list[RawItem]:
    requested = requested or {}
    items: list[RawItem] = []
    without_file: list[str] = []
    for movie in movies:
        if not movie.get("hasFile"):
            without_file.append(str(movie.get("title") or "?"))
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
            file_path=_movie_file_path(movie),
            # This Radarr instance's own root folders, or None if they could not be read.
            # Without them the folder corroborator cannot tell a real folder from the
            # container's mount point. An empty tuple stands the folder step down; None
            # refuses the whole id narrowing (identity._narrow_among_id_hits), because a
            # failed read also removes the folder-vs-size contradiction veto.
            root_folders=root_folders,
            index=plex_index,
        )
        matched = resolution.plex_item
        if resolution.rating_key is None:
            # The movie has a file in Radarr but Reaper could not confidently bind it to a
            # Plex row, so it appears only as "kept to be safe", never on the reap list.
            # Warned per item so an operator asking "why isn't this in review" finds the
            # reason in the log, not only on the row's why-panel. UNMATCHED = nothing in Plex
            # looked like it; AMBIGUOUS = more than one did and we refused to guess.
            log.warning(
                "scan.plex_unmatched",
                media_type="movie",
                instance_id=instance_id,
                title=str(movie.get("title") or ""),
                year=int(movie["year"]) if movie.get("year") else None,
                imdb_id=movie.get("imdbId") or None,
                tmdb_id=tmdb_id,
                match_status=str(resolution.status),
                detail=resolution.detail,
            )
        items.append(
            RawItem(
                # Identity is the *arr's, not Plex's. Plex rating keys are not stable
                # across library rebuilds or agent migrations.
                media_key=f"radarr:{instance_id}:{movie['id']}",
                title=str(movie.get("title") or ""),
                media_type="movie",
                # `or 0` here would turn a partial payload into a 0-byte file. Radarr
                # says it holds a file (has_file below), so a missing size means we
                # could not read it, not that there is nothing to read.
                size_bytes=_reported_size(movie),
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
                library=matched.library if matched is not None else None,
                plex_ratings=matched.ratings if matched is not None else (),
                arr_ratings=tuple(from_radarr(movie.get("ratings"))),
            )
        )
    if without_file:
        # Not unmatched, just nothing to reap yet: monitored in Radarr with no downloaded
        # file. Counted (names at debug) so "no movie candidates" reads apart from "every
        # movie was skipped", and a specific missing title can be traced.
        log.info("scan.movies_without_file", instance_id=instance_id, count=len(without_file))
        log.debug("scan.movies_without_file_titles", instance_id=instance_id, titles=without_file)
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

    # One pass over the movie rows for all three figures, where three separate GROUP BY
    # scans of the same table did before. The windowed watcher count rides along as a
    # conditional distinct-count: watched_at outside the window yields NULL, which COUNT
    # DISTINCT ignores, so it equals the old `WHERE watched_at >= :since` query exactly.
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT rating_key, "
                    "MAX(watched_at) AS last, "
                    "COUNT(DISTINCT user_id) AS ever, "
                    "COUNT(DISTINCT CASE WHEN watched_at >= :since THEN user_id END) AS window "
                    "FROM watch_event WHERE media_type = 'movie' GROUP BY rating_key"
                ),
                {"since": window_start},
            )
        ).all()

    last: dict[int, datetime] = {}
    window: dict[int, int] = {}
    ever: dict[int, int] = {}
    for r in rows:
        key = int(r.rating_key)
        played = from_epoch(r.last)
        if played is not None:
            last[key] = played
        ever[key] = int(r.ever)
        # Keep only keys with a play INSIDE the window, exactly as the old windowed query
        # returned: a 0 (has plays, none recent) is dropped so the dict stays byte-identical
        # to before. Downstream reads it as `.get(key, 0)`, so absent and 0 are the same fact.
        if r.window:
            window[key] = int(r.window)

    return last, window, ever


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
    # the surviving providers are canceled and drained rather than left refreshing
    # lists for a scan that is already dead.
    await gather_reaped(*runs)
    return synced


#: How long since the last successful watch-history sync (``history_sync.last_synced_at``)
#: before the snapshot degrades: one missed nightly ingest is a blip, two is a pattern, and
#: past that the dormancy every score leans on is being measured against a frozen mirror.
#: Set tighter than this and a paused Tautulli blocks every scan; looser, and items drift
#: toward condemnation for as long as the outage lasts. Same bound as
#: WHITELIST_STALE_AFTER and the same two-nightly-cycles logic behind it, but a different
#: quantity: that one bounds a *failed* sync coasting on stored keep-list membership,
#: this one bounds a sync that stopped running at all.
MIRROR_STALE_AFTER = timedelta(hours=48)


#: How long a failed whitelist may coast on its stored membership before the snapshot
#: degrades anyway. A stale-but-populated keep-list still protects everything already on
#: it, so one missed nightly sync is tolerated (the tradeoff: a title keep-tagged after
#: the last successful sync is unprotected until the next one). Past this bound the
#: unprotected window is no longer a blip, so the scan stops being executable until a
#: sync succeeds. Two nightly cycles plus slack.
WHITELIST_STALE_AFTER = timedelta(hours=48)


async def protection_sync_degradations(
    engine: AsyncEngine, synced: Mapping[str, int | str]
) -> list[str]:
    """Which failed protection-list syncs must degrade the snapshot.

    ``sync_protection_lists`` records a failed provider as ``"error: ..."`` and then only
    the caller can decide what to do about it. Only **whitelist**-kind lists can fail
    *open* (a curated soft-list that fails merely loses a scoring nudge; it never
    unprotects a kept title), and a failed whitelist degrades the snapshot in three
    cases, each resolving toward keeping files:

    * **No membership to fall back on.** A first scan, or a newly-added keep-list that
      has never synced once: the WhitelistGate reads an empty keep-list and fails to
      fire, so an executable snapshot would reap the very titles the list was meant to
      save.
    * **Stored membership older than ``WHITELIST_STALE_AFTER``.** The atomic swap in
      ``lists.sync`` keeps the prior membership on a failed refresh, so a *fresh-enough*
      copy still protects and a transient failure need not stop the scan -- but every
      hour of staleness is an hour in which a newly keep-tagged title is unprotected, so
      past the bound the snapshot degrades until a sync succeeds.
    * **No record of a successful sync at all** (members present but no
      ``last_synced_at``): recency cannot be confirmed, so it is not assumed.
    """
    await lists.ensure_schema(engine)
    reasons: list[str] = []
    now = utcnow()
    async with engine.connect() as conn:
        for slug, outcome in synced.items():
            if not (isinstance(outcome, str) and outcome.startswith("error:")):
                continue
            row = (
                await conn.execute(
                    text("SELECT kind, last_synced_at FROM protection_list WHERE slug = :slug"),
                    {"slug": slug},
                )
            ).one_or_none()
            kind = row[0] if row is not None else None
            last_synced_at = row[1] if row is not None else None
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
                    f"protection list '{slug}' failed to sync and its keep-list is empty: "
                    "a scan must not reap titles the list would have saved"
                )
                continue
            # Members exist, so the stored copy still protects -- but only a fresh-enough
            # copy. last_synced_at is written only on success (lists.sync), so it IS the
            # last successful sync; from_epoch returns None for a null or zero stamp.
            last_success = from_epoch(last_synced_at)
            if last_success is None:
                reasons.append(
                    f"protection list '{slug}' failed to sync, and Reaper has no record of "
                    "it ever syncing successfully. Titles on it may be unprotected, so "
                    "nothing may be deleted from this scan"
                )
            elif now - last_success > WHITELIST_STALE_AFTER:
                hours = int(WHITELIST_STALE_AFTER.total_seconds() // 3600)
                reasons.append(
                    f"protection list '{slug}' failed to sync and its stored copy is more "
                    f"than {hours} hours old. Titles added to it since then are "
                    "unprotected, so nothing may be deleted from this scan"
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
