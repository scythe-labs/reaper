# SPDX-License-Identifier: AGPL-3.0-or-later
"""The unauthenticated surface, and what it costs a stranger to hammer it.

Four properties that must hold evenly across every pre-auth route, not just some of them:

* Plex sign-in is rate limited. The password routes have a consecutive-failure lockout,
  which never trips on an endpoint whose calls all succeed.
* A forwarded header is believed only from a proxy the operator listed. That was already
  true of ``X-Forwarded-For`` but not of ``X-Forwarded-Proto``.
* The Argon2 gate counts hashes, not requests.
* A recovery code is spent only when it actually signs someone in.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session

from reaper.auth.passwords import hash_password
from reaper.auth.proxy import parse_proxy_networks
from reaper.auth.ratelimit import (
    ConcurrencyGate,
    RateLimiter,
    login_throttle,
    password_throttle,
    plex_poll_limit,
    plex_start_limit,
    recover_throttle,
)
from reaper.auth.recovery import recovery_base_url
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import AppUser, AuthProvider, RecoveryToken
from reaper.main import create_app

HEADERS = {"X-Reaper-CSRF": "1"}


@pytest.fixture(autouse=True)
def _fresh_limits() -> Iterator[None]:
    for limiter in (plex_start_limit, plex_poll_limit):
        limiter.reset()
    for throttle in (login_throttle, recover_throttle, password_throttle):
        throttle.reset()
    yield
    for limiter in (plex_start_limit, plex_poll_limit):
        limiter.reset()
    for throttle in (login_throttle, recover_throttle, password_throttle):
        throttle.reset()


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, secret_key="k")


class TestTheRateLimiter:
    """The unit itself. A fixed window, counting every call rather than every failure."""

    def test_it_allows_up_to_the_limit_then_refuses(self) -> None:
        now = [0.0]
        limiter = RateLimiter(limit=3, window=60.0, clock=lambda: now[0])
        assert [limiter.retry_after("ip") for _ in range(3)] == [0.0, 0.0, 0.0]
        assert limiter.retry_after("ip") > 0.0

    def test_the_window_reopens(self) -> None:
        now = [0.0]
        limiter = RateLimiter(limit=1, window=60.0, clock=lambda: now[0])
        assert limiter.retry_after("k") == 0.0
        assert limiter.retry_after("k") > 0.0
        now[0] = 60.0
        assert limiter.retry_after("k") == 0.0

    def test_keys_are_independent(self) -> None:
        limiter = RateLimiter(limit=1, window=60.0, clock=lambda: 0.0)
        assert limiter.retry_after("a") == 0.0
        assert limiter.retry_after("b") == 0.0

    def test_success_does_not_forgive_it(self) -> None:
        """The difference from ``Throttle``. A flood made of calls that all succeed is
        exactly the case a failure-lockout cannot see."""
        limiter = RateLimiter(limit=2, window=60.0, clock=lambda: 0.0)
        limiter.retry_after("k")
        limiter.retry_after("k")
        assert limiter.retry_after("k") > 0.0


class TestTheArgon2GateCountsHashes:
    def test_a_caller_takes_one_slot_per_hash(self) -> None:
        """A request takes one slot per password hash it runs, one per local admin, so the
        cap bounds the actual CPU work rather than just the request count."""
        gate = ConcurrencyGate(4)
        assert gate.acquire(3) == 3
        # Only one slot left, so a two-hash caller is shed rather than admitted.
        assert gate.acquire(2) == 0
        assert gate.acquire(1) == 1

    def test_release_gives_back_exactly_what_was_taken(self) -> None:
        gate = ConcurrencyGate(4)
        taken = gate.acquire(3)
        gate.release(taken)
        assert gate.acquire(4) == 4

    def test_a_caller_wanting_more_than_the_gate_holds_is_clamped_not_stuck(self) -> None:
        """Clamping is why the taken count is returned. An install with more admins than
        the gate has slots must still be able to sign in on a quiet server."""
        gate = ConcurrencyGate(2)
        assert gate.acquire(5) == 2
        gate.release(2)
        assert gate.acquire(1) == 1


class TestTheRecoveryBanner:
    #: Bind values that name every interface. Spelled once so the S104 suppression sits in
    #: one place. These are strings under test, not an address anything binds to.
    EVERY_INTERFACE = ("0.0.0.0", "::", "")  # noqa: S104

    @pytest.mark.parametrize("host", EVERY_INTERFACE)
    def test_a_bind_that_means_every_interface_is_not_printed_as_an_address(
        self, host: str
    ) -> None:
        """It printed the bind address on a default install. That is not a place a
        locked-out operator can open."""
        url = recovery_base_url(host, 8420)
        assert host not in url or not host
        assert "<your-reaper-address>" in url
        assert "8420" in url

    def test_a_real_address_is_kept(self) -> None:
        assert recovery_base_url("192.0.2.10", 8420) == "http://192.0.2.10:8420"


class TestForwardedHeadersNeedATrustedPeer:
    """``is_secure_request`` decides the session cookie's ``Secure`` flag and ``__Host-``
    name off ``X-Forwarded-Proto``. Believing that from anyone lets a caller name its own
    cookie. Over plain HTTP the browser then drops it, so the sign-in silently does
    nothing."""

    def _request(
        self,
        *,
        proxies: tuple[object, ...],
        peer: str,
        proto: str | None,
        scheme: str = "http",
    ) -> bool:
        """``proto=None`` sends no ``X-Forwarded-Proto`` at all. ``scheme`` is the ASGI one."""
        from starlette.requests import Request

        from reaper.auth.cookie import is_secure_request

        class _App:
            class state:  # noqa: N801 -- mimics Starlette's app.state
                trusted_proxies = proxies

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "scheme": scheme,
            "headers": [] if proto is None else [(b"x-forwarded-proto", proto.encode())],
            "client": (peer, 1234),
            "server": ("testserver", 80),
            "query_string": b"",
            "app": _App(),
        }
        return is_secure_request(Request(scope))

    def test_an_untrusted_peer_is_ignored(self) -> None:
        assert not self._request(proxies=(), peer="203.0.113.7", proto="https")

    def test_a_peer_outside_the_listed_networks_is_ignored(self) -> None:
        proxies = parse_proxy_networks(["172.16.0.0/12"])
        assert not self._request(proxies=proxies, peer="203.0.113.7", proto="https")

    def test_a_listed_proxy_is_honored(self) -> None:
        proxies = parse_proxy_networks(["172.16.0.0/12"])
        assert self._request(proxies=proxies, peer="172.16.0.5", proto="https")

    def test_a_listed_proxy_reporting_http_stays_insecure(self) -> None:
        proxies = parse_proxy_networks(["172.16.0.0/12"])
        assert not self._request(proxies=proxies, peer="172.16.0.5", proto="http")

    def test_an_untrusted_https_claim_cannot_launder_through_the_scheme(self) -> None:
        """The ASGI scheme must not count as evidence when the caller itself supplied the
        header that produced it.

        An upstream ``ProxyHeadersMiddleware`` derives ``request.url.scheme`` from
        ``X-Forwarded-Proto``, so trusting the scheme alone would let an unauthenticated
        caller's own header claim decide Reaper's answer. This case sends the ``https``
        scheme alongside the exact header that produced it, to prove that loop is closed.
        """
        assert not self._request(proxies=(), peer="127.0.0.1", proto="https", scheme="https")

    def test_direct_tls_with_no_forwarded_header_is_still_secure(self) -> None:
        """The narrowness that keeps the case above from costing anyone their Secure flag.

        Only a *claim* is refused, never the transport. An install terminating TLS in the app
        sends no ``X-Forwarded-Proto``, so nothing can have rewritten the scheme, and it is
        believed. That is the one place a bare scheme is still evidence.
        """
        assert self._request(proxies=(), peer="203.0.113.7", proto=None, scheme="https")

    def test_a_listed_proxy_reporting_http_outranks_an_https_leg(self) -> None:
        """The question is about the browser's leg, not the proxy's leg to Reaper.

        A proxy that speaks HTTPS to Reaper while serving the browser over plain HTTP must
        not get a ``Secure``/``__Host-`` cookie. The browser would drop a cookie like that
        immediately, making the sign-in silently do nothing.
        """
        proxies = parse_proxy_networks(["172.16.0.0/12"])
        assert not self._request(proxies=proxies, peer="172.16.0.5", proto="http", scheme="https")

    def test_a_listed_proxy_that_claims_nothing_falls_back_to_the_scheme(self) -> None:
        proxies = parse_proxy_networks(["172.16.0.0/12"])
        assert self._request(proxies=proxies, peer="172.16.0.5", proto=None, scheme="https")
        assert not self._request(proxies=proxies, peer="172.16.0.5", proto=None, scheme="http")


class TestTheServerDoesNotDecidePeerTrust:
    """What ``--no-proxy-headers`` buys, demonstrated against the real middleware.

    ``tests/test_repo_hygiene.py`` pins the flag onto every launch. This shows why it is
    worth pinning, by running the layer the flag removes and showing what reaches
    :func:`reaper.auth.proxy.client_ip` underneath it. Reverse-proxy trust is off here and no
    proxy is listed, which is the default install.

    It also acts as a canary on the dependency. If uvicorn ever stops trusting loopback by
    default, the first assertion fails and this whole apparatus can be revisited.
    """

    SPOOF = "198.51.100.77"

    def _rate_limit_key(self, *, peer: str, middleware: bool) -> str:
        """What ``client_ip`` keys the sign-in lockout on, with and without uvicorn's layer."""
        import anyio
        from starlette.requests import Request
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        from reaper.auth.proxy import client_ip

        seen: list[str] = []

        class _App:
            class state:  # noqa: N801 -- mimics Starlette's app.state
                trusted_proxies = ()

        async def endpoint(scope: dict[str, Any], receive: object, send: object) -> None:
            seen.append(client_ip(Request(scope)))

        async def _noop(*_args: object) -> None:
            return None

        # trusted_hosts is uvicorn's own default, resolved from forwarded_allow_ips=None.
        target: object = (
            ProxyHeadersMiddleware(endpoint, trusted_hosts="127.0.0.1")  # type: ignore[arg-type]
            if middleware
            else endpoint
        )
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "scheme": "http",
            "headers": [(b"x-forwarded-for", self.SPOOF.encode())],
            "client": (peer, 51234),
            "server": ("testserver", 8420),
            "query_string": b"",
            "app": _App(),
        }
        anyio.run(lambda: target(scope, _noop, _noop))  # type: ignore[operator]
        return seen[0]

    def test_the_middleware_lets_a_loopback_caller_write_its_own_rate_limit_key(self) -> None:
        """Without this, five bad passwords from one machine could count as five different
        callers."""
        assert self._rate_limit_key(peer="127.0.0.1", middleware=True) == self.SPOOF

    def test_the_same_spoof_from_off_box_is_already_ignored(self) -> None:
        """The middleware only rewrites for a peer it trusts, which bounds the blast radius.

        Under the shipped bridge-network compose, the in-container peer is the gateway
        rather than loopback, so the default deployment was never reachable this way from
        outside the host. That bounds the risk. It does not restore the guarantee, which is
        the point of the fix.
        """
        assert self._rate_limit_key(peer="172.18.0.1", middleware=True) == "172.18.0.1"

    def test_without_the_middleware_the_real_peer_is_what_gets_locked_out(self) -> None:
        """What every launch asks for with ``--no-proxy-headers``."""
        assert self._rate_limit_key(peer="127.0.0.1", middleware=False) == "127.0.0.1"


