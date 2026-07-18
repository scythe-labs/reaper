# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fairness leaderboard endpoint.

Read-only. Builds Seerr, Tautulli and *arr clients from the stored instances, gathers the
requester rule's inputs, and returns the per-person roll-up. Deletes nothing, plans
nothing -- it is a report.

If Seerr is not configured the endpoint says so with a 400, rather than returning an
empty leaderboard that would read as "nobody has requested anything".
"""

from __future__ import annotations

from contextlib import AsyncExitStack

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from reaper.api.schemas import FairnessReportOut, RequesterRowOut
from reaper.clients.arr import RadarrClient, SonarrClient
from reaper.clients.base import IntegrationError
from reaper.clients.seerr import SeerrClient
from reaper.clients.tautulli import TautulliClient
from reaper.config import RuntimeSafety
from reaper.crypto import SecretBox
from reaper.db.models import Instance, InstanceKind
from reaper.services import fairness

router = APIRouter(prefix="/api")


@router.get("/fairness")
async def get_fairness(request: Request) -> FairnessReportOut:
    box: SecretBox = request.app.state.secret_box
    # Read-only: the fairness view only ever GETs -- from Seerr, Tautulli and the *arr.
    safety = RuntimeSafety(destructive_enabled=False)

    async with request.app.state.session_factory() as session:
        rows = (
            (await session.execute(select(Instance).where(Instance.enabled.is_(True))))
            .scalars()
            .all()
        )
    seerr_row = next((r for r in rows if r.kind is InstanceKind.SEERR), None)
    tautulli_row = next((r for r in rows if r.kind is InstanceKind.TAUTULLI), None)
    radarr_rows = [r for r in rows if r.kind is InstanceKind.RADARR]
    sonarr_rows = [r for r in rows if r.kind is InstanceKind.SONARR]

    if seerr_row is None or tautulli_row is None:
        raise HTTPException(
            400,
            "The fairness view needs a Seerr and a Tautulli instance: Seerr for who "
            "requested what, Tautulli for who watched it. Configure them in Settings.",
        )

    # Each client carries its instance's own TLS setting (``verify_tls``, on by default):
    # the decrypted API keys travel on these connections, so certificate verification is
    # only relaxed where the operator explicitly turned it off for that one instance in
    # Settings -- never silently, and never for the others.
    seerr = SeerrClient(
        seerr_row.base_url,
        box.decrypt(seerr_row.api_key_enc),
        safety=safety,
        verify=seerr_row.verify_tls,
    )
    tautulli = TautulliClient(
        tautulli_row.base_url,
        box.decrypt(tautulli_row.api_key_enc),
        safety=safety,
        verify=tautulli_row.verify_tls,
    )
    # The *arr are read for one thing here: the real size on disk of each requested title,
    # which is what they -- not Tautulli -- are the authority on. Radarr sizes movies,
    # Sonarr sizes shows. Both are optional; without them the sizes fall back to Tautulli's.
    radarrs = [
        RadarrClient(
            r.base_url,
            box.decrypt(r.api_key_enc),
            safety=safety,
            api_path_prefix=r.api_path_prefix,
            verify=r.verify_tls,
        )
        for r in radarr_rows
    ]
    sonarrs = [
        SonarrClient(
            r.base_url,
            box.decrypt(r.api_key_enc),
            safety=safety,
            api_path_prefix=r.api_path_prefix,
            verify=r.verify_tls,
        )
        for r in sonarr_rows
    ]
    try:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(seerr)
            await stack.enter_async_context(tautulli)
            for arr in (*radarrs, *sonarrs):
                await stack.enter_async_context(arr)
            report = await fairness.build_report(
                seerr=seerr,
                tautulli=tautulli,
                cache_engine=request.app.state.cache_engine,
                radarrs=radarrs,
                sonarrs=sonarrs,
            )
    except IntegrationError as exc:
        # An unreachable Seerr/Tautulli is a 502 with the reason -- never a partial
        # leaderboard that looks complete.
        raise HTTPException(502, f"Could not build the fairness view: {exc}") from exc

    return FairnessReportOut(
        total_requests=report.total_requests,
        total_reclaimable_bytes=report.total_reclaimable_bytes,
        total_reclaimable_items=report.total_reclaimable_items,
        unmatched_requests=report.unmatched_requests,
        horizon_at=report.horizon_at.isoformat() if report.horizon_at else None,
        rows=[
            RequesterRowOut(
                name=row.name,
                requests_made=row.requests_made,
                gb_granted_bytes=row.gb_granted_bytes,
                played_by_them=row.played_by_them,
                reclaimable_items=row.reclaimable_items,
                reclaimable_bytes=row.reclaimable_bytes,
                unwatched_titles=row.unwatched_titles[:25],
            )
            for row in report.rows
        ],
    )
