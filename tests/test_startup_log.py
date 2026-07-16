# SPDX-License-Identifier: AGPL-3.0-or-later
"""The startup banner must tell the truth about deletion.

``REAPER_DESTRUCTIVE_ACTIONS_ENABLED`` only seeds the first run; after that the
stored toggle wins. The one place an operator looks after a restart is the log,
so the banner reads the EFFECTIVE permission: an install armed from the web UI
must never boot saying "nothing can be deleted", and a fresh install must never
boot claiming it is armed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import insert

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import AppSetting
from reaper.main import create_app


class _RecordingLogger:
    """Stands in for ``reaper.main.log``: captures events instead of parsing output.

    Deliberately not ``structlog.testing.capture_logs``: ``create_app`` calls
    ``configure_logging`` during startup, which would silently replace the capture
    configuration and record nothing.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **kw: object) -> None:
        self.events.append(("info", event, kw))

    def warning(self, event: str, **kw: object) -> None:
        self.events.append(("warning", event, kw))

    def names(self) -> list[str]:
        return [event for _level, event, _kw in self.events]


def _make(tmp_path: Path, *, stored_destructive: bool | None) -> Settings:
    """A schema-initialised install; optionally with the deletion toggle already stored,
    the way a real install looks after someone armed it in the web UI."""
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    if stored_destructive is not None:
        with engine.begin() as conn:
            conn.execute(
                insert(AppSetting).values(
                    key="destructive_enabled",
                    value_json=json.dumps(stored_destructive),
                    updated_at=utcnow(),
                )
            )
    engine.dispose()
    return settings


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _RecordingLogger:
    # Hermetic, matching the other app-startup tests: no .env seeding, no dataset
    # download racing teardown.
    monkeypatch.setattr("reaper.main.load_raw_env", lambda _s: {})
    monkeypatch.setattr("reaper.main.catch_up_on_startup", _noop)
    rec = _RecordingLogger()
    monkeypatch.setattr("reaper.main.log", rec)
    return rec


class TestStartupBanner:
    def test_an_install_armed_in_the_ui_boots_saying_so(
        self, tmp_path: Path, recorder: _RecordingLogger
    ) -> None:
        """Env says disabled, the stored toggle says armed: the stored value is the
        truth, and the old banner's "nothing can be deleted" would be a false claim."""
        settings = _make(tmp_path, stored_destructive=True)
        assert settings.destructive_actions_enabled is False  # the env default

        with TestClient(create_app(settings)):
            pass

        assert "reaper.armed" in recorder.names()
        assert "reaper.safe_mode" not in recorder.names()
        started = next(kw for _l, event, kw in recorder.events if event == "reaper.started")
        assert started["destructive_actions_enabled"] is True

    def test_a_fresh_install_boots_read_only(
        self, tmp_path: Path, recorder: _RecordingLogger
    ) -> None:
        settings = _make(tmp_path, stored_destructive=None)

        with TestClient(create_app(settings)):
            pass

        assert "reaper.safe_mode" in recorder.names()
        assert "reaper.armed" not in recorder.names()
        started = next(kw for _l, event, kw in recorder.events if event == "reaper.started")
        assert started["destructive_actions_enabled"] is False

    def test_disarming_in_the_ui_survives_a_restart_of_an_armed_env(
        self, tmp_path: Path, recorder: _RecordingLogger
    ) -> None:
        """The mirror case: env ships armed, but someone turned deletion OFF in the UI.
        The stored OFF must win, or a restart would silently re-arm the tool."""
        settings = _make(tmp_path, stored_destructive=False)
        settings.destructive_actions_enabled = True  # the env shipped armed

        with TestClient(create_app(settings)):
            pass

        assert "reaper.safe_mode" in recorder.names()
        assert "reaper.armed" not in recorder.names()
