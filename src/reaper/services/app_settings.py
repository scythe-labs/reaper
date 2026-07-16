# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small singleton settings the admin edits in the web UI.

These live in the ``app_setting`` key/value table, distinct from ``Settings`` (the
environment/bootstrap concerns that cannot live in the database they protect). Two
things belong here so far:

* **whether deletion is enabled** -- the one destructive-action switch. It is turned on
  from the web UI, but the route that flips it on first checks the admin password (see
  ``api.settings``); turning it off needs no password. The environment variable only seeds
  the first-run default; after that this stored value wins.
* **the scan schedule** -- an optional cron for an automatic, read-only scan. A scan
  never deletes, so scheduling one is safe; it just keeps the review queue fresh.

Every value is stored as JSON so the column stays one shape whatever the type.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clock import utcnow
from reaper.config import RuntimeSafety, Settings
from reaper.crypto import SecretBox
from reaper.db.models import AppSetting

DESTRUCTIVE_KEY = "destructive_enabled"
SCAN_SCHEDULE_KEY = "scan_schedule"
#: The Discord webhook, stored Fernet-encrypted exactly like an instance API key -- its
#: token lives in the URL path, so the whole URL is a credential.
DISCORD_WEBHOOK_KEY = "discord_webhook_enc"
#: The set of rating keys already announced as "leaving soon". Persisted so the heads-up is
#: idempotent across repeated syncs even when the Plex label write never lands (preview /
#: unarmed) -- see :func:`reaper.services.leaving_soon.sync`.
LEAVING_SOON_ANNOUNCED_KEY = "leaving_soon_announced"
#: Where "open in Plex" links send the admin. A plain URL, not a secret -- most installs
#: keep the hosted Plex Web default; a self-hosted Plex Web front-end overrides it.
PLEX_WEB_URL_KEY = "plex_web_url"

DEFAULT_PLEX_WEB_URL = "https://app.plex.tv"


async def _get(session: AsyncSession, key: str, default: Any) -> Any:
    row = await session.get(AppSetting, key)
    if row is None:
        return default
    return json.loads(row.value_json)


