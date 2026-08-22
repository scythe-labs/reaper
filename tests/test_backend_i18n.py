# SPDX-License-Identifier: AGPL-3.0-or-later
"""`reaper.i18n`'s backend catalog: the Discord webhook and the desktop launcher's tray
and dialogs, the two operator-facing surfaces with no browser to compose a sentence in
(docs/history/I18N_PLAN.md's catalog work, phase 10a).

Four things pinned here, each named in its own class: the production ICU formatter proven
against the exact fixtures `frontend/src/why.test.ts` pins for the real `ui.json` catalog
(rule 119: this is what keeps `tests/_reasons.py`'s twin, which now imports the same
formatter, honest against the frontend's own composer); the two-way agreement between
`say(...)`'s literal call-site keys and `backend.json`'s leaves (rule 145); every catalog
key carrying exactly one translator note (rule 145 again, the same shape
`frontend/src/i18n-notes.test.ts` checks for `ui.json`); and the formatter's documented
subset, both what it supports and what it degrades on rather than raising.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

import reaper.i18n as reaper_i18n
from reaper.i18n import format_icu, say

from ._reasons import catalog_entry

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "reaper"
BACKEND_CATALOG = SRC / "locales" / "en" / "backend.json"
BACKEND_NOTES = SRC / "locales" / "en" / "backend.notes.json"
UI_LOCALES = REPO / "frontend" / "src" / "locales"

#: Every module that calls `say(...)` today. A future `notify.<agent>` module (the plan's
#: own words for what comes after this one) joins this tuple in the same change that adds
#: it, which is what keeps the two-way count below honest.
_SAY_CALL_SITES = (
    SRC / "notify" / "discord.py",
    SRC / "api" / "settings.py",
    SRC / "launcher.py",
)

#: Rule 145: the population this file's two scanning guards claim to cover, reconciled by
#: hand against `backend.json` -- 4 discord.leaving_soon.* keys, 2 discord.test.* keys, 3
#: launcher.tray.* keys, 3 launcher.dialog.* keys, 2 launcher.move.* keys.
_EXPECTED_CATALOG_KEY_COUNT = 14


def _leaves(node: object, prefix: str = "") -> dict[str, str]:
    """A nested catalog dict flattened to dotted-key -> message, the same shape
    `frontend/src/test/catalog.ts`'s `leaves()` gives the frontend's own catalog gates."""
    if isinstance(node, str):
        return {prefix: node}
    assert isinstance(node, dict), f"{prefix or '<root>'} is neither a string nor a section"
    out: dict[str, str] = {}
    for name, child in node.items():
        out.update(_leaves(child, f"{prefix}.{name}" if prefix else name))
    return out


def _backend_catalog_leaves() -> dict[str, str]:
    return _leaves(json.loads(BACKEND_CATALOG.read_text(encoding="utf-8")))


def _say_call_keys(path: Path) -> set[str]:
    """Every literal key passed as `say(...)`'s first argument in `path` (rule 147: this
    matches the call shape by parsing the AST, not by a text pattern that a reformatted
    call could slip past)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "say"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
    return keys


class TestTheProductionIcuFormatterMatchesTheFrontendComposer:
    """`reaper.i18n.format_icu`, over the real `ui.json` catalog, rendering the exact
    strings `frontend/src/why.test.ts` pins for the same message and params -- so the two
    languages' composers are proven to agree, not merely to each read their own catalog
    without crashing."""

    def test_a_plural_count_with_an_already_derived_window(self) -> None:
        # why.test.ts, "pluralizes counts through the catalog's ICU, not the caller".
        # window_days_window is `format.ts`'s humanWindow output, passed through as a
        # plain string -- computing it is that function's own twin's job, not this one's.
        message = catalog_entry("popularity_watched")
        assert (
            format_icu(message, {"count": 1, "window_days_window": "year"})
            == "watched here: 1 person in the last year"
        )
        assert (
            format_icu(message, {"count": 3, "window_days_window": "3 months"})
            == "watched here: 3 people in the last 3 months"
        )

    def test_a_selectordinal_season_rank(self) -> None:
        # why.test.ts, "orders a season's place with the selectordinal".
        message = catalog_entry("signal_season_rank")
        assert format_icu(message, {"rank": 1}) == "the newest season on disk"
        assert format_icu(message, {"rank": 2}) == "the second-newest season on disk"
        assert format_icu(message, {"rank": 8}) == "the 8th-newest season on disk"

    def test_an_error_namespace_plural(self) -> None:
        # why.test.ts, "pluralizes an error.* entry's count param".
        message = catalog_entry("password.too_short", namespace="error")
        assert format_icu(message, {"min_length": 1}) == "Use at least 1 character."
        assert format_icu(message, {"min_length": 8}) == "Use at least 8 characters."


