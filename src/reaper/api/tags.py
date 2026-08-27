# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sections the API reference is organized into.

Without tags, every operation renders as one unbroken list at ``/api/docs``. There is
nothing to scan, nothing to collapse, and no way to find the route you came for.
OpenAPI's answer is a tag per operation, so this module holds the tag names.

**A router sets the tag, or its routes do, never both.** Most routers here set
``tags=`` on the ``APIRouter`` itself, which tags every route in the file. A few leave
the router bare and tag each route instead, because those routes span more than one
section: ``review`` and ``settings`` genuinely serve more than one, ``plex`` splits a
prefix with ``settings``, and ``policy``, ``simulate``, ``vocabulary``, and ``about``
share that same bare pattern. Converting one of those to a router-level tag means
deleting its route-level tags in the same change, because FastAPI concatenates a
route-level tag with its router's tag rather than overriding it, which would land the
operation in two sidebar sections at once. ``tests/test_openapi_tags.py`` refuses that.
A router whose routes split across sections needs a second router, the way
``api/runs.py`` does for the pace settings.

The names match the app's own: ``Review``, ``Policy``, ``Reap``, ``Scales``, and the
Settings tabs. Someone reading the reference has used the UI, so a section here lands
them on the page they already know instead of a word only this file uses.

``GROUPS`` is the single declaration. It fixes the sidebar order, carries each
section's one-line description, and gathers the sections under the three headings
Scalar renders from ``x-tagGroups``. There is no parallel list to fall out of step
with it, and ``tests/test_openapi_tags.py`` fails on an operation tagged with anything
not declared here, or with nothing at all.
"""

from __future__ import annotations

from typing import Final

SIGN_IN: Final = "Sign in"
SETUP: Final = "Setup"

SCANS: Final = "Scans"
REVIEW: Final = "Review"
POLICY: Final = "Policy"
REAP: Final = "Reap"
SCALES: Final = "Scales"

GENERAL: Final = "General"
SERVICES: Final = "Services"
PLEX: Final = "Plex"
LISTS: Final = "Lists"
JOBS: Final = "Jobs"
NOTIFICATIONS: Final = "Notifications"
SECURITY: Final = "Security"
BACKUP: Final = "Backup & Restore"
LOGS: Final = "Logs"
ABOUT: Final = "About"

#: Heading -> the sections under it, each with the line shown beneath its name.
#: Order here is order in the sidebar.
GROUPS: Final[tuple[tuple[str, tuple[tuple[str, str], ...]], ...]] = (
    (
        "Start here",
        (
            (SIGN_IN, "Sign in, sign out, and check who you are signed in as."),
            (SETUP, "Whether Reaper is up, and what is still missing before it can scan."),
        ),
    ),
    (
        "Your library",
        (
            (SCANS, "Start a scan, watch it run, and see what the last one found."),
            (
                REVIEW,
                "What Reaper wants to delete and why, plus the titles you keep or spare.",
            ),
            (
                POLICY,
                "What flags a title, what is always kept, and how much one run may "
                "remove. Try a change before you save it.",
            ),
            (REAP, "Approve a plan, run it, and read back what was removed."),
            (SCALES, "Who watches what, and how much of the library each person accounts for."),
        ),
    ),
    (
        "Settings",
        (
            (GENERAL, "Name, time zone, colors, and the API key another tool signs in with."),
            (SERVICES, "The Radarr, Sonarr, Tautulli, and Seerr servers Reaper works through."),
            (PLEX, "Link a Plex server and choose which libraries Reaper may touch."),
            (LISTS, "The lists that keep titles safe, and whether each one is still working."),
            (JOBS, "Scheduled work: scans, upkeep, and the Leaving Soon shelf."),
            (NOTIFICATIONS, "Where Reaper posts what it is about to delete."),
            (SECURITY, "The deletion switch from Policy → Deletion, and the password."),
            (BACKUP, "Take a copy of your settings and history, or put one back."),
            (LOGS, "Read the log, download it, and set how much detail it keeps."),
            (ABOUT, "Which version is running."),
        ),
    ),
)

#: Every declared tag, in sidebar order.
ALL: Final[tuple[str, ...]] = tuple(name for _, members in GROUPS for name, _ in members)


def openapi_tags() -> list[dict[str, str]]:
    """Build the schema's root ``tags`` array, in the order Scalar lists them."""
    return [{"name": name, "description": text} for _, members in GROUPS for name, text in members]


def openapi_tag_groups() -> list[dict[str, object]]:
    """Build the ``x-tagGroups`` extension, which Scalar renders as the sidebar's top level."""
    return [{"name": heading, "tags": [name for name, _ in members]} for heading, members in GROUPS]
