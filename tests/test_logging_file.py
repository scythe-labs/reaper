# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mirroring the log ring to rotating files on disk.

The rules pinned here:

* the ring's lines are mirrored to ``<data_dir>/logs/reaper.log``, in order;
* the on-disk footprint is bounded to three files of 20 MiB (the live file plus two
  rotations) -- a fourth never appears, so logs cannot fill the data volume;
* the mirror is fed from the same convergence point as the ring, so a line already
  redacted for the ring is already redacted on disk (there is no second path);
* a data dir that cannot be written degrades to "no files," never a crash.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from reaper import logbuffer


@pytest.fixture
def _restore_file_sink() -> Iterator[None]:
    """Leave the module-global file sink as it was before the test.

    ``configure_file_logging`` sets process-wide state; without this a temp-dir sink
    would linger and later appends would chase a deleted path.
    """
    yield
    with logbuffer._file_lock:
        sink = logbuffer._file_sink
        logbuffer._file_sink = None
        logbuffer._log_path = None
        # A degradation test flips this process-global; reset it so a later test starts healthy.
        logbuffer._file_sink_healthy = True
    if sink is not None:
        sink.close()


def test_the_rotation_bounds_are_three_files_of_twenty_mib() -> None:
    # The operator asked for at most three files of 20 MiB. backupCount is the number of
    # rotations kept ALONGSIDE the live file, so two rotations plus the live file is three.
    assert logbuffer.LOG_MAX_BYTES == 20 * 1024 * 1024
    assert logbuffer.LOG_BACKUP_COUNT == 2


def test_lines_are_mirrored_to_disk_in_order(tmp_path: Path, _restore_file_sink: None) -> None:
    logbuffer.configure_file_logging(tmp_path)
    for n in range(3):
        logbuffer.RING.append(ts="2026-01-01T00:00:00Z", level="info", text=f"disk line {n}")

    files = logbuffer.log_files()
    assert files == [tmp_path / "logs" / "reaper.log"]
    body = files[0].read_text()
    assert "disk line 0" in body
    assert "disk line 2" in body
    assert body.index("disk line 0") < body.index("disk line 2")  # oldest first


def test_a_stray_percent_is_written_verbatim(tmp_path: Path, _restore_file_sink: None) -> None:
    # The line is not a format string: a '%' in a logged path or message must land as-is,
    # never be interpreted (which would raise or mangle the line).
    logbuffer.configure_file_logging(tmp_path)
    logbuffer.RING.append(ts="2026-01-01T00:00:00Z", level="info", text="progress 50% done")
    assert "progress 50% done" in (tmp_path / "logs" / "reaper.log").read_text()


def test_rotation_never_keeps_a_fourth_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _restore_file_sink: None
) -> None:
    # A tiny cap so a handful of lines forces many rollovers; the count is what matters.
    monkeypatch.setattr(logbuffer, "LOG_MAX_BYTES", 1024)
    logbuffer.configure_file_logging(tmp_path)
    for n in range(200):
        logbuffer.RING.append(ts="2026-01-01T00:00:00Z", level="info", text=f"{n:04d} " + "x" * 200)

    files = logbuffer.log_files()
    assert len(files) == 3  # live + two rotations, and never more
    assert {p.name for p in files} == {"reaper.log", "reaper.log.1", "reaper.log.2"}
    assert not (tmp_path / "logs" / "reaper.log.3").exists()


def test_log_files_reports_nothing_before_any_write(_restore_file_sink: None) -> None:
    # No sink configured (or nothing written yet): the download falls back to the ring, so
    # log_files must be empty rather than pointing at a file that does not exist.
    with logbuffer._file_lock:
        logbuffer._file_sink = None
        logbuffer._log_path = None
    assert logbuffer.log_files() == []


def test_dump_lines_returns_the_whole_ring_oldest_first() -> None:
    ring = logbuffer.LogRing(maxlen=5)
    for n in range(3):
        ring.append(ts="t", level="info", text=f"line {n}")
    # dump_lines reads the shared RING; drive it through a local ring's own since(0) to
    # prove the ordering contract the download relies on.
    held = ring.since(0, limit=logbuffer.RING_SIZE)
    assert [line.text for line in held] == ["line 0", "line 1", "line 2"]


def test_an_unwritable_data_dir_degrades_to_no_files(
    tmp_path: Path, _restore_file_sink: None
) -> None:
    # A data dir that is actually a file cannot hold a logs/ subdir: setup must swallow the
    # error and leave file logging off, never raise into app startup.
    wall = tmp_path / "not-a-dir"
    wall.write_text("")
    logbuffer.configure_file_logging(wall)
    assert logbuffer.log_files() == []
    # The ring still works; the failed sink just does not capture to disk.
    logbuffer.RING.append(ts="t", level="info", text="still logging")


def test_the_log_directory_is_owner_only(tmp_path: Path, _restore_file_sink: None) -> None:
    # S-6: the decision trail (DEBUG carries the full per-item reasoning) must be no more
    # readable than the databases beside it. The dir is owner-only, so no other local account
    # can traverse into it regardless of the files' own modes.
    logbuffer.configure_file_logging(tmp_path)
    mode = (tmp_path / "logs").stat().st_mode & 0o777
    assert mode == 0o700, oct(mode)


def test_a_preexisting_world_readable_log_dir_is_tightened(
    tmp_path: Path, _restore_file_sink: None
) -> None:
    # A dir left world-readable by an earlier version is chmod'd on the next boot, not only
    # a freshly created one (the chmod runs unconditionally after mkdir).
    loose = tmp_path / "logs"
    loose.mkdir()
    loose.chmod(0o755)  # deliberately world-readable: the old shape configure_file_logging tightens
    logbuffer.configure_file_logging(tmp_path)
    assert (loose.stat().st_mode & 0o777) == 0o700


def test_a_steady_state_write_failure_degrades_loudly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _restore_file_sink: None
) -> None:
    # PR-2: a volume that goes read-only AFTER setup fails every mirror write. The sink must
    # flip to degraded and announce it through the ring exactly once, never swallow it forever.
    logbuffer.configure_file_logging(tmp_path)
    assert logbuffer.file_sink_healthy() is True

    sink = logbuffer._file_sink
    assert sink is not None
    monkeypatch.setattr(
        sink._handler, "handle", lambda record: (_ for _ in ()).throw(OSError("read-only"))
    )

    before = logbuffer.RING.last_seq()
    logbuffer.RING.append(ts="t", level="info", text="line while the disk is read-only")
    assert logbuffer.file_sink_healthy() is False
    announced = [
        line for line in logbuffer.RING.since(before) if "Log file writing failed" in line.text
    ]
    assert len(announced) == 1
    assert announced[0].level == "WARNING"

    # A second failing write does not announce again: the flag is already down.
    mid = logbuffer.RING.last_seq()
    logbuffer.RING.append(ts="t", level="info", text="another line, still read-only")
    assert not [
        line for line in logbuffer.RING.since(mid) if "Log file writing failed" in line.text
    ]


def test_reconfiguring_the_sink_clears_a_prior_degradation(
    tmp_path: Path, _restore_file_sink: None
) -> None:
    # A fresh sink on a dir we just proved writable starts trusted again.
    with logbuffer._file_lock:
        logbuffer._file_sink_healthy = False
    logbuffer.configure_file_logging(tmp_path)
    assert logbuffer.file_sink_healthy() is True
