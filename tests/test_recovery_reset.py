# SPDX-License-Identifier: AGPL-3.0-or-later
"""The way back into a locked-out install, end to end.

Recovery mode existed and signed an operator in, but it dead-ended twice on the desktop
builds (#433):

* the code was printed to a console those builds do not have, so it reached nobody; and
* the session it opened still had to prove the current password before it could change
  it, which is the one thing an operator using recovery does not know.

These pin both halves at the route level. The unit-level half of the file channel lives in
``tests/test_auth_lockout.py``; what is here is what an operator actually does.
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
    """Settings over ``tmp_path``. Call it again on the same path for a second BOOT of the
    same install: ``create_all`` is idempotent, so only the flags change."""
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
    """Sign in as the admin a previous boot seeded.

    Not ``_auth.login``: that seeds one first, and the username is unique, so a second boot
    over the same database would fail on the insert rather than on anything under test.
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

    Calls the production function rather than writing the row by hand (rule 119): a field
    ``mint_recovery_token`` starts setting, or a change to how the token is hashed, breaks this
    helper instead of leaving it quietly minting something the route will not accept. It also
    means the file channel is exercised by every test below rather than only the two about it.
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
        """A spent code must not sit in the data folder afterwards. It authorizes nothing
        once used, but it is still a credential-shaped string beside the database."""
        path = recovery_file_path(_settings_of(client).data_dir)
        arm_recovery(client)
        assert path.exists()

        sign_in_with_recovery(client)
        assert not path.exists()

    def test_a_rejected_code_leaves_the_file_where_it_was(self, client: TestClient) -> None:
        """Rule 125's shape for the file rather than the row: a wrong paste must not cost the
        operator the copy they were about to read. The real code still works afterwards."""
        path = recovery_file_path(_settings_of(client).data_dir)
        token = arm_recovery(client)

        refused = client.post("/api/auth/recover", json={"token": "not-the-code"})
        assert refused.status_code == 401
        assert path.exists()

        assert client.post("/api/auth/recover", json={"token": token}).status_code == 200

    def test_booting_without_recovery_sweeps_a_stale_file(self, tmp_path: Path) -> None:
        """ "Set REAPER_RECOVERY=false and restart" is what the banner tells the operator to do,
        so the restart has to be what actually tidies up. Nothing else ever would: the file is
        written at boot, and a code that expired unredeemed leaves no other trace."""
        settings = _make(tmp_path)
        stale = recovery_file_path(settings.data_dir)
        stale.write_text("an expired code", encoding="utf-8")

        with TestClient(create_app(settings)):
            pass

        assert not stale.exists()

    def test_booting_with_recovery_writes_one(self, tmp_path: Path) -> None:
        """The other half of the same branch, so the sweep above cannot pass by never
        writing at all (rule 145: drive both sides, not only the one you found)."""
        settings = _make(tmp_path, recovery=True)

        with _booted(settings):
            pass

        assert "Reaper recovery code" in recovery_file_path(settings.data_dir).read_text(
            encoding="utf-8"
        )


class TestRecoveryModeHoldsDeletionOff:
    """Recovery mode is a door that opens for anyone who can reach the host. While it is
    open, Reaper must not also be able to delete media: the prime directive resolves the
    overlap toward keeping the file, and a lockout is exactly when nobody is watching.

    The hold lives in ``RuntimeSafety.destructive_allowed``, so it reaches the transport
    guard, the executor and the planner without any of them being asked (rule 3/22).
    """

    def test_arming_is_refused_while_recovery_is_on(self, tmp_path: Path) -> None:
        settings = _make(tmp_path, recovery=True)
        with _booted(settings) as client:
            login(client, settings)
            refused = client.put(
                "/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD}
            )
            # Not 403: the password was right. Saying "that didn't match" would send the
            # operator to guess at a password that is not the problem.
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
            assert safety["note_reason"]["k"] == "error.safety.recovery_mode_active"

    def test_turning_deletion_off_still_needs_nothing(self, tmp_path: Path) -> None:
        """Making Reaper safer is never gated, and recovery mode does not change that."""
        settings = _make(tmp_path, recovery=True)
        with _booted(settings) as client:
            login(client, settings)
            off = client.put("/api/settings/safety", json={"enabled": False})
            assert off.status_code == 200, off.text

    def test_an_install_armed_before_the_lockout_comes_back_read_only(self, tmp_path: Path) -> None:
        """The stored switch is written off at a recovery boot, not merely overridden for
        the life of that process. Otherwise turning recovery back off would silently re-arm
        deletion on an install whose operator had just spent an afternoon locked out of it,
        and nothing on screen would name the change they never made.

        Three boots, because the last one is the one the operator ends on and the only one
        where the difference between "held off" and "turned off" is visible at all.
        """
        settings = _make(tmp_path)
        with _booted(settings) as client:
            login(client, settings)
            armed = client.put(
                "/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD}
            )
            assert armed.json()["destructive_enabled"] is True

        with _booted(_make(tmp_path, recovery=True)):
            pass  # the recovery boot: mints a code, and writes the switch off

        after = _make(tmp_path)
        with _booted(after) as client:
            _sign_in(client)
            safety = client.get("/api/settings/safety").json()
            assert safety["recovery_mode"] is False  # the hold is gone...
            assert safety["destructive_enabled"] is False  # ...and it is still off


