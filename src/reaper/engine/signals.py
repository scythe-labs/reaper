# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signals: reasons to believe nobody will watch this title again.

Each signal contributes a number between 0 and its own weight, and 0 is the floor:
the lowest a signal can push the score. No signal can protect a title, and no signal
can lower the score. All protection lives in the gates.

## Why unsigned

A signed score would start at a neutral point, say 50, add points for "unwatched
for ages," and subtract points for "highly rated" or "globally popular." That
design is wrong: when a value cannot be read (a timed-out API call, a stale
ratings file), it would remove a negative contribution, and the item's score
would rise. A service outage would make a well-loved, much-watched film look
more deletable.

The unsigned design rules that out:

* every signal measures a reason to delete, between 0 and its weight
* a value that cannot be read contributes exactly 0, the minimum
* the denominator is fixed: it sums every enabled weight, including the ones
  whose value could not be read

So missing data can only push the score down, toward keeping the file. An outage
can only make Reaper more cautious.

## Coverage

Because the denominator counts weights whose value could not be read, an item
Reaper knows little about scores low automatically. ``coverage`` reports the
share of the total weight Reaper actually managed to check, and the policy's
coverage floor holds an item back below that share.

This also caps the score itself: ``base <= MAX_SCORE * coverage``. Every unchecked
signal adds 0 while still counting its weight in the denominator, so the score can
never exceed the share of the evidence Reaper could read. In practice this means
the condemn threshold doubles as a coverage floor: an item cannot reach a threshold
of 70 unless at least 70% of the policy's weight was readable, whatever the
operator's coverage floor says. A change that let a rule add points outside the
denominator would silently remove this floor.
``tests/test_engine_invariants.TestLosingEvidenceCannotCondemn`` checks it holds.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal, assert_never

from reaper.engine import fields
from reaper.engine.gates import Facts, blocked_reason
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.engine.reason import Reason
from reaper.text import fold

MAX_SCORE = 100


class SignalId(enum.StrEnum):
    """Reasons to believe nobody will watch this title again.

    Size is deliberately not one of them. ``SIZE`` still exists for an owner who
    wants it, off by default, and turning it on raises a danger warning on the
    policy page (``policy_warnings.inspect``). Tested against real viewing history,
    a scorer that weighted size did worse than picking titles at random among
    films of the same age.

    The reason: a very large file is usually a 4K feature, and big files are big
    because they are popular. Weighting size as a reason to delete aims the scorer
    at the most-watched content in the library. Size measures how much space
    deleting a file frees. That has nothing to do with whether anyone wants the
    file, so it can only rank titles the score already picked for removal. It
    never decides a title's fate on its own. See docs/SIGNALS.md.
    """

    UNWATCHED = "unwatched"
    SEASON_RANK = "season_rank"
    FEW_WATCHERS = "few_watchers"
    LOW_RATING = "low_rating"

    SIZE = "size"
    """Not used to decide removal. It measures how much space deleting it would free,
    unrelated to whether the title is likely to be watched again, and stays off by default."""


@dataclass(frozen=True, slots=True)
class SignalConfig:
    """Integers only. See ``reaper.engine.policy`` for why."""

    signal: SignalId
    weight: int = 0
    """0 disables the signal and removes it from both the score and the denominator it is
    scored against.

    Turning a signal off can RAISE every other item's score. Dropping a weight ``w`` that
    was carrying deletion pressure ``p`` moves the score from ``100P/D`` to
    ``100(P-p)/(D-w)``, and that rises whenever ``p/w`` is smaller than the average
    pressure per weight. That is exactly true of a signal that was arguing to keep a
    title, such as a good rating or recent viewers. So turning off an inconvenient signal
    is one of the most effective ways to raise every other score, and it looks like
    simplification. The policy editor must never present it as free.
    """

    #: The value at which this signal reaches its full weight.
    saturate_at: int = 1

    #: Below this value, the signal adds nothing.
    floor: int = 0

    @property
    def enabled(self) -> bool:
        return self.weight > 0