class TestPlexSignInIsRateLimited:
    @pytest.fixture
    def client(self, tmp_path: Path) -> Iterator[TestClient]:
        settings = _settings(tmp_path)
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        engine.dispose()
        with TestClient(create_app(settings)) as c:
            yield c

    def test_start_refuses_past_its_cap(
        self, client: TestClient, httpx2_mock: respx.Router
    ) -> None:
        """Every call inserts a pending row and asks plex.tv for a PIN, so a flood grows the
        table and can get the install's egress address rate-limited by plex.tv. That would
        lock the real operator out of Plex sign-in."""
        minted = {"n": 0}

        def new_pin(request: object) -> httpx.Response:
            minted["n"] += 1
            return httpx.Response(201, json={"id": minted["n"], "code": "ABCD"})

        pins = httpx2_mock.post("https://plex.tv/api/v2/pins").mock(side_effect=new_pin)
        for _ in range(plex_start_limit.limit):
            assert client.post("/api/auth/plex/start", headers=HEADERS).status_code == 200

        refused = client.post("/api/auth/plex/start", headers=HEADERS)
        assert refused.status_code == 429
        assert refused.headers["Retry-After"]
        # Refused before the outbound call, which is the half that protects plex.tv (and
        # therefore the operator's own ability to sign in) rather than just our own table.
        assert pins.call_count == plex_start_limit.limit

    def test_poll_has_a_much_looser_cap(self, client: TestClient) -> None:
        """One honest sign-in polls every two seconds for up to five minutes, so a cap
        that broke at 150 would break the flow it protects."""
        assert plex_poll_limit.limit > 150
        assert plex_poll_limit.limit > plex_start_limit.limit

    def test_poll_refuses_past_its_cap(self, client: TestClient) -> None:
        for _ in range(plex_poll_limit.limit):
            client.post("/api/auth/plex/poll", json={"pin_id": 1}, headers=HEADERS)
        refused = client.post("/api/auth/plex/poll", json={"pin_id": 1}, headers=HEADERS)
        assert refused.status_code == 429


