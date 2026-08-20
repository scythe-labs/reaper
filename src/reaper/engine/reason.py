# SPDX-License-Identifier: AGPL-3.0-or-later
"""A typed reason: what the engine says instead of an English sentence.

Every ``GateResult.detail``, ``SignalResult.detail`` and keep detail used to be a composed
English sentence, stored verbatim and in three places parsed back apart (docs/I18N_PLAN.md
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


def from_wire(data: Any) -> Reason:
    """Rebuild a reason from :func:`to_wire` output, or from a stored legacy sentence.

    A bare string is a detail frozen before the conversion and comes back as
    ``Reason("legacy")``. Anything else illegible degrades the same way, rendered raw
    rather than raising a row off the queue (rule 96): a reason nobody can read still
    holds whatever it was holding, it just stops being pretty.
    """
    if isinstance(data, str):
        return legacy(data)
    if not isinstance(data, dict) or not isinstance(data.get("k"), str):
        return legacy(str(data))
    raw = data.get("p")
    params: dict[str, ReasonParam] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict):
                params[str(key)] = from_wire(value)
            elif isinstance(value, list):
                params[str(key)] = tuple(from_wire(v) for v in value)
            elif isinstance(value, str | int | float | bool):
                params[str(key)] = value
            else:
                params[str(key)] = str(value)
    return Reason(str(data["k"]), params)
