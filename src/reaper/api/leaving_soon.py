# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the Leaving Soon sync.

One pass reconciles the "Leaving Soon" collection and label in every enabled library
against the current grace set, then sends the Discord heads-up for anything newly in
grace. In read-only mode, without the opt-in, the pass only previews. It reads what is
already marked, computes what would change, and announces new titles, while the shelf
writes stay guarded and Plex is left unchanged. It reports that refusal honestly, as
``applied: false``.

The same pass runs automatically after every scan (see ``leaving_soon.after_scan``).
This route is the "update now" button on the Reap page.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from reaper.api import tags as api_tags
from reaper.api.errors import refuse, refuse_from
from reaper.api.schemas import LeavingSoonOut
from reaper.clients.plex import PlexError
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.engine.explanation import ReasonKey
from reaper.engine.reason import to_wire
from reaper.services import leaving_soon
from reaper.services.leaving_soon import (
    LeavingSoonDegradedError,
    LeavingSoonDisabledError,
    LeavingSoonUnlinkedError,
)

router = APIRouter(prefix="/api", tags=[api_tags.JOBS])


@router.post("/leaving-soon/sync")
async def sync_leaving_soon(request: Request) -> LeavingSoonOut:
    settings: Settings = request.app.state.settings
    box: SecretBox = request.app.state.secret_box

    # A pass declines for a reason the operator can act on. The shelf is off, the last
    # scan could not be trusted, or no server is linked. Each reason carries operator
    # copy, and each responds with 400.
    try:
        result = await leaving_soon.run_sync(request.app.state.session_factory, settings, box)
    except (LeavingSoonDisabledError, LeavingSoonDegradedError, LeavingSoonUnlinkedError) as exc:
        refuse_from(exc)
    # A linked server that does not answer is a Plex problem, not a mistake in the
    # operator's request. The client's own error text is written for a log ("movie listing
    # for section 3 stalled at 200 of 1000"), not for someone deciding what to delete. This
    # block translates it to plain language and answers 502, matching the sibling routes.
    except PlexError as exc:
        refuse(502, "error.plex.unreachable", error=exc.as_reason())

    # ``ok`` and ``result_reason`` come from the pass's own facts, already stored on the
    # Jobs row by ``LeavingSoonResult.summary``. This response reuses that derivation
    # instead of recomputing the outcome, so the row and the response always describe
    # the same pass.
    return LeavingSoonOut(
        ok=result.ok, result_reason=ReasonKey.model_validate(to_wire(result.summary))
    )
