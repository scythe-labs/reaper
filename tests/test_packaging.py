# SPDX-License-Identifier: AGPL-3.0-or-later
"""The provenance file's producer and consumer agree, proven by round-trip.

``scripts/write_buildinfo.py`` writes what CI knows at build time;
``reaper.launcher.export_buildinfo`` is what a packaged install reads at boot. The
two live far apart (a script CI runs vs. the shipped package), so a key renamed on
one side would strand the other silently: the launcher skips unknown keys on
purpose, and a build whose provenance never lands just reports "dev" forever.
A round-trip through a real file is the one shape that cannot drift (rule 131:
producer and consumer bounds derive from the same declaration, or a test writes
through one and reads through the other).
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
    # Two-way: every key the writer can emit is one the launcher understands, so a
    # key added on one side must be added on the other in the same change.
    assert set(writer.KEYS) == set(launcher._BUILDINFO_KEYS)


def test_a_dev_build_writes_no_release_flag(tmp_path: Path) -> None:
    """The dev payload omits "release" rather than writing a falsy value: the reading
    side treats presence as a string to export, and "0" exported as REAPER_RELEASE
    would still need parsing everywhere. Absence is the one unambiguous "no"."""
    writer = _load_writer()
    payload = writer.build_payload(version=None, commit="abc1234", release=False, repo=None)
    assert payload == {"commit": "abc1234"}

    path = tmp_path / "buildinfo.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    env: dict[str, str] = {}
    launcher.export_buildinfo(env, path)
    assert env == {"REAPER_GIT_SHA": "abc1234"}
