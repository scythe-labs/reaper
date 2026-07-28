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
  keeps their hold. The guard can only see the viewers the watch mirror reaches, so where
  the mirror is shallower than that hold -- or the hold is unbounded -- the viewer set is
  *un-establishable* rather than empty and every season on disk is held instead
  (:func:`progress_is_establishable`).

* **Never touch a season that is mid-run or short of episodes.** Maintainerr #949: a
  mid-season break longer than the timeout leaves the back half permanently undownloaded.
  A season Sonarr is still filling is not a candidate for removal. The missing-episodes
  half is the only one an operator can turn off (``protect_incomplete``), for an ended show
  Sonarr permanently lists as missing an episode; the mid-run half always applies. Neither
  observation is as sharp as it sounds, and the reasons shown to the operator say only what
  was seen: Sonarr reports *download intent*, not a live queue, and ``airing_seasons``
  returns the season to *treat as* airing rather than one known to be broadcasting. See
  :func:`_protection_reason`.

* **Keep-rule conflict detector.** If the rule would remove a season that has *strictly
  more* viewers than one it keeps, that is almost certainly not what the owner wants
  ("season 1 is the only good one"). Reaper does not silently obey -- it raises a plain
  warning and refuses to auto-approve, leaving the call to a human. Both counts are
  all-time, so both are only answers where the watch mirror reaches back to the day that
  season arrived; short of it a count is a LOWER BOUND, and the truncation is biased
  against exactly the old seasons keep-last wants to prune. So the detector takes each
  season's shortfall alongside its count (``shortfall_by_season``) and raises the conflict
  wherever more history could overturn the outcome (:func:`_detect_conflicts`). The policy
  can turn the detector off (``flag_keep_conflicts``), and then the keep rule is simply
  followed.

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


#: The one reason that is not a protection Reaper *checked*, but one it could not check:
#: the mirror does not span the hold, so "who is part-way through" has no answer. Named once
#: here so the producer (:func:`_protection_reason`) and the flag the caller reads off it
#: cannot drift apart -- rule 142's lesson, kept inside one module.
PROGRESS_UNESTABLISHABLE_REASON = "your watch history is too short to tell who is part-way through"


@dataclass(frozen=True)
class ProtectedSeason:
    season_number: int
    reason: str

    unestablishable: bool = False
    """Whether this season is held because the guard could not be *answered*, rather than
    because a protection fired.

    A typed flag, never the wording (rule 142). It is what lets
    :func:`services.season_scan.guard_result` mark the result ``blocked``: an unanswerable
    check is ``Unknown``, not a definite keep (rule 93), and a block is what holds a hand
    reap fail-closed. Emitting it as a plain PROTECT read as a definite value and left the
    season hand-reapable -- weaker, against a hand reap, than the keep-rule conflict it
    displaced, because ``verdict.STRUCTURAL_GATES`` does not carry this gate."""


