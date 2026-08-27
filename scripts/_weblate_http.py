# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared HTTP plumbing for the Weblate scripts (`weblate_component.py`, `weblate_glossary.py`,
`weblate_notes.py`): the API key lookup, the host-locked `urlopen` wrapper, and the
5-attempt/429-backoff request loop. This is the one copy, so a fix here reaches all three
scripts instead of needing to be repeated in each.

Not a script itself. Nothing here is run directly, so there is no `__main__` and no
shebang. Standalone by design, using only the standard library, and imported the same way
`weblate_component.py` already imports `COPIED_FIELDS` from `weblate_glossary.py`.
`scripts/` is not a package, and each script runs as `python3 scripts/<name>.py`, which
puts `scripts/` on `sys.path[0]` for every sibling import here too.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_KEY_FILE = Path("/opt/reaper_1/.weblate_api")

#: Retries on a transient failure, and the ceiling on how long one retry may sleep. A
#: `Retry-After` header is a number the remote server chose, so this clamps it instead of
#: trusting it outright.
MAX_ATTEMPTS = 5
MAX_RETRY_AFTER = 60


def api_key(key_file: Path) -> str:
    key = os.environ.get("WEBLATE_API_KEY")
    if key:
        return key.strip()
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    raise SystemExit(
        f"no Weblate API key: set WEBLATE_API_KEY or create {key_file}. Never pass one on the "
        "command line, where it would land in shell history."
    )


def http_open(url: str, *, method: str = "GET", key: str, body: bytes | None = None) -> Any:
    """Make one request, scheme-checked so nothing but `https://hosted.weblate.org` is ever asked.

    The one place every request goes through, so the header carrying the key is attached
    only here. It is never logged, and never visible in a caller's own code.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "hosted.weblate.org":
        raise ValueError(f"refusing to call a non-Weblate host: {url}")
    request = urllib.request.Request(url, data=body, method=method)  # noqa: S310 (host checked above)
    request.add_header("Authorization", f"Token {key}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(request, timeout=30)  # noqa: S310 (host checked above)


def request(
    url: str, *, method: str = "GET", key: str, body: bytes | None = None, allow_404: bool = False
) -> Any:
    """Make one call, retried on a transient failure or a rate limit, and raise otherwise.

    A non-2xx status other than 429 is a real failure, such as a bad key or a moved
    unit, and is never retried, since retrying would only delay the same error.
    `allow_404` returns ``None`` for a 404 instead of raising, for the "does this exist
    yet" probes each caller makes.
    """
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with http_open(url, method=method, key=key, body=body) as response:
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
