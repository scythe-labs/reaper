# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gates: protections that cannot delete anything.

A gate has no way to condemn a file. ``evaluate`` returns only ``PROTECT`` or
``ABSTAIN``, and ``mypy --strict`` enforces that no other value can compile. No
misconfiguration, no null, no server error, no typo, and no future change can make a
protection delete a file. The worst a broken gate can do is fail to protect, which
is why gates also fail closed on ``Unknown`` input.

In Maintainerr, Janitorr, Deleterr, and Reclaimerr, protections live inside the same
boolean expression as the reasons to delete, so a protection is just another clause
in one big OR. There, an unknown value, a failed API call, or a mis-set option can
silently disarm a protection. Reaper puts protections in a separate lane with a
separate type instead, so that cannot happen here.

Two outcomes, and a third thing that is not an outcome:

* ``PROTECT``: a reason to keep this file. Always beats the score.
* ``ABSTAIN``: this gate has nothing to say. It did not fire.
* An ``Unknown`` input never produces a verdict at all. It raises the item's
  ``blocked`` flag, which forces the whole evaluation to ABSTAIN. Not being able to
  check a protection is treated as though the protection might have fired.
"""

from __future__ import annotations

import enum
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from reaper.engine import identity
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.engine.reason import Reason
from reaper.ratings import (
    Rating,
    RatingSource,
    is_percentage_source,
)

GateOutcome = Literal["PROTECT", "ABSTAIN"]
PROTECT: GateOutcome = "PROTECT"
ABSTAIN: GateOutcome = "ABSTAIN"

#: The safe default for the custom-rule fact fields below. A shared, immutable ``Absent``
#: singleton: ``Absent`` never matches a condition that would delete a file and never
#: protects one, so an omitted field cannot make a rule condemn or a gate fire.
#:
#: It is not harmless on the keep side, though. A graded keep rule reads ``Absent`` as "we
#: looked, there is genuinely none" and grants no discount to the score, where ``Unknown``
#: grants the full discount (``signals.evaluate_keep``). So an omitted field leaves the
#: score where an honest read failure would have lowered it. The live scan builders set
#: every one of these fields explicitly, and mark anything they could not read as
#: ``Unknown`` instead of this default.
_UNSET: Absent = Absent(source="unset")


class GateId(enum.StrEnum):
    WHITELISTED = "whitelisted"
    """Retired as a gate. List membership now protects through the operator's own keep
    rules (the ``on_list`` field), one rule per list, at either strength. Unlike the two
    retirements below, this one was a live protection, so it is not in
    ``PolicyBody.RETIRED_GATES``, since silently dropping it would withdraw real cover.
    The ``convert_list_protections`` shim rewrites a stored body's gate row into the
    equivalent ``on_list`` rules instead. Kept so stored explanations still decode."""

    STREAMING_NOW = "streaming_now"
    RATING_FLOOR = "rating_floor"
    SERVER_POPULARITY = "server_popularity"

    OTHERS_WATCHING = "others_watching"
    """Retired. No gate implements it, and no fact builder gathers the count it would
    need (see the note near the bottom of this module). Kept only so an explanation
    stored while it was still built can still decode; ``scan_runner.GATE_TYPES``
    refuses to build it."""

    CURATED_LIST = "curated_list"
    """Retired as a gate, converted the same way as ``WHITELISTED``: an IMDb list now
    protects through an ``on_list`` keep rule naming it, so its strength is the
    operator's own choice per list rather than one switch over every list at once."""

    DATA_HORIZON = "data_horizon"

    UNMANAGED = "unmanaged"
    """Retired. The candidate set is built by asking Sonarr and Radarr what they hold,
    so every fact builder writes ``Known(True)`` for this field and the gate could never
    fire (see the note near the bottom of this module). Kept only so an explanation
    stored while it was still built can still decode; ``scan_runner.GATE_TYPES``
    refuses to build it."""

    MIN_DORMANCY = "min_dormancy"
    """The most important gate. Nothing under the dormancy floor may be deleted at
    all, whatever else it scores. See MinDormancyGate."""

    REWATCH_ODDS = "rewatch_odds"
    """Opt-in, on both movie and TV policies: keep anything whose dormancy cohort gets
    watched again at or above the operator's chosen percentage. See RewatchOddsGate.
    Every policy body carries this row (``PolicyBody._rewatch_odds_row``), each reading
    its own frozen cohort: a movie's own, or a season's show's."""

    RETURNED = "returned"
    """Opt-in, on both movie and TV policies: hold a title that left the library and
    came back. A return is the clearest evidence Reaper can get that removing something
    was wrong. See ReturnedGate."""

    SEASON_PROGRESSION = "season_progression"
    """Not authorable in a policy. The engine emits it from the season judgment
    (``season_evidence.guard_result``). No policy row builds it."""

    CUSTOM = "custom"
    """Not built from a gate row. Tags an operator-authored protect condition: one
    ``fields.CustomProtectGate`` per ``protect_conditions`` entry, built in
    ``scan_runner.build_gates``. Each can only return PROTECT or ABSTAIN.
    The operator's other two keep kinds carry different ids: ``graded_keeps`` is a
    score discount through ``keep_configs()`` and builds no gate, and
    ``keep_rating_rules`` tags ``RATING_FLOOR``. ``custom_condemn`` is the removal side
    and reaches no gate."""


