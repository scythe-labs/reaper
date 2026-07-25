# SPDX-License-Identifier: AGPL-3.0-or-later
"""Application factory."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from reaper import __version__, logbuffer
from reaper.api.auth import router as auth_router
from reaper.api.backup import router as backup_router
from reaper.api.breakdown import router as breakdown_router
from reaper.api.fairness import router as fairness_router
from reaper.api.leaving_soon import router as leaving_soon_router
from reaper.api.logs import router as logs_router
from reaper.api.middleware import AuthGuard
from reaper.api.plex_trash import router as plex_trash_router
from reaper.api.poster import close_artwork_client
from reaper.api.poster import router as poster_router
from reaper.api.routes import router
from reaper.api.runs import router as runs_router
from reaper.api.scan import router as scan_router
from reaper.api.settings import router as settings_router
from reaper.api.setup import router as setup_router
from reaper.api.whitelist import router as whitelist_router
from reaper.auth.admins import count_local_admins
from reaper.auth.proxy import parse_proxy_networks
from reaper.auth.recovery import mint_recovery_token, recovery_base_url
from reaper.buildinfo import build_version
from reaper.config import (
    Settings,
    get_settings,
    load_raw_env,
    parse_instance_seeds,
)
from reaper.crypto import SecretBox
from reaper.db.session import create_cache_engine, create_engine, create_session_factory
from reaper.logging import configure_logging
from reaper.secrets import resolve_kdf_salt, resolve_old_keys, resolve_secret_key
from reaper.services import app_settings
from reaper.services.scheduler import (
    apply_maintenance_schedule,
    apply_scan_schedule,
    build_scheduler,
    catch_up_on_startup,
    track_running_jobs,
)
from reaper.services.seeding import seed_instances

log = structlog.get_logger(__name__)


class HealthResponse(BaseModel):
    """Note: route return types must be resolvable at runtime.

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

        if settings.recovery:
            await mint_recovery_token(
                session, base_url=recovery_base_url(settings.host, settings.port)
            )

        # Warn loudly if Plex OAuth is the only way in: a plex.tv outage, a
        # revoked token, or a rebuilt server would then lock the owner out of
        # their own tool.
        if await count_local_admins(session) == 0:
            log.warning(
                "auth.no_local_admin",
                detail=(
                    "No local admin exists. If Plex sign-in fails you will be locked out. "
                    "Create a fallback with: reaper-admin create-admin --username <name>"
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
        if await app_settings.proxy_trust_enabled(session, settings):
            app.state.trusted_proxies = parse_proxy_networks(
                await app_settings.get_trusted_proxies(session, settings)
            )
        else:
            app.state.trusted_proxies = ()
        await session.commit()

    log.info(
        "reaper.started",
        version=build_version(),
        destructive_actions_enabled=safety.destructive_allowed,
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
    # correct, loud behavior, not a broken one.
    # The server time zone every timed job runs on: the stored setting, else the
    # REAPER_TIMEZONE seed, else the host's own zone (see app_settings.get_timezone). A cron
    # set for 2 AM fires at 2 AM here; without a pinned zone APScheduler would use the
    # container's own, which is UTC in most images. get_timezone only ever returns a
    # validated IANA name, so ZoneInfo cannot raise on it.
    async with factory() as session:
        scheduler_tz = ZoneInfo(await app_settings.get_timezone(session, settings))
        scan_cron = await app_settings.get_scan_schedule(session)
        maintenance_schedules = await app_settings.get_maintenance_schedules(session)

    scheduler = build_scheduler(
        cache_engine,
        settings.data_dir,
        session_factory=factory,
        secret_box=box,
        timezone=scheduler_tz,
    )
    # Track which jobs are executing before the scheduler starts, so the very first firing
    # is seen. The Jobs page reads this to show an honest "running now" per job.
    app.state.running_jobs = track_running_jobs(scheduler)
    scheduler.start()
    app.state.scheduler = scheduler

    # Restore the owner's automatic-scan schedule, if they set one. A scan is read-only,
    # so this is the one scheduled job that produces new review candidates; the rest is
    # cache upkeep. A stored-but-malformed cron is logged and skipped rather than crashing
    # startup.
    if scan_cron:
        try:
            apply_scan_schedule(
                scheduler,
                scan_cron,
                settings=settings,
                session_factory=factory,
                cache_engine=cache_engine,
                secret_box=box,
                timezone=scheduler_tz,
            )
        except ValueError:
            log.warning("scheduler.bad_scan_cron", cron=scan_cron)

    # Restore any upkeep-job schedule the owner changed from the built-in default -- a new
    # cron, or off entirely. build_scheduler already wired the defaults; this overrides only
    # the jobs with a stored value. A malformed stored cron leaves the default in place.
    for job_id, cron in maintenance_schedules.items():
        try:
            apply_maintenance_schedule(
                scheduler,
                job_id,
                cron,
                cache_engine=cache_engine,
                data_dir=settings.data_dir,
                session_factory=factory,
                secret_box=box,
                timezone=scheduler_tz,
            )
        except (ValueError, KeyError):
            log.warning("scheduler.bad_maintenance_cron", job=job_id, cron=cron)

    catch_up = asyncio.create_task(catch_up_on_startup(cache_engine, settings.data_dir))
    catch_up.add_done_callback(_report_background_failure)

    try:
        yield
    finally:
        catch_up.cancel()
        # A background scan (api/scan.py) is detached from any request, so cancel it here
        # rather than leaving a pending task when the loop stops. A scan writes only our own
        # rows and can be dropped, so it is cancelled but not awaited.
        scan_task = getattr(app.state, "scan_task", None)
        if scan_task is not None and not scan_task.done():
            scan_task.cancel()
        # A reap (api/runs.py) is detached too, but it is DELETING -- so it must be cancelled
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        level=settings.log_level, json_logs=settings.log_json, data_dir=settings.data_dir
    )

    app = FastAPI(
        title="Reaper",
        version=__version__,
        description="Explainable media library pruning for Plex.",
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

    @app.get("/api/health")
    async def health() -> HealthResponse:
        # An UNAUTHENTICATED liveness probe (the container HEALTHCHECK hits it), so it
        # tells an anonymous caller nothing: no armed state, no safety note, no exact
        # version. The safety banner reads the authenticated /api/settings/safety.
        return HealthResponse(status="ok")

    def openapi_with_api_key() -> dict[str, Any]:
        """The stock schema plus the ``X-Api-Key`` security scheme, so the reference
        UI offers an auth box whose try-it-out requests actually authenticate."""
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                description=app.description,
                routes=app.routes,
            )
            components = schema.setdefault("components", {})
            components.setdefault("securitySchemes", {})["ApiKey"] = {
                "type": "apiKey",
                "in": "header",
                "name": "X-Api-Key",
                "description": (
                    "The instance API key from Settings, General. Reads and writes as "
                    "the operator, but can never turn deletion on, execute a reap, or "
                    "change sign-in settings."
                ),
            }
            schema["security"] = [{"ApiKey": []}]
            app.openapi_schema = schema
        return app.openapi_schema

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
            "    });\n"
            "  </script>\n"
            "</body>\n"
            "</html>\n"
        )

    app.include_router(auth_router)
    app.include_router(setup_router)
    app.include_router(settings_router)
    app.include_router(backup_router)
    app.include_router(router)
    app.include_router(scan_router)
    app.include_router(poster_router)
    app.include_router(runs_router)
    app.include_router(whitelist_router)
    app.include_router(fairness_router)
    app.include_router(breakdown_router)
    app.include_router(plex_trash_router)
    app.include_router(leaving_soon_router)
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
    dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
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
