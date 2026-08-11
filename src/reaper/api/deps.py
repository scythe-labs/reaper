# SPDX-License-Identifier: AGPL-3.0-or-later
"""What every router needs off the request, and the gate that gets asked before deletion is armed.

Two things live here. The gate is why this module is under ``.claude/rules/auth.md``'s globs;
the accessors would not have earned that on their own.

**The admin-password gate.** Four routes ask for the admin password before doing something
consequential: arming deletion, changing that password, forgetting the watch record, and
confirming a restore. All four ran the same four-step ritual by hand, copied line for line,
varying only in the log name and the sentence the operator reads.
:func:`require_admin_password` is that ritual, written once.

**The request accessors.** Seven routers each wrote their own two-line reader for the same three
attributes of
``app.state``, under two spellings -- ``_factory`` in ``api/auth.py``, ``api/backup.py``,
``api/settings.py`` and ``api/setup.py``, ``_sessions`` in ``api/review.py``,
``api/runs.py`` and ``api/whitelist.py`` -- and four more modules imported one of those
copies rather than adding an eighth. ``_latest_snapshot`` was written twice and called
from four modules. They are one declaration each now. :func:`state_singleton` is the same
collapse for the lazily-built per-app objects, which were four copies of one read-build-store.

Routers that read ``request.app.state`` inline are left alone. They copied no function,
so they are outside what this module collapses, and the pull request that landed it
records the deferral (``docs/SIMPLIFICATION_PLAN.md``, wave 3). **Thirty-two such reads
survive across eleven modules, and three of them are in ``api/runs.py``** -- the one route
that deletes -- so a later sweep that walks only the read-only routers has not honored the
deferral. Exactly three cannot adopt these accessors without a signature change, all in
``api/scan.py``'s ``launch_scan(app: FastAPI)``, which holds no ``Request`` at all.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import structlog
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    Scales request cache, and the artwork client's lock. One per app rather than one in
    the lifespan, so a test app that never ran a lifespan still gets its own, bound to
    its own running loop.

    **There is no await between the read and the write**, so two concurrent requests
    cannot both install one and hand out different objects. That was written down at one
    of the four call sites and silently relied on at the other three; keeping it true is
    now this function's job rather than each caller's.
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
    # The peer address, with one deliberate carve-out: when the operator turned on
    # reverse-proxy trust (Settings -> General) and the peer IS a listed proxy,
    # X-Forwarded-For is honored -- see auth.proxy.client_ip for the walk. From any
    # other peer that header is attacker-controlled and ignored, because trusting it
    # would let a single host dodge the per-IP lockout by rotating a spoofed value.
    # The per-account lock still runs alongside either way.
    return proxy.client_ip(request)


def refuse_if_waiting(retry: float) -> None:
    """Turn a positive wait into the one 429 every limit answers with."""
    if retry > 0.0:
        seconds = max(1, math.ceil(retry))
        raise HTTPException(
            429,
            "Too many attempts. Please wait and try again.",
            headers={"Retry-After": str(seconds)},
        )


def throttled(throttle: Throttle, *keys: str) -> None:
    """Raise 429 if any of ``keys`` is currently locked out, else return.

    Checked *before* any password work happens, so a locked-out attacker never
    reaches the expensive Argon2 verify -- that is what the throttle is for.
    """
    refuse_if_waiting(max((throttle.retry_after(k) for k in keys), default=0.0))


def _record_password_failure(throttle: Throttle, keys: Sequence[str], *, gate: str) -> None:
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


# ---------------------------------------------------------------------------
# The admin-password gate
# ---------------------------------------------------------------------------


def busy_hashing() -> HTTPException:
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
        raise busy_hashing() from exc


async def require_admin_password(
    session: AsyncSession,
    password: str,
    *,
    keys: tuple[str, ...],
    gate: str,
    refusal: str,
) -> None:
    """Ask for the admin password behind the lockout, or refuse. The four gates' ritual, once.

    **Returns nothing, and that is the safety property.** A ``-> bool`` a caller can forget to
    read is a gate a caller can forget to close, and this one arms deletion. Every refusal
    leaves as an exception, so a call site that ignores the result still cannot continue on a
    wrong password.

    ``keys`` is passed, never derived. All four gates share the per-IP key and each carries its
    own ``account:`` key. An ``account:`` lockout refuses from every source address, so merging
    them would let five wrong restore passwords from anywhere lock the operator out of arming
    deletion from their own machine. The shared ``ip:`` half is deliberate: all four verify the
    same secret, so one address guessing it at any gate is one guesser at one password.

    A full Argon2 gate raises 503 out of :func:`_verify_admin_password` before the failure is
    recorded, so a capacity refusal never reaches the lockout counters (rule 11/98). That is
    structural rather than a branch here: the exception leaves before the ``if`` below runs.

    ``refusal`` is the sentence the operator reads, and it names what was kept rather than what
    went wrong (rule 21). ``gate`` names the interlock in the log and is never shown.
    """
    throttled(password_throttle, *keys)
    if not await _verify_admin_password(session, password):
        _record_password_failure(password_throttle, keys, gate=gate)
        raise HTTPException(403, refusal)
    for key in keys:
        password_throttle.record_success(key)
