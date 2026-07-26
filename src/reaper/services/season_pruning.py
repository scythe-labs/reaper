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
  this, "keep the last 2 seasons" deletes the season a user is mid-binge on. "Last
  watched" means most recent in TIME, not highest-numbered: anchoring on the number alone
  left every re-watcher and every out-of-order viewer with no protection at all, because
  someone who has finished the show is judged to be waiting on a season that does not
  exist (see :func:`sequential_protections`, which now anchors on both). Unioned
  across viewers, because the set of "next episodes people are about to watch" is the
  union of everyone's. The policy can turn the guard off (``keep_in_progress``), and a
  viewer's hold expires once their whole-show activity is older than the policy's
  ``in_progress_hold_days`` (see :func:`active_progress`) -- an abandoned half-watched
  season must not pin a show forever. A viewer whose last-watched time cannot be read
  keeps their hold.

* **Never touch a currently-airing (or still-downloading) season.** Maintainerr #949: a
  mid-season break longer than the timeout leaves the back half permanently undownloaded.
  A season Sonarr is still filling is not a candidate for removal. The still-downloading
  half is the only one an operator can turn off (``protect_incomplete``), for an ended show
  Sonarr permanently lists as missing an episode; the airing half always applies.

* **Keep-rule conflict detector.** If the rule would remove a season that has *strictly
  more* viewers than one it keeps, that is almost certainly not what the owner wants
  ("season 1 is the only good one"). Reaper does not silently obey -- it raises a plain
  warning and refuses to auto-approve, leaving the call to a human. The policy can turn
  the detector off (``flag_keep_conflicts``), and then the keep rule is simply followed.

The whole module is pure: it takes already-gathered facts and returns a decision. No
network, no database, no clock -- so every branch above is tested exhaustively, and the
one place a season's fate is decided has no hidden inputs.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from reaper.clients.sonarr_stats import SeasonStats, rank_seasons

#: Default seasons to protect BEYOND a viewer's current position while they binge. 0 keeps
#: exactly the season they are on (or the next one, if they finished the current). The policy
#: supplies the real value; this constant is only the fallback.
SEQUENTIAL_LOOKAHEAD = 0

#: Season 0 is specials: out-of-run content that is frequently the oldest and newest at
#: once. Never auto-pruned by default (the policy's ``keep_specials`` can allow it) and
#: never occupies a keep-slot either way.
SPECIALS_SEASON = 0


@dataclass(frozen=True)
class ProtectedSeason:
    season_number: int
    reason: str


def _because(kept_reason: str) -> str:
    """Turn a kept season's protection reason into a "because ..." clause, so a conflict
    names *why* the season it was compared against is being kept -- not just "your keep
    rule." Reads season_pruning's own closed vocabulary (the strings ``_protection_reason``
    returns); anything unrecognized falls back to a safe generic clause, never an error."""
    if kept_reason.startswith("Sonarr is still downloading"):
        return "Sonarr is still downloading it"
    if kept_reason == "currently airing":
        return "it is currently airing"
    if kept_reason.startswith("the first season"):
        return "it is the first season, so the show can still be started"
    if kept_reason.startswith("within the last") or kept_reason.startswith("this show has only"):
        return "it is one of the newest seasons your rule keeps"
    if kept_reason.startswith("a viewer is part-way"):
        return "a viewer is part-way through it"
    return "your season rule keeps it"


