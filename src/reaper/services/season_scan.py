# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gathering TV seasons for a scan -- the read-only half of season pruning.

The movie scan (``snapshot.scan``) reads Radarr, joins each film to Plex by title, and
judges it. This module is the same shape for television, with one hard extra step: the
unit of action is a **season**, not a show, so every season must be resolved to its own
Plex rating key before its watch history can be read.

That resolution is the one part of this feature that cannot be proven by a unit test
alone, and the whole module is built so that when it is *uncertain* it fails toward
keeping the season:

* A Sonarr instance that cannot be read **degrades the snapshot** -- exactly like a
  missing Radarr -- so no run may execute against a library we only partly saw.
* A series we cannot locate in Plex, or a season whose Plex rating key we cannot
  resolve, still becomes a candidate -- but with ``Unknown`` watch facts, which the
  gates turn into an ABSTAIN. A season we cannot see is never condemned.
* The season-pruning **guards** (``services.season_pruning``) run first, as a hard
  floor: the last N seasons, the first season, a season someone is part-way through,
  and any currently-airing season are protected outright, whatever they score.

Only seasons that survive the guards as *prunable* can ever be condemned; a show whose every
season is guard-kept is still gathered and surfaced as kept, so content is never hidden from
the UI (only a show with nothing on disk has no season to show). Plex resolution is one paged
sweep over every season in the allowed show libraries (see below), so it is no longer bounded
per show; the per-show fallback (when the sweep cannot resolve a show) covers every show with
content, and only the Sonarr episode fan-out stays limited to shows that have something
prunable -- a fully-kept show needs no mid-binge precision.

## The season -> Plex rating key join (verify against a live server)

Tautulli has no season-sweep command (``get_library_media_info`` returns show-level rows
only), but Plex itself lists every season in a show library in a handful of paged
``type=3`` reads. So a library's seasons are resolved in one sweep
(``PlexClient.library_season_index``), grouped under each show's rating key, replacing a
per-show ``get_children_metadata`` call each. A show the sweep does not return falls back
to that per-show call, so the join can only ever match or beat the old coverage. The
season keys are identical either way: both address the same linked server, so a swept
season's rating key equals the per-show call's and joins the same watch history (verified
live, key-for-key, before the sweep replaced the fan-out). These field assumptions are
load-bearing:

1. A show row from ``get_library_media_info`` carries ``rating_key`` and ``year``, and
   its ``title`` matches the Sonarr series title closely enough to join on.
2. A ``type=3`` season row carries ``ratingKey`` (the season's Plex key), ``index`` (the
   season number), and ``parentRatingKey`` (its show) -- and ``get_children_metadata``
   returns the same ``rating_key`` and ``media_index`` for the fallback.
3. Each season row carries its OWN ``added_at`` -- the date that season's files landed,
   not the show's -- so a season backfilled into an old show reads as recently arrived.
   Dormancy is floored on this, never on the show's old date. A season whose ``added_at``
   cannot be read is still judged when it has a play, because dormancy IS days since the
   last play (``engine.dormancy.reference_instant``); only a season with neither a play nor
   an arrival date has nothing to measure from, and that one is Unknown-dormant and abstains.

All are the documented shapes, but "documented" is not "verified", so the ambiguity policy
and each fact are isolated in pure functions with fail-closed defaults rather than trusted
inline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import batched
from typing import Any

import structlog
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.aio import gather_reaped
from reaper.clients.base import IntegrationError
from reaper.clients.plex import PlexClient, PlexError, PlexSeasonRow
from reaper.clients.sonarr_stats import SeasonStats, parse_season_stats, rank_seasons
from reaper.clients.tautulli import TautulliClient
from reaper.clock import from_epoch, utcnow
from reaper.db.models import SizeSource
from reaper.engine import identity
from reaper.engine.dormancy import dormancy_days, reference_instant
from reaper.engine.gates import ABSTAIN as GATE_ABSTAIN
from reaper.engine.gates import PROTECT as GATE_PROTECT
from reaper.engine.gates import (
    Facts,
    GateId,
    GateResult,
    lifetime_shortfall,
    progress_is_establishable,
)
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.ratings import Rating, RatingSource, merge_by_source
from reaper.services import history_sync, library_index, lists, requested_by, watch_evidence
from reaper.services.display_meta import (
    IMDB_UNREADABLE_REASON,
    NO_IMDB_ID_REASON,
    build_ratings_json,
    dataset_lookup,
)
from reaper.services.imdb_dataset import DatasetDegradedError, ImdbRating, ImdbRatings
from reaper.services.season_pruning import (
    SPECIALS_SEASON,
    SeriesPrunePlan,
    active_progress,
    plan_series_prune,
)

log = structlog.get_logger(__name__)

#: Fail-safe default for the optional custom-rule fact observations (see gates._UNSET).
_UNSET_OBS: Absent = Absent(source="unset")


def _rating_obs(value: float | None, looked_up: bool, dataset_degraded: bool) -> Observation[int]:
    """One IMDb figure as a three-state observation. See build_season_facts."""
    if value is not None:
        return Known(value=int(value), source="imdb")
    if dataset_degraded:
        # The dataset itself was unreadable, so the empty lookup map is not an answer
        # about this show. Absent here would claim we checked and found it unrated --
        # for every show at once -- which withdraws every rating-based keep.
        return Unknown(reason=IMDB_UNREADABLE_REASON, source="imdb")
    if looked_up:
        return Absent(source="imdb")
    return Unknown(reason=NO_IMDB_ID_REASON, source="imdb")


#: How many shows to read per-show against Tautulli / Sonarr at once. Season resolution is
#: now one paged Plex ``type=3`` sweep of the show libraries, not a call per show, so this
#: bounds only the Sonarr ``episodes()`` fan-out (episode-precise mid-binge) and the season
#: sweep's per-show fallback (a show Plex did not return). Set high enough to collapse many
#: round trips into a handful of batches, low enough that a modest self-hosted Tautulli/Sonarr
#: sees a bounded burst, never a stampede. Every call is a read that fails closed (an
#: unresolved show abstains; a failed episodes() falls back to season-level protection), so a
#: timeout under load never over-condemns -- it only keeps.
RESOLVE_CONCURRENCY = 8


def _series_summary(series: Mapping[str, Any]) -> str | None:
    overview = series.get("overview")
    if not isinstance(overview, str) or not overview.strip():
        return None
    return overview.strip()[:600]


@dataclass(frozen=True, slots=True)
class SonarrSource:
    """One Sonarr instance, and the id its seasons are keyed by."""

    client: Any  # SonarrClient; typed loosely so tests can pass a fake
    instance_id: int
    name: str
    # This instance's HD/4K library map: {root folder path: Plex library title}. Empty when the
    # operator has set none, which keeps a show duplicated across two libraries on its old
    # abstain-and-keep behavior (a show has no size or usable folder to fall back on).
    library_map: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SeasonJudgment:
    """One season, gathered and ready for the shared judge loop.

    Carries the season-guard verdict as a ``GateResult`` so the scan can merge it with
    the ordinary gates: the season-pruning guards are just another lane of hard
    protection, and the why-panel renders them exactly like any other gate.
    """

    media_key: str
    plex_rating_key: int | None
    title: str
    size_bytes: int | None
    size_source: str | None
    """Which measurement ``size_bytes`` holds, and None exactly when that is None. Read by
    the executor to compare like with like, and counted per scan so Reaper can say how
    often a size is simply never reported."""

    facts: Facts
    guard_result: GateResult
    watch_reading: watch_evidence.Reading | None = None
    """What this scan measured for the season's watch history, for the caller to fold into
    the high-water marks. ``None`` when the season resolved to no Plex key, which is not the
    same as zero: an unmatched season was never looked up, and recording a zero for it would
    hold its true mark down and stop the check ever firing for it (rule 93)."""

    watch_blind_reason: str | None = None
    """Set when this season's watch history stopped being readable, and already applied to
    ``facts``. Carried so the caller can COUNT it without deciding it a second time: one
    decision, made where the marks are compared, read everywhere else."""
    # Display fields, carried onto the candidate. A season's poster/blurb/year are the
    # show's; ``group_key``/``group_title`` collapse every season under one show row in the
    # review queue. None of them affect the verdict.
    year: int | None = None
    summary: str | None = None
    poster_url: str | None = None
    requested_by: str | None = None
    group_key: str | None = None
    group_title: str | None = None
    # The SHOW's Plex rating key, for the card poster (a show always has one; many seasons
    # do not). Distinct from plex_rating_key, which is the season's, used for watch stats.
    poster_rating_key: int | None = None
    # How the show was bound to its Plex row (shared by every season of the show).
    matched_by: identity.MatchedBy | None = None
    match_detail: str | None = None
    match_status: identity.MatchStatus | None = None
    match_candidates: tuple[int, ...] = ()
    # Show-level display metadata shared by every season row: the Sonarr web-route
    # coordinate, certification, episode runtime, and the frozen ratings row. A season
    # has none of its own; the show's stand in. Display only, never a verdict input.
    title_slug: str | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    # The show's TVDb id (Sonarr's native id), stamped on every season row. Often the only
    # id a show and its Seerr request reliably share, so Scales joins on it (fairness).
    tvdb_id: int | None = None
    content_rating: str | None = None
    runtime_minutes: int | None = None
    # The Plex library (section) the show lives in, stamped on every one of its seasons.
    library: str | None = None
    ratings_json: str | None = None
    # Whether the show is finished ("ended" / "continuing" / "unknown"), from the same
    # observation the custom-rule field reads. See show_status_key for why "unknown" is
    # a value of its own.
    show_status: str | None = None


@dataclass(frozen=True, slots=True)
class PlexSeason:
    """One Plex season: its rating key and its OWN arrival date.

    The added-at is the season's, not the show's -- a season backfilled into an old show
    arrived recently, and dormancy must be measured from when the season's files landed,
    or a just-added season reads as decades dormant.
    """

    rating_key: int
    added_at: datetime | None


@dataclass
class _SeriesWork:
    """One series carried through the gather pipeline, accumulating what each pass learns.

    The plan is recomputed once watch evidence is available (the sequential and
    conflict guards need it); ``show_rating_key`` and ``seasons_in_plex`` are filled in by
    the Plex resolution pass, and stay empty for a series Plex could not match -- which is
    what makes every one of its seasons abstain.
    """

    source: SonarrSource
    series: dict[str, Any]
    seasons: list[SeasonStats]
    plan: SeriesPrunePlan
    # No season is prunable -- every one is kept by a guard. Still gathered and surfaced as
    # kept (never hide content), but spared the per-show episodes() read, which only feeds
    # mid-binge precision a fully-kept show cannot use.
    fully_protected: bool = False
    show_rating_key: int | None = None
    matched_by: identity.MatchedBy | None = None
    match_detail: str | None = None
    match_status: identity.MatchStatus | None = None
    # The Plex rows an abstain was choosing between; empty on any bind. Display only, and
    # shared by every season of the show, exactly like the three fields above it.
    match_candidates: tuple[int, ...] = ()
    # The matched Plex show's imdb id, used as a fallback for the IMDb rating lookup when
    # Sonarr's series imdbId is missing or wrong (common for reality/recent shows).
    plex_imdb_id: str | None = None
    seasons_in_plex: dict[int, PlexSeason] = field(default_factory=dict)
    season_final_episode: dict[int, int | None] = field(default_factory=dict)
    # Show-level display metadata from the matched Plex row, shared by every season of
    # the show (a season has no certification/ratings of its own). Display only.
    show_content_rating: str | None = None
    show_runtime_minutes: int | None = None
    show_plex_ratings: tuple[Rating, ...] = ()
    show_library: str | None = None