class TestTheFormatterSubset:
    """What `format_icu` supports, and where the subset stops -- see its docstring in
    `reaper.i18n` for the full boundary. Every case here either renders correctly or
    degrades quietly; none of them may raise, since `say` promises exactly that."""

    def test_a_bare_interpolation(self) -> None:
        assert format_icu("{name} is here", {"name": "Reaper"}) == "Reaper is here"

    def test_number_format_groups(self) -> None:
        assert format_icu("{n, number}", {"n": 1234}) == "1,234"

    def test_select(self) -> None:
        message = "{media, select, movie {a movie} other {a show}}"
        assert format_icu(message, {"media": "movie"}) == "a movie"
        assert format_icu(message, {"media": "tv"}) == "a show"

    def test_a_plural_nested_inside_a_plural_keeps_its_own_hash(self) -> None:
        # The bug `_replace_hash`'s own docstring names: a naive substitution before
        # recursing would clobber the inner plural's `#` with the outer count.
        message = "{a, plural, other {# outer {b, plural, other {# inner}}}}"
        assert format_icu(message, {"a": 2, "b": 5}) == "2 outer 5 inner"

    def test_an_escaped_literal_brace_and_a_doubled_quote(self) -> None:
        assert format_icu("a literal '{' brace", {}) == "a literal { brace"
        assert format_icu("two quotes '' make one", {}) == "two quotes ' make one"

    def test_a_plain_apostrophe_in_prose_is_not_an_escape(self) -> None:
        # The regression this formatter's quote handling exists to avoid: real catalog
        # prose is full of contractions, and none of them sit next to {, } or #, so none
        # of them may start a quoted region (`launcher.dialog.port_in_use`'s "didn't" is
        # the catalog entry that surfaced this).
        assert format_icu("Reaper didn't start: port {port}.", {"port": 8420}) == (
            "Reaper didn't start: port 8420."
        )

    def test_an_unterminated_quote_next_to_syntax_reads_to_end_of_string(self) -> None:
        assert format_icu("before '{ never closes", {}) == "before { never closes"

    def test_an_unrecognized_argument_kind_degrades_to_the_raw_value(self) -> None:
        # date/time/duration are out of scope (module docstring): nothing shipped uses one,
        # so this only has to not crash, which it proves by falling back to the raw value.
        assert format_icu("{d, date}", {"d": "2026-01-01"}) == "2026-01-01"

    def test_selectordinal_has_no_non_english_category_table(self) -> None:
        # Also out of scope: `tag` never changes which ordinal category `n` falls into.
        # Still never raises -- it always resolves through English's four categories.
        message = "{n, selectordinal, one {#st} two {#nd} few {#rd} other {#th}}"
        assert format_icu(message, {"n": 21}, tag="es") == "21st"


class TestPluralRulesCoverEveryShippedUiTranslation:
    def test_every_shipped_ui_locale_has_a_backend_plural_rule(self) -> None:
        """A UI translation Weblate ships names a tag with no backend plural rule until
        this table grows to match it -- pluralizing a Discord or launcher message in that
        language would silently take English's one/other split, which is wrong for a
        language whose singular/plural boundary sits somewhere else (rule 145: a shipped
        tag with no rule must fail this, not silently inherit English's)."""
        shipped = {
            path.name
            for path in UI_LOCALES.iterdir()
            if path.is_dir() and path.name not in {"en", "glossary"}
        }
        assert shipped, "no shipped UI translation found to check the rule table against"
        missing = shipped - set(reaper_i18n.PLURAL_RULES)
        assert not missing, (
            f"add a cardinal plural rule to reaper.i18n.PLURAL_RULES for: {sorted(missing)}"
        )

    def test_the_table_itself(self) -> None:
        """Spot-checks, not a restatement of CLDR: the shape each rule has to get right at
        n=0, n=1 and n=2, which is where en/de/es (one at n=1 only) and fr/pt (one at n=0
        and n=1 too) actually differ."""
        assert reaper_i18n.PLURAL_RULES["en"](1) == "one"
        assert reaper_i18n.PLURAL_RULES["en"](0) == "other"
        assert reaper_i18n.PLURAL_RULES["fr"](0) == "one"
        assert reaper_i18n.PLURAL_RULES["fr"](1) == "one"
        assert reaper_i18n.PLURAL_RULES["fr"](2) == "other"
        assert reaper_i18n.PLURAL_RULES["es"](1) == "one"
        assert reaper_i18n.PLURAL_RULES["es"](2) == "other"


