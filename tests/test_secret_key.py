# SPDX-License-Identifier: AGPL-3.0-or-later
"""Secret-key resolution.

The dangerous failure here is not "no key" -- that fails loudly. It is a key that
*changes*. Every integration credential in the database is encrypted with it, so
a fresh key on the next boot silently renders all of them unreadable, and the
damage is only discovered the next time a scan tries to reach Sonarr.

So the property under test is: **stable across boots**.
"""

from __future__ import annotations

import stat
from pathlib import Path

from pydantic import SecretStr

from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.secrets import key_file_path, resolve_secret_key


def _settings(tmp_path: Path, key: str | None = None) -> Settings:
    return Settings(  # type: ignore[call-arg]
        data_dir=tmp_path,
        secret_key=SecretStr(key) if key else None,
    )


class TestAutoGeneration:
    def test_a_key_is_generated_when_none_is_configured(self, tmp_path: Path) -> None:
        key = resolve_secret_key(_settings(tmp_path))

        assert key
        assert key_file_path(_settings(tmp_path)).is_file()

    def test_the_key_is_stable_across_boots(self, tmp_path: Path) -> None:
        """The whole point. A key that changes destroys every stored credential."""
        first = resolve_secret_key(_settings(tmp_path))
        second = resolve_secret_key(_settings(tmp_path))

        assert first == second

    def test_credentials_encrypted_on_one_boot_decrypt_on_the_next(self, tmp_path: Path) -> None:
        """The failure this guards against, stated end to end."""
        boot1 = SecretBox(resolve_secret_key(_settings(tmp_path)))
        ciphertext = boot1.encrypt("sonarr-api-key")

        boot2 = SecretBox(resolve_secret_key(_settings(tmp_path)))
        assert boot2.decrypt(ciphertext) == "sonarr-api-key"

    def test_the_key_file_is_not_world_readable(self, tmp_path: Path) -> None:
        resolve_secret_key(_settings(tmp_path))
        path = key_file_path(_settings(tmp_path))

        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"key file is {oct(mode)}, expected 0o600"

    def test_generated_keys_are_unique_per_installation(self, tmp_path: Path) -> None:
        a = resolve_secret_key(_settings(tmp_path / "a"))
        b = resolve_secret_key(_settings(tmp_path / "b"))
        assert a != b

    def test_an_empty_key_file_is_regenerated_rather_than_used(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        settings.ensure_data_dir()
        key_file_path(settings).write_text("   \n")

        key = resolve_secret_key(settings)
        assert key.strip()


class TestExplicitKeyWins:
    def test_the_env_key_is_used_when_set(self, tmp_path: Path) -> None:
        assert resolve_secret_key(_settings(tmp_path, "explicit-key")) == "explicit-key"

    def test_no_key_file_is_written_when_the_key_is_configured(self, tmp_path: Path) -> None:
        """If you manage the key in a secret manager, Reaper must not scatter a
        copy of it next to the database."""
        resolve_secret_key(_settings(tmp_path, "explicit-key"))

        assert not key_file_path(_settings(tmp_path)).exists()

    def test_an_explicit_key_does_not_overwrite_an_existing_key_file(self, tmp_path: Path) -> None:
        """Setting REAPER_SECRET_KEY later must not destroy the generated key --
        the database is still encrypted with the old one, and the operator may
        need to recover it."""
        generated = resolve_secret_key(_settings(tmp_path))

        resolve_secret_key(_settings(tmp_path, "explicit-key"))

        assert key_file_path(_settings(tmp_path)).read_text().strip() == generated

    def test_a_blank_env_key_falls_back_to_generation(self, tmp_path: Path) -> None:
        """An unset variable in a .env file arrives as '' rather than None."""
        key = resolve_secret_key(_settings(tmp_path, "   "))

        assert key.strip()
        assert key_file_path(_settings(tmp_path)).is_file()
