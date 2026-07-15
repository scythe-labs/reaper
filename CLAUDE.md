# Reaper — guide for agent sessions

Reaper is a self-hosted web tool that finds media nobody watches, **explains why it thinks
each item is expendable** (every signal, and every protection that was checked and did
*not* fire), lets you review and approve, then removes it safely *through* Sonarr/Radarr and
refreshes Plex. Python 3.13 / FastAPI backend + React 19 / Vite frontend, one container.

> **Prime directive: Reaper deletes irreplaceable data from a server other people depend on.
> Every ambiguity resolves toward keeping the file.** When in doubt, fail closed.

## Golden rules (read first)

- **No identifying information in code, docs, tests, or commit messages.** Reaper ships to
  operators whose servers we will never see. Never commit a real title, host, path,
  username, or stat — use generic placeholders. Live-testing findings are recorded as
  ratios and shapes, never fingerprints. This applies to commit messages too.
- **Treat Reaper as production code.** It will be released; write for an unknown operator,
  never for one specific server.
- **Keep `docs/PLAN.md` current.** It is the living plan — what is done and, more
  importantly, *which assumptions turned out wrong*. Update it as work proceeds. Record
  findings (including negative results) in `docs/LEARNINGS.md` / `docs/SIGNALS.md`.
- **Pre-release: migrations stay at one Alembic baseline** and the dev DB is disposable.
- **Commit only when asked**; end commit messages with the `Co-Authored-By` trailer.

## Branch & merge workflow

- **`dev` is the default branch, and all work lands there.** Push to `dev`, or to a feature
  branch off `dev` that merges back into `dev`.
- **`main` is release-only.** Never push to `main` directly. To promote `dev` to `main`,
  open a pull request from `dev` → `main` and **squash-merge** it, so `main`'s history is a
  clean sequence of squashed releases while the granular history lives on `dev`.
  - With the `tea` CLI: `tea pr create --base main --head dev`, then squash-merge the PR
    (`tea pr merge --style squash <n>`), and delete any temporary feature branch after.

## Verification gates (all must pass before calling work done — these mirror CI)

```
uv run ruff check .
uv run ruff format --check .
uv run mypy src/reaper                 # src only; tests are not type-checked
uv run pytest
uv run alembic upgrade head            # then `alembic check` for model/migration drift
npm --prefix frontend run build        # tsc --noEmit, then vite build
docker build -t reaper:ci .            # the shipped artifact must build
```

Run the relevant subset while iterating; run the full set before a commit. **Always run
`uv run ruff format .` (not just `--check`) before staging — format failures are the most
common CI break.** When a change is observable in the app, *drive it end-to-end* (see the
`verify` skill), don't stop at green tests.

## Dev environment

- **API :8420, frontend :5173** (Vite proxies `/api`). Start them via `.claude/launch.json`
  (`preview_start` with name `reaper-api` / `reaper-frontend`) — never hand-run dev servers.
- API calls require the header **`X-Reaper-CSRF: 1`**; auth is a cookie session.
- Secrets live in a gitignored **`.env.local`**; `data/` (`reaper.db`, `cache.db`) is
  gitignored and rebuildable. Never paste real keys into the transcript or a commit.

## Architecture

- `src/reaper/clients/` — the **only** place HTTP lives. `GuardedTransport` (and its
  `GuardedSession` twin for plexapi) refuses any mutating request unless deletion is armed
  on the host **and** the executor declared the intent to the journal first.
- `src/reaper/engine/` — `gates` (hard, fail-closed protections), `signals` (soft weighted),
  `score` (baseline-50, fixed denominator), and the explainable "why" record.
- `src/reaper/services/` — `snapshot` (gather → freeze → hash → score), `planner` (build the
  journalled plan), `executor` (the real send + interlocks), plus grace, leaving_soon,
  scan_runner, whitelist, etc.
- `src/reaper/api/` — FastAPI routers. `frontend/src/` — the React SPA.
- A **scan is a snapshot**: all evidence is frozen and hashed *before* scoring, so a
  transient timeout can never flip an item's fate mid-run.

## Safety model

Two **independent** layers sit under every mutation, and neither is trusted alone:
1. The executor's `dry_run` (the default) walks every interlock and records what it *would*
   send, but sends nothing.
2. The transport guard refuses any mutating call unless the host is armed **and** the intent
   was journalled first — a property of the host a browser cannot reach.

Deletion is armed only from the UI (password-gated). The **one** route that deletes is
`POST /api/runs/{id}/execute` — it requires the host armed and the exact content-bound
confirmation phrase (recomputed server-side). The scheduler never deletes. The executor's
interlocks (manifest re-check, caps that abort-not-truncate, the canary, the per-item
streaming veto and played-since-approval check) each resolve toward keeping the file.

## Where things are documented

- `docs/PLAN.md` — the living plan (start here for current state).
- `docs/LEARNINGS.md`, `docs/SIGNALS.md` — findings from real data.
- `docs/CODE_REVIEW.md` — the whole-codebase review pass.

