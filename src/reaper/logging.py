# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured logging.

Reaper's logs are an audit trail, not just a debugging aid: "why did this get
deleted" must be answerable from them. So every event is structured, and the
score explanation is emitted as the same dict that the UI renders -- one source,
two sinks, no drift between what the user was shown and what actually happened.

Credentials must never reach a log. ``redact_secrets`` is a processor rather
than a convention because MDBList only accepts its key as a *query parameter*,
so a logged URL is a logged credential.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

from reaper import logbuffer

_SECRET_KEYS = frozenset(
    {
        "apikey",
        "api_key",
        "token",
        "authtoken",
        "auth_token",
        "password",
        "secret",
        "x-plex-token",
        "x-api-key",
        "api_key_enc",
        "token_enc",
        # A Discord webhook URL carries its token in the path, not a query string -- the
        # whole URL is a credential, so never log it under these keys.
        "webhook",
        "webhook_url",
    }
)

# Keys smuggled into a URL query string, e.g. Tautulli's ?apikey= and MDBList's.
_SECRET_QS = re.compile(r"([?&](?:apikey|api_key|token|X-Plex-Token)=)[^&\s]+", re.IGNORECASE)

REDACTED = "[redacted]"


def _redact_str(text: str) -> str:
    return _SECRET_QS.sub(rf"\1{REDACTED}", text) if "=" in text else text


def _redact_value(value: Any) -> Any:
    """Scrub secrets from a value of any shape.

    A secret does not only arrive as a top-level string: it can be nested in a dict
    or list (``params={'apikey': ...}``, a headers dict), or logged as ``bytes``. This
    recurses so the last-line-of-defense actually covers those, redacting any nested
    key whose name is a secret and applying the query-string pattern to every leaf.
    """
    if isinstance(value, dict):
        return {
            k: (REDACTED if str(k).lower() in _SECRET_KEYS else _redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            return value
        redacted = _redact_str(text)
        # Keep it as bytes unless we actually scrubbed something, so ordinary binary
        # payloads log as they did before.
        return redacted if redacted != text else value
    if isinstance(value, str):
        return _redact_str(value)
    return value


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    for key, value in event_dict.items():
        if key.lower() in _SECRET_KEYS:
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact_value(value)
    return event_dict


# Third-party loggers pinned to WARNING, so Reaper's own DEBUG level shows Reaper's events,
# never a library's firehose. Two distinct harms, both cured at the source:
#
# * The HTTP clients log request URLs verbatim through the stdlib -- which the structlog
#   redaction processor never sees, because it only runs on structlog events. httpx logs
#   every request at INFO as "HTTP Request: GET https://host/...?apikey=SECRET", and
#   Tautulli, Plex and MDBList all carry their credential in the query string, so an
#   unquieted httpx logger writes those keys straight to the log in cleartext.
# * aiosqlite (and SQLAlchemy's engine) log one DEBUG line per SQL cursor operation. A single
#   scan inserts thousands of candidate rows, so at DEBUG those tens of thousands of lines
#   flood the ring and the on-disk file and EVICT every diagnostic that matters -- the
#   per-item scan decisions, the Plex-match lines -- before the operator can read them,
#   turning DEBUG mode against itself. (Observed live: a downloaded DEBUG log was 99.98%
#   aiosqlite, with zero decision lines left.)
#
# Reaper emits its own structured, redacted event for anything that matters, so none of these
# libraries' own logs are needed. WARNING is an EXPLICIT level, so it survives a runtime root
# switch to DEBUG (logbuffer.set_level only moves the root); genuine SQL/HTTP errors still
# surface, because WARNING and above pass.
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "plexapi", "aiosqlite", "sqlalchemy")

# Keys the ring's plain-text line should not repeat: they are carried as their own
# columns (or are rendering internals), not payload.
_RING_STRUCTURAL_KEYS = frozenset({"event", "level", "timestamp", "exception", "stack"})


def _drop_below_level(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Dynamic level filtering, consulted per event.

    The wrapper class passes everything through and THIS decides, so the operator's
    Settings -> Logs choice takes effect immediately -- cached bound loggers and all --
    instead of only applying to loggers bound after a reconfigure.
    """
    name = str(event_dict.get("level", "info")).upper()
    if getattr(logging, name, logging.INFO) < logbuffer.level_no():
        raise structlog.DropEvent
    return event_dict


def _capture_to_ring(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Copy the (already redacted) event into the UI's log ring, render-ready.

    Sits after ``redact_secrets`` and ``format_exc_info`` on purpose: the ring must
    never hold a secret the console would not, and a traceback should arrive as text.
    Read-only on the event dict -- the real renderer still runs after this.
    """
    parts = [str(event_dict.get("event", ""))]
    parts += [f"{k}={event_dict[k]}" for k in event_dict if k not in _RING_STRUCTURAL_KEYS]
    exception = event_dict.get("exception")
    text = " ".join(p for p in parts if p)
    if exception:
        text = f"{text}\n{exception}"
    logbuffer.RING.append(
        ts=str(event_dict.get("timestamp", "")),
        level=str(event_dict.get("level", "info")),
        text=text,
    )
    return event_dict


class _RingHandler(logging.Handler):
    """The stdlib bridge: uvicorn, alembic, apscheduler and friends land in the same
    ring the structlog pipeline feeds, so the Logs tab shows one merged stream.

    Redacts query-string credentials itself -- these records never pass the structlog
    processors -- and, like any logging handler, must never raise.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created, tz=UTC).isoformat()
            text = f"{record.name}: {_redact_str(record.getMessage())}"
            if record.exc_info and record.exc_info[0] is not None:
                text = f"{text}\n{self.format(record)}"
            logbuffer.RING.append(ts=ts, level=record.levelname, text=text)
        except Exception:
            self.handleError(record)


def configure_logging(
    *, level: str = "INFO", json_logs: bool = False, data_dir: Path | None = None
) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper()),
    )

    # One ring handler on the root, however many times configure_logging runs (tests
    # build many apps in one process; stacking handlers would duplicate every line).
    root = logging.getLogger()
    if not any(isinstance(h, _RingHandler) for h in root.handlers):
        root.addHandler(_RingHandler())

    logbuffer.set_level(level)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            _drop_below_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _capture_to_ring,
            renderer,
        ],
        # Filtering happens in _drop_below_level per event, against the LIVE level, so
        # the Settings -> Logs change needs no reconfigure and no cache invalidation.
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        cache_logger_on_first_use=True,
    )

    # Mirror the ring to rotating files on disk when we have a data dir (the running app
    # always does; a bare configure_logging in a test does not, so it stays stdout-only).
    if data_dir is not None:
        logbuffer.configure_file_logging(data_dir)
