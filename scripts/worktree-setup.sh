#!/usr/bin/env bash
#
# Install everything the verification gates need into a fresh checkout or worktree.
# Safe to re-run. Both installs read the committed lockfiles, so the tree stays clean.
#
# A worktree gets no .env.local of its own. scripts/dev-local.sh reads the main checkout's,
# because that file holds the key that decrypts the shared data/ directory.
set -euo pipefail
cd "$(dirname "$0")/.."

# A node older than the .nvmrc pin installs cleanly and then fails silently later: an
# outdated jsdom/undici combo throws inside vitest's worker startup, vitest counts that as an
# "error" rather than a failed file, and the run reports a short file count with no file ever
# named as skipped. Catch the mismatch here instead, while it is still one line to explain.
pinned_node="$(cat .nvmrc)"
node_major="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
if [[ "$node_major" != "$pinned_node" ]]; then
    echo "error: node $(node --version) does not match the version pinned in .nvmrc (${pinned_node}.x.x)." >&2
    echo "Install the pinned major version (nvm: 'nvm install', from .nvmrc) and re-run." >&2
    exit 1
fi

# Plain `uv sync` skips pytest, ruff and mypy: `dev` is an extra, not a dependency group.
# `--frozen` installs what uv.lock pins. Without it, uv once resolved a newer ruff than CI runs.
uv sync --frozen --extra dev

# `npm install` rewrites package-lock.json under a different npm and dirties the tree.
npm --prefix frontend ci
