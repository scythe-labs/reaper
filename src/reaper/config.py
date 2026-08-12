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

import errno
import os
import re
import secrets
import tempfile
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


#: The precious, migrated database, named once (rule 104). Both driver URLs and
#: :attr:`Settings.database_path` read it, and so does every caller that holds a bare
#: ``data_dir`` rather than a ``Settings``. It was spelled out at five sites before the boot
#: schema gate wanted a sixth, which is when four of them disagreeing became a real risk
#: rather than a tidiness argument.
DATABASE_FILENAME = "reaper.db"

#: The file an install that cannot receive environment variables is configured through,
#: named once (rule 104). :mod:`reaper.launcher` owns it: it writes the template, reads the
#: file into the environment, and rewrites keys a settings save changes. The backup carries
#: it and the restore puts it back and disarms recovery inside it, so those three cannot
#: drift onto different spellings of one filename.
#:
#: **It is declared here rather than in the launcher, and moving it back re-creates 7 import
#: cycles.** `services/backup.py` and `services/restore.py` imported the process entry point
#: for this string alone, and `launcher.main()` imports `reaper.preflight` and `reaper.main`
#: to serve, which closes the ring. Both already import this module, whose own in-tree import
#: closure is empty. `test_every_import_cycle_under_src_is_one_someone_declared` is what fails
#: if the import comes back; nothing did before it.
LAUNCHER_CONF_NAME = "launcher.conf"


class DataDirError(RuntimeError):
    """The data directory is missing or not writable, so Reaper cannot start.

    SQLite reports this as ``unable to open database file`` -- an error that names
    neither the path nor the cause. The usual trigger is a bind-mounted data folder
    owned by a different user than the one Reaper runs as (uid mismatch). This carries
    a plain, actionable message so the operator sees the fix, not a driver traceback.
    """

    def __init__(self, data_dir: Path, cause: OSError) -> None:
        self.data_dir = data_dir
        self.cause = cause
        super().__init__(_data_dir_error_message(data_dir, cause))


