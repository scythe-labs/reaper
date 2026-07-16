# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bootstrap settings.

Only two kinds of setting belong here:

1. Things needed *before* the database exists, or that protect it.
   ``REAPER_SECRET_KEY`` encrypts the credentials stored in the database, so it
   cannot itself live there.

2. Things that must be changeable *without* the web UI -- because the UI is
   broken, or because you no longer trust it. That is the kill switch
   (``REAPER_DESTRUCTIVE_ACTIONS_ENABLED``) and the lockout escape hatch
   (``REAPER_RECOVERY``).

Everything else -- instance URLs, API keys, policies, schedules, the whitelist --
lives in the database, Fernet-encrypted, and is edited in the web UI. Integration
credentials may be *seeded* from the environment on first boot (see
``reaper.services.seeding``) as a convenience for declarative deployments, but
the database is the source of truth thereafter.
"""

from __future__ import annotations

import os
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# REAPER_SONARR_1_URL / _API_KEY / _NAME  ->  ("sonarr", "1", "URL")
_SEED_PATTERN = re.compile(
    r"^REAPER_(SONARR|RADARR|TAUTULLI|SEERR)_(\w+?)_(URL|API_KEY|NAME)$",
    re.IGNORECASE,
)


class InstanceSeed(BaseModel):
    """An integration declared in the environment, to be imported into the database once."""

    kind: str
    slot: str
    name: str
    base_url: str
    api_key: SecretStr

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        # Every client joins paths onto this; a trailing slash yields '//api/v3'.
        return v.strip().rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        env_prefix="REAPER_",
        extra="ignore",  # seed vars are parsed separately, so don't reject them
    )

    # --- Core -----------------------------------------------------------------
    data_dir: Path = Path("data")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False

    host: str = "0.0.0.0"  # noqa: S104 -- a container must bind all interfaces
    port: int = 8420

    # The shipped container is a single service: this process serves the API *and* the
    # built SPA from frontend/dist on one port. Development is the exception -- Vite
    # serves the UI live on its own port and proxies /api here, so mounting dist as well
    # would put a second UI on this port, frozen at whatever the last `npm run build`
    # produced. It looks identical right up until you edit a component, and then it
    # silently lags. Turning this off (see .claude/launch.json) leaves exactly one UI in
    # development. Production must leave it on.
    serve_spa: bool = True

    # --- Secrets --------------------------------------------------------------
    # REAPER_SECRET_KEY decrypts every stored credential, so changing it to a *fresh*
    # value would render them all unreadable. To rotate, set the new key here and the
    # retired one(s) in REAPER_SECRET_KEY_OLD (comma-separated for a chain): Reaper
    # encrypts under the new key while still decrypting what the old ones wrote, then
    # you drop the old key once everything has been re-saved. See reaper.secrets.
    secret_key: SecretStr | None = None
    secret_key_old: SecretStr | None = None

    # --- Notifications --------------------------------------------------------
    # The Discord webhook is the *real* notification channel -- the "Leaving Soon"
    # Plex label only reaches users who have pinned the library, which we cannot
    # force. The whole URL is a credential (its token lives in the path), so it is a
    # SecretStr and is redacted from logs.
    #
    # This is a FIRST-BOOT SEED only. The webhook is a credential and is not needed
    # before the database exists, so -- like an instance API key -- it lives in the
    # database, Fernet-encrypted, and is edited in the web UI (see
    # ``app_settings.get_discord_webhook``). This env var is imported into the database
    # once, on first read, and the database is the source of truth thereafter. Optional:
    # absent, with nothing stored, means notifications are off.
    discord_webhook: SecretStr | None = None

    # --- Safety ---------------------------------------------------------------
    # The FIRST-RUN default for whether deletion is allowed. After first boot the
    # database is the source of truth (toggled from the UI, password-gated), so this
    # only decides the starting state -- a declarative deployment can ship armed by
    # setting it true, and everyone else starts read-only until they turn it on.
    destructive_actions_enabled: bool = False

    # Writing the "Leaving Soon" Plex label is a mutation, but a benign one: it is
    # reversible and touches no files. Its whole purpose is to WARN users during the
    # grace countdown -- which is precisely when Reaper is unarmed. Gated like a
    # delete by default (so it writes only when deletion is enabled); set this true
    # on the host to allow the label to be written while still read-only. Like the
    # kill switch, it lives at the host boundary: a browser can never enable it.
    allow_unarmed_leaving_soon: bool = False

    # --- Anti-lockout ---------------------------------------------------------
    # Set REAPER_RECOVERY=true and restart: Reaper prints a single-use, 15-minute
    # login link to the container log. Requires the ability to set an env var and
    # read the logs -- i.e. host access -- so it grants nothing an attacker who
    # already has the host doesn't have.
    recovery: bool = False

    @field_validator("data_dir")
    @classmethod
    def _resolve_data_dir(cls, v: Path) -> Path:
        return v.expanduser().resolve()

    def ensure_data_dir(self) -> Path:
        """Both the app and Alembic need this before opening the database."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir

    @property
    def database_url(self) -> str:
        """Reaper's own state: policies, candidates, audit, credentials.

        Small, precious, migrated by Alembic. Losing it loses your decisions.
        """
        return f"sqlite+aiosqlite:///{self.data_dir / 'reaper.db'}"

    @property
    def sync_database_url(self) -> str:
        """Alembic runs migrations synchronously."""
        return f"sqlite:///{self.data_dir / 'reaper.db'}"

    @property
    def cache_database_url(self) -> str:
        """Other people's data, mirrored locally: Tautulli's watch history, the IMDb
        dataset, curated lists.

        **A separate file, deliberately.** These tables are large, slow to rebuild (a
        full history sync runs for minutes) and entirely reconstructible -- while
        ``reaper.db`` is small, precious and migrated. Keeping them together means a
        schema reset, a failed migration, or a corrupt file destroys hours of sync
        along with it.

        Separating them also states the invariant out loud: **nothing in here is a
        source of truth.** It can be deleted at any time and rebuilt.
        """
        return f"sqlite+aiosqlite:///{self.data_dir / 'cache.db'}"


