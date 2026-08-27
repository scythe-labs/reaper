# SPDX-License-Identifier: AGPL-3.0-or-later
"""The version string shown to the operator on the About page.

A tagged release shows the real version unchanged. Every other build (a dev image,
a PR image, a local checkout) shows ``dev`` plus the short commit it was built
from, so one dev build can be told apart from the next.

The shipped artifacts have no ``.git``: it is excluded from the Docker build, and
a binary bundle carries none. So the build bakes provenance in as environment
values: the short commit as ``REAPER_GIT_SHA``, a release flag as
``REAPER_RELEASE``, and on a release the calendar-versioned tag as
``REAPER_VERSION`` (``__version__`` is the fallback for a source install). The
container gets these as image environment variables; the binaries carry a
``buildinfo.json`` that :mod:`reaper.launcher` exports before anything reads
them. In a local checkout none of these are set, so the commit is read straight
from ``.git``, with no subprocess call. None of these values is a secret.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Literal

from reaper import __version__
from reaper.config import configured_env

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_SHORT = 7


def env_flag(key: str, *, default: bool, env: Mapping[str, str] | None = None) -> bool:
    """Read one environment value as a boolean, the form every Reaper-defined
    boolean env variable uses (not the pydantic ``Settings`` fields).

    Recognizes eight tokens, four for true and four for false, ignoring case and
    whitespace. Anything else falls back to ``default``, and that direction
    matters: treating an unrecognized value as False buys whatever False means
    at that key. On a frozen macOS build, ``REAPER_TRAY=ture`` silently produced
    an app with no menu-bar icon, which is its only route to Quit
    (``LSUIElement`` hides the Dock icon too).

    ``env`` lets a caller pass its own mapping instead of the process
    environment. The launcher resolves both before it serves requests. With no
    ``env``, the source is ``config.configured_env()``, not ``os.environ``:
    pydantic-settings reads ``.env.local`` into ``Settings`` without exporting
    it to the process environment, so reading straight from ``os.environ``
    would leave ``REAPER_TRAY``, ``REAPER_DOCK_ICON``, ``REAPER_LAUNCH_BROWSER``,
    and ``REAPER_UPDATE_CHECK`` silently ignored when an operator set them there.

    The pydantic ``Settings`` booleans do not read through this function. An
    unrecognized value there raises and refuses the boot, which is the
    strongest answer available for ``destructive_actions_enabled`` and
    ``recovery``. These four stay outside ``Settings`` for the opposite reason:
    promoting them would turn ``REAPER_TRAY=ture`` from a tolerated default
    into a refused boot, on a build with no stderr anyone reads.
    ``scan_runner``'s three-state token pair follows the same reasoning, and
    explains it where it is defined.
    """
    raw = (configured_env() if env is None else env).get(key, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


InstallKind = Literal["container", "snap", "desktop", "source"]

#: A container runtime plants one of these at the filesystem root of everything it
#: starts: Docker the first, Podman the second. Neither exists on a host install.
_CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))


def frozen_bundle() -> Path | None:
    """The unpacked PyInstaller bundle, or ``None`` when this is not a frozen build.

    This is the only place in the codebase that reads ``sys._MEIPASS``.
    `install_root`, `install_kind`, and `launcher._bundle_root` each answer a
    different question from this one fact.
    """
    root = getattr(sys, "_MEIPASS", None)
    return Path(root) if root else None


def install_kind() -> InstallKind:
    """Which of the four shipped shapes is running, for the boot log.

    Each branch checks a signal only that shape sets. A frozen bundle is one of
    the double-clicked desktop builds. ``REAPER_HOME`` marks the snap
    (``snapcraft.yaml`` sets it to ``$SNAP``, and no other shape sets it, which
    `launcher.reads_launcher_conf` relies on too). A container runtime plants a
    marker file. Anything unrecognized reports ``source``, since that shape
    makes the fewest promises rather than guessing at a fifth shape.

    Support uses this before anything else: the four shapes keep their data in
    four different places, take configuration by four different routes, and
    fail differently.
    """
    if frozen_bundle() is not None:
        return "desktop"
    if os.environ.get("REAPER_HOME", "").strip():
        return "snap"
    if any(marker.exists() for marker in _CONTAINER_MARKERS):
        return "container"
    return "source"


def install_root() -> Path | None:
    """Where a packaged install keeps the pieces that live beside the code: the built
    SPA, the migrations, and ``buildinfo.json``. ``REAPER_HOME`` names it directly
    (the snap sets it to ``$SNAP``); for a frozen (PyInstaller) build it is the
    unpacked bundle. A source checkout returns ``None``, and `project_root` falls
    back to the repo root, so development never needs either value set."""
    named = os.environ.get("REAPER_HOME", "").strip()
    if named:
        return Path(named)
    return frozen_bundle()


def project_root() -> Path:
    """Where the pieces beside the code are, in every install shape: `install_root`
    when a packaged install names one, the repo root otherwise.

    This is the only place that counts directory levels up to the checkout root.
    Duplicating that count elsewhere is risky: two copies can share the same
    depth by coincidence, and then silently disagree if either file moves.
    `db.schema_gate.alembic_dir` takes a different approach on purpose: it
    checks every parent directory for one that identifies itself, which works
    for shapes this function cannot handle.
    """
    return install_root() or Path(__file__).resolve().parent.parent.parent


def build_version() -> str:
    """What the About page shows after ``Reaper``. A release shows the plain version.
    A dev build shows ``dev (<short commit>)``, or just ``dev`` when the commit is
    unknown."""
    if is_release():
        return version_number()
    sha = short_commit()
    return f"dev ({sha})" if sha else "dev"


def is_release() -> bool:
    """Whether this build was cut from a release. This value also selects the
    update channel. A release build follows published releases, and every other
    build follows the dev branch."""
    return env_flag("REAPER_RELEASE", default=False)


def version_number() -> str:
    """The plain version number, without any channel label. Returns what CI baked
    in as ``REAPER_VERSION``, or the package's own version otherwise. Update
    checks compare this value against a release tag."""
    return os.environ.get("REAPER_VERSION", "").strip() or __version__


def short_commit() -> str | None:
    """The short commit this build was cut from. Returns the value CI baked in, or
    reads it from the local checkout's ``.git``, or ``None`` if neither is
    available (the shipped container has neither)."""
    baked = os.environ.get("REAPER_GIT_SHA", "").strip()
    if baked:
        return baked[:_SHORT]
    return _commit_from_git_dir()


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value.lower())


@lru_cache(maxsize=1)
def _commit_from_git_dir() -> str | None:
    """Read the current commit straight from ``.git``, handling a plain checkout, a
    detached HEAD, and a linked worktree (where ``.git`` is a file and the ref
    lives in the shared common dir). Any surprise resolves to ``None``. An
    unknown commit only ever shows ``dev`` on the About page. It never raises
    out of that route."""
    try:
        entry = _find_git(Path(__file__).resolve().parent)
        if entry is None:
            return None

        if entry.is_dir():
            git_dir = entry
        else:
            # A worktree's ".git" is a file containing "gitdir: <path to the shared git dir>".
            pointer = entry.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            target = Path(pointer.split(":", 1)[1].strip())
            git_dir = target if target.is_absolute() else (entry.parent / target).resolve()

        # A linked worktree's loose refs and packed-refs live in the shared common
        # dir, not its own git dir. HEAD is the one thing that stays local.
        commondir = git_dir / "commondir"
        if commondir.exists():
            rel = commondir.read_text(encoding="utf-8").strip()
            common_dir = Path(rel) if Path(rel).is_absolute() else (git_dir / rel).resolve()
        else:
            common_dir = git_dir

        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if _is_sha(head):
            return head[:_SHORT]  # detached HEAD points straight at the commit
        if not head.startswith("ref:"):
            return None
        ref = head.split(":", 1)[1].strip()

        for base in (git_dir, common_dir):
            loose = base / ref
            if loose.exists():
                value = loose.read_text(encoding="utf-8").strip()
                return value[:_SHORT] if _is_sha(value) else None

        packed = common_dir / "packed-refs"
        if packed.exists():
            for line in packed.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "^")):
                    continue
                sha, _, name = line.partition(" ")
                if name == ref and _is_sha(sha):
                    return sha[:_SHORT]
        return None
    except OSError:
        return None


def _find_git(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / ".git"
        if candidate.exists():
            return candidate
    return None
