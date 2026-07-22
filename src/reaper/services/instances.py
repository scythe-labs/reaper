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

import json
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clients.arr import RadarrClient, SonarrClient
from reaper.clients.base import BaseClient, IntegrationError
from reaper.clients.seerr import SeerrClient, SeerrService
from reaper.clients.tautulli import TautulliClient
from reaper.clock import utcnow
from reaper.config import RuntimeSafety
from reaper.crypto import SecretBox
from reaper.db.models import Instance, InstanceKind
from reaper.engine import identity

log = structlog.get_logger(__name__)

#: What each service is called in operator-facing copy: the name on its own web UI, not
#: the internal enum value.
_KIND_LABEL: dict[InstanceKind, str] = {
    InstanceKind.SONARR: "Sonarr",
    InstanceKind.RADARR: "Radarr",
    InstanceKind.TAUTULLI: "Tautulli",
    InstanceKind.SEERR: "Seerr",
}

#: Kinds that may only ever have one instance. Radarr, Sonarr and Seerr are genuinely
#: multi (an HD plus a 4K server, two request portals) and every reader merges them.
#: Tautulli is not: it mirrors ONE Plex server's watch history keyed by that server's
#: rating keys, and Reaper connects to exactly one Plex (a single-row invariant). A second
#: Tautulli could only double-count the same server's history or carry rating keys from a
#: Plex Reaper never reads, so it is refused at creation rather than silently ignored by the
#: scan. Enforced in two places that both create instances: :func:`create_instance` (the
#: UI) and ``services.seeding.seed_instances`` (the environment); the scan then reads the
#: one Tautulli as a singleton safely.
SINGLETON_KINDS: frozenset[InstanceKind] = frozenset({InstanceKind.TAUTULLI})


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
    add_import_exclusion: bool
    plex_library_map: dict[str, str]
    service_instance_map: dict[str, int]
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


