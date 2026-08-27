# SPDX-License-Identifier: AGPL-3.0-or-later
"""The packaged-install entry point: one process that prepares, then serves.

The container has ``docker-entrypoint.sh``. The Windows and macOS binaries and
the snap have this, in the same order for the same reasons: pick a writable
data folder, verify it (preflight, which also applies a staged restore), apply
migrations, then serve. A half-migrated schema must never serve traffic for a
tool that deletes media.

Provenance rides along as ``buildinfo.json``: a frozen bundle carries it next
to the executable, the snap's copy is found through ``REAPER_HOME``
(``REAPER_BUILDINFO`` exists to name a path outright when neither applies),
and its values are exported into the environment (never overriding one the
operator set) before anything reads them. :mod:`reaper.buildinfo` and the
update check read only the environment, so every install shape answers the
same way.

The data folder default is per-platform only when frozen. Run from a source
checkout, this launcher keeps the repo-relative ``data/`` every other dev
entry point uses.

``launcher.conf`` in the data folder is how an install that cannot be handed
an environment variable is configured at all: the frozen desktop builds,
which are double-clicked, and the snap, which is a daemon with no configure
hook. See :func:`reads_launcher_conf` for which shapes read it and why the
container does not.

The frozen desktop builds also keep a menu-bar (macOS) or tray (Windows) icon
while the server runs, with Open Reaper and Quit, so a windowed build is
never an invisible process. The icon owns the main thread, which AppKit
requires, and uvicorn serves from a worker thread. ``launcher.conf`` turns the
icon off (``REAPER_TRAY``) or puts the macOS Dock icon back beside it
(``REAPER_DOCK_ICON``).
"""

from __future__ import annotations

import contextlib
import json
import locale
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast

from reaper.buildinfo import env_flag, frozen_bundle, install_root, project_root
from reaper.config import LAUNCHER_CONF_NAME, Settings, configured_env
from reaper.i18n import say

if TYPE_CHECKING:
    import uvicorn


#: buildinfo.json key -> the environment value it seeds. Operator-set values win:
#: everything is ``setdefault``, so an env override behaves exactly as in the container.
_BUILDINFO_KEYS = {
    "version": "REAPER_VERSION",
    "commit": "REAPER_GIT_SHA",
    "release": "REAPER_RELEASE",
    "repo": "REAPER_UPDATE_REPO",
}


def _bundle_root() -> Path | None:
    """The unpacked PyInstaller bundle, or ``None`` when running from source."""
    return frozen_bundle()


def export_buildinfo(env: MutableMapping[str, str], path: Path | None) -> None:
    """Seed provenance env values from ``buildinfo.json``, if one is present.

    A missing or unreadable file is a plain dev run, never an error: everything then
    reports ``dev`` exactly as a source checkout does.
    """
    if path is None:
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    for key, name in _BUILDINFO_KEYS.items():
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            env.setdefault(name, value.strip())


def _buildinfo_path() -> Path | None:
    named = os.environ.get("REAPER_BUILDINFO", "").strip()
    if named:
        return Path(named)
    root = install_root()
    return root / "buildinfo.json" if root else None


def default_data_dir(platform: str, env: MutableMapping[str, str]) -> Path:
    """Where a packaged install keeps its data, per platform convention.

    The same folder every run, whatever directory the operator launched from. A
    relative default under a double-clicked binary would scatter databases across
    whichever folder each launch happened to start in.
    """
    home = Path.home()
    if platform == "win32":
        base = env.get("LOCALAPPDATA", "").strip()
        return (Path(base) if base else home / "AppData" / "Local") / "Reaper"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Reaper"
    base = env.get("XDG_DATA_HOME", "").strip()
    return (Path(base) if base else home / ".local" / "share") / "reaper"


