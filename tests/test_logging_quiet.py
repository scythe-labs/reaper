# SPDX-License-Identifier: AGPL-3.0-or-later
"""Noisy library loggers stay quiet even when the operator turns Reaper up to DEBUG.

The regression this guards: aiosqlite logs one line per SQL cursor operation, so at DEBUG a
single scan's thousands of candidate inserts flooded the ring and the on-disk file and
evicted every diagnostic that matters (the per-item scan decisions, the Plex-match lines) --
a downloaded DEBUG log came back 99.98% aiosqlite with zero decision lines. Pinning these
loggers to WARNING with an EXPLICIT level keeps them quiet through a runtime switch to DEBUG,
because logbuffer.set_level only moves the root level.
"""

from __future__ import annotations

import logging

from reaper import logbuffer
from reaper.logging import _NOISY_LOGGERS, configure_logging

# ``_restore_logging`` is shared from conftest: these tests call configure_logging
# in their own body, and every one of its effects is process-global.


def test_aiosqlite_and_sql_stay_at_warning(_restore_logging: None) -> None:
    assert "aiosqlite" in _NOISY_LOGGERS  # the logger the fix was about
    # httpx2 renames its loggers; both spellings must stay quiet through the migration, or
    # a Discord webhook token (in the URL path) leaks into the log in cleartext.
    assert "httpx2" in _NOISY_LOGGERS and "httpcore2" in _NOISY_LOGGERS
    configure_logging(level="DEBUG")
    # Even booted at DEBUG, the SQL firehose is capped, so it cannot drown the diagnostics.
    for name in ("aiosqlite", "sqlalchemy", "httpx", "httpx2"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING, name


def test_it_survives_a_runtime_switch_to_debug(_restore_logging: None) -> None:
    """The Settings -> Logs toggle moves the root level; the explicit WARNING must hold."""
    configure_logging(level="INFO")
    logbuffer.set_level("DEBUG")
    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG  # Reaper is at DEBUG
    assert logging.getLogger("aiosqlite").getEffectiveLevel() == logging.WARNING  # SQL is not
