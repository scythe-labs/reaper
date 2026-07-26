# Reaper — guide for agent sessions

Reaper is a self-hosted web tool that finds media nobody watches, **explains why it thinks
each item is expendable** (every signal, and every protection that was checked and did
*not* fire), lets you review and approve, then removes it safely *through* Sonarr/Radarr and
refreshes Plex. Python 3.13 / FastAPI backend + React 19 / Vite frontend, one container.

> **Prime directive: Reaper deletes irreplaceable data from a server other people depend on.
> Every ambiguity resolves toward keeping the file.** When in doubt, fail closed.

## Where the engineering rules live

133 numbered blockers, distilled and adversarially verified across six review passes. **The
numbers are permanent** — tests and source comments cite them by number (`rule 28` in
`snapshot.py`, `rule 88` in `tests/test_lists_matching.py`), so never renumber and never reuse
a number for a different rule. A comment may only cite a rule that exists: 37 comments once
cited rules 70–87 while the list ended at 69, making every one of them unverifiable.
`tests/test_repo_hygiene.py` now fails on a citation with no rule behind it. New rules append
to the scoped file that governs them and continue from 134.

They live in `.claude/rules/`, scoped by `paths` frontmatter so each set loads when you read a
file it governs — and since a file must be read before it can be edited, the rules for a file
are always in context before you change it:

| File | Governs | Rules |
| --- | --- | --- |
| `.claude/rules/backend.md` | `src/reaper/**/*.py`, `alembic/**/*.py` — safety path, engine, evidence, clients, auth, persistence | 1–6, 8–14, 22–23, 26–35, 38, 52, 55–59, 63, 65, 70–71, 73–78, 81–84, 87–117, 124–131 |
| `.claude/rules/frontend.md` | `frontend/src/**/*.{ts,tsx,css}` and `frontend/index.html` (rule 69 governs it) — UI grammar, the review queue, the two-level spare, gating surfaces | 17–20, 36, 39–51, 53–54, 60–62, 66–67, 69, 79–80, 85–86, 120–123 |
| `.claude/rules/tests.md` | `tests/**/*.py`, `frontend/src/**/*.test.ts{,x}` — test discipline | 37, 118–119, 132–133 |

Nine rules bind every file and stay here, under *Rules that apply everywhere*. Where two rules
overlap, the more specific one governs. Read the governing file before working in a tree you
haven't touched this session.

## Golden rules

- **Nothing identifying, anywhere** — code, docs, tests, and commit messages alike. Reaper
  ships to operators whose servers we will never see: never commit a real title, host, path,
  username, or stat. Live-testing findings are recorded as ratios and shapes, never
  fingerprints.
- **American English everywhere**, including identifiers and commit messages
  (`normalize_label`, `SeasonJudgment`). The only exceptions are names owned by someone else
  and spelled British at the source: `asyncio.CancelledError`, `aria-labelledby`.
- **Treat Reaper as production code.** It will be released; write for an unknown operator,
  never for one specific server.
- **Operator copy is read at a glance, never twice.** A phrase over a sentence, a sentence
  over two; lead with the outcome and leave the explanation to help text bound to the
  control. These surfaces are *scanned* while deciding what to delete, so long copy does not
  get read at all. After writing an operator string, cut it once more. Rule 21 governs its
  vocabulary, this rule its length.
- **Mock up UI/UX before touching code.** Present a rendered, self-contained HTML artifact
  that faithfully represents Reaper's look and feel, and iterate on *that* until it is
  approved — only then edit frontend code. Iterating on a picture is far faster and cheaper
  than iterating on a diff.
- **Ship additive, non-breaking migrations. Never make a tester rebuild their DB.** Testers
  run Reaper on real data, so the Alembic baseline (`22777b2b5015`) is **frozen** — never
  edit it. Every schema change is its own new revision chained onto the current head by
  `down_revision` (a nullable `ADD COLUMN`, a new table, a backfill), so `alembic upgrade
  head` on an existing database only ever adds. New columns are nullable or carry a server
  default, and the next scan backfills them; a not-yet-backfilled `NULL` reads as "unknown,"
  never as a wrong definite value. `cache.db` stays disposable and unmigrated.
- **A change that alters what the app *does* updates `docs/STATUS.md` in the same commit** —
  edit the line that is now wrong, never append beside it. Measured findings, including
  negative results, go to `docs/LEARNINGS.md`. `docs/README.md` says what belongs where: state,
  knowledge, and history have different lifespans and never share a file.
- **Commit only when asked**; end commit messages with the `Co-Authored-By` trailer.

## Rules that apply everywhere

**7 / 24. A comment may not claim a safeguard that is not implemented, and one that names a
safeguard cites the function implementing it** — verified to exist and to be called before
merging. If you cannot cite it, correct the comment in the same commit. A review pass once
found six safeguards that existed only as prose.

**15. The shipped artifact keeps building in CI.** Install from the committed lockfile with
digest-pinned base images; never let unpinned `>=` floors resolve fresh at build time. CI
runs `docker build`, so don't build the image locally to satisfy a gate.

**16. Every operator-configurable credential lives in the DB-backed, encrypted, UI-editable
surface and is documented in `.env.example`.** Never strand a setting as env-only and
undocumented while the UI advertises its outcome.

