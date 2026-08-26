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
| `.claude/rules/auth.md` | `src/reaper/auth/**`, `secrets.py`, `logbuffer.py`, `services/{backup,restore}.py`, `api/{settings,plex,backup,deps,auth}.py` — credentials, sessions, at-rest key material | 11, 12, 14, 74–76, 83, 84, 98–101, 125, 126, 130 |
| `.claude/rules/frontend.md` | `frontend/src/**/*.{ts,tsx,css}` and `frontend/index.html` (rule 69 governs it) — UI grammar, gating surfaces | 17–20, 36, 39–47, 51, 53, 54, 60–62, 66, 67, 69, 79, 80, 85, 86, 138, 139, 146 |
| `.claude/rules/review-queue.md` | `ReviewQueue`, `OverrideControls`, `ShowPanel`, `SeasonList`, `reviewFate` — fate, overrides, the two-level spare | 48–50, 120–123 |
| `.claude/rules/tests.md` | `tests/**/*.py`, `frontend/src/**/*.test.ts{,x}`, `frontend/src/test/**` — test discipline | 37, 118, 119, 132, 133, 135–137, 141, 145, 147 |

Eleven rules bind every file and stay here, under *Rules that apply everywhere*. Where two rules
overlap, the more specific one governs. Read the governing file before working in a tree you
haven't touched this session.

This table, each file's `Holds` line, and the count are one fact written four times, so
`test_every_index_of_the_rules_matches_the_rules` checks all of them and names each one that
disagrees.

**A new rule earns its number, and most candidates do not.** Before appending 149, in order:
**extend an existing rule** if one covers the class; **write the gate instead** if the
violation is greppable, since `test_repo_hygiene.py` binds an author who never read the rules
and prose cannot; then **append**, instruction only, no narrative. A rule narrating the gate
that enforces it pays twice for one constraint.

## Golden rules

- **Nothing identifying in the tree** — code, docs, tests, and commit messages alike.
  CONTRIBUTING.md's Prose section states the rule in full.
  **Screenshots are the one exception, and only of the maintainer's own instance.**
  **Other people are never in the exception**: Scales lists the names of everyone who requested
  something, so a shot of it is cropped above that list, never retouched.
  `scripts/gen_screenshot_mockup.py` builds an invented version under
  `docs/media/review-queue-mockup.png`, kept so reversing this call is a one-line README edit.
- **American English everywhere**, including identifiers and commit messages, except names
  spelled British at the source (`asyncio.CancelledError`, `aria-labelledby`). CONTRIBUTING.md's
  Writing section is the full copy.
- **Treat Reaper as production code.** It will be released; write for an unknown operator,
  never for one specific server.
- **Operator copy is read at a glance, never twice.** A phrase over a sentence, a sentence
  over two. Lead with the outcome and leave the explanation to help text bound to the control.
  Help text obeys the same budget, and cut copy is cut, never parked in a code comment.
  After writing an operator string, cut it once more. Rule 21 governs its vocabulary, this
  rule its length.
- **Mock up UI/UX before touching code.** Load the `reaper-artifact` skill, then present a
  rendered, self-contained HTML artifact in Reaper's look and feel and iterate on *that* until it
  is approved — only then edit frontend code. The skill hands you the app's live tokens and
  component styles so the mockup matches without re-researching them.
  Iterating on a picture is far cheaper than iterating on a diff.
- **Ship additive, non-breaking migrations. Never make a tester rebuild their DB.**
  CONTRIBUTING.md's Database migrations section has the mechanics: the frozen baseline, one
  revision per change, nullable columns the next scan backfills.
  **Schema still has to be able to leave, and rule 148 is the only door.** Removal is a
  two-release sequence, never an ad-hoc drop; the reasoning lives in `docs/DECISIONS.md`'s
  Migrations section.
- **A change that alters what the app *does* updates `docs/STATUS.md` in the same commit** —
  edit the line that is now wrong, never append beside it. Measured findings, including
  negative results, go to `docs/LEARNINGS.md`.
  **`STATUS.md` records the state, never the route to it, and it is budgeted twice at
  120 lines and 100 columns**, both enforced. `docs/README.md`'s "A line budget is not a
  size budget" section has the incident that shaped the second cap. **A row holds a phrase,
  never a sentence.** Reasoning behind a locked choice goes to `docs/DECISIONS.md`, one
  section per daggered row, and `test_repo_hygiene.py` checks the two agree both ways.
  **Closed work leaves the file entirely** — a shipped fix is not state, its record is the
  tracker and the code.
