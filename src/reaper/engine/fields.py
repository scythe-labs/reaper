# SPDX-License-Identifier: AGPL-3.0-or-later
"""The field registry -- what a user may say, and in which lane.

Reaper has two lanes and they are not symmetrical.

**The condemn lane is a flat AND of typed conditions.** No OR, no nesting, no NOT,
no operator dropdown on a field where the direction inverts the meaning. Every one
of the owner's requirements fits inside that -- "keep the last 2 seasons" is not a
logic problem, it is a *derived field* (season rank). Maintainerr's issue tracker is
full of "why did my rule match", and the cause is never that boolean algebra is
hard; it is that a user built an expression saying something subtly different from
what they meant, and nothing caught it.

**The protect lane is composable and user-authored.** Because a protection cannot
delete anything: the worst case of a badly written protect rule is that nothing gets
deleted. That asymmetry is the same one already in the kill switch -- the UI can
disable deletion but never enable it -- and it is what lets us hand the owner real
expressive power without handing them a loaded gun.

The registry enforces the asymmetry *structurally*. Each field declares which lanes
it may appear in and which operators it accepts, and the API filters by lane before
the vocabulary ever reaches the browser: ``GET /api/vocabulary?lane=condemn`` cannot
return a protect-only field, so a condemn rule referencing one is not merely rejected
-- it is unconstructable.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from reaper.engine.gates import ABSTAIN, PROTECT, Facts, GateId, GateResult
from reaper.engine.observation import Known, Observation, Unknown


class Lane(enum.StrEnum):
    CONDEMN = "condemn"
    PROTECT = "protect"


class Op(enum.StrEnum):
    """Operators. Deliberately few, and typed.

    Note what is absent: no ``NOT``, no ``!=`` on a numeric, no free-text
    comparison. Each is a way to invert a meaning by accident.
    """

    GTE = "gte"
    LTE = "lte"
    EQ = "eq"
    IN = "in"
    CONTAINS = "contains"


class FieldType(enum.StrEnum):
    DAYS = "days"
    BYTES = "bytes"
    COUNT = "count"
    RATING_TENTHS = "rating_tenths"
    BOOL = "bool"
    TEXT = "text"


NUMERIC_OPS = (Op.GTE, Op.LTE)
BOOL_OPS = (Op.EQ,)
TEXT_OPS = (Op.EQ, Op.IN, Op.CONTAINS)


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One thing a user may write a condition about."""

    key: str
    label: str
    """What the UI shows. Carries the unit, because a bare number is how a rating
    floor of 7.5 ends up compared against a Tomatometer of 96."""

    help_text: str
    type: FieldType
    lanes: tuple[Lane, ...]
    ops: tuple[Op, ...]
    read: Callable[[Facts], Observation[object]]

    unit_suffix: str = ""
    """Rendered inside the input, so the unit cannot be misread."""

    def allows(self, lane: Lane, op: Op) -> bool:
        return lane in self.lanes and op in self.ops


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

