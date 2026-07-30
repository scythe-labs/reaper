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

import hashlib
import json
import re
import secrets
from ipaddress import ip_network
from typing import Any
from urllib.parse import SplitResult, urlsplit
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.api import tags as api_tags
from reaper.api.auth import _busy_hashing, _client_ip, _throttled, _verify_admin_password
from reaper.auth.proxy import parse_proxy_networks
from reaper.auth.ratelimit import argon2_gate, password_throttle
from reaper.auth.sessions import resolve_session_from_cookies
from reaper.clients.base import IntegrationError
from reaper.clients.plex import PlexClient, PlexError
from reaper.clients.plextv import PlexConnection, PlexTvClient, connection_identity
from reaper.clock import utcnow
from reaper.config import RuntimeSafety, Settings
from reaper.crypto import SecretBox
from reaper.db.models import AppSetting, InstanceKind, PlexServer
from reaper.notify.discord import DiscordNotifier, Embed, build_notifier
from reaper.services import admin_password, app_settings, instances, leaving_soon
from reaper.services.app_settings import ExpandSeasonsMode
from reaper.services.plex_link import (
    PlexLinkError,
    PlexLinkRetryableError,
    PlexServerChoiceNeededError,
    client_identifier,
    poll_link,
    start_link,
    switch_server,
)
from reaper.services.scheduler import (
    DEFAULT_MAINTENANCE_CRONS,
    MAINTENANCE_JOB_IDS,
    SCAN_JOB_ID,
    SCHEDULABLE_JOB_IDS,
    apply_maintenance_schedule,
    apply_scan_schedule,
    effective_maintenance_cron,
    reschedule_timezone,
    run_maintenance_now,
)

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
            422,
            f'"{value}" is not a service Reaper knows. Use sonarr, radarr, tautulli or seerr.',
        ) from exc


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class InstanceOut(BaseModel):
    id: int
    kind: str
    name: str
    base_url: str
    external_url: str | None = None
    enabled: bool
    verify_tls: bool
    add_import_exclusion: bool
    plex_library_map: dict[str, str]
    service_instance_map: dict[str, int]
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
    add_import_exclusion: bool = False
    # The address links open; blank/omitted means links use base_url. Display only.
    external_url: str | None = None


