#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Push a repo notes file into Weblate as each unit's explanation, for every component that
carries one: `ui` (`frontend/src/locales/en/ui.notes.json`) and, since phase 10b, `backend`
(`src/reaper/locales/en/backend.notes.json`).

Standalone by design (stdlib only, rule 15): the CI job that runs this on every push to either
notes file installs nothing beyond Python itself. Weblate stores a translator note per source
string as a unit's `explanation` field; this script is the one place that writes it, so a note is
edited here, in the repository, never by hand on Weblate (CONTRIBUTING's "Translate it").

    python3 scripts/weblate_notes.py            # writes the diff, both components
    python3 scripts/weblate_notes.py --dry-run   # prints the diff, writes nothing

The API key is read from `WEBLATE_API_KEY`, or from the file `--key-file` names
(default `/opt/reaper_1/.weblate_api`). Never printed, never logged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _weblate_http import DEFAULT_KEY_FILE, api_key, request

API_ROOT = "https://hosted.weblate.org/api"
PROJECT = "reaper"
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Every component this script keeps in sync, and the one repo file that holds its notes. Both
#: sides are edited only here (CONTRIBUTING's "Translate it"): Weblate's own explanation field
#: is overwritten by the next push regardless of a hand edit there.
_COMPONENTS: tuple[tuple[str, Path], ...] = (
    ("ui", REPO_ROOT / "frontend/src/locales/en/ui.notes.json"),
    ("backend", REPO_ROOT / "src/reaper/locales/en/backend.notes.json"),
)


def _component_url(component: str) -> str:
    return f"{API_ROOT}/components/{PROJECT}/{component}/"


def _units_url(component: str) -> str:
    return f"{API_ROOT}/translations/{PROJECT}/{component}/en/units/?page_size=100"


def _fetch_units(key: str, units_url: str) -> dict[str, dict[str, Any]]:
    """Every English unit's `context` (the dotted catalog key) mapped to its `source_unit`
    URL and current `explanation`, paging through `next` until it is null."""
    units: dict[str, dict[str, Any]] = {}
    url: str | None = units_url
    while url is not None:
        page = request(url, key=key)
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
    by propagating out of `_weblate_http.request` uncaught, since a PATCH that failed partway
    through is not a state a "missing" count can describe.
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
            request(unit["patch_url"], method="PATCH", key=key, body=body)
            print(f"changed: {context}")

    verb = "would change" if dry_run else "changed"
    print(f"\n{verb}: {changed}, unchanged: {unchanged}, missing: {missing}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the diff, change nothing")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    args = parser.parse_args(argv)

    key = api_key(args.key_file)
    for component, notes_path in _COMPONENTS:
        print(f"== {component} ==")
        # A component this script's own notes file names before `scripts/weblate_component.py`
        # has created it: the workflow fires on the same push that adds a component's notes
        # file, which can land before anyone runs that script by hand. Skipping rather than
        # raising keeps that push from failing CI over a component that is created moments
        # later -- the next push (or a manual re-run) picks up the notes once it exists.
        if request(_component_url(component), key=key, allow_404=True) is None:
            print(f"  {component} does not exist on Weblate yet, skipping")
            continue
        notes: dict[str, str] = json.loads(notes_path.read_text(encoding="utf-8"))
        units = _fetch_units(key, _units_url(component))
        sync(notes, units, dry_run=args.dry_run, key=key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
