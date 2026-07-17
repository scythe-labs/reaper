# SPDX-License-Identifier: AGPL-3.0-or-later
"""The in-memory log ring the Settings -> Logs tab reads.

Reaper's logs are an audit trail ("why did this get deleted" must be answerable), and
the operator should not need shell access to a container to read them. This module
holds the newest lines in memory -- a bounded ring, never a file -- with a monotonic
sequence number so the UI can poll incrementally ("everything after seq N") without
re-sending the window on every tick.

It also owns the *dynamic* log level. The stored setting (Settings -> Logs) wins over
the ``REAPER_LOG_LEVEL`` environment value after first boot, exactly like every other
env-seeded switch, and changing it takes effect immediately: the structlog pipeline
consults :func:`level_no` per event (see ``reaper.logging``), and the stdlib root logger
is re-levelled in :func:`set_level`.

Everything appended here has already passed the redaction layer -- the structlog
processor sits after ``redact_secrets``, and the stdlib handler scrubs query-string
credentials itself -- so the ring never holds a secret the log files would not.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass

#: The levels the operator can pick. ERROR is deliberately absent from the UI choices:
#: hiding warnings from a tool that deletes files serves nobody.
LEVELS = ("DEBUG", "INFO", "WARNING")

#: How many lines the ring keeps. Enough to cover a full scan with room around it,
#: small enough to be irrelevant to memory.
RING_SIZE = 2000


@dataclass(frozen=True, slots=True)
class LogLine:
    seq: int
    ts: str
    """ISO timestamp, UTC, as the logging pipeline stamped it."""
    level: str
    text: str


class LogRing:
    """A bounded, thread-safe ring of rendered log lines.

    Thread-safe because not everything logs from the event loop: APScheduler jobs and
    sync client code can emit from worker threads, and a torn append under a reader
    would be a crash in the very tool used to debug crashes.
    """

    def __init__(self, maxlen: int = RING_SIZE) -> None:
        self._lines: deque[LogLine] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def append(self, *, ts: str, level: str, text: str) -> None:
        with self._lock:
            self._seq += 1
            self._lines.append(LogLine(seq=self._seq, ts=ts, level=level.upper(), text=text))

    def since(self, after: int, *, limit: int = 500) -> list[LogLine]:
        """The lines newer than ``after``, oldest first, capped at ``limit``.

        ``after=0`` is the UI's first poll: it gets the newest ``limit`` lines the ring
        still holds. A cursor older than the ring's tail simply yields what remains --
        dropped lines are gone, and pretending otherwise would be inventing evidence.
        """
        with self._lock:
            fresh = [line for line in self._lines if line.seq > after]
        return fresh[-limit:]

    def last_seq(self) -> int:
        with self._lock:
            return self._seq


#: The one ring the app writes and the API reads.
RING = LogRing()

_level_lock = threading.Lock()
_level_no = logging.INFO


def normalize_level(name: str) -> str | None:
    """The canonical level name, or None for anything not offered in the UI."""
    upper = (name or "").strip().upper()
    return upper if upper in LEVELS else None


def level_no() -> int:
    """The current numeric threshold, consulted per event by the logging pipeline."""
    return _level_no


def level_name() -> str:
    return logging.getLevelName(_level_no)


def set_level(name: str) -> str:
    """Apply a new level everywhere at once: the structlog filter (via
    :func:`level_no`) and the stdlib root logger. Returns the canonical name.

    Unknown names fall back to INFO rather than raising: the logging system must never
    be crashable by a stored setting (the API validates before storing; this is the
    second net).
    """
    global _level_no
    canonical = normalize_level(name) or "INFO"
    with _level_lock:
        _level_no = getattr(logging, canonical)
        logging.getLogger().setLevel(_level_no)
    return canonical
