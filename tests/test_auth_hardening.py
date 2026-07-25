# SPDX-License-Identifier: AGPL-3.0-or-later
"""The unauthenticated surface, and what it costs a stranger to hammer it.

Four properties, each of which used to hold for some of the pre-auth routes and not the
rest -- which is the shape of every finding here:

* Plex sign-in is rate limited (S-1). The password routes have a consecutive-FAILURE
  lockout, which never trips on an endpoint whose calls all succeed.
* A forwarded header is believed only from a proxy the operator listed (S-7). That was
  already true of ``X-Forwarded-For`` and not of ``X-Forwarded-Proto``.
* The Argon2 gate counts hashes, not requests (S-4).
* A recovery code is spent only when it actually signs someone in (B-13).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

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
    return Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]


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
        """The difference from ``Throttle``, and the whole point: a flood made of calls
        that all succeed is exactly the case a failure-lockout cannot see."""
        limiter = RateLimiter(limit=2, window=60.0, clock=lambda: 0.0)
        limiter.retry_after("k")
        limiter.retry_after("k")
        assert limiter.retry_after("k") > 0.0


class TestTheArgon2GateCountsHashes:
    def test_a_caller_takes_one_slot_per_hash(self) -> None:
        """One gated request used to occupy one slot while running a verification per
        local admin, so the cap bounded requests and not the CPU behind them (S-4)."""
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
        """Clamping is why the taken count is returned: an install with more admins than
        the gate has slots must still be able to sign in on a quiet server."""
        gate = ConcurrencyGate(2)
        assert gate.acquire(5) == 2
        gate.release(2)
        assert gate.acquire(1) == 1


class TestTheRecoveryBanner:
    #: Bind values that name every interface. Spelled once so the S104 suppression sits in
    #: one place: these are strings under test, not an address anything binds to.
    EVERY_INTERFACE = ("0.0.0.0", "::", "")  # noqa: S104

    @pytest.mark.parametrize("host", EVERY_INTERFACE)
    def test_a_bind_that_means_every_interface_is_not_printed_as_an_address(
        self, host: str
    ) -> None:
        """It printed the bind address on a default install: not a place a locked-out
        operator can open (B-12)."""
        url = recovery_base_url(host, 8420)
        assert host not in url or not host
        assert "<your-reaper-address>" in url
        assert "8420" in url

    def test_a_real_address_is_kept(self) -> None:
        assert recovery_base_url("192.0.2.10", 8420) == "http://192.0.2.10:8420"


class TestForwardedHeadersNeedATrustedPeer:
    """``is_secure_request`` decides the session cookie's ``Secure`` flag and ``__Host-``
    name off ``X-Forwarded-Proto``. Believing that from anyone let a caller name its own
    cookie: over plain HTTP the browser then DROPS it, so the sign-in silently does
    nothing (S-7)."""

    def _request(self, *, proxies: tuple[object, ...], peer: str, proto: str) -> bool:
        from starlette.requests import Request

        from reaper.auth.cookie import is_secure_request

        class _App:
            class state:  # noqa: N801 -- mimics Starlette's app.state
                trusted_proxies = proxies

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "scheme": "http",
            "headers": [(b"x-forwarded-proto", proto.encode())],
            "client": (peer, 1234),
            "server": ("testserver", 80),
            "query_string": b"",
            "app": _App(),
        }
        return is_secure_request(Request(scope))  # type: ignore[arg-type]

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
        """Every call inserts a pending row and asks plex.tv for a PIN, so a flood grows
        the table AND can get the install's egress address rate-limited by plex.tv --
        locking the real operator out of Plex sign-in (S-1)."""
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
        # Refused BEFORE the outbound call, which is the half that protects plex.tv (and
        # therefore the operator's own ability to sign in) rather than just our table.
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
        """The 409 path used to commit the redemption anyway, burning the operator's one
        15-minute code on a failure that had nothing to do with the code -- and "no admin
        exists" is exactly when recovery matters most (B-13)."""
        app_client, code = self._client(tmp_path, with_admin=False)
        with app_client as client:
            refused = client.post("/api/auth/recover", json={"token": code}, headers=HEADERS)
            assert refused.status_code == 409

        # A fresh app over the same database: the code still works once an admin exists.
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
        """The negative half: rolling back the failure case must not make the code
        multi-use, which is the property the whole recovery design rests on."""
        app_client, code = self._client(tmp_path, with_admin=True)
        with app_client as client:
            first = client.post("/api/auth/recover", json={"token": code}, headers=HEADERS)
            assert first.status_code == 200, first.text
            again = client.post("/api/auth/recover", json={"token": code}, headers=HEADERS)
            assert again.status_code == 401
