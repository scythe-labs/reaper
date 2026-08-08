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
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn

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
        text = conf.read_text(encoding="utf-8")
        assert "REAPER_PORT" in text
        assert "REAPER_TRAY" in text
        assert "REAPER_DOCK_ICON" in text
        # The anti-lockout switch, which this file is the only delivery route for on a
        # double-clicked app: an operator who cannot sign in cannot be told about it from
        # inside the app, so a key absent from the template does not exist for them (#433).
        assert "REAPER_RECOVERY" in text
        assert "recovery.txt" in text  # and where the code it mints will be
        env2: dict[str, str] = {}
        launcher.load_launcher_conf(env2, tmp_path / "data")
        assert env2 == {}  # the shipped template is all comments


class TestWhichInstallsReadTheConf:
    """The file is for installs nobody can hand an environment variable to, and the snap is
    one: snapd starts it at boot, ``snapcraft.yaml`` declares no configure hook, so `snap set`
    reaches nothing. It was missed, which left REAPER_RECOVERY with no route in on that
    install at all (#433, rule 72).

    Every shape is driven, not just the one that motivated the change: a flag-shaped
    assertion cannot tell a shape that complies from one that dropped out of the walk
    (rule 145). The container and a source checkout are the two that must stay OUT, and both
    are asserted rather than left to the default.
    """

    def test_a_frozen_desktop_build_reads_it(self) -> None:
        assert launcher.reads_launcher_conf({}, frozen=True) is True

    def test_the_snap_reads_it(self) -> None:
        # REAPER_HOME is what names the snap: snapcraft.yaml sets it to $SNAP.
        assert (
            launcher.reads_launcher_conf({"REAPER_HOME": "/snap/x/current"}, frozen=False) is True
        )

    def test_the_container_does_not(self) -> None:
        # A compose file IS a file of environment variables; a second one inside /data would
        # give every setting two homes. The Dockerfile sets REAPER_DATA_DIR, never REAPER_HOME.
        assert launcher.reads_launcher_conf({"REAPER_DATA_DIR": "/data"}, frozen=False) is False

    def test_a_source_checkout_does_not(self) -> None:
        assert launcher.reads_launcher_conf({}, frozen=False) is False

    def test_an_empty_home_is_not_a_snap(self) -> None:
        # `.strip()`, not truthiness on the raw value: an env var set to blank is how a
        # compose file spells "unset", and it must not switch the file on.
        assert launcher.reads_launcher_conf({"REAPER_HOME": "   "}, frozen=False) is False


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
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        launcher._say("a plain refusal", frozen=False)
        assert "a plain refusal" in capsys.readouterr().err
        assert calls == []


