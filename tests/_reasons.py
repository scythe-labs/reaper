# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers for asserting on typed reasons (docs/history/I18N_PLAN.md §5).

The engine stopped composing English: a detail is a ``Reason`` -- a catalog id plus raw
params -- and the sentence lives in ``frontend/src/locales/en/ui.json`` under ``why.*``.
Tests here assert on ids and params; the composed English is the frontend's to prove
(``frontend/src/why.test.ts`` renders the catalog), and ``test_review_chips.py`` walks the
two-way agreement between the ids the engine can emit and the entries the catalog holds.

``text``'s ``namespace`` argument is this module's twin of ``why.ts``'s ``composeIn``: both
default to ``"why"`` (the only namespace either side has production content for today), and
both take a dotted namespace ("chip.text") the same way. A nested ``Reason`` param always
composes under "why" regardless of the outer namespace -- see the paired comment atop
``why.ts`` for why -- so ``text`` recurses on itself with no ``namespace`` argument, never
the caller's.
"""

from __future__ import annotations

import json
import math
from functools import cache
from pathlib import Path
from typing import Any

from reaper.clock import humanize_days, humanize_window
from reaper.engine.reason import Reason
from reaper.i18n import format_icu as _icu


def flat(reason: Reason) -> str:
    """The reason flattened to one searchable line, nested reasons inlined.

    ``blocked[check=check.watch_history cause=cause.history_reach_short[reach_days=90]]``
    -- for assertions that care which sentence family and figures a result carries without
    restating the whole tree.
    """
    if not reason.params:
        return reason.id
    parts = []
    for name, value in reason.params.items():
        if isinstance(value, Reason):
            parts.append(f"{name}={flat(value)}")
        elif isinstance(value, tuple):
            parts.append(f"{name}=({', '.join(flat(v) for v in value)})")
        else:
            parts.append(f"{name}={value}")
    return f"{reason.id}[{' '.join(parts)}]"


def text(reason: Reason, namespace: str = "why") -> str:
    """The reason composed into its English sentence, from the real catalog.

    The test-side twin of ``frontend/src/why.ts``'s ``composeIn``, over the same
    ``ui.json`` -- so a sentence assertion in this suite pins the catalog entry AND the
    params the engine put in its slots, end to end. Formatting itself is
    ``reaper.i18n.format_icu`` (rule 119: this module stopped re-implementing it once the
    backend needed the same ICU subset for its own catalog -- see ``tests/test_backend_i18n.py``
    for the fixtures that hold this twin and the real ``why.ts`` composer to the same output).
    """
    if reason.id == "legacy":
        return str(reason.params.get("text", ""))
    params: dict[str, Any] = {}
    for name, value in reason.params.items():
        if isinstance(value, Reason):
            # Always "why": a nested reason quotes the shared check/cause/because
            # vocabulary, never the outer namespace's own section (see the module
            # docstring and why.ts's paired comment).
            params[name] = text(value)
        elif isinstance(value, tuple):
            params[name] = "; ".join(text(v) for v in value)
        else:
            params[name] = value
            if isinstance(value, int | float) and not isinstance(value, bool):
                # Rounded half-up before the span builders, the way JS Math.round rounds
                # inside format.ts's humanDays -- Python's round() banker's-rounds, and the
                # twins disagreeing at exact .5-day boundaries would let a test here pin
                # text the real composer never renders.
                half_up = math.floor(float(value) + 0.5)
                params[f"{name}_span"] = humanize_days(half_up)
                params[f"{name}_window"] = humanize_window(half_up)
                params[f"{name}_gb"] = f"{value / 1_000_000_000:.1f}"
                params[f"{name}_tenths"] = f"{value / 10:.1f}"
                params[f"{name}_fixed1"] = f"{value:.1f}"
    field = params.get("field")
    if isinstance(field, str):
        label = _lookup(f"field.{field}")
        params["field_label"] = label if label is not None else field
        params["field_subject"] = label if label is not None else field
        check = _lookup(f"check.{field}")
        params["field_check"] = check if check is not None else field
    source = params.get("source")
    if isinstance(source, str):
        label = _lookup(f"source.{source}")
        params["source_label"] = label if label is not None else source
    message = _lookup(reason.id, namespace)
    if message is not None:
        return _icu(message, params)
    raw = reason.params.get("text")
    if isinstance(raw, str) and raw:
        return raw
    return reason.id.removeprefix("cause.").removeprefix("check.")


def _lookup(dotted: str, namespace: str = "why") -> str | None:
    node: Any = catalog(namespace)
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


@cache
def catalog(namespace: str = "why") -> dict[str, Any]:
    """The English catalog's ``<namespace>`` section, for tests that pin a sentence's copy.

    ``namespace`` is dotted for a nested section ("chip.text"), the same path ``why.ts``'s
    ``composeIn`` looks its namespace argument up under. A namespace with no production
    content yet (``chip``, ``warning``, ahead of their first key) reads back ``{}`` rather
    than raising, so a caller need not guard the phase-2 case.
    """
    path = Path(__file__).resolve().parents[1] / "frontend" / "src" / "locales" / "en" / "ui.json"
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    node: Any = loaded
    for part in namespace.split("."):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    assert isinstance(node, dict)
    return node


def catalog_entry(dotted: str, namespace: str = "why") -> str:
    """One ``<namespace>.*`` entry by its reason id (``"cause.plex_unmatched"``)."""
    node: Any = catalog(namespace)
    for part in dotted.split("."):
        node = node[part]
    assert isinstance(node, str), f"{namespace}.{dotted} is a section, not an entry"
    return node


def refusal_text(code: str, **params: Any) -> str:
    """A coded refusal's English, from the real backend catalog (``reaper.refusal.MESSAGES``).

    The test-side twin of ``api.errors.refuse``: a suite asserting an API error's ``detail``
    renders it from the code and params the response actually carries, rather than
    transcribing the sentence -- the same reason ``text`` above renders a ``Reason`` from the
    ``why`` catalog instead of a hand-copied string. Phase 8a (docs/history/I18N_PLAN.md §5).
    """
    from reaper.refusal import MESSAGES

    return MESSAGES[code].format(**params)
