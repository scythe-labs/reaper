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

The URL's host comes from :func:`recovery_base_url`, not from the bind address --
see there for why.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.auth.tokens import RECOVERY_TTL, generate_token, hash_token
from reaper.clock import expiry, utcnow
from reaper.db.models import RecoveryToken

log = structlog.get_logger(__name__)

#: Bind addresses that are not somewhere a browser can go. ``0.0.0.0`` (the default) and
#: ``::`` mean "every interface" to the socket layer and nothing at all to a person.
_UNROUTABLE_BINDS = frozenset({"", "0.0.0.0", "::", "[::]", "*"})  # noqa: S104

#: Stands in for the bind address when it is one of the above. An operator in a lockout
#: knows their own address; what they need from the banner is the port and the path.
_HOST_PLACEHOLDER = "<your-reaper-address>"


def recovery_base_url(host: str, port: int) -> str:
    """Where to tell the operator to open the recovery page.

    The banner used to interpolate ``settings.host`` straight in, which prints
    ``http://0.0.0.0:8420/recover`` on a default install -- a bind address, not a place
    (B-12). A bind that names every interface is replaced by a placeholder the operator
    fills in; one that names a real address is kept, because then it IS the answer.
    """
    cleaned = (host or "").strip()
    shown = _HOST_PLACEHOLDER if cleaned in _UNROUTABLE_BINDS else cleaned
    return f"http://{shown}:{port}"


async def mint_recovery_token(session: AsyncSession, *, base_url: str) -> str:
    """Create a single-use recovery token and print the link to the log."""
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
