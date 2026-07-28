# Reaper — guide for agent sessions

Reaper is a self-hosted web tool that finds media nobody watches, **explains why it thinks
each item is expendable** (every signal, and every protection that was checked and did
*not* fire), lets you review and approve, then removes it safely *through* Sonarr/Radarr and
refreshes Plex. Python 3.13 / FastAPI backend + React 19 / Vite frontend, one container.

> **Prime directive: Reaper deletes irreplaceable data from a server other people depend on.
> Every ambiguity resolves toward keeping the file.** When in doubt, fail closed.

## Where the engineering rules live

143 numbered blockers, distilled and adversarially verified across six review passes. **The
numbers are permanent** — tests and source comments cite them by number (`rule 28` in
`snapshot.py`, `rule 88` in `tests/test_lists_matching.py`), so never renumber and never reuse
a number for a different rule. A comment may only cite a rule that exists: 37 comments once
cited rules 70–87 while the list ended at 69, making every one of them unverifiable.
`tests/test_repo_hygiene.py` now fails on a citation with no rule behind it. New rules append
to the scoped file that governs them and continue from 144.

They live in `.claude/rules/`, scoped by `paths` frontmatter so each set loads when you read a
file it governs — and since a file must be read before it can be edited, the rules for a file
are always in context before you change it:

| File | Governs | Rules |
| --- | --- | --- |
| `.claude/rules/backend.md` | `src/reaper/**/*.py`, `alembic/**/*.py` — safety path, engine, evidence, clients, auth, persistence | 1–6, 8–14, 22–23, 26–35, 38, 52, 55–59, 63, 65, 70–71, 73–78, 81–84, 87–117, 124–131, 140, 142–143 |
| `.claude/rules/frontend.md` | `frontend/src/**/*.{ts,tsx,css}` and `frontend/index.html` (rule 69 governs it) — UI grammar, the review queue, the two-level spare, gating surfaces | 17–20, 36, 39–51, 53–54, 60–62, 66–67, 69, 79–80, 85–86, 120–123, 138–139 |
| `.claude/rules/tests.md` | `tests/**/*.py`, `frontend/src/**/*.test.ts{,x}`, `frontend/src/test/**` — test discipline | 37, 118–119, 132–133, 135–137, 141 |