@dataclass
class SeasonWatchStats:
    """Per-season watch evidence, read from the local history mirror.

    Keyed by the season's *Plex rating key* (a season is a Plex item's ``parent`` from
    the episode's point of view), because that is what an episode play records.
    """

    last_played: dict[int, datetime] = field(default_factory=dict)
    watchers_window: dict[int, int] = field(default_factory=dict)
    watchers_all_time: dict[int, int] = field(default_factory=dict)
    user_season_keys: dict[int, set[int]] = field(default_factory=dict)
    """user_id -> the season rating keys that user has any episode play under. Mapped to
    season *numbers* per show to feed the sequential-progression guard."""

    user_season_progress: dict[int, dict[int, int]] = field(default_factory=dict)
    """user_id -> {season rating key -> highest COMPLETED episode number}. The episode-precise
    position for the mid-binge guard. Only rows with a known episode index and a completed
    watch; a season with only un-backfilled (NULL-index) rows is absent here, so the guard
    falls back to season-level protection for it."""

    user_season_last: dict[int, dict[int, datetime | None]] = field(default_factory=dict)
    """user_id -> {season rating key -> that user's most recent play under it, or ``None``
    when the stored timestamp cannot be read}. Rolled up per show, this is what expires an
    abandoned viewer's mid-binge hold (season_pruning.active_progress)."""


def season_media_key(instance_id: int, series_id: int, season_number: int) -> str:
    """``sonarr:1:42:3`` -- the four-part key the planner parses back into coordinates.

    Distinct from a whole-series key (three parts): season pruning acts on a season, and
    the extra segment is what routes it to the season delete path rather than a
    (nonexistent, refused) series delete.
    """
    return f"sonarr:{instance_id}:{series_id}:{season_number}"


def season_title(series_title: str, season_number: int) -> str:
    if season_number == SPECIALS_SEASON:
        return f"{series_title}, Specials"
    return f"{series_title}, Season {season_number}"


def season_requester(
    requested: dict[str, str],
    *,
    media_key: str,
    group_key: str,
    tvdb_id: int | None,
    season_number: int,
    show_rating_key: int | None,
) -> str | None:
    """The "requested by" name for one season row -- display only, never a gate.

    Best-first (requested_by.build_map): the EXACT copy where the operator mapped the Seerr
    service (this season's own ``media_key``, then the whole-show ``group_key``, which are by
    construction the precise keys); then this season's own loose tvdb key; then the show's Plex
    rating key; then the whole-show tvdb union.

    The season-precise tvdb key outranks the show-level rating key on purpose (B-10): Seerr
    stores a TV request's ratingKey at the SHOW level, so ranking it above the season key blurred
    "A asked S1, B asked S2" into "A + 1 other" on every season row. Rating-key still beats the
    whole-show union, so copy precision survives for a whole-show request.
    """
    return (
        requested.get(media_key)
        or requested.get(group_key)
        or requested.get(requested_by.season_key(tvdb_id, season_number) or "")
        or requested.get(requested_by.rating_key_key(show_rating_key) or "")
        or requested.get(requested_by.show_key(tvdb_id) or "")
    )


def parse_seasons(series: Mapping[str, Any]) -> list[SeasonStats]:
    """The season statistics for one Sonarr series, dropping any Sonarr cannot describe."""
    seasons: list[SeasonStats] = []
    for entry in series.get("seasons") or []:
        if isinstance(entry, dict):
            stats = parse_season_stats(entry)
            if stats is not None:
                seasons.append(stats)
    return seasons


def airing_seasons(series: Mapping[str, Any], seasons: list[SeasonStats]) -> set[int]:
    """The season(s) to treat as currently airing, protected outright.

    Deliberately conservative: for a series Sonarr still considers *running*, protect its
    latest content-bearing season. A season that is part of an active run must not be
    pruned mid-flight (Maintainerr #949), and over-protecting one season of a show that
    just ended costs nothing next to that.
    """
    status = str(series.get("status") or "").lower()
    ended = bool(series.get("ended", False))
    running = status == "continuing" or (status not in ("ended", "deleted") and not ended)
    if not running:
        return set()
    real = [
        s.season_number for s in seasons if s.season_number != SPECIALS_SEASON and s.has_content
    ]
    return {max(real)} if real else set()


def series_ended(series: Mapping[str, Any]) -> Observation[bool]:
    """Whether Sonarr considers the series finished -- three-state, for custom rules.

    ``Unknown`` when Sonarr reports no status at all, so an unreadable status can never be
    read as "ended" and add delete pressure. Mirrors the ``running`` logic in
    ``airing_seasons`` so the two never disagree.
    """
    status = str(series.get("status") or "").lower()
    if not status and "ended" not in series:
        return Unknown(reason=SERIES_STATUS_REASON, source="sonarr")
    ended = bool(series.get("ended", False))
    running = status == "continuing" or (status not in ("ended", "deleted") and not ended)
    return Known(value=not running, source="sonarr")


def show_status_key(ended: Observation[bool]) -> str | None:
    """The ended-ness observation, as the key stored on the candidate row.

    The single place the three-state fact becomes a wire value, so the queue, the panel
    and any future reader cannot drift apart on what "no status" looks like.

    ``Absent`` and ``Unknown`` deliberately map to different things, and collapsing them
    would be a lie in one direction or the other:

    * ``Absent`` -> ``None``. Nobody stamped a value because the question does not apply
      to this row (a movie is not a series). The UI shows nothing at all.
    * ``Unknown`` -> ``"unknown"``. This *is* a show and we could not read its status.
      That is a real thing to tell the owner, and it renders as "we could not check",
      never as a claim either way.

    ``Known(False)`` stores ``"continuing"``, but the operator-facing label is "Still
    going": the arm also covers a show that has not started yet, and "Continuing" would
    claim more than Sonarr said.
    """
    match ended:
        case Known(value=True):
            return "ended"
        case Known():
            return "continuing"
        case Unknown():
            return "unknown"
        case Absent():
            return None


def series_genres(series: Mapping[str, Any]) -> Observation[str]:
    """The show's genres, comma-joined. ``Absent`` when Sonarr recorded none."""
    genres = [str(g) for g in (series.get("genres") or []) if g]
    return Known(value=", ".join(genres), source="sonarr") if genres else Absent(source="sonarr")


def guard_result(plan: SeriesPrunePlan, season_number: int) -> GateResult:
    """Translate the season-pruning verdict for one season into a gate result.

    Three outcomes, mapped onto the gate vocabulary the why-panel already speaks:

    * **Protected by a guard** -> ``PROTECT``. Beats the score, like any gate.
    * **In a keep-rule conflict** (prunable by the rule, but more-watched than a season
      the rule keeps) -> a *blocked* ABSTAIN. ``blocked`` forces the whole item to
      abstain, which is exactly right: the rule is fighting the evidence, so a human must
      look. It renders amber, not green.
    * **Cleanly prunable** -> ABSTAIN, recorded so the panel shows the guard ran and had
      nothing to protect here.

    The conflict arm carries ``defers_to_owner``, and only where the comparison behind it
    was one Reaper could actually make. ``_detect_conflicts`` raises a conflict in three
    shapes:

    * the kept season's count was read and the rule lost it -- a comparison Reaper made;
    * that count could NOT be read (``kept_watchers is None`` -- on disk, but never resolved
      in Plex), a plumbing failure;
    * the watch mirror does not reach back to when one of the two seasons arrived
      (``shortfall``), so the count it reports for that season is a lower bound and more
      history could overturn the outcome either way.

    All three are blocked and all three send the item to a human. The last two are
    ``Unknown``, not a decision (rule 93): there is no comparison for the operator to
    *settle*, only evidence too thin to make one.

    **That distinction no longer decides a hand reap, and the flag is no longer an
    interlock.** A blocked gate does not hold a reap at all now -- see ``engine.verdict``
    -- so all three shapes are overrulable by hand, and the flag survives to pick what the
    operator is TOLD: the card's chip (``api.routes._chip``) and, across the wire through
    ``api.schemas.GateOutcomeOut``, the why panel's verdict note. Keeping the last two
    un-overrulable is exactly what made a short watch mirror refuse every TV reap on the
    server, which is the opposite of what "evidence too thin" should cost someone who can
    see the library themselves. Read off typed fields, never the wording (rule 142).
    """
    for protected in plan.protected:
        if protected.season_number == season_number:
            return GateResult(
                GateId.SEASON_PROGRESSION,
                GATE_PROTECT,
                detail=protected.reason,
                # A season held because the guard could not be ANSWERED is blocked as well
                # as protecting: `Evaluation.could_not_be_checked` selects on `blocked`
                # independently of the outcome, so the result rides in `protections_unknown`
                # and the panel shows it amber, "could not check", rather than green
                # "checked and passed" (rule 93). That is what `blocked` buys here.
                #
                # It no longer buys anything against a hand reap -- no blocked gate does
                # (`engine.verdict`) -- so the rule 143 argument this line was originally
                # added for has lapsed: PROTECT and blocked are now equally overrulable, and
                # only a FIRED structural gate refuses. The flag stays because the
                # Known/Absent/Unknown distinction is true and the operator is entitled to
                # see which one this is, which was always the better reason.
                blocked=protected.unestablishable,
            )

    # EVERY conflict naming this season, not just the first. ``_detect_conflicts`` raises
    # one per (pruned, kept) pair, so a single pruned season routinely carries more than one
    # shape at once -- on shipped defaults, a kept newest season still resolving in Plex
    # conflicts with every watched prunable season below it, while an older kept season's
    # count reads fine. A short mirror mixes them the same way: it truncates the seasons
    # that predate the horizon and leaves a recently-added one exact.
    matching = [c for c in plan.conflicts if c.pruned_season == season_number]
    if matching:
        # A refused comparison wins, and it decides the message as well as the flag.
        # Reading only the first conflict let a readable one mask an unread one, so the
        # operator saw only the comparison that HAD been made and nothing ever told them
        # one had not. That is now a reporting bug rather than a reap bug -- the reap is
        # theirs either way -- but it is the same bug: the sentence and the flag must come
        # from the same conflict (rule 92), and the season nobody could read is the one
        # worth putting in front of them.
        #
        # Both non-comparisons count as refused, and for the same reason: a count nobody
        # could take and a count taken over a mirror that cannot support it are equally
        # unable to settle "is this watched more than the season you keep", so neither may
        # be reported as a comparison Reaper made.
        refused = next(
            (c for c in matching if c.kept_watchers is None or c.shortfall is not None), None
        )
        conflict = refused or matching[0]
        return GateResult(
            GateId.SEASON_PROGRESSION,
            GATE_ABSTAIN,
            blocked=True,
            detail=conflict.message,
            defers_to_owner=refused is None,
        )

    return GateResult(
        GateId.SEASON_PROGRESSION,
        GATE_ABSTAIN,
        detail="checked: prunable by the keep-last / keep-first season rules",
    )


