# SPDX-License-Identifier: AGPL-3.0-or-later
"""The backend's own translation catalog, for the two surfaces with no browser to compose
a sentence in: the Discord webhook (`notify/discord.py`) and the desktop launcher's tray
and dialogs (`launcher.py`). Every other operator-facing sentence is composed in the SPA
from `frontend/src/locales/en/ui.json` -- this module is the same idea, shipped the way
Seerr ships its own backend catalog, for the two places that idea cannot reach.

`say(key, tag, **params)` is the one entry point: look `key` up in `tag`'s catalog
(`reaper.locales.<tag>.backend.json`), format it as ICU MessageFormat against `params`, and
never raise -- a webhook post or a tray label has no UI to show an error in, so a bad key or
a malformed message logs a warning and falls back to plain text instead.

`format_icu` is the formatter underneath `say`, exported so `tests/_reasons.py`'s test-side
composer for `frontend/src/locales/en/ui.json`'s `why.*` entries calls the same production
code rather than a second, hand-copied implementation (rule 119). It supports:

  * a bare `{name}` interpolation, and `{name, number}` (grouped: `1,234`)
  * `{name, plural, one {...} other {...}}` and `{name, selectordinal, ...}`, both with `#`
    substituted for the (grouped) number inside the picked branch -- at that branch's own
    brace depth only, so a plural nested inside another plural's branch keeps its own `#`
  * `{name, select, key {...} other {...}}`
  * a single-quoted literal: `''` for one literal quote, and `'...'` for a run of text
    with no ICU meaning of its own -- but only when the opening `'` sits right before
    `{`, `}`, `#` or another `'`, so a message can show a literal brace as `'{'` while
    every plain contraction in this codebase's copy ("didn't", "can't") still renders
    untouched: an apostrophe followed by an ordinary letter is never an escape

**Not supported, and out of scope for this subset**: `offset:`, the `date`/`time`/`duration`
argument types, and a `selectordinal` category outside English's four (`one`/`two`/`few`/
`other`) -- an untranslated ordinal falls to `other`, which is the general fallback every
unrecognized category takes. None of `say`'s catalogs use any of these today;
`tests/test_backend_i18n.py` names this boundary and proves the formatter degrades rather
than raising on syntax past it.

Plural categories are CLDR's cardinal rule, reduced to the integer case this codebase's
catalogs use (a whole count of titles, days, or seasons -- never a decimal). `PLURAL_RULES`
is the table: `en` is what the catalog ships today, and the tags after it are one line each,
ready for the day a Discord or launcher translation ships in that language.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from functools import cache
from importlib import resources
from typing import Any

import structlog

log = structlog.get_logger(__name__)

DEFAULT_TAG = "en"

#: CLDR cardinal plural rules, reduced to the integer case: every param this catalog
#: pluralizes on is a whole count, never a fraction, so the decimal-category clauses CLDR
#: also defines (Welsh's five-way split, Arabic's six) are not needed and not implemented.
#: A tag with no rule here falls back to `en`'s (`_plural_category`), so `say` never raises
#: for an untabled language -- `test_backend_i18n.py` is what makes ADDING one mandatory
#: once a UI translation for that language ships.
PLURAL_RULES: dict[str, Callable[[float], str]] = {
    "en": lambda n: "one" if n == 1 else "other",
    "de": lambda n: "one" if n == 1 else "other",
    "es": lambda n: "one" if n == 1 else "other",
    "fr": lambda n: "one" if n in (0, 1) else "other",
    "pt": lambda n: "one" if n in (0, 1) else "other",
}


def _plural_category(n: float, tag: str) -> str:
    rule = PLURAL_RULES.get(tag, PLURAL_RULES[DEFAULT_TAG])
    return rule(n)


def _ordinal_category(n: int) -> str:
    """English's four selectordinal categories. See the module docstring: this formatter
    implements no other language's ordinal rule, because nothing shipped needs one yet."""
    if n % 100 in (11, 12, 13):
        return "other"
    return {1: "one", 2: "two", 3: "few"}.get(n % 10, "other")


def _js_number(value: Any) -> str:
    """A number with no trailing `.0` and no grouping, matching a plain f-string/template
    interpolation of an int or a whole float."""
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _grouped(value: Any) -> str:
    """A number the way `{name, number}` and `#` render it: grouped (`1,234`)."""
    if isinstance(value, float) and value == int(value):
        return f"{int(value):,}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _replace_hash(text: str, replacement: str) -> str:
    """`#` substituted only at brace depth 0, so a plural nested inside another plural's
    branch keeps its OWN `#` for its own recursive pass (see `format_icu`'s docstring)."""
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
            out.append(ch)
        elif ch == "}":
            depth -= 1
            out.append(ch)
        elif ch == "#" and depth == 0:
            out.append(replacement)
        else:
            out.append(ch)
    return "".join(out)


def _split_options(body: str) -> dict[str, str]:
    """`one {...} other {...}` parsed into a selector-to-message map."""
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


#: A quote only escapes when it sits right before one of these -- otherwise it is plain
#: text, which is what keeps every contraction in this codebase's copy ("didn't", "can't")
#: from being read as the start of a quoted literal. Matches real ICU MessageFormat: a
#: bare apostrophe has no special meaning, only one adjacent to syntax does.
_ICU_QUOTE_STARTS = ("'", "{", "}", "#")


