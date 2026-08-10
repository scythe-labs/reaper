# Issue landing plan for the simplification branch

Which of the 32 open issues belong on `audit/simplification-plan` before PR #552 lands, and
which wait for `dev` afterwards. Measured 2026-08-10 against `audit/simplification-plan` at
`9fda787` and `origin/dev` at the same fetch.

This file dies when #552 merges. Its rows either close with the branch or move to the tracker.

## How each row was decided

Two questions, in order.

**Is it already fixed on the branch?** Seven are, because the phase that rewrote the file fixed
the defect on the way past. Those close when the branch lands and cost nothing now.

**Would a `dev` fix collide?** The branch is 322 files. `src/reaper/api/routes.py` and
`frontend/src/components/Settings.tsx` are gone, split into ten new modules; `engine/backtest.py`
and `engine/calibration.py` are deleted; `tests/test_repo_hygiene.py` grew 2,103 lines. A fix
landing on `dev` inside any of those has to be re-resolved by hand during the weekly dev merge,
against a diff nobody can review twice. That is the cost being weighed, and it is the only reason
a non-refactor fix earns a place on the branch.

Everything else lands after. The default is *after*: the branch is at phase 8 of 9 and every
added row is a phase that does not close.

Re-derive any row with `git diff --numstat origin/dev...HEAD -- <path>` and a grep for the fix.

## Verdict

