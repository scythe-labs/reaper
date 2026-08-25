# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings -> General, the API key lane, the docs lockdown, and the Logs tab.

The rules pinned here:

* the stock ``/docs`` and ``/openapi.json`` are gone -- the API description is served
  signed-in-only at ``/api/docs`` and ``/api/openapi.json`` (the second review pass's
  lesson applied forward: nothing outside ``/api`` is authenticated, so nothing
  sensitive may live outside ``/api``);
* the API key authenticates without a cookie and without the CSRF header (no cookie,
  no CSRF risk), backs off per address on bad guesses, and writes ONLY the automation
  allowlist -- scanning, planning, the policy, the run limits -- so the deletion switch,
  execute, sign-in and every other setting stay behind the browser, and the reference's
  auth box says so in those terms;
* reverse-proxy trust is off by default, applies immediately on save, and
  ``client_ip`` only honors a forwarded chain when the peer itself is a listed proxy;
* the log ring is redacted before storage, polls incrementally by sequence number, and
  the recording level applies instantly and persists (stored value over env seed).
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args
from zoneinfo import ZoneInfo

import pytest
import structlog
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session as SyncSession
from starlette.requests import Request

from reaper import logbuffer
from reaper.api import settings as settings_api
from reaper.api.middleware import (
    _API_KEY_READS_DENIED,
    _API_KEY_WRITES,
    _SIGNED_IN_ONLY_READS,
    CSRF_HEADER,
    CSRF_VALUE,
    _api_key_allowed,
    api_key_refused,
    api_key_scope_description,
    api_key_throttle,
    no_credential_needed,
)
from reaper.auth.cookie import DOCUMENTED_SESSION_COOKIE
from reaper.auth.proxy import client_ip, parse_proxy_networks
from reaper.config import Settings, parse_trusted_proxies
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.models import AppSetting
from reaper.logging import configure_logging
from reaper.main import create_app
from reaper.services import app_settings
from tests._auth import login

#: How many served operations are fenced to the signed-in browser, hand-reconciled. Every
#: irreversible authority plus every setting and credential write; an API key gets scanning,
#: planning, the policy and the reap profile. The number is here rather than in a docstring
#: because it was in one for two releases, drifting once per route added while
#: ``test_the_session_scheme_is_declared`` said "counted, not remembered" and asserted
#: nothing (rule 144).
#: -1 for `PUT /api/settings/notifications/language`, gone: the language is one setting now and
#: rides `PUT /api/settings/general`, which was already counted here.
FENCED_OPERATIONS = 48


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
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


def _store_raw(tmp_path: Path, key: str, value: object) -> None:
    """Put an app-setting row straight into the database the ``client`` fixture is on,
    holding whatever shape an older build left there. Goes around the API deliberately: the
    point is a stored value today's request models would never let through."""
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    with SyncSession(engine) as session:
        session.merge(
            AppSetting(key=key, value_json=json.dumps(value), updated_at=datetime.now(UTC))
        )
        session.commit()
    engine.dispose()


def _force_text_updated_at(tmp_path: Path, raw: str) -> None:
    """Overwrite every app-setting row's ``updated_at`` with a TEXT value. Raw SQL because the
    ORM type is what keeps these columns integer, so nothing else can put a string there."""
    engine = sa_create_engine(Settings(data_dir=tmp_path, secret_key="k").sync_database_url)
    with engine.begin() as conn:
        conn.execute(sa_text("UPDATE app_setting SET updated_at = :raw"), {"raw": raw})
    engine.dispose()


