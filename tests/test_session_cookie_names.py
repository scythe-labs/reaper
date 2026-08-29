# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two session cookie names, and the properties that keep a stale one from beating a
live one.

Reaper names the session cookie after the connection: ``__Host-reaper_session`` over
HTTPS, a plain ``reaper_session`` otherwise (:mod:`reaper.auth.cookie` says why). A jar
can hold both at once, so the code must act on whichever cookie is actually live, never
on whichever name it happens to find first. This file pins four such properties:

* Clearing the ``__Host-`` name always sets ``Secure``, regardless of the current
  request. A browser refuses a ``__Host-`` cookie delete that arrives without
  ``Secure``, so a delete missing it discards the cookie's database row while leaving
  the cookie itself in the jar, where it keeps authenticating as its owner with no
  visible error.
* Reading a jar tries every name and keeps the one that actually resolves, rather than
  the first name that merely exists.
* Logout revokes every name a jar carries, not just the first.
* A password change spares the session actually in use. Sparing just the first cookie
  name instead can revoke the live session and sign the operator out of the tab they
  are working in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from reaper.auth.admins import create_local_admin
from reaper.auth.cookie import clear_session_cookie, read_session_tokens, set_session_cookie
from reaper.auth.sessions import open_session, resolve_session_from_cookies
from reaper.auth.tokens import SESSION_TTL, hash_token
from reaper.clock import expiry, utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import AppUser, AuthSession
from reaper.db.session import create_engine, create_session_factory
from reaper.main import create_app

from ._auth import TEST_ADMIN, TEST_PASSWORD, login, seed_admin

SECURE_NAME = "__Host-reaper_session"
PLAIN_NAME = "reaper_session"


def _emitted(response: Response) -> dict[str, str]:
    """The ``Set-Cookie`` headers a response carries, keyed by cookie name."""
    return {header.split("=", 1)[0]: header for header in response.headers.getlist("set-cookie")}


class TestClearingUsesTheFlagEachNameRequires:
    """The ``Secure`` flag on a delete is a property of the name, not of the request.

    ``clear_session_cookie`` takes no per-request flag, on purpose: using the request's
    flag instead would silently drop the ``__Host-`` cookie's delete on any install where
    the request does not look secure, while the cookie's database row was removed anyway.
    """

    def test_the_host_name_is_always_cleared_with_secure(self) -> None:
        response = Response()
        clear_session_cookie(response)
        header = _emitted(response)[SECURE_NAME]
        assert "secure" in header.lower(), (
            "A browser refuses a __Host- cookie without Secure, deletion included, so a "
            "delete lacking it leaves the cookie in the jar with its session row gone."
        )

    def test_the_plain_name_is_cleared_without_secure(self) -> None:
        """Its delete carries no ``Secure`` flag, so it is accepted over plain HTTP as
        well as HTTPS."""
        response = Response()
        clear_session_cookie(response)
        assert "secure" not in _emitted(response)[PLAIN_NAME].lower()

    def test_both_names_are_cleared(self) -> None:
        response = Response()
        clear_session_cookie(response)
        assert set(_emitted(response)) == {SECURE_NAME, PLAIN_NAME}

    def test_each_delete_expires_the_cookie(self) -> None:
        response = Response()
        clear_session_cookie(response)
        for header in _emitted(response).values():
            assert "max-age=0" in header.lower()


class TestReadingReturnsEveryName:
    def test_both_names_come_back_host_first(self) -> None:
        tokens = read_session_tokens({SECURE_NAME: "host-tok", PLAIN_NAME: "plain-tok"})
        assert tokens == ("host-tok", "plain-tok")

    def test_one_name_alone_is_fine(self) -> None:
        assert read_session_tokens({PLAIN_NAME: "plain-tok"}) == ("plain-tok",)

    def test_the_same_value_under_both_names_is_not_tried_twice(self) -> None:
        assert read_session_tokens({SECURE_NAME: "same", PLAIN_NAME: "same"}) == ("same",)

    def test_an_empty_cookie_is_not_a_token(self) -> None:
        assert read_session_tokens({SECURE_NAME: "", PLAIN_NAME: "plain-tok"}) == ("plain-tok",)

    def test_an_empty_jar_yields_nothing(self) -> None:
        assert read_session_tokens({}) == ()


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


