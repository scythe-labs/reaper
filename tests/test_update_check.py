# SPDX-License-Identifier: AGPL-3.0-or-later
"""The update check: channel selection, version ordering, caching, and the route.

The property pinned hardest: a check that cannot answer -- disabled, unreachable,
malformed, unorderable -- answers "unknown" (``update_available=None``), never an
error and never a guess. This surface informs; nothing may gate on it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine

from reaper.config import Settings
from reaper.db.base import Base
from reaper.main import create_app
from reaper.services.update_check import DEFAULT_REPO, UpdateChecker, _newer

from ._auth import login

pytestmark = pytest.mark.httpx2(assert_all_called=False)

_RELEASES = f"https://api.github.com/repos/{DEFAULT_REPO}/releases/latest"
_DEV_TIP = f"https://api.github.com/repos/{DEFAULT_REPO}/commits/dev"


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


class TestReleaseChannel:
    @pytest.mark.usefixtures("_release_build")
    async def test_a_newer_release_is_reported(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(
                200,
                json={
                    "tag_name": "v2026.9.1",
                    "html_url": f"https://github.com/{DEFAULT_REPO}/releases/tag/v2026.9.1",
                },
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

    @pytest.mark.usefixtures("_release_build")
    async def test_the_current_release_reports_no_update(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(200, json={"tag_name": "v2026.8.1"})
        )
        status = await UpdateChecker().status()
        assert status.update_available is False
        assert status.url is None  # no html_url in the payload: absent, not invented

    @pytest.mark.usefixtures("_release_build")
    async def test_a_payload_without_a_tag_reads_as_unknown(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(_RELEASES).mock(return_value=httpx.Response(200, json={"name": "x"}))
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
            return_value=httpx.Response(200, json={"tag_name": "v2026.8.1"})
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
            return_value=httpx.Response(200, json={"tag_name": "v2026.8.1"})
        )
        monkeypatch.setenv("REAPER_UPDATE_CHECK", "true")
        status = await checker.status()
        assert status.enabled is True
        assert len(httpx2_mock.calls) == 1

    @pytest.mark.usefixtures("_release_build")
    async def test_a_configured_repo_is_asked_and_a_malformed_one_is_not(
        self, httpx2_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The slug is spliced into a URL path, so anything that is not owner/name
        falls back to the default rather than being sent."""
        monkeypatch.setenv("REAPER_UPDATE_REPO", "fork-owner/reaper")
        forked = httpx2_mock.get(
            "https://api.github.com/repos/fork-owner/reaper/releases/latest"
        ).mock(return_value=httpx.Response(200, json={"tag_name": "v2026.8.1"}))
        await UpdateChecker().status()
        assert len(forked.calls) == 1

        monkeypatch.setenv("REAPER_UPDATE_REPO", "../repos/evil")
        upstream = httpx2_mock.get(_RELEASES).mock(
            return_value=httpx.Response(200, json={"tag_name": "v2026.8.1"})
        )
        await UpdateChecker().status()
        assert len(upstream.calls) == 1


class TestDevChannel:
    @pytest.mark.usefixtures("_dev_build")
    async def test_a_moved_dev_branch_is_reported(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get(_DEV_TIP).mock(
            return_value=httpx.Response(
                200,
                json={
                    "sha": "def5678" + "0" * 33,
                    "html_url": f"https://github.com/{DEFAULT_REPO}/commit/def5678",
                },
            )
        )
        status = await UpdateChecker().status()
        assert status.channel == "dev"
        assert status.current == "dev (abc1234)"
        assert status.latest == "dev (def5678)"
        assert status.update_available is True

    @pytest.mark.usefixtures("_dev_build")
    async def test_the_current_dev_build_reports_no_update(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get(_DEV_TIP).mock(
            return_value=httpx.Response(200, json={"sha": "abc1234" + "0" * 33})
        )
        status = await UpdateChecker().status()
        assert status.update_available is False

    @pytest.mark.usefixtures("_dev_build")
    async def test_a_build_that_cannot_name_its_commit_reads_as_unknown(
        self, httpx2_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No baked sha and no readable .git: the tip is still shown, but "update
        available" is unknown -- a nag that is almost always true helps nobody."""
        monkeypatch.setattr("reaper.services.update_check.short_commit", lambda: None)
        httpx2_mock.get(_DEV_TIP).mock(
            return_value=httpx.Response(200, json={"sha": "def5678" + "0" * 33})
        )
        status = await UpdateChecker().status()
        assert status.latest == "dev (def5678)"
        assert status.update_available is None
        assert status.checked_at is not None

    @pytest.mark.usefixtures("_dev_build")
    async def test_a_payload_without_a_sha_reads_as_unknown(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(_DEV_TIP).mock(return_value=httpx.Response(200, json={"sha": True}))
        status = await UpdateChecker().status()
        assert status.update_available is None
        assert status.checked_at is None


class TestRoute:
    @pytest.fixture
    def client(self, tmp_path: Path) -> Iterator[TestClient]:
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        engine.dispose()
        with TestClient(create_app(settings)) as c:
            login(c, settings)
            yield c

    @pytest.mark.usefixtures("_dev_build")
    def test_the_route_answers_from_the_shared_checker(
        self, client: TestClient, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(_DEV_TIP).mock(
            return_value=httpx.Response(
                200,
                json={
                    "sha": "def5678" + "0" * 33,
                    "html_url": f"https://github.com/{DEFAULT_REPO}/commit/def5678",
                },
            )
        )
        body = client.get("/api/about/update").json()
        assert body["channel"] == "dev"
        assert body["enabled"] is True
        assert body["current"] == "dev (abc1234)"
        assert body["latest"] == "dev (def5678)"
        assert body["update_available"] is True
        assert body["url"].endswith("/commit/def5678")
        assert body["checked_at"] is not None

    @pytest.mark.usefixtures("_dev_build")
    def test_an_unreachable_check_still_answers_the_page(
        self, client: TestClient, httpx2_mock: respx.Router
    ) -> None:
        """The route never fails a page over a network problem: unknown, not 502."""
        httpx2_mock.get(_DEV_TIP).mock(return_value=httpx.Response(500))
        response = client.get("/api/about/update")
        assert response.status_code == 200
        body = response.json()
        assert body["update_available"] is None
        assert body["current"] == "dev (abc1234)"
