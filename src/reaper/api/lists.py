# SPDX-License-Identifier: AGPL-3.0-or-later
"""The protection lists and whether each one is actually protecting anything. Read-only.

Every list already recorded its own health on every sync -- ``item_count``,
``last_synced_at`` and ``last_error`` -- and none of it reached the operator. The only thing
that ever did was the degraded-scan notice, which fires late: a failed sync coasts for
``snapshot.WHITELIST_STALE_AFTER`` before it degrades anything, and a check that succeeded
while leaving part of the list uncovered never said so at all (#475).

This route is what ``lists.configured`` was written for. It adds no state of its own: the
columns were always there, and the health verdict comes from ``lists.health``, which is also
what the degradation check's bound is read against, so the screen and the notice cannot tell
the operator two different stories.

**Retired rows are excluded.** ``lists.retire_absent`` disables a slug the current
configuration no longer produces and deliberately keeps its members, so a disabled row still
reads as populated. Listing it would show the operator a keep list that looks healthy and
protects nothing -- and, because a slug carries the operator's match mode, tightening a keep
rule from ANY to ALL and back leaves both spellings behind.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from reaper.api import tags as api_tags
from reaper.api.schemas import ProtectionListOut
from reaper.clock import utcnow
from reaper.services import lists
from reaper.services.snapshot import WHITELIST_STALE_AFTER

router = APIRouter(prefix="/api", tags=[api_tags.LISTS])


@router.get("/lists")
async def get_lists(request: Request) -> list[ProtectionListOut]:
    now = utcnow()
    return [
        ProtectionListOut(
            slug=row.slug,
            name=row.display_name,
            state=row.health(stale_after=WHITELIST_STALE_AFTER, now=now).value,
            item_count=row.item_count,
            last_checked_at=row.last_success,
            error=row.last_error,
        )
        for row in await lists.configured(request.app.state.cache_engine)
        if row.enabled
    ]
