# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Leaving Soon endpoint.

Runs one full shelf pass: reconciles the "Leaving Soon" collection and label in every
enabled library against the current grace set, and sends the Discord heads-up for
anything newly in grace. In read-only mode (without the opt-in) this is a *preview*: it
reads what is already marked, computes what would change, and announces new titles --
but the shelf writes themselves are guarded, so nothing is written to Plex. That refusal
is reported honestly (``applied: false``), never swallowed.

The same pass runs automatically after every scan (see ``leaving_soon.after_scan``);
this route is the "update now" button on the Reap page.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from reaper.api import tags as api_tags
from reaper.api.schemas import LeavingSoonOut
from reaper.clients.plex import PlexError
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.services import leaving_soon

router = APIRouter(prefix="/api", tags=[api_tags.JOBS])


@router.post("/leaving-soon/sync")
async def sync_leaving_soon(request: Request) -> LeavingSoonOut:
    settings: Settings = request.app.state.settings
    box: SecretBox = request.app.state.secret_box

    try:
        result = await leaving_soon.run_sync(request.app.state.session_factory, settings, box)
    except leaving_soon.LeavingSoonDisabledError as exc:
        raise HTTPException(400, str(exc)) from exc
    except leaving_soon.LeavingSoonDegradedError as exc:
        raise HTTPException(400, str(exc)) from exc
    except PlexError as exc:
        raise HTTPException(400, str(exc)) from exc

    problems = result.problems
    if not result.outcomes:
        problems = ["No libraries are turned on under Settings, Plex, so no shelf was updated."]

    return LeavingSoonOut(
        added_count=result.added,
        cleared_count=result.removed,
        applied=result.applied,
        notified=result.notified,
        movies_on_shelves=result.movies_on_shelves,
        seasons_on_shelves=result.seasons_on_shelves,
        problems=problems,
    )
