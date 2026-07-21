# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Scales endpoint.

Read-only. Joins the available Seerr requests to the latest scan's candidates and returns
the per-person roll-up. Deletes nothing, plans nothing -- it is a report.

Scales sits on the last scan (see ``services.fairness``), so it needs no live Radarr,
Sonarr or Tautulli read: sizes, titles and verdicts come from the stored candidates, and
watches from the mirror. It still requires **Seerr** (who requested what) and a configured
**Tautulli** (without a watch mirror every title would read as never-played) -- if either is
missing it says so with a 400 rather than returning a leaderboard that looks complete.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from reaper.api.schemas import FairnessReportOut, ReclaimableTitleOut, RequesterRowOut
from reaper.clients.base import IntegrationError
from reaper.clients.seerr import SeerrClient
from reaper.config import RuntimeSafety
from reaper.crypto import SecretBox
from reaper.db.models import Instance, InstanceKind
from reaper.services import fairness

router = APIRouter(prefix="/api")


@router.get("/fairness")
async def get_fairness(request: Request) -> FairnessReportOut:
    box: SecretBox = request.app.state.secret_box
    # Read-only: Scales only ever GETs -- from Seerr, and from the local snapshot and mirror.
    safety = RuntimeSafety(destructive_enabled=False)

    async with request.app.state.session_factory() as session:
        rows = (
            (await session.execute(select(Instance).where(Instance.enabled.is_(True))))
            .scalars()
            .all()
        )
    seerr_row = next((r for r in rows if r.kind is InstanceKind.SEERR), None)
    tautulli_row = next((r for r in rows if r.kind is InstanceKind.TAUTULLI), None)

    if seerr_row is None or tautulli_row is None:
        raise HTTPException(
            400,
            "Scales needs a Seerr and a Tautulli instance: Seerr for who requested what, "
            "Tautulli for who watched it. Configure them in Settings.",
        )

    # The decrypted key travels on this connection, so certificate verification is only
    # relaxed where the operator explicitly turned it off for this instance in Settings.
    seerr = SeerrClient(
        seerr_row.base_url,
        box.decrypt(seerr_row.api_key_enc),
        safety=safety,
        verify=seerr_row.verify_tls,
    )
    try:
        async with seerr:
            report = await fairness.build_report(
                session_factory=request.app.state.session_factory,
                seerr=seerr,
                cache_engine=request.app.state.cache_engine,
            )
    except IntegrationError as exc:
        # An unreachable Seerr is a 502 with the reason -- never a partial leaderboard that
        # looks complete.
        raise HTTPException(502, f"Could not build Scales: {exc}") from exc

    return FairnessReportOut(
        total_requests=report.total_requests,
        total_reclaimable_bytes=report.total_reclaimable_bytes,
        total_reclaimable_items=report.total_reclaimable_items,
        not_in_scan=report.not_in_scan,
        no_snapshot=report.no_snapshot,
        horizon_at=report.horizon_at.isoformat() if report.horizon_at else None,
        rows=[
            RequesterRowOut(
                user_id=row.user_id,
                name=row.name,
                requests_made=row.requests_made,
                gb_granted_bytes=row.gb_granted_bytes,
                played_by_them=row.played_by_them,
                reclaimable_items=row.reclaimable_items,
                reclaimable_bytes=row.reclaimable_bytes,
                # Capped for transport: the count above stays exact, the list is the
                # heaviest 25 (sorted in the service) so the view can name a few and say
                # "+N more" against the real count.
                reclaimable=[
                    ReclaimableTitleOut(
                        title=t.title,
                        size_bytes=t.size_bytes,
                        item_id=t.item_id,
                        group_key=t.group_key,
                    )
                    for t in row.reclaimable[:25]
                ],
            )
            for row in report.rows
        ],
    )
