# SPDX-License-Identifier: AGPL-3.0-or-later
"""Freezing a scan's per-item evidence, and thawing it exactly.

A scan gathers each item's :class:`~reaper.engine.gates.Facts` once, scores it, and throws
the Facts away -- only the scoring *outputs* (the rounded score, the verdict) survive on the
Candidate row. That is why re-deciding a snapshot under a new weight or a new rating bar used
to need a full re-scan: the raw inputs were gone.

This module serializes the Facts (and the season-pruning guard result merged alongside them)
to canonical JSON so the simulator can replay the **real** engine -- ``score``,
``evaluate_all``, ``decide_verdict`` -- over the frozen evidence with zero API calls, and get
a verdict bit-identical to a fresh scan of the same evidence.

**The three-state ``Observation`` must round-trip exactly.** ``Known``/``Absent``/``Unknown``
are not interchangeable: an ``Unknown`` serialized as ``Absent`` would flip the scorer's
fail-safe arithmetic from "weight retained, pushes score down" to "evaluated as real absence",
a fail-OPEN regression. Every field is tagged with its arm, and a round-trip test asserts an
arbitrary Facts survives unchanged.
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
    """Every ``Observation``-typed field on ``Facts``, read off the dataclass itself.

    ``cls`` is a parameter so a test can hand it a stand-in for the ``Facts`` of some future
    commit -- one with a field added, or one with a field this module could not encode --
    and check the two outcomes below without waiting for that commit to exist.

    Derived, never transcribed. A hand-written list is a fail-**open** waiting to happen:
    every custom-rule field on ``Facts`` carries a default of ``_UNSET`` (an ``Absent``), so
    one added to ``Facts`` and forgotten here would construct fine, round-trip as ``Absent``,
    and silently drop whatever keep discount the real value would have earned (rule 35). No
    exception, no failing assertion, just a simulator that quietly disagrees with the scan.

    A field that is neither an observation nor one of the two handled by hand raises here, at
    import: nothing else in this module would serialize it, so it would vanish across the
    freeze in the same silence. Loud at build time beats wrong at scan time.
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
    # A bare id (``"imdb_unreadable"``) stores as itself, same as always. A producer that
    # needed a param (the movie/season media select) hands ``Unknown`` a full ``Reason``
    # instead (``gates.no_key_reason`` and friends), and that one is wire-encoded like any
    # other stored ``Reason`` so its params round-trip too.
    reason = to_wire(obs.reason) if isinstance(obs.reason, Reason) else obs.reason
    return {"k": "unknown", "r": reason, "s": obs.source}


def _obs_from_dict(d: dict[str, Any]) -> Observation[Any]:
    kind = d["k"]
    if kind == "known":
        return Known(value=d["v"], source=d["s"])
    if kind == "absent":
        return Absent(source=d["s"])
    # A reason frozen before the i18n conversion is the English sentence itself, not a
    # catalog id. It stays that string: a re-decision wraps it as a cause via
    # ``gates.blocked_reason``, which a consumer keyed on catalog ids simply will not
    # match, and the frontend's missing-entry fallback (``why.ts`` composeIn) renders it
    # raw -- the same "not translated" reading it had before the conversion. A dict is the
    # media-typed shape ``to_wire`` produces above, and decodes back through ``from_wire``.
    raw = d["r"]
    reason = from_wire(raw) if isinstance(raw, dict) else raw
    return Unknown(reason=reason, source=d["s"])


def _rating_to_dict(r: Rating) -> dict[str, Any]:
    # as_of is always None in production (from_plex/from_radarr set it), so it is not
    # serialized; a future dated rating would add it here.
    return {"src": r.source.value, "val": r.value, "votes": r.votes, "prov": r.provider}


def _rating_from_dict(d: dict[str, Any]) -> Rating:
    return Rating(
        source=RatingSource(d["src"]), value=d["val"], votes=d["votes"], provider=d["prov"]
    )