#: The show-side twin of ``snapshot._NO_KEY_REASONS``: why this season has no Plex rating
#: key, one entry per non-matched resolver outcome. Same contract -- each value is a key
#: into ``WhyPanel``'s ``CAUSE_COPY``, and
#: ``test_review_chips.py::TestTheMatchStatusVocabulary`` fails on one with no entry
#: there. Kept beside its own builder rather than shared with the movie map because the
#: subjects differ ("this season" against "this item", "this show" against "this title").
_NO_KEY_REASONS: dict[identity.MatchStatus | None, str] = {
    identity.MatchStatus.UNMATCHED: "Plex has not matched this season",
    identity.MatchStatus.AMBIGUOUS: "more than one Plex item matches this show",
    identity.MatchStatus.CONFLICTED: "Plex and Sonarr describe this show differently",
}

#: The season twin of ``snapshot.NO_ADDED_AT_REASON``, and the same contract: a key into
#: ``CAUSE_COPY``, named so the drift test covers it (rule 144). Its own wording, for the
#: reason the map above keeps its own -- the subject is "this season", not "this item".
NO_ADDED_AT_REASON = "no added-at date for this season"

#: Why a season's size is unreadable: Sonarr reported no size on disk. Reaches the panel
#: through a keep rule on "Size on disk"; the movie lane says it of a file, so the two are
#: named apart (rule 144).
NO_SIZE_REASON = "the season's size was not reported"

#: Why "has the show ended?" is unreadable: Sonarr sent neither a status nor an ended flag.
SERIES_STATUS_REASON = "Sonarr did not report series status"


def build_season_facts(
    *,
    title: str,
    season: SeasonStats,
    rank: int | None,
    plex_rating_key: int | None,
    season_added_at: datetime | None,
    horizon: datetime,
    reach_days: int,
    last_played: datetime | None,
    watchers_window: int | None,
    watchers_all_time: int | None,
    active_rating_keys: set[int],
    activity_degraded: bool,
    whitelisted: bool,
    curated: list[lists.Membership],
    imdb_rating: ImdbRating | None = None,
    # Whether the show carried an IMDb id to look a rating up with. `imdb_rating=None`
    # alone cannot tell "this show is unrated" from "we never asked", and those are
    # opposite instructions to the keep lane. Defaults to the fail-closed reading: a
    # caller that does not say keeps fully.
    rating_looked_up: bool = False,
    # Whether the IMDb dataset could be read AT ALL this scan. A third state on top of
    # `rating_looked_up`: the show may well carry an imdb id (looked_up True) and still
    # have never been asked about, because the data was missing or stale. Defaults False,
    # which with `rating_looked_up`'s False default still lands a silent caller on
    # Unknown -- the reading that keeps fully.
    rating_dataset_degraded: bool = False,
    plex_ratings: tuple[Rating, ...] = (),
    requested: Observation[bool] = _UNSET_OBS,
    show_ended: Observation[bool] = _UNSET_OBS,
    genres: Observation[str] = _UNSET_OBS,
    show_match_status: identity.MatchStatus | None = None,
    # Set when this season measured fewer plays than it has measured before, which a
    # library cannot do -- see ``services.watch_evidence``. The season's history is read by
    # its Plex ``parent_rating_key``, and a re-added season carries a new one while its
    # earlier plays stay filed under the old, so "no rows" is ambiguous between churn and a
    # season nobody watched. When set, dormancy and both watcher counts below are Unknown
    # instead of a measured zero, exactly as on the movie path (rule 72).
    watch_blind_reason: str | None = None,
) -> Facts:
    """Assemble one season's evidence, with the same Unknown-discipline as the movie path.

    A season we could not resolve in Plex (``plex_rating_key is None``) has no watch
    history to read, so its dormancy and popularity are ``Unknown`` -- and Unknown, run
    through the gates, abstains. A file we cannot see is never condemned.
    ``show_match_status`` picks the honest wording for that Unknown: an AMBIGUOUS show
    (two Plex items share its id) is a different story from one Plex has no match for, and
    a CONFLICTED one (each kind of evidence named a different row, so Plex and Sonarr
    describe the show differently) is a third. The why-panel must not tell the owner the
    wrong one, so the wording comes from :data:`_NO_KEY_REASONS` rather than a ternary that
    lumps every new outcome in with "not matched".
    """
    dormancy: Observation[float]
    recent: Observation[int]
    all_time: Observation[int]
    streaming: Observation[bool]

    if plex_rating_key is None:
        no_key_reason = _NO_KEY_REASONS.get(show_match_status, "Plex has not matched this season")
        dormancy = Unknown(reason=no_key_reason, source="plex")
        recent = Unknown(reason=no_key_reason, source="plex")
        all_time = Unknown(reason=no_key_reason, source="plex")
        streaming = Unknown(reason=no_key_reason, source="plex")
        if show_match_status is identity.MatchStatus.MATCHED:
            # The show bound to Plex, but this content-bearing season did not: Plex has no
            # matching season (one it has not scanned yet, or a split/duplicate "Season n"
            # that seasons_from_rows dropped as ambiguous). Its facts are Unknown, so it
            # abstains and appears only as "kept to be safe", never on the reap list. The
            # whole-show miss is warned once at resolve time (scan.plex_unmatched, above);
            # this names the per-season gap so "why is this season kept" is answerable from
            # the log. Same event, keyed by media_type. Only a MATCHED show warns here: an
            # unmatched or ambiguous show already logged its miss once, and re-logging it
            # per season would flood the log for one unresolved show.
            log.warning(
                "scan.plex_unmatched",
                media_type="season",
                title=title,
                season=season.season_number,
                match_status="unmatched",
                detail=no_key_reason,
            )
    else:
        # Dormancy is measured from THIS SEASON's own arrival date, never the show's -- a
        # season backfilled into an old show arrived recently even though the show is old,
        # and using the show's date would read a just-added season as decades dormant and
        # condemn a file nobody has had a chance to watch. This mirrors the movie path,
        # which floors on each item's own added_at. Through the one shared derivation
        # (engine/dormancy.py), the same one the movie scan, the backtest and the prior
        # calibration use (rule 3); we never fabricate a Known dormancy from the horizon.
        #
        # A play is enough on its own: with no arrival date but a play in scope
        # `reference_instant` measures from that play and the season is judged. The movie path
        # takes the same branch as of #272 -- the thaw for a record missing `added_at` moved
        # into `engine/dormancy.py`, so this lane no longer has to force it by passing the
        # play as the arrival date, and the both-missing case is one shared answer rather than
        # a matching arm in each caller.
        if watch_blind_reason is not None:
            # Before the measurement, and it has to stay there, for the same reason as the
            # movie path: a re-added season carries a fresh added_at while its earlier plays
            # stay filed under the key it no longer holds, so the measurement would read a
            # confident, tiny dormancy off the one input that still looks readable when the
            # plays behind it are not.
            dormancy = Unknown(reason=watch_blind_reason, source="tautulli")
        elif (
            reference := reference_instant(
                last_played=last_played, added_at=season_added_at, horizon=horizon
            )
        ) is None:
            # Matched to a Plex season, but no arrival date and no play history, so dormancy
            # cannot be measured and the season abstains: kept to be safe, never reaped. Warn
            # so "why isn't this season reapable" is answerable from the log, the same as the
            # movie path. Rare: a matched Plex season almost always carries an added_at.
            log.warning(
                "scan.no_added_at",
                media_type="season",
                title=title,
                season=season.season_number,
                plex_rating_key=plex_rating_key,
            )
            dormancy = Unknown(reason=NO_ADDED_AT_REASON, source="tautulli")
        else:
            dormancy = Known(value=dormancy_days(reference, now=utcnow()), source="tautulli")

        if watch_blind_reason is not None:
            recent = Unknown(reason=watch_blind_reason, source="tautulli")
            all_time = Unknown(reason=watch_blind_reason, source="tautulli")
        else:
            recent = Known(value=watchers_window or 0, source="tautulli")
            all_time = Known(value=watchers_all_time or 0, source="tautulli")
        if activity_degraded:
            streaming = Unknown(reason=watch_evidence.NO_SESSIONS_REASON, source="tautulli")
        else:
            streaming = Known(value=plex_rating_key in active_rating_keys, source="tautulli")

    curated_names = ", ".join(m.describe() for m in curated)
    in_curated: Observation[str] = (
        Known(value=curated_names, source="lists") if curated else Absent(source="lists")
    )

    # The multi-source keep gate reads this. TV has no Radarr-style ratings object, so it
    # is the show's IMDb dataset value (applied to each season, like the single-source
    # rating floor above) plus whatever Plex carries for the show (which may add TMDb or a
    # Rotten Tomatoes score, depending on the Plex agent). The dataset value wins for IMDb.
    dataset_rating = (
        [
            Rating(
                source=RatingSource.IMDB,
                value=imdb_rating.average_rating,
                votes=int(imdb_rating.num_votes),
                provider="imdb-dataset",
            )
        ]
        if imdb_rating is not None
        else []
    )
    rating_set = merge_by_source(dataset_rating, list(plex_ratings))

    return Facts(
        title=title,
        days_observed_unwatched=dormancy,
        distinct_watchers=recent,
        distinct_watchers_all_time=all_time,
        # The movie lane's twin (``snapshot.build_facts``, rule 72): the same mirror backs
        # both counts, so both must say how far back it reaches -- and from the same
        # instant, which is why this is passed in rather than measured here
        # (``snapshot.ScanContext.reach_days``).
        history_reach_days=Known(value=reach_days, source="tautulli"),
        # The span an all-time count would need, from THIS season's own arrival date --
        # the same reason dormancy uses it above, a season backfilled into an old show
        # arrived recently. The movie lane's twin (``snapshot.build_facts``, rule 72).
        days_since_added=(
            Known(value=dormancy_days(season_added_at, now=utcnow()), source="plex")
            if season_added_at is not None
            else Unknown(reason=NO_ADDED_AT_REASON, source="plex")
        ),
        size_bytes=(
            Known(value=season.size_on_disk, source="sonarr")
            if season.size_on_disk is not None
            else Unknown(reason=NO_SIZE_REASON, source="sonarr")
        ),
        # Sonarr's own ratings are flat TVDB, but the IMDb dataset we already ingest carries
        # a rating for the *series* (keyed by its imdbId). We apply the show's rating to each
        # of its seasons -- a season has no distinct IMDb title -- so a well-rated show's
        # seasons get the same rating-floor protection a well-rated film does.
        #
        # The two no-rating cases are NOT the same and must not collapse. Unrated is
        # `Absent`, which withdraws a rating keep, correctly. No id to look one up with is
        # `Unknown`, which keeps fully: recording it as `Absent` would claim we checked.
        # The movie path draws the same line (snapshot.build_facts, display_meta
        # .dataset_lookup); see tests/test_fact_layer_states.py.
        imdb_rating_tenths=_rating_obs(
            imdb_rating.average_rating * 10 if imdb_rating else None,
            rating_looked_up,
            rating_dataset_degraded,
        ),
        imdb_votes=_rating_obs(
            imdb_rating.num_votes if imdb_rating else None,
            rating_looked_up,
            rating_dataset_degraded,
        ),
        # A rank of None here is not an outage: rank_seasons deliberately leaves specials
        # (and content-less seasons, already filtered out before this point) out of the
        # newest->oldest ranking, so the only season reaching this branch with no rank is a
        # special. We looked, and it genuinely has no rank slot -- that is Absent, not
        # Unknown. Recording it as Unknown told the owner Sonarr could not be read and made
        # the SEASON_RANK signal read "could not tell which season this is", dragging the
        # special's coverage down for a rank it was never meant to have. See
        # engine.signals.evaluate_signal, which reads this Absent as NOT_APPLICABLE.
        season_rank=(
            Known(value=rank, source="sonarr") if rank is not None else Absent(source="sonarr")
        ),
        is_streaming_now=streaming,
        is_managed=Known(value=True, source="sonarr"),
        in_curated_list=in_curated,
        is_whitelisted=Known(value=whitelisted, source="lists"),
        # --- fields authorable in custom rules ---------------------------------
        requested=requested,
        genres=genres,
        # No clean per-season release date, and a season mixes episode qualities, so both
        # are Absent for seasons in v1 -- never condemn, never protect (the movie/season
        # not-applicable precedent).
        release_age_days=Absent(source="sonarr"),
        quality=Absent(source="sonarr"),
        show_ended=show_ended,
        ratings=rating_set,
    )