def _resolve_data_dir(env: MutableMapping[str, str], *, frozen: bool) -> None:
    """Pin ``REAPER_DATA_DIR`` before the first settings read, frozen installs only.

    Set as an env value rather than passed inward, so every later reader
    (preflight, alembic's own ``get_settings`` call, the app) resolves the same
    folder.
    """
    if frozen and not env.get("REAPER_DATA_DIR", "").strip():
        env["REAPER_DATA_DIR"] = str(default_data_dir(sys.platform, env))


#: What the template written on first run offers. Only REAPER_ keys are
#: honored on read, so the file cannot reach PATH or anything else the
#: process inherits.
#:
#: REAPER_RECOVERY is here because this file is the only way a double-clicked
#: app receives it, and an operator who cannot sign in cannot be told about it
#: from inside the app. A key nobody knows to type does not exist for them, so
#: its comment says what turning it on does, since the console that used to
#: say so is not there either. Every line is also enumerated in prose on the
#: Windows and macOS install pages. Update those pages too when this changes.
_CONF_TEMPLATE = """\
# Reaper reads this file when it starts. One setting per line; # starts a comment.
# Remove the leading # to use a line. Real environment variables still win.
#
# REAPER_PORT=8421
# REAPER_HOST=0.0.0.0
# REAPER_LAUNCH_BROWSER=false
# REAPER_UPDATE_CHECK=false
# REAPER_TRAY=false
# REAPER_DOCK_ICON=true
#
# Locked out? Set this to true and restart. Reaper writes a single-use sign-in code
# to recovery.txt, in this folder. Set it back to false and restart afterwards.
# REAPER_RECOVERY=true
"""


