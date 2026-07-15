# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wire formats.

Note that route return types must be **resolvable at runtime**. ``from __future__
import annotations`` turns them into strings, and FastAPI builds a response model by
resolving them -- so a type imported only under ``TYPE_CHECKING`` yields a 500 at
request time rather than an error at import time. There is a test that walks every
route and forces its response model to resolve.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from reaper.engine.fields import FieldType, Lane, Op
from reaper.engine.gates import GateId
from reaper.engine.signals import SignalId


class SignalContribution(BaseModel):
    id: str
    contribution: float
    weight: int
    detail: str
    evaluated: bool
    """False means the input was Unknown. Its weight still counts in the denominator,
    so an unevaluated signal drags the score DOWN, never up."""


class GateOutcomeOut(BaseModel):
    gate: str
    detail: str


class Explanation(BaseModel):
    """The why-panel.

    Three blocks, and the last two are what make a verdict trustworthy. Every
    competitor shows which rules matched. None of them show the work.
    """

    score: float
    threshold: int
    coverage: float

    signals: list[SignalContribution]

    protections_fired: list[GateOutcomeOut]
    """Why it is being kept. A tool that only explains deletions cannot be trusted
    about keeps."""

    protections_checked: list[GateOutcomeOut]
    """Protections evaluated that did NOT fire -- **with the actual numbers**:
    "checked: recently watched -- last play 612d ago, your floor is 730d"."""

    protections_unknown: list[GateOutcomeOut]
    """Protections that COULD NOT BE CHECKED. Rendered amber, not green. "We could not
    look" is not "we looked and it was fine", and displaying them alike is the entire
    Deleterr failure class."""


class CandidateOut(BaseModel):
    id: int
    media_key: str
    title: str
    media_type: str
    size_bytes: int
    verdict: str
    score: int
    coverage_bp: int
    first_flagged_at: str | None = None
    # Display fields captured at scan time. None of them change the verdict; they are
    # what the review queue draws around it.
    year: int | None = None
    summary: str | None = None
    poster_url: str | None = None
    requested_by: str | None = None
    group_key: str | None = None
    group_title: str | None = None
    reason: str | None = None
    """The one-line "why", drawn from the explanation: the protection that keeps a spared
    item, or the top reason a reaped one scored. It is what the card shows in place of a plot
    synopsis -- on the review queue you want to know why Reaper judged it, not what it is."""
    spared: bool = False
    """True if the owner has hand-spared this media_key (or its show). Lets the queue strike it
    through before the next scan moves it to the Spared list for real."""
    override: str | None = None
    """The owner's manual decision on this item -- ``"spare"``, ``"reap"``, or ``None``. Set
    the moment they click, so the card can show the pending intent before the next scan bakes
    it into the stored verdict. Inherited from the show for a season the owner overrode whole."""


class CandidateDetail(CandidateOut):
    explanation: Explanation


class SnapshotOut(BaseModel):
    id: int
    created_at: str
    policy_hash: str
    horizon_at: str
    item_count: int

    degraded: bool
    """No run may execute against a degraded snapshot. It may still be VIEWED -- the
    owner should be able to see exactly what went wrong."""

    degraded_reason: str | None = None

    condemned: int = 0
    protected: int = 0
    abstained: int = 0
    reclaimable_bytes: int = 0


class ProfileSettingsIO(BaseModel):
    """The caps, grace and approval settings -- how much Reaper may do, and how long it
    waits. Deliberately not part of the policy hash: tightening a cap never voids a
    pending approval. Validation (a run cap above the rolling cap, a grace under a week)
    is enforced by the domain, so an out-of-range value comes back as a 422 with the
    reason, not a silent clamp.
    """

    max_items_per_run: int = Field(ge=1, le=1000)
    max_bytes_per_run: int = Field(ge=1)
    max_items_per_30d: int = Field(ge=1)
    max_bytes_per_30d: int = Field(ge=1)
    grace_days: int = Field(ge=7)
    require_approval: bool = True


class ActionStepOut(BaseModel):
    """One journalled action, exactly as it would be sent. No credentials.

    This is the third block of the why-panel made literal: the actual method, path and
    body the approval would issue. Safe to render because the api key is injected only at
    send time, never stored here.
    """

    media_key: str
    ordinal: int
    kind: str
    method: str
    path: str
    body: dict[str, object] | None = None
    state: str
    is_canary: bool


