# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every module logger stays interceptable by ``capture_logs``, whatever ran before.

The suite asserts on log events in a few dozen places, and structlog will quietly stop
delivering them. ``configure_logging`` sets ``cache_logger_on_first_use=True``, so the
first use of a module logger after it freezes that proxy against the processor LIST
OBJECT live at that moment. ``capture_logs`` mutates the configured list in place --
deliberately, to keep bound-logger references working -- so the frozen logger keeps up
until some later ``configure_logging`` installs a *fresh* list. From then on the frozen
logger renders through the old one and no ``capture_logs`` can reach it. Two app boots is
all it takes, and the suite builds many apps.

What that cost: a test asserting the executor names its own failed call read an empty
event list and failed, on a config-only change touching neither logging nor the executor
(#481). Whether it failed depended on which xdist worker it landed on, since only a worker
that had already booted two apps carried the damage. A test that fails for a reason
unrelated to what it tests is worse than no test: it spends the reviewer's attention and
teaches them to re-run rather than read.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import structlog
from structlog._config import BoundLoggerLazyProxy
from structlog.testing import capture_logs

from reaper.logging import configure_logging
from reaper.services import executor
from tests.conftest import uncache_module_loggers

#: How every logger in ``src/`` is declared. The walk in ``uncache_module_loggers`` reaches
#: a proxy held in a module's globals, so a logger built any other way would drop out of
#: the guard silently; this is the spelling that keeps that true.
_DECLARATION = re.compile(r"^log = structlog\.get_logger\(__name__\)$", re.MULTILINE)

_SRC = Path(__file__).resolve().parents[1] / "src" / "reaper"


def _freeze_the_executor_logger() -> None:
    """Cache the proxy against one processor list, then leave a newer one configured.

    Both halves through the real ``configure_logging``, which is what a ``create_app``
    boot calls: the first sets the caching flag and a fresh list, the log freezes the
    proxy against it, and the second boot swaps the list the proxy no longer holds.
    """
    configure_logging()
    executor.log.info("materializing this logger freezes it")
    configure_logging()
    structlog.configure(cache_logger_on_first_use=False)


class TestALoggerFrozenByAnEarlierTestIsStillCapturable:
    def test_the_guard_thaws_it(self) -> None:
        _freeze_the_executor_logger()

        uncache_module_loggers()

        with capture_logs() as logs:
            executor.log.warning("reap.journal_revive_failed")
        assert [entry["event"] for entry in logs] == ["reap.journal_revive_failed"]

    def test_without_the_guard_it_is_deaf(self) -> None:
        """The other half, so the test above cannot pass by the freezing never happening
        (rule 118). This is #481 reproduced in one process: the event is emitted, and
        ``capture_logs`` sees nothing at all."""
        _freeze_the_executor_logger()

        with capture_logs() as logs:
            executor.log.warning("reap.journal_revive_failed")
        assert logs == []

        # Left frozen, this leaks into whatever runs next on this worker, which IS the
        # bug. The autouse fixture thaws it before the next test; so does this, so that
        # this test cannot be the thing it is complaining about.
        uncache_module_loggers()


class TestTheGuardReachesEveryLoggerInTheTree:
    """A guard that SCANS is proven against the population it claims to cover, not against
    one member of it (rule 145). The count is reconciled by hand: 50 files under
    ``src/reaper`` declare a module-level logger, and ``grep -rl`` over the tree agrees.

    It was 47 before ``services/list_config.py`` (the operator's own protection lists) landed
    with a logger and this number did not move with it, which is what the guard is for, then 49
    with ``services/list_rules.py`` (the keep rules a list acts through), 47 again once the two
    retired lab engines left and took theirs, and 48 with ``api/plex.py``. Then 50, when the old
    single API routes module became five: three of the five log, so one logger left and three
    arrived. ``api/vocabulary.py`` and ``api/about.py`` declare none, because neither logs --
    a split inherits its parent's logger only where it inherited something to say."""

    @staticmethod
    def _modules_declaring_a_logger() -> set[str]:
        found = set()
        for path in _SRC.rglob("*.py"):
            if not _DECLARATION.search(path.read_text()):
                continue
            rel = path.relative_to(_SRC.parent).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts.pop()
            found.add(".".join(parts))
        return found

    def test_the_count_is_the_one_reconciled_by_hand(self) -> None:
        declared = self._modules_declaring_a_logger()
        assert len(declared) == 50, (
            f"expected 50 modules declaring a logger, found {len(declared)}. Bump the number "
            "here AND in this class's docstring above, which states it again and which nothing "
            "asserts (rule 144). Leave the *Landed* rows in docs/SIMPLIFICATION_PLAN.md alone: "
            "their figures are historical deltas, so editing one makes a correct record false."
        )

    def test_every_declared_logger_is_reachable_from_the_walk(self) -> None:
        """Imported first, because the walk can only see loaded modules -- which is also
        why a bare count of what it happens to find would drift with import order."""
        declared = self._modules_declaring_a_logger()
        for name in declared:
            importlib.import_module(name)

        walked = {
            name
            for name, module in list(sys.modules.items())
            if module is not None and (name == "reaper" or name.startswith("reaper."))
            for value in vars(module).values()
            if isinstance(value, BoundLoggerLazyProxy)
        }

        assert declared <= walked, sorted(declared - walked)
