#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
set -eu

# When started as root (the image default), fix the ownership of the data folder
# and immediately drop to an unprivileged user. This is the ONLY root-privileged
# step: it runs before anything opens the database, then re-execs this same script
# as the target user via gosu, so the migration and the app never run as root.
#
# Why this exists: the data folder is a bind mount on most installs, and Docker
# creates a bind mount owned by root (on Unraid, nobody:users). A fixed-uid image
# could not write it, and the operator had to chown it by hand first. Chowning it
# here needs CAP_CHOWN, which only root has -- hence the brief root phase.
#
# PUID/PGID choose that user and default to 1000, the uid the image is built around,
# so the default install is unchanged. Unraid users can set 99:100 to keep their
# appdata owned by nobody:users. If the container is instead started as a non-root
# user (a pinned `user:` in compose), we skip this entirely and run in place -- the
# preflight below still fails clearly if that user cannot write the folder.
#
# REAPER_ENTRYPOINT_DROPPED guards against a re-exec loop when PUID happens to be 0.
if [ "$(id -u)" = "0" ] && [ -z "${REAPER_ENTRYPOINT_DROPPED:-}" ]; then
  PUID="${PUID:-1000}"
  PGID="${PGID:-1000}"
  # Best effort: if a file cannot be chowned, the preflight will still catch a
  # genuinely unwritable folder and print the actionable message.
  chown -R "${PUID}:${PGID}" /data 2>/dev/null || true
  export REAPER_ENTRYPOINT_DROPPED=1
  exec gosu "${PUID}:${PGID}" "$0" "$@"
fi

# Verify the data folder is writable before anything opens the database, so an
# unwritable mount fails with a plain message naming the fix, not SQLite's opaque
# "unable to open database file" traceback.
python -m reaper.preflight

# Migrations run before the app opens a connection. Failing here is correct:
# a half-migrated schema must never serve traffic for a tool that deletes media.
echo "reaper: applying database migrations"
alembic upgrade head

exec "$@"