class InstanceUpdateIn(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # blank/omitted keeps the stored key
    enabled: bool | None = None
    verify_tls: bool | None = None  # omitted keeps the stored setting; explicit False sticks
    add_import_exclusion: bool | None = None  # omitted keeps the stored setting
    # The address links open. Omitted (None) keeps the stored value; a blank string clears it to
    # NULL (links fall back to base_url); a value sets it.
    external_url: str | None = None
    # The HD/4K library map: {root folder path: Plex library title}. Omitted keeps the stored
    # map; a dict (even empty) replaces it, and an empty one clears it. Only Sonarr/Radarr.
    plex_library_map: dict[str, str] | None = None
    # The multi-Seerr requester map: {Seerr service id: Reaper instance id}. Omitted keeps the
    # stored map; a dict (even empty) replaces it, and an empty one clears it. Only Seerr.
    service_instance_map: dict[str, int] | None = None


class InstanceTestIn(BaseModel):
    kind: str
    base_url: str
    api_key: str
    verify_tls: bool = True


class RootFolderOut(BaseModel):
    path: str
    suggested_library: str | None = None


class SeerrServiceOut(BaseModel):
    service_id: int
    kind: str  # "sonarr" | "radarr"
    name: str
    is_4k: bool
    suggested_instance_id: int | None = None


class TestOut(BaseModel):
    ok: bool
    detail: str
    version: str | None = None


class PlexStatusOut(BaseModel):
    linked: bool
    name: str | None = None
    connection_uri: str | None = None
    last_ok_at: str | None = None
    verify_tls: bool = True
    """Whether the server's TLS certificate is checked (mirrors the per-instance
    setting). True when unlinked, since on is the only default."""
    web_url: str = ""
    """Where "open in Plex" links point. Always present, linked or not -- the hosted
    Plex Web default until the operator overrides it."""


# A comment, not the docstring: this class's docstring is the schema description in the API
# reference an operator can browse, so the incident lives here and the plain sentence lives there.
#
# Both fields need three states, and `web_url` had two. `str = ""` could not tell "I am not
# changing the address" from "reset it to the hosted default", so every caller wrote the address
# whether it meant to or not (rule 1). The browser's certificate switch was such a caller and
# filled the field from its CACHED status row, so flipping a setting about certificates wrote back
# an address that may have moved since -- silently reverting it, and pointing every "open in Plex"
# link in the app at plex.tv (#204). `None` means keep; `""` still means reset.
#
# Both sentences of the contract are in the CLASS docstring because that is the only part of this
# model an operator can read: Pydantic harvests the per-field docstrings below only under
# `use_attribute_docstrings`, which this tree does not set (`schemas.py` says so as well). So the
# empty-string reset -- the one way back to the hosted default -- was written beside the field it
# describes and published nowhere, leaving the browsable schema naming neither field's semantics
# while every test stayed green (rule 144).
class PlexUpdateIn(BaseModel):
    """The editable Plex settings. Send only what you are changing: a field you leave out
    keeps its stored value, and an empty web address puts it back to the hosted default."""

    web_url: str | None = None
    """Where "open in Plex" links point."""
    verify_tls: bool | None = None
    """Whether to check the linked server's TLS certificate."""


class PlexLinkStartOut(BaseModel):
    pin_id: int
    auth_url: str


class PlexLinkPollIn(BaseModel):
    pin_id: int
    # Multi-server accounts only: the machine identifier of the owned server the admin
    # picked, echoed back from a "choose_server" response.
    machine_identifier: str | None = None
    # The certificate-check choice made in the link form. Off lets a self-signed HTTPS
    # server be reached at all; it is stored on the server row when the link completes.
    verify_tls: bool = True


class PlexServerChoiceOut(BaseModel):
    name: str
    machine_identifier: str


class PlexLinkPollOut(BaseModel):
    status: str  # "pending" | "retrying" | "ok" | "choose_server"
    server: PlexStatusOut | None = None
    # Present only with status "choose_server": the owned servers to pick from.
    servers: list[PlexServerChoiceOut] | None = None
    # Present only with status "retrying": why this poll could not finish yet, in the
    # operator's words. The sign-in is still good and the browser keeps polling.
    reason: str | None = None


class PlexResourceConnectionOut(BaseModel):
    """One address a server can be reached at, for the connection picker."""

    uri: str
    local: bool
    relay: bool
    protocol: str


class PlexResourceOut(BaseModel):
    name: str
    machine_identifier: str
    current: bool
    """Whether this is the server Reaper is linked to right now."""
    connections: list[PlexResourceConnectionOut]


class PlexResourcesOut(BaseModel):
    source: str
    """``"plex.tv"`` when the listing is live, ``"stored"`` when plex.tv could not be
    reached and this is the linked server's addresses as remembered at link time. Honest
    about staleness rather than pretending a cache is live."""
    servers: list[PlexResourceOut]
    owner_username: str | None = None
    """The signed-in Plex account's name -- the person, not the server. Known only on the
    live path (it comes from plex.tv); ``None`` on the stored fallback, where the UI shows
    the server name instead."""


class PlexServerSwitchIn(BaseModel):
    machine_identifier: str
    verify_tls: bool | None = (
        None  # omitted keeps the stored setting; a self-signed target needs False
    )


class PlexConnectionIn(BaseModel):
    """A connection choice: one of the discovered addresses, or a manually typed one.
    The address is probed before anything is saved -- a typo changes nothing."""

    uri: str
    verify_tls: bool | None = None


class PlexLibraryOut(BaseModel):
    key: int
    title: str
    kind: str
    """``"movie"`` or ``"show"``."""
    enabled: bool


class PlexLibrariesIn(BaseModel):
    """The full set of enabled section keys. Keys not in the stored list are ignored;
    an empty list turns every library off, which just means no shelf is managed
    anywhere -- this scopes a warning feature, never a deletion."""

    enabled_keys: list[int]


class LeavingSoonLastOut(BaseModel):
    at: str
    movies: int
    seasons: int
    applied: bool
    #: Whether the last sync was actually clean: false only for a real per-library
    #: problem, never merely because it ran in preview (unarmed). This, not ``applied``,
    #: is what should color the Jobs page's status dot.
    ok: bool
    #: A short plain-language summary of the last sync, for the Jobs page's resting line.
    result: str


class LeavingSoonSettingsOut(BaseModel):
    enabled: bool
    allow_unarmed: bool
    last: LeavingSoonLastOut | None = None


class LeavingSoonSettingsIn(BaseModel):
    enabled: bool | None = None
    allow_unarmed: bool | None = None


class ScheduledJobOut(BaseModel):
    id: str
    #: The schedule the job runs on now, ``null`` when it is off. For the scan this is the
    #: automatic-scan cron; for an upkeep job, its stored override or built-in default.
    cron: str | None
    #: The built-in default cron, for reference in the editor. ``null`` for the scan, which
    #: has no default (off until the owner sets one).
    default_cron: str | None
    next_run_at: str | None
    #: Whether the job is executing right this moment.
    running: bool
    #: The last completion of this job: when it finished (ISO), whether it succeeded, and a
    #: short plain-language result the Jobs page shows. All ``null`` for a job that has never
    #: run. For the scan, a SUCCESSFUL run is read from the latest snapshot instead (see
    #: ``ScanRow``); these fields are populated for the scan only when a scheduled run
    #: crashed outright and wrote no snapshot, so ScanRow can still show it failed.
    last_run_at: str | None = None
    last_ok: bool | None = None
    last_result: str | None = None


class ScheduleOut(BaseModel):
    jobs: list[ScheduledJobOut]


class JobScheduleIn(BaseModel):
    cron: str | None = None


class SafetyOut(BaseModel):
    destructive_enabled: bool
    """Whether Reaper may delete right now."""
    has_password: bool
    """Whether an admin password has been set. Turning deletion on requires one."""
    note: str | None = None


class SafetyIn(BaseModel):
    enabled: bool
    password: str | None = Field(default=None, max_length=128)
    """Required to turn deletion ON (checked against the admin password). Not needed to
    turn it off -- making Reaper safer is never gated. Bounded, like every field that
    reaches Argon2: hashing unbounded input is a CPU-exhaustion vector."""


class AdminPasswordIn(BaseModel):
    password: str = Field(max_length=128)
    current_password: str | None = Field(default=None, max_length=128)
    """Required when a password already exists. A borrowed signed-in session must not
    be able to swap the arming credential without knowing it."""


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


def _required_web_url(raw: str, *, refusal: str) -> tuple[SplitResult, str]:
    """Rule 84's one shared check: a real http(s) address with a host, else 422.

    A scheme-less paste (``host:8989``), a ``javascript:``/``data:`` value, a scheme with no host
    behind it (``http://``), and a blank string are all refused here rather than carried further
    (rules 84/13). A ``type="url"`` input is not validation, so this is the real check even where
    the browser mirrors it.

    ``refusal`` is the operator's sentence, one per field, because they need to know which box to
    fix and not merely that some URL somewhere was wrong.

    Returns the parsed URL and its host, so a caller that needs the pieces (a probe wanting the
    host and port) reads them from the value this validated instead of re-parsing and re-deciding
    what a missing host means.

    This IS the shared validator rule 84 asks for, and the docstring that stood here until #255
    said there was not one -- naming the four divergent implementations and ``base_url``'s missing
    check. All five now route through here. ``_validated_discord_webhook`` stays separate on
    purpose: it checks a host allow-list, which is a narrower question than URL shape.
    """
    parts = urlsplit(raw.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(422, refusal)
    return parts, parts.hostname


def _require_web_url(raw: str | None, *, refusal: str) -> None:
    """The same check for an OPTIONAL field, where blank is a real answer and passes.

    Blank means the operator turned the setting off (a link address cleared, a Plex web address
    reset to the hosted default), or -- on a required field like ``base_url`` -- that the "this is
    required" refusal downstream is the better sentence than one about URL shape. ``None``, the
    field omitted from a partial update, keeps the stored value and is not our concern.
    """
    if raw is None or not raw.strip():
        return
    _required_web_url(raw, refusal=refusal)


#: The address every Reaper request for a service goes to, so it is the most consequential URL an
#: operator types -- and the one that used to reach storage unchecked, surfacing much later as a
#: connection or scan failure rather than at the box that was wrong (#255).
_BASE_URL_REFUSAL = "The service address must be a full web address, like https://192.0.2.10:8989."


def _validate_external_url(raw: str | None) -> None:
    """The per-service link address Reaper renders into a jump link for every signed-in user."""
    _require_web_url(
        raw, refusal="The external URL must be a full web address, like https://192.0.2.10:8989."
    )


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


@router.get("/instances", tags=[api_tags.SERVICES])
async def list_instances(request: Request) -> list[InstanceOut]:
    async with _factory(request)() as session:
        return [InstanceOut.of(v) for v in await instances.list_instances(session)]


@router.post("/instances", tags=[api_tags.SERVICES])
async def create_instance(request: Request, payload: InstanceCreateIn) -> InstanceOut:
    _require_web_url(payload.base_url, refusal=_BASE_URL_REFUSAL)
    _validate_external_url(payload.external_url)
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
                add_import_exclusion=payload.add_import_exclusion,
                external_url=payload.external_url,
            )
        except instances.InstanceConflictError as exc:
            # A duplicate name is a conflict; anything else the service refused (a blank
            # field, say) is a validation failure -- mirror update_instance's split.
            raise HTTPException(409, str(exc)) from exc
        except instances.InstanceError as exc:
            raise HTTPException(422, str(exc)) from exc
        await session.commit()
        return InstanceOut.of(view)


@router.put("/instances/{instance_id}", tags=[api_tags.SERVICES])
async def update_instance(
    request: Request, instance_id: int, payload: InstanceUpdateIn
) -> InstanceOut:
    _require_web_url(payload.base_url, refusal=_BASE_URL_REFUSAL)
    _validate_external_url(payload.external_url)
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
                add_import_exclusion=payload.add_import_exclusion,
                external_url=payload.external_url,
                plex_library_map=payload.plex_library_map,
                service_instance_map=payload.service_instance_map,
            )
        except instances.InstanceConflictError as exc:
            # A rename into an existing name is a conflict, not a missing resource.
            raise HTTPException(409, str(exc)) from exc
        except instances.InstanceError as exc:
            raise HTTPException(404, str(exc)) from exc
        await session.commit()
        return InstanceOut.of(view)


