# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn a catalog code into the HTTP layer's refusal, one way, everywhere.

``reaper.refusal`` declares the codes and their English; this module is what the API
routes and the raw-ASGI auth guard both go through to answer with one. ``refuse`` is what
a route calls directly. ``refuse_from`` re-raises a domain :class:`~reaper.refusal.Refusal`
(the engine's or a service's own exception) as the same shape. ``refusal_body`` is the one
serializer both the exception handler (``main.py``) and ``api.middleware``'s raw ASGI
response build from, so the JSON shape is declared once (rule 104's spirit, for a response
body rather than a stored byte).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from fastapi import HTTPException

from reaper.engine.reason import Reason, to_wire
from reaper.refusal import MESSAGES, Refusal, english


class RefusalHTTPException(HTTPException):
    """An ``HTTPException`` that also carries the code and params behind its ``detail``.

    ``detail`` stays the plain formatted English -- a deliberate deviation from a
    ``{code, params, message}`` envelope: an API-key client or a documented example reads
    ``detail`` exactly as it always has. ``code`` and ``params`` ride beside it, read by the
    exception handler that serializes the top-level wire shape (``main.py``) and by any test
    asserting on the typed refusal rather than a transcribed sentence.

    A param may itself be a nested :class:`~reaper.engine.reason.Reason` (an
    ``IntegrationError``/``PlexError``'s own code, nested via ``as_reason()``) or a tuple of
    them, exactly as a stored explanation's params can. ``detail`` renders it through
    :func:`~reaper.refusal.english`, which recurses, rather than ``str.format`` alone, which
    would print the dataclass's ``repr`` (rule 104: one renderer). ``params`` on the wire
    encode it through :func:`~reaper.engine.reason.to_wire` for the same reason ``params``
    themselves are wire-encoded there -- a raw ``Reason`` is not JSON serializable, and the
    browser's own ``composeIn`` already knows how to read this exact shape back.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        params: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.params: dict[str, Any] = dict(params or {})
        detail = english(Reason(code, self.params))
        super().__init__(status_code=status_code, detail=detail, headers=headers)


def refuse(
    status: int, code: str, /, *, headers: dict[str, str] | None = None, **params: Any
) -> NoReturn:
    """Raise the coded refusal every API route answers with. The one conversion of a
    condition into an HTTP response; a route names the code, never composes English."""
    raise RefusalHTTPException(status, code, params, headers=headers)


def refuse_from(exc: Refusal) -> NoReturn:
    """Re-raise a caught domain :class:`Refusal` (the engine's or a service's own) as the
    HTTP layer's coded exception, carrying the same code, params and status forward."""
    raise RefusalHTTPException(exc.status, exc.code, exc.params) from exc


def refusal_body(status: int, code: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """The wire shape every refusal answers with: ``detail`` (the formatted English)
    beside ``code`` and ``params``. One serializer for the exception handler and the
    raw-ASGI auth guard alike, so the shape cannot drift between the two (rule 104).

    ``detail`` renders through :func:`~reaper.refusal.english`, so a nested ``Reason``
    param (an ``IntegrationError``/``PlexError``'s own code) composes into its sentence
    rather than printing a dataclass. ``params`` on the wire is ``to_wire``'s encoding of
    the same nested value, never the raw ``Reason`` object -- which ``json.dumps`` (this
    body's caller, both of them) cannot serialize at all."""
    reason = Reason(code, dict(params or {}))
    detail = english(reason)
    wire_params = to_wire(reason).get("p", {})
    return {"detail": detail, "code": code, "params": wire_params}


def validation_error_items(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pydantic/FastAPI error dicts, translated into the wire shape the SPA renders.

    Strips the ``Value error, `` prefix pydantic prepends to a validator's own message
    (``policy._to_body`` and ``main._validation_reason`` both did this by hand before; one
    derivation now). Adds ``code``/``params`` wherever the underlying error is one this
    catalog knows: a bare ``PydanticCustomError`` whose ``type`` IS a catalog code (the wire
    schema's own field validators, ``api/schemas.py``), or a wrapped :class:`Refusal` raised
    inside a ``@model_validator`` -- pydantic parks the original exception on
    ``ctx["error"]`` for a plain ``ValueError``/subclass it catches, so that instance's own
    ``code``/``params`` are read straight off it rather than re-derived, through
    :func:`~reaper.engine.reason.to_wire` the same way :func:`refusal_body` encodes its own
    ``params`` -- never the raw dict, which can hold a nested ``Reason``.

    Shared by ``main._validation_reason`` (FastAPI's own ``RequestValidationError``),
    ``api.policy._to_body`` and ``api.runs.update_profile`` (both catching a domain
    ``pydantic.ValidationError`` raised from constructing an engine model directly) -- the
    same list-of-dicts shape either way, from ``ValidationError.errors()`` /
    ``RequestValidationError.errors()``.
    """
    items: list[dict[str, Any]] = []
    for error in errors:
        item: dict[str, Any] = {
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "")).removeprefix("Value error, "),
            "type": error.get("type", ""),
        }
        ctx = error.get("ctx") or {}
        underlying = ctx.get("error")
        if isinstance(underlying, Refusal):
            item["code"] = underlying.code
            # Through `to_wire`, the same encoding `refusal_body` applies -- a param can be
            # a nested `Reason` (an `IntegrationError`/`PlexError` carried in via
            # `as_reason()`), and a raw `Reason` dataclass is not JSON serializable. No
            # current `Refusal(...)` call site nests one, but the constructor's signature
            # allows it, and the raw dict used to reach this handler's `JSONResponse`
            # unencoded, which would 500 instead of answering with the refusal.
            item["params"] = to_wire(underlying.as_reason()).get("p", {})
        elif error.get("type") in MESSAGES:
            item["code"] = error.get("type")
            item["params"] = {k: v for k, v in ctx.items() if k != "error"}
        items.append(item)
    return items
