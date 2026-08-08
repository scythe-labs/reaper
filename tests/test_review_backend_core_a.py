# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regressions from the backend-core-A review pass.

These cover the security-relevant edges the review found: the at-rest key must be
stretched *and* stay backward-compatible with data written under the old derivation,
a key rotation must actually work, a password change must invalidate other sessions,
and the log redactor must reach secrets nested below the top level.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.auth.admins import create_local_admin
from reaper.auth.admins import set_password as cli_reset_password
from reaper.auth.sessions import close_all_for_user, open_session, resolve_session
from reaper.auth.tokens import hash_token
from reaper.config import Settings
from reaper.crypto import SecretBox, _derive_legacy_fernet_key
from reaper.db.models import AuthSession
from reaper.logging import REDACTED, redact_secrets
from reaper.secrets import (
    SecretMaterialError,
    key_file_path,
    resolve_old_keys,
    resolve_secret_key,
)

# ---------------------------------------------------------------------------
# Credential encryption: stretching + backward compatibility + rotation
# ---------------------------------------------------------------------------


class TestSecretBoxKdf:
    def test_roundtrip_under_the_stretched_key(self) -> None:
        box = SecretBox("a-strong-operator-secret-key")
        assert box.decrypt(box.encrypt("sonarr-api-key")) == "sonarr-api-key"

    def test_data_written_with_the_old_sha256_derivation_still_decrypts(self) -> None:
        """The KDF changed from unsalted SHA-256 to scrypt. An upgrade must not brick
        credentials written under the old derivation, or every integration breaks
        silently on the next scan."""
        secret = "a-strong-operator-secret-key"
        legacy_ciphertext = Fernet(_derive_legacy_fernet_key(secret)).encrypt(b"legacy").decode()

        box = SecretBox(secret)
        assert box.decrypt(legacy_ciphertext) == "legacy"

    def test_a_key_rotation_decrypts_data_written_under_the_old_key(self) -> None:
        """The supported rotation: new key current, old key retired-but-still-readable."""
        old_box = SecretBox("old-key-value")
        old_ciphertext = old_box.encrypt("radarr-key")

        new_box = SecretBox("new-key-value", "old-key-value")
        assert new_box.decrypt(old_ciphertext) == "radarr-key"

    def test_a_fresh_key_with_no_old_key_cannot_decrypt_prior_data(self) -> None:
        """States the failure the rotation path exists to prevent: switch the key to a
        fresh value *without* supplying the old one and prior credentials are lost."""
        prior = SecretBox("old-key-value").encrypt("plex-token")
        with pytest.raises(ValueError):
            SecretBox("brand-new-key").decrypt(prior)


# ---------------------------------------------------------------------------
# Secret-key resolution: old-key config + atomic owner-only file
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(data_dir=tmp_path, secret_key=None, **overrides)  # type: ignore[arg-type]


class TestOldKeys:
    def test_comma_separated_old_keys_are_parsed_and_trimmed(self, tmp_path: Path) -> None:
        s = _settings(tmp_path, secret_key_old=SecretStr(" a , b ,, c "))
        assert resolve_old_keys(s) == ["a", "b", "c"]

    def test_no_old_keys_configured_is_empty(self, tmp_path: Path) -> None:
        assert resolve_old_keys(_settings(tmp_path)) == []


class TestKeyFilePermissions:
    def test_the_generated_key_file_is_owner_only(self, tmp_path: Path) -> None:
        """Created 0600 from the outset, never world-readable then chmod-ed."""
        resolve_secret_key(_settings(tmp_path))
        mode = stat.S_IMODE(key_file_path(_settings(tmp_path)).stat().st_mode)
        assert mode == 0o600, oct(mode)

    def test_a_blank_file_is_refused_rather_than_replaced(self, tmp_path: Path) -> None:
        """This used to mint a replacement key and carry on, which reads as recovery and is
        the opposite: the file decrypts every stored service credential, so a new one makes
        all of them unreadable in silence (S-5). Permissions are moot when nothing is
        written; what matters is that the file is left for the operator to fix.
        """
        s = _settings(tmp_path)
        s.ensure_data_dir()
        path = key_file_path(s)
        path.write_text("   \n")  # a blank file, most likely a crashed write
        path.chmod(0o644)

        with pytest.raises(SecretMaterialError, match="empty"):
            resolve_secret_key(s)
        assert path.read_text() == "   \n"


# ---------------------------------------------------------------------------
# Password change / reset must revoke sessions
# ---------------------------------------------------------------------------


class TestPasswordChangeRevokesSessions:
    async def test_admin_password_change_keeps_the_acting_session_and_revokes_others(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The intuitive 'reset my password after a suspected theft' must actually evict
        the other cookie, while not logging the operator out of the tab they used."""
        from reaper.services.admin_password import set_password

        async with async_factory() as session:
            user, _ = await create_local_admin(session, "owner", "origpassword")
            acting = await open_session(session, user)
            stolen = await open_session(session, user)
            await session.commit()

        async with async_factory() as session:
            await set_password(session, "newpassword1", keep_session_token=acting)
            await session.commit()

        async with async_factory() as session:
            assert await resolve_session(session, acting) is not None
            assert await resolve_session(session, stolen) is None

    async def test_cli_reset_revokes_every_session(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with async_factory() as session:
            user, _ = await create_local_admin(session, "owner", "origpassword")
            token = await open_session(session, user)
            await session.commit()

        async with async_factory() as session:
            await cli_reset_password(session, "owner", "resetpassword1")
            await session.commit()

        async with async_factory() as session:
            assert await resolve_session(session, token) is None

    async def test_close_all_except_spares_one_token(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with async_factory() as session:
            user, _ = await create_local_admin(session, "owner", "pw")
            keep = await open_session(session, user)
            drop = await open_session(session, user)
            await session.commit()
            uid = user.id

        async with async_factory() as session:
            await close_all_for_user(session, uid, except_token_hash=hash_token(keep))
            await session.commit()

        async with async_factory() as session:
            rows = (await session.execute(select(AuthSession))).scalars().all()
            assert [r.token_hash for r in rows] == [hash_token(keep)]
            assert await resolve_session(session, drop) is None


# ---------------------------------------------------------------------------
# Log redaction reaches nested secrets
# ---------------------------------------------------------------------------


class TestNestedRedaction:
    def test_a_secret_nested_in_a_dict_value_is_redacted(self) -> None:
        out = redact_secrets(None, "info", {"params": {"apikey": "SECRET", "page": 2}})  # type: ignore[arg-type]
        assert out["params"]["apikey"] == REDACTED
        assert out["params"]["page"] == 2

    def test_a_secret_nested_in_a_list_of_dicts_is_redacted(self) -> None:
        out = redact_secrets(None, "info", {"headers": [{"x-plex-token": "T"}]})  # type: ignore[arg-type]
        assert out["headers"][0]["x-plex-token"] == REDACTED

    def test_a_query_string_secret_in_a_bytes_value_is_redacted(self) -> None:
        out = redact_secrets(None, "info", {"body": b"?apikey=LEAK&x=1"})  # type: ignore[arg-type]
        assert "LEAK" not in str(out["body"])

    def test_binary_without_a_secret_is_left_alone(self) -> None:
        out = redact_secrets(None, "info", {"body": b"\xff\xfe raw"})  # type: ignore[arg-type]
        assert out["body"] == b"\xff\xfe raw"