@dataclass(frozen=True, slots=True)
class CustomSignalConfig:
    """A user-authored reason to remove a title, translated from the policy for scoring.

    Two kinds, both between 0 and ``weight``:

    * boolean: a matched ``condition`` adds the full weight, an unmatched one adds
      nothing, and a value that cannot be read adds nothing but keeps its weight.
    * graded: a numeric ``field`` ramps between ``floor`` and ``saturate_at``, the same
      way a built-in signal does.

    This type lives in the engine, separate from the policy's own type, so ``score()`` never
    has to import the policy module. The caller translates a policy's rules into this shape, the
    same way it does for ``SignalConfig``.
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
    """A user-authored reason to lean toward keeping a title, translated from the policy.

    A discount in score points, subtracted after the score is computed. A value that
    cannot be read takes the maximum discount, so missing data always favors keeping. The
    discount can only lower a score, and it can never un-protect a title a
    gate already protected (``services.snapshot._verdict`` decides protection first).

    ``direction`` says which end of the ramp keeps the title: ``high_keeps`` (a high value,
    such as many all-time watchers, keeps it) or ``low_keeps``.
    """

    name: str
    max_discount: int
    field: str
    floor: int
    saturate_at: int
    direction: Literal["high_keeps", "low_keeps"] = "high_keeps"
    value: str | None = None
    """For a membership field (``on_list``): the list's name. This keep is binary: being
    on the list takes the full discount, being off it takes none, and a membership that
    could not be read takes the full discount like every other keep. ``None`` means the
    field ramps instead."""

    min_viewings: int | None = None
    """One of the built-in rewatch keep's two bars (``REWATCH_KEEP``). Carried on the config
    so the condition is decided against frozen facts, which lets a bar edit replay exactly
    against a past scan. ``None`` on every user-authored keep; only
    ``policy.PolicyBody.keep_configs`` sets it."""
    recent_days: int | None = None

    media_type: Literal["movie", "tv"] = "movie"
    """Which wording the rewatch detail uses. A movie's ``rewatch_viewings`` counts
    qualified viewings ("Watched 12 times"); a show's counts whole re-watches ("Watched
    again 3 times", ``services.rewatch.replay_period_count``). Only the ``REWATCH_KEEP``
    detail helpers read this field; a user-authored keep leaves it at the default."""

    @property
    def enabled(self) -> bool:
        return self.max_discount > 0


@dataclass(frozen=True, slots=True)
class KeepResult:
    name: str
    discount: float
    """Points subtracted from the score, between 0 and max_discount."""
    max_discount: int
    detail: Reason
    evaluated: bool
    """False when the value could not be read. A keep that could not be read takes its
    maximum discount, so missing data always favors keeping the file."""


class SignalState(enum.StrEnum):
    """What a signal's result actually means, for a reader who sees only a number.

    Four situations all produce zero pressure toward deletion and would look identical
    without this:

    * ``ADDS``: the signal pushed toward removing the title. The only state with a value
      above zero.
    * ``ARGUES_KEEP``: Reaper read a real value and it argues for keeping the title, such
      as a rating above the floor, enough watchers, or a recent watch.
    * ``NOT_APPLICABLE``: the signal does not apply here. A yes/no rule that did not
      match, a field with nothing recorded, or a rule that is turned off. It just means
      nothing was found either way.
    * ``UNREADABLE``: Reaper could not check. This is the only state that lowers
      coverage, and the UI must show it differently from a checked, healthy value.

    ``ARGUES_KEEP`` and ``NOT_APPLICABLE`` are kept separate on purpose. Merging them
    would make a title with nothing in its favor look like something argued for keeping
    it, which overstates the case for keeping and hides how thin the evidence actually is.
    """

    ADDS = "adds"
    ARGUES_KEEP = "argues_keep"
    NOT_APPLICABLE = "not_applicable"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class SignalResult:
    signal: SignalId | str
    """A built-in ``SignalId``, or a custom rule's name."""
    pressure: float
    """Between 0 and weight."""
    weight: int
    detail: Reason
    evaluated: bool
    """False when the value could not be read. The weight still counts toward the
    denominator, so an unevaluated signal can only lower the score."""
    state: SignalState
    """Has no default on purpose. Only the branch that produced this result knows whether
    a zero means "argues for keeping," "does not apply," or "could not check," and that
    distinction is lost once the result is read elsewhere. Every new result must state it."""

    floor: int | None = None
    """The ramp this result was scored against: no points below ``floor``, the full weight
    at ``saturate_at``, and a straight line between.

    Carried here so the why-panel can show the arithmetic without reading the current
    policy, which may have changed since this item was scored. The panel must describe the
    policy that produced this result, never today's policy: a run's approval is bound to the
    policy it was scored under and cannot execute against an edited one.

    ``None`` when there is no ramp to describe, such as a boolean custom rule that simply
    matched or did not. A result stored before these fields existed also reads as ``None``,
    and the panel must never show an invented number: it leaves out that sentence instead."""
    saturate_at: int | None = None


