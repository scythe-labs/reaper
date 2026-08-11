# SPDX-License-Identifier: AGPL-3.0-or-later
"""The About-page version string: a release shows the plain version, every other build
shows "dev" plus the short commit it was cut from."""

from pathlib import Path

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


class TestTheEnvBooleanVocabulary:
    """``env_flag`` is the one reading of an environment value as a boolean, for the six
    raw reads that each spelled it themselves. Not one test anywhere passed an
    unrecognized value to any of them, which is the hole this class exists to close."""

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on"])
    def test_a_true_token_is_true_whatever_the_default(self, raw: str) -> None:
        assert buildinfo.env_flag("K", default=False, env={"K": raw}) is True
        assert buildinfo.env_flag("K", default=True, env={"K": raw}) is True

    @pytest.mark.parametrize("raw", ["0", "false", "No", " OFF "])
    def test_a_false_token_is_false_whatever_the_default(self, raw: str) -> None:
        assert buildinfo.env_flag("K", default=True, env={"K": raw}) is False
        assert buildinfo.env_flag("K", default=False, env={"K": raw}) is False

    @pytest.mark.parametrize("raw", ["ture", "enabled", "2", "yes please"])
    def test_an_unrecognized_value_falls_to_the_default(self, raw: str) -> None:
        """The ``default=True`` half is the load-bearing one (rule 141): where the default
        is False, a hardcoded False and a fall-to-default are the same output.

        ``""`` and ``"  "`` are deliberately not in this table. Both already fell to the
        default at every site before this helper existed, so they cannot fail here and
        would read as coverage of the case that could."""
        assert buildinfo.env_flag("K", default=True, env={"K": raw}) is True
        assert buildinfo.env_flag("K", default=False, env={"K": raw}) is False

    def test_an_absent_key_is_the_default(self) -> None:
        assert buildinfo.env_flag("MISSING", default=True, env={}) is True
        assert buildinfo.env_flag("MISSING", default=False, env={}) is False

    def test_the_process_environment_is_the_default_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``env=`` is for the launcher, which holds a mapping. Everyone else reads
        ``os.environ``, and passing an empty mapping must not be how that happens."""
        monkeypatch.setenv("K", "on")
        assert buildinfo.env_flag("K", default=False) is True


class TestProjectRoot:
    """The one walk from this package up to the checkout root, and its packaged override."""

    def test_a_packaged_install_uses_the_root_it_names(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("REAPER_HOME", str(tmp_path))
        assert buildinfo.project_root() == tmp_path

    def test_a_source_checkout_reaches_the_directory_holding_src(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asserted on what the root must CONTAIN, not on a second count of the levels.

        The two callers that inlined this walk read ``frontend/`` and ``alembic/`` out of
        the answer, and both sit beside ``src/`` in the checkout. Counting parents here
        instead would agree with a wrong depth as readily as with the right one (rule 119).
        """
        monkeypatch.delenv("REAPER_HOME", raising=False)
        root = buildinfo.project_root()
        assert (root / "src" / "reaper" / "buildinfo.py").is_file(), root
        assert (root / "alembic" / "env.py").is_file(), root
        assert (root / "frontend").is_dir(), root
