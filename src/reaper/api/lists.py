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
from contextlib import AsyncExitStack

from fastapi import APIRouter, HTTPException, Request

from reaper.api import tags as api_tags
from reaper.api.schemas import (
    ListConfigIn,
    ListConfigOut,
    ListConfigPatch,
    ListSyncIn,
    ListSyncOut,
    ProtectionListOut,
)
from reaper.clients.plex import PlexError
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.models import ListConfig
from reaper.services import list_config, lists, profiles, scan_runner, snapshot
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
            list_id=row.list_id,
        )
        for row in await lists.configured(request.app.state.cache_engine)
        if row.enabled
    ]


def _refused(exc: list_config.ListConfigError) -> HTTPException:
    """The service's own words, at 400. It writes for the operator, so nothing is reworded
    here -- a second phrasing of one refusal is the copy that drifts (rule 144)."""
    return HTTPException(status_code=400, detail=str(exc))


def _out(row: ListConfig) -> ListConfigOut:
    """One stored definition, on the wire. A stored body that will not parse reads as an
    empty one rather than raising a row off the screen (rule 96): the operator can see the
    list, and Edit rewrites the body through ``_clean_config``, which is the way out."""
    try:
        config = json.loads(row.config_json or "{}")
    except ValueError:
        config = {}
    return ListConfigOut(
        id=row.id,
        name=row.name,
        source=row.source,
        config=config if isinstance(config, dict) else {},
        enabled=row.enabled,
        built_in=row.built_in,
    )


@router.get("/lists/configured")
async def get_configured(request: Request) -> list[ListConfigOut]:
    """The list DEFINITIONS: what the operator named and where each one points.

    Separate from ``GET /lists``, which reports what each is currently protecting. One is
    configuration in ``reaper.db`` and the other is membership in the cache, and collapsing
    them would tie a screen that must render before the first sync to a table that does not
    exist until one has run. The browser joins them on ``ProtectionListOut.list_id``.
    """
    async with request.app.state.session_factory() as session:
        return [_out(row) for row in await list_config.all_lists(session)]


@router.post("/lists/configured", status_code=201)
async def add_list(request: Request, body: ListConfigIn) -> ListConfigOut:
    """Add a list. Answers with the whole stored row, not an id.

    The body that comes back is the CLEANED one -- trimmed name, trimmed tags, match mode
    defaulted -- so the form re-seeds from what was actually stored rather than from what it
    sent (rule 39). Those differ on every save that trimmed anything.
    """
    async with request.app.state.session_factory() as session:
        try:
            row = await list_config.create(
                session, name=body.name, source=body.source, config=body.config
            )
        except list_config.ListConfigError as exc:
            raise _refused(exc) from None
        return _out(row)


@router.patch("/lists/configured/{list_id}")
async def edit_list(request: Request, list_id: int, body: ListConfigPatch) -> ListConfigOut:
    async with request.app.state.session_factory() as session:
        try:
            row = await list_config.update(
                session, list_id, name=body.name, config=body.config, enabled=body.enabled
            )
        except list_config.ListConfigError as exc:
            raise _refused(exc) from None
        return _out(row)


@router.post("/lists/sync")
async def sync_lists(request: Request, body: ListSyncIn) -> ListSyncOut:
    """Check one list, or all of them, now. The Lists screen's "Check now".

    The same pass a scan runs, with the same guards, because it IS
    ``snapshot.sync_protection_lists`` -- a second way to refresh a protection list would be a
    second set of fail-closed rules to keep in step (rule 3/22's shape for the safety path).

    Synchronous, like the Plex library sync and the Leaving Soon shelf beside it: a refresh
    reads every *arr and Plex once, and the answer is the whole point of pressing the button,
    so there is nothing useful to show before it lands.

    A narrowed pass never retires anything -- see ``sync_protection_lists``. What that means
    here: checking one list can never switch another one off.
    """
    app = request.app
    settings: Settings = app.state.settings
    box: SecretBox = app.state.secret_box
    async with AsyncExitStack() as stack:
        try:
            radarrs, sonarrs, _tautulli, _seerrs, plex = await scan_runner.build_sources(
                app.state.session_factory, settings, box, stack=stack
            )
        except scan_runner.ScanConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        # Plex is optional and fails CLOSED, exactly as it does in a scan: with no live
        # server no collection provider is built, so nothing is synced for one and nothing
        # is retired either. The stored membership stays as the last good check left it.
        plex_server: object | None = None
        plex_error: str | None = None
        if plex is not None:
            try:
                plex_server = await plex.connect()
            except PlexError as exc:
                plex_error = (
                    f"Reaper couldn't reach Plex, so its collections were not checked: {exc}"
                )

        async with app.state.session_factory() as session:
            definitions = await list_config.definitions(session)
            active_movie, active_tv = await profiles.active_policies(session)

        synced = await snapshot.sync_protection_lists(
            app.state.cache_engine,
            definitions=definitions,
            only=body.list_id,
            keep_tags_only=body.keep_tags,
            radarrs=radarrs,
            sonarrs=sonarrs,
            movie_keep_tags=active_movie.body.keep_tags,
            movie_keep_match=active_movie.body.keep_tags_match,
            tv_keep_tags=active_tv.body.keep_tags,
            tv_keep_match=active_tv.body.keep_tags_match,
            keep_tags_trusted=not (active_movie.fell_back or active_tv.fell_back),
            plex_server=plex_server,
        )

    # What the operator is told. Counting the failures rather than replaying each list's error
    # here: every one of them is already on the row it belongs to, which this response makes
    # the screen refetch, and a summary that restated them would be the same refusal written
    # twice (rule 144).
    failed = sum(1 for v in synced.values() if isinstance(v, str) and v.startswith("error:"))
    checked = sum(1 for v in synced.values() if isinstance(v, int))
    return ListSyncOut(checked=checked, failed=failed, plex_error=plex_error)


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
