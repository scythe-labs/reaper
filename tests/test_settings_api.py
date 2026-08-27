# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests the configuration surface: instances, safety, schedule, setup status.

These are the routes a first-run install lives on: adding the services Reaper reads from,
seeing what is left to set up, and turning deletion on and off. The properties pinned here:

* An API key goes in encrypted and never comes back out.
* A fresh install starts read-only. Turning deletion on needs the admin password. Turning
  it off needs nothing.
* The setup status tells the wizard exactly what is still missing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

import httpx
import httpx2
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from starlette.requests import Request

import reaper.api.settings
import reaper.refusal
from reaper.api.plex import PlexUpdateIn, update_plex_settings
from reaper.auth.ratelimit import argon2_gate
from reaper.clients.base import IntegrationError
from reaper.clients.plex import PlexClient, PlexError, PlexSection
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import InstanceKind, PlexServer, Snapshot
from reaper.engine.reason import Reason, from_wire, to_wire
from reaper.services import instances as instances_service

from ._auth import TEST_PASSWORD, clear_admin_password
from ._reasons import catalog
from ._reasons import text as reason_text

pytestmark = pytest.mark.httpx2(assert_all_called=False)


def _make(tmp_path: Path, **overrides: object) -> Settings:
    settings = Settings(data_dir=tmp_path, secret_key="k", **overrides)  # type: ignore[arg-type]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return settings


def _add_snapshot(client: TestClient) -> None:
    """Put one Snapshot row in, so ``has_scanned`` is true.

    The setup status only asks whether any snapshot exists, so this is the smallest row
    that satisfies it rather than a scored one. Nothing here reads its contents.
    """
    settings: Settings = client.app.state.settings  # type: ignore[attr-defined]
    engine = sa_create_engine(settings.sync_database_url)
    now = utcnow()
    with Session(engine) as session:
        session.add(
            Snapshot(
                created_at=now,
                policy_hash="p",
                scoring_hash="s",
                horizon_at=now,
                item_count=0,
                degraded=False,
            )
        )
        session.commit()
    engine.dispose()


def _make_scan_ready(client: TestClient) -> None:
    """The smallest configuration ``scan_ready`` accepts: a Tautulli and one *arr."""
    for kind, name in [("radarr", "HD"), ("tautulli", "T")]:
        client.post(
            "/api/settings/instances",
            json={"kind": kind, "name": name, "base_url": f"http://{name}", "api_key": "k"},
        )


def _link_plex(client: TestClient) -> None:
    """Put one PlexServer row in, so ``plex_linked`` is true.

    Written straight to the table for the same reason ``_add_snapshot`` is: the setup status
    only asks whether a row exists, and the real path there is a plex.tv OAuth round trip.
    """
    settings: Settings = client.app.state.settings  # type: ignore[attr-defined]
    engine = sa_create_engine(settings.sync_database_url)
    with Session(engine) as session:
        session.add(
            PlexServer(
                machine_identifier="abc123",
                name="Example Server",
                connection_uri="http://plex.local:32400",
                token_enc="enc",
                created_at=utcnow(),
            )
        )
        session.commit()
    engine.dispose()


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
        # The key is never serialized, under any field name.
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

    def test_a_second_tautulli_is_refused(self, client: TestClient) -> None:
        """Tautulli is a singleton. It mirrors one Plex's watch history, and Reaper
        connects to one Plex, so a second one, even under a different name and URL, gets
        a 409, never a second row the scan would silently ignore."""
        first = client.post(
            "/api/settings/instances",
            json={
                "kind": "tautulli",
                "name": "Main",
                "base_url": "http://t1.local",
                "api_key": "k1",
            },
        )
        assert first.status_code == 200, first.text
        second = client.post(
            "/api/settings/instances",
            json={
                "kind": "tautulli",
                "name": "Other",
                "base_url": "http://t2.local",
                "api_key": "k2",
            },
        )
        assert second.status_code == 409, second.text
        # Only the one survived.
        listed = client.get("/api/settings/instances").json()
        assert [i["name"] for i in listed if i["kind"] == "tautulli"] == ["Main"]

    def test_multiple_seerr_and_arr_instances_are_allowed(self, client: TestClient) -> None:
        """Radarr, Sonarr and Seerr are genuinely multi (HD + 4K servers, two request
        portals): a second of each, under its own name, is accepted."""
        for kind, name in [
            ("radarr", "HD"),
            ("radarr", "4K"),
            ("sonarr", "HD"),
            ("sonarr", "4K"),
            ("seerr", "Main"),
            ("seerr", "Second"),
        ]:
            resp = client.post(
                "/api/settings/instances",
                json={
                    "kind": kind,
                    "name": name,
                    "base_url": f"http://{kind}-{name}.local",
                    "api_key": "k",
                },
            )
            assert resp.status_code == 200, resp.text
        listed = client.get("/api/settings/instances").json()
        assert sum(1 for i in listed if i["kind"] == "seerr") == 2

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
        """Two instances exist. Renaming the second onto the first's name is a 409, a
        well-formed request that collides, never a 404, which reads as "no such instance"
        and misleads the caller into thinking the target vanished.
        """
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

    def test_every_route_answers_the_status_the_error_class_declares(
        self, client: TestClient
    ) -> None:
        """Covers the three reads on both arms: a missing instance, and a real instance of
        the wrong kind.

        ``services.instances`` declares one status per exception subclass, and the routes
        read it off the exception, so the mapping is one fact. But a fact stated once is
        still worth driving, since nothing else here would notice the declaration itself
        going wrong. A missing instance is 404 on all three. A real instance of the wrong
        kind is 422, since the request named something that exists and asked it for a
        thing that kind does not have. Neither arm reaches the network. Both refuse before
        a client is constructed.
        """
        tautulli = client.post(
            "/api/settings/instances",
            json={"kind": "tautulli", "name": "T", "base_url": "http://t.local", "api_key": "k"},
        )
        assert tautulli.status_code == 200, tautulli.text
        wrong_kind = tautulli.json()["id"]

        for path in (
            "/api/settings/instances/9999/root-folders",
            "/api/settings/instances/9999/seerr-services",
        ):
            assert client.get(path).status_code == 404, path
        assert client.post("/api/settings/instances/9999/test").status_code == 404

        for path in (
            f"/api/settings/instances/{wrong_kind}/root-folders",
            f"/api/settings/instances/{wrong_kind}/seerr-services",
        ):
            response = client.get(path)
            assert response.status_code == 422, f"{path}: {response.text}"

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
        """``verify_tls`` is on unless the operator turns it off. An explicit off
        round-trips, and an update that never mentions it leaves the choice alone. Omitted
        must mean "unchanged", never "back to the default".
        """
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

    def test_the_redownload_block_defaults_off_and_round_trips(self, client: TestClient) -> None:
        """``add_import_exclusion`` is off unless the operator turns it on. An explicit on
        round-trips, and an update that never mentions it leaves the choice alone. Omitted
        must mean "unchanged", never "back to the default".
        """
        created = client.post(
            "/api/settings/instances",
            json={"kind": "radarr", "name": "HD", "base_url": "https://a.local", "api_key": "k"},
        ).json()
        assert created["add_import_exclusion"] is False

        on = client.post(
            "/api/settings/instances",
            json={
                "kind": "radarr",
                "name": "UHD",
                "base_url": "https://b.local",
                "api_key": "k",
                "add_import_exclusion": True,
            },
        ).json()
        assert on["add_import_exclusion"] is True

        renamed = client.put(f"/api/settings/instances/{on['id']}", json={"name": "4K"}).json()
        assert renamed["add_import_exclusion"] is True  # untouched by an unrelated update

        back_off = client.put(
            f"/api/settings/instances/{on['id']}", json={"add_import_exclusion": False}
        ).json()
        assert back_off["add_import_exclusion"] is False

    def test_external_url_is_optional_normalized_and_clearable(self, client: TestClient) -> None:
        """The link address is null unless set. It is normalized like base_url, survives
        an unrelated update, and a blank string clears it back to null, since links fall
        back to base_url. Omitting it leaves the stored value alone.
        """
        bare = client.post(
            "/api/settings/instances",
            json={"kind": "radarr", "name": "HD", "base_url": "http://a.local", "api_key": "k"},
        ).json()
        assert bare["external_url"] is None  # unset by default

        made = client.post(
            "/api/settings/instances",
            json={
                "kind": "radarr",
                "name": "UHD",
                "base_url": "http://b.local",
                "api_key": "k",
                "external_url": "https://radarr.example.com/",
            },
        ).json()
        assert made["external_url"] == "https://radarr.example.com"  # trailing slash stripped

        renamed = client.put(f"/api/settings/instances/{made['id']}", json={"name": "4K"}).json()
        assert renamed["external_url"] == "https://radarr.example.com"  # untouched

        moved = client.put(
            f"/api/settings/instances/{made['id']}",
            json={"external_url": "https://movies.example.com"},
        ).json()
        assert moved["external_url"] == "https://movies.example.com"

        cleared = client.put(
            f"/api/settings/instances/{made['id']}", json={"external_url": "  "}
        ).json()
        assert cleared["external_url"] is None  # blank clears to null

    def test_external_url_must_be_a_full_web_address(self, client: TestClient) -> None:
        """The link address is rendered into an href for every signed-in user, so a
        scheme-less paste or a non-http scheme is refused at the edge like every sibling
        URL field, never stored verbatim. A blank still clears. An update with a bad value
        changes nothing.
        """
        # A scheme-less host:port paste is refused on create.
        scheme_less = client.post(
            "/api/settings/instances",
            json={
                "kind": "radarr",
                "name": "HD",
                "base_url": "http://a.local",
                "api_key": "k",
                "external_url": "movies.example.com:8989",
            },
        )
        assert scheme_less.status_code == 422

        # A dangerous scheme is refused too.
        dangerous = client.post(
            "/api/settings/instances",
            json={
                "kind": "radarr",
                "name": "HD",
                "base_url": "http://a.local",
                "api_key": "k",
                "external_url": "javascript:alert(1)",
            },
        )
        assert dangerous.status_code == 422

        made = client.post(
            "/api/settings/instances",
            json={
                "kind": "radarr",
                "name": "HD",
                "base_url": "http://a.local",
                "api_key": "k",
                "external_url": "https://radarr.example.com",
            },
        ).json()

        # An update to a malformed value is refused and the stored one is untouched.
        rejected = client.put(
            f"/api/settings/instances/{made['id']}",
            json={"external_url": "not a url"},
        )
        assert rejected.status_code == 422
        still = client.get("/api/settings/instances").json()
        kept = next(row for row in still if row["id"] == made["id"])
        assert kept["external_url"] == "https://radarr.example.com"

    @pytest.mark.parametrize(
        "address",
        [
            "radarr.local:7878",  # a scheme-less host:port paste, the shape an operator types
            "javascript:alert(1)",  # a scheme, but not one anything here may dial
            "http://",  # a scheme with no host at all
            "not a url",
        ],
    )
    def test_the_service_address_must_be_a_full_web_address(
        self, client: TestClient, address: str
    ) -> None:
        """``base_url`` is where every Reaper request for this service goes, so a
        scheme-less address must never reach storage. Saved with no complaint, it would
        surface much later as a connection or scan failure, far from the box that was
        wrong. This is one shared check, at the edge, for every URL an operator types.

        The refused values are spelled out directly rather than derived from the
        validator's own branches.
        """
        on_create = client.post(
            "/api/settings/instances",
            json={"kind": "radarr", "name": "HD", "base_url": address, "api_key": "k"},
        )
        assert on_create.status_code == 422, on_create.text

        made = client.post(
            "/api/settings/instances",
            json={"kind": "radarr", "name": "HD", "base_url": "http://a.local", "api_key": "k"},
        ).json()
        on_update = client.put(f"/api/settings/instances/{made['id']}", json={"base_url": address})
        assert on_update.status_code == 422, on_update.text
        # Refused means nothing changed, not "refused and stored anyway".
        listed = client.get("/api/settings/instances").json()
        assert (
            next(row for row in listed if row["id"] == made["id"])["base_url"] == "http://a.local"
        )


