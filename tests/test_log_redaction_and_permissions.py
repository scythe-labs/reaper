# SPDX-License-Identifier: AGPL-3.0-or-later
"""What reaches the log ring and the file beside it, and who can read the file.

The Logs tab is downloadable, and the file under ``data/logs`` outlives the process, so
anything that lands there is as good as published to whoever can read the volume. Two
properties are pinned here:

* **Nothing arrives unscrubbed.** A credential can arrive three ways: in a query string, in a
  Discord webhook's URL path, or inside a rendered traceback. All three must be scrubbed on
  both the stdlib and structlog logging paths.
* **The log file is owner-only from the moment it is created**, not just placed inside a
  directory that is owner-only.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from reaper import logbuffer
from reaper.logging import REDACTED, _redact_str, _RingHandler, configure_logging, redact_secrets

#: A stand-in Discord webhook. The id is public (it names the channel). The segment after
#: it is the credential, and it is the only part that must disappear.
WEBHOOK = "https://discord.com/api/webhooks/123456789/AAAAsecret-token-BBBB"
TOKEN_SEGMENT = "AAAAsecret-token-BBBB"

#: A Tautulli-shaped URL: the credential rides the query string instead.
QS_URL = "http://tautulli.example.net:8181/api/v2?apikey=SUPERSECRET&cmd=get_history"


@pytest.fixture(autouse=True)
def _fresh_ring() -> Iterator[None]:
    """Each test reads its own lines, not the ones a neighbor left behind.

    The file sink is a module global too, but it is torn down for the whole suite by
    ``conftest.py``'s ``_no_file_sink_left_behind`` rather than here.
    """
    logbuffer.RING = logbuffer.LogRing()
    yield
    logbuffer.RING = logbuffer.LogRing()


def _ring_text() -> str:
    return "\n".join(line.text for line in logbuffer.RING.since(0))


class TestTheStdlibBridgeScrubs:
    """``_RingHandler`` is the only scrubber the stdlib path gets: uvicorn, alembic and
    APScheduler records never pass a structlog processor."""

    def _emit(self, message: str, *, exc: BaseException | None = None) -> None:
        handler = _RingHandler()
        record = logging.LogRecord(
            name="somelib",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg=message,
            args=None,
            exc_info=(type(exc), exc, exc.__traceback__) if exc is not None else None,
        )
        handler.emit(record)

    def test_a_query_string_secret_is_scrubbed(self) -> None:
        self._emit(f"GET {QS_URL}")
        assert "SUPERSECRET" not in _ring_text()
        assert REDACTED in _ring_text()

    def test_a_webhook_path_secret_is_scrubbed(self) -> None:
        self._emit(f"POST {WEBHOOK} failed")
        text = _ring_text()
        assert TOKEN_SEGMENT not in text
        # The channel id survives. It is what makes the line diagnosable at all.
        assert "123456789" in text

    def test_an_exception_record_yields_one_scrubbed_copy_of_the_message(self) -> None:
        """Every stdlib record with ``exc_info`` must come out scrubbed exactly once. The
        handler sets no formatter, so a caller that skipped scrubbing the fallback would see
        ``self.format(record)`` render the raw message a second time under
        ``"%(message)s"``, putting the credential back in the clear and printing the
        exception line twice."""
        try:
            raise ValueError("boom")
        except ValueError as exc:
            self._emit(f"GET {QS_URL}", exc=exc)

        text = _ring_text()
        assert "SUPERSECRET" not in text
        assert text.count(REDACTED) == 1, text
        # And the traceback still arrives, which is why exc_info was set.
        assert "ValueError: boom" in text

    def test_the_traceback_itself_is_scrubbed(self) -> None:
        """The more reachable half of the same leak. An HTTP error's ``str()`` carries the
        request URL, so the credential ends up in the traceback even when the message itself
        is clean.
        """
        try:
            raise RuntimeError(f"Server error '500' for url '{QS_URL}'")
        except RuntimeError as exc:
            self._emit("upstream call failed", exc=exc)

        text = _ring_text()
        assert "SUPERSECRET" not in text
        assert REDACTED in text


class TestTheStructlogPipelineScrubs:
    def test_a_rendered_traceback_is_scrubbed(self, tmp_path: Path) -> None:
        """``format_exc_info`` renders the traceback into the event dict, so the scrubber
        must run after it in the processor chain. Run the other way, the traceback would be
        created after the one step that could clean it."""
        configure_logging(level="INFO")
        log = structlog.get_logger("test")
        try:
            raise RuntimeError(f"Server error '500' for url '{QS_URL}'")
        except RuntimeError:
            log.warning("call.failed", exc_info=True)

        text = _ring_text()
        assert "SUPERSECRET" not in text
        assert "call.failed" in text

    def test_a_webhook_url_under_a_neutral_key_is_scrubbed(self) -> None:
        """Key-name matching only catches a webhook logged under a name this module lists. A
        plain ``url=`` key needs the path pattern instead to catch the credential."""
        out = redact_secrets(None, "warning", {"event": "notify.failed", "url": WEBHOOK})
        assert TOKEN_SEGMENT not in str(out["url"])
        assert REDACTED in str(out["url"])

    def test_a_webhook_nested_in_a_dict_is_scrubbed(self) -> None:
        out = redact_secrets(None, "warning", {"event": "x", "req": {"target": WEBHOOK}})
        assert TOKEN_SEGMENT not in str(out["req"])


class TestTheScrubberLeavesOrdinaryTextAlone:
    """A scrubber that mangles ordinary lines makes the log worse, so pin the negative."""

    @pytest.mark.parametrize(
        "text",
        [
            "scan complete: 412 items",
            "GET http://sonarr.example.net:8989/api/v3/series",
            "a=b c=d",
            "/api/webhooks/ is not a webhook url",
        ],
    )
    def test_untouched(self, text: str) -> None:
        assert _redact_str(text) == text


class TestTheLogFileIsOwnerOnly:
    def test_the_file_is_created_owner_only(self, tmp_path: Path) -> None:
        """``RotatingFileHandler`` takes no mode argument, so without an explicit chmod the
        log file would be created at the default 0644 under a normal umask. The file itself
        must be owner-only from the moment it is created, not just the directory holding
        it."""
        old_umask = os.umask(0o022)  # the typical default, made explicit
        try:
            logbuffer.configure_file_logging(tmp_path)
            logbuffer.RING.append(ts="2026-07-24T00:00:00Z", level="INFO", text="hello")
        finally:
            os.umask(old_umask)

        path = tmp_path / logbuffer.LOG_DIRNAME / logbuffer.LOG_FILENAME
        assert path.is_file()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"log file is {oct(mode)}, expected 0o600"

    def test_the_directory_stays_owner_only_too(self, tmp_path: Path) -> None:
        logbuffer.configure_file_logging(tmp_path)
        mode = stat.S_IMODE((tmp_path / logbuffer.LOG_DIRNAME).stat().st_mode)
        assert mode == 0o700, f"log dir is {oct(mode)}, expected 0o700"

    def test_a_file_left_world_readable_by_an_earlier_version_is_tightened(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / logbuffer.LOG_DIRNAME
        log_dir.mkdir()
        path = log_dir / logbuffer.LOG_FILENAME
        path.write_text("old line\n")
        path.chmod(0o644)

        logbuffer.configure_file_logging(tmp_path)
        logbuffer.RING.append(ts="2026-07-24T00:00:00Z", level="INFO", text="hello")

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
