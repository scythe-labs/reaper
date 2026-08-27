# SPDX-License-Identifier: AGPL-3.0-or-later
"""Policy: the operator's configuration, and the hash that pins it.

Three ideas, each closing a specific way a tool like this can destroy data by
accident.

## 1. Integers only

Every number in a policy body is an integer: a rating floor is stored in tenths
(7.5 becomes ``75``), sizes in bytes, times in epoch seconds, percentages in
basis points. This guards against a real failure: a float does not produce the same bytes on
every machine: ``0.1 + 0.2`` is not ``0.3``, and ``json.dumps`` of a float can
differ across Python versions, so a hash over a policy containing floats would
not be stable. An approval is bound to a policy hash, so an unstable hash would
make approvals void themselves silently, or fail to void themselves when they
should.

Integers also catch a unit mistake at the boundary: typing ``75`` into a field
labeled "IMDb floor (tenths)" must be refused outright. Saving it silently would protect
nothing, since 7.5 would then be compared against a Tomatometer of 96.

## 2. The hash covers meaning

``policy_hash`` is the sha256 of the canonical JSON of the fields that change
what Reaper decides, plus ``schema_version`` and ``scorer_version``. It must never cover
identity: it leaves out ``id``, ``name`` and ``created_at`` on purpose, since renaming a
policy must not void a pending approval, but changing a threshold must.

## 3. Nothing that is "off" is spelled as zero or blank

``0`` never means disabled, and blank never means unlimited. Both are a common
way a half-finished setting becomes an unbounded deletion: another tool,
Janitorr, once let an operator write ``movie-expiration: {100: 10d}`` meaning
"when the library is 100% full," while the code read it as "always." Every
switch here is an explicit ``enabled: false``, and every protective number has a
floor (``min_votes >= 1``, ``grace_days >= 7``).

## What is not here

Two modules read a policy body without being one, and both import this module; it
never imports them back, so the two cannot loop: ``policy_migrations`` loads a body an
older Reaper wrote, and ``policy_warnings`` tells an operator a legal setting is probably
not what they meant.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reaper.engine.fields import (
    BY_KEY,
    Condition,
    FieldType,
    Lane,
    Op,
)
from reaper.engine.gates import (
    GateId,
    RatingRule,
)
from reaper.engine.signals import (
    MAX_SCORE,
    REWATCH_KEEP,
    CustomSignalConfig,
    KeepConfig,
    SignalId,
)
from reaper.ratings import RatingSource, is_percentage_source, source_label
from reaper.refusal import Refusal

SCHEMA_VERSION = 3
"""Bumped when the stored shape changes. 3 marks bodies written after the rating bar moved
off the RATING_FLOOR gate row into ``keep_rating_rules``
(see ``policy_migrations.recover_rating_rules``, which backfills a body written before it)."""

SCORER_VERSION = 5
"""Bumped only when the scorer's meaning changes; a schema field addition alone leaves it
as is.

Both this and ``SCHEMA_VERSION`` sit inside the policy hash, so an item scored under a
different scorer was never approved under this one.

Changing what a scan gathers is what forces a bump. A new ``Facts`` field, signal, or gate
leaves an existing stored body hashing exactly as before, so a plan already approved would
otherwise execute against evidence gathered without that field. Bumping this number is one
way to force a re-scan; rewriting every affected stored body through a loader shim is the
other, since that rewrite moves the hash itself. ``tests/test_scorer_surface.py`` checks
that a change to what a scan gathers is matched by one or the other, so the choice always
gets made deliberately.

