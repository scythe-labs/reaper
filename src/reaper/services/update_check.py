# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checking GitHub for a newer Reaper, on this build's own channel.

A release build compares its version against the newest published release; every
other build -- the ``:dev`` image, a dev binary, a source checkout -- follows the tip
of the dev branch. The answer feeds read-only UI: the About row, and the account
chip's light in the header.

The check informs; it never gates. A failure (no network, GitHub down, an API limit)
resolves to "unknown" and is retried after a short pause, so an air-gapped install
shows nothing rather than an error. ``REAPER_UPDATE_CHECK=false`` turns it off
entirely: no request leaves the box, and the surface says checks are off.

Two callers, and which door they take is the point. The nightly job
(``scheduler.check_for_updates``) calls :meth:`UpdateChecker.refresh`, so an install
nobody opens still learns a release exists. The route calls :meth:`UpdateChecker.status`,
which answers from the cache the job has usually just filled, and the TTLs below bound
how often a *page load* becomes a real ask. Until that job existed the route was the only
caller: a server nobody signed in to never checked at all, while the About panel told the
operator Reaper checked a few times a day (#464).

Which repository to ask is baked at build time as ``REAPER_UPDATE_REPO`` (CI passes
its own repository, so a fork's builds follow the fork); a source checkout falls back
to the upstream repository.

Every call narrates itself at DEBUG under ``update_check.*``: which of the three
things happened (off, cache, ask), what came back, and when the next ask is due. The
route half is demand-driven, so silence between the job's firings has two readings --
nobody had Reaper open, or the answer was still cached -- and only
``REAPER_LOG_LEVEL=DEBUG`` tells them apart. A failure stays at INFO, since that one is
worth seeing without being asked for.
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
_DEV_BRANCH = "dev"
DEFAULT_REPO = "scythe-labs/reaper"

#: ``owner/name`` and nothing else: the slug is spliced into a URL path, so a value
#: that could smuggle a separator falls back to the default instead of being sent.
#: The owner charset is GitHub's own (no dots), and the name must hold at least one
#: non-dot character, or ``../..`` would pass the charset and normalize into a path
#: escape before the request leaves.
_REPO_SHAPE = re.compile(r"^[A-Za-z0-9-]+/(?=.*[^.])[A-Za-z0-9._-]+$")

#: A successful answer holds for hours -- versions change daily at most, and the
#: unauthenticated GitHub API allows 60 requests an hour for the whole host.
_SUCCESS_TTL = 6 * 3600.0
#: A failure is retried sooner, but not per request: one unreachable check must not
#: turn every About view into a fresh network attempt.
_FAILURE_TTL = 15 * 60.0

Channel = Literal["release", "dev"]

#: Per-release cap on the notes carried to the UI. A generated changelog is a few
#: kilobytes; the cap only bounds the response against a hand-written epic, and the
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
    answer (disabled, unreachable, or an unparseable version), which the surface shows
    as nothing rather than as either verdict. ``changes`` holds the releases newer
    than the running one (bounded by one API page), newest first; it is empty whenever
    ``update_available`` is not ``True``, and always empty on the dev channel, whose
    builds have no notes.
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

        The lock is held across the fetch on purpose: concurrent requests during the
        once-per-TTL refresh wait for one GitHub call instead of each making their
        own, and the wait is bounded by the client's own retry budget.
        """
        return await self._answer(force=False)

    async def refresh(self) -> UpdateStatus:
        """Ask now, whatever the cache holds, and keep the answer for the usual TTL.

        The scheduled job (``scheduler.check_for_updates``) and the Jobs page's Run now
        take this door. A check somebody scheduled or pressed a button for that answered
        out of a cache is not a check: it would report "ran just now" over an answer up
        to six hours old, which is the shape of claim #464 was about. The route keeps
        :meth:`status`, because a page load is not a request to go ask.

        The off switch still governs (rule 55): disabled returns the disabled answer and
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
        # Emitted before the request, not after it: an ask with no matching
        # ``answered`` is how a hung or slow GitHub call shows up at all.
        log.debug("update_check.asking", channel=channel, repo=self._repo, current=current)
        try:
            if channel == "release":
                return await self._check_release(current)
            return await self._check_dev(current)
        except IntegrationError as exc:
            log.info("update_check.unavailable", error=str(exc))
            return UpdateStatus(channel=channel, enabled=True, current=current)

    async def _check_release(self, current: str) -> UpdateStatus:
        """The newest release as the headline, plus notes for the releases the
        operator has not taken, newest first.

        The list read replaces ``releases/latest`` because ``latest`` carries one
        body: an operator two releases behind would be shown only the newest one's
        notes and read the middle release as never having happened. Drafts are not
        visible to this anonymous read; prereleases (the rolling dev build) are
        filtered because they are not the release channel.

        **The newest release is chosen by version, never by list position.** GitHub
        orders this endpoint by creation date, so a hotfix cut for an older line
        sits first; trusting position would report that hotfix as the headline and
        suppress a genuinely newer release for a whole cache TTL.

        One API page bounds what this can describe: an operator further behind than
        a page still gets the right headline and the newest page of notes.
        """
        payload = await self._fetch(f"/repos/{self._repo}/releases", params={"per_page": 30})
        if not isinstance(payload, list):
            raise IntegrationError("update-check", "expected a list of releases")
        entries: list[tuple[str, dict[str, Any]]] = []
        for row in payload:
            if not isinstance(row, dict) or row.get("prerelease") or row.get("draft"):
                continue
            tag = row.get("tag_name")
            if isinstance(tag, str) and tag.strip():
                entries.append((tag.strip().removeprefix("v"), row))
        if not entries:
            raise IntegrationError("update-check", "the answer carried no releases")

        orderable = sorted(
            ((parsed, version, row) for version, row in entries if (parsed := _parse(version))),
            key=lambda item: item[0],
            reverse=True,
        )
        # No orderable version anywhere: show whatever sits newest and answer
        # "unknown", never a guess (list position is a creation date, not a verdict).
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
        payload = await self._fetch(f"/repos/{self._repo}/commits/{_DEV_BRANCH}")
        if not isinstance(payload, dict):
            raise IntegrationError("update-check", "expected an object for the branch tip")
        sha = payload.get("sha")
        if not isinstance(sha, str) or len(sha) < 7:
            raise IntegrationError("update-check", "branch payload carried no commit sha")
        mine = short_commit()
        return UpdateStatus(
            channel="dev",
            enabled=True,
            current=current,
            latest=f"dev ({sha[:7]})",
            # A local checkout that cannot name its own commit gets "unknown", never a
            # nag that is almost always true and almost never actionable.
            update_available=None if mine is None else not sha.startswith(mine),
            # The branch's commit list, not the tip commit the payload names: "what
            # changed" on the dev channel is a run of commits, and the single-commit
            # page shows exactly one of them.
            url=f"https://github.com/{self._repo}/commits/{_DEV_BRANCH}",
            checked_at=utcnow(),
        )

    async def _fetch(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """One GET, shape-checked by the caller: the release read wants a list, the
        branch read an object."""
        async with PublicClient(_API) as client:
            return await client.get_json(
                path, params=params, headers={"Accept": "application/vnd.github+json"}
            )


def _change(version: str, row: dict[str, Any]) -> ReleaseChange:
    url = row.get("html_url")
    notes = row.get("body")
    if isinstance(notes, str) and len(notes) > _MAX_NOTES:
        notes = notes[:_MAX_NOTES] + "\n\nRead the rest on GitHub."
    return ReleaseChange(
        version=version,
        # The UI puts this straight into a link; only an https URL qualifies, however
        # the API answer was bent on the way here.
        url=url if isinstance(url, str) and url.startswith("https://") else None,
        notes=notes if isinstance(notes, str) else None,
    )
