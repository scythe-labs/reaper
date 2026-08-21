#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Create Reaper's Weblate glossary component, and seed its terms into every language.

Standalone by design (stdlib only, rule 15), and run by hand rather than by CI: creating a
component is a one-time, human-approved step, and seeding follows a language being added,
which a person does on Weblate. `frontend/src/locales/glossary/en.tbx` is the glossary
itself. TBX is a bilingual format on Weblate, so a term lives inside each target language's
file and the English file is never read by Weblate: this script reads it instead, and posts
every term it holds into each target language's glossary translation, with the TBX
``descrip`` as the term's explanation. "Sanctuary" and "Limbo", Reaper's own product names,
carry Weblate's `read-only` flag, so a translator sees them without a target box inviting a
translation that would never be used.

    python3 scripts/weblate_glossary.py            # creates the component if missing, seeds
    python3 scripts/weblate_glossary.py --dry-run   # prints what it would do, changes nothing

Idempotent either way: a component that already exists is left alone, a term a language
already holds is not posted again, and a term already flagged read-only is not re-flagged.
With no target language yet, seeding has nowhere to go and says so (#878). The API key is
read from `WEBLATE_API_KEY`, or from the file `--key-file` names (default
`/opt/reaper_1/.weblate_api`). Never printed, never logged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

API_ROOT = "https://hosted.weblate.org/api"
PROJECT = "reaper"
UI_COMPONENT_URL = f"{API_ROOT}/components/{PROJECT}/ui/"
GLOSSARY_COMPONENT_URL = f"{API_ROOT}/components/{PROJECT}/glossary/"
GLOSSARY_TRANSLATIONS_URL = f"{GLOSSARY_COMPONENT_URL}translations/?page_size=100"
CREATE_COMPONENT_URL = f"{API_ROOT}/projects/{PROJECT}/components/"
DEFAULT_KEY_FILE = Path("/opt/reaper_1/.weblate_api")

REPO_ROOT = Path(__file__).resolve().parents[1]
GLOSSARY_DIR = "frontend/src/locales/glossary"
GLOSSARY_FILE = f"{GLOSSARY_DIR}/en.tbx"

#: Copied from the `ui` component (task #868 phase 6), never invented here: a glossary that
#: pushes to a different repo, branch or license than the strings it defines terms for is a
#: second source of truth nobody asked for.
COPIED_FIELDS = ("repo", "branch", "push", "license", "vcs")

#: Product names that stay untranslated in every language, per CONTRIBUTING's "Translate it".
READ_ONLY_TERMS = ("Sanctuary", "Limbo")

MAX_ATTEMPTS = 5
MAX_RETRY_AFTER = 60


def _api_key(key_file: Path) -> str:
    key = os.environ.get("WEBLATE_API_KEY")
    if key:
        return key.strip()
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    raise SystemExit(
        f"no Weblate API key: set WEBLATE_API_KEY or create {key_file}. Never pass one on the "
        "command line, where it would land in shell history."
    )


def _http_open(url: str, *, method: str = "GET", key: str, body: bytes | None = None) -> Any:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "hosted.weblate.org":
        raise ValueError(f"refusing to call a non-Weblate host: {url}")
    request = urllib.request.Request(url, data=body, method=method)  # noqa: S310 (host checked above)
    request.add_header("Authorization", f"Token {key}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(request, timeout=30)  # noqa: S310 (host checked above)


def _request(
    url: str, *, method: str = "GET", key: str, body: bytes | None = None, allow_404: bool = False
) -> Any:
    """One call, retried on a transient failure or a rate limit. `allow_404` returns ``None``
    for a 404 instead of raising, for the "does this exist yet" probes."""
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with _http_open(url, method=method, key=key, body=body) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and allow_404:
                return None
            if exc.code == 429 and attempt + 1 < MAX_ATTEMPTS:
                wait = min(int(exc.headers.get("Retry-After", "5") or "5"), MAX_RETRY_AFTER)
                print(f"  rate-limited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                last = exc
                continue
            raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {exc.read()[:500]!r}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"{method} {url} failed after {MAX_ATTEMPTS} tries: {last}")


def _ensure_component(key: str, *, dry_run: bool) -> bool:
    """True once the glossary component exists. Creates it only when absent."""
    existing = _request(GLOSSARY_COMPONENT_URL, key=key, allow_404=True)
    if existing is not None:
        print("glossary component already exists, nothing to create")
        return True

    ui = _request(UI_COMPONENT_URL, key=key)
    payload = {field: ui[field] for field in COPIED_FIELDS}
    payload.update(
        {
            "name": "Glossary",
            "slug": "glossary",
            "file_format": "tbx",
            "is_glossary": True,
            "filemask": f"{GLOSSARY_DIR}/*.tbx",
        }
    )
    if dry_run:
        print("would POST to", CREATE_COMPONENT_URL)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return False

    _request(CREATE_COMPONENT_URL, method="POST", key=key, body=json.dumps(payload).encode())
    print("created the glossary component")
    return True


def _terms_from_tbx(path: Path) -> list[tuple[str, str]]:
    """Every ``(term, explanation)`` in the English TBX, in file order.

    One ``termEntry`` holds one ``term`` and at most one ``descrip``, which CONTRIBUTING's
    "Translate it" describes as the one-line explanation a translator sees beside the term.
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
    """``(language code, units URL)`` for every glossary language except the source."""
    component = _request(GLOSSARY_COMPONENT_URL, key=key)
    source_code = component["source_language"]["code"]
    targets: list[tuple[str, str]] = []
    url: str | None = GLOSSARY_TRANSLATIONS_URL
    while url is not None:
        page = _request(url, key=key)
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
        page = _request(url, key=key)
        for unit in page["results"]:
            source = (unit.get("source") or [""])[0]
            found.setdefault(source, unit)
        url = page.get("next")
    return found


def _seed_language(
    code: str, units_url: str, terms: list[tuple[str, str]], key: str, *, dry_run: bool
) -> None:
    """Post every term the language does not hold yet, then make sure the product names
    carry `read-only`. A read-only term's target is the term itself, translated, which is
    how Weblate's own "untranslatable" checkbox stores one. Every other term is seeded the
    way Weblate's "copy source" does it: the English term in the target, marked "needs
    editing" (state 10), because the API refuses a blank target."""
    present = _units_by_source(units_url, key)
    posted = 0
    for term, explanation in terms:
        if term in present:
            continue
        read_only = term in READ_ONLY_TERMS
        body = {
            "source": [term],
            "target": [term],
            "explanation": explanation,
            "extra_flags": "read-only" if read_only else "",
            "state": 20 if read_only else 10,
        }
        if dry_run:
            print(f"  {code}: would add {term!r}" + (" (read-only)" if read_only else ""))
            continue
        _request(units_url.split("?", 1)[0], method="POST", key=key, body=json.dumps(body).encode())
        posted += 1
    if not dry_run:
        print(f"  {code}: added {posted}, already present {len(present)}")

    for term in READ_ONLY_TERMS:
        unit = present.get(term)
        if unit is None:
            continue
        flags = [f for f in (unit.get("flags") or "").split(",") if f]
        if "read-only" in flags:
            continue
        if dry_run:
            print(f"  {code}: would mark read-only: {term}")
            continue
        flags.append("read-only")
        _request(
            unit["url"],
            method="PATCH",
            key=key,
            body=json.dumps({"flags": ",".join(flags)}).encode(),
        )
        print(f"  {code}: marked read-only: {term}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    args = parser.parse_args(argv)

    key = _api_key(args.key_file)
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
        _seed_language(code, units_url, terms, key, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