def format_icu(message: str, params: Mapping[str, Any], tag: str = DEFAULT_TAG) -> str:
    """`message` rendered as the ICU MessageFormat subset this module's docstring names,
    against `params`. `tag` picks the plural rule (`PLURAL_RULES`); `selectordinal` is
    English-only regardless of `tag` (see `_ordinal_category`).

    Known gap, out of scope for this subset: a literal brace escaped with `'{'` inside a
    plural or selectordinal branch can desync `_replace_hash`/`_split_options`'s own brace
    depth counting, since neither is quote-aware. No catalog entry combines the two today.
    """
    out: list[str] = []
    i = 0
    n = len(message)
    while i < n:
        ch = message[i]
        if ch == "'" and i + 1 < n and message[i + 1] in _ICU_QUOTE_STARTS:
            if message[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            end = message.find("'", i + 1)
            if end == -1:
                out.append(message[i + 1 :])
                break
            out.append(message[i + 1 : end])
            i = end + 1
            continue
        if ch != "{":
            out.append(ch)
            i += 1
            continue
        depth = 0
        j = i
        while j < n:
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
        numeric = value if isinstance(value, int | float) else 0
        if kind == "plural":
            picked = options.get(f"={_js_number(numeric)}")
            if picked is None:
                picked = options.get(_plural_category(numeric, tag), options.get("other", ""))
            out.append(format_icu(_replace_hash(picked, _grouped(numeric)), params, tag))
        elif kind == "selectordinal":
            picked = options.get(f"={_js_number(numeric)}")
            if picked is None:
                picked = options.get(_ordinal_category(int(numeric)), options.get("other", ""))
            out.append(format_icu(_replace_hash(picked, _grouped(numeric)), params, tag))
        elif kind == "select":
            key = value if isinstance(value, str) else _js_number(value)
            picked = options.get(key, options.get("other", ""))
            out.append(format_icu(picked, params, tag))
        else:
            out.append(str(value))
    return "".join(out)


def _load(tag: str) -> dict[str, Any] | None:
    """`<tag>/backend.json`, or `None` if it does not exist, is unreadable, or is not a JSON
    object. Loaded through `importlib.resources`, anchored on the `reaper.locales` package
    -- never a path relative to the current working directory -- so this resolves the same
    way from a test run, a wheel install, and the editable install the Docker image uses."""
    try:
        traversable = resources.files("reaper.locales").joinpath(tag, "backend.json")
        if not traversable.is_file():
            return None
        raw = traversable.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("i18n.catalog_unreadable", tag=tag, error=type(exc).__name__)
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("i18n.catalog_malformed", tag=tag)
        return None
    if not isinstance(data, dict):
        log.warning("i18n.catalog_malformed", tag=tag)
        return None
    return data


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """`overlay` layered onto `base`: a nested dict recurses, a non-blank string wins, and a
    blank string or any other shape is skipped, so `base`'s value shows through underneath
    it -- the fallback `catalog` and `say` promise for an untranslated or blank key, the same
    posture as the SPA's `returnEmptyString: false`."""
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        elif isinstance(value, str) and value != "":
            merged[key] = value
    return merged


@cache
def catalog(tag: str = DEFAULT_TAG) -> dict[str, Any]:
    """`tag`'s catalog, English always loaded underneath it as the fallback.

    A shipped `<tag>/backend.json` need only carry the leaves a translator filled in;
    `_merge` overlays its non-blank strings onto the English tree, so a key it omits, or
    left blank, reads back the English sentence rather than a hole. Cached: a shipped
    catalog is read once per tag for the life of the process."""
    english = _load(DEFAULT_TAG) or {}
    if tag == DEFAULT_TAG:
        return english
    translated = _load(tag)
    if translated is None:
        return english
    return _merge(english, translated)


def _lookup(node: Mapping[str, Any], dotted: str) -> str | None:
    current: Any = node
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current if isinstance(current, str) else None


def say(key: str, tag: str | None = None, **params: Any) -> str:
    """`key`'s message in `tag` (English when `tag` is `None`, or when `tag` ships no
    catalog), formatted with `params`.

    Never raises. `notify/discord.py`'s webhook post and `launcher.py`'s tray/dialogs are
    best-effort surfaces with no UI to render a formatting error in, so a key missing from
    every catalog, or a message this formatter cannot render, logs a warning and returns
    the bare key -- readable, and the same posture `why.ts`'s composer takes for an id its
    build has no entry for.
    """
    resolved_tag = tag or DEFAULT_TAG
    message = _lookup(catalog(resolved_tag), key)
    if message is None:
        log.warning("i18n.missing_key", key=key, tag=resolved_tag)
        return key
    try:
        return format_icu(message, params, resolved_tag)
    except Exception:
        # Broad on purpose (the docstring's guarantee): a malformed message must not raise
        # into a webhook post or a tray label, either of which has no UI to show it in.
        log.warning("i18n.format_failed", key=key, tag=resolved_tag, exc_info=True)
        return key
