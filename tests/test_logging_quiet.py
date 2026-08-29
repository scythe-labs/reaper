# SPDX-License-Identifier: AGPL-3.0-or-later
"""Noisy library loggers stay quiet even when the operator turns Reaper up to DEBUG.

aiosqlite logs one line per SQL cursor operation. At DEBUG, a single scan's candidate inserts
can flood the log and push out the diagnostics that matter, like the per-item scan decisions
and the Plex-match lines. Pinning these loggers to WARNING with an explicit level keeps them
quiet through a runtime switch to DEBUG, because logbuffer.set_level only moves the root
level.
"""

from __future__ import annotations

import logging

from reaper import logbuffer
from reaper.logging import _NOISY_LOGGERS, configure_logging

# ``_restore_logging`` is shared from conftest. These tests call configure_logging in their
# own body, and every one of its effects is process-global.


def test_aiosqlite_and_sql_stay_at_warning(_restore_logging: None) -> None:
    assert "aiosqlite" in _NOISY_LOGGERS  # the logger DEBUG floods with SQL lines
    # httpx2 renames its loggers. Both spellings must stay quiet during the migration, or a
    # Discord webhook token in the URL path leaks into the log in cleartext.
    assert "httpx2" in _NOISY_LOGGERS and "httpcore2" in _NOISY_LOGGERS
    configure_logging(level="DEBUG")
    # Even at DEBUG, SQL logging stays capped so other diagnostics remain visible.
    for name in ("aiosqlite", "sqlalchemy", "httpx", "httpx2"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING, name


def test_it_survives_a_runtime_switch_to_debug(_restore_logging: None) -> None:
    """The Settings -> Logs toggle moves the root level.

    The explicit WARNING level must hold anyway.
    """
    configure_logging(level="INFO")
    logbuffer.set_level("DEBUG")
    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG  # Reaper is at DEBUG
    assert logging.getLogger("aiosqlite").getEffectiveLevel() == logging.WARNING  # SQL is not
