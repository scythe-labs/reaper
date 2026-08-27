#!/usr/bin/env bash
#
# Run a published Reaper image on a docker host, so a pull request can be tried in a
# browser without editing a compose file for every change.
#
# CI publishes an image for every push. A pull request is tagged "pr-<number>", the dev
# branch is "dev", main is "latest", and every build is also tagged with its short commit.
# This script pulls one of those, runs it under a name of its own, and tears it down again.
#
# Several instances run side by side: each one is a container, a data volume, and a host
# port that belong to a single instance name, and every command is scoped to that name.
# So a second pull request cannot disturb the first, and neither can touch the production
# container this host may also be running.
#
# Usage:
#   scripts/try-image.sh up --pr 400 --port 8421      pull, run, wait for health, print URL
#   scripts/try-image.sh up --tag dev --port 8422     the dev branch instead
#   scripts/try-image.sh list                         every instance this script started
#   scripts/try-image.sh logs pr-400 [-f]             its container logs
#   scripts/try-image.sh down pr-400                  stop and remove it, keep its data
#   scripts/try-image.sh down pr-400 --purge          and delete its data volume
#   scripts/try-image.sh clean [--purge]              the same, for every instance at once
#
# Which image (pick one; --pr is the usual one):
#   --pr <number>     the image built for that pull request  (tag "pr-<number>")
#   --tag <tag>       any tag: dev, latest, or a short commit
#   --image <ref>     a full image reference, for a fork or a local build
#
# Where its data comes from (--data, and the default is right for most tries):
#   (omitted)         a volume of this instance's own, kept between up and down, so
#                     re-running `up` on a rebuilt image keeps the setup you entered
#   new               the same, emptied first: a first-run install with nothing in it
#   copy:<volume>     a COPY of another instance's data, so you can try a pull request
#   copy:<path>       against a real setup while the original stays untouched
#   <volume>          an existing docker volume, used in place
#   <path>            a host directory, used in place  (anything with a "/" in it)
#
# Copying is the one to reach for when the change is only visible against real data.
# Using a real volume or directory in place is the risky one, and the script says so:
# Reaper applies its database migrations on every boot, so a pull request that adds one
# upgrades that database, and the older code you go back to afterwards may not read it.
# Migrations only ever add, so nothing is lost, but the copy costs seconds and the
# original then cannot be touched at all.
#
# Other options for `up`:
#   --name <name>     name this instance yourself (default: the tag, e.g. "pr-400").
#                     Two instances of one pull request, on different data, want this.
#   --port <port>     host port to publish on (default 8420). "auto" picks a free one.
#                     This is the host side only, and it is all you normally set. The
#                     port inside the container follows REAPER_PORT when you pass one,
#                     and the publish mapping follows it too.
#   --env K=V         an environment variable for the container; repeatable
#   --env-file <path> a file of them, in docker's --env-file format
#   --no-pull         skip the registry pull and use the image already on this host
#
# Environment:
#   REAPER_IMAGE_REPO   the image path to pull tags from
#                       (default ghcr.io/scythe-labs/reaper; set it for a fork)
#
if [ -z "${BASH_VERSION:-}" ]; then
  # `sh try-image.sh` is the reflex on a server, and it bypasses the shebang above: on
  # Debian /bin/sh is dash, which has no arrays, so the first array assignment below
  # would be a parse error reported tens of lines away from anything the operator typed.
  # Re-exec under bash rather than explain that. This block stays POSIX so dash can read
  # it, and stays ahead of `set -o pipefail`, which dash only learned in 0.5.12.
  #
  # It also has to sit below the help text, which is read off the top of this file.
  if command -v bash > /dev/null 2>&1; then
    exec bash "$0" "$@"
  fi
  echo "try-image.sh needs bash, and this host does not have it. Install bash and try again." >&2
  exit 1
fi

set -euo pipefail

IMAGE_REPO="${REAPER_IMAGE_REPO:-ghcr.io/scythe-labs/reaper}"

