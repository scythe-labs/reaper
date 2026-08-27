# SPDX-License-Identifier: AGPL-3.0-or-later
"""The protection lists, and whether each one is actually protecting anything. Read-only.

Every list already records its own health on every sync, as ``item_count``,
``last_synced_at``, and ``last_error``. None of that reached the operator before. The
only thing that did was the degraded-scan notice, and it fires late. A failed sync
coasts for ``snapshot.WHITELIST_STALE_AFTER`` before it degrades anything, and a check
that succeeded while leaving part of the list uncovered never said so at all.

This route is what ``lists.configured`` was written for. It adds no state of its own.
The columns were always there, and the health verdict comes from ``lists.health``,
which is also what the degradation check's bound is read against, so the screen and
the notice cannot tell the operator two different stories.

**Retired rows are excluded.** ``lists.retire_absent`` disables a slug the current
configuration no longer produces, and deliberately keeps its members, so a disabled
row still reads as populated. Listing it would show the operator a keep list that
looks healthy and protects nothing. And because a slug carries the operator's match
mode, tightening a keep rule from ANY to ALL and back leaves both spellings behind.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import AsyncExitStack

from fastapi import APIRouter, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from reaper.api import tags as api_tags
from reaper.api.errors import refuse, refuse_from
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
from reaper.engine import policy_migrations
from reaper.engine.reason import Reason, to_wire
from reaper.services import app_settings, list_config, list_rules, lists, scan_runner, snapshot
from reaper.services.snapshot import WHITELIST_STALE_AFTER
from reaper.text import fold

router = APIRouter(prefix="/api", tags=[api_tags.LISTS])


def _tag_stats(row: lists.ConfiguredList) -> tuple[dict[str, int] | None, str | None]:
    """Return the per-tag counts and server name a tag list's last good check
    recorded. Absent or malformed data reads as unknown, never as zero counts."""
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
    # An adopted row also learns the name its keep rule matches, which the legacy row
    # it was built from never carried. This is cheap, and it means the roll-up and
    # the protection arrive together, instead of the screen showing a healthy list
    # that keeps nothing.
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


async def _authorable_media(
    session: AsyncSession, cache_engine: AsyncEngine, definitions: Sequence[ListConfig]
) -> dict[int, list[str]]:
    """Return, per definition id, the media types a keep rule on it can be authored
    for. This is the set the Policy picker offers it on
    (``policy_migrations.authorable_media_scope``).

    Reads the Plex library kinds and the synced content, both best-effort. This feeds
    a screen that renders before the first sync, so a settings or cache read that
    fails leaves each list on its source-derived scope and withholds only what
    nothing else can type. Both reads are read-only and gate no deletion, so
    degrading them narrows what the picker offers, never what a scan removes.
    """
    try:
        libraries = await app_settings.get_plex_libraries(session)
    except (SQLAlchemyError, ValueError, TypeError):
        libraries = []
    library_types = policy_migrations.library_media_types(libraries)
    try:
        rows = await lists.configured(cache_engine)
    except SQLAlchemyError:
        rows = []
    observed: dict[int, set[str]] = {}
    synced: set[int] = set()
    for row in rows:
        list_id = row.list_id
        if list_id is None:
            continue
        observed.setdefault(list_id, set()).update(row.media_types)
        if row.last_synced_at is not None:
            synced.add(list_id)
    return {
        d.id: sorted(
            policy_migrations.authorable_media_scope(
                d.source,
                d.config_json,
                frozenset(observed.get(d.id, set())),
                d.id in synced,
                library_types,
            )
        )
        for d in definitions
    }


def _out(
    row: ListConfig,
    uses: list[ListPolicyUseOut] | None = None,
    authorable: list[str] | None = None,
) -> ListConfigOut:
    """Map one stored definition to the wire. A stored body that will not parse reads
    as an empty one instead of dropping the row off the screen. The operator can
    still see the list, and Edit rewrites the body through ``_clean_config``, which
    is the way out."""
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
        authorable_media=authorable or [],
    )


@router.get("/lists/configured")
async def get_configured(request: Request) -> list[ListConfigOut]:
    """Return the list definitions. This is what the operator named and where each
    one points.

    Separate from ``GET /lists``, which reports what each is currently protecting.
    One is configuration in ``reaper.db`` and the other is membership in the cache.
    Collapsing them would tie a screen that must render before the first sync to a
    table that does not exist until one has run. The browser joins them on
    ``ProtectionListOut.list_id``.
    """
    async with request.app.state.session_factory() as session:
        rows = await list_config.all_lists(session)
        uses = await list_rules.usage(session)
        authorable = await _authorable_media(session, request.app.state.cache_engine, rows)
    return [
        _out(
            row,
            [ListPolicyUseOut(**u) for u in uses.get(fold(row.name), [])],
            authorable.get(row.id, []),
        )
        for row in rows
    ]


@router.post("/lists/configured", status_code=201)
async def add_list(request: Request, body: ListConfigIn) -> ListConfigOut:
    """Add a list. Answers with the whole stored row, not an id.

    The body that comes back is the cleaned one, with the name trimmed, the tags
    trimmed, and the match mode defaulted. The form re-seeds from what was actually
    stored, rather than from what it sent, and those differ on every save that
    trimmed anything.
    """
    async with request.app.state.session_factory() as session:
        try:
            row = await list_config.create(
                session, name=body.name, source=body.source, config=body.config
            )
        except list_config.ListConfigError as exc:
            refuse_from(exc)
        # A list the operator adds here writes no keep rule on its own. Settings
        # defines what a list is. Policy defines what it does, and the operator
        # chooses whether and how strongly it protects. The row renders "Not used by
        # your policy yet" until they do, so adding a list is never a silent
        # protection the operator did not ask for. The lists Reaper ships, and the
        # ones an upgrade migrated, are named by the policy body directly (the
        # default body, ``convert_list_protections``), not from here.
        uses = await list_rules.usage(session)
        authorable = await _authorable_media(session, request.app.state.cache_engine, [row])
        return _out(
            row,
            [ListPolicyUseOut(**u) for u in uses.get(fold(row.name), [])],
            authorable.get(row.id, []),
        )


@router.patch("/lists/configured/{list_id}")
async def edit_list(request: Request, list_id: int, body: ListConfigPatch) -> ListConfigOut:
    async with request.app.state.session_factory() as session:
        try:
            before = (await list_config.get(session, list_id)).name
            row = await list_config.update(session, list_id, name=body.name, config=body.config)
        except list_config.ListConfigError as exc:
            refuse_from(exc)
        # A renamed list's rules follow it, or every one of them would go on naming a list
        # that no longer exists while rendering as a live protection.
        await list_rules.rename_list(session, before, row.name)
        # Its stored membership follows the rename too, the other half of the same
        # fix. The rules now spell the new name, and the rows still carried the old
        # one, so without this the protection would be off until the next successful
        # check.
        await lists.sync_rule_names(
            request.app.state.cache_engine, await list_config.definitions(session)
        )
        uses = await list_rules.usage(session)
        authorable = await _authorable_media(session, request.app.state.cache_engine, [row])
        return _out(
            row,
            [ListPolicyUseOut(**u) for u in uses.get(fold(row.name), [])],
            authorable.get(row.id, []),
        )


@router.post("/lists/sync")
async def sync_lists(request: Request, body: ListSyncIn) -> ListSyncOut:
    """Check one list, or all of them, now. This is the Lists screen's "Check now"
    button.

    This runs the same pass a scan runs, with the same guards, because it calls
    ``snapshot.sync_protection_lists`` directly. A second way to refresh a
    protection list would be a second set of fail-closed rules to keep in step with
    the first.

    Synchronous, like the Plex library sync and the Leaving Soon shelf beside it. A
    refresh reads every *arr and Plex once, and the answer is the whole point of
    pressing the button, so there is nothing useful to show before it lands.

    A narrowed pass sweeps only the definition it checked. See
    ``sync_protection_lists``. Checking one list can never switch another one off,
    and checking a list the operator has just edited stands its superseded rows
    down, so the count beside it stops adding the old configuration's titles to the
    new one's.
    """
    app = request.app
    settings: Settings = app.state.settings
    box: SecretBox = app.state.secret_box
    async with AsyncExitStack() as stack:
        try:
            # This is not a scan. It reads the *arr and Plex and nothing else, so it
            # does not carry a scan's Tautulli precondition. Without that
            # precondition, an install with Plex linked and no Tautulli can still
            # check its Plex collection, instead of being refused in words about
            # scans and watch history that would not make sense here.
            radarrs, sonarrs, _tautulli, _seerrs, plex = await scan_runner.build_sources(
                app.state.session_factory,
                settings,
                box,
                stack=stack,
                require_scan_sources=False,
            )
        except scan_runner.ScanConfigError as exc:
            refuse_from(exc)

        # Plex is optional and fails closed, exactly as it does in a scan. With no
        # live server, no collection provider is built, so nothing is synced for one
        # and nothing is retired either. The stored membership stays as the last
        # good check left it.
        plex_server: object | None = None
        # This carries the wire dict rather than `ReasonKey` itself. Pydantic
        # coerces it when it lands on `ListSyncOut`'s constructor kwarg below, the
        # same as `to_wire(...)` passed inline anywhere else in the tree
        # (`ChipOut`/`PolicyWarningOut`).
        plex_error_reason: dict[str, object] | None = None
        if plex is not None:
            try:
                plex_server = await plex.connect()
            except PlexError as exc:
                plex_error_reason = to_wire(Reason("plexError", {"error": str(exc)}))

        async with app.state.session_factory() as session:
            try:
                definitions = await list_config.definitions(session, strict=True)
            except list_config.ListRegistryUnreadableError:
                # This pass retires rows, so a row that will not decode has to stop
                # it, instead of reading as one the operator deleted.
                refuse(409, "error.lists.registry_unreadable")

        synced = await snapshot.sync_protection_lists(
            app.state.cache_engine,
            definitions=definitions,
            only=body.list_id,
            radarrs=radarrs,
            sonarrs=sonarrs,
            plex_server=plex_server,
        )

    # What the operator is told. This counts the failures instead of replaying each
    # list's error here, since every one of them is already on the row it belongs
    # to, which this response makes the screen refetch. A summary that restated them
    # would be the same refusal written twice.
    failed = sum(1 for v in synced.values() if isinstance(v, str) and v.startswith("error:"))
    checked = sum(1 for v in synced.values() if isinstance(v, int))
    return ListSyncOut(checked=checked, failed=failed, plex_error_reason=plex_error_reason)


@router.delete("/lists/configured/{list_id}", status_code=204)
async def remove_list(request: Request, list_id: int) -> None:
    """Delete a list, and the keep rules naming it, in the same request. Deleting
    only the row would leave rules rendering as live protections that cover nothing.
    Deleting only the rules would leave a list that quietly stopped acting. Both
    changes are re-saved through the editor's own append-only path, so pending
    approvals bound to the old hash refuse to execute."""
    async with request.app.state.session_factory() as session:
        try:
            name = (await list_config.get(session, list_id)).name
            await list_config.delete(session, list_id)
        except list_config.ListConfigError as exc:
            refuse_from(exc)
        # The rules naming it leave with it, so none goes on rendering as a live
        # protection covering nothing.
        await list_rules.detach_list(session, name)
