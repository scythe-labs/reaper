# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gates -- protections that cannot delete anything.

The central structural claim of this module:

    **A gate has no CONDEMN constructor.**

``evaluate`` returns ``PROTECT`` or ``ABSTAIN`` and nothing else, enforced by
``mypy --strict``. No misconfiguration, no null, no transient 500, no typo, and no
future contributor can make a protection delete a file. The worst a broken gate can
do is fail to protect -- which is why gates *also* fail closed on ``Unknown``.

This is the lesson from every shipping competitor. In Maintainerr, Janitorr,
Deleterr and Reclaimerr, protections live inside the same boolean expression as the
condemnations -- so a protection is just another clause in a big OR, and an unknown
value, a transient API failure or a mis-set operator silently *disarms* it. Reaper
puts protections in a separate lane with a separate type, so that cannot happen.

Two outcomes, and a third thing that is not an outcome:

* ``PROTECT``  -- a reason to keep this file. Always beats the score.
* ``ABSTAIN``  -- this gate has nothing to say. It did not fire.
* ``Unknown`` inputs never produce a verdict at all: they raise the item's
  ``blocked`` flag, which forces the whole evaluation to ABSTAIN. Not being able to
  check a protection is treated as though the protection *might* have fired.
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

#: Fail-safe default for the custom-rule fact fields below. A shared, immutable ``Absent``
#: singleton: ``Absent`` never matches a condemn comparison and never protects, so an
#: omitted field cannot make a rule condemn or a gate fire.
#:
#: It is **not** inert on the keep lane, and the comment here used to claim it was. A
#: graded keep reads ``Absent`` as "we looked, there is genuinely none" and grants no
#: discount, where ``Unknown`` grants the full one (``signals.evaluate_keep``). So an
#: omitted field leaves the score where an honest failure would have lowered it. That is
#: why the live scan builders set every one of these explicitly, and why anything that
#: could not be read is ``Unknown`` and never this.
_UNSET: Absent = Absent(source="unset")


class GateId(enum.StrEnum):
    WHITELISTED = "whitelisted"
    """Retired as a gate: list membership now protects through the operator's own keep
    rules (the ``on_list`` field), one rule per list, either strength. Unlike the two
    retirements below this one WAS a live protection, so it is not in
    ``PolicyBody.RETIRED_GATES`` -- silently dropping it would withdraw cover. The
    ``convert_list_protections`` shim rewrites a stored body's gate row into the
    equivalent ``on_list`` rules instead. Kept so stored explanations still decode."""

    STREAMING_NOW = "streaming_now"
    RATING_FLOOR = "rating_floor"
    SERVER_POPULARITY = "server_popularity"

    OTHERS_WATCHING = "others_watching"
    """Retired: no gate implements it and no fact builder gathers the count (see the note
    where OthersWatchingGate used to be). Kept only so an explanation stored while it was
    built still decodes; ``scan_runner.GATE_TYPES`` refuses to build it."""

    CURATED_LIST = "curated_list"
    """Retired as a gate, same conversion as WHITELISTED: an IMDb list now protects
    through an ``on_list`` keep rule naming it, so its strength is the operator's choice
    per list rather than one switch over every list at once."""

    DATA_HORIZON = "data_horizon"

    UNMANAGED = "unmanaged"
    """Retired: the candidate set is built by asking Sonarr and Radarr what they hold, so
    every fact builder wrote ``Known(True)`` and the gate could not fire (see the note where
    UnmanagedGate used to be). Kept only so an explanation stored while it was built still
    decodes; ``scan_runner.GATE_TYPES`` refuses to build it."""

    MIN_DORMANCY = "min_dormancy"
    """The most important gate. Nothing under the dormancy floor may be deleted at
    all, whatever else it scores. See MinDormancyGate."""

    REWATCH_ODDS = "rewatch_odds"
    """Opt-in, both lanes: keep anything whose dormancy cohort gets watched again at or above
    the operator's percentage. See RewatchOddsGate; every body carries the row, movie and TV
    alike (``PolicyBody._rewatch_odds_row``), off each policy's own frozen cohort -- a movie's
    own, a season's the show's."""

    RETURNED = "returned"
    """Opt-in, both lanes: hold a title that left the library and came back, because a return
    is the clearest evidence Reaper can get that removing it was wrong. See ReturnedGate."""

    SEASON_PROGRESSION = "season_progression"
    """Not authorable in a policy. The engine emits it from the season judgment
    (``season_evidence.guard_result``); no policy row builds it."""

    CUSTOM = "custom"
    """Not built from a gate row. Tags an operator-authored protect condition: one
    ``fields.CustomProtectGate`` per ``protect_conditions`` entry, built in
    ``scan_runner.build_gates``, and each can only return PROTECT or ABSTAIN.
    The operator's other two keep kinds carry different ids: ``graded_keeps`` is a
    score discount through ``keep_configs()`` and builds no gate, and
    ``keep_rating_rules`` tags ``RATING_FLOOR``.
    ``custom_condemn`` is the removal side and reaches no gate."""


