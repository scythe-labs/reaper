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
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

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
    recurses so the last-line-of-defence actually covers those, redacting any nested
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


# Third-party loggers that log request URLs verbatim through the stdlib -- which the
# structlog redaction processor never sees, because it only runs on structlog events.
# httpx logs every request at INFO as "HTTP Request: GET https://host/...?apikey=SECRET",
# and Tautulli, Plex and MDBList all carry their credential in the query string. So an
# unquieted httpx logger writes those keys straight to the log in cleartext.
#
# We do not need httpx's request log: Reaper emits its own structured, redacted event for
# anything that matters. Lifting these to WARNING removes the leak at the source rather
# than trying to scrub it after the fact.
_NOISY_HTTP_LOGGERS = ("httpx", "httpcore", "urllib3", "plexapi")


def configure_logging(*, level: str = "INFO", json_logs: bool = False) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper()),
    )

    for name in _NOISY_HTTP_LOGGERS:
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
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        cache_logger_on_first_use=True,
    )
