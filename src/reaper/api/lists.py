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
    ListPolicyUseOut,
    ListSyncIn,
    ListSyncOut,
    ProtectionListOut,
)
from reaper.clients.plex import PlexError
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.models import ListConfig
from reaper.services import list_config, list_rules, lists, scan_runner, snapshot
from reaper.services.snapshot import WHITELIST_STALE_AFTER
from reaper.text import fold

router = APIRouter(prefix="/api", tags=[api_tags.LISTS])


def _tag_stats(row: lists.ConfiguredList) -> tuple[dict[str, int] | None, str | None]:
    """The per-tag counts and server name a tag list's last good check recorded. Absent or
    malformed reads as unknown, never as zero counts (rule 96's direction)."""
    stats = row.stats or {}
    tags = stats.get("tags")
    counts = (
        {str(k): int(v) for k, v in tags.items() if isinstance(v, int)}
        if isinstance(tags, dict)
        else None
    )
    server = stats.get("server")
    return counts, str(server) if server else None


@router.get("/lists")
async def get_lists(request: Request) -> list[ProtectionListOut]:
    now = utcnow()
    # Rows stored before their definition existed are re-homed under the definition before
    # this screen reads them, so an upgrade's lists render rolled up and editable at once
    # rather than after the next successful check (``lists.adopt_legacy``).
    async with request.app.state.session_factory() as session:
        definitions = await list_config.definitions(session)
    await lists.adopt_legacy(request.app.state.cache_engine, definitions)
    # And an adopted row learns the name its keep rule matches, which the legacy row it was
    # built from never carried. Cheap, and it means the roll-up and the protection arrive
    # together rather than the screen showing a healthy list that keeps nothing (#507).
    await lists.sync_rule_names(request.app.state.cache_engine, definitions)
    out: list[ProtectionListOut] = []
    for row in await lists.configured(request.app.state.cache_engine):
        if not row.enabled:
            continue
        tag_counts, server = _tag_stats(row)
        out.append(
            ProtectionListOut(
                slug=row.slug,
                name=row.display_name,
                source=row.source.value,
                state=row.health(stale_after=WHITELIST_STALE_AFTER, now=now).value,
                item_count=row.item_count,
                last_checked_at=row.last_success,
                error=row.last_error,
                list_id=row.list_id,
                tags=tag_counts,
                server=server,
                media_types=sorted(row.media_types),
            )
        )
    return out


def _refused(exc: list_config.ListConfigError) -> HTTPException:
    """The service's own words, at 400. It writes for the operator, so nothing is reworded
    here -- a second phrasing of one refusal is the copy that drifts (rule 144)."""
    return HTTPException(status_code=400, detail=str(exc))


def _out(row: ListConfig, uses: list[ListPolicyUseOut] | None = None) -> ListConfigOut:
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
        policy_use=uses or [],
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
        rows = await list_config.all_lists(session)
        uses = await list_rules.usage(session)
    return [
        _out(
            row,
            [ListPolicyUseOut(**u) for u in uses.get(fold(row.name), [])],
        )
        for row in rows
    ]


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
        # A list the operator adds here writes no keep rule on its own. Settings owns what a
        # list IS; Policy owns what it does, and the operator chooses whether and how strongly
        # it protects. The row renders "Not used by your policy yet" until they do, so adding a
        # list is never a silent protection the operator did not ask for. The lists Reaper
        # ships and the ones an upgrade migrated are named by the policy body directly (the
        # default body, ``convert_list_protections``), not from here.
        uses = await list_rules.usage(session)
        return _out(row, [ListPolicyUseOut(**u) for u in uses.get(fold(row.name), [])])


@router.patch("/lists/configured/{list_id}")
async def edit_list(request: Request, list_id: int, body: ListConfigPatch) -> ListConfigOut:
    async with request.app.state.session_factory() as session:
        try:
            before = (await list_config.get(session, list_id)).name
            row = await list_config.update(session, list_id, name=body.name, config=body.config)
        except list_config.ListConfigError as exc:
            raise _refused(exc) from None
        # A renamed list's rules follow it, or every one of them would go on naming a list
        # that no longer exists while rendering as a live protection.
        await list_rules.rename_list(session, before, row.name)
        # And so does its stored membership, which is the other half of the same rename:
        # the rules now spell the new name and the rows still carried the old one, so the
        # protection would be off until the next successful check (#507).
        await lists.sync_rule_names(
            request.app.state.cache_engine, await list_config.definitions(session)
        )
        uses = await list_rules.usage(session)
        return _out(row, [ListPolicyUseOut(**u) for u in uses.get(fold(row.name), [])])


@router.post("/lists/sync")
async def sync_lists(request: Request, body: ListSyncIn) -> ListSyncOut:
    """Check one list, or all of them, now. The Lists screen's "Check now".

    The same pass a scan runs, with the same guards, because it IS
    ``snapshot.sync_protection_lists`` -- a second way to refresh a protection list would be a
    second set of fail-closed rules to keep in step (rule 3/22's shape for the safety path).

    Synchronous, like the Plex library sync and the Leaving Soon shelf beside it: a refresh
    reads every *arr and Plex once, and the answer is the whole point of pressing the button,
    so there is nothing useful to show before it lands.

    A narrowed pass sweeps only the definition it checked -- see ``sync_protection_lists``.
    What that means here: checking one list can never switch another one off, and checking a
    list the operator has just edited stands its superseded rows down, so the count beside it
    stops adding the old configuration's titles to the new one's.
    """
    app = request.app
    settings: Settings = app.state.settings
    box: SecretBox = app.state.secret_box
    async with AsyncExitStack() as stack:
        try:
            # Not a scan: this reads the *arr and Plex and nothing else, so it does not carry
            # a scan's Tautulli precondition. With it on, an install with Plex linked and no
            # Tautulli was refused a check of its Plex collection, in words about scans and
            # watch history (rule 21).
            radarrs, sonarrs, _tautulli, _seerrs, plex = await scan_runner.build_sources(
                app.state.session_factory,
                settings,
                box,
                stack=stack,
                require_scan_sources=False,
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
            try:
                definitions = await list_config.definitions(session, strict=True)
            except list_config.ListRegistryUnreadableError:
                # This pass retires, so a row that will not decode has to stop it rather than
                # read as one the operator deleted (rules 65/91).
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "One of your lists is saved in a form Reaper can't read, so it didn't "
                        "check any of them. Open that list, set it up again, and save it."
                    ),
                ) from None

        synced = await snapshot.sync_protection_lists(
            app.state.cache_engine,
            definitions=definitions,
            only=body.list_id,
            radarrs=radarrs,
            sonarrs=sonarrs,
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
    """Delete a list, and the keep rules naming it in the same request. Deleting only the
    row would leave rules rendering as live protections that cover nothing (rule 25);
    deleting only the rules would leave a list that quietly stopped acting. Both policies
    are re-saved through the editor's own append-only path, so pending approvals bound to
    the old hash refuse to execute (rule 113)."""
    async with request.app.state.session_factory() as session:
        try:
            name = (await list_config.get(session, list_id)).name
            await list_config.delete(session, list_id)
        except list_config.ListConfigError as exc:
            raise _refused(exc) from None
        # The rules naming it leave with it, so none goes on rendering as a live
        # protection covering nothing (rule 25).
        await list_rules.detach_list(session, name)
