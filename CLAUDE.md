# Reaper: guide for agent sessions

Reaper is a self-hosted web tool. It finds media nobody watches and explains why it thinks each
item is expendable. The explanation lists every signal, and every protection that was checked
and did not fire. You review and approve, then Reaper removes the item through Sonarr or Radarr
and refreshes Plex. The backend is Python 3.13 and FastAPI, the frontend is React 19 and Vite,
and both ship in one container.

> **Prime directive.** Reaper deletes irreplaceable data from a server other people depend on.
> Every ambiguity resolves toward keeping the file. When in doubt, fail closed.

The conventions live in [`CONTRIBUTING.md`](CONTRIBUTING.md). They bind an agent session exactly
as they bind a human contributor. CONTRIBUTING states setup, the verification gates, commit and
pull request conventions, how to write operator-facing text, and the AI policy, once for both
audiences. This file holds the numbered rules, the architecture, the safety model, and the
documentation discipline, which are specific to working inside the repository. Where the two
files cover the same subject, CONTRIBUTING is the copy to correct. [`AGENTS.md`](AGENTS.md) is
the entry point other agent tools read, and it routes back here.

## Where the engineering rules live

Reaper has 148 numbered rules. A violation of any of them blocks the change. The numbers are
permanent, because existing tests and the archived review passes in `docs/history/` cite them.
Never renumber a rule, and never reuse a number for a different rule. A code comment or test
docstring states its constraint in plain language that a first-time reader gets in one pass. It
does not cite a rule number. The number lives in the rule file. A comment that does cite a rule
may cite only one that exists. `tests/test_repo_hygiene.py` fails on a citation of a rule that
does not exist. New rules append to the scoped file that governs them, from 149.

The rules live in `.claude/rules/`. Each file's `paths` frontmatter scopes it, so a rule set
loads when you read a file it governs. A file must be read before it can be edited. A rule set
that loads for work it does not govern costs context in every unrelated session. So a cluster of
rules large enough to notice gets its own scoped file.

| File | Governs | Rules |
| --- | --- | --- |
| `.claude/rules/backend.md` | `src/reaper/**/*.py`, `alembic/**/*.py` — safety path, engine, evidence, clients, persistence | 1–6, 8–10, 13, 22, 23, 26–35, 38, 52, 55–59, 63, 65, 70, 71, 73, 77, 78, 81, 82, 87–97, 102–117, 124, 127–129, 131, 140, 142, 143, 148 |
| `.claude/rules/auth.md` | `src/reaper/auth/**`, `secrets.py`, `crypto.py`, `logbuffer.py`, `logging.py`, `services/{backup,restore,admin_password,login}.py`, `api/{settings,plex,backup,deps,auth}.py` — credentials, sessions, at-rest key material | 11, 12, 14, 74–76, 83, 84, 98–101, 125, 126, 130 |
| `.claude/rules/frontend.md` | `frontend/src/**/*.{ts,tsx,css}` and `frontend/index.html` (rule 69 governs it) — UI grammar, gating surfaces | 17–20, 36, 39–45, 47, 53, 54, 60–62, 66, 67, 69, 79, 80, 85, 86, 138, 139, 146 |
| `.claude/rules/review-queue.md` | `ReviewQueue`, `OverrideControls`, `ShowPanel`, `SeasonList`, `reviewFate`, `styles/24-overrides.css`, `styles/25-select-mode.css` — fate, overrides, the two-level spare, the queue's action grammar | 46, 48–51, 120–123 |
| `.claude/rules/tests.md` | `tests/**/*.py`, `frontend/src/**/*.test.ts{,x}`, `frontend/src/test/**` — test discipline | 37, 118, 119, 132, 133, 135–137, 141, 145, 147 |

Eleven rules bind every file and stay here, under *Rules that apply everywhere*. Where two rules
overlap, the more specific one governs. Read the governing file before working in a tree you
haven't touched this session.

This table, each rule file's `Holds` line, and the rule count state one fact in several places.
`test_every_index_of_the_rules_matches_the_rules` checks all of them and names each one that
disagrees.

