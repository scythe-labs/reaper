# SPDX-License-Identifier: AGPL-3.0-or-later
"""Linking the Plex server, without anyone pasting a token.

The owner signs in on plex.tv itself, so Reaper never sees a password. What it gets
back is an **account token**, and that is worth being blunt about:

    Verified against a live account: ``resource.accessToken == account.authToken``.

The per-resource ``accessToken`` that plex.tv hands out for an *owned* server is **the
same string** as the account credential. It is not narrower, not scoped to that server,
and not a mitigation of any kind. Reaper stores a credential with full administrative
control of the Plex account -- including permanent deletion -- and the README says so
in exactly those words rather than implying a boundary that does not exist.

We store it anyway, because it is what ``plexapi.connect()`` uses and there is no
narrower credential on offer. What we do *not* do is pretend.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clients.base import IntegrationError
from reaper.clients.plextv import (
    PlexAccount,
    PlexConnection,
    PlexResource,
    PlexTvClient,
    probe_connection,
)
from reaper.clock import expiry, utcnow
from reaper.config import RuntimeSafety
from reaper.crypto import SecretBox
from reaper.db.models import AppSetting, PendingPlexLogin, PlexServer

log = structlog.get_logger(__name__)

CLIENT_ID_KEY = "plex_client_identifier"

#: How long a browser has to finish an in-app (re-)link before its PIN expires.
LINK_TTL = timedelta(minutes=10)


class PlexLinkError(RuntimeError):
    """The link could not be completed."""


class PlexLinkRetryableError(PlexLinkError):
    """A *transient* link failure: the sign-in succeeded and the PIN is still valid.

    Distinct from the permanent refusals (owns no server / owns several). It means the
    reachable-connection probe momentarily failed -- the server was mid-restart, a network
    blip, a relay hiccup -- none of which invalidate the sign-in. Treated separately so the
    in-app poll does not burn the pending PIN over it and force the owner through a fresh
    OAuth round-trip despite having already authenticated. A subclass of ``PlexLinkError``
    so existing ``except PlexLinkError`` callers (the CLI flow) still catch it.
    """


async def client_identifier(session: AsyncSession) -> str:
    """Reaper's stable identity to plex.tv. Generated once, then **never regenerated**.

    Not derived from the host. ``plexapi`` defaults this to ``hex(getnode())`` -- the
    machine's MAC address -- which is both unstable inside a container (so plex.tv sees
    a brand-new device on every redeploy, and the token can be invalidated) and a
    needless leak of host hardware. A persisted uuid4 is neither.
    """
    row = await session.get(AppSetting, CLIENT_ID_KEY)
    if row is not None:
        value: str = json.loads(row.value_json)
        return value

    generated = str(uuid.uuid4())
    session.add(
        AppSetting(
            key=CLIENT_ID_KEY,
            value_json=json.dumps(generated),
            updated_at=utcnow(),
        )
    )
    await session.flush()
    return generated


@dataclass(frozen=True)
class LinkedServer:
    name: str
    machine_identifier: str
    connection_uri: str
    local: bool
    relay: bool


async def _reachable(resource: PlexResource, token: str) -> PlexConnection:
    """Probe every connection and take the best one that answers.

    Ordered local/https, then local/http, then remote, then **relay last**: the relay is
    bandwidth-capped and proxied through Plex, so it is a fallback rather than a default.

    Every alternative is stored alongside the winner, so that a URI which stops working
    can be re-resolved later without dragging the owner back through OAuth.
    """
    for connection in resource.preferred_connections():
        if await probe_connection(connection, token):
            return connection

    # Retryable, deliberately: a server that answers none of its advertised addresses right
    # now may simply be restarting. The caller (poll_link) must not consume the PIN over
    # this, so the browser can re-poll the still-valid PIN once the server is back.
    raise PlexLinkRetryableError(
        f"Found your server ({resource.name!r}) but could not reach it on any of its "
        f"{len(resource.connections)} advertised addresses. Reaper has to talk to the "
        "server directly; check that it is running and reachable from this host."
    )


async def link(
    session_factory: async_sessionmaker[AsyncSession],
    box: SecretBox,
    *,
    safety: RuntimeSafety,
    on_prompt: object = None,
) -> LinkedServer:
    """Run the PIN flow end to end and persist the result.

    Takes a session *factory*, not a session, and holds a database transaction only for
    the two brief moments it needs one -- reading the client identifier at the start and
    writing the server row at the end. **The multi-minute wait for the human to sign in
    happens with no transaction open at all.**

    That is not tidiness. SQLite gives a writer the database, and an ``AsyncSession`` held
    open across ``wait_for_pin`` keeps a connection (and its lock) for up to five minutes
    -- long enough to block every other writer and, on a busy instance, to stall the app
    while someone fishes out their phone. Hold the lock for milliseconds, not minutes.

    ``on_prompt`` is called with the auth URL so the caller can render it -- the CLI
    prints it, the web UI opens it. The *backend* polls for the token; the browser never
    handles one. (Overseerr has the browser POST the authToken to its own API. Do not
    copy that: it puts a full account credential in a place a page script can read.)
    """
    # -- brief DB touch: our stable identity to plex.tv --------------------
    async with session_factory() as session:
        cid = await client_identifier(session)
        await session.commit()

    # -- no transaction held across any of this: it is all network + a human ----
    async with PlexTvClient(cid, safety=safety) as plextv:
        pin = await plextv.create_pin()

        if callable(on_prompt):
            on_prompt(pin.auth_url(cid))

        token = await plextv.wait_for_pin(pin.pin_id)
        if not token:
            raise PlexLinkError("Sign-in was not completed in time. Nothing was saved.")

        account = await plextv.account(token)
        owned = await plextv.owned_servers(token)

    return await complete_link(session_factory, box, token=token, account=account, owned=owned)


async def complete_link(
    session_factory: async_sessionmaker[AsyncSession],
    box: SecretBox,
    *,
    token: str,
    account: PlexAccount,
    owned: list[PlexResource],
) -> LinkedServer:
    """Turn a signed-in owner's discovered servers into a persisted link.

    Split out of :func:`link` so the web setup flow can reuse it: that flow runs
    its own poll-based PIN loop (the browser needs to be told to open the auth
    URL and then polled), but the *decision* -- refuse if they own no server or
    more than one, probe for a reachable connection, encrypt and persist -- is
    identical, and must not drift between the CLI and the UI.
    """
    if not owned:
        raise PlexLinkError(
            f"Signed in as {account.username!r}, but that account does not own a Plex "
            "server. Reaper must be linked by the server owner -- it is going to be "
            "given permission to delete media."
        )
    if len(owned) > 1:
        # Picking one arbitrarily is how you point a deletion tool at the wrong
        # library. Refuse, and make the caller choose.
        names = ", ".join(repr(r.name) for r in owned)
        raise PlexLinkError(
            f"This account owns more than one server ({names}). Reaper will not guess "
            "which one to point a deletion tool at -- select one explicitly."
        )

    resource = owned[0]
    connection = await _reachable(resource, resource.access_token or token)
    token_enc = box.encrypt(resource.access_token or token)

    # -- brief DB touch: persist the linked server -------------------------
    async with session_factory() as session:
        existing = (
            await session.execute(
                select(PlexServer).where(
                    PlexServer.machine_identifier == resource.client_identifier
                )
            )
        ).scalar_one_or_none()

        row = existing or PlexServer(
            machine_identifier=resource.client_identifier,
            created_at=utcnow(),
        )
        row.name = resource.name
        row.connection_uri = connection.uri
        row.connections_json = json.dumps(
            [
                {"uri": c.uri, "local": c.local, "relay": c.relay, "protocol": c.protocol}
                for c in resource.preferred_connections()
            ]
        )
        row.token_enc = token_enc
        row.owner_plex_account_id = account.account_id
        row.last_ok_at = utcnow()

        if existing is None:
            session.add(row)
        await session.commit()

    log.info("plex.linked", server=resource.name, local=connection.local, relay=connection.relay)
    return LinkedServer(
        name=resource.name,
        machine_identifier=resource.client_identifier,
        connection_uri=connection.uri,
        local=connection.local,
        relay=connection.relay,
    )


# ---------------------------------------------------------------------------
# In-app (re-)link: a browser-driven PIN flow for an already-signed-in admin
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkStart:
    pin_id: int
    auth_url: str


async def start_link(
    session_factory: async_sessionmaker[AsyncSession], *, safety: RuntimeSafety
) -> LinkStart:
    """Begin an in-app link: create a PIN and return the URL for the browser to open.

    This is the Settings-page twin of the setup flow. The backend polls; the browser
    never handles a token. Purpose ``"link"`` keeps these pendings distinct from the
    ``"login"`` ones, so a re-link cannot be mistaken for a sign-in.
    """
    async with session_factory() as session:
        cid = await client_identifier(session)
        await session.execute(
            delete(PendingPlexLogin).where(PendingPlexLogin.expires_at <= utcnow())
        )
        await session.commit()

    async with PlexTvClient(cid, safety=safety) as plextv:
        pin = await plextv.create_pin()

    async with session_factory() as session:
        session.add(
            PendingPlexLogin(
                pin_id=pin.pin_id,
                pin_code=pin.code,
                purpose="link",
                created_at=utcnow(),
                expires_at=expiry(LINK_TTL),
            )
        )
        await session.commit()

    return LinkStart(pin_id=pin.pin_id, auth_url=pin.auth_url(cid))


async def poll_link(
    session_factory: async_sessionmaker[AsyncSession],
    box: SecretBox,
    *,
    pin_id: int,
    safety: RuntimeSafety,
) -> LinkedServer | None:
    """Check an in-app link once. ``None`` while still pending; the linked server once done.

    Raises :class:`PlexLinkError` on a *permanent* refusal (owns no server, or owns several)
    and consumes the pending row so the obtained token cannot be replayed. A transient
    failure -- the reachable-connection probe not answering -- raises
    :class:`PlexLinkRetryableError` and leaves the pending row **intact**, so the browser can
    re-poll the still-valid PIN without a fresh OAuth round-trip. ``complete_link`` upserts by
    machine id, so re-linking the same server just refreshes its stored connection and token.
    """
    async with session_factory() as session:
        pending = await session.scalar(
            select(PendingPlexLogin).where(
                PendingPlexLogin.pin_id == pin_id, PendingPlexLogin.purpose == "link"
            )
        )
        if pending is None:
            raise PlexLinkError("This link request is no longer valid. Please start again.")
        expired = pending.expires_at <= utcnow()
        cid = await client_identifier(session)
        if expired:
            await session.delete(pending)
        await session.commit()

    if expired:
        raise PlexLinkError("This link request timed out. Please start again.")

    async with PlexTvClient(cid, safety=safety) as plextv:
        try:
            token = await plextv.check_pin(pin_id)
        except IntegrationError as exc:
            if exc.status == 429:
                return None  # we polled too eagerly; tell the browser to retry
            raise PlexLinkError("Could not reach Plex to check the link.") from exc

        if not token:
            return None  # not approved yet

        try:
            account = await plextv.account(token)
            owned = await plextv.owned_servers(token)
        except IntegrationError as exc:
            raise PlexLinkError("Signed in to Plex, but could not read the account.") from exc

    # Consume the pending PIN only on a *final* outcome: success, or a permanent refusal
    # (owns no server / owns several). A transient probe failure leaves it intact so the
    # browser can re-poll the still-valid PIN -- otherwise a server briefly unreachable at
    # the instant of sign-in would force the owner through a fresh OAuth flow despite having
    # just authenticated. Consuming on the final outcomes still prevents token replay.
    consume_pending = True
    try:
        linked = await complete_link(
            session_factory, box, token=token, account=account, owned=owned
        )
    except PlexLinkRetryableError:
        consume_pending = False  # PIN still valid; let the browser re-poll it
        raise
    finally:
        if consume_pending:
            async with session_factory() as session:
                await session.execute(
                    delete(PendingPlexLogin).where(PendingPlexLogin.pin_id == pin_id)
                )
                await session.commit()

    return linked
