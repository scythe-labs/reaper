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
import socket
import sys
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


class TestLauncherConf:
    def test_values_reach_the_environment_and_real_env_wins(self, tmp_path: Path) -> None:
        (tmp_path / "launcher.conf").write_text(
            "# a comment\nREAPER_PORT=8421\nREAPER_HOST = 127.0.0.1\nPATH=/evil\nnot a line\n",
            encoding="utf-8",
        )
        env = {"REAPER_HOST": "10.0.0.5"}
        launcher.load_launcher_conf(env, tmp_path)
        assert env["REAPER_PORT"] == "8421"
        assert env["REAPER_HOST"] == "10.0.0.5"  # the real environment outranks the file
        assert "PATH" not in env  # only REAPER_ keys are honored

    def test_a_missing_file_is_written_as_a_commented_template(self, tmp_path: Path) -> None:
        """The operator edits rather than guesses: first run leaves the file with every
        offered key present but commented, so it changes nothing until touched."""
        env: dict[str, str] = {}
        conf = launcher.load_launcher_conf(env, tmp_path / "data")
        assert conf.exists()
        assert env == {}
        assert "REAPER_PORT" in conf.read_text(encoding="utf-8")
        env2: dict[str, str] = {}
        launcher.load_launcher_conf(env2, tmp_path / "data")
        assert env2 == {}  # the shipped template is all comments


class TestLoopbackGuard:
    def test_a_listening_port_reads_as_occupied(self) -> None:
        with socket.socket() as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            port = held.getsockname()[1]
            assert launcher._loopback_occupied(port) is True

    def test_a_free_port_reads_as_free(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        assert launcher._loopback_occupied(port) is False

    def test_the_refusal_is_stderr_only_from_source(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The dialog is for the double-clicked binary that has no readable stderr;
        a source run must never pop native UI."""
        calls: list[object] = []
        monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: calls.append(a))
        launcher._say("a plain refusal", frozen=False)
        assert "a plain refusal" in capsys.readouterr().err
        assert calls == []


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
        monkeypatch.setattr("reaper.preflight.main", lambda: int(captured["preflight_code"]))
        monkeypatch.setattr(
            "reaper.launcher._migrate",
            lambda root: captured.__setitem__("migrated", True),
        )
        monkeypatch.setenv("REAPER_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("REAPER_LAUNCH_BROWSER", "false")
        # Pinned free: the real check reads this machine's ports, and the suite must
        # not fail because a dev server happens to hold 8420 (rule 119). The occupied
        # branch pins the opposite explicitly.
        monkeypatch.setattr(launcher, "_loopback_occupied", lambda port: False)
        monkeypatch.delenv("REAPER_HOST", raising=False)
        monkeypatch.delenv("REAPER_PORT", raising=False)
        # main() reads these through export_buildinfo/install_root; a machine that
        # happens to set them would change what this suite proves (rule 133).
        monkeypatch.delenv("REAPER_HOME", raising=False)
        monkeypatch.delenv("REAPER_BUILDINFO", raising=False)
        return captured

    def test_the_serve_call_never_trusts_forwarded_headers(self, serve: dict[str, Any]) -> None:
        """proxy_headers=False is the programmatic --no-proxy-headers: peer trust is
        decided by reaper.auth.proxy alone, never a header rewritten above it."""
        from reaper.main import create_app

        launcher.main()
        assert serve["args"] == (create_app,)
        assert serve["kwargs"].get("factory") is True
        assert serve["kwargs"].get("proxy_headers") is False
        assert serve["kwargs"].get("port") == 8420
        assert serve["migrated"] is True

    def test_an_install_without_migrations_stops_before_touching_disk(
        self, serve: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A plain `pip install reaper` puts reaper-server on PATH with no alembic/
        beside it; continuing would create a data folder nothing can bring current.
        The refusal lands before preflight, so not even the folder is created."""
        monkeypatch.setenv("REAPER_HOME", str(tmp_path))  # a root with no alembic/
        with pytest.raises(SystemExit) as excinfo:
            launcher.main()
        assert excinfo.value.code == 2
        assert serve["migrated"] is False
        assert "kwargs" not in serve

    def test_an_occupied_loopback_refuses_instead_of_hiding(
        self, serve: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another process answering 127.0.0.1 on our port means the browser would
        open onto the WRONG server (a wildcard bind can still succeed beside it, so
        uvicorn would come up and nobody would ever see this install). Refusing with
        a message that names the port is the only honest outcome."""
        monkeypatch.setattr(launcher, "_loopback_occupied", lambda port: True)
        said: list[str] = []
        monkeypatch.setattr(launcher, "_say", lambda m, *, frozen: said.append(m))
        with pytest.raises(SystemExit) as excinfo:
            launcher.main()
        assert excinfo.value.code == 2
        assert "8420" in said[0]
        assert "kwargs" not in serve  # uvicorn was never asked to serve

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


class TestResolveDataDir:
    def test_frozen_without_a_choice_gets_the_platform_folder(self) -> None:
        env: dict[str, str] = {}
        launcher._resolve_data_dir(env, frozen=True)
        assert env["REAPER_DATA_DIR"] == str(launcher.default_data_dir(sys.platform, env))

    def test_an_operator_choice_is_never_overridden(self) -> None:
        env = {"REAPER_DATA_DIR": "/somewhere/else"}
        launcher._resolve_data_dir(env, frozen=True)
        assert env["REAPER_DATA_DIR"] == "/somewhere/else"

    def test_a_source_run_keeps_the_repo_relative_default(self) -> None:
        env: dict[str, str] = {}
        launcher._resolve_data_dir(env, frozen=False)
        assert "REAPER_DATA_DIR" not in env


class TestBuildinfoPath:
    def test_a_named_path_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("REAPER_BUILDINFO", str(tmp_path / "info.json"))
        assert launcher._buildinfo_path() == tmp_path / "info.json"

    def test_the_install_root_is_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("REAPER_BUILDINFO", raising=False)
        monkeypatch.setenv("REAPER_HOME", str(tmp_path))
        assert launcher._buildinfo_path() == tmp_path / "buildinfo.json"

    def test_a_source_checkout_has_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REAPER_BUILDINFO", raising=False)
        monkeypatch.delenv("REAPER_HOME", raising=False)
        assert launcher._buildinfo_path() is None
