# SPDX-License-Identifier: AGPL-3.0-or-later
"""What is left to configure. The state a first-run wizard reads.

A fresh Reaper is useless until it is pointed at the services it reads from. The setup
flow makes that obvious and quick instead of leaving the operator to find every
setting by hand. This one endpoint answers what still needs doing, so the UI can show
the right next step and stop nagging once everything is in place.

This sits behind the auth gate. The very first admin is claimed on the login screen,
when the Plex owner signs in, and everything here happens once someone is signed in.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.api import tags as api_tags
from reaper.api.deps import session_factory
from reaper.db.models import AppUser, Instance, InstanceKind, PlexServer, Snapshot
from reaper.services import admin_password

router = APIRouter(prefix="/api/setup", tags=[api_tags.SETUP])


class SetupStatus(BaseModel):
    admin_exists: bool
    has_password: bool
    """Whether an admin password exists, which is also whether a local account exists.

    The wizard's first step sets this password, and it reads this field to know
    whether that step is done. The step the operator is on is derived from server
    state, never from how far this browser happened to get, so closing the tab resumes
    where it left off.

    This password does more than gate the wizard. It also arms deletion
    (``PUT /api/settings/safety``) and confirms a restore (``POST .../restore/confirm``,
    which refuses outright without one). Until it is set, there is no local account at
    all, so a plex.tv outage would lock the owner out of an install the login screen
    tells them keeps one.
    """
    plex_linked: bool
    instances: dict[str, int]
    """How many of each kind are configured, for example {"radarr": 2, "tautulli": 1}."""

    has_radarr: bool
    has_sonarr: bool
    has_tautulli: bool
    has_seerr: bool
    has_scanned: bool

    scan_ready: bool
    """The minimum needed to run a scan. This requires a Tautulli, plus at least one
    Radarr or Sonarr. Mirrors the guard in ``services.scan_runner.build_sources``."""
    reap_ready: bool
    """The minimum for a real run, a strictly higher bar than ``scan_ready``.

    Scanning and reaping need different readiness checks. Each condition below
    restates a refusal that already exists elsewhere, as a question the UI can ask
    before the operator picks what to delete.

    * ``has_password`` reflects that ``PUT /api/settings/safety`` is password-gated,
      so deletion cannot be armed at all without one.
    * ``plex_linked`` and ``has_tautulli`` mirror the two 409s that
      ``api.runs._preflight_refusal`` returns, and that ``services.executor.execute``
      also checks as a backstop. The check for who is watching runs through Plex. The
      played-since-approval check runs through Tautulli.
    * Needing a Radarr or a Sonarr reflects that deletion goes through an *arr, so
      with neither one, there is nothing a plan could remove.

    The first three conditions are ``scan_ready``, so this field adds a password and a
    linked Plex on top. It checks configuration only. A service that is configured but
    unreachable still fails at run time, and nothing here can know that in advance.
    """
    complete: bool
    """Whether the wizard has nothing left to push. That means a password is set,
    scanning is ready, and a scan has run.

    ``has_password`` is part of it because the wizard now asks for one. Without it, an
    install with no local account could read as complete and skip past the step that
    creates one. An existing install without a password is therefore no longer
    complete, and meets the wizard once more, on the password step, with every later
    step already satisfied.

    This field leaves out ``plex_linked`` on purpose. An install with no Plex is
    finished with the wizard, since Plex is optional for a scan and the wizard exists
    to get a scan running. Whether that install can reap is a separate question,
    answered by ``reap_ready`` and published beside this field, so this field does not
    have to mean two things.
    """


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
    async with session_factory(request)() as session:
        admins = int(
            (await session.execute(select(func.count()).select_from(AppUser))).scalar_one()
        )
        # Reads the password state directly instead of deriving it from `admins`. A
        # Plex-provider admin is not a local one, so an install can have admins and
        # still have no password, and no local account, at all.
        password_set = await admin_password.has_password(session)
        counts = await _counts(session)
        plex_linked = (
            await session.execute(select(PlexServer.id).limit(1))
        ).scalar_one_or_none() is not None
        has_scanned = (
            await session.execute(select(Snapshot.id).limit(1))
        ).scalar_one_or_none() is not None

    has_radarr = counts.get(str(InstanceKind.RADARR), 0) > 0
    has_sonarr = counts.get(str(InstanceKind.SONARR), 0) > 0
    has_tautulli = counts.get(str(InstanceKind.TAUTULLI), 0) > 0
    scan_ready = (has_radarr or has_sonarr) and has_tautulli

    return SetupStatus(
        admin_exists=admins > 0,
        has_password=password_set,
        plex_linked=plex_linked,
        instances=counts,
        has_radarr=has_radarr,
        has_sonarr=has_sonarr,
        has_tautulli=has_tautulli,
        has_seerr=counts.get(str(InstanceKind.SEERR), 0) > 0,
        has_scanned=has_scanned,
        scan_ready=scan_ready,
        reap_ready=password_set and scan_ready and plex_linked,
        complete=password_set and scan_ready and has_scanned,
    )
