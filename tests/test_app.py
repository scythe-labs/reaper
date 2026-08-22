# SPDX-License-Identifier: AGPL-3.0-or-later
"""Application-level tests.

These exercise the app through a real client rather than calling handlers
directly. A route whose return annotation is unresolvable at runtime -- easy to
write under ``from __future__ import annotations`` -- imports and type-checks
cleanly and only fails when a request arrives. Only a real request catches it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from reaper.config import Settings
from reaper.refusal import MESSAGES


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_destructive_actions_are_off_by_default(client: TestClient) -> None:
    """The master safety switch ships off. If this test ever fails, a default
    changed and Reaper became able to delete media without anyone asking it to."""
    assert Settings.model_fields["destructive_actions_enabled"].default is False
    # The open probe deliberately says NOTHING about the armed state: an anonymous
    # caller must not learn whether deletion is on. The authenticated settings
    # surface is the readout (see test_settings_api's safety tests).
    assert "destructive_actions_enabled" not in client.get("/api/health").json()


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


def test_a_coded_refusal_carries_its_code_and_params_beside_the_english(
    client: TestClient,
) -> None:
    """``main._refusal_reason`` (phase 8a): every ``api.errors.refuse`` refusal answers with
    ``detail`` (the plain English an API client already reads), plus ``code`` and ``params``
    at the top level for a typed reader. A fresh install has no API key yet, which is the
    plainest ``refuse(404, "error.settings.no_api_key")`` in the tree.
    """
    response = client.get("/api/settings/general/api-key")
    assert response.status_code == 404
    assert response.json() == {
        "detail": MESSAGES["error.settings.no_api_key"],
        "code": "error.settings.no_api_key",
        "params": {},
    }


def test_a_schema_refusal_types_the_field_it_understands_and_leaves_the_rest_plain(
    client: TestClient,
) -> None:
    """``main._validation_reason`` (phase 8a): a wire-schema body carrying both a coded
    refusal (``GateSettingIn``'s ``_must_be_authorable``, a ``PydanticCustomError`` whose
    ``type`` is the catalog code) and an ordinary type mismatch pydantic catches on its own
    answers with one ``detail`` list holding both shapes side by side -- the coded item
    carrying ``code``/``params``, the plain one carrying neither.
    """
    response = client.post(
        "/api/policy",
        json={
            # A retired gate id: PolicyBody.RETIRED_GATES lists it, so GateSettingIn's
            # own validator refuses it as unauthorable -- the coded item.
            "gates": [{"gate": "season_progression", "enabled": True}],
            # Every other required field is simply missing, which is FastAPI's own
            # "missing" type error -- plain, uncoded, on several items in the same list.
        },
    )
    assert response.status_code == 422
    items = response.json()["detail"]
    assert items, "expected at least one validation item"

    coded = [item for item in items if item.get("code") == "error.policy.retired_gate"]
    assert len(coded) == 1
    assert coded[0]["params"] == {}
    assert coded[0]["msg"] == MESSAGES["error.policy.retired_gate"]
    assert coded[0]["loc"][-2:] == ["0", "gate"]
    assert "Value error" not in coded[0]["msg"]

    plain = [item for item in items if item["type"] == "missing"]
    assert plain, "expected at least one ordinary missing-field error"
    assert all("code" not in item for item in plain)