def _ramp(value: float, floor: float, saturate: float) -> float:
    """Linear 0 -> 1 between ``floor`` and ``saturate``, clamped at both ends."""
    if saturate <= floor:
        return 1.0 if value >= saturate else 0.0
    return max(0.0, min(1.0, (value - floor) / (saturate - floor)))


def _numeric(observation: Observation[float] | Observation[int]) -> float | None:
    return float(observation.value) if isinstance(observation, Known) else None


def evaluate_signal(config: SignalConfig, facts: Facts, *, window_days: int = 365) -> SignalResult:
    """Score one built-in signal, always between 0 and its weight.

    Every return path stamps the ramp (``floor`` and ``saturate_at``) onto the result, so
    the why-panel can always describe the arithmetic behind a row.

    ``window_days`` is the span ``distinct_watchers`` was counted over. It shapes the "few
    watchers" detail text and it changes the score: FEW_WATCHERS is withheld (zero
    pressure, weight kept in the denominator, coverage lowered) when the watch mirror does
    not reach back that far (``fields.reach_shortfall``).

    Pass the policy's real window, from ``policy.popularity_window_days()``, on every scan.
    A shorter window is easier for the mirror to cover, so passing one understates how far
    back the mirror needs to reach and lets the signal take full pressure on a count the
    real window could not support. For example, a 180-day mirror checked against a real
    365-day window should score 0 at 0% coverage, but scores the full weight at 100%
    coverage if called with 90 or 180 instead of 365.

    ``engine.preview.probe_signal`` is the one caller allowed to use a different window: it
    tests a hypothetical rule, so it builds a mirror wide enough that
    the shortfall can never fire.
    """
    return replace(
        _branch_signal(config, facts, window_days=window_days),
        floor=config.floor,
        saturate_at=config.saturate_at,
    )