@router.delete("/instances/{instance_id}", tags=[api_tags.SERVICES])
async def delete_instance(request: Request, instance_id: int) -> dict[str, bool]:
    async with _factory(request)() as session:
        removed = await instances.delete_instance(session, instance_id)
        await session.commit()
    return {"removed": removed}


@router.post("/instances/test", tags=[api_tags.SERVICES])
async def test_new_instance(request: Request, payload: InstanceTestIn) -> TestOut:
    """Test a URL and key before saving, so a typo is caught on the add form."""
    result = await instances.test_connection(
        _kind(payload.kind), payload.base_url, payload.api_key, verify=payload.verify_tls
    )
    return TestOut(ok=result.ok, detail=result.detail, version=result.version)


@router.post("/instances/{instance_id}/test", tags=[api_tags.SERVICES])
async def test_saved_instance(request: Request, instance_id: int) -> TestOut:
    """Test a stored instance and record the outcome on it."""
    async with _factory(request)() as session:
        try:
            result = await instances.test_saved_instance(session, _box(request), instance_id)
        except instances.InstanceError as exc:
            raise HTTPException(404, str(exc)) from exc
        await session.commit()
    return TestOut(ok=result.ok, detail=result.detail, version=result.version)


@router.get("/instances/{instance_id}/root-folders", tags=[api_tags.SERVICES])
async def instance_root_folders(request: Request, instance_id: int) -> list[RootFolderOut]:
    """This instance's root folders, each with a suggested Plex library to prefill the map.

    The suggestion compares each root folder to the Plex libraries' own folders; it only fills
    a control the operator confirms, never binds. Sonarr/Radarr only. A 502 when the instance
    cannot be reached, so the modal can say so rather than show an empty list as if the
    instance had no folders.
    """
    # The Plex side of the suggestion: {library title: folder paths}. Best-effort -- if Plex is
    # not linked or is unreachable the folders still come back, just with no suggestions.
    # Titled, not keyed, because the stored library map itself is titled; two libraries
    # sharing a title contribute BOTH their folder lists here rather than one dropping the
    # other, so the prefill considers everything under that name.
    section_paths: dict[str, list[str]] = {}
    async with _factory(request)() as session:
        server = (await session.execute(select(PlexServer))).scalars().first()
        safety = await app_settings.runtime_safety(session, _settings(request))
    if server is not None and server.connection_uri:
        plex = PlexClient(
            server.connection_uri,
            _box(request).decrypt(server.token_enc),
            safety=safety,
            verify=server.verify_tls,
        )
        try:
            for section in await plex.section_paths():
                section_paths.setdefault(section.title, []).extend(section.locations)
        except PlexError:
            section_paths = {}
        finally:
            await plex.aclose()

    async with _factory(request)() as session:
        try:
            folders = await instances.instance_root_folders(
                session, _box(request), instance_id, section_paths=section_paths
            )
        except instances.InstanceNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except instances.InstanceError as exc:
            raise HTTPException(422, str(exc)) from exc
        except IntegrationError as exc:
            raise HTTPException(502, f"Could not read the folder list: {exc}") from exc
    return [RootFolderOut(path=f.path, suggested_library=f.suggested_library) for f in folders]


@router.get("/instances/{instance_id}/seerr-services", tags=[api_tags.SERVICES])
async def instance_seerr_services(request: Request, instance_id: int) -> list[SeerrServiceOut]:
    """This Seerr portal's Sonarr/Radarr services, each with a suggested Reaper instance.

    The suggestion matches the service's own address to a Reaper instance; it only fills a
    control the operator confirms, never binds. Seerr only. A 502 when the portal cannot be
    reached (or its key is not admin, so settings are refused), so the modal can say so rather
    than show an empty list as if the portal had no services.
    """
    async with _factory(request)() as session:
        try:
            services = await instances.seerr_services(session, _box(request), instance_id)
        except instances.InstanceNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except instances.InstanceError as exc:
            raise HTTPException(422, str(exc)) from exc
        except IntegrationError as exc:
            raise HTTPException(502, f"Could not read the service list: {exc}") from exc
    return [
        SeerrServiceOut(
            service_id=s.service_id,
            kind=s.kind,
            name=s.name,
            is_4k=s.is_4k,
            suggested_instance_id=s.suggested_instance_id,
        )
        for s in services
    ]


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
        verify_tls=server.verify_tls,
        web_url=web_url,
    )


@router.get("/plex", tags=[api_tags.PLEX])
async def plex_status(request: Request) -> PlexStatusOut:
    async with _factory(request)() as session:
        return await _plex_status(session)