class TestARecoveryCodeIsSpentOnlyOnASignIn:
    def _client(self, tmp_path: Path, *, with_admin: bool) -> tuple[TestClient, str]:
        tmp_path.mkdir(parents=True, exist_ok=True)
        settings = _settings(tmp_path)
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        from reaper.auth.tokens import RECOVERY_TTL, hash_token

        now = utcnow()
        with Session(engine) as session:
            session.add(
                RecoveryToken(
                    token_hash=hash_token("code-abc"),
                    created_at=now,
                    expires_at=now + RECOVERY_TTL,
                )
            )
            if with_admin:
                session.add(
                    AppUser(
                        provider=AuthProvider.LOCAL,
                        username="admin",
                        password_hash=hash_password("a-long-enough-password"),
                        is_active=True,
                        created_at=now,
                    )
                )
            session.commit()
        engine.dispose()
        return TestClient(create_app(settings)), "code-abc"

    def test_no_admin_to_sign_in_as_leaves_the_code_usable(self, tmp_path: Path) -> None:
        """A 409 must never commit the redemption. That would burn the operator's one
        15-minute code on a failure that had nothing to do with the code, and "no admin
        exists" is exactly when recovery matters most."""
        app_client, code = self._client(tmp_path, with_admin=False)
        with app_client as client:
            refused = client.post("/api/auth/recover", json={"token": code}, headers=HEADERS)
            assert refused.status_code == 409

        # A fresh app over the same database. The code still works once an admin exists.
        settings = _settings(tmp_path)
        engine = sa_create_engine(settings.sync_database_url)
        with Session(engine) as session:
            session.add(
                AppUser(
                    provider=AuthProvider.LOCAL,
                    username="admin",
                    password_hash=hash_password("a-long-enough-password"),
                    is_active=True,
                    created_at=utcnow(),
                )
            )
            session.commit()
        engine.dispose()

        with TestClient(create_app(settings)) as client:
            ok = client.post("/api/auth/recover", json={"token": code}, headers=HEADERS)
            assert ok.status_code == 200, ok.text

    def test_a_successful_redemption_still_spends_it(self, tmp_path: Path) -> None:
        """The negative half of the property above. Rolling back the failure case must not
        make the code multi-use, which is the property the whole recovery design rests on."""
        app_client, code = self._client(tmp_path, with_admin=True)
        with app_client as client:
            first = client.post("/api/auth/recover", json={"token": code}, headers=HEADERS)
            assert first.status_code == 200, first.text
            again = client.post("/api/auth/recover", json={"token": code}, headers=HEADERS)
            assert again.status_code == 401

    def test_the_redeemed_audit_line_fires_only_on_the_durable_login(self, tmp_path: Path) -> None:
        """The "gained admin access" line records an outcome, so it fires only after the
        commit, never at flush time, where a 409 rollback would undo the redemption first.
        Logging at flush time would announce a sign-in that never happened, while the
        recovery code remained usable, which is the one recovery event a security review
        reads first."""
        from reaper import logbuffer

        def redeemed_since(cursor: int) -> list[str]:
            return [
                line.text
                for line in logbuffer.RING.since(cursor)
                if "recovery.redeemed" in line.text
            ]

        # No admin to sign in as. The 409 rolls the redemption back, so nothing was gained
        # and nothing may be logged as gained.
        no_admin, code = self._client(tmp_path / "no-admin", with_admin=False)
        with no_admin as client:
            cursor = logbuffer.RING.last_seq()
            refused = client.post("/api/auth/recover", json={"token": code}, headers=HEADERS)
            assert refused.status_code == 409
            assert redeemed_since(cursor) == []

        # An admin exists. The redemption commits and the line fires exactly once.
        with_admin, code = self._client(tmp_path / "with-admin", with_admin=True)
        with with_admin as client:
            cursor = logbuffer.RING.last_seq()
            ok = client.post("/api/auth/recover", json={"token": code}, headers=HEADERS)
            assert ok.status_code == 200, ok.text
            assert len(redeemed_since(cursor)) == 1
