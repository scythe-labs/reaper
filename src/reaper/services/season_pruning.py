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

* **Sequential-progression guard.** For every viewer part-way through the show, keep where
  they are: the season they are mid-way through, or, once they have finished it, the next
  one that has files. One or the other, never both -- both only when the episode counts are
  unreadable and ``_anchor_positions`` fails closed. This docstring claimed "and the next
  one" long after the guard became episode-precise, and a manual was nearly written from
  it: the sentence promised a cushion that only ``season_lookahead`` (default 0) supplies.
  Without the guard, "keep the last 2 seasons" deletes the season a user is mid-binge on. "Last
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
  (:func:`reaper.engine.gates.progress_is_establishable`). **Depth is not the only way to
  lose a viewer, and there are three ways in all.** A season whose plays stopped being
  readable takes its viewer's place in the show with it, on a mirror that spans the hold
  perfectly, so ``progress_unreadable`` holds every season the same way
  (``services.watch_evidence``); and a season with no Plex rating key at all this scan was
  never *asked* about, so ``progress_seasons_unmatched`` holds them for that too. All three
  hold the *siblings*, which is the point: the season nobody could read abstains on its own
  evidence, while the season a viewer is actually about to watch has fully readable facts of
  its own and would condemn at full confidence. One duplicate "Season 3" in Plex was enough
  to put the season a viewer had just finished season 3 for onto the reap list (#472).

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
from reaper.engine.reason import Reason, ReasonParam

#: Default seasons to protect BEYOND a viewer's current position while they binge. 0 keeps
#: exactly the season they are on (or the next one, if they finished the current). The policy
#: supplies the real value; this constant is only the fallback.
SEQUENTIAL_LOOKAHEAD = 0

#: Season 0 is specials: out-of-run content that is frequently the oldest and newest at
#: once. Never auto-pruned by default (the policy's ``keep_specials`` can allow it) and
#: never occupies a keep-slot either way.
SPECIALS_SEASON = 0


#: The reasons that are not a protection Reaper *checked*, but ones it could not check, so
#: "who is part-way through" has no answer. Named once here so the producer
#: (:func:`_protection_reason`) and the flag the caller reads off them cannot drift apart --
#: rule 142's lesson, kept inside one module.
#:
#: The four differ only in WHY the answer is missing, and each is worth its own sentence: the
#: first is fixed by holding more history, the second by repairing it at the source, the third
#: by fixing the season's Plex match and the fourth by fixing the show's, and copy that named
#: one cause for four would send the operator to the wrong place. That is #489: a show that
#: never bound to Plex was told its watch history was too short, which is true and is not the
#: wall it is standing at -- no depth of mirror can read a place in a show that has no address.
PROGRESS_UNESTABLISHABLE_REASON = "progress_history_short"
PROGRESS_UNREADABLE_REASON = "progress_plays_unreadable"
PROGRESS_UNMATCHED_REASON = "progress_season_unmatched"
PROGRESS_SHOW_UNMATCHED_REASON = "progress_show_unmatched"

#: Every reason above, for the caller that asks "was this held because we could not answer".
#: A membership test against the constants in this module, never a prefix test on the wording
#: across a boundary (rule 142); adding a fourth reason above means adding it here, and
#: ``tests/test_season_pruning.py`` fails on one that is missing.
UNANSWERABLE_REASONS = frozenset(
    {
        PROGRESS_UNESTABLISHABLE_REASON,
        PROGRESS_UNREADABLE_REASON,
        PROGRESS_UNMATCHED_REASON,
        PROGRESS_SHOW_UNMATCHED_REASON,
    }
)

#: The same four as the reason ids ``_protection_reason`` returns them under: a season held
#: because the guard could not be answered carries its cause as its "why kept" sentence, so
#: the id lives in the catalog's ``why.cause.*`` namespace and both slots share one entry.
_UNANSWERABLE_REASON_IDS = frozenset(f"cause.{r}" for r in UNANSWERABLE_REASONS)


@dataclass(frozen=True)
class ProtectedSeason:
    season_number: int
    reason: Reason

    unestablishable: bool = False
    """Whether this season is held because the guard could not be *answered*, rather than
    because a protection fired.

    A typed flag, never the wording (rule 142). It is what lets
    :func:`services.season_evidence.guard_result` mark the result ``blocked``: an unanswerable
    check is ``Unknown``, not a definite keep (rule 93), so the panel shows it amber
    ("couldn't check") rather than green ("checked and passed"), and the operator can see
    which of the two they are looking at.

    **That is now the only reason, and it is the durable one.** The flag was originally
    added for a second reason that has lapsed: a block used to hold a hand reap where a
    plain PROTECT on this gate did not, which made ``blocked`` the strictly stronger
    encoding. No blocked gate holds a hand reap any more (``engine.verdict``), so both
    encodings are equally overrulable and the choice is purely about what the operator is
    told. Do not read this flag as buying a reap refusal; it does not."""


#: The because-clause for each kept-season reason id. The clauses restate their reason,
#: they never sharpen it: the mid-binge one used to rewrite "part-way through the show" as
#: "part-way through *it*", which moves the claim onto this particular season -- and
#: ``sequential_protections`` may have picked this season precisely because it is the
#: untouched NEXT one (``_anchor_positions`` returns ``_next_after``), so the same sentence
#: could say nobody has played it and that someone is midway through it.
_BECAUSE_IDS: dict[str, str] = {
    "season_keep.incomplete": "because.incomplete",
    "season_keep.airing": "because.airing",
    "season_keep.first": "because.first",
    "season_keep.keep_last": "because.keep_last",
    "season_keep.keep_all": "because.keep_last",
    "season_keep.midbinge": "because.midbinge",
}


def _because(kept_reason: Reason) -> Reason:
    """Turn a kept season's protection reason into a "because ..." clause, so a conflict
    names *why* the season it was compared against is being kept -- not just "your keep
    rule." Keyed on the reason id, never its wording (rule 92); anything unrecognized
    falls back to a safe generic clause, never an error."""
    return Reason(_BECAUSE_IDS.get(kept_reason.id, "because.rule"))


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
    kept_reason: Reason

    shortfall: Reason | None = None
    """Why the watch mirror cannot settle this comparison, or ``None`` when it can.

    A third conflict shape, and the one that is *not* a comparison Reaper made: both counts
    are all-time, and a mirror that begins after a season arrived reports a lower bound for
    it. Where more history could overturn the outcome, the conflict is raised on the
    uncertainty rather than on the arithmetic, and this carries the reason in the operator's
    own words (``engine.gates.lifetime_shortfall``, the same sentence every other reader of
    a truncated count prints).

    Typed, not inferred from the wording (rule 142): ``season_evidence.guard_result`` reads it
    exactly as it reads ``kept_watchers is None``, and both mean "we could not establish
    this". It used to decide whether a hand reap could overrule the block; it no longer
    does, because no blocked gate holds a hand reap (see ``engine.verdict``). What it
    decides now is which sentence the operator is shown and which chip sits beside it --
    still worth typing, because "Reaper could not tell" and "the rule lost the comparison"
    are different things to tell someone choosing what to delete."""

    @property
    def message(self) -> Reason:
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
        common: dict[str, ReasonParam] = {
            "pruned_season": self.pruned_season,
            "kept_season": self.kept_season,
            "because": _because(self.kept_reason),
        }
        if self.shortfall is not None:
            # Deliberately ahead of the ``kept_watchers is None`` arm. A pruned count the
            # mirror cannot support reaches here with the kept count ALSO unreadable, and
            # that arm would print the bound as a measurement -- the one sentence this
            # whole reach fix exists to stop (rules 93, 140).
            return Reason("conflict.shortfall", {**common, "cause": self.shortfall})
        if self.kept_watchers is None:
            return Reason("conflict.unread", {**common, "pruned_watchers": self.pruned_watchers})
        return Reason("conflict.counted", {**common, "pruned_watchers": self.pruned_watchers})


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


def _with_lookahead(start: int, lookahead: int, on_disk: Collection[int] | None) -> set[int]:
    """``start`` plus the next ``lookahead`` seasons the viewer actually reaches.

    The same walk as :func:`_next_after`, repeated: a cushion counts seasons that hold
    files, never season numbers. ``start + offset`` landed it on numbers nothing holds as
    soon as a show's numbering had a hole, so a viewer part-way through a season got no
    cushion at all while the setting read as applied -- and the anchor itself was still
    held, which hid it. ``None`` keeps the old arithmetic for a caller that does not know
    what is on disk.
    """
    if on_disk is None:
        return {start + offset for offset in range(lookahead + 1)}
    return {start, *sorted(n for n in on_disk if n > start)[:lookahead]}


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

    Each anchor is then resolved by :func:`_anchor_positions` and extended by ``lookahead``
    further seasons that hold files (:func:`_with_lookahead`). ``include_specials`` adds
    Season 0 to the anchor candidates, for the
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
                protected |= _with_lookahead(start, lookahead, on_disk)
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

    A viewer the mirror never saw is a third case, and this function cannot reach it: they
    contribute no rows, so there is nobody here to keep and an empty result is
    indistinguishable from "nobody is part-way through". That one is answered separately by
    :func:`reaper.engine.gates.progress_is_establishable`, whose answer ``plan_series_prune``
    takes.
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
    # answer at all (``gates.progress_is_establishable``). False holds every season on disk: the
    # viewer set is un-establishable, not empty. Defaults True because that is what a
    # default-configured operator has -- nothing is condemnable at all until the mirror
    # spans min_dormancy (1095 days), well past the 180-day default hold.
    progress_established: bool = True,
    # The same gap reached by a different route: at least one of this show's seasons had its
    # plays go unreadable, so a viewer part-way through THAT season leaves no trace to name a
    # place with. The mirror's depth is fine; the rows are filed under a rating key the season
    # no longer carries (``services.watch_evidence``). ``progress_established`` cannot express
    # it, because the mirror does span the hold. True holds every season on disk, exactly as a
    # short mirror does, and for the identical reason: the viewer set is unreadable, not empty.
    progress_unreadable: bool = False,
    # The third route to the same gap, and the one that needs no history to have moved: at
    # least one content-bearing season of this show has no Plex rating key at all this scan, so
    # its plays were never queried and a viewer part way through THAT season is invisible. The
    # season itself abstains on its own Unknown facts; its siblings do not, and one of them is
    # the season that viewer is about to watch. Neither flag above expresses it -- the mirror
    # spans the hold and nothing fell, there is simply no address to read
    # (``gates.NO_KEY_REASON_IDS``). True holds every season on disk, for the
    # identical reason: the viewer set is unreadable, not empty.
    progress_seasons_unmatched: bool = False,
    # The whole show, rather than one of its seasons: nothing here bound to Plex at all, so
    # `key_to_number` is empty, no play was ever queried, and the guard below answers "is
    # anyone part way through this" having asked nobody. The three flags above all describe a
    # show that DID bind, so none of them fires, and the season fell through to the
    # short-mirror sentence -- true, and naming a remedy that cannot work while the show has
    # no address (#489). It holds nothing extra: every season already takes Unknown from its
    # own facts. It picks the sentence.
    progress_show_unmatched: bool = False,
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
    shortfall_by_season: Mapping[int, Reason | None] | None = None,
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
    # Same off-switch, same reasoning: an operator who turned the guard off is making no claim
    # for an unreadable play to undermine.
    progress_plays_unreadable = keep_in_progress and progress_unreadable
    # And the third, on the same switch for the same reason.
    progress_season_unmatched = keep_in_progress and progress_seasons_unmatched
    # The fourth, same switch again.
    progress_show_unbound = keep_in_progress and progress_show_unmatched
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
            progress_plays_unreadable=progress_plays_unreadable,
            progress_season_unmatched=progress_season_unmatched,
            progress_show_unbound=progress_show_unbound,
        )
        if reason is None:
            prunable.append(n)
        else:
            protected.append(
                ProtectedSeason(
                    season_number=n,
                    reason=reason,
                    # Compared against the reason ids the producer returned, in this one
                    # module -- not a prefix test across a serialization boundary, which is
                    # the shape rule 142 forbids. What crosses to `guard_result` is the flag.
                    unestablishable=reason.id in _UNANSWERABLE_REASON_IDS,
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
    progress_plays_unreadable: bool = False,
    progress_season_unmatched: bool = False,
    progress_show_unbound: bool = False,
) -> Reason | None:
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
        return Reason("season_keep.specials")
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
        return Reason("season_keep.incomplete")
    if n in airing:
        return Reason("season_keep.airing")
    if keep_first_season and n == first_real:
        return Reason("season_keep.first")
    if apply_keep_last and rank is not None and rank <= keep_last:
        if keep_last >= total_ranked:
            return Reason("season_keep.keep_all", {"total": total_ranked, "keep_last": keep_last})
        return Reason("season_keep.keep_last", {"keep_last": keep_last, "rank": rank})
    if n in seq_protected:
        return Reason("season_keep.midbinge")
    # Last, so a season we can actually name a viewer for gets that sharper reason instead.
    # These are the honest sentences for the rest: the mid-binge guard was asked a question the
    # watch history cannot answer, and "we could not tell" holds the season.
    #
    # Widest failure first, since that is the one whose remedy fixes the others as a side
    # effect: a mirror too short to span the hold is the operator's own setting, an unreadable
    # play is repaired at the source, and an unmatched season is the narrowest -- it names one
    # season to go look at.
    #
    # `progress_show_unbound` re-words whichever of the three fired; it never fires alone.
    # A show with no rating key anywhere is standing at a wall behind all three -- no depth of
    # mirror, no repair, no per-season match can read a place in a show that has no address --
    # so it is the sentence to say (#489). But saying it is ALL it does: making it hold on its
    # own would move every unmatched show out of the review queue's abstain lane and onto the
    # Protected page, which is the UX trade #486 declined and
    # `test_a_show_plex_never_matched_at_all_is_left_alone` pins. The seasons here are already
    # held by the cause that fired, and already abstain on their own Unknown facts otherwise.
    held_by = progress_unestablished or progress_plays_unreadable or progress_season_unmatched
    if not held_by:
        return None
    if progress_show_unbound:
        return Reason(f"cause.{PROGRESS_SHOW_UNMATCHED_REASON}")
    if progress_unestablished:
        return Reason(f"cause.{PROGRESS_UNESTABLISHABLE_REASON}")
    if progress_plays_unreadable:
        return Reason(f"cause.{PROGRESS_UNREADABLE_REASON}")
    return Reason(f"cause.{PROGRESS_UNMATCHED_REASON}")


def _detect_conflicts(
    prunable: Sequence[int],
    protected: Sequence[ProtectedSeason],
    watchers_by_season: Mapping[int, int | None],
    shortfall_by_season: Mapping[int, Reason | None],
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
    the price of not letting an unread number clear a hold.

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
    by the SCAN, and automatic TV pruning is inert until the mirror catches up. That is
    the prime directive's answer, since the alternative is deciding on two numbers Reaper
    knows are wrong -- but it is a blanket effect, not a narrow one, and an earlier draft of
    this docstring claimed the opposite (rule 7/24). ``docs/DECISIONS.md`` records it under
    **Watch-history reach**, and
    ``test_a_short_mirror_holds_every_prunable_season_of_an_old_show`` pins it, because the
    mutation that turns this into a true blanket hold otherwise passes the whole suite.

    **The hold is the scan's, and the operator still overrules it.** A hand reap on one of
    these seasons condemns (``condemned.reap_override_verdict_decoded``): a conflict is a
    question Reaper could not answer, not a protection that fired, and only a *fired*
    ``verdict.STRUCTURAL_GATES`` member refuses a reap since the #96 reversal deleted
    ``DEFERRABLE_BLOCK_GATES``. That is what makes a blanket hold bearable rather than a
    dead feature, and it is the half of this paragraph that used to say the opposite: the
    claim that these seasons refuse a hand reap predates the reversal and stopped being
    true the day it landed (rule 7/24).

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
