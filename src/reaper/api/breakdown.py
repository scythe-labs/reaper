# SPDX-License-Identifier: AGPL-3.0-or-later
"""Show what a reap would remove, and why. Read-only.

It reports the same set the runs API would plan, built from the latest snapshot and the
owner's live overrides, so the numbers match what the planner would act on.
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
    # Copies the service record field by field, including the nested `condemned_by` counts.
    # The wire model's field list controls what copies over. If the model names a field the
    # record does not have, `test_api_type_mirror.py` catches it. Without that test,
    # `from_attributes` would silently fill the field with its default instead of failing.
    return ReapBreakdownOut.model_validate(report, from_attributes=True)
