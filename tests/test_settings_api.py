# SPDX-License-Identifier: AGPL-3.0-or-later
"""The configuration surface: instances, safety, schedule, setup status.

These are the routes a first-run install lives on -- adding the services Reaper reads
from, seeing what is left to set up, and turning deletion on and off. The load-bearing
properties, each pinned here:

* an API key goes in encrypted and never comes back out;
* a fresh install starts read-only, and the asymmetry holds: turning deletion ON needs
  the admin password, turning it OFF needs nothing;
* the setup status tells the wizard exactly what is still missing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine

from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import InstanceKind
from reaper.main import create_app
from reaper.services import instances as instances_service

from ._auth import TEST_PASSWORD, login


def _make(tmp_path: Path, **overrides: object) -> Settings:
    settings = Settings(data_dir=tmp_path, secret_key="k", **overrides)  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return settings


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    # Startup seeding and the catch-up network fetch are stubbed for every test by the
    # autouse ``_hermetic`` fixture in conftest.py, so booting the app here is safe.
    settings = _make(tmp_path)
    with TestClient(create_app(settings)) as c:
        login(c, settings)  # seeds a local admin whose password is TEST_PASSWORD
        yield c


class TestInstancesCrud:
    def test_a_created_instance_lists_without_its_key(self, client: TestClient) -> None:
        created = client.post(
            "/api/settings/instances",
            json={
                "kind": "radarr",
                "name": "HD",
                "base_url": "http://radarr.local:7878/",
                "api_key": "super-secret",
            },
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["kind"] == "radarr"
        assert body["has_key"] is True
        assert body["base_url"] == "http://radarr.local:7878"  # trailing slash stripped
        # The key is never serialised, under any field name.
        assert "super-secret" not in created.text

        listed = client.get("/api/settings/instances").json()
        assert [i["name"] for i in listed] == ["HD"]
        assert "super-secret" not in client.get("/api/settings/instances").text

    def test_a_duplicate_name_is_refused(self, client: TestClient) -> None:
        payload = {
            "kind": "radarr",
            "name": "HD",
            "base_url": "http://a.local",
            "api_key": "k1",
        }
        assert client.post("/api/settings/instances", json=payload).status_code == 200
        clash = client.post("/api/settings/instances", json=payload)
        assert clash.status_code == 409

    def test_a_blank_name_is_a_validation_error_not_a_conflict(self, client: TestClient) -> None:
        """Only a name clash is a 409. A blank required field is the caller's payload
        being wrong, and calling it a conflict misdirects whoever reads the error."""
        response = client.post(
            "/api/settings/instances",
            json={"kind": "radarr", "name": "   ", "base_url": "http://a.local", "api_key": "k"},
        )
        assert response.status_code == 422

    def test_renaming_into_an_existing_name_is_a_conflict_not_a_not_found(
        self, client: TestClient
    ) -> None:
        """Two instances exist; renaming the second onto the first's name is a 409
        (a well-formed request that collides), never a 404 (which reads as 'no such
        instance' and misleads the caller into thinking the target vanished)."""
        base = {"kind": "radarr", "base_url": "http://a.local", "api_key": "k"}
        assert (
            client.post("/api/settings/instances", json={**base, "name": "HD"}).status_code == 200
        )
        second = client.post("/api/settings/instances", json={**base, "name": "UHD"})
        assert second.status_code == 200
        second_id = second.json()["id"]

        clash = client.put(f"/api/settings/instances/{second_id}", json={"name": "HD"})
        assert clash.status_code == 409

        missing = client.put("/api/settings/instances/9999", json={"name": "Whatever"})
        assert missing.status_code == 404  # a genuinely absent instance still 404s

    def test_updating_without_a_key_keeps_the_stored_one(self, client: TestClient) -> None:
        created = client.post(
            "/api/settings/instances",
            json={"kind": "tautulli", "name": "T", "base_url": "http://t.local", "api_key": "orig"},
        ).json()

        updated = client.put(
            f"/api/settings/instances/{created['id']}",
            json={"base_url": "http://t2.local", "enabled": False},  # no api_key
        )
        assert updated.status_code == 200, updated.text
        body = updated.json()
        assert body["base_url"] == "http://t2.local"
        assert body["enabled"] is False
        assert body["has_key"] is True  # the key survived the keyless update

    def test_a_deleted_instance_is_gone(self, client: TestClient) -> None:
        created = client.post(
            "/api/settings/instances",
            json={"kind": "seerr", "name": "S", "base_url": "http://s.local", "api_key": "k"},
        ).json()
        assert client.delete(f"/api/settings/instances/{created['id']}").json() == {"removed": True}
        assert client.get("/api/settings/instances").json() == []

    def test_an_unknown_kind_is_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/api/settings/instances",
            json={"kind": "plexarr", "name": "X", "base_url": "http://x", "api_key": "k"},
        )
        assert resp.status_code == 422

    def test_certificate_checking_defaults_on_and_survives_unrelated_updates(
        self, client: TestClient
    ) -> None:
        """``verify_tls`` is on unless the operator turns it off, an explicit off
        round-trips, and an update that never mentions it leaves the choice alone --
        omitted must mean "unchanged", never "back to the default"."""
        created = client.post(
            "/api/settings/instances",
            json={"kind": "radarr", "name": "HD", "base_url": "https://a.local", "api_key": "k"},
        ).json()
        assert created["verify_tls"] is True

        off = client.post(
            "/api/settings/instances",
            json={
                "kind": "radarr",
                "name": "UHD",
                "base_url": "https://b.local",
                "api_key": "k",
                "verify_tls": False,
            },
        ).json()
        assert off["verify_tls"] is False

        renamed = client.put(f"/api/settings/instances/{off['id']}", json={"name": "4K"}).json()
        assert renamed["verify_tls"] is False  # untouched by an unrelated update

        back_on = client.put(
            f"/api/settings/instances/{off['id']}", json={"verify_tls": True}
        ).json()
        assert back_on["verify_tls"] is True

        off_again = client.put(
            f"/api/settings/instances/{off['id']}", json={"verify_tls": False}
        ).json()
        assert off_again["verify_tls"] is False


