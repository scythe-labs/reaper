#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Create Reaper's Weblate component for the backend catalog (`src/reaper/locales/*/backend.json`,
docs/history/I18N_PLAN.md's phase 10b), and install its git-squash add-on.

Standalone by design (stdlib only, rule 15), and run by hand rather than by CI: creating a
component is a one-time, human-approved step. Modeled on `scripts/weblate_glossary.py`'s own
`_ensure_component`, which is `ui`'s component copied field for field -- ``COPIED_FIELDS`` plus
``ui``'s own ``language_regex`` here, since this component is a monolingual JSON catalog with a
template (like ``ui``), not a bilingual TBX one (like ``glossary``).

    python3 scripts/weblate_component.py            # creates the component if missing
    python3 scripts/weblate_component.py --dry-run   # prints what it would do, changes nothing

Idempotent either way: a component that already exists, or an add-on already installed, is left
alone. The API key is read from `WEBLATE_API_KEY`, or from the file `--key-file` names (default
`/opt/reaper_1/.weblate_api`). Never printed, never logged.

Do not run this for real before this change has merged to `dev`: the filemask
(`src/reaper/locales/*/backend.json`) names a path Weblate cannot yet find on the branch it
reads.
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

from weblate_glossary import COPIED_FIELDS

API_ROOT = "https://hosted.weblate.org/api"
PROJECT = "reaper"
UI_COMPONENT_URL = f"{API_ROOT}/components/{PROJECT}/ui/"
BACKEND_COMPONENT_URL = f"{API_ROOT}/components/{PROJECT}/backend/"
BACKEND_ADDONS_URL = f"{BACKEND_COMPONENT_URL}addons/"
CREATE_COMPONENT_URL = f"{API_ROOT}/projects/{PROJECT}/components/"
DEFAULT_KEY_FILE = Path("/opt/reaper_1/.weblate_api")

BACKEND_DIR = "src/reaper/locales"
BACKEND_FILEMASK = f"{BACKEND_DIR}/*/backend.json"
#: The English file is both the template Weblate diffs translations against and the seed a new
#: language starts from -- the same file serving both roles, as `ui`'s own component does.
BACKEND_TEMPLATE = f"{BACKEND_DIR}/en/backend.json"

#: `icu-message-format` enforces the exact subset `reaper.i18n.format_icu` implements (rule 21's
#: "explain a technical term once": see that module's docstring); `icu-flags:xml` matches `ui`'s
#: own check flags rather than inventing a second spelling for the same ICU dialect.
CHECK_FLAGS = "icu-message-format, icu-flags:xml"
ENFORCED_CHECKS = ["icu_message_format"]

#: Installed on `ui` and `glossary` alike (CONTRIBUTING's "Translate it"): one commit per sync
#: instead of one per string, so a translator's session lands as a single, reviewable commit.
GIT_SQUASH_ADDON: dict[str, Any] = {
    "name": "weblate.git.squash",
    "configuration": {"squash": "all", "append_trailers": True, "commit_message": ""},
}


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
    for attempt in range(5):
        try:
            with _http_open(url, method=method, key=key, body=body) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and allow_404:
                return None
            if exc.code == 429 and attempt + 1 < 5:
                wait = min(int(exc.headers.get("Retry-After", "5") or "5"), 60)
                print(f"  rate-limited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                last = exc
                continue
            raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {exc.read()[:500]!r}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"{method} {url} failed after 5 tries: {last}")


def _ensure_component(key: str, *, dry_run: bool) -> bool:
    """True once the backend component exists. Creates it only when absent."""
    existing = _request(BACKEND_COMPONENT_URL, key=key, allow_404=True)
    if existing is not None:
        print("backend component already exists, nothing to create")
        return True

    ui = _request(UI_COMPONENT_URL, key=key)
    payload = {field: ui[field] for field in COPIED_FIELDS}
    # Not the glossary's bilingual exclusion filter (there is no English translation unit to
    # skip here, since `en` is the template): `ui`'s own filter is the one to copy.
    payload["language_regex"] = ui["language_regex"]
    payload.update(
        {
            "name": "Backend",
            "slug": "backend",
            "file_format": "json-nested",
            "filemask": BACKEND_FILEMASK,
            "template": BACKEND_TEMPLATE,
            "new_base": BACKEND_TEMPLATE,
            "check_flags": CHECK_FLAGS,
            "enforced_checks": ENFORCED_CHECKS,
        }
    )
    if dry_run:
        print("would POST to", CREATE_COMPONENT_URL)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return False

    _request(CREATE_COMPONENT_URL, method="POST", key=key, body=json.dumps(payload).encode())
    print("created the backend component")
    return True


def _ensure_git_squash_addon(key: str, *, dry_run: bool) -> None:
    addons = _request(BACKEND_ADDONS_URL, key=key)
    results = addons.get("results", addons) if isinstance(addons, dict) else addons
    if any(a.get("name") == GIT_SQUASH_ADDON["name"] for a in results):
        print("git.squash add-on already installed")
        return
    if dry_run:
        print("would POST the git.squash add-on")
        print(json.dumps(GIT_SQUASH_ADDON, indent=2, sort_keys=True))
        return
    _request(BACKEND_ADDONS_URL, method="POST", key=key, body=json.dumps(GIT_SQUASH_ADDON).encode())
    print("installed the git.squash add-on")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    args = parser.parse_args(argv)

    key = _api_key(args.key_file)
    component_exists = _ensure_component(key, dry_run=args.dry_run)
    if not component_exists:
        print("component not created yet (dry run), skipping the add-on check")
        return 0
    _ensure_git_squash_addon(key, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
