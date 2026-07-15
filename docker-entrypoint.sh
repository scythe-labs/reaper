#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
set -eu

# Migrations run before the app opens a connection. Failing here is correct:
# a half-migrated schema must never serve traffic for a tool that deletes media.
echo "reaper: applying database migrations"
alembic upgrade head

exec "$@"
