# SPDX-License-Identifier: AGPL-3.0-or-later
"""Raising the KDF cost without stranding anything, and the housekeeping beside it.

Raising ``_SCRYPT_N`` is only safe if every derivation Reaper has ever written under stays
readable, so the compatibility half is what most of this file pins (S-3). The rest covers
the expired-session sweep (PR-13) and the sign-in poll's deadline (S2-2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from reaper import crypto
from reaper.auth import sessions
from reaper.auth.tokens import hash_token
from reaper.clients import plextv
from reaper.clients.base import IntegrationError
from reaper.clock import utcnow
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.models import AppUser, AuthProvider, AuthSession

SALT = b"0123456789abcdef"


def _record(seen: list[int], cost: int, derived: bytes) -> bytes:
    """Note the scrypt cost that was used, and hand the key straight back.

    `(seen.append(n), derived)[1]` said the same thing, but `append` returns None and mypy
    reads the tuple's first element as that -- so the expression only looked like it had a
    value. A named function says which half is the record and which is the answer.
    """
    seen.append(cost)
    return derived


def _legacy_box(n: int, *, salt: bytes | None) -> object:
    """A SecretBox pinned to an older scrypt cost, standing in for an older Reaper."""
    from cryptography.fernet import Fernet, MultiFernet

    write_salt = salt if salt is not None else crypto._DEFAULT_KDF_SALT
    return MultiFernet([Fernet(crypto._derive_fernet_key("the-key", write_salt, n))])


def test_the_cheap_kdf_derives_a_different_key_per_cost() -> None:
    """The class below is vacuous unless two costs derive two keys, and cannot notice.

    ``tests/conftest.py`` replaces ``_derive_fernet_key`` with a cheap one for the session, so
    everything here runs against the wrapper rather than against scrypt at 64 MiB. Every test
    below reads "data written at the old cost still opens under the new one" -- which a wrapper
    mapping every cost to ONE key satisfies by making the two costs the same key, silently.
    Measured on a deliberately collapsing wrapper: 30 tests pass, and four of the six
    compatibility tests prove nothing.

    So the wrapper's injectivity is pinned here, beside the suite that leans on it (rule 145).
    Against the real derivation this holds trivially, which is the point: it can only fail for
    a harness that stopped standing in for it.
    """
    assert crypto._derive_fernet_key("the-key", SALT) != crypto._derive_fernet_key(
        "the-key", SALT, 2**14
    )


class TestTheRaisedCostStillOpensEverything:
    """Every derivation this codebase has shipped, encrypted-then-read across the change."""

    def test_the_current_cost_is_what_new_data_is_written_under(self) -> None:
        assert max(crypto._SUPERSEDED_SCRYPT_N) < crypto._SCRYPT_N

    def test_data_written_at_the_old_cost_with_a_salt_still_opens(self) -> None:
        old = _legacy_box(2**14, salt=SALT)
        token = old.encrypt(b"sonarr-api-key").decode("ascii")  # type: ignore[attr-defined]
        assert SecretBox("the-key", salt=SALT).decrypt(token) == "sonarr-api-key"

    def test_data_written_at_the_old_cost_without_a_salt_still_opens(self) -> None:
        """An install from before the per-install salt: the fixed v1 salt AND the old cost."""
        old = _legacy_box(2**14, salt=None)
        token = old.encrypt(b"plex-token").decode("ascii")  # type: ignore[attr-defined]
        assert SecretBox("the-key", salt=SALT).decrypt(token) == "plex-token"

    def test_the_oldest_derivation_of_all_still_opens(self) -> None:
        """The pre-scrypt unsalted SHA-256. Nothing that ever worked may stop working."""
        from cryptography.fernet import Fernet

        token = (
            Fernet(crypto._derive_legacy_fernet_key("the-key")).encrypt(b"tautulli").decode("ascii")
        )
        assert SecretBox("the-key", salt=SALT).decrypt(token) == "tautulli"

    def test_a_retired_key_at_the_old_cost_still_opens(self) -> None:
        """Both dimensions at once: a rotated-away key AND a superseded cost."""
        from cryptography.fernet import Fernet, MultiFernet

        old = MultiFernet([Fernet(crypto._derive_fernet_key("old-key", SALT, 2**14))])
        token = old.encrypt(b"secret").decode("ascii")
        assert SecretBox("new-key", "old-key", salt=SALT).decrypt(token) == "secret"

    def test_rotate_re_keys_an_old_token_to_the_current_derivation(self) -> None:
        """``MultiFernet.rotate`` can only re-key a token one of its OWN fernets opens, and
        the superseded derivations deliberately sit outside that set -- yet old tokens are
        exactly the ones worth rotating."""
        old = _legacy_box(2**14, salt=SALT)
        token = old.encrypt(b"secret").decode("ascii")  # type: ignore[attr-defined]

        box = SecretBox("the-key", salt=SALT)
        fresh = box.rotate(token)

        assert box.decrypt(fresh) == "secret"
        # The rotated token opens under the CURRENT derivation alone, so the old one has
        # genuinely aged out of it rather than still being needed.
        current_only = _legacy_box(crypto._SCRYPT_N, salt=SALT)
        assert current_only.decrypt(fresh.encode("ascii")) == b"secret"  # type: ignore[attr-defined]

    def test_a_wrong_key_is_still_a_clear_error(self) -> None:
        """The fallback must not turn a genuinely wrong key into something else: it is the
        message that tells an operator their REAPER_SECRET_KEY changed."""
        token = SecretBox("original", salt=SALT).encrypt("hunter2")
        with pytest.raises(ValueError, match="REAPER_SECRET_KEY"):
            SecretBox("different", salt=SALT).decrypt(token)


class TestTheCostIsPaidOnceAndOnlyWhenNeeded:
    def test_construction_derives_one_key_per_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The current cost is deliberately high, so deriving every historical variant up
        front would multiply boot time by the number of derivations ever shipped."""
        calls: list[int] = []
        real = crypto._derive_fernet_key
        monkeypatch.setattr(
            crypto,
            "_derive_fernet_key",
            lambda secret, salt, n=crypto._SCRYPT_N: _record(calls, n, real(secret, salt, n)),
        )

        SecretBox("the-key", salt=SALT)
        assert calls == [crypto._SCRYPT_N]

    def test_the_superseded_derivations_are_built_only_on_a_miss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        box = SecretBox("the-key", salt=SALT)
        fresh = box.encrypt("x")

        built: list[int] = []
        real = crypto._derive_fernet_key
        monkeypatch.setattr(
            crypto,
            "_derive_fernet_key",
            lambda secret, salt, n=crypto._SCRYPT_N: _record(built, n, real(secret, salt, n)),
        )

        assert box.decrypt(fresh) == "x"
        assert built == [], "a token the current derivation opens must cost nothing extra"

        old = _legacy_box(2**14, salt=SALT)
        # Building the old box derives a key of its own, under the same patch, so the
        # assertion below reads the fixture rather than the miss unless this is cleared.
        built.clear()
        assert box.decrypt(old.encrypt(b"y").decode("ascii")) == "y"  # type: ignore[attr-defined]
        assert built, "a miss must build the superseded set"

        # And only once: a second old token reuses what the first one built.
        built.clear()
        assert box.decrypt(old.encrypt(b"z").decode("ascii")) == "z"  # type: ignore[attr-defined]
        assert built == []


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A throwaway database and the factory over it, which is what the scheduler's jobs
    take. ``session`` below is one session out of the same factory."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def session(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as s:
        yield s