def _because(kept_reason: str) -> str:
    """Turn a kept season's protection reason into a "because ..." clause, so a conflict
    names *why* the season it was compared against is being kept -- not just "your keep
    rule." Reads season_pruning's own closed vocabulary (the strings ``_protection_reason``
    returns); anything unrecognized falls back to a safe generic clause, never an error.

    The clauses restate their reason, they never sharpen it. The mid-binge one used to
    rewrite "part-way through the show" as "part-way through *it*", which moves the claim
    onto this particular season -- and ``sequential_protections`` may have picked this
    season precisely because it is the untouched NEXT one (``_anchor_positions`` returns
    ``_next_after``), so the same sentence could say nobody has played it and that someone
    is midway through it."""
    if kept_reason.startswith("episodes are missing"):
        return "episodes are missing from it"
    if kept_reason.startswith("the newest season of a show"):
        return "it is the newest season of a show that is still running"
    if kept_reason.startswith("the earliest season"):
        return "it is the earliest season on disk, so there is somewhere to start"
    if kept_reason.startswith("within the last") or kept_reason.startswith("this show has only"):
        return "it is one of the newest seasons your rule keeps"
    if kept_reason.startswith("a viewer is part-way"):
        return "a viewer is part-way through the show"
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
    #: can name the real protection ("episodes are missing") instead of a vague "keep rule."
    kept_reason: str

    shortfall: str | None = None
    """Why the watch mirror cannot settle this comparison, or ``None`` when it can.

    A third conflict shape, and the one that is *not* a comparison Reaper made: both counts
    are all-time, and a mirror that begins after a season arrived reports a lower bound for
    it. Where more history could overturn the outcome, the conflict is raised on the
    uncertainty rather than on the arithmetic, and this carries the reason in the operator's
    own words (``engine.gates.lifetime_shortfall``, the same sentence every other reader of
    a truncated count prints).

    Typed, not inferred from the wording (rule 142): ``season_scan.guard_result`` reads it
    exactly as it reads ``kept_watchers is None``, and both mean "we could not establish
    this". It used to decide whether a hand reap could overrule the block; it no longer
    does, because no blocked gate holds a hand reap (see ``engine.verdict``). What it
    decides now is which sentence the operator is shown and which chip sits beside it --
    still worth typing, because "Reaper could not tell" and "the rule lost the comparison"
    are different things to tell someone choosing what to delete."""

    @property
    def message(self) -> str:
        """The sentence the operator reads. All three shapes end the same way, and that is
        now the honest ending: a hand reap overrules every one of them.

        This closing phrase was briefly split. Two of the three shapes are conflicts Reaper
        could not settle, and while a blocked gate still held a hand reap they ended "Kept
        for now" instead, because inviting a decision the engine would refuse is rule 92's
        failure pointed at operator copy. ``verdict`` no longer works that way: only a
        structural stop (streaming now, unmanaged) holds a reap, so the invitation is real
        for all three and the split would now be the misleading half. See
        ``engine.verdict``'s module docstring.

        What survives the reversal, because it never depended on it: no shape states a
        watcher count it cannot stand behind. The shortfall arm is checked FIRST for that
        reason -- where the pruned count is a lower bound it is never printed, whichever
        other thing also went wrong with the pair.
        """
        viewers = "person" if self.pruned_watchers == 1 else "people"
        if self.shortfall is not None:
            # Deliberately ahead of the ``kept_watchers is None`` arm. A pruned count the
            # mirror cannot support reaches here with the kept count ALSO unreadable, and
            # that arm would print the bound as a measurement -- the one sentence this
            # whole reach fix exists to stop (rules 93, 140).
            return (
                f"Reaper cannot tell whether Season {self.pruned_season} is watched more "
                f"than Season {self.kept_season}, since {self.shortfall}. Season "
                f"{self.kept_season} is kept because {_because(self.kept_reason)}. "
                "Left for you to decide instead of removing it."
            )
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


