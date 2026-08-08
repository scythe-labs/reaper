# SPDX-License-Identifier: AGPL-3.0-or-later
"""Application factory."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from platform import python_version
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from reaper import __version__, logbuffer
from reaper.api import tags as api_tags
from reaper.api.auth import router as auth_router
from reaper.api.backup import router as backup_router
from reaper.api.breakdown import router as breakdown_router
from reaper.api.fairness import router as fairness_router
from reaper.api.leaving_soon import router as leaving_soon_router
from reaper.api.lists import router as lists_router
from reaper.api.logs import router as logs_router
from reaper.api.middleware import (
    CSRF_HEADER,
    CSRF_VALUE,
    AuthGuard,
    api_key_refused,
    api_key_scope_description,
    no_credential_needed,
)
from reaper.api.plex import router as plex_router
from reaper.api.plex_trash import router as plex_trash_router
from reaper.api.poster import close_artwork_client
from reaper.api.poster import router as poster_router
from reaper.api.routes import router
from reaper.api.runs import profile_router, reap_in_flight
from reaper.api.runs import router as runs_router
from reaper.api.scan import router as scan_router
from reaper.api.settings import router as settings_router
from reaper.api.setup import router as setup_router
from reaper.api.whitelist import router as whitelist_router
from reaper.auth.admins import count_local_admins
from reaper.auth.cookie import DOCUMENTED_SESSION_COOKIE
from reaper.auth.proxy import parse_proxy_networks
from reaper.auth.recovery import clear_recovery_file, mint_recovery_token, recovery_base_url
from reaper.auth.sessions import clear_recovery_marks
from reaper.buildinfo import build_version, install_kind, install_root, is_release, short_commit
from reaper.config import (
    Settings,
    get_settings,
    load_raw_env,
    parse_instance_seeds,
)
from reaper.crypto import SecretBox
from reaper.db.models import Instance, InstanceKind, PlexServer
from reaper.db.session import (
    create_cache_engine,
    create_engine,
    create_session_factory,
    journal_mode,
)
from reaper.logging import configure_logging
from reaper.secrets import resolve_kdf_salt, resolve_old_keys, resolve_secret_key
from reaper.services import app_settings
from reaper.services.scheduler import (
    apply_stored_schedules,
    build_scheduler,
    catch_up_on_startup,
    track_running_jobs,
)
from reaper.services.seeding import seed_instances
from reaper.services.update_check import UpdateChecker

log = structlog.get_logger(__name__)


class HealthResponse(BaseModel):
    """Route return types must be resolvable at runtime.

    ``from __future__ import annotations`` turns them into strings, and FastAPI
    builds a response model by resolving them -- so a type imported only under
    ``TYPE_CHECKING`` yields a 500 at request time, not an error at import time.
    """

    status: str


def _report_background_failure(task: asyncio.Task[Any]) -> None:
    """Log why a detached startup task died, instead of letting asyncio mumble at GC time.

    A task nobody awaits keeps its exception until it is garbage collected, and then all
    the operator gets is a bare "Task exception was never retrieved" with no name attached
    (PR-12). Cancellation is the normal shutdown path and says nothing.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("startup.background_task_failed", task=task.get_name(), error=str(exc))


async def _schema_revision(session: AsyncSession) -> str | None:
    """The Alembic revision this database sits at, for the boot log.

    Nothing else in the app reads it outside backup and restore, so "which schema is
    this install on, and did the upgrade run" has no answer today -- migrations are
    applied by a different process (the container entrypoint, or `launcher._migrate`)
    whose output never reaches the log file the operator downloads. ``None`` is a
    database built straight from the models, which is what a test carries.
    """
    try:
        result = await session.execute(text("SELECT version_num FROM alembic_version"))
    except OperationalError:
        return None
    row = result.first()
    return str(row[0]) if row and row[0] else None