#: The gate ids a policy body may carry: exactly the ones ``scan_runner.build_gates`` can
#: construct from a policy row. Every other member of ``GateId`` is either retired (kept only
#: so a stored explanation still decodes, see ``PolicyBody.RETIRED_GATES``) or emitted by the
#: engine itself with no policy row behind it.
#:
#: Declared here rather than derived from ``GATE_TYPES`` because the save boundary
#: (``api.schemas.GateSettingIn``) is a leaf that must not import the scan stack. Rule 131
#: makes the two agree the only way that scales: ``tests/test_policy.py`` pins this set
#: against ``GATE_TYPES`` plus the explicitly-built ``RATING_FLOOR``, so adding a gate and
#: forgetting this list fails a test rather than quietly making the gate unauthorable.
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
    """One gate's verdict, and the numbers behind it.

    ``detail`` is not a log line -- it is what the user reads, as a typed
    :class:`~reaper.engine.reason.Reason` the frontend composes from the catalog
    (docs/history/I18N_PLAN.md §5). Every gate that was checked and did *not* fire still
    reports its actual figures, because "protections checked that did not fire,
    with the numbers" is the block that makes a deletion trustworthy.
    """

    gate: GateId
    outcome: GateOutcome
    detail: Reason

    blocked: bool = False
    """True when the gate could not be evaluated at all (an ``Unknown`` input).

    Rendered distinctly from "checked and did not fire" -- amber, not green. A
    protection that could not be checked is not a protection that passed, and
    displaying them alike is the entire Deleterr failure class.
    """

    defers_to_owner: bool = False
    """Only meaningful on a ``blocked`` result: this block is a deliberate "the owner
    should decide" flag, NOT a source Reaper could not read.

    **This no longer decides anything about a hand reap, and that is a deliberate
    reversal.** It used to be half the interlock: a blocked gate held a reap unless its
    gate was in ``verdict.DEFERRABLE_BLOCK_GATES`` and this flag was set. A blocked gate
    now never holds a hand reap at all -- only a *fired* structural stop does -- so the
    flag's reap job is gone along with that constant. ``engine.verdict``'s module
    docstring carries why.

    What it still does is tell two SHAPES of block apart for the operator's copy, which
    was always the more honest use of it. ``season_evidence.guard_result`` sets it when every
    ``season_pruning.PruneConflict`` naming the season was a comparison Reaper could
    actually make: a readable ``kept_watchers`` AND a ``shortfall`` of ``None``. The
    second is not implied by the first -- a count read off a mirror that does not reach
    back to when the season arrived is a number all the same, and settles nothing (#94).
    ``api.review._chip`` reads it to choose between "this was watched more than a season
    your rule keeps" and "couldn't check who watched these seasons", which are genuinely
    different things to tell someone deciding what to delete.

    The ``False`` default still matters for that: a producer that forgets says "Reaper did
    not establish this", which is the claim that asserts less. Typed rather than inferred
    from the detail text (rule 142) -- the wording test it replaced,
    ``detail.startswith("could not check")``, never matched the one message it existed
    for, because that message opens with the watcher count.
    """

    unestablishable: bool = False
    """Only meaningful on a ``blocked`` result: this check never ran, as against one that ran
    and left its answer to the operator.

    Set by the season guard alone (``services.season_evidence.guard_result``), the one producer
    whose blocked results are not all of a kind, and read by ``WhyPanel.keepRuleConflict``.
    A keep-rule conflict made the comparison and found the rule fighting the evidence, which
    is a decision waiting for a person ("Needs a look"). The same guard on a show Plex never
    resolved asked nobody: with no rating key anywhere there is no place in the show to read,
    so it is Limbo, and the four Plex-dependent gates beside it already say why. ``blocked``
    is true of both, so it cannot be what tells them apart.

    It also covers the guard's third shape, a season PROTECTED because the check could not be
    answered (``season_pruning.ProtectedSeason.unestablishable``). That one rides in
    ``protections_unknown`` too and was kept out of the panel's conflict branch only by the
    verdict being ``protect`` with a non-empty fired list -- true today, and nothing
    structural held it (see that function's own note about a row slipping through).

    ``False`` on every other producer, where nothing reads it: an ordinary gate's blocked
    result has one shape and needs no discriminator (the same scoping ``defers_to_owner``
    carries, for the same reason).

    Three-state on the wire, two here (rule 142): ``GateOutcomeOut.unestablishable`` carries
    ``None`` for a row frozen before the flag, since nothing in such a row says which shape
    it is. The panel reads that ``None`` as "not this", which is how those rows already
    render.
    """

    @property
    def fired(self) -> bool:
        return self.outcome == PROTECT


