# SPDX-License-Identifier: AGPL-3.0-or-later
"""Builds a snapshot: gathers evidence, freezes it, then judges every item.

Every item is gathered and frozen before anything is scored, so a Sonarr timeout
partway through cannot change the fate of items judged after it. An item's fate must
never depend on network luck.

## Degradation is loud, and it blocks execution

If a source is unreachable, the snapshot is marked degraded. An owner can still view a
degraded snapshot to see what went wrong, but nothing may be executed against it.
Partial evidence must never let a scan delete a file it could not fully check.

## Every fact carries its source

A fact is Known, Absent, or Unknown. An empty result from a failed call is Unknown,
never Absent and never an empty list. Absent means nobody watched this: real evidence,
and it may count toward condemning the item. Unknown means Reaper could not find out:
never evidence, and it can only protect the item. Every adapter below has a test
asserting which one it produces.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Any

import structlog
from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from reaper.aio import gather_reaped, reap
from reaper.clients.arr import RadarrClient
from reaper.clients.base import IntegrationError
from reaper.clients.plex import PlexClient, PlexCollectionRow, PlexError
from reaper.clients.tautulli import TautulliClient
from reaper.clock import from_epoch, utcnow
from reaper.db import KEY_CHUNK
from reaper.db.models import (
    Candidate,
    FirstFlagged,
    SeasonPruneEvidence,
    SizeSource,
    Snapshot,
)
from reaper.engine import facts_codec, identity
from reaper.engine.dormancy import dormancy_days, history_reach_days, reference_instant
from reaper.engine.gates import (
    REWATCH_BLOCK_FLOOR_N,
    Evaluation,
    Facts,
    Gate,
    GateResult,
    evaluate_all,
    no_added_at_reason,
    no_key_reason,
    no_size_reason,
    wilson_upper,
)
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.engine.policy import PolicyBody, combine_hashes
from reaper.engine.reason import Reason, to_wire
from reaper.engine.signals import (
    CustomSignalConfig,
    KeepConfig,
    Score,
    SignalConfig,
    SignalId,
    score,
)
from reaper.engine.verdict import decide_verdict
from reaper.ratings import Rating, RatingSource, from_radarr, merge_by_source
from reaper.services import (
    history_sync,
    identity_churn,
    library_index,
    library_seen,
    list_config,
    lists,
    requested_by,
    season_evidence,
    season_scan,
    watch_evidence,
    whitelist,
)
from reaper.services.condemned import reap_override_verdict
from reaper.services.display_meta import (
    IMDB_UNREADABLE_REASON,
    NO_IMDB_ID_REASON,
    build_ratings_json,
    dataset_entry,
    dataset_lookup,
    normalize_resolution,
)
from reaper.services.imdb_dataset import DatasetDegradedError, ImdbRating, ImdbRatings
from reaper.services.rewatch import (
    NO_REWATCH_ESTIMATE_REASON,
    RewatchBlock,
    RewatchCurve,
    RewatchStats,
    cohort_block,
    fit_blocks,
    movie_rewatch_outcomes,
    movie_rewatch_stats,
    training_pair,
)
from reaper.text import fold

log = structlog.get_logger(__name__)

#: Default for a caller (mainly a test) that does not pass rewatch stats: every item reads
#: as zero qualified viewings rather than an unreadable mirror. ``scan`` always passes the
#: real map, gathered beside ``_watch_stats`` over the same candidate key set.
_NO_REWATCH_STATS: Mapping[int, RewatchStats] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Progress:
    """One step of a scan's progress, polled by the browser via ``GET /api/scan/status``.

    ``detail`` is a typed reason under ``shell.scanBar.step.*``: a bare id plus raw params
    (``scoring``'s ``title``, ``done``'s ``count``). The browser composes the English text;
    this module never does. ``None`` on the final "complete" emit, which the route blanks
    anyway.
    """

    phase: str
    done: int
    total: int
    detail: Reason | None = None


ProgressFn = Callable[[Progress], None]


@dataclass
class ScanContext:
    """Everything gathered, before anything is judged."""

    horizon: datetime
    active_rating_keys: set[int] = field(default_factory=set)
    degraded_reasons: list[str] = field(default_factory=list)

    reach_days: int = field(init=False)
    """How far back the watch mirror reaches, in days. Sampled once, here.

    This is a property of the mirror, not of an item, so it belongs beside ``horizon``
    rather than in the per-item builder. Reading the clock separately for each item could
    let one scan use two different reach values, so ``gates.ServerPopularityGate`` and
    both the movie and season builders read this single value instead.
    """

    activity_degraded: bool = False
    """True when Reaper could not read what is playing right now.

    An empty ``active_rating_keys`` is ambiguous: it looks the same whether nobody is
    watching or Tautulli was unreachable. Anything that vetoes a deletion on "nothing is
    streaming" must read this flag, never test whether the set is empty. ``scan`` sets it
    wherever the activity read fails.

    This is a typed field, not a substring match on ``degraded_reasons``, so a veto never
    depends on the wording of a free-text reason.
    """

    imdb_degraded: bool = False
    """True when Reaper could not read the IMDb ratings data at all.

    This is the same trap as ``activity_degraded``: an empty lookup map looks the same as
    "every title is genuinely unrated" unless something says so. ``build_facts`` reads
    this flag to record the rating and vote count as Unknown rather than Absent, because
    Absent would remove every rating-based protection on evidence that was never checked.
    """

    def __post_init__(self) -> None:
        self.reach_days = history_reach_days(self.horizon, now=utcnow())

    @property
    def degraded(self) -> bool:
        return bool(self.degraded_reasons)

    def degrade(self, reason: str) -> None:
        log.warning("snapshot.degraded", reason=reason)
        # Every reason passes through here: the many call sites, `build_index`'s callback,
        # and the caller's `extra_degrade_reasons` all reach this one function.
        #
        # Each reason ends with punctuation. Every notice joins the reasons into one
        # paragraph and writes more text directly after them, so a reason with no closing
        # period would run into the sentence that follows it.
        #
        # Each reason starts with a capital letter, so a list of reasons joined with "; "
        # reads as separate sentences instead of one lower-case run-on. Only the first
        # character is touched, so an id or a quoted name that starts a reason keeps its
        # own spelling.
        said = reason if reason.endswith((".", "!", "?")) else f"{reason}."
        self.degraded_reasons.append(said[:1].upper() + said[1:])


@dataclass(frozen=True, slots=True)
class RawItem:
    """One movie, as the *arr sees it, before any judgment."""

    media_key: str
    title: str
    media_type: str
    size_bytes: int | None
    """Bytes on disk, or ``None`` when the *arr reported a file but no size for it.

    Never store ``None`` as ``0``. A file Radarr holds but never sized is not a 0-byte
    file, and treating it as one would corrupt any signal or keep rule based on size. See
    ``tests/test_fact_layer_states.py``.
    """

    imdb_id: str | None
    tmdb_id: int | None
    plex_rating_key: int | None
    added_at: datetime | None
    # Display fields, carried onto the candidate so the review queue can show a blurb
    # without a second data source. None of them influence the verdict. There is no
    # poster field: `api/review._candidate_out` derives it from the Plex rating key at
    # read time instead.
    year: int | None = None
    summary: str | None = None
    requested_by: str | None = None
    # How this item was bound to its Plex row, and why if it was not, for the why-panel.
    matched_by: identity.MatchedBy | None = None
    match_detail: str | None = None
    match_status: identity.MatchStatus | None = None
    # Every Plex listing the bind covers, when the file is listed more than once. Includes
    # plex_rating_key. Watch reads must consult all of them. Empty for a normal bind.
    merged_rating_keys: tuple[int, ...] = ()
    # The Plex rows an abstain was choosing between. Empty on any bind. Display only: it
    # reaches the why-panel so the operator can open the rows Reaper could not choose
    # between. The verdict never reads it.
    match_candidates: tuple[int, ...] = ()
    # The matched Plex item's imdb id, used as a fallback when Radarr's imdbId is missing
    # or does not resolve in the IMDb dataset.
    plex_imdb_id: str | None = None
    # Metadata an operator can use in custom rules (the weighting feature). Taken from the
    # Radarr payload the scan already holds, so it costs no extra fetch.
    genres: tuple[str, ...] = ()
    quality: str | None = None
    # Display metadata frozen onto the candidate (services.display_meta). Plex's value
    # comes first, the Radarr payload fills any gap. None of it affects the verdict.
    video_resolution: str | None = None
    content_rating: str | None = None
    runtime_minutes: int | None = None
    # The Plex library (section) the matched item lives in. Used for display and filtering.
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
    watch_blind_reason: str | None = None,
    rewatch: Mapping[int, RewatchStats] = _NO_REWATCH_STATS,
    rewatch_curve: RewatchCurve | None = None,
    seen: library_seen.Seen | None = None,
) -> Facts:
    """Assemble one item's evidence.

    Watch for how often ``Unknown`` appears. Coercing a missing value to ``0``, ``[]`` or
    ``False`` would quietly condemn an item Reaper knows nothing about.

    ``watch_blind_reason`` covers one case the item's own evidence cannot reveal.
    ``services.watch_evidence`` sets it when this item measured fewer plays than it
    measured before, which a real library never does. The mirror is read by the item's
    current rating key, and a re-added file gets a new one while its earlier plays stay
    filed under the old key. That makes "no rows" ambiguous between a re-added file and a
    genuinely unwatched one. When this reason is set, dormancy and both watcher counts
    read Unknown instead of a measured zero.

    ``rewatch_curve`` is the fit ``scan`` refits once per scan and shares across every
    item. It is ``None`` for a caller that has not fit one yet, such as a test fixture or a
    call before the scan's own fit runs, which reads the same as "no usable block" below.
    """
    rating_key = item.plex_rating_key
    # The three no-key states tell different stories, and the why-panel must not conflate
    # them. "Unmatched" means Plex has no such item as far as Reaper can tell. "Ambiguous"
    # means Plex has more than one (a 1080p and a 4K copy sharing one TMDB id) and Reaper
    # refused to guess whose watch history to read. "Conflicted" means each kind of
    # evidence found one row, and the two rows differ: the two apps describe one file
    # differently, not evidence that Plex holds several copies. All three keep the file.
    # Only the words shown to the owner differ, and the wrong words send them to fix the
    # wrong thing. Each string is a key into the catalog's why.cause.* entries, and
    # test_review_chips.py::TestTheMatchStatusVocabulary fails when one has no entry there.
    # `gates.no_key_reason` produces the same catalog id for the movie and season lanes, so
    # the panel's ICU `mediaType` select ("movie" here) always picks the matching wording.
    no_key_cause = no_key_reason(item.match_status, "movie")

    # --- dormancy -----------------------------------------------------------
    # The most important derived field. "Days since last play" is null for exactly the
    # items Reaper cares about most. Coercing that null to epoch 0 would read as about
    # 20,600 days unwatched, the highest score a dormancy signal can produce, for the item
    # Reaper knows least about.
    dormancy: Observation[float]
    if rating_key is None:
        dormancy = Unknown(reason=no_key_cause, source="plex")
    elif watch_blind_reason is not None:
        # Checked before the measurement below. A re-added file carries a fresh added_at
        # while its earlier plays stay filed under the key it no longer holds. Measuring
        # dormancy here would read a confident, tiny value off the one input that still
        # looks readable, even though the plays behind it are not.
        dormancy = Unknown(reason=watch_blind_reason, source="tautulli")
    else:
        # Uses the one shared derivation in engine/dormancy.py, so the season scan
        # measures this the same way. A play alone is enough: `reference_instant` measures
        # from it, and only an item with neither a play nor an arrival date comes back
        # with nothing to measure from.
        reference = reference_instant(
            last_played=last_played.get(rating_key),
            added_at=item.added_at,
            horizon=context.horizon,
        )
        if reference is None:
            # Matched to Plex, but Plex reports no arrival date and no play is in scope, so
            # there is genuinely nothing to measure from. The item abstains and appears
            # only as "kept to be safe", never on the reap list. Warn so "why isn't this
            # reapable" is answerable from the log, the same as an unmatched item. This is
            # rare: a matched Plex item almost always carries an added_at. The reason is a
            # key into WhyPanel's copy map, named in `gates.no_added_at_reason` so
            # `test_review_chips.py::TestTheMatchStatusVocabulary` fails if the two sides
            # drift. It is also the state the season lane's both-missing case reports, so
            # the two lanes say the same thing.
            log.warning(
                "scan.no_added_at",
                media_type="movie",
                media_key=item.media_key,
                title=item.title,
                imdb_id=item.imdb_id or None,
                tmdb_id=item.tmdb_id,
                plex_rating_key=rating_key,
            )
            dormancy = Unknown(reason=no_added_at_reason("movie"), source="tautulli")
        else:
            dormancy = Known(value=dormancy_days(reference, now=utcnow()), source="tautulli")

    # --- popularity ---------------------------------------------------------
    recent: Observation[int]
    all_time: Observation[int]
    if rating_key is None:
        recent = Unknown(reason=no_key_cause, source="plex")
        all_time = Unknown(reason=no_key_cause, source="plex")
    elif watch_blind_reason is not None:
        recent = Unknown(reason=watch_blind_reason, source="tautulli")
        all_time = Unknown(reason=watch_blind_reason, source="tautulli")
    else:
        recent = Known(value=watchers_window.get(rating_key, 0), source="tautulli")
        all_time = Known(value=watchers_all_time.get(rating_key, 0), source="tautulli")

    # --- rewatch --------------------------------------------------------------
    # `rewatch` is already folded over any merged Plex listings. `scan` gathers it via
    # `services.rewatch.movie_rewatch_stats` with the same `groups` mapping
    # `_fold_merged_watch_stats` uses, so a lookup by the canonical `rating_key` alone is
    # correct here, exactly as it is for `watchers_window`/`watchers_all_time` above.
    viewings_obs: Observation[int]
    last_play_days_obs: Observation[float]
    if rating_key is None:
        viewings_obs = Unknown(reason=no_key_cause, source="plex")
        last_play_days_obs = Unknown(reason=no_key_cause, source="plex")
    elif watch_blind_reason is not None:
        viewings_obs = Unknown(reason=watch_blind_reason, source="tautulli")
        last_play_days_obs = Unknown(reason=watch_blind_reason, source="tautulli")
    else:
        # The mirror was read either way, so viewings is Known even at 0. Recency is
        # Absent, not Unknown, when this movie has no qualified play at all: Reaper
        # looked, and there is genuinely nothing to measure the last one from.
        rewatch_stats = rewatch.get(rating_key)
        viewings_obs = Known(
            value=rewatch_stats.viewings if rewatch_stats is not None else 0, source="tautulli"
        )
        last_play_days_obs = (
            Known(value=dormancy_days(rewatch_stats.last_play, now=utcnow()), source="tautulli")
            if rewatch_stats is not None and rewatch_stats.last_play is not None
            else Absent(source="tautulli")
        )

    # --- rewatch cohort -------------------------------------------------------
    # Known only when the current dormancy is Known and the fit found a usable block for
    # it. Unknown for every other reason at once (no fit, dormancy Unknown, past the
    # fitted range, a dropped bucket, withheld by reach), because the operator's takeaway
    # is the same either way. See docs/history/REWATCH_PLAN.md, Stage 2.
    #
    # `cohort_block` is the one place the lookup and the withhold combine. `scan`'s
    # per-item judge call re-derives the identical block from this same dormancy value
    # (read back from the Facts this call returns) and the same curve, so the stored
    # explanation's rewatch_odds context can never disagree with these two fields.
    cohort_n_obs: Observation[int]
    cohort_k_obs: Observation[int]
    block = (
        cohort_block(rewatch_curve, dormancy.value, reach_days=context.reach_days)
        if rewatch_curve is not None and isinstance(dormancy, Known)
        else None
    )
    if block is not None:
        cohort_n_obs = Known(value=block.n, source="tautulli")
        cohort_k_obs = Known(value=block.k, source="tautulli")
    else:
        cohort_n_obs = Unknown(reason=NO_REWATCH_ESTIMATE_REASON, source="tautulli")
        cohort_k_obs = Unknown(reason=NO_REWATCH_ESTIMATE_REASON, source="tautulli")

    # --- ratings ------------------------------------------------------------
    rating: Observation[int]
    votes: Observation[int]
    # Radarr's imdbId first, then the Plex-matched imdb id as a fallback: Radarr may lack
    # it, or carry one the IMDb dataset doesn't have. The shared helper also feeds the
    # display ratings row, so the signal and the row can never show different numbers.
    entry, looked_up = dataset_lookup(imdb, item.imdb_id, item.plex_imdb_id)
    if entry is not None:
        rating = Known(value=int(entry.average_rating * 10), source="imdb")
        votes = Known(value=int(entry.num_votes), source="imdb")
    elif context.imdb_degraded:
        # Reaper never got to ask about this title either: the whole dataset was
        # unreadable, so the empty map below is not an answer about any film in it.
        # ``looked_up`` is still True here, since the item does carry an imdb id. Taking
        # the Absent branch on that would tell every rating-based protection "checked, and
        # it is unrated" for the entire library at once, on evidence Reaper has already
        # declared untrustworthy.
        unreadable = Unknown(reason=IMDB_UNREADABLE_REASON, source="imdb")
        rating = unreadable
        votes = unreadable
    elif looked_up:
        # Absent, not Unknown: Reaper looked and this title genuinely has no IMDb rating.
        # A degraded dataset is different and is caught by the branch above: it degrades
        # the snapshot and reads Unknown, because degrading alone would still leave every
        # film here silently unprotected.
        rating = Absent(source="imdb")
        votes = Absent(source="imdb")
    else:
        # Reaper never got to ask: no imdbId from Radarr and no Plex match to borrow one
        # from. Absent here would tell every rating-based protection "this title has no
        # IMDb rating", withdrawing it while coverage still read 100%. See dataset_lookup.
        no_id = Unknown(reason=NO_IMDB_ID_REASON, source="imdb")
        rating = no_id
        votes = no_id

    # The multi-source keep protection reads this. The IMDb dataset value goes first (it
    # carries the authoritative vote count the score already uses), then Radarr's ratings
    # object (IMDb/TMDb/RT-critic/Metacritic), then Plex's two slots (which can add the RT
    # audience score). merge_by_source keeps one value per source, first writer winning,
    # and drops any UNKNOWN-source value, so a protection is never decided on a number
    # Reaper cannot interpret. No extra fetch: all three are already frozen on the item.
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
    # Whitelist and curated are different reasons to keep a file, and collapsing them
    # would tell the owner "whitelisted" about a film they never touched. The why-panel
    # must be able to say which.
    #
    # Every id the movie carries is passed together, the same as on the TV path. Radarr is
    # tmdb-native and a blank imdbId is ordinary, so the imdb id may be the one Plex
    # matched. A keep-list row stored under imdb alone, which is what a legacy-agent Plex
    # library yields for a "Never Reap" collection, must still protect it. Matching on one
    # id kind alone fails open on the deletion path.
    #
    # Every listing of a merged bind, or the single key of a normal one, is derived once
    # here and read again by the streaming veto below. A "Never Reap" entry no agent ever
    # matched is stored under its Plex key alone, so without this the operator could add a
    # title to the list and still watch Reaper condemn it.
    plex_keys = item.merged_rating_keys or ((rating_key,) if rating_key else ())
    memberships = membership_index.lookup(
        media_type="movie",
        imdb_id=item.imdb_id or item.plex_imdb_id,
        tmdb_id=item.tmdb_id,
        plex_rating_keys=plex_keys,
    )
    whitelists = [m for m in memberships if m.is_whitelist]
    curated_lists = [m for m in memberships if not m.is_whitelist]

    curated: Observation[str] = (
        Known(value=", ".join(m.describe() for m in curated_lists), source="lists")
        if curated_lists
        else Absent(source="lists")
    )
    is_whitelisted: Observation[bool] = Known(
        value=bool(whitelists) or item.media_key in whitelisted,
        source=whitelists[0].display_name if whitelists else "lists",
    )
    # Every list, by the name its keep rule spells. This is the `on_list` field's input,
    # derived in `lists.on_list_fact` for both fact builders at once.
    on_lists = lists.on_list_fact(memberships)

    # --- streaming right now ------------------------------------------------
    streaming: Observation[bool]
    if context.activity_degraded:
        # Reaper could not check. Never assume False: that is how a tool deletes a file
        # somebody is watching.
        streaming = Unknown(reason=watch_evidence.NO_SESSIONS_REASON, source="tautulli")
    else:
        # A merged bind covers several listings of one file. Someone streaming any of
        # them is streaming this very file, so the veto checks every key in the group.
        watch_keys = plex_keys
        if not watch_keys:
            # No key to match a session against, so this was never checked. An AMBIGUOUS
            # item is the case that matters most here: Plex holds the title in two copies,
            # someone can be streaming it right now, and Known(False) would assert
            # otherwise. Every sibling fact above takes Unknown on the same condition, and
            # the season builder's matching case does too.
            streaming = Unknown(reason=no_key_cause, source="plex")
        else:
            streaming = Known(
                value=any(key in context.active_rating_keys for key in watch_keys),
                source="tautulli",
            )

    returned_days_ago, returned_by_reaper = library_seen.observations(seen, now=utcnow())

    return Facts(
        title=item.title,
        days_observed_unwatched=dormancy,
        distinct_watchers=recent,
        distinct_watchers_all_time=all_time,
        # How much of the popularity window those counts could actually see. Scan-wide
        # rather than per-item, so it is sampled once on the context and only carried here,
        # where it is read alongside the count it qualifies (see ``Facts.history_reach_days``
        # and ``ScanContext.reach_days``).
        history_reach_days=Known(value=context.reach_days, source="tautulli"),
        # And how much history an ALL-TIME count would need: back to the day it arrived.
        # Measured from the arrival date itself, never from dormancy, which is clamped to
        # the mirror's edge and so can never report the shortfall (``Facts.days_since_added``).
        days_since_added=(
            Known(value=dormancy_days(item.added_at, now=utcnow()), source="plex")
            if item.added_at is not None
            else Unknown(reason=no_added_at_reason("movie"), source="plex")
        ),
        size_bytes=(
            Known(value=item.size_bytes, source="radarr")
            if item.size_bytes is not None
            else Unknown(reason=no_size_reason("movie"), source="radarr")
        ),
        imdb_rating_tenths=rating,
        imdb_votes=votes,
        season_rank=Absent(source="radarr"),  # movies have no season
        is_streaming_now=streaming,
        is_managed=Known(value=True, source="radarr"),  # it came FROM radarr
        in_curated_list=curated,
        is_whitelisted=is_whitelisted,
        on_lists=on_lists,
        rewatch_viewings=viewings_obs,
        rewatch_last_play_days=last_play_days_obs,
        rewatch_cohort_n=cohort_n_obs,
        rewatch_cohort_k=cohort_k_obs,
        # Not applicable outside the requester rule: with no requester, "others" is
        # everyone, and the gate would protect anything ever played.
        # --- fields usable in custom rules ---------------------------------
        requested=(
            request_index.movie_requested(item.tmdb_id)
            if request_index is not None
            else Unknown(reason=requested_by.REQUESTS_NOT_LOADED_REASON, source="seerr")
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
        # Whether this title left the library and came back. Read off the ledger row the
        # scan looked up before judging, never re-derived here: the detection is visible
        # for one scan and the hold runs for months. One shared helper serves both lanes.
        returned_days_ago=returned_days_ago,
        returned_by_reaper=returned_by_reaper,
        ratings=rating_set,
    )


@dataclass(frozen=True, slots=True)
class RadarrSource:
    """One Radarr instance, and the id it is known by."""

    client: RadarrClient
    instance_id: int
    name: str
    # This instance's HD/4K library map: {root folder path: Plex library title}. Empty when the
    # operator has set none, which keeps the resolver on its size/abstain behavior for a movie
    # listed in two libraries.
    library_map: Mapping[str, str] = field(default_factory=dict)


#: Re-exported so callers wire both media types from one module. The TV gather itself
#: lives in ``season_scan``, since it is a large, self-contained read path, but a Sonarr
#: instance is a scan source exactly as a Radarr one is.
SonarrSource = season_scan.SonarrSource


#: The size-tally bucket for an item no source would size. Not a ``SizeSource`` value,
#: because it is the absence of one. Kept distinct so the log line reads as counts of
#: rungs plus a miss count, rather than inventing a rung that means "none".
_UNMEASURED = "unmeasured"


def _size_bucket(source: str | None) -> str:
    """Which tally bucket one item falls in: the rung that fired, or the miss bucket.

    Returns a plain ``str`` rather than the enum member, so the log line reads
    ``{'radarr': 900, 'unmeasured': 3}`` instead of a row of enum reprs. This exists to
    be read by an operator pasting a log into an issue.
    """
    return str(source) if source else _UNMEASURED


#: Bounds concurrent `collection_children` reads within one snapshot's collection pass.
#: This only covers the collections the item tags could not account for, usually a
#: handful. Kept bounded anyway: nothing caps how many collections a library keeps
#: membership for elsewhere. Separate from leaving_soon.SHELF_CONCURRENCY, which bounds a
#: different fan-out: whole libraries, not collections within one.
_COLLECTION_CHILDREN_CONCURRENCY = 8


async def _collection_membership(
    plex: PlexClient | None, *, allowed_sections: set[int] | None
) -> tuple[dict[int, list[str]], dict[str, int]]:
    """Every Plex item's collection names, and every collection's Plex-reported size.

    Collections are for navigation only, never protection: see
    docs/history/COLLECTIONS_PLAN.md. A read failure here, such as no Plex configured, a
    section listing that raises, or one bad collection, returns whatever Reaper gathered
    so far. It never raises into the scan or degrades it, since a collection is not
    evidence, so the cost of a failed read is only a missing chip.

    The collection name is the identity. Reaper's own Leaving Soon shelf creates a
    same-named collection in every section, so an operator with two libraries sees this by
    default and expects one "Leaving Soon" chip to cover both. Same-named collections in
    different sections merge into one membership entry, and their known sizes are summed
    rather than letting a later section overwrite an earlier one's count.

    A collection whose child count Plex never reported is a different fact from one Plex
    reported as empty. It is left out of the size map rather than folded to 0, because the
    sort below would otherwise treat it as the smallest collection and give it the chip's
    first slot ahead of a genuinely small one. Each item's list sorts smallest known
    collection first (Plex's own child count), with an unknown size sorting last and ties
    broken alphabetically. This tie-break makes the chip render the same collection scan
    to scan, instead of flipping with dict-iteration order.

    **Membership comes from the items, not from the collections.** ``plex.collection_tags``
    reads about once per 400 items. Asking each collection for its own children would cost
    one request per collection, which on a library with hundreds of them would dominate a
    scan's Plex traffic and starve the GUID sweep running beside it.

    A collection Plex reports more members for than the tags showed falls back to a
    per-collection read. This covers two kinds of collection whose membership is not a
    tag: a smart collection is a saved filter, and a collection of seasons or episodes
    holds objects the section-level listing never lists. Comparing against ``child_count``
    finds both without asking Plex which kind it is. The ``smart`` flag cannot do this,
    because it is absent from the listing on a server with no smart collections, so a
    check keyed on it could not be proven to work.

    A tag naming no collection in the section's own listing is dropped, since Plex leaves
    one behind when a collection is deleted, and a chip for a shelf nobody can open is
    worse than no chip. Tags are matched to the listing casefolded, and the listing's own
    spelling is stored, so a chip's name is the same name the size map is keyed by.

    The fallback reads run concurrently, bounded by ``_COLLECTION_CHILDREN_CONCURRENCY``.
    One collection's failure is caught inside its own task and logged, rather than raised
    into the fan-out, so it can never cancel a sibling read or degrade the snapshot. A
    collection read both ways lands on one chip, since the per-item names are a set.
    """
    if plex is None:
        return {}, {}
    try:
        sections = await plex.video_sections()
    except PlexError as exc:
        log.warning("snapshot.collections_unreadable", error=str(exc))
        return {}, {}

    # A SET per item: a collection the tags covered AND the fallback read returns arrives
    # twice, and the sort below is a total order, so nothing depends on insertion order.
    membership: dict[int, set[str]] = {}
    sizes: dict[str, int] = {}
    bound = asyncio.Semaphore(_COLLECTION_CHILDREN_CONCURRENCY)

    async def _children(row: PlexCollectionRow) -> tuple[str, set[int] | None]:
        try:
            async with bound:
                return row.title, await plex.collection_children(row.rating_key)
        except PlexError as exc:
            log.warning(
                "snapshot.collection_children_unreadable",
                collection=row.rating_key,
                error=str(exc),
            )
            return row.title, None

    for section in sections:
        if allowed_sections is not None and section.key not in allowed_sections:
            continue
        try:
            rows = await plex.list_collections(section.key)
        except PlexError as exc:
            log.warning("snapshot.collections_unreadable", section=section.key, error=str(exc))
            continue

        for row in rows:
            if row.child_count is not None:
                sizes[row.title] = sizes.get(row.title, 0) + row.child_count

        try:
            tags = await plex.collection_tags(section.key, kind=section.kind)
        except PlexError as exc:
            # The section's own listing already succeeded, so its sizes stand and every
            # collection in it falls to the per-collection read below. A collection is
            # not evidence, so this failure does not degrade the snapshot.
            log.warning("snapshot.collection_tags_unreadable", section=section.key, error=str(exc))
            tags = {}

        by_fold = {fold(row.title): row.title for row in rows}
        seen: Counter[str] = Counter()
        for key, names in tags.items():
            for name in names:
                stored = by_fold.get(fold(name))
                if stored is None:
                    continue
                seen[stored] += 1
                membership.setdefault(key, set()).add(stored)

        unexplained = [row for row in rows if row.child_count and seen[row.title] < row.child_count]
        if unexplained:
            log.info(
                "snapshot.collection_children_fallback",
                section=section.key,
                collections=len(unexplained),
                of=len(rows),
            )
        # Argument order, not completion order (reaper.aio.gather_reaped), so the merge
        # below is deterministic run to run even though the reads race.
        for title, children in await gather_reaped(*(_children(row) for row in unexplained)):
            if children is None:
                continue
            for key in children:
                membership.setdefault(key, set()).add(title)

    def _size_key(name: str) -> tuple[int, int, str]:
        size = sizes.get(name)
        return (0, size, name) if size is not None else (1, 0, name)

    sorted_membership = {key: sorted(names, key=_size_key) for key, names in membership.items()}
    return sorted_membership, sizes


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
    list_config_hash: str | None = None,
) -> Snapshot:
    """Gathers, freezes, judges, and persists a snapshot. Read-only throughout.

    Movies are judged under ``movie_policy`` and seasons under ``tv_policy``, since the two
    are tuned separately, so the snapshot's ``policy_hash`` and ``scoring_hash`` are the
    combination of both. See ``policy.combine_hashes``. The simulator recombines the same
    way to stay honest per media type.

    Every Radarr instance is scanned, not just one. A separate 4K instance alongside the HD
    one is a common setup, and scanning only the first instance found would silently ignore
    an entire library while still reporting a clean, confident, non-degraded result.

    ``media_key`` already carries the instance id, so the same film in the HD and 4K
    instances is two distinct rows. That is correct: they are two distinct files.
    """
    emit = on_progress or (lambda _p: None)
    requested = requested or {}

    context = ScanContext(horizon=utcnow())

    # ---- gather ------------------------------------------------------------
    emit(Progress("gathering", 0, 5, Reason("watch_history")))

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
        # Degrade after the context is rebuilt with the resolved horizon, not before: the
        # earlier throwaway context is about to be replaced, so degrading it first would
        # silently drop the reason. A scan with no watch history at all can judge nothing
        # safely, so it must never look non-degraded and executable.
        context.degrade("no watch history at all: nothing can be judged")
    else:
        # An empty mirror is caught above. A stale one is invisible without this. Watch
        # stats come from the local mirror, not a live call, so a stopped ingest raises
        # nothing: watcher counts stay frozen at their last value while dormancy keeps
        # climbing, and every item drifts toward condemnation at the rate of the outage.
        #
        # Ask when the ingest last ran (`history_sync.last_synced_at`), never when somebody
        # last watched something (`mirror.latest`). The two look identical for a stalled
        # ingest and for a quiet library, so gating on the newest play would tell a server
        # whose users went away for a weekend that its watch history is broken, and block
        # every deletion until somebody watches something.
        synced = await history_sync.last_synced_at(engine)
        if synced is None or utcnow() - synced > MIRROR_STALE_AFTER:
            context.degrade(
                "watch history has not updated recently, so nothing can be judged on how "
                "long it has gone unwatched"
            )
        # The third state is the one both checks above call healthy: populated, synced
        # recently, and holding only a fraction of what the source has. It is reached
        # through the ordinary route, not a broken one: a restore leaves the mirror behind
        # (`services/backup.py` excludes it deliberately), and every sync from then until
        # the first full sweep is incremental, so each one completes correctly against a
        # paging total that is only the size of its own increment and never notices the
        # hole underneath. `synced_at` is stamped by `_check_regression` before the walk,
        # so the clock above always reads fresh.
        #
        # A short mirror caps dormancy, making an item look less condemnable, and also
        # reports fewer distinct watchers, making it look more condemnable. So this
        # degrades rather than warns: coverage collapsing is the scorer working correctly
        # (a score can only fall as evidence goes missing), but presenting that result as
        # executable is not.
        elif (shortfall := mirror.shortfall) is not None and (
            shortfall > MIRROR_SHORTFALL_FLOOR
            and shortfall > (mirror.source_total or 0) * MIRROR_SHORTFALL_FRACTION
        ):
            # `shortfall is None` means no sync ever recorded a source total: Reaper was
            # never told, which is Unknown, not a clean bill of health. That case is left
            # to the staleness guard above, which a mirror nobody has ever synced already
            # fails.
            context.degrade(
                "watch history is still catching up, so nothing can be judged on how long "
                "it has gone unwatched. Let it finish, then scan again"
            )

    # Failures the caller detected before the gather (an unreachable Plex, a protection list
    # that failed to sync with an empty keep-list) degrade this snapshot the same loud,
    # un-executable way an in-gather failure does. A whitelist that could not refresh must
    # never let a reap run against an empty keep-list. Fail closed, exactly as the source
    # failures below do.
    for reason in extra_degrade_reasons or ():
        context.degrade(reason)

    emit(Progress("gathering", 1, 5, Reason("active_streams")))
    try:
        activity = await tautulli.activity()
        sessions = activity.get("sessions")
        if not isinstance(sessions, list) or not all(isinstance(s, dict) for s in sessions):
            # Treat a 200 with a null or wrong-shaped body as unreadable, never as "nobody
            # is watching". Coercing it to an empty list would read as a measured "nothing
            # is streaming" and defeat the veto on every item. This gets the same treatment
            # as an unreachable server: Reaper could not check.
            context.degrade("could not read what is playing right now")
            context.activity_degraded = True
        else:
            unreadable = False
            for session_data in sessions:
                for key in ("rating_key", "parent_rating_key", "grandparent_rating_key"):
                    value = session_data.get(key)
                    if not value:
                        continue
                    try:
                        context.active_rating_keys.add(int(value))
                    except (TypeError, ValueError):
                        # One stream Reaper cannot identify. It may be an item this scan is
                        # about to judge, so this counts as "could not check", not "not it".
                        unreadable = True
            if unreadable:
                context.degrade("could not read what is playing right now")
                context.activity_degraded = True
    except IntegrationError as exc:
        # Do NOT assume nothing is playing. That is how you delete a file mid-stream.
        context.degrade(f"could not read what is playing right now: {exc}")
        context.activity_degraded = True

    # Manual spares are applied via the override, not the whitelist gate, so the gate is left to
    # the *arr-tag / collection whitelists alone. An empty set keeps that path tag-only.
    tag_only_whitelist: set[str] = set()
    # Every enabled protection-list row, loaded once and shared by the movie and season
    # judges. The judge loops below ask "which lists contain this item?" for every movie
    # and every show. Answering each from SQLite made the loop's runtime scale with the
    # library. One load, then dict hits.
    membership_index = await lists.load_membership_index(engine)

    # ---- fan out across the independent sources -----------------------------
    # Everything past the activity read touches different services: the Plex and Tautulli
    # movie index, each Radarr's movie list, and the whole TV season gather (Sonarr, Plex,
    # Tautulli again). They run concurrently, so the gather takes as long as its slowest
    # source instead of the sum of all of them. The freeze-then-judge rule still holds:
    # nothing is scored until every one of these has completed or degraded. Only the
    # waiting overlaps.
    emit(Progress("gathering", 2, 5, Reason("libraries")))

    # Wall clock of the whole concurrent gather (fan-out to last await). The per-source
    # self-times above tell which source dominates this wall. This is the wall itself.
    gather_wall_started = time.monotonic()

    # Every task the fan-out creates goes through _spawn, so the reap on failure below
    # covers all of them by construction. A future branch cannot be forgotten.
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
    # Read before the TV task is spawned, because that task holds no session of its own and
    # its season keys do not exist yet. Serves both lanes: one read per scan, not per item.
    watch_marks = await watch_evidence.recall_all(session)
    # Read here for the same reason, and handed to the season task the same way: the
    # ledger is keyed on external ids that `season_scan.gather` resolves internally, and
    # the scan timings answer "did Reaper run while this was missing" for both lanes at
    # once.
    seen_marks = await library_seen.recall_all(session)
    seen_scans = await library_seen.scan_instants(session)
    if sonarrs:
        season_task = _spawn(
            season_scan.gather(
                engine,
                watch_marks=watch_marks,
                seen_marks=seen_marks,
                seen_scans=seen_scans,
                seen_absence_days=tv_policy.returned_absence_days(),
                sonarrs=sonarrs,
                tautulli=tautulli,
                plex=plex,
                horizon=context.horizon,
                reach_days=context.reach_days,
                active_rating_keys=context.active_rating_keys,
                activity_degraded=context.activity_degraded,
                # The scan and the simulator both reach the planner's nine season settings
                # through one path, ``SeasonPolicy.from_body``, so a field added to the
                # season card only needs to be written once.
                season_policy=season_evidence.SeasonPolicy.from_body(tv_policy),
                window_days=tv_policy.popularity_window_days(),
                whitelisted=tag_only_whitelist,
                degrade=context.degrade,
                requested=requested,
                request_index=request_index,
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

        Fetched once per instance, never per item. ``None`` is not ``()``: an instance
        that reports no roots is answering, while a failed read is not. On ``None``,
        :func:`identity._narrow_among_id_hits` refuses to narrow an ambiguous id at all.
        Losing the roots removes the folder-vs-size contradiction veto, and without it a
        stale Plex size could bind a copy the folder would have disputed.

        A failure here keeps the snapshot un-degraded, unlike most evidence failures: the
        refusal above is the compensating control, and it keeps every affected file.
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
    # A read-only extension of the same gather. It never raises, see the function's own
    # docstring, so it needs no place in the except below: there is nothing for it to
    # leave half-done that a reap would need to clean up.
    collections_task = _spawn(
        _collection_membership(plex, allowed_sections=allowed_sections), name="collections"
    )
    movie_tasks = [_spawn(_movies_from(source), name="radarr") for source in radarrs]
    roots_tasks = [_spawn(_roots_from(source)) for source in radarrs]

    items: list[RawItem] = []
    season_judgments: list[season_scan.SeasonJudgment] = []
    try:
        # Awaited in the same order the sequential code used, so the first failure to
        # surface is the same one it would have raised then. The except below reaps every
        # other task.
        plex_index = await index_task
        collection_membership, collection_sizes = await collections_task
        for source, movie_task, roots_task in zip(radarrs, movie_tasks, roots_tasks, strict=True):
            movies = await movie_task
            roots = await roots_task
            if movies is None:
                continue
            items.extend(
                _raw_items(
                    movies,
                    plex_index,
                    source.instance_id,
                    requested,
                    root_folders=roots,
                    library_map=source.library_map,
                )
            )

        emit(Progress("gathering", 4, 5, Reason("ratings")))
        # Look up by BOTH Radarr's imdbId and the matched Plex item's imdb id, so a film
        # whose Radarr record lacks (or has a wrong) imdbId still gets its rating when
        # Plex knows it.
        # Deduped: an item's Radarr imdbId and its Plex-matched imdb id are usually the
        # same string, and the dataset lookup returns a keyed map, so passing each id once
        # changes nothing but the chunk count.
        imdb_ids = list(dict.fromkeys(x for i in items for x in (i.imdb_id, i.plex_imdb_id) if x))
        try:
            imdb = await ImdbRatings(engine).lookup(imdb_ids)
            # The first thing to check for "why did the rating floor protect nothing": low
            # coverage means most items had no dataset rating to keep them. Per scan, so info.
            log.info(
                "scan.imdb_coverage", media="movie", requested=len(imdb_ids), resolved=len(imdb)
            )
        except DatasetDegradedError as exc:
            # The inverted failure: a missing rating removes protection. Degrade loudly,
            # and flag it so build_facts reads every title as "could not check" rather
            # than "checked, and it is unrated". Degrading alone does not stop the condemn
            # set from being built on ratings nobody could read.
            context.degrade(str(exc))
            imdb = {}
            context.imdb_degraded = True
        movie_candidate_keys = {i.plex_rating_key for i in items if i.plex_rating_key}
        # Every merged bind's listing keys, by its canonical key. Shared below by the
        # popularity fold and the rewatch gather, so a file listed twice in Plex is
        # clustered over the same union of listings for both.
        merged_groups = {
            i.plex_rating_key: i.merged_rating_keys
            for i in items
            if i.plex_rating_key is not None and i.merged_rating_keys
        }
        last_played, watchers_window, watchers_all_time = await _watch_stats(
            engine,
            rating_keys=movie_candidate_keys,
            window_days=movie_policy.popularity_window_days(),
        )
        # A merged bind is one file listed several times in Plex. Its plays are split
        # across the listings' rating keys. Fold each group's stats onto its canonical
        # key, or the item would under-count its own watching, the direction that
        # condemns.
        await _fold_merged_watch_stats(
            engine,
            groups=merged_groups,
            window_days=movie_policy.popularity_window_days(),
            last_played=last_played,
            watchers_window=watchers_window,
            watchers_all_time=watchers_all_time,
        )
        # Qualified viewing stats for the habitual-rewatch keep, over the same candidate
        # set and the same merged-listing fold as the popularity counts above.
        rewatch_stats = await movie_rewatch_stats(
            engine, movie_candidate_keys, groups=merged_groups
        )
        # The movie lane's rewatch-probability fit, refit every scan over exactly the
        # candidate set the scorer scores below (the same movie_candidate_keys and
        # merged_groups the stats gather above uses). The season task fits the TV curve
        # the same way, in season_scan.gather, over its own candidate set. Cutoff is a
        # year back from scan time (see docs/history/REWATCH_PLAN.md, Stage 2 Fit). Added
        # dates for the fallback training-pair route come off the scan's own items, never
        # a second read.
        rewatch_cutoff = utcnow() - timedelta(days=365)
        rewatch_outcomes = await movie_rewatch_outcomes(
            engine, movie_candidate_keys, cutoff=rewatch_cutoff, groups=merged_groups
        )
        rewatch_pairs = [
            pair
            for item in items
            if item.plex_rating_key is not None
            and (
                pair := training_pair(
                    rewatch_outcomes.get(item.plex_rating_key),
                    added_at=item.added_at,
                    cutoff=rewatch_cutoff,
                )
            )
            is not None
        ]
        rewatch_curve = fit_blocks(rewatch_pairs)
        if season_task is not None:
            emit(Progress("gathering", 4, 5, Reason("seasons")))
            season_judgments = await season_task
    except BaseException:
        # A failure on any branch aborts the scan. The surviving branches are reaped
        # first (canceled, drained, late failures logged), so nothing keeps reading from
        # sources after the scan is already dead, and no task's failure goes unobserved.
        # Every task is in fanned_out because every task was created by _spawn.
        await reap(fanned_out)
        raise

    gather_wall_ms = round((time.monotonic() - gather_wall_started) * 1000)

    # ---- freeze ------------------------------------------------------------
    snapshot = Snapshot(
        created_at=utcnow(),
        # Movies and seasons are judged under different policies, so the snapshot records
        # the combination of both, movie first, TV second. See policy.combine_hashes.
        policy_hash=combine_hashes(movie_policy.policy_hash(), tv_policy.policy_hash()),
        scoring_hash=combine_hashes(movie_policy.scoring_hash(), tv_policy.scoring_hash()),
        # What was gathered and frozen. This lets the simulator replay a weight or rating
        # edit from each Candidate's facts_json without a re-scan
        # (services.snapshot._judge_item freezes them). Movie first, TV second, exactly
        # like the other combined hashes.
        evidence_hash=combine_hashes(movie_policy.evidence_hash(), tv_policy.evidence_hash()),
        # The lists this scan gathered membership from. Not a policy field, so it is
        # recorded separately from the hash above: a retagged or renamed list changes what
        # every `on_list` rule protects without moving one byte of any policy body. `None`
        # when the registry could not be read. That fails closed, and the scan is degraded
        # and un-plannable either way.
        list_config_hash=list_config_hash,
        horizon_at=context.horizon,
        item_count=len(items) + len(season_judgments),
        degraded=context.degraded,
        # A space, not "; ": `degrade` terminates every reason, so these are whole
        # sentences, and a semicolon between them would read "...this scan.; radarr 'x'
        # unreachable".
        degraded_reason=" ".join(context.degraded_reasons) or None,
        # Every collection this scan saw, matched to Plex's own member count. NULL when
        # none were read, whether none exist or the read failed. The two are
        # indistinguishable on purpose: see docs/history/COLLECTIONS_PLAN.md. This is
        # navigation, never protection, so it never degrades the snapshot either way.
        collection_sizes_json=(json.dumps(collection_sizes) if collection_sizes else None),
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
    # Scoring configs are pure functions of the frozen policies, identical for every
    # item, so build them once here instead of once per item inside the judge loops.
    movie_custom = movie_policy.custom_signal_configs()
    movie_keeps = movie_policy.keep_configs()
    tv_custom = tv_policy.custom_signal_configs()
    tv_keeps = tv_policy.keep_configs()
    now = utcnow()
    # The owner's manual overrides (``media_key -> "spare" | "reap"``) apply to every
    # item's verdict. A spared file is judged PROTECT rather than surfacing in "would
    # delete". A reaped one is forced onto the list, short of a hard safety gate. Keys may
    # be a show's, covering all its seasons. Read as of the scan's `now`, so an expired
    # timed spare is dropped here, the one place a spare's clock is realized, and the item
    # is re-judged from scratch, re-entering the reap flow on a fresh grace window (see
    # record_first_flagged_bulk below). A live consumer that has not run this yet keeps an
    # expired spare in force.
    override_map = await whitelist.overrides_effective_at(session, now)
    # Realize the expiry durably. `overrides_effective_at` dropped expired spares from the
    # map above (the read half), so their rows are deleted now, in this same transaction
    # (the write half). Otherwise every live consumer that reads `whitelist.overrides()`
    # (planner, executor, grace, review queue) would keep the expired spare in force
    # forever, and the item would dead-end: unplannable and un-executable. Using the same
    # `now` keeps the two halves in exact agreement. The re-condemned item earns a fresh
    # grace clock from record_first_flagged_bulk below, because its old clock was deleted
    # when the spare was set. Only a count is logged, never a title or key.
    expired_spares = await whitelist.purge_expired_spares(session, now)
    if expired_spares:
        log.info("scan.spares_expired", snapshot=snapshot.id, count=len(expired_spares))
    total = len(items) + len(season_judgments)

    # Both lanes append here, and every count of the condemned set is this list's length.
    condemned_keys: list[str] = []
    # Which rung of the size ladder actually fired, counted across the whole scan. This is
    # the only place that tracks how often a size goes unreported. Counts only, never a
    # title or a path.
    #
    # The miss bucket is seeded so it is reported as 0 rather than omitted. It is the one
    # number an operator greps this line for, and a `Counter` drops an absent key, so
    # "nothing went unmeasured" and "the line was never emitted" would read identically. A
    # rung is left unseeded on purpose: absent there genuinely means it did not fire, and
    # seeding every member would print `sonarr: 0` on a Radarr-only install.
    size_sources: Counter[str] = Counter({_UNMEASURED: 0})

    # Scoring is pure in-memory (no per-item I/O), so this measures the CPU cost of judging
    # every movie and season. It is kept apart from the source-read wall above, so a slow
    # scan is attributable to a source or to scoring, never lumped into one number.
    score_started = time.monotonic()
    watch_readings: dict[str, watch_evidence.Reading] = {}
    watch_blind = 0
    # Both lanes append sightings here, accumulated in memory and flushed once after both
    # lanes finish, exactly as `watch_readings` is. Whether Reaper's own journal claims a
    # return is decided later, in one query, off `seen_returned` below.
    seen_keys: dict[str, set[int]] = {}
    seen_returned: set[str] = set()
    movie_absence_days = movie_policy.returned_absence_days()
    for index, item in enumerate(items):
        if index % 100 == 0:
            emit(Progress("scoring", index, total, Reason("scoring", {"title": item.title})))
            # The judge is pure computation (no per-item queries), so without this the
            # loop would hold the event loop for the whole scoring phase, freezing the
            # very progress endpoint the emit above feeds.
            await asyncio.sleep(0)

        reading = watch_evidence.reading_for(item.plex_rating_key, watchers_all_time, last_played)
        blind_reason: str | None = None
        if reading is not None:
            watch_readings[item.media_key] = reading
            blind_reason = watch_evidence.went_blind(watch_marks.get(item.media_key), reading)
            if blind_reason is not None:
                watch_blind += 1
                # Per item, because a count alone cannot be chased down, and this one asks
                # the operator to go look at Tautulli. The media_key is an internal
                # coordinate, never a title or a path.
                log.warning(
                    "scan.watch_history_unreadable",
                    media_key=item.media_key,
                    media_type=item.media_type,
                )

        # The ledger read, and the detection off it. Both need only values already in
        # hand, so this stays inside the pure loop and the write is deferred with the
        # rest. A key is built for every item that has one, but a sighting is recorded
        # only on a confident bind. No bind means no write, so a Plex outage records
        # nothing rather than recording an absence (`services.library_seen`).
        item_id_key = library_seen.id_key(
            media_type="movie",
            tmdb=item.tmdb_id,
            # Both id spellings, the movie path exactly as the TV path.
            imdb=item.imdb_id or item.plex_imdb_id,
        )
        seen = seen_marks.get(item_id_key) if item_id_key is not None else None
        if (
            item_id_key is not None
            and item.plex_rating_key is not None
            and item.match_status is identity.MatchStatus.MATCHED
        ):
            sighting = library_seen.Sighting(
                id_key=item_id_key,
                rating_key=item.plex_rating_key,
                added_at=item.added_at,
            )
            library_seen.note_sighting(seen_keys, sighting)
            if seen is not None and library_seen.is_return(
                seen,
                sighting,
                # The whole Plex index: an earlier key for this id could have been any
                # listing, and the index is the only complete answer to "does it still
                # exist". A dict lookup per recorded key, and a title has one or two.
                live_keys=plex_index.by_rating_key,
                scan_instants=seen_scans,
                cooling_off_days=movie_absence_days,
                now=now,
            ):
                seen_returned.add(item_id_key)

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
            watch_blind_reason=blind_reason,
            rewatch=rewatch_stats,
            rewatch_curve=rewatch_curve,
            seen=seen,
        )
        # The same cohort_block decision build_facts made internally, re-derived from the
        # dormancy value it froze onto `facts`. One derivation, two call sites, so the two
        # can never disagree. Carried to `_judge_item` separately because `Facts` does not
        # hold a block's dormancy bounds. `_rewatch_odds_context` reads this for the
        # stored explanation's rewatch_odds block.
        rewatch_block = (
            cohort_block(
                rewatch_curve, facts.days_observed_unwatched.value, reach_days=context.reach_days
            )
            if isinstance(facts.days_observed_unwatched, Known)
            else None
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
            # The scoring lane reads the honest Observation off `facts`. This is the
            # display and reclaim-accounting column. None means Radarr reported a file it
            # holds without a size, and it stays None: no file worth deleting is genuinely
            # 0 bytes, so a stored 0 would be a measurement Reaper never took.
            #
            # While the owner's allowance (`ProfileSettings.max_unmeasured_per_run`) is
            # shut, that costs the item deletion: `planner.build_plan` holds it back, and
            # `executor._may_send_unmeasured` refuses it again per item. Both caps and the
            # byte total the owner confirms leave it out. With the allowance open the item
            # is planned and does count against the item caps. Only the byte sums still
            # leave it out (`executor._deletable_bytes`). Either way it still scores,
            # still shows in the queue, and says "Size unknown" wherever its size would
            # appear. Never invent a size here.
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
            display=Display(
                year=item.year,
                summary=item.summary,
                requested_by=item.requested_by,
                tmdb_id=item.tmdb_id,
                # Radarr's id first, the Plex-matched one as fallback. Same precedence the
                # dataset lookup uses.
                imdb_id=item.imdb_id or item.plex_imdb_id,
                # A movie has no TVDb id, since Radarr is tmdb-native. Left None, so the
                # Scales join binds a movie request by tmdb or imdb.
                tvdb_id=None,
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
            match_candidates=item.match_candidates,
            override=whitelist.effective_override(item.media_key, override_map),
            # Typed onto the stored explanation so the panel can offer the per-title escape
            # without reading the reason text. False, not None: this scan took a reading
            # and it was honest. None is reserved for a row scanned before the key existed,
            # and for an item that had no reading to judge at all.
            watch_blind=blind_reason is not None if reading is not None else None,
            rewatch_block=rewatch_block,
            # A movie's own rating key is what a movie-library collection lists. None when
            # unmatched, since an unresolved item was never looked up.
            collections=(
                collection_membership.get(item.plex_rating_key)
                if item.plex_rating_key is not None
                else None
            ),
        )
        if verdict == "condemn":
            condemned_keys.append(item.media_key)

    # What each show's season plan was decided from, frozen once per show. Every season of
    # a show carries the same bundle object, so this dedupes to one row per show. Without
    # the dedupe, a ten-season show would store it ten times for nothing. Written even for
    # a show whose every season is kept, which is the show a lowered keep-last makes
    # prunable, and therefore the one the simulator is most often asked about.
    seen_shows: set[str] = set()
    for judgment in season_judgments:
        show = judgment.group_key
        if show is None or show in seen_shows:
            continue
        seen_shows.add(show)
        session.add(
            SeasonPruneEvidence(
                snapshot_id=snapshot.id,
                group_key=show,
                payload_json=json.dumps(season_evidence.to_dict(judgment.prune_input)),
            )
        )

    # Seasons run through the SAME judge: the season-pruning guard is merged in as an
    # extra gate result, so a protected season is protected by a gate exactly as a
    # streamed movie is, and the why-panel renders both identically.
    for offset, judgment in enumerate(season_judgments):
        if offset % 100 == 0:
            emit(
                Progress(
                    "scoring",
                    len(items) + offset,
                    total,
                    Reason("scoring", {"title": judgment.title}),
                )
            )
            await asyncio.sleep(0)  # keep the event loop live, see the movie loop above
        if judgment.watch_reading is not None:
            # The TV lane already decided blindness against these same marks and put the
            # reason on its facts. The reading is carried out here only so both lanes'
            # marks are raised in one write below.
            watch_readings[judgment.media_key] = judgment.watch_reading
        if judgment.watch_blind_reason is not None:
            # Counted from the decision the TV lane already made, never re-derived here.
            watch_blind += 1
        if judgment.seen_sighting is not None:
            # Same shape, same reason: the TV lane decided this against the same marks and
            # already put the result on its facts. The sighting rides out here only so
            # both lanes are written in one statement below. The population cap therefore
            # reads a whole scan rather than one lane.
            library_seen.note_sighting(seen_keys, judgment.seen_sighting)
            if judgment.seen_returned:
                seen_returned.add(judgment.seen_sighting.id_key)
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
            # A season's poster is the show's, not the season's. Shows always have one.
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
            display=Display(
                year=judgment.year,
                summary=judgment.summary,
                requested_by=judgment.requested_by,
                group_key=judgment.group_key,
                group_title=judgment.group_title,
                tmdb_id=judgment.tmdb_id,
                imdb_id=judgment.imdb_id,
                tvdb_id=judgment.tvdb_id,
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
            match_candidates=judgment.match_candidates,
            extra_results=(judgment.guard_result,),
            override=whitelist.effective_override(judgment.media_key, override_map),
            # The season lane reads its history by the season's own Plex key, so the same
            # blindness is detectable here, and the escape must be offerable on it too. The
            # season's reading is decided in `season_scan` before the show roll-up, so this
            # reads the judgment rather than recomputing it.
            watch_blind=(
                judgment.watch_blind_reason is not None
                if judgment.watch_reading is not None
                else None
            ),
            # The show's rewatch cohort block, the same one that fed
            # `judgment.facts.rewatch_cohort_n` and `rewatch_cohort_k`. The movie call
            # above passes its own the same way, for the same reason
            # (`_rewatch_odds_context`).
            rewatch_block=judgment.rewatch_block,
            # A TV collection lists shows, not seasons: the same key the poster uses,
            # never `plex_rating_key`, which is the season's own.
            collections=(
                collection_membership.get(judgment.poster_rating_key)
                if judgment.poster_rating_key is not None
                else None
            ),
        )
        if verdict == "condemn":
            condemned_keys.append(judgment.media_key)

    # A library-wide identity event, which is the Plex-side twin of the Tautulli regression
    # check. Both lanes have bound by here, so this is the first point the share can be
    # measured, and it reads evidence already in hand rather than asking anything.
    #
    # This check runs above the grace clocks deliberately. The grace clocks gate on
    # `context.degraded`, so a degradation found after them would leave a countdown
    # running on a scan that just declared itself untrustworthy.
    if (identity_moved := identity_churn.wholesale_change(seen_marks, seen_keys)) is not None:
        context.degrade(identity_moved)
        # `snapshot.degraded` and `degraded_reason` were already set once, from this same
        # context, when the Snapshot row was constructed above. This is the one
        # degradation that could not be known at that point, so it is restated here rather
        # than shared: that earlier value was an argument inside the constructor call, and
        # the join below is a space, for the same reason written there.
        snapshot.degraded = context.degraded
        snapshot.degraded_reason = " ".join(context.degraded_reasons) or None
        # The page the notice offers. Set here, rather than returned with the sentence,
        # because it belongs to this cause alone. A scan that also degraded for an
        # unreachable Radarr still points at the rebuild guide, the one thing the operator
        # can act on, and a later cause with its own page would overwrite a link nobody
        # can follow twice.
        snapshot.degraded_doc = identity_churn.HELP_DOC

    # Grace clocks for everything condemned this run, in one batched pass: the
    # _apply_first_flag decision per key, without a database round trip per item.
    #
    # Not on a degraded run. The condemn set here was built on evidence Reaper itself
    # declared untrustworthy, and planner.build_plan already refuses it outright. But the
    # clock write does not go through the planner, so it would start, or silently
    # continue, a countdown for files a healthy scan would have kept. Worse, because
    # _apply_first_flag restarts a clock only after a gap longer than a whole grace
    # window, a run of consecutive degraded scans keeps refreshing last_seen_condemned_at
    # and the window never restarts. The first healthy scan then finds it already spent,
    # and the item's warning time is gone. Skipping can only ever cause an extra restart,
    # which means more grace, the safe direction.
    if context.degraded:
        log.info(
            "scan.grace_clocks_skipped",
            snapshot=snapshot.id,
            condemned=len(condemned_keys),
            reason="degraded",
        )
    else:
        await record_first_flagged_bulk(session, condemned_keys, now, grace_days=grace_days)

    await session.flush()
    # Raise every item's mark to cover what this scan measured, both lanes in one write.
    # Written even for the items just flagged blind: the mark only ever rises, so a blind
    # reading cannot lower it, and the next scan asks the same question against the same
    # evidence instead of quietly accepting zero as the new truth.
    #
    # Deliberately not gated on `context.degraded`, unlike the grace clocks above. Most of
    # a degraded scan's side effects are gated on it because they act on the condemned
    # set: a clock, a shelf, a Discord post all push an item toward deletion. This write
    # is the opposite kind. The mark is evidence a later scan reads to lower an item's
    # deletion score, and raising it can only ever add a reason to keep the item. Skipping
    # it is what costs a protection.
    #
    # Concretely: `degraded` is snapshot-global and mostly fires on causes that say
    # nothing about this item's watch reading, such as one *arr being unreachable, or
    # sessions or ratings being unreadable. Skip the write on those, and a title watched
    # by five people whose first scan happened to be degraded stores no mark. When its
    # Plex key later churns, there is nothing to fall back to, and it reads Known(0) with
    # maximum dormancy. That is the exact defect `watch_evidence` exists to prevent,
    # reintroduced by a gate meant to be careful.
    # `TestTheWatchBlindnessGuardThroughAWholeScan
    # .test_a_degraded_scan_still_records_what_it_measured` goes red if the gate is added.
    await watch_evidence.record(session, watch_readings, now=now)
    # The came-back ledger, both lanes in one write. Not gated on `context.degraded`, for
    # the same reason as `watch_evidence.record` above: this is evidence a later scan
    # reads to lower an item's deletion score, and skipping it costs a protection.
    # Degradation cannot manufacture a sighting either. A sighting is written only for an
    # item bound to Plex, so an unreadable source leaves the row untouched rather than
    # recording an absence.
    #
    # The cap is applied here rather than per item, which is why the detection is
    # accumulated instead of acted on. A Plex library rebuilt slowly enough to outlast the
    # minimum absence satisfies every condition for every title at once, and only a whole
    # scan's count can see that. Refusing the batch costs the memory of any real return
    # inside it. Granting it protects the whole library instead.
    if seen_returned and not library_seen.within_cap(len(seen_returned), len(seen_keys)):
        log.warning(
            "scan.returns_refused_population",
            returned=len(seen_returned),
            bound=len(seen_keys),
        )
        seen_returned = set()
    seen_by_reaper = await library_seen.removed_by_reaper(session, seen_returned)
    if seen_returned:
        log.info("scan.returns_detected", returned=len(seen_returned), ours=len(seen_by_reaper))
    await library_seen.record(
        session,
        seen_keys,
        returns={key: key in seen_by_reaper for key in seen_returned},
        now=now,
    )
    # Stored so Settings can say how many items the last scan held back for this reason,
    # which is the one number that tells an operator whether they need the reset at all.
    # Always written, zero included: a scan that counted none is a different fact from a
    # snapshot that never counted, which stays NULL.
    snapshot.watch_blind_items = watch_blind

    if watch_blind:
        log.warning(
            "scan.watch_history_unreadable_total",
            items=watch_blind,
            of=total,
        )

    score_ms = round((time.monotonic() - score_started) * 1000)
    emit(Progress("done", total, total, Reason("done", {"count": len(condemned_keys)})))

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
        condemned=len(condemned_keys),
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
    """The presentation fields carried onto a candidate. None of them feed the verdict.

    Four are load-bearing off that path. ``tmdb_id``, ``imdb_id`` and ``tvdb_id`` go onto
    the stored row and are what ``services/fairness.py`` joins a request to its candidate
    on. ``title_slug`` builds the Sonarr link (``services/deep_links.py``).

    Every field defaults to ``None``, and ``scan`` packs one of these per lane by hand, so
    a field set in the movie pack and forgotten in the season pack drops that join for TV
    with nothing raising.
    ``test_every_display_field_the_source_carries_reaches_its_lanes_pack`` is what refuses
    it, and its ``_DISPLAY_LANE_EXCEPTIONS`` holds the four fields one lane genuinely
    cannot answer."""

    year: int | None = None
    summary: str | None = None
    requested_by: str | None = None
    group_key: str | None = None
    group_title: str | None = None
    # Deep-link coordinates (the *arr web routes key on these, not on internal ids)
    # and the frozen display metadata. See services.display_meta and deep_links.
    tmdb_id: int | None = None
    imdb_id: str | None = None
    # The show's TVDb id for a season row. None for a movie, since Radarr is tmdb-native.
    # Sonarr is tvdb-native, so this is what Scales joins a TV request to its candidate on
    # when the show carries no tmdb id (services.fairness). Join and link only, never a
    # verdict input.
    tvdb_id: int | None = None
    title_slug: str | None = None
    video_resolution: str | None = None
    content_rating: str | None = None
    runtime_minutes: int | None = None
    # The Plex library (section) title. The show's for a season, its own for a movie.
    library: str | None = None
    ratings_json: str | None = None
    # "ended" / "continuing" / "unknown" for a season row, None for a movie. Built once,
    # by season_scan.show_status_key.
    show_status: str | None = None


#: The "no display fields" default, as a singleton so it is not constructed per call.
_NO_DISPLAY = Display()

#: What a hand spare reads as in the why-panel's "Protections that fired" list. A hand spare
#: wears the whitelist gate id, so the review chip (``api.review._kept_reason``) and the
#: simulator tally tell it apart from a real keep-list entry by this reason id. Every
#: producer and reader imports the constant. Never re-type the literal.
HAND_SPARE_REASON = Reason("hand_spare")


@dataclass(frozen=True, slots=True)
class PolicyJudgment:
    """One item judged by policy alone: no session, no stored row, no hand override.

    The pure half of :func:`_judge_item`: everything it computes before the
    ``session.add``, lifted out so that a caller which only wants the decision runs the
    scan's own code rather than a lookalike. The policy lab (``tests/_policy_lab.py``)
    sweeps hundreds of de-identified real shapes through it. A lab that rebuilt this
    pipeline by hand would drift from the scan and pin the drift as ground truth.

    ``verdict`` is the pure policy verdict, hand override held out, exactly as stored on
    the Candidate row. :func:`effective_fate` applies the override on top.
    """

    evaluation: Evaluation
    item_score: Score
    score: int
    coverage_bp: int
    verdict: str
    explanation: str


def judge_facts(
    facts: Facts,
    gates: list[Gate],
    policy: PolicyBody,
    *,
    signals: list[SignalConfig],
    custom_condemn: list[CustomSignalConfig],
    keeps: list[KeepConfig],
    window_days: int = 365,
    extra_results: Sequence[GateResult] = (),
    plex_rating_key: int | None = None,
    matched_by: identity.MatchedBy | None = None,
    match_detail: str | None = None,
    match_status: identity.MatchStatus | None = None,
    merged_rating_keys: tuple[int, ...] = (),
    match_candidates: tuple[int, ...] = (),
    watch_blind: bool | None = None,
    rewatch_block: RewatchBlock | None = None,
) -> PolicyJudgment:
    """Evaluates, scores, rounds, decides, and explains: the whole judgment, storing nothing.

    Round first, then decide, and the returned integers are exactly what decided. The
    stored integers are what the table shows, what the why-panel explains, and what the
    simulator re-decides. If the verdict were taken from the underlying float instead, an
    item scoring 69.7 against a threshold of 70 would abstain while storing a 70, and the
    simulator would later condemn the very item the queue said it was sparing. There must
    be exactly one number, and everything must decide on it.

    ``extra_results`` (the season-pruning guard's outcome) is merged ahead of the ordinary
    gates. A guard PROTECT wins like any protection, and a guard blocked ABSTAIN (a
    keep-rule conflict) forces the item to abstain for a human to look at.

    ``rewatch_block`` is the caller's already-derived rewatch cohort block for this item,
    the same one that fed ``facts.rewatch_cohort_n`` and ``rewatch_cohort_k``. It is
    carried separately because ``Facts`` does not hold the block's dormancy bounds. Both
    live lanes freeze a real block off their own fit. ``None`` means this item's cohort
    could not be measured, or the caller passed hand-built ``Facts`` with no fit to derive
    one from at all, such as a policy-lab or test fixture.
    """
    evaluation = Evaluation(results=[*extra_results, *evaluate_all(gates, facts).results])
    item_score = score(
        signals,
        facts,
        custom_condemn=custom_condemn,
        keeps=keeps,
        window_days=window_days,
    )

    score_value = round(item_score.value)
    coverage_bp = round(item_score.coverage * 10_000)

    return PolicyJudgment(
        evaluation=evaluation,
        item_score=item_score,
        score=score_value,
        coverage_bp=coverage_bp,
        verdict=_verdict(evaluation, score_value, coverage_bp, policy),
        # One explanation, two uses: the frozen record stored on the row, and the same
        # string the effective-reap fate is read back from. The scan and every read-time
        # consumer replay the identical evidence.
        explanation=_explain(
            evaluation,
            item_score,
            policy,
            plex_rating_key=plex_rating_key,
            matched_by=matched_by,
            match_detail=match_detail,
            match_status=match_status,
            merged_rating_keys=merged_rating_keys,
            match_candidates=match_candidates,
            watch_blind=watch_blind,
            rewatch_odds=_rewatch_odds_context(facts, rewatch_block),
        ),
    )


def effective_fate(judgment: PolicyJudgment, override: str | None) -> str:
    """The item's fate with a hand override applied. What the scan acts and counts on.

    Never what is stored. The Candidate row keeps the pure-policy verdict, so an un-spare
    or un-reap falls back to the real policy result instead of the item taking the
    override on as its identity. This is derived on top, and derived again downstream the
    same way.

    A hand reap is read off the frozen explanation through the one shared decision
    (``condemned.reap_override_verdict``), never off the live evaluation. A bad Plex match
    holds a reap on the read side but never reaches the gate evaluation, so deciding it
    live would honor a reap the planner and grace-clock resync hold, orphaning a grace
    clock the delete set never claims. A pure-policy condemn is trivially effective,
    mirroring ``condemned.reap_is_effective``'s shortcut.
    """
    if override == "spare":
        return "protect"
    if override == "reap":
        reap_condemns = judgment.verdict == "condemn" or (
            reap_override_verdict(judgment.explanation, score=judgment.score) == "condemn"
        )
        return "condemn" if reap_condemns else "protect"
    return judgment.verdict


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
    display: Display = _NO_DISPLAY,
    matched_by: identity.MatchedBy | None = None,
    match_detail: str | None = None,
    match_status: identity.MatchStatus | None = None,
    merged_rating_keys: tuple[int, ...] = (),
    match_candidates: tuple[int, ...] = (),
    extra_results: Sequence[GateResult] = (),
    override: str | None = None,
    watch_blind: bool | None = None,
    rewatch_block: RewatchBlock | None = None,
    collections: list[str] | None = None,
) -> str:
    """Evaluates one item's gates and signals, stores its candidate, and returns its
    effective fate.

    The candidate is stored with its pure policy verdict, with the hand override held
    out. The returned string is the effective fate with the override applied, for the
    caller's grace clock and condemned tally only. Storage stays override-free so an
    un-spare or un-reap falls back to the real policy result. The effective fate is
    recomputed downstream the same way.

    Shared by the movie and season paths so both reach a verdict the same way, and the
    judgment itself is :func:`judge_facts`, shared further still with the policy lab, so a
    sweep of real library shapes exercises the scan's own pipeline rather than a copy.

    Seasons pass ``extra_results``, the season-pruning guard's outcome, which is merged
    ahead of the ordinary gates. A guard PROTECT wins like any protection, and a guard
    blocked ABSTAIN (a keep-rule conflict) forces the item to abstain for a human.
    """
    # The stored verdict and explanation are pure policy: the scan never bakes a hand
    # override into them. A hand "spare" or "reap" lives only in the whitelist and is
    # re-applied live at read and plan time (whitelist.effective_override,
    # condemned.effective_condemned, reap_is_effective, and the simulator). Keeping the
    # stored verdict override-free is what lets an un-spare or un-reap fall back to the
    # real policy result, before a rescan and after one, instead of the item taking the
    # override on as its identity. The season-pruning guard (extra_results) is genuine
    # policy and stays. Only the hand override is held out here and applied on top
    # downstream.
    #
    # The grace clock for a condemned item is set by the caller, batched across the whole
    # run (record_first_flagged_bulk): one query for every condemned key instead of a read
    # per item. The decision per key is unchanged: see _apply_first_flag.
    judged = judge_facts(
        facts,
        gates,
        policy,
        signals=signals,
        custom_condemn=custom_condemn,
        keeps=keeps,
        window_days=window_days,
        extra_results=extra_results,
        plex_rating_key=plex_rating_key,
        matched_by=matched_by,
        match_detail=match_detail,
        match_status=match_status,
        merged_rating_keys=merged_rating_keys,
        match_candidates=match_candidates,
        watch_blind=watch_blind,
        rewatch_block=rewatch_block,
    )

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
            requested_by=display.requested_by,
            # Suggestion fields for the rule editors' datalists, from evidence already in
            # hand. Facts carries genres comma-joined (genre names never contain ", ").
            genres_json=(
                json.dumps(facts.genres.value.split(", "))
                if isinstance(facts.genres, Known)
                else None
            ),
            # This item's Plex collections, already sorted smallest-first by the caller
            # (_collection_membership). Navigation only, never a verdict input: nothing
            # above this line reads `collections`.
            collections_json=(json.dumps(collections) if collections else None),
            quality=(facts.quality.value if isinstance(facts.quality, Known) else None),
            group_key=display.group_key,
            group_title=display.group_title,
            tmdb_id=display.tmdb_id,
            imdb_id=display.imdb_id,
            tvdb_id=display.tvdb_id,
            title_slug=display.title_slug,
            video_resolution=display.video_resolution,
            content_rating=display.content_rating,
            runtime_minutes=display.runtime_minutes,
            library_title=display.library,
            ratings_json=display.ratings_json,
            show_status=display.show_status,
            verdict=judged.verdict,
            score=judged.score,
            coverage_bp=judged.coverage_bp,
            explanation_json=judged.explanation,
            # The frozen scoring inputs: the Facts plus the season-pruning guard
            # (extra_results). The hand override is not included here. It is re-applied
            # live at replay time from the override map. This is what the simulator
            # replays under an edited policy. See facts_codec.
            facts_json=json.dumps(
                facts_codec.facts_to_dict(facts, extra_results=tuple(extra_results))
            ),
            created_at=now,
        )
    )
    # Return the effective fate, with the override applied, never the pure verdict just
    # stored. The caller uses it to set the grace clock and the condemned tally over the
    # set that will actually be removed: a honored hand reap earns a fresh grace window, a
    # hand spare gives up its clock. The fate is derived, never stored.
    return effective_fate(judged, override)


def _verdict(
    evaluation: Evaluation,
    score_value: int,
    coverage_bp: int,
    policy: PolicyBody,
) -> str:
    """The scan's adapter onto the one decision function, ``engine.verdict``.

    Takes the stored integers, not the underlying floats, so that this path and the
    simulator, which has only the stored integers to work with, cannot reach different
    verdicts for the same item under the same policy. Two code paths that answer the same
    question must answer it the same way, and the cheapest way to guarantee that is to
    give them the same function and the same inputs.

    No hand override reaches here, so this passes none of ``decide_verdict``'s reap
    arguments. A ``"spare"`` arrives as an extra PROTECT result and is already counted by
    ``evaluation.protected``. A ``"reap"`` is applied after the freeze, by
    ``effective_fate`` off the stored explanation, and re-decided later by
    ``condemned.reap_override_verdict_decoded``, the one function that answers what a hand
    reap may overrule.
    """
    return decide_verdict(
        protected=evaluation.protected,
        blocked=evaluation.blocked,
        score=score_value,
        coverage_bp=coverage_bp,
        condemn_at=policy.condemn_at,
        coverage_floor_bp=policy.coverage_floor_bp,
    )


def _rewatch_odds_context(facts: Facts, block: RewatchBlock | None) -> dict[str, Any] | None:
    """The stored ``rewatch_odds`` context, from the same in-memory values the item's
    ``Facts`` got.

    ``None`` only when ``facts.rewatch_cohort_n`` is ``Absent``: hand-built ``Facts`` with
    no fit behind them at all, such as a policy-lab or test fixture. It is never ``None``
    for a row either live lane froze, since both the movie item and the show behind a
    season row always have an opinion about their own cohort, even when that opinion is
    Unknown (``services.snapshot.build_facts``, ``services.season_scan.build_season_facts``).
    Otherwise this returns a zeroed placeholder with ``state="no_history"`` when there is
    no usable block, and the block's own pooled counts and range otherwise: ``"thin"``
    below ``gates.REWATCH_BLOCK_FLOOR_N``, ``"measured"`` at or above it.
    ``engine.explanation.RewatchOddsOut`` declares this same shape, and
    ``test_engine_derivations.TestTheStoredExplanationIsWrittenAsItIsDeclared`` holds both
    together.
    """
    if isinstance(facts.rewatch_cohort_n, Absent):
        return None
    if block is None:
        return {
            "n": 0,
            "k": 0,
            "lo_days": 0.0,
            "hi_days": None,
            "state": "no_history",
            "bound_pct": 0,
        }
    return {
        "n": block.n,
        "k": block.k,
        "lo_days": block.lo_days,
        "hi_days": block.hi_days,
        "state": "measured" if block.n >= REWATCH_BLOCK_FLOOR_N else "thin",
        # The same Wilson 95% upper bound the gate itself compares (gates.wilson_upper),
        # so this display block never reads a lower "probability" than the figure that can
        # protect the item.
        "bound_pct": round(wilson_upper(block.k, block.n) * 100),
    }


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
    match_candidates: tuple[int, ...] = (),
    watch_blind: bool | None = None,
    rewatch_odds: dict[str, Any] | None = None,
) -> str:
    """The why-panel.

    One dict, two sinks: the UI and the audit log. What the owner was shown and what
    actually happened cannot drift apart.

    Three blocks, and the middle two are what make a verdict trustworthy:

    * protections that fired
    * protections checked that did not fire, with the actual numbers
    * protections that could not be checked, rendered amber, not green, because "could
      not look" is not "looked, and it was fine"

    Plus a ``match`` block that says how, or whether, the item was bound to its Plex row,
    such as "bound by TMDB id 12345" or "kept: two Plex items share this id", so a file
    spared for a matching reason is not mistaken for one nobody looked at. And a
    ``rewatch_odds`` block, display only, written for both live lanes: see
    ``_rewatch_odds_context``.

    Hand-typed on purpose, and held to the read side by a test rather than built from it.
    ``engine.explanation`` declares what this document is, and
    ``test_engine_derivations.TestTheStoredExplanationIsWrittenAsItIsDeclared`` fails when
    a key here is not declared there, or the other way round.

    Building this from that declaration was measured and rejected. It is the wire model
    too (``api.schemas.CandidateDetail.explanation``), so an alias or ``exclude_none``
    change made for the API would reach disk. Its validators are also deliberately lenient
    about an illegible stored byte, which on this side normalizes a writer's own value to
    ``None`` where no reader can recover it.
    """
    return json.dumps(
        {
            # The same whole number stored on the row and compared by decide_verdict, so
            # the panel and the decision can never show two different scores.
            "score": round(item_score.value),
            # The condemnation subtotal before any keep discount, so the panel can show
            # "condemnation 67, keep -15, final 52" the way the operator expects from Radarr.
            "base_score": round(item_score.base_value, 1),
            "keep_discount": round(item_score.keep_discount, 1),
            "threshold": policy.condemn_at,
            # The coverage line the verdict was decided against, frozen beside the threshold
            # because it is the same class of number: a policy value the score is compared
            # to, which the panel restates so an abstain forced by too little readable
            # evidence can name the line it fell under. Read here, never off the live
            # policy, which by the time anyone opens the panel need not be the one this
            # item was scored under. Additive and nullable: a row frozen before this shipped
            # thaws to None, and the panel drops the floor clause, exactly as threshold does.
            "coverage_floor_bp": policy.coverage_floor_bp,
            "coverage": round(item_score.coverage, 3),
            # Whether this item was held because plays recorded earlier stopped being
            # readable. Typed, because the panel offers the per-title escape on it, and the
            # only other way to know is to match `watch_evidence.BLIND_REASON` inside an
            # observation's reason, which is operator copy that will be reworded.
            #
            # Three-state, never a bare bool: a row scanned before this shipped has no key
            # and thaws to None, which the panel reads as "cannot tell" and shows no
            # control for. False is the positive claim that this item read honestly.
            "watch_blind": watch_blind,
            "match": {
                # status is what the UI reads: "matched" means stay quiet, anything else
                # means a plain "kept to be safe" notice, worded per status. MatchStatus
                # says what each one means, and they are not interchangeable. by and detail
                # are kept for the audit log, not shown to the owner.
                "status": match_status.value if match_status is not None else None,
                "by": matched_by.value if matched_by is not None else None,
                "detail": match_detail,
                "rating_key": plex_rating_key,
                # Every listing a merged bind covers, one file listed several times in
                # Plex. The executor's live interlocks re-read this list, so the keys they
                # protect are exactly the keys the owner was shown.
                "merged_rating_keys": (list(merged_rating_keys) if merged_rating_keys else None),
                # The rows an abstain was choosing between, so the panel can offer a link to
                # each instead of naming a problem in Plex with no way to open it. Display
                # only: no verdict reads it. There is no rating_key precisely because Reaper
                # does not know which of these rows the file actually is.
                "candidate_rating_keys": (list(match_candidates) if match_candidates else None),
            },
            # The rewatch-probability context, written for both live lanes: see
            # _rewatch_odds_context. None for an item whose own cohort could not be
            # measured, for hand-built Facts with no fit behind them at all, and for a row
            # frozen before this field existed. The panel reads all three as nothing to
            # show. Written unconditionally, like every other optional key here: the
            # top-level document always carries the keys engine.explanation.Explanation
            # declares, whatever their value
            # (test_engine_derivations.TestTheStoredExplanationIsWrittenAsItIsDeclared).
            "rewatch_odds": rewatch_odds,
            "signals": [
                {
                    # Built-in signals carry a SignalId. A custom rule carries its own name.
                    "id": r.signal.value if isinstance(r.signal, SignalId) else r.signal,
                    "contribution": round(r.pressure, 1),
                    "weight": r.weight,
                    "detail_key": to_wire(r.detail),
                    "evaluated": r.evaluated,
                    # What the zero means. Four situations all land on a contribution of 0
                    # and are otherwise identical on the wire. Only the engine branch that
                    # produced the result can tell them apart. See SignalState.
                    "state": r.state.value,
                    # The line this row was measured against, frozen with everything else
                    # the scan froze. The panel states the arithmetic from these. It must
                    # never read them off the live policy, which by the time anyone opens
                    # the panel need not be the policy this score was computed under. Null
                    # on a rule with no ramp, such as a boolean custom rule that matched or
                    # did not, and the panel omits the sentence rather than invent a line.
                    "floor": r.floor,
                    "saturate_at": r.saturate_at,
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
                    "detail_key": to_wire(k.detail),
                    "evaluated": k.evaluated,
                }
                for k in item_score.keep_results
            ],
            "protections_fired": [
                {"gate": r.gate.value, "detail_key": to_wire(r.detail)}
                for r in evaluation.protectors
            ],
            "protections_checked": [
                {"gate": r.gate.value, "detail_key": to_wire(r.detail)}
                for r in evaluation.checked_and_did_not_fire
            ],
            # ``defers_to_owner`` is written on every entry, never omitted when False, so a
            # row frozen by this version is distinguishable from one frozen before the flag
            # existed. The card's chip (``api.review._chip``) and the why panel's verdict
            # note both read that difference, the panel through
            # ``api.schemas.GateOutcomeOut``: present and True names the comparison Reaper
            # made, present and False says it could not make one, and absent names neither,
            # falling to the vague-but-true wording.
            #
            # It decides nothing about a hand reap. The write is kept for the chip alone,
            # because a legacy row genuinely cannot tell the two shapes apart and must not
            # be made to assert either.
            # ``unestablishable`` rides beside it on the same terms and for the same
            # reason: written on every entry so a row frozen by this version says which
            # shape it is, where a row frozen before it says nothing, and the panel reads
            # that as the shape those rows already had (a keep-rule conflict).
            "protections_unknown": [
                {
                    "gate": r.gate.value,
                    "detail_key": to_wire(r.detail),
                    "defers_to_owner": r.defers_to_owner,
                    "unestablishable": r.unestablishable,
                }
                for r in evaluation.could_not_be_checked
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
    """Sets the grace clock once, and never moves it while the item stays condemned, but
    restarts it when an item that had left the condemned set comes back.

    A transient Sonarr timeout that drops an item from one snapshot must not reset the
    clock, or the item could never age out and the grace period would become
    unreachable. That is why ``first_flagged_at`` is not touched on an ordinary
    re-condemn.

    The other direction matters just as much: an item condemned long ago, then rescued
    (watched, spared, or re-judged as protect), and later condemned again a full dormancy
    period afterward, must serve a fresh grace window. Its old ``first_flagged_at`` is far
    in the past, so grace_report would drop it straight into ``ready`` with no countdown
    and no Leaving Soon warning. The window holds nothing back either way, see
    ``services.grace``. What is lost is the warning. Reaper detects the return by the gap
    since the item was last seen condemned. When that gap exceeds the grace window, so it
    genuinely left rather than just missing a snapshot to an outage, the clock restarts.
    ``last_seen_condemned_at`` exists for exactly this reset.

    This is the decision, applied per key by :func:`record_first_flagged_bulk`, the only
    write path to the grace clock. A key with no row yet is returned as a new row rather
    than inserted here, so the recorder can insert it conflict-tolerantly.
    """
    if existing is None:
        return FirstFlagged(media_key=media_key, first_flagged_at=now, last_seen_condemned_at=now)

    last_seen = existing.last_seen_condemned_at
    gap = timedelta(days=grace_days)
    if last_seen is None or (now - last_seen) > gap:
        # It left the condemned set for longer than a whole grace window and has
        # returned. This is a new condemnation, so it earns a new window. Keying on the
        # gap exceeding the window, not a single missed snapshot, keeps a transient
        # outage from resetting a clock that was legitimately still running. Logged at
        # debug, since this fires occasionally per item.
        log.debug(
            "scan.grace_clock_restarted",
            media_key=media_key,
            gap_days=(now - last_seen).days if last_seen is not None else None,
        )
        existing.first_flagged_at = now
    existing.last_seen_condemned_at = now
    return None


async def _insert_first_flags(session: AsyncSession, rows: Sequence[FirstFlagged]) -> None:
    """Inserts new grace-clock rows, tolerating a competing writer.

    ``ON CONFLICT DO NOTHING`` on the key: if another writer landed the same key between
    the read and this write, its row already says "condemned around now", and keeping it
    is correct. A primary-key collision must never abort a scan. Chunked at 300 rows
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

    The only write path to the grace clock. It applies :func:`_apply_first_flag` per key.
    The existing rows arrive in chunked ``IN`` queries instead of a ``session.get`` (and
    its autoflush) per condemned item, and brand-new keys are inserted conflict-tolerantly
    (:func:`_insert_first_flags`), so two writers racing on one key cannot abort the scan.
    """
    keys = list(dict.fromkeys(media_keys))
    if not keys:
        return
    existing: dict[str, FirstFlagged] = {}
    for start in range(0, len(keys), KEY_CHUNK):
        chunk = keys[start : start + KEY_CHUNK]
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


async def build_movie_index(
    tautulli: TautulliClient,
    plex: PlexClient | None,
    *,
    degrade: Callable[[str], None],
    allowed_sections: set[int] | None = None,
) -> identity.PlexIndex:
    """The Plex movie library, inverted for id, basename, and title matching.

    One shared implementation with ``season_scan.build_tv_index``. See
    ``services.library_index`` for the spine and sweep design and its failure semantics.
    ``allowed_sections`` scopes the read to the movie libraries the operator included in
    scans (``None`` means all). A movie-only deployment with no Plex configured simply
    gets no enrichment. Its snapshot was already un-executable, since a real reap refuses
    without Plex.
    """
    return await library_index.build_index(
        tautulli, plex, section_type="movie", degrade=degrade, allowed_sections=allowed_sections
    )


def _movie_file_basename(movie: Mapping[str, Any]) -> str | None:
    """The movie's file name, for the basename match tier.

    Radarr nests the file under ``movieFile``. The relative path is just the file name,
    which is what Plex's ``locations[0]`` basename also reduces to, so the two compare
    equal across the mount-root difference. Falls back to the full path, still basenamed.
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

    ``sizeOnDisk`` is a sum over the movie's tracked ``MovieFiles`` rows, not a folder
    walk, and it is the best number Radarr offers for the reclaim estimate and the byte
    cap. The delete removes the movie folder, so bytes no row tracks are freed and never
    counted here. Measured across 200 sampled movies, the real folder held up to 44% more
    than this number in the worst case. So it is a close lower bound on what the delete
    frees, never an overstatement. That is the wrong direction for a byte cap, which is
    why ``ProfileSettings``'s caps comment states what they actually bound. Accepted
    rather than repaired: ``docs/DECISIONS.md`` under Size acquisition explains why, and
    what was declined. Distinct from :func:`_movie_file_size`, which reads
    ``movieFile.size`` for file-to-file identity comparison.

    Missing or zero is ``None``, never ``0``. See ``RawItem.size_bytes``.
    """
    size = movie.get("sizeOnDisk")
    return int(size) if isinstance(size, int | float) and size > 0 else None


def _movie_file_size(movie: Mapping[str, Any]) -> int | None:
    """The exact byte count Radarr records for the movie's file, or ``None``.

    The corroborator that tells apart several Plex listings carrying the same file name.
    An exact byte match means the same file, or a bit-identical copy of it. A mismatch
    means a different file. Deliberately ``movieFile.size`` and not ``sizeOnDisk``,
    because the comparison must be file-to-file, and ``sizeOnDisk`` sums every tracked
    row. It holds no untracked byte at all, and equaled ``movieFile.size`` for every
    movie holding a file on two live libraries. See docs/LEARNINGS.md. Zero or missing is
    unknown.
    """
    movie_file = movie.get("movieFile")
    if not isinstance(movie_file, dict):
        return None
    size = movie_file.get("size")
    return int(size) if isinstance(size, int) and size > 0 else None


def _summary(text: Any) -> str | None:
    """A trimmed overview. Kept short, since the card shows a couple of lines."""
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

    Derived rather than the raw year, because "how old" composes with "how long
    unwatched". The year-level granularity is deliberate: a finer release date is not
    worth a second fetch.
    """
    if not year:
        return Absent(source="radarr")
    try:
        # Dec 31, not Jan 1: only the year is known, so resolve the ambiguity toward
        # keeping. Jan 1 would overstate age by up to about 364 days on a field that can
        # raise the deletion score, over-matching every `release_age >= N` rule the owner
        # writes.
        age = (utcnow().date() - date(year, 12, 31)).days
    except (ValueError, OverflowError):
        return Absent(source="radarr")
    return Known(value=float(max(0, age)), source="radarr")


def _log_movie_decision(instance_id: int, movie: Mapping[str, Any], *, outcome: str) -> None:
    """One greppable DEBUG line per movie: what Radarr reported, and why the film did or
    did not become a candidate. The movie twin of ``season_scan.series_decision``.

    ``outcome`` is ``candidate`` (Radarr holds a file, so it is judged) or ``no_file`` (no
    downloaded file, nothing to reap). Grepping a title answers "why isn't my movie in the
    queue" without re-running the scan. Plex match status is logged separately
    (``scan.plex_matched`` / ``scan.plex_unmatched``). An unmatched movie still becomes a
    candidate and is never dropped here.

    The ids are the cleaned ones (``identity.ExternalIds.of``), not Radarr's raw strings,
    so the line says what Reaper matched with. A source emitting the ``tt0000000``
    sentinel logs it as no id, which is what it was treated as.
    """
    ids = identity.ExternalIds.of(imdb=movie.get("imdbId"), tmdb=movie.get("tmdbId"))
    log.debug(
        "scan.movie_decision",
        instance_id=instance_id,
        title=str(movie.get("title") or "?"),
        tmdb_id=ids.tmdb,
        imdb_id=ids.imdb,
        year=int(movie["year"]) if movie.get("year") else None,
        outcome=outcome,
        has_file=bool(movie.get("hasFile")),
        size_bytes=_reported_size(movie),
    )


def _raw_items(
    movies: list[dict[str, Any]],
    plex_index: identity.PlexIndex,
    instance_id: int,
    requested: dict[str, str] | None = None,
    root_folders: Sequence[str] | None = (),
    library_map: Mapping[str, str] = MappingProxyType({}),
) -> list[RawItem]:
    requested = requested or {}
    items: list[RawItem] = []
    without_file: list[str] = []
    # Stale-mapping guard, aggregated across this one instance's movies. A mapping that
    # never once matched a candidate library is warned about after the loop. One that
    # matched even a single movie is working and stays quiet.
    mapped_lib_hits: set[str] = set()
    stale_map_misses: dict[str, str] = {}
    for movie in movies:
        if not movie.get("hasFile"):
            without_file.append(str(movie.get("title") or "?"))
            _log_movie_decision(instance_id, movie, outcome="no_file")
            continue
        _log_movie_decision(instance_id, movie, outcome="candidate")
        # The entry point for this movie's ids (identity.ExternalIds.of). The sentinel
        # filter runs once here, and everything below reads `ids` rather than the raw
        # payload. A raw `imdbId` of "tt0000000" is truthy, so carrying it onto the
        # RawItem would shadow the id Plex matched at every
        # `item.imdb_id or item.plex_imdb_id` downstream, including the keep-list lookup
        # in `build_facts`, which would then run under an id no list row carries and
        # condemn a keep-listed film.
        ids = identity.ExternalIds.of(imdb=movie.get("imdbId"), tmdb=movie.get("tmdbId"))
        tmdb_id = ids.tmdb
        # The Plex library the operator mapped this movie's root folder to, if any. Tried ahead
        # of the folder and size corroborators, but a positive size contradiction still vetoes.
        plex_library = identity.library_for_path(_movie_file_path(movie), library_map)
        # Bind to Plex through the one shared resolver: external id (tmdb, then imdb) ->
        # file basename -> title+year, abstaining on any ambiguity or cross-tier conflict.
        # An abstain/unmatched leaves plex_rating_key None, which makes the item's facts
        # Unknown -> ABSTAIN, and the executor spares a keyless item.
        resolution = identity.resolve_movie(
            ids=ids,
            title=str(movie.get("title") or ""),
            year=int(movie["year"]) if movie.get("year") else None,
            file_basename=_movie_file_basename(movie),
            file_size=_movie_file_size(movie),
            file_path=_movie_file_path(movie),
            # This Radarr instance's own root folders, or None if they could not be read.
            # Without them the folder corroborator cannot tell a real folder from the
            # container's mount point. An empty tuple stands the folder step down. None
            # refuses the whole id narrowing (identity._narrow_among_id_hits), because a
            # failed read also removes the folder-vs-size contradiction veto.
            root_folders=root_folders,
            plex_library=plex_library,
            index=plex_index,
        )
        if plex_library is not None:
            if fold(plex_library) in identity.libraries_for_ids(
                ids, plex_index, identity.MOVIE_ID_PRIORITY
            ):
                mapped_lib_hits.add(plex_library)
            elif resolution.status in identity.ABSTAIN_STATUSES:
                # Both abstain statuses count, not AMBIGUOUS alone: CONFLICTED was split
                # out of it and carries the same evidence about the map. The operator
                # declared which library this root folder lands in, and no copy the ids
                # named is there, which means the mapping is wrong whether the rows were
                # several or merely contradictory.
                stale_map_misses.setdefault(plex_library, str(movie.get("title") or ""))
        matched = resolution.plex_item
        if resolution.rating_key is None:
            # The movie has a file in Radarr but Reaper could not confidently bind it to a
            # Plex row, so it appears only as "kept to be safe", never on the reap list.
            # Warned per item so an operator asking "why isn't this in review" finds the
            # reason in the log, not only on the row's why-panel. UNMATCHED means nothing
            # in Plex looked like it. AMBIGUOUS means more than one did, and Reaper
            # refused to guess.
            # For an AMBIGUOUS movie the library map is the operator's declaration of
            # which library each root folder lands in. A movie still leans on size after,
            # but the map is tried first. Naming what was mapped for this item (None means
            # nothing mapped) and the libraries the copies live in makes "no file size to
            # tell them apart" actionable from the log rather than a dead end.
            log.warning(
                "scan.plex_unmatched",
                media_type="movie",
                instance_id=instance_id,
                title=str(movie.get("title") or ""),
                year=int(movie["year"]) if movie.get("year") else None,
                imdb_id=ids.imdb,
                tmdb_id=tmdb_id,
                match_status=str(resolution.status),
                detail=resolution.detail,
                mapped_library=plex_library,
                candidate_libraries=identity.candidate_libraries(
                    ids, plex_index, identity.MOVIE_ID_PRIORITY
                )
                or None,
            )
        else:
            # The matched path: the common case, and the only place the tricky binds (a
            # shared id narrowed by file name, size, or folder, or several Plex listings
            # of one file merged) are decided and recorded. At debug so a large library
            # does not flood the log, but every bind is traceable when checking "why did
            # this match that Plex row".
            log.debug(
                "scan.plex_matched",
                media_type="movie",
                media_key=f"radarr:{instance_id}:{movie['id']}",
                title=str(movie.get("title") or ""),
                rating_key=resolution.rating_key,
                matched_by=str(resolution.matched_by),
                detail=resolution.detail,
                merged_rating_keys=resolution.merged_rating_keys or None,
            )
        items.append(
            RawItem(
                # Identity is the *arr's, not Plex's. Plex rating keys are not stable
                # across library rebuilds or agent migrations.
                media_key=f"radarr:{instance_id}:{movie['id']}",
                title=str(movie.get("title") or ""),
                media_type="movie",
                # `or 0` here would turn a partial payload into a 0-byte file. Every movie
                # reaching this loop cleared the `hasFile` filter above, so a missing size
                # means we could not read it, not that there is nothing to read.
                size_bytes=_reported_size(movie),
                imdb_id=ids.imdb,
                tmdb_id=tmdb_id,
                # added_at comes from the matched Plex item (Tautulli spine), which is
                # what the dormancy floor is measured against.
                plex_rating_key=resolution.rating_key,
                added_at=matched.added_at if matched is not None else None,
                year=int(movie["year"]) if movie.get("year") else None,
                summary=_summary(movie.get("overview")),
                # Three tiers, best-first (requested_by.build_map): the exact media_key where the
                # operator mapped the Seerr service, then this copy's Plex rating key (zero-config,
                # copy-true when a portal scans only its own library), then the loose tmdb union.
                requested_by=(
                    requested.get(
                        requested_by.movie_instance_key(instance_id, int(movie["id"])) or ""
                    )
                    or requested.get(requested_by.rating_key_key(resolution.rating_key) or "")
                    or requested.get(requested_by.movie_key(tmdb_id) or "")
                ),
                matched_by=resolution.matched_by,
                match_detail=resolution.detail,
                match_status=resolution.status,
                merged_rating_keys=resolution.merged_rating_keys,
                match_candidates=resolution.candidate_rating_keys,
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
        # file, so there is nothing on disk to put in the queue. Warned, not logged at
        # info, so the operator notices some monitored movies are absent for a benign
        # reason. Each such movie also emitted a scan.movie_decision line (outcome=no_file)
        # naming it at debug.
        log.warning(
            "scan.movies_without_file",
            instance_id=instance_id,
            count=len(without_file),
            detail=(
                f"{len(without_file)} movies are monitored with no file downloaded, so they are "
                "not in the review queue. There is nothing on disk to remove."
            ),
        )
    # The stale-mapping guard: warn once for a mapped library that never matched a
    # candidate library across this instance's movies (renamed library, or a wrong
    # mapping). Advisory, visible in the in-app Logs beside scan.plex_unmatched. Never
    # degrades or changes a verdict.
    for library, example in stale_map_misses.items():
        if library in mapped_lib_hits:
            continue
        log.warning(
            "scan.stale_library_map",
            media_type="movie",
            instance_id=instance_id,
            library=library,
            example_title=example,
            detail=(
                f"No movies on this Radarr were found in the Plex library {library!r} that its "
                "folder is mapped to. The library may have been renamed, or the mapping is "
                "wrong. Duplicated movies under that folder lean on size, then are kept."
            ),
        )
    return items


async def _watch_stats(
    engine: AsyncEngine, *, rating_keys: set[int], window_days: int
) -> tuple[dict[int, datetime], dict[int, int], dict[int, int]]:
    """Last-played and distinct-watcher counts, from the local history mirror."""
    if not rating_keys:
        return {}, {}, {}

    # The cache is rebuildable and may be empty on a fresh install. Ensure the table
    # exists, so a never-synced cache reads as "no plays" rather than crashing the scan
    # with "no such table". The dormancy observation alone does not make this fail
    # closed: a zero-row mirror leaves `mirror.earliest` None, so `scan` resolves the
    # horizon to `utcnow()`, and an item with an arrival date reads
    # `max(added_at, utcnow())`, meaning Known zero days dormant. The real hold comes
    # from `scan` degrading the whole snapshot un-plannably on that same empty mirror
    # ("no watch history at all: nothing can be judged"), with the zero-day reading
    # under any dormancy floor as a second layer.
    await history_sync.ensure_schema(engine)

    # Deliberately not clamped up to the data horizon, even though a window reaching
    # past it is exactly the bug this guards. Clamping here would change no count: the
    # horizon is the oldest row, so there is nothing between it and `window_start` to
    # find, and the query would return the same number while reading as though the hole
    # were closed. The hole is in what the number means, so it is closed where the
    # number is interpreted. `Facts.history_reach_days` records the reach, and
    # `gates.ServerPopularityGate` refuses to report a protection as checked over a
    # window the mirror does not span.
    window_start = int((utcnow() - timedelta(days=window_days)).timestamp())

    # One pass over the movie rows computes all three figures, instead of three separate
    # GROUP BY scans of the same table. The windowed watcher count rides along as a
    # conditional distinct-count: watched_at outside the window yields NULL, which COUNT
    # DISTINCT ignores, so it is equivalent to a `WHERE watched_at >= :since` query.
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
        # Keep only keys with a play inside the window. A 0 (has plays, none recent) is
        # dropped, since downstream reads this as `.get(key, 0)`, so absent and 0 are the
        # same fact.
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
    """Folds each merged listing group's watch stats onto its canonical rating key.

    A merged group is one file listed several times in Plex. See
    ``identity.MatchedBy.MERGED_LISTINGS``. Its plays are split across the listings'
    rating keys. Exact, not additive: distinct watchers are counted over the union of the
    group's events, so one person who played the file through two listings still counts
    once, and last-played is the latest play through any listing. Only the canonical
    keys' entries are rewritten. Every other item's stats are untouched.
    """
    all_keys = sorted({key for group in groups.values() for key in group})
    if not all_keys:
        return
    # Unclamped by the horizon for the reason spelled out in `_watch_stats`: the clamp
    # would move no count, and the reach is carried on `Facts.history_reach_days` instead.
    window_start = int((utcnow() - timedelta(days=window_days)).timestamp())
    per_key: dict[int, list[Any]] = {}
    async with engine.connect() as conn:
        # Chunked, like every sibling that expands an IN over library-sized key sets
        # (season_watch_stats, record_first_flagged_bulk). SQLite caps the number of bound
        # variables in one statement. A library with enough merged listings to pass that
        # cap would raise OperationalError, which is not an IntegrationError, so nothing
        # would catch it and the whole scan would die instead of skipping one fold.
        for start in range(0, len(all_keys), KEY_CHUNK):
            chunk = all_keys[start : start + KEY_CHUNK]
            rows = (
                await conn.execute(
                    text(
                        "SELECT rating_key, user_id, MAX(watched_at) AS last FROM watch_event "
                        "WHERE media_type = 'movie' AND rating_key IN :keys "
                        "GROUP BY rating_key, user_id"
                    ).bindparams(bindparam("keys", expanding=True)),
                    {"keys": chunk},
                )
            ).all()
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
    definitions: Sequence[list_config.ListDefinition] | None = (),
    only: int | None = None,
    radarrs: Sequence[RadarrSource] = (),
    sonarrs: Sequence[season_scan.SonarrSource] = (),
    plex_server: object | None = None,
) -> dict[str, int | str]:
    """Refreshes every protection list, before a scan reads them.

    This is the wiring that makes the list-based protections actually fire. A provider or
    membership table existing is not enough: unless something populates it at scan time,
    a "Never Reap" collection, a ``reaper-keep`` tag, or the IMDb Top 250 sits silently
    empty, and an empty protection protects nothing. A whitelist that quietly fails open
    is the worst kind of bug this tool can have.

    Every list comes from ``definitions``, the registry the operator edits on
    Settings -> Lists, keep tags included: they are a tag list like any other, not a
    special policy parameter. A definition names the library it applies to, so nothing
    here has to guess.

    A provider that finds nothing is not an error, since the owner may not have made the
    tag or collection yet. A provider that fails is recorded against its slug and does
    not abort the others, but the caller can see which lists are stale, and a scan that
    relied on a failed hard-gate list with no stored copy should treat itself as degraded
    rather than delete something the list would have protected. The atomic swap in
    ``lists.sync`` guarantees a failed refresh leaves the previous membership intact
    rather than emptying it.

    This pass also retires the lists the current configuration no longer produces
    (``lists.retire_absent``), because a stored list outlives the setting that made it.
    Tightening "match ANY" to "match ALL" changes the slug, and so does switching a list
    off or deleting it. Without the sweep, the old row sits there enabled, still
    protecting from a definition the operator has already replaced, and the change they
    saved never takes effect.

    Retiring is a durable protection-disabling write, so a family is retired only when
    the configuration it is judged against was actually readable, and only when the
    lists that replace it actually synced. Both Plex families need a live
    ``plex_server``.

    ``definitions`` is three-state, and the third state is the one that matters: ``None``
    means the registry could not be read, which is not the same fact as an operator
    having no lists. An empty tuple builds no providers and retires everything the
    registry no longer produces, which is correct when the answer is genuinely "none".
    Doing that on a failed read would disable every list on the install because a table
    was briefly unavailable. So ``None`` builds nothing and retires nothing, and the
    caller degrades the scan.

    ``only`` narrows the pass to one row of the Lists screen, for its "Check now" button.
    A narrowed pass never sweeps a whole family: a family sweep disables every stored
    list the pass did not produce, so running one over a single list's output would
    switch off every other list in that family. It sweeps that one definition instead,
    which is the truth a pass over one definition actually holds: editing a list changes
    its slug, and the superseded row would otherwise stay enabled under the same
    definition id.
    """
    synced: dict[str, int | str] = {}
    #: Slugs whose sync raised this pass. A retire sweep reads it and stands down: the list
    #: that was meant to replace the stored one did not land, so the stored one is still the
    #: live protection, and disabling it would withdraw cover on a transient failure.
    failed: set[str] = set()

    async def _run(provider: lists.ListProvider, *, kind: lists.ListKind) -> None:
        try:
            synced[provider.slug] = await lists.sync(
                engine, provider, mode=lists.ListMode.HARD, kind=kind
            )
        except Exception as exc:
            synced[provider.slug] = f"error: {exc}"
            failed.add(provider.slug)
            log.warning("lists.sync_failed", slug=provider.slug, error=str(exc))

    # Every provider reads a different service, and each one already fails soft on its
    # own, so they refresh concurrently. The whole pass takes as long as the slowest
    # provider instead of the sum. The database writes inside lists.sync stay atomic per
    # list. SQLite allows one writer at a time, and the 5s busy_timeout pragma (see
    # db/session.py) queues the brief overlapping writes. Each provider's write is a few
    # hundred rows, far inside that budget.
    runs: list[Coroutine[Any, Any, None]] = []

    # Every slug this configuration produces, per family, collected as the providers are
    # built. Each retire sweep below reads its own set as the whole truth about that family.
    imdb_slugs: set[str] = set()
    plex_slugs: set[str] = set()
    watchlist_slugs: set[str] = set()
    keep_tag_slugs: set[str] = set()

    # A deleted definition builds no provider, which is what puts its slug outside the
    # `current` set below and retires it. Without the sweep the stored membership would
    # stay enabled, and the list would go on protecting after being removed.
    #: Whether the registry was readable at all. See the docstring: unreadable retires nothing.
    registry_known = definitions is not None
    if registry_known:
        # Rows stored before their definition existed take the definition's slug first,
        # so this pass refreshes the adopted row in place. If its own sync then fails,
        # the membership the legacy row earned is already under the slug that coasts.
        await lists.adopt_legacy(engine, definitions or ())
        # And the name its keep rule matches, which the legacy row never carried. The
        # coast above is what makes this load-bearing: an adopted row whose own sync then
        # fails keeps `rule_name` NULL, so `Membership.matched_by()` falls back to the
        # display name ("Radarr (HD) tag: reaper-keep") while the rule spells the
        # definition's name. The membership is stored, the scan is executable, and it
        # protects nothing. `api/lists.py` pairs these two calls on both of its paths.
        await lists.sync_rule_names(engine, definitions or ())
    wanted = [d for d in definitions or () if d.enabled and (only is None or d.id == only)]
    for definition in wanted:
        if definition.source is lists.ListSource.IMDB:
            imdb = lists.ImdbList(
                variant=definition.imdb_variant, list_id=definition.id, list_name=definition.name
            )
            imdb_slugs.add(imdb.slug)
            runs.append(_run(imdb, kind=lists.ListKind.CURATED))
        elif definition.source is lists.ListSource.PLEX_WATCHLIST:
            # Account data, but read through the connected server's client: the stored
            # token IS the account credential (see lists.PlexWatchlist). No live server,
            # no provider, and the watchlist retire below stands down the same way.
            if plex_server is None:
                continue
            watchlist = lists.PlexWatchlist(
                server=plex_server, list_id=definition.id, list_name=definition.name
            )
            watchlist_slugs.add(watchlist.slug)
            runs.append(_run(watchlist, kind=lists.ListKind.WHITELIST))
        elif definition.source is lists.ListSource.PLEX_COLLECTION:
            # No live server, no provider, and no slug. The Plex retire below stands
            # down for the same reason, so an unreachable Plex leaves the stored keep
            # collection exactly as the last good sync left it.
            library = str(definition.config.get("library", "")).strip()
            collection = str(definition.config.get("collection", "")).strip()
            if plex_server is None or not library or not collection:
                continue
            plex_list = lists.PlexCollection(
                server=plex_server,
                section_name=library,
                collection_name=collection,
                list_id=definition.id,
                list_name=definition.name,
            )
            plex_slugs.add(plex_list.slug)
            runs.append(_run(plex_list, kind=lists.ListKind.WHITELIST))
        elif definition.source is lists.ListSource.ARR_TAG:
            # One list to the operator, one stored row per *arr instance: a tag is a thing
            # each server knows about separately, and a shared slug would have each
            # instance's sync atomically erase the other's members (see `ArrTagRule`).
            if not definition.tags:
                continue
            # Every connected *arr, both kinds: a tag list the operator defined is about the
            # tag, not about which server happens to hold the title, and a movie and a show
            # carrying it are the same instruction. The policy keep tags below are the ones
            # split by media type, because the policy itself is per media type.
            everywhere: list[RadarrSource | season_scan.SonarrSource] = [*radarrs, *sonarrs]
            for source in everywhere:
                tag_list = lists.ArrTagRule(
                    source.client,
                    definition.tags,
                    definition.match,
                    instance_id=source.instance_id,
                    instance_name=source.name,
                    list_id=definition.id,
                    list_name=definition.name,
                )
                keep_tag_slugs.add(tag_list.slug)
                runs.append(_run(tag_list, kind=lists.ListKind.WHITELIST))

    # gather_reaped, not bare gather: _run swallows every per-provider failure, so only
    # something unexpected (a cache-database fault) can raise here. When it does, the
    # surviving providers are canceled and drained rather than left refreshing lists for
    # a scan that is already dead.
    await gather_reaped(*runs)

    # Retire the lists this configuration no longer produces. A stored list outlives the
    # setting that made it: flipping the keep-tag match, clearing the tags, renaming the
    # collection, deleting an instance, and switching a list off on the Lists screen all
    # leave a row that keeps protecting from a definition the operator already replaced, so
    # the change they saved never takes effect.
    #
    # A narrowed pass sweeps no family at all: it produced one list's slugs, and a sweep
    # reading that as the whole truth about a family would disable every other list in it.
    # Its own definition it does sweep, below.
    async def _retire(family: str, current: set[str], *, when: bool) -> None:
        """Sweeps one family, if its inputs were readable and its own syncs landed.

        This is a constraint the caller cannot express on its own: a slug whose sync
        failed is in ``current``, so the sweep would leave it alone. But the row it is
        meant to replace is not, and disabling that one on the strength of a sync that
        did not land withdraws the only membership still protecting anything. The stored
        copy is the live protection until something actually replaces it.
        """
        if only is not None or not when or not registry_known:
            return
        if current & failed:
            log.info("lists.retire_skipped", family=family, failed=sorted(current & failed))
            return
        for slug in await lists.retire_absent(engine, family=family, current=current):
            synced[slug] = "retired"

    # The tag family's `current` set is built from the definitions and the *arr rows
    # alone, both local settings, so a briefly unreachable instance is still in it and
    # its list is never retired over a network blip. It also carries the legacy
    # no-definition slugs the policy keep tags wrote before the registry existed. Once
    # the definition-driven sync lands, the old spelling drops out of `current`, and the
    # sweep stands the old rows down.
    await _retire(lists.KEEP_TAG_SLUGS, keep_tag_slugs, when=True)
    # The Plex families only when Plex actually answered. With no server there is no
    # provider and no slug, and retiring on that would unprotect every title on the
    # operator's keep collection because Plex was briefly unreachable.
    await _retire(lists.PLEX_COLLECTION_SLUGS, plex_slugs, when=plex_server is not None)
    await _retire(lists.PLEX_WATCHLIST_SLUGS, watchlist_slugs, when=plex_server is not None)
    # The IMDb family needs nothing to be reachable: it is built from the registry alone,
    # so an empty set here means the operator removed the shipped list, which is exactly
    # the case that has to retire.
    await _retire(lists.IMDB_SLUGS, imdb_slugs, when=True)

    # A narrowed pass sweeps its own definition, which is a narrower claim than the four
    # above rather than an exception to the rule that stops them. Those read one family's
    # whole output as the truth about that family. This reads one definition's output as
    # the truth about that definition, which is exactly what a pass over one definition
    # knows. Editing a list changes its slug (a tag list's carries the match mode, see
    # ``ArrTagRule.slug``), so without this the superseded row stays enabled under the
    # same definition id, and the Lists screen sums both into one "Protecting N titles"
    # roughly twice the real number until a full pass runs.
    #
    # This runs here, and not in ``api.lists.edit_list``, so the sweep only runs after
    # the replacing rows actually landed. A pass that produced no slug at all, such as a
    # Plex collection checked while Plex is unreachable, sweeps nothing, since the stored
    # row is still the live protection until something replaces it.
    if only is not None and registry_known:
        produced = keep_tag_slugs | plex_slugs | watchlist_slugs | imdb_slugs
        if produced and not (produced & failed):
            for slug in await lists.retire_absent(
                engine, family=lists.list_slugs(only), current=produced
            ):
                synced[slug] = "retired"
    return synced


#: How long since the last successful watch-history sync (``history_sync.last_synced_at``)
#: before the snapshot degrades: one missed nightly ingest is a blip, two is a pattern, and
#: past that the dormancy every score leans on is being measured against a frozen mirror.
#: Set tighter than this, and a paused Tautulli blocks every scan. Looser, and items
#: drift toward condemnation for as long as the outage lasts. Same bound as
#: WHITELIST_STALE_AFTER and the same two-nightly-cycles logic behind it, but a different
#: quantity: that one bounds a *failed* sync coasting on stored keep-list membership,
#: this one bounds a sync that stopped running at all.
MIRROR_STALE_AFTER = timedelta(hours=48)


#: How far short of the source's own count the mirror may sit before the snapshot
#: degrades, as a fraction of that count. Empty is caught by the horizon test above, and
#: stale is caught by the clock above. This is the third state, and the only one of the
#: three that looks healthy from every angle: populated, freshly synced, and missing a
#: third of the evidence.
#:
#: Measured on a 425,604-row history: an incremental sync's own paging total only covers
#: its increment, so it completed correctly while the mirror actually sat 35% short of
#: the source. Nothing in that walk was wrong, and nothing in it could notice.
#:
#: The legitimate gap is far smaller and has a known cause: a play still in progress is
#: counted by the source and deliberately skipped by the ingest
#: (`history.rows_skipped`), so the mirror can never equal the total, and requiring
#: equality here would degrade every scan forever. The fraction leaves wide headroom
#: above that legitimate gap while still catching a defect the size measured above.
#:
#: The floor makes this safe on a small history, where a handful of concurrent plays
#: would otherwise trip the fraction alone. A scan degrades only when the mirror is
#: short by both.
MIRROR_SHORTFALL_FRACTION = 0.01
MIRROR_SHORTFALL_FLOOR = 500


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

    ``sync_protection_lists`` records a failed provider as ``"error: ..."`` and leaves the
    caller to decide what to do about it. A list that feeds a hard gate (it can protect a
    title outright) fails open when its stored copy is empty: the gate reads no members,
    protects nothing, and an executable snapshot would reap the very titles the list
    exists to save. So any failed hard-mode list with no stored members degrades the
    snapshot, whether it is a keep-list (whitelist) or a curated protected list such as
    the IMDb Top 250. A soft list only feeds a scoring nudge and never unprotects a kept
    title, so a failure of one does not degrade.

    Beyond the empty case, recency is a keep-list concern. The atomic swap in
    ``lists.sync`` keeps the prior membership on a failed refresh, so a populated list
    still protects. But a whitelist the owner actively adds to must reflect a title
    tagged since the last good sync, so a stale or never-confirmed whitelist degrades
    too. Each case resolves toward keeping files:

    * No membership to fall back on (any hard list): a first scan, or a newly-added
      keep-list or curated list that has never synced once.
    * Stored membership older than ``WHITELIST_STALE_AFTER`` (whitelist only). Every hour
      of staleness is an hour a newly keep-tagged title is unprotected, so past the bound
      the snapshot degrades until a sync succeeds. A curated external list churns slowly
      and keeps protecting from its stored copy. Its staleness bound is a separate
      policy, not here.
    * No record of a successful sync at all (whitelist only, members present but no
      ``last_synced_at``): recency cannot be confirmed, so it is not assumed.

    A fourth case is not a failed sync at all: a keep list this pass did not check, so it
    never reaches ``synced`` and none of the three above can see it. Unlinking Plex is the
    way in. ``DELETE /api/settings/plex`` drops only the server row, so the collection and
    watchlist definitions stay enabled, but with no live server no provider is built for
    either. The stored membership goes on protecting and goes on aging with nothing
    bounding it, so a keep list unreadable for months reads exactly like one checked
    minutes ago. That is the same unprotected window the recency bound above exists for,
    reached without an error, so it is bounded the same way and by the same constant,
    over the stored rows rather than over this pass's output.

    Keyed on a stored row, never on a definition, which is what keeps a Plex-less install
    off it: a row is written only by a sync that actually ran (``lists.sync``, and
    ``_record_sync_error`` on the failing path), so the seeded Plex collection on an
    install that has never linked Plex has no row and cannot degrade anything. Disabled
    rows are skipped for the reason they always are: ``retire_absent`` disables a
    superseded slug and keeps its members, so it reads as populated while protecting
    nothing.
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
                    text(
                        "SELECT mode, kind, last_synced_at, display_name "
                        "FROM protection_list WHERE slug = :slug"
                    ),
                    {"slug": slug},
                )
            ).one_or_none()
            mode = row[0] if row is not None else None
            kind = row[1] if row is not None else None
            last_synced_at = row[2] if row is not None else None
            # What the operator calls this list, for the sentences below. They reach the
            # scan banner and the reap page verbatim, and a slug is internal vocabulary
            # kept out of operator copy. It carries a match mode, an instance id, and now
            # a definition id, none of which name anything the reader can go and fix. The
            # slug stays as the fallback for a list that failed before it was ever
            # stored, which is the one case where no display name exists yet.
            named = str(row[3]) if row is not None and row[3] else slug
            # Only a HARD-mode list feeds a PROTECT gate, so only it can fail *open* when its
            # stored copy is empty. A SOFT list merely feeds a scoring nudge, and losing that
            # never unprotects a kept title. A missing row (never synced even once) is treated
            # as hard-shaped: fail closed rather than guess.
            if mode is not None and str(mode) != lists.ListMode.HARD.value:
                continue
            # Count only members of an enabled list, because only an enabled list
            # protects anything. ``lists.retire_absent`` disables a superseded slug with
            # ``enabled = 0`` and deliberately keeps its members, so a disabled row still
            # reads as populated. A slug carries the operator's match mode, so flipping
            # keep-tags from "any" to "all" and back retires and revives slugs in normal
            # use. Counting rows alone would let a disabled list vouch for a failed sync
            # and skip the degrade, which is exactly the state where the gate protects
            # nothing.
            members = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM protection_list_item i "
                        "JOIN protection_list l ON l.slug = i.slug "
                        "WHERE i.slug = :slug AND l.enabled = 1"
                    ),
                    {"slug": slug},
                )
            ).scalar_one()
            if int(members or 0) == 0:
                # No stored members: the hard gate protects nothing, so the list fails OPEN,
                # whether it is a keep-list (whitelist) or a curated protected list (e.g. the
                # IMDb Top 250 whose first sync never landed). Either way a scan would reap
                # the titles the list exists to protect, so degrade regardless of kind.
                reasons.append(
                    f"the protection list '{named}' failed to check and nothing is stored "
                    "for it, so it is protecting nothing: a scan must not delete titles that "
                    "list would have kept"
                )
                continue
            # Members exist, so the stored copy still protects. Recency, though, is a keep-list
            # concern: a title keep-tagged since the last good sync is unprotected until the
            # whitelist refreshes. A curated external list churns slowly and keeps protecting
            # from its stored copy, so the recency checks below apply to the whitelist only.
            if kind is not None and str(kind) != lists.ListKind.WHITELIST.value:
                continue
            # last_synced_at is written only on success (lists.sync), so it is the last
            # successful sync. from_epoch returns None for a null or zero stamp.
            last_success = from_epoch(last_synced_at)
            if last_success is None:
                reasons.append(
                    f"the protection list '{named}' failed to check, and Reaper has no "
                    "record of it ever checking successfully. Titles on it may not be "
                    "protected, so nothing may be deleted from this scan"
                )
            elif now - last_success > WHITELIST_STALE_AFTER:
                hours = int(WHITELIST_STALE_AFTER.total_seconds() // 3600)
                reasons.append(
                    f"the protection list '{named}' failed to check and the stored copy is "
                    f"more than {hours} hours old. Anything added to it since then is not "
                    "protected, so nothing may be deleted from this scan"
                )
        reasons += await _unchecked_keep_list_degradations(conn, synced, now=now)
    return reasons


async def _unchecked_keep_list_degradations(
    conn: AsyncConnection, synced: Mapping[str, int | str], *, now: datetime
) -> list[str]:
    """Keep lists this pass never checked at all, past the same recency bound.

    The loop above walks ``synced``, so it can only see a list that was checked and failed.
    A list nothing built a provider for is absent from it entirely and coasts on its stored
    membership forever. See :func:`protection_sync_degradations` for why the row, not the
    definition, is the key.

    Whitelist only, matching the recency rule above: a curated external list churns slowly
    and keeps protecting from its stored copy, and bounding that is a separate policy.
    """
    rows = (
        await conn.execute(
            text(
                "SELECT slug, mode, kind, last_synced_at, display_name "
                "FROM protection_list WHERE enabled = 1"
            )
        )
    ).all()
    hours = int(WHITELIST_STALE_AFTER.total_seconds() // 3600)
    reasons: list[str] = []
    for row in rows:
        if str(row.slug) in synced:
            continue  # checked this pass, the walk above already had its say
        if str(row.mode) != lists.ListMode.HARD.value:
            continue
        if str(row.kind) != lists.ListKind.WHITELIST.value:
            continue
        named = str(row.display_name) if row.display_name else str(row.slug)
        last_success = from_epoch(row.last_synced_at)
        if last_success is None:
            reasons.append(
                f"the protection list '{named}' was not checked, and Reaper has no record "
                "of it ever checking successfully. Titles on it may not be protected, so "
                "nothing may be deleted from this scan"
            )
        elif now - last_success > WHITELIST_STALE_AFTER:
            reasons.append(
                f"the protection list '{named}' was not checked and the stored copy is more "
                f"than {hours} hours old. Anything added to it since then is not protected, "
                "so nothing may be deleted from this scan"
            )
    return reasons
