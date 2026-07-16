# SPDX-License-Identifier: AGPL-3.0-or-later
"""Running a scan, independent of how it was triggered.

A scan is the same read-only pipeline whether a person clicked "Scan" or a schedule
fired it at 4am: build the clients from the configured instances, pull watch history
into the local mirror, refresh the protection lists, then gather-freeze-judge. The only
difference is where the progress goes -- to an SSE stream for a person, to the log for a
timer -- so that difference is a callback, and everything else lives here once.

**A scan cannot delete anything.** It reads from the *arr and Tautulli, scores, and
writes rows to Reaper's own database. ``GuardedTransport`` refuses a mutating call even
if one were attempted.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from reaper.clients.arr import RadarrClient, SonarrClient
from reaper.clients.base import BaseClient, IntegrationError
from reaper.clients.plex import PlexClient, PlexError
from reaper.clients.seerr import SeerrClient
from reaper.clients.tautulli import TautulliClient
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.models import Instance, InstanceKind, PlexServer, Snapshot
from reaper.engine.gates import (
    CuratedListGate,
    DataHorizonGate,
    Gate,
    GateConfig,
    GateId,
    MinDormancyGate,
    OthersWatchingGate,
    RatingFloorGate,
    ServerPopularityGate,
    StreamingNowGate,
    UnmanagedGate,
    WhitelistGate,
)
from reaper.engine.policy import PolicyBody
from reaper.services import app_settings, history_sync, profiles, requested_by
from reaper.services import snapshot as snapshot_service
from reaper.services.snapshot import Progress, ProgressFn

if TYPE_CHECKING:
    from reaper.services.executor import ReapGateway

log = structlog.get_logger(__name__)


class ScanConfigError(RuntimeError):
    """A scan cannot run because the required instances are not configured."""


#: Every gate the catalogue knows how to build. A gate in a policy with no entry here
#: would be a protection that silently does not fire, so the builder raises instead.
GATE_TYPES: dict[GateId, type] = {
    GateId.WHITELISTED: WhitelistGate,
    GateId.STREAMING_NOW: StreamingNowGate,
    GateId.RATING_FLOOR: RatingFloorGate,
    GateId.SERVER_POPULARITY: ServerPopularityGate,
    GateId.OTHERS_WATCHING: OthersWatchingGate,
    GateId.CURATED_LIST: CuratedListGate,
    GateId.DATA_HORIZON: DataHorizonGate,
    GateId.UNMANAGED: UnmanagedGate,
    GateId.MIN_DORMANCY: MinDormancyGate,
}


def build_gates(policy: PolicyBody) -> list[Gate]:
    """Instantiate every enabled gate.

    An unknown gate id **raises**. Silently skipping it would mean a protection the owner
    switched on simply does not run -- the quietest possible way to delete something they
    meant to keep.
    """
    gates: list[Gate] = []
    for setting in policy.gates:
        if not setting.enabled:
            continue
        gate_type = GATE_TYPES.get(setting.gate)
        if gate_type is None:
            raise ScanConfigError(
                f"Policy enables the {setting.gate.value!r} protection, but Reaper has no "
                "implementation for it. Refusing to scan rather than silently skipping a "
                "protection you asked for."
            )
        gates.append(
            gate_type(
                GateConfig(
                    gate=setting.gate,
                    enabled=True,
                    threshold=setting.threshold,
                    secondary=setting.secondary,
                    window_days=setting.window_days,
                )
            )
        )

    # The owner's own protections, on top of the built-in gates. Each condition is its own
    # gate (protect-only, evaluated through the field registry), so a matched one keeps the
    # title and shows up in the why-panel exactly like a stock protection.
    from reaper.engine.fields import CustomProtectGate

    gates.extend(CustomProtectGate(c.to_condition()) for c in policy.protect_conditions)
    return gates


async def build_sources(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    box: SecretBox,
) -> tuple[
    list[snapshot_service.RadarrSource],
    list[snapshot_service.SonarrSource],
    TautulliClient,
    SeerrClient | None,
    PlexClient | None,
]:
    """Build clients for EVERY enabled instance.

    Not ``next(...)``. A separate 4K Radarr alongside the HD one is a common setup, and
    scanning whichever came first would silently ignore an entire library while reporting
    a clean, confident, non-degraded result.

    Sonarr and Seerr are optional. A movie-only deployment runs with no Sonarr and
    produces no season candidates; with no Seerr, items simply carry no "requested by".
    Radarr and Tautulli are required -- without them there is nothing to scan against.
    """
    async with session_factory() as session:
        safety = await app_settings.runtime_safety(session, settings)
        rows = (
            (await session.execute(select(Instance).where(Instance.enabled.is_(True))))
            .scalars()
            .all()
        )
        server = (await session.execute(select(PlexServer))).scalars().first()
        plex_token = box.decrypt(server.token_enc) if server is not None else None
        plex_uri = server.connection_uri if server is not None else None

    radarr_rows = [r for r in rows if r.kind is InstanceKind.RADARR]
    sonarr_rows = [r for r in rows if r.kind is InstanceKind.SONARR]
    tautulli_row = next((r for r in rows if r.kind is InstanceKind.TAUTULLI), None)
    seerr_row = next((r for r in rows if r.kind is InstanceKind.SEERR), None)

    if not radarr_rows or tautulli_row is None:
        raise ScanConfigError(
            "A scan needs at least one Radarr and one Tautulli instance. "
            "Add them in Settings first."
        )

    radarrs = [
        snapshot_service.RadarrSource(
            client=RadarrClient(
                r.base_url,
                box.decrypt(r.api_key_enc),
                safety=safety,
                api_path_prefix=r.api_path_prefix,
            ),
            instance_id=r.id,
            name=r.name,
        )
        for r in radarr_rows
    ]
    sonarrs = [
        snapshot_service.SonarrSource(
            client=SonarrClient(
                r.base_url,
                box.decrypt(r.api_key_enc),
                safety=safety,
                api_path_prefix=r.api_path_prefix,
            ),
            instance_id=r.id,
            name=r.name,
        )
        for r in sonarr_rows
    ]
    tautulli = TautulliClient(
        tautulli_row.base_url, box.decrypt(tautulli_row.api_key_enc), safety=safety
    )
    seerr = (
        # TLS verification stays ON (the client default), consistent with Tautulli and the
        # *arr: the decrypted Seerr API key travels on this connection and must not be
        # harvestable by an on-path attacker. A self-signed internal Seerr is an explicit
        # opt-out, never disabled silently here.
        SeerrClient(seerr_row.base_url, box.decrypt(seerr_row.api_key_enc), safety=safety)
        if seerr_row is not None
        else None
    )
    plex = (
        PlexClient(plex_uri, plex_token, safety=safety)
        if plex_uri is not None and plex_token is not None
        else None
    )
    return radarrs, sonarrs, tautulli, seerr, plex


async def build_reap_gateway(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    box: SecretBox,
) -> tuple[ReapGateway, list[BaseClient]]:
    """Build the live clients a real reap drives, keyed by the instance each item is from.

    Returns the gateway plus the httpx-backed clients that must be closed after the run --
    the caller enters them into an ``AsyncExitStack`` so a run cannot leak connections. The
    Radarr/Sonarr clients are keyed by ``instance.id`` because a plan routes each item to
    *its* server by ``media_key``; picking "the first Radarr" would delete from the wrong
    one on a 4K + HD split. Plex and Tautulli are single, and a real run refuses without
    them (the executor enforces that) -- so they are built when present and simply absent
    otherwise, letting the executor produce the precise refusal.

    The clients are built with the live :class:`RuntimeSafety`; when a real execute reaches
    them they will be armed, and the transport guard enforces that independently of the
    executor's own check.
    """
    from reaper.services.executor import ReapGateway

    async with session_factory() as session:
        safety = await app_settings.runtime_safety(session, settings)
        rows = (
            (await session.execute(select(Instance).where(Instance.enabled.is_(True))))
            .scalars()
            .all()
        )
        server = (await session.execute(select(PlexServer))).scalars().first()
        plex_token = box.decrypt(server.token_enc) if server is not None else None
        plex_uri = server.connection_uri if server is not None else None

    radarr: dict[int, RadarrClient] = {}
    sonarr: dict[int, SonarrClient] = {}
    closers: list[BaseClient] = []
    tautulli: TautulliClient | None = None

    for row in rows:
        key = box.decrypt(row.api_key_enc)
        if row.kind is InstanceKind.RADARR:
            client = RadarrClient(
                row.base_url, key, safety=safety, api_path_prefix=row.api_path_prefix
            )
            radarr[row.id] = client
            closers.append(client)
        elif row.kind is InstanceKind.SONARR:
            sclient = SonarrClient(
                row.base_url, key, safety=safety, api_path_prefix=row.api_path_prefix
            )
            sonarr[row.id] = sclient
            closers.append(sclient)
        elif row.kind is InstanceKind.TAUTULLI and tautulli is None:
            tautulli = TautulliClient(row.base_url, key, safety=safety)
            closers.append(tautulli)

    plex = (
        PlexClient(plex_uri, plex_token, safety=safety)
        if plex_uri is not None and plex_token is not None
        else None
    )

    gateway = ReapGateway(radarr=radarr, sonarr=sonarr, plex=plex, tautulli=tautulli)
    return gateway, closers


async def run_scan(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    cache_engine: AsyncEngine,
    box: SecretBox,
    on_progress: ProgressFn | None = None,
) -> Snapshot:
    """The whole read-only pipeline, from configured instances to a persisted snapshot.

    Raises :class:`ScanConfigError` if the required instances are missing, and
    :class:`IntegrationError` if a source fails hard mid-run. A per-instance failure is
    caught inside the snapshot and degrades it (loud, viewable, un-executable) rather than
    aborting -- partial evidence must never look complete.
    """
    emit = on_progress or (lambda _p: None)

    radarrs, sonarrs, tautulli, seerr, plex = await build_sources(session_factory, settings, box)

    async with session_factory() as policy_session:
        from reaper.api.routes import active_policies

        movie_policy, tv_policy = await active_policies(policy_session)
        # The grace window is a profile setting, read here so the scan can restart the grace
        # clock for an item that left the condemned set and returned (see
        # snapshot._record_first_flagged); a longer gap than this means a genuine departure.
        profile_settings = await profiles.active_profile_settings(policy_session)
    movie_gates = build_gates(movie_policy)
    tv_gates = build_gates(tv_policy)

    async with AsyncExitStack() as stack:
        for source in radarrs:
            await stack.enter_async_context(source.client)
        for sonarr in sonarrs:
            await stack.enter_async_context(sonarr.client)
        await stack.enter_async_context(tautulli)
        session = await stack.enter_async_context(session_factory())

        # Pull watch history into the local mirror BEFORE scoring reads it. Incremental
        # after the first time, but on a fresh install it is what populates the table at
        # all -- without it every item's dormancy is Unknown and the scan judges nothing.
        emit(Progress("history", 0, 0, "syncing watch history"))
        try:
            hist = await history_sync.sync(cache_engine, tautulli)
            log.info("scan.history_synced", rows=hist.rows)
        except IntegrationError as exc:
            log.warning("scan.history_sync_failed", error=str(exc))

        # Refresh the protection lists BEFORE scoring reads them, or a "Never Reap"
        # collection and the IMDb Top 250 are silently empty and protect nothing.
        emit(Progress("lists", 0, 0, "refreshing protection lists"))
        # Failures to gather BEFORE the freeze are collected here and handed to the scan,
        # which degrades the snapshot for each (loud, viewable, un-executable) exactly as an
        # in-gather source failure does. None of them may silently pass through.
        pre_scan_degradations: list[str] = []

        # Plex is optional (a movie-only deployment runs without it), but a *configured*
        # Plex that is briefly unreachable must degrade, not crash the whole scan the way an
        # uncaught PlexError from connect() would. Critically it must fail CLOSED: with no
        # live server the "Never Reap" collection cannot refresh, so we skip it (the atomic
        # swap keeps any prior membership) AND degrade, so a reap cannot run against a
        # keep-list that could not be confirmed.
        plex_server: object | None = None
        if plex is not None:
            try:
                plex_server = await plex.connect()
            except PlexError as exc:
                plex_server = None
                pre_scan_degradations.append(
                    f"Plex unreachable: {exc} -- the 'Never Reap' collection could not be "
                    "refreshed, so no reap may run against a keep-list we could not confirm"
                )
        synced = await snapshot_service.sync_protection_lists(
            cache_engine,
            radarrs=[s.client for s in radarrs],
            sonarrs=[s.client for s in sonarrs],
            movie_keep_tags=movie_policy.keep_tags,
            movie_keep_match=movie_policy.keep_tags_match,
            tv_keep_tags=tv_policy.keep_tags,
            tv_keep_match=tv_policy.keep_tags_match,
            plex_server=plex_server,
        )
        log.info("scan.lists_synced", **{str(k): v for k, v in synced.items()})
        # A whitelist that failed to sync with an empty keep-list fails OPEN -- the worst
        # direction. Degrade the snapshot for each such list so it cannot be executed.
        pre_scan_degradations += await snapshot_service.protection_sync_degradations(
            cache_engine, synced
        )

        # Who requested what, keyed by media_key, so each candidate can carry a "requested
        # by" and the review queue can filter to just-requested media. Optional and soft:
        # no Seerr, or an unreachable one, means an empty map, never a failed scan.
        requested = await requested_by.build_map(seerr) if seerr is not None else {}
        # A separate three-state index used as a scoring FACT (was this requested?), built
        # from every request and fail-closed to Unknown when Seerr can't be read -- distinct
        # from the display map above, which is deliberately loose and available-only.
        request_index = await requested_by.build_request_index(seerr)

        snapshot = await snapshot_service.scan(
            cache_engine,
            session,
            radarrs=radarrs,
            sonarrs=sonarrs,
            tautulli=tautulli,
            # The same PlexClient the "Never Reap" collection used above -- reused so the
            # GUID sweep that powers id-based matching goes through the one connected,
            # guarded session, and a sweep failure degrades the snapshot.
            plex=plex,
            movie_policy=movie_policy,
            movie_gates=movie_gates,
            tv_policy=tv_policy,
            tv_gates=tv_gates,
            requested=requested,
            request_index=request_index,
            grace_days=profile_settings.grace_days,
            extra_degrade_reasons=pre_scan_degradations,
            on_progress=on_progress,
        )
        await session.commit()
        emit(Progress("complete", snapshot.item_count, snapshot.item_count, str(snapshot.id)))
        return snapshot

    # AsyncExitStack always yields; this is unreachable but satisfies the type checker,
    # which cannot see that the `async with` body returns on every path.
    raise AssertionError("unreachable")
