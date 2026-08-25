# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pure helpers in ``scripts/delete_threshold_ratio_measure.py``: the ramp/gate
boundaries, the curve math, the ratio wording, and the cutoff/lane split that must never
leak a play after the cutoff into a "before" reading (rule 119: agreement tests call the
real function, never a transcribed copy -- every assertion here calls the script's own
code, including the real ``reaper.services.rewatch.training_pair`` it is built on)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# scripts/ is not on mypy's checked path (return_signal_measure.py precedent: scripts/
# is not part of the `mypy src/reaper tests/` gate).
import delete_threshold_ratio_measure as measure  # type: ignore[import-not-found]

DAY = 86_400


def test_unwatched_score_ramps_between_the_shipped_floor_and_saturate() -> None:
    assert measure.unwatched_score(0) == 0.0
    assert measure.unwatched_score(365) == 0.0
    assert measure.unwatched_score(1825) == 100.0
    assert measure.unwatched_score(5000) == 100.0
    midpoint = measure.unwatched_score(365 + (1825 - 365) / 2)
    assert 49.0 < midpoint < 51.0
    # Monotone: more dormancy never lowers the score.
    days = [0, 100, 365, 900, 1095, 1500, 1825, 3000]
    scores = [measure.unwatched_score(d) for d in days]
    assert scores == sorted(scores)


def test_min_dormancy_gate_holds_below_its_threshold_and_releases_at_it() -> None:
    assert measure.min_dormancy_protects(0) is True
    assert measure.min_dormancy_protects(measure.MIN_DORMANCY_GATE_DAYS - 1) is True
    assert measure.min_dormancy_protects(measure.MIN_DORMANCY_GATE_DAYS) is False
    assert measure.min_dormancy_protects(5000) is False


def test_build_curve_counts_flagged_and_mistakes_by_hand() -> None:
    # Four titles: two score under every threshold (young), one scores high and was a
    # mistake, one scores high and was not.
    pairs = [
        (400.0, False),  # low score, never flagged at 60+
        (1825.0, False),  # score 100, flagged, correct
        (1825.0, True),  # score 100, flagged, a mistake
        (1400.0, False),  # score ~71, flagged only at low thresholds
    ]
    rows = {
        threshold: (flagged, mistakes)
        for threshold, flagged, mistakes in measure.build_curve(pairs)
    }
    assert rows[95] == (2, 1)  # only the two saturated titles clear 95
    assert rows[60] == (3, 1)  # the 1400-day title clears 60 too


def test_build_curve_is_deterministic_across_runs() -> None:
    pairs = [(float(d), d % 3 == 0) for d in range(200, 2000, 17)]
    assert measure.build_curve(pairs) == measure.build_curve(pairs)


def test_ratio_text_never_reads_zero_mistakes_as_a_bare_infinity() -> None:
    assert measure.ratio_text(0, 0) == "no titles flagged"
    zero = measure.ratio_text(40, 0)
    assert "Wilson" in zero and "0 mistakes" in zero
    nonzero = measure.ratio_text(40, 2)
    assert "1 mistake per 20 cleared" in nonzero


def test_title_pair_never_leaks_a_play_after_the_outcome_window() -> None:
    # A play two years after the cutoff must not count as "watched again within a year".
    cutoff = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
    window_end = cutoff + 365 * DAY
    far_future = cutoff + 2 * 365 * DAY
    plays = {"t1": [far_future]}
    pair, in_lane_a = measure.title_pair(
        "t1",
        plays,
        added_at_epoch=cutoff - 10 * DAY,
        cutoff_epoch=cutoff,
        window_end_epoch=window_end,
    )
    assert in_lane_a is False
    assert pair is not None
    days, watched_again = pair
    assert watched_again is False
    assert days == 10.0


def test_title_pair_splits_lanes_on_a_play_before_the_cutoff() -> None:
    cutoff = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
    window_end = cutoff + 365 * DAY
    plays = {"played": [cutoff - 400 * DAY], "never": []}

    pair, in_lane_a = measure.title_pair(
        "played",
        plays,
        added_at_epoch=cutoff - 900 * DAY,
        cutoff_epoch=cutoff,
        window_end_epoch=window_end,
    )
    assert in_lane_a is True
    assert pair is not None
    assert pair[0] == 400.0

    pair, in_lane_a = measure.title_pair(
        "never",
        plays,
        added_at_epoch=cutoff - 500 * DAY,
        cutoff_epoch=cutoff,
        window_end_epoch=window_end,
    )
    assert in_lane_a is False
    assert pair is not None
    assert pair[0] == 500.0


def test_title_pair_withholds_a_title_added_inside_the_lookback_year() -> None:
    # No play either side, and it arrived after the cutoff: it was not in the library
    # yet, so it must not be scored as if it had sat dormant since the cutoff.
    cutoff = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
    window_end = cutoff + 365 * DAY
    pair, in_lane_a = measure.title_pair(
        "new",
        {},
        added_at_epoch=cutoff + 10 * DAY,
        cutoff_epoch=cutoff,
        window_end_epoch=window_end,
    )
    assert pair is None
    assert in_lane_a is False