Most candidate rules do not earn a number. Before you append 149, try these in order. First,
extend an existing rule if one covers the class. Second, write the gate instead if the violation
is greppable. A gate in `test_repo_hygiene.py` binds an author who never read the rules, and
prose cannot. Only then append a new rule, as an instruction with no narrative. When a gate
enforces a constraint, never also write a rule that narrates the gate.

## Golden rules

- **Nothing identifying in the tree.** This binds code, docs, tests, and commit messages alike.
  Write no real titles, hosts, paths, usernames, or library stats. Record findings from real data
  as ratios and shapes. Screenshots are the one exception, and only screenshots of the
  maintainer's own instance. Other people are never in the exception. Scales lists the names of
  everyone who requested something, so crop a screenshot of it above that list, and never
  retouch it. `scripts/gen_screenshot_mockup.py` builds an invented version under
  `docs/media/review-queue-mockup.png`. It is kept so that a one-line README edit can reverse the
  screenshot exception.
- **American English everywhere.** This includes identifiers and commit messages. The exception
  is a name spelled British at the source, such as `asyncio.CancelledError` and
  `aria-labelledby`. CONTRIBUTING.md's Writing section is the full copy.
- **Treat Reaper as production code.** It will be released. Write for an unknown operator, never
  for one specific server.
- **Operator copy is read at a glance, never twice.** Use a phrase over a sentence, and one
  sentence over two. Lead with the outcome. Put the explanation in help text bound to the
  control. Help text has the same length limit. Delete cut copy, and never move it into a code
  comment. After you write an operator string, cut it once more. Rule 21 governs its vocabulary,
  and this rule governs its length.
- **Mock up UI/UX before touching code.** Load the `reaper-artifact` skill. Present a rendered,
  self-contained HTML artifact in Reaper's look and feel. Iterate on the artifact until it is
  approved, and only then edit frontend code. The skill gives you the app's live tokens and
  component styles, so the mockup matches the app without re-researching them. Iterating on a
  picture costs far less than iterating on a diff.
- **Ship additive, non-breaking migrations. Never make a tester rebuild their DB.**
  CONTRIBUTING.md's Database migrations section has the mechanics. They are the frozen baseline,
  one revision per change chained by `down_revision`, and nullable `ADD COLUMN`s that the next
  scan backfills. Schema must stay removable, and rule 148 is the only way to remove it. Removal
  is a two-release sequence, never an ad-hoc drop. The reasoning lives in `docs/DECISIONS.md`'s
  Migrations section.
- **A change that alters what the app does updates `docs/STATUS.md` in the same commit.** Edit
  the line that is now wrong, and never append a new line beside it. Measured findings, including
  negative results, go to `docs/LEARNINGS.md`. `STATUS.md` records the state, never the route to
  it. Its budget is 120 lines and 100 columns, and tests enforce both caps. `docs/README.md`'s
  "A line budget is not a size budget" section explains the second cap. A row holds a phrase,
  never a sentence. Reasoning behind a locked choice goes to `docs/DECISIONS.md`, one section per
  daggered row. `test_repo_hygiene.py` checks that the two files agree both ways. Closed work
  leaves `STATUS.md` entirely. The record of a shipped fix is the tracker and the code.
- **A bug you are not fixing becomes an issue before the session ends. Don't wait to be
  asked.** A defect left only in a transcript is lost when the session ends, and a mention in
  the summary is not a record. Every defect leaves fixed or filed, in every session, including
  sessions outside `/reaper-review`. Most defects are found while building something else.
  Filing needs no approval. Say what you filed in the summary, and do not ask first. File a
  candidate you could not demonstrate too, as a question. Give it `Status/Need More Info` and no
  `Reviewed/` label, because an issue must not assert a defect that nobody showed. Promoting it
  later takes both a label edit and the evidence that settled it, written into the issue. To kill
  it, close it as `Reviewed/Invalid`. The `reaper-review` skill's *Opening issues* section holds
  every mechanic, label and cap. It binds every session, not only a review pass. Read it before
  filing. The mechanics are stated only there, so this file cannot drift from them.
