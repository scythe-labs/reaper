# SPDX-License-Identifier: AGPL-3.0-or-later
"""Logging in: Plex OAuth (with the ownership check) and the local fallback.

This is distinct from :mod:`reaper.services.plex_link`, which *links the server*.
Linking is a one-time setup act; logging in happens every session. What they share lives
there: the client identifier and the ownership decision, both imported here, and the PIN
start (:func:`~reaper.services.plex_link.start_pin`), which each flow's own route calls
with its purpose. The polling halves are not shared, and the note above
:func:`poll_plex_login` says why.

Three shapes of sign-in resolve to the same thing -- a minted session for a
verified admin:

* **Plex login.** The server is already linked, so we know its machine id. The
  user signs in on plex.tv, and we require *their own* token to report this
  machine as ``owned``. Authenticating is not enough; a stranger with a valid
  Plex account, or one of the hundred people you share the library with, must be
  turned away. (See :meth:`PlexTvClient.owns_server`.)

* **Plex setup.** No server is linked yet. The first owner to sign in claims the
  server: we link it (refusing anyone who owns no server, or more than one) and
  create their admin account in the same step. Once a server is linked this path
  is unreachable, so it cannot be used to hijack an already-configured Reaper.

* **Local login.** Username and Argon2id password. The anti-lockout account, and
  the way in when plex.tv is down. Failures are deliberately indistinguishable
  from each other, and take the same time whether or not the user exists.

The browser never handles a Plex token. It is told to open an auth URL; the
*backend* polls plex.tv for approval. (Overseerr posts the token from the page to
its own API, exposing a full-power account credential to anything running in the
tab. We do not.)
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.auth.passwords import generate_password, hash_password, verify_password
from reaper.auth.sessions import open_session
from reaper.clients.base import IntegrationError
from reaper.clients.plextv import PlexAccount, PlexTvClient
from reaper.clock import utcnow
from reaper.config import RuntimeSafety
from reaper.crypto import SecretBox
from reaper.db.models import AppUser, AuthProvider, PendingPlexLogin, PlexServer
from reaper.engine.reason import ReasonParam
from reaper.refusal import Refusal
from reaper.services.admin_password import unique_username
from reaper.services.plex_link import (
    PlexLinkError,
    PlexLinkRetryableError,
    PlexServerChoiceNeededError,
    client_identifier,
    complete_link,
)

log = structlog.get_logger(__name__)

# A precomputed hash to verify against when the account does not exist, so a login
# attempt costs the same Argon2 work whether or not the username is real. Without
# it, "no such user" returns fast and "wrong password" returns slow, and the
# difference enumerates valid usernames.
_DUMMY_HASH = hash_password(generate_password())


class LoginError(Refusal):
    """Sign-in did not succeed. A catalog code plus raw params (``reaper.refusal``).

    Defaults to 401 rather than ``Refusal``'s own 422: every route answering one of these
    (``api.auth``'s Plex and local sign-in) has always answered 401, a credential refusal,
    never 422's "well-formed but refused content."
    """

    def __init__(self, code: str, /, *, status: int = 401, **params: ReasonParam) -> None:
        super().__init__(code, status=status, **params)


@dataclass(frozen=True)
class UserView:
    """The admin, as the frontend sees them. No secrets."""

    id: int
    username: str
    provider: str
    thumb_url: str | None


@dataclass(frozen=True)
class LoginResult:
    session_token: str
    user: UserView
    # True when this sign-in also performed first-run setup (linked the server).
    setup: bool = False


def _view(user: AppUser) -> UserView:
    return UserView(
        id=user.id,
        username=user.username,
        provider=str(user.provider),
        thumb_url=user.thumb_url,
    )


# ---------------------------------------------------------------------------
# Plex. The flow opens at plex_link.start_pin(purpose="login"); what follows is the half
# that is not shared with the link flow. The two pollers agree on the plex.tv round trip
# and diverge after it, so one function serving both would take a flag, which is what
# killed W6-3's shared paged(). The divergence: this one mints a session, and it branches
# on whether a server is already linked, authorizing against that machine id when one is
# and running first-run setup through complete_link when none is. It consumes the pending
# row on each refusal arm, where poll_link consumes in a finally.
# ---------------------------------------------------------------------------


async def poll_plex_login(
    session_factory: async_sessionmaker[AsyncSession],
    box: SecretBox,
    *,
    pin_id: int,
    safety: RuntimeSafety,
    user_agent: str | None = None,
    choice: str | None = None,
) -> LoginResult | None:
    """Check a pending Plex login once.

    Returns ``None`` while the user has not yet approved (the frontend keeps
    polling), a :class:`LoginResult` once they have and the checks pass, and
    raises :class:`LoginError` on any refusal. A refused attempt consumes its
    pending row, so a rejected token cannot be replayed.

    First-run setup only: an owner of several servers raises
    :class:`PlexServerChoiceNeededError`, and a server that is briefly unreachable raises
    :class:`PlexLinkRetryableError` -- neither is a refusal, so the pending row survives
    in both cases and the frontend re-polls the same PIN (carrying the picked server, or
    simply waiting for the server to come back).
    """
    async with session_factory() as session:
        pending = await session.scalar(
            select(PendingPlexLogin).where(
                PendingPlexLogin.pin_id == pin_id, PendingPlexLogin.purpose == "login"
            )
        )
        if pending is None:
            raise LoginError("error.auth.login_request_invalid")
        expired = pending.expires_at <= utcnow()
        cid = await client_identifier(session)
        if expired:
            await session.delete(pending)
        await session.commit()

    if expired:
        raise LoginError("error.auth.login_request_timed_out")

    async with PlexTvClient(cid, safety=safety) as plextv:
        try:
            token = await plextv.check_pin(pin_id)
        except IntegrationError as exc:
            if exc.status == 429:
                # We polled plex.tv too eagerly. Not a failure -- tell the browser
                # to try again shortly.
                return None
            raise LoginError("error.auth.login_check_failed") from exc

        if not token:
            return None  # not approved yet

        try:
            account = await plextv.account(token)
            owned = await plextv.owned_servers(token)
        except IntegrationError as exc:
            raise LoginError("error.auth.login_account_unreadable") from exc

    async with session_factory() as session:
        server = (await session.execute(select(PlexServer).limit(1))).scalar_one_or_none()

    if server is not None:
        # LOGIN: authorize against the machine id we already trust.
        if not any(r.client_identifier == server.machine_identifier for r in owned):
            await _consume_pending(session_factory, pin_id)
            log.warning("login.plex_not_owner", account=account.account_id)
            raise LoginError("error.auth.plex_not_owner")
    else:
        # SETUP: the first owner claims the server. complete_link refuses a non-owner,
        # asks for a choice on a multi-server account, and persists the link.
        try:
            await complete_link(
                session_factory, box, token=token, account=account, owned=owned, choice=choice
            )
        except PlexServerChoiceNeededError:
            # Not a refusal: the sign-in succeeded and the owner just has to pick a
            # server. Leave the pending row intact so the same PIN can finish the job
            # once the frontend re-polls with the choice.
            raise
        except PlexLinkRetryableError:
            # Also not a refusal: the sign-in succeeded, and the server simply did not
            # answer this instant -- it may be restarting. Leave the pending row intact
            # and let it bubble, so the browser keeps polling the still-valid sign-in
            # instead of being sent back through the whole approval round trip. Must sit
            # ABOVE the PlexLinkError arm below, which is its parent class and would
            # otherwise consume the pending row (B2-14, in the setup twin of the link
            # poll).
            raise
        except PlexLinkError as exc:
            await _consume_pending(session_factory, pin_id)
            # Carries the link failure's own code and params forward rather than flattening
            # it to a string: this arm is one more raiser of whichever plex.* condition
            # `complete_link` hit, not a distinct login-time refusal (rule 144).
            raise LoginError(exc.code, status=exc.status, **exc.params) from exc

    async with session_factory() as session:
        user = await _upsert_plex_user(session, account)
        if not user.is_active:
            await _delete_pending(session, pin_id)
            await session.commit()
            raise LoginError("error.auth.account_deactivated")

        await _delete_pending(session, pin_id)
        token_str = await open_session(session, user, user_agent=user_agent)
        result = LoginResult(session_token=token_str, user=_view(user), setup=server is None)
        await session.commit()

    log.info("login.plex", user=result.user.username, setup=result.setup)
    return result


async def _upsert_plex_user(session: AsyncSession, account: PlexAccount) -> AppUser:
    """Find this Plex account's admin row, or create it. Refreshes the profile."""
    user = await session.scalar(
        select(AppUser).where(AppUser.plex_account_id == account.account_id)
    )
    if user is not None:
        user.email = account.email or user.email
        user.thumb_url = account.thumb or user.thumb_url
        return user

    user = AppUser(
        provider=AuthProvider.PLEX,
        plex_account_id=account.account_id,
        username=await unique_username(session, account.username, "plex-user"),
        email=account.email,
        thumb_url=account.thumb,
        is_active=True,
        created_at=utcnow(),
    )
    session.add(user)
    await session.flush()
    return user


async def _delete_pending(session: AsyncSession, pin_id: int) -> None:
    await session.execute(delete(PendingPlexLogin).where(PendingPlexLogin.pin_id == pin_id))


async def _consume_pending(session_factory: async_sessionmaker[AsyncSession], pin_id: int) -> None:
    async with session_factory() as session:
        await _delete_pending(session, pin_id)
        await session.commit()


# ---------------------------------------------------------------------------
# Local
# ---------------------------------------------------------------------------


async def login_local(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    username: str,
    password: str,
    user_agent: str | None = None,
) -> LoginResult:
    """Verify a local username/password and mint a session.

    Every failure -- unknown user, Plex-only account, deactivated, wrong password
    -- returns the same message and takes the same time, so nothing here tells an
    attacker which usernames exist.
    """
    async with session_factory() as session:
        user = await session.scalar(select(AppUser).where(AppUser.username == username))

        usable = (
            user is not None
            and user.is_active
            and user.provider == AuthProvider.LOCAL
            and user.password_hash is not None
        )
        # Always run a verify, against the real hash if we have one and a decoy
        # otherwise, so timing does not distinguish the branches.
        stored_hash = user.password_hash if usable and user is not None else _DUMMY_HASH
        ok, new_hash = verify_password(password, stored_hash or _DUMMY_HASH)

        if not usable or not ok or user is None:
            log.info("login.local_rejected", username=username[:64])
            raise LoginError("error.auth.wrong_credentials")

        if new_hash is not None:
            # Argon2 parameters were raised since this hash was written; rewrite it.
            user.password_hash = new_hash

        token = await open_session(session, user, user_agent=user_agent)
        result = LoginResult(session_token=token, user=_view(user), setup=False)
        await session.commit()

    log.info("login.local", user=result.user.username)
    return result
