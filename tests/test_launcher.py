# SPDX-License-Identifier: AGPL-3.0-or-later
"""The packaged-install launcher: data-dir choice, provenance export, and the serve call.

The serve call's ``proxy_headers=False`` is pinned here because the hygiene walk that
proves ``--no-proxy-headers`` on every CLI launch cannot see a programmatic
``uvicorn.run`` -- this test is that walk's counterpart for the one launch site that
is a function call (rule 118: the interlock is tested directly, and rule 141: the
captured value is asserted, not just the call).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reaper import launcher


class TestDefaultDataDir:
    def test_windows_uses_localappdata_when_set(self) -> None:
        chosen = launcher.default_data_dir("win32", {"LOCALAPPDATA": "C:\\Users\\op\\Local"})
        assert chosen == Path("C:\\Users\\op\\Local") / "Reaper"

    def test_windows_falls_back_under_home(self) -> None:
        chosen = launcher.default_data_dir("win32", {})
        assert chosen == Path.home() / "AppData" / "Local" / "Reaper"

    def test_macos_uses_application_support(self) -> None:
        chosen = launcher.default_data_dir("darwin", {})
        assert chosen == Path.home() / "Library" / "Application Support" / "Reaper"

    def test_linux_honors_xdg_data_home(self, tmp_path: Path) -> None:
        chosen = launcher.default_data_dir("linux", {"XDG_DATA_HOME": str(tmp_path)})
        assert chosen == tmp_path / "reaper"

    def test_linux_falls_back_to_local_share(self) -> None:
        chosen = launcher.default_data_dir("linux", {})
        assert chosen == Path.home() / ".local" / "share" / "reaper"


class TestExportBuildinfo:
    def _write(self, tmp_path: Path, payload: object) -> Path:
        path = tmp_path / "buildinfo.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_values_are_seeded(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            {"version": "2026.8.1", "commit": "abc1234", "release": "1", "repo": "owner/name"},
        )
        env: dict[str, str] = {}
        launcher.export_buildinfo(env, path)
        assert env == {
            "REAPER_VERSION": "2026.8.1",
            "REAPER_GIT_SHA": "abc1234",
            "REAPER_RELEASE": "1",
            "REAPER_UPDATE_REPO": "owner/name",
        }

    def test_an_operator_env_value_wins(self, tmp_path: Path) -> None:
        """Baked provenance behaves like the container's image env: a value the
        operator set outranks it, exactly as `docker run -e` outranks `ENV`."""
        path = self._write(tmp_path, {"version": "2026.8.1"})
        env = {"REAPER_VERSION": "2026.9.9"}
        launcher.export_buildinfo(env, path)
        assert env["REAPER_VERSION"] == "2026.9.9"

    @pytest.mark.parametrize("payload", ["not-a-dict", {"version": 3}, {"version": "  "}])
    def test_junk_is_ignored(self, tmp_path: Path, payload: object) -> None:
        path = self._write(tmp_path, payload)
        env: dict[str, str] = {}
        launcher.export_buildinfo(env, path)
        assert env == {}

    def test_a_missing_file_is_a_plain_dev_run(self, tmp_path: Path) -> None:
        env: dict[str, str] = {}
        launcher.export_buildinfo(env, tmp_path / "absent.json")
        assert env == {}


class TestBrowserChoice:
    @pytest.mark.parametrize(
        ("configured", "frozen", "expected"),
        [
            (None, True, True),  # a double-click gives no other signal it worked
            (None, False, False),  # snap daemons and source runs stay quiet
            ("false", True, False),
            ("1", False, True),
        ],
    )
    def test_the_default_follows_the_install_shape(
        self, configured: str | None, frozen: bool, expected: bool
    ) -> None:
        env = {} if configured is None else {"REAPER_LAUNCH_BROWSER": configured}
        assert launcher._browser_wanted(env, frozen=frozen) is expected


class TestPort:
    def test_the_default_port_matches_the_container(self) -> None:
        assert launcher._port({}) == 8420

    def test_a_configured_port_is_used(self) -> None:
        assert launcher._port({"REAPER_PORT": "9000"}) == 9000

    def test_garbage_stops_with_a_plain_message(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            launcher._port({"REAPER_PORT": "eighty"})
        assert excinfo.value.code == 2


class TestMain:
    @pytest.fixture
    def serve(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
        """Stub every boundary main() crosses and record what reaches the serve call."""
        captured: dict[str, Any] = {"migrated": False, "preflight_code": 0}

        def fake_run(*args: Any, **kwargs: Any) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

        import uvicorn

        monkeypatch.setattr(uvicorn, "run", fake_run)
        monkeypatch.setattr(
            "reaper.preflight.main", lambda: int(captured["preflight_code"])
        )
        monkeypatch.setattr(
            "reaper.launcher._migrate",
            lambda root: captured.__setitem__("migrated", True),
        )
        monkeypatch.setenv("REAPER_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("REAPER_LAUNCH_BROWSER", "false")
        monkeypatch.delenv("REAPER_HOST", raising=False)
        monkeypatch.delenv("REAPER_PORT", raising=False)
        return captured

    def test_the_serve_call_never_trusts_forwarded_headers(self, serve: dict[str, Any]) -> None:
        """proxy_headers=False is the programmatic --no-proxy-headers: peer trust is
        decided by reaper.auth.proxy alone, never a header rewritten above it."""
        launcher.main()
        assert serve["args"] == ("reaper.main:create_app",)
        assert serve["kwargs"].get("factory") is True
        assert serve["kwargs"].get("proxy_headers") is False
        assert serve["kwargs"].get("port") == 8420
        assert serve["migrated"] is True

    def test_a_failed_preflight_stops_before_migrations_or_serving(
        self, serve: dict[str, Any]
    ) -> None:
        """The same order the container entrypoint enforces: an unwritable data folder
        stops the process before a half-migrated schema can exist."""
        serve["preflight_code"] = 1
        with pytest.raises(SystemExit) as excinfo:
            launcher.main()
        assert excinfo.value.code == 1
        assert serve["migrated"] is False
        assert "kwargs" not in serve
