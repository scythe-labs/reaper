---
paths:
  - "src/reaper/auth/**/*.py"
  - "src/reaper/secrets.py"
  - "src/reaper/logbuffer.py"
  - "src/reaper/services/restore.py"
  - "src/reaper/services/backup.py"
  - "src/reaper/api/settings.py"
  - "src/reaper/api/plex.py"
  - "src/reaper/api/backup.py"
  - "src/reaper/api/deps.py"
  - "src/reaper/api/auth.py"
---

# Auth, secrets, and the artifacts that carry them

Every rule here is a blocker. Rule numbers are permanent, because existing tests and the
archived review passes in `docs/history/` cite them. Where two rules overlap, the more specific
one governs. A code comment or test docstring states its constraint in plain language and
does not cite a rule number. This file governs credentials, sessions, at-rest key material, and
the backup and restore that move that material. Only rules about those belong here. Rules that
bind every backend file are in `.claude/rules/backend.md`. Rules that bind every file are in the
root `CLAUDE.md`. Holds 11, 12, 14, 74–76, 83, 84, 98–101, 125, 126, 130.

**11 / 98. Throttles and the Argon2 gate bind at the granularity of the thing being abused.**
Every unauthenticated, state-establishing endpoint is throttled both per IP and per account,
with backoff and lockout. `plex/start` and `plex/poll` are throttled exactly as `/local` and
`/recover` are. A route that amplifies outbound requests caps resource creation per IP. The
concurrency gate acquires one slot per hash, not per request. A gate wrapped around a loop of N
Argon2 verifications bounds nothing. A full gate returns 503 and must never count as a failed
attempt. Otherwise an attacker who fills the gate triggers the account lockout. Never rely on a
fixed CSRF header or a password-length rule as the only defense.

**12 / 75. A credential change invalidates existing sessions, and restoring or importing an
auth-bearing database is a credential change.** Call the sign-out-everywhere primitive on
password reset and on deactivation. Never leave an issued cookie valid on `token_hash` + expiry
alone. On restore, purge session rows, recovery tokens, and pending logins in the staged data at
arm time, in the same function that forces deletion off.

**14 / 83. Owner-only from creation, for every copy.** Create secret files atomically with
`os.open(..., O_EXCL, 0o600)`. Never write a file and then `chmod` it. This also binds restored
or extracted key material, and newly created decision-trail and log directories. They get 0600
for files and 0700 for directories at creation. A later `chmod` leaves a window in which the
path has wider permissions.

**76. Provenance and self-sufficiency fields derive from runtime precedence, not file
existence.** Code that reports where a key or credential comes from, or whether an artifact is
self-contained, consults the same resolution order the runtime uses (`resolve_secret_key`
precedence). It never uses a bare `is_file()`.

**84. Every operator-supplied URL validates its scheme as http or https at the API edge,
through one shared check.** Every new URL-shaped setting reuses the same validator its sibling
fields use.

**99. The scrubber covers path-embedded secrets, and nothing renders a record the scrubber has
not seen.** Add the webhook path shape to `_redact_str`, so a token in a URL path is scrubbed
under any log key. Redaction runs after exception formatting on both paths, the stdlib handler
and the structlog chain alike. An HTTP error's `str()` embeds the full request URL. A processor
order that redacts before `format_exc_info` writes the secret in the clear. So does a handler
that appends `self.format(record)` beside an already-redacted copy.

**100. When key or salt material is present but unreadable, the app refuses to boot. It never
regenerates the material and proceeds.** Regenerated material cannot decrypt any credential
written under the prior material, and nothing reports the loss. Raise with an actionable
message and surface it in the UI safety state. Material that is missing may still be generated,
because missing material means a first run. Material that is present but corrupt is never
treated as a first run.

**101. A forwarded request header that changes an auth or security decision is trusted only from
a configured trusted proxy.** `X-Forwarded-Proto` passes the same `trusted_proxies` check that
`X-Forwarded-For` passes.

**125. A single-use credential is consumed only after the operation it authorizes succeeds.**
A recovery token consumed before the target is resolved is lost to a recoverable error, such as
a 409 name clash. The operator's only 15-minute token is spent, and nobody is signed in. Mark
the credential used on the success path, or restore it on any error that leaves the operator
able to retry.

**126. A two-stage swap of irreplaceable material is crash-atomic, and its failure message
never claims a state the code did not verify.** A restore interrupted between its two move
loops must never leave the staged key and salt deleted while the database they decrypt is
already live. Stage first, then swap, so that any interruption leaves one complete set intact.
Never print "your current data was kept" from a path that did not confirm it (rule 7/24, and
the prime directive).

**130. A KDF cost is bound to a stated threat model, and raised by forward-compatible
rotation.** Record the cost parameter beside the material it derived. Register prior costs as
decrypt-only, so raising the cost cannot make tokens already stored unreadable. A cost chosen
once and never revisited falls behind attacker hardware, and rule 13's "salted KDF" weakens with
no warning.

**74. A gate on an uploaded or restored artifact validates the artifact, never its manifest.**
Any property a safety check depends on (schema revision, version, counts) is read from the
artifact itself. A manifest or header claim may be cross-checked, but is never trusted alone.
