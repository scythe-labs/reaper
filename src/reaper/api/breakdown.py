# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reap-breakdown endpoint: what a reap would remove, and why. Read-only.

Deletes nothing and plans nothing -- it explains the set the runs API would plan, built
from the latest snapshot and the owner's live overrides so the numbers match the exact
set the planner will act on.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from reaper.api import tags as api_tags
from reaper.api.schemas import ReapBreakdownOut
from reaper.services import breakdown

router = APIRouter(prefix="/api", tags=[api_tags.REAP])


@router.get("/reap/breakdown")
async def get_reap_breakdown(request: Request) -> ReapBreakdownOut:
    async with request.app.state.session_factory() as session:
        report = await breakdown.reap_breakdown(session)
    # Field for field off the service record, including the nested `condemned_by` counts.
    # The wire model's own field list is what selects; a name it declares that the record
    # does not carry is caught by `test_api_type_mirror.py`'s service-side check, since
    # `from_attributes` would otherwise fill a defaulted field silently.
    return ReapBreakdownOut.model_validate(report, from_attributes=True)
