# SPDX-License-Identifier: AGPL-3.0-or-later
"""The data-dir writability probe and the container preflight.

An unwritable data folder is the single most common deploy failure (a bind mount
owned by root while the container runs unprivileged). These lock in that it fails
with a plain, actionable message rather than SQLite's opaque driver traceback.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from reaper import preflight
from reaper.config import DataDirError, Settings


def _settings(data_dir: Path) -> Settings:
    return Settings(data_dir=data_dir)  # type: ignore[call-arg]


def test_ensure_data_dir_creates_and_leaves_nothing(tmp_path: Path) -> None:
    target = tmp_path / "data"
    resolved = _settings(target).ensure_data_dir()
    assert resolved == target.resolve()
    assert resolved.is_dir()
    # The write probe cleans up after itself.
    assert list(resolved.iterdir()) == []


def test_permission_error_names_the_chown_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _denied(*_a: object, **_k: object) -> object:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr("reaper.config.tempfile.TemporaryFile", _denied)
    with pytest.raises(DataDirError) as excinfo:
        _settings(tmp_path).ensure_data_dir()

    msg = str(excinfo.value)
    assert "chown -R" in msg
    assert str(tmp_path.resolve()) in msg
    assert str(os.getuid()) in msg


def test_non_permission_error_omits_the_chown_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A full disk is not an ownership problem, so the chown fix would be wrong.
    def _full(*_a: object, **_k: object) -> object:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("reaper.config.tempfile.TemporaryFile", _full)
    with pytest.raises(DataDirError) as excinfo:
        _settings(tmp_path).ensure_data_dir()

    msg = str(excinfo.value)
    assert "chown" not in msg
    assert "No space left on device" in msg


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses directory permissions")
def test_real_readonly_directory_is_caught(tmp_path: Path) -> None:
    readonly = tmp_path / "ro"
    readonly.mkdir()
    readonly.chmod(0o500)  # r-x: the owner cannot create files here
    try:
        with pytest.raises(DataDirError):
            _settings(readonly).ensure_data_dir()
    finally:
        readonly.chmod(0o700)  # let pytest clean the tree up


def test_preflight_ok_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "get_settings", lambda: _settings(tmp_path))
    assert preflight.main() == 0
    assert tmp_path.is_dir()


def test_preflight_prints_message_and_returns_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _Unwritable:
        def ensure_data_dir(self) -> Path:
            raise DataDirError(tmp_path, PermissionError(errno.EACCES, "Permission denied"))

    monkeypatch.setattr(preflight, "get_settings", lambda: _Unwritable())
    assert preflight.main() == 1
    captured = capsys.readouterr()
    assert "chown -R" in captured.err
    # The actionable message goes to stderr, not stdout.
    assert captured.out == ""
