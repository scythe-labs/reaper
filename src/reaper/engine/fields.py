# SPDX-License-Identifier: AGPL-3.0-or-later
"""The field registry: what an owner may write a rule about, and in which lane.

Reaper has two lanes for rules, and they work differently on purpose.

**The CONDEMN lane (rules that can lead to removal) only allows a flat list of
conditions, all of which must match.** There is no OR, no nesting, and no NOT.
Every real removal rule fits that shape: "keep the last 2 seasons" is a separate
field (season rank) the owner picks a number for. A tool
that instead lets owners build free-form boolean expressions runs into people whose
rule matched something they did not mean, because nothing caught the mistake.

**The PROTECT lane (rules that can only keep things) can be freely composed.** A
badly written protect rule can at worst fail to keep something. It can never delete
anything. That is why owners get real expressive power there.

The registry enforces this by structure: each field declares
which lane or lanes it may appear in and which operators it accepts. The API filters
the vocabulary by lane before it ever reaches the browser, so a request for the
CONDEMN vocabulary cannot return a protect-only field. A condemn rule referencing
one cannot be built at all.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, assert_never

from reaper.engine.gates import (
    ABSTAIN,
    PROTECT,
    Facts,
    GateId,
    GateResult,
    blocked_reason,
    history_shortfall,
    lifetime_shortfall,
)
from reaper.engine.observation import Known, Observation, Unknown
from reaper.engine.reason import Reason
from reaper.refusal import Refusal
from reaper.text import fold


class Lane(enum.StrEnum):
    CONDEMN = "condemn"
    PROTECT = "protect"


#: A policy governs one media type, movie or TV, tuned separately. A field may not
#: apply to both: "the show has ended" means nothing for a movie. Season scoring
#: always uses the TV policy.
MediaType = Literal["movie", "tv"]
ALL_MEDIA: tuple[MediaType, ...] = ("movie", "tv")


class Op(enum.StrEnum):
    """The comparison operators a rule may use. Deliberately few.

    There is no ``NOT``, no ``!=`` on a number, and no free-text comparison. Each of
    those makes it easy to write a rule that means the opposite of what the owner
    intended.
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


class ReachSpan(enum.StrEnum):
    """How far back a field's value needs watch history to reach before it is trustworthy.

    A watcher count comes from a watch-history mirror that only goes back so far
    (``Facts.history_reach_days``). Short of that span, the count is only a lower bound:
    the plays it cannot see are exactly the ones that would argue
    to keep the file. A field carrying one of these values asks every reader to check
    the reach first. ``None`` marks a field the mirror does not bound at all, such as
    a size, a rating, or a genre.
    """

    POPULARITY_WINDOW = "popularity_window"
    """Counted over the policy's popularity window, so the mirror must span that window."""

    ITEM_LIFETIME = "item_lifetime"
    """Counted over the item's whole life, so the mirror must reach back to when it
    was added (``Facts.days_since_added``)."""


#: Which wording family a numeric field's rule number uses when explained to the
#: owner. The phrases live in the catalog (``why.bar.<family>.<side>``), four per
#: family: ``gte`` fires at its number and ``lte`` stops at its number, so only one
#: side of each pair can say "over" or "under" plainly. Different fact types need
#: different wording: a span of time reads as "past" or "within" a window, a size or
#: rating reads as a plain number, a count usually lands exactly on its number so only
#: the strict side can say "over", and a season rule states the number as the seasons
#: the owner keeps.
_TYPE_FAMILY: dict[FieldType, str] = {
    FieldType.DAYS: "days",
    FieldType.BYTES: "bytes",
    FieldType.RATING_TENTHS: "rating",
    FieldType.COUNT: "count",
}

#: Per-field overrides of the family above, checked before it.
_BAR_FAMILY: dict[str, str] = {"season_rank": "season"}


