# SPDX-License-Identifier: AGPL-3.0-or-later
"""Anti-lockout tests.

These guard the property that keeps the owner able to reach their own tool:
Plex OAuth is additive convenience, never the sole key to the door.
"""

from __future__ import annotations

import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.auth.admins import (
    LastAdminError,
    count_local_admins,
    create_local_admin,
    deactivate,
    set_password,
)
from reaper.auth.passwords import hash_password, verify_password
from reaper.auth.recovery import (
    clear_recovery_file,
    mint_recovery_token,
    recovery_file_path,
    redeem_recovery_token,
)
from reaper.auth.tokens import generate_token, hash_token
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import AppUser, AuthProvider
from reaper.db.session import create_engine, create_session_factory


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


class TestLastAdminInvariant:
    async def test_last_local_admin_cannot_be_deactivated(self, session: AsyncSession) -> None:
        await create_local_admin(session, "admin")

        with pytest.raises(LastAdminError, match="last local admin"):
            await deactivate(session, "admin")

        # And they are genuinely still able to log in.
        assert await count_local_admins(session) == 1

    async def test_a_local_admin_can_be_deactivated_when_another_remains(
        self, session: AsyncSession
    ) -> None:
        await create_local_admin(session, "admin")
        await create_local_admin(session, "backup")

        await deactivate(session, "admin")
        assert await count_local_admins(session) == 1

    async def test_a_plex_only_user_does_not_count_as_a_way_back_in(
        self, session: AsyncSession
    ) -> None:
        """A Plex-authenticated admin is useless if plex.tv is unreachable or the token
        has been revoked, so it must not satisfy the 'at least one admin' invariant."""
        session.add(
            AppUser(
                provider=AuthProvider.PLEX,
                username="plexuser",
                plex_account_id=1234,
                password_hash=None,
                is_active=True,
                created_at=utcnow(),
            )
        )
        await session.flush()

        assert await count_local_admins(session) == 0

    async def test_reset_password_restores_local_login_for_a_plex_only_account(
        self, session: AsyncSession
    ) -> None:
        """The realistic recovery: your only admin is Plex-linked, Plex breaks,
        and you need in. `reaper-admin reset-password` must work anyway."""
        session.add(
            AppUser(
                provider=AuthProvider.PLEX,
                username="owner",
                plex_account_id=1234,
                password_hash=None,
                is_active=True,
                created_at=utcnow(),
            )
        )
        await session.flush()
        assert await count_local_admins(session) == 0

        plaintext = await set_password(session, "owner")

        assert await count_local_admins(session) == 1
        user = await session.scalar(
            __import__("sqlalchemy", fromlist=["select"])
            .select(AppUser)
            .where(AppUser.username == "owner")
        )
        assert user is not None and user.password_hash is not None
        ok, _ = verify_password(plaintext, user.password_hash)
        assert ok


class TestPasswords:
    def test_roundtrip(self) -> None:
        h = hash_password("correct horse battery staple")
        ok, _ = verify_password("correct horse battery staple", h)
        assert ok

    def test_wrong_password_rejected(self) -> None:
        ok, _ = verify_password("wrong", hash_password("right"))
        assert not ok

    def test_hash_is_argon2id_and_not_the_plaintext(self) -> None:
        h = hash_password("hunter2")
        assert "hunter2" not in h
        assert h.startswith("$argon2id$")

    def test_generated_passwords_are_unique(self) -> None:
        from reaper.auth.passwords import generate_password

        assert len({generate_password() for _ in range(20)}) == 20


