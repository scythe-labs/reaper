# SPDX-License-Identifier: AGPL-3.0-or-later
"""Startup preflight, run by the container entrypoint before migrations.

Migrations (and then the app) open the database as their first act. If the data
directory is not writable, SQLite fails with ``unable to open database file`` --
a message that names neither the path nor the cause, buried under a driver
traceback. Running this first turns that into a plain line the operator can act
on, printed to stderr (which is where ``docker logs`` reads), and stops the
container cleanly before a half-migrated schema can exist.

    python -m reaper.preflight
"""

from __future__ import annotations

import sys

from reaper.config import DataDirError, get_settings


def main() -> int:
    try:
        get_settings().ensure_data_dir()
    except DataDirError as exc:
        # Just the message -- no traceback. The operator needs the fix, not a stack.
        sys.stderr.write(str(exc) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
