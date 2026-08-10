# Issue landing plan for the simplification branch

Which of the 32 open issues belong on `audit/simplification-plan` before PR #552 lands, and
which wait for `dev` afterwards. Measured 2026-08-10 against `audit/simplification-plan` at
`9fda787` and `origin/dev` at `4bd2c02`.

This file dies when #552 merges. Its rows either close with the branch or move to the tracker.

## How each row was decided

Two questions, in order.

**Is it already fixed on the branch?** Seven are, because the phase that rewrote the file fixed
the behavior on the way past. Those close when the branch lands and cost nothing now.

**Would a `dev` fix collide?** The branch is 322 files. `src/reaper/api/routes.py` is deleted and
its routes are seven new modules; `frontend/src/components/Settings.tsx` survives as a shell, 2,875
lines lighter, its panels split into seven files; `engine/backtest.py` and `engine/calibration.py`
are deleted. A fix landing on `dev` inside any of those has to be re-resolved by hand during the
weekly dev merge, against a diff nobody can review twice.

**Measure the collision at the fix site, never at the file.** This is the rule that moved five rows
between lanes on review, in both directions. `tests/test_repo_hygiene.py` grew 2,103 lines and
still merges a `dev`-side test cleanly, because the growth is one append at the end.
`src/reaper/engine/gates.py` shows 46/45 and `describe_bar` is byte-identical on both trees. A file
the branch touched is not a collision. A file whose fix site the branch replaced, moved or deleted
is. `git diff origin/dev...HEAD -- <path>` and read the hunk headers against the line the fix
needs.

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
| 682 | Low | on branch, in flight | PR #692 is open, and it edits the exact line |
| 691 | Low | on branch | #692's drive is what settles a `Need More Info` row |
| 624 | Low | on branch | the branch replaced the whole `lsStatus` closure |
| 584 | Medium | on branch | `_sync_libraries` moved to `src/reaper/api/plex.py:599` |
| 598 | Medium | on branch | the branch edits the same `functions=` tuple the fix appends to |
| 685 | Medium | on branch | both of the gate's inputs moved on the branch |
| 558 | Medium | on branch | four of half one's four fix sites were rewritten |
| 622 | Medium | on branch | the branch added a third invisible refusal the dev fix misses |
| 657 | Medium | after | changes what a stored rule matches; port the fold, see below |
| 549 | Low | after | PR #560 is open on `dev`; the port is real work, see below |
| 566 | High | after | builds on #565, which has not landed yet |
| 550 | Medium | after | `services/planner.py` is not among the 322 |
| 623 | Medium | after | `describe_bar` is byte-identical on both trees |
| 629 | Low | after | the docstring is byte-identical on both trees |
| 661 | Low | after | phase 8 killed the parameter objects; the signature will not move |
| 557 | Low | after | `api/scan.py` is not among the 322 |
| 658 | Low | after | `config.py:290` sits between two branch hunks |
| 589 | Low | after | the `changes` job's `case` block is untouched |
| 688 | Low | after | the string sits between two branch hunks; needs a mockup |
| 576 | Low | after | half went with the deleted file; the rest may be invalid |
| 651 | Low | after | merges, but the measurement needs redoing after #552 |
| 606 | Low | after | phase 6 dropped the row on purpose |
| 607 | Low | after | phase 6 dropped the row on purpose |
| 553 | Medium | after | feature |
| 554 | Low | after | feature |

## Fixed on the branch, nothing to do

Seven issues describe behavior that no longer exists on `audit/simplification-plan`. They stay open
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

**1. #682, land PR #692.** Already open, three files, and the branch's own hunk at
`executor.py:2381` replaces the exact `check=` string the issue is about. Nothing to re-decide.

**2. #691, using #692's drive.** It is `Status/Need More Info`, and the fix site is
`_send_for_real`, roughly 600 lines from #692's `_send_season`. The reason it belongs here is the
drive, not the diff: #692 already stands up an armed executor against a stub Sonarr, which is the
rig that settles this. Plan an item, spare it, claim the run, remove the spare mid-run, read
`outcome.detail`. Confirmed means one copy fix. Unreachable means close it `Reviewed/Invalid` and
write the refutation into `references/refuted.md`.

**3. #624, the shelf status line.** The strongest collision of the eight. The branch's hunk
`@@ -505,11 +489,36` replaces the whole `lsStatus` closure, five lines becoming thirty, and
`LeavingSoonRow` moved into the new `JobsPanel.tsx`. The issue's "Where" section names lines that
no longer exist. The defect survives the rewrite: the new closure still never reads
`leavingSoon.data.enabled`. Gate it the way the Jobs row does, or say "Off" in its place.

**4. #584, the Plex library sync success path.** `_sync_libraries` moved from
`api/settings.py:1201` to `api/plex.py:599`, and `tests/test_settings_api.py` changed inside
`TestPlexLinkChoice` itself. A test written on `dev` names a file that no longer holds the
function. Stub `PlexClient.video_sections` to return sections and assert the merge: a disabled
library stays disabled, a new one arrives enabled, a vanished one drops out.

**5. #598, the mutation zone.** The branch's hunk `@@ -1168,8 +1153,6` removes two entries from the
same `engine-gates` `functions=` tuple this fix appends to, and two other zones re-pointed `module=`
at `policy_migrations.py` and `policy_warnings.py`, which the proposed drift check has to read.
Declare `_blocked`, `GateResult.fired`, `RatingFloorGate._miss_phrase` and the four `Evaluation`
properties, then add the check that fails when a zone's list and its module's callables disagree.

**6. #685, the rule-scope gate.** Not because `test_repo_hygiene.py` is long. The test itself is
byte-identical, and a `dev`-side append would merge. It belongs here because **both of the gate's
inputs moved on the branch**: `.claude/rules/*.md` gained 68 lines across five files and
`CLAUDE.md` is +26/-5. A gate written against `dev`'s scopes would be asserting the wrong pairs the
day it lands.

**7. #558, half one.** The document first put this in "after" and that was wrong. All four fix
sites of half one were rewritten: `update_check.py:119`, `launcher.py:325`, `:377` and `:578` are
now `env_flag(...)` calls, and `_TRUE`/`_FALSE` were deleted from both modules. Promote the four
flags to `Settings` on the branch, against the shape that exists there. **Half two is genuinely
untouched** (`_port` at `:276`, `REAPER_HOST` at `:533`, `config.py:116-117`), so the port and host
half can go either way. Doing both here keeps one issue in one place.

**8. #622, and this one has a safety consequence.** The branch **added a third fatal stderr-only
refusal**: `preflight.main` now writes `schema_gate.refusal`'s message and returns an int, the same
shape as the two the issue names. #622's fix gives `preflight.main` a `refuse` callback "used for
the two fatal messages only." A fix written on `dev` covers two and leaves the branch's third
invisible on a frozen desktop build, which is the exact fail-open class the issue exists for. The
branch also inserted four comment lines immediately after `raise SystemExit(code)`, the second fix
site verbatim. Write it here, against three refusals.

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

Seventeen issues. Six carry a caution that is not obvious from the issue text.

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
322. Landing it on `dev` reaches operators sooner, and the issue closes whenever the fifth site
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

The rest are independent and small: **#557** (two `add_done_callback` lines, `api/scan.py` is not
among the 322), **#658** (fold `slot` the way its two siblings are folded). **#553** and **#554**
are features and take a design pass each.

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
