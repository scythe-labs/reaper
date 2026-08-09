# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trying a signal's range against a value, answered by the engine rather than the browser.

The editor draws a probe under each signal and asks this while the operator drags it. The
whole reason it is a round trip is that the alternative -- the same ramp reimplemented in
TypeScript, beside the control that tunes deletions -- is a second scorer free to disagree
with the first. So the thing worth pinning here is not that the route responds: it is that
the number it responds with is the number a scan would produce.
"""

from __future__ import annotations

import pytest

from reaper.engine.preview import READS, UnprobableSignalError, probe_signal
from reaper.engine.signals import SignalConfig, SignalId, evaluate_signal

from .test_signal_state import _facts

LOW_RATING = SignalConfig(signal=SignalId.LOW_RATING, weight=10, saturate_at=60)


class TestTheProbeAgreesWithTheScorer:
    """Not a transcription of the ramp: the expectation is what ``evaluate_signal`` returns
    for the same value, so a change to the arithmetic moves both sides together and this
    test can only fail when the two paths genuinely disagree (rule 119)."""

    @pytest.mark.parametrize("tenths", [0, 30, 55, 59, 60, 64, 100])
    def test_a_rating_scores_the_same_either_way(self, tenths: int) -> None:
        from reaper.engine.observation import Known

        scanned = evaluate_signal(
            LOW_RATING, _facts(imdb_rating_tenths=Known(value=tenths, source="imdb"))
        )
        probed = probe_signal(LOW_RATING, tenths)

        assert probed.points == pytest.approx(scanned.pressure)

    def test_the_shipped_rating_ramp_pays_what_it_is_documented_to_pay(self) -> None:
        # The one place a literal belongs: these are the numbers the operator is shown, and
        # an agreement test alone would keep agreeing if BOTH sides moved.
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
        """The mirror's reach withholds this signal during a real scan when the history does
        not span the window (rule 140). A probe satisfies that check instead: it describes
        the RULE, and a shortfall would report zero at every value, which teaches nothing
        about the shape the operator is setting. The shortfall has its own policy warning,
        beside the history it is actually about."""
        few = SignalConfig(signal=SignalId.FEW_WATCHERS, weight=20, saturate_at=3)

        assert probe_signal(few, 3).points == 0
        assert probe_signal(few, 0).points == 20

    def test_no_other_fact_can_move_the_answer(self) -> None:
        # Everything but the probed value is Unknown, so a probe cannot quietly inherit a
        # number from somewhere else and report it as this rule's doing.
        rating = probe_signal(LOW_RATING, 30)
        unwatched = SignalConfig(signal=SignalId.UNWATCHED, weight=70, saturate_at=1825, floor=365)

        assert rating.points == 5
        # Same probed number, a different signal, and no bleed between them.
        assert probe_signal(unwatched, 30).points == 0


class TestEverySignalCanBeProbed:
    def test_the_fact_map_covers_the_whole_signal_set(self) -> None:
        """A signal missing from ``READS`` has no probe, and its editor row would offer a
        control that answers with a refusal. The map mirrors an enum, so it needs the guard
        that fails when the enum grows (rule 103)."""
        assert set(READS) == set(SignalId), (
            "every SignalId needs a fact to probe against; add it to engine.preview.READS "
            "and give the new signal a row in frontend/src/components/signalRamp.ts"
        )

    def test_an_unmapped_signal_refuses_rather_than_guessing(self) -> None:
        # Reached only if the guard above is ever bypassed. Guessing a fact would answer
        # confidently about the wrong evidence, so it raises for the route to turn into a
        # refusal (rule 118: the tripwire gets its own test where the guard above hides it).
        stray = SignalConfig(signal="not_a_signal", weight=10, saturate_at=60)  # type: ignore[arg-type]

        with pytest.raises(UnprobableSignalError):
            probe_signal(stray, 30)
