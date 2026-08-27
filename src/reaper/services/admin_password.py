# SPDX-License-Identifier: AGPL-3.0-or-later
"""The admin password: the key that turns deletion on.

Turning deletion on is the one destructive-action control the UI is allowed to flip, and
it is gated on this password so that a stray click, a shared session, or a stale browser
tab cannot arm the tool by accident. It is the local admin password, the same one that
serves as the anti-lockout fallback (see ``auth.admins``), so an install always has one to
verify against once it has been set.

Turning deletion off is never gated. Making Reaper safer should always be one click.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.auth.passwords import generate_password, hash_password, verify_password
from reaper.auth.ratelimit import argon2_gate
from reaper.auth.sessions import close_all_for_user
from reaper.auth.tokens import hash_token
from reaper.clock import utcnow
from reaper.db.models import AppUser, AuthProvider
from reaper.refusal import Refusal

# The admin password arms deletion, so it needs a stronger floor than a throwaway login
# would. Length is the single biggest factor in resistance to guessing. This only applies
# when a password is set: an existing shorter password still logs in, and the
# auto-generated one (GENERATED_PASSWORD_LENGTH = 24) clears this floor comfortably.
MIN_PASSWORD_LENGTH = 12

#: A decoy hash to verify against when no local admin exists, so "wrong password" and "no
#: password set yet" take the same time and neither is distinguishable by timing.
_DECOY = hash_password(generate_password())


class PasswordError(Refusal):
    """A password could not be set (too short, etc.). A catalog code plus raw params."""


class PasswordVerificationBusyError(RuntimeError):
    """Too much password hashing is already in flight; try again in a moment.

    A capacity refusal, never a credential one. The caller answers 503 and must not
    count it against any lockout, or a busy server would lock out the real operator.
    """


async def _local_admins(session: AsyncSession) -> list[AppUser]:
    return list(
        (
            await session.execute(
                select(AppUser)
                .where(
                    AppUser.provider == AuthProvider.LOCAL,
                    AppUser.is_active.is_(True),
                    AppUser.password_hash.is_not(None),
                )
                .order_by(AppUser.id)
            )
        )
        .scalars()
        .all()
    )


async def has_password(session: AsyncSession) -> bool:
    """Whether an admin password has been set (i.e. an active local admin exists)."""
    return bool(await _local_admins(session))


async def verify(session: AsyncSession, password: str) -> bool:
    """Check ``password`` against every local admin. Runs a constant amount of hashing
    whether or not one matches, so timing does not leak whether a password is set.

    Takes one Argon2 gate slot per hash it is about to run, and raises
    :class:`PasswordVerificationBusyError` if that many are not free. The gate caps how
    much Argon2 work can run at once, and this is the one caller whose work is not a
    single hash: an install with several local admins verifies against each one, so the
    gate must charge for every hash this call makes, not just one, or the real CPU cost
    would be unbounded. The gate is taken here rather than at the route, because only
    this function knows how many hashes there will be.
    """
    admins = await _local_admins(session)
    # At least one: with no admins we still hash the decoy, to keep the timing.
    slots = argon2_gate.acquire(max(1, len(admins)))
    if not slots:
        raise PasswordVerificationBusyError
    try:
        matched = False
        for user in admins:
            ok, _ = verify_password(password, user.password_hash or _DECOY)
            matched = matched or ok
        if not admins:
            verify_password(password, _DECOY)  # keeps the timing the same even with no admins
        return matched
    finally:
        argon2_gate.release(slots)


async def unique_username(session: AsyncSession, desired: str, fallback: str) -> str:
    """A username not already taken. Plex usernames are unique on Plex, but a
    local admin might happen to share one, and ``username`` is unique here."""
    base = desired.strip() or fallback
    candidate, suffix = base, 2
    while await session.scalar(select(AppUser.id).where(AppUser.username == candidate)):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


async def set_password(
    session: AsyncSession,
    password: str,
    *,
    username_hint: str = "admin",
    keep_session_token: str | None = None,
) -> str:
    """Set or create the admin password. Returns the local admin's username.

    Updates the first existing local admin if there is one, otherwise creates one, so a
    Plex-only install can set a password without touching the CLI. Refuses a password
    shorter than :data:`MIN_PASSWORD_LENGTH`.

    Changing an existing password revokes that admin's other sessions. Resetting a
    password after a suspected cookie theft must actually lock the thief out, and
    validating a cookie never touches ``password_hash``, so nothing else would revoke
    those sessions. Pass ``keep_session_token``, the acting admin's own cookie, to spare
    the current tab.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError("error.password.too_short", min_length=MIN_PASSWORD_LENGTH)

    admins = await _local_admins(session)
    if admins:
        admins[0].password_hash = hash_password(password)
        # A password change must invalidate every session the old password could have
        # authorized, except, optionally, the one making the change.
        await close_all_for_user(
            session,
            admins[0].id,
            except_token_hash=hash_token(keep_session_token) if keep_session_token else None,
        )
        await session.flush()
        return admins[0].username

    user = AppUser(
        provider=AuthProvider.LOCAL,
        username=await unique_username(session, username_hint, "admin"),
        password_hash=hash_password(password),
        is_active=True,
        created_at=utcnow(),
    )
    session.add(user)
    await session.flush()
    return user.username
