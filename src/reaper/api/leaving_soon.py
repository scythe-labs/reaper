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

from fastapi import APIRouter, Request

from reaper.api import tags as api_tags
from reaper.api.errors import refuse, refuse_from
from reaper.api.schemas import LeavingSoonOut
from reaper.clients.plex import PlexError
from reaper.config import Settings
from reaper.crypto import SecretBox
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

    # The three ways a pass declines for a reason the operator can act on: the shelf is off,
    # the last scan could not be trusted, or no server is linked. Each carries operator copy
    # and each is a 400.
    try:
        result = await leaving_soon.run_sync(request.app.state.session_factory, settings, box)
    except (LeavingSoonDisabledError, LeavingSoonDegradedError, LeavingSoonUnlinkedError) as exc:
        refuse_from(exc)
    # A linked server that did not answer is not the operator's request being wrong, and the
    # client's own text is written for a log ("movie listing for section 3 stalled at 200 of
    # 1000"), not for someone deciding what to delete. 502 in Reaper's words, the way the
    # three sibling routes answer it (rule 10, rule 21, rule 72). It used to share the 400
    # above, because the service raised `PlexError` for the not-linked case too (#734).
    except PlexError as exc:
        refuse(502, "error.plex.unreachable", error=str(exc))

    # ``ok`` and ``result`` are the pass's own words, already stored on the Jobs row by the
    # same derivation (``LeavingSoonResult.summary``). This route used to add the
    # no-libraries case here instead, after the row had been written, so the row and this
    # response described one pass differently (#555).
    return LeavingSoonOut(ok=result.ok, result=result.summary)
