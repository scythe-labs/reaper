# SPDX-License-Identifier: AGPL-3.0-or-later
"""The version string shown to the operator on the About page.

A tagged release shows the real version unchanged. Every other build -- a dev image,
a PR image, a local checkout -- shows ``dev`` plus the short commit it was built
from, so one dev build is told apart from the next.

The shipped artifacts have no ``.git`` (it is dockerignored, and a binary bundle
carries none), so the build bakes provenance in as environment values: the short
commit as ``REAPER_GIT_SHA``, a release flag as ``REAPER_RELEASE``, and on a release
the CalVer the tag names as ``REAPER_VERSION`` (``__version__`` is the fallback for a
source install). The container gets them as image env; the binaries carry a
``buildinfo.json`` that :mod:`reaper.launcher` exports before anything reads them.
In a local checkout all are absent, so the commit is read from ``.git`` directly, no
subprocess. None of these values is a secret.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Literal

from reaper import __version__

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_SHORT = 7


def env_flag(key: str, *, default: bool, env: Mapping[str, str] | None = None) -> bool:
    """One environment value read as a boolean, for every env boolean Reaper has.

    Eight tokens, four each way, case- and whitespace-insensitive. **Anything else falls
    to ``default``**, which is the direction that matters: reading an unrecognized value
    as False silently buys whatever False means at that key, and on a frozen macOS build
    ``REAPER_TRAY=ture`` bought an app with no menu-bar icon, which is its only route to
    Quit (``LSUIElement`` hides the Dock one).

    ``env`` is for a caller holding a mapping rather than the process environment; the
    launcher resolves both before it serves.

    The pydantic ``Settings`` booleans deliberately do NOT read through this. An
    unrecognized value there raises and refuses the boot, which for
    ``destructive_actions_enabled`` and ``recovery`` is the strongest answer available.
    ``scan_runner``'s three-state token pair is likewise its own, and says why in place.
    """
    raw = (os.environ if env is None else env).get(key, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


InstallKind = Literal["container", "snap", "desktop", "source"]

#: A container runtime plants one of these in the filesystem root of everything it
#: starts -- Docker the first, Podman the second. Neither exists on a host install.
_CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))


def frozen_bundle() -> Path | None:
    """The unpacked PyInstaller bundle, or ``None`` when this is not a frozen build.

    The one reading of ``sys._MEIPASS`` in the codebase (rule 104): `install_root`,
    `install_kind` and `launcher._bundle_root` are three questions off this one fact.
    """
    root = getattr(sys, "_MEIPASS", None)
    return Path(root) if root else None


def install_kind() -> InstallKind:
    """Which of the four shipped shapes is running, for the boot log.

    Each branch reads a signal only that shape sets: a frozen bundle is one of the
    double-clicked desktop builds, ``REAPER_HOME`` is the snap (``snapcraft.yaml``
    sets it to ``$SNAP`` and no other shape sets it, which
    `launcher.reads_launcher_conf` relies on for the same reason), and a container
    runtime plants a marker file. **Anything unrecognized reports ``source``**, which
    is the shape with the fewest promises attached rather than a guess at a fifth.

    Support reads this before anything else: the four shapes keep their data in four
    places, take their configuration by four routes, and fail differently.
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
    SPA, the migrations, and ``buildinfo.json``. ``REAPER_HOME`` names it outright
    (the snap sets it to ``$SNAP``); a frozen (PyInstaller) build is its unpacked
    bundle. A source checkout returns ``None``, and `project_root` is the fallback,
    so development never needs either value set."""
    named = os.environ.get("REAPER_HOME", "").strip()
    if named:
        return Path(named)
    return frozen_bundle()


def project_root() -> Path:
    """Where the pieces beside the code are, in every install shape: `install_root`
    when a packaged install names one, the repo root otherwise.

    The walk counting levels up to the checkout root lives here and nowhere else. Two
    callers inlined it from two modules that happened to sit at the same depth, so moving
    either one gave a silently wrong answer instead of an import error.
    `db.schema_gate.alembic_dir` keeps its own shape on purpose: it tries every parent for
    a directory that identifies itself, which answers for shapes this cannot.
    """
    return install_root() or Path(__file__).resolve().parent.parent.parent


def build_version() -> str:
    """What the About page shows after ``Reaper``. A release shows the plain version; a
    dev build shows ``dev (<short commit>)``, or just ``dev`` when the commit is unknown."""
    if is_release():
        return version_number()
    sha = short_commit()
    return f"dev ({sha})" if sha else "dev"


def is_release() -> bool:
    """Whether this build was cut from a release, which is also its update channel:
    a release follows published releases, everything else follows the dev branch."""
    return env_flag("REAPER_RELEASE", default=False)


def version_number() -> str:
    """The plain version with no channel dressing: what CI baked as ``REAPER_VERSION``,
    else the package's own. This is the value update checks compare against a tag."""
    return os.environ.get("REAPER_VERSION", "").strip() or __version__


def short_commit() -> str | None:
    """The short commit this build was cut from: the value CI baked in, else the local
    checkout's ``.git``, else ``None`` (the shipped container has neither)."""
    baked = os.environ.get("REAPER_GIT_SHA", "").strip()
    if baked:
        return baked[:_SHORT]
    return _commit_from_git_dir()


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value.lower())


@lru_cache(maxsize=1)
def _commit_from_git_dir() -> str | None:
    """Read the current commit straight from ``.git``, handling a plain checkout, a
    detached HEAD, and a linked worktree (where ``.git`` is a file and the ref lives in
    the shared common dir). Any surprise resolves to ``None`` -- an unknown commit only
    ever shows ``dev``, it never raises out of the About route."""
    try:
        entry = _find_git(Path(__file__).resolve().parent)
        if entry is None:
            return None

        if entry.is_dir():
            git_dir = entry
        else:
            # A worktree's ".git" is a file: "gitdir: <path to the worktree's git dir>".
            pointer = entry.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            target = Path(pointer.split(":", 1)[1].strip())
            git_dir = target if target.is_absolute() else (entry.parent / target).resolve()

        # Loose refs and packed-refs of a linked worktree live in the shared common dir,
        # not the worktree's own git dir; HEAD is the one thing that stays local.
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
