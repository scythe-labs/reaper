# SPDX-License-Identifier: AGPL-3.0-or-later
"""Port of `frontend/src/i18n-locales.test.ts` for the backend catalog (see
docs/history/I18N_PLAN.md). This is the review a translated
`src/reaper/locales/<tag>/backend.json` gets before it is safe to ship, the same review
Weblate's own pull request gets for a translated `ui.json` automatically.

Five things are checked, for every shipped tag other than `en`:

* Every key the translation carries is a real `en/backend.json` key. An orphan catalog
  entry is exactly as much a bug as a typo'd call site, just seen from the other side.
* Every message renders through `reaper.i18n.format_icu` against the English entry's own
  argument names, so a translation this project's ICU subset cannot parse is caught rather
  than degrading silently in production (`say`'s own broad `except` would hide it there).
* No message references an argument the English original does not have. A translation may
  drop an argument, but never invent one. `format_icu` would render an invented one as an
  empty string, which reads as a translator's typo rather than a missing feature.
* The tag is a canonical BCP 47 spelling (`pt-BR`, never `pt_BR` or `pt-br`). The frontend
  checks this against `Intl.getCanonicalLocales`, which has no Python equivalent in the
  standard library, so this is a small hand-written checker, proven against the shapes it
  must accept and reject.
* `reaper.i18n.PLURAL_RULES` has an entry for the tag. `test_backend_i18n.py`'s
  `TestPluralRulesCoverEveryShippedUiTranslation` already proves this for every tag
  shipped under `frontend/src/locales`, a *different* directory that can drift from this
  one (a language could ship a UI translation with no backend catalog, or the reverse).
  This is not a restatement of that test. It is the same obligation over this catalog's
  own population, read through `reaper.i18n.shipped_tags()` rather than a second directory
  walk.

No committed non-English `backend.json` exists yet. Weblate has nowhere to open one from
until `scripts/weblate_component.py` creates the `backend` component, which this same
change ships but does not run (see that script's own docstring). So every check below is
proven against a fixture written to `tmp_path`, with `reaper.i18n`'s locales root
monkeypatched onto it, never a committed non-English file.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import reaper.i18n as reaper_i18n
from reaper.i18n import _ICU_QUOTE_STARTS, _split_options, format_icu

REPO = Path(__file__).resolve().parents[1]
EN_BACKEND = REPO / "src" / "reaper" / "locales" / "en" / "backend.json"


def _leaves(node: object, prefix: str = "") -> dict[str, str]:
    """A nested catalog dict flattened to dotted-key -> message, the same shape
    `test_backend_i18n.py`'s own `_leaves` gives `backend.json` and `test/catalog.ts`'s
    `leaves()` gives the frontend's own catalog gates."""
    if isinstance(node, str):
        return {prefix: node}
    assert isinstance(node, dict), f"{prefix or '<root>'} is neither a string nor a section"
    out: dict[str, str] = {}
    for name, child in node.items():
        out.update(_leaves(child, f"{prefix}.{name}" if prefix else name))
    return out


def _referenced_names(message: str) -> set[str]:
    """Every argument name `message` references, across *every* branch of a nested plural,
    selectordinal or select, not just the one branch the data in hand would pick.

    This is deliberately a second walk over the syntax rather than a byproduct of
    `format_icu`'s own. `format_icu` renders *one* branch, whichever the params select, so
    a translator's typo in a branch no fixture happens to exercise would render clean and
    ship silent. Checking a translation's argument names has to see every branch before
    anything picks one, which is a different traversal from rendering one message, so it
    earns its own function rather than bolting a second output onto the render pass.

    This shares `format_icu`'s own quote and brace handling (`_ICU_QUOTE_STARTS`,
    `_split_options`), so an escaped literal brace does not desync this walk the same way
    `format_icu`'s own docstring warns it can desync that one.
    """
    names: set[str] = set()
    i = 0
    n = len(message)
    while i < n:
        ch = message[i]
        if ch == "'" and i + 1 < n and message[i + 1] in _ICU_QUOTE_STARTS:
            if message[i + 1] == "'":
                i += 2
                continue
            end = message.find("'", i + 1)
            i = (end + 1) if end != -1 else n
            continue
        if ch != "{":
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
        names.add(head.strip())
        if rest:
            kind, _, body = rest.partition(",")
            if kind.strip() in ("plural", "selectordinal", "select"):
                for branch in _split_options(body).values():
                    names |= _referenced_names(branch)
    return names


