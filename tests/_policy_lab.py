# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared glue for the policy permutation lab.

Turns a de-identified fact vector (see ``tests/fixtures/policy_lab_vectors.json``) back
into engine ``Facts`` and hands it to the scan's own judging function --
``services.snapshot.judge_facts``, the pure half of ``_judge_item`` -- so the sweep
exercises production rather than a lookalike. The lab never re-implements a production
decision; it replays the production code over recorded shapes.

The fixture is generated from a real library by ``scripts/policy_lab_extract.py``. It
contains only shapes and ratios: observation states, day counts, watcher counts, rounded
sizes and vote counts, genre tokens, season numbers. No titles, ids, keys, or hosts.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from reaper.engine.gates import ABSTAIN, PROTECT, Evaluation, Facts, GateId, GateResult
from reaper.engine.observation import Absent, Known, Unknown
from reaper.engine.policy import PolicyBody
from reaper.engine.signals import SignalConfig
from reaper.ratings import Rating, RatingSource
from reaper.services.scan_runner import build_gates
from reaper.services.snapshot import HAND_SPARE_DETAIL, effective_fate, judge_facts

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


@lru_cache(maxsize=1)
def mirror_reach_days() -> float:
    """How far back the watch history behind these vectors reached, as a lower bound.

    The reach is a property of the *mirror*, not of an item, so a fixture that records
    items has no field for it -- but it is recoverable from what the fixture does record:
    a play logged 3,000 days ago proves the mirror held a row that old, so the oldest play
    across every vector is a floor under the real reach.

    Stated rather than left to the default, which is ``Absent``: ``ServerPopularityGate``
    reads that as "cannot check how far back the history goes" and blocks, so a lab that
    omitted it would pin a baseline of 440 un-checkable rows and stop exercising the gate
    at all (rule 132 -- infrastructure may not imply coverage it does not have).
    """
    return max(
        (day for v in load_fixture()["vectors"] for day in v.get("play_recency_days", ())),
        default=0.0,
    )


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


def _ratings_from(f: dict[str, Any]) -> tuple[Rating, ...]:
    """Synthesize the multi-source rating set from the fixture's IMDb fields.

    The fixture predates the multi-source gate and records only the IMDb dataset value
    (``imdb_rating_tenths`` / ``imdb_votes``). Production turns that same value into a
    ``Rating`` in ``Facts.ratings`` (services.snapshot.build_facts), so mirroring it here
    lets the pinned baseline keep reproducing under the new gate without regenerating the
    fixture -- the number the gate reads is identical, just carried as a Rating."""
    tenths = f["imdb_rating_tenths"]
    if tenths.get("state") != "known":
        return ()
    votes_obs = f["imdb_votes"]
    votes = int(votes_obs["value"]) if votes_obs.get("state") == "known" else None
    return (
        Rating(
            source=RatingSource.IMDB,
            value=int(tenths["value"]) / 10,
            votes=votes,
            provider="imdb-dataset",
        ),
    )


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
        requested=to_observation("requested", f["requested"]),
        genres=to_observation("genres", f["genres"]),
        release_age_days=to_observation("release_age_days", f["release_age_days"]),
        quality=to_observation("quality", f["quality"]),
        show_ended=to_observation("show_ended", f["show_ended"]),
        history_reach_days=Known(value=mirror_reach_days(), source="lab"),
        # Stated, and stated Unknown, which is the one honest reading here (rule 35). The
        # span an all-time count needs is how long the item has been on the server, and
        # unlike the reach above that is NOT recoverable in the safe direction. The oldest
        # play under a vector does prove the item is at least that old, but a FLOOR under
        # the age makes ``reach >= age`` pass more readily, so it would hand the lab a
        # count the real scan would have refused -- the opposite of ``mirror_reach_days``,
        # where understating the reach only ever blocks more.
        #
        # What that costs, measured over all 440 vectors, because rule 132 forbids
        # implying coverage the fixture does not have. ``test_policy_permutations`` DOES
        # author rules on this field -- ``TestProtectConditions`` sweeps every PROTECT-lane
        # key and ``NUMERIC_VALUES`` lists ``watchers_all_time`` -- and half the outcome
        # matrix is now unreachable for it:
        #
        #   graded keep    -> {40.0: 440}: every vector at the maximum discount, so
        #                     "a keep never raises a score" holds trivially and would stay
        #                     green if the ramp broke outright. The unbounded twin still
        #                     spreads: ``recent_watchers`` gives four discount values under
        #                     the sweep's own ``saturate_at`` (the field's largest
        #                     ``NUMERIC_VALUES`` entry, 3), and six at the 5 used here, so
        #                     the collapse is the reach bound and not the spec.
        #   protect gte 1  -> 415 matched, 25 blocked, 0 checked-and-did-not-fire
        #   protect lte 5  -> 268 blocked, 172 checked-and-did-not-fire, 0 matched
        #
        # The two surviving arms are exactly the outcomes ``_survives_more_history`` lets
        # through as already earned; the two a deeper mirror could overturn are all
        # blocked. That is the guard working, not a bug, but the sweep no longer exercises
        # the ramp or either overturnable arm for this field. Closing it properly needs an
        # arrival date in the fixture, which is a regeneration
        # (``scripts/policy_lab_extract.py``), not a default.
        days_since_added=Unknown(reason="the lab fixture records no arrival date", source="lab"),
        ratings=_ratings_from(f),
    )


