# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signals -- weighted condemnation pressure.

Signals are **unsigned**. Each contributes between ``0`` and its weight, and zero
is the *floor*, not the middle. All protection lives in the gate lane; nothing here
can protect, and nothing here can be negative.

That sounds like a detail. It is the difference between a correct scorer and one
that deletes your favorite film during an outage.

## Why unsigned

The tempting design is a signed score: start at a neutral 50, add points for
"unwatched for ages", subtract points for "highly rated" and "globally popular".
It reads naturally and it is wrong. Under signed weights, an ``Unknown`` -- a Trakt
timeout, a stale ratings file -- removes a *negative* contribution, and the item's
score therefore **rises**. An outage makes media more condemned. A well-loved,
much-watched film flips from "spare" to "condemn" because a third-party API had a
bad minute.

Unsigned makes that unrepresentable:

* every signal measures a reason to delete, in ``[0, weight]``
* an ``Unknown`` contributes exactly ``0`` -- the minimum
* the denominator is fixed: it sums **all enabled weights**, including those whose
  value we could not read

So missing data can only ever push the score *down*, toward keeping the file. The
worst an outage can do is make Reaper more conservative.

## Coverage

Because the denominator includes unknown weights, an item we know little about
scores low automatically -- it is fail-safe by arithmetic rather than by a rule
somebody has to remember. ``coverage`` reports how much of the weight we actually
managed to evaluate, and a coverage floor abstains below it.

That gives a second, implicit floor, and it is load-bearing::

    base <= MAX_SCORE * coverage

Every unevaluated signal contributes pressure ``0`` while keeping its weight in the
denominator, so the score cannot exceed the share of evidence we could read. The
consequence is that **``condemn_at`` is itself a coverage floor**: an item cannot
reach a threshold of 70 without at least 70% of the policy's weight being readable,
whatever ``coverage_floor_bp`` says. Any change that lets a rule add points *outside*
the denominator deletes that floor silently. Pinned by
``tests/test_engine_invariants.TestLosingEvidenceCannotCondemn``.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from reaper.clock import humanize_days, humanize_window
from reaper.engine import fields
from reaper.engine.gates import Facts
from reaper.engine.observation import Absent, Known, Observation, Unknown

MAX_SCORE = 100


class SignalId(enum.StrEnum):
    """Reasons to believe nobody will watch this again.

    Note what is **not** here: size.

    ``SIZE`` remains available for owners who insist, but it is off by default and
    enabling it raises a ``danger`` warning on the policy page (``policy.inspect``, the
    same one a hand-written size rule raises). Backtested against real watch history, a
    scorer that weighted
    size produced a condemned set with *worse* regret than picking at random among
    films of the same age -- it scored below the baseline it was supposed to beat.

    The reason is obvious in hindsight: a very large file is a 4K feature. Big files
    are big *because* they are popular. Weighting size as a reason to delete aims the
    scorer at the most-watched content in the library.

    **Size measures how much you gain. It says nothing about whether anyone wants it.**
    So it ranks the candidates the score has already chosen -- "of the things it is
    safe to delete, do the big ones first" -- and never decides an item's fate. See
    docs/SIGNALS.md.
    """

    UNWATCHED = "unwatched"
    SEASON_RANK = "season_rank"
    FEW_WATCHERS = "few_watchers"
    LOW_RATING = "low_rating"

    SIZE = "size"
    """DEPRECATED as a condemnation signal. Reward, not risk. Off by default."""


@dataclass(frozen=True, slots=True)
class SignalConfig:
    """Integers only. See ``reaper.engine.policy`` for why."""

    signal: SignalId
    weight: int = 0
    """0 disables the signal entirely, removing it from the denominator as well as the
    numerator.

    Note what that does NOT mean. Turning a signal off *raises* every remaining score
    whenever the signal being removed was pulling its weight down: dropping weight ``w``
    carrying pressure ``p`` moves the score from ``100P/D`` to ``100(P-p)/(D-w)``, which
    rises whenever ``p/w < P/D``. The signals that satisfy that are exactly the ones
    arguing to keep -- a well-rated title, one people still watch -- so switching off an
    inconvenient protection is the single most effective way to condemn more, and it
    looks like simplification. The policy editor must never present it as free.
    """

    #: The value at which this signal reaches full pressure.
    saturate_at: int = 1

    #: Below this, the signal contributes nothing at all.
    floor: int = 0

    @property
    def enabled(self) -> bool:
        return self.weight > 0