def _write_text_atomic(path: Path, text: str) -> None:
    """Write through a same-directory sibling and ``os.replace``, atomic on macOS,
    Linux, and NTFS. ``write_text`` truncates before the new bytes land, and the
    conf reader treats a truncated file as valid, so a crash mid-save would
    silently revert every operator line to the defaults. REAPER_HOST's default is
    the wildcard bind."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def load_launcher_conf(env: MutableMapping[str, str], data_dir: Path) -> Path:
    """Read ``launcher.conf`` from the data folder into the environment.

    A double-clicked app has no way to receive an environment variable, so this
    file is how a packaged install is configured at all. Precedence matches the
    container: a real environment value wins over the file (``setdefault``), and
    the file wins over the defaults. Only ``REAPER_``-prefixed keys are honored. A
    missing file is written as a commented template, so the operator edits rather
    than guesses. A file that cannot be read or written is skipped, never fatal.
    """
    conf = data_dir / LAUNCHER_CONF_NAME
    try:
        if not conf.exists():
            data_dir.mkdir(parents=True, exist_ok=True)
            _write_text_atomic(conf, _CONF_TEMPLATE)
            return conf
        for line in conf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("REAPER_") and value.strip():
                env.setdefault(key, value.strip())
    except OSError:
        pass
    return conf


#: The two launcher.conf keys Settings -> General edits on a desktop install.
DESKTOP_TRAY_KEY = "REAPER_TRAY"
DESKTOP_DOCK_KEY = "REAPER_DOCK_ICON"


def desktop_platform(platform: str | None = None, *, frozen: bool | None = None) -> str | None:
    """``"macos"`` or ``"windows"`` when this process is a frozen desktop build,
    else ``None``. What gates the Desktop app group in Settings -> General: the
    container, the snap, and a source run never see it."""
    if frozen is None:
        frozen = _bundle_root() is not None
    if not frozen:
        return None
    resolved = sys.platform if platform is None else platform
    if resolved == "darwin":
        return "macos"
    if resolved == "win32":
        return "windows"
    return None


def write_conf_values(data_dir: Path, values: MutableMapping[str, str]) -> None:
    """Set keys in ``launcher.conf``, preserving everything else the operator wrote.

    An active ``KEY=`` line is rewritten in place. A key with no active line is
    appended. The file is how a double-clicked install is configured, so Settings
    edits it rather than inventing a second store the launcher would not read:
    one declaration, here the file itself. Raises ``OSError`` for the caller to
    turn into a plain refusal, since a settings save must not half-apply silently.
    """
    conf = data_dir / LAUNCHER_CONF_NAME
    text = conf.read_text(encoding="utf-8") if conf.exists() else _CONF_TEMPLATE
    lines = text.splitlines()
    remaining = dict(values)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip()
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    data_dir.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(conf, "\n".join(lines) + "\n")


def _migrate(root: Path) -> None:
    """``alembic upgrade head``, addressed into this install's copy of ``alembic/``.

    Programmatic because a bundle has no ``alembic.ini`` on a search path. The
    config is built here, and ``alembic/env.py`` reads the database URL from
    settings as it always does. ``config_file_name`` stays unset, which env.py
    treats as "skip logging config", since uvicorn owns logging in this process.
    """
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(config, "head")


def _settings_default(field: str) -> str:
    """One declared default, read off the model instead of being spelled a second
    time. Reading it from ``config.Settings`` keeps the port this launcher binds
    and the port `main.py` prints in the anti-lockout recovery link as one value,
    never two copies that could quietly drift apart.
    """
    return str(Settings.model_fields[field].default)


def _port(env: Mapping[str, str], *, frozen: bool) -> int:
    """The port to bind, refusing a value that is not one.

    Reports the refusal through ``_say``, not ``sys.stderr`` directly: on a
    desktop build this value comes from ``launcher.conf``, the one file that
    shape is configured through, and a windowed build's stderr is written to the
    null device, so a stderr-only refusal there would never be seen. ``frozen``
    is required rather than defaulted, so a call site cannot silently take the
    console-only path.
    """
    raw = env.get("REAPER_PORT", "").strip() or _settings_default("port")
    try:
        return int(raw)
    except ValueError:
        _say(f"REAPER_PORT must be a port number; it is set to {raw!r}.", frozen=frozen)
        raise SystemExit(2) from None


def _host(env: Mapping[str, str]) -> str:
    return env.get("REAPER_HOST", "").strip() or _settings_default("host")


def _loopback_occupied(port: int) -> bool:
    """Whether something already answers on the loopback address the browser will
    be sent to. Reaper has not started yet, so any answer is another process,
    commonly another Reaper or the dev server. Starting anyway would hide this
    install behind it: the OS routes loopback connections to the most specific
    listener, so this process's wildcard bind can succeed while 127.0.0.1 stays
    someone else's, and the browser then opens onto the wrong server.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _os_locale_tag() -> str:
    """The OS's language as a bare BCP 47 primary tag (``es_ES`` -> ``es``), or
    ``"en"`` when the OS reports nothing readable.

    A double-clicked build has no browser language to read and no Settings page
    open yet, so the OS locale is the only signal this early. `reaper.i18n.say`
    already falls back to English for a tag with no shipped catalog, so a region
    this build has no catalog for (``es_MX`` when only ``es`` ships) still finds
    it: only the language subtag is kept."""
    try:
        raw = locale.getlocale()[0]
    except ValueError:
        raw = None
    if not raw:
        return "en"
    tag = raw.replace("_", "-").split("-")[0].strip().lower()
    return tag or "en"


def _say(message: str, *, frozen: bool) -> None:
    """Put a refusal where this install's operator can see it: stderr always, and
    a native dialog when frozen, because a double-clicked windowed app has no
    stderr anyone reads. The dialog is best effort. The refusal itself never
    depends on it."""
    sys.stderr.write(message + "\n")
    if not frozen:
        return
    if sys.platform == "darwin":
        with contextlib.suppress(Exception):
            # S603/S607: fixed argv, no shell, and osascript resolves from the
            # system PATH by design. Hardcoding /usr/bin would break nothing
            # and prove less.
            subprocess.run(  # noqa: S603
                ["osascript", "-e", f'display alert "Reaper" message {json.dumps(message)}'],  # noqa: S607
                check=False,
                timeout=30,
            )
    elif sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "Reaper", 0x10)


