# SPDX-License-Identifier: AGPL-3.0-or-later
"""The authentication endpoints -- the only part of the API reachable logged-out.

The frontend drives three flows against this router:

* **Plex sign-in.** ``POST /plex/start`` returns a URL to open on plex.tv; the
  page opens it and then polls ``POST /plex/poll`` until the user approves. The
  browser never touches a Plex token -- the backend polls plex.tv and does the
  ownership check. On success a session cookie is set.
* **Local sign-in.** ``POST /local`` with a username and password.
* **Recovery.** ``POST /recover`` redeems the single-use link Reaper prints to its
  log when booted with ``REAPER_RECOVERY=true`` -- the last way in when both Plex
  and the local password have failed you.

``GET /me`` and ``GET /context`` let the SPA decide what to render before anyone
has logged in. Everything here is exempt from the session requirement (see
``reaper.api.middleware``) but *not* from CSRF: forging a login is an attack too.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.api import tags as api_tags
from reaper.api.schemas import NO_PLEX_FORWARD, OkOut, PlexServerChoiceOut, PlexStartIn
from reaper.auth.admins import count_local_admins
from reaper.auth.cookie import (
    clear_session_cookie,
    is_secure_request,
    read_session_tokens,
    set_session_cookie,
)
from reaper.auth.proxy import client_ip
from reaper.auth.ratelimit import (
    RateLimiter,
    Throttle,
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
from reaper.config import RuntimeSafety, Settings
from reaper.crypto import SecretBox
from reaper.db.models import AppUser, AuthProvider, PlexServer
from reaper.services import admin_password
from reaper.services.login import (
    LoginError,
    UserView,
    login_local,
    poll_plex_login,
    start_plex_login,
)
from reaper.services.plex_link import PlexLinkRetryableError, PlexServerChoiceNeededError

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=[api_tags.SIGN_IN])


def _factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _safety(request: Request) -> RuntimeSafety:
    # The Plex client here only signs in and reads -- it never deletes -- so it is built
    # read-only regardless of whether deletion is enabled elsewhere.
    return RuntimeSafety(destructive_enabled=False)


def _box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.secret_box
    return box


def _client_ip(request: Request) -> str:
    # The peer address, with one deliberate carve-out: when the operator turned on
    # reverse-proxy trust (Settings -> General) and the peer IS a listed proxy,
    # X-Forwarded-For is honored -- see auth.proxy.client_ip for the walk. From any
    # other peer that header is attacker-controlled and ignored, because trusting it
    # would let a single host dodge the per-IP lockout by rotating a spoofed value.
    # The per-username lock (below) still runs alongside either way.
    return client_ip(request)


def _throttled(throttle: Throttle, *keys: str) -> None:
    """Raise 429 if any of ``keys`` is currently locked out, else return.

    Checked *before* any password work happens, so a locked-out attacker never
    reaches the expensive Argon2 verify -- that is what the throttle is for.
    """
    retry = max((throttle.retry_after(k) for k in keys), default=0.0)
    _refuse_if_waiting(retry)


def _rate_limited(limiter: RateLimiter, key: str) -> None:
    """Count this call against ``key``'s window and raise 429 once it is over the cap.

    The Plex sign-in pair has no password to get wrong, so :func:`_throttled` above never
    fires on it -- a flood there is made of calls that all *succeed*. This is the bound
    that does apply: it counts every call, not just refused ones (S-1).
    """
    _refuse_if_waiting(limiter.retry_after(key))


def _refuse_if_waiting(retry: float) -> None:
    """Turn a positive wait into the one 429 both limits answer with."""
    if retry > 0.0:
        seconds = max(1, math.ceil(retry))
        raise HTTPException(
            429,
            "Too many attempts. Please wait and try again.",
            headers={"Retry-After": str(seconds)},
        )


def record_password_failure(throttle: Throttle, keys: Sequence[str], *, gate: str) -> None:
    """Count a wrong password against every key, and say which interlock it was.

    Four routes are gated on the admin password -- arming deletion, changing that
    password, forgetting the watch record, and confirming a restore -- and each recorded
    the failure silently. So a hundred attempts to arm deletion from a borrowed session
    left no trace whatever, while the one that eventually succeeded logged
    ``safety.destructive_set``. The local login has warned on its lockout crossing all
    along (``auth.local_locked_out``); this is that same line for its four siblings
    (rule 72).

    The throttle KEY and the gate, never ``payload.password``: an attempted password is
    of no use to anyone reading this and is the one thing here that must never be
    written down (rule 13).
    """
    locked_for = 0.0
    for key in keys:
        locked_for = max(locked_for, throttle.record_failure(key))
    if locked_for > 0:
        log.warning("auth.password_locked_out", gate=gate, retry_after=math.ceil(locked_for))
    else:
        log.debug("auth.password_rejected", gate=gate)


def _busy_hashing() -> HTTPException:
    """The one 503 every password-hashing route sheds load with."""
    return HTTPException(
        503,
        "The server is busy checking passwords. Please try again shortly.",
        headers={"Retry-After": "2"},
    )


async def _verify_admin_password(session: AsyncSession, password: str) -> bool:
    """Check the admin password, turning a full Argon2 gate into a 503 rather than a
    "wrong password".

    The distinction matters: a capacity refusal must never reach the lockout counters, or
    a server under load would lock out the operator who typed the right password. The gate
    itself is taken inside ``admin_password.verify``, which is the only place that knows
    how many hashes the call will run (S-4).
    """
    try:
        return await admin_password.verify(session, password)
    except admin_password.PasswordVerificationBusyError as exc:
        raise _busy_hashing() from exc


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class AuthContext(BaseModel):
    """What the login screen needs to know before anyone signs in."""

    setup_needed: bool  # no admin exists yet -- the first Plex owner claims the server
    plex_linked: bool  # a server is linked, so Plex sign-in is a login rather than setup
    local_login_available: bool  # at least one local admin exists to accept a password


class UserOut(BaseModel):
    id: int
    username: str
    provider: str
    thumb_url: str | None = None
    #: This session was opened with a recovery code, so Settings -> Security lets it set a
    #: new admin password without the current one. False on every ordinary sign-in, which
    #: is the only safe default: a caller that cannot tell must ask for the old password.
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
    # First-run setup, multi-server accounts only: the machine identifier of the owned
    # server the user picked, echoed back from a "choose_server" response.
    machine_identifier: str | None = None


class PlexPollOut(BaseModel):
    status: str  # "pending" | "retrying" | "ok" | "choose_server"
    user: UserOut | None = None
    setup: bool = False
    # Present only with status "choose_server": the owned servers to pick from.
    servers: list[PlexServerChoiceOut] | None = None
    # Present only with status "retrying": why this poll could not finish yet, in the
    # operator's words. The sign-in is still good and the browser keeps polling.
    reason: str | None = None


class LocalLoginIn(BaseModel):
    # Bounded, like every field that reaches Argon2 or a lockout key: hashing
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
    """Unauthenticated: the shape of the login screen.

    Deliberately low-detail. It reveals only whether setup is still pending and
    which sign-in methods can succeed -- nothing about *who* the admins are.
    """
    async with _factory(request)() as session:
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
    async with _factory(request)() as session:
        user, token = await resolve_session_from_cookies(session, request.cookies)
        # Read the mark before the commit: this is the same answer the password route will
        # act on, and the Security panel grays out its current-password box from it.
        via_recovery = await session_via_recovery(session, token)
        await session.commit()
        if user is None:
            raise HTTPException(401, "Not authenticated.")
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
    """Begin a Plex sign-in: mint a PIN and hand back the URL to approve it on.

    Rate-limited per address before any work happens. Every call writes a pending row and
    asks plex.tv for a PIN, so an unthrottled flood both grows the table and pushes the
    install's egress address into plex.tv's own rate limiting -- which would lock the real
    operator out of Plex sign-in entirely (S-1).
    """
    _rate_limited(plex_start_limit, _client_ip(request))
    start = await start_plex_login(
        _factory(request), safety=_safety(request), forward_url=payload.forward_url()
    )
    return PlexStartOut(pin_id=start.pin_id, auth_url=start.auth_url)


@router.post("/plex/poll")
async def plex_poll(request: Request, payload: PlexPollIn, response: Response) -> PlexPollOut:
    # Far looser than /plex/start: one real sign-in polls every two seconds for up to five
    # minutes, so the cap has to clear ~150 calls without touching an honest browser.
    _rate_limited(plex_poll_limit, _client_ip(request))
    try:
        result = await poll_plex_login(
            _factory(request),
            _box(request),
            pin_id=payload.pin_id,
            safety=_safety(request),
            user_agent=request.headers.get("user-agent"),
            choice=payload.machine_identifier,
        )
    except PlexServerChoiceNeededError as exc:
        # First-run setup, account owns several servers. The sign-in itself succeeded;
        # the PIN stays valid, and the browser re-polls with the owner's pick.
        return PlexPollOut(
            status="choose_server",
            servers=[
                PlexServerChoiceOut(name=c.name, machine_identifier=c.machine_identifier)
                for c in exc.candidates
            ],
        )
    except PlexLinkRetryableError as exc:
        # First-run setup: the sign-in was approved but the server did not answer this
        # instant. ``poll_plex_login`` keeps the pending row for exactly this case, so
        # answering with an error would strand a sign-in that is still good -- the browser
        # aborts its poll loop on any thrown status (B2-14). A non-final status instead,
        # so the loop keeps polling until the server is back or the deadline passes.
        return PlexPollOut(status="retrying", reason=str(exc))
    except LoginError as exc:
        raise HTTPException(401, str(exc)) from exc

    if result is None:
        return PlexPollOut(status="pending")

    set_session_cookie(response, result.session_token, secure=is_secure_request(request))
    return PlexPollOut(status="ok", user=UserOut.of(result.user), setup=result.setup)


# ---------------------------------------------------------------------------
# Local
# ---------------------------------------------------------------------------


@router.post("/local")
async def local(request: Request, payload: LocalLoginIn, response: Response) -> UserOut:
    ip = _client_ip(request)
    # Key the lockout on both the source IP and the attempted username, so neither
    # a single host hammering many names nor many hosts hammering one name slips
    # through. Truncated to bound the key length against a hostile username.
    user_key = f"user:{payload.username[:64]}"
    ip_key = f"ip:{ip}"
    _throttled(login_throttle, ip_key, user_key)

    # Shed load before hashing: if too many Argon2 verifications are already in
    # flight, refuse quickly rather than adding to the CPU pile-up. This is a
    # capacity limit, not a credential failure, so it does not count against the
    # lockout counters.
    if not argon2_gate.acquire():
        raise HTTPException(
            503,
            "The server is busy verifying sign-ins. Please try again shortly.",
            headers={"Retry-After": "2"},
        )
    try:
        result = await login_local(
            _factory(request),
            username=payload.username,
            password=payload.password,
            user_agent=request.headers.get("user-agent"),
        )
    except LoginError as exc:
        # A real failed credential. Count it against both keys; if either just
        # crossed into a lockout, log at warning level so the operator sees a
        # brute-force attempt rather than only quiet info-level rejections.
        locked = max(
            login_throttle.record_failure(ip_key),
            login_throttle.record_failure(user_key),
        )
        if locked > 0.0:
            log.warning(
                "auth.local_locked_out", ip=ip, username=payload.username[:64], retry_after=locked
            )
        raise HTTPException(401, str(exc)) from exc
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
    # Revoke every session the jar can present, not just the first name carrying a cookie.
    # With two names in play a stale cookie used to absorb the logout -- its row was
    # already gone, so the delete was a no-op -- and the genuinely live session under the
    # other name stayed valid in the database after the operator had asked to sign out.
    async with _factory(request)() as session:
        for token in read_session_tokens(request.cookies):
            await close_session(session, token)
        await session.commit()
    clear_session_cookie(response)
    return OkOut(ok=True)


@router.post("/recover")
async def recover(request: Request, payload: RecoverIn, response: Response) -> UserOut:
    """Redeem a recovery link and sign in as an admin.

    The token is single-use and 15 minutes old at most; obtaining it required host
    access (setting an env var, then reading either the console or the 0600 file in the
    data folder). It logs in as an existing admin -- a local one by preference, since
    recovery exists precisely for when Plex is unreachable.

    The session it opens is marked ``via_recovery``, which is what makes the landing page's
    promise true: Settings -> Security accepts a new admin password from this session
    without the current one. Without the mark the operator arrived at a form asking for the
    password they came here because they had forgotten (#433).
    """
    # No Argon2 here -- recovery redeems a random single-use token, so brute force
    # is already near-hopeless -- but a per-IP cap still stops a token-guessing
    # flood from tying up the endpoint.
    ip_key = f"ip:{_client_ip(request)}"
    _throttled(recover_throttle, ip_key)

    async with _factory(request)() as session:
        if not await redeem_recovery_token(session, payload.token):
            await session.commit()
            recover_throttle.record_failure(ip_key)
            raise HTTPException(401, "That recovery link is invalid, expired, or already used.")

        target = await _recovery_target(session)
        if target is None:
            # Give the code back. ``redeem_recovery_token`` already stamped ``used_at``,
            # and committing that here would burn the operator's ONE 15-minute code on a
            # failure that is nothing to do with the code -- forcing another
            # REAPER_RECOVERY reboot to mint a fresh one, at the exact moment recovery is
            # most needed (B-13). Rolling back leaves the token unused, so it still works
            # once an admin exists.
            await session.rollback()
            # Name a route that exists on the install reading this. ``reaper-admin`` is not
            # in the Windows or macOS bundle at all (one PyInstaller executable, built from
            # ``packaging/pyinstaller/entry.py``, which runs the launcher and nothing else),
            # so offering it alone sent half of all operators after a command they do not
            # have (rule 25, #433). Plex sign-in claims an unclaimed server everywhere.
            raise HTTPException(
                409,
                "The recovery code was valid, but this install has no admin account yet. "
                "Sign in with Plex to claim the server, or on Docker and snap run: "
                "reaper-admin create-admin --username admin",
            )

        token_str = await open_session(
            session, target, user_agent=request.headers.get("user-agent"), via_recovery=True
        )
        await session.commit()
        # Past the commit: the redemption is durable and the admin session is open, so this
        # records an outcome. Logging it at flush time asserted a sign-in the 409 rollback
        # then undid, while the code was in fact still live (rule 26, #467).
        log.warning("recovery.redeemed", detail="A recovery link was used to gain admin access.")

    # Only now, past the commit: the code is spent, so the copy in the data folder is a
    # secret with no remaining use. Deleting it earlier would take the operator's only
    # written copy on a path that can still roll the redemption back (rule 125) -- the
    # no-admin 409 above does exactly that, and leaves the file where it was.
    clear_recovery_file(_settings(request).data_dir)

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
    """The admin a recovery link logs in as: the first active local admin, or any
    active admin if none is local."""
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