@dataclass(frozen=True, slots=True)
class CustomSignalConfig:
    """A user-authored condemnation signal, translated from the policy for scoring.

    Two flavors, both unsigned and both in ``[0, weight]``:

    * **boolean** -- a matched ``condition`` contributes the full weight, an unmatched one
      zero; an ``Unknown`` input contributes zero with the weight retained.
    * **graded** -- a numeric ``field`` ramped between ``floor`` and ``saturate_at``,
      exactly like a built-in signal.

    Kept an engine-level type (not the policy spec) so ``score()`` never imports the policy
    layer, and translated from the policy at the call site like ``SignalConfig`` is.
    """

    name: str
    weight: int
    kind: Literal["boolean", "graded"]
    field: str
    condition: fields.Condition | None = None
    saturate_at: int = 1
    floor: int = 0

    @property
    def enabled(self) -> bool:
        return self.weight > 0


@dataclass(frozen=True, slots=True)
class KeepConfig:
    """A user-authored graded "lean toward keeping", translated from the policy.

    A strictly-subtractive discount applied AFTER the normalized score, in score points.
    Fail-closed on purpose: an ``Unknown`` input yields the MAXIMUM discount (toward
    keeping), and the discount can only ever lower a score -- never raise one, and never
    (by construction, see ``services.snapshot._verdict``) un-protect a gate-protected item.

    ``direction`` says which end of the ramp keeps: ``high_keeps`` (many all-time watchers
    -> keep) or ``low_keeps``.
    """

    name: str
    max_discount: int
    field: str
    floor: int
    saturate_at: int
    direction: Literal["high_keeps", "low_keeps"] = "high_keeps"

    @property
    def enabled(self) -> bool:
        return self.max_discount > 0


@dataclass(frozen=True, slots=True)
class KeepResult:
    name: str
    discount: float
    """Points subtracted from the score, in [0, max_discount]."""
    max_discount: int
    detail: str
    evaluated: bool
    """False when the input was Unknown -- and an Unknown keep takes its MAXIMUM discount,
    so missing data pushes toward keeping the file."""


class SignalState(enum.StrEnum):
    """What this row actually says, for a reader who only sees the number.

    Four states, because four genuinely different situations all end at zero pressure
    and are otherwise indistinguishable once the result is serialized:

    * ``ADDS`` -- it pushed toward removing (the only state with pressure above zero).
    * ``ARGUES_KEEP`` -- we read a real value and it argues for keeping: a rating above
      the floor, enough watchers, watched recently.
    * ``NOT_APPLICABLE`` -- it simply does not apply here. A yes/no rule that did not
      match, a field with nothing recorded, or a rule turned off. Not an argument for
      keeping: nothing about this item was found to be worth keeping.
    * ``UNREADABLE`` -- we could not look. The ONLY state that lowers coverage, and the
      one the UI must keep visually distinct: "we could not look" is not "we looked and
      it was fine".

    ``ARGUES_KEEP`` and ``NOT_APPLICABLE`` are kept apart deliberately. Folding them
    together would let an item with nothing in its favor read as if something argued
    for keeping it, which overstates the case for keeping in one direction and hides a
    thin evidence base in the other.
    """

    ADDS = "adds"
    ARGUES_KEEP = "argues_keep"
    NOT_APPLICABLE = "not_applicable"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class SignalResult:
    signal: SignalId | str
    """A built-in ``SignalId`` or a custom rule's name."""
    pressure: float
    """In [0, weight]. Never negative."""
    weight: int
    detail: str
    evaluated: bool
    """False when the input was Unknown. The weight still counts toward the
    denominator, so an unevaluated signal drags the score down, never up."""
    state: SignalState
    """Deliberately has no default: only the branch that produced this result knows
    whether a zero means "argues for keeping", "does not apply", or "could not look",
    and that knowledge is gone by the time anything downstream sees the result. A new
    construction site must choose."""

    floor: int | None = None
    """The ramp this result was scored against: no points below ``floor``, all of them at
    ``saturate_at``, straight line between.

    Carried so the panel can state the arithmetic without asking the policy, which by then
    may not be the one this item was scored under. That gap is not hypothetical -- a run's
    approval is bound to its policy hash and refuses to execute across an edit (rule 113) --
    so a panel printing today's line beside yesterday's score would be confidently wrong
    exactly where the operator is deciding what to delete.

    ``None`` where there is no ramp to state: a boolean custom rule matched or did not.
    Stored explanations frozen before these fields existed also thaw as ``None``, and the
    panel omits the sentence rather than inventing a line (rule 142's three-state)."""
    saturate_at: int | None = None


