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
deleted. That asymmetry is the same one already in the kill switch -- arming it is
password-gated, disarming it is one ungated click, because making Reaper safer is
never worth guarding -- and it is what lets us hand the owner real expressive power
without handing them a loaded gun.

The registry enforces the asymmetry *structurally*. Each field declares which lanes
it may appear in and which operators it accepts, and the API filters by lane before
the vocabulary ever reaches the browser: ``GET /api/vocabulary?lane=condemn`` cannot
return a protect-only field, so a condemn rule referencing one is not merely rejected
-- it is unconstructable.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, assert_never

from reaper.clock import humanize_days
from reaper.engine.gates import (
    ABSTAIN,
    PROTECT,
    Facts,
    GateId,
    GateResult,
    history_shortfall,
    lifetime_shortfall,
)
from reaper.engine.observation import Known, Observation, Unknown
from reaper.text import fold


class Lane(enum.StrEnum):
    CONDEMN = "condemn"
    PROTECT = "protect"


#: A policy governs one media type, and the two are tuned separately. A field may not
#: apply to both: "the show has ended" is meaningless for a movie. Season scoring is
#: derived from the TV policy, so the policy-level type is always "movie" or "tv".
MediaType = Literal["movie", "tv"]
ALL_MEDIA: tuple[MediaType, ...] = ("movie", "tv")


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


class ReachSpan(enum.StrEnum):
    """How much watch history a field's value needs before it means what it says.

    Watcher counts come from a mirror that begins somewhere
    (``Facts.history_reach_days``). Under that span the count is a LOWER BOUND, not an
    answer, and the plays it cannot see are exactly the ones that would have kept the
    file. A spec carrying one of these is asking every lane that reads it to check the
    reach first (rule 140); a spec carrying ``None`` reads a fact the mirror does not
    bound -- a size, a rating, a genre.
    """

    POPULARITY_WINDOW = "popularity_window"
    """Counted over the policy's popularity window, so the mirror must span it."""

    ITEM_LIFETIME = "item_lifetime"
    """Counted over all time, so the mirror must span the item's whole life here
    (``Facts.days_since_added``)."""


@dataclass(frozen=True, slots=True)
class BarPhrases:
    """How a rule's own number reads beside the value it was compared against.

    Four phrases, not two, because the operators bracket their number differently:
    ``gte`` fires *at* its number and ``lte`` stops *at* its number, so in each pair
    only one side may claim "over" or "under" outright. Getting that wrong tells an
    owner their file was over a bar it in fact sat exactly on.
    """

    gte_met: str
    """value >= the number. Each phrase takes the number as ``{}``."""
    gte_missed: str
    """value < the number, strictly."""
    lte_met: str
    """value <= the number."""
    lte_missed: str
    """value > the number, strictly."""


_DEFAULT_BARS = BarPhrases(
    gte_met="at or over your {}",
    gte_missed="under your {}",
    lte_met="at or under your {}",
    lte_missed="over your {}",
)
"""Exact on both sides, which is what a count needs: watcher and vote counts land
*on* their number often, so only the strict side may say "over" or "under" flatly."""

_BARS: dict[FieldType, BarPhrases] = {
    # A span of time is "past" or "within" a window, never over or under it.
    FieldType.DAYS: BarPhrases(
        gte_met="past your {}",
        gte_missed="within your {}",
        lte_met="within your {}",
        lte_missed="past your {}",
    ),
    # A size and a rating are continuous: landing exactly on the number does not
    # happen the way a whole count does, so the plainer word is the honest one.
    FieldType.BYTES: BarPhrases(
        gte_met="over your {}",
        gte_missed="under your {}",
        lte_met="within your {}",
        lte_missed="over your {}",
    ),
    FieldType.RATING_TENTHS: BarPhrases(
        gte_met="over your {}",
        gte_missed="under your {}",
        lte_met="at or under your {}",
        lte_missed="over your {}",
    ),
}

_SEASON_BARS = BarPhrases(
    # "keep the last N" is an lte rule, so there the number really is the count kept.
    # A gte rule sets where removal starts instead, and saying "you keep" of it would
    # misstate the owner's own rule by one season.
    gte_met="at or past the {} you set",
    gte_missed="newer than the {} you set",
    lte_met="within the {} you keep",
    lte_missed="past the {} you keep",
)


