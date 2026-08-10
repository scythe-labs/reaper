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
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.api import tags as api_tags
from reaper.api.deps import session_factory
from reaper.db.models import AppUser, Instance, InstanceKind, PlexServer, Snapshot
from reaper.services import admin_password

router = APIRouter(prefix="/api/setup", tags=[api_tags.SETUP])


class SetupStatus(BaseModel):
    admin_exists: bool
    has_password: bool
    """Whether an admin password exists, which is also whether a local account does.

    The wizard's first step sets it, and it reads this to know whether that step is behind
    it -- the step the operator is on is derived from server state, never from how far this
    browser happened to get, so closing the tab resumes where it left off.

    It is not merely a wizard convenience. The same password arms deletion
    (``PUT /api/settings/safety``) and confirms a restore (``POST .../restore/confirm``,
    which refuses outright without one), and until it is set there is no local account at
    all -- so a plex.tv outage locks the owner out of an install the login screen tells them
    keeps one.
    """
    plex_linked: bool
    instances: dict[str, int]
    """How many of each kind are configured -- e.g. {"radarr": 2, "tautulli": 1}."""

    has_radarr: bool
    has_sonarr: bool
    has_tautulli: bool
    has_seerr: bool
    has_scanned: bool

    scan_ready: bool
    """The minimum to run a scan: a Tautulli, plus at least one Radarr or Sonarr.
    Mirrors the guard in ``services.scan_runner.build_sources``."""
    reap_ready: bool
    """The minimum for a *real* run, which is a strictly higher bar than ``scan_ready``.

    Scanning and reaping are two different readinesses and the endpoint used to publish only
    the first, under a ``complete`` that reads like both: an install with Tautulli and one
    *arr and no Plex finished the wizard, was told it was all set, and had its first reap
    refused outright at the button (#383). Each conjunct here is a refusal that already
    exists somewhere else, restated as a question the UI can ask *before* the operator picks
    what to delete:

    * ``has_password`` -- ``PUT /api/settings/safety`` is password-gated, so deletion cannot
      be armed at all without one.
    * ``plex_linked`` and ``has_tautulli`` -- ``api.runs._preflight_refusal`` returns a 409
      for each, and ``services.executor.execute`` raises the same two sentences as its
      backstop. Those refusals are correct and stay: the check for who is watching runs
      through Plex, and the played-since-approval check through Tautulli.
    * a Radarr or a Sonarr -- deletion goes *through* an *arr, so with neither there is
      nothing a plan could remove.

    The last three are ``scan_ready``, so this is that plus a password and a linked Plex.
    Configuration only: a service that is configured but unreachable still fails at run
    time, and nothing here can know that in advance.
    """
    complete: bool
    """Nothing left the wizard needs to push: a password, scan-ready, and a scan has run.

    ``has_password`` is part of it because the wizard now asks for one, and a "complete"
    that ignored it would send an install with no local account straight past the step that
    creates it. That does mean an *existing* install without a password is no longer
    complete and meets the wizard once more -- on the password step, with every later step
    already satisfied.

    It deliberately does NOT include ``plex_linked``: an install with no Plex is finished
    with the wizard, because Plex is optional for a scan and the wizard exists to get one
    running. What that install cannot do is *reap*, and that is ``reap_ready``'s question
    rather than this one -- published beside this field so the answer is available without
    this one having to mean two things (#383).
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
        # Read, never derived from `admins`: a Plex-provider admin is not a local one, so an
        # install can hold admins and still have no password (and no local account) at all.
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
