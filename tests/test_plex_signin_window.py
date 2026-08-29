# SPDX-License-Identifier: AGPL-3.0-or-later
"""Closes the Plex sign-in window without letting plex.tv reach Reaper's sign-in page.

The window opens with ``noopener``, so plex.tv holds no handle on Reaper's page, and
Reaper holds no handle on the window either. Because neither side holds a handle, closing
the window from outside is not possible. A script-opened window can still close itself, so
the close happens from inside: plex.tv forwards the window to Reaper's own page, and that
page closes itself.

This file pins the half a Python test can see: both start routes put the browser's own
address into the URL handed to plex.tv. ``test_repo_hygiene`` pins the page's own
``window.close()`` call and checks that the two agree on the path.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import select

from reaper.api.schemas import PLEX_FORWARD_PATH, PlexStartIn
from reaper.config import Settings
from reaper.db.models import PendingPlexLogin
from reaper.main import create_app

from ._auth import login

pytestmark = pytest.mark.httpx2(assert_all_called=False)

ORIGIN = "https://reaper.example.net"
CSRF = {"X-Reaper-CSRF": "1"}


def _forward_url(auth_url: str) -> str | None:
    """The ``forwardUrl`` plex.tv is being handed, or None if there is none.

    plex.tv's auth page takes its parameters in the fragment, after ``#``, not in the
    query string. Reading the query string instead would find nothing and quietly pass.
    """
    fragment = urlparse(auth_url).fragment.lstrip("?")
    values = parse_qs(fragment).get("forwardUrl")
    return values[0] if values else None


class TestTheOriginTheBrowserNames:
    """``PlexStartIn`` takes an origin and appends the path itself.

    The browser has to name the address because the server cannot. Vite's dev proxy and
    any reverse proxy rewrite ``Host``, so a URL built from the request would forward the
    window to somewhere the operator is not. Taking an origin rather than a full URL lets
    a caller name a host, but never a target path.
    """

    def test_an_origin_becomes_the_forward_address(self) -> None:
        assert PlexStartIn(forward_origin=ORIGIN).forward_url() == ORIGIN + PLEX_FORWARD_PATH

    def test_no_origin_forwards_nowhere(self) -> None:
        """An older cached SPA calls without an origin, and the sign-in still works.

        Its window just stays open instead of closing itself.
        """
        assert PlexStartIn().forward_url() is None

    @pytest.mark.parametrize(
        "value",
        [
            "https://reaper.example.net/somewhere",  # a path also names a target
            "https://reaper.example.net?next=x",  # so does a query
            "https://reaper.example.net#frag",
            "javascript:alert(1)",  # not a browser origin at all
            "file:///etc/passwd",
            "reaper.example.net",  # no scheme, so no origin
            "",
        ],
    )
    def test_anything_that_is_not_a_bare_http_origin_is_refused(self, value: str) -> None:
        with pytest.raises(ValueError):
            PlexStartIn(forward_origin=value)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def pins(httpx2_mock: respx.Router) -> respx.Route:
    return httpx2_mock.post("https://plex.tv/api/v2/pins").mock(
        return_value=httpx.Response(201, json={"id": 77, "code": "ABCD"})
    )


class TestBothStartRoutesForwardTheWindowHome:
    """Sign-in and the Settings re-link are the same flow twice, and either one leaves the
    window open if it skips the browser's address."""

    def test_sign_in_hands_plex_the_browsers_address(
        self, client: TestClient, pins: respx.Route
    ) -> None:
        body = client.post(
            "/api/auth/plex/start", json={"forward_origin": ORIGIN}, headers=CSRF
        ).json()

        assert _forward_url(body["auth_url"]) == ORIGIN + PLEX_FORWARD_PATH

    def test_the_settings_relink_hands_plex_the_same_address(
        self, client: TestClient, settings: Settings, pins: respx.Route
    ) -> None:
        login(client, settings)
        body = client.post("/api/settings/plex/link/start", json={"forward_origin": ORIGIN}).json()

        assert _forward_url(body["auth_url"]) == ORIGIN + PLEX_FORWARD_PATH

    def test_a_caller_that_names_no_origin_still_signs_in(
        self, client: TestClient, pins: respx.Route
    ) -> None:
        """A sign-in must still work even if the window never closes itself.

        The origin field stays optional because an older cached SPA can send none at all.
        """
        response = client.post("/api/auth/plex/start", headers=CSRF)

        assert response.status_code == 200, response.text
        assert _forward_url(response.json()["auth_url"]) is None
        # The other half of this route's response body. Only `api.ts`'s poll call reads
        # `pin_id`, so renaming the field would break the frontend while the backend
        # suite stayed green.
        assert response.json()["pin_id"] == 77

    def test_an_origin_naming_a_target_is_refused(
        self, client: TestClient, pins: respx.Route
    ) -> None:
        response = client.post(
            "/api/auth/plex/start",
            json={"forward_origin": "https://reaper.example.net/anywhere"},
            headers=CSRF,
        )

        assert response.status_code == 422
        assert pins.call_count == 0, "refused before asking plex.tv for a PIN"


def _stored_purpose(settings: Settings) -> str:
    engine = sa_create_engine(settings.sync_database_url)
    try:
        with engine.connect() as conn:
            return str(conn.execute(select(PendingPlexLogin.purpose)).scalar_one())
    finally:
        engine.dispose()


class TestEachStartRouteClaimsItsOwnPurpose:
    """The other property the two start routes must keep separate.

    ``purpose`` decides which poller can consume the pending-login row. The sign-in
    poller is reached from an open route, and the settings-relink poller is not, so a
    wrong purpose would let the wrong poller claim a row. Both routes share one
    ``start_pin`` function and pass ``purpose`` as an argument, so this test pins the
    value at the call site, not only at the poller that reads it.

    Both routes are checked, because a shared helper hardcoding one value would still
    pass a test that only checked the route that wanted that value.
    """

    def test_signing_in_writes_a_login_pin(
        self, client: TestClient, settings: Settings, pins: respx.Route
    ) -> None:
        assert client.post("/api/auth/plex/start", headers=CSRF).status_code == 200

        assert _stored_purpose(settings) == "login"

    def test_the_settings_relink_writes_a_link_pin(
        self, client: TestClient, settings: Settings, pins: respx.Route
    ) -> None:
        login(client, settings)
        assert client.post("/api/settings/plex/link/start").status_code == 200

        assert _stored_purpose(settings) == "link"
