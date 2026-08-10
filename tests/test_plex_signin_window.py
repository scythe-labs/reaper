# SPDX-License-Identifier: AGPL-3.0-or-later
"""Closing the Plex sign-in window, without letting plex.tv reach the page that takes
the operator's Reaper password.

The window is opened with ``noopener``, so plex.tv holds no handle on Reaper's sign-in
page -- and Reaper holds no handle on the window either, because that is the same
relationship. It was read as a trade for a while: keep the operator safe OR close the
window (#372, and the comment this replaced in ``Login.tsx``). It is not one. A
script-opened window may close *itself* with no opener at all, so the close moves into
the window: plex.tv is told to forward it to Reaper's own page, and that page closes it.

What is pinned here is the half a Python test can see -- that both start routes put the
browser's own address into the URL handed to plex.tv. The page's own ``window.close()``
is pinned by ``test_repo_hygiene``, which also checks the two agree on the path.
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

    plex.tv's auth page takes its parameters in the FRAGMENT, so this parses the part
    after ``#``: reading the query would find nothing and quietly pass.
    """
    fragment = urlparse(auth_url).fragment.lstrip("?")
    values = parse_qs(fragment).get("forwardUrl")
    return values[0] if values else None


class TestTheOriginTheBrowserNames:
    """``PlexStartIn`` takes an origin and appends the path itself.

    The browser has to name the address because the server cannot: Vite's dev proxy and
    any reverse proxy rewrite ``Host``, so a URL built from the request forwards the
    window to somewhere the operator is not. Taking an origin rather than a URL is what
    keeps a caller able to name a host but never a target.
    """

    def test_an_origin_becomes_the_forward_address(self) -> None:
        assert PlexStartIn(forward_origin=ORIGIN).forward_url() == ORIGIN + PLEX_FORWARD_PATH

    def test_no_origin_forwards_nowhere(self) -> None:
        """An older cached SPA calls without one. The sign-in still works; its window
        just stays open, exactly as before this existed."""
        assert PlexStartIn().forward_url() is None

    @pytest.mark.parametrize(
        "value",
        [
            "https://reaper.example.net/somewhere",  # a path: this names a target
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
    """Sign-in and the Settings re-link are the same flow twice, and the window is left
    open by whichever one does not carry the address (rule 72)."""

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
        """The window not closing must never be the reason a sign-in cannot start, so the
        body stays optional: a cached SPA from before this change sends none at all."""
        response = client.post("/api/auth/plex/start", headers=CSRF)

        assert response.status_code == 200, response.text
        assert _forward_url(response.json()["auth_url"]) is None
        # The other half of this route's response body. Nothing else reads it back over
        # HTTP, so renaming the field would break `api.ts`'s poll call with the backend
        # suite green.
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
    """The other property the two start routes hold apart, on the same rule 72 footing.

    ``purpose`` decides which poller can spend the row, and the sign-in poller is reached
    from an open route where the link one is not. It used to be a literal inside each
    flow's own function; both functions are now one ``start_pin`` and the literal moved
    out to these two calls, one keyword apart. So the value is pinned where it is now
    written, at the routes, and not only at the pollers that read it.

    Both routes are swept, because a shared helper hardcoding either value is green
    against the route that wanted that value (rule 141).
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
