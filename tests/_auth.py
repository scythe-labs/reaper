# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test helper: put a TestClient behind the auth gate.

The API is fronted by authentication (see ``reaper.api.middleware``), so a bare
``client.get("/api/...")`` now gets a 401. Tests that exercise the surface don't
care about *how* they authenticated -- only that they are in -- so this seeds a
local admin and logs the client in, and defaults the CSRF header the frontend
would otherwise send on every request.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.orm import Session

from reaper.auth.passwords import hash_password
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.models import AppUser, AuthProvider

TEST_ADMIN = "tester"
TEST_PASSWORD = "conftest-admin-password"


def seed_admin(settings: Settings) -> None:
    """Insert a local admin directly, so the login endpoint has something to accept."""
    engine = sa_create_engine(settings.sync_database_url)
    with Session(engine) as session:
        session.add(
            AppUser(
                provider=AuthProvider.LOCAL,
                username=TEST_ADMIN,
                password_hash=hash_password(TEST_PASSWORD),
                is_active=True,
                created_at=utcnow(),
            )
        )
        session.commit()
    engine.dispose()


def clear_admin_password(client: TestClient) -> None:
    """Leave the signed-in session standing, but with no admin password behind it.

    The state a Plex-only install is in: the owner claimed the server over OAuth and never set
    one. Nulling the hash rather than deleting the row keeps the cookie valid, because resolving
    a session never touches ``password_hash`` -- so this isolates "no password set" from "not
    signed in", which are different refusals and only one of them is what these tests are about.

    Shared because three routes refuse on it and each was tested from its own copy of this
    (rule 119): arming deletion, confirming a restore, and forgetting the watch record.
    """
    settings: Settings = client.app.state.settings  # type: ignore[attr-defined]
    engine = sa_create_engine(settings.sync_database_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE app_user SET password_hash = NULL"))
    engine.dispose()


def login(client: TestClient, settings: Settings) -> None:
    """Authenticate ``client`` in place: default the CSRF header and sign in.

    The TestClient keeps a cookie jar, so the session cookie set here rides along
    on every subsequent request.
    """
    seed_admin(settings)
    client.headers["X-Reaper-CSRF"] = "1"
    response = client.post(
        "/api/auth/local", json={"username": TEST_ADMIN, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
