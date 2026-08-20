# SPDX-License-Identifier: AGPL-3.0-or-later
"""The frozen "why" record: the stored explanation document and how to read it.

Written by ``services.snapshot._explain``, served by ``api.review`` as the why panel, and
consulted by ``services.condemned`` before a hand reap is honored. It lives here, below both,
because those two readers must agree on one question -- **can this document be read at all?**
-- and they answer it by running the same validation rather than by each testing a different
part of the row (rule 104).

That is not the arrangement this started with. The panel's test was ``Explanation(**decoded)``
raising, which is any bad field anywhere in the document; the reap path's was a non-dict test
over the two protections lists alone, which is a much narrower thing. A row whose
``signals[].contribution`` was a string, or whose ``coverage`` was a list, passed the second and
failed the first -- so the operator was shown a panel with no signals, no protections and no
threshold, above a Reap button that still condemned. ``engine.verdict`` promises they "cannot
consent to reasons the panel never rendered", and for that class of row they could (#142).

These are Pydantic models rather than plain dataclasses because the validation IS the readability
test: what the panel can render is exactly what the model accepts. ``api.schemas`` re-exports them
so the wire names and the published OpenAPI components are unchanged.

**They declare the document; ``services.snapshot._explain`` writes it, and the two are held
together by a test rather than by one of them generating the other.**
``test_engine_derivations.TestTheStoredExplanationIsWrittenAsItIsDeclared`` walks every object the
writer produces against the model that types it.

Making the writer build these models instead was measured and refused. The three
``mode="before"`` validators below keep an illegible STORED byte from blanking the panel. On the
write side that same leniency turns a writer's own value into ``None``, with nothing raising:
``Explanation(match=MatchOut(...))`` yields ``match=None`` today, because ``_thaw_match`` reads a
submodel instance as "not a mapping". That drops the merged listing keys
``executor._equivalent_keys`` hands the streaming veto, and clears the bad-match hold on a hand
reap.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from reaper.engine.gates import thaw_defers_to_owner
from reaper.engine.signals import SignalState


class ReasonKey(BaseModel):
    """A typed detail on the wire: the catalog key ``why.<k>`` plus its params.

    Param values may be nested reason keys (``{"k": ..., "p": ...}``) or lists of them --
    the rating gate's per-bar clauses, a blocked check's cause -- so ``p`` stays untyped
    here and the frontend's composer recurses (``frontend/src/why.ts``). Rows written
    before the conversion carry a prose ``detail`` and no key; rows written after carry a
    key and no prose. See docs/I18N_PLAN.md §5.
    """

    k: str
    p: dict[str, object] | None = None


def thaw_reason_key(value: object) -> ReasonKey | None:
    """Read a stored detail key, or nothing where the row carries no legible one.

    One derivation for every reader of this byte (rule 104), and lenient the way the gate
    flags are (rule 96): a malformed key fails THIS clause, never the enclosing
    ``Explanation`` -- which would blank the operator's whole panel over one field the
    prose ``detail`` may still cover.
    """
    if isinstance(value, ReasonKey):
        return value
    if isinstance(value, dict) and isinstance(value.get("k"), str):
        p = value.get("p")
        return ReasonKey(k=value["k"], p=p if isinstance(p, dict) else None)
    return None


class SignalContribution(BaseModel):
    id: str
    contribution: float
    weight: int
    detail: str | None = None
    """The prose line of a row frozen before details were typed, rendered verbatim.
    ``None`` on every row written since; ``detail_key`` carries the sentence instead."""

    detail_key: ReasonKey | None = None
    """The typed detail the panel composes from the catalog. ``None`` on a legacy row."""

    evaluated: bool
    """False means the input was Unknown. Its weight still counts in the denominator,
    so an unevaluated signal drags the score DOWN, never up."""

    state: SignalState | None = None
    """What a zero actually means: it pushed toward removing, it argued for keeping, it
    did not apply, or it could not be read. See ``engine.signals.SignalState``.

    Optional because snapshots taken before this field existed carry rows without it.
    The UI's fallback for such a row is ``not_applicable``, never ``argues_keep``:
    claiming an old row argued for keeping, when nothing recorded whether it did, would
    overstate the case for keeping the file."""

    floor: int | None = None
    """The ramp this row was scored against: no points below ``floor``, all of them at
    ``saturate_at``. The panel states the arithmetic from these two.

    They are frozen here rather than read off the live policy because the policy may have
    moved since the scan, and the panel would then explain a score using a line that score
    was never computed against. Rule 113 refuses a reap across the same gap.

    ``None`` in two cases the UI must not tell apart by guessing: a rule with no ramp (a
    boolean custom rule matched or did not), and a row frozen before these fields shipped.
    Both mean "no line to state", so both render the plain row the panel showed before
    (rule 142's three-state -- a `0` default would assert a line about every legacy row)."""
    saturate_at: int | None = None

    @field_validator("detail_key", mode="before")
    @classmethod
    def _thaw_detail_key(cls, value: object) -> ReasonKey | None:
        """Lenient for ``thaw_reason_key``'s reason: one bad key must not blank the panel."""
        return thaw_reason_key(value)


class GateOutcomeOut(BaseModel):
    """One protection's outcome on one item.

    ``defers_to_owner`` has three states and folding the third into ``false`` asserts
    something nobody established: ``true`` means Reaper made the comparison behind this
    hold, ``false`` means it could not make it, and ``null`` means nothing legible in the
    row tells those two apart -- either a row frozen before the flag existed, or one
    carrying a value that is not a bool (``engine.gates.thaw_defers_to_owner``). Read it
    off ``protections_unknown``; entries in the other two lists never carry it, so every
    one of them reads ``null`` for a reason that is not the third state.

    ``unestablishable`` is three-state on the same terms and separates a different pair:
    ``true`` means the check never ran at all, ``false`` that it ran and left its answer to
    the operator, and ``null`` a row frozen before the flag. The two are read together --
    a season keep-rule conflict is ``unestablishable: false``, and ``defers_to_owner`` then
    says whether the comparison behind it was one Reaper made.

    Said here rather than on the field because a class docstring is the only one of the
    two that reaches the published document: Pydantic harvests attribute docstrings only
    under ``use_attribute_docstrings``, which this tree does not set, so the contract
    below this line is invisible to anyone building against ``/api/docs``.
    """

    gate: str
    detail: str | None = None
    """The prose line of a row frozen before details were typed, rendered verbatim.
    ``None`` on every row written since; ``detail_key`` carries the sentence instead."""

    detail_key: ReasonKey | None = None
    """The typed detail the panel composes from the catalog. ``None`` on a legacy row."""

    defers_to_owner: bool | None = None
    """Whether the comparison behind this hold is one Reaper actually made.

    Set only by the season keep-rule guard (``services.season_evidence.guard_result``), where a
    conflict arrives in three shapes and only one of them is a comparison: the other two are
    a kept season's watcher count that could not be read, and a watch mirror too short to
    stand behind the counts it reports. All three send the item to the operator; only the
    first may be described to them as "watched more than a season your rule keeps".

    **Three-state, and the third state is what matters (rule 142).** A row frozen before
    the flag existed carries no key, and nothing in it can tell the shapes apart -- the
    wording that used to stand in for the flag is exactly what failed. ``None`` is that row,
    and a plain ``bool`` here would silently assert one shape about every legacy
    explanation. ``None`` is also a row carrying a value that is not a bool at all: the
    validator below reads it there rather than coercing it (which contradicts the chip) or
    refusing it (which blanks the whole panel), because a value nobody can read tells the
    shapes apart exactly as well as no value does. ``services.snapshot._explain`` writes the
    key on every ``protections_unknown`` entry, never omitting it when ``False``, so
    present-and-``False`` stays distinguishable from absent ON DISK -- and on the wire the
    absent key arrives as ``null``, since nothing here or on the route sets ``exclude_none``.

    The same model types ``protections_fired`` and ``protections_checked``, where the key is
    never written and every entry therefore reads ``None``. That is honest for a gate with no
    opinion, but it is NOT "no answer recorded" for the one row that appears in two lists: an
    unestablishable season PROTECT rides in both, and only its ``protections_unknown`` copy
    carries the flag. Read this field off ``protections_unknown``, which is where the guard
    that sets it puts its blocked result.

    Declared here because a wire schema must name every key the UI reads: ``api.review._chip``
    already reads it off the stored row, and until this field existed the panel that opens
    beside that chip could not, so it went on running the retired wording test and asserted a
    comparison its own reason block denied (#86)."""

    unestablishable: bool | None = None
    """Whether this block is a check that never ran, as against one that ran and left its
    answer to the operator.

    ``GateResult.unestablishable`` carries what sets it and why only the season guard does.
    What matters at this boundary is the third state: ``None`` is a row frozen before the
    flag, and nothing in such a row tells the two apart. The panel reads ``None`` as "not
    this", which is how those rows already render -- before the flag, the only season guard
    result reaching this list on an abstaining item was a keep-rule conflict, so the legacy
    reading and the correct one are the same reading.

    Written on every ``protections_unknown`` entry by ``services.snapshot._explain``, never
    omitted when ``False``, so present-and-``False`` stays distinguishable from absent on
    disk. Declared here because a wire schema must name every key the UI reads
    (``WhyPanel.keepRuleConflict``)."""

    @field_validator("detail_key", mode="before")
    @classmethod
    def _thaw_detail_key(cls, value: object) -> ReasonKey | None:
        """Lenient for the reason ``thaw_reason_key`` records: one bad key must not blank
        the panel. ``mode="before"`` so it runs instead of pydantic's strict parse."""
        return thaw_reason_key(value)

    @field_validator("defers_to_owner", "unestablishable", mode="before")
    @classmethod
    def _thaw_gate_flag(cls, value: object) -> bool | None:
        """Read the stored byte the way ``_chip`` reads it, which is the point (rule 104).

        ``mode="before"`` because the whole job is to run INSTEAD of Pydantic's bool coercion,
        not after it. Left to Pydantic, ``1``/``"true"`` became a chip-contradicting ``True``
        and ``2``/``"banana"``/``[]`` raised -- and a raise here does not degrade this one
        field, it fails the enclosing ``Explanation`` and blanks the operator's whole why
        panel while every other reader of the same row carries on. ``thaw_defers_to_owner``
        holds the reasoning and is the only place this rule is written down (#112).

        Both flags, one validator: they are gate flags off the same row under the same rule,
        and a second copy of it is how one of them would come to keep pydantic's coercion.
        """
        return thaw_defers_to_owner(value)


class MatchOut(BaseModel):
    """How (or whether) the item was bound to its Plex row. ``status`` is what the UI
    reads: quiet on ``matched``, a plain "kept to be safe" notice otherwise."""

    status: str | None = None
    by: str | None = None
    detail: str | None = None
    rating_key: int | None = None
    merged_rating_keys: list[int] | None = None
    """Every listing a merged bind covers, when one file is listed several times in Plex.
    Audit only; absent for a normal single-listing bind."""

    candidate_rating_keys: list[int] | None = None
    """The Plex rows an abstain was choosing between, on ``ambiguous`` and ``conflicted``.

    Display only, and the reason the panel can offer a way out: without it the operator is
    told Reaper could not tell which Plex row this is and given no link to any of them,
    because ``rating_key`` (which every jump link is built from) is null on exactly these
    rows. ``None`` on a bind, and on a record stored before this shipped."""


class KeepContributionOut(BaseModel):
    """A graded keep's pull on the score. ``evaluated=False`` means the input was
    Unknown, which takes the FULL discount -- fail-closed toward keeping."""

    name: str
    discount: float
    max_discount: float
    detail: str | None = None
    """Legacy prose, exactly as the two detail pairs above."""
    detail_key: ReasonKey | None = None
    evaluated: bool

    @field_validator("detail_key", mode="before")
    @classmethod
    def _thaw_detail_key(cls, value: object) -> ReasonKey | None:
        """Lenient for ``thaw_reason_key``'s reason: one bad key must not blank the panel."""
        return thaw_reason_key(value)


class RewatchOddsOut(BaseModel):
    """The Stage 2 rewatch-probability context (#554): what fraction of similarly-dormant
    titles got watched again, from the operator's own history (``docs/history/REWATCH_PLAN.md``,
    Stage 2, "Storage and display"). Display only -- no verdict input and no signal; the
    opt-in protective hold reads the frozen ``Facts.rewatch_cohort_n`` / ``rewatch_cohort_k``
    directly, never this block.

    ``n`` and ``k`` are the block's pooled cohort size and watched-again count, ``lo_days``
    and ``hi_days`` its half-open dormancy range (``hi_days`` null on the open tail bucket).
    In the ``"no_history"`` state there is no usable block, and ``n``/``k``/``lo_days`` carry
    the placeholder ``0``/``0``/``0.0`` -- the panel reads ``state`` first and never these
    three in that state."""

    n: int
    k: int
    lo_days: float
    hi_days: float | None
    state: Literal["measured", "thin", "no_history"]
    """``"measured"`` at or above ``gates.REWATCH_BLOCK_FLOOR_N``, ``"thin"`` below it,
    ``"no_history"`` when the item's dormancy has no usable block at all."""


def thaw_threshold(value: object) -> int | None:
    """Read the stored score-to-beat, or nothing where the row carries no legible one.

    One derivation for all three readers of this byte (rule 104), which is the same shape #112
    gave ``defers_to_owner`` and the twin rule 72 binds to it. ``review._chip`` and
    ``review._primary_reason`` each tested it with ``isinstance(value, int)`` and coped with
    anything else, while ``Explanation.threshold`` read it through pydantic's lax ``int | None``
    -- a different rule, which REFUSES ``70.5`` and ``"abc"``. A refusal does not degrade this
    one field: it fails the enclosing ``Explanation``, drops ``_explanation_out`` to its degraded
    body, and hands the operator a panel with no signals, no protections and no threshold, beside
    a chip that read the same row perfectly well.

    A ``bool`` is not accepted even though Python calls it an ``int``: a threshold of ``True`` is
    not a score of 1, it is a row nobody can read. All three readers go through here, so that
    judgment is made once rather than three times.

    ``Explanation.coverage_floor_bp`` is the same class of frozen policy int and thaws the same
    way, so its validator reads it here too (rule 72): a floor of ``True`` is not 50 bp either.
    """
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


class Explanation(BaseModel):
    """The why-panel.

    Three blocks, and the last two are what make a verdict trustworthy. Every
    competitor shows which rules matched. None of them show the work.

    ``match``, ``keeps`` and the score split are optional with safe defaults: the
    stored explanation JSON has carried them since they shipped, but pydantic's
    ``extra="ignore"`` silently DROPPED them at this boundary until the fields were
    declared here -- which is why the panel's "kept to be safe" notice and keep
    breakdown never rendered. Wire schemas must name every key the UI reads.
    """

    score: float
    base_score: float | None = None
    keep_discount: float | None = None
    threshold: int | None = None
    """The score an item had to beat. ``None`` only when the stored explanation could not
    be read at all and the panel is showing the degraded fallback (review._explanation_out):
    the panel omits its "your threshold is N" clause rather than print an invented figure."""
    coverage: float
    coverage_floor_bp: int | None = None
    """The share of an item's scoring weight that had to be readable before a verdict, in
    basis points (5000 = 50%). Frozen beside ``threshold`` because it is the same class of
    value: a policy number the verdict is decided against. An abstain forced by the floor
    scores at or above ``threshold`` and is held anyway, so the panel restates this line to
    name what stopped it (``decide_verdict``'s order: coverage before score).

    Frozen, not read off the live policy, for ``threshold``'s reason: the policy may have
    moved since the scan and the panel would then explain a hold against a floor the item
    was never measured against (rule 113).

    ``None`` where the stored explanation could not be read, or was frozen before this field
    shipped. Both mean "no line to state", and the panel drops the floor clause, exactly as
    it drops the threshold clause for a ``None`` threshold."""
    watch_blind: bool | None = None
    """Whether this title is held because plays recorded earlier stopped being readable.

    The panel offers the per-title escape on it (#275). Typed rather than read off an
    observation's reason text, which is operator copy and will be reworded (rule 92).

    Three-state (rule 142): ``None`` is "cannot tell" -- a row scanned before the key existed,
    or an item with no reading to judge -- and shows no control, because offering to discard a
    record on a guess is the wrong direction. ``False`` is the positive claim that the scan
    took a reading and it was honest.
    """
    match: MatchOut | None = None

    rewatch_odds: RewatchOddsOut | None = None
    """The rewatch-probability context (#554), both lanes: a movie's own dormancy block, a
    season's the show's, off the lane's per-scan fit. ``None`` for a row stored before the
    lane froze cohorts -- read as nothing to show, the same safe default every optional
    block here takes (rule 104)."""

    signals: list[SignalContribution]
    keeps: list[KeepContributionOut] = Field(default_factory=list)

    protections_fired: list[GateOutcomeOut]
    """Why it is being kept. A tool that only explains deletions cannot be trusted
    about keeps."""

    protections_checked: list[GateOutcomeOut]
    """Protections evaluated that did NOT fire -- **with the actual numbers**:
    "Untouched for 5 years, 7 months, past the 3 years it has to sit unwatched first."

    Every one of these is a whole sentence built by a gate's ABSTAIN branch in
    ``engine.gates``, and the example above is ``MinDormancyGate``'s, quoted verbatim. It used
    to be an invented ``"checked: <label> -- <numbers>"`` shape that no gate has ever emitted
    (#419), in every file that illustrated the feature at once, each author reading a different
    copy -- so ``test_repo_hygiene.test_the_documented_checked_example_is_one_a_gate_emits``
    derives the sentence by running the gate and names the files holding a copy, rather than a
    comment asking the next author to remember (rule 144)."""

    protections_unknown: list[GateOutcomeOut]
    """Protections that COULD NOT BE CHECKED. Rendered amber, not green. "We could not
    look" is not "we looked and it was fine", and displaying them alike is the entire
    Deleterr failure class."""

    @field_validator("threshold", "coverage_floor_bp", mode="before")
    @classmethod
    def _thaw_frozen_policy_int(cls, value: object) -> int | None:
        """Read an illegible frozen policy number as absent rather than failing the whole panel.

        ``mode="before"`` for the same reason ``GateOutcomeOut`` uses it: the job is to run
        INSTEAD of pydantic's coercion, not after it. ``None`` costs the operator nothing here --
        it is a state the panel already renders, by omitting the clause that would restate the
        number rather than printing an invented figure (see the field docstrings above).

        Both fields, one validator: ``threshold`` and ``coverage_floor_bp`` are frozen policy
        ints read the same way, and a second copy of the coercion is how one of them would come
        to keep pydantic's lax bool-as-int (the ``_thaw_gate_flag`` reasoning, rule 72).
        """
        return thaw_threshold(value)

    @field_validator("match", mode="before")
    @classmethod
    def _thaw_match(cls, value: object) -> object:
        """Read a match block that is not a mapping as absent, for the same reason (rule 72).

        ``review._match_status`` reads the stored match off the raw dict and copes with any
        shape; refusing it here blanks every other block on the panel with it. ``None`` is a
        shape the panel already handles, being what a row scanned before the match block existed
        carries.

        Scoped deliberately to the block's own shape: a mapping whose INNER fields are illegible
        still raises, and is left to do so rather than silently emptying a match the panel could
        have partly rendered. Rule 7/24 -- this guards the outer shape only, and says so.
        """
        return value if value is None or isinstance(value, dict) else None

    @field_validator("rewatch_odds", mode="before")
    @classmethod
    def _thaw_rewatch_odds(cls, value: object) -> object:
        """Read a rewatch-odds block that is not a mapping as absent, for the same reason
        ``_thaw_match`` does (rule 72): a row predating this field, or one carrying a
        non-mapping value, has nothing to show rather than a reason to blank the whole
        panel. Scoped to the outer shape only, exactly as ``_thaw_match`` is (rule 7/24)."""
        return value if value is None or isinstance(value, dict) else None


def read_explanation(decoded: object) -> Explanation | None:
    """One stored explanation as the panel renders it, or ``None`` where it cannot be read.

    **The single definition of "unreadable" for this document (rule 104).** Both readers that
    must agree call it: ``api.review._explanation_out``, which degrades the panel to an empty
    body, and ``services.condemned.reap_override_verdict_decoded``, which holds the hand reap.
    Deriving it twice is what let them disagree, and the permissive copy was the one on the
    destructive path (#142).

    The direction is fail-closed and deliberately so (rule 96, and the prime directive): a
    document this cannot read holds the file. It can only ever *withdraw* a reap that the panel
    was already refusing to explain, never permit one, so the widened test cannot delete anything
    the narrow one kept.

    ``None`` covers both a top level that is not a mapping -- a decode that failed, a stored
    ``null`` or list -- and a mapping that fails validation.
    """
    if not isinstance(decoded, dict):
        return None
    try:
        return Explanation(**decoded)
    except ValidationError:
        return None
