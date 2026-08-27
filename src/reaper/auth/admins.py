# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin account management, and the rule that keeps an operator from a lockout.

If Plex OAuth were the only way to sign in, any of these could lock an operator
out of their own tool, and none of them is under Reaper's control:

* plex.tv is down, or its API changes
* the Plex token is revoked (a password change with "sign out devices" checked)
* the Plex server is rebuilt, so its ``machineIdentifier`` changes and the
  ownership check no longer matches
* Plex retires the legacy sign-in flow this app was built against

So **Reaper always keeps at least one working local admin account.** Setup
creates it: the first-run wizard's opening step cannot be skipped, and the
password it sets goes through :func:`services.admin_password.set_password`,
which creates a local admin on an install that has none.
:class:`LastAdminError` then keeps it: it refuses to delete the last local
admin, and refuses to turn off that admin's local login.

:func:`count_local_admins` counts only ``LOCAL`` accounts, so an owner who
signed in through Plex OAuth alone does not count as one.

Plex OAuth is an added way to sign in. It is never the only way in.
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
    user.provider = AuthProvider.LOCAL  # resetting the password re-enables local login
    user.is_active = True
    # A password reset must also sign out every existing session, because
    # resolve_session checks only the token, never the password hash.
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
    # resolve_session already refuses a deactivated user's cookie the next time it is
    # used, but deleting the session rows now too keeps the sessions list from still
    # showing a deactivated account's devices.
    await close_all_for_user(session, user.id)
    await session.flush()
