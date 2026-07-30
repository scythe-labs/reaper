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
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from reaper.clock import humanize_days, humanize_window
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.ratings import (
    Rating,
    RatingSource,
    describe_votes,
    is_percentage_source,
    source_label,
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
    STREAMING_NOW = "streaming_now"
    RATING_FLOOR = "rating_floor"
    SERVER_POPULARITY = "server_popularity"

    OTHERS_WATCHING = "others_watching"
    """Retired: no gate implements it and no fact builder gathers the count (see the note
    where OthersWatchingGate used to be). Kept only so an explanation stored while it was
    built still decodes; ``scan_runner.GATE_TYPES`` refuses to build it."""

    CURATED_LIST = "curated_list"
    DATA_HORIZON = "data_horizon"

    UNMANAGED = "unmanaged"
    """Retired: the candidate set is built by asking Sonarr and Radarr what they hold, so
    every fact builder wrote ``Known(True)`` and the gate could not fire (see the note where
    UnmanagedGate used to be). Kept only so an explanation stored while it was built still
    decodes; ``scan_runner.GATE_TYPES`` refuses to build it."""

    MIN_DORMANCY = "min_dormancy"
    """The most important gate. Nothing under the dormancy floor may be deleted at
    all, whatever else it scores. See MinDormancyGate."""

    SEASON_PROGRESSION = "season_progression"
    """Not authorable in a policy. The engine emits it from the season judgment
    (``season_scan.guard_result``); no policy row builds it."""

    CUSTOM = "custom"
    """Not authorable in a policy. Tags the result of an operator-authored custom rule,
    which is configured under ``custom_condemn``, not as a gate."""


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
        GateId.WHITELISTED,
        GateId.STREAMING_NOW,
        GateId.RATING_FLOOR,
        GateId.SERVER_POPULARITY,
        GateId.CURATED_LIST,
        GateId.DATA_HORIZON,
        GateId.MIN_DORMANCY,
    }
)


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's verdict, and the numbers behind it.

    ``detail`` is not a log line -- it is what the user reads. Every gate that was
    checked and did *not* fire still reports its actual figures, because
    "protections checked that did not fire, with the numbers" is the block that
    makes a deletion trustworthy.
    """

    gate: GateId
    outcome: GateOutcome
    detail: str

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
    was always the more honest use of it. ``season_scan.guard_result`` sets it when every
    ``season_pruning.PruneConflict`` naming the season was a comparison Reaper could
    actually make: a readable ``kept_watchers`` AND a ``shortfall`` of ``None``. The
    second is not implied by the first -- a count read off a mirror that does not reach
    back to when the season arrived is a number all the same, and settles nothing (#94).
    ``api.routes._chip`` reads it to choose between "this was watched more than a season
    your rule keeps" and "couldn't check who watched these seasons", which are genuinely
    different things to tell someone deciding what to delete.

    The ``False`` default still matters for that: a producer that forgets says "Reaper did
    not establish this", which is the claim that asserts less. Typed rather than inferred
    from the detail text (rule 142) -- the wording test it replaced,
    ``detail.startswith("could not check")``, never matched the one message it existed
    for, because that message opens with the watcher count.
    """

    @property
    def fired(self) -> bool:
        return self.outcome == PROTECT