def _bare(client: TestClient) -> TestClient:
    """A second client over the SAME app: no cookies, no CSRF header, no session."""
    return TestClient(client.app)


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
            # Null, not "en": nobody has chosen, and the browser seeds it on first sign-in.
            # A default of "en" here would be the server asserting a choice it never saw.
            "language": None,
            "api_key_set": False,
            "expand_seasons_mode": "off",
            "default_spare_days": 0,
            "proxy_trust_enabled": False,
            "trusted_proxies": [],
            # The suite runs from source, which is not a desktop build, so the
            # Desktop app group is absent; TestDesktopSettings drives the other arm.
            "desktop": None,
        }

    def test_a_language_is_stored_and_read_back_as_sent(self, client: TestClient) -> None:
        data = client.put("/api/settings/general", json={"language": "pt-BR"}).json()
        assert data["language"] == "pt-BR"
        assert client.get("/api/settings/general").json()["language"] == "pt-BR"

    def test_a_language_with_no_backend_catalog_is_still_stored(self, client: TestClient) -> None:
        """The browser ships a translation a release before ``backend.json`` does, so the
        picker offers tags this process cannot compose a notification in. Refusing one would
        stop the operator setting the language they are already reading the app in; storing it
        is what makes the notification start speaking it the release that catalog lands. Rule
        141: ``de`` on purpose, a tag no fixture and no shipped backend catalog holds, so a
        pass cannot come from the value happening to be the default."""
        assert client.put("/api/settings/general", json={"language": "de"}).status_code == 200
        assert client.get("/api/settings/general").json()["language"] == "de"

    def test_a_value_that_is_not_a_language_tag_is_refused(self, client: TestClient) -> None:
        client.put("/api/settings/general", json={"language": "es"})
        response = client.put("/api/settings/general", json={"language": "not a tag!"})
        assert response.status_code == 422
        assert response.json()["code"] == "error.settings.language_invalid"
        # The bad value never landed; the previous language still stands.
        assert client.get("/api/settings/general").json()["language"] == "es"

    def test_a_text_updated_at_does_not_take_this_page_down(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """``updated_at`` is an epoch integer, and a hand ``sqlite3`` edit leaves a string
        there. ``session.get`` decodes every column of the row it fetches, so one such row
        used to 500 this route over a column no setting reads (#937)."""
        client.put("/api/settings/general", json={"language": "es"})
        _force_text_updated_at(tmp_path, "2026-08-24T14:30:56.744603+00:00")

        response = client.get("/api/settings/general")

        assert response.status_code == 200
        assert response.json()["language"] == "es"

    def test_a_valid_accent_is_saved_lowercased(self, client: TestClient) -> None:
        data = client.put("/api/settings/general", json={"accent_color": "#4F46E5"}).json()
        assert data["accent_color"] == "#4f46e5"

    def test_a_malformed_accent_is_refused_and_changes_nothing(self, client: TestClient) -> None:
        client.put("/api/settings/general", json={"accent_color": "#4f46e5"})
        response = client.put("/api/settings/general", json={"accent_color": "blue"})
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "error.settings.accent_color_invalid"
        assert "#" in body["detail"]
        # The bad value never landed; the previous color still stands.
        assert client.get("/api/settings/general").json()["accent_color"] == "#4f46e5"

    def test_an_empty_accent_resets_to_the_default(self, client: TestClient) -> None:
        client.put("/api/settings/general", json={"accent_color": "#000000"})
        data = client.put("/api/settings/general", json={"accent_color": ""}).json()
        assert data["accent_color"] == "#25c3ff"

    def test_every_expand_seasons_mode_round_trips(self, client: TestClient) -> None:
        # Off on a fresh install, so an existing library keeps its collapsed cards.
        assert client.get("/api/settings/general").json()["expand_seasons_mode"] == "off"
        for mode in app_settings.EXPAND_SEASONS_MODES:
            data = client.put("/api/settings/general", json={"expand_seasons_mode": mode}).json()
            assert data["expand_seasons_mode"] == mode
            assert client.get("/api/settings/general").json()["expand_seasons_mode"] == mode

    def test_an_unknown_expand_seasons_mode_is_refused_and_changes_nothing(
        self, client: TestClient
    ) -> None:
        client.put("/api/settings/general", json={"expand_seasons_mode": "mobile"})
        refused = client.put("/api/settings/general", json={"expand_seasons_mode": "tablet"})
        assert refused.status_code == 422
        assert client.get("/api/settings/general").json()["expand_seasons_mode"] == "mobile"

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            (True, "both"),  # the old switch on meant every screen
            (False, "off"),
            ("tablet", "off"),  # a hand-edited row, or one from a build since rolled back
            (3, "off"),
        ],
    )
    def test_the_boolean_this_replaced_still_reads(
        self, client: TestClient, tmp_path: Path, stored: object, expected: str
    ) -> None:
        """The switch became a four-way choice with no migration, so the row an install
        already has is still the boolean and must keep answering. Anything the mode set does
        not contain reads as the shipped default rather than raising a display preference
        into a 500."""
        _store_raw(tmp_path, app_settings.EXPAND_SEASONS_MODE_KEY, stored)
        assert client.get("/api/settings/general").json()["expand_seasons_mode"] == expected

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
        body = response.json()
        assert body["code"] == "error.settings.application_url_invalid"
        assert "http" in body["detail"]

    def test_a_malformed_proxy_entry_is_refused(self, client: TestClient) -> None:
        response = client.put("/api/settings/general", json={"trusted_proxies": ["not-an-address"]})
        assert response.status_code == 422

    def test_one_bad_field_writes_none_of_the_others(self, client: TestClient) -> None:
        """The General panel's save bar sends every unsaved field in ONE request, so a body
        carrying five good fields and one bad one is the shape the operator actually produces.
        This route's docstring promises "nothing is changed" on a refusal, and it holds because
        all four validations run before the first write and one commit ends them -- but every
        other test here sends a single field, so nothing pinned it. Moving any `set_*` above a
        check would half-apply a six-field save with the operator told it failed."""
        before = client.get("/api/settings/general").json()

        response = client.put(
            "/api/settings/general",
            json={
                "application_name": "Media Reaper",
                "timezone": "America/New_York",
                "accent_color": "#25c3ff",
                "default_spare_days": 30,
                "trusted_proxies": ["172.16.0.0/12"],
                # The one that is refused, checked before any of the five is written.
                "application_url": "reaper.local",
            },
        )
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "error.settings.application_url_invalid"
        assert "http" in body["detail"]
        assert client.get("/api/settings/general").json() == before

        # The same body with that field corrected writes all six, so the test cannot pass by
        # the route simply refusing everything.
        response = client.put(
            "/api/settings/general",
            json={
                "application_name": "Media Reaper",
                "timezone": "America/New_York",
                "accent_color": "#25c3ff",
                "default_spare_days": 30,
                "trusted_proxies": ["172.16.0.0/12"],
                "application_url": "https://reaper.example.com",
            },
        )
        assert response.status_code == 200
        saved = response.json()
        assert saved["application_name"] == "Media Reaper"
        assert saved["timezone"] == "America/New_York"
        assert saved["accent_color"] == "#25c3ff"
        assert saved["default_spare_days"] == 30
        assert saved["trusted_proxies"] == ["172.16.0.0/12"]
        assert saved["application_url"] == "https://reaper.example.com"

    def test_saving_proxy_trust_applies_immediately(self, client: TestClient) -> None:
        client.put(
            "/api/settings/general",
            json={"proxy_trust_enabled": True, "trusted_proxies": ["172.16.0.0/12"]},
        )
        networks = client.app.state.trusted_proxies  # type: ignore[attr-defined]
        assert len(networks) == 1

        client.put("/api/settings/general", json={"proxy_trust_enabled": False})
        assert client.app.state.trusted_proxies == ()  # type: ignore[attr-defined]

    def test_every_general_field_is_a_row_or_a_declared_exception(self) -> None:
        """The route walks ``_GENERAL_FIELDS`` twice, so a field added to the request model
        and forgotten there is accepted, echoed back unchanged, and silently never stored.
        That is the shape issue #90 had, and no route test can see it: a field nobody wrote
        a case for is a field nobody sends.

        Derived from the model rather than listed here (rule 103), so the reconciliation is
        with the declaration and not with a second copy of it. A field that genuinely
        cannot be a row joins ``_GENERAL_FIELD_EXCEPTIONS`` with the reason, which is what
        makes this fail *loudly* rather than invite a silencing edit.
        """
        covered = {field.name for field in settings_api._GENERAL_FIELDS}
        declared = covered | set(settings_api._GENERAL_FIELD_EXCEPTIONS)
        assert declared == set(settings_api.GeneralSettingsIn.model_fields), (
            "every GeneralSettingsIn field is either an app-settings row in _GENERAL_FIELDS "
            "or carries its reason in _GENERAL_FIELD_EXCEPTIONS."
        )
        assert not covered & set(settings_api._GENERAL_FIELD_EXCEPTIONS), (
            "a field cannot be both a row and an exception to being one."
        )
        assert all(why.strip() for why in settings_api._GENERAL_FIELD_EXCEPTIONS.values()), (
            "an exception with an empty reason is a silenced gate."
        )

    def test_the_checking_pass_refuses_with_no_session_in_hand(self) -> None:
        """The no-partial-write promise rests on two independent layers, and the route tests
        cannot tell them apart. ``test_one_bad_field_writes_none_of_the_others`` above and
        ``test_a_refused_tray_writes_none_of_the_settings_beside_it`` below both hold whether
        the checks run before the writes or after them, because the single commit at the end
        of the route rolls a half-applied save back either way. Measured, not argued: the
        desktop checks were moved back below the write loop and both stayed green. So neither
        one discriminates the layers, and neither is named as if it did (rule 118).

        This is the half that can be pinned. The checking pass takes the payload and nothing
        else, so it holds no session and cannot write through one. Thread a session into
        either signature and this fails.
        """
        assert list(inspect.signature(settings_api._cleaned_general_values).parameters) == [
            "payload"
        ]
        assert list(inspect.signature(settings_api._validated_desktop_values).parameters) == [
            "payload"
        ]

        with pytest.raises(HTTPException) as bad_zone:
            settings_api._cleaned_general_values(
                settings_api.GeneralSettingsIn(
                    application_name="Media Reaper", timezone="Nowhere/Nothing"
                )
            )
        assert getattr(bad_zone.value, "code", None) == "error.settings.timezone_unknown"
        assert "time zone" in bad_zone.value.detail
        # The suite runs from source, so `desktop_platform()` is None and this is the
        # container operator's refusal, reached with no session and nothing written.
        with pytest.raises(HTTPException) as bad_platform:
            settings_api._validated_desktop_values(settings_api.GeneralSettingsIn(tray=False))
        assert getattr(bad_platform.value, "code", None) == "error.settings.desktop_only"
        assert "Windows and macOS apps" in bad_platform.value.detail


