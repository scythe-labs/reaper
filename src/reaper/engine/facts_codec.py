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

from reaper.engine.gates import Facts, GateId, GateResult
from reaper.engine.observation import Absent, Known, Observation, Unknown
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
    return {"k": "unknown", "r": obs.reason, "s": obs.source}


def _obs_from_dict(d: dict[str, Any]) -> Observation[Any]:
    kind = d["k"]
    if kind == "known":
        return Known(value=d["v"], source=d["s"])
    if kind == "absent":
        return Absent(source=d["s"])
    return Unknown(reason=d["r"], source=d["s"])


def _rating_to_dict(r: Rating) -> dict[str, Any]:
    # as_of is always None in production (from_plex/from_radarr set it), so it is not
    # serialized; a future dated rating would add it here.
    return {"src": r.source.value, "val": r.value, "votes": r.votes, "prov": r.provider}


def _rating_from_dict(d: dict[str, Any]) -> Rating:
    return Rating(
        source=RatingSource(d["src"]), value=d["val"], votes=d["votes"], provider=d["prov"]
    )


def _result_to_dict(r: GateResult) -> dict[str, Any]:
    return {"gate": r.gate.value, "outcome": r.outcome, "detail": r.detail, "blocked": r.blocked}


def _result_from_dict(d: dict[str, Any]) -> GateResult:
    return GateResult(
        gate=GateId(d["gate"]), outcome=d["outcome"], detail=d["detail"], blocked=d["blocked"]
    )


def facts_to_dict(facts: Facts, *, extra_results: tuple[GateResult, ...] = ()) -> dict[str, Any]:
    """The frozen evidence for one item: its Facts plus any extra gate results merged into
    its evaluation (the season-pruning guard). Stored as ``Candidate.facts_json``.

    The season guard is frozen alongside because it is computed from Sonarr data the replay
    cannot re-derive; a change to the season-pruning policy that would move it is caught by
    the evidence hash (``policy.PolicyBody.evidence_hash``), which forces a fresh scan rather
    than replaying a stale guard.
    """
    return {
        "title": facts.title,
        "obs": {name: _obs_to_dict(getattr(facts, name)) for name in _OBS_FIELDS},
        "ratings": [_rating_to_dict(r) for r in facts.ratings],
        "extra": [_result_to_dict(r) for r in extra_results],
    }


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
        else Unknown(reason="this scan did not record it", source="snapshot")
        for name in _OBS_FIELDS
    }
    facts = Facts(
        title=d.get("title", ""),
        ratings=tuple(_rating_from_dict(r) for r in d.get("ratings", [])),
        **kwargs,
    )
    extra = tuple(_result_from_dict(r) for r in d.get("extra", []))
    return facts, extra
