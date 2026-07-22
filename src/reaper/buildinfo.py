# SPDX-License-Identifier: AGPL-3.0-or-later
"""The version string shown to the operator on the About page.

A tagged release shows the real version (``__version__``) unchanged. Every other
build -- a dev image, a PR image, a local checkout -- shows ``dev`` plus the short
commit it was built from, so one dev build is told apart from the next.

The shipped container has no ``.git`` (it is dockerignored), so CI bakes the short
commit in as ``REAPER_GIT_SHA`` and marks a release with ``REAPER_RELEASE`` at build
time. In a local checkout both are absent, so the commit is read from ``.git``
directly, no subprocess. Neither value is a secret.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from reaper import __version__

_TRUE = {"1", "true", "yes", "on"}
_SHORT = 7


def build_version() -> str:
    """What the About page shows after ``Reaper``. A release shows the plain version; a
    dev build shows ``dev (<short commit>)``, or just ``dev`` when the commit is unknown."""
    if os.environ.get("REAPER_RELEASE", "").strip().lower() in _TRUE:
        return __version__
    sha = _short_commit()
    return f"dev ({sha})" if sha else "dev"


def _short_commit() -> str | None:
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
