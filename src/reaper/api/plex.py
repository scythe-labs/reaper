# SPDX-License-Identifier: AGPL-3.0-or-later
"""Linking a Plex server, choosing its address, and what Reaper reads from it.

This router holds the link flow, the server and connection choice, the library
shelf, and the watch-evidence mirror the shelf is read against. It keeps the
``/api/settings`` prefix and its own ``PLEX`` tag, matching how
``api/plex_trash.py`` and ``api/leaving_soon.py`` already sit beside
``api/settings.py`` as sibling routers under the same tag.

The request accessors come from ``api.deps``, the same source every router that
needs them reads from.

Two things are true of this router, as they are of ``api/settings.py``. It
requires a session, since these routes sit behind the auth gate (see
``api.middleware``). And the Plex token is write-only: encrypted the instant it
arrives and never read back to the browser.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.api import tags as api_tags
from reaper.api.deps import (
    client_ip,
    require_admin_password,
    runtime_settings,
    secret_box,
    session_factory,
)
from reaper.api.errors import refuse, refuse_from
from reaper.api.schemas import (
    NO_PLEX_FORWARD,
    PlexServerChoiceOut,
    PlexStartIn,
    RemovedOut,
)

# Private on purpose, and imported rather than copied: this router keeps ``api/settings.py``'s
# prefix, so it validates an operator-typed URL through that file's own checks.
from reaper.api.settings import _require_web_url, _required_web_url
from reaper.clients.base import IntegrationError
from reaper.clients.plex import PlexClient, PlexError
from reaper.clients.plextv import PlexConnection, PlexTvClient, connection_identity
from reaper.clock import utcnow
from reaper.db.models import PlexServer, Snapshot, WatchHighWater
from reaper.engine.explanation import ReasonKey
from reaper.engine.reason import Reason, to_wire
from reaper.services import admin_password, app_settings, leaving_soon, watch_evidence
from reaper.services.plex_link import (
    PlexLinkError,
    PlexLinkRetryableError,
    PlexServerChoiceNeededError,
    client_identifier,
    poll_link,
    start_pin,
    switch_server,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/settings")


class PlexStatusOut(BaseModel):
    linked: bool
    name: str | None = None
    connection_uri: str | None = None
    last_ok_at: str | None = None
    verify_tls: bool = True
    """Whether the server's TLS certificate is checked (mirrors the per-instance
    setting). True when unlinked, since on is the only default."""
    web_url: str = ""
    """Where "open in Plex" links point. Always present, linked or not. This is
    the hosted Plex Web default until the operator overrides it."""


# A comment, not the docstring. This class's docstring is the schema description
# the API reference shows an operator, so it must stay a plain sentence, and this
# comment explains the contract in more detail.
#
# `web_url` needs three states. `None` keeps the stored address, `""` resets it
# to the hosted default, and any other string sets it. A plain `str = ""` cannot
# tell "not changing it" from "reset it", so every caller would write the
# address whether it meant to or not.
#
# Both sentences of the contract belong in the class docstring, not the field
# docstrings, because Pydantic only publishes per-field docstrings under
# `use_attribute_docstrings`, which this tree does not set (`schemas.py` says so
# too). A note written on the field alone is invisible in the browsable schema.
class PlexUpdateIn(BaseModel):
    """The editable Plex settings. Send only what you are changing. A field you
    leave out keeps its stored value, and an empty web address puts it back to
    the hosted default."""

    web_url: str | None = None
    """Where "open in Plex" links point."""
    verify_tls: bool | None = None
    """Whether to check the linked server's TLS certificate."""


class PlexLinkStartOut(BaseModel):
    pin_id: int
    auth_url: str


class PlexLinkPollIn(BaseModel):
    pin_id: int
    # Multi-server accounts only. The machine identifier of the owned server the
    # admin picked, echoed back from a "choose_server" response.
    machine_identifier: str | None = None
    # The certificate-check choice made in the link form. Off lets a self-signed
    # HTTPS server be reached at all. It is stored on the server row when the
    # link completes.
    verify_tls: bool = True


