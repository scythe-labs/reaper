# Reaper — guide for agent sessions

Reaper is a self-hosted web tool that finds media nobody watches, **explains why it thinks
each item is expendable** (every signal, and every protection that was checked and did
*not* fire), lets you review and approve, then removes it safely *through* Sonarr/Radarr and
refreshes Plex. Python 3.13 / FastAPI backend + React 19 / Vite frontend, one container.

> **Prime directive: Reaper deletes irreplaceable data from a server other people depend on.
> Every ambiguity resolves toward keeping the file.** When in doubt, fail closed.

## Where the engineering rules live

147 numbered blockers, adversarially verified across seven review passes. **The numbers are
permanent** — code and comments cite them (`rule 28` in `snapshot.py`), so never renumber and
never reuse a number for a different rule. A comment may only cite a rule that exists;
`tests/test_repo_hygiene.py` fails on one that does not. New rules append to the scoped file
that governs them, from 148.

They live in `.claude/rules/`, scoped by `paths` frontmatter so each set loads when you read a
file it governs, and a file must be read before it can be edited. **The scoping is the budget**:
a set that loads for work it does not govern is paid for on every unrelated session, so a
cluster large enough to notice earns its own file.

| File | Governs | Rules |
| --- | --- | --- |
| `.claude/rules/backend.md` | `src/reaper/**/*.py`, `alembic/**/*.py` — safety path, engine, evidence, clients, persistence | 1–6, 8–10, 13, 22, 23, 26–35, 38, 52, 55–59, 63, 65, 70, 71, 73, 77, 78, 81, 82, 87–97, 102–117, 124, 127–129, 131, 140, 142, 143 |
| `.claude/rules/auth.md` | `src/reaper/auth/**`, `secrets.py`, `logbuffer.py`, `services/{backup,restore}.py`, `api/settings.py` — credentials, sessions, at-rest key material | 11, 12, 14, 74–76, 83, 84, 98–101, 125, 126, 130 |
| `.claude/rules/frontend.md` | `frontend/src/**/*.{ts,tsx,css}` and `frontend/index.html` (rule 69 governs it) — UI grammar, gating surfaces | 17–20, 36, 39–47, 51, 53, 54, 60–62, 66, 67, 69, 79, 80, 85, 86, 138, 139, 146 |
| `.claude/rules/review-queue.md` | `ReviewQueue`, `OverrideControls`, `ShowPanel`, `SeasonList`, `reviewFate` — fate, overrides, the two-level spare | 48–50, 120–123 |
| `.claude/rules/tests.md` | `tests/**/*.py`, `frontend/src/**/*.test.ts{,x}`, `frontend/src/test/**` — test discipline | 37, 118, 119, 132, 133, 135–137, 141, 145, 147 |

Eleven rules bind every file and stay here, under *Rules that apply everywhere*. Where two rules
overlap, the more specific one governs. Read the governing file before working in a tree you
haven't touched this session.

This table, each file's `Holds` line, and the count are one fact written four times, so
`test_every_index_of_the_rules_matches_the_rules` checks all of them and names each one that
disagrees. They had already drifted (rule 144).

**A new rule earns its number, and most candidates do not.** Before appending 148, in order:
**extend an existing rule** if one covers the class — rules 127, 140, 142 and 143 each described
rule 72's sweep at a different target, five rules where four instances do it; **write the gate
instead** if the violation is greppable, since `test_repo_hygiene.py` binds an author who never
read the rules and prose cannot; then **append**, instruction first, incident cut to a clause. A
rule narrating the gate that enforces it pays twice for one constraint.

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
- **Mock up UI/UX before touching code.** Present a rendered, self-contained HTML artifact in
  Reaper's look and feel and iterate on *that* until it is approved — only then edit frontend
  code.
  Iterating on a picture is far cheaper than iterating on a diff.
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
- **A bug you are not fixing becomes an issue before the session ends — don't wait to be
  asked.** A defect left in a transcript dies with the session, and "I flagged it in the
  summary" is not a record. Every defect leaves fixed or filed, in *every* session, not just
  `/reaper-review`: most are found while building something else, which is when the temptation
  to note it and move on is strongest. Filing needs no approval; say what you filed in the
  summary rather than asking first. **A candidate you could not demonstrate is filed too, as a
  question** — `Status/Need More Info`, no `Reviewed/` label, because an issue asserting a
  defect must not assert what nobody showed. Promoting it later is one label edit *and* the
  evidence that settled it, written into the issue; killing it is a close as `Reviewed/Invalid`.
  The `reaper-review` skill's *Opening issues* section holds every mechanic, label and cap, and
  binds every session rather than only a review pass — read it before filing. It is stated once,
  there, so nothing here can drift from it.