@router.put("/plex", tags=[api_tags.PLEX])
async def update_plex_settings(request: Request, payload: PlexUpdateIn) -> PlexStatusOut:
    """Save the Plex settings: the "open in Plex" web address (empty resets to the
    hosted default) and, once a server is linked, the certificate check. Each field is
    independent, and one left out is left alone."""
    cleaned = payload.web_url.strip() if payload.web_url is not None else None
    _require_web_url(
        cleaned,
        refusal="The Plex web address must be a full web address, like https://192.0.2.10:32400.",
    )
    async with _factory(request)() as session:
        # Only when the caller sent the field. `cleaned` is `None` for "not sent" and `""` for
        # "reset to the hosted default", and those are different requests (rule 1).
        if cleaned is not None:
            await app_settings.set_plex_web_url(session, cleaned or None)
        if payload.verify_tls is not None:
            server = (await session.execute(select(PlexServer))).scalars().first()
            if server is None:
                raise HTTPException(
                    422, "No Plex server is linked yet. Link one before changing this."
                )
            server.verify_tls = payload.verify_tls
        status = await _plex_status(session)
        await session.commit()
    log.info("settings.plex_saved")
    return status


@router.post("/plex/link/start", tags=[api_tags.PLEX])
async def plex_link_start(request: Request) -> PlexLinkStartOut:
    async with _factory(request)() as session:
        safety = await app_settings.runtime_safety(session, _settings(request))
    start = await start_link(_factory(request), safety=safety)
    return PlexLinkStartOut(pin_id=start.pin_id, auth_url=start.auth_url)


@router.post("/plex/link/poll", tags=[api_tags.PLEX])
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
            verify_tls=payload.verify_tls,
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
    except PlexLinkRetryableError as exc:
        # The sign-in was approved but the server could not be reached this instant -- it
        # may just be restarting. ``poll_link`` deliberately KEEPS the pending login for
        # exactly this case, so the answer must not be an error: the browser aborts its
        # poll loop on any thrown status, which would strand the still-valid sign-in and
        # send the operator back through the whole approval round trip (B2-14). Answered
        # as a non-final status instead, so the loop keeps polling until it works or the
        # deadline passes.
        return PlexLinkPollOut(status="retrying", reason=str(exc))
    except PlexLinkError as exc:
        raise HTTPException(400, str(exc)) from exc

    if linked is None:
        return PlexLinkPollOut(status="pending")
    async with _factory(request)() as session:
        return PlexLinkPollOut(status="ok", server=await _plex_status(session))


@router.delete("/plex", tags=[api_tags.PLEX])
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


async def _linked_server(session: AsyncSession) -> PlexServer:
    server = (await session.execute(select(PlexServer))).scalars().first()
    if server is None:
        raise HTTPException(400, "No Plex server is linked yet. Link one first.")
    return server


@router.get("/plex/resources", tags=[api_tags.PLEX])
async def plex_resources(request: Request) -> PlexResourcesOut:
    """The servers this Plex account owns and every address each can be reached at,
    for the server and connection pickers.

    Asks plex.tv live using the stored token. When plex.tv cannot be reached, falls
    back to the linked server's addresses as remembered at link time -- marked
    ``source: "stored"`` so the UI can say the list may be stale rather than imply it
    is fresh.
    """
    async with _factory(request)() as session:
        server = await _linked_server(session)
        current_id = server.machine_identifier
        token = _box(request).decrypt(server.token_enc)
        cid = await client_identifier(session)
        safety = await app_settings.runtime_safety(session, _settings(request))
        stored_connections = json.loads(server.connections_json or "[]")
        stored_name = server.name
        await session.commit()

    try:
        async with PlexTvClient(cid, safety=safety) as plextv:
            owned = await plextv.owned_servers(token)
            # The signed-in person's name, for the "who you're linked as" line. Same token,
            # same live call as the server list; a failure here degrades to stored below.
            account = await plextv.account(token)
    except IntegrationError as exc:
        log.warning("plex.resources_unreachable", error=str(exc))
        return PlexResourcesOut(
            source="stored",
            servers=[
                PlexResourceOut(
                    name=stored_name,
                    machine_identifier=current_id,
                    current=True,
                    connections=[
                        PlexResourceConnectionOut(
                            uri=str(c.get("uri") or ""),
                            local=bool(c.get("local")),
                            relay=bool(c.get("relay")),
                            protocol=str(c.get("protocol") or "https"),
                        )
                        for c in stored_connections
                        if c.get("uri")
                    ],
                )
            ],
        )

    return PlexResourcesOut(
        source="plex.tv",
        owner_username=account.username,
        servers=[
            PlexResourceOut(
                name=r.name,
                machine_identifier=r.client_identifier,
                current=r.client_identifier == current_id,
                connections=[
                    PlexResourceConnectionOut(
                        uri=c.uri, local=c.local, relay=c.relay, protocol=c.protocol
                    )
                    for c in r.preferred_connections()
                ],
            )
            for r in owned
        ],
    )


@router.put("/plex/server", tags=[api_tags.PLEX])
async def plex_switch_server(request: Request, payload: PlexServerSwitchIn) -> PlexStatusOut:
    """Point Reaper at a different server the same account owns.

    Resolved against the live OWNED list from plex.tv and probed before anything is
    saved. Switching clears the library choices and the announced set -- they were keyed
    to the old server and would silently mis-target the new one. The certificate check
    rides along when given, so switching to a self-signed server can turn it off in the
    same step rather than being stuck on the old server's setting.
    """
    async with _factory(request)() as session:
        safety = await app_settings.runtime_safety(session, _settings(request))
    try:
        await switch_server(
            _factory(request),
            _box(request),
            machine_identifier=payload.machine_identifier,
            safety=safety,
            verify_tls=payload.verify_tls,
        )
    except PlexLinkRetryableError as exc:
        raise HTTPException(502, str(exc)) from exc
    except PlexLinkError as exc:
        raise HTTPException(400, str(exc)) from exc

    async with _factory(request)() as session:
        status = await _plex_status(session)
    log.info("plex.server_switched")
    return status