#: Plain words for a lane, shown in a saved-policy error the policy editor renders
#: verbatim. The enum values themselves ("condemn", "protect") are internal names and
#: must never reach the owner. `policy.py` phrases the same refusal with the same
#: words, since a mismatch would show the owner two different vocabularies.
_LANE_USE: dict[Lane, str] = {
    Lane.CONDEMN: "remove things",
    Lane.PROTECT: "keep things",
}
_LANE_HOME: dict[Lane, str] = {
    Lane.CONDEMN: "a removal rule",
    Lane.PROTECT: "a protection",
}

#: The operators spelled the way a rule sentence already spells them, so a rejection
#: message uses the same words as the rule it rejects.
_OP_NAME: dict[Op, str] = {
    Op.GTE: "at or above",
    Op.LTE: "at or below",
    Op.EQ: "is",
    Op.IN: "is one of",
    Op.CONTAINS: "contains",
}


def _join_or(parts: list[str]) -> str:
    """Join as `"a", "b" or "c"`, the way a person reads a list at a glance, unlike a
    comma-joined dump."""
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return f"{', '.join(parts[:-1])} or {parts[-1]}"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One thing an owner may write a rule about."""

    key: str

    type: FieldType
    lanes: tuple[Lane, ...]
    ops: tuple[Op, ...]
    read: Callable[[Facts], Observation[object]]

    media_types: tuple[MediaType, ...] = ALL_MEDIA
    """Which policy (movie, TV, or both) offers this field. A TV-only field
    (``show_ended``, ``season_rank``) is not offered on a movie policy, since a movie
    has no show and no season and the rule would never fire. The API filters on this
    the same way it filters on ``lanes``, so a movie policy cannot even build the
    rule. A rule saved before this filter existed still reads Absent and only ever
    leans toward keeping, so it does not affect scoring."""

    multi: bool = False
    """The fact is a comma-joined list ("Horror, Comedy") of every value that applies.
    ``eq`` and ``in`` compare against each element, since otherwise a title with several
    genres could never equal any one of them and a protection the owner wrote would
    never fire."""

    # ---- How this field explains itself -----------------------------------
    # A label is a short form caption ("Whitelisted"). The why-panel
    # states what Reaper found in the owner's own words, so each field's phrasing
    # lives in the catalog: ``why.field.<key>`` (the sentence subject),
    # ``why.check.<key>`` (the "could not check ..." phrase), ``why.cond_value.<key>``
    # per numeric field, and ``why.cond_bool.<key>.true`` / ``.false`` per boolean
    # one. A test fails when a registry key is missing its catalog entries.

    reach_span: ReachSpan | None = None
    """Set when the value comes from the watch mirror and is only trustworthy once
    the mirror reaches far enough back. Declared here so every reader of this field
    automatically inherits the check."""

    def allows(self, lane: Lane, op: Op) -> bool:
        return lane in self.lanes and op in self.ops


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

REGISTRY: tuple[FieldSpec, ...] = (
    FieldSpec(
        key="days_unwatched",
        type=FieldType.DAYS,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=NUMERIC_OPS,
        read=lambda f: f.days_observed_unwatched,
    ),
    FieldSpec(
        key="size_bytes",
        type=FieldType.BYTES,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=NUMERIC_OPS,
        read=lambda f: f.size_bytes,
    ),
    FieldSpec(
        key="recent_watchers",
        type=FieldType.COUNT,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=NUMERIC_OPS,
        read=lambda f: f.distinct_watchers,
        reach_span=ReachSpan.POPULARITY_WINDOW,
    ),
    FieldSpec(
        key="watchers_all_time",
        type=FieldType.COUNT,
        # Protect only: an all-time watcher count is a good reason to keep something
        # and a bad reason to delete it. Restricting the lane here makes the bad rule
        # impossible to build.
        lanes=(Lane.PROTECT,),
        ops=NUMERIC_OPS,
        read=lambda f: f.distinct_watchers_all_time,
        reach_span=ReachSpan.ITEM_LIFETIME,
    ),
    FieldSpec(
        key="imdb_rating",
        type=FieldType.RATING_TENTHS,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=NUMERIC_OPS,
        read=lambda f: f.imdb_rating_tenths,
    ),
    FieldSpec(
        key="imdb_votes",
        type=FieldType.COUNT,
        lanes=(Lane.PROTECT,),
        ops=NUMERIC_OPS,
        read=lambda f: f.imdb_votes,
    ),
    FieldSpec(
        key="season_rank",
        type=FieldType.COUNT,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        media_types=("tv",),
        ops=NUMERIC_OPS,
        read=lambda f: f.season_rank,
    ),
    FieldSpec(
        key="on_list",
        # Every list, whatever its source. The fact behind this is ``Facts.on_lists``,
        # the names of every list holding the item, so a rule here works for any list:
        # tag, collection, watchlist, or IMDb. A rule stored under the old field name
        # ``on_curated_list`` is rewritten to this one by
        # ``policy_migrations.convert_list_protections``.
        type=FieldType.TEXT,
        lanes=(Lane.PROTECT,),
        ops=TEXT_OPS,
        multi=True,
        read=lambda f: f.on_lists,
    ),
    FieldSpec(
        key="whitelisted",
        type=FieldType.BOOL,
        lanes=(Lane.PROTECT,),
        ops=BOOL_OPS,
        read=lambda f: f.is_whitelisted,
    ),
    FieldSpec(
        key="streaming_now",
        type=FieldType.BOOL,
        lanes=(Lane.PROTECT,),
        ops=BOOL_OPS,
        read=lambda f: f.is_streaming_now,
    ),
    FieldSpec(
        key="requested",
        type=FieldType.BOOL,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=BOOL_OPS,
        read=lambda f: f.requested,
    ),
    FieldSpec(
        key="genre",
        type=FieldType.TEXT,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=TEXT_OPS,
        multi=True,
        read=lambda f: f.genres,
    ),
    FieldSpec(
        key="release_age",
        type=FieldType.DAYS,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        # Movie-only: a season has no single release date, so ``season_scan.
        # build_season_facts`` writes Absent for every season. Offering this on a TV
        # policy would sell a protection that could never fire. A TV removal rule on
        # it would be worse than useless: removal weights always total 100, so a rule
        # that never fires would permanently take its points away from every TV score.
        media_types=("movie",),
        ops=NUMERIC_OPS,
        read=lambda f: f.release_age_days,
    ),
    FieldSpec(
        key="quality",
        type=FieldType.TEXT,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        # Movie-only for the same reason as ``release_age``: a season mixes episode
        # qualities, so ``build_season_facts`` writes Absent for every season.
        media_types=("movie",),
        ops=TEXT_OPS,
        read=lambda f: f.quality,
    ),
    FieldSpec(
        key="show_ended",
        type=FieldType.BOOL,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        media_types=("tv",),
        ops=BOOL_OPS,
        read=lambda f: f.show_ended,
        # Known-false covers a show still airing and one that has not started yet, so
        # this says the show is not finished and claims nothing more than that.
    ),
)

BY_KEY: dict[str, FieldSpec] = {spec.key: spec for spec in REGISTRY}

RECENT_WATCHERS: FieldSpec = BY_KEY["recent_watchers"]
"""The windowed watcher count's spec. This must use direct subscripting, never
``BY_KEY.get("recent_watchers")``: a renamed key then raises immediately, since
subscripting has no silent fallback.

