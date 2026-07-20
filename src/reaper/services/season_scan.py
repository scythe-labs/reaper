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

Only seasons that survive the guards as *prunable* are scored, and only shows that have
at least one such season are resolved against Plex at all -- which bounds the per-show
Plex calls to the shows that actually have something removable.

## The season -> Plex rating key join (verify against a live server)

There is no Tautulli sweep that lists seasons; ``get_library_media_info`` returns
show-level rows only. So a show's seasons are resolved via one
``get_children_metadata`` call per show (several shows in flight at once, bounded by
``RESOLVE_CONCURRENCY``), and two field assumptions are load-bearing and must be
checked against a real instance before the first live TV reap:

1. A show row from ``get_library_media_info`` carries ``rating_key`` and ``year``, and
   its ``title`` matches the Sonarr series title closely enough to join on.
2. ``get_children_metadata`` on that show returns one child per season, each with
   ``rating_key`` (the season's Plex key) and ``media_index`` (the season number).
3. Each season child carries its OWN ``added_at`` -- the date that season's files
   landed, not the show's -- so a season backfilled into an old show reads as recently
   arrived. Dormancy is floored on this; a season whose ``added_at`` cannot be read is
   Unknown-dormant and therefore abstains, never condemned off the show's old date.

All three are the documented shapes, but "documented" is not "verified", so each is
isolated in a pure function with a fail-closed default rather than trusted inline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import batched
from typing import Any

import structlog
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.aio import gather_reaped
from reaper.clients.base import IntegrationError
from reaper.clients.plex import PlexClient
from reaper.clients.sonarr_stats import SeasonStats, parse_season_stats, rank_seasons
from reaper.clients.tautulli import TautulliClient
from reaper.clock import from_epoch, utcnow
from reaper.db.models import SizeSource
from reaper.engine import identity
from reaper.engine.gates import ABSTAIN as GATE_ABSTAIN
from reaper.engine.gates import PROTECT as GATE_PROTECT
from reaper.engine.gates import Facts, GateId, GateResult
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.ratings import Rating, RatingSource, merge_by_source
from reaper.services import library_index, lists, requested_by
from reaper.services.display_meta import build_ratings_json, dataset_lookup
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


def _rating_obs(value: float | None, looked_up: bool) -> Observation[int]:
    """One IMDb figure as a three-state observation. See build_season_facts."""
    if value is not None:
        return Known(value=int(value), source="imdb")
    if looked_up:
        return Absent(source="imdb")
    return Unknown(reason="no IMDb id to look up", source="imdb")


#: How many shows to resolve against Tautulli / Sonarr at once. The single biggest cost in
#: a TV scan is one Tautulli ``get_children_metadata`` plus one Sonarr ``episodes`` call per
#: prunable show, and this bounds how many run concurrently. On a large library that is the
#: longest serial-ish stretch of the whole scan, so this is set high enough to collapse
#: hundreds of round trips into a handful of batches, and low enough that a modest
#: self-hosted Tautulli/Sonarr sees a bounded burst, never a stampede. Every call is a
#: read that fails closed (an unresolved show abstains; a failed episodes() falls back to
#: season-level protection), so a timeout under load never over-condemns -- it only keeps.
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
    # Show-level display metadata shared by every season row: the Sonarr web-route
    # coordinate, certification, episode runtime, and the frozen ratings row. A season
    # has none of its own; the show's stand in. Display only, never a verdict input.
    title_slug: str | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    content_rating: str | None = None
    runtime_minutes: int | None = None
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
    show_rating_key: int | None = None
    matched_by: identity.MatchedBy | None = None
    match_detail: str | None = None
    match_status: identity.MatchStatus | None = None
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
        return f"{series_title} · Specials"
    return f"{series_title} · Season {season_number}"


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
        return Unknown(reason="Sonarr did not report series status", source="sonarr")
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
    """
    for protected in plan.protected:
        if protected.season_number == season_number:
            return GateResult(GateId.SEASON_PROGRESSION, GATE_PROTECT, detail=protected.reason)

    for conflict in plan.conflicts:
        if conflict.pruned_season == season_number:
            return GateResult(
                GateId.SEASON_PROGRESSION, GATE_ABSTAIN, blocked=True, detail=conflict.message
            )

    return GateResult(
        GateId.SEASON_PROGRESSION,
        GATE_ABSTAIN,
        detail="checked: prunable by the keep-last / keep-first season rules",
    )


def build_season_facts(
    *,
    title: str,
    season: SeasonStats,
    rank: int | None,
    plex_rating_key: int | None,
    season_added_at: datetime | None,
    horizon: datetime,
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
    plex_ratings: tuple[Rating, ...] = (),
    requested: Observation[bool] = _UNSET_OBS,
    show_ended: Observation[bool] = _UNSET_OBS,
    genres: Observation[str] = _UNSET_OBS,
    show_match_status: identity.MatchStatus | None = None,
) -> Facts:
    """Assemble one season's evidence, with the same Unknown-discipline as the movie path.

    A season we could not resolve in Plex (``plex_rating_key is None``) has no watch
    history to read, so its dormancy and popularity are ``Unknown`` -- and Unknown, run
    through the gates, abstains. A file we cannot see is never condemned.
    ``show_match_status`` picks the honest wording for that Unknown: an AMBIGUOUS show
    (two Plex items share its id) is a different story from one Plex has no match for,
    and the why-panel must not tell the owner the wrong one.
    """
    dormancy: Observation[float]
    recent: Observation[int]
    all_time: Observation[int]
    streaming: Observation[bool]

    if plex_rating_key is None:
        no_key_reason = (
            "more than one Plex item matches this show"
            if show_match_status is identity.MatchStatus.AMBIGUOUS
            else "Plex has not matched this season"
        )
        dormancy = Unknown(reason=no_key_reason, source="plex")
        recent = Unknown(reason=no_key_reason, source="plex")
        all_time = Unknown(reason=no_key_reason, source="plex")
        streaming = Unknown(reason=no_key_reason, source="plex")
    else:
        # Dormancy is measured from THIS SEASON's own arrival date, never the show's -- a
        # season backfilled into an old show arrived recently even though the show is old,
        # and using the show's date would read a just-added season as decades dormant and
        # condemn a file nobody has had a chance to watch. This mirrors the movie path,
        # which floors on each item's own added_at. When we cannot establish the season's
        # arrival at all, dormancy is Unknown -- which protects -- exactly as a movie with
        # no added-at date does; we never fabricate a Known dormancy from the horizon.
        if last_played is not None:
            dormancy = Known(value=(utcnow() - last_played).days, source="tautulli")
        elif season_added_at is not None:
            reference = max(season_added_at, horizon)
            dormancy = Known(value=(utcnow() - reference).days, source="tautulli")
        else:
            dormancy = Unknown(reason="no added-at date for this season", source="tautulli")
        recent = Known(value=watchers_window or 0, source="tautulli")
        all_time = Known(value=watchers_all_time or 0, source="tautulli")
        if activity_degraded:
            streaming = Unknown(reason="could not read active sessions", source="tautulli")
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
        size_bytes=(
            Known(value=season.size_on_disk, source="sonarr")
            if season.size_on_disk is not None
            else Unknown(reason="the season's size was not reported", source="sonarr")
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
            imdb_rating.average_rating * 10 if imdb_rating else None, rating_looked_up
        ),
        imdb_votes=_rating_obs(imdb_rating.num_votes if imdb_rating else None, rating_looked_up),
        season_rank=(
            Known(value=rank, source="sonarr")
            if rank is not None
            else Unknown(reason="season has no rank", source="sonarr")
        ),
        is_streaming_now=streaming,
        is_managed=Known(value=True, source="sonarr"),
        in_curated_list=in_curated,
        is_whitelisted=Known(value=whitelisted, source="lists"),
        others_watching=Absent(source="tautulli"),
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


async def resolve_season_keys(
    tautulli: TautulliClient, show_rating_key: int
) -> dict[int, PlexSeason]:
    """season number -> its Plex season (rating key + own added-at), for one show.

    Resolved from ``get_children_metadata``; a season we cannot find here simply has no
    key, so its facts go Unknown and it abstains. Returns an empty map on any read
    failure rather than raising -- one show that will not resolve is not a reason to
    abort the whole scan, and an empty map is the fail-closed outcome (every season
    abstains).

    A season number that appears **twice** (a split or mis-scanned Plex library can emit
    two "Season N" items) is dropped entirely rather than bound to whichever rating key
    happened to sort last -- picking one risks reading an empty duplicate's history for a
    season people actually watched. An ambiguous season, like an ambiguous show, abstains.
    """
    try:
        children = await tautulli.children_metadata(show_rating_key)
    except IntegrationError as exc:
        log.warning("season_scan.children_failed", show=show_rating_key, error=str(exc))
        return {}
    result: dict[int, PlexSeason] = {}
    ambiguous: set[int] = set()
    for child in children:
        index = child.get("media_index")
        rk = child.get("rating_key")
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
        result[n] = PlexSeason(rating_key=key, added_at=from_epoch(child.get("added_at")))
    return result


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


async def gather(
    engine: AsyncEngine,
    *,
    sonarrs: list[SonarrSource],
    tautulli: TautulliClient,
    plex: PlexClient | None = None,
    horizon: datetime,
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
    in_progress_hold_days: int = 0,
    keep_specials: bool = True,
    flag_keep_conflicts: bool = True,
    membership_index: lists.MembershipIndex | None = None,
    allowed_sections: set[int] | None = None,
) -> list[SeasonJudgment]:
    """Gather every prunable season across every Sonarr instance, ready to judge.

    Read-only. Reads Sonarr for series and their season statistics, runs the guards to
    find prunable seasons, resolves only those shows against Plex, reads their watch
    history from the local mirror, and returns a ``SeasonJudgment`` per content-bearing
    season of a show that has something removable.

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

    # First pass, pure and offline: decide prunable/protected per series from Sonarr's
    # own season statistics. Only shows with a prunable season are resolved against Plex.
    work: list[_SeriesWork] = []
    fully_protected = 0
    for source, series_list in zip(sonarrs, series_lists, strict=True):
        if series_list is None:
            continue
        for series in series_list:
            seasons = parse_seasons(series)
            if not any(s.has_content for s in seasons):
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
                flag_keep_conflicts=flag_keep_conflicts,
                airing_seasons=airing_seasons(series, seasons),
            )
            if not plan.prunable:
                fully_protected += 1
                continue
            work.append(_SeriesWork(source=source, series=series, seasons=seasons, plan=plan))

    if fully_protected:
        # Not a silent drop: a fully-protected show has nothing to act on, so it is left
        # out of the candidate list, and the count is logged so "no TV candidates" can be
        # told apart from "TV was skipped".
        log.info("season_scan.fully_protected_shows", count=fully_protected)

    # Resolve the shows that made the cut: bind each to its Plex row (pure, in memory),
    # then fetch what the judging needs over the network -- each distinct show's season
    # keys from Tautulli, and each show's on-disk episode list from Sonarr.
    for item in work:
        series = item.series
        # The one shared resolver: bind the show by tvdb, then file basename, then
        # title+year -- abstaining on any ambiguity or cross-tier conflict, exactly as the
        # movie path does. A None binding leaves every season's facts Unknown -> abstain.
        resolution = identity.resolve_show(
            ids=identity.ExternalIds.of(imdb=series.get("imdbId"), tvdb=series.get("tvdbId")),
            title=str(series.get("title") or ""),
            year=_as_year(series.get("year")),
            file_basename=identity.to_basename(series.get("path")),
            # The full series folder, not just its leaf. Passed for completeness and for
            # the root check, NOT because it can narrow a show: Sonarr puts a series
            # directly under its root, so below the root there is only the leaf both copies
            # already matched on, and a deeper path means the reported root is wrong. Either
            # way the folder step stands down, so two same-leaf show listings under one id
            # abstain and are kept (identity._narrow_by_path_depth says why).
            file_path=str(series["path"]) if series.get("path") else None,
            root_folders=roots_by_instance.get(item.source.instance_id),
            index=tv_index,
        )
        item.show_rating_key = resolution.rating_key
        item.matched_by = resolution.matched_by
        item.match_detail = resolution.detail
        item.match_status = resolution.status
        item.plex_imdb_id = resolution.plex_item.ids.imdb if resolution.plex_item else None
        if resolution.plex_item is not None:
            # Show-level display metadata, inherited by every season row of the show.
            item.show_content_rating = resolution.plex_item.content_rating
            item.show_runtime_minutes = resolution.plex_item.runtime_minutes
            item.show_plex_ratings = resolution.plex_item.ratings

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

    # One Tautulli read per DISTINCT matched show: two Sonarr series can bind to the same
    # Plex show, and it is still one show's season list. One flat reaped fan-out, so an
    # unexpected failure in any one read cancels and drains ALL the others -- nothing
    # keeps polling Tautulli or Sonarr for a scan that is already dead.
    show_keys = list(
        dict.fromkeys(item.show_rating_key for item in work if item.show_rating_key is not None)
    )
    season_coros = [_seasons_for(rk) for rk in show_keys]
    fanned = await gather_reaped(*season_coros, *(_episodes_for(item) for item in work))
    resolved_shows: dict[int, dict[int, PlexSeason]] = dict(fanned[: len(season_coros)])

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
    try:
        ratings = await ImdbRatings(engine).lookup(imdb_ids) if imdb_ids else {}
    except DatasetDegradedError as exc:
        degrade(str(exc))
        ratings = {}

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
                flag_keep_conflicts=flag_keep_conflicts,
                ratings=ratings,
                membership_index=membership_index,
            )
        )

    log.info("season_scan.gathered", seasons=len(judgments), shows_pruned=len(work))
    return judgments