def thaw_defers_to_owner(value: object) -> bool | None:
    """``defers_to_owner`` as it comes off a stored explanation: three states, no coercion.

    The one derivation, because it had two and they disagreed (rule 104). ``api.review._chip``
    read the raw dict with ``is True`` / ``is False``, so anything else fell to its
    vague-but-true chip; ``explanation.GateOutcomeOut`` read the same byte through Pydantic's
    lax bool coercion, which takes ``1`` and ``"true"`` as True, ``0`` as False, and REFUSES
    ``2``, ``"banana"``, ``[]`` and ``{}``. A refusal there is not a smaller failure than a
    disagreement: it fails the whole ``Explanation``, so ``_explanation_out`` falls to its
    degraded body and the operator gets a panel with no signals, no protections and no
    threshold -- while the chip beside it and every other display extractor go on reading that
    same row perfectly well. The reap path used to as well, and that was #142; it now refuses
    the row instead (``explanation.read_explanation``). Costing the operator a hand reap over
    one illegible byte is still the wrong price, which is why this thaw stays.

    So: exactly ``True`` or exactly ``False`` is the flag; anything else is ``None``, which is
    the state the field already has a meaning for -- "nothing here can tell a comparison Reaper
    made from one it refused". A row frozen before the flag shipped reaches that answer by
    carrying no key; a row carrying a value nobody can read reaches it by carrying nothing
    legible, which is the same thing to say to an operator. Rule 96: the fallback on an
    unreadable field asserts LESS, never more, and it must not cost the operator the evidence
    that is still readable around it.
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

    Deliberately *not* a bag of raw values: an int here could be zero-meaning-none
    or zero-meaning-we-could-not-look, and those must never collapse.
    """

    title: str
    days_observed_unwatched: Observation[float]
    """Days since the last play -- or, if never played, days since
    ``max(added_at, history_begins_at)``. Unknown when it has neither: with no play and no
    arrival date there is nothing to measure from, and ``dormancy.reference_instant``
    returns no instant rather than inventing one.

    Derived rather than raw, because "days since last play" is null for the exact
    items we care about most, and every naive implementation coerces that null to
    epoch 0 -- which reads as five decades of dormancy, the maximum condemnation
    pressure the scale can express. The item nobody has watched becomes the top
    deletion candidate for the wrong reason, and does so with total confidence.
    """

    distinct_watchers: Observation[int]
    """Distinct watchers WITHIN THE POLICY'S POPULARITY WINDOW, not all time.

    Windowed deliberately. On a long-lived server nearly every title has been watched
    by *someone*, eventually: measured against real history, an all-time watcher count
    protected the overwhelming majority of the library and left the scorer with almost
    nothing to condemn at any threshold. Only a fraction of those titles still have
    watchers within the last year.

    An all-time count protects a film that five people watched years ago and nobody has
    touched since -- which is precisely the film we exist to find. Popularity has to
    mean popular *lately*, and there is deliberately no way to spell "all time" here.
    """

    distinct_watchers_all_time: Observation[int]
    """Kept for display only. Never gate on it -- see above."""

    size_bytes: Observation[int]
    imdb_rating_tenths: Observation[int]
    """Tenths, not a float: 7.5 is stored as 75. Floats do not canonicalise, and
    the policy hash must be exact."""
    imdb_votes: Observation[int]
    season_rank: Observation[int]
    """1 = newest content-bearing season. Computed over seasons with files on
    disk only; Sonarr's episodeCount is download intent and is poison here."""

    is_streaming_now: Observation[bool]
    is_managed: Observation[bool]
    in_curated_list: Observation[str]
    """The IMDb-kind lists holding this item, comma-joined. Superseded by ``on_lists``,
    which every current reader uses; still populated so a replayed stored snapshot and the
    retired ``curated_list`` gate's stored explanations keep their meaning."""
    is_whitelisted: Observation[bool]
    """Whether any list the operator curates by hand (a tag list, a collection, the
    watchlist) holds this item. The ``whitelisted`` field reads it; the gate that did is
    retired."""

    # --- fields authorable in custom rules (the weighting feature) --------------------
    # Given fail-safe defaults so a Facts built outside the scan (a test fixture, a thawed
    # snapshot) need not enumerate them; the live scan
    # builders set them explicitly, which they must -- ``Absent`` is fail-closed on the
    # condemn and gate lanes but not on the keep lane. See ``_UNSET``.
    requested: Observation[bool] = _UNSET
    """Was this title asked for via Seerr? Three-state: ``Unknown`` when Seerr is
    absent/partial or the item has no id to join on -- never coerced to ``False``, which
    would add delete pressure on missing data. Set in build_facts / build_season_facts."""

    genres: Observation[str] = _UNSET
    """The *arr's genres, comma-joined. ``Absent`` when the payload carries none."""

    on_lists: Observation[str] = _UNSET
    """Every protection list holding this item, comma-joined by the operator's names for
    them, whatever each list's source -- the ``on_list`` field's input. Defaulted like the
    fields above; a stored snapshot predating it thaws as un-checkable, never as "on no
    list" (rule 104)."""

    release_age_days: Observation[float] = _UNSET
    """Days since the title's release. Derived (age composes with dormancy); ``Absent``
    for seasons in v1, which have no clean per-season release date."""

    quality: Observation[str] = _UNSET
    """The file's quality/resolution name (e.g. "Bluray-1080p"). Movies only in v1."""

    show_ended: Observation[bool] = _UNSET
    """TV: has the series ended (vs still returning)? ``Absent`` for movies -- the
    ``season_rank`` precedent -- so it never condemns and never protects where it does
    not apply."""

    # --- how far the evidence itself reaches ------------------------------------------

    history_reach_days: Observation[float] = _UNSET
    """How many days of watch history the mirror held when this item was judged.

    Not about the item: it is the *reach* of the evidence every windowed count is drawn
    from (``services.history_sync`` names it that). ``distinct_watchers`` is only a
    complete answer while the reach covers the policy's window. Under that, a count below
    the floor is a lower bound rather than "nobody watched it", and the plays it cannot
    see are exactly the ones that would have fired the protection -- so
    ``ServerPopularityGate`` fails closed instead of printing a claim about a year it saw
    three months of.

    Defaulted like the custom-rule fields above so historical and hand-built Facts need
    not enumerate it, and read as un-checkable unless ``Known`` -- the keep direction, and
    what a stored snapshot predating the field thaws as (rule 104).
    """

    days_since_added: Observation[float] = _UNSET
    """Days since the item arrived on the server -- the span an ALL-TIME count needs.

    ``distinct_watchers`` needs the reach to cover the policy's window;
    ``distinct_watchers_all_time`` needs it to cover the item's whole life here, which is
    this number. Only with both can a count be read as the answer rather than a lower
    bound (``fields.reach_shortfall``).

    ``days_observed_unwatched`` cannot stand in for it, though it looks like it could.
    Dormancy is deliberately clamped to the mirror's edge --
    ``dormancy.reference_instant`` measures a never-played item from
    ``max(added_at, horizon)``, a played one from a play the mirror by definition holds,
    and an item with neither from nothing at all -- so it is never larger than
    ``history_reach_days``, and comparing the two would pronounce every all-time count
    complete. (The third arm yields no number, so it bounds nothing and cannot break that.)
    This one is measured from the arrival date itself and is free to exceed the reach, which
    is exactly the case that must fail closed.

    Defaulted like ``history_reach_days`` above, and read the same way: anything but
    ``Known`` is "cannot establish", the keep direction (rule 104).
    """

    # --- rewatch (#554 stage 1) ---------------------------------------------------------

    rewatch_viewings: Observation[int] = _UNSET
    """How many qualified viewings this title has, all time, any user -- a viewing being a
    cluster of qualified plays under ``services.rewatch.viewing_count``. Exists for the
    built-in habitual-rewatch keep and is not offered in the rule-authoring vocabulary.
    Movies only in v1; the season lane sets it ``Absent`` (``season_scan.build_season_facts``).

    Defaulted like the fields above, and read the same way: anything but ``Known`` never
    condemns and never argues the keep (rule 104)."""

    rewatch_last_play_days: Observation[float] = _UNSET
    """Days since the most recent QUALIFIED play, at scan time. A raw, frozen input, not a
    verdict: whether ``rewatch_viewings`` and this add up to a habitual-rewatch keep is a
    policy-configurable bar (a viewing floor, a recency window) decided in
    ``engine/signals.py``, not here, so an operator's threshold edit replays against these
    frozen facts in the simulator without a re-scan (``docs/history/REWATCH_PLAN.md``, Stage 1).
    Four states:

    * ``Known(n)`` -- the mirror was read and at least one qualified play exists.
    * ``Absent`` -- the mirror was read and this movie has no qualified play at all: we
      looked, there is genuinely nothing to measure from. ``rewatch_viewings`` is
      ``Known(0)`` alongside it, never ``Unknown``.
    * ``Unknown`` -- the mirror could not be read for this item (no Plex key, or the item
      is watch-blind). Never a measured absence (rule 93).
    * The season lane sets this ``Absent`` too, with its own comment
      (``season_scan.build_season_facts``): it ships no validated TV rewatch answer yet.

    Defaulted like the fields above; a stored snapshot predating this field thaws as
    ``Unknown``, never as a false "checked, nothing there" (rule 104)."""

    # --- rewatch cohort (#554 stage 2) ---------------------------------------------------

    rewatch_cohort_n: Observation[int] = _UNSET
    """How many candidates, in the same dormancy block as this item, were tracked by the
    Stage 2 rewatch-probability fit -- the block's cohort size.

    Frozen raw, not judged here: the display floor and the withhold are decided by
    consumers against ``REWATCH_BLOCK_FLOOR_N`` and ``rewatch.cohort_block``, so a thin
    block freezes ``Known`` at its small ``n`` rather than pretending it was not measured.
    That is what lets the opt-in protective hold (decided in the engine by the gate that
    reads it) and the simulator replay exactly against these frozen counts, the same reason
    ``rewatch_last_play_days`` above is frozen rather than pre-judged
    (``docs/history/REWATCH_PLAN.md``, Stage 2).

    Known only when the current dormancy is Known AND the fit found a non-withheld block
    for it; Unknown otherwise (no key, watch-blind, dormancy Unknown, past the fitted range,
    a dropped bucket, or withheld by reach) -- never Absent, unlike the stage 1 pair above:
    a candidate this scan measured always has an opinion about its own dormancy block, even
    when that opinion is "cannot say". Both lanes freeze it -- a movie its own block
    (``services.snapshot.build_facts``), a season its show's, off the TV curve the season
    task fits the same way (``services.season_scan._judge_series``). Absent means
    hand-built Facts that never gathered a curve at all.

    Defaulted like the fields above, and read the same way: anything but ``Known`` never
    condemns and never argues the hold (rule 104)."""

    rewatch_cohort_k: Observation[int] = _UNSET
    """How many of ``rewatch_cohort_n`` were watched again inside the fit's outcome window --
    the block's watched-again count. Same block, same fit, same freeze as
    ``rewatch_cohort_n`` immediately above, including its Known/Unknown/Absent states; the
    rate is ``k / n``, derived and never stored separately (``docs/history/REWATCH_PLAN.md``,
    Stage 2)."""

    # --- a title that came back (#553) ---------------------------------------------------

    returned_days_ago: Observation[float] = _UNSET
    """Days since Reaper recorded that this title left the library and came back.

    The clock the hold counts down from, frozen at scan time like every other span here. Read
    off ``db.LibrarySeen.returned_at``, which is written by the scan that DETECTS the return
    and read back by every scan after it -- a return is visible for one scan only, so the fact
    has to be stored rather than re-derived (``services.library_seen``).

    Three states, and the third is not "no return":

    * ``Known(n)`` -- the ledger holds a return for this title's external id.
    * ``Absent`` -- the ledger holds a row for it and no return: we looked, there is genuinely
      nothing. The ordinary state of a title that has sat in one place.
    * ``Unknown`` -- there was nothing to look up. No Plex bind, no external id, or a title
      Reaper is seeing for the first time. Never a measured absence (rule 93).

    Defaulted like the fields above, and a stored snapshot predating it thaws ``Unknown``
    (rule 104)."""

    returned_by_reaper: Observation[bool] = _UNSET
    """Whether Reaper's own journal says it removed this title before the return.

    Chooses which sentence the operator reads and nothing else: the hold is the same length
    either way, because splitting them would mean a second knob for a difference nobody has
    measured. ``Known(False)`` is a real answer -- Reaper has no record of removing it, so the
    operator did, or something else did. ``Absent`` beside a ``Known`` ``returned_days_ago``
    cannot happen; both are written together (``services.library_seen.record``)."""

    ratings: tuple[Rating, ...] = ()
    """Every interpretable rating the scan froze for this item, one per source (IMDb,
    TMDb, Rotten Tomatoes critics/audience, Metacritic). Read only by the multi-source
    ``RatingFloorGate``, a protection, so a missing or unreadable source can only ever
    fail to keep a title, never condemn one. Empty by default, which simply means the
    gate does not fire. Not hashed (Facts is evidence, not policy)."""


