# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings -> General, the API key lane, the docs lockdown, and the Logs tab.

The rules pinned here:

* the stock ``/docs`` and ``/openapi.json`` are gone -- the API description is served
  signed-in-only at ``/api/docs`` and ``/api/openapi.json`` (the second review pass's
  lesson applied forward: nothing outside ``/api`` is authenticated, so nothing
  sensitive may live outside ``/api``);
* the API key authenticates without a cookie and without the CSRF header (no cookie,
  no CSRF risk), backs off per address on bad guesses, and is FENCED off the three
  irreversible authorities: the deletion switch, execute, and sign-in/key management;
* reverse-proxy trust is off by default, applies immediately on save, and
  ``client_ip`` only honors a forwarded chain when the peer itself is a listed proxy;
* the log ring is redacted before storage, polls incrementally by sequence number, and
  the recording level applies instantly and persists (stored value over env seed).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from starlette.requests import Request

from reaper import logbuffer
from reaper.api.middleware import (
    _api_key_allowed,
    api_key_throttle,
    client_ip,
    parse_proxy_networks,
)
from reaper.config import Settings, parse_trusted_proxies
from reaper.db.base import Base
from reaper.main import create_app
from tests._auth import login


class TestScanProgressPercent:
    """The bar reads a monotonic 0-100, never a raw done/total whose denominator changes
    between phases (which made it start full, then jump to 40%)."""

    def test_the_starting_phase_is_near_zero_not_full(self) -> None:
        from reaper.api.scan import _phase_percent

        # total=0 in the early phases must sit at the band start, never divide-by-zero to
        # 100 -- the exact "starts full" bug.
        assert _phase_percent("starting", 0, 0) == 0
        assert _phase_percent("history", 0, 0) == 2
        assert _phase_percent("gathering", 0, 5) == 18

    def test_percent_only_rises_across_a_whole_scan(self) -> None:
        from reaper.api.scan import _phase_percent

        steps = [
            ("starting", 0, 0),
            ("history", 0, 0),
            ("lists", 0, 0),
            ("gathering", 2, 5),
            ("gathering", 5, 5),
            ("scoring", 0, 3446),
            ("scoring", 1700, 3446),
            ("scoring", 3446, 3446),
            ("done", 3446, 3446),
            ("shelves", 0, 0),
            ("complete", 3446, 3446),
        ]
        percents = [_phase_percent(p, d, t) for p, d, t in steps]
        assert percents == sorted(percents), percents  # monotonic
        assert percents[0] == 0
        assert percents[-1] == 100


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A logged-in client over an empty database: exactly a fresh install."""
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


def _bare(client: TestClient) -> TestClient:
    """A second client over the SAME app: no cookies, no CSRF header, no session."""
    return TestClient(client.app)  # type: ignore[arg-type]


class TestGeneralSettings:
    def test_fresh_install_defaults(self, client: TestClient) -> None:
        data = client.get("/api/settings/general").json()
        # The fresh-install time zone is the host's own zone (no stored value, no env seed),
        # so it varies by machine -- assert only that it is a real IANA name, then hold the
        # rest to their fixed defaults.
        tz = data.pop("timezone")
        assert ZoneInfo(tz)
        assert data == {
            "application_name": "Reaper",
            "application_url": None,
            "accent_color": "#25c3ff",
            "api_key_set": False,
            "expand_seasons_default": False,
            "default_spare_days": 0,
            "proxy_trust_enabled": False,
            "trusted_proxies": [],
        }

    def test_a_valid_accent_is_saved_lowercased(self, client: TestClient) -> None:
        data = client.put("/api/settings/general", json={"accent_color": "#4F46E5"}).json()
        assert data["accent_color"] == "#4f46e5"

    def test_a_malformed_accent_is_refused_and_changes_nothing(self, client: TestClient) -> None:
        client.put("/api/settings/general", json={"accent_color": "#4f46e5"})
        response = client.put("/api/settings/general", json={"accent_color": "blue"})
        assert response.status_code == 422
        assert "#" in response.json()["detail"]
        # The bad value never landed; the previous color still stands.
        assert client.get("/api/settings/general").json()["accent_color"] == "#4f46e5"

    def test_an_empty_accent_resets_to_the_default(self, client: TestClient) -> None:
        client.put("/api/settings/general", json={"accent_color": "#000000"})
        data = client.put("/api/settings/general", json={"accent_color": ""}).json()
        assert data["accent_color"] == "#25c3ff"

    def test_expand_seasons_default_round_trips(self, client: TestClient) -> None:
        # Off on a fresh install, so an existing library keeps its collapsed cards.
        assert client.get("/api/settings/general").json()["expand_seasons_default"] is False
        data = client.put("/api/settings/general", json={"expand_seasons_default": True}).json()
        assert data["expand_seasons_default"] is True
        assert client.get("/api/settings/general").json()["expand_seasons_default"] is True
        # Turning it back off is a real choice and is kept.
        data = client.put("/api/settings/general", json={"expand_seasons_default": False}).json()
        assert data["expand_seasons_default"] is False

    def test_default_spare_days_round_trips(self, client: TestClient) -> None:
        # Zero on a fresh install: a plain Spare keeps forever, exactly as before.
        assert client.get("/api/settings/general").json()["default_spare_days"] == 0
        data = client.put("/api/settings/general", json={"default_spare_days": 30}).json()
        assert data["default_spare_days"] == 30
        assert client.get("/api/settings/general").json()["default_spare_days"] == 30
        # Back to forever is a real choice and is kept.
        data = client.put("/api/settings/general", json={"default_spare_days": 0}).json()
        assert data["default_spare_days"] == 0

    def test_a_negative_default_spare_days_is_refused(self, client: TestClient) -> None:
        assert (
            client.put("/api/settings/general", json={"default_spare_days": -5}).status_code == 422
        )

    def test_partial_save_changes_only_what_was_sent(self, client: TestClient) -> None:
        data = client.put("/api/settings/general", json={"application_name": "Media Reaper"}).json()
        assert data["application_name"] == "Media Reaper"
        assert data["proxy_trust_enabled"] is False

        data = client.put(
            "/api/settings/general", json={"application_url": "https://reaper.example.com/"}
        ).json()
        assert data["application_name"] == "Media Reaper"
        assert data["application_url"] == "https://reaper.example.com"  # trailing slash gone

    def test_a_malformed_url_is_refused_in_plain_words(self, client: TestClient) -> None:
        response = client.put("/api/settings/general", json={"application_url": "reaper.local"})
        assert response.status_code == 422
        assert "http" in response.json()["detail"]

    def test_a_malformed_proxy_entry_is_refused(self, client: TestClient) -> None:
        response = client.put("/api/settings/general", json={"trusted_proxies": ["not-an-address"]})
        assert response.status_code == 422

    def test_saving_proxy_trust_applies_immediately(self, client: TestClient) -> None:
        client.put(
            "/api/settings/general",
            json={"proxy_trust_enabled": True, "trusted_proxies": ["172.16.0.0/12"]},
        )
        networks = client.app.state.trusted_proxies  # type: ignore[attr-defined]
        assert len(networks) == 1

        client.put("/api/settings/general", json={"proxy_trust_enabled": False})
        assert client.app.state.trusted_proxies == ()  # type: ignore[attr-defined]


class TestReverseProxyEnvSeed:
    """REAPER_PROXY_TRUST_ENABLED / REAPER_TRUSTED_PROXIES seed the first-boot default;
    the stored value (Settings -> General) wins thereafter, exactly like the deletion
    switch. A declarative deployment can ship trust configured with no UI visit."""

    def _seeded(self, tmp_path: Path) -> Settings:
        settings = Settings(  # type: ignore[call-arg]
            data_dir=tmp_path,
            secret_key="k",
            proxy_trust_enabled=True,
            trusted_proxies="172.16.0.0/12, 10.0.0.5",
        )
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        engine.dispose()
        return settings

    def test_the_env_seed_governs_a_fresh_install(self, tmp_path: Path) -> None:
        settings = self._seeded(tmp_path)
        with TestClient(create_app(settings)) as c:
            login(c, settings)
            data = c.get("/api/settings/general").json()
            assert data["proxy_trust_enabled"] is True
            assert data["trusted_proxies"] == ["172.16.0.0/12", "10.0.0.5"]
            # The live middleware state is armed from the seed at boot, not just the view.
            assert len(c.app.state.trusted_proxies) == 2  # type: ignore[attr-defined]

    def test_the_stored_value_wins_over_the_seed(self, tmp_path: Path) -> None:
        settings = self._seeded(tmp_path)
        with TestClient(create_app(settings)) as c:
            login(c, settings)
            # Turn it off in the UI: the stored false must win over the env seed, and take
            # effect immediately (an empty tuple ignores every forwarded header again).
            c.put("/api/settings/general", json={"proxy_trust_enabled": False})
            assert c.get("/api/settings/general").json()["proxy_trust_enabled"] is False
            assert c.app.state.trusted_proxies == ()  # type: ignore[attr-defined]


def test_parse_trusted_proxies_splits_on_commas_and_whitespace() -> None:
    assert parse_trusted_proxies("172.16.0.0/12, 10.0.0.5") == ["172.16.0.0/12", "10.0.0.5"]
    assert parse_trusted_proxies("172.16.0.0/12  10.0.0.5") == ["172.16.0.0/12", "10.0.0.5"]
    assert parse_trusted_proxies("   ") == []
    assert parse_trusted_proxies("") == []


class TestTheApiKeyLane:
    def _issue(self, client: TestClient) -> str:
        response = client.post("/api/settings/general/api-key")
        assert response.status_code == 200, response.text
        key: str = response.json()["key"]
        return key

    def test_generate_reveal_and_flag(self, client: TestClient) -> None:
        assert client.get("/api/settings/general/api-key").status_code == 404

        key = self._issue(client)
        assert client.get("/api/settings/general/api-key").json()["key"] == key
        assert client.get("/api/settings/general").json()["api_key_set"] is True

    def test_the_key_reads_without_cookie_or_csrf(self, client: TestClient) -> None:
        key = self._issue(client)
        bare = _bare(client)

        # No key, no cookie: the gate holds.
        assert bare.get("/api/settings/general").status_code == 401
        # The key alone reads, with NO cookie and NO CSRF header: nothing ambient for a
        # cross-site page to abuse. A setting *write* still needs the browser (see the
        # fence test below) -- a config change can transmit a stored secret.
        ok = bare.get("/api/settings/general", headers={"X-Api-Key": key})
        assert ok.status_code == 200

    def test_rotation_is_revocation(self, client: TestClient) -> None:
        old = self._issue(client)
        new = self._issue(client)
        bare = _bare(client)

        assert bare.get("/api/settings/general", headers={"X-Api-Key": new}).status_code == 200
        assert bare.get("/api/settings/general", headers={"X-Api-Key": old}).status_code == 401

    def test_bad_keys_back_off_per_address(self, client: TestClient) -> None:
        self._issue(client)
        bare = _bare(client)
        try:
            statuses = [
                bare.get("/api/settings/general", headers={"X-Api-Key": f"guess-{i}"}).status_code
                for i in range(8)
            ]
            assert 429 in statuses
            # Locked out means locked out for the RIGHT key too, until the backoff passes.
        finally:
            # The throttle is process-global; leave it clean for other tests.
            api_key_throttle.record_success("api-key:testclient")

    def test_the_fence_names_what_a_key_may_never_do(self, client: TestClient) -> None:
        key = self._issue(client)
        bare = _bare(client)
        headers = {"X-Api-Key": key}

        # The deletion switch, and key management itself.
        assert (
            bare.put("/api/settings/safety", json={"enabled": True}, headers=headers).status_code
            == 403
        )
        assert bare.post("/api/settings/general/api-key", headers=headers).status_code == 403
        assert bare.get("/api/settings/general/api-key", headers=headers).status_code == 403
        assert (
            bare.post(
                "/api/settings/admin-password", json={"password": "x"}, headers=headers
            ).status_code
            == 403
        )
        # And every other setting write. A general write could loosen the proxy trust the
        # login lockout keys on; a Plex-connection write could hand the stored token to an
        # attacker's address. Both were reachable before the allowlist inversion.
        assert (
            bare.put(
                "/api/settings/general", json={"application_name": "x"}, headers=headers
            ).status_code
            == 403
        )
        assert (
            bare.put(
                "/api/settings/plex/connection",
                json={"uri": "https://attacker.example"},
                headers=headers,
            ).status_code
            == 403
        )
        assert (
            bare.put("/api/logs/level", json={"level": "debug"}, headers=headers).status_code == 403
        )

    def test_the_key_cannot_read_the_logs(self, client: TestClient) -> None:
        """The logs are a running transcript of who watched what, and the download is
        every rotating file at once. The operator is told a key can "read your library" --
        the catalog, not everyone's viewing (S-3)."""
        key = self._issue(client)
        bare = _bare(client)
        headers = {"X-Api-Key": key}

        assert bare.get("/api/logs", headers=headers).status_code == 403
        assert bare.get("/api/logs/download", headers=headers).status_code == 403
        # Still readable in the browser, where the Logs tab lives.
        assert client.get("/api/logs").status_code == 200

    def test_the_allowlist_matches_by_method_and_shape(self) -> None:
        # Reads are open to the key, except the handful that hand back more than a catalog.
        assert _api_key_allowed("GET", "/api/candidates") is True
        assert _api_key_allowed("GET", "/api/settings/general") is True
        assert _api_key_allowed("GET", "/api/settings/general/api-key") is False
        assert _api_key_allowed("GET", "/api/settings/backup/download") is False
        assert _api_key_allowed("GET", "/api/logs") is False
        assert _api_key_allowed("GET", "/api/logs/download") is False
        # Writes are closed except the automation allowlist: scan, plan, policy.
        assert _api_key_allowed("POST", "/api/scan/start") is True
        assert _api_key_allowed("POST", "/api/policy") is True
        assert _api_key_allowed("POST", "/api/runs/12/dry-run") is True
        assert _api_key_allowed("POST", "/api/runs/12/execute") is False
        assert _api_key_allowed("PUT", "/api/settings/safety") is False
        assert _api_key_allowed("PUT", "/api/settings/general") is False
        assert _api_key_allowed("PUT", "/api/settings/plex/connection") is False


