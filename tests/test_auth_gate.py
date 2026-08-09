# SPDX-License-Identifier: AGPL-3.0-or-later
"""The gate in front of the whole API.

The threat is concrete: before this, the API had no auth at all -- anyone who could
reach the port could drive a tool that deletes a media library. These tests hold
the line that every ``/api`` route requires a session, that the handful which must
work logged-out are exactly the handful named, and that a cross-site page cannot
forge a state-changing request.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from reaper.api.middleware import AuthGuard
from reaper.config import Settings
from reaper.main import create_app

from ._auth import TEST_ADMIN, TEST_PASSWORD, seed_admin

CSRF = {"X-Reaper-CSRF": "1"}


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


class TestTheGateIsClosedByDefault:
    def test_an_api_route_requires_a_session(self, client: TestClient) -> None:
        assert client.get("/api/snapshots/latest").status_code == 401

    def test_the_health_probe_stays_open(self, client: TestClient) -> None:
        """Liveness checks run unauthenticated."""
        assert client.get("/api/health").status_code == 200

    def test_the_auth_context_stays_open(self, client: TestClient) -> None:
        """The login screen needs to render before anyone has a session."""
        response = client.get("/api/auth/context")
        assert response.status_code == 200
        body = response.json()
        assert body["setup_needed"] is True  # fresh DB, no admins yet
        assert body["plex_linked"] is False

    def test_me_is_401_when_logged_out(self, client: TestClient) -> None:
        assert client.get("/api/auth/me").status_code == 401


class TestCsrf:
    def test_a_state_changing_request_without_the_header_is_blocked(
        self, client: TestClient
    ) -> None:
        """No custom header -> 403, before authentication is even considered. A
        cross-origin page cannot set the header without a preflight this server
        never grants."""
        response = client.post("/api/auth/local", json={"username": "x", "password": "y"})
        assert response.status_code == 403

    def test_a_cross_site_fetch_metadata_is_blocked(self, client: TestClient) -> None:
        response = client.post(
            "/api/auth/local",
            json={"username": "x", "password": "y"},
            headers={**CSRF, "Sec-Fetch-Site": "cross-site"},
        )
        assert response.status_code == 403

    def test_a_safe_method_needs_no_csrf(self, client: TestClient) -> None:
        assert client.get("/api/health").status_code == 200


class TestLocalLoginFlow:
    def test_sign_in_then_reach_the_api(self, client: TestClient, settings: Settings) -> None:
        seed_admin(settings)

        bad = client.post(
            "/api/auth/local",
            json={"username": TEST_ADMIN, "password": "nope"},
            headers=CSRF,
        )
        assert bad.status_code == 401

        ok = client.post(
            "/api/auth/local",
            json={"username": TEST_ADMIN, "password": TEST_PASSWORD},
            headers=CSRF,
        )
        assert ok.status_code == 200
        assert ok.json()["username"] == TEST_ADMIN

        # The cookie now rides along, so guarded routes open up.
        assert client.get("/api/auth/me").json()["username"] == TEST_ADMIN
        assert client.get("/api/snapshots/latest").status_code in (200, 404)

    def test_logout_revokes_the_session(self, client: TestClient, settings: Settings) -> None:
        seed_admin(settings)
        client.post(
            "/api/auth/local",
            json={"username": TEST_ADMIN, "password": TEST_PASSWORD},
            headers=CSRF,
        )
        assert client.get("/api/auth/me").status_code == 200

        signed_out = client.post("/api/auth/logout", headers=CSRF)
        assert signed_out.status_code == 200
        assert signed_out.json() == {"ok": True}
        assert client.get("/api/auth/me").status_code == 401


class TestOnlyHttpGetsThroughTheGate:
    """The guard authenticates by reading headers and cookies off an ``http`` scope, so
    it refuses every other kind rather than passing it through (L-1).

    Reaper declares no websocket route, so nothing is turned away that ever worked. The
    point is the day someone adds one: it must not be born with no session check and no
    CSRF, silently, because the guard waved through everything that was not ``http``.
    """

    def test_a_websocket_route_behind_the_guard_never_runs(self) -> None:
        reached = False

        async def spy(websocket: WebSocket) -> None:  # pragma: no cover - must not run
            nonlocal reached
            reached = True
            await websocket.accept()

        app = Starlette(routes=[WebSocketRoute("/api/ws", spy)])
        app.add_middleware(AuthGuard)

        with (
            TestClient(app) as client,
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect("/api/ws"),
        ):
            pass  # pragma: no cover - the connect above never succeeds

        # 1008 is the guard's refusal. Starlette's own not-found close is 1000, so this
        # pins that the guard turned it away *before* routing, not that the route missed.
        assert refused.value.code == 1008
        assert reached is False, "the guard handed a websocket to the app unauthenticated"

    def test_lifespan_still_reaches_the_app(self, settings: Settings) -> None:
        """The one non-http scope that must pass: startup/shutdown carries no session,
        and the app never boots if the guard eats it. Entering the context runs it."""
        with TestClient(create_app(settings)) as client:
            assert client.get("/api/health").status_code == 200
