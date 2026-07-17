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

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.auth.admins import count_local_admins
from reaper.auth.cookie import (
    clear_session_cookie,
    is_secure_request,
    read_session_token,
    set_session_cookie,
)
from reaper.auth.ratelimit import Throttle, argon2_gate, login_throttle, recover_throttle
from reaper.auth.recovery import redeem_recovery_token
from reaper.auth.sessions import close_session, open_session, resolve_session
from reaper.config import RuntimeSafety, Settings
from reaper.crypto import SecretBox
from reaper.db.models import AppUser, AuthProvider, PlexServer
from reaper.services.login import (
    LoginError,
    UserView,
    login_local,
    poll_plex_login,
    start_plex_login,
)
from reaper.services.plex_link import PlexServerChoiceNeededError

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/auth")


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
    # The direct peer address. Deliberately *not* X-Forwarded-For: that header is
    # attacker-controlled unless a trusted proxy sets it, and trusting it would let
    # a single host dodge the per-IP lockout by rotating a spoofed value. Behind a
    # reverse proxy every client collapses to the proxy's address, so the per-IP
    # lock is coarser there -- which is why the per-username lock (below) runs
    # alongside it and a successful login always clears both.
    client = request.client
    return client.host if client is not None else "unknown"


def _throttled(throttle: Throttle, *keys: str) -> None:
    """Raise 429 if any of ``keys`` is currently locked out, else return.

    Checked *before* any password work happens, so a locked-out attacker never
    reaches the expensive Argon2 verify -- the whole point of the throttle.
    """
    retry = max((throttle.retry_after(k) for k in keys), default=0.0)
    if retry > 0.0:
        seconds = max(1, math.ceil(retry))
        raise HTTPException(
            429,
            "Too many attempts. Please wait and try again.",
            headers={"Retry-After": str(seconds)},
        )


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
    email: str | None = None
    thumb_url: str | None = None

    @classmethod
    def of(cls, view: UserView) -> UserOut:
        return cls(
            id=view.id,
            username=view.username,
            provider=view.provider,
            email=view.email,
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


class PlexServerChoiceOut(BaseModel):
    name: str
    machine_identifier: str


class PlexPollOut(BaseModel):
    status: str  # "pending" | "ok" | "choose_server"
    user: UserOut | None = None
    setup: bool = False
    # Present only with status "choose_server": the owned servers to pick from.
    servers: list[PlexServerChoiceOut] | None = None


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
    token = read_session_token(request.cookies)
    async with _factory(request)() as session:
        user = await resolve_session(session, token)
        await session.commit()
        if user is None:
            raise HTTPException(401, "Not authenticated.")
        return UserOut(
            id=user.id,
            username=user.username,
            provider=str(user.provider),
            email=user.email,
            thumb_url=user.thumb_url,
        )


# ---------------------------------------------------------------------------
# Plex
# ---------------------------------------------------------------------------


@router.post("/plex/start")
async def plex_start(request: Request) -> PlexStartOut:
    start = await start_plex_login(_factory(request), safety=_safety(request))
    return PlexStartOut(pin_id=start.pin_id, auth_url=start.auth_url)


@router.post("/plex/poll")
async def plex_poll(request: Request, payload: PlexPollIn, response: Response) -> PlexPollOut:
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
async def logout(request: Request, response: Response) -> dict[str, bool]:
    token = read_session_token(request.cookies)
    async with _factory(request)() as session:
        await close_session(session, token)
        await session.commit()
    clear_session_cookie(response, secure=is_secure_request(request))
    return {"ok": True}


@router.post("/recover")
async def recover(request: Request, payload: RecoverIn, response: Response) -> UserOut:
    """Redeem a recovery link and sign in as an admin.

    The token is single-use and 15 minutes old at most; obtaining it required host
    access (setting an env var and reading the log). It logs in as an existing
    admin -- a local one by preference, since recovery exists precisely for when
    Plex is unreachable -- so the operator lands somewhere they can reset a
    password or re-link the server.
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
            await session.commit()
            raise HTTPException(
                409,
                "The recovery link was valid, but there is no admin account to sign in as. "
                "Create one with: reaper-admin create-admin --username <name>",
            )

        token_str = await open_session(
            session, target, user_agent=request.headers.get("user-agent")
        )
        await session.commit()

    recover_throttle.record_success(ip_key)
    log.warning("auth.recovery_login", user=target.username)
    set_session_cookie(response, token_str, secure=is_secure_request(request))
    return UserOut(
        id=target.id,
        username=target.username,
        provider=str(target.provider),
        email=target.email,
        thumb_url=target.thumb_url,
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
