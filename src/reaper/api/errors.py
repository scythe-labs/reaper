# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn a catalog code into an HTTP refusal, the same way everywhere.

``reaper.refusal`` declares the codes and their English text. The API routes and the
raw-ASGI auth guard both answer through this module. ``refuse`` is what a route calls
directly. ``refuse_from`` re-raises a domain :class:`~reaper.refusal.Refusal` (from the
engine or a service) as the same shape. ``refusal_body`` is the one serializer both the
exception handler (``main.py``) and ``api.middleware``'s raw ASGI response build from,
so every response has the same JSON shape.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from fastapi import HTTPException

from reaper.engine.reason import Reason, to_wire
from reaper.refusal import MESSAGES, Refusal, english


class RefusalHTTPException(HTTPException):
    """An ``HTTPException`` that also carries the code and params behind its ``detail``.

    ``detail`` stays the same plain English sentence it always was, instead of switching
    to a ``{code, params, message}`` envelope, so an API-key client or a documented
    example keeps reading ``detail`` the way it always has. ``code`` and ``params`` ride
    beside it for the exception handler that builds the wire shape (``main.py``) and for
    any test that checks the typed refusal instead of the sentence.

    A param can itself be a nested :class:`~reaper.engine.reason.Reason` (an
    ``IntegrationError`` or ``PlexError``'s own code, carried in through
    ``as_reason()``), or a tuple of them, the same way a stored explanation's params can.
    ``detail`` renders it through :func:`~reaper.refusal.english`, which recurses into
    the nested reason instead of printing the dataclass with ``str.format``. ``params``
    on the wire go through :func:`~reaper.engine.reason.to_wire` too, since a raw
    ``Reason`` is not JSON serializable and the frontend's ``composeIn`` already reads
    this exact shape.
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
    """Raise the coded refusal every API route answers with.

    This is the one place that turns a condition into an HTTP response. A route names
    the code here instead of writing the English message itself.
    """
    raise RefusalHTTPException(status, code, params, headers=headers)


def refuse_from(exc: Refusal) -> NoReturn:
    """Re-raise a caught domain :class:`Refusal`, raised by the engine or a service, as
    the HTTP layer's coded exception. It carries the same code, params, and status
    forward."""
    raise RefusalHTTPException(exc.status, exc.code, exc.params) from exc


def refusal_body(status: int, code: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the wire shape every refusal answers with.

    The body holds ``detail`` (the formatted English) beside ``code`` and ``params``.
    Both the exception handler and the raw-ASGI auth guard build their response from
    this one serializer, so the shape cannot drift between the two.

    ``detail`` renders through :func:`~reaper.refusal.english`, so a nested ``Reason``
    param (an ``IntegrationError`` or ``PlexError``'s own code) composes into its
    sentence instead of printing a dataclass. ``params`` on the wire carries
    ``to_wire``'s encoding of that same nested value, since ``json.dumps``, which both
    callers use, cannot serialize a raw ``Reason`` object.
    """
    reason = Reason(code, dict(params or {}))
    detail = english(reason)
    wire_params = to_wire(reason).get("p", {})
    return {"detail": detail, "code": code, "params": wire_params}


def validation_error_items(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Translate pydantic and FastAPI error dicts into the wire shape the SPA renders.

    Strips the ``Value error, `` prefix pydantic adds to a validator's own message.

    Adds ``code`` and ``params`` when the underlying error is one this catalog knows.
    That is either a bare ``PydanticCustomError`` whose ``type`` is itself a catalog
    code (from the wire schema's field validators in ``api/schemas.py``), or a
    :class:`Refusal` raised inside a ``@model_validator``. Pydantic stores that
    original exception on ``ctx["error"]``, so this function reads its ``code`` and
    ``params`` straight off the instance instead of re-deriving them, and encodes them
    through :func:`~reaper.engine.reason.to_wire`, the same way :func:`refusal_body`
    does, since the raw dict can hold a nested ``Reason`` that ``json.dumps`` cannot
    serialize.

    Shared by ``main._validation_reason`` (FastAPI's own ``RequestValidationError``),
    ``api.policy._to_body``, and ``api.runs.update_profile`` (both of which catch a
    domain ``pydantic.ValidationError`` raised while constructing an engine model
    directly). All three produce the same list of dicts, from
    ``ValidationError.errors()`` or ``RequestValidationError.errors()``.
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
            # Uses `to_wire`, the same encoding `refusal_body` applies, because a param
            # can be a nested `Reason` (an `IntegrationError` or `PlexError` carried in
            # via `as_reason()`), and a raw `Reason` dataclass is not JSON serializable.
            # No current `Refusal(...)` call site nests one, but the constructor allows
            # it, so encoding here keeps a future nested param from crashing the
            # response with a 500.
            item["params"] = to_wire(underlying.as_reason()).get("p", {})
        elif error.get("type") in MESSAGES:
            item["code"] = error.get("type")
            item["params"] = {k: v for k, v in ctx.items() if k != "error"}
        items.append(item)
    return items
