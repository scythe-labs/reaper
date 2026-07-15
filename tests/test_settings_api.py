# SPDX-License-Identifier: AGPL-3.0-or-later
"""The configuration surface: instances, safety, schedule, setup status.

These are the routes a first-run install lives on -- adding the services Reaper reads
from, seeing what is left to set up, and throwing the emergency stop. The load-bearing
properties, each pinned here:

* an API key goes in encrypted and never comes back out;
* the emergency stop can only make Reaper *safer* -- it subtracts from the host ceiling,
  never adds to it;
* the setup status tells the wizard exactly what is still missing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine

from reaper.config import Settings
from reaper.db.base import Base
from reaper.main import create_app

from ._auth import TEST_PASSWORD, login


def _make(tmp_path: Path, **overrides: object) -> Settings:
    settings = Settings(data_dir=tmp_path, secret_key="k", **overrides)  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return settings


async def _no_catch_up(*_args: object, **_kwargs: object) -> None:
    return None


def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the app's startup off the network and off a developer's local ``.env``.

    Two things the lifespan does that a unit test must not: seed instances from a real
    ``.env.local`` (the seeder would silently pre-populate an install these tests assert is
    empty), and run the startup catch-up, which downloads the ~280 MB IMDb ratings dataset the
    first time it sees an empty cache -- a real network fetch that races teardown and makes the
    suite slow and flaky. Both are stubbed out here so the tests are hermetic and match CI."""
    monkeypatch.setattr("reaper.main.load_raw_env", lambda _s: {})
    monkeypatch.setattr("reaper.main.catch_up_on_startup", _no_catch_up)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    _hermetic(monkeypatch)
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

        # Right password: deletion turns on, and the health banner reflects it.
        ok = client.put("/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD})
        assert ok.status_code == 200, ok.text
        assert ok.json()["destructive_enabled"] is True
        assert client.get("/api/health").json()["destructive_actions_enabled"] is True

    def test_turning_deletion_off_needs_no_password(self, client: TestClient) -> None:
        client.put("/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD})
        off = client.put("/api/settings/safety", json={"enabled": False})
        assert off.status_code == 200
        assert off.json()["destructive_enabled"] is False

    def test_setting_a_new_admin_password_then_enabling_with_it(self, client: TestClient) -> None:
        set_pw = client.post("/api/settings/admin-password", json={"password": "brandnew12345"})
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
        resp = client.post("/api/settings/admin-password", json={"password": "short"})
        assert resp.status_code == 422


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
        }

    def test_unlinking_when_nothing_is_linked_is_a_noop(self, client: TestClient) -> None:
        assert client.delete("/api/settings/plex").json() == {"removed": False}
