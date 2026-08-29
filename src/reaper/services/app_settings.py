# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small singleton settings the admin edits in the web UI.

These live in the ``app_setting`` key/value table, distinct from ``Settings`` (the
environment/bootstrap concerns that cannot live in the database they protect). What
belongs here:

* **whether deletion is enabled**, the one destructive-action switch. It is turned on
  from the web UI, but the route that flips it on first checks the admin password (see
  ``api.settings``); turning it off needs no password. The environment variable only seeds
  the first-run default; after that this stored value wins.
* **the scan schedule**, an optional cron for an automatic, read-only scan. A scan
  never deletes, so scheduling one is safe; it just keeps the review queue fresh.
* **the Leaving Soon switches and library choices**: whether the Plex shelf is on,
  whether it may be written while read-only (env-seeded, stored value wins), and which
  Plex libraries Reaper may touch at all.

Every value is stored as JSON so the column stays one shape whatever the type.
"""

from __future__ import annotations

import json
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tzlocal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clock import utcnow
from reaper.config import RuntimeSafety, Settings, parse_trusted_proxies
from reaper.crypto import SecretBox
from reaper.db.models import AppSetting
from reaper.engine.reason import Reason, from_wire, to_wire
from reaper.i18n import DEFAULT_TAG, shipped_tags

DESTRUCTIVE_KEY = "destructive_enabled"
SCAN_SCHEDULE_KEY = "scan_schedule"
#: Per-job cron override for one background upkeep job, stored one row per job under
#: ``maintenance_schedule:{job_id}``. One row per job, rather than one shared dict, so saving
#: two jobs at the same time cannot overwrite each other's change. A row present with a null
#: value means the job is off; no row falls back to the code default. See
#: ``scheduler.DEFAULT_MAINTENANCE_CRONS`` and ``get_maintenance_schedules``.
MAINTENANCE_SCHEDULE_PREFIX = "maintenance_schedule:"
#: One row per upkeep job records its last completion: when it ran, whether it succeeded,
#: and a short plain-language result, so the Jobs page shows the same last-run line for
#: every job. One key per job, never a shared dict, so writing one job's result cannot
#: overwrite another's. The scan and Leaving Soon read their own successful last run from
#: their own sources (the snapshot, the leaving_soon_last row). This store also holds one
#: row for the scan itself (job id ``scheduled_scan``), written only when a scheduled scan
#: crashes outright, so a run that produced no snapshot still shows up on the Jobs page
#: (see ``scheduler.scheduled_scan``).
JOB_LAST_RUN_PREFIX = "job_last_run:"
#: The Discord webhook, stored Fernet-encrypted exactly like an instance API key. Its
#: token lives in the URL path, so the whole URL is a credential.
DISCORD_WEBHOOK_KEY = "discord_webhook_enc"
#: The rating keys already announced as "leaving soon". Stored so an item is never
#: announced twice, even across repeated syncs where the Plex label write never lands (in
#: preview, or while deletion is off). See :func:`reaper.services.leaving_soon.announce_new`.
LEAVING_SOON_ANNOUNCED_KEY = "leaving_soon_announced"
#: Whether the "Leaving Soon" shelf in Plex is on at all. Off (the default) means Reaper
#: never touches the shelf and never runs the reconcile on its own.
LEAVING_SOON_ENABLED_KEY = "leaving_soon_enabled"
#: Whether the shelf may be written while deletion is off. The stored value wins; the
#: environment variable (REAPER_ALLOW_UNARMED_LEAVING_SOON) is only the first-run default,
#: exactly like the deletion switch. Edited in Settings -> Plex.
LEAVING_SOON_UNARMED_KEY = "leaving_soon_unarmed"
#: What the shelf is called in Plex: the collection title and the label, one name for one
#: shelf. Every Plex user on the server reads it, so it is the operator's own word for
#: their library, never translated. Not a credential, so `.env.example` carries no matching
#: variable. Edited in Settings -> Plex.
LEAVING_SOON_NAME_KEY = "leaving_soon_name"
#: The name Reaper last wrote to Plex, which is what the server still shows until the next
#: pass. A rename does not take effect by itself, so the reconcile needs the old name to
#: find the collection and carry its labels across. This row only advances after a pass
#: writes every library cleanly, so a rename that half-lands is retried rather than
#: stranded under a name nothing remembers. An install that predates this row has always
#: been writing the default name, which is exactly what the default here says.
LEAVING_SOON_APPLIED_NAME_KEY = "leaving_soon_applied_name"
#: What the last shelf update did and when: the status line under the Leaving Soon
#: settings. ``{"at": iso, "movies": n, "seasons": n, "applied": bool, "ok": bool,
#: "result": str}``. ``applied`` is false both in preview and on a real per-library error,
#: so it alone cannot color the Jobs page's status dot. ``ok`` is false only for a real
#: per-library problem, and that is what the dot and the ``result`` line reflect.
LEAVING_SOON_LAST_KEY = "leaving_soon_last"
#: The last shelf pass that did not complete: ``{"at": iso, "result": str}``. Written only
#: by ``leaving_soon.after_scan``, whose skips (an untrustworthy scan, an unreachable Plex, a
#: surprise) all return before reaching the row above. Without this row, a scan that never
#: touched the shelf would leave the previous pass's status showing as the current answer.
#: Read alongside that row and preferred only while it is newer, the same way ScanRow prefers
#: ``job_last_run:scheduled_scan`` over a stale snapshot, so a pass that later completes wins
#: on its own timestamp and nothing has to clear this row.
LEAVING_SOON_LAST_SKIP_KEY = "leaving_soon_last_skip"
#: The Plex libraries Reaper may touch, as last synced from the server:
#: ``[{"key": int, "title": str, "kind": "movie"|"show", "enabled": bool}]``. Only video
#: libraries are stored; the enabled flags survive a re-sync.
PLEX_LIBRARIES_KEY = "plex_libraries"
#: Where "open in Plex" links send the admin. A plain URL, not a secret: most installs
#: keep the hosted Plex Web default; a self-hosted Plex Web front-end overrides it.
PLEX_WEB_URL_KEY = "plex_web_url"
#: What this install calls itself, in Discord messages and the browser tab. Purely
#: cosmetic; never used as an identifier anywhere.
APPLICATION_NAME_KEY = "application_name"
#: Where people reach this install (``https://reaper.example.com``). Used to build the
#: links notifications carry; empty means notifications simply carry no links.
APPLICATION_URL_KEY = "application_url"
#: The color the whole UI is tinted with: buttons, links, highlights, the scan line.
#: A ``#rrggbb`` string, purely cosmetic. Unlike the per-browser theme it is stored on the
#: server, so every browser that opens this install sees it. Validated at the API edge.
ACCENT_COLOR_KEY = "accent_color"
#: Where the review queue opens each TV show with its season list already expanded:
#: ``off``, ``desktop``, ``both``, or ``mobile`` (see ``EXPAND_SEASONS_MODES``). A display
#: preference only, setting the starting state of the queue's show cards, never a deletion
#: behavior. Stored on the server like the accent color, so every browser that opens this
#: install starts the same way. Off by default.
#:
#: The key name still matches an older boolean setting, so an install with that setting
#: already on keeps its stored row and needs no migration. ``get_expand_seasons_mode`` reads
#: both a legacy boolean row and the current string values.
EXPAND_SEASONS_MODE_KEY = "expand_seasons_default"
#: The screens the queue may open seasons on. ``both`` is what a stored legacy ``True``
#: reads back as, meaning every screen. This type is the one declaration; the tuple below
#: and the name lookup derive from it, and the API's request and response models import the
#: type itself, so all three stay in agreement.
ExpandSeasonsMode = Literal["off", "desktop", "both", "mobile"]
EXPAND_SEASONS_MODES: tuple[ExpandSeasonsMode, ...] = get_args(ExpandSeasonsMode)
_EXPAND_SEASONS_BY_NAME: dict[str, ExpandSeasonsMode] = {m: m for m in EXPAND_SEASONS_MODES}
DEFAULT_EXPAND_SEASONS_MODE: ExpandSeasonsMode = "off"
#: How long a plain Spare press keeps an item, in days. ``0`` means forever, the shipped
#: default, so an existing install's Spare button keeps items for good until the operator
#: sets a length. A single title can still be spared for a different length from its own
#: Spare menu; this is only the default the button uses.
DEFAULT_SPARE_DAYS_KEY = "default_spare_days"
#: The one instance API key, Fernet-encrypted like every stored credential. Sent by
#: callers as ``X-Api-Key``; the middleware compares a SHA-256 of it (see main.py's
#: startup, which caches the digest on app.state).
API_KEY_KEY = "api_key_enc"
#: Reverse-proxy trust: whether forwarded headers are honored at all, and from which
#: proxy addresses (single IPs or CIDR ranges). Off by default: a forwarded header
#: from an untrusted peer is attacker-controlled and is always ignored.
PROXY_TRUST_ENABLED_KEY = "proxy_trust_enabled"
TRUSTED_PROXIES_KEY = "trusted_proxies"
#: The logging level the operator picked, one of ``logbuffer.UI_LEVELS``
#: (DEBUG/INFO/WARNING). Stored value wins; ``REAPER_LOG_LEVEL`` is only the seed until
#: then, like every other env-seeded switch, and it may also carry ERROR, which the picker
#: does not offer.
LOG_LEVEL_KEY = "log_level"
#: The server time zone the scheduler's timed jobs run on: the nightly scan and the upkeep
#: jobs. An IANA name like ``America/New_York``, so a cron set for 2 AM fires at 2 AM here,
#: not in the container's own zone. Stored value wins; ``REAPER_TIMEZONE`` is only the
#: first-boot seed, and an unset seed falls back to the host's own zone. See ``get_timezone``.
TIMEZONE_KEY = "timezone"
#: When a backup was last downloaded (ISO 8601, UTC). Only ever surfaced as "last backup"
#: on the Backup panel, so a losable copy on someone else's schedule is not a source of truth.
BACKUP_LAST_AT_KEY = "backup_last_at"
#: The one language setting: what the app is shown in, and what a notification is written
#: in, as a BCP 47 tag. Not a credential, and not seeded from the environment like the
#: timezone, since no first-boot deployment concern depends on a language choice; so
#: `.env.example` carries no matching variable. Edited in Settings -> General.
#:
#: It holds a tag from the browser's own list of languages, which is not the same list as
#: `reaper.i18n.shipped_tags()`: a translation reaches the UI a release before its
#: `backend.json` ships. So there are two readers: `get_language` returns what is stored,
#: and `get_notification_language` narrows it to a tag the notifier can actually serve.
#:
#: An empty row means nobody has chosen yet. The browser sets it on first sign-in from its
#: own preferred languages, so the row stays empty only until then.
LANGUAGE_KEY = "language"

DEFAULT_PLEX_WEB_URL = "https://app.plex.tv"
DEFAULT_APPLICATION_NAME = "Reaper"
#: What the Plex shelf is called until the operator says otherwise. Plex title-cases this on
#: the way in, and every comparison in the Plex client lower-cases both sides, so the display
#: form is what Reaper writes and searches for.
DEFAULT_LEAVING_SOON_NAME = "Leaving Soon"
#: How long a shelf name may be. A Plex collection title and a label are both free text, so
#: this is Reaper's own limit: long enough for a phrase in any language, short enough that
#: the name still reads on a shelf row. Enforced once, at the route, which refuses a longer
#: name rather than storing a truncated one the operator never typed.
LEAVING_SOON_NAME_MAX = 60
#: The last-resort time zone: what APScheduler would fall back to if the host's own zone
#: cannot be read either. UTC is the safe, universal choice.
DEFAULT_TIMEZONE = "UTC"
#: The built-in accent, a sky blue. The default the UI ships with and resets to.
DEFAULT_ACCENT_COLOR = "#25c3ff"


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


def _env_seeded_switch(stored: Any, seed: bool) -> bool:
    """An on/off switch the environment only seeds: the stored value wins, and a missing row
    falls back to ``seed``.

    Only a missing row counts as "nothing stored". A stored ``false`` is the operator
    turning the switch off on purpose. Treating it as unset would hand the answer back to
    an environment variable that is usually still set, so the switch would silently turn
    itself back on at the next restart. Written here rather than in each getter, so every
    env-seeded switch follows the same order.
    ``tests/test_app_settings_precedence.py`` fails until a new switch also has a case
    there, as long as it reads its row through ``_get``, which is the only call the test
    can see.
    """
    return seed if stored is None else bool(stored)


def _decrypted_or_absent(box: SecretBox, stored: Any) -> str | None:
    """A stored credential under the current key, or ``None`` when it will not decrypt.

    A credential written under a secret key that has since rotated reads as absent rather
    than raising. A broken webhook must not break a scan, a plan, or a run, and a broken API
    key must not break a request path; re-entering either in the UI fixes it. Every caller
    must agree on that reading, because a send that skips the credential and a panel that
    says it is connected describe the same credential two different ways.
    ``GeneralSettingsOut.api_key_set`` resolves through ``get_api_key`` for that reason.
    """
    try:
        return box.decrypt(str(stored))
    except ValueError:
        return None


# --- deletion enabled ------------------------------------------------------


async def lists_seeded(session: AsyncSession) -> bool:
    """Whether the default protection lists were ever created. A flag rather than checking
    "do any rows exist", because an operator who deletes the shipped lists means it, and
    reseeding on the next read would bring back a protection they removed."""
    return bool(await _get(session, "lists_seeded", False))


async def set_lists_seeded(session: AsyncSession) -> None:
    await _set(session, "lists_seeded", True)


async def destructive_enabled(session: AsyncSession, settings: Settings) -> bool:
    """Whether deletion is currently on.

    The stored value wins. If nothing has been stored yet (a fresh install), fall back to
    the environment's first-run default, so a declarative deployment can ship armed while
    a normal install starts read-only until someone turns it on.
    """
    stored = await _get(session, DESTRUCTIVE_KEY, default=None)
    return _env_seeded_switch(stored, settings.destructive_actions_enabled)


async def set_destructive_enabled(session: AsyncSession, *, enabled: bool) -> None:
    """Turn deletion on or off. The API verifies the admin password before turning it ON;
    turning it off is always allowed, because making Reaper safer is never gated."""
    await _set(session, DESTRUCTIVE_KEY, bool(enabled))


async def runtime_safety(session: AsyncSession, settings: Settings) -> RuntimeSafety:
    """The current deletion permission. The one place that assembles it, so every caller
    agrees on whether Reaper may delete right now.

    ``recovery_mode`` comes straight from the environment rather than the database, because
    it is the one input here that must answer for THIS process: recovery is armed by a
    restart, so the boot that armed it is exactly the boot that must hold deletion off.
    """
    return RuntimeSafety(
        destructive_enabled=await destructive_enabled(session, settings),
        allow_leaving_soon_unarmed=await leaving_soon_unarmed(session, settings),
        recovery_mode=settings.recovery,
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


# --- application identity ----------------------------------------------------


async def get_application_name(session: AsyncSession) -> str:
    """What this install calls itself. Defaults to "Reaper"."""
    value = await _get(session, APPLICATION_NAME_KEY, default=None)
    name = str(value).strip() if value else ""
    return name or DEFAULT_APPLICATION_NAME


async def set_application_name(session: AsyncSession, name: str | None) -> None:
    """Store the display name. Empty resets to the default."""
    cleaned = (name or "").strip()
    await _set(session, APPLICATION_NAME_KEY, cleaned or None)


async def get_application_url(session: AsyncSession) -> str | None:
    """Where people reach this install, or None (notifications carry no links)."""
    value = await _get(session, APPLICATION_URL_KEY, default=None)
    return str(value) if value else None


async def set_application_url(session: AsyncSession, url: str | None) -> None:
    cleaned = (url or "").strip().rstrip("/")
    await _set(session, APPLICATION_URL_KEY, cleaned or None)


def is_valid_timezone(name: str) -> bool:
    """Whether ``name`` is a known IANA zone. The one check the API edge and the resolver
    share, so a value that would fail to build a ``ZoneInfo`` never reaches the scheduler."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _detect_host_timezone() -> str:
    """The host's own IANA zone name (from the standard TZ variable or /etc/localtime), or
    UTC if it can't be read. This is the last fallback, so an install whose container zone
    was already correct keeps firing at the same wall-clock time."""
    try:
        name = tzlocal.get_localzone_name()
    except Exception:
        return DEFAULT_TIMEZONE
    return name if name and is_valid_timezone(name) else DEFAULT_TIMEZONE


