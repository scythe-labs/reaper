# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sections the API reference is organized into.

Untagged, the 87 operations render as one unbroken list at ``/api/docs``: nothing to
scan, nothing to collapse, and no way to find the four routes that do the thing you
came for. OpenAPI's answer is a tag per operation, so this module holds the tag names
and every router picks one.

The names are the app's own: ``Review``, ``Policy``, ``Reap``, ``Scales``, and the
Settings tabs. Someone reading the reference is someone who has used the UI, so a
section here lands them on the page they already know rather than on a word only this
file uses.

``GROUPS`` is the single declaration -- it fixes the sidebar order, carries each
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
            (SCANS, "Start a scan, watch it run, and read the evidence it froze."),
            (
                REVIEW,
                "What Reaper wants to delete and why, plus the titles you keep or spare.",
            ),
            (
                POLICY,
                "The rules that flag a title and what is always kept. "
                "Try a change before you save it.",
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
            (JOBS, "Scheduled work: scans, upkeep, and the Leaving Soon shelf."),
            (NOTIFICATIONS, "Where Reaper posts what it is about to delete."),
            (SECURITY, "Turn deletion on or off, and change the password."),
            (BACKUP, "Take a copy of your settings and history, or put one back."),
            (LOGS, "Read the log, download it, and set how much detail it keeps."),
            (ABOUT, "Which version is running."),
        ),
    ),
)

#: Every declared tag, in sidebar order.
ALL: Final[tuple[str, ...]] = tuple(name for _, members in GROUPS for name, _ in members)


def openapi_tags() -> list[dict[str, str]]:
    """The schema's root ``tags`` array. Order here is the order Scalar lists them in."""
    return [{"name": name, "description": text} for _, members in GROUPS for name, text in members]


def openapi_tag_groups() -> list[dict[str, object]]:
    """The ``x-tagGroups`` extension, which Scalar renders as the sidebar's top level."""
    return [{"name": heading, "tags": [name for name, _ in members]} for heading, members in GROUPS]
