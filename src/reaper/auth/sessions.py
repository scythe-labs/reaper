# SPDX-License-Identifier: AGPL-3.0-or-later
"""The session lifecycle: open a session, resolve one from a cookie, revoke.

Sessions are opaque random tokens, hashed before storage -- never JWTs. The
reasoning lives in :mod:`reaper.auth.tokens`: one server, so statelessness buys
nothing, while instant revocation of a stolen cookie is worth a great deal for a
tool that can delete a media library.

Only the SHA-256 of the token is stored. The plaintext exists for exactly as long
as it takes to put it in a ``Set-Cookie`` header; a database leak therefore hands
out no live sessions.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.auth.tokens import SESSION_TTL, generate_token, hash_token
from reaper.clock import expiry, utcnow
from reaper.db.models import AppUser, AuthSession

# A session is a FIXED 30-day window from login, not a sliding one: the cookie is set at
# login (and at a Plex poll or a recovery redemption, which are logins too) and never
# re-set afterwards, so using the app does not extend it. Deliberate -- a stolen cookie
# that is being used should still expire on schedule -- and stated here because the
# comment that used to sit in this spot described a per-request refresh that no code does
# (I-3), which would have made the throttle below look like a write-amplification guard
# rather than what it is.
#
# What IS written per request is the row's ``last_seen``, and rewriting that on every
# authenticated request would be a needless write per page load. Bump it at most this
# often: fresh enough to answer "when was this device last used?", cheap enough not to be
# a write amplifier under WAL.
_LAST_SEEN_THROTTLE = timedelta(minutes=5)


async def open_session(
    session: AsyncSession, user: AppUser, *, user_agent: str | None = None
) -> str:
    """Mint a session for ``user`` and return the plaintext token.

    The token is returned exactly once, to be written into a cookie; only its hash
    is persisted. The caller commits.
    """
    token = generate_token()
    now = utcnow()
    session.add(
        AuthSession(
            token_hash=hash_token(token),
            user_id=user.id,
            created_at=now,
            expires_at=expiry(SESSION_TTL, now=now),
            last_seen_at=now,
            # Trim the UA: it is a courtesy field for "your devices", not evidence.
            user_agent=(user_agent or None) and user_agent[:300],
        )
    )
    user.last_login_at = now
    await session.flush()
    return token


async def resolve_session(session: AsyncSession, token: str | None) -> AppUser | None:
    """The active user behind a cookie, or ``None``.

    Returns ``None`` for an absent, unknown, expired, or deactivated session --
    every failure mode collapses to "not logged in", because the caller should
    treat them identically and a distinguishable error only helps an attacker.

    Expired rows are deleted on sight so the table self-prunes, and ``last_seen``
    is bumped (throttled) so the sessions list stays honest. The caller commits.
    """
    if not token:
        return None

    row = (
        await session.execute(
            select(AuthSession).where(AuthSession.token_hash == hash_token(token))
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    now = utcnow()
    if row.expires_at <= now:
        await session.delete(row)
        return None

    user = await session.get(AppUser, row.user_id)
    if user is None or not user.is_active:
        # A deactivated admin's live sessions must stop working immediately.
        await session.delete(row)
        return None

    if row.last_seen_at is None or (now - row.last_seen_at) > _LAST_SEEN_THROTTLE:
        row.last_seen_at = now

    return user


async def close_session(session: AsyncSession, token: str | None) -> None:
    """Revoke a single session (logout). Idempotent. The caller commits."""
    if not token:
        return
    await session.execute(delete(AuthSession).where(AuthSession.token_hash == hash_token(token)))


async def sweep_expired(session: AsyncSession) -> int:
    """Delete every session whose window has closed. Returns how many went. Caller commits.

    :func:`resolve_session` already drops an expired row the moment its cookie is presented
    again, but a device that is never opened again presents nothing -- so its row sat there
    forever, and the table only grew on an install that had been used for a while (PR-13).
    An expired row authorizes nothing either way; this is housekeeping, not a control.
    """
    result = await session.execute(delete(AuthSession).where(AuthSession.expires_at <= utcnow()))
    return int(getattr(result, "rowcount", 0) or 0)


async def close_all_for_user(
    session: AsyncSession, user_id: int, *, except_token_hash: str | None = None
) -> None:
    """Revoke every session for a user -- the "sign out everywhere" primitive.

    Called when an admin's password is reset (a stolen cookie must stop working) and
    when an admin is deactivated. Pass ``except_token_hash`` to spare one session --
    the acting admin's own, so changing your own password does not log you out of the
    tab you are using while it still evicts every *other* device. The caller commits.
    """
    stmt = delete(AuthSession).where(AuthSession.user_id == user_id)
    if except_token_hash is not None:
        stmt = stmt.where(AuthSession.token_hash != except_token_hash)
    await session.execute(stmt)