class TestTheDocsLockdown:
    def test_the_stock_docs_are_gone(self, client: TestClient) -> None:
        """The old routes no longer serve the API description to the unauthenticated.
        With the built SPA present they fall back to its index.html (any unknown path
        does); what matters is that no schema and no reference UI comes back."""
        bare = _bare(client)
        assert "swagger" not in bare.get("/docs").text.lower()
        assert '"openapi"' not in bare.get("/openapi.json").text
        assert "redoc" not in bare.get("/redoc").text.lower()

    def test_the_reference_needs_a_session(self, client: TestClient) -> None:
        bare = _bare(client)
        assert bare.get("/api/docs").status_code == 401
        assert bare.get("/api/openapi.json").status_code == 401

        page = client.get("/api/docs")
        assert page.status_code == 200
        assert "/vendor/scalar.js" in page.text

    def test_the_schema_declares_the_api_key_scheme(self, client: TestClient) -> None:
        schema = client.get("/api/openapi.json").json()
        scheme = schema["components"]["securitySchemes"]["ApiKey"]
        assert scheme["in"] == "header"
        assert scheme["name"] == "X-Api-Key"


def _request(
    *,
    peer: str,
    forwarded: str | None = None,
    proxies: tuple[object, ...] = (),
) -> Request:
    class _AppState:
        trusted_proxies = proxies

    class _App:
        state = _AppState()

    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/health",
        "headers": headers,
        "client": (peer, 1234),
        "app": _App(),
    }
    return Request(scope)


