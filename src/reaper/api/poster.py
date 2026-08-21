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

import asyncio
from typing import cast

import structlog
from fastapi import APIRouter, FastAPI, Request, Response
from sqlalchemy import select

from reaper.api import tags as api_tags
from reaper.api.deps import state_singleton
from reaper.api.errors import refuse
from reaper.clients.tautulli import TautulliClient
from reaper.config import RuntimeSafety
from reaper.crypto import SecretBox
from reaper.db.models import Instance, InstanceKind

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=[api_tags.REVIEW])

#: What makes one artwork client different from another: the instance it talks to and
#: every parameter of that connection. Rotating the key, editing the URL, turning the
#: certificate check off, or pointing at a different Tautulli all change this, and a
#: changed fingerprint retires the cached client instead of leaving a stale one serving.
_Fingerprint = tuple[int, str, str, bool]


def _fingerprint(row: Instance) -> _Fingerprint:
    return (row.id, row.base_url, row.api_key_enc, row.verify_tls)


async def _artwork_client(app: FastAPI, row: Instance, box: SecretBox) -> TautulliClient:
    """The shared, read-only Tautulli client this route proxies artwork through.

    A cold review queue asks for a few hundred posters at once, and each request used to
    build a whole new client -- new connection pool, new TLS handshake -- and tear it down
    again (P-5). One client is kept on the app instead and reused across requests; the
    lifespan closes it at shutdown (:func:`close_artwork_client`), so it has an owner
    (rule 34).
    """
    lock = state_singleton(app, "artwork_client_lock", asyncio.Lock)

    want = _fingerprint(row)
    cached: tuple[_Fingerprint, TautulliClient] | None = getattr(app.state, "artwork_client", None)
    if cached is not None and cached[0] == want:
        return cached[1]

    async with lock:
        # Re-read under the lock: another request may have built it while we waited.
        cached = cast(
            "tuple[_Fingerprint, TautulliClient] | None",
            getattr(app.state, "artwork_client", None),
        )
        if cached is not None and cached[0] == want:
            return cached[1]
        # Read-only: this only ever GETs an image.
        client = TautulliClient(
            row.base_url,
            box.decrypt(row.api_key_enc),
            safety=RuntimeSafety(),
            verify=row.verify_tls,
        )
        app.state.artwork_client = (want, client)
        if cached is not None:
            # The instance changed under us. Close the client for the old one; nothing new
            # can reach it now that state points at the replacement.
            await cached[1].aclose()
        return client


async def close_artwork_client(app: FastAPI) -> None:
    """Close the cached artwork client, if one was ever built. Called by the lifespan."""
    cached: tuple[_Fingerprint, TautulliClient] | None = getattr(app.state, "artwork_client", None)
    app.state.artwork_client = None
    if cached is not None:
        await cached[1].aclose()


# Image bytes, not JSON. Without this the route publishes ``application/json`` with an empty
# schema, which tells a script author to parse a PNG as JSON (rule 72, with the two download
# routes in ``api/logs.py`` and ``api/backup.py``).
@router.get(
    "/poster/{rating_key}",
    response_class=Response,
    responses={200: {"content": {"image/*": {}}}},
)
async def poster(request: Request, rating_key: int, kind: str = "poster") -> Response:
    """Return one item's Plex artwork as image bytes, or 404 so the UI shows a placeholder.

    ``kind=poster`` (default) is the tall cover; ``kind=art`` is the wide backdrop the
    review cards and the why-panel header fade behind their text. Cached hard: artwork
    does not change often, and the review queue re-renders constantly.
    """
    box: SecretBox = request.app.state.secret_box

    async with request.app.state.session_factory() as session:
        # Tautulli is a singleton (enforced at creation), so there is one. Ordered-first
        # rather than one-or-none so this read can never raise even if the invariant were
        # somehow violated -- artwork is not the place to surface a config error.
        row = (
            (
                await session.execute(
                    select(Instance)
                    .where(Instance.kind == InstanceKind.TAUTULLI, Instance.enabled.is_(True))
                    .order_by(Instance.id)
                )
            )
            .scalars()
            .first()
        )

    if row is None:
        refuse(404, "error.poster.no_tautulli")

    client = await _artwork_client(request.app, row, box)
    result = await (client.art(rating_key) if kind == "art" else client.poster(rating_key))

    if result is None:
        refuse(404, "error.poster.not_found")

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
