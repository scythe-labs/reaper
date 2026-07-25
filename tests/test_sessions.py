# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sessions and the local-login path.

The invariants that matter for a tool that can delete a library: a revoked or
expired session stops working *immediately* (we store server-side state precisely
so it can), a deactivated admin's live sessions die with the account, and a failed
local login says nothing about which usernames exist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx
import httpx2
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.auth.admins import create_local_admin
from reaper.auth.sessions import (
    close_all_for_user,
    close_session,
    open_session,
    resolve_session,
)
from reaper.auth.tokens import hash_token
from reaper.clock import expiry, utcnow
from reaper.config import RuntimeSafety, Settings, generate_secret_key
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.models import AppUser, AuthProvider, AuthSession, PendingPlexLogin, PlexServer
from reaper.db.session import create_engine, create_session_factory
from reaper.services.login import PLEX_LOGIN_TTL, LoginError, login_local, poll_plex_login
from reaper.services.plex_link import (
    PlexLinkRetryableError,
    PlexServerChoiceNeededError,
)

pytestmark = pytest.mark.httpx2(assert_all_called=False)

SAFETY = RuntimeSafety(destructive_enabled=False)


def _box() -> SecretBox:
    return SecretBox(generate_secret_key())


def _resource(machine_id: str, name: str = "Home") -> dict[str, object]:
    return {
        "name": name,
        "clientIdentifier": machine_id,
        "owned": True,
        "provides": "server",
        "accessToken": "server-token",
        "connections": [
            {
                "uri": "https://x.plex.direct:32400",
                "address": "10.0.0.2",
                "port": 32400,
                "local": True,
                "relay": False,
                "protocol": "https",
            }
        ],
    }


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


async def _admin(session: AsyncSession, username: str = "owner", password: str = "pw") -> AppUser:
    user, _ = await create_local_admin(session, username, password)
    return user


