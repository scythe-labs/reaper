# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sonarr and Radarr.

They are close cousins, but they differ in exactly the places that delete files.
Each one silently ignores the other's parameter and still returns 200. Getting this
wrong does not raise an error. It just fails to add the exclusion, and the *arr
re-downloads the file you just deleted:

    Radarr   DELETE /api/v3/movie/{id}?deleteFiles=&addImportExclusion=
             exclusions at GET/POST /api/v3/exclusions
    Sonarr   DELETE /api/v3/series/{id}?deleteFiles=&addImportListExclusion=
             exclusions at GET/POST /api/v3/importlistexclusion

These names come from the projects' own OpenAPI specs. Each subclass holds its own
spelling, so a call site cannot pick the wrong one. After any delete-with-exclusion,
this client re-reads the exclusion list and confirms the id is present, because a
200 response does not prove the exclusion was added.

The API path prefix comes from ``system/status`` rather than a hardcoded value:
Sonarr's v5-develop branch ships a real ``/api/v5`` with a structurally different
SeriesResource.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any, ClassVar

import httpx2

from reaper.clients.base import BaseClient, IntegrationError
from reaper.config import RuntimeSafety

#: Sonarr and Radarr serve one media management settings object, always as id 1.
_MEDIA_MANAGEMENT_ID = 1

#: Read limit for one file delete. It stops an instance that answers pings but never
#: finishes from holding a reap forever. Loss of contact ends a delete much sooner.
_DELETE_CEILING = 1800.0
#: Seconds between liveness pings while a delete runs.
_PING_EVERY = 10.0
#: Read limit for one liveness ping.
_PING_TIMEOUT = 10.0
#: Consecutive missed pings that end a delete.
_MISSES_ALLOWED = 3