class PlexLinkPollOut(BaseModel):
    status: str  # "pending" | "retrying" | "ok" | "choose_server"
    server: PlexStatusOut | None = None
    # Present only with status "choose_server". Lists the owned servers to pick from.
    servers: list[PlexServerChoiceOut] | None = None
    # Present only with status "retrying". Says why this poll could not finish
    # yet, as the typed id plus raw params, the same shape every other reason
    # field on the wire takes. The sign-in is still good, and the browser keeps
    # polling.
    reason: ReasonKey | None = None


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
    """The signed-in Plex account's name, not the server's. Known only on the
    live path, since it comes from plex.tv. ``None`` on the stored fallback,
    where the UI shows the server name instead."""


class PlexServerSwitchIn(BaseModel):
    machine_identifier: str
    verify_tls: bool | None = (
        None  # omitted keeps the stored setting. A self-signed target needs False
    )


class PlexConnectionIn(BaseModel):
    """A connection choice. One of the discovered addresses, or a manually typed
    one. The address is probed before anything is saved, so a typo changes
    nothing."""

    uri: str
    verify_tls: bool | None = None


class PlexLibraryOut(BaseModel):
    key: int
    title: str
    kind: str
    """``"movie"`` or ``"show"``."""
    enabled: bool


class WatchEvidenceOut(BaseModel):
    """How much watching Reaper has recorded, and what the last scan could not read.

    ``titles`` is how many titles hold a record. ``held_back`` is how many items
    the last scan found had plays it could no longer read. It is null when no
    scan has counted, either because none has run, or because the newest one
    predates the count. Null is not zero, and must never be rendered as zero: a
    scan that did not count is not the same as a scan that counted none.

    It counts what was measured, never what was decided. Such an item is
    normally held back, but by three gates the operator can each switch off,
    and nothing here consults the verdict. Copy that calls this figure items
    held back or kept asserts a protection this number is not computed from.

    The contract lives in this class docstring, not on the fields. Attribute
    docstrings are not published in the schema here (see ``PlexUpdateIn``
    above), so a field-level note is invisible to anyone reading the API
    reference.
    """

    titles: int
    held_back: int | None = None


class WatchEvidenceResetIn(BaseModel):
    """The admin password, which is what confirms forgetting the record.

    Optional on the wire rather than required, so an omitted password comes back
    as the same plain "that password didn't match" the wrong one gets, instead
    of a validator's sentence. Its siblings ``SafetyIn`` and ``RestoreConfirmIn``
    are typed the same way.
    """

    password: str | None = Field(default=None, max_length=128)


class WatchEvidenceResetOut(BaseModel):
    """How many titles Reaper forgot."""

    forgotten: int


class PlexLibrariesIn(BaseModel):
    """The full set of enabled section keys. Keys not in the stored list are
    ignored. An empty list turns every library off, which just means no shelf
    is managed anywhere. This scopes a warning feature, never a deletion."""

    enabled_keys: list[int]


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
    async with session_factory(request)() as session:
        return await _plex_status(session)


@router.put("/plex", tags=[api_tags.PLEX])
async def update_plex_settings(request: Request, payload: PlexUpdateIn) -> PlexStatusOut:
    """Save the Plex settings. This covers the "open in Plex" web address (empty
    resets to the hosted default) and, once a server is linked, the certificate
    check. Each field is independent, and one left out is left alone."""
    cleaned = payload.web_url.strip() if payload.web_url is not None else None
    _require_web_url(cleaned, code="error.plex.web_url_invalid")
    async with session_factory(request)() as session:
        # Only when the caller sent the field. `cleaned` is `None` for "not sent"
        # and `""` for "reset to the hosted default", and those are different
        # requests.
        if cleaned is not None:
            await app_settings.set_plex_web_url(session, cleaned or None)
        if payload.verify_tls is not None:
            server = (await session.execute(select(PlexServer))).scalars().first()
            if server is None:
                refuse(422, "error.plex.verify_tls_no_server")
            server.verify_tls = payload.verify_tls
        status = await _plex_status(session)
        await session.commit()
    log.info("settings.plex_saved")
    return status


