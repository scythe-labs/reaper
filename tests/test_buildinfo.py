# SPDX-License-Identifier: AGPL-3.0-or-later
"""The About-page version string: a release shows the plain version, every other build
shows "dev" plus the short commit it was cut from."""

import pytest

from reaper import __version__, buildinfo


class TestBuildVersion:
    def test_a_release_shows_the_plain_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REAPER_RELEASE", "1")
        monkeypatch.setenv("REAPER_GIT_SHA", "abcdef1234567890")
        # The commit is baked in, but a release never appends it.
        assert buildinfo.build_version() == __version__

    @pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on"])
    def test_release_flag_is_read_loosely(self, monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
        monkeypatch.setenv("REAPER_RELEASE", flag)
        assert buildinfo.build_version() == __version__

    def test_a_dev_build_shows_the_baked_short_commit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("REAPER_RELEASE", raising=False)
        monkeypatch.setenv("REAPER_GIT_SHA", "abcdef1234567890")
        assert buildinfo.build_version() == "dev (abcdef1)"

    def test_an_empty_release_flag_is_not_a_release(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REAPER_RELEASE", "")
        monkeypatch.setenv("REAPER_GIT_SHA", "0123456abcdef")
        assert buildinfo.build_version() == "dev (0123456)"

    def test_a_dev_build_with_no_commit_shows_just_dev(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("REAPER_RELEASE", raising=False)
        monkeypatch.delenv("REAPER_GIT_SHA", raising=False)
        # No baked commit and no readable .git (the shipped container's case).
        monkeypatch.setattr(buildinfo, "_commit_from_git_dir", lambda: None)
        assert buildinfo.build_version() == "dev"