- **Commit as you go, in focused commits — don't wait to be asked.** One commit tells one
  story, and a fix ships with the test that pins it and the doc line it corrects, because those
  are the same story. Nothing else rides along. **The reviewer's attention is the scarce
  resource, so balance it both ways:** don't dribble one change across a string of tiny commits,
  and don't lump unrelated work into a big one. Aim for the fewest commits that each still stand
  alone and read clearly. End commit messages with the `Co-Authored-By` trailer.

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

**72. A fix lands on every sibling of the thing you fixed, in the same change.** Grep for the
siblings before closing, then fix or defer each *in writing*; a "when next touched" deferral is
honored the moment ANY commit touches the sibling, not when someone remembers. On its own this
covers a copied **function** (paging loops, section resolution, error mapping). Four backend
rules say what "sibling" means otherwise: **127** an interlock whose docstring claims *every*,
**140** a value you re-qualified, **142** a discriminator you typed, **143** a set whose
membership you changed. Same sweep each time, so finding one usually means checking the rest.

**134. A gate is judged by its exit code, never by the output you kept.** A pipeline exits with
its LAST command's status, so `npm --prefix frontend run build | tail -4` reports `tail`'s
success while a failing `tsc` scrolls past, and anything chained after it with `&&` then runs on
a broken tree — which is exactly how a TypeScript error once reached a commit. Run each gate on
its own and read the status (`cmd > /tmp/out 2>&1; echo "exit: $?"`), and only pipe a command
whose success you are not currently deciding on. `| head` is worse still: it SIGPIPEs the
writer, so the command dies partway and reports that as its own result. This binds every
verification in this file, and reporting a gate green on a pipe's exit code is a false statement
about the work.

**144. Generating one copy of an operator-facing claim raises the risk on every copy you did
not generate.** Rule 72 sweeps siblings of a *function* and rule 103 guards a *list* mirroring a
declaration; this is the same obligation for a *sentence*. One fact about what the app does is
normally stated in several places — a help paragraph, an API description, the error body that
fires when it is enforced — each written by someone reading a different one. Deriving one from
the code does not make the rest safe; it makes them **more** dangerous, because the derived copy
is demonstrably correct and vouches for a consistency that does not exist. The API key fence is
the case: its auth box was generated from the allowlist, and all three ungenerated siblings were
then wrong in the same reassuring direction — two denied capabilities a key actually has, and the
third promised a try-it-out button that cannot send a write at all. So grep the sibling copies of
any sentence you are about
to generate, and either generate them from the same declaration or **point the generated one's
test at them by name** — a failure message naming the other file costs one line, where a comment
asking future authors to remember does nothing. The direction is not luck: a rounded claim is
written to reassure, so it fails toward telling the operator the app is safer than it is.

## Branch & merge workflow

- **`dev` is the default branch, and all work lands there.** Every unit of work gets its own
  branch that merges back into `dev`.
- **Cut that branch from the latest upstream `dev`, and confirm what "latest" is rather than
  assuming.** Ask the remote first — `git fetch origin`, then branch explicitly off the remote
  ref, never off whatever the working copy happens to be sitting on:

  ```
  git fetch origin
  git checkout -b <branch> origin/dev     # not `dev`, not the current HEAD
  ```

  A local `dev` is a cache of the answer, and nothing in a session refreshes it. **A worktree
  makes this sharper**: the session opens on whatever branch that worktree was cut for, often an
  old feature branch, so branching from HEAD carries someone else's commits in and the diff
  reads as yours. Verify with `git log --oneline origin/dev..HEAD` before you start — empty
  means current, anything else means a stale base.