def describe_season_rank(rank: float) -> str:
    """A season's place, counting back from the newest season that has files.

    Rank 1 is the *most recent* season with files on disk (see
    ``clients.sonarr_stats.rank_seasons``), so it is the newest season and must never
    be described as an old one. Callers supply the article and any suffix: "The newest
    season" in a rule explanation, "the newest season on disk" in a signal.
    """
    place = int(rank)
    if place <= 1:
        return "newest season"
    if place == 2:
        return "second-newest season"
    if place == 3:
        return "third-newest season"
    return f"{place}{_ordinal_suffix(place)}-newest season"


def _ordinal_suffix(number: int) -> str:
    if 11 <= number % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")


#: How a lane reads to the person who chose it. The enum's own values ("condemn",
#: "protect") are engine words and must never reach a saved-policy error, which is
#: rendered verbatim in the policy editor. `policy.py` phrases the same refusal the
#: same way; the two have to agree or one page shows two vocabularies.
_LANE_USE: dict[Lane, str] = {
    Lane.CONDEMN: "remove things",
    Lane.PROTECT: "keep things",
}
_LANE_HOME: dict[Lane, str] = {
    Lane.CONDEMN: "a removal rule",
    Lane.PROTECT: "a protection",
}

#: The operator keys spelled the way the rule sentences already spell them, so a
#: rejection and the rule it was rejected from speak the same language.
_OP_NAME: dict[Op, str] = {
    Op.GTE: "at or above",
    Op.LTE: "at or below",
    Op.EQ: "is",
    Op.IN: "is one of",
    Op.CONTAINS: "contains",
}