@router.post("/plex/link/start", tags=[api_tags.PLEX])
async def plex_link_start(
    request: Request, payload: PlexStartIn = NO_PLEX_FORWARD
) -> PlexLinkStartOut:
    async with session_factory(request)() as session:
        safety = await app_settings.runtime_safety(session, runtime_settings(request))
    start = await start_pin(
        session_factory(request),
        purpose="link",
        safety=safety,
        forward_url=payload.forward_url(),
    )
    return PlexLinkStartOut(pin_id=start.pin_id, auth_url=start.auth_url)


@router.post("/plex/link/poll", tags=[api_tags.PLEX])
async def plex_link_poll(request: Request, payload: PlexLinkPollIn) -> PlexLinkPollOut:
    async with session_factory(request)() as session:
        safety = await app_settings.runtime_safety(session, runtime_settings(request))
    try:
        linked = await poll_link(
            session_factory(request),
            secret_box(request),
            pin_id=payload.pin_id,
            safety=safety,
            choice=payload.machine_identifier,
            verify_tls=payload.verify_tls,
        )
    except PlexServerChoiceNeededError as exc:
        # The account owns several servers. The PIN stays valid. The browser
        # shows the candidates and re-polls with the admin's pick.
        return PlexLinkPollOut(
            status="choose_server",
            servers=[
                PlexServerChoiceOut(name=c.name, machine_identifier=c.machine_identifier)
                for c in exc.candidates
            ],
        )
    except PlexLinkRetryableError as exc:
        # The sign-in was approved but the server could not be reached this
        # instant. It may just be restarting. ``poll_link`` deliberately keeps
        # the pending login for exactly this case, so the answer must not be an
        # error. The browser aborts its poll loop on any thrown status, which
        # would strand the still-valid sign-in and send the operator back
        # through the whole approval round trip. This answers with a non-final
        # status instead, so the loop keeps polling until it works or the
        # deadline passes.
        return PlexLinkPollOut(
            status="retrying",
            reason=ReasonKey.model_validate(to_wire(Reason(exc.code, dict(exc.params)))),
        )
    except PlexLinkError as exc:
        refuse_from(exc)

    if linked is None:
        return PlexLinkPollOut(status="pending")
    # The server is linked as of this line, so its libraries are readable for the first time.
    await _sync_libraries_after_link(request)
    async with session_factory(request)() as session:
        return PlexLinkPollOut(status="ok", server=await _plex_status(session))


@router.delete("/plex", tags=[api_tags.PLEX])
async def plex_unlink(request: Request) -> RemovedOut:
    """Forget the linked Plex server. This deletes nothing in Plex. It drops the
    stored connection and token, so Leaving Soon and the collection whitelist go
    quiet until a server is linked again."""
    async with session_factory(request)() as session:
        server = (await session.execute(select(PlexServer))).scalars().first()
        if server is None:
            return RemovedOut(removed=False)
        await session.delete(server)
        await session.commit()
    log.info("plex.unlinked")
    return RemovedOut(removed=True)


async def _linked_server(session: AsyncSession) -> PlexServer:
    server = (await session.execute(select(PlexServer))).scalars().first()
    if server is None:
        refuse(400, "error.plex.not_linked")
    return server