def _branch_signal(config: SignalConfig, facts: Facts, *, window_days: int) -> SignalResult:
    """The work behind ``evaluate_signal``, before the ramp is stamped on. Every return
    here leaves ``floor`` and ``saturate_at`` unset."""
    if not config.enabled:
        return SignalResult(
            config.signal,
            0.0,
            0,
            Reason("disabled"),
            evaluated=True,
            state=SignalState.NOT_APPLICABLE,
        )

    raw: float | None
    detail: Reason
    # Each branch stores the fact it read here, so the shared code at the end of this
    # function can tell "checked, and there is genuinely nothing" (Absent) from "could not
    # check" (Unknown) without knowing which fact it was.
    observation: Observation[float] | Observation[int]

    match config.signal:
        case SignalId.UNWATCHED:
            observation = facts.days_observed_unwatched
            raw = _numeric(observation)
            detail = (
                Reason("signal_unwatched", {"days": raw})
                if raw is not None
                else Reason("signal_unwatched_no_history")
            )
        case SignalId.SIZE:
            observation = facts.size_bytes
            size = _numeric(observation)
            raw = size / 1_000_000_000 if size is not None else None
            if raw is not None:
                detail = Reason("signal_size", {"gb": round(raw, 1)})
            elif isinstance(observation, Absent):
                detail = Reason("signal_size_none")
            else:
                detail = Reason("signal_size_unreadable")
        case SignalId.SEASON_RANK:
            # A special (season 0) has no rank slot in the newest-to-oldest ranking, so its
            # rank reads as Absent: checked, and there is genuinely nothing there. This must
            # score as NOT_APPLICABLE: reading it as UNREADABLE would tell the owner Reaper
            # could not tell which season this is, and would lower the special's
            # coverage for a rank it was never meant to have. A real Sonarr read failure is
            # Unknown, and still reaches the UNREADABLE branch below.
            if isinstance(facts.season_rank, Absent):
                return SignalResult(
                    config.signal,
                    0.0,
                    config.weight,
                    Reason("signal_season_special"),
                    evaluated=True,
                    state=SignalState.NOT_APPLICABLE,
                )
            observation = facts.season_rank
            raw = _numeric(observation)
            # Season ranks count from the newest season with files (rank 1) toward the
            # oldest.
            detail = (
                Reason("signal_season_rank", {"rank": int(raw)})
                if raw is not None
                else Reason("signal_season_unreadable")
            )
        case SignalId.FEW_WATCHERS:
            observation = facts.distinct_watchers
            watchers = _numeric(observation)
            # Fewer watchers means more pressure to delete, so this signal is inverted:
            # the pressure comes from how far the count falls short of saturate_at, and
            # that shortfall is never negative.
            raw = max(0.0, float(config.saturate_at) - watchers) if watchers is not None else None
            # A watcher count only means what it says while the watch history spans the
            # whole window. Below that, it is a lower bound: a count from a 90-day
            # history checked against a 365-day window could show full pressure at full
            # coverage, when the same title would show zero pressure once the history
            # reaches back far enough to see the same plays. Scoring the count as-is would
            # report the title as fully checked while hiding that gap.
            #
            # So a count the watch history does not support is treated as unreadable: zero
            # pressure, its weight stays in the denominator, and coverage drops honestly.
            # The SERVER_POPULARITY gate withholds the same way for the same reason.
            #
            # This check only applies to a value Reaper actually read (Known). A value
            # that could not be read (Unknown) usually has a different cause, commonly a
            # show Plex never matched, and blaming that on watch-history depth would point
            # the operator at the wrong fix. Both cases end up scored the same way either
            # way: zero pressure, weight kept, coverage reduced.
            #
            # A genuine zero (Absent, "checked and there is nothing") still goes through
            # this check. Absent normally means real evidence, but "zero watchers" is a claim
            # about the whole window, and a history that does not span the window can only
            # say nothing happened in the part it holds. So a short history still withholds
            # the signal even when the count reads as Absent.
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
                    Reason(
                        "signal_watchers_unchecked",
                        {"window_days": window_days, "cause": short},
                    ),
                    evaluated=False,
                    state=SignalState.UNREADABLE,
                )
            if watchers is None and isinstance(observation, Absent):
                detail = Reason("signal_watchers_no_history")
            elif watchers is None:
                detail = Reason("signal_watchers_unreadable")
            elif watchers == 0:
                detail = Reason("signal_watchers_none", {"window_days": window_days})
            else:
                detail = Reason(
                    "signal_watchers_few",
                    {"count": int(watchers), "window_days": window_days},
                )
        case SignalId.LOW_RATING:
            observation = facts.imdb_rating_tenths
            rating = _numeric(observation)
            # Same shape as FEW_WATCHERS: a low rating is pressure to delete, expressed as
            # the shortfall below saturate_at, so it stays between 0 and weight.
            raw = max(0.0, float(config.saturate_at) - rating) if rating is not None else None
            if rating is not None:
                detail = Reason("signal_imdb", {"value": rating})
            elif isinstance(observation, Absent):
                detail = Reason("signal_imdb_none")
            else:
                detail = Reason("signal_imdb_unreadable")
        case _:
            # A new SignalId with no arm here would leave `raw` and `observation` unset,
            # crashing on the first item scored under it. `assert_never` makes mypy catch
            # the missing arm at type-check time, when the signal is added.
            assert_never(config.signal)

    if raw is None and isinstance(observation, Absent):
        # Checked, and there genuinely is no value: an unrated show, a season with no
        # single release date. This must count as real evidence, evaluated with the weight
        # kept and coverage unaffected, the same way SEASON_RANK
        # and the graded custom-rule branch read an Absent value. Reading it as UNREADABLE
        # would tell the owner Reaper could not read the IMDb rating for a title that
        # simply has none, and would lower every unrated show's coverage for no reason.
        return SignalResult(
            config.signal,
            0.0,
            config.weight,
            detail,
            evaluated=True,
            state=SignalState.NOT_APPLICABLE,
        )

    if raw is None:
        # A value that could not be read contributes zero pressure, the floor. Its weight
        # still counts in the denominator, so a failed read can only lower the score.
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
        # A built-in signal always has a real measurement here, so a zero means the value
        # sits below the floor: watched recently, rated well, or watched by enough people.
        # That argues for keeping the title.
        state=SignalState.ADDS if fraction > 0 else SignalState.ARGUES_KEEP,
    )