def _result_to_dict(r: GateResult) -> dict[str, Any]:
    # Names every ``GateResult`` field one by one, so a field added to the dataclass and not
    # added here is silently dropped. ``defers_to_owner`` was exactly that: the season guard
    # is the only producer that sets it AND the only result frozen through here, so the
    # simulator replay read every keep-rule conflict as a plumbing failure while the scan
    # read it as the owner's call to make. Rule 103's drift guard is the field-coverage test
    # in ``tests/test_facts_codec.py``; keep this list under it.
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
        # A str here is a detail frozen before reasons were typed; it thaws as a legacy
        # reason and renders raw, exactly as it did before (docs/history/I18N_PLAN.md §5).
        detail=from_wire(d["detail"]),
        blocked=d["blocked"],
        # The thaw, stated rather than left to a ``KeyError`` (rule 104): a row frozen before
        # the flag existed carries nothing that distinguishes a comparison that was made from
        # one that was refused, so it reads as "Reaper did not establish this" -- the claim
        # that asserts less. That answer reaches the operator's chip (``api.review._chip``),
        # not any reap decision: no blocked gate holds a hand reap (``engine.verdict``).
        #
        # Through the shared reader, which is where that rule is now written down, even though
        # this consumer is two-state by construction: ``GateResult.defers_to_owner`` is a plain
        # ``bool``, so "cannot tell" and "refused" collapse here on purpose. Routing it anyway
        # means a change to what counts as a legible flag lands on all three readers at once
        # rather than on the two somebody remembered (#112).
        defers_to_owner=thaw_defers_to_owner(d.get("defers_to_owner")) is True,
        # Same thaw, deliberately the same function: both are gate flags off the same frozen
        # row, and one reader of "what counts as a legible flag" is the whole point of routing
        # either through here (rule 104). A row frozen before this flag reads False, which is
        # what those rows meant -- the only season guard result reaching the blocked list on an
        # abstaining item was a keep-rule conflict.
        unestablishable=thaw_defers_to_owner(d.get("unestablishable")) is True,
    )


def facts_to_dict(facts: Facts, *, extra_results: tuple[GateResult, ...] = ()) -> dict[str, Any]:
    """The frozen evidence for one item: its Facts plus any extra gate results merged into
    its evaluation (the season-pruning guard). Stored as ``Candidate.facts_json``.

    The season guard is frozen alongside so a stored row can EXPLAIN itself without the
    show's bundle. It is no longer what a re-decision reads: the scan freezes the plan's
    inputs per show (``db.models.SeasonPruneEvidence``) and the simulator re-derives the
    guard from them through ``services.season_evidence.plan_from_frozen``.

    So the hash no longer stands behind this value. The nine season fields are in
    ``policy.PolicyBody._EVIDENCE_REPLAYABLE_FIELDS``, which means a season edit does not move
    ``evidence_hash`` and does not force a fresh scan. What refuses instead is
    ``api.simulate._season_guard_replay``, asked of the stored evidence rather than of the
    policy -- a hash cannot say whether a show's bundle is present and readable.
    """
    return {
        "title": facts.title,
        "obs": {name: _obs_to_dict(getattr(facts, name)) for name in _OBS_FIELDS},
        "ratings": [_rating_to_dict(r) for r in facts.ratings],
        "extra": [_result_to_dict(r) for r in extra_results],
    }


#: Why a field is unreadable on a snapshot written before that field existed.
#:
#: One of seven reasons in ``src/`` with NO ``why.cause.*`` catalog entry, and the
#: reasoning here is about where it can be READ: ``facts_from_dict`` has a single caller,
#: the policy simulator (``api.simulate``), which reads a re-decided score and verdict and
#: never builds or stores an ``Explanation``. So this string reaches a reader only through
#: a stored explanation that does not exist, and giving it panel copy would claim a route
#: it cannot take (rule 25). Named anyway, so the ban on hand-typed reasons has no
#: exception and the exemption is a line in ``test_review_chips.py`` rather than a
#: silence. If a route ever renders a thawed Facts, it wants a catalog entry and this
#: comment is the wrong answer.
NOT_RECORDED_REASON = "not_recorded"


def facts_from_dict(d: dict[str, Any]) -> tuple[Facts, tuple[GateResult, ...]]:
    """Rebuild the Facts and its frozen extra results from :func:`facts_to_dict` output.

    A field the stored snapshot does not carry thaws as ``Unknown``, not ``Absent``. Old
    snapshots outlive the code that wrote them: add a field to ``Facts`` and every scan
    already on disk is missing it. ``Unknown`` is the honest reading -- that scan never
    looked -- and it is the fail-safe one: the gates abstain on it and the scorer keeps its
    weight while adding no pressure, so an old snapshot re-decides toward keeping rather
    than inventing a real absence the scan never observed.
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
