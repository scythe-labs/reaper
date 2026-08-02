# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checking GitHub for a newer Reaper, on this build's own channel.

A release build compares its version against the newest published release; every
other build -- the ``:dev`` image, a dev binary, a source checkout -- follows the tip
of the dev branch. The answer feeds one read-only About surface.

The check informs; it never gates. A failure (no network, GitHub down, an API limit)
resolves to "unknown" and is retried after a short pause, so an air-gapped install
shows nothing rather than an error. ``REAPER_UPDATE_CHECK=false`` turns it off
entirely: no request leaves the box, and the surface says checks are off.

Which repository to ask is baked at build time as ``REAPER_UPDATE_REPO`` (CI passes
its own repository, so a fork's builds follow the fork); a source checkout falls back
to the upstream repository.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import structlog

from reaper.buildinfo import build_version, is_release, short_commit, version_number
from reaper.clients.base import IntegrationError
from reaper.clients.public import PublicClient
from reaper.clock import utcnow

log = structlog.get_logger(__name__)

_API = "https://api.github.com"
_DEV_BRANCH = "dev"
DEFAULT_REPO = "scythe-labs/reaper"

#: ``owner/name`` and nothing else: the slug is spliced into a URL path, so a value
#: that could smuggle a separator falls back to the default instead of being sent.
_REPO_SHAPE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

_FALSE = {"0", "false", "no", "off"}

#: A successful answer holds for hours -- versions change daily at most, and the
#: unauthenticated GitHub API allows 60 requests an hour for the whole host.
_SUCCESS_TTL = 6 * 3600.0
#: A failure is retried sooner, but not per request: one unreachable check must not
#: turn every About view into a fresh network attempt.
_FAILURE_TTL = 15 * 60.0

Channel = Literal["release", "dev"]


@dataclass(frozen=True)
class UpdateStatus:
    """One answer, complete enough to render without consulting anything else.

    ``update_available`` is three-state on purpose: ``None`` means the check could not
    answer (disabled, unreachable, or an unparseable version), which the surface shows
    as nothing rather than as either verdict.
    """

    channel: Channel
    enabled: bool
    current: str
    latest: str | None = None
    update_available: bool | None = None
    url: str | None = None
    checked_at: datetime | None = None


def _channel() -> Channel:
    return "release" if is_release() else "dev"


def _enabled() -> bool:
    return os.environ.get("REAPER_UPDATE_CHECK", "").strip().lower() not in _FALSE


def _repo() -> str:
    configured = os.environ.get("REAPER_UPDATE_REPO", "").strip()
    if configured and _REPO_SHAPE.match(configured):
        return configured
    if configured:
        log.warning("update_check.bad_repo", configured=configured, using=DEFAULT_REPO)
    return DEFAULT_REPO


def _parse(version: str) -> tuple[int, ...] | None:
    """A dotted-integer version as a comparable tuple, or ``None`` for anything else.

    Handles CalVer (``2026.8.1``) and the semver-shaped fallback the source install
    reports. Anything with a suffix or a non-numeric part is unparseable on purpose:
    guessing an order between shapes would answer "update available" from a guess.
    """
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


def _newer(latest: str, current: str) -> bool | None:
    """Whether ``latest`` is strictly newer, with unequal lengths padded (``2026.8``
    and ``2026.8.0`` are the same release, not an upgrade)."""
    a, b = _parse(latest), _parse(current)
    if a is None or b is None:
        return None
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


class UpdateChecker:
    """The one instance the app holds, so the cache is shared across requests.

    ``clock`` is injectable (monotonic seconds) so tests drive the TTLs instead of
    sleeping through them.
    """

    def __init__(self, *, repo: str | None = None, clock: Callable[[], float] | None = None):
        self._repo = repo or _repo()
        self._clock = clock or time.monotonic
        self._lock = asyncio.Lock()
        self._cached: UpdateStatus | None = None
        self._cache_until = 0.0

    async def status(self) -> UpdateStatus:
        """The current answer, from cache when it is fresh.

        The disabled answer is never cached: flipping ``REAPER_UPDATE_CHECK`` back on
        must take effect on the next request, not after a TTL.
        """
        if not _enabled():
            return UpdateStatus(channel=_channel(), enabled=False, current=build_version())
        async with self._lock:
            if self._cached is not None and self._clock() < self._cache_until:
                return self._cached
            status = await self._check()
            ttl = _SUCCESS_TTL if status.checked_at is not None else _FAILURE_TTL
            self._cached = status
            self._cache_until = self._clock() + ttl
            return status

    async def _check(self) -> UpdateStatus:
        channel = _channel()
        current = build_version()
        try:
            if channel == "release":
                return await self._check_release(current)
            return await self._check_dev(current)
        except IntegrationError as exc:
            log.info("update_check.unavailable", error=str(exc))
            return UpdateStatus(channel=channel, enabled=True, current=current)

    async def _check_release(self, current: str) -> UpdateStatus:
        payload = await self._fetch(f"/repos/{self._repo}/releases/latest")
        tag = payload.get("tag_name")
        if not isinstance(tag, str) or not tag.strip():
            raise IntegrationError("update-check", "release payload carried no tag_name")
        latest = tag.strip().removeprefix("v")
        url = payload.get("html_url")
        return UpdateStatus(
            channel="release",
            enabled=True,
            current=current,
            latest=latest,
            update_available=_newer(latest, version_number()),
            url=url if isinstance(url, str) else None,
            checked_at=utcnow(),
        )

    async def _check_dev(self, current: str) -> UpdateStatus:
        payload = await self._fetch(f"/repos/{self._repo}/commits/{_DEV_BRANCH}")
        sha = payload.get("sha")
        if not isinstance(sha, str) or len(sha) < 7:
            raise IntegrationError("update-check", "branch payload carried no commit sha")
        mine = short_commit()
        url = payload.get("html_url")
        return UpdateStatus(
            channel="dev",
            enabled=True,
            current=current,
            latest=f"dev ({sha[:7]})",
            # A local checkout that cannot name its own commit gets "unknown", never a
            # nag that is almost always true and almost never actionable.
            update_available=None if mine is None else not sha.startswith(mine),
            url=url if isinstance(url, str) else None,
            checked_at=utcnow(),
        )

    async def _fetch(self, path: str) -> dict[str, Any]:
        async with PublicClient(_API) as client:
            payload = await client.get_json(path, headers={"Accept": "application/vnd.github+json"})
        if not isinstance(payload, dict):
            raise IntegrationError("update-check", f"expected an object from {path}")
        return payload