def guard_result(vector: dict[str, Any]) -> GateResult | None:
    """The season-pruning guard outcome recorded for this vector, as the scan merges it.

    ``"unknown"`` models ONE of the blocked shapes ``season_scan.guard_result`` can emit:
    the keep-rule conflict whose comparison was made and lost, which sets
    ``defers_to_owner``. Three have no vector here, because nothing in the policy sweep
    varies what they turn on -- the refused conflicts (a kept season nobody could read, and
    a comparison the watch mirror is too short to settle) turn on the mirror, and the
    unestablishable arm on whether the show bound to Plex at all. All three reach the same
    verdict as this one (no block holds a hand reap) and differ in what the operator is
    shown. Each is pinned directly in ``tests/test_season_scan.py`` and
    ``tests/test_review_chips.py``.
    ``tests/test_override_truth.py`` covers the stored-row path and carries only the
    unreadable-kept shape, not the shortfall one -- and cannot tell them apart anyway, since
    both encode as ``defers_to_owner: False`` once frozen (rule 132: this helper must not
    read as coverage of an arm it does not build, nor credit a file with a shape it lacks).
    """
    guard = vector.get("guard")
    if not guard:
        return None
    if guard["state"] == "fired":
        return GateResult(GateId.SEASON_PROGRESSION, PROTECT, detail="season guard")
    if guard["state"] == "unknown":
        return GateResult(
            GateId.SEASON_PROGRESSION,
            ABSTAIN,
            blocked=True,
            detail="keep-rule conflict",
            defers_to_owner=True,
        )
    return GateResult(GateId.SEASON_PROGRESSION, ABSTAIN, detail="prunable")


def judge(
    vector: dict[str, Any],
    policy: PolicyBody,
    gates: list[Any] | None = None,
    *,
    facts: Facts | None = None,
) -> tuple[str, int, int, Evaluation, Any]:
    """Judge one vector with the SCAN's own pipeline: ``snapshot.judge_facts``.

    Returns ``(fate, score, coverage_bp, evaluation, score_object)``, where ``fate`` is the
    EFFECTIVE one -- the hand override applied on top of the pure-policy verdict, exactly as
    ``snapshot._judge_item`` returns it. ``gates`` may be passed to reuse a built list across
    many vectors; ``facts`` to reuse or perturb them.

    This function used to rebuild the pipeline by hand, and had already drifted twice in ways
    that only a vector the fixture does not happen to contain would expose: it decided without
    ``blocked_holds_reap`` (so a hand reap on a keep-rule conflict protected here and condemned
    in production), and it pushed the override straight through ``decide_verdict`` rather than
    deriving the reap's fate from the frozen explanation the way every read-side consumer does.
    Both are the rule 22 shape, and the danger is specific: the fixture's pinned baseline is
    generated by THIS function, so a lab that disagrees with the scan does not fail -- it
    records the disagreement as ground truth. Call production; never mirror it.
    """
    if gates is None:
        gates = build_gates(policy)
    if facts is None:
        facts = to_facts(vector)
    extra: list[GateResult] = []
    if vector.get("override") == "spare":
        # In a real scan a hand spare is on the keep list, so the whitelist gate fires from
        # Facts. The fixture records the gate's states separately from the override, so the
        # PROTECT is injected here to reproduce that row -- the same shape snapshot passes
        # through ``extra_results``.
        extra.append(GateResult(GateId.WHITELISTED, PROTECT, detail=HAND_SPARE_DETAIL))
    if (g := guard_result(vector)) is not None:
        extra.append(g)
    judged = judge_facts(
        facts,
        gates,
        policy,
        signals=[
            SignalConfig(signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor)
            for s in policy.signals
        ],
        custom_condemn=policy.custom_signal_configs(),
        keeps=policy.keep_configs(),
        window_days=policy.popularity_window_days(),
        extra_results=extra,
    )
    fate = effective_fate(judged, vector.get("override"))
    return fate, judged.score, judged.coverage_bp, judged.evaluation, judged.item_score


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
