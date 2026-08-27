# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests the way back into a locked-out install, end to end.

Recovery mode signs an operator back in using a one-time code. Two properties matter on the
desktop builds specifically: the code has to reach somewhere the operator can actually read
it, not only a console those builds do not have, and the session it opens must be able to
set a new password without first proving the current one, since that is the one thing an
operator using recovery does not know.

These pin both halves at the route level. The unit-level half of the file channel lives in
``tests/test_auth_lockout.py``. What is here is what an operator actually does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from reaper.auth.recovery import mint_recovery_token, recovery_file_path
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.session import create_engine, create_session_factory
from reaper.main import create_app

from ._auth import TEST_ADMIN, TEST_PASSWORD, login

NEW_PASSWORD = "a-brand-new-admin-password"


def _make(tmp_path: Path, *, recovery: bool = False) -> Settings:
    """Settings over ``tmp_path``. Call it again on the same path to boot the same install
    a second time. ``create_all`` is idempotent, so only the flags change."""
    settings = Settings(data_dir=tmp_path, secret_key="k", recovery=recovery)
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return settings


@contextmanager
def _booted(settings: Settings) -> Iterator[TestClient]:
    """One boot of the app, with the CSRF header the frontend always sends."""
    with TestClient(create_app(settings)) as client:
        client.headers["X-Reaper-CSRF"] = "1"
        yield client


