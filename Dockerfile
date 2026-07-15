# SPDX-License-Identifier: AGPL-3.0-or-later

# ---- Stage 1: frontend -------------------------------------------------------
FROM node:22-alpine AS frontend
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
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS runtime

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

# Never run as root: this container holds credentials that can delete a media library.
RUN useradd --system --uid 1000 --create-home reaper \
    && mkdir -p /data \
    && chown -R reaper:reaper /data /app
USER reaper

VOLUME ["/data"]
EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8420/api/health', timeout=3).status==200 else 1)"

COPY docker-entrypoint.sh /usr/local/bin/
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "reaper.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8420"]