@router.put("/plex/connection", tags=[api_tags.PLEX])
async def plex_set_connection(request: Request, payload: PlexConnectionIn) -> PlexStatusOut:
    """Save how Reaper reaches the linked server: a discovered address or a manual one.

    The address is probed with the stored token before anything is written, so a typo
    or a dead address changes nothing. The certificate check rides along when given
    (a self-signed HTTPS server needs it off to be probed at all).

    The probe also asks the server who it is and refuses anything but the linked one.
    This address is typed by hand, so it can be any Plex on the network; saving one
    that belongs to a different server would point Reaper's Leaving Soon writes and its
    Never-Reap read at a library nobody asked it to touch (B-10). A server that will
    not say who it is is refused for the same reason: unconfirmed is not confirmed.
    """
    uri = payload.uri.strip().rstrip("/")
    # The required form: this address is dialed, not stored for display, so a blank one is refused
    # here rather than carried into the probe. `host` is the validated host the probe needs.
    parts, host = _required_web_url(
        uri, refusal="The server address must be a full web address, like https://192.0.2.10:32400."
    )

    async with _factory(request)() as session:
        server = await _linked_server(session)
        token = _box(request).decrypt(server.token_enc)
        verify = payload.verify_tls if payload.verify_tls is not None else server.verify_tls
        expected = server.machine_identifier
        expected_name = server.name

    probe = PlexConnection(
        uri=uri,
        address=host,
        port=parts.port or (443 if parts.scheme == "https" else 32400),
        local=False,
        relay=False,
        protocol=parts.scheme,
    )
    answered = await connection_identity(probe, token, verify=verify)
    if answered is None:
        raise HTTPException(
            502,
            "Couldn't reach a Plex server at that address, so nothing was changed. "
            "Check the address and port, and whether the certificate check should be off.",
        )
    if answered != expected:
        log.warning("plex.connection_wrong_server")
        raise HTTPException(
            409,
            f"That address is a different Plex server, so nothing was changed. Reaper is "
            f"linked to {expected_name}; use an address for that server, or link the other "
            f"one instead.",
        )

    async with _factory(request)() as session:
        server = await _linked_server(session)
        server.connection_uri = uri
        if payload.verify_tls is not None:
            server.verify_tls = payload.verify_tls
        server.last_ok_at = utcnow()
        status = await _plex_status(session)
        await session.commit()
    log.info("plex.connection_saved")
    return status


# ---------------------------------------------------------------------------
# Plex libraries
# ---------------------------------------------------------------------------


def _libraries_out(stored: list[dict[str, Any]]) -> list[PlexLibraryOut]:
    return [
        PlexLibraryOut(
            key=int(lib.get("key", 0)),
            title=str(lib.get("title", "")),
            kind=str(lib.get("kind", "movie")),
            enabled=bool(lib.get("enabled", True)),
        )
        for lib in stored
    ]


@router.get("/plex/libraries", tags=[api_tags.PLEX])
async def plex_libraries(request: Request) -> list[PlexLibraryOut]:
    """The video libraries as last synced, each with its enabled flag. Empty until the
    first sync."""
    async with _factory(request)() as session:
        return _libraries_out(await app_settings.get_plex_libraries(session))


@router.post("/plex/libraries/sync", tags=[api_tags.PLEX])
async def sync_plex_libraries(request: Request) -> list[PlexLibraryOut]:
    """Refresh the library list from the server.

    Merge, not replace: a library the operator already turned off stays off across a
    re-sync; a newly discovered library starts ON, so the default install marks every
    movie and TV library without further setup. Libraries that no longer exist on the
    server are dropped.
    """
    async with _factory(request)() as session:
        server = await _linked_server(session)
        safety = await app_settings.runtime_safety(session, _settings(request))
        stored = {
            int(lib["key"]): bool(lib.get("enabled", True))
            for lib in await app_settings.get_plex_libraries(session)
        }
        plex = PlexClient(
            server.connection_uri,
            _box(request).decrypt(server.token_enc),
            safety=safety,
            verify=server.verify_tls,
        )

    try:
        sections = await plex.video_sections()
    except PlexError as exc:
        raise HTTPException(502, f"Could not reach Plex: {exc}") from exc
    finally:
        await plex.aclose()

    merged = [
        {
            "key": s.key,
            "title": s.title,
            "kind": s.kind,
            "enabled": stored.get(s.key, True),
        }
        for s in sections
    ]
    async with _factory(request)() as session:
        await app_settings.set_plex_libraries(session, merged)
        result = _libraries_out(await app_settings.get_plex_libraries(session))
        await session.commit()
    log.info("plex.libraries_synced", count=len(merged))
    return result


@router.put("/plex/libraries", tags=[api_tags.PLEX])
async def set_plex_libraries(request: Request, payload: PlexLibrariesIn) -> list[PlexLibraryOut]:
    """Turn libraries on or off. The keys name the enabled set; everything else stored
    turns off. Unknown keys are ignored rather than invented.

    A library that just turned OFF gets one last empty-reconcile (when Reaper is allowed
    to write), so its "Leaving Soon" shelf does not linger unmanaged -- the reconcile
    never visits a disabled library again, and a stale warning shelf is a lie.
    """
    enabled = {int(k) for k in payload.enabled_keys}
    async with _factory(request)() as session:
        stored = await app_settings.get_plex_libraries(session)
        turned_off = [
            lib for lib in stored if lib.get("enabled") and int(lib.get("key", 0)) not in enabled
        ]
        for lib in stored:
            lib["enabled"] = int(lib.get("key", 0)) in enabled
        await app_settings.set_plex_libraries(session, stored)
        result = _libraries_out(await app_settings.get_plex_libraries(session))
        await session.commit()

    if turned_off:
        # Best-effort, after the choice is committed: failure is logged inside, never
        # raised, and never blocks the settings change itself.
        await leaving_soon.cleanup_sections(
            _factory(request), _settings(request), _box(request), sections=turned_off
        )

    log.info("plex.libraries_set", enabled=len(enabled), cleaned=len(turned_off))
    return result


# ---------------------------------------------------------------------------
# Leaving Soon
# ---------------------------------------------------------------------------


async def _leaving_soon_out(session: AsyncSession, settings: Settings) -> LeavingSoonSettingsOut:
    last = await app_settings.get_leaving_soon_last(session)
    return LeavingSoonSettingsOut(
        enabled=await app_settings.leaving_soon_enabled(session),
        allow_unarmed=await app_settings.leaving_soon_unarmed(session, settings),
        last=LeavingSoonLastOut(
            at=str(last.get("at", "")),
            movies=int(last.get("movies", 0)),
            seasons=int(last.get("seasons", 0)),
            applied=bool(last.get("applied", False)),
            ok=bool(last.get("ok", True)),
            result=str(last.get("result", "")),
        )
        if last
        else None,
    )


@router.get("/leaving-soon", tags=[api_tags.JOBS])
async def get_leaving_soon_settings(request: Request) -> LeavingSoonSettingsOut:
    async with _factory(request)() as session:
        return await _leaving_soon_out(session, _settings(request))