async def _integration_inventory(
    session: AsyncSession, box: SecretBox, settings: Settings
) -> dict[str, int | bool]:
    """What this install is wired to, as counts and flags.

    Never a base URL, a name, or a key: an instance's address is one of the two places
    a credential can hide (rule 13), and the count is what answers "why is my second
    Radarr missing from this scan". Disabled instances are excluded, because the scan
    excludes them too and a total that disagrees with the scan is worse than no total.
    """
    rows = await session.execute(
        select(Instance.kind, func.count())
        .where(Instance.enabled.is_(True))
        .group_by(Instance.kind)
    )
    inventory: dict[str, int | bool] = {str(kind.value): count for kind, count in rows.all()}
    for kind in InstanceKind:
        inventory.setdefault(str(kind.value), 0)
    plex = await session.execute(select(func.count()).select_from(PlexServer))
    inventory["plex_linked"] = plex.scalar_one() > 0
    inventory["discord"] = await app_settings.has_discord_webhook(session, box, settings)
    return inventory


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    engine = create_engine(settings)
    app.state.engine = engine
    factory = create_session_factory(engine)
    app.state.session_factory = factory

    # The caches live in their own file. See Settings.cache_database_url.
    cache_engine = create_cache_engine(settings)
    app.state.cache_engine = cache_engine

    # Generates and persists a key on first boot if none was configured. It must
    # persist: it decrypts credentials written on previous boots. Any retired keys
    # (REAPER_SECRET_KEY_OLD) ride along decrypt-only, so a key rotation does not
    # brick credentials written under the previous key, and the per-install KDF salt
    # (secret.salt, minted the same way) makes the derivation unique to this install.
    box = SecretBox(
        resolve_secret_key(settings),
        *resolve_old_keys(settings),
        salt=resolve_kdf_salt(settings),
    )
    app.state.secret_box = box

    async with factory() as session:
        # Import any instances declared in the environment. Never overwrites
        # anything already in the database.
        # load_raw_env, not os.environ: .env files are read by pydantic-settings
        # and never exported to the process environment.
        await seed_instances(session, parse_instance_seeds(load_raw_env(settings)), box)

        # Before the mint, and on every boot whichever way the flag is set: a recovery
        # session's elevated permission lasts one uptime and no longer. `spend_recovery_mark`
        # only fires when the operator actually resets the password, so one who signs in with
        # a code and changes nothing would otherwise keep it for the session's full 30 days,
        # past the restart the manual gives as the last step of getting back in.
        demoted = await clear_recovery_marks(session)
        if demoted:
            log.info("auth.recovery_marks_cleared", sessions=demoted)

        if settings.recovery:
            await mint_recovery_token(
                session,
                base_url=recovery_base_url(settings.host, settings.port),
                data_dir=settings.data_dir,
            )
            # Recovery mode is a door that opens for anyone who can reach the host, so this
            # is the last boot on which Reaper should also be able to delete media.
            # `RuntimeSafety.destructive_allowed` already holds it off for the life of this
            # process; writing the stored switch off as well is what makes the state survive
            # the restart that turns recovery back off. Otherwise an install that was armed
            # before the lockout would come back armed the moment the operator "finished",
            # with the change they never made. Re-arming is one password away, deliberately.
            # Restore does the same thing for the same reason (`restore._force_destructive_off`).
            await app_settings.set_destructive_enabled(session, enabled=False)
        else:
            # Recovery is off, so any recovery.txt left in the data folder is a spent or
            # expired code. Sweeping it here is what makes "set REAPER_RECOVERY=false and
            # restart" actually tidy up, rather than leaving a stale secret beside the
            # database for whoever looks next.
            clear_recovery_file(settings.data_dir)

        # Warn loudly if Plex OAuth is the only way in: a plex.tv outage, a
        # revoked token, or a rebuilt server would then lock the owner out of
        # their own tool.
        if await count_local_admins(session) == 0:
            log.warning(
                "auth.no_local_admin",
                # Settings first, because it is the only one of the two that exists on
                # every install: the desktop bundles ship no ``reaper-admin`` (rule 25).
                detail=(
                    "No local admin exists. If Plex sign-in fails you will be locked out. "
                    "Set an admin password in Settings, Security, or on Docker and snap "
                    "run: reaper-admin create-admin --username admin"
                ),
            )

        # The EFFECTIVE deletion permission: the stored toggle, which the env var only
        # seeds on first run. The startup banner must tell the truth about what this
        # process can do right now -- an install armed from the web UI must not log
        # "nothing can be deleted" on its next restart. (/api/health reads the same way.)
        safety = await app_settings.runtime_safety(session, settings)

        # The stored logging level wins over the environment seed, like every other
        # env-seeded switch -- and it applies to this very startup's remaining lines.
        stored_level = await app_settings.get_log_level_setting(session)
        if stored_level:
            logbuffer.set_level(stored_level)

        # The API key's digest and the trusted-proxy networks live on app.state so the
        # middleware's hot path never touches the database. Both are refreshed by their
        # settings routes on change; this is the boot-time load.
        api_key = await app_settings.get_api_key(session, box)
        app.state.api_key_digest = (
            hashlib.sha256(api_key.encode("utf-8")).digest() if api_key else None
        )
        proxy_trust = await app_settings.proxy_trust_enabled(session, settings)
        offered = await app_settings.get_trusted_proxies(session, settings) if proxy_trust else []
        app.state.trusted_proxies = parse_proxy_networks(offered) if proxy_trust else ()
        revision = await _schema_revision(session)
        integrations = await _integration_inventory(session, box, settings)
        await session.commit()

    log.info(
        "reaper.started",
        version=build_version(),
        channel="release" if is_release() else "dev",
        commit=short_commit(),
        destructive_actions_enabled=safety.destructive_allowed,
    )
    # The install fingerprint, at INFO and not DEBUG: this is the first thing support asks
    # for, and an operator should not have to turn anything on to already have it in the log
    # they are about to send. The four shapes keep their data in four places and take their
    # configuration by four different routes, so "which one is this" comes before every other
    # question -- and `log_level_from` answers the one that blocks all the others, since a
    # stored level silently outranks REAPER_LOG_LEVEL and DEBUG is how anything else is chased.
    log.info(
        "reaper.install",
        install=install_kind(),
        platform=sys.platform,
        python=python_version(),
        data_dir=str(settings.data_dir),
        log_level=logbuffer.level_name(),
        log_level_from="settings" if stored_level else "environment",
        host=settings.host,
        port=settings.port,
    )
    # Counts and flags only, never a base URL or a key: those carry credentials (rule 13).
    log.info("reaper.integrations", **integrations)
    # Offered against accepted, because `parse_proxy_networks` drops an entry it cannot parse
    # and carries on. Only the env-seeded path can hold a typo -- the settings route validates
    # and refuses -- and that is the declarative deployment with no UI to tell. Silently
    # dropping one is how a reverse-proxied install ends up keying every lockout and rate
    # limit on the proxy's own address instead of the caller's (rule 101).
    log.info(
        "reaper.proxy_trust",
        enabled=proxy_trust,
        offered=len(offered),
        accepted=len(app.state.trusted_proxies),
    )
    # The cache read is caught and reaper.db's is not, deliberately. `cache.db` is disposable
    # by contract (`create_cache_engine`) and is rebuilt by the next sync, so a cache file that
    # cannot be opened -- truncated by an unclean shutdown, or owned by another uid after a
    # container's user changed -- must not stop the app from starting. This line is the first
    # thing in the boot to open it, so without the catch a diagnostic would be the one thing
    # standing between the operator and a UI they could have deleted the file from.
    try:
        cache_mode = await journal_mode(cache_engine)
    except SQLAlchemyError:
        cache_mode = "unreadable"
        log.warning(
            "db.cache_unreadable",
            detail=(
                "Reaper cannot read its cache database. Nothing is lost: stop Reaper, delete "
                "cache.db from the data folder, and start it again to rebuild it."
            ),
        )
    log.info(
        "db.ready",
        revision=revision,
        journal_mode=await journal_mode(engine),
        cache_journal_mode=cache_mode,
    )
    if safety.destructive_allowed:
        # A warning, deliberately: this is the one line an operator whose .env still says
        # disabled will look for when the toggle was turned on in the web UI.
        log.warning(
            "reaper.armed",
            detail="Deletion is turned on. Reaper can remove media through Sonarr and Radarr.",
        )
    else:
        log.info(
            "reaper.safe_mode",
            detail="Destructive actions are disabled. Nothing can be deleted.",
        )

    # Background maintenance. Refreshes only -- it never deletes. The startup catch-up
    # runs in a task rather than inline so a first-boot 280 MB dataset download does not
    # block the app from serving; the first scan degrades until it lands, which is the
    # correct, loud behavior.
    # The server time zone every timed job runs on: the stored setting, else the
    # REAPER_TIMEZONE seed, else the host's own zone (see app_settings.get_timezone). A cron
    # set for 2 AM fires at 2 AM here; without a pinned zone APScheduler would use the
    # container's own, which is UTC in most images. get_timezone only ever returns a
    # validated IANA name, so ZoneInfo cannot raise on it.
    async with factory() as session:
        scheduler_tz = ZoneInfo(await app_settings.get_timezone(session, settings))
        scan_cron = await app_settings.get_scan_schedule(session)
        maintenance_schedules = await app_settings.get_maintenance_schedules(session)
    # The zone every timed job resolved to, whichever of the three layers won. A cron set
    # for 2 AM firing at the wrong hour is the symptom; this is the cause, and a typo'd
    # REAPER_TIMEZONE otherwise falls through to the host zone with no trace.
    log.info("scheduler.timezone", timezone=str(scheduler_tz))

    scheduler = build_scheduler(
        cache_engine,
        settings.data_dir,
        session_factory=factory,
        secret_box=box,
        settings=settings,
        # The app's own checker, so the nightly check and the About route share one cache.
        update_checker=app.state.update_checker,
        timezone=scheduler_tz,
        # A reap's live flag lives on app state, which the scheduler has no handle on. Passed
        # as a predicate rather than read at wiring time so the snapshot sweep asks the
        # question when it is about to take the write lock, not twelve hours earlier (#325).
        reap_running=lambda: reap_in_flight(app),
    )
    # Track which jobs are executing before the scheduler starts, so the very first firing
    # is seen. The Jobs page reads this to show an honest "running now" per job.
    app.state.running_jobs = track_running_jobs(scheduler)
    scheduler.start()
    app.state.scheduler = scheduler

    # Restore what the owner stored: the automatic-scan cron if they set one, and any upkeep
    # job they moved off its built-in default or turned off. A scan is read-only, so it is the
    # one scheduled job that produces new review candidates; the rest is cache upkeep.
    #
    # The same call the timezone save makes, which is the point -- this used to be the same two
    # ladders written out again here, and a guard that boot survives is only worth anything if
    # the runtime replay of the same data survives it too (rule 87). A stored-but-malformed
    # cron is logged and skipped in there rather than crashing startup.
    apply_stored_schedules(
        scheduler,
        scheduler_tz,
        settings=settings,
        session_factory=factory,
        cache_engine=cache_engine,
        secret_box=box,
        update_checker=app.state.update_checker,
        data_dir=settings.data_dir,
        scan_cron=scan_cron,
        maintenance=maintenance_schedules,
    )

    # Every registered job with its next firing, after the stored schedules have been
    # applied so this is the table that will actually run. Without it "why did my nightly
    # scan stop" has no answer: a job that was never scheduled and one whose stored cron was
    # skipped as malformed look identical, and the resolved zone is what a cron firing at
    # the wrong hour turns on. One line per job, about six.
    for job in scheduler.get_jobs():
        log.info(
            "scheduler.job",
            job=job.id,
            trigger=str(job.trigger),
            next_run=job.next_run_time.isoformat() if job.next_run_time else None,
        )
    if not scan_cron:
        log.info(
            "scheduler.no_scan_scheduled",
            detail="No automatic scan is scheduled. Reaper only scans when you ask it to.",
        )

    catch_up = asyncio.create_task(catch_up_on_startup(cache_engine, settings.data_dir))
    catch_up.add_done_callback(_report_background_failure)

    try:
        yield
    finally:
        catch_up.cancel()
        # A background scan (api/scan.py) is detached from any request, so cancel it here
        # rather than leaving a pending task when the loop stops. A scan writes only our own
        # rows and can be dropped, so it is canceled but not awaited.
        scan_task = getattr(app.state, "scan_task", None)
        if scan_task is not None and not scan_task.done():
            scan_task.cancel()
        # A reap (api/runs.py) is detached too, but it is DELETING -- so it must be canceled
        # AND awaited before the engines go, so the executor's CancelledError branch marks the
        # run ABORTED and its finally commits that state and tidies Plex against the still-live
        # DB and clients. Bounded, so a slow Plex cannot hang shutdown forever; if it exceeds
        # the bound the second cancel (from wait_for) interrupts the cleanup and we proceed.
        reap_task = getattr(app.state, "reap_task", None)
        if reap_task is not None and not reap_task.done():
            reap_task.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(reap_task, timeout=20)
        scheduler.shutdown(wait=False)
        # The artwork proxy keeps one Tautulli client alive across requests (api/poster.py).
        # It is built lazily on the first poster request, so it may never exist; closing it
        # here is what gives it an owner (rule 34).
        await close_artwork_client(app)
        await engine.dispose()
        await cache_engine.dispose()
        log.info("reaper.stopped")


