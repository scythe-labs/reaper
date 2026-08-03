# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a season plan is decided from, frozen, and the one derivation off it.

``services.season_pruning.plan_series_prune`` is pure and takes nineteen arguments. Ten of
them are the operator's policy; the rest are evidence a scan gathered. This module is the
seam: :class:`SeasonPruneInput` holds the evidence half for one show, :func:`plan_from_frozen`
supplies the policy half and calls the real planner, and the scan freezes the bundle so the
policy simulator can call the same function again under an edited policy.

**One derivation, not two.** The scan and the simulator both reach ``plan_series_prune``
through :func:`plan_from_frozen`, so the exactness the simulator claims is structural rather
than measured: there is no second implementation to drift (rule 3/22, and
``docs/LEARNINGS.md``'s "Two code paths answering one question will drift"). The pieces that
turn a policy number into a planner argument -- the mid-binge expiry, the mirror-reach
predicate, the keep-last scope -- live here for the same reason, since each was a place the
two could have disagreed.

**A missing episode map is ``None``, never ``{}``.** ``season_final_episode`` is read from
Sonarr only while ``keep_in_progress`` is on, so a scan taken with that guard off holds no
map at all -- and an empty dict is what a show whose episodes were read and found nothing
would carry. Conflating them tells the simulator it may answer a question nobody gathered
for (rule 93). :attr:`SeasonPruneInput.season_final_episode` is therefore three-state, and
:func:`missing_episode_map` is what the route asks before it previews an edit that turns the
guard on.

Pure: no clock, no network, no database. The scan instant rides in the bundle.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from reaper.clients.sonarr_stats import SeasonStats
from reaper.clock import from_epoch
from reaper.engine.gates import progress_is_establishable
from reaper.engine.policy import PolicyBody
from reaper.services.season_pruning import SeriesPrunePlan, active_progress, plan_series_prune


@dataclass(frozen=True, slots=True)
class SeasonPruneInput:
    """Everything one show's prune plan reads that is *not* the operator's policy.

    Frozen by the scan, thawed by the simulator. Every field here is evidence: it came off
    Sonarr, off Plex, or off the watch mirror, and re-reading it is what a fresh scan is
    for. A field that is really a policy value belongs in :func:`plan_from_frozen`'s
    signature instead, or the simulator would replay the scan's setting rather than the
    operator's edit.
    """

    series_title: str
    seasons: tuple[SeasonStats, ...]
    airing_seasons: tuple[int, ...]

    progress_by_user: Mapping[str, Mapping[int, int | None]]
    """Each viewer's highest completed episode per season, **before** the hold expires any
    of them. Expiry is ``in_progress_hold_days``, an operator setting, so it is applied in
    :func:`plan_from_frozen` rather than baked in here -- freezing the expired set would
    make that setting unpreviewable while looking exactly as if it were not."""

    last_watched_by_user: Mapping[str, datetime | None]
    """When each viewer last played anything of this show, the other half of that expiry.
    ``None`` for a viewer whose timestamp could not be read, which ``active_progress``
    keeps."""

    last_play_by_user: Mapping[str, Mapping[int, datetime | None]]
    season_final_episode: Mapping[int, int | None] | None
    """The highest episode on disk per season, or ``None`` when this scan never asked.

    Three-state on purpose: see the module docstring. ``None`` also covers a Sonarr read
    that failed, which is the same absence and the same refusal."""

    watchers_by_season: Mapping[int, int | None]
    shortfall_by_season: Mapping[int, str | None]
    progress_unreadable: bool
    progress_seasons_unmatched: bool
    progress_unknown_reason: str | None
    """Why nobody could be asked about this show's viewers, or ``None``. Passed to
    ``season_scan.guard_result``, not to the planner."""

    requested_known_false: bool
    """Whether the request index said, definitely, that nobody requested this show. The one
    bit ``keep_last_scope`` reads, resolved here because the index is a scan-time source
    (fail-closed: an Unknown is *not* a definite no, so the floor still applies)."""

    reach_days: int
    now: datetime
    """The scan's one clock read, so the mid-binge expiry re-decides against the instant the
    evidence was taken rather than against whenever the operator opened the editor."""


@dataclass(frozen=True, slots=True)
class SeasonPolicy:
    """The nine ``PolicyBody`` fields a season plan reads, and nothing else.

    A carrier, so the scan and the simulator hand :func:`plan_from_frozen` the same shape.
    The scan builds one from the values ``services.snapshot.scan`` already unpacks off the
    stored body; the simulator builds one straight off the draft with :meth:`from_body`.

    **Two roads from one ``PolicyBody`` to these nine values, which is rule 144's shape** --
    a field added to the season card has to reach both, and the one written second is the
    one that reads correct while being incomplete. The guard is not a field-name check but
    ``tests/test_scan_pipeline.py``'s exactness test: it drives a real scan under a body with
    every one of these set away from its default, then replays the frozen bundle under that
    same body and demands the scan's own verdicts back. A value that reaches one road and
    not the other cannot survive that, whichever road drops it.
    """

    keep_last_seasons: int
    keep_first_season: bool
    keep_last_scope: str
    season_lookahead: int
    keep_in_progress: bool
    in_progress_hold_days: int
    keep_specials: bool
    protect_incomplete_seasons: bool
    flag_keep_conflicts: bool

    @classmethod
    def from_body(cls, body: PolicyBody) -> SeasonPolicy:
        return cls(
            keep_last_seasons=body.keep_last_seasons,
            keep_first_season=body.keep_first_season,
            keep_last_scope=body.keep_last_scope,
            season_lookahead=body.season_lookahead,
            keep_in_progress=body.keep_in_progress,
            in_progress_hold_days=body.in_progress_hold_days,
            keep_specials=body.keep_specials,
            protect_incomplete_seasons=body.protect_incomplete_seasons,
            flag_keep_conflicts=body.flag_keep_conflicts,
        )


def keep_last_applies(*, keep_last_scope: str, requested_known_false: bool) -> bool:
    """Whether the keep-last floor applies to a show under this scope.

    Fail-closed under ``requested``: the floor still applies unless the show is KNOWN not to
    have been requested, so an unreadable Seerr never widens what may be pruned.
    """
    return keep_last_scope != "requested" or not requested_known_false


def plan_from_frozen(inp: SeasonPruneInput, *, policy: SeasonPolicy) -> SeriesPrunePlan:
    """The one place a season plan is derived from evidence plus a policy.

    Called by ``season_scan._judge_series`` during a scan and by the policy simulator's
    replay afterwards, with the same bundle and a different policy. Both reach the real
    ``plan_series_prune``; nothing here re-implements a protection (rule 3/22).
    """
    return plan_series_prune(
        series_title=inp.series_title,
        seasons=inp.seasons,
        keep_last=policy.keep_last_seasons,
        keep_first_season=policy.keep_first_season,
        apply_keep_last=keep_last_applies(
            keep_last_scope=policy.keep_last_scope,
            requested_known_false=inp.requested_known_false,
        ),
        progress_by_user=active_progress(
            inp.progress_by_user,
            inp.last_watched_by_user,
            now=inp.now,
            hold_days=policy.in_progress_hold_days,
        ),
        last_play_by_user=inp.last_play_by_user,
        # `or {}` collapses the three-state to the planner's two: it reads an empty map as
        # "no episode is known to be the last", which protects whole seasons rather than
        # positions. Safe for the scan, and NOT exact for a replay -- which is why the route
        # asks `missing_episode_map` first rather than letting this line answer quietly.
        season_final_episode=inp.season_final_episode or {},
        season_lookahead=policy.season_lookahead,
        keep_in_progress=policy.keep_in_progress,
        progress_established=progress_is_establishable(
            reach_days=inp.reach_days, hold_days=policy.in_progress_hold_days
        ),
        progress_unreadable=inp.progress_unreadable,
        progress_seasons_unmatched=inp.progress_seasons_unmatched,
        keep_specials=policy.keep_specials,
        protect_incomplete=policy.protect_incomplete_seasons,
        flag_keep_conflicts=policy.flag_keep_conflicts,
        airing_seasons=inp.airing_seasons,
        watchers_by_season=inp.watchers_by_season,
        shortfall_by_season=inp.shortfall_by_season,
    )


def missing_episode_map(inp: SeasonPruneInput, *, policy: SeasonPolicy) -> bool:
    """Whether replaying this show under ``policy`` would read a map the scan never gathered.

    Only ``keep_in_progress`` consults ``season_final_episode``, so a draft with that guard
    off replays exactly off a bundle that holds no map -- the planner short-circuits the
    sequential guard before the map is touched. A draft with it *on* over a bundle that
    holds none would silently fall back to whole-season protection, which reads as a
    confident preview of an answer nobody gathered.
    """
    return policy.keep_in_progress and inp.season_final_episode is None


#: Every field of :class:`SeasonPruneInput`, and the codec key it is stored under. Written
#: out rather than derived from the field names so the stored shape is stable across a
#: rename, and checked against the dataclass at import (below) so a field added later cannot
#: be dropped from the freeze in silence -- a bundle missing an input replays a plan the scan
#: never made (rule 103).
_KEYS: dict[str, str] = {
    "series_title": "title",
    "seasons": "seasons",
    "airing_seasons": "airing",
    "progress_by_user": "progress",
    "last_watched_by_user": "last_watched",
    "last_play_by_user": "last_play",
    "season_final_episode": "finals",
    "watchers_by_season": "watchers",
    "shortfall_by_season": "shortfall",
    "progress_unreadable": "unreadable",
    "progress_seasons_unmatched": "unmatched",
    "progress_unknown_reason": "no_progress_reason",
    "requested_known_false": "not_requested",
    "reach_days": "reach",
    "now": "at",
}

_DECLARED = {f.name for f in dataclasses.fields(SeasonPruneInput)}
if set(_KEYS) != _DECLARED:
    raise RuntimeError(  # pragma: no cover - import-time drift guard
        "SeasonPruneInput fields changed: "
        f"{_DECLARED ^ set(_KEYS)}. Give each a key in season_evidence._KEYS and freeze it, "
        "or the simulator replays a plan built from evidence the scan did not record."
    )


def _epoch(value: datetime | None) -> int | None:
    return int(value.timestamp()) if value is not None else None


def _season_to_dict(s: SeasonStats) -> dict[str, Any]:
    return {
        "n": s.season_number,
        "monitored": s.monitored,
        "files": s.episode_file_count,
        "size": s.size_on_disk,
        "total": s.total_episode_count,
        "wanted": s.wanted_episode_count,
    }


def _season_from_dict(d: Mapping[str, Any]) -> SeasonStats:
    return SeasonStats(
        season_number=int(d["n"]),
        monitored=bool(d["monitored"]),
        episode_file_count=int(d["files"]),
        # The one nullable member, and it stays nullable: `sonarr_stats._reported_size` maps
        # an unreported size to None precisely so nothing downstream reads a 0 it never
        # measured.
        size_on_disk=None if d["size"] is None else int(d["size"]),
        total_episode_count=int(d["total"]),
        wanted_episode_count=int(d["wanted"]),
    )


def to_dict(inp: SeasonPruneInput) -> dict[str, Any]:
    """Freeze one show's bundle. Integer keys become strings, as JSON requires."""
    return {
        _KEYS["series_title"]: inp.series_title,
        _KEYS["seasons"]: [_season_to_dict(s) for s in inp.seasons],
        _KEYS["airing_seasons"]: list(inp.airing_seasons),
        _KEYS["progress_by_user"]: {
            user: {str(n): ep for n, ep in progress.items()}
            for user, progress in inp.progress_by_user.items()
        },
        _KEYS["last_watched_by_user"]: {
            user: _epoch(at) for user, at in inp.last_watched_by_user.items()
        },
        _KEYS["last_play_by_user"]: {
            user: {str(n): _epoch(at) for n, at in plays.items()}
            for user, plays in inp.last_play_by_user.items()
        },
        _KEYS["season_final_episode"]: (
            None
            if inp.season_final_episode is None
            else {str(n): ep for n, ep in inp.season_final_episode.items()}
        ),
        _KEYS["watchers_by_season"]: {str(n): c for n, c in inp.watchers_by_season.items()},
        _KEYS["shortfall_by_season"]: {str(n): r for n, r in inp.shortfall_by_season.items()},
        _KEYS["progress_unreadable"]: inp.progress_unreadable,
        _KEYS["progress_seasons_unmatched"]: inp.progress_seasons_unmatched,
        _KEYS["progress_unknown_reason"]: inp.progress_unknown_reason,
        _KEYS["requested_known_false"]: inp.requested_known_false,
        _KEYS["reach_days"]: inp.reach_days,
        _KEYS["now"]: _epoch(inp.now),
    }


def from_dict(d: Mapping[str, Any]) -> SeasonPruneInput:
    """Thaw one show's bundle.

    Raises ``KeyError``/``TypeError``/``ValueError`` on anything it cannot read, rather than
    defaulting a member. There is no safe default here: every one of these is evidence, and
    a missing key means the scan did not record it, which is a refusal (rule 93). The caller
    catches and refuses to preview -- see ``api.routes._season_bundles``.
    """
    at = from_epoch(d[_KEYS["now"]])
    if at is None:
        raise ValueError("a frozen season bundle has no scan instant")
    finals = d[_KEYS["season_final_episode"]]
    return SeasonPruneInput(
        series_title=str(d[_KEYS["series_title"]]),
        seasons=tuple(_season_from_dict(s) for s in d[_KEYS["seasons"]]),
        airing_seasons=tuple(int(n) for n in d[_KEYS["airing_seasons"]]),
        progress_by_user={
            str(user): {int(n): None if ep is None else int(ep) for n, ep in progress.items()}
            for user, progress in d[_KEYS["progress_by_user"]].items()
        },
        last_watched_by_user={
            str(user): from_epoch(at_) for user, at_ in d[_KEYS["last_watched_by_user"]].items()
        },
        last_play_by_user={
            str(user): {int(n): from_epoch(when) for n, when in plays.items()}
            for user, plays in d[_KEYS["last_play_by_user"]].items()
        },
        season_final_episode=(
            None
            if finals is None
            else {int(n): None if ep is None else int(ep) for n, ep in finals.items()}
        ),
        watchers_by_season={
            int(n): None if c is None else int(c) for n, c in d[_KEYS["watchers_by_season"]].items()
        },
        shortfall_by_season={
            int(n): None if r is None else str(r)
            for n, r in d[_KEYS["shortfall_by_season"]].items()
        },
        progress_unreadable=bool(d[_KEYS["progress_unreadable"]]),
        progress_seasons_unmatched=bool(d[_KEYS["progress_seasons_unmatched"]]),
        progress_unknown_reason=(
            None
            if d[_KEYS["progress_unknown_reason"]] is None
            else str(d[_KEYS["progress_unknown_reason"]])
        ),
        requested_known_false=bool(d[_KEYS["requested_known_false"]]),
        reach_days=int(d[_KEYS["reach_days"]]),
        now=at,
    )