@router.put("/leaving-soon", tags=[api_tags.JOBS])
async def set_leaving_soon_settings(
    request: Request, payload: LeavingSoonSettingsIn
) -> LeavingSoonSettingsOut:
    """Flip the Leaving Soon switches. No password: these can only touch the shelf --
    a collection and a label -- never a file.

    Turning the shelf OFF runs one last pass that takes everything off it (when Reaper
    is allowed to write), so nothing stale lingers in the library.
    """
    async with _factory(request)() as session:
        was_enabled = await app_settings.leaving_soon_enabled(session)
        if payload.enabled is not None:
            await app_settings.set_leaving_soon_enabled(session, enabled=payload.enabled)
        if payload.allow_unarmed is not None:
            await app_settings.set_leaving_soon_unarmed(session, allowed=payload.allow_unarmed)
        await session.commit()

    if was_enabled and payload.enabled is False:
        # Best-effort: takes everything off the shelves so nothing stale lingers.
        # Failure is logged inside, never raised -- turning a warning off must succeed.
        await leaving_soon.cleanup_shelves(_factory(request), _settings(request), _box(request))

    async with _factory(request)() as session:
        result = await _leaving_soon_out(session, _settings(request))
    log.info(
        "leaving_soon.settings_saved",
        enabled=payload.enabled,
        allow_unarmed=payload.allow_unarmed,
    )
    return result


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


@router.get("/schedule", tags=[api_tags.JOBS])
async def get_schedule(request: Request) -> ScheduleOut:
    """Every schedulable job, in display order: the automatic scan and the upkeep jobs.

    A job the owner turned off is still listed (with ``cron`` null and no next run), so the
    Jobs page can offer to schedule it again -- it is never dropped from the list just
    because it is off.
    """
    scheduler = request.app.state.scheduler
    running: set[str] = getattr(request.app.state, "running_jobs", set())
    async with _factory(request)() as session:
        scan_cron = await app_settings.get_scan_schedule(session)
        maintenance = await app_settings.get_maintenance_schedules(session)
        last_runs = await app_settings.get_job_last_runs(session)

    jobs = []
    for job_id in SCHEDULABLE_JOB_IDS:
        if job_id == SCAN_JOB_ID:
            cron = scan_cron
            default_cron = None
        else:
            cron = effective_maintenance_cron(job_id, maintenance)
            default_cron = DEFAULT_MAINTENANCE_CRONS[job_id]
        job = scheduler.get_job(job_id)
        last = last_runs.get(job_id)
        jobs.append(
            ScheduledJobOut(
                id=job_id,
                cron=cron,
                default_cron=default_cron,
                next_run_at=job.next_run_time.isoformat() if job and job.next_run_time else None,
                running=job_id in running,
                last_run_at=last.get("at") if last else None,
                last_ok=last.get("ok") if last else None,
                last_result=last.get("result") if last else None,
            )
        )
    return ScheduleOut(jobs=jobs)


@router.put("/jobs/{job_id}/schedule", tags=[api_tags.JOBS])
async def set_job_schedule(request: Request, job_id: str, payload: JobScheduleIn) -> ScheduleOut:
    """Set (or turn off) one job's schedule. The scan and every upkeep job are read-only, so
    changing when they run -- or turning one off -- is always safe and never gated.

    A malformed cron is a 422 with the reason: an owner who thinks they scheduled a nightly
    run must not silently get nothing. An unknown job id is a 404.
    """
    cron = (payload.cron or "").strip() or None
    scheduler = request.app.state.scheduler
    # Read the cron in the current server zone, so a job set for 2 AM fires at 2 AM there.
    async with _factory(request)() as session:
        job_tz = ZoneInfo(await app_settings.get_timezone(session, _settings(request)))
    if job_id == SCAN_JOB_ID:
        try:
            apply_scan_schedule(
                scheduler,
                cron,
                settings=_settings(request),
                session_factory=_factory(request),
                cache_engine=request.app.state.cache_engine,
                secret_box=_box(request),
                timezone=job_tz,
            )
        except ValueError as exc:
            raise HTTPException(
                422, f"That is not a valid schedule: {exc}. Use cron form, e.g. '30 4 * * *'."
            ) from exc
        async with _factory(request)() as session:
            await app_settings.set_scan_schedule(session, cron)
            await session.commit()
    elif job_id in MAINTENANCE_JOB_IDS:
        try:
            apply_maintenance_schedule(
                scheduler,
                job_id,
                cron,
                cache_engine=request.app.state.cache_engine,
                data_dir=_settings(request).data_dir,
                session_factory=_factory(request),
                secret_box=_box(request),
                timezone=job_tz,
            )
        except ValueError as exc:
            raise HTTPException(
                422, f"That is not a valid schedule: {exc}. Use cron form, e.g. '30 4 * * *'."
            ) from exc
        async with _factory(request)() as session:
            await app_settings.set_maintenance_schedule(session, job_id, cron)
            await session.commit()
    else:
        raise HTTPException(404, f'No schedulable job named "{job_id}".')

    log.info("schedule.updated", job=job_id, cron=cron)
    return await get_schedule(request)


@router.post("/jobs/{job_id}/run", tags=[api_tags.JOBS])
async def run_job(request: Request, job_id: str) -> dict[str, str]:
    """Run an upkeep job now, whether or not it is on a schedule.

    A scheduled job is nudged to fire immediately; one the owner turned off is run once
    without turning its schedule back on. Either way the schedule is left as it was. These
    are read-only upkeep jobs (refreshing ratings and lists, sweeping watch history) -- none
    can delete anything. The library scan is deliberately absent: it runs through
    ``/api/scan/start`` as a polled background job so the UI can show progress.
    """
    if job_id not in MAINTENANCE_JOB_IDS:
        raise HTTPException(404, f'No runnable job named "{job_id}".')
    run_maintenance_now(
        request.app.state.scheduler,
        job_id,
        cache_engine=request.app.state.cache_engine,
        data_dir=_settings(request).data_dir,
        session_factory=_factory(request),
        secret_box=_box(request),
    )
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


@router.get("/safety", tags=[api_tags.SECURITY])
async def get_safety(request: Request) -> SafetyOut:
    async with _factory(request)() as session:
        safety = await app_settings.runtime_safety(session, _settings(request))
        return await _safety_out(session, safety)


@router.put("/safety", tags=[api_tags.SECURITY])
async def set_safety(request: Request, payload: SafetyIn) -> SafetyOut:
    """Turn deletion on or off.

    Turning it ON checks the admin password first -- so a stray click or a stale tab cannot
    arm the tool. Turning it OFF needs nothing, because making Reaper safer should always be
    one click. If no admin password has been set yet, enabling is refused with a message
    pointing at the password step.

    The verify runs behind the same per-IP + per-account lockout and Argon2 concurrency
    gate as login (``password_throttle`` / ``argon2_gate``): arming is a password-guessing
    surface too, and Argon2 is expensive by design.
    """
    keys = (f"ip:{_client_ip(request)}", "account:safety-arm")
    async with _factory(request)() as session:
        if payload.enabled:
            if not await admin_password.has_password(session):
                raise HTTPException(
                    400,
                    "Set an admin password first. It's what confirms turning deletion on.",
                )
            _throttled(password_throttle, *keys)
            ok = await _verify_admin_password(session, payload.password or "")
            if not ok:
                for key in keys:
                    password_throttle.record_failure(key)
                raise HTTPException(403, "That password didn't match. Deletion stays off.")
            for key in keys:
                password_throttle.record_success(key)
        await app_settings.set_destructive_enabled(session, enabled=payload.enabled)
        await session.commit()
        safety = await app_settings.runtime_safety(session, _settings(request))
        result = await _safety_out(session, safety)
    log.info("safety.destructive_set", enabled=payload.enabled)
    return result