async def get_timezone(session: AsyncSession, settings: Settings) -> str:
    """The effective server time zone, as an IANA name.

    Stored value wins (Settings -> General); then the ``REAPER_TIMEZONE`` first-boot seed;
    then the host's own zone; then UTC. Every layer is validated, so a corrupt stored value
    or a typo in the environment falls through to the next source rather than raising, and
    the scheduler can always build a zone from what this returns.
    """
    stored = await _get(session, TIMEZONE_KEY, default=None)
    if stored and is_valid_timezone(str(stored)):
        return str(stored)
    if settings.timezone and is_valid_timezone(settings.timezone):
        return settings.timezone
    return _detect_host_timezone()


async def set_timezone(session: AsyncSession, name: str) -> None:
    """Store the server time zone. Validated to a real IANA name at the API edge."""
    await _set(session, TIMEZONE_KEY, name)


async def get_accent_color(session: AsyncSession) -> str:
    """The UI accent color as ``#rrggbb``. Defaults to the built-in sky blue."""
    value = await _get(session, ACCENT_COLOR_KEY, default=None)
    color = str(value).strip().lower() if value else ""
    return color or DEFAULT_ACCENT_COLOR


async def set_accent_color(session: AsyncSession, color: str | None) -> None:
    """Store the accent color, lower-cased. Empty resets to the default. The value is
    validated to ``#rrggbb`` at the API edge, so a malformed color never reaches here."""
    cleaned = (color or "").strip().lower()
    await _set(session, ACCENT_COLOR_KEY, cleaned or None)


