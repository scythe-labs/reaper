# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin account management, and the invariant that keeps you out of a lockout.

The lockout risk is real and specific. If Plex OAuth is the only way in, then
*any* of these locks you out of your own tool:

* plex.tv is down, or its API changes
* your Plex token is revoked (a password change with "sign out devices" ticked)
* you rebuild the Plex server and the ``machineIdentifier`` changes, so the
  ownership check no longer matches
* Plex completes its migration to JWT device auth and the legacy flow stops working

None of those are hypothetical, and all of them are outside our control. So:

    **Reaper keeps at least one working local admin account.**

That has two halves, and for a long time only the second one existed. Setup
*establishes* it: the first-run wizard's opening step cannot be skipped, and the
password it sets goes through :func:`services.admin_password.set_password`, which
creates a local admin on an install that has none. :class:`LastAdminError` then
*maintains* it: the last local admin cannot be deleted, and cannot have local login
disabled.

Without the first half zero was a reachable state that nothing here ever tripped on,
because the invariant is enforced downward from one and never upward from zero. An
owner who claimed the server over Plex OAuth got a Plex-provider user;
:func:`count_local_admins` counts only ``LOCAL`` ones, so it was zero on an install
whose sign-in screen told the operator the opposite (#382).

Plex OAuth is *additive* convenience, never the sole key to the door.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.auth.passwords import generate_password, hash_password
from reaper.auth.sessions import close_all_for_user
from reaper.clock import utcnow
from reaper.db.models import AppUser, AuthProvider


class LastAdminError(RuntimeError):
    """Raised when an operation would leave Reaper with no way to log in."""


async def count_local_admins(session: AsyncSession, *, exclude_id: int | None = None) -> int:
    stmt = (
        select(func.count())
        .select_from(AppUser)
        .where(
            AppUser.provider == AuthProvider.LOCAL,
            AppUser.is_active.is_(True),
            AppUser.password_hash.is_not(None),
        )
    )
    if exclude_id is not None:
        stmt = stmt.where(AppUser.id != exclude_id)
    return int((await session.execute(stmt)).scalar_one())


async def create_local_admin(
    session: AsyncSession,
    username: str,
    password: str | None = None,
) -> tuple[AppUser, str]:
    """Create a local admin. Returns the user and the plaintext password.

    The plaintext is returned exactly once, for display to the operator; it is
    never stored and never logged.
    """
    existing = await session.scalar(select(AppUser).where(AppUser.username == username))
    if existing is not None:
        raise ValueError(f"A user named {username!r} already exists.")

    plaintext = password or generate_password()
    user = AppUser(
        provider=AuthProvider.LOCAL,
        username=username,
        password_hash=hash_password(plaintext),
        is_active=True,
        created_at=utcnow(),
    )
    session.add(user)
    await session.flush()
    return user, plaintext


async def set_password(session: AsyncSession, username: str, password: str | None = None) -> str:
    """Reset a local admin's password. Returns the new plaintext."""
    user = await session.scalar(select(AppUser).where(AppUser.username == username))
    if user is None:
        raise ValueError(f"No user named {username!r}.")

    plaintext = password or generate_password()
    user.password_hash = hash_password(plaintext)
    user.provider = AuthProvider.LOCAL  # a reset re-enables local login by definition
    user.is_active = True
    # A reset is the out-of-band lockout escape hatch; it must also evict any existing
    # sessions, since resolve_session validates only the token, never password_hash.
    await close_all_for_user(session, user.id)
    await session.flush()
    return plaintext


async def deactivate(session: AsyncSession, username: str) -> None:
    """Deactivate an admin, refusing if it would leave no way in."""
    user = await session.scalar(select(AppUser).where(AppUser.username == username))
    if user is None:
        raise ValueError(f"No user named {username!r}.")

    if await count_local_admins(session, exclude_id=user.id) == 0:
        raise LastAdminError(
            f"Refusing to deactivate {username!r}: it is the last local admin. "
            "Reaper would then be reachable only through Plex OAuth, and a Plex "
            "outage, a revoked token, or a rebuilt server would lock you out of "
            "your own tool. Create another local admin first."
        )

    user.is_active = False
    # resolve_session already refuses a deactivated user's cookie lazily, but evict the
    # rows now too: defense in depth, and the sessions table stops advertising a dead
    # account's devices.
    await close_all_for_user(session, user.id)
    await session.flush()
