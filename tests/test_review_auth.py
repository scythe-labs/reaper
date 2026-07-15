# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brute-force throttling and CPU-shedding on the login endpoints.

These guard the fix for the review finding that ``POST /api/auth/local`` ran a
full Argon2id verification on every unauthenticated request with no lockout: a
scripted attacker could both brute-force the admin password and pin the CPU. The
unit tests pin the :class:`Throttle` / :class:`ConcurrencyGate` mechanics with an
injected clock; the route tests prove the wiring -- a burst of wrong passwords
earns a 429, and the lock lifts once its window elapses.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine

from reaper.auth.ratelimit import (
    ConcurrencyGate,
    Throttle,
    argon2_gate,
    login_throttle,
    recover_throttle,
)
from reaper.config import Settings
from reaper.db.base import Base
from reaper.main import create_app

from ._auth import TEST_ADMIN, TEST_PASSWORD, seed_admin

CSRF = {"X-Reaper-CSRF": "1"}


class _FakeClock:
    """A monotonic clock the test drives by hand -- no real sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Throttle mechanics
# ---------------------------------------------------------------------------


class TestThrottle:
    def test_under_the_threshold_never_locks(self) -> None:
        clock = _FakeClock()
        t = Throttle(threshold=5, base_delay=2.0, clock=clock)
        for _ in range(4):  # one shy of the threshold
            assert t.record_failure("ip:x") == 0.0
        assert t.retry_after("ip:x") == 0.0

    def test_the_threshold_failure_locks_for_the_base_delay(self) -> None:
        clock = _FakeClock()
        t = Throttle(threshold=3, base_delay=2.0, clock=clock)
        assert t.record_failure("ip:x") == 0.0
        assert t.record_failure("ip:x") == 0.0
        assert t.record_failure("ip:x") == 2.0  # threshold crossed
        assert t.retry_after("ip:x") == 2.0

    def test_the_lockout_backs_off_exponentially_and_caps(self) -> None:
        clock = _FakeClock()
        t = Throttle(threshold=1, base_delay=2.0, max_delay=10.0, clock=clock)
        assert t.record_failure("ip:x") == 2.0  # 2 * 2**0
        assert t.record_failure("ip:x") == 4.0  # 2 * 2**1
        assert t.record_failure("ip:x") == 8.0  # 2 * 2**2
        assert t.record_failure("ip:x") == 10.0  # would be 16, capped at max_delay
        assert t.record_failure("ip:x") == 10.0  # stays capped

    def test_retry_after_counts_down_and_reaches_zero(self) -> None:
        clock = _FakeClock()
        t = Throttle(threshold=1, base_delay=10.0, clock=clock)
        t.record_failure("ip:x")
        assert t.retry_after("ip:x") == 10.0
        clock.advance(4.0)
        assert t.retry_after("ip:x") == 6.0
        clock.advance(6.0)
        assert t.retry_after("ip:x") == 0.0

    def test_a_success_forgives_prior_failures(self) -> None:
        clock = _FakeClock()
        t = Throttle(threshold=2, base_delay=5.0, clock=clock)
        t.record_failure("ip:x")
        t.record_failure("ip:x")
        assert t.retry_after("ip:x") > 0.0
        t.record_success("ip:x")
        assert t.retry_after("ip:x") == 0.0
        # And the counter is genuinely back to zero, not merely unlocked.
        assert t.record_failure("ip:x") == 0.0

    def test_an_idle_key_decays_so_the_counter_resets(self) -> None:
        clock = _FakeClock()
        t = Throttle(threshold=2, base_delay=5.0, decay=100.0, clock=clock)
        t.record_failure("ip:x")  # one failure on the books
        clock.advance(101.0)  # walk away past the decay window
        # The next failure starts a fresh count rather than tipping into lockout.
        assert t.record_failure("ip:x") == 0.0

    def test_keys_are_tracked_independently(self) -> None:
        clock = _FakeClock()
        t = Throttle(threshold=1, base_delay=5.0, clock=clock)
        t.record_failure("ip:a")
        assert t.retry_after("ip:a") == 5.0
        assert t.retry_after("ip:b") == 0.0  # a different key is untouched


class TestConcurrencyGate:
    def test_it_admits_up_to_the_limit_then_refuses(self) -> None:
        gate = ConcurrencyGate(2)
        assert gate.acquire() is True
        assert gate.acquire() is True
        assert gate.acquire() is False  # full

    def test_releasing_frees_a_slot(self) -> None:
        gate = ConcurrencyGate(1)
        assert gate.acquire() is True
        assert gate.acquire() is False
        gate.release()
        assert gate.acquire() is True

    def test_release_cannot_underflow_into_over_admission(self) -> None:
        gate = ConcurrencyGate(1)
        gate.release()  # mispaired release before any acquire
        gate.release()
        assert gate.acquire() is True
        assert gate.acquire() is False  # still a hard cap of one

    def test_a_zero_limit_is_clamped_to_one(self) -> None:
        gate = ConcurrencyGate(0)
        assert gate.limit == 1


# ---------------------------------------------------------------------------
# Route wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = sa_create_engine(s.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return s


@pytest.fixture(autouse=True)
def _clean_limiters() -> Iterator[None]:
    """The throttle/gate singletons are process-wide. Reset them around every test
    here so a deliberate lockout cannot bleed into the rest of the suite (which
    logs in over the same TestClient host)."""
    login_throttle.reset()
    recover_throttle.reset()
    argon2_gate.reset()
    yield
    login_throttle.reset()
    recover_throttle.reset()
    argon2_gate.reset()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


class TestLocalLoginThrottle:
    def test_a_burst_of_wrong_passwords_earns_a_429_with_retry_after(
        self, client: TestClient, settings: Settings
    ) -> None:
        seed_admin(settings)
        # The default threshold is 5 consecutive failures. The 6th attempt (or
        # sooner, once the lock is set) is refused with 429 rather than reaching
        # the Argon2 verify at all.
        statuses = []
        for _ in range(8):
            r = client.post(
                "/api/auth/local",
                json={"username": TEST_ADMIN, "password": "wrong"},
                headers=CSRF,
            )
            statuses.append(r.status_code)

        assert 429 in statuses
        locked = client.post(
            "/api/auth/local",
            json={"username": TEST_ADMIN, "password": "wrong"},
            headers=CSRF,
        )
        assert locked.status_code == 429
        assert int(locked.headers["Retry-After"]) >= 1

    def test_once_locked_even_the_right_password_is_refused(
        self, client: TestClient, settings: Settings
    ) -> None:
        """The whole point of the lock: past the threshold, the endpoint stops
        answering credential questions for a while -- correct or not."""
        seed_admin(settings)
        for _ in range(6):
            client.post(
                "/api/auth/local",
                json={"username": TEST_ADMIN, "password": "wrong"},
                headers=CSRF,
            )
        good = client.post(
            "/api/auth/local",
            json={"username": TEST_ADMIN, "password": TEST_PASSWORD},
            headers=CSRF,
        )
        assert good.status_code == 429

    def test_the_lock_lifts_once_its_window_elapses(
        self, client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_admin(settings)
        for _ in range(6):
            client.post(
                "/api/auth/local",
                json={"username": TEST_ADMIN, "password": "wrong"},
                headers=CSRF,
            )
        assert (
            client.post(
                "/api/auth/local",
                json={"username": TEST_ADMIN, "password": TEST_PASSWORD},
                headers=CSRF,
            ).status_code
            == 429
        )
        # Jump the throttle's monotonic clock past the lockout window instead of
        # sleeping through it.
        base = login_throttle.clock()
        monkeypatch.setattr(login_throttle, "clock", lambda: base + login_throttle.max_delay + 1.0)

        ok = client.post(
            "/api/auth/local",
            json={"username": TEST_ADMIN, "password": TEST_PASSWORD},
            headers=CSRF,
        )
        assert ok.status_code == 200

    def test_a_few_wrong_attempts_still_let_the_right_one_through(
        self, client: TestClient, settings: Settings
    ) -> None:
        """Fat-fingering the password a couple of times must not lock the operator
        out -- the throttle only bites past the threshold."""
        seed_admin(settings)
        for _ in range(3):  # under the threshold of 5
            assert (
                client.post(
                    "/api/auth/local",
                    json={"username": TEST_ADMIN, "password": "typo"},
                    headers=CSRF,
                ).status_code
                == 401
            )
        assert (
            client.post(
                "/api/auth/local",
                json={"username": TEST_ADMIN, "password": TEST_PASSWORD},
                headers=CSRF,
            ).status_code
            == 200
        )

    def test_the_argon2_gate_sheds_load_when_full(
        self, client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With every verification slot held, a new login is turned away with 503
        rather than piling more Argon2 work onto a saturated CPU."""
        seed_admin(settings)
        monkeypatch.setattr(argon2_gate, "acquire", lambda: False)
        busy = client.post(
            "/api/auth/local",
            json={"username": TEST_ADMIN, "password": TEST_PASSWORD},
            headers=CSRF,
        )
        assert busy.status_code == 503
        assert "Retry-After" in busy.headers