class ArrClient(BaseClient):
    """Shared Sonarr/Radarr behavior.

    Every read below goes through ``get_list`` or ``get_dict``, which raise on a body of
    the wrong shape instead of coercing it to something empty. See ``get_list``'s
    docstring for why.
    """

    service: ClassVar[str] = "arr"
    default_prefix: ClassVar[str] = "/api/v3"

    # Declared here but never assigned a value. This gives ``exclusions`` below a type
    # without picking a default: each subclass assigns its own spelling, so a bare
    # ``ArrClient`` can still call the method, but it raises ``AttributeError`` instead
    # of silently using the wrong *arr's spelling.
    exclusion_param: ClassVar[str]
    exclusion_path: ClassVar[str]

    async def _ping(self) -> bool:
        """One liveness check. Any answer other than a 2xx is a miss. Never retried."""
        started = time.monotonic()
        status: int | None = None
        try:
            response = await self._client.request(
                "GET",
                f"{self.prefix}/system/status",
                timeout=httpx2.Timeout(_PING_TIMEOUT),
            )
            status = response.status_code
            return response.is_success
        except httpx2.HTTPError:
            return False
        finally:
            self._trace("GET", f"{self.prefix}/system/status", status, started)

    async def _watched_delete(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> None:
        """Send a file delete and wait while the instance still answers pings.

        The delete may run longer than any fixed read limit, because the instance
        answers only after every file is gone. A ping goes out every ``_PING_EVERY``
        seconds. After ``_MISSES_ALLOWED`` misses in a row, the delete is canceled and
        this raises a timeout with no status, which the executor treats as "no answer"
        and re-reads. The delete's own error is raised unchanged. A cancel of the
        caller cancels the delete and sends nothing more.
        """
        task = asyncio.ensure_future(
            self._mutate("DELETE", path, params=params, json=json, read_timeout=_DELETE_CEILING)
        )
        misses = 0
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=_PING_EVERY)
                if done:
                    task.result()
                    return
                misses = 0 if await self._ping() else misses + 1
                if misses >= _MISSES_ALLOWED:
                    raise IntegrationError(
                        self.service, "error.integration.timed_out", read_timed_out=True
                    )
        finally:
            if not task.done():
                task.cancel()
                await asyncio.wait({task})

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        safety: RuntimeSafety,
        api_path_prefix: str | None = None,
        verify: bool = True,
    ) -> None:
        prefix = api_path_prefix or self.default_prefix
        settings_path = f"{prefix}/config/mediamanagement/{_MEDIA_MANAGEMENT_ID}"
        super().__init__(
            base_url,
            safety=safety,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            verify=verify,
            restore_paths=frozenset({httpx2.URL(base_url.rstrip("/") + settings_path).path}),
        )
        self.prefix = prefix
        self._settings_path = settings_path

    async def system_status(self) -> dict[str, Any]:
        """Check connectivity and read the version.

        A successful call proves the URL, the key and the API path all work, and it
        returns the version Reaper shows beside the instance. This does not set the API
        path: that comes from the instance's stored ``api_path_prefix``, and nothing
        derives it from this response.
        """
        return await self.get_dict(f"{self.prefix}/system/status")

    async def tags(self) -> list[dict[str, Any]]:
        """Tags. A ``reaper-keep`` tag works as a keep-list with no extra setup: the
        owner applies it in the Sonarr or Radarr UI they already use.

        Raising on a malformed response, rather than returning an empty list, stops a
        keep-tag sync from reading an error page as an empty keep-list and wiping out a
        real one.
        """
        return await self.get_list(f"{self.prefix}/tag")

    async def root_folders(self) -> list[dict[str, Any]]:
        """Root folders, including ``accessible``.

        Two things read this:

        * ``identity.root_folder_paths`` uses each folder's ``path`` so the folder
          corroborator can measure a path below the instance's real root, instead of
          guessing where a container mount ends. It ignores ``accessible``.
        * ``executor._mount_is_up`` reads ``accessible`` before the post-reap trash
          purge. An unmounted volume makes media look like it vanished, and a purge
          would then destroy library records.
        """
        return await self.get_list(f"{self.prefix}/rootfolder")

    async def media_management(self) -> dict[str, Any]:
        """The media management settings, which hold the recycle bin's folder
        (``recycleBin``, empty when there is no bin) and how many days it keeps a file
        (``recycleBinCleanupDays``)."""
        return await self.get_dict(f"{self.prefix}/config/mediamanagement")

    async def turn_off_recycle_bin(self, config: dict[str, Any]) -> None:
        """Save the media management settings with no recycle bin.

        Sonarr and Radarr save every field of the body, so ``config`` must be the whole
        object :meth:`media_management` just returned."""
        await self._mutate("PUT", self._settings_path, json={**config, "recycleBin": ""})

    async def restore_recycle_bin(self, config: dict[str, Any], path: str) -> None:
        """Save the media management settings with the recycle bin at ``path``.

        The transport guard lets this through with deletion off, so it refuses an empty
        ``path``: that would turn a bin off."""
        if not path:
            raise ValueError("a restored recycle bin needs a folder")
        await self._mutate(
            "PUT", self._settings_path, json={**config, "recycleBin": path}, restore=True
        )

    async def exclusions(self) -> list[dict[str, Any]]:
        """The import exclusions this *arr holds, at its own spelling of the path.

        Read after every delete-with-exclusion, because the *arr answers 200 whether or
        not the exclusion landed. The executor re-reads this list and confirms the id is
        in it.
        """
        return await self.get_list(f"{self.prefix}{self.exclusion_path}")


