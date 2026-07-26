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
- **American English spelling everywhere — code, identifiers, comments, docs, tests,
  operator copy, and commit messages.** Write `color`, `behavior`, `honor`, `normalize`,
  `serialize`, `judgment`, `canceled`, `labeled`, `gray`, `license`, `defense`, `center`,
  `recognize`, `artifact`, not their British forms (`colour`, `behaviour`, `judgement`,
  `grey`, …). Identifiers follow too (`normalize_label`, `SeasonJudgment`). The *only*
  exceptions are names owned by someone else and spelled British at the source: standard-
  library tokens like `asyncio.CancelledError` and the ARIA `aria-labelledby` attribute keep
  their real spelling. When in doubt, prefer the American form.
- **Treat Reaper as production code.** It will be released; write for an unknown operator,
  never for one specific server.
- **Keep `docs/PLAN.md` current.** It is the living plan — what is done and, more
  importantly, *which assumptions turned out wrong*. Update it as work proceeds. Record
  findings (including negative results) in `docs/LEARNINGS.md` / `docs/SIGNALS.md`.
- **Ship additive, non-breaking migrations. Never make a tester rebuild their DB.**
  Testers now run Reaper with real data, so the Alembic baseline (`22777b2b5015`) is
  **frozen**: never edit it. Every schema change is its own new revision chained onto the
  current head by `down_revision` — a nullable `ADD COLUMN`, a new table, a backfill — so
  `alembic upgrade head` on an existing database only ever adds, never drops or rewrites
  what is already there. New columns are nullable (or carry a server default) and the next
  scan backfills them; the app must read a not-yet-backfilled `NULL` as "unknown," never as
  a wrong definite value. `cache.db` stays disposable and unmigrated (raw DDL, rebuildable).
- **Operator copy is read at a glance, never twice.** A phrase over a sentence, a sentence
  over two; lead with the outcome and leave the explanation to help text bound to the
  control. These surfaces are *scanned* while deciding what to delete, so long copy does
  not get read at all. Rule 21 governs the vocabulary, this one the length — after writing
  an operator string, cut it once more.
