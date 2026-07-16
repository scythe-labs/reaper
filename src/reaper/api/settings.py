# SPDX-License-Identifier: AGPL-3.0-or-later
"""Configuring Reaper from the web UI.

Everything an operator needs to stand the tool up and keep it running lives here: the
external services it reads from, the Plex link, the schedule, and the safety switch.

Two things are true of the whole router:

* **It requires a session.** These routes are behind the auth gate (see
  ``api.middleware``); only a signed-in admin can change what Reaper is pointed at.
* **API keys are write-only.** A key is encrypted the instant it arrives and is never
  read back to the browser -- a view says only *whether* a key is set. The deletion
  switch is asymmetric: turning it ON requires the admin password (see ``set_safety``),
  turning it OFF requires nothing, because making Reaper safer is never gated.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.auth.cookie import read_session_token
from reaper.clock import utcnow
from reaper.config import RuntimeSafety, Settings
from reaper.crypto import SecretBox
from reaper.db.models import InstanceKind, PlexServer
from reaper.notify.discord import DiscordNotifier, Embed, build_notifier
from reaper.services import admin_password, app_settings, instances
from reaper.services.plex_link import (
    PlexLinkError,
    PlexServerChoiceNeededError,
    poll_link,
    start_link,
)
from reaper.services.scheduler import SCAN_JOB_ID, apply_scan_schedule

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/settings")


def _factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.secret_box
    return box


def _kind(value: str) -> InstanceKind:
    try:
        return InstanceKind(value)
    except ValueError as exc:
        raise HTTPException(
            422, f"{value!r} is not a service Reaper knows. Use sonarr, radarr, tautulli or seerr."
        ) from exc


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class InstanceOut(BaseModel):
    id: int
    kind: str
    name: str
    base_url: str
    enabled: bool
    verify_tls: bool
    has_key: bool
    api_path_prefix: str
    detected_version: str | None = None
    last_ok_at: str | None = None
    last_error: str | None = None

    @classmethod
    def of(cls, view: instances.InstanceView) -> InstanceOut:
        return cls(**view.__dict__)


class InstanceCreateIn(BaseModel):
    kind: str
    name: str
    base_url: str
    api_key: str
    verify_tls: bool = True


class InstanceUpdateIn(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # blank/omitted keeps the stored key
    enabled: bool | None = None
    verify_tls: bool | None = None  # omitted keeps the stored setting; explicit False sticks


class InstanceTestIn(BaseModel):
    kind: str
    base_url: str
    api_key: str
    verify_tls: bool = True


class TestOut(BaseModel):
    ok: bool
    detail: str
    version: str | None = None


class PlexStatusOut(BaseModel):
    linked: bool
    name: str | None = None
    connection_uri: str | None = None
    last_ok_at: str | None = None
    web_url: str = ""
    """Where "open in Plex" links point. Always present, linked or not -- the hosted
    Plex Web default until the operator overrides it."""


class PlexWebUrlIn(BaseModel):
    """The one editable Plex display setting. Empty resets to the hosted default."""

    web_url: str = ""


class PlexLinkStartOut(BaseModel):
    pin_id: int
    auth_url: str


class PlexLinkPollIn(BaseModel):
    pin_id: int
    # Multi-server accounts only: the machine identifier of the owned server the admin
    # picked, echoed back from a "choose_server" response.
    machine_identifier: str | None = None


class PlexServerChoiceOut(BaseModel):
    name: str
    machine_identifier: str


class PlexLinkPollOut(BaseModel):
    status: str  # "pending" | "ok" | "choose_server"
    server: PlexStatusOut | None = None
    # Present only with status "choose_server": the owned servers to pick from.
    servers: list[PlexServerChoiceOut] | None = None


class ScheduledJobOut(BaseModel):
    id: str
    label: str
    next_run_at: str | None
    trigger: str


class ScheduleOut(BaseModel):
    scan_cron: str | None
    jobs: list[ScheduledJobOut]


class ScheduleIn(BaseModel):
    scan_cron: str | None = None


class SafetyOut(BaseModel):
    destructive_enabled: bool
    """Whether Reaper may delete right now."""
    has_password: bool
    """Whether an admin password has been set. Turning deletion on requires one."""
    note: str | None = None


class SafetyIn(BaseModel):
    enabled: bool
    password: str | None = None
    """Required to turn deletion ON (checked against the admin password). Not needed to
    turn it off -- making Reaper safer is never gated."""


class AdminPasswordIn(BaseModel):
    password: str


class NotificationsOut(BaseModel):
    has_webhook: bool
    """Whether a Discord webhook is stored. The URL itself is a credential and is NEVER
    echoed back to the browser -- a view says only *whether* one is set, exactly like an
    instance API key."""


class NotificationsIn(BaseModel):
    webhook_url: str


class NotificationsTestIn(BaseModel):
    webhook_url: str | None = None
    """The URL to test. Omit to test the already-stored webhook, so an operator can verify a
    saved channel without re-pasting the secret."""


#: Only https URLs whose host is Discord's webhook endpoint are accepted; the token lives in
#: the path, so a typo'd host would leak it to a stranger. Subdomains (ptb., canary.) count.
_DISCORD_WEBHOOK_HOSTS = ("discord.com", "discordapp.com")


def _validated_discord_webhook(raw: str) -> str:
    """Return the stripped URL if it is a Discord webhook, else 422. Server-side twin of the
    form validation -- never trust the browser to have checked."""
    url = (raw or "").strip()
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    ok_host = host in _DISCORD_WEBHOOK_HOSTS or any(
        host.endswith("." + h) for h in _DISCORD_WEBHOOK_HOSTS
    )
    if parsed.scheme != "https" or not ok_host or not parsed.path.startswith("/api/webhooks/"):
        raise HTTPException(
            422,
            "That is not a Discord webhook URL. Paste the full "
            "https://discord.com/api/webhooks/… URL from the channel's integration settings.",
        )
    return url


def _sample_embed() -> Embed:
    return Embed(
        title="Reaper is connected",
        description=(
            "This is a test message. If you can see it, the household will get a heads-up "
            "here before any title is removed."
        ),
    )


# ---------------------------------------------------------------------------
# Instances
# ---------------------------------------------------------------------------


@router.get("/instances")
async def list_instances(request: Request) -> list[InstanceOut]:
    async with _factory(request)() as session:
        return [InstanceOut.of(v) for v in await instances.list_instances(session)]


@router.post("/instances")
async def create_instance(request: Request, payload: InstanceCreateIn) -> InstanceOut:
    async with _factory(request)() as session:
        try:
            view = await instances.create_instance(
                session,
                _box(request),
                kind=_kind(payload.kind),
                name=payload.name,
                base_url=payload.base_url,
                api_key=payload.api_key,
                verify_tls=payload.verify_tls,
            )
        except instances.InstanceError as exc:
            raise HTTPException(409, str(exc)) from exc
        await session.commit()
        return InstanceOut.of(view)


@router.put("/instances/{instance_id}")
async def update_instance(
    request: Request, instance_id: int, payload: InstanceUpdateIn
) -> InstanceOut:
    async with _factory(request)() as session:
        try:
            view = await instances.update_instance(
                session,
                _box(request),
                instance_id,
                name=payload.name,
                base_url=payload.base_url,
                api_key=payload.api_key,
                enabled=payload.enabled,
                verify_tls=payload.verify_tls,
            )
        except instances.InstanceConflictError as exc:
            # A rename into an existing name is a conflict, not a missing resource.
            raise HTTPException(409, str(exc)) from exc
        except instances.InstanceError as exc:
            raise HTTPException(404, str(exc)) from exc
        await session.commit()
        return InstanceOut.of(view)


@router.delete("/instances/{instance_id}")
async def delete_instance(request: Request, instance_id: int) -> dict[str, bool]:
    async with _factory(request)() as session:
        removed = await instances.delete_instance(session, instance_id)
        await session.commit()
    return {"removed": removed}


@router.post("/instances/test")
async def test_new_instance(request: Request, payload: InstanceTestIn) -> TestOut:
    """Test a URL and key before saving, so a typo is caught on the add form."""
    result = await instances.test_connection(
        _kind(payload.kind), payload.base_url, payload.api_key, verify=payload.verify_tls
    )
    return TestOut(ok=result.ok, detail=result.detail, version=result.version)


@router.post("/instances/{instance_id}/test")
async def test_saved_instance(request: Request, instance_id: int) -> TestOut:
    """Test a stored instance and record the outcome on it."""
    async with _factory(request)() as session:
        try:
            result = await instances.test_saved_instance(session, _box(request), instance_id)
        except instances.InstanceError as exc:
            raise HTTPException(404, str(exc)) from exc
        await session.commit()
    return TestOut(ok=result.ok, detail=result.detail, version=result.version)


# ---------------------------------------------------------------------------
# Plex
# ---------------------------------------------------------------------------


async def _plex_status(session: AsyncSession) -> PlexStatusOut:
    web_url = await app_settings.get_plex_web_url(session)
    server = (await session.execute(select(PlexServer))).scalars().first()
    if server is None:
        return PlexStatusOut(linked=False, web_url=web_url)
    return PlexStatusOut(
        linked=True,
        name=server.name,
        connection_uri=server.connection_uri,
        last_ok_at=server.last_ok_at.isoformat() if server.last_ok_at else None,
        web_url=web_url,
    )


@router.get("/plex")
async def plex_status(request: Request) -> PlexStatusOut:
    async with _factory(request)() as session:
        return await _plex_status(session)


@router.put("/plex")
async def set_plex_web_url(request: Request, payload: PlexWebUrlIn) -> PlexStatusOut:
    """Save where "open in Plex" links point. Empty resets to the hosted default."""
    cleaned = payload.web_url.strip()
    if cleaned and not (cleaned.startswith("https://") or cleaned.startswith("http://")):
        raise HTTPException(422, "The Plex web address must start with https:// or http://.")
    async with _factory(request)() as session:
        await app_settings.set_plex_web_url(session, cleaned or None)
        status = await _plex_status(session)
        await session.commit()
    log.info("settings.plex_web_url_saved")
    return status


@router.post("/plex/link/start")
async def plex_link_start(request: Request) -> PlexLinkStartOut:
    async with _factory(request)() as session:
        safety = await app_settings.runtime_safety(session, _settings(request))
    start = await start_link(_factory(request), safety=safety)
    return PlexLinkStartOut(pin_id=start.pin_id, auth_url=start.auth_url)


@router.post("/plex/link/poll")
async def plex_link_poll(request: Request, payload: PlexLinkPollIn) -> PlexLinkPollOut:
    async with _factory(request)() as session:
        safety = await app_settings.runtime_safety(session, _settings(request))
    try:
        linked = await poll_link(
            _factory(request),
            _box(request),
            pin_id=payload.pin_id,
            safety=safety,
            choice=payload.machine_identifier,
        )
    except PlexServerChoiceNeededError as exc:
        # The account owns several servers. The PIN stays valid; the browser shows the
        # candidates and re-polls with the admin's pick.
        return PlexLinkPollOut(
            status="choose_server",
            servers=[
                PlexServerChoiceOut(name=c.name, machine_identifier=c.machine_identifier)
                for c in exc.candidates
            ],
        )
    except PlexLinkError as exc:
        raise HTTPException(400, str(exc)) from exc

    if linked is None:
        return PlexLinkPollOut(status="pending")
    async with _factory(request)() as session:
        return PlexLinkPollOut(status="ok", server=await _plex_status(session))


@router.delete("/plex")
async def plex_unlink(request: Request) -> dict[str, bool]:
    """Forget the linked Plex server. Deletes nothing in Plex -- it just drops the stored
    connection and token, so Leaving Soon and the collection whitelist go quiet until a
    server is linked again."""
    async with _factory(request)() as session:
        server = (await session.execute(select(PlexServer))).scalars().first()
        if server is None:
            return {"removed": False}
        await session.delete(server)
        await session.commit()
    log.info("plex.unlinked")
    return {"removed": True}


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

#: Friendly labels for the background jobs, so the schedule page reads in English.
_JOB_LABELS = {
    "refresh_ratings": "Refresh IMDb ratings",
    "refresh_curated_lists": "Refresh curated lists",
    "full_history_sweep": "Full watch-history sweep",
    SCAN_JOB_ID: "Automatic library scan",
}


@router.get("/schedule")
async def get_schedule(request: Request) -> ScheduleOut:
    scheduler = request.app.state.scheduler
    async with _factory(request)() as session:
        scan_cron = await app_settings.get_scan_schedule(session)

    jobs = [
        ScheduledJobOut(
            id=job.id,
            label=_JOB_LABELS.get(job.id, job.id),
            next_run_at=job.next_run_time.isoformat() if job.next_run_time else None,
            trigger=str(job.trigger),
        )
        for job in scheduler.get_jobs()
    ]
    jobs.sort(key=lambda j: j.next_run_at or "9999")
    return ScheduleOut(scan_cron=scan_cron, jobs=jobs)


@router.put("/schedule")
async def set_schedule(request: Request, payload: ScheduleIn) -> ScheduleOut:
    """Set (or clear) the automatic-scan cron. A scan never deletes, so this is safe.

    A malformed cron is a 422 with the reason -- an owner who thinks they scheduled a
    nightly scan must not silently get nothing.
    """
    cron = (payload.scan_cron or "").strip() or None
    scheduler = request.app.state.scheduler
    try:
        apply_scan_schedule(
            scheduler,
            cron,
            settings=_settings(request),
            session_factory=_factory(request),
            cache_engine=request.app.state.cache_engine,
            secret_box=_box(request),
        )
    except ValueError as exc:
        raise HTTPException(
            422, f"That is not a valid schedule: {exc}. Use cron form, e.g. '30 4 * * *'."
        ) from exc

    async with _factory(request)() as session:
        await app_settings.set_scan_schedule(session, cron)
        await session.commit()
    log.info("schedule.updated", scan_cron=cron)
    return await get_schedule(request)


#: Maintenance jobs the owner may nudge to run now. The library scan is deliberately absent:
#: it runs through ``/api/scan/start`` as a polled background job, so the UI can show progress.
_RUNNABLE_JOBS = frozenset({"refresh_ratings", "refresh_curated_lists", "full_history_sweep"})


@router.post("/jobs/{job_id}/run")
async def run_job(request: Request, job_id: str) -> dict[str, str]:
    """Run a maintenance job now, without touching its schedule.

    Nudges APScheduler to fire the job immediately by moving its next run to now; the
    schedule itself is untouched, so the next regular run still happens as planned. These
    are read-only upkeep jobs (refreshing ratings and lists, sweeping watch history) -- none
    of them can delete anything.
    """
    if job_id not in _RUNNABLE_JOBS:
        raise HTTPException(404, f"No runnable job named {job_id!r}.")
    job = request.app.state.scheduler.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job {job_id!r} is not scheduled.")
    job.modify(next_run_time=utcnow())
    log.info("jobs.run_now", job=job_id)
    return {"status": "started", "job": job_id}


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


async def _safety_out(session: AsyncSession, safety: RuntimeSafety) -> SafetyOut:
    return SafetyOut(
        destructive_enabled=safety.destructive_allowed,
        has_password=await admin_password.has_password(session),
        note=safety.why_blocked(),
    )


@router.get("/safety")
async def get_safety(request: Request) -> SafetyOut:
    async with _factory(request)() as session:
        safety = await app_settings.runtime_safety(session, _settings(request))
        return await _safety_out(session, safety)


@router.put("/safety")
async def set_safety(request: Request, payload: SafetyIn) -> SafetyOut:
    """Turn deletion on or off.

    Turning it ON checks the admin password first -- so a stray click or a stale tab cannot
    arm the tool. Turning it OFF needs nothing, because making Reaper safer should always be
    one click. If no admin password has been set yet, enabling is refused with a message
    pointing at the password step.
    """
    async with _factory(request)() as session:
        if payload.enabled:
            if not await admin_password.has_password(session):
                raise HTTPException(
                    400,
                    "Set an admin password first. It's what confirms turning deletion on.",
                )
            if not await admin_password.verify(session, payload.password or ""):
                raise HTTPException(403, "That password didn't match. Deletion stays off.")
        await app_settings.set_destructive_enabled(session, enabled=payload.enabled)
        await session.commit()
        safety = await app_settings.runtime_safety(session, _settings(request))
        result = await _safety_out(session, safety)
    log.info("safety.destructive_set", enabled=payload.enabled)
    return result


@router.post("/admin-password")
async def set_admin_password(request: Request, payload: AdminPasswordIn) -> dict[str, bool]:
    """Set (or change) the admin password.

    This is the password that later confirms turning deletion on, and it doubles as the
    local sign-in / anti-lockout account. Any signed-in admin may set it.
    """
    # Preserve the caller's own cookie so changing your password does not log you out of
    # the tab you are using; every *other* session for that admin is still revoked.
    keep = read_session_token(request.cookies)
    async with _factory(request)() as session:
        try:
            username = await admin_password.set_password(
                session, payload.password, keep_session_token=keep
            )
        except admin_password.PasswordError as exc:
            raise HTTPException(422, str(exc)) from exc
        await session.commit()
    log.info("safety.admin_password_set", username=username)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


@router.get("/notifications")
async def get_notifications(request: Request) -> NotificationsOut:
    """Whether a Discord webhook is configured. The URL is write-only -- like an API key,
    only its presence is ever reported, never the value."""
    async with _factory(request)() as session:
        has = await app_settings.has_discord_webhook(session, _settings(request))
    return NotificationsOut(has_webhook=has)


@router.put("/notifications")
async def set_notifications(request: Request, payload: NotificationsIn) -> NotificationsOut:
    """Store (or replace) the Discord webhook. The URL is validated to a Discord https host
    and encrypted at rest; it is never read back to the browser."""
    url = _validated_discord_webhook(payload.webhook_url)
    async with _factory(request)() as session:
        await app_settings.set_discord_webhook(session, _box(request), url)
        await session.commit()
    log.info("notifications.webhook_set")
    return NotificationsOut(has_webhook=True)


@router.delete("/notifications")
async def clear_notifications(request: Request) -> NotificationsOut:
    """Forget the webhook -- Leaving Soon warnings go silent until one is set again."""
    async with _factory(request)() as session:
        await app_settings.clear_discord_webhook(session)
        await session.commit()
    log.info("notifications.webhook_cleared")
    return NotificationsOut(has_webhook=False)


@router.post("/notifications/test")
async def test_notifications(request: Request, payload: NotificationsTestIn) -> TestOut:
    """Post a sample embed so an operator can confirm the channel before trusting it.

    Tests the URL in the body (the one about to be saved), or the stored webhook when the
    body omits it. Best-effort like all Discord posting: a bad webhook comes back as
    ``ok: false`` with a reason, it never raises.
    """
    if payload.webhook_url is not None:
        notifier: DiscordNotifier | None = DiscordNotifier(
            _validated_discord_webhook(payload.webhook_url)
        )
    else:
        async with _factory(request)() as session:
            notifier = await build_notifier(session, _box(request), _settings(request))
        if notifier is None:
            return TestOut(ok=False, detail="No Discord webhook is configured to test.")

    # Both branches above leave ``notifier`` set (the stored branch returns early when None).
    assert notifier is not None
    ok = await notifier.post(_sample_embed())
    detail = (
        "Posted a test message to your Discord channel."
        if ok
        else "Could not post to that webhook. Check the URL and that the channel still exists."
    )
    return TestOut(ok=ok, detail=detail)
