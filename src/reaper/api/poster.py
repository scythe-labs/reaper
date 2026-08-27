# SPDX-License-Identifier: AGPL-3.0-or-later
"""Poster images, proxied from Plex.

The review queue shows a poster for each item. The art comes from Plex, through
Tautulli, which already holds the server token and proxies the image. Sonarr and
Radarr's own posters lag behind what Plex actually shows, and often will not load
from a browser at all.

This route proxies the image instead of pointing ``<img>`` straight at Plex, for two
reasons. The Plex token must never appear in a URL a browser can read, and a
same-origin image is cached by the browser and rides the session cookie, so it needs
no separate auth. The response carries a day-long cache header, so a queue of a few
hundred posters costs one fetch each, then nothing.

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

#: What makes one artwork client different from another. This includes the instance it
#: talks to and every parameter of that connection. Rotating the key, editing the URL,
#: turning the certificate check off, or pointing at a different Tautulli all change
#: this value, and a changed fingerprint retires the cached client instead of leaving a
#: stale one serving.
_Fingerprint = tuple[int, str, str, bool]


def _fingerprint(row: Instance) -> _Fingerprint:
    return (row.id, row.base_url, row.api_key_enc, row.verify_tls)


async def _artwork_client(app: FastAPI, row: Instance, box: SecretBox) -> TautulliClient:
    """Return the shared, read-only Tautulli client this route proxies artwork through.

    A cold review queue asks for a few hundred posters at once. Building a new client
    for each one would mean a new connection pool and a new TLS handshake per request,
    so one client is kept on the app and reused across requests. The lifespan closes it
    at shutdown (:func:`close_artwork_client`), so it always has an owner.
    """
    lock = state_singleton(app, "artwork_client_lock", asyncio.Lock)

    want = _fingerprint(row)
    cached: tuple[_Fingerprint, TautulliClient] | None = getattr(app.state, "artwork_client", None)
    if cached is not None and cached[0] == want:
        return cached[1]

    async with lock:
        # Re-read under the lock. Another request may have built it while this one waited.
        cached = cast(
            "tuple[_Fingerprint, TautulliClient] | None",
            getattr(app.state, "artwork_client", None),
        )
        if cached is not None and cached[0] == want:
            return cached[1]
        # Read-only. This only ever sends a GET for an image.
        client = TautulliClient(
            row.base_url,
            box.decrypt(row.api_key_enc),
            safety=RuntimeSafety(),
            verify=row.verify_tls,
        )
        app.state.artwork_client = (want, client)
        if cached is not None:
            # The instance changed while this ran. Close the client for the old one.
            # Nothing new can reach it now that state points at the replacement.
            await cached[1].aclose()
        return client


async def close_artwork_client(app: FastAPI) -> None:
    """Close the cached artwork client, if one was ever built. Called by the lifespan."""
    cached: tuple[_Fingerprint, TautulliClient] | None = getattr(app.state, "artwork_client", None)
    app.state.artwork_client = None
    if cached is not None:
        await cached[1].aclose()


# These are image bytes, not JSON. Without this response class, the route would publish
# ``application/json`` with an empty schema, telling a script author to parse a PNG as
# JSON. The download routes in ``api/logs.py`` and ``api/backup.py`` need the same
# response class for the same reason.
@router.get(
    "/poster/{rating_key}",
    response_class=Response,
    responses={200: {"content": {"image/*": {}}}},
)
async def poster(request: Request, rating_key: int, kind: str = "poster") -> Response:
    """Return one item's Plex artwork as image bytes, or 404 so the UI shows a placeholder.

    ``kind=poster`` (default) is the tall cover. ``kind=art`` is the wide backdrop the
    review cards and the why-panel header fade behind their text. The response caches
    hard, since artwork rarely changes and the review queue re-renders constantly.
    """
    box: SecretBox = request.app.state.secret_box

    async with request.app.state.session_factory() as session:
        # Tautulli is a singleton, enforced at creation, so there is exactly one row.
        # This reads the first by order instead of asserting one-or-none, so the read
        # can never raise even if that invariant were somehow violated. Artwork is not
        # the place to surface a config error.
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
    # The bytes are relayed from Plex on Reaper's own origin, so this pins how the
    # browser reads them. ``nosniff`` stops the browser from MIME-sniffing an unexpected
    # payload into something executable. The Tautulli client already restricts
    # ``content_type`` to a raster allow-list, with no ``image/svg+xml``, so together
    # these two stop an upstream image from running script same-origin.
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )
