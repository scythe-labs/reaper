# SPDX-License-Identifier: AGPL-3.0-or-later
"""Password hashing for the local fallback account.

Argon2id via ``pwdlib``. Explicitly *not* ``passlib``: its last release was
2020-10-08, it is unmaintained, and it breaks against modern bcrypt and the
removal of the ``crypt`` module in Python 3.13+.
"""

from __future__ import annotations

import secrets
import string

from pwdlib import PasswordHash

_hasher = PasswordHash.recommended()

# Long enough that online guessing is hopeless. Operator-set passwords can be
# shorter (see admin_password.MIN_PASSWORD_LENGTH), so the login route also
# throttles failed attempts and locks out after a burst -- see
# reaper.auth.ratelimit. This length is the floor for the *auto-generated* one.
GENERATED_PASSWORD_LENGTH = 24
_ALPHABET = string.ascii_letters + string.digits


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> tuple[bool, str | None]:
    """Return ``(is_valid, updated_hash)``.

    ``updated_hash`` is non-None when the stored hash used older parameters and
    should be rewritten -- that is how Argon2 cost is raised over time without
    forcing a password reset.
    """
    return _hasher.verify_and_update(password, password_hash)


def generate_password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(GENERATED_PASSWORD_LENGTH))