def thaw_defers_to_owner(value: object) -> bool | None:
    """``defers_to_owner`` as it comes off a stored explanation: three states, no coercion.

    The one derivation, because it had two and they disagreed (rule 104). ``api.routes._chip``
    read the raw dict with ``is True`` / ``is False``, so anything else fell to its
    vague-but-true chip; ``api.schemas.GateOutcomeOut`` read the same byte through Pydantic's
    lax bool coercion, which takes ``1`` and ``"true"`` as True, ``0`` as False, and REFUSES
    ``2``, ``"banana"``, ``[]`` and ``{}``. A refusal there is not a smaller failure than a
    disagreement: it fails the whole ``Explanation``, so ``_explanation_out`` falls to its
    degraded body and the operator gets a panel with no signals, no protections and no
    threshold -- while the chip beside it, the reap-override read, and every other extractor
    go on reading that same row perfectly well, and a hand Reap on it still condemns.

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
    is_whitelisted: Observation[bool]

    # --- fields authorable in custom rules (the weighting feature) --------------------
    # Given fail-safe defaults so the non-production Facts builders (backtest
    # reconstruction, calibration, test fixtures) need not enumerate them; the live scan
    # builders set them explicitly, which they must -- ``Absent`` is fail-closed on the
    # condemn and gate lanes but not on the keep lane. See ``_UNSET``.
    requested: Observation[bool] = _UNSET
    """Was this title asked for via Seerr? Three-state: ``Unknown`` when Seerr is
    absent/partial or the item has no id to join on -- never coerced to ``False``, which
    would add delete pressure on missing data. Set in build_facts / build_season_facts."""

    genres: Observation[str] = _UNSET
    """The *arr's genres, comma-joined. ``Absent`` when the payload carries none."""

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

    ratings: tuple[Rating, ...] = ()
    """Every interpretable rating the scan froze for this item, one per source (IMDb,
    TMDb, Rotten Tomatoes critics/audience, Metacritic). Read only by the multi-source
    ``RatingFloorGate``, a protection, so a missing or unreadable source can only ever
    fail to keep a title, never condemn one. Empty by default and for the historical
    reconstruction (backtest/calibration have no rating source), which simply means the
    gate does not fire. Not hashed (Facts is evidence, not policy)."""


@dataclass(frozen=True, slots=True)
class GateConfig:
    """The user-tunable part of a gate. Integers only -- see ``policy``."""

    gate: GateId
    enabled: bool = True
    threshold: int = 0
    #: No ``secondary`` here. It carried the rating gate's vote floor until that bar moved to
    #: ``PolicyBody.keep_rating_rules``, where it is now ``RatingRule.min_votes``, read by
    #: ``RatingFloorGate.evaluate`` and the ``_miss_phrase`` helper it calls. After the move no
    #: gate read ``secondary``, and ``scan_runner`` was copying a dead number into every gate it
    #: built. The policy body still stores it -- see ``policy.GateSetting.secondary`` for why
    #: that one cannot follow.

    window_days: int = 365
    """How far back "recently" reaches, for gates that count activity.

    Not cosmetic. An unwindowed popularity gate protects anything anyone ever
    played, which silently disables the whole scorer."""


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