def _browser_wanted(env: Mapping[str, str], *, frozen: bool) -> bool:
    """Open the operator's browser once the server is up? On for a frozen binary
    (a double-click gives no other signal it worked), off everywhere else. The
    ``REAPER_LAUNCH_BROWSER`` env value overrides either default."""
    return env_flag("REAPER_LAUNCH_BROWSER", default=frozen, env=env)


def _open_browser_when_up(port: int) -> None:
    """From a daemon thread: wait for the local health probe, then open the UI.

    The poll is stdlib urllib against this process's own loopback port, the same
    probe the container HEALTHCHECK runs. It is a sanctioned exception to the
    rule that all outbound HTTP goes through the client layer (see
    ``.claude/rules/backend.md``): the request asks Reaper itself, carries no
    credentials, and can mutate nothing. Give up silently after a minute. The
    server log is the fallback signal, and a browser that never opens must not
    stop a server that is otherwise fine.
    """

    def poll() -> None:
        deadline = time.monotonic() + 60
        url = f"http://127.0.0.1:{port}/api/health"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    if response.status == 200:
                        webbrowser.open(f"http://127.0.0.1:{port}")
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.5)

    threading.Thread(target=poll, name="reaper-open-browser", daemon=True).start()


def _serve_kwargs(host: str, port: int) -> dict[str, Any]:
    """The one declaration both launch shapes read: the plain ``uvicorn.run`` and
    the tray path's ``uvicorn.Config`` must not drift apart.

    ``proxy_headers=False`` is the same load-bearing choice as the container
    CMD's ``--no-proxy-headers``: ``reaper.auth.proxy`` alone decides peer trust,
    never a forwarded header rewritten one layer above it. ``test_launcher`` pins
    it on both launch shapes.
    """
    return {"factory": True, "host": host, "port": port, "proxy_headers": False}


def _tray_wanted(platform: str, env: Mapping[str, str], *, frozen: bool) -> bool:
    """An icon whenever this install would otherwise be invisible: the frozen
    desktop builds. ``REAPER_TRAY`` overrides either way (a source run can opt in
    while testing). Platforms without a tray to sit in never get one, since the
    snap is a service snapd already shows."""
    if platform not in ("win32", "darwin"):
        return False
    return env_flag(DESKTOP_TRAY_KEY, default=frozen, env=env)


def _tray_backend() -> ModuleType | None:
    """pystray, or ``None`` where the ``package`` extra is not installed (a source
    run). A frozen desktop build always carries it. Serving without the icon is
    still the right failure for a bundle that somehow lost it."""
    try:
        import pystray
    except ImportError:
        return None
    return cast("ModuleType", pystray)


def _tray_image() -> Any | None:
    """The committed brand image, from the served SPA's own assets: Vite copies
    ``frontend/public/`` into ``dist/``, and the bundle carries ``dist``. A source
    checkout without a built SPA falls back to ``public/`` itself."""
    try:
        from PIL import Image
    except ImportError:
        return None
    root = project_root()
    for candidate in (
        root / "frontend" / "dist" / "icon-512.png",
        root / "frontend" / "public" / "icon-512.png",
    ):
        if candidate.is_file():
            return Image.open(candidate)
    return None


def _show_dock_icon() -> None:
    """Put the Dock icon back beside the menu-bar icon (``REAPER_DOCK_ICON=true``).

    The .app ships ``LSUIElement`` true, the standard shape for a menu-bar
    server, so the Dock icon is opt-in, restored by flipping the live process
    back to a regular app. Best effort: AppKit ships only in the frozen macOS
    build, and a Dock icon that cannot be shown must not stop the server."""
    with contextlib.suppress(Exception):
        from AppKit import NSApplication, NSApplicationActivationPolicyRegular

        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyRegular)


