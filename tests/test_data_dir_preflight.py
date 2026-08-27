# SPDX-License-Identifier: AGPL-3.0-or-later
"""The data-dir writability probe and the container preflight.

An unwritable data folder is the single most common deploy failure (a bind mount
owned by root while the container runs unprivileged). These lock in that it fails
with a plain, actionable message rather than SQLite's opaque driver traceback.

One test here is uid-dependent and always will be. Root bypasses directory permissions, so
a chmod-based probe cannot fail for it. CI runs jobs in a container whose uid this
repository does not pin, so that test may or may not run there, which is why the real
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
from reaper.db import schema_gate
from reaper.services import backup, restore


def _settings(data_dir: Path) -> Settings:
    return Settings(data_dir=data_dir)


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
    directory permissions, so on a CI runner that runs jobs as root it silently skips, and
    the module's stated subject ("the single most common deploy failure") goes unverified
    with nothing on screen to say so. Its two other siblings monkeypatch ``TemporaryFile``
    to raise, which tests the *message* rather than the probe.

    A data dir under a regular *file* is the failure no uid can bypass. ``mkdir`` gets
    ENOTDIR from the kernel whoever asks. Different errno, same contract. A real OSError
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
    that held it, so the wiring is pinned here and not only the sweep itself."""
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