class TestTheNameExtractorReadsEveryFormTheCatalogWrites:
    """Written down and proven against every ICU shape `backend.json` uses today, plus the
    shapes a translation must not be able to hide an argument inside, before this walk is
    trusted to check either."""

    def test_a_bare_interpolation(self) -> None:
        assert _referenced_names("{name} is here") == {"name"}

    def test_a_number_argument(self) -> None:
        assert _referenced_names("{n, number}") == {"n"}

    def test_a_plural_collects_every_branch_not_just_one(self) -> None:
        message = "{count, plural, one {# file} other {# files, see {extra}}}"
        assert _referenced_names(message) == {"count", "extra"}

    def test_a_selectordinal(self) -> None:
        assert _referenced_names("{n, selectordinal, one {#st} other {#th}}") == {"n"}

    def test_a_select(self) -> None:
        assert _referenced_names("{media, select, movie {a movie} other {a show}}") == {"media"}

    def test_a_plural_nested_inside_a_plural_collects_both(self) -> None:
        message = "{a, plural, other {# outer {b, plural, other {# inner {c}}}}}"
        assert _referenced_names(message) == {"a", "b", "c"}

    def test_a_plain_apostrophe_in_prose_is_not_an_escape(self) -> None:
        assert _referenced_names("Reaper didn't start: port {port}.") == {"port"}

    def test_an_escaped_literal_brace_hides_no_argument(self) -> None:
        assert _referenced_names("a literal '{' brace {n}") == {"n"}

    def test_plain_text_has_no_arguments(self) -> None:
        assert _referenced_names("Quit Reaper") == set()


#: A canonical BCP 47 tag, reduced to the shapes this catalog can actually ship: a lowercase
#: 2-3 letter primary subtag, an optional title-case 4-letter script, an optional region as
#: two uppercase letters or three digits. `Intl.getCanonicalLocales` (the frontend's own
#: check, `i18n-locales.test.ts`) covers the full BCP 47 grammar. Nothing in the standard
#: library does, and this project adds no dependency just for it, so this is the reduced,
#: hand-written equivalent.
_CANONICAL_BCP47 = re.compile(r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?$")


def _is_canonical_bcp47(tag: str) -> bool:
    return bool(_CANONICAL_BCP47.fullmatch(tag))


class TestTheCanonicalTagChecker:
    """Proven against the shapes it must accept and reject, for the second hand-written
    matcher this file adds."""

    def test_accepts_the_shapes_the_catalog_uses(self) -> None:
        assert _is_canonical_bcp47("en")
        assert _is_canonical_bcp47("de")
        assert _is_canonical_bcp47("pt-BR")

    def test_rejects_the_shapes_a_hand_edit_or_a_bad_sync_could_introduce(self) -> None:
        assert not _is_canonical_bcp47("pt_BR")
        assert not _is_canonical_bcp47("pt-br")
        assert not _is_canonical_bcp47("PT-BR")
        assert not _is_canonical_bcp47("")
        assert not _is_canonical_bcp47("english")


def _translation_problems(english: dict[str, str], translated: dict[str, str]) -> list[str]:
    """What is wrong with `translated` standing in for `english`, one line per finding. This
    is the same review `frontend/src/i18n-locales.test.ts`'s `catalogProblems` gives a
    translated `ui.json`, over this catalog's simpler ICU subset. An empty message is
    untranslated and serves English (`reaper.i18n._merge`), so it is never a finding."""
    problems: list[str] = []
    for key, message in translated.items():
        original = english.get(key)
        if original is None:
            problems.append(f"{key}: not in the English catalog")
            continue
        if message == "":
            continue
        known = _referenced_names(original)
        try:
            format_icu(message, dict.fromkeys(known, 2), tag="en")
        except Exception as exc:  # `format_icu` supports no syntax that should reach here. A
            # translation that does used a shape past reaper.i18n's own documented boundary.
            problems.append(f"{key}: {exc!r}")
            continue
        extra = sorted(_referenced_names(message) - known)
        if extra:
            names = ", ".join(f"{{{name}}}" for name in extra)
            problems.append(f"{key}: argument {names} is not in the English message")
    return problems


class TestTranslationReviewFailsOnEachThingItClaimsToCatch:
    """Proven against a fixture for each finding it claims to make, not only against a
    clean example."""

    def test_a_clean_translation_is_not_a_finding(self) -> None:
        english = {"a": "{n, plural, one {# file} other {# files}}", "b": "Plain"}
        assert _translation_problems(english, {"a": "eine Datei", "b": ""}) == []

    def test_a_key_the_english_catalog_lacks(self) -> None:
        assert _translation_problems({"a": "Plain"}, {"b": "x"}) == [
            "b: not in the English catalog"
        ]

    def test_an_invented_argument(self) -> None:
        english = {"a": "{n, plural, one {# file} other {# files}}"}
        translated = {"a": "{count, plural, one {# Datei} other {# Dateien}}"}
        assert _translation_problems(english, translated) == [
            "a: argument {count} is not in the English message"
        ]

    def test_dropping_an_argument_is_allowed(self) -> None:
        # A translation may drop a tag or an argument. It may not invent one.
        assert _translation_problems({"a": "{n} files"}, {"a": "Dateien"}) == []

    def test_unparseable_icu_is_a_finding(self) -> None:
        assert _translation_problems({"a": "Plain"}, {"a": "{n, plural"})[0].startswith("a:")


def _write_backend_locale(root: Path, tag: str, catalog: dict[str, Any]) -> None:
    directory = root / tag
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "backend.json").write_text(json.dumps(catalog), encoding="utf-8")