- **A bug you are not fixing becomes an issue before the session ends — don't wait to be
  asked.** A defect left in a transcript dies with the session, and "I flagged it in the
  summary" is not a record. Every defect leaves fixed or filed, in *every* session, not just
  `/reaper-review`: most are found while building something else. Filing needs no approval; say
  what you filed in the summary rather than asking first. **A candidate you could not
  demonstrate is filed too, as a question** — `Status/Need More Info`, no `Reviewed/` label, because an issue asserting a
  defect must not assert what nobody showed. Promoting it later is one label edit *and* the
  evidence that settled it, written into the issue; killing it is a close as `Reviewed/Invalid`.
  The `reaper-review` skill's *Opening issues* section holds every mechanic, label and cap, and
  binds every session rather than only a review pass — read it before filing. It is stated once,
  there, so nothing here can drift from it.
- **A defect your own unlanded branch created is FIXED on that branch, never filed.** The
  tracker describes what an operator can hit, and nobody can hit a branch. So the first question
  about any candidate found while building is not "how severe" but **"is this on `dev`?"** —
  `git show origin/dev:<path>` and read the line. If the branch introduced it, it belongs in the
  diff that introduced it, with the test that pins it; filing it instead ships a known-broken
  change and asks someone else to notice.
  **When a tracking issue is genuinely wanted anyway** — the fix is deferred, or it spans work
  someone else holds — the title says so and the body opens with it: `on <branch>, not on dev`,
  plus the base commit and the `git show` that proves the contrast. An issue that does not say
  which tree it lives on will be verified against `dev`, because that is the only tree a reader
  has, and it will be closed.
- **Commit as you go, and keep the pull request focused — don't wait to be asked.**
  **The pull request is the unit that tells one story**: a fix ships with the test that pins
  it and the doc line it corrects, and unrelated work earns its own branch. CONTRIBUTING.md's
  PR-title section covers the squash mechanics. Put the `Co-Authored-By` trailer at the end of
  the **PR description**, since that is the text that becomes the commit message.

## Rules that apply everywhere