@dataclass(frozen=True, slots=True)
class GateConfig:
    """The user-tunable part of a gate. Integers only -- see ``policy``.

    No ``gate`` id and no ``enabled`` flag. Both were written at the one construction site
    (``services.scan_runner.build_gates``) and read by no gate: each gate class carries its
    own ``id``, and a gate the operator switched off is never built at all, so the flag was
    only ever ``True``. A config that could say ``enabled=False`` invites a reader to think
    something checks it.
    """

    threshold: int = 0
    #: No ``secondary`` here. It carried the rating gate's vote floor until that bar moved to
    #: ``PolicyBody.keep_rating_rules``, where it is now ``RatingRule.min_votes``, read by
    #: ``RatingFloorGate.evaluate`` and the ``_miss_phrase`` helper it calls. After the move no
    #: gate read ``secondary``, and ``scan_runner`` was copying a dead number into every gate it
    #: built. The policy body has since dropped it too, via migration ``e6f708192a3b`` -- see
    #: ``policy_migrations.drop_retired_gate_keys`` for the stored bodies that still carry the key.

    window_days: int = 365
    """How far back "recently" reaches, for gates that count activity.

    Not cosmetic. An unwindowed popularity gate protects anything anyone ever
    played, which silently disables the whole scorer."""


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


