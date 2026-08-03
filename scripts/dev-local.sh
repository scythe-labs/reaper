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
# REAPER_DATA_DIR=/some/dir to point elsewhere (e.g. a disposable copy). The .env / .env.local
# beside THAT data dir are loaded and exported, because they carry the key it was encrypted
# under -- a worktree has neither of its own, and the wrong key loses credentials (#286).
#
# Usage:
#   scripts/dev-local.sh [up]     start both, wait for health, print URLs   (default)
#   scripts/dev-local.sh down     stop both and free the ports
#   scripts/dev-local.sh status   show whether each is listening
#   scripts/dev-local.sh logs     tail both logs (Ctrl-C to stop tailing)
#
# Serve another branch/worktree (flags go BEFORE the command), so an agent can boot a PR fast
# without cd-ing around -- the code comes from the target tree, the data dir from main:
#   scripts/dev-local.sh --branch <name> [up|down|...]   the worktree checked out on <name>
#   scripts/dev-local.sh --worktree <path> [up|down|...] that checkout directly
#
# Env overrides:
#   REAPER_DATA_DIR       data dir to serve (default: <main checkout>/data if it has a
#                         reaper.db, else <this tree>/data)
#   REAPER_PORT           API port    (default 8420); Vite's /api proxy target follows it
#   REAPER_WEB_PORT       Vite port   (default 5173)
#   REAPER_DEV_NO_MIGRATE 1 to skip `alembic upgrade head` (booting on a DB behind the
#                         branch head usually fails, because the models expect the new
#                         columns -- the upgrade is additive-only, so it is safe to run)
#
# Two instances side by side: give the second BOTH REAPER_PORT and REAPER_WEB_PORT -- they move
# together, because Vite's /api proxy target reads REAPER_PORT (see the note further down), so
# moving only the web port leaves the second UI talking to the first instance's API. Every stop
# this script performs is scoped to its own two ports, on `down` and on `up` alike, so a second
# instance cannot disturb a running first one. Its logs are its own too: .dev-logs holds one file
# per PORT, in the main checkout beside data/, so an instance booted from a worktree is still
# readable from here. `down` and `logs` therefore need the SAME two ports the `up` had, or they
# reach the default instance instead; a successful `up` prints the exact spelling to use.
# One thing IS shared on purpose: the data dir, so both instances serve the same real DB.
#
set -euo pipefail

log()  { printf '\033[36m[dev]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[dev]\033[0m %s\n' "$*"; }

# --- locate the tree and the real data dir --------------------------------------------------
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# --- optional: serve a DIFFERENT worktree/branch (so an agent can test a PR fast) -----------
# `--worktree <path>` boots that checkout's code; `--branch <name>` finds the worktree already
# checked out on that branch. Either re-execs this script FROM the target tree, so its code is
# what runs -- while the data dir still resolves to the main checkout's data/ (below) and the
# standard ports still apply. Flags come before the command:  dev-local.sh --branch X up
TARGET_TREE=""
while [ $# -gt 0 ]; do
  case "${1:-}" in
    -w|--worktree) TARGET_TREE="${2:?--worktree needs a path}"; shift 2 ;;
    -b|--branch)
      want="${2:?--branch needs a name}"; shift 2
      # The worktree whose checked-out branch is <want>, from `git worktree list` (porcelain:
      # a "worktree <path>" line followed by "branch refs/heads/<name>").
      TARGET_TREE="$(git worktree list --porcelain \
        | awk -v b="branch refs/heads/$want" '/^worktree /{w=$2} $0==b{print w; exit}')"
      [ -n "$TARGET_TREE" ] || {
        warn "no worktree is checked out on branch '$want'"
        warn "create one first:  git worktree add .claude/worktrees/$want $want"
        exit 2
      }
      ;;
    *) break ;;
  esac
done
# REAPER_DEV_REEXEC guards against a re-exec loop; the target run has no flags left anyway.
if [ -n "$TARGET_TREE" ] && [ -z "${REAPER_DEV_REEXEC:-}" ]; then
  TARGET_TREE="$(cd "$TARGET_TREE" 2>/dev/null && pwd)" \
    || { warn "worktree path does not exist"; exit 2; }
  target_script="$TARGET_TREE/scripts/dev-local.sh"
  [ -x "$target_script" ] || { warn "no runnable scripts/dev-local.sh in $TARGET_TREE"; exit 2; }
  log "serving worktree: $TARGET_TREE"
  exec env REAPER_DEV_REEXEC=1 "$target_script" "$@"