class TestDesktopSettings:
    """The Desktop app group's lane: present only on a frozen desktop build, saved to
    launcher.conf so the next start reads it, mirrored into the environment so this
    process keeps answering with the value it just accepted."""

    @pytest.fixture
    def desktop_env(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """A clean slate for the two keys, restored even though the PUT route writes
        os.environ directly (monkeypatch cannot see that write, rule 133)."""
        import os

        monkeypatch.delenv("REAPER_TRAY", raising=False)
        monkeypatch.delenv("REAPER_DOCK_ICON", raising=False)
        yield
        os.environ.pop("REAPER_TRAY", None)
        os.environ.pop("REAPER_DOCK_ICON", None)

    def test_saving_tray_off_a_desktop_build_is_refused(self, client: TestClient) -> None:
        response = client.put("/api/settings/general", json={"tray": False})
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "error.settings.desktop_only"
        assert "Windows and macOS apps" in body["detail"]

    def test_a_refused_tray_writes_none_of_the_settings_beside_it(self, client: TestClient) -> None:
        """The other half of ``test_one_bad_field_writes_none_of_the_others``, for the one
        refusal that is not a settings row. The save bar sends every unsaved field at once,
        so a container operator with the Desktop group somehow on screen sends this shape,
        and nothing pinned it: that test's body carries no ``tray``.

        Named for what it pins, which is the operator-visible promise that a refusal changes
        nothing. It does not discriminate the two layers that deliver it, and
        ``test_the_checking_pass_refuses_with_no_session_in_hand`` above says why."""
        before = client.get("/api/settings/general").json()

        response = client.put(
            "/api/settings/general",
            json={"application_name": "Media Reaper", "accent_color": "#4f46e5", "tray": False},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "error.settings.desktop_only"
        assert "Windows and macOS apps" in body["detail"]
        assert client.get("/api/settings/general").json() == before

    def test_a_desktop_save_lands_in_launcher_conf_and_the_next_read(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        desktop_env: None,
    ) -> None:
        from reaper import launcher

        monkeypatch.setattr(launcher, "desktop_platform", lambda *a, **k: "macos")
        fresh = client.get("/api/settings/general").json()["desktop"]
        assert fresh == {"platform": "macos", "tray": True, "dock_icon": False}

        saved = client.put("/api/settings/general", json={"tray": False, "dock_icon": True}).json()[
            "desktop"
        ]
        assert saved == {"platform": "macos", "tray": False, "dock_icon": True}

        conf = (tmp_path / "launcher.conf").read_text(encoding="utf-8")
        assert "REAPER_TRAY=false" in conf
        assert "REAPER_DOCK_ICON=true" in conf
        # A later read answers from what was just accepted, not the boot-time value.
        again = client.get("/api/settings/general").json()["desktop"]
        assert again == {"platform": "macos", "tray": False, "dock_icon": True}

    def test_a_failed_commit_leaves_launcher_conf_alone(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        desktop_env: None,
    ) -> None:
        """The route promises that a refusal changes nothing, and one write was outside the
        transaction that delivers it (#748).

        `launcher.conf` is a file and `os.environ` is process state, so neither rolls back.
        Written before the commit, a commit that then failed left the switch on in the file
        and echoed back by `_desktop_out` from the environment, while the five settings rows
        saved beside it went back and the operator was told the save failed.

        The commit is broken at the session rather than at the disk, because the window this
        is about is the commit failing for any reason at all. The three things the failure
        must leave untouched are asserted separately: the file, the environment, and the
        rows. A test reading only the response would pass with all three still moved.

        **Only the route's own commit is broken**, found by frame name. A blanket patch on
        ``AsyncSession.commit`` takes the auth middleware's commit down first, so the
        request dies before ``put_general`` runs at all and the test passes against the
        broken order it was written to catch (which is how it was written first, and it
        passed).
        """
        import os
        import traceback

        from sqlalchemy.ext.asyncio import AsyncSession

        from reaper import launcher

        monkeypatch.setattr(launcher, "desktop_platform", lambda *a, **k: "macos")
        before = client.get("/api/settings/general").json()
        real_commit = AsyncSession.commit

        async def _fails(session: AsyncSession) -> None:
            if any(frame.name == "put_general" for frame in traceback.extract_stack()):
                raise RuntimeError("the database is locked")
            await real_commit(session)

        monkeypatch.setattr(AsyncSession, "commit", _fails)

        with pytest.raises(RuntimeError, match="locked"):
            client.put(
                "/api/settings/general",
                json={"application_name": "Media Reaper", "tray": False, "dock_icon": True},
            )

        monkeypatch.undo()
        monkeypatch.setattr(launcher, "desktop_platform", lambda *a, **k: "macos")
        assert not (tmp_path / "launcher.conf").exists()
        assert "REAPER_TRAY" not in os.environ
        assert "REAPER_DOCK_ICON" not in os.environ
        assert client.get("/api/settings/general").json() == before

    def test_the_dock_icon_is_refused_off_the_mac_app(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        desktop_env: None,
    ) -> None:
        """Nothing on Windows reads REAPER_DOCK_ICON, so accepting the field there
        would persist an inert line that every later read echoes back as a live
        switch — the schema's own docstring promises the refusal."""
        from reaper import launcher

        monkeypatch.setattr(launcher, "desktop_platform", lambda *a, **k: "windows")
        response = client.put("/api/settings/general", json={"dock_icon": True})
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "error.settings.dock_icon_macos_only"
        assert "macOS app" in body["detail"]
        conf = tmp_path / "launcher.conf"
        assert not conf.exists() or "REAPER_DOCK_ICON" not in conf.read_text(encoding="utf-8")
        # tray, the knob Windows does have, still saves on its own.
        assert client.put("/api/settings/general", json={"tray": False}).status_code == 200

    def test_a_desktop_save_keeps_the_operator_lines_around_it(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        desktop_env: None,
    ) -> None:
        """launcher.conf is the operator's file; Settings edits its own keys in place
        and never rewrites the rest."""
        from reaper import launcher

        monkeypatch.setattr(launcher, "desktop_platform", lambda *a, **k: "macos")
        (tmp_path / "launcher.conf").write_text(
            "# my note\nREAPER_PORT=8421\nREAPER_TRAY=true\n", encoding="utf-8"
        )
        client.put("/api/settings/general", json={"tray": False})
        conf = (tmp_path / "launcher.conf").read_text(encoding="utf-8")
        assert "# my note" in conf
        assert "REAPER_PORT=8421" in conf
        assert conf.count("REAPER_TRAY") == 1
        assert "REAPER_TRAY=false" in conf


class TestReverseProxyEnvSeed:
    """REAPER_PROXY_TRUST_ENABLED / REAPER_TRUSTED_PROXIES seed the first-boot default;
    the stored value (Settings -> General) wins thereafter, exactly like the deletion
    switch. A declarative deployment can ship trust configured with no UI visit."""

    def _seeded(self, tmp_path: Path) -> Settings:
        settings = Settings(
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

    def test_removing_the_key_closes_the_lane(self, client: TestClient) -> None:
        """Rotating swaps one working key for another, so it never closes this lane.

        An operator who generated a key for a one-off script had no way to shut the header
        credential off again. Removing it must also stop it authenticating immediately:
        the check reads a digest held on the app, not the database, so a key deleted from
        storage alone would have kept working until the next restart.
        """
        key = self._issue(client)
        bare = _bare(client)
        assert bare.get("/api/settings/general", headers={"X-Api-Key": key}).status_code == 200

        removed = client.delete("/api/settings/general/api-key")
        assert removed.status_code == 200, removed.text
        assert removed.json() == {"removed": True}

        assert bare.get("/api/settings/general", headers={"X-Api-Key": key}).status_code == 401
        assert client.get("/api/settings/general/api-key").status_code == 404
        assert client.get("/api/settings/general").json()["api_key_set"] is False

    def test_a_key_under_a_rotated_secret_reads_as_not_set(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """The flag answers for a key this install can use, not for a row.

        A key encrypted under a secret that has since rotated cannot be decrypted, so nothing
        can authenticate with it and the reveal route 404s. Reading the row instead left the
        General panel offering Show and Delete over a credential the operator's scripts were
        already being refused on, and no screen said why. Rule 76: the flag resolves the way
        the runtime does.
        """
        rotated_away = SecretBox("a-secret-key-this-install-no-longer-holds")
        _store_raw(tmp_path, app_settings.API_KEY_KEY, rotated_away.encrypt("unreadable"))

        assert client.get("/api/settings/general").json()["api_key_set"] is False
        assert client.get("/api/settings/general/api-key").status_code == 404

    def test_removing_a_key_that_is_not_there_is_not_an_error(self, client: TestClient) -> None:
        assert client.delete("/api/settings/general/api-key").status_code == 200

    def test_a_key_cannot_delete_itself(self, client: TestClient) -> None:
        """Deny-by-default covers the new route without naming it anywhere."""
        key = self._issue(client)
        bare = _bare(client)
        assert (
            bare.delete("/api/settings/general/api-key", headers={"X-Api-Key": key}).status_code
            == 403
        )
        # And it still works, because the refusal never reached the delete.
        assert bare.get("/api/settings/general", headers={"X-Api-Key": key}).status_code == 200

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
        """The logs are a running transcript of the library, and the download concatenates
        every rotating file, so one GET is the whole history (S-3).

        The privacy half of that reason holds now, and did not when this test was written:
        S-3 argued a key is told it reads "your library", meaning the catalog and not
        everyone's viewing, while ``/api/fairness/people/{identity}`` answered a bare key
        with one person's whole viewing breakdown. #117 closed that by moving the fairness
        reads behind the browser, so both descriptions of the fence can make the argument
        again. What this pins is narrower and stood on its own either way: the log FILE
        stays off a header credential.
        """
        key = self._issue(client)
        bare = _bare(client)
        headers = {"X-Api-Key": key}

        assert bare.get("/api/logs", headers=headers).status_code == 403
        assert bare.get("/api/logs/download", headers=headers).status_code == 403
        # Still readable in the browser, where the Logs tab lives.
        assert client.get("/api/logs").status_code == 200

    def test_a_refused_write_hears_which_writes_a_key_can_make(self, client: TestClient) -> None:
        """Rule 119: the expectation is the fence's contract, written out, not a read-back
        of the generator. The refused caller is told what the key CAN write, because a list
        of what it cannot is the one that falls behind the fence."""
        key = self._issue(client)
        bare = _bare(client)
        refused = bare.put(
            "/api/settings/safety", json={"enabled": True}, headers={"X-Api-Key": key}
        )
        assert refused.status_code == 403
        body = refused.json()
        assert body["code"] == "error.auth.api_key_write_denied"
        assert body["detail"] == (
            "This needs the web app, signed in. An API key writes only these: start a scan, "
            "plan a run and dry run it, edit the policy, and change the run limits and grace."
        )

    def test_a_denied_read_hears_which_reads_are_denied(self, client: TestClient) -> None:
        """The other half. A refused read is not helped by a list of writes, so it hears
        the exclusion list that explains it."""
        key = self._issue(client)
        bare = _bare(client)
        denied = bare.get("/api/logs", headers={"X-Api-Key": key})
        assert denied.status_code == 403
        body = denied.json()
        assert body["code"] == "error.auth.api_key_read_denied"
        assert body["detail"] == (
            "This needs the web app, signed in. An API key reads everything except the key "
            "itself, the backup download, the logs, who watched what, and who you are signed in as."
        )

    def test_a_key_cannot_read_one_persons_viewing(self, client: TestClient) -> None:
        """#117, driven the way it was found: with a live key and no cookie, against both
        fairness routes. Both answered 200, so an operator handing a key to a third-party
        dashboard handed over who watched what.

        The per-person route is the one that needs the subtree match. It is templated in
        the schema and concrete in a request, and an exact-path denylist matches neither
        spelling against the other -- so a denylist holding only ``/api/fairness`` would
        pass the first assertion here and fail the second, which is precisely the shape of
        the original bug.

        **This test is also the guard on a hand-written sentence, and it is the only one
        that can be.** ``test_the_sentence_leads_with_what_the_key_can_do`` pins the
        Settings paragraph against the declaration's PHRASES; the refusal in that
        paragraph ("it cannot ... see who watched what") rests on the declaration's PATHS,
        which no phrase test can see. Emptying this entry's paths while keeping its phrase
        leaves that guard green and both descriptions asserting a refusal the fence no
        longer makes, so the failures here name the file to edit (rule 144).
        """
        key = self._issue(client)
        bare = _bare(client)
        headers = {"X-Api-Key": key}
        twin = (
            "the fairness reads are open to a key again, so the hand-written refusal in "
            "frontend/src/components/GeneralPanel.tsx ('it cannot ... see who watched what') "
            "and the generated one in middleware.api_key_scope_description are both false. "
            "Fix the fence or change BOTH sentences"
        )

        assert bare.get("/api/fairness", headers=headers).status_code == 403, twin
        person = bare.get("/api/fairness/people/someone", headers=headers)
        assert person.status_code == 403, twin
        person_body = person.json()
        assert person_body["code"] == "error.auth.api_key_read_denied", twin
        assert "who watched what" in person_body["detail"], twin

        # The browser still reaches it: this fenced a credential, it did not retire a page.
        # 400 is the handler's own "Scales needs a Seerr and a Tautulli" on a fixture that
        # configures neither, which is the proof -- the guard passed the cookie through and
        # something behind it answered, where the key never got that far.
        signed_in = client.get("/api/fairness")
        assert signed_in.status_code == 400, signed_in.text
        signed_in_body = signed_in.json()
        assert signed_in_body["code"] == "error.fairness.needs_seerr_and_tautulli"
        assert "who watched what" not in signed_in_body["detail"]

    def test_the_refusal_never_denies_a_write_the_fence_allows(self, client: TestClient) -> None:
        """The regression, driven from both ends in one test.

        The refusal used to say "Changing any setting, arming deletion, and running a reap
        stay behind your password" while ``/api/profile`` sat in the write allowlist -- so
        it told the caller the run caps were out of reach in the request right before the
        one that turned them off (S-2). Neither half alone can catch that: the copy reads
        true until you drive the write it denies.
        """
        key = self._issue(client)
        bare = _bare(client)
        headers = {"X-Api-Key": key}

        refused_body = bare.put(
            "/api/settings/safety", json={"enabled": True}, headers=headers
        ).json()
        assert refused_body["code"] == "error.auth.api_key_write_denied"
        detail = refused_body["detail"]

        profile = bare.get("/api/profile", headers=headers)
        assert profile.status_code == 200, profile.text
        body = dict(profile.json())
        body["caps_enabled"] = False
        turned_off = bare.put("/api/profile", json=body, headers=headers)
        assert turned_off.status_code == 200, turned_off.text
        assert turned_off.json()["caps_enabled"] is False

        # So the refusal may not say otherwise, and must name it as something the key does.
        assert "change the run limits and grace" in detail
        assert "stay behind your password" not in detail

    def test_the_allowlist_matches_by_method_and_shape(self) -> None:
        # Reads are open to the key, except the handful that hand back more than a catalog.
        assert _api_key_allowed("GET", "/api/candidates") is True
        assert _api_key_allowed("GET", "/api/settings/general") is True
        assert _api_key_allowed("GET", "/api/settings/general/api-key") is False
        assert _api_key_allowed("GET", "/api/settings/backup/download") is False
        assert _api_key_allowed("GET", "/api/logs") is False
        assert _api_key_allowed("GET", "/api/logs/download") is False
        # Denied by subtree, so both spellings of the per-person route answer the same:
        # the schema's template, and the concrete path a request actually carries.
        assert _api_key_allowed("GET", "/api/fairness") is False
        assert _api_key_allowed("GET", "/api/fairness/people/{identity}") is False
        assert _api_key_allowed("GET", "/api/fairness/people/someone") is False
        # The subtree stops at a path boundary: a sibling starting with the same letters
        # is a different route and stays open.
        assert _api_key_allowed("GET", "/api/fairness-summary") is True
        # Writes are closed except the automation allowlist: scan, plan, policy.
        assert _api_key_allowed("POST", "/api/scan/start") is True
        assert _api_key_allowed("POST", "/api/policy") is True
        assert _api_key_allowed("POST", "/api/runs/12/dry-run") is True
        assert _api_key_allowed("POST", "/api/runs/12/execute") is False
        assert _api_key_allowed("PUT", "/api/settings/safety") is False
        assert _api_key_allowed("PUT", "/api/settings/general") is False
        assert _api_key_allowed("PUT", "/api/settings/plex/connection") is False
        # Recording a decision by hand is a signed-in act, not an automation one. Pinned
        # because #326 rests on it: the 404 these writes can raise is reachable from no
        # script, which is what settled it as a wording fix rather than a behavior one.
        # There used to be a third line here, `GET /api/whitelist` allowed, pinning that the
        # read was the one part of the keep list a key got. That route is gone and so is the
        # behavior, so the case retires rather than being handed a stand-in: `/api/candidates`
        # is already asserted above, and it is not the same question anyway -- an override on
        # an item the latest snapshot does not hold appears in neither.
        assert _api_key_allowed("POST", "/api/override") is False
        assert _api_key_allowed("DELETE", "/api/override/{media_key}") is False


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


class TestTheAuthBoxDescribesTheFence:
    """The reference offers the key on every operation, so its auth box is the only place
    a script author learns which ones it will get through. It used to say the key could do
    anything but turn deletion on, execute, or change sign-in -- while the fence refused
    every settings write, sparing a title, and every override. Rule 103: the sentence is
    generated from the allowlist, and these fail if a route slips past the generator.
    """

    def _writes(self, schema: dict[str, object]) -> list[tuple[str, str]]:
        paths: dict[str, dict[str, object]] = schema["paths"]  # type: ignore[assignment]
        return [
            (method.upper(), path)
            for path, methods in sorted(paths.items())
            for method in sorted(methods)
            if method.upper() not in {"GET", "HEAD", "OPTIONS"}
        ]

    def test_every_write_the_fence_allows_is_named_in_the_sentence(
        self, client: TestClient
    ) -> None:
        """Rule 118, named for what it actually discriminates: a second shape-matched
        branch added to ``_api_key_allowed`` with no phrase behind it.

        It does NOT catch a path added to ``_API_KEY_WRITES`` without a phrase -- that
        drift is structurally impossible, because ``_API_KEY_WRITE_ALLOW`` is a
        comprehension over the very tuple that carries the phrases, so ``unnamed == []``
        is a theorem for any path-shaped entry. The derivation is the guard there. What is
        still hand-written is the dry run's ``startswith``/``endswith`` test, which is
        admitted by shape and named by a phrase nothing ties to it; a third such branch
        would ship an undocumented automation authority, and this is what says so.
        """
        schema = client.get("/api/openapi.json").json()
        allowed = [(m, p) for m, p in self._writes(schema) if _api_key_allowed(m, p)]
        assert allowed, "the fence opened no writes at all, so this proves nothing"

        named = {path for _, paths in _API_KEY_WRITES for path in paths}
        # The dry run is matched by shape, and rides the "plan a run and dry run it" phrase.
        unnamed = [
            f"{m} {p}"
            for m, p in allowed
            if p not in named and not (p.startswith("/api/runs/") and p.endswith("/dry-run"))
        ]
        assert unnamed == [], f"reachable with a key, but the auth box never says so: {unnamed}"
        assert "dry run" in api_key_scope_description()

    def test_every_path_the_sentence_is_built_from_is_a_real_route(
        self, client: TestClient
    ) -> None:
        """The other direction: a retired route leaves a phrase describing something the
        operator can no longer do, and nothing else would notice."""
        schema = client.get("/api/openapi.json").json()
        served = set(schema["paths"])
        declared = {
            path
            for _, paths in (*_API_KEY_WRITES, *_API_KEY_READS_DENIED, *_SIGNED_IN_ONLY_READS)
            for path in paths
        }
        missing = sorted(declared - served)
        assert missing == [], f"named in the auth box, but no such route: {missing}"

    def test_the_served_box_carries_the_generated_sentence(self, client: TestClient) -> None:
        schema = client.get("/api/openapi.json").json()
        description = schema["components"]["securitySchemes"]["ApiKey"]["description"]
        assert description == api_key_scope_description()
        # Rule 21: this renders in the reference, so it is operator copy.
        assert "—" not in description

    def test_the_sentence_leads_with_what_the_key_can_do(self) -> None:
        """Rule 119: written from the fence's own contract, not read back off the
        generator. A key reads all but five things and writes four.

        Rule 103, and the second job this test does: it is also the drift guard for the
        sentence's HAND-WRITTEN twin, the API key help in Settings, General. That paragraph
        is the surface an operator actually decides on, no test in either tree asserts it,
        and nothing else fails when the fence moves under it. So every assertion here
        carries the pointer rather than leaving it to a comment nobody runs.
        """
        twin = (
            "the fence moved, so the hand-written twin in "
            "frontend/src/components/GeneralPanel.tsx has to move with it"
        )
        description = api_key_scope_description()
        assert (
            "reads everything except the key itself, the backup download, the logs, "
            "who watched what, and who you are signed in as" in description
        ), twin
        assert (
            "writes only these: start a scan, plan a run and dry run it, edit the policy, "
            "and change the run limits and grace" in description
        ), twin
        assert "Every other write is refused" in description, twin


class TestEveryOperationSaysWhichCredentialReachesIt:
    """The auth box tells the truth once; these put it on the operation the reader is
    looking at. A scheme applied document-wide renders a working auth box over every
    operation in the document, so try-it-out looked available on routes that answer 403 --
    the reader had no way to tell which without sending the request.
    """

    def _operations(self, client: TestClient) -> list[tuple[str, str, dict[str, Any]]]:
        schema = client.get("/api/openapi.json").json()
        return [
            (method.upper(), path, operation)
            for path, methods in sorted(schema["paths"].items())
            for method, operation in sorted(methods.items())
        ]

    def test_the_session_scheme_is_declared(self, client: TestClient) -> None:
        """A fenced operation narrows to this scheme, so a missing declaration would leave
        every fenced operation pointing at a credential the document never defines.

        **Counted here, not remembered.** This docstring used to name the figure (42, then
        40 before #117 moved the two fairness reads behind the browser) and nothing asserted
        it, so it read as measured while drifting once per route added: it was 42 against a
        real 48. The count moved into the assertion below, where a wrong one fails.
        """
        schema = client.get("/api/openapi.json").json()
        schemes = schema["components"]["securitySchemes"]
        assert schemes["Session"]["in"] == "cookie"
        assert schemes["Session"]["name"] == DOCUMENTED_SESSION_COOKIE
        assert "—" not in schemes["Session"]["description"]  # rule 21
        # Order, not just membership: Scalar preselects the first scheme and sends its
        # placeholder, and a placeholder API key makes the guard answer its own reference
        # "That API key is not valid." A cookie placeholder cannot be sent by a page
        # script, so leading with Session is what leaves try-it-out working signed in.
        assert schema["security"] == [{"Session": []}, {"ApiKey": []}]

        # The figure this docstring used to carry, now where a wrong one fails. Reconcile it
        # by hand when a route is fenced or unfenced (rule 145): the walk below counts what
        # the document declares, and a fence that never reached the schema is missing from
        # both the count and the flag-shaped assertions above it.
        fenced = sum(
            1 for _, _, op in self._operations(client) if op.get("security") == [{"Session": []}]
        )
        assert fenced == FENCED_OPERATIONS, (
            f"{fenced} operations are fenced to the browser, not {FENCED_OPERATIONS}. If a "
            "route was deliberately fenced or unfenced, update FENCED_OPERATIONS; if not, a "
            "write just became reachable by an API key."
        )

    def test_the_routes_a_live_key_was_refused_on_are_marked(self, client: TestClient) -> None:
        """Rule 119: the expectation is the evidence from issue #104, driven with a real
        key against a real install, not a re-reading of the allowlist. Each of these
        answered 403 while the reference offered the key on it.
        """
        marked = {
            (m, p)
            for m, p, op in self._operations(client)
            if op.get("security") == [{"Session": []}]
        }
        for refused in (
            ("POST", "/api/override"),
            ("DELETE", "/api/override/{media_key}"),
            ("PUT", "/api/settings/plex"),
            ("POST", "/api/settings/notifications/test"),
            ("POST", "/api/runs/{run_id}/execute"),
            ("PUT", "/api/settings/safety"),
            # Same evidence, from #117: these two answered a live key 200 until the fence
            # took them, and the reference offered the key on them throughout. The reads
            # are the case the marking is easiest to get wrong, because every other read
            # in the document really is key-reachable.
            ("GET", "/api/fairness"),
            ("GET", "/api/fairness/people/{identity}"),
        ):
            assert refused in marked, (
                f"a key is refused here and the reference does not say so: {refused}"
            )

    def test_the_automation_lane_is_left_reachable(self, client: TestClient) -> None:
        """The other half of the same claim: marking everything session-only would pass
        the test above and make the key look useless. These inherit both credentials."""
        marked = {
            (m, p)
            for m, p, op in self._operations(client)
            if op.get("security") == [{"Session": []}]
        }
        for reachable in (
            ("POST", "/api/scan/start"),
            ("POST", "/api/runs"),
            ("POST", "/api/runs/{run_id}/dry-run"),
            ("PUT", "/api/profile"),
            ("GET", "/api/candidates"),
        ):
            assert reachable not in marked, (
                f"a key reaches this, so it must not be fenced: {reachable}"
            )

    def test_a_signed_in_write_needs_the_header_the_session_scheme_names(
        self, client: TestClient
    ) -> None:
        """Rule 119: the scheme's copy is checked against the guard, not against itself.

        A bare cookie reads and does not write. That asymmetry is why the Session
        description names the header, and it is what a script author writing their own
        client meets on their first write. The reference page no longer meets it (#120,
        the test below), which changes who needs telling and changes nothing about the
        guard: if this stops being a refusal, the sentence naming the header is the line
        to delete.
        """
        panel = _bare(client)
        panel.cookies.update(client.cookies)

        # The half a bare cookie is enough for.
        assert panel.get("/api/candidates").status_code == 200
        # The same credential, one unsafe method, no header.
        refused = panel.post("/api/override", json={})
        assert refused.status_code == 403
        body = refused.json()
        assert body["code"] == "error.auth.csrf_blocked"
        assert "CSRF" in body["detail"]

        schema = client.get("/api/openapi.json").json()
        session_scheme = schema["components"]["securitySchemes"]["Session"]
        assert "X-Reaper-CSRF: 1" in session_scheme["description"]

    def test_the_reference_page_sends_the_csrf_header_it_names(self, client: TestClient) -> None:
        """#120: the only thing standing behind "reads and writes as you".

        That clause is hand-written, and the hook that earns it lives in a JavaScript
        string no other Python test reads. Delete the hook and the Session scheme goes
        back to promising a write that answers 403, with every gate still green -- the
        drift rule 144 is about, in the copy that was never generated. Driving the button
        needs a browser, so what is pinned here is narrower and still load-bearing: the
        page carries the hook, and it sends the header its own auth box names with the
        VALUE the guard accepts.

        Pinning the name alone was not enough, which is worth stating because it looked
        like it was. ``_csrf_ok`` requires exactly ``"1"``; a hook setting any other value
        leaves this file green while every try-it-out write 403s again -- the same silent
        outcome as deleting the hook, reached through a one-character edit. So the value is
        read out of the page and compared to what the guard demands, rather than assumed.
        """
        page = client.get("/api/docs").text
        described = client.get("/api/openapi.json").json()["components"]["securitySchemes"][
            "Session"
        ]["description"]

        assert "onBeforeRequest" in page, (
            "the reference page dropped the hook, so try-it-out writes 403 again while "
            "the Session scheme in main.openapi_with_api_key still promises they work"
        )
        # The exact call the hook makes, value included, built from the guard's own
        # constants rather than restated here (rule 119).
        assert f"headers.set('{CSRF_HEADER}', '{CSRF_VALUE}')" in page, (
            f"the reference page must send {CSRF_HEADER} with the value _csrf_ok accepts "
            f"({CSRF_VALUE!r}); any other value 403s every try-it-out write while the "
            "Session scheme in main.openapi_with_api_key still promises they work. If you "
            "changed the constants in api/middleware.py, the SPA's own copy cannot follow "
            "them and must be edited by hand: frontend/src/api.ts's CSRF_HEADER, pinned by "
            "frontend/src/api.test.ts"
        )
        assert f"{CSRF_HEADER}: {CSRF_VALUE}" in described

    def test_the_open_route_that_refuses_a_key_is_marked_like_any_other(
        self, client: TestClient
    ) -> None:
        """Rule 119: driven with a real key, not read back off the predicate that marks it.

        ``/api/auth/me`` is open to the guard, so the key lane never judges it, and then
        the handler answers 401 because the cookie resolves to nobody. Marking the whole
        open set "either credential" published this one as key-reachable: a document
        written to stop the reference offering a key on routes that refuse it, offering a
        key on a route that refuses it.
        """
        key = client.post("/api/settings/general/api-key").json()["key"]
        bare = _bare(client)
        assert bare.get("/api/auth/me", headers={"X-Api-Key": key}).status_code == 401

        marked = {
            (m, p)
            for m, p, op in self._operations(client)
            if op.get("security") == [{"Session": []}]
        }
        assert ("GET", "/api/auth/me") in marked

    def test_a_route_that_needs_no_credential_says_so(self, client: TestClient) -> None:
        """The third answer, also driven. The health probe and the sign-in endpoints have
        to answer before anyone is signed in, and inheriting the document default
        published a credential requirement on all seven: the page a script author needs
        first read as one they could not call without already being past it.
        """
        bare = _bare(client)
        assert bare.get("/api/health").status_code == 200
        assert bare.get("/api/auth/context").status_code == 200

        anonymous = {(m, p) for m, p, op in self._operations(client) if op.get("security") == []}
        for open_route in (
            ("GET", "/api/health"),
            ("GET", "/api/auth/context"),
            ("POST", "/api/auth/local"),
            ("POST", "/api/auth/recover"),
        ):
            assert open_route in anonymous, f"asks for no credential, but claims one: {open_route}"
        # The exception, from the other side: it is open, and it is NOT credential-free.
        assert ("GET", "/api/auth/me") not in anonymous

    def test_no_operation_is_left_unclassified(self, client: TestClient) -> None:
        """The annotation pass has to reach every operation, and each has to get exactly
        one of the three answers. This agrees the served schema with ``api_key_refused``
        and ``no_credential_needed``, so it catches an operation the walk skipped (a
        method spelling it does not know, a cached schema built before the pass, a stock
        ``FastAPI.openapi`` winning the race) -- NOT a wrong answer from the fence itself,
        which is what ``test_the_allowlist_matches_by_method_and_shape`` pins from a
        table. Rule 118: it is named for what it discriminates.
        """
        operations = self._operations(client)
        wrong = [
            f"{m} {p}"
            for m, p, op in operations
            if (op.get("security") == [{"Session": []}]) != api_key_refused(m, p)
            or (op.get("security") == []) != no_credential_needed(p)
        ]
        assert wrong == [], f"marked against what the guard does: {wrong}"
        # And all three answers are actually in use, or the agreement above is vacuous.
        assert {len(op.get("security", [{}, {}])) for _, _, op in operations} == {0, 1, 2}

    def test_a_fenced_operation_says_so_in_words(self, client: TestClient) -> None:
        """The security requirement is the machine-readable half. This is the half a
        person reads, and it leads the description rather than trailing it."""
        silent = [
            f"{m} {p}"
            for m, p, op in self._operations(client)
            if api_key_refused(m, p)
            and not op.get("description", "").startswith("**Signed in only.**")
        ]
        assert silent == [], f"fenced, but the operation never says it: {silent}"

    def test_the_note_does_not_displace_what_the_route_does(self, client: TestClient) -> None:
        """Prepended, never substituted: a route's own description is the reason someone
        is reading the entry at all."""
        _, _, override = next(
            (m, p, op)
            for m, p, op in self._operations(client)
            if (m, p) == ("POST", "/api/override")
        )
        assert "Override an item's verdict by hand" in override["description"]
        assert "—" not in override["description"]  # rule 21


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


class TestTheRequestTraceSpendsNoHistoryOnItself:
    """The Logs tab polls `/api/logs` every 2s while it is open.

    That read goes into the same bounded ring it is reading, so tracing it spends the
    operator's visible history on the act of watching it. Every other route stays traced,
    including the two `/api/logs/*` operator actions, so the skip cannot widen by accident.
    """

    def test_reading_the_log_writes_no_line_into_it(self, client: TestClient) -> None:
        try:
            client.put("/api/logs/level", json={"level": "debug"})
            before = logbuffer.RING.last_seq()

            client.get("/api/logs")

            traces = [
                line
                for line in logbuffer.RING.since(before, limit=logbuffer.RING_SIZE)
                if "http.request" in line.text
            ]
            assert traces == []
        finally:
            client.put("/api/logs/level", json={"level": "INFO"})

    def test_every_other_route_is_still_traced(self, client: TestClient) -> None:
        """The skip is one path and one method, so a route that merely sorts nearby in the
        tree keeps its trace. Driven per route rather than asserted as a flag over whatever
        the walk collected (rule 145)."""
        try:
            client.put("/api/logs/level", json={"level": "debug"})
            for path in ("/api/health", "/api/logs/download"):
                before = logbuffer.RING.last_seq()
                client.get(path)
                traces = [
                    line
                    for line in logbuffer.RING.since(before, limit=logbuffer.RING_SIZE)
                    if "http.request" in line.text and path in line.text
                ]
                assert traces, f"{path} must still be traced"
        finally:
            client.put("/api/logs/level", json={"level": "INFO"})


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


class TestTheEnvironmentsLogLevel:
    """``REAPER_LOG_LEVEL`` sets the level until the operator picks one in the UI, and it
    may carry ERROR, which the picker deliberately does not offer (#700)."""

    def test_error_from_the_environment_is_the_level_reaper_runs_at(
        self, _restore_logging: None
    ) -> None:
        """Booting at ERROR left the app at INFO, with nothing said about it.

        ``LEVELS`` omitted ERROR, so ``normalize_level`` returned None for a value
        ``Settings.log_level`` had already accepted, and ``set_level``'s fallback -- there
        to survive a corrupt stored setting -- overrode an operator's choice instead of
        catching a bad one. Someone quieting a noisy log kept every INFO line.
        """
        configure_logging(level="ERROR")

        assert logbuffer.level_name() == "ERROR"
        assert logging.getLogger().getEffectiveLevel() == logging.ERROR

    def test_the_picker_still_refuses_error(self, client: TestClient) -> None:
        """Widening what the app runs at must not widen what the UI sells. Hiding warnings
        from a tool that deletes files is still not a choice on offer, so the route narrows
        to ``UI_LEVELS`` rather than to everything ``normalize_level`` takes."""
        assert logbuffer.UI_LEVELS == ("DEBUG", "INFO", "WARNING"), (
            "UI_LEVELS is what the picker offers, so LogsPanel.tsx's option list mirrors it. "
            "Change one and the other is wrong: an option the route refuses 422s on save, and "
            "a level with no option leaves the operator no way back to it (rule 144)."
        )

        assert client.put("/api/logs/level", json={"level": "ERROR"}).status_code == 422

    def test_the_env_and_the_logger_offer_the_same_levels(self) -> None:
        """The two declarations behind #700, pinned against each other.

        Prose asking a future author to keep them in step does nothing; this fails instead.
        A level in one and not the other is accepted at boot and then silently resolved to
        something the operator did not ask for.
        """
        blessed = get_args(Settings.model_fields["log_level"].annotation)

        assert set(blessed) == set(logbuffer.LEVELS), (
            "config.Settings.log_level validates REAPER_LOG_LEVEL; logbuffer.LEVELS is what "
            "set_level will actually run at. They name one set. Changing it also changes "
            "frontend/src/components/LogsPanel.tsx's picker and the Log level field in "
            "contrib/unraid/my-Reaper.xml (rule 144)."
        )


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