async def get_language(session: AsyncSession) -> str | None:
    """The stored BCP 47 tag, or ``None`` while nobody has chosen one.

    Returned exactly as stored. This is what the Settings picker shows, and it holds a tag
    from the browser's own list, which the backend's ``shipped_tags()`` need not contain.
    ``get_notification_language`` is the reader that narrows it to a servable tag.
    """
    stored = await _get(session, LANGUAGE_KEY, default=None)
    return str(stored) if stored else None


async def set_language(session: AsyncSession, tag: str) -> None:
    """Store the language. Validated at the API edge to the shape of a BCP 47 tag, never
    against a list of known tags: the browser can offer a tag this build has no
    ``backend.json`` for yet, and storing it now is what lets Discord start using it as soon
    as that catalog ships."""
    await _set(session, LANGUAGE_KEY, tag)


async def get_notification_language(session: AsyncSession) -> str:
    """The BCP 47 tag ``notify.discord`` writes its Leaving Soon message in.

    Falls back to English when nothing is stored, and also when the stored tag has no
    shipped backend catalog yet (the UI can carry a translation a release before its
    ``backend.json`` ships). This is the one place that tells the operator's real choice
    apart from a tag the notifier cannot actually serve.
    """
    stored = await get_language(session)
    if stored and stored in shipped_tags():
        return stored
    return DEFAULT_TAG


