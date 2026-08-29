# SPDX-License-Identifier: AGPL-3.0-or-later
"""The frozen "why" record: the stored explanation document and how to read it.

Written by ``services.snapshot._explain``, served by ``api.review`` as the why panel, and
read by ``services.condemned`` before a hand reap is honored. Both readers must agree on
one question: can this document be read at all? They must answer it by calling the same
function, ``read_explanation``, never by each testing a different part of the stored
row: a row that passed one reader's own check but failed the other's could otherwise show
the operator a blank panel while still letting the reap proceed.

These must be Pydantic models, never plain dataclasses: validation IS the
readability test, and what the panel can render is exactly what the model accepts.
``api.schemas`` re-exports them so the wire names and the published OpenAPI components
match.

``services.snapshot._explain`` writes the document that these models declare.
``test_engine_derivations.TestTheStoredExplanationIsWrittenAsItIsDeclared`` checks the two
stay in sync, by walking every object the writer produces against the model that types it.

The writer must emit plain dicts, never instances of these models. The ``mode="before"``
validators below expect raw stored data: handed a real ``MatchOut`` instance,
``_thaw_match`` reads it as "not a mapping" and silently stores ``match=None``. That
drops the streaming veto's merged listing keys (``executor._equivalent_keys``) and
clears the bad-match hold on a hand reap.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from reaper.engine.gates import thaw_defers_to_owner
from reaper.engine.reason import legacy, to_wire
from reaper.engine.signals import SignalState


class ReasonKey(BaseModel):
    """A typed detail on the wire: the catalog key ``why.<k>`` plus its params.

    A param value may be a nested reason key (``{"k": ..., "p": ...}``) or a list of them,
    for the rating gate's per-bar clauses or a blocked check's cause. So ``p`` stays
    untyped here, and the frontend's composer recurses (``frontend/src/why.ts``). An older
    stored row carries a prose ``detail`` and no key; a newer row carries a key and no
    prose. See docs/history/I18N_PLAN.md §5.
    """

    k: str
    p: dict[str, object] | None = None


def thaw_reason_key(value: object) -> ReasonKey | None:
    """Read a stored detail key, or nothing where the row carries no legible one.

    Every reader of this stored value calls this one function. It is lenient on purpose: a
    malformed key fails only this field, never the enclosing ``Explanation``. Raising there
    would blank the operator's whole panel over one field that the prose ``detail`` may
    still cover.
    """
    if isinstance(value, ReasonKey):
        return value
    if isinstance(value, dict) and isinstance(value.get("k"), str):
        p = value.get("p")
        return ReasonKey(k=value["k"], p=p if isinstance(p, dict) else None)
    return None


def absorb_legacy_detail(data: object) -> object:
    """Fold an older prose ``detail`` into ``detail_key``, so every reader handles one shape.

    An older stored row carries prose ``detail`` and no ``detail_key``; a newer row carries
    the reverse. Wrapping the prose as a ``legacy`` reason here means every downstream
    reader only ever reads ``detail_key``, and the sentence still reaches the panel through
    that key. Two callers use this: each of the three models below, in its own
    ``model_validator(mode="before")`` (Pydantic validators are declared per model), and
    ``api.review._detail_reason``, which reads a stored entry as a raw dict and never
    builds these models at all.

    Folds whenever the stored ``detail_key`` is missing or unreadable. This is checked
    through ``thaw_reason_key``, never by presence alone: a hand-edited or corrupted row
    can carry a ``detail_key`` that reads as nothing, and the prose beside it is the only
    legible text such a row has left.
    """
    if not isinstance(data, dict) or thaw_reason_key(data.get("detail_key")) is not None:
        return data
    detail = data.get("detail")
    if not isinstance(detail, str) or not detail:
        return data
    thawed = dict(data)
    thawed["detail_key"] = to_wire(legacy(detail))
    thawed.pop("detail", None)
    return thawed


class SignalContribution(BaseModel):
    id: str
    contribution: float
    weight: int
    detail_key: ReasonKey | None = None
    """The typed detail the panel composes from the catalog. An older row carries prose
    ``detail`` instead; ``absorb_legacy_detail`` wraps it as a ``legacy`` reason and stores
    it here before validation runs."""

    evaluated: bool
    """``False`` means the input was Unknown. Its weight still counts in the denominator,
    so an unevaluated signal can only lower the score."""

    state: SignalState | None = None
    """What a zero actually means. It pushed toward removing, argued for keeping, did not
    apply, or could not be read. See ``engine.signals.SignalState``.

    Optional because a snapshot taken before this field existed carries rows without it.
    The UI reads such a row as ``not_applicable``, never ``argues_keep``. Claiming an old
    row argued for keeping, when nothing recorded whether it did, would overstate the case
    for keeping the file."""

    floor: int | None = None
    """The ramp this row was scored against: no points below ``floor``, all of them at
    ``saturate_at``. The panel states the arithmetic from these two.

    This must be frozen here, never read off the live policy: the policy may have moved
    since the scan, and reading it live would explain a score using a line the score was
    never computed against. A run's approval is bound to its policy hash and refuses to run
    again across the same kind of edit.

    ``None`` in two cases the UI must not tell apart by guessing: a rule with no ramp (a
    boolean custom rule that matched or did not), and a row frozen before these fields
    shipped. Both mean there is no line to state, so both render the plain row the panel
    showed before this field existed. A default of ``0`` would wrongly assert a line for
    every such row."""
    saturate_at: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_detail(cls, data: object) -> object:
        return absorb_legacy_detail(data)

    @field_validator("detail_key", mode="before")
    @classmethod
    def _thaw_detail_key(cls, value: object) -> ReasonKey | None:
        """Lenient for the same reason ``thaw_reason_key`` is: one bad key must not blank
        the panel."""
        return thaw_reason_key(value)


class GateOutcomeOut(BaseModel):
    """One protection's outcome on one item.

    ``defers_to_owner`` has three states. ``true`` means Reaper made the comparison
    behind this hold. ``false`` means it could not make that comparison. ``null`` means
    the row does not say which of those two is true, either because it was frozen before
    this flag existed or because it holds a value that is not a bool
    (``engine.gates.thaw_defers_to_owner``). Folding ``null`` into ``false`` would claim
    something nobody actually checked. Read this field off ``protections_unknown`` only;
    entries in the other two lists never carry a real value here.

    ``unestablishable`` is three-state the same way, for a different pair. ``true`` means
    the check never ran. ``false`` means it ran and left the answer to the operator.
    ``null`` means a row frozen before this flag existed. Read the two fields together: a
    season keep-rule conflict is ``unestablishable: false``, and ``defers_to_owner`` then
    says whether Reaper made the comparison behind it.

    This must be documented here, never on each field: Pydantic only publishes a field's
    own docstring to the API schema when ``use_attribute_docstrings`` is turned on, and
    this project leaves it off. A docstring on the field would be invisible to
    anyone reading ``/api/docs``.
    """

    gate: str
    detail_key: ReasonKey | None = None
    """The typed detail the panel composes from the catalog. An older row carries prose
    ``detail`` instead; ``absorb_legacy_detail`` wraps it as a ``legacy`` reason and
    stores it here before validation runs."""

    defers_to_owner: bool | None = None
    """Whether the comparison behind this hold is one Reaper actually made.

    Set only by the season keep-rule guard (``services.season_evidence.guard_result``). A
    conflict there arrives in three shapes, and only one of them is a comparison: the
    other two are a kept season's watcher count that could not be read, and a watch
    mirror too short to stand behind the counts it reports. All three send the item to
    the operator, but only the first can be described to them as "watched more than a
    season your rule keeps".

    Three-state, and the third state matters. ``true`` means Reaper made the comparison.
    ``false`` means it could not. ``None`` means the row was frozen before this flag
    existed, or it holds a value that is not a bool. Reading ``None`` as ``false`` would
    assert something about an old row that nobody checked, so the validator below must
    leave it as ``None``, never coerce or refuse it. ``services.snapshot._explain``
    always writes this key on a ``protections_unknown`` entry, even when the value is
    ``false``, so "checked and false" stays distinguishable from "never written" in
    storage. On the wire, "never written" arrives as ``null``.

    The same model also types ``protections_fired`` and ``protections_checked``, where
    this key is never written, so every entry there reads ``None``. That is correct for a
    gate with no opinion on the comparison. For a row that appears in more than one list,
    the real answer lives only on ``protections_unknown``, which is where the guard that
    sets it puts its result: read this field there."""

    unestablishable: bool | None = None
    """Whether this block is a check that never ran, as against one that ran and left its
    answer to the operator.

    ``GateResult.unestablishable`` explains what sets this flag; only the season guard
    does. ``None`` means the row was frozen before this flag existed, and nothing in it
    tells the two states apart. The panel reads ``None`` the same way it already read
    those older rows, since a keep-rule conflict was the only season result reaching this
    list before the flag existed.

    ``services.snapshot._explain`` writes this key on every ``protections_unknown``
    entry, even when the value is ``false``, so "checked and false" stays distinguishable
    from "never written". The field is declared here because the wire schema must name
    every key the UI reads (``WhyPanel.keepRuleConflict``)."""

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_detail(cls, data: object) -> object:
        return absorb_legacy_detail(data)

    @field_validator("detail_key", mode="before")
    @classmethod
    def _thaw_detail_key(cls, value: object) -> ReasonKey | None:
        """Lenient for the reason ``thaw_reason_key`` gives: one bad key must not blank
        the panel. Runs ``mode="before"`` to replace pydantic's own strict parse."""
        return thaw_reason_key(value)

    @field_validator("defers_to_owner", "unestablishable", mode="before")
    @classmethod
    def _thaw_gate_flag(cls, value: object) -> bool | None:
        """Read the stored value the same way ``api.review._chip`` reads it.

        Runs ``mode="before"`` to replace pydantic's own bool coercion, never merely to
        run after it. Left to pydantic, ``1`` or ``"true"`` would become ``True``,
        contradicting the chip's own reading, and ``2``, ``"banana"``, or ``[]`` would
        raise. A raise here would fail the whole ``Explanation``, blanking the operator's
        whole why panel, while every other reader of the same row keeps
        working. ``thaw_defers_to_owner`` holds the full reasoning.

        Both fields share one validator because they are the same kind of flag on the
        same row, read the same way.
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
    For audit only. Absent for a normal single-listing bind."""

    candidate_rating_keys: list[int] | None = None
    """The Plex rows an abstain was choosing between, on ``ambiguous`` and ``conflicted``.

    Display only. It gives the panel a way out: without it, the operator would be told
    Reaper could not tell which Plex row this is, with no link to any of them, because
    ``rating_key`` (which every jump link is built from) is null on exactly these rows.
    ``None`` on a normal bind, and on a record stored before this field shipped."""


