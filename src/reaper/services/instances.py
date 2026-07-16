# SPDX-License-Identifier: AGPL-3.0-or-later
"""Managing the external services Reaper reads from, in the web UI.

Sonarr, Radarr, Tautulli and Seerr are configured here rather than only in the
environment. The environment seed (``services.seeding``) is a first-boot convenience;
once an instance exists, the database is the source of truth and this is where it is
edited.

Two rules run through everything below:

* **The API key is write-only.** It is Fernet-encrypted the moment it arrives and is
  never read back out to the browser. A view of an instance says *whether* a key is set,
  never what it is. Updating without a new key keeps the stored one.
* **A connection test only ever reads.** It builds a client with destructive actions
  disabled and calls the one status endpoint each service offers, so "does this URL and
  key work?" can be answered before anything relies on it -- and answered honestly, with
  the reason it failed.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clients.arr import RadarrClient, SonarrClient
from reaper.clients.base import BaseClient, IntegrationError
from reaper.clients.seerr import SeerrClient
from reaper.clients.tautulli import TautulliClient
from reaper.clock import utcnow
from reaper.config import RuntimeSafety
from reaper.crypto import SecretBox
from reaper.db.models import Instance, InstanceKind

log = structlog.get_logger(__name__)


class InstanceError(RuntimeError):
    """A configuration change could not be applied (e.g. a duplicate name)."""


class InstanceNotFoundError(InstanceError):
    """The referenced instance does not exist -- the caller should see a 404."""


class InstanceConflictError(InstanceError):
    """The change collides with an existing instance (a duplicate name) -- a 409, not a
    404: the request was well-formed and the target exists, it just cannot be applied."""


@dataclass(frozen=True)
class InstanceView:
    """One configured instance, safe to send to the browser -- no key, ever."""

    id: int
    kind: str
    name: str
    base_url: str
    enabled: bool
    verify_tls: bool
    has_key: bool
    api_path_prefix: str
    detected_version: str | None
    last_ok_at: str | None
    last_error: str | None


@dataclass(frozen=True)
class TestResult:
    ok: bool
    detail: str
    version: str | None = None


def _view(row: Instance) -> InstanceView:
    return InstanceView(
        id=row.id,
        kind=str(row.kind),
        name=row.name,
        base_url=row.base_url,
        enabled=row.enabled,
        verify_tls=row.verify_tls,
        has_key=bool(row.api_key_enc),
        api_path_prefix=row.api_path_prefix,
        detected_version=row.detected_version,
        last_ok_at=row.last_ok_at.isoformat() if row.last_ok_at else None,
        last_error=row.last_error,
    )


async def list_instances(session: AsyncSession) -> list[InstanceView]:
    rows = (
        (await session.execute(select(Instance).order_by(Instance.kind, Instance.name)))
        .scalars()
        .all()
    )
    return [_view(r) for r in rows]


async def _get(session: AsyncSession, instance_id: int) -> Instance:
    row = await session.get(Instance, instance_id)
    if row is None:
        raise InstanceNotFoundError("No such instance.")
    return row


async def create_instance(
    session: AsyncSession,
    box: SecretBox,
    *,
    kind: InstanceKind,
    name: str,
    base_url: str,
    api_key: str,
    verify_tls: bool = True,
) -> InstanceView:
    name = name.strip()
    base_url = base_url.strip().rstrip("/")
    if not name or not base_url or not api_key:
        raise InstanceError("A name, a URL and an API key are all required.")

    clash = await session.scalar(
        select(Instance).where(Instance.kind == kind, Instance.name == name)
    )
    if clash is not None:
        raise InstanceConflictError(f"A {kind.value} instance named {name!r} already exists.")

    row = Instance(
        kind=kind,
        name=name,
        base_url=base_url,
        api_key_enc=box.encrypt(api_key),
        enabled=True,
        verify_tls=verify_tls,
        created_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    log.info("instance.created", kind=kind.value, name=name)
    return _view(row)


async def update_instance(
    session: AsyncSession,
    box: SecretBox,
    instance_id: int,
    *,
    name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    enabled: bool | None = None,
    verify_tls: bool | None = None,
) -> InstanceView:
    """Update an instance. An omitted (or blank) ``api_key`` keeps the stored one.

    The key is write-only: the browser cannot read it back, so "no new key" must mean
    "leave it alone", never "clear it". Clearing a key would silently break the next scan.
    """
    row = await _get(session, instance_id)

    if name is not None and name.strip():
        new_name = name.strip()
        if new_name != row.name:
            clash = await session.scalar(
                select(Instance).where(
                    Instance.kind == row.kind,
                    Instance.name == new_name,
                    Instance.id != row.id,
                )
            )
            if clash is not None:
                raise InstanceConflictError(
                    f"A {row.kind.value} instance named {new_name!r} already exists."
                )
        row.name = new_name
    if base_url is not None and base_url.strip():
        row.base_url = base_url.strip().rstrip("/")
    if api_key:  # a blank/omitted key means "keep the existing one"
        row.api_key_enc = box.encrypt(api_key)
    if enabled is not None:
        row.enabled = enabled
    if verify_tls is not None:  # None means "leave it as it is"; an explicit False sticks
        row.verify_tls = verify_tls

    await session.flush()
    log.info("instance.updated", kind=row.kind.value, name=row.name)
    return _view(row)


async def delete_instance(session: AsyncSession, instance_id: int) -> bool:
    row = await session.get(Instance, instance_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    log.info("instance.deleted", id=instance_id)
    return True


# ---------------------------------------------------------------------------
# Connection test -- read-only, and honest about failure
# ---------------------------------------------------------------------------


def _client(kind: InstanceKind, base_url: str, api_key: str, *, verify: bool = True) -> BaseClient:
    # Destructive actions disabled: a connection test never mutates anything. ``verify`` is
    # the instance's own TLS setting and defaults ON: the API key travels on this
    # connection, so skipping certificate verification is an explicit per-instance choice
    # the operator makes in Settings (a self-signed server they run themselves), never
    # something turned off on their behalf.
    safety = RuntimeSafety(destructive_enabled=False)
    base_url = base_url.strip().rstrip("/")
    if kind is InstanceKind.RADARR:
        return RadarrClient(base_url, api_key, safety=safety, verify=verify)
    if kind is InstanceKind.SONARR:
        return SonarrClient(base_url, api_key, safety=safety, verify=verify)
    if kind is InstanceKind.TAUTULLI:
        return TautulliClient(base_url, api_key, safety=safety, verify=verify)
    return SeerrClient(base_url, api_key, safety=safety, verify=verify)


async def test_connection(
    kind: InstanceKind, base_url: str, api_key: str, *, verify: bool = True
) -> TestResult:
    """Reach the service and report what came back. Never raises -- a failure is a result.

    Each service has one cheap status endpoint. For the *arr it doubles as the version
    probe (Reaper version-gates its API path off it); for Tautulli and Seerr it just
    proves the URL and key are good.
    """
    try:
        client = _client(kind, base_url, api_key, verify=verify)
    except Exception as exc:  # a malformed URL, say
        return TestResult(ok=False, detail=str(exc))

    try:
        async with client:
            if kind in (InstanceKind.RADARR, InstanceKind.SONARR):
                status = await client.system_status()  # type: ignore[attr-defined]
                version = str(status.get("version") or "") or None
                app = str(status.get("appName") or kind.value).strip()
                return TestResult(ok=True, detail=f"Connected to {app}.", version=version)
            if kind is InstanceKind.TAUTULLI:
                info = await client.server_info()  # type: ignore[attr-defined]
                name = str(info.get("pms_name") or "Plex").strip()
                return TestResult(ok=True, detail=f"Connected. Watching {name}.")
            status = await client.status()  # type: ignore[attr-defined]
            version = str(status.get("version") or "") or None
            return TestResult(ok=True, detail="Connected to Seerr.", version=version)
    except IntegrationError as exc:
        return TestResult(ok=False, detail=str(exc))
    except Exception as exc:  # network/TLS/timeout -- report, don't crash the request
        return TestResult(ok=False, detail=f"{type(exc).__name__}: {exc}")


async def test_saved_instance(
    session: AsyncSession, box: SecretBox, instance_id: int
) -> TestResult:
    """Test a stored instance and record the outcome on the row (last_ok_at / last_error)."""
    row = await _get(session, instance_id)
    result = await test_connection(
        row.kind, row.base_url, box.decrypt(row.api_key_enc), verify=row.verify_tls
    )
    if result.ok:
        row.last_ok_at = utcnow()
        row.last_error = None
        if result.version:
            row.detected_version = result.version
    else:
        row.last_error = result.detail
    await session.flush()
    return result