class TestDesktopHelpers:
    """What Settings -> General's Desktop app group stands on: the platform gate,
    the boot-resolved flag read, and the in-place launcher.conf write."""

    @pytest.mark.parametrize(
        ("platform", "frozen", "expected"),
        [
            ("darwin", True, "macos"),
            ("win32", True, "windows"),
            ("linux", True, None),  # the snap freezes nothing, but hold the line anyway
            ("darwin", False, None),  # a source run on a Mac is not the Mac app
        ],
    )
    def test_the_platform_gate(self, platform: str, frozen: bool, expected: str | None) -> None:
        assert launcher.desktop_platform(platform, frozen=frozen) == expected

    def test_a_flag_reads_the_environment_else_its_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("REAPER_TRAY", raising=False)
        assert launcher.desktop_flag("REAPER_TRAY", default=True) is True
        assert launcher.desktop_flag("REAPER_TRAY", default=False) is False
        monkeypatch.setenv("REAPER_TRAY", "false")
        assert launcher.desktop_flag("REAPER_TRAY", default=True) is False
        monkeypatch.setenv("REAPER_TRAY", "1")
        assert launcher.desktop_flag("REAPER_TRAY", default=False) is True

    def test_writing_replaces_in_place_and_appends_the_rest(self, tmp_path: Path) -> None:
        (tmp_path / "launcher.conf").write_text(
            "# a note the operator wrote\nREAPER_PORT=8421\nREAPER_TRAY=true\n",
            encoding="utf-8",
        )
        launcher.write_conf_values(tmp_path, {"REAPER_TRAY": "false", "REAPER_DOCK_ICON": "true"})
        text = (tmp_path / "launcher.conf").read_text(encoding="utf-8")
        assert text.splitlines() == [
            "# a note the operator wrote",
            "REAPER_PORT=8421",
            "REAPER_TRAY=false",
            "REAPER_DOCK_ICON=true",
        ]

    def test_a_torn_write_cannot_empty_the_operator_conf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The writer lands through a sibling and os.replace (rule 118): the reader
        accepts a truncated conf as valid, so an in-place truncate that dies mid-save
        would silently revert every operator line — REAPER_HOST's default is the
        wildcard bind."""
        original = "# a note the operator wrote\nREAPER_HOST=127.0.0.1\n"
        (tmp_path / "launcher.conf").write_text(original, encoding="utf-8")

        def torn_replace(src: object, dst: object) -> None:
            raise OSError("disk went away")

        monkeypatch.setattr("reaper.launcher.os.replace", torn_replace)
        with pytest.raises(OSError):
            launcher.write_conf_values(tmp_path, {"REAPER_TRAY": "false"})
        assert (tmp_path / "launcher.conf").read_text(encoding="utf-8") == original

    def test_a_successful_write_leaves_no_sibling_behind(self, tmp_path: Path) -> None:
        (tmp_path / "launcher.conf").write_text("REAPER_TRAY=true\n", encoding="utf-8")
        launcher.write_conf_values(tmp_path, {"REAPER_TRAY": "false"})
        assert [p.name for p in tmp_path.iterdir()] == ["launcher.conf"]

    def test_writing_into_nothing_starts_from_the_template(self, tmp_path: Path) -> None:
        """A first save must leave the same self-describing file a first launch
        writes, with the new value active at the end of it."""
        launcher.write_conf_values(tmp_path / "data", {"REAPER_DOCK_ICON": "true"})
        text = (tmp_path / "data" / "launcher.conf").read_text(encoding="utf-8")
        assert text.startswith("# Reaper reads this file when it starts.")
        assert text.rstrip().endswith("REAPER_DOCK_ICON=true")
        # The round trip: what was written is what the next boot's read applies.
        env: dict[str, str] = {}
        launcher.load_launcher_conf(env, tmp_path / "data")
        assert env == {"REAPER_DOCK_ICON": "true"}


class TestTrayChoice:
    @pytest.mark.parametrize(
        ("platform", "configured", "frozen", "expected"),
        [
            ("darwin", None, True, True),  # the .app would otherwise be invisible
            ("win32", None, True, True),
            ("linux", None, True, False),  # the snap is a service snapd already shows
            ("darwin", None, False, False),  # source runs stay plain
            ("darwin", "false", True, False),
            ("win32", "1", False, True),  # a dev run can opt in while testing
        ],
    )
    def test_the_default_follows_the_install_shape(
        self, platform: str, configured: str | None, frozen: bool, expected: bool
    ) -> None:
        env = {} if configured is None else {"REAPER_TRAY": configured}
        assert launcher._tray_wanted(platform, env, frozen=frozen) is expected

    def test_both_launch_shapes_read_one_declaration(self) -> None:
        """rule 104: uvicorn.run and the tray path's Config spread this one dict, so
        proxy_headers=False cannot drift between them."""
        assert launcher._serve_kwargs("10.0.0.5", 8437) == {
            "factory": True,
            "host": "10.0.0.5",
            "port": 8437,
            "proxy_headers": False,
        }


class _FakeMenuItem:
    def __init__(self, label: str, action: Any, default: bool = False) -> None:
        self.label = label
        self.action = action
        self.default = default


class _FakeIcon:
    """pystray's contract: ``run()`` blocks its caller until ``stop()``, and the
    setup callback runs on a thread pystray starts once the icon is ready."""

    def __init__(self, name: str, icon: Any = None, title: str = "", menu: Any = ()) -> None:
        self.menu = menu
        self.visible = False
        self.stopped = threading.Event()

    def run(self, setup: Any) -> None:
        runner = threading.Thread(target=setup, args=(self,))
        runner.start()
        assert self.stopped.wait(timeout=10), "the tray loop was never stopped"
        runner.join(timeout=10)

    def stop(self) -> None:
        self.stopped.set()


class _FakeServer(uvicorn.Server):
    """uvicorn's seam: ``run()`` blocks until ``should_exit``, or dies with the
    scripted error. Self-terminating, so a failed assertion cannot hang the suite.

    Inherits the real `Server` so `_serve_with_tray`'s parameter type holds, and skips its
    `__init__` so no `Config` is built and nothing binds a port.
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.should_exit = False
        self.running = threading.Event()
        self._error = error

    def run(self, sockets: list[socket.socket] | None = None) -> None:
        self.running.set()
        if self._error is not None:
            raise self._error
        deadline = time.monotonic() + 30
        while not self.should_exit and time.monotonic() < deadline:
            time.sleep(0.005)


