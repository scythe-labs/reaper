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
)
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
from reaper.engine.verdict import decide_verdict
from reaper.ratings import Rating, RatingSource, from_radarr, merge_by_source
from reaper.services import (
    history_sync,
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

    reach_days: int = field(init=False)
    """How far back the watch mirror reaches, in days -- sampled ONCE, here.

    A property of the mirror and not of an item, so it belongs beside ``horizon`` rather
    than in the per-item builder. Derived at construction because the horizon is: reading
    the clock again per item let one scan freeze two different reaches, and an item built
    after the day count ticked up to the popularity window would be answered where an
    identical item built a moment earlier was held (``gates.ServerPopularityGate``). Both
    lanes take it from here, so the movie and season builders cannot disagree either.
    """

    activity_degraded: bool = False
    """True when we could not read what is playing right now.

    ``active_rating_keys`` being empty is ambiguous on its own: it is what a healthy
    server with nobody watching looks like, and also what an unreachable or malformed
    Tautulli looks like. Anything that vetoes a deletion on "nothing is streaming" must
    read this flag, never the emptiness of the set. Set it wherever the activity read
    fails; ``_gather`` is the only writer today.

    This is a typed field and not a substring of ``degraded_reasons`` on purpose: the
    veto used to be coupled to the wording of a free-text reason, so rewording the
    message would have silently turned "we could not check" into "nothing is playing".
    """

    imdb_degraded: bool = False
    """True when the IMDb ratings data could not be read at all.

    Same shape of trap as ``activity_degraded``: the lookup map comes back empty, which
    is indistinguishable from "every one of these titles is genuinely unrated" unless
    something says so. ``build_facts`` reads this to emit Unknown rather than Absent for
    the rating and vote count, because Absent withdraws every rating-based keep -- the
    protection is REMOVED by the missing evidence, which is the inverted direction this
    codebase fails closed against.
    """

    def __post_init__(self) -> None:
        self.reach_days = history_reach_days(self.horizon, now=utcnow())

    @property
    def degraded(self) -> bool:
        return bool(self.degraded_reasons)

    def degrade(self, reason: str) -> None:
        log.warning("snapshot.degraded", reason=reason)
        # Made a SENTENCE here, the one point every reason passes through -- the ~18 call
        # sites, `build_index`'s callback, and the caller's `extra_degrade_reasons` alike.
        #
        # Terminated, because the reasons are joined into one operator-facing string and every
        # notice rendering it writes more prose directly after, so an unterminated fragment
        # fuses into the sentence that follows: the incomplete-scan notice read "... could not
        # be matched to your libraries You can still look at it" (#514).
        #
        # And capitalized, because terminating them turned a "; "-joined LIST into a run of
        # sentences, and all but the seven that carry a lane prefix start lower case: the
        # notice then read "... what is playing right now. radarr 'Main' unreachable." -- a
        # lower-case sentence after a full stop, with the product's name mis-spelled against
        # every other surface. Only the first character is touched, so an id or a quoted name
        # that starts a reason keeps its own spelling.
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

    ``None`` is not zero. A partial payload (``hasFile`` true, ``sizeOnDisk`` missing)
    carried as ``0`` becomes an affirmative measurement: it reads as maximum pressure on
    a size signal, and it silently withdraws any "keep large files" rule. See
    ``tests/test_fact_layer_states.py``.
    """

    imdb_id: str | None
    tmdb_id: int | None
    plex_rating_key: int | None
    added_at: datetime | None
    # Display fields, carried onto the candidate so the review queue can show a blurb
    # without a second data source. None of them influence the verdict. No poster: it is
    # derived from the Plex rating key at read time (`api/review._candidate_out`), which is
    # why the stored column carried a NULL for its whole life and retired in release M.
    year: int | None = None
    summary: str | None = None
    requested_by: str | None = None
    # How this item was bound to its Plex row (and why, if it was not) -- for the why-panel.
    matched_by: identity.MatchedBy | None = None
    match_detail: str | None = None
    match_status: identity.MatchStatus | None = None
    # Every Plex listing the bind covers when the file is listed more than once (includes
    # plex_rating_key). Watch reads must consult all of them; empty for a normal bind.
    merged_rating_keys: tuple[int, ...] = ()
    # The Plex rows an abstain was choosing between; empty on any bind. Display only -- it
    # reaches the why-panel so the operator can open the rows Reaper could not choose
    # between, and nothing in the verdict reads it.
    match_candidates: tuple[int, ...] = ()
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


#: Why this item has no Plex rating key, one entry per non-matched resolver outcome. Each
#: value is a KEY into ``WhyPanel``'s ``CAUSE_COPY``, which turns it into the sentence the
#: owner reads; a key with no entry there falls back to printing this string raw, so
#: ``test_review_chips.py::TestTheMatchStatusVocabulary`` fails on one. ``None`` (a record
#: from before the field shipped) takes the unmatched wording, which it has always read as.
_NO_KEY_REASONS: dict[identity.MatchStatus | None, str] = {
    identity.MatchStatus.UNMATCHED: "Plex has not matched this item",
    identity.MatchStatus.AMBIGUOUS: "more than one Plex item matches this title",
    identity.MatchStatus.CONFLICTED: "Plex and Radarr describe this file differently",
}

#: Why dormancy could not be measured: matched to Plex, but no arrival date AND no play, so
#: there is no instant to measure from. A KEY into ``CAUSE_COPY`` exactly as the reasons
#: above are, and named here rather than typed at each site so the same drift test covers it
#: (rule 144) -- it was written twice by hand, on both sides of the tree, and nothing failed.
NO_ADDED_AT_REASON = "no added-at date"

#: Why a movie's size is unreadable: Radarr reported no size for the file. A KEY into
#: ``CAUSE_COPY`` like the rest -- it reaches the panel through a keep rule on "Size on
#: disk", the same route the request reasons take (rule 144). The season lane says this in
#: its own words, so the two are named apart rather than shared.
NO_SIZE_REASON = "the file's size was not reported"


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

    Note how often ``Unknown`` appears. Every one of them is a place where a naive
    implementation would have written ``0``, ``[]`` or ``False`` -- and every one of
    those would have quietly condemned an item we know nothing about.

    ``watch_blind_reason`` is one more of them, and it is the only one the item's own
    evidence cannot reveal: set when ``services.watch_evidence`` finds this item measured
    fewer plays than it has measured before, which a library cannot do. The mirror is read
    by the rating key the item carries now, and a re-added file carries a new one while its
    earlier plays stay filed under the old, so "no rows" is ambiguous between churn and a
    genuinely unwatched item. When it is set, dormancy and both watcher counts are Unknown
    rather than a measured zero.

    ``rewatch_curve`` is ``scan``'s Stage 2 fit (#554), refit once per scan and shared by
    every item; ``None`` for a caller that has not fit one (a test fixture, or before the
    scan's own fit runs), which reads the same as "no usable block" below.
    """
    rating_key = item.plex_rating_key
    # The three no-key states are DIFFERENT stories and the why-panel must not conflate
    # them: "unmatched" means Plex has no such item as far as Reaper can tell; "ambiguous"
    # means Plex has MORE than one (a 1080p and a 4K copy sharing one TMDB id) and Reaper
    # refused to guess whose watch history to read; "conflicted" means each kind of evidence
    # found ONE row and they were different rows, which is the two apps describing one file
    # differently and is not a statement that Plex holds several copies. All three keep the
    # file; only the words shown to the owner differ, and the wrong words send them to fix
    # the wrong thing. Each string is a key into WhyPanel's CAUSE_COPY (rule 144);
    # test_review_chips.py::TestTheMatchStatusVocabulary fails when one has no entry there.
    no_key_reason = _NO_KEY_REASONS.get(item.match_status, "Plex has not matched this item")

    # --- dormancy -----------------------------------------------------------
    # THE derived field. "Days since last play" is null for exactly the items we care
    # about most, and coercing that null to epoch 0 reads as ~20,600 days unwatched --
    # the maximum condemnation pressure, for the item we know least about.
    dormancy: Observation[float]
    if rating_key is None:
        dormancy = Unknown(reason=no_key_reason, source="plex")
    elif watch_blind_reason is not None:
        # Checked BEFORE the measurement below, and it has to stay there: a re-added file
        # carries a fresh added_at while its earlier plays stay filed under the key it no
        # longer holds, so the measurement would read a confident, tiny dormancy off the one
        # input that still looks readable when the plays behind it are not.
        dormancy = Unknown(reason=watch_blind_reason, source="tautulli")
    else:
        # Through the one shared derivation (engine/dormancy.py), so the season scan
        # measures this the same way (rule 3) -- and
        # since #272 the *thaw* is shared too, so a missing arrival date resolves identically
        # on both lanes. A play alone is enough: `reference_instant` measures from it, and
        # only an item with neither a play nor an arrival date comes back with nothing to
        # measure from. This lane used to take Unknown the moment `added_at` was missing,
        # whatever history it held, while `season_scan.build_season_facts` measured from the
        # play -- one value, two thaw rules, and a why-panel here saying dormancy could not be
        # measured with a play for that item in scope.
        reference = reference_instant(
            last_played=last_played.get(rating_key),
            added_at=item.added_at,
            horizon=context.horizon,
        )
        if reference is None:
            # Matched to Plex, but Plex reports no arrival date and no play is in scope, so
            # there is genuinely nothing to measure from and the item abstains: it appears
            # only as "kept to be safe", never on the reap list. Warn so "why isn't this
            # reapable" is answerable from the log, the same as an unmatched item. Rare: a
            # matched Plex item almost always carries an added_at. The reason is a key into
            # WhyPanel's copy map, named in `NO_ADDED_AT_REASON` so
            # `test_review_chips.py::TestTheMatchStatusVocabulary` fails if the two sides
            # drift (rule 144) -- it is also the same state the season lane's both-missing
            # arm reports, so the two lanes say one thing.
            log.warning(
                "scan.no_added_at",
                media_type="movie",
                media_key=item.media_key,
                title=item.title,
                imdb_id=item.imdb_id or None,
                tmdb_id=item.tmdb_id,
                plex_rating_key=rating_key,
            )
            dormancy = Unknown(reason=NO_ADDED_AT_REASON, source="tautulli")
        else:
            dormancy = Known(value=dormancy_days(reference, now=utcnow()), source="tautulli")

    # --- popularity ---------------------------------------------------------
    recent: Observation[int]
    all_time: Observation[int]
    if rating_key is None:
        recent = Unknown(reason=no_key_reason, source="plex")
        all_time = Unknown(reason=no_key_reason, source="plex")
    elif watch_blind_reason is not None:
        recent = Unknown(reason=watch_blind_reason, source="tautulli")
        all_time = Unknown(reason=watch_blind_reason, source="tautulli")
    else:
        recent = Known(value=watchers_window.get(rating_key, 0), source="tautulli")
        all_time = Known(value=watchers_all_time.get(rating_key, 0), source="tautulli")

    # --- rewatch (#554 stage 1) ----------------------------------------------
    # `rewatch` is already folded over any merged Plex listings: `scan` gathers it via
    # `services.rewatch.movie_rewatch_stats` with the same `groups` mapping
    # `_fold_merged_watch_stats` uses, so a lookup by the canonical `rating_key` alone is
    # correct here, exactly as it is for `watchers_window`/`watchers_all_time` above.
    viewings_obs: Observation[int]
    last_play_days_obs: Observation[float]
    if rating_key is None:
        viewings_obs = Unknown(reason=no_key_reason, source="plex")
        last_play_days_obs = Unknown(reason=no_key_reason, source="plex")
    elif watch_blind_reason is not None:
        viewings_obs = Unknown(reason=watch_blind_reason, source="tautulli")
        last_play_days_obs = Unknown(reason=watch_blind_reason, source="tautulli")
    else:
        # The mirror was read either way, so viewings is Known even at 0. Recency is
        # Absent (not Unknown) when this movie has no qualified play at all: we looked,
        # and there is genuinely nothing to measure the last one from (rule 93).
        rewatch_stats = rewatch.get(rating_key)
        viewings_obs = Known(
            value=rewatch_stats.viewings if rewatch_stats is not None else 0, source="tautulli"
        )
        last_play_days_obs = (
            Known(value=dormancy_days(rewatch_stats.last_play, now=utcnow()), source="tautulli")
            if rewatch_stats is not None and rewatch_stats.last_play is not None
            else Absent(source="tautulli")
        )

    # --- rewatch cohort (#554 stage 2) ---------------------------------------
    # Known only when the current dormancy is Known AND the fit found a non-withheld block
    # for it; Unknown for every other reason at once (no fit, dormancy Unknown, past the
    # fitted range, a dropped bucket, withheld by reach) -- one reason constant, since the
    # operator's takeaway is the same either way (docs/history/REWATCH_PLAN.md, Stage 2).
    #
    # `cohort_block` is the one place the lookup and the withhold combine (rule 104):
    # `scan`'s per-item judge call re-derives the identical block off this same dormancy
    # value (read back from the Facts this call returns) and the same curve, so the stored
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
    # Radarr's imdbId first, then the Plex-matched imdb id as a fallback (Radarr may lack
    # it, or carry one the IMDb dataset doesn't have). The shared helper is also what the
    # display ratings row reads, so the signal and the row can never show different numbers.
    entry, looked_up = dataset_lookup(imdb, item.imdb_id, item.plex_imdb_id)
    if entry is not None:
        rating = Known(value=int(entry.average_rating * 10), source="imdb")
        votes = Known(value=int(entry.num_votes), source="imdb")
    elif context.imdb_degraded:
        # We never got to ask about THIS title either: the dataset as a whole was
        # unreadable, so the empty map below is not an answer about any film in it.
        # ``looked_up`` is still True here (the item does carry an imdb id), and taking
        # the Absent branch on that would tell the keep lane "checked, and it is unrated"
        # for the entire library at once -- every rating floor reporting
        # checked-and-did-not-fire, every graded rating keep withdrawn, on evidence
        # Reaper has already declared untrustworthy.
        unreadable = Unknown(reason=IMDB_UNREADABLE_REASON, source="imdb")
        rating = unreadable
        votes = unreadable
    elif looked_up:
        # Absent, not Unknown: we looked and this title genuinely has no IMDb rating.
        # (A *degraded* dataset is different, and is caught by the branch above -- it
        # degrades the snapshot AND reads Unknown, because degrading alone would still
        # have left every film here silently unprotected.)
        rating = Absent(source="imdb")
        votes = Absent(source="imdb")
    else:
        # We never got to ask: no imdbId from Radarr and no Plex match to borrow one from.
        # Absent here would tell the keep lane "this title has no IMDb rating", withdrawing
        # every rating-based keep while coverage still read 100%. See dataset_lookup.
        no_id = Unknown(reason=NO_IMDB_ID_REASON, source="imdb")
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
    # Every id the movie carries is passed together, the TV path's rule (rule 29): Radarr is
    # tmdb-native and a blank imdbId is ordinary, so the imdb id may be the one Plex matched.
    # A keep-list row stored under imdb alone -- what a legacy-agent Plex library yields for
    # a "Never Reap" collection -- must still protect it. Matching on one id kind alone fails
    # open on the deletion path.
    #
    # Every listing of a merged bind, or the single key of a normal one -- derived once
    # here and read again by the streaming veto below (rule 104). A "Never Reap" entry no
    # agent ever matched is stored under its Plex key alone, so without this the operator
    # can put a title on the list and watch Reaper condemn it.
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
    # Every list, by the name its keep rule spells -- the `on_list` field's input, derived
    # in `lists.on_list_fact` for both fact builders at once.
    on_lists = lists.on_list_fact(memberships)

    # --- streaming right now ------------------------------------------------
    streaming: Observation[bool]
    if context.activity_degraded:
        # We could not check. Never assume False -- that is how a tool deletes a file
        # somebody is watching.
        streaming = Unknown(reason=watch_evidence.NO_SESSIONS_REASON, source="tautulli")
    else:
        # A merged bind covers several listings of one file; someone streaming ANY of
        # them is streaming this very file, so the veto checks every key in the group.
        watch_keys = plex_keys
        if not watch_keys:
            # No key to match a session against, so this was never checked. It used to land
            # as a definite Known(False) -- "nobody is watching this" -- on the very items
            # Reaper knows least about, and AMBIGUOUS is the case that stings: Plex does hold
            # the title, in two copies, someone can be streaming it right now, and the fact
            # asserted otherwise. Every sibling fact above takes Unknown on the same
            # condition, and the season builder's twin already does (rules 93 and 72).
            streaming = Unknown(reason=no_key_reason, source="plex")
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
            else Unknown(reason=NO_ADDED_AT_REASON, source="plex")
        ),
        size_bytes=(
            Known(value=item.size_bytes, source="radarr")
            if item.size_bytes is not None
            else Unknown(reason=NO_SIZE_REASON, source="radarr")
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
        # --- fields authorable in custom rules ---------------------------------
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
        # Whether this title left the library and came back (#553). Read off the ledger row
        # the scan looked up before judging, never re-derived here: the detection is visible
        # for one scan and the hold runs for months. One helper for both lanes (rule 35).
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


#: Bounds concurrent `collection_children` reads within one snapshot's collection pass --
#: now only the collections the item tags could not account for, which on a live server was
#: 1 of 397. Kept bounded anyway: what makes a collection need this read is that Plex holds
#: its membership somewhere other than the members' tags, and nothing caps how many of those
#: a library has. Separate from leaving_soon.SHELF_CONCURRENCY, which bounds a different
#: fan-out (whole libraries, not collections within one).
_COLLECTION_CHILDREN_CONCURRENCY = 8


async def _collection_membership(
    plex: PlexClient | None, *, allowed_sections: set[int] | None
) -> tuple[dict[int, list[str]], dict[str, int]]:
    """Every Plex item's collection names, and every collection's Plex-reported size.

    Collections are navigation, never protection (the fence in docs/history/COLLECTIONS_PLAN.md):
    a read failure here -- no Plex configured, a section listing that raises, one bad
    collection -- returns whatever was gathered so far and never more, rather than raising
    into the scan or degrading it. Rule 28 binds *evidence* sources, and a collection is not
    evidence, so the compensating cost of a failed read is a missing chip, nothing else.

    The collection NAME is the identity. Reaper's own Leaving Soon shelf creates a
    same-named collection in every section, so an operator with two libraries hits that
    case by default, and one "Leaving Soon" chip covering both is what they mean.
    Same-named collections in different sections merge into one membership entry, and
    their known sizes are summed rather than the later section overwriting the earlier
    one's count (rule 6).

    A collection whose child count Plex never reported is a different fact from one Plex
    reported as empty, so it is left out of the returned size map rather than folded to
    0 -- the sort below would otherwise read it as the smallest collection and hand it the
    chip's element-0 slot ahead of a genuinely small one. Each item's list is sorted
    smallest known collection first (Plex's own child count), a collection with no known
    size sorting last, ties broken alphabetically -- the plan's tie-break, so the chip
    renders the same collection scan to scan instead of flipping with dict-iteration
    order.

    **Membership comes from the items, not from the collections.** One read per ~400 items
    (``plex.collection_tags``) carries every member's collection names, where asking each
    collection for its children costs one read per collection. Measured across a live
    server's libraries: the whole pass ran in 37 seconds and about 50 requests, where it had
    been 397 requests and 667 seconds of Plex time -- 93% of everything that scan asked Plex
    for -- and it stopped saturating the server the GUID sweep reads beside it, where a
    126 ms read had been taking 7 seconds.

    A collection Plex reports more members for than the tags showed is read the old way,
    per collection. That is what covers the two kinds of collection whose membership is not
    a tag: a smart collection is a saved filter, and a collection of seasons or episodes
    holds objects the section-level listing never lists. Comparing against ``child_count``
    finds both without asking Plex which kind it is -- the ``smart`` flag is absent from the
    listing on a server with no smart collection, so a pass keyed on it could not be shown
    to work. On that server this fell back for 1 collection of 397, and the membership it
    returned held every one of the 1,029 memberships Plex declared.

    A tag naming no collection in the section's own listing is dropped: Plex leaves a
    ``collection`` tag behind on items whose collection is gone (3 such names on the live
    library), and a chip for a shelf the operator can no longer open is worse than no chip.
    Tags are matched to the listing casefolded and the LISTING's spelling is what is stored
    (rule 88), so the name a chip shows is the name the size map is keyed by.

    The fallback reads run concurrently, bounded by ``_COLLECTION_CHILDREN_CONCURRENCY``;
    one collection's failure is caught inside its own task and logged rather than raised
    into the fan-out, so it can never cancel a sibling read or degrade the snapshot.
    """
    if plex is None:
        return {}, {}
    try:
        sections = await plex.video_sections()
    except PlexError as exc:
        log.warning("snapshot.collections_unreadable", error=str(exc))
        return {}, {}

    membership: dict[int, list[str]] = {}
    sizes: dict[str, int] = {}
    bound = asyncio.Semaphore(_COLLECTION_CHILDREN_CONCURRENCY)

    def _add(key: int, name: str) -> None:
        """One item's chip list, without repeating a name a fallback read also returned."""
        names = membership.setdefault(key, [])
        if name not in names:
            names.append(name)

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
            # collection in it falls to the per-collection read below (rule 28 does not
            # bind here: a collection is not evidence).
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
                _add(key, stored)

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
                _add(key, title)

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
        # ingest and for a quiet library, so gating on the newest play tells a server whose
        # users went away for a weekend that its watch history is broken, and blocks every
        # deletion until somebody watches something.
        synced = await history_sync.last_synced_at(engine)
        if synced is None or utcnow() - synced > MIRROR_STALE_AFTER:
            context.degrade(
                "watch history has not updated recently, so nothing can be judged on how "
                "long it has gone unwatched"
            )
        # The third state, and the one both tests above call healthy: populated, synced an
        # hour ago, and holding a fraction of what the source has. It is reachable by the
        # ordinary route rather than a broken one -- a restore leaves the mirror behind
        # (`services/backup.py` excludes it deliberately), and every sync from then until the
        # first full sweep is INCREMENTAL, so each one completes correctly against a paging
        # total that is the size of its own increment and never notices the hole underneath.
        # `synced_at` is stamped by `_check_regression` BEFORE the walk, so the clock above
        # reads fresh throughout.
        #
        # Measured: a scan in that window moved 245 titles off the condemned list, 2.17 TB,
        # on a mirror at 65% -- with an identical policy, scorer and evidence hash, and
        # `degraded = 0`. Coverage collapsing is the engine working (an unsigned score can
        # only fall as evidence goes missing); presenting the result as executable is not.
        #
        # It is not only the keep direction, which is why this degrades rather than warns.
        # A short mirror caps dormancy, lowering pressure, AND reports fewer distinct
        # watchers, raising it. `history_sync` already calls a truncated mirror the largest
        # mass-deletion vector here (rule 56).
        elif (shortfall := mirror.shortfall) is not None and (
            shortfall > MIRROR_SHORTFALL_FLOOR
            and shortfall > (mirror.source_total or 0) * MIRROR_SHORTFALL_FRACTION
        ):
            # `shortfall is None` means no sync ever recorded a source total: "we were not
            # told", which is rule 93's Unknown and not a clean bill of health. It is left to
            # the staleness guard above, which a mirror nobody has ever synced already fails.
            context.degrade(
                "watch history is still catching up, so nothing can be judged on how long "
                "it has gone unwatched. Let it finish, then scan again"
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
        sessions = activity.get("sessions")
        if not isinstance(sessions, list) or not all(isinstance(s, dict) for s in sessions):
            # A 200 carrying a null or wrong-shaped body is NOT "nobody is watching". Coercing
            # it to an empty list reads as a measured "nothing is streaming" and defeats the
            # veto on every item. Same treatment as an unreachable server: we could not check.
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
                        # One stream we cannot identify. It may well be an item this scan
                        # is about to judge, so this is "we could not check", not "not it".
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
    # Read before the TV task is spawned, because that task holds no session of its own and
    # its season keys do not exist yet. Serves both lanes: one read per scan, not per item.
    watch_marks = await watch_evidence.recall_all(session)
    # Read here for the same reason, and handed to the season task the same way (#553): the
    # ledger is keyed on external ids that lane resolves inside `gather`, and the scan
    # timings answer "did Reaper run while this was missing" for both lanes at once.
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
                # The scan and the simulator now reach the planner's nine season settings
                # through one road, ``SeasonPolicy.from_body``. Unpacking the body into nine
                # keywords here was the second road, and a field added to the season card
                # had to be written onto both (rule 144).
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
    # A read-only extension of the same gather (#816 phase 2). Never raises -- see the
    # function's own docstring -- so it needs no place in the except below: there is
    # nothing for it to leave half-done that a reap would need to clean up.
    collections_task = _spawn(
        _collection_membership(plex, allowed_sections=allowed_sections), name="collections"
    )
    movie_tasks = [_spawn(_movies_from(source), name="radarr") for source in radarrs]
    roots_tasks = [_spawn(_roots_from(source)) for source in radarrs]

    items: list[RawItem] = []
    season_judgments: list[season_scan.SeasonJudgment] = []
    try:
        # Awaited in the sequential code's order, so the first failure to surface is the
        # same one it would have raised then; the except below reaps every other task.
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
            # The first thing to check for "why did the rating floor protect nothing": low
            # coverage means most items had no dataset rating to keep them. Per scan, so info.
            log.info(
                "scan.imdb_coverage", media="movie", requested=len(imdb_ids), resolved=len(imdb)
            )
        except DatasetDegradedError as exc:
            # The inverted failure: a missing rating REMOVES protection. Degrade loudly,
            # and flag it so build_facts reads every title as "we could not check" rather
            # than "we checked and it is unrated" -- degrading alone does not stop the
            # condemn set from being built on ratings nobody could read.
            context.degrade(str(exc))
            imdb = {}
            context.imdb_degraded = True
        movie_candidate_keys = {i.plex_rating_key for i in items if i.plex_rating_key}
        # Every merged bind's listing keys, by its canonical key -- shared below by the
        # popularity fold and the rewatch gather, so a file listed twice in Plex is
        # clustered over the same union of listings for both (rule 72).
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
        # A merged bind is one file listed several times in Plex; its plays are split
        # across the listings' rating keys. Fold each group's stats onto its canonical
        # key, or the item would under-count its own watching -- the direction that
        # condemns.
        await _fold_merged_watch_stats(
            engine,
            groups=merged_groups,
            window_days=movie_policy.popularity_window_days(),
            last_played=last_played,
            watchers_window=watchers_window,
            watchers_all_time=watchers_all_time,
        )
        # Qualified viewing stats for the habitual-rewatch keep (#554 stage 1), over the
        # same candidate set and the same merged-listing fold as the popularity counts
        # above.
        rewatch_stats = await movie_rewatch_stats(
            engine, movie_candidate_keys, groups=merged_groups
        )
        # The Stage 2 rewatch-probability fit (#554), refit every scan -- the movie lane's
        # fit, over exactly the candidate set the scorer scores below (the same
        # movie_candidate_keys and merged_groups the stats gather above uses, rule 72). The
        # season task fits the TV curve the same way, in season_scan.gather, over its own
        # candidate set. Cutoff is a year back from scan time (docs/history/REWATCH_PLAN.md,
        # Stage 2 Fit); added dates for the fallback training-pair route come off the scan's
        # own items, never a second read.
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
        # The lists this scan gathered membership from. Not a policy field and so not in the
        # hash above, which is the whole reason it is recorded separately: a retagged or
        # renamed list changes what every `on_list` rule protects without moving one byte of
        # any policy body. `None` when the registry could not be read -- fail-closed, and the
        # scan is degraded and un-plannable in that case anyway.
        list_config_hash=list_config_hash,
        horizon_at=context.horizon,
        item_count=len(items) + len(season_judgments),
        degraded=context.degraded,
        # A space, not "; ": `degrade` terminates every reason, so these are whole sentences
        # now and a semicolon between them would read "...this scan.; radarr 'x' unreachable".
        degraded_reason=" ".join(context.degraded_reasons) or None,
        # Every collection this scan saw, name to Plex's own member count (#816 phase 2).
        # NULL when none were read, whether none exist or the read failed -- the two are
        # indistinguishable on purpose (docs/history/COLLECTIONS_PLAN.md's fence): this is
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
    # Scoring configs are pure functions of the frozen policies -- identical for every
    # item -- so build them once here instead of once per item inside the judge loops.
    movie_custom = movie_policy.custom_signal_configs()
    movie_keeps = movie_policy.keep_configs()
    tv_custom = tv_policy.custom_signal_configs()
    tv_keeps = tv_policy.keep_configs()
    now = utcnow()
    # The owner's manual overrides -- ``media_key -> "spare" | "reap"`` -- applied to every
    # item's verdict: a spared file is judged PROTECT rather than surfacing in "would delete";
    # a reaped one is forced onto the list (short of a hard safety gate). Keys may be a show's,
    # covering all its seasons. Read *as of the scan's `now`* so an EXPIRED timed spare is
    # dropped here -- the one place a spare's clock is realized -- and the item is re-judged
    # from scratch, re-entering the reap flow on a fresh grace window (record_first_flagged_bulk
    # below). Live consumers keep an expired spare in force until this runs, failing to keep.
    override_map = await whitelist.overrides_effective_at(session, now)
    # Realize the expiry durably: `overrides_effective_at` dropped expired spares from the map
    # above (the read half), so delete their rows now in this same transaction (the write half)
    # -- otherwise every live consumer that reads `whitelist.overrides()` (planner, executor,
    # grace, review queue) keeps the expired spare in force forever and the item dead-ends,
    # unplannable and un-executable (rule 70). Same `now`, so the two halves agree exactly; the
    # re-condemned item earns a fresh grace clock from record_first_flagged_bulk below because
    # its old clock was deleted when the spare was set. Count only in the log -- no title/key.
    expired_spares = await whitelist.purge_expired_spares(session, now)
    if expired_spares:
        log.info("scan.spares_expired", snapshot=snapshot.id, count=len(expired_spares))
    total = len(items) + len(season_judgments)

    # Both lanes append here, and every count of the condemned set is this list's length.
    condemned_keys: list[str] = []
    # Which rung of the size ladder actually fired, counted across the whole scan. This
    # answers a question nothing in Reaper has ever measured: how often is a size simply
    # not reported? Counts only, never a title or a path.
    #
    # The miss bucket is seeded so it is reported as 0 rather than omitted (#321). It is the
    # one number an operator greps this line for, and a `Counter` drops an absent key, so
    # "nothing went unmeasured" and "the line was never emitted" would read identically. A
    # rung is left unseeded on purpose: absent there genuinely means it did not fire, and
    # seeding every member would print `sonarr: 0` on a Radarr-only install.
    size_sources: Counter[str] = Counter({_UNMEASURED: 0})

    # Scoring is pure in-memory now (no per-item I/O), so this measures the CPU cost of
    # judging every movie and season -- kept apart from the source-read wall above so a
    # slow scan is attributable to a source or to scoring, never lumped into one number.
    score_started = time.monotonic()
    watch_readings: dict[str, watch_evidence.Reading] = {}
    watch_blind = 0
    # This scan's ledger work (#553), accumulated in memory and flushed once after both lanes,
    # exactly as `watch_readings` is. `seen_returns` maps an id key to whether Reaper's own
    # journal claims the removal, which is filled in after the loop by one query rather than
    # per item.
    seen_keys: dict[str, set[int]] = {}
    seen_returned: set[str] = set()
    movie_absence_days = movie_policy.returned_absence_days()
    for index, item in enumerate(items):
        if index % 100 == 0:
            emit(Progress("scoring", index, total, item.title))
            # The judge is pure computation now (no per-item queries), so without this
            # the loop would hold the event loop for the whole scoring phase -- freezing
            # the very progress endpoint the emit above feeds.
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

        # The ledger read, and the detection off it (#553). Both need only values already in
        # hand, so this stays inside the pure loop and the write is deferred with the rest.
        # A key is built for every item that HAS one, but a sighting is recorded only on a
        # confident bind: no bind, no write, so a Plex outage records nothing rather than
        # recording an absence (`services.library_seen`).
        item_id_key = library_seen.id_key(
            media_type="movie",
            tmdb=item.tmdb_id,
            # Both spellings, the movie path exactly as the TV path (rule 29/106).
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
        # The same cohort_block decision build_facts made internally, re-derived off the
        # dormancy value it froze onto `facts` (rule 104: one derivation, two call sites,
        # so the two can never disagree) -- carried to `_judge_item` separately because
        # `Facts` does not hold a block's dormancy bounds. `_rewatch_odds_context` reads
        # this for the stored explanation's rewatch_odds block (#554 stage 2).
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
            # The scoring lane reads the honest Observation off `facts`; this is the
            # display and reclaim-accounting column. None means Radarr reported a file it
            # holds without a size, and it stays None: no file worth deleting is genuinely
            # 0 bytes, so a stored 0 would be a measurement Reaper never took.
            #
            # What that costs the item is deletion, while the owner's allowance
            # (`ProfileSettings.max_unmeasured_per_run`) is shut: `planner.build_plan`
            # holds it back, `executor._may_send_unmeasured` refuses it again per item, and
            # both caps and the byte total the owner confirms leave it out. With the
            # allowance open it IS planned and DOES count against the item caps; only the
            # byte sums still leave it out (`executor._deletable_bytes`). Either way it
            # still scores, still shows in the queue, and says "Size unknown" wherever its
            # size would appear. Do not "fix" this by inventing a size here.
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
                # Radarr's id first, the Plex-matched one as fallback -- the same
                # precedence the dataset lookup uses.
                imdb_id=item.imdb_id or item.plex_imdb_id,
                # A movie has no TVDb id (Radarr is tmdb-native); left None, so the Scales
                # join binds a movie request by tmdb/imdb as before.
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
            # (#275) without reading the reason text. False, not None: this scan took a
            # reading and it was honest. None is reserved for a row scanned before the key
            # existed, and for an item that had no reading to judge at all.
            watch_blind=blind_reason is not None if reading is not None else None,
            rewatch_block=rewatch_block,
            # A movie's own rating key is what a movie-library collection lists (#816
            # phase 2). None when unmatched -- an unresolved item was never looked up.
            collections=(
                collection_membership.get(item.plex_rating_key)
                if item.plex_rating_key is not None
                else None
            ),
        )
        if verdict == "condemn":
            condemned_keys.append(item.media_key)

    # What each show's season plan was decided from, frozen once per show. Every season of a
    # show carries the same bundle object, so this dedupes to one row per show; without the
    # dedupe a ten-season show would store it ten times for nothing. Written even for a show
    # whose every season is kept, which is the show a lowered keep-last makes prunable and
    # therefore the one the simulator is most often asked about.
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
            emit(Progress("scoring", len(items) + offset, total, judgment.title))
            await asyncio.sleep(0)  # keep the event loop live; see the movie loop above
        if judgment.watch_reading is not None:
            # The TV lane already decided blindness against these same marks and put the
            # reason on its facts; the reading is carried out here only so both lanes' marks
            # are raised in one write below.
            watch_readings[judgment.media_key] = judgment.watch_reading
        if judgment.watch_blind_reason is not None:
            # Counted from the decision the TV lane already made, never re-derived here.
            watch_blind += 1
        if judgment.seen_sighting is not None:
            # Same shape, same reason (#553): the TV lane decided this against the same marks
            # and already put the result on its facts, and the sighting rides out here only so
            # both lanes are written in one statement below. The population cap therefore reads
            # a whole scan rather than one lane.
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
            # Rule 72: the season lane reads its history by the season's own Plex key, so the
            # same fall is detectable here and the escape must be offerable on it too. The
            # season's reading is decided in `season_scan` before the show roll-up, so this
            # reads the judgment rather than recomputing it.
            watch_blind=(
                judgment.watch_blind_reason is not None
                if judgment.watch_reading is not None
                else None
            ),
            # The show's Stage 2 rewatch cohort block (#554 TV), the same one that fed
            # `judgment.facts.rewatch_cohort_n`/`rewatch_cohort_k` -- the movie call above
            # passes its own the same way, for the same reason (`_rewatch_odds_context`).
            rewatch_block=judgment.rewatch_block,
            # A TV collection lists SHOWS, not seasons (#816 phase 2) -- the same key the
            # poster uses, never `plex_rating_key`, which is the season's own.
            collections=(
                collection_membership.get(judgment.poster_rating_key)
                if judgment.poster_rating_key is not None
                else None
            ),
        )
        if verdict == "condemn":
            condemned_keys.append(judgment.media_key)

    # Grace clocks for everything condemned this run, in one batched pass -- the
    # _apply_first_flag decision per key, without a database round trip per item.
    #
    # Not on a degraded run. The condemn set here was built on evidence Reaper itself
    # declared untrustworthy, and planner.build_plan already refuses it outright -- but
    # the clock write does not go through the planner, so it would start (or silently
    # continue) a countdown for files a healthy scan would have kept. Worse, because
    # _apply_first_flag restarts a clock only after a gap longer than a whole grace
    # window, a run of consecutive degraded scans keeps refreshing last_seen_condemned_at
    # and the window never restarts: the first healthy scan then finds it already spent,
    # and the item's warning time is gone. Skipping can only ever cause an EXTRA restart,
    # which is more grace, the safe direction.
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
    # reading cannot lower it, and the next scan therefore asks the same question against the
    # same evidence instead of quietly accepting zero as the new truth.
    #
    # Deliberately NOT gated on `context.degraded`, unlike the grace clocks above (#276).
    # Rule 116 gates a degraded scan's side effects because they act on the condemned set --
    # a clock, a shelf, a Discord post all push an item toward deletion. This write is the
    # opposite kind: the mark is the evidence a LATER scan reads to withhold pressure, and
    # raising it can only ever add a reason to keep. Skipping it is what costs a protection.
    # Rule 28's sanctioned exception is the same trade read from the other side.
    #
    # Direction, concretely: `degraded` is snapshot-global and mostly fires on causes that say
    # nothing about this item's watch reading -- one *arr unreachable, sessions unreadable,
    # ratings unreadable. Skip the write on those and a title watched by five people whose
    # first scan happened to be degraded stores no mark; when its Plex key later churns, the
    # fall has nothing to fall from, and it reads Known(0) with maximum dormancy. That is the
    # exact defect `watch_evidence` exists to prevent, reintroduced by the gate meant to be
    # careful. `TestTheWatchBlindnessGuardThroughAWholeScan
    # .test_a_degraded_scan_still_records_what_it_measured` goes red if the gate is added.
    await watch_evidence.record(session, watch_readings, now=now)
    # The came-back ledger, both lanes in one write (#553). Not gated on `context.degraded`,
    # for `watch_evidence.record`'s reason two paragraphs up: this is evidence a LATER scan
    # reads to withhold deletion pressure, and skipping it costs a protection. Degradation
    # cannot manufacture a sighting either -- one is written only where an item bound to Plex,
    # so an unreadable source leaves the row untouched rather than recording an absence.
    #
    # **The cap is applied here rather than per item**, which is why the detection is
    # accumulated instead of acted on. A Plex library rebuilt slowly enough to outlast the
    # minimum absence satisfies every condition for every title at once, and only a whole
    # scan's count can see that. Refusing the batch costs the memory of any real return inside
    # it; granting it holds the library. #809 is the general scan-level guard and this stays
    # after it lands, because it is about what THIS feature will believe.
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
    emit(Progress("done", total, total, f"{len(condemned_keys)} candidates"))

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

    Four are load-bearing off that path. ``tmdb_id``, ``imdb_id`` and ``tvdb_id`` go onto the
    stored row and are what ``services/fairness.py`` joins a request to its candidate on
    (rules 29/106); ``title_slug`` builds the Sonarr link (``services/deep_links.py``).

    Every field defaults to ``None``, and ``scan`` packs one of these per lane by hand, so a
    field set in the movie pack and forgotten in the season pack drops that join for TV with
    nothing raising. ``test_every_display_field_the_source_carries_reaches_its_lanes_pack``
    is what refuses it, and its ``_DISPLAY_LANE_EXCEPTIONS`` holds the four fields one lane
    genuinely cannot answer."""

    year: int | None = None
    summary: str | None = None
    requested_by: str | None = None
    group_key: str | None = None
    group_title: str | None = None
    # Deep-link coordinates (the *arr web routes key on these, not on internal ids)
    # and the frozen display metadata. See services.display_meta and deep_links.
    tmdb_id: int | None = None
    imdb_id: str | None = None
    # The show's TVDb id for a season row; None for a movie (Radarr is tmdb-native). Sonarr
    # is tvdb-native, so this is what Scales joins a TV request to its candidate on when the
    # show carries no tmdb id (services.fairness). Join/link only, never a verdict input.
    tvdb_id: int | None = None
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
#: so the review chip (``api.review._kept_phrase``) tells it apart from a real keep-list
#: entry by this exact string. Every producer and that one reader import this constant;
#: never re-type the literal.
HAND_SPARE_DETAIL = "you spared this by hand"


@dataclass(frozen=True, slots=True)
class PolicyJudgment:
    """One item judged by policy alone: no session, no stored row, no hand override.

    The pure half of :func:`_judge_item` -- everything it computes before the ``session.add``
    -- lifted out so that a caller which only wants the *decision* is running the scan's own
    code rather than a lookalike. The policy lab (``tests/_policy_lab.py``) sweeps hundreds of
    de-identified real shapes through it; a lab that rebuilt this pipeline by hand would drift
    from the scan and pin the drift as ground truth (rules 3/22).

    ``verdict`` is the PURE POLICY verdict, hand override held out, exactly as stored on the
    Candidate row; :func:`effective_fate` applies the override on top.
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
    """Evaluate, score, round, decide, explain -- the whole judgment, storing nothing.

    Round FIRST, then decide -- and the returned integers are exactly what decided. The
    stored integers are what the table shows, what the why-panel explains, and what the
    simulator re-decides; if the verdict were taken from the underlying float instead, an
    item scoring 69.7 against a threshold of 70 would abstain while storing a 70, and the
    simulator would later condemn the very item the queue said it was sparing. There must be
    exactly one number, and everything must decide on it.

    ``extra_results`` (the season-pruning guard's outcome) is merged AHEAD of the ordinary
    gates: a guard PROTECT wins like any protection, and a guard *blocked* ABSTAIN (a
    keep-rule conflict) forces the item to abstain for a human to look at.

    ``rewatch_block`` (#554 stage 2) is the caller's already-derived rewatch cohort block
    for this item -- the same one that fed ``facts.rewatch_cohort_n``/``rewatch_cohort_k``
    -- carried separately because ``Facts`` does not hold the block's dormancy bounds.
    Both live lanes freeze a real block off their own fit; ``None`` means this item's cohort
    could not be measured, or the caller is hand-built ``Facts`` with no fit to derive one
    from at all (e.g. a policy-lab or test fixture).
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
        # One explanation, two uses: the frozen record stored on the row, and the same string
        # the effective-reap fate is read back from -- so the scan and every read-time
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
    """The item's fate with a hand override applied -- what the scan acts and counts on.

    Never what is stored. The Candidate row keeps the pure-policy verdict so an un-spare /
    un-reap falls back to the real policy result instead of the item taking the override on
    as its identity; this is derived on top, and derived again downstream the same way.

    A hand reap is read off the frozen EXPLANATION via the one shared decision
    (``condemned.reap_override_verdict``), NOT off the live evaluation: a bad Plex match
    holds a reap read-side but never reaches the gate evaluation, so deciding it live would
    honor a reap the planner and grace-clock resync hold -- orphaning a grace clock the
    delete set never claims (rules 3/4/22). A pure-policy condemn is trivially effective
    (mirrors ``condemned.reap_is_effective``'s shortcut).
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
    """Evaluate one item's gates and signals, store its candidate, return its EFFECTIVE fate.

    The candidate is stored with its PURE POLICY verdict (the hand override is held out); the
    returned string is the EFFECTIVE fate with the override applied, for the caller's grace
    clock and condemned tally only. Storage stays override-free so an un-spare / un-reap falls
    back to the real policy result; the effective fate is recomputed downstream the same way.

    Shared by the movie and season paths so both reach a verdict the same way, and the
    judgment itself is :func:`judge_facts` -- shared further still, with the policy lab, so
    a sweep of real library shapes exercises the scan's own pipeline rather than a copy.

    Seasons pass ``extra_results`` -- the season-pruning guard's outcome -- which is merged
    ahead of the ordinary gates: a guard PROTECT wins like any protection, and a guard
    *blocked* ABSTAIN (a keep-rule conflict) forces the item to abstain for a human.
    """
    # The stored verdict and explanation are PURE POLICY: the scan never bakes a hand override
    # into them. A hand "spare"/"reap" lives only in the whitelist and is re-applied live at
    # read and plan time (whitelist.effective_override, condemned.effective_condemned,
    # reap_is_effective, and the simulator). Keeping the stored verdict override-free is what
    # lets an un-spare / un-reap fall back to the real policy result -- before a rescan and
    # after one -- instead of the item taking the override on as its identity. The season-
    # pruning guard (extra_results) is genuine policy and stays; only the hand override is held
    # out here and applied on top downstream. See rules 48-51.
    #
    # The grace clock for a condemned item is set by the CALLER, batched across the whole
    # run (record_first_flagged_bulk) -- one query for every condemned key instead of a
    # read per item. The decision per key is unchanged: see _apply_first_flag.
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
            # This item's Plex collections (#816 phase 2), already sorted smallest-first by
            # the caller (_collection_membership). Navigation only, never a verdict input --
            # nothing above this line reads `collections`.
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
            # The frozen scoring inputs: the Facts plus the season-pruning guard (extra_results,
            # NOT the hand-override, which is re-applied live at replay time from the override
            # map). This is what the simulator replays under an edited policy. See facts_codec.
            facts_json=json.dumps(
                facts_codec.facts_to_dict(facts, extra_results=tuple(extra_results))
            ),
            created_at=now,
        )
    )
    # Return the EFFECTIVE fate (the override applied) -- NOT the pure verdict we stored. The
    # caller uses it to set the grace clock and the condemned tally over the set that will
    # actually be removed: a honored hand reap earns a fresh grace window, a hand spare gives up
    # its clock (rules 4/50). The fate is derived, never stored.
    return effective_fate(judged, override)


