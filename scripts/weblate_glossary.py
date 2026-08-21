#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Create Reaper's Weblate glossary component, and keep its product-name terms read-only.

Standalone by design (stdlib only, rule 15), and run by hand rather than by CI: creating a
component is a one-time, human-approved step. `frontend/src/locales/glossary/en.tbx` is the
glossary itself; this script only makes sure Weblate has a component pointed at it, and that
"Sanctuary" and "Limbo" -- Reaper's own product names -- carry Weblate's `read-only` flag, so a
translator sees them without a target box inviting a translation that would never be used.

    python3 scripts/weblate_glossary.py            # creates the component if missing
    python3 scripts/weblate_glossary.py --dry-run   # prints what it would do, changes nothing

Idempotent either way: a component that already exists is left alone, and a term already
flagged read-only is not re-flagged. The API key is read from `WEBLATE_API_KEY`, or from the
file `--key-file` names (default `/opt/reaper_1/.weblate_api`). Never printed, never logged.
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
from pathlib import Path
from typing import Any

API_ROOT = "https://hosted.weblate.org/api"
PROJECT = "reaper"
UI_COMPONENT_URL = f"{API_ROOT}/components/{PROJECT}/ui/"
GLOSSARY_COMPONENT_URL = f"{API_ROOT}/components/{PROJECT}/glossary/"
CREATE_COMPONENT_URL = f"{API_ROOT}/projects/{PROJECT}/components/"
GLOSSARY_UNITS_URL = f"{API_ROOT}/translations/{PROJECT}/glossary/en/units/?page_size=100"
DEFAULT_KEY_FILE = Path("/opt/reaper_1/.weblate_api")

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
            "template": GLOSSARY_FILE,
            "new_base": GLOSSARY_FILE,
        }
    )
    if dry_run:
        print("would POST to", CREATE_COMPONENT_URL)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return False

    _request(CREATE_COMPONENT_URL, method="POST", key=key, body=json.dumps(payload).encode())
    print("created the glossary component")
    return True


def _sync_read_only_flags(key: str, *, dry_run: bool) -> None:
    """Add Weblate's `read-only` flag to every term in READ_ONLY_TERMS, leaving any flag
    already set on a unit in place."""
    remaining = {term: True for term in READ_ONLY_TERMS}
    url: str | None = GLOSSARY_UNITS_URL
    while url is not None:
        page = _request(url, key=key)
        for unit in page["results"]:
            source = (unit.get("source") or [""])[0]
            if source not in remaining:
                continue
            remaining.pop(source)
            flags = [f for f in (unit.get("flags") or "").split(",") if f]
            if "read-only" in flags:
                print(f"already read-only: {source}")
                continue
            if dry_run:
                print(f"would mark read-only: {source}")
                continue
            flags.append("read-only")
            body = json.dumps({"flags": ",".join(flags)}).encode("utf-8")
            _request(unit["url"], method="PATCH", key=key, body=body)
            print(f"marked read-only: {source}")
        url = page.get("next")

    for term in remaining:
        print(f"term not found in the glossary yet: {term}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    args = parser.parse_args(argv)

    key = _api_key(args.key_file)
    component_exists = _ensure_component(key, dry_run=args.dry_run)
    if not component_exists:
        print("component not created yet (dry run), skipping the read-only flag pass")
        return 0
    _sync_read_only_flags(key, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