def evaluate_custom(
    config: CustomSignalConfig, facts: Facts, *, window_days: int | None = None
) -> SignalResult:
    """Score one user-authored signal, always between 0 and its weight.

    A matched boolean rule, or a graded field above its floor, adds pressure. A value that
    could not be read adds none but keeps its weight in the denominator, so missing data
    can only lower the score, the same as a built-in signal.

    ``window_days`` is the policy's popularity window. A rule authored on a watcher count
    needs it, because the watch mirror only supports that count as far back as it reaches
    (``fields.reach_shortfall``).

    A graded rule carries its ramp on the result, the same as a built-in signal. A boolean
    rule carries none, since it only ever matched or did not, and there is no ramp to
    describe.
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
    """The work behind ``evaluate_custom``, before the ramp is stamped on. Every return
    here leaves it unset."""
    if not config.enabled:
        return SignalResult(
            config.name,
            0.0,
            0,
            Reason("disabled"),
            evaluated=True,
            state=SignalState.NOT_APPLICABLE,
        )

    spec = fields.BY_KEY.get(config.field)

    if config.kind == "graded":
        observation = spec.read(facts) if spec is not None else None
        if isinstance(observation, Absent):
            # Absent is real evidence, "checked, nothing recorded," so this counts as
            # evaluated with the weight kept and coverage unaffected. It must not lower
            # coverage the way a value that could not be read does, or a graded rule on a
            # field one media type never carries would push every one of its items below
            # the coverage floor.
            return SignalResult(
                config.name,
                0.0,
                config.weight,
                Reason("none_recorded", {"field": config.field}),
                evaluated=True,
                # Checked, and there is genuinely nothing recorded. The rule simply has
                # nothing to work with.
                state=SignalState.NOT_APPLICABLE,
            )
        raw = (
            float(observation.value)
            if isinstance(observation, Known) and isinstance(observation.value, int | float)
            else None
        )
        if raw is None:
            # Could not be read: zero pressure, weight retained, not evaluated, so
            # coverage honestly reflects the unchecked value.
            return SignalResult(
                config.name,
                0.0,
                config.weight,
                Reason("field_unreadable", {"field": config.field}),
                evaluated=False,
                state=SignalState.UNREADABLE,
            )
        # A watcher count the watch history does not reach back far enough to support is a
        # lower bound, so it is reported as unchecked. This is more
        # cautious than strictly necessary: the ramp only rises with the count, so a
        # truncated count can only score at or below the true value, unlike FEW_WATCHERS
        # above, where the same truncation would overcharge.
        #
        # It is withheld anyway because a ramp has no value that is already "safe" however
        # much more history arrives: every value it produces is honest but incomplete, so
        # this reads the same way an unreadable value does, keeping coverage honest.
        # (`watchers_all_time` can never reach this branch, since it is protect-only, so
        # `recent_watchers` is the only field a graded rule scores here.)
        if (short := fields.reach_shortfall(spec, facts, window_days=window_days)) is not None:
            return SignalResult(
                config.name,
                0.0,
                config.weight,
                blocked_reason(config.field, short),
                evaluated=False,
                state=SignalState.UNREADABLE,
            )
        fraction = _ramp(raw, float(config.floor), float(config.saturate_at))
        return SignalResult(
            config.name,
            fraction * config.weight,
            config.weight,
            Reason("field_value", {"field": config.field, "value": round(raw)}),
            evaluated=True,
            # A real value below the ramp's floor argues for keeping the title.
            state=SignalState.ADDS if fraction > 0 else SignalState.ARGUES_KEEP,
        )

    # boolean
    if config.condition is None:  # pragma: no cover -- the policy always sets one
        return SignalResult(
            config.name,
            0.0,
            config.weight,
            Reason("misconfigured"),
            evaluated=False,
            state=SignalState.UNREADABLE,
        )
    result = fields.evaluate(config.condition, facts, window_days=window_days)
    if result.blocked:
        # Could not check the input, so it cannot add pressure. Weight retained.
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
        # A yes/no rule that did not match simply does not describe this item.
        state=SignalState.ADDS if result.matched else SignalState.NOT_APPLICABLE,
    )


#: The name of the built-in rewatch keep. Used both as ``KeepConfig.field`` and as this
#: row's name in the stored explanation. It sits outside ``fields.BY_KEY``: the keep cannot
#: be authored, it never appears in the vocabulary, and ``evaluate_keep`` reads the facts for
#: it directly. ``PolicyBody._no_duplicates`` refuses a user-authored keep with this name,
#: so it can never collide with this row.
REWATCH_KEEP = "rewatch_habit"


def _rewatch_count(config: KeepConfig, viewings: Observation[int]) -> Reason:
    """The count clause, worded for the right media type. A movie's viewings are plays; a
    show's are whole re-watches (``KeepConfig.media_type``), so the show wording says
    "again" everywhere the count appears. A count that cannot be read falls back to a
    vaguer "again and again" clause."""
    if isinstance(viewings, Known):
        return Reason("rewatch_count", {"n": int(viewings.value), "media": config.media_type})
    return Reason("rewatch_count_many")


def _rewatch_firing_detail(config: KeepConfig, facts: Facts) -> Reason:
    """The figures behind a firing rewatch keep. The recency figure is the last qualified
    play the condition read."""
    count = _rewatch_count(config, facts.rewatch_viewings)
    last = facts.rewatch_last_play_days
    if isinstance(last, Known):
        return Reason("rewatch_keep_fired_recent", {"count": count, "last_days": float(last.value)})
    return Reason("rewatch_keep_fired", {"count": count})


def _rewatch_miss_detail(config: KeepConfig, facts: Facts) -> Reason:
    """Why the rewatch keep did not fire, with the item's own numbers."""
    viewings = facts.rewatch_viewings
    if not isinstance(viewings, Known) or viewings.value == 0:
        # A show at zero may still have been watched once through: its count tracks whole
        # re-watches, which only start after that first watch-through. The honest wording
        # there is "never again," unlike a bare "never."
        return Reason("rewatch_keep_never", {"media": config.media_type})
    n = int(viewings.value)
    count = _rewatch_count(config, viewings)
    if config.min_viewings is not None and n < config.min_viewings:
        return Reason("rewatch_keep_few", {"count": count})
    if config.recent_days is None:
        # With no recency window set, there is no "not in the last ..." claim to make, so
        # state the total alone.
        return Reason("rewatch_keep_few", {"count": count})
    return Reason("rewatch_keep_stale", {"count": count, "window_days": config.recent_days})


