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
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import create_async_engine

from reaper import buildinfo
from reaper.buildinfo import install_kind
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import AppSetting
from reaper.db.session import journal_mode
from reaper.main import create_app
from reaper.services import scheduler


class _RecordingLogger:
    """Stands in for ``reaper.main.log``: captures events instead of parsing output.

    Deliberately not ``structlog.testing.capture_logs``: ``create_app`` calls
    ``configure_logging`` during startup, which would silently replace the capture
    configuration and record nothing.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def debug(self, event: str, **kw: object) -> None:
        self.events.append(("debug", event, kw))

    def info(self, event: str, **kw: object) -> None:
        self.events.append(("info", event, kw))

    def warning(self, event: str, **kw: object) -> None:
        self.events.append(("warning", event, kw))

    def names(self) -> list[str]:
        return [event for _level, event, _kw in self.events]


def _make(
    tmp_path: Path,
    *,
    stored_destructive: bool | None,
    stored_log_level: str | None = None,
    revision: str | None = None,
) -> Settings:
    """A schema-initialized install; optionally with the deletion toggle already stored,
    the way a real install looks after someone armed it in the web UI.

    ``revision`` writes the ``alembic_version`` row that ``create_all`` never makes, so the
    banner's revision field can be pinned to a value that is not the default (rule 141)."""
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    if revision is not None:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num TEXT NOT NULL)"))
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": revision}
            )
    stored: dict[str, object] = {}
    if stored_destructive is not None:
        stored["destructive_enabled"] = stored_destructive
    if stored_log_level is not None:
        stored["log_level"] = stored_log_level
    if stored:
        with engine.begin() as conn:
            for key, value in stored.items():
                conn.execute(
                    insert(AppSetting).values(
                        key=key, value_json=json.dumps(value), updated_at=utcnow()
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


class TestTheInstallFingerprint:
    """Which of the four shipped shapes is running.

    Support reads this before anything else, because the four keep their data in four
    places and take their configuration by four routes. Each branch is driven through
    the signal only that shape sets, and the source-checkout case is driven with every
    signal absent -- an unrecognized install must report the shape with the fewest
    promises attached rather than guess at a fifth (rule 145: every branch, not only
    the one the default state hands you).
    """

    def test_a_frozen_bundle_is_a_desktop_build(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
        monkeypatch.setenv("REAPER_HOME", "/snap/reaper/current")  # frozen still wins
        assert install_kind() == "desktop"

    def test_reaper_home_alone_is_the_snap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        monkeypatch.setenv("REAPER_HOME", "/snap/reaper/current")
        assert install_kind() == "snap"

    @pytest.mark.parametrize("marker", ["/.dockerenv", "/run/.containerenv"])
    def test_either_runtime_marker_is_a_container(
        self, monkeypatch: pytest.MonkeyPatch, marker: str
    ) -> None:
        """Docker plants the first, Podman the second."""
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        monkeypatch.delenv("REAPER_HOME", raising=False)
        monkeypatch.setattr(
            buildinfo.Path, "exists", lambda self: str(self) == marker, raising=False
        )
        assert install_kind() == "container"

    def test_no_signal_at_all_is_a_source_checkout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        monkeypatch.delenv("REAPER_HOME", raising=False)
        monkeypatch.setattr(buildinfo.Path, "exists", lambda self: False, raising=False)
        assert install_kind() == "source"


class TestTheBootBannerDescribesTheInstall:
    def test_it_names_the_shape_the_paths_and_where_the_level_came_from(
        self, tmp_path: Path, recorder: _RecordingLogger
    ) -> None:
        """ "It doesn't work on my server" starts here. ``log_level_from`` is the field
        that unblocks every other session: a stored level silently outranks
        REAPER_LOG_LEVEL, so "I set DEBUG in compose and restarted" can do nothing at
        all with no way to tell."""
        settings = _make(tmp_path, stored_destructive=None)

        with TestClient(create_app(settings)):
            pass

        install = next(kw for _l, event, kw in recorder.events if event == "reaper.install")
        assert install["install"] in {"container", "snap", "desktop", "source"}
        assert install["data_dir"] == str(tmp_path)
        assert install["log_level_from"] == "environment"
        assert install["python"] and install["platform"]

        started = next(kw for _l, event, kw in recorder.events if event == "reaper.started")
        assert started["channel"] in {"release", "dev"}

    def test_a_stored_level_says_it_outranked_the_environment(
        self, tmp_path: Path, recorder: _RecordingLogger
    ) -> None:
        settings = _make(tmp_path, stored_destructive=None, stored_log_level="WARNING")

        with TestClient(create_app(settings)):
            pass

        install = next(kw for _l, event, kw in recorder.events if event == "reaper.install")
        assert install["log_level_from"] == "settings"
        assert install["log_level"] == "WARNING"

    def test_the_database_reports_its_revision_and_journal_mode(
        self, tmp_path: Path, recorder: _RecordingLogger
    ) -> None:
        """WAL is asked for and not always granted: a database on a network share stays
        on the rollback journal, which is every "database is locked" report.

        The revision is pinned to a stored value rather than to the ``None`` a
        ``create_all`` database returns anyway, so replacing the read with a constant
        fails here instead of passing on the fixture's own default (rule 141)."""
        settings = _make(tmp_path, stored_destructive=None, revision="deadbeef0001")

        with TestClient(create_app(settings)):
            pass

        db = next(kw for _l, event, kw in recorder.events if event == "db.ready")
        assert db["journal_mode"] == "wal"
        assert db["cache_journal_mode"] == "wal"
        assert db["revision"] == "deadbeef0001"

    async def test_journal_mode_reports_the_mode_the_database_settled_on(
        self, tmp_path: Path
    ) -> None:
        """The case the banner exists for, driven directly: a database that did not get WAL.

        It cannot be driven through the boot, because ``create_engine`` attaches
        ``_configure_sqlite`` per engine and that listener re-asks for WAL on every pooled
        connection. A plain engine has no listener, so the mode it was put in survives, and
        a `journal_mode` reduced to ``return "wal"`` fails here (rule 145)."""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/plain.db")
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql("PRAGMA journal_mode=DELETE")
            assert await journal_mode(engine) == "delete"
        finally:
            await engine.dispose()

    def test_an_unreadable_cache_database_does_not_stop_the_boot(
        self, tmp_path: Path, recorder: _RecordingLogger
    ) -> None:
        """``cache.db`` is disposable by contract, so it must never gate startup.

        The banner's read is the first thing in the boot to open it, and without the catch
        a truncated file (an unclean shutdown) or one owned by another uid aborts `lifespan`
        outright: uvicorn exits and the operator gets no UI to fix it from."""
        settings = _make(tmp_path, stored_destructive=None)
        (tmp_path / "cache.db").write_bytes(b"this is not a database" * 64)

        with TestClient(create_app(settings)) as client:
            assert client.get("/api/health").status_code == 200

        db = next(kw for _l, event, kw in recorder.events if event == "db.ready")
        assert db["cache_journal_mode"] == "unreadable"
        assert db["journal_mode"] == "wal"
        assert "db.cache_unreadable" in recorder.names()

    def test_every_registered_job_is_named_with_its_next_firing(
        self, tmp_path: Path, recorder: _RecordingLogger
    ) -> None:
        """ "Why did my nightly scan stop" -- a job that was never scheduled and one whose
        stored cron was skipped as malformed are otherwise indistinguishable.

        The population is pinned rather than walked for a flag, because a flag-shaped
        assertion cannot tell a job that complies from one that dropped out of the walk
        (rule 145). This fixture stores no schedules, so the set is exactly the built-in
        upkeep jobs: a stored cron would add the scan job and a stored null would remove a
        maintenance one."""
        settings = _make(tmp_path, stored_destructive=None)

        with TestClient(create_app(settings)):
            pass

        jobs = [kw for _l, event, kw in recorder.events if event == "scheduler.job"]
        assert {job["job"] for job in jobs} == {
            *scheduler.MAINTENANCE_JOB_IDS,
            scheduler.SESSION_SWEEP_JOB_ID,
            scheduler.SNAPSHOT_SWEEP_JOB_ID,
        }
        assert all(job["trigger"] for job in jobs)
        # `.get`, so a dropped field fails as a missing firing time rather than a KeyError
        # raised somewhere else entirely (rule 141).
        assert all(job.get("next_run") for job in jobs)
        assert "scheduler.no_scan_scheduled" in recorder.names()