@dataclass(frozen=True)
class PruneConflict:
    """A season the rule would remove has more viewers than a season it keeps."""

    pruned_season: int
    kept_season: int
    pruned_watchers: int
    kept_watchers: int | None
    """How many watched the kept season, or ``None`` when that could not be read.

    ``None`` is a conflict in its own right, not a missing one: the comparison could not
    be made, so the season is held for the operator rather than cleared by default. It is
    kept distinct from ``0`` (read, and nobody watched) because collapsing the two is what
    produced a message asserting a count nobody ever took."""
    #: Why the kept season is being kept -- its ``ProtectedSeason.reason``, so the message
    #: can name the real protection ("still downloading") instead of a vague "keep rule."
    kept_reason: str

    @property
    def message(self) -> str:
        viewers = "person" if self.pruned_watchers == 1 else "people"
        if self.kept_watchers is None:
            return (
                f"{self.pruned_watchers} {viewers} watched Season {self.pruned_season}. "
                f"Reaper could not check who watched Season {self.kept_season}, which it "
                f"is keeping because {_because(self.kept_reason)}. Left for you to decide "
                "instead of removing it."
            )
        return (
            f"{self.pruned_watchers} {viewers} watched Season {self.pruned_season}, more "
            f"than watched Season {self.kept_season}, which Reaper is keeping because "
            f"{_because(self.kept_reason)}. Left for you to decide instead of removing it."
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


def _next_after(anchor: int, on_disk: Collection[int] | None) -> set[int]:
    """The season a viewer moves to once they have finished ``anchor``.

    ``anchor + 1`` only when a season actually holds files there. A show's seasons are not
    always a contiguous run -- Sonarr never filled one, someone deleted one by hand, or
    Reaper pruned one on an earlier run -- and advancing blindly into a hole pinned the
    hold on a season number nothing holds, leaving the season the viewer is genuinely
    about to watch prunable while the guard read as having run. Worse, it compounds: every
    prune widens the hole that hid the next one. Rule 124 asks for exactly this check --
    that the anchored position is a member of the set before it counts as cover.

    Empty when nothing follows: a viewer who finished the last season is not mid-binge,
    which is the same answer the specials branch gives and the same *effective* answer as
    before (``anchor + 1`` simply matched no season). ``None`` keeps the old arithmetic for
    a caller that does not know what is on disk.
    """
    if on_disk is None:
        return {anchor + 1}
    later = [n for n in on_disk if n > anchor]
    return {min(later)} if later else set()


def _anchor_positions(
    anchor: int,
    progress: Mapping[int, int | None],
    season_final_episode: Mapping[int, int | None],
    on_disk: Collection[int] | None = None,
) -> set[int]:
    """What one anchor season protects: the season being watched, or the next one.

    Fail-closed: if the anchor's final episode is unknown (Sonarr unavailable) or the
    viewer's episode index is unknown (a season with only un-backfilled rows), protect
    BOTH the anchor and the one after -- exactly the old season-level behavior, never
    less.
    """
    final = season_final_episode.get(anchor)
    watched = progress.get(anchor)
    if anchor == SPECIALS_SEASON:
        # Specials are not a sequence. There is no "next special" to line up, and
        # season 1 is emphatically not what follows season 0, so a viewer part-way
        # through the specials holds the specials themselves and nothing else.
        if final is not None and watched is not None and watched >= final:
            return set()
        return {SPECIALS_SEASON}
    if final is None or watched is None:
        return {anchor} | _next_after(anchor, on_disk)
    if watched >= final:
        return _next_after(anchor, on_disk)  # finished the anchor, ready for the next
    return {anchor}  # still watching it


def sequential_protections(
    progress_by_user: Mapping[str, Mapping[int, int | None]],
    season_final_episode: Mapping[int, int | None],
    *,
    lookahead: int = SEQUENTIAL_LOOKAHEAD,
    last_play_by_user: Mapping[str, Mapping[int, datetime | None]] | None = None,
    include_specials: bool = False,
    on_disk: Collection[int] | None = None,
) -> set[int]:
    """Seasons to protect because a viewer is part-way through -- episode-precise.

    Each viewer contributes up to two anchors, and the union across viewers is the set
    somebody is watching or about to:

    * the season of their **most recent play** (``last_play_by_user``), which is what
      "the season they last watched" in this module's contract actually means; and
    * the **highest-numbered** season they have any play under.

    Both, because either can be the one they are on and neither subsumes the other. The
    number alone was the whole bug: a viewer who has finished the show and started it
    again is anchored on the finale, judged ready for a season that does not exist, and
    protected nowhere -- so the season they are re-watching today is prunable, and the
    conflict detector does not catch it either (every season shares the same all-time
    watcher count). The recency anchor alone is not enough on its own either: someone
    mid-binge on the newest season who dips back into an old one would lose the hold on
    the season they are actually working through.

    Each anchor is then resolved by :func:`_anchor_positions` and extended by
    ``lookahead``. ``include_specials`` adds Season 0 to the anchor candidates, for the
    one configuration where a special can be pruned at all (``keep_specials`` off);
    otherwise specials are excluded, since a hold on something nothing can remove only
    costs the operator seasons they wanted gone.
    """
    last_play_by_user = last_play_by_user or {}
    protected: set[int] = set()
    for user, progress in progress_by_user.items():
        candidates = [n for n in progress if include_specials or n != SPECIALS_SEASON]
        if not candidates:
            continue
        anchors = {max(candidates)}
        # A viewer with no readable timestamps keeps exactly the old anchor: unreadable
        # is not evidence that they are somewhere else.
        times = {
            n: when
            for n, when in (last_play_by_user.get(user) or {}).items()
            if when is not None and n in progress and (include_specials or n != SPECIALS_SEASON)
        }
        if times:
            anchors.add(max(times, key=lambda n: times[n]))
        for anchor in anchors:
            for start in _anchor_positions(anchor, progress, season_final_episode, on_disk):
                for offset in range(lookahead + 1):
                    protected.add(start + offset)
    return protected


def active_progress(
    progress_by_user: Mapping[str, Mapping[int, int | None]],
    last_watched_by_user: Mapping[str, datetime | None],
    *,
    now: datetime,
    hold_days: int,
) -> dict[str, Mapping[int, int | None]]:
    """Only the viewers whose place in the show is still held -- the expiry half of the
    sequential guard (policy ``in_progress_hold_days``).

    A viewer counts as still watching when their last play of *this show* is within
    ``hold_days`` of ``now``. Two deliberate keep-leaning edges: ``hold_days <= 0`` means
    the hold never expires (every viewer passes), and a viewer with no readable
    last-watched time keeps their hold -- "we could not look" is not "they quit".
    Pure: the clock is an argument, never read here.
    """
    if hold_days <= 0:
        return dict(progress_by_user)
    cutoff = now - timedelta(days=hold_days)
    return {
        user: progress
        for user, progress in progress_by_user.items()
        if (last := last_watched_by_user.get(user)) is None or last >= cutoff
    }


def plan_series_prune(
    *,
    series_title: str,
    seasons: Sequence[SeasonStats],
    keep_last: int,
    keep_first_season: bool = True,
    apply_keep_last: bool = True,
    progress_by_user: Mapping[str, Mapping[int, int | None]] | None = None,
    # Per viewer, per season, when they last played it. The sequential guard anchors on
    # the most recent of these, which is the only way to tell a re-watcher (or an
    # out-of-order viewer) from someone working steadily up the season numbers.
    last_play_by_user: Mapping[str, Mapping[int, datetime | None]] | None = None,
    season_final_episode: Mapping[int, int | None] | None = None,
    season_lookahead: int = SEQUENTIAL_LOOKAHEAD,
    keep_in_progress: bool = True,
    keep_specials: bool = True,
    protect_incomplete: bool = True,
    flag_keep_conflicts: bool = True,
    airing_seasons: Collection[int] = (),
    # Season number -> all-time watcher count, or None for a season nobody could
    # measure (on disk, but never resolved in Plex). None is NOT zero: see
    # _detect_conflicts.
    watchers_by_season: Mapping[int, int | None] | None = None,
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
    # Needed by the mid-binge guard below, which must advance a finished viewer to a season
    # that EXISTS rather than to `anchor + 1` (rule 124), so it is derived before the call
    # rather than beside the prune loop that also uses it.
    on_disk = [s for s in seasons if s.has_content]
    on_disk_numbers = {s.season_number for s in on_disk}
    # The guard's off-switch empties the protected set here, in the one decision function,
    # so no caller can half-apply it. Expiry (in_progress_hold_days) happens upstream in
    # active_progress, which needs the per-viewer timestamps this function never sees.
    seq_protected = (
        sequential_protections(
            progress_by_user,
            season_final_episode,
            lookahead=season_lookahead,
            last_play_by_user=last_play_by_user,
            # Specials can only be pruned with keep_specials off, so that is the only
            # configuration where a viewer part-way through them needs the hold.
            include_specials=not keep_specials,
            on_disk=on_disk_numbers,
        )
        if keep_in_progress
        else set()
    )
    total_ranked = len(ranks)

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
            keep_specials=keep_specials,
            protect_incomplete=protect_incomplete,
            total_ranked=total_ranked,
            first_real=first_real,
            airing=airing,
            seq_protected=seq_protected,
        )
        if reason is None:
            prunable.append(n)
        else:
            protected.append(ProtectedSeason(season_number=n, reason=reason))

    # The policy can silence the conflict detector; an empty list is exactly "no conflict
    # found", so auto_approvable and every downstream consumer behave as if none fired.
    conflicts = (
        _detect_conflicts(prunable, protected, watchers_by_season) if flag_keep_conflicts else []
    )

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
    keep_specials: bool,
    protect_incomplete: bool,
    total_ranked: int,
    first_real: int | None,
    airing: set[int],
    seq_protected: set[int],
) -> str | None:
    """Why this season is kept, or ``None`` if it may be pruned.

    Ordered safety-first: the checks that describe an *active* or *fragile* season come
    before the mechanical keep-last rule, so the reason shown is the most important one.

    With ``keep_specials`` off, specials fall through to the checks below: the airing guard
    (and the still-downloading guard, unless ``protect_incomplete`` is off) still applies,
    but rank/first-season never do (specials are excluded from both by construction), so an
    idle Season 0 becomes prunable.
    """
    n = season.season_number

    if n == SPECIALS_SEASON and keep_specials:
        return "specials are never auto-pruned"
    if protect_incomplete and season.is_incomplete:
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
    watchers_by_season: Mapping[int, int | None],
) -> list[PruneConflict]:
    """Flag any prunable season with strictly more viewers than a kept season.

    Compared only against kept seasons that hold content (the ones in ``protected``),
    and never against Season 0: specials sit outside the run, are rarely watched, and a
    kept-but-unwatched specials season would otherwise flag every watched prunable
    season as a conflict -- refusing auto-approval for a comparison that means nothing.
    Specials are excluded from both sides. Strictly greater, so an equal count --
    common when neither has been watched -- is not a conflict.

    A season with no watcher count (``None``: on disk, but never resolved in Plex, so
    nobody could read its history) is NOT the same as one nobody watched, and the two
    sides treat it differently on purpose.

    Reading ``None`` as 0 turned an unmeasured season into a measured-and-unwatched one
    and invented conflicts out of nothing: the show sat in permanent abstain, scan after
    scan, and the operator was told in plain words that N people watched one season "more
    than watched" another -- a comparison against a number that was never taken. But
    *skipping* it on the kept side, which is what replaced that, threw the hold away with
    the bad sentence: a well-watched prunable season became condemnable purely because the
    season it would have been measured against could not be read. That is unreadable
    evidence clearing a protection, which is the one direction this codebase never
    resolves toward (rule 93).

    So an unreadable kept season is a conflict, carried as ``kept_watchers=None`` and
    worded "could not check" rather than dressed up as a count. The item is still held for
    the operator; only the false arithmetic is gone.

    On the pruned side ``None`` still passes, and that is not the same asymmetry: with no
    readable count for the season being removed there is nothing to compare FROM, so no
    conflict can be stated at all. A hold there would have to rest on some other signal,
    not on this comparison.

    The cost, stated so nobody reads a routine hold as a bug: a season is unreadable here
    exactly when it is on disk but not yet in Plex, and the commonest cause is benign. A
    keep-last-N show whose newest season has just finished downloading, or a season
    ``protect_incomplete`` is holding while Sonarr fills it, is kept AND unresolved, so
    every watched prunable season below it conflicts against it and the show sits in
    "Needs a look" until Plex catches up. That is fail-closed and it clears itself. It is
    the price of not letting an unread number clear a hold, and it is the right way round.
    """
    conflicts: list[PruneConflict] = []
    kept_seasons = [p for p in protected if p.season_number != SPECIALS_SEASON]
    for pruned in prunable:
        if pruned == SPECIALS_SEASON:
            continue
        pruned_watchers = watchers_by_season.get(pruned)
        if pruned_watchers is None or pruned_watchers == 0:
            # Unreadable, or read and nobody watched. Either way this season cannot be
            # shown to out-rank anything, so there is no comparison to make. Kept as two
            # named cases rather than one falsy test: collapsing them is the bug above.
            continue
        for kept in kept_seasons:
            kept_watchers = watchers_by_season.get(kept.season_number)
            if kept_watchers is None or pruned_watchers > kept_watchers:
                conflicts.append(
                    PruneConflict(
                        pruned_season=pruned,
                        kept_season=kept.season_number,
                        pruned_watchers=pruned_watchers,
                        kept_watchers=kept_watchers,
                        kept_reason=kept.reason,
                    )
                )
    return conflicts