def _ramp(value: float, floor: float, saturate: float) -> float:
    """Linear 0 -> 1 between ``floor`` and ``saturate``, clamped at both ends."""
    if saturate <= floor:
        return 1.0 if value >= saturate else 0.0
    return max(0.0, min(1.0, (value - floor) / (saturate - floor)))


def _numeric(observation: Observation[float] | Observation[int]) -> float | None:
    return float(observation.value) if isinstance(observation, Known) else None


def evaluate_signal(config: SignalConfig, facts: Facts, *, window_days: int = 365) -> SignalResult:
    """One signal, carrying the ramp it was scored against. Always in ``[0, weight]``.

    The ramp is stamped here rather than at each of the seven returns below, because a
    branch that forgot it would produce a row whose line the panel silently cannot state,
    and "silently" is the whole problem: a missing line reads identically to a rule that
    never had one.

    ``window_days`` is the popularity window -- the span ``distinct_watchers`` was counted
    over. It phrases the "few watches" detail as "in the last <period>", and it moves
    numbers: FEW_WATCHERS is withheld entirely when the watch mirror does not reach back
    that far (``fields.reach_shortfall``, rule 140), taking zero pressure and leaving its
    weight in the denominator so coverage falls with it.

    **A caller that understates the window charges MORE than the evidence supports**, which
    is why every caller passes ``policy.popularity_window_days()`` and none may lean on the
    default above. A shorter window is easier for the mirror to cover, so the shortfall
    stops firing and the signal takes full pressure at full coverage on a count the true
    window could not establish. Measured on a 180-day mirror against a count taken over the
    real 365-day window: 0.00/20 at coverage 0.00 when passed 365, and 20.00/20 at coverage
    1.00 when passed 180 or 90. ``score()``'s docstring names the same hazard, and
    ``backtest.run`` was a live instance of it.

    The span is named in the withheld arm too, where the shortfall clause that follows it
    says why it could not be checked.
    """
    return replace(
        _branch_signal(config, facts, window_days=window_days),
        floor=config.floor,
        saturate_at=config.saturate_at,
    )


