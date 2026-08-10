# SPDX-License-Identifier: AGPL-3.0-or-later
"""Configuring Reaper from the web UI.

Everything an operator needs to stand the tool up and keep it running lives here: the
external services it reads from, the schedule, and the safety switch. The Plex link and the
library shelf moved to ``api/plex.py``, which keeps this prefix and restates the two facts
below for its own readers.

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
import os
import re
import secrets
from ipaddress import ip_network
from typing import Literal
from urllib.parse import SplitResult, urlsplit
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper import launcher
from reaper.api import tags as api_tags
from reaper.api.deps import (
    busy_hashing,
    client_ip,
    require_admin_password,
    runtime_settings,
    secret_box,
    session_factory,
)
from reaper.api.schemas import JobRunOut, OkOut, RemovedOut
from reaper.auth.proxy import parse_proxy_networks
from reaper.auth.ratelimit import argon2_gate
from reaper.auth.sessions import (
    resolve_session_from_cookies,
    session_via_recovery,
    spend_recovery_mark,
)
from reaper.buildinfo import env_flag
from reaper.clients.base import IntegrationError
from reaper.clients.plex import PlexClient, PlexError
from reaper.config import RuntimeSafety, Settings
from reaper.db.models import AppSetting, InstanceKind, PlexServer
from reaper.notify.discord import DiscordNotifier, Embed, build_notifier
from reaper.services import (
    admin_password,
    app_settings,
    instances,
    leaving_soon,
)
from reaper.services.app_settings import ExpandSeasonsMode
from reaper.services.scheduler import (
    DEFAULT_MAINTENANCE_CRONS,
    MAINTENANCE_JOB_IDS,
    SCAN_JOB_ID,
    SCHEDULABLE_JOB_IDS,
    apply_maintenance_schedule,
    apply_scan_schedule,
    apply_stored_schedules,
    effective_maintenance_cron,
    run_maintenance_now,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/settings")


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
    # No `api_path_prefix` here: no route writes it, so it could only ever publish its
    # default (#274, rule 25). `db.models.Instance.api_path_prefix` holds the reasoning.
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
    # Both maps are accepted at creation because the add form maps the service before saving
    # it: a passing connection test hands back the root folders (or the portal's services), so
    # the mapping is made on the screen that adds the connection rather than only on a later
    # edit. Omitted or empty stores NULL, which reads back as "no map".
    plex_library_map: dict[str, str] | None = None
    service_instance_map: dict[str, int] | None = None


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
    """The verdict on one connection test: did it reach the service, and what to say about it.

    What a test of a SAVED instance and a webhook test can both answer. The mapping a
    pre-save probe reads is on :class:`InstanceProbeOut` below, because only that route can
    answer it and a shared shape said otherwise: the published contract had a Discord webhook
    test declaring it may return Sonarr root folders (rule 25)."""

    ok: bool
    detail: str
    version: str | None = None


class InstanceProbeOut(TestOut):
    """The pre-save test on the add form, which also reads what the connection has to map.

    Only this route can: it is the only caller with no instance id, so the mapping has to come
    back on the same pass that proved the credentials. That is what lets the form map a service
    before it is saved.
    """

    # Only one is ever populated -- a test is for exactly one kind -- and both stay empty on a
    # failed test, since nothing was reached to read them from.
    root_folders: list[RootFolderOut] = []
    seerr_services: list[SeerrServiceOut] = []
    # Why the list above is empty, when it is empty because the read FAILED rather than because
    # the service genuinely has nothing to map. The two must not look alike: "this instance
    # reports no root folders" is a claim about the instance, and printing it over a read that
    # never landed asserts something nobody checked (rule 93's Absent-vs-Unknown, and the same
    # trap the modal's own empty-vs-stale notices are divided against). ``None`` means the read
    # landed, so an empty list beside it really is nothing to map.
    map_error: str | None = None


class LeavingSoonLastOut(BaseModel):
    at: str
    movies: int
    seasons: int
    applied: bool
    #: Whether the last sync did what it set out to do: no library failed, and there was
    #: one turned on to update. Never false merely because it ran in preview (unarmed).
    #: This, not ``applied``, is what should color the Jobs page's status dot.
    ok: bool
    #: The pass's own one-line summary (``LeavingSoonResult.summary``). Rendered as it
    #: arrives: no surface words a pass of its own (#555). The Plex panel's shelf status
    #: shows it on every pass; the Jobs row shows it only when ``ok`` is false, since
    #: ``JobStatus`` reads it as the reason a run failed and a run that worked is already
    #: described by the counts beside it.
    result: str


class LeavingSoonLastSkipOut(BaseModel):
    """A scan that finished without updating the shelf.

    Reported beside ``last`` rather than replacing it, because a skipped pass writes
    nothing to Plex: the shelf still holds what the last completed pass put there, and
    those counts are the only true ones anybody has. What is no longer true is that they
    are the outcome of the most recent scan, which is what this says.
    """

    at: str
    #: Why, in one clause, because the Jobs row trails it after the exact time.
    result: str


class LeavingSoonSettingsOut(BaseModel):
    enabled: bool
    allow_unarmed: bool
    last: LeavingSoonLastOut | None = None
    #: Present whenever a skip has ever been recorded. It is the READER that decides
    #: whether it still governs, by preferring it only while it is newer than ``last`` --
    #: nothing clears it, so a pass that later completes wins on its own timestamp. Same
    #: arrangement as the scan's crash record (``ScheduledJob.last_ok``), and the reason
    #: this is not resolved server-side is that both fields are already on the wire and a
    #: second, disagreeing answer to "which is current" is worth less than one.
    last_skip: LeavingSoonLastSkipOut | None = None


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
    recovery_mode: bool = False
    """Whether REAPER_RECOVERY is armed on this process. It holds ``destructive_enabled``
    false however the stored switch is set, and the banner says so in its own tone: an
    operator told only "read-only" would go to Policy, Deletion and find a switch that
    refuses (rule 53, for a state rather than a limit)."""
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
    """Required when a password already exists, unless a recovery code opened this session.
    A borrowed signed-in session must not be able to swap the arming credential without
    knowing it; a recovery session is the one that already proved host access instead."""


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
            "This is a test message. If you can see it, your users will get a heads-up "
            "here before any title is removed."
        ),
    )


# ---------------------------------------------------------------------------
# Instances
# ---------------------------------------------------------------------------


@router.get("/instances", tags=[api_tags.SERVICES])
async def list_instances(request: Request) -> list[InstanceOut]:
    async with session_factory(request)() as session:
        return [InstanceOut.of(v) for v in await instances.list_instances(session)]


@router.post("/instances", tags=[api_tags.SERVICES])
async def create_instance(request: Request, payload: InstanceCreateIn) -> InstanceOut:
    _require_web_url(payload.base_url, refusal=_BASE_URL_REFUSAL)
    _validate_external_url(payload.external_url)
    async with session_factory(request)() as session:
        try:
            view = await instances.create_instance(
                session,
                secret_box(request),
                kind=_kind(payload.kind),
                name=payload.name,
                base_url=payload.base_url,
                api_key=payload.api_key,
                verify_tls=payload.verify_tls,
                add_import_exclusion=payload.add_import_exclusion,
                external_url=payload.external_url,
                plex_library_map=payload.plex_library_map,
                service_instance_map=payload.service_instance_map,
            )
        except instances.InstanceError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        await session.commit()
        return InstanceOut.of(view)


@router.put("/instances/{instance_id}", tags=[api_tags.SERVICES])
async def update_instance(
    request: Request, instance_id: int, payload: InstanceUpdateIn
) -> InstanceOut:
    _require_web_url(payload.base_url, refusal=_BASE_URL_REFUSAL)
    _validate_external_url(payload.external_url)
    async with session_factory(request)() as session:
        try:
            view = await instances.update_instance(
                session,
                secret_box(request),
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
        except instances.InstanceError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        await session.commit()
        return InstanceOut.of(view)


@router.delete("/instances/{instance_id}", tags=[api_tags.SERVICES])
async def delete_instance(request: Request, instance_id: int) -> RemovedOut:
    async with session_factory(request)() as session:
        removed = await instances.delete_instance(session, instance_id)
        await session.commit()
    return RemovedOut(removed=removed)


async def _plex_section_paths(request: Request) -> dict[str, list[str]]:
    """``{library title: that library's folder paths}``, the Plex side of a folder suggestion.

    Best-effort: if Plex is not linked or is unreachable this answers ``{}`` and the folders
    still come back, just with nothing suggested. Titled, not keyed, because the stored library
    map itself is titled; two libraries sharing a title contribute BOTH their folder lists here
    rather than one dropping the other, so the prefill considers everything under that name.

    Written once because two routes need it -- the test below, which suggests for a service
    that has no row yet, and ``instance_root_folders``, which suggests for one that does. A
    second copy would be two suggestion sources for one control (rule 144).
    """
    section_paths: dict[str, list[str]] = {}
    async with session_factory(request)() as session:
        server = (await session.execute(select(PlexServer))).scalars().first()
        safety = await app_settings.runtime_safety(session, runtime_settings(request))
    if server is None or not server.connection_uri:
        return section_paths
    plex = PlexClient(
        server.connection_uri,
        secret_box(request).decrypt(server.token_enc),
        safety=safety,
        verify=server.verify_tls,
    )
    try:
        for section in await plex.section_paths():
            section_paths.setdefault(section.title, []).extend(section.locations)
    except PlexError:
        return {}
    finally:
        await plex.aclose()
    return section_paths


@router.post("/instances/test", tags=[api_tags.SERVICES])
async def test_new_instance(request: Request, payload: InstanceTestIn) -> InstanceProbeOut:
    """Test a URL and key before saving, and hand back what this connection has to map.

    The add form gates its Save on this passing, so a service can never be saved at an address
    Reaper has not reached. A pass therefore has to arrive carrying everything the operator
    still has to decide, because an instance that is not saved yet has no id and cannot be
    asked a second question: for Sonarr and Radarr that is the root folders with their
    suggested Plex libraries, and for Seerr the portal's services with their suggested Reaper
    instances.

    The mapping read never decides the verdict. It runs only after the connection passed, and
    its own failure is reported as ``map_error`` beside an empty list rather than turning a
    reachable service into a failed test -- the credentials really were proven, and refusing
    the save over a folder list would strand an operator whose *arr answers ``/system/status``
    but not ``/rootfolder``.
    """
    kind = _kind(payload.kind)
    result = await instances.test_connection(
        kind, payload.base_url, payload.api_key, verify=payload.verify_tls
    )
    out = InstanceProbeOut(ok=result.ok, detail=result.detail, version=result.version)
    if not result.ok:
        return out
    try:
        if kind in (InstanceKind.SONARR, InstanceKind.RADARR):
            found = await instances.probe_root_folders(
                kind,
                payload.base_url,
                payload.api_key,
                verify=payload.verify_tls,
                section_paths=await _plex_section_paths(request),
            )
            out.root_folders = [
                RootFolderOut(path=f.path, suggested_library=f.suggested_library) for f in found
            ]
        elif kind is InstanceKind.SEERR:
            async with session_factory(request)() as session:
                arr_rows = await instances.arr_rows(session)
            services = await instances.probe_seerr_services(
                payload.base_url, payload.api_key, verify=payload.verify_tls, arr_rows=arr_rows
            )
            out.seerr_services = [
                SeerrServiceOut.model_validate(s, from_attributes=True) for s in services
            ]
    except (IntegrationError, instances.InstanceError) as exc:
        # The raw exception stays in the log, where a diagnosis needs it, and what the operator
        # is shown is the same plain-language translation the test's own failure gets. Pasting
        # `str(exc)` here put "radarr: HTTP 500 for GET /api/v3/rootfolder" in front of someone
        # trying to get a URL and a key right -- the exact string shape `explain_failure` exists
        # to prevent, on the one path that had not been given it (rule 21, rule 72).
        log.warning(
            "instance.map_probe_failed", kind=kind.value, error=f"{type(exc).__name__}: {exc}"
        )
        out.map_error = (
            f"Reaper reached this service but couldn't read what to map. "
            f"{instances.explain_failure(kind, exc)}"
        )
    return out


@router.post("/instances/{instance_id}/test", tags=[api_tags.SERVICES])
async def test_saved_instance(request: Request, instance_id: int) -> TestOut:
    """Test a stored instance and record the outcome on it."""
    async with session_factory(request)() as session:
        try:
            result = await instances.test_saved_instance(session, secret_box(request), instance_id)
        except instances.InstanceError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
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
    # The Plex side of the suggestion, from the one helper the test route also uses so a
    # folder cannot be suggested differently depending on which screen asked (rule 144).
    section_paths = await _plex_section_paths(request)

    async with session_factory(request)() as session:
        try:
            folders = await instances.instance_root_folders(
                session, secret_box(request), instance_id, section_paths=section_paths
            )
        except instances.InstanceError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
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
    async with session_factory(request)() as session:
        try:
            services = await instances.seerr_services(session, secret_box(request), instance_id)
        except instances.InstanceError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except IntegrationError as exc:
            raise HTTPException(502, f"Could not read the service list: {exc}") from exc
    return [SeerrServiceOut.model_validate(s, from_attributes=True) for s in services]


# ---------------------------------------------------------------------------
# Leaving Soon
# ---------------------------------------------------------------------------


async def _leaving_soon_out(session: AsyncSession, settings: Settings) -> LeavingSoonSettingsOut:
    last = await app_settings.get_leaving_soon_last(session)
    skip = await app_settings.get_leaving_soon_last_skip(session)
    return LeavingSoonSettingsOut(
        enabled=await app_settings.leaving_soon_enabled(session),
        allow_unarmed=await app_settings.leaving_soon_unarmed(session, settings),
        last_skip=LeavingSoonLastSkipOut(
            at=str(skip.get("at", "")),
            result=str(skip.get("result", "")),
        )
        if skip
        else None,
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
    async with session_factory(request)() as session:
        return await _leaving_soon_out(session, runtime_settings(request))


@router.put("/leaving-soon", tags=[api_tags.JOBS])
async def set_leaving_soon_settings(
    request: Request, payload: LeavingSoonSettingsIn
) -> LeavingSoonSettingsOut:
    """Flip the Leaving Soon switches. No password: these can only touch the shelf --
    a collection and a label -- never a file.

    Turning the shelf OFF runs one last pass that takes everything off it (when Reaper
    is allowed to write), so nothing stale lingers in the library.
    """
    async with session_factory(request)() as session:
        was_enabled = await app_settings.leaving_soon_enabled(session)
        if payload.enabled is not None:
            await app_settings.set_leaving_soon_enabled(session, enabled=payload.enabled)
        if payload.allow_unarmed is not None:
            await app_settings.set_leaving_soon_unarmed(session, allowed=payload.allow_unarmed)
        await session.commit()

    if was_enabled and payload.enabled is False:
        # Best-effort: takes everything off the shelves so nothing stale lingers.
        # Failure is logged inside, never raised -- turning a warning off must succeed.
        await leaving_soon.cleanup_shelves(
            session_factory(request), runtime_settings(request), secret_box(request)
        )

    async with session_factory(request)() as session:
        result = await _leaving_soon_out(session, runtime_settings(request))
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
    async with session_factory(request)() as session:
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
    async with session_factory(request)() as session:
        job_tz = ZoneInfo(await app_settings.get_timezone(session, runtime_settings(request)))
    if job_id == SCAN_JOB_ID:
        try:
            apply_scan_schedule(
                scheduler,
                cron,
                settings=runtime_settings(request),
                session_factory=session_factory(request),
                cache_engine=request.app.state.cache_engine,
                secret_box=secret_box(request),
                timezone=job_tz,
            )
        except ValueError as exc:
            raise HTTPException(
                422, f"That is not a valid schedule: {exc}. Use cron form, e.g. '30 4 * * *'."
            ) from exc
        async with session_factory(request)() as session:
            await app_settings.set_scan_schedule(session, cron)
            await session.commit()
    elif job_id in MAINTENANCE_JOB_IDS:
        try:
            apply_maintenance_schedule(
                scheduler,
                job_id,
                cron,
                cache_engine=request.app.state.cache_engine,
                session_factory=session_factory(request),
                secret_box=secret_box(request),
                settings=runtime_settings(request),
                update_checker=request.app.state.update_checker,
                timezone=job_tz,
            )
        except ValueError as exc:
            raise HTTPException(
                422, f"That is not a valid schedule: {exc}. Use cron form, e.g. '30 4 * * *'."
            ) from exc
        async with session_factory(request)() as session:
            await app_settings.set_maintenance_schedule(session, job_id, cron)
            await session.commit()
    else:
        raise HTTPException(404, f'No schedulable job named "{job_id}".')

    log.info("schedule.updated", job=job_id, cron=cron)
    return await get_schedule(request)


@router.post("/jobs/{job_id}/run", tags=[api_tags.JOBS])
async def run_job(request: Request, job_id: str) -> JobRunOut:
    """Run an upkeep job now, whether or not it is on a schedule.

    A scheduled job is nudged to fire immediately; one the owner turned off is run once
    without turning its schedule back on. Either way the schedule is left as it was. These
    are read-only upkeep jobs (refreshing ratings and lists, sweeping watch history, asking
    GitHub whether a newer Reaper exists) -- none can delete anything. The library scan is
    deliberately absent: it runs through
    ``/api/scan/start`` as a polled background job so the UI can show progress.
    """
    if job_id not in MAINTENANCE_JOB_IDS:
        raise HTTPException(404, f'No runnable job named "{job_id}".')
    run_maintenance_now(
        request.app.state.scheduler,
        job_id,
        cache_engine=request.app.state.cache_engine,
        session_factory=session_factory(request),
        secret_box=secret_box(request),
        settings=runtime_settings(request),
        update_checker=request.app.state.update_checker,
    )
    log.info("jobs.run_now", job=job_id)
    return JobRunOut(status="started", job=job_id)


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


async def _safety_out(session: AsyncSession, safety: RuntimeSafety) -> SafetyOut:
    return SafetyOut(
        destructive_enabled=safety.destructive_allowed,
        has_password=await admin_password.has_password(session),
        recovery_mode=safety.recovery_mode,
        note=safety.why_blocked(),
    )


@router.get("/safety", tags=[api_tags.SECURITY])
async def get_safety(request: Request) -> SafetyOut:
    async with session_factory(request)() as session:
        safety = await app_settings.runtime_safety(session, runtime_settings(request))
        return await _safety_out(session, safety)


@router.put("/safety", tags=[api_tags.SECURITY])
async def set_safety(request: Request, payload: SafetyIn) -> SafetyOut:
    """Turn deletion on or off.

    Turning it ON checks the admin password first -- so a stray click or a stale tab cannot
    arm the tool. Turning it OFF needs nothing, because making Reaper safer should always be
    one click. If no admin password has been set yet, enabling is refused with a message
    pointing at the password step.

    The check runs through :func:`reaper.api.deps.require_admin_password`. That is the same
    per-IP + per-account lockout shape as login and the same Argon2 concurrency gate
    (``argon2_gate``), on its own counter (``password_throttle``, not ``login_throttle``):
    arming is a password-guessing surface too, and Argon2 is expensive by design.
    """
    keys = (f"ip:{client_ip(request)}", "account:safety-arm")
    async with session_factory(request)() as session:
        if payload.enabled:
            # Refused before the password is even looked at, because no password makes this
            # allowed: `RuntimeSafety.destructive_allowed` holds deletion off for the whole
            # life of a recovery-mode process, so accepting the flip would write a stored
            # `true` the app then ignores and the banner contradicts. Answering here is what
            # keeps the switch and the state one thing.
            if runtime_settings(request).recovery:
                raise HTTPException(
                    409,
                    "Recovery mode is on, so deletion stays off. Turn it off and restart first.",
                )
            if not await admin_password.has_password(session):
                raise HTTPException(
                    400,
                    "Set an admin password first. It's what confirms turning deletion on.",
                )
            await require_admin_password(
                session,
                payload.password or "",
                keys=keys,
                gate="arm_deletion",
                refusal="That password didn't match. Deletion stays off.",
            )
        await app_settings.set_destructive_enabled(session, enabled=payload.enabled)
        await session.commit()
        safety = await app_settings.runtime_safety(session, runtime_settings(request))
        result = await _safety_out(session, safety)
    log.info("safety.destructive_set", enabled=payload.enabled)
    return result


@router.post("/admin-password", tags=[api_tags.SECURITY])
async def set_admin_password(request: Request, payload: AdminPasswordIn) -> OkOut:
    """Set (or change) the admin password.

    This is the password that later confirms turning deletion on, and it doubles as the
    local sign-in / anti-lockout account. Setting the FIRST password needs only a
    signed-in session; changing an existing one also requires the current password, so a
    borrowed session or an unattended tab cannot quietly swap the arming credential.
    Verify and hash both run behind the login's lockout and Argon2 concurrency gate.

    **One session is excused from the current password: one opened with a recovery code.**
    A forgotten password is what recovery mode is for, so demanding it here left the
    operator signed in and still locked out of the only credential that arms deletion,
    with no way forward on a desktop build (#433). The excusal grants nothing new: minting
    that code took host access, and anyone holding host access can rewrite the hash in
    ``reaper.db`` directly. It is spent immediately -- ``spend_recovery_mark`` runs in the
    same transaction as the new hash, so a second change from that session asks for the
    password like any other, and the mark cannot outlive the reset it was for.
    """
    keys = (f"ip:{client_ip(request)}", "account:admin-password")
    async with session_factory(request)() as session:
        # Preserve the caller's own cookie so changing your password does not log you out
        # of the tab you are using; every *other* session for that admin is still revoked.
        # It has to be the token that actually RESOLVES: with two cookie names in play, a
        # stale cookie under the other name would be the one spared here while the live
        # session was revoked, signing the operator out of the very tab they were in.
        _, keep = await resolve_session_from_cookies(session, request.cookies)
        via_recovery = await session_via_recovery(session, keep)
        if await admin_password.has_password(session) and not via_recovery:
            await require_admin_password(
                session,
                payload.current_password or "",
                keys=keys,
                gate="change_password",
                refusal="The current password didn't match. Nothing was changed.",
            )
        # Hashing the NEW password is one more Argon2 run, so it takes its own slot. Not part
        # of the gate above: the verify has already passed, so a refusal here records nothing.
        if not argon2_gate.acquire():
            raise busy_hashing()
        try:
            username = await admin_password.set_password(
                session, payload.password, keep_session_token=keep
            )
        except admin_password.PasswordError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            argon2_gate.release()
        # After set_password, so a refused password (too short) leaves the mark intact and
        # the operator can try again -- rule 125's shape, for the permission rather than
        # the code. set_password already revoked every OTHER session for this admin.
        if via_recovery:
            await spend_recovery_mark(session, keep)
        await session.commit()
    log.info("safety.admin_password_set", username=username, via_recovery=via_recovery)
    return OkOut(ok=True)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


@router.get("/notifications", tags=[api_tags.NOTIFICATIONS])
async def get_notifications(request: Request) -> NotificationsOut:
    """Whether a Discord webhook is configured. The URL is write-only -- like an API key,
    only its presence is ever reported, never the value."""
    async with session_factory(request)() as session:
        has = await app_settings.has_discord_webhook(
            session, secret_box(request), runtime_settings(request)
        )
    return NotificationsOut(has_webhook=has)


@router.put("/notifications", tags=[api_tags.NOTIFICATIONS])
async def set_notifications(request: Request, payload: NotificationsIn) -> NotificationsOut:
    """Store (or replace) the Discord webhook. The URL is validated to a Discord https host
    and encrypted at rest; it is never read back to the browser."""
    url = _validated_discord_webhook(payload.webhook_url)
    async with session_factory(request)() as session:
        await app_settings.set_discord_webhook(session, secret_box(request), url)
        await session.commit()
    log.info("notifications.webhook_set")
    return NotificationsOut(has_webhook=True)


@router.delete("/notifications", tags=[api_tags.NOTIFICATIONS])
async def clear_notifications(request: Request) -> NotificationsOut:
    """Forget the webhook -- Leaving Soon warnings go silent until one is set again."""
    async with session_factory(request)() as session:
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
        async with session_factory(request)() as session:
            notifier = await build_notifier(session, secret_box(request), runtime_settings(request))
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


class DesktopSettingsOut(BaseModel):
    """The desktop build's own knobs, present only when Reaper runs as the Mac or
    Windows app. Backed by ``launcher.conf`` in the data folder, which the launcher
    reads at start, so every change applies the next time Reaper opens."""

    platform: Literal["macos", "windows"]
    tray: bool
    """The menu-bar (macOS) / tray (Windows) icon with Open Reaper and Quit."""
    dock_icon: bool
    """macOS only: show the Dock icon beside the menu-bar icon. The UI never renders
    the row on Windows and the PUT refuses to set it there; the reported value echoes
    launcher.conf, which nothing on Windows reads."""


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
    desktop: DesktopSettingsOut | None = None
    """Present only on the Windows and macOS apps; the container, the snap, and a
    source run report ``null`` and the UI shows no Desktop app group."""


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
    tray: bool | None = None
    """The desktop build's menu-bar/tray icon; refused off a desktop build."""
    dock_icon: bool | None = None
    """The macOS app's Dock icon; refused off a desktop build."""


class ApiKeyOut(BaseModel):
    key: str


def _desktop_out() -> DesktopSettingsOut | None:
    platform = launcher.desktop_platform()
    if platform is None:
        return None
    # The value the launcher resolved this boot. `load_launcher_conf` seeded the file into
    # the environment before serving, so the environment is the effective record; the file
    # only matters again at the next start.
    #
    # `default=True` is the same fact `launcher._tray_wanted` writes as `return frozen`, and
    # the two agree only because the guard above returns None off a frozen build, which is
    # the one shape where `frozen` is False (rule 104).
    return DesktopSettingsOut(
        platform=platform,
        tray=env_flag(launcher.DESKTOP_TRAY_KEY, default=True),
        dock_icon=env_flag(launcher.DESKTOP_DOCK_KEY, default=False),
    )


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
        desktop=_desktop_out(),
    )


async def _refresh_proxy_state(request: Request, session: AsyncSession) -> None:
    """Re-derive the middleware's live trusted-proxy networks from what is stored.

    The middleware reads ``app.state.trusted_proxies`` per request, so this is what
    makes a General save take effect immediately. Disabled means an empty tuple:
    forwarded headers from anywhere are ignored, exactly like a fresh install.
    """
    settings = runtime_settings(request)
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
    async with session_factory(request)() as session:
        scan_cron = await app_settings.get_scan_schedule(session)
        maintenance = await app_settings.get_maintenance_schedules(session)
    apply_stored_schedules(
        scheduler,
        ZoneInfo(name),
        settings=runtime_settings(request),
        session_factory=session_factory(request),
        cache_engine=request.app.state.cache_engine,
        secret_box=secret_box(request),
        update_checker=request.app.state.update_checker,
        scan_cron=scan_cron,
        maintenance=maintenance,
    )


@router.get("/general", tags=[api_tags.GENERAL])
async def get_general(request: Request) -> GeneralSettingsOut:
    async with session_factory(request)() as session:
        return await _general_out(session, runtime_settings(request))


@router.put("/general", tags=[api_tags.GENERAL])
async def put_general(request: Request, payload: GeneralSettingsIn) -> GeneralSettingsOut:
    """Save the General settings. Partial: only the fields sent change.

    The application URL must be a plain http(s) address (or empty to clear it), and
    every trusted-proxy entry must parse as an address or a range -- refused with a
    plain message otherwise, and nothing is changed.
    """
    async with session_factory(request)() as session:
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
        if payload.tray is not None or payload.dock_icon is not None:
            platform = launcher.desktop_platform()
            if platform is None:
                raise HTTPException(422, "These settings exist only on the Windows and macOS apps.")
            # Refused where it is inert: accepting it would write a launcher.conf line
            # nothing on Windows reads, and every later read would echo a switch the
            # platform cannot honor.
            if payload.dock_icon is not None and platform != "macos":
                raise HTTPException(422, "The Dock icon setting exists only on the macOS app.")
            desktop_values: dict[str, str] = {}
            if payload.tray is not None:
                desktop_values[launcher.DESKTOP_TRAY_KEY] = "true" if payload.tray else "false"
            if payload.dock_icon is not None:
                desktop_values[launcher.DESKTOP_DOCK_KEY] = "true" if payload.dock_icon else "false"
            try:
                launcher.write_conf_values(runtime_settings(request).data_dir, desktop_values)
            except OSError:
                raise HTTPException(
                    500, "Reaper couldn't save this to launcher.conf in its data folder."
                ) from None
            # The environment is the boot-resolved record _desktop_out reads (the
            # launcher seeded the file into it), so mirror the write there too:
            # the response and every later read then show the value the next start
            # will use, instead of snapping the switch back.
            os.environ.update(desktop_values)
        await session.commit()
        await _refresh_proxy_state(request, session)
        if cleaned_timezone is not None:
            await _apply_timezone_to_scheduler(request, cleaned_timezone)
        result = await _general_out(session, runtime_settings(request))
    log.info("settings.general_saved")
    return result


@router.get("/general/api-key", tags=[api_tags.GENERAL])
async def reveal_api_key(request: Request) -> ApiKeyOut:
    """The stored key, for the Show button. Session-only: the middleware fences this
    route away from API-key auth, so a key cannot read or manage itself."""
    async with session_factory(request)() as session:
        key = await app_settings.get_api_key(session, secret_box(request))
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
    async with session_factory(request)() as session:
        await app_settings.set_api_key(session, secret_box(request), key)
        await session.commit()
    request.app.state.api_key_digest = hashlib.sha256(key.encode("utf-8")).digest()
    log.info("settings.api_key_rotated")
    return ApiKeyOut(key=key)


@router.delete("/general/api-key", tags=[api_tags.GENERAL])
async def remove_api_key(request: Request) -> RemovedOut:
    """Close the header-credential lane: delete the key, and stop honoring it now.

    Rotating replaces one working key with another, so an operator who generated a key for
    a one-off script had no way to shut the lane again (PR2-2). This is that way.

    The stored digest on the app is cleared in the same breath. Auth reads that digest, not
    the database, so a deleted key would otherwise keep working until the next restart.
    Session-only, like every write here: the middleware is deny-by-default for anything
    that is not a safe method, so a key cannot delete itself or anyone else's.
    """
    async with session_factory(request)() as session:
        await app_settings.clear_api_key(session)
        await session.commit()
    request.app.state.api_key_digest = None
    log.info("settings.api_key_removed")
    return RemovedOut(removed=True)