def _serve_with_tray(
    pystray_mod: ModuleType,
    server: uvicorn.Server,
    port: int,
    image: Any,
    *,
    dock_icon: bool,
    tag: str = "en",
) -> BaseException | None:
    """Give the main thread to the icon and serve from a worker thread.

    The macOS status item must own the main thread (AppKit), so uvicorn runs on a
    worker via ``Server.run()``, which skips signal handlers off the main thread.
    Quit only sets ``server.should_exit``, the graceful stop uvicorn honors
    between requests. The watcher joins the worker and then stops the icon, so
    the icon also leaves when the server dies on its own. Returns what killed the
    worker, or ``None`` for a clean quit.

    ``tag`` names the tray's own language (``_os_locale_tag``), independent of anything
    the browser later picks.
    """
    failure: list[BaseException] = []

    def serve() -> None:
        try:
            server.run()
        except BaseException as exc:  # carried to the caller below, never swallowed
            failure.append(exc)

    worker = threading.Thread(target=serve, name="reaper-serve")
    worker.start()

    def open_ui(icon: Any = None, item: Any = None) -> None:
        webbrowser.open(f"http://127.0.0.1:{port}")

    def quit_(icon: Any = None, item: Any = None) -> None:
        server.should_exit = True

    icon = pystray_mod.Icon(
        "Reaper",
        icon=image,
        title=say("launcher.tray.tooltip", tag),
        menu=pystray_mod.Menu(
            pystray_mod.MenuItem(say("launcher.tray.open", tag), open_ui, default=True),
            pystray_mod.MenuItem(say("launcher.tray.quit", tag), quit_),
        ),
    )

    def watch(started: Any) -> None:
        started.visible = True
        worker.join()
        started.stop()

    if dock_icon:
        _show_dock_icon()
    try:
        icon.run(setup=watch)
    except Exception:
        # No status bar to sit in (a session without a window server, as in
        # CI's boot probe). The server is already up on the worker, so keep
        # serving.
        worker.join()
    return failure[0] if failure else None


def reads_launcher_conf(env: MutableMapping[str, str], *, frozen: bool) -> bool:
    """Whether this install shape configures itself through ``launcher.conf``.

    The file exists for installs nobody can hand an environment variable to. Two
    qualify. A frozen desktop build is double-clicked. The snap is a daemon
    snapd starts at boot, and ``snap/snapcraft.yaml`` declares no configure hook,
    so ``snap set`` reaches nothing and its ``environment:`` block is baked at
    build time. Without this file, ``REAPER_RECOVERY`` would have no route into
    a snap install at all, the same dead end the desktop builds have for a
    different reason.

    The container is deliberately not here: a compose file is already a file of
    environment variables, so a second one inside ``/data`` would give every
    setting two homes and a precedence order to get wrong. A source checkout is
    not here either, since it has ``.env``.

    ``REAPER_HOME`` is what names the snap. ``snapcraft.yaml`` sets it to
    ``$SNAP``, and no other install shape sets it (the Dockerfile does not).
    """
    return frozen or bool(env.get("REAPER_HOME", "").strip())


