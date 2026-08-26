# SPDX-License-Identifier: AGPL-3.0-or-later
"""The update check: channel selection, version ordering, caching, and the route.

The property pinned hardest: a check that cannot answer -- disabled, unreachable,
malformed, unorderable -- answers "unknown" (``update_available=None``), never an
error and never a guess. This surface informs; nothing may gate on it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from reaper.services.update_check import (
    _MAX_NOTES,
    DEFAULT_REPO,
    UpdateChecker,
    _enabled,
    _newer,
)

pytestmark = pytest.mark.httpx2(assert_all_called=False)

_RELEASES = f"https://api.github.com/repos/{DEFAULT_REPO}/releases"
_DEV_TIP = f"https://api.github.com/repos/{DEFAULT_REPO}/commits/dev"
_GHCR = f"https://ghcr.io/v2/{DEFAULT_REPO}"
_INDEX_DIGEST = {"amd64": "sha256:" + "a" * 64, "arm64": "sha256:" + "b" * 64}
_CONFIG_DIGEST = {"amd64": "sha256:" + "c" * 64, "arm64": "sha256:" + "d" * 64}


def _publish_dev_image(
    mock: respx.Router, commits: Mapping[str, str | None], *, multi_arch: bool = True
) -> None:
    """Stand up the four registry reads the dev check makes, with one baked commit per
    architecture. ``multi_arch=False`` publishes the tag as a plain manifest instead of
    an index, which is what :dev is before the first nightly stitches arm64 in.

    A value of ``None`` publishes an image config with no ``REAPER_GIT_SHA`` in it.
    """
    mock.get("https://ghcr.io/token").mock(return_value=httpx.Response(200, json={"token": "t"}))
    for arch, commit in commits.items():
        env = ["REAPER_DATA_DIR=/data"] + ([f"REAPER_GIT_SHA={commit}"] if commit else [])
        mock.get(f"{_GHCR}/manifests/{_INDEX_DIGEST[arch]}").mock(
            return_value=httpx.Response(200, json={"config": {"digest": _CONFIG_DIGEST[arch]}})
        )
        mock.get(f"{_GHCR}/blobs/{_CONFIG_DIGEST[arch]}").mock(
            return_value=httpx.Response(200, json={"config": {"Env": env}})
        )
    tag: dict[str, Any]
    if multi_arch:
        tag = {
            "manifests": [
                {"digest": _INDEX_DIGEST[arch], "platform": {"architecture": arch, "os": "linux"}}
                for arch in commits
            ]
        }
    else:
        (arch,) = commits
        tag = {"config": {"digest": _CONFIG_DIGEST[arch]}}
    mock.get(f"{_GHCR}/manifests/dev").mock(return_value=httpx.Response(200, json=tag))


@pytest.fixture
def _release_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REAPER_RELEASE", "1")
    monkeypatch.setenv("REAPER_VERSION", "2026.8.1")
    monkeypatch.delenv("REAPER_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("REAPER_UPDATE_REPO", raising=False)


@pytest.fixture
def _dev_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REAPER_RELEASE", raising=False)
    monkeypatch.delenv("REAPER_VERSION", raising=False)
    monkeypatch.delenv("REAPER_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("REAPER_UPDATE_REPO", raising=False)
    # Pin the build's own commit through the baked-env path, so the answer never
    # depends on this checkout's .git (rule 119: no environmental accidents).
    monkeypatch.setenv("REAPER_GIT_SHA", "abc1234")


class TestVersionOrdering:
    """The comparison table, written from the spec rather than the code (rule 119)."""

    @pytest.mark.parametrize(
        ("latest", "current", "expected"),
        [
            ("2026.9.1", "2026.8.1", True),
            ("2027.1.1", "2026.12.9", True),
            ("2026.8.1", "2026.8.1", False),
            ("2026.8.1", "2026.9.1", False),
            ("2026.8", "2026.8.0", False),  # padded, not lexicographic: same release
            ("2026.8.10", "2026.8.9", True),  # numeric: 10 > 9 despite "1" < "9"
            ("2026.8.1", "0.1.0", True),  # a source install upgrading to CalVer
            ("2026.8.1-beta", "2026.8.1", None),  # a suffix is unorderable, not newer
            ("nightly", "2026.8.1", None),
        ],
    )
    def test_ordering(self, latest: str, current: str, expected: bool | None) -> None:
        assert _newer(latest, current) is expected


def _release(tag: str, *, notes: str | None = None, prerelease: bool = False) -> dict[str, object]:
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "html_url": f"https://github.com/{DEFAULT_REPO}/releases/tag/{tag}",
        "body": notes,
    }


class TestReleaseChannel:
    @pytest.mark.usefixtures("_release_build")
    async def test_a_newer_release_is_reported_with_its_notes(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(
                200, json=[_release("v2026.9.1", notes="## What changed\n* a fix")]
            )
        )
        status = await UpdateChecker().status()
        assert status.channel == "release"
        assert status.enabled is True
        assert status.current == "2026.8.1"
        assert status.latest == "2026.9.1"
        assert status.update_available is True
        assert status.url is not None and status.url.endswith("v2026.9.1")
        assert status.checked_at is not None
        assert [c.version for c in status.changes] == ["2026.9.1"]
        assert status.changes[0].notes == "## What changed\n* a fix"

    @pytest.mark.usefixtures("_release_build")
    async def test_every_release_behind_is_carried_newest_first(
        self, httpx2_mock: respx.Router
    ) -> None:
        """An operator two releases behind sees both sets of notes: the middle
        release must not read as never having happened."""
        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(
                200,
                json=[
                    _release("v2026.9.2", notes="second"),
                    _release("v2026.9.1", notes="first"),
                    _release("v2026.8.1", notes="taken already"),
                ],
            )
        )
        status = await UpdateChecker().status()
        assert [c.version for c in status.changes] == ["2026.9.2", "2026.9.1"]

    @pytest.mark.usefixtures("_release_build")
    async def test_the_headline_is_the_newest_version_not_the_newest_row(
        self, httpx2_mock: respx.Router
    ) -> None:
        """GitHub orders /releases by creation date, so a hotfix cut for an older
        line sits FIRST. Trusting position reported that hotfix as the headline and
        suppressed a genuinely newer release for a whole cache TTL; the current
        2026.8.1 build here must still be told about 2026.9.1."""
        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(
                200,
                json=[
                    _release("v2026.7.2", notes="hotfix for the old line"),  # newest ROW
                    _release("v2026.9.1", notes="the real newest"),
                    _release("v2026.8.1", notes="taken already"),
                    _release("v2026.7.1"),
                ],
            )
        )
        status = await UpdateChecker().status()
        assert status.latest == "2026.9.1"
        assert status.update_available is True
        assert status.url is not None and status.url.endswith("v2026.9.1")
        assert [c.version for c in status.changes] == ["2026.9.1"]

    @pytest.mark.usefixtures("_release_build")
    async def test_an_unparseable_head_does_not_blank_the_parseable_rest(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(
                200,
                json=[_release("nightly-special"), _release("v2026.9.1", notes="real")],
            )
        )
        status = await UpdateChecker().status()
        assert status.latest == "2026.9.1"
        assert status.update_available is True
        assert [c.version for c in status.changes] == ["2026.9.1"]

    @pytest.mark.usefixtures("_release_build")
    async def test_the_rolling_dev_prerelease_is_not_a_release(
        self, httpx2_mock: respx.Router
    ) -> None:
        """The dev-build prerelease sits newest in the list; treating it as the
        headline would tell every release operator an update exists nightly."""
        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(
                200,
                json=[_release("dev-build", prerelease=True), _release("v2026.8.1")],
            )
        )
        status = await UpdateChecker().status()
        assert status.latest == "2026.8.1"
        assert status.update_available is False
        assert status.changes == ()

    @pytest.mark.usefixtures("_release_build")
    async def test_the_current_release_reports_no_update(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(200, json=[{"tag_name": "v2026.8.1"}])
        )
        status = await UpdateChecker().status()
        assert status.update_available is False
        assert status.changes == ()
        assert status.url is None  # no html_url in the payload: absent, not invented

    @pytest.mark.usefixtures("_release_build")
    async def test_endless_notes_are_cut_and_say_so(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(
                200, json=[_release("v2026.9.1", notes="x" * (_MAX_NOTES + 1))]
            )
        )
        status = await UpdateChecker().status()
        notes = status.changes[0].notes
        assert notes is not None
        assert len(notes) < _MAX_NOTES + 100
        assert notes.endswith("Read the rest on GitHub.")

    @pytest.mark.usefixtures("_release_build")
    async def test_a_payload_without_releases_reads_as_unknown(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(_RELEASES).mock(return_value=httpx.Response(200, json=[{"name": "x"}]))
        status = await UpdateChecker().status()
        assert status.update_available is None
        assert status.latest is None
        assert status.checked_at is None

    @pytest.mark.usefixtures("_release_build")
    async def test_an_unreachable_check_reads_as_unknown_and_is_not_hammered(
        self, httpx2_mock: respx.Router
    ) -> None:
        """A failure is an "unknown", and it is held briefly: one broken check must
        not turn every About view into a fresh network attempt."""
        httpx2_mock.get(_RELEASES).mock(return_value=httpx.Response(500))
        clock = [0.0]
        checker = UpdateChecker(clock=lambda: clock[0])

        first = await checker.status()
        assert first.update_available is None
        assert first.checked_at is None

        clock[0] = 60.0  # within the failure hold
        await checker.status()
        assert len(httpx2_mock.calls) == 1

        clock[0] = 16 * 60.0  # past it: the check tries again
        await checker.status()
        assert len(httpx2_mock.calls) == 2

    @pytest.mark.usefixtures("_release_build")
    async def test_a_successful_answer_is_cached_for_hours(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(200, json=[{"tag_name": "v2026.8.1"}])
        )
        clock = [0.0]
        checker = UpdateChecker(clock=lambda: clock[0])

        await checker.status()
        clock[0] = 3 * 3600.0
        await checker.status()
        assert len(httpx2_mock.calls) == 1

        clock[0] = 7 * 3600.0
        await checker.status()
        assert len(httpx2_mock.calls) == 2

    @pytest.mark.usefixtures("_release_build")
    async def test_disabled_means_no_request_leaves_the_box(
        self, httpx2_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The off switch governs the network call itself, and its answer is never
        cached -- flipping it back on answers on the next request, not after a TTL."""
        monkeypatch.setenv("REAPER_UPDATE_CHECK", "false")
        checker = UpdateChecker()
        status = await checker.status()
        assert status.enabled is False
        assert status.update_available is None
        assert len(httpx2_mock.calls) == 0

        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(200, json=[{"tag_name": "v2026.8.1"}])
        )
        monkeypatch.setenv("REAPER_UPDATE_CHECK", "true")
        status = await checker.status()
        assert status.enabled is True
        assert len(httpx2_mock.calls) == 1

    @pytest.mark.usefixtures("_release_build")
    async def test_refresh_asks_through_a_fresh_cache_and_status_still_holds_it(
        self, httpx2_mock: respx.Router
    ) -> None:
        """The scheduled job and Run now take ``refresh``, which must ask even when the
        cache is warm: a check somebody scheduled that answered out of a six-hour-old
        cache would record "ran just now" over a stale answer (#464). The route's
        ``status`` keeps the TTL, so a page load never turns into a fresh call, and the
        answer ``refresh`` stores is what that page load then reads."""
        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(200, json=[{"tag_name": "v2026.8.1"}])
        )
        clock = [0.0]
        checker = UpdateChecker(clock=lambda: clock[0])

        await checker.status()
        assert len(httpx2_mock.calls) == 1

        # Well inside the six-hour hold, where `status` would serve the cache.
        clock[0] = 60.0
        await checker.refresh()
        assert len(httpx2_mock.calls) == 2

        # And the forced answer re-armed the hold rather than leaving it expired.
        await checker.status()
        assert len(httpx2_mock.calls) == 2

    @pytest.mark.usefixtures("_release_build")
    async def test_refresh_sends_nothing_while_the_check_is_off(
        self, httpx2_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rule 55: the off switch governs every path that runs the job, and the job's
        path is this one. An off check that still asked from a timer would be the one
        request an operator explicitly turned off, made on a schedule they never see."""
        monkeypatch.setenv("REAPER_UPDATE_CHECK", "false")
        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(200, json=[{"tag_name": "v2026.9.1"}])
        )
        status = await UpdateChecker().refresh()
        assert status.enabled is False
        assert status.update_available is None
        assert len(httpx2_mock.calls) == 0

    @pytest.mark.usefixtures("_release_build")
    async def test_a_configured_repo_is_asked_and_a_malformed_one_is_not(
        self, httpx2_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The slug is spliced into a URL path, so anything that is not owner/name
        falls back to the default rather than being sent."""
        monkeypatch.setenv("REAPER_UPDATE_REPO", "fork-owner/reaper")
        forked = httpx2_mock.get("https://api.github.com/repos/fork-owner/reaper/releases").mock(
            return_value=httpx.Response(200, json=[{"tag_name": "v2026.8.1"}])
        )
        await UpdateChecker().status()
        assert len(forked.calls) == 1

        monkeypatch.setenv("REAPER_UPDATE_REPO", "../..")
        upstream = httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(200, json=[{"tag_name": "v2026.8.1"}])
        )
        await UpdateChecker().status()
        assert len(upstream.calls) == 1


