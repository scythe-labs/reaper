# SPDX-License-Identifier: AGPL-3.0-or-later
"""The packaged-install entry point: one process that prepares, then serves.

The container has ``docker-entrypoint.sh``; the Windows and macOS binaries and the
snap have this, in the same order for the same reasons: pick a writable data folder,
verify it (preflight, which also applies a staged restore), apply migrations, then
serve. A half-migrated schema must never serve traffic for a tool that deletes media.

Provenance rides along as ``buildinfo.json``: a frozen bundle carries it next to the
executable, the snap points ``REAPER_BUILDINFO`` at its copy, and its values are
exported into the environment (never overriding one the operator set) before anything
reads them -- :mod:`reaper.buildinfo` and the update check read only the environment,
so every install shape answers the same way.

The data folder default is per-platform only when frozen. Run from a source checkout
this launcher keeps the repo-relative ``data/`` every other dev entry point uses.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import MutableMapping
from pathlib import Path

from reaper.buildinfo import install_root

_TRUE = {"1", "true", "yes", "on"}

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
    root = getattr(sys, "_MEIPASS", None)
    return Path(root) if root else None


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


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

    The same folder every run, whatever directory the operator launched from -- a
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

    Set as an env value rather than passed inward, so every later reader -- preflight,
    alembic's own ``get_settings`` call, the app -- resolves the same folder.
    """
    if frozen and not env.get("REAPER_DATA_DIR", "").strip():
        env["REAPER_DATA_DIR"] = str(default_data_dir(sys.platform, env))


def _migrate(root: Path) -> None:
    """``alembic upgrade head``, addressed into this install's copy of ``alembic/``.

    Programmatic because a bundle has no ``alembic.ini`` on a search path; the config
    is built here and ``alembic/env.py`` reads the database URL from settings as it
    always does. ``config_file_name`` stays unset, which env.py treats as "skip
    logging config" -- uvicorn owns logging in this process.
    """
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(config, "head")


def _port(env: MutableMapping[str, str]) -> int:
    raw = env.get("REAPER_PORT", "").strip() or "8420"
    try:
        return int(raw)
    except ValueError:
        sys.stderr.write(f"REAPER_PORT must be a port number; it is set to {raw!r}.\n")
        raise SystemExit(2) from None


def _browser_wanted(env: MutableMapping[str, str], *, frozen: bool) -> bool:
    """Open the operator's browser once the server is up? On for a frozen binary
    (a double-click gives no other signal it worked), off everywhere else; the
    ``REAPER_LAUNCH_BROWSER`` env value overrides either default."""
    configured = env.get("REAPER_LAUNCH_BROWSER", "").strip().lower()
    if configured:
        return configured in _TRUE
    return frozen


def _open_browser_when_up(port: int) -> None:
    """From a daemon thread: wait for our own health probe, then open the UI.

    The poll is stdlib urllib against this process's loopback port -- the same probe
    the container HEALTHCHECK runs, not an integration call, which is why it does not
    go through ``clients/`` (rule 33 governs external services; this asks ourselves).
    Give up silently after a minute: the server log is the fallback signal, and a
    browser that never opens must not stop the server that is otherwise fine.
    """

    def poll() -> None:
        deadline = time.monotonic() + 60
        url = f"http://127.0.0.1:{port}/api/health"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310
                    if response.status == 200:
                        webbrowser.open(f"http://127.0.0.1:{port}")
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.5)

    threading.Thread(target=poll, name="reaper-open-browser", daemon=True).start()


def main() -> None:
    frozen = _bundle_root() is not None
    export_buildinfo(os.environ, _buildinfo_path())
    _resolve_data_dir(os.environ, frozen=frozen)

    # Imported after the environment is settled: get_settings() caches on first call,
    # and that first call must see the data dir chosen above.
    from reaper.preflight import main as preflight

    code = preflight()
    if code:
        raise SystemExit(code)

    root = install_root() or _repo_root()
    _migrate(root)

    host = os.environ.get("REAPER_HOST", "").strip() or "0.0.0.0"
    port = _port(os.environ)
    if _browser_wanted(os.environ, frozen=frozen):
        _open_browser_when_up(port)

    data_dir = os.environ.get("REAPER_DATA_DIR", "").strip() or "data"
    print(f"Reaper is starting on http://127.0.0.1:{port} (data folder: {data_dir})")

    import uvicorn

    # The factory is passed as an object, not the usual "reaper.main:create_app"
    # string: uvicorn's string import fails under a frozen bundle and masks the
    # underlying error as "could not import module".
    from reaper.main import create_app

    # proxy_headers=False is the same load-bearing choice as the container CMD's
    # --no-proxy-headers: reaper.auth.proxy alone decides peer trust, never a
    # forwarded header rewritten one layer above it. test_launcher pins it.
    uvicorn.run(
        create_app,
        factory=True,
        host=host,
        port=port,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