async def _set(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(AppSetting, key)
    payload = json.dumps(value)
    if row is None:
        session.add(AppSetting(key=key, value_json=payload, updated_at=utcnow()))
    else:
        row.value_json = payload
        row.updated_at = utcnow()
    await session.flush()


# --- deletion enabled ------------------------------------------------------


async def destructive_enabled(session: AsyncSession, settings: Settings) -> bool:
    """Whether deletion is currently on.

    The stored value wins. If nothing has been stored yet (a fresh install), fall back to
    the environment's first-run default -- so a declarative deployment can ship armed while
    a normal install starts read-only until someone turns it on.
    """
    stored = await _get(session, DESTRUCTIVE_KEY, default=None)
    if stored is None:
        return settings.destructive_actions_enabled
    return bool(stored)


async def set_destructive_enabled(session: AsyncSession, *, enabled: bool) -> None:
    """Turn deletion on or off. The API verifies the admin password before turning it ON;
    turning it off is always allowed, because making Reaper safer is never gated."""
    await _set(session, DESTRUCTIVE_KEY, bool(enabled))


async def runtime_safety(session: AsyncSession, settings: Settings) -> RuntimeSafety:
    """The current deletion permission. The one place that assembles it, so every caller
    agrees on whether Reaper may delete right now."""
    return RuntimeSafety(
        destructive_enabled=await destructive_enabled(session, settings),
        allow_leaving_soon_unarmed=settings.allow_unarmed_leaving_soon,
    )


# --- Plex web address ------------------------------------------------------


async def get_plex_web_url(session: AsyncSession) -> str:
    """Where "open in Plex" links point. Defaults to the hosted Plex Web app."""
    value = await _get(session, PLEX_WEB_URL_KEY, default=None)
    return str(value) if value else DEFAULT_PLEX_WEB_URL


async def set_plex_web_url(session: AsyncSession, url: str | None) -> None:
    """Store the Plex web address. ``None`` or empty resets to the hosted default."""
    cleaned = (url or "").strip().rstrip("/")
    await _set(session, PLEX_WEB_URL_KEY, cleaned or None)


# --- scan schedule ---------------------------------------------------------


async def get_scan_schedule(session: AsyncSession) -> str | None:
    """The cron expression for the automatic read-only scan, or None if disabled.

    ``None`` (the default) means no automatic scan -- the owner runs scans by hand.
    A stored value is a 5-field cron string (minute hour day month day-of-week).
    """
    value = await _get(session, SCAN_SCHEDULE_KEY, default=None)
    return str(value) if value else None


async def set_scan_schedule(session: AsyncSession, cron: str | None) -> None:
    await _set(session, SCAN_SCHEDULE_KEY, cron or None)


# --- Discord webhook -------------------------------------------------------


async def get_discord_webhook(
    session: AsyncSession, box: SecretBox, settings: Settings
) -> str | None:
    """The stored Discord webhook URL, decrypted, or ``None`` when notifications are off.

    The stored value wins. On a fresh install with ``REAPER_DISCORD_WEBHOOK`` set in the
    environment, that env value is a *first-boot seed*: it is imported into the database
    (encrypted) once, on first read, and the database is the source of truth thereafter --
    so removing the env var later does not silently turn notifications off, and a webhook
    edited in the UI is never clobbered by a stale env value. This mirrors how instance API
    keys are seeded (see ``reaper.services.seeding``).

    A stored value that will not decrypt (the secret key changed) is treated as *absent*
    rather than raised: a broken notification credential must never break a scan, a plan, or
    a run -- it can be re-entered in the UI.
    """
    stored = await _get(session, DISCORD_WEBHOOK_KEY, default=None)
    if stored is not None:
        try:
            return box.decrypt(str(stored))
        except ValueError:
            return None
    seed = settings.discord_webhook
    if seed is None:
        return None
    url = seed.get_secret_value().strip()
    if not url:
        return None
    # Seed-once: persist under the current key so it survives the env var going away. The
    # caller commits; if it does not, the seed is simply re-derived on the next read.
    await _set(session, DISCORD_WEBHOOK_KEY, box.encrypt(url))
    return url


async def set_discord_webhook(session: AsyncSession, box: SecretBox, url: str) -> None:
    """Store (or replace) the webhook, encrypted. The URL is validated at the API edge."""
    await _set(session, DISCORD_WEBHOOK_KEY, box.encrypt(url))


async def clear_discord_webhook(session: AsyncSession) -> None:
    """Forget the webhook -- notifications go silent until one is set again."""
    row = await session.get(AppSetting, DISCORD_WEBHOOK_KEY)
    if row is not None:
        await session.delete(row)
        await session.flush()


async def has_discord_webhook(session: AsyncSession, settings: Settings | None = None) -> bool:
    """Whether a webhook is configured -- the only fact a browser is ever told about it.

    Counts an unseeded ``REAPER_DISCORD_WEBHOOK`` too, so the UI reports "connected" from
    first boot rather than only after the seed has been read once.
    """
    if await _get(session, DISCORD_WEBHOOK_KEY, default=None) is not None:
        return True
    if settings is not None and settings.discord_webhook is not None:
        return bool(settings.discord_webhook.get_secret_value().strip())
    return False


# --- Leaving Soon announced set --------------------------------------------


async def get_leaving_soon_announced(session: AsyncSession) -> set[int]:
    """The rating keys already announced as leaving soon."""
    value = await _get(session, LEAVING_SOON_ANNOUNCED_KEY, default=[])
    return {int(k) for k in value}


async def set_leaving_soon_announced(session: AsyncSession, keys: set[int]) -> None:
    """Persist the announced set. Sorted so the stored JSON is stable and diffable."""
    await _set(session, LEAVING_SOON_ANNOUNCED_KEY, sorted(keys))
