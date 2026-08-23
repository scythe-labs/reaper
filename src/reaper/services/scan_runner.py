# SPDX-License-Identifier: AGPL-3.0-or-later
"""Running a scan, independent of how it was triggered.

A scan is the same read-only pipeline whether a person clicked "Scan" or a schedule
fired it at 4am: build the clients from the configured instances, pull watch history
into the local mirror, refresh the protection lists, then gather-freeze-judge. The only
difference is where the progress goes -- to ``app.state.scan_status`` for a person to poll,
to the log for a timer -- so that difference is a callback, and everything else lives here once.

**A scan cannot delete anything.** It reads from the *arr and Tautulli, scores, and
writes rows to Reaper's own database. ``GuardedTransport`` refuses a mutating call even
if one were attempted.
"""

from __future__ import annotations

import time
from contextlib import AsyncExitStack
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
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
from reaper.engine.fields import CustomProtectGate
from reaper.engine.gates import (
    DataHorizonGate,
    Gate,
    GateConfig,
    GateId,
    MinDormancyGate,
    RatingFloorGate,
    ReturnedGate,
    RewatchOddsGate,
    ServerPopularityGate,
    StreamingNowGate,
)
from reaper.engine.policy import PolicyBody, join_and
from reaper.engine.policy_migrations import LIST_GATES_NOW_KEEP_RULES, PolicyRepair
from reaper.engine.reason import Reason
from reaper.refusal import Refusal
from reaper.services import (
    app_settings,
    history_sync,
    instances,
    leaving_soon,
    list_config,
    profiles,
    requested_by,
)
from reaper.services import snapshot as snapshot_service
from reaper.services.executor import ReapGateway
from reaper.services.snapshot import Progress, ProgressFn
from reaper.text import fold

log = structlog.get_logger(__name__)


class ScanConfigError(Refusal):
    """A scan cannot run because the required instances are not configured. A catalog code
    plus raw params.

    Defaults to 400 rather than ``Refusal``'s own 422: the one route that turns this into an
    HTTP response (``api.lists``'s check-now) has always answered 400. The two background
    catchers (``api.scan``, ``services.scheduler``) read ``str(exc)`` for a log line and
    ``exc.as_reason()`` for the typed reason a poller reads.
    """

    def __init__(
        self, code: str, /, *, status: int = 400, **params: str | int | float | bool
    ) -> None:
        super().__init__(code, status=status, **params)


class ScanInProgressError(Refusal):
    """A scan is already running, so this one was refused rather than run on top of it.

    A ``Refusal`` subclass (phase 11a) rather than a bare ``RuntimeError``, so ``api.scan``
    can turn it into a typed ``error_reason`` the same way it does ``ScanConfigError`` and
    ``IntegrationError`` -- every catcher still matches it by class exactly as it did before.
    """

    def __init__(self) -> None:
        super().__init__("error.scan.already_running", status=409)


# One scan at a time, enforced HERE so every caller shares it: the browser's scan button
# and the scheduler's cron job both come through run_scan, and before this claim existed
# only the button checked anything (an in-memory flag the scheduler never saw). Two
# overlapping scans double-read every source and race each other's grace-clock inserts.
# A plain bool is atomic enough: the API and the scheduler share one event loop, and the
# check-and-set in _claim_scan never awaits between the check and the set.
_scan_running = False


def _claim_scan() -> bool:
    global _scan_running
    if _scan_running:
        return False
    _scan_running = True
    return True


def _release_scan() -> None:
    global _scan_running
    _scan_running = False


def scan_running() -> bool:
    """Whether a scan holds the claim right now.

    Read by ``scheduler.sweep_old_snapshots``, which must not start a ``VACUUM`` while a scan
    is gathering: the scan writes its whole result in one commit at the end, and a write lock
    it waits only 5s for (``db.session``) can cost it every source read it made (#325).
    """
    return _scan_running


#: What the incomplete-scan notice tells the operator to look at, per repair. This is the
#: only remedy the degradation names, so a repair with no entry here would send them to the
#: policy page to check nothing in particular.
#:
#: The frontend's twin is ``REPAIR_NOTICES`` in ``frontend/src/components/PolicyEditor.tsx``:
#: one repair, two sentences, written by two people reading different files (rule 144).
#: ``tests/test_policy_repairs.py`` walks ``PolicyRepair`` against BOTH and names the file
#: that is missing a member, so neither can be the one that quietly has no copy.
#: Each entry is the TARGET alone, so the sentence below supplies one "check" however many
#: repairs compose ("check your keep rules and the points", never "check X and check Y").
_REPAIR_CHECKS: dict[PolicyRepair, str] = {
    PolicyRepair.RESCALED: "the points",
    PolicyRepair.FELL_BACK: "the values",
    PolicyRepair.RATING_RULES_RESTORED: "Keep well-rated titles",
    PolicyRepair.LISTS_MIGRATED: "your keep rules",
}