def decode_library_map(raw: str | None) -> dict[str, str]:
    """The stored HD/4K library map as a ``{root folder: Plex library}`` dict, or ``{}``.

    A missing, malformed, or wrong-typed body reads as an empty map -- never a crash, and
    never a partial guess. A bad map must be exactly as harmless as an absent one: the
    resolver reads ``{}`` as "no library declared" and keeps a duplicated title (rule 32,
    a stored config must not be able to crash a scan).
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k.strip(): v.strip()
        for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
    }


def _shared_suffix(left: Sequence[str], right: Sequence[str]) -> int:
    """How many trailing path segments two paths share, compared from the leaf back."""
    depth = 0
    for a, b in zip(reversed(left), reversed(right), strict=False):
        if a != b:
            break
        depth += 1
    return depth


def suggest_library(root_path: str, section_paths: Mapping[str, Sequence[str]]) -> str | None:
    """The Plex library a root folder most likely lands in, or ``None`` for "cannot tell".

    A *prefill only* -- the operator confirms it in Settings, so this may guess where the
    resolver never would. ``section_paths`` is ``{library title: [that library's folder paths]}``.
    Each library is scored by the deepest trailing-segment overlap between the root folder and
    any of its folders (the same mount-root-agnostic comparison the resolver uses: ``/tv-4k``
    and Plex's ``/media/tv-4k`` share the ``tv-4k`` leaf). The strictly-deepest library wins; a
    tie or no overlap suggests nothing, so the operator picks rather than get a coin-flip.
    """
    root_segs = identity.to_segments(root_path)
    if not root_segs:
        return None
    best_title: str | None = None
    best_depth = 0
    tie = False
    for title, folders in section_paths.items():
        depth = max(
            (_shared_suffix(root_segs, identity.to_segments(folder)) for folder in folders),
            default=0,
        )
        if depth > best_depth:
            best_depth, best_title, tie = depth, title, False
        elif depth == best_depth and depth > 0:
            tie = True
    return None if best_depth == 0 or tie else best_title


def _encode_library_map(mapping: Mapping[str, str] | None) -> str | None:
    """The map as a stored JSON string, or ``None`` to store SQL NULL (no map).

    Blank keys and values are dropped, and an empty result stores NULL rather than ``"{}"``
    so "no mappings" is one state, not two -- the resolver and :func:`decode_library_map`
    both read NULL and ``{}`` identically as "no map".
    """
    if not mapping:
        return None
    cleaned = {
        k.strip(): v.strip()
        for k, v in mapping.items()
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
    }
    return json.dumps(cleaned, sort_keys=True) if cleaned else None


def decode_service_instance_map(raw: str | None) -> dict[str, int]:
    """The stored map as ``{Seerr service id: Reaper instance id}``, or ``{}``.

    Keys are the Seerr portal's own service ids as strings (JSON object keys are strings);
    values are the Reaper Sonarr/Radarr instance id each service adds media to. A missing,
    malformed, or wrong-typed body reads as an empty map -- never a crash, and exactly as
    harmless as an absent one: ``build_map`` then falls back to the loose tmdb/tvdb union, which
    is today's behavior (rule 32, a stored config must not be able to crash a scan). A value
    that is not a positive int is dropped, since it can never match a real instance id.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in data.items():
        if not (isinstance(k, str) and k.strip()):
            continue
        # bool is an int subclass; a JSON true/false is never a real instance id, so exclude it.
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            continue
        out[k.strip()] = v
    return out


def _encode_service_instance_map(mapping: Mapping[str, int] | None) -> str | None:
    """The map as a stored JSON string, or ``None`` to store SQL NULL (no map).

    Blank keys and non-positive-int values are dropped, and an empty result stores NULL rather
    than ``"{}"`` so "no mappings" is one state, not two -- :func:`decode_service_instance_map`
    reads NULL and ``{}`` identically as "no map".
    """
    if not mapping:
        return None
    cleaned = {
        k.strip(): v
        for k, v in mapping.items()
        if isinstance(k, str)
        and k.strip()
        and not isinstance(v, bool)
        and isinstance(v, int)
        and v > 0
    }
    return json.dumps(cleaned, sort_keys=True) if cleaned else None


def _view(row: Instance) -> InstanceView:
    return InstanceView(
        id=row.id,
        kind=str(row.kind),
        name=row.name,
        base_url=row.base_url,
        enabled=row.enabled,
        verify_tls=row.verify_tls,
        add_import_exclusion=row.add_import_exclusion,
        plex_library_map=decode_library_map(row.plex_library_map),
        service_instance_map=decode_service_instance_map(row.service_instance_map),
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
    add_import_exclusion: bool = False,
) -> InstanceView:
    name = name.strip()
    base_url = base_url.strip().rstrip("/")
    if not name or not base_url or not api_key:
        raise InstanceError("A name, a URL and an API key are all required.")

    if kind in SINGLETON_KINDS:
        existing = await session.scalar(select(Instance).where(Instance.kind == kind))
        if existing is not None:
            raise InstanceConflictError(
                f"Reaper uses one {_KIND_LABEL.get(kind, 'service')}. It reads a single "
                "Plex server's watch history, and Reaper connects to one Plex. Edit the "
                "one you have, or remove it and add a different one."
            )

    clash = await session.scalar(
        select(Instance).where(Instance.kind == kind, Instance.name == name)
    )
    if clash is not None:
        raise InstanceConflictError(
            f'A {_KIND_LABEL.get(kind, "service")} connection named "{name}" already exists.'
        )

    row = Instance(
        kind=kind,
        name=name,
        base_url=base_url,
        api_key_enc=box.encrypt(api_key),
        enabled=True,
        verify_tls=verify_tls,
        add_import_exclusion=add_import_exclusion,
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
    add_import_exclusion: bool | None = None,
    plex_library_map: Mapping[str, str] | None = None,
    service_instance_map: Mapping[str, int] | None = None,
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
                    f"A {_KIND_LABEL.get(row.kind, 'service')} connection named "
                    f'"{new_name}" already exists.'
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
    if add_import_exclusion is not None:  # None keeps the stored value; explicit False sticks
        row.add_import_exclusion = add_import_exclusion
    if plex_library_map is not None:  # a dict (even empty) replaces; None keeps the stored map
        # An empty dict clears the map to NULL, so "the operator removed every mapping" and
        # "there was never a map" are the one state the resolver reads as "no library declared".
        row.plex_library_map = _encode_library_map(plex_library_map)
    if service_instance_map is not None:  # a dict (even empty) replaces; None keeps the stored map
        # An empty dict clears the map to NULL, so "removed every mapping" and "never had one"
        # are the one state build_map reads as "no map" (fall back to the tmdb/tvdb union).
        row.service_instance_map = _encode_service_instance_map(service_instance_map)

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

#: Shown when nothing in the chain below recognizes the failure. Never a bare class name:
#: "ConnectError: All connection attempts failed" is the first thing a new operator sees
#: if a URL is wrong, and it teaches them nothing about what to change.
_GENERIC_FAILURE = "Couldn't connect. The full reason is in Reaper's log."


def _causes(exc: BaseException) -> list[BaseException]:
    """``exc`` and everything it was raised from, outermost first.

    The client layer maps transport failures to :class:`IntegrationError` with
    ``raise ... from exc``, so the original httpx (and, under it, the ssl) exception is
    still reachable. Keying the operator message on those *types* beats sniffing the
    text of a message that upstream is free to reword.
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


#: OpenSSL verify codes where "the certificate signs for itself, or for an authority this
#: machine doesn't know" is the whole story: self-signed, or an issuer that cannot be
#: reached. Only these are safe to answer with "turn off the certificate check" -- an
#: expired certificate, a name that doesn't match, or something intercepting the
#: connection are all real failures that turning the check off would hide.
_SELF_SIGNED_VERIFY_CODES = frozenset(
    {
        2,  # unable to get issuer certificate
        18,  # self-signed certificate
        19,  # self-signed certificate in the chain
        20,  # unable to get local issuer certificate
        21,  # unable to verify the first certificate
    }
)


def _self_signed(chain: list[BaseException]) -> bool:
    """True only for the unknown-authority family, which the operator can safely wave off.

    Anything else -- an expired certificate, a certificate for a different address, a
    handshake failure, or a missing ``verify_code`` -- is treated as not-self-signed, so
    the advice to skip verification is never offered for a failure it would paper over.
    The API key travels on this connection.
    """
    return any(
        isinstance(e, ssl.SSLCertVerificationError)
        and getattr(e, "verify_code", None) in _SELF_SIGNED_VERIFY_CODES
        for e in chain
    )


def _explain_failure(kind: InstanceKind, exc: BaseException) -> str:
    """One plain sentence an operator can act on, for the families we can recognize.

    Everything else falls through to :data:`_GENERIC_FAILURE`; the raw exception is
    logged by the caller either way, so nothing is lost, it just is not put in front of
    someone who is only trying to get a URL and a key right.

    **Branch order is load-bearing** and is pinned by ``tests/test_instances.py``:
    certificate failures must be read before the transport families (they arrive wrapped
    in a ``ConnectError``) and before the ``ValueError`` body branch
    (``ssl.SSLCertVerificationError`` is itself a ``ValueError``), and the
    ``IntegrationError``-without-a-status branch must come last of all, because every
    transport failure is also wrapped in one.
    """
    chain = _causes(exc)
    label = _KIND_LABEL.get(kind, "The server")

    # Certificate problems first: they surface as a ConnectError, so the transport
    # branch below would otherwise swallow the one detail that names the fix.
    if any(isinstance(e, ssl.SSLError) for e in chain):
        if _self_signed(chain):
            return (
                "The server's certificate is signed by an authority this machine doesn't "
                "know. Only turn off the certificate check if this is your own server on "
                "your own network: your API key travels on this connection."
            )
        return (
            "The server's certificate was rejected. It may have expired, or be for a "
            "different address, or something may be sitting between Reaper and the server."
        )

    status = exc.status if isinstance(exc, IntegrationError) else None
    if status is not None:
        if status in (401, 403):
            return f"{label} refused the API key. Copy it again from its own settings."
        if status == 404:
            return (
                f"{label} answered, but there is nothing at this address. Check for a "
                "missing or extra path at the end of the URL."
            )
        if status == 429:
            return f"{label} asked Reaper to slow down. Wait a moment and test again."
        if 300 <= status < 400:
            return (
                "The server sent Reaper somewhere else, and Reaper won't send your API "
                "key to a different address. Check the URL and anything proxying it."
            )
        if status >= 500:
            return f"{label} reported a problem of its own (HTTP {status}). Check its log."
        return f"{label} refused the request (HTTP {status})."

    if any(isinstance(e, httpx.TimeoutException) for e in chain):
        if any(isinstance(e, httpx.ConnectTimeout | httpx.PoolTimeout) for e in chain):
            return "Couldn't open a connection to the server in time."
        return "The server didn't answer in time."
    if any(isinstance(e, httpx.UnsupportedProtocol | httpx.InvalidURL) for e in chain):
        return "That isn't an address Reaper can use. Start it with http:// or https://."
    if any(isinstance(e, httpx.ConnectError | httpx.ProxyError) for e in chain):
        return (
            "Couldn't reach the server at this address. Check the URL and port, and that "
            "the service is running."
        )
    if any(isinstance(e, httpx.TransportError) for e in chain):
        return "The connection to the server broke before it answered."
    if any(isinstance(e, ValueError) for e in chain):
        # A body that would not parse: usually a login page or a proxy error page.
        return f"The address answered, but not with data from {label}. Check the URL."
    if isinstance(exc, IntegrationError):
        # The server answered and reported a problem of its own, with no HTTP status to
        # go on. This is the commonest Tautulli misconfiguration: it answers a bad API
        # key with a normal HTTP 200 whose body says the request failed. It also covers
        # an answer in a shape Reaper could not use, and a URL that redirects in a loop.
        # The raw text stays in the log; the key and the URL are what an operator can act
        # on.
        return (
            f"{label} answered, but turned the request down. Check the API key first, then the URL."
        )
    return _GENERIC_FAILURE


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

    Each service is probed with cheap reads that also exercise the key. For the *arr the
    status endpoint doubles as the version probe (Reaper version-gates its API path off
    it) and carries the key; Tautulli's status read carries its key too. Seerr's
    ``/status`` is public -- it proves the URL and gives the version but would pass with a
    wrong key -- so Seerr is probed a second time on an authenticated route, and that
    probe is what actually confirms the key.
    """
    try:
        client = _client(kind, base_url, api_key, verify=verify)
    except Exception as exc:  # a malformed URL, say
        log.warning("instance.test_failed", kind=kind.value, stage="build", error=str(exc))
        return TestResult(ok=False, detail=_explain_failure(kind, exc))

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
            # /status needs no key, so it passes even with a wrong one. Probe an
            # authenticated route too, so a rejected key fails the test here instead of
            # going quiet and surfacing later as a scan warning with the requester signal
            # dark. A 401/403 lands in _explain_failure's key-refused branch.
            await client.requests(take=1)  # type: ignore[attr-defined]
            return TestResult(ok=True, detail="Connected to Seerr.", version=version)
    except Exception as exc:  # network/TLS/timeout/HTTP -- report, don't crash the request
        # The raw exception stays here, in the log, where a diagnosis needs it. What the
        # operator is shown is the plain-language translation.
        log.warning(
            "instance.test_failed",
            kind=kind.value,
            stage="request",
            error=f"{type(exc).__name__}: {exc}",
        )
        return TestResult(ok=False, detail=_explain_failure(kind, exc))


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


@dataclass(frozen=True)
class RootFolderSuggestion:
    """One of an *arr instance's root folders, with a suggested Plex library to prefill."""

    path: str
    suggested_library: str | None


async def instance_root_folders(
    session: AsyncSession,
    box: SecretBox,
    instance_id: int,
    *,
    section_paths: Mapping[str, Sequence[str]],
) -> list[RootFolderSuggestion]:
    """This instance's root folders, each with a suggested Plex library (a prefill only).

    Sonarr/Radarr only -- Tautulli and Seerr have no root folders and no library map.
    The live ``/rootfolder`` read exercises the stored key; ``section_paths`` is the Plex side
    (``{library title: folder paths}``), passed in by the caller so this function needs no
    Plex client of its own. Raises ``InstanceError`` for a wrong kind or a missing instance,
    and lets the arr client's error surface for a connection failure (the caller maps it).
    """
    row = await _get(session, instance_id)
    if row.kind not in (InstanceKind.SONARR, InstanceKind.RADARR):
        raise InstanceError("Only Sonarr and Radarr have root folders to map to a Plex library.")
    client = _client(row.kind, row.base_url, box.decrypt(row.api_key_enc), verify=row.verify_tls)
    async with client:
        payload = await client.root_folders()  # type: ignore[attr-defined]
    return [
        RootFolderSuggestion(path=p, suggested_library=suggest_library(p, section_paths))
        for p in identity.root_folder_paths(payload)
    ]


@dataclass(frozen=True)
class ServiceInstanceSuggestion:
    """One Sonarr/Radarr service on a Seerr portal, with a suggested Reaper instance to map to."""

    service_id: int
    kind: str  # "sonarr" | "radarr"
    name: str
    is_4k: bool
    suggested_instance_id: int | None


def _host_port(url: str) -> tuple[str, int]:
    """A URL's ``(lowercased host, port)`` with the scheme's default port filled in."""
    parts = urlsplit(url if "://" in url else f"//{url}", scheme="http")
    host = (parts.hostname or "").strip().lower()
    port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
    return host, port


def _suggest_instance(service: SeerrService, arr_rows: Sequence[Instance]) -> int | None:
    """The Reaper instance whose address matches this Seerr service, or ``None``.

    A prefill only: matched on kind + host + port, and suggested only when EXACTLY ONE Reaper
    instance matches, so an ambiguous or missing match leaves the operator to pick rather than
    get a wrong default. Seerr and Reaper often reach the same server at different addresses
    (a container hostname vs a LAN ip), so a miss here is expected, not an error.
    """
    if not service.hostname or service.port is None:
        return None
    want_kind = InstanceKind.SONARR if service.kind == "sonarr" else InstanceKind.RADARR
    svc_host = service.hostname.strip().lower()
    matches = [
        r.id
        for r in arr_rows
        if r.kind is want_kind and _host_port(r.base_url) == (svc_host, service.port)
    ]
    return matches[0] if len(matches) == 1 else None


async def seerr_services(
    session: AsyncSession,
    box: SecretBox,
    instance_id: int,
) -> list[ServiceInstanceSuggestion]:
    """This Seerr portal's Sonarr/Radarr services, each with a suggested Reaper instance.

    Seerr only. The live ``/settings`` read exercises the stored (admin) key. The suggestion
    matches the service's own host:port to a Reaper Sonarr/Radarr instance -- a prefill only,
    since the two apps may reach the same server at different addresses, so the operator
    confirms. Raises ``InstanceError`` for a wrong kind or a missing instance, and lets the Seerr
    client's error surface for a connection or non-admin-key failure (the caller maps it).
    """
    row = await _get(session, instance_id)
    if row.kind is not InstanceKind.SEERR:
        raise InstanceError("Only Seerr portals have request services to map to an instance.")
    arr_rows = (
        await session.scalars(
            select(Instance).where(Instance.kind.in_((InstanceKind.SONARR, InstanceKind.RADARR)))
        )
    ).all()
    client = _client(row.kind, row.base_url, box.decrypt(row.api_key_enc), verify=row.verify_tls)
    async with client:
        services = await client.services()  # type: ignore[attr-defined]
    return [
        ServiceInstanceSuggestion(
            service_id=s.service_id,
            kind=s.kind,
            name=s.name,
            is_4k=s.is_4k,
            suggested_instance_id=_suggest_instance(s, arr_rows),
        )
        for s in services
    ]
