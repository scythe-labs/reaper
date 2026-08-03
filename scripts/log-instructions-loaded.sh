#!/usr/bin/env bash
# Records every load of CLAUDE.md or .claude/rules/*.md, so we can confirm the
# path-scoped rules actually reach an agent's context before it edits the code
# they govern.
#
# The rule files are scoped by `paths:` frontmatter and load lazily, when Claude
# reads a matching file. That is the whole design: a file must be read before it
# can be edited, so its rules land first. But "designed to" is not "does," and
# this is the only way to see it happen. Read the log after a session:
#
#     tail .claude/instructions-loaded.log
#
# A backend session with no backend.md line means the glob is not matching and
# the rules are silently absent -- treat that as a broken safety net, not a
# cosmetic bug.
#
# Wired as an InstructionsLoaded hook in .claude/settings.json. That event
# cannot block anything (its exit code is ignored), so this only ever observes.
#
# Payload shape (confirmed empirically 2026-07-26, and against the hooks
# reference). The event fires ONCE PER FILE, so `file_path` is a single string,
# never a list:
#
#     {"session_id": ..., "transcript_path": ..., "cwd": ...,
#      "hook_event_name": "InstructionsLoaded",
#      "load_reason": "path_glob_match",              # or session_start,
#                                                     # nested_traversal,
#                                                     # include, compact
#      "memory_type": "Project",
#      "file_path": "<abs>/.claude/rules/backend.md", # the file that loaded
#      "globs": ["src/reaper/**/*.py", ...],          # its paths: frontmatter
#      "trigger_file_path": "<abs>/src/reaper/..."}   # the read that pulled it
#
# The first version of this script guessed `.matcher`/`.instruction_files` and
# logged its own `//` fallbacks, so 31 consecutive loads recorded as "?  |" and
# read like data. A parse that cannot find its keys now logs PARSE-FAILED and
# the payload's actual key names, so the next schema drift announces itself
# instead of quietly flatlining.
set -uo pipefail

project_dir="${CLAUDE_PROJECT_DIR:-.}"
log_dir="${project_dir}/.claude"
log_file="${log_dir}/instructions-loaded.log"

# Never let a logging failure disturb a session.
mkdir -p "${log_dir}" 2>/dev/null || exit 0

payload="$(cat 2>/dev/null || true)"

# python3 is a project dependency (3.13), so parse with it and keep a grep
# fallback for the case where it is somehow absent. Paths are logged relative to
# the repo so a line stays readable at a glance.
if command -v python3 >/dev/null 2>&1; then
  detail="$(printf '%s' "${payload}" | python3 -c '
import json, os, sys

root = os.environ.get("CLAUDE_PROJECT_DIR", "") or os.getcwd()


def rel(p):
    """Repo-relative when the path is inside the repo, absolute when it is not.

    User-level memory (~/.claude/CLAUDE.md) loads on session_start and lives
    outside the project, where relpath would render it as a run of "..".
    """
    if not p:
        return "-"
    try:
        r = os.path.relpath(p, root)
    except ValueError:  # different drive, or a path relpath cannot express
        return p
    return p if r.startswith(os.pardir) else r


try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE-FAILED | payload was not JSON")
    sys.exit(0)

reason = d.get("load_reason")
loaded = d.get("file_path")
if reason is None and loaded is None:
    # The schema moved. Say so, and name what we did get, so the fix is obvious.
    print("PARSE-FAILED | payload keys: " + ",".join(sorted(d)))
    sys.exit(0)

trigger = rel(d.get("trigger_file_path"))
print((reason or "?") + " | " + rel(loaded) + " | triggered by " + trigger)
' 2>/dev/null)"
else
  reason="$(printf '%s' "${payload}" | grep -oE '"load_reason"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
  loaded="$(printf '%s' "${payload}" | grep -oE '(^|[{,])[[:space:]]*"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
  trigger="$(printf '%s' "${payload}" | grep -oE '"trigger_file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
  if [ -z "${reason}" ] && [ -z "${loaded}" ]; then
    detail="PARSE-FAILED | no load_reason or file_path (grep fallback; python3 absent)"
  else
    detail="${reason:-?} | ${loaded#"${project_dir}"/} | triggered by ${trigger:-–}"
  fi
fi

[ -z "${detail}" ] && detail="PARSE-FAILED | parser produced no output"

printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${detail}" >>"${log_file}" 2>/dev/null || true
exit 0
