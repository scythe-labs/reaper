# SPDX-License-Identifier: AGPL-3.0-or-later
"""The data-dir writability probe and the container preflight.

An unwritable data folder is the single most common deploy failure (a bind mount
owned by root while the container runs unprivileged). These lock in that it fails
with a plain, actionable message rather than SQLite's opaque driver traceback.

One test here is uid-dependent and always will be: root bypasses directory permissions,
so a chmod-based probe cannot fail for it. CI runs jobs in a container whose uid this
repository does not pin, so that test may or may not run there -- which is why the real
filesystem is also exercised by a case no uid can bypass
(``test_a_real_unusable_data_dir_is_caught_whatever_the_uid``). The coverage no longer
depends on who the runner happens to be.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from reaper import preflight
from reaper.config import DataDirError, Settings
from reaper.services import restore


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


def test_a_real_unusable_data_dir_is_caught_whatever_the_uid(tmp_path: Path) -> None:
    """A genuine filesystem refusal, on every runner, root or not.

    The permission case below is the one operators actually hit, but root bypasses
    directory permissions, so on a CI runner that runs jobs as root it silently skips --
    and the module's stated subject ("the single most common deploy failure") goes
    unverified with nothing on screen to say so. Its two other siblings monkeypatch
    ``TemporaryFile`` to raise, which tests the *message* rather than the probe.

    A data dir under a regular FILE is the failure no uid can bypass: ``mkdir`` gets
    ENOTDIR from the kernel whoever asks. Different errno, same contract -- a real OSError
    from a real path becomes a plain ``DataDirError`` rather than SQLite's opaque
    "unable to open database file" several layers later.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")

    with pytest.raises(DataDirError):
        _settings(blocker / "data").ensure_data_dir()


@pytest.mark.skipif(
    os.getuid() == 0,
    reason=(
        "root bypasses directory permissions; the always-on ENOTDIR case above covers "
        "the real-filesystem path here"
    ),
)
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


def test_preflight_clears_a_staged_restore_nobody_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Boot is the one place that can reclaim a staging whose token died with the browser
    that held it, so the wiring is pinned here and not only the sweep itself (#388)."""
    settings = _settings(tmp_path)
    settings.ensure_data_dir()
    pending = tmp_path / restore.PENDING_DIR
    pending.mkdir()
    (pending / "reaper.db").write_bytes(b"SQLite format 3\x00")
    (pending / "secret.key").write_text("key material nobody owns")
    (pending / restore.TOKEN_MARKER).write_text("deadbeef\n")

    monkeypatch.setattr(preflight, "get_settings", lambda: _settings(tmp_path))
    assert preflight.main() == 0

    assert not pending.exists()
    assert "never confirmed" in capsys.readouterr().err


def test_preflight_leaves_an_armed_restore_for_the_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same boot applies an armed restore, so the sweep above must not reach one.

    Both legs consume the staging directory, so its absence afterwards proves nothing about
    which one took it. What discriminates is who says so: the swap reports on it, and the
    sweep must stay silent."""
    settings = _settings(tmp_path)
    settings.ensure_data_dir()
    pending = tmp_path / restore.PENDING_DIR
    pending.mkdir()
    (pending / restore.READY_MARKER).write_text("")

    monkeypatch.setattr(preflight, "get_settings", lambda: _settings(tmp_path))
    assert preflight.main() == 0

    err = capsys.readouterr().err
    assert "never confirmed" not in err  # the sweep did not take it
    assert "unreadable" in err  # the swap did, and kept the live data


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
