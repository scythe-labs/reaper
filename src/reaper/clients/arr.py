# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sonarr and Radarr.

They are close cousins but differ in exactly the places that delete files, and
each **silently ignores the other's parameter and returns 200**. Getting this
wrong does not raise -- it just fails to add the exclusion, and the *arr
re-downloads what you just deleted:

    Radarr   DELETE /api/v3/movie/{id}?deleteFiles=&addImportExclusion=
             exclusions at GET/POST /api/v3/exclusions
    Sonarr   DELETE /api/v3/series/{id}?deleteFiles=&addImportListExclusion=
             exclusions at GET/POST /api/v3/importlistexclusion

Both are confirmed from the projects' own OpenAPI specs. The names live in the
subclasses so a call site cannot pick the wrong one, and after any
delete-with-exclusion we re-read the exclusion list and assert the id is present
-- because a 200 here means nothing.

The API path prefix is discovered from ``system/status``, never hardcoded:
Sonarr's v5-develop branch ships a real ``/api/v5`` with a structurally different
SeriesResource.
"""

from __future__ import annotations

from typing import Any, ClassVar

import structlog

from reaper.clients.base import BaseClient, IntegrationError
from reaper.config import RuntimeSafety

log = structlog.get_logger(__name__)


class ArrClient(BaseClient):
    """Shared Sonarr/Radarr behavior."""

    service: ClassVar[str] = "arr"
    default_prefix: ClassVar[str] = "/api/v3"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        safety: RuntimeSafety,
        api_path_prefix: str | None = None,
        verify: bool = True,
    ) -> None:
        super().__init__(
            base_url,
            safety=safety,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            verify=verify,
        )
        self.prefix = api_path_prefix or self.default_prefix

    async def system_status(self) -> dict[str, Any]:
        """Connectivity + version check.

        Reaching it proves the URL, the key and the API path all work, and it reports the
        version Reaper shows beside the instance. It does NOT gate the API path: the path
        comes from the instance's stored ``api_path_prefix``, which nothing derives from
        this response (rule 24 -- the comment used to claim a gate that is not here).
        """
        data = await self.get_json(f"{self.prefix}/system/status")
        if not isinstance(data, dict):
            raise IntegrationError(self.service, "system/status did not return an object")
        return data

    async def tags(self) -> list[dict[str, Any]]:
        """Tags. A `reaper-keep` tag is a zero-integration whitelist: the owner
        applies it in a UI they already use."""
        data = await self.get_json(f"{self.prefix}/tag")
        if not isinstance(data, list):
            # A 200 whose body is not a list (a reverse proxy's HTML error page, a
            # schema change) is never "no tags exist". Masking it as [] would let a
            # keep-tag sync read an empty whitelist out of an error page -- and
            # atomically replace a populated one with nothing.
            raise IntegrationError(self.service, f"{self.prefix}/tag did not return a list")
        return list(data)

    async def root_folders(self) -> list[dict[str, Any]]:
        """Root folders, including `accessible`.

        Two consumers, and neither is a scan-time preflight:

        * `identity.root_folder_paths` takes the `path` of each, so the folder
          corroborator can measure a path below the instance's real root instead of
          guessing where a container mount ends. It ignores `accessible`.
        * `executor._mount_is_up` reads `accessible` before the post-reap trash purge,
          because an unmounted volume makes media look vanished and a purge would then
          destroy library records.
        """
        data = await self.get_json(f"{self.prefix}/rootfolder")
        if not isinstance(data, list):
            # A 200 whose body is not a list is never "there are none of these".
            # See ``tags`` for the reasoning; this is the same defect (rule 72).
            raise IntegrationError(self.service, f"{self.prefix}/rootfolder did not return a list")
        return list(data)


class SonarrClient(ArrClient):
    service: ClassVar[str] = "sonarr"

    # Sonarr's spelling. Radarr's differs, and each ignores the other's silently.
    exclusion_param: ClassVar[str] = "addImportListExclusion"
    exclusion_path: ClassVar[str] = "/importlistexclusion"

    async def series(self) -> list[dict[str, Any]]:
        data = await self.get_json(f"{self.prefix}/series")
        if not isinstance(data, list):
            # A 200 whose body is not a list is never "there are none of these".
            # See ``tags`` for the reasoning; this is the same defect (rule 72).
            raise IntegrationError(self.service, f"{self.prefix}/series did not return a list")
        return list(data)

    async def series_by_id(self, series_id: int) -> dict[str, Any]:
        data = await self.get_json(f"{self.prefix}/series/{series_id}")
        if not isinstance(data, dict):
            raise IntegrationError(self.service, f"series/{series_id} did not return an object")
        return data

    async def episode_files(self, series_id: int) -> list[dict[str, Any]]:
        """Episode files for a series -- the unit of deletion for season pruning.

        There is no "delete season" endpoint. Pruning is:
            POST /seasonpass  (unmonitor)
              -> GET /series/{id}, ASSERT seasons[n].monitored is False
              -> DELETE /episodefile/bulk

        The order is not arbitrary. The two half-applied states are asymmetric:
        "unmonitored, files intact" is benign and resumable, while "files gone,
        still monitored" makes the *arr re-download everything we just removed.
        """
        data = await self.get_json(f"{self.prefix}/episodefile", params={"seriesId": series_id})
        if not isinstance(data, list):
            # A 200 whose body is not a list is never "there are none of these".
            # See ``tags`` for the reasoning; this is the same defect (rule 72).
            raise IntegrationError(self.service, f"{self.prefix}/episodefile did not return a list")
        return list(data)

    async def episodes(self, series_id: int) -> list[dict[str, Any]]:
        data = await self.get_json(f"{self.prefix}/episode", params={"seriesId": series_id})
        if not isinstance(data, list):
            # A 200 whose body is not a list is never "there are none of these".
            # See ``tags`` for the reasoning; this is the same defect (rule 72).
            raise IntegrationError(self.service, f"{self.prefix}/episode did not return a list")
        return list(data)

    async def exclusions(self) -> list[dict[str, Any]]:
        data = await self.get_json(f"{self.prefix}{self.exclusion_path}")
        if not isinstance(data, list):
            # A 200 whose body is not a list is never "there are none of these".
            # See ``tags`` for the reasoning; this is the same defect (rule 72).
            raise IntegrationError(
                self.service, f"{self.prefix}{self.exclusion_path} did not return a list"
            )
        return list(data)

    async def unmonitor_season(self, series_id: int, season_number: int) -> None:
        """Stop monitoring one season, via the season-pass edit. Reversible.

        This is the FIRST step of a season prune and the safe one: unmonitoring touches
        no files. It must be *verified* (re-read the series, assert the season is no longer
        monitored) before any file is deleted, because Sonarr returns 200 for a season-pass
        edit whether or not it took -- and 'files gone, still monitored' makes Sonarr
        re-download everything just removed.
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

        The LAST step of a season prune, reached only after the unmonitor is verified. The
        ids are resolved live from :meth:`episode_files` immediately before this call --
        never frozen at plan time -- so a file Sonarr grabbed between plan and run is
        included and a file already gone is not re-requested. An empty list is a no-op:
        there is nothing to delete, and sending an empty bulk delete is meaningless.
        """
        if not episode_file_ids:
            return
        await self._mutate(
            "DELETE",
            f"{self.prefix}/episodefile/bulk",
            json={"episodeFileIds": episode_file_ids},
        )


class RadarrClient(ArrClient):
    service: ClassVar[str] = "radarr"

    # Radarr's spelling. Note both differ from Sonarr's.
    exclusion_param: ClassVar[str] = "addImportExclusion"
    exclusion_path: ClassVar[str] = "/exclusions"

    async def movies(self) -> list[dict[str, Any]]:
        """Every movie, with `ratings` already attached.

        Radarr returns a real ratings object -- imdb, tmdb, metacritic,
        rottenTomatoes, trakt -- so movie ratings cost us no extra call and no
        extra API key. (Sonarr does not: its ratings are flat TVDB.)
        """
        data = await self.get_json(f"{self.prefix}/movie")
        if not isinstance(data, list):
            # A 200 whose body is not a list is never "this instance holds no movies".
            # Coerced to [], an auth proxy's JSON error page read as an empty library:
            # every movie on that Radarr silently left the scan, and the snapshot stayed
            # executable, so the operator was told a complete run over a partial library
            # (rules 28 and 93). See ``tags`` -- same defect, same answer (rule 72).
            raise IntegrationError(self.service, f"{self.prefix}/movie did not return a list")
        return list(data)

    async def movie_by_id(self, movie_id: int) -> dict[str, Any]:
        data = await self.get_json(f"{self.prefix}/movie/{movie_id}")
        if not isinstance(data, dict):
            raise IntegrationError(self.service, f"movie/{movie_id} did not return an object")
        return data

    async def exclusions(self) -> list[dict[str, Any]]:
        data = await self.get_json(f"{self.prefix}{self.exclusion_path}")
        if not isinstance(data, list):
            # A 200 whose body is not a list is never "there are none of these".
            # See ``tags`` for the reasoning; this is the same defect (rule 72).
            raise IntegrationError(
                self.service, f"{self.prefix}{self.exclusion_path} did not return a list"
            )
        return list(data)

    async def delete_movie(
        self, movie_id: int, *, delete_files: bool = True, add_exclusion: bool = True
    ) -> None:
        """Remove a movie, its files, and (by default) add an import exclusion.

        This is the destructive call. It goes through :meth:`_mutate`, so the transport
        guard refuses it unless deletion is enabled on the host AND the executor declared
        the intent -- there is no unguarded path to it.

        ``addImportExclusion`` is Radarr's spelling; Sonarr's differs and each silently
        ignores the other's, so it lives on the subclass as ``exclusion_param`` and the
        caller cannot pick the wrong one. **A 200 here does not prove the exclusion
        landed** -- the executor re-reads :meth:`exclusions` and asserts the tmdbId is
        present, because that footgun returns 200 while doing nothing.
        """
        await self._mutate(
            "DELETE",
            f"{self.prefix}/movie/{movie_id}",
            params={"deleteFiles": delete_files, self.exclusion_param: add_exclusion},
        )