class RunOut(BaseModel):
    """A planned or executed reap run: the durable record of what Reaper would do."""

    id: int
    snapshot_id: int
    policy_hash: str
    state: str

    item_count: int
    total_bytes: int
    confirmation_phrase: str
    """The content-bound typed confirmation, e.g. "REAP 7 ITEMS 214 GB". Derived from the
    exact set this run would delete, so a stale plan reads as obviously different."""

    approved_manifest_hash: str
    approved_by: str
    approved_at: str

    steps: list[ActionStepOut]


class RunCheckOut(BaseModel):
    """One line in an item's after-action checklist: a step the reap did or verified, and
    whether it passed. Rendered as a ``✓``/``✗`` tick, like the why-panel's checks."""

    label: str
    ok: bool


class RunOutcomeOut(BaseModel):
    """What happened to one item in the run."""

    media_key: str
    title: str
    kind: str
    state: str  # verified | failed | skipped
    detail: str
    checks: list[RunCheckOut]


class RunReportOut(BaseModel):
    """What a run did, or -- in a dry run -- would do. Every mutating step in a dry run is
    proven and none is sent; in a real run each was issued and its result verified."""

    run_id: int
    dry_run: bool
    state: str
    aborted_reason: str | None = None

    would_delete_items: int
    """The count of items removed. In a real run this is what was actually deleted; in a
    dry run it is 0, because a dry run proves the plan by skipping every send."""

    deleted_bytes: int = 0
    """Bytes reclaimed by a real run. 0 for a dry run."""

    skipped: int
    outcomes: list[RunOutcomeOut]
    """Per item: what happened, with a plain-English checklist of the steps performed and
    which (if any) failed."""


class CreateRunIn(BaseModel):
    """Optional body for building a plan. Omit for a plan over the whole condemned set;
    pass ``media_keys`` to reap just those items -- the safe path for a first, single,
    hand-picked deletion, and the future 'reap selected' action."""

    media_keys: list[str] | None = None


class ExecuteRunIn(BaseModel):
    """The typed, content-bound confirmation required to execute a real reap.

    Not a checkbox: the phrase carries the exact count and size ("REAP 7 ITEMS 214 GB"),
    derived from the plan, so muscle memory cannot carry someone through it and a stale
    tab's phrase no longer matches the current plan. The server recomputes the expected
    phrase and refuses anything else.
    """

    confirmation_phrase: str


class GateSettingIn(BaseModel):
    gate: GateId
    enabled: bool = True
    threshold: int = 0
    secondary: int = 0
    window_days: int = Field(default=365, ge=1)


class SignalSettingIn(BaseModel):
    signal: SignalId
    weight: int = Field(ge=0, le=100)
    saturate_at: int = Field(ge=1)
    floor: int = Field(default=0, ge=0)


class ConditionIn(BaseModel):
    """One user-authored protect condition: keep a title when ``field op value``."""

    field: str
    op: Op
    value: int | str | bool


class PolicyIn(BaseModel):
    name: str = "default"
    media_type: str = "movie"
    condemn_at: int = Field(ge=1, le=100)
    coverage_floor_bp: int = Field(default=5000, ge=0, le=10_000)
    keep_last_seasons: int = Field(default=2, ge=0)
    keep_first_season: bool = True
    gates: list[GateSettingIn]
    signals: list[SignalSettingIn]
    protect_conditions: list[ConditionIn] = Field(default_factory=list)
    keep_tags: list[str] = Field(default_factory=lambda: ["reaper-keep"])
    keep_tags_match: Literal["any", "all"] = "any"


class PolicyWarningOut(BaseModel):
    field: str
    message: str
    severity: str


class PolicyOut(BaseModel):
    policy_hash: str
    name: str
    body: PolicyIn
    warnings: list[PolicyWarningOut]
    """Things that are legal but probably not what you meant. A validator cannot tell
    an IMDb floor of 96 (meaning 9.6) from a Rotten Tomatoes 96 typed into the wrong
    box -- both are legal -- so it says so instead of pretending to know."""