class KeepContributionOut(BaseModel):
    """A graded keep's pull on the score. ``evaluated=False`` means the input was
    Unknown, which takes the full discount. Missing data always favors keeping the
    file."""

    name: str
    discount: float
    max_discount: float
    detail_key: ReasonKey | None = None
    """The typed detail the panel composes from the catalog. An older row carries prose
    ``detail`` instead; ``absorb_legacy_detail`` wraps it as a ``legacy`` reason and
    stores it here before validation runs."""
    evaluated: bool

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_detail(cls, data: object) -> object:
        return absorb_legacy_detail(data)

    @field_validator("detail_key", mode="before")
    @classmethod
    def _thaw_detail_key(cls, value: object) -> ReasonKey | None:
        """Lenient for the same reason ``thaw_reason_key`` is: one bad key must not blank
        the panel."""
        return thaw_reason_key(value)


class RewatchOddsOut(BaseModel):
    """What fraction of similarly-dormant titles got watched again, from the operator's
    own history (``docs/history/REWATCH_PLAN.md``, Stage 2, "Storage and display").

    Display only. It plays no part in the verdict or the score; the opt-in protective
    hold reads the frozen ``Facts.rewatch_cohort_n`` / ``rewatch_cohort_k`` directly,
    never this block.

    ``n`` and ``k`` are the block's pooled cohort size and watched-again count.
    ``lo_days`` and ``hi_days`` are its half-open dormancy range, with ``hi_days`` null
    on the open tail bucket. In the ``"no_history"`` state there is no usable block, and
    ``n``, ``k`` and ``lo_days`` carry the placeholder ``0``, ``0`` and ``0.0``. The
    panel reads ``state`` first and never reads those three fields in that state."""

    n: int
    k: int
    lo_days: float
    hi_days: float | None
    state: Literal["measured", "thin", "no_history"]
    """``"measured"`` at or above ``gates.REWATCH_BLOCK_FLOOR_N``, ``"thin"`` below it,
    ``"no_history"`` when the item's dormancy has no usable block at all."""

    bound_pct: int | None = None
    """The Wilson 95% upper bound of ``k``/``n`` (``gates.wilson_upper``), as a whole
    percent. It is the same figure ``RewatchOddsGate`` compares against the operator's
    floor, so this display never shows a lower "probability" than the figure that can
    actually protect the item. ``None`` only for a row stored before this field shipped;
    the panel then computes it from ``k`` and ``n`` itself
    (``frontend/src/why.ts``'s ``wilsonUpperPct``)."""


