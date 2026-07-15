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