def test_preflight_says_when_it_cleared_crash_leftovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The twin of the line above, swept for the same reason. Both sweeps report what they
    took, and neither message had a test."""
    settings = _settings(tmp_path)
    settings.ensure_data_dir()
    (tmp_path / ".restore-tmp-abandoned").mkdir()
    (tmp_path / ".backup-tmp-abandoned").mkdir()

    monkeypatch.setattr(preflight, "get_settings", lambda: _settings(tmp_path))
    assert preflight.main() == 0

    assert not (tmp_path / ".restore-tmp-abandoned").exists()
    assert "cleared 2 leftover" in capsys.readouterr().err


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


class TestBootSurvivesHousekeeping:
    """Preflight runs before the app, so what it refuses, nobody can start.

    Its three legs are deliberately not alike: the two housekeeping sweeps say what went
    wrong and boot anyway, because a directory Reaper could not tidy is not a reason to
    lock the operator out of their own install, while the restore swap is fatal, because a
    half-swapped database must never be served. All three are pinned here, since the
    difference between them is the whole contract and none of it is visible from a diff.
    """

    @staticmethod
    def _raise(*_a: object, **_k: object) -> object:
        raise OSError(errno.EACCES, "Permission denied")

    def test_a_failed_restore_sweep_does_not_stop_boot(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(preflight, "get_settings", lambda: _settings(tmp_path))
        monkeypatch.setattr(restore, "clear_unarmed_staging", self._raise)

        assert preflight.main() == 0
        assert "could not clear the unconfirmed restore" in capsys.readouterr().err

    def test_a_failed_temp_sweep_does_not_stop_boot(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The sibling of the one above, and the reason it is here. The two handlers are
        # the same shape, so a change that narrows one is a change that should have swept
        # both. This one had no test either.
        monkeypatch.setattr(preflight, "get_settings", lambda: _settings(tmp_path))
        monkeypatch.setattr(backup, "sweep_stale_temp", self._raise)

        assert preflight.main() == 0
        assert "could not sweep leftover temp entries" in capsys.readouterr().err

    def test_a_failed_restore_swap_stops_boot_instead(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The one that must not be tidied into the pattern above. Serving an uncertain
        # database is the failure the prime directive exists to refuse.
        monkeypatch.setattr(preflight, "get_settings", lambda: _settings(tmp_path))
        monkeypatch.setattr(restore, "apply_pending_restore", self._raise)

        assert preflight.main() == 1
        assert "the restore could not be completed" in capsys.readouterr().err


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


class TestTheFourFatalMessagesReachTheCaller:
    """A frozen desktop build's operator sees a refusal, or sees nothing at all.

    Windows is windowed and macOS is `LSUIElement`, PyInstaller leaves the streams `None`,
    and `packaging/pyinstaller/entry.py` rebinds them to `os.devnull`. So a stderr-only
    refusal is written to the null device. A double-clicked Reaper that will not start
    closes with no window, no dialog and no message. `preflight.main` returned an int and
    kept its sentence to itself, so the launcher had nothing to hand `_say`.

    Three fatal messages ride this path, not the two originally reported. The third is the
    schema gate's refusal, which `audit/simplification-plan` added, and it has exactly the
    same shape. A fix written against `dev` alone would have covered two and left it
    invisible.

    The fourth is a pre-migration snapshot that could not be written, and its test is not
    here. It needs a database migrated to a real revision, so it lives with the rest of
    that feature, as
    `test_pre_migration_snapshot.TestPreflightRefusesRatherThanMigrateUnprotected
    ::test_a_snapshot_that_cannot_be_written_stops_the_boot`. Three tests below, four
    messages, and this paragraph is what says so rather than the class implying otherwise.
    """

    @staticmethod
    def _refusals() -> tuple[list[str], object]:
        seen: list[str] = []
        return seen, seen.append

    def test_an_unwritable_data_folder_reaches_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class _Unwritable:
            def ensure_data_dir(self) -> Path:
                raise DataDirError(tmp_path, PermissionError(errno.EACCES, "Permission denied"))

        monkeypatch.setattr(preflight, "get_settings", lambda: _Unwritable())
        seen, refuse = self._refusals()

        assert preflight.main(refuse) == 1  # type: ignore[arg-type]

        assert len(seen) == 1
        assert "chown -R" in seen[0]
        # And it is no longer *also* on stderr, so a caller routing it elsewhere does not
        # print it twice on a console build.
        assert capsys.readouterr().err == ""

    def test_a_restore_that_could_not_complete_reaches_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(_settings: object) -> None:
            raise RuntimeError("disk full")

        monkeypatch.setattr(preflight, "get_settings", lambda: _settings(tmp_path))
        monkeypatch.setattr(restore, "apply_pending_restore", _raise)
        seen, refuse = self._refusals()

        assert preflight.main(refuse) == 1  # type: ignore[arg-type]

        assert len(seen) == 1
        assert "the restore could not be completed" in seen[0]

    def test_the_schema_gates_refusal_reaches_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The third one, and the reason this could not be fixed on `dev`.

        `preflight.main` gained the schema-gate refusal on this branch, in the same
        stderr-only shape as the two originally reported.
        """
        monkeypatch.setattr(preflight, "get_settings", lambda: _settings(tmp_path))
        monkeypatch.setattr(
            schema_gate, "refusal", lambda _revision: "Reaper can't open this database."
        )
        seen, refuse = self._refusals()

        assert preflight.main(refuse) == 1  # type: ignore[arg-type]

        assert seen == ["Reaper can't open this database."]

    def test_the_housekeeping_lines_do_not_reach_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The distinction the callback is drawn on, driven rather than argued.

        A swept temp directory and a cleared unconfirmed restore are notes about work that
        succeeded. Routing them here would put a dialog on the screen at every desktop start
        and train the operator to dismiss the one that matters.
        """
        monkeypatch.setattr(preflight, "get_settings", lambda: _settings(tmp_path))
        monkeypatch.setattr(backup, "sweep_stale_temp", lambda _settings: 3)
        monkeypatch.setattr(restore, "clear_unarmed_staging", lambda _settings: True)
        seen, refuse = self._refusals()

        assert preflight.main(refuse) == 0  # type: ignore[arg-type]

        assert seen == []
        printed = capsys.readouterr().err
        assert "cleared 3 leftover" in printed
        assert "never confirmed" in printed

    def test_the_default_is_still_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The container reads stderr through `docker logs` and passes no callback."""

        class _Unwritable:
            def ensure_data_dir(self) -> Path:
                raise DataDirError(tmp_path, PermissionError(errno.EACCES, "Permission denied"))

        monkeypatch.setattr(preflight, "get_settings", lambda: _Unwritable())

        assert preflight.main() == 1

        assert "chown -R" in capsys.readouterr().err
