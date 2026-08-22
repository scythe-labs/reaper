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

## What is not here

Two jobs that read a body without being one live beside this module and import it:
``policy_migrations`` loads a body an older Reaper wrote, and ``policy_warnings``
tells an owner a legal setting is probably not what they meant. Neither is imported
back, so the model can be read on its own.
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
"""Bumped when the stored SHAPE changes. 3 marks bodies written after the rating bar moved
off the RATING_FLOOR gate row into ``keep_rating_rules``
(see ``policy_migrations.recover_rating_rules``, which backfills a body written before it)."""

SCORER_VERSION = 5
"""Bumped when the SCORER changes meaning, not when the schema gains a field.

5 marks the build where a title that came back takes a hold (#553). Both lanes start freezing
``Facts.returned_days_ago`` and ``returned_by_reaper`` where they froze nothing, and a new gate
reads them. A stored body hashes exactly as it did without this -- the gate row is appended
disabled and no existing field moved -- so without the bump a plan approved before the upgrade
would execute against evidence gathered after it, which is the "adding evidence" case two
paragraphs down.

4 marks the build where the rewatch keep goes live on the TV lane (#554): ``keep_configs``
stops gating it on ``media_type``, and season facts start carrying the show's replay
periods where they carried ``Absent``. A stored TV body hashes exactly as it did --
``rewatch_keep_enabled`` defaults on and no field changed -- so without the bump a plan
approved before the upgrade executes over seasons this build discounts toward keeping,
the "adding evidence" case two paragraphs down.

3 marked the build where ``fields._compare`` folds a ``contains`` target the way ``eq`` and
``in`` already did, so a stored text rule could start matching text it used to miss (#657):
a rule that starts matching moves an item in whichever direction it was written for.

Both are inside the policy hash: an item scored under a different scorer was not
approved under this one.

Adding evidence changes what a scan gathers, and the body cannot express that: a new
``Facts`` field, signal or gate leaves a stored body hashing exactly as it did, so a plan
already approved executes on evidence gathered without it (rule 113). Bumping here is one
answer; a loader shim that rewrites every affected body is the other, since that edit moves
those hashes itself. ``tests/test_scorer_surface.py`` records the declarations against this
number and fails when one moves and the other does not, so the choice is made rather than
missed -- which is what it was until that file existed.

Deliberately plain ``int`` and not ``Literal``. Pinning the field to a single literal
means the *next* bump makes every stored body fail ``model_validate_json``, and the one
caller that reads them (``services.profiles.active_policy``) has no fallback -- so the
bump would take out the scan path and the policy editor together, including the page an
operator would use to fix it. Bodies from a NEWER Reaper are still refused, below: those
we genuinely cannot interpret."""


#: How long a title that came back resists condemning, in days. A year and a half.
#:
#: **A judgment call, and written here as one** so a later measurement can replace it without
#: archaeology. Nothing reachable has deleted something and had it come back, so there is no
#: regret data to fit against on this library or any other (``docs/history/RETURN_PLAN.md``).
#: What the number expresses is that a regret is worth remembering longer than the dormancy floor,
#: which defaults to three years, so the hold is meaningful without being permanent.
RETURN_HOLD_DAYS = 548