def _sign_in(client: TestClient) -> None:
    """Sign in as the admin a previous boot already seeded, instead of using ``_auth.login``.

    ``_auth.login`` seeds a new admin first. Because the username is unique, a second boot
    over the same database would fail on that insert rather than on anything this test
    means to check.
    """
    response = client.post(
        "/api/auth/local", json={"username": TEST_ADMIN, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    # Startup seeding and the catch-up network fetch are stubbed by the autouse ``_hermetic``
    # fixture in conftest.py, so booting the app here is safe.
    settings = _make(tmp_path)
    with TestClient(create_app(settings)) as c:
        login(c, settings)  # seeds a local admin whose password is TEST_PASSWORD
        yield c


def _settings_of(client: TestClient) -> Settings:
    settings: Settings = client.app.state.settings  # type: ignore[attr-defined]
    return settings


def arm_recovery(client: TestClient) -> str:
    """Mint a live code the way boot does, and return it.

    This calls the production ``mint_recovery_token`` function instead of writing the row by
    hand. A field it starts setting, or a change to how the token is hashed, then breaks this
    helper directly instead of leaving it quietly minting something the route will not
    accept. It also means the file channel is exercised by every test below, not only the
    two that test it directly.
    """
    settings = _settings_of(client)

    async def _mint() -> str:
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        try:
            async with factory() as session:
                token = await mint_recovery_token(
                    session, base_url="http://localhost:8420", data_dir=settings.data_dir
                )
                await session.commit()
                return token
        finally:
            await engine.dispose()

    return asyncio.run(_mint())


def sign_in_with_recovery(client: TestClient) -> None:
    """Redeem a fresh code on ``client``, leaving it holding a recovery session."""
    response = client.post("/api/auth/recover", json={"token": arm_recovery(client)})
    assert response.status_code == 200, response.text


class TestTheCodeReachesTheOperator:
    def test_redeeming_removes_the_written_copy(self, client: TestClient) -> None:
        """A spent code is deleted from the data folder afterward.

        It authorizes nothing once used, but it is still a credential-shaped string sitting
        next to the database.
        """
        path = recovery_file_path(_settings_of(client).data_dir)
        arm_recovery(client)
        assert path.exists()

        sign_in_with_recovery(client)
        assert not path.exists()

    def test_a_rejected_code_leaves_the_file_where_it_was(self, client: TestClient) -> None:
        """A wrong paste must not cost the operator the copy they were about to read.

        The real code still works afterward.
        """
        path = recovery_file_path(_settings_of(client).data_dir)
        token = arm_recovery(client)

        refused = client.post("/api/auth/recover", json={"token": "not-the-code"})
        assert refused.status_code == 401
        assert path.exists()

        assert client.post("/api/auth/recover", json={"token": token}).status_code == 200

    def test_booting_without_recovery_sweeps_a_stale_file(self, tmp_path: Path) -> None:
        """The banner tells the operator to set REAPER_RECOVERY=false and restart, so the
        restart itself has to clean up the leftover file. The file is written at boot, and
        a code that expired unredeemed leaves no other trace to clean it up.
        """
        settings = _make(tmp_path)
        stale = recovery_file_path(settings.data_dir)
        stale.write_text("an expired code", encoding="utf-8")

        with TestClient(create_app(settings)):
            pass

        assert not stale.exists()

    def test_booting_with_recovery_writes_one(self, tmp_path: Path) -> None:
        """The other half of the same branch, so the sweep above cannot pass by never
        writing a file at all."""
        settings = _make(tmp_path, recovery=True)

        with _booted(settings):
            pass

        assert "Reaper recovery code" in recovery_file_path(settings.data_dir).read_text(
            encoding="utf-8"
        )


class TestRecoveryModeHoldsDeletionOff:
    """Anyone who can reach the host can use recovery mode to sign in. While it is on,
    Reaper must not also be able to delete media, so ambiguity resolves toward keeping the
    file, and a lockout is exactly when nobody is watching to catch a mistake.

    The hold lives in ``RuntimeSafety.destructive_allowed``, so it reaches the transport
    guard, the executor, and the planner without any of them being asked directly.
    """

    def test_arming_is_refused_while_recovery_is_on(self, tmp_path: Path) -> None:
        settings = _make(tmp_path, recovery=True)
        with _booted(settings) as client:
            login(client, settings)
            refused = client.put(
                "/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD}
            )
            # 409, because the password was actually right. Returning 403, as for a wrong
            # password, would send the operator to guess at a password that is not the problem.
            assert refused.status_code == 409, refused.text
            refused_body = refused.json()
            assert refused_body["code"] == "error.settings.recovery_mode_blocks_arming"
            assert "Recovery mode is on" in refused_body["detail"]
            assert client.get("/api/settings/safety").json()["destructive_enabled"] is False

    def test_the_banner_is_told_which_state_it_is_in(self, tmp_path: Path) -> None:
        """Read-only alone would send the operator to Policy, Deletion, where the switch
        refuses them. The flag is what lets the banner say the thing they can act on."""
        settings = _make(tmp_path, recovery=True)
        with _booted(settings) as client:
            login(client, settings)
            safety = client.get("/api/settings/safety").json()
            assert safety["recovery_mode"] is True
            assert safety["destructive_enabled"] is False

    def test_turning_deletion_off_still_needs_nothing(self, tmp_path: Path) -> None:
        """Making Reaper safer is never gated, and recovery mode does not change that."""
        settings = _make(tmp_path, recovery=True)
        with _booted(settings) as client:
            login(client, settings)
            off = client.put("/api/settings/safety", json={"enabled": False})
            assert off.status_code == 200, off.text

    def test_an_install_armed_before_the_lockout_comes_back_read_only(self, tmp_path: Path) -> None:
        """A recovery boot writes the stored switch off permanently, in the database, not
        only for the life of that process.

        Otherwise turning recovery back off would silently re-arm deletion on an install
        whose operator had just spent an afternoon locked out of it, with nothing on screen
        naming a change they never made.

        Three boots, because the last one is the one the operator ends on. It is the only
        one where the difference between "held off" and "turned off" is visible at all.
        """
        settings = _make(tmp_path)
        with _booted(settings) as client:
            login(client, settings)
            armed = client.put(
                "/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD}
            )
            assert armed.json()["destructive_enabled"] is True

        with _booted(_make(tmp_path, recovery=True)):
            pass  # the recovery boot mints a code and writes the switch off

        after = _make(tmp_path)
        with _booted(after) as client:
            _sign_in(client)
            safety = client.get("/api/settings/safety").json()
            assert safety["recovery_mode"] is False  # the hold is gone...
            assert safety["destructive_enabled"] is False  # ...and it is still off


class TestARecoverySessionCanSetANewPassword:
    def test_me_reports_the_mark(self, client: TestClient) -> None:
        """The Security panel reads this to decide whether to hide its current-password
        box. An ordinary session reports false, which is what keeps that box visible
        everywhere else."""
        assert client.get("/api/auth/me").json()["via_recovery"] is False

        sign_in_with_recovery(client)
        assert client.get("/api/auth/me").json()["via_recovery"] is True

    def test_the_new_password_is_accepted_without_the_old_one(self, client: TestClient) -> None:
        """A recovery session can set a new password without proving the old one.

        Without this, the operator would be signed in on the Security page, unable to do
        the one thing recovery brought them there for.
        """
        sign_in_with_recovery(client)

        saved = client.post("/api/settings/admin-password", json={"password": NEW_PASSWORD})
        assert saved.status_code == 200, saved.text
        assert saved.json() == {"ok": True}

        # The new password now works for arming deletion, and the old one no longer does.
        assert (
            client.put(
                "/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD}
            ).status_code
            == 403
        )
        armed = client.put("/api/settings/safety", json={"enabled": True, "password": NEW_PASSWORD})
        assert armed.status_code == 200, armed.text

    def test_an_ordinary_session_still_has_to_prove_the_old_one(self, client: TestClient) -> None:
        """The excusal belongs to the session that redeemed the recovery code, not to the
        whole install. Signing in normally, with a password already set, is refused exactly
        as before. That is what stops a borrowed tab from swapping the credential that arms
        deletion.
        """
        refused = client.post("/api/settings/admin-password", json={"password": NEW_PASSWORD})
        assert refused.status_code == 403
        refused_body = refused.json()
        assert refused_body["code"] == "error.auth.change_password_mismatch"
        assert refused_body["detail"] == ("The current password didn't match. Nothing was changed.")

    def test_the_mark_is_spent_on_the_first_change(self, client: TestClient) -> None:
        """Single-use, like the code that granted it. Left standing, one recovery boot
        would give thirty days during which the arming credential could be changed without
        anyone knowing.
        """
        sign_in_with_recovery(client)
        assert (
            client.post("/api/settings/admin-password", json={"password": NEW_PASSWORD}).status_code
            == 200
        )

        assert client.get("/api/auth/me").json()["via_recovery"] is False
        again = client.post("/api/settings/admin-password", json={"password": "another-password-1"})
        assert again.status_code == 403

    def test_a_restart_ends_the_elevated_permission(self, tmp_path: Path) -> None:
        """An operator who signs in with a code and changes nothing must not keep the right
        to rewrite the arming credential. ``spend_recovery_mark`` only fires on a successful
        reset, so without a boot-time sweep the mark would stand for the session's full 30
        days, past the restart the manual gives as the last step, with nothing on screen
        saying so. A stolen cookie would then do what an ordinary one cannot.
        """
        settings = _make(tmp_path)
        with _booted(settings) as client:
            login(client, settings)
            sign_in_with_recovery(client)
            assert client.get("/api/auth/me").json()["via_recovery"] is True
            # The SAME cookie carries across the restart. Signing in again on the second
            # boot would mint a fresh, unmarked session regardless of the sweep, so the test
            # would pass even with the sweep deleted.
            cookies = dict(client.cookies)

        after = _make(tmp_path)
        with _booted(after) as client:
            client.cookies.update(cookies)
            me = client.get("/api/auth/me")
            assert me.status_code == 200, "demoted, not revoked: still signed in"
            assert me.json()["via_recovery"] is False
            # The permission is actually revoked, not just left off the response.
            refused = client.post("/api/settings/admin-password", json={"password": NEW_PASSWORD})
            assert refused.status_code == 403

    def test_a_refused_password_keeps_the_mark(self, client: TestClient) -> None:
        """A password rejected for being too short is a recoverable error, so it must not
        spend the one thing that lets the operator retry."""
        sign_in_with_recovery(client)

        assert (
            client.post("/api/settings/admin-password", json={"password": "short"}).status_code
            == 422
        )
        assert client.get("/api/auth/me").json()["via_recovery"] is True
        assert (
            client.post("/api/settings/admin-password", json={"password": NEW_PASSWORD}).status_code
            == 200
        )

    def test_every_other_session_is_revoked(self, client: TestClient, tmp_path: Path) -> None:
        """The excusal does not relax the rule that a credential change revokes every other
        session. A recovery reset is still a credential change, so every session the old
        password could have authorized stops working. That matters most when the lockout
        happened because of a stolen cookie in the first place.
        """
        other = TestClient(client.app)
        other.headers["X-Reaper-CSRF"] = "1"
        signed_in = other.post(
            "/api/auth/local", json={"username": "tester", "password": TEST_PASSWORD}
        )
        assert signed_in.status_code == 200, signed_in.text

        sign_in_with_recovery(client)
        assert (
            client.post("/api/settings/admin-password", json={"password": NEW_PASSWORD}).status_code
            == 200
        )

        assert other.get("/api/auth/me").status_code == 401
        # The session that made the change stays signed in, so the operator is not sent to
        # a login screen holding a password they set one second ago.
        assert client.get("/api/auth/me").status_code == 200

    def test_a_first_password_is_unaffected(self, client: TestClient) -> None:
        """A Plex-only install has no current password to excuse, so the recovery mark
        changes nothing there. Pinned because the route now has two reasons to skip the
        verify and only one of them is new."""
        settings = _settings_of(client)
        engine = sa_create_engine(settings.sync_database_url)
        with engine.begin() as conn:
            conn.execute(text("UPDATE app_user SET password_hash = NULL"))
        engine.dispose()

        saved = client.post("/api/settings/admin-password", json={"password": NEW_PASSWORD})
        assert saved.status_code == 200, saved.text
