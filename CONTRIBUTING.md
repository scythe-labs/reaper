# Contributing to Reaper

Thanks for being here. Reaper is a hobby project that other people's media libraries depend
on, and help of every size is welcome: a bug report, a paragraph of documentation that
finally made sense, or a patch.

Before anything else, the one idea that shapes every decision in this codebase:

> **Reaper deletes irreplaceable data from a server other people depend on. Every ambiguity
> resolves toward keeping the file.**

A change that makes Reaper faster, prettier, or cleverer is welcome. A change that makes it
delete something it would previously have kept needs to earn that, out loud, in the pull
request. Everything below is downstream of this.

If you want to know who writes this and how, the README has an honest answer under
[How Reaper is built](README.md#how-reaper-is-built).

---

## Ways to help that are not code

- **Report a bug.** The [issue templates](https://github.com/scythe-labs/reaper/issues/new/choose)
  ask for what is actually needed to chase one down.
- **Report a wrong call.** If Reaper flagged media that should have been kept, that is the
  most valuable report this project receives, and it has a template of its own.
- **Ask a question.** [Discussions](https://github.com/scythe-labs/reaper/discussions) is the
  place for setup help, ideas that are still forming, and anything that is not yet an issue.
- **Fix the docs.** Instructions that did not work are a bug.
- **Try a pull request.** Every push builds an image, so a change can be run before it lands:
  `scripts/try-image.sh up --pr <number> --port 8421` on any machine with docker. It runs
  beside whatever else is on that host, and `--data copy:<volume-or-path>` gives it a copy of
  a real setup so the original is never written to. `scripts/try-image.sh --help` has the
  rest, including teardown.

Security problems go through [SECURITY.md](SECURITY.md), which opens a private report.

Everyone taking part agrees to the [Code of Conduct](CODE_OF_CONDUCT.md).

---

## AI assistance

Reaper is built with AI assistance, so using an assistant to write code here is ordinary and
welcome.

**Understand what you are submitting.** You should be able to explain why it works, and
answer a question about it next week. Everything below follows from that.

**Say so in the pull request.** One line in the template tells a reviewer where to read
closely. "None" is a perfectly good answer.

**Keep it to one thing.** A broad prompt produces a broad diff. A patch that fixes one bug
while quietly reformatting four other files is slow to review and easy to get wrong. Extra
work is welcome as its own pull request.

**Write the description in your own words.** A summary generated from the diff describes the
diff, and the reviewer already has that. Say what you were trying to do, and what you tried
first.

Two failure modes read as correct and survive a green test run:

- **A comment that claims a safeguard the code does not implement.** Assistants write
  confident prose about protections that were discussed and never built. When a comment names
  a guard, point at the function that is the guard.
- **A test that runs code without asserting anything about it.** It passes forever and proves
  nothing.

---

## Getting set up

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13 or newer.

```bash
git clone https://github.com/scythe-labs/reaper.git
cd reaper

uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

cp .env.example .env.local      # no key needed; one is generated on first boot

python -m reaper.preflight    # applies a staged restore, checks the data dir is writable
alembic upgrade head
uvicorn reaper.main:create_app --factory --no-proxy-headers --reload --port 8420
```

Preflight runs first, and before migrations, exactly as `docker-entrypoint.sh` orders them.
Skip it and a restore staged in the UI is never applied, so the banner asks for a restart
that cannot finish however many times it is given one (#381).

The web interface is a separate dev server that proxies to it:

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, proxies /api to the app on :8420
```

In production there is no second server. `npm run build` emits `frontend/dist`, and FastAPI
serves it as low-priority routes: the `/api` routes match first and everything else falls
back to `index.html`. Both dev and production therefore talk to a same-origin `/api`, which
is why there is no CORS configuration anywhere to accidentally loosen.

**Never commit credentials.** `.env.local` is gitignored. API keys entered in the web
interface are encrypted at rest and redacted from logs.

---

## Verification gates

These mirror CI. Run the relevant ones while you work, and the full set before you push.

On a pull request, CI decides which of them apply from the files you changed: a change to
documentation runs the repository hygiene test, and a change to code runs the Python suite,
the frontend suite, and an image build. The check named **CI gate** collects whichever ran,
and it is the one that has to be green before a pull request can merge. Seeing jobs marked
*skipped* beside it is the system working.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/reaper tests/          # both trees; pass src/reaper, or tests/ checks nothing
uv run pytest -n auto                  # what CI runs; ~80s on 8 cores, minutes on one
uv run alembic upgrade head            # then `alembic check` for model/migration drift

npm --prefix frontend run lint         # eslint
npm --prefix frontend run format:check # prettier; `run format` writes
npm --prefix frontend run test         # vitest
npm --prefix frontend run build        # tsc --noEmit, then vite build
```

Run the writing form of both formatters before you stage: `uv run ruff format .` and
`npm --prefix frontend run format`. Formatting is the most common reason CI goes red.

**Pass `-n auto`.** The suite is a few thousand tests and one worker takes minutes where the
whole machine takes about one, which is the difference between running it before every commit
and running it once at the end. Drop the flag only to debug a single test, where `-s` and
`--pdb` need one worker. A test that fails only under `-n auto` is a test leaking
process-global state into the next worker, not a reason to stop using it.

**Judge a gate by its exit code.** A pipeline reports its last command's status, so
`npm --prefix frontend run build | tail -4` prints `tail`'s success while a failing compile
scrolls past, and anything chained after it with `&&` then runs on a broken tree. Run each
gate on its own and check `$?`. Piping to `head` carries the same hazard and adds one: it
kills the writer partway and reports that as the result.

When a change is visible in the running app, click through it. Green tests and a working
feature are two separate claims, and each is worth making on its own evidence.

---

## How work lands

### Branches

`dev` is the default branch and everything lands there first. `main` is release-only.

Cut your branch from the current `dev`, asking the remote what current means. A local
`dev` is a cached answer, and a session refreshes it only when you say so:

```bash
git fetch origin
git checkout -b my-branch origin/dev
```

Give it a name that says what it is: `fix-season-spare`, `docs-unraid-setup`. Rebase onto
`origin/dev` before you push, since `dev` moves while you work.

### Your pull request title becomes the commit message

Pull requests are squash-merged, so the whole branch arrives on `dev` as a single commit
whose subject is your PR title and whose body is your PR description. Write the title as a
[Conventional Commit](https://www.conventionalcommits.org/en/v1.0.0/):

```
fix(retention): an open pull request keeps its image
feat(policy): season-level spare
docs: contributor guide
```

The types in use here:

- `feat` — behavior an operator can see that did not exist before
- `fix` — a defect corrected
- `docs` — documentation only
- `test` — tests only
- `refactor` — the structure moved, the behavior is identical
- `perf` — measurably faster, with the measurement in the description
- `ci` — workflows, automation, and the image build
- `chore` — dependencies and housekeeping

A scope in parentheses is optional and helps when the area is not obvious from the subject.
For a change that breaks compatibility, add `!` before the colon and explain the break in
the description.

A workflow checks the title on every push and edit, and it will tell you what to fix. It
checks the type and nothing else, so subject wording is yours.

Commits on your branch are yours to shape however you like, since they are collapsed on the
way in. Meaningful ones make review easier and nothing more is required of them.

### Review and merge

Open a draft early if you want feedback on a direction. Add the `Kind/` and `Priority/`
labels that match the issue it closes, so the pull request shows up in the same filters the
backlog uses.

CI runs on every push. Once it is green and the review is done, a maintainer squash-merges
it and deletes the branch.

---

## Writing

### Text an operator reads

Anything shown in the interface or returned as an error message is read at a glance while
somebody decides what to delete, and long copy does not get read at all.

- Lead with the outcome and what it means for their files.
- A phrase beats a sentence; a sentence beats two.
- Plain words only. No rating keys, no database ids, no internal vocabulary. If a normal
  person would not say it out loud, reword it.
- Use a comma where you were reaching for an em dash or a middot. A screen reader voices a
  middot as "middle dot" or drops it silently, and either way the sentence arrives broken.
- Arrows are fine: `Policy → Deletion`.
- Only describe features that exist. If copy names a mechanism, confirm the route or the
  screen is really there.

After you write an operator string, cut it once more.

### Prose in the repository

American English throughout, including identifiers and commit messages. The exceptions are
names spelled British by whoever owns them, like `asyncio.CancelledError` and
`aria-labelledby`.

Markdown and YAML have no formatter watching them, so their shape is yours to choose. Wrap
them wherever they read comfortably in a terminal, somewhere around 100 columns to match the
code. Your editor, a formatter, or your own eye all get there. The thing that matters is
that the next person can read a diff of it without scrolling sideways.

Python and everything under `frontend/` are machine-formatted, so write code at whatever
shape is clearest and let the formatter settle it. Prettier's territory stops at `frontend/`.
Running it from the repository root finds no config, falls back to 80 columns, and reflows
every hand-wrapped line of documentation in the project.

### One fact, one place

A statement about what Reaper does is usually written in several places: a help paragraph, an
API description, the error that fires when it is enforced. When you change the behavior,
search for the other copies of the sentence and fix them in the same pull request. A
half-updated claim is worse than a stale one, because the correct copy vouches for the rest.

---

## Things worth knowing before you change code

### The dependency budget

The web interface has eight dependencies: React, React-DOM, TanStack Query, and the build
toolchain. No component library and no CSS framework. This is a tool that can delete a media
library, and every transitive package ends up in the bundle that renders the delete button.
Adding one is a conversation worth having in the pull request.

Keeping the ones we have current is not your job: `.github/dependabot.yml` opens a grouped
pull request each Monday for the lockfiles, the Dockerfile's base images, and the pinned
action shas. Review it like any other, and read it as a proposal rather than a verdict. A
major version of a Python or npm package comes as its own pull request, so it can be argued
with separately from the week's patches.

A release has to be two weeks old before it is offered at all. A compromised package looks
exactly like a good one on the day it ships, and the wait is what gives somebody else time to
find that. Base images are the exception at three days, because there a new digest is usually
the security patch. Neither delay applies to a fix for a published advisory, which arrives at
once.

### Every timestamp is an integer

Timestamps are UTC instants stored as integer unix epochs (`src/reaper/db/types.py`) and
presented to Python as timezone-aware datetimes.

This removes a bug class rather than guarding against one. SQLite stores no timezone, so
`DateTime(timezone=True)` is silently a no-op there: aware datetimes go in and naive ones
come back, and comparing the two either raises or comes out quietly wrong by your UTC
offset. Every deletion decision rests on *when was this last watched*, so quietly wrong is
the failure that matters. An integer cannot carry the ambiguity.

`reaper.clock` is the only sanctioned boundary. Note that `from_epoch()` maps `0` and `""`
to `None`, because Tautulli and Plex use them for *never played*. Reading those as 1970
would make an item look maximally stale and condemn exactly the media that must be left
alone.

To read the database by hand:

```sql
SELECT datetime(first_flagged_at, 'unixepoch') FROM first_flagged;
```

### Two things that cannot be changed later

The metadata naming convention (`src/reaper/db/base.py`) and Alembic's `render_as_batch=True`
(`alembic/env.py`). SQLite cannot drop an unnamed constraint, so without both, future
migrations fail and the only repair is rewriting the entire migration history.

`tests/test_migrations.py` guards both halves. It imports the real naming convention and
proves a named constraint can be dropped under batch mode, and it runs the real
`alembic/env.py` to capture what that file passes to `context.configure()`. There is one call
site: the offline (`--sql`) branch had no invoker and could not have worked, since 9
revisions call `op.get_bind()`, so it was removed rather than kept as a second path nothing
exercises. Flipping `render_as_batch` to `False` fails that test today, which is a great deal
better than discovering it years from now in the first migration that needs it.

### Reading git history

`.git-blame-ignore-revs` lists the whitespace-only commits. Running
`git config blame.ignoreRevsFile .git-blame-ignore-revs` once per clone keeps `git blame`
pointing at the commit that changed a line's meaning. If a reformat ever sweeps files you
did not touch, land it as its own commit and add it to that list.

---

## Database migrations

Testers run Reaper against real libraries, so **migrations only ever add.** Nobody should
have to rebuild their database to take an update.

- The Alembic baseline is frozen. Never edit it.
- Every schema change is a new revision chained onto the current head.
- New columns are nullable or carry a server default, and the next scan fills them in.
- A column that has not been filled in yet reads as "unknown" and never as a wrong definite
  value.

`cache.db` is disposable and carries no migrations.

---

## Where things are documented

`docs/README.md` says what belongs in which file. In short:

- `docs/STATUS.md` — what is true right now. Small, edited in place, and kept to a budget.
- `docs/DECISIONS.md` — why each locked decision is what it is.
- `docs/LEARNINGS.md` and `docs/SIGNALS.md` — findings from real data, including the ones
  that did not work out.
- `docs/history/` — frozen. Archived plans and review passes are never updated.

A change that alters what the app does updates `docs/STATUS.md` in the same commit, by
editing the line that is now wrong.

Reaper ships to operators whose servers nobody here will ever see, so nothing identifying
goes into the repository: no real titles, hostnames, paths, usernames, or library
statistics, in code, docs, tests, or commit messages alike. Findings from real data are
recorded as ratios and shapes.

---

## Working on this with an AI agent

If you are pointing an agent at this repository, [AGENTS.md](AGENTS.md) is its entry point.
It carries the architecture, the safety model, and the numbered engineering rules the
codebase cites by number. This guide holds the conventions, and the agent instructions defer
to it, so there is one copy of each rule to keep current.
