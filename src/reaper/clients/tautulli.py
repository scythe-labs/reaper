# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tautulli, the watch-history source.

Two properties of this API shape the design here.

Its key has full admin rights, and its destructive commands are GETs. The API is a
single dispatcher, ``GET /api/v2?apikey=...&cmd=...``, so ``cmd=delete_library``,
``cmd=delete_history`` and ``cmd=restart`` all arrive as ordinary GETs.
``GuardedTransport`` only filters on HTTP method, so it would let every one of those
through. This client enforces a command allow-list instead: a command not on the
list never becomes a request. Reaper only ever reads from Tautulli.

Its API key travels in the query string, so a logged URL is a logged credential.
That is why ``reaper.logging.redact_secrets`` scrubs query strings, not just header
values.

Response envelope::

    {"response": {"result": "success"|"error", "message": ..., "data": ...}}
"""

from __future__ import annotations

from typing import Any, ClassVar, Final

import structlog

from reaper.clients.base import BaseClient, IntegrationError, SafetyViolationError
from reaper.config import RuntimeSafety

log = structlog.get_logger(__name__)

# Every command Reaper is allowed to issue. All of them are read-only.
#
# This is an allow-list rather than a deny-list, so a new Tautulli release that adds
# a destructive command is safe by default. A deny-list would let a new command
# through until someone noticed and blocked it.
READ_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "get_activity",  # active streams, checked right before every delete
        "get_history",
        "get_libraries",
        "get_libraries_table",
        "get_library_names",  # the section list, read from Tautulli's own table
        "get_library_media_info",  # the sweep (last_played, play_count, file_size, added_at)
        "get_metadata",
        "get_children_metadata",
        "get_item_watch_time_stats",
        "get_item_user_stats",
        "get_user",
        "get_users",  # includes keep_history, which the scan must know about
        "get_server_info",
        "get_server_identity",
        "pms_image_proxy",  # fetch Plex artwork through Tautulli (read-only)
        "status",
    }
)

# Poster art must be a raster image, nothing else. ``pms_image_proxy`` relays whatever
# Plex stored, and those bytes are served back same-origin from ``/api/poster``. A
# check for just the substring "image" would admit ``image/svg+xml``, and an SVG
# opened directly renders same-origin and can execute an embedded script. So this
# uses an explicit raster allow-list and rejects everything else, SVG included,
# rather than trusting the upstream Content-Type.
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

    async def call(self, cmd: str, *, read_timeout: float | None = None, **params: Any) -> Any:
        """Issue a read command and unwrap the response envelope.

        ``read_timeout`` widens the read budget for this one call (see
        ``BaseClient._request``). Every other command keeps the client's shared
        budget: the history sweep asks for tens of thousands of rows, while the
        artwork proxy on the same client is answering a browser waiting on a page.
        """
        if cmd not in READ_COMMANDS:
            # This is a programming error, not an IntegrationError, so the request
            # must never be built. Tautulli's key can delete libraries and restart
            # the service, and its destructive commands are GETs, so
            # GuardedTransport's HTTP-method guard cannot catch them.
            raise SafetyViolationError("error.integration.tautulli_write_refused", cmd=cmd)

        query: dict[str, Any] = {"apikey": self._api_key, "cmd": cmd}
        query.update({k: v for k, v in params.items() if v is not None})

        payload = await self.get_json("/api/v2", params=query, read_timeout=read_timeout)
        if not isinstance(payload, dict):
            raise IntegrationError(self.service, "error.integration.unexpected_shape", path=cmd)

        response = payload.get("response") or {}
        if response.get("result") != "success":
            raise IntegrationError(
                self.service,
                "error.integration.tautulli_command_failed",
                detail=str(response.get("message") or "error"),
            )
        return response.get("data")

    # -- connectivity ---------------------------------------------------------

    async def server_info(self) -> dict[str, Any]:
        data = await self.call("get_server_info")
        return data if isinstance(data, dict) else {}

    # -- users ----------------------------------------------------------------

    async def users(self) -> list[dict[str, Any]]:
        """Users, including ``keep_history``.

        A user with history recording turned off is invisible in the history table.
        They look exactly like someone who never watches anything, so everything only
        they watch reads as never played. The scan checks this
        (``scan_runner._keep_history_degradations``) and degrades the snapshot while
        any active user has recording off, because "nobody watched it" cannot be
        trusted in that case.
        """
        data = await self.call("get_users")
        return list(data) if isinstance(data, list) else []

    # -- libraries ------------------------------------------------------------

    async def libraries(self) -> list[dict[str, Any]]:
        """Every library section: its id, its name and its type.

        ``get_library_names`` answers from Tautulli's own table in one local query.
        ``get_libraries`` returns the same three fields plus item counts, and it pays for
        those counts with a live Plex call per section, three for a show or artist
        section. Nothing in Reaper reads a count. The scan that follows a reap runs while
        Plex is still rescanning the paths the reap emptied, so those calls are slowest
        exactly when the scan needs them, and the read budget expired instead.

        The table keeps a row for a library Plex no longer serves, and a synthetic Live TV
        row, so this list is a superset of what Plex holds now. Each extra section answers
        the sweep with no rows, so it costs one empty page and adds nothing to the index.
        The table is unique on server and section together, and the answer does not say
        which server a row came from, so ``services.library_index`` walks one section id
        once however many rows carry it.
        """
        data = await self.call("get_library_names")
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
        """The library sweep. This is the only endpoint that returns per-item
        ``last_played``, ``play_count``, ``file_size`` and ``added_at`` in one
        paginated call.

        ``last_played`` and ``play_count`` are recomputed live from the history
        database on every call, so ``refresh=true`` is not needed for fresh watch
        data. That flag only re-pulls the item list and file sizes from Plex.

        Never send ``section_type`` without a ``rating_key``: doing so corrupts the
        owner's own Tautulli Media Info page. This client never sends it.
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
        include_activity: int | None = None,
        read_timeout: float | None = None,
    ) -> dict[str, Any]:
        """Watch history.

        The parameter name is ``order_column``, not ``order_by``. For TV, query by
        ``grandparent_rating_key``. Seerr stores the show's own rating key, but
        history rows are per episode, so filtering on ``rating_key`` would find
        nothing and report a watched show as never played.

        ``include_activity=0`` leaves plays in progress out of the listing. Otherwise
        Tautulli appends its temporary session table to the history rows (the
        operator's own setting decides this when the caller does not), and a session
        row with no start time sorts last and fails every page it lands on with HTTP
        500 (``history_sync.MAX_UNSERVABLE_ROWS``). The mirror walk always passes 0.
        It drops any row with no ``row_id`` anyway, and ``get_activity`` already
        answers "what is playing right now." ``None`` sends nothing and leaves the
        choice to Tautulli.

        ``read_timeout`` is the caller's own read budget for one page. The mirror
        sweep sets it because it asks for tens of thousands of rows at a time
        (``history_sync.PAGE_READ_TIMEOUT``). A per-item lookup passes nothing and
        keeps the client's shared budget.
        """
        data = await self.call(
            "get_history",
            read_timeout=read_timeout,
            rating_key=rating_key,
            parent_rating_key=parent_rating_key,
            grandparent_rating_key=grandparent_rating_key,
            user_id=user_id,
            after=after,
            length=length,
            start=start,
            grouping=grouping,
            include_activity=include_activity,
        )
        return data if isinstance(data, dict) else {}

    async def children_metadata(self, rating_key: int) -> list[dict[str, Any]]:
        """The direct children of a Plex item, such as a show's seasons or a season's
        episodes.

        Season pruning needs the Plex rating key of each season, and there is no
        sweep endpoint that lists seasons. ``get_library_media_info`` returns
        show-level rows only, so this resolves seasons with one call per show,
        several shows in flight at once under a small bound, and only for shows that
        actually have a prunable season. That keeps the call count bounded.

        Each child carries ``rating_key`` and ``media_index`` (the season number).
        The envelope nests them under ``children_list``. An item with no children
        returns an empty list, never an error.
        """
        data = await self.call("get_children_metadata", rating_key=rating_key)
        if not isinstance(data, dict):
            return []
        children = data.get("children_list")
        return list(children) if isinstance(children, list) else []

    async def _image(self, query: dict[str, Any]) -> tuple[bytes, str] | None:
        """Fetch one image through ``pms_image_proxy`` and return ``(bytes, content_type)``.

        Shared by :meth:`poster` and :meth:`art`. This bypasses ``call()``, because
        ``pms_image_proxy`` returns raw image bytes, not the JSON envelope every other
        command uses. Returns ``None`` on any error or non-image response, so the
        caller can fall back to a placeholder instead of showing a broken image.
        """
        params = {"apikey": self._api_key, "cmd": "pms_image_proxy", **query}
        try:
            response = await self._send("GET", "/api/v2", params=params)
        except IntegrationError as exc:
            # Logged, because a placeholder in the queue otherwise looks exactly
            # like an item that genuinely has no art.
            log.warning("artwork.fetch_failed", error=str(exc))
            return None
        content = response.content
        ctype = response.headers.get("content-type", "image/jpeg")
        # Compare only the media type, ignoring any "; charset=" parameter, against
        # the raster allow-list. A bare "image" substring check would let
        # image/svg+xml through, which can carry a script and would be relayed
        # same-origin.
        media_type = ctype.split(";", 1)[0].strip().lower()
        if not content or media_type not in ALLOWED_IMAGE_TYPES:
            log.warning("artwork.not_an_image", media_type=media_type, bytes=len(content))
            return None
        return content, media_type

    async def poster(
        self, rating_key: int, *, width: int = 300, height: int = 450
    ) -> tuple[bytes, str] | None:
        """An item's poster (the tall 2:3 cover), fetched from Plex through Tautulli.

        This is the current Plex artwork. Tautulli proxies the image from the Plex
        server it monitors and holds the token itself, so the browser never sees it.
        Read-only.
        """
        return await self._image({"rating_key": rating_key, "width": width, "height": height})

    async def art(
        self, rating_key: int, *, width: int = 1280, height: int = 720
    ) -> tuple[bytes, str] | None:
        """An item's background art (the wide 16:9 backdrop), when Plex has one.

        Requested by its image path rather than a rating key, because that is how
        ``pms_image_proxy`` addresses the ``art`` (fanart) resource instead of the
        default poster. Returns ``None`` for the many items with no separate
        backdrop, and the caller falls back to the poster or a placeholder.
        """
        return await self._image(
            {
                "img": f"/library/metadata/{rating_key}/art",
                "width": width,
                "height": height,
            }
        )

    async def metadata(self, rating_key: int) -> dict[str, Any]:
        """Item metadata, including ``guids``, the join key to Sonarr/Radarr.

        ``guids`` (``["imdb://tt...", "tmdb://...", "tvdb://..."]``) is populated
        only for libraries using the newer Plex agents. Legacy agents return an
        empty list, and the external id must be parsed out of the legacy ``guid``
        string instead.
        """
        data = await self.call("get_metadata", rating_key=rating_key)
        return data if isinstance(data, dict) else {}

    # -- safety ---------------------------------------------------------------

    async def activity(self) -> dict[str, Any]:
        """What is playing right now.

        Checked again immediately before every single delete. The veto set is the
        union of rating_key, parent_rating_key and grandparent_rating_key across
        every active session. No other tool in this space does this check.
        """
        data = await self.call("get_activity")
        return data if isinstance(data, dict) else {}