@router.post("/admin-password", tags=[api_tags.SECURITY])
async def set_admin_password(request: Request, payload: AdminPasswordIn) -> dict[str, bool]:
    """Set (or change) the admin password.

    This is the password that later confirms turning deletion on, and it doubles as the
    local sign-in / anti-lockout account. Setting the FIRST password needs only a
    signed-in session; changing an existing one also requires the current password, so a
    borrowed session or an unattended tab cannot quietly swap the arming credential.
    Verify and hash both run behind the login's lockout and Argon2 concurrency gate.
    """
    keys = (f"ip:{_client_ip(request)}", "account:admin-password")
    async with _factory(request)() as session:
        # Preserve the caller's own cookie so changing your password does not log you out
        # of the tab you are using; every *other* session for that admin is still revoked.
        # It has to be the token that actually RESOLVES: with two cookie names in play, a
        # stale cookie under the other name would be the one spared here while the live
        # session was revoked, signing the operator out of the very tab they were in.
        _, keep = await resolve_session_from_cookies(session, request.cookies)
        if await admin_password.has_password(session):
            _throttled(password_throttle, *keys)
            ok = await _verify_admin_password(session, payload.current_password or "")
            if not ok:
                for key in keys:
                    password_throttle.record_failure(key)
                raise HTTPException(403, "The current password didn't match. Nothing was changed.")
            for key in keys:
                password_throttle.record_success(key)
        # Hashing the NEW password is one more Argon2 run, so it takes its own slot.
        if not argon2_gate.acquire():
            raise _busy_hashing()
        try:
            username = await admin_password.set_password(
                session, payload.password, keep_session_token=keep
            )
        except admin_password.PasswordError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            argon2_gate.release()
        await session.commit()
    log.info("safety.admin_password_set", username=username)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


@router.get("/notifications", tags=[api_tags.NOTIFICATIONS])
async def get_notifications(request: Request) -> NotificationsOut:
    """Whether a Discord webhook is configured. The URL is write-only -- like an API key,
    only its presence is ever reported, never the value."""
    async with _factory(request)() as session:
        has = await app_settings.has_discord_webhook(session, _box(request), _settings(request))
    return NotificationsOut(has_webhook=has)


@router.put("/notifications", tags=[api_tags.NOTIFICATIONS])
async def set_notifications(request: Request, payload: NotificationsIn) -> NotificationsOut:
    """Store (or replace) the Discord webhook. The URL is validated to a Discord https host
    and encrypted at rest; it is never read back to the browser."""
    url = _validated_discord_webhook(payload.webhook_url)
    async with _factory(request)() as session:
        await app_settings.set_discord_webhook(session, _box(request), url)
        await session.commit()
    log.info("notifications.webhook_set")
    return NotificationsOut(has_webhook=True)


@router.delete("/notifications", tags=[api_tags.NOTIFICATIONS])
async def clear_notifications(request: Request) -> NotificationsOut:
    """Forget the webhook -- Leaving Soon warnings go silent until one is set again."""
    async with _factory(request)() as session:
        await app_settings.clear_discord_webhook(session)
        await session.commit()
    log.info("notifications.webhook_cleared")
    return NotificationsOut(has_webhook=False)


@router.post("/notifications/test", tags=[api_tags.NOTIFICATIONS])
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


# ---------------------------------------------------------------------------
# General: application identity, the API key, reverse proxy trust
# ---------------------------------------------------------------------------


#: A six-digit hex color, ``#rrggbb``. The one shape the accent may take.
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class GeneralSettingsOut(BaseModel):
    application_name: str
    application_url: str | None = None
    timezone: str
    """The server time zone every timed job runs on, as an IANA name (e.g.
    ``America/New_York``). The effective value: the stored setting, else the env seed, else
    the host's own zone."""
    accent_color: str
    """The UI accent as ``#rrggbb``; the built-in sky blue until changed."""
    api_key_set: bool
    """Whether a key exists at all -- the value itself only leaves through the
    dedicated reveal route, never rides along on a settings read."""
    expand_seasons_mode: ExpandSeasonsMode
    """Which screens the review queue opens each show's season list expanded on. A display
    preference; ``off`` until the operator picks a screen."""
    default_spare_days: int
    """How long a plain Spare press keeps an item, in days. ``0`` means forever -- the shipped
    default and the original behavior. A single title can still be spared for a different length
    from its Spare menu; this is only what the button does by default."""
    proxy_trust_enabled: bool
    trusted_proxies: list[str]


class GeneralSettingsIn(BaseModel):
    """Partial update: only the fields present change."""

    application_name: str | None = Field(default=None, max_length=60)
    application_url: str | None = Field(default=None, max_length=500)
    timezone: str | None = Field(default=None, max_length=64)
    """An IANA time-zone name, validated to a real zone at the edge. ``None`` leaves it
    unchanged."""
    accent_color: str | None = Field(default=None, max_length=7)
    expand_seasons_mode: ExpandSeasonsMode | None = None
    """Which screens the review queue opens seasons on. ``None`` leaves it unchanged."""
    default_spare_days: int | None = Field(default=None, ge=0, le=3650)
    """Days a plain Spare keeps an item; ``0`` = forever. ``None`` leaves it unchanged."""
    proxy_trust_enabled: bool | None = None
    trusted_proxies: list[str] | None = Field(default=None, max_length=20)


class ApiKeyOut(BaseModel):
    key: str


async def _general_out(session: AsyncSession, settings: Settings) -> GeneralSettingsOut:
    return GeneralSettingsOut(
        application_name=await app_settings.get_application_name(session),
        application_url=await app_settings.get_application_url(session),
        timezone=await app_settings.get_timezone(session, settings),
        accent_color=await app_settings.get_accent_color(session),
        api_key_set=(await session.get(AppSetting, app_settings.API_KEY_KEY)) is not None,
        expand_seasons_mode=await app_settings.get_expand_seasons_mode(session),
        default_spare_days=await app_settings.get_default_spare_days(session),
        proxy_trust_enabled=await app_settings.proxy_trust_enabled(session, settings),
        trusted_proxies=await app_settings.get_trusted_proxies(session, settings),
    )


