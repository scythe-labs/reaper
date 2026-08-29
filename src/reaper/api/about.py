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
    """Return one SQLite database's size on disk, including its live WAL.

    The backup service holds the only implementation, since it weighs the same files
    to size a download. The About page and the Backup panel must show the same number.
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
    """Say whether a newer Reaper is available.

    A release build compares against the newest published release. A dev build
    compares against the tip of the dev branch. The answer comes from a shared cache
    that holds for hours. If the check itself fails, the answer is unknown rather
    than an error, so this route never fails the page.
    """
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