def blocked_reason(check: Reason | str, cause: Reason | str) -> Reason:
    """The one "could not check {check}: {cause}" shape, as a typed reason.

    Both slots take a bare id where there are no params to carry -- a check with a window
    takes a full ``Reason``. A bare check id resolves under the catalog's ``why.check.*``
    namespace and a bare cause id under ``why.cause.*``; the cause is usually an
    ``Unknown.reason`` id, and a legacy sentence thawed off an old snapshot rides through
    the same slot and renders raw.
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


#: A movie or a season/show -- the same two-way split ``why.panel.rewatch.thin`` and
#: ``why.panel.keptNotice.conflicted`` already carry as an ICU ``mediaType`` select in the
#: catalog. Named here because the reasons below are the shared producer for both lanes.
MediaKind = Literal["movie", "season"]

#: Why an item/season/show carries no Plex rating key, one entry per non-matched resolver
#: outcome -- shared by the movie and season lanes (rule 72: this used to be two near-copies,
#: ``snapshot._NO_KEY_REASONS`` and ``season_evidence._NO_KEY_REASONS``, that differed only in
#: which literal each ``MatchStatus`` mapped to). A KEY into the catalog's ``why.cause.*``
#: entries, which the ICU ``mediaType`` select turns into "this title"/"this season" wording;
#: ``test_review_chips.py::TestTheMatchStatusVocabulary`` fails on one with no entry there.
NO_KEY_REASON_IDS: dict[identity.MatchStatus | None, str] = {
    identity.MatchStatus.UNMATCHED: "plex_unmatched",
    identity.MatchStatus.AMBIGUOUS: "plex_ambiguous",
    identity.MatchStatus.CONFLICTED: "radarr_plex_disagree",
}


def no_key_reason_id(match_status: identity.MatchStatus | None) -> str:
    """The bare catalog id for why an item has no Plex rating key, with no media wording
    attached. ``None`` (a record from before the field shipped) takes the unmatched wording,
    which it has always read as.

    This is the shape ``season_evidence``'s ``SeasonPruneInput.progress_unknown_reason`` field
    stores: a plain id, because that field's own codec (``season_evidence._KEYS``) freezes it
    as a bare string, and the mid-binge guard (``season_evidence.guard_result``) is the one
    place season context attaches the ``mediaType`` param, since every caller of *this*
    function is season-only anyway.
    """
    return NO_KEY_REASON_IDS.get(match_status, "plex_unmatched")


def no_key_reason(match_status: identity.MatchStatus | None, media_type: MediaKind) -> Reason:
    """The typed cause for a missing Plex rating key: the shared id above, plus which wording
    the panel's ``mediaType`` select should pick. What ``Unknown(reason=...)`` carries directly
    on the movie and season fact builders (rule 72)."""
    return Reason(f"cause.{no_key_reason_id(match_status)}", {"mediaType": media_type})


#: Why dormancy (or an all-time span) could not be measured: matched to Plex, but no arrival
#: date and no play, so there is no instant to measure from. Shared by the movie and season
#: lanes (rule 72: this used to be ``snapshot.NO_ADDED_AT_REASON``/``season_scan.NO_ADDED_AT_
#: REASON``, two constants naming the same concept two different ways). A KEY into the
#: catalog's ``why.cause.*`` entries, named here so the drift test covers it (rule 144).
NO_ADDED_AT_REASON = "no_added_at"

#: Why a file/season's size is unreadable: the *arr reported no size on disk. Shared the same
#: way as the reason above (rule 72: was ``snapshot.NO_SIZE_REASON``/``season_scan.NO_SIZE_
#: REASON``). Reaches the panel through a keep rule on "Size on disk".
NO_SIZE_REASON = "no_file_size"


def no_added_at_reason(media_type: MediaKind) -> Reason:
    """The typed cause for a missing arrival date, media-selected the same way as
    :func:`no_key_reason`."""
    return Reason(f"cause.{NO_ADDED_AT_REASON}", {"mediaType": media_type})


def no_size_reason(media_type: MediaKind) -> Reason:
    """The typed cause for a missing on-disk size, media-selected the same way as
    :func:`no_key_reason`."""
    return Reason(f"cause.{NO_SIZE_REASON}", {"mediaType": media_type})


def _rating_value(rating: Rating) -> Reason:
    """A rating the item really has, as the value clause the panel prints.

    The structured twin of ``ratings.Rating.describe_for_user`` -- percentage sources read
    as a percentage, the 0-10 sources on their own scale with the vote count that makes the
    number mean something -- and the catalog holds the words.
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

    ``floor`` is in tenths (7.5 -> 75), the same convention the whole policy uses, and it
    reads the same for a percentage source: 75% is 75, because 84% arrives normalized to
    8.4 on the 0-10 scale and its tenths are 84. ``min_votes`` only bites on sources that
    count votes (IMDb, TMDb); it is ignored for Rotten Tomatoes and Metacritic, which are
    percentages with no vote concept (see ``ratings.Rating.has_meaningful_vote_count``).
    """

    source: RatingSource
    floor: int
    min_votes: int = 0

    def describe_bar(self) -> Reason:
        """The full bar, for the why-panel and the checked line: the number, the source, and
        the vote floor where the source has one (``7.5 on IMDb from 1,000+ votes``).

        The floor's catalog entries (``why.rating_bar*``) keep their own vote clause rather
        than sharing the value clause's: the why-panel prints a bar and a real count one
        line apart, and sharing the wording made "from 1,000 votes" a floor in one sentence
        and a measurement in the next (#623). The "+" is the whole difference.
        `PolicyEditor.tsx`'s `describeBar` renders this same clause for the same rule; the
        two are pinned together in `test_the_bar_names_its_vote_floor_as_a_floor`.
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
    """Keep anything well-rated by enough people, on ANY source the owner trusts.

    The owner picks a set of bars -- IMDb 7.5 from 1,000 votes, Rotten Tomatoes critics
    75%, and so on -- and a title clearing **any one** of them is kept (or **all**, if the
    owner tightens the match). Each bar reads the frozen rating for its source and the
    single-source case (just IMDb) behaves exactly as the original gate did.

    The vote floor is not optional on the sources that have one: a high score from a
    handful of voters is noise. The rating's *provenance* is pinned per source, never
    inferred: the same Plex field held IMDb on one server and a Rotten Tomatoes percentage
    on another, and ``ratings.Rating`` carries where each number came from so a 7.5 IMDb
    bar is never compared against a Tomatometer of 96.

    A protection, so it has no CONDEMN outcome: a source we could not read, or a rule with
    no matching rating, simply does not fire. It cannot delete a file, only keep one.
    """

    rules: tuple[RatingRule, ...] = ()
    match: Literal["any", "all"] = "any"
    id: GateId = GateId.RATING_FLOOR

    def _miss_reason(self, rule: RatingRule, rating: Rating | None) -> Reason:
        """Why one bar was not cleared, with the item's own numbers where we have them --
        the "checked and did not fire, with the numbers" explainability the panel needs."""
        if rating is None:
            return Reason(
                "rating_miss_none", {"source": rule.source.value, "bar": rule.describe_bar()}
            )
        # The same predicate `evaluate` decides the bar on, so the sentence cannot claim a
        # vote floor was missed on a count `Rating.meets` counted as enough (rule 104).
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

        # Fail closed if a source we keep on could not be read. IMDb is the one source that
        # carries a three-state observation in Facts (imdb_rating_tenths / imdb_votes); the
        # others come from frozen Radarr/Plex data whose failure degrades the whole snapshot
        # upstream, so they are never per-item Unknown. When an IMDb bar's own rating cannot
        # be read, blocking keeps the file rather than silently dropping the protection it
        # was carrying -- the same fail-closed the single-source gate had.
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

        # ANY: one cleared bar keeps it. ALL: every bar must clear, and a source we could
        # not read counts as a miss (there is nothing to clear), so ALL fails closed toward
        # NOT protecting -- the safe direction for a keep, which can only ever spare a file.
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

    Re-checked live in the seconds before the delete, not merely at scan time. No
    shipping competitor does this at all.
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


#: How much shorter than the span it must cover a reach must be before the copy names it
#: in days. Both spans are phrased by ``clock.humanize_days``, whose months are 30 days and
#: whose years are 365, so 360 days renders "12 months" while a 365-day span renders
#: "year": true, and it reads to the operator as though the history were LONGER than the
#: span it cannot cover. One whole month of margin is the cheapest bound that cannot
#: invert, because a reach at least a month short always renders a smaller leading unit.
#: Inside the margin the copy states the comparison instead of the number, which is shorter
#: anyway.
_REACH_NAMEABLE_MARGIN_DAYS = 30


def history_shortfall(reach: Observation[float], needed: float) -> Reason | None:
    """Why the watch mirror cannot cover ``needed`` days, as a typed reason.

    ``None`` when it does cover them -- the only case in which a count drawn from the
    mirror may be read as the answer rather than as a lower bound, because the plays a
    short mirror cannot see are precisely the ones that would have kept the file.

    The single derivation behind every reader of a watcher count (rules 104, 140):
    ``ServerPopularityGate`` below asks it about the policy's popularity window,
    ``fields.reach_shortfall`` asks it for the operator-authored protect, condemn and keep
    lanes, and :func:`lifetime_shortfall` asks it for every all-time count. A bound honored
    in one lane and not the next is the bug it exists to prevent.
    """
    if not isinstance(reach, Known):
        return Reason("cause.reach_not_recorded")
    if reach.value >= needed:
        return None
    if reach.value <= needed - _REACH_NAMEABLE_MARGIN_DAYS:
        return Reason("cause.history_reach_short", {"reach_days": reach.value})
    return Reason("cause.history_not_that_far")


def lifetime_shortfall(reach: Observation[float], age: Observation[float]) -> Reason | None:
    """Why the mirror cannot support an ALL-TIME watcher count, as a typed reason.

    ``None`` when it can. An all-time count is only an answer where the mirror reaches back
    to the day the item arrived, because every play it could ever have had happened after
    that; short of it the count is a LOWER BOUND, and the plays behind the horizon are
    exactly the ones that would have kept the file.

    Without the arrival date there is no span to compare the reach against, so the count
    cannot be established either way -- ``Unknown``, never a permissive ``Absent``
    (rule 93).

    The one derivation of "what span does an all-time count need" (rules 104, 140).
    ``fields.reach_shortfall`` asks it for the operator-authored lanes off ``Facts``, and
    ``services.season_scan`` asks it per season for the keep-rule conflict detector, which
    compares two all-time counts and reads no ``Facts`` at all. That second reader is why
    this is a named helper rather than an arm inside ``reach_shortfall``: a season-path
    caller with no ``Facts`` in hand would otherwise have had to restate the span, which is
    how the first sweep of these readers missed it.
    """
    if not isinstance(age, Known):
        return Reason("cause.added_at_not_recorded")
    return history_shortfall(reach, float(age.value))


def progress_is_establishable(*, reach_days: int, hold_days: int) -> bool:
    """Whether the watch mirror reaches back far enough to answer "who is part-way through".

    The guard holds a viewer whose last play of the show falls inside ``hold_days``. It can
    only see plays the mirror holds, and the mirror begins at its horizon, ``reach_days``
    back, so the answer is sound exactly when the mirror spans the whole hold window. Past
    ``reach_days`` an invisible viewer and an expired one are the same viewer and losing
    them costs nothing; *inside* it they are not, and the viewer simply has no rows.

    That gap is what this predicate exists to name.
    :func:`reaper.services.season_pruning.active_progress` reads no rows as
    "nobody is part-way through" -- a genuine ``Absent`` -- when the truth is "the mirror
    cannot see far enough to know", which is ``Unknown`` (rule 93). It is careful in the
    right way and cannot help: it keeps a viewer whose last-watched time is *unreadable*,
    but this viewer is not unreadable, they are missing, and there is nobody to keep. So the
    caller must ask this separately, and ``services.season_pruning.plan_series_prune`` holds
    every season on disk when the answer is False.

    It lives here beside :func:`history_shortfall` and :func:`lifetime_shortfall` because it is
    the third member of one family -- a span the mirror is asked to cover, and what it means
    when it cannot -- and because ``policy_warnings.inspect`` has to ask it to warn about this one
    before a scan runs into it. An engine module may not import a service, and reimplementing
    the two-line predicate at the second caller is exactly what rule 104 forbids.

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


@dataclass(frozen=True, slots=True)
class ServerPopularityGate:
    """Keep what your users actually watch, regardless of who asked for it.

    The count is drawn from a mirror that begins somewhere (``Facts.history_reach_days``),
    so this gate asks its question twice: how many people watched it, and whether the
    evidence goes back far enough for that number to mean what it says.
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
        # Everything below here says the protection did NOT fire, and that is only an
        # answer if the mirror actually saw the whole window. A history reaching back
        # three months cannot report who watched a title over a year: the count it returns
        # is a lower bound, and the plays it cannot see are precisely the ones that would
        # have kept the file. Printing "nobody watched it" there is the horizon vector
        # (``DataHorizonGate``, ``services.history_sync``) arriving down the watcher lane
        # instead of the dormancy one, so fail closed (rules 2, 93).
        #
        # The PROTECT above deliberately needs no such check: a play seen inside part of
        # the window did happen inside the window, so a lower bound that already clears
        # the floor clears it however much more history arrives.
        if (short := history_shortfall(facts.history_reach_days, window)) is not None:
            return GateResult(
                self.id,
                ABSTAIN,
                blocked=True,
                # This block holds nothing against a hand reap any more -- no blocked gate
                # does (see ``engine.verdict``) -- and on a mirror shorter than the
                # popularity window it is the block an operator meets most often, so
                # keeping it un-overrulable was what made a shallow Tautulli refuse
                # reaps library-wide. It still forces ABSTAIN, which is its real job.
                #
                # The ``blocked`` reason id IS still load-bearing, for the two surfaces
                # that read it: ``api.review._chip`` sends it to "Some checks couldn't
                # run" instead of "left for you to decide", and ``WhyPanel`` reads its
                # check and cause slots. Re-id it and a plumbing failure starts reading
                # to the operator as their own decision.
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


# A `WhitelistGate` (keep tags, the "Never Reap" collection) and a `CuratedListGate` (the
# IMDb Top 250) lived here. Both were live protections, so unlike the two gates deleted for
# never firing they were not simply retired: every list -- tag, collection, watchlist, IMDb
# -- now protects through the operator's own keep rules on the ``on_list`` field, evaluated
# by ``fields.CustomProtectGate``, so each list's strength is a per-list choice on Policy.
# ``policy_migrations.convert_list_protections`` rewrites a stored body's gate rows into the
# equivalent rules, and their `GateId`s survive above so a stored explanation still decodes.


@dataclass(frozen=True, slots=True)
class MinDormancyGate:
    """Nothing may be deleted until it has sat unwatched for long enough.

    A hard gate, not a weight, because a weight can be outvoted by other signals --
    and that is exactly how an early version of this engine ended up condemning films
    that had close to a one-in-three chance of being played again within the year.

    The default threshold is not a guess; it comes from the shape of the rewatch
    curve. That probability **roughly halves at the one-year mark, then decays only
    slowly, and its tail never reaches zero** -- see ``docs/SIGNALS.md``, "There is no
    cliff. Nothing is ever free to delete." Measured on one real library and tabulated
    there under "Ground truth: rewatch probability by dormancy", it runs about 61% inside
    the first year, about 30% between two and three years, about 19% from three to five,
    and about 13% beyond five. So a title one to three years dormant still has close to a
    one-in-three chance of being played again within the year, and past 1,095 days the odds
    improve but never reach free. Dormancy of a year or two means very little -- people
    circle back to films on that timescale all the time.

    That curve is a *property of an audience*, not a universal constant, and the figures
    above are documented defaults measured on one real library, not figures fitted to
    this server. Nothing in Reaper fits one: **the threshold this gate enforces is the
    operator's own stored number** (``config.threshold``), read straight off the policy,
    and nothing adjusts it. The gate exists either way: the shallow tail is the invariant,
    and the threshold is a default the operator may move.
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
            # We cannot establish that it HAS been dormant long enough, so we must not delete
            # it. Reachable for ``Absent`` only: ``_blocked`` above already answered every
            # ``Unknown`` with a blocked ABSTAIN, which is a different hold from this PROTECT
            # (rule 143's corollary -- "we could not answer" is blocked, never a bare PROTECT).
            # No fact builder emits ``Absent`` for this field today, so the PROTECT does not
            # fire; it stays because ``Absent`` arriving later must keep the file, and the
            # ``isinstance`` is load-bearing regardless -- ``_blocked`` returns a result, not a
            # narrowed type, so ``.value`` below needs it.
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


#: The cohort size under which a fitted rewatch block displays no number and can never fire
#: the hold (``docs/history/REWATCH_PLAN.md``, stage 2). It lives here rather than in
#: ``services/rewatch.py`` because ``RewatchOddsGate`` below reads it and an engine module
#: may not import a service.
REWATCH_BLOCK_FLOOR_N = 30


def wilson_upper(k: int, n: int) -> float:
    """The Wilson 95% upper bound of ``k/n``, as a fraction.

    The hold compares this rather than the point rate so a small library never loses
    protection to sampling noise; it converges to ``k/n`` as ``n`` grows. One derivation,
    read by the gate below and by the policy page's consequence echo (``api.policy``)."""
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

    Opt-in on both lanes, and the one gate that reads the fitted rewatch curve: the item's
    frozen cohort (``Facts.rewatch_cohort_n`` / ``rewatch_cohort_k``) is its merged dormancy
    block from the per-scan fit -- a movie's own, a season's the show's
    (``docs/LEARNINGS.md``, the TV fit entry) -- and the comparison is the Wilson 95% upper
    bound of that block's rate against ``config.threshold`` percent.

    **An unreadable cohort abstains without blocking, and that is a documented deviation**
    from the fail-closed ``_blocked`` arm every other gate takes (rule 143's corollary,
    owned here in writing): the plan states a withheld block never blocks and never
    condemns, because on a shallow mirror most of the library has no measurable cohort and
    an opt-in extra protection must not amber-flag all of it. The items whose history is
    genuinely unreadable are already blocked by the dormancy and popularity gates reading
    the same sources, so failing quiet here withdraws no cover (``docs/history/REWATCH_PLAN.md``,
    stage 2, "The hold").
    """

    config: GateConfig
    id: GateId = GateId.REWATCH_ODDS
    media_type: MediaKind = "movie"
    """Which wording the panel's ICU ``mediaType`` select should pick for this gate's four
    cohort Reasons -- "movie" or "season". ``Facts`` carries no media discriminator of its own
    (unlike ``KeepConfig.media_type`` in ``engine/signals.py``, the rewatch keep's own twin), so
    this is set once at construction (``scan_runner.build_gates``, off the policy's own
    ``media_type``) rather than read per item. Defaulted to "movie" so a hand-built gate in a
    test needs no opinion about it; rule 141 is answered by
    ``test_signal_quality.py::TestTheRewatchOddsGate``, which sweeps both values across all four
    Reasons."""

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
            # Hand-built Facts that never gathered a curve: both live lanes freeze a
            # cohort now, so a genuine Absent means nothing to compare (rule 93's Absent,
            # not a failed read).
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
        # The number the decision itself compares (#936): a 0-of-30 cohort can still clear
        # a low floor on this bound alone, so the sentence quotes IT, not the point rate --
        # "0 of 30 keep getting watched" was the bug this fixes. Rounded for display only;
        # the comparison above stays on the unrounded value so display precision never
        # moves the decision.
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
                    # keep"), and plain rounding breaks that order near the floor: a 24.9
                    # bound at a 25 floor would display as "25%, under the 25% you keep".
                    # Clamp so the displayed bound stays under the floor it is under.
                    "bound_pct": min(bound_pct, floor - 1),
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class ReturnedGate:
    """Hold a title that left the library and came back.

    A return is the clearest evidence Reaper can get that removing something was wrong:
    somebody went and fetched it again. What counts as one is decided in the scan, not here --
    four conditions over Plex rating keys, a minimum absence and a count of the scans that ran
    during it (``services.library_seen``) -- and this gate reads the one number that came out
    of it. That split is what lets an operator move the hold's length and have the simulator
    replay it exactly, the same reason ``Facts.rewatch_last_play_days`` is frozen raw.

    ``config.threshold`` is how long the hold lasts, in days. ``config.window_days`` is the
    minimum absence, read by the scan rather than by this gate.

    **An unreadable return abstains without blocking, and that is a documented deviation**
    from the fail-closed ``_blocked`` arm every other gate takes (rule 143's corollary, owned
    here in writing, exactly as ``RewatchOddsGate`` owns its own). The ledger is EMPTY on a
    fresh install and on every install the scan after this ships, so blocking on ``Unknown``
    would amber-flag the entire library and abstain every verdict in it until the ledger
    filled -- months, during which Reaper could condemn nothing at all. The items whose Plex
    bind genuinely failed are already blocked by the four Plex-dependent gates reading the same
    resolution, so failing quiet here withdraws no cover.
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
        # Rounded UP, so a hold with any of itself left never reads as spent. Rule 31: the
        # bound that produces less deletion pressure is the one that keeps the file.
        left = math.ceil(hold - returned.value)
        if left <= 0:
            return GateResult(
                self.id,
                ABSTAIN,
                detail=Reason("returned_past_hold", {"days": returned.value, "hold_days": hold}),
            )
        by_reaper = facts.returned_by_reaper
        removed_by_us = isinstance(by_reaper, Known) and by_reaper.value
        # The journal's one job. Same hold either way: splitting the length would mean a
        # second knob for a difference nobody has measured. Two ids so the lead can say
        # whether Reaper itself removed it; both carry the countdown the chip reads.
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

    Tautulli cannot import Plex history from before it was installed, so everything watched
    before that looks never-watched -- the single biggest mass-deletion vector in the whole
    ecosystem. It arrives down two lanes, and neither defense is this gate's. On the watcher
    lane, ``ServerPopularityGate`` refuses to report a protection as checked over a window
    its history does not span (``Facts.history_reach_days``). On the dormancy lane, which is
    the rest of this docstring, the defense lives in fact *derivation*:
    dormancy is measured from ``max(added_at, horizon)`` (``engine.dormancy.reference_instant``,
    through ``services.snapshot.build_facts`` and its season twin), so a pre-horizon item
    is clamped to the horizon rather than read as decades dormant.

    This gate's only independent job, therefore, is to fail closed when dormancy is
    ``Unknown`` -- it is not handed ``added_at`` and cannot itself re-check the clamp, so it
    must not claim to have. It duplicates ``MinDormancyGate``'s Unknown fail-closed on
    purpose: defense-in-depth on the one fact whose absence would otherwise condemn the item
    we know least about. When dormancy is Known it abstains, and it does NOT assert that
    history "covers" the item, because it never independently verified that.
    """

    config: GateConfig
    id: GateId = GateId.DATA_HORIZON

    def evaluate(self, facts: Facts) -> GateResult:
        if blocked := _blocked(self.id, facts.days_observed_unwatched, "watch_horizon"):
            return blocked
        return GateResult(self.id, ABSTAIN, detail=Reason("data_horizon_ok"))


# An `UnmanagedGate` ("if no *arr owns it, Reaper cannot delete it") lived here, enabled by
# default in both shipped policies. It could not fire. Reaper builds its candidate list BY
# asking Sonarr and Radarr what they hold, so a file neither owns can never reach the set this
# gate filtered: both evidence builders of ``Facts.is_managed`` write a hardcoded ``Known(True)``
# (`snapshot`, `season_scan`), and neither of the two other `Facts` constructors can reach a gate
# with anything else -- `facts_codec.facts_from_dict` thaws only what a builder already wrote, and
# `preview._bare_facts` does write ``Unknown`` here but is handed to `evaluate_signal` alone,
# never to `evaluate_all`. That last one is the load-bearing half, so it is stated rather than
# left to a count of construction sites. The PROTECT branch
# and the gate's half of ``verdict.STRUCTURAL_GATES`` were both unreachable while the operator
# saw a switch, on by default, warned in red if they turned it off (rule 38/117).
#
# It differs from `OthersWatchingGate` below in a way worth recording: THAT gate's input was
# never gathered, so its ABSTAIN line asserted a check that never ran. This one's input is
# genuinely observed -- the item did come from an *arr -- so "Managed by Sonarr or Radarr" was
# true, and no file was ever wrongly deleted by it. Retired as dead safety-adjacent code, not
# as a hole.
#
# ``Facts.is_managed`` stays: it is a real observation, frozen into every stored snapshot, and
# it is exactly what a re-wiring would need. Bringing the gate back means giving Reaper a scan
# path that can find media NO *arr manages -- reading Plex directly rather than the *arrs --
# so that the fact can be something other than True. Gate, builders and tests return together.
# ``GateId.UNMANAGED`` survives so a stored explanation still decodes. Three surfaces read one
# back, and all three stay for that reason: ``verdict.STRUCTURAL_GATES``, `api.review`'s chip
# phrasing, and `WhyPanel.tsx`'s held-reap line. A stored blocked detail naming "which *arr owns
# this" (this gate's own blocked branch, whose only producer was the code deleted here) is a
# legacy sentence now: no typed check id ever backed it, so it renders through the panel's
# generic legacy fallback rather than through copy of its own (#899).


# An `OthersWatchingGate` ("the requester ignored it, but other people did not") lived here.
# No fact builder ever produced a Known ``others_watching`` -- every one wrote Absent -- so
# the count was always 0 against a floor of at least 1 and
# the gate could not PROTECT anything, while its ABSTAIN line read to the owner like a check
# that ran. A protection that cannot fire is deleted, not stockpiled (rule 38): the evidence
# it needs (per-user plays excluding the requester) is not gathered anywhere in the scan.
# ``GateId.OTHERS_WATCHING`` survives so a stored explanation written while it was built can
# still be decoded, and ``scan_runner.GATE_TYPES`` no longer carries it, so a policy that
# still enables it refuses to scan rather than running a protection that keeps nothing.
# Wiring it back means gathering the count first, then restoring gate, fact and builders
# together.


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

        Deliberately distinct from ``protected``: "we did not manage to look" is
        not the same as "we looked and it is fine", and treating them alike is
        exactly how these tools delete during an outage.
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

    Stopping at the first protection would be faster and would destroy the product:
    the "checked and did not fire, with the numbers" block requires that every gate
    reports, including the ones that had nothing to say.
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