class TestSayCallSitesAgreeWithTheCatalogBothWays:
    """Rule 145 (a scanning guard is proven against a reconciled count) and rule 147 (the
    matcher is proven against what it should and should not collect): every literal key a
    `say(...)` call names is a real `backend.json` leaf, and every leaf is called from
    somewhere -- an orphan catalog entry is exactly as much a bug as a typo'd call site,
    just invisible in the opposite direction."""

    def test_the_matcher_collects_a_call_and_ignores_a_non_literal_or_a_different_callee(
        self, tmp_path: Path
    ) -> None:
        sample = tmp_path / "sample.py"
        sample.write_text(
            "from reaper.i18n import say\n"
            "\n"
            "say('a.literal.key')\n"
            "say(some_variable)\n"
            "other_function('not.say')\n"
            "say('another.literal', tag, count=1)\n",
            encoding="utf-8",
        )
        assert _say_call_keys(sample) == {"a.literal.key", "another.literal"}

    def test_every_call_site_key_is_a_real_catalog_leaf_and_every_leaf_is_called(self) -> None:
        used: set[str] = set()
        for path in _SAY_CALL_SITES:
            used |= _say_call_keys(path)
        catalog_keys = set(_backend_catalog_leaves())

        missing_from_catalog = used - catalog_keys
        never_called = catalog_keys - used
        assert not missing_from_catalog, (
            f"say() names a key backend.json does not have: {sorted(missing_from_catalog)}"
        )
        assert not never_called, (
            "backend.json carries a key no say() call site uses: "
            f"{sorted(never_called)}. Wire it, or drop it from the catalog."
        )
        assert len(catalog_keys) == _EXPECTED_CATALOG_KEY_COUNT, (
            f"backend.json now has {len(catalog_keys)} leaves, expected "
            f"{_EXPECTED_CATALOG_KEY_COUNT}. Update the count here (and the reconciliation "
            "comment above it) once you've checked every key by hand."
        )


class TestTranslatorNotes:
    """The same coverage `frontend/src/i18n-notes.test.ts` holds `ui.notes.json` to,
    reduced to this catalog's simpler rule: every key gets a note (`backend.notes.json`
    is small enough that there is no "required subset" to derive)."""

    def test_every_key_has_exactly_one_nonblank_note_and_no_note_is_orphaned(self) -> None:
        catalog_keys = set(_backend_catalog_leaves())
        notes: dict[str, str] = json.loads(BACKEND_NOTES.read_text(encoding="utf-8"))
        note_keys = set(notes)

        missing = catalog_keys - note_keys
        orphaned = note_keys - catalog_keys
        blank = {key for key in note_keys if not notes[key].strip()}

        assert not missing, f"catalog keys with no translator note: {sorted(missing)}"
        assert not orphaned, f"notes for keys backend.json doesn't have: {sorted(orphaned)}"
        assert not blank, f"blank translator notes: {sorted(blank)}"
        assert len(catalog_keys) == _EXPECTED_CATALOG_KEY_COUNT
        assert len(note_keys) == _EXPECTED_CATALOG_KEY_COUNT


class TestCatalogLoading:
    """`catalog`/`say`'s own contract: loaded through `importlib.resources` rather than a
    cwd-relative path, a blank or missing translated leaf falls back to English, and
    nothing here ever raises out to a caller."""

    def test_loads_the_same_way_whatever_the_process_cwd_is(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        reaper_i18n.catalog.cache_clear()
        monkeypatch.chdir(tmp_path)
        try:
            assert say("launcher.tray.quit") == "Quit Reaper"
        finally:
            reaper_i18n.catalog.cache_clear()

    def test_merge_prefers_a_nonblank_translated_leaf_and_falls_back_otherwise(self) -> None:
        base = {"a": "A", "nested": {"x": "X", "y": "Y"}}
        overlay: dict[str, Any] = {"a": "", "nested": {"x": "X-translated"}, "extra": 4}
        merged = reaper_i18n._merge(base, overlay)
        # A blank overlay string, and a non-string/non-dict overlay value, both leave the
        # English base showing through; a non-blank string and a nested dict both win.
        assert merged == {"a": "A", "nested": {"x": "X-translated", "y": "Y"}}

    def test_say_falls_back_to_the_bare_key_and_warns_when_no_catalog_has_it(self) -> None:
        with capture_logs() as logs:
            result = say("not.a.real.key")
        assert result == "not.a.real.key"
        assert any(entry["event"] == "i18n.missing_key" for entry in logs)

    def test_say_never_raises_when_a_param_cannot_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`int(float("nan"))` raises inside the selectordinal branch -- the one input this
        subset's arithmetic cannot absorb quietly -- so this is what proves `say`'s
        try/except is load-bearing rather than dead code."""
        monkeypatch.setattr(
            reaper_i18n,
            "catalog",
            lambda tag=reaper_i18n.DEFAULT_TAG: {
                "broken": "{n, selectordinal, one {#st} other {#th}}"
            },
        )
        with capture_logs() as logs:
            result = say("broken", n=float("nan"))
        assert result == "broken"
        assert any(entry["event"] == "i18n.format_failed" for entry in logs)
