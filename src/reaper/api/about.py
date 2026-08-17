# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which version is running, and whether a newer one exists."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request

from reaper.api import tags as api_tags
from reaper.api.schemas import (
    AboutOut,
    ReleaseChangeOut,
    UpdateOut,
)
from reaper.buildinfo import build_version
from reaper.config import Settings
from reaper.services import (
    backup,
)
from reaper.services.update_check import UpdateChecker

router = APIRouter(prefix="/api")


def _db_bytes(base: Path) -> int:
    """The size of one SQLite database on disk, including its live WAL.

    The one implementation lives with the backup service, which weighs the same files
    to size a download; the About page and the Backup panel must not drift apart.
    """
    return backup.db_size_on_disk(base)


@router.get("/about", tags=[api_tags.ABOUT])
async def about(request: Request) -> AboutOut:
    """What's running and where its data lives. Read-only facts for the About page."""
    settings: Settings = request.app.state.settings
    data_dir = settings.data_dir
    return AboutOut(
        version=build_version(),
        license="AGPL-3.0",
        data_dir=str(data_dir),
        reaper_db_bytes=_db_bytes(settings.database_path),
        cache_db_bytes=_db_bytes(data_dir / "cache.db"),
    )


@router.get("/about/update", tags=[api_tags.ABOUT])
async def update_status(request: Request) -> UpdateOut:
    """Whether a newer Reaper exists, from this build's channel: a release compares
    against the newest published release, a dev build against the tip of the dev
    branch. Answered from a shared cache that holds for hours; a check that cannot
    answer returns unknown rather than an error, so this route never fails a page."""
    checker: UpdateChecker = request.app.state.update_checker
    status = await checker.status()
    return UpdateOut(
        channel=status.channel,
        enabled=status.enabled,
        current=status.current,
        latest=status.latest,
        update_available=status.update_available,
        url=status.url,
        checked_at=status.checked_at,
        changes=[
            ReleaseChangeOut(version=c.version, url=c.url, notes=c.notes) for c in status.changes
        ],
    )
