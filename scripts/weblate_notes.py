#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Push `frontend/src/locales/en/ui.notes.json` into Weblate as each unit's explanation.

Standalone by design (stdlib only, rule 15): the CI job that runs this on every push to
`frontend/src/locales/en/ui.notes.json` installs nothing beyond Python itself. Weblate stores a
translator note per source string as a unit's `explanation` field; this script is the one place
that writes it, so a note is edited here, in the repository, never by hand on Weblate (§6 of the
i18n plan, CONTRIBUTING's "Translate it").

    python3 scripts/weblate_notes.py            # writes the diff
    python3 scripts/weblate_notes.py --dry-run   # prints the diff, writes nothing

The API key is read from `WEBLATE_API_KEY`, or from the file `--key-file` names
(default `/opt/reaper_1/.weblate_api`). Never printed, never logged.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_ROOT = "https://hosted.weblate.org/api"
UNITS_URL = f"{API_ROOT}/translations/reaper/ui/en/units/?page_size=100"
NOTES_PATH = Path(__file__).resolve().parent.parent / "frontend/src/locales/en/ui.notes.json"
DEFAULT_KEY_FILE = Path("/opt/reaper_1/.weblate_api")

#: Retries on a transient failure, and the ceiling on how long one retry may sleep -- a
#: `Retry-After` is a remote server's number, and rule 114 (backend.md) is the same doctrine
#: for any sleep driven by one: clamp it, never trust it outright.
MAX_ATTEMPTS = 5
MAX_RETRY_AFTER = 60


def _api_key(key_file: Path) -> str:
    import os

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
    """One request, scheme-checked so nothing but `https://hosted.weblate.org` is ever asked.

    The one place every request goes through, so a header carrying the key can only ever be
    attached here -- never logged, and never visible in a caller's own code.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "hosted.weblate.org":
        raise ValueError(f"refusing to call a non-Weblate host: {url}")
    request = urllib.request.Request(url, data=body, method=method)  # noqa: S310 (host checked above)
    request.add_header("Authorization", f"Token {key}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(request, timeout=30)  # noqa: S310 (host checked above)


def _request_json(url: str, *, method: str = "GET", key: str, body: bytes | None = None) -> Any:
    """One call, retried on a transient failure or a rate limit, else raised.

    A non-2xx status other than 429 is a real failure (a bad key, a moved unit) and is not
    retried -- retrying it would only hide the failure behind a delay before the same error.
    """
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with _http_open(url, method=method, key=key, body=body) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
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


def _fetch_units(key: str) -> dict[str, dict[str, Any]]:
    """Every English unit's `context` (the dotted catalog key) mapped to its `source_unit`
    URL and current `explanation`, paging through `next` until it is null."""
    units: dict[str, dict[str, Any]] = {}
    url: str | None = UNITS_URL
    while url is not None:
        page = _request_json(url, key=key)
        for unit in page["results"]:
            context = unit.get("context")
            if not context:
                continue
            # The English translation IS the source language here, so `source_unit` is
            # usually this same unit's own URL; falling back to `url` covers a component
            # where Weblate ever leaves it null instead.
            units[context] = {
                "explanation": unit.get("explanation") or "",
                "patch_url": unit.get("source_unit") or unit["url"],
            }
        url = page.get("next")
    return units


def sync(
    notes: dict[str, str], units: dict[str, dict[str, Any]], *, dry_run: bool, key: str
) -> None:
    """Write every note that differs from Weblate's stored explanation.

    A key with no matching unit is reported as `missing`, not raised: it is the ordinary gap
    between a merge landing here and Weblate's next pull of `dev`, not a failure this job should
    fail CI over. An HTTP failure is the one thing that ends the run non-zero, and it does that
    by propagating out of `_request_json` uncaught, since a PATCH that failed partway through is
    not a state a "missing" count can describe.
    """
    changed = unchanged = missing = 0
    for context in sorted(notes):
        note = notes[context]
        unit = units.get(context)
        if unit is None:
            missing += 1
            print(f"missing (no Weblate unit yet): {context}")
            continue
        if unit["explanation"] == note:
            unchanged += 1
            continue
        changed += 1
        if dry_run:
            print(f"would change: {context}")
        else:
            body = json.dumps({"explanation": note}).encode("utf-8")
            _request_json(unit["patch_url"], method="PATCH", key=key, body=body)
            print(f"changed: {context}")

    verb = "would change" if dry_run else "changed"
    print(f"\n{verb}: {changed}, unchanged: {unchanged}, missing: {missing}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the diff, change nothing")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    args = parser.parse_args(argv)

    key = _api_key(args.key_file)
    notes: dict[str, str] = json.loads(NOTES_PATH.read_text(encoding="utf-8"))
    units = _fetch_units(key)
    sync(notes, units, dry_run=args.dry_run, key=key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