fi

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

# The dotenv file follows the DATA dir, never the code tree, because what it carries is the key
# that decrypts that data. `src/reaper/config.py` resolves `env_file=(".env", ".env.local")`
# against the process cwd, and both are gitignored -- so a worktree has neither, REAPER_SECRET_KEY
# goes unset, and `secrets.resolve_secret_key` finds a real `data/secret.key` and returns it: a
# DIFFERENT key from the one that database was encrypted under. Nothing warns, because a genuinely
# missing key is a first run and this looks like neither. Every scan then aborts on a stored
# credential, and the natural repair -- re-entering it in the UI -- encrypts under the wrong key
# and overwrites the good ciphertext, so the credentials are lost for the main checkout too (#286).
#
# Exporting is what makes it stick: a real environment variable beats a dotenv file in
# pydantic-settings, whichever tree uvicorn is launched from, and `.env.local` last matches the
# precedence that tuple already has. It reaches `config.load_raw_env` too, whose instance seeds
# read the same two files by relative path for the same reason.
ENV_ROOT="$(cd "$DATA_DIR/.." 2>/dev/null && pwd || echo "$REPO")"
ENV_LOADED=""
for env_file in "$ENV_ROOT/.env" "$ENV_ROOT/.env.local"; do
  [ -f "$env_file" ] || continue
  set -a
  # shellcheck disable=SC1090  # the developer's own dotenv; the path is only known at runtime
  . "$env_file"
  set +a
  ENV_LOADED="$ENV_LOADED${ENV_LOADED:+, }$(basename "$env_file")"
done

API_PORT_DEFAULT=8420
WEB_PORT_DEFAULT=5173
API_PORT="${REAPER_PORT:-$API_PORT_DEFAULT}"
WEB_PORT="${REAPER_WEB_PORT:-$WEB_PORT_DEFAULT}"