async def get_expand_seasons_mode(session: AsyncSession) -> ExpandSeasonsMode:
    """Which screens the review queue starts each show's season list expanded on: one of
    ``EXPAND_SEASONS_MODES``. Off until the operator picks a screen, so an existing install
    keeps its collapsed cards.

    The stored row can hold either a boolean or a string, since no migration ever rewrote
    old rows: a stored ``True`` means "expanded everywhere" and reads back as ``both``, and
    ``False`` reads back as ``off``. Anything else, such as a hand-edited row or a value
    from a newer build, also reads as ``off``, the shipped default, rather than turning a
    display preference into a server error.
    """
    stored = await _get(session, EXPAND_SEASONS_MODE_KEY, default=None)
    if isinstance(stored, bool):
        return "both" if stored else DEFAULT_EXPAND_SEASONS_MODE
    if isinstance(stored, str):
        return _EXPAND_SEASONS_BY_NAME.get(stored, DEFAULT_EXPAND_SEASONS_MODE)
    return DEFAULT_EXPAND_SEASONS_MODE


async def set_expand_seasons_mode(session: AsyncSession, *, mode: ExpandSeasonsMode) -> None:
    """Store which screens open seasons. The API edge validates against the same
    ``ExpandSeasonsMode``, so an unknown value never reaches here."""
    await _set(session, EXPAND_SEASONS_MODE_KEY, mode)


