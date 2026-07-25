# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recovery mode -- the last resort when you cannot log in at all.

Set ``REAPER_RECOVERY=true`` and restart. Reaper mints one single-use link,
valid for 15 minutes, and prints it to the container log:

    docker compose logs reaper | grep -A2 RECOVERY

Redeeming it grants a normal admin session, bypassing both Plex OAuth and the
local password. Then turn the flag back off.

Why this is safe: obtaining the link requires setting an environment variable
*and* reading the container's logs. Anyone who can do both already has host
access, and could simply open the SQLite file and rewrite the password hash.
Recovery mode adds no new capability -- it only makes the legitimate path
convenient instead of requiring surgery on the database.

Why it is still bounded: single-use, 15 minutes, minted only at boot with the
flag set, and every redemption is written to the audit trail.

The token is delivered as a **code to paste**, not embedded in the link's query
string: the banner prints a bare ``/recover`` URL plus the code on its own line, and
the browser sends the code in the ``POST /recover`` body. Nothing carries the token in
a request line, so a fronting reverse proxy's access log never records it -- closing
the one residual exposure a ``GET /recover?token=...`` link would have left open.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.auth.tokens import RECOVERY_TTL, generate_token, hash_token
from reaper.clock import expiry, utcnow
from reaper.db.models import RecoveryToken

log = structlog.get_logger(__name__)


async def mint_recovery_token(session: AsyncSession, *, base_url: str) -> str:
    """Create a single-use recovery token and print it to the CONSOLE.

    Not "to the log": the banner below goes out through ``print``, deliberately (see the
    comment on it), so it never reaches structlog, the in-app Logs tab, or the rotating
    files the Logs tab downloads. ``docker logs`` is where the operator finds it, and the
    recovery screen's copy has to say so (U-11).
    """
    # Invalidate any earlier unredeemed tokens: only one may be live at a time.
    stale = (
        (await session.execute(select(RecoveryToken).where(RecoveryToken.used_at.is_(None))))
        .scalars()
        .all()
    )
    now = utcnow()
    for token in stale:
        token.used_at = now

    plaintext = generate_token()
    session.add(
        RecoveryToken(
            token_hash=hash_token(plaintext),
            created_at=now,
            expires_at=expiry(RECOVERY_TTL),
        )
    )
    await session.flush()

    recover_url = f"{base_url.rstrip('/')}/recover"
    minutes = int(RECOVERY_TTL.total_seconds() // 60)

    # Deliberately not a structlog event: the token must not be shipped to a log
    # aggregator as a structured, searchable field. It goes to the console only.
    # The code is printed on its own line rather than baked into the URL, so it is
    # never carried in a request line that a reverse proxy would log.
    banner = (
        "\n"
        "  ============================ RECOVERY ============================\n"
        f"  Open:  {recover_url}\n"
        f"  Code:  {plaintext}\n"
        f"  Paste the code on that page. Single use, expires in {minutes} minutes.\n"
        "  The code is not in the URL, so a reverse proxy's access log never sees it.\n"
        "  Set REAPER_RECOVERY=false and restart once you are back in.\n"
        "  =================================================================\n"
    )
    print(banner)  # noqa: T201 -- console-only by design; see above
    return plaintext


async def redeem_recovery_token(session: AsyncSession, plaintext: str) -> bool:
    """Consume a recovery token. False if unknown, expired, or already used."""
    token = await session.scalar(
        select(RecoveryToken).where(RecoveryToken.token_hash == hash_token(plaintext))
    )
    if token is None:
        log.warning("recovery.rejected", reason="unknown token")
        return False
    if token.used_at is not None:
        log.warning("recovery.rejected", reason="token already used")
        return False
    if token.expires_at <= utcnow():
        log.warning("recovery.rejected", reason="token expired")
        return False

    token.used_at = utcnow()
    await session.flush()
    log.warning("recovery.redeemed", detail="A recovery link was used to gain admin access.")
    return True
