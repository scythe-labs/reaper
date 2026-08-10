# SPDX-License-Identifier: AGPL-3.0-or-later
"""What every router needs off the request, declared once.

Seven routers each wrote their own two-line reader for the same three attributes of
``app.state``, under two spellings -- ``_factory`` in ``api/auth.py``, ``api/backup.py``,
``api/settings.py`` and ``api/setup.py``, ``_sessions`` in ``api/review.py``,
``api/runs.py`` and ``api/whitelist.py`` -- and four more modules imported one of those
copies rather than adding an eighth. ``_latest_snapshot`` was written twice and called
from four modules. They are one declaration each now.

Routers that read ``request.app.state`` inline are left alone. They copied no function,
so they are outside what this module collapses, and the pull request that landed it
records the deferral (``docs/SIMPLIFICATION_PLAN.md``, wave 3).
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.models import Snapshot


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


async def newest_snapshot(session: AsyncSession) -> Snapshot | None:
    return (
        await session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1))
    ).scalar_one_or_none()
