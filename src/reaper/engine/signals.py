# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signals -- weighted condemnation pressure.

Signals are **unsigned**. Each contributes between ``0`` and its weight, and zero
is the *floor*, not the middle. All protection lives in the gate lane; nothing here
can protect, and nothing here can be negative.

That sounds like a detail. It is the difference between a correct scorer and one
that deletes your favourite film during an outage.

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
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass

from reaper.clock import humanize_days, humanize_window
from reaper.engine.gates import Facts
from reaper.engine.observation import Known, Observation

MAX_SCORE = 100


class SignalId(enum.StrEnum):
    """Reasons to believe nobody will watch this again.

    Note what is **not** here: size.

    ``SIZE`` remains available for owners who insist, but it is off by default and the
    UI warns about it. Backtested against real watch history, a scorer that weighted
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
    """0 disables the signal entirely -- and, crucially, removes it from the
    denominator too, so disabling a signal does not silently inflate every score."""

    #: The value at which this signal reaches full pressure.
    saturate_at: int = 1

    #: Below this, the signal contributes nothing at all.
    floor: int = 0

    @property
    def enabled(self) -> bool:
        return self.weight > 0


@dataclass(frozen=True, slots=True)
class SignalResult:
    signal: SignalId
    pressure: float
    """In [0, weight]. Never negative."""
    weight: int
    detail: str
    evaluated: bool
    """False when the input was Unknown. The weight still counts toward the
    denominator, so an unevaluated signal drags the score down, never up."""


def _ramp(value: float, floor: float, saturate: float) -> float:
    """Linear 0 -> 1 between ``floor`` and ``saturate``, clamped at both ends."""
    if saturate <= floor:
        return 1.0 if value >= saturate else 0.0
    return max(0.0, min(1.0, (value - floor) / (saturate - floor)))


def _numeric(observation: Observation[float] | Observation[int]) -> float | None:
    return float(observation.value) if isinstance(observation, Known) else None


def evaluate_signal(config: SignalConfig, facts: Facts, *, window_days: int = 365) -> SignalResult:
    """One signal. Always in ``[0, weight]``.

    ``window_days`` is the popularity window, used only to phrase the "few watches" detail
    as "in the last <period>"; it never changes a number, only how one reads.
    """
    if not config.enabled:
        return SignalResult(config.signal, 0.0, 0, "disabled", evaluated=True)

    raw: float | None
    detail: str

    match config.signal:
        case SignalId.UNWATCHED:
            raw = _numeric(facts.days_observed_unwatched)
            detail = (
                f"not watched in {humanize_days(raw)}"
                if raw is not None
                else "no watch history to say how long it has gone unwatched"
            )
        case SignalId.SIZE:
            size = _numeric(facts.size_bytes)
            raw = size / 1_000_000_000 if size is not None else None
            detail = (
                f"{raw:.1f} GB on disk"
                if raw is not None
                else "could not read the file size"
            )
        case SignalId.SEASON_RANK:
            raw = _numeric(facts.season_rank)
            detail = (
                f"an older season — number {raw:.0f} counting back from the newest"
                if raw is not None
                else "could not tell which season this is"
            )
        case SignalId.FEW_WATCHERS:
            watchers = _numeric(facts.distinct_watchers)
            # Inverted, but still unsigned: FEWER watchers means MORE pressure.
            # The pressure is computed from the shortfall, never as a negative.
            raw = max(0.0, float(config.saturate_at) - watchers) if watchers is not None else None
            if watchers is None:
                detail = "could not tell who watched it"
            elif watchers == 0:
                detail = f"nobody watched it in the last {humanize_window(window_days)}"
            else:
                people = "person" if watchers == 1 else "people"
                window_text = humanize_window(window_days)
                detail = f"only {watchers:.0f} {people} watched it in the last {window_text}"
        case SignalId.LOW_RATING:
            rating = _numeric(facts.imdb_rating_tenths)
            # Same shape: a LOW rating is pressure to delete. Expressed as the
            # shortfall below saturate_at, so it stays in [0, weight].
            raw = max(0.0, float(config.saturate_at) - rating) if rating is not None else None
            detail = f"IMDb {rating / 10:.1f}" if rating is not None else "no IMDb rating"

    if raw is None:
        # Unknown contributes ZERO pressure -- the floor. Its weight still counts
        # in the denominator, so an outage can only make the score lower.
        return SignalResult(config.signal, 0.0, config.weight, detail, evaluated=False)

    fraction = _ramp(raw, float(config.floor), float(config.saturate_at))
    return SignalResult(
        signal=config.signal,
        pressure=fraction * config.weight,
        weight=config.weight,
        detail=detail,
        evaluated=True,
    )


@dataclass(frozen=True, slots=True)
class Score:
    value: float
    """0-100. Higher means more pressure to delete."""

    coverage: float
    """The share of enabled weight we could actually evaluate, 0-1.

    An item at 40% coverage is one we mostly cannot see. The policy's coverage
    floor abstains below a threshold rather than guessing.
    """

    results: Sequence[SignalResult]

    @property
    def unevaluated(self) -> list[SignalResult]:
        return [r for r in self.results if not r.evaluated and r.weight > 0]


def score(configs: Sequence[SignalConfig], facts: Facts, *, window_days: int = 365) -> Score:
    """Total condemnation pressure, 0-100.

    The denominator is the sum of **all enabled weights**, including those whose
    value could not be read. That is what makes missing data fail safe: the numerator
    loses the contribution, the denominator keeps it, so the score falls.

    ``window_days`` is passed through purely for phrasing the "few watches" detail; it is
    never part of any number.
    """
    results = [evaluate_signal(config, facts, window_days=window_days) for config in configs]

    denominator = sum(r.weight for r in results)
    if denominator == 0:
        return Score(value=0.0, coverage=1.0, results=results)

    numerator = sum(r.pressure for r in results)
    evaluated_weight = sum(r.weight for r in results if r.evaluated)

    return Score(
        value=MAX_SCORE * numerator / denominator,
        coverage=evaluated_weight / denominator,
        results=results,
    )