class SimulationOut(BaseModel):
    """Re-deciding the last snapshot under a candidate policy. Zero API calls.

    The point is that the knob and its blast radius sit in the same viewport: move the
    threshold, watch the count and the byte total move with it.
    """

    exact: bool
    """Whether these numbers actually answer the question that was asked.

    The simulator re-compares **stored** scores and verdicts against new thresholds.
    That is exact for ``condemn_at`` and ``coverage_floor_bp``, and **wrong for
    anything else**: change a signal weight or a gate and the stored scores were
    produced by the old ones, so every count below would be confidently stale.

    When this is false the counts are zeroed and ``stale_reason`` says why. Reaper
    would rather show nothing than show a number it cannot stand behind -- a plausible
    wrong answer is worse than a blank, because the owner acts on it.
    """

    stale_reason: str | None = None

    condemned: int
    protected: int
    abstained: int
    reclaimable_bytes: int

    newly_condemned: int
    """Items this policy would condemn that the current one does not. The number the
    owner actually needs before saving."""

    no_longer_condemned: int

    histogram: list[int]
    """Score distribution in 10-point buckets, so the threshold can be placed against
    the shape of the library rather than guessed."""


class FieldOut(BaseModel):
    key: str
    label: str
    help_text: str
    type: FieldType
    unit_suffix: str
    ops: list[Op]


class VocabularyOut(BaseModel):
    """The fields available in ONE lane.

    Filtered server-side, before serialisation. A protect-only field is never even
    offered to the condemn editor, so a dangerous condition is not merely rejected --
    it is unconstructable.
    """

    lane: Lane
    fields: list[FieldOut]


class BacktestOut(BaseModel):
    cutoff: str
    condemn_at: int

    condemned: int
    reclaimable_bytes: int
    protected: int

    rescued: int
    """Played DURING the grace window, so the pre-delete re-check spares them. NOT
    regrets -- counting rescues as failures would slander the policy."""

    regrets: int
    """Played AFTER the grace period expired. Media that was actually gone when a real
    person went looking for it."""

    regret_rate: float
    expected_regret_rate: float
    """What you'd get by picking randomly among films of the same age."""

    lift: float
    """How much better than age alone. **Negative means the signals are worse than
    nothing** and no profile may be armed."""

    prior_is_derived: bool
    """Was the baseline measured on THIS library, or borrowed? A lift number computed
    against somebody else's library is worth nothing."""

    beats_random: bool
    regret_titles: list[str]


class LeavingSoonOut(BaseModel):
    """The result of reconciling the Leaving Soon label set against the grace set."""

    to_add_count: int
    to_remove_count: int
    applied: bool
    """Whether the label writes landed. False in read-only mode -- writing a label is
    guarded like a delete, so the plan is computed and announced but not written."""
    notified: bool
    sample_added: list[str]


class GraceItemOut(BaseModel):
    media_key: str
    title: str
    size_bytes: int
    grace_ends_at: str
    days_remaining: int
    in_grace: bool


class GraceReportOut(BaseModel):
    grace_days: int
    in_grace_count: int
    ready_count: int
    total_bytes_in_grace: int
    total_bytes_ready: int
    in_grace: list[GraceItemOut]
    ready: list[GraceItemOut]


class RequesterRowOut(BaseModel):
    """One person's row in the fairness leaderboard."""

    name: str
    requests_made: int
    gb_granted_bytes: int
    played_by_them: int
    reclaimable_items: int
    reclaimable_bytes: int
    unwatched_titles: list[str]


class FairnessReportOut(BaseModel):
    total_requests: int
    total_reclaimable_bytes: int
    total_reclaimable_items: int
    unmatched_requests: int
    rows: list[RequesterRowOut]


class WhitelistEntryOut(BaseModel):
    """One hand-overridden item, as the "Spared" / overrides list shows it."""

    media_key: str
    title: str
    note: str | None
    decision: str
    """``"spare"`` (never reap) or ``"reap"`` (force onto the reap list)."""
    created_at: str


class SpareIn(BaseModel):
    """Spare an item. The title is looked up server-side from the latest snapshot, so
    the client sends only what identifies the file and, optionally, why."""

    media_key: str
    note: str | None = Field(default=None, max_length=500)


class OverrideIn(BaseModel):
    """Override an item's verdict by hand -- spare it (keep) or reap it (force onto the list).

    The title is resolved server-side from the item's latest candidate, so the client sends
    only the identity, the decision, and optionally a note. A ``media_key`` may be a whole
    show's, in which case the decision applies to every one of its seasons."""

    media_key: str
    decision: Literal["spare", "reap"]
    note: str | None = Field(default=None, max_length=500)


class HealthOut(BaseModel):
    status: str
    version: str
    destructive_actions_enabled: bool
    safety_note: str | None = None