class SonarrClient(ArrClient):
    service: ClassVar[str] = "sonarr"

    # Sonarr's own spelling. Radarr uses a different one, and each ignores the other's
    # parameter silently.
    exclusion_param: ClassVar[str] = "addImportListExclusion"
    exclusion_path: ClassVar[str] = "/importlistexclusion"

    async def series(self) -> list[dict[str, Any]]:
        return await self.get_list(f"{self.prefix}/series")

    async def series_by_id(self, series_id: int) -> dict[str, Any]:
        return await self.get_dict(f"{self.prefix}/series/{series_id}")

    async def episode_files(self, series_id: int) -> list[dict[str, Any]]:
        """Episode files for a series. This is the unit of deletion for season pruning.

        There is no "delete season" endpoint. Pruning a season is three steps:
            POST /seasonpass  (unmonitor)
              -> GET /series/{id}, confirm seasons[n].monitored is False
              -> DELETE /episodefile/bulk

        The order matters. The two half-applied states are not equally safe:
        "unmonitored, files intact" is safe and can resume later, while "files gone,
        still monitored" makes Sonarr re-download everything just removed.
        """
        return await self.get_list(f"{self.prefix}/episodefile", params={"seriesId": series_id})

    async def episodes(self, series_id: int) -> list[dict[str, Any]]:
        return await self.get_list(f"{self.prefix}/episode", params={"seriesId": series_id})

    async def unmonitor_season(self, series_id: int, season_number: int) -> None:
        """Stop monitoring one season, through the season-pass edit. Reversible.

        This is the first, safe step of a season prune: unmonitoring touches no files.
        It must be verified, by re-reading the series and confirming the season is no
        longer monitored, before any file is deleted. Sonarr returns 200 for a
        season-pass edit whether or not it actually took effect, and "files gone, still
        monitored" makes Sonarr re-download everything just removed.
        """
        await self._mutate(
            "POST",
            f"{self.prefix}/seasonpass",
            json={
                "series": [
                    {
                        "id": series_id,
                        "seasons": [{"seasonNumber": season_number, "monitored": False}],
                    }
                ],
                "monitoringOptions": {"monitor": "none"},
            },
        )

    async def delete_episode_files(self, episode_file_ids: list[int]) -> None:
        """Delete a specific set of episode files in one bulk call.

        This is the last step of a season prune, reached only after the unmonitor is
        verified. The ids come from a live call to :meth:`episode_files` made right
        before this one, never from ids frozen at plan time, so a file Sonarr added
        between plan and run is included, and a file already gone is not requested
        again. An empty list is a no-op, because there is nothing to delete.
        """
        if not episode_file_ids:
            return
        await self._watched_delete(
            f"{self.prefix}/episodefile/bulk", json={"episodeFileIds": episode_file_ids}
        )


class RadarrClient(ArrClient):
    service: ClassVar[str] = "radarr"

    # Radarr's own spelling. Both fields differ from Sonarr's.
    exclusion_param: ClassVar[str] = "addImportExclusion"
    exclusion_path: ClassVar[str] = "/exclusions"

    async def movies(self, *, tmdb_id: int | None = None) -> list[dict[str, Any]]:
        """Every movie, with ``ratings`` already attached.

        Radarr returns a full ratings object (imdb, tmdb, metacritic, rottenTomatoes,
        trakt), so movie ratings cost no extra call and no extra API key. Sonarr does
        not: its ratings are flat TVDB only.

        With ``tmdb_id``, only that movie, or an empty list once it is deleted.

        This is the read that first showed why ``get_list`` must raise instead of
        returning an empty list: an auth proxy's error page, read as an empty list,
        once looked like an empty library and silently dropped every movie from the
        scan.
        """
        params = None if tmdb_id is None else {"tmdbId": tmdb_id}
        return await self.get_list(f"{self.prefix}/movie", params=params)

    async def movie_by_id(self, movie_id: int) -> dict[str, Any]:
        return await self.get_dict(f"{self.prefix}/movie/{movie_id}")

    async def delete_movie(
        self, movie_id: int, *, delete_files: bool = True, add_exclusion: bool = True
    ) -> None:
        """Remove a movie, its files, and, by default, add an import exclusion.

        This is the destructive call. It goes through :meth:`_watched_delete`, so
        :class:`~reaper.clients.base.GuardedTransport` refuses it unless deletion is
        enabled on the host and the executor has declared the intent. There is no path
        to this call that skips the guard.

        ``addImportExclusion`` is Radarr's spelling. Sonarr's differs, and each
        silently ignores the other's, so it lives on the subclass as
        ``exclusion_param`` and the caller cannot pick the wrong one. A 200 response
        here does not prove the exclusion was added: the executor re-reads
        :meth:`exclusions` and confirms the tmdbId is present, because Radarr returns
        200 even when it silently did nothing.
        """
        await self._watched_delete(
            f"{self.prefix}/movie/{movie_id}",
            params={"deleteFiles": delete_files, self.exclusion_param: add_exclusion},
        )