# Every container and volume this script creates carries these, so `list` and `clean` can
# find them again without keeping a state file, and can never match anything else on the
# host, including a production Reaper container started from a compose file.
LABEL_MARK="net.scythe-labs.reaper.try"
PREFIX="reaper-try"

# What `up` was asked to pass into the container. Global because the port the container
# listens on is read back out of them, below.
ENVS=()
ENVFILES=()

log()  { printf '\033[36m[try]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[try]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[try]\033[0m %s\n' "$*" >&2; exit 1; }

# The help text is the header comment: everything from line 2 down to the first line that
# is not a comment, which keeps it next to the flags it documents. Anchored on "not a
# comment" rather than on whatever statement happens to come first, so moving that
# statement cannot quietly truncate the help or spill code into it.
usage() { sed -n '2,/^[^#]/p' "$0" | sed 's/^# \{0,1\}//; $d'; }

# --- docker availability ---------------------------------------------------------------
# Checked once, up front, so every later failure is about Reaper and not about docker.
require_docker() {
  command -v docker >/dev/null 2>&1 || die "docker is not installed, or not on PATH."
  if ! docker info >/dev/null 2>&1; then
    die "cannot reach the docker daemon. Start it, or run this with sudo if your user is not in the docker group."
  fi
}

container_of() { printf '%s-%s' "$PREFIX" "$1"; }
volume_of()    { printf '%s-%s' "$PREFIX" "$1"; }

# The port the app serves on inside the container. It is 8420 unless the operator sets
# REAPER_PORT, which the image's CMD passes to uvicorn and its healthcheck reads. So
# publishing to a fixed 8420 would hand back a URL nothing is listening on, while the
# container still reports itself healthy on the port it actually moved to.
#
# Reads the same sources docker does, in docker's own precedence: every --env-file in
# order, then every --env, last one winning.
container_port_from_env() {
  local port=8420 f v line
  for f in ${ENVFILES[@]+"${ENVFILES[@]}"}; do
    [ -f "$f" ] || continue
    while IFS= read -r line; do
      case "$line" in
        REAPER_PORT=*) v="${line#REAPER_PORT=}"; [ -n "$v" ] && port="$v" ;;
      esac
    done < "$f"
  done
  for v in ${ENVS[@]+"${ENVS[@]}"}; do
    case "$v" in
      REAPER_PORT=*) port="${v#REAPER_PORT=}" ;;
    esac
  done
  printf '%s' "$port"
}

exists()        { docker container inspect "$1" >/dev/null 2>&1; }
volume_exists() { docker volume inspect "$1" >/dev/null 2>&1; }

# A volume this script created, and may therefore delete. A volume the operator named
# themselves is never removed by `down --purge`, because it is the only copy of data that
# was not ours to make.
is_managed_volume() {
  [ "$(docker volume inspect --format '{{if .Labels}}{{index .Labels "'"$LABEL_MARK"'"}}{{end}}' "$1" 2>/dev/null)" = "1" ]
}

# The instance name each container was started under. Read from its label rather than
# from its container name, so the two can never drift apart.
label_name_of() {
  docker inspect --format '{{if .Config.Labels}}{{index .Config.Labels "'"$LABEL_MARK"'.name"}}{{end}}' "$1" 2>/dev/null
}

# `up --pr 400` names the instance "pr-400", but the number is what you remember, so
# `down 400` finds it too. An exact match always wins, so an instance genuinely named
# "400" is never mistaken for "pr-400".
#
# The volume counts as a match, not only the container: `down 400` leaves the volume
# behind by design, so by the time you come back to `down 400 --purge` there is no
# container left to resolve the name from, and matching on the container alone would
# report success while leaving the data it was asked to delete.
resolve_name() {
  local given="$1"
  if exists "$(container_of "$given")" || volume_exists "$(volume_of "$given")"; then
    printf '%s' "$given"
  elif exists "$(container_of "pr-$given")" || volume_exists "$(volume_of "pr-$given")"; then
    printf 'pr-%s' "$given"
  else
    printf '%s' "$given"
  fi
}

