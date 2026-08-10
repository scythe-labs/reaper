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
from typing import Literal

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
from reaper.services import app_settings

log = structlog.get_logger(__name__)

CLIENT_ID_KEY = "plex_client_identifier"

#: How long a browser has to finish a PIN flow, sign-in or link, before the pending row
#: expires. Twice ``PlexTvClient.PIN_TIMEOUT`` so the row outlives the poll window rather
#: than expiring underneath an operator who is still typing their plex.tv password.
PIN_TTL = timedelta(minutes=10)

#: Which flow a pending row belongs to. ``poll_plex_login`` reads only ``"login"`` rows and
#: ``poll_link`` only ``"link"`` ones, so a PIN approved for one flow can never be spent by
#: the other. That fence is what keeps an admin's re-link from being redeemed for a session
#: at ``/api/auth/plex/poll``, which is an open route where the link routes are not.
PinPurpose = Literal["login", "link"]


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


@dataclass(frozen=True)
class PlexServerCandidate:
    """One owned server the signing-in account could link. Safe to show a browser:
    a name and an identifier, never a token."""

    name: str
    machine_identifier: str


class PlexServerChoiceNeededError(PlexLinkError):
    """The account owns several servers, and Reaper will not guess between them.

    Not a refusal: the sign-in succeeded and the owner merely has to say which library
    this deletion tool should manage. Callers that can render a choice (the web flows,
    the CLI) catch this, present ``candidates``, and retry with an explicit pick. Like
    the retryable error it must NOT consume the pending PIN -- the same sign-in has to
    finish once the choice is made. A subclass of ``PlexLinkError`` so any caller not
    yet showing a picker degrades to the old refusal, never to a silent guess.
    """

    def __init__(self, candidates: list[PlexServerCandidate]) -> None:
        names = ", ".join(repr(c.name) for c in candidates)
        super().__init__(
            f"This account owns more than one Plex server ({names}). "
            "Pick the one Reaper should manage."
        )
        self.candidates = candidates


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
class PinStart:
    """A minted PIN, and the plex.tv URL the browser has to open to approve it."""

    pin_id: int
    auth_url: str


async def start_pin(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    purpose: PinPurpose,
    safety: RuntimeSafety,
    forward_url: str | None = None,
) -> PinStart:
    """Create a PIN, record it as pending, and return the URL to open.

    Both browser-driven plex.tv flows start here: signing an operator in
    (:func:`reaper.services.login.poll_plex_login`) and linking a server from Settings
    (:func:`poll_link`). They were written out twice and differed only in ``purpose``,
    which is now the one argument. Whichever value is passed is the only poller that can
    ever spend the row.

    The backend polls plex.tv; the browser never handles a token. ``forward_url`` is where
    plex.tv sends the sign-in window when the operator is done, which is how that window
    gets closed (``schemas.PLEX_FORWARD_PATH``).

    No transaction is held across the network call. Two brief database touches sit either
    side of it, for the same reason :func:`link` gives at length: SQLite hands one writer
    the database, and a session held open across a plex.tv round trip blocks every other
    writer for as long as it takes.
    """
    async with session_factory() as session:
        cid = await client_identifier(session)
        # Opportunistically drop stale pendings so the table cannot grow without bound
        # from abandoned sign-ins. This is the only sweeper the table has, and it covers
        # every purpose, so a third flow added here inherits it rather than forgetting it.
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
                purpose=purpose,
                created_at=utcnow(),
                expires_at=expiry(PIN_TTL),
            )
        )
        await session.commit()

    return PinStart(pin_id=pin.pin_id, auth_url=pin.auth_url(cid, forward_url))


@dataclass(frozen=True)
class LinkedServer:
    name: str
    machine_identifier: str
    connection_uri: str
    local: bool
    relay: bool


async def reachable_connection(
    resource: PlexResource, token: str, *, verify: bool = True
) -> PlexConnection:
    """Probe every connection and take the best one that answers.

    Ordered local/https, then local/http, then remote, then **relay last**: the relay is
    bandwidth-capped and proxied through Plex, so it is a fallback rather than a default.

    Every alternative is stored alongside the winner, so that a URI which stops working
    can be re-resolved later without dragging the owner back through OAuth. ``verify``
    is the operator's certificate-check choice for THIS server (a self-signed HTTPS
    Plex is unreachable with it on); it rides through to every probe.
    """
    for connection in resource.preferred_connections():
        if await probe_connection(connection, token, verify=verify):
            return connection

    # Retryable, deliberately: a server that answers none of its advertised addresses right
    # now may simply be restarting. The caller (poll_link) must not consume the PIN over
    # this, so the browser can re-poll the still-valid PIN once the server is back.
    raise PlexLinkRetryableError(
        f'Found your server ("{resource.name}") but could not reach it on any of its '
        f"{len(resource.connections)} advertised addresses. Reaper has to talk to the "
        "server directly; check that it is running and reachable from this host."
    )