class TestClientIp:
    def test_without_trust_the_peer_answers(self) -> None:
        request = _request(peer="203.0.113.9", forwarded="198.51.100.7")
        assert client_ip(request) == "203.0.113.9"

    def test_a_trusted_proxy_reveals_the_visitor(self) -> None:
        proxies = parse_proxy_networks(["172.16.0.0/12"])
        request = _request(peer="172.16.0.1", forwarded="198.51.100.7", proxies=proxies)
        assert client_ip(request) == "198.51.100.7"

    def test_the_walk_skips_trusted_hops_right_to_left(self) -> None:
        proxies = parse_proxy_networks(["172.16.0.0/12"])
        request = _request(peer="172.16.0.1", forwarded="198.51.100.7, 172.16.5.5", proxies=proxies)
        assert client_ip(request) == "198.51.100.7"

    def test_an_untrusted_peer_cannot_claim_to_forward(self) -> None:
        proxies = parse_proxy_networks(["172.16.0.0/12"])
        request = _request(peer="203.0.113.9", forwarded="198.51.100.7", proxies=proxies)
        assert client_ip(request) == "203.0.113.9"

    def test_a_malformed_chain_falls_back_to_the_peer(self) -> None:
        proxies = parse_proxy_networks(["172.16.0.0/12"])
        request = _request(peer="172.16.0.1", forwarded="not-an-ip", proxies=proxies)
        assert client_ip(request) == "172.16.0.1"

    def test_malformed_stored_entries_trust_nobody_extra(self) -> None:
        assert parse_proxy_networks(["nonsense", "", "10.0.0.1"]) == parse_proxy_networks(
            ["10.0.0.1"]
        )