# ========================================================================================
# up
# ========================================================================================
cmd_up() {
  local pr="" tag="" image="" name="" port="8420" data="" pull=1
  ENVS=(); ENVFILES=()

  while [ $# -gt 0 ]; do
    case "$1" in
      --pr)       pr="${2:?--pr needs a pull request number}"; shift 2 ;;
      --tag)      tag="${2:?--tag needs a tag}"; shift 2 ;;
      --image)    image="${2:?--image needs an image reference}"; shift 2 ;;
      --name)     name="${2:?--name needs a name}"; shift 2 ;;
      --port)     port="${2:?--port needs a port or \"auto\"}"; shift 2 ;;
      --data)     data="${2:?--data needs a value}"; shift 2 ;;
      --env)      ENVS+=("${2:?--env needs KEY=VALUE}"); shift 2 ;;
      --env-file) ENVFILES+=("${2:?--env-file needs a path}"); shift 2 ;;
      --no-pull)  pull=0; shift ;;
      -h|--help)  usage; return 0 ;;
      *)          die "unknown option for up: $1" ;;
    esac
  done

  # --- resolve which image ---
  local chosen=0
  [ -n "$pr" ] && chosen=$((chosen + 1))
  [ -n "$tag" ] && chosen=$((chosen + 1))
  [ -n "$image" ] && chosen=$((chosen + 1))
  [ "$chosen" -le 1 ] || die "pick one of --pr, --tag or --image, not several."
  [ "$chosen" -eq 1 ] || die "say which image to run: --pr <number>, --tag <tag>, or --image <ref>."

  if [ -n "$pr" ]; then
    case "$pr" in
      ''|*[!0-9]*) die "--pr takes a pull request number, for example: --pr 400" ;;
    esac
    tag="pr-${pr}"
  fi
  if [ -z "$image" ]; then
    image="${IMAGE_REPO}:${tag}"
  fi

  # Default instance name: the tag, which reads as what it is ("pr-400"). A full --image
  # reference is reduced to its tag, or to the last path segment when it carries none.
  if [ -z "$name" ]; then
    if [ -n "$tag" ]; then
      name="$tag"
    else
      local last="${image##*/}"          # "reaper:pr-400", or "reaper" when untagged
      case "$last" in
        *:*) name="${last##*:}" ;;
        *)   name="$last" ;;
      esac
    fi
  fi
  # The name reaches a container name, a volume name and a label, so hold it to what all
  # three accept rather than discovering the difference halfway through a boot.
  case "$name" in
    ''|*[!a-zA-Z0-9._-]*) die "instance name may use letters, digits, dot, dash and underscore only: $name" ;;
  esac

  local container volume
  container="$(container_of "$name")"
  volume="$(volume_of "$name")"

  require_docker

  # --- resolve the port ---
  if [ "$port" = "auto" ]; then
    port=0                                  # docker picks a free one, read back after boot
  else
    case "$port" in
      ''|*[!0-9]*) die "--port takes a port number or \"auto\": $port" ;;
    esac
  fi

  # --- pull ---
  # A failed pull falls back to a copy already on this host, because an image built here
  # (`--image`, and `docker build` never pushes anywhere) has nothing to pull from. The
  # fallback is loud and dates the copy: testing a pull request against yesterday's build
  # of it reads as "the fix did not work", which is the worst answer this script could
  # give quietly. Nothing is assumed about which case you are in. Both get the date.
  if [ "$pull" -eq 1 ]; then
    log "pulling $image"
    local pulled=1
    docker pull "$image" || pulled=0

    if ! docker image inspect "$image" >/dev/null 2>&1; then
      warn "could not pull $image, and there is no copy of it on this host."
      warn "If the registry needs a login:  docker login ghcr.io"
      [ -n "$pr" ] && warn "If the pull request is new, its image may still be building. Check its CI run."
      die "no image to run."
    fi

    if [ "$pulled" -eq 0 ]; then
      local built
      built="$(docker image inspect --format '{{.Created}}' "$image" 2>/dev/null)"
      warn "the pull failed, so this is the copy already on this host, from ${built%%T*}."
      warn "That is what you want for an image you built here. For a pull request it may"
      warn "be an older build of it, which would read as a fix that did not work."
    fi
  else
    docker image inspect "$image" >/dev/null 2>&1 \
      || die "$image is not on this host, and --no-pull was given."
  fi

  # --- resolve the data source into one docker -v argument ---
  # The case below sets all three: MOUNT_SRC is what goes left of the colon, DATA_NOTE
  # describes it for the label and the summary line, and RISKY marks a source used in place.
  local MOUNT_SRC DATA_NOTE RISKY=0

  case "${data:-}" in
    "")
      MOUNT_SRC="$volume"; DATA_NOTE="own volume ($volume)"
      ;;
    new)
      if volume_exists "$volume"; then
        log "emptying $volume"
        exists "$container" && docker rm -f "$container" >/dev/null
        docker volume rm "$volume" >/dev/null
      fi
      MOUNT_SRC="$volume"; DATA_NOTE="own volume, emptied ($volume)"
      ;;
    copy:*)
      local src="${data#copy:}"
      [ -n "$src" ] && [ "$src" != "$volume" ] || die "--data copy: needs a source that is not this instance's own volume."
      MOUNT_SRC="$volume"; DATA_NOTE="copy of $src"
      # The copy happens once, when the volume is made. A second `up` on the same instance
      # keeps whatever the first one has become, which is what you want while iterating on
      # a rebuilt image. It is not a fresh copy though, and reading it as one would mean
      # judging a pull request against state an earlier run left behind.
      if volume_exists "$volume"; then
        DATA_NOTE="earlier copy of $src"
        log "keeping the copy already in $volume, not copying $src again."
        log "  a fresh copy is:  $0 down $name --purge   then up again"
      fi
      ;;
    */*|.|..|~*)
      [ -d "$data" ] || die "no such directory: $data"
      MOUNT_SRC="$(cd "$data" && pwd)"       # docker needs it absolute
      DATA_NOTE="host directory $MOUNT_SRC, used in place"; RISKY=1
      ;;
    *)
      volume_exists "$data" || die "no docker volume named \"$data\". Give a path, a volume that exists, or copy:$data to work on a copy."
      MOUNT_SRC="$data"; DATA_NOTE="volume $data, used in place"; RISKY=1
      ;;
  esac

  if [ "$RISKY" -eq 1 ]; then
    warn "This instance writes to $DATA_NOTE."
    warn "Reaper applies its database migrations on boot, so this image may upgrade that"
    warn "database and older code may then refuse to read it. To leave the original alone:"
    warn "    --data copy:${data}"
  fi

  # --- replace any earlier container of this instance ---
  if exists "$container"; then
    log "replacing the running $name"
    docker rm -f "$container" >/dev/null
  fi

  # --- create and seed the volume, when this instance owns one ---
  if [ "$MOUNT_SRC" = "$volume" ] && ! volume_exists "$volume"; then
    docker volume create --label "$LABEL_MARK=1" --label "$LABEL_MARK.name=$name" "$volume" >/dev/null
    case "${data:-}" in
      copy:*)
        local src="${data#copy:}" from
        if [ -d "$src" ]; then
          from="$(cd "$src" && pwd)"
        elif volume_exists "$src"; then
          from="$src"
        else
          docker volume rm "$volume" >/dev/null
          die "cannot copy from \"$src\": it is neither a directory nor a docker volume on this host."
        fi
        log "copying $src into $volume"
        # The Reaper image is already pulled and runs as root before it drops privileges,
        # so it can do the copy, and nothing extra is downloaded to do it. The source is
        # read-only, which is the whole point of copying.
        docker run --rm \
          -v "$from":/from:ro -v "$volume":/to \
          --entrypoint sh "$image" -c 'cp -a /from/. /to/' \
          || { docker volume rm "$volume" >/dev/null; die "the copy failed, so nothing was started."; }
        ;;
    esac
  fi

  # --- run ---
  local cport; cport="$(container_port_from_env)"
  case "$cport" in
    ''|*[!0-9]*) die "REAPER_PORT must be a port number, and you passed: $cport" ;;
  esac

  local -a args=(
    run -d
    --name "$container"
    --label "$LABEL_MARK=1"
    --label "$LABEL_MARK.name=$name"
    --label "$LABEL_MARK.image=$image"
    --label "$LABEL_MARK.data=$DATA_NOTE"
    --label "$LABEL_MARK.cport=$cport"
    --restart unless-stopped
    -p "${port}:${cport}"
    -v "${MOUNT_SRC}:/data"
    # A try starts unable to delete, whatever the operator's own compose file does. This
    # seeds a brand new install only; a copied database keeps whatever it was set to, and
    # arming is password-gated in the web UI either way.
    -e REAPER_DESTRUCTIVE_ACTIONS_ENABLED=false
  )
  local f v
  for f in ${ENVFILES[@]+"${ENVFILES[@]}"}; do
    [ -f "$f" ] || die "no such env file: $f"
    args+=(--env-file "$f")
  done
  for v in ${ENVS[@]+"${ENVS[@]}"}; do
    args+=(-e "$v")
  done
  args+=("$image")

  log "starting $name"
  docker "${args[@]}" >/dev/null || die "the container could not start. See: docker logs $container"

  # The published port is read back from docker rather than echoed, because --port auto
  # only learns its number here.
  local published
  published="$(docker port "$container" "${cport}/tcp" 2>/dev/null | head -1)"
  published="${published##*:}"
  [ -n "$published" ] || published="$port"

  wait_for_health "$container" "$name" || return 1

  local digest
  digest="$(docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' "$image" 2>/dev/null || true)"

  echo
  log "$name is up"
  log "  http://localhost:${published}"
  log "  image  $image"
  [ -n "$digest" ] && log "  digest ${digest##*@}"
  log "  data   $DATA_NOTE"
  echo
  log "logs:  $0 logs $name -f"
  log "down:  $0 down $name"
}

# Poll the image's own healthcheck. It reports what Reaper thinks of itself, which a
# connection test from outside does not: migrations run before the app serves anything,
# and a database it refuses to open must read as a failure here, not as a slow start.
wait_for_health() {
  local container="$1" name="$2" waited=0 limit=120 state health
  log "waiting for it to answer"
  while [ "$waited" -lt "$limit" ]; do
    state="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || echo gone)"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container" 2>/dev/null || true)"

    case "$state" in
      running)
        # No healthcheck declared (an older image, or a fork's build): treat running as up.
        [ -z "$health" ] && return 0
        [ "$health" = "healthy" ] && return 0
        ;;
      exited|dead)
        warn "$name stopped on its own. Its last lines:"
        docker logs --tail 30 "$container" >&2 || true
        warn "full log:  $0 logs $name"
        return 1
        ;;
      gone)
        warn "$name disappeared while starting."
        return 1
        ;;
    esac
    sleep 2
    waited=$((waited + 2))
  done

  warn "$name is still not healthy after ${limit}s. Its last lines:"
  docker logs --tail 30 "$container" >&2 || true
  warn "It may only be slow. Check again with:  $0 list"
  return 1
}

# ========================================================================================
# down / clean
# ========================================================================================
cmd_down() {
  local name="" purge=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --purge)   purge=1; shift ;;
      -h|--help) usage; return 0 ;;
      -*)        die "unknown option for down: $1" ;;
      *)         name="$1"; shift ;;
    esac
  done
  [ -n "$name" ] || die "which instance? Try: $0 list"
  require_docker
  remove_instance "$(resolve_name "$name")" "$purge"
}

remove_instance() {
  local name="$1" purge="$2" container volume
  container="$(container_of "$name")"
  volume="$(volume_of "$name")"

  if exists "$container"; then
    docker rm -f "$container" >/dev/null
    log "removed $name"
  else
    warn "no container for \"$name\" (already down?)"
  fi

  if [ "$purge" -eq 1 ]; then
    if ! volume_exists "$volume"; then
      : # nothing of its own to delete: it ran on a directory or on someone else's volume
    elif is_managed_volume "$volume"; then
      docker volume rm "$volume" >/dev/null
      log "deleted its data ($volume)"
    else
      warn "left $volume alone: this script did not create it."
    fi
  elif volume_exists "$volume"; then
    log "kept its data ($volume). Delete it with: $0 down $name --purge"
  fi
}

cmd_clean() {
  local purge=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --purge)   purge=1; shift ;;
      -h|--help) usage; return 0 ;;
      *)         die "unknown option for clean: $1" ;;
    esac
  done
  require_docker

  local ids id name found=0
  ids="$(docker ps -aq --filter "label=$LABEL_MARK=1" || true)"
  if [ -n "$ids" ]; then
    while IFS= read -r id; do
      [ -n "$id" ] || continue
      name="$(label_name_of "$id")"
      [ -n "$name" ] || continue
      found=1
      remove_instance "$name" "$purge"
    done <<< "$ids"
  fi
  [ "$found" -eq 1 ] || log "no instances were running."

  # A volume can outlive its container, when `down` ran without --purge earlier.
  if [ "$purge" -eq 1 ]; then
    local vols vol
    vols="$(docker volume ls -q --filter "label=$LABEL_MARK=1" || true)"
    if [ -n "$vols" ]; then
      while IFS= read -r vol; do
        [ -n "$vol" ] || continue
        docker volume rm "$vol" >/dev/null 2>&1 && log "deleted leftover data ($vol)"
      done <<< "$vols"
    fi
  fi
}

# ========================================================================================
# list / logs
# ========================================================================================
cmd_list() {
  require_docker
  local ids
  ids="$(docker ps -aq --filter "label=$LABEL_MARK=1" || true)"
  if [ -z "$ids" ]; then
    log "no instances. Start one with: $0 up --pr <number>"
    return 0
  fi
  printf '%-16s %-10s %-9s %-34s %s\n' NAME PORT STATE IMAGE DATA
  local id
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    local name state health port image data shown
    name="$(label_name_of "$id")"
    image="$(docker inspect --format '{{if .Config.Labels}}{{index .Config.Labels "'"$LABEL_MARK"'.image"}}{{end}}' "$id")"
    data="$(docker inspect --format '{{if .Config.Labels}}{{index .Config.Labels "'"$LABEL_MARK"'.data"}}{{end}}' "$id")"
    state="$(docker inspect --format '{{.State.Status}}' "$id")"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$id")"
    local cport
    cport="$(docker inspect --format '{{if .Config.Labels}}{{index .Config.Labels "'"$LABEL_MARK"'.cport"}}{{end}}' "$id")"
    port="$(docker port "$id" "${cport:-8420}/tcp" 2>/dev/null | head -1)"; port="${port##*:}"
    shown="$state"
    [ -n "$health" ] && [ "$state" = "running" ] && shown="$health"
    printf '%-16s %-10s %-9s %-34s %s\n' "$name" "${port:--}" "$shown" "$image" "$data"
  done <<< "$ids"
}

cmd_logs() {
  local name="" ; local -a passthru=()
  while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help) usage; return 0 ;;
      -*)        passthru+=("$1"); shift ;;
      *)         name="$1"; shift ;;
    esac
  done
  [ -n "$name" ] || die "which instance? Try: $0 list"
  require_docker
  local container; container="$(container_of "$(resolve_name "$name")")"
  exists "$container" || die "no instance named \"$name\". Try: $0 list"
  docker logs ${passthru[@]+"${passthru[@]}"} "$container"
}

# ========================================================================================
case "${1:-}" in
  up)             shift; cmd_up "$@" ;;
  down)           shift; cmd_down "$@" ;;
  clean)          shift; cmd_clean "$@" ;;
  list|ls)        shift; cmd_list "$@" ;;
  logs)           shift; cmd_logs "$@" ;;
  -h|--help|help) usage ;;
  "")             usage; exit 1 ;;
  *)              die "unknown command: $1  (try: $0 --help)" ;;
esac
