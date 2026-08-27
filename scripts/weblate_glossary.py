#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Create Reaper's Weblate glossary component, and seed its terms into every language.

Standalone by design, using only the standard library, and run by hand rather than by
CI, since creating a component is a one-time, human-approved step, and seeding follows
a language being added, which a person does on Weblate.
`frontend/src/locales/glossary/en.tbx` is the glossary itself. TBX is a bilingual
format on Weblate, so a term lives inside each target language's file, and Weblate
never reads the English file itself (the component's language filter skips it). This
script reads the English file instead, and posts every term it holds into each target
language's glossary translation, with the TBX `descrip` as the term's explanation.
"Sanctuary" and "Limbo", Reaper's own product names, carry Weblate's `read-only` flag,
so a translator sees them without a target box inviting a translation that would never
be used.

    python3 scripts/weblate_glossary.py            # creates the component if missing, seeds
    python3 scripts/weblate_glossary.py --dry-run   # prints what it would do, changes nothing

Idempotent either way: a component that already exists is left alone, a term a
language already holds is not posted again, and a term already flagged read-only is
not re-flagged. With no target language yet, seeding has nowhere to go and says so.
The API key is read from `WEBLATE_API_KEY`, or from the file `--key-file` names
(default `/opt/reaper_1/.weblate_api`). Never printed, never logged.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from _weblate_http import DEFAULT_KEY_FILE, api_key, request

API_ROOT = "https://hosted.weblate.org/api"
PROJECT = "reaper"
UI_COMPONENT_URL = f"{API_ROOT}/components/{PROJECT}/ui/"
GLOSSARY_COMPONENT_URL = f"{API_ROOT}/components/{PROJECT}/glossary/"
GLOSSARY_TRANSLATIONS_URL = f"{GLOSSARY_COMPONENT_URL}translations/?page_size=100"
#: The source-language side, where Weblate keeps a term's explanation and its flags. Every
#: target language's unit points back at one of these through `source_unit`.
GLOSSARY_SOURCE_UNITS_URL = f"{API_ROOT}/translations/{PROJECT}/glossary/en/units/?page_size=100"
CREATE_COMPONENT_URL = f"{API_ROOT}/projects/{PROJECT}/components/"

REPO_ROOT = Path(__file__).resolve().parents[1]
GLOSSARY_DIR = "frontend/src/locales/glossary"
GLOSSARY_FILE = f"{GLOSSARY_DIR}/en.tbx"

#: Copied from the `ui` component, never invented here. A glossary that pushes to a
#: different repo, branch, or license than the strings it defines terms for becomes a
#: second source of truth nobody asked for.
COPIED_FIELDS = ("repo", "branch", "push", "license", "vcs")

#: Product names that stay untranslated in every language, per CONTRIBUTING's "Translate it".
READ_ONLY_TERMS = ("Sanctuary", "Limbo")


def _language_filter(source_code: str) -> str:
    """Return the language filter that keeps the English file out of Weblate's language list.

    The component is bilingual, with no template, so ``en.tbx`` matches the filemask
    as an English translation of an English component, and Weblate raises its
    "Duplicated translation" alert for it. The file stays in git as the glossary this
    script seeds from. This filter is how Weblate is told to skip it.
    """
    return f"^(?!{source_code}$)[^.]+$"


def _ensure_language_filter(component: dict[str, Any], *, key: str, dry_run: bool) -> None:
    """Reconcile an existing component's language filter, so a re-run converges."""
    wanted = _language_filter(component["source_language"]["code"])
    if component.get("language_regex") == wanted:
        print("glossary language filter already set")
        return
    if dry_run:
        print(f"would PATCH language_regex to {wanted!r}")
        return
    request(
        GLOSSARY_COMPONENT_URL,
        method="PATCH",
        key=key,
        body=json.dumps({"language_regex": wanted}).encode(),
    )
    print("set the glossary language filter")


def _ensure_component(key: str, *, dry_run: bool) -> bool:
    """Return True once the glossary component exists. Creates it only when absent."""
    existing = request(GLOSSARY_COMPONENT_URL, key=key, allow_404=True)
    if existing is not None:
        print("glossary component already exists, nothing to create")
        _ensure_language_filter(existing, key=key, dry_run=dry_run)
        return True

    ui = request(UI_COMPONENT_URL, key=key)
    payload = {field: ui[field] for field in COPIED_FIELDS}
    payload.update(
        {
            "name": "Glossary",
            "slug": "glossary",
            "file_format": "tbx",
            "is_glossary": True,
            "filemask": f"{GLOSSARY_DIR}/*.tbx",
            "language_regex": _language_filter(ui["source_language"]["code"]),
        }
    )
    if dry_run:
        print("would POST to", CREATE_COMPONENT_URL)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return False

    request(CREATE_COMPONENT_URL, method="POST", key=key, body=json.dumps(payload).encode())
    print("created the glossary component")
    return True


def _terms_from_tbx(path: Path) -> list[tuple[str, str]]:
    """Return every ``(term, explanation)`` in the English TBX, in file order.

    One ``termEntry`` holds one ``term`` and at most one ``descrip``, which
    CONTRIBUTING's "Translate it" section describes as the one-line explanation a
    translator sees beside the term.
    """
    root = ET.parse(path).getroot()  # noqa: S314 (a committed file of our own, not input)
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    prefix = f"{{{namespace}}}" if namespace else ""
    terms: list[tuple[str, str]] = []
    for entry in root.iter(f"{prefix}termEntry"):
        term = next(entry.iter(f"{prefix}term"), None)
        descrip = next(entry.iter(f"{prefix}descrip"), None)
        if term is None or not (term.text or "").strip():
            continue
        terms.append(
            (
                (term.text or "").strip(),
                " ".join((descrip.text or "").split()) if descrip is not None else "",
            )
        )
    return terms


def _target_translations(key: str) -> list[tuple[str, str]]:
    """Return ``(language code, units URL)`` for every glossary language except the source."""
    component = request(GLOSSARY_COMPONENT_URL, key=key)
    source_code = component["source_language"]["code"]
    targets: list[tuple[str, str]] = []
    url: str | None = GLOSSARY_TRANSLATIONS_URL
    while url is not None:
        page = request(url, key=key)
        for translation in page["results"]:
            code = translation["language_code"]
            if code == source_code:
                continue
            targets.append((code, f"{translation['url']}units/?page_size=100"))
        url = page.get("next")
    return targets


def _units_by_source(units_url: str, key: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    url: str | None = units_url
    while url is not None:
        page = request(url, key=key)
        for unit in page["results"]:
            source = (unit.get("source") or [""])[0]
            found.setdefault(source, unit)
        url = page.get("next")
    return found


def _seed_language(code: str, units_url: str, terms: list[str], key: str, *, dry_run: bool) -> None:
    """Post every term the language does not hold yet.

    A product name is posted as its own translation, the term itself, which is how
    Weblate's own "untranslatable" checkbox stores one. Every other term is seeded
    the way Weblate's "copy source" feature does it: the English term in the target,
    marked "needs editing" (state 10), since the API refuses a blank target. The
    explanation and the `read-only` flag are not sent here. Weblate keeps both on the
    source unit, which `_sync_source_units` writes once per term.
    """
    present = _units_by_source(units_url, key)
    posted = 0
    for term in terms:
        if term in present:
            continue
        read_only = term in READ_ONLY_TERMS
        body = {"source": [term], "target": [term], "state": 20 if read_only else 10}
        if dry_run:
            print(f"  {code}: would add {term!r}" + (" (read-only)" if read_only else ""))
            continue
        request(units_url.split("?", 1)[0], method="POST", key=key, body=json.dumps(body).encode())
        posted += 1
    if not dry_run:
        print(f"  {code}: added {posted}, already present {len(present)}")


def _sync_source_units(terms: list[tuple[str, str]], key: str, *, dry_run: bool) -> None:
    """Write each term's explanation and, for the product names, the `read-only` flag onto
    its source unit, the `en` side every language shares. Sends only the fields that
    differ, so a re-run stays silent. A term with no source unit yet has not been
    seeded into any language, and is named in the output."""
    present = _units_by_source(GLOSSARY_SOURCE_UNITS_URL, key)
    changed = 0
    for term, explanation in terms:
        unit = present.get(term)
        if unit is None:
            print(f"  no source unit yet: {term}")
            continue
        patch: dict[str, str] = {}
        if (unit.get("explanation") or "") != explanation:
            patch["explanation"] = explanation
        if term in READ_ONLY_TERMS:
            flags = [f for f in (unit.get("extra_flags") or "").split(",") if f]
            if "read-only" not in flags:
                patch["extra_flags"] = ",".join([*flags, "read-only"])
        if not patch:
            continue
        if dry_run:
            print(f"  would set {sorted(patch)} on {term!r}")
            continue
        request(unit["source_unit"], method="PATCH", key=key, body=json.dumps(patch).encode())
        changed += 1
    if not dry_run:
        print(f"  source units updated: {changed}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    args = parser.parse_args(argv)

    key = api_key(args.key_file)
    component_exists = _ensure_component(key, dry_run=args.dry_run)
    if not component_exists:
        print("component not created yet (dry run), skipping the seeding pass")
        return 0

    terms = _terms_from_tbx(REPO_ROOT / GLOSSARY_FILE)
    targets = _target_translations(key)
    if not targets:
        print(f"{len(terms)} terms in {GLOSSARY_FILE}, no target language on Weblate yet to seed")
        return 0
    print(f"{len(terms)} terms in {GLOSSARY_FILE}, {len(targets)} target language(s)")
    for code, units_url in targets:
        _seed_language(code, units_url, [term for term, _ in terms], key, dry_run=args.dry_run)
    _sync_source_units(terms, key, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
