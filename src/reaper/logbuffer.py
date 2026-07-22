# SPDX-License-Identifier: AGPL-3.0-or-later
"""The log ring the Settings -> Logs tab reads, and the rotating files on disk.

Reaper's logs are an audit trail ("why did this get deleted" must be answerable), and
the operator should not need shell access to a container to read them. This module
holds the newest lines in memory -- a bounded ring -- with a monotonic sequence number
so the UI can poll incrementally ("everything after seq N") without re-sending the
window on every tick.

The same lines are also mirrored to rotating files under ``<data_dir>/logs`` (see
:func:`configure_file_logging`), so the trail survives a restart and can be downloaded
whole from the Logs tab -- a fuller history than the in-memory window keeps. The mirror
is fed from the one place every line already converges, :meth:`LogRing.append`, so the
files carry exactly what the UI shows: there is no second, unredacted path to disk.

It also owns the *dynamic* log level. The stored setting (Settings -> Logs) wins over
the ``REAPER_LOG_LEVEL`` environment value after first boot, exactly like every other
env-seeded switch, and changing it takes effect immediately: the structlog pipeline
consults :func:`level_no` per event (see ``reaper.logging``), and the stdlib root logger
is re-leveled in :func:`set_level`.

Everything appended here has already passed the redaction layer -- the structlog
processor sits after ``redact_secrets``, and the stdlib handler scrubs query-string
credentials itself -- so neither the ring nor the files hold a secret the console would
not.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: The levels the operator can pick. ERROR is deliberately absent from the UI choices:
#: hiding warnings from a tool that deletes files serves nobody.
LEVELS = ("DEBUG", "INFO", "WARNING")

#: How many lines the ring keeps. Enough to cover a full scan with room around it,
#: small enough to be irrelevant to memory.
RING_SIZE = 2000

#: Rotating log files on disk. Bounded on purpose: at most three files of 20 MiB each
#: (the live file plus two rotations), so the logs can never fill the data volume. The
#: newest lines land in ``reaper.log``; when it fills it rolls to ``reaper.log.1`` and
#: the older one to ``reaper.log.2``, and the oldest is discarded. These mirror the ring.
LOG_DIRNAME = "logs"
LOG_FILENAME = "reaper.log"
LOG_MAX_BYTES = 20 * 1024 * 1024
LOG_BACKUP_COUNT = 2


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
        # Mirror the same, already-redacted line to disk. Read the sink outside the ring
        # lock so file I/O never blocks a reader, and via a plain module-global read (a
        # single reference load is atomic in CPython; configure_file_logging swaps it).
        sink = _file_sink
        if sink is not None:
            sink.write(ts=ts, level=level.upper(), text=text)

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


class _FileSink:
    """A rotating file the ring mirrors each line into.

    Written through the stdlib :class:`RotatingFileHandler`, which owns the rollover and
    the lock -- so it is thread-safe and caps the on-disk footprint on its own. It is fed
    pre-rendered, already-redacted lines (see :meth:`LogRing.append`), so it never touches
    the redaction path itself. ``delay=True`` means the file is not created until the
    first line, so an install that never logs leaves no empty file behind.
    """

    def __init__(self, path: Path) -> None:
        self._handler = RotatingFileHandler(
            path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))

    def write(self, *, ts: str, level: str, text: str) -> None:
        # args=None so getMessage never runs %-formatting: a line containing a stray '%'
        # is written verbatim, not interpreted as a format string.
        record = logging.LogRecord(
            name="reaper",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=f"{ts} {level:<7} {text}",
            args=None,
            exc_info=None,
        )
        # A logging sink must never raise into its caller: a full or vanished disk cannot
        # be allowed to crash the very tool used to debug it. handle() takes the handler
        # lock and rolls over as needed.
        with suppress(Exception):
            self._handler.handle(record)

    def close(self) -> None:
        with suppress(Exception):
            self._handler.close()


_file_lock = threading.Lock()
_file_sink: _FileSink | None = None
_log_path: Path | None = None


def configure_file_logging(data_dir: Path) -> None:
    """Start (or re-point) mirroring the ring to rotating files under ``data_dir/logs``.

    Idempotent: called once per app boot, it closes any prior sink and opens a fresh one,
    so repeated ``create_app`` calls in one process (the test suite) never stack file
    handles. File logging is a convenience layered on top of stdout and the in-memory
    ring; a data dir that cannot be written must degrade to "no files," never stop the
    app from booting or logging, so a failure here is logged and swallowed.
    """
    global _file_sink, _log_path
    log_dir = data_dir / LOG_DIRNAME
    path = log_dir / LOG_FILENAME
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        sink = _FileSink(path)
    except OSError:
        logging.getLogger(__name__).warning(
            "log file setup failed; logging to the console and Logs tab only",
            exc_info=True,
        )
        return
    with _file_lock:
        old, _file_sink, _log_path = _file_sink, sink, path
    if old is not None:
        old.close()


def log_files() -> list[Path]:
    """The on-disk log files that currently exist, oldest first.

    Ordered ``reaper.log.2, reaper.log.1, reaper.log`` (the rotations are numbered oldest
    to newest and the live file has no suffix), so concatenating them reads in time order.
    Empty when file logging is off or nothing has been written yet.
    """
    with _file_lock:
        base = _log_path
    if base is None:
        return []
    ordered = [base.with_name(f"{base.name}.{n}") for n in range(LOG_BACKUP_COUNT, 0, -1)]
    ordered.append(base)
    return [p for p in ordered if p.exists()]


def dump_lines() -> list[LogLine]:
    """Every line the in-memory ring still holds, oldest first.

    The download's fallback when no file exists on disk yet, so the button always yields
    the recent history rather than an empty file.
    """
    return RING.since(0, limit=RING_SIZE)


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
