# Issue landing plan for the simplification branch

Which of the 44 open issues belong on `audit/simplification-plan` before PR #552 lands, and
which wait for `dev` afterwards. Measured twice. The first 32 rows on 2026-08-10 against
`audit/simplification-plan` at `9fda787`, the twelve issues filed since on 2026-08-11 against
the branch at `b03b0b2`. `origin/dev` was `4bd2c02` both times. The older rows were not
re-measured wholesale. What the second pass did revisit, and corrected in place: #682's lane, now
that #692 has landed; #691's, #557's and #550's evidence; the `test_repo_hygiene.py` example below;
and every `after` row on both passes against the branch-only-gate test, which the first pass did
not apply. That last check moved three of the twelve and no older row, though #657 and #623 each
gained the stated reason they were missing.

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
between lanes on the first review and seven more on the second, in both directions.
`src/reaper/engine/gates.py` shows 46/45 and
`describe_bar` is byte-identical on both trees. A file the branch touched is not a collision. A
file whose fix site the branch replaced, moved or deleted is. `git diff origin/dev...HEAD --
<path>` and read the hunk headers against the line the fix needs.

`tests/test_repo_hygiene.py` was this paragraph's second example and no longer is. It has grown
3,603 lines over 39 hunks, and 2,329 of them are appended past the end of `dev`'s file, which is
the anchor a `dev`-side test would append at too. The clean-append reading was true at `9fda787`
and is not true now.

**A change to what the engine decides waits for `dev`.** A change to how a decision is worded may
ride the branch, since the branch already re-captures Tier B per PR. That is why #657 waits and
#682 did not: one changes what a stored rule matches, the other changed a sentence.

**Everything else lands after, and *after* is the default for one reason: most fixes survive the
merge.** It is not a budget. How near the branch is to closing, how many rows this document has
already put on it, and how open an issue's own question still is are none of them inputs. A
`Status/Need More Info` issue whose fix site the branch deleted is on the branch, because whoever
eventually writes that fix pays a hand-resolved conflict for writing it on `dev`. The lane answers
one question and only one: which tree does this fix get written against.

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
| 729 | Medium | on branch | `_decrypted_or_absent` replaced the fix site in both functions |
| 704 | Medium | on branch | `App.test.tsx`'s mock literal is gone; 6 of the 20 are branch-only |
| 734 | Low | on branch | the `except PlexError` arm is a three-class tuple on the branch |
| 748 | Low | on branch | the block is `_write_desktop_values` on the branch, not inline |
| 710 | Low | on branch | the delete the fix removes is in `start_link`, which the branch deleted |
| 726 | Low | on branch | `PolicyRuleEditors.test.tsx:463` asserts the old string, branch-only |
| 736 | Low | on branch | `styles-chip-dismiss.test.ts:131` pins `.bar-x`, branch-only |
| 740 | Low | on branch | the shared hover the fix folds into is branch-only |
| 657 | Medium | after | no `dev` spelling survives the merge; see below |
| 549 | Low | on branch | PR #560's 45 lines sit inside a 1,601-line branch deletion |
| 566 | High | after | builds on #565, which has not landed yet |
| 550 | Medium | after | `services/planner.py` is not among the 368 |
| 623 | Medium | after | `describe_bar` is byte-identical; its gate appends, see below |
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
| 709 | High | after | eleven sites, all untouched context, and no branch gate reads them |
| 700 | Medium | after | `logbuffer.py` and the Unraid template are untouched on the branch |
| 718 | Low | after | the repair that does not move `_EXPECTED_STANDING`; see below |
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

Sixteen issues. Each is a sub-PR cut from `audit/simplification-plan` and squash-merged into it,
the same way every phase item arrives. Every row here was checked at its fix site.

**A row is here when a `dev`-side fix would not survive the merge as written.** Three ways that
happens, and nothing else is an input: the branch replaced, moved or deleted the source line the
fix edits; the branch carries a test or rule absent on `dev` that the `dev` fix would fail the day
#552 lands; or the `dev` fix moves a constant the branch already moved, so the merge keeps one
value and the tree needs another. How open the issue's question is, what its `Priority/` says, and
how many items this list already holds are all beside the point.

**1. #691, using #692's rig. Landed, confirmed.** It was `Status/Need More Info`, and the fix site
is `_send_for_real` at `executor.py:1778`, roughly 600 lines from #692's `_send_season`. The reason
it belonged here is the rig, not the diff: #692 landed on 2026-08-10 and stands up an armed
executor against a stub Sonarr, which is what settles this.

