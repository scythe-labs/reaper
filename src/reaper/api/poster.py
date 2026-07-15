# SPDX-License-Identifier: AGPL-3.0-or-later
"""Poster images, proxied from Plex.

The review queue shows a poster for each item. The art comes from **Plex** (via Tautulli,
which already holds the server token and proxies the image), not from Sonarr/Radarr — the
*arr posters lag behind the artwork you actually see in Plex, and often will not load from
a browser at all.

Why proxy at all, rather than pointing ``<img>`` straight at Plex? Two reasons: the Plex
token must never appear in a URL a browser can read, and a same-origin image is cached by
the browser and rides the session cookie, so it needs no separate auth. The response
carries a day-long cache header, so a queue of a few hundred posters costs one fetch each,
then nothing.

Read-only, like everything Reaper does to Tautulli and Plex.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select

from reaper.clients.tautulli import TautulliClient
from reaper.config import RuntimeSafety
from reaper.crypto import SecretBox
from reaper.db.models import Instance, InstanceKind

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api")


@router.get("/poster/{rating_key}")
async def poster(request: Request, rating_key: int, kind: str = "poster") -> Response:
    """Return one item's Plex artwork as image bytes, or 404 so the UI shows a placeholder.

    ``kind=poster`` (default) is the tall cover; ``kind=art`` is the wide backdrop the
    review cards and the why-panel header fade behind their text. Cached hard: artwork
    does not change often, and the review queue re-renders constantly.
    """
    box: SecretBox = request.app.state.secret_box

    async with request.app.state.session_factory() as session:
        row = (
            await session.execute(
                select(Instance).where(
                    Instance.kind == InstanceKind.TAUTULLI, Instance.enabled.is_(True)
                )
            )
        ).scalar_one_or_none()

    if row is None:
        raise HTTPException(404, "No Tautulli configured to fetch artwork from.")

    # Read-only: this only ever GETs an image.
    client = TautulliClient(row.base_url, box.decrypt(row.api_key_enc), safety=RuntimeSafety())
    async with client:
        result = await (client.art(rating_key) if kind == "art" else client.poster(rating_key))

    if result is None:
        raise HTTPException(404, "No artwork for this item.")

    content, content_type = result
    # The bytes are relayed from Plex on Reaper's own origin, so pin how the browser reads
    # them. ``nosniff`` stops MIME-sniffing an unexpected payload into something executable;
    # the Tautulli client already restricts ``content_type`` to a raster allow-list (no
    # image/svg+xml), so between the two an upstream image cannot run script same-origin.
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )
