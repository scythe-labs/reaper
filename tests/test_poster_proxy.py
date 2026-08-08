# SPDX-License-Identifier: AGPL-3.0-or-later
"""The artwork proxy's one shared Tautulli client.

A cold review queue asks for a few hundred posters at once. Each request used to build a
whole client -- new connection pool, new TLS handshake -- and tear it down again, so the
page paid a full connection setup per image. One client is kept on the app instead.

Two properties matter and neither is about speed: the client must be RETIRED when the
instance it talks to changes (otherwise a rotated key or an edited URL keeps serving
through the old connection), and it must be CLOSED at shutdown rather than leaked
(rule 34: every constructed client has an owner that closes it).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.models import Instance, InstanceKind
from reaper.main import create_app

from ._auth import login

PNG = (b"\x89PNG\r\n\x1a\n", "image/png")


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """One Tautulli configured, and its artwork reads stubbed to return bytes."""
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    assert settings.secret_key is not None
    box = SecretBox(settings.secret_key.get_secret_value())
    with Session(engine) as session:
        session.add(
            Instance(
                kind=InstanceKind.TAUTULLI,
                name="T",
                base_url="https://t.example.net",
                api_key_enc=box.encrypt("key-1"),
                enabled=True,
                created_at=utcnow(),
            )
        )
        session.commit()
    engine.dispose()

    from reaper.clients.tautulli import TautulliClient

    async def fake_poster(self: TautulliClient, rating_key: int) -> tuple[bytes, str]:
        return PNG

    monkeypatch.setattr(TautulliClient, "poster", fake_poster)
    monkeypatch.setattr(TautulliClient, "art", fake_poster)

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


def _is_closed(client: object) -> bool:
    """Whether a built client's underlying transport was shut down."""
    return bool(client._client.is_closed)  # type: ignore[attr-defined]


def _built(client: TestClient) -> object | None:
    cached = getattr(client.app.state, "artwork_client", None)
    return None if cached is None else cached[1]


class TestTheArtworkClientIsShared:
    def test_many_requests_share_one_client(self, client: TestClient) -> None:
        assert _built(client) is None  # built lazily, so an install with no queue pays nothing

        assert client.get("/api/poster/1").status_code == 200
        first = _built(client)
        assert first is not None

        for key in range(2, 20):
            assert client.get(f"/api/poster/{key}").status_code == 200
        assert _built(client) is first

    def test_the_backdrop_shares_it_too(self, client: TestClient) -> None:
        assert client.get("/api/poster/1").status_code == 200
        first = _built(client)
        assert client.get("/api/poster/1", params={"kind": "art"}).status_code == 200
        assert _built(client) is first


class TestItIsRetiredWhenTheInstanceChanges:
    def test_a_rotated_key_builds_a_new_client_and_closes_the_old(
        self, client: TestClient, settings: Settings
    ) -> None:
        """A cached client holds the OLD credential in its headers. If it outlived a key
        rotation it would keep authenticating with a key the operator replaced."""
        assert client.get("/api/poster/1").status_code == 200
        old = _built(client)
        assert old is not None

        instances = client.get("/api/settings/instances").json()
        instance_id = instances[0]["id"]
        updated = client.put(
            f"/api/settings/instances/{instance_id}",
            json={"api_key": "key-2"},
        )
        assert updated.status_code == 200, updated.text

        assert client.get("/api/poster/1").status_code == 200
        new = _built(client)
        assert new is not None and new is not old
        assert _is_closed(old)

    def test_an_edited_url_retires_it_too(self, client: TestClient) -> None:
        assert client.get("/api/poster/1").status_code == 200
        old = _built(client)

        instance_id = client.get("/api/settings/instances").json()[0]["id"]
        updated = client.put(
            f"/api/settings/instances/{instance_id}",
            json={"base_url": "https://t2.example.net"},
        )
        assert updated.status_code == 200, updated.text

        assert client.get("/api/poster/1").status_code == 200
        assert _built(client) is not old


class TestItHasAnOwner:
    def test_shutdown_closes_it(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        """Kept across requests means kept until something closes it. The lifespan does."""
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        assert settings.secret_key is not None
        box = SecretBox(settings.secret_key.get_secret_value())
        with Session(engine) as session:
            session.add(
                Instance(
                    kind=InstanceKind.TAUTULLI,
                    name="T",
                    base_url="https://t.example.net",
                    api_key_enc=box.encrypt("key-1"),
                    enabled=True,
                    created_at=utcnow(),
                )
            )
            session.commit()
        engine.dispose()

        from reaper.clients.tautulli import TautulliClient

        async def fake_poster(self: TautulliClient, rating_key: int) -> tuple[bytes, str]:
            return PNG

        monkeypatch.setattr(TautulliClient, "poster", fake_poster)

        app = create_app(settings)
        with TestClient(app) as c:
            login(c, settings)
            assert c.get("/api/poster/1").status_code == 200
            built = app.state.artwork_client[1]
            assert not _is_closed(built)

        # The lifespan has exited by here.
        assert _is_closed(built)
        assert getattr(app.state, "artwork_client", None) is None
