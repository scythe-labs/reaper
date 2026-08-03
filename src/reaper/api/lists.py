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

import json

from fastapi import APIRouter, HTTPException, Request

from reaper.api import tags as api_tags
from reaper.api.schemas import ListConfigIn, ListConfigPatch, ProtectionListOut
from reaper.clock import utcnow
from reaper.services import list_config, lists
from reaper.services.snapshot import WHITELIST_STALE_AFTER

router = APIRouter(prefix="/api", tags=[api_tags.LISTS])


@router.get("/lists")
async def get_lists(request: Request) -> list[ProtectionListOut]:
    now = utcnow()
    return [
        ProtectionListOut(
            slug=row.slug,
            name=row.display_name,
            source=row.source.value,
            state=row.health(stale_after=WHITELIST_STALE_AFTER, now=now).value,
            item_count=row.item_count,
            last_checked_at=row.last_success,
            error=row.last_error,
        )
        for row in await lists.configured(request.app.state.cache_engine)
        if row.enabled
    ]


def _refused(exc: list_config.ListConfigError) -> HTTPException:
    """The service's own words, at 400. It writes for the operator, so nothing is reworded
    here -- a second phrasing of one refusal is the copy that drifts (rule 144)."""
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/lists/configured")
async def get_configured(request: Request) -> list[dict[str, object]]:
    """The list DEFINITIONS: what the operator named and where each one points.

    Separate from ``GET /lists``, which reports what each is currently protecting. One is
    configuration in ``reaper.db`` and the other is membership in the cache, and collapsing
    them would tie a screen that must render before the first sync to a table that does not
    exist until one has run.
    """
    async with request.app.state.session_factory() as session:
        rows = await list_config.all_lists(session)
        return [
            {
                "id": row.id,
                "name": row.name,
                "source": row.source,
                "config": json.loads(row.config_json or "{}"),
                "enabled": row.enabled,
                "built_in": row.built_in,
            }
            for row in rows
        ]


@router.post("/lists/configured", status_code=201)
async def add_list(request: Request, body: ListConfigIn) -> dict[str, object]:
    async with request.app.state.session_factory() as session:
        try:
            row = await list_config.create(
                session, name=body.name, source=body.source, config=body.config
            )
        except list_config.ListConfigError as exc:
            raise _refused(exc) from None
        return {"id": row.id, "name": row.name}


@router.patch("/lists/configured/{list_id}")
async def edit_list(request: Request, list_id: int, body: ListConfigPatch) -> dict[str, object]:
    async with request.app.state.session_factory() as session:
        try:
            row = await list_config.update(
                session, list_id, name=body.name, config=body.config, enabled=body.enabled
            )
        except list_config.ListConfigError as exc:
            raise _refused(exc) from None
        return {"id": row.id, "name": row.name, "enabled": row.enabled}


@router.delete("/lists/configured/{list_id}", status_code=204)
async def remove_list(request: Request, list_id: int) -> None:
    """Delete a list. Refused for one Reaper ships with; switching it off is always available.

    **The rules-still-name-it check is deliberately absent, not forgotten.** Deleting a list
    a keep rule points at would withdraw a protection while the rule went on rendering as a
    live one, so that refusal belongs here -- but no policy rule can name a list until the
    ``on_list`` field exists, so the check could not fire today and a guard that cannot fire
    reads as protection that is not there (rule 38/117). It lands in the change that adds the
    field, and `tests/test_list_config.py` pins that this route refuses a built-in so the
    other half is not the only thing standing between an operator and a deleted protection.
    """
    async with request.app.state.session_factory() as session:
        try:
            await list_config.delete(session, list_id)
        except list_config.ListConfigError as exc:
            raise _refused(exc) from None