class TestExpiredSessionsAreSwept:
    """``resolve_session`` drops an expired row when its cookie is presented again, so a
    device that is never opened again presented nothing and its row stayed forever (PR-13).
    """

    async def _seed(self, session: AsyncSession) -> AppUser:
        now = utcnow()
        user = AppUser(
            provider=AuthProvider.LOCAL, username="admin", is_active=True, created_at=now
        )
        session.add(user)
        await session.flush()
        session.add_all(
            [
                AuthSession(
                    token_hash=hash_token("stale"),
                    user_id=user.id,
                    created_at=now - timedelta(days=40),
                    expires_at=now - timedelta(days=10),
                ),
                AuthSession(
                    token_hash=hash_token("live"),
                    user_id=user.id,
                    created_at=now,
                    expires_at=now + timedelta(days=10),
                ),
            ]
        )
        await session.flush()
        return user

    async def test_it_removes_the_expired_and_keeps_the_live(self, session: AsyncSession) -> None:
        await self._seed(session)
        assert await sessions.sweep_expired(session) == 1
        assert await sessions.resolve_session(session, "live") is not None
        assert await sessions.resolve_session(session, "stale") is None

    async def test_it_is_a_no_op_when_nothing_has_expired(self, session: AsyncSession) -> None:
        now = utcnow()
        user = AppUser(
            provider=AuthProvider.LOCAL, username="admin", is_active=True, created_at=now
        )
        session.add(user)
        await session.flush()
        session.add(
            AuthSession(
                token_hash=hash_token("live"),
                user_id=user.id,
                created_at=now,
                expires_at=now + timedelta(days=10),
            )
        )
        await session.flush()
        assert await sessions.sweep_expired(session) == 0

    async def test_the_sweep_is_not_operator_schedulable(self) -> None:
        """Deleting rows whose window has already closed is not a choice to hand over, and
        an off switch on it could only ever let the table grow."""
        from reaper.services import scheduler

        assert scheduler.SESSION_SWEEP_JOB_ID not in scheduler.SCHEDULABLE_JOB_IDS
        assert scheduler.SESSION_SWEEP_JOB_ID not in scheduler.DEFAULT_MAINTENANCE_CRONS

    async def test_the_one_firing_sweeps_both_tables(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """`pending_plex_login` was the one table with a TTL and no scheduled sweeper.

        Its rows were dropped only inside `plex_link.start_pin`, which runs when somebody
        starts ANOTHER PIN, so an abandoned sign-in on an install where nobody starts one
        again sat there indefinitely. Its sibling `AuthSession` is the same shape and got a
        job with the reasoning written down (#710, rule 129).

        Driven through the scheduler's job rather than through the two sweep functions:
        those are already covered above and separately, and what this pins is that the ONE
        firing reaches both, which is the thing the fix is. Both tables carry a live row
        beside the expired one, so a job that emptied them wholesale would fail here.
        """
        from reaper.db.models import PendingPlexLogin
        from reaper.services import scheduler

        now = utcnow()
        async with factory() as session:
            session.add(
                AuthSession(
                    token_hash="dead",
                    user_id=1,
                    created_at=now - timedelta(days=40),
                    expires_at=now - timedelta(days=10),
                )
            )
            session.add(
                AuthSession(
                    token_hash="live",
                    user_id=1,
                    created_at=now,
                    expires_at=now + timedelta(days=10),
                )
            )
            session.add(
                PendingPlexLogin(
                    pin_id=101,
                    purpose="link",
                    created_at=now - timedelta(hours=2),
                    expires_at=now - timedelta(hours=1),
                )
            )
            session.add(
                PendingPlexLogin(
                    pin_id=102,
                    purpose="login",
                    created_at=now,
                    expires_at=now + timedelta(minutes=10),
                )
            )
            await session.commit()

        await scheduler.sweep_expired_sessions(factory)

        async with factory() as session:
            tokens = (await session.execute(select(AuthSession.token_hash))).scalars().all()
            pins = (await session.execute(select(PendingPlexLogin.pin_id))).scalars().all()
        assert sorted(tokens) == ["live"]
        assert sorted(pins) == [102]


class TestTheSignInPollHonorsItsDeadline:
    """``wait_for_pin`` checked its deadline only at the top of the loop, then slept for
    whatever a 429 named. A ``Retry-After`` of hours parked ``reaper-admin link-plex`` on a
    sleep with the terminal stuck on "Waiting..." (S2-2)."""

    class _Client(plextv.PlexTvClient):
        """Inherits the real client so an override that stops matching it fails the build,
        and never calls its `__init__`, so it owns no HTTP client and cannot reach out."""

        def __init__(self, retry_after: float | None) -> None:
            self.calls = 0
            self._retry_after = retry_after

        async def check_pin(self, pin_id: int) -> str | None:
            self.calls += 1
            raise IntegrationError(
                "plex.tv", "rate limited", status=429, retry_after=self._retry_after
            )

    async def _run(
        self, retry_after: float | None, timeout: float
    ) -> tuple[object, TestTheSignInPollHonorsItsDeadline._Client]:
        client = self._Client(retry_after)
        token = await plextv.PlexTvClient.wait_for_pin(client, 1, timeout=timeout)
        return token, client

    async def test_an_outrageous_retry_after_does_not_outlive_the_deadline(
        self, slept: list[float]
    ) -> None:
        """A day of ``Retry-After`` is capped to the backoff maximum, and the maximum is then
        clipped again to the twenty seconds the caller had left.

        What the deadline is worth has to be read off the delay, not off elapsed time: an
        unclipped sleep is instant here too, so a "it returned quickly" assertion held with
        the clipping deleted (rule 118, #346).
        """
        token, client = await self._run(86400.0, timeout=20.0)
        assert token is None
        assert slept == [20.0], "the poll slept past its own timeout"
        assert client.calls == 1

    async def test_the_backoff_is_capped_even_with_room_left(self) -> None:
        assert plextv.PIN_RATE_LIMIT_MAX_BACKOFF < 86400.0
        assert plextv.PIN_RATE_LIMIT_BACKOFF <= plextv.PIN_RATE_LIMIT_MAX_BACKOFF

    async def test_a_bare_429_still_backs_off_and_keeps_polling(self, slept: list[float]) -> None:
        """The reason the 429 arm exists at all: it means we polled too eagerly, not that
        the sign-in failed, so it must keep going rather than abandon the flow. Twelve
        seconds of window is two full backoffs and the two seconds left of a third."""
        token, client = await self._run(None, timeout=12.0)
        assert token is None
        assert client.calls == 3
        assert slept == [plextv.PIN_RATE_LIMIT_BACKOFF, plextv.PIN_RATE_LIMIT_BACKOFF, 2.0]
