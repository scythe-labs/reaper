#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Write the ``buildinfo.json`` a packaged build carries.

CI calls this before PyInstaller (and the snap part calls it before staging), so
every packaged install can name its version, commit, channel, and home repository
without a ``.git`` beside it. ``reaper.launcher.export_buildinfo`` is the reader;
the key set here and the ``_BUILDINFO_KEYS`` map there must agree, and
``tests/test_packaging.py`` checks they do.

    python scripts/write_buildinfo.py --out packaging/pyinstaller/buildinfo.json \\
        --version 2026.8.1 --commit abc1234 --release --repo owner/reaper
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

#: Everything a packaged build knows about itself. "release" is written as the "1"
#: the env-reading side treats as true, or omitted entirely for a dev build.
KEYS = ("version", "commit", "release", "repo")


def build_payload(
    *, version: str | None, commit: str | None, release: bool, repo: str | None
) -> dict[str, str]:
    payload: dict[str, str] = {}
    if version and version.strip():
        payload["version"] = version.strip()
    if commit and commit.strip():
        payload["commit"] = commit.strip()
    if release:
        payload["release"] = "1"
    if repo and repo.strip():
        payload["repo"] = repo.strip()
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--version", default=None)
    parser.add_argument("--commit", default=None)
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--repo", default=None)
    args = parser.parse_args()

    payload = build_payload(
        version=args.version, commit=args.commit, release=args.release, repo=args.repo
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
