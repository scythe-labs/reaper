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
- **Mock up UI/UX before touching code.** When the work is about UI or UX, present a
  rendered mockup first (an inline visual widget or a self-contained HTML artifact) and
  iterate on *that* until it's approved — only then edit frontend code. Iterating on a
  picture is far faster and cheaper than iterating on a diff.
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
21. **Write every operator-facing string in plain language — sensible, concise, readable by
    anyone, not just engineers.** Lead with the outcome, say what it means for their files,
    and keep internal vocabulary out of the UI: no rating keys, no tmdb/imdb/tvdb ids, no
    "collision"/"abstain-as-jargon"/"guard"/"coverage bp". If a normal person wouldn't say
    it, reword it. This applies to notices, tooltips, empty states, and error text alike.
    **No em dashes in operator-facing copy** (frontend strings and backend `detail`/message
    strings alike): reword with a period, comma, or colon. Middots as separators
    ("70/100 · 20% of the score") and arrows ("Policy → Deletion") are fine.

## Blockers from the second review pass

Direct constraints from the second whole-codebase review (`docs/CODE_REVIEW.md`, dev @
`5b885f5`), derived from what that review actually found. Written as blockers, not
suggestions. They extend the rules above; where one sharpens an earlier rule (22 → 3,
24 → 7, 30 → 5, 33 → 9, 36 → 17), the newer, more specific obligation governs.

22. **One decision function.** The condemn/abstain/protect decision lives in exactly one
    engine-level function — `engine/verdict.decide_verdict` — and `snapshot`, `simulate`,
    and `backtest` must import it. Never write `score >= threshold` or
    `coverage_bp >= floor` inline outside it, and any agreement test must call the real
    functions, never a transcribed copy.
23. **Every re-decision surface handles every stored verdict state.** If you add or
    consume a Candidate verdict, enumerate all states (protect, abstain-blocked,
    abstain-by-score, condemn, overrides) at every consumer, and add the blocked/override
    cases to the simulator test in the same change.
24. **A comment naming a safeguard must cite its implementing function**, and you must
    verify that function exists and is called before merging. If you cannot cite it,
    change the comment in the same commit. The second review pass found six safeguards
    that existed only as prose.
25. **Operator-facing copy may only reference features that are wired.** Before writing UI
    or warning text that names a mechanism (backtest, cap, interlock), confirm the route
    or UI path exists; a DB constraint or schema for an unwired feature is a blocker, not
    a placeholder.
26. **Journal and state-transition writes on the deletion path must be durably committed
    at each step.** Never rely on `flush()` inside a run-long transaction for anything
    described as an audit record, and every state-transition guard must be an atomic
    `UPDATE … WHERE state = :expected`.
27. **A protection container that cannot be found is an error, never an empty result.**
    When a tag, collection, or list fetch would replace stored members, distinguish
    container-missing or malformed-body from genuinely-empty; missing-with-existing-
    members must raise so the previous membership survives and the snapshot degrades.
28. **Failure of any evidence source degrades the snapshot.** Any `except` around a source
    read in the scan pipeline must append to `pre_scan_degradations` (or call
    `context.degrade`); a bare `log.warning` on a source failure is a review-blocker.
    Watch history is a source.
29. **Every identity or membership lookup passes every id the item carries.** When calling
    `membership_index.lookup` or any cross-system join, pass imdb+tmdb+tvdb together;
    adding a new id kind to storage requires grepping and updating every lookup call site
    in the same change.
30. **Rank, cap, and count computations run over the exact set acted on.** Filter first
    (content-bearing seasons, non-spared deletable items, fetched-vs-all groups), then
    rank or count; any number shown beside a destructive button must be derived from the
    same set the server will act on.
31. **Derived condemn-lane values round toward keeping.** When precision is reduced on
    any field that can add deletion pressure (dates from years, sizes, ages), choose the
    bound that produces less pressure.
32. **Typed condition values validate against the field's type at the boundary, and
    evaluation never raises out of a scan.** Rule evaluation errors degrade that item as
    blocked; a stored policy must not be able to crash `score()` or `evaluate_all`.
33. **All HTTP goes through `clients/`.** A raw `httpx`/`requests` usage outside
    `src/reaper/clients/` is a blocker unless this file names it as a sanctioned
    exception. GET-shaped mutating endpoints must be classified and gated by path in the
    guard, not assumed safe by method. Public unauthenticated GETs go through
    `clients/public.py`. **Sanctioned exception:** `notify/discord.py`'s webhook POST.
    The webhook URL embeds a per-operator secret path, which the guard's exact-path
    allow-list cannot express; the URL is validated to Discord's hosts at the API edge,
    and the client sends only outbound notifications.
34. **Every constructed client has an owner that closes it.** A client constructed
    outside an exit stack (or without entering one in the same scope) is a leak; add the
    close path in the same diff as the construction.
35. **New `Facts` fields must be populated (or explicitly `Absent` with a comment) in
    every fact builder**: snapshot movies, season_scan, backtest, calibration. Grep all
    builders when adding a field; a field populated in one path silently changes scores
    and coverage in the others.
36. **Frontend gating and safety surfaces handle `isPending` and `error` explicitly.**
    `return null` on a failed query for an always-visible component is a blocker, and
    every async onClick is a mutation with a rendered error state.
37. **Tests that boot the app must be hermetic.** Use the shared autouse `_hermetic`
    fixture in `tests/conftest.py`, which stubs env seeding and startup network; never
    let a test read the developer's `.env` or reach the network.
38. **Dead safety-adjacent code is deleted, not stockpiled.** A method "for when the
    interlock lands" (as `PlexClient.item_count` was, until its count-delta interlock
    landed) must land with its interlock and tests, or not exist.
39. **Drafts and dirty checks compare canonical forms.** Never compare serialized state
    with raw `JSON.stringify` across frontend/backend boundaries; re-seed from the server
    response after a save.