#: Said on every operation an API key cannot reach. Short because it repeats about forty
#: times, and first in the description because it is the sentence that stops a script
#: being written against a route that will refuse it.
_SESSION_ONLY_NOTE = "**Signed in only.** An API key cannot make this call."

#: What ``paths`` holds besides operations (``parameters``, ``summary``, a ``$ref``). None
#: are emitted today, so this is the guard on a future one being read as a verb.
_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def _mark_credentials(schema: dict[str, Any]) -> None:
    """Give every operation the credential that actually reaches it.

    Three answers, and rule 7/24 governs all of them: each is a predicate in
    ``api/middleware.py``, evaluated for that exact method and path, never a list kept
    beside the fence.

    * **Refused a key** (``api_key_refused``) narrows to ``Session``, which is strictly
      narrower than the document-wide default, so this can only ever ask for *more* than
      the auth box offers. Both halves of that mark matter: ``security`` is the
      machine-readable half a generated client and the try-it-out panel read, and the
      sentence is the half a person reads. It goes first in the description because a
      security requirement is quiet and the 403 it predicts is not.
    * **Asks for nothing** (``no_credential_needed``) gets ``security: []``: the health
      probe and the sign-in endpoints, which have to answer before anyone is signed in.
    * **Everything else** is left inheriting either credential, because either works.

    The middle answer was once the whole open set, left inheriting on the argument that
    ``security: []`` would trade one wrong claim for another, since several open routes
    refuse anonymously for their own reasons. That was true of exactly one of them, and
    leaving all eight alone published a credential requirement on the seven that have
    none and offered the key on the one that refuses it. The exception is now declared
    where the guard is (``_SIGNED_IN_ONLY_READS``) and read by both predicates, so the
    document states what was measured on all three.
    """
    for path, operations in schema.get("paths", {}).items():
        for method, operation in operations.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            if api_key_refused(method.upper(), path):
                operation["security"] = [{"Session": []}]
                existing = operation.get("description", "").strip()
                operation["description"] = (
                    f"{_SESSION_ONLY_NOTE}\n\n{existing}" if existing else _SESSION_ONLY_NOTE
                )
            elif no_credential_needed(path):
                operation["security"] = []


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        level=settings.log_level, json_logs=settings.log_json, data_dir=settings.data_dir
    )

    app = FastAPI(
        title="Reaper",
        version=__version__,
        description="Grave decisions, clearly explained.",
        lifespan=lifespan,
        # The stock /docs, /redoc and /openapi.json sit OUTSIDE /api, which the
        # AuthGuard lets straight through -- so the defaults would publish the whole
        # API description to anyone who can reach the port. They are disabled here and
        # served signed-in-only at /api/docs and /api/openapi.json below instead.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    # One instance for the app's lifetime, so its hours-long answer cache is shared across
    # requests -- and with the nightly `check_for_updates` job, which is handed this same
    # instance so its ask leaves the answer ready for the next page load. Lazy: nothing is
    # fetched until the job fires or a surface asks.
    app.state.update_checker = UpdateChecker()

    @app.exception_handler(RequestValidationError)
    async def _validation_reason(_: Request, exc: RequestValidationError) -> JSONResponse:
        """Strip pydantic's ``Value error,`` prefix off a wire-schema refusal.

        The SPA renders ``detail[].msg`` verbatim (``api.ts``'s ``reason``), so a domain
        refusal raised in a schema validator reached the operator as "Value error, There is
        no ... protection to switch on" -- internal vocabulary in front of a plain sentence,
        which rule 21 does not allow. ``routes._to_body`` already strips exactly this for
        refusals raised inside a route; this is the same removal for the ones FastAPI
        handles before the route body ever runs, so both paths read alike.

        Shape is otherwise FastAPI's own, so nothing downstream has to change.
        """
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {
                        "loc": [str(part) for part in error.get("loc", ())],
                        "msg": str(error.get("msg", "")).removeprefix("Value error, "),
                        "type": error.get("type", ""),
                    }
                    for error in exc.errors()
                ]
            },
        )

    @app.get("/api/health", tags=[api_tags.SETUP])
    async def health() -> HealthResponse:
        # An UNAUTHENTICATED liveness probe (the container HEALTHCHECK hits it), so it
        # tells an anonymous caller nothing: no armed state, no safety note, no exact
        # version. The safety banner reads the authenticated /api/settings/safety.
        return HealthResponse(status="ok")

    def openapi_with_api_key() -> dict[str, Any]:
        """The stock schema plus the two security schemes, marked per operation, and the
        section list that keeps the whole API from rendering as one flat scroll.

        **Which credential reaches which route is part of the reference, not a footnote.**
        The scheme alone was applied globally, so the auth box rendered over all 87
        operations and try-it-out looked available on every one of them -- while the fence
        in ``api/middleware.py`` refuses a key on most. Each operation therefore carries
        the credential that actually reaches it, from ``api_key_refused``, the same two
        predicates the guard itself runs: nothing here restates the fence, so nothing here
        can fall behind it. A session-only operation also says so in words, because a
        security requirement is a quiet signal and a 403 nobody predicted is a loud one.

        **The order of the two schemes is load-bearing.** Scalar preselects
        the FIRST one an operation accepts and sends it, so with ``ApiKey`` first every
        try-it-out went out carrying an ``X-Api-Key`` header holding the unfilled scheme's
        placeholder, which is the empty string -- and the guard judges a presented key on
        the key alone, never falling back to the cookie, so the reference answered its own
        requests "That API key is not valid." With ``Session`` first the preselected
        credential is a cookie, which no page script can set (``Cookie`` is a forbidden
        fetch header, so the browser drops it), and the real session rides along instead.
        Measured against the shipped bundle: reading the last snapshot answered 401 under
        one order and authenticated under the other, and the key stays selectable in the
        same dropdown.

        **The guard wants a third thing, and the page now supplies it.** Every unsafe
        method off the key lane needs ``X-Reaper-CSRF: 1``, which a cookie cannot carry on
        its own, so the panel's own three headers (``accept``, ``content-type``,
        ``cookie``) met a 403 on all 47 writes. ``api_docs`` sets it from the reference's
        ``onBeforeRequest`` hook, which is handed the mutable builder the outgoing request
        is built FROM -- the hook's ``request`` argument is a throwaway built from the
        payload, so mutating that one changes nothing, and ``requestBuilder`` is the live
        object (``@scalar/api-reference``'s ``map-config-plugins``). The sibling
        ``onRequestBuilt`` hook would also have worked, on the real ``Request`` rather than
        the builder; this one is chosen because the builder is the documented place to add
        a header, not because the ``Request`` is closed to it.
        Measured against the shipped bundle: ``POST /api/policy/validate`` from try-it-out
        went 403 CSRF before and reaches the handler after, with ``x-reaper-csrf: 1`` on
        the wire beside the cookie. The ``Session`` description now says the button writes,
        and still names the header for the script author writing their own client.

        **This widens what the PAGE may do. It does not widen what the CREDENTIAL may
        do.** Before the hook the reference could send none of the document's 47 writes;
        now it sends all of them, as the operator, and 15 succeed on the first Send with
        the body Scalar prefills from the schema example -- including rotating the API key
        under every script pointed at it, and dropping the stored Plex server. None of
        that is reach the session lacked, since the SPA holds the same cookie; what
        changes is that the SPA spends it behind its own confirmations and this page
        spends it on one click. Say it plainly wherever the link is handed out rather than
        rounding it off here: the "API reference" row in ``Settings.tsx`` is the copy that
        has to carry it, and this paragraph existing is not a substitute for that one.

        The header itself is not a credential. It proves same-origin, which this page is,
        being served by Reaper at ``/api/docs`` behind the same session, and any
        same-origin page could always set it; nothing server-side changed.

        **A stray Send still cannot delete anything, and here is what stops it.**
        ``POST /api/runs/{run_id}/execute`` reaches ``api/runs.py``'s ``execute_run``,
        which refuses unless ``app_settings.runtime_safety`` reports
        ``destructive_allowed``, then recomputes the content-bound phrase with
        ``planner.confirmation_phrase`` and compares it server-side before anything is
        sent. Arming is itself password-gated in the request body
        (``api/settings.py``'s ``set_safety``), so the prefilled empty password is
        refused. Driven in the reference-page request shape: unarmed 403, armed with the
        prefilled empty phrase 409, armed with the correct phrase stopped at the
        executor's own preflight.

        ``tags`` names and orders the sections; ``x-tagGroups`` is the vendor extension
        Scalar reads to nest them under three headings. Both come from
        ``api.tags.GROUPS``, the one place a section is declared.

        A route carrying a tag that is not in that list **vanishes from the reference
        entirely** -- once ``x-tagGroups`` is present Scalar builds the sidebar from the
        group members alone, so an ungrouped tag emits no navigation entry and its
        operations are reachable only by someone who already knows the path. Measured
        against the shipped bundle by retagging one route: every other operation
        navigable, the retagged one absent, and no stray heading.
        ``tests/test_openapi_tags.py`` refuses it for that reason, not for tidiness.
        """
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                description=app.description,
                routes=app.routes,
                tags=api_tags.openapi_tags(),
            )
            schema["x-tagGroups"] = api_tags.openapi_tag_groups()
            components = schema.setdefault("components", {})
            components.setdefault("securitySchemes", {})["ApiKey"] = {
                "type": "apiKey",
                "in": "header",
                "name": "X-Api-Key",
                # Built from the fence in api/middleware.py, never written beside it: the
                # box that offers the key on every operation in the document has to say
                # which ones it will actually get through. Neither sentence carries the
                # operation count any more -- both said 87 against a real 96, drifting once
                # per route added and reading as measured the whole time (rule 144). The
                # count was load-bearing in neither, so it is gone rather than gated.
                "description": api_key_scope_description(),
            }
            components["securitySchemes"]["Session"] = {
                "type": "apiKey",
                "in": "cookie",
                "name": DOCUMENTED_SESSION_COOKIE,
                # Still names the CSRF header, for the reason it always did: this is the
                # only place the document mentions it, and a script author writing their
                # OWN client needs it before their first write rather than after the 403
                # it causes. What changed is that the button no longer needs telling --
                # the reference page sets it (``api_docs``), so the sentence describes
                # the header as something a script sends rather than something missing.
                #
                # Header and value interpolated from the guard's own constants, so the
                # sentence a script author copies cannot outlive what _csrf_ok accepts.
                "description": (
                    "The sign-in cookie your browser already holds. Nothing to paste: it "
                    "rides along on its own, so the try-it-out button reads and writes as "
                    "you while you are signed in. Your own scripts need one more thing: "
                    f"the header {CSRF_HEADER}: {CSRF_VALUE} on every write. An HTTPS "
                    f"install names the same cookie __Host-{DOCUMENTED_SESSION_COOKIE}."
                ),
            }
            # Either credential, unless an operation below narrows it. Session leads for
            # the try-it-out reason in the docstring, not as a statement of preference.
            schema["security"] = [{"Session": []}, {"ApiKey": []}]
            _mark_credentials(schema)
            app.openapi_schema = schema
        return app.openapi_schema

    # The one producer of the schema, so there is no second way to build it. Both this and
    # the stock ``FastAPI.openapi`` cache into ``app.openapi_schema`` and short-circuit on
    # it, so whichever runs first wins for the life of the process: one stock call before
    # the first request would serve the reference a document with no ``tags``, no
    # ``x-tagGroups`` and no ``ApiKey`` scheme -- a flat scroll under an auth box that does
    # not authenticate -- and nothing would raise. No such caller exists today (``docs_url``,
    # ``redoc_url`` and ``openapi_url`` are all ``None`` above, so FastAPI registers no route
    # that calls it), which is exactly why the trap is worth closing here rather than in
    # whichever change first adds one.
    app.openapi = openapi_with_api_key  # type: ignore[method-assign]

    @app.get("/api/openapi.json", include_in_schema=False)
    async def openapi_json() -> JSONResponse:
        # Inside /api, so the AuthGuard fronts it: a session cookie or a valid API key.
        return JSONResponse(openapi_with_api_key())

    @app.get("/api/docs", include_in_schema=False)
    async def api_docs() -> HTMLResponse:
        # The API reference, rendered by a Scalar bundle the frontend build vendors to
        # /vendor/scalar.js -- shipped in the container, no CDN, works offline. Signed-in
        # only, exactly like the schema it renders.
        return HTMLResponse(
            "<!doctype html>\n"
            "<html>\n"
            "<head>\n"
            '  <meta charset="utf-8" />\n'
            "  <title>Reaper API</title>\n"
            '  <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
            "</head>\n"
            "<body>\n"
            '  <div id="app"></div>\n'
            '  <script src="/vendor/scalar.js"></script>\n'
            "  <script>\n"
            "    Scalar.createApiReference('#app', {\n"
            "      url: '/api/openapi.json',\n"
            "      hideClientButton: true,\n"
            "      // The one thing the cookie cannot carry on its own. Set on the builder\n"
            "      // before the Request is frozen, so nothing has to rebuild a body.\n"
            "      onBeforeRequest: function (event) {\n"
            f"        event.requestBuilder.headers.set('{CSRF_HEADER}', '{CSRF_VALUE}');\n"
            "      },\n"
            "    });\n"
            "  </script>\n"
            "</body>\n"
            "</html>\n"
        )

    app.include_router(auth_router)
    app.include_router(setup_router)
    app.include_router(settings_router)
    # Beside its parent, which is where a reader looks for it. Not an ordering
    # constraint: `paths` insertion order moves whatever the include order, and
    # nothing reads it.
    app.include_router(plex_router)
    app.include_router(backup_router)
    app.include_router(router)
    app.include_router(scan_router)
    app.include_router(poster_router)
    app.include_router(runs_router)
    app.include_router(profile_router)
    app.include_router(whitelist_router)
    app.include_router(fairness_router)
    app.include_router(breakdown_router)
    app.include_router(plex_trash_router)
    app.include_router(leaving_soon_router)
    app.include_router(lists_router)
    app.include_router(logs_router)

    # The gate. Every /api route above requires a session and passes a CSRF check,
    # except the health probe and /api/auth (you cannot sign in if signing in needs
    # a session). Added last so it wraps the whole app -- including the SPA, which
    # it lets straight through. See reaper.api.middleware.
    app.add_middleware(AuthGuard)

    # The built SPA, served as low-priority routes: every /api route above is matched
    # first, and only then does the frontend get a look. Missing paths fall back to
    # index.html so client-side routing survives a refresh or a bookmarked deep link.
    # This is what makes the shipped container one service on one port.
    #
    # Off in development (REAPER_SERVE_SPA=false, set in .claude/launch.json), because
    # Vite serves the UI on its own port and proxies /api here; mounting dist too would
    # leave a stale second copy of the UI on this one. A missing dist in *production* is
    # a broken image, and should not be papered over by silently serving 404s from a
    # directory that was never built -- so that case still warns rather than passing.
    # In a packaged install (frozen bundle, snap) the install root stands in for the
    # repo root: the built SPA travels inside it, with no src/ level above the package.
    root = install_root() or Path(__file__).resolve().parent.parent.parent
    dist = root / "frontend" / "dist"
    if not settings.serve_spa:
        log.info(
            "frontend.not_served",
            detail="REAPER_SERVE_SPA is off. This process serves the API only; "
            "the Vite dev server serves the UI.",
        )
    elif dist.is_dir():
        app.frontend("/", directory=dist)
    else:
        log.info(
            "frontend.not_built",
            detail=f"No SPA at {dist}. The API is up; run the Vite dev server for the UI.",
        )

    return app