**21. Every operator-facing string is plain language** — frontend strings and backend
`detail`/message strings alike. Lead with the outcome and say what it means for their files.
Keep internal vocabulary out of it: no rating keys, no tmdb/imdb/tvdb ids, no
"collision"/"guard"/"coverage bp"/abstain-as-jargon. If a normal person wouldn't say it,
reword it. This binds notices, tooltips, empty states, and error text alike. **No em dashes
in operator-facing copy**: reword with a period, comma, or colon. Middots as separators
("70/100 · 20% of the score") and arrows ("Policy → Deletion") are fine. A string that is
plain but long still fails.

**25. Operator copy may only reference features that are wired.** Confirm the route or UI
path exists before writing text that names a mechanism (backtest, cap, interlock); a DB
constraint or schema for an unwired feature is a blocker, not a placeholder.

**64. Removing a surface removes its whole supply chain in the same change** — route,
schemas, client method, props, query-key invalidations, and comments naming it. Grep for the
query key and the prop name before closing.

**68. Generated assets ship with their generator.** A comment saying an asset is generated
names a committed, runnable script (`frontend/scripts/gen-icons.mjs`,
`scripts/policy_lab_extract.py`), and a drift test covers every generated artifact, not just
one.

**72. A hardening fix lands on every twin of the fixed function in the same change.** Before
closing a fix to a copied pattern (paging loops, section resolution, error mapping), grep for
the pattern's siblings and fix or explicitly defer each in writing. A "when next touched"
deferral is honored the moment ANY commit touches the twin, not only when someone remembers.

## Branch & merge workflow

- **`dev` is the default branch, and all work lands there.** Push to `dev`, or to a feature
  branch off `dev` that merges back into `dev`.
- **`main` is release-only.** Never push to `main` directly. To promote `dev`, open a pull
  request from `dev` → `main` and **squash-merge** it, so `main`'s history is a clean
  sequence of squashed releases while the granular history lives on `dev`. With the `tea`
  CLI: `tea pr create --base main --head dev`, then `tea pr merge --style squash <n>`, and
  delete any temporary feature branch after.

## Verification gates (these mirror CI)

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

Run the relevant subset while iterating; run the full set before a commit. **Always run
`uv run ruff format .` (not just `--check`) before staging — format failures are the most
common CI break.** When a change is observable in the app, *drive it end-to-end* (the
`verify` skill), don't stop at green tests.

## Dev environment

- **API :8420, frontend :5173** (Vite proxies `/api`). In an interactive session start them
  via `.claude/launch.json` (`preview_start`, names `reaper-api` / `reaper-frontend`) — never
  hand-run dev servers.
- **Headless / background job (no `preview_start`):** run **`scripts/dev-local.sh`**. It
  applies migrations, boots both auto-reloading servers (API `--reload`, Vite HMR) against
  the shared real `data/`, waits for health, and prints the URLs. `down` stops them,
  `status` / `logs` inspect. The UI at :5173 is the live dev server, not a build;
  `npm run build` is a CI gate only.
- API calls require the header **`X-Reaper-CSRF: 1`**; auth is a cookie session.
- Secrets live in a gitignored **`.env.local`**; `data/` (`reaper.db`, `cache.db`) is
  gitignored and rebuildable. Never paste real keys into the transcript or a commit.

## Architecture

- `src/reaper/clients/` — the **only** place HTTP lives (one sanctioned exception,
  `notify/discord.py`'s webhook POST; see rule 33). `GuardedTransport` (and its
  `GuardedSession` twin for plexapi) refuses any mutating request unless deletion is armed on
  the host **and** the executor declared the intent to the journal first.
- `src/reaper/engine/` — `gates` (hard, fail-closed protections), `signals` (soft weighted
  pressure, and `score()`: **unsigned** pressure over a fixed denominator, so missing or
  keep-arguing evidence can only ever *lower* the score, never a signed score off a neutral
  baseline, which inverts under failure — see the "Why unsigned" note atop `signals.py`),
  `verdict.decide_verdict` (the one condemn/abstain/protect decision), and the explainable
  "why" record.
- `src/reaper/services/` — `snapshot` (gather → freeze → hash → score), `planner` (build the
  journalled plan), `executor` (the real send + interlocks), plus grace, leaving_soon,
  scan_runner, whitelist.
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
`POST /api/runs/{id}/execute`; it requires the host armed and the exact content-bound
confirmation phrase, recomputed server-side. The scheduler never deletes. The executor's
interlocks (manifest re-check, caps that abort-not-truncate, the canary, the per-item
streaming veto and played-since-approval check) each resolve toward keeping the file.

## Where things are documented

- `docs/README.md` — what belongs in which file, and the rule that keeps them current.
- `docs/STATUS.md` — **start here.** What is true right now: milestones, open work, decisions
  locked. Small and edited in place.
- `docs/LEARNINGS.md`, `docs/SIGNALS.md` — findings from real data. Read `SIGNALS.md` before
  touching `signals.py`, `policy.py`, or `calibration.py`; it is cited from three places in `src/`.
- `docs/SIZE_TRUTH_PLAN.md` — the one feature plan still live (4 of 9 stages remain).
- `docs/history/` — frozen: the retired plan narrative and the review passes, including the
  finding IDs behind the numbered rules. Never edit an archived file to bring it up to date.