def _branch_signal(config: SignalConfig, facts: Facts, *, window_days: int) -> SignalResult:
    """``evaluate_signal`` without the ramp stamp. Every return here leaves it unset."""
    if not config.enabled:
        return SignalResult(
            config.signal, 0.0, 0, "disabled", evaluated=True, state=SignalState.NOT_APPLICABLE
        )

    raw: float | None
    detail: str
    # The observation the branch read, so the shared tail below can tell "we looked and
    # there is genuinely nothing" (Absent) from "we could not look" (Unknown). Only the
    # branch knows which fact it read, and that knowledge is gone by the tail.
    observation: Observation[float] | Observation[int]

    match config.signal:
        case SignalId.UNWATCHED:
            observation = facts.days_observed_unwatched
            raw = _numeric(observation)
            detail = (
                f"not watched in {humanize_days(raw)}"
                if raw is not None
                else "no watch history to say how long it has gone unwatched"
            )
        case SignalId.SIZE:
            observation = facts.size_bytes
            size = _numeric(observation)
            raw = size / 1_000_000_000 if size is not None else None
            if raw is not None:
                detail = f"{raw:.1f} GB on disk"
            elif isinstance(observation, Absent):
                detail = "no file size recorded"
            else:
                detail = "could not read the file size"
        case SignalId.SEASON_RANK:
            # A special (season 0) is deliberately left out of the newest->oldest ranking,
            # so its rank is Absent: we looked, and it genuinely has no rank slot. That is
            # NOT_APPLICABLE, exactly as evaluate_custom reads an Absent field -- evaluated,
            # zero pressure, weight kept, coverage intact -- and must NOT read as UNREADABLE,
            # which would tell the owner "could not tell which season this is" and drag the
            # special's coverage down for a rank it was never meant to have. A genuine Sonarr
            # read failure is Unknown and still falls through to the UNREADABLE branch below.
            if isinstance(facts.season_rank, Absent):
                return SignalResult(
                    config.signal,
                    0.0,
                    config.weight,
                    "not one of the numbered seasons",
                    evaluated=True,
                    state=SignalState.NOT_APPLICABLE,
                )
            observation = facts.season_rank
            raw = _numeric(observation)
            # Rank 1 is the NEWEST season with files, not the oldest. Calling it an
            # older season while charging it deletion pressure told the owner the
            # opposite of what the ranking means.
            detail = (
                f"the {fields.describe_season_rank(raw)} on disk"
                if raw is not None
                else "could not tell which season this is"
            )
        case SignalId.FEW_WATCHERS:
            observation = facts.distinct_watchers
            watchers = _numeric(observation)
            # Inverted, but still unsigned: FEWER watchers means MORE pressure.
            # The pressure is computed from the shortfall, never as a negative.
            raw = max(0.0, float(config.saturate_at) - watchers) if watchers is not None else None
            # The count is only an answer while the mirror spans the window. Under that it
            # is a LOWER BOUND, and the pressure this signal charges comes from the
            # shortfall below ``saturate_at`` -- so a truncated count charges pressure the
            # evidence does not support, and lands it at full confidence: ``score()``
            # counts the signal evaluated because its input was Known, keeping its weight
            # in the numerator AND reporting coverage 1.0, so ``coverage_floor_bp`` cannot
            # see the hole. Measured at 20.00/20 pressure with coverage_bp 10000 on a
            # 90-day mirror against the 365-day window, where the same title takes 0.00/20
            # once the mirror reaches the identical plays.
            #
            # So withhold it rather than narrow it: route to the UNREADABLE arm below (raw
            # None on a non-Absent observation), which is the treatment Unknown already
            # gets -- zero pressure, weight retained in the denominator, coverage honestly
            # discounted (rules 31, 140). The gate refuses to answer here too
            # (``gates.ServerPopularityGate``), off the same shared derivation.
            #
            # Not asked of an UNKNOWN input, which has its own cause -- a season Plex never
            # matched is the common one. Answering that with the mirror's depth points the
            # operator at their history retention while the real problem is a Plex match.
            # Both twins skip it for Unknown the same way (``ServerPopularityGate``'s
            # ``_blocked``, ``fields.evaluate``'s Unknown arm), and for Unknown the numbers
            # really are identical either way -- zero pressure, weight retained, coverage
            # discounted -- so only the sentence changes.
            #
            # ABSENT is deliberately NOT exempted, and the distinction is the whole point.
            # "We looked and there is genuinely nothing" is a claim about the window, and a
            # mirror that does not span the window cannot support it: it establishes only
            # that nothing happened in the part it holds. So rule 93's precondition (a
            # GENUINE absence) is not met, rule 140 governs as the more specific, and the
            # count stays on the reach check. Exempting it would move this signal to the
            # fail-open side of its own twin -- ``_blocked`` matches Unknown only, so an
            # Absent count falls through to the gate's shortfall arm and blocks -- while
            # reporting coverage 1.0, which is exactly the hole the branch above describes.
            span = f"in the last {humanize_window(window_days)}"
            short = (
                None
                if isinstance(observation, Unknown)
                else fields.reach_shortfall(fields.RECENT_WATCHERS, facts, window_days=window_days)
            )
            if short is not None:
                return SignalResult(
                    config.signal,
                    0.0,
                    config.weight,
                    f"could not tell who watched it {span}: {short}",
                    evaluated=False,
                    state=SignalState.UNREADABLE,
                )
            if watchers is None and isinstance(observation, Absent):
                detail = "no watch history recorded for it"
            elif watchers is None:
                detail = "could not tell who watched it"
            elif watchers == 0:
                detail = f"nobody watched it {span}"
            else:
                people = "person" if watchers == 1 else "people"
                detail = f"only {watchers:.0f} {people} watched it {span}"
        case SignalId.LOW_RATING:
            observation = facts.imdb_rating_tenths
            rating = _numeric(observation)
            # Same shape: a LOW rating is pressure to delete. Expressed as the
            # shortfall below saturate_at, so it stays in [0, weight].
            raw = max(0.0, float(config.saturate_at) - rating) if rating is not None else None
            if rating is not None:
                detail = f"IMDb {rating / 10:.1f}"
            elif isinstance(observation, Absent):
                detail = "no IMDb rating"
            else:
                detail = "could not read the IMDb rating"

    if raw is None and isinstance(observation, Absent):
        # We looked, and there genuinely is no value: an unrated show, a season with no
        # single release date. Real evidence, not a read failure -- so EVALUATED, with the
        # weight retained and coverage intact, exactly as SEASON_RANK above and
        # evaluate_custom's graded arm read an Absent. Reading it as UNREADABLE told the
        # owner "could not read the IMDb rating" about a title that simply has none, and
        # dragged every unrated show's coverage toward the abstain floor.
        return SignalResult(
            config.signal,
            0.0,
            config.weight,
            detail,
            evaluated=True,
            state=SignalState.NOT_APPLICABLE,
        )

    if raw is None:
        # Unknown contributes ZERO pressure -- the floor. Its weight still counts
        # in the denominator, so an outage can only make the score lower.
        return SignalResult(
            config.signal, 0.0, config.weight, detail, evaluated=False, state=SignalState.UNREADABLE
        )

    fraction = _ramp(raw, float(config.floor), float(config.saturate_at))
    return SignalResult(
        signal=config.signal,
        pressure=fraction * config.weight,
        weight=config.weight,
        detail=detail,
        evaluated=True,
        # A built-in signal always reads a real measurement here, so a zero means the
        # value sits below the floor -- watched recently, rated well, watched by enough
        # people. That is an argument for keeping, not an inapplicable rule.
        state=SignalState.ADDS if fraction > 0 else SignalState.ARGUES_KEEP,
    )