class TestTheLogRing:
    def test_incremental_polling_by_sequence(self) -> None:
        ring = logbuffer.LogRing(maxlen=10)
        for n in range(3):
            ring.append(ts=f"t{n}", level="info", text=f"line {n}")

        first = ring.since(0)
        assert [line.text for line in first] == ["line 0", "line 1", "line 2"]
        assert ring.since(first[-1].seq) == []

        ring.append(ts="t3", level="warning", text="line 3")
        fresh = ring.since(first[-1].seq)
        assert [line.text for line in fresh] == ["line 3"]
        assert fresh[0].level == "WARNING"

    def test_the_window_is_bounded(self) -> None:
        ring = logbuffer.LogRing(maxlen=5)
        for n in range(20):
            ring.append(ts="t", level="info", text=f"line {n}")
        held = ring.since(0, limit=500)
        assert len(held) == 5
        assert held[-1].text == "line 19"

    def test_secrets_never_reach_the_ring(self, client: TestClient) -> None:
        """The capture processor sits after redact_secrets: a credential logged as a
        key-value must arrive scrubbed. The client fixture guarantees logging is
        configured the way production configures it."""
        before = logbuffer.RING.last_seq()
        structlog.get_logger("test").warning("test.secret_event", apikey="super-secret")
        lines = [line for line in logbuffer.RING.since(before) if "secret_event" in line.text]
        assert lines, "the event should have been captured"
        assert "super-secret" not in lines[0].text
        assert "[redacted]" in lines[0].text