def _data_dir_error_message(data_dir: Path, cause: OSError) -> str:
    lead = f"Reaper can't write to its data folder ({data_dir})."
    # EACCES/EPERM is the ownership case -- give the chown fix. Anything else (a full
    # disk, a read-only mount) gets the plain lead plus the original error, since the
    # ownership advice would be wrong there.
    if cause.errno in (errno.EACCES, errno.EPERM):
        uid = os.getuid()
        gid = os.getgid()
        return (
            f"{lead}\n"
            "It keeps its database there, so it can't start. The folder is owned by a\n"
            f"different user than the one Reaper runs as (uid {uid}). If you bind-mounted\n"
            "a host folder, give it to that user and restart:\n\n"
            f"  chown -R {uid}:{gid} <the host folder you mapped to {data_dir}>\n\n"
            f"Original error: {cause}"
        )
    return f"{lead} It keeps its database there, so it can't start.\n\nOriginal error: {cause}"


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

    # First-boot seed for the server time zone the scheduler's timed jobs run on -- the
    # nightly scan and the upkeep jobs. An IANA name like "America/New_York"; empty means
    # detect from the host (the standard TZ / /etc/localtime). A cron such as "0 2 * * *"
    # then fires at 2 AM in THIS zone. Without it APScheduler falls back to the container's
    # own zone (UTC in most images), so a nightly scan set for 2 AM would silently run at
    # 2 AM UTC. Only the first-run default; after that the stored value (Settings ->
    # General) wins, like every other env-seeded setting. See app_settings.get_timezone.
    timezone: str = ""

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

    # --- Reverse proxy --------------------------------------------------------
    # First-boot seed for reverse-proxy trust: whether forwarded headers
    # (X-Forwarded-For) are believed at all, and from which proxy addresses. Both are
    # OFF/empty by default, so an unconfigured install ignores forwarded headers
    # entirely -- a header from an untrusted peer is attacker-controlled and would let a
    # stranger spoof the address the login lockout keys on. These only seed the FIRST-RUN
    # default; after that the stored value (Settings -> General) wins, exactly like the
    # deletion switch, so a declarative deployment can ship trust configured while the UI
    # stays the live control. REAPER_TRUSTED_PROXIES is comma- or space-separated (single
    # addresses or CIDR ranges); an entry that does not parse is dropped downstream,
    # trusting nobody extra. See ``app_settings.proxy_trust_enabled`` / ``get_trusted_proxies``.
    proxy_trust_enabled: bool = False
    trusted_proxies: str = ""

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

    # Writing the "Leaving Soon" shelf (a Plex collection plus label) is a mutation,
    # but a benign one: reversible, and it touches no files. Its whole purpose is to
    # WARN users during the grace countdown -- which is precisely when Reaper is
    # unarmed. Gated like a delete by default (so it writes only when deletion is
    # enabled). This is the FIRST-RUN default only: after first boot the stored value
    # (Settings -> Plex -> "Update while read-only") is the source of truth, exactly
    # like the deletion switch above. Exposing it in the UI is safe because the guard
    # confines it structurally to label and collection edits; it can never widen what
    # may be deleted.
    allow_unarmed_leaving_soon: bool = False

    # --- Anti-lockout ---------------------------------------------------------
    # Set REAPER_RECOVERY=true and restart: Reaper mints a single-use, 15-minute sign-in
    # code, prints it to the console, and writes it to recovery.txt in the data folder
    # (auth.recovery). Two channels because the console is not one on a windowed desktop
    # build. Requires the ability to set an env var and read either -- i.e. host access --
    # so it grants nothing an attacker who already has the host doesn't have.
    recovery: bool = False

    @field_validator("data_dir")
    @classmethod
    def _resolve_data_dir(cls, v: Path) -> Path:
        return v.expanduser().resolve()

    def ensure_data_dir(self) -> Path:
        """Both the app and Alembic need this before opening the database.

        ``mkdir(exist_ok=True)`` succeeds on an already-present mount even when it is
        not writable, so the failure would otherwise surface much later as SQLite's
        opaque ``unable to open database file``. Probe write access here -- with a
        temp file that leaves nothing behind -- and fail with a plain, actionable
        message (see ``DataDirError``) instead.
        """
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            # TemporaryFile is unlinked immediately on POSIX, so a crash cannot strand
            # a probe file, and its unique name cannot collide with a real one.
            with tempfile.TemporaryFile(dir=self.data_dir):
                pass
        except OSError as exc:
            raise DataDirError(self.data_dir, exc) from exc
        return self.data_dir

    @property
    def database_path(self) -> Path:
        """Where the precious database lives.

        The two URLs below are two drivers reading this one file, and the boot schema gate
        (``reaper.db.schema_gate``) opens it directly with ``sqlite3`` before either engine
        exists. All three read :data:`DATABASE_FILENAME`, which is the one declaration of
        the name (rule 104), so does every caller holding only a bare ``data_dir`` and no
        ``Settings`` -- ``services/retention.py``'s compaction is the one of those.
        """
        return self.data_dir / DATABASE_FILENAME

    @property
    def database_url(self) -> str:
        """Reaper's own state: policies, candidates, audit, credentials.

        Small, precious, migrated by Alembic. Losing it loses your decisions.
        """
        return f"sqlite+aiosqlite:///{self.database_path}"

    @property
    def sync_database_url(self) -> str:
        """Alembic runs migrations synchronously."""
        return f"sqlite:///{self.database_path}"

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


