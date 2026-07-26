# What is written down, and where it goes

Reaper keeps four kinds of writing, split by **how long a statement stays true**. Putting a
sentence in the wrong one is why docs go stale: a fact with a lifespan of days, filed next to
one with a lifespan of years, makes both untrustworthy.

| Kind | Lives in | Lifespan | How it is edited |
|---|---|---|---|
| **Rules** — how to work on Reaper | `CLAUDE.md`, `.claude/rules/` | until a review changes them | edited in place; numbered, and the numbers are permanent |
| **State** — what is true right now | `docs/STATUS.md` | days | **edited in place, never appended to** |
| **Knowledge** — what we measured and learned | `docs/LEARNINGS.md`, `docs/SIGNALS.md` | years | appended into the right topic section |
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

## Which file takes what

- **A milestone changed state, a decision got locked, a limitation was lifted** → `STATUS.md`.
  Change the line that is now wrong. Do not add a new line beside it.
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
| `STATUS.md` | state | **live** — edit in place |
| `LEARNINGS.md` | knowledge | **live** — append by topic |
| `SIGNALS.md` | knowledge | stable; cited from `src/` |
| `SIZE_TRUTH_PLAN.md` | state (one feature) | **live** — 4 of 9 stages remain; archive it when they land |
| `brand/README.md` | reference | stable; nothing here ships |
| `history/PLAN-narrative.md` | history | frozen — the retired living plan |
| `history/CODE_REVIEW.md` | history | frozen — 100/100 findings remediated |
| `history/CODE_REVIEW_PHASES.md` | history | frozen — 10/10 phases done |
| `history/UI_REVIEW.md` | history | frozen — 92/94 findings fixed |
