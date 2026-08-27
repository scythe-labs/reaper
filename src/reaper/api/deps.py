# SPDX-License-Identifier: AGPL-3.0-or-later
"""Give every router what it needs off the request, and gate what happens before deletion is armed.

Two things live here. The gate is why this module falls under ``.claude/rules/auth.md``.

**The admin-password gate.** Four routes ask for the admin password before doing
something consequential: arming deletion, changing that password, forgetting the watch
record, and confirming a restore. :func:`require_admin_password` runs that check once,
for all four.

**The request accessors.** These read three attributes off ``app.state``: the session
factory, the settings, and the secret box. :func:`state_singleton` does the same job
for objects that build lazily on first use, one per app.

Some routers still read ``request.app.state`` directly instead of using these
accessors. That is left alone by choice. ``api/scan.py``'s ``launch_scan(app: FastAPI)``
cannot adopt them without a signature change, since it holds an ``app``, not a
``Request``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import structlog
from fastapi import FastAPI, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.api.errors import RefusalHTTPException, refuse
from reaper.auth import proxy
from reaper.auth.ratelimit import Throttle, password_throttle
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.models import Snapshot
from reaper.services import admin_password

log = structlog.get_logger(__name__)


def session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def runtime_settings(request: Request) -> Settings:
    """The process settings. Named for what it returns rather than ``settings``, which
    every router already uses for a profile or a wire model."""
    settings: Settings = request.app.state.settings
    return settings


def secret_box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.secret_box
    return box


def state_singleton[T](app: FastAPI, name: str, build: Callable[[], T]) -> T:
    """The one ``app.state.<name>`` for this app, built on first ask.

    Four callers keep a per-app object this way: the scan status, the reap status, the
    Scales request cache, and the artwork client's lock. It builds here rather than in
    the lifespan, so a test app that never ran one still gets its own, bound to its own
    running loop.

    **There is no await between the read and the write**, so two concurrent requests
    cannot both install one and hand out different objects.
    """
    existing: T | None = getattr(app.state, name, None)
    if existing is None:
        existing = build()
        setattr(app.state, name, existing)
    return existing


async def newest_snapshot(session: AsyncSession) -> Snapshot | None:
    return (
        await session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1))
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------


def client_ip(request: Request) -> str:
    # Returns the peer address, with one exception. If the operator turned on
    # reverse-proxy trust (Settings, General) and the peer is a listed proxy, this reads
    # X-Forwarded-For instead (see auth.proxy.client_ip). From any other peer, that
    # header is attacker-controlled, so it is ignored. Trusting it there would let a
    # single host dodge the per-IP lockout by rotating a spoofed value. The per-account
    # lock still applies either way.
    return proxy.client_ip(request)


def refuse_if_waiting(retry: float) -> None:
    """Turn a positive wait into the one 429 every limit answers with."""
    if retry > 0.0:
        seconds = max(1, math.ceil(retry))
        refuse(429, "error.auth.too_many_attempts", headers={"Retry-After": str(seconds)})


def throttled(throttle: Throttle, *keys: str) -> None:
    """Raise 429 if any of ``keys`` is currently locked out, otherwise return.

    Callers check this before doing any password work, so a locked-out attacker never
    reaches the expensive Argon2 verify. That is what the throttle is for.
    """
    refuse_if_waiting(max((throttle.retry_after(k) for k in keys), default=0.0))


def _record_password_failure(throttle: Throttle, keys: Sequence[str], *, gate: str) -> None:
    """Count a wrong password against every key, and log which gate it was.

    This covers all four gates behind the admin password: arming deletion, changing
    that password, forgetting the watch record, and confirming a restore. It logs the
    same way the local login already warns on its own lockout (``auth.local_locked_out``).

    Never log the attempted password. The log carries only the throttle key and the
    gate name. A wrong password has no use to anyone reading this and must not be
    stored anywhere.
    """
    locked_for = 0.0
    for key in keys:
        locked_for = max(locked_for, throttle.record_failure(key))
    if locked_for > 0:
        log.warning("auth.password_locked_out", gate=gate, retry_after=math.ceil(locked_for))
    else:
        log.debug("auth.password_rejected", gate=gate)


# ---------------------------------------------------------------------------
# The admin-password gate
# ---------------------------------------------------------------------------


def busy_hashing() -> RefusalHTTPException:
    """The one 503 every password-hashing route sheds load with."""
    return RefusalHTTPException(
        503,
        "error.auth.password_hashing_busy",
        headers={"Retry-After": "2"},
    )


async def _verify_admin_password(session: AsyncSession, password: str) -> bool:
    """Check the admin password, and turn a full Argon2 concurrency gate into a 503
    instead of a "wrong password".

    A capacity refusal must never reach the lockout counters, or a server under load
    would lock out the operator who typed the right password. ``admin_password.verify``
    takes the gate itself, since it is the only place that knows how many hashes the
    call will run.
    """
    try:
        return await admin_password.verify(session, password)
    except admin_password.PasswordVerificationBusyError as exc:
        raise busy_hashing() from exc


async def require_admin_password(
    session: AsyncSession,
    password: str,
    *,
    keys: tuple[str, ...],
    gate: str,
    code: str,
) -> None:
    """Ask for the admin password behind the lockout, or refuse. All four gates share
    this one check.

    **Returns nothing, and that is the safety property.** A ``-> bool`` a caller can
    forget to read is a gate a caller can forget to close, and this one arms deletion.
    Every refusal leaves as an exception, so a call site that ignores the return value
    still cannot proceed on a wrong password.

    Callers pass ``keys`` rather than have this function derive them. All four gates
    share one per-IP key and each carries its own ``account:`` key. Merging the
    ``account:`` keys would let five wrong restore-password guesses from anywhere lock
    the operator out of arming deletion from their own machine. Sharing the ``ip:`` key
    is deliberate: all four gates verify the same secret, so one address guessing it at
    any gate is one guesser at one password.

    A full Argon2 gate raises 503 out of :func:`_verify_admin_password` before the
    failure is recorded, so a capacity refusal never reaches the lockout counters. That
    guarantee is structural: the exception leaves before the ``if`` below ever runs.

    ``code`` names the catalog sentence the operator reads, which says what was kept
    rather than what went wrong. ``gate`` names the interlock in the log only and is
    never shown to the operator.
    """
    throttled(password_throttle, *keys)
    if not await _verify_admin_password(session, password):
        _record_password_failure(password_throttle, keys, gate=gate)
        refuse(403, code)
    for key in keys:
        password_throttle.record_success(key)