- **Re-check before you open the PR, because `dev` moves while you work.** `git fetch origin &&
  git rebase origin/dev` immediately before pushing, then re-run the gates: a branch that was
  current when it was cut can still merge into a tree its tests were never run against.
- **A pull request carries its `Kind/` and `Priority/` labels, same vocabulary as an issue**
  (`tea pr create -L "Kind/Bug,Priority/Critical"`, or `tea pr edit <n> --add-labels` after the
  fact). A PR closing an issue inherits that issue's two; `Reviewed/` is issue triage and stays
  off a PR. Reviewers filter the queue the same way the backlog is filtered, and an unlabeled
  PR is missing from it.
- **Landing a branch into `dev` is CI-gated, and the style is `rebase`.** Poll the API for the
  branch head's status before merging (a fresh PR sits `pending` for minutes), then `tea pr
  merge --style rebase <n>`, which keeps the commit message you wrote; `merge` (the default) and
  `squash` both compose a new one from the PR title and body and throw that text away. Three
  round-trip costs:
  a **draft will not merge** and there is no ready-for-review flag, so strip the `WIP:` prefix
  with `tea pr edit <n> --title "…"`; `tea` has no `--delete-branch`, so `git push origin
  --delete <branch>` after; and **a rebase-merge lands a sha CI never tested**, since the branch
  replays onto whatever `dev` is now, so landing several PRs back to back ends with the gates
  re-run on the merged `dev` rather than three green per-branch runs.
- **`main` is release-only.** Never push to `main` directly. Promote `dev` with a pull request
  from `dev` → `main`, **squash-merged**, so `main` reads as a sequence of releases while the
  granular history lives on `dev`: `tea pr create --base main --head dev`, then `tea pr merge
  --style squash <n>`, and delete any temporary feature branch after.

## Verification gates (these mirror CI)

```
uv run ruff check .
uv run ruff format --check .
uv run mypy src/reaper                 # src only; tests are not type-checked
uv run pytest
uv run alembic upgrade head            # then `alembic check` for model/migration drift
npm --prefix frontend run lint         # eslint; the two react-hooks rules are errors
npm --prefix frontend run format:check # prettier; `run format` writes
npm --prefix frontend run test         # vitest component tests (the execute gate first)
npm --prefix frontend run build        # tsc --noEmit, then vite build
```

Run the relevant subset while iterating; run the full set before a commit. **Always run the
writing form of both formatters before staging — `uv run ruff format .` and `npm --prefix
frontend run format`, not just `--check` — format failures are the most common CI break.**
When a change is observable in the app, *drive it end-to-end* (the `verify` skill), don't
stop at green tests.

**Read the exit code, not the tail of the output** — rule 134, and the one that silently
defeats every other gate on this list.

**A green `npm run test` is not a quiet one.** Vitest's console interception drops test console
output on some Node versions — nothing on Node 26, printed on CI's pinned Node 24 — so `act()`
warnings and unhandled rejections are invisible exactly where you would act on them, and 302
"no queryFn" warnings once piled up behind a green suite. Run `npx vitest run <file>
--disableConsoleIntercept` to see them locally, or read the CI log. Rule 135 is the standing
answer: a test with something to tell you must fail, not warn.

**Asking whether CI is green** is far cheaper than reading a log: `$B/commits/<sha>/status`
returns a combined `state` plus one entry per job, and it is the merge gate above. A read-only
`curl` with tea's token is the sanctioned way to get it; anything that *changes* a PR goes
through `tea`. **Which jobs appear depends on what the commit touched.** `ci.yml` (`check`,
`frontend`, `docker`) ignores exactly the paths `docs.yml` (`hygiene`, the repo-hygiene test
alone) claims — `docs/**`,
`**.md`, `.claude/**` — so a docs-only commit reports `hygiene` alone, and a missing `docker`
there is a skipped lane, not a stall. The two lists are complementary; edit one and you must
edit the other.

**Reading a CI log.** Gitea Actions, so `gh` does not reach it and `tea` has no log subcommand.
Fetch with tea's token (macOS: `~/Library/Application Support/tea/config.yml`, *not*
`~/.config/tea/`) against `$B` = `<host>/api/v1/repos/<owner>/reaper`. **The `id` in
`$B/actions/tasks` is a task id; `$B/actions/jobs/<id>/logs` wants a job id** — mixing them
returns an unrelated job's log, which reads as a real answer and cost one wrong diagnosis
already. Go through `$B/actions/runs/<run>/jobs` (the run id is the tail of a task's `url`) and
confirm `head_sha` against `git rev-parse HEAD`. Log timestamps are runner-local and trail the API's UTC by hours; match on the sha,
never the clock.

**Both halves of the tree are machine-formatted, so never hand-argue style.** `ruff format` owns
Python, prettier owns `frontend/`, both run in CI, and they share one width (prettier's
`printWidth` is 100 because that is ruff's `line-length`; `frontend/prettier.config.mjs` carries
the rest). Write code at whatever shape is clearest, run the formatter, stage what it gives you.
If a reformat sweeps files you did not touch, someone skipped the gate: land it as its own
commit and add it to `.git-blame-ignore-revs`. **Prettier's territory stops at `frontend/`** —
invoke it as `npm --prefix frontend run format`, never `npx prettier` from the repo root, which
finds no config, falls back to 80 columns, and reflows every hand-wrapped line of `CLAUDE.md`,
`docs/`, and the CI workflow. Markdown and YAML here are wrapped by hand and no formatter owns
them.

## Dev environment

- **API :8420, frontend :5173** (Vite proxies `/api`). In an interactive session start them
  via `.claude/launch.json` (`preview_start`, names `reaper-api` / `reaper-frontend`) — never
  hand-run dev servers.
- **Headless / background job (no `preview_start`):** run **`scripts/dev-local.sh`**. It
  applies migrations, boots both auto-reloading servers (API `--reload`, Vite HMR) against
  the shared real `data/`, waits for health, and prints the URLs. `down` stops them,
  `status` / `logs` inspect. The UI at :5173 is the live dev server, not a build;
  `npm run build` is a CI gate only. **A second instance beside a running one** is
  `REAPER_PORT` + `REAPER_WEB_PORT`, and the two move together: `REAPER_PORT` reaches
  uvicorn *and* Vite, whose `/api` proxy target reads it (`frontend/vite.config.ts`).
  Setting only the web port leaves the second UI talking to the first instance's API.
- API calls require the header **`X-Reaper-CSRF: 1`**; auth is a cookie session.
- Secrets live in a gitignored **`.env.local`**; `data/` (`reaper.db`, `cache.db`) is
  gitignored and rebuildable. Never paste real keys into the transcript or a commit.

## Architecture

- `src/reaper/clients/` — the **only** place HTTP lives (one sanctioned exception,
  `notify/discord.py`'s webhook POST; see rule 33). `GuardedTransport` (and its
  `GuardedSession` twin for plexapi) refuses any mutating request unless deletion is armed on
  the host **and** the executor declared the intent to the journal first.
- `src/reaper/engine/` — `gates` (hard, fail-closed protections), `signals` (soft weighted
  pressure; `score()` is **unsigned** pressure over a fixed denominator, so missing or
  keep-arguing evidence can only *lower* the score — a signed score off a neutral baseline
  inverts under failure, see the "Why unsigned" note atop `signals.py`),
  `verdict.decide_verdict` (the one condemn/abstain/protect decision), and the "why" record.
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
- `docs/LEARNINGS.md`, `docs/SIGNALS.md` — findings from real data. `SIGNALS.md` is cited from
  five places in `src/`: `engine/signals.py`, `engine/policy.py` (twice), `engine/gates.py`, and
  `api/routes.py`. Read it before touching any of them, and before the rewatch curve in
  `engine/backtest.py`.
- `docs/SIZE_TRUTH_PLAN.md` — the one feature plan still live (4 of 9 stages remain).
- `docs/history/` — frozen: the retired plan narrative and the review passes, including the
  finding IDs behind the numbered rules. Never edit an archived file to bring it up to date.