---

# Engineering rules

Standing rules for anyone — human or agent — working on Reaper. They are the distilled,
adversarially-verified lessons from this codebase's reviews. Most resolve toward *keeping
the file* and *failing closed*. Read them before touching the safety, auth, or client paths.

## The safety / deletion path

1. **Omitted field ≠ explicit empty collection.** On any destructive or filtering path,
   treat `None` and `[]` differently, and make an empty selection **fail closed** — an
   empty selection must never expand to "everything."
2. **Never fail open in the safety/deletion path.** When a whitelist/keep-list sync, a
   protection source, or an optional dependency (Plex) fails, degrade the snapshot to
   **un-executable** rather than proceeding with empty or stale protection data.
3. **Reuse the single production verdict/decision function** across engine, backtest,
   planner, and snapshot paths. Never reimplement condemn/score/coverage logic (including
   rounding and floors) in a second place where it can drift.
4. **Reset time-window clocks (grace) on re-entry into the tracked state,** and remove or
   consult per-item tracking rows when an item leaves the set. Never let a stale
   first-flagged timestamp skip a safety window.
5. **Never expand caps/counts over items that will later be filtered out.** Compute
   enforcement counts against the exact set that will be acted on — matching the count
   shown in the user's confirmation.

## Data integrity & honesty

6. **Disambiguate cross-system joins by a stable identifier** (year + title, not title
   alone), and refuse to bind on ambiguity (return Unknown / ABSTAIN). Never silently
   last-write-wins into a `dict[title, row]` map.
7. **Never let a comment or docstring claim a safeguard that is not implemented**
   (rate limiting, crash-recovery de-dup, drift detection, `0600`-from-creation). Either
   implement it, or correct the comment in the same change.
8. **Make notifications and side-effecting writes idempotent across repeated calls,** keyed
   on durably-persisted state (an announced-set), not on a diff that is never persisted.
   Gate announcements so preview / read-only mode cannot re-spam.

## HTTP clients & error handling

9. **Route external HTTP through the shared client's error-mapping and retry layer** so
   transport/JSON errors become the domain error type. Never call `self._client.request`
   directly, and ensure `@retry` predicates match the exceptions actually thrown (don't
   convert-then-fail-to-retry).
10. **Report the accurate error/status.** Map a name-clash to `409` (not `404`), report the
    actual timeout kind (not a hardcoded budget), and honor upstream retry signals (e.g.
    Discord `Retry-After`) instead of dropping them.

## Auth & secrets

11. **Throttle authentication and recovery endpoints** with per-IP *and* per-account
    backoff/lockout, and cap concurrent expensive (Argon2) verifications. Never rely on a
    fixed CSRF header or a password-length rule as the only brute-force / DoS defense.
12. **Invalidate existing sessions on a credential change.** Call the sign-out-everywhere
    primitive on password reset and deactivation; never leave issued cookies valid on
    `token_hash` + expiry alone after the password changes.
13. **Never put secrets (tokens, keys, API keys) in URL query strings or path components
    that get logged.** Keep them in request bodies/headers, default `verify=True` for TLS,
    and derive at-rest keys with a salted KDF plus an entropy floor on operator-supplied
    keys.
14. **Create secret files atomically with owner-only mode** — `os.open(..., O_EXCL, 0o600)`
    — never write-then-`chmod`.

## Build & configuration

15. **Keep the shipped artifact building in CI** (run `docker build`), and install from the
    committed lockfile with digest-pinned base images. Never let unpinned `>=` floors
    resolve fresh at build time.
16. **Every operator-configurable credential lives in the DB-backed, encrypted, UI-editable
    surface and is documented in `.env.example`.** Never strand a configuration option
    (e.g. the Discord webhook) as an env-only, undocumented setting while the UI advertises
    its outcome.

## Frontend

17. **Handle React Query loading AND error states in gating / always-on UI.** Render an
    explicit unknown/error fallback for safety indicators and setup gates; never
    `return null` on missing data for a component whose contract is "always visible."
18. **Reuse the existing shared component / token / pattern** for tabs, segmented controls,
    notices, loading affordances, form-field labels, confirmation dialogs, CSS
    success/accent colors, and modal sizing (`dvh` on mobile). Never introduce a parallel
    one-off implementation, an undefined CSS variable, a native `confirm()`, or
    white-on-`--accent` text that fails WCAG AA.
19. **Give components stable keys and stable effect dependencies** — list keys unique among
    siblings, memoized arrays, `useRef` for cross-render mutable flags, and `useEffect`
    resets on identity-changing props. Never key on a value shared by sibling rows, or
    depend an effect on a freshly-allocated array.
20. **Use `Promise.allSettled` (not `Promise.all`) for independent bulk operations,** then
    reconcile UI state (invalidate queries, clear/retain selection) regardless of partial
    failure.
