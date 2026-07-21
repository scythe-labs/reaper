# SPDX-License-Identifier: AGPL-3.0-or-later

# ---- Stage 1: frontend -------------------------------------------------------
# Digest-pinned: the tag documents intent, the digest is what actually builds.
FROM node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2 AS frontend
WORKDIR /app/frontend

# Lockfile first, so a source-only change does not reinstall the dependency tree.
# `npm ci` installs exactly what is locked and fails if package.json disagrees with the
# lockfile -- a plain `npm install` would silently resolve something newer, which is not
# a thing you want happening unattended inside an image build.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# Runs `tsc --noEmit && vite build`, so a type error fails the image build rather than
# shipping a bundle that throws in the browser.
RUN npm run build

# ---- Stage 2: runtime --------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    REAPER_DATA_DIR=/data

WORKDIR /app

# Dependencies first, so a source-only change doesn't reinstall the world.
#
# We install from the committed lockfile (uv.lock), NOT the `>=` floors in pyproject.toml:
# `uv export --frozen` fails if the lock is stale and emits the exact, hash-pinned
# transitive set that CI tested, so two builds weeks apart ship the same tree. For a tool
# that stores full-power Plex credentials and issues delete requests, letting an
# unaudited `>=` resolution land in the image is a supply-chain exposure the lock exists
# to close. `--no-emit-project` excludes reaper itself here -- src/ isn't copied yet, and
# the project is installed editable (with --no-deps) below.
#
# README.md is copied in this same layer because pyproject declares `readme = "README.md"`;
# hatchling's metadata build (the editable install below) raises OSError without it.
COPY pyproject.toml uv.lock README.md ./
RUN uv export --frozen --no-emit-project -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt

COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY src/ ./src/
RUN uv pip install --system --no-cache --no-deps -e .

COPY --from=frontend /app/frontend/dist ./frontend/dist

# The app must not run as root: this container holds credentials that can delete a
# media library. But the data folder is a bind mount on most installs, and Docker
# creates a bind mount owned by root -- which a fixed-uid image cannot write. So the
# entrypoint starts as root, chowns /data to PUID:PGID (default 1000), and drops to
# that user with gosu BEFORE anything opens the database. The app process is never
# root, and /app stays root-owned so a compromised process cannot rewrite what it
# executes. gosu comes from Debian's signed repo (like the base image's own apt).
#
# There is no `USER` line on purpose: PID 1 is root only long enough to fix ownership
# and drop. If you would rather it never touch root, pin `user: "1000:1000"` in your
# compose and the entrypoint skips the root branch and runs in place.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && gosu nobody true \
    && useradd --system --uid 1000 --create-home reaper \
    && mkdir -p /data \
    && chown -R reaper:reaper /data

VOLUME ["/data"]
# The default port; REAPER_PORT below overrides the actual bind.
EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; port=os.environ.get('REAPER_PORT','8420'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=3).status==200 else 1)"

COPY docker-entrypoint.sh /usr/local/bin/
ENTRYPOINT ["docker-entrypoint.sh"]
# REAPER_HOST/REAPER_PORT are honored here (they also shape the recovery link the app
# prints), so .env.example tells the truth about what they do.
CMD ["sh", "-c", "exec uvicorn reaper.main:create_app --factory --host \"${REAPER_HOST:-0.0.0.0}\" --port \"${REAPER_PORT:-8420}\""]