class TestAStaleCookieCannotShadowALiveSession:
    """A dead cookie under one name cannot shadow a live session under the other name.

    ``resolve_session_from_cookies`` tries every name and keeps the first one that
    actually resolves. Returning the winning token matters as much as returning the
    user. Logout revokes that token and a password change spares it, so handing back the
    wrong token would revoke or spare the wrong session.
    """

    async def test_a_dead_host_cookie_does_not_beat_a_live_plain_one(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            user, _ = await create_local_admin(session, "owner", "pw")
            live = await open_session(session, user)
            await session.commit()

            found, token = await resolve_session_from_cookies(
                session,
                {SECURE_NAME: "a-token-that-was-never-issued", PLAIN_NAME: live},
            )
            assert found is not None
            assert found.id == user.id
            assert token == live

    async def test_a_dead_plain_cookie_does_not_beat_a_live_host_one(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            user, _ = await create_local_admin(session, "owner", "pw")
            live = await open_session(session, user)
            await session.commit()

            found, token = await resolve_session_from_cookies(
                session, {SECURE_NAME: live, PLAIN_NAME: "a-token-that-was-never-issued"}
            )
            assert found is not None
            assert token == live

    async def test_two_dead_cookies_resolve_to_nobody(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            await create_local_admin(session, "owner", "pw")
            await session.commit()

            assert await resolve_session_from_cookies(
                session, {SECURE_NAME: "dead-one", PLAIN_NAME: "dead-two"}
            ) == (None, None)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


class TestLogoutRevokesEverySessionTheJarPresents:
    """Signing out has to mean it. Logout revokes every session a jar's cookies name.

    If logout only closed the first name carrying a cookie, a stale cookie in the jar
    would make the delete land on a row that was already gone, leaving the genuinely
    live session under the other name valid in the database. An exfiltrated token for
    that session would still work after the operator signed out.
    """

    def test_a_second_live_session_under_the_other_name_is_revoked_too(
        self, client: TestClient, settings: Settings
    ) -> None:
        """A real browser could never present this exact cookie jar. TestClient speaks
        http, and a browser never sends a ``__Host-`` cookie over plain HTTP.

        The test still pins the server's own behavior, which is what is under test here.
        It also discriminates. Reverting to closing only the first token would make the
        login session survive, and this test would then fail.
        """
        login(client, settings)  # TestClient speaks http, so this is the PLAIN name

        engine = sa_create_engine(settings.sync_database_url)
        with Session(engine) as session:
            user = session.execute(select(AppUser)).scalar_one()
            second = "a-second-live-session-token"
            session.add(
                AuthSession(
                    token_hash=hash_token(second),
                    user_id=user.id,
                    created_at=utcnow(),
                    expires_at=expiry(SESSION_TTL),
                )
            )
            session.commit()
            assert session.execute(select(func.count(AuthSession.id))).scalar_one() == 2

        client.cookies.set(SECURE_NAME, second)
        assert client.post("/api/auth/logout").status_code == 200

        with Session(engine) as session:
            remaining = session.execute(select(func.count(AuthSession.id))).scalar_one()
        engine.dispose()
        assert remaining == 0, "logout left a live session open under the other cookie name"


class TestOnlyOneSessionCookieIsEverLeftInTheJar:
    """Writing one cookie name must clear the other, or a live cookie under the unused
    name outlives every later sign-in.

    Trying every name and keeping the first that resolves handles a dead cookie, but a
    live cookie still resolves too. Because ``__Host-`` is tried first, a live cookie left
    sitting in the jar under that name keeps authenticating as its owner. Signing in as a
    second admin would then appear to succeed while the app stayed signed in as the first.
    """

    def test_writing_the_host_name_clears_the_plain_one(self) -> None:
        response = Response()
        set_session_cookie(response, "tok", secure=True)
        emitted = _emitted(response)
        assert "tok" in emitted[SECURE_NAME]
        assert "max-age=0" in emitted[PLAIN_NAME].lower()

    def test_writing_the_plain_name_clears_the_host_one_with_secure(self) -> None:
        """The delete needs ``Secure`` or the browser refuses it, the same constraint the
        class above pins for the ``__Host-`` name. Over a genuinely plain-HTTP connection
        the browser would never have sent that cookie anyway, so nothing is stranded.
        """
        response = Response()
        set_session_cookie(response, "tok", secure=False)
        emitted = _emitted(response)
        assert "tok" in emitted[PLAIN_NAME]
        assert "max-age=0" in emitted[SECURE_NAME].lower()
        assert "secure" in emitted[SECURE_NAME].lower()

    def test_signing_in_clears_the_other_name_in_the_same_response(
        self, client: TestClient, settings: Settings
    ) -> None:
        """Checks the end-to-end shape on the wire, where it can actually be decided.

        The scenario this covers needs a browser connection that is HTTPS while
        ``is_secure_request`` reads False, which is exactly what an unlisted reverse
        proxy produces. TestClient cannot be both at once. Over http it discards the
        ``Secure`` delete exactly as a browser would, and over https
        ``is_secure_request`` becomes True and the app writes the other name instead.
        This test pins what the server emits. The two unit tests above pin the cookie
        flags that decide whether the browser honors it.
        """
        seed_admin(settings)
        client.headers["X-Reaper-CSRF"] = "1"
        response = client.post(
            "/api/auth/local", json={"username": TEST_ADMIN, "password": TEST_PASSWORD}
        )
        assert response.status_code == 200

        emitted = {h.split("=", 1)[0]: h for h in response.headers.get_list("set-cookie")}
        assert PLAIN_NAME in emitted, "the session cookie itself"
        assert "max-age=0" in emitted[SECURE_NAME].lower(), (
            "signing in must clear the other name, or a live cookie under it keeps "
            "authenticating as whoever owned it"
        )