async def get_default_spare_days(session: AsyncSession) -> int:
    """Days a plain Spare press keeps an item; ``0`` means forever. Forever until the
    operator sets a length, so an existing install's Spare button keeps items for good.

    A stored value below zero, only reachable by hand-editing the database, clamps to
    ``0`` rather than becoming a negative time span that would expire a spare in the past."""
    return max(0, int(await _get(session, DEFAULT_SPARE_DAYS_KEY, default=0)))


async def set_default_spare_days(session: AsyncSession, *, days: int) -> None:
    await _set(session, DEFAULT_SPARE_DAYS_KEY, max(0, int(days)))


# --- backup ------------------------------------------------------------------


async def get_last_backup_at(session: AsyncSession) -> str | None:
    """When a backup was last downloaded (ISO 8601, UTC), or ``None`` if never."""
    value = await _get(session, BACKUP_LAST_AT_KEY, default=None)
    return str(value) if value else None


async def set_last_backup_at(session: AsyncSession, when: str) -> None:
    await _set(session, BACKUP_LAST_AT_KEY, when)


# --- the instance API key ----------------------------------------------------


async def get_api_key(session: AsyncSession, box: SecretBox) -> str | None:
    """The stored API key, decrypted, or None when none has been generated.

    A stored value that will not decrypt (the secret key changed) reads as absent, the
    same posture as the Discord webhook: a broken credential is re-generated in the UI,
    never allowed to break a request path.
    """
    stored = await _get(session, API_KEY_KEY, default=None)
    if stored is None:
        return None
    return _decrypted_or_absent(box, stored)


async def set_api_key(session: AsyncSession, box: SecretBox, key: str) -> None:
    await _set(session, API_KEY_KEY, box.encrypt(key))


async def clear_api_key(session: AsyncSession) -> None:
    row = await session.get(AppSetting, API_KEY_KEY)
    if row is not None:
        await session.delete(row)
        await session.flush()


# --- reverse proxy trust -----------------------------------------------------


async def proxy_trust_enabled(session: AsyncSession, settings: Settings) -> bool:
    """Whether forwarded headers are honored at all. Off by default: fail closed.

    The stored value wins; ``REAPER_PROXY_TRUST_ENABLED`` is only the first-boot seed,
    like every other env-seeded switch.
    """
    stored = await _get(session, PROXY_TRUST_ENABLED_KEY, default=None)
    return _env_seeded_switch(stored, settings.proxy_trust_enabled)


async def set_proxy_trust_enabled(session: AsyncSession, *, enabled: bool) -> None:
    await _set(session, PROXY_TRUST_ENABLED_KEY, bool(enabled))


