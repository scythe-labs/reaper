# SPDX-License-Identifier: AGPL-3.0-or-later
"""The grace endpoint: what is counting down, and what has cleared.

Read-only. Reports where each currently-condemned item sits in its grace window, using
the active profile's ``grace_days``. Deletes nothing and plans nothing -- cancelling a
grace is done by sparing the item (POST /api/whitelist), and the reap of a cleared item
is the separate, supervised, still-unbuilt step.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from reaper.api.schemas import GraceItemOut, GraceReportOut
from reaper.services import grace
from reaper.services.profiles import active_profile_settings

router = APIRouter(prefix="/api")


def _item_out(item: grace.GraceItem) -> GraceItemOut:
    return GraceItemOut(
        media_key=item.media_key,
        candidate_id=item.candidate_id,
        title=item.title,
        size_bytes=item.size_bytes,
        grace_ends_at=item.grace_ends_at.isoformat(),
        days_remaining=item.days_remaining,
        in_grace=item.in_grace,
    )


@router.get("/grace")
async def get_grace(request: Request) -> GraceReportOut:
    async with request.app.state.session_factory() as session:
        settings = await active_profile_settings(session)
        report = await grace.grace_report(session, grace_days=settings.grace_days)

    # The full ready list is bounded interest; the in-grace list can be the whole condemned
    # set. Cap what crosses the wire and let the counts carry the totals -- the owner acts
    # on the soonest-to-clear, not on item 400.
    return GraceReportOut(
        grace_days=report.grace_days,
        in_grace_count=len(report.in_grace),
        ready_count=len(report.ready),
        total_bytes_in_grace=report.total_bytes_in_grace,
        total_bytes_ready=report.total_bytes_ready,
        in_grace=[_item_out(i) for i in report.in_grace[:100]],
        ready=[_item_out(i) for i in report.ready[:100]],
    )
