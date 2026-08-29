# SPDX-License-Identifier: AGPL-3.0-or-later
"""``reaper.api.errors``. The wire shape a caught refusal renders as, at the unit level.

``test_api.py`` only proves this end to end, through a live route. This file pins
``validation_error_items``'s own contract. A caught :class:`~reaper.refusal.Refusal`
reaches the wire through the same encoding ``refusal_body`` applies, never as the raw
``Refusal.params`` dict.
"""

from __future__ import annotations

import json

from reaper.api.errors import validation_error_items
from reaper.engine.reason import Reason, from_wire
from reaper.refusal import Refusal


def test_a_nested_reason_param_reaches_the_wire_encoded() -> None:
    """No current ``Refusal(...)`` call site nests a ``Reason`` inside its params (every
    site passes only str/int/float), but the constructor's signature is ``ReasonParam``,
    which allows one. ``api.leaving_soon``/``api.plex`` already nest an
    ``IntegrationError``'s reason the same way through ``refuse(..., error=exc.as_reason())``.

    ``validation_error_items`` used to attach a caught ``Refusal``'s params straight onto
    the wire item, unencoded. A raw ``Reason`` dataclass instance is not JSON serializable,
    so FastAPI's ``RequestValidationError`` handler (``main.py``) would 500 instead of
    answering with the refusal, the moment a validator nested one. This reproduces that
    shape directly, the same ``ctx["error"]`` pydantic parks a caught exception on.
    """
    cause = Reason("error.plex.unreachable", {"error": "timed out"})
    refusal = Refusal("error.policy.unknown_field", field="genre", cause=cause)
    pydantic_error = {
        "loc": ("body", "policy", "gates", 0, "field"),
        "msg": 'Value error, There is no field named "genre".',
        "type": "value_error",
        "ctx": {"error": refusal},
    }

    items = validation_error_items([pydantic_error])

    assert len(items) == 1
    item = items[0]
    assert item["code"] == "error.policy.unknown_field"
    # This pins the actual failure mode. The old code put a raw `Reason` instance at
    # params["cause"], which `json.dumps` cannot serialize. That is exactly what
    # `main.py`'s `JSONResponse` does to this list before it reaches the operator.
    json.dumps(items)
    assert item["params"] == {
        "field": "genre",
        "cause": {"k": "error.plex.unreachable", "p": {"error": "timed out"}},
    }
    # This is the wire shape `from_wire` (the backend's mirror of the frontend's
    # `composeIn` decoder) expects. Round-tripping it recovers the exact reason
    # `as_reason()` built.
    assert from_wire({"k": item["code"], "p": item["params"]}) == refusal.as_reason()