def configured_env() -> dict[str, str]:
    """The full environment, as ``Settings`` sees it: the dotenv files under ``os.environ``.

    Variables in a ``.env`` file are read by pydantic-settings into ``Settings``;
    they are **not** exported into ``os.environ``. So anything reading ``os.environ``
    directly is blind to a ``.env.local``, and reads as configured while doing nothing --
    which is what four documented desktop keys and the launcher's own port did (#558).
    **Every reader of an operator-settable key goes through this or through a ``Settings``
    field**, never through ``os.environ`` alone.

    Precedence matches pydantic-settings: a real environment variable wins over
    the file, and a later file wins over an earlier one.

    ``Settings.model_config`` rather than an instance, because the env-file tuple is
    declared on the class. That also keeps this honest under ``tests/conftest.py``'s
    ``_hermetic``, which clears that key so no test reads the developer's dotenv: the same
    clearing empties this, so the two cannot drift apart.
    """
    merged: dict[str, str] = {}
    env_files = Settings.model_config.get("env_file") or ()
    if isinstance(env_files, str | Path):
        env_files = (env_files,)

    for env_file in env_files:
        path = Path(env_file)
        if path.is_file():
            merged.update({k: v for k, v in dotenv_values(path).items() if v is not None})

    merged.update(os.environ)
    return merged


def load_raw_env(settings: Settings) -> dict[str, str]:
    """The full environment, as the seeder sees it.

    The seed keys are dynamic (``REAPER_SONARR_4K_URL`` and friends) so they never become
    ``Settings`` fields, which is why the seeder needs the merged mapping rather than the
    model. ``settings`` is kept in the signature because the seeding call site reads as a
    function of the install it is seeding.
    """
    return configured_env()


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


def parse_trusted_proxies(raw: str) -> list[str]:
    """Split the ``REAPER_TRUSTED_PROXIES`` seed into individual entries.

    Comma- or whitespace-separated, mirroring how ``REAPER_SECRET_KEY_OLD`` lists a
    chain. Blank entries are dropped. The entries are NOT validated here -- that is the
    ``auth.proxy.parse_proxy_networks``, which drops anything malformed (fail closed:
    an unparseable entry trusts nobody extra).
    """
    return [part.strip() for part in re.split(r"[,\s]+", raw) if part.strip()]


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

    ``recovery_mode`` overrides it downward and can never raise it. Recovery mode is a door
    that opens for anyone who can reach the host, so a boot with it armed is the last moment
    Reaper should also be able to delete media. It is not a second switch to forget: the flag
    is read from the environment on every boot, so turning it off restores whatever the
    database said -- and the boot with it ON has already forced the stored switch off
    (``main.lifespan``), so getting back in never silently re-arms anything.
    """

    destructive_enabled: bool = Field(
        default=False,
        description="May Reaper delete right now? Turned on in the UI, password-gated.",
    )

    recovery_mode: bool = Field(
        default=False,
        description="REAPER_RECOVERY is armed, which holds deletion off however it is set.",
    )

    # Permit the benign "Leaving Soon" shelf write (collection + label) while deletion is
    # off. It never widens what can be *deleted* -- file deletions still require
    # destructive_enabled AND a journalled declaration. Assembled from the stored setting
    # (Settings -> Plex), which the environment variable only seeds on first run; see
    # app_settings.leaving_soon_unarmed.
    allow_leaving_soon_unarmed: bool = Field(
        default=False,
        description="Settings -> Plex -> Update while read-only (env-seeded)",
    )

    @property
    def destructive_allowed(self) -> bool:
        # One place, so every consumer inherits the hold: the transport guard, the executor's
        # interlocks, the planner, and the banner all read this rather than the raw switch.
        return self.destructive_enabled and not self.recovery_mode

    @property
    def leaving_soon_write_allowed(self) -> bool:
        """May the benign Leaving Soon shelf (collection + label) be written now?

        When deletion is on, yes (it is at least as safe as a delete). When not, only if
        the operator turned on "Update while read-only" in Settings -> Plex.
        """
        return self.destructive_allowed or self.allow_leaving_soon_unarmed

    def why_blocked(self) -> str | None:
        # Recovery first: it is the reason that the operator cannot clear from the UI, so
        # naming the other one would send them to a switch that will not help.
        if self.recovery_mode:
            return (
                "Recovery mode is on, so Reaper can look but can't remove anything. "
                "Turn it off and restart."
            )
        if not self.destructive_enabled:
            return (
                "Deletion is turned off, so Reaper can look but can't remove anything. "
                "Turn it on in Policy -> Deletion when you're ready."
            )
        return None