def _judge_series(
    item: _SeriesWork,
    *,
    stats: SeasonWatchStats,
    horizon: datetime,
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
    in_progress_hold_days: int = 0,
    keep_specials: bool = True,
    flag_keep_conflicts: bool = True,
    ratings: dict[str, ImdbRating] | None = None,
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
    # Expire abandoned viewers before the guard sees them: a place in the show is held
    # only while its viewer stayed active within the policy's hold window. The helper
    # keeps every viewer whose last-watched time cannot be read, and 0 disables expiry.
    progress = active_progress(
        _progress_by_user(stats, key_to_number),
        _last_watched_by_user(stats, key_to_number),
        now=now or utcnow(),
        hold_days=in_progress_hold_days,
    )
    watchers_by_season = {
        n: stats.watchers_all_time.get(s.rating_key, 0) for n, s in item.seasons_in_plex.items()
    }
    plan = plan_series_prune(
        series_title=series_title,
        seasons=item.seasons,
        keep_last=keep_last_seasons,
        keep_first_season=keep_first_season,
        apply_keep_last=_keep_last_applies(series, keep_last_scope, request_index),
        progress_by_user=progress,
        season_final_episode=item.season_final_episode,
        season_lookahead=season_lookahead,
        keep_in_progress=keep_in_progress,
        keep_specials=keep_specials,
        flag_keep_conflicts=flag_keep_conflicts,
        airing_seasons=airing_seasons(series, item.seasons),
        watchers_by_season=watchers_by_season,
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
            else Unknown(reason="requests not loaded", source="seerr")
        )
        facts = build_season_facts(
            title=title,
            season=season,
            rank=ranks.get(n),
            plex_rating_key=plex_key,
            season_added_at=in_plex.added_at if in_plex else None,
            horizon=horizon,
            last_played=stats.last_played.get(plex_key) if plex_key else None,
            watchers_window=stats.watchers_window.get(plex_key) if plex_key else None,
            watchers_all_time=stats.watchers_all_time.get(plex_key) if plex_key else None,
            active_rating_keys=active_rating_keys,
            activity_degraded=activity_degraded,
            whitelisted=bool(whitelists) or media_key in whitelisted,
            curated=curated,
            imdb_rating=show_rating,
            rating_looked_up=show_rating_looked_up,
            plex_ratings=item.show_plex_ratings,
            requested=requested_obs,
            show_ended=show_ended_obs,
            genres=show_genres_obs,
            show_match_status=item.match_status,
        )
        # Requested-by: prefer a request that named this season; fall back to a
        # whole-show request. Display only -- never a gate.
        season_requester = requested.get(
            requested_by.season_key(tvdb_id, n) or ""
        ) or requested.get(requested_by.show_key(tvdb_id) or "")
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
                # What that costs the season is deletion. `planner.build_plan` holds it
                # back, `executor.size_confirmed` refuses it again per item, and both caps
                # and the byte total the owner confirms leave it out. It still scores and
                # still shows in the queue, saying "Size unknown".
                # Do not "fix" this by inventing a size here.
                size_bytes=season.size_on_disk,
                size_source=SizeSource.SONARR if season.size_on_disk is not None else None,
                facts=facts,
                guard_result=guard_result(plan, n),
                year=show_year,
                summary=show_summary,
                requested_by=season_requester,
                group_key=group_key,
                group_title=series_title,
                poster_rating_key=item.show_rating_key,
                matched_by=item.matched_by,
                match_detail=item.match_detail,
                match_status=item.match_status,
                title_slug=title_slug,
                tmdb_id=show_tmdb_id,
                imdb_id=show_imdb_id,
                content_rating=item.show_content_rating,
                runtime_minutes=item.show_runtime_minutes,
                ratings_json=show_ratings_json,
                show_status=show_status_key(show_ended_obs),
            )
        )
    return judgments