def _join_or(parts: list[str]) -> str:
    """Join as `"a", "b" or "c"`: a list a person reads, not a comma-joined dump."""
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return f"{', '.join(parts[:-1])} or {parts[-1]}"


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

    media_types: tuple[MediaType, ...] = ALL_MEDIA
    """Which policy this field applies to. Both by default. A TV-only field
    (``show_ended``, ``season_rank``) is not offered on a movie policy: a movie has no
    show and no season, so the rule would silently never fire. The API filters on this
    before serializing, the same as ``lanes`` -- a movie policy cannot even build the
    rule. A stale rule saved before this filter still reads Absent and only ever leans
    toward keeping, so scoring is unaffected."""

    unit_suffix: str = ""
    """Rendered inside the input, so the unit cannot be misread."""

    multi: bool = False
    """The fact is a comma-joined list ("Horror, Comedy"), not one value. ``eq`` and
    ``in`` evaluate per element -- a multi-genre title could otherwise never equal any
    single genre, and a protection the owner wrote would silently never fire."""

    # ---- How this field explains itself -----------------------------------
    # A label is a form caption ("Whitelisted"), and a caption is not a sentence. The
    # why-panel has to say what Reaper found in words the owner would use, so each
    # field carries its own phrasing rather than having the explanation glue the label
    # to a raw operator key.

    subject: str = ""
    """What a text explanation calls this field. Defaults to ``label``; set it where
    the caption will not take a verb ("On a protected list includes ...")."""

    true_phrase: str = ""
    false_phrase: str = ""
    """The two things a boolean field can report. Both are written as statements of
    the fact, because a rule that missed and a rule that matched on the opposite value
    describe the same world, and only one wording keeps a miss from reading as though
    the opposite were true."""

    value_phrase: str = ""
    """A numeric field's value in a sentence, with ``{}`` where the number goes
    ("{} on disk"). ``person|people`` picks its side from the number itself."""

    value_render: Callable[[float], str] | None = None
    """Set where the number is not the thing to show. Season rank shows a place in an
    order ("the newest season"), and printing the rank instead is how a panel ends up
    calling the newest season an old one."""

    bars: BarPhrases | None = None
    """Overrides the phrasing of the rule's own number. Defaults by field type."""

    reach_span: ReachSpan | None = None
    """Set where the value is drawn from the watch mirror and is only an answer while
    the mirror reaches far enough. Declared here, on the field, so every lane that reads
    it through this registry inherits the check rather than each remembering it."""

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
            "Counted from the last play. If it has never been played, it is counted "
            "from whichever is later: when it was added, or the start of your watch "
            "history. Never from 1970."
        ),
        type=FieldType.DAYS,
        unit_suffix="days",
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=NUMERIC_OPS,
        read=lambda f: f.days_observed_unwatched,
        value_phrase="Not watched in {}",
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
        value_phrase="{} on disk",
    ),
    FieldSpec(
        key="recent_watchers",
        label="People who watched it recently",
        help_text=(
            "How many different people have watched this within your popularity "
            "window. Windowed on purpose: on a long-lived server almost everything "
            "has been watched by someone, eventually, so an all-time count protects "
            "nearly the whole library and the rule stops meaning anything. Only a "
            "fraction of those items still have watchers in the last year, and that "
            "is the number that tells you the title is still alive."
        ),
        type=FieldType.COUNT,
        unit_suffix="people",
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=NUMERIC_OPS,
        read=lambda f: f.distinct_watchers,
        value_phrase="{} person|people watched it recently",
        reach_span=ReachSpan.POPULARITY_WINDOW,
    ),
    FieldSpec(
        key="watchers_all_time",
        label="People who have ever watched it",
        help_text=(
            "Everyone who has ever watched this. It can only be used to keep a title, "
            "never to remove one. Using it to remove things would make recent viewing "
            "count for nothing."
        ),
        type=FieldType.COUNT,
        unit_suffix="people",
        # Protect only. This is the registry doing its job: an all-time watcher count
        # is a fine reason to KEEP something and a terrible reason to delete it, and
        # the lane list is what makes the latter unconstructable.
        lanes=(Lane.PROTECT,),
        ops=NUMERIC_OPS,
        read=lambda f: f.distinct_watchers_all_time,
        value_phrase="{} person|people has|have ever watched it",
        reach_span=ReachSpan.ITEM_LIFETIME,
    ),
    FieldSpec(
        key="imdb_rating",
        label="IMDb rating",
        help_text=(
            "Always pair this with a vote floor. An 8.3 drawn from a few hundred votes "
            "is noise, not quality. Every library holds a few of them, and a rating "
            "floor on its own would keep every one of them, forever."
        ),
        type=FieldType.RATING_TENTHS,
        unit_suffix="/10",
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=NUMERIC_OPS,
        read=lambda f: f.imdb_rating_tenths,
        value_phrase="IMDb {}",
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
        value_phrase="{} vote|votes on IMDb",
    ),
    FieldSpec(
        key="season_rank",
        label="How far back the season is",
        help_text=(
            "The newest season on disk is 1, the one before it 2, and so on. Keeping the "
            "last 2 seasons means 2 or less. Counted over seasons that actually hold "
            "files, specials excluded, never from what Sonarr planned to download."
        ),
        type=FieldType.COUNT,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        media_types=("tv",),
        ops=NUMERIC_OPS,
        read=lambda f: f.season_rank,
        value_phrase="The {}",
        value_render=describe_season_rank,
        bars=_SEASON_BARS,
    ),
    FieldSpec(
        key="on_list",
        # Every list, whatever its source: the fact behind this is ``Facts.on_lists``, the
        # operator's names for every list holding the item, so a rule here is how ANY list
        # -- tag, collection, watchlist, IMDb -- comes to keep or lean. The stored rules
        # that used to spell this ``on_curated_list`` are re-spelled by
        # ``policy_migrations.convert_list_protections``.
        label="On one of your lists",
        help_text="Matches a list by the name it has on Settings → Lists.",
        type=FieldType.TEXT,
        lanes=(Lane.PROTECT,),
        ops=TEXT_OPS,
        multi=True,
        read=lambda f: f.on_lists,
        subject="List membership",
    ),
    FieldSpec(
        key="whitelisted",
        label="On a list you curate yourself",
        help_text=(
            'A tag list, a Plex collection, or your watchlist. Use "On one of your '
            'lists" to match one list by name; this is the yes/no over all of them.'
        ),
        type=FieldType.BOOL,
        lanes=(Lane.PROTECT,),
        ops=BOOL_OPS,
        read=lambda f: f.is_whitelisted,
        true_phrase="On a list you curate yourself",
        false_phrase="Not on any list you curate yourself",
    ),
    FieldSpec(
        key="streaming_now",
        label="Being watched right now",
        help_text="Re-checked in the seconds before any delete, never only at scan time.",
        type=FieldType.BOOL,
        lanes=(Lane.PROTECT,),
        ops=BOOL_OPS,
        read=lambda f: f.is_streaming_now,
        true_phrase="Someone is watching it right now",
        false_phrase="Nobody is watching it right now",
    ),
    FieldSpec(
        key="requested",
        label="Requested by a user",
        help_text=(
            "Whether someone asked for this through your requests app. If Reaper cannot "
            "tell, because the requests app is unreachable or this title could not be "
            "matched to a request, this is left unknown and never counts toward removal."
        ),
        type=FieldType.BOOL,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=BOOL_OPS,
        read=lambda f: f.requested,
        true_phrase="Someone requested this",
        false_phrase="Nobody requested this",
    ),
    FieldSpec(
        key="genre",
        label="Genre",
        help_text=(
            'The genres recorded for this title. Use "contains" to match one genre '
            "within a title that has several (for example, contains Reality)."
        ),
        type=FieldType.TEXT,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        ops=TEXT_OPS,
        multi=True,
        read=lambda f: f.genres,
    ),
    FieldSpec(
        key="release_age",
        label="Age since release",
        help_text=(
            "How long ago the title was released. Pairs well with how long it has gone "
            "unwatched: old and untouched is a stronger case than either alone."
        ),
        type=FieldType.DAYS,
        unit_suffix="days",
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        # Movie-only, because ``season_scan.build_season_facts`` has no clean per-season
        # release date and writes Absent for every season. Offering it on a TV policy sold
        # a protection that could never keep anything, and a TV *condemn* rule on it was
        # worse than inert: removal weights total exactly 100 over a fixed denominator, so
        # a rule that never fires permanently removes its points from every TV score.
        media_types=("movie",),
        ops=NUMERIC_OPS,
        read=lambda f: f.release_age_days,
        value_phrase="Released {} ago",
    ),
    FieldSpec(
        key="quality",
        label="File quality",
        help_text=(
            "The quality of the file on disk, as your library names it (for example "
            'Bluray-1080p, SDTV). Use "contains" to match a resolution: contains 2160p '
            "for 4K."
        ),
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
        label="The show has ended",
        help_text=(
            "Whether the series has finished for good. An ended show will get no new "
            "seasons to draw viewers back; a returning one still might. TV only."
        ),
        type=FieldType.BOOL,
        lanes=(Lane.CONDEMN, Lane.PROTECT),
        media_types=("tv",),
        ops=BOOL_OPS,
        read=lambda f: f.show_ended,
        true_phrase="The show has ended",
        # Known-false covers a show still airing and one that has not started yet, so
        # this says the show is not finished and claims nothing more than that.
        false_phrase="The show is still going",
    ),
)

