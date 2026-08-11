# Issue landing plan for the simplification branch

Which of the 44 open issues belong on `audit/simplification-plan` before PR #552 lands, and
which wait for `dev` afterwards. Measured twice. The first 32 rows on 2026-08-10 against
`audit/simplification-plan` at `9fda787`, the twelve issues filed since on 2026-08-11 against
the branch at `b03b0b2`. `origin/dev` was `4bd2c02` both times. The older rows were not
re-measured, apart from five claims the second pass found stale and corrected in place: #682's
lane, #691's and #557's evidence, #550's file count, and the `test_repo_hygiene.py` example below.

This file dies when #552 merges. Its rows either close with the branch or move to the tracker.

## How each row was decided

Two questions, in order.

**Is it already fixed on the branch?** Nine are, because the phase that rewrote the file fixed
the behavior on the way past. Those close when the branch lands and cost nothing now.

**Would a `dev` fix collide?** The branch is 368 files, 322 when this was first measured.
`src/reaper/api/routes.py` is deleted and its routes are seven new modules;
`frontend/src/components/Settings.tsx` survives as a shell, 2,875 lines lighter, its panels split
into seven files; `engine/backtest.py` and `engine/calibration.py` are deleted. A fix landing on
`dev` inside any of those has to be re-resolved by hand during the weekly dev merge, against a diff
nobody can review twice.

**Measure the collision at the fix site, never at the file.** This is the rule that moved five rows
between lanes on review, in both directions. `src/reaper/engine/gates.py` shows 46/45 and
`describe_bar` is byte-identical on both trees. A file the branch touched is not a collision. A
file whose fix site the branch replaced, moved or deleted is. `git diff origin/dev...HEAD --
<path>` and read the hunk headers against the line the fix needs.

`tests/test_repo_hygiene.py` was this paragraph's second example and no longer is. It has grown
3,603 lines over 39 hunks, and 2,329 of them are appended past the end of `dev`'s file, which is
the anchor a `dev`-side test would append at too. The clean-append reading was true at `9fda787`
and is not true now.

**A change to what the engine decides waits for `dev`.** A change to how a decision is worded may
ride the branch, since the branch already re-captures Tier B per PR. That is why #657 waits and
#682 does not: one changes what a stored rule matches, the other changes a sentence.

Everything else lands after. The default is *after*: the branch is at phase 8 of 9, and every added
row is a phase that does not close.

## Verdict