def evaluate_custom(
    config: CustomSignalConfig, facts: Facts, *, window_days: int | None = None
) -> SignalResult:
    """One user-authored condemnation signal. Always in ``[0, weight]``.

    Unsigned like every signal: a matched boolean rule (or a graded field above its floor)
    adds pressure; an ``Unknown`` input adds none but keeps its weight in the denominator,
    so a custom rule can only ever push the score DOWN on missing data.

    ``window_days`` is the policy's popularity window, needed because a rule may be
    authored on a watcher count, whose value the watch mirror only supports so far back
    (``fields.reach_shortfall``).

    A GRADED rule carries its ramp out the same way a built-in does. A boolean one carries
    none, and that is the distinction the field exists to keep: it did not land part-way, so
    there is no line to state and none is invented.
    """
    graded = config.kind == "graded"
    return replace(
        _branch_custom(config, facts, window_days=window_days),
        floor=config.floor if graded else None,
        saturate_at=config.saturate_at if graded else None,
    )


def _branch_custom(
    config: CustomSignalConfig, facts: Facts, *, window_days: int | None
) -> SignalResult:
    """``evaluate_custom`` without the ramp stamp. Every return here leaves it unset."""
    if not config.enabled:
        return SignalResult(
            config.name, 0.0, 0, "disabled", evaluated=True, state=SignalState.NOT_APPLICABLE
        )

    spec = fields.BY_KEY.get(config.field)
    label = spec.label if spec is not None else config.field

    if config.kind == "graded":
        observation = spec.read(facts) if spec is not None else None
        if isinstance(observation, Absent):
            # Absent is real evidence ("none recorded"), exactly as the boolean path
            # reads it: zero pressure, weight retained, and EVALUATED. It must not drag
            # coverage under the floor the way an unreadable (Unknown) input rightly
            # does, or a graded rule on a field one media type never carries would
            # silently push every one of its items below the coverage floor.
            return SignalResult(
                config.name,
                0.0,
                config.weight,
                f"{label}: none recorded",
                evaluated=True,
                # We looked and there is genuinely nothing recorded. Nothing about the
                # item argues for keeping it; the rule just has nothing to work with.
                state=SignalState.NOT_APPLICABLE,
            )
        raw = (
            float(observation.value)
            if isinstance(observation, Known) and isinstance(observation.value, int | float)
            else None
        )
        if raw is None:
            # Unknown (we could not look): zero pressure, weight retained, and NOT
            # evaluated -- coverage honestly reflects the unchecked input.
            return SignalResult(
                config.name,
                0.0,
                config.weight,
                f"could not read {label.lower()}",
                evaluated=False,
                state=SignalState.UNREADABLE,
            )
        # A Known watcher count the mirror does not reach far enough to support is a lower
        # bound, so it is reported unchecked rather than scored (rule 140). Deliberately
        # more conservative than the direction alone requires, and worth saying which way
        # it runs: ``_ramp`` is monotone INCREASING in the count and ``GradedCondemnSpec``
        # has no direction knob, so a truncated count lands at or below the true fraction
        # and this rule can only ever UNDER-charge. That is the safe direction already --
        # unlike the built-in FEW_WATCHERS branch above, whose inverted
        # ``saturate_at - watchers`` turns the same truncation into over-charging.
        #
        # The reason to withhold anyway is that the graded arm has no "already earned"
        # outcome to preserve. ``fields._survives_more_history`` can let a comparison stand
        # because more history cannot overturn it; a ramp has no such threshold, so every
        # value it produces is honest-but-incomplete. Same treatment as the Unknown above,
        # which is what keeps coverage honest about it.
        # (``watchers_all_time`` cannot reach here at all -- protect-only, refused by
        # ``GradedCondemnSpec._valid_graded`` -- so ``recent_watchers`` is the only field
        # this arm ever sees.)
        if (short := fields.reach_shortfall(spec, facts, window_days=window_days)) is not None:
            return SignalResult(
                config.name,
                0.0,
                config.weight,
                f"could not check {label.lower()}: {short}",
                evaluated=False,
                state=SignalState.UNREADABLE,
            )
        fraction = _ramp(raw, float(config.floor), float(config.saturate_at))
        return SignalResult(
            config.name,
            fraction * config.weight,
            config.weight,
            f"{label}: {raw:.0f}",
            evaluated=True,
            # A real number below the ramp's floor: measured, and it argues for keeping.
            state=SignalState.ADDS if fraction > 0 else SignalState.ARGUES_KEEP,
        )

    # boolean
    if config.condition is None:  # pragma: no cover -- the policy always sets one
        return SignalResult(
            config.name,
            0.0,
            config.weight,
            "misconfigured rule",
            evaluated=False,
            state=SignalState.UNREADABLE,
        )
    result = fields.evaluate(config.condition, facts, window_days=window_days)
    if result.blocked:
        # Unknown input: could not check, so it cannot add pressure. Weight retained.
        return SignalResult(
            config.name,
            0.0,
            config.weight,
            result.detail,
            evaluated=False,
            state=SignalState.UNREADABLE,
        )
    pressure = float(config.weight) if result.matched else 0.0
    return SignalResult(
        config.name,
        pressure,
        config.weight,
        result.detail,
        evaluated=True,
        # A yes/no rule that did not match does not argue for keeping. It just does not
        # describe this item, and its detail is phrased as the plain fact.
        state=SignalState.ADDS if result.matched else SignalState.NOT_APPLICABLE,
    )