def _blocked(gate: GateId, observation: Observation[object], what: str) -> GateResult | None:
    """Fail closed on an Unknown input."""
    if isinstance(observation, Unknown):
        return GateResult(
            gate=gate,
            outcome=ABSTAIN,
            blocked=True,
            detail=f"could not check {what}: {observation.reason}",
        )
    return None


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

    def threshold_text(self) -> str:
        """Just the number, in the source's own units: ``7.5`` or ``75%``."""
        return f"{self.floor}%" if is_percentage_source(self.source) else f"{self.floor / 10:.1f}"

    def describe_bar(self) -> str:
        """The full bar, for the why-panel and the checked line: the number, the source, and
        the vote floor where the source has one (``7.5 on IMDb from 1,000 votes``)."""
        if is_percentage_source(self.source):
            return f"{source_label(self.source)} {self.floor}%"
        # `describe_votes` is the one derivation of this clause (rule 104); it also renders a
        # count of 1 as "vote", which all three copies of the phrase used to get wrong.
        return (
            f"{self.threshold_text()} on {source_label(self.source)}"
            f"{describe_votes(self.min_votes)}"
        )


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

    def _miss_phrase(self, rule: RatingRule, rating: Rating | None) -> str:
        """Why one bar was not cleared, with the item's own numbers where we have them --
        the "checked and did not fire, with the numbers" explainability the panel needs."""
        if rating is None:
            return f"no {source_label(rule.source)} rating (you keep {rule.describe_bar()})"
        too_few_votes = (
            rule.min_votes > 0
            and rating.has_meaningful_vote_count
            and (rating.votes is None or rating.votes < rule.min_votes)
        )
        if too_few_votes:
            return f"{rating.describe_for_user()}, too few to trust (you need {rule.min_votes:,})"
        return f"{rating.describe_for_user()}, below the {rule.threshold_text()} you keep"

    def evaluate(self, facts: Facts) -> GateResult:
        if not self.rules:
            return GateResult(self.id, ABSTAIN, detail="No rating is set that would keep a title.")

        # Fail closed if a source we keep on could not be read. IMDb is the one source that
        # carries a three-state observation in Facts (imdb_rating_tenths / imdb_votes); the
        # others come from frozen Radarr/Plex data whose failure degrades the whole snapshot
        # upstream, so they are never per-item Unknown. When an IMDb bar's own rating cannot
        # be read, blocking keeps the file rather than silently dropping the protection it
        # was carrying -- the same fail-closed the single-source gate had.
        if any(r.source is RatingSource.IMDB for r in self.rules):
            if blocked := _blocked(self.id, facts.imdb_rating_tenths, "the IMDb rating"):
                return blocked
            if blocked := _blocked(self.id, facts.imdb_votes, "the IMDb vote count"):
                return blocked

        by_source = {r.source: r for r in facts.ratings}
        cleared: list[str] = []
        missed: list[str] = []
        for rule in self.rules:
            rating = by_source.get(rule.source)
            if rating is not None and rating.meets(rule.floor / 10, min_votes=rule.min_votes):
                cleared.append(rating.describe_for_user())
            else:
                missed.append(self._miss_phrase(rule, rating))

        # ANY: one cleared bar keeps it. ALL: every bar must clear, and a source we could
        # not read counts as a miss (there is nothing to clear), so ALL fails closed toward
        # NOT protecting -- the safe direction for a keep, which can only ever spare a file.
        protects = bool(cleared) if self.match == "any" else (not missed and bool(self.rules))
        if protects:
            # No trailing period: a fired protection reads as a lowercase fragment in the
            # "Protections that fired" list, alongside "someone is watching it right now"
            # and "on your keep list, never reaped". The ABSTAIN details below are full
            # sentences by the same convention, so they keep theirs.
            return GateResult(self.id, PROTECT, detail="well rated: " + "; ".join(cleared))
        if cleared:
            return GateResult(
                self.id,
                ABSTAIN,
                detail=(
                    "cleared "
                    + "; ".join(cleared)
                    + ", but not every bar you asked for: "
                    + "; ".join(missed)
                    + "."
                ),
            )
        return GateResult(self.id, ABSTAIN, detail="; ".join(missed) + ".")


@dataclass(frozen=True, slots=True)
class StreamingNowGate:
    """Never delete something that is playing right now.

    Re-checked live in the seconds before the delete, not merely at scan time. No
    shipping competitor does this at all.
    """

    config: GateConfig
    id: GateId = GateId.STREAMING_NOW

    def evaluate(self, facts: Facts) -> GateResult:
        if blocked := _blocked(self.id, facts.is_streaming_now, "active streams"):
            return blocked
        streaming = facts.is_streaming_now
        if isinstance(streaming, Known) and streaming.value:
            return GateResult(self.id, PROTECT, detail="someone is watching it right now")
        return GateResult(self.id, ABSTAIN, detail="Nobody is watching it right now.")


#: How much shorter than the span it must cover a reach must be before the copy names it
#: in days. Both spans are phrased by ``clock.humanize_days``, whose months are 30 days and
#: whose years are 365, so 360 days renders "12 months" while a 365-day span renders
#: "year": true, and it reads to the operator as though the history were LONGER than the
#: span it cannot cover. One whole month of margin is the cheapest bound that cannot
#: invert, because a reach at least a month short always renders a smaller leading unit.
#: Inside the margin the copy states the comparison instead of the number, which is shorter
#: anyway.
_REACH_NAMEABLE_MARGIN_DAYS = 30