# ---------------------------------------------------------------------------
# Orchestration -- reads live clients, but every branch fails closed.
# ---------------------------------------------------------------------------


def _as_year(value: Any) -> int | None:
    """A show's release year, or ``None`` -- used only to disambiguate duplicate titles."""
    if isinstance(value, int | str) and str(value).isdigit():
        return int(value)
    return None


async def build_tv_index(
    tautulli: TautulliClient,
    plex: PlexClient | None,
    *,
    degrade: Any,
    allowed_sections: set[int] | None = None,
) -> identity.PlexIndex:
    """The Plex show library, inverted for id / basename / title matching.

    One shared implementation with ``snapshot.build_movie_index`` -- see
    ``services.library_index`` for the spine + sweep design and its failure semantics.
    ``allowed_sections`` scopes the read to the show libraries the operator included in
    scans (``None`` = all). A show's own added_at is not used for dormancy, which is
    measured per season.
    """
    return await library_index.build_index(
        tautulli, plex, section_type="show", degrade=degrade, allowed_sections=allowed_sections
    )


def seasons_from_rows(
    rows: Iterable[tuple[Any, Any, Any]],
) -> dict[int, PlexSeason]:
    """season number -> its Plex season, from ``(media_index, rating_key, added_at)`` rows.

    The one place the season-number ambiguity policy lives, shared by the Plex season sweep
    and the per-show Tautulli fallback so the two can never drift. A season number that
    appears **twice** (a split or mis-scanned library can emit two "Season N" items) is
    dropped entirely rather than bound to whichever rating key sorts last -- picking one
    risks reading an empty duplicate's history for a season people actually watched. An
    ambiguous season, like an ambiguous show, abstains. ``from_epoch`` parses ``added_at``
    whether it arrives as Tautulli's epoch int or Plex's epoch string.
    """
    result: dict[int, PlexSeason] = {}
    ambiguous: set[int] = set()
    for index, rk, added in rows:
        if index is None or rk is None:
            continue
        try:
            n, key = int(index), int(rk)
        except (TypeError, ValueError):
            continue
        if n in ambiguous:
            continue
        if n in result:
            del result[n]  # a second "Season n" -> drop both and fail closed
            ambiguous.add(n)
            continue
        result[n] = PlexSeason(rating_key=key, added_at=from_epoch(added))
    return result


async def resolve_season_keys(
    tautulli: TautulliClient, show_rating_key: int
) -> dict[int, PlexSeason]:
    """season number -> its Plex season (rating key + own added-at), for one show.

    The per-show fallback: the season sweep (``PlexClient.library_season_index``) resolves
    every show in a handful of paged reads, and this covers only a show that sweep did not
    return. Resolved from ``get_children_metadata``; a season we cannot find here simply has
    no key, so its facts go Unknown and it abstains. Returns an empty map on any read failure
    rather than raising -- one show that will not resolve is not a reason to abort the whole
    scan, and an empty map is the fail-closed outcome (every season abstains).
    """
    try:
        children = await tautulli.children_metadata(show_rating_key)
    except IntegrationError as exc:
        log.warning("season_scan.children_failed", show=show_rating_key, error=str(exc))
        return {}
    return seasons_from_rows(
        (child.get("media_index"), child.get("rating_key"), child.get("added_at"))
        for child in children
    )


async def season_watch_stats(
    engine: AsyncEngine, season_keys: set[int], *, window_days: int
) -> SeasonWatchStats:
    """Read per-season watch evidence for a batch of season rating keys, in two queries.

    Episodes carry ``parent_rating_key`` = the season's Plex key, so a season's plays are
    every episode play under it. Kept as one batched read across all shows being pruned,
    rather than a query per show, because a large library prunes many shows at once.
    """
    stats = SeasonWatchStats()
    if not season_keys:
        return stats

    # The cache is rebuildable and may be empty on a fresh install. Ensure the table exists so
    # a never-synced cache reads as "no plays" rather than crashing the scan with 'no such
    # table'. What makes that fail-closed is NOT the dormancy observation -- a zero-row mirror
    # resolves the horizon to `utcnow()`, so a season with an arrival date reads Known ZERO
    # days dormant, not Unknown. The hold is `snapshot.scan` degrading the snapshot
    # un-plannably on that same empty mirror; see the twin note in `snapshot._watch_stats`.
    #
    # Every reader of this mirror that a scan can reach carries the guard (rule 72):
    # `snapshot._watch_stats` and `fairness._evidence_index` call it directly, and
    # `snapshot._fold_merged_watch_stats` / `fairness._distinct_episodes` inherit it by running
    # after a guarded sibling over a subset of its keys. `backtest._plays` and
    # `calibration.derive` do NOT have it, and have no route, CLI or schedule reaching them.
    # Deferred rather than swept (rule 72, issue #283) for a reason worth writing down: both
    # sit in `engine/`, which imports nothing from `services/`, so the one-line fix its four
    # siblings took is not available there. Whoever wires a backtest route calls this from the
    # route, where the layering allows it; docs/STATUS.md open item 3 carries that obligation.
    # Today the season task also always runs after `scan()` has touched the mirror, so the
    # table exists; this does not depend on that ordering holding.
    await history_sync.ensure_schema(engine)

    # Unclamped by the horizon, like the movie twin (`snapshot._watch_stats`): the clamp
    # would move no count, because `watch_event` itself begins at the horizon. That is also
    # why neither count here is an answer on its own -- both are LOWER BOUNDS, and every
    # consumer takes the reach alongside them (rule 140). `watchers_window` rides
    # `Facts.history_reach_days` to the shared popularity gate and the operator's own rules;
    # `watchers_all_time` rides it there too, and to the keep-rule conflict detector as
    # `shortfall_by_season` (`_judge_series`), which reads no `Facts` at all and so had to
    # be handed the bound directly. Naming only the first of those was how the second went
    # a release comparing two truncated counts at full confidence (#94).
    since = int((utcnow() - timedelta(days=window_days)).timestamp())
    keys = sorted(season_keys)

    aggregate = text(
        "SELECT parent_rating_key AS k, MAX(watched_at) AS last, "
        "  COUNT(DISTINCT user_id) AS all_w, "
        "  COUNT(DISTINCT CASE WHEN watched_at >= :since THEN user_id END) AS win_w "
        "FROM watch_event "
        "WHERE parent_rating_key IN :keys AND media_type = 'episode' "
        "GROUP BY parent_rating_key"
    ).bindparams(bindparam("keys", expanding=True))

    # One row per (user, season) with that user's most recent play under it -- the same
    # coverage the old DISTINCT pair query had, plus the timestamp the mid-binge expiry
    # needs (season_pruning.active_progress).
    pairs = text(
        "SELECT parent_rating_key AS k, user_id AS u, MAX(watched_at) AS last "
        "FROM watch_event "
        "WHERE parent_rating_key IN :keys AND media_type = 'episode' "
        "GROUP BY parent_rating_key, user_id"
    ).bindparams(bindparam("keys", expanding=True))

    # Episode-precise position: each user's highest COMPLETED episode per season. NULL-index
    # rows (movies, or pre-backfill TV) are excluded, so a season with only those is simply
    # absent here and the guard falls back to season-level protection for it.
    #
    # `max_unknown` is the highest episode whose completion Tautulli never reported. If it
    # sits ABOVE the highest known-completed one, the viewer may be further along than the
    # position says, and being wrong in that direction unprotects the season they are about
    # to watch next: `sequential_protections` reads "finished season m" as ready-for-m+1 and
    # anything less as still-on-m, and the default lookahead is 0, so there is no cushion.
    # Those pairs are dropped below, which makes the position Unknown and fails the guard
    # closed to season level, exactly as a season with no episode indexes at all does.
    progress = text(
        "SELECT user_id AS u, parent_rating_key AS k, "
        "       MAX(CASE WHEN watched_status = 1 THEN media_index END) AS max_ep, "
        "       MAX(CASE WHEN watched_status IS NULL THEN media_index END) AS max_unknown "
        "FROM watch_event "
        "WHERE parent_rating_key IN :keys AND media_type = 'episode' "
        "  AND media_index IS NOT NULL "
        "GROUP BY user_id, parent_rating_key"
    ).bindparams(bindparam("keys", expanding=True))

    async with engine.connect() as conn:
        # Chunked like imdb_dataset.lookup: the ``expanding`` bindparam turns every key
        # into one bound variable, and a very large library can exceed SQLite's limit.
        # Chunks are disjoint keys, so accumulating across them is exact.
        for chunk in batched(keys, 500, strict=False):
            key_chunk = list(chunk)
            rows = (await conn.execute(aggregate, {"keys": key_chunk, "since": since})).all()
            for row in rows:
                key = int(row.k)
                when = from_epoch(row.last)
                if when is not None:
                    stats.last_played[key] = when
                stats.watchers_all_time[key] = int(row.all_w or 0)
                stats.watchers_window[key] = int(row.win_w or 0)
            for row in (await conn.execute(pairs, {"keys": key_chunk})).all():
                user, key = int(row.u), int(row.k)
                stats.user_season_keys.setdefault(user, set()).add(key)
                # from_epoch returns None for an unreadable timestamp; kept as None so the
                # expiry treats that viewer as still active rather than silently stale.
                stats.user_season_last.setdefault(user, {})[key] = from_epoch(row.last)
            for row in (await conn.execute(progress, {"keys": key_chunk})).all():
                if row.max_ep is None:
                    continue  # nothing completed here: position unknown, guard falls back
                if row.max_unknown is not None and int(row.max_unknown) > int(row.max_ep):
                    continue  # they may be further on than this; see the query note
                stats.user_season_progress.setdefault(int(row.u), {})[int(row.k)] = int(row.max_ep)

    return stats