#: How long a title has to be missing before its return counts, in days.
#:
#: Set by measurement, unlike the hold above. Every rating-key change observed on a real
#: library completed within 2.5 to 30 hours, and that figure is an upper bound on the true
#: absence, so seven days clears the measured ceiling more than five times over
#: (``docs/LEARNINGS.md``, "The Plex rating key is stable enough to detect a return"). Three
#: days would already have rejected all of it.
RETURN_ABSENCE_DAYS = 7


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GateSetting(Frozen):
    """One protection, as the user configures it."""

    gate: GateId
    enabled: bool = True

    threshold: int = 0

    #: No ``secondary`` here. It held the rating gate's vote floor until that bar moved to
    #: ``keep_rating_rules``, and the model stopped declaring it once migration
    #: ``e6f708192a3b`` had rewritten every stored body where the number was inert (issue
    #: #266). Two kinds of body still carry the key and neither reaches this model with it:
    #: a row the migration deliberately skipped, because the number is the last copy of a
    #: rating bar ``policy_migrations.recover_rating_rules`` has yet to put back, and any body
    #: predating the migration on a database it has not reached. Both shims strip it, through
    #: ``policy_migrations.drop_retired_gate_keys``. Do not re-add a field here to accommodate
    #: a stored key: ``Frozen`` is
    #: ``extra="forbid"`` on purpose, and the strip is what keeps that honest.

    window_days: int = Field(default=365, ge=1)
    """How far back "recently" reaches, for gates that count activity.

    There is no way to spell "all time", and that is deliberate. An unwindowed
    popularity gate protects anything anyone has *ever* played, and on a long-lived
    server that is very nearly the whole library -- only a fraction of those titles
    still have watchers in the last year. Measured against real history, the
    unwindowed version silently disabled the entire scorer: rehearsed at every
    threshold it found almost nothing to delete, and the tool looks "safe" while
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
    """0 disables the signal, and removes it from the denominator so the remaining
    scores do not silently inflate."""

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

    Protect-only *by construction*. It is validated against the PROTECT lane of the field
    registry, so the worst a mis-authored condition can do is fail to keep something -- it can
    never mark a title for removal. That asymmetry is why these are safe to hand to the
    owner (see ``engine.fields``).
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

    A numeric field, ramped ``floor`` -> ``saturate_at``, whose discount is in score
    POINTS (not a share). Fail-closed: a value we cannot read keeps the title fully. It can
    only ever LOWER a score, and can never un-protect a title a gate protected -- the verdict
    checks protection before it ever reads the score. Any numeric field may drive a keep,
    including protect-only ones like all-time watchers or vote count, which is the point."""

    name: str = Field(min_length=1, max_length=60)
    field: str
    value: str | None = Field(default=None, max_length=200)
    """For a membership field (``on_list``): which list, by name. That keep is FLAT, not a
    ramp -- on the list takes the full ``max_discount``, off it takes none, and a
    membership that could not be read takes the full one, fail-closed like every keep.
    ``None`` for every numeric field, whose ramp is below."""
    max_discount: int = Field(ge=1, le=100)
    """Points to subtract at full strength. ``ge=1`` -- "off" is expressed by omitting the rule."""
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
            # The membership form: flat, so the ramp fields are inert and unvalidated.
            #
            # Keyed on the field, not on its SHAPE. `TEXT and multi` also describes `genre`,
            # which validated, granted the keep, and explained itself as 'on your list
            # "Comedy"' (#505). Nothing widened -- a keep only ever lowers a score -- but the
            # operator was told a genre is a list (rule 21). The editor offers this box for
            # `on_list` alone, so the reachable paths were an imported or restored body and
            # the API.
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
                raise Refusal(
                    "error.policy.rating_vote_floor_on_percentage",
                    source=source_label(self.source),
                )
        elif self.min_votes < 1:
            # A rating floor with no vote floor protects an 8.3 drawn from a few hundred
            # votes -- a number that means nothing. The same refusal the single-source gate
            # carried, now per source that actually counts votes.
            raise Refusal("error.policy.rating_vote_floor_zero", source=source_label(self.source))
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

        It moves ``policy_hash``, which voids a plan approved before the upgrade and asks for
        a re-scan (rule 113), and ``scoring_hash``, since ``gates`` is not in
        ``_POST_SCORE_FIELDS`` -- so the simulator loses its stored-score tier. It does NOT
        move ``evidence_hash`` any more: ``gates`` reaches that hash only through
        ``_gathering_evidence``, and no retired gate is the popularity gate, so the window is
        unchanged and the frozen-facts replay answers. The first Policy page after such an
        upgrade therefore shows numbers rather than a blank, which is the honest outcome and
        the reverse of what this paragraph said while the whole list sat in the hash.

        What it must NOT do is let a surface blame the operator for it. The simulator's stale
        notice once opened "You changed what the scan reads" at an install that had changed
        nothing; it now states the condition instead (``PolicySimulator.tsx``, and the matching
        ``stale_reason`` in ``api.simulate``). No post-upgrade code can reproduce the old hash,
        so the copy is the only thing that can carry the truth here.
        """
        kept = tuple(g for g in self.gates if g.gate not in self.RETIRED_GATES)
        if len(kept) != len(self.gates):
            object.__setattr__(self, "gates", kept)
        return self

    @model_validator(mode="after")
    def _rewatch_odds_row(self) -> Self:
        """Every body carries the rewatch-odds row, appended on load when missing.

        The append is what puts the protection's switch in front of an operator whose body
        was stored before it existed: rows render from the body's own gate list, so without
        this a pre-upgrade body simply never shows the control. Appended DISABLED at the
        shipped starting percentage, so it changes no verdict until the operator turns it
        on -- not a repair, and no degrade, by the same reasoning as the retirement above:
        nothing was protecting, so nothing was withdrawn. It moves ``policy_hash`` for
        every stored body lacking the row (rule 113 voids pre-upgrade approvals) and
        ``scoring_hash``, and leaves ``evidence_hash`` alone, exactly like the drop above.

        This appended for movies alone, and stripped the row from TV bodies, while the
        season lane froze the gate's two cohort facts ``Absent`` (rule 38: a switch for a
        gate that can never fire). The TV fit cleared its own backtest
        (``docs/LEARNINGS.md``, the TV fit entry), season rows now freeze real cohorts, and
        the append moving every stored TV body's hash is what voids pre-upgrade TV
        approvals for the parity stage the same way the movie append did for stage 2.
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
        """Every body carries the came-back row, appended on load when missing (#553).

        The row above's twin, for the same reason and on the same terms: rows render from the
        body's own gate list, so a body stored before this protection existed would never show
        its switch. Appended DISABLED at the shipped lengths, so it changes no verdict until
        the operator turns it on -- not a repair and no degrade, because nothing was protecting
        and nothing was withdrawn.

        Off in the shipped defaults too, so the same sentence is true of every install and
        the operator documentation has one answer rather than two (``DEFAULT_MOVIE_POLICY``
        carries why).

        Moves ``policy_hash`` for every stored body lacking the row (rule 113 voids
        pre-upgrade approvals) and ``scoring_hash``. It does NOT move ``evidence_hash``,
        unlike an enabled row would: ``returned_absence_days`` reads this gate's window only
        while the gate is on, so a disabled row falls back to the shipped default and gathers
        exactly what the tree gathered before it.
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
    """Score at or above which an item is a candidate. Never 0: a threshold of 0
    condemns everything the gates do not save."""

    coverage_floor_bp: int = Field(default=5000, ge=0, le=10_000)
    """Minimum share of signal weight we must actually have evaluated, in basis
    points (5000 = 50%). Below it the item abstains rather than being judged on
    fragments. Guards against condemning an item we can barely see."""

    keep_last_seasons: int = Field(default=2, ge=0, le=1_000)
    """Season pruning: the N most recent seasons of a show are protected outright,
    whatever they score. Movies ignore this. A hard floor, not a weight -- ``0`` means
    "keep no season on age alone" (the other guards and the score still apply); it does
    NOT mean "unlimited", which is the Janitorr footgun the whole policy module avoids.
    See ``services.season_pruning``.

    The ceiling is arithmetic hygiene rather than a policy opinion, and the same on all
    three season numbers here: it sits far above any real setting, so it cannot invalidate
    a stored body. ``api.schemas.PolicyIn`` declares it too, and
    ``tests/test_policy.py::TestTheTwoPolicyDeclarationsAgree`` fails when the two drift
    (rules 95 and 131)."""

    keep_first_season: bool = True
    """Season pruning: protect the first content-bearing season of every show, so a
    library never throws away the pilot that lets a new viewer start the show. On by
    default; movies ignore it."""

    keep_last_scope: Literal["all", "requested"] = "all"
    """Whether the keep-last-N floor applies to every show (``all``) or only to shows someone
    requested (``requested``). Fail-closed: under ``requested``, when we cannot tell whether a
    show was requested, the floor still applies -- Unknown counts as "might be requested"."""

    season_lookahead: int = Field(default=0, ge=0, le=1_000)
    """How many seasons BEYOND a viewer's current position to also protect while they binge.
    ``0`` protects exactly the season they are mid-way through, or the next one if they have
    finished the current. Replaces the old hardcoded look-ahead. Movies ignore it.

    ``season_pruning.sequential_protections`` builds ``range(lookahead + 1)`` once per anchor
    per viewer per show, so the ceiling is what keeps an unbounded draft from allocating
    inside the event loop that serves ``/policy/simulate``."""

    keep_in_progress: bool = True
    """Season pruning: protect the season a viewer is partway through (and the next one,
    once they finish it) -- the sequential-progression guard in ``services.season_pruning``.
    On by default; turning it off removes that guard entirely. Movies ignore it."""

    in_progress_hold_days: int = Field(default=180, ge=0, le=36_500)
    """How long a viewer's place in a show is held after their last watch of that show.
    Past this many days without watching, the show counts as abandoned by that viewer and
    their half-finished season no longer protects anything. ``0`` holds forever. A viewer
    whose last-watched time cannot be read keeps their hold (fail closed). Only meaningful
    while ``keep_in_progress`` is on; movies ignore it.

    This is a span the guard *claims to cover*, not a bound on the watch mirror, so setting
    it past how far the mirror reaches (``0`` included, which no finite mirror can cover)
    makes the claim unsupportable: ``gates.progress_is_establishable``
    then holds every season on disk rather than letting an unseeable viewer read as an
    absent one.

    ``season_pruning.active_progress`` computes ``now - timedelta(days=hold_days)``, which
    raises ``OverflowError`` before any of that once the value runs past what a ``datetime``
    can hold. The ceiling is 100 years, which no mirror reaches and ``0`` already covers."""

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

    rewatch_keep_enabled: bool = True
    """The built-in rewatch keep (``docs/history/REWATCH_PLAN.md``): a title watched at
    least ``rewatch_min_viewings`` times, most recently within ``rewatch_recent_days``, has
    its score lowered by ``rewatch_keep_discount`` points. A soft keep exactly like a
    ``graded_keeps`` row, never a protection. Live on both lanes: the TV formulation
    cleared the same backtest bar a stage later (``docs/LEARNINGS.md``, the TV entry), with
    the count meaning whole re-watches of the show there (``rewatch_min_viewings`` below).
    A stored body predating these fields thaws to the defaults, the keep direction; an
    explicit ``False`` is the operator's choice and honored."""

    rewatch_keep_discount: int = Field(default=20, ge=1, le=50)
    """Points subtracted while the rewatch condition holds. ``ge=1``: off is the switch
    above, not a zero."""

    rewatch_min_viewings: int = Field(default=10, ge=1, le=1_000)
    """The viewing-count bar, whose unit is the lane's. On a movie body: qualified viewings
    (any user, plays inside ``services.rewatch.VIEWING_GAP_DAYS`` of each other clustered
    as one). On a TV body: whole re-watches of the show
    (``services.rewatch.replay_period_count``), where the validated bar is 2
    (``DEFAULT_TV_POLICY`` sets it; the class default here is the movie backtest's 10, so a
    TV body stored before the TV stage keeps firing only for heavily re-watched shows until
    the operator lowers it -- conservative, and visible on the card). Both defaults are
    starting values from out-of-sample backtests on one heavy-rewatch library
    (``docs/LEARNINGS.md``), not truths: a quieter library is untested, and loosening buys
    coverage at some precision. The ceiling is arithmetic hygiene (rule 95)."""

    rewatch_recent_days: int = Field(default=730, ge=1, le=36_500)
    """How recent the last qualified play must be for the keep to fire. Same backtested
    starting value and the same 100-year ceiling as ``in_progress_hold_days``."""

    # ``keep_tags`` / ``keep_tags_match`` lived here: the *arr tags that spared a title,
    # configured on Policy while every other list lived on Settings -> Lists. They are a
    # LIST now -- defined once, on Lists, protecting through an ``on_list`` keep rule like
    # every other list -- and ``policy_migrations.convert_list_protections`` carries a stored
    # body's tags into that shape on load.

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
            if over > 0:
                raise Refusal("error.policy.weights_over_100", total=total, amount=over)
            raise Refusal("error.policy.weights_under_100", total=total, amount=-over)
        return self

    # An `_at_least_one_signal` validator lived here, refusing an all-zero policy. A total
    # of exactly 100 cannot be reached with every weight at 0, so it became unreachable
    # the moment the rule above landed, and unreachable safety code is deleted rather than
    # kept for reassurance. If the total is ever relaxed, restore it in the same change.

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
        # collide with the built-in row: the why-panel would drop its "Your rule" tag and
        # render two rows under one key, and the audit record could not tell them apart.
        builtin = {s.value for s in SignalId}
        for name in names:
            if name in builtin:
                raise Refusal("error.policy.custom_rule_name_collides", name=name)
        keep_names = [k.name for k in self.graded_keeps]
        if len(set(keep_names)) != len(keep_names):
            raise Refusal("error.policy.duplicate_keep_rule_name")
        # The stored explanation identifies keep rows by name, and the built-in rewatch keep
        # rides in the same list -- the same collision the custom-rule check above prevents.
        if REWATCH_KEEP in keep_names:
            raise Refusal("error.policy.keep_rule_name_collides_rewatch", name=REWATCH_KEEP)
        rating_sources = [r.source for r in self.keep_rating_rules]
        if len(set(rating_sources)) != len(rating_sources):
            raise Refusal("error.policy.duplicate_rating_source")
        return self

    def popularity_window_days(self) -> int:
        """The window the recent-watchers fact counts over, in days.

        Reads the SERVER_POPULARITY gate's window only while that gate is ENABLED: a
        disabled gate's leftover window must not keep steering the ``distinct_watchers``
        fact, where a short stale window quietly raises FEW_WATCHERS pressure across the
        whole library. Falls back to the 365-day default otherwise. Every reader of the
        window comes here, so the default lives in exactly one place.
        """
        return next(
            (g.window_days for g in self.gates if g.gate is GateId.SERVER_POPULARITY and g.enabled),
            365,
        )

    def returned_absence_days(self) -> int:
        """How long a title must be missing before its return counts, in days.

        ``popularity_window_days``' twin, and read the same way: the RETURNED gate's window
        only while that gate is enabled, so a disabled gate's leftover number cannot steer what
        the scan writes into the ledger. Falls back to the shipped default otherwise, and every
        reader comes here, so the default lives in one place.

        A gathering value, not a judging one: it decides what the scan RECORDS as a return, and
        the gate reads the recorded answer. That is why it belongs in ``_gathering_evidence``
        and the hold's length does not.

        **It is a gathering value whether the gate is on or off**, because the ledger is
        written on every scan either way (``services.library_seen``). That is what makes the
        protection useful the day it is switched on rather than months later: Reaper can only
        notice a title coming back if it saw the title before it left. So the number is always
        in the hash, and switching the gate off means gathering under the shipped default
        rather than gathering nothing.
        """
        return next(
            (g.window_days for g in self.gates if g.gate is GateId.RETURNED and g.enabled),
            RETURN_ABSENCE_DAYS,
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
        """Translate the graded-keep specs into engine keep configs for ``score()``.

        The built-in rewatch keep is appended HERE rather than where the scan assembles its
        keeps, because this method has two callers -- ``services.snapshot.scan`` and the
        simulator replay (``api.simulate``) -- and a built-in appended at one of them is a
        keep the other silently drops (rule 104; ``docs/history/REWATCH_PLAN.md`` records the
        deviation). Both lanes: the TV formulation cleared the backtest bar a stage later
        (``docs/LEARNINGS.md``, the TV entry), and ``media_type`` on the config picks the
        lane's wording.
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
        enumerated in ``api.simulate.simulate``.

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
    #: ``gates`` is here, but only ever through ``_gathering_evidence`` below: a gate decides
    #: what to make of an item, and every fact it reads is gathered whether or not it is
    #: enabled. The one exception is the popularity window, which is the span
    #: ``distinct_watchers`` is counted over, so it is folded back in as its own key.
    #: ``scorer_version`` belongs here for the same reason the weights do: a replay runs the
    #: CURRENT ``score``/``evaluate_all``/``decide_verdict`` over the frozen Facts, so a new
    #: scorer's answer is reproduced exactly. It stays in ``scoring_hash``, which is what
    #: routes a scorer bump to the replay instead of to the stale stored scores.
    #:
    #: **The nine season fields are here, and they are the one entry not answered by
    #: ``facts_json`` alone.** A season's guard result is decided per SHOW, from Sonarr's
    #: season statistics and who is part-way through it -- inputs that never reached ``Facts``
    #: -- so freezing the guard's output was enough to explain a scan and never enough to
    #: re-decide one. The scan now freezes those inputs too
    #: (``db.models.SeasonPruneEvidence``) and the replay re-derives the plan through the same
    #: ``season_evidence.plan_from_frozen`` the scan used. What that buys is a hash that no
    #: longer refuses a season rule; what it does NOT buy is an answer for a snapshot with no
    #: bundle, or for turning the mid-binge hold on over a scan that never read Sonarr's
    #: episode lists. Neither is a hash question -- a hash cannot say WHY it mismatched
    #: (``docs/LEARNINGS.md`` §13) -- so both are asked of the stored evidence itself in
    #: ``api.simulate._season_guard_replay``, which is what lets the panel name the one control at
    #: fault instead of blanking nine.
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
            # fresh scan on every strength edit forever. One transient window, accepted in
            # ``docs/history/REWATCH_PLAN.md``: a replay over a snapshot frozen before the upgrade
            # thaws both observations Unknown, so the preview takes the full discount
            # (toward keeping, shown as "couldn't check") until the first post-upgrade scan.
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

    #: Every field of a gate row, split by whether a scan can read it BEFORE it freezes an
    #: item's Facts. ``window_days`` is the one that can: it is the span
    #: ``snapshot._watch_stats`` counts ``distinct_watchers`` over. The others reach the scan
    #: only through ``scan_runner.build_gates``, which the replay calls itself, over facts
    #: that were gathered whether or not the gate asking for them was switched on.
    #:
    #: ``enabled`` is in the judging half and also feeds ``popularity_window_days``, which is
    #: not a contradiction: what it selects there is a *window*, and the window is the only
    #: thing the gather phase ever learns from this list.
    #:
    #: Split by name rather than by hashing the row, so a gate field added later lands in
    #: neither set and ``test_policy.py``'s drift guard fails until someone classifies it. A
    #: new gathering field defaulting quietly into the judging half would put a confident
    #: wrong preview in front of an operator, which is the failure the three tiers exist to
    #: prevent (rule 103).
    _GATHERING_GATE_FIELDS: ClassVar[frozenset[str]] = frozenset({"window_days"})
    _JUDGING_GATE_FIELDS: ClassVar[frozenset[str]] = frozenset({"gate", "enabled", "threshold"})

    def _gathering_evidence(self) -> dict[str, object]:
        """What ``gates`` tells a scan before it freezes anything.

        Two numbers, and each already carries the enabled flag it depends on: a disabled gate
        falls back to its shipped default, so switching one off gathers exactly what it
        gathered before and the replay stays exact. Switching one ON at any other value moves
        this, which is the whole point -- the frozen evidence was taken under a span the edited
        policy no longer asks for.

        The second is #553's minimum absence. It has to be here rather than in the judging half
        because it decides what the SCAN writes into the ledger, and the gate then reads that
        stored answer: replayed against frozen facts it would report an edited policy's verdict
        computed under the old number, which is the confident wrong preview
        ``_GATHERING_GATE_FIELDS`` exists to prevent. Adding it changed this formula, so no
        snapshot from an earlier build matches and every simulator tier refuses until the next
        scan -- the same one-scan cost ``evidence_hash`` documents below.
        """
        return {
            "popularity_window_days": self.popularity_window_days(),
            "returned_absence_days": self.returned_absence_days(),
        }

    def evidence_hash(self) -> str:
        """Identifies what a scan under this policy would GATHER and FREEZE per item.

        Two policies with the same evidence hash produce byte-identical Facts (and the same
        season-pruning guard) for every item -- so the simulator may rebuild those Facts
        from ``Candidate.facts_json`` and replay the real ``score``/``evaluate_all``/
        ``decide_verdict`` under the edited policy, exact for any change to the replayable
        fields (weights, rating bars, custom rules, protect conditions, thresholds).

        When it differs, the edit changed the evidence itself -- the popularity window, the
        media type -- so the frozen Facts are stale and a real scan is required.
        The set of replayable fields is an allow-list, so an unclassified field falls into
        this hash and forces the safe, honest fresh scan.

        A season rule used to be on that list and no longer is: the scan freezes its plan's
        inputs per show now (``db.models.SeasonPruneEvidence``), so the replay re-derives the
        guard rather than reading a stale one. What a matching hash therefore promises about
        a TV row is narrower than it looks -- it says the FACTS replay, not that the show's
        bundle is present, readable, and describes that season.
        ``api.simulate._season_guard_replay`` asks the evidence that second question, because a
        hash cannot answer it: two policies that gather identically can still disagree about
        stored evidence, which is a fact about the snapshot and not about the policy.

        Moving those nine fields out of here changed the formula, so this is another instance
        of the one-scan cost below: no snapshot written by an earlier build can match it, and
        until the next scan every edit refuses. The season-specific refusals therefore describe
        the state *after* that scan, never the upgrade.

        The allow-list is the right default and it has one sharp edge: a field that is pure
        bookkeeping falls in here too and forces a rescan that can never help. That is what
        ``schema_version`` did, permanently (see ``_NON_BEHAVIORAL_FIELDS``). Classify a new
        field into one of the three sets when you add it.

        **Turning a protection on or off is a judging edit, not a gathering one.** The fact
        every gate reads is gathered unconditionally -- no fact builder branches on a gate's
        enabled flag -- so the scan freezes the same bytes either way and the replay answers
        exactly. Folding the whole ``gates`` list in here spent that exactness on nothing:
        the rating bars were already replayable while the switch above them was not, so
        moving the bar previewed instantly and unticking the box it sat in blanked the panel
        and asked for a scan. Only ``_gathering_evidence`` survives from the list now.

        **Changing what this hash covers costs every stored snapshot one scan.** A snapshot
        records the hash its own scan computed (``services.snapshot``), so one written by an
        earlier build cannot match this formula whatever the operator does, and until the next
        scan a weight or bar edit refuses where it used to replay. It heals on that scan and
        the notice's "run a scan, then this becomes exact again" is true throughout, which is
        the whole difference from ``schema_version``: that one could never be scanned away,
        because each scan wrote the stale value back. Verified on a live install before
        landing: a stored snapshot whose ``policy_hash`` and ``scoring_hash`` both still
        matched, and whose evidence hash could not. Weigh that one-scan window against the
        edit being bought whenever this set moves again."""
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

    #: Four caps, not two. The rolling BYTE cap is what keeps a 4 TB incident out of
    #: reach: no sequence of runs is admitted past it, because each run is admitted only
    #: if the whole of it still fits. The per-run caps are enforced by
    #: ``executor._check_caps`` and the rolling 30-day caps by
    #: ``Executor._check_rolling_caps``, both aborting (never truncating) before any send.
    #:
    #: What the BYTE caps count is what Sonarr and Radarr track, and for a movie that is
    #: the file rather than the folder the delete removes, so a run can free a little more
    #: than the cap admitted. Measured at 0.2% of a sampled library and heavy-tailed, since
    #: the untracked bytes are mostly extras rather than artwork (``snapshot._reported_size``,
    #: #317). Read them as a close bound, not an equality. The ITEM caps have no such gap,
    #: and neither does the season side.
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
    """How long a condemned item is shown as leaving, so your users can catch it.

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
    """Join as `"a", "b" and "c"`. The conjunctive twin of `fields._join_or`; both exist
    because a comma-joined dump is not something an operator reads at a glance.

    Public, so a second module needing the same conjunction imports this rather than growing
    a third copy: `policy_warnings.inspect` and `services/scan_runner.py` both join with it.

    It stays here rather than moving with `inspect`, because it is a sentence builder and not
    a warning: `scan_runner` reaches for it to join repair remedies, which is nothing to do
    with a dangerous config."""
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


#: The names of the two lists a fresh install is seeded with (``list_config``), spelled
#: here because the default policies' keep rules below name them and the engine must not
#: import the service layer. An upgraded body's rules point at the same rows without
#: reading these: ``policy_migrations.conversion_list_names`` finds them by source, preset
#: and tag, so it picks up whatever the seeded row is actually called.
DEFAULT_TAG_LIST_NAME = "Titles you've tagged"
DEFAULT_IMDB_LIST_NAME = "IMDb Top 250"

#: The keep rules a fresh install starts with: the seeded lists keep their titles
#: outright, the protection level the retired WHITELISTED and CURATED_LIST gates gave the
#: same sources. Softening one to a lean, or removing it, is a per-list choice on Policy.
#:
#: Scoped by the media type each list can hold. The tag list holds movies (Radarr) and shows
#: (Sonarr) under the one keep tag, so both policies name it. The IMDb chart is movies only
#: -- ``services/lists.py`` hardcodes ``media_type="movie"`` for it -- so a TV rule naming it
#: can never match a season: it protects nothing and reads as a configured protection the
#: operator never chose (rule 38). So the TV default names the tag list alone.
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
        # Off by default: it overlaps the dormancy floor above, and its threshold only
        # means something once the operator has read their own fitted ladder
        # (docs/history/REWATCH_PLAN.md, stage 2). 25 is the shipped starting percentage.
        GateSetting(gate=GateId.REWATCH_ODDS, enabled=False, threshold=25),
        # A title that left the library and came back is held for a year and a half (#553).
        # Off by default, the same shape as the row above and for a related reason: the hold's
        # LENGTH is a judgment call rather than a fit, since nothing reachable has deleted
        # something and had it come back, so there is no regret data to set it from. The
        # detector's precision is measured; how long a regret is worth remembering is not.
        # Off here and off when appended to a stored body, so one sentence is true of every
        # install and an upgrade changes nobody's policy under them.
        GateSetting(
            gate=GateId.RETURNED,
            enabled=False,
            threshold=RETURN_HOLD_DAYS,
            window_days=RETURN_ABSENCE_DAYS,
        ),
    ),
    signals=(
        # Dormancy dominates, and the numbers come from the measured rewatch curve
        # (docs/SIGNALS.md, "Ground truth: rewatch probability by dormancy"): floor at 365
        # (below which ~61% of films are played again within the year) and saturating at
        # 1825 (beyond which the rate is still ~13%, never zero -- nothing is ever free
        # to delete).
        SignalSetting(signal=SignalId.UNWATCHED, weight=70, saturate_at=1825, floor=365),
        SignalSetting(signal=SignalId.FEW_WATCHERS, weight=20, saturate_at=3),
        # 6.0, and it was briefly 8.0 on the way here. Raising it makes an average rating
        # carry a little of this signal, which is the condemn direction: measured against
        # `evaluate_signal`, a far end of 8.0 takes a 7.0 title from 0 to 1.25 of these 10
        # points and a 6.0 title from 0 to 2.5. It also moves the bar for "this rating argues
        # for KEEPING it" up with it, so a 7.0 stops reading as a reason to keep. Back at 6.0
        # a title has to be genuinely poorly rated before this signal says anything.
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
    # The same gates as movies -- a TV season is kept for the same reasons a film is -- but
    # the tag list alone as a keep rule: the IMDb chart above is movies only, so naming it
    # here would seed a protection that can never keep a season (rule 38).
    protect_conditions=DEFAULT_TV_LIST_CONDITIONS,
    gates=DEFAULT_MOVIE_POLICY.gates,
    signals=(
        SignalSetting(signal=SignalId.UNWATCHED, weight=60, saturate_at=1825, floor=365),
        SignalSetting(signal=SignalId.FEW_WATCHERS, weight=15, saturate_at=3),
        # TV-only: an older season carries more pressure to prune than the newest, but only
        # as a weight -- a much-rewatched season 1 can still out-score its rank. The
        # keep-last-N seasons floor above is the hard guard.
        SignalSetting(signal=SignalId.SEASON_RANK, weight=15, saturate_at=6),
        # Same ramp as the movie lane above, where the reasoning lives.
        SignalSetting(signal=SignalId.LOW_RATING, weight=10, saturate_at=60),
    ),
    # IMDb only by default for TV: Sonarr carries no rich ratings object, so a show's
    # IMDb score (from the dataset) is the one bar reliably available. Owners may add
    # Rotten Tomatoes or TMDb bars, which fire only when Plex happens to serve them.
    keep_rating_rules=(RatingRuleSpec(source=RatingSource.IMDB, floor=75, min_votes=1000),),
    # The TV lane's viewing count is whole re-watches of the show, and 2 is the validated
    # bar (docs/LEARNINGS.md, the TV entry) -- the class default is the movie lane's 10.
    # A stored TV body keeps whatever it saved; this covers fresh policies only.
    rewatch_min_viewings=2,
)


def combine_hashes(*hashes: str) -> str:
    """A stable hash over several policy hashes, in the order given.

    A snapshot is scored under two policies -- one for movies, one for TV -- so its single
    ``policy_hash`` / ``scoring_hash`` is the combination of both, in a fixed order (movie,
    then TV). The simulator recombines the same way to check whether a stored score still
    describes an edited policy, without needing a per-media-type column on the snapshot.
    """
    return hashlib.sha256("".join(hashes).encode("ascii")).hexdigest()
