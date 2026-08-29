# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checks that the provenance file's writer and reader agree, using a real file.

``scripts/write_buildinfo.py`` writes what CI knows at build time. ``reaper.launcher.
export_buildinfo`` reads it back when a packaged install boots. The two live far apart, a
script CI runs versus the shipped package, so a key renamed on one side would silently
strand the other: the launcher skips any key it does not recognize, and a build whose
provenance never lands reports "dev" forever. Writing through a real file and reading it
back is the check that catches a key added on one side and not the other.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

from reaper import launcher

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "write_buildinfo.py"


def _load_writer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("write_buildinfo", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_everything_the_writer_writes_lands_in_the_environment(tmp_path: Path) -> None:
    writer = _load_writer()
    payload = writer.build_payload(
        version="2026.8.1", commit="abc1234", release=True, repo="owner/reaper"
    )
    path = tmp_path / "buildinfo.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    env: dict[str, str] = {}
    launcher.export_buildinfo(env, path)

    assert env == {
        "REAPER_VERSION": "2026.8.1",
        "REAPER_GIT_SHA": "abc1234",
        "REAPER_RELEASE": "1",
        "REAPER_UPDATE_REPO": "owner/reaper",
    }
    # Every key the writer can emit is one the launcher understands. A key added on
    # one side must be added to the other in the same change.
    assert set(writer.KEYS) == set(launcher._BUILDINFO_KEYS)


def test_a_dev_build_writes_no_release_flag(tmp_path: Path) -> None:
    """The dev payload has no "release" key at all.

    The reading side treats any string value as present, so a falsy string like "0" would
    still need special-casing everywhere it is read. Leaving the key out is the one way to
    say "no" that cannot be misread.
    """
    writer = _load_writer()
    payload = writer.build_payload(version=None, commit="abc1234", release=False, repo=None)
    assert payload == {"commit": "abc1234"}

    path = tmp_path / "buildinfo.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    env: dict[str, str] = {}
    launcher.export_buildinfo(env, path)
    assert env == {"REAPER_GIT_SHA": "abc1234"}
