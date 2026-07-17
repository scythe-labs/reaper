# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tautulli -- the watch-history source.

Two properties of this API drive the design:

**Its key is full admin, and destructive commands are GETs.** The API is a single
dispatcher, ``GET /api/v2?apikey=...&cmd=...``, so ``cmd=delete_library``,
``cmd=delete_history`` and ``cmd=restart`` all arrive as ordinary GETs.
``GuardedTransport`` filters on HTTP method and would wave every one of them
through. So this client enforces a **command allow-list** instead: if a command is
not on it, the request is never constructed. Reaper only ever *reads* from
Tautulli.

**Its API key travels in the query string.** A logged URL is therefore a logged
credential, which is why ``reaper.logging.redact_secrets`` scrubs query strings and
not merely header values.

Response envelope::

    {"response": {"result": "success"|"error", "message": ..., "data": ...}}
"""

from __future__ import annotations

from typing import Any, ClassVar, Final

import structlog

from reaper.clients.base import BaseClient, IntegrationError, SafetyViolationError
from reaper.config import RuntimeSafety

log = structlog.get_logger(__name__)

# Every command Reaper is permitted to issue. Read-only, exhaustively.
#
# This is an allow-list rather than a deny-list on purpose: a new Tautulli release
# that adds a destructive command is then safe by default, whereas a deny-list
# would silently permit it.
READ_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "get_activity",  # active streams -- the pre-delete veto
        "get_history",
        "get_libraries",
        "get_libraries_table",
        "get_library_media_info",  # the sweep: last_played, play_count, file_size, added_at
        "get_metadata",
        "get_children_metadata",
        "get_item_watch_time_stats",
        "get_item_user_stats",
        "get_user",
        "get_server_info",
        "get_server_identity",
        "pms_image_proxy",  # fetch Plex artwork through Tautulli (read-only)
        "status",
    }
)

# Poster art is raster, and only raster. ``pms_image_proxy`` relays whatever Plex stored,
# and those bytes are served back same-origin from ``/api/poster``. A substring ``"image"``
# check admits ``image/svg+xml`` -- which, opened directly, renders same-origin and executes
# any embedded script. So the proxy accepts an explicit raster allow-list and rejects
# everything else (SVG included) rather than trusting the upstream Content-Type.
ALLOWED_IMAGE_TYPES: Final[frozenset[str]] = frozenset({"image/jpeg", "image/png", "image/webp"})


class TautulliClient(BaseClient):
    service: ClassVar[str] = "tautulli"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        safety: RuntimeSafety,
        verify: bool = True,
    ) -> None:
        super().__init__(
            base_url, safety=safety, headers={"Accept": "application/json"}, verify=verify
        )
        self._api_key = api_key

    async def call(self, cmd: str, **params: Any) -> Any:
        """Issue a read command and unwrap the response envelope."""
        if cmd not in READ_COMMANDS:
            # Not an IntegrationError: this is a programming error, and the request
            # must never be built. Tautulli's key can delete libraries and restart
            # the service, and its destructive commands are GETs -- so the HTTP-method
            # guard in GuardedTransport cannot catch them.
            raise SafetyViolationError(
                f"Tautulli command {cmd!r} is not on Reaper's read-only allow-list. "
                "Reaper never writes to Tautulli."
            )

        query: dict[str, Any] = {"apikey": self._api_key, "cmd": cmd}
        query.update({k: v for k, v in params.items() if v is not None})

        payload = await self.get_json("/api/v2", params=query)
        if not isinstance(payload, dict):
            raise IntegrationError(self.service, f"{cmd}: unexpected response shape")

        response = payload.get("response") or {}
        if response.get("result") != "success":
            raise IntegrationError(self.service, f"{cmd}: {response.get('message') or 'error'}")
        return response.get("data")

    # -- connectivity ---------------------------------------------------------

    async def server_info(self) -> dict[str, Any]:
        data = await self.call("get_server_info")
        return data if isinstance(data, dict) else {}

    # -- libraries ------------------------------------------------------------

    async def libraries(self) -> list[dict[str, Any]]:
        data = await self.call("get_libraries")
        return list(data) if isinstance(data, list) else []

    async def library_media_info(
        self,
        section_id: int,
        *,
        start: int = 0,
        length: int = 100,
        order_column: str = "added_at",
        order_dir: str = "desc",
    ) -> dict[str, Any]:
        """The library sweep -- the only endpoint giving per-item ``last_played``,
        ``play_count``, ``file_size`` and ``added_at`` in one paginated call.

        ``last_played`` and ``play_count`` are recomputed live from the history
        database on every call, so ``refresh=true`` is *not* needed for fresh watch
        data (it only re-pulls the item list and file sizes from Plex).

        Note the invariant: ``section_type`` must never be sent without a
        ``rating_key`` -- doing so corrupts the user's own Tautulli Media Info page.
        This client never sends it.
        """
        data = await self.call(
            "get_library_media_info",
            section_id=section_id,
            start=start,
            length=length,
            order_column=order_column,
            order_dir=order_dir,
        )
        return data if isinstance(data, dict) else {}

    # -- history --------------------------------------------------------------

    async def history(
        self,
        *,
        rating_key: int | None = None,
        parent_rating_key: int | None = None,
        grandparent_rating_key: int | None = None,
        user_id: int | None = None,
        after: str | None = None,
        length: int = 100,
        start: int = 0,
        grouping: int = 0,
    ) -> dict[str, Any]:
        """Watch history.

        Note the key names: it is ``order_column``, not ``order_by``. And for TV,
        query by ``grandparent_rating_key`` -- Seerr stores the *show* rating key,
        but history rows are per-episode, so filtering on ``rating_key`` would find
        nothing and report a watched show as never played.
        """
        data = await self.call(
            "get_history",
            rating_key=rating_key,
            parent_rating_key=parent_rating_key,
            grandparent_rating_key=grandparent_rating_key,
            user_id=user_id,
            after=after,
            length=length,
            start=start,
            grouping=grouping,
        )
        return data if isinstance(data, dict) else {}

    async def children_metadata(self, rating_key: int) -> list[dict[str, Any]]:
        """The direct children of a Plex item: a show's *seasons*, a season's episodes.

        Season pruning needs the Plex rating key of each *season*, and there is no sweep
        endpoint that lists seasons -- ``get_library_media_info`` returns show-level rows
        only. So we resolve seasons with one call per show -- several shows in flight at
        once under a small bound, and only for shows that actually have a prunable
        season, to keep the call count bounded.

        Each child carries ``rating_key`` and ``media_index`` (the season number). The
        envelope nests them under ``children_list``; an item with no children returns an
        empty list, never an error.
        """
        data = await self.call("get_children_metadata", rating_key=rating_key)
        if not isinstance(data, dict):
            return []
        children = data.get("children_list")
        return list(children) if isinstance(children, list) else []

    async def _image(self, query: dict[str, Any]) -> tuple[bytes, str] | None:
        """Fetch one image through ``pms_image_proxy`` and return ``(bytes, content_type)``.

        Shared by :meth:`poster` and :meth:`art`. Does *not* go through ``call()``:
        ``pms_image_proxy`` returns raw image bytes, not the JSON envelope every other
        command uses. Returns ``None`` on any error or non-image response so the caller can
        fall back to a placeholder rather than surface a broken image.
        """
        params = {"apikey": self._api_key, "cmd": "pms_image_proxy", **query}
        try:
            response = await self._send("GET", "/api/v2", params=params)
        except IntegrationError:
            return None
        content = response.content
        ctype = response.headers.get("content-type", "image/jpeg")
        # Compare only the media type, ignoring any "; charset=" parameter, and against the
        # raster allow-list -- never a bare "image" substring, which would let image/svg+xml
        # (script-bearing) through to be relayed same-origin.
        media_type = ctype.split(";", 1)[0].strip().lower()
        if not content or media_type not in ALLOWED_IMAGE_TYPES:
            return None
        return content, media_type

    async def poster(
        self, rating_key: int, *, width: int = 300, height: int = 450
    ) -> tuple[bytes, str] | None:
        """An item's poster (the tall 2:3 cover), fetched from Plex *through* Tautulli.

        This is the current Plex artwork -- Tautulli proxies the image from the Plex server
        it monitors, holding the token so the browser never sees it. Read-only.
        """
        return await self._image({"rating_key": rating_key, "width": width, "height": height})

    async def art(
        self, rating_key: int, *, width: int = 1280, height: int = 720
    ) -> tuple[bytes, str] | None:
        """An item's background art (the wide 16:9 backdrop), if Plex has one.

        Requested by its image path rather than a rating key, because that is how
        ``pms_image_proxy`` addresses the ``art`` (fanart) resource as opposed to the
        default poster. Falls back to ``None`` -- and the caller to the poster or a
        placeholder -- for the many items that have no separate backdrop.
        """
        return await self._image(
            {
                "img": f"/library/metadata/{rating_key}/art",
                "width": width,
                "height": height,
            }
        )

    async def metadata(self, rating_key: int) -> dict[str, Any]:
        """Item metadata, including ``guids`` -- the join key to Sonarr/Radarr.

        ``guids`` (``["imdb://tt...", "tmdb://...", "tvdb://..."]``) is populated
        only for libraries using the *new* Plex agents. Legacy agents return an
        empty list, and the external id must be parsed out of the legacy ``guid``
        string instead.
        """
        data = await self.call("get_metadata", rating_key=rating_key)
        return data if isinstance(data, dict) else {}

    # -- safety ---------------------------------------------------------------

    async def activity(self) -> dict[str, Any]:
        """What is playing right now.

        Re-polled immediately before every single delete: the veto set is
        the union of rating_key, parent_rating_key and grandparent_rating_key across all
        sessions. No shipping competitor does this.
        """
        data = await self.call("get_activity")
        return data if isinstance(data, dict) else {}