def history_shortfall(reach: Observation[float], needed: float) -> str | None:
    """Why the watch mirror cannot cover ``needed`` days, in the operator's words.

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
        return "this scan did not record how far back your watch history goes"
    if reach.value >= needed:
        return None
    if reach.value <= needed - _REACH_NAMEABLE_MARGIN_DAYS:
        return f"your watch history only goes back {humanize_days(reach.value)}"
    return "your watch history does not go back that far"


def lifetime_shortfall(reach: Observation[float], age: Observation[float]) -> str | None:
    """Why the mirror cannot support an ALL-TIME watcher count, in the operator's words.

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
        return "this scan did not record when it was added"
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
    when it cannot -- and because ``policy.inspect`` has to ask it to warn about this one
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
        if blocked := _blocked(self.id, facts.distinct_watchers, "watch history"):
            return blocked
        watchers = facts.distinct_watchers
        count = watchers.value if isinstance(watchers, Known) else 0
        floor = self.config.threshold

        window = self.config.window_days

        window_text = humanize_window(window)
        if count >= floor:
            people = "person" if count == 1 else "people"
            return GateResult(
                self.id,
                PROTECT,
                detail=f"watched here: {count} {people} in the last {window_text}",
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
                # The "could not check ..." prefix IS still load-bearing, for the two
                # surfaces that read it: ``api.routes._chip`` sends a detail starting
                # with it to "Some checks couldn't run" instead of "left for you to
                # decide", and ``WhyPanel`` splits it into check and cause. Reword it and a
                # plumbing failure starts reading to the operator as their own decision.
                detail=f"could not check who watched it in the last {window_text}: {short}",
            )
        if count == 0:
            return GateResult(
                self.id,
                ABSTAIN,
                detail=f"Nobody here watched it in the last {window_text}.",
            )
        people = "person" if count == 1 else "people"
        return GateResult(
            self.id,
            ABSTAIN,
            detail=(
                f"Only {count} {people} watched it here in the last {window_text} "
                f"(it takes {floor} to keep on that alone)."
            ),
        )


@dataclass(frozen=True, slots=True)
class WhitelistGate:
    """A tag in Sonarr/Radarr, or membership of a Plex collection.

    Deliberately reuses interfaces the owner already has: a `reaper-keep` tag, or a
    "Never Reap" collection curated in the Plex app -- editable from a phone, with
    no new screen to learn.
    """

    config: GateConfig
    id: GateId = GateId.WHITELISTED

    def evaluate(self, facts: Facts) -> GateResult:
        if blocked := _blocked(self.id, facts.is_whitelisted, "the whitelist"):
            return blocked
        listed = facts.is_whitelisted
        if isinstance(listed, Known) and listed.value:
            return GateResult(self.id, PROTECT, detail="on your keep list, never reaped")
        return GateResult(self.id, ABSTAIN, detail="Not on your keep list.")


@dataclass(frozen=True, slots=True)
class CuratedListGate:
    """Membership of a protected list -- the IMDb Top 250, and anything else."""

    config: GateConfig
    id: GateId = GateId.CURATED_LIST

    def evaluate(self, facts: Facts) -> GateResult:
        if blocked := _blocked(self.id, facts.in_curated_list, "curated lists"):
            return blocked
        member = facts.in_curated_list
        if isinstance(member, Known) and member.value:
            return GateResult(self.id, PROTECT, detail=f"on a protected list: {member.value}")
        return GateResult(self.id, ABSTAIN, detail="Not on any protected list.")


