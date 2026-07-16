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
from dataclasses import dataclass, field, fields
from typing import Literal, Protocol

from reaper.clock import humanize_days, humanize_window
from reaper.engine.observation import Absent, Known, Observation, Unknown, describe

GateOutcome = Literal["PROTECT", "ABSTAIN"]
PROTECT: GateOutcome = "PROTECT"
ABSTAIN: GateOutcome = "ABSTAIN"

#: Fail-safe default for the custom-rule fact fields below. A shared, immutable ``Absent``
#: singleton: ``Absent`` never matches a condemn comparison and never protects, so a Facts
#: builder that does not set one of these fields cannot change any verdict. The live scan
#: builders set them explicitly.
_UNSET: Absent = Absent(source="unset")


class GateId(enum.StrEnum):
    WHITELISTED = "whitelisted"
    STREAMING_NOW = "streaming_now"
    RATING_FLOOR = "rating_floor"
    SERVER_POPULARITY = "server_popularity"
    OTHERS_WATCHING = "others_watching"
    CURATED_LIST = "curated_list"
    DATA_HORIZON = "data_horizon"
    UNMANAGED = "unmanaged"

    MIN_DORMANCY = "min_dormancy"
    """The most important gate. Nothing under the dormancy floor may be deleted at
    all, whatever else it scores. See MinDormancyGate."""

    SEASON_PROGRESSION = "season_progression"
    CUSTOM = "custom"


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

    @property
    def fired(self) -> bool:
        return self.outcome == PROTECT


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
    ``max(added_at, history_begins_at)``.

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
    others_watching: Observation[int]

    # --- fields authorable in custom rules (the weighting feature) --------------------
    # Given fail-safe defaults so the non-production Facts builders (backtest
    # reconstruction, calibration, test fixtures) need not enumerate them; the live scan
    # builders set them explicitly. ``Absent`` is fail-closed on every lane -- it never
    # matches a condemn comparison and never protects.
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

    def unknowns(self) -> list[Unknown]:
        # fields(), not vars(): slots=True removes __dict__, so vars() raises.
        return [value for f in fields(self) if isinstance(value := getattr(self, f.name), Unknown)]


@dataclass(frozen=True, slots=True)
class GateConfig:
    """The user-tunable part of a gate. Integers only -- see ``policy``."""

    gate: GateId
    enabled: bool = True
    threshold: int = 0
    secondary: int = 0
    """A second coupled number, where the gate needs one: the rating gate's vote
    floor lives here, because a rating floor without a vote floor protects an 8.3
    from 388 votes."""

    window_days: int = 365
    """How far back "recently" reaches, for gates that count activity.

    Not cosmetic. An unwindowed popularity gate protects anything anyone ever
    played, which silently disables the whole scorer."""


# ---------------------------------------------------------------------------
# The catalogue
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
class RatingFloorGate:
    """Keep anything well-rated by enough people.

    The vote floor is not optional. A high score from a handful of voters is noise --
    an 8.3 from a few hundred votes says nothing -- and a bare rating floor would
    preserve such an item forever. Every real library holds some of these.

    The rating's *provenance* is pinned by the policy, never inferred: the same
    Plex field held IMDb on one server and a Rotten Tomatoes percentage on another,
    and comparing a 7.5 floor against a Tomatometer of 96 protects nothing at all.
    """

    config: GateConfig
    id: GateId = GateId.RATING_FLOOR

    def evaluate(self, facts: Facts) -> GateResult:
        floor, min_votes = self.config.threshold, self.config.secondary

        if blocked := _blocked(self.id, facts.imdb_rating_tenths, "the IMDb rating"):
            return blocked
        if blocked := _blocked(self.id, facts.imdb_votes, "the IMDb vote count"):
            return blocked

        rating, votes = facts.imdb_rating_tenths, facts.imdb_votes

        if not isinstance(rating, Known):
            return GateResult(
                self.id,
                ABSTAIN,
                detail=f"no IMDb rating to keep it on (you keep {floor / 10:.1f}+)",
            )
        if not isinstance(votes, Known):
            return GateResult(self.id, ABSTAIN, detail="no IMDb vote count")

        rating_value, vote_value = rating.value, votes.value

        if vote_value < min_votes:
            return GateResult(
                self.id,
                ABSTAIN,
                detail=(
                    f"rated {rating_value / 10:.1f} on IMDb, but from only {vote_value:,} votes, "
                    f"too few to trust (you need {min_votes:,})"
                ),
            )
        if rating_value >= floor:
            return GateResult(
                self.id,
                PROTECT,
                detail=(
                    f"well rated: {rating_value / 10:.1f} on IMDb from {vote_value:,} votes, "
                    f"at or above the {floor / 10:.1f} you keep"
                ),
            )
        return GateResult(
            self.id,
            ABSTAIN,
            detail=(
                f"rated {rating_value / 10:.1f} on IMDb from {vote_value:,} votes, "
                f"below the {floor / 10:.1f} you keep"
            ),
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
        if blocked := _blocked(self.id, facts.is_streaming_now, "active streams"):
            return blocked
        streaming = facts.is_streaming_now
        if isinstance(streaming, Known) and streaming.value:
            return GateResult(self.id, PROTECT, detail="someone is watching it right now")
        return GateResult(self.id, ABSTAIN, detail="Nobody is watching it right now.")


@dataclass(frozen=True, slots=True)
class ServerPopularityGate:
    """Keep what your users actually watch, regardless of who asked for it."""

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
    curve. The probability that a dormant film is played again within a year decays
    slowly at first and then falls off a cliff at roughly **three years**: below about
    1,095 days of dormancy, deleting is close to a coin-flip against your users, and
    beyond about 1,825 days it is nearly free. Dormancy of a year or two means very
    little -- people circle back to films on that timescale all the time.

    That curve is a *property of an audience*, not a universal constant, so Reaper
    derives it from the operator's own watch history at calibration time (see
    ``engine.calibration``) and only falls back to a documented default curve when
    there is not enough history to fit one. The gate exists either way: the cliff is
    the invariant, its exact position is not.
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
            # We cannot establish that it HAS been dormant long enough, so we must
            # not delete it. Unknown protects.
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
                    f"untouched for just {humanize_days(dormant.value)}, less than the "
                    f"{humanize_days(floor)} Reaper waits before removing anything. Titles left "
                    "alone for under three years still get watched again a fifth to a third of the "
                    "time."
                ),
            )
        return GateResult(
            self.id,
            ABSTAIN,
            detail=(
                f"Untouched for {humanize_days(dormant.value)}, past the "
                f"{humanize_days(floor)} it has to sit unwatched first."
            ),
        )


