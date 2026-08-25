# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /api/policy/resolve-ratio``: resolve "one mistake per N cleared" into a
delete-threshold score, from the newest scan's own fitted rewatch curve
(docs/LEARNINGS.md, "The delete threshold buys volume, not precision").

The core is ``api.policy.resolve_ratio_curve``, a pure function over already-decoded rows
(``RatioCandidate``) -- covered directly here without a database. The route wiring (reading
the newest snapshot, the active policy's coverage floor, the wire shapes) gets its own,
smaller set of end-to-end cases below.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session

from reaper.api.policy import (
    RatioCandidate,
    _cohort,
    _measured_or_thin_rate,
    _mistake_probability,
    resolve_ratio_curve,
)
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Candidate, Snapshot
from reaper.engine.gates import wilson_upper
from reaper.main import create_app

from ._auth import login
from ._lists import seeded_fingerprint

# --------------------------------------------------------------------------- fixtures


def _measured(n: int, k: int) -> dict[str, object]:
    return {"state": "measured", "lo_days": 0.0, "hi_days": None, "n": n, "k": k}


def _thin(n: int, k: int) -> dict[str, object]:
    return {"state": "thin", "lo_days": 0.0, "hi_days": None, "n": n, "k": k}


_NO_HISTORY: dict[str, object] = {
    "state": "no_history",
    "lo_days": 0.0,
    "hi_days": None,
    "n": 0,
    "k": 0,
}


def _row(
    *,
    protected: bool = False,
    blocked: bool = False,
    score: int = 70,
    coverage_bp: int = 10_000,
    rewatch_odds: dict[str, object] | None = None,
) -> RatioCandidate:
    return RatioCandidate(
        protected=protected,
        blocked=blocked,
        score=score,
        coverage_bp=coverage_bp,
        rewatch_odds=rewatch_odds,
    )


# --------------------------------------------------------------------------- the core


class TestCohortReading:
    """``_cohort`` / ``_measured_or_thin_rate``: what one candidate's stored rewatch-odds
    block contributes, before any fallback is applied."""

    def test_measured_reads_the_wilson_upper_bound_like_the_gate_does(self) -> None:
        # Never the plain rate: a measured cohort with k=0 read as a bare 0.0 would zero
        # the scan's expected mistakes and let any ratio "resolve" at the bottom of the
        # score range (the review's demonstration; same bound RewatchOddsGate compares).
        bound = _measured_or_thin_rate(_measured(40, 8))
        assert bound == wilson_upper(8, 40)
        assert bound is not None and bound > 8 / 40

    def test_a_zero_comeback_measured_cohort_reads_above_zero(self) -> None:
        bound = _measured_or_thin_rate(_measured(200, 0))
        assert bound is not None and bound > 0

    def test_thin_reads_the_wilson_upper_bound_not_the_point_rate(self) -> None:
        # A thin cohort's own rate (1/5 = 0.2) is NOT what gets used -- the conservative
        # Wilson 95% upper bound is, and it is well above the point rate for n=5.
        odds = _thin(5, 1)
        bound = _measured_or_thin_rate(odds)
        assert bound == wilson_upper(1, 5)
        assert bound > 1 / 5

    def test_no_history_reads_as_nothing_usable(self) -> None:
        assert _measured_or_thin_rate(_NO_HISTORY) is None

    def test_missing_block_reads_as_nothing_usable(self) -> None:
        assert _measured_or_thin_rate(None) is None

    def test_bool_n_is_not_read_as_an_int(self) -> None:
        # JSON's true/false satisfy isinstance(_, int); a hand-corrupted row must not read
        # as a cohort of size 1.
        assert _cohort({"state": "measured", "n": True, "k": 0}) is None
        assert _cohort({"state": "measured", "n": 5, "k": False}) is None

    def test_mistake_probability_falls_back_when_nothing_usable(self) -> None:
        assert _mistake_probability(_NO_HISTORY, fallback=0.42) == 0.42
        assert _mistake_probability(None, fallback=0.42) == 0.42

    def test_mistake_probability_uses_its_own_rate_when_usable(self) -> None:
        assert _mistake_probability(_measured(40, 8), fallback=0.99) == wilson_upper(8, 40)


class TestResolveRatioCurve:
    """The resolver's contract, over hand-built rows with cohorts large enough that the
    Wilson bound sits near the plain rate. Ten "cheap" titles score 60 with a 60-of-300
    cohort (bound ~0.249); ten "expensive" (safer) titles score 90 with 3-of-300 (bound
    ~0.029). Below score 61 all twenty are flagged, ratio 20/(10*0.249+10*0.029) ~ 7.2;
    at or above 61 only the ten scored-90 titles are, ratio 10/(10*0.029) ~ 34.5.
    """

    @staticmethod
    def _library() -> list[RatioCandidate]:
        cheap = [_row(score=60, rewatch_odds=_measured(300, 60)) for _ in range(10)]
        expensive = [_row(score=90, rewatch_odds=_measured(300, 3)) for _ in range(10)]
        return cheap + expensive

    def test_ratio_8_resolves_above_the_cheap_lane(self) -> None:
        # Below score 61 the achieved ratio (~7.2) is short of 8; at or above 61 (through
        # 90) it is ~34.5. The resolver must land inside that second zone, not at score 1.
        result = resolve_ratio_curve(self._library(), ratio=8, coverage_floor_bp=0)

        assert result.state == "resolved"
        assert result.score == 61
        assert result.flagged_items == 10
        assert result.expected_mistakes == math.ceil(10 * wilson_upper(3, 300))

    def test_lowest_score_at_least_the_ratio_is_returned_not_any_satisfying_one(self) -> None:
        # Every threshold from 61 through 90 achieves the identical ~34.5 ratio (the
        # flagged set does not change across that span), so this is the case that actually
        # distinguishes "lowest" from "some" -- a resolver that scanned differently could
        # legally return anything in [61, 90] and still pass a looser assertion.
        result = resolve_ratio_curve(self._library(), ratio=10, coverage_floor_bp=0)

        assert result.state == "resolved"
        assert result.score == 61

    def test_ratio_50_is_unreachable_and_floors_at_the_best_available(self) -> None:
        # No threshold in the whole domain reaches 50 (the best is ~34.5, in the score
        # 61-90 zone), so this must FLOOR rather than silently resolve to something worse.
        result = resolve_ratio_curve(self._library(), ratio=50, coverage_floor_bp=0)

        assert result.state == "floored"
        assert result.score == 90  # the highest threshold that still flags anything
        assert result.flagged_items == 10
        assert result.expected_mistakes == math.ceil(10 * wilson_upper(3, 300))
        assert result.best_ratio == math.floor(1 / wilson_upper(3, 300))

    def test_a_zero_comeback_cohort_floors_an_extreme_ratio_instead_of_resolving_it(self) -> None:
        # The review's demonstration on the unfixed code: one measured cohort with zero
        # comebacks read as a plain 0.0, zeroed the scan's expected mistakes, and any
        # requested ratio "resolved" at the bottom of the score range with 0 expected
        # mistakes. The Wilson bound keeps every cohort's contribution above zero, so an
        # unreachable ratio floors honestly instead.
        rows = [_row(score=80, rewatch_odds=_measured(200, 0)) for _ in range(5)]
        result = resolve_ratio_curve(rows, ratio=99, coverage_floor_bp=0)

        assert result.state == "floored"
        assert result.score == 80
        assert result.expected_mistakes >= 1

    def test_no_candidates_is_not_enough_history(self) -> None:
        result = resolve_ratio_curve([], ratio=8, coverage_floor_bp=0)
        assert result.state == "not_enough_history"

    def test_no_measured_cohort_anywhere_is_not_enough_history(self) -> None:
        # Every row is either no-history or has no block at all: the fit never found a
        # trustworthy band anywhere in this scan, so there is nothing to resolve from.
        rows = [
            _row(score=80, rewatch_odds=_NO_HISTORY),
            _row(score=90, rewatch_odds=None),
        ]
        result = resolve_ratio_curve(rows, ratio=8, coverage_floor_bp=0)
        assert result.state == "not_enough_history"

    def test_a_missing_cohort_falls_back_to_the_worst_measured_rate_in_the_scan(self) -> None:
        # One row this server could never measure (no_history) sits beside two it could
        # (bounds ~0.249 and ~0.029). Its contribution must be the WORST of those two
        # (~0.249), never a bare zero -- the prime directive: missing data must not look
        # safer than the worst thing this scan actually measured.
        rows = [
            _row(score=80, rewatch_odds=_measured(300, 60)),
            _row(score=80, rewatch_odds=_measured(300, 3)),
            _row(score=80, rewatch_odds=_NO_HISTORY),
        ]
        result = resolve_ratio_curve(rows, ratio=2, coverage_floor_bp=0)
        assert result.state == "resolved"
        assert result.flagged_items == 3
        # mistakes = 2 * wilson(60,300) + wilson(3,300), the no-history row at the worst
        expected = 2 * wilson_upper(60, 300) + wilson_upper(3, 300)
        assert result.expected_mistakes == math.ceil(expected)

    def test_a_thin_cohort_contributes_its_wilson_upper_bound(self) -> None:
        bound = wilson_upper(1, 5)
        rows = [
            _row(score=80, rewatch_odds=_measured(300, 3)),  # anchors the fallback
            _row(score=80, rewatch_odds=_thin(5, 1)),
        ]
        result = resolve_ratio_curve(rows, ratio=2, coverage_floor_bp=0)
        assert result.state == "resolved"
        assert result.flagged_items == 2
        assert result.expected_mistakes == math.ceil(wilson_upper(3, 300) + bound)

    def test_a_protected_row_is_never_flagged_at_any_threshold(self) -> None:
        rows = [
            _row(
                score=100, protected=True, rewatch_odds=_measured(30, 30)
            ),  # would be all mistakes
            _row(score=50, rewatch_odds=_measured(30, 1)),
        ]
        # Even the loosest ratio must never count the protected row: if it did, mistakes
        # would include a 30/30 = 1.0 contribution and the ratio could never clear 2.
        result = resolve_ratio_curve(rows, ratio=2, coverage_floor_bp=0)
        assert result.state == "resolved"
        assert result.flagged_items == 1

    def test_a_blocked_row_is_never_flagged_at_any_threshold(self) -> None:
        rows = [
            _row(score=100, blocked=True, rewatch_odds=_measured(30, 30)),
            _row(score=50, rewatch_odds=_measured(30, 1)),
        ]
        result = resolve_ratio_curve(rows, ratio=2, coverage_floor_bp=0)
        assert result.state == "resolved"
        assert result.flagged_items == 1

    def test_coverage_below_the_floor_abstains_regardless_of_score(self) -> None:
        rows = [_row(score=100, coverage_bp=1000, rewatch_odds=_measured(30, 1))]
        result = resolve_ratio_curve(rows, ratio=2, coverage_floor_bp=5000)
        assert result.state == "not_enough_history"  # nothing ever flags: no population

    def test_same_inputs_resolve_identically_every_time(self) -> None:
        # No clock, no randomness: determinism is part of the contract.
        library = TestResolveRatioCurve._library()
        first = resolve_ratio_curve(library, ratio=8, coverage_floor_bp=0)
        second = resolve_ratio_curve(library, ratio=8, coverage_floor_bp=0)
        assert first == second


# --------------------------------------------------------------------------- the route


def _explanation(score: float, rewatch_odds: dict[str, object] | None = None) -> str:
    payload: dict[str, Any] = {
        "score": score,
        "threshold": 70,
        "coverage": 1.0,
        "signals": [],
        "protections_fired": [],
        "protections_checked": [],
        "protections_unknown": [],
    }
    if rewatch_odds is not None:
        payload["rewatch_odds"] = rewatch_odds
    return json.dumps(payload)


def _fixture_hash() -> str:
    return "0" * 64


@pytest.fixture
def resolve_client(tmp_path: Path) -> Iterator[TestClient]:
    """A snapshot whose 40 movie candidates give the resolver something real to chew on:
    20 scored 60 with a 60-of-300 cohort (bound ~0.249), 20 scored 90 with 3-of-300 (~0.029) --
    the same shape ``TestResolveRatioCurve._library`` proves by hand, but written through
    ``Candidate.verdict``/``explanation_json`` the way a real scan would freeze them, so this
    exercises ``_ratio_candidates``' decode path too."""
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    list_hash = seeded_fingerprint(settings)

    now: datetime = utcnow()
    with Session(engine) as session:
        snapshot = Snapshot(
            created_at=now,
            policy_hash=_fixture_hash(),
            scoring_hash=_fixture_hash(),
            list_config_hash=list_hash,
            horizon_at=now,
            item_count=40,
            degraded=False,
        )
        session.add(snapshot)
        session.flush()

        rows = []
        for i in range(20):
            rows.append(
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key=f"radarr:1:{i}",
                    title=f"Cheap {i}",
                    media_type="movie",
                    verdict="abstain",
                    score=60,
                    coverage_bp=10_000,
                    explanation_json=_explanation(60, _measured(300, 60)),
                    created_at=now,
                )
            )
        for i in range(20):
            rows.append(
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key=f"radarr:2:{i}",
                    title=f"Expensive {i}",
                    media_type="movie",
                    verdict="abstain",
                    score=90,
                    coverage_bp=10_000,
                    explanation_json=_explanation(90, _measured(300, 3)),
                    created_at=now,
                )
            )
        session.add_all(rows)
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestResolveRatioRoute:
    def test_no_snapshot_is_not_enough_history(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        engine.dispose()

        with TestClient(create_app(settings)) as c:
            login(c, settings)
            body = c.get("/api/policy/resolve-ratio?ratio=8").json()

        assert body == {"state": "not_enough_history"}

    def test_resolves_end_to_end_against_a_real_scan(self, resolve_client: TestClient) -> None:
        body = resolve_client.get("/api/policy/resolve-ratio?ratio=8").json()

        assert body["state"] == "resolved"
        assert body["score"] == 61
        assert body["flagged_items"] == 20  # only the "expensive" (scored 90) lane

    def test_unreachable_ratio_floors_end_to_end(self, resolve_client: TestClient) -> None:
        body = resolve_client.get("/api/policy/resolve-ratio?ratio=99").json()

        assert body["state"] == "floored"
        assert body["score"] == 90

    def test_media_type_tv_reads_no_rows_here(self, resolve_client: TestClient) -> None:
        # This fixture wrote only movie rows; a TV policy scores seasons, so the same scan
        # has no population on that lane at all.
        body = resolve_client.get("/api/policy/resolve-ratio?ratio=8&media_type=tv").json()
        assert body == {"state": "not_enough_history"}

    @pytest.mark.parametrize("ratio", [1, 100, 0, -5])
    def test_ratio_out_of_bounds_is_refused(self, resolve_client: TestClient, ratio: int) -> None:
        response = resolve_client.get(f"/api/policy/resolve-ratio?ratio={ratio}")
        assert response.status_code == 422

    def test_media_type_rejects_an_unknown_value(self, resolve_client: TestClient) -> None:
        response = resolve_client.get("/api/policy/resolve-ratio?ratio=8&media_type=book")
        assert response.status_code == 422

    def test_ratio_is_required(self, resolve_client: TestClient) -> None:
        response = resolve_client.get("/api/policy/resolve-ratio")
        assert response.status_code == 422


class TestAppliedRatioRoundTrips:
    """``PolicyBody.applied_ratio``: display-only metadata the save path carries, so a
    later load can re-resolve the same ratio and compare (the drift rule)."""

    def test_applied_ratio_round_trips_through_save_and_load(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        engine.dispose()

        with TestClient(create_app(settings)) as c:
            login(c, settings)
            saved = c.post(
                "/api/policy",
                json={
                    "condemn_at": 61,
                    "gates": [],
                    "signals": [
                        {"signal": "unwatched", "weight": 100, "saturate_at": 1825, "floor": 365}
                    ],
                    "applied_ratio": {"ratio": 8, "resolved_score": 61},
                },
            ).json()
            assert saved["body"]["applied_ratio"] == {"ratio": 8, "resolved_score": 61}

            loaded = c.get("/api/policy").json()
            assert loaded["body"]["applied_ratio"] == {"ratio": 8, "resolved_score": 61}

    def test_applied_ratio_is_optional(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        engine.dispose()

        with TestClient(create_app(settings)) as c:
            login(c, settings)
            saved = c.post(
                "/api/policy",
                json={
                    "condemn_at": 70,
                    "gates": [],
                    "signals": [
                        {"signal": "unwatched", "weight": 100, "saturate_at": 1825, "floor": 365}
                    ],
                },
            ).json()
            assert saved["body"]["applied_ratio"] is None
