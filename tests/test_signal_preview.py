# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checks a signal's ramp at one value, answered by the engine instead of the browser.

The editor draws a probe line under each signal while the operator drags its slider, and
this route answers that probe. Reimplementing the same ramp in TypeScript would risk a
second scorer that disagrees with the first, so the route asks the real engine instead. What
this test pins is not that the route responds. It is that the number it returns is the
number a scan would produce for the same value.
"""

from __future__ import annotations

import pytest

from reaper.engine.preview import READS, UnprobableSignalError, probe_signal
from reaper.engine.signals import SignalConfig, SignalId, evaluate_signal

from .test_signal_state import _facts

LOW_RATING = SignalConfig(signal=SignalId.LOW_RATING, weight=10, saturate_at=60)


class TestTheProbeAgreesWithTheScorer:
    """Compares the probe's answer to ``evaluate_signal``'s answer for the same value.

    The expected value comes from calling ``evaluate_signal`` itself, not from a copy of its
    arithmetic. A change to the scoring math moves both sides together, so this test can
    only fail when the probe and the scorer genuinely disagree.
    """

    @pytest.mark.parametrize("tenths", [0, 30, 55, 59, 60, 64, 100])
    def test_a_rating_scores_the_same_either_way(self, tenths: int) -> None:
        from reaper.engine.observation import Known

        scanned = evaluate_signal(
            LOW_RATING, _facts(imdb_rating_tenths=Known(value=tenths, source="imdb"))
        )
        probed = probe_signal(LOW_RATING, tenths)

        assert probed.points == pytest.approx(scanned.pressure)

    def test_the_shipped_rating_ramp_pays_what_it_is_documented_to_pay(self) -> None:
        # These literal numbers are what the operator is shown. An agreement test alone
        # would stay green even if both sides drifted the same way, so this pins the values.
        assert probe_signal(LOW_RATING, 60).points == 0
        assert probe_signal(LOW_RATING, 55).points == pytest.approx(0.833, abs=1e-3)
        assert probe_signal(LOW_RATING, 0).points == 10

    def test_a_dormancy_ramp_pays_half_way_across(self) -> None:
        unwatched = SignalConfig(signal=SignalId.UNWATCHED, weight=70, saturate_at=1825, floor=365)

        assert probe_signal(unwatched, 365).points == 0
        assert probe_signal(unwatched, 1095).points == pytest.approx(35)
        assert probe_signal(unwatched, 1825).points == 70


class TestWhatTheProbeDeliberatelyIgnores:
    def test_a_watcher_count_is_answered_rather_than_withheld(self) -> None:
        """A probe answers this signal even when a real scan would withhold it.

        During a real scan, this signal is withheld when the watch history does not span
        the required window. A probe always answers instead: a withheld probe would return
        zero at every value, which would teach the operator nothing about the shape they are
        setting. The real shortfall is reported separately, as its own warning next to the
        watch history.
        """
        few = SignalConfig(signal=SignalId.FEW_WATCHERS, weight=20, saturate_at=3)

        assert probe_signal(few, 3).points == 0
        assert probe_signal(few, 0).points == 20

    def test_no_other_fact_can_move_the_answer(self) -> None:
        # Every fact except the probed value is Unknown, so the probe cannot inherit a
        # number from somewhere else and report it as this signal's own.
        rating = probe_signal(LOW_RATING, 30)
        unwatched = SignalConfig(signal=SignalId.UNWATCHED, weight=70, saturate_at=1825, floor=365)

        assert rating.points == 5
        # Probing the same value against a different signal confirms the two do not interfere.
        assert probe_signal(unwatched, 30).points == 0


class TestEverySignalCanBeProbed:
    def test_the_fact_map_covers_the_whole_signal_set(self) -> None:
        """Every ``SignalId`` has an entry in ``READS``.

        A signal missing from ``READS`` has no probe, so its editor row would offer a
        control that always refuses. This guard fails as soon as a new signal is added
        without a matching entry.
        """
        assert set(READS) == set(SignalId), (
            "every SignalId needs a fact to probe against; add it to engine.preview.READS "
            "and give the new signal a row in frontend/src/components/signalRamp.ts"
        )

    def test_an_unmapped_signal_refuses_rather_than_guessing(self) -> None:
        # Reached only if the guard above is ever bypassed. Guessing a fact would answer
        # confidently about the wrong evidence, so this raises instead, and the route turns
        # that into a refusal.
        stray = SignalConfig(signal="not_a_signal", weight=10, saturate_at=60)  # type: ignore[arg-type]

        with pytest.raises(UnprobableSignalError):
            probe_signal(stray, 30)
