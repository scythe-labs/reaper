# SPDX-License-Identifier: AGPL-3.0-or-later
"""Policy -- the user's configuration, and the hash that pins it.

Three ideas, each closing a specific way these tools destroy data.

## 1. Integers only

Every number in a policy body is an integer: a rating floor is stored in *tenths*
(``75``, not ``7.5``), sizes in bytes, times in epoch seconds, percentages in
basis points. Not fussiness -- floats do not canonicalise. ``0.1 + 0.2`` is not
``0.3``, and ``json.dumps`` of a float is platform- and version-dependent, so a
hash over a policy containing floats is not stable. Since an approval is bound to
a policy hash, an unstable hash means approvals silently void themselves, or worse,
silently *don't*.

It also kills a class of unit bug at the boundary: typing ``75`` into a field
labeled "IMDb floor (tenths)" is a 422, not a policy that protects nothing because
7.5 was compared against a Tomatometer of 96.

## 2. The hash covers meaning, not identity

``policy_hash`` is the sha256 of the canonical JSON of the *semantic* fields, plus
``schema_version`` and ``scorer_version``. It deliberately **excludes** ``id``,
``name`` and ``created_at``: renaming a policy must not void every pending approval,
but changing a threshold must.

## 3. Nothing that is "off" is spelled as zero or blank

``0`` never means disabled and blank never means unlimited. Both idioms are how a
half-finished config becomes an unbounded deletion: Janitorr's
``movie-expiration: {100: 10d}`` was read by its author as "when 100% full" and by
the code as "always". Every switch is an explicit ``enabled: false``, and every
protective number has a floor (``min_votes >= 1``, ``grace_days >= 7``).
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Annotated, Any, ClassVar, Literal, Self, assert_never

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from reaper.clock import humanize_days, humanize_window
from reaper.engine.fields import (
    BY_KEY,
    Condition,
    Lane,
    Op,
    ReachSpan,
    can_add_pressure_under_a_shortfall,
)
from reaper.engine.gates import (
    GateId,
    RatingRule,
    history_shortfall,
    progress_is_establishable,
)
from reaper.engine.observation import Known
from reaper.engine.signals import MAX_SCORE, CustomSignalConfig, KeepConfig, SignalId
from reaper.engine.verdict import decide_verdict
from reaper.ratings import RatingSource, is_percentage_source, source_label

SCHEMA_VERSION = 3
"""Bumped when the stored SHAPE changes. 3 marks bodies written after the rating bar moved
off the RATING_FLOOR gate row into ``keep_rating_rules`` (see ``recover_rating_rules``,
which backfills a body written before it)."""

SCORER_VERSION = 2
"""Bumped when the SCORER changes meaning, not when the schema gains a field.
Both are inside the policy hash: an item scored under a different scorer was not
approved under this one.