@dataclass(frozen=True, slots=True)
class MinDormancyGate:
    """Nothing may be deleted until it has sat untouched for long enough.

    A hard gate, not a weight, because a weight can be outvoted by other signals --
    and that is exactly how an early version of this engine ended up condemning films
    that had close to a one-in-three chance of being played again within the year.

    The default threshold is not a guess; it comes from the shape of the rewatch
    curve. That probability **roughly halves at the one-year mark, then decays only
    slowly, and its tail never reaches zero** -- see ``docs/SIGNALS.md``, "There is no
    cliff. Nothing is ever free to delete." Measured on one real library
    (``backtest.FALLBACK_REWATCH_PRIOR``) it runs about 61% inside the first year, about
    30% between two and three years, about 19% from three to five, and about 13% beyond
    five. So a title one to three years dormant still has close to a one-in-three chance
    of being played again within the year, and past 1,095 days the odds improve but never
    reach free. Dormancy of a year or two means very little -- people circle back to
    films on that timescale all the time.

    That curve is a *property of an audience*, not a universal constant, and the figures
    above are documented defaults measured on one real library, not figures fitted to
    this server. **The threshold this gate enforces is the operator's own stored number**
    (``config.threshold``), read straight off the policy: nothing adjusts it.
    ``engine.calibration`` derives a bucketed rewatch prior, which is the backtest's lift
    baseline and not this threshold, and it has no caller in ``src/`` in any case (see the
    note at the top of that module). The gate exists either way: the shallow tail is the
    invariant, and the threshold is a default the operator may move.
    """

    config: GateConfig
    id: GateId = GateId.MIN_DORMANCY

    def evaluate(self, facts: Facts) -> GateResult:
        blocked = _blocked(self.id, facts.days_observed_unwatched, "when it was last watched")
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
            return GateResult(
                self.id, PROTECT, detail="no watch history, so its dormancy cannot be established"
            )

        if dormant.value < floor:
            # "Untouched", not "last watched": for a never-played item the clock runs from
            # the day it arrived, and claiming a watch that never happened would be a lie
            # in the one panel whose job is to be believed.
            return GateResult(
                self.id,
                PROTECT,
                detail=(
                    f"untouched for just {humanize_days(dormant.value)}, "
                    f"less than the {humanize_window(floor)} Reaper waits"
                ),
            )
        return GateResult(
            self.id,
            ABSTAIN,
            detail=(
                f"Untouched for {humanize_days(dormant.value)}, past the "
                f"{humanize_window(floor)} it has to sit unwatched first."
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
    dormancy is measured from ``max(added_at, horizon)`` (see ``services.snapshot.build_facts``,
    ``engine.backtest.facts_as_of`` and ``engine.calibration.derive``), so a pre-horizon item
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
        if blocked := _blocked(self.id, facts.days_observed_unwatched, "the watch horizon"):
            return blocked
        return GateResult(
            self.id,
            ABSTAIN,
            detail="Unwatched time is known, never counted further back than its history goes.",
        )


# An `UnmanagedGate` ("if no *arr owns it, Reaper cannot delete it") lived here, enabled by
# default in both shipped policies. It could not fire. Reaper builds its candidate list BY
# asking Sonarr and Radarr what they hold, so a file neither owns can never reach the set this
# gate filtered: every builder of ``Facts.is_managed`` writes a hardcoded ``Known(True)``
# (`snapshot`, `season_scan`, `backtest`), and the only other place a `Facts` is constructed
# is `facts_codec.facts_from_dict`, which can thaw only what a builder already wrote. The
# PROTECT branch
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
# ``GateId.UNMANAGED`` survives so a stored explanation still decodes. Four surfaces read one
# back, and all four stay for that reason: ``verdict.STRUCTURAL_GATES``, `api.routes`' chip
# phrasing, `WhyPanel.tsx`'s held-reap line, and `WhyPanel.tsx`'s ``CHECK_COPY`` entry for
# "which *arr owns this", which was this gate's blocked branch and whose only producer was the
# code deleted here.


# An `OthersWatchingGate` ("the requester ignored it, but other people did not") lived here.
# No fact builder ever produced a Known ``others_watching`` -- snapshot, season_scan and
# backtest all wrote Absent -- so the count was always 0 against a floor of at least 1 and
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
    "CuratedListGate",
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
    "ServerPopularityGate",
    "StreamingNowGate",
    "WhitelistGate",
    "evaluate_all",
]