**The step that settled it was cheaper than the plan expected**: `TestAnOverrideChangedMidRun`
already drives the whole shape, and one of its tests was already reaching the arm and never read
the message. `test_a_reap_added_mid_run_cannot_smuggle_an_item_in` re-adds a reap mid-run and was
told the reap had been removed. So the arm is reachable twice over, and the second population is
the one the issue described: an item spared before the claim, un-spared mid-run, which was never
hand-reaped at all. Both now read the sentence. The guard is two `if`s, each with its own copy;
the run-start-set arm says `this was not part of the run you confirmed, so it is kept`.

**2. #624, the shelf status line. Landed.** The strongest collision of the sixteen. The branch's hunk
`@@ -505,11 +489,36` replaces the whole `lsStatus` closure, five lines becoming thirty, and
`LeavingSoonRow` moved into the new `JobsPanel.tsx`. The issue's "Where" section names lines that
no longer exist. The defect survived the rewrite: the new closure never read
`leavingSoon.data.enabled` either.

**The line is gated, not reworded.** The Jobs row draws "Off." because its switch is on another
screen; on this panel the switch sits two rows above the line, so saying it again would restate a
control already on screen. `lsStatus` returns `null` while the shelf is off.

**3. #584, the Plex library sync success path.** `_sync_libraries` moved from
`api/settings.py:1201` to `api/plex.py:599`, and `tests/test_settings_api.py` changed inside
`TestPlexLinkChoice` itself. A test written on `dev` names a file that no longer holds the
function. Stub `PlexClient.video_sections` to return sections and assert the merge: a disabled
library stays disabled, a new one arrives enabled, a vanished one drops out.

**4. #598, the mutation zone. Landed.** The branch's hunk `@@ -1168,8 +1153,6` removes two entries
from the same `engine-gates` `functions=` tuple this fix appends to, and two other zones re-pointed
`module=` at `policy_migrations.py` and `policy_warnings.py`, which the drift check has to read.

**The check is an exact set, so every zone had to be settled, not just `engine-gates`.** Four of the
five had drift; `ratings` was already exact. A callable now sits in a zone's `functions=` or in an
`Omission` carrying a written reason, and being in neither is the failure. `engine-gates` declares
all eight the issue names and omits the two `Gate` protocol stubs. `policy-inspect` gained
`_protect_blocks_on_reach`, which decides one of `inspect`'s warnings. The guard is
`test_repo_hygiene.py`, because the script is not in CI, and `main()` runs the same function before
it copies anything.

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

**8 through 11 are backend one-liners, and their order among themselves does not matter.** Each is
here for the first reason above: the branch replaced the line the fix edits, so a `dev`-side fix is
a modify/delete or a hand merge. None touches another's file.

**8. #729, the empty webhook.** Hunks `@@ -561,10 +583,7` and `@@ -603,11 +622,7` deleted the
`try` / `except ValueError` block from both `get_discord_webhook` and `has_discord_webhook`, which
is the fix site twice over. The branch's replacement is `_decrypted_or_absent` at
`app_settings.py:185`, and it answers a decrypt failure rather than an empty string, so the issue's
shape is still here. Put the clause at the two call sites, `:586` and `:625`, not in the helper:
`get_api_key` reads the same helper at `:410` and the issue measured nothing about API keys.
`tests/test_app_settings_precedence.py:143` is the neighboring test and is also branch-only.

**9. #734, the unreachable Plex server.** `dev`'s `except PlexError as exc:` is now the three-class
tuple at `api/leaving_soon.py:40`, and the comment three lines above it names #734 and says the
client's `PlexError` "lands here as it stands". The branch also added the two service errors the
fix needs to sort against (`LeavingSoonDisabledError`, `LeavingSoonDegradedError`). Give the
not-linked case at `services/leaving_soon.py:519` its own error, then answer a client `PlexError`
with 502 the way the three sibling routes do.

**10. #748, the launcher write.** `dev` has `write_conf_values`, `os.environ.update` and the commit
inline at `api/settings.py:1993-2003`. The branch moved the first two into `_write_desktop_values`
at `:1351`, called at `:1399`, one line before the commit at `:1400`. A `dev`-side reorder of a
block that no longer exists is a modify/delete. Moving the call to after the commit is one line
here.

**11. #710, the PIN sweeper.** Half the fix merges and half cannot. `sweep_expired_sessions` is
untouched context (`scheduler.py:495` on `dev`, `:491` here), so folding the delete in lands either
way. The half that does not is the opportunistic delete the fix retires: `dev` carries it in
`start_link` at `plex_link.py:413` and in `login.py`'s `start_plex_login`, and the branch replaced
both with one `start_pin` at `plex_link.py:150`. Two modify/deletes on `dev`, none here.

**12. #726, the rating unit's space.** The source line merges. `PolicyRuleEditors.test.tsx:463`
does not: it asserts `reads: "7.5 /10"`, it is the expectation the fix has to move, and the file
does not exist on `dev`. A `dev` fix is green until #552 lands and red after. Land the fix and its
expectation in one change, which is only possible here.

