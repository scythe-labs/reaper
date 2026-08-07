# What is written down, and where it goes

Reaper keeps four kinds of writing **for whoever works on it**, split by **how long a statement
stays true**. Putting a sentence in the wrong one is why docs go stale: a fact with a lifespan
of days, filed next to one with a lifespan of years, makes both untrustworthy.

Writing for whoever *runs* Reaper is a separate thing and lives outside `docs/`. It is split by
audience rather than by lifespan, so it gets its own section below.

| Kind | Lives in | Lifespan | How it is edited |
|---|---|---|---|
| **Rules** — how to work on Reaper | `CLAUDE.md`, `.claude/rules/` | until a review changes them | edited in place; numbered, and the numbers are permanent |
| **State** — what is true right now | `docs/STATUS.md` | days | **edited in place, never appended to** |
| **Knowledge** — what we measured and learned | `docs/LEARNINGS.md`, `docs/SIGNALS.md`, `docs/DECISIONS.md` | years | appended into the right topic section |
| **History** — what happened, and why | `docs/history/` | forever | **frozen; never edited** |

## The rule that keeps this working

**A change that alters what the app *does* updates `docs/STATUS.md` in the same commit.** Not
"soon," not "at the end of the session" — the same commit, because that is the only moment the
change is fully in mind.

This replaces "keep the plan current," which measurably did not hold: across the last 150 code
commits it ran at 24.7%, and it collapsed to 12% on the busiest days, exactly when the most was
changing. The cause was structural, not discipline. The old plan was append-only and had grown
to 3,508 lines, so adding a note meant first reading enough of it to find where the note went,
and that cost grew every day. `STATUS.md` is small and edited in place so that the update is
cheaper than the excuse. `tests/test_repo_hygiene.py` keeps it that way.

### A line budget is not a size budget

The first version of that budget capped lines only, and it failed in a way worth keeping written
down. `STATUS.md` reached exactly 200 of its 200 lines and stayed there, so every new fact had to
go onto a line that already existed. **A markdown table row cannot be wrapped**, so the pressure
landed in the cells: one "Decisions locked" cell reached 21,210 characters, three cells held two
thirds of the file, and changing a phrase meant editing a paragraph-length line. The gate was
green throughout — it was measuring the one dimension the file was no longer growing in.

So the budget is now **120 lines and 100 columns**, both enforced, and the width cap does most of
the work: at 100 columns a table cell holds a phrase and cannot hold narration. What that
displaces has somewhere to go — reasoning to `DECISIONS.md`, measurements to `LEARNINGS.md`, the
story to `history/`. **And closed work leaves the file**: narrating a fix you just landed is the
one habit that reliably refills it, and it feels like diligence while it happens.

## Which file takes what

- **A milestone changed state, a decision got locked, a limitation was lifted** → `STATUS.md`.
  Change the line that is now wrong. Do not add a new line beside it, and keep the row a phrase:
  a sentence in a table cell is the one shape this file cannot hold.
- **Why a locked decision is what it is** → `DECISIONS.md`, one `##` section per daggered row of
  `STATUS.md`'s table, matched by name in both directions and checked. A decision reversed is
  edited there in place with the reversal stated, because the reversal is what a future reader
  needs; a decision whose choice fits a cell on its own needs no section at all.
- **A fix you just landed** → nowhere, unless something it changed is still true and still worth
  a reader's time. The tracker and the code are its record. Correct the `STATUS.md` line the fix
  made wrong and stop there.
- **You measured something against a real library** → `LEARNINGS.md`, in the topic section it
  belongs to, using the shape the file already uses: a claim written as a sentence with
  `(measured <date>)`, evidence as ratios and orders of magnitude, and a `⇒` consequence line.
  **Negative results count** — "we tried X and it was worse" stops the next person re-trying X.
- **You learned what predicts that nobody will watch a title** → `SIGNALS.md`. It is cited from
  five places in `src/` — `engine/signals.py`, `engine/policy.py` (twice), `engine/gates.py` and
  `api/routes.py` — so read it before touching those, and before `engine/backtest.py`, which holds
  the rewatch curve `SIGNALS.md` tabulates (`FALLBACK_REWATCH_PRIOR`). `engine/calibration.py`
  cites it nowhere and holds only the machinery to fit a per-operator replacement, which has no
  caller in `src/`.
