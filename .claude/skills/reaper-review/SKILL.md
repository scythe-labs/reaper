---
name: reaper-review
description: Review Reaper code for defects, ranked by how close they sit to the deletion path. Use for any code review of this repo — a diff, a lane (safety/seam/backend/frontend), or a whole-tree pass before promoting dev to main.
---

# Reviewing Reaper

**The prime directive is the ranking function.** Reaper deletes irreplaceable data from
someone else's server. A defect matters in proportion to how close it sits to a file being
removed, not to how it would score on a generic severity rubric. A crash in a settings form
is an annoyance; a protection that evaluates to "nothing configured" is the worst outcome
this codebase has, and it is usually silent.

## Argument

The argument names a lane from the table below, optionally followed by a target — a commit, a
branch, or paths. Either part may be absent: with no lane, review the `diff` lane; with no
target, review the working diff. Positional `$1` substitution is deliberately not used here,
because it does not reliably resolve to the first word of a multi-word argument.

Adding `fix` anywhere in the argument (`/reaper-review fix`, `/reaper-review safety fix`) means
apply the findings after reporting them — see *Applying the fixes* below.

| Lane | Scope |
| --- | --- |
| `diff` | the working diff, or `git diff dev...HEAD` on a branch. **The default and the common case.** |
| `safety` | `engine/{gates,verdict,signals,policy}.py`, `services/{executor,planner,snapshot,season_pruning}.py`, both transport guards (`clients/base.py`'s `GuardedTransport` and its `GuardedSession` twin in `clients/plex.py` — rule 72 means a fix to one is reviewed against the other), the execute route at `api/runs.py:399`, and the arming UI. Spans both trees on purpose. |
| `seam` | route ↔ `api/schemas.py` ↔ frontend client method ↔ props ↔ query keys, for every surface the change touches. |
| `backend` | `src/reaper/**`, `alembic/**` |
| `frontend` | `frontend/src/**` |

`backend` and `frontend` are whole-tree passes. They are expensive and belong on a cadence —
before a `dev` → `main` promotion — not on a normal change.

**Lanes are not a partition of the files, they are a partition of the *risk*.** Reviewing
backend and frontend separately leaves the seam unreviewed by construction: the confirmation
phrase rendered client-side against the one recomputed server-side, query-key invalidation
after execute, a route returning a shape the SPA misreads. Each single-tree reviewer sees half
of that and neither is wrong to skip it. When a change adds or alters a surface, run `seam`.

## Before you look for anything

**Read `references/refuted.md`.** It holds candidates a previous pass raised and an
independent verifier killed. Re-raising one costs a full verify cycle and returns nothing.
Each entry records the commit it was refuted at — if the cited code has changed since, the
refutation is stale and the candidate is live again.

**Do not restate the numbered rules.** `.claude/rules/*.md` holds 133 blockers, distilled from
six adversarial passes, and they load automatically when you read a file they govern — reading
`src/reaper/engine/gates.py` pulls in `backend.md` before you can edit it. They are already in
your context. Never paste them into a prompt, never re-derive them, and never close a review
with a summary of them. **Cite by number** (`violates rule 93`) — that is the whole point of
the numbers being permanent.

## What counts as a finding

Report a defect only when you can state the concrete trigger: the input or state that produces
the wrong output, and what the operator loses. "This could be fragile" is not a finding.

Rank by blast radius, in this order:

1. **A protection that cannot fire, or fires and does not protect** — the silent class. Rules
   38/117, 93, 105, 115 all exist because a guard read as live while covering nothing.
2. **A path that widens what gets deleted** — fail-open on a read error, an empty selection
   expanding to everything, a cap or count computed over a different set than the one acted on.
3. **Loss of the audit trail or the operator's ability to intervene** — a journal write that is
   not durable, a spare that arrives too late to matter.
4. **Everything else** — correctness, security, performance, production readiness.

Within a lane, order findings by that scale, not by a critical/high/medium label alone.

Also sweep for, and fold into the ranking above: hacks and workarounds (`TODO`, `FIXME`,
`temp`, `workaround`, and comments claiming a safeguard — rule 7/24 makes an uncited safety
claim a blocker in itself), missing error handling that fails silently, hardcoded values that
belong in config, and duplication that has already drifted between its copies. Flag a refactor
only when a real defect lives in the duplication; "this could be tidier" is noise.

## Verify before reporting

Every candidate gets an adversarial pass whose job is to **refute** it: open the cited lines,
grep the real call sites, and check that the trigger can actually occur. Default to refuted
when uncertain. This pass has historically killed roughly one candidate in five — the whole
value of the review is that what survives is real.

Correct the line numbers during verification. A finding with a stale line reference costs the
fixing agent a search and earns a plausible wrong edit.

## Output

Report findings with the **`ReportFindings` tool**, most severe first. Do not also write them
out as prose, and do not produce a standalone markdown report — the fixing agent consumes the
structured list directly, and a document in between costs a full write and a full read to
convey the same thing.

The one exception is a whole-tree `backend` or `frontend` pass, which is an event worth
archiving: those land in `docs/history/` under the freeze banner that directory requires.

Two things go in the working tree, not the report:

- **Every refuted candidate** appends to `references/refuted.md`, with the commit it was
  refuted at. This is the only reason the next pass does not re-raise it.
- **A finding that represents a *class* the 133 rules do not cover** proposes exactly one new
  rule, appended at 134+ to the scoped file that governs it. One rule, in the file, in the same
  change. Do not emit a list of "agent rules" — that duplicates `.claude/rules/` and puts
  pressure on numbers that are permanent.

## Applying the fixes

Only when the argument said `fix`. Report first, then apply — never silently.

Apply only findings whose verdict is CONFIRMED, and only where the fix is the one the finding
names. A PLAUSIBLE finding is reported and left alone: its trigger was never proven, so editing
the deletion path on the strength of it trades a hypothetical bug for a real diff. Say which
ones you skipped and why.

Run the gates the change touches before you hand back — for backend edits at minimum
`uv run ruff format .`, `uv run ruff check .`, `uv run mypy src/reaper`, and the test files
covering what you changed. A fix that breaks a test means the finding was wrong about the
mechanism: return the corrected variant, or withdraw the finding to `references/refuted.md`.

Commit each fix as its own story, with the test that pins it, per `CLAUDE.md`.

## Do not

- Re-review stable code that the diff did not touch, on a `diff` lane.
- Comment on code that is fine.
- Report the same defect in two places because it has two aspects.
- Propose a fix you have not checked against the tests that cover the code. The frozen review
  records a verifier who applied a proposed fix, watched 11 tests fail, and returned a
  corrected variant; that is the standard.