def _what_to_check(repairs: tuple[PolicyRepair, ...]) -> str:
    """The remedy clause, naming every repair's target rather than only the first.

    A body can arrive carrying two repairs (lists converted, then weights rescaled), and
    naming one sends the operator to look at a control that is not the one that moved.
    Falls back to the generic target for a repair with no entry, so a member added without
    copy still produces a sentence -- the test above is what stops it staying that way.
    """
    named = [_REPAIR_CHECKS[r] for r in repairs if r in _REPAIR_CHECKS]
    return f"check {join_and(named) if named else 'the values'}"


#: Every gate the catalog knows how to build. A gate in a policy with no entry here
#: would be a protection that silently does not fire, so the builder raises instead.
#: RATING_FLOOR is absent on purpose: it takes a set of per-source bars from the policy
#: rather than a single GateConfig, so ``build_gates`` constructs it explicitly.
GATE_TYPES: dict[GateId, type] = {
    GateId.STREAMING_NOW: StreamingNowGate,
    GateId.SERVER_POPULARITY: ServerPopularityGate,
    GateId.DATA_HORIZON: DataHorizonGate,
    GateId.MIN_DORMANCY: MinDormancyGate,
    GateId.REWATCH_ODDS: RewatchOddsGate,
    GateId.RETURNED: ReturnedGate,
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
        # "Keep well-rated titles" is a set of per-source bars, not a single threshold, so
        # it is built from the policy's rating rules rather than a GateConfig. An enabled
        # gate with no bars keeps nothing -- harmless, like an empty keep-tag list.
        if setting.gate is GateId.RATING_FLOOR:
            gates.append(
                RatingFloorGate(rules=policy.rating_rules(), match=policy.keep_rating_match)
            )
            continue
        if setting.gate is GateId.REWATCH_ODDS:
            # The one gate whose Reason needs to say "shows" on a TV policy. `Facts` carries
            # no media discriminator of its own (`gates.no_key_reason`'s movie/season merge is
            # attached per item, off the fact builder; this gate is built once per POLICY), so
            # the wording is set here, off the same ``media_type`` the policy body carries.
            gates.append(
                RewatchOddsGate(
                    GateConfig(threshold=setting.threshold, window_days=setting.window_days),
                    media_type="season" if policy.media_type == "tv" else "movie",
                )
            )
            continue
        gate_type = GATE_TYPES.get(setting.gate)
        if gate_type is None:
            # The two list gates reach this by a different route and need their own sentence.
            # They are not unimplemented: they moved to Settings, Lists, and the loader leaves
            # the gate row in place exactly when it cannot find the list that gate was
            # protecting (``policy_migrations.convert_list_protections``), so a scan stops
            # rather than running a protection short. Telling that operator Reaper "has no
            # implementation" for something called `whitelisted` names an id they have never
            # seen on any screen and points at nothing they can do (rules 21, 25).
            if setting.gate in LIST_GATES_NOW_KEEP_RULES:
                # **The order in this sentence is the whole of it, and it used to be backwards.**
                # It said to open Policy and save first, then add the list. Following that
                # loses the protection for good: the editor cannot save while the row is
                # there (the save boundary refuses the id, so Save is held), the only way to
                # reach a save is to turn the row off, and that writes a body with neither the
                # gate nor `keep_tags` -- so `has_legacy_list_protections` is False forever and
                # the conversion can never fire again. Adding the list afterwards attaches no
                # keep rule (`api/lists.py add_list` says so outright), so those titles end up
                # covered by nothing. Adding it FIRST is what makes the next load convert the
                # gate into an `on_list` rule naming it, which is the outcome this whole path
                # exists to reach (rules 25, 144;
                # `tests/test_api.py` drives both orders).
                raise ScanConfigError("error.scan.list_gate_missing_list")
            raise ScanConfigError("error.scan.gate_unimplemented", gate=setting.gate.value)
        gates.append(
            gate_type(GateConfig(threshold=setting.threshold, window_days=setting.window_days))
        )

    # The owner's own protections, on top of the built-in gates. Each condition is its own
    # gate (protect-only, evaluated through the field registry), so a matched one keeps the
    # title and shows up in the why-panel exactly like a stock protection.
    #
    # The window rides along because a protect rule may be authored on a watcher count,
    # which the watch mirror only supports as far back as it reaches (rule 140,
    # ``fields.reach_shortfall``). It is the same window the fact was counted over
    # (``snapshot._watch_stats``), so the two can never describe different spans.
    window = policy.popularity_window_days()
    gates.extend(
        CustomProtectGate(c.to_condition(), window_days=window) for c in policy.protect_conditions
    )
    return gates


async def _allowed_sections(session: AsyncSession) -> tuple[set[int] | None, str | None]:
    """Which Plex library section keys the scan may enrich against, from the operator's
    Settings -> Plex choice, and a degradation reason if the choice could not be read.

    ``None`` means "no scoping configured, use every library". Once the operator HAS
    synced their libraries and turned some off, the enabled subset scopes the sweep and
    the spine; if they have turned every one off, that explicit choice is honored (an
    empty set, which enriches nothing).

    Widening is NOT the safe direction, whatever it looks like. Removing a library from
    enrichment is exactly what keeps its files: those items resolve unmatched, abstain,
    and are never condemned. So re-adding every library on a failed read hands watch
    facts to the dormant items in libraries the operator deliberately turned off, and
    walks them into the condemnable set. Note the asymmetry this fixes: an explicit "all
    libraries off" yields an empty set (enrich nothing), while a read *failure* used to
    yield ``None`` (enrich everything) -- the failure silently picked the strictly more
    permissive branch, in the condemn direction, with nothing surfaced.

    So a read failure still returns ``None`` (a viewable snapshot beats an aborted scan)
    but hands back a reason with it, which the caller adds to ``pre_scan_degradations``:
    the snapshot is loud and un-executable, so nothing widened here can be deleted, and
    the operator is told their library choices were not applied.
    """
    try:
        libraries = await app_settings.get_plex_libraries(session)
    except Exception as exc:
        log.warning("scan.library_scope_unreadable", error=str(exc))
        return None, (
            "your Plex library choices could not be read, so this scan covered every "
            "library, including any you turned off"
        )
    if not libraries:
        return None, None
    return {int(lib["key"]) for lib in libraries if lib.get("enabled")}, None


async def build_sources(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    box: SecretBox,
    *,
    stack: AsyncExitStack,
    require_scan_sources: bool = True,
) -> tuple[
    list[snapshot_service.RadarrSource],
    list[snapshot_service.SonarrSource],
    TautulliClient | None,
    list[requested_by.SeerrSource],
    PlexClient | None,
]:
    """Build clients for EVERY enabled instance, each owned by ``stack``.

    Not ``next(...)``. A separate 4K Radarr alongside the HD one is a common setup, and
    scanning whichever came first would silently ignore an entire library while reporting
    a clean, confident, non-degraded result.

    Tautulli is required (a scan judges dormancy, and dormancy is watch history), plus
    at least one library source: Radarr, Sonarr, or both. A movie-only deployment runs
    with no Sonarr and produces no season candidates; a TV-only deployment runs with no
    Radarr and produces no movie candidates. Seerr is optional and, like Radarr and Sonarr,
    genuinely multi: EVERY enabled Seerr is read and merged -- without any, items simply
    carry no "requested by". Tautulli alone is a singleton (it mirrors one Plex), enforced
    at creation, so taking the first is correct here and not a silent drop.

    Every constructed client is entered into the caller's ``stack`` immediately, so
    there is no window in which a raise can leak one (rule 34).

    ``require_scan_sources`` is the SCAN's precondition, and only a scan has it. Refreshing a
    protection list reads the *arr and Plex and nothing else, so an install with Plex linked
    and no Tautulli was told its Plex collection could not be checked because "a scan needs a
    Tautulli instance" -- a requirement of an action the operator did not take (rule 21). With
    it off, the Tautulli slot comes back ``None`` and every other source is built as usual.
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
        plex_verify = server.verify_tls if server is not None else True

    radarr_rows = [r for r in rows if r.kind is InstanceKind.RADARR]
    sonarr_rows = [r for r in rows if r.kind is InstanceKind.SONARR]
    seerr_rows = [r for r in rows if r.kind is InstanceKind.SEERR]
    # Tautulli is a singleton (one Plex, one watch mirror), enforced at creation, so first
    # is the only one. Seerr is multi and is built as a list below, like the *arr.
    tautulli_row = next((r for r in rows if r.kind is InstanceKind.TAUTULLI), None)

    if require_scan_sources and ((not radarr_rows and not sonarr_rows) or tautulli_row is None):
        raise ScanConfigError("error.scan.missing_sources")

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
        radarrs.append(
            snapshot_service.RadarrSource(
                client=rclient,
                instance_id=r.id,
                name=r.name,
                library_map=instances.decode_library_map(r.plex_library_map),
            )
        )
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
        sonarrs.append(
            snapshot_service.SonarrSource(
                client=sclient,
                instance_id=r.id,
                name=r.name,
                library_map=instances.decode_library_map(r.plex_library_map),
            )
        )
    # Only reachable as None with `require_scan_sources` off, where the caller reads no watch
    # history: the guard above is what makes it non-None for every scan.
    tautulli: TautulliClient | None = None
    if tautulli_row is not None:
        tautulli = TautulliClient(
            tautulli_row.base_url,
            box.decrypt(tautulli_row.api_key_enc),
            safety=safety,
            verify=tautulli_row.verify_tls,
        )
        await stack.enter_async_context(tautulli)
    seerrs: list[requested_by.SeerrSource] = []
    for r in seerr_rows:
        seerr = SeerrClient(
            r.base_url,
            box.decrypt(r.api_key_enc),
            safety=safety,
            verify=r.verify_tls,
        )
        await stack.enter_async_context(seerr)
        seerrs.append(
            requested_by.SeerrSource(
                client=seerr,
                service_instance_map=instances.decode_service_instance_map(r.service_instance_map),
            )
        )
    plex: PlexClient | None = None
    if plex_uri is not None and plex_token is not None:
        plex = PlexClient(plex_uri, plex_token, safety=safety, verify=plex_verify)
        await stack.enter_async_context(plex)
    return radarrs, sonarrs, tautulli, seerrs, plex


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
    async with session_factory() as session:
        rows = (
            (await session.execute(select(Instance).where(Instance.enabled.is_(True))))
            .scalars()
            .all()
        )
        server = (await session.execute(select(PlexServer))).scalars().first()
        plex_token = box.decrypt(server.token_enc) if server is not None else None
        plex_uri = server.connection_uri if server is not None else None
        plex_verify = server.verify_tls if server is not None else True

    radarr: dict[int, RadarrClient] = {}
    sonarr: dict[int, SonarrClient] = {}
    closers: list[BaseClient | PlexClient] = []
    tautulli: TautulliClient | None = None

    # Every client is registered for close the instant it exists, so a raise part-way
    # through -- box.decrypt on a row whose api_key_enc was written under a different key
    # is the realistic one -- closes the ones already built instead of stranding their
    # connection pools for the life of the process (rule 34). This is build_sources'
    # discipline, adapted: the caller cannot own the stack here, because these clients
    # must outlive the request that builds them (the background run closes them). So the
    # stack guards construction only, and pop_all() hands ownership back on success --
    # push_async_callback rather than enter_async_context, so nothing is entered twice
    # when the run's own stack enters them for real.
    async with AsyncExitStack() as building:
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
                building.push_async_callback(client.aclose)
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
                building.push_async_callback(sclient.aclose)
                sonarr[row.id] = sclient
                closers.append(sclient)
            elif row.kind is InstanceKind.TAUTULLI and tautulli is None:
                tautulli = TautulliClient(row.base_url, key, safety=safety, verify=row.verify_tls)
                building.push_async_callback(tautulli.aclose)
                closers.append(tautulli)

        plex = (
            PlexClient(plex_uri, plex_token, safety=safety, verify=plex_verify)
            if plex_uri is not None and plex_token is not None
            else None
        )
        if plex is not None:
            building.push_async_callback(plex.aclose)
            closers.append(plex)

        gateway = ReapGateway(radarr=radarr, sonarr=sonarr, plex=plex, tautulli=tautulli)
        # Built without a failure: detach every close callback, so leaving this block
        # closes nothing and the caller's ownership of `closers` is exactly as before.
        building.pop_all()

    return gateway, closers


#: What Tautulli might spell a boolean as, beyond the integers it actually stores.
#: Anything outside these reads as unreadable, not as true.
_TRUE_TOKENS = frozenset({"true", "t", "yes", "y", "on"})
_FALSE_TOKENS = frozenset({"false", "f", "no", "n", "off"})


def _flag(row: Any, name: str) -> bool | None:
    """A Tautulli boolean-ish field (``1``/``0``, ``True``/``False``, ``"1"``/``"0"``),
    or None when the row or field is missing or unreadable -- the caller decides what
    absence means, because it differs per flag.

    Every value that is not a recognized boolean returns None, including a truthy one.
    ``bool("false")`` is True, so falling back to plain truthiness read an unparseable
    keep-history value as "this user IS recording history": no degradation, and every
    title only that user watches then scores as never-played, which is the one case
    ``_keep_history_degradations`` exists to catch. Unreadable is not True.
    """
    if not isinstance(row, dict):
        return None
    value = row.get(name)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        # Tautulli stores these as INTEGERs, so this is the real path: 1/0, "1"/"0".
        return bool(int(value))
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        token = fold(value)
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    return None


async def _keep_history_degradations(tautulli: TautulliClient) -> list[str]:
    """Degrade the snapshot while Tautulli is not recording history for an active user.

    Tautulli's per-user "Keep History" toggle silently drops that user's plays, so a
    title only they watch looks never-played -- the exact evidence the condemn lane
    scores on. While any active user has it off, "nobody watched it" cannot be
    distinguished from "the recorder was off for the one person watching", so the scan
    stays viewable but nothing may be deleted from it.

    Fail-closed at every step (watch history is a source, rule 28): an unreadable user
    list degrades, and so does a user row whose Keep History flag cannot be read. A user
    marked inactive (removed from the server) is skipped -- their history is not being
    written anywhere, because they cannot play anything.
    """
    try:
        users = await tautulli.users()
    except Exception as exc:
        log.warning("scan.keep_history_unreadable", error=str(exc))
        return [
            "Reaper could not check whether Tautulli records watch history for every "
            f"user ({exc}). If anyone's history is off, their plays are invisible, so "
            "nothing may be deleted from this scan"
        ]

    if not users:
        # A configured Tautulli always reports at least the owner. An empty result means
        # the user list was unreadable (a success response with a null/malformed body
        # coerces to []), which is indistinguishable from "everyone records history", so
        # fail closed rather than trust it.
        log.warning("scan.keep_history_empty_user_list")
        return [
            "Reaper could not read the Tautulli user list, so it cannot confirm watch "
            "history is being recorded. Nothing may be deleted from this scan"
        ]

    unrecorded: list[str] = []
    unreadable = 0
    for row in users:
        # _flag guards against junk rows itself: a non-dict row reads as None for every
        # flag, so it lands in the cannot-confirm bucket below rather than crashing.
        active = _flag(row, "is_active")
        if active is False:
            # Inactive means removed from the Plex server: they cannot play anything,
            # so their history setting cannot hide a play.
            continue
        keeps = _flag(row, "keep_history")
        if keeps is None:
            unreadable += 1
        elif not keeps:
            name = str(row.get("friendly_name") or row.get("username") or "a user")
            unrecorded.append(name)

    reasons: list[str] = []
    if unrecorded:
        shown = ", ".join(sorted(unrecorded)[:5])
        more = len(unrecorded) - 5
        if more > 0:
            shown += f" and {more} more"
        reasons.append(
            f"Tautulli is not recording watch history for: {shown}. Anything only they "
            "watch looks never-played, so nothing may be deleted from this scan. Turn "
            "on Keep History for them in Tautulli under Users"
        )
    if unreadable:
        reasons.append(
            f"Reaper could not read the Keep History setting for {unreadable} Tautulli "
            "user(s), so it cannot confirm their plays are recorded. Nothing may be "
            "deleted from this scan"
        )
    return reasons


async def run_scan(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    cache_engine: AsyncEngine,
    box: SecretBox,
    on_progress: ProgressFn | None = None,
) -> Snapshot:
    """The whole read-only pipeline, from configured instances to a persisted snapshot.

    One scan runs at a time, whoever asked: a second call while one is in flight raises
    :class:`ScanInProgressError` immediately (the scheduler logs it and skips; the scan
    route surfaces it). Raises :class:`ScanConfigError` if the required instances are
    missing, and :class:`IntegrationError` if a source fails hard mid-run. A per-instance
    failure is caught inside the snapshot and degrades it (loud, viewable, un-executable)
    rather than aborting -- partial evidence must never look complete.
    """
    if not _claim_scan():
        raise ScanInProgressError()
    try:
        return await _run_scan_locked(
            settings=settings,
            session_factory=session_factory,
            cache_engine=cache_engine,
            box=box,
            on_progress=on_progress,
        )
    finally:
        _release_scan()


async def _run_scan_locked(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    cache_engine: AsyncEngine,
    box: SecretBox,
    on_progress: ProgressFn | None = None,
) -> Snapshot:
    """The pipeline body. Only ever entered holding the one-scan claim (see run_scan)."""
    emit = on_progress or (lambda _p: None)

    # Wall-clock metrics, so an operator can see how long a scan takes and where the time
    # goes. Monotonic, never the wall clock: a clock adjustment mid-scan must not produce a
    # negative or wildly wrong duration in the log.
    scan_started = time.monotonic()
    history_ms = 0
    lists_ms = 0
    #: Set when the list registry could not be read. Collected here rather than appended
    #: inside the session block, so it joins `pre_scan_degradations` with the others below.
    lists_degradation: str | None = None

    async with AsyncExitStack() as stack:
        # Sources are constructed INSIDE the stack scope, and build_sources enters each
        # client the moment it exists -- so a failure constructing a later client, or
        # anything below, can never leak the earlier ones (rule 34). Seerr and Plex are
        # owned by the same stack; they used to be reclaimed only by GC.
        radarrs, sonarrs, tautulli, seerrs, plex = await build_sources(
            session_factory, settings, box, stack=stack
        )
        # `require_scan_sources` defaults on, so this raised above rather than reaching here.
        # Asserted rather than assumed: a scan without watch history judges dormancy against
        # nothing, and every score leans on dormancy.
        if tautulli is None:  # pragma: no cover - the guard in build_sources precedes it
            raise ScanConfigError("error.scan.missing_sources")

        async with session_factory() as policy_session:
            active_movie, active_tv = await profiles.active_policies(policy_session)
            movie_policy, tv_policy = active_movie.body, active_tv.body
            # The grace window is a profile setting, read here so the scan can restart
            # the grace clock for an item that left the condemned set and returned (see
            # snapshot._record_first_flagged); a longer gap than this means a genuine
            # departure.
            active_profile = await profiles.active_profile(policy_session)
            profile_settings = active_profile.settings
            allowed_sections, scope_degradation = await _allowed_sections(policy_session)
            # The lists the operator defined on Settings -> Lists. Read here, with the policy,
            # because both are settings the scan freezes at its start: a list added while a
            # scan is running belongs to the next one, not to this half-gathered evidence.
            #
            # A read FAILURE is not "no lists configured" (rules 65/91). Every keep collection
            # and every curated list comes from this table, so an empty answer would sync none
            # of them and retire the lot -- withdrawing every one of those protections because
            # a query failed. `None` says "unknown", which holds the registry half of the sync
            # still and retires nothing, and the scan degrades so nothing can be deleted under
            # protections it could not confirm.
            #
            # A single row whose body will not parse takes the same branch, and for the same
            # reason: it is absent from the definitions either way, so the sweep would read it
            # as deleted and disable the membership it is still protecting with.
            list_definitions: list[list_config.ListDefinition] | None
            try:
                list_definitions = await list_config.definitions(policy_session, strict=True)
            except (SQLAlchemyError, list_config.ListRegistryUnreadableError) as exc:
                list_definitions = None
                # The exception text stays in the log and out of the sentence. A
                # SQLAlchemyError stringifies to the whole failing statement and
                # `ListRegistryUnreadableError` to a row id, and this line is rendered
                # verbatim in the incomplete-scan notice on three screens (rule 21).
                # Neither names anything the reader can go and fix, and the fixed sentence
                # already says what to do about it.
                log.warning("scan.lists_unreadable", error=str(exc))
                lists_degradation = (
                    "Your protection lists could not be read, so Reaper cannot tell which "
                    "lists should be keeping titles safe"
                )
        movie_gates = build_gates(movie_policy)
        tv_gates = build_gates(tv_policy)

        session = await stack.enter_async_context(session_factory())

        # Failures to gather BEFORE the freeze are collected here and handed to the scan,
        # which degrades the snapshot for each (loud, viewable, un-executable) exactly as an
        # in-gather source failure does. None of them may silently pass through.
        pre_scan_degradations: list[str] = []

        # The operator's library choices could not be read, so this scan enriched every
        # library instead of the subset they picked. That widens the condemnable set,
        # so it degrades (see _allowed_sections).
        if scope_degradation:
            pre_scan_degradations.append(scope_degradation)

        # The list registry was unreadable, so none of the lists it defines were refreshed
        # and none were retired. Whatever they were protecting is unconfirmed, which is the
        # keep direction for the stored copies and an un-executable scan for this run.
        if lists_degradation:
            pre_scan_degradations.append(lists_degradation)

        # A stored policy that no longer validated was repaired to load it (profiles
        # .ActivePolicy.repaired). The rescale cannot move a score, so scanning on it is
        # safe -- but it is NOT the policy the operator saved, and a run must never
        # execute against one nobody approved. Degrade, so the scan still produces a
        # viewable snapshot and the fix is one visit to the policy page.
        # "TV", not the raw `tv` media-type id: this lands verbatim in the incomplete-scan
        # notice on three screens, and the app spells it TV everywhere else, so lower case
        # read as a typo in the middle of a sentence telling the operator to go and fix
        # something. `api/simulate.py`'s simulator sentence already maps its own lane this way
        # (rules 21, 144).
        for label, active in (("movie", active_movie), ("TV", active_tv)):
            if active.repairs:
                pre_scan_degradations.append(
                    f"your {label} policy needs saving again before anything can be removed: "
                    f"open the policy page, {_what_to_check(active.repairs)}, and save"
                )
            # A different fault under the same `repaired` flag, and it needs a different
            # sentence: nothing was repaired and saving fixes nothing. The registry could not
            # be read while the shipped default was assembled, so this scan is judging with no
            # keep rule naming the operator's own Plex lists -- and the scan's own registry
            # read can succeed on a transient error, which is what let it run clean (rule 65).
            elif active.lists_unreadable:
                pre_scan_degradations.append(
                    f"your protection lists could not be read while the {label} policy was "
                    "worked out, so this scan ran without them and nothing may be removed "
                    "from it. Run another scan"
                )

        # Same reasoning for the profile's caps and grace: when the stored settings blob was
        # unreadable, the run is holding the shipped defaults, which can be LOOSER than what
        # the operator saved (a shorter grace, a higher cap). A run must never execute against
        # limits nobody saved, so degrade until they re-save (profiles.ActiveProfile.fell_back,
        # rule 65).
        if active_profile.fell_back:
            pre_scan_degradations.append(
                "your caps and grace need saving again before anything can be removed: open "
                "the policy page, check Pace and limits, and save"
            )

        # Pull watch history into the local mirror BEFORE scoring reads it. Incremental
        # after the first time, but on a fresh install it is what populates the table at
        # all -- without it every item's dormancy is Unknown and the scan judges nothing.
        emit(Progress("history", 0, 0, Reason("history_sync")))
        history_started = time.monotonic()
        try:
            hist = await history_sync.sync(cache_engine, tautulli)
            history_ms = round((time.monotonic() - history_started) * 1000)
            log.info("scan.history_synced", rows=hist.rows, duration_ms=history_ms)
            if hist.unservable:
                # A play the source refused to return is missing evidence, in the condemn
                # direction, so it degrades exactly as a sync that raised does (rule 28).
                # The walk stepped over the row rather than raising so the rows after it
                # still landed (`history_sync.MAX_UNSERVABLE_ROWS`).
                pre_scan_degradations.append(
                    f"Tautulli couldn't return {hist.unservable} of your plays, so they "
                    "don't count and nothing may be deleted from this scan."
                )
        except IntegrationError as exc:
            history_ms = round((time.monotonic() - history_started) * 1000)
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

        # The mirror only holds what Tautulli recorded. A user whose per-user "Keep
        # History" toggle is off is invisible in it: everything only they watch reads
        # never-played, which is the exact evidence the condemn lane scores on. Checked
        # every scan, fail-closed both ways (an active user with recording off degrades,
        # and so does failing to read the user list at all).
        pre_scan_degradations += await _keep_history_degradations(tautulli)

        # Refresh the protection lists BEFORE scoring reads them, or every list the
        # operator defined is silently empty and protects nothing.
        emit(Progress("lists", 0, 0, Reason("lists")))

        # Plex is optional (a movie-only deployment runs without it), but a *configured*
        # Plex that is briefly unreachable must degrade, not crash the whole scan the way an
        # uncaught PlexError from connect() would. Critically it must fail CLOSED: with no
        # live server no Plex-sourced list can refresh, so each is skipped (the atomic swap
        # keeps any prior membership) AND degrade, so a reap cannot run against a keep-list
        # that could not be confirmed.
        plex_server: object | None = None
        if plex is not None:
            try:
                plex_server = await plex.connect()
            except PlexError as exc:
                plex_server = None
                # Named by source, never by collection: the collection is the operator's to
                # name (#483) and a watchlist list has no collection at all, so spelling one
                # hardcoded title here claimed a list they may not have and hid the others
                # that went unrefreshed beside it (rules 21, 144).
                pre_scan_degradations.append(
                    f"Plex could not be reached: {exc}. The lists Reaper reads from Plex "
                    "were not refreshed, so nothing may be deleted from this scan"
                )
        # Reaper's own Plex server id, read off the connection we already opened (no extra
        # round-trip). It lets the requested-by map skip a portal synced to a DIFFERENT Plex,
        # whose rating keys would collide with Reaper's candidates (I-3). None when Plex is
        # absent or unreachable, which keeps the rating-key tier's current behavior.
        reaper_plex_machine_id: str | None = None
        if plex_server is not None:
            _mid = getattr(plex_server, "machineIdentifier", None)
            reaper_plex_machine_id = str(_mid) if _mid else None

        # Three independent reads, overlapped: the protection-list refresh (the *arr tag
        # sweeps, the Plex collection, the Top 250 mirror), the "requested by" display map,
        # and the requested-or-not scoring index (the two Seerr reads walk different
        # request filters). Each already fails soft on its own -- a failed list records an
        # error the degradation check below picks up, an unreachable Seerr yields an empty
        # map and an Unknown-producing index -- so overlapping them changes only the wait.
        # gather_reaped so an unexpected failure on one branch cancels and drains the
        # others before the exit stack closes the clients they are still reading through.
        lists_started = time.monotonic()
        synced, requested, request_index = await gather_reaped(
            snapshot_service.sync_protection_lists(
                cache_engine,
                definitions=list_definitions,
                radarrs=radarrs,
                sonarrs=sonarrs,
                plex_server=plex_server,
            ),
            # Who requested what: each candidate's "requested by", and the review queue's
            # just-requested filter. Keyed by the exact copy's media_key where the operator
            # mapped the Seerr service, else by tmdb/tvdb. Merged across every Seerr. Optional
            # and soft: no Seerr, or any unreachable one, just leaves those requests off the
            # map, never a failed scan.
            requested_by.build_map(seerrs, reaper_plex_machine_id=reaper_plex_machine_id),
            # A separate three-state index used as a scoring FACT (was this requested?),
            # built from every request in every Seerr and fail-closed to Unknown when ANY
            # Seerr can't be read -- distinct from the display map above, which is
            # deliberately loose and available-only.
            requested_by.build_request_index([s.client for s in seerrs]),
        )
        lists_ms = round((time.monotonic() - lists_started) * 1000)
        log.info(
            "scan.lists_synced", duration_ms=lists_ms, **{str(k): v for k, v in synced.items()}
        )
        # A whitelist that failed to sync with an empty keep-list fails OPEN -- the worst
        # direction. Degrade the snapshot for each such list so it cannot be executed.
        pre_scan_degradations += await snapshot_service.protection_sync_degradations(
            cache_engine, synced
        )

        gather_started = time.monotonic()
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
            allowed_sections=allowed_sections,
            # Off the definitions this scan actually synced against, never a second read:
            # a list edited between the two would otherwise be recorded as the configuration
            # a membership was gathered under when it was not. `None` when they could not be
            # read at all, which the branch above already degraded the scan for.
            list_config_hash=(
                None if list_definitions is None else list_config.fingerprint(list_definitions)
            ),
        )
        gather_ms = round((time.monotonic() - gather_started) * 1000)
        commit_started = time.monotonic()
        await session.commit()
        commit_ms = round((time.monotonic() - commit_started) * 1000)

        # The snapshot is committed and safe on disk, so the grace set just changed -- this
        # is the moment the "Leaving Soon" shelf (and the Discord heads-up) go stale. That
        # reconcile is a full per-library Plex round-trip, and it is NOT scan work: the run
        # is not finished until it returns and `running` flips false (api/scan.py). So give
        # it its own visible phase instead of emitting "complete"/100% here and letting the
        # bar sit at a false 100% while several more Plex calls run. Best-effort by design:
        # after_scan swallows and logs its own failures -- the warning layer must never fail
        # a scan that already landed.
        #
        # Skipped entirely on a degraded snapshot. The shelf labels items in Plex and
        # posts a heads-up naming them, both of which are assertions to other people that
        # a file is going away -- and this snapshot's condemn set was built on evidence
        # Reaper declared untrustworthy and refuses to plan from. Announcing it would
        # warn about files nothing can delete, and that a healthy scan may well keep. The
        # previous shelf stands until a clean scan replaces it.
        after_scan_ms: int | None = None
        if snapshot.degraded:
            log.info("scan.shelves_skipped", snapshot=snapshot.id, reason="degraded")
        else:
            emit(Progress("shelves", 0, 0, Reason("shelves")))
            after_scan_started = time.monotonic()
            await leaving_soon.after_scan(session_factory, settings, box)
            after_scan_ms = round((time.monotonic() - after_scan_started) * 1000)

        # Only now is the run truly done: bar to 100%, and the browser's next poll sees
        # `running` flip false right behind it.
        emit(Progress("complete", snapshot.item_count, snapshot.item_count, None))

        # The one line an operator reads to answer "how long did that take, and where did
        # the time go?" -- the TRUE end-to-end wall clock (including the shelf reconcile,
        # which a total measured before after_scan silently hid) plus the phase breakdown,
        # all monotonic milliseconds. gather_ms is dominated by the source reads; the
        # intra-gather split (plex_index / radarr / season / score) is logged separately by
        # snapshot.scan as `snapshot.gather_timing`, so a slow scan can be pinned to one
        # source without guessing.
        log.info(
            "scan.completed",
            snapshot=snapshot.id,
            items=snapshot.item_count,
            degraded=snapshot.degraded,
            history_ms=history_ms,
            lists_ms=lists_ms,
            gather_ms=gather_ms,
            commit_ms=commit_ms,
            after_scan_ms=after_scan_ms,
            wall_ms=round((time.monotonic() - scan_started) * 1000),
        )
        return snapshot

    # AsyncExitStack always yields; this is unreachable but satisfies the type checker,
    # which cannot see that the `async with` body returns on every path.
    raise AssertionError("unreachable")
