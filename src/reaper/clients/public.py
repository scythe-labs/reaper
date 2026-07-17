# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetching public, unauthenticated resources: dataset mirrors and curated lists.

These are plain GETs of public data, but they still belong in ``clients/`` -- the one
place HTTP lives -- so they ride the shared retry, timeout, error-mapping and redirect
machinery instead of growing bespoke copies in the services layer.

One deliberate difference from the credentialed clients: **cross-origin redirects are
followed here.** No API key or token rides on these requests, so there is nothing for a
redirect to carry away, and public mirrors genuinely do bounce to CDNs.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import httpx

from reaper.clients.base import _REDIRECTS, BaseClient, IntegrationError
from reaper.config import RuntimeSafety

#: Streamed-download chunk size. Large enough that the 280 MB IMDb dataset is a few
#: thousand writes, small enough to keep memory flat.
_CHUNK = 1 << 16


class PublicClient(BaseClient):
    """A read-only client for one public origin. No credentials, GETs only.

    Constructed with a fresh, default :class:`RuntimeSafety`: the guard only ever gates
    mutations and this client issues none, so callers need not thread safety state into
    code that cannot write anything anywhere.
    """

    service: ClassVar[str] = "public-fetch"

    def __init__(self, base_url: str, *, timeout: httpx.Timeout | None = None) -> None:
        super().__init__(
            base_url,
            safety=RuntimeSafety(),
            timeout=timeout,
            allow_cross_origin_redirects=True,
        )

    async def stream_to(self, path: str, destination: Path) -> None:
        """Stream one GET body to ``destination``, following a few redirects.

        The redirect loop is manual for the same reason as ``_send``'s (the client never
        auto-follows), but unlike ``_send`` a cross-origin hop is fine here: these
        requests carry no credentials. Errors map to :class:`IntegrationError` exactly
        like every other client call; the caller owns temp-file-and-rename semantics.
        """
        target = path
        for _ in range(4):  # the request itself, plus at most three redirects
            try:
                async with self._client.stream("GET", target) as response:
                    if response.status_code in _REDIRECTS:
                        location = response.headers.get("location")
                        if not location:
                            raise IntegrationError(
                                self.service,
                                f"redirect with no Location for GET {path}",
                                status=response.status_code,
                            )
                        target = str(response.request.url.join(location))
                        continue
                    if response.status_code >= 400:
                        raise IntegrationError(
                            self.service,
                            f"HTTP {response.status_code} for GET {path}",
                            status=response.status_code,
                        )
                    with destination.open("wb") as handle:
                        async for chunk in response.aiter_bytes(_CHUNK):
                            handle.write(chunk)
                    return
            except httpx.TimeoutException as exc:
                raise IntegrationError(self.service, f"timed out ({type(exc).__name__})") from exc
            except httpx.TransportError as exc:
                raise IntegrationError(self.service, f"unreachable ({exc})") from exc
        raise IntegrationError(self.service, f"too many redirects for GET {path}")