class TestTheApiPathIsStoredAndUnreachable:
    """`Instance.api_path_prefix` feeds the *arr clients, and no route reads or writes it.

    The column is real work: the scan and Test Connection both hand it to the client, so
    the two always probe the same path. But it has only ever held its default, because
    nothing can set it. An unwritable field on the wire would only ever tell an operator a
    constant, which is a blocker, not a placeholder.

    Retiring the column would need a migration, and the baseline is frozen, so the column
    stays and the wire stays off. Both halves are pinned here: putting the field back on
    the response without also adding a writer fails, and so does a writer that lands
    without a surface.
    """

    def test_the_instance_response_does_not_carry_the_api_path(self, client: TestClient) -> None:
        created = client.post(
            "/api/settings/instances",
            json={
                "kind": "radarr",
                "name": "HD",
                "base_url": "http://radarr.local:7878",
                "api_key": "k",
            },
        )
        assert created.status_code == 200, created.text
        assert "api_path_prefix" not in created.json()

        listed = client.get("/api/settings/instances").json()
        assert listed and all("api_path_prefix" not in row for row in listed)

    def test_no_route_can_write_the_api_path(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Offered to create AND update, then read back through the value the client
        actually receives rather than off the column, so a writer landing on either route
        fails here.

        `/api/v9` is the value that would prove a writer exists. The assertion below is
        the shipped default, which only holds because neither route stored what was
        offered.
        """
        made = client.post(
            "/api/settings/instances",
            json={
                "kind": "radarr",
                "name": "HD",
                "base_url": "http://radarr.local:7878",
                "api_key": "k",
                "api_path_prefix": "/api/v9",
            },
        )
        assert made.status_code == 200, made.text
        instance_id = made.json()["id"]
        updated = client.put(
            f"/api/settings/instances/{instance_id}",
            json={"api_path_prefix": "/api/v9"},
        )
        assert updated.status_code == 200, updated.text

        prefixes: list[str | None] = []

        async def fake_test(
            kind: InstanceKind,
            base_url: str,
            api_key: str,
            *,
            verify: bool = True,
            api_path_prefix: str | None = None,
        ) -> instances_service.TestResult:
            prefixes.append(api_path_prefix)
            return instances_service.TestResult(
                ok=True, detail=Reason("legacy", {"text": "Connected."})
            )

        monkeypatch.setattr(instances_service, "test_connection", fake_test)
        assert client.post(f"/api/settings/instances/{instance_id}/test").status_code == 200
        assert prefixes == ["/api/v3"]

    def test_a_saved_instance_test_answers_the_verdict_and_no_mapping(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The route has no instance-less pass to read folders on, so it cannot answer the
        mapping fields, and its published shape must not say it may. This body was never
        asserted before, so a narrowing that went the wrong way had no guard.
        """
        made = client.post(
            "/api/settings/instances",
            json={
                "kind": "radarr",
                "name": "HD",
                "base_url": "http://radarr.local:7878",
                "api_key": "k",
            },
        )
        assert made.status_code == 200, made.text
        instance_id = made.json()["id"]

        async def fake_test(*_a: object, **_k: object) -> instances_service.TestResult:
            return instances_service.TestResult(
                ok=True, detail=Reason("legacy", {"text": "Connected."}), version="4.0.1"
            )

        monkeypatch.setattr(instances_service, "test_connection", fake_test)

        body = client.post(f"/api/settings/instances/{instance_id}/test").json()

        assert body == {
            "ok": True,
            "detail_reason": {"k": "legacy", "p": {"text": "Connected."}},
            "version": "4.0.1",
        }


class TestTheStoredTestResultDescribesWhatWasTested:
    """A connection test's outcome is stored on the instance row and rendered as the
    service card's badge, so it must describe the credentials in force, not the ones it
    was computed from before an edit.

    The green direction is the one that matters. A stale "Reached" tells the operator
    Reaper can reach the app it deletes through, when nothing has checked the address now
    configured.
    """

    @staticmethod
    def _pass_a_test(client: TestClient, monkeypatch: pytest.MonkeyPatch, instance_id: int) -> None:
        async def fake_test(
            kind: InstanceKind,
            base_url: str,
            api_key: str,
            *,
            verify: bool = True,
            api_path_prefix: str | None = None,
        ) -> instances_service.TestResult:
            return instances_service.TestResult(
                ok=True, detail=Reason("legacy", {"text": "Connected."}), version="4.0.1"
            )

        monkeypatch.setattr(instances_service, "test_connection", fake_test)
        assert client.post(f"/api/settings/instances/{instance_id}/test").status_code == 200

    @staticmethod
    def _row(client: TestClient, instance_id: int) -> dict[str, Any]:
        listed = client.get("/api/settings/instances").json()
        row: dict[str, Any] = next(r for r in listed if r["id"] == instance_id)
        return row

    def _saved_and_tested(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> dict[str, Any]:
        made: dict[str, Any] = client.post(
            "/api/settings/instances",
            json={"kind": "radarr", "name": "HD", "base_url": "http://a.local", "api_key": "k"},
        ).json()
        self._pass_a_test(client, monkeypatch, made["id"])
        stored = self._row(client, made["id"])
        # This precondition is asserted, not assumed. Without a stored pass to clear,
        # every case below would hold on an empty row and prove nothing.
        assert stored["last_ok_at"] is not None
        assert stored["detected_version"] == "4.0.1"
        return made

    @pytest.mark.parametrize(
        ("what_changed", "edit"),
        [
            ("the address", {"base_url": "http://b.local"}),
            ("the key", {"api_key": "rotated"}),
            ("the certificate check", {"verify_tls": False}),
        ],
    )
    def test_changing_what_was_tested_clears_the_stored_outcome(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        what_changed: str,
        edit: dict[str, object],
    ) -> None:
        """Each of the three inputs ``test_saved_instance`` computes its answer from,
        driven on its own. Nothing cleared these columns except a real test.
        """
        made = self._saved_and_tested(client, monkeypatch)

        assert client.put(f"/api/settings/instances/{made['id']}", json=edit).status_code == 200

        after = self._row(client, made["id"])
        assert after["last_ok_at"] is None, f"a pass survived {what_changed} changing"
        assert after["last_error"] is None
        # Cleared too, or the badge would name the build found at the old address.
        assert after["detected_version"] is None

    def test_an_edit_that_changes_nothing_tested_keeps_the_outcome(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The discriminating case. A rename, and a save that resends the same address,
        both keep the pass. Without this, the clearing above would be indistinguishable
        from clearing on every update, which would leave no service card able to show a
        result at all.
        """
        made = self._saved_and_tested(client, monkeypatch)

        renamed = client.put(f"/api/settings/instances/{made['id']}", json={"name": "4K"})
        assert renamed.status_code == 200
        assert renamed.json()["last_ok_at"] is not None

        resent = client.put(
            f"/api/settings/instances/{made['id']}",
            json={"base_url": "http://a.local", "verify_tls": True},  # both unchanged
        )
        assert resent.status_code == 200
        assert resent.json()["last_ok_at"] is not None
        assert resent.json()["detected_version"] == "4.0.1"

    def test_a_stored_failure_is_cleared_by_the_same_edit(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both directions, because the badge renders ``last_error`` ahead of
        ``last_ok_at``. A failure left behind would blame the new address for the old
        one's refusal.
        """
        made = client.post(
            "/api/settings/instances",
            json={"kind": "radarr", "name": "HD", "base_url": "http://a.local", "api_key": "k"},
        ).json()

        async def failing_test(
            kind: InstanceKind,
            base_url: str,
            api_key: str,
            *,
            verify: bool = True,
            api_path_prefix: str | None = None,
        ) -> instances_service.TestResult:
            return instances_service.TestResult(
                ok=False, detail=Reason("legacy", {"text": "Couldn't reach it."})
            )

        monkeypatch.setattr(instances_service, "test_connection", failing_test)
        assert client.post(f"/api/settings/instances/{made['id']}/test").status_code == 200
        assert self._row(client, made["id"])["last_error"] == "Couldn't reach it."

        moved = client.put(
            f"/api/settings/instances/{made['id']}", json={"base_url": "http://b.local"}
        )
        assert moved.status_code == 200
        assert moved.json()["last_error"] is None


class TestConnectionTestsHonorTheTlsChoice:
    """The TLS choice must reach the client that actually dials out. The stored
    ``verify_tls`` for a saved instance, and the checkbox value sent with the request
    for the pre-save test on the add form.
    """

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
            kind: InstanceKind,
            base_url: str,
            api_key: str,
            *,
            verify: bool = True,
            api_path_prefix: str | None = None,
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
        prefixes: list[str | None] = []

        async def fake_test(
            kind: InstanceKind,
            base_url: str,
            api_key: str,
            *,
            verify: bool = True,
            api_path_prefix: str | None = None,
        ) -> instances_service.TestResult:
            seen.append(verify)
            prefixes.append(api_path_prefix)
            return instances_service.TestResult(
                ok=True, detail=Reason("legacy", {"text": "Connected."})
            )

        monkeypatch.setattr(instances_service, "test_connection", fake_test)
        resp = client.post(f"/api/settings/instances/{created['id']}/test")
        assert resp.status_code == 200
        assert seen == [False]
        # The stored API path rides along too, so Test Connection probes the path the scan
        # will use rather than whichever one the client defaults to.
        assert prefixes == ["/api/v3"]

    def test_the_pre_save_test_carries_the_checkbox_value(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[bool] = []

        async def fake_test(
            kind: InstanceKind, base_url: str, api_key: str, *, verify: bool = True
        ) -> instances_service.TestResult:
            seen.append(verify)
            return instances_service.TestResult(
                ok=True, detail=Reason("legacy", {"text": "Connected."})
            )

        monkeypatch.setattr(instances_service, "test_connection", fake_test)

        # A passing test goes on to read the folder list, so this arm has to be stubbed
        # too, or the route really dials ``a.local``. It would pass anyway, since the
        # connect failure is caught into ``map_error_reason``, but on nothing better than
        # that host not resolving here. Its three siblings below already carry this.
        async def folders(
            *_a: object, **_k: object
        ) -> list[instances_service.RootFolderSuggestion]:
            return []

        monkeypatch.setattr(instances_service, "probe_root_folders", folders)
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
        # The original password still arms deletion. Nothing was changed.
        armed = client.put(
            "/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD}
        )
        assert armed.status_code == 200, armed.text

    def test_with_no_admin_password_set_arming_points_at_the_password_step(
        self, client: TestClient
    ) -> None:
        """This answers 400, not 403. A Plex-only install has nothing to type, so "that
        didn't match" would send the operator to guess at a password that does not exist.
        Deletion stays off.

        This is one of three routes refusing this way, and the last of the three to get a
        test for it. The restore confirm and the watch-record reset carry the same pair.
        """
        clear_admin_password(client)

        refused = client.put("/api/settings/safety", json={"enabled": True, "password": ""})
        assert refused.status_code == 400, refused.text
        refused_body = refused.json()
        assert refused_body["code"] == "error.settings.no_password_set_for_arming"
        assert refused_body["detail"] == (
            "Set an admin password first. It's what confirms turning deletion on."
        )
        assert client.get("/api/settings/safety").json()["destructive_enabled"] is False

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


class TestTheAdminPasswordGate:
    """The one gate four routes ask through: ``deps.require_admin_password``.

    Arming deletion, changing the arming password, forgetting the watch record, and
    confirming a restore all call it. The tests those routes already had cover one gate
    each. These cover the properties that only exist because it is one function, which a
    fifth caller would otherwise have to re-derive by hand.
    """

    def test_repeated_wrong_current_passwords_are_locked_out(self, client: TestClient) -> None:
        """Changing the arming credential is a password-guessing surface like arming itself,
        so past the threshold it answers 429 instead of running another Argon2 verify."""
        codes = [
            client.post(
                "/api/settings/admin-password",
                json={"password": "brandnew12345", "current_password": f"wrong-{n}"},
            ).status_code
            for n in range(6)
        ]
        assert codes == [403] * 5 + [429]
        # The shared per-IP key is locked too, so arming from this address is 429 rather than
        # 403. That the OTHER gates still answer is asserted below, from a second address.
        assert (
            client.put(
                "/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD}
            ).status_code
            == 429
        )

    def test_the_right_password_clears_every_key_it_was_checked_against(
        self, client: TestClient
    ) -> None:
        """The success path resets both keys, so a near-miss costs the operator nothing.

        Four wrong attempts sit one under the threshold. If the success cleared neither
        key, or only the per-IP one, the fifth wrong attempt below would be a 429. It is a
        403, so the count restarted on both. This is written because it is the step an
        extraction could drop without any other test noticing. Every existing throttle
        test stops at the lockout and never comes back through a success.
        """
        for n in range(4):
            assert (
                client.put(
                    "/api/settings/safety", json={"enabled": True, "password": f"wrong-{n}"}
                ).status_code
                == 403
            )
        armed = client.put(
            "/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD}
        )
        assert armed.status_code == 200, armed.text

        for n in range(4):
            assert (
                client.put(
                    "/api/settings/safety", json={"enabled": True, "password": f"again-{n}"}
                ).status_code
                == 403
            ), f"attempt {n} after a success was throttled, so the success cleared nothing"

    def test_a_busy_argon2_gate_is_not_a_wrong_password(self, client: TestClient) -> None:
        """A full gate answers 503, and it must never register as a failed attempt, or the
        load-shedding defense becomes the lockout.

        This is asserted black-box, because ``Throttle._buckets`` is private and reading
        it would pin the implementation rather than the behavior. Four real failures sit
        one under the threshold of five. If a 503 counted, it would be the fifth, and the
        next wrong password would come back 429. It comes back 403, so none of the three
        counted.
        """
        for n in range(4):
            assert (
                client.post(
                    "/api/settings/admin-password",
                    json={"password": "brandnew12345", "current_password": f"wrong-{n}"},
                ).status_code
                == 403
            )

        taken = argon2_gate.acquire(argon2_gate.limit)
        assert taken == argon2_gate.limit
        try:
            for _ in range(3):
                busy = client.post(
                    "/api/settings/admin-password",
                    json={"password": "brandnew12345", "current_password": "wrong-again"},
                )
                assert busy.status_code == 503, busy.text
                assert busy.headers["Retry-After"] == "2"
        finally:
            argon2_gate.release(taken)

        still_counting = client.post(
            "/api/settings/admin-password",
            json={"password": "brandnew12345", "current_password": "wrong-5th"},
        )
        assert still_counting.status_code == 403, still_counting.text

    def test_a_lockout_at_one_gate_leaves_the_other_gates_open(
        self, client: TestClient, settings: Settings
    ) -> None:
        """Each gate carries its own ``account:`` key, and that is what bounds a lockout.

        An ``account:`` key is not scoped to an address, so it refuses from everywhere at once.
        Were the four collapsed onto one, five wrong watch-record passwords from any host on
        the internet would lock the operator out of arming deletion from their own machine.
        This is the assertion the per-route tests cannot reach: they share both keys.

        Driven from a SECOND address, because all four gates deliberately share the per-IP key
        (one guesser at one secret is one guesser, wherever they knock) and that shared key
        would otherwise mask the account keys entirely.
        """
        for n in range(6):
            client.post("/api/settings/watch-evidence/reset", json={"password": f"wrong-{n}"})
        assert (
            client.post(
                "/api/settings/watch-evidence/reset", json={"password": "wrong-again"}
            ).status_code
            == 429
        )

        elsewhere = TestClient(client.app, client=("203.0.113.7", 44321))
        elsewhere.headers["X-Reaper-CSRF"] = "1"
        elsewhere.cookies.update(client.cookies)

        # Locked out of forgetting the watch record from here too: that is the account key
        # doing its job across addresses.
        assert (
            elsewhere.post(
                "/api/settings/watch-evidence/reset", json={"password": "wrong"}
            ).status_code
            == 429
        )
        # The other three still answer on their merits, which is the point.
        assert (
            elsewhere.put(
                "/api/settings/safety", json={"enabled": True, "password": "wrong"}
            ).status_code
            == 403
        )
        assert (
            elsewhere.post(
                "/api/settings/admin-password",
                json={"password": "brandnew12345", "current_password": "wrong"},
            ).status_code
            == 403
        )
        assert (
            elsewhere.post(
                "/api/settings/backup/restore/confirm", json={"password": "wrong"}
            ).status_code
            == 403
        )
        # And the right password still arms, so the lockout next door cost nothing real.
        assert (
            elsewhere.put(
                "/api/settings/safety", json={"enabled": True, "password": TEST_PASSWORD}
            ).status_code
            == 200
        )


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
        """A TV-only deployment is a real deployment. Sonarr counts as the library
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

    def test_has_password_reports_whether_a_local_account_exists(self, client: TestClient) -> None:
        """The wizard derives which step it is on from this, so it must track the real
        state.

        The seeded admin has a password. Nulling the hash simulates the Plex-only install,
        where the owner claimed the server over OAuth and no local account was ever
        created.
        """
        assert client.get("/api/setup/status").json()["has_password"] is True
        clear_admin_password(client)
        assert client.get("/api/setup/status").json()["has_password"] is False

    def test_setup_is_not_complete_without_a_password(self, client: TestClient) -> None:
        """Being scan-ready and scanned is not enough on its own.

        This is isolated deliberately. Everything else `complete` asks for is satisfied
        here, so the only thing holding it False is the missing password. Without this,
        the wizard would wave through an install with no local account, no way to arm
        deletion, and no way to confirm a restore.
        """
        for kind, name in [("radarr", "HD"), ("tautulli", "T")]:
            client.post(
                "/api/settings/instances",
                json={"kind": kind, "name": name, "base_url": f"http://{name}", "api_key": "k"},
            )
        _add_snapshot(client)
        ready = client.get("/api/setup/status").json()
        assert ready["scan_ready"] is True
        assert ready["has_scanned"] is True
        assert ready["complete"] is True

        clear_admin_password(client)
        after = client.get("/api/setup/status").json()
        assert after["scan_ready"] is True, "only the password changed"
        assert after["has_scanned"] is True, "only the password changed"
        assert after["complete"] is False

    def test_scan_ready_without_plex_is_not_reap_ready(self, client: TestClient) -> None:
        """Everything a scan needs is present, and a reap is still refused.

        This install finishes the wizard, since ``complete`` is deliberately blind to
        Plex, because Plex is optional for a scan. So the only thing that can tell the
        operator their first real run will be turned away is ``reap_ready``.
        """
        _make_scan_ready(client)
        _add_snapshot(client)
        status = client.get("/api/setup/status").json()
        assert status["scan_ready"] is True
        assert status["complete"] is True
        assert status["plex_linked"] is False
        assert status["reap_ready"] is False

    def test_linking_plex_is_what_makes_it_reap_ready(self, client: TestClient) -> None:
        _make_scan_ready(client)
        _link_plex(client)
        status = client.get("/api/setup/status").json()
        assert status["plex_linked"] is True
        assert status["reap_ready"] is True

    def test_reap_ready_needs_the_password_that_arms_deletion(self, client: TestClient) -> None:
        """Isolated the way ``complete``'s password case is: everything else is satisfied.

        Deletion is armed through ``PUT /api/settings/safety``, which refuses without an
        admin password, so an install missing one cannot reach a real run however much of
        the rest is configured.
        """
        _make_scan_ready(client)
        _link_plex(client)
        assert client.get("/api/setup/status").json()["reap_ready"] is True

        clear_admin_password(client)
        after = client.get("/api/setup/status").json()
        assert after["plex_linked"] is True, "only the password changed"
        assert after["scan_ready"] is True, "only the password changed"
        assert after["reap_ready"] is False

    def test_reap_ready_asks_the_same_two_questions_the_executor_refuses_on(
        self, client: TestClient
    ) -> None:
        """``reap_ready`` is only worth publishing if it predicts the actual refusal.

        The sentence "you cannot reap without this" is written in three places:
        ``api.runs._preflight_refusal``, ``services.executor.execute``'s backstop, and this
        field, which the wizard and the Reap page both read. So the field is pinned
        against the refusal itself rather than against a transcription of it. A gateway
        missing either client refuses, and a setup missing the same thing is not
        reap-ready.
        """
        from reaper.api.runs import _preflight_refusal
        from reaper.services.executor import ReapGateway

        whole = ReapGateway(plex=object(), tautulli=object())  # type: ignore[arg-type]
        assert _preflight_refusal(whole) is None

        no_plex = ReapGateway(plex=None, tautulli=object())  # type: ignore[arg-type]
        assert (code := _preflight_refusal(no_plex)) is not None
        assert "without Plex" in reaper.refusal.MESSAGES[code]

        no_tautulli = ReapGateway(plex=object(), tautulli=None)  # type: ignore[arg-type]
        assert (code := _preflight_refusal(no_tautulli)) is not None
        assert "without Tautulli" in reaper.refusal.MESSAGES[code]

        # ...and the same two absences, asked of the configuration instead of the clients.
        _make_scan_ready(client)
        _link_plex(client)
        assert client.get("/api/setup/status").json()["reap_ready"] is True

        instances = client.get("/api/settings/instances").json()
        tautulli = next(i for i in instances if i["kind"] == "tautulli")
        assert client.delete(f"/api/settings/instances/{tautulli['id']}").status_code == 200
        assert client.get("/api/setup/status").json()["reap_ready"] is False


class TestSchedule:
    def test_every_schedulable_job_is_listed(self, client: TestClient) -> None:
        schedule = client.get("/api/settings/schedule").json()
        by_id = {j["id"]: j for j in schedule["jobs"]}
        # The scan and every upkeep job are always listed, off or not.
        assert {
            "scheduled_scan",
            "refresh_ratings",
            "refresh_curated_lists",
            "full_history_sweep",
            "check_for_updates",
        } <= (by_id.keys())
        # The update check reaches the operator's Jobs page through the same route as the
        # rest, on its own schedule and armed out of the box. The whole point is that an
        # install nobody opens still checks, which a listed-but-unscheduled row would not
        # do. This is the wiring end to end: a real app boot, a real scheduler holding the
        # app's own UpdateChecker, and the payload the page renders.
        assert by_id["check_for_updates"]["cron"] == "15 4 * * *"
        assert by_id["check_for_updates"]["next_run_at"] is not None
        assert by_id["scheduled_scan"]["cron"] is None  # no automatic scan by default
        # An upkeep job carries its built-in default and runs on it out of the box.
        assert by_id["refresh_ratings"]["default_cron"] == "30 3 * * *"
        assert by_id["refresh_ratings"]["cron"] == "30 3 * * *"
        assert by_id["refresh_ratings"]["running"] is False
        # A job that has never completed reads as "hasn't run yet".
        assert by_id["refresh_ratings"]["last_run_at"] is None
        assert by_id["refresh_ratings"]["last_ok"] is None

    def test_a_recorded_last_run_surfaces_on_the_job(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once a job has completed, its stored last run (when, ok, reason) shows on the row;
        a job with no record stays null so the page can say "hasn't run yet"."""
        from reaper.engine.reason import Reason
        from reaper.services import app_settings

        async def fake_last_runs(session: object) -> dict[str, dict[str, object]]:
            return {
                "refresh_ratings": {
                    "at": "2026-07-24T03:30:00+00:00",
                    "ok": True,
                    "result": Reason("ratings_refreshed"),
                }
            }

        monkeypatch.setattr(app_settings, "get_job_last_runs", fake_last_runs)
        by_id = {j["id"]: j for j in client.get("/api/settings/schedule").json()["jobs"]}
        assert by_id["refresh_ratings"]["last_run_at"] == "2026-07-24T03:30:00+00:00"
        assert by_id["refresh_ratings"]["last_ok"] is True
        assert by_id["refresh_ratings"]["last_result_reason"] == {
            "k": "ratings_refreshed",
            "p": None,
        }
        assert by_id["full_history_sweep"]["last_run_at"] is None
        assert by_id["full_history_sweep"]["last_ok"] is None

    async def test_a_job_row_stored_before_the_typed_conversion_still_reads(
        self,
        client: TestClient,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A row written by an older build carries a bare English phrase under
        ``"result"`` instead of a wire-encoded reason. An old row must still read back as
        ``Reason("legacy", {"text": ...})``, exactly as ``engine.reason.from_wire`` already
        does for a bare stored string. It just stops composing through the catalog. This
        writes straight into storage rather than through ``set_job_last_run``, which only
        ever writes the new shape now, since the point is to simulate a value an older
        build left behind.

        ``async_factory`` shares the same on-disk database as ``client``, since both are
        built off the same ``settings`` fixture, so a commit through it is visible to the
        route.
        """
        from reaper.services import app_settings

        async with async_factory() as session:
            await app_settings._set(
                session,
                f"{app_settings.JOB_LAST_RUN_PREFIX}refresh_ratings",
                {"at": "2026-06-01T03:30:00+00:00", "ok": True, "result": "Ratings refreshed"},
            )
            await session.commit()

        by_id = {j["id"]: j for j in client.get("/api/settings/schedule").json()["jobs"]}
        assert by_id["refresh_ratings"]["last_result_reason"] == {
            "k": "legacy",
            "p": {"text": "Ratings refreshed"},
        }

    def test_the_scan_cron_is_stored_and_a_bad_one_refused(self, client: TestClient) -> None:
        ok = client.put("/api/settings/jobs/scheduled_scan/schedule", json={"cron": "30 4 * * *"})
        assert ok.status_code == 200, ok.text
        by_id = {j["id"]: j for j in ok.json()["jobs"]}
        assert by_id["scheduled_scan"]["cron"] == "30 4 * * *"
        assert by_id["scheduled_scan"]["next_run_at"] is not None  # now scheduled

        bad = client.put("/api/settings/jobs/scheduled_scan/schedule", json={"cron": "not a cron"})
        assert bad.status_code == 422

        # Clearing it removes the job again.
        cleared = client.put(
            "/api/settings/jobs/scheduled_scan/schedule", json={"cron": None}
        ).json()
        by_id = {j["id"]: j for j in cleared["jobs"]}
        assert by_id["scheduled_scan"]["cron"] is None
        assert by_id["scheduled_scan"]["next_run_at"] is None

    def test_an_upkeep_job_can_be_rescheduled_or_turned_off(self, client: TestClient) -> None:
        off = client.put("/api/settings/jobs/refresh_ratings/schedule", json={"cron": None})
        assert off.status_code == 200, off.text
        by_id = {j["id"]: j for j in off.json()["jobs"]}
        assert by_id["refresh_ratings"]["cron"] is None  # off
        assert by_id["refresh_ratings"]["next_run_at"] is None  # no longer scheduled

        on = client.put("/api/settings/jobs/refresh_ratings/schedule", json={"cron": "0 6 * * *"})
        by_id = {j["id"]: j for j in on.json()["jobs"]}
        assert by_id["refresh_ratings"]["cron"] == "0 6 * * *"
        assert by_id["refresh_ratings"]["next_run_at"] is not None

        bad = client.put("/api/settings/jobs/refresh_ratings/schedule", json={"cron": "nope"})
        assert bad.status_code == 422

    def test_an_unknown_job_schedule_is_a_404(self, client: TestClient) -> None:
        resp = client.put("/api/settings/jobs/not_a_job/schedule", json={"cron": "0 6 * * *"})
        assert resp.status_code == 404

    def test_both_job_families_refuse_a_bad_cron_in_the_one_declared_sentence(
        self, client: TestClient
    ) -> None:
        """The scan arm and the upkeep arm answer a cron the scheduler will not take with
        the same words, and those words are ``MESSAGES["error.settings.bad_cron"]`` with
        its slot filled. Both arms raise the same code
        (``refuse(422, "error.settings.bad_cron", ...)``) rather than spelling English
        out, so a reword of the one catalog template moves both.

        **Three mutations, one assertion each, and none of the three catches another.** Do
        not collapse them:

        - *An arm is reworded.* Caught by ``startswith``/``endswith``. This is the only one the
          first version of this test caught.
        - *An arm re-inlines the sentence verbatim.* Caught by the source count alone. Both
          copies render identically, which is what the dedup means, so no assertion over a
          response can ever see it.
        - *An arm raises the template unformatted.* Caught by the ``{reason}`` assertion
          alone. The raw template starts and ends with the very halves being compared, and
          its literal ``{reason}`` is long enough to satisfy the length bound, so every
          other check here passes while the placeholder ships to the operator.

        Every expectation is derived from the declaration, so none of this restates the
        sentence.
        """
        prefix, suffix = reaper.refusal.MESSAGES["error.settings.bad_cron"].split("{reason}")

        # `prefix` is the declaration's own first half, so a re-inlined f-string anywhere
        # in the tree is a second hit however the rest of it was reworded.
        package = Path(reaper.refusal.__file__).parent
        copies = sum(f.read_text(encoding="utf-8").count(prefix) for f in package.rglob("*.py"))
        assert copies == 1, (
            f"{copies} copies of the cron refusal in src/reaper, expected only the catalog's "
            "declaration. An arm spelling its own sentence renders identically to this one."
        )

        for job_id in ("scheduled_scan", "refresh_ratings"):
            resp = client.put(f"/api/settings/jobs/{job_id}/schedule", json={"cron": "nope"})
            assert resp.status_code == 422, job_id
            body = resp.json()
            assert body["code"] == "error.settings.bad_cron", job_id
            detail = body["detail"]
            assert detail.startswith(prefix), (job_id, detail)
            assert detail.endswith(suffix), (job_id, detail)
            assert "{reason}" not in detail, (job_id, detail)
            assert len(detail) > len(prefix) + len(suffix), (job_id, detail)

    def test_saving_one_upkeep_job_leaves_the_others_untouched(self, client: TestClient) -> None:
        """Each job's schedule is its own stored row, so saving one never drops another. A
        shared-dict read-modify-write could otherwise let a concurrent save overwrite
        another one entirely.
        """
        client.put("/api/settings/jobs/refresh_ratings/schedule", json={"cron": "0 6 * * *"})
        client.put("/api/settings/jobs/refresh_curated_lists/schedule", json={"cron": None})
        resp = client.put(
            "/api/settings/jobs/full_history_sweep/schedule", json={"cron": "0 7 * * *"}
        )
        by_id = {j["id"]: j for j in resp.json()["jobs"]}
        # All three overrides survive, each in its own row.
        assert by_id["refresh_ratings"]["cron"] == "0 6 * * *"
        assert by_id["refresh_curated_lists"]["cron"] is None
        assert by_id["full_history_sweep"]["cron"] == "0 7 * * *"


class TestRunJob:
    def test_a_known_maintenance_job_can_be_run_now(self, client: TestClient) -> None:
        # Pause the scheduler first so "run now" moves the job's next-run without actually
        # firing the real, network-touching job inside the test. The endpoint's job is only
        # to nudge the schedule. The work itself is APScheduler's, tested nowhere near here.
        client.app.state.scheduler.pause()  # type: ignore[attr-defined]
        resp = client.post("/api/settings/jobs/refresh_curated_lists/run", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "started", "job": "refresh_curated_lists"}

    def test_an_unknown_job_is_a_404(self, client: TestClient) -> None:
        assert client.post("/api/settings/jobs/not_a_job/run", json={}).status_code == 404

    def test_the_scan_is_not_runnable_here(self, client: TestClient) -> None:
        # The library scan runs through the streaming /api/scan endpoint, so the UI can
        # show progress, never this fire-and-forget one. It is deliberately not on the list.
        assert client.post("/api/settings/jobs/scheduled_scan/run", json={}).status_code == 404

    def test_a_turned_off_job_can_still_be_run_now(self, client: TestClient) -> None:
        # Turning a job off removes it from the scheduler, but "run now" must still work.
        # It runs once without turning the schedule back on. Pause first so the real,
        # network-touching work never fires inside the test.
        client.put("/api/settings/jobs/refresh_curated_lists/schedule", json={"cron": None})
        client.app.state.scheduler.pause()  # type: ignore[attr-defined]
        resp = client.post("/api/settings/jobs/refresh_curated_lists/run", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "started", "job": "refresh_curated_lists"}


class TestPoster:
    def test_no_tautulli_means_a_404_not_a_crash(self, client: TestClient) -> None:
        """With nothing to fetch artwork from, the poster route 404s and the card falls
        back to a placeholder. It never 500s.
        """
        assert client.get("/api/poster/123").status_code == 404


class TestPlexStatus:
    def test_an_unlinked_server_reports_so(self, client: TestClient) -> None:
        assert client.get("/api/settings/plex").json() == {
            "linked": False,
            "name": None,
            "connection_uri": None,
            "last_ok_at": None,
            # On is the only default; the operator can opt out per server once linked.
            "verify_tls": True,
            # Present whether or not a server is linked: links need somewhere to point.
            "web_url": "https://app.plex.tv",
        }

    def test_unlinking_when_nothing_is_linked_is_a_noop(self, client: TestClient) -> None:
        assert client.delete("/api/settings/plex").json() == {"removed": False}

    def test_unlinking_a_linked_server_reports_the_removal(self, client: TestClient) -> None:
        """The other arm of the same route. Only the no-op branch was covered, so the answer
        an operator actually gets back after unlinking was never asserted."""
        _link_plex(client)

        assert client.delete("/api/settings/plex").json() == {"removed": True}

    def test_the_certificate_check_cannot_be_set_before_linking(self, client: TestClient) -> None:
        response = client.put("/api/settings/plex", json={"web_url": "", "verify_tls": False})
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "error.plex.verify_tls_no_server"
        assert "Link one" in body["detail"]

    @pytest.mark.parametrize("address", ["plex.example", "http://", "https://", "ftp://a.local"])
    def test_the_web_address_must_be_a_full_web_address(
        self, client: TestClient, address: str
    ) -> None:
        """A bare scheme with no host behind it must never pass, the same way it is
        refused for every sibling URL setting. All four of these are refused here for the
        same reason they are refused on a service address.
        """
        refused = client.put("/api/settings/plex", json={"web_url": address})
        assert refused.status_code == 422, refused.text
        # Nothing was stored, so links still point at the hosted default.
        assert client.get("/api/settings/plex").json()["web_url"] == "https://app.plex.tv"


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

    @pytest.fixture(autouse=True)
    def _plex_server_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A finished link fires a library autosync, and that one goes over plexapi.

        `httpx2_mock` covers httpx, but plexapi speaks `requests`, which `respx` does not
        intercept. Without this fixture, every test in this class would let the autosync
        try a real DNS lookup on the connection host. `_sync_libraries_after_link` catches
        every exception, since its docstring promises a sign-in never strands on a failed
        library refresh, so those lookups would fail silently rather than break the test.
        This fixture stubs `PlexClient._connect` to raise `PlexError` directly, the same
        outcome an unreachable server produces, without the real network round trip.
        """

        async def _unreachable(_self: PlexClient) -> NoReturn:
            raise PlexError("no Plex server in these tests")

        monkeypatch.setattr(PlexClient, "_connect", _unreachable)

    def _mock_plextv(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.post("https://plex.tv/api/v2/pins").mock(
            return_value=httpx.Response(201, json={"id": 42, "code": "ABCD"})
        )
        httpx2_mock.get("https://plex.tv/api/v2/pins/42").mock(
            return_value=httpx.Response(200, json={"id": 42, "authToken": "tok"})
        )
        httpx2_mock.get("https://plex.tv/api/v2/user").mock(
            return_value=httpx.Response(200, json={"id": 7, "username": "owner"})
        )
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(
                200,
                json=[_plex_resource("machine-a", "Den"), _plex_resource("machine-b", "Attic")],
            )
        )
        httpx2_mock.get("https://x.plex.direct:32400/identity").mock(
            return_value=httpx.Response(200, json={})
        )

    def test_a_multi_server_account_gets_the_choices_and_the_pin_survives(
        self, client: TestClient, httpx2_mock: respx.Router
    ) -> None:
        self._mock_plextv(httpx2_mock)
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

    def test_the_certificate_choice_reaches_the_probe_and_sticks_to_the_row(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, httpx2_mock: respx.Router
    ) -> None:
        """Linking with the certificate check off must (a) probe the server without
        verification, since a self-signed HTTPS Plex is unreachable otherwise, and (b)
        store the choice on the server row, where every later client reads it. Flipping
        it back on afterward is a plain settings edit.
        """
        from reaper.services import plex_link

        captured: dict[str, object] = {}
        # The spy is installed through `plex_link` because the module under test reads
        # this name, and rebinding any other copy patches nothing.
        real_probe = plex_link.probe_connection  # type: ignore[attr-defined]

        async def spying_probe(
            connection: object, token: str, *, timeout: float = 5.0, verify: bool = True
        ) -> bool:
            captured["verify"] = verify
            return await real_probe(connection, token, timeout=timeout, verify=verify)  # type: ignore[arg-type]

        monkeypatch.setattr(plex_link, "probe_connection", spying_probe)

        self._mock_plextv(httpx2_mock)
        start = client.post("/api/settings/plex/link/start").json()
        done = client.post(
            "/api/settings/plex/link/poll",
            json={
                "pin_id": start["pin_id"],
                "machine_identifier": "machine-b",
                "verify_tls": False,
            },
        )
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "ok"
        assert done.json()["server"]["verify_tls"] is False

        assert captured["verify"] is False
        assert client.get("/api/settings/plex").json()["verify_tls"] is False

        # Only the field being changed, which is what the switch sends.
        flipped = client.put("/api/settings/plex", json={"verify_tls": True})
        assert flipped.status_code == 200
        assert flipped.json()["verify_tls"] is True

    def test_saving_one_plex_setting_leaves_the_other_alone(
        self, client: TestClient, httpx2_mock: respx.Router
    ) -> None:
        """The route is a patch, and `web_url` needs three states to be one: omitted,
        explicitly blank, and a real value.

        `str = ""` could not tell "I am not changing the address" from "reset it to the
        hosted default", so every caller would write the address whether it meant to or
        not. A caller that only means to flip the certificate switch, filling the field
        from a cached status row, would then silently revert an address that had moved
        since, and every "open in Plex" link in the app would point at plex.tv. Omitted
        must never mean the same thing as empty.
        """
        self._mock_plextv(httpx2_mock)
        start = client.post("/api/settings/plex/link/start").json()
        client.post(
            "/api/settings/plex/link/poll",
            json={"pin_id": start["pin_id"], "machine_identifier": "machine-b"},
        )

        stored = "https://plex.example.net:32400"
        assert client.put("/api/settings/plex", json={"web_url": stored}).status_code == 200

        # The certificate switch's request. It says nothing about the address.
        kept = client.put("/api/settings/plex", json={"verify_tls": False})
        assert kept.status_code == 200
        assert kept.json()["web_url"] == stored
        assert kept.json()["verify_tls"] is False

        # And the address save says nothing about the certificate check.
        again = client.put("/api/settings/plex", json={"web_url": stored})
        assert again.json()["verify_tls"] is False

        # An explicit empty string is still the operator asking for the hosted default back,
        # which is the request `None` had been standing in for.
        reset = client.put("/api/settings/plex", json={"web_url": ""})
        assert reset.status_code == 200
        assert reset.json()["web_url"] == "https://app.plex.tv"

    def test_the_patch_contract_is_published_where_someone_can_read_it(self) -> None:
        """The empty string is the one way back to the hosted default, so it must be
        published somewhere a caller can read it.

        ``PlexUpdateIn``'s per-field docstrings do not reach the schema. Pydantic harvests
        those only under ``use_attribute_docstrings``, which this tree does not set (see
        ``api/schemas.py``). So a contract written beside a field only documents this
        file, not the published schema. One fact needs two copies, the class docstring
        and the route description, so both are checked here, and each failure names the
        other.
        """
        described = PlexUpdateIn.model_json_schema().get("description", "")
        # Words, not a phrase, so a rewording that keeps the fact still passes. Both are absent
        # the moment the reset moves back down to an attribute docstring.
        assert "empty" in described.lower() and "hosted default" in described.lower(), (
            "PlexUpdateIn's CLASS docstring is the published schema description, and an "
            "attribute docstring is published nowhere. Say there how the address resets."
        )
        assert "keeps its stored value" in described

        route_doc = (update_plex_settings.__doc__ or "").lower()
        assert "empty" in route_doc and "hosted default" in route_doc, (
            "The PUT /plex route description is the sibling copy of PlexUpdateIn's class "
            "docstring. Both name the empty-string reset; correcting one means both."
        )

    def test_a_server_that_is_briefly_unreachable_keeps_the_sign_in_alive(
        self, client: TestClient, httpx2_mock: respx.Router
    ) -> None:
        """The operator approves the sign-in at the instant their server is restarting.

        ``poll_link`` deliberately keeps the pending sign-in for this case. It answers with
        a non-final "retrying" status rather than an error, since the browser stops
        polling for good on any thrown status, which would strand the pending sign-in and
        force the whole approval round trip to be redone. The same sign-in finishes once
        the server answers.
        """
        self._mock_plextv(httpx2_mock)
        # The server is down, then it comes back. This is driven by a flag rather than a
        # fixed list of responses, since one probe may legitimately make several attempts
        # (the shared client retries a transient transport error), and this scenario is
        # "the server is not answering yet", not "it refuses exactly once".
        server_down = {"value": True}

        def identity(request: object) -> httpx.Response:
            if server_down["value"]:
                raise httpx2.ConnectError("connection refused")
            return httpx.Response(200, json={})

        httpx2_mock.get("https://x.plex.direct:32400/identity").mock(side_effect=identity)
        start = client.post("/api/settings/plex/link/start").json()

        blip = client.post(
            "/api/settings/plex/link/poll",
            json={"pin_id": start["pin_id"], "machine_identifier": "machine-b"},
        )
        assert blip.status_code == 200, blip.text
        assert blip.json()["status"] == "retrying"
        # And it carries why, as a typed reason, so a longer wait reads as a wait rather
        # than a hang once the frontend composes it.
        reason = blip.json()["reason"]
        assert reason["k"] == "error.plex.link_unreachable"
        assert reason["p"] == {"name": "Attic", "count": 1}
        assert client.get("/api/settings/plex").json()["linked"] is False

        # The SAME sign-in finishes once the server answers. Nothing was burned.
        server_down["value"] = False
        done = client.post(
            "/api/settings/plex/link/poll",
            json={"pin_id": start["pin_id"], "machine_identifier": "machine-b"},
        )
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "ok"
        assert client.get("/api/settings/plex").json()["linked"] is True

    def test_a_pick_matching_nothing_owned_fails_closed(
        self, client: TestClient, httpx2_mock: respx.Router
    ) -> None:
        self._mock_plextv(httpx2_mock)
        start = client.post("/api/settings/plex/link/start").json()

        bad = client.post(
            "/api/settings/plex/link/poll",
            json={"pin_id": start["pin_id"], "machine_identifier": "somebody-elses-machine"},
        )
        assert bad.status_code == 400
        bad_body = bad.json()
        assert bad_body["code"] == "error.plex.link_choice_not_found"
        assert "No server this account owns" in bad_body["detail"]

        # The refusal consumed the PIN, so the obtained token cannot be replayed.
        retry = client.post(
            "/api/settings/plex/link/poll",
            json={"pin_id": start["pin_id"], "machine_identifier": "machine-b"},
        )
        assert retry.status_code == 400

        assert client.get("/api/settings/plex").json()["linked"] is False

    def _link_machine_b(self, client: TestClient, httpx2_mock: respx.Router) -> None:
        """Link the account's second server, so a connection can be saved against it."""
        self._mock_plextv(httpx2_mock)
        start = client.post("/api/settings/plex/link/start").json()
        done = client.post(
            "/api/settings/plex/link/poll",
            json={"pin_id": start["pin_id"], "machine_identifier": "machine-b"},
        )
        assert done.status_code == 200, done.text

    def test_a_typed_address_belonging_to_another_server_is_refused(
        self, client: TestClient, httpx2_mock: respx.Router
    ) -> None:
        """The manual address box takes any host on the network, so the probe must
        confirm which server answered, not merely that something did. Saving a
        neighbor's Plex would point Reaper's Leaving Soon writes and its Never-Reap read
        at a library nobody asked it to touch, with the UI still naming the linked server.
        """
        self._link_machine_b(client, httpx2_mock)

        # Something answers at the typed address. It is simply not the linked server.
        httpx2_mock.get("https://192.0.2.50:32400/identity").mock(
            return_value=httpx.Response(
                200, json={"MediaContainer": {"machineIdentifier": "machine-a"}}
            )
        )
        refused = client.put(
            "/api/settings/plex/connection", json={"uri": "https://192.0.2.50:32400"}
        )
        assert refused.status_code == 409, refused.text
        refused_body = refused.json()
        assert refused_body["code"] == "error.plex.connection_wrong_server"
        assert "a different Plex server" in refused_body["detail"]
        # Nothing was written. The stored address is still the one linking found.
        assert (
            client.get("/api/settings/plex").json()["connection_uri"]
            == "https://x.plex.direct:32400"
        )

    def test_an_address_that_will_not_say_who_it_is_is_refused_too(
        self, client: TestClient, httpx2_mock: respx.Router
    ) -> None:
        """Unconfirmed is not confirmed. A 200 with no machineIdentifier is exactly the
        shape a reverse proxy in front of the wrong thing produces, so it fails closed."""
        self._link_machine_b(client, httpx2_mock)

        httpx2_mock.get("https://192.0.2.51:32400/identity").mock(
            return_value=httpx.Response(200, json={"MediaContainer": {}})
        )
        refused = client.put(
            "/api/settings/plex/connection", json={"uri": "https://192.0.2.51:32400"}
        )
        assert refused.status_code == 502, refused.text
        assert (
            client.get("/api/settings/plex").json()["connection_uri"]
            == "https://x.plex.direct:32400"
        )

    def test_the_linked_server_s_own_address_still_saves(
        self, client: TestClient, httpx2_mock: respx.Router
    ) -> None:
        """The check must not cost the operator the thing the box is for."""
        self._link_machine_b(client, httpx2_mock)

        httpx2_mock.get("https://192.0.2.52:32400/identity").mock(
            return_value=httpx.Response(
                200, json={"MediaContainer": {"machineIdentifier": "machine-b"}}
            )
        )
        saved = client.put(
            "/api/settings/plex/connection", json={"uri": "https://192.0.2.52:32400"}
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["connection_uri"] == "https://192.0.2.52:32400"


class TestPlexLibrarySync:
    """Drives the library refresh on a server that actually answers, since nothing else
    exercises the merge logic directly.

    ``video_sections`` builds a plexapi server, and plexapi speaks ``requests``, which
    ``respx`` does not intercept. ``_sync_libraries_after_link`` catches every exception on
    purpose, so a class that only exercises it through a failure would never run the merge
    itself. This drives the merge directly: an operator's off stays off, a new library
    arrives on, and one that left the server drops out.
    """

    @pytest.fixture
    def linked(self, client: TestClient) -> None:
        """A linked server whose token really opens, since ``_sync_libraries`` decrypts it
        before it builds the client. ``_link_plex`` stores the literal ``"enc"``, which is
        enough for the row-exists checks it was written for and not for this."""
        settings: Settings = client.app.state.settings  # type: ignore[attr-defined]
        box = client.app.state.secret_box  # type: ignore[attr-defined]
        engine = sa_create_engine(settings.sync_database_url)
        with Session(engine) as session:
            session.add(
                PlexServer(
                    machine_identifier="machine-sync",
                    name="Example Server",
                    connection_uri="http://plex.example:32400",
                    token_enc=box.encrypt("tok"),
                    created_at=utcnow(),
                )
            )
            session.commit()
        engine.dispose()

    @staticmethod
    def _sections(
        monkeypatch: pytest.MonkeyPatch, sections: list[tuple[int, str, str]]
    ) -> list[str]:
        """Answer ``video_sections`` instead of raising, and record that it was asked.

        This is stubbed at ``video_sections`` rather than at ``_connect``, since the layer
        under it is plexapi's synchronous ``library.sections()``, and standing that up
        would be testing plexapi. The list it returns is the client's own ``PlexSection``,
        which is the type ``_sync_libraries`` reads.
        """
        asked: list[str] = []

        async def _fake(_self: PlexClient) -> list[PlexSection]:
            asked.append("video_sections")
            return [PlexSection(key=k, title=t, kind=kind) for k, t, kind in sections]

        monkeypatch.setattr(PlexClient, "video_sections", _fake)
        return asked

    def test_the_sync_route_merges_rather_than_replaces(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, linked: None
    ) -> None:
        """The three merge outcomes at once, because they are one dict lookup with a default
        and a test naming only the new-library case cannot tell that default from a rewrite.

        The operator's choice is the one that fails toward writing to a library they turned
        off, so it is the arm the assertion leads with.
        """
        asked = self._sections(
            monkeypatch, [(2, "Movies", "movie"), (5, "TV", "show"), (9, "Kids", "movie")]
        )
        first = client.post("/api/settings/plex/libraries/sync")
        assert first.status_code == 200, first.text
        assert asked == ["video_sections"]
        # A fresh library starts ON, so a default install marks every library without setup.
        assert [(lib["key"], lib["enabled"]) for lib in first.json()] == [
            (2, True),
            (5, True),
            (9, True),
        ]

        client.put("/api/settings/plex/libraries", json={"enabled_keys": [2, 9]})

        # Second pass: 5 is still off, 9 has left the server, 12 is new.
        self._sections(
            monkeypatch, [(2, "Movies", "movie"), (5, "TV", "show"), (12, "Docs", "show")]
        )
        again = client.post("/api/settings/plex/libraries/sync")
        assert again.status_code == 200, again.text
        assert [(lib["key"], lib["enabled"]) for lib in again.json()] == [
            (2, True),  # untouched
            (5, False),  # the operator turned it off, a refresh must not turn it back on
            (12, True),  # new since the last sync
        ]
        # Dropped, not left behind enabled. A library that is gone from the server would
        # otherwise keep a stored `enabled` row nothing can ever reach to turn off.
        assert 9 not in {lib["key"] for lib in again.json()}

    def test_the_sync_route_answers_502_for_an_unreachable_server(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, linked: None
    ) -> None:
        """The route maps ``PlexError`` rather than letting it reach the operator as a
        500."""

        async def _boom(_self: PlexClient) -> NoReturn:
            raise PlexError("no route to host")

        monkeypatch.setattr(PlexClient, "video_sections", _boom)

        answer = client.post("/api/settings/plex/libraries/sync")
        assert answer.status_code == 502, answer.text
        answer_body = answer.json()
        assert answer_body["code"] == "error.plex.libraries_sync_failed"
        assert "Could not reach Plex" in answer_body["detail"]

    def test_the_sync_route_refuses_before_a_server_is_linked(self, client: TestClient) -> None:
        assert client.post("/api/settings/plex/libraries/sync").status_code == 400

    async def test_the_autosync_after_a_link_stores_the_libraries(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, linked: None
    ) -> None:
        """The other route into the sync logic, alongside ``video_sections``.

        ``_sync_libraries_after_link`` returns nothing and reports nothing, so the only
        evidence it did its job is the stored list afterward. Driving it through the route
        would mean standing up the whole plex.tv link flow again, which
        ``TestPlexLinkChoice`` already pins. This calls the function the way both link
        endpoints do.
        """
        from reaper.api.plex import _sync_libraries_after_link

        self._sections(monkeypatch, [(2, "Movies", "movie")])
        request = Request({"type": "http", "app": client.app, "headers": []})

        await _sync_libraries_after_link(request)

        stored = client.get("/api/settings/plex/libraries").json()
        assert [(lib["key"], lib["title"], lib["enabled"]) for lib in stored] == [
            (2, "Movies", True)
        ]

    async def test_a_failed_autosync_never_strands_the_sign_in(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, linked: None
    ) -> None:
        """The promise in ``_sync_libraries_after_link``'s own docstring: a failure here
        must never surface as an error. It has to keep holding, so it is pinned here
        rather than left as prose alone.
        """
        from reaper.api.plex import _sync_libraries_after_link

        async def _boom(_self: PlexClient) -> NoReturn:
            raise PlexError("no route to host")

        monkeypatch.setattr(PlexClient, "video_sections", _boom)
        request = Request({"type": "http", "app": client.app, "headers": []})

        await _sync_libraries_after_link(request)  # returns, does not raise

        assert client.get("/api/settings/plex/libraries").json() == []


class TestConnectionTestCarriesTheMapping:
    """A passing test hands back what the connection still has to map.

    The add form gates Save on this call, and an instance that is not saved yet has no id,
    so there is no second question to ask. Whatever the operator must decide has to arrive
    on the pass itself, since the folder map would otherwise only ever appear on a service
    that is already saved, which is never where a first-run operator is.
    """

    def _pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def ok(*_a: object, **_k: object) -> instances_service.TestResult:
            return instances_service.TestResult(
                ok=True, detail=Reason("legacy", {"text": "Connected to Radarr."}), version="5.4.6"
            )

        monkeypatch.setattr(instances_service, "test_connection", ok)

    def test_a_passing_arr_test_returns_its_root_folders(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._pass(monkeypatch)

        async def folders(
            *_a: object, **_k: object
        ) -> list[instances_service.RootFolderSuggestion]:
            return [
                instances_service.RootFolderSuggestion(path="/movies", suggested_library="Movies"),
                instances_service.RootFolderSuggestion(path="/movies-4k", suggested_library=None),
            ]

        monkeypatch.setattr(instances_service, "probe_root_folders", folders)
        body = client.post(
            "/api/settings/instances/test",
            json={"kind": "radarr", "base_url": "http://r.local:7878", "api_key": "k"},
        ).json()

        assert body["ok"] is True
        assert [f["path"] for f in body["root_folders"]] == ["/movies", "/movies-4k"]
        # The suggestion rides along, and "cannot tell" stays null rather than becoming a guess.
        assert body["root_folders"][0]["suggested_library"] == "Movies"
        assert body["root_folders"][1]["suggested_library"] is None
        # Nothing was read that a Seerr would have, and the read landed, so no error is claimed.
        assert body["seerr_services"] == []
        assert body["map_error_reason"] is None

    def test_a_failed_test_reads_nothing_to_map(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing was reached, so there is nothing to have read, and no probe is attempted."""

        async def bad(*_a: object, **_k: object) -> instances_service.TestResult:
            return instances_service.TestResult(
                ok=False, detail=Reason("legacy", {"text": "Radarr refused that key."})
            )

        monkeypatch.setattr(instances_service, "test_connection", bad)

        async def never(*_a: object, **_k: object) -> list[instances_service.RootFolderSuggestion]:
            raise AssertionError("the folder read must not run for a connection that failed")

        monkeypatch.setattr(instances_service, "probe_root_folders", never)
        body = client.post(
            "/api/settings/instances/test",
            json={"kind": "radarr", "base_url": "http://r.local:7878", "api_key": "nope"},
        ).json()

        assert body["ok"] is False
        assert body["root_folders"] == []
        assert body["map_error_reason"] is None

    def test_an_unreadable_folder_list_is_said_apart_from_an_empty_one(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The credentials really were proved, so the test still passes. But the empty
        list must not read as "this instance has no folders", which is a claim nobody
        checked. The two states are told apart by ``map_error_reason``, never by the empty
        list.
        """
        self._pass(monkeypatch)

        async def boom(*_a: object, **_k: object) -> list[instances_service.RootFolderSuggestion]:
            raise IntegrationError("radarr", "connection reset")

        monkeypatch.setattr(instances_service, "probe_root_folders", boom)
        body = client.post(
            "/api/settings/instances/test",
            json={"kind": "radarr", "base_url": "http://r.local:7878", "api_key": "k"},
        ).json()

        # A folder list that could not be read never turns a reachable service into a
        # failed test. Refusing the save over it would strand an *arr that answers
        # /system/status but not /rootfolder.
        assert body["ok"] is True
        assert body["root_folders"] == []
        # Plain language, not the raw exception. Pasting `str(exc)` would put "radarr:
        # connection reset" in front of someone trying to get a URL and a key right. This
        # is the exact string shape `explain_failure` exists to prevent.
        reason = body["map_error_reason"]
        assert reason["k"] == "mapError"
        explained = instances_service.explain_failure(
            InstanceKind.RADARR, IntegrationError("radarr", "connection reset")
        )
        assert reason["p"] == {"error": to_wire(explained)}
        assert "connection reset" not in reaper.refusal.english(explained)
        assert "couldn't read what to map" in reason_text(
            from_wire(reason), namespace="services.modal"
        )

    def test_a_seerr_test_returns_the_portals_services(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._pass(monkeypatch)

        async def services(
            *_a: object, **_k: object
        ) -> list[instances_service.ServiceInstanceSuggestion]:
            return [
                instances_service.ServiceInstanceSuggestion(
                    service_id=0, kind="radarr", name="Movies", is_4k=False, suggested_instance_id=4
                )
            ]

        monkeypatch.setattr(instances_service, "probe_seerr_services", services)
        body = client.post(
            "/api/settings/instances/test",
            json={"kind": "seerr", "base_url": "http://s.local:5055", "api_key": "k"},
        ).json()

        assert body["ok"] is True
        assert [s["name"] for s in body["seerr_services"]] == ["Movies"]
        assert body["seerr_services"][0]["suggested_instance_id"] == 4
        # A Seerr has no root folders, so that list stays empty rather than being invented.
        assert body["root_folders"] == []

    def test_both_routes_publishing_a_service_list_send_the_same_shape(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SeerrServiceOut`` is built at two routes, the pre-save probe above and the
        saved instance's own list. Both read the same service record, and only one of them
        had its body asserted, so the pair could diverge without a test noticing."""
        self._pass(monkeypatch)
        suggestion = instances_service.ServiceInstanceSuggestion(
            service_id=7, kind="sonarr", name="Shows", is_4k=True, suggested_instance_id=None
        )

        async def probe(
            *_a: object, **_k: object
        ) -> list[instances_service.ServiceInstanceSuggestion]:
            return [suggestion]

        monkeypatch.setattr(instances_service, "probe_seerr_services", probe)
        monkeypatch.setattr(instances_service, "seerr_services", probe)

        created = client.post(
            "/api/settings/instances",
            json={
                "kind": "seerr",
                "name": "Portal",
                "base_url": "http://s.local:5055",
                "api_key": "k",
            },
        )
        assert created.status_code == 200, created.text

        from_probe = client.post(
            "/api/settings/instances/test",
            json={"kind": "seerr", "base_url": "http://s.local:5055", "api_key": "k"},
        ).json()["seerr_services"]
        from_saved = client.get(
            f"/api/settings/instances/{created.json()['id']}/seerr-services"
        ).json()

        assert from_probe == from_saved
        assert from_probe == [
            {
                "service_id": 7,
                "kind": "sonarr",
                "name": "Shows",
                "is_4k": True,
                "suggested_instance_id": None,
            }
        ]


class TestCreateStoresTheMapping:
    def test_a_new_arr_is_created_with_the_map_made_on_the_add_form(
        self, client: TestClient
    ) -> None:
        """The mapping is made before the save now, so it has to survive the create. If it
        were sent only on the update route, a first Radarr's HD/4K map would be silently
        dropped, and the operator would have to reopen the service and make it again.
        """
        created = client.post(
            "/api/settings/instances",
            json={
                "kind": "radarr",
                "name": "HD",
                "base_url": "http://r.local:7878",
                "api_key": "k",
                "plex_library_map": {"/movies": "Movies"},
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["plex_library_map"] == {"/movies": "Movies"}
        # And it is stored, not merely echoed.
        listed = client.get("/api/settings/instances").json()
        assert listed[0]["plex_library_map"] == {"/movies": "Movies"}

    def test_an_omitted_map_stays_empty(self, client: TestClient) -> None:
        created = client.post(
            "/api/settings/instances",
            json={"kind": "radarr", "name": "HD", "base_url": "http://r.local", "api_key": "k"},
        )
        assert created.json()["plex_library_map"] == {}


class TestTheDiscordTestRouteSendsATypedReason:
    """``POST /api/settings/notifications/test``'s three outcomes. The server sends an id,
    never a sentence."""

    WEBHOOK = "https://discord.com/api/webhooks/123/token-is-a-secret"

    def test_a_successful_post_says_posted(
        self, client: TestClient, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.post(self.WEBHOOK).mock(return_value=httpx.Response(204))
        body = client.post(
            "/api/settings/notifications/test", json={"webhook_url": self.WEBHOOK}
        ).json()
        assert body["ok"] is True
        assert body["reason"] == {"k": "posted", "p": None}

    def test_a_rejected_post_says_failed(
        self, client: TestClient, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.post(self.WEBHOOK).mock(
            return_value=httpx.Response(400, json={"message": "bad"})
        )
        body = client.post(
            "/api/settings/notifications/test", json={"webhook_url": self.WEBHOOK}
        ).json()
        assert body["ok"] is False
        assert body["reason"] == {"k": "failed", "p": None}

    def test_no_stored_webhook_says_not_configured(self, client: TestClient) -> None:
        body = client.post("/api/settings/notifications/test", json={}).json()
        assert body["ok"] is False
        assert body["reason"] == {"k": "not_configured", "p": None}


#: The ids ``test_notifications`` can emit (``api/settings.py``). Hand-maintained, since
#: they are inline literals across the route's three branches rather than an enum.
_DISCORD_TEST_RESULT_IDS: frozenset[str] = frozenset({"posted", "failed", "not_configured"})


class TestTheDiscordTestResultVocabulary:
    """Every id ``test_notifications`` can emit, under ``services.discord.testResult.*``,
    checked both directions, modeled on ``test_review_chips.TestTheChipVocabulary``.
    """

    def test_the_id_population_is_pinned(self) -> None:
        assert len(_DISCORD_TEST_RESULT_IDS) == 3

    def test_every_id_has_catalog_copy(self) -> None:
        entries = set(catalog("services.discord.testResult"))
        missing = _DISCORD_TEST_RESULT_IDS - entries
        assert not missing, f"ids with no services.discord.testResult entry: {sorted(missing)}"

    def test_every_catalog_entry_has_a_producer(self) -> None:
        entries = set(catalog("services.discord.testResult"))
        orphaned = entries - _DISCORD_TEST_RESULT_IDS
        assert not orphaned, f"services.discord.testResult entries with no id: {sorted(orphaned)}"


class TestTheMapErrorVocabulary:
    """``test_new_instance``'s one Plex-couldn't-map reason, checked both directions. A
    population of one id, the sibling of ``lists.py``'s ``plexError``.
    """

    def test_the_id_has_a_catalog_entry(self) -> None:
        assert "mapError" in catalog("services.modal")

    def test_the_entry_takes_the_integration_text_as_a_param(self) -> None:
        assert "{error}" in catalog("services.modal")["mapError"]
