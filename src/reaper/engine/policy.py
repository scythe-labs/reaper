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
labelled "IMDb floor (tenths)" is a 422, not a policy that protects nothing because
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

import hashlib
import json
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reaper.engine.fields import Condition, Lane, Op
from reaper.engine.gates import GateId
from reaper.engine.signals import SignalId

SCHEMA_VERSION: Literal[1] = 1
SCORER_VERSION: Literal[1] = 1
"""Bumped when the SCORER changes meaning, not when the schema gains a field.
Both are inside the policy hash: an item scored under a different scorer was not
approved under this one."""


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

        if self.gate is GateId.RATING_FLOOR:
            if not 1 <= self.threshold <= 100:
                raise ValueError(
                    "The IMDb floor is in tenths: 7.5 is 75. It must be between 1 and 100."
                )
            if self.secondary < 1:
                raise ValueError(
                    "A vote floor of 0 makes the rating floor meaningless -- it would "
                    "protect an 8.3 rating drawn from 388 votes. Use at least 1 "
                    "(1000 is a sensible default)."
                )
        if self.gate is GateId.SERVER_POPULARITY and self.threshold < 1:
            raise ValueError(
                "Keeping anything watched by 0 people would protect your whole library. "
                "Set it to at least 1 — or switch this protection off instead."
            )
        if self.gate is GateId.MIN_DORMANCY and self.threshold < 365:
            raise ValueError(
                "Give titles at least a year before removing them. Anything left alone for "
                "only a year or two still gets watched again a lot of the time, so removing it "
                "then is close to a coin-flip against your users. If you really mean to, switch "
                "this protection off with its toggle rather than setting it this low."
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


class PolicyBody(Frozen):
    """The hashed, immutable part of a policy.

    Everything here changes what Reaper would *decide*. Anything that changes only
    how much it may *do* (caps) or how long it waits (grace) lives on the Profile,
    so that tightening a cap does not void every pending approval.
    """

    schema_version: Literal[1] = SCHEMA_VERSION
    scorer_version: Literal[1] = SCORER_VERSION

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

    gates: tuple[GateSetting, ...]
    signals: tuple[SignalSetting, ...]

    protect_conditions: tuple[ConditionSpec, ...] = ()
    """The owner's own protections, on top of the built-in gates. Each keeps a title when it
    matches; together they are an OR (any one is enough). Protect-only -- see ConditionSpec."""

    keep_tags: tuple[str, ...] = ("reaper-keep",)
    """The *arr tags that spare a title outright -- the configurable form of "honour your keep
    list". A title carrying one of these (or all of them, per ``keep_tags_match``) is kept
    whatever it scores. Read at scan time and synced into the whitelist before scoring. Movies
    read Radarr tags, TV reads Sonarr tags, so the two policies carry their own."""

    keep_tags_match: Literal["any", "all"] = "any"
    """Whether a title needs ANY of ``keep_tags`` (the usual case) or ALL of them to be kept."""

    @model_validator(mode="after")
    def _at_least_one_signal(self) -> Self:
        if not any(s.weight > 0 for s in self.signals):
            raise ValueError(
                "Every signal has weight 0, so every item would score 0 and nothing "
                "would ever be a candidate. This is almost certainly not what you meant."
            )
        return self

    @model_validator(mode="after")
    def _no_duplicates(self) -> Self:
        if len({g.gate for g in self.gates}) != len(self.gates):
            raise ValueError("A gate is configured twice; the second would silently win.")
        if len({s.signal for s in self.signals}) != len(self.signals):
            raise ValueError("A signal is configured twice; the second would silently win.")
        return self

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

    def scoring_hash(self) -> str:
        """Identifies the policy's *scoring behaviour*, ignoring the thresholds.

        Two policies with the same scoring hash assign every item the same score and
        the same gate outcomes; they may still disagree about the verdict, because
        ``condemn_at`` and ``coverage_floor_bp`` are compared against those results
        afterwards.

        This is what makes the zero-API-call simulator honest. Re-deciding a stored
        snapshot at a new threshold is exact. Re-deciding it under a new *weight* or a
        new *gate* is not -- the stored scores and verdicts were produced by the old
        ones, and there is no way to recover the new answer without re-reading the
        library. So the snapshot records this hash, the simulator compares against it,
        and when they differ it **refuses to report numbers** rather than reporting
        confident, stale ones.
        """
        payload = {
            k: v
            for k, v in self.model_dump(mode="json").items()
            if k not in self._POST_SCORE_FIELDS
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()


class ProfileSettings(Frozen):
    """The mutable part: how much Reaper may do, and how long it waits.

    Kept *out* of the hash on purpose. Tightening a cap or lengthening the grace
    period is always safe, and voiding every pending approval because the owner
    reduced a limit would train them to stop reading the diff.
    """

    #: Four caps, not two. The rolling BYTE cap is what makes a 4 TB incident
    #: arithmetically unreachable: no sequence of runs can exceed it.
    max_items_per_run: int = Field(default=10, ge=1, le=1000)
    max_bytes_per_run: int = Field(default=500 * 1_000_000_000, ge=1)
    max_items_per_30d: int = Field(default=100, ge=1)
    max_bytes_per_30d: int = Field(default=2_000 * 1_000_000_000, ge=1)

    grace_days: int = Field(default=14, ge=7)
    """How long a condemned item sits, cancellable, before it is actually deleted.
    Floored at 7: a grace period shorter than a week is one your users cannot
    realistically act on."""

    require_approval: bool = True
    """Turned off only by an AutonomyGrant, never directly."""

    @model_validator(mode="after")
    def _run_cap_within_rolling_cap(self) -> Self:
        if self.max_items_per_run > self.max_items_per_30d:
            raise ValueError(
                f"A single run may delete {self.max_items_per_run} items but the 30-day "
                f"cap is {self.max_items_per_30d}. The rolling cap would be meaningless."
            )
        if self.max_bytes_per_run > self.max_bytes_per_30d:
            raise ValueError("A single run may delete more bytes than the entire 30-day budget.")
        return self


class PolicyWarning(Frozen):
    """A config that is legal but probably not what the owner meant."""

    field: str
    message: str
    severity: Literal["warn", "danger"]


def inspect(body: PolicyBody, settings: ProfileSettings) -> list[PolicyWarning]:
    """The dangerous-config detector.

    Validation refuses what is *provably* wrong. This catches what is merely
    *probably* wrong -- and a validator cannot tell the two apart, because the
    values are legal either way.

    The archetype: an IMDb floor is stored in tenths, so ``75`` means 7.5. A user
    thinking in Rotten Tomatoes types ``96``, which is legal (it means 9.6) and
    protects almost nothing. No validator can distinguish that from someone who
    genuinely wants a 9.6 floor. So we say so, loudly, and show the blast radius
    next to it rather than pretending to know.
    """
    warnings: list[PolicyWarning] = []

    rating = next((g for g in body.gates if g.gate is GateId.RATING_FLOOR and g.enabled), None)
    if rating is not None:
        if rating.threshold >= 90:
            warnings.append(
                PolicyWarning(
                    field="gates.rating_floor.threshold",
                    severity="warn",
                    message=(
                        f"An IMDb floor of {rating.threshold / 10:.1f} will protect almost "
                        "nothing -- very few films rate that highly. If you meant a Rotten "
                        "Tomatoes percentage, note this field is IMDb, in tenths: 7.5 is 75."
                    ),
                )
            )
        if rating.threshold <= 20:
            warnings.append(
                PolicyWarning(
                    field="gates.rating_floor.threshold",
                    severity="warn",
                    message=(
                        f"An IMDb floor of {rating.threshold / 10:.1f} protects essentially "
                        "everything. Did you mean 7.0, which is 70?"
                    ),
                )
            )

    disabled = {g.gate for g in body.gates if not g.enabled}
    for gate, why in (
        (GateId.STREAMING_NOW, "Reaper could delete a file while someone is watching it."),
        (GateId.DATA_HORIZON, "Media older than your watch history would look never-watched."),
        (
            GateId.UNMANAGED,
            "Reaper cannot delete unmanaged files anyway; disabling this only hides them.",
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
                    "protections do not save. Run a backtest before arming this."
                ),
            )
        )

    if not settings.require_approval:
        warnings.append(
            PolicyWarning(
                field="require_approval",
                severity="danger",
                message="This profile deletes without a human looking at the list first.",
            )
        )

    return warnings


DEFAULT_MOVIE_POLICY = PolicyBody(
    media_type="movie",
    condemn_at=70,
    gates=(
        GateSetting(gate=GateId.WHITELISTED),
        GateSetting(gate=GateId.STREAMING_NOW),
        GateSetting(gate=GateId.UNMANAGED),
        GateSetting(gate=GateId.DATA_HORIZON),
        GateSetting(gate=GateId.CURATED_LIST),
        # THE MOST IMPORTANT GATE. Nothing under three years dormant may be deleted at
        # all, whatever else it scores. The rewatch rate decays slowly through the
        # first two years and then falls off a cliff at roughly three -- so below this
        # line, deleting is close to a coin-flip against your users. A GATE, not a
        # weight: a weight can be outvoted, and that is exactly how an early version
        # ended up condemning films with a large chance of coming back.
        GateSetting(gate=GateId.MIN_DORMANCY, threshold=1095),
        # 7.5 out of 10, with at least 1,000 votes. The vote floor is what makes this
        # mean anything: it rejects the handful of films in any library that rate
        # highly on a few hundred votes, which a bare rating floor would keep forever.
        GateSetting(gate=GateId.RATING_FLOOR, threshold=75, secondary=1000),
        # 3 distinct watchers IN THE LAST YEAR. Unwindowed, this protects nearly the
        # whole library and nothing is ever deletable. OTHERS_WATCHING is deliberately
        # absent: it belongs to the requester rule, where "others" means "somebody
        # other than the person who asked". In a general policy it would degenerate
        # into protecting anything ever played by anyone.
        GateSetting(gate=GateId.SERVER_POPULARITY, threshold=3, window_days=365),
    ),
    signals=(
        # Dormancy dominates, and the numbers come from the measured rewatch curve:
        # floor at 365 (below which a third of films come back) and saturating at 1825
        # (beyond which the rate is 2%).
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
)


def combine_hashes(*hashes: str) -> str:
    """A stable hash over several policy hashes, in the order given.

    A snapshot is scored under two policies -- one for movies, one for TV -- so its single
    ``policy_hash`` / ``scoring_hash`` is the combination of both, in a fixed order (movie,
    then TV). The simulator recombines the same way to check whether a stored score still
    describes an edited policy, without needing a per-media-type column on the snapshot.
    """
    return hashlib.sha256("".join(hashes).encode("ascii")).hexdigest()