class TestTheLogsRoutes:
    def test_reading_needs_a_session_and_pages_by_cursor(self, client: TestClient) -> None:
        assert _bare(client).get("/api/logs").status_code == 401

        page = client.get("/api/logs").json()
        assert page["level"] in ("DEBUG", "INFO", "WARNING")
        assert page["last_seq"] >= 0
        again = client.get(f"/api/logs?after={page['last_seq']}").json()
        assert all(line["seq"] > page["last_seq"] for line in again["lines"])

    def test_the_level_applies_immediately_and_persists(self, client: TestClient) -> None:
        try:
            response = client.put("/api/logs/level", json={"level": "debug"})
            assert response.status_code == 200
            assert response.json()["level"] == "DEBUG"
            assert logbuffer.level_name() == "DEBUG"

            # Debug lines now flow into the ring.
            before = logbuffer.RING.last_seq()
            structlog.get_logger("test").debug("test.debug_line", marker=int(time.time()))
            assert any("test.debug_line" in line.text for line in logbuffer.RING.since(before))
        finally:
            client.put("/api/logs/level", json={"level": "INFO"})

    def test_only_the_offered_levels_are_accepted(self, client: TestClient) -> None:
        response = client.put("/api/logs/level", json={"level": "CRITICAL"})
        assert response.status_code == 422

    def test_the_response_reports_how_many_files_are_kept(self, client: TestClient) -> None:
        # I-6: the Logs tab renders this instead of hardcoding "3", so the copy tracks the
        # backend retention constant.
        page = client.get("/api/logs").json()
        assert page["files_kept"] == logbuffer.LOG_BACKUP_COUNT + 1


class TestTheLogDownload:
    """The full log downloads as one timestamped text file, behind the session, and the
    on-disk copy is redacted exactly as the ring is (it is fed from the same place)."""

    def test_downloading_needs_a_session(self, client: TestClient) -> None:
        assert _bare(client).get("/api/logs/download").status_code == 401

    def test_the_download_is_an_attachment_carrying_the_trail(self, client: TestClient) -> None:
        marker = f"download.marker_{time.time_ns()}"
        structlog.get_logger("test").info(marker)

        response = client.get("/api/logs/download")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        assert ".log" in disposition
        assert marker in response.text  # the line we just logged is on disk and served back

    def test_secrets_never_reach_the_download(self, client: TestClient) -> None:
        structlog.get_logger("test").warning("download.secret_probe", apikey="super-secret")
        body = client.get("/api/logs/download").text
        assert "download.secret_probe" in body
        assert "super-secret" not in body
        assert "[redacted]" in body

    def test_a_degraded_sink_appends_the_ring_after_the_files(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PR-2: when the on-disk mirror stopped accepting writes mid-run, the files end where
        # writing failed. The download appends the in-memory ring behind a marker so recent
        # lines that never reached disk are still carried, rather than ending silently.
        marker = f"degraded.ring_tail_{time.time_ns()}"
        structlog.get_logger("test").info(marker)
        monkeypatch.setattr(logbuffer, "file_sink_healthy", lambda: False)

        body = client.get("/api/logs/download").text
        assert "Log file writing failed at some point above" in body
        assert marker in body

    def test_a_healthy_sink_does_not_append_the_ring(self, client: TestClient) -> None:
        # The append marker is present only when degraded; a healthy download is the files alone.
        body = client.get("/api/logs/download").text
        assert "Log file writing failed at some point above" not in body