def _progress_by_user(
    stats: SeasonWatchStats, season_key_to_number: Mapping[int, int]
) -> dict[str, dict[int, int | None]]:
    """For one show, each viewer's per-season position: season number -> highest completed
    episode, or ``None`` when they touched the season but we have no episode index for it.

    The anchor is every season the viewer has any play under (``user_season_keys``); a
    ``None`` value means "position unknown for this season" and drops the guard to its
    season-level fallback there. Scoped to this show's keys, so progress in another series
    does not leak in.
    """
    show_keys = set(season_key_to_number)
    result: dict[str, dict[int, int | None]] = {}
    for user_id, keys in stats.user_season_keys.items():
        per_season: dict[int, int | None] = {}
        progressed = stats.user_season_progress.get(user_id, {})
        for key in keys & show_keys:
            per_season[season_key_to_number[key]] = progressed.get(key)
        if per_season:
            result[str(user_id)] = per_season
    return result


def _last_play_by_user_season(
    stats: SeasonWatchStats, season_key_to_number: Mapping[int, int]
) -> dict[str, dict[int, datetime | None]]:
    """For one show, each viewer's last play of each season: season number -> when.

    The recency half of the sequential guard's anchor (season_pruning
    .sequential_protections). ``None`` for a season whose timestamp is unreadable, so the
    anchor can skip it rather than treat an unknown time as old. Scoped to this show's
    keys, exactly like :func:`_progress_by_user`.
    """
    show_keys = set(season_key_to_number)
    result: dict[str, dict[int, datetime | None]] = {}
    for user_id, keys in stats.user_season_keys.items():
        per_key = stats.user_season_last.get(user_id, {})
        per_season: dict[int, datetime | None] = {
            season_key_to_number[key]: per_key.get(key) for key in keys & show_keys
        }
        if per_season:
            result[str(user_id)] = per_season
    return result


def _last_watched_by_user(
    stats: SeasonWatchStats, season_key_to_number: Mapping[int, int]
) -> dict[str, datetime | None]:
    """For one show, each viewer's most recent play of ANY of its seasons.

    ``None`` when any of that viewer's per-season timestamps is unreadable: the unreadable
    one could be their most recent, so the expiry must treat the viewer as still active
    (season_pruning.active_progress holds on ``None``) rather than judge them stale from a
    partial view. Scoped to this show's keys, exactly like ``_progress_by_user``.
    """
    show_keys = set(season_key_to_number)
    result: dict[str, datetime | None] = {}
    for user_id, keys in stats.user_season_keys.items():
        relevant = keys & show_keys
        if not relevant:
            continue
        per_key = stats.user_season_last.get(user_id, {})
        times: list[datetime] = []
        for key in relevant:
            when = per_key.get(key)
            if when is None:
                times.clear()
                break
            times.append(when)
        result[str(user_id)] = max(times) if times else None
    return result


def _final_episodes(episodes: list[dict[str, Any]]) -> dict[int, int | None]:
    """{season number -> highest ON-DISK episode number}, from Sonarr's episode list.

    Uses ``hasFile``, never ``episodeCount`` (Sonarr's download intent) or
    ``totalEpisodeCount`` (which counts unaired episodes, so a season could never be
    "finished"), so gaps and unaired episodes do not mislead the finished check.
    """
    final: dict[int, int | None] = {}
    for episode in episodes:
        if not episode.get("hasFile"):
            continue
        try:
            season = int(episode["seasonNumber"])
            number = int(episode["episodeNumber"])
        except (KeyError, TypeError, ValueError):
            continue
        current = final.get(season)
        if current is None or number > current:
            final[season] = number
    return final


def _keep_last_applies(
    series: Mapping[str, Any],
    keep_last_scope: str,
    request_index: requested_by.RequestIndex | None,
) -> bool:
    """Whether the keep-last floor applies to this show under the scope.

    Fail-closed under a "requested only" scope: apply keep-last unless we KNOW the show was
    not requested (Unknown counts as "might be requested").
    """
    if keep_last_scope != "requested":
        return True
    tvdb_id = int(series["tvdbId"]) if series.get("tvdbId") else None
    requested = request_index.show_requested(tvdb_id) if request_index is not None else None
    return not (isinstance(requested, Known) and requested.value is False)


def _season_digest(seasons: list[SeasonStats]) -> list[dict[str, Any]]:
    """A compact per-season view of what Sonarr reported -- the raw facts behind the prune
    decision. One entry per season: its number, how many episodes are on disk, and whether
    Sonarr is still filling it. Read straight off the ``season_scan.series_decision`` line so
    "why is this show kept" is answerable without re-reading Sonarr."""
    return [
        {"n": s.season_number, "files": s.episode_file_count, "incomplete": s.is_incomplete}
        for s in sorted(seasons, key=lambda s: s.season_number)
    ]


def _log_series_decision(
    source: SonarrSource,
    series: Mapping[str, Any],
    seasons: list[SeasonStats],
    *,
    outcome: str,
    plan: SeriesPrunePlan | None,
) -> None:
    """One greppable DEBUG line per series: what Sonarr reported, and why the show did or did
    not become a candidate.

    The single record to grep by title when a show is not in the queue. ``outcome`` is
    ``candidate`` (has a prunable season, so it is judged), ``no_content`` (no season holds
    files, nothing to reap) or ``fully_protected`` (every on-disk season is kept by a guard).
    Plex binding happens after this offline decision, so match status is logged separately
    (``scan.plex_matched`` / ``scan.plex_unmatched``); an unmatched show still becomes a
    candidate and is never dropped here.

    **This is the OFFLINE pass, and ``prunable`` here is not the final answer.** The claim
    that used to sit here -- that every prunable and protected season is named with its
    reason, so "why is this season kept" is answerable without re-running the scan -- is
    false for every guard that needs watch evidence, because none of it exists yet at this
    call site. ``plan_series_prune`` runs again in ``_judge_series`` with the mirror's reach,
    the mid-binge hold (``progress_established``) and the keep-rule conflict's per-season
    shortfalls, and a season listed ``prunable`` here is routinely held there. On a mirror
    shallower than the library that is not an edge case but the normal outcome. So this line
    answers "what did Sonarr say, and did the show reach the evidence pass", and nothing
    about a season's fate; the authoritative record is the stored explanation behind the
    panel. Narrowed rather than fixed because the line is developer-facing only -- a repo-wide
    grep finds ``season_scan.series_decision`` named in no operator doc, UI string or support
    text (rules 7/24, 132).
    """
    log.debug(
        "season_scan.series_decision",
        instance_id=source.instance_id,
        instance=source.name,
        title=str(series.get("title") or "?"),
        sonarr_id=series.get("id"),
        tvdb_id=series.get("tvdbId") or None,
        imdb_id=series.get("imdbId") or None,
        status=series.get("status") or None,
        ended=series.get("ended"),
        outcome=outcome,
        seasons=_season_digest(seasons),
        prunable=list(plan.prunable) if plan is not None else [],
        protected=(
            [{"season": p.season_number, "reason": p.reason} for p in plan.protected]
            if plan is not None
            else []
        ),
    )