def load_raw_env(settings: Settings) -> dict[str, str]:
    """The full environment, as the seeder sees it.

    Variables in a ``.env`` file are read by pydantic-settings into ``Settings``;
    they are **not** exported into ``os.environ``. Since the seed keys are dynamic
    (``REAPER_SONARR_4K_URL`` and friends) they never become ``Settings`` fields
    either -- so reading ``os.environ`` alone silently finds nothing, and the
    import quietly does no work. The dotenv files must be read directly.

    Precedence matches pydantic-settings: a real environment variable wins over
    the file, and a later file wins over an earlier one.
    """
    merged: dict[str, str] = {}
    env_files = settings.model_config.get("env_file") or ()
    if isinstance(env_files, str | Path):
        env_files = (env_files,)

    for env_file in env_files:
        path = Path(env_file)
        if path.is_file():
            merged.update({k: v for k, v in dotenv_values(path).items() if v is not None})

    merged.update(os.environ)
    return merged


def parse_instance_seeds(env: dict[str, str]) -> list[InstanceSeed]:
    """Collect ``REAPER_<KIND>_<SLOT>_<FIELD>`` groups from the environment.

    Example::

        REAPER_SONARR_HD_URL=https://sonarr.example.net
        REAPER_SONARR_HD_API_KEY=...
        REAPER_SONARR_4K_URL=https://sonarr-4k.example.net
        REAPER_SONARR_4K_API_KEY=...

    The slot is free-form and becomes the instance's display name unless an
    explicit ``_NAME`` is given. A group missing a URL or an API key is skipped
    rather than half-imported.
    """
    groups: dict[tuple[str, str], dict[str, str]] = {}
    for raw_key, value in env.items():
        match = _SEED_PATTERN.match(raw_key)
        if not match or not value.strip():
            continue
        kind, slot, field = match.group(1).lower(), match.group(2), match.group(3).upper()
        groups.setdefault((kind, slot), {})[field] = value.strip().strip("\"'")

    seeds: list[InstanceSeed] = []
    for (kind, slot), fields in sorted(groups.items()):
        url, api_key = fields.get("URL"), fields.get("API_KEY")
        if not url or not api_key:
            continue
        seeds.append(
            InstanceSeed(
                kind=kind,
                slot=slot,
                name=fields.get("NAME") or slot,
                base_url=url,
                api_key=SecretStr(api_key),
            )
        )
    return seeds


def generate_secret_key() -> str:
    """A URL-safe 32-byte key, the shape Fernet expects."""
    return secrets.token_urlsafe(32)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


class RuntimeSafety(BaseModel):
    """Whether Reaper may delete right now.

    ``destructive_enabled`` is the one switch. It lives in the database and is turned on
    from the web UI -- but only by someone who can supply the admin password (the
    ``api.settings`` route verifies it before flipping this on). Turning it *off* needs no
    password: making Reaper safer should never be gated. The environment variable
    ``REAPER_DESTRUCTIVE_ACTIONS_ENABLED`` only seeds the first-run default; after that the
    database is the source of truth, so a declarative deployment can still ship armed while
    the UI stays the live control.
    """

    destructive_enabled: bool = Field(
        default=False,
        description="May Reaper delete right now? Turned on in the UI, password-gated.",
    )

    # Permit the benign "Leaving Soon" label write while deletion is off. It never widens
    # what can be *deleted* -- file deletions still require destructive_enabled AND a
    # journalled declaration. This only lets the reversible, file-touching-nothing label be
    # written, so the grace-period warning can appear before deletion is turned on.
    allow_leaving_soon_unarmed: bool = Field(
        default=False, description="REAPER_ALLOW_UNARMED_LEAVING_SOON"
    )

    @property
    def destructive_allowed(self) -> bool:
        return self.destructive_enabled

    @property
    def leaving_soon_write_allowed(self) -> bool:
        """May the benign Leaving Soon label be written now?

        When deletion is on, yes (it is at least as safe as a delete). When not, only if
        the operator opted in on the host.
        """
        return self.destructive_allowed or self.allow_leaving_soon_unarmed

    def why_blocked(self) -> str | None:
        if not self.destructive_enabled:
            return (
                "Deletion is turned off, so Reaper can look but can't remove anything. "
                "Turn it on in Policy -> Deletion when you're ready."
            )
        return None
