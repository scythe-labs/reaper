#!/usr/bin/env bash
#
# Boot Reaper's dev servers so an agent (or a person) can drive the real UI end to end.
#
# Starts two auto-reloading processes in the background and returns once both answer:
#   * API      -> uvicorn --reload on :8420 (backend .py edits restart the app)
#   * frontend -> Vite dev + HMR on :5173 (frontend edits hot-swap live, no rebuild)
# The UI at :5173 is the live dev server, NOT a static build; `npm run build` only writes
# frontend/dist and is a CI gate, never what you serve here.
#
# Why a script: the harness `preview_start` (see .claude/launch.json) is only available in an
# interactive Claude Code session. In a background/headless job it is not, so the sanctioned
# way to boot there is exactly this -- launch both with the right data dir and wait for ready.
#
# Data: by default the shared real DB in the MAIN checkout's data/ (derived from git, so no
# path is baked in), which is what gives you real review-queue cards. Override with
# REAPER_DATA_DIR=/some/dir to point elsewhere (e.g. a disposable copy).
#
# Usage:
#   scripts/dev-local.sh [up]     start both, wait for health, print URLs   (default)
#   scripts/dev-local.sh down     stop both and free the ports
#   scripts/dev-local.sh status   show whether each is listening
#   scripts/dev-local.sh logs     tail both logs (Ctrl-C to stop tailing)
#
# Env overrides:
#   REAPER_DATA_DIR       data dir to serve (default: <main checkout>/data if it has a
#                         reaper.db, else <this tree>/data)
#   REAPER_PORT           API port    (default 8420)
#   REAPER_WEB_PORT       Vite port   (default 5173)
#   REAPER_DEV_NO_MIGRATE 1 to skip `alembic upgrade head` (booting on a DB behind the
#                         branch head usually fails, because the models expect the new
#                         columns -- the upgrade is additive-only, so it is safe to run)
#
set -euo pipefail

# --- locate the tree and the real data dir --------------------------------------------------
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# The main checkout owns data/, shared by every worktree. Derive it from git rather than
# hardcoding an absolute path (golden rule: no identifying paths in committed files).
common_git="$(git rev-parse --git-common-dir)"
case "$common_git" in
  /*) ;;                              # already absolute
  *)  common_git="$REPO/$common_git" ;;
esac
MAIN_ROOT="$(cd "$(dirname "$common_git")" && pwd)"

if [ -n "${REAPER_DATA_DIR:-}" ]; then
  DATA_DIR="$REAPER_DATA_DIR"
elif [ -f "$MAIN_ROOT/data/reaper.db" ]; then
  DATA_DIR="$MAIN_ROOT/data"
else
  DATA_DIR="$REPO/data"
fi

API_PORT="${REAPER_PORT:-8420}"
WEB_PORT="${REAPER_WEB_PORT:-5173}"
LOG_DIR="$REPO/.dev-logs"
API_LOG="$LOG_DIR/api.log"
WEB_LOG="$LOG_DIR/web.log"

log()  { printf '\033[36m[dev]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[dev]\033[0m %s\n' "$*"; }

port_pids() { lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' | sed 's/ *$//' || true; }

wait_ready() { # url label
  local url="$1" label="$2"
  # -s (not -sS): --retry-connrefused retries a not-yet-bound port, and -S would print each
  # transient "connection refused" before it finally answers. A real failure still returns >0.
  if curl -s --retry 60 --retry-delay 1 --retry-connrefused -o /dev/null "$url"; then
    log "$label ready -> $url"
  else
    warn "$label did not come up; see its log"
    return 1
  fi
}

stop_all() {
  local killed=0
  for p in "$API_PORT" "$WEB_PORT"; do
    local pids; pids="$(port_pids "$p")"
    if [ -n "$pids" ]; then log "stopping :$p ($pids)"; kill $pids 2>/dev/null || true; killed=1; fi
  done
  # uvicorn's --reload spawns a child; make sure the reloader parent is gone too.
  pkill -f "uvicorn reaper.main:create_app" 2>/dev/null || true
  [ "$killed" = 1 ] || log "nothing was running"
}

cmd="${1:-up}"
case "$cmd" in
  down|stop) stop_all; exit 0 ;;
  status)
    for pair in "API $API_PORT" "web $WEB_PORT"; do
      set -- $pair
      pids="$(port_pids "$2")"
      if [ -n "$pids" ]; then log "$1 :$2 listening ($pids)"; else warn "$1 :$2 not running"; fi
    done
    exit 0 ;;
  logs)
    [ -f "$API_LOG" ] || { warn "no logs yet; run 'up' first"; exit 1; }
    tail -n 40 -f "$API_LOG" "$WEB_LOG" ;;
  up|"") : ;;  # fall through
  *) warn "unknown command: $cmd"; sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 2 ;;
esac

# --- up ------------------------------------------------------------------------------------
mkdir -p "$LOG_DIR"

# If both are already answering, don't stack a second copy.
if [ -n "$(port_pids "$API_PORT")" ] && [ -n "$(port_pids "$WEB_PORT")" ] \
   && curl -sS -o /dev/null "http://127.0.0.1:$API_PORT/api/health" 2>/dev/null; then
  log "already up -- API :$API_PORT, frontend :$WEB_PORT (use 'down' to restart)"
  exit 0
fi
stop_all  # clear any half-up state / stale listeners

log "data dir: $DATA_DIR"

if [ "${REAPER_DEV_NO_MIGRATE:-}" = 1 ]; then
  warn "skipping migrations (REAPER_DEV_NO_MIGRATE=1); boot may fail if the DB is behind head"
else
  log "applying migrations (additive-only) -> alembic upgrade head"
  REAPER_DATA_DIR="$DATA_DIR" uv run alembic upgrade head
fi

log "starting API (uvicorn --reload) on :$API_PORT"
REAPER_DATA_DIR="$DATA_DIR" REAPER_SERVE_SPA=false \
  nohup uv run uvicorn reaper.main:create_app --factory --reload --port "$API_PORT" \
  > "$API_LOG" 2>&1 &

log "starting frontend (Vite HMR) on :$WEB_PORT"
nohup npm --prefix frontend run dev -- --port "$WEB_PORT" --strictPort \
  > "$WEB_LOG" 2>&1 &

wait_ready "http://127.0.0.1:$API_PORT/api/health" "API"
wait_ready "http://localhost:$WEB_PORT/" "frontend"

cat <<EOF

  Reaper dev is up:
    UI        http://localhost:$WEB_PORT     (open this)
    API       http://localhost:$API_PORT/api
    logs      scripts/dev-local.sh logs   (or tail $LOG_DIR/*.log)
    stop      scripts/dev-local.sh down

  Both auto-update: backend edits reload the API, frontend edits hot-swap in the browser.
  Log in with your normal account, or mint a throwaway local admin (prints a one-time
  password): uv run reaper-admin create-admin --username local-test
EOF