Deliberately typed as a plain ``int``, never pinned to a ``Literal``: doing so would
make the next bump fail to load every stored body, and the one caller that reads them
(``services.profiles.active_policy``) has no fallback, so the bump would take out both the
scan path and the policy editor an operator would use to fix it. A body from a newer Reaper
is still refused, below, since that one genuinely cannot be interpreted."""


#: How long a title that came back resists being condemned again, in days. A year and a
#: half.
#:
#: This is a judgment call, written here as one so a later measurement can replace it
#: without digging through history. There is no data yet on how often a deleted title
#: comes back, on this library or any other. The number expresses that a return is worth
#: remembering longer than the dormancy floor (three years by default), so the hold is
#: meaningful without being permanent.
RETURN_HOLD_DAYS = 548

#: How long a title has to be missing before its return counts, in days.
#:
#: Set by measurement, unlike the hold above. Every rating-key change observed on a real
#: library completed within 2.5 to 30 hours, well inside this seven-day window
#: (``docs/LEARNINGS.md``, "The Plex rating key is stable enough to detect a return").
RETURN_ABSENCE_DAYS = 7


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GateSetting(Frozen):
    """One protection, as the operator configures it."""

    gate: GateId
    enabled: bool = True

    threshold: int = 0

    #: There is no ``secondary`` field here. It once held the rating gate's vote floor,
    #: before that bar moved to ``keep_rating_rules``. A stored body can still carry the
    #: key: either a row a migration deliberately skipped, because the number is the last
    #: copy of a rating bar ``policy_migrations.recover_rating_rules`` has yet to restore,
    #: or a body from before the migration ran. Both cases are stripped on load through
    #: ``policy_migrations.drop_retired_gate_keys``. Do not re-add a field here to
    #: accommodate a stored key: ``Frozen`` forbids extra keys on purpose, and the strip is
    #: what keeps that honest.

    window_days: int = Field(default=365, ge=1)
    """How far back "recently" reaches, for gates that count activity.

    There is no way to spell "all time," and that is deliberate. A popularity gate with
    no window would protect anything anyone has ever played, which on a long-lived server
    is very nearly the whole library, since only a fraction of those titles still have
    watchers in the last year. Tested against real history, an unwindowed version
    silently disabled the entire scorer: it found almost nothing to delete at any
    threshold, and the tool looked safe while actually being broken.
    """

    @model_validator(mode="after")
    def _protective_floors(self) -> Self:
        """A protection with a nonsensical bound is a protection that does not fire.

        The rating gate is the one that bites: a vote floor of 0 protects a high rating
        drawn from a handful of votes, and a rating floor of 0 protects literally
        everything, which sounds safe until the operator wonders why Reaper never finds
        anything and "fixes" it by disabling the gate entirely.
        """
        if not self.enabled:
            return self

        # The rating floor lives as a set of per-source bars
        # (``PolicyBody.keep_rating_rules``), each validated by ``RatingRuleSpec``. The
        # RATING_FLOOR gate setting here is only the on/off switch, with no threshold of
        # its own to check.
        if self.gate is GateId.SERVER_POPULARITY and self.threshold < 1:
            raise Refusal("error.policy.gate_popularity_floor_zero")
        if self.gate is GateId.RETURNED and self.threshold < 1:
            raise Refusal("error.policy.gate_returned_floor_zero")
        if self.gate is GateId.MIN_DORMANCY and self.threshold < 5:
            raise Refusal("error.policy.gate_dormancy_floor_low")
        return self


class SignalSetting(Frozen):
    """One weighted reason to delete."""

    signal: SignalId
    weight: int = Field(ge=0, le=100)
    """0 disables the signal. Its weight leaves the denominator too, so the remaining
    scores do not inflate silently."""

    saturate_at: int = Field(ge=1)
    floor: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _floor_below_saturation(self) -> Self:
        if self.floor >= self.saturate_at:
            raise Refusal(
                "error.policy.signal_floor_not_below_saturation",
                floor=self.floor,
                saturate_at=self.saturate_at,
            )
        return self


class ConditionSpec(Frozen):
    """One user-authored protection: keep a title when ``<field> <op> <value>``.

    Protect-only by construction. It is validated against the protect side of the field
    registry, so the worst a mis-authored condition can do is fail to keep something. It
    can never mark a title for removal. That asymmetry is why an operator can safely write
    these (see ``engine.fields``).
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

    Validated against the condemn side of the field registry, so a protect-only field is
    unconstructable, and the worst a mis-authored rule can do is fail to add any weight to
    the score. It can never protect a title, since that is the gates' job. Unsigned like
    every removal signal: a value that cannot be read adds nothing."""

    kind: Literal["boolean"] = "boolean"
    name: str = Field(min_length=1, max_length=60)
    field: str
    op: Op
    value: int | str | bool
    weight: int = Field(ge=0, le=100)
    """0 disables the rule and removes its weight from the denominator, like a built-in signal."""

    @model_validator(mode="after")
    def _valid_condemn_condition(self) -> Self:
        # Reuse the registry's own validation: an unknown field, a protect-only field, or an
        # operator the field does not accept all raise here, with the API's message.
        Condition(field=self.field, op=self.op, value=self.value).validate_for(Lane.CONDEMN)
        return self


class GradedCondemnSpec(Frozen):
    """A user-authored reason to remove that ramps a numeric field, like a built-in signal.

    The field must be numeric and usable on the condemn side. Its weight rises linearly
    from ``floor`` to ``saturate_at`` and is capped at ``weight``."""

    kind: Literal["graded"] = "graded"
    name: str = Field(min_length=1, max_length=60)
    field: str
    weight: int = Field(ge=0, le=100)
    saturate_at: int = Field(ge=1)
    floor: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _valid_graded(self) -> Self:
        if self.floor >= self.saturate_at:
            raise Refusal(
                "error.policy.custom_rule_floor_not_below_saturation",
                floor=self.floor,
                saturate_at=self.saturate_at,
            )
        spec = BY_KEY.get(self.field)
        if spec is None:
            raise Refusal("error.policy.unknown_field", field=self.field)
        if Lane.CONDEMN not in spec.lanes:
            raise Refusal("error.policy.field_wrong_lane_condemn", field=self.field)
        if Op.GTE not in spec.ops:
            raise Refusal("error.policy.custom_rule_not_numeric", field=self.field)
        return self


#: A custom condemnation rule, discriminated on ``kind`` so a policy can carry a mix.
CustomCondemnSpec = Annotated[BooleanCondemnSpec | GradedCondemnSpec, Field(discriminator="kind")]


class GradedKeepSpec(Frozen):
    """A user-authored graded "lean toward keeping": lowers the score, never vetoes.

    A numeric field, ramped from ``floor`` to ``saturate_at``, whose discount is a fixed
    number of score points, unlike a built-in signal's share of the total. Fail-closed: a
    value that cannot be read keeps the title fully. It can only ever lower a score, and
    can never un-protect a title a gate already
    protected, since the verdict checks protection before it ever reads the score. Any
    numeric field may drive a keep, including a protect-only one such as all-time watchers
    or vote count, which is the point."""

    name: str = Field(min_length=1, max_length=60)
    field: str
    value: str | None = Field(default=None, max_length=200)
    """For a membership field (``on_list``): the list's name. This keep is binary:
    being on the list takes the full ``max_discount``, being off it takes none, and a
    membership that could not be read takes the full discount, fail-closed like every
    keep. ``None`` for every numeric field, whose ramp is below."""
    max_discount: int = Field(ge=1, le=100)
    """Points to subtract at full strength. ``ge=1``: "off" means omitting
    the rule, never setting it to zero."""
    floor: int = Field(ge=0)
    saturate_at: int = Field(ge=1)
    direction: Literal["high_keeps", "low_keeps"] = "high_keeps"
    """Which end of the ramp keeps: a high all-time-watcher count keeps (``high_keeps``); a
    low value keeps (``low_keeps``)."""

    @model_validator(mode="after")
    def _valid_keep(self) -> Self:
        spec = BY_KEY.get(self.field)
        if spec is None:
            raise Refusal("error.policy.unknown_field", field=self.field)
        if spec.key == "on_list":
            # The membership form is binary, so the ramp fields below are unused and
            # unvalidated.
            #
            # This must key on the specific field, never on its shape: a shape match alone
            # is not enough, since a text-and-multi field like `genre` also matches, and if it
            # reached here it would validate and explain itself as though genre were a
            # list. The editor only offers this form for `on_list`, so the only way to
            # reach it with another field is an imported or restored body, or the API.
            if not (self.value or "").strip():
                raise Refusal("error.policy.keep_rule_missing_list")
            return self
        if spec.type is FieldType.TEXT and spec.multi:
            raise Refusal("error.policy.keep_rule_not_a_list", field=self.field)
        if self.value is not None:
            raise Refusal("error.policy.keep_rule_unexpected_list_value", field=self.field)
        if self.floor >= self.saturate_at:
            raise Refusal(
                "error.policy.keep_rule_floor_not_below_saturation",
                floor=self.floor,
                saturate_at=self.saturate_at,
            )
        if Op.GTE not in spec.ops:
            raise Refusal("error.policy.keep_rule_not_numeric", field=self.field)
        return self


class RatingRuleSpec(Frozen):
    """One "keep it if it clears this bar" rule, for one rating source.

    Integers only, like every policy number, so the hash stays byte-stable. ``floor`` is
    stored in tenths (7.5 becomes 75) and reads the same way for a percentage source (75%
    becomes 75), since Plex normalizes an 84% score to 8.4, whose tenths are 84.
    ``min_votes`` only means something for a source that counts votes (IMDb, TMDb). For a
    percentage source (Rotten Tomatoes, Metacritic) it must be 0, because a vote floor
    there would silently do nothing.
    """

    source: RatingSource
    floor: int = Field(ge=1, le=100)
    min_votes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _vote_floor_matches_the_source(self) -> Self:
        if is_percentage_source(self.source):
            if self.min_votes != 0:
                raise Refusal(
                    "error.policy.rating_vote_floor_on_percentage",
                    source=source_label(self.source),
                )
        elif self.min_votes < 1:
            # A rating floor with no vote floor protects a high rating drawn from a
            # handful of votes, a number that means nothing.
            raise Refusal("error.policy.rating_vote_floor_zero", source=source_label(self.source))
        return self


class PolicyBody(Frozen):
    """The hashed, immutable part of a policy.

    Everything here changes what Reaper would decide. Anything that changes only how much
    it may do (a cap) or how long a flagged title shows as leaving (grace) lives on the
    Profile instead, so tightening a cap does not void every pending approval.
    """

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1, le=SCHEMA_VERSION)

    scorer_version: int = Field(default=SCORER_VERSION, ge=1, le=SCORER_VERSION)
    """Which scorer produced the numbers under this policy. See ``_pin_to_the_running_scorer``
    for why this is not simply whatever value the stored row had."""

    @model_validator(mode="after")
    def _pin_to_the_running_scorer(self) -> Self:
        """This field always tracks the running code, never the stored row.

        A body is loaded, then this value is overwritten with the current
        ``SCORER_VERSION``. Without that, a body saved under an older scorer would keep
        that scorer's number forever, so its hash would look identical before and after a
        scorer bump. An approval made under the old scorer would then still match, and the
        executor could delete files using the new scorer's numbers without ever asking for
        a re-scan. A body from a genuinely newer Reaper is still refused by ``le`` above,
        since that one cannot be interpreted at all.

        Pinning it here is what makes a scorer bump actually void every pending approval
        and route the simulator to a frozen-facts replay. The operator re-scans, which is
        the point.

        This must use ``object.__setattr__`` rather than return a modified copy: the model
        is frozen, so an "after" validator that returns anything but
        ``self`` is silently ignored when the model is constructed directly. Returning
        a copy would pin the value for a body loaded from the database but not for one
        built in code.
        """
        if self.scorer_version != SCORER_VERSION:
            object.__setattr__(self, "scorer_version", SCORER_VERSION)
        return self

    #: Gates that shipped, could not fire, and were retired. Their ``GateId`` stays alive so
    #: a stored explanation written while they existed still decodes (``engine.gates``
    #: records what each retirement would need to bring it back), but a policy body may no
    #: longer carry one. Without this, a stored policy naming a retired protection would
    #: name something ``scan_runner.build_gates`` has no implementation for, and a scan
    #: correctly refuses rather than silently skipping it, which is exactly why the body has
    #: to be cleaned first.
    #:
    #: Every retired id belongs here, not only the one whose retirement created this set.
    #: A retired id left out of this set, even one that never shipped in a default policy,
    #: could still reach a stored body through the API and take that install's scans offline
    #: permanently with no way to self-heal. ``tests/test_policy.py`` checks the membership
    #: so a future retirement cannot be left out by accident.
    RETIRED_GATES: ClassVar[frozenset[GateId]] = frozenset(
        {GateId.UNMANAGED, GateId.OTHERS_WATCHING}
    )

    @model_validator(mode="after")
    def _drop_retired_gates(self) -> Self:
        """Silently drop a retired gate from a body that still names one.

        This does not flag a repair and does not degrade the scan. A retired gate could
        never keep a file, so dropping it withdraws no protection and no scan run under it
        was ever untrustworthy. Degrading here would make the first scan after an upgrade
        un-plannable for every install, over a protection that was never doing anything.

        It moves ``policy_hash``, which voids a plan approved before the upgrade and asks
        for a re-scan, and it moves ``scoring_hash`` too, so the simulator loses its
        stored-score tier for that edit. It does not move ``evidence_hash``: no retired gate
        is the popularity gate, so what a scan gathers is unchanged and a frozen-facts
        replay can still answer. The first Policy page after such an upgrade shows numbers.

        The one thing this must not do is let a surface blame the operator for it. The
        simulator's stale notice must state the condition, never imply the operator
        changed something (``PolicySimulator.tsx``, and the matching ``stale_reason`` in
        ``api.simulate``), since no post-upgrade code can reproduce the pre-upgrade hash to
        compare against.
        """
        kept = tuple(g for g in self.gates if g.gate not in self.RETIRED_GATES)
        if len(kept) != len(self.gates):
            object.__setattr__(self, "gates", kept)
        return self

    @model_validator(mode="after")
    def _rewatch_odds_row(self) -> Self:
        """Every body carries the rewatch-odds row, appended on load when missing.

        A gate's switch renders from the body's own gate list, so a body stored before
        this gate existed would never show its control without this append. It is
        appended disabled, at the shipped starting percentage, so it changes no verdict
        until the operator turns it on. This is not a repair and does not degrade the
        scan, since nothing was protecting before and nothing is withdrawn now. It moves
        ``policy_hash`` for every stored body lacking the row, which voids a pre-upgrade
        approval, and it moves ``scoring_hash`` too. It leaves ``evidence_hash`` alone,
        the same as the drop above.

        This applies to both movies and TV: the season lane freezes the gate's two cohort
        facts for every season, so the gate can fire there as well.
        """
        if all(g.gate is not GateId.REWATCH_ODDS for g in self.gates):
            object.__setattr__(
                self,
                "gates",
                (
                    *self.gates,
                    GateSetting(gate=GateId.REWATCH_ODDS, enabled=False, threshold=25),
                ),
            )
        return self

    @model_validator(mode="after")
    def _returned_row(self) -> Self:
        """Every body carries the came-back row, appended on load when missing.

        The row above's twin, for the same reason and on the same terms: a gate's switch
        renders from the body's own gate list, so a body stored before this protection
        existed would never show it otherwise. Appended disabled, at the shipped lengths,
        so it changes no verdict until the operator turns it on. Not a repair and no
        degrade, because nothing was protecting and nothing is withdrawn.

        Off in the shipped defaults too, so the same sentence is true of every install
        (``DEFAULT_MOVIE_POLICY`` carries why).

        Moves ``policy_hash`` for every stored body lacking the row, which voids a
        pre-upgrade approval, and moves ``scoring_hash`` too. It does not move
        ``evidence_hash``, unlike an enabled row would: ``returned_absence_days`` reads
        this gate's window only while the gate is on, so a disabled row falls back to the
        shipped default and gathers exactly what it gathered before.
        """
        if all(g.gate is not GateId.RETURNED for g in self.gates):
            object.__setattr__(
                self,
                "gates",
                (
                    *self.gates,
                    GateSetting(
                        gate=GateId.RETURNED,
                        enabled=False,
                        threshold=RETURN_HOLD_DAYS,
                        window_days=RETURN_ABSENCE_DAYS,
                    ),
                ),
            )
        return self

    media_type: Literal["movie", "tv"] = "movie"

    condemn_at: int = Field(ge=1, le=100)
    """Score at or above which an item is a candidate. Never 0: a threshold of 0 would
    condemn everything the gates do not save."""

    coverage_floor_bp: int = Field(default=5000, ge=0, le=10_000)
    """Minimum share of signal weight Reaper must actually have evaluated, in basis points
    (5000 = 50%). Below this share the item is held back, guarding against condemning an
    item Reaper can barely see."""

    keep_last_seasons: int = Field(default=2, ge=0, le=1_000)
    """Season pruning: the N most recent seasons of a show are protected outright,
    whatever they score. Movies ignore this. A hard floor that sits outside the weighted
    score: ``0`` means "keep no season on age alone" (the other guards and the score still
    apply). See ``services.season_pruning``.

    The ceiling is arithmetic hygiene, the same on
    all three season numbers here: it sits far above any real setting, so it can never
    invalidate a stored body. ``api.schemas.PolicyIn`` declares the same ceiling, and
    ``tests/test_policy.py::TestTheTwoPolicyDeclarationsAgree`` fails if the two drift
    apart."""

    keep_first_season: bool = True
    """Season pruning: protect the first content-bearing season of every show, so a
    library never throws away the pilot that lets a new viewer start the show. On by
    default; movies ignore it."""

    keep_last_scope: Literal["all", "requested"] = "all"
    """Whether the keep-last-N floor applies to every show (``all``) or only to shows
    someone requested (``requested``). Fail-closed: under ``requested``, when Reaper cannot
    tell whether a show was requested, the floor still applies. Not knowing counts as
    "might be requested"."""

    season_lookahead: int = Field(default=0, ge=0, le=1_000)
    """How many seasons beyond a viewer's current position to also protect while they
    binge. ``0`` protects exactly the season they are mid-way through, or the next one if
    they have finished the current one. Movies ignore it.

    ``season_pruning.sequential_protections`` builds a range from this value once per
    anchor, per viewer, per show, so the ceiling keeps that from growing unbounded inside
    the event loop that serves ``/policy/simulate``."""

    keep_in_progress: bool = True
    """Season pruning: protect the season a viewer is partway through, and the next one
    once they finish it, through the sequential-progression guard in
    ``services.season_pruning``. On by default; turning it off removes that guard
    entirely. Movies ignore it."""

    in_progress_hold_days: int = Field(default=180, ge=0, le=36_500)
    """How long a viewer's place in a show is held after their last watch of that show.
    Past this many days without watching, the show counts as abandoned by that viewer and
    their half-finished season no longer protects anything. ``0`` holds forever. A viewer
    whose last-watched time cannot be read keeps their hold, fail closed. Only meaningful
    while ``keep_in_progress`` is on; movies ignore it.

    This is a span the guard claims to cover, not a bound on the watch history. Setting it
    past how far the watch history reaches, including ``0``, which no finite history can
    cover, makes that claim unsupportable, so ``gates.progress_is_establishable`` then
    holds every season on disk instead of treating an unseeable viewer as one who has
    stopped watching.

    The ceiling is 100 years, well past any watch history and already covered by ``0``."""

    keep_specials: bool = True
    """Season pruning: never remove specials (Season 0). On by default. When off, specials
    are judged like any other season and can be condemned by score, but they still never
    occupy a keep-last slot, and the airing and still-downloading guards still apply."""

    protect_incomplete_seasons: bool = True
    """Season pruning: keep a season Sonarr has not finished downloading, one that wants an
    aired episode it does not have yet, so a removal never fights an in-progress download.
    On by default. When off, a partly-downloaded season is judged like any other, which is
    useful for an ended show Sonarr permanently lists as missing an episode. The airing
    guard is separate and still applies. Movies ignore it. See
    ``services.season_pruning``."""

    flag_keep_conflicts: bool = True
    """Season pruning: when a season the keep rule would remove was watched by more people
    than a season it keeps, block it as "Needs a look" instead of removing it. On by
    default. When off, the keep rule is followed without flagging."""

    gates: tuple[GateSetting, ...]
    signals: tuple[SignalSetting, ...]

    protect_conditions: tuple[ConditionSpec, ...] = ()
    """The operator's own protections, on top of the built-in gates. Each keeps a title
    when it matches, and together they are an OR: any one is enough. Protect-only, see
    ConditionSpec."""

    custom_condemn: tuple[CustomCondemnSpec, ...] = ()
    """The operator's own reasons to remove a title, on top of the built-in signals. Each
    is an unsigned signal, a boolean bonus or a graded ramp, that joins the same fixed
    denominator, so a value that cannot be read can only lower the score. Never a
    protection, see BooleanCondemnSpec."""

    graded_keeps: tuple[GradedKeepSpec, ...] = ()
    """The operator's own graded reasons to keep a title: a discount applied after the
    score, fail-closed. A softer companion to a hard protect condition. It lowers a score
    but never vetoes, and missing data keeps the file. See GradedKeepSpec."""

    rewatch_keep_enabled: bool = True
    """The built-in rewatch keep: a title watched at least ``rewatch_min_viewings`` times,
    most recently within ``rewatch_recent_days``, has its score lowered by
    ``rewatch_keep_discount`` points. A soft keep exactly like a ``graded_keeps`` row,
    never a protection. Live on both movie and TV policies, with the count meaning whole
    re-watches of the show on a TV body (``rewatch_min_viewings`` below). A stored body
    from before these fields existed loads with the defaults; an explicit ``False`` is the
    operator's own choice and is honored."""

    rewatch_keep_discount: int = Field(default=20, ge=1, le=50)
    """Points subtracted while the rewatch condition holds. ``ge=1``: off means using the
    switch above, never setting this to zero."""

    rewatch_min_viewings: int = Field(default=10, ge=1, le=1_000)
    """The viewing-count bar, whose unit depends on the media type. On a movie body:
    qualified viewings, plays by any user inside ``services.rewatch.VIEWING_GAP_DAYS`` of
    each other clustered as one. On a TV body: whole re-watches of the show
    (``services.rewatch.replay_period_count``), where ``DEFAULT_TV_POLICY`` sets the bar
    to 2. Both defaults are starting values from a backtest on one heavy-rewatch library
    (``docs/LEARNINGS.md``): a quieter library is untested, and
    loosening the bar buys coverage at the cost of some precision. The ceiling is
    arithmetic hygiene, well past any real setting."""

    rewatch_recent_days: int = Field(default=730, ge=1, le=36_500)
    """How recent the last qualified play must be for the keep to fire. Same backtested
    starting value and the same 100-year ceiling as ``in_progress_hold_days``."""

    # No `keep_tags` / `keep_tags_match` fields here: the *arr tags that spare a title are
    # a list now, defined once on Settings -> Lists, protecting through an `on_list` keep
    # rule like every other list. `policy_migrations.convert_list_protections` carries a
    # stored body's tags into that shape on load.

    keep_rating_rules: tuple[RatingRuleSpec, ...] = ()
    """The per-source bars behind "Keep well-rated titles" (the RATING_FLOOR gate). A title
    clearing any of them, or all of them under ``keep_rating_match``, is kept whatever it
    scores. Empty means the protection keeps nothing. Movies can back every source, since
    Radarr carries them; TV backs IMDb plus whatever Plex serves for the show."""

    keep_rating_match: Literal["any", "all"] = "any"
    """Whether a title needs to clear any one rating bar (the usual case) or all of them."""

    @model_validator(mode="after")
    def _weights_total_one_hundred(self) -> Self:
        """Every removal weight, built-in and operator-authored, sums to exactly 100.

        This is what makes a weight mean points. ``signals.score`` normalizes by the sum
        of enabled weights, so a weight is a share of a running total: at a total of 140,
        a rule written as 20 delivers about 14, and adding a second rule shrinks the
        first. Pinning the total at ``MAX_SCORE`` makes the score formula collapse to the
        raw sum of weight actually earned, so the number an operator types is the number
        the score moves by, matching the keep lane, whose discounts are already literal
        points.

        This must check equality, never "at most 100": allowing less than 100 would stretch
        the remaining weight the same way going over it would shrink it, just in the other
        direction, and an outage touching both lanes could then raise the score net,
        because keeps stay absolute points while the removal side would be stretched.

        Both shipped defaults already total exactly 100, so this changes no score by
        itself. It also closes one real gap: turning a signal's weight to 0 would otherwise
        drop it from the denominator and raise every remaining signal's score. Because the
        total must stay at 100, those points always move to another signal instead, so the
        denominator never moves.
        """
        total = sum(s.weight for s in self.signals) + sum(c.weight for c in self.custom_condemn)
        if total != MAX_SCORE:
            over = total - MAX_SCORE
            if over > 0:
                raise Refusal("error.policy.weights_over_100", total=total, amount=over)
            raise Refusal("error.policy.weights_under_100", total=total, amount=-over)
        return self

    # A validator refusing an all-zero policy once lived here. A total of exactly 100 can
    # never be reached with every weight at 0, so the check above already makes that case
    # impossible, and the extra validator was removed as redundant.

    @model_validator(mode="after")
    def _no_duplicates(self) -> Self:
        if len({g.gate for g in self.gates}) != len(self.gates):
            raise Refusal("error.policy.duplicate_gate")
        if len({s.signal for s in self.signals}) != len(self.signals):
            raise Refusal("error.policy.duplicate_signal")
        names = [c.name for c in self.custom_condemn]
        if len(set(names)) != len(names):
            raise Refusal("error.policy.duplicate_custom_rule_name")
        # A custom rule may not take a built-in signal's id as its name. The stored score
        # breakdown identifies rows by that string, so a rule named "unwatched" would
        # collide with the built-in row. The why-panel would drop its "Your rule" tag and
        # render two rows under one key, and the audit record could not tell them apart.
        builtin = {s.value for s in SignalId}
        for name in names:
            if name in builtin:
                raise Refusal("error.policy.custom_rule_name_collides", name=name)
        keep_names = [k.name for k in self.graded_keeps]
        if len(set(keep_names)) != len(keep_names):
            raise Refusal("error.policy.duplicate_keep_rule_name")
        # The stored explanation identifies keep rows by name, and the built-in rewatch keep
        # rides in the same list, so this prevents the same collision the custom-rule check
        # above does.
        if REWATCH_KEEP in keep_names:
            raise Refusal("error.policy.keep_rule_name_collides_rewatch", name=REWATCH_KEEP)
        rating_sources = [r.source for r in self.keep_rating_rules]
        if len(set(rating_sources)) != len(rating_sources):
            raise Refusal("error.policy.duplicate_rating_source")
        return self

    def popularity_window_days(self) -> int:
        """The window the recent-watchers fact counts over, in days.

        Reads the SERVER_POPULARITY gate's window only while that gate is enabled. A
        disabled gate's leftover window must not keep steering the ``distinct_watchers``
        fact, since a short stale window would quietly raise FEW_WATCHERS pressure across
        the whole library. Falls back to the 365-day default otherwise. Every reader of
        the window comes through this method, so the default lives in exactly one place.
        """
        return next(
            (g.window_days for g in self.gates if g.gate is GateId.SERVER_POPULARITY and g.enabled),
            365,
        )

    def returned_absence_days(self) -> int:
        """How long a title must be missing before its return counts, in days.

        The twin of ``popularity_window_days``, and read the same way: the RETURNED
        gate's window only while that gate is enabled, so a disabled gate's leftover
        number cannot steer what the scan writes into the ledger. Falls back to the
        shipped default otherwise, and every reader comes through this method.

        This is a gathering value: it decides what the scan records as
        a return, and the gate later reads the recorded answer. That is why it belongs in
        ``_gathering_evidence``, while the hold's length belongs in the judging half.

        It is a gathering value whether the gate is on or off, because the ledger is
        written on every scan either way (``services.library_seen``). That is what makes
        the protection useful the moment the operator turns it on: Reaper
        can only notice a title coming back if it saw the title before it left. So this
        number is always in the hash, and switching the gate off means gathering under
        the shipped default.
        """
        return next(
            (g.window_days for g in self.gates if g.gate is GateId.RETURNED and g.enabled),
            RETURN_ABSENCE_DAYS,
        )

    def rating_rules(self) -> tuple[RatingRule, ...]:
        """Translate the per-source keep bars into engine rating rules for the gate.

        The one place the policy's rating specs become a gate input, mirroring how
        ``keep_configs``/``custom_signal_configs`` translate their own specs, so the
        engine gate never has to import the policy layer."""
        return tuple(
            RatingRule(source=r.source, floor=r.floor, min_votes=r.min_votes)
            for r in self.keep_rating_rules
        )

    def keep_configs(self) -> list[KeepConfig]:
        """Translate the graded-keep specs into engine keep configs for ``score()``.

        The built-in rewatch keep must be appended here, never at each call site: this
        method has two callers, ``services.snapshot.scan`` and the
        simulator replay in ``api.simulate``, and appending it at only one of them would
        make the other silently drop it. This applies to both movies and TV, with
        ``media_type`` on the config picking the right wording for the lane.
        """
        configs = [
            KeepConfig(
                name=k.name,
                max_discount=k.max_discount,
                field=k.field,
                value=k.value,
                floor=k.floor,
                saturate_at=k.saturate_at,
                direction=k.direction,
            )
            for k in self.graded_keeps
        ]
        if self.rewatch_keep_enabled:
            configs.append(
                KeepConfig(
                    name=REWATCH_KEEP,
                    max_discount=self.rewatch_keep_discount,
                    field=REWATCH_KEEP,
                    # The flat rewatch arm reads neither ramp bound; its bars ride below.
                    floor=0,
                    saturate_at=1,
                    min_viewings=self.rewatch_min_viewings,
                    recent_days=self.rewatch_recent_days,
                    media_type=self.media_type,
                )
            )
        return configs

    def custom_signal_configs(self) -> list[CustomSignalConfig]:
        """Translate the custom-condemn specs into engine configs for ``score()``.

        The one place the policy's specs become scoring inputs, mirroring how ``signals``
        become ``SignalConfig``s at the call site, so ``score()`` never has to import the
        policy layer."""
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

        Sorted keys, tight separators, and integers-only together make this an exact
        canonicalization: the same policy always produces the same bytes, on any machine,
        in any Python version.
        """
        payload = self.model_dump(mode="json")
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def policy_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("ascii")).hexdigest()

    #: The fields applied to an item after it has been scored, by comparing the stored
    #: score and coverage against a number. Changing one of these re-decides a snapshot
    #: without re-reading anything. Changing anything else does not.
    _POST_SCORE_FIELDS: ClassVar[frozenset[str]] = frozenset({"condemn_at", "coverage_floor_bp"})

    #: Pure bookkeeping: this cannot change a score, a verdict, or what a scan
    #: gathers, so it is excluded from both simulator hashes below. ``policy_hash`` still
    #: covers it, so an approval stays bound to the exact body it was planned under.
    #:
    #: ``schema_version`` belongs here because it is the storage shape, and the wire schema
    #: sent to the browser does not carry it. A body round-tripped through ``api.schemas``
    #: would come back stamped with the current code's default while the stored row kept
    #: the older number, so the two simulator hashes would differ forever: the scan
    #: recorded the stored body's hash, the simulator computed the round-tripped one, and
    #: no scan could ever clear the mismatch. A hash that decides whether a feature answers
    #: at all must cover only fields that actually change the answer.
    _NON_BEHAVIORAL_FIELDS: ClassVar[frozenset[str]] = frozenset({"schema_version"})

    def scoring_hash(self) -> str:
        """Identifies the policy's scoring behavior, ignoring the thresholds.

        Two policies with the same scoring hash assign every item the same score and the
        same gate outcomes. They may still disagree about the verdict, because
        ``condemn_at`` and ``coverage_floor_bp`` are compared against those results
        afterwards.

        This is the first of the simulator's three tiers, and what makes its
        zero-API-call path honest. Re-deciding a stored snapshot at a new threshold is
        exact, so while this hash matches, re-comparing the stored scores is enough. When
        it differs, the stored scores were produced by different weights or gates and
        cannot be reused, so the simulator falls through to ``evidence_hash``. That hash
        decides whether the frozen Facts can be replayed under the edited policy (tier 2,
        still exact and still zero API calls), or whether the edit changed what a scan
        would gather at all, in which case the simulator must refuse to report numbers,
        never report confident, stale ones (tier 3). The three tiers are enumerated in
        ``api.simulate.simulate``.

        ``scorer_version`` is deliberately still covered here: if the scorer itself
        changed, the stored scores are not comparable, and this hash must reflect that so
        a scorer bump routes to tier 2.
        ``schema_version`` is not covered, per ``_NON_BEHAVIORAL_FIELDS``.
        """
        payload = {
            k: v
            for k, v in self.model_dump(mode="json").items()
            if k not in self._POST_SCORE_FIELDS and k not in self._NON_BEHAVIORAL_FIELDS
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()

    #: Fields a frozen-facts replay reproduces exactly, so a change to any of them does not
    #: need a fresh scan: the scorer and the protect and rating gates are pure functions of
    #: the frozen Facts. Everything else falls into the evidence hash instead. This is an
    #: allow-list, never a deny-list, so a field nobody classified yet defaults to
    #: needing a fresh scan, which is safe, never to a replay that could preview
    #: wrong. ``gates`` is here, but only ever through ``_gathering_evidence`` below: a gate
    #: decides what to make of an item, and every fact it reads is gathered whether or not
    #: it is enabled. The one exception is the popularity window, the span
    #: ``distinct_watchers`` is counted over, which is folded back in as its own key.
    #: ``scorer_version`` belongs here for the same reason the weights do: a replay runs the
    #: current ``score``/``evaluate_all``/``decide_verdict`` over the frozen Facts, so a new
    #: scorer's answer is reproduced exactly. It also stays in ``scoring_hash``, which is
    #: what routes a scorer bump to the replay.
    #:
    #: The nine season fields are here too, and they are the one case ``facts_json`` alone
    #: cannot answer. A season's guard result is decided per show, from Sonarr's season
    #: statistics and who is part-way through it, inputs that never reached ``Facts``. So
    #: the scan freezes those inputs separately (``db.models.SeasonPruneEvidence``), and the
    #: replay re-derives the plan through the same ``season_evidence.plan_from_frozen`` the
    #: scan used. This buys a hash that now allows a season rule edit to replay. It
    #: leaves two things unanswered: a snapshot with no bundle, and turning the mid-binge
    #: hold on over a scan that never read Sonarr's episode lists, since a hash cannot say
    #: why it mismatched. Both are asked of the stored evidence itself in
    #: ``api.simulate._season_guard_replay``, which lets the panel point to the one control
    #: at fault, narrowing down from all nine.
    _EVIDENCE_REPLAYABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "condemn_at",
            "coverage_floor_bp",
            "scorer_version",
            "signals",
            "custom_condemn",
            "graded_keeps",
            # The rewatch keep's four knobs judge two frozen observations
            # (``Facts.rewatch_viewings`` / ``rewatch_last_play_days``), so an edit replays
            # exactly, like every other keep. Classifying them as evidence would force a
            # fresh scan on every strength edit forever. One accepted gap: a replay over a
            # snapshot frozen before this keep existed reads both observations as unable to
            # be checked, so the preview takes the full discount (toward keeping, shown as
            # "couldn't check") until the first scan after the upgrade.
            "rewatch_keep_enabled",
            "rewatch_keep_discount",
            "rewatch_min_viewings",
            "rewatch_recent_days",
            "keep_rating_rules",
            "keep_rating_match",
            "protect_conditions",
            "gates",
            "keep_last_seasons",
            "keep_first_season",
            "keep_last_scope",
            "season_lookahead",
            "keep_in_progress",
            "in_progress_hold_days",
            "keep_specials",
            "protect_incomplete_seasons",
            "flag_keep_conflicts",
        }
    )

    #: Every field of a gate row, split by whether a scan can read it before it freezes an
    #: item's Facts. ``window_days`` is the one that can: it is the span
    #: ``snapshot._watch_stats`` counts ``distinct_watchers`` over. The others reach the scan
    #: only through ``scan_runner.build_gates``, which the replay calls itself, over facts
    #: that were gathered whether or not the gate asking for them was switched on.
    #:
    #: ``enabled`` is in the judging half and also feeds ``popularity_window_days``. That is
    #: consistent: what it selects there is a window, and the window is the only
    #: thing the gather phase ever learns from this list.
    #:
    #: This must split by field name, never by hashing the whole row: a gate field added
    #: later then lands in neither set, and a drift guard in ``test_policy.py`` fails until
    #: someone classifies it. A new gathering field defaulting quietly into the judging
    #: half would put a confident wrong preview in front of an operator, which is the
    #: failure the three simulator tiers exist to prevent.
    _GATHERING_GATE_FIELDS: ClassVar[frozenset[str]] = frozenset({"window_days"})
    _JUDGING_GATE_FIELDS: ClassVar[frozenset[str]] = frozenset({"gate", "enabled", "threshold"})

    def _gathering_evidence(self) -> dict[str, object]:
        """What ``gates`` tells a scan before it freezes anything.

        Two numbers, and each already carries the enabled flag it depends on: a disabled
        gate falls back to its shipped default, so switching one off gathers exactly what
        it gathered before and the replay stays exact. Switching one on at any other value
        moves this, which is the point: the frozen evidence was taken under a span the
        edited policy no longer asks for.

        The second is the minimum absence a title's return must clear. It belongs in the
        gathering half because it decides what the scan writes into the
        ledger, and the gate later reads that stored answer. Replayed against frozen facts,
        an edited policy would otherwise report a verdict computed under the old number,
        which is exactly the confident wrong preview ``_GATHERING_GATE_FIELDS`` exists to
        prevent.
        """
        return {
            "popularity_window_days": self.popularity_window_days(),
            "returned_absence_days": self.returned_absence_days(),
        }

    def evidence_hash(self) -> str:
        """Identifies what a scan under this policy would GATHER and FREEZE per item.

        Two policies with the same evidence hash produce byte-identical Facts, and the same
        season-pruning guard, for every item. So the simulator can rebuild those Facts from
        ``Candidate.facts_json`` and replay the real ``score``/``evaluate_all``/
        ``decide_verdict`` under the edited policy, exact for any change to a replayable
        field such as weights, rating bars, custom rules, protect conditions, or
        thresholds.

        When it differs, the edit changed the evidence itself, such as the popularity
        window or the media type, so the frozen Facts are stale and a real scan is
        required. The set of replayable fields is an allow-list, so an unclassified field
        falls into this hash and forces the safe, honest fresh scan.

        The scan freezes each season's guard inputs per show (``db.models.SeasonPruneEvidence``),
        and the replay must re-derive the guard from them, never read a stale cached
        result. A matching hash for a TV row promises only that the Facts replay
        exactly. Whether the show's season bundle is present, readable, and describes that
        season is a separate question, answered by ``api.simulate._season_guard_replay``
        reading the stored evidence directly, since a hash cannot say why two snapshots
        that gathered identically might still disagree about what they stored.

        The allow-list default has one sharp edge: a field that is pure bookkeeping still
        falls in here and forces a rescan that can never help, which is what happened to
        ``schema_version`` permanently (see ``_NON_BEHAVIORAL_FIELDS``). Classify a new
        field into one of the three sets when it is added.

        Turning a protection on or off is a judging edit. The fact
        every gate reads is gathered unconditionally, since no fact builder branches on a
        gate's enabled flag, so the scan freezes the same bytes either way and the replay
        answers exactly. Only ``_gathering_evidence`` needs anything from the gate list at
        all.

        Changing what this hash covers costs every stored snapshot one scan. A snapshot
        records the hash its own scan computed (``services.snapshot``), so one written by
        an earlier build cannot match a changed formula, and until the next scan a weight
        or bar edit refuses where it used to replay. It heals on that scan, unlike
        ``schema_version``, which a scan could never heal because each scan wrote the stale
        value back."""
        payload = {
            k: v
            for k, v in self.model_dump(mode="json").items()
            if k not in self._EVIDENCE_REPLAYABLE_FIELDS and k not in self._NON_BEHAVIORAL_FIELDS
        }
        payload |= self._gathering_evidence()
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()


class ProfileSettings(Frozen):
    """The mutable part: how much Reaper may do, and how long a flagged title shows as
    leaving.

    Kept out of the policy hash on purpose. Tightening a cap or lengthening the grace
    period is always safe, and voiding every pending approval because the operator
    reduced a limit would train them to stop reading the diff.

    ``max_unmeasured_per_run`` is out of the hash too, for a different reason, and the
    difference matters: it is the one field here that loosens what may be deleted, so
    "tightening is always safe" does not cover it. It is safe to leave out because of when
    it is read. It is consumed when a plan is built, so raising it cannot add items to a
    plan already approved, and the executor re-reads it at execute time, so lowering it to
    0 after approval causes those items to be kept instead. Both directions resolve toward
    keeping the file, which is why the timing makes the hash unnecessary here.
    """

    #: Four separate caps: max items and max bytes, each enforced per run and again over a
    #: rolling 30 days. The rolling byte cap is what keeps a multi-terabyte incident
    #: out of reach: no sequence of runs is admitted past it, because each run is admitted
    #: only if the whole of it still fits. The per-run caps are enforced by
    #: ``executor._check_caps`` and the rolling 30-day caps by
    #: ``Executor._check_rolling_caps``, both aborting, never truncating, before any send.
    #:
    #: What the byte caps count is what Sonarr and Radarr track: for a movie, the file's own
    #: size. The delete removes the whole folder, so a run can free a little more
    #: than the cap admitted. Measured at 0.2% of a sampled library, mostly from extras
    #: rather than artwork (``snapshot._reported_size``). Read the byte caps as a close
    #: bound. The item caps have no such gap, and neither does the season
    #: side.
    max_items_per_run: int = Field(default=10, ge=1, le=1000)
    max_bytes_per_run: int = Field(default=500 * 1_000_000_000, ge=1)
    max_items_per_30d: int = Field(default=100, ge=1)
    max_bytes_per_30d: int = Field(default=2_000 * 1_000_000_000, ge=1)

    caps_enabled: bool = True
    """Whether the four caps above are enforced at all. On by default, so an install that
    configures nothing still runs bounded. Off, the per-run and 30-day caps stop aborting
    a run, for a big first cleanup, say, while every other gate still stands: the
    deletion password, the typed confirmation, the frozen-manifest re-check, the canary,
    and the live per-item vetoes. It never touches ``max_unmeasured_per_run``, which is
    a separate keep-unknown-size rule, unrelated to run size. Left out of the policy hash
    like the caps, since it only ever loosens, and the executor re-reads it at execute
    time."""

    grace_days: int = Field(default=14, ge=7)
    """How long a condemned item is shown as leaving, so an operator's users can catch it.

    A notice window only: nothing on the deletion path reads it (see the module
    docstring on ``services/grace.py``), so it drives the Leaving Soon shelf and the
    Discord notice and never delays a send. Floored at 7, since a countdown shorter than
    a week is one nobody can realistically act on."""

    max_unmeasured_per_run: int = Field(default=0, ge=0, le=25)
    """How many items with no known size a single run may delete. ``0``, the default,
    means never: an item Reaper cannot measure is held back
    (``planner.build_plan``).

    Expressed as a count, because an unmeasured item contributes nothing
    to either byte cap, since there is nothing honest to add, so the byte caps cannot
    bound this population at all. The count is the bound instead, which is also why the
    ceiling is low.

    Whatever it is set to, three things do not move: an unmeasured item never sorts
    first, so the run's test item is always one whose cost is known; unmeasured items
    still count against the item caps; and a plan wanting more than the allowance must
    abort, never truncate: truncating would let sort order pick which
    unmeasured file dies."""

    @model_validator(mode="after")
    def _run_cap_within_rolling_cap(self) -> Self:
        if not self.caps_enabled:
            # The caps are off, so the relationships between them constrain nothing.
            # Checking them here would reject legal combinations, such as a run cap above
            # the rolling cap, that can never fire while enforcement is off.
            return self
        if self.max_items_per_run > self.max_items_per_30d:
            raise Refusal(
                "error.policy.run_cap_exceeds_rolling_cap_items",
                run_cap=self.max_items_per_run,
                rolling_cap=self.max_items_per_30d,
            )
        if self.max_bytes_per_run > self.max_bytes_per_30d:
            raise Refusal("error.policy.run_cap_exceeds_rolling_cap_bytes")
        if self.max_unmeasured_per_run > self.max_items_per_run:
            raise Refusal(
                "error.policy.unmeasured_cap_exceeds_run_cap",
                unmeasured_cap=self.max_unmeasured_per_run,
                run_cap=self.max_items_per_run,
            )
        return self


def join_and(parts: list[str]) -> str:
    """Join as `"a", "b" and "c"`. The conjunctive twin of `fields._join_or`. Both exist
    because an operator should be able to read the result at a glance, unlike a
    comma-joined dump.

    Public: another module needing the same phrasing must import this, never write its own
    copy. `policy_warnings.inspect` and `services/scan_runner.py` both use it.
    It is a sentence builder, kept apart from `inspect`'s warnings: `scan_runner` uses it to
    join repair remedies, which have nothing to do with a dangerous configuration."""
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


#: The names of the two lists a fresh install is seeded with (``list_config``), spelled
#: here because the default policies' keep rules below name them, and the engine must not
#: import the service layer. An upgraded body's rules point at the same rows without
#: reading these constants: ``policy_migrations.conversion_list_names`` finds the rows by
#: source, preset, and tag, so it works whatever the seeded row is actually named.
DEFAULT_TAG_LIST_NAME = "Titles you've tagged"
DEFAULT_IMDB_LIST_NAME = "IMDb Top 250"

#: The keep rules a fresh install starts with: the seeded lists keep their titles
#: outright. Softening one to a lean, or removing it, is a per-list choice on Policy.
#:
#: Scoped by the media type each list can hold. The tag list holds both movies (Radarr)
#: and shows (Sonarr) under the one keep tag, so both policies name it. The IMDb chart is
#: movies only, since ``services/lists.py`` hardcodes ``media_type="movie"`` for it, so a
#: TV rule naming it could never match a season: it would protect nothing while reading as
#: a protection the operator never chose. So the TV default names the tag list alone.
DEFAULT_TAG_CONDITION = ConditionSpec(field="on_list", op=Op.EQ, value=DEFAULT_TAG_LIST_NAME)
DEFAULT_IMDB_CONDITION = ConditionSpec(field="on_list", op=Op.EQ, value=DEFAULT_IMDB_LIST_NAME)
DEFAULT_MOVIE_LIST_CONDITIONS: tuple[ConditionSpec, ...] = (
    DEFAULT_TAG_CONDITION,
    DEFAULT_IMDB_CONDITION,
)
DEFAULT_TV_LIST_CONDITIONS: tuple[ConditionSpec, ...] = (DEFAULT_TAG_CONDITION,)


DEFAULT_MOVIE_POLICY = PolicyBody(
    media_type="movie",
    condemn_at=70,
    protect_conditions=DEFAULT_MOVIE_LIST_CONDITIONS,
    gates=(
        GateSetting(gate=GateId.STREAMING_NOW),
        GateSetting(gate=GateId.DATA_HORIZON),
        # The most important gate. Nothing dormant for under three years may be deleted
        # at all, whatever else it scores. The measured rewatch rate decays slowly after
        # the first year and never reaches zero (docs/SIGNALS.md, "There is no cliff"):
        # about 30% of films are watched again in the two-to-three-year range, and about
        # 19% are watched again past three years. Three years is where the odds drop
        # below roughly one in three; the risk of a rewatch never fully ends. This must be
        # a gate, never a weight: a weight can be outvoted by the rest of the
        # score, and that is how an early version of this scorer ended up condemning
        # films with a large chance of coming back.
        GateSetting(gate=GateId.MIN_DORMANCY, threshold=1095),
        # "Keep well-rated titles." The bars themselves live in keep_rating_rules below;
        # this setting is only the on/off switch. The default bar is IMDb 7.5 from at
        # least 1,000 votes. The vote floor is what makes the bar mean anything, since it
        # rejects the handful of films that rate highly on only a few hundred votes.
        GateSetting(gate=GateId.RATING_FLOOR),
        # 3 distinct watchers in the last year. Without a window, this would protect
        # nearly the whole library, and nothing would ever be deletable. OTHERS_WATCHING
        # is deliberately absent here: it belongs to the requester rule, where "others"
        # means someone other than the person who asked. In a general policy it would
        # protect anything ever played by anyone.
        GateSetting(gate=GateId.SERVER_POPULARITY, threshold=3, window_days=365),
        # Off by default: it overlaps the dormancy floor above, and its threshold only
        # means something once the operator has read their own rewatch-odds ladder. 25 is
        # the shipped starting percentage.
        GateSetting(gate=GateId.REWATCH_ODDS, enabled=False, threshold=25),
        # A title that left the library and came back is held for a year and a half.
        # Off by default, for a reason related to the row above: the hold's length is a
        # judgment call, since there is no data yet on how
        # often a deleted title comes back. Off here and off when appended to a stored
        # body, so the same sentence is true of every install, and an upgrade changes
        # nobody's policy.
        GateSetting(
            gate=GateId.RETURNED,
            enabled=False,
            threshold=RETURN_HOLD_DAYS,
            window_days=RETURN_ABSENCE_DAYS,
        ),
    ),
    signals=(
        # Dormancy carries the most weight, and its ramp comes from the measured rewatch
        # curve (docs/SIGNALS.md, "Ground truth: rewatch probability by dormancy"). The
        # floor sits at 365 days: below that, about 61% of films are watched again within
        # a year. It saturates at 1825 days: even past that, about 13% are still watched
        # again, so nothing is ever free to delete.
        SignalSetting(signal=SignalId.UNWATCHED, weight=70, saturate_at=1825, floor=365),
        SignalSetting(signal=SignalId.FEW_WATCHERS, weight=20, saturate_at=3),
        # Set at 6.0. Raising this toward 8.0 would let an average rating start carrying
        # some of this signal, which pushes toward condemning: a 7.0-rated title would
        # move from 0 to about 1.25 of these 10 points, and a 6.0-rated title from 0 to
        # about 2.5. It would also raise the bar for a rating to argue for keeping a
        # title, so a 7.0 would stop counting as a reason to keep it. At 6.0, a title has
        # to be genuinely poorly rated before this signal adds anything.
        SignalSetting(signal=SignalId.LOW_RATING, weight=10, saturate_at=60),
        # Size is deliberately absent here.
        #
        # It measures how much space you would reclaim, unrelated to how unlikely a title
        # is to be watched. A 50 GB file is usually a 4K blockbuster, exactly the content
        # people do watch. Weighting it produced a condemned set with worse regret than
        # picking randomly among films of the same age (a -50% lift): it condemned
        # popular films precisely because they were large.
        #
        # Size can still rank the candidates the score has already chosen. It never
        # decides a title's fate. See docs/SIGNALS.md.
    ),
    # IMDb 7.5 from at least 1,000 votes, the single bar the original gate carried, now
    # expressed as one entry in the multi-source set. Owners can add Rotten Tomatoes,
    # Metacritic, or TMDb bars alongside it.
    keep_rating_rules=(RatingRuleSpec(source=RatingSource.IMDB, floor=75, min_votes=1000),),
)


DEFAULT_TV_POLICY = PolicyBody(
    media_type="tv",
    condemn_at=70,
    keep_last_seasons=2,
    keep_first_season=True,
    # The same gates as movies, since a TV season is kept for the same reasons a film is,
    # but the tag list alone as a keep rule: the IMDb chart above is movies only, so
    # naming it here would seed a protection that can never keep a season.
    protect_conditions=DEFAULT_TV_LIST_CONDITIONS,
    gates=DEFAULT_MOVIE_POLICY.gates,
    signals=(
        SignalSetting(signal=SignalId.UNWATCHED, weight=60, saturate_at=1825, floor=365),
        SignalSetting(signal=SignalId.FEW_WATCHERS, weight=15, saturate_at=3),
        # TV-only: an older season carries more pressure to prune than the newest, but
        # only as a weight, so a much-rewatched season 1 can still out-score its rank.
        # The keep-last-N seasons floor above is the hard guard.
        SignalSetting(signal=SignalId.SEASON_RANK, weight=15, saturate_at=6),
        # Same ramp as the movie lane above, where the reasoning is explained.
        SignalSetting(signal=SignalId.LOW_RATING, weight=10, saturate_at=60),
    ),
    # IMDb only by default for TV: Sonarr carries no rich ratings object, so a show's
    # IMDb score is the one bar reliably available. Owners may add Rotten Tomatoes or
    # TMDb bars, which fire only when Plex happens to serve them.
    keep_rating_rules=(RatingRuleSpec(source=RatingSource.IMDB, floor=75, min_votes=1000),),
    # The TV lane's viewing count means whole re-watches of the show, so 2 is the
    # validated bar, well below the movie lane's default of 10. A stored TV body keeps
    # whatever it saved; this covers fresh policies only.
    rewatch_min_viewings=2,
)


def combine_hashes(*hashes: str) -> str:
    """A stable hash over several policy hashes, in the order given.

    A snapshot is scored under two policies, one for movies and one for TV, so its single
    ``policy_hash`` and ``scoring_hash`` are each the combination of both, in a fixed
    order (movie, then TV). The simulator recombines the same way to check whether a
    stored score still describes an edited policy, without needing a per-media-type
    column on the snapshot.
    """
    return hashlib.sha256("".join(hashes).encode("ascii")).hexdigest()