# A log belongs to the INSTANCE, and what identifies an instance is its PORT -- not the tree it
# was booted from. So both halves of the path follow the port: the files are named for it, and
# they sit in the main checkout, which is where data/ already resolves to and the scope `down`
# and `status` already act at, since lsof reaches a port whichever tree started it.
#
# Keyed to the tree, both halves failed, and quietly. One directory per tree meant two instances
# from one checkout shared one pair of files, and `nohup ... > "$API_LOG"` truncates on open: the
# second one's start emptied the log the first was still writing to, while the first's uvicorn
# held its file offset across that truncation and went on appending at a stale one. The other
# half is `--branch`/`--worktree`, which re-execs from the TARGET tree: logs landed over there
# while `down` and `logs` for those same ports were run from here, so `logs` reported nothing
# running for a live instance -- or tailed an identically named file left by an earlier run and
# presented it as that instance's current output.
LOG_DIR="$MAIN_ROOT/.dev-logs"
API_LOG="$LOG_DIR/api-$API_PORT.log"
WEB_LOG="$LOG_DIR/web-$WEB_PORT.log"

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
  # One process is left over, and it is not the one you would guess: --reload binds the socket
  # in the reloader parent and hands it to a `multiprocessing.spawn` child, so lsof reports
  # BOTH of those and the loop above already has them. What it cannot see is the `uv run`
  # wrapper, which holds no socket. Hence a pattern match -- SCOPED TO THIS PORT, which the
  # wrapper's own argv carries. Unscoped, the pattern matched every Reaper API on the machine,
  # and since `up` calls stop_all unconditionally (below), starting a second instance killed a
  # first one's API before printing a line, while its Vite kept serving and every request in
  # the browser failed against a backend that was gone. The digit class is load-bearing: a bare
  # `--port 6553` is a substring of `--port 65535`.
  pkill -f "uvicorn reaper.main:create_app.*--port $API_PORT([^0-9]|$)" 2>/dev/null || true
  # TERM is a request, and a wedged reload supervisor declines it: three polite downs
  # in a row once left the same PID on the port, each printing "stopping" and reading
  # as success. So the claim is checked against the port, and a survivor is forced.
  local waited=0
  while [ "$waited" -lt 6 ]; do
    local left=""
    for p in "$API_PORT" "$WEB_PORT"; do left="$left$(port_pids "$p")"; done
    [ -n "$left" ] || break
    sleep 0.5; waited=$((waited + 1))
  done
  for p in "$API_PORT" "$WEB_PORT"; do
    local pids; pids="$(port_pids "$p")"
    if [ -n "$pids" ]; then
      warn "still holding :$p ($pids); forcing"
      kill -9 $pids 2>/dev/null || true
    fi
  done
  if pgrep -f "uvicorn reaper.main:create_app.*--port $API_PORT([^0-9]|$)" > /dev/null 2>&1; then
    warn "the uv run wrapper declined to exit; forcing"
    pkill -9 -f "uvicorn reaper.main:create_app.*--port $API_PORT([^0-9]|$)" 2>/dev/null || true
  fi
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
    # Keying the files to the port moves the ambiguity from write time to read time unless this
    # says which instance it looked for: `logs` without the env vars a second instance was
    # started with reads the DEFAULT pair, and a bare "no logs yet" would report that as
    # "nothing is running" while the instance you meant streams on untouched.
    [ -f "$API_LOG" ] || {
      warn "no logs for API :$API_PORT / web :$WEB_PORT -- run 'up' first, or set"
      warn "REAPER_PORT and REAPER_WEB_PORT to the instance you meant"
      # `|| true` is load-bearing under `set -euo pipefail`: with no log dir yet, `ls` fails,
      # pipefail hands that status to the assignment and -e exits RIGHT HERE, skipping the
      # `exit 1` below. It read as correct only because macOS `ls` happens to return 1.
      have="$(ls "$LOG_DIR" 2>/dev/null | sed -n 's/^api-\(.*\)\.log$/\1/p' | tr '\n' ' ')" || true
      [ -n "$have" ] && warn "logs on disk for API port(s): $have"
      exit 1
    }
    tail -n 40 -f "$API_LOG" "$WEB_LOG" ;;
  up|"") : ;;  # fall through
  # Print the header comment however long it grows: a hardcoded line range truncates the help
  # mid-sentence the first time anything above `set -euo pipefail` is edited, and says nothing.
  *) warn "unknown command: $cmd"; awk 'NR>1 && !/^#/{exit} NR>1' "${BASH_SOURCE[0]}"; exit 2 ;;
esac

# --- up ------------------------------------------------------------------------------------
mkdir -p "$LOG_DIR"

# If both are already answering, don't stack a second copy. BOUNDED, because a bound port is not
# a live server: under --reload the reloader PARENT holds the listening socket and hands accepted
# connections to a worker, so a worker that exited leaves the port bound and every connection
# accepted by the kernel and then answered by nobody. An untimed curl there does not fail, it waits
# forever -- so `up` hung on its own liveness probe rather than clearing the corpse and booting.
#
# That state is now reachable from the UI: `Restart now` on an armed restore stops the worker, and
# in dev nothing supervises it, so the very next `up` is the one that has to survive this. Five
# seconds is far past a local health read and far short of noticing a hang.
if [ -n "$(port_pids "$API_PORT")" ] && [ -n "$(port_pids "$WEB_PORT")" ] \
   && curl -sS -m 5 -o /dev/null "http://127.0.0.1:$API_PORT/api/health" 2>/dev/null; then
  log "already up -- API :$API_PORT, frontend :$WEB_PORT (use 'down' to restart)"
  exit 0
fi
stop_all  # clear any half-up state / stale listeners

log "data dir: $DATA_DIR"
if [ -n "$ENV_LOADED" ]; then
  log "env: $ENV_LOADED (from $ENV_ROOT, beside the data dir)"
elif [ -f "$DATA_DIR/reaper.db" ] && [ -z "${REAPER_SECRET_KEY:-}" ]; then
  # No dotenv beside a database that already exists. Normal on an install keyed by
  # data/secret.key; the one thing it must not do is pass silently (#286).
  warn "no .env beside $DATA_DIR -- if that DB was encrypted under REAPER_SECRET_KEY,"
  warn "stored credentials will not decrypt. Do NOT re-enter them: that overwrites the good"
  warn "ciphertext under the wrong key. Point REAPER_DATA_DIR elsewhere, or restore the .env."
