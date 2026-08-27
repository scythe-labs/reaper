# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetching public, unauthenticated resources: dataset mirrors and curated lists.

These are plain GETs of public data, but they still belong in ``clients/``, the one
place HTTP lives, so they use the same retry, timeout, error-mapping and redirect
code as every other client instead of a separate copy in the services layer.

One difference from the credentialed clients: cross-origin redirects are followed
here. These requests carry no API key or token, so there is nothing for a redirect to
carry away, and public mirrors genuinely do bounce to CDNs.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import ClassVar

import httpx2

from reaper.clients.base import (
    _REDIRECTS,
    BaseClient,
    IntegrationError,
    http_failure,
    transient_retry,
    transport_failure,
)
from reaper.config import RuntimeSafety

#: Streamed-download chunk size. Large enough that the 280 MB IMDb dataset is a few
#: thousand writes, small enough to keep memory flat.
_CHUNK = 1 << 16


class PublicClient(BaseClient):
    """A read-only client for one public origin. No credentials, GETs only.

    Constructed with a fresh, default :class:`RuntimeSafety`. The guard only gates
    mutations, and this client never sends one, so a caller does not need to pass
    safety state into code that can never write anything.
    """

    service: ClassVar[str] = "public-fetch"

    def __init__(self, base_url: str, *, timeout: httpx2.Timeout | None = None) -> None:
        super().__init__(
            base_url,
            safety=RuntimeSafety(),
            timeout=timeout,
            allow_cross_origin_redirects=True,
        )

    async def stream_to(self, path: str, destination: Path) -> None:
        """Stream one GET body to ``destination``, following a few redirects.

        The redirect loop is manual for the same reason as :meth:`_send`'s: the client
        never auto-follows a redirect. Unlike :meth:`_send`, a cross-origin hop is fine
        here, because these requests carry no credentials. Errors map to
        :class:`IntegrationError` exactly like every other client call. The caller owns
        the temp-file-and-rename logic.

        Retried on a transient transport failure like every other read
        (:meth:`_stream_once` holds the retry policy), so a single blip partway through
        a large download does not abort the whole transfer.
        """
        try:
            await self._stream_once(path, destination)
        except httpx2.TransportError as exc:
            raise transport_failure(self.service, exc) from exc

    @transient_retry
    async def _stream_once(self, path: str, destination: Path) -> None:
        """One whole attempt at the streamed download. Transport errors are left unmapped.

        This follows the same split as :meth:`_request` and :meth:`_send`: the retry
        predicate matches raw ``httpx2`` errors, so mapping them here would keep the
        backoff from ever firing. :meth:`stream_to` maps whatever error survives every
        attempt.

        Each attempt restarts the download from the beginning. ``destination`` is
        reopened ``"wb"`` every time, so a half-written body is replaced rather than
        appended to. That matters more than the wasted bytes: a resumed transfer glued
        onto a partial one would parse as a valid dataset with the wrong contents, and
        nothing downstream would notice.
        """
        # This streamed download bypasses `_send`, so it traces itself here. `path` is
        # what gets logged, never the post-redirect target, matching every other
        # `client.call` line.
        started = time.monotonic()
        status: int | None = None
        try:
            target = path
            for _ in range(4):  # the request itself, plus at most three redirects
                async with self._client.stream("GET", target) as response:
                    status = response.status_code
                    if response.status_code in _REDIRECTS:
                        location = response.headers.get("location")
                        if not location:
                            raise IntegrationError(
                                self.service,
                                "error.integration.redirect_missing_location",
                                status=response.status_code,
                                path=path,
                            )
                        target = str(response.request.url.join(location))
                        continue
                    if response.status_code >= 400:
                        raise http_failure(self.service, response, "GET", path)
                    with destination.open("wb") as handle:
                        async for chunk in response.aiter_bytes(_CHUNK):
                            handle.write(chunk)
                    return
            raise IntegrationError(
                self.service, "error.integration.too_many_redirects", method="GET", path=path
            )
        finally:
            self._trace("GET", path, status, started)