def _rewatch_keep(config: KeepConfig, facts: Facts) -> KeepResult:
    """The built-in rewatch keep. Binary, the same as the list-membership keep.

    The condition is decided here, against two frozen observations and the two bars the
    config carries, so editing a bar replays exactly against a
    stored snapshot. Meeting the condition takes the full discount. Reading it and missing,
    or finding nothing recorded, takes zero, evaluated, with the real figures shown. A value
    that could not be read takes the full discount with ``evaluated=False``. Every keep
    invariant applies here too: the discount can only lower a score, and it can never
    un-protect a title a gate already protected (``services.snapshot._verdict``).

    This has no check against the watch history's reach on purpose. The viewing count is
    already bounded by how far the watch history reaches, and the count that set the
    default bars was measured under that same bound, so a genuine miss is an honest
    zero-discount answer.
    """
    viewings = facts.rewatch_viewings
    last = facts.rewatch_last_play_days
    if isinstance(viewings, Unknown) or isinstance(last, Unknown):
        return KeepResult(
            config.name,
            float(config.max_discount),
            config.max_discount,
            Reason("keep_unchecked", {"check": Reason("check.watch_history")}),
            evaluated=False,
        )
    if isinstance(viewings, Absent):
        # Every real fact builder writes a viewing count, movies as qualified viewings and
        # seasons as the show's replay periods, so a genuine Absent here only comes from a
        # hand-built Facts (in a preview or a test) that never gathered watch history at
        # all. The keep has nothing to say there and discounts nothing.
        return KeepResult(config.name, 0.0, config.max_discount, Reason("does_not_apply"), True)
    met = (
        config.min_viewings is not None
        and config.recent_days is not None
        and int(viewings.value) >= config.min_viewings
        and isinstance(last, Known)
        and float(last.value) <= float(config.recent_days)
    )
    if met:
        return KeepResult(
            config.name,
            float(config.max_discount),
            config.max_discount,
            _rewatch_firing_detail(config, facts),
            True,
        )
    return KeepResult(
        config.name, 0.0, config.max_discount, _rewatch_miss_detail(config, facts), True
    )