class TestSessionLifecycle:
    async def test_open_then_resolve_round_trips(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            user = await _admin(session)
            token = await open_session(session, user)
            await session.commit()

        async with factory() as session:
            resolved = await resolve_session(session, token)
            assert resolved is not None
            assert resolved.username == "owner"

    async def test_only_the_hash_is_stored(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """A database leak must not hand out live sessions."""
        async with factory() as session:
            user = await _admin(session)
            token = await open_session(session, user)
            await session.commit()

        async with factory() as session:
            rows = (await session.execute(select(AuthSession))).scalars().all()
            assert len(rows) == 1
            assert token not in rows[0].token_hash
            assert rows[0].token_hash == hash_token(token)

    @pytest.mark.parametrize("token", [None, "", "not-a-real-token"])
    async def test_absent_or_unknown_token_resolves_to_nobody(
        self, factory: async_sessionmaker[AsyncSession], token: str | None
    ) -> None:
        async with factory() as session:
            assert await resolve_session(session, token) is None

    async def test_an_expired_session_is_refused_and_pruned(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            user = await _admin(session)
            token = await open_session(session, user)
            # Backdate it past its expiry.
            row = (await session.execute(select(AuthSession))).scalar_one()
            row.expires_at = utcnow() - timedelta(minutes=1)
            await session.commit()

        async with factory() as session:
            assert await resolve_session(session, token) is None
            await session.commit()

        async with factory() as session:
            # And the dead row is gone, so the table self-prunes.
            assert (await session.execute(select(AuthSession))).first() is None

    async def test_a_deactivated_admin_loses_live_sessions(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            user = await _admin(session)
            token = await open_session(session, user)
            user.is_active = False
            await session.commit()

        async with factory() as session:
            assert await resolve_session(session, token) is None

    async def test_logout_revokes_immediately(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            user = await _admin(session)
            token = await open_session(session, user)
            await session.commit()

        async with factory() as session:
            await close_session(session, token)
            await session.commit()

        async with factory() as session:
            assert await resolve_session(session, token) is None

    async def test_sign_out_everywhere_revokes_all(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            user = await _admin(session)
            t1 = await open_session(session, user)
            t2 = await open_session(session, user)
            await session.commit()
            uid = user.id

        async with factory() as session:
            await close_all_for_user(session, uid)
            await session.commit()

        async with factory() as session:
            assert await resolve_session(session, t1) is None
            assert await resolve_session(session, t2) is None


class TestLocalLogin:
    async def test_correct_password_signs_in(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            await create_local_admin(session, "owner", "correct horse")
            await session.commit()

        result = await login_local(factory, username="owner", password="correct horse")
        assert result.user.username == "owner"
        assert result.session_token

        async with factory() as session:
            assert await resolve_session(session, result.session_token) is not None

    async def test_wrong_password_is_refused(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            await create_local_admin(session, "owner", "correct horse")
            await session.commit()

        with pytest.raises(LoginError):
            await login_local(factory, username="owner", password="wrong")

    async def test_an_unknown_user_is_refused_the_same_way(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """No account exists, and the error must not reveal that."""
        with pytest.raises(LoginError, match="Wrong username or password"):
            await login_local(factory, username="ghost", password="anything")

    async def test_a_plex_only_account_cannot_be_password_guessed(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A Plex-provisioned admin has no password hash. It must not be loggable-in
        with an empty or any password."""
        async with factory() as session:
            session.add(
                AppUser(
                    provider=AuthProvider.PLEX,
                    plex_account_id=999,
                    username="plexonly",
                    is_active=True,
                    created_at=utcnow(),
                )
            )
            await session.commit()

        with pytest.raises(LoginError):
            await login_local(factory, username="plexonly", password="")

    async def test_a_deactivated_account_is_refused(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            user, _ = await create_local_admin(session, "owner", "pw")
            user.is_active = False
            await session.commit()

        with pytest.raises(LoginError):
            await login_local(factory, username="owner", password="pw")


async def _link_server(factory: async_sessionmaker[AsyncSession], machine_id: str) -> None:
    async with factory() as session:
        session.add(
            PlexServer(
                machine_identifier=machine_id,
                name="Home",
                connection_uri="https://x.plex.direct:32400",
                token_enc="enc",
                owner_plex_account_id=1,
                created_at=utcnow(),
            )
        )
        await session.commit()


async def _pending(factory: async_sessionmaker[AsyncSession], pin_id: int) -> None:
    async with factory() as session:
        session.add(
            PendingPlexLogin(
                pin_id=pin_id,
                pin_code="ABCD",
                purpose="login",
                created_at=utcnow(),
                expires_at=expiry(PLEX_LOGIN_TTL),
            )
        )
        await session.commit()


class TestPlexLoginPoll:
    """The authorization boundary, exercised through the login poller. The server
    is already linked, so the check is 'does this token own *this* machine?'."""

    async def test_not_yet_approved_is_pending(
        self, factory: async_sessionmaker[AsyncSession], httpx2_mock: respx.Router
    ) -> None:
        await _link_server(factory, "ours")
        await _pending(factory, 42)
        httpx2_mock.get("https://plex.tv/api/v2/pins/42").mock(
            return_value=httpx.Response(200, json={"id": 42, "authToken": None})
        )
        result = await poll_plex_login(factory, _box(), pin_id=42, safety=SAFETY)
        assert result is None

    async def test_the_owner_is_admitted(
        self, factory: async_sessionmaker[AsyncSession], httpx2_mock: respx.Router
    ) -> None:
        await _link_server(factory, "ours")
        await _pending(factory, 42)
        httpx2_mock.get("https://plex.tv/api/v2/pins/42").mock(
            return_value=httpx.Response(200, json={"id": 42, "authToken": "tok"})
        )
        httpx2_mock.get("https://plex.tv/api/v2/user").mock(
            return_value=httpx.Response(200, json={"id": 7, "username": "owner"})
        )
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(200, json=[_resource("ours")])
        )
        result = await poll_plex_login(factory, _box(), pin_id=42, safety=SAFETY)

        assert result is not None
        assert result.user.username == "owner"
        async with factory() as session:
            assert await resolve_session(session, result.session_token) is not None

    async def test_a_non_owner_is_refused(
        self, factory: async_sessionmaker[AsyncSession], httpx2_mock: respx.Router
    ) -> None:
        """Authenticated to Plex, owns a *different* server -> not our admin."""
        await _link_server(factory, "ours")
        await _pending(factory, 42)
        httpx2_mock.get("https://plex.tv/api/v2/pins/42").mock(
            return_value=httpx.Response(200, json={"id": 42, "authToken": "tok"})
        )
        httpx2_mock.get("https://plex.tv/api/v2/user").mock(
            return_value=httpx.Response(200, json={"id": 8, "username": "stranger"})
        )
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(200, json=[_resource("someone-elses-server")])
        )
        with pytest.raises(LoginError, match="does not own this server"):
            await poll_plex_login(factory, _box(), pin_id=42, safety=SAFETY)

        # No account was created, and the pending was consumed so it can't be retried.
        async with factory() as session:
            assert (await session.execute(select(AppUser))).first() is None
            assert (await session.execute(select(PendingPlexLogin))).first() is None


class TestFirstRunServerChoice:
    """First-run setup for an account owning SEVERAL servers.

    The properties that matter for a deletion tool: Reaper never guesses which
    library to manage, the sign-in survives while the owner picks (no second
    OAuth round-trip), and a pick can only ever land on a server the account
    owns -- whatever string the browser sends back.
    """

    def _mock_signin(self, httpx2_mock: respx.Router, resources: list[dict[str, object]]) -> None:
        httpx2_mock.get("https://plex.tv/api/v2/pins/42").mock(
            return_value=httpx.Response(200, json={"id": 42, "authToken": "tok"})
        )
        httpx2_mock.get("https://plex.tv/api/v2/user").mock(
            return_value=httpx.Response(200, json={"id": 7, "username": "owner"})
        )
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(200, json=resources)
        )
        # The reachability probe for whichever server gets picked.
        httpx2_mock.get("https://x.plex.direct:32400/identity").mock(
            return_value=httpx.Response(200, json={})
        )

    async def test_two_owned_servers_ask_for_a_choice_and_keep_the_pin(
        self, factory: async_sessionmaker[AsyncSession], httpx2_mock: respx.Router
    ) -> None:
        await _pending(factory, 42)
        self._mock_signin(
            httpx2_mock, [_resource("machine-a", "Den"), _resource("machine-b", "Attic")]
        )
        with pytest.raises(PlexServerChoiceNeededError) as exc_info:
            await poll_plex_login(factory, _box(), pin_id=42, safety=SAFETY)

        # Both owned servers are offered, by name and identifier.
        offered = {(c.name, c.machine_identifier) for c in exc_info.value.candidates}
        assert offered == {("Den", "machine-a"), ("Attic", "machine-b")}

        async with factory() as session:
            # Nothing was linked and no admin was created: the choice is still open.
            assert (await session.execute(select(PlexServer))).first() is None
            assert (await session.execute(select(AppUser))).first() is None
            # The pending PIN survives, so the same sign-in can finish the job.
            assert (await session.execute(select(PendingPlexLogin))).first() is not None

    async def test_the_choice_links_exactly_the_picked_server(
        self, factory: async_sessionmaker[AsyncSession], httpx2_mock: respx.Router
    ) -> None:
        await _pending(factory, 42)
        resources = [_resource("machine-a", "Den"), _resource("machine-b", "Attic")]
        self._mock_signin(httpx2_mock, resources)
        with pytest.raises(PlexServerChoiceNeededError):
            await poll_plex_login(factory, _box(), pin_id=42, safety=SAFETY)

        # The re-poll carries the pick; the still-valid PIN completes setup.
        self._mock_signin(httpx2_mock, resources)
        result = await poll_plex_login(
            factory, _box(), pin_id=42, safety=SAFETY, choice="machine-b"
        )

        assert result is not None and result.setup is True
        async with factory() as session:
            server = (await session.execute(select(PlexServer))).scalar_one()
            assert server.machine_identifier == "machine-b"
            assert server.name == "Attic"
            # The pick consumed the pending row: the token cannot be replayed.
            assert (await session.execute(select(PendingPlexLogin))).first() is None
            assert await resolve_session(session, result.session_token) is not None

    async def test_a_briefly_unreachable_server_keeps_the_pin(
        self, factory: async_sessionmaker[AsyncSession], httpx2_mock: respx.Router
    ) -> None:
        """The twin of the in-app link flow's transient blip, on the setup path.

        ``complete_link`` raises the retryable error when no advertised address answers
        right now. It is NOT a refusal, so it must not consume the sign-in -- but it is a
        subclass of the permanent link error, and the arm that consumes the PIN used to
        catch it first. Getting this wrong forces the owner through the whole OAuth round
        trip again over a server that was merely restarting.
        """
        await _pending(factory, 42)
        resources = [_resource("machine-a", "Den"), _resource("machine-b", "Attic")]
        self._mock_signin(httpx2_mock, resources)
        httpx2_mock.get("https://x.plex.direct:32400/identity").mock(
            side_effect=httpx2.ConnectError("connection refused")
        )

        with pytest.raises(PlexLinkRetryableError):
            await poll_plex_login(factory, _box(), pin_id=42, safety=SAFETY, choice="machine-b")

        async with factory() as session:
            assert (await session.execute(select(PlexServer))).first() is None
            # The sign-in survives, so the same PIN can finish once the server is back.
            assert (await session.execute(select(PendingPlexLogin))).first() is not None

        # And it does.
        self._mock_signin(httpx2_mock, resources)
        result = await poll_plex_login(
            factory, _box(), pin_id=42, safety=SAFETY, choice="machine-b"
        )
        assert result is not None and result.setup is True

    async def test_a_choice_matching_nothing_owned_is_refused(
        self, factory: async_sessionmaker[AsyncSession], httpx2_mock: respx.Router
    ) -> None:
        """Fail closed: a stale or hostile identifier can never link a server."""
        await _pending(factory, 42)
        self._mock_signin(
            httpx2_mock, [_resource("machine-a", "Den"), _resource("machine-b", "Attic")]
        )
        with pytest.raises(LoginError, match="No server this account owns"):
            await poll_plex_login(
                factory, _box(), pin_id=42, safety=SAFETY, choice="somebody-elses-machine"
            )

        async with factory() as session:
            assert (await session.execute(select(PlexServer))).first() is None
            # A permanent refusal consumes the PIN, exactly like the other refusals.
            assert (await session.execute(select(PendingPlexLogin))).first() is None

    async def test_a_choice_by_exact_name_works_and_a_duplicate_name_is_refused(
        self, factory: async_sessionmaker[AsyncSession], httpx2_mock: respx.Router
    ) -> None:
        """The CLI passes what a human types: an exact name resolves, an ambiguous
        one is refused rather than guessed."""
        await _pending(factory, 42)
        self._mock_signin(
            httpx2_mock, [_resource("machine-a", "Den"), _resource("machine-b", "Attic")]
        )
        result = await poll_plex_login(factory, _box(), pin_id=42, safety=SAFETY, choice="Attic")
        assert result is not None
        async with factory() as session:
            server = (await session.execute(select(PlexServer))).scalar_one()
            assert server.machine_identifier == "machine-b"

    async def test_two_servers_sharing_the_chosen_name_are_refused(
        self, factory: async_sessionmaker[AsyncSession], httpx2_mock: respx.Router
    ) -> None:
        await _pending(factory, 42)
        self._mock_signin(
            httpx2_mock, [_resource("machine-a", "Home"), _resource("machine-b", "Home")]
        )
        with pytest.raises(LoginError, match="more than one server named"):
            await poll_plex_login(factory, _box(), pin_id=42, safety=SAFETY, choice="Home")

        async with factory() as session:
            assert (await session.execute(select(PlexServer))).first() is None