async def gather(
    engine: AsyncEngine,
    *,
    sonarrs: list[SonarrSource],
    tautulli: TautulliClient,
    plex: PlexClient | None = None,
    horizon: datetime,
    reach_days: int,
    active_rating_keys: set[int],
    activity_degraded: bool,
    keep_last_seasons: int,
    keep_first_season: bool,
    window_days: int,
    whitelisted: set[str],
    degrade: Any,
    requested: dict[str, str] | None = None,
    request_index: requested_by.RequestIndex | None = None,
    keep_last_scope: str = "all",
    season_lookahead: int = 0,
    keep_in_progress: bool = True,
    # Mirrors ``TvPolicy.in_progress_hold_days``'s own default, which is what the only
    # production caller passes (``services.snapshot.scan``). It read 0 here, a value no
    # shipped policy has, and 0 means the hold never expires -- so every caller that omitted
    # it was exercising an unbounded claim the mirror cannot support (rule 141).
    in_progress_hold_days: int = 180,
    keep_specials: bool = True,
    protect_incomplete_seasons: bool = True,
    flag_keep_conflicts: bool = True,
    membership_index: lists.MembershipIndex | None = None,
    allowed_sections: set[int] | None = None,
    # The most watch evidence ever measured for each item, read once by the caller (which
    # holds the session; this task does not). Handed in whole rather than filtered because a
    # season ``media_key`` is derived here, so there is nothing to filter on until it is too
    # late. An empty map is honest -- no marks means nothing can have fallen -- which is
    # exactly why this is REQUIRED and carries no default: omitting it is byte-identical to a
    # first scan, so the TV half of the guard would cover no season at all while the code, the
    # log line and the Settings panel all still read as live. mypy is the gate that catches the
    # omission, because no test can (rule 118).
    watch_marks: Mapping[str, watch_evidence.Mark],
) -> list[SeasonJudgment]:
    """Gather the seasons of every show with content on disk, ready to judge.

    Read-only. Reads Sonarr for series and their season statistics, runs the guards to
    find prunable seasons, resolves each show with content against Plex, reads their watch
    history from the local mirror, and returns a ``SeasonJudgment`` per content-bearing
    season. A show whose every season is guard-kept is surfaced as kept, not dropped, so
    content is never hidden from the UI; only a show with nothing on disk (``no_content``)
    is left out, because it has no season to show.

    ``degrade`` is the snapshot's degrade callback: an unreadable Sonarr marks the
    snapshot degraded (no run may execute against it) exactly as a missing Radarr does.
    """
    if not sonarrs:
        return []

    # The scan passes its already-loaded index so movies and seasons read the same
    # frozen list state; a direct caller (tests) gets a fresh load.
    if membership_index is None:
        membership_index = await lists.load_membership_index(engine)

    async def _series_from(source: SonarrSource) -> list[dict[str, Any]] | None:
        try:
            series: list[dict[str, Any]] = await source.client.series()
        except IntegrationError as exc:
            degrade(f"sonarr '{source.name}' unreachable: {exc}")
            return None
        return series

    async def _roots_from(source: SonarrSource) -> tuple[str, ...] | None:
        """This instance's root folder paths, or ``None`` if they could not be read.

        Read once per instance, never per show. ``None`` is not ``()``: an instance that
        reports no roots is answering, while a failed read is not, and
        :func:`identity._narrow_among_id_hits` refuses to narrow an ambiguous id at all on
        ``None`` -- because losing the roots also removes the folder-vs-size contradiction
        veto, not just the folder's ability to bind.

        A failure here does NOT degrade the snapshot, unlike an unreadable series list
        above. That is a deliberate exception to rule 28, whose compensating control is the
        refusal named above: every affected show is kept.
        """
        try:
            folders = await source.client.root_folders()
        except IntegrationError as exc:
            log.warning("season_scan.rootfolders", instance=source.name, error=str(exc))
            return None
        return identity.root_folder_paths(folders)

    # The show index, each Sonarr's series list and each Sonarr's root folders live on
    # different services, so they are fetched concurrently -- the same shape as the movie
    # fan-out in snapshot.scan, with the same reap-on-failure discipline.
    tv_index, *per_source = await gather_reaped(
        build_tv_index(tautulli, plex, degrade=degrade, allowed_sections=allowed_sections),
        *(_series_from(source) for source in sonarrs),
        *(_roots_from(source) for source in sonarrs),
    )
    series_lists = per_source[: len(sonarrs)]
    roots_by_instance: dict[int, tuple[str, ...] | None] = {
        source.instance_id: roots
        for source, roots in zip(sonarrs, per_source[len(sonarrs) :], strict=True)
    }

    # First pass, pure and offline: decide prunable/protected per series from Sonarr's own
    # season statistics, logging one decision line per series (season_scan.series_decision,
    # below). Every show with content on disk is gathered and resolved against Plex -- one with
    # no prunable season is surfaced as kept, not dropped, so content is never hidden from the UI.
    work: list[_SeriesWork] = []
    fully_protected: list[str] = []
    no_content: list[str] = []
    for source, series_list in zip(sonarrs, series_lists, strict=True):
        if series_list is None:
            continue  # unreachable instance -- already degraded above, with the reason
        # How many series this Sonarr returned, the twin of snapshot.radarr's movie count. A
        # show absent from this instance's per-series decision lines but present in Sonarr means
        # the read was short; a show in neither means it is not on this instance at all -- the
        # line that tells "not in the queue" apart from "not in this Sonarr / not scanned".
        log.info("season_scan.sonarr", instance=source.name, series=len(series_list))
        for series in series_list:
            seasons = parse_seasons(series)
            if not any(s.has_content for s in seasons):
                no_content.append(str(series.get("title") or "?"))
                _log_series_decision(source, series, seasons, outcome="no_content", plan=None)
                continue
            plan = plan_series_prune(
                series_title=str(series.get("title") or ""),
                seasons=seasons,
                keep_last=keep_last_seasons,
                keep_first_season=keep_first_season,
                apply_keep_last=_keep_last_applies(series, keep_last_scope, request_index),
                # keep_specials must reach this offline pass too: with it off, a show whose
                # only removable season is Season 0 has something to act on and must not be
                # counted fully-protected before the evidence pass ever sees it. The other
                # toggles are passed for symmetry; without watch evidence the sequential
                # guard and the conflict detector protect nothing here either way.
                keep_in_progress=keep_in_progress,
                keep_specials=keep_specials,
                protect_incomplete=protect_incomplete_seasons,
                flag_keep_conflicts=flag_keep_conflicts,
                airing_seasons=airing_seasons(series, seasons),
            )
            fully = not plan.prunable
            if fully:
                # No prunable season, but the show has content on disk, so it is NOT dropped:
                # it is judged and surfaced as kept, every season protected by its guard, so the
                # operator always sees it (with the reason) instead of it vanishing from the UI.
                # Never hide content. The flag only spares it the per-show episodes() read below,
                # which feeds mid-binge precision it does not need (every season is already kept).
                fully_protected.append(str(series.get("title") or "?"))
            _log_series_decision(
                source,
                series,
                seasons,
                outcome="fully_protected" if fully else "candidate",
                plan=plan,
            )
            work.append(
                _SeriesWork(
                    source=source, series=series, seasons=seasons, plan=plan, fully_protected=fully
                )
            )

    # Every series above emitted one greppable DEBUG decision line (season_scan.series_decision)
    # naming its outcome and reasons -- that is where "why isn't this specific show in review"
    # is answered, per title. These INFO counts are the snapshot-level summary on top of it.
    # A fully-protected show is now GATHERED, not dropped: it is surfaced as kept (never hide
    # content), so this counts how many shows have no reapable season, not how many vanished.
    # Only a no-content show (nothing on disk) is left out, because it has nothing to show.
    if no_content:
        # These shows have nothing on disk, so there is no season to put in the queue. Warned
        # (not INFO) so the operator is aware some monitored shows are absent for a benign
        # reason; the titles are in the per-show season_scan.series_decision lines
        # (outcome=no_content) at debug.
        log.warning(
            "season_scan.shows_without_content",
            count=len(no_content),
            detail=(
                f"{len(no_content)} TV shows are monitored with no episodes downloaded, so they "
                "are not in the review queue. There is nothing on disk to remove."
            ),
        )
    if fully_protected:
        log.info("season_scan.fully_protected_shows", count=len(fully_protected))

    # Resolve the shows that made the cut: bind each to its Plex row (pure, in memory),
    # then fetch what the judging needs over the network -- each distinct show's season
    # keys from Tautulli, and each show's on-disk episode list from Sonarr.
    # Stale-mapping guard: an operator whose library map points at a library holding none of an
    # ambiguous show's copies has a wrong or renamed mapping. Collected per (instance, library)
    # so it warns once, and only for a mapping that never once matched a candidate library.
    mapped_lib_hits: set[tuple[int, str]] = set()
    stale_map_misses: dict[tuple[int, str], str] = {}
    for item in work:
        series = item.series
        series_path = str(series["path"]) if series.get("path") else None
        ids = identity.ExternalIds.of(imdb=series.get("imdbId"), tvdb=series.get("tvdbId"))
        # The Plex library the operator mapped this series' root folder to, if any. It is the
        # ONLY thing that can tell two same-name show listings apart -- a show has no size and
        # its folder is identical in both an HD and a 4K library, so the path never can.
        plex_library = identity.library_for_path(series_path, item.source.library_map)
        # The one shared resolver: bind the show by tvdb, then file basename, then
        # title+year -- abstaining on any ambiguity or cross-tier conflict, exactly as the
        # movie path does. A None binding leaves every season's facts Unknown -> abstain.
        resolution = identity.resolve_show(
            ids=ids,
            title=str(series.get("title") or ""),
            year=_as_year(series.get("year")),
            file_basename=identity.to_basename(series.get("path")),
            # The full series folder, for the root check. Sonarr puts a series directly under
            # its root, so below the root there is only the leaf both copies already matched on:
            # the path cannot narrow a show. The operator's library map (plex_library) can.
            file_path=series_path,
            root_folders=roots_by_instance.get(item.source.instance_id),
            plex_library=plex_library,
            index=tv_index,
        )
        if plex_library is not None:
            key = (item.source.instance_id, plex_library)
            if plex_library.strip().casefold() in identity.libraries_for_ids(
                ids, tv_index, identity.SHOW_ID_PRIORITY
            ):
                mapped_lib_hits.add(key)
            elif resolution.status in identity.ABSTAIN_STATUSES:
                # Both abstains, not AMBIGUOUS alone -- the movie twin's reasoning, rule 143.
                stale_map_misses.setdefault(key, str(series.get("title") or ""))
        item.show_rating_key = resolution.rating_key
        item.matched_by = resolution.matched_by
        item.match_detail = resolution.detail
        item.match_status = resolution.status
        item.match_candidates = resolution.candidate_rating_keys
        item.plex_imdb_id = resolution.plex_item.ids.imdb if resolution.plex_item else None
        if resolution.plex_item is not None:
            # Show-level display metadata, inherited by every season row of the show.
            item.show_content_rating = resolution.plex_item.content_rating
            item.show_runtime_minutes = resolution.plex_item.runtime_minutes
            item.show_plex_ratings = resolution.plex_item.ratings
            item.show_library = resolution.plex_item.library
            # The matched path, mirroring the movie scan's scan.plex_matched. At debug so a
            # large show library does not flood the log, but every show's bind is traceable.
            log.debug(
                "scan.plex_matched",
                media_type="show",
                instance_id=item.source.instance_id,
                title=str(series.get("title") or ""),
                rating_key=resolution.rating_key,
                matched_by=str(resolution.matched_by),
                detail=resolution.detail,
            )
        else:
            # Prunable in Sonarr, but Reaper could not bind the show to a Plex row, so every
            # season abstains and the show appears only as "kept to be safe", never on the
            # reap list. Warned per show so "why isn't my show in review" is answerable from
            # the log. UNMATCHED = nothing in Plex matched; AMBIGUOUS = more than one did.
            # For an AMBIGUOUS show the fix is almost always the library map: two same-name
            # listings a show has no size or folder to split need the operator's declaration
            # of which library each root folder lands in. So the log names what was mapped for
            # this item (None = nothing mapped for its folder) and the libraries the copies
            # actually live in, so "no file size to tell them apart" is not a dead end.
            log.warning(
                "scan.plex_unmatched",
                media_type="show",
                instance_id=item.source.instance_id,
                title=str(series.get("title") or ""),
                year=_as_year(series.get("year")),
                imdb_id=series.get("imdbId") or None,
                tvdb_id=series.get("tvdbId") or None,
                match_status=str(resolution.status),
                detail=resolution.detail,
                mapped_library=plex_library,
                candidate_libraries=identity.candidate_libraries(
                    ids, tv_index, identity.SHOW_ID_PRIORITY
                )
                or None,
            )

    # The stale-mapping guard fires once per mapping that never once matched a candidate
    # library across the whole scan: a mapping that hit for even one show is working and is
    # never warned about. Visible in the in-app Logs, where the operator already reads
    # scan.plex_unmatched. Advisory only -- it never degrades the scan or changes a verdict.
    for (instance_id, library), example in stale_map_misses.items():
        if (instance_id, library) in mapped_lib_hits:
            continue
        log.warning(
            "scan.stale_library_map",
            media_type="show",
            instance_id=instance_id,
            library=library,
            example_title=example,
            detail=(
                f"No shows on this Sonarr were found in the Plex library {library!r} that its "
                "folder is mapped to. The library may have been renamed, or the mapping is "
                "wrong. Duplicated shows under that folder are kept, not matched."
            ),
        )

    # The per-show reads are independent of each other, so they run concurrently under
    # small bounds: one for Tautulli, one per Sonarr instance (two instances are two
    # servers; sharing one bound would halve each for no one's protection). A large
    # library prunes hundreds of shows, and reading them one show at a time was the
    # scan's longest sequential stretch; the bound keeps a modest self-hosted service at
    # a handful of parallel reads, never a stampede. Failure semantics are per call and
    # unchanged: an unresolvable show's seasons stay Unknown (abstain), a failed episode
    # read falls back to season-level protection.
    tautulli_bound = asyncio.Semaphore(RESOLVE_CONCURRENCY)
    arr_bounds = {source.instance_id: asyncio.Semaphore(RESOLVE_CONCURRENCY) for source in sonarrs}

    async def _seasons_for(show_rk: int) -> tuple[int, dict[int, PlexSeason]]:
        async with tautulli_bound:
            return show_rk, await resolve_season_keys(tautulli, show_rk)

    async def _episodes_for(item: _SeriesWork) -> None:
        # Episode-precise mid-binge needs each season's last on-disk episode -- one extra
        # Sonarr read per prunable show. On failure the map stays empty and every season
        # falls back to season-level protection, never less.
        async with arr_bounds[item.source.instance_id]:
            try:
                episodes = await item.source.client.episodes(int(item.series["id"]))
            except IntegrationError as exc:
                log.warning(
                    "season_scan.episodes_unreachable",
                    show=item.series.get("title"),
                    error=str(exc),
                )
                return
        item.season_final_episode = _final_episodes(episodes)

    # The DISTINCT matched shows: two Sonarr series can bind to the same Plex show, and it is
    # still one show's season list.
    show_keys = list(
        dict.fromkeys(item.show_rating_key for item in work if item.show_rating_key is not None)
    )

    # One paged Plex type=3 sweep of the show libraries resolves EVERY show's seasons at once,
    # replacing a per-show Tautulli read each. The keys are identical -- both address the same
    # linked server -- so a swept season joins the same watch history the per-show call would
    # have. library_season_index never returns a partial map: it either sweeps completely or
    # raises, and on a raise we fall back to the per-show path for every show rather than
    # degrade, since the same data is reachable one show at a time (slower, never less safe).
    swept: dict[int, list[PlexSeasonRow]] = {}
    if plex is not None and show_keys:
        try:
            swept = await plex.library_season_index(allowed_sections=allowed_sections)
        except PlexError as exc:
            log.warning("season_scan.season_sweep_failed", error=str(exc))

    resolved_shows: dict[int, dict[int, PlexSeason]] = {}
    fallback_keys: list[int] = []
    for rk in show_keys:
        rows = swept.get(rk)
        if rows is None:
            # A show the sweep did not return (a whole-sweep failure leaves every show here; a
            # healthy sweep leaves none). Resolved per show below, exactly as before S2.
            fallback_keys.append(rk)
        else:
            resolved_shows[rk] = seasons_from_rows(
                (r.season_index, r.rating_key, r.added_at) for r in rows
            )
    if fallback_keys:
        log.info("season_scan.season_sweep_fallback", shows=len(fallback_keys))

    # Per-show fallback reads plus the episodes() reads, in one flat reaped fan-out so an
    # unexpected failure in any one cancels and drains ALL the others -- nothing keeps polling
    # Tautulli or Sonarr for a scan that is already dead. The episodes() read exists only to
    # feed episode-precise mid-binge protection; with keep_in_progress off, season_final_episode
    # is never consulted (season_pruning short-circuits to no sequential protection), so the
    # whole Sonarr fan-out is skipped. Skipping only ever keeps MORE: with no final-episode map
    # the guard falls back to whole-season protection.
    season_coros = [_seasons_for(rk) for rk in fallback_keys]
    episode_coros = (
        [_episodes_for(item) for item in work if not item.fully_protected]
        if keep_in_progress
        else []
    )
    fanned = await gather_reaped(*season_coros, *episode_coros)
    for rk, seasons in fanned[: len(season_coros)]:
        resolved_shows[rk] = seasons

    all_season_keys: set[int] = set()
    for item in work:
        if item.show_rating_key is not None:
            item.seasons_in_plex = resolved_shows[item.show_rating_key]
            all_season_keys.update(s.rating_key for s in item.seasons_in_plex.values())

    stats = await season_watch_stats(engine, all_season_keys, window_days=window_days)

    # Series-level IMDb ratings, from the dataset we already ingest, applied to each season
    # (a season has no IMDb title of its own). A degraded dataset degrades the whole snapshot
    # exactly as it does on the movie path -- a missing rating REMOVES protection.
    # Look up by BOTH the Sonarr series imdbId and the matched Plex show's imdb id, so a
    # show Sonarr has no (or a wrong) imdbId for still gets its rating when Plex knows it.
    imdb_ids = [str(w.series["imdbId"]) for w in work if w.series.get("imdbId")]
    imdb_ids += [w.plex_imdb_id for w in work if w.plex_imdb_id]
    # A show's Sonarr imdbId and its Plex-matched imdb id are usually the same string;
    # the dataset lookup returns a keyed map, so deduping only trims the chunk count.
    imdb_ids = list(dict.fromkeys(imdb_ids))
    ratings_degraded = False
    try:
        ratings = await ImdbRatings(engine).lookup(imdb_ids) if imdb_ids else {}
        # Same coverage signal as the movie path (snapshot.scan): low coverage means the
        # series rating floor protected little. Per scan, so info.
        log.info("scan.imdb_coverage", media="tv", requested=len(imdb_ids), resolved=len(ratings))
    except DatasetDegradedError as exc:
        # Degrading is not enough on its own: with an empty map every show would read
        # "checked, and unrated", withdrawing every rating-based keep at once. Flag it
        # so each season records "could not check" instead. Twin of the movie path's
        # handler in snapshot.scan (rule 72).
        degrade(str(exc))
        ratings = {}
        ratings_degraded = True

    # The one clock read for the mid-binge expiry, taken once so every show in this scan
    # judges viewer activity against the same instant -- the snapshot discipline.
    now = utcnow()

    judgments: list[SeasonJudgment] = []
    for item in work:
        judgments.extend(
            _judge_series(
                item,
                stats=stats,
                horizon=horizon,
                reach_days=reach_days,
                now=now,
                active_rating_keys=active_rating_keys,
                activity_degraded=activity_degraded,
                keep_last_seasons=keep_last_seasons,
                keep_first_season=keep_first_season,
                whitelisted=whitelisted,
                requested=requested or {},
                request_index=request_index,
                keep_last_scope=keep_last_scope,
                season_lookahead=season_lookahead,
                keep_in_progress=keep_in_progress,
                in_progress_hold_days=in_progress_hold_days,
                keep_specials=keep_specials,
                protect_incomplete_seasons=protect_incomplete_seasons,
                flag_keep_conflicts=flag_keep_conflicts,
                ratings=ratings,
                ratings_degraded=ratings_degraded,
                membership_index=membership_index,
                watch_marks=watch_marks,
            )
        )

    log.info(
        "season_scan.gathered",
        seasons=len(judgments),
        shows_gathered=len(work),
        shows_fully_kept=sum(1 for item in work if item.fully_protected),
    )
    return judgments