Ten rules bind every file and stay here, under *Rules that apply everywhere*. Where two rules
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
- **A confirmed bug you are not fixing becomes an issue before the session ends — don't wait to
  be asked.** A defect left in a transcript is lost when the session is: nobody greps a dead
  conversation, and "I flagged it in the summary" is not a record. So every confirmed defect
  leaves the session one of two ways, fixed or filed. This binds *every* session, not just
  `/reaper-review` — most bugs are found while building something else, which is exactly when
  the temptation to note it and move on is strongest. Filing needs no approval; say what you
  filed in the summary rather than asking first. **Only what you confirmed**: a defect whose
  trigger you could not demonstrate goes to
  `.claude/skills/reaper-review/references/unproven.md` with the evidence that would settle it,
  because an issue asserts a defect exists. That skill's *Opening issues* section holds the
  mechanics — Gitea via `tea` (`gh` does not reach it), one issue per *fix* rather than per
  finding, a duplicate check on the `finding:` fingerprint first, the commit pinned with
  `--referenced-version`, **three labels (`Kind/` + `Priority/` + `Reviewed/Confirmed`, and
  priority ranks the operator's loss, not the size of the fix)**, and a title naming what the
  operator loses. An unlabeled issue is absent from every filter the backlog is triaged
  through, which wastes the filing.
- **Commit as you go, in focused commits — don't wait to be asked.** One commit tells one
  story: a feature, a bug fix, a cleanup that stands on its own. A fix ships with the test
  that pins it and the doc line it corrects, because those are the same story. Nothing else
  rides along. **The reviewer's attention is the scarce resource, so balance it both ways:**
  don't dribble one change across a string of tiny commits, and don't lump unrelated work into
  a big one. Aim for the fewest commits that each still stand alone and read clearly. End
  commit messages with the `Co-Authored-By` trailer.

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

**134. A gate is judged by its exit code, never by the output you kept.** A pipeline exits with
its LAST command's status, so `npm --prefix frontend run build | tail -4` reports `tail`'s
success and a failing `tsc` scrolls past; anything chained after it with `&&` then runs on a
broken tree. That is not hypothetical — a TypeScript error reached a commit exactly this way,
because the `&& git commit` after the pipe saw the pipe succeed. Run each gate on its own and
read the status (`cmd > /tmp/out 2>&1; echo "exit: $?"`, or a `for` loop over the gate names),
and only pipe a command whose success you are not currently deciding on. `| head` is worse
still: it SIGPIPEs the writer, so the command dies partway and reports that as its own result.
This binds every verification in this file, and reporting a gate as green on a pipe's exit code
is a false statement about the work.

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

  A local `dev` is a cache of the answer, not the answer, and nothing in a session refreshes it
  on its own. **A worktree makes this sharper**: a session opens on the branch that worktree was
  created for, which is routinely an old feature branch, so branching from HEAD there carries
  someone else's commits into the new branch and the diff reads as yours. Verify with
  `git log --oneline origin/dev..HEAD` before you start — empty means you are current, and
  anything else means you are about to build on a stale base. Getting this right by luck is the
  normal outcome, which is why it is checked rather than assumed.
- **Re-check before you open the PR, because `dev` moves while you work.** `git fetch origin &&
  git rebase origin/dev` immediately before pushing, then re-run the gates: a branch that was
  current when it was cut can still merge into a tree its tests were never run against.
- **A pull request carries its `Kind/` and `Priority/` labels, same vocabulary as an issue**
  (`tea pr create -L "Kind/Bug,Priority/Critical"`, or `tea pr edit <n> --add-labels` after the
  fact). A PR closing an issue inherits that issue's two; `Reviewed/` is issue triage and stays
  off a PR. Reviewers filter the queue the same way the backlog is filtered, and an unlabeled
  PR is missing from it.
- **Landing a branch into `dev` is CI-gated, and the style is `rebase`.** Ask the API whether
  the branch head is green before merging (below) — a fresh PR sits `pending` for minutes, so
  poll rather than assume — then `tea pr merge --style rebase <n>`, which keeps the commit
  message you wrote. `merge` (the default) and `squash` both compose a new message out of the
  PR title and body, throwing that text away; `squash` is right for `dev` → `main` below, where
  the granular history is deliberately collapsed, and wrong here. Three things that cost a
  round trip each: a **draft will not merge** and there is no ready-for-review flag, so strip
  the `WIP:` prefix with `tea pr edit <n> --title "…"`; `tea` has no `--delete-branch`, so
  `git push origin --delete <branch>` after; and **a rebase-merge lands a sha CI never tested**,
  since the branch is replayed onto whatever `dev` is now. Landing several PRs back to back
  therefore ends with the gates re-run on the merged `dev`, not with three green per-branch
  runs — same reason the re-check above exists, one step later in the sequence.
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
output entirely on some Node versions: on Node 26 a bare `console.error` inside a test prints
nothing, while CI (pinned to Node 24) prints it. So `act()` warnings, unhandled rejections and
library errors are invisible exactly where you would act on them — 302 React Query "no queryFn"
warnings once piled up behind a green suite, every one of them a component quietly rendering a
failed read. To see console output locally, run
`npx vitest run <file> --disableConsoleIntercept`; otherwise read the CI job log. Rule 135 is
the standing answer: a test that has something to tell you must fail, not warn.

**Asking whether CI is green** is a different question from reading a log, and a much cheaper
one: `$B/commits/<sha>/status` returns a combined `state` plus one entry per job. It is the
merge gate above. A read-only `curl` with tea's token is the sanctioned way to get it — but
anything that *changes* a PR goes through `tea`. **Which jobs appear depends on what the
commit touched.** CI runs in two lanes: `ci.yml` (`check`, `frontend`, `docker`) ignores
exactly the paths `docs.yml` (`hygiene`, the repo-hygiene test alone) claims — `docs/**`,
`**.md`, `.claude/**` — so a docs-only commit reports `hygiene` and nothing else, and a
missing `docker` there is a skipped lane, not a stalled run. The two path lists are
complementary by construction; edit one and you must edit the other.

**Reading a CI log.** CI is Gitea Actions, so `gh` does not reach it and `tea` has no log
subcommand. Fetch with tea's token (macOS: `~/Library/Application Support/tea/config.yml`,
*not* `~/.config/tea/`) against `$B` = `<host>/api/v1/repos/<owner>/reaper`. **The `id` in
`$B/actions/tasks` is a task id, and `$B/actions/jobs/<id>/logs` wants a job id** — mixing them
up returns some unrelated job's log, which reads as a real answer and cost one wrong diagnosis
already. Go through the run (`$B/actions/runs/<run>/jobs`, the run id being the tail of a task's
`url`) and confirm `head_sha` against `git rev-parse HEAD`. Log timestamps are the runner's
local time, so they trail the API's UTC by hours; match on the sha, never the clock.

**Both halves of the tree are machine-formatted, so never hand-argue style.** `ruff format`
owns Python, prettier owns `frontend/`, and both run in CI. They share one width: prettier's
`printWidth` is 100 because that is ruff's `line-length`. Everything else is prettier's
default, measured against this tree rather than assumed — `frontend/prettier.config.mjs`
carries the numbers. Write code at whatever shape is clearest, run the formatter, and stage
what it gives you. If a reformat ever sweeps files you did not touch, that means someone
skipped the gate; land the sweep as its own commit and add it to `.git-blame-ignore-revs`.

**Prettier's territory stops at `frontend/`.** Invoke it as `npm --prefix frontend run format`,
never `npx prettier` from the repo root: the config lives in `frontend/`, so a root invocation
finds none, falls back to an 80-column default, and reflows every hand-wrapped line of
`CLAUDE.md`, `docs/`, and the commented CI workflow. Markdown and YAML here are wrapped by
hand, where the line breaks carry meaning, and no formatter owns them.

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
- `docs/LEARNINGS.md`, `docs/SIGNALS.md` — findings from real data. `SIGNALS.md` is cited from
  five places in `src/`: `engine/signals.py`, `engine/policy.py` (twice), `engine/gates.py`, and
  `api/routes.py`. Read it before touching any of them, and before the rewatch curve in
  `engine/backtest.py`.
- `docs/SIZE_TRUTH_PLAN.md` — the one feature plan still live (4 of 9 stages remain).
- `docs/history/` — frozen: the retired plan narrative and the review passes, including the
  finding IDs behind the numbered rules. Never edit an archived file to bring it up to date.