| # | Priority | Lane | Why |
| --- | --- | --- | --- |
| 565 | High | fixed on branch | `src/reaper/db/schema_gate.py`, read from preflight and lifespan |
| 654 | High | fixed on branch | `SWEEP_MAX_PAGES` at `clients/plex.py:88` (#684) |
| 559 | High | fixed on branch | pages on `recordsTotal`, `_SPINE_MAX_PAGES` at `library_index.py:138` |
| 660 | Medium | fixed on branch | `per_loop_lock` at `services/lists.py:733` (#672) |
| 556 | Medium | fixed on branch | `KEY_CHUNK`, `grace.py:116` batches (#618) |
| 551 | Medium | fixed on branch | `policyMeta.ts` declares `season_progression` and `custom`, typed |
| 555 | Medium | fixed on branch | `leaving_soon.py:215` derives it once, route injection and TS ladder gone |
| 682 | Low | on branch, in flight | PR #692 is open against the branch |
| 691 | Low | on branch | `executor.py:1776`, the function #692 is already editing |
| 661 | Low | on branch | `_judge_item` is phase 8's own named parameter-object case |
| 623 | Medium | on branch | `gates.py` is open there, and the fix wants a Tier B re-capture |
| 629 | Low | on branch | `gates.py`, one word, same file as 623 |
| 624 | Low | on branch | the branch rewrote `lsStatus` and moved `LeavingSoonRow` to `JobsPanel.tsx` |
| 584 | Medium | on branch | `_sync_libraries` moved to `src/reaper/api/plex.py` |
| 685 | Medium | on branch | `test_repo_hygiene.py` grew 2,103 lines there |
| 598 | Medium | on branch | `scripts/mutation_scope.py` is touched, and its target module moved |
| 550 | Medium | on branch | four of five comments are already gone there; one is left |
| 576 | Low | on branch | half of it went with `tests/test_calibration.py` |
| 657 | Medium | after | changes what a stored rule matches; the issue asks for its own PR |
| 549 | Low | after | PR #560 is already open against `dev` |
| 566 | High | after | builds on #565, which has not landed yet |
| 622 | Medium | after | `launcher.py`, no collision |
| 558 | Medium | after | `launcher.py` and `config.py`, no collision |
| 557 | Low | after | `api/scan.py` is untouched by the branch |
| 658 | Low | after | `config.py`, one line, no collision |
| 589 | Low | after | `ci.yml`, but see the caution below |
| 688 | Low | after | copy only, and it needs a mockup first |
| 651 | Low | after | test timing, no collision |
| 606 | Low | after | phase 6 dropped the row on purpose |
| 607 | Low | after | phase 6 dropped the row on purpose |
| 553 | Medium | after | feature |
| 554 | Low | after | feature |

## Fixed on the branch, nothing to do

Seven issues describe defects that no longer exist on `audit/simplification-plan`. They stay open
because an operator on `dev` can still hit them, which is the tracker working as intended.

**Close them in the #552 merge, not before.** Each one's closing comment names the commit on the
branch that fixed it, so the record survives the squash.

| # | Where it is fixed |
| --- | --- |
| 565 | `src/reaper/db/schema_gate.py`, with `refusal()` read from `preflight` and `main.lifespan` |
| 654 | `src/reaper/clients/plex.py:88`, `SWEEP_MAX_PAGES` tripping at `:516` and raising |
| 660 | `src/reaper/services/lists.py:733`, `_widen_lock = per_loop_lock()` |
| 559 | `src/reaper/services/library_index.py:138` and the `recordsTotal` read at `:293` |
| 556 | `src/reaper/services/grace.py:116`, batched on `KEY_CHUNK` |
| 551 | `frontend/src/components/policyMeta.ts:169,174`, both ids labeled and the map typed |
| 555 | `src/reaper/services/leaving_soon.py:215`, one derivation, pinned by `JobsShelfSkip.test.tsx` |

## On the branch, in this order

Eleven issues. Each is a sub-PR cut from `audit/simplification-plan` and squash-merged into it,
the same way every phase item arrives.

**1. #682, land PR #692.** Already open, already reviewed, three files. It unblocks #691, which
edits the same file.

**2. #691, in the same file.** Status is `Need More Info`, so drive it first: plan an item, spare
it, claim the run, remove the spare mid-run, read `outcome.detail` on the skip. Confirmed means one
copy fix at `executor.py:1776` and a test. Unreachable means close it `Reviewed/Invalid` and add
the refutation to `references/refuted.md`.

**3. #550, the fifth comment.** `src/reaper/services/planner.py:172` says `_movie_steps` returns
"delete-with-exclusion, then verify, then refresh" and the body returns `[delete]`. The branch
already removed the other four claims. `planner.py` is untouched by the branch, so this is one
edit with no rebase cost, and it closes an issue the branch otherwise leaves 80% done.

**4. #629, one word in `gates.py`.** `GateId.CUSTOM`'s docstring names `custom_condemn`, the
removal lane, for a keep gate. Point it at `protect_conditions` and cite `fields.CustomProtectGate`.

**5. #623, `describe_bar`, same file.** Give the vote-floor clause its own wording so `from N
votes` stops meaning two things. It cannot be a bare `+` append: a floor of 1 has to read
`from 1+ votes`. Its test fails with a message naming `PolicyEditor.tsx`'s `describeBar`, per rule
144. This one re-captures Tier B, which the branch's per-PR protocol already runs.

**6. #661, `_judge_item`'s dead `grace_days`.** Phase 8 holds W3's parameter objects and the plan
names `_judge_item` as its sharpest case. Dropping the parameter on `dev` instead would hand phase
8 a signature that moved under it. Three lines, 27 parameters to 26.

**7. #624, the shelf status line.** The branch rewrote this exact closure for the skip case
(`PlexPanel.tsx:489`) and moved `LeavingSoonRow` into `JobsPanel.tsx`. Gate `lsStatus` on
`leavingSoon.data.enabled` the way the Jobs row does, or say "Off" in its place. A `dev` fix would
land inside a function body the branch replaced.

**8. #584, the Plex library sync success path.** `_sync_libraries` now lives in
`src/reaper/api/plex.py`, so a test written on `dev` names a file that no longer holds it. Stub
`PlexClient.video_sections` to return sections and assert the merge: a disabled library stays
disabled, a new one arrives enabled, a vanished one drops out.

**9. #576, the remaining SQLite connection.** `tests/test_calibration.py` went with
`engine/calibration.py`, so only `tests/test_list_config.py` is left. Close the connection in
teardown.

**10. #685, the rule-scope gate.** `test_scoped_rule_files_declare_their_paths` asserts only that
`paths:` exists. Compare each file's frontmatter against `CLAUDE.md`'s `Governs` cell.
`test_repo_hygiene.py` is 2,103 lines longer on the branch, so a `dev`-side test lands in the
middle of that diff. Two instances of this class were already found by hand, which is what earns
the gate.

**11. #598, the mutation zone.** Declare `_blocked`, `GateResult.fired`,
`RatingFloorGate._miss_phrase` and the four `Evaluation` properties, then add the check that fails
when a zone's list and its module's callables disagree. Rule 103 applied to the zone list.
`scripts/mutation_scope.py` is touched on the branch and `gates.py` moved under it.

### Two issue bodies the branch invalidates

Not defects, and not filings. The branch created them, so the branch fixes them (CLAUDE.md, "a
defect your own unlanded branch created").

- **#554** opens with "read `engine/calibration.py` before starting: it records two mistakes made
  while writing it." The branch deletes that file. The wrong-population lesson survives at
  `docs/LEARNINGS.md:487`. Repoint the issue there and cite the pre-deletion sha.
- **#553** calls itself the successor to `engine/backtest.py`, also deleted. Same edit.

Do both before #552 goes ready, or the two feature issues cite files nobody can open.

## After the branch lands

Fourteen issues, no collision with the refactor. Nothing here needs sequencing against #552 beyond
waiting for it.

**#566 waits on #565 by construction.** The pre-migration snapshot is a call site plus a retention
bound, and the revision-skew guard it sits beside is on the branch. Build it on top of
`schema_gate.py` rather than beside a copy of it.

**#657 must not ride along.** The issue says so itself, and the branch's verification rests on
every operator string and every decision being byte-identical before and after. A one-line change
to what a stored `contains` rule matches breaks that reading. Its own PR, with a mixed-case test
and a Tier B re-capture.

**#549 is already moving.** PR #560 targets `dev` and touches `api/schemas.py`, `engine/policy.py`
and `api/lists.py`, all three of which the branch edits. That is fine and by design: land it on
`dev`, and the branch absorbs it at the next weekly merge. The alternative, holding it for weeks,
costs more.

**#589 needs care that its size hides.** Reordering `ci.yml`'s `case` arms is two lines, and
`tests/test_repo_hygiene.py` pins all three workflow path lists by name. The second half of the
issue, `docs-deploy.yml` editing itself into the `code` lane, is a separate edit to the same file.

**#688 needs a mockup first.** Operator copy on the Policy editor, so the golden rule applies:
load the `reaper-artifact` skill and get the help text approved as a rendered artifact before
touching `PolicyEditor.tsx`.

**#606 and #607 are the phase-6 leftovers.** Both were dropped from the refactor on purpose,
because neither is pure motion. They are the natural first pieces of frontend work after #552,
and each wants rule 146 driven through every child's loading and failed-read branch, not just the
happy path.

The rest are independent and small: **#622** (route two launcher refusals through `_say`),
**#558** (four flags and the port onto `Settings`), **#557** (two `add_done_callback` lines),
**#658** (fold `slot` the way its two siblings are folded), **#651** (warm the wizard boundary or
raise that one assertion's timeout). **#553** and **#554** are features and take a design pass
each.

## The one call for the owner

Three `Priority/High` issues are fixed on the branch and unreachable from `dev`: **#654**, **#559**
and **#565**. Two `Priority/Medium` join them, **#660** and **#556**. An operator running `dev`
today can still hit all five, and the branch is at phase 8 of 9.

Two ways to go.

1. **Wait for #552.** No extra merges, no cherry-pick conflicts, and the five close in one
   sweep. Right if phase 8 and 9 are weeks away.
2. **Cherry-pick the three High ones onto `dev` now.** Each is a self-contained commit
   (`SWEEP_MAX_PAGES`, the `recordsTotal` walk, `schema_gate.py`). The branch then absorbs its own
   change back at the next weekly dev merge, which resolves clean because the trees agree. Right
   if phase 9 is not close.

Recommended: **wait**, and revisit if phase 9 has not started in two weeks. None of the five loses
a file. #654 and #559 cost availability and a partial read; #565 costs a confusing failure after a
rollback. All three fail toward keeping.