def evaluate_keep(
    config: KeepConfig, facts: Facts, *, window_days: int | None = None
) -> KeepResult:
    """One graded keep. A discount between 0 and max_discount, fail-closed toward keeping.

    A value that cannot be read takes the full discount, so missing data always favors
    keeping. Absence must count as real evidence, never an automatic reason to keep the
    title: a value that was checked and genuinely does not exist takes no discount.

    A value the watch history does not reach back far enough to support is treated the
    same as an unreadable value, taking the full discount (``window_days``,
    ``fields.reach_shortfall``).
    """
    if not config.enabled:
        return KeepResult(config.name, 0.0, 0, Reason("disabled"), evaluated=True)

    if config.field == REWATCH_KEEP:
        return _rewatch_keep(config, facts)

    spec = fields.BY_KEY.get(config.field)
    observation = spec.read(facts) if spec is not None else None

    if config.value is not None:
        # The membership form is binary, per GradedKeepSpec.value. Both sides of the
        # name match are lower-cased for comparison, and the fact is a comma-joined list,
        # split the same way `fields.evaluate` splits one.
        wanted = fold(config.value)
        if isinstance(observation, Known) and isinstance(observation.value, str):
            names = {fold(part) for part in observation.value.split(",")}
            on_it = wanted in names
            return KeepResult(
                config.name,
                float(config.max_discount) if on_it else 0.0,
                config.max_discount,
                Reason("keep_on_list" if on_it else "keep_not_on_list", {"name": config.value}),
                True,
            )
        if isinstance(observation, Absent):
            return KeepResult(config.name, 0.0, config.max_discount, Reason("keep_no_lists"), True)
        # The membership could not be read, so this takes the full discount, fail-closed.
        return KeepResult(
            config.name,
            float(config.max_discount),
            config.max_discount,
            Reason("keep_unchecked", {"check": Reason("check.lists")}),
            evaluated=False,
        )

    raw = (
        float(observation.value)
        if isinstance(observation, Known) and isinstance(observation.value, int | float)
        else None
    )
    if raw is None:
        if isinstance(observation, Absent):
            return KeepResult(
                config.name,
                0.0,
                config.max_discount,
                Reason("none_recorded", {"field": config.field}),
                True,
            )
        # Could not be read: fail closed to the maximum discount.
        return KeepResult(
            config.name,
            float(config.max_discount),
            config.max_discount,
            Reason("keep_unchecked", {"check": Reason(f"check.{config.field}")}),
            evaluated=False,
        )

    if (short := fields.reach_shortfall(spec, facts, window_days=window_days)) is not None:
        # A watcher count from a watch history that does not reach back far enough is a
        # lower bound. Under the default `high_keeps` direction, scoring it as-is would make
        # the discount too small, quietly shrinking a keep on evidence Reaper does not
        # actually have, which is the direction that can lose a file. (`low_keeps` would err
        # generously instead, but both take the same path: the honest answer is that this
        # was not checked.) So this takes the maximum discount, the same fail-closed answer
        # a value that could not be read gets above.
        return KeepResult(
            config.name,
            float(config.max_discount),
            config.max_discount,
            Reason(
                "keep_unchecked_cause",
                {"check": Reason(f"check.{config.field}"), "cause": short},
            ),
            evaluated=False,
        )

    fraction = _ramp(raw, float(config.floor), float(config.saturate_at))
    if config.direction == "low_keeps":
        fraction = 1.0 - fraction
    return KeepResult(
        config.name,
        fraction * config.max_discount,
        config.max_discount,
        Reason("field_value", {"field": config.field, "value": round(raw)}),
        True,
    )