- **A defect your own unlanded branch created is fixed on that branch, never filed.** The tracker
  describes what an operator can hit, and no operator can hit a branch. So the first question
  about any candidate found while building is "is this on `dev`?", and severity comes after. Run
  `git show origin/dev:<path>` and read the line. If the branch introduced the defect, fix it in
  the diff that introduced it, with the test that pins it. Filing it instead ships a
  known-broken change and leaves someone else to notice. A tracking issue is sometimes wanted
  anyway, when the fix is deferred or it spans work someone else holds. Then the title says the
  defect is on the branch. The body opens with `on <branch>, not on dev`, plus the base commit and
  the `git show` that proves the contrast. A reader verifies an issue that does not name its tree
  against `dev`, because `dev` is the only tree a reader has. The reader then closes the issue.
- **Commit as you go, and keep the pull request focused. Don't wait to be asked.** One pull
  request tells one story. A fix ships with the test that pins it and the doc line it corrects.
  Unrelated work gets its own branch. CONTRIBUTING.md's How work lands section covers the squash
  mechanics. Put the `Co-Authored-By` trailer at the end of
  the **PR description**, since that is the text that becomes the commit message.

## Rules that apply everywhere

**7 / 24. A comment states what the code must do or must never do, and never argues for
it.** Cut history, hypotheticals about a change nobody made, and answers to "why not X". Put them
in the pull request or `docs/`. Many existing comments break this rule. Never match their voice
or length, whatever an instruction to match surrounding comments says. CONTRIBUTING.md's Code
comments section has an example. A comment may not claim a safeguard that is not implemented. A
comment that names a safeguard cites the function that implements it. Before merging, verify
that the function exists and is called. If you cannot cite it, correct the comment in the same
commit.

**15. The shipped artifact keeps building in CI.** Install from the committed lockfile with
digest-pinned base images. Never let unpinned `>=` floors resolve fresh at build time. CI runs
`docker build`, so don't build the image locally to satisfy a gate.

**16. Every operator-configurable credential lives in the DB-backed, encrypted, UI-editable
surface and is documented in `.env.example`.** Never strand a setting as env-only and
undocumented while the UI advertises its outcome.