**13. #736 and 14. #740 are one sweep, and land together.** Both are chip dismiss controls and both
turn on declarations that exist on one tree only. #736's `.bar-x` padding is pinned at
`styles-chip-dismiss.test.ts:131`, a branch-only file, so a `dev`-side `padding: 0` turns that pin
red at merge even though `14-policy-editor.css` is byte-identical. #740's `.fchip-x:hover` is
byte-identical too, but its second repair folds the tint into the shared chip-dismiss rule at
`04-buttons.css:88`, which the branch created and `dev` does not have. Rule 72: they are siblings
of one control, so decide both against the same cascade rather than one at a time.

**15. #704, the suite's undefined-read gate.** Same shape as #622, one lane over. `setup.ts` is
byte-identical on both trees, so the three-line gate merges, and it would then be measuring a tree
that has six more failures than the one it was written against: `SetupPlexStep.test.tsx` carries 6
of the 20 and exists only here. The `App.test.tsx` half is a collision outright, the branch having
replaced `dev`'s hand-listed `apiMock` literal with `makeApiMock()` from `test/apiMock.ts`, which
is where the missing `vocabularyValues` answer now goes. Second to last because the count is
measured rather than reasoned and the tree it measures has to stop moving first: #726, #736 and
#740 are all frontend items above it. It still reads 20 here (`npx vitest run
--disableConsoleIntercept`, 2026-08-11, exit 0). Fix both files first, then add the gate, or it
lands red.

**16. #549, PR #560 ported.** The work is written and reviewed; only its target is wrong. #560 adds
45 lines at `engine/policy.py:1395`, inside the branch's `@@ -996,1601 +994,26` deletion, and
`own_list_media_scope` now lives at `engine/policy_migrations.py:369`. Landing it on `dev` leaves a
modify/delete conflict to resolve by hand into a file `dev` has never had, which is the one shape
this file exists to prevent. Port the diff onto a branch cut from `audit/simplification-plan` and
close #560 as superseded, naming the port. That is a relocation, not a discard: no reviewed line is
thrown away. Last because it is the only row whose fix already exists, so nothing else waits on it.

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

Nineteen issues. All but five carry a caution that is not obvious from the issue text.

**A row is here because a `dev` fix survives the merge, or because no `dev` fix can be written at
all.** Those are different reasons and the row says which. Everything that does collide is on the
list above, whatever its priority and whatever is still open about it.

**#657 has no `dev` spelling that survives, so nothing can be written there before #552.** The
issue says it must not ride along, and the branch's verification rests on every decision being
byte-identical, which is still the reason it is not on the list above. The measurement underneath
that: the branch rewrote both sibling arms of the same `match`, `Op.EQ` and `Op.IN` now fold
through `fold(...)`, and the defective `Op.CONTAINS` line is two lines of trailing context away. So
a `dev` author copies the sibling idiom, `.strip().casefold()`, and
`test_repo_hygiene.py:538` bans that literal anywhere under `src/`. The alternative spelling is not
available either: `src/reaper/text.py` does not exist on `dev` (`git show origin/dev:` exits 128).
Written as `fold(...)` after the merge it is a one-line change.

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
**That test is the part to watch.** It appends to `tests/test_repo_hygiene.py`, and so does the
branch, 2,329 lines at the same anchor. The fix itself merges; a gate appended beside it on `dev`
before #552 does not.

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

**#709 is the new `Priority/High`, and it is here on a measurement, not on its priority.** All
eleven sites the issue names are untouched context on the branch: `snapshot.py:438`,
`lists.py:278`, `season_scan.py:1230`, `seerr.py:217` and `identity.py:207`, `dev` lines. No
branch-only gate reads them either. The one that mentions the shape,
`test_repo_hygiene.py`'s `_display_pack_sites`, walks `Display(...)` calls for `ast.Name` bases and
still finds `item` whichever way the id is cleaned; the issue's preferred repair is at the write
sites, which leaves that call untouched. So a `dev` fix survives the merge, and the engine rule
decides the rest: cleaning the id changes what a protection-list lookup matches. One branch item
rides with it. `docs/SIMPLIFICATION_PLAN.md:193` records W11-2 as deferred rather than killed,
because it is the carrier for this issue's raw `imdbId` writes and nothing else wants it.

**#718 has two repairs and only one of them is safe here.** Both fix sites are untouched context.
But the branch has already moved `_EXPECTED_STANDING` from 35 to 36, so the "pass `standing`"
repair moves the same constant to the same value on `dev`, the merge keeps one 36, and the true
count on the merged tree is 37. That repair belongs on the branch if it is the one wanted. The
other, narrowing the comment at `PolicyEditor.tsx:1833` on `dev` to name what makes the savebar
half a reaction, moves no constant and is why this row is not on the list above.

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
