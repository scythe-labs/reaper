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
    params the engine put in its slots, end to end. It implements only the ICU subset the
    ``why`` entries use (plural, select, selectordinal, number); ``why.test.ts`` proves
    the real composer renders the same sentences, which is what keeps the twins honest.
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


def _js_number(value: Any) -> str:
    """A number the way JS templates print it: no trailing ``.0``, no grouping."""
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _grouped(value: Any) -> str:
    """A number the way ICU's ``number`` format and ``#`` print it: grouped."""
    if isinstance(value, float) and value == int(value):
        return f"{int(value):,}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _split_options(body: str) -> dict[str, str]:
    """``one {...} other {...}`` parsed into a selector-to-message map."""
    options: dict[str, str] = {}
    i = 0
    while i < len(body):
        while i < len(body) and body[i].isspace():
            i += 1
        start = i
        while i < len(body) and body[i] != "{":
            i += 1
        selector = body[start:i].strip()
        depth = 0
        j = i
        while j < len(body):
            if body[j] == "{":
                depth += 1
            elif body[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        options[selector] = body[i + 1 : j]
        i = j + 1
    return options


def _ordinal_category(n: int) -> str:
    if n % 100 in (11, 12, 13):
        return "other"
    return {1: "one", 2: "two", 3: "few"}.get(n % 10, "other")


def _icu(message: str, params: dict[str, Any]) -> str:
    """The ICU MessageFormat subset the ``why`` entries use, resolved for English."""
    out: list[str] = []
    i = 0
    while i < len(message):
        ch = message[i]
        if ch != "{":
            out.append(ch)
            i += 1
            continue
        depth = 0
        j = i
        while j < len(message):
            if message[j] == "{":
                depth += 1
            elif message[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        inner = message[i + 1 : j]
        i = j + 1
        head, _, rest = inner.partition(",")
        name = head.strip()
        value = params.get(name, "")
        if not rest:
            out.append(value if isinstance(value, str) else _js_number(value))
            continue
        kind, _, body = rest.partition(",")
        kind = kind.strip()
        if kind == "number":
            out.append(_grouped(value))
            continue
        options = _split_options(body)
        n = value if isinstance(value, int | float) else 0
        if kind == "plural":
            picked = options.get(f"={_js_number(n)}")
            if picked is None:
                picked = options.get("one" if n == 1 else "other", options.get("other", ""))
            out.append(_icu(picked.replace("#", _grouped(n)), params))
        elif kind == "selectordinal":
            picked = options.get(f"={_js_number(n)}")
            if picked is None:
                picked = options.get(_ordinal_category(int(n)), options.get("other", ""))
            out.append(_icu(picked.replace("#", _grouped(n)), params))
        elif kind == "select":
            key = value if isinstance(value, str) else _js_number(value)
            picked = options.get(key, options.get("other", ""))
            out.append(_icu(picked, params))
        else:
            out.append(str(value))
    return "".join(out)


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