def _judge_series(
    item: _SeriesWork,
    *,
    stats: SeasonWatchStats,
    horizon: datetime,
    reach_days: int,
    now: datetime | None = None,
    active_rating_keys: set[int],
    activity_degraded: bool,
    keep_last_seasons: int,
    keep_first_season: bool,
    whitelisted: set[str],
    membership_index: lists.MembershipIndex,
    requested: dict[str, str] | None = None,
    request_index: requested_by.RequestIndex | None = None,
    keep_last_scope: str = "all",
    season_lookahead: int = 0,
    keep_in_progress: bool = True,
    # Mirrors ``TvPolicy.in_progress_hold_days``'s own default, which is what the only
    # production caller passes (``services.snapshot.scan``). It read 0 here, a value no
    # shipped policy has, and 0 means the hold never expires -- so every caller that omitted
    # it was exercising an unbounded claim the mirror cannot support (rule 141).
    in_progress_hold_days: int = 180,
    # Required for the same reason as on ``gather`` above, and it is the same class of defect
    # rule 141 records one parameter up: a default that silently exercises a claim the caller
    # never made. Here the default disabled a protection outright.
    watch_marks: Mapping[str, watch_evidence.Mark],
    keep_specials: bool = True,
    protect_incomplete_seasons: bool = True,
    flag_keep_conflicts: bool = True,
    ratings: dict[str, ImdbRating] | None = None,
    # True when the IMDb dataset could not be read at all, so `ratings` being empty
    # says nothing about any show in it. See build_season_facts.
    ratings_degraded: bool = False,
) -> list[SeasonJudgment]:
    """Build a judgment for every content-bearing season of one series.

    Prunable AND protected seasons are emitted, so the Protected page can show the
    reasoning for the kept siblings -- not only the season that would go.
    """
    requested = requested or {}
    ratings = ratings or {}
    series = item.series
    series_id = int(series["id"])
    series_title = str(series.get("title") or "")
    ranks = rank_seasons(list(item.seasons))

    # The show's IMDb rating (if any), shared by every season -- see build_season_facts.
    # Prefer Sonarr's imdbId; fall back to the Plex-matched imdb id when Sonarr's is
    # missing or does not resolve (reality/recent shows TVDB has no IMDb mapping for).
    # The bool is whether we had any id to ask with: a show with neither a Sonarr imdbId
    # nor a Plex-matched one was never looked up, and must not be recorded as unrated.
    show_rating, show_rating_looked_up = dataset_lookup(
        ratings, str(series.get("imdbId") or "") or None, item.plex_imdb_id
    )

    # Show-level display fields, shared by every season row of this series.
    tvdb_id = int(series["tvdbId"]) if series.get("tvdbId") else None
    show_year = int(series["year"]) if series.get("year") else None
    show_summary = _series_summary(series)
    group_key = f"sonarr:{item.source.instance_id}:{series_id}"
    title_slug = str(series.get("titleSlug") or "") or None
    # Outbound-link coordinates: Seerr and TMDb key on the show's tmdb id; the IMDb page
    # on its imdb id (Sonarr's first, the Plex-matched one as fallback -- the same
    # precedence the rating lookup uses).
    show_tmdb_id = int(series["tmdbId"]) if series.get("tmdbId") else None
    show_imdb_id = str(series.get("imdbId") or "") or item.plex_imdb_id
    # The frozen ratings row: the dataset entry the scoring signal used first (they must
    # never disagree), the matched Plex show's ratings filling the rest.
    show_ratings_json = build_ratings_json(show_rating, item.show_plex_ratings)

    # Show-level facts shared by every season row: ended-vs-returning and genre.
    show_ended_obs = series_ended(series)
    show_genres_obs = series_genres(series)

    # Re-plan WITH the watch evidence now available: the sequential guard and the
    # keep-rule conflict detector both need per-user and per-season watcher counts that
    # only exist after the Plex resolution and the mirror read.
    key_to_number = {s.rating_key: n for n, s in item.seasons_in_plex.items()}
    # Decided BEFORE the roll-up, not after, because everything below reads the same mirror
    # this answers a question about (rule 140). A season whose plays went unreadable does not
    # only mis-score itself: its viewer's place in the show disappears from
    # `_progress_by_user`, and its own watcher count reads as a measured zero to the conflict
    # detector. Both of those decide the fate of OTHER seasons, which take no `Unknown` of
    # their own and would condemn at full confidence on evidence that moved.
    blind_by_season: dict[int, str | None] = {}
    for season in item.seasons:
        in_plex_for_blind = item.seasons_in_plex.get(season.season_number)
        blind_reading = watch_evidence.reading_for(
            in_plex_for_blind.rating_key if in_plex_for_blind is not None else None,
            stats.watchers_all_time,
            stats.last_played,
        )
        blind_by_season[season.season_number] = (
            watch_evidence.went_blind(
                watch_marks.get(
                    season_media_key(item.source.instance_id, series_id, season.season_number)
                ),
                blind_reading,
            )
            if blind_reading is not None
            else None
        )
    show_watch_unreadable = any(reason is not None for reason in blind_by_season.values())
    # Expire abandoned viewers before the guard sees them: a place in the show is held
    # only while its viewer stayed active within the policy's hold window. The helper
    # keeps every viewer whose last-watched time cannot be read, and 0 disables expiry.
    # What it CANNOT do is keep a viewer the mirror never saw, which is why the reach is
    # asked separately below rather than left for this filter to imply.
    progress = active_progress(
        _progress_by_user(stats, key_to_number),
        _last_watched_by_user(stats, key_to_number),
        now=now or utcnow(),
        hold_days=in_progress_hold_days,
    )
    # Built over the seasons ON DISK -- the exact set the conflict detector compares --
    # not over the ones Plex happened to resolve (rule 30). A season on disk that Plex
    # never resolved has no rating key, so nobody could read its history: that is None,
    # "not measured", and the detector skips it. Built from seasons_in_plex alone it was
    # simply absent, and `.get(n, 0)` then asserted nobody watched it, which invented
    # conflicts and told the operator a count that was never taken. 0 still means what it
    # always did: resolved, and nobody watched it.
    #
    # Each count is qualified in the same pass, by the same rule every other reader of an
    # all-time count uses (rule 140): the mirror must reach back to the day THAT season
    # arrived, since every play it could ever have had happened after that. Short of it the
    # count is a lower bound, and the shortfall says so in the operator's words. A season
    # Plex never resolved has no arrival date here either, so it is unbounded on both
    # counts -- consistent, and the detector skips it for the count alone.
    watchers_by_season: dict[int, int | None] = {}
    shortfall_by_season: dict[int, str | None] = {}
    reach = Known(value=float(reach_days), source="tautulli")
    for season in item.seasons:
        in_plex = item.seasons_in_plex.get(season.season_number)
        # A blind season joins the never-resolved one as None. `0` here would assert
        # "resolved, and nobody watched it" about a season whose plays are simply filed
        # elsewhere, which is the affirmative measurement rule 93 forbids -- and the detector
        # would then compare a real count against that zero and find no conflict where one is
        # exactly what the operator needs to see.
        watchers_by_season[season.season_number] = (
            stats.watchers_all_time.get(in_plex.rating_key, 0)
            if in_plex is not None and blind_by_season.get(season.season_number) is None
            else None
        )
        added_at = in_plex.added_at if in_plex is not None else None
        # Measured from this season's OWN arrival, never the show's: a season backfilled
        # into an old show arrived recently, and the show's date would call its exact count
        # a truncated one. The same derivation `build_season_facts` records as
        # `Facts.days_since_added` below, off the same date.
        age: Observation[float] = (
            Known(value=float(dormancy_days(added_at, now=now or utcnow())), source="plex")
            if added_at is not None
            else Unknown(reason=NO_ADDED_AT_REASON, source="plex")
        )
        shortfall_by_season[season.season_number] = lifetime_shortfall(reach, age)
    plan = plan_series_prune(
        series_title=series_title,
        seasons=item.seasons,
        keep_last=keep_last_seasons,
        keep_first_season=keep_first_season,
        apply_keep_last=_keep_last_applies(series, keep_last_scope, request_index),
        progress_by_user=progress,
        last_play_by_user=_last_play_by_user_season(stats, key_to_number),
        season_final_episode=item.season_final_episode,
        season_lookahead=season_lookahead,
        keep_in_progress=keep_in_progress,
        # The same reach that qualifies the watcher counts on `Facts.history_reach_days`,
        # read here by the mid-binge half of this roll-up (rule 140). A hold the mirror does
        # not span makes the viewer set un-establishable, and the planner holds the seasons
        # rather than reading "no rows" as "nobody is part-way through".
        progress_established=progress_is_establishable(
            reach_days=reach_days, hold_days=in_progress_hold_days
        ),
        # The same guard's other blind spot, and the reason the check above is not enough: the
        # mirror can span the hold perfectly and still not hold the rows, because they are
        # filed under a rating key the season no longer carries. `active_progress` keeps a
        # viewer whose last-watched time is unreadable, but this viewer is not unreadable, they
        # are missing, and there is nobody to keep -- the exact sentence
        # `gates.progress_is_establishable` uses about a short mirror (rule 140).
        progress_unreadable=show_watch_unreadable,
        keep_specials=keep_specials,
        protect_incomplete=protect_incomplete_seasons,
        flag_keep_conflicts=flag_keep_conflicts,
        airing_seasons=airing_seasons(series, item.seasons),
        watchers_by_season=watchers_by_season,
        # The other half of the same sweep: these counts come off the same mirror, so the
        # keep-conflict detector is handed the bound with them rather than comparing two
        # lower bounds at full confidence.
        shortfall_by_season=shortfall_by_season,
    )

    # Every id the show carries is passed together: a show without an imdbId in Sonarr is
    # common, and a keep tag or "Never Reap" row stored under its tvdb or tmdb id must
    # still protect it. Matching on one id kind alone fails open on the deletion path.
    curated_by_series = membership_index.lookup(
        media_type="tv", imdb_id=show_imdb_id, tmdb_id=show_tmdb_id, tvdb_id=tvdb_id
    )
    hard = [m for m in curated_by_series if m.mode is lists.ListMode.HARD]
    whitelists = [m for m in hard if m.is_whitelist]
    curated = [m for m in hard if not m.is_whitelist]

    judgments: list[SeasonJudgment] = []
    for season in item.seasons:
        if not season.has_content:
            continue
        n = season.season_number
        media_key = season_media_key(item.source.instance_id, series_id, n)
        in_plex = item.seasons_in_plex.get(n)
        plex_key = in_plex.rating_key if in_plex else None
        title = season_title(series_title, n)
        requested_obs = (
            request_index.season_requested(tvdb_id, n)
            if request_index is not None
            else Unknown(reason=requested_by.REQUESTS_NOT_LOADED_REASON, source="seerr")
        )
        # A season's history is read by its own Plex key, so the same fall this scan can
        # detect for a movie is detectable here, against the same marks (rule 72). Decided
        # above, before the show roll-up, and only read here: one decision per season, so the
        # facts, the count handed to the conflict detector and the mid-binge hold cannot
        # disagree about which seasons went blind.
        reading = watch_evidence.reading_for(plex_key, stats.watchers_all_time, stats.last_played)
        season_blind = blind_by_season.get(n)
        if season_blind is not None:
            log.warning(
                "scan.watch_history_unreadable",
                media_key=media_key,
                media_type="season",
            )
        facts = build_season_facts(
            title=title,
            season=season,
            rank=ranks.get(n),
            plex_rating_key=plex_key,
            watch_blind_reason=season_blind,
            season_added_at=in_plex.added_at if in_plex else None,
            horizon=horizon,
            reach_days=reach_days,
            last_played=stats.last_played.get(plex_key) if plex_key else None,
            watchers_window=stats.watchers_window.get(plex_key) if plex_key else None,
            watchers_all_time=stats.watchers_all_time.get(plex_key) if plex_key else None,
            active_rating_keys=active_rating_keys,
            activity_degraded=activity_degraded,
            whitelisted=bool(whitelists) or media_key in whitelisted,
            curated=curated,
            imdb_rating=show_rating,
            rating_looked_up=show_rating_looked_up,
            rating_dataset_degraded=ratings_degraded,
            plex_ratings=item.show_plex_ratings,
            requested=requested_obs,
            show_ended=show_ended_obs,
            genres=show_genres_obs,
            show_match_status=item.match_status,
        )
        # Requested-by, display only, never a gate. The tier precedence (including B-10's
        # season-key-above-show-rating-key ordering) lives in the one season_requester helper.
        season_requester_name = season_requester(
            requested,
            media_key=media_key,
            group_key=group_key,
            tvdb_id=tvdb_id,
            season_number=n,
            show_rating_key=item.show_rating_key,
        )
        judgments.append(
            SeasonJudgment(
                media_key=media_key,
                plex_rating_key=plex_key,
                title=title,
                # The scoring lane reads the honest Observation off `facts`; this is the
                # display and reclaim-accounting column. None means Sonarr reported a
                # season holding files without sizing it, and it stays None: no season
                # worth deleting is genuinely 0 bytes, so a stored 0 would be a
                # measurement Reaper never took.
                #
                # What that costs the season is deletion, while the owner's allowance
                # (`ProfileSettings.max_unmeasured_per_run`) is shut: `planner.build_plan`
                # holds it back, `executor._may_send_unmeasured` refuses it again per item,
                # and both caps and the byte total the owner confirms leave it out. With the
                # allowance open it IS planned and DOES count against the item caps; only
                # the byte sums still leave it out (`executor._deletable_bytes`). Either way
                # it still scores and still shows in the queue, saying "Size unknown".
                # Do not "fix" this by inventing a size here.
                size_bytes=season.size_on_disk,
                size_source=SizeSource.SONARR if season.size_on_disk is not None else None,
                facts=facts,
                guard_result=guard_result(plan, n),
                watch_reading=reading,
                watch_blind_reason=season_blind,
                year=show_year,
                summary=show_summary,
                requested_by=season_requester_name,
                group_key=group_key,
                group_title=series_title,
                poster_rating_key=item.show_rating_key,
                matched_by=item.matched_by,
                match_detail=item.match_detail,
                match_status=item.match_status,
                match_candidates=item.match_candidates,
                title_slug=title_slug,
                tmdb_id=show_tmdb_id,
                imdb_id=show_imdb_id,
                tvdb_id=tvdb_id,
                content_rating=item.show_content_rating,
                runtime_minutes=item.show_runtime_minutes,
                library=item.show_library,
                ratings_json=show_ratings_json,
                show_status=show_status_key(show_ended_obs),
            )
        )
    return judgments