async def get_trusted_proxies(session: AsyncSession, settings: Settings) -> list[str]:
    """The proxy addresses (IPs or CIDR ranges) whose forwarded headers are trusted.
    Validated at the API edge; stored as the cleaned strings.

    The stored value wins; ``REAPER_TRUSTED_PROXIES`` (comma- or space-separated) is only
    the first-boot seed. An empty stored list is a real choice and is kept, distinct from
    never-stored, which falls back to the seed.
    """
    value = await _get(session, TRUSTED_PROXIES_KEY, default=None)
    if value is None:
        return parse_trusted_proxies(settings.trusted_proxies)
    return [str(v) for v in value if str(v).strip()]


async def set_trusted_proxies(session: AsyncSession, proxies: list[str]) -> None:
    await _set(session, TRUSTED_PROXIES_KEY, [p.strip() for p in proxies if p.strip()])


# --- logging level -----------------------------------------------------------


async def get_log_level_setting(session: AsyncSession) -> str | None:
    """The stored logging level, or None when the environment seed still governs."""
    value = await _get(session, LOG_LEVEL_KEY, default=None)
    return str(value) if value else None


async def set_log_level(session: AsyncSession, level: str) -> None:
    await _set(session, LOG_LEVEL_KEY, level.upper())


# --- scan schedule ---------------------------------------------------------


async def get_scan_schedule(session: AsyncSession) -> str | None:
    """The cron expression for the automatic read-only scan, or None if disabled.

    ``None`` (the default) means no automatic scan, so the owner runs scans by hand.
    A stored value is a 5-field cron string (minute hour day month day-of-week).
    """
    value = await _get(session, SCAN_SCHEDULE_KEY, default=None)
    return str(value) if value else None


async def set_scan_schedule(session: AsyncSession, cron: str | None) -> None:
    await _set(session, SCAN_SCHEDULE_KEY, cron or None)


# --- background-job schedules ----------------------------------------------


