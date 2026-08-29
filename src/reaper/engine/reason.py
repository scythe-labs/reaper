# SPDX-License-Identifier: AGPL-3.0-or-later
"""A typed reason: what the engine says instead of an English sentence.

``GateResult.detail``, ``SignalResult.detail``, and keep details all carry a
:class:`Reason` instead of a finished sentence. A ``Reason`` holds an id and the values
that fill its slots; the catalog (``frontend/src/locales/en/ui.json``, the ``why``
namespace) holds the actual words, and the frontend composes them. This keeps the
wording free to change, or to be translated, without a producer and a reader drifting
out of sync, because no code reads the words themselves.

A value inside a ``Reason`` can itself be another ``Reason`` (the cause of a blocked
check, the reason behind a season conflict) or a tuple of them (the rating gate's
per-bar results). The composer on the frontend recurses through these; a message
template only ever sees plain strings and numbers.

``Reason("legacy", {"text": ...})`` wraps a sentence saved before typed reasons existed.
The composer renders its ``text`` as-is, without translation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

type ReasonParam = str | int | float | bool | Reason | tuple["Reason", ...]


@dataclass(frozen=True)
class Reason:
    """One catalog sentence, by id, with the values that fill its slots.

    ``id`` is a stable identifier (``dormancy_under_floor``), never prose. The catalog key
    is ``why.<slot>.<id>``, and translators own the sentence. ``params`` carry raw values,
    such as a day count as a plain number rather than a formatted span, so the composing
    side can format them for its own locale.
    """

    id: str
    params: dict[str, ReasonParam] = field(default_factory=dict)


def legacy(text: str) -> Reason:
    """A sentence stored before typed reasons existed, carried as-is. Never translated."""
    return Reason("legacy", {"text": text})


def to_wire(reason: Reason) -> dict[str, Any]:
    """The stored and wire shape: ``{"k": id}``, plus ``"p"`` only when there are params.

    The frozen explanation document, ``facts_json``'s extra results, and the API all use
    this one encoding, written once here so the three cannot drift apart.
    """
    if not reason.params:
        return {"k": reason.id}
    encoded: dict[str, Any] = {}
    for key, value in reason.params.items():
        if isinstance(value, Reason):
            encoded[key] = to_wire(value)
        elif isinstance(value, tuple):
            encoded[key] = [to_wire(v) for v in value]
        else:
            encoded[key] = value
    return {"k": reason.id, "p": encoded}


#: The deepest stored nesting :func:`from_wire` will decode. The engine writes at most
#: three levels deep (a conflict's cause inside its because-slot inside the row), so
#: anything deeper is a corrupted or hand-built value. Decoding it without this limit
#: would raise ``RecursionError``, which would drop the row from the review queue instead
#: of just showing it without translation.
_MAX_WIRE_DEPTH = 32


def from_wire(data: Any, *, _depth: int = 0) -> Reason:
    """Rebuild a reason from :func:`to_wire` output, or from a stored legacy sentence.

    A bare string was stored before typed reasons existed, and comes back as
    ``Reason("legacy")``. Anything else unreadable, such as a malformed dict or one nested
    past ``_MAX_WIRE_DEPTH``, degrades the same way: it renders raw instead of translated,
    rather than dropping the row from the review queue.
    """
    if isinstance(data, str):
        return legacy(data)
    if _depth > _MAX_WIRE_DEPTH or not isinstance(data, dict) or not isinstance(data.get("k"), str):
        return legacy(str(data))
    raw = data.get("p")
    params: dict[str, ReasonParam] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict):
                params[str(key)] = from_wire(value, _depth=_depth + 1)
            elif isinstance(value, list):
                params[str(key)] = tuple(from_wire(v, _depth=_depth + 1) for v in value)
            elif isinstance(value, str | int | float | bool):
                params[str(key)] = value
            else:
                params[str(key)] = str(value)
    return Reason(str(data["k"]), params)


def to_stored(reason: Reason) -> str:
    """The text a journal column stores for a reason (``ReapRun.aborted_reason``,
    ``ActionStep.error``): ``to_wire`` of the reason, dumped as JSON.
    :func:`from_stored` is the one place that reads it back."""
    return json.dumps(to_wire(reason))


def from_stored(text: str | None) -> Reason | None:
    """The reason a journal column's stored text decodes to. ``None`` for ``None`` or empty
    text: a step that never failed or skipped, a run that never aborted.

    A row written by the typed system holds :func:`to_stored`'s JSON and decodes through
    :func:`from_wire`. An older row holds a bare English sentence and comes back as
    ``legacy(text)``, the same fallback a malformed value gets. Neither case falls back to
    ``None``, which would drop the row from the queue's why-panel instead of just showing it
    without translation."""
    if not text:
        return None
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        return legacy(text)
    if isinstance(decoded, dict) and isinstance(decoded.get("k"), str):
        return from_wire(decoded)
    return legacy(text)
