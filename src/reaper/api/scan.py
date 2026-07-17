# SPDX-License-Identifier: AGPL-3.0-or-later
"""Running a scan from the browser, as a background job.

A full scan of a large library takes tens of seconds. It runs **detached from the request
that starts it**, so closing the tab or navigating to another screen does not stop it -- the
owner should be able to kick off a scan and go read the review queue while it works. Progress
lives on ``app.state.scan_status``; the browser polls ``GET /api/scan/status`` to follow along
and to pick a scan already in flight back up when it returns to the page.

The scan itself lives in ``services.scan_runner`` so a schedule can run the identical pipeline
with no browser attached. This module only starts it and reports where it has got to.

A scan **cannot delete anything**. It reads, it scores, it writes rows to our own database.
``GuardedTransport`` would refuse a mutating call even if one were attempted.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel

from reaper.clients.base import IntegrationError
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.services import scan_runner
from reaper.services.snapshot import Progress

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api")


class ScanStatus(BaseModel):
    """Where a scan has got to. Polled by the browser; mutated in place by the running job."""

    running: bool = False
    phase: str = "idle"
    done: int = 0
    total: int = 0
    detail: str = ""
    error: str | None = None
    snapshot_id: int | None = None


def _status(request: Request) -> ScanStatus:
    status: ScanStatus | None = getattr(request.app.state, "scan_status", None)
    if status is None:
        status = ScanStatus()
        request.app.state.scan_status = status
    return status


@router.post("/scan/start")
async def start_scan(request: Request) -> ScanStatus:
    """Start a background scan, or return the one already running.

    Idempotent while a scan is in flight: a second click (or a second tab) does not launch a
    parallel scan, it just gets the current progress back. Read-only throughout.
    """
    status = _status(request)
    if status.running:
        return status

    # Reset for a fresh run. Mutated in place so the polling endpoint sees each update.
    status.running = True
    status.phase = "starting"
    status.done = 0
    status.total = 0
    status.detail = ""
    status.error = None
    status.snapshot_id = None

    settings: Settings = request.app.state.settings
    box: SecretBox = request.app.state.secret_box
    cache_engine = request.app.state.cache_engine
    factory = request.app.state.session_factory

    def on_progress(progress: Progress) -> None:
        status.phase = progress.phase
        status.done = progress.done
        status.total = progress.total
        status.detail = progress.detail

    async def run() -> None:
        try:
            snapshot = await scan_runner.run_scan(
                settings=settings,
                session_factory=factory,
                cache_engine=cache_engine,
                box=box,
                on_progress=on_progress,
            )
            status.snapshot_id = snapshot.id
            status.phase = "complete"
            status.detail = ""
        except scan_runner.ScanInProgressError as exc:
            # The scheduler's scan beat this one to the shared claim (the guard above only
            # sees browser-started scans). Nothing is wrong; say what is happening.
            status.error = str(exc)
            status.phase = "error"
        except (scan_runner.ScanConfigError, IntegrationError) as exc:
            # A misconfiguration (no Radarr/Tautulli yet) or an unreachable source is the
            # owner's to fix -- report it rather than letting the scan die silently and leave
            # a stale queue looking current.
            status.error = str(exc)
            status.phase = "error"
        except Exception as exc:
            # A background task must never crash silently -- surface it as an error the UI shows.
            status.error = str(exc)
            status.phase = "error"
            log.warning("scan.background_failed", error=str(exc))
        finally:
            status.running = False

    # Held on app.state so the task is not garbage-collected mid-run, and can be cancelled on
    # shutdown. It is deliberately NOT tied to this request's lifetime.
    request.app.state.scan_task = asyncio.create_task(run())
    return status


@router.get("/scan/status")
async def scan_status(request: Request) -> ScanStatus:
    """The current (or last) scan's progress. Cheap; the browser polls it while a scan runs."""
    return _status(request)