- **Mock up UI/UX before touching code.** When the work is about UI or UX, present a
  rendered mockup first (it must be a self-contained HTML artifact that faithfully
  represents reapers look and feel) and iterate on *that* until it's approved — only then
  edit frontend code. Iterating on a picture is far faster and cheaper than iterating
  on a diff.
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
npm --prefix frontend run lint         # eslint; the two react-hooks rules are errors
npm --prefix frontend run test         # vitest component tests (the execute gate first)
npm --prefix frontend run build        # tsc --noEmit, then vite build
```

The container image build (`docker build -t reaper:ci .`) is **CI-only**: the pipeline
builds the shipped artifact, so don't run it locally to satisfy these gates.

Run the relevant subset while iterating; run the full set before a commit. **Always run
`uv run ruff format .` (not just `--check`) before staging — format failures are the most
common CI break.** When a change is observable in the app, *drive it end-to-end* (see the
`verify` skill), don't stop at green tests.

## Dev environment

- **API :8420, frontend :5173** (Vite proxies `/api`). In an interactive session start them
  via `.claude/launch.json` (`preview_start` with name `reaper-api` / `reaper-frontend`) —
  never hand-run dev servers.
- **Headless / background job (no `preview_start`):** run **`scripts/dev-local.sh`** — it
  applies migrations, boots both auto-reloading servers (API `--reload`, Vite HMR) against the
  shared real `data/`, waits for health, and prints the URLs. `down` stops them, `status` /
  `logs` inspect. The served UI at :5173 is the live dev server, not a build; `npm run build`
  is a CI gate only. See the `verify` skill for driving the UI once it is up.
- API calls require the header **`X-Reaper-CSRF: 1`**; auth is a cookie session.
- Secrets live in a gitignored **`.env.local`**; `data/` (`reaper.db`, `cache.db`) is
  gitignored and rebuildable. Never paste real keys into the transcript or a commit.

## Architecture

- `src/reaper/clients/` — the **only** place HTTP lives. `GuardedTransport` (and its
  `GuardedSession` twin for plexapi) refuses any mutating request unless deletion is armed
  on the host **and** the executor declared the intent to the journal first.
- `src/reaper/engine/` — `gates` (hard, fail-closed protections), `signals` (soft weighted
  pressure, and the `score()` function: **unsigned** pressure over a fixed denominator, so
  missing or keep-arguing evidence can only ever *lower* the score — never a signed score
  off a neutral baseline, which inverts under failure; see the "Why unsigned" note at the
  top of `signals.py`), `verdict.decide_verdict` (the one condemn/abstain/protect
  decision), and the explainable "why" record.
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
   **un-executable** rather than proceeding with empty or unverifiable protection data.
   One bounded exception, chosen deliberately (P-6): a failed whitelist refresh may
   coast on stored membership from a *recent* successful sync
   (`snapshot.WHITELIST_STALE_AFTER`, 48h) -- the stored copy still protects everything
   on it; past the bound, or with no record of a successful sync, degrade.
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

15. **Keep the shipped artifact building in CI** (CI runs `docker build`; don't build it
    locally), and install from the committed lockfile with digest-pinned base images. Never
    let unpinned `>=` floors resolve fresh at build time.
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
    ("70/100 · 20% of the score") and arrows ("Policy → Deletion") are fine. Length is
    governed by the golden rule above: read at a glance, never twice. A string that is
    plain but long still fails.

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
    Watch history is a source. **Sanctioned exception:** a *per-item* missing size does
    not degrade. Degradation is snapshot-global and a degraded snapshot is un-plannable
    outright, so one movie Radarr will not size would block the operator's entire run --
    the wrong blast radius. The compensating control is narrower and stronger: that item
    alone is held back from every plan (`planner.build_plan`) and refused again at send
    (`executor.size_confirmed`), and the operator is told the count and which items. The
    exception covers the item's own size only; a source that fails to *respond* still
    degrades. A success response carrying a null or malformed body is not a genuine empty
    either: distinguish it and degrade, rather than reading it as "nothing found."
    **Second sanctioned exception:** a source that can only ever *add* condemn evidence
    (the batch enrichment in `season_scan`) may log instead of degrading, because losing it
    can only lower pressure, which is the keep direction; the comment must say so. A source
    whose loss can *withdraw* a protection never qualifies (I-1).
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
    close path in the same diff as the construction. Every branch counts, including early
    returns and exceptions raised before the `try`: construct the client only *after* the
    guard that can return early, or wrap construction-through-use in one `try/finally` or
    `AsyncExitStack`. When the caller's own stack already entered the client for real,
    register the close with `push_async_callback`, never a second `enter_async_context`
    (PR-3, PR2-4).
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

## UI grammar (from the operator-approved consistency pass, 2026-07-17)

The whole UI speaks one control grammar. These rules came out of an audit that found the
same job done three or four different ways on one page (mocked as an artifact, approved,
then implemented app-wide except the review queue). They sharpen rule 18's "reuse the
shared pattern" into named obligations; a new variant of any of these is a blocker, not
a style choice.

40. **A number with a unit is one of two components, always.** A changeable unit is
    `QuantityInput`; a fixed unit ("days", "people", "seasons", "/ 10", "%", "+ votes")
    is `FixedQuantity` with the unit as a suffix in the same box (both in
    `components/QuantityInput.tsx`, sharing the `.qty` chrome). Never a bare
    `<input type="number">` beside loose unit text, and never a new input size: every
    text, number, and select box sits on the one control standard documented at the top
    of `index.css` (`0.42rem 0.6rem` padding, `--border-strong`, `--radius-sm`, `--bg`
    fill, accent focus ring). Width is the only thing that may vary.
41. **A choice between two visible options is the shared `Segmented`**
    (`components/Segmented.tsx`); a `<select>` is only for open lists (rating sources,
    fields, units, servers, log levels). Growing a segmented past three options may turn
    it into a select; hiding a binary inside a dropdown is never allowed. `Switch` stays
    the one on/off control (its file says why), and a settings-bearing group's
    sub-controls render only while its toggle is on — hidden, not disabled, matching the
    gates.
42. **A warning renders beside the control that fixes it.** Policy warnings anchor by
    `field` to their rule (the anchor list + `WarnBlock` in `PolicyEditor`); adding a
    warning field means adding an anchor or knowingly letting it fall through to the
    bottom stack, which exists so no field is ever silently dropped. Action failures
    everywhere are `.notice.notice-error` with a plain-language lead ("The scan didn't
    start: …"); bare red `.error` text survives only in the review surfaces and the
    simulator's dedicated failure panel.
43. **One save affordance per page.** The policy editor's sticky `.savebar` is the only
    save UI on that page: it names what is dirty, states when each part takes effect,
    saves everything with one click, and offers Discard. New savable state on that page
    joins the bar; never add a second Save button beside it.
44. **Settings-bearing groups are cards; plain toggles are rows.** A protection or
    group with sub-configuration is a `.rules-card` with its `Switch` in `.card-head`
    (rating bars, keep-tags; the season card shares the container); an on/off with
    nothing else stays a bare `.rule-row`. Rows of repeated per-item controls (the
    rating bars) align in one grid with a shared label column, never one boxed well
    per row.
45. **Help text binds to exactly one control, directly beneath it.** Never one help
    paragraph covering two controls, and never help detached from the row it explains.
    Known deferred exception to the notice unification: the `.warn` banner (ScanBar +
    the review card) merges into `.notice-warn` whenever the review UI is next touched.

## The queue's action grammar (from the review-queue UX pass, 2026-07-19)

How a queue row presents its Spare/Reap decision, settled over a run of approved mockups and
driven end-to-end in a real browser — which is where the one bug behind rule 46 surfaced, in
Safari alone. New variants are blockers, like the rules above.

46. **Row actions reveal on hover; a decided row rests as its icon.** The per-row Spare/Reap
    (`OverrideControls`) is hidden until the row is hovered or keyboard-focused — kept in flow
    with `visibility`, never `display`, so nothing reflows. A row carrying a hand override rests
    as a small icon of it (`OverrideMark`: ∞ spared, scythe reaped) in the buttons' slot, faded
    out by the same hover. Never park the full buttons on every row at rest, and never show the
    icon and the buttons at once. Give the toggling buttons a fixed width so a label change
    (Spare↔Spared, Reap↔Reaping) never resizes them: a shrinking button left a red ghost in the
    region it vacated, in Safari only. A **spent** spare is not a decision at rest and draws no
    mark at all (see rule 122): the row rests bare, and its button offers a fresh spare.
47. **Card hover is the accent, additive on the open card.** A card's hover is the accent edge,
    not gray; the open (`card-selected`) card keeps its accent selection bar on hover and deepens
    it, never trading it for the plain hover (which reads as a deselect). Any `:hover` that can
    also be `.active`/selected re-asserts the selected treatment at equal-or-higher specificity,
    so the chosen state is not lost under the pointer.
48. **Reap is dropped wherever the item is already condemned; Keep-first colors the pair.** A
    hand Reap does nothing to an already-condemned item, so it is hidden in every surface carrying
    `OverrideControls` (card, panel, season list, bulk bar) via `hideReap` — judged by the item's
    OWN verdict (`verdict === "condemn"`), never the tab's, so a mixed season expansion drops Reap
    on exactly the condemned rows (rule 51); the bulk bar is the one exception and keys on the tab
    verdict (a heterogeneous selection is not one item). Never reimplement that test inline. Spare
    is never a no-op and is never hidden. "Reap now" (the real deletion) is a different
    control and is never hidden. Spare invites in green; Reap stays the quiet gray of a plain
    button until hovered; a chosen decision is the solid hand-decision chip.
    - **A whole show is not atomic, so it uses its own no-op test.** A movie/season on the
      Condemned lane is fully condemned (`verdict === "condemn"` → Reap hidden). A show is on
      that lane because *some* season is, and a whole-show Reap still takes the seasons the scan
      kept — so both buttons stay until *every* season is condemned. That show test is
      `showReapIsNoop` (in `components/ReviewQueue.tsx`), the one place it lives; the show card's
      whole-show control and the show panel both call it, never a fourth inline copy. **Every
      whole-show `hideReap` computation runs over the whole show, every lane** — `showReapIsNoop`
      and `groupReapEffective` both take `group.seasons` in the panel and `group_seasons` (the
      strip marks, held as `showSeasons`) on the card, never the tab-filtered page. On the
      Condemned lane that page holds only the show's reaped/condemned seasons, which all agree
      "condemn"/"reap" and would hide the one control that reaps the show's kept seasons. The
      whole-show control's *lit* state is a separate question and is never an aggregate: it reads
      the show's OWN decision (`show_override`), so it can only ever clear what it lit (rule 50).
      The show
      panel (`ShowPanel`) carries the whole-show Spare/Reap in its own bottom `.why-actions`
      footer, the same placement the movie/season panel uses. The bulk bar's Reap still keys off
      the tab verdict alone (a heterogeneous selection is not a single item); refining it for
      show-only selections is an open follow-up, not a silent gap.

49. **A fate-bearing cell colors by the item's fate, never by the scan verdict alone.** The
    score badge (`Score`) and the season strip square (`SeasonStrip`) both route their color
    through the one `handFate` helper (`components/ReviewQueue.tsx`): a hand spare or an
    *effective* hand reap paints SOLID ("you chose this"); a reap the engine *can't honor yet*
    reads **dashed red** (`--condemn` on `--condemn-soft`, a dashed border, never solid), and on
    the strip it also carries a small scythe corner-mark (`.strip-mark`) so it still reads as
    YOUR ask and never blends into the plain condemned outline beside it; an untouched cell keeps
    its scan verdict. **Amber (`--unknown`) now means exactly one thing — "left for you to
    decide" (the abstain `status-look` chip) — and never a held reap.** A held reap must never
    wear the solid red that means "removed," and a hand decision must never leave the number the
    color the scan first gave it. Keep the held-reap language consistent across movies and
    seasons: a movie already carries the scythe via its resting `OverrideMark`, the strip square
    carries it as the corner-mark, and both wear the dashed-red `.score-refused` /
    `.strip-ov-reap-refused` / `.status-reap-held` / `.chip-reap-refused` classes. Never recolor
    these cells by `verdict` inline; add the surface to `handFate`, and its `.score-*` /
    `.strip-ov-*` class after the scan-verdict classes so it wins.

50. **An override control reflects and acts on its OWN level; the effective (inherited) decision
    colors the row but never lights a control.** The whitelist keeps a decision at two levels —
    a whole show (its show key) or a single season (its own key), the season's winning over the
    show's — so three views ride on every candidate, built once in `_candidate_out` / `GroupOut`
    (`api/routes.py`) from the one `whitelist.effective_override` + `show_key`, never recomputed
    as a client-side aggregate: `override` is the decision *in effect* (own or inherited) and
    colors the chip, score, and strip; `override_own` is the item's own decision and is the ONLY
    value a Spare/Reap control passes to `OverrideControls` (a movie's `override_own` equals its
    `override`); `show_override` is the show's own decision, which lights the whole-show control
    (card + `ShowPanel`). Each control clears the key it lit — a season control the season key, a
    whole-show control the show key — so it can only ever reverse what it showed. Lighting a
    control from the effective/aggregate state it *cannot* clear was the dead toggle this rule
    exists to prevent: undoing a season kept by a whole-show spare changed nothing, because
    clearing the season key left the show-level spare in force. When a whole-show decision keeps
    or reaps a season, `KeptByShowNote` (`components/ReviewQueue.tsx`) names it beside that
    season's control — its wording turning on whether the season's own decision is absent, the
    same, or opposite — and a season-level clear NEVER silently un-decides the whole show (that
    strips protection from every other season: fail-open, forbidden). The grace clock follows the
    same effective set: `_sync_grace_clocks` (`api/whitelist.py`) deletes the clock when an
    override takes an item off the reap list, so a scan-condemned item the owner spares and later
    un-spares re-enters on a FRESH window, never a spent one (rule 4).

51. **Row actions align in fixed columns; the size holds still; every season is actable in
    place.** In the expanded season list (`SeasonList`) Spare keeps the left button column and
    Reap the right, both to the LEFT of a right-pinned size in its own column, so buttons and
    sizes read as straight columns whatever a row's button count. The button track and the size
    track are FIXED width, never `auto`: each `.season-row` is its own grid, so an `auto` track
    sizes to that row's own text ("22.5 GiB" vs "9.1 GiB") and drifts the buttons row to row. The
    button track is the `--btns` custom property, set once per list by `SeasonList` from whether
    any season there can show Reap (any non-condemned one), so a show condemned top to bottom
    reserves no empty Reap slot; `.season-row .override-controls` is `justify-self: start` so a
    lone Spare lands in the same column as a paired row's Spare. Every season row is actable from
    here — the old read-only "other-lane" row (and its edge marker) is gone — each with its own
    `OverrideControls` keyed to `override_own` (rule 50) and `hideReap` from that season's own
    verdict (rule 48). A per-tab `hideReap` on a list row, a read-only season row, or an `auto`
    button/size track that lets the columns drift is a regression.

## Blockers from the third review pass

Direct constraints from the diff review of the 16 commits since `4478aa7`
(`docs/CODE_REVIEW.md`, dev, 2026-07-21) — all 43 findings it reports are already fixed on
`dev`; these rules are what stops them recurring. Written as blockers, not suggestions. They
extend the rules above; where one sharpens an earlier rule (52 → 6/29, 53 → 7/25, 56 → 27,
61 → 49, 62 → 23/30, 65 → 2, 68 → 24), the newer, more specific obligation governs.

52. **A bare tmdb id is not a stable key across media kinds.** Movie and TV tmdb ids share
    the same integer space, so every map, index, or lookup keyed on a tmdb id carries the
    media kind alongside the number, on both the write and the read side (sharpens rules
    6/29 — B-1).
53. **A rendered limit checks its enable switch.** Any UI string, summary clause, or
    simulator note that states a cap, budget, or bound must branch on the setting that
    enables enforcement; showing the stored figure while the switch is off is a blocker
    (sharpens rules 7/25 — B-2).
54. **A preset that promises enforcement stages the enabling switch too.** Applying a preset
    sets every switch its help text implies, not just the values behind the switch (B-10).
55. **A job's off switch governs every path that runs the job.** Startup catch-ups, recovery
    paths, and other side entrances honor the stored off value, or the off-warning copy
    explicitly names the exception; off-warning copy states the real, code-verified
    consequence of turning the job off, including degradation that blocks runs, never a
    guessed softer one (B-3, U-2).
56. **Pagination advances and terminates on the raw page count, never a filtered one.** A
    defensive filter that can shrink a page must raise on anomaly rather than silently
    resize the page, and a total-size fallback must never default to the page size. A
    complete-or-raise docstring is a contract: violating input raises, it never returns a
    partial result (sharpens rule 27 — B-4).
57. **Plex tag-style removals address the stored spelling and resolve sections by key.**
    Group items by the exact stored tag spelling (casefold-matched, following
    `remove_label`) and resolve sections via `sectionByID`, never by title (B-5, B-13).
    `library.section(title)` is banned in `src/` outright, and this binds every call, not
    just label and collection writes: trash, refresh, count, and refresh-status too. Where
    only a title is known and it is ambiguous, ask each same-titled library in turn
    (`lists.PlexCollection` is the model), never the first match (B-2, B-3).
58. **A check-then-write re-reads inside the write transaction.** Splitting a state check
    into a read connection is fine only if the write transaction re-reads the state it acts
    on; DDL or destructive writes driven by pre-lock reads are a blocker (B-6).
59. **Multi-key JSON settings update per key or under a guarded merge.** A read-modify-write
    of a whole settings dict across an `await` is a blocker (B-12).
60. **Interactive children of a keyboard-handling row stop Enter/Space propagation.** Any
    control nested inside a row or card that has its own Enter/Space handler either stops
    propagation (the `SeasonStrip` guard is the model) or the container checks
    `e.target === e.currentTarget`; adding a control to a row without this check is a
    blocker (B-7).
61. **Prose about a removal consults the effective decision, not just the scan verdict.**
    Any note, chip, or sentence asserting an item "will be removed" or "will be kept" must
    branch on `override_effective`, including held reaps and opposing season-level decisions
    (extends rule 49 from color to wording — U-1, U-3).
62. **Every number on the Reap page derives from the planner's exact set.** Headline,
    ledger, and per-line counts consult the same branches the planner does, including the
    unknown-size allowance via `useHoldsBackUnmeasured()`, and every stored override state
    (held reaps included) appears in the ledger or is explicitly summarized (sharpens rules
    23/30 — B-8, PR-2).
63. **Rows are keyed and aggregated by a stable server id, never a display name.** If the
    schema lacks an id, add one in the same change; user-level roll-ups key on the
    always-present per-user id, not an optional linked-account id (B-9, U-8). This binds
    membership indexes and path tables as much as it binds display rows: any dict whose key
    can collide is a bug, and a display name always can (B-3).
64. **Removing a surface removes its whole supply chain in the same change.** Route,
    schemas, client method, props, query-key invalidations, and comments naming it; grep for
    the query key and prop name before closing (R-1, R-2, R-3).
65. **Silent recovery on operator-configured safety values is forbidden.** A fallback that
    replaces saved profile/policy values must surface a flag the UI renders and degrade the
    scan, following the `ActivePolicy` pattern; a log line alone is a blocker (sharpens
    rule 2 — PR-1).
66. **Server-defined lists render from the server response.** A hardcoded frontend copy of a
    backend id list (jobs, phases, states) is a blocker when the server already returns the
    list; fallback copy handles unknown ids only (R-5).
67. **Values coupled across TSX and CSS are derived from one declaration.** A width, gap, or
    count that must agree between a component and a stylesheet lives in one custom property
    both read, or both sites carry a cross-reference comment; the `--btns` track (rule 51)
    is the model, not the only case this applies to (H-1).
68. **Generated assets ship with their generator.** A comment saying an asset is generated
    must name a committed, runnable script, and a drift test covers every generated
    artifact, not just one (extends rule 24 to assets — PR-6).
69. **The icon link the app rewrites at runtime is declared last in `index.html`.** Static
    fallback icons precede the dynamic one; adding an icon link after `#favicon` is a
    blocker (B-11).

