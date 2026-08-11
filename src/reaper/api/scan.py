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
from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel

from reaper.api import tags as api_tags
from reaper.api.deps import state_singleton
from reaper.clients.base import IntegrationError
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.services import scan_runner
from reaper.services.snapshot import Progress

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=[api_tags.SCANS])


#: Each phase's slice of the 0-100 bar, in order. The scan reports progress as (done, total)
#: within a phase, but ``total`` changes meaning between phases (a handful of gather steps,
#: then the item count for scoring), so a raw done/total jumps around. Mapping each phase to
#: a fixed band and filling within it gives one bar that only ever rises. The bands are the
#: rough share of a real scan's wall clock, with gathering and scoring the long ones.
_PHASE_BANDS: dict[str, tuple[int, int]] = {
    "starting": (0, 2),
    "history": (2, 12),
    "lists": (12, 18),
    "gathering": (18, 45),
    "scoring": (45, 90),
    "done": (90, 95),
    # The snapshot is committed by here; "shelves" is the post-scan Leaving Soon reconcile
    # (a full Plex round-trip). It gets its own band UNDER 100 so the bar honestly shows
    # "Updating shelves" while it runs, instead of sitting at a false 100%. See scan_runner.
    "shelves": (95, 99),
    "complete": (100, 100),
}


def _phase_percent(phase: str, done: int, total: int) -> int:
    """A monotonic 0-100 for the progress bar, from the phase and its within-phase fraction.

    An unknown phase spans the whole bar so it can never read as complete early. ``total``
    of 0 (the history and lists phases report no sub-steps) sits at the band's start rather
    than dividing by zero, which is what made the old bar read 100% before any work began.
    """
    lo, hi = _PHASE_BANDS.get(phase, (0, 100))
    fraction = min(1.0, max(0.0, done / total)) if total > 0 else 0.0
    return round(lo + (hi - lo) * fraction)


class ScanStatus(BaseModel):
    """Where a scan has got to. Polled by the browser; mutated in place by the running job."""

    running: bool = False
    phase: str = "idle"
    done: int = 0
    total: int = 0
    percent: int = 0
    """A monotonic 0-100 for the progress bar. Derived from the phase and its fraction so it
    only ever rises; the browser renders this directly instead of dividing done by a total
    whose meaning changes between phases."""
    detail: str = ""
    error: str | None = None
    snapshot_id: int | None = None
    followup_queued: bool = False
    """A second scan will start the moment the current one finishes. Set when a start
    request arrives mid-scan (the auto-rescan after a policy save, usually): the running
    scan is reading the library under the policies in force when it *began*, so only a
    scan that starts after the request can reflect the caller's changes. Without this,
    a save landing mid-scan was silently swallowed -- the snapshot arrived with the old
    policy's hashes and the "needs a fresh scan" notice never cleared."""


def _status(app: FastAPI) -> ScanStatus:
    return state_singleton(app, "scan_status", ScanStatus)


def launch_scan(app: FastAPI) -> ScanStatus:
    """Start a background scan, or queue one behind the scan already running.

    Extracted from the ``/scan/start`` endpoint so a scan can be launched by something other
    than a browser request -- notably the auto-rescan the reap kicks off when it finishes,
    since removing files leaves the last snapshot's queue and policy preview stale. There is
    exactly ONE scan mechanism; this is it, and both callers go through it.

    Never launches a parallel scan: a second request while one is in flight queues exactly
    one follow-up run instead. The follow-up matters because a scan reads the library under
    the policies in force when it **began** -- whoever asks for a scan mid-run needs one that
    starts after their request, or their change would never be scanned in. Read-only throughout.
    """
    status = _status(app)
    if status.running:
        status.followup_queued = True
        return status

    # Reset for a fresh run. Mutated in place so the polling endpoint sees each update.
    status.running = True
    status.phase = "starting"
    status.done = 0
    status.total = 0
    status.percent = 0
    status.detail = ""
    status.error = None
    status.snapshot_id = None
    status.followup_queued = False

    settings: Settings = app.state.settings
    box: SecretBox = app.state.secret_box
    cache_engine = app.state.cache_engine
    factory = app.state.session_factory

    def on_progress(progress: Progress) -> None:
        status.phase = progress.phase
        status.done = progress.done
        status.total = progress.total
        status.detail = progress.detail
        # Monotonic: a phase's band never dips below where the previous one ended, and max()
        # guards against any out-of-order emit, so the bar cannot jump backward.
        status.percent = max(
            status.percent, _phase_percent(progress.phase, progress.done, progress.total)
        )

    async def run() -> None:
        try:
            while True:
                snapshot = await scan_runner.run_scan(
                    settings=settings,
                    session_factory=factory,
                    cache_engine=cache_engine,
                    box=box,
                    on_progress=on_progress,
                )
                status.snapshot_id = snapshot.id
                if not status.followup_queued:
                    status.phase = "complete"
                    status.percent = 100
                    status.detail = ""
                    return
                # A start request arrived mid-run, so that caller's changes are not in the
                # snapshot that just landed. Consume the queue and go again -- the next
                # run re-reads the active policies. `running` stays true across the
                # hand-off, so the browser sees one operation, not a flicker of "done".
                status.followup_queued = False
                status.phase = "starting"
                status.done = 0
                status.total = 0
                status.percent = 0
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
            # An errored run does not honor a queued follow-up: rerunning would repeat
            # (or mask) the failure the owner needs to see first.
            status.running = False
            status.followup_queued = False

    # Held on app.state so the task is not garbage-collected mid-run, and can be canceled on
    # shutdown. It is deliberately NOT tied to any request's lifetime.
    app.state.scan_task = asyncio.create_task(run())
    return status


@router.post("/scan/start")
async def start_scan(request: Request) -> ScanStatus:
    """Start a background scan from the browser (or queue one behind a running scan)."""
    return launch_scan(request.app)


@router.get("/scan/status")
async def scan_status(request: Request) -> ScanStatus:
    """The current (or last) scan's progress. Cheap; the browser polls it while a scan runs."""
    return _status(request.app)