#: The gate ids a policy body may carry: exactly the ones ``scan_runner.build_gates`` can
#: construct from a policy row. Every other member of ``GateId`` is either retired (kept
#: only so a stored explanation still decodes, see ``PolicyBody.RETIRED_GATES``) or emitted
#: by the engine itself with no policy row behind it.
#:
#: Declared here rather than derived from ``GATE_TYPES``, because the save boundary
#: (``api.schemas.GateSettingIn``) is a leaf that must not import the scan stack. Instead,
#: ``tests/test_policy.py`` pins this set against ``GATE_TYPES`` plus the explicitly-built
#: ``RATING_FLOOR``, so adding a gate and forgetting this list fails a test rather than
#: quietly making the gate unauthorable.
POLICY_AUTHORABLE_GATES: frozenset[GateId] = frozenset(
    {
        GateId.STREAMING_NOW,
        GateId.RATING_FLOOR,
        GateId.SERVER_POPULARITY,
        GateId.DATA_HORIZON,
        GateId.MIN_DORMANCY,
        GateId.REWATCH_ODDS,
        GateId.RETURNED,
    }
)


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's outcome, and the numbers behind it.

    ``detail`` is the operator-facing explanation: a typed
    :class:`~reaper.engine.reason.Reason` the frontend composes from its catalog. Every
    gate that was checked and did not fire still reports its actual figures, because
    "protections checked that did not fire, with the numbers" is what makes a deletion
    trustworthy.
    """

    gate: GateId
    outcome: GateOutcome
    detail: Reason

    blocked: bool = False
    """True when the gate could not be evaluated at all (an ``Unknown`` input).

    The panel shows this as amber, distinct from the green "checked and did not fire."
    Treating a check that could not run the same as one that ran and passed is exactly
    the mistake this field exists to prevent.
    """

    defers_to_owner: bool = False
    """Only meaningful on a ``blocked`` result: marks a deliberate "the owner should
    decide" block, as against a source Reaper simply could not read.

    A blocked gate never holds a hand reap by itself. Only a fired structural stop does
    (see ``engine.verdict``'s module docstring for why). What this flag does is tell the
    operator's copy which of two kinds of block they are looking at.
    ``season_evidence.guard_result`` sets it when a keep-rule conflict was a comparison
    Reaper could actually make: a readable ``kept_watchers`` count, and a ``shortfall`` of
    ``None``. A readable count does not rule out a shortfall on its own, since a count
    drawn from a history that does not reach back to when the season arrived settles
    nothing. ``api.review._chip`` reads this flag to choose between "this was watched more
    than a season your rule keeps" and "couldn't check who watched these seasons," which
    are different things to tell someone deciding what to delete.

    The ``False`` default matters here too: a producer that forgets to set this says
    "Reaper did not establish this," the answer that claims less. It is a typed field
    rather than something inferred from the detail text, since a wording-based check
    could not reliably match the one message it needed to.
    """

    unestablishable: bool = False
    """Only meaningful on a ``blocked`` result: this check never ran at all, as against
    one that ran and left its answer for the operator to decide.

    Set only by the season guard (``services.season_evidence.guard_result``), the one
    producer whose blocked results are not all of one kind, and read by
    ``WhyPanel.keepRuleConflict``. A keep-rule conflict made the comparison and found the
    rule disagreeing with the evidence, which is a decision waiting for a person. The same
    guard on a show Plex never matched asks nobody, since with no rating key there is
    nothing in the show to read; the four Plex-dependent gates beside it already explain
    why. ``blocked`` is true in both cases, so it cannot be what tells them apart.

    This also covers the guard's third shape: a season protected because the check could
    not be answered at all (``season_pruning.ProtectedSeason.unestablishable``).

    Every other producer leaves this ``False``, since an ordinary gate's blocked result
    has only one shape and needs no further label.

    The wire format carries a third state, ``None``, for a row saved before this flag
    existed, since nothing in such a row says which shape it was. The panel reads that
    ``None`` the same way it reads ``False``.
    """

    @property
    def fired(self) -> bool:
        return self.outcome == PROTECT


def thaw_defers_to_owner(value: object) -> bool | None:
    """Read ``defers_to_owner`` back from a stored explanation: three states, no guessing.

    This is the one place that decodes the raw stored value, so every reader agrees on
    what it means. Accepting only an exact ``True`` or ``False`` as the flag matters: a
    looser bool conversion would also accept values like ``1`` or ``"true"``, and would
    then have to reject something like ``2`` or ``"banana"`` by failing the whole read,
    which can blank out signals, protections, and the threshold on a panel that would
    otherwise render fine.

    So this function accepts only an exact ``True`` or ``False`` as the flag. Anything
    else, including a missing key or an unreadable value, comes back as ``None``, the
    state this field already uses to mean "nothing here can tell a comparison Reaper made
    apart from one it refused." The fallback on an unreadable value must assert less than
    the data justifies, never more, and it must not cost the operator evidence that is
    still readable elsewhere in the same row.
    """
    return value if value is True or value is False else None


class Gate(Protocol):
    """Any protection.

    Note the return type. There is no way to spell "delete this".
    """

    @property
    def id(self) -> GateId:
        # Read-only: a gate's identity never changes, and every implementation is a frozen
        # dataclass, so the protocol asks only that the id be *readable*.
        ...

    def evaluate(self, facts: Facts) -> GateResult: ...


@dataclass(frozen=True, slots=True)
class Facts:
    """Everything known about one item, as three-way observations.

    This is deliberately not a bag of raw values. An int here could mean zero, or could
    mean "we could not look," and those two must never collapse into the same number.
    """

    title: str
    days_observed_unwatched: Observation[float]
    """Days since the last play, or if never played, days since
    ``max(added_at, history_begins_at)``. Unknown when it has neither: with no play and
    no arrival date there is nothing to measure from, and ``dormancy.reference_instant``
    returns no instant rather than inventing one.

    This is derived rather than read directly, because "days since last play" is null
    for exactly the items we care about most. Code that treats that null as zero makes
    the item look unwatched since 1970, roughly five decades of dormancy, the strongest
    possible case for deleting it. The item nobody has watched would become the top
    deletion candidate for the wrong reason, and would look completely certain about it.
    """

    distinct_watchers: Observation[int]
    """Distinct watchers within the policy's popularity window.

    This is windowed on purpose. On a long-lived server, nearly every title has been
    watched by someone eventually. Measured against real history, an all-time watcher
    count protected almost the whole library and left the scorer with almost nothing to
    condemn at any threshold. Only a fraction of those titles still have watchers within
    the last year.

    An all-time count would protect a film that five people watched years ago and nobody
    has touched since, which is exactly the film Reaper exists to find. Popularity has
    to mean popular lately, so there is deliberately no way to ask for an all-time count
    here.
    """

    distinct_watchers_all_time: Observation[int]
    """Kept for display only. Never gate on it; see the field above."""

    size_bytes: Observation[int]
    imdb_rating_tenths: Observation[int]
    """Tenths: 7.5 is stored as 75, since floats do not serialize consistently and the
    policy hash must be exact."""
    imdb_votes: Observation[int]
    season_rank: Observation[int]
    """1 means the newest season that has files on disk. Computed over seasons with
    files on disk only. Sonarr's episodeCount reflects what is planned to download, not
    what actually exists, so using it here would rank seasons wrong."""

    is_streaming_now: Observation[bool]
    is_managed: Observation[bool]
    in_curated_list: Observation[str]
    """The IMDb-kind lists holding this item, joined by commas. Superseded by
    ``on_lists``, which every current reader uses. Still populated so a replayed stored
    snapshot, and the retired ``curated_list`` gate's stored explanations, keep their
    meaning."""
    is_whitelisted: Observation[bool]
    """Whether any list the operator curates by hand (a tag list, a collection, the
    watchlist) holds this item. The ``whitelisted`` field reads it. The gate that used
    to read it is retired."""

    # --- fields an operator can use in a custom rule -----------------------------------
    # These default to a safe value so a Facts built outside a scan (a test fixture, a
    # snapshot read back from storage) does not have to set every one of them. The live
    # scan builders must still set them explicitly: ``Absent`` is safe on the condemn and
    # gate lanes, but not on the keep lane. See ``_UNSET``.
    requested: Observation[bool] = _UNSET
    """Was this title requested through Seerr? Three states: ``Unknown`` when Seerr is
    not configured, only partly configured, or the item has no id to match against it.
    Never treated as ``False``, which would raise the deletion score on missing data.
    Set in ``build_facts`` and ``build_season_facts``."""

    genres: Observation[str] = _UNSET
    """The *arr's genres, joined by commas. ``Absent`` when the payload carries none."""

    on_lists: Observation[str] = _UNSET
    """Every protection list holding this item, joined by commas, using the operator's
    own names for each list regardless of its source. Feeds the ``on_list`` field.
    Defaults like the fields above. A snapshot saved before this field existed reads
    back as un-checkable, never confirmed as on no list."""

    release_age_days: Observation[float] = _UNSET
    """Days since the title's release. Derived, since age combines with dormancy in
    scoring. ``Absent`` for seasons today, which have no clean per-season release
    date."""

    quality: Observation[str] = _UNSET
    """The file's quality and resolution name (for example, "Bluray-1080p"). Movies
    only today."""

    show_ended: Observation[bool] = _UNSET
    """For TV: has the series ended, rather than still returning? ``Absent`` for
    movies, the same choice as ``season_rank`` above, so it never condemns and never
    protects where it does not apply."""

    # --- how far the evidence itself reaches ------------------------------------------

    history_reach_days: Observation[float] = _UNSET
    """How many days of watch history existed when this item was judged.

    How far back the evidence behind every windowed count actually reaches
    (``services.history_sync`` calls this the reach). ``distinct_watchers`` only gives a
    complete answer while this reach covers the policy's window. Below that, a count
    under the floor is a lower bound: the plays it cannot see are exactly the ones that
    would have protected the file. ``ServerPopularityGate`` fails closed instead of
    making a claim about a year of history when it only saw three months.

    Defaults like the custom-rule fields above, so older or hand-built Facts do not have
    to set it, and reads as un-checkable unless ``Known``. That is the keep direction,
    and what a snapshot saved before this field existed reads back as.
    """

    days_since_added: Observation[float] = _UNSET
    """Days since the item arrived on the server: the span an all-time count needs to
    cover.

    ``distinct_watchers`` needs the watch history to cover the policy's window.
    ``distinct_watchers_all_time`` needs it to cover the item's whole life here, which is
    this number. Only with both can a count be read as complete, rather than as a lower
    bound (``fields.reach_shortfall``).

    ``days_observed_unwatched`` looks like it could stand in for this, but cannot.
    Dormancy is deliberately clamped to the edge of the watch history:
    ``dormancy.reference_instant`` measures a never-played item from
    ``max(added_at, horizon)``, a played item from a play the history by definition
    holds, and an item with neither returns no number at all. So dormancy is never
    larger than ``history_reach_days``, and comparing the two would call every all-time
    count complete when it is not. This field is measured from the arrival date itself,
    so it is free to exceed the reach, which is exactly the case that must fail closed.

    Defaults like ``history_reach_days`` above, and reads the same way: anything but
    ``Known`` means "cannot establish", the keep direction.
    """

    # --- rewatch, stage 1 ---------------------------------------------------------------

    rewatch_viewings: Observation[int] = _UNSET
    """How many qualified viewings this title has had, all time, across every user. A
    viewing is a cluster of qualified plays, defined by
    ``services.rewatch.viewing_count``. Exists for the built-in habitual-rewatch keep
    rule, and is not offered as a value an operator can write a custom rule against.
    Movies only today; the season lane sets it ``Absent``
    (``season_scan.build_season_facts``).

    Defaults like the fields above, and reads the same way: anything but ``Known`` never
    condemns and never argues for keeping the file."""

    rewatch_last_play_days: Observation[float] = _UNSET
    """Days since the most recent qualified play, at scan time. This is the raw, saved
    input. Whether this and ``rewatch_viewings`` add up to a habitual-rewatch keep is a
    policy-configurable bar (a viewing floor, a recency window), decided in
    ``engine/signals.py``. That lets an operator's threshold edit replay against these
    saved facts in the simulator without a re-scan. Four states:

    * ``Known(n)``: the watch history was read and at least one qualified play exists.
    * ``Absent``: the watch history was read and this movie has no qualified play at
      all. We looked, and there is genuinely nothing to measure from.
      ``rewatch_viewings`` is ``Known(0)`` alongside it.
    * ``Unknown``: the watch history could not be read for this item, such as when it
      has no Plex key or its watch history is blocked. Never a measured absence.
    * The season lane sets this ``Absent`` too (``season_scan.build_season_facts``),
      since it has no validated TV rewatch answer yet.

    Defaults like the fields above. A snapshot saved before this field existed reads
    back as ``Unknown``, never as a false "checked, nothing there"."""

    # --- rewatch cohort, stage 2 ----------------------------------------------------------

    rewatch_cohort_n: Observation[int] = _UNSET
    """How many candidates in the same dormancy block as this item were tracked by the
    stage 2 rewatch-probability fit: the block's cohort size.

    Saved raw. The display floor and the withhold decision are made downstream by
    consumers, against ``REWATCH_BLOCK_FLOOR_N`` and ``rewatch.cohort_block``, so a thin
    block still saves ``Known`` at its small ``n`` rather than pretending it was not
    measured. That lets the opt-in protective hold, and the simulator, replay exactly
    against these saved counts, the same reason ``rewatch_last_play_days`` above is
    saved raw rather than pre-judged.

    ``Known`` only when the current dormancy is Known and the fit found a usable block
    for it. ``Unknown`` otherwise, such as no Plex key, blocked watch history, unknown
    dormancy, a value outside the fitted range, a dropped bucket, or a block withheld
    for too little history. Never ``Absent``, unlike the stage 1 pair above: a candidate
    this scan measured always has an opinion about its own dormancy block, even when
    that opinion is "cannot say." Both lanes save it the same way: a movie its own block
    (``services.snapshot.build_facts``), a season its show's block, off the same TV
    curve the season task fits (``services.season_scan._judge_series``). ``Absent``
    means hand-built Facts that never gathered a curve at all.

    Defaults like the fields above, and reads the same way: anything but ``Known``
    never condemns and never argues for the hold."""

    rewatch_cohort_k: Observation[int] = _UNSET
    """How many of ``rewatch_cohort_n`` were watched again inside the fit's outcome
    window: the block's watched-again count. Same block, same fit, and the same Known,
    Unknown, and Absent states as ``rewatch_cohort_n`` above. The rate is ``k / n``,
    computed on demand and never stored separately."""

    # --- a title that came back -----------------------------------------------------------

    returned_days_ago: Observation[float] = _UNSET
    """Days since Reaper recorded that this title left the library and came back.

    The clock the hold counts down from, saved at scan time like every other span here.
    Read from ``db.LibrarySeen.returned_at``, which the scan that detects the return
    writes, and every scan after it reads back. A return is only visible for one scan,
    so this fact has to be stored rather than recomputed (``services.library_seen``).

    Three states, and the third is not "no return":

    * ``Known(n)``: the ledger holds a return for this title's external id.
    * ``Absent``: the ledger holds a row for it and no return. We looked, and there is
      genuinely nothing: the ordinary state of a title that has sat in one place.
    * ``Unknown``: there was nothing to look up, such as no Plex match, no external
      id, or a title Reaper is seeing for the first time. Never a measured absence.

    Defaults like the fields above. A snapshot saved before this field existed reads
    back as ``Unknown``."""

    returned_by_reaper: Observation[bool] = _UNSET
    """Whether Reaper's own journal says it removed this title before the return.

    Chooses which sentence the operator reads and nothing else. The hold lasts the same
    length either way, since splitting it into two would mean a second setting for a
    difference nobody has measured. ``Known(False)`` is a real answer: Reaper has no
    record of removing it, so the operator did, or something else did. ``Absent`` next
    to a ``Known`` ``returned_days_ago`` cannot happen, since both fields are written
    together (``services.library_seen.record``)."""

    ratings: tuple[Rating, ...] = ()
    """Every readable rating the scan saved for this item, one per source (IMDb, TMDb,
    Rotten Tomatoes critics and audience, Metacritic). Read only by the multi-source
    ``RatingFloorGate``, a protection, so a missing or unreadable source can only ever
    fail to keep a title, never condemn one. Empty by default, which just means the
    gate does not fire. Not part of the policy hash, since the hash covers the rules
    the operator set, and Facts is the evidence a scan gathered."""


@dataclass(frozen=True, slots=True)
class GateConfig:
    """The user-tunable part of a gate, holding only integers. See ``policy`` for the rest.

    No ``gate`` id and no ``enabled`` flag. Both are written at the one construction site
    (``services.scan_runner.build_gates``) and read by no gate: each gate class carries
    its own ``id``, and a gate the operator switched off is never built at all, so the
    flag would only ever be ``True``. A config that could say ``enabled=False`` would
    invite a reader to think something checks it.
    """

    threshold: int = 0
    #: No ``secondary`` field here: the rating gate's vote floor lives in
    #: ``PolicyBody.keep_rating_rules`` as ``RatingRule.min_votes`` instead, read by
    #: ``RatingFloorGate.evaluate``. Migration ``e6f708192a3b``
    #: (``policy_migrations.drop_retired_gate_keys``) drops the old key from any stored policy
    #: body that still carries it.

    window_days: int = 365
    """How far back "recently" reaches, for gates that count activity.

    Not cosmetic. An unwindowed popularity gate protects anything anyone ever
    played, which silently disables the whole scorer."""


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


def blocked_reason(check: Reason | str, cause: Reason | str) -> Reason:
    """The one "could not check {check}: {cause}" shape, as a typed reason.

    Both slots take a bare id when there is no value to carry. A check with a window
    attached takes a full ``Reason`` instead. A bare check id resolves under the
    catalog's ``why.check.*`` entries, and a bare cause id under ``why.cause.*``. The
    cause is usually an ``Unknown.reason`` id, and a legacy sentence read back from an
    old snapshot rides through the same slot and renders as raw text.
    """
    return Reason(
        "blocked",
        {
            "check": Reason(f"check.{check}") if isinstance(check, str) else check,
            "cause": Reason(f"cause.{cause}") if isinstance(cause, str) else cause,
        },
    )


def _blocked(
    gate: GateId, observation: Observation[object], what: Reason | str
) -> GateResult | None:
    """Fail closed on an Unknown input."""
    if isinstance(observation, Unknown):
        return GateResult(
            gate=gate,
            outcome=ABSTAIN,
            blocked=True,
            detail=blocked_reason(what, observation.reason),
        )
    return None


#: A movie or a season/show: the same two-way split ``why.panel.rewatch.thin`` and
#: ``why.panel.keptNotice.conflicted`` already carry as a media-type choice in the
#: catalog. Named here because the reasons below are the shared producer for both.
MediaKind = Literal["movie", "season"]

#: Why an item, season, or show carries no Plex rating key, one entry per unmatched
#: resolver outcome. Shared by the movie and season lanes rather than duplicated for
#: each. A key into the catalog's ``why.cause.*`` entries, which the media-type choice
#: turns into "this title" or "this season" wording.
#: ``test_review_chips.py::TestTheMatchStatusVocabulary`` fails on one with no entry
#: there.
NO_KEY_REASON_IDS: dict[identity.MatchStatus | None, str] = {
    identity.MatchStatus.UNMATCHED: "plex_unmatched",
    identity.MatchStatus.AMBIGUOUS: "plex_ambiguous",
    identity.MatchStatus.CONFLICTED: "radarr_plex_disagree",
}


def no_key_reason_id(match_status: identity.MatchStatus | None) -> str:
    """The bare catalog id for why an item has no Plex rating key, with no media wording
    attached. A record saved before this field existed carries ``None``, and reads as
    the unmatched wording.

    This is the shape ``season_evidence``'s ``SeasonPruneInput.progress_unknown_reason``
    field stores: a plain id, since that field's own codec (``season_evidence._KEYS``)
    saves it as a bare string. The mid-binge guard (``season_evidence.guard_result``) is
    the one place that attaches the media-type value to it, since every caller of this
    function is season-only anyway.
    """
    return NO_KEY_REASON_IDS.get(match_status, "plex_unmatched")


def no_key_reason(match_status: identity.MatchStatus | None, media_type: MediaKind) -> Reason:
    """The typed cause for a missing Plex rating key: the shared id above, plus which
    wording the panel should pick for movie or season. What ``Unknown(reason=...)``
    carries directly on the movie and season fact builders."""
    return Reason(f"cause.{no_key_reason_id(match_status)}", {"mediaType": media_type})


#: Why dormancy, or an all-time span, could not be measured: matched to Plex, but with no
#: arrival date and no play, so there is no instant to measure from. Shared by the movie
#: and season lanes rather than duplicated for each. A key into the catalog's
#: ``why.cause.*`` entries, named here so the drift test covers it.
NO_ADDED_AT_REASON = "no_added_at"

#: Why a file or season's size is unreadable: the *arr reported no size on disk. Shared
#: the same way as the reason above. Reaches the panel through a keep rule on "Size on
#: disk".
NO_SIZE_REASON = "no_file_size"


def no_added_at_reason(media_type: MediaKind) -> Reason:
    """The typed cause for a missing arrival date, with wording chosen the same way as
    :func:`no_key_reason`."""
    return Reason(f"cause.{NO_ADDED_AT_REASON}", {"mediaType": media_type})


def no_size_reason(media_type: MediaKind) -> Reason:
    """The typed cause for a missing on-disk size, with wording chosen the same way as
    :func:`no_key_reason`."""
    return Reason(f"cause.{NO_SIZE_REASON}", {"mediaType": media_type})


def _rating_value(rating: Rating) -> Reason:
    """A rating the item really has, as the value the panel prints.

    The typed twin of ``ratings.Rating.describe_for_user``: a percentage source reads as
    a percentage, and a 0-10 source reads on its own scale with the vote count that makes
    the number mean something. The catalog holds the actual words.
    """
    if is_percentage_source(rating.source):
        return Reason(
            "rating_value_pct",
            {"source": rating.source.value, "pct": round(rating.value * 10)},
        )
    if rating.votes is None:
        return Reason(
            "rating_value", {"source": rating.source.value, "value": round(rating.value, 1)}
        )
    return Reason(
        "rating_value_votes",
        {
            "source": rating.source.value,
            "value": round(rating.value, 1),
            "votes": rating.votes,
        },
    )


@dataclass(frozen=True, slots=True)
class RatingRule:
    """One "keep it if it clears this bar" rule, for one rating source.

    ``floor`` is in tenths (7.5 becomes 75), the same convention the whole policy uses.
    It reads the same way for a percentage source: 75% is stored as 75, since 84% is
    normalized to 8.4 on the 0-10 scale first, and its tenths are 84. ``min_votes`` only
    applies to sources that count votes (IMDb, TMDb). It is ignored for Rotten Tomatoes
    and Metacritic, which are percentages with no vote count (see
    ``ratings.Rating.has_meaningful_vote_count``).
    """

    source: RatingSource
    floor: int
    min_votes: int = 0

    def describe_bar(self) -> Reason:
        """The full bar, for the why-panel and the checked line: the number, the source, and
        the vote floor where the source has one (``7.5 on IMDb from 1,000+ votes``).

        The floor's catalog entries (``why.rating_bar*``) keep their own vote wording
        rather than sharing the value clause's, because the why-panel prints a bar and a
        real count one line apart, and using the same wording for both made a vote floor
        read like a measurement. The "+" is the whole difference. `PolicyEditor.tsx`'s
        `describeBar` renders this same wording for the same rule, and the two are
        pinned together in `test_the_bar_names_its_vote_floor_as_a_floor`.
        """
        if is_percentage_source(self.source):
            return Reason("rating_bar_pct", {"source": self.source.value, "pct": self.floor})
        if self.min_votes > 0:
            return Reason(
                "rating_bar_votes",
                {"source": self.source.value, "floor": self.floor / 10, "votes": self.min_votes},
            )
        return Reason("rating_bar", {"source": self.source.value, "floor": self.floor / 10})


@dataclass(frozen=True, slots=True)
class RatingFloorGate:
    """Keep anything well-rated by enough people, on any source the owner trusts.

    The owner picks a set of bars, such as IMDb 7.5 from 1,000 votes or Rotten Tomatoes
    critics at 75%, and a title clearing any one of them is kept (or every one, if the
    owner tightens the match). Each bar reads the saved rating for its source, and the
    single-source case, just IMDb, behaves exactly as the original gate did.

    The vote floor is not optional on sources that have one, since a high score from a
    handful of voters is noise. Each rating's source is pinned explicitly, never guessed:
    the same Plex field held IMDb ratings on one server and Rotten Tomatoes percentages
    on another, so ``ratings.Rating`` records where each number came from, and a 7.5 IMDb
    bar is never compared against a Tomatometer score of 96.

    A protection, so it has no way to condemn: a source we could not read, or a rule with
    no matching rating, simply does not fire. It cannot delete a file, only keep one.
    """

    rules: tuple[RatingRule, ...] = ()
    match: Literal["any", "all"] = "any"
    id: GateId = GateId.RATING_FLOOR

    def _miss_reason(self, rule: RatingRule, rating: Rating | None) -> Reason:
        """Why one bar was not cleared, with the item's own numbers where we have them. This
        is the "checked and did not fire, with the numbers" detail the panel needs."""
        if rating is None:
            return Reason(
                "rating_miss_none", {"source": rule.source.value, "bar": rule.describe_bar()}
            )
        # The same check `evaluate` uses to decide the bar runs here too, so the sentence
        # cannot claim a vote floor was missed on a count `Rating.meets` already counted
        # as enough.
        if rating.short_of_vote_floor(rule.min_votes):
            return Reason(
                "rating_miss_votes",
                {"value": _rating_value(rating), "need": rule.min_votes},
            )
        if is_percentage_source(rule.source):
            return Reason(
                "rating_miss_below_pct",
                {"value": _rating_value(rating), "floor_pct": rule.floor},
            )
        return Reason(
            "rating_miss_below",
            {"value": _rating_value(rating), "floor": rule.floor / 10},
        )

    def evaluate(self, facts: Facts) -> GateResult:
        if not self.rules:
            return GateResult(self.id, ABSTAIN, detail=Reason("rating_none_set"))

        # Fail closed if a source we keep on could not be read. IMDb is the one source
        # that carries a three-state observation in Facts (imdb_rating_tenths and
        # imdb_votes). The others come from saved Radarr and Plex data, and a failure to
        # read those degrades the whole snapshot upstream, so they are never per-item
        # Unknown. When an IMDb bar's own rating cannot be read, blocking keeps the file
        # rather than silently dropping the protection it was carrying.
        if any(r.source is RatingSource.IMDB for r in self.rules):
            if blocked := _blocked(self.id, facts.imdb_rating_tenths, "imdb_rating"):
                return blocked
            if blocked := _blocked(self.id, facts.imdb_votes, "imdb_votes"):
                return blocked

        by_source = {r.source: r for r in facts.ratings}
        cleared: list[Reason] = []
        missed: list[Reason] = []
        for rule in self.rules:
            rating = by_source.get(rule.source)
            if rating is not None and rating.meets(rule.floor / 10, min_votes=rule.min_votes):
                cleared.append(_rating_value(rating))
            else:
                missed.append(self._miss_reason(rule, rating))

        # With "any", one cleared bar keeps the file. With "all", every bar must clear,
        # and a source we could not read counts as a miss, since there is nothing to
        # clear. So "all" fails closed toward not protecting, the safe direction for a
        # keep rule, which can only ever spare a file.
        protects = bool(cleared) if self.match == "any" else (not missed and bool(self.rules))
        if protects:
            return GateResult(
                self.id, PROTECT, detail=Reason("rating_cleared", {"clauses": tuple(cleared)})
            )
        if cleared:
            return GateResult(
                self.id,
                ABSTAIN,
                detail=Reason(
                    "rating_cleared_some",
                    {"cleared": tuple(cleared), "missed": tuple(missed)},
                ),
            )
        return GateResult(
            self.id, ABSTAIN, detail=Reason("rating_missed", {"clauses": tuple(missed)})
        )


@dataclass(frozen=True, slots=True)
class StreamingNowGate:
    """Never delete something that is playing right now.

    The executor re-checks this live, in the seconds before the delete, so a stream
    that starts after the scan still blocks it. No other shipping media-pruning tool
    does this.
    """

    config: GateConfig
    id: GateId = GateId.STREAMING_NOW

    def evaluate(self, facts: Facts) -> GateResult:
        if blocked := _blocked(self.id, facts.is_streaming_now, "active_streams"):
            return blocked
        streaming = facts.is_streaming_now
        if isinstance(streaming, Known) and streaming.value:
            return GateResult(self.id, PROTECT, detail=Reason("streaming_now"))
        return GateResult(self.id, ABSTAIN, detail=Reason("streaming_nobody"))


#: How much shorter than the span it must cover a reach must be before the copy names the
#: number of days directly. Both spans are phrased by ``clock.humanize_days``, where a
#: month is 30 days and a year is 365. A 360-day reach renders as "12 months" while a
#: 365-day span renders as "a year," and showing both that way could read to the operator
#: as though the shorter history were the longer span. One month of margin is the
#: cheapest bound that cannot invert, since a reach at least a month short always renders
#: a smaller leading unit. Inside the margin, the copy states the comparison instead of
#: the number, which is shorter anyway.
_REACH_NAMEABLE_MARGIN_DAYS = 30


def history_shortfall(reach: Observation[float], needed: float) -> Reason | None:
    """Why the watch history cannot cover ``needed`` days, as a typed reason.

    Returns ``None`` when it does cover them: the only case where a count drawn from the
    watch history can be read as the answer, rather than as a lower bound, since the
    plays a short history cannot see are exactly the ones that would have kept the file.

    The one place every reader of a watcher count checks this: ``ServerPopularityGate``
    below asks it about the policy's popularity window, ``fields.reach_shortfall`` asks
    it for the operator-authored protect, condemn, and keep rules, and
    :func:`lifetime_shortfall` asks it for every all-time count. A bound honored in one
    place and not the next is the bug this function exists to prevent.
    """
    if not isinstance(reach, Known):
        return Reason("cause.reach_not_recorded")
    if reach.value >= needed:
        return None
    if reach.value <= needed - _REACH_NAMEABLE_MARGIN_DAYS:
        return Reason("cause.history_reach_short", {"reach_days": reach.value})
    return Reason("cause.history_not_that_far")


def lifetime_shortfall(reach: Observation[float], age: Observation[float]) -> Reason | None:
    """Why the watch history cannot support an all-time watcher count, as a typed reason.

    Returns ``None`` when it can. An all-time count is only a real answer when the watch
    history reaches back to the day the item arrived, since every play it could ever have
    had happened after that. Short of that, the count is a lower bound, and the plays
    before the history began are exactly the ones that would have kept the file.

    Without the arrival date there is no span to compare the reach against, so the count
    cannot be established either way. That reads as ``Unknown``, never as a permissive
    ``Absent``.

    This is the one place that defines what span an all-time count needs.
    ``fields.reach_shortfall`` asks it for the operator-authored rules off ``Facts``, and
    ``services.season_scan`` asks it per season for the keep-rule conflict detector,
    which compares two all-time counts directly and reads no ``Facts`` at all. That
    second reader is why this is its own function rather than a branch inside
    ``reach_shortfall``: a season-path caller with no ``Facts`` in hand would otherwise
    have had to restate the span.
    """
    if not isinstance(age, Known):
        return Reason("cause.added_at_not_recorded")
    return history_shortfall(reach, float(age.value))


def progress_is_establishable(*, reach_days: int, hold_days: int) -> bool:
    """Whether the watch history reaches back far enough to answer "who is part-way through."

    The guard holds back a viewer whose last play of the show falls inside ``hold_days``.
    It can only see plays the watch history holds, and that history begins
    ``reach_days`` back, so the answer is sound exactly when the history spans the whole
    hold window. Past ``reach_days``, an invisible viewer and one whose hold expired look
    the same, and losing them costs nothing. Inside it they are not the same: the viewer
    simply has no rows to find.

    That gap is what this function exists to name.
    :func:`reaper.services.season_pruning.active_progress` reads no rows as "nobody is
    part-way through," a genuine ``Absent``, when the truth may be "the history does not
    reach far enough back to know," which is ``Unknown``. That function keeps a viewer
    whose last-watched time is unreadable, but a missing viewer is not unreadable, they
    are simply absent, and there is nobody to keep. So the caller asks this question
    separately, and ``services.season_pruning.plan_series_prune`` holds every season on
    disk when the answer is False.

    This lives here beside :func:`history_shortfall` and :func:`lifetime_shortfall` as
    the third member of one family: a span the watch history is asked to cover, and what
    it means when it cannot. ``policy_warnings.inspect`` also needs it, to warn about
    this before a scan runs into it, and an engine module may not import a service, so
    this stays a shared helper rather than being reimplemented at the second caller.

    ``hold_days <= 0`` means the hold never expires, and no history of finite length can
    support an unbounded claim: a viewer whose every play predates the horizon is
    invisible at any reach, and with no expiry to make that harmless, the set can never
    be established.

    ``in_progress_hold_days`` (passed in here as ``hold_days``) is the span the guard
    claims to cover, so a history shallower than it is exactly the unsupported claim
    this function exists to catch. This function is pure: it takes the reach as an
    argument instead of measuring it.
    """
    if hold_days <= 0:
        return False
    return reach_days >= hold_days


@dataclass(frozen=True, slots=True)
class ServerPopularityGate:
    """Keep what your users actually watch, regardless of who asked for it.

    The count is drawn from a watch history that begins somewhere
    (``Facts.history_reach_days``), so this gate asks its question twice: how many
    people watched it, and whether the evidence goes back far enough for that number to
    mean what it says.
    """

    config: GateConfig
    id: GateId = GateId.SERVER_POPULARITY

    def evaluate(self, facts: Facts) -> GateResult:
        if blocked := _blocked(self.id, facts.distinct_watchers, "watch_history"):
            return blocked
        watchers = facts.distinct_watchers
        count = watchers.value if isinstance(watchers, Known) else 0
        floor = self.config.threshold

        window = self.config.window_days

        if count >= floor:
            return GateResult(
                self.id,
                PROTECT,
                detail=Reason("popularity_watched", {"count": count, "window_days": window}),
            )
        # Everything below here says the protection did not fire, and that is only a
        # real answer if the watch history actually covered the whole window. A history
        # reaching back three months cannot report who watched a title over a year: the
        # count it returns is a lower bound, and the plays it cannot see are exactly the
        # ones that would have kept the file. Printing "nobody watched it" there would
        # let a short watch history reach the operator through the watcher check instead
        # of the dormancy one, so this fails closed instead.
        #
        # The PROTECT above needs no such check: a play seen inside part of the window
        # did happen inside the window, so a lower bound that already clears the floor
        # clears it however much more history arrives.
        if (short := history_shortfall(facts.history_reach_days, window)) is not None:
            return GateResult(
                self.id,
                ABSTAIN,
                blocked=True,
                # An operator's reap override bypasses this block, exactly as it
                # bypasses every blocked gate (see ``engine.verdict``). A watch history
                # shorter than the popularity window triggers this block most often,
                # and holding a reap here would let a shallow Tautulli history block
                # every override library-wide. This block still forces ABSTAIN, which
                # remains its job.
                #
                # The ``blocked`` reason id is still load-bearing, for two readers:
                # ``api.review._chip`` sends it to "Some checks couldn't run" instead of
                # "left for you to decide," and ``WhyPanel`` reads its check and cause
                # values. Renaming it would make a plumbing failure read to the operator
                # as their own decision.
                detail=blocked_reason(
                    Reason("check.recent_watchers_window", {"window_days": window}), short
                ),
            )
        if count == 0:
            return GateResult(
                self.id,
                ABSTAIN,
                detail=Reason("popularity_nobody", {"window_days": window}),
            )
        return GateResult(
            self.id,
            ABSTAIN,
            detail=Reason(
                "popularity_few", {"count": count, "window_days": window, "floor": floor}
            ),
        )


# ``WhitelistGate`` (keep tags, the "Never Reap" collection) and ``CuratedListGate``
# (the IMDb Top 250) are gone. Every list, tag, collection, watchlist, and IMDb list
# now protects through the operator's own keep rules on the ``on_list`` field,
# evaluated by ``fields.CustomProtectGate``, so each list's strength is a per-list
# choice on Policy.
# ``policy_migrations.convert_list_protections`` rewrites a stored body's gate rows into
# the equivalent rules, and their `GateId`s survive above so a stored explanation still
# decodes.


@dataclass(frozen=True, slots=True)
class MinDormancyGate:
    """Nothing may be deleted until it has sat unwatched for long enough.

    This must stay a hard gate. A weighted signal can be outvoted, and the rewatch
    curve says a recently dormant film is still likely to be watched again.

    The default threshold comes from that measured curve (``docs/SIGNALS.md``, "There
    is no cliff. Nothing is ever free to delete."). About 61% of films are watched
    again within the first year of dormancy, about 30% between two and three years,
    about 19% from three to five, and about 13% beyond five. The curve decays slowly
    and never reaches zero.

    Those figures were measured on one real library. Reaper fits nothing to this
    server. The gate enforces the operator's own stored number (``config.threshold``)
    exactly as saved. The threshold is a default the operator may move. The shallow
    tail of the curve is what stays true everywhere.
    """

    config: GateConfig
    id: GateId = GateId.MIN_DORMANCY

    def evaluate(self, facts: Facts) -> GateResult:
        blocked = _blocked(self.id, facts.days_observed_unwatched, "last_watched")
        if blocked:
            return blocked

        dormant = facts.days_observed_unwatched
        floor = self.config.threshold

        if not isinstance(dormant, Known):
            # We cannot establish that it has been dormant long enough, so we must not
            # delete it. Reachable for ``Absent`` only: ``_blocked`` above already
            # answered every ``Unknown`` with a blocked ABSTAIN, which is a different
            # hold from this PROTECT ("we could not answer" is blocked, never a bare
            # PROTECT). No fact builder emits ``Absent`` for this field today, so this
            # branch does not fire yet. It stays so that an ``Absent`` arriving later
            # still keeps the file, and the ``isinstance`` check is load-bearing either
            # way, since ``_blocked`` returns a result rather than narrowing the type,
            # so ``.value`` below needs it.
            return GateResult(self.id, PROTECT, detail=Reason("dormancy_unestablished"))

        if dormant.value < floor:
            # "Untouched", not "last watched": for a never-played item the clock runs from
            # the day it arrived, and claiming a watch that never happened would be a lie
            # in the one panel whose job is to be believed.
            return GateResult(
                self.id,
                PROTECT,
                detail=Reason("dormancy_under_floor", {"days": dormant.value, "floor_days": floor}),
            )
        return GateResult(
            self.id,
            ABSTAIN,
            detail=Reason("dormancy_past_floor", {"days": dormant.value, "floor_days": floor}),
        )


#: The cohort size under which a fitted rewatch block shows no number and can never
#: trigger the hold (see ``docs/history/REWATCH_PLAN.md``, stage 2). It lives here rather
#: than in ``services/rewatch.py`` because ``RewatchOddsGate`` below reads it, and an
#: engine module may not import a service.
REWATCH_BLOCK_FLOOR_N = 30


def wilson_upper(k: int, n: int) -> float:
    """The Wilson 95% upper bound of ``k/n``, as a fraction.

    The hold compares this rather than the raw rate, so a small library never loses
    protection to sampling noise. It converges to ``k/n`` as ``n`` grows. Computed once
    here, and read by the gate below and by the policy page's consequence preview
    (``api.policy``)."""
    if n <= 0:
        return 0.0
    z = 1.96
    p = k / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (center + spread) / denom


@dataclass(frozen=True, slots=True)
class RewatchOddsGate:
    """Keep anything whose kind gets watched again at or above the operator's percentage.

    Opt-in on both movie and TV policies, and the one gate that reads the fitted rewatch
    curve. The item's saved cohort (``Facts.rewatch_cohort_n`` and
    ``rewatch_cohort_k``) is its merged dormancy block from the per-scan fit, a movie's
    own or a season's show's (see ``docs/LEARNINGS.md``, the TV fit entry). The
    comparison is the Wilson 95% upper bound of that block's rate against
    ``config.threshold`` percent.

    An unreadable cohort abstains without blocking, unlike every other gate, which fails
    closed and blocks on an unreadable input. A withheld block never blocks and never
    condemns, because on a shallow watch history most of the library has no measurable
    cohort, and an opt-in extra protection must not amber-flag all of it. Items whose
    history is genuinely unreadable are already blocked by the dormancy and popularity
    gates reading the same sources, so staying quiet here withdraws no cover (see
    ``docs/history/REWATCH_PLAN.md``, stage 2, "The hold").
    """

    config: GateConfig
    id: GateId = GateId.REWATCH_ODDS
    media_type: MediaKind = "movie"
    """Which wording the panel should pick for this gate's four cohort reasons: "movie" or
    "season". ``Facts`` carries no media discriminator of its own (unlike
    ``KeepConfig.media_type`` in ``engine/signals.py``, the rewatch keep's own twin), so
    this is set once at construction (``scan_runner.build_gates``, off the policy's own
    ``media_type``) rather than read per item. Defaults to "movie" so a hand-built gate in
    a test needs no opinion about it. ``test_signal_quality.py::TestTheRewatchOddsGate``
    sweeps both values across all four reasons."""

    def evaluate(self, facts: Facts) -> GateResult:
        n_obs = facts.rewatch_cohort_n
        k_obs = facts.rewatch_cohort_k
        if isinstance(n_obs, Unknown) or isinstance(k_obs, Unknown):
            return GateResult(
                self.id,
                ABSTAIN,
                detail=Reason("rewatch_no_history", {"mediaType": self.media_type}),
            )
        if not (isinstance(n_obs, Known) and isinstance(k_obs, Known)):
            # Hand-built Facts that never gathered a curve. Both live lanes save a
            # cohort now, so a genuine Absent here means there is nothing to compare,
            # not that a read failed.
            return GateResult(self.id, ABSTAIN, detail=Reason("does_not_apply"))
        n = int(n_obs.value)
        k = int(k_obs.value)
        if n < REWATCH_BLOCK_FLOOR_N:
            return GateResult(
                self.id,
                ABSTAIN,
                detail=Reason("rewatch_thin", {"mediaType": self.media_type}),
            )
        floor = self.config.threshold
        raw_bound_pct = wilson_upper(k, n) * 100
        # This is the number the decision itself compares. A 0-of-30 cohort can still
        # clear a low floor on this bound alone, so the sentence quotes this number, not
        # the raw rate: quoting the raw rate would print "0 of 30 keep getting watched"
        # for a title that just cleared the floor. Rounded for display only; the
        # comparison above stays on the unrounded value so display precision never moves
        # the decision.
        bound_pct = round(raw_bound_pct)
        if raw_bound_pct >= floor:
            # Lowercase fragment: it renders in the "Protections that fired" list.
            return GateResult(
                self.id,
                PROTECT,
                detail=Reason(
                    "rewatch_watched_again",
                    {"k": k, "n": n, "mediaType": self.media_type, "bound_pct": bound_pct},
                ),
            )
        return GateResult(
            self.id,
            ABSTAIN,
            detail=Reason(
                "rewatch_under_floor",
                {
                    "k": k,
                    "n": n,
                    "floor_pct": floor,
                    "mediaType": self.media_type,
                    # The sentence orders the two numbers ("under the {floor_pct}% you
                    # keep"), and plain rounding breaks that order near the floor: a
                    # 24.9 bound at a 25 floor would display as "25%, under the 25% you
                    # keep." Clamp so the displayed bound always stays under the floor
                    # it is under.
                    "bound_pct": min(bound_pct, floor - 1),
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class ReturnedGate:
    """Hold a title that left the library and came back.

    A return is the clearest evidence Reaper can get that removing something was wrong:
    somebody went and fetched it again. What counts as a return is decided in the scan,
    not here: four conditions over Plex rating keys, a minimum absence, and a count of
    the scans that ran during it (``services.library_seen``). This gate reads the one
    number that comes out of that. Splitting the two lets an operator move the hold's
    length and have the simulator replay it exactly, the same reason
    ``Facts.rewatch_last_play_days`` is saved raw.

    ``config.threshold`` is how long the hold lasts, in days. ``config.window_days`` is
    the minimum absence, read by the scan rather than by this gate.

    An unreadable return abstains without blocking, unlike every other gate, which fails
    closed and blocks on an unreadable input. The ledger is empty on a fresh install, and
    stays empty on every existing install until the first scan after this shipped, so
    blocking on ``Unknown`` would amber-flag the entire library and abstain every
    verdict in it for months, during which Reaper could condemn nothing at all. Items
    whose Plex bind genuinely failed are already blocked by the four Plex-dependent
    gates reading the same resolution, so staying quiet here withdraws no cover.
    """

    config: GateConfig
    id: GateId = GateId.RETURNED

    def evaluate(self, facts: Facts) -> GateResult:
        returned = facts.returned_days_ago
        if isinstance(returned, Unknown):
            return GateResult(
                self.id,
                ABSTAIN,
                detail=Reason("returned_no_record"),
            )
        if not isinstance(returned, Known):
            return GateResult(self.id, ABSTAIN, detail=Reason("returned_not_returned"))

        hold = self.config.threshold
        # Rounded up, so a hold with any of itself left never reads as spent. The bound
        # that produces less deletion pressure is the one that keeps the file.
        left = math.ceil(hold - returned.value)
        if left <= 0:
            return GateResult(
                self.id,
                ABSTAIN,
                detail=Reason("returned_past_hold", {"days": returned.value, "hold_days": hold}),
            )
        by_reaper = facts.returned_by_reaper
        removed_by_us = isinstance(by_reaper, Known) and by_reaper.value
        # The journal's one job. The hold lasts the same length either way, since
        # splitting it would mean a second setting for a difference nobody has measured.
        # Two reason ids exist so the wording can say whether Reaper itself removed it;
        # both carry the countdown the chip reads.
        return GateResult(
            self.id,
            PROTECT,
            detail=Reason(
                "returned_came_back_ours" if removed_by_us else "returned_came_back",
                {"days_left": left},
            ),
        )


@dataclass(frozen=True, slots=True)
class DataHorizonGate:
    """Fail closed when we cannot say how long an item has gone unwatched.

    Tautulli cannot import Plex history from before it was installed, so everything
    watched before that looks never-watched: one of the biggest mass-deletion risks in
    this kind of tool. That risk arrives down two paths, and neither defense against it
    is this gate's job. On the watcher path, ``ServerPopularityGate`` refuses to report a
    protection as checked over a window its history does not span
    (``Facts.history_reach_days``). On the dormancy path, which the rest of this
    docstring is about, the defense lives in how the fact is derived: dormancy is
    measured from ``max(added_at, horizon)`` (``engine.dormancy.reference_instant``,
    through ``services.snapshot.build_facts`` and its season twin), so an item older
    than the watch history is clamped to the history's edge rather than read as decades
    dormant.

    This gate's only independent job is to fail closed when dormancy is ``Unknown``. It
    is not handed ``added_at`` and cannot re-check that clamp itself, so it must not
    claim to have. It duplicates ``MinDormancyGate``'s fail-closed behavior on Unknown on
    purpose, as a second layer of defense on the one fact whose absence would otherwise
    condemn the item we know least about. When dormancy is Known, it abstains, and it
    does not claim that the watch history "covers" the item, since it never
    independently verified that.
    """

    config: GateConfig
    id: GateId = GateId.DATA_HORIZON

    def evaluate(self, facts: Facts) -> GateResult:
        if blocked := _blocked(self.id, facts.days_observed_unwatched, "watch_horizon"):
            return blocked
        return GateResult(self.id, ABSTAIN, detail=Reason("data_horizon_ok"))


# No gate implements ``GateId.UNMANAGED`` ("if no *arr owns it, Reaper cannot delete
# it"). Reaper builds its candidate list by asking Sonarr and Radarr what they hold, so
# a file neither owns can never reach the set such a gate would filter, and every fact
# builder hardcodes ``Facts.is_managed`` to ``Known(True)`` (`snapshot`, `season_scan`).
# The gate's check would always have been true, so removing it deleted dead code, and
# no file was ever exposed by its absence.
#
# ``Facts.is_managed`` stays, since it is a real observation saved into every stored
# snapshot, and a re-wired gate would need it. Bringing the gate back means giving
# Reaper a scan path that can find media no *arr manages, by reading Plex directly, so
# the fact can be something other than True. Gate, builders, and tests return together.
# ``GateId.UNMANAGED`` survives so a stored explanation still decodes, read by
# ``verdict.STRUCTURAL_GATES``, `api.review`'s chip wording, and `WhyPanel.tsx`'s
# held-reap line.


# No gate implements ``GateId.OTHERS_WATCHING`` ("the requester ignored it, but other
# people did watch it"). No fact builder produces a Known ``others_watching``: every
# one writes Absent, so the count is always 0 against a floor of at least 1, and such a
# gate could never protect anything, while its ABSTAIN line would read to the owner
# like a check that ran. A protection that cannot fire should not exist: the evidence
# it needs (per-user plays excluding the requester) is not gathered anywhere in the
# scan. ``GateId.OTHERS_WATCHING`` survives so a stored explanation from before this
# removal can still be decoded. ``scan_runner.GATE_TYPES`` does not carry it, so a
# policy that enables it refuses to scan instead of running a protection that keeps
# nothing. Wiring it back means gathering the count first, then restoring gate, fact,
# and builders together.


@dataclass
class Evaluation:
    """The full result for one item: every gate, fired or not."""

    results: Sequence[GateResult] = field(default_factory=list)

    @property
    def protected(self) -> bool:
        return any(r.fired for r in self.results)

    @property
    def blocked(self) -> bool:
        """A protection could not be checked, so we must not act.

        This is deliberately distinct from ``protected``. "We did not manage to look" is
        not the same as "we looked and it is fine," and treating them alike is exactly
        how a media-pruning tool deletes something during an outage.
        """
        return any(r.blocked for r in self.results)

    @property
    def protectors(self) -> list[GateResult]:
        return [r for r in self.results if r.fired]

    @property
    def checked_and_did_not_fire(self) -> list[GateResult]:
        return [r for r in self.results if not r.fired and not r.blocked]

    @property
    def could_not_be_checked(self) -> list[GateResult]:
        return [r for r in self.results if r.blocked]


def evaluate_all(gates: Sequence[Gate], facts: Facts) -> Evaluation:
    """Run every gate. Never short-circuits.

    Stopping at the first protection would be faster, but it would break the point of
    the tool: the "checked and did not fire, with the numbers" block needs every gate
    to report, including the ones that had nothing to say.
    """
    return Evaluation(results=[gate.evaluate(facts) for gate in gates])


__all__ = [
    "ABSTAIN",
    "PROTECT",
    "REWATCH_BLOCK_FLOOR_N",
    "DataHorizonGate",
    "Evaluation",
    "Facts",
    "Gate",
    "GateConfig",
    "GateId",
    "GateOutcome",
    "GateResult",
    "MinDormancyGate",
    "RatingFloorGate",
    "RatingRule",
    "ReturnedGate",
    "RewatchOddsGate",
    "ServerPopularityGate",
    "StreamingNowGate",
    "evaluate_all",
    "wilson_upper",
]
