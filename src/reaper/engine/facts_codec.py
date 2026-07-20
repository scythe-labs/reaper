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

from typing import Any

from reaper.engine.gates import Facts, GateId, GateResult
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.ratings import Rating, RatingSource

#: Every ``Observation``-typed field on Facts. Title and ratings are handled separately.
_OBS_FIELDS: tuple[str, ...] = (
    "days_observed_unwatched",
    "distinct_watchers",
    "distinct_watchers_all_time",
    "size_bytes",
    "imdb_rating_tenths",
    "imdb_votes",
    "season_rank",
    "is_streaming_now",
    "is_managed",
    "in_curated_list",
    "is_whitelisted",
    "others_watching",
    "requested",
    "genres",
    "release_age_days",
    "quality",
    "show_ended",
)


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
    """Rebuild the Facts and its frozen extra results from :func:`facts_to_dict` output."""
    obs = d["obs"]
    kwargs = {name: _obs_from_dict(obs[name]) for name in _OBS_FIELDS}
    facts = Facts(
        title=d["title"],
        ratings=tuple(_rating_from_dict(r) for r in d.get("ratings", [])),
        **kwargs,
    )
    extra = tuple(_result_from_dict(r) for r in d.get("extra", []))
    return facts, extra
