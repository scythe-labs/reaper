# SPDX-License-Identifier: AGPL-3.0-or-later
"""The time helpers -- in particular ``humanize_days``, which is what turns a raw dormancy
count into the phrase the why-panel and the review cards show. It is display-only (it never
feeds a number back into a decision), so the contract that matters is simply that it reads
like a person wrote it."""

from __future__ import annotations

import pytest

from reaper.clock import humanize_days


class TestHumanizeDays:
    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (0, "today"),
            (0.4, "today"),
            (1, "1 day"),
            (5, "5 days"),
            (30, "1 month"),
            (90, "3 months"),
            (365, "1 year"),
            (400, "1 year, 1 month"),
            (730, "2 years"),
            (1095, "3 years"),  # the default dormancy floor
            (2060, "5 years, 7 months"),  # the "unwatched for 2060 days" from the screenshot
        ],
    )
    def test_reads_in_human_units(self, days: float, expected: str) -> None:
        assert humanize_days(days) == expected

    def test_keeps_to_two_significant_units(self) -> None:
        # 5 years, 7 months, 25 days -> the days are dropped; two units is enough to read.
        assert humanize_days(2060).count(",") == 1
