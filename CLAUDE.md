# Reaper — guide for agent sessions

Reaper is a self-hosted web tool that finds media nobody watches, **explains why it thinks
each item is expendable** (every signal, and every protection that was checked and did
*not* fire), lets you review and approve, then removes it safely *through* Sonarr/Radarr and
refreshes Plex. Python 3.13 / FastAPI backend + React 19 / Vite frontend, one container.

> **Prime directive: Reaper deletes irreplaceable data from a server other people depend on.
> Every ambiguity resolves toward keeping the file.** When in doubt, fail closed.

**The conventions live in [`CONTRIBUTING.md`](CONTRIBUTING.md), and they bind a session here
exactly as they bind a human contributor.** Setup, the verification gates, commit and pull
request conventions, how operator-facing text is written, and the AI policy are stated there
once, for both audiences. This file holds what is specific to working inside the repository:
the numbered rules, the architecture, the safety model, and the documentation discipline.
Where the two touch the same subject, CONTRIBUTING is the copy to correct.
[`AGENTS.md`](AGENTS.md) is the entry point other agent tools read, and it routes back here.

## Where the engineering rules live

148 numbered blockers, adversarially verified across seven review passes. **The numbers are
permanent** — code and comments cite them (`rule 28` in `snapshot.py`), so never renumber and
never reuse a number for a different rule. A comment may only cite a rule that exists;
`tests/test_repo_hygiene.py` fails on one that does not. New rules append to the scoped file
that governs them, from 149.

They live in `.claude/rules/`, scoped by `paths` frontmatter so each set loads when you read a
file it governs, and a file must be read before it can be edited. **The scoping is the budget**:
a set that loads for work it does not govern is paid for on every unrelated session, so a
cluster large enough to notice earns its own file.

| File | Governs | Rules |
| --- | --- | --- |
| `.claude/rules/backend.md` | `src/reaper/**/*.py`, `alembic/**/*.py` — safety path, engine, evidence, clients, persistence | 1–6, 8–10, 13, 22, 23, 26–35, 38, 52, 55–59, 63, 65, 70, 71, 73, 77, 78, 81, 82, 87–97, 102–117, 124, 127–129, 131, 140, 142, 143, 148 |
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

**A new rule earns its number, and most candidates do not.** Before appending 149, in order:
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
- **Mock up UI/UX before touching code.** Load the `reaper-artifact` skill, then present a
  rendered, self-contained HTML artifact in Reaper's look and feel and iterate on *that* until it
  is approved — only then edit frontend code. The skill hands you the app's live tokens and
  component styles so the mockup matches without re-researching them.
  Iterating on a picture is far cheaper than iterating on a diff.
- **Ship additive, non-breaking migrations. Never make a tester rebuild their DB.** Testers
  run Reaper on real data, so the Alembic baseline (`22777b2b5015`) is **frozen** — never
  edit it. Every schema change is its own new revision chained onto the current head by
  `down_revision` (a nullable `ADD COLUMN`, a new table, a backfill), so `alembic upgrade
  head` on an existing database ordinarily only adds. New columns are nullable or carry a server
  default, and the next scan backfills them; a not-yet-backfilled `NULL` reads as "unknown,"
  never as a wrong definite value. `cache.db` stays disposable and unmigrated.
  **Schema still has to be able to leave, and rule 148 is the only door.** Additive-by-default
  with no exit is how dead columns accumulate forever behind a growing exclusion list whose
  job is to hide a `drop_column` from a reviewer — the wrong direction for a repository that
  fails closed. Removal is a two-release sequence, never an ad-hoc drop.
