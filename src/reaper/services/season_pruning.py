# SPDX-License-Identifier: AGPL-3.0-or-later
"""Season pruning: which seasons of a show may be removed, and which must not.

"Keep the last N seasons" sounds trivial and is a minefield. Every guard here closes a
bug a shipping competitor actually has, and each one resolves toward *keeping* a season:

* **Keep the last N.** Rank seasons newest-first; the N most recent are kept. Rank comes
  from :func:`rank_seasons`, which excludes specials AND fileless seasons (an announced
  next season with nothing downloaded yet) so neither can silently shift the count and
  spend a keep-slot on nothing. (Season *rank* is also a scoring signal elsewhere -- a
  beloved old season earns enough negative pressure from its popularity to survive on
  score. This module is the
  hard floor underneath that: things that must be kept regardless of any score.)

* **Keep the first season.** On by default. The pilot is how anyone starts the show; a
  library that has thrown away season 1 is a library nobody new can begin.

* **Sequential-progression guard.** For every viewer part-way through the show, keep the
  season they last watched *and the next one* -- the one they are about to watch. Without
  this, "keep the last 2 seasons" deletes the season a user is mid-binge on. Unioned
  across viewers, because the set of "next episodes people are about to watch" is the
  union of everyone's.

* **Never touch a currently-airing (or still-downloading) season.** Maintainerr #949: a
  mid-season break longer than the timeout leaves the back half permanently undownloaded.
  A season Sonarr is still filling is not a candidate for removal.

* **Keep-rule conflict detector.** If the rule would remove a season that has *strictly
  more* viewers than one it keeps, that is almost certainly not what the owner wants
  ("season 1 is the only good one"). Reaper does not silently obey -- it raises a plain
  warning and refuses to auto-approve, leaving the call to a human.

The whole module is pure: it takes already-gathered facts and returns a decision. No
network, no database, no clock -- so every branch above is tested exhaustively, and the
one place a season's fate is decided has no hidden inputs.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from reaper.clients.sonarr_stats import SeasonStats, rank_seasons

#: Default seasons to protect BEYOND a viewer's current position while they binge. 0 keeps
#: exactly the season they are on (or the next one, if they finished the current). The policy
#: supplies the real value; this constant is only the fallback.
SEQUENTIAL_LOOKAHEAD = 0

#: Season 0 is specials: out-of-run content that is frequently the oldest and newest at
#: once. It is never auto-pruned and never occupies a keep-slot.
SPECIALS_SEASON = 0


@dataclass(frozen=True)
class ProtectedSeason:
    season_number: int
    reason: str


@dataclass(frozen=True)
class PruneConflict:
    """A season the rule would remove has more viewers than a season it keeps."""

    pruned_season: int
    kept_season: int
    pruned_watchers: int
    kept_watchers: int

    @property
    def message(self) -> str:
        viewers = "person" if self.pruned_watchers == 1 else "people"
        return (
            f"{self.pruned_watchers} {viewers} watched Season {self.pruned_season}, more "
            f"than watched Season {self.kept_season}, which your keep rule protects. "
            f"Reaper left it for you to decide instead of removing it."
        )


@dataclass(frozen=True)
class SeriesPrunePlan:
    """The verdict for one series: what may be pruned, what is kept and why, and any
    conflicts that must send it to a human."""

    series_title: str
    prunable: list[int] = field(default_factory=list)
    protected: list[ProtectedSeason] = field(default_factory=list)
    conflicts: list[PruneConflict] = field(default_factory=list)

    @property
    def auto_approvable(self) -> bool:
        """A conflict means the rule is fighting the evidence. Refuse auto-approval and
        make a person look."""
        return not self.conflicts


def sequential_protections(
    progress_by_user: Mapping[str, Mapping[int, int | None]],
    season_final_episode: Mapping[int, int | None],
    *,
    lookahead: int = SEQUENTIAL_LOOKAHEAD,
) -> set[int]:
    """Seasons to protect because a viewer is part-way through -- episode-precise.

    For each viewer, anchor on ``m`` = the highest real season they have any play under. If
    they have completed ``m``'s last on-disk episode they are ready for ``m+1`` (protect
    ``m+1``); otherwise they are still watching ``m`` (protect ``m``). Then extend each
    protected season by ``lookahead``. The union across viewers is the set somebody is
    watching or about to.

    Fail-closed: if ``m``'s final episode is unknown (Sonarr unavailable) or the viewer's
    episode index is unknown (a season with only un-backfilled rows), protect BOTH ``m`` and
    ``m+1`` -- exactly the old season-level behaviour, never less.
    """
    protected: set[int] = set()
    for progress in progress_by_user.values():
        real = [n for n in progress if n != SPECIALS_SEASON]
        if not real:
            continue
        m = max(real)
        final = season_final_episode.get(m)
        watched = progress.get(m)
        if final is None or watched is None:
            positions = {m, m + 1}  # fail closed to season-level
        elif watched >= final:
            positions = {m + 1}  # finished m, ready for the next
        else:
            positions = {m}  # still watching m
        for start in positions:
            for offset in range(lookahead + 1):
                protected.add(start + offset)
    return protected


def plan_series_prune(
    *,
    series_title: str,
    seasons: Sequence[SeasonStats],
    keep_last: int,
    keep_first_season: bool = True,
    apply_keep_last: bool = True,
    progress_by_user: Mapping[str, Mapping[int, int | None]] | None = None,
    season_final_episode: Mapping[int, int | None] | None = None,
    season_lookahead: int = SEQUENTIAL_LOOKAHEAD,
    airing_seasons: Collection[int] = (),
    watchers_by_season: Mapping[int, int] | None = None,
) -> SeriesPrunePlan:
    """Decide, for one series, which seasons may be pruned.

    Only seasons that actually hold files (``has_content``) are considered -- an empty or
    unmonitored-and-gone season is nothing to remove. Everything else is protected, with
    the reason recorded so the why-panel can show it. ``keep_last`` is clamped at 0; a
    negative keep would be nonsense and must never widen what is prunable.

    ``apply_keep_last`` is the keep-last-scope gate: under a "requested only" scope it is
    False for a show nobody requested, so its older seasons are no longer shielded by the
    keep-last floor (every other guard and the score still apply).
    """
    keep_last = max(0, keep_last)
    progress_by_user = progress_by_user or {}
    season_final_episode = season_final_episode or {}
    watchers_by_season = watchers_by_season or {}
    airing = set(airing_seasons)

    ranks = rank_seasons(list(seasons))
    seq_protected = sequential_protections(
        progress_by_user, season_final_episode, lookahead=season_lookahead
    )
    total_ranked = len(ranks)

    on_disk = [s for s in seasons if s.has_content]
    real_numbers = [s.season_number for s in on_disk if s.season_number != SPECIALS_SEASON]
    first_real = min(real_numbers) if real_numbers else None

    prunable: list[int] = []
    protected: list[ProtectedSeason] = []

    for season in sorted(on_disk, key=lambda s: s.season_number):
        n = season.season_number
        reason = _protection_reason(
            season,
            rank=ranks.get(n),
            keep_last=keep_last,
            keep_first_season=keep_first_season,
            apply_keep_last=apply_keep_last,
            total_ranked=total_ranked,
            first_real=first_real,
            airing=airing,
            seq_protected=seq_protected,
        )
        if reason is None:
            prunable.append(n)
        else:
            protected.append(ProtectedSeason(season_number=n, reason=reason))

    conflicts = _detect_conflicts(prunable, protected, watchers_by_season)

    return SeriesPrunePlan(
        series_title=series_title,
        prunable=sorted(prunable),
        protected=protected,
        conflicts=conflicts,
    )


def _protection_reason(
    season: SeasonStats,
    *,
    rank: int | None,
    keep_last: int,
    keep_first_season: bool,
    apply_keep_last: bool,
    total_ranked: int,
    first_real: int | None,
    airing: set[int],
    seq_protected: set[int],
) -> str | None:
    """Why this season is kept, or ``None`` if it may be pruned.

    Ordered safety-first: the checks that describe an *active* or *fragile* season come
    before the mechanical keep-last rule, so the reason shown is the most important one.
    """
    n = season.season_number

    if n == SPECIALS_SEASON:
        return "specials are never auto-pruned"
    if season.is_incomplete:
        return "Sonarr is still downloading this season"
    if n in airing:
        return "currently airing"
    if keep_first_season and n == first_real:
        return "the first season is kept so the show can still be started"
    if apply_keep_last and rank is not None and rank <= keep_last:
        if keep_last >= total_ranked:
            plural = "s" if total_ranked != 1 else ""
            return (
                f"this show has only {total_ranked} season{plural} on disk, so your keep-last-"
                f"{keep_last} rule keeps all of them"
            )
        return f"within the last {keep_last} seasons (rank {rank})"
    if n in seq_protected:
        return "a viewer is part-way through the show"
    return None


def _detect_conflicts(
    prunable: Sequence[int],
    protected: Sequence[ProtectedSeason],
    watchers_by_season: Mapping[int, int],
) -> list[PruneConflict]:
    """Flag any prunable season with strictly more viewers than a kept season.

    Compared only against kept seasons that hold content (the ones in ``protected``);
    specials and empties are not meaningful comparisons. Strictly greater, so an equal
    count -- common when neither has been watched -- is not a conflict.
    """
    conflicts: list[PruneConflict] = []
    kept_numbers = [p.season_number for p in protected]
    for pruned in prunable:
        pruned_watchers = watchers_by_season.get(pruned, 0)
        for kept in kept_numbers:
            kept_watchers = watchers_by_season.get(kept, 0)
            if pruned_watchers > kept_watchers:
                conflicts.append(
                    PruneConflict(
                        pruned_season=pruned,
                        kept_season=kept,
                        pruned_watchers=pruned_watchers,
                        kept_watchers=kept_watchers,
                    )
                )
    return conflicts