BY_KEY: dict[str, FieldSpec] = {spec.key: spec for spec in REGISTRY}

RECENT_WATCHERS: FieldSpec = BY_KEY["recent_watchers"]
"""The windowed watcher count's spec, resolved once here rather than looked up by string.

``signals.evaluate_signal``'s built-in ``FEW_WATCHERS`` branch reads the same fact this
spec declares and has to consult the same reach bound (rule 140). Reaching it through
``BY_KEY.get("recent_watchers")`` would hand back ``None`` the day that key is renamed, and
``reach_shortfall(None, ...)`` means "no bound applies" -- the guard would switch itself off
in silence, in the condemn direction. Subscripting here raises at import instead."""


def vocabulary(lane: Lane, media_type: MediaType | None = None) -> list[FieldSpec]:
    """The fields available in one lane, optionally narrowed to one media type.

    The API calls this before serializing, so a protect-only field is never even
    offered to the condemn editor. A condemn rule referencing ``watchers_all_time``
    is not rejected -- it cannot be built. When ``media_type`` is given, a field that
    does not apply to it (``show_ended`` on a movie policy) is dropped the same way, so
    the editor only ever offers reasons that fit the policy being tuned. ``None`` keeps
    every field, for callers that are not editing one media type in particular.
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
            raise ValueError(f'Unknown field "{self.field}".') from None

    def validate_for(self, lane: Lane) -> None:
        spec = self.spec()
        if lane not in spec.lanes:
            allowed = _join_or([_LANE_HOME[x] for x in spec.lanes])
            raise ValueError(
                f'"{spec.label}" cannot be used to {_LANE_USE[lane]}. It only works as {allowed}.'
            )
        if self.op not in spec.ops:
            allowed = _join_or([f'"{_OP_NAME[o]}"' for o in spec.ops])
            raise ValueError(
                f'"{spec.label}" cannot be compared with "{_OP_NAME[self.op]}". '
                f"It works with {allowed}."
            )
        self._validate_value_type(spec)

    def _validate_value_type(self, spec: FieldSpec) -> None:
        """The typed value must match the field's type, checked at the save boundary.

        JSON keeps ``"500"`` a string on a numeric field (the wire type is
        ``int | str | bool`` and nothing coerces), so without this check the policy
        saves and hashes cleanly and the NEXT SCAN crashes inside ``score()`` /
        ``evaluate_all`` on every item. A 422 naming the field at save time is the
        honest failure. It also closes the quieter half: ``in``/``contains`` with a
        non-text value can never match, so a protection the owner believes exists
        would silently do nothing.

        An empty or whitespace-only text value is refused for the same reason, and it is
        the more dangerous of the two. ``contains ""`` is ``"" in anything`` -- True for
        every item whose text fact is Known -- so a removal rule written that way adds its
        full weight to the ENTIRE library and renders as the unfinished sentence "Genre
        contains ". A single space is the same rule wherever the text holds a space. The
        mirror on the protect lane is quiet instead of loud: ``in ""`` splits to no
        elements, so it can never match and the protection reads green forever. Both are
        refused here, at the save boundary, so a stored policy cannot carry a rule that
        matches everything or nothing.

        An ``eq`` target carrying the separator its fact is joined on is the third of the
        same shape, and the quietest: it reads as an ordinary single value everywhere it
        renders. See the check itself below.
        """
        value = self.value
        if spec.type is FieldType.BOOL:
            if not isinstance(value, bool):
                raise ValueError(f'"{spec.label}" expects true or false, got {value}.')
        elif spec.type is FieldType.TEXT:
            if not isinstance(value, str):
                raise ValueError(f'"{spec.label}" expects text, got {value}.')
            if not value.strip():
                raise ValueError(f'"{spec.label}" needs a value.')
            if self.op is Op.IN and not _split_csv(value):
                # A comma-only list ("," / " , ") survives the strip above but splits to
                # nothing, which is the same never-matches protection by another spelling.
                raise ValueError(f'"{spec.label}" needs at least one value to match.')
            if self.op is Op.EQ and spec.multi and "," in value:
                # The separator half, reached from the rule end. A multi-valued fact is one
                # comma-joined string and ``_compare`` splits it back to test membership, so
                # an ``eq`` target carrying a comma is never an element of its own fact: the
                # rule saves, hashes, and renders on Policy as a live protection covering
                # nothing. ``list_config._clean_name`` refuses the separator where the name
                # is TYPED, which is where an operator can act on it; this is the boundary a
                # hand-written body or an imported policy comes through (rule 108).
                raise ValueError(
                    f'"{spec.label}" can\'t match a value with a comma in it. '
                    "Reaper separates values with one, so pick a single one."
                )
        # Numeric field types (days, bytes, count, rating tenths). bool is an int
        # subclass in Python, so it must be rejected explicitly.
        elif isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f'"{spec.label}" expects a whole number, got {value}.')


@dataclass(frozen=True, slots=True)
class ConditionResult:
    matched: bool
    blocked: bool
    """The field was Unknown, so the condition could not be evaluated."""
    detail: str


def reach_shortfall(spec: FieldSpec | None, facts: Facts, *, window_days: int | None) -> str | None:
    """Why the watch mirror cannot support this field's value, in the operator's words.

    ``None`` when it can, and for every field the mirror does not bound. The one place
    the two watcher counts are qualified, so the protect lane, the condemn lane, the
    graded keeps and the built-in ``FEW_WATCHERS`` signal cannot drift apart (rule 140).

    ``window_days`` is the policy's popularity window -- the span
    ``distinct_watchers`` was counted over, and the one every production caller derives
    from ``policy.PolicyBody.popularity_window_days``, the same call that built the count
    (``snapshot._watch_stats``), so the two can never describe different spans. ``None``
    means the caller did not state it, and that is deliberately not a license to assume
    the shipped default: against an operator running a longer window, a truncated count
    would be read as complete, which is the exact fail-open this guards. It resolves to
    "cannot establish" instead.
    """
    if spec is None or spec.reach_span is None:
        return None
    # Matched member by member with no ``else``, so a third span cannot inherit an answer
    # computed for a different one. It used to: ``ITEM_LIFETIME`` sat on the fallback arm,
    # so a new member silently measured the mirror against the item's AGE. Driven on a
    # local third member -- reach 90d, item age 30d, window 365d -- it answered ``None``,
    # "no shortfall", because the mirror does span that item's life. That is the permissive
    # direction on a helper whose whole job is withholding unsupported counts, and no test
    # went red (issue #168). ``assert_never`` makes mypy the guard rule 103 asks for, and
    # ``TestEveryReachSpanIsRoutedByName`` names both sites for an author who skips it.
    match spec.reach_span:
        case ReachSpan.POPULARITY_WINDOW:
            if window_days is None:
                return "this scan did not record the window the count covers"
            return history_shortfall(facts.history_reach_days, float(window_days))
        case ReachSpan.ITEM_LIFETIME:
            # Through the shared helper, which the season path's keep-rule conflict
            # detector also asks -- it compares two all-time counts and reads no ``Facts``,
            # so the span it needs must not be restated there (rules 104, 140).
            return lifetime_shortfall(facts.history_reach_days, facts.days_since_added)
        case _:
            assert_never(spec.reach_span)


def _survives_more_history(op: Op, *, matched: bool) -> bool:
    """Would this outcome still hold once the plays the mirror cannot see arrived?

    A count drawn from a mirror that does not reach far enough is a LOWER BOUND, and the
    history it is missing can only ever RAISE it. Two of the four outcomes are therefore
    already earned and need no reach at all: "at least N" that the truncated count
    *already* clears stays cleared however much more history arrives, and "at most N"
    that it *already* exceeds stays exceeded. The other two are the ones a deeper mirror
    could overturn, and they are exactly the dangerous pair -- an unmatched ``gte``
    withdraws a protection ("nobody has ever watched it"), and a matched ``lte`` lands a
    removal rule's full weight ("nobody watched it recently").

    Only ``NUMERIC_OPS`` reach here, since only numeric specs carry a ``reach_span``;
    anything else is treated as overturnable, which is the keep direction.
    """
    match op:
        case Op.GTE:
            return matched
        case Op.LTE:
            return not matched
        case _:  # pragma: no cover -- no text or bool spec is reach-bounded
            return False


def can_add_pressure_under_a_shortfall(op: Op) -> bool:
    """Whether a BOOLEAN rule under this operator can still land its weight on any item.

    A boolean rule is unsigned and all-or-nothing: it adds its full weight when it matches
    and nothing when it does not. So if a MATCH is the outcome :func:`evaluate` blocks, the
    rule contributes zero pressure to every item at once for as long as the shortfall
    stands -- blocked where it matched, and honestly zero where it did not.

    That is what makes the weight part of the score CEILING rather than a per-item
    discount, and it is exactly the discrimination ``policy_warnings.inspect``'s condemn lane needs:
    under ``gte`` a matched item still earns the weight, so the list is not empty, while
    under ``lte`` no item can earn it at all. Asked here rather than restated at the caller,
    so there is one derivation of what a short mirror does to an outcome (rule 104).
    """
    return _survives_more_history(op, matched=True)


def evaluate(
    condition: Condition, facts: Facts, *, window_days: int | None = None
) -> ConditionResult:
    """Evaluate one condition against one item.

    An ``Unknown`` never matches, and says so. In the protect lane that means the
    protection does not fire but is reported as *blocked* -- amber, not green -- so
    "we could not check" is visibly different from "we checked and it was fine".

    A ``Known`` value the evidence cannot actually support reads the same way. See
    ``reach_shortfall``: a watcher count from a mirror that does not reach far enough is
    a lower bound, so the outcomes it could still overturn are blocked rather than
    reported as checked. ``window_days`` defaults to ``None`` -- "not stated" -- because
    guessing it is the fail-open direction.
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
    try:
        matched = _compare(condition.op, value, target, multi=spec.multi)
        detail = _explain(spec, condition.op, value, target, matched=matched)
    except ValueError as exc:
        # Belt and suspenders under validate_for's boundary check: a stored rule whose
        # value cannot be compared against this field (saved before the type check
        # existed, or edited by hand) degrades THIS item as blocked -- amber, "could not
        # check" -- instead of raising out of score()/evaluate_all and aborting the
        # whole scan. Blocked fails safe in both lanes: a protect condition blocks the
        # verdict to abstain, a condemn rule adds no pressure and keeps its weight in
        # the denominator.
        return ConditionResult(
            matched=False,
            blocked=True,
            detail=f"could not check {spec.label.lower()}: {exc}",
        )

    # A Known value the evidence cannot actually carry. Checked AFTER the comparison
    # because only the outcome says whether the reach matters: the two earned outcomes
    # above are true of any deeper mirror, and blocking them would withhold a protection
    # the operator's own rule did fire (and, on the condemn lane, drop pressure that was
    # honestly measured). The other two are reported as unchecked, in the same amber
    # "could not check" shape as an Unknown input, prefix included -- ``api.review._chip``
    # and ``WhyPanel`` both read it.
    if not _survives_more_history(condition.op, matched=matched) and (
        (short := reach_shortfall(spec, facts, window_days=window_days)) is not None
    ):
        return ConditionResult(
            matched=False,
            blocked=True,
            detail=f"could not check {spec.label.lower()}: {short}",
        )

    return ConditionResult(matched=matched, blocked=False, detail=detail)