async def link(
    session_factory: async_sessionmaker[AsyncSession],
    box: SecretBox,
    *,
    safety: RuntimeSafety,
    on_prompt: object = None,
    choice: str | None = None,
) -> LinkedServer:
    """Run the PIN flow end to end and persist the result.

    Takes a session *factory*, not a session, and holds a database transaction only for
    the two brief moments it needs one -- reading the client identifier at the start and
    writing the server row at the end. **The multi-minute wait for the human to sign in
    happens with no transaction open at all.**

    SQLite gives a writer the database, and an ``AsyncSession`` held
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

    return await complete_link(
        session_factory, box, token=token, account=account, owned=owned, choice=choice
    )


def _select_owned(owned: list[PlexResource], choice: str) -> PlexResource:
    """Resolve an explicit server choice against the OWNED list, and only that list.

    Matching from ``owned`` is the property that matters: whatever string arrives here,
    the result can never be a server the account does not own. The choice is a machine
    identifier (what the web picker sends back) or an exact name (what a human types at
    the CLI). Two owned servers sharing the chosen name is refused rather than guessed,
    exactly like every other ambiguity on this path.
    """
    by_id = [r for r in owned if r.client_identifier == choice]
    if by_id:
        return by_id[0]  # identifiers are unique per server; plex.tv will not list dupes

    by_name = [r for r in owned if r.name == choice]
    if len(by_name) > 1:
        ids = ", ".join(r.client_identifier for r in by_name)
        raise PlexLinkError(
            f'This account owns more than one server named "{choice}". Pick by machine '
            f"identifier instead: {ids}."
        )
    if not by_name:
        names = ", ".join(f'"{r.name}"' for r in owned)
        raise PlexLinkError(
            f'No server this account owns matches "{choice}". It owns: {names}. '
            "Start the sign-in again and pick one of those."
        )
    return by_name[0]


async def complete_link(
    session_factory: async_sessionmaker[AsyncSession],
    box: SecretBox,
    *,
    token: str,
    account: PlexAccount,
    owned: list[PlexResource],
    choice: str | None = None,
    verify_tls: bool = True,
) -> LinkedServer:
    """Turn a signed-in owner's discovered servers into a persisted link.

    Split out of :func:`link` so the web setup flow can reuse it: that flow runs
    its own poll-based PIN loop (the browser needs to be told to open the auth
    URL and then polled), but the *decision* -- refuse if they own no server,
    demand an explicit choice if they own several, probe for a reachable
    connection, encrypt and persist -- is identical, and must not drift between
    the CLI and the UI.

    ``choice`` names one of the *owned* servers (machine identifier, or exact
    name from the CLI). Absent, a single owned server is linked and several
    raise :class:`PlexServerChoiceNeededError` -- never a guess, because picking one
    arbitrarily is how you point a deletion tool at the wrong library.
    """
    if not owned:
        raise PlexLinkError(
            f'Signed in as "{account.username}", but that account does not own a Plex '
            "server. Reaper must be linked by the server owner: it is going to be "
            "given permission to delete media."
        )
    if choice is not None:
        resource = _select_owned(owned, choice)
    elif len(owned) > 1:
        raise PlexServerChoiceNeededError(
            [
                PlexServerCandidate(name=r.name, machine_identifier=r.client_identifier)
                for r in owned
            ]
        )
    else:
        resource = owned[0]
    connection = await reachable_connection(
        resource, resource.access_token or token, verify=verify_tls
    )
    token_enc = box.encrypt(resource.access_token or token)

    # -- brief DB touch: persist the linked server -------------------------
    async with session_factory() as session:
        rows = list((await session.execute(select(PlexServer))).scalars().all())
        existing = next(
            (r for r in rows if r.machine_identifier == resource.client_identifier), None
        )
        # One linked server is the invariant every reader (`select(PlexServer).first()`)
        # relies on. Re-linking a DIFFERENT server must replace, never accumulate: two
        # rows would make "the" server an arbitrary pick -- exactly the ambiguity a
        # deletion tool cannot carry.
        others = [r for r in rows if r is not existing]
        for other in others:
            await session.delete(other)
        if others:
            # The linked server changed, so everything keyed to the old server's
            # rating keys and section ids is meaningless now: the library choices and
            # the announced set start over.
            await app_settings.set_plex_libraries(session, [])
            await app_settings.set_leaving_soon_announced(session, set())

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
        # The certificate-check choice made while linking sticks to the server row;
        # every later client (scan, reap gateway, Leaving Soon) reads it from here.
        row.verify_tls = verify_tls
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


async def switch_server(
    session_factory: async_sessionmaker[AsyncSession],
    box: SecretBox,
    *,
    machine_identifier: str,
    safety: RuntimeSafety,
    verify_tls: bool | None = None,
) -> LinkedServer:
    """Point Reaper at a different server the same account owns, without a fresh OAuth.

    The stored token is the account credential (see the module docstring), so plex.tv can
    be asked which servers it owns right now; the choice is resolved against that OWNED
    list and nothing else, exactly like the link flow. Reuses :func:`complete_link`, so
    the probe, the single-row invariant, and the stale-state clearing cannot drift from
    the OAuth path.

    ``verify_tls`` overrides the certificate check for the NEW server; omitted, it keeps
    the current server's setting. The old value is the wrong default when the target is a
    different, self-signed server -- the probe would fail with no way to turn it off.
    """
    async with session_factory() as session:
        row = (await session.execute(select(PlexServer))).scalars().first()
        if row is None:
            raise PlexLinkError("No Plex server is linked yet. Link one first.")
        token = box.decrypt(row.token_enc)
        cid = await client_identifier(session)
        resolved_verify_tls = row.verify_tls if verify_tls is None else verify_tls
        await session.commit()

    async with PlexTvClient(cid, safety=safety) as plextv:
        try:
            account = await plextv.account(token)
            owned = await plextv.owned_servers(token)
        except IntegrationError as exc:
            raise PlexLinkRetryableError(
                f"Could not ask plex.tv which servers this account owns: {exc}"
            ) from exc

    return await complete_link(
        session_factory,
        box,
        token=token,
        account=account,
        owned=owned,
        choice=machine_identifier,
        verify_tls=resolved_verify_tls,
    )


# ---------------------------------------------------------------------------
# In-app (re-)link: a browser-driven PIN flow for an already-signed-in admin.
# It starts at start_pin(purpose="link"); what follows is the half that is its own.
# ---------------------------------------------------------------------------


async def poll_link(
    session_factory: async_sessionmaker[AsyncSession],
    box: SecretBox,
    *,
    pin_id: int,
    safety: RuntimeSafety,
    choice: str | None = None,
    verify_tls: bool = True,
) -> LinkedServer | None:
    """Check an in-app link once. ``None`` while still pending; the linked server once done.

    Raises :class:`PlexLinkError` on a *permanent* refusal (owns no server, or a choice
    that matches none) and consumes the pending row so the obtained token cannot be
    replayed. Two outcomes leave the pending row **intact**, so the browser can re-poll
    the still-valid PIN without a fresh OAuth round-trip: a transient failure of the
    reachable-connection probe (:class:`PlexLinkRetryableError`), and an account owning
    several servers (:class:`PlexServerChoiceNeededError`) -- the browser shows the candidates
    and re-polls with ``choice`` set. ``complete_link`` upserts by machine id, so
    re-linking the same server just refreshes its stored connection and token.
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
    # (owns no server / a choice matching nothing). Two intermediate outcomes leave it
    # intact so the browser can re-poll the still-valid PIN -- a transient probe failure
    # (otherwise a server briefly unreachable at the instant of sign-in forces a fresh
    # OAuth flow despite a successful sign-in), and a multi-server account waiting on its
    # owner's choice. Consuming on the final outcomes still prevents token replay.
    consume_pending = True
    try:
        linked = await complete_link(
            session_factory,
            box,
            token=token,
            account=account,
            owned=owned,
            choice=choice,
            verify_tls=verify_tls,
        )
    except (PlexLinkRetryableError, PlexServerChoiceNeededError):
        # PIN still valid in both cases; let the browser re-poll it -- after the
        # transient blip passes, or carrying the owner's server choice.
        consume_pending = False
        raise
    finally:
        if consume_pending:
            async with session_factory() as session:
                await session.execute(
                    delete(PendingPlexLogin).where(PendingPlexLogin.pin_id == pin_id)
                )
                await session.commit()

    return linked
