# SPDX-License-Identifier: AGPL-3.0-or-later
"""Application factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from pydantic import BaseModel

from reaper import __version__
from reaper.api.auth import router as auth_router
from reaper.api.fairness import router as fairness_router
from reaper.api.grace import router as grace_router
from reaper.api.leaving_soon import router as leaving_soon_router
from reaper.api.middleware import AuthGuard
from reaper.api.poster import router as poster_router
from reaper.api.routes import router
from reaper.api.runs import router as runs_router
from reaper.api.scan import router as scan_router
from reaper.api.settings import router as settings_router
from reaper.api.setup import router as setup_router
from reaper.api.whitelist import router as whitelist_router
from reaper.auth.admins import count_local_admins
from reaper.auth.recovery import mint_recovery_token
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
from reaper.services.scheduler import apply_scan_schedule, build_scheduler, catch_up_on_startup
from reaper.services.seeding import seed_instances

log = structlog.get_logger(__name__)


class HealthResponse(BaseModel):
    """Note: route return types must be resolvable at runtime.

    ``from __future__ import annotations`` turns them into strings, and FastAPI
    builds a response model by resolving them -- so a type imported only under
    ``TYPE_CHECKING`` yields a 500 at request time, not an error at import time.
    """

    status: str


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
            await mint_recovery_token(session, base_url=f"http://{settings.host}:{settings.port}")

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
        await session.commit()

    log.info(
        "reaper.started",
        version=__version__,
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
    # correct, loud behaviour, not a broken one.
    scheduler = build_scheduler(
        cache_engine, settings.data_dir, session_factory=factory, secret_box=box
    )
    scheduler.start()
    app.state.scheduler = scheduler

    # Restore the owner's automatic-scan schedule, if they set one. A scan is read-only,
    # so this is the one scheduled job that produces new review candidates; the rest is
    # cache upkeep. A stored-but-malformed cron is logged and skipped rather than crashing
    # startup.
    async with factory() as session:
        scan_cron = await app_settings.get_scan_schedule(session)
    if scan_cron:
        try:
            apply_scan_schedule(
                scheduler,
                scan_cron,
                settings=settings,
                session_factory=factory,
                cache_engine=cache_engine,
                secret_box=box,
            )
        except ValueError:
            log.warning("scheduler.bad_scan_cron", cron=scan_cron)

    catch_up = asyncio.create_task(catch_up_on_startup(cache_engine, settings.data_dir))

    try:
        yield
    finally:
        catch_up.cancel()
        # A background scan (api/scan.py) is detached from any request, so cancel it here
        # rather than leaving a pending task when the loop stops.
        scan_task = getattr(app.state, "scan_task", None)
        if scan_task is not None and not scan_task.done():
            scan_task.cancel()
        scheduler.shutdown(wait=False)
        await engine.dispose()
        await cache_engine.dispose()
        log.info("reaper.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)

    app = FastAPI(
        title="Reaper",
        version=__version__,
        description="Explainable media library pruning for Plex.",
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.get("/api/health")
    async def health() -> HealthResponse:
        # An UNAUTHENTICATED liveness probe (the container HEALTHCHECK hits it), so it
        # tells an anonymous caller nothing: no armed state, no safety note, no exact
        # version. The safety banner reads the authenticated /api/settings/safety.
        return HealthResponse(status="ok")

    app.include_router(auth_router)
    app.include_router(setup_router)
    app.include_router(settings_router)
    app.include_router(router)
    app.include_router(scan_router)
    app.include_router(poster_router)
    app.include_router(runs_router)
    app.include_router(whitelist_router)
    app.include_router(fairness_router)
    app.include_router(grace_router)
    app.include_router(leaving_soon_router)

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