@router.get("/plex/resources", tags=[api_tags.PLEX])
async def plex_resources(request: Request) -> PlexResourcesOut:
    """List the servers this Plex account owns, and every address each can be
    reached at, for the server and connection pickers.

    Asks plex.tv live using the stored token. When plex.tv cannot be reached,
    this falls back to the linked server's addresses as remembered at link
    time, marked ``source: "stored"`` so the UI can say the list may be stale
    instead of implying it is fresh.
    """
    async with session_factory(request)() as session:
        server = await _linked_server(session)
        current_id = server.machine_identifier
        token = secret_box(request).decrypt(server.token_enc)
        cid = await client_identifier(session)
        safety = await app_settings.runtime_safety(session, runtime_settings(request))
        stored_connections = json.loads(server.connections_json or "[]")
        stored_name = server.name
        await session.commit()

    try:
        async with PlexTvClient(cid, safety=safety) as plextv:
            owned = await plextv.owned_servers(token)
            # The signed-in person's name, for the "who you're linked as" line.
            # Same token, same live call as the server list. A failure here
            # degrades to stored below.
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

    Resolved against the live owned list from plex.tv and probed before
    anything is saved. Switching clears the library choices and the announced
    set, since they were keyed to the old server and would silently mis-target
    the new one. The certificate check rides along when given, so switching to
    a self-signed server can turn it off in the same step, instead of being
    stuck on the old server's setting.
    """
    async with session_factory(request)() as session:
        safety = await app_settings.runtime_safety(session, runtime_settings(request))
    try:
        await switch_server(
            session_factory(request),
            secret_box(request),
            machine_identifier=payload.machine_identifier,
            safety=safety,
            verify_tls=payload.verify_tls,
        )
    except PlexLinkRetryableError as exc:
        refuse_from(exc)
    except PlexLinkError as exc:
        refuse_from(exc)

    # Switching cleared the library choices, which were keyed to the old server.
    # This refills them from the new one here, for the same reason the link
    # path does. The stored list is otherwise empty until something presses
    # Sync, and the library pickers read that list.
    await _sync_libraries_after_link(request)
    async with session_factory(request)() as session:
        status = await _plex_status(session)
    log.info("plex.server_switched")
    return status


@router.put("/plex/connection", tags=[api_tags.PLEX])
async def plex_set_connection(request: Request, payload: PlexConnectionIn) -> PlexStatusOut:
    """Save how Reaper reaches the linked server. This can be a discovered
    address or a manual one.

    The address is probed with the stored token before anything is written, so
    a typo or a dead address changes nothing. The certificate check rides
    along when given, since a self-signed HTTPS server needs it off to be
    probed at all.

    The probe also asks the server who it is and refuses anything but the
    linked one. This address is typed by hand, so it can be any Plex on the
    network. Saving one that belongs to a different server would point
    Reaper's Leaving Soon writes and its Never-Reap read at a library nobody
    asked it to touch. A server that will not say who it is is refused for the
    same reason. Unconfirmed is not confirmed.
    """
    uri = payload.uri.strip().rstrip("/")
    # The required form. This address is dialed, not stored for display, so a
    # blank one is refused here instead of being carried into the probe.
    # `host` is the validated host the probe needs.
    parts, host = _required_web_url(uri, code="error.plex.connection_address_invalid")

    async with session_factory(request)() as session:
        server = await _linked_server(session)
        token = secret_box(request).decrypt(server.token_enc)
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
        refuse(502, "error.plex.connection_probe_failed")
    if answered != expected:
        log.warning("plex.connection_wrong_server")
        refuse(409, "error.plex.connection_wrong_server", expected_name=expected_name)

    async with session_factory(request)() as session:
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
    async with session_factory(request)() as session:
        return _libraries_out(await app_settings.get_plex_libraries(session))


async def _sync_libraries_after_link(request: Request) -> None:
    """Refresh the library list because the linked server just changed. Never raises.

    The library list is a property of the server that was just linked, so this
    is where it becomes knowable. Reading it here means no later screen has to
    remember to sync it itself. ``GET /plex/libraries`` only answers "as last
    synced", so without a sync call somewhere in the link path, an operator
    whose sign-in flow skips the wizard's Plex step would see empty library
    pickers even though Plex has real libraries.

    Best-effort by design. A sync failure must not fail the sign-in the
    operator just approved. Both callers have a manual Sync button behind
    them, and ``PlexPanel`` re-syncs an empty list on sight, so the recovery
    path is the one that already existed.
    """
    # This deliberately catches every exception. `_sync_libraries` also
    # decrypts the stored token, writes and commits, and closes the client in
    # a `finally`, so `PlexError` and network failures are not the whole
    # surface. An `InvalidToken` or a locked database would otherwise come out
    # of `plex_link_poll` as a 500, after the pin was already consumed and the
    # server already linked, stranding the sign-in this function exists to
    # protect. The docstring's "never raises" claim has to be true.
    try:
        await _sync_libraries(request)
    except Exception as exc:
        log.warning("plex.libraries_autosync_failed", error=f"{type(exc).__name__}: {exc}")


@router.post("/plex/libraries/sync", tags=[api_tags.PLEX])
async def sync_plex_libraries(request: Request) -> list[PlexLibraryOut]:
    """Refresh the library list from the server."""
    try:
        return await _sync_libraries(request)
    except PlexError as exc:
        refuse(502, "error.plex.libraries_sync_failed", error=exc.as_reason())


async def _sync_libraries(request: Request) -> list[PlexLibraryOut]:
    """Do the refresh itself, raising ``PlexError`` for an unreachable server.

    This merges instead of replacing. A library the operator already turned
    off stays off across a re-sync. A newly discovered library starts on, so
    the default install marks every movie and TV library without further
    setup. Libraries that no longer exist on the server are dropped.
    """
    async with session_factory(request)() as session:
        server = await _linked_server(session)
        safety = await app_settings.runtime_safety(session, runtime_settings(request))
        stored = {
            int(lib["key"]): bool(lib.get("enabled", True))
            for lib in await app_settings.get_plex_libraries(session)
        }
        plex = PlexClient(
            server.connection_uri,
            secret_box(request).decrypt(server.token_enc),
            safety=safety,
            verify=server.verify_tls,
        )

    try:
        sections = await plex.video_sections()
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
    async with session_factory(request)() as session:
        await app_settings.set_plex_libraries(session, merged)
        result = _libraries_out(await app_settings.get_plex_libraries(session))
        await session.commit()
    log.info("plex.libraries_synced", count=len(merged))
    return result


@router.get("/watch-evidence", tags=[api_tags.PLEX])
async def get_watch_evidence(request: Request) -> WatchEvidenceOut:
    """Return how many titles hold a watch record, and how many the last scan
    could not read.

    The second number is the one that answers whether the operator needs to
    press this reset. A nonzero count is items whose recorded plays Reaper can
    no longer see. It is what was measured, not what was decided. See
    ``WatchEvidenceOut``, which says why the difference matters here.
    """
    async with session_factory(request)() as session:
        titles = int(
            (await session.execute(select(func.count()).select_from(WatchHighWater))).scalar() or 0
        )
        held_back = (
            await session.execute(
                select(Snapshot.watch_blind_items).order_by(Snapshot.id.desc()).limit(1)
            )
        ).scalar()
    return WatchEvidenceOut(titles=titles, held_back=held_back)


@router.post("/watch-evidence/reset", tags=[api_tags.PLEX])
async def reset_watch_evidence(
    request: Request, payload: WatchEvidenceResetIn
) -> WatchEvidenceResetOut:
    """Forget how much watching Reaper has measured for each title, and start over.

    Reaper records the most watch history it has ever seen per title, so that
    a title whose plays suddenly read as zero can be told apart from one
    nobody ever watched. Plays go unreadable when Plex reissues an item's id,
    which happens when a file leaves the library and comes back. The plays
    stay filed under the old id.

    Rebuilding a whole library without repairing that history makes every
    watched title read zero at once, so every one of them is held back and
    nothing is reapable. That is the honest answer, and no amount of
    re-scanning changes it. This discards the record, so the next scan accepts
    the library as it is now.

    This is deliberately not paired with a cache rebuild. The watch mirror is
    a faithful copy of the source, so re-syncing it fetches the same rows
    back. The repair that restores the real numbers happens on the source
    side, in Tautulli.

    Gated on the admin password, exactly like arming deletion
    (:func:`reaper.api.settings.set_safety`) and confirming a restore
    (:func:`reaper.api.backup.restore_confirm`). It is literally the same
    gate, since all three call :func:`reaper.api.deps.require_admin_password`,
    plus the same refusal when no password has been set at all. It earns that
    gate on blast radius. The record is the only thing that can tell a title
    whose plays went unreadable apart from one nobody ever watched, so
    discarding it withdraws that protection from every title at once, and the
    three gates that were holding those titles (``MIN_DORMANCY``,
    ``SERVER_POPULARITY``, ``DATA_HORIZON``) stop holding on the next scan. A
    stray click or a stale tab must not be able to do that, the same property
    ``set_safety`` is written to hold.

    This carries no content-binding token, and that is a decision rather than
    an omission. There is nothing staged for the operator to review. They are
    not approving a list, and the action discards the whole record whatever
    the count beside it says, so a token bound to that count could only
    refuse a press over a change that cannot alter what the press does.
    ``set_safety`` is the shape this follows. ``restore_confirm`` binds a
    token because it has a staged artifact to bind one to.
    """
    keys = (f"ip:{client_ip(request)}", "account:watch-evidence-reset")
    async with session_factory(request)() as session:
        if not await admin_password.has_password(session):
            refuse(400, "error.plex.no_password_set_for_watch_reset")
        await require_admin_password(
            session,
            payload.password or "",
            keys=keys,
            gate="forget_watch_record",
            code="error.auth.forget_watch_password_mismatch",
        )
        forgotten = await watch_evidence.forget_all(session)
        await session.commit()
    return WatchEvidenceResetOut(forgotten=forgotten)


@router.delete("/watch-evidence/{media_key}", tags=[api_tags.PLEX])
async def forget_watch_evidence_for(request: Request, media_key: str) -> RemovedOut:
    """Accept what Reaper can see now for one title, and judge it on that from
    the next scan.

    This is the narrow twin of the reset above. Reaper holds a title back when
    the plays it recorded earlier stop being readable, because it cannot tell
    that from a title nobody watched. The usual cause is a file that left the
    library and came back. Plex gives it a new id, and the earlier plays stay
    filed under the old one.

    Two other events read the same way but are not that: removing a duplicate
    copy of a title held twice, and rebuilding a Radarr or Sonarr database so
    a different title inherits the record. Nothing in the scan can tell the
    three apart, which is why this is a control the operator presses rather
    than something Reaper decides.

    This never deletes anything and never approves a removal. It returns
    whether a record existed. The title goes back to being judged by the
    policy on its current plays, like any other.
    """
    async with session_factory(request)() as session:
        removed = await watch_evidence.forget_one(session, media_key)
        await session.commit()
    return RemovedOut(removed=removed)


@router.put("/plex/libraries", tags=[api_tags.PLEX])
async def set_plex_libraries(request: Request, payload: PlexLibrariesIn) -> list[PlexLibraryOut]:
    """Turn libraries on or off. The keys name the enabled set. Everything else
    stored turns off. Unknown keys are ignored rather than invented.

    A library that just turned off gets one last empty-reconcile, when Reaper
    is allowed to write, so its "Leaving Soon" shelf does not linger
    unmanaged. The reconcile never visits a disabled library again, and a
    stale warning shelf would be a lie.
    """
    enabled = {int(k) for k in payload.enabled_keys}
    async with session_factory(request)() as session:
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
        # Best-effort, after the choice is committed. Failure is logged
        # inside, never raised, and never blocks the settings change itself.
        await leaving_soon.cleanup_sections(
            session_factory(request),
            runtime_settings(request),
            secret_box(request),
            sections=turned_off,
        )

    log.info("plex.libraries_set", enabled=len(enabled), cleaned=len(turned_off))
    return result
