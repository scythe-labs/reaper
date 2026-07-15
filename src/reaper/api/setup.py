# SPDX-License-Identifier: AGPL-3.0-or-later
"""What is left to configure -- the state a first-run wizard reads.

A fresh Reaper is useless until it is pointed at the services it reads from, and the point
of the setup flow is to make that obvious and quick rather than a treasure hunt through a
settings page. This one endpoint answers "what still needs doing?" so the UI can show the
right next step and stop nagging once everything is in place.

Behind the auth gate: the very first admin is claimed on the login screen (the Plex owner
signs in), and everything here happens once someone is signed in.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.db.models import AppUser, Instance, InstanceKind, PlexServer, Snapshot

router = APIRouter(prefix="/api/setup")


def _factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


class SetupStatus(BaseModel):
    admin_exists: bool
    plex_linked: bool
    instances: dict[str, int]
    """How many of each kind are configured -- e.g. {"radarr": 2, "tautulli": 1}."""

    has_radarr: bool
    has_tautulli: bool
    has_seerr: bool
    has_scanned: bool

    scan_ready: bool
    """The minimum to run a scan: at least one Radarr and one Tautulli."""
    complete: bool
    """Ready AND a scan has actually run -- nothing left the wizard needs to push."""


async def _counts(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Instance.kind, func.count())
            .where(Instance.enabled.is_(True))
            .group_by(Instance.kind)
        )
    ).all()
    return {str(kind): int(n) for kind, n in rows}


@router.get("/status")
async def setup_status(request: Request) -> SetupStatus:
    async with _factory(request)() as session:
        admins = int(
            (await session.execute(select(func.count()).select_from(AppUser))).scalar_one()
        )
        counts = await _counts(session)
        plex_linked = (
            await session.execute(select(PlexServer.id).limit(1))
        ).scalar_one_or_none() is not None
        has_scanned = (
            await session.execute(select(Snapshot.id).limit(1))
        ).scalar_one_or_none() is not None

    has_radarr = counts.get(str(InstanceKind.RADARR), 0) > 0
    has_tautulli = counts.get(str(InstanceKind.TAUTULLI), 0) > 0
    scan_ready = has_radarr and has_tautulli

    return SetupStatus(
        admin_exists=admins > 0,
        plex_linked=plex_linked,
        instances=counts,
        has_radarr=has_radarr,
        has_tautulli=has_tautulli,
        has_seerr=counts.get(str(InstanceKind.SEERR), 0) > 0,
        has_scanned=has_scanned,
        scan_ready=scan_ready,
        complete=scan_ready and has_scanned,
    )