def evaluate_keep(
    config: KeepConfig, facts: Facts, *, window_days: int | None = None
) -> KeepResult:
    """One user-authored graded keep. A discount in ``[0, max_discount]``, fail-closed.

    A value we cannot read yields the FULL discount -- missing data pushes toward keeping.
    An ``Absent`` value (we looked, there genuinely is none) yields no discount: absence is
    real evidence, not a reason to keep.

    A value the watch mirror does not reach far enough to support reads as the first of
    those, not the second (``window_days``, ``fields.reach_shortfall``).
    """
    if not config.enabled:
        return KeepResult(config.name, 0.0, 0, "disabled", evaluated=True)

    spec = fields.BY_KEY.get(config.field)
    label = spec.label if spec is not None else config.field
    observation = spec.read(facts) if spec is not None else None
    raw = (
        float(observation.value)
        if isinstance(observation, Known) and isinstance(observation.value, int | float)
        else None
    )
    if raw is None:
        if isinstance(observation, Absent):
            return KeepResult(
                config.name, 0.0, config.max_discount, f"{label}: none recorded", True
            )
        # Unknown (or an unreadable field): fail-closed to the maximum keep.
        return KeepResult(
            config.name,
            float(config.max_discount),
            config.max_discount,
            f"kept fully: could not check {label.lower()}",
            evaluated=False,
        )

    if (short := fields.reach_shortfall(spec, facts, window_days=window_days)) is not None:
        # A Known watcher count from a mirror that does not reach far enough is a lower
        # bound. On the default ``high_keeps`` direction the ramp is monotone in it, so the
        # discount comes out too SMALL -- the keep quietly shrinks on evidence nobody has,
        # which is the one direction that loses a file. (``low_keeps`` errs the generous
        # way, but both take the same arm: the honest report is that it was not checked,
        # and a keep is never the place to split that hair.) The maximum discount is the
        # same fail-closed answer an Unknown gets above.
        return KeepResult(
            config.name,
            float(config.max_discount),
            config.max_discount,
            f"kept fully: could not check {label.lower()}: {short}",
            evaluated=False,
        )

    fraction = _ramp(raw, float(config.floor), float(config.saturate_at))
    if config.direction == "low_keeps":
        fraction = 1.0 - fraction
    return KeepResult(
        config.name,
        fraction * config.max_discount,
        config.max_discount,
        f"{label}: {raw:.0f}",
        True,
    )


