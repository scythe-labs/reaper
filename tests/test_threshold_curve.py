# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests ``GET /api/policy/threshold-curve``, the score-to-consequence curve behind the
delete-threshold slider. It comes from the newest scan's own fitted rewatch curve
(docs/LEARNINGS.md, "The delete threshold buys volume, not precision").

The core is ``api.policy.threshold_curve_rows``, a pure function over already-decoded rows
(``RatioCandidate``), covered directly here without a database. The route wiring, reading
the newest snapshot, the active policy's coverage floor, and the wire shapes, gets its own,
smaller set of end-to-end cases below.

The frontend reads this curve once and re-decides locally for every slider position.
``_cohort``, ``_measured_or_thin_rate``, and ``_mistake_probability`` are unchanged and are
not tested again here; their own tests still cover them.
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

from reaper.api.policy import RatioCandidate, threshold_curve_rows
from reaper.api.schemas import (
    ThresholdCurveCountsOnlyOut,
    ThresholdCurveCountsOnlyRowOut,
    ThresholdCurveMeasuredOut,
    ThresholdCurveMeasuredRowOut,
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


def _rows_by_score(
    curve: ThresholdCurveMeasuredOut | ThresholdCurveCountsOnlyOut,
) -> dict[int, ThresholdCurveMeasuredRowOut | ThresholdCurveCountsOnlyRowOut]:
    """A measured/counts_only curve's rows, keyed by score, for asserting on one point at a
    time without caring about the others."""
    return {row.score: row for row in curve.rows}


def _measured_rows_by_score(curve: ThresholdCurveMeasuredOut) -> dict[int, int]:
    """A measured curve's ``expected_mistakes``, keyed by score. This is a separate helper
    rather than widening ``_rows_by_score``'s return type, since ``expected_mistakes`` only
    exists on the measured row. The counts_only arm proves it absent.
    """
    return {row.score: row.expected_mistakes for row in curve.rows}


# --------------------------------------------------------------------------- the core


class TestThresholdCurveRows:
    """The curve's contract, over hand-built rows with cohorts large enough that the Wilson
    bound sits near the plain rate. Ten "cheap" titles score 60 with a 60-of-300 cohort
    (bound about 0.249). Ten "expensive" (safer) titles score 90 with a 3-of-300 cohort
    (bound about 0.029). Below score 61, all twenty are flagged. At or above 61, only the
    ten scored-90 titles are.
    """

    @staticmethod
    def _library() -> list[RatioCandidate]:
        cheap = [_row(score=60, rewatch_odds=_measured(300, 60)) for _ in range(10)]
        expensive = [_row(score=90, rewatch_odds=_measured(300, 3)) for _ in range(10)]
        return cheap + expensive

    def test_no_candidates_is_counts_only_with_no_rows(self) -> None:
        result = threshold_curve_rows([], coverage_floor_bp=0)
        assert result.state == "counts_only"
        assert result.rows == []

    def test_no_measured_cohort_anywhere_is_counts_only(self) -> None:
        # Every row is either no-history or has no block at all. The fit never found a
        # trustworthy band anywhere in this scan, so the count stands without a comeback
        # estimate, rather than making one up.
        rows = [
            _row(score=80, rewatch_odds=_NO_HISTORY),
            _row(score=90, rewatch_odds=None),
        ]
        result = threshold_curve_rows(rows, coverage_floor_bp=0)
        assert result.state == "counts_only"
        by_score = _rows_by_score(result)
        assert by_score[80].flagged == 2  # both rows flag at or below 80
        assert by_score[90].flagged == 1
        assert not hasattr(by_score[80], "expected_mistakes")

    def test_measured_curve_has_one_row_per_flagged_score_only(self) -> None:
        result = threshold_curve_rows(self._library(), coverage_floor_bp=0)
        assert result.state == "measured"
        by_score = _rows_by_score(result)
        # Below 61, all 20 are flagged. From 61 through 90, only the 10 "expensive" ones
        # are. Above 90, nothing is flagged, so there is no row past the last one on the list.
        assert by_score[1].flagged == 20
        assert by_score[60].flagged == 20
        assert by_score[61].flagged == 10
        assert by_score[90].flagged == 10
        assert 91 not in by_score
        assert max(by_score) == 90

    def test_expected_mistakes_matches_the_wilson_bound_sum(self) -> None:
        result = threshold_curve_rows(self._library(), coverage_floor_bp=0)
        assert result.state == "measured"
        mistakes = _measured_rows_by_score(result)
        # At 61-90, only the 10 "expensive" (3-of-300) titles flag.
        assert mistakes[61] == math.ceil(10 * wilson_upper(3, 300))
        # Below 61, all 20 flag. That is 10 cheap (60-of-300) plus 10 expensive (3-of-300).
        assert mistakes[1] == math.ceil(10 * wilson_upper(60, 300) + 10 * wilson_upper(3, 300))

    def test_expected_mistakes_is_never_zero_while_flagged_is_positive(self) -> None:
        # A measured cohort with zero comebacks, read as a plain rate of 0.0, would zero
        # the expected mistakes outright. The Wilson bound keeps every cohort's
        # contribution above zero, so every row here pins a value greater than 0.
        rows = [_row(score=80, rewatch_odds=_measured(200, 0)) for _ in range(5)]
        result = threshold_curve_rows(rows, coverage_floor_bp=0)
        assert result.state == "measured"
        assert result.rows
        for row in result.rows:
            assert row.flagged > 0
            assert row.expected_mistakes >= 1

    def test_a_missing_cohort_falls_back_to_the_worst_measured_rate_in_the_scan(self) -> None:
        # One row this server could never measure (no_history) sits beside two it could
        # (bounds about 0.249 and 0.029). Its contribution must be the worse of those two
        # (about 0.249), never a bare zero. Missing data must never look safer than the
        # worst thing this scan actually measured.
        rows = [
            _row(score=80, rewatch_odds=_measured(300, 60)),
            _row(score=80, rewatch_odds=_measured(300, 3)),
            _row(score=80, rewatch_odds=_NO_HISTORY),
        ]
        result = threshold_curve_rows(rows, coverage_floor_bp=0)
        assert result.state == "measured"
        by_score = _rows_by_score(result)
        assert by_score[80].flagged == 3
        expected = 2 * wilson_upper(60, 300) + wilson_upper(3, 300)
        assert _measured_rows_by_score(result)[80] == math.ceil(expected)

    def test_a_thin_cohort_contributes_its_wilson_upper_bound(self) -> None:
        bound = wilson_upper(1, 5)
        rows = [
            _row(score=80, rewatch_odds=_measured(300, 3)),  # anchors the fallback
            _row(score=80, rewatch_odds=_thin(5, 1)),
        ]
        result = threshold_curve_rows(rows, coverage_floor_bp=0)
        assert result.state == "measured"
        by_score = _rows_by_score(result)
        assert by_score[80].flagged == 2
        assert _measured_rows_by_score(result)[80] == math.ceil(wilson_upper(3, 300) + bound)

    def test_a_protected_row_is_never_flagged_at_any_threshold(self) -> None:
        rows = [
            _row(
                score=100, protected=True, rewatch_odds=_measured(30, 30)
            ),  # would be all mistakes
            _row(score=50, rewatch_odds=_measured(30, 1)),
        ]
        result = threshold_curve_rows(rows, coverage_floor_bp=0)
        by_score = _rows_by_score(result)
        # The protected row never flags, so the top of the domain has no row at all.
        assert 100 not in by_score
        assert by_score[50].flagged == 1

    def test_a_blocked_row_is_never_flagged_at_any_threshold(self) -> None:
        rows = [
            _row(score=100, blocked=True, rewatch_odds=_measured(30, 30)),
            _row(score=50, rewatch_odds=_measured(30, 1)),
        ]
        result = threshold_curve_rows(rows, coverage_floor_bp=0)
        by_score = _rows_by_score(result)
        assert 100 not in by_score
        assert by_score[50].flagged == 1

    def test_coverage_below_the_floor_abstains_regardless_of_score(self) -> None:
        rows = [_row(score=100, coverage_bp=1000, rewatch_odds=_measured(30, 1))]
        result = threshold_curve_rows(rows, coverage_floor_bp=5000)
        # Nothing ever flags, so there is no population. This is the same shape as having
        # no candidates at all, since neither case has anything to put in front of the
        # operator.
        assert result.state == "measured"
        assert result.rows == []

    def test_same_inputs_produce_the_same_curve_every_time(self) -> None:
        # No clock and no randomness. Determinism is part of the contract.
        library = TestThresholdCurveRows._library()
        first = threshold_curve_rows(library, coverage_floor_bp=0)
        second = threshold_curve_rows(library, coverage_floor_bp=0)
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
def curve_client(tmp_path: Path) -> Iterator[TestClient]:
    """A snapshot with 40 movie candidates for the curve builder to read. 20 are scored 60
    with a 60-of-300 cohort (bound about 0.249), and 20 are scored 90 with a 3-of-300 cohort
    (bound about 0.029). This is the same shape ``TestThresholdCurveRows._library`` builds by
    hand, but written through ``Candidate.verdict`` and ``explanation_json`` the way a real
    scan would freeze them, so this also exercises ``_ratio_candidates``' decode path.
    """
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


class TestThresholdCurveRoute:
    def test_no_snapshot_is_no_scan(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        engine.dispose()

        with TestClient(create_app(settings)) as c:
            login(c, settings)
            body = c.get("/api/policy/threshold-curve").json()

        assert body == {"state": "no_scan"}

    def test_reads_the_whole_curve_end_to_end_against_a_real_scan(
        self, curve_client: TestClient
    ) -> None:
        body = curve_client.get("/api/policy/threshold-curve").json()

        assert body["state"] == "measured"
        by_score = {row["score"]: row for row in body["rows"]}
        assert by_score[1]["flagged"] == 40
        assert by_score[61]["flagged"] == 20  # only the "expensive" (scored 90) lane
        assert by_score[90]["flagged"] == 20
        assert 91 not in by_score

    def test_media_type_tv_reads_no_rows_here(self, curve_client: TestClient) -> None:
        # This fixture wrote only movie rows. A TV policy scores seasons, so the same scan
        # has no population on that lane at all.
        body = curve_client.get("/api/policy/threshold-curve?media_type=tv").json()
        assert body == {"state": "counts_only", "rows": []}

    def test_media_type_rejects_an_unknown_value(self, curve_client: TestClient) -> None:
        response = curve_client.get("/api/policy/threshold-curve?media_type=book")
        assert response.status_code == 422

    def test_media_type_defaults_to_movie(self, curve_client: TestClient) -> None:
        default = curve_client.get("/api/policy/threshold-curve").json()
        explicit = curve_client.get("/api/policy/threshold-curve?media_type=movie").json()
        assert default == explicit