class TestConnectionTestsHonourTheTlsChoice:
    """The TLS choice must reach the client that actually dials out -- the stored
    ``verify_tls`` for a saved instance, and the checkbox value sent with the request
    for the pre-save test on the add form."""

    async def test_test_connection_builds_its_client_with_the_given_verify(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[bool] = []

        class FakeClient:
            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def system_status(self) -> dict[str, str]:
                return {"version": "1.0", "appName": "Radarr"}

        def fake_client(
            kind: InstanceKind, base_url: str, api_key: str, *, verify: bool = True
        ) -> FakeClient:
            seen.append(verify)
            return FakeClient()

        monkeypatch.setattr(instances_service, "_client", fake_client)
        result = await instances_service.test_connection(
            InstanceKind.RADARR, "https://a.local", "k", verify=False
        )
        assert result.ok is True
        assert seen == [False]

    def test_a_saved_instances_stored_choice_is_used(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created = client.post(
            "/api/settings/instances",
            json={
                "kind": "tautulli",
                "name": "T",
                "base_url": "https://t.local",
                "api_key": "k",
                "verify_tls": False,
            },
        ).json()

        seen: list[bool] = []

        async def fake_test(
            kind: InstanceKind, base_url: str, api_key: str, *, verify: bool = True
        ) -> instances_service.TestResult:
            seen.append(verify)
            return instances_service.TestResult(ok=True, detail="Connected.")

        monkeypatch.setattr(instances_service, "test_connection", fake_test)
        resp = client.post(f"/api/settings/instances/{created['id']}/test")
        assert resp.status_code == 200
        assert seen == [False]

    def test_the_pre_save_test_carries_the_checkbox_value(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[bool] = []

        async def fake_test(
            kind: InstanceKind, base_url: str, api_key: str, *, verify: bool = True
        ) -> instances_service.TestResult:
            seen.append(verify)
            return instances_service.TestResult(ok=True, detail="Connected.")

        monkeypatch.setattr(instances_service, "test_connection", fake_test)
        resp = client.post(
            "/api/settings/instances/test",
            json={
                "kind": "radarr",
                "base_url": "https://a.local",
                "api_key": "k",
                "verify_tls": False,
            },
        )
        assert resp.status_code == 200
        assert seen == [False]


class TestSafety:
    def test_it_starts_read_only(self, client: TestClient) -> None:
        before = client.get("/api/settings/safety").json()
        assert before["destructive_enabled"] is False
        assert before["has_password"] is True  # the seeded local admin

    def test_turning_deletion_on_requires_the_admin_password(self, client: TestClient) -> None:
        # Wrong password: refused, and deletion stays off.
        wrong = client.put("/api/settings/safety", json={"enabled": True, "password": "nope"})
        assert wrong.status_code == 403
        assert client.get("/api/settings/safety").json()["destructive_enabled"] is False

        # Right password: deletion turns on, and the settings surface (which the safety
        # banner reads) reflects it. /api/health deliberately says nothing about it.
        ok = client.put("/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD})
        assert ok.status_code == 200, ok.text
        assert ok.json()["destructive_enabled"] is True
        assert client.get("/api/settings/safety").json()["destructive_enabled"] is True
        assert "destructive_actions_enabled" not in client.get("/api/health").json()

    def test_turning_deletion_off_needs_no_password(self, client: TestClient) -> None:
        client.put("/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD})
        off = client.put("/api/settings/safety", json={"enabled": False})
        assert off.status_code == 200
        assert off.json()["destructive_enabled"] is False

    def test_setting_a_new_admin_password_then_enabling_with_it(self, client: TestClient) -> None:
        set_pw = client.post(
            "/api/settings/admin-password",
            json={"password": "brandnew12345", "current_password": TEST_PASSWORD},
        )
        assert set_pw.status_code == 200, set_pw.text
        # The old password no longer works; the new one does.
        assert (
            client.put(
                "/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD}
            ).status_code
            == 403
        )
        enabled = client.put(
            "/api/settings/safety", json={"enabled": True, "password": "brandnew12345"}
        )
        assert enabled.json()["destructive_enabled"] is True

    def test_a_too_short_password_is_refused(self, client: TestClient) -> None:
        resp = client.post(
            "/api/settings/admin-password",
            json={"password": "short", "current_password": TEST_PASSWORD},
        )
        assert resp.status_code == 422

    def test_changing_the_password_requires_the_current_one(self, client: TestClient) -> None:
        """A borrowed signed-in session must not be able to swap the arming credential.
        The seeded admin already has a password, so omitting (or flubbing) the current
        one is refused and nothing changes."""
        omitted = client.post("/api/settings/admin-password", json={"password": "brandnew12345"})
        assert omitted.status_code == 403
        wrong = client.post(
            "/api/settings/admin-password",
            json={"password": "brandnew12345", "current_password": "not-it"},
        )
        assert wrong.status_code == 403
        # The original password still arms deletion: nothing was changed.
        armed = client.put(
            "/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD}
        )
        assert armed.status_code == 200, armed.text

    def test_repeated_wrong_arming_passwords_are_locked_out(self, client: TestClient) -> None:
        """Arming is a password-guessing surface: past the threshold, further attempts
        get a 429 with Retry-After instead of another Argon2 verify."""
        codes = [
            client.put(
                "/api/settings/safety", json={"enabled": True, "password": f"wrong-{n}"}
            ).status_code
            for n in range(6)
        ]
        assert codes[:5] == [403] * 5
        assert codes[5] == 429


class TestSetupStatus:
    def test_a_bare_install_is_not_scan_ready(self, client: TestClient) -> None:
        status = client.get("/api/setup/status").json()
        assert status["admin_exists"] is True  # the test admin
        assert status["scan_ready"] is False
        assert status["complete"] is False

    def test_a_radarr_and_tautulli_make_it_scan_ready(self, client: TestClient) -> None:
        for kind, name in [("radarr", "HD"), ("tautulli", "T")]:
            client.post(
                "/api/settings/instances",
                json={"kind": kind, "name": name, "base_url": f"http://{name}", "api_key": "k"},
            )
        status = client.get("/api/setup/status").json()
        assert status["has_radarr"] is True
        assert status["has_tautulli"] is True
        assert status["scan_ready"] is True
        assert status["complete"] is False  # ready, but no scan has run yet

    def test_a_sonarr_and_tautulli_also_make_it_scan_ready(self, client: TestClient) -> None:
        """A TV-only deployment is a real deployment: Sonarr counts as the library
        source exactly like Radarr does."""
        for kind, name in [("sonarr", "TV"), ("tautulli", "T")]:
            client.post(
                "/api/settings/instances",
                json={"kind": kind, "name": name, "base_url": f"http://{name}", "api_key": "k"},
            )
        status = client.get("/api/setup/status").json()
        assert status["has_radarr"] is False
        assert status["has_sonarr"] is True
        assert status["scan_ready"] is True

    def test_tautulli_alone_is_not_scan_ready(self, client: TestClient) -> None:
        client.post(
            "/api/settings/instances",
            json={"kind": "tautulli", "name": "T", "base_url": "http://t", "api_key": "k"},
        )
        assert client.get("/api/setup/status").json()["scan_ready"] is False


class TestSchedule:
    def test_the_maintenance_jobs_are_listed(self, client: TestClient) -> None:
        schedule = client.get("/api/settings/schedule").json()
        ids = {j["id"] for j in schedule["jobs"]}
        assert "refresh_ratings" in ids
        assert schedule["scan_cron"] is None  # no automatic scan by default

    def test_a_valid_cron_is_stored_and_a_bad_one_refused(self, client: TestClient) -> None:
        ok = client.put("/api/settings/schedule", json={"scan_cron": "30 4 * * *"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["scan_cron"] == "30 4 * * *"
        assert any(j["id"] == "scheduled_scan" for j in ok.json()["jobs"])

        bad = client.put("/api/settings/schedule", json={"scan_cron": "not a cron"})
        assert bad.status_code == 422

        # Clearing it removes the job again.
        cleared = client.put("/api/settings/schedule", json={"scan_cron": None}).json()
        assert cleared["scan_cron"] is None
        assert not any(j["id"] == "scheduled_scan" for j in cleared["jobs"])


class TestRunJob:
    def test_a_known_maintenance_job_can_be_run_now(self, client: TestClient) -> None:
        # Pause the scheduler first so "run now" moves the job's next-run without actually
        # firing the real, network-touching job inside the test. The endpoint's job is only to
        # nudge the schedule; the work itself is APScheduler's, tested nowhere near here.
        client.app.state.scheduler.pause()  # type: ignore[attr-defined]
        resp = client.post("/api/settings/jobs/refresh_curated_lists/run", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "started", "job": "refresh_curated_lists"}

    def test_an_unknown_job_is_a_404(self, client: TestClient) -> None:
        assert client.post("/api/settings/jobs/not_a_job/run", json={}).status_code == 404

    def test_the_scan_is_not_runnable_here(self, client: TestClient) -> None:
        # The library scan runs through the streaming /api/scan endpoint (so the UI can show
        # progress), never this fire-and-forget one -- it is deliberately not on the list.
        assert client.post("/api/settings/jobs/scheduled_scan/run", json={}).status_code == 404


class TestPoster:
    def test_no_tautulli_means_a_404_not_a_crash(self, client: TestClient) -> None:
        """With nothing to fetch artwork from, the poster route 404s and the card falls back
        to a placeholder -- it never 500s."""
        assert client.get("/api/poster/123").status_code == 404


class TestPlexStatus:
    def test_an_unlinked_server_reports_so(self, client: TestClient) -> None:
        assert client.get("/api/settings/plex").json() == {
            "linked": False,
            "name": None,
            "connection_uri": None,
            "last_ok_at": None,
            # Present whether or not a server is linked: links need somewhere to point.
            "web_url": "https://app.plex.tv",
        }

    def test_unlinking_when_nothing_is_linked_is_a_noop(self, client: TestClient) -> None:
        assert client.delete("/api/settings/plex").json() == {"removed": False}


def _plex_resource(machine_id: str, name: str) -> dict[str, object]:
    return {
        "name": name,
        "clientIdentifier": machine_id,
        "owned": True,
        "provides": "server",
        "accessToken": "server-token",
        "connections": [
            {
                "uri": "https://x.plex.direct:32400",
                "address": "10.0.0.2",
                "port": 32400,
                "local": True,
                "relay": False,
                "protocol": "https",
            }
        ],
    }


class TestPlexLinkChoice:
    """The Settings-page link flow for an account owning several servers: the exact
    API contract the PlexPanel picker consumes. The login-time twin of this flow is
    pinned in test_sessions; this pins the in-app route."""

    def _mock_plextv(self) -> None:
        respx.post("https://plex.tv/api/v2/pins").mock(
            return_value=httpx.Response(201, json={"id": 42, "code": "ABCD"})
        )
        respx.get("https://plex.tv/api/v2/pins/42").mock(
            return_value=httpx.Response(200, json={"id": 42, "authToken": "tok"})
        )
        respx.get("https://plex.tv/api/v2/user").mock(
            return_value=httpx.Response(200, json={"id": 7, "username": "owner"})
        )
        respx.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(
                200,
                json=[_plex_resource("machine-a", "Den"), _plex_resource("machine-b", "Attic")],
            )
        )
        respx.get("https://x.plex.direct:32400/identity").mock(
            return_value=httpx.Response(200, json={})
        )

    def test_a_multi_server_account_gets_the_choices_and_the_pin_survives(
        self, client: TestClient
    ) -> None:
        with respx.mock:
            self._mock_plextv()
            start = client.post("/api/settings/plex/link/start").json()

            first = client.post("/api/settings/plex/link/poll", json={"pin_id": start["pin_id"]})
            assert first.status_code == 200, first.text
            body = first.json()
            assert body["status"] == "choose_server"
            assert {(s["name"], s["machine_identifier"]) for s in body["servers"]} == {
                ("Den", "machine-a"),
                ("Attic", "machine-b"),
            }

            # The choice did not burn the PIN: the SAME sign-in finishes with the pick.
            done = client.post(
                "/api/settings/plex/link/poll",
                json={"pin_id": start["pin_id"], "machine_identifier": "machine-b"},
            )
            assert done.status_code == 200, done.text
            assert done.json()["status"] == "ok"
            assert done.json()["server"]["name"] == "Attic"

        assert client.get("/api/settings/plex").json()["linked"] is True

    def test_a_pick_matching_nothing_owned_fails_closed(self, client: TestClient) -> None:
        with respx.mock:
            self._mock_plextv()
            start = client.post("/api/settings/plex/link/start").json()

            bad = client.post(
                "/api/settings/plex/link/poll",
                json={"pin_id": start["pin_id"], "machine_identifier": "somebody-elses-machine"},
            )
            assert bad.status_code == 400
            assert "No server this account owns" in bad.json()["detail"]

            # The refusal consumed the PIN, so the obtained token cannot be replayed.
            retry = client.post(
                "/api/settings/plex/link/poll",
                json={"pin_id": start["pin_id"], "machine_identifier": "machine-b"},
            )
            assert retry.status_code == 400

        assert client.get("/api/settings/plex").json()["linked"] is False