async def _refresh_proxy_state(request: Request, session: AsyncSession) -> None:
    """Re-derive the middleware's live trusted-proxy networks from what is stored.

    The middleware reads ``app.state.trusted_proxies`` per request, so this is what
    makes a General save take effect immediately. Disabled means an empty tuple:
    forwarded headers from anywhere are ignored, exactly like a fresh install.
    """
    settings = _settings(request)
    if await app_settings.proxy_trust_enabled(session, settings):
        entries = await app_settings.get_trusted_proxies(session, settings)
        request.app.state.trusted_proxies = parse_proxy_networks(entries)
    else:
        request.app.state.trusted_proxies = ()


async def _apply_timezone_to_scheduler(request: Request, name: str) -> None:
    """Move the live scheduler's timed jobs onto a new server time zone, so a change in
    Settings -> General takes effect now, not at the next restart. ``name`` is already
    validated to a real zone by the caller.

    A test app may run without a scheduler; if so there is nothing to move.
    """
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return
    async with _factory(request)() as session:
        scan_cron = await app_settings.get_scan_schedule(session)
        maintenance = await app_settings.get_maintenance_schedules(session)
    reschedule_timezone(
        scheduler,
        ZoneInfo(name),
        settings=_settings(request),
        session_factory=_factory(request),
        cache_engine=request.app.state.cache_engine,
        secret_box=_box(request),
        data_dir=_settings(request).data_dir,
        scan_cron=scan_cron,
        maintenance=maintenance,
    )


@router.get("/general", tags=[api_tags.GENERAL])
async def get_general(request: Request) -> GeneralSettingsOut:
    async with _factory(request)() as session:
        return await _general_out(session, _settings(request))


@router.put("/general", tags=[api_tags.GENERAL])
async def put_general(request: Request, payload: GeneralSettingsIn) -> GeneralSettingsOut:
    """Save the General settings. Partial: only the fields sent change.

    The application URL must be a plain http(s) address (or empty to clear it), and
    every trusted-proxy entry must parse as an address or a range -- refused with a
    plain message otherwise, and nothing is changed.
    """
    async with _factory(request)() as session:
        if payload.application_url is not None:
            _require_web_url(
                payload.application_url,
                refusal=(
                    "The application URL must be a full web address, like "
                    "https://reaper.example.com"
                ),
            )
        if payload.trusted_proxies is not None:
            for entry in payload.trusted_proxies:
                cleaned_entry = entry.strip()
                if not cleaned_entry:
                    continue
                try:
                    ip_network(cleaned_entry, strict=False)
                except ValueError:
                    raise HTTPException(
                        422,
                        f'"{cleaned_entry}" is not an address or a range. Use entries '
                        "like 172.16.0.1 or 172.16.0.0/12.",
                    ) from None

        if payload.accent_color is not None:
            cleaned_color = payload.accent_color.strip()
            if cleaned_color and not _HEX_COLOR.match(cleaned_color):
                raise HTTPException(
                    422,
                    "The accent color must be a hex code like #25c3ff.",
                )

        cleaned_timezone: str | None = None
        if payload.timezone is not None:
            cleaned_timezone = payload.timezone.strip()
            if not cleaned_timezone or not app_settings.is_valid_timezone(cleaned_timezone):
                raise HTTPException(
                    422,
                    "That is not a known time zone. Pick one from the list.",
                )

        if payload.application_name is not None:
            await app_settings.set_application_name(session, payload.application_name)
        if payload.application_url is not None:
            await app_settings.set_application_url(session, payload.application_url)
        if cleaned_timezone is not None:
            await app_settings.set_timezone(session, cleaned_timezone)
        if payload.accent_color is not None:
            await app_settings.set_accent_color(session, payload.accent_color)
        if payload.expand_seasons_mode is not None:
            await app_settings.set_expand_seasons_mode(session, mode=payload.expand_seasons_mode)
        if payload.default_spare_days is not None:
            await app_settings.set_default_spare_days(session, days=payload.default_spare_days)
        if payload.proxy_trust_enabled is not None:
            await app_settings.set_proxy_trust_enabled(session, enabled=payload.proxy_trust_enabled)
        if payload.trusted_proxies is not None:
            await app_settings.set_trusted_proxies(session, payload.trusted_proxies)
        await session.commit()
        await _refresh_proxy_state(request, session)
        if cleaned_timezone is not None:
            await _apply_timezone_to_scheduler(request, cleaned_timezone)
        result = await _general_out(session, _settings(request))
    log.info("settings.general_saved")
    return result


@router.get("/general/api-key", tags=[api_tags.GENERAL])
async def reveal_api_key(request: Request) -> ApiKeyOut:
    """The stored key, for the Show button. Session-only: the middleware fences this
    route away from API-key auth, so a key cannot read or manage itself."""
    async with _factory(request)() as session:
        key = await app_settings.get_api_key(session, _box(request))
    if key is None:
        raise HTTPException(404, "No API key exists yet. Generate one first.")
    return ApiKeyOut(key=key)


@router.post("/general/api-key", tags=[api_tags.GENERAL])
async def generate_api_key(request: Request) -> ApiKeyOut:
    """Generate the key, replacing any previous one. The old key stops working the
    moment this returns, so rotating revokes the previous key. It does not CLOSE the
    header-credential lane, though: there is always a working key afterwards. Turning the
    lane off is what ``DELETE`` below is for."""
    key = secrets.token_urlsafe(32)
    async with _factory(request)() as session:
        await app_settings.set_api_key(session, _box(request), key)
        await session.commit()
    request.app.state.api_key_digest = hashlib.sha256(key.encode("utf-8")).digest()
    log.info("settings.api_key_rotated")
    return ApiKeyOut(key=key)


@router.delete("/general/api-key", tags=[api_tags.GENERAL])
async def remove_api_key(request: Request) -> dict[str, bool]:
    """Close the header-credential lane: delete the key, and stop honoring it now.

    Rotating replaces one working key with another, so an operator who generated a key for
    a one-off script had no way to shut the lane again (PR2-2). This is that way.

    The stored digest on the app is cleared in the same breath. Auth reads that digest, not
    the database, so a deleted key would otherwise keep working until the next restart.
    Session-only, like every write here: the middleware is deny-by-default for anything
    that is not a safe method, so a key cannot delete itself or anyone else's.
    """
    async with _factory(request)() as session:
        await app_settings.clear_api_key(session)
        await session.commit()
    request.app.state.api_key_digest = None
    log.info("settings.api_key_removed")
    return {"removed": True}
