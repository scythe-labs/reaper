# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every module logger stays interceptable by ``capture_logs``, whatever ran before it.

The suite asserts on log events in a few dozen places, and structlog can quietly stop
delivering them. ``configure_logging`` sets ``cache_logger_on_first_use=True``. The first use
of a module logger after that freezes its proxy against the processor list live at that
moment. ``capture_logs`` mutates the configured list in place, on purpose, to keep
bound-logger references working, so the frozen logger keeps up until a later
``configure_logging`` installs a fresh list. From then on, the frozen logger renders through
the old list, and no ``capture_logs`` can reach it. Two app boots are enough to cause this,
and the suite builds many apps.

A test that fails for a reason unrelated to what it tests is worse than no test. It spends
the reviewer's attention and teaches them to re-run rather than read.
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

#: How every logger in ``src/`` is declared. The walk in ``uncache_module_loggers`` reaches a
#: proxy held in a module's globals, so a logger built any other way would silently drop out
#: of the guard. This is the spelling that keeps that from happening.
_DECLARATION = re.compile(r"^log = structlog\.get_logger\(__name__\)$", re.MULTILINE)

_SRC = Path(__file__).resolve().parents[1] / "src" / "reaper"


def _freeze_the_executor_logger() -> None:
    """Cache the proxy against one processor list, then leave a newer one configured.

    Both calls go through the real ``configure_logging``, the function a ``create_app`` boot
    calls. The first call sets the caching flag and a fresh list, and logging through it
    freezes the proxy against that list. The second call installs a new list, so the proxy
    keeps pointing at the one it no longer holds.
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
        """The other half, so the test above cannot pass just because the freezing never
        happened. Without the guard, the event is emitted and ``capture_logs`` sees nothing
        at all."""
        _freeze_the_executor_logger()

        with capture_logs() as logs:
            executor.log.warning("reap.journal_revive_failed")
        assert logs == []

        # Left frozen, this logger would leak into whatever runs next on this worker. The
        # autouse fixture undoes the freeze before the next test, and so does this line, so
        # this test cannot cause the bug it demonstrates.
        uncache_module_loggers()


class TestTheGuardReachesEveryLoggerInTheTree:
    """A guard that scans a tree is proven against the whole population it claims to cover,
    not just one member that already matches. The count below is reconciled by hand: 54 files
    under ``src/reaper`` declare a module-level logger, and ``grep -rl`` over the tree agrees.
    Adding or removing a logger changes this count, and the same ``grep -rl`` search
    reconciles the new one."""

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
        assert len(declared) == 54, (
            f"expected 54 modules declaring a logger, found {len(declared)}. Bump the number "
            "here AND in this class's docstring above, which restates it and which nothing "
            "else asserts (rule 144). Those are the only two live copies. The archived "
            "simplification plan restates the figure too, and it is frozen history, so its "
            "copy stays at the number it measured."
        )

    def test_every_declared_logger_is_reachable_from_the_walk(self) -> None:
        """Imports every declared module first, because the walk can only see loaded
        modules. A bare count of what it happens to find would otherwise drift with import
        order."""
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
