#!/usr/bin/env bash
#
# Install everything the verification gates need into a fresh checkout or worktree.
# Safe to re-run. Both installs read the committed lockfiles, so the tree stays clean.
#
# A worktree gets no .env.local of its own. scripts/dev-local.sh reads the main checkout's,
# because that file holds the key that decrypts the shared data/ directory.
set -euo pipefail
cd "$(dirname "$0")/.."

# Plain `uv sync` skips pytest, ruff and mypy: `dev` is an extra, not a dependency group.
uv sync --extra dev

# `npm install` rewrites package-lock.json under a different npm and dirties the tree.
npm --prefix frontend ci
