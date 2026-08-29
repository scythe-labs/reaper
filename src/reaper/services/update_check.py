# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checking GitHub for a newer Reaper, on this build's own channel.

A release build compares its version against the newest published release. Every other
build, the ``:dev`` image, a dev binary, or a source checkout, follows the published
``:dev`` container image instead. The answer feeds read-only UI: the About row, and the
account chip's light in the header.

The dev channel follows the image, never the branch. CI builds a ``:dev`` image only when
a push touches code, so a docs or rules commit moves the branch without publishing
anything new to pull. Following the branch tip instead would tell a dev operator an update
is waiting when there is nothing new to install.

The check informs; it never gates anything. A failure, such as no network, GitHub being
down, or an API limit, resolves to "unknown" and is retried after a short pause, so an
air-gapped install shows nothing rather than an error. ``REAPER_UPDATE_CHECK=false`` turns
it off entirely: no request leaves the box, and the surface says checks are off.

There are two callers, and which door they take matters. The nightly job
(``scheduler.check_for_updates``) calls :meth:`UpdateChecker.refresh`, so an install
nobody opens still learns a release exists. The route calls :meth:`UpdateChecker.status`,
which answers from the cache the job has usually just filled, and the TTLs below bound
how often a page load becomes a real network request.

Which repository to ask is baked in at build time as ``REAPER_UPDATE_REPO`` (CI passes its
own repository, so a fork's builds follow the fork); a source checkout falls back to the
upstream repository.

Every call logs itself at DEBUG under ``update_check.*``: which of the three things
happened (off, cache, or ask), what came back, and when the next ask is due. The route
half only runs on demand, so silence between the job's firings could mean either nobody
had Reaper open or the answer was still cached, and only ``REAPER_LOG_LEVEL=DEBUG`` tells
those apart. A failure is logged at INFO instead, since that is worth seeing without being
asked for.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import structlog

from reaper.buildinfo import (
    build_version,
    env_flag,
    is_release,
    short_commit,
    version_number,
)
from reaper.clients.base import IntegrationError
from reaper.clients.public import PublicClient
from reaper.clock import utcnow

log = structlog.get_logger(__name__)

_API = "https://api.github.com"
_GHCR = "https://ghcr.io"
_DEV_BRANCH = "dev"
_DEV_TAG = "dev"
DEFAULT_REPO = "scythe-labs/reaper"

#: What the registry may answer the tag with: a multi-arch index, or a plain
#: single-architecture manifest. Both shapes are handled, because ``:dev`` can be
#: amd64-only until a later nightly publishes an arm64 half to stitch in.
_MANIFEST_TYPES = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

#: This machine's architecture in the registry's spelling. The two halves of ``:dev``
#: can be built on different schedules, so reading the wrong half could tell an arm64
#: operator to pull an image that does not exist for them yet. Anything unrecognized
#: reads as amd64, the half that always exists.
_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}

#: ``owner/name`` and nothing else: the slug is spliced into a URL path, so a value
#: that could smuggle a path separator falls back to the default instead of being sent.
#: The owner charset matches GitHub's own (no dots), and the name must hold at least one
#: non-dot character, or a value like ``../..`` would pass the charset check and
#: normalize into a path escape before the request leaves.
_REPO_SHAPE = re.compile(r"^[A-Za-z0-9-]+/(?=.*[^.])[A-Za-z0-9._-]+$")

#: A successful answer holds for hours, since versions change at most daily and the
#: unauthenticated GitHub API caps requests per hour for the whole host.
_SUCCESS_TTL = 6 * 3600.0
#: A failure is retried sooner, but not per request: one unreachable check must not
#: turn every About view into a fresh network attempt.
_FAILURE_TTL = 15 * 60.0

Channel = Literal["release", "dev"]

#: Per-release cap on the notes carried to the UI. A generated changelog is only a few
#: kilobytes; this cap only bounds the response against a hand-written epic, and the
#: modal's GitHub link carries the rest.
_MAX_NOTES = 20_000