def main() -> None:
    frozen = _bundle_root() is not None
    tag = _os_locale_tag()
    export_buildinfo(os.environ, _buildinfo_path())
    _resolve_data_dir(os.environ, frozen=frozen)
    conf: Path | None = None
    # `.get`, not `[...]`: only `_resolve_data_dir` guarantees the key, and it
    # sets it for frozen builds alone. The snap arrives with it already in the
    # environment. Anything else reaching here without one falls back to the
    # settings default rather than raising.
    data_dir = os.environ.get("REAPER_DATA_DIR", "").strip()
    if reads_launcher_conf(os.environ, frozen=frozen) and data_dir:
        conf = load_launcher_conf(os.environ, Path(data_dir))

    # Before anything touches the disk: a plain `pip install reaper` puts this script
    # on PATH with no migrations beside it (site-packages has no alembic/), and
    # continuing would create a data folder that can never be brought current.
    root = project_root()
    if not (root / "alembic").is_dir():
        _say(say("launcher.dialog.no_migrations", tag), frozen=frozen)
        raise SystemExit(2)

    # Refuse the occupied port before preflight touches the disk: preflight
    # applies a staged restore by renaming the live database files aside, and
    # on macOS/Linux those renames succeed even under a running server's open
    # handles. A doubled launch would then swap the data out from under the
    # copy that keeps serving. A launch that will not serve must mutate
    # nothing.
    # `configured_env`, not `os.environ`: a source checkout's `.env.local` is
    # what `Settings` reads. Reading `os.environ` directly here could bind a
    # different port than the one `main.py` prints in the anti-lockout
    # recovery link, which is the way back in for a locked-out operator.
    # `load_launcher_conf` above has already `setdefault`-ed the desktop conf
    # into `os.environ`, which wins here the same way a real environment
    # variable wins inside `Settings`.
    settled = configured_env()
    host = _host(settled)
    port = _port(settled, frozen=frozen)
    if _loopback_occupied(port):
        move = (
            say("launcher.move.conf", tag, conf=str(conf))
            if conf is not None
            else say("launcher.move.env", tag)
        )
        _say(
            say("launcher.dialog.port_in_use", tag, port=port, move=move),
            frozen=frozen,
        )
        raise SystemExit(2)

    # Imported after the environment is settled: get_settings() caches on first call,
    # and that first call must see the data dir chosen above.
    from reaper.preflight import main as preflight

    # `_say`, not preflight's own stderr write: its four fatal sentences are
    # the ones a double-clicked build has to see, and a windowed PyInstaller
    # build's stderr is `os.devnull`. The branch's schema-gate refusal is the
    # third of them. The fourth is a pre-migration snapshot that could not be
    # written.
    code = preflight(lambda message: _say(message, frozen=frozen))
    if code:
        raise SystemExit(code)

    # Preflight above is also what refuses a database this build cannot serve
    # (``reaper.db.schema_gate.refusal``, called at the end of
    # ``preflight.main``). It has to be that call and not one here: it runs
    # after preflight's staged-restore swap, and restoring a backup is one of
    # the two ways out the refusal names. The snapshot the line below may
    # need is taken there too, for the same reason and one line later
    # (``backup.snapshot_before_migration``). So this call site needs
    # nothing, and neither do the container entrypoint or
    # ``scripts/dev-local.sh``.
    _migrate(root)
    if _browser_wanted(settled, frozen=frozen):
        _open_browser_when_up(port)

    data_dir = os.environ.get("REAPER_DATA_DIR", "").strip() or "data"
    print(f"Reaper is starting on http://127.0.0.1:{port} (data folder: {data_dir})")

    import uvicorn

    # The factory is passed as an object, not the usual "reaper.main:create_app"
    # string: uvicorn's string import fails under a frozen bundle and masks the
    # underlying error as "could not import module".
    #
    # The import stays in the function because ``reaper.main`` mounts every router and
    # ``api/settings.py`` imports this module. Promoted to module level it is fatal, not
    # slow: ``main``, ``api.settings`` and ``api.plex`` all raise ImportError on a circular
    # import. Those are the two cycles ``_KNOWN_IMPORT_CYCLES`` declares in
    # ``tests/test_repo_hygiene.py``, and this edge is what closes both.
    from reaper.main import create_app

    if _tray_wanted(sys.platform, settled, frozen=frozen):
        backend = _tray_backend()
        image = _tray_image() if backend is not None else None
        if backend is not None and image is not None:
            server = uvicorn.Server(uvicorn.Config(create_app, **_serve_kwargs(host, port)))
            dock = sys.platform == "darwin" and env_flag(DESKTOP_DOCK_KEY, default=False)
            error = _serve_with_tray(backend, server, port, image, dock_icon=dock, tag=tag)
            if error is not None:
                _say(say("launcher.dialog.crashed", tag), frozen=frozen)
                raise SystemExit(1)
            return
        sys.stderr.write("No tray icon in this install; serving without one.\n")

    uvicorn.run(create_app, **_serve_kwargs(host, port))


if __name__ == "__main__":
    main()