def _split_csv(text: str) -> list[str]:
    """Comma-separated elements, trimmed and casefolded -- how both sides of ``in``
    (and the multi-valued side of ``eq``) are read."""
    return [part.casefold() for part in _split_raw(text)]


def _split_raw(text: str) -> list[str]:
    """The same split, keeping the spelling. Explanations quote a genre back the way
    the library spells it, not the way the comparison folded it."""
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
                # matches when ANY of its elements equals the target.
                if multi:
                    return fold(target) in _split_csv(value)
                return fold(value) == fold(target)
            return bool(value == target)
        case Op.IN:
            if not isinstance(target, str):
                return False  # rejected at save by validate_for; a stored one cannot match
            targets = set(_split_csv(target))
            # Trimmed and casefolded on BOTH sides, or the space after a comma in
            # "Anime, Documentary" (and Plex's casing) makes the list silently match
            # nothing. A multi-valued fact matches on any shared element.
            if multi and isinstance(value, str):
                return not targets.isdisjoint(_split_csv(value))
            return fold(str(value)) in targets
        case Op.CONTAINS:
            # Folded on both sides, the same as eq and in above. `.lower()` left the
            # target's leading and trailing spaces in and case-folded only ASCII, so
            # `on_list contains "Kids "` stopped matching the list named `Kids` and
            # `contains "STRASSE"` stopped matching `Straße`. `on_list` is protect-only,
            # so a rule that stops matching leaves the item in the condemn lane while
            # still reading as a live protection on the panel.
            return isinstance(target, str) and fold(target) in fold(str(value))