@dataclass(frozen=True, slots=True)
class Score:
    value: float
    """0-100. Higher means more pressure to delete. This is ``base_value`` minus the keep
    discount, floored at 0 -- the number the verdict and the simulator decide on."""

    coverage: float
    """The share of enabled weight we could actually evaluate, 0-1.

    An item at 40% coverage is one we mostly cannot see. The policy's coverage
    floor abstains below a threshold rather than guessing. Keeps are NOT in coverage:
    coverage measures how much *condemnation evidence* we saw, and a keep is orthogonal.
    """

    results: Sequence[SignalResult]

    base_value: float = 0.0
    """The condemnation score before any keep discount -- what the why-panel shows as the
    'condemnation' subtotal."""

    keep_discount: float = 0.0
    """Points the graded keeps subtracted from ``base_value`` to reach ``value``."""

    keep_results: Sequence[KeepResult] = ()

    @property
    def unevaluated(self) -> list[SignalResult]:
        return [r for r in self.results if not r.evaluated and r.weight > 0]


def score(
    configs: Sequence[SignalConfig],
    facts: Facts,
    *,
    custom_condemn: Sequence[CustomSignalConfig] = (),
    keeps: Sequence[KeepConfig] = (),
    window_days: int = 365,
) -> Score:
    """Total condemnation pressure, 0-100.

    The denominator is the sum of **all enabled weights**, including those whose
    value could not be read. That is what makes missing data fail safe: the numerator
    loses the contribution, the denominator keeps it, so the score falls.

    User-authored ``custom_condemn`` signals join this same denominator, so they inherit
    the fail-safe arithmetic for free: an Unknown custom rule drags the score down exactly
    as a built-in one does, and there is no separate additive layer to reason about.

    ``keeps`` are a strictly-subtractive discount applied AFTER normalization:
    ``value = max(0, base - Σdiscount)``. Because every discount is non-negative and an
    Unknown keep takes its maximum, more missing data can only lower the score -- a keep can
    never raise a score, and never un-protect a gate-protected item (that is decided before
    the score is ever read; see ``services.snapshot._verdict``).

    ``window_days`` is the policy's popularity window -- the span ``distinct_watchers`` was
    counted over. It phrases the "few watches" detail, and it is the span every reader of a
    watcher count checks the mirror's reach against before treating that count as an answer
    (``fields.reach_shortfall``, rule 140). So it does move numbers, in one direction only:
    a window the history does not cover withdraws pressure and lowers coverage, never the
    reverse. Pass the policy's own value; the default here is the shipped window and agrees
    with ``policy.PolicyBody.popularity_window_days``'s fallback, but a policy that has
    changed it and a caller that omits it would disagree in the fail-open direction.
    """
    results = [evaluate_signal(config, facts, window_days=window_days) for config in configs]
    results += [
        evaluate_custom(config, facts, window_days=window_days) for config in custom_condemn
    ]

    keep_results = [evaluate_keep(keep, facts, window_days=window_days) for keep in keeps]
    keep_discount = sum(kr.discount for kr in keep_results)

    denominator = sum(r.weight for r in results)
    if denominator == 0:
        base = 0.0
        coverage = 1.0
    else:
        base = MAX_SCORE * sum(r.pressure for r in results) / denominator
        coverage = sum(r.weight for r in results if r.evaluated) / denominator

    return Score(
        value=max(0.0, base - keep_discount),
        coverage=coverage,
        results=results,
        base_value=base,
        keep_discount=keep_discount,
        keep_results=keep_results,
    )