@pytest.fixture
def locales_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """`reaper.i18n`'s locales root repointed at `tmp_path`, seeded with the *real* shipped
    English catalog. Every check below runs against the actual sentences a translator sees,
    never a synthetic stand-in for them. Both caches this module keys on the locales root
    (`catalog`, `shipped_tags`) are cleared on the way in and the way out, so a stale
    tmp_path result can never leak into a test that runs after this one in the same worker.
    """
    english = json.loads(EN_BACKEND.read_text(encoding="utf-8"))
    _write_backend_locale(tmp_path, "en", english)
    monkeypatch.setattr(reaper_i18n, "_locales_root", lambda: tmp_path)
    reaper_i18n.catalog.cache_clear()
    reaper_i18n.shipped_tags.cache_clear()
    try:
        yield tmp_path
    finally:
        reaper_i18n.catalog.cache_clear()
        reaper_i18n.shipped_tags.cache_clear()


#: A well-formed German fixture covering a plural, a plain sentence and a bare
#: interpolation, enough of `en/backend.json`'s own shapes to prove the whole review passes
#: something real, without transcribing every key it has.
_GOOD_DE_BACKEND: dict[str, Any] = {
    "discord": {
        "leaving_soon": {
            "title": "{count, plural, one {# Titel läuft} other {# Titel laufen}} bald ab",
            "open_link": "{app_name} öffnen",
        }
    },
    "launcher": {"tray": {"quit": "Reaper beenden"}},
}


class TestEveryShippedBackendLocale:
    """The five checks this file ports, each run over `locales_root`'s one fixture tag
    (`de`, well-formed) so a passing member of each population is on record, alongside
    `TestTranslationReviewFailsOnEachThingItClaimsToCatch` proving the checks themselves fire.
    """

    def _shipped_non_english(self) -> list[str]:
        return [tag for tag in reaper_i18n.shipped_tags() if tag != "en"]

    def test_every_key_is_an_english_key(self, locales_root: Path) -> None:
        _write_backend_locale(locales_root, "de", _GOOD_DE_BACKEND)
        english = _leaves(json.loads(EN_BACKEND.read_text(encoding="utf-8")))
        for tag in self._shipped_non_english():
            translated = _leaves(json.loads((locales_root / tag / "backend.json").read_text()))
            missing = set(translated) - set(english)
            assert not missing, f"{tag}: keys not in English: {sorted(missing)}"

    def test_every_message_formats_through_format_icu_with_the_english_entrys_params(
        self, locales_root: Path
    ) -> None:
        _write_backend_locale(locales_root, "de", _GOOD_DE_BACKEND)
        english = _leaves(json.loads(EN_BACKEND.read_text(encoding="utf-8")))
        for tag in self._shipped_non_english():
            translated = _leaves(json.loads((locales_root / tag / "backend.json").read_text()))
            for key, message in translated.items():
                if message == "":
                    continue
                params = dict.fromkeys(_referenced_names(english[key]), 2)
                format_icu(message, params, tag="en")  # must not raise

    def test_no_message_uses_an_argument_the_english_lacks(self, locales_root: Path) -> None:
        _write_backend_locale(locales_root, "de", _GOOD_DE_BACKEND)
        english = _leaves(json.loads(EN_BACKEND.read_text(encoding="utf-8")))
        for tag in self._shipped_non_english():
            translated = _leaves(json.loads((locales_root / tag / "backend.json").read_text()))
            assert _translation_problems(english, translated) == []

    def test_the_tag_is_canonical_bcp_47(self, locales_root: Path) -> None:
        _write_backend_locale(locales_root, "de", _GOOD_DE_BACKEND)
        for tag in self._shipped_non_english():
            assert _is_canonical_bcp47(tag), f"{tag} is not a canonical BCP 47 tag"

    def test_plural_rules_has_the_tag(self, locales_root: Path) -> None:
        _write_backend_locale(locales_root, "de", _GOOD_DE_BACKEND)
        for tag in self._shipped_non_english():
            assert tag in reaper_i18n.PLURAL_RULES, (
                f"add a cardinal plural rule to reaper.i18n.PLURAL_RULES for {tag!r}"
            )

    def test_a_noncanonical_or_unruled_tag_fails_its_own_checks(self, locales_root: Path) -> None:
        """These two checks are proven to *fire*, not only to pass, against a directory
        Weblate could plausibly create (a bad sync, or a language this table has not caught
        up with yet)."""
        _write_backend_locale(locales_root, "de_DE", {"launcher": {"tray": {"quit": ""}}})
        _write_backend_locale(locales_root, "xx", {"launcher": {"tray": {"quit": ""}}})
        shipped = self._shipped_non_english()
        assert "de_DE" in shipped and "xx" in shipped
        assert not _is_canonical_bcp47("de_DE")
        assert "xx" not in reaper_i18n.PLURAL_RULES
