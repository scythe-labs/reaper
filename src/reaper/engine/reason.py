# SPDX-License-Identifier: AGPL-3.0-or-later
"""A typed reason: what the engine says instead of an English sentence.

Every ``GateResult.detail``, ``SignalResult.detail`` and keep detail used to be a composed
English sentence, stored verbatim and in three places parsed back apart (docs/history/I18N_PLAN.md
§5). A :class:`Reason` carries the sentence's identity and its numbers instead; the catalog
(``frontend/src/locales/en/ui.json``, the ``why`` namespace) holds the words, and the
frontend composes them. Rule 92's constraint, applied to the whole detail vocabulary: the
wording can now be reworded -- or translated -- without a producer and a consumer drifting
apart, because nothing reads the words.

A param value may itself be a :class:`Reason` (the cause slot of a blocked check, the
because-clause of a season conflict) or a tuple of them (the rating gate's per-bar clauses).
The composer recurses; a message template only ever sees flat strings and numbers.

``Reason("legacy", {"text": ...})`` wraps a sentence from a snapshot frozen before the
conversion. The composer renders its ``text`` verbatim, which is exactly how those rows read
before -- pre-conversion rows must still render (§5), they just stop being translated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

type ReasonParam = str | int | float | bool | Reason | tuple["Reason", ...]


@dataclass(frozen=True)
class Reason:
    """One catalog sentence by id, with the values that fill its slots.

    ``id`` is a stable identifier (``dormancy_under_floor``), never prose: the catalog key
    is ``why.<slot>.<id>`` and translators own the sentence. Params carry raw values --
    day counts as numbers, never humanized spans -- so the composing side formats them for
    its own locale.
    """

    id: str
    params: dict[str, ReasonParam] = field(default_factory=dict)


def legacy(text: str) -> Reason:
    """A pre-conversion sentence, carried verbatim. Rendered raw, never translated."""
    return Reason("legacy", {"text": text})


def to_wire(reason: Reason) -> dict[str, Any]:
    """The stored / wire shape: ``{"k": id}`` plus ``"p"`` only when there are params.

    One encoding for the frozen explanation document, ``facts_json``'s extra results, and
    the API -- written once here so the three cannot drift (rule 104).
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
#: three levels (a conflict's cause inside its because-slot inside the row), so anything
#: deeper is a corrupted or hand-built blob, and recursing into it unbounded raises
#: ``RecursionError`` -- which is not in any reader's carefully-chosen except tuple and
#: would raise a row off the queue, the one thing this decoder must never do (rule 96).
_MAX_WIRE_DEPTH = 32


def from_wire(data: Any, *, _depth: int = 0) -> Reason:
    """Rebuild a reason from :func:`to_wire` output, or from a stored legacy sentence.

    A bare string is a detail frozen before the conversion and comes back as
    ``Reason("legacy")``. Anything else illegible -- a malformed dict, or one nested past
    ``_MAX_WIRE_DEPTH`` -- degrades the same way, rendered raw rather than raising a row
    off the queue (rule 96): a reason nobody can read still holds whatever it was holding,
    it just stops being pretty.
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
    ``ActionStep.error``, since #899): ``to_wire`` of the reason, dumped as JSON.
    :func:`from_stored` is the one place that reads it back."""
    return json.dumps(to_wire(reason))


def from_stored(text: str | None) -> Reason | None:
    """The reason a journal column's stored text thaws as. ``None`` for ``None`` or empty
    text -- a step that never failed or skipped, a run that never aborted.

    A row written since #899 holds :func:`to_stored`'s JSON and decodes through
    :func:`from_wire`. A row written before it holds a bare English sentence and comes back
    as ``legacy(text)`` (rule 96), the same fallback a malformed blob gets: neither degrades
    to ``None``, which would raise the row off the queue's why-panel instead of just
    rendering it un-translated."""
    if not text:
        return None
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        return legacy(text)
    if isinstance(decoded, dict) and isinstance(decoded.get("k"), str):
        return from_wire(decoded)
    return legacy(text)