REGISTRY: tuple[FieldSpec, ...] = (
    FieldSpec(
        key="days_unwatched",
        label="Days since anyone watched it",
        help_text=(
            "Counted from the last play -- or, if it has never been played, from "
            "whichever is later: when it was added, or the start of your watch "
            "history. Never from 1970."
        ),
        type=FieldType.DAYS,
        unit_suffix="days",
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=NUMERIC_OPS,
        read=lambda f: f.days_observed_unwatched,
    ),
    FieldSpec(
        key="size_bytes",
        label="Size on disk",
        help_text="How much space this file occupies.",
        type=FieldType.BYTES,
        unit_suffix="GB",
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=NUMERIC_OPS,
        read=lambda f: f.size_bytes,
    ),
    FieldSpec(
        key="recent_watchers",
        label="Distinct watchers (recently)",
        help_text=(
            "How many different people have watched this within your popularity "
            "window. Windowed on purpose: on a long-lived server almost everything "
            "has been watched by *someone*, eventually, so an all-time count protects "
            "nearly the whole library and the rule stops meaning anything. Only a "
            "fraction of those items still have watchers in the last year -- and that "
            "is the number that tells you the title is still alive."
        ),
        type=FieldType.COUNT,
        unit_suffix="people",
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=NUMERIC_OPS,
        read=lambda f: f.distinct_watchers,
    ),
    FieldSpec(
        key="watchers_all_time",
        label="Distinct watchers (ever)",
        help_text=(
            "Everyone who has ever watched this. Available as a PROTECTION only -- "
            "using it to condemn would make the recency signal meaningless."
        ),
        type=FieldType.COUNT,
        unit_suffix="people",
        # Protect only. This is the registry doing its job: an all-time watcher count
        # is a fine reason to KEEP something and a terrible reason to delete it, and
        # the lane list is what makes the latter unconstructable.
        lanes=(Lane.PROTECT,),
        ops=NUMERIC_OPS,
        read=lambda f: f.distinct_watchers_all_time,
    ),
    FieldSpec(
        key="imdb_rating",
        label="IMDb rating",
        help_text=(
            "Always pair this with a vote floor. An 8.3 drawn from a few hundred votes "
            "is noise, not quality -- every library holds a few of them, and a bare "
            "rating floor would preserve every one, forever."
        ),
        type=FieldType.RATING_TENTHS,
        unit_suffix="/10",
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=NUMERIC_OPS,
        read=lambda f: f.imdb_rating_tenths,
    ),
    FieldSpec(
        key="imdb_votes",
        label="IMDb vote count",
        help_text=(
            "How many people rated it. A useful protection in its own right: a film "
            "with a million votes is culturally significant even if nobody here has "
            "watched it lately."
        ),
        type=FieldType.COUNT,
        unit_suffix="votes",
        lanes=(Lane.PROTECT,),
        ops=NUMERIC_OPS,
        read=lambda f: f.imdb_votes,
    ),
    FieldSpec(
        key="season_rank",
        label="Season rank (1 = newest)",
        help_text=(
            "Counted over seasons that actually hold files, specials excluded. "
            '"Keep the last 2 seasons" is season_rank <= 2 -- no boolean cleverness '
            "required. Never derived from Sonarr's episodeCount, which is its download "
            "intent, not what is on disk."
        ),
        type=FieldType.COUNT,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=NUMERIC_OPS,
        read=lambda f: f.season_rank,
    ),
    FieldSpec(
        key="on_curated_list",
        label="On a protected list",
        help_text="e.g. the IMDb Top 250, or any list you subscribe to.",
        type=FieldType.TEXT,
        lanes=(Lane.PROTECT,),
        ops=TEXT_OPS,
        read=lambda f: f.in_curated_list,
    ),
    FieldSpec(
        key="whitelisted",
        label="Whitelisted",
        help_text=(
            "Tagged 'reaper-keep' in Sonarr/Radarr, or in your Plex \"Never Reap\" collection."
        ),
        type=FieldType.BOOL,
        lanes=(Lane.PROTECT,),
        ops=BOOL_OPS,
        read=lambda f: f.is_whitelisted,
    ),
    FieldSpec(
        key="streaming_now",
        label="Being watched right now",
        help_text="Re-checked in the seconds before any delete, never only at scan time.",
        type=FieldType.BOOL,
        lanes=(Lane.PROTECT,),
        ops=BOOL_OPS,
        read=lambda f: f.is_streaming_now,
    ),
    FieldSpec(
        key="requested",
        label="Requested by a user",
        help_text=(
            "Whether someone asked for this through your requests app. If Reaper cannot "
            "tell -- the requests app is unreachable, or the title has no id to match on "
            "-- this is left unknown and never counts toward removal."
        ),
        type=FieldType.BOOL,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=BOOL_OPS,
        read=lambda f: f.requested,
    ),
    FieldSpec(
        key="genre",
        label="Genre",
        help_text=(
            'The genres recorded for this title. Use "contains" to match one genre '
            "within a title that has several (e.g. contains Reality)."
        ),
        type=FieldType.TEXT,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=TEXT_OPS,
        read=lambda f: f.genres,
    ),
    FieldSpec(
        key="release_age",
        label="Age since release",
        help_text=(
            "How long ago the title was released. Pairs well with how long it has gone "
            "unwatched -- old and untouched is a stronger case than either alone."
        ),
        type=FieldType.DAYS,
        unit_suffix="days",
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=NUMERIC_OPS,
        read=lambda f: f.release_age_days,
    ),
    FieldSpec(
        key="quality",
        label="File quality",
        help_text=(
            "The quality of the file on disk, as your library names it (e.g. Bluray-1080p, "
            'SDTV). Use "contains" to match a resolution -- contains 2160p for 4K.'
        ),
        type=FieldType.TEXT,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=TEXT_OPS,
        read=lambda f: f.quality,
    ),
    FieldSpec(
        key="show_ended",
        label="The show has ended",
        help_text=(
            "Whether the series has finished for good. An ended show will get no new "
            "seasons to draw viewers back; a returning one still might. TV only."
        ),
        type=FieldType.BOOL,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=BOOL_OPS,
        read=lambda f: f.show_ended,
    ),
)