Deliberately plain ``int`` and not ``Literal``. Pinning the field to a single literal
means the *next* bump makes every stored body fail ``model_validate_json``, and the one
caller that reads them (``services.profiles.active_policy``) has no fallback -- so the
bump would take out the scan path and the policy editor together, including the page an
operator would use to fix it. Bodies from a NEWER Reaper are still refused, below: those
we genuinely cannot interpret."""


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GateSetting(Frozen):
    """One protection, as the user configures it."""

    gate: GateId
    enabled: bool = True

    threshold: int = 0
    secondary: int = 0

    window_days: int = Field(default=365, ge=1)
    """How far back "recently" reaches, for gates that count activity.

    There is no way to spell "all time", and that is deliberate. An unwindowed
    popularity gate protects anything anyone has *ever* played, and on a long-lived
    server that is very nearly the whole library -- only a fraction of those titles
    still have watchers in the last year. Measured against real history, the
    unwindowed version silently disabled the entire scorer: a backtest at every
    threshold then finds almost nothing to delete, and the tool looks "safe" while
    actually being broken.
    """

    @model_validator(mode="after")
    def _protective_floors(self) -> Self:
        """A protection with a nonsensical bound is a protection that does not fire.

        The rating gate is the one that bites: a vote floor of 0 protects an 8.3
        from 388 votes, and a rating floor of 0 protects literally everything --
        which sounds safe until the user wonders why Reaper never finds anything and
        "fixes" it by disabling the gate entirely.
        """
        if not self.enabled:
            return self

        # The rating floor no longer lives here: it is a set of per-source bars
        # (``PolicyBody.keep_rating_rules``), each validated by ``RatingRuleSpec``. The
        # RATING_FLOOR gate setting is now only the on/off switch, so it carries no
        # threshold of its own to police.
        if self.gate is GateId.SERVER_POPULARITY and self.threshold < 1:
            raise ValueError(
                "Keeping anything watched by 0 people would protect your whole library. "
                "Set it to at least 1, or switch this protection off instead."
            )
        if self.gate is GateId.MIN_DORMANCY and self.threshold < 5:
            raise ValueError(
                "Give titles at least 5 days before removing them. To remove things faster "
                "than that, switch this protection off with its toggle rather than setting "
                "it this low."
            )
        return self


class SignalSetting(Frozen):
    """One weighted reason to delete."""

    signal: SignalId
    weight: int = Field(ge=0, le=100)
    """0 disables the signal, and removes it from the denominator so the remaining
    scores do not silently inflate."""

    saturate_at: int = Field(ge=1)
    floor: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _floor_below_saturation(self) -> Self:
        if self.floor >= self.saturate_at:
            raise ValueError(
                f"floor ({self.floor}) must be below saturate_at ({self.saturate_at}), "
                "or the signal is either always off or always at full pressure."
            )
        return self


class ConditionSpec(Frozen):
    """One user-authored protection: keep a title when ``<field> <op> <value>``.

    Protect-only *by construction*. It is validated against the PROTECT lane of the field
    registry, so the worst a mis-authored condition can do is fail to keep something -- it can
    never mark a title for removal. That asymmetry is the whole reason these are safe to hand
    to the owner (see ``engine.fields``).
    """

    field: str
    op: Op
    value: int | str | bool

    @model_validator(mode="after")
    def _valid_protect_condition(self) -> Self:
        # Reuse the registry's own validation: an unknown field, a condemn-only field, or an
        # operator the field does not accept all raise here, with the message the API shows.
        self.to_condition().validate_for(Lane.PROTECT)
        return self

    def to_condition(self) -> Condition:
        return Condition(field=self.field, op=self.op, value=self.value)


class BooleanCondemnSpec(Frozen):
    """A user-authored reason to remove: when the condition matches, add the full weight.

    Validated against the CONDEMN lane of the field registry, so a protect-only field is
    unconstructable and the worst a mis-authored rule can do is fail to add pressure -- it
    can never protect (that is the gate lane's job). Unsigned, like every condemnation
    signal: an ``Unknown`` input adds nothing."""

    kind: Literal["boolean"] = "boolean"
    name: str = Field(min_length=1, max_length=60)
    field: str
    op: Op
    value: int | str | bool
    weight: int = Field(ge=0, le=100)
    """0 disables the rule and removes its weight from the denominator, like a built-in signal."""

    @model_validator(mode="after")
    def _valid_condemn_condition(self) -> Self:
        # Reuse the registry's own validation (rule #3): unknown field, protect-only field,
        # or an operator the field does not accept all raise here, with the API's message.
        Condition(field=self.field, op=self.op, value=self.value).validate_for(Lane.CONDEMN)
        return self


class GradedCondemnSpec(Frozen):
    """A user-authored reason to remove that ramps a numeric field, like a built-in signal.

    The field must be numeric and usable in the CONDEMN lane; pressure rises linearly from
    ``floor`` to ``saturate_at`` and is capped at ``weight``."""

    kind: Literal["graded"] = "graded"
    name: str = Field(min_length=1, max_length=60)
    field: str
    weight: int = Field(ge=0, le=100)
    saturate_at: int = Field(ge=1)
    floor: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _valid_graded(self) -> Self:
        if self.floor >= self.saturate_at:
            raise ValueError(
                f"floor ({self.floor}) must be below saturate_at ({self.saturate_at}), "
                "or the rule is either always off or always at full pressure."
            )
        spec = BY_KEY.get(self.field)
        if spec is None:
            raise ValueError(f'Unknown field "{self.field}".')
        if Lane.CONDEMN not in spec.lanes:
            raise ValueError(f'"{spec.label}" cannot be used to remove things.')
        if Op.GTE not in spec.ops:
            raise ValueError(
                f'"{spec.label}" is not a number, so it cannot be graded. Use a yes/no rule.'
            )
        return self


#: A custom condemnation rule, discriminated on ``kind`` so a policy can carry a mix.
CustomCondemnSpec = Annotated[BooleanCondemnSpec | GradedCondemnSpec, Field(discriminator="kind")]


class GradedKeepSpec(Frozen):
    """A user-authored graded "lean toward keeping": lowers the score, never vetoes.

    A numeric field, ramped ``floor`` -> ``saturate_at``, whose discount is in score
    POINTS (not a share). Fail-closed: a value we cannot read keeps the title fully. It can
    only ever LOWER a score, and can never un-protect a title a gate protected -- the verdict
    checks protection before it ever reads the score. Any numeric field may drive a keep,
    including protect-only ones like all-time watchers or vote count, which is the point."""

    name: str = Field(min_length=1, max_length=60)
    field: str
    max_discount: int = Field(ge=1, le=100)
    """Points to subtract at full strength. ``ge=1`` -- "off" is expressed by omitting the rule."""
    floor: int = Field(ge=0)
    saturate_at: int = Field(ge=1)
    direction: Literal["high_keeps", "low_keeps"] = "high_keeps"
    """Which end of the ramp keeps: a high all-time-watcher count keeps (``high_keeps``); a
    low value keeps (``low_keeps``)."""

    @model_validator(mode="after")
    def _valid_keep(self) -> Self:
        if self.floor >= self.saturate_at:
            raise ValueError(
                f"floor ({self.floor}) must be below saturate_at ({self.saturate_at})."
            )
        spec = BY_KEY.get(self.field)
        if spec is None:
            raise ValueError(f'Unknown field "{self.field}".')
        if Op.GTE not in spec.ops:
            raise ValueError(
                f'"{spec.label}" is not a number, so it cannot be graded. Use a protection instead.'
            )
        return self


class RatingRuleSpec(Frozen):
    """One "keep it if it clears this bar" rule, for one rating source.

    Integers only, like every policy number, so the hash stays byte-stable. ``floor`` is
    in tenths (7.5 -> 75) and reads identically for a percentage source (75% -> 75), since
    Plex normalizes an 84% score to 8.4 whose tenths are 84. ``min_votes`` only means
    something on the sources that count votes (IMDb, TMDb); for a percentage source
    (Rotten Tomatoes, Metacritic) it is required to be 0, because a vote floor there would
    silently do nothing.
    """

    source: RatingSource
    floor: int = Field(ge=1, le=100)
    min_votes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _vote_floor_matches_the_source(self) -> Self:
        if is_percentage_source(self.source):
            if self.min_votes != 0:
                raise ValueError(
                    f"{source_label(self.source)} is a percentage with no vote count, so a "
                    "vote floor on it would do nothing. Leave the votes at 0 for this source."
                )
        elif self.min_votes < 1:
            # A rating floor with no vote floor protects an 8.3 drawn from a few hundred
            # votes -- a number that means nothing. The same refusal the single-source gate
            # carried, now per source that actually counts votes.
            raise ValueError(
                f"A vote floor of 0 makes the {source_label(self.source)} bar meaningless: it "
                "would protect a high score drawn from a handful of votes. Use at least 1 "
                "(1000 is a sensible default)."
            )
        return self


class PolicyBody(Frozen):
    """The hashed, immutable part of a policy.

    Everything here changes what Reaper would *decide*. Anything that changes only
    how much it may *do* (caps) or how long a flagged title is shown as leaving (grace)
    lives on the Profile, so that tightening a cap does not void every pending approval.
    """

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1, le=SCHEMA_VERSION)

    scorer_version: int = Field(default=SCORER_VERSION, ge=1, le=SCORER_VERSION)
    """Which scorer produced the numbers under this policy. See ``_pin_to_the_running_scorer``
    for why it is not simply whatever the row said."""

    @model_validator(mode="after")
    def _pin_to_the_running_scorer(self) -> Self:
        """This field tracks the CODE, not the row. Load it, then overwrite it.

        ``le=SCORER_VERSION`` above still does the work it was written for: a body from a
        newer Reaper is refused, because we genuinely cannot interpret it. But a body from an
        OLDER one loaded back as whatever it stored, which made the version a fossil of the
        save rather than a statement about the scorer now running -- so ``policy_hash()`` was
        byte-identical either side of a bump, ``live_policy_hash`` still matched an approval
        made under the superseded scorer, and the executor deleted files on its numbers with
        no re-scan refusal (rule 113). Two snapshots scored by different scorers hashed the
        same, so the journal could not tell them apart either.

        Pinning it here makes the docstring on ``SCORER_VERSION`` true: a bump moves every
        policy's hash, which voids pending approvals and any autonomy grant keyed to it, and
        routes the simulator to a frozen-facts replay rather than to stale stored scores
        (``scoring_hash``). The operator re-scans, which is the point.

        Written with ``object.__setattr__`` rather than by returning a modified copy because
        this model is frozen and a top-level "after" validator that returns anything but
        ``self`` is silently ignored on the ``__init__`` path -- which would leave the pin
        holding for a body loaded from the database and not for one built in code, the exact
        half-fix this rule exists to prevent.
        """
        if self.scorer_version != SCORER_VERSION:
            object.__setattr__(self, "scorer_version", SCORER_VERSION)
        return self

    #: Gates that shipped, could not fire, and were retired. Their ``GateId`` stays alive so a
    #: stored explanation written while they existed still decodes (``engine.gates`` records
    #: what each retirement would need to come back), but a policy BODY may no longer carry
    #: one. Retiring a gate without this leaves every stored policy naming a protection
    #: ``scan_runner.build_gates`` has no implementation for, and it refuses to scan rather
    #: than silently skip -- correctly, which is exactly why the body has to be cleaned first.
    #:
    #: **Every** retired id belongs here, not just the one whose retirement prompted the
    #: mechanism. ``OTHERS_WATCHING`` was retired earlier and was left out of the first
    #: version of this set: it never shipped in a default policy, so nothing in the UI could
    #: store one, but ``GateSettingIn.gate`` is typed as a bare ``GateId`` and accepts it, and
    #: a body that got one would take that install's scans offline permanently with no
    #: self-heal -- the exact failure this set exists to prevent (rule 72).
    #: ``tests/test_policy.py`` pins the membership so a future retirement cannot forget it
    #: (rule 103).
    RETIRED_GATES: ClassVar[frozenset[GateId]] = frozenset(
        {GateId.UNMANAGED, GateId.OTHERS_WATCHING}
    )

    @model_validator(mode="after")
    def _drop_retired_gates(self) -> Self:
        """Silently drop a retired gate from a body that still names one.

        Deliberately NOT flagged as a repair, and deliberately does not degrade the scan, so
        this is the exception to rule 105 rather than a violation of it. That rule degrades
        because a shim restoring a protection-bearing field proves something WAS unprotected
        and the operator needs to know. Here the opposite is true: a retired gate is one that
        could never keep a file, so dropping it withdraws nothing and no scan run under it was
        untrustworthy. Degrading on this would make the first scan after an upgrade
        un-plannable for every install, over a protection that was never doing anything.

        It moves all three hashes, not just ``policy_hash``. ``policy_hash`` voids a plan
        approved before the upgrade and asks for a re-scan (rule 113). ``scoring_hash`` and
        ``evidence_hash`` move too, because ``gates`` is excluded from neither
        ``_POST_SCORE_FIELDS`` nor ``_EVIDENCE_REPLAYABLE_FIELDS`` -- so the policy simulator
        misses both its exact tier and its frozen-facts replay tier and withholds every number
        until that re-scan. That is the honest outcome: the stored policy really is not the one
        now in force, even though every verdict it produces is identical.

        What it must NOT do is let a surface blame the operator for it. The simulator's stale
        notice once opened "You changed what the scan reads" at an install that had changed
        nothing; it now states the condition instead (``PolicySimulator.tsx``, and the matching
        ``stale_reason`` in ``api.routes``). No post-upgrade code can reproduce the old hash,
        so the copy is the only thing that can carry the truth here.
        """
        kept = tuple(g for g in self.gates if g.gate not in self.RETIRED_GATES)
        if len(kept) != len(self.gates):
            object.__setattr__(self, "gates", kept)
        return self

    media_type: Literal["movie", "tv"] = "movie"

    condemn_at: int = Field(ge=1, le=100)
    """Score at or above which an item is a candidate. Never 0: a threshold of 0
    condemns everything the gates do not save."""

    coverage_floor_bp: int = Field(default=5000, ge=0, le=10_000)
    """Minimum share of signal weight we must actually have evaluated, in basis
    points (5000 = 50%). Below it the item abstains rather than being judged on
    fragments. Guards against condemning an item we can barely see."""

    keep_last_seasons: int = Field(default=2, ge=0)
    """Season pruning: the N most recent seasons of a show are protected outright,
    whatever they score. Movies ignore this. A hard floor, not a weight -- ``0`` means
    "keep no season on age alone" (the other guards and the score still apply); it does
    NOT mean "unlimited", which is the Janitorr footgun the whole policy module avoids.
    See ``services.season_pruning``."""

    keep_first_season: bool = True
    """Season pruning: protect the first content-bearing season of every show, so a
    library never throws away the pilot that lets a new viewer start the show. On by
    default; movies ignore it."""

    keep_last_scope: Literal["all", "requested"] = "all"
    """Whether the keep-last-N floor applies to every show (``all``) or only to shows someone
    requested (``requested``). Fail-closed: under ``requested``, when we cannot tell whether a
    show was requested, the floor still applies -- Unknown counts as "might be requested"."""

    season_lookahead: int = Field(default=0, ge=0)
    """How many seasons BEYOND a viewer's current position to also protect while they binge.
    ``0`` protects exactly the season they are mid-way through, or the next one if they have
    finished the current. Replaces the old hardcoded look-ahead. Movies ignore it."""

    keep_in_progress: bool = True
    """Season pruning: protect the season a viewer is partway through (and the next one,
    once they finish it) -- the sequential-progression guard in ``services.season_pruning``.
    On by default; turning it off removes that guard entirely. Movies ignore it."""

    in_progress_hold_days: int = Field(default=180, ge=0)
    """How long a viewer's place in a show is held after their last watch of that show.
    Past this many days without watching, the show counts as abandoned by that viewer and
    their half-finished season no longer protects anything. ``0`` holds forever. A viewer
    whose last-watched time cannot be read keeps their hold (fail closed). Only meaningful
    while ``keep_in_progress`` is on; movies ignore it.

    This is a span the guard *claims to cover*, not a bound on the watch mirror, so setting
    it past how far the mirror reaches (``0`` included, which no finite mirror can cover)
    makes the claim unsupportable: ``gates.progress_is_establishable``
    then holds every season on disk rather than letting an unseeable viewer read as an
    absent one."""

    keep_specials: bool = True
    """Season pruning: never remove specials (Season 0). On by default. When off, specials
    are judged like any other season -- they can be condemned by score -- but they still
    never occupy a keep-last slot and the airing/still-downloading guards still apply."""

    protect_incomplete_seasons: bool = True
    """Season pruning: keep a season Sonarr has not finished downloading (it wants an aired
    episode it does not have yet), so a removal never fights an in-progress download. On by
    default. When off, a partly-downloaded season is judged like any other -- useful for an
    ended show that Sonarr permanently lists as missing an episode. The airing guard is
    separate and still applies. Movies ignore it. See ``services.season_pruning``."""

    flag_keep_conflicts: bool = True
    """Season pruning: when a season the keep rule would remove was watched by more people
    than a season it keeps, block it as "Needs a look" instead of removing it. On by
    default. When off, the keep rule is followed without flagging."""

    gates: tuple[GateSetting, ...]
    signals: tuple[SignalSetting, ...]

    protect_conditions: tuple[ConditionSpec, ...] = ()
    """The owner's own protections, on top of the built-in gates. Each keeps a title when it
    matches; together they are an OR (any one is enough). Protect-only -- see ConditionSpec."""

    custom_condemn: tuple[CustomCondemnSpec, ...] = ()
    """The owner's own reasons to REMOVE, on top of the built-in signals. Each is an unsigned
    signal (boolean bonus or graded ramp) that joins the same fixed denominator, so a missing
    input can only lower the score. Never a protection -- see BooleanCondemnSpec."""

    graded_keeps: tuple[GradedKeepSpec, ...] = ()
    """The owner's own graded reasons to KEEP -- a subtractive discount applied after the
    score, fail-closed. A softer companion to a hard protect condition; it lowers a score
    but never vetoes, and missing data keeps the file. See GradedKeepSpec."""

    keep_tags: tuple[str, ...] = ("reaper-keep",)
    """The *arr tags that spare a title outright -- the configurable form of "honor your keep
    list". A title carrying one of these (or all of them, per ``keep_tags_match``) is kept
    whatever it scores. Read at scan time and synced into the whitelist before scoring. Movies
    read Radarr tags, TV reads Sonarr tags, so the two policies carry their own."""

    keep_tags_match: Literal["any", "all"] = "any"
    """Whether a title needs ANY of ``keep_tags`` (the usual case) or ALL of them to be kept."""

    keep_rating_rules: tuple[RatingRuleSpec, ...] = ()
    """The per-source bars behind "Keep well-rated titles" (the RATING_FLOOR gate). A title
    clearing ANY of them (or ALL, per ``keep_rating_match``) is kept whatever it scores. Empty
    means the protection keeps nothing, exactly like an empty keep-tag list. Movies can back
    every source (Radarr carries them); TV backs IMDb plus whatever Plex serves for the show."""

    keep_rating_match: Literal["any", "all"] = "any"
    """Whether a title needs to clear ANY rating bar (the usual case) or ALL of them."""

    @model_validator(mode="after")
    def _weights_total_one_hundred(self) -> Self:
        """Every removal weight, built-in and operator-authored, sums to exactly 100.

        This is what makes a weight mean points. ``signals.score`` normalizes by the sum
        of enabled weights, so a weight is a *share* of a running total: at a total of
        140 a rule written as 20 delivers about 14, and adding a second rule shrinks the
        first. Pin the total at ``MAX_SCORE`` and ``100 * P / D`` collapses to ``P``, so
        the number an operator types is the number the score moves by, and it matches the
        keep lane, whose discounts were always literal points.

        Equality, not ``<=``. Under-allocating renormalizes just as badly in the other
        direction: at 75 the lane is stretched, every label goes back to lying, and one
        outage touching both lanes can net *upward* because keeps stay absolute while the
        condemn side is attenuated.

        The arithmetic is unchanged, and both shipped defaults already total exactly 100,
        so this moves no score. It closes one real hole as a side effect: setting a signal
        to 0 used to drop it from the denominator and inflate every remaining signal (see
        ``SignalConfig.weight``). Now its points must go somewhere, so the denominator
        cannot move at all.
        """
        total = sum(s.weight for s in self.signals) + sum(c.weight for c in self.custom_condemn)
        if total != MAX_SCORE:
            over = total - MAX_SCORE
            fix = f"Take {over} away" if over > 0 else f"Give out the other {-over}"
            raise ValueError(
                f"Your rules add up to {total} points. {fix} before saving. "
                f"Removal points always total {MAX_SCORE}, so each one is worth "
                "the same wherever you spend it."
            )
        return self

    # An `_at_least_one_signal` validator lived here, refusing an all-zero policy. A total
    # of exactly 100 cannot be reached with every weight at 0, so it became unreachable
    # the moment the rule above landed, and unreachable safety code is deleted rather than
    # kept for reassurance. If the total is ever relaxed, restore it in the same change.

    @model_validator(mode="after")
    def _no_duplicates(self) -> Self:
        if len({g.gate for g in self.gates}) != len(self.gates):
            raise ValueError("A gate is configured twice; the second would silently win.")
        if len({s.signal for s in self.signals}) != len(self.signals):
            raise ValueError("A signal is configured twice; the second would silently win.")
        names = [c.name for c in self.custom_condemn]
        if len(set(names)) != len(names):
            raise ValueError(
                "Two custom rules share a name; the second would silently double-count."
            )
        # A custom rule may not take a built-in signal's id as its name. The stored score
        # breakdown identifies rows by that string, so a rule named "unwatched" would
        # collide with the built-in row: the why-panel would drop its "Your rule" tag and
        # render two rows under one key, and the audit record could not tell them apart.
        builtin = {s.value for s in SignalId}
        for name in names:
            if name in builtin:
                raise ValueError(
                    f'"{name}" is the name of a built-in signal. Give your rule a '
                    "different name so the score breakdown cannot confuse the two."
                )
        keep_names = [k.name for k in self.graded_keeps]
        if len(set(keep_names)) != len(keep_names):
            raise ValueError("Two keep rules share a name; the second would silently double-count.")
        rating_sources = [r.source for r in self.keep_rating_rules]
        if len(set(rating_sources)) != len(rating_sources):
            raise ValueError(
                "The same rating source is listed twice. Set one bar per source, or the "
                "second would silently win."
            )
        return self

    def popularity_window_days(self) -> int:
        """The window the recent-watchers fact counts over, in days.

        Reads the SERVER_POPULARITY gate's window only while that gate is ENABLED: a
        disabled gate's leftover window must not keep steering the ``distinct_watchers``
        fact, where a short stale window quietly raises FEW_WATCHERS pressure across the
        whole library. Falls back to the 365-day default otherwise. Snapshot and backtest
        both read the window from here, so the default lives in exactly one place.
        """
        return next(
            (g.window_days for g in self.gates if g.gate is GateId.SERVER_POPULARITY and g.enabled),
            365,
        )

    def rating_rules(self) -> tuple[RatingRule, ...]:
        """Translate the per-source keep bars into engine rating rules for the gate.

        The one place the policy's rating specs become a gate input, mirroring how
        ``keep_configs``/``custom_signal_configs`` translate their specs, so the engine gate
        never imports the policy layer."""
        return tuple(
            RatingRule(source=r.source, floor=r.floor, min_votes=r.min_votes)
            for r in self.keep_rating_rules
        )

    def keep_configs(self) -> list[KeepConfig]:
        """Translate the graded-keep specs into engine keep configs for ``score()``."""
        return [
            KeepConfig(
                name=k.name,
                max_discount=k.max_discount,
                field=k.field,
                floor=k.floor,
                saturate_at=k.saturate_at,
                direction=k.direction,
            )
            for k in self.graded_keeps
        ]

    def custom_signal_configs(self) -> list[CustomSignalConfig]:
        """Translate the custom-condemn specs into engine configs for ``score()``.

        The one place the policy's specs become scoring inputs -- mirroring how ``signals``
        become ``SignalConfig``s at the call site -- so ``score()`` never imports the policy."""
        out: list[CustomSignalConfig] = []
        for spec in self.custom_condemn:
            if isinstance(spec, BooleanCondemnSpec):
                out.append(
                    CustomSignalConfig(
                        name=spec.name,
                        weight=spec.weight,
                        kind="boolean",
                        field=spec.field,
                        condition=Condition(field=spec.field, op=spec.op, value=spec.value),
                    )
                )
            else:
                out.append(
                    CustomSignalConfig(
                        name=spec.name,
                        weight=spec.weight,
                        kind="graded",
                        field=spec.field,
                        saturate_at=spec.saturate_at,
                        floor=spec.floor,
                    )
                )
        return out

    def canonical_json(self) -> str:
        """Byte-stable JSON. The basis of the hash.

        ``sort_keys`` plus tight separators plus integers-only makes this an exact
        canonicalisation: the same policy always produces the same bytes, on any
        machine, in any Python.
        """
        payload = self.model_dump(mode="json")
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def policy_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("ascii")).hexdigest()

    #: The fields applied to an item *after* it has been scored, by comparing the
    #: stored score and coverage against a number. Changing one of these re-decides a
    #: snapshot without re-reading anything. Changing anything else does not.
    _POST_SCORE_FIELDS: ClassVar[frozenset[str]] = frozenset({"condemn_at", "coverage_floor_bp"})

    #: Bookkeeping, not behavior: this cannot change a score, a verdict, or what a scan
    #: gathers, so it is excluded from BOTH simulator hashes. ``policy_hash`` still covers it,
    #: so an approval stays bound to the exact body it was planned under (rule 113).
    #:
    #: ``schema_version`` earned its place here the hard way. It is the *storage shape*, and
    #: the wire schema does not carry it, so a body round-tripped through ``api.schemas``
    #: came back stamped with the current code default while the stored row kept the older
    #: number. Both hashes then differed forever: the scan recorded the stored body's hash and
    #: the simulator computed the round-tripped one, so every edit read "Needs a fresh scan"
    #: and **scanning could not clear it**. Any install whose policy predated a version bump
    #: had a permanently dead simulator. A hash that decides whether a feature answers at all
    #: must cover only fields that change the answer.
    _NON_BEHAVIORAL_FIELDS: ClassVar[frozenset[str]] = frozenset({"schema_version"})

    def scoring_hash(self) -> str:
        """Identifies the policy's *scoring behavior*, ignoring the thresholds.

        Two policies with the same scoring hash assign every item the same score and
        the same gate outcomes; they may still disagree about the verdict, because
        ``condemn_at`` and ``coverage_floor_bp`` are compared against those results
        afterwards.

        This is the **first** of the simulator's three tiers, and what makes the
        zero-API-call path honest. Re-deciding a stored snapshot at a new threshold is
        exact, so while this hash matches, re-comparing the stored scores is enough.
        When it differs, the stored scores were produced by different weights or gates
        and cannot be reused -- so the simulator falls through to ``evidence_hash``,
        which decides whether the frozen Facts may be *replayed* under the edited policy
        (tier 2, still exact and still zero API calls) or whether the edit changed what
        a scan would gather at all, in which case it **refuses to report numbers**
        rather than reporting confident, stale ones (tier 3). The three tiers are
        enumerated in ``api.routes.simulate``.

        ``scorer_version`` is deliberately still in here: if the scorer itself changed, the
        stored scores are not comparable and this hash must say so, which is what routes a
        scorer bump to tier 2 rather than to stale stored scores. ``schema_version`` is not
        (see ``_NON_BEHAVIORAL_FIELDS``).
        """
        payload = {
            k: v
            for k, v in self.model_dump(mode="json").items()
            if k not in self._POST_SCORE_FIELDS and k not in self._NON_BEHAVIORAL_FIELDS
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()

    #: Fields a frozen-facts replay reproduces exactly, so a change to any of them does NOT
    #: need a fresh scan: the scorer and the protect/rating gates are pure functions of the
    #: frozen Facts. Everything ELSE is folded into the evidence hash, deliberately -- an
    #: allow-list, not a deny-list, so a field nobody remembered to classify defaults to
    #: "needs a fresh scan" (safe) rather than a stale replay (a plausible wrong preview).
    #: Notably ``gates`` is NOT here: the popularity gate's window changes the frozen
    #: watcher counts, so any gate edit re-scans -- a conservative, correct choice.
    #: ``scorer_version`` belongs here for the same reason the weights do: a replay runs the
    #: CURRENT ``score``/``evaluate_all``/``decide_verdict`` over the frozen Facts, so a new
    #: scorer's answer is reproduced exactly. It stays in ``scoring_hash``, which is what
    #: routes a scorer bump to the replay instead of to the stale stored scores.
    _EVIDENCE_REPLAYABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "condemn_at",
            "coverage_floor_bp",
            "scorer_version",
            "signals",
            "custom_condemn",
            "graded_keeps",
            "keep_rating_rules",
            "keep_rating_match",
            "protect_conditions",
        }
    )

    def evidence_hash(self) -> str:
        """Identifies what a scan under this policy would GATHER and FREEZE per item.

        Two policies with the same evidence hash produce byte-identical Facts (and the same
        season-pruning guard) for every item -- so the simulator may rebuild those Facts
        from ``Candidate.facts_json`` and replay the real ``score``/``evaluate_all``/
        ``decide_verdict`` under the edited policy, exact for any change to the replayable
        fields (weights, rating bars, custom rules, protect conditions, thresholds).

        When it differs, the edit changed the evidence itself -- the popularity window, a
        keep-tag, a season-pruning rule, the media type -- so the frozen Facts are stale and
        a real scan is required. The set of replayable fields is an allow-list, so an
        unclassified field falls into this hash and forces the safe, honest fresh scan.

        The allow-list is the right default and it has one sharp edge: a field that is pure
        bookkeeping falls in here too and forces a rescan that can never help. That is what
        ``schema_version`` did, permanently (see ``_NON_BEHAVIORAL_FIELDS``). Classify a new
        field into one of the three sets when you add it."""
        payload = {
            k: v
            for k, v in self.model_dump(mode="json").items()
            if k not in self._EVIDENCE_REPLAYABLE_FIELDS and k not in self._NON_BEHAVIORAL_FIELDS
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()


class ProfileSettings(Frozen):
    """The mutable part: how much Reaper may do, and how long a flagged title shows as
    leaving.

    Kept *out* of the hash on purpose. Tightening a cap or lengthening the grace
    period is always safe, and voiding every pending approval because the owner
    reduced a limit would train them to stop reading the diff.

    ``max_unmeasured_per_run`` is out of the hash too, but NOT for that reason, and the
    difference matters: it is the one field here that *loosens* what may be deleted, so
    "tightening is always safe" does not cover it. It is safe to leave out because of
    when it is read. It is consumed at plan construction, so raising it cannot add items
    to a plan already approved; and the executor re-reads it at execute time, so lowering
    it to 0 after approval causes those items to be kept. Both directions resolve toward
    keeping the file, which is why the timing makes the hash unnecessary here.
    """

    #: Four caps, not two. The rolling BYTE cap is what makes a 4 TB incident
    #: arithmetically unreachable: no sequence of runs can exceed it. The per-run caps
    #: are enforced by ``executor._check_caps`` and the rolling 30-day caps by
    #: ``Executor._check_rolling_caps``, both aborting (never truncating) before any send.
    max_items_per_run: int = Field(default=10, ge=1, le=1000)
    max_bytes_per_run: int = Field(default=500 * 1_000_000_000, ge=1)
    max_items_per_30d: int = Field(default=100, ge=1)
    max_bytes_per_30d: int = Field(default=2_000 * 1_000_000_000, ge=1)

    caps_enabled: bool = True
    """Whether the four caps above are enforced at all. On by default, so an install that
    configures nothing still runs bounded. Off, the per-run and 30-day caps stop aborting
    a run -- for a big first cleanup, say -- while every other gate still stands: the
    deletion password, the typed confirmation, the frozen-manifest re-check, the canary,
    and the live per-item vetoes. It never touches ``max_unmeasured_per_run``, which is
    the separate keep-unknown-size rule, not a run-size cap. Un-hashed like the caps: it
    only ever loosens, and the executor re-reads it at execute time."""

    grace_days: int = Field(default=14, ge=7)
    """How long a condemned item is shown as leaving, so the household can catch it.

    A **notice** window, not a gate: nothing on the deletion path reads it (see the module
    docstring on ``services/grace.py``), so it drives the Leaving Soon shelf and the
    Discord notice and never defers a send. Floored at 7: a countdown shorter than a week
    is one your users cannot realistically act on."""

    max_unmeasured_per_run: int = Field(default=0, ge=0, le=25)
    """How many items with no size a single run may delete. ``0``, the default, means
    never: an item Reaper cannot measure is held back (``planner.build_plan``).

    A count rather than an on/off switch, because an unmeasured item contributes nothing
    to either byte cap -- there is nothing honest to add -- so the byte caps cannot bound
    this population at all. The count IS the bound, which is also why the ceiling is low.

    Whatever it is set to, three things do not move: an unmeasured item never sorts first,
    so the run's test item is always one whose cost is known; they still count against the
    item caps; and a plan wanting more than the allowance aborts rather than truncating,
    because truncating would let sort order pick which unmeasured file dies."""

    @model_validator(mode="after")
    def _run_cap_within_rolling_cap(self) -> Self:
        if not self.caps_enabled:
            # The caps are off, so the relationships between them constrain nothing. Keeping
            # the validation here would reject legal combinations (a run cap above the
            # rolling cap, an unknown-size allowance above the hidden run cap) that can
            # never fire while enforcement is off.
            return self
        if self.max_items_per_run > self.max_items_per_30d:
            raise ValueError(
                f"A single run may delete {self.max_items_per_run} items but the 30-day "
                f"cap is {self.max_items_per_30d}. The rolling cap would be meaningless."
            )
        if self.max_bytes_per_run > self.max_bytes_per_30d:
            raise ValueError("A single run may delete more bytes than the entire 30-day budget.")
        if self.max_unmeasured_per_run > self.max_items_per_run:
            raise ValueError(
                f"A run may delete {self.max_unmeasured_per_run} items with an unknown "
                f"size but only {self.max_items_per_run} items in total. Lower the first "
                "number, or raise the second."
            )
        return self


class PolicyWarning(Frozen):
    """A config that is legal but probably not what the owner meant."""

    field: str
    message: str
    severity: Literal["warn", "danger"]


def _join_and(parts: list[str]) -> str:
    """Join as `"a", "b" and "c"`. The conjunctive twin of `fields._join_or`, kept in the
    module that needs it rather than reaching across for a private name; both exist because a
    comma-joined dump is not something an operator reads at a glance."""
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def rebalance(raw: object) -> dict[str, Any] | None:
    """A stored policy body rescaled so its removal weights total exactly 100.

    For bodies written before ``PolicyBody._weights_total_one_hundred`` existed, which
    were free to total anything. Those cannot be loaded any more, and falling back to the
    shipped default would silently throw away an operator's tuning and show them numbers
    they never chose.

    Rescaling is the right migration because the *exact* rescale is score-preserving: the
    score is ``100 * Σpressure / Σweight`` already, so dividing every weight by the same
    factor cannot move it. **Integer rounding can, by more than a point.** Largest-remainder
    bounds each weight's own error at 1, but those errors do not cancel in the score, since
    a rule that gained a point may be carrying pressure while one that lost a point is not:
    ``score' - score = Σ (w'ᵢ - wᵢ·100/T)·fillᵢ``. Weights ``(1, 1, 1, 5)`` become
    ``(13, 13, 12, 62)`` and move a score by a full point; six equal weights become
    ``17,17,17,17,16,16`` and move one by 1.33, which is enough to cross a condemn line.
    The drift is bounded by half the number of weighted rules, and no allocation does
    better, because which rules will carry pressure is unknowable at rescale time (weighting
    the remainder toward the larger weights does nothing at all in the equal-weight case).

    So a rescaled body is never adopted silently. The caller flags it
    (``services.profiles.ActivePolicy.rescaled``), which makes ``ActivePolicy.repaired``
    true, degrades the scan, and opens the editor on it as an unsaved draft the operator
    reviews and re-saves themselves. ``tests/test_policy.py`` pins both the bound and the
    fact that a verdict near the line can move.

    Returns ``None`` when the body is unreadable for any *other* reason -- including valid
    JSON that is not an object at all -- so the caller can tell "needs rebalancing" from
    "genuinely broken" and never present a repaired body it does not understand. This must
    not raise: ``services.profiles.active_policy`` relies on it to keep the policy editor
    reachable.
    """
    try:
        if not isinstance(raw, dict):
            return None
        body: dict[str, Any] = copy.deepcopy(raw)
        parts: list[dict[str, Any]] = [
            *(body.get("signals") or []),
            *(body.get("custom_condemn") or []),
        ]
        total = sum(int(p["weight"]) for p in parts)
        if total <= 0:
            return None
        exact = [int(p["weight"]) * MAX_SCORE / total for p in parts]
        floors = [int(x) for x in exact]
        order = sorted(range(len(parts)), key=lambda i: exact[i] - floors[i], reverse=True)
        for i in order[: MAX_SCORE - sum(floors)]:
            floors[i] += 1
        for part, weight in zip(parts, floors, strict=True):
            part["weight"] = weight
        PolicyBody.model_validate(body)  # only hand back something that actually loads
    except (AttributeError, KeyError, TypeError, ValueError, ValidationError):
        # AttributeError covers a body whose "signals"/"custom_condemn" entries are not
        # objects either, so a `.get`/`["weight"]` on the wrong shape returns None here
        # rather than escaping a function whose whole job is not to raise.
        return None
    return body


def recover_rating_rules(raw: object) -> dict[str, Any] | None:
    """A stored body whose rating bar was lost when the bar moved off the gate row.

    The bar used to live on the RATING_FLOOR gate setting as ``threshold`` (tenths) plus
    ``secondary`` (minimum votes). It now lives in ``keep_rating_rules`` as one spec per
    rating source, and the move shipped no backfill. A body written before it still
    **validates cleanly** -- the gate keeps its now-meaningless numbers, ``keep_rating_rules``
    defaults to empty -- and an empty rule set makes ``RatingFloorGate`` abstain on every
    item with "No rating is set that would keep a title." So the operator's "keep anything
    at 7.5 from 1,000 votes" silently protects nothing, on a healthy, executable snapshot.
    Every install seeded before that move is in this state, whether or not anyone opened
    the editor: ``services.profiles`` persists the shipped default as a real row the first
    time a profile is saved.

    Returns the body with the equivalent IMDb bar synthesized, or ``None`` when there is
    nothing to recover. The caller flags it (``services.profiles.ActivePolicy
    .rating_rules_recovered``), which makes ``repaired`` true, degrades the scan, and opens
    the editor on it as an unsaved draft -- never a silent substitution of an operator's
    own safety value (rule 65).

    What it keys on, and why not ``schema_version``: affected bodies already carry
    ``schema_version: 2`` (it was 2 before the move too), so the version cannot tell them
    apart. The trigger is the raw key ``keep_rating_rules`` being **absent** -- an explicit
    ``[]`` is an operator who deliberately cleared their bars and must keep an empty set
    (rule 1) -- plus an ENABLED ``rating_floor`` gate carrying numbers the old validator
    would have accepted (``1 <= threshold <= 100``, ``secondary >= 1``). A disabled gate is
    left alone: nothing was protecting anything either way, so there is nothing to restore
    and no reason to degrade a scan over it. IMDb is the right source because it is the
    only one the old single-source gate ever read.

    Must not raise: ``services.profiles.active_policy`` keeps the policy editor reachable.
    """
    if not isinstance(raw, dict) or "keep_rating_rules" in raw:
        return None
    gates = raw.get("gates")
    if not isinstance(gates, list):
        return None
    for gate in gates:
        if not isinstance(gate, dict) or gate.get("gate") != GateId.RATING_FLOOR.value:
            continue
        if not gate.get("enabled", True):
            return None
        floor, min_votes = gate.get("threshold"), gate.get("secondary")
        # bool is an int subclass, so a body carrying `true` must not read as 1.
        if isinstance(floor, bool) or isinstance(min_votes, bool):
            return None
        if not isinstance(floor, int) or not isinstance(min_votes, int):
            return None
        if not 1 <= floor <= 100 or min_votes < 1:
            return None
        body = copy.deepcopy(raw)
        body["keep_rating_rules"] = [
            {"source": RatingSource.IMDB.value, "floor": floor, "min_votes": min_votes}
        ]
        # Write back at the current schema so a body that has been through the editor
        # since can be told apart, and this shim can eventually retire.
        body["schema_version"] = SCHEMA_VERSION
        return body
    return None


def _protect_blocks_on_reach(cond: ConditionSpec) -> ReachSpan | None:
    """Which span's shortfall would block this rule on EVERY item, or ``None`` for neither.

    Two tests, and the second is the one that is easy to miss. The registry owns the span
    (``FieldSpec.reach_span``), so a FIELD that gains or loses that bound moves this with it
    rather than leaving a second list to drift (rule 103). An unknown key is ``None``: a rule
    that no longer validates cannot be blocking anything.

    That sentence used to be written as though it covered a new SPAN too, and it never did
    (rules 7/24, 103). The consumers hand-enumerate the members -- the two ``in
    protect_spans`` tests below, one per warning, and the lean loop's match -- so a third
    ``ReachSpan`` took the ``else`` and was scored against the wrong bound with nothing
    failing (issue #168). The two routing sites now match member by member and mypy holds
    them (``fields.reach_shortfall``, the lean loop). The membership tests cannot be closed
    that way, because each one carries copy written for its own span: a third member simply
    gets no warning, which is silence rather than a wrong answer.
    ``tests.test_policy.TestEveryReachSpanIsRoutedByName`` is what fails when the set
    changes, and it names every site that has to grow a branch.

    It answers with the SPAN rather than a yes for one of them, because the two spans need
    different world-facts to decide whether the shortfall is live and the caller has to tell
    them apart (rule 140). The operator test below is span-agnostic -- ``_survives_more_history``
    reads only the op -- so scoping this to the popularity window was never the registry
    speaking, just the one lane somebody had reached for. ``watchers_all_time`` carries the
    other span and is PROTECT-only, so it was the one field this could not see and the only
    lane with no warning at all.

    But the span alone does not decide it -- ``fields._survives_more_history`` reads the
    OPERATOR, because a truncated watcher count is a lower bound and only two of the four
    outcomes can be overturned by history nobody has yet. Under ``gte`` every item is either
    a fired PROTECT or a block, so nothing is condemned and the caller's "nothing will be
    flagged" holds. Under ``lte`` it inverts: an item already OVER the bar has an outcome
    more history cannot change, so it comes back a plain checked ABSTAIN and stays
    condemnable. Claiming an empty list there would be false in the reassuring direction,
    and the remedy the caller offers -- remove the rule -- would drop a live protection off
    the items that ARE blocked (rules 7/24, 144).
    """
    spec = BY_KEY.get(cond.field)
    if spec is None or spec.reach_span is None or cond.op is not Op.GTE:
        return None
    return spec.reach_span


def inspect(
    body: PolicyBody,
    settings: ProfileSettings,
    *,
    requests_app_configured: bool = True,
    history_reach_days: float | None = None,
) -> list[PolicyWarning]:
    """The dangerous-config detector.

    Validation refuses what is *provably* wrong. This catches what is merely
    *probably* wrong -- and a validator cannot tell the two apart, because the
    values are legal either way.

    The archetype: an IMDb floor is stored in tenths, so ``75`` means 7.5. A user
    thinking in Rotten Tomatoes types ``96``, which is legal (it means 9.6) and
    protects almost nothing. No validator can distinguish that from someone who
    genuinely wants a 9.6 floor. So we say so, loudly, and show the blast radius
    next to it rather than pretending to know.

    ``requests_app_configured`` is the one thing here a policy cannot know about
    itself: whether the operator has a Seerr connected. It defaults to True, meaning
    "assume they do, and stay quiet". A caller that cannot tell should not guess,
    because the only warning it gates says a setting is doing nothing -- and telling
    someone to connect a service they already have is worse than saying nothing.

    ``history_reach_days`` is the second such fact: how far back the watch mirror goes
    (``dormancy.history_reach_days`` off ``services.history_sync.horizon``, the one
    derivation ``services.snapshot.ScanContext`` uses for the number the gate reads).
    Same posture and same reason -- ``None`` means "could not tell, stay quiet", because
    a caller that guessed short would tell an operator their window is useless when it
    is fine.
    """
    warnings: list[PolicyWarning] = []

    rating_on = any(g.gate is GateId.RATING_FLOOR and g.enabled for g in body.gates)
    if rating_on:
        if not body.keep_rating_rules:
            warnings.append(
                PolicyWarning(
                    field="keep_rating_rules",
                    severity="warn",
                    message=(
                        "Keep well-rated titles is turned on, but it has no rating sources "
                        "yet, so it is not keeping anything. Add a rating source to it, or "
                        "turn the protection off."
                    ),
                )
            )
        for rule in body.keep_rating_rules:
            label = source_label(rule.source)
            if is_percentage_source(rule.source):
                # A percentage source read on the 0-10 scale is the usual mix-up: typing 8
                # meaning "80%" sets an 8% bar that keeps everything.
                if rule.floor <= 20:
                    warnings.append(
                        PolicyWarning(
                            field="keep_rating_rules",
                            severity="warn",
                            message=(
                                f"A {label} bar of {rule.floor}% protects almost everything. "
                                "This field is a percentage: for 80% enter 80, not 8."
                            ),
                        )
                    )
            else:
                if rule.floor >= 90:
                    warnings.append(
                        PolicyWarning(
                            field="keep_rating_rules",
                            severity="warn",
                            message=(
                                f"A {label} bar of {rule.floor / 10:.1f} will protect almost "
                                "nothing: very few titles rate that highly."
                            ),
                        )
                    )
                if rule.floor <= 20:
                    warnings.append(
                        PolicyWarning(
                            field="keep_rating_rules",
                            severity="warn",
                            message=(
                                f"A {label} bar of {rule.floor / 10:.1f} protects essentially "
                                "everything. Did you mean 7.0?"
                            ),
                        )
                    )

    # The span every reader of a watcher count is measured against -- NOT the enabled gate
    # row. ``PolicyBody.popularity_window_days`` falls back to 365 when the gate is off or
    # absent, and ``services.scan_runner.build_gates`` hands that fallback to
    # ``CustomProtectGate`` regardless of the switch, so one bound governs every reader
    # (rule 140). Reading the row here scoped both warnings below to one of those readers
    # and left an operator's own keep-outright rule blocking library-wide against a year
    # they never set and, with the gate off, cannot even see.
    window_days = body.popularity_window_days()
    popularity = next(
        (g for g in body.gates if g.gate is GateId.SERVER_POPULARITY and g.enabled), None
    )
    # Only an enabled gate can carry a window this short: the fallback the disabled case
    # resolves to is the 365-day default, which never trips it.
    very_short = popularity is not None and window_days < 30

    # The same window in the other direction, and the reason this detector needed a second
    # world-fact at all. ``gates.ServerPopularityGate.evaluate`` fails closed when the mirror
    # is shorter than the window it is being asked about: a count over three months cannot
    # answer "who watched this in the last year", so the gate blocks. The reach is a property
    # of the operator's DATA rather than of any one title, so it blocks library-wide, and it
    # goes on blocking for as long as the shortfall lasts.
    #
    # Most blocks clear on the next scan (an unreachable Seerr, an unread session list, a
    # missing id), which is why no surface was ever obliged to name a remedy for one. The
    # ones that do not are all the same family, a mirror shallower than the question, and the
    # other members are held on the season path: ``season_scan``'s lifetime-shortfall
    # conflict and ``gates.progress_is_establishable``.
    #
    # This used to read "the member with a control the operator can turn, so the editor is
    # where it has to be said", which quietly justified saying nothing about the other two.
    # It was false of the mid-binge hold from the day that guard shipped:
    # ``in_progress_hold_days`` is a control on this same editor, one card down, and a hold
    # the mirror cannot span holds every season on disk (issue #154). That branch is at the
    # foot of this function now. The lifetime-shortfall conflict is the one member with no
    # control behind it -- it turns on each ITEM's age against the reach, and no setting the
    # operator can reach moves either -- so it stays unwarned here, deliberately and not by
    # omission (rules 7/24, 72).
    #
    # WHO blocks on this window, which is what makes "nothing will be flagged" true. This
    # detector claims it for the PROTECT lane only: a blocked protect ABSTAINs every item
    # (``verdict.decide_verdict``), library-wide, for as long as the shortfall lasts. Two
    # readers sit in that lane and the built-in gate is only one of them -- an operator's
    # own keep-outright rule on a popularity-window field is the other, and ``build_gates``
    # hands it this same span whether the gate is on or off.
    #
    # **The lean lane is warned about too now, further down**, and this comment carried it as
    # a known gap for a while: a graded keep takes its FULL ``max_discount`` on a shortfall,
    # for every item (``signals.evaluate_keep``), and ``score()`` floors at zero under a
    # bounded base, so a keep worth more than ``MAX_SCORE - condemn_at`` empties the list just
    # as provably as a blocked protect does. The ``graded_keeps`` warning at the end of this
    # function is still not that warning: it fires on ``total_keep >= condemn_at``, a much
    # higher bar, and says nothing about the mirror. ``PolicyRuleEditors``' ``leanFields`` is
    # not gate-filtered, so the field is offered as a lean whichever way the switch is set,
    # which is why the lean check does not read the gate either (rules 7/24, 140).
    #
    # This branch remains about the PROTECT lane only. The three lanes and what each does under
    # a shortfall, since the asymmetry is the whole reason there are separate checks: a protect
    # blocks and abstains, a lean takes its full discount, and a condemn rule withholds its
    # pressure while keeping its weight in the denominator.
    #
    # That last one lowers scores without blocking anything, so it cannot empty the list
    # through PRESSURE -- but it can through COVERAGE, and **that lane is warned about now
    # too, further down** (issue #164 closed). This paragraph used to rule it safe and was
    # wrong to (rule 7/24): a blocked signal is unevaluated, so its weight leaves the
    # numerator and stays in the denominator, and enough weight on reach-bounded fields drops
    # coverage under ``coverage_floor_bp`` for every item at once.
    #
    # ``warn``, not ``danger``: the outcome is that Reaper deletes nothing, which is the
    # keep direction. Every ``danger`` here marks a config that removes MORE.
    #
    # The cause clause comes from ``gates.history_shortfall`` rather than being restated,
    # because the why-panel prints that same sentence off the same helper for the same
    # operator (rule 144), and it already decides when the gap is too small to name a number.
    #
    # ONLY where the block is what is actually holding the list back, which is what
    # ``reach_clears_dormancy`` tests. ``MinDormancyGate`` PROTECTs anything younger than its
    # threshold, ``verdict.decide_verdict`` puts PROTECT ahead of blocked, and dormancy is
    # clamped to the mirror (``dormancy.reference_instant`` measures from the horizon at the
    # earliest). So while the reach is under the floor, every item is kept on age alone and
    # the popularity window decides nothing. Without this test the warning fires on both
    # shipped policies -- floor 1095, window 365 -- for every operator holding under a year
    # of history, and the remedy it names cannot move a single verdict.
    dormancy_floor = next(
        (g for g in body.gates if g.gate is GateId.MIN_DORMANCY and g.enabled), None
    )
    reach_clears_dormancy = dormancy_floor is None or (
        history_reach_days is not None and history_reach_days >= dormancy_floor.threshold
    )
    # Kept as pairs rather than reduced straight to a set: the gate-off message below has to
    # name the rules doing the blocking and count them, and a set of spans cannot say which
    # conditions produced it (issue #157).
    blocking = [
        (c, span)
        for c in body.protect_conditions
        if (span := _protect_blocks_on_reach(c)) is not None
    ]
    # The floor itself, which is the ROOT of this family rather than another member of it, and
    # had no warning at all (issue #217). Dormancy is clamped to the mirror --
    # ``dormancy.reference_instant`` is ``last_played or max(added_at, horizon)``, and all three
    # arms are at most the reach -- so the most dormant any item can read IS the reach.
    # ``MinDormancyGate`` PROTECTs anything under its threshold and PROTECT beats everything in
    # ``decide_verdict``, so a floor above the reach keeps the entire library on age alone until
    # the mirror catches up. On the shipped 1095-day floor that is every operator holding under
    # three years of history, which is most new installs.
    #
    # It has to be said HERE because ``reach_clears_dormancy`` is read four times below to
    # SILENCE the other warnings in this family, each correctly: under the floor their remedies
    # would move no verdict. The aggregate was a page that went quietest exactly where the list
    # was emptiest, with nothing speaking for the condition that silenced everything. This
    # branch is that voice, and it cannot stack with the four, because it fires on precisely
    # the negation they are guarded on.
    if dormancy_floor is not None and history_reach_days is not None and not reach_clears_dormancy:
        floor_short = history_shortfall(
            Known(value=history_reach_days, source="tautulli"), float(dormancy_floor.threshold)
        )
        warnings.append(
            PolicyWarning(
                field=f"gates.{GateId.MIN_DORMANCY.value}.threshold",
                severity="warn",
                message=(
                    # ``humanize_days``, not ``humanize_window``: the window helper drops a
                    # lone "1" so it composes as "in the last year", and this slot has no
                    # article to carry it ("waits year of no watching"). Rule 21.
                    "Nothing will be flagged for removal. Reaper waits "
                    f"{humanize_days(dormancy_floor.threshold)} of no watching before "
                    f"anything can go, and {floor_short}, so it can't yet show a title sitting "
                    "untouched that long. Wait for it to build up, or lower this wait."
                ),
            )
        )

    protect_spans = {span for _, span in blocking}
    window_blockers = [c for c, span in blocking if span is ReachSpan.POPULARITY_WINDOW]
    owner_protect_on_window = bool(window_blockers)
    # Derived once and read by both lanes below (rule 104). The protect lane additionally
    # requires a reader on the window, which is what ``short`` adds; the lean lane does not,
    # because a graded keep on a window field is discounted whether the gate is on or off.
    window_short: str | None = None
    if history_reach_days is not None and reach_clears_dormancy:
        window_short = history_shortfall(
            Known(value=history_reach_days, source="tautulli"), float(window_days)
        )
    short = window_short if (popularity is not None or owner_protect_on_window) else None
    if short is not None:
        window_text = humanize_window(window_days)
        if popularity is not None:
            # The window control is on the page while the gate is on (``PolicyEditor``'s
            # ``GateRow`` renders it under ``gate.enabled``), so the remedy may name it.
            #
            # Except when the window is ALSO under the short-window floor, where "lower it"
            # is advice in the direction the other warning is pushing back on. Both faults
            # are real and their remedies genuinely oppose, so one message carries the pair
            # rather than two stacking on one control and cancelling out. Note what is and
            # is not claimed: shortening to the reach DOES clear the shortfall, it just
            # buys the other fault to do it -- an even shorter window counts almost nothing
            # as watched. Waiting is the only move that clears one without deepening the
            # other, which is why it leads.
            remedy = (
                "Wait for it to build up: a shorter window would leave almost nothing "
                "counted as watched."
                if very_short
                else "Lower this window to match your history, or wait for it to build up."
            )
            field = f"gates.{GateId.SERVER_POPULARITY.value}.window_days"
            # The window is named BEFORE the cause clause, because ``history_shortfall``'s
            # in-margin arm is "your watch history does not go back that far" and "that far"
            # needs the span to have been said already.
            message = (
                "Nothing will be flagged for removal. Reaper can't say who watched a title "
                f"in the last {window_text} from a shorter history, and {short}. {remedy}"
            )
        else:
            # The gate is off, so the window is the 365-day fallback and its control is not
            # rendered at all. Anchoring on it would name a box that is not on the page, so
            # this rides with the rule that is actually blocking, where both remedies are in
            # reach: the rule can be deleted right there, and waiting always works. Naming
            # the protection they would have to switch back on to expose the window is
            # deliberately NOT done -- its label lives in ``frontend`` (``policyMeta.ts``)
            # and a second spelling here would drift from it (rule 144).
            #
            # The rule is NAMED, and counted, because neither was safe to leave implicit
            # (issue #157). Two rules on the same field are constructible -- ``addHard``
            # appends unconditionally and ``PolicyBody`` validates the pair -- so a singular
            # "remove that rule" was factually wrong there: removing one leaves the warning
            # byte-identical while a live protection is gone, with nothing saying the pick
            # was wrong. And "counts who watched a title" is not a discriminator when the
            # card beside it reads "People who have ever watched it", which also counts who
            # watched a title; the only thing telling them apart was the window, which is
            # unrendered here by this branch's own premise.
            #
            # The label comes from the registry the editor renders from -- backend-owned,
            # served verbatim through ``GET /api/vocabulary`` to ``describeCondition`` -- so
            # this names the string already on the operator's screen rather than a second
            # spelling of it (rule 144). Distinct labels joined, so a span that ever gains a
            # second field does not silently name one of them.
            field = "protect_conditions"
            labels = _join_and(
                list(dict.fromkeys(f'"{BY_KEY[c.field].label}"' for c in window_blockers))
            )
            many = len(window_blockers) > 1
            subject = f"Your {len(window_blockers)} keep rules" if many else "Your keep rule"
            counts = "count" if many else "counts"
            message = (
                f"Nothing will be flagged for removal. {subject} on {labels} {counts} the "
                f"last {window_text}, and {short}. Wait for it to build up, or remove "
                f"{'them' if many else 'that rule'}."
            )
        warnings.append(PolicyWarning(field=field, severity="warn", message=message))

    # The OTHER span, and the reader that had no warning at all. ``watchers_all_time`` is
    # PROTECT-only and carries ``ITEM_LIFETIME``, so ``fields.evaluate`` blocks it through
    # ``gates.lifetime_shortfall`` for every item the mirror does not reach back to the arrival
    # of -- and under ``gte`` a blocked protect abstains while a matched one keeps, so not one
    # of those items can be condemned.
    #
    # What is NOT claimed here is the whole library. The span this one needs is the ITEM's age,
    # not a policy setting, so the affected set is "everything added before the history starts"
    # and ``inspect`` cannot size it: it is handed one reach, never a list of arrival dates. So
    # the message names the set instead of asserting an empty list, which is the difference
    # between this and the window branch above. Saying "nothing will be flagged" would be false
    # in the reassuring direction for a young library the mirror covers outright (rules 7/24).
    #
    # The dormancy guard still applies for the same reason it does above -- under the floor
    # every item is kept on age alone, so this rule is deciding nothing and its remedy would
    # move no verdict.
    if (
        ReachSpan.ITEM_LIFETIME in protect_spans
        and history_reach_days is not None
        and reach_clears_dormancy
    ):
        warnings.append(
            PolicyWarning(
                field="protect_conditions",
                severity="warn",
                message=(
                    "Titles added before your watch history starts won't be flagged for "
                    "removal. Your keep rule counts everyone who has ever watched a title, and "
                    "Reaper can't count plays from before your history begins, so it holds "
                    "those titles instead of guessing. Wait for it to build up, or remove that "
                    "rule."
                ),
            )
        )

    # The CONDEMN lane, the third of the four and the one the comment above used to rule out.
    # A blocked condemn rule withholds its pressure and keeps its weight in the denominator
    # (``signals.score``), so it cannot empty the list through pressure. The weight it leaves
    # behind lowers BOTH bounds ``decide_verdict`` reads, though: coverage, which is what
    # issue #164 measured, and the score ceiling with it -- ``signals``' "``condemn_at`` is
    # itself a coverage floor" note. So the question is put to the real decision function
    # rather than answered here, which covers both bounds and keeps the floor comparison in
    # the one place allowed to make it (rule 3/22).
    #
    # Summed over the readers whose block is LIBRARY-WIDE, which is not every reader of the
    # field. Driven at a 90-day reach against the 365-day fallback, coverage per item:
    #
    #     built-in FEW_WATCHERS:   0.45 at 0 watchers, 0.45 at 50   -- always withheld
    #     a graded custom rule:    0.45 at 0 watchers, 0.45 at 50   -- always withheld
    #     a boolean custom rule:   0.45 at 0 watchers, 0.80 at 50   -- per item
    #
    # The built-in withholds on every observation it can take: a Known count fails the reach
    # check, an Absent one fails it too (rule 93's precondition is a GENUINE absence, which a
    # window the mirror does not span cannot establish), and an Unknown has no number to ramp.
    # The graded arm exempts an Absent input, which ``distinct_watchers`` never is -- every
    # builder writes Known or Unknown, none of them Absent -- so it too is withheld for every
    # item. ``watchers_all_time`` cannot appear on either: it is PROTECT-only, so
    # ``ITEM_LIFETIME`` never reaches the condemn lane and the window is the only span here.
    #
    # A BOOLEAN rule lowers ONE of the two bounds, which is why it is summed separately
    # rather than either counted with the rest or left out. It goes through
    # ``fields.evaluate``, which keeps ``_survives_more_history``'s earned outcomes, so an
    # item the truncated count already settles comes back EVALUATED and keeps its weight in
    # coverage -- the 0.80 row above. But a boolean rule is all-or-nothing, and under ``lte``
    # the outcome that gets blocked is the MATCH: an item over the bar earns nothing because
    # the rule did not fire, and one under it earns nothing because the rule was blocked. So
    # the weight leaves the score for every item at once while coverage keeps it, and no item
    # can reach a threshold that needs it. Under ``gte`` the reverse holds and a matched item
    # does earn the weight, so the list is genuinely not empty and counting it would be false
    # in the reassuring direction (rule 144). ``fields.can_add_pressure_under_a_shortfall``
    # is that discrimination, asked rather than restated (rule 104).
    #
    # What this still does NOT claim is the partial case: where the remaining weight can
    # reach the threshold, an ``lte`` rule abstains exactly the titles nobody watched
    # recently, a set ``inspect`` cannot size from one reach. That is issue #215, and it is
    # filed rather than guessed at here.
    withheld = 0
    never_earned = 0
    # Kept apart from the totals so the anchor below can weigh the two cards against each
    # other. The built-in slider is the only reach-bounded signal, so this IS the signals
    # card's share; everything else in the totals comes from the custom-rules card.
    on_the_signals_card = 0
    if window_short is not None:
        for signal in body.signals:
            if signal.signal is SignalId.FEW_WATCHERS and signal.weight > 0:
                withheld += signal.weight
                on_the_signals_card += signal.weight
        for condemn in body.custom_condemn:
            condemn_spec = BY_KEY.get(condemn.field)
            if (
                condemn.weight <= 0
                or condemn_spec is None
                or condemn_spec.reach_span is not ReachSpan.POPULARITY_WINDOW
            ):
                continue
            if isinstance(condemn, GradedCondemnSpec):
                withheld += condemn.weight
            elif not can_add_pressure_under_a_shortfall(condemn.op):
                never_earned += condemn.weight
    if withheld > 0 or never_earned > 0:
        # The best any item can do once that weight is gone. The denominator is pinned at
        # ``MAX_SCORE`` (``_weights_total_one_hundred``), so a weight IS its share, and the
        # two bounds differ only by the boolean weight that stays evaluated.
        covered = MAX_SCORE - withheld
        ceiling = covered - never_earned
        # Each is a genuine upper bound on its own, and ``decide_verdict`` is monotone in
        # both, so passing the best of each independently is the most permissive reading
        # available. The warning can therefore only fire late, never falsely.
        best_case = decide_verdict(
            protected=False,
            blocked=False,
            score=ceiling,
            coverage_bp=round(covered / MAX_SCORE * 10_000),
            condemn_at=body.condemn_at,
            coverage_floor_bp=body.coverage_floor_bp,
        )
        if best_case != "condemn":
            warnings.append(
                PolicyWarning(
                    # Beside the points that have to move. The built-in's slider and the
                    # custom rules sit in different cards and one ``field`` can only claim
                    # one of them, so it goes to whichever holds MORE of the weight rather
                    # than to whichever holds any (rule 42). It used to fire on the built-in's
                    # mere presence, so 5 points on the slider beside 50 on a custom rule sent
                    # the operator to the smaller number and left the card that has to change
                    # unmarked. Ties go to the signals card, which is the one above.
                    field=(
                        "signals"
                        if on_the_signals_card * 2 >= withheld + never_earned
                        else "custom_condemn"
                    ),
                    severity="warn",
                    message=(
                        f"Nothing will be flagged for removal. {withheld + never_earned} of "
                        f"your {MAX_SCORE} removal points count who watched a title in the last "
                        f"{humanize_window(window_days)}, and {window_short}, so only "
                        f"{ceiling} points are left to judge on. Wait for it to build up, or "
                        "move those points to a reason that doesn't count watchers."
                    ),
                )
            )

    # The lean lane, which the comment above used to name as a known gap. A graded keep takes
    # its FULL ``max_discount`` on a shortfall, on every item it reaches, with no
    # ``_survives_more_history`` test to earn an outcome back (``signals.evaluate_keep``) -- and
    # ``score()`` is ``max(0, base - keep_discount)`` over a base bounded by ``MAX_SCORE``. So a
    # single keep worth more than the headroom holds every affected item under the threshold as
    # provably as a blocked protect does, and it does it on a lane the operator was told was
    # safe.
    #
    # The existing ``graded_keeps`` warning is not this one and does not cover it: it fires on
    # the keeps TOTALLING at least ``condemn_at``, a much higher bar (70 against 31 on shipped
    # values), and says nothing about the mirror. A keep at 40 sits in that dead zone.
    #
    # Anchored on ``graded_keeps`` beside the rule doing it, and it can name the rule, which
    # the protect lanes above cannot: a ``GradedKeepSpec`` carries a name the operator typed.
    # Summed per span, never per rule. ``evaluate_keep`` grants each blocked keep its full
    # ``max_discount`` and ``score()`` subtracts the SUM, so two keeps of 20 against a headroom
    # of 30 empty the list exactly as one keep of 40 does. Testing each rule alone left that
    # case silent, which is the same dead zone this warning exists to close, one arity up.
    #
    # The two spans are kept apart because they bound different things. A window shortfall is a
    # property of the operator's DATA, so a window keep's discount lands on every item; a
    # lifetime shortfall is a property of each ITEM's age, and ``inspect`` is handed one reach
    # and never a list of arrival dates. So window keeps alone crossing the headroom is the only
    # case that may claim an empty list, and the combined case names the affected set instead
    # (rule 144's reassuring-direction failure, which is why the wider claim is not made here).
    headroom = MAX_SCORE - body.condemn_at
    window_keeps: list[GradedKeepSpec] = []
    lifetime_keeps: list[GradedKeepSpec] = []
    if history_reach_days is not None and reach_clears_dormancy:
        for keep in body.graded_keeps:
            keep_spec = BY_KEY.get(keep.field)
            if keep_spec is None or keep_spec.reach_span is None:
                continue
            # Matched member by member, ``fields.reach_shortfall``'s twin and for the same
            # reason: the ``else`` here filed any third span under lifetime, which would
            # print the "plays from before your history begins" copy about a span that is
            # not the one blocking them, and score it against the wrong bound (issue #168).
            match keep_spec.reach_span:
                case ReachSpan.POPULARITY_WINDOW:
                    if window_short is not None:
                        window_keeps.append(keep)
                case ReachSpan.ITEM_LIFETIME:
                    lifetime_keeps.append(keep)
                case _:
                    assert_never(keep_spec.reach_span)

    windowed_total = sum(k.max_discount for k in window_keeps)
    combined_total = windowed_total + sum(k.max_discount for k in lifetime_keeps)
    contributors: list[GradedKeepSpec] = []
    scope = cause = ""
    total = 0
    if windowed_total > headroom:
        contributors, total = window_keeps, windowed_total
        scope = "Nothing will be flagged for removal."
        cause = (
            f"Reaper can't say who watched a title in the last "
            f"{humanize_window(window_days)}, and {window_short}, so "
        )
    elif combined_total > headroom:
        contributors, total = window_keeps + lifetime_keeps, combined_total
        scope = "Titles added before your watch history starts won't be flagged for removal."
    if contributors:
        named = _join_and([f'"{k.name}"' for k in contributors])
        many = len(contributors) > 1
        rule_phrase = f"your keep rules {named} take" if many else f"your keep rule {named} takes"
        theirs = "their" if many else "its"
        # ``max_discount`` is ``ge=1``, so at ``condemn_at`` 100 the headroom is 0 and every
        # settable value is too high. Naming a number there sends the operator to a control
        # that refuses it, so the remedy drops to the one move that still works.
        if headroom < 1:
            remedy = f"remove {'those rules' if many else 'that rule'}"
        else:
            unit = "point" if headroom == 1 else "points"
            what = "their total" if many else "it"
            remedy = f"set {what} to {headroom} {unit} or less"
        # The cause clause leads only on the window branch; without it the sentence starts on
        # "your", so it is capitalized here rather than carried as a second literal that could
        # drift out of step with the one above it.
        said = (
            f"{cause}{rule_phrase} all {total} of {theirs} points off "
            f"{'every title' if cause else 'them'}."
        )
        if not cause:
            said = said[:1].upper() + said[1:]
        warnings.append(
            PolicyWarning(
                field="graded_keeps",
                severity="warn",
                message=f"{scope} {said} Wait for it to build up, or {remedy}.",
            )
        )

    # Only where the shortfall is NOT already speaking for this control: it carries the pair
    # itself in that case, and stacking both told the operator to raise and to lower the same
    # number in adjacent sentences.
    if very_short and short is None:
        warnings.append(
            PolicyWarning(
                field=f"gates.{GateId.SERVER_POPULARITY.value}.window_days",
                severity="warn",
                message=(
                    f"A {window_days}-day watch window is very short: almost nothing gets "
                    "watched inside it, so the few-recent-watchers pressure applies to nearly "
                    "your whole library. A year is the usual setting."
                ),
            )
        )

    # The season path's member of the same family, one field down the same editor card, and
    # the last of the four lanes a shallow mirror empties (rule 72, issue #154). The mid-binge
    # guard holds a viewer whose last play falls inside ``in_progress_hold_days``; where the
    # mirror does not span that hold, an invisible viewer and an expired one are the same
    # viewer, so the set is UN-ESTABLISHABLE rather than empty and ``plan_series_prune`` holds
    # every season on disk. Nothing is left for the scoring lane to judge, and until now the
    # page said nothing at all: ``in_progress_hold_days`` appeared in this module only as a
    # field declaration.
    #
    # The journey this closes is the one that reads as Reaper being broken. An operator on a
    # short mirror gets the popularity-window warning, follows it, lowers the window to match
    # their history, and clears it -- and the list is still empty, now with no warning on the
    # page at all, because the one surface that ever named their history reach was the warning
    # they just cleared.
    #
    # Guarded on ``progress_is_establishable`` rather than on a shortfall, because the two
    # disagree at ``hold_days = 0``: that means "hold a place forever", which no finite mirror
    # can support and the predicate answers False at any reach, while
    # ``history_shortfall(reach, 0.0)`` sees a span of zero days, finds it covered, and returns
    # None. So the predicate decides WHETHER to speak and the shortfall supplies the cause
    # clause only, and the zero arm needs its own cause. Asking the predicate is also what
    # keeps one derivation of "does the mirror span the hold" (rule 104); it moved to
    # ``engine.gates`` beside its two siblings so this could ask it without an engine module
    # importing a service.
    if (
        body.media_type == "tv"
        and body.keep_in_progress
        and history_reach_days is not None
        and reach_clears_dormancy
        and not progress_is_establishable(
            reach_days=int(history_reach_days), hold_days=body.in_progress_hold_days
        )
    ):
        if body.in_progress_hold_days <= 0:
            # No number to compare a reach against, so no shortfall sentence exists to
            # borrow. The editor's own help text under this control already says a 0 keeps
            # every season; this is the same fact at the moment it is true (rule 144).
            cause = (
                "At 0 days a viewer's place is held forever, and no watch history reaches "
                "back far enough to check that"
            )
            remedy = "Set a number of days, or turn this protection off."
        else:
            hold_short = history_shortfall(
                Known(value=history_reach_days, source="tautulli"),
                float(body.in_progress_hold_days),
            )
            # The hold is named BEFORE the cause clause, for the reason the window branch
            # names its span first: the in-margin arm is "does not go back that far".
            # ``humanize_days`` for the same reason as the dormancy branch above: "for year
            # after they last watched" has no article to carry the dropped "1" (rule 21).
            cause = (
                "Reaper holds a viewer's place for "
                f"{humanize_days(body.in_progress_hold_days)} after they last watched, and "
                f"{hold_short}"
            )
            remedy = "Wait for it to build up, or lower this to match your history."
        warnings.append(
            PolicyWarning(
                field="in_progress_hold_days",
                severity="warn",
                message=(
                    f"No TV season will be flagged for removal. {cause}, so it can't tell "
                    f"who is partway through a show and holds every season. {remedy}"
                ),
            )
        )

    disabled = {g.gate for g in body.gates if not g.enabled}
    # Each of these states the consequence THIS switch has, verified against the code that
    # would deliver it (rules 7/24 and 25). Both used to name a consequence that cannot
    # occur, because the outcome each described is delivered somewhere the switch does not
    # reach:
    #
    # * The active-stream veto lives in the executor and is unconditional -- ``_reap_one``
    #   calls ``_being_watched_now`` on every real send without ever consulting the policy
    #   gate, and ``execute`` refuses a real run outright when Plex is missing. So turning
    #   the gate off cannot delete a file mid-play. What it does do is let the title be
    #   condemned, listed, and approved, and then skipped at the last moment.
    # * The horizon defense is the dormancy CLAMP in fact derivation
    #   (``services.snapshot.build_facts``, ``max(added_at, horizon)``), which runs whatever
    #   this switch says. ``DataHorizonGate`` can never PROTECT -- its own docstring says so,
    #   and ``evaluate`` has only a blocked branch and an abstain -- and its one independent
    #   job is failing closed on an Unknown dormancy, which ``MinDormancyGate`` also does.
    for gate, why in (
        (
            GateId.STREAMING_NOW,
            "A title someone is watching still reaches your reap list. Reaper skips it at "
            "the last moment, so a run removes less than it showed you.",
        ),
        (
            GateId.DATA_HORIZON,
            "Reaper drops one of the two checks that keep a title whose unwatched time it "
            "could not read.",
        ),
    ):
        if gate in disabled:
            warnings.append(
                PolicyWarning(field=f"gates.{gate.value}.enabled", severity="danger", message=why)
            )

    if body.condemn_at <= 30:
        warnings.append(
            PolicyWarning(
                field="condemn_at",
                severity="danger",
                message=(
                    f"A threshold of {body.condemn_at} condemns almost everything the "
                    "protections do not save. Check the simulator's counts and review "
                    "the flagged list carefully before arming this."
                ),
            )
        )

    if settings.max_unmeasured_per_run > 0:
        # Legal, and probably not what most operators mean: exactly what this detector is
        # for. The GB caps genuinely cannot cover these items, so saying so is not a
        # scare, it is the one fact that makes the setting understandable.
        warnings.append(
            PolicyWarning(
                field="max_unmeasured_per_run",
                severity="warn",
                message=(
                    f"Reaper will delete up to {settings.max_unmeasured_per_run} items it "
                    "can't measure. The GB caps won't cover them."
                ),
            )
        )

    for spec in body.custom_condemn:
        if spec.field == "size_bytes" and spec.weight > 0:
            warnings.append(
                PolicyWarning(
                    field="custom_condemn",
                    severity="danger",
                    message=(
                        f'Your rule "{spec.name}" removes things for being large. File size '
                        "measures how much space you reclaim, not whether anyone wants the "
                        "title, and big files are usually the popular 4K ones. Review what "
                        "this rule flags over a few scans before arming it."
                    ),
                )
            )

    # The same footgun through the built-in signal, which had no warning at all while the
    # hand-written equivalent above got a danger one -- and the SignalId.SIZE docstring
    # claimed the UI warned about it (rule 24). Neither shipped default enables it.
    size_signal = next(
        (s for s in body.signals if s.signal is SignalId.SIZE and s.weight > 0), None
    )
    if size_signal is not None:
        warnings.append(
            PolicyWarning(
                field="signals",
                severity="danger",
                message=(
                    f'"Large files" is adding {size_signal.weight} points toward removal. File '
                    "size measures how much space you reclaim, not whether anyone wants the "
                    "title, and big files are usually the popular 4K ones. Review what it "
                    "flags over a few scans before arming it."
                ),
            )
        )

    # A rule written on a field this media type cannot read. `Condition.validate_for`
    # checks the lane, the operator and the type, but NOT the media type, so a rule saved
    # before a field was narrowed (``release_age`` and ``quality`` are movie-only: a season
    # has no single release date and mixes episode qualities) keeps validating and simply
    # stops being offered in the editor. Left unsaid, a protection reads as "checked, did
    # not fire" forever, and a removal rule is worse than inert -- its points still count
    # toward the fixed 100-point denominator, so it holds down every score in this policy.
    for anchor, kind, rules in (
        ("protect_conditions", "protection", [(c.field, "") for c in body.protect_conditions]),
        ("custom_condemn", "rule", [(c.field, c.name) for c in body.custom_condemn]),
        ("graded_keeps", "keep rule", [(k.field, k.name) for k in body.graded_keeps]),
    ):
        for field_key, name in rules:
            field_spec = BY_KEY.get(field_key)
            if field_spec is None or body.media_type in field_spec.media_types:
                continue
            called = f' "{name}"' if name else ""
            where = "seasons" if body.media_type == "tv" else "movies"
            warnings.append(
                PolicyWarning(
                    field=anchor,
                    severity="danger",
                    message=(
                        f"Your {kind}{called} uses {field_spec.label}, which Reaper cannot read "
                        f"for {where}, so it never does anything. Remove it here, and set it "
                        "on your other policy instead."
                    ),
                )
            )

    # There was a dilution warning here, telling an owner that a rule written as 20 was
    # really adding about 14. `PolicyBody._weights_total_one_hundred` makes that state
    # unrepresentable: a body whose weights do not total exactly 100 no longer validates,
    # so a weight and the points it adds can never disagree. A warning for a condition
    # that cannot occur is worse than no warning, so it is gone rather than reworded.

    if body.media_type == "tv" and body.keep_last_seasons >= 10:
        warnings.append(
            PolicyWarning(
                field="keep_last_seasons",
                severity="warn",
                message=(
                    f"Keeping the last {body.keep_last_seasons} seasons protects every season of "
                    "most shows, so TV pruning is effectively off: most series have fewer "
                    "seasons than this."
                ),
            )
        )

    # "Requested only" needs Seerr to tell a requested show from an unrequested one.
    # Without it, season_scan._keep_last_applies never sees a Known answer, so it falls
    # back to protecting (Unknown counts as "might be requested") and the floor covers
    # the whole library. That is the safe outcome, and an invisible one: the setting
    # reads as narrower than it behaves. Only worth saying while the floor is on --
    # at 0 seasons the scope decides nothing.
    if (
        body.media_type == "tv"
        and body.keep_last_scope == "requested"
        and body.keep_last_seasons > 0
        and not requests_app_configured
    ):
        warnings.append(
            PolicyWarning(
                field="keep_last_scope",
                severity="warn",
                message=(
                    f"Reaper is keeping the last {body.keep_last_seasons} seasons of every "
                    "show, not just requested ones: telling them apart needs Seerr, and no "
                    'Seerr service is connected. Connect one, or switch this to "All shows" '
                    "so the setting says what actually happens."
                ),
            )
        )

    total_keep = sum(k.max_discount for k in body.graded_keeps)
    if total_keep >= body.condemn_at:
        warnings.append(
            PolicyWarning(
                field="graded_keeps",
                severity="warn",
                message=(
                    f"Your keep rules can subtract up to {total_keep} points, at or above your "
                    f"remove threshold of {body.condemn_at}. Together they could keep almost "
                    "everything. Check the simulator still shows items to remove."
                ),
            )
        )

    return warnings


DEFAULT_MOVIE_POLICY = PolicyBody(
    media_type="movie",
    condemn_at=70,
    gates=(
        GateSetting(gate=GateId.WHITELISTED),
        GateSetting(gate=GateId.STREAMING_NOW),
        GateSetting(gate=GateId.DATA_HORIZON),
        GateSetting(gate=GateId.CURATED_LIST),
        # THE MOST IMPORTANT GATE. Nothing under three years dormant may be deleted at
        # all, whatever else it scores. The measured rewatch rate decays slowly after the
        # first year and its tail never reaches zero (docs/SIGNALS.md, "There is no
        # cliff"): ~30% at two to three years, ~19% past three. Three years is where the
        # odds stop being close to one in three, not where the risk ends. A GATE, not a
        # weight: a weight can be outvoted, and that is exactly how an early version ended
        # up condemning films with a large chance of coming back.
        GateSetting(gate=GateId.MIN_DORMANCY, threshold=1095),
        # "Keep well-rated titles". The bars themselves live in keep_rating_rules below;
        # this setting is only the on/off switch. The default bar is IMDb 7.5 from at
        # least 1,000 votes -- the vote floor is what makes it mean anything, rejecting
        # the handful of films that rate highly on a few hundred votes.
        GateSetting(gate=GateId.RATING_FLOOR),
        # 3 distinct watchers IN THE LAST YEAR. Unwindowed, this protects nearly the
        # whole library and nothing is ever deletable. OTHERS_WATCHING is deliberately
        # absent: it belongs to the requester rule, where "others" means "somebody
        # other than the person who asked". In a general policy it would degenerate
        # into protecting anything ever played by anyone.
        GateSetting(gate=GateId.SERVER_POPULARITY, threshold=3, window_days=365),
    ),
    signals=(
        # Dormancy dominates, and the numbers come from the measured rewatch curve
        # (backtest.FALLBACK_REWATCH_PRIOR): floor at 365 (below which ~61% of films are
        # played again within the year) and saturating at 1825 (beyond which the rate is
        # still ~13%, never zero -- nothing is ever free to delete).
        SignalSetting(signal=SignalId.UNWATCHED, weight=70, saturate_at=1825, floor=365),
        SignalSetting(signal=SignalId.FEW_WATCHERS, weight=20, saturate_at=3),
        SignalSetting(signal=SignalId.LOW_RATING, weight=10, saturate_at=60),
        # SIZE IS DELIBERATELY ABSENT.
        #
        # It measures REWARD (how much you reclaim), not RISK (how unlikely it is to
        # be watched) -- and a 50 GB file is a 4K blockbuster, i.e. exactly the content
        # people do watch. Including it produced a condemned set with WORSE regret than
        # picking randomly among films of the same age (-50% lift): it was condemning
        # popular films precisely because they were large.
        #
        # Size ranks the candidates the score has already chosen. It never decides an
        # item's fate. See docs/SIGNALS.md.
    ),
    # IMDb 7.5 from at least 1,000 votes -- the single bar the original gate carried,
    # now expressed as one entry in the multi-source set. Owners add Rotten Tomatoes,
    # Metacritic or TMDb bars alongside it.
    keep_rating_rules=(RatingRuleSpec(source=RatingSource.IMDB, floor=75, min_votes=1000),),
)


DEFAULT_TV_POLICY = PolicyBody(
    media_type="tv",
    condemn_at=70,
    keep_last_seasons=2,
    keep_first_season=True,
    # The same protections as movies -- a TV season is kept for the same reasons a film is.
    gates=DEFAULT_MOVIE_POLICY.gates,
    signals=(
        SignalSetting(signal=SignalId.UNWATCHED, weight=60, saturate_at=1825, floor=365),
        SignalSetting(signal=SignalId.FEW_WATCHERS, weight=15, saturate_at=3),
        # TV-only: an older season carries more pressure to prune than the newest, but only
        # as a weight -- a much-rewatched season 1 can still out-score its rank. The
        # keep-last-N seasons floor above is the hard guard.
        SignalSetting(signal=SignalId.SEASON_RANK, weight=15, saturate_at=6),
        SignalSetting(signal=SignalId.LOW_RATING, weight=10, saturate_at=60),
    ),
    # IMDb only by default for TV: Sonarr carries no rich ratings object, so a show's
    # IMDb score (from the dataset) is the one bar reliably available. Owners may add
    # Rotten Tomatoes or TMDb bars, which fire only when Plex happens to serve them.
    keep_rating_rules=(RatingRuleSpec(source=RatingSource.IMDB, floor=75, min_votes=1000),),
)


def combine_hashes(*hashes: str) -> str:
    """A stable hash over several policy hashes, in the order given.

    A snapshot is scored under two policies -- one for movies, one for TV -- so its single
    ``policy_hash`` / ``scoring_hash`` is the combination of both, in a fixed order (movie,
    then TV). The simulator recombines the same way to check whether a stored score still
    describes an edited policy, without needing a per-media-type column on the snapshot.
    """
    return hashlib.sha256("".join(hashes).encode("ascii")).hexdigest()