- **A review pass, a migration, a finished remediation** → `docs/history/`, with a banner
  saying it is frozen and what supersedes it. Never edit an archived file to bring it up to
  date; that is what makes an archive lie.
- **A rule for how to work** → `.claude/rules/`, not here. See `CLAUDE.md`.

## The operator's manual, which lives outside `docs/`

Everything above is written for whoever works on Reaper. The **manual** is written for whoever
runs it, and it lives in [`manual/`](../manual), served as a site by [`website/`](../website).
Filing it here would put a page an operator reads next to a review pass they never will.

Half of it is **generated**. The pages under Policy and Safety come from the app's own help
content (`frontend/src/docs/content/*.ts`) through `frontend/src/docs/toMdx.ts`, so the app's
help panel and the site say the same thing by construction. Edit the TypeScript, then run
`npm --prefix frontend run gen-manual`; `manual.gen.test.ts` fails when a page is stale. The
rest, install and configuration and the like, is hand-written `.mdx` beside them and appears
only on the site: you cannot read in-app help for a thing you have not installed yet.

**`README.md` is a third surface, and the split is by kind, not by length.** Features and the
safety posture belong on the front page, because someone deciding whether to trust Reaper with
their library should not have to hunt for them. **Instructions do not**: install, configuration,
tags, ports and volumes live in the manual alone, so there is one copy to keep right. A
`docker compose` line in the README was the second copy that prompted this rule.

## Figures never identify a server

Reaper ships to operators whose libraries we will never see. Findings are recorded as ratios
and shapes, never as one server's numbers, and never with a real title, host, path, or
username. This binds `docs/` exactly as it binds code and commit messages.

## Auto memory is not a substitute for any of this

Claude Code keeps a per-repository auto memory at `~/.claude/projects/<project>/memory/`. It is
useful, and it is **machine-local**: never committed, invisible to teammates and to CI, not
loaded into subagents, and gone on a new machine.

So: a finding anyone else would need — a measurement, an API footgun, a decision — goes in
`docs/`, even if it is already in auto memory. Auto memory is for *your* workflow (which
command to run, a harness quirk, a personal preference). If you find yourself relying on an
auto-memory note to explain the product, move it here.

## The map

| File | Kind | Status |
|---|---|---|
| `STATUS.md` | state | **live** — edit in place; 120 lines and 100 columns |
| `DECISIONS.md` | knowledge | **live** — one section per daggered `STATUS.md` row |
| `LEARNINGS.md` | knowledge | **live** — append by topic |
| `SIGNALS.md` | knowledge | stable; cited from `src/` |
| `CSS_SPLIT_PLAN.md` | state (one feature) | **live** — 3 of 7 stages landed; the last 4 are optional |
| `I18N_PLAN.md` | state (one feature) | **live** — a proposal; nothing landed, no stage committed to |
| `SIMPLIFICATION_PLAN.md` | state (one pass) | **live** — a whole-tree audit, reviewed; nothing applied, phase 0 of 8 |
| `brand/README.md` | reference | stable; nothing here ships |
| `../manual/` | the operator's manual | **live** — half generated from the app's help content |
| `../website/` | the manual's site | **live** — Docusaurus; owns no words of its own |
| `history/PLAN-narrative.md` | history | frozen — the retired living plan |
| `history/CODE_REVIEW.md` | history | frozen — 100/100 findings remediated |
| `history/CODE_REVIEW_PHASES.md` | history | frozen — 10/10 phases done |
| `history/UI_REVIEW.md` | history | frozen — 92/94 findings fixed |
| `history/SCREEN_READER_SWEEP.md` | history | frozen — the sweep's narrative; open half in `STATUS.md` |
| `history/SIZE_TRUTH_PLAN.md` | history | frozen — 5 stages shipped, 4 retired on a false premise |