@dataclass(frozen=True)
class ReleaseChange:
    """One release the operator has not taken yet, notes included, for the
    what-changed modal."""

    version: str
    url: str | None
    notes: str | None


@dataclass(frozen=True)
class UpdateStatus:
    """One answer, complete enough to render without consulting anything else.

    ``update_available`` is three-state on purpose: ``None`` means the check could not
    answer, because it was disabled, unreachable, or found an unparseable version, and
    the surface shows that as nothing rather than as either verdict. ``changes`` holds
    the releases newer than the running one, bounded by one API page, newest first. It
    is empty whenever ``update_available`` is not ``True``, and always empty on the dev
    channel, whose builds have no release notes.
    """

    channel: Channel
    enabled: bool
    current: str
    latest: str | None = None
    update_available: bool | None = None
    url: str | None = None
    checked_at: datetime | None = None
    changes: tuple[ReleaseChange, ...] = ()


def _channel() -> Channel:
    return "release" if is_release() else "dev"


def _enabled() -> bool:
    return env_flag("REAPER_UPDATE_CHECK", default=True)


def _repo() -> str:
    configured = os.environ.get("REAPER_UPDATE_REPO", "").strip()
    if configured and _REPO_SHAPE.match(configured):
        return configured
    if configured:
        log.warning("update_check.bad_repo", configured=configured, using=DEFAULT_REPO)
    return DEFAULT_REPO


