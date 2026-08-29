# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opaque token generation and hashing.

Sessions and recovery links are random opaque strings, not JWTs. There is exactly one
server, so a stateless token buys nothing, while instant revocation, of a stolen cookie
or of every device at once, matters a great deal for a tool that can delete a media
library.

Only the SHA-256 of a token is stored, so a leaked database hands out no live sessions.
Lookups compare hashes, so they are constant-time in the value that matters.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

SESSION_TTL = timedelta(days=30)
RECOVERY_TTL = timedelta(minutes=15)

_TOKEN_BYTES = 32


def generate_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
