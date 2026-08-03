# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured logging.

Reaper's logs are an audit trail, not just a debugging aid: "why did this get
deleted" must be answerable from them. So every event is structured, and the
score explanation is emitted as the same dict that the UI renders -- one source,
two sinks, no drift between what the user was shown and what actually happened.

Credentials must never reach a log. ``redact_secrets`` is a processor rather
than a convention because MDBList only accepts its key as a *query parameter*,
so a logged URL is a logged credential.

**A ``log.debug(...)`` call evaluates its arguments even when the level is INFO.**
The wrapper class passes everything through and `_drop_below_level` decides per event,
which is what makes the operator's Settings choice apply instantly -- and it means the
argument list has already been built by the time anything can drop it. Free for a line
made of locals already in hand, which is nearly all of them. Worth a thought before
making a debug line's arguments out of a sort, a join, or a walk over the library.
"""

from __future__ import annotations

import logging
import re
import sys
import traceback
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

# A Discord webhook carries its secret in the URL *path*, not a query string:
# ``https://discord.com/api/webhooks/<id>/<token>``. Key-name matching (``_SECRET_KEYS``)
# only catches it when the URL happens to be logged under one of those keys, and a URL is
# just as likely to arrive as a bare string -- inside an exception message, or under a
# neutral ``url=``. Match the shape itself so the token is scrubbed however it is logged
# (S-2). The id is left visible: it identifies the channel, it is not the credential.
#
# Case-sensitive on purpose, matching the marker guard below exactly: the only webhook URL
# that can reach a log is one the operator stored, and ``_validated_discord_webhook``
# (api/settings.py) refuses anything whose path does not literally start "/api/webhooks/".
# An IGNORECASE regex behind a case-sensitive guard would look broader than it is.
_SECRET_PATH = re.compile(r"(/api/webhooks/\d+/)[^/\s?\"'&]+")

#: The cheap pre-test that decides whether a string can possibly hold a secret. Every
#: string leaf of every event passes through here, so the common case must not run two
#: regexes: a query-string secret needs an ``=``, and a webhook path needs the literal
#: below. Anything with neither is returned untouched.
_WEBHOOK_MARKER = "/api/webhooks/"

REDACTED = "[redacted]"


def _redact_str(text: str) -> str:
    if "=" in text:
        text = _SECRET_QS.sub(rf"\1{REDACTED}", text)
    if _WEBHOOK_MARKER in text:
        text = _SECRET_PATH.sub(rf"\1{REDACTED}", text)
    return text


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
#   Tautulli, Plex and MDBList all carry their credential in the query string -- while a
#   Discord webhook carries its token in the PATH -- so an unquieted HTTP logger writes
#   those secrets straight to the log in cleartext. Both httpx and httpx2 are pinned here:
#   Reaper is mid-migration off the unmaintained httpx onto httpx2 (see
#   docs/history/PLAN-narrative.md), and
#   httpx2 renames its loggers to "httpx2"/"httpcore2" -- quiet only "httpx" and the secret
#   leak silently returns the moment a client moves over. (notify/discord.py is already on
#   httpx2.)
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
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "httpx2",
    "httpcore2",
    "urllib3",
    "plexapi",
    "aiosqlite",
    "sqlalchemy",
)

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

    Sits after ``format_exc_info`` AND then ``redact_secrets``, in that order, on purpose:
    a traceback should arrive as text, and the scrubber has to run on it once it is text,
    or the ring holds a secret the console would not. Read-only on the event dict -- the
    real renderer still runs after this.
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

    Redacts credentials itself -- these records never pass the structlog
    processors -- and, like any logging handler, must never raise.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created, tz=UTC).isoformat()
            text = f"{record.name}: {_redact_str(record.getMessage())}"
            if record.exc_info and record.exc_info[0] is not None:
                # Format ONLY the traceback, and scrub it. This used to call
                # ``self.format(record)``, which -- with no formatter set on this handler --
                # falls back to "%(message)s" and re-rendered the message UNREDACTED beneath
                # the redacted copy, so every exception line landed in the ring and on disk
                # with its credentials in the clear (S2-1). The traceback needs the scrubber
                # in its own right: httpx2.HTTPStatusError's str() embeds the full request
                # URL, so any library reporting an *arr/Tautulli HTTP error with exc_info
                # carries the query-string key even when the message itself is clean.
                trace = "".join(traceback.format_exception(*record.exc_info))
                text = f"{text}\n{_redact_str(trace)}"
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
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # AFTER format_exc_info, not before. That processor replaces ``exc_info`` with
            # a rendered ``exception`` string, and running the scrubber first meant the
            # traceback was born after the only thing that would have cleaned it -- the
            # structlog twin of the stdlib handler's S2-1 leak. It matters because an
            # httpx2.HTTPStatusError's str() embeds the full request URL, so any
            # ``log.warning(..., exc_info=True)`` around an *arr/Tautulli call carried that
            # query-string key into the ring and onto disk. Everything else in the event
            # dict is scrubbed exactly as before; this only widens what is covered.
            redact_secrets,
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