def thaw_threshold(value: object) -> int | None:
    """Read the stored score-to-beat, or nothing where the row carries no legible one.

    Three readers share this one function. ``review._chip`` and ``review._primary_reason``
    each used to test the value with ``isinstance(value, int)`` and cope with anything
    else, while ``Explanation.threshold`` read it through pydantic's own lax
    ``int | None``, which refuses ``70.5`` or ``"abc"`` outright. A refusal there fails
    the whole ``Explanation``, dropping the panel to
    its degraded body and showing the operator no signals, no protections and no
    threshold, beside a chip that read the same row fine.

    A ``bool`` is rejected even though Python treats it as an ``int``. A ``True`` value
    here means the row is unreadable; it must never be scored as a threshold of 1.
    Routing all three readers through here makes that judgment once instead of three times.

    ``Explanation.coverage_floor_bp`` is the same kind of frozen policy number and reads
    the same way, through this same function: a floor of ``True`` also means the row is
    unreadable, never a real 50 basis points.
    """
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


class Explanation(BaseModel):
    """The why-panel: the complete record behind a verdict, covering the protections,
    signals, and keeps that decided it.

    ``match``, ``keeps`` and the score split are optional with safe defaults. The stored
    explanation JSON has carried them since they shipped, but pydantic's
    ``extra="ignore"`` silently dropped them at this boundary until they were declared
    here, which is why the panel's "kept to be safe" notice and keep breakdown never
    rendered. A wire schema must name every key the UI reads.
    """

    score: float
    base_score: float | None = None
    keep_discount: float | None = None
    threshold: int | None = None
    """The score an item had to beat. ``None`` only when the stored explanation could not
    be read at all, and the panel is showing its degraded fallback
    (``review._explanation_out``). The panel must omit its "your threshold is N" clause
    here, never print an invented figure."""
    coverage: float
    coverage_floor_bp: int | None = None
    """The share of an item's scoring weight that had to be readable before a verdict, in
    basis points (5000 = 50%). Frozen beside ``threshold`` because it is the same kind of
    value: a policy number the verdict is decided against. An item held by this floor can
    still score at or above ``threshold``, so the panel states this line to name what
    actually stopped it (``decide_verdict`` checks coverage before score).

    This must be frozen, never read off the live policy, for the same reason as
    ``threshold``: the policy may have changed since the scan, and reading it live would
    explain a hold
    against a floor the item was never measured against.

    ``None`` where the stored explanation could not be read, or where it was frozen
    before this field shipped. Both mean there is no line to state, so the panel drops
    the floor clause exactly as it drops the threshold clause for a ``None`` threshold."""
    watch_blind: bool | None = None
    """Whether this title is held because plays recorded earlier stopped being readable.

    The panel offers a per-title escape from this hold. This field must stay typed, never
    derived from an observation's reason text, because that text is operator copy and can
    be reworded independently.

    Three-state. ``None`` means "cannot tell": a row scanned before this field existed,
    or an item with no reading to judge. The panel shows no control in that case, because
    offering to discard a record on a guess is the wrong direction. ``False`` is the
    positive claim that the scan took a reading and it was honest.
    """
    match: MatchOut | None = None

    rewatch_odds: RewatchOddsOut | None = None
    """The rewatch-probability context, on both lanes: a movie's own dormancy block, or a
    season's off its show's, from that lane's per-scan fit. ``None`` for a row stored
    before a scan froze these cohorts, read as nothing to show, the same safe default
    every optional block here takes."""

    signals: list[SignalContribution]
    keeps: list[KeepContributionOut] = Field(default_factory=list)

    protections_fired: list[GateOutcomeOut]
    """Why it is being kept. A tool that only explains deletions cannot be trusted
    about keeps."""

    protections_checked: list[GateOutcomeOut]
    """Protections evaluated that did not fire, with the actual numbers: "Unwatched for
    5 years, 7 months, past the 3 years Reaper waits."

    Every one of these is a whole sentence built by a gate's ABSTAIN branch in
    ``engine.gates``. The example above is ``MinDormancyGate``'s, quoted verbatim.
    ``test_repo_hygiene.test_the_documented_checked_example_is_one_a_gate_emits`` runs
    the gate and checks this docstring still quotes a sentence it actually emits."""

    protections_unknown: list[GateOutcomeOut]
    """Protections that could not be checked. Rendered amber, because "we
    could not look" must never be shown the same way as "we looked and it was fine": that
    conflation is the mistake this design avoids."""

    @field_validator("threshold", "coverage_floor_bp", mode="before")
    @classmethod
    def _thaw_frozen_policy_int(cls, value: object) -> int | None:
        """Read an illegible frozen policy number as absent; never let it fail the whole panel.

        Runs ``mode="before"`` for the same reason ``GateOutcomeOut`` does: the job is to
        replace pydantic's own coercion, never merely run after it. ``None`` costs the
        operator nothing here, since it is a state the panel already renders, by omitting
        the clause that would restate the number; it never prints an invented figure
        (see the field docstrings above).

        Both fields share one validator: ``threshold`` and ``coverage_floor_bp`` are
        frozen policy numbers read the same way, and a second copy of the coercion is how
        one of them would come to keep pydantic's lax bool-as-int reading (the
        ``_thaw_gate_flag`` reasoning above).
        """
        return thaw_threshold(value)

    @field_validator("match", mode="before")
    @classmethod
    def _thaw_match(cls, value: object) -> object:
        """Read a match block that is not a mapping as absent, for the same reason.

        ``review._match_status`` reads the stored match off the raw dict and copes with
        any shape; refusing it here would blank every other block on the panel along
        with it. ``None`` is a shape the panel already handles, since it is what a row
        scanned before the match block existed carries.

        Scoped deliberately to the block's own shape: a mapping whose inner fields are
        illegible still raises. This must never silently empty out a
        match the panel could have partly rendered; it guards the outer shape only.
        """
        return value if value is None or isinstance(value, dict) else None

    @field_validator("rewatch_odds", mode="before")
    @classmethod
    def _thaw_rewatch_odds(cls, value: object) -> object:
        """Read a rewatch-odds block that is not a mapping as absent, for the same reason
        ``_thaw_match`` does: a row predating this field, or one carrying a non-mapping
        value, simply has nothing to show. This must never blank the whole panel over it.
        Scoped to the outer shape only, exactly as ``_thaw_match`` is."""
        return value if value is None or isinstance(value, dict) else None


def read_explanation(decoded: object) -> Explanation | None:
    """One stored explanation as the panel renders it, or ``None`` where it cannot be read.

    The single definition of "unreadable" for this document. Two readers call it and
    must agree: ``api.review._explanation_out``, which degrades the panel to an empty
    body, and ``services.condemned.reap_override_verdict_decoded``, which holds the hand
    reap on it.

    Fail-closed, deliberately: a document this cannot read holds the file. It can only
    ever withdraw a reap that the panel was already refusing to explain, never permit one
    it was blocking.

    ``None`` covers both a top level that is not a mapping (a failed decode, a stored
    ``null`` or list) and a mapping that fails validation.
    """
    if not isinstance(decoded, dict):
        return None
    try:
        return Explanation(**decoded)
    except ValidationError:
        return None