def _parse(version: str) -> tuple[int, ...] | None:
    """A dotted-integer version as a comparable tuple, or ``None`` for anything else.

    Handles CalVer (``2026.8.1``) and the semver-shaped fallback a source install
    reports. Anything with a suffix or a non-numeric part is deliberately unparseable:
    guessing an order between two different shapes would answer "update available"
    from a guess rather than a real comparison.
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

    ``clock`` takes an injected monotonic-seconds function, so tests can drive the
    TTLs directly instead of sleeping through them.
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
        must take effect on the next request, not after a TTL expires.

        The lock is held across the fetch on purpose: concurrent requests during the
        once-per-TTL refresh wait for one GitHub call instead of each making their own,
        and the wait is bounded by the client's own retry budget.
        """
        return await self._answer(force=False)

    async def refresh(self) -> UpdateStatus:
        """Ask now, whatever the cache holds, and keep the answer for the usual TTL.

        The scheduled job (``scheduler.check_for_updates``) and the Jobs page's Run now
        take this door. A check somebody scheduled or pressed a button for must
        actually ask GitHub: answering out of a stale cache would report "ran just now"
        over an answer that could be hours old. The route keeps :meth:`status` instead,
        because a page load is not a request to go ask.

        The off switch still governs here: disabled returns the disabled answer and
        sends nothing, from this door exactly as from the other.
        """
        return await self._answer(force=True)

    async def _answer(self, *, force: bool) -> UpdateStatus:
        if not _enabled():
            log.debug("update_check.disabled")
            return UpdateStatus(channel=_channel(), enabled=False, current=build_version())
        async with self._lock:
            if not force and self._cached is not None and self._clock() < self._cache_until:
                log.debug(
                    "update_check.cached",
                    latest=self._cached.latest,
                    update_available=self._cached.update_available,
                    held_for=round(self._cache_until - self._clock()),
                )
                return self._cached
            status = await self._check()
            ttl = _SUCCESS_TTL if status.checked_at is not None else _FAILURE_TTL
            self._cached = status
            self._cache_until = self._clock() + ttl
            log.debug(
                "update_check.answered",
                latest=status.latest,
                update_available=status.update_available,
                behind=len(status.changes),
                next_ask_in=round(ttl),
                forced=force,
            )
            return status

    async def _check(self) -> UpdateStatus:
        channel = _channel()
        current = build_version()
        # Logged before the request, not after it: an "asking" line with no matching
        # "answered" line is how a hung or slow GitHub call shows up in the log at all.
        log.debug("update_check.asking", channel=channel, repo=self._repo, current=current)
        try:
            if channel == "release":
                return await self._check_release(current)
            return await self._check_dev(current)
        except IntegrationError as exc:
            log.info("update_check.unavailable", error=str(exc))
            return UpdateStatus(channel=channel, enabled=True, current=current)

    async def _check_release(self, current: str) -> UpdateStatus:
        """The newest release as the headline, plus notes for every release the
        operator has not taken yet, newest first.

        Reads the list instead of ``releases/latest``, because ``latest`` carries only
        one release's notes: an operator two releases behind would see only the
        newest one's notes and never learn the middle release happened. Drafts are
        not visible to this anonymous read; prereleases, the rolling dev build, are
        filtered out because they are not the release channel.

        The newest release is chosen by version, never by list position. GitHub
        orders this endpoint by creation date, so a hotfix cut for an older line can
        sit first; trusting position would report that hotfix as the headline and
        hide a genuinely newer release for a whole cache TTL.

        One API page bounds what this can describe: an operator further behind than
        that still gets the right headline and the newest page of notes.
        """
        payload = await self._fetch(f"/repos/{self._repo}/releases", params={"per_page": 30})
        if not isinstance(payload, list):
            raise IntegrationError(
                "update-check", "error.integration.unexpected_shape", path="/releases"
            )
        entries: list[tuple[str, dict[str, Any]]] = []
        for row in payload:
            if not isinstance(row, dict) or row.get("prerelease") or row.get("draft"):
                continue
            tag = row.get("tag_name")
            if isinstance(tag, str) and tag.strip():
                entries.append((tag.strip().removeprefix("v"), row))
        if not entries:
            raise _incomplete()

        orderable = sorted(
            ((parsed, version, row) for version, row in entries if (parsed := _parse(version))),
            key=lambda item: item[0],
            reverse=True,
        )
        # No orderable version anywhere: show whatever sits first in list order and
        # answer "unknown", never a guess, since list order is a creation date, not
        # a version comparison.
        latest, headline = (orderable[0][1], orderable[0][2]) if orderable else entries[0]
        mine = version_number()
        newer = _newer(latest, mine)
        changes = tuple(
            _change(version, row)
            for _, version, row in orderable  # already sorted newest-first
            if _newer(version, mine)
        )
        url = headline.get("html_url")
        return UpdateStatus(
            channel="release",
            enabled=True,
            current=current,
            latest=latest,
            update_available=newer,
            url=url if isinstance(url, str) and url.startswith("https://") else None,
            checked_at=utcnow(),
            changes=changes if newer else (),
        )

    async def _check_dev(self, current: str) -> UpdateStatus:
        """The commit inside the published ``:dev`` image, never the tip of the branch.

        CI builds an image only when a push touches code, so a docs commit moves the
        branch without publishing anything new. Reading the branch tip instead would
        report an update on every prose-only merge that the operator could not
        actually pull.

        The registry is the one place that records the real fact, and reading it takes
        four hops: an anonymous pull token, the tag's index, this platform's child
        manifest, and the image config, whose ``REAPER_GIT_SHA`` the Dockerfile bakes
        in. Any hop that answers a shape this does not recognize raises, so the surface
        says "couldn't check" rather than naming a version nobody verified. A private
        or missing package reads the same way, which is the safe direction for a fork
        to fail in.
        """
        async with PublicClient(_GHCR) as client:
            sha = await self._dev_image_commit(client)
        mine = short_commit()
        return UpdateStatus(
            channel="dev",
            enabled=True,
            current=current,
            latest=f"dev ({sha[:7]})",
            # A local checkout that cannot name its own commit gets "unknown", never a
            # nag that would almost always fire and rarely mean anything.
            update_available=None if mine is None else not sha.startswith(mine),
            url=self._dev_url(mine, sha),
            checked_at=utcnow(),
        )

    async def _dev_image_commit(self, client: PublicClient) -> str:
        """The short commit baked into this platform's half of the published ``:dev``
        image. The registry path is lowercased because Docker requires it, and a repo
        owner name is commonly capitalized."""
        repo = self._repo.lower()
        token = _text(
            await client.get_json(
                "/token", params={"scope": f"repository:{repo}:pull", "service": "ghcr.io"}
            ),
            "token",
        )
        headers = {"Authorization": f"Bearer {token}", "Accept": _MANIFEST_TYPES}
        manifest = await client.get_json(f"/v2/{repo}/manifests/{_DEV_TAG}", headers=headers)
        child = _child_digest(manifest)
        if child is not None:
            manifest = await client.get_json(f"/v2/{repo}/manifests/{child}", headers=headers)
        blob = await client.get_json(
            f"/v2/{repo}/blobs/{_text(manifest, 'config', 'digest')}", headers=headers
        )
        return _baked_commit(blob)

    def _dev_url(self, mine: str | None, sha: str) -> str:
        """Where "see what changed" goes: the range of commits between the build
        running here and the one in the image, so the page holds only what an update
        would bring. A build that cannot name its own commit has no range to ask for
        and falls back to the branch's commit list."""
        if mine and not sha.startswith(mine):
            return f"https://github.com/{self._repo}/compare/{mine}...{sha[:7]}"
        return f"https://github.com/{self._repo}/commits/{_DEV_BRANCH}"

    async def _fetch(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """One GET, shape-checked by the caller: the release read expects a list, the
        branch read expects an object."""
        async with PublicClient(_API) as client:
            return await client.get_json(
                path, params=params, headers={"Accept": "application/vnd.github+json"}
            )


def _incomplete() -> IntegrationError:
    """The one error every unrecognized registry answer raises. The operator sees it
    as "couldn't check for updates", the right message for a shape nobody expected."""
    return IntegrationError("update-check", "error.integration.update_check_incomplete")


def _text(payload: Any, *keys: str) -> str:
    """One nested string out of a JSON answer, or the shared incomplete error. Every
    registry read is checked here for shape rather than trusted as-is."""
    value: Any = payload
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise _incomplete()
    return value.strip()


def _child_digest(manifest: Any) -> str | None:
    """This platform's image inside a multi-arch index, or ``None`` when the tag is
    already a plain single-architecture manifest and is itself the answer.

    An index that publishes no half for this architecture raises: there is no image to
    pull here, so both "up to date" and "update available" would be wrong answers.
    """
    children = manifest.get("manifests") if isinstance(manifest, dict) else None
    if not isinstance(children, list):
        return None
    want = _ARCH.get(platform.machine().strip().lower(), "amd64")
    for child in children:
        if not isinstance(child, dict):
            continue
        where = child.get("platform")
        if isinstance(where, dict) and where.get("architecture") == want:
            return _text(child, "digest")
    raise _incomplete()


def _baked_commit(config: Any) -> str:
    """The commit CI passed as ``REAPER_GIT_SHA``, read back out of the image config's
    environment. The Dockerfile turns that build argument into an ``ENV`` entry, which
    puts it here and lets the running container report the same value."""
    inner = config.get("config") if isinstance(config, dict) else None
    env = inner.get("Env") if isinstance(inner, dict) else None
    for entry in env if isinstance(env, list) else ():
        if isinstance(entry, str) and entry.startswith("REAPER_GIT_SHA="):
            value = entry.partition("=")[2].strip()
            if value:
                return value
    raise _incomplete()


def _change(version: str, row: dict[str, Any]) -> ReleaseChange:
    url = row.get("html_url")
    notes = row.get("body")
    if isinstance(notes, str) and len(notes) > _MAX_NOTES:
        notes = notes[:_MAX_NOTES] + "\n\nRead the rest on GitHub."
    return ReleaseChange(
        version=version,
        # The UI puts this straight into a link, so only an https URL qualifies,
        # whatever shape the API answer arrived in.
        url=url if isinstance(url, str) and url.startswith("https://") else None,
        notes=notes if isinstance(notes, str) else None,
    )
