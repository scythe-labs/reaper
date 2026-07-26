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
set -uo pipefail

log_dir="${CLAUDE_PROJECT_DIR:-.}/.claude"
log_file="${log_dir}/instructions-loaded.log"

# Never let a logging failure disturb a session.
mkdir -p "${log_dir}" 2>/dev/null || exit 0

payload="$(cat 2>/dev/null || true)"

# Pull the load reason and any file paths out of the hook payload. jq is not a
# project dependency, so fall back to a grep when it is missing.
if command -v jq >/dev/null 2>&1; then
  detail="$(printf '%s' "${payload}" |
    jq -r '[(.matcher // .reason // "?"), ((.instruction_files // .files // []) | join(" "))] | join(" | ")' \
    2>/dev/null)"
else
  detail="$(printf '%s' "${payload}" | tr ',' '\n' | grep -oE '"[^"]*(CLAUDE\.md|rules/[^"]*\.md)"' | tr -d '"' | tr '\n' ' ')"
fi

[ -z "${detail}" ] && detail="(no file detail in payload)"

printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${detail}" >>"${log_file}" 2>/dev/null || true
exit 0