def _verdict(
    evaluation: Evaluation,
    score_value: int,
    coverage_bp: int,
    policy: PolicyBody,
) -> str:
    """The scan's adapter onto the ONE decision function, ``engine.verdict``.

    Takes the **stored** integers, not the underlying floats, so that this path and the
    simulator -- which has only the stored integers to work with -- cannot reach
    different verdicts for the same item under the same policy. Two code paths that
    answer the same question must answer it the same way, and the cheapest way to
    guarantee that is to give them the same function and the same inputs.

    **No hand override reaches here**, so this passes none of ``decide_verdict``'s reap
    arguments. A ``"spare"`` arrives as an extra PROTECT result and is already counted by
    ``evaluation.protected``; a ``"reap"`` is applied after the freeze, by ``effective_fate``
    off the stored explanation, and re-decided later by
    ``condemned.reap_override_verdict_decoded`` -- which is the one function that answers what
    a hand reap may overrule.
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
    """The stored ``rewatch_odds`` context (#554 stage 2), from the same in-memory values
    the item's ``Facts`` got.

    ``None`` when ``facts.rewatch_cohort_n`` is ``Absent`` -- hand-built ``Facts`` with no
    fit behind them at all (a policy-lab or test fixture), never a row either live lane
    froze: both the movie item and the show behind a season row always have an opinion
    about their own cohort, even when that opinion is Unknown (``services.snapshot
    .build_facts``, ``services.season_scan.build_season_facts``). Otherwise: the Unknown
    arms' zeroed placeholder with ``state="no_history"`` when there is no usable block, and
    the block's own pooled counts and range otherwise -- ``"thin"`` below
    ``gates.REWATCH_BLOCK_FLOOR_N``, ``"measured"`` at or above it. ``engine.explanation
    .RewatchOddsOut`` declares this same shape; both are held together by
    ``test_engine_derivations.TestTheStoredExplanationIsWrittenAsItIsDeclared``.
    """
    if isinstance(facts.rewatch_cohort_n, Absent):
        return None
    if block is None:
        return {"n": 0, "k": 0, "lo_days": 0.0, "hi_days": None, "state": "no_history"}
    return {
        "n": block.n,
        "k": block.k,
        "lo_days": block.lo_days,
        "hi_days": block.hi_days,
        "state": "measured" if block.n >= REWATCH_BLOCK_FLOOR_N else "thin",
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

    One dict, two sinks -- the UI and the audit log -- so what the owner was shown and
    what actually happened cannot drift apart.

    Three blocks, and the middle two are what make a verdict trustworthy:

    * protections that FIRED
    * protections CHECKED that did not fire, **with the actual numbers**
    * protections that COULD NOT BE CHECKED -- rendered amber, not green, because "we
      could not look" is not "we looked and it was fine"

    Plus a ``match`` block that says how (or whether) the item was bound to its Plex row --
    "bound by TMDB id 12345", or "kept: two Plex items share this id" -- so a file that was
    spared for a *matching* reason is not mistaken for one nobody looked at. And a
    ``rewatch_odds`` block (#554 stage 2), display only, written for both live lanes: see
    ``_rewatch_odds_context``.

    **Hand-typed on purpose, and held to the read side by a test rather than built from it.**
    ``engine.explanation`` declares what this document is, and
    ``test_engine_derivations.TestTheStoredExplanationIsWrittenAsItIsDeclared`` fails when a key
    here is not declared there, or the other way round.

    Building this from that declaration was measured and refused (the simplification plan's
    W5-1). It is the WIRE model too (``api.schemas.CandidateDetail.explanation``), so an alias
    or ``exclude_none`` change made for the API would reach disk. And its validators are
    deliberately lenient about an illegible stored byte, which on this side normalizes a
    writer's own value to ``None`` where no reader can recover it.
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
            # The coverage line the verdict was decided against, frozen beside the threshold
            # because it is the same class of number: a policy value the score is compared to,
            # which the panel restates so an abstain forced by too little readable evidence can
            # name the line it fell under. Read here, never off the live policy, which by the
            # time anyone opens the panel need not be the one this item was scored under (rule
            # 113). Additive and nullable: a row frozen before this shipped thaws to None and
            # the panel drops the floor clause, exactly as threshold does.
            "coverage_floor_bp": policy.coverage_floor_bp,
            "coverage": round(item_score.coverage, 3),
            # Whether THIS item was held because plays recorded earlier stopped being
            # readable. Typed, because the panel offers the per-title escape (#275) on it and
            # the only other way to know is to match `watch_evidence.BLIND_REASON` inside an
            # observation's reason, which is operator copy and will be reworded (rule 92).
            #
            # Three-state, never a bare bool (rule 142): a row scanned before this shipped has
            # no key and thaws to None, which the panel reads as "cannot tell" and shows no
            # control for. False is the positive claim that this item read honestly.
            "watch_blind": watch_blind,
            "match": {
                # status is what the UI reads: "matched" -> stay quiet, anything else -> a
                # plain "kept to be safe" notice, worded per status (MatchStatus says what
                # each one means, and they are not interchangeable). by/detail are kept for
                # the audit log, not shown to the owner.
                "status": match_status.value if match_status is not None else None,
                "by": matched_by.value if matched_by is not None else None,
                "detail": match_detail,
                "rating_key": plex_rating_key,
                # Every listing a merged bind covers (one file listed several times in
                # Plex). The executor's live interlocks re-read THIS list, so the keys
                # they protect are exactly the keys the owner was shown.
                "merged_rating_keys": (list(merged_rating_keys) if merged_rating_keys else None),
                # The rows an abstain was choosing between, so the panel can offer a link to
                # each instead of naming a problem in Plex with no way to open it. Display
                # only -- no verdict reads it, because not knowing which of these the file
                # is, is why there is no rating_key.
                "candidate_rating_keys": (list(match_candidates) if match_candidates else None),
            },
            # The Stage 2 rewatch-probability context (#554), written for both live lanes --
            # see _rewatch_odds_context. None for an item whose own cohort could not be
            # measured, for hand-built Facts with no fit behind them at all, and for a row
            # frozen before this field existed, all read by the panel as nothing to show.
            # Written unconditionally, like every other optional key here: the top-level
            # document always carries the keys engine.explanation.Explanation declares,
            # whatever their value (test_engine_derivations
            # .TestTheStoredExplanationIsWrittenAsItIsDeclared).
            "rewatch_odds": rewatch_odds,
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
                    # The line this row was measured against, frozen with everything else
                    # the scan froze. The panel states the arithmetic from these; it must
                    # never read them off the live policy, which by the time anyone opens
                    # the panel need not be the policy this score was computed under.
                    # Null on a rule with no ramp -- a boolean custom rule matched or did
                    # not -- and the panel omits the sentence rather than invent a line.
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
            # ``defers_to_owner`` is written on every entry, never omitted when False, so
            # a row frozen by THIS version is distinguishable from one frozen before the
            # flag existed (rule 104's explicit thaw). The card's chip (``api.review._chip``)
            # and the why panel's verdict note both read that difference, the panel through
            # ``api.schemas.GateOutcomeOut``: present-and-True names the comparison Reaper
            # made, present-and-False says it could not make one, and absent names neither,
            # falling to the vague-but-true wording.
            #
            # It decides nothing about a hand reap. It used to -- and the write is worth
            # keeping for the chip alone, because a legacy row genuinely cannot tell the two
            # shapes apart and must not be made to assert either.
            # ``unestablishable`` rides beside it on the same terms and for the same reason:
            # written on every entry so a row frozen by THIS version says which shape it is,
            # where a row frozen before it says nothing and the panel reads that as the shape
            # those rows already had (a keep-rule conflict).
            "protections_unknown": [
                {
                    "gate": r.gate.value,
                    "detail": r.detail,
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
    """Set the grace clock once, and never move it while the item stays condemned --
    but DO restart it when an item that had left the condemned set comes back.

    A transient Sonarr timeout that drops an item from one snapshot must not reset the
    clock -- otherwise the item can never age out and the grace period becomes unreachable.
    That is why ``first_flagged_at`` is not touched on an ordinary re-condemn.

    The other direction is just as important, and was the actual gap: an item condemned
    long ago, then *rescued* (watched, spared, or re-judged as protect) and later condemned
    again a full dormancy period afterwards, must serve a FRESH grace window. Its old
    ``first_flagged_at`` is far in the past, so grace_report would drop it straight into
    ``ready`` with no countdown and no Leaving Soon warning. (The window holds nothing back
    either way, see ``services.grace``; what is lost is the warning.) We detect the return
    by the gap since it was last seen condemned: when that gap exceeds the grace window (so
    it genuinely left, not just missed a snapshot to an outage), the clock restarts.
    ``last_seen_condemned_at`` exists for exactly this reset.

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
        # Trace it: this fresh window is why a returned item has a full countdown again, and
        # was historically the actual gap. At debug, occasional per item.
        log.debug(
            "scan.grace_clock_restarted",
            media_key=media_key,
            gap_days=(now - last_seen).days if last_seen is not None else None,
        )
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

    ``sizeOnDisk`` is a ``SUM`` over the movie's tracked ``MovieFiles`` rows, not a folder
    walk, and it is the best number Radarr offers for the reclaim estimate and the byte
    cap. The delete removes the movie folder, so bytes no row tracks are freed and never
    counted here. **Measured** (#317): the folder held more than this number in every one
    of 200 sampled movies, by 0.02% at the median, 1.2% at the 90th percentile and 11% at
    the worst, with one folder on a second library at 44%. So it is a close LOWER bound on
    what the delete frees, never an over-statement -- which is the wrong direction for a
    byte cap, and why ``ProfileSettings``'s caps comment states what they bound. Accepted
    rather than repaired; ``docs/DECISIONS.md`` under *Size acquisition* says why, and what
    was declined. Distinct from :func:`_movie_file_size`, which reads ``movieFile.size``
    for file-to-file identity comparison.

    Missing or zero is ``None``, never ``0``: see ``RawItem.size_bytes``.
    """
    size = movie.get("sizeOnDisk")
    return int(size) if isinstance(size, int | float) and size > 0 else None


def _movie_file_size(movie: Mapping[str, Any]) -> int | None:
    """The exact byte count Radarr records for the movie's file, or ``None``.

    The corroborator that tells apart several Plex listings carrying the same file name:
    an exact byte match is the same file (or a bit-identical copy of it); a mismatch is a
    different file. Deliberately ``movieFile.size`` and not ``sizeOnDisk``, because the
    comparison must be file-to-file and ``sizeOnDisk`` sums every tracked row. Not because
    ``sizeOnDisk`` "includes extras", which this said and which is backwards: it holds no
    untracked byte at all, and equalled ``movieFile.size`` for every movie holding a file
    on two live libraries (learning 14). Zero or missing is unknown.
    """
    movie_file = movie.get("movieFile")
    if not isinstance(movie_file, dict):
        return None
    size = movie_file.get("size")
    return int(size) if isinstance(size, int) and size > 0 else None


def _summary(text: Any) -> str | None:
    """A trimmed overview. Kept short -- the card shows a couple of lines."""
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


def _log_movie_decision(instance_id: int, movie: Mapping[str, Any], *, outcome: str) -> None:
    """One greppable DEBUG line per movie: what Radarr reported, and why the film did or did
    not become a candidate. The movie twin of ``season_scan.series_decision``.

    ``outcome`` is ``candidate`` (Radarr holds a file, so it is judged) or ``no_file`` (no
    downloaded file, nothing to reap). Grepping a title answers "why isn't my movie in the
    queue" without re-running the scan. Plex match status is logged separately
    (``scan.plex_matched`` / ``scan.plex_unmatched``); an unmatched movie still becomes a
    candidate and is never dropped here.

    The ids are the CLEANED ones (``identity.ExternalIds.of``), not Radarr's raw strings, so
    the line says what Reaper matched with. A source emitting the ``tt0000000`` sentinel logs
    it as no id, which is what it was treated as.
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
    # Stale-mapping guard, aggregated across this one instance's movies: a mapping that never
    # once matched a candidate library is warned about after the loop; one that matched even a
    # single movie is working and stays quiet.
    mapped_lib_hits: set[str] = set()
    stale_map_misses: dict[str, str] = {}
    for movie in movies:
        if not movie.get("hasFile"):
            without_file.append(str(movie.get("title") or "?"))
            _log_movie_decision(instance_id, movie, outcome="no_file")
            continue
        _log_movie_decision(instance_id, movie, outcome="candidate")
        # THE door in for this movie's ids (identity.ExternalIds.of): the sentinel filter runs
        # once here, and everything below reads `ids` rather than the raw payload. A raw
        # `imdbId` of "tt0000000" is truthy, so carrying it onto the RawItem would shadow the
        # id Plex matched at every `item.imdb_id or item.plex_imdb_id` downstream -- including
        # the keep-list lookup in `build_facts`, which would then run under an id no list row
        # carries and condemn a keep-listed film (#709).
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
            # container's mount point. An empty tuple stands the folder step down; None
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
                # Both abstains, not AMBIGUOUS alone (rule 143): CONFLICTED was split out of
                # it and carries the same evidence about the map. The operator declared which
                # library this root folder lands in and no copy the ids named is there, which
                # is the mapping being wrong whether the rows were several or merely
                # contradictory.
                stale_map_misses.setdefault(plex_library, str(movie.get("title") or ""))
        matched = resolution.plex_item
        if resolution.rating_key is None:
            # The movie has a file in Radarr but Reaper could not confidently bind it to a
            # Plex row, so it appears only as "kept to be safe", never on the reap list.
            # Warned per item so an operator asking "why isn't this in review" finds the
            # reason in the log, not only on the row's why-panel. UNMATCHED = nothing in Plex
            # looked like it; AMBIGUOUS = more than one did and we refused to guess.
            # For an AMBIGUOUS movie the library map is the operator's declaration of which
            # library each root folder lands in; a movie still leans on size after, but the
            # map is tried first. Naming what was mapped for this item (None = nothing mapped)
            # and the libraries the copies live in makes "no file size to tell them apart"
            # actionable from the log rather than a dead end.
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
            # The matched path -- the common case, and the only place the tricky binds
            # (a shared id narrowed by file name/size/folder, or several Plex listings of
            # one file merged) are decided and recorded. At debug so a large library does
            # not flood the log, but every bind is traceable when checking "why did this
            # match that Plex row".
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
                # added_at comes from the matched Plex item (Tautulli spine), preserving the
                # dormancy floor exactly as before.
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
        # Not unmatched, just nothing to reap yet: monitored in Radarr with no downloaded file,
        # so there is nothing on disk to put in the queue. Warned (not INFO) so the operator is
        # aware some monitored movies are absent for a benign reason; each such movie also
        # emitted a scan.movie_decision line (outcome=no_file) naming it at debug.
        log.warning(
            "scan.movies_without_file",
            instance_id=instance_id,
            count=len(without_file),
            detail=(
                f"{len(without_file)} movies are monitored with no file downloaded, so they are "
                "not in the review queue. There is nothing on disk to remove."
            ),
        )
    # The stale-mapping guard: warn once for a mapped library that never matched a candidate
    # library across this instance's movies (renamed library, or a wrong mapping). Advisory,
    # visible in the in-app Logs beside scan.plex_unmatched; never degrades or changes a verdict.
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

    # The cache is rebuildable and may be empty on a fresh install. Ensure the table exists
    # so a never-synced cache reads as "no plays" rather than crashing the scan with 'no such
    # table'. What makes that fail-closed is NOT the dormancy observation: a zero-row mirror
    # leaves `mirror.earliest` None, so `scan` resolves the horizon to `utcnow()` and an item
    # with an arrival date reads `max(added_at, utcnow())`, i.e. Known ZERO days dormant. The
    # hold comes from `scan` degrading the whole snapshot un-plannably on that same empty
    # mirror ("no watch history at all: nothing can be judged"), with the zero-day reading
    # under any dormancy floor as the second layer.
    await history_sync.ensure_schema(engine)

    # Deliberately NOT clamped up to the data horizon, though a window reaching past it is
    # exactly the bug this guards. Clamping here would change no count: the horizon IS the
    # oldest row, so there is nothing between it and `window_start` to find, and the query
    # would return the same number while reading as though the hole were closed. The hole
    # is in what the number MEANS, so it is closed where the number is interpreted --
    # `Facts.history_reach_days` records the reach and `gates.ServerPopularityGate` refuses
    # to report a protection as checked over a window the mirror does not span.
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
    # Unclamped by the horizon for the reason spelled out in `_watch_stats`: the clamp
    # would move no count, and the reach is carried on `Facts.history_reach_days` instead
    # (rule 72).
    window_start = int((utcnow() - timedelta(days=window_days)).timestamp())
    per_key: dict[int, list[Any]] = {}
    async with engine.connect() as conn:
        # Chunked, like every sibling that expands an IN over library-sized key sets
        # (season_watch_stats, record_first_flagged_bulk). SQLite caps the number of bound
        # variables in one statement; a library with enough merged listings to pass that
        # cap raised OperationalError, which is not an IntegrationError and so was caught
        # nowhere: the whole scan died rather than one fold being skipped.
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
    """Refresh every protection list, **before a scan reads them.**

    This is the wiring that makes the list-based protections actually fire. The
    providers and the membership tables have always existed, but nothing populated them
    at scan time -- so a "Never Reap" collection, a ``reaper-keep`` tag, and the IMDb
    Top 250 were all silently empty, and a protection that is empty is a protection that
    does not protect. A whitelist that quietly fails open is the worst kind of bug this
    tool can have.

    **Every list comes from ``definitions``**, the registry the operator edits on
    Settings -> Lists -- the keep tags included, which used to arrive as policy
    parameters here and are a tag list like any other now. The registry replaced three
    hardcoded parameters, and the Plex one was a live bug: the keep collection was read
    out of a library hardcoded to ``"Movies"``, so an operator whose movie library is
    called anything else had their "Never Reap" collection silently never read, and --
    a failed sync of it degrading the scan -- could not reap at all (#483). A definition
    names the library, so there is nothing left to guess.

    A provider that finds nothing is not an error (the owner may not have made the tag
    or collection yet). A provider that *fails* is recorded against its slug and does not
    abort the others -- but the caller can see which lists are stale, and a scan that
    relied on a failed hard-gate list left with no stored copy should treat itself as
    degraded rather than delete something the list would have protected. The atomic-swap
    in ``lists.sync`` guarantees a failed refresh leaves the previous membership intact
    rather than emptying it.

    This pass also **retires** the lists the current configuration no longer produces
    (``lists.retire_absent``), because a stored list outlives the setting that made it:
    tightening "match ANY" to "match ALL" changes the slug, and so does switching a list
    off or deleting it. Without the sweep the old row sits there enabled, still protecting
    from a definition the operator has already replaced, and the change they saved never
    takes effect.

    Retiring is a durable protection-DISABLING write, so a family is retired only when the
    configuration it is judged against was actually readable, and only when the lists that
    replace it actually synced (rule 115). Both Plex families need a live ``plex_server``.

    ``definitions`` is three-state, and the third state is the one that matters: ``None``
    means the registry could not be READ, which is not the same fact as an operator having no
    lists (rule 1, rule 93). An empty tuple builds no providers and retires everything the
    registry no longer produces, which is correct when the answer is genuinely "none"; doing
    that on a failed read would disable every list on the install because a table was briefly
    unavailable. So ``None`` builds nothing AND retires nothing, and the caller degrades the
    scan.

    ``only`` narrows the pass to one row of the Lists screen, for its "Check now" button.
    **A narrowed pass never sweeps a FAMILY**: a family sweep disables every stored list the
    pass did not produce, so running one over a single list's output would switch off every
    other list in that family. It sweeps that one DEFINITION instead, which is the truth a
    pass over one definition actually holds: editing a list changes its slug, and the
    superseded row would otherwise stay enabled under the same definition id.
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
    # own, so they refresh concurrently -- the whole pass takes as long as the slowest
    # provider instead of the sum. The database writes inside lists.sync stay atomic per
    # list; SQLite allows one writer at a time, and the 5s busy_timeout pragma (see
    # db/session.py) queues the brief overlapping writes -- each provider's write is a
    # few hundred rows, far inside that budget.
    runs: list[Coroutine[Any, Any, None]] = []

    # Every slug this configuration produces, per family, collected as the providers are
    # built. Each retire sweep below reads its own set as the whole truth about that family.
    imdb_slugs: set[str] = set()
    plex_slugs: set[str] = set()
    watchlist_slugs: set[str] = set()
    keep_tag_slugs: set[str] = set()

    # A deleted definition builds no provider, which is what puts its slug outside the
    # `current` set below and retires it: without the sweep the stored membership would stay
    # enabled and the list would go on protecting after being removed (rule 25).
    #: Whether the registry was readable at all. See the docstring: unreadable retires nothing.
    registry_known = definitions is not None
    if registry_known:
        # Rows stored before their definition existed take the definition's slug first, so
        # this pass refreshes the adopted row in place -- and if its own sync then fails,
        # the membership the legacy row earned is already under the slug that coasts.
        await lists.adopt_legacy(engine, definitions or ())
        # And the name its keep rule matches, which the legacy row never carried. The coast
        # above is what makes this load-bearing: an adopted row whose own sync then fails
        # keeps `rule_name` NULL, so `Membership.matched_by()` falls back to the display name
        # ("Radarr (HD) tag: reaper-keep") while the rule spells the definition's name -- the
        # membership is stored, the scan is executable, and it protects nothing. `api/lists.py`
        # pairs these two calls on both of its paths; this sibling was not swept (rule 72).
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
            # No live server, no provider and no slug -- and the Plex retire below stands
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
    # something unexpected (a cache-database fault) can raise here -- and when it does,
    # the surviving providers are canceled and drained rather than left refreshing
    # lists for a scan that is already dead.
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
        """Sweep one family, if its inputs were readable AND its own syncs landed.

        Rule 115's second half, which the caller cannot express on its own: a slug whose sync
        FAILED is in ``current``, so the sweep would leave it alone -- but the row it is meant
        to replace is not, and disabling that one on the strength of a sync that did not land
        withdraws the only membership still protecting anything. The stored copy is the live
        protection until something actually replaces it.
        """
        if only is not None or not when or not registry_known:
            return
        if current & failed:
            log.info("lists.retire_skipped", family=family, failed=sorted(current & failed))
            return
        for slug in await lists.retire_absent(engine, family=family, current=current):
            synced[slug] = "retired"

    # The tag family's `current` set is built from the definitions and the *arr rows alone
    # -- both local settings -- so a briefly unreachable instance is still in it and its
    # list is never retired over a network blip. It also carries the legacy no-definition
    # slugs the policy keep tags wrote before the upgrade, which is what re-homes them: the
    # definition-driven sync lands, the old spelling drops out of `current`, and the sweep
    # stands the old rows down.
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

    # A narrowed pass sweeps its OWN definition, which is a narrower claim than the four
    # above rather than an exception to the rule that stops them: those read one family's
    # whole output as the truth about that family, and this reads one definition's output as
    # the truth about that definition, which is exactly what a pass over one definition
    # knows. Editing a list changes its slug -- a tag list's carries the match mode
    # (``ArrTagRule.slug``) -- so without this the superseded row stays enabled under the
    # same definition id, and the Lists screen sums both into one "Protecting N titles"
    # roughly twice the real number until a full pass runs.
    #
    # Rule 115 both ways, which is why this is here and not in ``api.lists.edit_list``: the
    # sweep runs only after the replacing rows actually landed, and a pass that produced no
    # slug at all (a Plex collection checked while Plex is unreachable) sweeps nothing, since
    # the stored row is still the live protection until something replaces it.
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
#: Set tighter than this and a paused Tautulli blocks every scan; looser, and items drift
#: toward condemnation for as long as the outage lasts. Same bound as
#: WHITELIST_STALE_AFTER and the same two-nightly-cycles logic behind it, but a different
#: quantity: that one bounds a *failed* sync coasting on stored keep-list membership,
#: this one bounds a sync that stopped running at all.
MIRROR_STALE_AFTER = timedelta(hours=48)


#: How far short of the source's own count the mirror may sit before the snapshot degrades,
#: as a fraction of that count. Empty is caught by the horizon test and stale by the clock
#: above; this is the third state, and the only one of the three that looks healthy from
#: every angle: populated, freshly synced, and missing a third of the evidence.
#:
#: **Both ends of this are measured, on a 425,604-row history.** An incremental sync fetches
#: only what is new, so its own paging total is the size of the increment (`of=266`) and it
#: completes correctly while the mirror sits at 274,992 of 425,596. Nothing in that walk is
#: wrong, and nothing in it can notice. The gap was 35%.
#:
#: The *legitimate* gap is far smaller and has a known cause: a play still in progress is
#: counted by the source and deliberately skipped by the ingest (`history.rows_skipped`),
#: so the mirror can never equal the total and an equality here would degrade every scan
#: forever. On the full sweep that gap was 8 rows, 0.002%. A percent leaves that three
#: orders of magnitude of headroom and still catches a defect 35 times its size.
#:
#: The floor is what makes it safe on a SMALL history, where the same handful of live plays
#: is a large fraction: 50 concurrent streams against 5,000 rows is 1% on the ratio alone.
#: A scan degrades only when the mirror is short by BOTH.
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
    caller to decide what to do about it. A list that feeds a **HARD** gate (it can PROTECT
    a title outright) fails *open* when its stored copy is empty: the gate reads no members,
    protects nothing, and an executable snapshot would reap the very titles the list exists
    to save. So any failed HARD-mode list with no stored members degrades the snapshot,
    whether it is a keep-list (whitelist) or a curated protected list such as the IMDb Top
    250. A **SOFT** list only feeds a scoring nudge and never unprotects a kept title, so a
    failure of one does not degrade.

    Beyond the empty case, recency is a keep-list concern. The atomic swap in ``lists.sync``
    keeps the prior membership on a failed refresh, so a populated list still protects -- but
    a whitelist the owner actively adds to must reflect a title tagged since the last good
    sync, so a stale or never-confirmed whitelist degrades too. Each case resolves toward
    keeping files:

    * **No membership to fall back on** (any HARD list): a first scan, or a newly-added
      keep-list or curated list that has never synced once.
    * **Stored membership older than ``WHITELIST_STALE_AFTER``** (whitelist only). Every hour
      of staleness is an hour a newly keep-tagged title is unprotected, so past the bound the
      snapshot degrades until a sync succeeds. A curated external list churns slowly and keeps
      protecting from its stored copy; its staleness bound is a separate policy, not here.
    * **No record of a successful sync at all** (whitelist only, members present but no
      ``last_synced_at``): recency cannot be confirmed, so it is not assumed.

    A fourth case is not a failed sync at all: a keep list this pass **did not check**, so it
    never reaches ``synced`` and none of the three above can see it. Unlinking Plex is the way
    in -- ``DELETE /api/settings/plex`` drops only the server row, the collection and watchlist
    definitions stay enabled, and with no live server no provider is built for either. The
    stored membership goes on protecting and goes on aging, with nothing bounding it, so a keep
    list that has been unreadable for months reads exactly like one checked minutes ago. That
    is the same unprotected window the recency bound above exists for, reached without an
    error, so it is bounded the same way and by the same constant, over the stored rows rather
    than over this pass's output.

    **Keyed on a stored row, never on a definition**, which is what keeps a Plex-less install
    off it: a row is written only by a sync that actually ran (``lists.sync``, and
    ``_record_sync_error`` on the failing path), so the seeded Plex collection on an install
    that has never linked Plex has no row and cannot degrade anything. Disabled rows are
    skipped for the reason they always are: ``retire_absent`` disables a superseded slug and
    keeps its members, so it reads as populated while protecting nothing.
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
            # What the operator calls this list, for the sentences below. They reach the scan
            # banner and the reap page verbatim, and a slug is internal vocabulary that rule 21
            # keeps out of operator copy -- it carries a match mode, an instance id and now a
            # definition id, none of which name anything the reader can go and fix. The slug
            # stays as the fallback for a list that failed before it was ever stored, which is
            # the one case where no display name exists yet.
            named = str(row[3]) if row is not None and row[3] else slug
            # Only a HARD-mode list feeds a PROTECT gate, so only it can fail *open* when its
            # stored copy is empty. A SOFT list merely feeds a scoring nudge, and losing that
            # never unprotects a kept title. A missing row (never synced even once) is treated
            # as hard-shaped: fail closed rather than guess.
            if mode is not None and str(mode) != lists.ListMode.HARD.value:
                continue
            # Count only members of an ENABLED list, because only an enabled list protects
            # anything. ``lists.retire_absent`` disables a superseded slug with
            # ``enabled = 0`` and deliberately KEEPS its members, so a disabled row still
            # reads as populated -- and a slug carries the operator's match mode, so
            # flipping keep-tags from "any" to "all" and back retires and revives slugs in
            # normal use. Counting rows alone let a disabled list vouch for a failed sync
            # and skip the degrade, which is exactly the state where the gate protects
            # nothing (rules 2 and 115).
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
            # last_synced_at is written only on success (lists.sync), so it IS the last
            # successful sync; from_epoch returns None for a null or zero stamp.
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
            continue  # checked this pass; the walk above already had its say
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
