# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Leaving Soon endpoint.

Reconciles the "Leaving Soon" Plex label against the current grace set and sends the
Discord heads-up for anything newly marked. In read-only mode this is a *preview*: it
reads what is already labelled, computes what would change, and announces new titles --
but the label writes themselves are guarded, so nothing is written to Plex until Reaper
is armed. That refusal is reported honestly (``applied: false``), never swallowed.

Needs a linked Plex server; without one it is a 400, because a Leaving Soon reconcile
with no library to reconcile against is a no-op dressed up as a success.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from reaper.api.schemas import LeavingSoonOut
from reaper.clients.base import SafetyViolationError
from reaper.clients.plex import PlexClient, PlexError
from reaper.config import RuntimeSafety, Settings
from reaper.crypto import SecretBox
from reaper.db.models import PlexServer
from reaper.notify.discord import build_notifier
from reaper.services import app_settings, grace, leaving_soon
from reaper.services.profiles import active_profile_settings

router = APIRouter(prefix="/api")


async def _safety(request: Request) -> RuntimeSafety:
    """Whether deletion is on right now: the stored toggle, falling back to the env
    default on a fresh install. Never the env var alone -- see app_settings.runtime_safety."""
    settings: Settings = request.app.state.settings
    async with request.app.state.session_factory() as session:
        return await app_settings.runtime_safety(session, settings)


async def _plex_client(request: Request, safety: RuntimeSafety) -> PlexClient | None:
    box: SecretBox = request.app.state.secret_box
    async with request.app.state.session_factory() as session:
        server = (await session.execute(select(PlexServer))).scalars().first()
    if server is None:
        return None
    return PlexClient(server.connection_uri, box.decrypt(server.token_enc), safety=safety)


@router.post("/leaving-soon/sync")
async def sync_leaving_soon(request: Request) -> LeavingSoonOut:
    settings: Settings = request.app.state.settings

    safety = await _safety(request)
    plex = await _plex_client(request, safety)
    if plex is None:
        raise HTTPException(
            400,
            "Leaving Soon needs a linked Plex server to mark items in. Link Plex first.",
        )

    async with request.app.state.session_factory() as session:
        profile = await active_profile_settings(session)
        report = await grace.grace_report(session, grace_days=profile.grace_days)

    try:
        sections = await plex.movie_section_titles()
    except PlexError as exc:
        raise HTTPException(502, f"Could not reach Plex: {exc}") from exc
    if not sections:
        raise HTTPException(400, "No movie library found in Plex to mark.")

    target = leaving_soon.PlexLabelTarget(plex, sections[0])

    box: SecretBox = request.app.state.secret_box
    async with request.app.state.session_factory() as session:
        # The webhook lives in the DB (encrypted); build_notifier reads it and may seed it
        # from REAPER_DISCORD_WEBHOOK on first boot, so commit to persist that one-time seed.
        notifier = await build_notifier(session, box, settings)
        already_announced = await app_settings.get_leaving_soon_announced(session)
        await session.commit()

    # Write the label only when the guard would permit it -- armed, or the host opted in
    # via REAPER_ALLOW_UNARMED_LEAVING_SOON. Otherwise this is a preview: it reads what is
    # labelled, computes the diff, and sends the Discord heads-up, but writes nothing.
    # Deciding here (rather than letting the guarded write raise) keeps a read-only,
    # opted-out instance returning an honest ``applied: false`` instead of a 500.
    apply = safety.leaving_soon_write_allowed
    try:
        result = await leaving_soon.sync(
            target, report, notifier=notifier, apply=apply, already_announced=already_announced
        )
    except (PlexError, SafetyViolationError) as exc:
        raise HTTPException(502, f"Plex refused the reconcile: {exc}") from exc

    # Persist the announced set so a repeated sync does not re-spam the same titles -- the
    # heads-up is idempotent even when the label write never landed (preview / unarmed).
    async with request.app.state.session_factory() as session:
        await app_settings.set_leaving_soon_announced(session, set(result.announced))
        await session.commit()

    newly = set(result.plan.to_add)
    sample = [i.title for i in report.in_grace if i.plex_rating_key in newly][:20]
    return LeavingSoonOut(
        to_add_count=len(result.plan.to_add),
        to_remove_count=len(result.plan.to_remove),
        applied=result.applied,
        notified=result.notified,
        sample_added=sample,
    )