class TestRecoveryToken:
    async def test_redeem_succeeds_once_then_never_again(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        token = await mint_recovery_token(
            session, base_url="http://localhost:8420", data_dir=tmp_path
        )

        assert await redeem_recovery_token(session, token) is True
        # Single use: the same link cannot be replayed.
        assert await redeem_recovery_token(session, token) is False

    async def test_the_code_is_never_carried_in_the_url(
        self, session: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The token must be a code to paste, not a ``?token=`` query parameter -- else
        the ``GET /recover?token=...`` request line lands in a reverse proxy's access log.
        The printed banner shows a bare ``/recover`` URL and the code on its own line."""
        token = await mint_recovery_token(
            session, base_url="http://localhost:8420", data_dir=tmp_path
        )
        banner = capsys.readouterr().out

        assert "?token=" not in banner  # the token never rides in the URL
        assert f"?token={token}" not in banner
        assert "http://localhost:8420/recover" in banner  # a bare path, no query
        assert token in banner  # the code is printed for the operator to paste

    async def test_unknown_token_rejected(self, session: AsyncSession, tmp_path: Path) -> None:
        await mint_recovery_token(session, base_url="http://localhost:8420", data_dir=tmp_path)
        assert await redeem_recovery_token(session, generate_token()) is False

    async def test_minting_again_invalidates_the_previous_link(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """Only one recovery link may be live, so a forgotten one left in an old
        log cannot be used later."""
        first = await mint_recovery_token(
            session, base_url="http://localhost:8420", data_dir=tmp_path
        )
        await mint_recovery_token(session, base_url="http://localhost:8420", data_dir=tmp_path)

        assert await redeem_recovery_token(session, first) is False

    async def test_expired_token_rejected(self, session: AsyncSession, tmp_path: Path) -> None:
        from datetime import timedelta

        from sqlalchemy import select

        from reaper.clock import utcnow
        from reaper.db.models import RecoveryToken

        token = await mint_recovery_token(
            session, base_url="http://localhost:8420", data_dir=tmp_path
        )
        row = await session.scalar(
            select(RecoveryToken).where(RecoveryToken.token_hash == hash_token(token))
        )
        assert row is not None
        row.expires_at = utcnow() - timedelta(seconds=1)
        await session.flush()

        assert await redeem_recovery_token(session, token) is False

    async def test_only_the_hash_is_persisted(self, session: AsyncSession, tmp_path: Path) -> None:
        """A database leak must not hand out a live recovery link."""
        from sqlalchemy import select

        from reaper.db.models import RecoveryToken

        token = await mint_recovery_token(
            session, base_url="http://localhost:8420", data_dir=tmp_path
        )
        rows = (await session.execute(select(RecoveryToken))).scalars().all()

        assert all(r.token_hash != token for r in rows)
        assert any(r.token_hash == hash_token(token) for r in rows)


class TestTheRecoveryCodeReachesADesktopOperator:
    """The console is not a channel on a windowed Windows build or a Finder-launched
    ``.app``: PyInstaller hands those processes no stdout and ``entry.py`` substitutes
    devnull, so the banner went nowhere and a locked-out operator had no way back in at
    all (#433). The file in the data folder is the channel that reaches them."""

    async def test_the_code_is_written_to_the_data_folder(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        token = await mint_recovery_token(
            session, base_url="http://localhost:8420", data_dir=tmp_path
        )

        written = recovery_file_path(tmp_path).read_text(encoding="utf-8")
        assert token in written
        assert "http://localhost:8420/recover" in written
        assert "?token=" not in written  # same rule as the banner: never in a URL

    async def test_the_file_is_owner_only_from_creation(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """0600 at creation, never a chmod after the bytes have landed (rule 14/83).

        Skipped on Windows, where the mode bits are inert and the folder's ACL is the
        protection -- but the assertion still runs everywhere else, so the mode can never
        drift unnoticed on the platforms that honor it (rule 119)."""
        if sys.platform == "win32":  # pragma: no cover -- CI and dev are POSIX
            pytest.skip("POSIX mode bits are not the access control on Windows")
        await mint_recovery_token(session, base_url="http://localhost:8420", data_dir=tmp_path)

        assert stat.S_IMODE(recovery_file_path(tmp_path).stat().st_mode) == 0o600

    async def test_minting_again_replaces_the_previous_code(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """The file must never offer a code the database has already invalidated."""
        first = await mint_recovery_token(
            session, base_url="http://localhost:8420", data_dir=tmp_path
        )
        second = await mint_recovery_token(
            session, base_url="http://localhost:8420", data_dir=tmp_path
        )

        written = recovery_file_path(tmp_path).read_text(encoding="utf-8")
        assert second in written
        assert first not in written

    async def test_a_missing_data_folder_is_created_not_fatal(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        target = tmp_path / "not-yet-there"
        token = await mint_recovery_token(
            session, base_url="http://localhost:8420", data_dir=target
        )

        assert token in recovery_file_path(target).read_text(encoding="utf-8")

    async def test_an_unwritable_data_folder_still_mints(
        self, session: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A file that cannot be written must never cost the operator the code itself:
        the console is still live on the container and the snap, and the token is already
        in the database. It degrades to one channel, it does not fail."""
        if sys.platform == "win32":  # pragma: no cover -- CI and dev are POSIX
            pytest.skip("a read-only directory is not the same refusal on Windows")
        blocked = tmp_path / "blocked"
        blocked.mkdir(mode=0o500)
        try:
            token = await mint_recovery_token(
                session, base_url="http://localhost:8420", data_dir=blocked
            )
            assert token in capsys.readouterr().out
            assert await redeem_recovery_token(session, token) is True
        finally:
            blocked.chmod(0o700)

    def test_clearing_is_idempotent_and_removes_the_file(self, tmp_path: Path) -> None:
        path = recovery_file_path(tmp_path)
        path.write_text("a spent code", encoding="utf-8")

        clear_recovery_file(tmp_path)
        assert not path.exists()
        clear_recovery_file(tmp_path)  # nothing there is not an error
        assert not path.exists()

    def test_clearing_takes_the_half_written_sibling_too(self, tmp_path: Path) -> None:
        """A kill between the O_EXCL open and the rename strands a `.tmp` holding a live
        code. Neither the redemption sweep nor the boot sweep would ever have looked at it,
        so it is the one shape this channel must not leave behind."""
        stranded = recovery_file_path(tmp_path).with_name("recovery.txt.tmp")
        stranded.write_text("a live code nobody knows about", encoding="utf-8")

        clear_recovery_file(tmp_path)
        assert not stranded.exists()

    async def test_a_failed_write_leaves_nothing_rather_than_a_dead_code(
        self, session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Minting invalidates the previous code before the file is written, so a write that
        fails must not leave the old file sitting there. It reads exactly like a working one
        and cannot sign anyone in, on the builds where it is the only channel there is, and
        the warning about it reaches only the Logs tab the operator cannot get to."""
        first = await mint_recovery_token(
            session, base_url="http://localhost:8420", data_dir=tmp_path
        )
        assert first in recovery_file_path(tmp_path).read_text(encoding="utf-8")

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("no space left on device")

        monkeypatch.setattr("reaper.auth.recovery._write_owner_only", _boom)
        second = await mint_recovery_token(
            session, base_url="http://localhost:8420", data_dir=tmp_path
        )

        assert not recovery_file_path(tmp_path).exists()
        # The old code really is dead by then, which is what made leaving it so bad.
        assert await redeem_recovery_token(session, first) is False
        assert await redeem_recovery_token(session, second) is True