@dataclass(frozen=True, slots=True)
class Score:
    value: float
    """0 to 100. Higher means more pressure to delete. This is ``base_value`` minus the
    keep discount, floored at 0, and it is the number the verdict and the simulator decide
    on."""

    coverage: float
    """The share of enabled weight Reaper could actually check, from 0 to 1.

    An item at 40% coverage is one Reaper mostly cannot see. The policy's coverage floor
    holds an item back below a threshold, so it is judged only once enough evidence exists.
    Coverage measures how much removal evidence Reaper saw; a keep decides a separate
    question.
    """

    results: Sequence[SignalResult]

    base_value: float = 0.0
    """The score before any keep discount, shown in the why-panel as the subtotal for
    removal reasons."""

    keep_discount: float = 0.0
    """Points the graded keeps subtracted from ``base_value`` to reach ``value``."""

    keep_results: Sequence[KeepResult] = ()


def score(
    configs: Sequence[SignalConfig],
    facts: Facts,
    *,
    custom_condemn: Sequence[CustomSignalConfig] = (),
    keeps: Sequence[KeepConfig] = (),
    window_days: int = 365,
) -> Score:
    """Total pressure to delete this title, from 0 to 100.

    The denominator is the sum of every enabled weight, including the ones whose value
    could not be read. That is what makes missing data safe: the numerator loses the
    contribution, but the denominator keeps the weight, so the score falls.

    User-authored ``custom_condemn`` signals join the same denominator, so a custom rule
    that could not be read lowers the score exactly the way a built-in one does. There is
    no separate layer of arithmetic to reason about.

    ``keeps`` are a discount applied after the score is computed:
    ``value = max(0, base - total discount)``. Every discount is zero or positive, and a
    keep that could not be read takes its maximum discount, so more missing data can only
    lower the score. A keep can never raise a score, and it can never un-protect a title a
    gate already protected (that is decided before the score is ever read, in
    ``services.snapshot._verdict``).

    ``window_days`` is the policy's popularity window, the span ``distinct_watchers`` was
    counted over. It shapes the "few watchers" detail text, and every reader of a watcher
    count checks the watch history's reach against it before trusting the count
    (``fields.reach_shortfall``). So it can only move the score in one direction: a window
    the watch history does not cover withdraws pressure and lowers coverage. Callers should
    pass the policy's own value. The default here matches the
    shipped window, but a policy that changed it and a caller that omits this argument
    would then disagree in the direction that overstates coverage.
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