@dataclass(frozen=True, slots=True)
class DataHorizonGate:
    """Fail closed when we cannot say how long an item has gone unwatched.

    Tautulli cannot import Plex history from before it was installed, so everything watched
    before that looks never-watched -- the single biggest mass-deletion vector in the whole
    ecosystem. The real defence against it lives in fact *derivation*, not in this gate:
    dormancy is measured from ``max(added_at, horizon)`` (see ``services.snapshot.build_facts``,
    ``engine.backtest.facts_as_of`` and ``engine.calibration.derive``), so a pre-horizon item
    is clamped to the horizon rather than read as decades dormant.

    This gate's only independent job, therefore, is to fail closed when dormancy is
    ``Unknown`` -- it is not handed ``added_at`` and cannot itself re-check the clamp, so it
    must not claim to have. It duplicates ``MinDormancyGate``'s Unknown fail-closed on
    purpose: defence-in-depth on the one fact whose absence would otherwise condemn the item
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
            detail="Dormancy is known (clamped to the watch horizon during fact derivation).",
        )


@dataclass(frozen=True, slots=True)
class UnmanagedGate:
    """If no *arr owns it, Reaper cannot delete it -- it has no filesystem path."""

    config: GateConfig
    id: GateId = GateId.UNMANAGED

    def evaluate(self, facts: Facts) -> GateResult:
        if blocked := _blocked(self.id, facts.is_managed, "which *arr owns this"):
            return blocked
        managed = facts.is_managed
        if isinstance(managed, Known) and not managed.value:
            return GateResult(
                self.id,
                PROTECT,
                detail="no Sonarr or Radarr manages this file, so Reaper cannot remove it",
            )
        return GateResult(self.id, ABSTAIN, detail="Managed by Sonarr or Radarr.")


@dataclass(frozen=True, slots=True)
class OthersWatchingGate:
    """The requester ignored it, but other people did not."""

    config: GateConfig
    id: GateId = GateId.OTHERS_WATCHING

    def evaluate(self, facts: Facts) -> GateResult:
        if blocked := _blocked(self.id, facts.others_watching, "who else watched it"):
            return blocked
        others = facts.others_watching
        count = others.value if isinstance(others, Known) else 0
        floor = max(self.config.threshold, 1)

        if count >= floor:
            return GateResult(
                self.id,
                PROTECT,
                detail=(
                    f"{count} other {'person is' if count == 1 else 'people are'} watching it. "
                    "Removing it would punish them for someone else's request"
                ),
            )
        return GateResult(
            self.id,
            ABSTAIN,
            detail="Nobody besides the requester has watched it."
            if count == 0
            else f"Only {count} besides the requester has watched it.",
        )


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
    "OthersWatchingGate",
    "RatingFloorGate",
    "ServerPopularityGate",
    "StreamingNowGate",
    "UnmanagedGate",
    "WhitelistGate",
    "describe",
    "evaluate_all",
]
