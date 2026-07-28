# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reap-breakdown endpoint: what a reap would remove, and why. Read-only.

Deletes nothing and plans nothing -- it explains the set the runs API would plan, built
from the latest snapshot and the owner's live overrides so the numbers match the exact
set the planner will act on.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from reaper.api import tags as api_tags
from reaper.api.schemas import ReapBreakdownOut, SignalCountOut
from reaper.services import breakdown

router = APIRouter(prefix="/api", tags=[api_tags.REAP])


@router.get("/reap/breakdown")
async def get_reap_breakdown(request: Request) -> ReapBreakdownOut:
    async with request.app.state.session_factory() as session:
        report = await breakdown.reap_breakdown(session)
    return ReapBreakdownOut(
        has_snapshot=report.has_snapshot,
        policy_condemned=report.policy_condemned,
        policy_condemned_bytes=report.policy_condemned_bytes,
        policy_condemned_unknown=report.policy_condemned_unknown,
        hand_spared=report.hand_spared,
        spares_expired=report.spares_expired,
        hand_reaped=report.hand_reaped,
        hand_reaped_bytes=report.hand_reaped_bytes,
        hand_reaped_unknown=report.hand_reaped_unknown,
        hand_reaped_held=report.hand_reaped_held,
        will_reap=report.will_reap,
        will_reap_bytes=report.will_reap_bytes,
        will_reap_unknown=report.will_reap_unknown,
        movies=report.movies,
        movies_unknown=report.movies_unknown,
        seasons=report.seasons,
        seasons_unknown=report.seasons_unknown,
        condemned_by=[
            SignalCountOut(id=s.id, count=s.count, bytes=s.bytes, unknown_size=s.unknown_size)
            for s in report.condemned_by
        ],
    )
