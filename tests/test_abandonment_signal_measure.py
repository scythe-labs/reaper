# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pure logic in ``scripts/abandonment_signal_measure.py``: the abandoned/control
split, the dormancy-age band boundaries, and the pooled-lift math the stop gate reads.
These tests call the real function directly, never a transcribed copy."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# scripts/ is not on mypy's checked path. test_delete_threshold_ratio_measure.py sets the
# same precedent, since `mypy src/reaper tests/` does not include scripts/ either.
import abandonment_signal_measure as measure  # type: ignore[import-not-found]


def test_classify_title_splits_abandoned_from_completed_only() -> None:
    assert (
        measure.classify_title(has_abandoned=True, has_qualified=False, has_play_before=True)
        == measure.ABANDONED
    )
    assert (
        measure.classify_title(has_abandoned=False, has_qualified=True, has_play_before=True)
        == measure.CONTROL_COMPLETED
    )


def test_classify_title_excludes_a_mixed_history() -> None:
    # Both an abandoned and a completed play happened before the cutoff. That title counts
    # as neither an honest abandoned-only title nor an honest completed-only control.
    assert (
        measure.classify_title(has_abandoned=True, has_qualified=True, has_play_before=True) is None
    )


def test_classify_title_reads_no_play_before_as_never_played() -> None:
    assert (
        measure.classify_title(has_abandoned=False, has_qualified=False, has_play_before=False)
        == measure.CONTROL_NEVER_PLAYED
    )


def test_band_for_matches_the_fixed_edges_half_open_closed_at_zero() -> None:
    assert measure.band_for(0.0) == (0.0, 365.0)
    assert measure.band_for(365.0) == (0.0, 365.0)
    assert measure.band_for(365.0001) == (365.0, 548.0)
    assert measure.band_for(548.0) == (365.0, 548.0)
    assert measure.band_for(1825.0) == (1095.0, 1825.0)
    assert measure.band_for(5000.0) == (1825.0, None)


def test_band_for_is_deterministic_and_covers_every_band_boundary() -> None:
    days = [0.0, 1.0, 365.0, 366.0, 548.0, 730.0, 1095.0, 1825.0, 1826.0, 9000.0]
    assert [measure.band_for(d) for d in days] == [measure.band_for(d) for d in days]
    # Every returned band's own edges must actually contain the day count it was asked for.
    for d in days:
        lo, hi = measure.band_for(d)
        assert d > lo or (lo == 0 and d == 0)
        assert hi is None or d <= hi


def _one_band_table(*, a_n: int, a_k: int, c_n: int, c_k: int) -> measure.BandTable:
    band = (0.0, 365.0)
    return {
        band: {
            measure.ABANDONED: measure.Cohort(n=a_n, k=a_k),
            measure.CONTROL_COMPLETED: measure.Cohort(n=c_n, k=c_k),
        }
    }


def test_pooled_lift_excludes_a_band_thinner_than_the_floor_on_either_side() -> None:
    thin = _one_band_table(a_n=10, a_k=5, c_n=100, c_k=10)
    lift, pooled_a, pooled_c, included = measure.pooled_lift(
        thin, measure.CONTROL_COMPLETED, floor=30
    )
    assert included == []
    assert lift is None
    assert pooled_a.n == 0
    assert pooled_c.n == 0


def test_pooled_lift_pools_two_bands_that_each_clear_the_floor() -> None:
    bands: measure.BandTable = {
        (0.0, 365.0): {
            measure.ABANDONED: measure.Cohort(n=40, k=20),  # rate 0.5
            measure.CONTROL_COMPLETED: measure.Cohort(n=40, k=8),  # rate 0.2
        },
        (365.0, 548.0): {
            measure.ABANDONED: measure.Cohort(n=60, k=30),  # rate 0.5
            measure.CONTROL_COMPLETED: measure.Cohort(n=60, k=12),  # rate 0.2
        },
    }
    lift, pooled_a, pooled_c, included = measure.pooled_lift(
        bands, measure.CONTROL_COMPLETED, floor=30
    )
    assert len(included) == 2
    assert pooled_a == measure.Cohort(n=100, k=50)
    assert pooled_c == measure.Cohort(n=100, k=20)
    assert lift is not None
    assert abs(lift - 0.3) < 1e-9


def test_verdict_line_reads_the_005_bar_in_both_directions() -> None:
    assert "signal justified" in measure.verdict_line(0.05)
    assert "signal not justified" in measure.verdict_line(0.0499)
    assert "signal justified" in measure.verdict_line(-0.05)
    assert "argues delete" in measure.verdict_line(-0.2)
    assert "argues keep" in measure.verdict_line(0.2)
    assert "no band had enough" in measure.verdict_line(None)
