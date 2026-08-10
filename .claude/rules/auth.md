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

Blockers, not suggestions. **Rule numbers are permanent** (tests and comments cite them); where
two overlap, the more specific governs. Split out of `.claude/rules/backend.md` because these
bind credentials, sessions, at-rest key material, and the backup/restore that moves it — and
nothing else. Rules binding every backend file stay there; rules binding every file are in the
root `CLAUDE.md`. Holds 11, 12, 14, 74–76, 83, 84, 98–101, 125, 126, 130.

**11 / 98. Throttles and the Argon2 gate bind at the granularity of the thing being abused.**
Every unauthenticated, state-establishing endpoint is throttled per-IP *and* per-account with
backoff/lockout (`plex/start` and `plex/poll` exactly as `/local` and `/recover`), and
outbound-amplifying routes cap per-IP resource creation. The concurrency gate acquires one slot
per *hash*, not per request: a gate wrapping a loop of N Argon2 verifications bounds nothing. A
full gate returns 503 and must never register as a failed attempt, or the DoS defense becomes
the lockout. Never rely on a fixed CSRF header or a password-length rule as the only defense.

**12 / 75. A credential change invalidates existing sessions — and restoring or importing an
auth-bearing database is a credential change.** Call the sign-out-everywhere primitive on
password reset and deactivation; never leave issued cookies valid on `token_hash` + expiry
alone. On restore, purge session rows, recovery tokens, and pending logins in the staged data at
arm time, in the same function that forces deletion off.

**14 / 83. Owner-only from creation, for every copy.** Create secret files atomically with
`os.open(..., O_EXCL, 0o600)` — never write-then-`chmod`. This binds restored or extracted key
material and newly created decision-trail/log directories too: 0600 / 0700 at creation, not
after a later chmod window.

**76. Provenance and self-sufficiency fields derive from runtime precedence, not file
existence.** Anything reporting where a key or credential comes from, or whether an artifact is
self-contained, consults the same resolution order the runtime uses (`resolve_secret_key`
precedence), never a bare `is_file()`.

**84. Operator-supplied URLs validate scheme http/https at the API edge, everywhere, via one
shared check.** Any new URL-shaped setting reuses the same validator its sibling fields use.

**99. The scrubber covers path-embedded secrets, and nothing renders a record the scrubber has
not seen.** Add the webhook path shape to `_redact_str` so a token in a URL path is scrubbed
whatever log key it rides under. Redaction runs *after* exception formatting on both paths, the
stdlib handler and the structlog chain alike: an HTTP error's `str()` embeds the full request
URL, so a processor order that redacts before `format_exc_info` — or a handler that appends
`self.format(record)` beside an already-redacted copy — writes the secret in the clear.

**100. Key or salt material that is present but unreadable refuses to boot; it never regenerates
and proceeds.** Regenerating silently bricks every credential written under the prior material.
Raise with an actionable message and surface it in the UI safety state. Genuinely *missing*
material is a different case and may still be generated: absent is a first run, corrupt is a
disaster.

**101. A forwarded request header that changes an auth or security decision is trusted only from
a configured trusted proxy.** `X-Forwarded-Proto` passes the same `trusted_proxies` check
`X-Forwarded-For` already does.

**125. A single-use credential is consumed only after the operation it authorizes succeeds.**
Burning a recovery token before the target is resolved means a recoverable error (a 409 name
clash) spends the operator's only 15-minute token without signing anyone in. Mark it used on
the success path, or restore it on any error that leaves the operator able to retry.

**126. A two-stage swap of irreplaceable material is crash-atomic, and its failure message
never claims a state the code did not verify.** A restore interrupted between its two move
loops must not leave the staged key and salt deleted while the database they decrypt is
already live. Stage, then swap so that any interruption leaves one complete set intact, and
never print "your current data was kept" from a path that did not confirm it (rule 7/24, and
the prime directive).

**130. A KDF cost is bound to a stated threat model, and raised by forward-compatible
rotation.** Record the cost parameter alongside the material it derived, and register prior
costs decrypt-only so raising the cost cannot brick tokens already stored. A cost chosen once
and never revisited is a silent expiry date on rule 13's "salted KDF."

**74. A gate on an uploaded or restored artifact validates the artifact, never its manifest.**
Any property a safety check depends on (schema revision, version, counts) is read from the
artifact itself; a manifest or header claim may be cross-checked but never trusted alone.