**21. Every operator-facing string is plain language.** This binds frontend strings and backend
`detail`/message strings alike. Lead with the outcome and say what it means for their files.
Keep internal vocabulary out of it. That means no rating keys, no tmdb/imdb/tvdb ids, and no
"collision", "guard", "coverage bp" or abstain-as-jargon. If a normal person wouldn't say it,
reword it. This binds notices, tooltips, empty states, and error text alike. Use no em dashes in
operator-facing copy. Reword with a period, comma, or colon. Never separate two facts with a
middot or a dash either. A screen reader may voice that punctuation
("40 titles *middle dot* 1.2 TB freed") or drop it entirely. Use a comma as the separator, because a screen reader reads it
as the pause it looks like (#177). Arrows ("Policy → Deletion") are fine. A middot is still fine
where it is only decoration, such as a dot between chips or a placeholder for "no value". There
it carries `aria-hidden`, because nothing is lost when a screen reader skips it. The test is
whether a reader who never hears the character still gets the sentence. A string that is plain
but long still fails.

**25. Operator copy may only reference features that are wired.** Confirm the route or UI path
exists before writing text that names a mechanism (backtest, cap, interlock). A DB constraint or
schema for an unwired feature is a blocker, never a placeholder.

**64. Removing a surface removes its whole supply chain in the same change.** The supply chain
is the route, schemas, client method, props, query-key invalidations, and comments naming it.
Grep for the query key and the prop name before closing.

**68. Generated assets ship with their generator.** A comment saying an asset is generated names
a committed, runnable script, such as `frontend/scripts/gen-icons.mjs` or
`scripts/policy_lab_extract.py`. A drift test covers every generated artifact. An artifact that cannot have one says why beside
its generator.

**72. A fix lands on every sibling of the thing you fixed, in the same change.** Grep for the
siblings before closing. Then fix each one, or defer it in writing. A "when next touched"
deferral is due in the first commit that touches the sibling, whatever that commit is for. On its
own this rule covers a copied function, such as a paging loop, section resolution, or error
mapping. Four backend rules say what "sibling" means for other things. Rule 127 covers an
interlock whose docstring claims "every". Rule 140 covers a value you re-qualified. Rule 142
covers a discriminator you typed. Rule 143 covers a set whose membership you changed. The sweep
is the same each time, so when one of these applies, check the others too.

**134. A gate is judged by its exit code, never by the output you kept.** A pipeline exits with
its last command's status. So `npm --prefix frontend run build | tail -4` reports `tail`'s
success while a failing `tsc` scrolls past. Anything chained after it with `&&` then runs on a
broken tree. Run each gate on its own and read the status
(`cmd > /tmp/out 2>&1; echo "exit: $?"`). Pipe only a command whose success you are not
currently deciding on. `| head` also SIGPIPEs the writer, so the command dies partway and
reports that as its own result. This rule binds every verification in this file.

**144. Generating one copy of an operator-facing claim raises the risk on every copy you did
not generate.** Rule 72 sweeps the siblings of a function, and rule 103 guards a list that
mirrors a declaration. This rule is the same obligation for a sentence. One fact about what the
app does is normally stated in several places, such as a help paragraph, an API description,
and the error body that fires when the app enforces it. Each copy is written by someone reading
a different one. Deriving one copy from the code makes the other copies more dangerous, because
the derived copy is demonstrably correct and vouches for a consistency that does not exist. So
grep the sibling copies of any sentence you are about to generate. Then either generate them
from the same declaration, or point the generated copy's test at them by name. Use a failure
message that names the other file, never a comment asking future authors to remember. A rounded
claim is written to reassure, so it fails toward telling the operator the app is safer than it
is.

## Branch & merge workflow

- **`dev` is the default branch, and all work lands there.** CONTRIBUTING.md's Branches section
  has the details. Every unit of work gets its own branch that merges back into `dev`.
- **Cut that branch from the latest upstream `dev`.** CONTRIBUTING.md's Branches section has the
  `git fetch origin` / `git checkout -b <branch> origin/dev` recipe. In a worktree, the session
  opens on whatever branch the worktree was cut for, so branching from HEAD can carry someone
  else's commits in. Check `git log --oneline origin/dev..HEAD` first. Empty output means the
  branch is current.
- **Re-check before you open the PR.** CONTRIBUTING.md covers the rebase. After the rebase,
  re-run the gates. A branch that was current when cut can still merge into a tree its tests
  never ran against.
- **A pull request carries its `Kind/` and `Priority/` labels, same vocabulary as an issue.** Set
  them with `gh pr create --label "Kind/Bug,Priority/Critical"`, or with
  `gh pr edit <n> --add-label` after the fact. `Reviewed/` is issue triage and stays off a PR.
  CONTRIBUTING.md's Review and merge section has the rest.
- **Landing a branch into `dev` is CI-gated, and the style is `squash`.** The repository allows
  only squash. Merge commits and rebase merges are both turned off. Check the run with
  `gh pr checks <n>` before merging, then run `gh pr merge --squash <n>`. A fresh PR sits pending
  for minutes, and `--watch` blocks until it settles. The squash commit takes its subject from
  the PR title and its body from the PR description (`squash_merge_commit_title=PR_TITLE`,
  `squash_merge_commit_message=PR_BODY`). `.github/workflows/pr-validation.yml` checks that the
  title parses as a Conventional Commit, because that subject is permanent and feeds the release
  notes. GitHub deletes the head branch on merge. A draft will not merge. `gh pr ready <n>`
  clears the draft state, so a `WIP:` title is only a label for humans and needs no stripping. A
  squash-merge lands a sha CI never tested, because GitHub replays the change onto whatever
  `dev` is at merge time. So after landing several PRs back to back, re-run the gates on the
  merged `dev`, because the per-branch runs tested other shas.
- **`main` is release-only.** Never push to `main` directly. Promote `dev` with a squash-merged
  pull request from a promotion branch. Then `main` reads as a sequence of releases, and the
  granular history stays on `dev`. The promotion branch carries `main`'s tip without touching the
  tree. That moves the PR's merge base to `main` and limits the diff to what landed since the
  last release. Open it with these commands.

  ```
  git fetch origin
  git checkout -b promote-<version> origin/dev
  git merge -s ours origin/main -m "Merge main back so the promotion diffs against the last release"
  git push -u origin promote-<version>
  gh pr create --base main --head promote-<version> --label "Kind/Release,Priority/High"
  ```

  The `Kind/Release` label keeps this pull request out of the next release's notes, because
  `.github/release.yml` excludes that label (#934).

  Then run `gh pr merge --squash <n>`. The ours-strategy merge keeps `dev`'s tree bit for bit,
  and release.yml verifies that before tagging. It never conflicts, whatever the histories look
  like. GitHub deletes the head branch at merge. The release tag keeps the branch's head commit
  alive. That commit's first parent is `dev`'s line, so the next release's generated notes cover
  only the PRs that landed since this one.

## Verification gates

The gate list and how to run it live in
[`CONTRIBUTING.md`](CONTRIBUTING.md#verification-gates). That section also holds the rule that
the writing form of both formatters runs before anything is staged. Run the relevant subset
while iterating, and the full set before a commit. When a change is observable in the app, drive
it end-to-end with the `verify` skill. Do not stop at green tests for such a change. The rest of
this section is what a session needs on top of that list.

Read the exit code, not the tail of the output (rule 134). Reading the tail silently defeats
every other gate.

A green `npm run test` can still hide warnings. Vitest's console interception drops test console
output on some Node versions. Node 26 prints nothing, and CI's pinned Node 24 prints it. So
`act()` warnings and unhandled rejections can be invisible in a local run, where you would act
on them. Run `npx vitest run <file> --disableConsoleIntercept` to see them locally, or
read the CI log. Under rule 135, a test with something to tell you must fail, never only warn.

To ask whether CI is green, run `gh pr checks <n>`, which costs less than reading a log. It
lists one row per job, and `CI gate` is the required check. A `paths` filter that skips a
workflow publishes no check run. An `if:` that skips a job still publishes one, marked
`skipped`, and `CI gate` counts it as a pass. Which jobs run depends on the files changed. For a
PR that is its diff. For a push it is everything since the branch's last green CI run.
`ci.yml`'s `changes` job sorts paths into three lanes, and the first match wins.
`manual/*`/`website/*` is site (#589), `docs/*`/`.claude/*`/`*.md` is prose, and everything
else is code. Prose runs `hygiene` alone. Code runs `check`, `frontend` and `docker`. Site runs
`site`, plus `hygiene` and `frontend`, because the guards that read `manual/` and `website/`
live in those two (#783). The manual publishes from Cloudflare Pages, which reads `dev`
directly, so `site` gates nothing. `codeql.yml` and `weblate-notes.yml` carry their own path
lists, and `tests/test_repo_hygiene.py` pins all three by name.

To read a CI log, start with `gh run view --log-failed`. It prints only the failing steps, and
that is almost always enough. `gh run list --branch <branch>` finds the run,
`gh run view <id> --log` dumps it in full, and `gh run watch <id>` blocks until it finishes.
Confirm the run is for the sha you think it is. Compare `gh run view <id> --json headSha`
against `git rev-parse HEAD`. After a squash-merge or a fresh push, the newest run for a branch
is often for a different commit than the one you hold. Then a green log can belong to someone
else's code.

Both halves of the tree are machine-formatted, so never hand-argue style. CONTRIBUTING.md says
which formatter owns which files, what goes wrong when prettier runs from the repository root,
and what to do if a reformat sweeps files you did not touch.

## Dev environment

- **API :8420, frontend :5173.** Vite proxies `/api`. In an interactive session, start them via
  `.claude/launch.json` (`preview_start`, names `reaper-api` / `reaper-frontend`). Never hand-run
  dev servers.
- **Headless or background job (no `preview_start`).** Run `scripts/dev-local.sh`. It runs
  preflight and then migrations, in the entrypoint's order, so it applies a staged restore
  exactly as the container does. Then it boots both auto-reloading servers (API `--reload`, Vite
  HMR) against the shared real `data/`, waits for health, and prints the URLs. `down` stops
  them, and `status` / `logs` inspect them. The UI at :5173 is the live dev server, not a build.
  `npm run build` is a CI gate only. To run a second instance beside a running one, set
  `REAPER_PORT` and `REAPER_WEB_PORT` together. `REAPER_PORT` reaches both uvicorn and Vite,
  whose `/api` proxy target reads it (`frontend/vite.config.ts`). Setting only the web port
  leaves the second UI talking to the first instance's API. The two ports also name the
  instance's logs (`.dev-logs/api-<port>.log`, in the main checkout, whichever tree booted it).
  So pass them to `down` / `logs` / `status` as well. Otherwise those commands answer for the
  default instance.
- API calls require the header `X-Reaper-CSRF: 1`. Auth is a cookie session.
- Secrets live in a gitignored `.env.local`. `data/` (`reaper.db`, `cache.db`) is gitignored and
  rebuildable. Never paste real keys into the transcript or a commit.

## Architecture

- `src/reaper/clients/` is the only place HTTP lives. The one sanctioned exception is
  `notify/discord.py`'s webhook POST (see rule 33). `GuardedTransport`, and its `GuardedSession`
  twin for plexapi, refuses any mutating request unless deletion is armed on the host and the
  executor also declared the intent to the journal first.
- `src/reaper/engine/` holds `gates` (hard, fail-closed protections), `signals` (soft weighted
  pressure), `verdict.decide_verdict` (the one condemn/abstain/protect decision), and the "why"
  record. `score()` is unsigned pressure over a fixed denominator, so missing or keep-arguing
  evidence can only lower the score. Keep it unsigned. A signed score off a neutral baseline
  inverts under failure, as the "Why unsigned" note atop `signals.py` explains.
- `src/reaper/services/` holds `snapshot` (gather → freeze → hash → score), `planner` (build the
  journaled plan), `executor` (the real send and its interlocks), plus grace, leaving_soon,
  scan_runner, and whitelist.
- `src/reaper/api/` holds the FastAPI routers. `frontend/src/` holds the React SPA.
- A scan is a snapshot. It freezes and hashes all evidence before scoring, so a transient timeout
  can never flip an item's fate during the scan.

## Safety model

Two independent layers sit under every mutation, and neither is trusted alone.

1. The executor's `dry_run`, the default, walks every interlock and records what it would send.
   It sends nothing.
2. The transport guard refuses any mutating call unless the host is armed and the intent was
   also journaled first. The guard is a property of the host that a browser cannot reach.

Deletion is armed only from the UI, behind a password. The one route that deletes is
`POST /api/runs/{id}/execute`. It requires both the armed host and the exact content-bound
confirmation phrase, which the server recomputes. The scheduler never deletes. Each of the
executor's interlocks resolves toward keeping the file. They include the manifest re-check, caps
that abort and never truncate, the canary, and the per-item streaming veto and
played-since-approval check.

## Where things are documented

- `docs/README.md` says what belongs in which file, and holds the rule that keeps them current.
- `docs/STATUS.md` is where to start. It records what is true right now, which is milestones,
  open work, and decisions locked. It is small, edited in place, and budgeted (see Golden rules).
- `docs/DECISIONS.md` says why each locked decision is what it is, one section per daggered row
  of `STATUS.md`'s table. Before you change the behavior a row describes, read the row, then its
  section here. Several of these decisions were reversed once, and the section records the
  reversal, which is what a future reader needs most.
- `docs/LEARNINGS.md` and `docs/SIGNALS.md` hold findings from real data. Six places in `src/`
  cite `SIGNALS.md`. They are `engine/signals.py`, `engine/policy.py` (three times),
  `engine/gates.py`, and `api/review.py`. Read it before touching any of them. It is also the
  only place the rewatch curve is written down.
- No plan is live. `I18N_PLAN.md` and `RETURN_PLAN.md` were the last two, and both are frozen in
  `docs/history/` (#862 and #553 closed them). `docs/README.md`'s map is the list to correct.
- `docs/history/` is frozen. It holds the retired plan narratives and the review passes,
  including the finding IDs behind the numbered rules. Never edit an archived file to bring it up
  to date.