## Blockers from the fourth review pass

Direct constraints from the fourth diff review (dev @ `cea72d1`, 2026-07-23; its 36 findings
are remediated, and the review itself is preserved in this file's git history at `a7d7659`).
Written as blockers, not suggestions; where one sharpens an earlier rule (70 → 23, 71 → 4/50,
72 → 56/64, 74 → 27, 75 → 12, 77 → 61, 79 → 64, 82 → 24/28, 83 → 14, 86 → 21/53), the newer,
more specific obligation governs.

70. **Time-bounded state has exactly one durable realization point, shipped with the
    feature.** Any stored decision that expires (a timed spare, a deadline, a TTL) must be
    realized by code that WRITES the transition — an in-memory filter at read time is not
    a realization — and every live consumer must converge after it. A docstring saying
    "the next scan realizes it" requires the scan to actually persist that realization in
    the same change; shipping the read half without the write half is a blocker (B-1;
    extends rule 23: "expired" is a stored verdict state every consumer must handle).
71. **Clearing a protective override always restarts the grace clock.** When an override
    that kept an item off the reap list is removed, the FirstFlagged row is deleted before
    `record_first_flagged_bulk` runs, unconditionally — never trust `last_seen_condemned_at`
    continuity across a period when the item was invisible to the operator (B-2; sharpens
    rules 4/50).
72. **A hardening fix lands on every twin of the fixed function in the same change.**
    Before closing a fix to a copied pattern (paging loops, section resolution, error
    mapping), grep for the pattern's siblings and fix or explicitly defer each in writing;
    "when next touched" deferrals are honored the moment ANY commit touches the twin, not
    only when someone remembers (B-3, I-1; sharpens rules 56/64).
73. **A password-gated destructive confirm is content-bound.** The confirm request carries
    a server-verified token derived from the exact content the operator reviewed
    (recomputed or stored server-side at stage time), and the action refuses if the
    content changed since review. The execute route's phrase is the model; any new
    stage-review-confirm flow (restore, import, bulk apply) must carry the same binding
    (S-1).
74. **A gate on an uploaded or restored artifact validates the artifact, never its
    manifest.** Any property a safety check depends on (schema revision, version, counts)
    is read from the artifact itself; a manifest or header claim may be cross-checked but
    never trusted alone (S-2; extends rule 27's spirit to imports).
75. **Restoring or importing an auth-bearing database is a credential change.** Purge
    session rows, recovery tokens, and pending logins in the staged data at arm time, in
    the same function that forces deletion off (S-3; extends rule 12).
76. **Provenance and self-sufficiency fields derive from runtime precedence, not file
    existence.** Anything reporting where a key/credential comes from or whether an
    artifact is self-contained must consult the same resolution order the runtime uses
    (`resolve_secret_key` precedence), never a bare `is_file()` (B-4).
77. **Backend reporting surfaces consult effective overrides.** Any service that
    summarizes items as removable/reclaimable/kept (Scales, breakdowns, exports) merges
    live override state the same way the review routes do, or its copy explicitly states
    it shows scan verdicts only (B-5; extends rule 61 from frontend prose to backend
    aggregation).
78. **Attribution honors the request's scope.** When a request carries a season (or any
    partial) scope, per-person figures bind only the scoped subset; whole-title binding is
    allowed only for unscoped requests or with the granularity stated in the copy beside
    the number (B-6).
79. **A cache-invalidation helper that claims completeness is grep-verified against every
    query key, and a detail panel keyed on a row id is closed or re-resolved when its
    snapshot is replaced.** Invalidation alone is insufficient when the key itself points
    at superseded data (B-7; sharpens rule 64).
80. **Every close affordance runs the modal's close guard.** Browser Back, gestures, and
    any new dismissal path must honor the same `canClose` the scrim/Escape/✕ honor; a
    back-layer close that bypasses a declared guard is a blocker (B-11; extends rule 60's
    spirit to the history layer).
81. **A baseline edit — even one reverted within hours — obligates a guarded migration.**
    If the frozen baseline was ever wrong in a merged commit, every additive migration
    covering that window carries the heal migration's reflection guard so in-window
    databases upgrade instead of boot-looping. Never edit the baseline, and when the rule
    is broken anyway, the follow-up is guarded, not plain (B-8; extends the frozen-
    baseline golden rule).
82. **A persistent sink degrades loudly, once.** Any always-on writer (log file mirror,
    export stream) that can fail after setup carries a one-shot degradation flag surfaced
    where its output is consumed; a bare `suppress(Exception)` around a steady-state write
    whose output is documented as an audit trail is a blocker (PR-2; extends rules 24/28
    to infrastructure sinks).
83. **Owner-only-from-creation applies to every copy of a secret and to decision-trail
    dirs.** Restored/extracted key material and newly created log directories get 0600 /
    0700 at creation, not after a later chmod window (S-4, S-6; extends rule 14 beyond
    first creation).
84. **Operator-supplied URLs validate scheme http/https at the API edge, everywhere, via
    one shared check.** Any new URL-shaped setting reuses the same validator the sibling
    fields use; a `type="url"` input is not validation (S-5; extends rule 13's boundary
    discipline).
85. **Success copy fires on settled state.** A toast, timestamp, or "done" indicator is
    set only after the operation it describes has actually completed (refetch settled,
    final chunk streamed) — never at issuance (PR-5, I-2; extends rule 21's honesty to
    timing).
86. **Copy describing a clock, zone, or schedule renders the effective stored setting.**
    Any help text that tells the operator what time base applies must read the setting
    that governs it, not a static guess about the deployment (U-1; sharpens rules 53/55
    for time).
87. **A guarded startup replay is mirrored on every runtime replay of the same data.**
    When startup wraps a stored-value replay in a tolerant guard (malformed cron, bad
    zone), every settings-save or reschedule path replaying the same stored values carries
    the same guard, so a save can never 500-and-half-apply what boot survives (PR-4;
    extends rule 55's side-entrance principle in the other direction).

## Blockers from the fifth review pass

Direct constraints from the whole-backend review (`docs/CODE_REVIEW.md`), merging both of
its rule sets: the 23 carried from its first pass and the 18 new in its second. Written as
blockers, not suggestions.

Four of its 41 were already law here and were folded into the rule they duplicate rather
than restated (its 2 → rule 57, its 3 → rule 63, its 8 → rule 24, its 12 → rule 34); pairs
governing one mechanism were merged. **Several are worded against what was actually built,
not what the review proposed** — where remediation went a different way, the rule follows
the code. The sharpest case is rule 96: the review's own proposed fix would have read
unreadable evidence as "nothing was wrong," and the rule says the opposite. Where one
sharpens an earlier rule, the newer, more specific obligation governs.

88. **Case-fold both sides of every label, tag, collection, or list-name match.** When one
    side of a lookup is lower-cased, the other must be too. Lower-casing the source but not
    the operator's configured value is a fail-open protection bug: the protection stops
    matching and nothing announces it. Every new name-matching path ships a mixed-case test
    (B-1).
89. **Every windowed list read goes through the complete-or-raise paging helper.** Raw
    `server.query(...)` and raw multi-id metadata reads that can silently truncate are
    banned; page through `clients/plex.py`'s `_iter_pages` or assert `totalSize`
    completeness, and give any unbounded loop a page backstop (`MAX_HISTORY_PAGES` is the
    model). A truncated read of a protection source is a protection that quietly stopped
    covering most of the library (B-4, I-1, PR-6; sharpens rule 56).
90. **A populated container that filters down to zero usable items is a failure, not an
    empty success.** Rule 27 covers container-missing and malformed-body; this is the third
    case, where the body parses and every row in it is unusable. Distinguish it before any
    atomic `DELETE` + reinsert of protection membership: with members already stored,
    preserve them and degrade (B-5; sharpens rule 27).
91. **A settings or config read *failure* is not the same as "nothing configured."** On any
    safety-scoping path, a read error degrades the snapshot; only a successful read that
    finds nothing may fall back to the permissive default. A transient error must never
    silently widen what can be reaped, and copy calling the wider scope the "safe" fallback
    is wrong: widening is the condemn direction (PR-1; sharpens rules 2/65).
92. **Degradation is detected by a typed flag on the context, never by substring-matching a
    free-text reason.** Any `"some-source" in " ".join(reasons)` coupling between producer
    and consumer is a blocker, because the reason string is operator copy and will be
    reworded. Carry an explicit boolean (`activity_degraded`) (H-1, B-11).
93. **`Absent` means "we looked and there is genuinely nothing"; a source that could not be
    read is `Unknown`.** Never route a read failure to `Absent`: it withdraws the
    protection library-wide and prints a why-panel asserting a check that never ran, while
    `Unknown` blocks the gate and takes the full keep discount. Conversely, a genuine
    `Absent` on a numeric signal routes to `NOT_APPLICABLE` (evaluated, weight retained,
    coverage intact) as `SEASON_RANK` and the graded custom path already do, never to the
    `UNREADABLE` branch. Degrading the snapshot is necessary but not sufficient, and a
    comment claiming degradation already prevents this is the bug (B-7, B2-12; sharpens
    rules 2/28).
94. **Every `WHERE col IN :keys` over a scan-sized set is chunked at 500 or fewer.** An
    unchunked expanding bindparam overflows SQLite's variable ceiling and aborts the scan;
    chunk it, or express the filter as an anti-join. A new `parent_rating_key`-style filter
    also needs its covering index. Reconcile a `cache.db` index by name and create the
    missing one in place; never bump the column-shape tuple to force it, which drops the
    whole mirror (P-1, P-3, P-4).
95. **Every numeric API bound is validated at the boundary with `ge`/`le`, and every
    destructive-path list or string carries `max_length`.** A `min()` cap with no floor
    lets `limit=-1` become `LIMIT -1`, which is unbounded (B-8, PR-8).
96. **A why-panel extractor never raises a row off the queue, and its fallback resolves
    toward keeping.** Guard every `json.loads` plus model construction on a stored
    explanation with `(ValueError, TypeError)`. The value it falls back to is the
    *conservative* one, never the permissive one: a match record that is present but
    unreadable is a BAD match that holds the reap, not an absent one that clears it.
    Genuinely absent stays permissive; unreadable does not. Surface the unreadable state to
    the operator rather than printing a fabricated number in its place (PR-7, B-10).
97. **Anything that counts what was deleted counts the file's removal, not the bookkeeping
    that follows it.** A live re-resolve that returns no files is `_mark_skipped` ("no files
    resolved; kept"), never an approved size counted as deleted, and it never overwrites an
    already-VERIFIED step. In the other direction, a step whose file is confirmed gone but
    whose follow-up (exclusion, refresh) failed still charges the rolling caps: it stays
    FAILED, because marking it VERIFIED would make the journal claim a verification that
    explicitly failed, and the charge rides on the durable `file_removed_at` column instead
    (B-9, B2-10, PR2-1; sharpens rules 5/30).
98. **Throttles and the Argon2 gate bind at the granularity of the thing being abused.**
    Every unauthenticated, state-establishing endpoint is throttled per-IP (`plex/start`
    and `plex/poll` exactly as `/local` and `/recover`), and outbound-amplifying routes cap
    per-IP resource creation. The concurrency gate acquires one slot per *hash*, not per
    request: a gate wrapping a loop of N Argon2 verifications bounds nothing. A full gate
    returns 503 and must never be allowed to register as a failed attempt, or the DoS
    defense becomes the lockout (S-1, S-4; sharpens rule 11).
99. **The scrubber covers path-embedded secrets, and nothing renders a record the scrubber
    has not seen.** Add the webhook path shape to `_redact_str`, so a token in a URL path is
    scrubbed whatever log key it rides under. Redaction runs *after* exception formatting on
    both paths, the stdlib handler and the structlog chain alike: an HTTP error's `str()`
    embeds the full request URL, so a processor order that redacts before `format_exc_info`
    (or a handler that appends `self.format(record)` beside an already-redacted copy) writes
    the secret in the clear (S-2, S2-1; sharpens rule 13).
100. **Key or salt material that is present but unreadable refuses to boot; it never
     regenerates and proceeds.** Regenerating silently bricks every credential written
     under the prior material. Raise with an actionable message and surface it in the UI
     safety state. Genuinely *missing* material is a different case and may still be
     generated: absent is a first run, corrupt is a disaster (S-5; sharpens rule 2).
101. **A forwarded request header that changes an auth or security decision is trusted only
     from a configured trusted proxy.** `X-Forwarded-Proto` passes the same
     `trusted_proxies` check `X-Forwarded-For` already does (S-7).
102. **A task created with `create_task` has a done-callback that logs its exception.** A
     fire-and-forget startup or maintenance task must not swallow a raise at GC time
     (PR-12).
103. **A hardcoded list that mirrors the model or schema set carries a drift guard.** The
     restore auth-purge list, generated-asset manifests, and server-defined id lists either
     derive from one declaration or are covered by a test that fails when the set changes.
     When the guard flags a member, classify it in writing as considered-and-kept rather
     than silencing it (R-3; sharpens rules 66/68).
104. **A value derived two ways in two modules is derived once in a shared helper, and the
     helper defines what a record lacking it thaws as.** Dormancy days
     (`engine/dormancy.py`), condemn/score/coverage, and any parallel field list
     (`_OBS_FIELDS`) have exactly one derivation; prefer `dataclasses.fields(...)` over a
     hand-maintained parallel list. Moving a derivation to the write side moves the problem
     to the read side, so state the thaw explicitly: a key a stored snapshot predates is
     `Unknown`, never `Absent` and never a `KeyError` (R-1, R-2; sharpens rule 3).
105. **A stored policy body that gains a protection-bearing field ships a loader shim in the
     same change, and the shim degrades the scan.** When a field moves out of a gate row
     into the body (as the rating bars did), a body written before the move is migrated on
     load, keyed on the raw key being *absent* (an explicit `[]` is an operator who cleared
     it deliberately, rule 1) and never on `schema_version`, which cannot discriminate
     across a change that did not bump it. Recover only where something actually was
     protecting: a *disabled* gate is left alone, since nothing was protecting anything
     either way and there is no reason to degrade a scan over it. The migrated body sets the
     `ActivePolicy.repaired` flag, degrades the scan, and opens the editor on it as an
     unsaved draft. A protection that silently evaluates to "nothing configured" is the
     worst outcome this codebase has (B2-1; sharpens rule 65).
106. **Every *spelling* of an id the item carries goes into every lookup, on the movie path
     exactly as on the TV path.** An item holding both `imdb_id` and `plex_imdb_id` is
     looked up with `item.imdb_id or item.plex_imdb_id`; passing one where two exist is a
     fail-open protection bug. Rule 29 covers id *kinds*; this covers two sources for one
     kind (B2-6; sharpens rule 29).
107. **A field offered in the policy vocabulary is populated by the fact builder for every
     media type it is offered on.** A `FieldSpec` with no `media_types=` is offered on both
     policies; if the season builder hardcodes it `Absent`, restrict the spec in the same
     change, on *both* lanes. Removal weights sum to a fixed 100, so a condemn rule on an
     always-`Absent` field permanently depresses every score in that media type rather than
     merely never firing. Operators holding a stored rule that just became unofferable are
     warned, not silently dropped (B2-3; sharpens rule 35).
108. **A text condition value is rejected at the save boundary when it strips to empty.**
     `contains ""` matches every item and lands the rule's full weight library-wide; `in ""`
     can never match and reports as a green "checked, did not fire." Reject
     `value.strip() == ""` for CONTAINS/IN, and reject an IN target whose split yields no
     elements, so a comma-only list cannot pass (B2-4; sharpens rule 32).
109. **An identity tier that can corroborate a bind is computed even when an earlier tier
     already bound, as a cross-check only, never as an originator.** Pass the binding ids
     explicitly rather than reordering a priority tuple, so a corroborating id kind can add
     an abstain but can never originate a bind. A `tier1 is None` guard in front of a
     corroborating tier makes the documented contradiction veto structurally undetectable.
     A multi-hit tier is silence, not a contradiction, and a hit inside the earlier tier's
     merged group is agreement (B2-5, B2-7; sharpens rule 6).
110. **Every client method maps its failures to the client's domain error type.** One read
     that lets a raw transport exception escape defeats every `except <Domain>Error` in the
     call chain. A method documented "never fatal" catches `Exception`, not one mapped type
     (B2-2; sharpens rule 9).
111. **The executor's send loop and `execute()` each carry a catch-all that records terminal
     state.** An unmapped exception after a file is already deleted must not leave the step
     `SENT`, the run `EXECUTING`, and the report `None` with nothing able to reconcile it.
     Per-item surprises funnel through `_fail`; run-level surprises record `ABORTED` and
     return the report rather than re-raising into a caller that will not persist it
     (PR2-1; sharpens rule 26).
112. **The executor re-reads the operator's spare decisions before every item.** A decision
     map loaded once at run start means a Spare clicked during a multi-minute reap is
     ignored and the file is deleted. Refresh only the per-item spare and effective-set
     checks, intersected with the frozen run-start set so the refresh can only ever *remove*
     items, never add one the operator never approved; cap math stays on the run-start set
     (rule 30). Route it through the production `condemned.effective_verdict`, never a
     second membership copy (B2-9; sharpens rules 2/22).
113. **A run's approval is bound to the policy it was planned under.** `run.policy_hash` is
     recorded *and enforced at execute time*, with operator copy telling them to re-scan,
     since a policy edit does not trigger a scan on its own. A plan built after the edit is
     refused too, not just one built before it. Code and comments disagreeing about a safety
     binding is itself the blocker: never leave a hash recorded and unread (B2-8; sharpens
     rules 7/24).
114. **A sleep, retry budget, or allocation whose size comes from a remote server is
     clamped.** Clamp to a ceiling *and* to the caller's remaining deadline.
     `notify/discord.py`'s `_MAX_RETRY_AFTER` is the pattern; any other site honoring
     `Retry-After` matches it (S2-2).
115. **A protection-list slug that changes shape disables its predecessor in the same
     transaction.** Slugs derived from operator settings (match mode, instance id) leave
     orphaned rows that `enabled = 1` keeps protecting forever, so a tightening the operator
     saved never takes effect. Either disable every slug not produced by the current run, or
     keep the setting out of the slug. Retire only on a *successful* sync, and only for a
     family whose source was actually reachable: a failed sync's slug is exactly the
     membership that must survive (B2-25).
116. **A degraded snapshot's side effects are gated with its plan.** Un-plannable also means
     un-announced: grace clocks, the Leaving Soon shelf, and Discord all read the condemned
     set, and none may act on evidence the scan itself declared untrustworthy (B2-26;
     sharpens rules 2/8).
117. **A gate or option the operator can enable must be able to fire.** If every fact
     builder sets its input `Absent`, either wire the input or retire the gate: remove it
     from `GATE_TYPES` and refuse it in `build_gates`, keeping its `GateId` only so stored
     explanations still decode, and refuse to scan under a policy that enables it. A
     protection that is built, evaluated, hashed, and can never keep a file is worse than
     one that does not exist, because the operator counts on it (B2-15; sharpens rule 38).
118. **Every deletion-path interlock has a test that fails when the interlock is deleted.**
     Write the test against the interlock function directly when the guard upstream makes
     it unreachable through the public path: an unreachable tripwire with no test is one
     refactor away from silently gone. This covers the route-to-planner conversion of an
     empty selection as much as the byte cap itself. Where an interlock's two arms are
     genuinely indistinguishable at that function's interface, say so in the test's own
     docstring and name it for what it does pin. A test that cannot discriminate must never
     be left reading as a proof (T-1, T-3).
119. **A test never re-implements production logic, never asserts on a bare `Exception`, and
     never rests on an environmental accident.** Agreement tests call the real function.
     Expectations are explicit tables written from the spec, not transcriptions of the
     branch structure they check. Where a test mirrors a production pipeline, extract the
     shared part and point both at it rather than patching the copy: the divergence is
     otherwise invisible precisely when the fixture's own baseline is generated by the copy.
     Assert the domain error and its message, never `pytest.raises(Exception)`. A test whose
     evidence is a closed port, a non-root uid, or any other property of the machine is not
     a proof: stub the boundary and assert what was actually sent, and add a case no
     environment can skip (T-2, T-4, T-5, T-7, T-8; sharpens rule 22).

## Blockers from the two-level spare pass (2026-07-25)

Direct constraints from the review of how a season row reads a spare when its own and its
show's overlap. Written as blockers, not suggestions; where one sharpens an earlier rule
(120 → 50/61, 121 → 50, 122 → 46) the newer, more specific obligation governs.

120. **Precedence answers which decision is read; it never answers what will happen.** A
     surface that COLORS a row or asserts its fate reads the *covering* spare
     (`spare_covers_until`, from `whitelist.covering_spare_expiry`: own or show, whichever
     runs longer, forever winning outright). A control reads the spare in force by
     precedence (`spare_expires_at`, `effective_spare_expiry`), because that is the key it
     toggles and clears. Reading one field for both jobs drew dashed "expired" over a file a
     show spare keeps forever, and promised "then Reaper judges it again" about a
     re-judgment that changes nothing. A level must be *spared* to contribute cover, so a
     season spare lapsing under a show set to REAP still reads expired: there the file
     really is handed back. Derive it server-side and put it on every shape that colors,
     `GroupSeasonMark` included -- threading a show's decision down to each strip square is
     the `showReapReaches` bug waiting to happen (sharpens rules 50/61).
121. **A control that stops being a toggle stops looking like one, in all three signals at
     once.** When a press no longer undoes the state shown -- a spent spare, whose press now
     sets a fresh one -- the fill, `aria-pressed` and the click handler move together off
     one `pressed` flag; never leave a pressed-looking button whose press does something
     else. The undo it displaced moves to a surface with room to name it (the length menu's
     "Clear this spare"), never disappears. A count is how much is LEFT, so "0d" is not a
     smaller "27d" and must never sit in a lit button: it read as an active decision with
     none of itself remaining (sharpens rule 50).
122. **A control that knows only its own level never asserts the item's fate.** The Spare
     button's tooltip states what happened to *its* spare and what a press does
     (`spareRemaining().expiredOn`), never `note`'s "still kept until the next scan judges
     it again", which is false wherever a show-level spare outlasts it. What is still
     keeping the file is the covering spare's question, answered beside it by the row's chip
     and `KeptByShowNote`. Same reason a spent spare draws no resting mark: the mark is a
     decision in force, and that one no longer is (extends rule 46).
123. **Every branch a control can clear names what clearing does, in both directions.**
     `KeptByShowNote` told the operator "clearing this one won't remove it" when clearing was
     harmless and said nothing when clearing dropped the file onto the reap list. Warning
     only on the safe side is backwards for a codebase whose every ambiguity resolves toward
     keeping the file: a new branch ships its consequence clause, and the destructive one
     ships it first.