async def get_maintenance_schedules(session: AsyncSession) -> dict[str, str | None]:
    """Per-job cron overrides for the background upkeep jobs.

    A job id present with a cron string runs on that schedule; present with ``null`` is
    turned off; absent falls back to the built-in default (see
    ``scheduler.DEFAULT_MAINTENANCE_CRONS``). The present-with-null case is deliberately
    distinct from absent, so "off" survives a default-time change in the code.

    Read from the per-job rows (one ``maintenance_schedule:{job_id}`` each), so a job that
    was explicitly turned off is present with ``None`` and one never touched is simply absent.
    """
    rows = (
        (
            await session.execute(
                select(AppSetting).where(AppSetting.key.startswith(MAINTENANCE_SCHEDULE_PREFIX))
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, str | None] = {}
    for row in rows:
        job_id = row.key[len(MAINTENANCE_SCHEDULE_PREFIX) :]
        value = json.loads(row.value_json)
        out[job_id] = str(value) if value else None
    return out


async def set_maintenance_schedule(session: AsyncSession, job_id: str, cron: str | None) -> None:
    """Store one upkeep job's schedule. ``None`` turns it off, stored explicitly.

    Writes only this job's own row, so saving a different job at the same time cannot
    overwrite it."""
    await _set(session, f"{MAINTENANCE_SCHEDULE_PREFIX}{job_id}", cron or None)


# --- upkeep job last-run ---------------------------------------------------


def thaw_stored_reason(value: dict[str, Any]) -> Reason:
    """Read one stored job-outcome reason back into a ``Reason``. A fresh row stores
    ``{"k", "p"}``.

    An older row, written before this shape, carries a bare English phrase under
    ``"result"`` instead. That reads back as ``Reason("legacy", {"text": ...})``, the same
    way ``engine.reason.from_wire`` already handles a bare stored string: an old row still
    reads, it just stops being translated. Every job-outcome reader shares this one
    function (``get_job_last_runs``, ``get_leaving_soon_last``, and
    ``get_leaving_soon_last_skip``), so a record missing the fresh key reads back the
    same way everywhere.
    """
    return from_wire(
        {"k": value["k"], "p": value.get("p")} if "k" in value else str(value.get("result", ""))
    )


async def get_job_last_runs(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """The last completion of each upkeep job, keyed by job id.

    Each value is ``{"at": iso, "ok": bool, "result": Reason}``: when it last finished,
    whether it succeeded, and a typed reason the browser turns into a sentence
    (``JobStatus.tsx``'s ``jobResultText``). A job that has never completed is simply
    absent, which the Jobs page reads as "hasn't run yet". Read from the per-job rows, so
    one job's write never touches another's.
    """
    rows = (
        (
            await session.execute(
                select(AppSetting).where(AppSetting.key.startswith(JOB_LAST_RUN_PREFIX))
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = json.loads(row.value_json)
        if isinstance(value, dict):
            out[row.key[len(JOB_LAST_RUN_PREFIX) :]] = {
                "at": value.get("at"),
                "ok": value.get("ok"),
                "result": thaw_stored_reason(value),
            }
    return out


async def set_job_last_run(
    session: AsyncSession, job_id: str, *, at: str, ok: bool, result: Reason
) -> None:
    """Record one upkeep job's last completion. Writes only this job's own row."""
    await _set(
        session,
        f"{JOB_LAST_RUN_PREFIX}{job_id}",
        {"at": at, "ok": bool(ok), **to_wire(result)},
    )


# --- Discord webhook -------------------------------------------------------


async def get_discord_webhook(
    session: AsyncSession, box: SecretBox, settings: Settings
) -> str | None:
    """The stored Discord webhook URL, decrypted, or ``None`` when notifications are off.

    The stored value wins. On a fresh install with ``REAPER_DISCORD_WEBHOOK`` set in the
    environment, that value is a first-boot seed: it is encrypted and imported into the
    database once, on first read, and the database is the source of truth after that. So
    removing the environment variable later does not silently turn notifications off, and a
    webhook edited in the UI is never overwritten by a stale environment value. This
    mirrors how instance API keys are seeded (see ``reaper.services.seeding``).

    A stored value that will not decrypt, because the secret key changed, is treated as
    absent rather than raised: a broken notification credential must never break a scan, a
    plan, or a run. It can be re-entered in the UI.
    """
    stored = await _get(session, DISCORD_WEBHOOK_KEY, default=None)
    if stored is not None:
        # `or None`: a stored value that decrypts to an empty string means nothing is
        # configured, the same answer the seed branch below gives for an empty seed. Every
        # caller checks `is None` for "no webhook", so an empty string here would build a
        # notifier that posts nowhere while the panel still said connected. This clause
        # lives here rather than in `_decrypted_or_absent`, which only answers a decrypt
        # failure and is also read by `get_api_key`, where an empty key is not a concern.
        return (_decrypted_or_absent(box, stored) or "").strip() or None
    seed = settings.discord_webhook
    if seed is None:
        return None
    url = seed.get_secret_value().strip()
    if not url:
        return None
    # Seed once: store it encrypted under the current key, so it survives the environment
    # variable being removed later. If the caller does not commit, the seed is simply
    # derived again on the next read.
    await _set(session, DISCORD_WEBHOOK_KEY, box.encrypt(url))
    return url


async def set_discord_webhook(session: AsyncSession, box: SecretBox, url: str) -> None:
    """Store (or replace) the webhook, encrypted. The URL is validated at the API edge."""
    await _set(session, DISCORD_WEBHOOK_KEY, box.encrypt(url))


async def clear_discord_webhook(session: AsyncSession) -> None:
    """Forget the webhook. Notifications go silent until one is set again."""
    row = await session.get(AppSetting, DISCORD_WEBHOOK_KEY)
    if row is not None:
        await session.delete(row)
        await session.flush()


async def has_discord_webhook(
    session: AsyncSession, box: SecretBox, settings: Settings | None = None
) -> bool:
    """Whether a webhook is configured. The only fact a browser is ever told about it.

    Counts an unseeded ``REAPER_DISCORD_WEBHOOK`` too, so the UI reports "connected" from
    first boot rather than only after the seed has been read once. A stored value that no
    longer decrypts, because the secret key was rotated, counts as not configured: every
    send skips it (see ``get_discord_webhook``), and the UI must not claim notifications
    are on while grace warnings silently never post. Re-entering the URL in the UI fixes
    it.
    """
    stored = await _get(session, DISCORD_WEBHOOK_KEY, default=None)
    if stored is not None:
        # Same rule as `get_discord_webhook`'s stored branch. This answers whether the
        # panel says connected, and that one answers whether a send happens, so the two
        # must never disagree.
        return bool((_decrypted_or_absent(box, stored) or "").strip())
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


# --- Leaving Soon switches ---------------------------------------------------


async def leaving_soon_enabled(session: AsyncSession) -> bool:
    """Whether the "Leaving Soon" shelf is on. Off by default: a fresh install touches
    nothing in Plex until someone turns the shelf on in Settings -> Plex."""
    return bool(await _get(session, LEAVING_SOON_ENABLED_KEY, default=False))


async def set_leaving_soon_enabled(session: AsyncSession, *, enabled: bool) -> None:
    await _set(session, LEAVING_SOON_ENABLED_KEY, bool(enabled))


async def _shelf_name(session: AsyncSession, key: str) -> str:
    """One of the two shelf-name rows, or the shipped default when it is unset or blank."""
    value = await _get(session, key, default=None)
    name = str(value).strip() if value else ""
    return name or DEFAULT_LEAVING_SOON_NAME


async def get_leaving_soon_name(session: AsyncSession) -> str:
    """What the shelf is called in Plex."""
    return await _shelf_name(session, LEAVING_SOON_NAME_KEY)


async def set_leaving_soon_name(session: AsyncSession, name: str | None) -> None:
    """Store the shelf name. Empty resets to the default.

    Stores only the requested name. What Plex currently shows is a different fact, held in
    a different row (:func:`set_leaving_soon_applied_name`) that is written only once a pass
    has actually moved the shelf. So a rename saved here stays pending until then. The
    length limit is enforced at the route, which refuses a long name rather than storing a
    shorter one nobody typed.
    """
    await _set(session, LEAVING_SOON_NAME_KEY, (name or "").strip() or None)


async def get_leaving_soon_applied_name(session: AsyncSession) -> str:
    """The name Plex still shows: what the last completed pass wrote.

    Unset means every pass so far wrote the default, which is true for an install that
    predates this setting. This must read the last applied name, never the current
    requested name: falling back to the current name would tell the reconcile a rename
    had already landed, and strand the old collection and its labels under a name nothing
    would look for again.
    """
    return await _shelf_name(session, LEAVING_SOON_APPLIED_NAME_KEY)


async def set_leaving_soon_applied_name(session: AsyncSession, name: str) -> None:
    """Record the name a pass just wrote to Plex."""
    await _set(session, LEAVING_SOON_APPLIED_NAME_KEY, name)


async def leaving_soon_unarmed(session: AsyncSession, settings: Settings) -> bool:
    """Whether the shelf may be written while deletion is off.

    The stored value wins; ``REAPER_ALLOW_UNARMED_LEAVING_SOON`` only decides the
    first-run default, exactly like the deletion switch. This can only ever widen the
    shelf writes (collection + label); file deletions are untouched by it.
    """
    stored = await _get(session, LEAVING_SOON_UNARMED_KEY, default=None)
    return _env_seeded_switch(stored, settings.allow_unarmed_leaving_soon)


async def set_leaving_soon_unarmed(session: AsyncSession, *, allowed: bool) -> None:
    await _set(session, LEAVING_SOON_UNARMED_KEY, bool(allowed))


async def get_leaving_soon_last(session: AsyncSession) -> dict[str, Any] | None:
    """What the last shelf update did: when it ran, how many movies and seasons are on
    the shelves, and whether the writes actually landed in Plex.

    Returns the raw stored dict, wire-encoded reason included. ``api.settings`` reads its
    ``at``, ``movies``, ``seasons``, ``applied``, and ``ok`` fields directly, and reads the
    reason back itself with ``thaw_stored_reason``, the same helper
    ``get_job_last_runs`` and ``get_leaving_soon_last_skip`` use.
    """
    value = await _get(session, LEAVING_SOON_LAST_KEY, default=None)
    return dict(value) if isinstance(value, dict) else None


async def set_leaving_soon_last(
    session: AsyncSession,
    *,
    at: str,
    movies: int,
    seasons: int,
    applied: bool,
    ok: bool,
    reason: Reason,
) -> None:
    await _set(
        session,
        LEAVING_SOON_LAST_KEY,
        {
            "at": at,
            "movies": movies,
            "seasons": seasons,
            "applied": applied,
            "ok": ok,
            **to_wire(reason),
        },
    )


async def get_leaving_soon_last_skip(session: AsyncSession) -> tuple[str, Reason] | None:
    """When the last skip happened, and why, as a typed reason.

    An older row carries a bare English phrase under ``"result"`` instead of a
    wire-encoded reason. That reads back through the same shared helper every job-outcome
    reader uses (``thaw_stored_reason``): an old row still reads, it just stops being
    typed."""
    value = await _get(session, LEAVING_SOON_LAST_SKIP_KEY, default=None)
    if not isinstance(value, dict):
        return None
    at = str(value.get("at", ""))
    return at, thaw_stored_reason(value)


async def set_leaving_soon_last_skip(session: AsyncSession, *, at: str, reason: Reason) -> None:
    await _set(session, LEAVING_SOON_LAST_SKIP_KEY, {"at": at, **to_wire(reason)})


# --- Plex libraries ----------------------------------------------------------


async def get_plex_libraries(session: AsyncSession) -> list[dict[str, Any]]:
    """The video libraries as last synced from Plex, each with its enabled flag.

    Empty until the first sync. Only what the operator turned on may be touched by the
    Leaving Soon reconcile; everything else in Plex is invisible to it.
    """
    value = await _get(session, PLEX_LIBRARIES_KEY, default=[])
    return [dict(v) for v in value if isinstance(v, dict)]


async def set_plex_libraries(session: AsyncSession, libraries: list[dict[str, Any]]) -> None:
    """Store the synced library list. Sorted by section key so the JSON is stable."""
    await _set(session, PLEX_LIBRARIES_KEY, sorted(libraries, key=lambda d: int(d.get("key", 0))))


async def enabled_plex_libraries(session: AsyncSession) -> list[dict[str, Any]]:
    """Just the libraries the operator turned on."""
    return [lib for lib in await get_plex_libraries(session) if lib.get("enabled")]
