# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pre-reap Plex trash warning's evidence endpoint.

Reaper's end-of-run purge is section-wide, so it destroys the library records of
everything already in the trash, not just what the run deleted. Those items sit on both
sides of the executor's before/after count and cancel out of its gate, so this endpoint is
the only thing that can see them. Every branch here resolves toward telling the operator
something rather than reporting a reassuring zero.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from reaper.api import plex_trash
from reaper.clients.plex import PlexError
from reaper.crypto import SecretBox
from reaper.services import app_settings

from . import test_api

# The shared authenticated app fixture, re-exported rather than imported by name: a bare
# `from .test_api import client` is read as a redefinition the moment a test declares
# `client` as a parameter, which is every test here.
client = test_api.client


class _FakePlex:
    """Answers per section, so one library can fail while another succeeds."""

    def __init__(
        self,
        trash: dict[int, int | Exception],
        totals: dict[int, int] | None = None,
        auto_empty: bool | Exception = False,
    ) -> None:
        self._trash = trash
        self._totals = totals or {}
        self._auto = auto_empty
        self.closed = False

    async def trash_count(self, section_key: int) -> int:
        answer = self._trash[section_key]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def item_count(self, section_key: int) -> int:
        return self._totals.get(section_key, 10_000)

    async def empties_trash_after_scan(self) -> bool:
        if isinstance(self._auto, Exception):
            raise self._auto
        return self._auto

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def plex(monkeypatch: pytest.MonkeyPatch) -> list[_FakePlex]:
    """Installs whatever _FakePlex the test appends, and records it for close-checking."""
    made: list[_FakePlex] = []

    def build(*_a: Any, **_k: Any) -> _FakePlex:
        return made[0]

    monkeypatch.setattr(plex_trash, "PlexClient", build)
    # The shared fixture's stored token was sealed with a different key, and the route
    # decrypts before it builds a client. The token is not what these tests are about.
    monkeypatch.setattr(SecretBox, "decrypt", lambda self, blob: "token")

    async def libraries(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [
            {"key": 1, "title": "A", "kind": "movie", "enabled": True},
            {"key": 2, "title": "B", "kind": "show", "enabled": True},
            # Turned off: Reaper never refreshes it, so its trash is not this run's business.
            {"key": 3, "title": "C", "kind": "movie", "enabled": False},
        ]

    monkeypatch.setattr(app_settings, "get_plex_libraries", libraries)
    return made


class TestTheTrashWarningEvidence:
    def test_it_sums_only_the_libraries_included_in_scans(
        self, client: TestClient, plex: list[_FakePlex]
    ) -> None:
        plex.append(_FakePlex({1: 30, 2: 10, 3: 999}))
        body = client.get("/api/reap/plex-trash").json()
        # 3 is disabled, so its 999 must not appear.
        assert body == {
            "configured": True,
            "trashed": 40,
            "sections_unreadable": 0,
            "empties_after_scan": False,
        }
        assert plex[0].closed, "the client must be closed on the success path (rule 34)"

    def test_an_unreadable_library_is_counted_not_swallowed(
        self, client: TestClient, plex: list[_FakePlex]
    ) -> None:
        """A library that could not be read makes ``trashed`` a floor, and the page warns on
        ``sections_unreadable`` rather than reading silence as an empty trash (rule 93)."""
        plex.append(_FakePlex({1: 5, 2: PlexError("timed out")}))
        body = client.get("/api/reap/plex-trash").json()
        assert body["trashed"] == 5
        assert body["sections_unreadable"] == 1

    def test_a_server_that_ignores_the_filter_is_unreadable_not_alarming(
        self, client: TestClient, plex: list[_FakePlex]
    ) -> None:
        """``trash=1`` was confirmed against a live server against a control: an unknown
        parameter comes back with the WHOLE library while trash=1 narrows. A server that
        does not know the filter therefore answers with the library size, which would render
        as a huge, wrong trash count beside the operator's most dangerous button. Equality
        with the section's own item count means we cannot tell the two apart, so it is
        reported as unreadable."""
        plex.append(_FakePlex({1: 500, 2: 3}, totals={1: 500, 2: 900}))
        body = client.get("/api/reap/plex-trash").json()
        assert body["trashed"] == 3, "the honest library still counts"
        assert body["sections_unreadable"] == 1

    def test_an_empty_trash_says_so_plainly(
        self, client: TestClient, plex: list[_FakePlex]
    ) -> None:
        """The quiet case has to stay quiet: a warning that fires on every reap stops being
        read. Zero with nothing unreadable is what the page renders nothing from."""
        plex.append(_FakePlex({1: 0, 2: 0}))
        body = client.get("/api/reap/plex-trash").json()
        assert body["trashed"] == 0
        assert body["sections_unreadable"] == 0

    def test_an_unreadable_preference_is_null_not_false(
        self, client: TestClient, plex: list[_FakePlex]
    ) -> None:
        """Reporting "no" for a setting we could not read would tell the operator Reaper's
        own trash interlock is in force when it may not be."""
        plex.append(_FakePlex({1: 0, 2: 0}, auto_empty=PlexError("no such setting")))
        assert client.get("/api/reap/plex-trash").json()["empties_after_scan"] is None
