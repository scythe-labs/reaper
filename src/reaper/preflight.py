# SPDX-License-Identifier: AGPL-3.0-or-later
"""Startup preflight, run by the container entrypoint before migrations.

Migrations (and then the app) open the database as their first act. If the data
directory is not writable, SQLite fails with ``unable to open database file`` --
a message that names neither the path nor the cause, buried under a driver
traceback. Running this first turns that into a plain line the operator can act
on, printed to stderr (which is where ``docker logs`` reads), and stops the
container cleanly before a half-migrated schema can exist.

    python -m reaper.preflight

It also applies a pending restore. A confirmed restore stages a backup and arms it
(see :mod:`reaper.services.restore`); the swap must happen here, before
``alembic upgrade head`` runs, so the restored database is brought current on the
same boot. Running it in preflight is why "restart the container to finish" is all a
restore asks of the operator.
"""

from __future__ import annotations

import sys

from reaper.config import DataDirError, get_settings
from reaper.services import backup, restore


def main() -> int:
    try:
        settings = get_settings()
        settings.ensure_data_dir()
    except DataDirError as exc:
        # Just the message -- no traceback. The operator needs the fix, not a stack.
        sys.stderr.write(str(exc) + "\n")
        return 1
    # Clear crash-leftover backup/restore temp dirs before anything else. Nothing is in
    # flight this early, and a stale multi-GB partial snapshot only makes a full disk worse
    # (PR-3). A sweep failure is never fatal: a leftover temp is not a reason to refuse boot.
    try:
        swept = backup.sweep_stale_temp(settings)
        if swept:
            sys.stderr.write(f"reaper: cleared {swept} leftover backup/restore temp entries\n")
    except Exception as exc:  # housekeeping must never stop boot
        sys.stderr.write(f"reaper: could not sweep leftover temp entries: {exc}\n")
    try:
        restore.apply_pending_restore(settings)
    except Exception as exc:  # any swap failure must stop boot, not serve a half-restore
        # A restore that cannot complete must not let the app boot on a half-swapped
        # state. The previous data is preserved in the pre-restore directory; stop the
        # container with a plain message rather than serving an uncertain database.
        sys.stderr.write(f"reaper: the restore could not be completed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