| # | Priority | Lane | Why |
| --- | --- | --- | --- |
| 565 | High | fixed on branch | `src/reaper/db/schema_gate.py`, read from preflight and lifespan |
| 654 | High | fixed on branch | `SWEEP_MAX_PAGES` at `clients/plex.py:88` (#684) |
| 559 | High | fixed on branch | pages on the reported count, degrades short (#578) |
| 660 | Medium | fixed on branch | `per_loop_lock` at `services/lists.py:733` (#672) |
| 556 | Medium | fixed on branch | `KEY_CHUNK`, `grace.py:116` batches (#618) |
| 551 | Medium | fixed on branch | `policyMeta.ts` labels all four ids, and the map is typed |
| 555 | Medium | fixed on branch | `leaving_soon.py:201` derives it once, route injection gone |
| 682 | Low | fixed on branch | `executor.py:2393` says "No files left to remove" (#692) |
| 712 | Low | fixed on branch | `useScanStatus.ts:20` states the shell's second observer (#732) |
| 691 | Low | on branch | #692's landed rig is what settles a `Need More Info` row |
| 624 | Low | on branch | the branch replaced the whole `lsStatus` closure |
| 584 | Medium | on branch | `_sync_libraries` moved to `src/reaper/api/plex.py:599` |
| 598 | Medium | on branch | the branch edits the same `functions=` tuple the fix appends to |
| 685 | Medium | on branch | both of the gate's inputs moved on the branch |
| 558 | Medium | on branch | four of half one's four fix sites were rewritten |
| 622 | Medium | on branch | the branch added a third invisible refusal the dev fix misses |
| 704 | Medium | on branch | `App.test.tsx`'s mock literal is gone; 6 of the 20 are branch-only |
| 657 | Medium | after | changes what a stored rule matches; port the fold, see below |
| 549 | Low | after | PR #560 is open on `dev`; the port is real work, see below |
| 566 | High | after | builds on #565, which has not landed yet |
| 550 | Medium | after | `services/planner.py` is not among the 368 |
| 623 | Medium | after | `describe_bar` is byte-identical on both trees |
| 629 | Low | after | the docstring is byte-identical on both trees |
| 661 | Low | after | phase 8 killed the parameter objects; the signature will not move |
| 557 | Low | after | both `create_task` sites are untouched context |
| 658 | Low | after | `config.py:290` sits between two branch hunks |
| 589 | Low | after | the `changes` job's `case` block is untouched |
| 688 | Low | after | the string sits between two branch hunks; needs a mockup |
| 576 | Low | after | half went with the deleted file; the rest may be invalid |
| 651 | Low | after | merges, but the measurement needs redoing after #552 |
| 606 | Low | after | phase 6 dropped the row on purpose |
| 607 | Low | after | phase 6 dropped the row on purpose |
| 709 | High | after | changes what a protection lookup matches; eleven sites, all untouched context |
| 700 | Medium | after | `logbuffer.py` and the Unraid template are untouched on the branch |
| 729 | Medium | after | `_decrypted_or_absent` replaced both fix sites; port, see below |
| 718 | Low | after | both sites untouched; `_EXPECTED_STANDING` already moved, see below |
| 726 | Low | after | `PolicyRuleEditors.test.tsx:463` pins the wrong string, branch-only |
| 734 | Low | after | the `except` arm is a three-class tuple on the branch; port, see below |
| 736 | Low | after | `14-policy-editor.css` is byte-identical; the pin is branch-only |
| 740 | Low | after | the rule is untouched; the shared hover it folds into is branch-only |
| 748 | Low | after | the block is `_write_desktop_values` on the branch; port, see below |
| 710 | Low | after | `sweep_expired_sessions` is untouched; the `start_pin` half is branch-only |
| 553 | Medium | after | feature |
| 554 | Low | after | feature |

## Fixed on the branch, nothing to do

Nine issues describe behavior that no longer exists on `audit/simplification-plan`. They stay open
because an operator on `dev` can still hit them, which is the tracker working as intended.

**Close them in the #552 merge, not before.** Each one's closing comment names the commit on the
branch that fixed it, so the record survives the squash.

| # | Where it is fixed |
| --- | --- |
| 565 | `src/reaper/db/schema_gate.py:167`, `refusal()` read from `preflight.py:78` and `main.py:135` |
| 654 | `src/reaper/clients/plex.py:88`, `SWEEP_MAX_PAGES` tripping at `:516` and raising |
| 660 | `src/reaper/services/lists.py:733`, with the PRAGMA re-read inside the lock at `:810` |
| 559 | `src/reaper/services/library_index.py:138`, the count read at `:295`, degrading at `:321` |
| 556 | `src/reaper/services/grace.py:116`, batched on `KEY_CHUNK` from `db/__init__.py:25` |
| 551 | `frontend/src/components/policyMeta.ts:169`, all four ids labeled and the map typed |
| 555 | `src/reaper/services/leaving_soon.py:201`, one ladder, no-libraries tested first |
| 682 | `src/reaper/services/executor.py:2393`, the no-files skip split from the size skip |
| 712 | `frontend/src/useScanStatus.ts:20`, the shell's second observer stated; `ScanBar.tsx:104` |

**Two of them landed the drift guard the issue asked for, and the closing comment should say so.**
#551 closed its mirror with `satisfies Record<GateId | "hand_spare", GateMeta>` plus
`test_api_type_mirror.py:598`, so a new gate cannot ship without operator copy. #556 grew
`test_repo_hygiene.py:5426`, which walks the AST for all three `IN` spellings and requires a
written classification per site. Naming only the fix under-reports what the branch delivered.

#555 is pinned on both sides of the seam: `tests/test_leaving_soon.py:181`,
`tests/test_leaving_soon_settings.py:224` and `JobsShelfSkip.test.tsx:54` all assert the exact
string.

## On the branch, in this order

Eight issues. Each is a sub-PR cut from `audit/simplification-plan` and squash-merged into it, the
same way every phase item arrives. Every row here was checked at its fix site.

**1. #691, using #692's rig.** It is `Status/Need More Info`, and the fix site is `_send_for_real`
at `executor.py:1778`, roughly 600 lines from #692's `_send_season`. The reason it belongs here is
the rig, not the diff: #692 landed on 2026-08-10 and stands up an armed executor against a stub
Sonarr, which is what settles this. Plan an item, spare it, claim the run, remove the spare
mid-run, read `outcome.detail`. Confirmed means one copy fix. Unreachable means close it
`Reviewed/Invalid` and write the refutation into `references/refuted.md`.

**2. #624, the shelf status line.** The strongest collision of the eight. The branch's hunk
`@@ -505,11 +489,36` replaces the whole `lsStatus` closure, five lines becoming thirty, and
`LeavingSoonRow` moved into the new `JobsPanel.tsx`. The issue's "Where" section names lines that
no longer exist. The defect survives the rewrite: the new closure still never reads
`leavingSoon.data.enabled`. Gate it the way the Jobs row does, or say "Off" in its place.

**3. #584, the Plex library sync success path.** `_sync_libraries` moved from
`api/settings.py:1201` to `api/plex.py:599`, and `tests/test_settings_api.py` changed inside
`TestPlexLinkChoice` itself. A test written on `dev` names a file that no longer holds the
function. Stub `PlexClient.video_sections` to return sections and assert the merge: a disabled
library stays disabled, a new one arrives enabled, a vanished one drops out.

**4. #598, the mutation zone.** The branch's hunk `@@ -1168,8 +1153,6` removes two entries from the
same `engine-gates` `functions=` tuple this fix appends to, and two other zones re-pointed `module=`
at `policy_migrations.py` and `policy_warnings.py`, which the proposed drift check has to read.
Declare `_blocked`, `GateResult.fired`, `RatingFloorGate._miss_phrase` and the four `Evaluation`
properties, then add the check that fails when a zone's list and its module's callables disagree.

**5. #685, the rule-scope gate.** Not because `test_repo_hygiene.py` is long, though a `dev`-side
append now lands on the same anchor as the branch's own 2,329 lines. It belongs here because **both
of the gate's inputs moved on the branch**: `.claude/rules/*.md` is +74/-26 across five files and
`CLAUDE.md` is +26/-5. A gate written against `dev`'s scopes would be asserting the wrong pairs the
day it lands.

**6. #558, half one.** The document first put this in "after" and that was wrong. All four fix
sites of half one were rewritten: `update_check.py:119`, `launcher.py:325`, `:377` and `:578` are
now `env_flag(...)` calls, and `_TRUE`/`_FALSE` were deleted from both modules. Promote the four
flags to `Settings` on the branch, against the shape that exists there. **Half two is genuinely
untouched** (`_port` at `:276`, `REAPER_HOST` at `:533`, `config.py:116-117`), so the port and host
half can go either way. Doing both here keeps one issue in one place.

**7. #622, and this one has a safety consequence.** The branch **added a third fatal stderr-only
refusal**: `preflight.main` now writes `schema_gate.refusal`'s message and returns an int, the same
shape as the two the issue names. #622's fix gives `preflight.main` a `refuse` callback "used for
the two fatal messages only." A fix written on `dev` covers two and leaves the branch's third
invisible on a frozen desktop build, which is the exact fail-open class the issue exists for. The
branch also inserted four comment lines immediately after `raise SystemExit(code)`, the second fix
site verbatim. Write it here, against three refusals.

**8. #704, the suite's undefined-read gate.** Same shape as #622, one lane over. `setup.ts` is
byte-identical on both trees, so the three-line gate merges, and it would then be measuring a tree
that has six more failures than the one it was written against: `SetupPlexStep.test.tsx` carries 6
of the 20 and exists only here. The `App.test.tsx` half is a collision outright, the branch having
replaced `dev`'s hand-listed `apiMock` literal with `makeApiMock()` from `test/apiMock.ts`, which
is where the missing `vocabularyValues` answer now goes. Last of the eight because the count is
measured rather than reasoned and should be re-read against the settled tree. It still reads 20 on
the branch (`npx vitest run --disableConsoleIntercept`, 2026-08-11, exit 0). Fix both files first,
then add the gate, or it lands red.

### Three issue bodies the branch invalidates

Not defects, and not filings. The branch created them, so the branch fixes them (CLAUDE.md, "a
defect your own unlanded branch created").

- **#554** sends a reader to `engine/calibration.py:22` for two recorded mistakes and says to read
  it before starting. The branch deletes the file. The wrong-population lesson survives verbatim at
  `docs/LEARNINGS.md:487`. Repoint the citation there and name the pre-deletion sha so the
  sample-size half stays reachable.
- **#553** cites `engine/backtest.py` as its predecessor, also deleted. The sha is enough.
- **#553 again**, and this one is easy to miss: it cites `api/settings.py:1327` for the fact that a
  file which leaves the library and returns gets a new Plex id. That comment now lives at
  `src/reaper/api/plex.py:726`.

Do #554's repoint before #552 goes ready. It is the one that asks someone to open a file that will
not be there.

## After the branch lands

Twenty-seven issues. All but five carry a caution that is not obvious from the issue text.

**#657 must not ride along, and it still needs a port.** The issue says so itself, and the branch's
verification rests on every decision being byte-identical. But the branch rewrote both sibling arms
of the same `match`: `Op.EQ` and `Op.IN` now fold through `fold(...)`, and the defective
`Op.CONTAINS` line is two lines of trailing context away. A fix spelled `.strip().casefold()` on
`dev` merges textually and leaves one arm on an idiom the branch retired. Write it as `fold(...)`
so the port is a no-op.

**#549's port is real work, not a merge.** PR #560 adds 45 lines to `engine/policy.py:1395`, which
sits inside the branch's `@@ -996,1601 +994,26` deletion, and `own_list_media_scope` now lives at
`engine/policy_migrations.py:369`. That is a modify/delete conflict resolved by hand into a file
that does not exist on `dev`. Landing it on `dev` is still right, since holding a finished PR for
weeks costs more. Whoever runs the next weekly dev merge should expect this one.

**#566 waits on #565 by construction.** The pre-migration snapshot is a call site plus a retention
bound, and the revision-skew guard it sits beside is on the branch. Build it on top of
`schema_gate.py` rather than beside a copy of it.

**#661's stated reason died.** An earlier draft put this on the branch because phase 8 held W3's
parameter objects. `docs/SIMPLIFICATION_PLAN.md:1706` records the opposite: "Killed: all six
parameter objects. One gate lands instead," and #687 landed that gate. Its `_LANE_ARGUMENTS` set
does not contain `grace_days`, so phase 8 will never touch `_judge_item`'s signature. The three
lines merge clean.

**#576 may be closable as invalid.** The `tests/test_calibration.py` half went with
`engine/calibration.py`. The remaining half could not be reproduced: neither file opens a bare
`sqlite3.Connection`, every engine is disposed, and the issue itself records that it does not
reproduce per-file. Re-measure on the merged tree before writing a fix, and close it
`Reviewed/Invalid` if the warnings are gone.

**#651's measurement expires with the branch.** The assertion merges, but the branch rewrote
`AppStaleRead.test.tsx`'s mock scaffolding and `renderApp`, and the issue's measured rescuers all
work through `Settings.tsx`, which lost 2,875 lines. Re-run the isolated-file measurement after
#552 before fixing anything.

**#623 and #629 came off the branch on review.** Both are `gates.py`, and both fix sites are
byte-identical on the two trees. The branch rewrote `_miss_phrase`, which is `describe_bar`'s
caller, and edited the docstring of the enum member *preceding* `GateId.CUSTOM`. Neither is a
collision. #623 still needs its own PR for its own reason: it reverses a deliberate rule 104
consolidation, and its test must fail with a message naming `PolicyEditor.tsx`'s `describeBar`.

**#550's fifth comment is one edit with no rebase cost.** `services/planner.py` is not among the
368. Landing it on `dev` reaches operators sooner, and the issue closes whenever the fifth site
does. `_movie_steps` promises "delete-with-exclusion, then verify, then refresh" and returns
`[delete]`.

**#589 needs care that its size hides.** `ci.yml`'s only branch change is the Typecheck step, so
the `changes` job's `case` block and `docs-deploy.yml` are both untouched and the reorder merges.
`tests/test_repo_hygiene.py` pins all three workflow path lists by name, and the issue's second
half, `docs-deploy.yml` editing itself into the `code` lane, is a separate edit to the same file.

**#688 needs a mockup first.** Operator copy on the Policy editor, so the golden rule applies: load
the `reaper-artifact` skill and get the help text approved as a rendered artifact before touching
`PolicyEditor.tsx`.

**#606 and #607 are the phase-6 leftovers.** Both were dropped from the refactor on purpose,
because neither is pure motion. They are the natural first frontend work after #552, and each wants
rule 146 driven through every child's loading and failed-read branch, not just the happy path.

**#709 is the new `Priority/High`, and the engine rule holds it back.** All eleven sites the issue
names are untouched context on the branch (`snapshot.py:438`, `lists.py:278`, `season_scan.py:1230`,
`seerr.py:217` and `identity.py:207`, `dev` lines), so nothing about the merge argues for moving
it. Cleaning the id changes what the protection-list lookup matches, which is a decision and not a
wording, and the issue is `Status/Need More Info` besides: nobody has captured a source emitting
the sentinel, and the unit test that settles it runs on either tree. One branch item rides with it.
`docs/SIMPLIFICATION_PLAN.md:193` records W11-2 as deferred rather than killed, because it is the
carrier for this issue's raw `imdbId` writes and nothing else wants it.

**#729, #734 and #748 merge textually into a shape that is gone. Port them, do not merge them.**
#729's fix site is the `try` / `except ValueError` block in both `get_discord_webhook` and
`has_discord_webhook`; the branch replaced both with `_decrypted_or_absent` at
`app_settings.py:185`, which also serves `get_api_key`, so one clause there reaches further than
the issue measured. #734's `except PlexError` arm is now the three-class tuple at
`api/leaving_soon.py:40`, and the comment above it names the issue. #748's launcher write moved
into `_write_desktop_values` at `api/settings.py:1351`, called one line before the commit at
`:1399`, so a `dev`-side reorder of the old inline block is a modify/delete.

**#726 and #736 are pinned by tests that arrive with the branch.** #726's expected string is
written `"7.5 /10"` at `PolicyRuleEditors.test.tsx:463`, and #736's `.bar-x` padding is pinned at
`styles-chip-dismiss.test.ts:131`. Neither file exists on `dev`, so a fix landing there is green
until #552 merges and red after. Land the fix and its expectation together, on whichever tree.

**#718's counter merges to the wrong number.** Both fix sites are untouched context, but the branch
has already moved `_EXPECTED_STANDING` from 35 to 36. Taking the "pass `standing`" repair moves the
same constant to the same value on `dev`, the merge keeps one 36, and the true count on the merged
tree is 37. Narrowing the comment at `PolicyEditor.tsx:1833` on `dev` instead moves nothing.

**#710 and #740 each have a half that exists on one tree only.** #710's sweeper belongs in
`sweep_expired_sessions` (`scheduler.py:495` on `dev`, untouched), but the opportunistic delete it
replaces sits in `start_link`, which the branch rewrote as `start_pin` at `plex_link.py:150`.
#740's first repair, qualifying the selector, merges; its second, folding the tint into the shared
chip-dismiss rule, has no shared rule to fold into on `dev` (`04-buttons.css:88` is branch-only).

The rest are independent and small: **#557** (two `add_done_callback` lines, both sites untouched
context), **#658** (fold `slot` the way its two siblings are folded), **#700** (`logbuffer.py` and
`contrib/unraid/my-Reaper.xml` carry no branch change at all, and `config.py:126` only shifted).
**#553** and **#554** are features and take a design pass each.

## The one call for the owner

Three `Priority/High` issues are fixed on the branch and unreachable from `dev`: **#654**, **#559**
and **#565**. Two `Priority/Medium` join them, **#660** and **#556**. An operator running `dev`
today can still hit all five, and the branch is at phase 8 of 9.

**Recommended: wait for #552.** Three measurements say so, and the third is decisive.

**#565's hazard is on the branch too.** The issue's own text says the failure is rare while every
migration is additive and certain once a release drops a column. The first destructive revision is
`alembic/versions/20260808_1200_release_m_for_six_retired_columns.py`, and it exists **only on
`audit/simplification-plan`**. The guard and the thing it guards against land in the same PR.
Cherry-picking the guard forward buys an operator nothing, because no `dev` database can reach the
state it refuses.

**Two of the three do not cherry-pick.** Simulated against an index seeded from `origin/dev`:

| # | Commit | Files | Applies to `dev` |
| --- | --- | --- | --- |
| 654 | `fda8452` | 3 | yes, clean |
| 559 | `5d85732` | 3 | no, `tests/test_degraded_side_effects.py` rejects on branch-only test work |
| 565 | `2ddadac` | 15 | no, it carries `src/reaper/api/about.py`, a phase-6 split product |

Only #654 is the self-contained commit an earlier draft claimed all three were. The other two would
have to be re-authored against `dev`, which means writing the fix twice and reviewing it twice, for
a branch that is days from closing.

**The branch is days from closing, not weeks.** 114 commits in four days. Phase 8 was claimed
2026-08-09 and stands at 17 landed, 7 killed, with two rows open; phase 9 has two items.

**Nothing here loses a file.** #654 costs availability: an unbounded sweep leaves
`scan_runner._scan_running` set at `scan_runner.py:83`, so every later scan is refused until
restart. #660 aborts a scan, which deletes nothing, through a one-shot window that closes after the
first successful widen. #556 needs more than 32,766 condemned keys and breaks the Leaving Soon
report, not an interlock. #565 is inert on `dev` for the reason above.

**#559 is the one whose exposure this document had wrong.** The library index appends every swept
rating key the spine missed, so a truncated Tautulli read is refilled from Plex whenever the sweep
is alive, and a sweep that is also empty degrades the snapshot. What degrades silently is
provenance: refilled rows carry Plex's `addedAt` rather than Tautulli's, and the module docstring
names the spine's `added_at` as what keeps dormancy stable. That is a shift in condemn pressure
with no degrade beside it, which is worse than the partial read first claimed, and still not a lost
file.

**Revisit if phase 9 has not started within a week.** The earlier two-week tripwire was ten times
the observed per-phase duration and would never have fired.