BY_KEY: dict[str, FieldSpec] = {spec.key: spec for spec in REGISTRY}


def vocabulary(lane: Lane) -> list[FieldSpec]:
    """The fields available in one lane.

    The API calls this before serialising, so a protect-only field is never even
    offered to the condemn editor. A condemn rule referencing ``watchers_all_time``
    is not rejected -- it cannot be built.
    """
    return [spec for spec in REGISTRY if lane in spec.lanes]


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Condition:
    """One typed comparison. The atom of both lanes."""

    field: str
    op: Op
    value: int | str | bool

    def spec(self) -> FieldSpec:
        try:
            return BY_KEY[self.field]
        except KeyError:
            raise ValueError(f"Unknown field {self.field!r}.") from None

    def validate_for(self, lane: Lane) -> None:
        spec = self.spec()
        if lane not in spec.lanes:
            raise ValueError(
                f"{spec.label!r} cannot be used to {lane.value}. "
                f"It is available in: {', '.join(x.value for x in spec.lanes)}."
            )
        if self.op not in spec.ops:
            raise ValueError(
                f"{spec.label!r} does not support {self.op.value}. "
                f"Allowed: {', '.join(o.value for o in spec.ops)}."
            )


@dataclass(frozen=True, slots=True)
class ConditionResult:
    matched: bool
    blocked: bool
    """The field was Unknown, so the condition could not be evaluated."""
    detail: str


def evaluate(condition: Condition, facts: Facts) -> ConditionResult:
    """Evaluate one condition against one item.

    An ``Unknown`` never matches, and says so. In the protect lane that means the
    protection does not fire but is reported as *blocked* -- amber, not green -- so
    "we could not check" is visibly different from "we checked and it was fine".
    """
    spec = condition.spec()
    observation = spec.read(facts)

    if isinstance(observation, Unknown):
        # We could not look. Never evidence -- reported as blocked so the UI can
        # render it amber, distinct from "checked and it was fine".
        return ConditionResult(
            matched=False,
            blocked=True,
            detail=f"could not check {spec.label.lower()}: {observation.reason}",
        )

    if not isinstance(observation, Known):
        # Absent: we looked, and there genuinely is no value. Real evidence, but it
        # cannot satisfy a comparison.
        return ConditionResult(
            matched=False,
            blocked=False,
            detail=f"{spec.label}: none recorded",
        )

    value = observation.value
    target = condition.value
    matched = _compare(condition.op, value, target)

    return ConditionResult(
        matched=matched,
        blocked=False,
        detail=f"{spec.label}: {_render(spec, value)} {condition.op.value} {_render(spec, target)}",
    )