**7 / 24. A comment may not claim a safeguard that is not implemented, and one that names a
safeguard cites the function implementing it** — verified to exist and to be called before
merging. If you cannot cite it, correct the comment in the same commit.

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
read as the pause it looks like (#177). Arrows
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
a broken tree. Run each gate on
its own and read the status (`cmd > /tmp/out 2>&1; echo "exit: $?"`), and only pipe a command
whose success you are not currently deciding on. `| head` is worse still: it SIGPIPEs the
writer, so the command dies partway and reports that as its own result. This binds every
verification in this file.

**144. Generating one copy of an operator-facing claim raises the risk on every copy you did
not generate.** Rule 72 sweeps siblings of a *function* and rule 103 guards a *list* mirroring a
declaration; this is the same obligation for a *sentence*. One fact about what the app does is
normally stated in several places — a help paragraph, an API description, the error body that
fires when it is enforced — each written by someone reading a different one. Deriving one from
the code does not make the rest safe; it makes them **more** dangerous, because the derived copy
is demonstrably correct and vouches for a consistency that does not exist. So grep the sibling
copies of any sentence you are about to generate, and either generate them from the same
declaration or **point the generated one's test at them by name** — a failure message naming
the other file costs one line, where a comment asking future authors to remember does nothing.
A rounded claim is written to reassure, so it fails toward telling the operator the app is
safer than it is.

## Branch & merge workflow

- **`dev` is the default branch, and all work lands there** (CONTRIBUTING.md's Branches
  section). Every unit of work gets its own branch that merges back into `dev`.
- **Cut that branch from the latest upstream `dev`.** CONTRIBUTING.md's Branches section has
  the `git fetch origin` / `git checkout -b <branch> origin/dev` recipe. **A worktree sharpens
  this**: the session opens on whatever branch it was cut for, so branching from HEAD can carry
  someone else's commits in. Check `git log --oneline origin/dev..HEAD` first — empty means
  current.
- **Re-check before you open the PR.** CONTRIBUTING.md covers the rebase; then re-run the
  gates, since a branch current when cut can still merge into a tree its tests never ran
  against.
- **A pull request carries its `Kind/` and `Priority/` labels, same vocabulary as an issue**
  (`gh pr create --label "Kind/Bug,Priority/Critical"`). `Reviewed/` is issue triage and stays
  off a PR. CONTRIBUTING.md's Review and merge section has the rest.
- **Landing a branch into `dev` is CI-gated, and the style is `squash`.** It is the only style
  the repository allows; merge commits and rebase merges are both turned off, so there is no
  choice to get wrong. Check the run with `gh pr checks <n>` before merging (a fresh PR sits
  pending for minutes; `--watch` blocks until it settles), then `gh pr merge --squash <n>`.
  **The squash commit takes its subject from the PR title and its body from the PR
  description** (`squash_merge_commit_title=PR_TITLE`, `squash_merge_commit_message=PR_BODY`).
  `.github/workflows/pr-validation.yml` checks the title parses as a Conventional
  Commit, because that subject is permanent and feeds the release notes. The head branch is
  deleted automatically on merge. A **draft will not merge**, but `gh pr ready <n>` clears it,
  so a `WIP:` title is a label for humans and no longer a thing to strip. One round-trip cost
  survives: **a squash-merge lands a sha CI never tested**, since the change is replayed onto
  whatever `dev` is now, so landing several PRs back to back ends with the gates re-run on the
  merged `dev` rather than three green per-branch runs.
- **`main` is release-only.** Never push to `main` directly. Promote `dev` with a
  **squash-merged** pull request from a promotion branch, so `main` reads as a sequence of
  releases while the granular history lives on `dev`. The promotion branch carries `main`'s
  tip without touching the tree, which moves the PR's merge base to `main` and shrinks the
  diff to what actually landed:

  ```
  git fetch origin
  git checkout -b promote-<version> origin/dev
  git merge -s ours origin/main -m "Merge main back so the promotion diffs against the last release"
  git push -u origin promote-<version>
  gh pr create --base main --head promote-<version> --label "Kind/Release,Priority/High"
  ```

  **`Kind/Release` is what keeps this pull request out of the next release's notes.**
  `.github/release.yml` excludes that label (#934).

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
warnings and unhandled rejections are invisible exactly where you would act on them. Run
`npx vitest run <file> --disableConsoleIntercept` to see them locally, or read the CI log. Rule 135 is the standing
answer: a test with something to tell you must fail, not warn.

**Asking whether CI is green** is cheaper than reading a log: `gh pr checks <n>` lists one row per
job, and `CI gate` is the required check. A `paths` filter that skips a workflow publishes no check
run. An `if:` that skips a job still publishes one, marked `skipped`, which `CI gate` counts as a
pass. **Which jobs run depends on what the commit touched.** `ci.yml`'s `changes` job sorts paths
into three lanes, first match winning: `manual/*`/`website/*` is site (#589), `docs/*`/`.claude/*`/
`*.md` is prose, everything else is code. Prose runs `hygiene` alone, code runs `check`, `frontend`
and `docker`, and site runs `site`, `hygiene` and `frontend` too, since the guards that read
`manual/` and `website/` live in those last two (#783). **The manual publishes from Cloudflare
Pages**, which reads `dev` directly, so `site` gates nothing. `codeql.yml` and `weblate-notes.yml`
carry their own path lists, and `tests/test_repo_hygiene.py` pins all three by name.

**Reading a CI log.** `gh run view --log-failed` is almost always the whole answer: it prints
only the failing steps. `gh run list --branch <branch>` finds the run, `gh run view <id> --log`
dumps it in full, and `gh run watch <id>` blocks until it finishes. **Confirm the run is for the
sha you think it is** (`gh run view <id> --json headSha`, against `git rev-parse HEAD`) — a
squash-merge or a fresh push means the newest run for a branch is often not the commit you are
holding, which is the one way to read a green log for someone else's code and believe it.

**Both halves of the tree are machine-formatted, so never hand-argue style.** CONTRIBUTING.md
carries the division of territory, prettier's root-directory hazard, and what to do if a
reformat sweeps files you did not touch.

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
  locked. Small and edited in place, budgeted (see Golden rules).
- `docs/DECISIONS.md` — why each locked decision is what it is, one section per daggered row of
  `STATUS.md`'s table. Read the row, then this if you are about to change the behavior it
  describes: several of these decisions were reversed once already, and the reversal is the part
  a future reader needs most.
- `docs/LEARNINGS.md`, `docs/SIGNALS.md` — findings from real data. `SIGNALS.md` is cited from
  six places in `src/`: `engine/signals.py`, `engine/policy.py` (three times), `engine/gates.py`,
  and `api/review.py`. Read it before touching any of them: it is also the only place the rewatch
  curve is written down, now that the engines that measured it are gone.
- No plan is live. `I18N_PLAN.md` and `RETURN_PLAN.md` were the last two and are frozen into
  `docs/history/` (#862 and #553 closed them). `docs/README.md`'s map is the list to correct.
- `docs/history/` — frozen: the retired plan narratives and the review passes, including the
  finding IDs behind the numbered rules. Never edit an archived file to bring it up to date.