``signals.evaluate_signal``'s built-in ``FEW_WATCHERS`` branch reads the same fact this
spec declares, so it must consult the same reach bound. A ``None`` spec would make
``reach_shortfall(None, ...)`` read as "no bound applies": the check would
switch itself off without anyone noticing, in the direction that lets a scan condemn on
bad evidence."""


def vocabulary(lane: Lane, media_type: MediaType | None = None) -> list[FieldSpec]:
    """The fields available in one lane, optionally narrowed to one media type.

    The API calls this before sending fields to the browser, so a protect-only field
    is never even offered to the removal-rule editor. ``watchers_all_time`` can never be
    built into a removal rule, since the field is simply never offered there. When
    ``media_type`` is given, a field that does not apply to it (``show_ended`` on a
    movie policy) is dropped the same way, so the editor only offers fields that fit
    the policy being edited. ``None`` keeps every field, for a caller not editing one
    particular media type.
    """
    return [
        spec
        for spec in REGISTRY
        if lane in spec.lanes and (media_type is None or media_type in spec.media_types)
    ]


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
            raise Refusal("error.policy.unknown_field", field=self.field) from None

    def validate_for(self, lane: Lane) -> None:
        spec = self.spec()
        if lane not in spec.lanes:
            allowed = _join_or([_LANE_HOME[x] for x in spec.lanes])
            raise Refusal(
                "error.policy.field_wrong_lane",
                field=self.field,
                use=_LANE_USE[lane],
                allowed=allowed,
            )
        if self.op not in spec.ops:
            allowed = _join_or([f'"{_OP_NAME[o]}"' for o in spec.ops])
            raise Refusal(
                "error.policy.field_wrong_operator",
                field=self.field,
                op=_OP_NAME[self.op],
                allowed=allowed,
            )
        self._validate_value_type(spec)

    def _validate_value_type(self, spec: FieldSpec) -> None:
        """The typed value must match the field's type. Checked when the policy is saved.

        JSON keeps ``"500"`` a string on a numeric field, since the wire type is
        ``int | str | bool`` and nothing coerces it. Without this check, the policy
        would save and hash cleanly, then crash inside ``score()`` or
        ``evaluate_all`` on the next scan. Refusing it here, with a clear error naming
        the field, is the honest failure. It also catches a quieter problem: ``in`` or
        ``contains`` against a non-text value can never match, so a protection the
        owner believes exists would silently do nothing.

        An empty or whitespace-only text value is refused for the same reason, and it
        is the more dangerous case. ``contains ""`` matches every item whose text fact
        is known, so a removal rule written that way adds its full weight across the
        entire library while rendering as the unfinished sentence "Genre contains ".
        The same problem hits a value that is just a space. On the protect lane the
        failure is quiet instead of loud: ``in ""`` splits into no elements, so it can
        never match and the protection looks fine forever while doing nothing. Both
        are refused here, so a stored policy cannot carry a rule that matches
        everything or nothing.

        An ``eq`` target that contains the character the field's list is joined on is
        the same problem in a third, quieter shape: it looks like an ordinary single
        value everywhere it is shown. See the check below.
        """
        value = self.value
        if spec.type is FieldType.BOOL:
            if not isinstance(value, bool):
                raise Refusal("error.policy.field_expects_bool", field=self.field, value=value)
        elif spec.type is FieldType.TEXT:
            if not isinstance(value, str):
                raise Refusal("error.policy.field_expects_text", field=self.field, value=value)
            if not value.strip():
                raise Refusal("error.policy.field_needs_value", field=self.field)
            if self.op is Op.IN and not _split_csv(value):
                # A comma-only list (",", " , ") survives the strip above but splits
                # into nothing, so it is the same never-matches protection in disguise.
                raise Refusal("error.policy.field_needs_list_value", field=self.field)
            if self.op is Op.EQ and spec.multi and "," in value:
                # A multi-valued fact is one comma-joined string, and ``_compare``
                # splits it back apart to test membership. So an ``eq`` target that
                # contains a comma can never be one of its own fact's elements: the
                # rule saves, hashes, and shows on the Policy page as a live
                # protection that covers nothing. ``list_config._clean_name`` refuses
                # a comma where a list name is typed by the owner. This check catches
                # the same problem coming through a hand-written or imported policy.
                raise Refusal("error.policy.field_value_has_comma", field=self.field)
        # Numeric field types (days, bytes, count, rating tenths). Python treats bool
        # as a subclass of int, so it must be rejected explicitly.
        elif isinstance(value, bool) or not isinstance(value, int):
            raise Refusal("error.policy.field_expects_number", field=self.field, value=value)


@dataclass(frozen=True, slots=True)
class ConditionResult:
    matched: bool
    blocked: bool
    """The field was Unknown, so the condition could not be evaluated."""
    detail: Reason


def reach_shortfall(
    spec: FieldSpec | None, facts: Facts, *, window_days: int | None
) -> Reason | None:
    """Why the watch mirror cannot support this field's value, as a typed reason.

    Returns ``None`` when the mirror can support it, and also for every field the
    mirror does not bound at all. This is the one place that checks both watcher
    counts against the mirror's reach, so the PROTECT lane, the CONDEMN lane, the
    graded keeps, and the built-in ``FEW_WATCHERS`` signal all agree.

    ``window_days`` is the policy's popularity window, the span ``distinct_watchers``
    was counted over. Every production caller derives it from
    ``policy.PolicyBody.popularity_window_days``, the same call that built the count
    in ``snapshot._watch_stats``, so the two always describe the same span. ``None``
    means the caller did not state a window. This must never be read as license to
    assume the shipped default: doing so could tell an operator running a longer window
    that a truncated count is complete. So a missing window resolves to "cannot
    establish" instead.
    """
    if spec is None or spec.reach_span is None:
        return None
    # Matched member by member with no fallback, so a new span can never inherit the
    # answer computed for a different one. A fallback arm here once let a new span
    # silently measure the mirror against the wrong thing, which is the permissive
    # direction for a helper whose whole job is withholding unsupported counts.
    # ``assert_never`` makes mypy flag a missing case, and a test named
    # ``TestEveryReachSpanIsRoutedByName`` catches an author who adds a span without
    # updating every site that switches on it.
    match spec.reach_span:
        case ReachSpan.POPULARITY_WINDOW:
            if window_days is None:
                return Reason("cause.window_not_recorded")
            return history_shortfall(facts.history_reach_days, float(window_days))
        case ReachSpan.ITEM_LIFETIME:
            # Through the same shared helper the season path's keep-rule conflict
            # detector uses: it compares two all-time counts directly, without
            # reading ``Facts``, so the span it needs has to come from one place.
            return lifetime_shortfall(facts.history_reach_days, facts.days_since_added)
        case _:
            assert_never(spec.reach_span)


def _survives_more_history(op: Op, *, matched: bool) -> bool:
    """Would this outcome still hold once the plays the mirror cannot see are added?

    A count drawn from a mirror that does not reach far enough is a lower bound. Any
    history it is missing can only raise it. So two of the four
    possible outcomes are already settled and need no more reach: "at least N" that
    the count already clears stays cleared however much more history arrives, and "at
    most N" that it already exceeds stays exceeded. The other two outcomes are the
    ones a deeper mirror could still overturn, and they are the dangerous pair: an
    unmatched ``gte`` would withdraw a protection ("nobody has ever watched it"), and
    a matched ``lte`` would add a removal rule's full weight ("nobody watched it
    recently").

    Only numeric operators reach here, since only numeric fields carry a
    ``reach_span``. Anything else is treated as overturnable, which is the direction
    that favors keeping the file.
    """
    match op:
        case Op.GTE:
            return matched
        case Op.LTE:
            return not matched
        case _:  # pragma: no cover -- no text or bool spec is reach-bounded
            return False


def can_add_pressure_under_a_shortfall(op: Op) -> bool:
    """Whether a boolean rule under this operator can still raise any item's score.

    A boolean rule is all-or-nothing: it adds its full weight when it matches and
    nothing when it does not. If a match is the outcome :func:`evaluate` blocks, the
    rule adds nothing to any item's score for as long as the shortfall lasts, whether
    that item matched or not.

    So under ``gte`` a matched item still earns the weight and the rule can still
    raise scores, while under ``lte`` no item can earn it at all until the shortfall
    clears. ``policy_warnings.inspect`` needs exactly this distinction for its removal-rule
    warnings, so it must call this function, never work it out itself.
    """
    return _survives_more_history(op, matched=True)


def evaluate(
    condition: Condition, facts: Facts, *, window_days: int | None = None
) -> ConditionResult:
    """Evaluate one condition against one item.

    An ``Unknown`` value never matches, and says so plainly. On the PROTECT lane that
    means the protection does not fire, but it is reported as blocked, shown amber,
    so "we could not check" reads differently from "we checked and
    it was fine".

    A ``Known`` value the evidence cannot actually support is treated the same way.
    See ``reach_shortfall``: a watcher count from a mirror that does not reach far
    enough back is a lower bound, so an outcome it could still overturn is reported as
    blocked. ``window_days`` defaults to ``None``, meaning "not
    stated", because guessing a window would risk trusting a count the mirror cannot
    actually support.
    """
    spec = condition.spec()
    observation = spec.read(facts)

    if isinstance(observation, Unknown):
        # We could not look, so this is never evidence. Reported as blocked so the
        # UI can render it amber, distinct from "checked and it was fine".
        return ConditionResult(
            matched=False,
            blocked=True,
            detail=blocked_reason(spec.key, observation.reason),
        )

    if not isinstance(observation, Known):
        # Absent: we looked, and there genuinely is no value. Real evidence, but it
        # cannot satisfy a comparison.
        return ConditionResult(
            matched=False,
            blocked=False,
            detail=Reason("none_recorded", {"field": spec.key}),
        )

    value = observation.value
    target = condition.value
    try:
        matched = _compare(condition.op, value, target, multi=spec.multi)
        detail = _explain(spec, condition.op, value, target, matched=matched)
    except ValueError as exc:
        # A backup check behind ``validate_for``'s check at save time: a stored rule
        # whose value cannot be compared against this field (saved before the type
        # check existed, or edited by hand) marks just this item as blocked, amber,
        # "could not check". This must never raise out of ``score()``/``evaluate_all``
        # and abort the whole scan. Blocked fails safe on both lanes: a protect
        # condition blocked this way makes the verdict abstain, and a removal rule
        # adds no pressure while its weight still counts toward the total.
        return ConditionResult(
            matched=False,
            blocked=True,
            detail=blocked_reason(spec.key, Reason("cause.error", {"text": str(exc)})),
        )

    # A known value the evidence cannot actually support. Checked after the
    # comparison, because only the outcome tells us whether the mirror's reach
    # matters: two of the four outcomes hold true whatever a deeper mirror would add,
    # and blocking those would withhold a protection that genuinely fired, or drop
    # pressure that was honestly measured. The other two outcomes are reported as
    # unchecked, in the same amber "could not check" shape as an Unknown input, which
    # ``api.review._chip`` and ``WhyPanel`` both read.
    if not _survives_more_history(condition.op, matched=matched) and (
        (short := reach_shortfall(spec, facts, window_days=window_days)) is not None
    ):
        return ConditionResult(
            matched=False,
            blocked=True,
            detail=blocked_reason(spec.key, short),
        )

    return ConditionResult(matched=matched, blocked=False, detail=detail)


def _split_csv(text: str) -> list[str]:
    """Comma-separated elements, trimmed and lower-cased for comparison. How both
    sides of ``in`` (and the multi-valued side of ``eq``) are read."""
    return [part.casefold() for part in _split_raw(text)]


def _split_raw(text: str) -> list[str]:
    """The same split, keeping the original spelling. Explanations quote a genre back
    in the library's original spelling, before the comparison lower-cased it for
    matching."""
    return [part.strip() for part in text.split(",") if part.strip()]


def _compare(op: Op, value: object, target: object, *, multi: bool = False) -> bool:
    match op:
        case Op.GTE:
            return _num(value) >= _num(target)
        case Op.LTE:
            return _num(value) <= _num(target)
        case Op.EQ:
            if isinstance(value, str) and isinstance(target, str):
                # Case- and whitespace-insensitive for text: Plex title-cases what it
                # stores, and the owner types the target by hand. A multi-valued fact
                # matches when any of its elements equals the target.
                if multi:
                    return fold(target) in _split_csv(value)
                return fold(value) == fold(target)
            return bool(value == target)
        case Op.IN:
            if not isinstance(target, str):
                return False  # rejected when the rule is saved; a stored one cannot match
            targets = set(_split_csv(target))
            # Trimmed and lower-cased on both sides, or a list like "Anime, Documentary"
            # would silently match nothing because of the space after the comma. A
            # multi-valued fact matches on any shared element.
            if multi and isinstance(value, str):
                return not targets.isdisjoint(_split_csv(value))
            return fold(str(value)) in targets
        case Op.CONTAINS:
            # Lower-cased on both sides, the same as eq and in above, using a
            # comparison that also handles accented letters correctly (so
            # "STRASSE" still matches "Straße"). ``on_list`` is protect-only, so a
            # rule that silently stops matching would withdraw a protection while
            # the Policy page keeps showing it as live.
            return isinstance(target, str) and fold(target) in fold(str(value))


def _num(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    raise Refusal("error.policy.value_not_numeric", value=str(value))


# ---------------------------------------------------------------------------
# Explaining a condition
# ---------------------------------------------------------------------------
#
# Every explanation states what Reaper found first and what the rule asked for
# second. The owner already knows what they asked for; what they opened the panel
# to see is what this title actually is. Both must read as plain English: an
# unmatched condition appears in the why-panel just as often as a matched one,
# under "Your rule didn't match: ...".


def _explain(
    spec: FieldSpec, op: Op, value: object, target: int | str | bool, *, matched: bool
) -> Reason:
    match spec.type:
        case FieldType.BOOL:
            # States the fact, never the comparison. "eq false" that matched and "eq
            # true" that missed describe the same world, so stating the fact plainly
            # is the only wording where a miss cannot be misread as the opposite
            # being true. One catalog entry per field and side
            # (``why.cond_bool.<key>.true`` / ``.false``).
            return Reason(f"cond_bool.{spec.key}.{'true' if value else 'false'}")
        case FieldType.TEXT:
            return _explain_text(spec, op, value, target, matched=matched)
        case _:
            return _explain_number(spec, op, value, target, matched=matched)


def _explain_text(
    spec: FieldSpec, op: Op, value: object, target: int | str | bool, *, matched: bool
) -> Reason:
    """Build the explanation for a text condition.

    The subject resolves to ``why.field.<key>`` in the catalog. The owner's own typed
    values ride along as raw params, since they are the owner's own spelling, not
    catalog copy."""
    field_id = spec.key
    wanted = str(target).strip()
    match op:
        case Op.CONTAINS:
            # A substring test over the whole value, even where the value is a list.
            kind = "contains" if matched else "not_contains"
            return Reason(f"cond_text.{kind}", {"field": field_id, "wanted": wanted})
        case Op.EQ if spec.multi:
            # ``eq`` on a list checks each element, so the wording says "includes", never "is".
            kind = "includes" if matched else "not_includes"
            return Reason(f"cond_text.{kind}", {"field": field_id, "wanted": wanted})
        case Op.EQ:
            if matched:
                return Reason("cond_text.is", {"field": field_id, "value": str(value)})
            return Reason(
                "cond_text.is_not", {"field": field_id, "value": str(value), "wanted": wanted}
            )
        case Op.IN if spec.multi:
            if not matched:
                return Reason("cond_text.none_of", {"field": field_id, "list": _listed(wanted)})
            # Name what actually matched, not the whole list the rule offered, so the
            # owner does not have to work out which part fired.
            return Reason(
                "cond_text.includes",
                {"field": field_id, "wanted": _shared(str(value), wanted) or _listed(wanted)},
            )
        case _:
            # A match names what matched. A miss names what the rule wanted. Repeating
            # the whole list on a match would leave the owner to spot which part fired,
            # when the value is already the answer.
            if matched:
                return Reason("cond_text.is", {"field": field_id, "value": str(value)})
            return Reason(
                "cond_text.is_not_one_of",
                {"field": field_id, "value": str(value), "list": _listed(wanted)},
            )


def _explain_number(
    spec: FieldSpec, op: Op, value: object, target: int | str | bool, *, matched: bool
) -> Reason:
    """Build the explanation for a numeric condition: a value clause plus a bar clause.

    The value clause is per field (``why.cond_value.<key>``), so each field keeps its
    own phrasing and units. The bar clause is per family and operator side
    (``why.bar.<family>.<gte_met|gte_missed|lte_met|lte_missed>``), four phrases rather
    than two, because ``gte`` fires at its number while ``lte`` stops at its number, so
    only one side of each pair may say "over" or "under" outright. The day-family bar
    must state the typed number back in exact days, never a rounded phrase: rounding
    both sides could make a rule at 400 days and a title at 396 days read as
    the same number.
    """
    value_clause = Reason(f"cond_value.{spec.key}", {"value": _num(value)})
    match op:
        case Op.GTE:
            bar = "gte_met" if matched else "gte_missed"
        case Op.LTE:
            bar = "lte_met" if matched else "lte_missed"
        case _:  # pragma: no cover -- a numeric field accepts no other operator
            # Nothing to say about a bar we have no phrasing for, so claim nothing.
            return value_clause
    family = _BAR_FAMILY.get(spec.key) or _TYPE_FAMILY.get(spec.type, "count")
    return Reason(
        "cond_number",
        {
            "value": value_clause,
            "bar": Reason(f"bar.{family}.{bar}", {"target": _num(target)}),
        },
    )


def _listed(target: str) -> str:
    """A rule's list, with even spacing regardless of how the owner typed it."""
    return ", ".join(_split_raw(target))