def progress_is_establishable(*, reach_days: int, hold_days: int) -> bool:
    """Whether the watch mirror reaches back far enough to answer "who is part-way through".

    The guard holds a viewer whose last play of the show falls inside ``hold_days``. It can
    only see plays the mirror holds, and the mirror begins at its horizon, ``reach_days``
    back, so the answer is sound exactly when the mirror spans the whole hold window. Past
    ``reach_days`` an invisible viewer and an expired one are the same viewer and losing
    them costs nothing; *inside* it they are not, and the viewer simply has no rows.

    That gap is what this predicate exists to name. :func:`active_progress` reads no rows as
    "nobody is part-way through" -- a genuine ``Absent`` -- when the truth is "the mirror
    cannot see far enough to know", which is ``Unknown`` (rule 93). It is careful in the
    right way and cannot help: it keeps a viewer whose last-watched time is *unreadable*,
    but this viewer is not unreadable, they are missing, and there is nobody to keep. So the
    caller must ask this separately, and :func:`plan_series_prune` holds every season on
    disk when the answer is False.

    ``hold_days <= 0`` means the hold never expires, and no finite mirror supports an
    unbounded claim: a viewer whose every play predates the horizon is invisible at any
    reach, and with no expiry to make that harmless, the set is never establishable.

    ``in_progress_hold_days`` is not a bound on the mirror, it is the span the guard claims
    to cover, so a mirror shallower than it is exactly the unsupported claim rule 140 exists
    for. Pure: the reach is an argument, never measured here.
    """
    if hold_days <= 0:
        return False
    return reach_days >= hold_days


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

    A viewer the mirror never saw is a third case, and this function cannot reach it: they
    contribute no rows, so there is nobody here to keep and an empty result is
    indistinguishable from "nobody is part-way through". That one is answered separately by
    :func:`progress_is_establishable`, whose answer ``plan_series_prune`` takes.
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
    # Whether the watch mirror reaches far enough back for the mid-binge guard to have an
    # answer at all (``progress_is_establishable``). False holds every season on disk: the
    # viewer set is un-establishable, not empty. Defaults True because that is what a
    # default-configured operator has -- nothing is condemnable at all until the mirror
    # spans min_dormancy (1095 days), well past the 180-day default hold.
    progress_established: bool = True,
    keep_specials: bool = True,
    protect_incomplete: bool = True,
    flag_keep_conflicts: bool = True,
    airing_seasons: Collection[int] = (),
    # Season number -> all-time watcher count, or None for a season nobody could
    # measure (on disk, but never resolved in Plex). None is NOT zero: see
    # _detect_conflicts.
    watchers_by_season: Mapping[int, int | None] | None = None,
    # Season number -> why the watch mirror cannot support that season's count above, or
    # None where it can. The bound the counts are only answers under: they are all-time, so
    # each needs a mirror reaching back to the day that season arrived
    # (``engine.gates.lifetime_shortfall``, which the caller asks -- this module stays pure
    # and takes the answer). A season absent here is taken at face value, which is what a
    # caller passing no counts at all gets; see _detect_conflicts.
    shortfall_by_season: Mapping[int, str | None] | None = None,
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
    shortfall_by_season = shortfall_by_season or {}
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
    # The mirror-reach half of the same guard, and the same off-switch: an operator who
    # turned the guard off is making no claim for the mirror to fall short of. Where the
    # claim IS made and the mirror cannot support it, the seasons above are still held for
    # the viewers we CAN see -- they are real -- and everything else is held too, because a
    # viewer whose plays all predate the horizon leaves no trace to name a season with.
    progress_unestablished = keep_in_progress and not progress_established
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
            progress_unestablished=progress_unestablished,
        )
        if reason is None:
            prunable.append(n)
        else:
            protected.append(
                ProtectedSeason(
                    season_number=n,
                    reason=reason,
                    # Compared against the constant the producer returned, in this one
                    # module -- not a prefix test across a serialization boundary, which is
                    # the shape rule 142 forbids. What crosses to `guard_result` is the flag.
                    unestablishable=reason == PROGRESS_UNESTABLISHABLE_REASON,
                )
            )

    # The policy can silence the conflict detector; an empty list is exactly "no conflict
    # found", so auto_approvable and every downstream consumer behave as if none fired.
    conflicts = (
        _detect_conflicts(prunable, protected, watchers_by_season, shortfall_by_season)
        if flag_keep_conflicts
        else []
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
    progress_unestablished: bool = False,
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
    # Each reason names what was OBSERVED, not what it is usually a sign of. These read as
    # facts about the show on a panel the operator is checking against Sonarr, so a reason
    # that overstates its evidence is routinely wrong out loud (rule 21):
    #
    # * ``is_incomplete`` is ``wanted_episode_count > episode_file_count``, which
    #   ``clients.sonarr_stats`` documents as download *intent*, not a live queue. An ended
    #   show permanently missing one aired episode would say "still downloading" forever --
    #   the very case this module's docstring names as why the switch exists.
    # * ``airing_seasons`` returns the season to *treat as* airing: the newest
    #   content-bearing season of any series Sonarr calls running. A continuing show
    #   between seasons is the ordinary case, not an edge, and nothing is airing then.
    # * ``first_real`` is the minimum over seasons that HAVE files, so where season 1 was
    #   never downloaded or was deleted by hand, season 2 was called "the first season" and
    #   keeping it does not let anyone start the show.
    if protect_incomplete and season.is_incomplete:
        return "episodes are missing from this season"
    if n in airing:
        return "the newest season of a show that is still running"
    if keep_first_season and n == first_real:
        return "the earliest season on disk, so there is somewhere to start"
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
    # Last, so a season we can actually name a viewer for gets that sharper reason instead.
    # This one is the honest sentence for the rest: the mid-binge guard was asked a question
    # the watch history cannot answer, and "we could not tell" holds the season.
    if progress_unestablished:
        return PROGRESS_UNESTABLISHABLE_REASON
    return None


def _detect_conflicts(
    prunable: Sequence[int],
    protected: Sequence[ProtectedSeason],
    watchers_by_season: Mapping[int, int | None],
    shortfall_by_season: Mapping[int, str | None],
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

    **A count the mirror cannot support is a third state, and neither of the two above.**
    Both counts are all-time and the mirror begins at its horizon, so a season that arrived
    before it reports a LOWER BOUND rather than an answer -- and the truncation is not
    evenly spread, it falls on precisely the old seasons keep-last wants to prune, whose
    plays are the ones behind the horizon. Left unqualified, both sides read low, the pruned
    season's count reads 0, and the ``== 0`` skip below dropped the conflict entirely: a show
    that needed a human went auto-approvable and its old seasons became removable on score
    alone. Marking the truncated count *unreadable* instead fixes nothing, because ``None``
    on the pruned side reaches the same skip.

    So the reach is applied where it decides something, one pair at a time, by asking what
    survives the plays the mirror cannot see. More history can only ever RAISE a count, so:

    * the rule LOST the comparison (``pruned > kept``) -- definitive only where the kept
      count is an answer, since more history could lift it back above the pruned one;
    * the rule WON it -- definitive only where the pruned count is an answer, since more
      history could lift it above the kept one.

    Either outcome the bound already earns still stands on its own, exactly as
    ``fields._survives_more_history`` reads the operator's own rules; only the ones more
    history could overturn become conflicts, carrying ``shortfall``.

    **What that costs, stated plainly, because the reach is wide.** Two things do still
    clear on their own: a season the mirror covers for its whole life is compared on its
    real count, and a truncated count that ALREADY out-ranks an answered one still conflicts
    for the reason it always did. But a season the mirror does *not* cover conflicts against
    every kept season, whatever either count says -- its count is a lower bound, and more
    history can always lift a lower bound above anything. So wherever the watch history is
    shallower than the library is old, every prunable season of every affected show is held
    *and* refuses a hand reap, and TV pruning is inert until the mirror catches up. That is
    the prime directive's answer, since the alternative is deciding on two numbers Reaper
    knows are wrong -- but it is a blanket effect, not a narrow one, and an earlier draft of
    this docstring claimed the opposite (rule 7/24). ``docs/STATUS.md`` records it, and
    ``test_a_short_mirror_holds_every_prunable_season_of_an_old_show`` pins it, because the
    mutation that turns this into a true blanket hold otherwise passes the whole suite.

    ``shortfall_by_season`` empty means the caller stated no bound, and every count is taken
    at face value -- the pre-evidence pass in ``season_scan``, which passes no counts either
    and compares nothing, and tests stating exact counts. The one production caller that
    passes counts passes their shortfalls with them.
    """
    conflicts: list[PruneConflict] = []
    kept_seasons = [p for p in protected if p.season_number != SPECIALS_SEASON]
    for pruned in prunable:
        if pruned == SPECIALS_SEASON:
            continue
        pruned_watchers = watchers_by_season.get(pruned)
        if pruned_watchers is None:
            # Unreadable: there is nothing to compare FROM, so no conflict can be stated at
            # all. Kept as its own named case rather than folded in with 0 below, because
            # collapsing "we could not measure it" into "nobody watched it" is the bug the
            # docstring above records.
            continue
        pruned_shortfall = shortfall_by_season.get(pruned)
        if pruned_watchers == 0 and pruned_shortfall is None:
            # Read over a mirror that covers this season's whole life, and nobody watched
            # it: it cannot out-rank anything however the kept counts land, so there is no
            # comparison to make. A 0 the mirror CANNOT support says nothing of the kind and
            # goes on to the loop below.
            continue
        for kept in kept_seasons:
            kept_watchers = watchers_by_season.get(kept.season_number)
            if kept_watchers is None:
                conflicts.append(
                    PruneConflict(
                        pruned_season=pruned,
                        kept_season=kept.season_number,
                        pruned_watchers=pruned_watchers,
                        kept_watchers=None,
                        # The pruned season's own bound rides along even though the kept
                        # count is what could not be read. Both refuse the comparison and
                        # both hold, so the flag does not move -- but the MESSAGE does:
                        # without this, a pruned count the mirror cannot support is printed
                        # as "0 people watched Season N" beside a chip saying Reaper could
                        # not check, and the card denies itself. The skip above deliberately
                        # lets a truncated 0 reach this loop, so this arm is where it lands.
                        shortfall=pruned_shortfall,
                        kept_reason=kept.reason,
                    )
                )
                continue
            kept_shortfall = shortfall_by_season.get(kept.season_number)
            if pruned_watchers > kept_watchers:
                # The rule lost. Stated as the comparison it is where the kept count is an
                # answer; where it is a lower bound the hold is the same but the sentence
                # must not assert arithmetic against a number that can still move.
                shortfall = kept_shortfall
            elif pruned_shortfall is not None:
                # The rule won against a lower bound, which is not winning. This is the arm
                # the truncated mirror needs and the only one that was missing.
                shortfall = pruned_shortfall
            else:
                continue
            conflicts.append(
                PruneConflict(
                    pruned_season=pruned,
                    kept_season=kept.season_number,
                    pruned_watchers=pruned_watchers,
                    kept_watchers=kept_watchers,
                    kept_reason=kept.reason,
                    shortfall=shortfall,
                )
            )
    return conflicts
