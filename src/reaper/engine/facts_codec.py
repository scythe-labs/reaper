# SPDX-License-Identifier: AGPL-3.0-or-later
"""Saving a scan's per-item evidence to disk, and reading it back exactly.

A scan gathers each item's :class:`~reaper.engine.gates.Facts` once, scores it, and
discards the Facts. Only the scoring outputs (the rounded score, the verdict) survive on
the Candidate row. This module serializes the Facts instead, plus the season-pruning
guard result merged alongside them, as JSON, so the policy simulator can replay the real
engine (``score``, ``evaluate_all``, ``decide_verdict``) over the saved evidence with no
API calls, and get a verdict identical to a fresh scan of the same evidence.

The three-state ``Observation`` must survive a round trip exactly. ``Known``, ``Absent``,
and ``Unknown`` are not interchangeable: saving an ``Unknown`` as an ``Absent`` would turn
"this lowers the score, but the weight still counts toward coverage" into "this is a real
absence", which can raise a score that should stay lowered. Every field is tagged with its
state, and a round-trip test asserts that an arbitrary Facts comes back unchanged.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from reaper.engine.gates import Facts, GateId, GateResult, thaw_defers_to_owner
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.engine.reason import Reason, from_wire, to_wire
from reaper.ratings import Rating, RatingSource

#: The two ``Facts`` fields that are not observations, each serialized by hand above.
_HANDLED_SEPARATELY = frozenset({"title", "ratings"})

#: An ``Observation[...]`` annotation. Compared as text because ``from __future__ import
#: annotations`` leaves every annotation a string; that is fine here, since the point is to
#: recognize the ones this module knows how to encode and *refuse* everything else.
_OBSERVATION_ANNOTATION = re.compile(r"^Observation\[")


def _observation_fields(cls: type = Facts) -> tuple[str, ...]:
    """Every Observation-typed field on ``Facts``, read directly off the dataclass.

    ``cls`` lets a test pass in a stand-in for a future version of ``Facts``, such as one
    with a field added or one with a field this module cannot encode, and check both
    outcomes below without waiting for that change to actually happen.

    This list is derived, never hand-written. A hand-written list would go stale silently:
    every custom-rule field on ``Facts`` defaults to ``_UNSET`` (an ``Absent``), so a field
    added to ``Facts`` and forgotten here would still construct without error, round-trip
    as ``Absent``, and silently drop whatever protection the real value would have given
    the item. Nothing would raise. The simulator would just quietly disagree with the scan.

    A field that is neither an observation nor one of the two handled by hand raises here,
    at import time. Nothing else in this module serializes it, so it would otherwise
    disappear silently when the evidence is saved. Failing loudly at startup beats failing
    silently during a scan.
    """
    observed: list[str] = []
    unhandled: list[str] = []
    for field in dataclasses.fields(cls):
        if field.name in _HANDLED_SEPARATELY:
            continue
        if _OBSERVATION_ANNOTATION.match(str(field.type)):
            observed.append(field.name)
        else:
            unhandled.append(field.name)
    if unhandled:
        raise RuntimeError(
            f"{cls.__name__} fields {unhandled} are neither Observation-typed nor handled by "
            "hand in facts_codec. Add each to the encoder (and to _HANDLED_SEPARATELY) or the "
            "frozen evidence will not carry it."
        )
    return tuple(observed)


_OBS_FIELDS: tuple[str, ...] = _observation_fields()


def _obs_to_dict(obs: Observation[Any]) -> dict[str, Any]:
    if isinstance(obs, Known):
        return {"k": "known", "v": obs.value, "s": obs.source}
    if isinstance(obs, Absent):
        return {"k": "absent", "s": obs.source}
    # A bare id (``"imdb_unreadable"``) stores as itself, the same as always. A producer
    # that needs a value attached (choosing movie or season wording) gives ``Unknown`` a
    # full ``Reason`` instead (``gates.no_key_reason`` and similar functions), and that
    # Reason is wire-encoded like any other, so its values round-trip too.
    reason = to_wire(obs.reason) if isinstance(obs.reason, Reason) else obs.reason
    return {"k": "unknown", "r": reason, "s": obs.source}


def _obs_from_dict(d: dict[str, Any]) -> Observation[Any]:
    kind = d["k"]
    if kind == "known":
        return Known(value=d["v"], source=d["s"])
    if kind == "absent":
        return Absent(source=d["s"])
    # An old row's reason is a finished English sentence, and it stays that string: a
    # re-decision wraps it as a cause via ``gates.blocked_reason``, which no catalog id
    # will match, so the frontend's missing-entry fallback (``why.ts`` composeIn)
    # renders it as-is, untranslated. A dict is the shape ``to_wire`` produces above,
    # and decodes back through ``from_wire``.
    raw = d["r"]
    reason = from_wire(raw) if isinstance(raw, dict) else raw
    return Unknown(reason=reason, source=d["s"])


def _rating_to_dict(r: Rating) -> dict[str, Any]:
    # as_of is always None today, since neither from_plex nor from_radarr sets it, so it
    # is not serialized. A future dated rating would add it here.
    return {"src": r.source.value, "val": r.value, "votes": r.votes, "prov": r.provider}


def _rating_from_dict(d: dict[str, Any]) -> Rating:
    return Rating(
        source=RatingSource(d["src"]), value=d["val"], votes=d["votes"], provider=d["prov"]
    )


def _result_to_dict(r: GateResult) -> dict[str, Any]:
    # Names every ``GateResult`` field one by one, so a field added to the dataclass and not
    # added here is silently dropped instead of saved. The field-coverage test in
    # ``tests/test_facts_codec.py`` checks that this list stays complete.
    return {
        "gate": r.gate.value,
        "outcome": r.outcome,
        "detail": to_wire(r.detail),
        "blocked": r.blocked,
        "defers_to_owner": r.defers_to_owner,
        "unestablishable": r.unestablishable,
    }


def _result_from_dict(d: dict[str, Any]) -> GateResult:
    return GateResult(
        gate=GateId(d["gate"]),
        outcome=d["outcome"],
        # A str here is a detail saved before reasons were typed. It comes back as a
        # legacy reason and renders raw, exactly as it did before.
        detail=from_wire(d["detail"]),
        blocked=d["blocked"],
        # Read back through the shared function rather than a raw dict lookup that could
        # raise KeyError. A row saved before this flag existed carries nothing that tells a
        # comparison that was made apart from one that was refused, so it reads as "Reaper
        # did not establish this," the answer that claims less. That reaches the operator's
        # chip (``api.review._chip``), not any reap decision: no blocked gate holds a hand
        # reap (``engine.verdict``).
        #
        # ``GateResult.defers_to_owner`` is a plain bool, so "cannot tell" and "refused"
        # both collapse to the same value here on purpose. Reading it through the shared
        # function anyway means a future change to what counts as a readable flag reaches
        # every reader at once, not just the ones someone remembers to update.
        defers_to_owner=thaw_defers_to_owner(d.get("defers_to_owner")) is True,
        # Read back the same way, through the same function: both fields are gate flags off
        # the same saved row. A row saved before this flag existed reads False, which is
        # what those rows meant. The only season guard result that could reach the blocked
        # list on an abstaining item was a keep-rule conflict.
        unestablishable=thaw_defers_to_owner(d.get("unestablishable")) is True,
    )


def facts_to_dict(facts: Facts, *, extra_results: tuple[GateResult, ...] = ()) -> dict[str, Any]:
    """The saved evidence for one item: its Facts, plus any extra gate results merged into
    its evaluation, such as the season-pruning guard. Stored as ``Candidate.facts_json``.

    The season guard is saved alongside so a stored row can explain itself without the
    show's whole bundle. A re-decision does not read this copy: the scan saves the plan's
    inputs per show (``db.models.SeasonPruneEvidence``), and the simulator rebuilds the
    guard from those through ``services.season_evidence.plan_from_frozen``.

    The nine season fields listed in ``policy.PolicyBody._EVIDENCE_REPLAYABLE_FIELDS`` do
    not move ``evidence_hash``, so editing them does not force a fresh scan.
    ``api.simulate._season_guard_replay`` checks the saved evidence directly instead, since
    a hash cannot say whether a show's bundle is present and readable.
    """
    return {
        "title": facts.title,
        "obs": {name: _obs_to_dict(getattr(facts, name)) for name in _OBS_FIELDS},
        "ratings": [_rating_to_dict(r) for r in facts.ratings],
        "extra": [_result_to_dict(r) for r in extra_results],
    }


#: Why a field is unreadable on a snapshot saved before that field existed.
#:
#: This id has no entry in the ``why.cause.*`` catalog. ``facts_from_dict`` has one
#: caller, the policy simulator (``api.simulate``), which reads a re-decided score and
#: verdict and never builds or stores an explanation for the operator to see. So this id
#: can never actually reach a why-panel, and giving it panel wording would claim a route
#: it cannot take. It is named anyway, and ``test_review_chips.py`` exempts it by name,
#: so the exemption is written down instead of silent. If a future route ever renders
#: Facts read back this way, it needs a real catalog entry, and this comment is the wrong
#: answer.
NOT_RECORDED_REASON = "not_recorded"


def facts_from_dict(d: dict[str, Any]) -> tuple[Facts, tuple[GateResult, ...]]:
    """Rebuild the Facts and its saved extra results from :func:`facts_to_dict` output.

    A field the stored snapshot does not carry comes back as ``Unknown``, not ``Absent``.
    Old snapshots outlive the code that wrote them: adding a field to ``Facts`` means every
    scan already on disk is missing it. ``Unknown`` is the honest reading, since that scan
    never looked, and it is the safe one: the gates abstain on it, and the scorer counts
    its weight toward coverage without adding to the score. An old snapshot re-decides
    toward keeping the file rather than inventing a real absence the scan never saw.
    """
    obs = d.get("obs", {})
    kwargs = {
        name: _obs_from_dict(obs[name])
        if name in obs
        else Unknown(reason=NOT_RECORDED_REASON, source="snapshot")
        for name in _OBS_FIELDS
    }
    facts = Facts(
        title=d.get("title", ""),
        ratings=tuple(_rating_from_dict(r) for r in d.get("ratings", [])),
        **kwargs,
    )
    extra = tuple(_result_from_dict(r) for r in d.get("extra", []))
    return facts, extra