class TestARecoverySessionCanSetANewPassword:
    def test_me_reports_the_mark(self, client: TestClient) -> None:
        """The Security panel reads this to park its current-password box. An ordinary
        session reports false, which is what keeps that box live everywhere else."""
        assert client.get("/api/auth/me").json()["via_recovery"] is False

        sign_in_with_recovery(client)
        assert client.get("/api/auth/me").json()["via_recovery"] is True

    def test_the_new_password_is_accepted_without_the_old_one(self, client: TestClient) -> None:
        """The fix. Before it, this returned 403 and the operator was signed in, on the
        Security page, unable to do the one thing recovery had brought them there for."""
        sign_in_with_recovery(client)

        saved = client.post("/api/settings/admin-password", json={"password": NEW_PASSWORD})
        assert saved.status_code == 200, saved.text
        assert saved.json() == {"ok": True}

        # It really is the password now: it arms deletion, and the old one does not.
        assert (
            client.put(
                "/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD}
            ).status_code
            == 403
        )
        armed = client.put("/api/settings/safety", json={"enabled": True, "password": NEW_PASSWORD})
        assert armed.status_code == 200, armed.text

    def test_an_ordinary_session_still_has_to_prove_the_old_one(self, client: TestClient) -> None:
        """The excusal is a property of the SESSION, not of the install. Signing in normally
        with a password set is refused exactly as before, which is what stops a borrowed tab
        from swapping the credential that arms deletion."""
        refused = client.post("/api/settings/admin-password", json={"password": NEW_PASSWORD})
        assert refused.status_code == 403
        refused_body = refused.json()
        assert refused_body["code"] == "error.auth.change_password_mismatch"
        assert refused_body["detail"] == ("The current password didn't match. Nothing was changed.")

    def test_the_mark_is_spent_on_the_first_change(self, client: TestClient) -> None:
        """Single-use, like the code that granted it. Left standing, one recovery boot would
        buy thirty days of changing the arming credential without knowing it."""
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
        to rewrite the arming credential. `spend_recovery_mark` only fires on a successful
        reset, so without a boot-time sweep the mark stood for the session's full 30 days --
        past the restart the manual gives as the last step, with nothing on screen saying so.
        A lifted cookie would then do what an ordinary one cannot.
        """
        settings = _make(tmp_path)
        with _booted(settings) as client:
            login(client, settings)
            sign_in_with_recovery(client)
            assert client.get("/api/auth/me").json()["via_recovery"] is True
            # The SAME cookie across the restart. Signing in again on the second boot would
            # mint a fresh session, which is unmarked whatever the sweep does, so the test
            # would pass with the sweep deleted (rule 118).
            cookies = dict(client.cookies)

        after = _make(tmp_path)
        with _booted(after) as client:
            client.cookies.update(cookies)
            me = client.get("/api/auth/me")
            assert me.status_code == 200, "demoted, not revoked: still signed in"
            assert me.json()["via_recovery"] is False
            # ...and the permission really is gone, not just unreported.
            refused = client.post("/api/settings/admin-password", json={"password": NEW_PASSWORD})
            assert refused.status_code == 403

    def test_a_refused_password_keeps_the_mark(self, client: TestClient) -> None:
        """Rule 125's shape for the permission: a password rejected for being too short is a
        recoverable error, so it must not spend the one thing letting the operator retry."""
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
        """Rule 12/75 is not relaxed by the excusal. A recovery reset is still a credential
        change, so the sessions the OLD password could have authorized all stop working --
        which is the point when the reason for the lockout was a stolen cookie."""
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
        # ...and the session that made the change is still standing, so the operator is not
        # bounced to a login screen holding a password they set one second ago.
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
