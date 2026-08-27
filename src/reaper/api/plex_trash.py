# SPDX-License-Identifier: AGPL-3.0-or-later
"""Report what Plex would remove besides the files this reap deletes. Read-only.

Reaper's end-of-run purge is section-wide. ``PUT /library/sections/{key}/emptyTrash``
empties that section's whole trash, not just the part this run caused. Anything
already sitting in the trash when the run started is in the same state before and
after it, so it cancels out of the executor's count-delta gate
(``_trash_delta_is_ours``), which cannot see it. The purge destroys its library
records anyway, including watch history, ratings, and collection membership. No file
on disk is touched.

The operator is told this before they reap, and may proceed anyway. This endpoint is
the evidence behind that warning. It plans nothing, deletes nothing, and every call it
makes is a GET.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from sqlalchemy import select

from reaper.api import tags as api_tags
from reaper.api.errors import refuse
from reaper.api.schemas import PlexTrashOut
from reaper.clients.plex import PlexClient, PlexError
from reaper.db.models import PlexServer
from reaper.services import app_settings

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=[api_tags.REAP])


@router.get("/reap/plex-trash")
async def get_plex_trash(request: Request) -> PlexTrashOut:
    """Report how much sits in Plex's trash right now, and whether Plex empties it on
    its own.

    ``trashed`` is a floor, not a complete count: it counts only the libraries that
    answered. ``sections_unreadable`` says how many did not, and the UI warns on that
    instead of reading silence as zero. An unreadable source stays unknown rather than
    being treated as empty, and "we could not look" is exactly when the operator most
    needs telling. Reporting the two separately keeps a partial read useful, since
    knowing at least N items are in there is worth more than discarding the count
    because one library timed out.
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        server = (await session.execute(select(PlexServer))).scalars().first()
        if server is None:
            # No Plex configured at all. Nothing purges, so there is nothing to warn about.
            return PlexTrashOut(configured=False, trashed=0, sections_unreadable=0)
        safety = await app_settings.runtime_safety(session, request.app.state.settings)
        libraries = await app_settings.get_plex_libraries(session)
        plex = PlexClient(
            server.connection_uri,
            request.app.state.secret_box.decrypt(server.token_enc),
            safety=safety,
            verify=server.verify_tls,
        )

    # Only the libraries the operator actually included in scans. Reaper never
    # refreshes a library it never scans, so that library's trash is not this run's
    # business.
    keys = [int(lib["key"]) for lib in libraries if lib.get("enabled", True)]

    try:
        try:
            auto_empty: bool | None = await plex.empties_trash_after_scan()
        except PlexError as exc:
            log.warning("plex.auto_empty_unreadable", error=str(exc))
            auto_empty = None

        trashed = 0
        unreadable = 0
        for key in keys:
            try:
                count = await plex.trash_count(key)
            except PlexError as exc:
                log.warning("plex.trash_count_unreadable", section=key, error=str(exc))
                unreadable += 1
                continue
            # A server that does not understand ``trash=1`` ignores it and returns the
            # whole library, which would look like an alarming count that is just the
            # library size. A live server confirmed this behavior: an unknown parameter
            # returns the full count while ``trash=1`` narrows it. This equality check
            # cannot tell the two cases apart, so it reports unreadable instead of a
            # real number.
            try:
                total = await plex.item_count(key)
            except PlexError:
                total = -1
            if total >= 0 and count == total and count > 0:
                log.warning("plex.trash_filter_ignored", section=key)
                unreadable += 1
                continue
            trashed += count
    except Exception as exc:
        log.warning("plex.trash_probe_failed", error=str(exc))
        refuse(502, "error.plex_trash.unreadable", error=str(exc))
    finally:
        await plex.aclose()

    return PlexTrashOut(
        configured=True,
        trashed=trashed,
        sections_unreadable=unreadable,
        empties_after_scan=auto_empty,
    )
