# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a season plan is decided from, frozen, and the one derivation off it.

``services.season_pruning.plan_series_prune`` is pure and takes nineteen arguments. Ten of
them are the operator's policy; the rest are evidence a scan gathered. This module is the
seam: :class:`SeasonPruneInput` holds the evidence half for one show, :func:`plan_from_frozen`
supplies the policy half and calls the real planner, and the scan freezes the bundle so the
policy simulator can call the same function again under an edited policy. Two pure derivations
off that plan sit here for the same reason the planner call does. :func:`guard_result` turns one
season's verdict into the gate result the why-panel reads, and the scan and the replay both call
it, so there is no second implementation to drift. :func:`no_key_reason` is called by the scan
alone: it produces ``progress_unknown_reason``, which is :func:`guard_result`'s input and is
frozen onto the bundle, so the replay reads the scan's wording rather than re-deriving it off a
live match status. That is the same "one derivation, not two" this module exists for, reached
from the freeze side.

**One derivation, not two.** Every plan whose result is *stored* -- the scan's and the
simulator's alike -- reaches ``plan_series_prune`` through :func:`plan_from_frozen`, so the
exactness the simulator claims is structural rather than measured: there is no second
implementation to drift (rule 3/22, and ``docs/LEARNINGS.md``'s "Two code paths answering one
question will drift"). The pieces that turn a policy number into a planner argument -- the
mid-binge expiry, the mirror-reach predicate, the keep-last scope -- live here for the same
reason, since each was a place the two could have disagreed.

``season_scan.gather``'s offline first pass still calls ``plan_series_prune`` directly, and
that is not an exception to the above: it runs before any watch evidence exists, its answer
decides nothing (a log line and the fully-kept count), and its own docstring says so.

**A missing episode map is ``None``, never ``{}``.** ``season_final_episode`` is read from
Sonarr only while ``keep_in_progress`` is on, and an empty dict is what a show whose episodes
were read and found nothing carries, so the field is three-state rather than defaulted
(rule 93). Two different absences land on ``None`` and
:attr:`SeasonPruneInput.episodes_unreadable` separates them: the guard was off and nobody was
asked, or Sonarr was asked and did not answer. Only the first is unanswerable. In the second
the scan planned from the empty map itself, so a replay off that map returns the scan's own
verdicts -- and refusing over it cost the operator every TV preview until Sonarr answered for
a whole scan (#500). :func:`missing_episode_map` is what the route asks before it previews an
edit that turns the guard on.

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
from reaper.engine import identity
from reaper.engine.gates import ABSTAIN as GATE_ABSTAIN
from reaper.engine.gates import PROTECT as GATE_PROTECT
from reaper.engine.gates import GateId, GateResult, progress_is_establishable
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
    """The highest episode on disk per season, or ``None`` when this scan did not read one.

    Three-state on purpose: see the module docstring. ``None`` covers both a scan that never
    asked and a Sonarr read that failed; ``episodes_unreadable`` below says which."""

    episodes_unreadable: bool
    """Whether the episode fan-out ran for this show and Sonarr did not answer.

    The one bit that tells the two ``None`` maps apart. The scan planned from ``{}`` when
    this is set (:func:`plan_from_frozen`'s ``or {}``), so a replay off the same empty map
    reproduces what it decided, and :func:`missing_episode_map` lets that show through."""

    watchers_by_season: Mapping[int, int | None]
    shortfall_by_season: Mapping[int, str | None]
    progress_unreadable: bool
    progress_seasons_unmatched: bool
    progress_unknown_reason: str | None
    """Why nobody could be asked about this show's viewers, or ``None``.

    Its wording goes to :func:`guard_result`. Whether it is set at all also goes to
    the planner, as ``progress_show_unmatched``: a show with no rating key anywhere is the one
    shape whose mid-binge hold must not blame the watch mirror's depth (#489)."""

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
        # Non-None is exactly "this show has no Plex rating key" (`season_scan` derives it as
        # `no_key_reason(...) if item.show_rating_key is None else None`), which is the one
        # state where no depth of watch mirror can name a viewer's place. The planner takes
        # the bit rather than the sentence, so the wording stays operator copy (rule 142).
        progress_show_unmatched=inp.progress_unknown_reason is not None,
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
    sequential guard before the map is touched. A draft with it *on* over a bundle whose scan
    never asked would silently fall back to whole-season protection, which reads as a
    confident preview of an answer nobody gathered.

    A read Sonarr refused is not that state, which is what ``episodes_unreadable`` is for.
    The scan planned from the empty map and every verdict in the snapshot was decided from
    it, so a replay off the same empty map returns those verdicts. What it diverges from is a
    healthy re-scan, by exactly whole-season protection where episode-precise protection
    would have stood, which keeps more (rule 31). The refusal it replaces was whole-lane, so
    one show Sonarr would not answer for blanked every TV preview until a scan in which every
    show's read succeeded (#500).
    """
    return (
        policy.keep_in_progress and inp.season_final_episode is None and not inp.episodes_unreadable
    )


def guard_result(
    plan: SeriesPrunePlan, season_number: int, *, progress_unknown_reason: str | None = None
) -> GateResult:
    """Translate the season-pruning verdict for one season into a gate result.

    Four outcomes, mapped onto the gate vocabulary the why-panel already speaks:

    * **Protected by a guard** -> ``PROTECT``. Beats the score, like any gate.
    * **In a keep-rule conflict** (prunable by the rule, but more-watched than a season
      the rule keeps) -> a *blocked* ABSTAIN. ``blocked`` forces the whole item to
      abstain, which is exactly right: the rule is fighting the evidence, so a human must
      look. It renders amber, not green.
    * **Prunable, on a show that never bound to Plex** (``progress_unknown_reason``) -> a
      *blocked*, ``unestablishable`` ABSTAIN. Nothing is held on it: with no rating key
      anywhere every season already abstains on its own Unknown facts, and there is no
      readable sibling to endanger, which is why #485 scoped the hold away from here. What
      it corrects is the sentence. The mid-binge check asked nobody, and reporting that as
      a pass sat one fold above four gates saying the opposite on the same season (#486).
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
                # The same fact, carried to the panel rather than left to be inferred from
                # the verdict. This row reaches `protections_unknown` too, and the panel's
                # conflict branch skipped it only because a fired protection makes the
                # verdict `protect` and an earlier branch returns first (rule 142).
                unestablishable=protected.unestablishable,
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

    if progress_unknown_reason is not None:
        # Last, so a real protection and a real conflict both still win: this arm says only
        # that nobody could be asked, and either of those is something Reaper found. Neither
        # can co-occur with it in practice -- `_detect_conflicts` skips a season whose watcher
        # count is None, and every count is None when no season carries a rating key -- but
        # the order is what makes that safe rather than the coincidence.
        #
        # Worded as the `could not check {what}: {cause}` shape `engine.gates._blocked`
        # produces, on the SAME cause string this season's four Plex-dependent gates carry, so
        # the panel folds all five into one box naming the cause once instead of opening a
        # second box that says it again (`WhyPanel.LeftForYou`, rule 144).
        return GateResult(
            GateId.SEASON_PROGRESSION,
            GATE_ABSTAIN,
            blocked=True,
            unestablishable=True,
            detail=f"could not check who is part-way through it: {progress_unknown_reason}",
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
#: there. Two maps rather than one shared with the movie lane because the subjects differ
#: ("this season" against "this item", "this show" against "this title").
_NO_KEY_REASONS: dict[identity.MatchStatus | None, str] = {
    identity.MatchStatus.UNMATCHED: "Plex has not matched this season",
    identity.MatchStatus.AMBIGUOUS: "more than one Plex item matches this show",
    identity.MatchStatus.CONFLICTED: "Plex and Sonarr describe this show differently",
}


def no_key_reason(show_match_status: identity.MatchStatus | None) -> str:
    """Why this season has no Plex rating key, in the operator's words.

    One derivation for both readers (rule 104): ``season_scan.build_season_facts`` stamps it
    on every Unknown observation, and ``season_scan._judge_series`` hands the same string to
    the mid-binge guard so the panel groups all of them under one cause. Two ``.get`` calls
    with two fallbacks is how the guard's sentence would come to name a different cause from
    the four gates printed beside it.
    """
    return _NO_KEY_REASONS.get(show_match_status, "Plex has not matched this season")


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
    "episodes_unreadable": "finals_unread",
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


#: ``SeasonStats``' fields, mirrored by hand below because the stored shape has to survive a
#: rename. Checked at import for the same reason ``_KEYS`` is (rule 103), and it is the
#: sharper of the two: a field added to ``SeasonStats`` **with a default** would thaw as that
#: default in silence, and the replay would then plan from a season that is not the one the
#: scan measured. Without a default it raises and the bundle is refused, which is fine.
_SEASON_FIELDS = frozenset(
    {
        "season_number",
        "monitored",
        "episode_file_count",
        "size_on_disk",
        "total_episode_count",
        "wanted_episode_count",
    }
)

_DECLARED_SEASON = {f.name for f in dataclasses.fields(SeasonStats)}
if _SEASON_FIELDS != _DECLARED_SEASON:
    raise RuntimeError(  # pragma: no cover - import-time drift guard
        "SeasonStats fields changed: "
        f"{_SEASON_FIELDS ^ _DECLARED_SEASON}. Carry each through _season_to_dict and "
        "_season_from_dict, or a replay plans from a season the scan did not measure."
    )


def _epoch(value: datetime | None) -> int | None:
    """Whole seconds, matching how every timestamp in this codebase crosses a boundary.

    Sub-second precision is lost, and only ``now`` actually carries any -- Tautulli's values
    are already whole seconds. The truncation moves ``active_progress``'s cutoff *earlier*,
    so a replay keeps a viewer the scan expired at a sub-second boundary rather than the
    reverse, which is the keep direction (rule 31).
    """
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
        _KEYS["episodes_unreadable"]: inp.episodes_unreadable,
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

    Raises on anything it cannot read, rather than defaulting a member. There is no safe
    default here: every one of these is evidence, and a missing key means the scan did not
    record it, which is a refusal (rule 93). The caller catches and refuses to preview -- see
    ``api.routes._SeasonReplay``, which catches ``OSError`` and ``OverflowError`` alongside
    the obvious three, because ``from_epoch`` ends in ``datetime.fromtimestamp`` and that
    raises ``OSError`` for an out-of-range epoch. Do not narrow this to a list: the contract
    is that a payload this cannot read never becomes a plan, not that it fails in three
    named ways.
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
        episodes_unreadable=bool(d[_KEYS["episodes_unreadable"]]),
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
