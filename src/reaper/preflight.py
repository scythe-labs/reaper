# SPDX-License-Identifier: AGPL-3.0-or-later
"""Startup preflight, run by the container entrypoint before migrations.

Migrations (and then the app) open the database as their first act. If the data
directory is not writable, SQLite fails with ``unable to open database file``, a
message that names neither the path nor the cause, buried under a driver traceback.
Running this first turns that into a plain line the operator can act on, printed to
stderr (which is where ``docker logs`` reads), and stops the container cleanly before a
half-migrated schema can exist.

    python -m reaper.preflight

It also applies a pending restore. A confirmed restore stages a backup and arms it
(see :mod:`reaper.services.restore`); the swap must happen here, before
``alembic upgrade head`` runs, so the restored database is brought current on the same
boot. Running it in preflight is why restarting is all a restore asks of the operator,
and why ``POST /api/settings/backup/restore/restart`` can offer that as a button rather
than as an instruction.

Every way of starting Reaper runs this, or a staged restore is silently never applied.
The container entrypoint does, and so does ``scripts/dev-local.sh`` and
:func:`reaper.launcher.main`.

Last, it refuses a database this build cannot serve (:mod:`reaper.db.schema_gate`), and
then copies the database aside when a pending migration asks for it
(:func:`reaper.services.backup.snapshot_before_migration`).

Being the one thing every launcher runs before ``alembic upgrade head`` is exactly why
both sit here. It is the last moment a rolled-back install can be stopped before
anything is migrated, and the last moment a migration that its own ``downgrade()``
cannot undo can be given something to fall back to. It is also why neither needed a
line in the container entrypoint, the dev script, or the launcher: those three already
run this, in this order, and a copy in each would be three places for one of them to
fall out of.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from reaper.config import DataDirError, get_settings
from reaper.db import schema_gate
from reaper.services import backup, restore


def _to_stderr(message: str) -> None:
    sys.stderr.write(message + "\n")


def main(refuse: Callable[[str], None] = _to_stderr) -> int:
    """Prepare the data folder, apply a staged restore, refuse a schema this build cannot
    serve, and snapshot the database when a pending migration asks for one. Zero means go.

    ``refuse`` carries the four fatal messages only: an unwritable data folder, a restore
    swap that could not complete, the schema gate's refusal, and a pre-migration snapshot
    that could not be written. This lets a caller that can reach the operator some other
    way handle them. That caller is the frozen desktop builds: Windows is windowed and
    macOS is `LSUIElement`, PyInstaller leaves the streams `None`, and
    `packaging/pyinstaller/entry.py` rebinds them to `os.devnull`, so a stderr-only
    refusal is written to the null device and a double-clicked Reaper that will not start
    closes with no window and no message. `launcher.main` passes `_say` instead.

    The housekeeping lines below stay on stderr and are not routed here, and the
    difference is the point: a swept temp directory or an unconfirmed restore that was
    cleared is a note about work that succeeded, and a dialog for it on every desktop
    start would train the operator to dismiss the one that matters.
    """
    try:
        settings = get_settings()
        settings.ensure_data_dir()
    except DataDirError as exc:
        # Just the message, no traceback. The operator needs the fix, not a stack.
        refuse(str(exc))
        return 1
    # Clear crash-leftover backup/restore temp dirs before anything else. Nothing is in
    # flight this early, and a stale multi-GB partial snapshot only makes a full disk
    # worse. A sweep failure is never fatal: a leftover temp is not a reason to refuse
    # boot.
    try:
        swept = backup.sweep_stale_temp(settings)
        if swept:
            sys.stderr.write(f"reaper: cleared {swept} leftover backup/restore temp entries\n")
    except Exception as exc:  # housekeeping must never stop boot
        sys.stderr.write(f"reaper: could not sweep leftover temp entries: {exc}\n")
    # And a staged restore nobody can reach any more. Its token only ever lived in the
    # browser that uploaded it, so one still here at boot can never be armed or canceled.
    # Left alone, it would be a whole database plus the key material that decrypts it,
    # kept forever. Unarmed only: an armed staging is what the next few lines are for,
    # and this runs before them so the ordering says which is which. Non-fatal for the
    # same reason the sweep above is.
    try:
        if restore.clear_unarmed_staging(settings):
            sys.stderr.write("reaper: cleared a staged restore that was never confirmed\n")
    except Exception as exc:  # housekeeping must never stop boot
        sys.stderr.write(f"reaper: could not clear the unconfirmed restore: {exc}\n")
    try:
        restore.apply_pending_restore(settings)
    except Exception as exc:  # any swap failure must stop boot, not serve a half-restore
        # A restore that cannot complete must not let the app boot on a half-swapped
        # state. The previous data is preserved in the pre-restore directory; stop the
        # container with a plain message rather than serving an uncertain database.
        refuse(f"reaper: the restore could not be completed: {exc}")
        return 1
    # Last, and after the swap above rather than before it: the database the migrations
    # are about to open is the restored one, and restoring a backup is one of the two
    # ways out this refusal names. Checking first would refuse the boot that was about
    # to fix itself.
    revision = schema_gate.stored_revision(settings.database_path)
    message = schema_gate.refusal(revision)
    if message:
        refuse(message)
        return 1
    # And after the refusal, on the same revision it just judged: a database this build
    # must not open is not one to spend minutes copying either. Nothing sits between here
    # and `alembic upgrade head` in any of the three launchers, so this is the last moment
    # a destructive revision can be given something to fall back to.
    #
    # `stored_revision` answers None for three different things, and only two of them are
    # "nothing to lose": no file, and no `alembic_version` row. The third is a database it
    # could not read, locked by a second instance on a shared data dir (a documented dev
    # shape), or damaged, and that is an ambiguity, not an answer. It reaches here rather
    # than being refused above because the gate's own reasoning does not carry: an
    # unreadable file is not a schema this build is behind. That reasoning does not carry
    # to this second question either. A lock released between this read and alembic's open
    # would let the one migration a `downgrade()` cannot undo run with nothing to fall
    # back to, so a file that exists and would not say what it is gets copied, and a copy
    # that also cannot be taken stops the boot.
    unreadable = revision is None and settings.database_path.is_file()
    if unreadable or schema_gate.needs_snapshot(revision):
        try:
            path = backup.snapshot_before_migration(settings, revision)
        except Exception as exc:
            # Fatal, unlike the two sweeps above. A migration that its own `downgrade()`
            # cannot undo must not run unprotected, so a full disk stops the boot instead.
            refuse(f"{backup.SNAPSHOT_FAILED}\n\nOriginal error: {exc}")
            return 1
        sys.stderr.write(
            f"reaper: saved a backup before updating the database: "
            f"data/{backup.PRE_MIGRATION_DIR}/{path.name}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
