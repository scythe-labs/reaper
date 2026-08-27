# SPDX-License-Identifier: AGPL-3.0-or-later
"""The authentication endpoints. This is the only part of the API reachable logged-out.

The frontend drives three flows against this router:

* **Plex sign-in.** ``POST /plex/start`` returns a URL to open on plex.tv. The page
  opens it, then polls ``POST /plex/poll`` until the user approves. The browser
  never touches a Plex token. The backend polls plex.tv and does the ownership
  check. On success a session cookie is set.
* **Local sign-in.** ``POST /local`` with a username and password.
* **Recovery.** ``POST /recover`` redeems the single-use link Reaper prints to its
  log when booted with ``REAPER_RECOVERY=true``. This is the last way in when both
  Plex and the local password have failed.

``GET /me`` and ``GET /context`` let the SPA decide what to render before anyone
has logged in. Everything here is exempt from the session requirement (see
``reaper.api.middleware``), but not from CSRF. Forging a login is an attack too.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.api import tags as api_tags
from reaper.api.deps import (
    client_ip,
    refuse_if_waiting,
    runtime_settings,
    secret_box,
    session_factory,
    throttled,
)
from reaper.api.errors import refuse, refuse_from
from reaper.api.schemas import NO_PLEX_FORWARD, OkOut, PlexServerChoiceOut, PlexStartIn
from reaper.auth.admins import count_local_admins
from reaper.auth.cookie import (
    clear_session_cookie,
    is_secure_request,
    read_session_tokens,
    set_session_cookie,
)
from reaper.auth.ratelimit import (
    RateLimiter,
    argon2_gate,
    login_throttle,
    plex_poll_limit,
    plex_start_limit,
    recover_throttle,
)
from reaper.auth.recovery import clear_recovery_file, redeem_recovery_token
from reaper.auth.sessions import (
    close_session,
    open_session,
    resolve_session_from_cookies,
    session_via_recovery,
)
from reaper.config import RuntimeSafety
from reaper.db.models import AppUser, AuthProvider, PlexServer
from reaper.engine.explanation import ReasonKey
from reaper.engine.reason import Reason, to_wire
from reaper.services.login import (
    LoginError,
    UserView,
    login_local,
    poll_plex_login,
)
from reaper.services.plex_link import (
    PlexLinkRetryableError,
    PlexServerChoiceNeededError,
    start_pin,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=[api_tags.SIGN_IN])


def _safety(request: Request) -> RuntimeSafety:
    # The Plex client here only signs in and reads. It never deletes, so it is built
    # read-only regardless of whether deletion is enabled elsewhere.
    return RuntimeSafety(destructive_enabled=False)


def _rate_limited(limiter: RateLimiter, key: str) -> None:
    """Count this call against ``key``'s window and raise 429 once it is over the cap.

    The Plex sign-in pair has no password to get wrong, so
    :func:`reaper.api.deps.throttled` never fires on it, since a flood there is made
    of calls that all succeed. This bound applies instead. It counts every call, not
    just refused ones.
    """
    refuse_if_waiting(limiter.retry_after(key))


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class AuthContext(BaseModel):
    """What the login screen needs to know before anyone signs in."""

    setup_needed: bool  # No admin exists yet. The first Plex owner claims the server.
    plex_linked: bool  # A server is linked, so Plex sign-in is a login, not setup.
    local_login_available: bool  # At least one local admin exists to accept a password.


class UserOut(BaseModel):
    id: int
    username: str
    provider: str
    thumb_url: str | None = None
    #: This session was opened with a recovery code, so Settings -> Security lets it
    #: set a new admin password without the current one. False on every ordinary
    #: sign-in. That is the only safe default, since a caller that cannot tell must
    #: ask for the old password.
    via_recovery: bool = False

    @classmethod
    def of(cls, view: UserView) -> UserOut:
        return cls(
            id=view.id,
            username=view.username,
            provider=view.provider,
            thumb_url=view.thumb_url,
        )


class PlexStartOut(BaseModel):
    pin_id: int
    auth_url: str


class PlexPollIn(BaseModel):
    pin_id: int
    # First-run setup, multi-server accounts only. The machine identifier of the
    # owned server the user picked, echoed back from a "choose_server" response.
    machine_identifier: str | None = None


class PlexPollOut(BaseModel):
    status: str  # "pending" | "retrying" | "ok" | "choose_server"
    user: UserOut | None = None
    setup: bool = False
    # Present only with status "choose_server". Lists the owned servers to pick from.
    servers: list[PlexServerChoiceOut] | None = None
    # Present only with status "retrying". Says why this poll could not finish yet,
    # as the typed id plus raw params, the same shape every other reason field on
    # the wire takes. The frontend composes the sentence from these. The sign-in is
    # still good, and the browser keeps polling.
    reason: ReasonKey | None = None


class LocalLoginIn(BaseModel):
    # Bounded, like every field that reaches Argon2 or a lockout key. Hashing
    # unbounded input is a CPU-exhaustion vector, and a megabyte "username" should
    # be a 422, not a lockout-table entry.
    username: str = Field(max_length=128)
    password: str = Field(max_length=128)


class RecoverIn(BaseModel):
    token: str = Field(max_length=256)


# ---------------------------------------------------------------------------
# Context / identity
# ---------------------------------------------------------------------------


@router.get("/context")
async def context(request: Request) -> AuthContext:
    """Unauthenticated. Describes the shape of the login screen.

    This is deliberately low-detail. It never reveals who the admins are, only
    whether setup is still pending and which sign-in methods can succeed.
    """
    async with session_factory(request)() as session:
        user_count = int(
            (await session.execute(select(func.count()).select_from(AppUser))).scalar_one()
        )
        plex_linked = (
            await session.execute(select(PlexServer.id).limit(1))
        ).scalar_one_or_none() is not None
        locals_ = await count_local_admins(session)

    return AuthContext(
        setup_needed=user_count == 0,
        plex_linked=plex_linked,
        local_login_available=locals_ > 0,
    )


@router.get("/me")
async def me(request: Request) -> UserOut:
    """The signed-in admin, or 401. The SPA calls this to decide login vs app."""
    async with session_factory(request)() as session:
        user, token = await resolve_session_from_cookies(session, request.cookies)
        # Reads the mark before the commit. This is the same answer the password
        # route will act on, and the Security panel grays out its current-password
        # box from it.
        via_recovery = await session_via_recovery(session, token)
        await session.commit()
        if user is None:
            refuse(401, "error.auth.not_authenticated")
        return UserOut(
            id=user.id,
            username=user.username,
            provider=str(user.provider),
            thumb_url=user.thumb_url,
            via_recovery=via_recovery,
        )


# ---------------------------------------------------------------------------
# Plex
# ---------------------------------------------------------------------------


@router.post("/plex/start")
async def plex_start(request: Request, payload: PlexStartIn = NO_PLEX_FORWARD) -> PlexStartOut:
    """Begin a Plex sign-in. Mint a PIN and hand back the URL to approve it on.

    Rate-limited per address before any work happens. Every call writes a pending
    row and asks plex.tv for a PIN, so an unthrottled flood both grows the table and
    pushes the install's egress address into plex.tv's own rate limiting. That would
    lock the real operator out of Plex sign-in entirely.
    """
    _rate_limited(plex_start_limit, client_ip(request))
    start = await start_pin(
        session_factory(request),
        purpose="login",
        safety=_safety(request),
        forward_url=payload.forward_url(),
    )
    return PlexStartOut(pin_id=start.pin_id, auth_url=start.auth_url)


@router.post("/plex/poll")
async def plex_poll(request: Request, payload: PlexPollIn, response: Response) -> PlexPollOut:
    # Far looser than /plex/start. One real sign-in polls every two seconds for up
    # to five minutes, so the cap has to clear about 150 calls without touching an
    # honest browser.
    _rate_limited(plex_poll_limit, client_ip(request))
    try:
        result = await poll_plex_login(
            session_factory(request),
            secret_box(request),
            pin_id=payload.pin_id,
            safety=_safety(request),
            user_agent=request.headers.get("user-agent"),
            choice=payload.machine_identifier,
        )
    except PlexServerChoiceNeededError as exc:
        # First-run setup, account owns several servers. The sign-in itself
        # succeeded. The PIN stays valid, and the browser re-polls with the owner's
        # pick.
        return PlexPollOut(
            status="choose_server",
            servers=[
                PlexServerChoiceOut(name=c.name, machine_identifier=c.machine_identifier)
                for c in exc.candidates
            ],
        )
    except PlexLinkRetryableError as exc:
        # First-run setup. The sign-in was approved but the server did not answer
        # this instant. ``poll_plex_login`` keeps the pending row for exactly this
        # case, so answering with an error would strand a sign-in that is still
        # good. The browser aborts its poll loop on any thrown status, so this
        # returns a non-final status instead, and the loop keeps polling until the
        # server is back or the deadline passes.
        return PlexPollOut(
            status="retrying",
            reason=ReasonKey.model_validate(to_wire(Reason(exc.code, dict(exc.params)))),
        )
    except LoginError as exc:
        refuse_from(exc)

    if result is None:
        return PlexPollOut(status="pending")

    set_session_cookie(response, result.session_token, secure=is_secure_request(request))
    return PlexPollOut(status="ok", user=UserOut.of(result.user), setup=result.setup)


# ---------------------------------------------------------------------------
# Local
# ---------------------------------------------------------------------------


@router.post("/local")
async def local(request: Request, payload: LocalLoginIn, response: Response) -> UserOut:
    ip = client_ip(request)
    # Key the lockout on both the source IP and the attempted username, so neither
    # a single host hammering many names nor many hosts hammering one name slips
    # through. Truncated to bound the key length against a hostile username.
    user_key = f"user:{payload.username[:64]}"
    ip_key = f"ip:{ip}"
    throttled(login_throttle, ip_key, user_key)

    # Sheds load before hashing. If too many Argon2 verifications are already in
    # flight, this refuses quickly instead of adding to the CPU pile-up. This is a
    # capacity limit, not a credential failure, so it does not count against the
    # lockout counters.
    if not argon2_gate.acquire():
        refuse(503, "error.auth.sign_in_busy", headers={"Retry-After": "2"})
    try:
        result = await login_local(
            session_factory(request),
            username=payload.username,
            password=payload.password,
            user_agent=request.headers.get("user-agent"),
        )
    except LoginError as exc:
        # A real failed credential. Count it against both keys. If either just
        # crossed into a lockout, log at warning level so the operator sees a
        # brute-force attempt, instead of only quiet info-level rejections.
        locked = max(
            login_throttle.record_failure(ip_key),
            login_throttle.record_failure(user_key),
        )
        if locked > 0.0:
            log.warning(
                "auth.local_locked_out", ip=ip, username=payload.username[:64], retry_after=locked
            )
        refuse_from(exc)
    finally:
        argon2_gate.release()

    login_throttle.record_success(ip_key)
    login_throttle.record_success(user_key)
    set_session_cookie(response, result.session_token, secure=is_secure_request(request))
    return UserOut.of(result.user)


# ---------------------------------------------------------------------------
# Logout / recovery
# ---------------------------------------------------------------------------


@router.post("/logout")
async def logout(request: Request, response: Response) -> OkOut:
    # Revokes every session the jar can present, not just the first name carrying a
    # cookie. With two cookie names in play, revoking only one would let a stale
    # cookie absorb the logout while a genuinely live session under the other name
    # stayed valid in the database.
    async with session_factory(request)() as session:
        for token in read_session_tokens(request.cookies):
            await close_session(session, token)
        await session.commit()
    clear_session_cookie(response)
    return OkOut(ok=True)


@router.post("/recover")
async def recover(request: Request, payload: RecoverIn, response: Response) -> UserOut:
    """Redeem a recovery link and sign in as an admin.

    The token is single-use and at most 15 minutes old. Obtaining it required host
    access, either the console after setting an env var, or the 0600 file in the
    data folder. This logs in as an existing admin, a local one by preference, since
    recovery exists precisely for when Plex is unreachable.

    The session it opens is marked ``via_recovery``, which is what makes the landing
    page's promise true. Settings -> Security accepts a new admin password from this
    session without the current one. Without the mark, the operator would land on a
    form asking for the password they came here because they had forgotten.
    """
    # No Argon2 here. Recovery redeems a random single-use token, so brute force is
    # already near-hopeless. A per-IP cap still stops a token-guessing flood from
    # tying up the endpoint.
    ip_key = f"ip:{client_ip(request)}"
    throttled(recover_throttle, ip_key)

    async with session_factory(request)() as session:
        if not await redeem_recovery_token(session, payload.token):
            await session.commit()
            recover_throttle.record_failure(ip_key)
            refuse(401, "error.auth.recovery_link_invalid")

        target = await _recovery_target(session)
        if target is None:
            # Gives the code back. ``redeem_recovery_token`` already stamped
            # ``used_at``, and committing that here would burn the operator's one
            # 15-minute code on a failure that has nothing to do with the code,
            # forcing another REAPER_RECOVERY reboot to mint a fresh one at the
            # exact moment recovery is most needed. Rolling back leaves the token
            # unused, so it still works once an admin exists.
            await session.rollback()
            # Names a route that exists on the install reading this. ``reaper-admin``
            # is not in the Windows or macOS bundle at all. It ships as one
            # PyInstaller executable, built from ``packaging/pyinstaller/entry.py``,
            # which runs only the launcher, so recommending that command alone
            # leaves those operators with nothing they can run. Plex sign-in claims
            # an unclaimed server everywhere, so it works on every install.
            refuse(409, "error.auth.recovery_no_admin")

        token_str = await open_session(
            session, target, user_agent=request.headers.get("user-agent"), via_recovery=True
        )
        await session.commit()
        # This logs only past the commit, once the redemption is durable and the
        # admin session is open, so the log records a real outcome. Logging any
        # earlier could assert a sign-in that a later rollback undoes, while the
        # code was in fact still live.
        log.warning("recovery.redeemed", detail="A recovery link was used to gain admin access.")

    # This runs only now, past the commit. The code is spent, so the copy in the
    # data folder is a secret with no remaining use. Deleting it earlier would take
    # the operator's only written copy on a path that can still roll the redemption
    # back. The no-admin 409 above does exactly that, and leaves the file where it
    # was.
    clear_recovery_file(runtime_settings(request).data_dir)

    recover_throttle.record_success(ip_key)
    log.warning("auth.recovery_login", user=target.username)
    set_session_cookie(response, token_str, secure=is_secure_request(request))
    return UserOut(
        id=target.id,
        username=target.username,
        provider=str(target.provider),
        thumb_url=target.thumb_url,
        via_recovery=True,
    )


async def _recovery_target(session: AsyncSession) -> AppUser | None:
    """Return the admin a recovery link logs in as. This is the first active local
    admin, or any active admin if none is local."""
    local_admin = await session.scalar(
        select(AppUser)
        .where(
            AppUser.provider == AuthProvider.LOCAL,
            AppUser.is_active.is_(True),
            AppUser.password_hash.is_not(None),
        )
        .order_by(AppUser.id)
        .limit(1)
    )
    if local_admin is not None:
        return local_admin
    fallback: AppUser | None = await session.scalar(
        select(AppUser).where(AppUser.is_active.is_(True)).order_by(AppUser.id).limit(1)
    )
    return fallback