def _shared(value: str, target: str) -> str:
    """The elements a list-valued fact and a rule's list have in common, spelled the
    way the library spells them."""
    wanted = set(_split_csv(target))
    return ", ".join(part for part in _split_raw(value) if part.casefold() in wanted)


@dataclass(frozen=True, slots=True)
class CustomProtectGate:
    """A single owner-authored protection, built to match the built-in gate interface.

    One gate per condition, so the why-panel lists each one exactly like a built-in
    protection: a matched condition fires PROTECT, an unmatched one is a checked
    ABSTAIN shown green, and an Unknown input is blocked and shown amber, never
    assumed either way. This can only ever return PROTECT or ABSTAIN. There is no way
    for it to condemn, so a badly written condition can at worst fail to keep
    something. That is what makes these safe for an owner to author.
    """

    condition: Condition
    id: GateId = GateId.CUSTOM

    window_days: int | None = None
    """The policy's popularity window, so a rule on a windowed watcher count can tell
    whether the mirror covered it (``reach_shortfall``). Built from the policy in
    ``services.scan_runner.build_gates``. ``None`` reads as impossible to establish,
    so it must block, never assume an answer."""

    def evaluate(self, facts: Facts) -> GateResult:
        result = evaluate(self.condition, facts, window_days=self.window_days)
        if result.blocked:
            return GateResult(self.id, ABSTAIN, blocked=True, detail=result.detail)
        if result.matched:
            return GateResult(
                self.id, PROTECT, detail=Reason("custom_fired", {"cond": result.detail})
            )
        return GateResult(
            self.id, ABSTAIN, detail=Reason("custom_checked", {"cond": result.detail})
        )
