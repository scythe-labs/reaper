# SPDX-License-Identifier: AGPL-3.0-or-later
"""Application-level tests.

These exercise the app through a real client rather than calling handlers
directly. A route whose return annotation is unresolvable at runtime -- easy to
write under ``from __future__ import annotations`` -- imports and type-checks
cleanly and only fails when a request arrives. Only a real request catches it.
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

from ._auth import login


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]

    # The app's lifespan reads the database (to seed instances and check that a
    # local admin exists), so the schema must exist before it starts.
    sync_engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_destructive_actions_are_off_by_default(client: TestClient) -> None:
    """The master safety switch ships off. If this test ever fails, a default
    changed and Reaper became able to delete media without anyone asking it to."""
    assert Settings.model_fields["destructive_actions_enabled"].default is False
    assert client.get("/api/health").json()["destructive_actions_enabled"] is False


def test_every_route_response_model_resolves(client: TestClient) -> None:
    """Every declared route must be able to build its response model.

    This is the generic form of the bug above: it catches an unresolvable return
    annotation on any future route, not just the one we already fixed.
    """
    from fastapi.routing import APIRoute

    app = client.app
    routes = [r for r in app.routes if isinstance(r, APIRoute)]  # type: ignore[attr-defined]
    assert routes, "expected at least one API route"

    for route in routes:
        if route.response_field is not None:
            # Raises PydanticUserError if the annotation cannot be resolved.
            route.response_field.validate({}, {}, loc=("response",))
