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

from reaper.aio import gather_reaped
from reaper.clients.arr import RadarrClient, SonarrClient
from reaper.clients.base import BaseClient, IntegrationError
from reaper.clients.plex import PlexClient, PlexError
from reaper.clients.seerr import SeerrClient
from reaper.clients.tautulli import TautulliClient
from reaper.config import RuntimeSafety, Settings
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
    *,
    stack: AsyncExitStack,
) -> tuple[
    list[snapshot_service.RadarrSource],
    list[snapshot_service.SonarrSource],
    TautulliClient,
    SeerrClient | None,
    PlexClient | None,
]:
    """Build clients for EVERY enabled instance, each owned by ``stack``.

    Not ``next(...)``. A separate 4K Radarr alongside the HD one is a common setup, and
    scanning whichever came first would silently ignore an entire library while reporting
    a clean, confident, non-degraded result.

    Tautulli is required (a scan judges dormancy, and dormancy is watch history), plus
    at least one library source: Radarr, Sonarr, or both. A movie-only deployment runs
    with no Sonarr and produces no season candidates; a TV-only deployment runs with no
    Radarr and produces no movie candidates. Seerr is optional -- without it, items
    simply carry no "requested by".

    Every constructed client is entered into the caller's ``stack`` immediately, so
    there is no window in which a raise can leak one (rule 34).
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

    if (not radarr_rows and not sonarr_rows) or tautulli_row is None:
        raise ScanConfigError(
            "A scan needs a Tautulli instance plus at least one Radarr or Sonarr. "
            "Add them in Settings first."
        )

    # Each client carries its instance's own TLS setting (``verify_tls``, on by default):
    # the decrypted API key travels on these connections, so certificate verification is
    # only relaxed where the operator explicitly turned it off for that one instance in
    # Settings -- never silently, and never for the others.
    # Every client is entered into ``stack`` the moment it is constructed, so a failure
    # building a LATER one -- or anything the caller does before its own stack entry --
    # can never leak the earlier ones.
    radarrs: list[snapshot_service.RadarrSource] = []
    for r in radarr_rows:
        rclient = RadarrClient(
            r.base_url,
            box.decrypt(r.api_key_enc),
            safety=safety,
            api_path_prefix=r.api_path_prefix,
            verify=r.verify_tls,
        )
        await stack.enter_async_context(rclient)
        radarrs.append(snapshot_service.RadarrSource(client=rclient, instance_id=r.id, name=r.name))
    sonarrs: list[snapshot_service.SonarrSource] = []
    for r in sonarr_rows:
        sclient = SonarrClient(
            r.base_url,
            box.decrypt(r.api_key_enc),
            safety=safety,
            api_path_prefix=r.api_path_prefix,
            verify=r.verify_tls,
        )
        await stack.enter_async_context(sclient)
        sonarrs.append(snapshot_service.SonarrSource(client=sclient, instance_id=r.id, name=r.name))
    tautulli = TautulliClient(
        tautulli_row.base_url,
        box.decrypt(tautulli_row.api_key_enc),
        safety=safety,
        verify=tautulli_row.verify_tls,
    )
    await stack.enter_async_context(tautulli)
    seerr: SeerrClient | None = None
    if seerr_row is not None:
        seerr = SeerrClient(
            seerr_row.base_url,
            box.decrypt(seerr_row.api_key_enc),
            safety=safety,
            verify=seerr_row.verify_tls,
        )
        await stack.enter_async_context(seerr)
    plex: PlexClient | None = None
    if plex_uri is not None and plex_token is not None:
        plex = PlexClient(plex_uri, plex_token, safety=safety)
        await stack.enter_async_context(plex)
    return radarrs, sonarrs, tautulli, seerr, plex


async def build_reap_gateway(
    session_factory: async_sessionmaker[AsyncSession],
    box: SecretBox,
    *,
    safety: RuntimeSafety,
) -> tuple[ReapGateway, list[BaseClient | PlexClient]]:
    """Build the live clients a real reap drives, keyed by the instance each item is from.

    Returns the gateway plus the httpx-backed clients that must be closed after the run --
    the caller enters them into an ``AsyncExitStack`` so a run cannot leak connections. The
    Radarr/Sonarr clients are keyed by ``instance.id`` because a plan routes each item to
    *its* server by ``media_key``; picking "the first Radarr" would delete from the wrong
    one on a 4K + HD split. Plex and Tautulli are single, and a real run refuses without
    them (the executor enforces that) -- so they are built when present and simply absent
    otherwise, letting the executor produce the precise refusal.

    The clients are built with the CALLER'S :class:`RuntimeSafety` -- the same snapshot the
    executor itself runs under -- so the transport guard and the executor can never read
    two different switch states for one run. The guard still enforces armed-plus-declared
    independently of the executor's own check.
    """
    from reaper.services.executor import ReapGateway

    async with session_factory() as session:
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
    closers: list[BaseClient | PlexClient] = []
    tautulli: TautulliClient | None = None

    for row in rows:
        key = box.decrypt(row.api_key_enc)
        if row.kind is InstanceKind.RADARR:
            client = RadarrClient(
                row.base_url,
                key,
                safety=safety,
                api_path_prefix=row.api_path_prefix,
                verify=row.verify_tls,
            )
            radarr[row.id] = client
            closers.append(client)
        elif row.kind is InstanceKind.SONARR:
            sclient = SonarrClient(
                row.base_url,
                key,
                safety=safety,
                api_path_prefix=row.api_path_prefix,
                verify=row.verify_tls,
            )
            sonarr[row.id] = sclient
            closers.append(sclient)
        elif row.kind is InstanceKind.TAUTULLI and tautulli is None:
            tautulli = TautulliClient(row.base_url, key, safety=safety, verify=row.verify_tls)
            closers.append(tautulli)

    plex = (
        PlexClient(plex_uri, plex_token, safety=safety)
        if plex_uri is not None and plex_token is not None
        else None
    )
    if plex is not None:
        closers.append(plex)

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

    async with AsyncExitStack() as stack:
        # Sources are constructed INSIDE the stack scope, and build_sources enters each
        # client the moment it exists -- so a failure constructing a later client, or
        # anything below, can never leak the earlier ones (rule 34). Seerr and Plex are
        # owned by the same stack; they used to be reclaimed only by GC.
        radarrs, sonarrs, tautulli, seerr, plex = await build_sources(
            session_factory, settings, box, stack=stack
        )

        async with session_factory() as policy_session:
            movie_policy, tv_policy = await profiles.active_policies(policy_session)
            # The grace window is a profile setting, read here so the scan can restart
            # the grace clock for an item that left the condemned set and returned (see
            # snapshot._record_first_flagged); a longer gap than this means a genuine
            # departure.
            profile_settings = await profiles.active_profile_settings(policy_session)
        movie_gates = build_gates(movie_policy)
        tv_gates = build_gates(tv_policy)

        session = await stack.enter_async_context(session_factory())

        # Failures to gather BEFORE the freeze are collected here and handed to the scan,
        # which degrades the snapshot for each (loud, viewable, un-executable) exactly as an
        # in-gather source failure does. None of them may silently pass through.
        pre_scan_degradations: list[str] = []

        # Pull watch history into the local mirror BEFORE scoring reads it. Incremental
        # after the first time, but on a fresh install it is what populates the table at
        # all -- without it every item's dormancy is Unknown and the scan judges nothing.
        emit(Progress("history", 0, 0, "syncing watch history"))
        try:
            hist = await history_sync.sync(cache_engine, tautulli)
            log.info("scan.history_synced", rows=hist.rows)
        except IntegrationError as exc:
            # The mirror is the primary condemning evidence: dormancy and watcher counts
            # are read from it, and a play that landed after the last successful sync is
            # invisible to scoring (the streaming veto covers only right-now, and the
            # played-since-approval check starts at approval). Scoring quietly on a stale
            # mirror fails OPEN, so the snapshot must degrade -- loud, viewable, and
            # un-executable -- exactly like a Plex or whitelist failure below.
            log.warning("scan.history_sync_failed", error=str(exc))
            pre_scan_degradations.append(
                f"Watch history could not be refreshed: {exc}. Recent plays may be "
                "missing, so items were judged on stale evidence and nothing may be "
                "deleted from this scan."
            )

        # Refresh the protection lists BEFORE scoring reads them, or a "Never Reap"
        # collection and the IMDb Top 250 are silently empty and protect nothing.
        emit(Progress("lists", 0, 0, "refreshing protection lists"))

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
        # Three independent reads, overlapped: the protection-list refresh (the *arr tag
        # sweeps, the Plex collection, the Top 250 mirror), the "requested by" display map,
        # and the requested-or-not scoring index (the two Seerr reads walk different
        # request filters). Each already fails soft on its own -- a failed list records an
        # error the degradation check below picks up, an unreachable Seerr yields an empty
        # map and an Unknown-producing index -- so overlapping them changes only the wait.
        # gather_reaped so an unexpected failure on one branch cancels and drains the
        # others before the exit stack closes the clients they are still reading through.
        synced, requested, request_index = await gather_reaped(
            snapshot_service.sync_protection_lists(
                cache_engine,
                radarrs=radarrs,
                sonarrs=sonarrs,
                movie_keep_tags=movie_policy.keep_tags,
                movie_keep_match=movie_policy.keep_tags_match,
                tv_keep_tags=tv_policy.keep_tags,
                tv_keep_match=tv_policy.keep_tags_match,
                plex_server=plex_server,
            ),
            # Who requested what, keyed by media_key, so each candidate can carry a
            # "requested by" and the review queue can filter to just-requested media.
            # Optional and soft: no Seerr, or an unreachable one, means an empty map,
            # never a failed scan.
            requested_by.build_map(seerr),
            # A separate three-state index used as a scoring FACT (was this requested?),
            # built from every request and fail-closed to Unknown when Seerr can't be read
            # -- distinct from the display map above, which is deliberately loose and
            # available-only.
            requested_by.build_request_index(seerr),
        )
        log.info("scan.lists_synced", **{str(k): v for k, v in synced.items()})
        # A whitelist that failed to sync with an empty keep-list fails OPEN -- the worst
        # direction. Degrade the snapshot for each such list so it cannot be executed.
        pre_scan_degradations += await snapshot_service.protection_sync_degradations(
            cache_engine, synced
        )

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