class TestDebugNarration:
    """Every call says which of the three things it did, at DEBUG.

    The check is demand-driven and cached for hours, so "is it checking at all?" has
    no other answer: an idle server with nobody on the About surface makes no request
    and, without these lines, leaves no trace either way.
    """

    @staticmethod
    def _mine(logs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return [entry for entry in logs if str(entry["event"]).startswith("update_check.")]

    @pytest.mark.usefixtures("_release_build")
    async def test_an_ask_is_narrated_before_and_after_the_call(
        self, httpx2_mock: respx.Router
    ) -> None:
        """Before as well as after: an ``asking`` with no ``answered`` beside it is
        the only shape a hung GitHub call takes in the log."""
        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(200, json=[_release("v2026.9.1")])
        )
        with capture_logs() as logs:
            await UpdateChecker().status()

        asked, answered = self._mine(logs)
        assert asked["event"] == "update_check.asking"
        assert asked["log_level"] == "debug"
        assert asked["repo"] == DEFAULT_REPO
        assert asked["channel"] == "release"
        assert asked["current"] == "2026.8.1"
        assert answered["event"] == "update_check.answered"
        assert answered["log_level"] == "debug"
        assert answered["latest"] == "2026.9.1"
        assert answered["update_available"] is True
        assert answered["next_ask_in"] == 6 * 3600

    @pytest.mark.usefixtures("_release_build")
    async def test_a_held_answer_says_so_and_how_long_it_holds(
        self, httpx2_mock: respx.Router
    ) -> None:
        """A quiet check and a cached one look identical from outside the process."""
        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(200, json=[_release("v2026.9.1")])
        )
        clock = [0.0]
        checker = UpdateChecker(clock=lambda: clock[0])
        await checker.status()

        clock[0] = 2 * 3600.0
        with capture_logs() as logs:
            await checker.status()

        (held,) = self._mine(logs)
        assert held["event"] == "update_check.cached"
        assert held["log_level"] == "debug"
        assert held["latest"] == "2026.9.1"
        assert held["held_for"] == 4 * 3600
        assert len(httpx2_mock.calls) == 1

    @pytest.mark.usefixtures("_release_build")
    async def test_a_failed_ask_names_the_shorter_retry(self, httpx2_mock: respx.Router) -> None:
        """The retry pause is the number an operator watching a failure wants, and
        the INFO failure line does not carry it."""
        httpx2_mock.get(_RELEASES).mock(return_value=httpx.Response(500))
        with capture_logs() as logs:
            await UpdateChecker().status()

        answered = self._mine(logs)[-1]
        assert answered["event"] == "update_check.answered"
        assert answered["latest"] is None
        assert answered["update_available"] is None
        assert answered["next_ask_in"] == 15 * 60

    @pytest.mark.usefixtures("_release_build")
    async def test_the_off_switch_is_narrated_rather_than_silent(
        self, httpx2_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REAPER_UPDATE_CHECK", "false")
        with capture_logs() as logs:
            await UpdateChecker().status()

        (off,) = self._mine(logs)
        assert off["event"] == "update_check.disabled"
        assert off["log_level"] == "debug"
        assert len(httpx2_mock.calls) == 0


class TestDevChannel:
    """The dev channel follows the published :dev image, never the branch.

    CI builds an image only when a push touched code, so the branch runs ahead of the
    image on every docs or rules merge. Reading the tip announced an update those
    operators could not pull, and pointed them at a page of commits that shipped
    nothing.
    """

    @pytest.mark.usefixtures("_dev_build")
    async def test_a_newer_image_is_reported(self, httpx2_mock: respx.Router) -> None:
        _publish_dev_image(httpx2_mock, {"amd64": "def5678", "arm64": "def5678"})
        status = await UpdateChecker().status()
        assert status.channel == "dev"
        assert status.current == "dev (abc1234)"
        assert status.latest == "dev (def5678)"
        assert status.update_available is True

    @pytest.mark.usefixtures("_dev_build")
    async def test_the_image_this_build_came_from_reports_no_update(
        self, httpx2_mock: respx.Router
    ) -> None:
        _publish_dev_image(httpx2_mock, {"amd64": "abc1234", "arm64": "abc1234"})
        status = await UpdateChecker().status()
        assert status.update_available is False

    @pytest.mark.usefixtures("_dev_build")
    async def test_the_branch_is_never_asked(self, httpx2_mock: respx.Router) -> None:
        """The regression this change exists for. A docs merge moves the branch and
        publishes no image, so the branch tip cannot be consulted at all -- not even as
        a tiebreak, which would put the old answer back for exactly those pushes."""
        tip = httpx2_mock.get(_DEV_TIP).mock(
            return_value=httpx.Response(200, json={"sha": "def5678" + "0" * 33})
        )
        _publish_dev_image(httpx2_mock, {"amd64": "abc1234", "arm64": "abc1234"})
        status = await UpdateChecker().status()
        assert status.update_available is False
        assert len(tip.calls) == 0

    @pytest.mark.usefixtures("_dev_build")
    async def test_the_link_holds_only_the_commits_an_update_would_bring(
        self, httpx2_mock: respx.Router
    ) -> None:
        _publish_dev_image(httpx2_mock, {"amd64": "def5678", "arm64": "def5678"})
        status = await UpdateChecker().status()
        assert status.url == f"https://github.com/{DEFAULT_REPO}/compare/abc1234...def5678"

    @pytest.mark.usefixtures("_dev_build")
    async def test_each_architecture_reads_its_own_half(
        self, httpx2_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """amd64 is rebuilt on every code push and arm64 nightly, so the two halves of
        :dev routinely name different commits. Reading amd64's on an arm64 box would
        nag that operator toward an image that does not exist for them yet."""
        monkeypatch.setattr("reaper.services.update_check.platform.machine", lambda: "aarch64")
        _publish_dev_image(httpx2_mock, {"amd64": "def5678", "arm64": "abc1234"})
        status = await UpdateChecker().status()
        assert status.latest == "dev (abc1234)"
        assert status.update_available is False

    @pytest.mark.usefixtures("_dev_build")
    async def test_a_single_architecture_tag_is_read_directly(
        self, httpx2_mock: respx.Router
    ) -> None:
        """Before the first nightly there is no arm64 half, so :dev is a plain manifest
        rather than an index and there is no child to pick."""
        _publish_dev_image(httpx2_mock, {"amd64": "def5678"}, multi_arch=False)
        status = await UpdateChecker().status()
        assert status.latest == "dev (def5678)"
        assert status.update_available is True

    @pytest.mark.usefixtures("_dev_build")
    async def test_an_architecture_with_no_image_reads_as_unknown(
        self, httpx2_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is published for this machine, so both verdicts are wrong."""
        monkeypatch.setattr("reaper.services.update_check.platform.machine", lambda: "aarch64")
        _publish_dev_image(httpx2_mock, {"amd64": "def5678"})
        status = await UpdateChecker().status()
        assert status.update_available is None
        assert status.checked_at is None

    @pytest.mark.usefixtures("_dev_build")
    async def test_an_image_that_names_no_commit_reads_as_unknown(
        self, httpx2_mock: respx.Router
    ) -> None:
        _publish_dev_image(httpx2_mock, {"amd64": None, "arm64": None})
        status = await UpdateChecker().status()
        assert status.update_available is None
        assert status.checked_at is None

    @pytest.mark.usefixtures("_dev_build")
    async def test_a_build_that_cannot_name_its_commit_reads_as_unknown(
        self, httpx2_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No baked sha and no readable .git: the image's commit is still shown, but
        "update available" is unknown -- a nag that is almost always true helps nobody.
        With no commit to anchor a range on, the link falls back to the commit list."""
        monkeypatch.setattr("reaper.services.update_check.short_commit", lambda: None)
        _publish_dev_image(httpx2_mock, {"amd64": "def5678", "arm64": "def5678"})
        status = await UpdateChecker().status()
        assert status.latest == "dev (def5678)"
        assert status.update_available is None
        assert status.checked_at is not None
        assert status.url == f"https://github.com/{DEFAULT_REPO}/commits/dev"

    @pytest.mark.usefixtures("_dev_build")
    async def test_a_fork_follows_its_own_image(
        self, httpx2_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The registry path is lowercased because Docker requires it, and a repo owner
        is commonly capitalized."""
        monkeypatch.setenv("REAPER_UPDATE_REPO", "Fork-Owner/Reaper")
        httpx2_mock.get("https://ghcr.io/token").mock(
            return_value=httpx.Response(200, json={"token": "t"})
        )
        forked = httpx2_mock.get("https://ghcr.io/v2/fork-owner/reaper/manifests/dev").mock(
            return_value=httpx.Response(200, json={"config": {"digest": _CONFIG_DIGEST["amd64"]}})
        )
        httpx2_mock.get(
            f"https://ghcr.io/v2/fork-owner/reaper/blobs/{_CONFIG_DIGEST['amd64']}"
        ).mock(
            return_value=httpx.Response(200, json={"config": {"Env": ["REAPER_GIT_SHA=def5678"]}})
        )
        status = await UpdateChecker().status()
        assert len(forked.calls) == 1
        assert status.latest == "dev (def5678)"


class TestRoute:
    @pytest.mark.usefixtures("_dev_build")
    def test_the_route_answers_from_the_shared_checker(
        self, client: TestClient, httpx2_mock: respx.Router
    ) -> None:
        _publish_dev_image(httpx2_mock, {"amd64": "def5678", "arm64": "def5678"})
        body = client.get("/api/about/update").json()
        assert body["channel"] == "dev"
        assert body["enabled"] is True
        assert body["current"] == "dev (abc1234)"
        assert body["latest"] == "dev (def5678)"
        assert body["update_available"] is True
        assert body["url"].endswith("/compare/abc1234...def5678")
        assert body["checked_at"] is not None

    @pytest.mark.usefixtures("_dev_build")
    def test_an_unreachable_check_still_answers_the_page(
        self, client: TestClient, httpx2_mock: respx.Router
    ) -> None:
        """The route never fails a page over a network problem: unknown, not 502."""
        httpx2_mock.get("https://ghcr.io/token").mock(return_value=httpx.Response(500))
        response = client.get("/api/about/update")
        assert response.status_code == 200
        body = response.json()
        assert body["update_available"] is None
        assert body["current"] == "dev (abc1234)"


class TestOnlyAnExplicitFalseTurnsTheCheckOff:
    """``update_check._enabled`` used to read ``raw not in _FALSE``, so anything the
    vocabulary did not recognize left the check ON. It reads ``env_flag(default=True)``
    now, and the table below is what says the two agree on every input -- written out
    rather than re-deriving the retired expression (rule 119).

    The same change moved the tray the other way: an unrecognized value there now falls to
    its default instead of to False. This test is what keeps that widening from reaching a
    check that leaves the operator's network.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, True),  # nothing set: the check is on, as it shipped
            ("", True),
            ("   ", True),
            ("false", False),
            ("FALSE", False),
            ("0", False),
            ("no", False),
            ("off", False),
            ("true", True),
            ("ture", True),  # a typo does not silently turn it off, and never did
            ("2", True),
        ],
    )
    def test_the_vocabulary_did_not_move(
        self, monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool
    ) -> None:
        if raw is None:
            monkeypatch.delenv("REAPER_UPDATE_CHECK", raising=False)
        else:
            monkeypatch.setenv("REAPER_UPDATE_CHECK", raw)
        assert _enabled() is expected