fi

# Preflight first, in the order docker-entrypoint.sh runs it: before migrations, because the
# restore swap has to happen before `alembic upgrade head` brings the restored database current.
# It is the only caller of `restore.apply_pending_restore`, so without it a confirmed restore is
# never applied here -- the staged files sat in data/pending-restore/ across every restart while
# the UI went on saying "restart to finish", and nothing in the log said otherwise (#381). It
# also sweeps crash-leftover backup/restore temp dirs and turns an unwritable data dir into a
# plain line instead of SQLite's "unable to open database file" under a driver traceback.
#
# Runs whatever REAPER_DEV_NO_MIGRATE says: that switch is about the schema, and a staged restore
# and an unwritable data dir are neither.
log "preflight (applies a staged restore, checks the data dir)"
if ! REAPER_DATA_DIR="$DATA_DIR" uv run python -m reaper.preflight; then
  # Preflight returns 1 only where booting anyway would be worse than not booting: a restore
  # that could not complete must not serve a half-swapped database. Its own message above says
  # what happened, so this adds only the consequence.
  warn "preflight failed -- not starting. The line above says what to fix."
  exit 1
fi

if [ "${REAPER_DEV_NO_MIGRATE:-}" = 1 ]; then
  warn "skipping migrations (REAPER_DEV_NO_MIGRATE=1); boot may fail if the DB is behind head"
else
  log "applying migrations (additive-only) -> alembic upgrade head"
  REAPER_DATA_DIR="$DATA_DIR" uv run alembic upgrade head
fi

log "starting API (uvicorn --reload) on :$API_PORT"
# --no-proxy-headers matches the shipped CMD, and matters most here: a dev API is reached
# over loopback, which is exactly the peer uvicorn trusts by default, so without it every
# forwarded header a request carries is believed and dev stops behaving like production.
REAPER_DATA_DIR="$DATA_DIR" REAPER_SERVE_SPA=false \
  nohup uv run uvicorn reaper.main:create_app --factory --no-proxy-headers --reload --port "$API_PORT" \
  > "$API_LOG" 2>&1 &

log "starting frontend (Vite HMR) on :$WEB_PORT"
# REAPER_PORT reaches Vite too, because its /api proxy target has to follow the API it was
# just told to start (frontend/vite.config.ts reads it). Passing only --port moved the UI
# without moving what it talks to: on any non-default REAPER_PORT every /api call answered
# 502 and the UI looked like a crashed backend. Both halves move together or neither does.
REAPER_PORT="$API_PORT" \
  nohup npm --prefix frontend run dev -- --port "$WEB_PORT" --strictPort \
  > "$WEB_LOG" 2>&1 &

wait_ready "http://127.0.0.1:$API_PORT/api/health" "API"
wait_ready "http://localhost:$WEB_PORT/" "frontend"

# `logs` and `down` reach the instance whose ports they carry, so a second instance has to be
# told back in the spelling that reaches IT -- the bare command sends the reader to the default
# instance, which is the same wrong-instance mistake one layer up. Empty for the default pair.
ENVPFX=""
[ "$API_PORT" = "$API_PORT_DEFAULT" ] && [ "$WEB_PORT" = "$WEB_PORT_DEFAULT" ] \
  || ENVPFX="REAPER_PORT=$API_PORT REAPER_WEB_PORT=$WEB_PORT "

cat <<EOF

  Reaper dev is up:
    UI        http://localhost:$WEB_PORT     (open this)
    API       http://localhost:$API_PORT/api
    logs      ${ENVPFX}scripts/dev-local.sh logs   (or tail $API_LOG $WEB_LOG)
    stop      ${ENVPFX}scripts/dev-local.sh down

  Both auto-update: backend edits reload the API, frontend edits hot-swap in the browser.
  Log in with your normal account, or mint a throwaway local admin (prints a one-time
  password): uv run reaper-admin create-admin --username local-test
EOF