def _num(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    raise ValueError(f'"{value}" is not a number.')


def _render(spec: FieldSpec, value: object) -> str:
    """Human units. A bare number is how a rating floor meets a Tomatometer."""
    match spec.type:
        case FieldType.RATING_TENTHS:
            return f"{_num(value) / 10:.1f}"
        case FieldType.BYTES:
            return f"{_num(value) / 1_000_000_000:.1f} GB"
        case FieldType.DAYS:
            # The same spelling the built-in signals use. One panel showing "900 days"
            # beside "2 years, 5 months" reads as two different measurements. This is
            # for a *measured* value only; a rule's own number goes through
            # :func:`_render_bar`, which says why.
            return humanize_days(_num(value))
        case FieldType.COUNT:
            return f"{_num(value):,.0f}"
        case _:
            return str(value)


def _render_bar(spec: FieldSpec, target: object) -> str:
    """A rule's own number, in the units the owner typed it in.

    Deliberately not :func:`_render`. Humanizing a measured day count saves the reader
    dividing 900 by 365, but humanizing the rule's number too rounds both sides into
    the same phrase: :func:`reaper.clock.humanize_days` keeps two units and buckets
    months in 30-day steps, so a rule at 400 days against a title at 396 would read
    "Not watched in 1 year, 1 month, within your 1 year, 1 month" -- a line claiming
    the value sits under a number it prints as equal to itself, on exactly the
    marginal titles someone checks hardest before approving a deletion.

    The editor already shows this field with "days" beside the box
    (``FieldSpec.unit_suffix``), so echoing the typed number back is what they expect.
    """
    if spec.type is FieldType.DAYS:
        days = _num(target)
        return f"{days:,.0f} day" if abs(days) == 1 else f"{days:,.0f} days"
    return _render(spec, target)


# ---------------------------------------------------------------------------
# Explaining a condition
# ---------------------------------------------------------------------------
#
# Every explanation states what Reaper found first and what the rule asked for
# second. The owner already knows what they asked for; what they opened the panel
# for is what this title actually is. Both readings have to be true English: an
# unmatched condition is quoted in the why-panel just as often as a matched one,
# under "Your rule didn't match: ...".


def _explain(
    spec: FieldSpec, op: Op, value: object, target: int | str | bool, *, matched: bool
) -> str:
    match spec.type:
        case FieldType.BOOL:
            # The fact, never the comparison. "eq false" that matched and "eq true"
            # that missed describe the same world, and stating the fact is the only
            # wording where a miss cannot be read as the opposite being true.
            return spec.true_phrase if value else spec.false_phrase
        case FieldType.TEXT:
            return _explain_text(spec, op, value, target, matched=matched)
        case _:
            return _explain_number(spec, op, value, target, matched=matched)


def _explain_text(
    spec: FieldSpec, op: Op, value: object, target: int | str | bool, *, matched: bool
) -> str:
    subject = spec.subject or spec.label
    wanted = str(target).strip()
    match op:
        case Op.CONTAINS:
            # A substring test, over the whole value even where it is a list.
            if matched:
                return f"{subject} contains {wanted}"
            return f"{subject} does not contain {wanted}"
        case Op.EQ if spec.multi:
            # eq on a list is per element, so it is an "includes", not an "is".
            if matched:
                return f"{subject} includes {wanted}"
            return f"{subject} does not include {wanted}"
        case Op.EQ:
            if matched:
                return f"{subject} is {value}"
            return f"{subject} is {value}, not {wanted}"
        case Op.IN if spec.multi:
            if not matched:
                return f"{subject} is none of {_listed(wanted)}"
            # Name what actually matched. Printing the whole list the rule offered
            # leaves the owner to work out which part of it fired.
            return f"{subject} includes {_shared(str(value), wanted) or _listed(wanted)}"
        case _:
            # Matched names what matched; missed names what the rule wanted. Repeating
            # the whole list back on a match leaves the owner to spot which part fired,
            # and the value is already the answer.
            if matched:
                return f"{subject} is {value}"
            return f"{subject} is {value}, not one of {_listed(wanted)}"


def _explain_number(
    spec: FieldSpec, op: Op, value: object, target: int | str | bool, *, matched: bool
) -> str:
    number = _num(value)
    shown = spec.value_render(number) if spec.value_render else _render(spec, value)
    phrase = (
        spec.value_phrase.format(shown)
        if spec.value_phrase
        else f"{spec.subject or spec.label}: {shown}"
    )
    phrase = _plural(phrase, number)

    bars = spec.bars or _BARS.get(spec.type, _DEFAULT_BARS)
    match op:
        case Op.GTE:
            bar = bars.gte_met if matched else bars.gte_missed
        case Op.LTE:
            bar = bars.lte_met if matched else bars.lte_missed
        case _:  # pragma: no cover -- a numeric field accepts no other operator
            # Nothing to say about a bar we have no phrasing for, so claim nothing.
            return phrase
    return f"{phrase}, {bar.format(_render_bar(spec, target))}"


def _listed(target: str) -> str:
    """A rule's list, spaced evenly however the owner typed it."""
    return ", ".join(_split_raw(target))


def _shared(value: str, target: str) -> str:
    """The elements a list-valued fact and a rule's list have in common, spelled the
    way the library spells them."""
    wanted = set(_split_csv(target))
    return ", ".join(part for part in _split_raw(value) if part.casefold() in wanted)


_ALTERNATIVES = re.compile(r"(\w+)\|(\w+)")


def _plural(phrase: str, count: float) -> str:
    """Resolve ``person|people`` alternatives in a phrase against the number in it.

    Cheaper than a phrase per field, and it keeps "1 person watched it" out of the
    "1 people" territory that makes an explanation look machine-written.
    """
    singular = abs(count) == 1
    return _ALTERNATIVES.sub(lambda m: m.group(1) if singular else m.group(2), phrase)


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

    window_days: int | None = None
    """The policy's popularity window, so a rule on a windowed watcher count can tell
    whether the mirror covered it (``reach_shortfall``). Built from the policy in
    ``services.scan_runner.build_gates``; ``None`` reads as un-establishable, which
    blocks rather than assumes."""

    def evaluate(self, facts: Facts) -> GateResult:
        result = evaluate(self.condition, facts, window_days=self.window_days)
        if result.blocked:
            return GateResult(self.id, ABSTAIN, blocked=True, detail=result.detail)
        if result.matched:
            return GateResult(self.id, PROTECT, detail=f"your rule: {result.detail}")
        return GateResult(self.id, ABSTAIN, detail=f"checked your rule: {result.detail}")