- **A change that alters what the app *does* updates `docs/STATUS.md` in the same commit** —
  edit the line that is now wrong, never append beside it. Measured findings, including
  negative results, go to `docs/LEARNINGS.md`. `docs/README.md` says what belongs where: state,
  knowledge, and history have different lifespans and never share a file.
  **`STATUS.md` records the state, never the route to it, and it is budgeted twice: 120 lines
  and 100 columns.** Both are enforced, because a line budget alone is not a size budget — the
  file sat at exactly 200/200 lines for days, so every new fact went onto a line that already
  existed, and since a markdown table row cannot wrap, one cell reached 21,210 characters and
  three cells held two thirds of the file. So: **a row holds a phrase, never a sentence**;
  reasoning behind a locked choice goes to `docs/DECISIONS.md` (one section per daggered row,
  and `test_repo_hygiene.py` checks the two agree both ways); and **closed work leaves the file
  entirely** — a shipped fix is not state, its record is the tracker and the code. Narrating a
  fix you just landed is the single way this file grows, and it reads as diligence, which is why
  it needs a rule and not just a budget.
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
- **Commit as you go, and keep the pull request focused — don't wait to be asked.** Branches
  are squash-merged, so a branch arrives on `dev` as one commit whose subject is the PR title
  and whose body is the PR description. **The pull request is the unit that tells one story**:
  a fix ships with the test that pins it and the doc line it corrects, because those are the
  same story, and unrelated work earns its own branch. Commits on the branch are working state
  that gets collapsed on the way in, so commit freely for your own sake and spend the writing
  care on the title and body, which are what survive. The title is a Conventional Commit and CI
  checks it. Put the `Co-Authored-By` trailer at the end of the **PR description**, since that
  is the text that becomes the commit message.

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
in operator-facing copy**: reword with a period, comma, or colon. **A middot does not separate
two facts either, and neither does a dash**: it is punctuation a screen reader may voice
("40 titles *middle dot* 1.2 TB freed") or drop entirely, so the separator is a comma, which is
read as the pause it looks like. This clause used to bless the middot, and 49 of them were
sitting in running text when someone finally listened to the app (#177). Arrows
("Policy → Deletion") are fine. A middot is still fine where it is *only* decoration — a dot
between chips, a placeholder for "no value" — and there it carries `aria-hidden`, because
nothing is lost by not hearing it. The test is whether a reader who never hears the character
still gets the sentence. A string that is plain but long still fails.

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
  (`gh pr create --label "Kind/Bug,Priority/Critical"`, or `gh pr edit <n> --add-label` after
  the fact). A PR closing an issue inherits that issue's two; `Reviewed/` is issue triage and
  stays off a PR. Reviewers filter the queue the same way the backlog is filtered, and an
  unlabeled PR is missing from it.
- **Landing a branch into `dev` is CI-gated, and the style is `squash`.** It is the only style
  the repository allows; merge commits and rebase merges are both turned off, so there is no
  choice to get wrong. Check the run with `gh pr checks <n>` before merging (a fresh PR sits
  pending for minutes; `--watch` blocks until it settles), then `gh pr merge --squash <n>`.
  **The squash commit takes its subject from the PR title and its body from the PR
  description** (`squash_merge_commit_title=PR_TITLE`, `squash_merge_commit_message=PR_BODY`),
  so those two fields *are* the commit message and nothing you wrote on the branch survives
  beside them. `.github/workflows/pr-validation.yml` checks the title parses as a Conventional
  Commit, because that subject is permanent and feeds the release notes. The head branch is
  deleted automatically on merge. A **draft will not merge**, but `gh pr ready <n>` clears it,
  so a `WIP:` title is a label for humans and no longer a thing to strip. One round-trip cost
  survives: **a squash-merge lands a sha CI never tested**, since the change is replayed onto
  whatever `dev` is now, so landing several PRs back to back ends with the gates re-run on the
  merged `dev` rather than three green per-branch runs.
- **`main` is release-only.** Never push to `main` directly. Promote `dev` with a
  **squash-merged** pull request from a promotion branch, so `main` reads as a sequence of
  releases while the granular history lives on `dev`. A bare `dev` → `main` PR worked exactly
  once: squash promotions never connect the two histories, so from the second promotion on,
  every file changed since the last release reads as an add/add conflict against the
  repository baseline. The promotion branch carries `main`'s tip without touching the tree,
  which moves the PR's merge base to `main` and shrinks the diff to what actually landed:

  ```
  git fetch origin
  git checkout -b promote-<version> origin/dev
  git merge -s ours origin/main -m "Merge main back so the promotion diffs against the last release"
  git push -u origin promote-<version>
  gh pr create --base main --head promote-<version>
  ```

  Then `gh pr merge --squash <n>`. The ours-strategy merge keeps `dev`'s tree bit for bit
  (release.yml verifies that before tagging) and never conflicts, whatever the histories look
  like. The head branch deletes itself at merge; the release tag keeps its head commit alive,
  and that commit's first parent is `dev`'s line, which is what scopes the next release's
  generated notes to the PRs that landed since this one.

## Verification gates

**The gate list and how to run it live in
[`CONTRIBUTING.md`](CONTRIBUTING.md#verification-gates)**, along with the rule that the
writing form of both formatters runs before anything is staged. Run the relevant subset while
iterating and the full set before a commit. When a change is observable in the app, *drive it
end-to-end* (the `verify` skill) rather than stopping at green tests. What follows is what a
session needs on top of that list.

**Read the exit code, not the tail of the output** — rule 134, and the one that silently
defeats every other gate.

**A green `npm run test` is not a quiet one.** Vitest's console interception drops test console
output on some Node versions — nothing on Node 26, printed on CI's pinned Node 24 — so `act()`
warnings and unhandled rejections are invisible exactly where you would act on them, and 302
"no queryFn" warnings once piled up behind a green suite. Run `npx vitest run <file>
--disableConsoleIntercept` to see them locally, or read the CI log. Rule 135 is the standing
answer: a test with something to tell you must fail, not warn.

**Asking whether CI is green** is far cheaper than reading a log: `gh pr checks <n>` lists one
row per job with its conclusion, and it is the merge gate above. **Which jobs appear depends on
what the commit touched.** `ci.yml`'s `changes` job classifies the diff once, into three lanes
(`docs/**`, `.claude/**` and `*.md` are prose; `manual/**` and `website/**` are the site;
everything else is code), and every other job in that file reads the verdict rather than
filtering itself: a prose-only commit runs `hygiene` alone, a code-only commit runs `check`,
`frontend` and `docker`, and a commit touching both runs everything.
**Two workflows outside it carry their own path lists and have to** — a `paths` filter decides
whether a workflow starts, so it cannot read another one's output. `codeql.yml` restates the
prose globs as `paths-ignore` once per trigger, and `docs-deploy.yml` carries the site lane by
another spelling. `tests/test_repo_hygiene.py` counts all three, so a fourth cannot arrive
quietly and this paragraph go stale again. **A skipped job publishes no check run at all**, which is why the required check is
`CI gate` — it runs on every commit, counts a skipped lane as a pass and a cancelled one as a
failure, and is the one job whose absence means something is genuinely wrong.
`pr-validation.yml` is separate, runs on every pull request whatever the paths, and reads the
title alone.

**Reading a CI log.** `gh run view --log-failed` is almost always the whole answer: it prints
only the failing steps. `gh run list --branch <branch>` finds the run, `gh run view <id> --log`
dumps it in full, and `gh run watch <id>` blocks until it finishes. **Confirm the run is for the
sha you think it is** (`gh run view <id> --json headSha`, against `git rev-parse HEAD`) — a
squash-merge or a fresh push means the newest run for a branch is often not the commit you are
holding, which is the one way to read a green log for someone else's code and believe it.

**Both halves of the tree are machine-formatted, so never hand-argue style.** CONTRIBUTING
carries the division of territory and prettier's root-directory hazard. The part that matters
mid-session: if a reformat sweeps files you did not touch, someone skipped the gate, so land it
as its own commit and add it to `.git-blame-ignore-revs`.

## Dev environment

- **API :8420, frontend :5173** (Vite proxies `/api`). In an interactive session start them
  via `.claude/launch.json` (`preview_start`, names `reaper-api` / `reaper-frontend`) — never
  hand-run dev servers.
- **Headless / background job (no `preview_start`):** run **`scripts/dev-local.sh`**. It
  runs preflight and then migrations, in the entrypoint's order, so a staged restore is
  applied here exactly as it is in the container; then it boots both auto-reloading servers
  (API `--reload`, Vite HMR) against the shared real `data/`, waits for health, and prints
  the URLs. `down` stops them,
  `status` / `logs` inspect. The UI at :5173 is the live dev server, not a build;
  `npm run build` is a CI gate only. **A second instance beside a running one** is
  `REAPER_PORT` + `REAPER_WEB_PORT`, and the two move together: `REAPER_PORT` reaches
  uvicorn *and* Vite, whose `/api` proxy target reads it (`frontend/vite.config.ts`).
  Setting only the web port leaves the second UI talking to the first instance's API. Those
  two ports also name its logs (`.dev-logs/api-<port>.log`, in the main checkout whichever
  tree booted it), so pass them to `down` / `logs` / `status` as well, or those commands
  answer for the default instance instead.
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
  locked. Small and edited in place, budgeted at 120 lines and 100 columns.
- `docs/DECISIONS.md` — why each locked decision is what it is, one section per daggered row of
  `STATUS.md`'s table. Read the row, then this if you are about to change the behavior it
  describes: several of these decisions were reversed once already, and the reversal is the part
  a future reader needs most.
- `docs/LEARNINGS.md`, `docs/SIGNALS.md` — findings from real data. `SIGNALS.md` is cited from
  five places in `src/`: `engine/signals.py`, `engine/policy.py` (twice), `engine/gates.py`, and
  `api/routes.py`. Read it before touching any of them, and before the rewatch curve in
  `engine/backtest.py`.
- `docs/CSS_SPLIT_PLAN.md` — the one feature plan still live (4 optional stages remain).
- `docs/history/` — frozen: the retired plan narratives and the review passes, including the
  finding IDs behind the numbered rules. Never edit an archived file to bring it up to date.
