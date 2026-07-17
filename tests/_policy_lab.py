# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared glue for the policy permutation lab.

Turns a de-identified fact vector (see ``tests/fixtures/policy_lab_vectors.json``) back
into engine ``Facts`` and judges it exactly the way ``services.snapshot._judge_item``
does: same gate builder, same scorer, same rounding, same single decision function. The
lab never re-implements a production decision; it replays the production code over
recorded shapes.

The fixture is generated from a real library by ``scripts/policy_lab_extract.py``. It
contains only shapes and ratios: observation states, day counts, watcher counts, rounded
sizes and vote counts, genre tokens, season numbers. No titles, ids, keys, or hosts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reaper.engine.gates import (
    ABSTAIN,
    PROTECT,
    Evaluation,
    Facts,
    GateId,
    GateResult,
    evaluate_all,
)
from reaper.engine.observation import Absent, Known, Unknown
from reaper.engine.policy import PolicyBody
from reaper.engine.signals import SignalConfig, score
from reaper.engine.verdict import STRUCTURAL_GATES, decide_verdict
from reaper.services.scan_runner import build_gates

FIXTURE = Path(__file__).parent / "fixtures" / "policy_lab_vectors.json"

#: Facts fields whose Known values are integers on the wire.
INT_FIELDS = frozenset(
    {
        "distinct_watchers",
        "distinct_watchers_all_time",
        "size_bytes",
        "imdb_rating_tenths",
        "imdb_votes",
        "season_rank",
    }
)

#: Facts fields a permutation may degrade to Unknown (everything observable).
DEGRADABLE = (
    "days_observed_unwatched",
    "distinct_watchers",
    "distinct_watchers_all_time",
    "imdb_rating_tenths",
    "imdb_votes",
    "season_rank",
    "is_streaming_now",
    "is_managed",
    "in_curated_list",
    "is_whitelisted",
    "genres",
    "release_age_days",
    "quality",
    "requested",
    "show_ended",
)

VERDICT_RANK = {"protect": 0, "abstain": 1, "condemn": 2}


def load_fixture() -> dict[str, Any]:
    with FIXTURE.open() as f:
        return json.load(f)


def to_observation(name: str, obs: dict[str, Any]) -> Known[Any] | Absent | Unknown:
    state = obs["state"]
    if state == "known":
        value = obs["value"]
        if name in INT_FIELDS:
            value = int(value)
        return Known(value=value, source="lab")
    if state == "absent":
        return Absent(source="lab")
    return Unknown(reason="recorded as unobservable", source="lab")


def to_facts(vector: dict[str, Any]) -> Facts:
    f = vector["facts"]
    return Facts(
        title="item",
        days_observed_unwatched=to_observation(
            "days_observed_unwatched", f["days_observed_unwatched"]
        ),
        distinct_watchers=to_observation("distinct_watchers", f["distinct_watchers"]),
        distinct_watchers_all_time=to_observation(
            "distinct_watchers_all_time", f["distinct_watchers_all_time"]
        ),
        size_bytes=to_observation("size_bytes", f["size_bytes"]),
        imdb_rating_tenths=to_observation("imdb_rating_tenths", f["imdb_rating_tenths"]),
        imdb_votes=to_observation("imdb_votes", f["imdb_votes"]),
        season_rank=to_observation("season_rank", f["season_rank"]),
        is_streaming_now=to_observation("is_streaming_now", f["is_streaming_now"]),
        is_managed=to_observation("is_managed", f["is_managed"]),
        in_curated_list=to_observation("in_curated_list", f["in_curated_list"]),
        is_whitelisted=to_observation("is_whitelisted", f["is_whitelisted"]),
        others_watching=to_observation("others_watching", f["others_watching"]),
        requested=to_observation("requested", f["requested"]),
        genres=to_observation("genres", f["genres"]),
        release_age_days=to_observation("release_age_days", f["release_age_days"]),
        quality=to_observation("quality", f["quality"]),
        show_ended=to_observation("show_ended", f["show_ended"]),
    )


def guard_result(vector: dict[str, Any]) -> GateResult | None:
    """The season-pruning guard outcome recorded for this vector, as the scan merges it."""
    guard = vector.get("guard")
    if not guard:
        return None
    if guard["state"] == "fired":
        return GateResult(GateId.SEASON_PROGRESSION, PROTECT, detail="season guard")
    if guard["state"] == "unknown":
        return GateResult(
            GateId.SEASON_PROGRESSION, ABSTAIN, blocked=True, detail="keep-rule conflict"
        )
    return GateResult(GateId.SEASON_PROGRESSION, ABSTAIN, detail="prunable")


def judge(
    vector: dict[str, Any],
    policy: PolicyBody,
    gates: list[Any] | None = None,
    *,
    facts: Facts | None = None,
) -> tuple[str, int, int, Evaluation, Any]:
    """Mirror of ``services.snapshot._judge_item``: evaluate, score, round, decide.

    Returns ``(verdict, score, coverage_bp, evaluation, score_object)``. ``gates`` may be
    passed to reuse a built list across many vectors; ``facts`` to reuse or perturb them.
    """
    if gates is None:
        gates = build_gates(policy)
    if facts is None:
        facts = to_facts(vector)
    extra: list[GateResult] = []
    if vector.get("override") == "spare":
        extra.append(GateResult(GateId.WHITELISTED, PROTECT, detail="spared by hand"))
    if (g := guard_result(vector)) is not None:
        extra.append(g)
    evaluation = Evaluation(results=[*extra, *evaluate_all(gates, facts).results])
    signal_configs = [
        SignalConfig(signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor)
        for s in policy.signals
    ]
    item_score = score(
        signal_configs,
        facts,
        custom_condemn=policy.custom_signal_configs(),
        keeps=policy.keep_configs(),
        window_days=policy.popularity_window_days(),
    )
    score_value = round(item_score.value)
    coverage_bp = round(item_score.coverage * 10_000)
    verdict = decide_verdict(
        protected=evaluation.protected,
        blocked=evaluation.blocked,
        safety_protected=any(r.fired and r.gate in STRUCTURAL_GATES for r in evaluation.results),
        score=score_value,
        coverage_bp=coverage_bp,
        condemn_at=policy.condemn_at,
        coverage_floor_bp=policy.coverage_floor_bp,
        override=vector.get("override"),
    )
    return verdict, score_value, coverage_bp, evaluation, item_score


def degraded(vector: dict[str, Any], names: list[str]) -> dict[str, Any]:
    """A copy of the vector with the named facts flipped to Unknown."""
    copy = {**vector, "facts": {**vector["facts"]}}
    for name in names:
        copy["facts"][name] = {"state": "unknown"}
    return copy


def with_watchers_window(vector: dict[str, Any], window_days: int) -> dict[str, Any]:
    """A copy with ``distinct_watchers`` recomputed for a different popularity window,
    from the per-viewer recency list the fixture carries."""
    copy = {**vector, "facts": {**vector["facts"]}}
    if vector["facts"]["distinct_watchers"]["state"] == "known":
        count = sum(1 for d in vector.get("play_recency_days", ()) if d <= window_days)
        copy["facts"]["distinct_watchers"] = {"state": "known", "value": count}
    return copy