class TestServeWithTray:
    def _module(self) -> tuple[Any, list[_FakeIcon]]:
        created: list[_FakeIcon] = []

        def icon(*args: Any, **kwargs: Any) -> _FakeIcon:
            made = _FakeIcon(*args, **kwargs)
            created.append(made)
            return made

        return SimpleNamespace(Icon=icon, Menu=lambda *i: i, MenuItem=_FakeMenuItem), created

    def _item(self, created: list[_FakeIcon], label: str) -> _FakeMenuItem:
        deadline = time.monotonic() + 10
        while not created and time.monotonic() < deadline:
            time.sleep(0.005)
        assert created, "the icon was never built"
        found: _FakeMenuItem = next(i for i in created[0].menu if i.label == label)
        return found

    def test_quit_stops_the_server_then_the_icon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The quit path of #431: the menu only sets should_exit (uvicorn's graceful
        stop); the watcher sees the worker end and takes the icon down after it."""
        docked: list[bool] = []
        monkeypatch.setattr(launcher, "_show_dock_icon", lambda: docked.append(True))
        module, created = self._module()
        server = _FakeServer()

        def press_quit() -> None:
            assert server.running.wait(timeout=10)
            self._item(created, "Quit Reaper").action()

        presser = threading.Thread(target=press_quit)
        presser.start()
        error = launcher._serve_with_tray(module, server, 8437, object(), dock_icon=False)
        presser.join(timeout=10)
        assert error is None
        assert server.should_exit is True
        assert created[0].stopped.is_set()
        assert created[0].visible is True
        assert docked == []  # the Dock stays hidden unless asked for

    def test_a_dying_server_takes_the_icon_down_and_is_reported(self) -> None:
        """A server that cannot come up must not leave a live-looking icon behind,
        and the caller needs the failure to say something (a windowed build has no
        stderr anyone reads)."""
        module, created = self._module()
        boom = RuntimeError("bind failed")
        error = launcher._serve_with_tray(
            module, _FakeServer(error=boom), 8437, object(), dock_icon=False
        )
        assert error is boom
        assert created[0].stopped.is_set()

    def test_open_reaper_opens_the_local_url_and_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """default=True is what makes a double-click on the Windows tray icon open
        the UI rather than only the right-click menu."""
        opened: list[str] = []
        monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
        module, created = self._module()
        server = _FakeServer()

        def drive() -> None:
            assert server.running.wait(timeout=10)
            item = self._item(created, "Open Reaper")
            assert item.default is True
            item.action()
            self._item(created, "Quit Reaper").action()

        driver = threading.Thread(target=drive)
        driver.start()
        launcher._serve_with_tray(module, server, 8437, object(), dock_icon=False)
        driver.join(timeout=10)
        assert opened == ["http://127.0.0.1:8437"]

    def test_the_dock_icon_returns_only_when_asked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        docked: list[bool] = []
        monkeypatch.setattr(launcher, "_show_dock_icon", lambda: docked.append(True))
        module, created = self._module()
        server = _FakeServer()

        def press_quit() -> None:
            assert server.running.wait(timeout=10)
            self._item(created, "Quit Reaper").action()

        presser = threading.Thread(target=press_quit)
        presser.start()
        launcher._serve_with_tray(module, server, 8437, object(), dock_icon=True)
        presser.join(timeout=10)
        assert docked == [True]


class TestMain:
    @pytest.fixture
    def serve(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
        """Stub every boundary main() crosses and record what reaches the serve call."""
        captured: dict[str, Any] = {"migrated": False, "preflight_code": 0, "preflighted": False}

        def fake_run(*args: Any, **kwargs: Any) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

        def fake_preflight() -> int:
            captured["preflighted"] = True
            return int(captured["preflight_code"])

        import uvicorn

        monkeypatch.setattr(uvicorn, "run", fake_run)
        monkeypatch.setattr("reaper.preflight.main", fake_preflight)
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
        a message that names the port is the only honest outcome — and the refusal
        lands before preflight or migrations, because preflight applies a staged
        restore by renaming the live database files aside, which on macOS/Linux
        succeeds under the running copy's open handles."""
        monkeypatch.setattr(launcher, "_loopback_occupied", lambda port: True)
        said: list[str] = []
        monkeypatch.setattr(launcher, "_say", lambda m, *, frozen: said.append(m))
        with pytest.raises(SystemExit) as excinfo:
            launcher.main()
        assert excinfo.value.code == 2
        assert "8420" in said[0]
        assert serve["preflighted"] is False  # nothing on disk was touched
        assert serve["migrated"] is False
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

    @pytest.fixture
    def tray(self, serve: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Route main() down the tray path with every boundary captured."""
        import uvicorn

        captured: dict[str, Any] = {"tray_error": None}

        class FakeConfig:
            def __init__(self, app: Any, **kwargs: Any) -> None:
                captured["app"] = app
                captured["config_kwargs"] = kwargs

        monkeypatch.setattr(uvicorn, "Config", FakeConfig)
        monkeypatch.setattr(uvicorn, "Server", lambda config: SimpleNamespace(config=config))
        monkeypatch.setattr(launcher, "_tray_wanted", lambda *a, **k: True)
        monkeypatch.setattr(launcher, "_tray_backend", lambda: SimpleNamespace())
        monkeypatch.setattr(launcher, "_tray_image", lambda: object())

        def fake_tray(
            mod: Any, server: Any, port: int, image: Any, *, dock_icon: bool
        ) -> BaseException | None:
            captured["tray_port"] = port
            captured["dock_icon"] = dock_icon
            error: BaseException | None = captured["tray_error"]
            return error

        monkeypatch.setattr(launcher, "_serve_with_tray", fake_tray)
        return captured

    def test_the_tray_path_carries_the_same_serve_kwargs(
        self, serve: dict[str, Any], tray: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The second programmatic launch site (rule 72's sibling of the uvicorn.run
        call): its Config must pin proxy_headers=False exactly as the plain path
        does. The port is non-default so an argument dropped on the way to Config
        cannot hide behind the default (rule 141)."""
        monkeypatch.setenv("REAPER_PORT", "8437")
        from reaper.main import create_app

        launcher.main()
        assert tray["app"] is create_app
        assert tray["config_kwargs"].get("proxy_headers") is False
        assert tray["config_kwargs"].get("factory") is True
        assert tray["config_kwargs"].get("port") == 8437
        assert tray["tray_port"] == 8437
        assert "kwargs" not in serve  # the plain uvicorn.run path was not taken

    def test_a_tray_serve_failure_is_said_out_loud(
        self, serve: dict[str, Any], tray: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A windowed build whose icon just vanished has no stderr anyone reads; the
        dialog is the one signal left."""
        tray["tray_error"] = RuntimeError("bind failed")
        said: list[str] = []
        monkeypatch.setattr(launcher, "_say", lambda m, *, frozen: said.append(m))
        with pytest.raises(SystemExit) as excinfo:
            launcher.main()
        assert excinfo.value.code == 1
        assert said and "stopped" in said[0]

    def test_without_a_backend_the_plain_path_still_serves(
        self, serve: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bundle that somehow lost pystray serves without an icon rather than
        refusing: the server itself is fine, and the operator can still reach it."""
        monkeypatch.setattr(launcher, "_tray_wanted", lambda *a, **k: True)
        monkeypatch.setattr(launcher, "_tray_backend", lambda: None)
        launcher.main()
        assert serve["kwargs"].get("proxy_headers") is False
        assert serve["kwargs"].get("factory") is True


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