def _compare(op: Op, value: object, target: object) -> bool:
    match op:
        case Op.GTE:
            return _num(value) >= _num(target)
        case Op.LTE:
            return _num(value) <= _num(target)
        case Op.EQ:
            return bool(value == target)
        case Op.IN:
            return isinstance(target, str) and str(value) in target.split(",")
        case Op.CONTAINS:
            return isinstance(target, str) and target.lower() in str(value).lower()


def _num(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    raise ValueError(f"{value!r} is not numeric.")


def _render(spec: FieldSpec, value: object) -> str:
    """Human units. A bare number is how a rating floor meets a Tomatometer."""
    match spec.type:
        case FieldType.RATING_TENTHS:
            return f"{_num(value) / 10:.1f}"
        case FieldType.BYTES:
            return f"{_num(value) / 1_000_000_000:.1f} GB"
        case FieldType.DAYS:
            return f"{_num(value):.0f} days"
        case _:
            return str(value)


Mode = Literal["all"]
"""The condemn lane joins conditions with AND, and only AND.

Not an oversight. OR is expressible by making a second profile, which forces the
owner to name the second thing they mean -- and a named profile can be backtested,
capped and approved on its own terms. A nested OR cannot.
"""


@dataclass(frozen=True, slots=True)
class RuleSet:
    """A lane's conditions."""

    lane: Lane
    conditions: tuple[Condition, ...]

    def __post_init__(self) -> None:
        for condition in self.conditions:
            condition.validate_for(self.lane)


@dataclass(frozen=True, slots=True)
class RuleSetResult:
    matched: bool
    blocked: bool
    results: tuple[ConditionResult, ...]


@dataclass(frozen=True, slots=True)
class CustomProtectGate:
    """A single user-authored protection, wearing the built-in Gate interface.

    One gate per condition, so the why-panel lists each one exactly like a stock protection:
    a matched condition fires ``PROTECT``, an unmatched one is a checked ``ABSTAIN`` (green),
    and an ``Unknown`` input is ``blocked`` (amber) -- never assumed. Because it can only ever
    return PROTECT or ABSTAIN -- there is no CONDEMN constructor to reach -- a mis-authored
    condition can at worst fail to keep something. That is what makes these safe to author.
    """

    condition: Condition
    id: GateId = GateId.CUSTOM

    def evaluate(self, facts: Facts) -> GateResult:
        result = evaluate(self.condition, facts)
        if result.blocked:
            return GateResult(self.id, ABSTAIN, blocked=True, detail=result.detail)
        if result.matched:
            return GateResult(self.id, PROTECT, detail=f"your rule: {result.detail}")
        return GateResult(self.id, ABSTAIN, detail=f"checked your rule: {result.detail}")


def evaluate_rules(rules: RuleSet, facts: Facts) -> RuleSetResult:
    """Evaluate a lane.

    CONDEMN is a flat AND: every condition must match, and a single blocked
    condition means we cannot say the item qualifies -- so it does not.

    PROTECT is an OR of conditions: *any* reason to keep a file is sufficient. That
    is safe by construction, which is exactly why this lane may be user-authored.
    """
    results = tuple(evaluate(condition, facts) for condition in rules.conditions)
    if not results:
        return RuleSetResult(matched=False, blocked=False, results=())

    blocked = any(r.blocked for r in results)

    if rules.lane is Lane.CONDEMN:
        # A blocked condition cannot be assumed true. Unknown never condemns.
        return RuleSetResult(
            matched=all(r.matched for r in results) and not blocked,
            blocked=blocked,
            results=results,
        )

    return RuleSetResult(
        matched=any(r.matched for r in results),
        blocked=blocked,
        results=results,
    )
