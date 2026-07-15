# SPDX-License-Identifier: AGPL-3.0-or-later
"""Season pruning: which seasons of a show may be removed, and which must not.

"Keep the last N seasons" sounds trivial and is a minefield. Every guard here closes a
bug a shipping competitor actually has, and each one resolves toward *keeping* a season:

* **Keep the last N.** Rank seasons newest-first; the N most recent are kept. Rank comes
  from :func:`rank_seasons`, which excludes specials so they cannot silently shift the
  count. (Season *rank* is also a scoring signal elsewhere -- a beloved old season earns
  enough negative pressure from its popularity to survive on score. This module is the
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

#: Keep the last watched season *and* this many after it -- the "up next" a viewer is
#: about to reach. One lookahead season is the episode they will play tonight.
SEQUENTIAL_LOOKAHEAD = 1

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
            f"{self.pruned_watchers} {viewers} watched Season {self.pruned_season} — more "
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
    watched_max_by_user: Mapping[str, int], *, lookahead: int = SEQUENTIAL_LOOKAHEAD
) -> set[int]:
    """Seasons to protect because a viewer is part-way through.

    For each viewer's highest watched season ``m``, protect ``m`` and the next
    ``lookahead`` seasons (``m+1`` ... ``m+lookahead``). The union across viewers is the
    set of seasons somebody is watching or about to watch.
    """
    protected: set[int] = set()
    for highest in watched_max_by_user.values():
        for offset in range(lookahead + 1):
            protected.add(highest + offset)
    return protected


def plan_series_prune(
    *,
    series_title: str,
    seasons: Sequence[SeasonStats],
    keep_last: int,
    keep_first_season: bool = True,
    watched_max_by_user: Mapping[str, int] | None = None,
    airing_seasons: Collection[int] = (),
    watchers_by_season: Mapping[int, int] | None = None,
) -> SeriesPrunePlan:
    """Decide, for one series, which seasons may be pruned.

    Only seasons that actually hold files (``has_content``) are considered -- an empty or
    unmonitored-and-gone season is nothing to remove. Everything else is protected, with
    the reason recorded so the why-panel can show it. ``keep_last`` is clamped at 0; a
    negative keep would be nonsense and must never widen what is prunable.
    """
    keep_last = max(0, keep_last)
    watched_max_by_user = watched_max_by_user or {}
    watchers_by_season = watchers_by_season or {}
    airing = set(airing_seasons)

    ranks = rank_seasons(list(seasons))
    seq_protected = sequential_protections(watched_max_by_user)

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
    if rank is not None and rank <= keep_last:
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
