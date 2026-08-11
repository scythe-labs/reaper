# Simplification plan

A whole-tree audit for accidental complexity, run 2026-08-07 against `6f36a0c`. Thirteen
read-only passes covered every directory: the deletion path, the scan pipeline, the season
subsystem, the engine, identity and clients, the API layer, settings and credentials, the
remaining services and persistence, three frontend lanes, the test suite, and the seams between
them.

A **second pass** follows it, run against `4b73f14`. Twelve more lanes, sliced by axis rather
than by directory, hunting only what the first pass structurally could not see. It starts at
[Second pass](#second-pass).

A **third pass attacked both**, run against `4b73f14`. Ten adversarial lanes re-derived every
falsifiable claim from the code instead of from the plan. It killed four findings that named live
code as dead, reclassified five, and caught one proposal that would have uncoupled the typed
confirmation phrase from the set the executor deletes. Its corrections are folded into the
findings in place. [Execution](#execution) is the plan for landing what survived.

A **fourth pass attacked the execution plan** rather than the findings, run against `17bec21`. It
rewrote phase 1, which specified a re-scan against live data and a salted fixture: the machinery
already exists, one of its five capture items hashed the wrong thing and would have failed green,
and a re-scan moves 45% of its own lines in 30 days from the calendar alone. It rejected a proposal
to land phases 1 to 5 on `dev` and replaced it with a per-item escape hatch. It found the
"which phases may move the baseline" list written a third time, wrong, in the verification
protocol. Its changes are in place throughout; nothing below it was reopened.

**This document is the living state of the work, not a proposal about it.** It is edited as each
piece lands, in the same commit that lands it. [Progress](#progress) is the current status and is
the first thing to read; [Execution](#execution) is how to run the next phase. An agent that
finishes a piece and does not update Progress has left the next session guessing.

**All of this lands on one branch, `audit/simplification-plan`, and reaches `dev` once.** See
[Branch and pull request workflow](#branch-and-pull-request-workflow).

## How to read this

Every finding carries a file and a line, a size estimate, a risk class and the test that pins the
behavior today. Findings are cited by **ID**, positional within the wave's own list: `W1.1-a` is
the first row of 1.1's table, `W7-3` the third bullet of wave 7, `W8-2` the second of wave 8. A
letter means a table row, a number a bullet.

**Four waves hold more than one list, and take a sub-letter**: `W3a-*` is wave 3's *Already
drifted*, `W3b-*` its *Largest by volume*, `W3c` the parameter-object paragraph. `W12a-*` is wave
12's bullets. Wave 11's four prose lanes carry no IDs at all — cite those by lane name and symbol
(`W11 control-flow, _kept_phrase`), and name the symbol in the PR. Wave 1.5 is one paragraph, read
as four items in sentence order.

**A pull request names the IDs it closes, and the symbol.** Line numbers in this document go stale
at **phase 4**, not at phase 6: W10 item 2 rewrites five mappings in `api/settings.py`, and phase 5
deletes from `routes.py`, `season_scan.py` and `snapshot.py`. Symbol names do not go stale, and a
symbol alone is not always an anchor — eight citations sit hundreds of lines inside `scan`,
`simulate`, `list_candidates` and `ReviewQueue`, and two pairs share one symbol. **The durable form
is the symbol plus a quoted fragment of the line.**

**Phase numbers are frozen.** They were assigned once, in execution order, and a phase that turns
out to belong elsewhere moves by editing the *Why here* column, never by renumbering. Thirty-seven
lines cite a phase by number; a renumber that misses one sends a session to the wrong work.

Risk classes: `none` (pure motion or deletion of unreachable code), `behavior` (observable output
could move), `safety-path` (touches or sits beside a deletion interlock), `migration`, `a11y`,
`visual`, `ci`.

**A `> Corrected:` line under a finding is the third pass overruling the first two.** It is the
current state of that finding. Where it says a claim is false, the work does not happen as
written.

## What the third pass changed

Killed outright, because the code is live: **W1.1-k** (`history_sync.state`), the
`CustomProtectGate` half of **W1.1-a**, **W1.1-i**'s `poster_url`, and **W7-5**'s `window_days`.
**Two of those four kills have since been reversed by a later decision, and both reversals are
recorded on the finding, not here**: C1 sent W1.1-i's poster chain to phase 5 whole, and #597
removed the `PolicyProbeOut.detail` that made `window_days` live, so the condition this line
rests on no longer holds for it. Read the finding's `> Corrected:` block before trusting a kill
in this paragraph.

Reclassified upward: **W5-1** and the parameter-object paragraph to `safety-path`, **W5-5** off
`none`, **W1.1-n** to an external contract change, **W8-2** to the plan's one genuinely dangerous
item.

Rewritten because the proposed shape weakens a protection: **W6-3** (one `paged()` cannot serve
five different failure contracts), **W6-2** (one chunk constant would put 2,000 bound variables in
a statement), **W3**'s `plex.py` helper (it would swallow the transport guard's refusal), and
**W3**'s pragma unification (`journal_mode=WAL` writes the file it is reading).

Several counts moved. `engine/policy.py` is 2,709 lines, not 2,263, and every Python line count in
this document is short by 0.2% to 16%. They size a session and settle nothing else (S5).

## Branch and pull request workflow

**Every change from this plan lands on `audit/simplification-plan`. Nothing goes to `dev` until
the last phase closes.** The tree moves too far for a partial landing to be reviewable, and
several phases only make sense against the shape an earlier phase left behind.

That inverts three habits CLAUDE.md sets for normal work, so an agent following its defaults will
get all three wrong:

1. **Cut the working branch from `audit/simplification-plan`, not from `origin/dev`.** Name it
   `simp/<phase>-<short-symbol>` (`simp/2-hygiene-lru-cache`). Open its pull request with
   `--base audit/simplification-plan`. A PR based on `dev` carries the whole audit in its diff.
2. **`dev` is merged *into* this branch, never rebased onto it.** Working branches are cut from it
   and PRs merge into it, so its history is shared and a rebase rewrites commits other branches are
   built on. Sync weekly, and always before starting a phase:
   `git fetch origin && git merge origin/dev`. Resolve toward `dev` for any file this plan has not
   touched yet.
3. **Sub-PRs squash into this branch the same way anything squashes into `dev`.** One finding, one
   PR, one squash commit here. That commit's subject is the sub-PR title, so it still parses as a
   Conventional Commit and still names its finding IDs.

**One escape hatch, and it is per item rather than per phase.** A change that touches only `tests/`
or a doc sentence, needs no baseline, and cannot collide with a file phases 6 to 9 move is worth
more landed on `dev` than parked here: it reaches every other session at once and its conflict
surface is nil. That is W1.3's `lru_cache`, W12's scrypt wrapper (with its injectivity guard), the
`probe_root_folders` stub, `test_openapi_tags`'s fixture scope, the twelve `@vitest-environment
node` docblocks, and W6-6's two-sentence rule 7/24 correction. **W1.4's scaffolding stays here** —
it is a real refactor and phase 8 depends on it.

**"Its conflict surface is nil" was measured once and is not nil.** W6-6 grew a gate on the way
out, and that gate landed at the same anchor in `test_repo_hygiene.py` as one this branch already
had, so the return merge conflicted. Both blocks were kept and the file went to 74 tests, which
is the resolution working — but note the second half, because it is the one nobody sees coming:
**the sub-PR that carries the merge back is itself squashed, which flattens the merge out of this
branch's history.** The tree was correct and `git merge-base --is-ancestor origin/dev HEAD` was
false, so the next weekly merge would have replayed the same commit into a file that already held
it.

**So push the `dev` merge to this branch directly, never inside a sub-PR.** Done that way it is
an ordinary merge commit and `git merge-base --is-ancestor origin/dev HEAD` is true afterwards,
so the next merge starts from the right base. Both round trips were measured: the first went
through the phase-close PR and needed repairing, the second was pushed directly and did not. If
one has already been flattened, the repair is `git merge -s ours origin/dev`, also pushed
directly — it records the ancestry and touches no file. Send an item to `dev` when what it
corrects is read by every session, as CLAUDE.md is, and keep it here when it is not.

**Splitting whole phases off is the version that does not work**, and it was considered and
rejected rather than never raised. Every sub-PR edits this document under S10 and `docs/STATUS.md`
under S6; both append at a fixed position, which is the one construct git cannot merge.
`docs/SIMPLIFICATION_PLAN.md` does not exist on `dev` at all, so the weekly merge's conflict
surface is **zero** today and would not stay that way — and a *Landed* row dropped while resolving
a conflict is lost silently, in the table this plan calls the history the squash merge will
destroy. The phase-3 gates are the case that looks strongest and is not: the socket guard found
exactly one violation across the repository's whole history, so its expected yield on `dev` during
the audit is about zero, while its hand-reconciled counters (S7) would conflict on every merge.

**PR #552 is the integration pull request.** It stays open as a **draft** for the duration, base
`dev`, head `audit/simplification-plan`, carrying `Kind/Chore` and `Priority/Medium`. It is
what CI runs against on every push and what makes the accumulated diff visible. Draft is what
stops it merging early. Its body does **not** duplicate the log below: it links to
[Progress](#progress), because one fact written twice is the failure rule 144 describes.

**The landing is one squash commit on `dev`, and that is a real cost.** The repository allows no
other style, so months of work arrive as a single commit and `git bisect` cannot see inside it.
**Tag the branch tip before merging** — `git tag audit/pre-squash && git push origin
audit/pre-squash` — because the head branch auto-deletes at merge and the tag is then the only
thing keeping the sub-PR commits reachable. That restores `git bisect` inside the squash for one
command, and it is free: no workflow triggers on a tag, `release.yml` being `push: branches:
[main]` plus `workflow_dispatch`. The [Landed](#landed) table is the index into those commits: each
row names the finding, the symbol and the sub-PR.

**Retitle and rewrite #552 before marking it ready.** Its current title and body describe an
audit that touched no code. They become the permanent commit message for a change that touches
most of the tree.

**The last PR retires this document.** It moves to `docs/history/` under the FROZEN banner
`test_archived_docs_declare_they_are_frozen` checks, and the same commit corrects `docs/README.md:117` and
CLAUDE.md's live-plans paragraph. Both assert this plan is live, no gate covers either, and
`test_docs_referenced_from_code_exist` scans code and `pyproject.toml` rather than those two files
— so a move without them leaves two confident false statements and a dangling path behind a green
suite. Where the file goes needs no instruction; `docs/README.md` already routes a finished
remediation.

**Run the full gate set on the branch tip after every fifth merge, and at the end of every phase.**
A squash merge lands a sha CI never tested, because the change is replayed onto whatever the branch
is now. Over sixty sub-PRs that is sixty untested commits on the branch everything else is cut
from, and a per-PR green tells you nothing about the tip. The end-of-phase run is the one that
matters: it is the last point where the phase that broke something is still obvious.

## Progress

**Edit this section in the same commit that changes its truth.** A phase that finishes without its
row moving is indistinguishable from one that never started.

| # | Phase | Status | Landed | Notes |
| --- | --- | --- | --- | --- |
| 0 | Correct the plan | **done** | — | Third pass folded in. C1 settled |
| 1 | Behavioral baseline | **done** | 2 of 2 | C13 settled on redaction; its coverage half is a standing limit, not a blocker |
| 2 | Test-suite wall clock | **done** | 9 of 9 | C2 settled: the cheap KDF stays. The gate went 83.44s to 38.74s |
| 3 | Gates that land green | **done** | 4 of 4 | C3's counts all held under an adversarial re-derivation; three of the four gates had a hole beside the count, each fixed and driven |
| 4 | Drift corrections | **done** | 4 of 4 | Every item proved latent or off the decision surface, so the re-freeze moved nothing: Tier B re-captured byte-identical. C12 settled, boot log keeps the added lines |
| 5 | Deletions | **done** | 4 of 4 | W1.1-l killed: `tautulli.metadata` has a caller in `scripts/`. Release M's review found a keep collection silently unprotected since the first Pace save. Tier B moved by one line, the recorded alembic head; every decision identical. C5 settled retrospectively on 2026-08-10, both drives passing against a real M-1 backup; C4 stays owed |
| 6 | Structural motion | **done** | 6 of 8, 2 dropped | The by-design ceiling. Exit task finished: every `path:NNN` in this document resolves against the tree. **The two dropped are re-asked after phase 8 closes** (owner, 2026-08-10), under the same question phase 8's kills are being re-asked under: not whether the proposed shape works, but what shape would |
| 7 | Wire contract | **done** | 8 of 8 | C7, C11 and C8 all settled. Eight items, not ~5: W8-3 measures 20 unread fields and W8-4's anonymous payloads are 11 routes over 4 shapes. W8-1 took two PRs, the route's shape then the rollup |
| 8 | Dedup and carriers | **in progress** | 38 findings landed, 14 killed, 1 open, W11 27 unstarted | **The count is findings, and it was PRs until a completeness audit measured it.** 34 sub-PRs closed 38 findings: five closed none (#659 pins what #676 then built, #678 corrects prose, #686 follows up an already-killed row, #683 and #687 land the surviving halves of rows they kill), two closed one each and were never counted (#616, #618, rows written at the audit), and four closed two each (#653, #681, #698, which kills W3b-2 and lands W11-32, and #705, which builds W3b-12 and W9-4). #699 kills W9-5 and W9-6 together, #706 kills W5-1 while landing the pinning test that kill argues for, #703 kills W3b-9 the same way, and #719 kills all four of W3b-11's sub-items while landing the three drifts the extraction would have removed. **The killed figure can be checked against a table and the landed one cannot.** *Killed while executing* carries one row per finding, 17 of them. **Three are not kills, and each says so in its own first line**: phase 5's W1.1-l, W3b-9's kill that #725 reversed by measuring a different helper shape, and W11-10's job-block half, whose four getters built, so the plan counts that finding in *Landed*. The phase's killed figure is fourteen. *Landed* is keyed by sub-PR and no row records which phase it served, so the sub-PR figure here is carried forward by hand and the exception list above does not reconcile against it. A findings-per-phase column in that table is what would make it re-derivable. **Open, by ID**: W6-1 the CSS control standard. One name, one open. **No open item carries `safety-path` any more**: W5-1 and W5-3 were the last two, and each was driven under C9 before it closed. **The list used to carry W3b-4 while the count beside it already excluded that item**, #696 having moved it to phase 9, so the two disagreed by one from the day that PR landed. **W11 is in this phase's scope and has not been started**: it is **44 items over seven lanes**, enumerated `W11-1` to `W11-44` on 2026-08-10 because it had no IDs and the phase could not name what was left. **40 remain**: W11-32 closed at #698, out of the same function W3b-2 was measured in, then W11-15 and W11-12 built and W11-39 killed at #717, then W11-3, W11-22 and W11-24 all built at #723. The original `~25` denominator never contained it. Two of the 44 are defects rather than duplications, W11-15 and W11-40. **What subset lets the wave read `done` is an open scoping call for the owner.** **C14 is settled.** W9 took three PRs, all landed: 9 cycles to 2, and neither counter S7 named moved. W3's `api/deps.py` took two, both landed (#670, #681). **W3b-4 is no longer counted here: it folds into phase 9's W4.1** (owner, 2026-08-10). 22 of its 40 `.set-row` sites are in `GeneralPanel.tsx`, which W4.1 rewrites, and `<SetRow>` and `FIELDS` describe the same rows, so whichever landed second would be written against the other. One PR does both. `:495` stated the collision and never ordered it; there is no order to settle now |
| 9 | Declaration tax | not started | 0 of 2 | C10 outstanding. W4.1 absorbed phase 8's W3b-4, so its one PR closes both |
| 10 | Issues that land here | not started | 1 of 8 | #682 landed at #692. `ISSUE_LANDING_PLAN.md` holds the per-issue reasoning and dies when #552 merges |

Status vocabulary, and nothing else: `not started`, `in progress`, `blocked`, `done`. `blocked`
carries the reason in *Notes* and the checkpoint or decision that unblocks it. **`done` means every
finding landed or killed *and* the phase's exit task finished**, so phase 6 is not `done` until its
re-anchoring is; an outstanding checkpoint that gates nothing downstream goes in *Notes* rather
than holding the row open.

**A killed finding lowers the denominator.** Write the *Landed* cell as `4 landed, 1 killed` once
either is non-zero, because a phase that kills three findings would otherwise read `22 of 25`
forever while being genuinely complete. Phase 6 tops out at 6 of 8 by design: its last two rows are
"land them as their own work or drop them."

### Checkpoints

| Checkpoint | Status | Settled by |
| --- | --- | --- |
| C1 | **settled** | Owner, 2026-08-07. Six decisions, each recorded on the finding it governs: delete the whitelist routes (not shipped, so S4 has no reader to protect yet); delete the poster chain whole; move the flat-AND reasoning to `DECISIONS.md`; delete `run_migrations_offline`; keep `backtest_passed` and correct its docstring; and schema now leaves under rule 148 rather than accumulating |
| C13 | **part settled** | Owner, 2026-08-07, on redaction: the capture ships as cut. The coverage half stands open — both tiers hold `Facts` fixed, so a carrier that widens what is prunable on a real library moves nothing in either. S3's driven pass is the only thing that reaches it |
| C2 | **settled** | Owner, 2026-08-08. The cheap KDF stays: a test that needs an encryption key to exist should not pay 124ms to prove nothing. Both proofs accepted, the collapse demonstration being the load-bearing one. **The standing cost is recorded rather than fixed**: no test now exercises the real 64 MiB derivation, so its own correctness rests on `crypto.py` being unchanged, not on being run |
| C3 | **settled** | Owner, 2026-08-10: **closed on the audit, with nothing added.** The read that had stood open through five phases asked for one thing the audit had not supplied, which is whether the figures still describe the tree, so all four were re-derived at the tip before closing: `_EXPECTED_LAYERED_MODULES` 84, `_EXPECTED_SOURCE_MODULES` 116, `_DEFERRED_CROSS_PACKAGE_IMPORTS` empty, the path-filter gate present as `test_the_workflows_that_filter_themselves_by_path_are_the_ones_the_prose_names` over 9 workflows and 3 filters, and the socket guard hooking `getaddrinfo` plus its resolver siblings rather than `connect`. The three matcher holes below were the checkpoint's whole yield and all three are fixed and driven. The evidence the read rested on follows. Three independent auditors re-derived every count without being shown it first, and **every one held exactly** — including the socket guard's 7 violations, re-measured in a detached worktree. **What did not hold is the matcher beside each count**, which is the half rule 145 says a count cannot cover: the layering walk could not see `from reaper import services` (#588), the path-filter gate pinned a number where the prose names files (#591), and the network guard hooked 3 of 10 exits (#592). All three fixed and driven. The lesson for later phases: re-deriving a count is cheap and confirmed it; what the count could not answer is whether the *walk* sees the tree, and that needed a second party. Below is what each gate now covers. **W6-5**: 78 modules under the four packages *as audited*, 6 directed pairs, 3 deferred imports (the deferred figure is 0 now, W9 having promoted two, then the third once a PR was already paying the driven pass its kill had refused; the module figure is 84: #599 deleted two, phase 6 added `api/plex.py`, `engine/policy_migrations.py`, `engine/policy_warnings.py` and `routes.py`'s five, and phase 8 added `api/deps.py`); the reconciliation is that all 14 `engine/` modules and 8 of 9 `clients/` produce no cross-package edge at all. **W6-8**: 2,023 socketpairs across 1,674 tests allowed, 9 `getaddrinfo` calls seen, 7 of them real violations; the allowlist is 7 hosts and every one is driven. **W6-6**: 3 path filters across 9 workflows, counted twice by two different matchers. W1.5-c landed no gate, so nothing there to check |
| C12 | **settled** | Owner, 2026-08-08: **the boot log keeps the added lines.** The one cost put to them was ~2 lines per restart saying a job runs on its built-in default, and they took it — a boot that states every job's schedule out loud is worth more than a quiet one, which is the same argument `main.py`'s per-job "next firing" table already rests on. Nothing to change; #594 ships as merged. The evidence behind the read follows. Two questions, both measured rather than argued. **What startup now applies: the same job table, byte for byte.** The same stored config booted on the phase-3 tip and on the phase-4 tip gives six jobs with identical ids and triggers; the boot log differs by exactly three lines, and the `scheduler.*` event diff is the whole behavioral surface of #594 — an orphaned stored row's warning renamed from `bad_maintenance_cron` to `unknown_maintenance_job` and moved earlier, plus one `maintenance_scheduled` line each for the two jobs still on their built-in defaults, which the replay now re-applies from the same constant `build_scheduler` used. The interval sweeps are outside `MAINTENANCE_JOB_IDS`, so `sweep_old_snapshots` keeps its start delay and jitter. **The deliberate re-freeze is a no-op, and that is the finding.** Tier A: 114 replay tests pass and `tests/_policy_lab.py` is untouched. Tier B: re-captured against snapshot 86 and **byte-for-byte identical** to the committed file — 5,965 items, protect 4,261 / condemn 543 / abstain 1,161, same plan and manifest hash. The phase text expected corrected behavior to move the baseline; the corrections are why it did not, since all four items proved latent or off the decision surface entirely |
| C6 | **settled** | Owner, 2026-08-08: **five modules, and *Vocabulary* gets its own.** `api/review.py` (1,315, the *Snapshots and candidates* banner), `api/policy.py` (~485), `api/simulate.py` (~841), `api/vocabulary.py` (85), `api/about.py` (47), off a ~150-line shared preamble. So the *Policy* banner is cut at `:1853` (`_SIM_YIELD_EVERY`), a seam the file does not draw, and the 85-line *Vocabulary* banner stands alone rather than riding under the POLICY tag it shares with the editor. **Two cross-module edges are created and were measured before the read, not after**: `_to_body` is called by both the policy routes and `simulate`, so `simulate.py` imports `policy.py`; and `_replayed_evidence` is defined inside the simulate block yet called from `_deep_links` (`api/review.py`, "the replay can never disagree"), which is *review*, so `review.py` imports `simulate.py`. Neither is a design problem at two import lines, but the wave's "pure motion" framing does not predict them, and phase 8 plans against this graph. One route is filed under a banner its tag disagrees with: `season_shape` (`:208`) is POLICY-tagged and sits in *Snapshots and candidates*. It moves by banner, to `review.py`, because `test_openapi_tags.py` keys on method and path and the served tag is unchanged either way |
| C6, corrected on landing | **the edge count was low, and the two it found were a cycle** | Owner, 2026-08-09. C6 measured two cross-module edges and got the direction of both right; there are **four**, and the two it missed are what make its own pair a loop. `simulate` reads `_decode_explanation` and `_entries` from *review*, while review reads `_replayed_evidence` from *simulate* — `from x import y` both ways does not load, so the five-module cut as settled would not have booted. Settled by moving `_replayed_evidence` into `review.py`, which inverts the one edge C6 reasoned about explicitly. It is not only a cycle break: all three are readers of a stored explanation or its evidence, its two siblings were already in review, and the panel is their first reader. The graph is now `simulate → review`, `simulate → policy`, `policy → review`, `vocabulary → review`, and acyclic. **The lesson is C3's again at a different target**: re-deriving the count was not what a second party added — measuring *two* of something is no evidence about how many there are, and a partial edge measurement cannot see a cycle by construction, because a cycle is a property of the set |
| C7 | **settled** | Owner, 2026-08-09: **cap the window at 50, and add paging so the whole plan stays reachable.** The cap sits on the `steps=[...]` comprehension inside `_run_out` and nowhere else, since `_planned_candidates` runs off the same local one line earlier and `execute_run` re-derives the phrase through the same helper. The read corrected the proposal's own framing: the operator cannot scroll a long plan today either, because `ReapPlan.tsx:52` already slices to `LIST_CAP = 50` with no expand control, so the payload carries 1,500 rows to draw 50 and the cap removes nothing anyone can see. Paging is therefore new capability rather than preserved capability, and it lands as its own route (`GET /api/runs/{id}/steps`) rather than query parameters on the detail route: `_run_out` recomputes the effective condemned set and the phrase on every call, and `ReapConfirm.tsx:66` holds `["run", runId]` with `staleTime: Infinity`, so an offset in that cache key would move the confirm sheet's own run object. **The measurement that decides the test**: no test in either tree builds a plan larger than 5 items, so a `LIMIT 50` in the wrong helper passes the whole suite green |
| C11 | **settled** | Owner, 2026-08-09: **drop the eighteen, wire the one.** The field list put to them was measured rather than quoted: the plan says eleven and its correction says 14, and both are wrong. The correction is also inconsistent with itself, since the enumeration it defends sums to 14 with `RunOut` at three, so adding `approved_at` as a fourth the list misses makes 15. Measured against the SPA the set is **20 across 8 models**, the extras being `LeavingSoonOut.applied` and `RunSummaryOut`'s `snapshot_id` and `held_back_unknown_size`, whose look-alike reads are all on `RunOut`. The read also narrowed the S4 argument: an API key reaches only 12 of the 20, since `UserOut` answers 401 to a key and `LeavingSoonOut` and `RestoreSummaryOut` sit behind writes it cannot make, so the blanket "these are a published contract" holds of the schema document and overstates reachability for three of the eight models. `PlexTrashOut.empties_after_scan` is the one kept and wired (#644): it reports the case where Plex empties its own trash and the executor's interlock never gets a say, which is a fail-closed signal measured on every reap-sheet open and never shown. Its shape was the owner's second call and is smaller than proposed, a sentence on a warning that already fires rather than a warning of its own, because Plex ships that preference on and a notice over an empty trash would stand in front of every reap |
| C8 | **settled** | Driven at #650, after W8-1, on all three of its named surfaces. **The queue**: 92 show cards across many pages, every one drawing its removal line, and every card's strip square count equal to its own denominator, with no `NaN` and no page error. **A season row predating `show_status`**: the field never moved, so this is a check that the move did not disturb it; with the first row of 14 multi-row shows blanked, all 60 "Ended" chips still drew and none read "Status unknown", which is the `find` doing the job `[0]` could not. **The bulk bar**: "200 cards, 238 items" beside "Reap now…", and 238 is exactly the sum of those cards' own whole-snapshot counts, taken from rollups merged across every fetched page. The read also settled the item's headline: **"~90%" is about the show-level fields and reads as a page figure**, and the measured page saving is 7% to 16%, worst on a seasons-only page |
| C14 | **settled** | Owner, 2026-08-09: **option 5, plus a log line at the guard.** **Option 5 is written out here because the numbering pointed at a transcript and the next session could not resolve it**: `_call` covers reads AND writes and carries the `except SafetyViolationError: raise` arm itself, so the eight per-method arms become one declaration and the five structural opt-outs stay bespoke. Re-confirmed by the owner on 2026-08-10 when the reference was found dangling, against a list of the four shapes the surviving evidence admits. Landed at #676. The pinning test for the eight `except SafetyViolationError: raise` arms lands first (#659), then `_call` is built against it. And every blocked write is recorded at the point of refusal, whatever the reason, so a refusal survives a caller that swallows it. That second half is the owner's addition and is the better one: the arms protect eight methods, the guard log covers every write either guard ever refuses, including ones nobody has written yet. It lives in the two guards rather than at 23 call sites, and `refuse_mutation` raises as well as logs so a refusal added later cannot arrive without its line. The evidence the read rested on follows. |
| C9 | **satisfied per PR; the first drive is #676's** | Recurring by design, so it never reads "settled" once. **The mechanics are recorded here because a session concluded the drive was impossible and it is not**: `data/` on the dev box holds a real `reaper.db` and a linked Plex server, and a probe reaches the guard by decrypting `plex_server.token_enc` through `SecretBox` the way `main.lifespan` does, then calling the guarded methods with `RuntimeSafety(destructive_enabled=False)`. A fake token is what fails: `_connect` runs first, takes a 401, and raises its own `PlexError`, which is one of the five opt-outs behaving correctly and says nothing about the interlock. Two things bound the cost. `_connect` issues a GET, so no probe is network-silent. And the probe patches `requests`' adapter to raise on any mutating request, an independent net below the guard, so the run stays write-free even in the failure case it exists to look for. #676's drive is on its Landed row, and #681's is on its own: same shape against a different interlock, a throwaway database under `/tmp` rather than the real one, with the kill switch patching the three functions BELOW the password gate instead of the HTTP adapter |
| C14, the measurement | — | The evidence behind the read, measured against a tree identical to `dev`. **The finding and its correction are both wrong on most counts**: 23 `to_thread` sites, not 24 (the 24th is the module docstring, so the correction's figure is the right one); **five** structural opt-outs plus the write shape, not three or four, the unnamed fifth being `trash_count`'s `except PlexError: raise` at `plex.py:896`, which is the same re-raise-first shape as the eight write arms; **eight** byte-identical `except` arms, not 19; and ~50 net lines, not ~100. **The eight `except SafetyViolationError: raise` arms are unpinned, proven rather than argued**: the full suite with all eight deleted is 4,161 passed / 1 skipped / exit 0, identical to baseline. The correction's reason is wrong and its conclusion right — those files DO construct a refusing safety state, since `RuntimeSafety()` defaults `destructive_enabled=False`, but they inject `client._server`, so `_connect` returns early and `GuardedSession` is never built. **The blast radius is six of the eight, not eight**: `refresh_path` and `empty_trash` are already inside the executor's deliberate `except Exception`, whose docstrings promise "never raises", so converting those two is observably a no-op. What is actually traded away is an HTTP 500 on the Leaving Soon "Update now" button becoming a 200 carrying the same sentence an unreachable Plex produces (rules 92/93). **The `to_thread` constraint is confirmed and its stated reason is imprecise**: `asyncio.to_thread` copies the context, `loop.run_in_executor` does not do it for you, and a bare shared executor makes `_declared` read `False` inside the worker so every journalled write is refused. That is fail-closed in both directions, and both executor sites swallow it into a `log.warning`, so it is a silent breakage rather than a safety hole. Six options were put to the owner, from "do not build it" to "re-base `SafetyViolationError` outside `Exception`" |
| C4, C10 | not started | — |
| C5 | **settled, driven retrospectively 2026-08-10; both drives pass** | Held after the fact, `e6f7a8b9c0d1` having landed at #600. **A fresh install**: empty directory, `alembic upgrade head` exit 0, then release M's ORM writes all six tables the revision touches, exit 0, and the omitted columns store `0`, `0`, NULL, NULL, NULL, NULL. **A tester's database**: the 2026-08-09 backup, stamped `d5e6f7a8b9c0` and holding 208,460 candidate rows, upgraded exit 0 with `quick_check` ok, `foreign_key_check` empty, the hidden foreign key and `COLLATE NOCASE` both still on the table, and every count equal to its pre-migration count plus exactly the rows the drive inserted. **The C1 rollback claim holds in both directions and is no longer only an argument**: `origin/dev`'s models, loaded ahead of the branch's, write every retired column into the migrated database and read mixed NULL and non-NULL rows back, and the full two-step rollback then backfilled the NULLs, restored `NOT NULL`, kept the collation and accepted M-1's writes again. No reader would have met a NULL anyway, `origin/dev` holding two write sites for the three nullable-ized columns and no read site at all. **Two red demonstrations**: release M's ORM against the M-1 schema fails four of four first writes, one `IntegrityError` per NOT NULL column, with `snapshot` and `candidate` passing because `poster_url` needed no DDL; and the same broken shape stamped at head passes `alembic check` while those four writes still fail. **The blind spot, measured over six states.** `include_name` filters the reflected side and the metadata side is empty because the attribute is gone, so the check compares *nothing at all* about the six columns or `fk_profile_active_policy_id_policy`: not whether they exist, not their nullability, not their server default, not their type. It stayed green on all five columns reverted to the M-1 shape, on `profile.enabled` dropped outright, and on the foreign key dropped; it went red on a live column dropped and on a stamp behind head, so everything else about those tables is still covered. The one false red is the M+1 trap: re-declare an attribute under a retired name without clearing its `RETIRED_COLUMNS` entry and the check demands an `add_column` for a column that is already there, whose generated migration then dies on `duplicate column name`. `TestReleaseMLetsTheRetiredColumnsBeOmitted` is the only thing standing in that gap, because `conftest.py` builds every test schema from `Base.metadata` and no functional test ever inserts against the production shape. **One precondition recorded rather than fixed**: the first downgrade of the drive exited 1 on the revision's own `RuntimeError`, a profile row with a NULL `active_policy_id` and an empty `policy` table being unreversible, and that is the ordinary state of a fresh M install, since `services/profiles.py` deliberately writes no policy row any more. It fails closed, names the remedy, and bites the install with least to roll back to; the tester database with 51 policies rolled back clean |
| C4, and the phase it gates | **owed, and phase 5 closed without it** | C4 is a mandatory phase-5 checkpoint and phase 5 reads `done` with no recorded owner decision for it. A mandatory checkpoint records **what the owner decided**, so "never held" and "held and not written down" are indistinguishable here and the second is not recoverable. C5 is settled above and was recoverable, being a drive against artifacts that still exist; C4 is a read of a deletion list that was already carried out, which is not. Named rather than quietly carried: closing phase 8 on a counter is the failure this document already ruled out, and closing phase 5 on one is the same failure one phase earlier. C3's owner read has now stood open through five phases on the same footing |

Replace this row with one row per checkpoint as it is reached. A mandatory checkpoint (C4, C5,
C7, C9, C11, C13, C14) records **what the owner decided**, not that they looked. The decision is
the part the next session needs.

### Landed

One row per merged sub-PR. This is the history the squash merge will destroy, so it is written
here first and never reconstructed later.

| Sub-PR | Phase | Finding IDs | Symbol | Baseline moved? | Notes |
| --- | --- | --- | --- | --- | --- |
| #562 | 1 | Tier A | `_policy_lab.pinned_baseline` | n/a, it is the baseline | 880 blocks re-pinned, every leaf additive. Fixture 754 KB → 1,574 KB |
| #563 | 1 | Tier B | `scripts/baseline_capture.py` | n/a, it is the baseline | Snapshot 86, 5,965 items, 592 planned. Source database digest unchanged |
| #568 | 0 | C1 | the six decisions | no | Recorded on each finding. Rule 148 landed separately on `dev` (#567) |
| #570 | 2 | W1.3 | `_repo_text_files`, `_source_files_to_scan` | no | On `dev`. `test_repo_hygiene.py` 53.41s to 9.11s |
| #571 | 2 | W12a-1 | `conftest.pytest_configure` | no | On `dev`. `crypto.py` untouched; the cheap map is injective and pinned |
| #572 | 2 | W12a-2 | `test_the_pre_save_test_carries_the_checkbox_value` | no | On `dev`. 15.04s to 0.01s, and off rule 119's environmental accident |
| #573 | 2 | W12a-3 | `test_openapi_tags.schema` | no | On `dev`. 4.70s to 1.32s. The fixture boots hermetically itself, since `_hermetic` runs after it |
| #574 | 2 | W12a-4 | twelve `@vitest-environment node` docblocks | no | On `dev`. Frontend environment CPU 37.25s to 29.56s |
| #575 | 2 | W1.4, first bullet | `settings`, `sync_db`, `async_factory`, `client` | no | 16 hand-written boots retired across 15 files. The other three bullets are untouched |
| #577 | 2 | W1.4, second bullet | `renderWithProviders`, `renderHookWithProviders` | no | All 87 provider trees across 38 files, one left standing with its reason. 1,320 frontend tests either side. Widened the rendered-surface walk, which the rename had emptied by 29 files |
| #578 | 2 | W1.4, third bullet | `tests/_fakes.py`, `mypy src/reaper tests/_fakes.py` | no | 15 client fakes retired into 5, 84 suppressions gone. The gate widened to cover them, which is what makes inheriting the real client mean anything, and a hygiene test pins its four spellings |
| #579 | 2 | W1.4, fourth bullet | `src/test/apiMock.ts` | no | All 35 hoisted api mocks onto one 94-function mock, checked against `Object.keys(api)` both ways. The `vi.hoisted` idiom and its 784 call sites are untouched |
| #582 | 3 | W1.5-c | `test_the_select_name_matcher_rejects_what_it_claims_to_reject` | no | One case cut. It drove the same branch on the same tag as the entry above it, since the matcher returns before reading `text`. The orphaned comment moved to the loop it described |
| #583 | 3 | W6-5 | `_LAYERS`, `_EXPECTED_LAYERED_MODULES`, `_DEFERRED_CROSS_PACKAGE_IMPORTS` | no | Order is api → services → clients → engine, `identity.py`'s purity note deciding the last pair. 78 modules, 6 pairs, 3 deferred sites, all reconciled by hand. The deferred three are held to the rule rather than skipped: measured, all run downward |
| #585 | 3 | W6-8 | `NetworkReached`, `_LOOPBACK_HOSTS`, `tests/test_network_guard.py` | no | Hooks `getaddrinfo` plus both `connect` forms, allows loopback and AF_UNIX. Found **7** live violations, not one: plexapi speaks requests, which respx does not mock. Off `Exception`, or `PlexClient._connect` converts the refusal to `PlexError`. The allowed population is 2,023 socketpairs across 1,674 tests, not 58 across 38. Opened #584 for the sync path nothing has ever seen succeed |
| #586 | 3 | W6-6 | `_EXPECTED_WORKFLOW_PATH_FILTERS` | no | On `dev`. Three path filters in two workflows, against two sentences claiming one. `ci.yml`'s `changes` comment and CLAUDE.md's paragraph both corrected, and the gate's failure names them both |
| #588 | 3 | W6-5, audit | `_imported_modules` | no | `from reaper import services` produced no edge, so an upward import read as clean. Driven: passes at #583, fails here |
| #591 | 3 | W6-6, audit | `_WORKFLOW_PATH_FILTERS` | no | On `dev`. The gate pinned a count, which cannot see a filter moving between files; now a set. `ci.yml`'s header contradicted the comment #586 fixed, 50 lines up. Opened #589, #590 |
| #592 | 3 | W6-8, audit | `_is_allowed`, `_owner`, `_real_resolvers` | no | 3 hooks to 10. Five resolver siblings and both UDP forms escaped a live probe; `_host_of` crashed on an unhashable address and allowlisted a bare string. Every hook now driven refused and allowed, and a refusal names the test that owns it |
| #593 | 4 | W10-2 | `InstanceError.status` | no | Latent as the correction says: the two 404 arms covered the base class and were right only against today's callees. Five arms now read one declaration. The gate walks `api/` by AST, pins 6 handlers and 5 responses, and bans a literal status; three routes had no status test at all |
| #594 | 4 | W10-3 | `apply_stored_schedules` | **yes, named in the PR** | Boot calls the shared function. The correction's population difference is real and preserved: an orphaned stored row was a boot-only `KeyError` warning and is now an explicit event on both paths. Two boot tests where there were none, both driven red. Re-anchored `main.py:520-523`/`:706` |
| #595 | 4 | W10-6 | `_strut_comment`, `_bolding_and_strutted` | no | Comment-only in the CSS, as the correction says: the two rules are byte-identical and the enumeration omitted `.view-tab`. Six controls bold when chosen, five carry the strut, `.filter-mi` is exempt in writing. The gate reads the claim *sentence*, not the block, because the block's own narrative mention of `.view-tab` made the first version green on the very drift it was written for |
| #596 | 4 | W10-7 | `plexServerQueries.invalidateAllPlex` | no | Decided to fix here rather than move to #550, on the finding body: the fix adds keys, so it is rule 79's class and not a comment correction. Five server-changing paths across two components, one declaration. No symptom confirmed by reading `App.tsx`'s gate, not assumed |
| #597 | 5 | W1.1 a-h, i's `_SeriesWork.plan`, j, k, m, o; W7-3/4; W7-5's `detail` | `evaluate_rules`, `GateConfig.gate`, `_verdict(override=)`, `HealthOut` | no | 13 findings landed, W1.1-l killed. Two deletions were bigger than their rows: `_verdict`'s override took `blocked_holds_reap` and `safety_protected` with it (rule 64) and moved 14 reap assertions onto `reap_override_verdict_decoded`, the only caller production has; `is_available` took `MediaRequest.status` and the `MediaStatus` enum. `Condemn logic` daggered, `DECISION_SECTIONS` 17 to 18 |
| #599 | 5 | W1.2 | `engine/backtest.py`, `engine/calibration.py` | no | 1,974 lines of engine and test, plus ~30 prose sites nothing would have failed on. `FALLBACK_REWATCH_PRIOR` is NOT rehomed: its only reader was `rewatch_prior`, whose only caller was `BacktestResult._expected_rates` (this row first said `backtest.run`; same file, so the argument held, but the symbol was wrong), so the correction is right that moving the pair moves dead code. The curve survives in `SIGNALS.md` and a new hygiene test holds the two source docstrings to it by name (rule 144), which is what the deleted `TestTheRewatchPrior` used to do. **The review found one real coverage loss and it is repaired here**: the suite's only non-default `window_days` sweep lived on the replay lane, so `TestTheWindowScoredAgainstIsThePolicysOwn` now pins both readers of the span on the live scan, driven red against each. M3c/M3g dropped, M3f done, open item 2 gone. S7: 78→76 modules, 49→47 loggers, 43→39 reasons |

| #600 | 5 | W1.1-i's poster chain, rule 148 release M | `e6f7a8b9c0d1`, six write-only ORM attributes | no | **Row written after the fact, from the PR body**, which is why it is here and not in the landing commit; see #604. Rule 148 release M for six columns `src/`, `tests/` and `frontend/src/` only ever write. The attributes leave, nothing is dropped. Five are `NOT NULL` with no server default, so deleting the attribute alone breaks a fresh install's first write; the revision lands the shape ahead of them, per column: `sa.false()` where the retiring value is still a real answer (`profile.enabled`, `list_config.built_in`), nullable where there is no honest default (`pending_plex_login.pin_code`, `plex_server.owner_plex_account_id`, `profile.active_policy_id`). **Three traps, each found by driving rather than reading**: a `NOT NULL` FOREIGN KEY cannot take a `server_default` at all under `PRAGMA foreign_keys`; the `list_config` batch rebuild silently dropped `COLLATE NOCASE`, since reflection does not report collations, and two lists differing only in case then answered one keep rule; and `include_name` had to grow a `foreign_key_constraint` arm, because hiding a column from autogenerate does not hide its FK. Counter-proof at the previous head: the first settings save dies with `IntegrityError` on `profile.enabled` |
| #601 | 5 | W1.1-n | `SpareIn`, `whitelist.spare`, `whitelist.list_spared`, three `/api/whitelist` routes | no | **Row written after the fact, from the PR body**; see #604. One way to write a keep-list row, not three. **Nine test files changed and that is not the usual warning sign**: five assert the deleted routes exist, which is what the PR removes, and two were parametrized over the byte-identical pair precisely because only one was driven (rule 72). **The plan's file count was wrong and the correction is the general lesson**: it said six, counting the production sweep; four more test files call `spare()` as setup 32 times, so a count taken off `src/` understates the work whenever the deleted thing was also a test convenience. The 32 rewrites were AST-compared at base and HEAD, 41 calls per side, zero mismatches. Review: 19 candidates, 7 survived, all tier 4. The one worth carrying forward is **one count in five ungenerated prose copies**, two in `main.py` and three in `test_general_and_logs.py`, all saying 87 operations and 42 fenced against a measured 96 and 48, stale before the PR and moved further by it |
| #603 | 6 | W2, `season_scan` row | `guard_result`, `no_key_reason`, `_NO_KEY_REASONS` | no | 152 lines to `season_evidence.py`, every executable line byte-identical, and the served OpenAPI document byte-identical either side (194,926 bytes, built in-process from both revisions). `api/routes.py` no longer imports the scan module at all. Three review lanes found nothing at tiers 1-3. What they did find is the same class twice: comments moved with the code and were false on arrival ("kept beside its own builder", which stayed behind), and a docstring written for the pair claimed both are read by the simulator's replay when only `guard_result` is. 22 shifted plan citations re-anchored here under S10 rather than deferred to the exit sweep |
| #605 | 6 | W2, `api/settings.py` row | `api/plex.py`, 14 PLEX routes, 16 schemas | no | **Row written after the fact, from the PR body.** 698 lines out, `settings.py` 2,044 to 1,344; 700 consecutive lines of schema and route body match in exact order, by sequence matcher against the base. The **sorted** served document is byte-identical, 96 operations; `paths` insertion order moves and nothing reads it. `plex.py` imports the request accessors rather than copying them, so phase 8's `api/deps.py` still collapses five copies. Twelve findings, all fixed there. The one with teeth: `.claude/rules/auth.md` scoped rules 11/98/125/126 to `api/settings.py` and the watch-evidence gate moved out from under it, which nothing failed on because the hygiene test parses rule numbers and never the globs. `api/backup.py` held a fourth uncovered gate site; both added, in `auth.md` and in `CLAUDE.md`'s table row. Rule 144 three times, including a failure message that told the next author to edit a `Landed` row whose figures are a historical delta. Closed #604 by writing the #600 and #601 rows above |
| #608 | 6 | W2, `App.tsx` row | `SectionNav`, `ReapBar`, `ScanFreshness`, `UserMenu`, `WhyPanelFallback` | no | **Row written after the fact, from the PR body.** 506 lines out, `App.tsx` 1,225 to 719. Every moved span identical apart from comment corrections, checked by reconstructing the base and diffing each cut; deleting the two ranges leaves the import block as the only other difference. `ReapSheetLoader` stays, since `Dashboard` renders it. Rule 146 holds both ways. The query-failure map is conserved, App 8 to 7 plus `SectionNav` 1. Four findings, all fixed there: rule 72 swept in one direction only, so `App.tsx` still named a `signOut` that had left it at three sites and six comments cited moved components at their old address; a plan row whose 728 was arithmetic rather than measurement; two `refuted.md` fingerprints re-pathed. Rows 7 and 8 dropped and filed as #606 and #607 |
| #609 | 6 | W2, `engine/policy.py` row | `policy_migrations.py`, `policy_warnings.py` | no | 1,577 lines out, `policy.py` 2,710 to 1,133; every top-level symbol moved byte for byte, 44 before and 44 after. Same 96 operations and the same `paths` order, and the sorted document is NOT byte-identical: three description lines carry the renamed module, which FastAPI serves. `_EXPECTED_LAYERED_MODULES` 77 to 79; the logger counter does not move, since a split inherits its parent's loggers. Three lanes, nine findings, all fixed there. `ruff check` was red while `ruff format` was green, because the rename adds 11 characters to prose and format does not reflow it (rule 134). The new module's header claimed nothing in it is on the live path while `LIST_GATES_NOW_KEEP_RULES` is read by `scan_runner.build_gates` on every scan. Rule 144 for the fifth PR running, this time on the Landed cell's own figures, stale at its own tip. Eleven `refuted.md` rows re-pathed |
| #612 | 6 | W2, `api/routes.py` row; C6 | `api/review.py`, `api/policy.py`, `api/simulate.py`, `api/vocabulary.py`, `api/about.py` | no | 2,827 out, `routes.py` gone: review 1,469, simulate 854, policy 443, vocabulary 114, about 72, so the tree gains 125. **The invariant is exact** — 96 operations over 79 paths, every `(method, path, operationId, tags)` tuple identical, both documents built in-process and diffed. **C6's edge count was low and its two were a cycle**: `simulate` reads `_decode_explanation` and `_entries` from review while review reads `_replayed_evidence` from simulate, so the settled cut would not have booted. `_replayed_evidence` moved to `review.py` beside its two siblings, inverting the one edge C6 named; graph acyclic. The rule 64 sweep was **39 dotted citations across 26 files** and six test files, which is the correction's own figure exactly; the row's "roughly ten" was the low one. A first draft of this cell said 43 across 27 and called the correction an undercount, using the base population (which includes four citations the plan keeps as history) to overrule a figure that was right. **The guard the row called "worth considering" is written** and every spelling driven red, but only after review found it covered one of three: `api/routes.py` and bare `routes._chip` were 25 more citations it could not see, and a `.` in its lookbehind dropped the `:func:`reaper.…`` form its own docstring claimed to match. It found two citations already stale at this base (`services.leaving_soon.sync`, `db.types.TZDateTime`). Loggers 48 to **50**, not 52: two of the five log nothing and their generated loggers were deleted rather than counted. `_EXPECTED_LAYERED_MODULES` 79 to 83 |
| #611 | 6 | W2, `components/Settings.tsx` row | `GeneralPanel.tsx`, `JobsPanel.tsx`, `SecurityPanel.tsx`, `NotificationsPanel.tsx`, `ServicesPanel.tsx`, `AboutPanel.tsx`, `BackupPanel.tsx` | no | 2,847 lines out, `Settings.tsx` 3,086 to 239; the seven hold 2,934, so the tree gains 87, the eight headers and import blocks less the seven banners they replace. 2,797 moved lines reconstructed from the base and diffed: 2,790 byte-identical and **seven** changed, becoming eight because one comment gained a line. The two are the `export` `JobsPanel` and `BackupPanel` need across a file boundary; the other five are the three comments rule 72 re-pointed. The first draft of this cell said "two", which was the count of `export`s read off the fix list rather than off the diff, and the review caught it — 2,797 is the number of lines *compared*, not the number that matched. `PANELS` is byte for byte and the shell is bar three, all one class the review surfaced: the dirty record's comment said "the other four" and "the nine in `PANELS`" against ten sections and five silent ones, and the rail's said "nine labels". Wrong on `dev` since the tenth section arrived, and left wrong by a split that kept the block byte-identical — which is how a count survives a move that re-reads everything around it. **Every figure in this cell was re-measured at the tip after the review fixes landed, and four of them had moved** — the review's own edits changed two file sizes, which is #609's lesson arriving on schedule. **Two pinned per-file populations are the proof no branch changed shape**, and both conserve exactly: 8 query-failure handles to 8 across seven files, 6 reload sentences to 6 across five. The barrel re-exports `isDiscordWebhook` and `MIN_ADMIN_PASSWORD`, so `DiscordModal` and `SetupPasswordStep` are untouched; the `PlexPanel` re-export those were first modeled on had **zero** callers and is deleted, which both review lanes found independently. `JobsPanel` and `BackupPanel` are exported now only because the shell imports them; neither gained a test, so that half of the correction stands. Rule 72 both ways: three moved comments pointed outside their new file, and `index-outside-text.test.ts` asserts a class renders in the file it names, which would have gone red. Seven `refuted.md` rows re-pathed, three of them carrying line numbers already stale at this base. `I18N_PLAN.md`'s "76 files" is **not restated**, because adding seven to someone else's measurement is the arithmetic-for-measurement swap #608 shipped; the same review pass caught the first draft re-attributing that file's whole 142-string count to the panels, when the shell keeps the `PANELS` labels and the switch-confirm templates |
| #616 | 6 | W6-3, `library_index` half; closes issue #559 | `_SPINE_MAX_PAGES` | no | **Row written after the fact, at the phase 8 completeness audit**: the PR landed and no row was ever added, so W6-3 read as half done for two phases while both halves were in the tree. The spine paged on a short page rather than on Tautulli's reported count, so a library that pages short was read in part and reported as whole. It pages on the count now, bounded by `_SPINE_MAX_PAGES` at 1,000, and degrades short rather than returning a partial set as a complete one. Rule 56/89's complete-or-raise contract, on a read that feeds dormancy. The seerr half of the same wave row landed separately at #653, and the fourth loop of the class at #684 |
| #618 | 7 | W6-2, rule 94's `IN` bound | `KEY_CHUNK`, `grace.py` batching | no | **Row written after the fact, at the phase 8 completeness audit.** Landed out of band during phase 7 as commit `c7189d6`, which is why the phase 8 Notes cell says "W6-2 landed early as #618" while the Landed table never carried it. Issue #556 made rule 94's bound a live defect rather than a tidiness item: the grace report read the whole condemned set in one `IN` clause, which overflows SQLite's variable ceiling past 32,766 keys and aborts the read. Chunked on `KEY_CHUNK`, the one declaration rule 94 names. The gate that sweeps all three `IN` spellings for a written classification per site landed with it |
| #638 | 7 | W4.3, typing half | `engine.verdict.Verdict`, `engine.verdict.Override` | no | The app's central vocabulary was declared in TypeScript and passed around Python as a bare `str`. Two `Literal`s in `engine/verdict.py`, read by `decide_verdict`'s return, by `OverrideIn.decision` (rule 131, it restated the pair one import away) and by `baseline_capture._VERDICTS`. **Kept out of the response models deliberately, per the correction**: `CandidateOut.verdict` and `.override` validate rows already on disk, nothing constrains either column at the database (confirmed: no `CheckConstraint` on either, and the only two in the tree are `AutonomyGrant`'s), so narrowing them would turn a legacy value into a 500 on the review queue rather than a row that renders oddly. **Net a guard swap, not a guard addition**: `returned_string_literals` walked `decide_verdict`'s AST to reconcile a set with no declaration, and its own docstring said to delete it when one arrived, so it goes with its single caller (rule 64) and mypy takes the job — driven, a typo'd return is a `Literal` error. Three new tests hold the two TS unions to the Python declarations, which is the hole the mirror guard cannot see: it pairs `export interface`s and compares field *names*, so both sides agree `verdict` exists and neither notices they disagree about what may be in it. Driven red by shortening the union. `EXPECTED_INTERFACES` and `EXPECTED_PAIRS` re-verified against the tree at 91/88 and neither moves, since a `Literal` is not an interface. **The correction is wrong by one**: phase 7 says the 17 `export type` unions are exactly what the mirror does not cover, but `SimStale` is one of the 17 and is covered, so the uncovered count was 16 and is now 14 |
| #639 | 7 | W8-4 | `OkOut`, `RemovedOut`, `RestoreCancelOut`, `JobRunOut` | no | Eleven routes published an anonymous payload; four models name them. **Both the item and its correction get the type wrong, in opposite directions**: nine routes are `dict[str, bool]`, one is `dict[str, str]` (`run_job`) and one is a bare `JSONResponse` (`restore_restart`), so eleven was the right count of anonymous payloads and the correction's ten corrected a number that was not wrong while repeating the type error. The correction is right that `restore_cancel` needs its own model, and it is the fourth the three-model set silently loses `cleared` to. **Byte-identical is measured, not asserted**: the served document was built in process at the base and at the tip and diffed per route, every `additionalProperties` map became a `$ref`, and seven existing tests already assert these bodies with `==`. Four routes had no body assertion at all and now do, `plex_unlink`'s second arm among them, the only route of the eleven with two return paths. **Rule 72 carried it three routes further than the item**: `poster`, `logs/download` and `backup/download` published `application/json` with an empty schema for image, text and archive bytes, telling a script author to parse a PNG as JSON. `responses=` alone leaves `application/json` beside the real type, which reads as fixed and is not; `response_class=` is what removes it. **One language, deliberately**: the mirror guard walks browser types only, and all eleven client functions type their result with an inline object literal rather than an `export interface`, so `api.ts` is untouched and `EXPECTED_INTERFACES`/`EXPECTED_PAIRS` stay 91/88. `test_app.py`'s `test_every_route_response_model_resolves` has been vacuous for these eleven and now covers them |
| #640 | 7 | W7-5 | `SignalProbeIn.window_days`, `MAX_PROBE_WINDOW_DAYS` | no | The served request field the engine could not act on. #597 removed the `PolicyProbeOut.detail` it fed, which is what spent the third pass's kill and left it genuinely dead. **Measured rather than argued**: at the base, a `FEW_WATCHERS` probe returns the same six points across the whole range the wire allowed, 1 through 36,500, and the same six after the removal. The reach mirror was pinned to exactly the ceiling, so the shortfall arm could never fire from a probe. **Rule 64 is the half the correction glosses**: the field's removal strands `MAX_PROBE_WINDOW_DAYS`, whose entire documented purpose was the bound it enforced and whose docstring cited `SignalProbeIn.window_days` by name. The pair collapses to one `_REACH_DAYS`. `test_it_still_answers_at_the_widest_window_the_wire_accepts` is deleted rather than updated: its subject was the two constants being one declaration, and an updated version would assert a mirror against a bound nothing enforces. **S4, stated rather than assumed**: this removes a field from the published document and there is no route-level test for `POST /api/policy/probe` anywhere, so the request model's only coverage was at the function level. `GateSettingIn.window_days` is a different, heavily-read field and is untouched; the sweep went by symbol, never by name |
| #641 | 7 | W7-2 | `CandidateOut.spared` | no | A dead second name for `override == "spare"`, set literally that way and read by nothing: `grep -rn '\.spared\b'` over both trees and `tests/` returns no hit at all. Every render site asks `override` directly. **The correction's two production writers are real and are the whole reason this is not a one-line delete**: `useOverrideMutations.ts` patched it into the optimistic cache at both sites, typed against `Candidate`, so dropping the declaration alone is a `tsc` failure on a fresh object literal's excess-property check. Both write it, neither reads it. **The row's ~12 lines under-counts the same way #601's did**: six frontend fixture files set it to satisfy the type, so the real touch is 10 files. A count taken off `src/` understates the work whenever the deleted thing was also a test convenience, which the plan recorded two waves ago and then repeated. The field docstring went with it and was worth reading first: it claimed the queue strikes an item through off this field, which nothing implements, and named a Spared list retired in #601. Rule 72 sweep clean, `GroupOut`, `GroupSeasonMarkOut` and the database carry no sibling |
| #642 | 7 | W8-3 | `LeavingSoonOut`, `SignalCountOut`, `ReapBreakdownOut`, `RunOut`, `RunSummaryOut`, `UserOut`, `RestoreSummaryOut` | no | **The plan says eleven, its correction says 14, and the measured set is 20 across 8 models.** The correction is also internally inconsistent: the plan's own enumeration sums to 14 with `RunOut` at three, so adding `approved_at` as "a fourth the list misses" makes 15. What the enumeration misses is `LeavingSoonOut.applied` (`JobsPanel` reads `ok` and `result`, nothing else) and `RunSummaryOut`'s `snapshot_id` and `held_back_unknown_size`, whose look-alike reads are all on `RunOut`. 18 dropped here, `empties_after_scan` split out to be wired rather than dropped (C11) and `TestOut` to W8-5. **Every verdict was re-derived by grep before deleting, and two of the correction's citations are comment lines**: the interlocks read `run.approved_manifest_hash` at `executor.py:977` and `run.policy_hash` at `:1000`, not `:975`/`:995`. The audit fields are safe to drop because every interlock reads the ORM row; `approved_by` is additionally the constant string `"api"` on every response an operator can obtain, and the stub comment promising it becomes the signed-in admin "once auth is wired" is corrected. **Rule 64 took four service fields with them**: `SignalCount.bytes`/`.unknown_size` and `ReapBreakdown`'s two `_unknown` counters had exactly one reader each, the wire model, so the per-signal size accumulators are now dead and go. **`tsc` finds only half the stale fixtures**: the annotated ones fail, the `as Run` assertions and the `any`-typed `mockResolvedValue` arguments do not, so the sweep was by grep. `test_api.py`'s exact-field-set assertion on the run list is the one guard that fired |
| #643 | 7 | W8-5 | `TestOut`, `InstanceProbeOut`, `InstanceProbe` | no | The published contract said a Discord webhook test may return Sonarr root folders. Narrowed by inheritance rather than three per-route models: two of the three routes already construct only the verdict fields, so their bodies do not change at all, and the browser's `TestBadge` takes one shape for all three surfaces. **The win is a deletion the item does not mention**: `api.ts`'s `TestVerdict` was a hand-written `Pick<InstanceTest, ...>` existing precisely because the wide shape let the service card fill in a `map_error: null` about a read nobody ran, and the narrowing makes that structural, so the alias and its paragraph go (rule 64). **`ServiceModal` loses `carriesMapping`**, a boolean captured in `onMutate` doing by hand what the type now does: which shape came back is read off the payload (`"map_error" in result`) rather than from a flag beside it. That guard is what stops an absent list posing as "this instance has no folders" and pruning the stored map to nothing at save, so it was worth making unforgettable. **No test in either tree asserted the two narrowed routes' bodies**, so nothing would have failed if the narrowing were wrong; one now does. `EXPECTED_INTERFACES` 91 to 92 and `EXPECTED_PAIRS` 88 to 89, `InstanceProbe` pairing on the suffix rule with no ALIAS entry. The mirror's own note that `CandidateDetail extends Candidate` is "the only case today" was made false by this commit and is corrected in it (rule 144) |
| #644 | 7 | W8-3, `empties_after_scan` | `trashWarning.autoEmpties` | no | The one field of the twenty that is WIRED rather than dropped, settled at C11. It reports Plex purging its own trash after every scan, which is exactly when the executor's trash interlock never gets a say, and `usePlexTrash` was one clause from showing it. **The plan's rule 25 framing rests on nothing and the correction is right**: `PlexTrashOut`'s docstring makes no claim that a page reads this. The substance is what carried it. **Owner call on the shape**: it is a sentence on a warning that already fires, never a warning of its own. Plex ships the preference on and most servers leave it, so warning over an empty trash would stand in front of every reap and train the operator past the one that matters, which is the same argument the existing quiet case rests on. `show` is therefore unchanged and the acknowledgement still gates Reap on exactly today's states. `null` says nothing rather than "Plex does not empty its own trash" (rule 93). Three tests, each driven red first: silent on an empty trash, present on a full one, silent on an unread preference. Mocked up before any code moved |
| #645 | 7 | W8-2 | `STEP_PAGE`, `RunOut.step_count`, `RunStepsOut`, `get_run_steps` | no | The run detail shipped the whole journal to draw 50 rows. **The correction is right about the cap's location and it is the only safe one**: `_run_out` computes `planned` from the full step list one line above the serialization, and `execute_run` re-derives the same phrase through `_planned_candidates`, so a `LIMIT` in either shared helper shrinks the shown phrase and the server's expectation together while `services.executor` loads its own steps and deletes every one. Slicing the ITERABLE rather than rebinding `steps` is the whole discipline, since the line below is a later use of that name. **C7 settled it wider than the plan asked**: paging lands as its own route, because building a `RunOut` re-reads the effective condemned set per call and `ReapConfirm` holds the detail under one cache key with an infinite stale time. The read also corrected the proposal's premise, that the operator can scroll a long plan today: `ReapPlan.tsx` already slices to 50 with no expand control, so the cap removes nothing anyone could see and paging is new capability. **The measurement that decided the test**: no test in either tree built a plan larger than five items, so a `LIMIT 50` in the wrong helper passed the whole suite green. The new one plans 60 and is driven red against exactly that implementation, failing `50 == 60`. **A silent frontend trap, caught by driving rather than by `tsc`**: `ReapPlan.test.tsx`'s fixture is an `as Run` assertion, so a required `step_count` did not fail the build, `more` computed `NaN`, and no test asserted the "N more" line. Two tests now pin it. `EXPECTED_INTERFACES` 92 to 93, `EXPECTED_PAIRS` 89 to 90 |
| #649 | 7 | W8-1, first of two | `CandidatePageOut` | no | The reply's shape, ahead of the rollup move that carries the payload win. A bare list plus four custom headers becomes one model, and `api.ts`'s `candidates` stops hand-assembling the object the queue reads. **The empty branch was answering half the question**: with no snapshot it set two of the four headers, so the browser filled `unknown_size` and `snapshot_id` from two defaults it wrote itself, and a test now asserts the envelope verbatim. `X-Unknown-Size-Count` had zero assertions in either tree, as the item's scouting found; it is pinned on both the populated page and the empty one. **The browser type going snake_case is the mirror's call, not taste**: `CandidatePage` leaves `CLIENT_ONLY`, `CandidatePageOut` pairs on the suffix rule, and the guard compares field names literally, so `offset` is served rather than echoed from the request in order to make the pair exact. `EXPECTED_PAIRS` 90 to 91, `EXPECTED_INTERFACES` unmoved at 93. **One behavior change, with a new guard**: the hand-assembly defaulted an unreadable body to `items: []`, so a 200 with no body drew as an empty queue; this is the one read whose consumer holds a list of pages, so it refuses that reply now, driven red. **The fixture factory is annotated**, which outlasts the rename it was touched for: `apiMock.candidates` is a bare `vi.fn()`, so `page()`'s 57 call sites were invisible to `tsc`. 68 `.json()` reads across 8 test files gain `["items"]`; `_MEMBERSHIP_INVENTORY` does not move, since no `in_` crossed a `def` boundary |
| #662 | 8 | W5-7 | `schemas.PlexServerChoiceOut`, `_server_models`' collision assertion | no | One class declared twice under one name in two routers. **The masking is demonstrated rather than asserted**: `_server_models` buckets on `__name__` and `pkgutil.iter_modules` yields alphabetically, so `plex` is imported after `auth` and wins the key. A field added to the auth-side copy leaves the mirror suite green; the same field in the plex-side copy fails correctly. **Worse than the correction says**: nothing else covers the auth-side body either, since `test_settings_api.py:1316` asserts the field pair for the LINK poll alone and the one test touching `POST /api/auth/plex/poll` never reads the body. **S4 is clean**: the served document is unchanged apart from the component description the new docstring publishes, measured at 80 paths and 146 components on both sides with `PlexServerChoiceOut` a single component `$ref`'d twice, because Pydantic had already collapsed the two structurally identical models into one. 89 of the 146 already carry a description and several are longer. The row's mechanism is right on the part that is easy to miss, that a field on either side renames the component for BOTH operations including the one nobody edited; "silently" is the one word to push back on, since a regenerated client fails loudly. **The rename is not the deliverable, the gate is**: renaming alone leaves the next same-named pair equally invisible, so `_server_models` now refuses a collision. It keys on the class OBJECT rather than counting names, because `schemas.py:745` binds `PolicyProbeIn = SignalProbeIn` and a name-count gate would be red on arrival for a harmless alias. It is keyed per BUCKET as well, which the seam lane caught: `wire` and `inner` are separate dicts, so only a same-bucket collision masks, and keying on the name alone would have forbidden the engine/wire pairing `ALIAS` exists to describe. The walk's own header claimed `PolicyBody`/`ProfileSettings` live in both; the wire spells them `PolicyBodyOut` and `ProfileSettingsIO`, so that sentence was already false and is corrected. Driven both ways, and swept: this was the only collision among the 140 model names. **The diff lane then caught the gate being flag-shaped over an unpinned population** (rule 145): `pkgutil.iter_modules` does not recurse, and `main.HealthResponse` is a published component sitting outside the walk entirely, so a collision with it would module-qualify two operations with the assertion green. The walked count is pinned at 140 beside it, driven red by adding a model, and the three `BaseModel`s outside are classified in writing. **The class docstring also became operator copy**: Pydantic publishes it as the component `description`, so this repository's change history was about to render in the API reference; it is a comment above the class now, and the one-line docstring is what ships. `EXPECTED_INTERFACES` 94 and `EXPECTED_PAIRS` 92 both unmoved, since the browser always had ONE `PlexServerChoice` and what shrank is the server-side dict neither counter reads |
| #659 | 8 | C14's first half; W3's `clients/plex.py` row | `refuse_mutation`, `TestAGuardRefusalReachesTheCallerAsARefusal` | no | **The test C14 exists to demand, written before the refactor it guards.** The eight mutating Plex methods each carry `except SafetyViolationError: raise` ahead of their catch-all, and nothing anywhere pinned one of them: measured, deleting all eight leaves the suite at 4,161 passed, exit 0, identical to baseline. Eight parametrized cases now fail by name when the arms go, driven by deleting all eight at once. The stand-in issues a real PUT through a real `GuardedSession` rather than faking a refusal, and carries its own control asserting it reaches the guard, since a stand-in that stopped issuing the request would make all eight pass on an `AttributeError`. **The owner's addition is the larger half**: every blocked write is now recorded at the point of refusal. `refuse_mutation` logs and raises, in `clients/base.py`, used by both guards (rule 72), so a refusal added later cannot arrive without its line. `reason` is a discriminator rather than a sentence (rules 92/93), the message being operator copy that will be reworded. Seven log tests across BOTH guards, including the token-free path (rule 13) and a control at each asserting an ALLOWED write says nothing, since a guard that logged every mutation would bury the refusals. **The first draft covered one guard**, which the safety lane caught by reverting the http half and watching the suite stay green: the class was called ``TestEveryRefusalIsOnTheRecord`` and reached three of the five refusals, and the unpinned one sits in front of the real ``DELETE``. That is C14s own measurement reproduced against the sibling guard, in the PR that closed it. **What this does not do**: `_call` is not built here. That is the point of the sequence. |
| #655 | 8 | W3, `scan_runner`/`instances` row | `_arr_construction_sites`, `_EXPECTED_ARR_CONSTRUCTIONS` | no | **Measured before building, and the measurement changed what was built.** All six calls already pass all three arguments, so nothing is diverged and a shared constructor would have been pure motion on a deletion-path builder. The row's value is the NEXT divergence, and a helper only binds sites that call it, so this is the gate CLAUDE.md's "write the gate instead" clause points at: an AST walk collecting every `RadarrClient(`/`SonarrClient(` under `src/` and requiring each to pass `safety`, `verify` and `api_path_prefix`. **The pinned count caught the author's own miscount**: the row says "three places" and means functions, and the constant was first written as 4 by reading that figure rather than the tree. It is 6, three functions building two classes each. **Rule 147 is the reason the walk reads call nodes rather than text**: the tree spells these two ways, one per line and one wrapped over five, and a delimiter-anchored matcher reads the first only. Driven red three ways: a site added, a site removed, and one argument dropped. A `**kwargs` splat is collected as passing nothing, so the membership assertion names it rather than the walk losing it. **Two things measured and not fixed here.** `build_sources` builds five clients, not three, and Tautulli and Plex spell `verify=` off different locals, which is the row's own trap one level down; and Seerr's `instance_key`/`link_base_url` genuinely differ between `build_sources` and `api/fairness.py`, which is latent because nothing on the scan path reads `portal_key`. The two `instances.py` addresses in the row were stale by +21 before this branch and are re-anchored |
| #653 | 8 | W6-3, seerr half; W3's `clients/seerr.py` row | `MAX_PAGES` | no | Both seerr walks had no bound at all. `skip >= total` is their only normal exit and `total` is a number the portal re-picks on every page, so the walk's **length** was the server's to choose: the rows are a list, the page is not empty, and neither existing guard fires. **The first draft of this row and the constant's own comment named the wrong trigger**, and the inline comment twenty lines below them had it right — a large but *fixed* total terminates on its own after `ceil(total / 100)` pages, so the case that never ends is a total that keeps rising by a page's worth, and the case that ends too late is one that is merely absurd. That is why the trip reads the page count and never the total. **The trip RAISES where both in-repo models stop and warn**, and that is the whole design question the correction's "modeled on `MAX_HISTORY_PAGES`" glosses. A short history mirror degrades downstream on its own; nothing downstream of `all_requests` can tell a short list from a complete one, and `build_request_index` sets `available=True` on the claim that it read every portal in full. **The counter is explicit rather than derived from `skip`**, which is one line longer and is the only form that bounds `users(take=0)`, where `skip` never advances at all. No production caller passes it and the parameter is public. **The test had to be built so that deleting the cap FAILS rather than hangs**: the mock refuses the page past the cap, so a missing bound is an `AssertionError` in three round trips instead of a wedged suite, and the retry predicate matches transport errors only so nothing swallows it. Both caps are monkeypatched to values that are not production's 1,000 and not each other's (rule 141). **The operator string was cut twice**: the first went out at 126 characters and three colons through `api/fairness.py`'s "Could not build Scales: " wrapper, and carries HTTP paging vocabulary; it reads "the request list never finished, after 340 requests" now, at 82. **Rule 72's remaining sibling was deferred in writing here and is closed at #684**: `clients/plex.py`'s `_iter_pages` had no cap either, and it takes `SWEEP_MAX_PAGES` on the same shape. The deferral rested on nobody having shown a Plex that reports a rising total, which #654 no longer claims: the defect is the missing backstop rule 56/89 requires, one loop of four, and that is provable where the rising total was not |
| #652 | 8 | W3, `clients/arr.py` | `BaseClient.get_list`, `BaseClient.get_dict` | no | One shape guard, one declaration. Eleven hand-written copies in `arr.py` and the reasoning six times; the helper holds it once and has no `default=` or `coerce=`, which is the property that makes the extraction safe rather than convenient (rules 28/93). **The eight list messages are byte-identical after the move and the three object ones change**, exactly as the correction predicts: they were hand-written without the API prefix, so a v5 Sonarr said "series/7 did not return an object" and named a path it had not asked. Generating the message from the path fixes that and a test pins it. **Rule 72 reached `seerr.py`**: six top-level guards of the same shape adopt the helper, five of them gaining the same missing prefix. **The written half of that sweep is where the review found the errors, and both are corrected here.** `plextv.py`'s two stay, for two different reasons and not one: `account`'s message is operator copy ("could not read the Plex account"), so generating one from the path is a rule 21 regression, while `resources` is path-shaped and names `/resources` for a request to `/api/v2/resources`, which is the same defect the three arr messages were converted to fix. It is deferred rather than claimed identical, because it sits in the chain `owns_server` fails closed on. `sonarr_stats.py:103` is a parser, not an HTTP read. Three checks in `seerr.py` stay: the two nested `results` ones read a key inside the body rather than the body, and `plex_machine_id` coerces to `None` as a documented best-effort whose caller already returns `None` on the error. `services/lists.py:272` is the one outside `clients/` and has its own reason: it raises against the *list's* slug rather than the client's service, so the helper would re-attribute the error. **The three object guards had no test at all**, which is the population the correction names, and all 17 are now covered per site: 16 parametrize rows across two classes, plus `tags`, which keeps the test class its own incident is written on. A signature assertion fails on a `default=` parameter. **One message change is wider than "the missing prefix"** and is called out because the sweep's own sentence understates it: `seerr.quota` gains the `/user/{id}/` segment as well. It is a Seerr user id, not a credential (rule 13), and every one of the eight changed strings is log-only, since `explain_failure` classifies on exception type and status and nothing in either tree matches on this text (rule 92). All three driven red first. `SonarrClient.exclusions` and `RadarrClient.exclusions` were byte-identical and collapse onto `ArrClient`, with `exclusion_path` annotated but never assigned there so the base class still cannot answer the call. S7: the logger counter 50 to 49, `arr.py` having declared one it never logged through |
| #650 | 7 | W8-1, second of two; C8 | `GroupRollupOut`, `CandidatePageOut.groups`, `toRollups` | no | The four movable fields leave `CandidateOut` for one rollup per show. **The correction is right that only four of six can move**: `group_title` and `show_status` are read off a flat `CandidateDetail` in `WhyPanel`, `show_status` again off `GroupOut` in `ShowPanel`, and `list_candidates` sorts on `group_title` in SQL. **The riskiest edge is the merge and it is real**: `ReviewQueue` flattens items across pages but reads totals off `pages[0]`, so a `groups` array read the same way drops the rollup for every show first seen on page two, and the bulk bar's count prints beside "Reap now…". `toRollups` merges by `group_key` over every page; a straddling show carries identical figures in both, pinned by a test across three pages. **The `?? g.items.length` fallback is gone** rather than left as a plausible wrong number: a show with no rollup falls into the bulk bar's existing "cards only" arm, since the fetched-season count is the number this figure exists to avoid (rule 5/30). **The item's headline is about a different denominator than it reads as**: "~90%" is of the show-level fields, and the measured page saving on a real library is 7% to 16% (112.8 to 96.7 KiB on a 100-row condemned page; 858 to 723 KiB over that whole lane), worst on a seasons-only page. **#649's annotation is what made the fixture sweep safe**: `tsc` named all 20 stale sites including four `page(items, N)` calls whose second argument is now `groups`, where the untyped factory would have handed them through as `undefined`. The new `rollup()` helper defaults its three figures to zero rather than deriving them from the marks, so a test asserting a count states it (rules 119, 141). `EXPECTED_INTERFACES` 93 to 94, `EXPECTED_PAIRS` 91 to 92 |
| #666 | 8 | W5-2, W5-5, W5-6 | `test_the_wire_and_the_domain_state_the_same_bounds`, `test_a_body_missing_the_caps_is_refused_rather_than_reset`, `test_every_field_of_the_answer_is_compared_across_the_two_tiers` | no | **Three collapses killed and the two halves worth keeping landed as gates.** Each kill carries its `> Killed:` block on the finding body. W5-2 buys zero parameters, since `_judge_item` already takes the carrier whole, and three of the movie lane's overlapping fields are identity-path join keys. W5-5's collapse turns `PUT /api/profile {}` from a 422 into a **200 that saves the shipped defaults over every cap the operator narrowed**, on a route an API key can write, and nothing in the suite pinned it: both existing cap tests GET the full body first, mutate one key and PUT it back. That row is reclassified `safety-path`. The correction's own reason for it does not hold, `main.py`'s handler having already made the 422 sentence identical either way. W5-6's incident was in the loop rather than the constructor, and the two loops must stay different. **All three tests driven red first**, the third against the collapse itself: the route pointed at `ProfileSettings` with the hand-formatting deleted answers 200. Counters unmoved -- no module, logger, interface or pair changes -- and the frontend is untouched |
| #667 | 8 | W5-4 | `ReapBreakdownOut`, `RequesterRowOut`, `LinksOut`, `RestoreSummaryOut`, `SeerrServiceOut` | no | Seven pairs built off their record with `model_validate(obj, from_attributes=True)`, over six call sites, and **no shared base**: inheriting the record's fields publishes `UnmatchedTitle.tmdb_id` and a Seerr hostname and port to the browser, which is why the row has to name its mechanism. **The row's counts are stale both ways and the correction's population is exact**: `ReapBreakdownOut` is 16 fields not 18 and its nested list 2 for 2 not 4 for 4 (#642 dropped the two size accumulators), and the seven sites are 13 pairs. `api/runs.py:776` is W5-5's site in reverse, and the `RunReport` site W5-4 should have named is absent from the row. **No pair has drifted in 13**; three are narrower on purpose and documented at both ends, so what this removes is duplication rather than a live divergence. The other seven are deferred in writing with the blocker named per site: four `datetime` fields the wire types as `str`, three enums, one rename, two deliberate projections. **The guard is the deliverable, because `from_attributes` fails quietly** -- a wire field the record lacks raises at request time when required and is served as its own default when not, where the constructor raised for both -- so the pair walk asserts the record carries every declared field, driven red by adding one, with the site count pinned at six beside it (rule 145). **Three sites had no cover at all**: nothing read `/api/reap/breakdown`, both whole-dict `links` assertions are on rows whose `match_candidates` is empty, and one of the two Seerr service routes had no body assertion. S4 measured rather than argued: the served document is byte-identical at 203,688 bytes both sides. Counters unmoved at 94 and 92, `api.ts` untouched. S10: the plan's W6-7 tray citation and two `refuted.md` citations into `api/settings.py` re-anchored, both stale since #605; the W5-4 row's own addresses are not re-anchored, since the constructions are gone (wave 1.1's rule) |
| #668 | 8 | W6-7 | `buildinfo.env_flag` | no | One reading of an environment value as a boolean, adopted at all six raw reads; `launcher._TRUE`, `update_check._FALSE` and `desktop_flag` go with them. **The behavior change is one case and it is the point**: an unrecognized value read as False at three of the six sites and falls to the default now. On a frozen macOS build `REAPER_TRAY=ture` bought an app with no menu-bar icon, and `LSUIElement` hides the Dock one, so that icon is the only route to Quit. Two rows in `TestTrayChoice` pin the incident rather than the helper, both driven red against the old expression. **Two claims in the row's correction were measured and are wrong, and the block is amended in place**: `raw not in _FALSE` and `env_flag(default=True)` are the same function on every input, so the update check adopts the widened helper byte-identically and the incompatible-unification argument does not hold; and the "live divergence" is REFUTED rather than latent, since `_desktop_out` returns `None` whenever `desktop_platform()` is, which it always is off a frozen build. What survives is that the tray default is one declaration written twice and agrees only because of that gate (rule 104), which a comment now says. **The population is 12, not 6**: the six pydantic `Settings` booleans stay put, because an unrecognized value there raises and refuses the boot, which for `destructive_actions_enabled` and `recovery` is the strongest answer available, and `scan_runner`'s three-state token pair is three-state on purpose. Both are named as considered-and-kept in the helper's own docstring, and the refusal was driven rather than assumed. **The hole was that no test anywhere passed an unrecognized value to any env boolean**; the sweep now covers four against both defaults, with the `default=True` half load-bearing (rule 141) and `""`/`"  "` excluded in writing. The update check's vocabulary is written out as a table rather than re-derived from the retired expression (rule 119), so widening the tray can never quietly flip a check that leaves the operator's network. Placement is `buildinfo.py`, outside the walked packages, so `_EXPECTED_LAYERED_MODULES` stays 83, `_EXPECTED_LAYER_EDGES` does not move and the logger count stays 49. `.env.example` states the vocabulary once, in the header, where none of the twelve stated it before. No `STATUS.md` line was wrong |
| #669 | 8 | W6-4 | `reaper.text.fold`, `test_the_comparison_form_of_a_name_is_one_derivation` | no | Rule 88's comparison form as one function, adopted at every site spelling the composite. **Every count written down was low**: measured at the tip it is 37 expressions over 33 lines in 13 modules, against the row's "~28 across 10" and the correction's "30 across 11", plus five more in frozen `alembic/` revisions that are out of the walk. **No exemption list**, which is the design: `fold(value)` is `value.strip().casefold()` exactly, so adoption is behavior-identical by construction, and the gate bans the COMPOSITE only, so the three already-stripped bare `.casefold()` sites need no entry. **The correction's reason for those three is wrong and is the more frightening one** -- it says they omit `strip()` on purpose, where each reads input a line above already stripped -- and it is amended in place. `normalize_label`, `_tag_key` and `_name_key` delegate rather than being deleted, each keeping the domain reason a generic docstring should not absorb. **What the walk cannot see is stated in its own docstring** (rule 147): a bare `.casefold()` on unstripped input, or `.strip().lower()`, of which 11 are deliberate at this tip and two fold PATHS. (That figure was first written as `dev`'s 16; #668 had already taken six of them on this branch, and the review caught it.) Driven red by putting one site back. **The `func.lower` divergence is made louder, not repaired**: `_refuse_name_twice` compares SQLite's ASCII-only `lower()` against Python's `casefold`, and the `NOCASE` collation behind it is ASCII-only too, so both layers answer the same way today; named at both ends. **`libraries_for_ids` had no test at all** and is the entire input to the stale-mapping guard, whose two callers fold the other side, so a one-sided fold would warn the operator their correct mapping is wrong. Placement is top-level `src/reaper/text.py`, so `_EXPECTED_LAYERED_MODULES` stays 83, `_EXPECTED_LAYER_EDGES` does not move and the logger count stays 49. Rule 88 names the symbol now, and `docs/LEARNINGS.md`'s "only comparison form" sentence survives only because the helper delegates, which it now says |
| #672 | 8 | W3's cache-database row (killed); issue #660 | `aio.per_loop_lock`, `lists._widen_lock` | no | **The row is killed and the one real thing in the cluster is built.** Every load-bearing noun in it is wrong by one: two bootstraps not three, two singleton stamps not three, one SQL spelling not two, and "~90 lines" against a 418-line cluster with 25 to 35 removable. **"Adopt the strictest" has a dangerous reading**: `history_sync` is strict twice, and only the per-loop lock plus the re-read inside it generalizes. Its DROP-on-stale-shape must never reach either sibling, since at `lists` it empties every keep list until the next sync and at `imdb_dataset` it withdraws rating protection library-wide, so the primitive carries no schema policy at all. **The duplication the row missed is the lock, and finding it found a `dev` defect**: `lists.ensure_schema`'s `PRAGMA` and its `ALTER` are a check-then-write nothing serializes, pysqlite autocommitting DDL, so two callers raise `duplicate column name` and abort a scan. 7 of 30 rounds at two callers; the twelve-round test is 5 of 5 red without the lock. Settles **#660**, filed as a question by an earlier pass and now `Reviewed/Confirmed` with the evidence in its body. **Placement is `aio.py` rather than the scouted new module**, so no counter moves at all. **The review pass corrected two claims this branch inherited and promoted**: an `asyncio.Lock` binds to its loop on a CONTENDED acquire only, so a shared one raises intermittently rather than on the second test, and the weak keying collects a loop that never contended but not one that did, since the lock stores the loop on itself. All four `test_aio.py` tests contend, one being the control that shows the raise. It also caught the fold gate's "sixteen `.strip().lower()` sites", which was `dev`'s figure: #668 had already taken six of them on this branch, so it is 11 |
| #670 | 8 | W3, `api/deps.py` row | `api/deps.py`, `session_factory`, `runtime_settings`, `secret_box`, `newest_snapshot` | no | The request accessor half only; the admin-password gate is its own `safety-path` PR, and the contradiction paragraph at *Where the phases collide* says why the split does not reopen what it settled. **The row's shape was wrong three ways** and the finding body is corrected in place: the `_sessions` three are `api/{review,runs,whitelist}.py` since `routes.py` is gone; the cluster is 7 `_factory` / 3 `_settings` / 2 `_box`, not four of each, because `setup.py` declares only `_factory`; and `_latest_snapshot` is 2 definitions and 7 calls rather than 7 copies. Four more modules imported an accessor instead of declaring one, so the collapse touches 11. **The scout's own design carried a collision it could not see**: `api/review.py` already has a route handler named `latest_snapshot`, so the helper is `newest_snapshot` and no operationId moves. `auth.py`'s `_safety` stays put, being a hardcoded read-only safety rather than an `app.state` read. `_EXPECTED_LAYERED_MODULES` 83 to 84; **the logger counter does not move**, since none of the four functions logs. 32 inline `app.state` reads are deferred in writing (rule 72): they copied no function. **The review corrected that deferral twice, and both corrections matter to whoever honors it.** Its module list omitted `api/runs.py`, which holds three of the 32 and is the one route that deletes, so a sweep walking the named modules never reaches them. And the three said to read off a bare `app` are not those three: `lists.py:218` and `:240` sit inside a handler that aliases `app = request.app`, so they can adopt today, while all three genuinely blocked reads are in `api/scan.py`'s `launch_scan(app: FastAPI)`, which holds no `Request` at all. The corrected record lives in `deps.py`'s docstring, where the next author reads it |
| #671 | 8 | W9, `LAUNCHER_CONF_NAME` half | `config.LAUNCHER_CONF_NAME`, `_KNOWN_IMPORT_CYCLES` | no | The two services imported the process entry point to read one filename. **Measured either side with an AST walk over the 115 modules: 9 cycles to 2** under the plan's convention, the 7 that go being every cycle through `backup.py` or `restore.py`; `services.backup`'s import closure 305 to 279, the 26 being `reaper.launcher` plus 25 stdlib it pulled. The 7 that go, the 26, the 9 and the two survivors all match the scout exactly. **This cell first said the scout's nine was wrong and named the `api.plex` cycle as one it missed. That was false and is struck here**: the scout states 0/9/10 across three conventions and names that cycle outright. What is wrong is the FINDING BODY, which says 8, and its own correction, which says 6 of them exist for one string where it is 7 of 9. The mistake was reading a review's phrase "the scout's correction block" as the scout FILE rather than this document's correction block, which is what it cited by line. Its 292 and 460 predate `text.py`. **The gate the scout proposed would not have caught the regression it was proposed for**, which is the finding here: a top-level acyclicity assertion passes with the import put back, because every one of these cycles closes on the function-local imports in `launcher.main()`. So the walk counts function-local edges and `_KNOWN_IMPORT_CYCLES` declares the two survivors as an equality. Driven red both ways, 5 cycles from `backup.py` and 2 from `restore.py`. Three gates were looking at this and none could see it: the layering walk reads four packages and `launcher.py` is outside them, the deferred-import gate reads imports that do not run, and the top-level graph is acyclic either way. **S7 held**: 83 modules and 49 loggers both unmoved, re-verified against the tree. One counter added, `_EXPECTED_SOURCE_MODULES`, at 116 after merging #670's `api/deps.py` in (it was 115 when this branch was cut, and the counter caught the difference). **The boot path is unchanged and one measurement says so out loud**: importing `reaper.launcher` now pulls 170 more modules, 100 to 270, all of them already in `reaper.preflight`'s closure which `main()` imports a few lines later. Seven citations re-anchored, two broken by this diff and five already stale by 2 to 35 lines |
| #676 | 8 | W3, `clients/plex.py`; C14 | `PlexClient._call` | no | **C14's option 5, built against the test that was written first.** 18 sites adopt one helper; the eight `except SafetyViolationError: raise` arms become one declaration inside it, and five structural opt-outs stay bespoke and say why in place. **The value is the arm, not the lines**: a refusal is not a Plex failure, and re-labeling it `PlexError` tells the caller Plex is unwell and invites it to degrade, which `leaving_soon._reconcile` does per library. Eight methods each carried the arm by hand, so a ninth inherited nothing; now one cannot arrive without it, and a test drives exactly that through `_call` with a body carrying no arm anywhere near it. **Every operator message is byte-identical, proven rather than read**: the 22 `PlexError` templates were extracted by AST at the base and reconstructed at the tip from each site's `what=`, and the two inventories diff clean. **The red demonstration is nine tests, not one**: deleting the arm from `_call` fails all eight per-method cases plus the helper's own, which is what says the collapse did not quietly weaken them. **Five opt-outs, matching C14's measurement exactly**: `_connect` builds the server the helper assumes, `active_streams`'s message is the fail-closed reasoning rather than a verb phrase, `trash_count`'s own read raises `PlexError` that a shared mapping would wrap twice, `aclose` maps nothing because there is no caller left to tell, and `is_refreshing` degrades to a warning rather than raising. **The review caught that list naming a method that does not exist** (`refresh_running`), in the one place whose job is to be checked against the tree, and in two ungenerated copies of it (rule 144). **`asyncio.to_thread` is load-bearing and the docstring says why**: it copies the context, and the journalled-intent flag is a `ContextVar`, so a bare shared executor reads it unset and refuses every journalled write, silently, since both executor sites swallow it. S3's three suites ran alone: 277 passed, exit 0. **C9 is done, driven against a live server, and an earlier draft of this cell said the box could not do it** -- no `reaper.db`, no Plex to reach -- which was false both ways and came from reading a truncated directory listing as absence. One probe ran twice, this branch's `plex.py` and the base's, same server, deletion off and the shelf's read-only opt-in off: all eight guarded write methods raised `SafetyViolationError` on both trees, never `PlexError`, with the operator sentence byte-identical across all sixteen refusals and the two summaries diffing clean. Zero mutating requests reached the wire either time, the GET-shaped `refresh` and the `emptyTrash` PUT both classified and blocked, 8 `plex.write_blocked` lines logged per run at reason `not_armed`. The probe carried a kill switch below the guard, a patched `requests` adapter raising on any mutation rather than sending it, so a guard that failed to refuse would have been caught there instead of at the server; it never fired |
| #680 | 8 | W3, the pragma half of the `backup`/`restore`/`retention` row (killed) | `test_the_journal_mode_pragma_is_set_in_exactly_one_module`, `test_every_prose_copy_of_the_busy_timeout_states_the_declared_value` | no | **The row is killed and the gate binds a different thing than the row named.** `_configure_sqlite` is already one declaration for both engines; with `_read_revision` and `retention`'s `isolation_level=None` out of scope, the remainder is two one-line `busy_timeout` calls at 5000 and 30000, each value deliberate, plus three sites that need no pragma. **The correction's spread is the right one**: 5000 in one, 30000 in one, absent in three, against the row's 5000 in two and absent in two. The five blocks span four modules and the row's file list misses `db/schema_gate.py`, the module the correction exists to protect. **What is duplicated is the value.** `5000` is a SQL literal in one place and "5s" in five docstrings across `executor`, `imdb_dataset`, `retention`, `scan_runner` and `scheduler`, none derived from it (rule 144); `executor._commit_journal`'s copy is why the journal write takes two attempts with no sleep between them. The gate reads the seconds out of the declaration, so it cannot drift either. **Both anchors are load-bearing and the review is what proved it**: `scan_runner` and `scheduler` never name the pragma, `imdb_dataset` never cites `db.session`, and a first draft asserting one of each was wrong on both. It also caught a symbol that does not exist -- `executor._write_with_retry` for `_commit_journal`, in the test and in two plan copies, which is #676's `refresh_running` a second time -- and a matcher splitting on `". "` after normalizing the whole file, whose largest chunk was 3,417 characters in `main.py` and which read every `404s`/`409s`/`429s` in `src/` as a seconds figure. The matcher is a 120-character window either side of an anchor instead, and no status-code plural in the tree sits inside one. **The pin is per module rather than a set of names** (rule 147): the walk collects passages, so a second copy inside an already-listed module would hide behind the first. Four red demonstrations, each driven: a WAL set added to `restore._force_destructive_off` is named with its line; the declaration moved to 10000 fails all five prose copies by name; one copy reworded to "five-second" fails as a member that left; a second copy added inside `retention.py` fails as `quotes 5s, 5s, expected 1 x 5s`, which is the case a set of module names could not see. Named as out of reach rather than covered: `snapshot.py`'s figure-less "far inside that budget", and `docs/LEARNINGS.md`'s `5 s`/`30 s`, which record a measurement rather than justify a default. **S10**: the two citations this PR's insert shifted are re-anchored by symbol rather than re-numbered, the FROZEN banner check and `_repo_text_files`, and both were already stale at the base -- the banner one pointed at the rewatch-curve test. The two S6 cites above the insert are re-anchored the same way in passing. No `src/` change, so no counter moves |
| #681 | 8 | W3, the admin-password gate ritual; C9 | `deps.require_admin_password`, `password_throttle` | no | **The deletion path's front door, written once.** Re-measured at the tip rather than taken from the row: four gates (arm deletion, change that password, forget the watch record, confirm a restore), each running the same four steps, and the four copies are byte-identical apart from the `gate=` name and the 403 sentence. The helper returns `None` and raises, which is the safety property: a `-> bool` a caller can forget to read is a gate a caller can forget to close. The key tuple is passed rather than derived, as *Where the phases collide* settled -- an `account:` lockout refuses from every address, so merging the four would let five wrong restore passwords from anywhere lock the operator out of arming deletion from their own machine. **The 503 clause is inherited, not re-derived**: a full Argon2 gate leaves `_verify_admin_password` as an exception before the `if`, so a capacity refusal still cannot reach the lockout counters. Six helpers move out of `api/auth.py`, which imports three back; `_safety` and `_rate_limited` stay. **A gate lands beside the refactor** (CLAUDE.md's second rung): `password_throttle` is confined to `auth/ratelimit.py` and `api/deps.py`, so a fifth gate cannot spell three of the four steps by hand. Driven red by adding it back to `api/settings.py`. **Four new tests, each driven red against the mutation of the step it pins**: recording the failure before the verify, dropping the `record_success` loop, merging the account keys, and skipping the throttle check (which fails four tests including both pre-existing ones). **The step nothing covered was `record_success`** -- every throttle test stops at the lockout and never returns through a success, so deleting the loop that clears both keys was green on 4,233 tests. **C9 driven**: one probe, 28 observations across the four gates (wrong password, empty password, the six-deep lockout ladder, a full Argon2 gate, the attempt after it, the correct password, and no admin password set at all), run against this tree and the base with only `PYTHONPATH` varying and each run printing the `deps.py` it loaded. **The two summaries diff clean, exit 0.** The probe carries a kill switch below the gate, `set_destructive_enabled`, `watch_evidence.forget_all` and `restore.arm` all patched to raise, so a leaking gate could not arm anything; it fired on three of the four correct-password passes, which is what proves the success path was driven rather than silently 403ing. The served document is structurally identical, 97 operations either side; the only three strings that move are route docstrings now citing the helper by name (rule 7/24). Logger counter 49 to 50 with its two prose siblings; `_EXPECTED_LAYERED_MODULES` and `_EXPECTED_SOURCE_MODULES` do not move, `api/deps.py` having arrived at #670. `api/deps.py` and `api/auth.py` join `.claude/rules/auth.md`'s globs and CLAUDE.md's `Governs` cell: **`api/auth.py` had never been under them**, holding all six helpers, which is #605's incident pointed at a different file. **S9's two lanes found seven defects, all seven fixed on the branch.** The sharpest is that the `-> None` shape is stronger than it was claimed to be, measured rather than argued: drop the `await` on the new call and mypy refuses it as an unused coroutine, while the base's `ok = _verify_admin_password(...)` with the same `await` dropped type-checks clean and leaves the gate open, a coroutine being truthy. The hygiene gate's own blind-spot inventory was wrong in the reassuring direction (rule 144): it named two escapes it already caught and missed the one that matters, a fifth gate calling `admin_password.verify` straight and running NONE of the four steps, which every one of these routers is one import away from. That name is banned too now, driven red. Four stale citations re-anchored, three of them broken by this diff |
| #677 | 8 | W9, the workaround half | `scan_runner.build_gates`, `scan_runner.build_reap_gateway`, `simulate._replay_simulation`, `_DEFERRED_CROSS_PACKAGE_IMPORTS` | no | Four of the five deferred imports W9 measured as breaking no cycle are promoted to module level; the fifth is killed below. **The runtime graph does not move**: 679 edges and the same two cycles either side, because `test_repo_hygiene.py`'s walk counts a function-local import as an edge already, which is what "breaks no cycle" meant. What moves is the top-level graph, 674 edges to 677, still acyclic, and all 116 modules import in their own subprocess. `_DEFERRED_CROSS_PACKAGE_IMPORTS` 3 to 1. `_EXPECTED_LAYERED_MODULES` (84), `_EXPECTED_SOURCE_MODULES` (116), `_KNOWN_IMPORT_CYCLES` (2) and the logger count (49) are all unmoved, each re-derived from the tree rather than read off this document. **`launcher.py:559` gets the sentence it never had**: promoting it makes `main`, `api.settings` and `api.plex` raise ImportError and moves both declared cycles into the top-level graph, so it is one of TWO deferrals left in `src/` that genuinely close a cycle. The other is `services/lists.py:64`, whose own comment already says why, and it is invisible to all three gates: same-package, so the deferred-import walk skips it, and `TYPE_CHECKING`, so the cycle walk does. Pre-existing on `dev`. A first draft of this row called the launcher one exhaustive and the review measured the second. `:531`'s environment-ordering reason was already written and holds |
| #683 | 8 | W3, executor size interlock (killed as written); W9's `executor.py:137` (kill reversed) | `_CHECK_GREW_SINCE_APPROVED`, `_CHECK_SIZE_UNCONFIRMED`, `_DEFERRED_CROSS_PACKAGE_IMPORTS` | no | **The extraction was built twice and measured, then killed; the rule 144 half lands.** Reason-only is +7 lines and turns one operator sentence into a two-slot template; whole-branch is +9 and returns `an optional StepOutcome`. The sentinel is the measurement: dropping the check at one call site fails exactly ONE test of 4,235, on `assert [(42, 3)] == []`, a real unmonitor reaching Sonarr. `_grew_materially` was already the one declaration of the predicate, so the extraction guards nothing that can drift and adds a fail-open shape one test wide. **Two byte-identical `check=` sentences, not one**, and the second sits in the unreadable-size branch the row exempts; both become constants beside `_NO_APPROVED_SIZE_CHECK`. All four sites pinned, each driven red alone against a swapped literal, each naming a different test; the season's two had no assertion at all, which is why a grep for its wording finds only `executor.py`. **The empty-list premise is real, narrower than the row, and driven both ways**: the paths agree for a measured item, diverge only under the allowance, and only on "the server listed nothing at all", which one `sizeOnDisk` field cannot express separately from "listed but unsized" — an unmeasured season whose files Sonarr lists and will not size is deleted exactly as the movie is. **C9 driven**, 9 scenarios, deletion ARMED so the interlock is the only thing left to stop the run, real Radarr/Sonarr clients over real HTTP to a loopback stub, kill switch below the guard raising on any mutating request that leaves it. Both trees, distinct executor fingerprints, summaries diff clean: 4 drift scenarios skip with zero mutating requests, 2 controls delete, 3 premise scenarios identical. **W9's `TYPE_CHECKING` kill is reversed by the owner** because the price it refused, a driven pass plus a C9 read, was already being paid by this PR; its measurement was right. `_DEFERRED_CROSS_PACKAGE_IMPORTS` 1 to **0**, and its written classification leaves with the site. `_EXPECTED_LAYERED_MODULES` (84), `_EXPECTED_SOURCE_MODULES` (116), `_KNOWN_IMPORT_CYCLES` (2) and the logger count (49) re-derived and unmoved |
| #678 | 8 | W9, the finding-body corrections | none, prose only | no | Three claims in W9 that a reader would act on. **The header's counts**: 108 modules is 116, and 514 edges is a number no walk in the repository reproduces, since an edge count depends on how `from package import name` resolves; the line now points at the gate that measures it instead. "Zero top-level cycles" holds, measured under all three conventions either side of #671. **The cycle arithmetic**: "8" is 9 and "6 of them" is 7, both re-measured at `80d8a39~1`; two cycles survive the move rather than one, the second being `api.plex → api.settings → launcher → main`, which phase 6 created after W9 was measured; and `services.list_config ↔ services.lists` is a tenth needing `TYPE_CHECKING` counted, not the ninth. **The `LAUNCHER_CONF_NAME` bullet** gets a landed note: the closure was 304 to 278, never 347, and "which owns uvicorn, the tray and AppKit" is wrong about the cost, all three being deferred inside `launcher.py` and none of them ever in that closure. The 26 that left are `reaper.launcher` plus 25 stdlib. Both frontend cycles were re-checked and both still stand exactly as written, `PosterFallback` included at 12 lines |
| #679 | 8 | W3, the two panel heads | `PanelHead`, `PanelHead.test.tsx` | no | One head, two panels, and **the row named the one character in the block that was never divergent**: the `↗` is `JumpPill`'s and both heads already shared it. Measured, the two differ by the item panel's inline `title-ext` SVG, which the show panel's title link lacks, and by the pill order, Tautulli/Seerr/Radarr/Sonarr against Sonarr/Tautulli/Seerr. The finding body is corrected in place. **Owner decision: unify both**, so the show panel gains the arrow and the order is the item panel's, which is the panel the queue opens most and therefore the pixel-for-pixel unchanged one. Mocked up and approved before any TSX moved. **The extraction is what makes the pin possible**: the two copies stayed wrong because every test read one panel, so all six assertions render BOTH and compare them, 5 of 6 driven red against the pre-extraction show panel (the sixth is the unmatched-title branch, which never diverged). Two source assertions carry the half a render cannot see, since a re-pasted head spelling itself the same way renders identically; they ban `why-head`, `title-link`, `title-ext` and `JumpPill` in `ShowPanel.tsx` and state their own bound (rule 147). Pill LABELS are deliberately outside that ban: "Sonarr" is an ordinary word in this panel's prose. **Rule 64 reached one place**: `index-outside-text.test.ts` named both files as rendering `why-head` and now names one. `ScalesPanel`'s `scales-ext` arrow is a third copy of the same path and is deferred in writing at `PanelHead`: a person is not a title, its link wraps the heading rather than sitting inside it, and it already carries the arrow, so nothing is diverged there. **S1 holds with no wire change at all**, `EXPECTED_INTERFACES` and `EXPECTED_PAIRS` re-verified against the tree at 94 and 92 |
| #686 | 8 | W3, the pragma row's rule 147 gap, named by #680's own docstring | `_BUSY_TIMEOUT_DECLARATIONS`, `_busy_timeout_prose`, `_BUSY_TIMEOUT_PROSE`'s second column | no | **The gate #680 landed could only see a passage spelling a number**, and it said so in its docstring: `snapshot.py`'s "far inside that budget" plus a figure-less mention in each of `backup.py` and `retention.py`. All three carry the figure now. **Two of them are not about `db.session`**, which is the whole difficulty: `retention._compact_sync` sets its own 30000 for `VACUUM` and `backup._build_into` its own 5000 for `VACUUM INTO`, equal to the app's by coincidence. Writing "5s" into backup's passage and checking it against `db.session` would tie two unrelated values together forever and read as a proof that they must move together, so `_BUSY_TIMEOUT_PROSE` becomes module → declaration → count, exactly as its own comment prescribed, and every expected figure is read out of the declaration the passage is about. A second walk pins the declaration set, so a fourth cannot arrive without a decision about which passages describe it (rule 145). **Population reconciled by hand: ten passages, seven files, three declarations** — seven quote `db/session.py`, two backup's own, one retention's own. **The matcher moved and both walks widened, each measured** (rule 147): comment markers come out before the whitespace collapse, because `backup.py`'s passage wrapped across two `#` lines read as "busy # timeout" and left that file a passage short, which any reformat can do to any of them; the gap is bounded to one sentence as well as 120 characters, because `scheduler`'s *measured* `8s` vacuum sits 86 characters past a `db.session` it is not about and was excluded only by match-consumption order; and the figure accepts the adjectival `5-second`, which no busy-timeout passage uses but 39 other durations in `src/` do, so it is the form a new one is likeliest to arrive in. Deleting `scheduler`'s real copy shows the sentence bound's worth: the window alone reports "quotes 8s", substituting an unrelated measurement, the bound reports "no longer quotes it". Accepted and rejected spellings are written above the pattern and were run. The word-form sweep ("five seconds") found none. **Both walks read `src/` and `alembic/`**, the pair the journal-mode gate 60 lines up already reads (rule 72): scoped to `src/` alone, a pragma in `alembic/env.py`'s connect hook passed both gates. **Twelve demonstrations driven, ten red and two green**, including the two that prove the second column: moving backup's declaration fails backup alone, moving retention's fails retention alone, and the seven `db.session` copies stay green through both. **The review caught four**, all fixed here: an accepted-spelling entry claiming a form the tree does not use, the `alembic/` scope, a "never a quiet pass" claim false when a spurious figure precedes a file's only anchor, and the figure-less class leaving the docstring while `retention.SWEEP_BATCH` still had it (reworded, so it is the seventh `db.session` copy) |
| #684 | 8 | none of phase 8's own rows; issue #654, a `dev` defect fixed in-phase | `clients.plex.SWEEP_MAX_PAGES`, `TestASweepThatNeverFinishesIsBounded` | no | **Not a phase 8 finding, so the Progress cell does not move.** `_iter_pages` was the one paged read of four with no page backstop, which rule 56/89 requires of any unbounded loop, and it is the helper that rule points every other windowed read at. The three siblings had one and all three agree on 1,000. **It RAISES, which is where the three differ**: `seerr.MAX_PAGES` raises, `history_sync.MAX_HISTORY_PAGES` stops short and warns, `library_index._SPINE_MAX_PAGES` stops short, warns and degrades. Seerr is the one to match, `_iter_pages` being complete-or-raise with every caller reading a protection source. The trip sits after the `start >= totalSize` exit so a listing that finished is never refused, pinned by a third test that fails when it is hoisted. **The cost is a scan-dead instance, not a slow scan**: the sweeps run under `asyncio.to_thread`, which cannot be canceled, and `scan_runner._scan_running` is cleared in a `finally` a loop that never returns never reaches, so every later scan is refused until the container restarts. **C9 driven either side against a fake pager**, with `_connect` bypassed and `HTTPAdapter.send` replaced by one that raises on any request: before, 10,001 pages asked, stopped only by the probe's own runaway stop; after, exactly 1,000 and a `PlexError`; an ordinary two-page listing unchanged on both trees; zero requests on the wire. **Settles #654**, re-framed off "a Plex that pages forever," which nobody demonstrated, onto the one paged read of four with no backstop, which is provable, and promoted to `Reviewed/Confirmed`. Its body carried a claim false about `dev`, that three of four windowed reads have a backstop where only `history_sync` does there, corrected in place. It stays open until this branch reaches `dev`. **The review pass caught the comment undercounting its own siblings** at two, `_SPINE_MAX_PAGES` being invisible from `dev`, and the American-English gate caught two British spellings the same pass had read past |
| #708 | 8 | W5-3, `gather`'s nine loose policy fields (built) | `season_scan.gather`, `season_evidence.SeasonPolicy`, `tests/test_season_scan._season_policy` | no, Tier A unmoved and Tier B unmovable | **The one candidate in the parameter-object family whose carrier already existed, so it is a frame of unpacking removed rather than an object invented.** `_judge_series`, the function `gather` calls next, took `SeasonPolicy` at #499 and `gather` was left holding the loose copy; the converted half's comment still read "as on `gather` above". `gather` 25 parameters to 17, its nine-line repack deleted, `snapshot.scan`'s nine keywords replaced by `SeasonPolicy.from_body(tv_policy)`. Two roads to one, so a tenth season field is written at three sites instead of six and the simulator cannot replay a value the scan planned without. **Three measurements contradict the plan.** Nine fields is exact, but **two were REQUIRED, not defaulted**, so the correction's carrier argument covers seven. **1 production call site against 18 in tests**, not `plan_series_prune`'s 2-against-87, and the production one passed all nine explicitly. And **"`gather` is the same" as `plan_series_prune` is wrong**: its three permissive `False` defaults are not `gather` parameters at all, and driven across each field's range on the base tree only `keep_specials` moved the prunable set, protectively. The row's real danger was the second road, not a permissive default. **Omission is impossible rather than unlikely**, which is what the class demanded: `SeasonPolicy` is a frozen slots dataclass declaring no default for any of the nine. Driven, the base tree ACCEPTED all seven omissions and this tree REFUSES all eight, seven fields plus the carrier, at mypy and at runtime. **C9 driven, 11 scenarios, both trees, `__file__` printed per run**: the nine settings swept one at a time, the shipped defaults, and Sonarr's `series()` raising below `gather`; 1,616 lines of guard outcomes, guard details, per-show prunable/protected splits and degrade reasons **byte-identical**, the only diff being the five header lines naming the tree. Seven of the nine discriminate on that fixture, including the widening case, `keep_last_scope=requested` dropping the keep-last floor and taking a show from two prunable seasons to three, identically either side; the pinning test covers the other two and only its docstring is edited. The 18 test sites take a `_season_policy(**edits)` helper built through the real `from_body` off `DEFAULT_TV_POLICY`, so the seven values a test does not vary come from the shipped declaration rather than from a second copy of it (rule 119) — `gather`'s seven keyword defaults were that copy. `TestNoSeasonSettingCanBeOmitted` is what fails when a default comes back, driven red three ways, each on its own assertion: the last field gaining a default, a field deleted from the carrier along with its `from_body` line (which the field count catches and the default walk cannot), and `gather` defaulting the whole carrier. **Three of the row's line citations were wrong and the correction that flagged two of them was wrong about both**: the comment stood at `season_evidence.py:131`, the correction's `:121` was a blank line, and neither `:121` nor `:130` appeared in the document; both sites anchored by symbol now. **The review found no defect in the refactor and five in the prose written around it**, which is the split worth recording: the two lanes independently agreed the nine values are carried un-transposed and all 18 test sites are unchanged, then caught that the docstrings credited the wrong assertion of the pinning test, that "all but two protective, so a caller omitting one widened" does not follow from this branch's own measurement, that the #499 attribution said "until" where #499 left `gather`'s signature alone, that `docs/DECISIONS.md` still named the deleted `in_progress_hold_days` default (rule 64, the one sweep miss), and that the new guard read `parameters["season_policy"]` where rule 141 wants `.get`. All six corrected here |
| #687 | 8 | W3c, the parameter-object paragraph (killed) | `_judge_item_lane_arguments`, `_LANE_ARGUMENTS`, `_EXPECTED_JUDGE_ITEM_CALLS` | no | **All six killed on measurement; the paragraph's own sharp case is the only real thing in it and a gate closes that.** Every parameter count in the row is exact and the fix is still wrong: `build_season_facts` assembles its 24 arguments from 18 locals a carrier would hold one frame up plus 6 per-season expressions, `_judge_item`'s two sites unpack off `RawItem` and `SeasonJudgment`, which are different types, `scan`'s 12 pass-through arguments arrive from four unrelated sources, and `plan_series_prune` has 2 production call sites against 87 in tests, each resting on the defaults the correction calls the protection. The Plex match record is 6 parameters through **3** signatures, not 4, and is killed for W5-2's reason: two of the six are identity-path join keys read off a `RawItem` at four addresses. **`gather`'s nine policy fields are W5-3's row and stay open.** The sharp case measured: cross `custom_condemn`, `keeps` and `policy` at the movie `_judge_item` call and the new gate is the only test in the whole suite that fails, so the keep rules a movie is judged against and its condemn threshold could both come from the TV policy with no reader anywhere; `gates`, `signals` and `window_days` are the three `test_scan_pipeline.py` already catches, measured one at a time. The gate rather than a carrier, per CLAUDE.md's "write the gate instead" and #655's tie-break, which also keeps the change off `src/` and out of S3 and C9. Driven red nine ways, including both sites pointed at one lane and one value computed inline. **An independent verification pass corrected five numbers and found two defects in the first draft of the gate**: the closing assertion compared a set of at most two prefixes against the site count, so it could never survive a third call site while the assertion above it told a future author to add one; and the walk read a bare `Name` only, so a qualified `snapshot._judge_item(...)` escaped both the walk and the population count, which is what rule 145's count exists to stop |
| #692 | 8 | none of phase 8's own rows; issue #682, a `dev` defect fixed in-phase | the empty-file-list arm of `Executor._send_season`'s size re-read | no | **Not a phase 8 finding, so the Progress cell does not move.** One guard served two facts, `not live_sizes or (approved_size is not None and any(s is None for s in live_sizes))`, and both got the size sentence, so a season Sonarr holds no files for was reported as a file with a missing size. `any([])` is False, so the old first operand IS the new first guard: all four input classes reach the same fate and only the copy moves. **The wording is the deliverable and the first draft was wrong**: `no longer lists any file` asserts a change, and an item the unmeasured allowance admitted has `size_bytes IS NULL`, so the scan never established a file existed and there was no earlier state to change. Shipped as `Sonarr lists no files for season N, so there is nothing to delete. Kept.`, true for both populations. **The checklist line is paired with the post-unmonitor skip's and the reasons deliberately are not**: that skip listed files a moment earlier so `no longer` is exact there, and the two check lines are now pinned to each other verbatim with each test naming the other (rule 144). `_CHECK_SIZE_UNCONFIRMED` is untouched, #683 having made it shared with the movie path one PR earlier, so rewording it would have silently reworded a movie skip that is correct; the constants' comment stops claiming four branches share the two constants, which is two each now. **Driven red against the base**, on a test that already had #682's exact setup and asserted only that the season survived, which is how the copy shipped wrong. Both arms and the outright deletion of the arm each go red on a named test. **C9 driven, #683's probe reused unchanged**, nine scenarios either side with the module sha printed: the two summaries differ in exactly four lines and all four are the two new sentences, states and wire byte-identical, zero mutating requests on both scenarios that reach the arm. **Settles #682**, which stays open until this branch reaches `dev`. **The safety lane found one `dev` sibling of the same class and could not drive it**, filed as a question at #691: `_send_for_real`'s hand-reap skip is one sentence over a two-arm guard whose arms describe two different instants |
| #690 | 8 | none of phase 8's own rows; a branch-created defect fixed in-phase | `_repo_text_files`, `test_the_repo_walk_never_reads_a_gitignored_file` | no | **Not a phase 8 finding, so the Progress cell does not move.** `_repo_text_files` walked with `REPO.rglob("*")`, which honors no ignore file, behind a hand-kept skip set of eight names plus a `.claude/worktrees` special case. `test_the_typecheck_gate_names_the_same_targets_everywhere_it_is_written`, which landed on this branch and is absent from `dev`, then counted a fifth spelling of the mypy invocation out of `.claude/review-findings/`, so every local run was red while CI, which has no scratch on disk, stayed green. **The walk asks git now rather than mirroring `.gitignore` by hand** (rule 103): `git ls-files -z --cached --others --exclude-standard` at `cwd=REPO`, which subsumes all eight skip names and the special case and deletes them. **Measured in one tree, 640 files against the skip set's 769, and nothing added**: the 129 dropped fall under four gitignored entries the set never named, `.claude/review-findings/`, `.hypothesis/` (125 of them), `.pytest_cache/` and `mutation-report-*.json`. **Two of the four are created by running this suite**, which is why the mirror could not stay current by anyone's diligence, and why a first pass at this row said three: it measured before the suite had run. `--others` is what keeps a file created but not yet staged, the state a gate is most useful in. 778 ms to 161 ms for the one cached call, and the file 44.00s to 11.45s in the main checkout. **The drift-guard alternative was rejected on the same measurement**: guarding a mirror needs a second hand-kept list saying which ignore entries the walk needs no skip for, which is larger than the mirror it guards. **Sibling sweep, 13 walks** (rule 72): every `rglob`/`glob` here resolved to a root, each root's population intersected with `git ls-files --others --ignored --exclude-standard`. Twelve are rooted at a tracked subtree and filter on a source extension, and all twelve collect zero; `REPO.rglob("*")` was the only exposure, its raw walk touching 1,835 gitignored files of which six survived the skip set. The seven walks in the other test files were checked the same way and are rooted at `src/reaper`, `alembic/versions` or a temp data dir, so 19 siblings, none exposed. **The zero is the measurement, not the reasoning**: `.gitignore` carries `build/`, `lib/`, `var/`, `instance/`, `local_settings.py` and `*.sage.py`, each of which could put a `.py` under a walked root, so the twelve are clean today rather than clean by construction. **The rule 145 pin is over a synthetic tree**, the real checkout's count moving with every file added: four files, two ignored, an expected set of two reconciled by hand, with `.gitignore` in it because it is untracked and not ignored, the half `--cached` alone would drop. Driven red against the old walk, `Extra items in the left set: 'noisy.log', 'scratch/handoff.md'`. **Every argument of the listing is pinned separately**, six mutations each driven red alone: dropping `--cached`, `--others`, `--exclude-standard` or `-z`, replacing the `surrogateescape` decode with a strict one, and unsetting `cwd`. The first draft staged nothing, so `--cached` could be deleted with the test still green while the real walk collapsed to untracked-only; the review caught it. **The git environment is scrubbed three ways, each closing a hole that was measured**: `GIT_DIR` and `GIT_WORK_TREE` beat `cwd=`, and an inherited one made the test pass while `git add` wrote into the outer repository's index; `GIT_CONFIG_GLOBAL`/`SYSTEM` stop an `init.templateDir` seeding `.git/info/exclude`; and `core.excludesFile` is pinned in-repo because its default, `$XDG_CONFIG_HOME/git/ignore`, is read with no config file at all. Eight hostile environments driven green with no foreign index touched. **One consumer was measured as vacuous on an empty walk** and left alone: `test_the_vite_dev_server_refuses_a_taken_port` reads its file directly and iterates an empty ban, where the other seven go red. It is on `dev`, unchanged here, and now unreachable, since an empty listing raises out of git rather than returning nothing. **The relative-versus-absolute incident survives in its new shape**: `git ls-files` prints paths relative to the process's own directory, so running it anywhere but `REPO` makes every join name a file that does not exist and the walk comes back empty and green. **Two prose copies corrected rather than left behind** (rule 7/24): rule 145's tail prescribed the skip-set idiom this deletes and points at the function now, and `conftest.py`'s network-guard docstring said the one test that shells out launches nothing, where this walk launches `git` on every run. **The citation sweep, and what it found** (rule 72): this diff's `import subprocess` shifted every line number in `test_repo_hygiene.py` by one, so the four `path:NNN` cites into it are re-anchored by symbol. Only the `conftest.py:225` one was stale at the base; `test_repo_hygiene.py:442` was exact, and this diff is what broke it. Four bare intra-test line numbers in the same W1.5 block are **deferred in writing**: `:3116`, `:3110`, `:3005` and `:2230` name lines inside a test rather than a symbol, and each was already off by 513 to 671 lines at the base, so correcting them is a re-derivation of a landed analysis rather than this diff's shift. **One claim the re-anchoring exposed is corrected instead of carried**: the `_hermetic` paragraph still instructed a reader to fix a rule 7/24 violation that the socket guard already fixed, and the docstring it names now says the opposite of what the paragraph claims. No `src/` change, so no counter moves |
| #698 | 8 | W3b-2 (killed); W11-32 | `scheduler._refresh_curated_lists`, `test_an_unreadable_list_registry_records_not_ok` | no | **The decorator is killed and the row's own prose complaint is what lands.** Every count in W3b-2 is off: `_record_run` has 15 call sites and not 17, measured by AST; five of the seven jobs record on failure and not four; and a decorator fits four of them, since two record nothing by design and `scheduled_scan`'s two quiet skips must stay unrecorded. The kill block carries the rest. **What was left is W11-32, in the same function**: two inner handlers spelling the caller's catch-all back at it, `scheduler.lists_refresh_failed` / `ok=False` / "Couldn't refresh lists" in all three places, each ending in a `return` from inside the `AsyncExitStack`. 14 lines out, W11-32 said ~12. **Behavior-preserving, measured rather than argued**: neither `BaseClient.__aexit__` nor `PlexClient.__aexit__` returns anything, so no client entered by `build_sources` can suppress the raise on the way out, and `ScanConfigError` and `ListRegistryUnreadableError` are both `RuntimeError`. `test_a_pass_that_cannot_reach_its_sources_records_not_ok` already drove the first branch and stays green across the deletion, which is the proof for that half. **That arm was unreachable in production besides**: `build_sources` raises `ScanConfigError` at exactly one place, behind `require_scan_sources`, and this caller passes it `False`, so only a monkeypatched test ever entered it. **The second branch had no test at all**, which is why one lands: an unreadable `ListConfig` body drives the real `definitions(session, strict=True)` raise, and it asserts the pass never reached `sync_protection_lists` besides the row. Driven red two ways, by dropping `strict` and by narrowing the outer catch-all. **Six mutations in all, and one of them stayed green**: the seeded row's `source` was copied from `_seed_plex_list`, where it is load-bearing, and here it is not, so `"plex_collection"` told a reader the Plex arm was involved when the raise is at `json.loads` one line before a `ListSource` is ever constructed. Now `"imdb"`, with the reason in place. **One comment on `dev` is corrected in the same diff** rather than filed, because this test is now the only cover for the branch whose handler came out: `test_a_pass_that_cannot_reach_its_sources_records_not_ok` said "Its own stub, not `_wire_lists`" directly above the line that calls `_wire_lists`, whose own stub the test then replaces (rule 132's shape). **The docstring is the deliverable.** "Every exit records a run" was a rule 7/24 promise a reader had to take on trust and is now a shape: one call under one catch-all, stated with the one surviving handler (Plex unreachable) named, since that one changes the result string rather than ending the pass. The four raise routes it enumerated (`adopt_legacy`, `sync_rule_names`, `retire_absent`, `gather_reaped`) are a hand-kept list of another module's internals and are cut here; the copy in the test that exercises that class of failure survives, and all four were still accurate (rule 144). `after_scan`'s "Every skip below is written down" was contradicted by the comment three lines under it and now names the exception. **The first draft of the new docstring was itself a rule 7/24 violation and the safety lane caught it**: "Nothing here records a failure" reads as true beside two deleted handlers and is false of the function, whose last line writes `ok=False` on three outcome branches the caller's catch-all never sees. A reader trusting the headline could move that line and leave the Jobs row green after a total per-list failure. No `src/` behavior change, so no counter moves and `STATUS.md` is untouched |
| #699 | 8 | W9-5 and W9-6, both killed | none, prose only | no | **Both wave 9 leftovers killed on the same shape of measurement: the edge each row removes is paid by nobody.** W9-5's correction already said the transitive path survives, and this is that claim as numbers, one module at a time and under all four graph conventions: all 13 that import `IntegrationError` alone still reach `clients/base.py` through something they import for real work, so cutting the direct edge takes **zero** modules off **every** one of their closures. **`api/scan.py` is the sharpest case and the row calls it the clean one**: it is the only one of the 13 importing no client at all, so `clients/base.py` is its whole client edge, and it reaches base through `services.scan_runner` and `services.snapshot` regardless. **Three of the row's figures are wrong**: the population is 13 and not 12, 23 modules name the symbol, and the closure is 339 with `sys.modules` at 387, so the row's 384 is likelier a stale total than a wrong delta. The word wrong either way is httpx, pydantic's family being 74 of the 145 non-stdlib modules against httpx2's 24. W9-6 is the opposite measurement reaching the same verdict: the graph **does** move, `api.backup`'s closure going 73 to 38, and that figure has no consumer, because all three importers of `api/backup.py` import `api/runs.py` in the same breath and `reaper.main` loads 931 either way. **Every write of `app.state.reap_status` is in `api/runs.py`**, one assignment at `:468` and 34 field mutations across ten regions from `:549` to `:762`, so every destination splits the reader off: `api/deps.py` costs no module and is the worst, separating `reap_in_flight` from `_reap_status`, the same `getattr` written to create rather than to refuse; a new `api/reap_state.py` keeps that pair together for +1 on both counters and still leaves the mutations behind. **The correction's own "created fresh by `create_app`" is wrong** and is fixed in place: nothing in `main.py` touches `reap_status`, `_reap_status` makes one lazily, and the conclusion survives anyway. **"The only `api → api` edge" is five** (`backup → runs`, `runs:32 → scan`, `plex:54 → settings`, `simulate:30 → policy`, `simulate:31 → review`), the correction having found the second and stopped, and `auth` in the row's exempt set has had no importer under `src/reaper/api/` since W9-2. The layering gate sees none of the five, walking cross-*package* edges only. **Both of W9-6's anchors were stale and are re-anchored**, `:422` to `:472` and `test_scheduler.py:356` to `:357`. No `src/` change, so no counter moves: a kill is what keeps `_EXPECTED_LAYERED_MODULES` at 84 and `_EXPECTED_SOURCE_MODULES` at 116, since building either row would have moved both by one |
| #705 | 8 | W3b-12, W9-4 | `navIntent.Focus`, `AppFocus.test.tsx`, `components/PosterFallback.tsx`, `_frontend_import_graph` | no | **Both built, and both rows understated their own subject.** W3b-12's comment says a fourth focus belongs in "the same three places"; it is **five** (declared, `clearFocus`, the clearing effect, `goTo`, handed to the view), and the two it omits are the two *clearing* sites, which are the half a new slot's author forgets and the half both recorded incidents came from. One `Focus` union keyed on `view` retires them: the effect is `setFocus(f => (f?.view === view ? f : null))`, which names no view, and each view reads the arm that names it. **The payload types stay discriminated at no cost** — `tsc --noEmit` green under `exactOptionalPropertyTypes` and `verbatimModuleSyntax`, and neither consuming view's prop type changes except `ReviewQueue` spelling its own arm inline, which is `PolicyEditor`'s existing spelling. **`clearFocus()` is deleted from the section nav as provably redundant**: at rest the effect leaves `focus` null or matching `view`, so a tab click to a different view has nothing the effect would not also drop, and a click on the tab you are on runs neither (B-23, which the effect's `[view]` dep already carried). **The first test drive was wrong and is worth recording**: leaving Policy by another jump does not discriminate, because a jump overwrites the old aim, so the page reads unaimed whether or not anything drops it. The walk is jump in, leave by the section nav, return by Back, and each arm is driven red on its own. W9-4's two cycles are exactly as written, measured at the base: 202 modules, two cycles, both two-module, and the **same two with type-only edges counted**, so nothing was hiding behind an `import type`. **The poster fallback is two copies, not one component** — #678's re-check reached the twelve lines and not `ReviewQueue.tsx:257`, which held a hand-copied second drawing of the same paths at 20px against the panel's 18. The leaf takes a size and all three callers use it, so no pixel moves. **Nothing enforced frontend cycles**: no `eslint-plugin-import` in `frontend/package.json`, and the hygiene walk reads `src/reaper` only, so the gate is the durable half. It reuses `_import_cycles`, declares an empty set rather than two, pins the population (rule 145) and drives both regexes against the seven forms the tree spells (named, the same over several lines, default, side-effect, `export … from`, an inline `type` inside a value import, `await import()`), three that must resolve to nothing (`import type`, `export type`, `typeof import()` at three spacings), and three decoys a looser matcher reads as imports (a `from:` key, a quotation inside a template literal, a commented-out import); driven red by re-adding a cycle. **The review lanes found six holes in that gate and every one was in the first draft of these two regexes**, which is the row's own lesson arriving on it: `import(/* @vite-ignore */ …)`, a backtick specifier and `typeof` at any spacing but one space were all read as absent-or-real backwards, each of them fail-OPEN, which loses a cycle rather than inventing one; the resolver stripped the last dotted segment off a bare specifier, so `./dissolve.generated` landed on `brand/dissolve` and the generated module had **no incoming edges at all**; the barrel candidate skipped the containment guard, so one `import … from "../../package.json"` would have raised out of the walk; and the pinned count counts KEYS, so a `.ts` beside a `.tsx` of the same stem would have been green with one module unparsed. All six fixed here, each with its case in the rule 147 table. Counter: two more findings landed and two off the open list, W3b-12 and W9-4; against the cell as this branch merged it, 17 landed and 10 open become **19** and **8**. Filed #704, 20 frontend tests that render a failed read and pass because `src/test/setup.ts` gates one spelling of a mock gap and not the other |
| #706 | 8 | W5-1 (killed); C9 | `test_engine_derivations.TestTheStoredExplanationIsWrittenAsItIsDeclared` | no | **The pinning test the wave's caveat asks for lands, and the collapse it was written to unblock is killed by it.** Nothing asserted a fired entry's key set: the baseline fixture reads nine of the thirteen top-level keys and four fields of a signal row, so a key added on one side alone was invisible in both directions. **No live drift, measured**: the writer's 13 top-level keys, `match`'s 6, a signal row's 8, a keep's 5 and a `protections_unknown` entry's 4 are exactly what the models declare, `protections_fired`/`protections_checked` carry `{gate, detail}`, and every raw `.get` on a stored explanation in `src/` reads a declared key. **The collapse, built as the row asks, drops the whole match block**: `Explanation(match=MatchOut(...))` yields `match=None` on `dev` today, because `_thaw_match` is `mode="before"` and reads a submodel instance as "not a mapping". Nothing raises, and `extra="forbid"` does not see it, being about unknown keys. The general reason is that the read model's three `mode="before"` validators exist so an illegible STORED byte degrades one field rather than blanking the panel, and on the write side the same leniency normalizes a writer's own value to `None`: `thaw_threshold(70.5)` and `thaw_threshold(True)` are both `None`. One model cannot be lenient for a reader of old rows and strict for the writer, and the lenient half is the one that reaches disk. Two silent widenings ride along, `score` and `keeps[].max_discount` both int to float, and `_server_models`' pinned count moves 140 to 141 across three tests, `engine.explanation` being an INNER module of that walk, which is the correction's "already the wire model" measured. **The suite catches one of it and it is the cosmetic one**: against the collapse, 4,162 pass and the only pre-existing failure is `test_signal_state`'s whole-number score assertion on `52` to `52.0`. Zero pre-existing tests see the dropped match block; the four that do are the four written first. Both runs were against copies of `src/` under `/tmp`, so the running-from-`/tmp` artifacts appear on each side and cancel. **C9 driven, 31 observations in four groups either side**, 8 over `_equivalent_keys`, 5 driving the streaming veto, 6 driving the played-since-approval check across the movie and season query shapes, 12 over `reap_override_verdict_decoded`, each run printing the four modules it loaded. This branch against its base diffs clean but for those fingerprints. The collapse moves eight lines, six of them an outcome and the other two the query shape. **Three interlocks fail open**: the merged bind's key set goes `[4242, 4243]` to `[4242]`, so the veto stops seeing a stream on the second listing and the history check stops querying it at all (2 Tautulli calls to 1, on the movie and the season shape alike), and a bad Plex match goes `protect` to `condemn`, `condemned.bad_match` reading an absent match block as genuinely absent. Two nets below the interlocks, every gateway write raising and `socket.connect` raising; neither fired on any run, and no database or stored token was opened. **The walk derives its blocks off `Explanation`** rather than listing them, so a nested block added there enters without anyone extending the test (rule 145), with the block LABELS pinned beside it and the `protections_unknown`-only asymmetry a named exception. Driven red six ways. **The safety lane found four defects in the first draft and all four are fixed here.** Two are the same class one level apart: the annotation walk read one level, so a block spelled `list[X] | None` left it silently, which is the natural spelling for one added to a document that must still read old rows (rule 147); and the population was pinned as a TOTAL, which a redistribution across the three protection lists preserves while emptying one, making the flags assertion true over nothing. The round-trip read `merged_rating_keys` through the model's lax `list[int] | None`, where a stored `["4242"]` reads back as `[4242]` and `_equivalent_keys`' raw `isinstance(value, int)` filter comes back empty, so the raw list is asserted too. And the class docstring's two figures for the baseline fixture were both low, corrected in both copies of the sentence (rule 144). `explanation.py`'s claim that the two flags are never written on the other two lists had no test and now has one (rule 7/24). Two source comments name the gate and its failure message names both files (rule 144). **The seam lane then found four more, three of them in this PR's own prose** (rule 144 arriving on the writing rather than the code): the kill block still said "four ways" against this row's "six" and had not been re-swept after the safety fixes landed, the collapse suite figure read as one failure with ~95 tests unexplained until the `/tmp` caveat was carried across, and "all three copies" of the corrected figure was two. The fourth is in the test, a second hand copy of `_LISTS_WITHOUT_THE_GATE_FLAGS` inside the assertion that proves the asymmetry, so a third flag-free list would be added to the constant and missed by its own test. It also enumerated all 12 `_json` columns for rule 72: `payload_json` (`season_evidence`) has an import-time drift guard like `facts_codec`'s, `settings_json` round-trips one declaration, and the remaining eight are hand-written on both sides with no second declaration to drift from. `src/` changes are docstring lines only, so Tier A and Tier B cannot move and no counter does |
| #703 | 8 | W3b-9 (killed) | `tests/test_app_settings_precedence.py`, `_env_seeded_getters` | no | **Killed on measurement; a gate lands instead, and three of the seven sites were unpinned.** The `> Killed:` block sits on the finding body. **7 is right and 3 spellings is not, the tree holds 5**: the population is derivable, a function that calls `_get` and takes a `Settings`-annotated parameter, exactly seven and nothing else, with 16 seedless readers beside them and `runtime_safety` taking a seed and delegating. Only three share a spelling. A helper serves those three and buys 0 to 2 lines depending on how the call sites wrap, so the line count decides nothing (S5); it also binds only the getters that call it, where the exposure is the eighth getter nobody has written. **The W4.1 collision is one call site, not three**: its `SETTINGS` table carries an optional env field, but the same sentence keeps `destructive_enabled` and the encrypted credentials as declared exceptions and `leaving_soon_unarmed` is not a `_general_out` field, so `proxy_trust_enabled` is the whole overlap. That is a reason to prefer the gate rather than W3b-4's collision, and the block says so. **Each site's precedence was mutated and the whole suite run against it.** Four went red, each on exactly one test in a different file: `destructive_enabled` at `test_startup_log.py`, `proxy_trust_enabled` at `test_general_and_logs.py`, `leaving_soon_unarmed` at `test_leaving_soon_settings.py`, `get_timezone` at `test_timezone_setting.py`. **Three went green across 4,252 tests.** `get_trusted_proxies` reverting a stored empty list to the seed, which rule 1 forbids and its own docstring already claimed: `GeneralSettingsIn` stores `[]` as itself and omits `None`, so clearing the list is how an operator says trust nobody, and reverting leaves a forwarded header from a proxy they removed deciding auth (rule 101). `get_discord_webhook` letting a stale env var clobber a URL edited in the UI. `has_discord_webhook` reporting connected for a credential written under a rotated key, the one case its docstring says must read as absent. **All seven mutations now go red on their own named case**, and both arms of the walk go red against an eighth getter, the count pin first and the missing-case assert after it. `has_discord_webhook`'s two orders are indistinguishable with a readable stored value and a seed both present, so its case drives the rotated-key state instead and says so in its own docstring, which is rule 118's clause for arms a function's interface cannot tell apart, even though a UI presence probe is not one of the deletion-path interlocks that rule binds. The driven set is read off this file's own AST rather than listed by hand, so a hand-kept list cannot go stale unnoticed (rule 132); it reads a reference and not an assertion, and its docstring says so. **The review lane found one defect in the walk and it is fixed here**: `ast.parse(source).body` read top-level statements only, so an eighth getter nested in a class or a `try:` would leave the population at seven and the gate green, which is the hole a count cannot cover (rule 147). `ast.walk` now, driven red against a nested getter. **The log-level half is a misreading and moving it nets negative**: `get_log_level_setting` exists and `main.py:265` already goes through it, what sits in `main.py` is the apply, and the level is process-global state rather than a value. `configure_logging` sets it from the environment at `create_app`, before `lifespan` builds the session factory, so the two sources are a sequence and a getter could only express the second half, while erasing the provenance `log_level_from` reads off the same value (rule 76). **Filed while measuring it, #700**: `REAPER_LOG_LEVEL=ERROR` validates and silently resolves to INFO, `logbuffer.LEVELS` omitting ERROR while `config.py`'s `Literal` and the Unraid template both offer it. On `dev`. No `src/` change, so no counter moves |
| #701 | 8 | W3b-6 | `plex_link.start_pin`, `PinPurpose`, `PIN_TTL`, `PinStart`, `TestThePinPurposeFence`, `test_a_pending_plex_pin_is_written_in_exactly_one_place` | no | **The start half merges, the poll half does not, and only the start half is what the row's anchors named.** Both were stale before anything moved: `start_plex_login` was at `login.py:113` for a cited `:115`, `start_link` at `plex_link.py:394` for a cited `:395`. **The four-token claim holds for the start pair**, and three of the four positions carry the same name twice or the same value twice: `PlexLoginStart` and `LinkStart` were structurally identical frozen dataclasses, `PLEX_LOGIN_TTL` and `LINK_TTL` were both `timedelta(minutes=10)`. `purpose` is the one thing that differed, and it is now a `Literal["login", "link"]` argument to one `start_pin`, placed beside `client_identifier` because that is the shared plex.tv primitive `login.py` already imported from that module. **The poll pair is 63% divergent, 123 changed lines over 194**, and diverges exactly where W6-3's kill points: one mints a session and branches on whether a server is already linked, authorizing against that machine id when one is and running first-run setup through `complete_link` when none is; one consumes the pending row on each refusal arm, the other in a `finally`. A note above each says so rather than leaving the next reader to re-derive it. **The review refuted that note's first draft**, which said the sign-in poller "never stores the token": its setup branch reaches `complete_link`, which persists one, so the claim was false on exactly the path a first-run operator takes and false in the reassuring direction (rule 144). It had been copied into two more places before it was caught, which is the rule's own point. **The dedup is not why the PR was worth opening.** `purpose` was unobserved: dropping `PendingPlexLogin.purpose == "login"` from one poller and `== "link"` from the other left all 137 tests in the three covering files green, separately and together, because the two halves were wrong in agreement and no test could see either. That value is the fence between an unauthenticated route and an admin-only one, so an admin's in-flight re-link must not be redeemable for a session cookie at `/api/auth/plex/poll`. Four tests pin it, each driven red alone against the mutation it catches, both purposes swept so a hardcoded either fails on the other (rule 141), and `forward_url` swept off its `None` default in the same call. **A hygiene gate holds `PendingPlexLogin` to one construction site**, which is the only durable thing the merge buys: a third flow inherits the expiry sweep and cannot omit a purpose, where a docstring asking for that binds nobody. Its walk reads the bare name and the `models.PendingPlexLogin(...)` attribute form and is proven against the spelling the tree does not use, since that is the one a second site arrives in (rule 147). **`~65 lines` sizes the region, not the saving**: the region is 72 and `src/` moves 869 to 854, a net 15, the merge trading duplicated code for the note explaining the half that stays. **The row's rule note is half wrong and harmlessly so**: 11/98 does sit above the seam at the two routes, but rule 125 sits *inside* the pollers and is untouched only because they did not move. **`db/models.py`'s comment on the column was wrong in both halves**, reading `# "setup" | "login"` where the values are `"login"` and `"link"` and no `"setup"` purpose exists anywhere; wrong on `dev` too, and corrected here rather than filed because it sits on the discriminator this PR parameterizes. **Three review lanes, and the two findings with teeth were both in the PR's own new work.** The sweep test could not tell its `WHERE expires_at <= now` from an unconditional `delete(PendingPlexLogin)`: that mutation was green across all **4,259** tests, and it wipes a live PIN, so an admin with a re-link in flight who opens the sign-in route in another tab loses it. Driving both states rather than only the expired one is rule 145's "says nothing about the STATE each member was driven in". And the hygiene walk matched the class's own spelling, which `from reaper.db.models import PendingPlexLogin as Pending` walks straight past, an idiom already in the tree at `services/list_rules.py`; it resolves local names from each file's `ImportFrom` now and reads the Core `insert()` form too, with `getattr` written down as out of reach rather than implied covered (rule 147). **`purpose` was also unpinned one level up**: the merge moved both literals out to the routes, and flipping `api/auth.py`'s to `"link"` was green across 183 tests, so `TestEachStartRouteClaimsItsOwnPurpose` now sweeps both routes where `TestBothStartRoutesForwardTheWindowHome` already sweeps the same pair for `forward_url`. **`PIN_TTL`'s comment cited the wrong constant, on `dev` and more confidently here**: `PlexTvClient.PIN_TIMEOUT` governs `wait_for_pin`, whose only caller is the CLI `link`, and that path writes no pending row at all. The window a row must outlive is `DEADLINE_MS` in `PlexPin.tsx`, the poll hook all three browser flows drive, so a second gate holds that cross-language pair (rule 131) |
| #719 | 8 | W3b-11 (killed) | `DiscordModal`, `NotificationsPanel`, `ServicesPanel`, `SetupPasswordStep`, `SecurityPanel`, `test_a_held_test_result_is_stamped_when_its_request_is_issued` | no | **All four extractions killed on S5, and the two defects the duplication was hiding land instead.** The `> Killed:` block sits on the finding body with the arithmetic per sub-item, in non-blank lines throughout. **Two of the four counts are wrong.** The image-fallback ladder is two ladders at four sites: `Backdrop` and `WhyHero` share one, `ReviewQueue`'s `Poster` and `ScalesPanel`'s share another, and the row names both members of the first and misses the second site of the other. The test-result pairing is four sites, not three. The five and the two both hold. **The dirty-report five is the sub-item worth the least**: 20 lines out against 29 back, and what rule 146 asks is per-site, so a hook cannot carry it. All five satisfy it already and what they say differs, two naming the branches their report survives, two saying they have no early return, and `PlexPanel`'s three sitting below it. **Three of the four test-result surfaces computed the fingerprint at settle time**, where the boxes have moved: on the two webhook surfaces the box is never disabled during the send, so pasting a second webhook while the first is being tested leaves "Passed" beside a channel nobody sent to, and the stored `of` matches the live one by construction. All three are on `dev` (`Settings.tsx:1155` and `:2427` before the panel split, `DiscordModal.tsx:63`). **`ServiceModal` had it right and was unpinned**: its three badge tests edit the boxes with nothing in flight, which the fingerprint read at either end satisfies. A fourth drives the retype during the request and asserts the badge returns for the tested address, so a result never stored fails it too. A hygiene gate pins the four surfaces and allows an `of:` key only a name. **Its first two drafts were both fail-open, in the PR's own new work**, and the review lane demonstrated the second: a hunt for a call read `of: [kind, baseUrl()].join(" ")` as innocent, and the bracket walk that fixed that still passed the same fingerprint inlined as a template literal, which is the defect with no call in it. The check is an allowlist now, and the population is the `of:` keys it scans rather than the helper's name, so a fifth surface arrives whatever it calls its fingerprint (rules 145, 147). **The wizard's password step announced its live complaint on every keystroke**, `{pw.length} so far` inside a `role="alert"` region on the form that sets the key arming deletion, where `SecurityPanel`'s copy has been `standing` since #394 (rule 72). Its boxes also gain the sibling's `aria-invalid`, and `SetupPasswordStep.test.tsx` drives both arms of each plus the axe audit the step never had. `_EXPECTED_STANDING` 35 to 36, `_EXPECTED_RENDERING_TEST_FILES` 56 to 57, `_EXPECTED_FRONTEND_MODULES` 204 to 205. **`SecurityPanel`'s placeholder was the one ungenerated copy of the length floor**, `at least 12 characters` as a literal under a declaration whose own comment names the placeholder as derived (rule 7/24, rule 144). It is the one fix here with no test: 12 is what the constant holds, so a test reading the placeholder passes either way (rule 141). No `src/` change, so no counter moves and `STATUS.md` is untouched. Filed #718, the policy editor's two repair-notice renders disagree about `standing` while the comment over one says they do not. On `dev` |
| #720 | 8 | W3b-8 (killed); W3b-10′ | `test_every_client_carries_the_operators_own_tls_setting`, `_client_construction_sites`, `restore._PREPARE_FAILED`, `TestAPrepareStepThatFails` | no | **Two rows, one kill and one string lift, and both figures were off.** **W3b-8**: six `PlexClient` constructions is right, re-derived by AST here and at the audit's base commit, but they span five modules and only one sits in the file the anchor names. Four of the six pass the same four arguments in the same order, and each is six physical lines collapsing to one: 20 out against about 14 for the helper, a net of about six (S5); it reaches neither `scan_runner` site, both of which build outside the session block immediately before the close is registered, so moving construction inside re-opens the leak rule 34 already closed at `_plex_client`'s call sites. The row's "reads differently in two" is five branches over six sites, each that surface's own operator answer, the two scan sites alone agreeing. **The row is also right that there is no hole**, `safety` being keyword-only and required and `verify` defaulting to the strict `True`, and that is what shapes the gate: the omission costs agreement, not safety, so an operator with a self-signed certificate would get one surface that cannot reach their server. The gate binds all six plus the fifteen sibling constructions of `TautulliClient`, `SeerrClient`, `_ProbeClient` and the two `*arr` (rule 72), twenty-one pinned, reusing the `*arr` gate's walk rather than a second one. Driven red four ways. **The class list is the walk's real bound and no count covers it** (rule 145), so the four other classes declaring `verify` are excluded in writing; the review lane found `_ProbeClient` missing from the first draft, which is that hole arriving on its own gate. `PlexTvClient` is out of the population, with the reason written on the constant: no `verify` parameter, because plex.tv is not an address the operator configured. **W3b-10′**: the pragma row says two blocks share the operator string; there are **four** raise sites in three functions and one is not a sqlite block at all, `_force_recovery_off`'s `OSError` on the staged `launcher.conf`. One declaration now. **The string was half a sentence**: it said what failed and not what it meant for the install, and "Nothing was restored." is checkable rather than reassuring, since `arm` writes READY last and `apply_pending_restore` returns on its absence (rules 21, 126). **It was nearly true and the review lane caught the gap**: neither of `arm`'s checks rejects an arm over a staging that is already armed, so a retried confirm ran the steps again with READY on disk and a raise left the swap armed. `arm` clears READY first now, so a failed re-arm disarms, the keep direction. On `dev` too, fixed here because this row's own sentence is what asserts it. It matches `api/backup.py`'s existing "That password didn't match. Nothing was restored." **All three arms were unreached by the whole suite** behind 93% module coverage, and no test anywhere asserted the sentence; four do now, each also asserting the staging stays unarmed, uncovered statements 20 to 14. Seven mutations driven red, six on their own named case and the seventh, the READY write moved ahead of the prepare steps, on all three, and a per-table commit in the purge, which the third test could not see until the review lane moved its trigger onto the LAST auth table. The fourth raise site is the `_TABLE_NAME` guard, `pragma: no cover`, named as out of reach rather than covered (rule 147). The only behavior changes are the string and the READY clear, and no `STATUS.md` line describes either, so it is untouched |
| #725 | 8 | W3b-9 (rebuilt); W3b-2 (partial costed) | `app_settings._env_seeded_switch`, `app_settings._decrypted_or_absent` | no | **A row killed on arithmetic, re-asked as "what shape would work".** The kill measured one helper that swallows the `await _get(...)` call and priced it at 0 to 2 lines. **A helper taking the value `_get` already returned costs neither the line nor the gate.** `_env_seeded_switch(stored, seed)` is pure, synchronous and typed `-> bool`, holding "a stored `false` is a choice, not nothing stored" for `destructive_enabled`, `proxy_trust_enabled` and `leaving_soon_unarmed`, each call site landing at 76 to 79 columns where the swallowing version put one at 104. `_decrypted_or_absent(box, stored)` holds "a credential that will not decrypt reads as absent" and reaches **three** sites rather than the pair the row counted: both Discord getters and `get_api_key`, which takes no seed and so was never in the population. **+13 total lines, -9 code lines**, the difference being 18 docstring lines and six blanks, and one sentence deleted from `proxy_trust_enabled`'s for saying what the helper now says. **The first shape would have blinded the gate that killed the row.** `_env_seeded_getters` collects a function that calls `_get` and takes a `Settings`, so a helper standing between them takes all three switches off the walk and leaves the count at four with the gate green over three uncovered sites. Every getter still calls `_get` itself, so the population stays seven and `test_every_env_seeded_getter_is_driven_above` is untouched. That file's two docstrings said a helper could not exist and now say what the tree holds: the helper gives an author the right thing to call, the gate makes them look, and reading the two as alternatives is what killed this row. **Driven red**: `stored is None` to `not stored` fails three named cases at once, one mutation where the same defect needed three before; returning the raw ciphertext from `_decrypted_or_absent` fails the rotated-key case. Full suite 4,290 passed at the tip. **One measured negative**: `get_api_key`'s own decrypt-failure path is pinned by nothing of its own and stayed green under the mutation across 296 tests in four files, so it is covered transitively now, by the one Discord case that drives the shared declaration. **Writing the rule down is what found the site that broke it.** The helper's docstring says every caller must agree that an undecryptable credential is absent; the correctness lane checked that against the tree and found `GeneralSettingsOut.api_key_set` computing presence from row existence, so a key under a rotated secret reported as set while the reveal route 404s and the header lane refuses it (rule 76). On `dev`, fixed here rather than filed because the claim is this branch's, with one test driven red alone. **The sweep found no second site**: instance credentials let every `decrypt` raise, so `InstanceView.has_key` reading `bool(row.api_key_enc)` agrees with its own runtime. **W3b-2's partial is costed in the same pass and loses**, at +21 total lines built for real; the kill row and the finding body carry the number. The one behavior change is the flag, and no `STATUS.md` line describes it, so it is untouched |
| #717 | 8 | W11-15, W11-12; W11-39 (killed) | `fairness._TITLE_LOOKUP_CONCURRENCY`, `scheduler._maintenance_specs`, `TestEnrichTitles` | no | **The one defect in the batch is W11-15, and its row's justification is wrong twice over.** `ConcurrencyGate` has three production callers, not none, and it is a load-shedder rather than a bound, so it was never the tool for this. The measured block's own `four` counted the singleton and is corrected here (rule 144). The fan-out is real: `_TITLE_LOOKUP_CAP = 80` bounds the work per Scales load and nothing bounded the burst, httpx2's default pool of 100 sitting above the cap, so one page load could open 80 sockets to a single portal. `asyncio.Semaphore(8)`, the figure and the reasoning copied from `season_scan.RESOLVE_CONCURRENCY`. Driven red at 24 in flight against a bound of 8, with the same test asserting every row still gets its name so a bound that dropped lookups would fail too. **The bound lengthened the tail, so the deadline ships with it**: one stalled wave of 80 became ten of 8 against a page with no deadline of its own, found by this branch's own correctness review and fixed here rather than filed. The row's `~20` is a **+31** in `fairness.py` (+37/-6), of which +10 is the bound: bounding a fan-out adds lines. **W11-12 is five sites and -14 for the parameter, -10 landed.** Every production call site passed `settings.data_dir` beside the same `settings`, `api/settings.py`'s three through two calls to `runtime_settings(request)`, which returns one object. Net landed is -10, the difference being four docstring lines saying why the folder is derived. **One site could pass a divergent folder and it was a rule 141 test**, deliberately handing `build_scheduler` a folder that was not the engine's to prove the job read the argument. It asserts against `settings.database_path.parent` now, driven red against a `Path("data")` wiring. Keeping the parameter on `build_scheduler` alone counts -13 off the same diff and was rejected: it splits the ratings download and the snapshot sweep onto two sources for one folder, which is the hazard the row closes. **W11-39 is killed on a built measurement of +5.** The `> Killed:` block sits on the finding body. Two of its four sites are adjacent pairs; the other two sit 40 and about 150 lines apart, so collapsing them moves a read rather than removing one, and one of those is `reap_breakdown`, the ledger beside the destructive button |
| #723 | 8 | W11-3 (test and type; its table killed), W11-22, W11-24 | `api.FieldType`, `test_the_browser_knows_every_field_type_the_vocabulary_serves`, `backnav.useBackCloseMirror`, `WhyShell.PanelFallback` | no | **Three frontend wave-11 rows, and the lines were the deliverable in none of them.** **W11-3 re-measured: 30 dispatch sites, not six**, all in `PolicyRuleEditors.tsx`, counted two spellings (`.type ===` and `.type !==`, 29 lines carrying 30 comparisons). **The table is killed.** The four ladders are four different dispatches on one discriminator, not one copied four times: `rampDefaults` returns a pair, `coerceValue` does arithmetic, `describeCondition` formats and clears a unit, and the ramp bound picks between two *components*. A `Record<FieldType, …>` covering all four is longer than what it replaces, and a `satisfies` on the one that matters would give a compile error at that ladder while the other three stayed silent, which is rule 144's reassuring-copy shape in types. **What lands instead is the test and the type.** `VocabField.type` was a bare `string` (`api.ts:575`) and `FieldType` did not exist in the frontend at all; it is now the six-member union, pinned against `engine.fields.FieldType` by a mirror test whose failure names all eight dispatch sites, since not one is exhaustive. **Eight, not five**: three of them pick a `step`, and a whole step makes `QuantityInput` withhold a fractional keystroke, so a new fractional member is untypeable before `coerceValue` ever sees it. That message is the one place the list lives and `api.ts` points at it (rule 144). **Six tests pin the three conversions**, both directions, driven red five ways: dropping the `bytes` and `rating_tenths` arms of `coerceValue`, adding a spurious `days` arm, dropping the read-back unit suffix, and mis-scaling the read-back divisor. `days` reaches `coerceValue` through the fall-through and is pinned as such. **Typing it immediately found an invented fixture**: `StaleReadSweep.test.tsx` composed a rule on `runtime_minutes` of type `"int"`, and the server can serve neither. **W11-22's three is right and there is a fourth**, `ServiceModal` spelling its `canClose` expression at `:659` and again at `:751`, whose declaration comment already claimed it was "computed ONCE and handed to every path" (rule 7/24, now true). The defect under the duplication is `ScheduleModal` mirroring one TERM, `save.isPending`, against a shell handed `!save.isPending`: correct by coincidence, a rule 80 hole the moment a second refusal reason lands. `useBackCloseMirror(ref, canClose)` takes the whole predicate, so the one-term spelling has nowhere to live; two tests, one driving Back through the real parent/child split and one reading the unmount clear at the ref, which no caller can observe because all three parents arm the guard on the same state that mounts the modal (said in the test, rule 118). **W11-24's `~28` is code net +2**, and it landed for the copy divergence the two copies were hiding: the why panel's failure said "The item itself is unaffected" and its mirror did not. **Cut, not mirrored** (mocked before the edit): the panel opens beside cards carrying Spare and Reap, so an operator who just pressed one reads it as their decision not having landed; there is no honest version of it for a panel about a person's requests; and nothing on this path writes anything, so it reassures against a fear the failure never raised. `_EXPECTED_NOTICES` 143 → 142 for the one call site that left the walk. **Neither extraction saved a line**, W11-22 at code net 0 and W11-24 at +2, and the reasoning is in `LEARNINGS.md`: a duplication found by diffing the copies against each other tells you which is wrong, and a zero-net extraction is a kill only when the copies also agree |
| #728 | 8 | W3b-11's art ladder (un-killed); W5-2 (kill confirmed, gate lands); a re-review of thirteen kill rows | `components/artFallback.ts`, `useArtFallback`, `artFallback.test.tsx`, `test_every_display_field_the_source_carries_reaches_its_lanes_pack`, `_display_pack_sites` | no | **A pass over the phase's kills asking what shape WOULD work rather than whether the plan's did. Nine of the fourteen rested on a line count.** S5 gains the paragraph saying so: it is a line test, and "does this remove a place a future author must remember something" is a second question. **Two rows moved and eleven stood.** **W3b-11's art ladder is un-killed and lands as a hook**: the kill measured a shared COMPONENT, and `Backdrop` and `WhyHero` share the ladder while sharing none of the chrome, so the component's 35 lines back were the chrome arriving as props. `useArtFallback` returns `{ src, onError }`, 41 lines leave the two sites against 6 back plus 2 imports and a 38-line leaf module, about zero on total lines and about -8 on code. The payoff is the two comments that pointed at each other. Neither copy had a test; the new file drives all three rungs plus the reset through `WhyHero` rather than a probe, red three ways. `_EXPECTED_FRONTEND_MODULES` 205 to 207, `_EXPECTED_RENDERING_TEST_FILES` 57 to 58. **W5-2's collapse stays killed and the hazard under it becomes a gate**: all fifteen `Display` fields default to `None`, so a field packed on the movie lane and forgotten on the season lane raises nothing and mypy sees nothing, and W5-2's own text names three of them as identity-path join keys. The permitted omissions derive from the source dataclass rather than a list, so a sixteenth field needs no edit; red five ways, including a pack emptied so it reads as the `_NO_DISPLAY` singleton. `Display`'s docstring opened "None of them decide anything", quoted in that kill and left standing (rule 7/24). **Two premise-false kills re-verified rather than taken on trust**: W9-5's 13 modules still reach `clients/base.py` through something they import for real work, closure identical either side of the cut for all 13, and every importer of `api/backup.py` still imports `api/runs.py` in the same breath. **Measured and rejected**: shared `Annotated` bound aliases for W5-5's seven caps, which move a deletion cap's bound off the line declaring it; a hook for the second image ladder, whose two sites are 2 lines each. **A question the kills did not ask**: W3b-11 recorded `ScalesPanel`'s `Poster` as having no reset effect and preserved the difference. It is safe, because the row key carries the title id, and nothing said so beside a sibling calling the reset load-bearing. One comment lands there. No behavior change, so `STATUS.md` is untouched |

| #733 | 8 | W11-42, W11-19, W11-18, W11-10 (getters; its job blocks killed) | `binaries.yml`'s `probe`, `deps.state_singleton`, `useSwitchConfirm`, `useSuggestedMap` | no | **Four dedups, three of the four stated figures wrong, and the lines are the reason for none of them.** Code net, non-comment and non-blank: the macOS boot probe **-8**, `useSwitchConfirm` **-8**, the four `app.state` getters **-6**, `useSuggestedMap` **-3**, so **-25** over the four. The raw diff across the same files is **+21**, the shared declaration carrying the explanation the copies had split between them. So S5 read as a line test is answered four different ways here and settles nothing; what each one turns on is whether it removes a place a future author has to keep in step, and each removed one that had already been forgotten. **A first pass reported the getters at +2 by counting the helper's docstring as code**, which would have inverted that row's verdict. **The probe's two copies were carrying a live defect.** `curl -s … \| head -c 200 \| grep -qi` under `set -o pipefail` reports curl's status, and curl takes SIGPIPE once the page outgrows the pipe buffer: measured passing at 4 KB and **failing at 200 KB** against a built page under 5 KB, so a shipped gate is green on a size accident. Three copies on `dev`, two collapsed into the function and the snap's fixed beside it (rule 72). Driven four ways against a fake binary: healthy passes, 200 KB passes where the old form exits 1, a root serving JSON still fails, a binary that never boots still fails. **The gate is what pins it, not that drive**: `test_no_pipefail_gate_reads_its_verdict_through_a_short_circuiting_pipe` bans the shape in all 18 pipefail'd workflow steps and is driven red against the exact line this removed. Its population count corrected a hand reconciliation of 14 to 18 (rule 145). **`binaries.yml`'s provenance pair is settled rather than left**: both differences are forced, the `--out` paths by two consumers that read two locations (`reaper.spec:25` off `SPECPATH`, `snapcraft.yaml:100` off the repo root) and the interpreters by two jobs with different toolchains, and both steps now say so. The composite actions stay deferred. **`useSwitchConfirm` found a fourth caller the row does not count**, `JobsPanel`'s `onGoToPlex` at `Settings.tsx:208`, and its test harness was a third copy of the caller half, rewired onto the hook (rule 119). Four mutations driven red, one test each; the nonce bump was pinned by nothing. **`useSuggestedMap`'s no-clobber rule was unpinned behind rule 141**: the saved-mapping test set the stored value and the suggestion both to `TV`. Set apart it still passed, the assertion landing before the effect; a second folder the prefill may touch is what makes the wait mean anything. Three mutations driven red. **`state_singleton` is here for the invariant rather than the -6**, because "no `await` between the read and the write" was written at one of four sites and depended on at three; it is a plain `def`, so the invariant cannot be broken without turning every call site async. No behavior change anywhere, so `STATUS.md` is untouched |
| #735 | 8 | W11-5, W11-33, W11-34, W11-35, W11-40, W11-43 | `ix_action_step_run_id`, `f7a8b9c0d1e2`, `buildinfo.project_root`, `settings._BAD_CRON`, `restore._check_schema`, `snapshot.condemned_keys`, `test_a_runs_journal_read_searches_an_index_rather_than_scanning`, `test_both_job_families_refuse_a_bad_cron_in_the_one_declared_sentence`, `TestProjectRoot` | **yes, `f7a8b9c0d1e2`**, one `create_index`, additive | **W11-40 is the only defect in the batch and the only one whose value is not lines.** `SCAN action_step` to `SEARCH ... USING INDEX`, on a table retention never sweeps; `services/retention.py`'s exclusion untouched. **Code net, non-comment and non-blank: -3, -5, -3, -3, +4 with an 11-line revision, and -2**, against stated -3, -7, -5, -3, index-only and +2. Two exact, two short in the same direction, and W11-43 beating its estimate. This row first carried the same diff counted as total lines (-2, -2, +2, -1, +7), which is not the basis the rest of the table uses and reversed the reading; corrected at #740. W11-33's second half was already built at #720. W11-43 chose the consolidation over the gate: after it `src/` holds one multi-parent walk, so a ban would scan a population of one. W11-35's `PlexError` arm carries two causes and is #734 |
| #740 | 8 | none new; corrects #735 | `test_both_job_families_refuse_a_bad_cron_in_the_one_declared_sentence`, `f7a8b9c0d1e2`'s docstring, `ActionStep.run_id`, `api.leaving_soon.sync_leaving_soon` | no | **#735's own review lanes reported after it merged, and both found defects in its new work.** **The cron test could not fail for the duplication it exists to prevent**: the copies it replaced rendered identically, so asserting over the two responses stays green when an arm re-inlines the sentence (rule 144). A source count derived from the declaration's own prefix closes it. **It was fail-open a second way the lane did not reach**, found by driving it: raising `_BAD_CRON` unformatted passes every check, because the raw template starts and ends with the halves being compared and `{reason}` satisfies the length bound, shipping the placeholder to the operator (rule 21). Two assertions, driven red separately. **Three rule 7/24 claims corrected**: the revision named `_refresh_overrides` as a second `action_step` reader when it reads `WhitelistEntry` (it is `_revive`), credited `render_as_batch` for an index that never goes through `batch_alter_table`, and `leaving_soon`'s comment said the third arm was the unlinked case when a client `PlexError` lands there carrying its own log text (#734). **The column and test docstrings each restated the revision's reasoning in full** and are pointers now (rule 144). **Every figure #735 published was on the wrong basis**: it counted total lines into a table that is code-net throughout, which read as five of six costing more than stated. Code net: -3, -5, -3, -3, +4, -2, so two are exact and W11-43 beats its +2 estimate at -2. Same slip as #733's getters row one PR earlier, opposite direction. No behavior change, so `STATUS.md` is untouched |

### Killed while executing

A finding that turns out to be wrong is struck here rather than silently skipped, so the next
session does not re-derive it. The third pass killed four before any code moved; execution will
kill more.

**A kill also gets a `> Killed:` block on the finding body itself**, in the same commit, exactly as
the third pass folded its corrections in place. A phase-8 session reads the finding, not a table
1,400 lines above it, which is why *Entering a phase* has to tell it to re-read the
`> Corrected:` blocks at all.

| Finding | Killed because | Found by |
| --- | --- | --- |
| W1.1-l | `TautulliClient.metadata` is not dead. `scripts/validate_ingest.py:290` reads `added_at` through it for the dormancy-derivation check, and `docs/LEARNINGS.md` cites that harness. The row measured "no caller" over `src/` alone | Phase 5, PR 1 |
| W5-2 | The carrier is already passed whole and `_judge_item` already takes it as one parameter, so the row buys zero parameters and zero net lines. Three of the movie lane's overlapping fields are identity-path join keys, and `snapshot.py` is never split. **Still killed, and the hazard under it now has a gate**: all fifteen `Display` fields default to `None`, so a field packed on one lane and forgotten on the other is silent, and a missing join key drops a Scales join where a missing `title_slug` drops the Sonarr link. The permitted omissions derive from the source record | Phase 8, measured before building; re-reviewed at #728 |
| W5-5 | The collapse turns `PUT /api/profile {}` from a 422 into a 200 that resets every deletion cap to the shipped default, on a route an API key can write. The wire model's required fields are the protection. Reclassified `safety-path`; a seven-pair bounds test lands instead. **The smaller shape was measured and rejected too**: shared `Annotated[int, Field(ge=…)]` aliases would make the two bounds one declaration, and they move the bound on a deletion cap out of the line declaring it, where reading it in place is worth more than saving the second spelling. The gate holds both answers to one number and names both files when they disagree | Phase 8, measured before building; re-reviewed at #728 |
| W5-6 | Any extraction is a 13-parameter helper replacing a 13-key constructor (S5), and the incident the row cites was in the loop rather than the constructor. The parity sweep already compares all 13 keys across both sites; a `NUMBERS` derivation lands instead | Phase 8, measured before building |
| W3's executor size-interlock extraction | Built twice and measured. Reason-only is +7 lines and splits one operator sentence into a two-slot template (rule 21); whole-branch is +9 and returns `an optional StepOutcome`, and dropping that sentinel at one call site fails exactly one test of 4,235 while a real unmonitor reaches Sonarr. `_grew_materially` is already the predicate's one declaration, so the extraction guards nothing that can drift. The rule 144 half is real and lands: two byte-identical `check=` sentences, one of them in the branch the row exempts | Phase 8, measured on two patched trees |
| W3's cache-database row | Two bootstraps not three, two stamps in one spelling not three in two, and "~90 lines" matches nothing (418-line cluster, 25 to 35 removable). Merging two opposite stale-shape policies would need a per-caller flag, which is the one thing that must never be shared. The lock is extracted instead, and it carries the fix for a `dev` defect the row does not name | Phase 8, measured before building |
| W3's pragma unification, the sqlite half of the `backup`/`restore`/`retention` row | `_configure_sqlite` is already one declaration for both engines, and the correction takes `_read_revision` and `retention`'s `isolation_level=None` out of scope. What is left is two one-line `busy_timeout` calls whose values differ by design and three sites needing no pragma, so a `(connection, ms)` helper replaces one line with one line. The duplication is the VALUE: `5000` is quoted as prose in five docstrings, none derived. Two gates land instead. The row's operator-string half stays open | Phase 8, measured before building |
| W3b-2, the scheduler decorator | Four of the seven jobs fit it: two record nothing by design and `scheduled_scan`'s two quiet skips must stay unrecorded. `session_factory` sits at three different positional indexes and is `\| None` in three of the four, so the wrapper needs `inspect.signature().bind()`. 27 lines out, about 25 back, and the five catch-all comments each give a different reason that swallow is safe, so they stay at the sites either way. `_record_run` has 15 call sites, not 17. The prose guarantee is the finding, and W11-32's two inner handlers coming out is what makes it structural. **The four-job partial loses too, and 2026-08-10 is the first time it was costed.** Built for real over `refresh_ratings`, `refresh_curated_lists`, `full_history_sweep` and `check_for_updates`, formatted, mypy-clean and green on every test in `test_scheduler.py`, 37 of them at the commit it was measured on. It comes to **+21 total lines, +13 non-comment lines and +5 statements** on `scheduler.py`. `inspect.signature().bind()` is still needed after the narrowing to four, because three distinct positions survive it and `_maintenance_specs` adds every job positionally. Each of the four still declares its own job id, log event and result string at the decoration, so only `ok=False` and the width of the catch are centralized, and a reader of the job can no longer see that its failures are caught. **A fifth job is bound by neither shape, and what would bind it is a gate**: each of the four already has its own named test (`test_a_ratings_state_read_failure_still_records_not_ok` and its three siblings), so the missing piece is a walk over `_maintenance_specs` failing on a job that records nothing and is not named as deliberate | Phase 8, measured before building; the partial built and measured 2026-08-10 |
| W9-5, `clients/errors.py` | The closure is identical either side of the cut, measured for all 13 modules that import `IntegrationError` alone and under all four graph conventions: each still reaches `clients/base.py` through something it imports for real work, so zero modules leave any closure. `api/scan.py`, the one the row calls the clean case, imports no client at all and reaches base through `services.scan_runner`. The population is 13 and not 12, and the closure is 339 with `sys.modules` at 387, so the row's 384 is likelier a stale total than a wrong delta. The word that is wrong either way is httpx: pydantic's family is 74 of the 145 non-stdlib modules against httpx2's 24. A leaf module moves two counters and its re-export is one name importable from two places (rule 103/144), against a measured benefit of nothing | Phase 8, measured before building |
| W9-6, moving `reap_in_flight` | All three importers of `api/backup.py` import `api/runs.py` in the same breath, so the 35 modules the cut drops from `api.backup`'s closure are paid by nobody, and `reaper.main` loads 931 either way. Every write of `app.state.reap_status` is in `api/runs.py`, one assignment at `:468` and 34 field mutations across ten regions from `:549` to `:762`, so every destination splits the reader off: `api/deps.py` separates `reap_in_flight` from `_reap_status`, its own create-instead-of-refuse twin, and a new module costs +1 on both counters and still leaves the mutations behind. The row's "only such edge" is five, and `auth` in its exempt set has had no importer since W9-2 | Phase 8, measured before building |
| W3b-8, `leaving_soon`'s Plex client | Six sites is right and they span five modules, only one of them the file the anchor names. Four pass the same four arguments in the same order by AST, only the box spelled three ways, and each is six physical lines collapsing to one: 20 out against about 14 for the helper, a net of about six (S5). It reaches neither `scan_runner` site: both build outside the session block immediately before the close is registered (`stack.enter_async_context` at `:388`, `building.push_async_callback` at `:474`), so moving construction inside re-opens the leak rule 34 already closed at both of `_plex_client`'s call sites. The row's own "no hole" holds, `safety` being keyword-only and required and `verify` defaulting to the strict `True`, so what an omission costs is agreement rather than safety. A gate over all six plus their fifteen siblings lands instead | Phase 8, PR #720, measured before building |
| W3c, all six parameter objects | Every keyword at every production call site classified: `build_season_facts` assembles its 24 from 18 locals a carrier would hold one frame up, `_judge_item`'s two sites unpack off two different record types, `scan`'s 12 pass-through arguments come from four unrelated sources, and `plan_series_prune` has 87 test call sites resting on the defaults the correction calls the protection. The match record is the one clean pass-through and holds two identity-path join keys, which is W5-2's reason. `gather`'s nine policy fields stayed open as W5-3 and landed separately, being the one candidate here whose carrier already existed. The real hazard is the twelve parallel `movie_*`/`tv_*` locals, and a gate closes it from `tests/` | Phase 8, measured before building |
| W5-1, one model for the stored explanation | Built as the row asks, then measured: the collapse drops the whole match block, and three interlocks fail open with it. The read model's three `mode="before"` validators exist so an illegible stored byte degrades one field instead of blanking the panel, and on the write side that same leniency normalizes the writer's own value to `None`. One model cannot be lenient for a reader of old rows and strict for the writer, so `extra="forbid"` catches none of it. The row's premise was also unfounded: all 36 written keys already match the declarations. A pinning test lands instead | Phase 8, PR #706, measured against a C9 drive |
| W3b-9, a `stored_or_seed` helper | **Killed, then rebuilt in a different shape. Read both halves.** The kill was right about the row and wrong about the item. Right: "7 times in 3 spellings" is seven exactly and five spellings, and the gate it landed instead found **three of the seven precedence sites unpinned across the entire suite** (a stored empty proxy list reverting to the env seed, rule 1's shape and claimed by its own docstring; a stale env webhook clobbering a UI edit; a credential under a rotated key reporting "connected"). Wrong: it measured ONE helper that swallows the `_get` call, priced it at 0 to 2 lines, and read that arithmetic as the verdict (S5). Two helpers taking the value `_get` already returned cost neither the line nor the gate, and the rebuild lands them | Phase 8, PR #703, all seven mutations driven red |
| W11-10's two detached-background-job blocks (its four getters are built) | **Half a finding, so W11-10 is in *Landed* too and the two counts do not sum**; W11-3's killed table is recorded on its Landed row instead, and the two conventions disagree. `~45` is unreachable. `launch_scan`'s `run()` is 47 lines and `execute_run`'s `_reap()` is 87, and they share **5**: `except Exception as exc:`, `phase="error"`, `error=str(exc)`, `finally:` and `running = False`. Even the `log.warning` between them differs in event name and fields. The bodies are unlike by kind, one looping to consume a queued follow-up scan and the other walking an `AsyncExitStack` over the deletion clients and publishing a report. The status models differ too, `stopping` only on the reap and `followup_queued` only on the scan, so a shared wrapper takes both as parameters and holds nothing. The row already conceded the shape by making the cancel-and-await asymmetry a parameter (rule 128), and the second block is the deletion path | Phase 8, measured before building |
| W11-39, one read for the overrides and their expiries | Built as the row asks and measured: `whitelist.py` +14/-7 and `review.py` +2/-4, a **net +5**. The loop that splits one result set into two maps is ten lines, where each read it replaces is two statements, so the extraction is larger than what it removes (S5). **"Back to back at four call sites" is wrong**: two are adjacent pairs, `breakdown.py`'s pair sits 40 lines apart across the condemned read and `effective_condemned`, and `review.py`'s fourth sits about 150 apart, so collapsing either moves a read rather than removing one. `review.py:489` reads `spare_expiries` alone and would go from a filtered two-column select to an unfiltered three-column one. The benefit is two fewer SELECTs against a table holding one row per manual override, on two page loads, and the only version reaching the row's own figure widens the `overrides()` read the executor issues before every item of a live reap (rule 112) | Phase 8, built and measured, then reverted |
| W3b-11, four frontend hooks | All four extractions net positive, and two of the four counts are wrong. Non-blank lines throughout. The image-fallback ladder is **two** ladders at **four** sites: 14 lines at each of `Backdrop` and `WhyHero`, plus a five-line comment that relocates, so 33 leave and 35 come back, 29 of them a leaf module both callers can import without a cycle. The dirty-report **5** is exact and is the sub-item worth least, 20 out against 29 back, because rule 146's obligation is per-site and a hook cannot carry it. The test-result pairing is **four** sites, and it stays a kill on shape rather than on lines: one stores a union of two payload shapes and reads the held result at three places, the others store one and read it at one. The password form is **two**, 8 shared lines out, three boxes against two and three complaint branches against two. What the measurement found instead is **three of the four test surfaces computing the fingerprint at settle time**, where the boxes have already moved, so a badge vouches for an address nobody tried; plus a live complaint announced on every keystroke on the wizard's password step, and one ungenerated copy of the length floor. All fixed, with a gate over the fingerprint family. **One of the four is un-killed: the art ladder lands as a HOOK.** The kill measured a shared component, whose chrome the two sites do not share; `useArtFallback` shares the ladder alone, nets about zero and retires two comments cross-referencing each other. The other three stand | Phase 8, PR #719, the gate driven red and its own first two drafts fail-open; the ladder rebuilt at #728 |

## Execution

The findings are the *what*. This section is the *how*, and an agent picking up work reads it
first.

### Standing constraints

Ten hold across every phase. Each is a way an otherwise-correct change breaks something.

**S1. A wire field never moves in one language alone.** `tests/test_api_type_mirror.py` compares
`api/schemas.py` against `frontend/src/api.ts` and fails on a field present in one and absent from
the other, so a schema change is one commit touching both trees. Its two hand-reconciled counters
(`EXPECTED_INTERFACES`, `EXPECTED_PAIRS`) move with any added or removed interface. The guard
covers the 91 `export interface`s only; the 17 `export type` declarations, which is where
`Verdict`, `Override` and the chip tone live, sit outside it, as do nested inline objects below
their top level.

**S2. Deleting an ORM attribute needs a migration first.** Excluding a column from autogenerate
silences `alembic check`; it does nothing about the `INSERT`. `Profile.enabled`,
`PendingPlexLogin.pin_code`, `Profile.active_policy_id`, `PlexServer.owner_plex_account_id` and
`ListConfig.built_in` are all `NOT NULL` with no server default, and the Python-side `default=`
that keeps the first one working today is lost with the attribute. Dropping any of them means: an
additive `batch_alter_table` revision adding a `server_default` (the shape
`20260804_1300_heal_list_config_shape.py` already demonstrates), **then** the attribute plus the
`include_name` arm. Doing it in one step fails the first write on a fresh install.

**The `include_name` arm is required for *every* dropped attribute; the `server_default` revision
only for the `NOT NULL` ones.** `Candidate.poster_url` (W1.1-i) is `Mapped[str | None]` with
`default=None`, so it needs no revision — and it still needs the arm, or `alembic check` reports a
pending `drop_column` forever (#271). Phase 5 bundles it correctly; this constraint read alone used
to say nullable was safe.

**S3. A `safety-path` change ships with a driven pass, not green tests.** The `verify` skill,
against real data, plus `tests/test_reap_loop.py`, `tests/test_guarded_transport.py` and
`tests/test_plex_guard.py` run alone and read by exit code. Green tests are not enough here
because several of these interlocks are unpinned at the call site: dropping `plex.py`'s eight
`except SafetyViolationError: raise` arms goes green today.

**S4. Deleting a route is an external contract change.** `/api/openapi.json` is served and an API
key reaches every read not on the deny list, so "no frontend caller" is measured against the SPA
alone. An operator's script is not in this repository.

**S5. Line count is a symptom, not the goal.** The question for every finding is whether the result
is easier to read and change with the same behavior. Five lines becoming one is right when the one
is simpler. A split that adds a shared preamble is right when the pieces are coherent. A change
that removes 200 lines and makes the remainder harder to follow is not a win, and neither is a
parameter object that nets to zero — `_judge_item`'s 27 arguments are the complexity, and the count
of lines around them is noise.

This document's Python line counts are also short by 0.2% to 16%, so do not quote one in a commit
message. But the reason to ignore them is the first paragraph, not the second: a size estimate says
how big a session is, never whether the change is worth making.

**S5 is a line test, and it has been read as a kill criterion.** "Nets to zero" answers whether
an extraction pays for itself in size. The separate question is **how many places a future author
has to keep in step**, and a line count does not measure that. A rule written three times drifts,
which is CLAUDE.md's rule 144. A helper that saves zero lines and removes a drift surface is worth
building. One that saves zero lines and removes nothing is what S5 is right about. A kill needs
both halves said, and until 2026-08-10 most of this phase's said one.

**Counted on 2026-08-10, over two populations.** Ten of phase 8's fifteen kills rested on a line
count, seven citing S5 by name, while the same phase found five fresh drifts inside the code those
extractions would have covered. Separately, four wave 11 kill verdicts state a line figure as the
reason: **W11-8** nets 0, **W11-11** costs +7, **W11-23** nets -7, **W11-44** costs +10. In three
of those four the measured column also carries a premise correction the verdict never mentions, so
the arithmetic is not what the kill actually rests on.

**Re-asking the kills moved two, and both had been measured in the wrong shape.** **W3b-9** is
rebuilt at +13 total lines and -9 code lines: the shape that was priced swallowed the `_get` call,
and the shape that works takes the value it returns. **W3b-11's image ladder** is the same error in
the other language, a shared COMPONENT priced against two sites that share the ladder and none of
the chrome, with the hook they do share never measured at all. What stays killed on zero lines is
`_judge_item`'s carrier, which removes nothing.

**Where the honest answer is "the duplication is fine and nothing stops it drifting", the gate is
the answer.** Six of those ten kills landed one: W3b-8, W3b-9, W3c, W3b-11, the pragma row's pair,
and W5-6, whose `NUMBERS` derivation is a rule 103 guard. W5-2 is the seventh. Three landed
neither: W3b-2's two inner handlers came out with W11-32, the executor size interlock landed two
constants and four pinning tests, and W11-39 landed nothing.

**Two rows are the ones to copy.** **W11-39** reached its figure the right way round, built first
and measured at +5, then reverted. **W11-41** names the drift surface as absent rather than
counting lines, byte-identical DDL meaning no invariant can drift.

**A kill also has to ask whether the divergence it preserves is correct.** Measuring two copies to
decide they are cheaper apart reads each for its line count and neither for its behavior. W3b-11
recorded the second image ladder as "one with an `alt` and a reset effect and one with neither" and
never asked whether the absence is right. It is, and only because of a row key nothing had written
down.

**S6. `docs/STATUS.md` is full.** 120 of 120 lines, enforced at `tests/test_repo_hygiene.py`'s
`STATUS_MAX_LINES` alongside the 100-column bound at `STATUS_MAX_COLUMNS`. Phases 4, 7, 8 and 9 all alter what the app does, which
CLAUDE.md's golden rule says updates STATUS in the same commit, so **every such PR removes a line
to add one**. A new dagger costs more: a `docs/DECISIONS.md` section *and* a bump to the
hand-reconciled `DECISION_SECTIONS` at `:59`, which is checked both ways. W1.1-a's correction is
the case that will hit this first.

**S7. Hand-reconciled counters move with the populations they count.** S1 names two
(`EXPECTED_INTERFACES`, `EXPECTED_PAIRS`); `DECISION_SECTIONS` is a third, and every gate phase 3
lands under rule 145 adds another. Phase 6 splits two routers and phase 8 creates `api/deps.py`,
which moves populations that phase 3's gates count. **`LAUNCHER_CONF_NAME` does not, and this
paragraph used to say it did**: the layering walk covers the four packages only, and `launcher.py`
and `config.py` sit outside it, so moving a constant between them moves neither the module figure
nor the logger count. Measured on a patched tree, not reasoned. The counter that half does move is
`_DEFERRED_CROSS_PACKAGE_IMPORTS`, from 3 to **0**: two of W9's three sites went at #677, and the
third was killed there and un-killed later, once a PR was already paying the driven pass its kill
had refused to buy. Grep for the counter
before closing a PR that adds or removes a member. **The phase-3 counters, by name:**
`_EXPECTED_LAYERED_MODULES` (**84** modules under the four packages), the logger counter in
`tests/test_capturable_loggers.py` (**50**), and `_DEFERRED_CROSS_PACKAGE_IMPORTS` (**0** sites,
empty; of the three that went, one had moved file in #612). Empty is the interesting state for
that one, not a broken one: every cross-package edge in the four packages runs at import time now,
so the runtime graph is the whole truth about them. **Phase 8 added a fourth counter**,
`_EXPECTED_SOURCE_MODULES` (**116**), which counts every module under `src/reaper` rather than
the 84 under the four packages, so a new module moves both and each gate's failure message names
the other. Its neighbor `_KNOWN_IMPORT_CYCLES` is a set, not a count. The module figure has moved
five times:
#599's deletion took it to 76 without this paragraph noticing, phase 6's `api/plex.py` took it to
77, its `policy_migrations` / `policy_warnings` pair to 79, `routes.py` becoming five modules to
83, and phase 8's `api/deps.py` to 84. Each gate's failure message now names its prose siblings,
since nothing asserts them.

**The logger counter moved too, and this paragraph said it would not.** It read "a split inherits
its parent's loggers rather than declaring new ones, so W2 motion moves one counter and not both",
which held for #609 and broke on #612: five modules cut from one file are five *modules*, and
three of them log, so one logger left and three arrived. What survives of that claim is narrower
and is the useful half — a split inherits its parent's logger only where it inherited something to
say, which is why `api/vocabulary.py` and `api/about.py` declare none. **Both counters move on a
split; only the module figure moves on every split.**

**S8. Every PR diffs the behavioral baseline, and an unexplained line is a regression.** Phase 1
freezes what the app currently *decides* about a real library. The test suite does not cover
this — it pins mechanisms, and 3,164 green tests are compatible with the app reaching a different
conclusion about the same file. **Only the phases S6 names as altering what the app does may move
the baseline**, and each of their PRs names which lines moved and why, in the body. A diff out of
any other phase means stop. The two lists are one fact, so correct S6 and read it from there.

**The baseline has two tiers and they are diffed differently.** Tier A is the replay, runs in CI on
every PR, and is what makes this constraint a gate instead of prose: a diff is a stop. Tier B is
the owner-run capture, diffed at phase boundaries rather than per PR, and its verdict and score
lines are read as **counts and set membership, never line by line** — measured, the calendar alone
moves 45% of score lines in 30 days while holding the code and the library still, so a per-line
reading of Tier B is noise by the second week.

**Diff Tier B immediately after each weekly `dev` merge.** That merge is the one commit that
legitimately arrives carrying a week of someone else's behavior changes, in the worst possible
granularity for attributing anything, so it gets its own reading and the next PR starts clean.

**S9. Every PR gets a `/reaper-review` pass before it merges into the branch.** The skill ranks
findings by proximity to the deletion path, which is the axis these risk classes already use. The
checkpoints below are the owner reading a *decision*; this is an agent reading a *diff*, and
neither substitutes for the other. A `safety-path` PR gets both, plus S3's driven pass.

**S10. The PR that lands the work updates this document.** [Progress](#progress) moves in the same
commit: the phase row, the *Landed* row, and the *Killed while executing* row plus a `> Killed:`
block on the finding body if the session disproved a finding. A phase whose findings are all merged
sets its status to `done`. This is the only record that survives the squash merge, and a session
that skips it costs the next one an archaeology pass over 60 sub-PRs.

**A PR that shifts line numbers in a file another finding cites re-anchors that citation in the
same commit.** The stale window opens at phase 4, so phase 6's exit task is the sweep rather than
the only obligation, and a phase-8 session should not be the one discovering that phase 5 moved
`snapshot.py` out from under eight citations.

### The phases

Ten, ordered so each shrinks or stabilizes what the next one reads. Later phases are landable
earlier at a cost that is named in each. **The numbers are frozen** — see *How to read this*.

| # | Phase | Findings | Risk | Why here |
| --- | --- | --- | --- | --- |
| 0 | Correct the plan | this section | none | Done. The corrections below are the deliverable |
| 1 | Behavioral baseline | none | none | Nothing else can tell a refactor from a regression |
| 2 | Test-suite wall clock, then scaffolding | W1.3, W12, then W1.4 | none | Every later phase is paid for in test cycles |
| 3 | Gates that land green | W6-5, W6-6, W6-8, W1.5-c | none | A layering test is worthless after the graph moves |
| 4 | Drift corrections | W10 | `behavior` | Defects. Fixed before the baseline is trusted, so later phases preserve correct behavior rather than bugs |
| 5 | Deletions | W1.1, W1.2, W7 | none, `behavior`, `migration` | Shrinks what phases 6 to 9 must read |
| 6 | Structural motion | W2 | none | Dedup inside a 2,800-line file is where sessions run out of context |
| 7 | Wire contract | W8, W4.3, W7-2 | `behavior`, `safety-path` | Two-language commits; smaller files make them tractable |
| 8 | Dedup and carriers | W3, W5, W9, W6-1/2/3/4/7, W11 | up to `safety-path` | The bulk. Every safety-path item is its own PR |
| 9 | Declaration tax | W4.1, W4.2 | `behavior` | Most leverage, widest blast radius, smallest tree |
| 10 | Issues that land here | none | up to `behavior` | Not a wave. Tracker work that must ride this branch because its fix sites moved |

**One PR is one session.** A phase is several. A session whose context has been compacted stops
before a `safety-path` merge rather than pushing through.

**One session works one phase.** Never two, and never a phase another session is already in. Eight
files carry findings from four or more waves, so two sessions in different phases will meet inside
`api/routes.py` or `main.py`. The *Progress* table's `in progress` status is what claims a phase;
set it before starting and clear it before stopping, whether or not the work finished.

**Entering a phase, re-read the `> Corrected:` blocks for every ID it names.** Phases 4 to 9 run
weeks after the review that wrote them, and a finding's body still reads as originally written; the
correction is the part that goes stale in a reader's memory rather than on the page. Four findings
say plainly that the obvious implementation is the dangerous one.

### Where the phases collide

Eight files carry findings from four or more waves. Phase order is mostly *about* these.

| File | Waves | Consequence |
| --- | --- | --- |
| `api/routes.py` | 2, 5, 7, 8, 9, 11 | Phase 6 splits it into four. **Phase 6's exit task is re-anchoring every finding body that cites a `routes.py` line**, because IDs identify findings and not code |
| `services/snapshot.py` | 1.1, 3, 5, 7, 11 | Never split. Phase 5 deletes from it, phase 8 rewrites its carriers |
| `api/settings.py` | 2, 3, 4.1, 5, 6, 9, 10 | Phase 6 takes 14 Plex routes out. See the password-gate note below |
| `frontend/src/api.ts` | 1.1, 4.1, 4.2, 4.3, 7, 8 | Phase 7 hand-edits the type block; phase 9 deletes 1,239 lines of it |
| `components/Settings.tsx` | 2, 3, 4.1, 10 | Phase 6 extracted the 7 panels; phase 9's `FIELDS` descriptor now lands in `GeneralPanel.tsx` alone, which exists |
| `services/executor.py` | 1.1, 3, 8, 9, 11 | Phase 5's deletions are trivial; phase 8's size-interlock work is the sharpest thing in that phase |
| `main.py` | 2, 3, 4.2, 4.3, 9, 10, 11 | Spans phases 4, 8 and 9. Nothing splits it, so the risk is three phases editing one boot path |
| `services/leaving_soon.py` | 3, 6, 8, 9, 10 | Phases 4, 7 and 8. W3's `plex.py` helper changes what this file's error handling sees |
| `tests/conftest.py` | 1.3, 1.4, 6, 12 | Phase 2 twice and phase 3 once. Land the scrypt wrapper before the socket guard |
| `services/season_scan.py` | 1.1, 2, 3, 5 | Phase 6's `season_evidence.py` move makes phase 8's `gather` carrier cheaper |

**`alembic/env.py` carries only waves 1.1 and 7 and is under this table's threshold**, listed
because those two are one PR rather than three. See phase 5.

**One contradiction the waves do not resolve, decided here: the admin-password helpers go to
`api/deps.py`, not `api/auth.py`.** W3 asks for the gate ritual extracted out of `api/settings.py`
and W9 asks for the five helpers moved out of `api/auth.py`; both are phase 8, both are
`safety-path`, and whichever lands second silently re-homes the gate that guards arming deletion.
They are **one PR**: six functions to `api/deps.py` (`_refuse_if_waiting` is a shared callee and
travels with them), the ritual extracted onto them, the throttle key tuple passed rather than
derived, and the missing throttle tests for the two gates that lack them.

**Landed at #681, exactly as written.** The two gates were `change_password` and `restore`; this
paragraph said three, one more than the finding's own correction block says, and both are now
counted from the tree. The key tuple is passed, and a hygiene gate now confines `password_throttle`
to `auth/ratelimit.py` and `api/deps.py` so a fifth gate cannot re-derive it.

**The request accessors landed first and separately, at #670, and that does not reopen this.**
They are W3's own bullet, a third population this paragraph never named, and they share a
destination file with the gate and nothing else. What the contradiction is about is W3's ritual
against W9's helpers, and both are still in the one PR above. The split buys the `safety-path`
review a diff where every line is on the safety path: together they are roughly 350 lines across
12 files, of which about 250 are a mechanical rename a reviewer has to read past to reach the four
call sites that can arm deletion. `api/deps.py` therefore exists for one PR's duration without
being under `.claude/rules/auth.md`'s globs, which is harmless while it holds no gate and is the
second PR's first edit.

**Two items are each called dangerous, and they are dangerous differently.** W8-2 in phase 7 can
delete files nobody approved. W3's `plex.py` helper in phase 8 can turn a safety refusal into a
per-library warning. Neither ranks the other, so **both are designed before they are built**: C7
and C14. They used to be gated unequally, W8-2 before the work and `_call` only after it.

### Phase 1 — behavioral baseline

**The repository already records what the app decides for about a quarter of the decision surface,
and this phase extends that rather than building beside it.** `tests/_policy_lab.py` replays 440
de-identified real fact vectors through production `judge_facts` and pins `verdict`, `score` and
`coverage_bp` per vector; `scripts/policy_lab_extract.py` regenerates it from `data/reaper.db`
read-only, de-identified by construction, with a `--rebaseline` mode that needs no real library and
a refusal that blocks a moved baseline while `SCORER_VERSION` stands still (rule 113). *Do not
touch* names it already. This section used to open by saying nothing recorded what the app decides,
which its own page refuted.

What is still unpinned is the conclusion *outside* `judge_facts`, so a refactor that changes which
titles a real library would lose passes 3,164 tests. Every later phase is judged against what this
one freezes.

**Tier A — the replay. CI-enforced, every PR, and this is what makes S8 a gate.** Extend the
fixture's per-vector `baseline` block with:

1. **The three gate lists** — `protectors`, `checked_and_did_not_fire`, `could_not_be_checked`
   (`engine/gates.py:922`), **by gate id and outcome class only**. The second is the product
   promise, and the half a refactor silently drops.
2. **The explanation's decision-bearing numerics**: `base_score`, `keep_discount`, `threshold`,
   `coverage_floor_bp`, `watch_blind`, and per signal its `id`, `contribution`, `state` and
   `evaluated`.

**Never pin `detail`.** It is rule 21 operator copy that phases 4, 7 and 9 edit, it is roughly 60%
of the payload by bytes, and pinning it turns every copy edit into an S8 stop. Measured: ids and
outcome classes add ~371 KB to a 736 KB fixture, the full explanation text ~1,383 KB.

> **Landed.** `_policy_lab.pinned_baseline` builds the block off the serialized explanation, and
> `baseline_differences` reports a move leaf by leaf; `rejudge` and `TestPinnedBaseline` both call
> them, so the write and the comparison cannot disagree about what a baseline is. Re-pinned under
> the refusal's fourth named case (`--unbumped="the baseline block gained fields; the engine did
> not move"`): 880 blocks moved and **every** moved leaf was additive, which is the evidence the
> engine stood still. The fixture is 1,574 KB, 27 pinned leaves per block against 3. Three
> mutations measured, in `docs/LEARNINGS.md`: reordering `checked_and_did_not_fire` moves nothing
> in the other 4,035 tests and is caught here alone.

**Tier B — the capture. Owner-run, at phase boundaries and at C12/C13, never per PR.** A second
script reading `data/reaper.db` read-only exactly as the extractor does, emitting per item:

3. **A hash of `Candidate.facts_json`**, the per-item frozen evidence. `Snapshot.evidence_hash` is
   **not** this and belongs nowhere in the baseline: it hashes the *policy fields* that decide what
   gathering asks for (`engine/policy.py`'s `PolicyBody.evidence_hash`), there is one per snapshot, and it is constant while
   the policy is. A refactor that changed what gathering produced would not move it at all. This
   item previously claimed that hash was "already computed and the cheapest single check that
   gathering did not move," and it would have failed green.
4. **The verdict triple per item**, as the whole-library reading Tier A's 440-vector sample cannot
   give.
5. **The built plan** against that stored snapshot: `build_plan`'s ordered `media_key` list,
   ordinals, cap decisions and `manifest_hash`. Deterministic — its `utcnow()` calls only stamp
   `created_at`.

**De-identify by construction, and there is no salt.** This section used to ask for titles and ids
hashed under a salt in `.env.local`. That is weaker than the shipped precedent and is itself a
fingerprint under the golden rule's "ratios and shapes, never fingerprints." The extractor's answer
is positional ids (`v0000`), tokenized genres, votes to two significant figures, sizes to 100 MB,
and it scans even its own justification string for identifying data. Copy that. A baseline nobody
can commit is a baseline nobody re-runs.

**Neither tier needs a live server, and neither may re-scan.** Both read the stored snapshot,
because a re-scan is wall-clock dependent — `ScanContext(horizon=utcnow())` plus dormancy at
`snapshot.py:349` and `:496` — and measured against the lab, advancing only the clock by 30 days
moves 45.2% of score-or-coverage lines and 1.4% of verdicts. Over a plan that runs for months, a
re-scanned baseline is noise. Freezing the clock instead is the expensive road: `utcnow` is a
`from`-import in 32 modules, so patching `reaper.clock` reaches none of them, and the tree freezes
time at four sites total.

**Say what neither tier covers, because the riskiest work lives there.** Both hold `Facts` fixed,
so both are blind to the gather side: `clients/arr.py`'s shape guards, whose recorded incident is a
coerced `[]` read as an empty library; `plan_series_prune`'s nine permissive defaults; `scan`'s
twelve parallel `movie_*`/`tv_*` locals. A carrier that widens what is prunable on a real library
moves **zero** of the 440 vectors. The lab also reconstructs two fact fields (`_on_lists_from`,
`_ratings_from`) rather than replaying them, and phase 8's `lists.py` work sits directly under
that. **S3's driven pass is the only thing that reaches any of it**, and no baseline of any shape
substitutes for it.

Two PRs, roughly one session each: Tier A extends the fixture, Tier B adds the script and its
committed capture. Both ship with their generators under rule 68.

> **Landed.** `scripts/baseline_capture.py` writes `tests/fixtures/whole_library_baseline.json`:
> snapshot 86, 5,965 items, and the plan `build_plan` builds from it. Three things the section
> above did not anticipate. **`build_plan` writes**, so the source is opened `mode=ro` and copied
> to a temp directory and the plan is built against the copy (digest of the source unchanged,
> before and after). **A real database is behind head** — this one by three revisions — and the
> ORM names every mapped column, so the copy is migrated first and the revision it reached is
> recorded beside the capture. And **the plan is larger than the condemned set**, 592 against
> 543, because hand reaps add to it where spares subtract; a baseline pinning verdicts alone
> would have missed the override layer entirely.
>
> The capture carries no string that is not an item id, a digest, or one of a fixed vocabulary,
> and the writer refuses rather than warns. That is not decoration: three of `build_plan`'s
> refusals name the media keys they refused on, and the guard caught four wrong step-kind names
> on the first run.

**This phase is worth skipping only if the answer to "how would we know?" is already good.** It is
not: S3's own example is that dropping eight `except SafetyViolationError: raise` arms goes green.

### Phase 2 — test-suite wall clock

`@lru_cache` on `test_repo_hygiene.py`'s two file readers (W1.3), the scrypt cost wrapper, the
one test that dials the network, the `openapi_tags` fixture scope, and the twelve frontend files
that pay for jsdom (W12a).

Then **W1.4's scaffolding**: the `conftest` boot fixtures, `renderWithProviders`, `tests/_fakes.py`
and the complete api mock. It sits here because everything after is priced in test cycles, and
because `_fakes.py` retires the 65 `# type: ignore[arg-type]` suppressions that are the only reason
a client signature change fails the build — which phase 8's `clients/plex.py` and `clients/arr.py`
work depends on.

**W1.4 is four independent refactors, not one, and each is its own session.** The first landed
(#575): `settings`, `sync_db`, `async_factory` and `client` in `conftest.py`, and the 16 boots
that were exactly one of them deleted from 15 files. The bespoke `client` fixtures that survive
are the ones that SEED, which is what a file-local fixture should hold; they now compose on
`settings` or `sync_db` rather than rewriting the preamble. The second landed the same way
(#577): all 87 `<QueryClientProvider>` trees onto `src/test/renderWithProviders.tsx`, and the 55
file-local `render*` helpers KEPT, because each holds the props its own file passes. The third
(#578) is `tests/_fakes.py`, and its correction is the one to read before phase 8: the
suppressions it retires never held anything, because `tests/` was not type-checked at all, so the
deliverable is the fakes inheriting the real clients **and** the mypy run widened to see them.
Read all three `> Landed:` blocks before the last one — the shape they settled on is the same
every time, and each line-count estimate was wrong for the same reason. What remains is the
complete api mock, which carries a `coverage-loss` risk worth reading before starting.

Nine PRs, and the phase is closed. No production code changes. The scrypt wrapper is `conftest`-only,
must not touch `crypto.py`'s constant, and ships with the injectivity guard its correction names.

**Wave 12's figures are single-threaded and the documented gate is not.** `uv run pytest -n auto`
measured **81.97s** for 4,040 tests on 8 cores, against the ~268s single-threaded figure the wave
reasons from, because the scrypt cost overlaps across workers. So the saving to expect was tens of
seconds off a full run, not the ~93s the numbers imply. Everything here except W1.4's scaffolding
goes to `dev` under the escape hatch above.

> **Landed, and the caution was too pessimistic.** Measured on this branch's own tree either side
> of the five, `uv run pytest -n auto` on 8 cores: **83.44s to 38.74s**, and 330.93s to 182.75s of
> CPU. 4,099 passed and 1 skipped before, 4,100 and 1 after, the extra test being the injectivity
> guard. The frontend run moves as predicted and barely at all in wall clock: 26.84s to 25.93s,
> with its environment cost 37.25s to 29.56s of CPU. Wall clock more than halved because the
> overlap argument cuts both ways -- with the KDF gone the workers stop contending for it.

**Exit run, on the branch tip after #579.** Every gate alone, by exit code: `ruff check`,
`ruff format --check`, `mypy src/reaper tests/_fakes.py`, `alembic upgrade head` and `alembic
check` against a temp `REAPER_DATA_DIR`, `npm run lint`, `prettier --check`, `npm run build` --
all 0. `pytest -n auto` **41.71s, 4,101 passed and 1 skipped**; vitest **26.27s, 78 files and
1,322 passed**. The scaffolding added four tests to the suite and cost the wall clock nothing.

**Filed while executing, not fixed here.** #580: 204 `# type: ignore[arg-type]` comments remain
in `tests/`, all inert, because only `tests/_fakes.py` joined the mypy run. Deleting them and
widening the run are separable, and the second is a decision rather than a chore -- `mypy tests/`
reports 1,308 errors today. #576 (four leaked SQLite connections) is byte-identical to `dev` and
belongs there rather than here.

### Phase 3 — gates that land green

The layering AST test (W6-5), the socket guard (W6-8), the three path-list sentences (W6-6), and
deleting `W1.5-c` — **only that one**. Its correction kills the other three: the B017 grep is not
redundant, `test_instruction_files_exist` can fail, and the tagline test is a deliberate
diagnostic.

**A gate is proven against the population it claims to cover, not against one mutation of it.**
Rule 145 is explicit that breaking a member the matcher already found proves nothing about the
member it never saw, and this document's own W6-8 correction is the case: a socket guard hooking
`connect()` reports zero violations and zero false positives while seeing nothing at all. So each
new gate lands with **a count of what its scan collects, reconciled by hand** against the members
you believe exist. Demonstrating it red is worth doing and is not the proof.

W6-5's scope note interacts with phase 8: the layering test must skip `TYPE_CHECKING` and
function-local imports, and W9 proposed deleting three such workarounds. Pin those three sites by
name so the gate is not blind to the change it would most want to police. (Two were deleted, the
third killed; the pinning is what made either visible.)

### Phase 4 — drift corrections

W10's seven, minus item 5, which is already #558's second half. Items 1 and 4 are filed; 2, 3, 6
and 7 stay here. Item 3's fix changes behavior as written; see the correction.

**These are defects, and they run before anything is deleted or moved.** Every one of them changes
what the app does, so landing them after a refactor makes two questions out of one: did the
refactor break this, or was it already broken? Landing them here means phase 1's baseline is
re-frozen once, deliberately, against corrected behavior, and every diff after that is a
regression. Each PR names the baseline lines it moves (S8), and C12 checks item 3.

**Items 1, 4 and 5 are filed as issues and are not this phase's work.** A session that fixes one
anyway does it on a branch off `dev`, not here: they are independent defects and holding them
behind this branch delays a fix for months.

> **Closed 2026-08-08, 4 of 4 (#593, #594, #595, #596), and the re-freeze moved nothing.** The
> paragraph above opens "every one of them changes what the app does", which was the reading
> before wave 10's corrections landed and is not what the tree held. Item 2 was latent, item 6 was
> a comment, item 7 was unreachable behind the wizard's own gate, and item 3's job table came out
> byte-identical. So **Tier B re-captured identical to the committed file** and Tier A never moved:
> the deliberate re-freeze this phase exists to earn is a no-op, recorded rather than performed.
>
> **The order still paid for itself, for a reason worth carrying into phase 5.** What these four
> cost was not behavior, it was four *claims* — a status the exception knew and the route
> hand-wrote, a docstring saying startup shared a guard it copied, an enumeration of five listing
> four, a helper claiming completeness from inside one of the two components that needed it. Each
> is a sentence a later phase would have read and trusted while moving the code underneath it.
> Landing them first means phases 5 to 9 read a tree whose comments are true, which is the part of
> "fixed before the baseline is trusted" that survived the corrections.
>
> **Every one of the four grew a gate, and three of those gates were wrong first** — the status ban
> could not read `status_code=`, the strut matcher was satisfied by prose *about* its own claim,
> and the invalidation scan counted `useQuery` reads and then whole files. All three were found by
> driving them, none by reading them. Rule 147 is the standing lesson and phase 3's audit said the
> same thing from the other side: the count is the cheap half, the matcher is the half that needs a
> second party.

### Phase 5 — deletions

Four PRs, in order:

1. **Dead symbols** (W1.1 survivors, minus W1.1-i and W1.1-n). Nothing ORM-backed. Add W7-3
   (`HealthOut`), W7-4 (`DiscordNotifier`'s injection seam and its false docstring) and W7-5's
   `PolicyProbeOut.detail`.
2. **The two engines** (W1.2). Five test files, not two. Its prose sweep is 24 comment sites, two
   `SIGNALS.md` sections, two rule bodies and the STATUS roadmap, and none of that fails a test.
3. **The alembic hook** — `Profile.enabled`, `PendingPlexLogin.pin_code`, `CACHE_TABLES` (W7-6),
   `run_migrations_offline` (W7-7, which fails `test_migrations.py`'s `test_env_py_configures_batch_mode` deliberately), W7-8's three
   columns, and **W1.1-i's poster chain**, which terminates at the `Candidate.poster_url` column
   and so belongs here rather than in PR 1. One PR because they are one `include_name` and one
   `server_default` revision. S2 governs it.

   > **Decided 2026-08-07 (C1): this PR is release M of rule 148, and the drop is release M+1.**
   > S2's `server_default` revision *is* the deprecation half — it lets the previous image keep
   > inserting after the attribute goes. So this PR removes the attributes and ships that
   > revision; a later release drops the six columns in one sweep, under rule 148's three
   > obligations. The exclusion arm is a bridge for one release, never the permanent registry
   > this plan originally assumed.
   >
   > **Two things gate the sweep, both filed**: #565, an older image must refuse a newer
   > database at boot rather than fail at query time, and #566, a snapshot before a destructive
   > revision. **#564 gated it harder and is now fixed on `dev`** (#569), which is why it was
   > never this branch's work: a failed batch migration stranded `_alembic_tmp_<table>` and
   > every later boot failed. `alembic/env.py` now keeps DDL inside the migration's
   > transaction, so a failed sweep rolls the temp table back with everything else.
   > **The count in this paragraph was wrong and the direction matters**: it read "four shipped
   > migrations are exposed," from a hand list of the migrations that visibly `alter_column`.
   > Measured off the live statement stream, a fresh `alembic upgrade head` performs three
   > recreates and **two are `add_column` calls** — Alembic rebuilds the table when
   > `server_default` is a ClauseElement, and `sa.false()` is one. More was exposed than
   > counted. Rule 148's M+1 obligation is corrected to match; `docs/LEARNINGS.md` carries the
   > measurement.
   >
   > **`run_migrations_offline` goes, and the reason is stronger than the finding's.** Measured:
   > `alembic upgrade head --sql` exits 1 today, dying at revision 3, because 9
   > revisions call `op.get_bind()` and offline mode has no connection. Those are rule 81's reflection
   > guards, so the capability cannot be restored without giving them up. The deletion also
   > corrects `CONTRIBUTING.md:305-312`, which claims the test covers both call sites (rule 64).
   >
   > **The poster chain goes whole.** It stored the *arr's remote cover so the queue could show
   > a poster without a second sweep; that was replaced by the Plex proxy route before this
   > repository's first commit, and both halves were removed at once, leaving the plumbing.
   > Eight sites, zero readers, no test pins any link. The column is nullable, so it needs the
   > `include_name` arm and no `server_default` revision, and rule 148's symmetry does not bind
   > it: a nullable dead column can be abandoned for free.
4. **The whitelist routes** (W1.1-n), on its own because it is an external contract change (S4)
   touching nine test files. Those edits are the reason it is separate: the verification protocol
   below says a pinning test that has to change means the finding was wrong about the mechanism,
   and that rule is about *behavioral* pins. These nine assert the routes exist, so removing them
   is the change, not a symptom of a bad one. Say that in the PR body.

   > **Nine, not six.** The correction below counted the five files calling the ROUTES and read
   > "orphans `services.whitelist.spare`" as a claim about `src/`, where it is true. Four more
   > files call `spare()` as setup, 32 times between them, and they move to `set_override(...,
   > decision="spare")` because the shorthand goes with its only production caller. A count
   > taken off the production sweep understates a deletion every time the deleted thing was
   > also a test convenience.

   > **Decided 2026-08-07 (C1): delete them.** S4's caution is that an operator's script is not
   > in this repository. Reaper has not shipped, so there is no operator and no script — the
   > published contract has no reader outside the SPA yet. Deleting is cheaper now than at any
   > later point, and this is the window. S4 still governs every route touched after release.

W7-1's `ListMode` is separate and safe: `protection_list` is raw DDL on `cache.db`, and both
columns carry server defaults.

**Closed 2026-08-08. #597, #599, #600, #601.** Around 2,900 lines gone, and the reviews found
more than the deletions did.

**The one that mattered was not a deletion at all.** Retiring `Profile.active_policy_id` in #600
orphaned `_ensure_active_policy_row`, whose whole job was giving the foreign key a target — and
the body it persisted was the bare shipped policy, while `active_policy` computes a *wider* one
for an install that has never saved: a keep rule per Plex collection the registry holds. Once
that row existed, recency returned it forever. So an operator's keep collection stopped
protecting the first time they touched Pace, with `repaired` False, nothing degraded and no
notice. Predates this branch by a long way. **Rule 64's supply chain reaches what FED a column,
not only what read it**, and that is the sentence this phase is worth remembering for.

**Three more that the diff could not show.** A `NOT NULL` foreign key cannot take a
`server_default` under `PRAGMA foreign_keys`. A batch rebuild copies from reflection, and
reflection does not report collations, so touching one column silently un-protected a unique
index — in the downgrade as well as the upgrade, where the re-upgrade then wedges the container.
And hiding a column from autogenerate does not hide its foreign key. All three are in
`docs/DECISIONS.md` under *Migrations*, because the next release-M author looks there.

**Rule 144 fired four times in four PRs**, which is the rate worth noticing. A rewatch
percentage in three places, a `Facts(` builder list that had always been "the only two", a
`get_bind` count in four files, an operation count in five. Every one was a measured number that
nothing asserted, and every one drifted in the direction that reads as measured. Each is now
either derived or pinned, and each failure message names its siblings by name.

**Tier A never moved. Tier B moved by one line**, the recorded alembic head — 5,965 items,
protect 4,261 / condemn 543 / abstain 1,161, same plan and manifest hash as phase 4 left them.
The keep-collection fix is invisible to it by construction: the capture replays a stored
snapshot, and that snapshot was scored under the policy the operator had.

### Phase 6 — structural motion

W2's eight rows, one PR each, in ascending order of coupling: `season_scan` → `api/settings.py` →
`App.tsx` → `engine/policy.py` → `Settings.tsx` → `api/routes.py` → `ReviewQueue.tsx` →
`PlexPanel.tsx`.

The last two are **not** pure motion, and the last one is a component extraction. Land them as
their own work or drop them.

**Both are dropped, and both are filed.** `ReviewQueue.tsx` is #606 and `PlexPanel.tsx` is #607,
each carrying the correction's evidence rather than the row's. Phase 6 therefore tops out at
**6 of 8**, which is the by-design ceiling *Progress* already names. The decision is recorded here
rather than only in the issues, because a row that vanishes from a plan reads as one nobody
noticed.

**The `api/routes.py` split needs a decision before it needs a session** (C6): the file draws four
banners and the proposal names four modules, and they are not the same four. *Vocabulary* has no
home and there is no simulate banner. The invariant to hold is the same set of method, path,
`operationId` and tags, not a byte-identical document — `paths` is ordered by registration, and
four `include_router` calls will not reproduce the current order.

**Phase 6's exit task, and it blocks phase 7**: re-anchor every finding body in this document that
cites a line number in **any file this branch has edited**, not only a moved one, and mark the
phase `done` in *Progress* only once that is finished. `services/snapshot.py` is never split and
carries eight phase-7/8/9 citations that all shift when phase 5 deletes at `:202`; `executor.py`
carries five, `main.py` four. Re-anchor to the **symbol plus a quoted fragment**: eight of these sit
hundreds of lines inside `scan`, `simulate`, `list_candidates` or `ReviewQueue`, where the symbol
alone locates nothing, and two pairs share a symbol (`season_scan.py:1099`/`:1147` in `gather`,
the two `range(0, len(keys), KEY_CHUNK)` loops in `_group_rollups`, `api/review.py`), so a bare symbol silently merges two sites into one and
under-scopes W6-2's sweep. Phases 7 and 8 read those bodies, and a session that reads a stale line
number edits the wrong code with green tests behind it.

> **Done, and phase 7 is unblocked.** Every `path:NNN` in this document resolves against the tree.
> Rows 1 to 6 re-anchored their own shifts as they landed, so the exit sweep was the **18** that
> were already stale when phase 6 opened, each re-anchored to a symbol plus a quoted fragment and
> each validated against the tree before it was written — row 4's review caught three re-anchors
> naming a symbol that did not resolve, so the validation is the step, not the lookup.
>
> **Six of the eighteen point at code that no longer exists**, so they are past tense rather than
> re-pointed: `session_scope`, `FALLBACK_REWATCH_PRIOR`, `rewatch_prior`, `BacktestResult` and
> `engine/backtest.py` have zero occurrences left outside this document's own history.
>
> **The checker under-reports, which is the finding worth carrying into phase 7.** It walks
> `path:NNN`, so it is blind to a bare `:NNN` continuation: one `main.py` citation carried a second
> line as a bare `:NNN`, and that half was stale and unflagged while its neighbor was caught. and to a citation whose line is still in range but now points
> at something unrelated. Both classes were found by reading rather than by the walk: the
> `services/lists.py` weight-column citation had drifted by one *and* named one of two write
> sites, the `services/leaving_soon.py` summary-ladder one landed on a comment rather than the
> ladder under it, a `test_settings_api.py` line was stale beside a flagged sibling, and a
> `db/models.py` one was nine lines off. **Two entries were wrong rather than merely misplaced**
> and are corrected in place: the `AutonomyGrant` item cites three evidence sites and has had two
> since #599, and #599's own `Landed` row named `backtest.run` where the caller was
> `BacktestResult._expected_rates`.

### Phase 7 — wire contract

W8, W4.3, W7-2's `spared`, and **W7-5's `SignalProbeIn.window_days`**, which arrives here from
phase 5: #597 removed the `PolicyProbeOut.detail` it fed, so the third pass's kill no longer holds
and what is left is a served request field the engine cannot act on. Every PR is two languages
(S1), and each carries its `EXPECTED_INTERFACES`/`EXPECTED_PAIRS` reconciliation (S7).

**W4.3's `Literal` types come before phase 9's generator**, because generation off loose unions
bakes the looseness in, and the 17 `export type` unions are exactly what today's mirror guard does
not cover. Keep the `Literal` on `decide_verdict`'s return and out of `schemas.py`, per its
correction.

W8-2 is designed before it is built, not reviewed after (C7).

### Phase 8 — dedup and carriers

The bulk of the remaining line count and all of the remaining risk. Order within it: the already-
drifted clusters first (they are corrections), then the `none`-risk carriers, then each
`safety-path` item as its own PR with S3.

Four items in this phase are rewritten by the third pass and must not be built as originally
described: `plex.py`'s `_call`, the sqlite pragma unification, the scheduler decorator, and
`paged()`.

### Phase 9 — declaration tax

W4.1 then W4.2. W4.1's loop must validate every field before writing any, which
`tests/test_general_and_logs.py:236` exists to pin, and its `FIELDS` descriptor covers three of
`GeneralPanel`'s six fields cleanly and three with escape hatches.

**W4.1 absorbed phase 8's W3b-4, and it is one PR, not two** (owner, 2026-08-10). W3b-4 turns the
`.set-row` triplet into a `<SetRow>` component; 22 of its 40 sites are in `GeneralPanel.tsx`, the
file W4.1 rewrites. `<SetRow>` and `FIELDS` are two descriptions of the same rows, so whichever
landed second would have been written against the other. **Splitting it was considered and
rejected**: landing the descriptor without the loop ships scaffolding whose whole purpose is the
loop that consumes it, and the PR is the unit that tells one story. Two commits inside one PR keep
the behavior change readable, which is all the split bought. **The phase 8 counter dropped W3b-4
when this was decided**, so the item is counted once, here. W4.2's generator runs in-process
off `create_app(settings).openapi()`; an HTTP fetch needs a booted, authenticated server.

**W4.2 last is a risk call, not a cost one.** It deletes 1,239 hand-written lines and both mirror
counters, so phase 7's edits to those lines are thrown away either way. It goes last because a
generator lands against the smallest, most settled schema surface, and because W4.3's `Literal`
types (phase 7) must precede it.

### Phase 10 — issues that land here

**Not a wave, and the only phase whose items came from the tracker rather than from the audit.**
Eight open issues have to be fixed on this branch rather than on `dev`, because the branch moved or
deleted the code their fix sites name. A `dev`-side fix would be re-resolved by hand during the
weekly merge, against a diff nobody can review twice.

**[`docs/ISSUE_LANDING_PLAN.md`](ISSUE_LANDING_PLAN.md) holds the reasoning per issue, the
measurement behind each lane call, and the seventeen that wait for `dev` instead.** Read it before
starting one. It dies when #552 merges; these rows do not, so they say what to do and it says why.

**The rule that decided every row: measure the collision at the fix SITE, never at the file.** A
file the branch touched is not a collision; a file whose fix site the branch replaced, moved or
deleted is. `git diff origin/dev...HEAD -- <path>` and read the hunk headers against the line the
fix needs. That rule moved five rows between lanes on review, in both directions.

| # | Issue | Why it must land here |
| --- | --- | --- |
| 1 | **#682** | **Landed at #692.** The branch's own hunk replaced the exact `check=` string |
| 2 | #691 | `Status/Need More Info`. #692's armed-executor rig against a stub Sonarr is what settles it, which is the reason it belongs here. Confirmed means one copy fix; unreachable means close `Reviewed/Invalid` and write the refutation |
| 3 | #624 | The strongest collision of the eight: the branch replaced the whole `lsStatus` closure, five lines becoming thirty, and moved `LeavingSoonRow` into `JobsPanel.tsx`. The defect survives the rewrite, and the issue's own "Where" names lines that no longer exist |
| 4 | #584 | `_sync_libraries` moved out of `api/settings.py` into `api/plex.py`, and `test_settings_api.py` changed inside the class the test would go in |
| 5 | #598 | The branch removes two entries from the same `functions=` tuple this fix appends to, and re-points two other zones' `module=` at files the proposed drift check has to read |
| 6 | #685 | Not because the test file is long. **Both of the gate's inputs moved**: `.claude/rules/*.md` and `CLAUDE.md`. A gate written against `dev`'s scopes asserts the wrong pairs the day it lands |
| 7 | #558 | All four fix sites of half one were rewritten onto `buildinfo.env_flag`, and `_TRUE`/`_FALSE` deleted from both modules. Half two is genuinely untouched and could go either way; doing both here keeps one issue in one place |
| 8 | #622 | **A safety consequence.** The branch added a THIRD fatal stderr-only refusal (`preflight.main` writing `schema_gate.refusal`). A fix written on `dev` covers two and leaves the branch's third invisible on a frozen desktop build, which is the fail-open class the issue exists for |

**Three issue bodies this branch invalidates, fixed here because the branch created them.** #554
sends a reader to `engine/calibration.py` and #553 to `engine/backtest.py`; the branch deletes
both. The wrong-population lesson survives verbatim at `docs/LEARNINGS.md`, so #554's citation is
repointed there with the pre-deletion sha named. #553 also cites `api/settings.py` for the new-Plex-id
comment, which now lives in `api/plex.py`. **Do #554's repoint before #552 goes ready**: it is the
one that asks someone to open a file that will not be there.

**Nothing here is a wave, so nothing here has a `> Landed` block on a finding body.** Each item
closes its issue in the #552 merge, not before, since an operator on `dev` can still hit all of
them. #660 and #654 are the precedent for that wording.

### Review checkpoints

Fourteen places to stop and have the owner look. Each names what to check, because "review this
branch" at the wrong moment costs more than it catches. **Record the outcome in
[Progress](#progress)**: what was decided, not that it happened.

These are the owner reading a *decision*. They do not replace S9's `/reaper-review` pass on every
sub-PR, which is an agent reading a *diff*. The two catch different things and a `safety-path` PR
needs both.

| # | When | What to check | Rough size |
| --- | --- | --- | --- |
| C1 | Phase 0, now | The corrections. Every `> Corrected:` line, and whether any surviving finding still reads as safe to you | 30 min |
| C13 | **After phase 1, before phase 2** | The baseline's coverage and its redaction. What it captures is what every later phase is judged against, and what it misses is invisible for months | 30 min |
| C2 | After phase 2 | The measured before/after, that `crypto.py` is untouched, and that the injectivity guard fails when the mapping collapses | 15 min |
| C3 | Each phase-3 gate | Its hand-reconciled population count, against the members you believe exist. The red demonstration is evidence, not proof | 10 min each |
| C12 | After phase 4 | W10 item 3's fix, which changes what startup applies unless the two loops are reconciled deliberately, and the deliberate re-freeze of the baseline | 15 min |
| C4 | **Before phase 5 deletes anything** | The deletion list itself, plus PR 2's prose sweep, which no test covers. This is where a wrong call removes a feature, and four were already wrong | 45 min |
| C5 | **The alembic PR** | The migration against S2, driven: a fresh install's first settings save, and an existing tester's database. `alembic check` is silenced by the same PR, so nothing else catches this | 20 min |
| C6 | **Before phase 6 splits `api/routes.py`** | The four-way decomposition, including where *Vocabulary* goes and how the preamble is shared. Reversing it later is a second full session | 30 min |
| C7 | **Before W8-2 is built** | The proposed cap's location. It sits at `_run_out`'s serialization or the phrase decouples from the delete | 20 min |
| C8 | After W8-1 | Driven: the review queue, a show with a season row predating `show_status`, and the bulk bar's count | 20 min |
| C11 | **Before any published response field is dropped** (W8-3, W8-5) | The field list, as an API contract. An operator's script is not in this repository and no gate can see it | 20 min |
| C14 | **Before `plex.py`'s `_call` is built** | Its opt-out list: which of the four structural opt-outs plus the write shape stay bespoke, and whether the eight `except SafetyViolationError: raise` arms survive. That arm is entirely unpinned, so reviewing the diff afterward cannot tell you | 20 min |
| C9 | **Each phase-8 `safety-path` PR** | The interlock's behavior before and after, driven. Not the diff | 30 min each |
| C10 | Phase 9, before building | The `SETTINGS` table's shape, and which fields are declared exceptions | 30 min |

The table is ordered by when each fires, so **the numbers are out of sequence and that is
deliberate** — C13 and C14 were added after the rest and C12 moved with phase 4. Checkpoint
numbers are frozen for the same reason phase numbers are.

C4, C5, C7, C9, C11, C13 and C14 are not optional. Each guards something no gate in the repository
can see: a feature deleted, a tester's database, the confirmation phrase, an interlock, an external
contract, whether the baseline everything else leans on is worth leaning on, and an unpinned
refusal arm. The rest are worth skipping when the branch is small and the tests are honest.

C6, C7, C10, C13 and C14 sit **before** the work rather than after it, because in each case the
expensive part is the decision and reviewing the diff means reviewing a session that already went
the wrong way.

## The headline

**The codebase is not over-engineered, and its size is mostly earned.** Three measurements say so
and they should be read before any of the findings below:

- **The safety machinery is not redundant.** Every pass was asked to find interlocks that could
  merge. Across the executor, the two transport guards, the gates and the season pruner, the
  audit found **zero** genuinely duplicated protections and returned a 60-item register of
  complexity that looks removable and is not.
- **The test suite is not 1.4x its source.** Measured by lines it is. Measured by code, stripping
  docstrings, comments and blanks, Python tests are **0.82x** their source and the frontend is
  **0.45x**. Median test body is **12 lines**; only 8 of 3,164 exceed 80. The suite is not
  copy-pasted assertions: AST-exact duplicate bodies number **13** in 100k lines.
- **The comments are the record.** `identity.py` is 47% prose, `plex.py` 41%,
  `policy.py` 26%, and the frontend runs 31%. Nearly all of it is incident history citing issue
  and rule numbers. Rule 7/24 makes a comment naming a safeguard a checkable claim, and five
  comments that failed that check are filed as #550. The other several thousand passed.

The plan therefore targets four specific kinds of waste, of which only the fourth is large:

| Kind | Where it is | Rough size |
| --- | --- | --- |
| Unreachable code and unread state | scattered, 30+ sites, plus two whole engines | ~3,600 lines |
| Files that hold several unrelated jobs | 8 files, all self-declaring their seams | ~9,000 lines moved |
| One derivation written N times | ~25 clusters | ~1,400 lines |
| The declaration tax on adding anything | settings, wire types, cross-language enums | ~1,700 lines, and the real cost is elsewhere |
| Test scaffolding that was never lifted | fixtures, fakes, render helpers | ~3,300 lines and ~44s per run |

## Measured baseline

> **The Python line counts below and throughout are short**, by 0.2% to 16% against `wc -l` at the
> stated base. Backend Python measures 54,161, and `engine/policy.py` is 2,709 rather than the
> 2,263 the wave 2 table carries. Re-measure before quoting one (S5).

| Signal | Value |
| --- | --- |
| Backend Python | 51,123 lines, 108 files, 1,389 functions |
| Frontend TS/TSX | 34,339 lines, 105 files, 329 top-level components |
| CSS | 10,680 lines, 36 files, 91 custom properties, 850 class selectors |
| Python tests | 72,524 lines, 115 files, 3,164 test functions |
| Frontend tests | 27,774 lines, 77 files, 1,203 blocks |
| Functions over cyclomatic complexity 12 | 33 |
| Functions over 200 lines | 20 |
| Functions with 9 or more parameters | 22 |

The worst individual units, which is where waves 2 and 3 point:

| Unit | Lines | Complexity | Parameters |
| --- | --- | --- | --- |
| `engine/policy_warnings.py` `inspect` | 988 | 52 | 4 |
| `services/snapshot.py:570 scan` | 714 | 43 | 17 |
| `services/season_scan.py:1088 gather` | 478 | 34 | 25 |
| `services/planner.py:315 build_plan` | 375 | 27 | 5 |
| `services/snapshot.py:1447 _judge_item` | 111 | n/a | **27** |
| `components/PolicyEditor.tsx`'s `PolicyEditor` | 1,408 | n/a | n/a |
| `components/Settings.tsx`, pre-split | 3,086 | 65 hooks, 25 `useState` | n/a |

## Wave 1: deletions and mechanical wins

Nothing here changes what the app does. Each item is independently landable and most are a single
commit. This wave is the one to run first because it shrinks the surface every later wave reads.

### 1.1 Unreachable code

**Every row here has landed, and the `Site` line numbers are deliberately NOT re-anchored** — the
symbol is gone, so the number can only point at whatever moved up into its place. S10's obligation
covers citations to code that still exists, and every one of those was swept in the PR that moved
it. Thirteen rows landed in #597, `-i`'s poster chain in #600 and `-n` in #601; `-l` is killed, see
below. Read the *Landed* row before acting on a line number in this table.

| Site | What | Lines | Risk |
| --- | --- | --- | --- |
| `engine/fields.py:1016-1100` | `Mode`, `RuleSet`, `RuleSetResult`, `evaluate_rules`: a complete second lane evaluator with no importer in `src/`. Its docstring states the lane semantics as though it enforced them (part of #550) | 55 + 85 test | `none` |
| `engine/gates.py:390` | `GateConfig.gate` and `.enabled`: set at the one construction site, read by no gate | 15 + 18 test sites | `none` |
| `services/executor.py:809,811` | `exclusion_poll_attempts`, `plex_settle_attempts`: no caller passes either; each carries a `max(1, ...)` clamp guarding an input that cannot arrive | 8 | `none` |
| `services/grace.py:78,84` | `unknown_size_in_grace`, `unknown_size_ready`: summed, stored, read by nothing (#550) | 10 | `none` |
| `services/snapshot.py:1877` | `_as_year`: the third copy of a helper, and the only one with no caller | 9 | `none` |
| `services/snapshot.py:1582` | `_verdict`'s `override` parameter: the one call site passes `None` unconditionally, and 8 docstring lines describe the unreachable branch | 11 | `none` |
| `services/snapshot.py:202` | `RawItem.has_file`: constructed as literal `True`, read nowhere | 5 | `none` |
| `services/whitelist.py:242,337` | `is_spared`, `unspare`: superseded by `override_for` and `remove_override` | 12 | `none` |
| `services/season_scan.py:254,196` | `_SeriesWork.plan` (assigned, never read; the plan is recomputed) and `SeasonJudgment.poster_url` (never set) | 10 | `none` |
| `db/session.py` | `session_scope`: zero references anywhere; all 30 call sites open the factory by hand | 5 | `none` |
| `services/history_sync.py:542,550` | `state` (a bare forwarder to `_state`) and `latest` (test-only, superseded by `last_synced_at`) | 20 | `none` |
| `clients/tautulli.py:270` | `metadata`, plus the `get_metadata` entry in `READ_COMMANDS` that exists only for it | 12 | `none` |
| `clients/seerr.py:74,121` | `Requester.is_mappable` (#550) and `MediaRequest.is_available` | 15 | `none` |
| `api/whitelist.py:181,212,259` | Three routes with no frontend caller, two byte-identical to their `/api/override` siblings, plus `SpareIn`. `api.ts`'s keep-list comment ("three more ways to write the same safety-adjacent row") already records deleting the client methods for this reason | 70 | `behavior` |
| `styles/00-tokens.css:167`, `21-queue-cards.css:337`, `23-queue-chips.css:34` | `--radius-xs`, `.row-actions`, `.chip-reap`: no user in any construction path | 20 | `none` |

> **Corrected: W1.1-a's line range is wrong and holds live code.** `CustomProtectGate` sits at
> `fields.py:1016-1042`, inside the cited span, and is the whole user-authored-protection path
> (`services/scan_runner.py`'s `build_gates`, its one `gates.extend` and two `gates.append`). `evaluate_rules` ends at `:1104`. Delete the four named
> symbols, not the range. `tests/test_fields.py`'s dead blocks are `:127-217` and `:274-287`, with
> a live `TestCustomProtectGate` at `:239` between them. `docs/STATUS.md:69`'s "Flat AND of typed
> conditions" loses its only code expression and carries no dagger, so the reasoning survives
> nowhere unless the commit moves it.
>
> **Decided 2026-08-07 (C1): move it.** The deleting commit daggers that row and writes its
> `docs/DECISIONS.md` section, bumping `DECISION_SECTIONS` in `tests/test_repo_hygiene.py`
> (S6 and S7 both apply). A design choice whose last written trace is the code implementing it
> dies with the code, and this one is the shape of the whole rule lane.

> **Corrected: W1.1-k is false. `history_sync.state` is live.** `services/snapshot.py:617` calls
> it on every scan. Only `latest` is test-only. Applying this row as written breaks scanning.

> **Corrected: W1.1-i's `poster_url` has a reader.** `snapshot.py:1162` passes
> `judgment.poster_url` into `Display`, which reaches `Candidate.poster_url`. It is a three-hop
> chain, not a field drop. `RawItem.poster_url` is never assigned either, so the stored column is
> always `NULL` and `api/review.py`'s `_candidate_out` (`poster_url=(`) recomputes the poster and ignores it. Delete the whole
> chain or none of it. `_SeriesWork.plan` is confirmed dead and unaffected.

> **Corrected: W1.1-n is an external contract change, and two of its claims are wrong.** Only one
> of the three route pairs is byte-identical (`unspare_item` vs `clear_override`); `spare_item`
> takes a different schema and calls `whitelist.spare`; `GET /whitelist` has no `/override`
> sibling at all. `api.ts`'s keep-list comment ("three more ways to write the same
> safety-adjacent row") records deleting the *client methods* and explicitly keeping the
> routes, so citing it as precedent inverts it. `api/middleware.py:134-152` default-allows reads,
> so an API key reaches `GET /api/whitelist` today. Five test files depend on these routes
> (`test_general_and_logs.py:699,869,1050`, `test_override_truth.py:897,925`,
> `test_candidate_pagination.py:229,240`, `test_api.py:373,398`, `test_candidate_filters.py:266`).
> Deleting them also orphans `services.whitelist.spare` and `list_spared` (rule 64).
>
> **Landed in #601, and the file count was low by three.** The five files above call the routes;
> four more call `spare()` as *setup*, 32 times, and move to `set_override(..., decision="spare")`
> because the shorthand goes with its only production caller. A count taken off a production sweep
> understates a deletion every time the deleted thing was also a test convenience. Two of the five
> were parametrized over both delete routes precisely because the handlers were byte-identical
> (rule 72), and de-parametrizing them was the change rather than a symptom of one. `WhitelistEntryOut`
> stays, serving `POST /api/override`; its description named the "Spared" list and was corrected in
> the same PR (rule 64).

> **Corrected: W1.1-m's `is_mappable` deletion removes a prose claim, not a safeguard.** The real
> check is inline at `services/fairness.py:243`. Worth saying in the commit, since #550 is about
> exactly that class.
>
> **And it is bigger than two properties.** `is_available` was the only reader of
> `MediaRequest.status`, which was in turn the only reader of the `MediaStatus` enum, so rule 64
> takes all three: the portal already filters by availability server-side
> (`SeerrClient.requests(filter_="available")`), which is why nothing ever consulted the field.

> **Killed 2026-08-08: W1.1-l is false. `TautulliClient.metadata` has a live caller.**
> `scripts/validate_ingest.py:290` reads an item's `added_at` through it to re-derive the stored
> dormancy phrase against the source, one of that harness's five checks, and `docs/LEARNINGS.md`
> cites the harness by name. The method and the `get_metadata` allow-list entry both stay. The
> row's "no caller" was measured over `src/` alone; a committed, documented, runnable script is a
> caller.

**One trap, recorded here so it is not walked into.** `Profile.enabled` (`db/models.py:290`) is
also unread, and is deliberately kept: it is `NOT NULL` with no server default in the frozen
baseline, so removing the attribute leaves `alembic check`, a CI gate, reporting a pending
`drop_column` forever (#271). `PendingPlexLogin.pin_code` has the same shape. Both need the
`include_name` exclusion in `alembic/env.py:55` before the attribute can go. `SizeSource`'s three
never-written members are also *not* dead: the executor's growth interlock allow-lists them
exhaustively and fails closed, so deleting one narrows a fail-closed set (rule 143).

> **Corrected: the trap is deeper than autogenerate, and `include_name` is in `alembic/env.py`, not `:45`.**
> Excluding the column silences `alembic check` and does nothing about the `INSERT`. `enabled`
> works today only because the model carries a Python-side `default=False`, which is lost with the
> attribute; the column is then absent from the emitted `INSERT` and SQLite rejects it. The same
> holds for `PendingPlexLogin.pin_code` and for all three of W7-8's columns. Constraint S2 in
> [Execution](#execution) governs: a `server_default` revision first, the attribute second.
> `include_name` today filters tables and indexes only, so the column arm does not yet exist.
>
> **Landed as `e6f7a8b9c0d1` (#600), and three things the correction did not see.** A `NOT NULL`
> FOREIGN KEY cannot take a `server_default` at all -- `PRAGMA foreign_keys` is ON, so defaulting
> `Profile.active_policy_id` to `0` points at a policy row that does not exist and the insert
> fails, which is the very break S2 describes. It went nullable instead. A batch rebuild of
> `list_config` for an unrelated column silently recreated `name` case-SENSITIVE, because SQLite
> reflection does not report a collation; the behavioral test caught it and the diff never would
> have. And hiding a column from autogenerate does not hide its foreign key, so `include_name`
> needs a second arm or `alembic check` stays red. All three are in `docs/DECISIONS.md` under
> *Migrations*, because the next release-M author needs them and this document is not where they
> would look.

### 1.2 The two unreachable engines: delete both

`engine/backtest.py` (686) and `engine/calibration.py` (288), plus `tests/test_backtest.py` and
`tests/test_calibration.py` (657). **1,631 lines, no caller in `src/`.** Verified: nothing under
`src/` imports `engine.backtest` at all, and `calibration` is imported only by `backtest`, which
takes two names from it and then falls back to its own constant rather than calling `derive`.

The audit's first pass filed this as an owner decision between wiring them and fencing them off.
The owner's call is **delete**, and the reasoning is better than either option offered:

- **The backtest already banked its finding.** It is how Reaper learned that a size-weighted
  scorer "produced a condemned set with *worse* regret than picking at random among films of the
  same age" (`engine/signals.py:66`). That result is already in the shipped defaults: `SIZE` is
  off, and turning it on raises a danger warning. It was a lab instrument for *building* Reaper,
  not a feature an operator needs.
- **Neither engine is the shape the replacement features want.** Both successors below are
  cheaper, need no rewinding of history, and are honest by construction rather than by careful
  handling. Keeping 1,631 lines parked so that a future feature might reuse them is how the
  parking became permanent in the first place.

Two successors are filed as feature requests and are deliberately *not* attempts to wire this
code: **#553** (weigh a previously reaped title down, because its return is an observed regret)
and **#554** (probability of a future rewatch, over a long enough window to mean something).

**Before deleting, rehome one constant.** `FALLBACK_REWATCH_PRIOR` lived in `engine/backtest.py` and
is cited from `engine/gates.py:764` and `engine/policy.py`'s `DEFAULT_MOVIE_POLICY` ("the numbers come from
the measured rewatch curve"), plus `docs/SIGNALS.md:155`,
`docs/LEARNINGS.md:121` and `docs/README.md:64`. `engine/dormancy.py` is its natural home. Rule 64:
the doc citations move in the same change.

> **Corrected: five test files import these modules, not two, and the rehoming is the wrong
> resolution.**
>
> Beyond `test_backtest.py` and `test_calibration.py`, three more import them and go red on
> collection: `tests/test_engine_derivations.py:31`, `tests/test_review_scan.py:35,37,38` (three
> whole classes), and `tests/test_signal_quality.py:19` (about a third of the file). In
> `test_engine_derivations.py` only two of the eight tests in `TestDormancyIsDerivedOnce` touch
> backtest (`:183` and `:209`, the second self-described as a guard rather than a proof), and
> `test_fact_layer_states.py:163-172` and `test_policy.py`'s `test_the_clamp_that_makes_the_claim_true` pin the derivation too, so
> this is an excision, not a file rewrite.
>
> The constant's only reader is `rewatch_prior()`, in the file being deleted, and its only caller
> is `BacktestResult._expected_rates`, also in that file. Rehoming both to `dormancy.py` moves dead code together.
> The two comments citing it
> need the number's provenance, and `docs/SIGNALS.md` already tabulates the curve, so **delete
> both and repoint the two comments at `SIGNALS.md`.** `LEARNINGS.md`'s citation is at `:122`, and
> there is a **sixth** at `:796` the row misses.
>
> `SIGNALS.md` needs two sections corrected, not one: "How to check any future signal"
> (`:127-143`) is entirely the backtest's lift metric. The sweep in `src/` is 24 comment sites.
> `.claude/rules/backend.md:28` and `:137` both become false statements about the tree, and rule
> 35's builder list is already wrong: `calibration.py` builds no `Facts` at all.
>
> `docs/DECISIONS.md` needs no edit. The dagger test walks only rows under `## Decisions locked`,
> and M3c/M3f/M3g are milestones.
>
> **This collides with *Owner decisions* item 1**, which cites `BacktestResult.lift`'s docstring as one of
> three reasons to keep `AutonomyGrant`. That citation dies here, and the surviving
> `backtest_passed` column plus its CHECK constraint then name a feature with no code and no
> successor issue (rule 25).

**And correct the roadmap in the same commit.** `docs/STATUS.md` carries M3c, M3f and M3g plus
open work item 2, all of which describe wiring that is no longer going to happen. `SIGNALS.md`'s
"Your library is not this library" section promises a per-operator prior that `calibration.derive`
would have fitted; it must say the curve is borrowed, full stop, until #554 ships.

Removing these also retires a standing rule-35 tax: every new `Facts` field currently has to be
spelled in both modules' builders.

### 1.3 Test-suite wall clock, for two lines

`tests/test_repo_hygiene.py`'s `_repo_text_files()` did a full `rglob` plus `read_text` of
every file in the repository and is called from **7** sites; `_uvicorn_launches()` re-enters it.
(The `rglob` is gone as of #690, which reads the file list off `git ls-files`. The caching below
is what this section proposed and it landed unchanged.)
The file takes **53.04s**, and its 7 slowest tests, all callers, are **50.2s** of that. There is
no `functools` import in the file.

Adding `@lru_cache` to `_repo_text_files()` and `_source_files_to_scan()` is **+2 lines for ~44s
per run**. This is the single best value-to-effort item in the audit.

> **Confirmed by measurement, and safe.** 51.14s to 8.63s, 69 passed and 1 skipped either way.
> The staleness question is clean: all 11 call sites build a new list, none sorts or appends in
> place, and no test in the file writes a file (no `write_text`, `mkdir`, `tmp_path` or
> `monkeypatch` in its 3,652 lines). No defensive copy needed. `tests/_policy_lab.py:18` already
> imports `lru_cache`, so the idiom is precedented. One cost worth knowing: the cache pins ~23 MiB
> of text per xdist worker for the session.

### 1.4 Test scaffolding that was never lifted

The suite already shares `tests/_auth.py`'s `login()` across 28 files, so the pattern is accepted;
the layer beneath it was just never built.

- **No app or DB boot fixture.** `tests/conftest.py` ends at 244 lines without one. The sync boot
  appears **44 times in 25 files**, the async boot **27 times in 23 files**, and
  `TestClient(create_app(...)) + login(...)` **32 times in 19 files**, seven of them byte-identical.
  `Settings(data_dir=tmp_path...)  # type: ignore[call-arg]` is written **131 times**.
  Proposal: `settings`, `sync_db`, `async_factory`, `client` in `conftest.py`. **~1,000 lines**.
- **No `renderWithProviders`.** Grep returns **0**. There are **87** `<QueryClientProvider>` trees
  across 35 files, **95** `testQueryClient()` calls and **55 file-local render helpers in 32
  files**, four of them near-verbatim copies and two character-identical. **~800 lines**.

  > **Landed, and the duplicate-helper half does not reproduce.** All 87 trees are on
  > `src/test/renderWithProviders.tsx`; the counts of 87 and 55 were both exact. What is left is
  > **one** hand-built provider, in `useScanSettled.test.ts`, whose `Announcer` has to be a
  > sibling ABOVE the hook rather than a wrapper around it, and it says so where it stands.
  > Net 217 lines out of the test files against 77 in the helper, well under the ~800: the
  > estimate counted the 55 helpers as deletable, and they are not. Each holds the props its
  > own file passes, which is what a file-local helper is for — the same line #575 drew around
  > the bespoke `client` fixtures that SEED. Hashing all 55 with their own names normalized away
  > finds **zero** identical pairs, and the same pass over the pre-change tree finds zero too, so
  > "two character-identical" is not a claim this pass could confirm or act on.
  >
  > Two things worth having came out that the finding did not predict. `rerender` now keeps its
  > providers, because the provider is testing-library's `wrapper` rather than part of the
  > element: five call sites were re-rendering into a *fresh* `QueryClient`, which is a cache
  > swap no running app performs. And `test_repo_hygiene.py`'s rendered-surface walk matched
  > `\brender\(` alone, so 29 of its 53 files left the walk in one commit — rule 147 exactly,
  > caught by the pinned count beside it, and the matcher now reads every spelling.
- **67 structural fakes, 60 of them declaring no base class**, behind **65**
  `# type: ignore[arg-type]` suppressions. mypy is `strict = true`, so those suppressions are the
  only reason a real client signature change does not fail the build, and the fakes have already
  drifted from each other. Six independent Tautulli fakes, seven Sonarrs, four Seerrs. Proposal:
  one `tests/_fakes.py` per client, each inheriting the real class. **~600 lines and 65
  suppressions**, and it is strictly stronger than what it replaces.

  > **Landed, and the stated mechanism was wrong in a way that changes the deliverable.** The
  > suppressions were not "the only reason a client signature change does not fail the build."
  > They were inert: CI runs `mypy src/reaper`, so no file under `tests/` was type-checked at
  > all and a `# type: ignore` written there suppressed nothing. Probed both directions rather
  > than argued. Adding one keyword-only argument to `TautulliClient.children_metadata` and
  > updating its production caller turned **44 tests red** with `TypeError` while
  > `mypy src/reaper` stayed **green** — so a change to a method's *shape* was already caught,
  > loudly, by the suite. What nothing caught was a change to a *type* alone. So inheriting the
  > real class buys nothing on its own; it buys the type check only once the module is **on the
  > mypy run**, which is the second half and the reason this landed as
  > `uv run mypy src/reaper tests/_fakes.py`. A probe fake with a drifted parameter type is
  > reported as a Liskov violation under exactly that command and under nothing else.
  >
  > **The dedup half is real for Sonarr and Seerr and wrong for Tautulli.** Four Sonarr fakes
  > were character-identical apart from their class name, and five copies of one `_Unreachable`
  > subclass sat in `test_protection_sync.py` alone. But the two Tautulli `history` fakes are a
  > paging simulator and a per-key router with error injection: merging them yields a class
  > whose behavior no caller could predict from its arguments, so they stayed two classes,
  > `FakeTautulli` and `PagingTautulli`. **15 fakes retired into 5**, 84 suppressions gone
  > against the 65 predicted, and 677 lines out against 449 in.
  >
  > **`test_reap_loop.py` is deliberately not in it.** Its four deletion-path simulators are
  > each tuned by several subclasses declared beside them, and moving a simulator away from the
  > tests dictating its failure modes is the wrong trade on the one path that deletes. They are
  > the largest remaining fakes and the obvious next candidate; phase 8 should decide that with
  > the arr client change in front of it, not now.
- **No complete api mock.** 37 frontend files `vi.mock` the api module; only 16 import
  `apiFixtures`. Each redeclares its own `vi.fn()` set, ranging from 1 function to 32. Rule 135
  requires the mock to answer everything the tree reads, and the fixtures supply payloads but not
  the function list, which is why the count drifts. **~500 lines**, and it closes rule 135's gap
  by construction. Risk `coverage-loss`: a file relying on an *absent* mock to reject a read would
  start getting an answer, though `frontend/src/test/setup.ts:97` still fails the run on a genuine
  gap.

  > **Landed, and the `coverage-loss` risk does not survive its own caveat.** All 35 hoisted
  > mocks are on `src/test/apiMock.ts`'s `makeApiMock()`, 94 functions, checked against
  > `Object.keys(api)` in both directions. The risk needed the two shapes separating: a tree
  > reading `queryFn: api.foo` gets `undefined` from an omitted key, which `setup.ts` **already
  > fails the run on** — so no passing test relies on that absence and there were none to break.
  > A tree reading `queryFn: () => api.foo(x)` renders its failed-read branch silently either
  > way, an omitted key throwing a `TypeError` inside the arrow and an unconfigured `vi.fn()`
  > resolving `undefined`. Same branch, same silence, which is rule 135's own documented blind
  > spot and unchanged.
  >
  > **The mutations were the half nothing watched, and that is the stronger case for this than
  > the one the finding makes.** `setup.ts` fails a query with no `queryFn` and says nothing
  > about a mutation with no `mutationFn`, because React Query does not announce that one.
  > `removeApiKey` was missing from the General panel's mock until the Remove path needed
  > driving, and it went missing silently. For every mutation in the module, completeness is not
  > a tidier spelling of an existing gate; it is the only thing between a gap and a confusing
  > failure.
  >
  > 390 lines out against 82 in, plus 172 in the shared mock and its drift test. The
  > `vi.hoisted` idiom and all 784 `apiMock.x.mockY()` call sites are untouched: `vi.hoisted`
  > takes an async factory, so the shared one is reached with an `await import` inside it, which
  > was probed before anything was rewritten.

### 1.5 Four tests that a linter runs or that cannot fail

`test_no_bare_exception_assertions_in_tests` (in `test_repo_hygiene.py`) greps for
`pytest.raises(Exception)`; **ruff B017 is enabled, runs in CI, and is strictly broader**.
`test_instruction_files_exist` filters a list built from a glob for absent files, which a glob
cannot return. `test_the_select_name_matcher_rejects_what_it_claims_to_reject`'s case at `:3116`
can only fail after the test above it. `test_the_tagline_sites_all_exist` reads the tuple
`:2230` already read. Rule 118: **~40 lines**.

> **Corrected: two of the four are wrong and one is a deliberate diagnostic. Only W1.5-c survives.**
>
> **B017 is not strictly broader.** It *exempts* `pytest.raises(Exception, match="…")`, which the
> repo's grep bans through its `,` branch. Deleting the grep legalizes that form suite-wide, and
> rule 119 asks for the domain error *and* its message, not a match on a blind `Exception`. The
> two overlap; neither contains the other. Keep both, or widen the grep to cover B017's multi-line
> and `BaseException` cases first.
>
> **`test_instruction_files_exist` can fail.** `INSTRUCTION_FILES` is
> `[REPO / "CLAUDE.md", *glob(...)]` (`:66`) — the first element is a literal, so `missing` is
> non-empty the moment `CLAUDE.md` moves. It also carries a second assertion on the count.
>
> **`test_the_tagline_sites_all_exist` exists to fail cleanly.** `:2230`'s `read_text` raises
> `FileNotFoundError` on a renamed site; the docstring names that as rule 118's "fails for the
> wrong reason" case. Cutting it trades a named diagnostic for a stack trace.
>
> **W1.5-c holds, and is stronger than stated.** `_select_is_named` returns at `:3005` before
> reading `text`, so `:3116` and `:3110` drive the identical branch with the identical tag. The
> comment at `:3115` about "both spellings" is orphaned from the loop at `:3125` and should go
> with it.

## Wave 2: files that hold several unrelated jobs

Pure motion. No signature changes, no behavior changes, ~9,000 lines relocated and none removed.
Each of these files draws its own seams already, in banner comments or in the fact that its
**tests are already split** along the boundary the source is not.

| File | Now | Split | Evidence the seam is real |
| --- | --- | --- | --- |
| `api/routes.py` | 2,789 | `api/review.py`, `api/policy.py`, `api/simulate.py`, `api/vocabulary.py`, `api/about.py`. `routes.py` ceases to exist | **Landed, on C6's five modules and its `:1853` cut.** The file was 2,827 at this base, not 2,789. Out: review 1,469, simulate 854, policy 443, vocabulary 114, about 72 — 2,952, so the tree gains 125 across five headers and five import blocks. **The invariant holds exactly**: 96 operations over 79 paths, and every `(method, path, operationId, tags)` tuple identical to the base, compared by building both documents in-process. `main.py` grew four `include_router` calls. `_EXPECTED_LAYERED_MODULES` 79 to 83; the logger count 48 to 50, not 52, because `vocabulary.py` and `about.py` log nothing and a split inherits its parent's logger only where it inherited something to say |
| `engine/policy.py` | 2,263 | `+policy_migrations.py` (~530), `+policy_warnings.py` (~1,030) | **Landed.** The file was 2,710 at this base, not 2,263; the correction's 2,709 was right at `759507b` and one line arrived after it. 1,577 lines out, 2,710 to 1,133: `policy_migrations.py` is 572 and `policy_warnings.py` 1,083, so the tree gains 78 lines, the two module headers plus the comment rewraps the longer module names forced. Measured at the tip, not at the first commit: three follow-up commits moved every one of these figures. Both halves import the model and neither is imported back, measured rather than argued: migrations reads `PolicyBody` and `SCHEMA_VERSION` alone, warnings reads six model types and `join_and`. `join_and` stays in `policy.py`, because `scan_runner` joins repair remedies with it and that is not a warning. The pinned module count goes 77 to 79; the logger count does not move, since a split inherits its parent's loggers |
| `components/Settings.tsx` | 3,086 | 7 panels to their own files; the barrel keeps `PANELS`, the dirty record and the shell | **Landed, and it is 7 panels as the correction says, not 6.** 2,847 lines out, 3,086 to 239; the seven panels hold 2,934 between them, so the tree gains 87 — eight module headers and import blocks, less the seven banner comments they replace. 2,790 of the 2,797 moved lines are byte-identical; seven changed, becoming eight. Two are the `export` `JobsPanel` and `BackupPanel` take for the shell, and five are comments rule 72 re-pointed: `ServicesPanel`'s remove toggle ("mirroring the arm confirm in `DeletionToggle.tsx`") and its announce ("the webhook's in `NotificationsPanel`"), and `JobsPanel`'s `scanScheduleText` ("the same split `JobsPanel`'s own `StaleReadSlot` takes"), each of which named a neighbor that had stopped being one. `PANELS` is kept byte for byte; the shell is bar three stale counts the review found in it, wrong on `dev` since the tenth section arrived. Both pinned per-file populations conserve exactly, 8 query-failure handles to 8 and 6 reload sentences to 6, which is what says no branch moved. `isDiscordWebhook` and `MIN_ADMIN_PASSWORD` re-export from the barrel on `PlexPanel`'s own precedent, so `DiscordModal` and `SetupPasswordStep` are untouched |
| `api/settings.py` | 2,025 | `api/plex.py` (~630, 12 routes) | **Landed, and it is 14 routes as the correction says, not 12.** 698 lines out, settings 2,044 to 1,344. The **sorted** document is byte-identical either side, same 96 operations; the one thing that moves is `paths` insertion order, which nothing reads. Reported as plain "byte-identical" first, which is the reassuring direction rule 144 warns about. `plex.py` **imports** the request accessors from `settings.py` rather than copying them, so phase 8's `api/deps.py` still collapses five copies and not six |
| `components/PlexPanel.tsx` | 1,244 | 3 sections out (~450) | **Dropped from phase 6, filed as #607, and read the correction below before this cell.** It claimed the file draws the seams as banner comments; the banners are inside the function body. The rule-146 dirty contract half is the part that holds |
| `App.tsx` | 1,225 | 5 components to `components/` (~520) | **Landed.** 506 lines out, 1,225 to 719: 497 of them the five component spans, 9 more the import header that went with them. The row first said 728, which is 1,225 minus 497 and self-consistent, which is why it read as measured. Two carry the "exported for its tests" comment, as the correction says, and both were false on arrival: in `components/` every file exports its component, so the export carries no signal, and `WhyPanelFallback` is no longer "below". `NAV` moves with `SectionNav`; `ReapSheetLoader` does not move, since `Dashboard` renders it and not `ReapBar`. The query-failure map moves App 8 to 7 and gains `SectionNav` 1, total conserved |
| `components/ReviewQueue.tsx` | 2,654 | `QueueFilterBar` (~330), `queueChips.tsx` (~60), delete the re-export shim | **Dropped from phase 6, filed as #606, and read the correction below before this cell.** It claimed the filter block never reads `override`, `verdict` or a candidate; it reads the tab `verdict` at six sites. The shim's own comment calling itself transitional is the part that holds |
| `services/season_scan.py` | 2,060 | `guard_result` + `no_key_reason` to `season_evidence.py` (~145) | **Landed.** Both are pure, and `api/routes.py` imported the 2k-line I/O module solely to call `guard_result`; that import is gone. The move was 152 lines and the served OpenAPI document is byte-identical either side |

**One caveat that applies only to `routes.py`.** Roughly ten cross-module comments cite
`api.routes._chip`, `api.routes.simulate`, `api.routes._season_guard_replay` and
`api.routes._explanation_out` by dotted name, and five test files import its internals. No hygiene
test guards a dotted symbol citation the way `test_docs_referenced_from_code_exist` guards a doc
path. Fixing the comments is part of the change (rule 64); adding that guard is worth considering
in the same commit.

> **Landed. The row's "roughly ten" is low; the correction's 39 was exactly right.** 43 dotted
> citations existed, 4 of them in this very paragraph as history, so **39 across 26 files** were
> re-pathed — the correction's figure, over its 9 symbols, with `_chip` at 16. Six test files
> import internals, not five.
>
> **This block first said 43 across 27 and called the correction an undercount that "counted
> `src/` while a third live in `tests/` and `docs/`".** Both halves were wrong: 43 is the base
> population including the four sentences below, and the 39 already spanned src 22, tests 10,
> docs 5, frontend 2. A verified measurement was used to overrule a correct one, which is rule
> 144 running backwards — the reassuring direction here was believing the sweep had been bigger
> than the correction predicted. Caught in review, and the reconciliation is the useful record:
> the two numbers count different populations and both are right.
>
> All 39 re-pathed mechanically off the new modules' own symbol tables, so the mapping is the
> tree's rather than a reading of it. **The guard this paragraph says is "worth considering" was
> written**, and reviewing it found it covered one of the three spellings prose uses: `api/routes.py`
> and bare `routes._chip` account for 25 more citations it could not see, and a `.` in its
> lookbehind silently dropped every `:func:`reaper.…`` form. All four spellings are now driven red.

**Not recommended:** splitting `db/models.py` (1,066 lines, roughly two thirds per-column prose
explaining what a NULL means) or `engine/identity.py` (47% prose, and rule 3 is better served by
`resolve` staying beside its narrowers). Both move lines without removing any and scatter the
reasoning.

> **Corrected, wave 2. Two rows are not pure motion and the `routes.py` evidence is wrong.**
>
> **`api/routes.py` does not draw the seam the row claims.** Its four banners are *Snapshots and
> candidates* (`:151`), *Policy* (`:1467`), *Vocabulary* (`:2696`) and *About* (`:2781`) — there is
> **no simulate banner**, and *Vocabulary* has no home in the proposed set. The preamble
> (`:16-137`, ~122 lines of imports plus the `APIRouter`) is shared four ways, which costs lines
> and buys four coherent modules — the right trade under S5, and worth saying because the wave's
> own "~9,000 lines relocated and none removed" invites the opposite reading. Decide where
> *Vocabulary* lands (C6); the line arithmetic is not the open question.
> The rule 64 checklist is **39 comment citations across 9 symbols** and **6 test files**,
> not "~10 across 4" and 5. Everything else about the row is safe and better than argued: no
> hygiene test pins route-to-module, there are zero `operation_id=` overrides, the router carries
> no router-level tag, and `test_openapi_tags.py` keys on method and path, so the served document
> is unchanged by construction.
>
> **`ReviewQueue.tsx`'s filter block does read the tab `verdict`**, at six sites, and owns the
> search state feeding the primary `candidates` query. `QueueFilterBar` is a lift-and-thread of a
> dozen values that must also *return* `activeDimensions`, `addableDimensions` and `filtering`.
> The narrow reading — it never reads a *candidate's* `override`/`verdict` — is true and is not
> what the row says. Rules 48-50 and 120-123 are genuinely untouched. Two of the shim's four export
> lines (`:2652`, `:2653`) are **already dead** and deletable today with zero edits; the other two
> need four files touched.
>
> **`PlexPanel.tsx`'s banners are inside the function body**, marking regions of hooks rather than
> top-level functions, so "3 sections out" means three new child components with state re-threaded.
> It is the one row that is not motion at all. The rule 146 claim holds, and the new hazard is that
> extracted children could introduce early returns, which is rule 146's own failure mode.
>
> **`Settings.tsx` is 7 panels, not 6**, and the tests are split for **5**, not 6 — `JobsPanel` and
> `BackupPanel` are unexported and have no per-panel test, so the seam is asserted for those two,
> not demonstrated. The barrel must also keep `export { PlexPanel }`, `isDiscordWebhook` (read by
> `DiscordModal.tsx`) and `MIN_ADMIN_PASSWORD` (read by `SetupPasswordStep.tsx`), and
> `ScheduleModal` moves with `JobsPanel` as one unit. **All of this held on landing but the
> `PlexPanel` re-export, which turned out to have no caller at all.** `SetupWizard` stopped
> importing it when #384 broke first-start into four steps, so the barrel carried a name nothing
> read for a year, behind a comment still naming that caller — which is why the correction says
> "must keep" and why this row said so too. Deleted here, not carried. The two
> panels are exported now because the shell imports them across a file boundary, which is a
> consequence of the split and not a test; they are still driven only through the shell, so that
> half of the correction survives the change that answered the rest of it.
>
> `App.tsx` carries **two** "exported for its tests" comments, not three. `api/settings.py` has
> **14** PLEX-tagged routes, not 12, and the tag is a clean cut. `engine/policy.py`'s two split
> sizes are exact, which leaves a ~1,139-line residual rather than the ~700 the row implies.
> `season_scan.py`'s move is confirmed pure and cycle-free, at ~50 edit sites.
>
> **The *Not recommended* note argues half a case.** "Both move lines without removing any" is not
> a reason against anything (S5); "scatter the reasoning" is the whole argument, and it holds on
> its own for `db/models.py` and `engine/identity.py` alike. Its two prose ratios are also
> unreproducible — a naive count gives 208/1,066 and 214/1,395.

## Wave 3: one derivation written N times

Rules 72, 104 and 144 are the same obligation at three scales: a copied function, a value derived
twice, a sentence stated twice. These are the clusters where the copies exist and, in several
cases, **have already drifted**. Ordered by drift risk rather than line count, because the value
here is preventing a future divergence.

**Already drifted, fix first:**

- `clients/arr.py` — 11 copies of one shape guard, and a **verbatim 3-line comment repeated 6
  times** saying "this is the same defect (rule 72)". `SonarrClient.exclusions` and
  `RadarrClient.exclusions` are byte-identical. The file is 122 code lines carrying 55 of
  duplication. Risk `behavior` (rules 28/93: a coerced `[]` once read an auth-proxy error page as
  an empty library), fully pinned by a 7-case parametrize.

  > **Landed, #652.** `BaseClient.get_list` and `get_dict`, which the six sibling guards in
  > `clients/seerr.py` adopted in the same commit.
- `services/executor.py:2156,2387` — the size interlock written twice, and the season copy has
  grown an empty-list guard the movie copy has no analogue for. Extract the growth branch only;
  the unreadable-size branch's copy genuinely differs per path and rule 21 wants that. Risk
  `safety-path`, 8 pinning tests.

  > **Killed as written at #683; the rule 144 half lands instead.** The extraction was built
  > twice and measured, not argued from the diff. Extracting the reason sentence alone is +7 lines
  > (13 in, 6 out) and splits one operator sentence into a template with two interpolated
  > fragments, so neither sentence can be read whole anywhere (rule 21). Extracting the whole
  > branch, which is what the row asks for, is +9 lines and returns `StepOutcome | None`, so each
  > call site carries a sentinel check the send path did not have before. **That sentinel is the
  > measurement that decided it**: dropping the check at one call site leaves 4,234 tests passing
  > and fails exactly one, `test_an_upgraded_season_is_kept_before_anything_is_sent`, on
  > `assert [(42, 3)] == []` — a real unmonitor reaching Sonarr. One test out of 4,235 stands
  > between a forgotten `is not None` and a mutation, to save nothing: `_grew_materially` is
  > already the one declaration of the predicate, and it is the only part that can drift.
  >
  > **What was actually duplicated is a sentence, and the row exempts the branch holding half of
  > it.** Two `check=` strings are byte-identical across the two paths, not one:
  > `"It grew since you approved it. Kept."` and `"Couldn't confirm its current size. Kept."`,
  > the second sitting in the unreadable-size branch the row rules out. The *reasons* beside them
  > do differ per path and the row is right about those. Both sentences become module constants
  > beside the file's existing `_NO_APPROVED_SIZE_CHECK`, which is the shape this file already
  > uses for exactly this.
  >
  > **The season's copy was unread by anything**, which is why a grep finds it in `executor.py`
  > alone: its test asserts the files survive, never the message. All four sites are pinned now,
  > each driven red on its own by swapping its constant for a different literal, and each names a
  > different test.
  >
  > **The empty-list premise is real and narrower than the row states, measured by driving it.**
  > For a *measured* item the two paths already agree: `_payload_size` reads a movie's absent or
  > zero `sizeOnDisk` as unreadable and the guard above the growth check keeps it, which is the
  > movie's analogue. They diverge only under the unmeasured allowance, and only on one case.
  > Driven, deletion armed, real HTTP: an unmeasured movie whose Radarr reports no size is
  > DELETED; an unmeasured season Sonarr lists no files for is KEPT. But an unmeasured season
  > whose files Sonarr lists and will not size is DELETED too, so the season's unconditional guard
  > covers "the server listed nothing at all" and nothing wider. A movie has no list, so
  > `_payload_size` folds that case and "listed but unsized" into one `None` and the movie path
  > cannot tell them apart from one field. Making its guard unconditional is one clause wide and
  > was driven: it keeps the unmeasured movie. It also keeps the population the allowance exists
  > to reap, since a movie's only comparable source is the same Radarr `sizeOnDisk` the scan
  > failed to read. That is a decision about the allowance's contract, for the owner, not a
  > defect to fix under a dedup.
- `WhyPanel.tsx:1316` vs `ShowPanel.tsx:69` — the same panel head, written twice and diverged in
  two places. The divergence should be **decided**, not inherited.

  > **Corrected: the `↗` glyph is not what differs, and there are two differences rather than
  > one.** The `↗` is `JumpPill`'s, which both heads already share, so this row named the one
  > character in the block that was never divergent. Measured, the two differ by: the item
  > panel's title link carrying an inline "open in new" SVG (`className="title-ext"`, a 16x16
  > `viewBox`) that the show panel's does not; and the pill row being ordered Tautulli, Seerr,
  > Radarr, Sonarr on `WhyPanel` against Sonarr, Tautulli, Seerr on `ShowPanel`. Everything else
  > that differs is each panel's own content and stays that way: item bytes and media label
  > against "TV show, N seasons", and `MergedListingChip`/`MetaLine`/`RatingsRow` against
  > `Synopsis`/`StatusChip`/reason/`MatchCandidates`/the Seasons section.
  >
  > **Landed, #679.** Owner decision: unify both and extract the head. `PanelHead` in
  > `WhyPanel.tsx`, rendered by both panels with their own `sub` content.
- `clients/seerr.py:306-419` — the paging contract written three times; its own test is titled
  "Rule 72: the same loop, twenty lines down". `plex.py`'s `_iter_pages` is the complete-or-raise
  helper rule 56/89 names; Seerr never got one.

  > **Landed as the backstop, #653, not as a helper.** W6-3's correction governs both rows: one
  > `paged()` cannot serve loops with different failure contracts. The contract is still written
  > twice and that is the decision, not an omission.
- `services/scan_runner.py`'s `build_sources` (three `verify=r.verify_tls` arms) and
  `build_reap_gateway` (three `verify=row.verify_tls`, which is why grepping the scan spelling
  misses it) + `services/instances.py:603` — per-kind client construction
  in three places, and `instances.py:618` already records the drift incident (`api_path_prefix`
  reached the scan but not Test Connection).

  > **Landed as the gate, #655, and no shared constructor was built.** Measured first: all six
  > calls pass all three arguments today, so there is nothing diverged to fix. The row's value is
  > preventing the next divergence, and a helper only binds the sites that call it while the gate
  > binds a seventh site written by someone who never read this row. CLAUDE.md's "write the gate
  > instead" is the tie-break. The two addresses above were stale by +21 before this touched
  > anything; re-anchored here. `build_sources` builds **five** clients, not three: Tautulli
  > spells it `verify=tautulli_row.verify_tls` and Plex `verify=plex_verify`, which is the same
  > trap the row names one level down.

**Largest by volume:**

- `clients/plex.py` — **21 methods repeat the same off-thread plus error-map wrapper**
  (24 `to_thread` sites, 19 identical `except` arms). One `_call(fn, *, what, lock)` helper, with
  three documented opt-outs that must stay bespoke. **~100 lines**, risk `safety-path`, 32 pinning
  assertions.

  > **Landed at #676, as C14's option 5.** Every count in the row is wrong and C14's measurement
  > already said so: 23 `to_thread` sites not 24, eight byte-identical arms not 19, five
  > structural opt-outs not three, ~50 net lines not ~100. There is no `lock` parameter: the two
  > callers holding one hold it around the `_call`, not inside it. **The 32 pinning assertions
  > were not needed and would have been the wrong shape**: the eight arms were pinned first, at
  > #659, and the proof is that those eight tests stay green with the arm in one place and go
  > red with the nine-way demonstration when it is deleted.
- `services/scheduler.py` — **7 copies** of "run the job, record the outcome, swallow the failure",
  plus an eighth inner half in `services/leaving_soon.py`'s `_record_skip`. One decorator. `refresh_curated_lists`'s
  docstring currently has to *state in prose* that every exit records a run, which is a guarantee
  a decorator holds structurally. **~55 lines**.

  > **Killed: the decorator. The prose guarantee is the real finding, and the shape holds it
  > once wave 11's W11-32 comes out of the same function.** Re-measured at `9b7e625`, by AST
  > rather than grep. `_record_run` has **15 call sites, not 17**: the fifteen already listed
  > are the whole population and no sixteenth hides inside a block. Of the seven jobs a
  > decorator would wrap it fits **four**. Two record nothing by design, both "Not
  > operator-schedulable", and `scheduled_scan`'s two quiet skips must stay unrecorded, because
  > a "scan already running" row would hide the last scan behind "Scan failed". `session_factory`
  > sits at positional 1, 2, 2 and 3 across the four and is `| None` in three of them, so the
  > wrapper reaches it only through `inspect.signature().bind()`. **Net lines are about zero**:
  > 27 come out across the five recording jobs and about 25 go back as the factory and its
  > applications. The five catch-all comments each state a DIFFERENT reason that swallow is
  > safe (the load swaps atomically, rule 115 retires only on a landed sync, the lookup and the
  > decrypt are inside the try, the checker maps its own network failures, a quiet skip is not
  > a crash), so all five stay at the sites whatever wraps them. Three exceptions for four
  > uniform instances is five rules where four instances do it, and it buys reflection on the
  > scheduler path plus a swallow a reader of the job can no longer see.
  >
  > **The two inner handlers are the duplication that was real, and they were byte-equivalent
  > to the caller's**: same log event, same `ok=False`, same "Couldn't refresh lists", then a
  > `return` from inside the `AsyncExitStack`. Both deleted. **14 lines, not W11-32's ~12**, and
  > nothing in the unwind path suppresses, so raising past them records the same row from one
  > frame up. "Every exit records a run" is now a shape, one call under one catch-all, rather
  > than a sentence. **`leaving_soon.py`'s `_record_skip` is the WRITER**, `_record_run`'s twin,
  > and wants nothing: its wrapper `after_scan` calls it at three sites with three reasons on
  > three typed arms. That docstring's "Every skip below is written down" was contradicted by
  > the comment three lines under it, and now names the one skip that is not. Landed at #698.
  >
  > **The partial loses too. 2026-08-10 is the first time it was costed.** The kill above judged
  > the decorator all-or-nothing over seven, and nobody had priced decorating only the four that
  > fit. Built for real, formatted, mypy-clean and green on every test in `test_scheduler.py`, 37
  > of them at the commit it was measured on, it comes to **+21 total lines, +13 non-comment
  > lines and +5 statements** against an estimate of about zero. `inspect.signature().bind()` is
  > still required after the narrowing, because three distinct positions survive it and
  > `_maintenance_specs` adds every job positionally.
  >
  > **The drift question answers the same way, and the line count could not settle that.** Each
  > of the four still declares its own job id, log event and result string at the decoration. So
  > the only thing written once is `ok=False` and the width of the catch, and a reader of the job
  > can no longer see that its failures are caught. A fifth job is bound by neither shape.
  > **What would bind it is a gate**, and each of the four is already pinned one at a time
  > (`test_a_ratings_state_read_failure_still_records_not_ok` and its three siblings). The
  > missing piece is a walk over `_maintenance_specs` failing on a job that records nothing and
  > is not named as deliberate. Not built here.
- `services/lists.py:779`, `history_sync.py:220`, `imdb_dataset.py:213` — three hand-rolled
  cache-database bootstraps and three sync-state stamps in two different SQL spellings. `cache.db`
  is disposable by contract, so all three want one primitive. **~90 lines**. The generalization
  must adopt `history_sync`'s rebuild lock, which is the strictest of the three, rather than the
  average.

  > **Killed: the shared bootstrap primitive. The lock is extracted instead, and it carries a
  > real fix.** Every load-bearing noun in the row is wrong by one, measured. **Two bootstraps,
  > not three**: `imdb_dataset` has none by design, its table being the output of an atomic
  > `RENAME` whose absence is handled on the read side and degrades closed. **Two singleton
  > stamps, not three**, in **one** spelling, not two: `protection_list.last_synced_at` is a
  > column on a per-slug row inside a nine-column upsert with a second writer that deliberately
  > leaves it alone, so no singleton-stamp primitive can serve it. And **"~90 lines" matches
  > nothing** — the cluster spans 418 lines and the duplication a primitive could absorb is 25
  > to 35.
  >
  > **"Adopt the strictest" has two readings and one of them is dangerous.** `history_sync` is
  > strict in two unrelated ways, and only one generalizes: the per-loop lock plus the
  > authoritative re-read inside it, which is rule 58's letter. Its other strictness is
  > DROP-on-stale-shape, which must never reach either sibling. At `lists` it would empty
  > `protection_list_item` on a shape change, so every keep list stops protecting until the next
  > sync; at `imdb_dataset` it would leave `imdb_rating` empty between the rebuild and the next
  > load, which withdraws rating protection library-wide. So the primitive carries mutual
  > exclusion and no schema policy at all, and each site keeps its own answer.
  >
  > **The real duplication the row missed is the lock, and finding it found a `dev` defect.**
  > The per-loop weak-keyed lock already existed twice, at `history_sync._rebuild_lock` and
  > `leaving_soon._pass_lock`, which no row names. `lists.ensure_schema` is the site that needs
  > a third and does not have one: its `PRAGMA` read and its `ALTER TABLE` are a check-then-write
  > that nothing serializes, because pysqlite autocommits DDL. Two callers both read the
  > pre-widen shape and the second raises `duplicate column name`, aborting a scan. Issue #660,
  > raised as a question by an earlier pass, is settled and confirmed by it. Landed at #672.
- `components/GeneralPanel.tsx` and siblings — the `.set-row` label/help/control triplet typed out
  **26 times**. A `<SetRow>` also makes rule 45 structural: one help slot per
  row means one paragraph cannot cover two controls. **~100 lines**. The "three files" this said
  was counted before `Settings.tsx` split into seven panels: the triplets are conserved, the
  spread is not, so re-derive the file list before building this.
- `api/deps.py` (new) — a request accessor copy-pasted at **7** routers under two spellings
  (`_factory`/`_settings`/`_box` in `api/{auth,backup,settings,setup}.py`, `_sessions` in
  `api/{routes,runs,whitelist}.py`), `_latest_snapshot` at **7**
  sites. **~35 lines**.

> **Landed at #670, and three of the row's figures were wrong.** `routes.py` is gone, so the
> `_sessions` three are `api/{review,runs,whitelist}.py`. The cluster is not four copies of each:
> `_factory` is 7, `_settings` 3 and `_box` 2, because `setup.py` declares only `_factory`.
> `_latest_snapshot` is **2 definitions and 7 calls**, not 7 copies. Four modules import an
> accessor rather than declaring one (`plex.py` off `settings.py`; `simulate.py`, `vocabulary.py`
> and `policy.py` off `review.py`), so the collapse touches 11 modules. `auth.py`'s `_safety` does
> **not** move: it ignores its `request` and returns a hardcoded read-only `RuntimeSafety` for the
> sign-in Plex client, which a module named `deps` would offer as "the" safety.
- `services/plex_link.py:139` — the Plex PIN flow written twice, differing in four tokens. Rules
  11/98 and 125 sit above the seam and are untouched by the merge. **~65 lines**. The row cited
  `services/login.py:115` and `services/plex_link.py:395`, both stale by one to two lines and both
  now naming functions that no longer exist; the anchor is the merged `start_pin`.

  > **Landed at #701, the start half only, and the four-token claim holds for exactly the pair the
  > anchors named.** Measured at `9b7e625`: `start_plex_login` was at `login.py:113` and
  > `start_link` at `plex_link.py:394`, so both anchors were stale before anything moved. The two
  > bodies differed in **four token positions carrying three distinct tokens**, and only one of the
  > three was a distinct *value*: the return annotation and the constructor are the same name twice
  > (`PlexLoginStart` / `LinkStart`, two structurally identical frozen dataclasses), and
  > `PLEX_LOGIN_TTL` and `LINK_TTL` were both `timedelta(minutes=10)`, one value under two names.
  > `purpose` is the only thing that genuinely differed. One `start_pin` taking it as a
  > `Literal["login", "link"]` replaces both, beside `client_identifier`, which is the shared
  > plex.tv primitive `login.py` already imported from that module.
  >
  > **The poll halves do not merge, and calling the row "the PIN flow" hides that.** They are
  > 63% divergent: 123 changed lines over a 194-line pair, against four token positions over 72.
  > The contracts differ where W6-3's kill points. `poll_plex_login` mints a session and branches
  > on whether a server is already linked, authorizing against that machine id when one is and
  > running first-run setup through `complete_link` when none is; it consumes the pending row on
  > each refusal arm, where `poll_link` consumes in a `finally`. One function serving both takes a
  > keyword argument, which is the shape that killed W6-3.
  >
  > **The first draft of this paragraph said "mints a session and never stores the token", and the
  > safety lane refuted it.** The setup branch reaches `complete_link`, which encrypts and persists
  > the plex.tv account token, so the sign-in poller stores one on exactly the path a first-run
  > operator takes. Its twin clause, that `poll_link` has "no ownership check to make", was wrong
  > the same way: `complete_link` refuses an account owning no server. Both read in the reassuring
  > direction, which is rule 144's stated failure mode, and the sentence had already been copied
  > into two more places by the time it was caught.
  >
  > **The row's rule note is half wrong, though harmlessly.** 11/98 does sit above the seam, at the
  > two routes: `api/auth.py` throttles `/api/auth/plex/start` per IP, and
  > `/api/settings/plex/link/start` is behind the AuthGuard instead, which is the right answer for
  > an admin-only route. Rule 125 sits *inside* the pollers, not above them, and is untouched only
  > because the poll half did not move.
  >
  > **What made the PR worth opening is not the dedup: `purpose` was unobserved.** Deleting
  > `PendingPlexLogin.purpose == "login"` from one poller and `== "link"` from the other left all
  > 137 tests in the three covering files green, separately and together. The two halves were wrong
  > in agreement, so no test could see either. That value is the fence between an open route and an
  > admin-only one, `/api/auth/plex/*` being unauthenticated by design, so the row an admin's
  > re-link leaves behind must not be redeemable for a session at the sign-in poller.
  > `TestThePinPurposeFence` pins both directions plus the expiry sweep, each driven red against
  > the mutation it catches, and a hygiene gate holds `PendingPlexLogin` to one construction site
  > so a third flow inherits the sweep and cannot omit a purpose.
  >
  > **`~65 lines` describes the duplicated region, not the saving.** The region is 72 (35 + 37).
  > `src/` moves 869 lines to 854, a net 15, because the merge trades duplicated code for the note
  > saying why the other half is not merged. `db/models.py`'s comment on the column read
  > `# "setup" | "login"` and was wrong in both halves, on `dev` too: no `"setup"` purpose exists
  > anywhere in the tree.
- The admin-password gate ritual, copied at **4** call sites, each re-deriving
  rule 11/98's hardest clause (a full gate returns 503 and must never register as a failed
  attempt). The pieces are already extracted in `api/auth.py`; only the ordering is duplicated.
  Risk `safety-path`, and note **only one of the four gates has a throttle test**.

> **Corrected: two of the four have one.** Arming
> (`test_settings_api.py::test_repeated_wrong_arming_passwords_are_locked_out`) and forgetting a
> watch record (`test_watch_evidence.py::test_repeated_wrong_passwords_are_locked_out`).
> `change_password` and `restore` have none, and the extraction PR writes them. This block used
> to cite both by line, and both were stale by 61 and 13 lines; naming the test instead is what
> stops that recurring, since the extraction PR shifts one of them again.

> **Landed at #681.** The four rituals were byte-identical apart from the `gate=` name and the
> 403 sentence, so they are one call to `deps.require_admin_password`, which returns `None` and
> raises: a caller that forgets to read a result still cannot proceed on a wrong password. The
> key tuple is passed rather than derived, as the contradiction paragraph settled, and a new
> hygiene gate confines `password_throttle` to `auth/ratelimit.py` and `api/deps.py` so a fifth
> gate cannot hand-roll three of the four steps. Four new tests, each driven red against a
> mutation of the step it pins. **The step nothing covered was `record_success`**: every existing
> throttle test stops at the lockout and never comes back through a success, so dropping the loop
> that clears both keys was green.
- `services/leaving_soon.py:425` — Plex client construction at **6** sites, and the
  `None`-when-unlinked branch already reads differently in two. `safety` is keyword-only and
  required, so no copy can silently drop the guard: this is maintenance cost, not a hole.

  > **Killed: the helper. One gate lands instead, widened to every client that carries a TLS
  > switch.**
  >
  > **6 is right, and only one of the six is in this file.** Re-derived by AST at this tip and
  > at the audit's own base commit (`11548fc`), six both times: `api/plex.py:617`,
  > `api/plex_trash.py:52`, `api/settings.py:510`, `services/leaving_soon.py:460`, and
  > `services/scan_runner.py:387` and `:469`. They span five modules. At the base the spread
  > was four, and #612's route split moved one of `api/settings.py`'s two into `api/plex.py`.
  > The anchor pointed at `_plex_client` when it was written and is `:453` now.
  >
  > **The extraction is S5.** Four of the six read the `PlexServer` row and pass the same four
  > arguments in the same order, compared by AST. Only the box they decrypt with is spelled
  > three ways, which is why a helper would take it as a parameter. Each of those four sites is
  > six physical lines collapsing to one, so 20 lines come out; the helper costs about 14 with
  > its docstring and the imports at each site, a net of about six. It reaches neither
  > `scan_runner` site: both build the client OUTSIDE the session block, immediately before the
  > close is registered, `stack.enter_async_context` at `:388` and
  > `building.push_async_callback` at `:474`. Moving construction inside would put every other
  > client's construction between the build and the close, which is the leak both of
  > `_plex_client`'s call sites record having been fixed once already (`leaving_soon.py:512`
  > and `:705`, rule 34).
  >
  > **"Reads differently in two" is five branches over six sites.** Each is that surface's own
  > operator answer: `None` and then a `PlexError` sentence, a `configured=false` payload, a
  > 400 from `_linked_server`, an empty dict from a best-effort read, and the two scan sites,
  > which alone agree, both carrying `None` into the sources and the reap gateway. There is no
  > shared branch to unify.
  >
  > **The row is right that there is no hole, and that is what decides the gate's shape.**
  > `safety` is keyword-only and required (`PlexClient.__init__`, `clients/plex.py:573`), so a
  > copy cannot drop the guard. `verify` defaults to `True`, so a copy that forgets it fails
  > toward verifying TLS. What an omission costs is agreement: an operator whose server carries
  > a self-signed certificate gets one surface that cannot reach it while every other surface
  > can, and nothing announces the difference. So
  > `test_every_client_carries_the_operators_own_tls_setting` binds all six, plus the fifteen
  > sibling constructions of `TautulliClient`, `SeerrClient`, `_ProbeClient` and the two
  > `*arr` (rule 72), **twenty-one in all**. It reuses
  > `test_every_arr_client_is_built_with_the_same_arguments`'s walk rather than adding a
  > second, and inherits that gate's own reasoning: a shared constructor binds only the
  > callers that adopt it. Driven red four ways, `verify` dropped from a Plex site, from
  > `scheduler`'s Tautulli site and from `_ProbeClient`, and a seventh Plex site added.
  >
  > **The class list is the walk's real bound and no count can cover it**, since a class the
  > matcher never names contributes zero sites and the number never moves (rule 145). So the
  > four classes that also declare `verify` are excluded in writing: `PlexTvClient` reaches
  > plex.tv and declares none, `GuardedSession` is built inside `PlexClient` from the `verify`
  > that client already holds, and `BaseClient` and `ArrClient` are never constructed
  > directly. The review lane found `_ProbeClient` missing from the first draft, which is that
  > hole arriving on its own gate: it probes one advertised address of the operator's own
  > server, and both callers thread that server's stored switch into it. The check's own
  > ceiling is written on it too: it reads that `verify` was passed, not what was passed.
- `services/app_settings.py:185` — the "stored wins, else env seed" rule written **7 times in 3
  spellings**, with log level resolving in `main.py` instead of a getter.

  > **Killed, then rebuilt in a different shape. Read both halves.** The gate below landed first
  > and found three of the seven sites unpinned, which is the kill's lasting value. The
  > arithmetic three paragraphs down is what the rebuild overturns; the `> Rebuilt:` block at the
  > end of this finding says why, and neither block is complete on its own.
  >
  > **7 is right, 3 spellings is not; the tree holds 5.** The population is derivable: a function
  > in `app_settings.py` that calls `_get` and takes a `Settings`-annotated parameter, which is
  > exactly the seven and nothing else. 16 more readers take no seed, and `runtime_safety` takes
  > one and delegates. Only **three** share a spelling, byte-identical apart from the key and the
  > attribute: `destructive_enabled`, `proxy_trust_enabled`, `leaving_soon_unarmed`. The other
  > four are one apiece. `get_trusted_proxies` decodes the seed and cleans the stored side.
  > `get_timezone` validates both, tests truthiness rather than `is None`, and falls through to
  > `_detect_host_timezone`, which has a fallback of its own, so its own docstring counts four
  > layers where this counts three functions. `get_discord_webhook` is encrypted and writes the
  > seed back on first read. `has_discord_webhook` is the presence probe over the same row, and
  > its `Settings` is optional.
  >
  > **A helper serves three of seven and buys 0 to 2 lines either way**, which is S5. 12 lines of
  > branch come out. What goes in depends on how the call sites wrap: a two-line body plus a
  > docstring is 8 with its separators, and `leaving_soon_unarmed`'s call is 104 characters against
  > a 100-column limit so it wraps to three where the other two fit on one, which is 13 in against
  > 12 out. Read as bare statements it is 6 against 12, which argues the other way, and that is the
  > point: **the line count decides nothing here.** What decides it is that reaching the fourth
  > needs a decoder and a cleaner, the fifth a validator and a third layer, the last two a
  > `SecretBox` and rule 16's contract, and that a helper binds only the getters that call it,
  > where the row's real exposure is the eighth getter nobody has written yet.
  >
  > **Phase 9's W4.1 touches one of the three, not all of them.** Its `SETTINGS` table carries an
  > "optional env field" column, so the precedence would be declared twice, but the same sentence
  > keeps `destructive_enabled` and the two encrypted credentials as declared exceptions, and
  > `leaving_soon_unarmed` is served by `/api/settings/leaving-soon` rather than `_general_out`.
  > So `proxy_trust_enabled` is the whole overlap. W3b-4 folded because 22 of its 40 sites were in
  > the file being rewritten; this is one call site of three, with the largest excluded by name.
  > It is a reason to prefer the gate, not the collision W3b-4 was.
  >
  > **The measurement that decided it.** Each of the seven had its precedence mutated and the
  > whole suite run against it. Four went red, each on exactly one test, each in a different file:
  > `destructive_enabled` on `test_startup_log.py`, `proxy_trust_enabled` on
  > `test_general_and_logs.py`, `leaving_soon_unarmed` on `test_leaving_soon_settings.py`,
  > `get_timezone` on `test_timezone_setting.py`. **Three went green across 4,252 tests**:
  > `get_trusted_proxies` reverting a stored empty list to `REAPER_TRUSTED_PROXIES`. The getter's
  > own docstring claims that distinction and rule 1 requires it, and clearing the list in the UI
  > is how an operator says "trust nobody" while a forwarded header still decides auth (rule 101).
  > `get_discord_webhook` letting a stale env var clobber a URL edited in the UI; and
  > `has_discord_webhook` reporting "connected" for a credential written under a rotated key,
  > which is the one case its own docstring says must read as absent.
  >
  > `tests/test_app_settings_precedence.py` drives all seven, one case each, and reconciles the
  > population against the module by AST so an eighth fails until this file names it (rule 145,
  > with the count pinned so a broken matcher cannot pass by subtracting against an empty set).
  > It reads a reference rather than an assertion, so what it forbids is an eighth getter this
  > file never mentions, and its own docstring says so. Every one of the seven mutations goes red
  > on its own named case, and both arms of the walk go red against an eighth getter. **The walk
  > is `ast.walk` rather than the module's `body`**, so a definition nested in a class or a `try:`
  > is collected: the count pin cannot cover a member that never entered the walk, which leaves
  > the population at seven and the gate green (rule 147). The review lane caught that, and the
  > fix is driven red against a nested getter.
  >
  > **The log-level half is a misreading and moving it nets negative.** `get_log_level_setting`
  > already exists and `main.py:265` already goes through it. What sits in `main.py` is the
  > *apply*, and it cannot move: the level is process-global state, not a value.
  > `configure_logging` sets it from the env at `create_app` (`:516`), before `lifespan` builds
  > the session factory, because the boot lines before the database is reachable need a level. So
  > the two sources are a sequence, not a choice, and a getter could only express the second half.
  > A `get_log_level(session, settings)` returning the resolved string would also erase the
  > provenance `main.py:302` reads off the same value, `log_level_from` being a truthiness test on
  > `get_log_level_setting`'s `str | None` (rule 76), leaving two reads of one row where there is
  > now one. Measured while confirming it: `REAPER_LOG_LEVEL=ERROR` validates and
  > then silently resolves to INFO, because `logbuffer.LEVELS` omits ERROR while `config.py`'s
  > `Literal` and the Unraid template both offer it. On `dev`, filed as #700.
  >
  > **Rebuilt: two helpers, and the shape is what changed.** The kill measured one helper that
  > swallows the `_get` call, which is why `leaving_soon_unarmed` wrapped to three lines and why
  > the getters then dropped out of the gate's own population: `_env_seeded_getters` collects a
  > function that calls `_get` and takes a `Settings`, so a helper standing between them takes
  > all three off the walk and leaves it green at four. **A helper that takes the value `_get`
  > already returned costs neither.** `_env_seeded_switch(stored, seed)` is pure, synchronous and
  > typed `-> bool`; each of the three keeps its own `_get` line and returns in one, at 76 to 79
  > columns. `_decrypted_or_absent(box, stored)` is the second, and it reaches three sites rather
  > than two: both Discord getters and `get_api_key`, which the row never counted because it
  > takes no seed. **Measured: +13 total lines, -9 code lines**, the difference being the two
  > docstrings, which is the rule being written down once instead of implied three times. One
  > sentence was deleted from `proxy_trust_enabled`'s docstring for saying what the helper now
  > says. **Driven red.** `stored is None` to `not stored` in `_env_seeded_switch` fails three
  > named cases at once, one mutation for what used to need three; returning the raw ciphertext
  > from `_decrypted_or_absent` instead of `None` fails the rotated-key case. Full suite green,
  > 4,290 passed at the tip. **What the rebuild does NOT buy**, and the kill was right about it: the gate is
  > still the only thing that binds an eighth getter, because a helper binds only its callers.
  > The two are not alternatives, and reading them as alternatives is what killed this row. **One
  > measured negative**: `get_api_key`'s own decrypt-failure path is still pinned by nothing of
  > its own, `tests/test_settings_api.py`, `test_general_and_logs.py`, `test_foundations.py` and
  > `test_api.py` all staying green under the mutation across 296 tests. It is now covered
  > transitively instead, by the one Discord case that drives the shared declaration.
- `backup.py`/`restore.py`/`retention.py` — **5 raw `sqlite3.connect` blocks**, none using
  `db/session.py`'s `_configure_sqlite` declared pragma set, so `busy_timeout` is 5000 in two,
  30000 in one and absent in two. Two share a byte-identical operator string. Risk
  `safety-path`; the pragma unification and the string lift should be separate commits.

  > **Killed: the pragma unification only. Two gates land instead.** The byte-identical
  > operator string is the row's other half and stays open, as the row itself asks.
  >
  > **There is nothing left to unify.** `_configure_sqlite` is already one declaration serving
  > both engines. Take away what the correction below removes from scope, `_read_revision` and
  > `retention`'s `isolation_level=None`, and what remains is two one-line `busy_timeout` calls
  > whose values differ by design: `backup` waits 5s before a `VACUUM INTO` beside a live write,
  > `retention` waits 30s because it is about to hold the write lock itself. A helper taking
  > `(connection, ms)` replaces one line with one line (S5). The three sites with no pragma each
  > need none. `restore`'s two writers own the staged file outright, and `stored_revision` is a
  > `SELECT`.
  >
  > **The correction's figures hold and this row's do not**: 5000 in one, 30000 in one, absent in
  > three. The five blocks span four modules rather than three, and the module this row's file
  > list misses is `db/schema_gate.py` — the one the correction exists to protect, reachable from
  > `restore.py` only through the `_read_revision` alias.
  >
  > **The duplication is the value, not the calls.** `5000` is written once as a SQL literal and
  > quoted as "5s" in five docstrings, in `executor`, `imdb_dataset`, `retention`, `scan_runner`
  > and `scheduler`, none derived from it (rule 144). `executor._commit_journal`'s copy is the
  > reason the journal write takes two attempts with no sleep between them, so moving the pragma
  > makes that reasoning silently wrong on the write that records what was deleted. **Neither
  > anchor alone finds them**: `scan_runner` and `scheduler` never name the pragma, and
  > `imdb_dataset` never cites `db.session`, being about `cache.db`. A seventh copy carries no
  > figure at all (`snapshot.py`'s "far inside that budget") and is named in the gate as
  > out of reach rather than covered. So the gate derives the seconds from the declaration and
  > names all five on failure, and a second gate holds the correction's own parenthetical:
  > `PRAGMA journal_mode=WAL` is set in exactly one module, because it writes the file it reads.

  > **Landed: the operator-string half, W3b-10′.** The row says two of the five blocks share a
  > byte-identical operator string. The sentence has **four** raise sites in three functions,
  > and one of them is not a sqlite block at all: `restore._force_destructive_off`,
  > `_force_recovery_off` (an `OSError` writing the staged `launcher.conf`), and
  > `_purge_auth_state` twice. One declaration now, `_PREPARE_FAILED`, sitting above the three
  > functions it serves.
  >
  > **The sentence gained the half it was missing.** "Reaper couldn't prepare this backup to
  > restore." said what failed and not what it meant for the operator's install, so it is now
  > "Reaper couldn't prepare this backup. Nothing was restored." It is the shape the same flow
  > already uses one layer up, `api/backup.py`'s "That password didn't match. Nothing was
  > restored." (rule 21).
  >
  > **The second half was nearly true, and the review lane is what caught the gap.** `arm`
  > writes `READY_MARKER` last, so a raise cannot arm anything. Neither of its two checks
  > rejects an arm over a staging that is ALREADY armed: the token file survives an arm, so a
  > confirm retried after a client-side timeout runs the three steps again with READY on disk,
  > and a raise there left the swap armed while the operator read that nothing had happened.
  > Rule 126's exact shape, failing in the reassuring direction. `arm` clears READY before the
  > first step now, so a failed re-arm disarms, which is the keep direction, and the sentence
  > is a property of that order rather than of the state it happened to be called in. On `dev`
  > too, and fixed here rather than filed because the sentence this row lands is what asserts
  > it. A fourth test drives it and also asserts `apply_pending_restore` returns `False`.
  >
  > **All three arms were unreached by the entire suite**, behind 93% line coverage for the
  > module: nothing drove `_force_destructive_off`'s except, nor `_force_recovery_off`'s, nor
  > `_purge_auth_state`'s, and no test anywhere asserted the sentence. Four tests do now, each
  > also asserting the staging is still unarmed, and the module's uncovered statements fall
  > from 20 to 14. Seven mutations driven, each red on its own named case: the mapping arm
  > deleted from each of the three, `READY_MARKER` written first (all three red), one site
  > reworded to a fifth spelling, the READY clear removed, and the purge committed per table.
  > **That last one is the review lane again**: the trigger sat on the FIRST auth table, whose
  > row survives under a rollback and under a half-run alike, so the assertion could not fail.
  > It sits on the last now, and the two tables purged before it are the evidence. The fourth
  > raise site is the `_TABLE_NAME` identifier guard, which carries `pragma: no cover` and is
  > named here as out of reach rather than covered (rule 147).
- Frontend hooks: the image-fallback ladder **3 times** (`Backdrop`, `Poster`, `WhyHero`, whose
  comments already say they mirror each other), the upward dirty-report idiom **5 times**, the
  "a test result and the fingerprint it vouches for" pattern **3 times** (each fixed separately,
  in #178 twice and #264), the admin-password confirm form **twice** with a recorded drift.

  > **Killed: all four extractions. Two defects the duplication was hiding land instead, with a
  > gate over the third sub-item's whole family.** Each sub-item was measured as its own decision.
  > Two of the four counts are wrong. Line figures below are non-blank lines, one convention
  > throughout.
  >
  > **The ladder is two ladders at four sites.** `Backdrop` (`ReviewQueue.tsx:213`) and `WhyHero`
  > (`WhyPanel.tsx:343`) share the art-then-poster one: a `?kind=art` seed, a `fellBack` ref, a
  > reset effect and a one-step `onError`. `Poster` (`ReviewQueue.tsx:246`) shares a different one
  > with `ScalesPanel`'s own `Poster` (`:28`), a boolean that swaps in `PosterFallback`. The row
  > names both members of the first pairing and misses the second site of the other. Grepped two
  > ways, on `Backdrop|WhyHero` and on `onError` across every `.tsx`: the first finds three files
  > but only two of the four sites, `ShowPanel.tsx:18` being a consumer, and the second finds all
  > four. The art ladder is **14 lines at each site**, plus `WhyHero`'s five-line reset comment,
  > which relocates rather than goes. So 33 leave and 35 come back, 29 of them a leaf module both
  > callers can import without a cycle and 6 in call, import and `onError={onError}` lines. Both
  > callers already import the three React hooks, so nothing is saved there. That is +2. The
  > second ladder is 29 lines, 17 and 12, over two components whose markup shares nothing:
  > `div.poster.poster-empty` against `span.scales-poster`, one with an `alt` and a reset effect
  > and one with neither, so the shared component takes wrapper, class, alt and size to serve two
  > call sites.
  >
  > > **Un-killed, the art ladder only: it lands as a HOOK, `components/artFallback.ts`.** The
  > > kill above measured a shared COMPONENT and is right about it. The two sites share the
  > > ladder and share none of the chrome. A fragment with `card-bg`/`card-scrim` against a
  > > `div.why-hero` with its fade, so a component has to take that chrome as props, which is
  > > where its 35 lines back came from. `useArtFallback(posterUrl)` returns `{ src, onError }`
  > > and each site keeps its own markup: **41 lines out of the two sites against 4 back plus
  > > 2 imports, and a 38-line leaf module of which 13 are its docstring**, so about zero on
  > > total lines and about -8 on code lines. That is S5's wash. The payoff was never lines:
  > > `WhyHero`'s copy carried "Mirrors ReviewQueue's Backdrop" and `Poster`'s carries "exactly
  > > as Backdrop does", two cross-references to a fix that had to be made twice (rule 72).
  > >
  > > **Neither copy had a test, and the ladder has three rungs.** `artFallback.test.tsx` drives
  > > art, the fall to the poster, the drop when both fail, and the reset, through `WhyHero`
  > > rather than a probe (rule 119), plus an axe audit on two rungs. Driven red three ways: the
  > > flag reset dropped (1 of 4), the fall to the poster disabled (3 of 4), the `?kind=art` seed
  > > dropped (2 of 4). **The one thing the extraction changed is unreachable from `WhyHero`**,
  > > whose prop is a `string`: the hook keeps `Backdrop`'s null contract, where `WhyHero` used
  > > `""` for the give-up value and guarded nothing in `onError`. Both `WhyHero` call sites
  > > guard with `poster_url &&`, so no render path moves, and the review lane found the whole
  > > suite green against the other spelling. `renderHook` drives the null lane directly
  > > (rule 145). `_EXPECTED_FRONTEND_MODULES` 205 to 207 and
  > > `_EXPECTED_RENDERING_TEST_FILES` 57 to 58.
  > >
  > > **The second ladder stays killed, and the sentence above did not ask whether the
  > > difference it preserves is correct.** Measured: `ScalesPanel`'s `Poster` needs no reset,
  > > because its row key is `${t.item_id ?? t.group_key ?? t.title}-${i}` and a different title
  > > is therefore a different component. That key falls back to a display title, which rule 19
  > > forbids, so what holds it up is the first branch. Nothing said so, next to a sibling whose
  > > comment calls the reset load-bearing, and a change to that key would latch the film strip
  > > onto every title the row is reused for. One comment lands at that site instead of a
  > > component.
  >
  > **The dirty-report count is right, and it is the sub-item with nothing behind it.** Five, at
  > `GeneralPanel:419`, `PlexPanel:558`, `NotificationsPanel:143`, `SecurityPanel`'s
  > `AdminPasswordForm:156` and `RestoreCard`'s `RestoreFlow:321`; three panels and two children
  > of panels, not five panels. Grepped on `onDirtyChange` and again on `Dirty`, which agree. The
  > duplicated region is **4 lines** each, 20 in all, against a module of 19 plus 10 lines of call
  > and import: +9. What rule 146 asks of these is per-site and a hook cannot carry it, since the
  > obligation is that the signal is declared above every early return and that every early-return
  > state is re-read as one the report still fires in. All five satisfy it and all five were
  > re-checked, and what they say differs: `GeneralPanel` and `RestoreCard` name the branches their
  > report survives, `NotificationsPanel` and `AdminPasswordForm` say they have no early return
  > above it, and `PlexPanel` has three returns, all below.
  >
  > **The test-result pairing is four sites, and three of them were wrong.** `testedWith` is
  > declared in `ServiceModal`, `ServicesPanel`, `DiscordModal` and `NotificationsPanel`; grepped
  > on `testedWith` and again on `setTest(`, which agree. Only `ServiceModal` captured the
  > fingerprint in `onMutate`, and its own comment says why. The boxes stay live while the request
  > is out, so computing the fingerprint at success time files the answer against an address it was
  > never asked about. On the two webhook surfaces it is `url.trim()` off a box that is never
  > disabled during the send, so pasting a second webhook while the first is being tested leaves
  > "Passed" beside a channel nobody sent to. On `ServicesPanel` it is the instance's own address,
  > which moves when the Edit modal over the card saves a new one. All three are on `dev`
  > (`Settings.tsx:1155` and `:2427` before the panel split, `DiscordModal.tsx:63`) and all three
  > are fixed here. **`ServiceModal`'s own `onMutate` was unpinned**: the three tests in *what the
  > connection badge vouches for* edit the boxes with nothing in flight, which the fingerprint read
  > at either end satisfies. A fourth drives the retype during the request, and asserts the badge
  > returns when the tested address is typed back, so a result that was never stored fails it too.
  > The extraction stays a kill on shape rather than on a line count: `ServiceModal` stores a union
  > of two payload shapes where the others store one, carries `reachedAt`/`openedWith` beside the
  > fingerprint, and reads the held result at three places against one apiece. A hygiene gate binds
  > the family instead, which is what the row's "each fixed separately" is asking for.
  >
  > **The admin-password form is two, and the drift is three things, all on `dev`.** The
  > declaration's own comment says the length floor is stated once by "the placeholder, the live
  > message, and the server rule", and the placeholder was the literal `at least 12 characters`
  > (rule 7/24, and rule 144's one ungenerated copy of a claim whose siblings are all derived).
  > That one is fixed and cannot be pinned: 12 is what the constant holds, so a test reading the
  > placeholder passes either way (rule 141). `SetupPasswordStep`'s error region was not `standing`
  > while holding `{pw.length} so far`, so its text changed inside a live region on every keystroke,
  > which is what #394 fixed at the sibling and never swept here (rule 72). Its two boxes carried no
  > `aria-invalid` where the sibling's do. The last two are pinned, each driven red. The extraction
  > is not built: 8 shared lines out, and the two forms are three boxes against two, three complaint
  > branches against two, and a `valid` that carries `needCurrent` on one side only. The fourth
  > difference, "The passwords don't match." against "The two passwords don't match.", is measured
  > and left. Neither form is on screen while the other is, and a claim with no number in it cannot
  > drift in the direction rule 144 is about.

- `App.tsx:196` — three parallel focus slots whose own comment reads "Rule 72: three of these now,
  and a fourth belongs in the same three places". One value keyed on `view` retires the obligation.

**Parameter objects.** Six functions take a cohesive record apart and rebuild it:
`snapshot._judge_item` (**27 parameters**), `season_scan.gather` (**25**, and it reconstructs a
`SeasonPolicy` that `SeasonPolicy.from_body` already builds; `season_evidence.SeasonPolicy`'s own
docstring names this as rule 144's shape), `build_season_facts` (24), `plan_series_prune` (20),
`snapshot.scan` (17), the Plex match record threaded as **6 loose parameters through 4
signatures**. `snapshot.py:928` is the sharp case: 12 parallel `movie_*`/`tv_*` locals with
nothing structurally preventing the movie loop being handed `tv_keeps`.

> **Corrected: this paragraph carries no risk class and two of its six sit on the deletion path.**
> Every parameter count is exact. `plan_series_prune` (`services/season_pruning.py:414`) is the
> sole producer of `SeriesPrunePlan.prunable`/`.protected`, which becomes a hard protection in the
> verdict. Of its 17 defaulted parameters, **nine default permissive and seven protective**, and
> the three that matter are `progress_unreadable`, `progress_seasons_unmatched` and
> `progress_show_unmatched`, all `False`: a carrier that lets a caller omit one of those widens
> what is prunable, silently — rule 143's shape. (`apply_keep_last=True` is the *protective*
> default and is not an example of this.) `gather` is the same. Both are `safety-path`; the other
> four are `none`. Three more functions at 15 parameters go unnamed: `judge_facts`, `_judge_series`
> and `_protection_reason`.

> **Killed: all six parameter objects. One gate lands instead**,
> `test_a_judged_item_is_never_handed_the_other_lanes_policy`. Every count above is exact today,
> re-derived by AST, and the addresses are `_judge_item` `snapshot.py:1441`, `gather`
> `season_scan.py:1090`, `build_season_facts` `season_scan.py:464`, `plan_series_prune`
> `season_pruning.py:414`, `scan` `snapshot.py:572`; the three unnamed 15s are `judge_facts`
> `snapshot.py:1350`, `_judge_series` `season_scan.py:1568` and `_protection_reason`
> `season_pruning.py:586`. The counts are right and the fix is not.
>
> **Every keyword at every production call site was classified**, and no candidate survives it.
> `build_season_facts`'s one call site assembles its 24 arguments from 18 locals, 7 of them
> already same-named, plus 6 expressions evaluated per season; a carrier holds the same 18
> assignments one frame up and the 6 stay where they are. `_judge_item`'s two sites unpack 10 and
> 11 arguments off `RawItem` and `SeasonJudgment`, which are different types, so one carrier
> parameter cannot serve both and building one at each site is S5's object that nets to zero.
> `scan`'s 12 pass-through arguments arrive in `scan_runner.run_scan` from four unrelated
> sources, the clients, the two policies, the Seerr reads and the caller, so the only carrier
> that fits them is a bag of everything. `plan_series_prune` has 2 production call sites and
> **87 in tests**, each passing 3 to 7 of the 20 and taking the rest as defaults; the correction
> above is why those defaults are the protection, and a carrier either repeats them field for
> field and buys nothing, or drops them and rewrites 87 tests to spell 20 fields.
>
> **The Plex match record is 6 parameters through 3 signatures, not 4**, and it is the one clean
> pass-through in the paragraph: `plex_rating_key`, `matched_by`, `match_detail`, `match_status`,
> `merged_rating_keys` and `match_candidates` are declared on `_judge_item`, `judge_facts` and
> `_explain` (`snapshot.py:1600`), and nothing between branches on one. Five reach `_explain`
> alone; `plex_rating_key` also goes onto the stored `Candidate` row at `snapshot.py:1520`. It is
> still a kill, for W5-2's reason: `plex_rating_key` and `merged_rating_keys` are identity-path
> join keys read off a `RawItem` at `snapshot.py:291`, `:437`, `:874` and `:876`, so a carrier
> held on the record files a join key one attribute deeper (rules 29/106), and a carrier built at
> the call site spends 6 lines to save 6.
>
> **`gather`'s nine loose policy fields are W5-3's row and are not killed here.** That row has
> since landed, and it is the one candidate in this paragraph that was never a new parameter
> object: the carrier existed, `_judge_series` already took it, and only `gather` was still
> unpacking it. Two of its nine were required rather than defaulted, and its one production call
> site passed all nine, so the correction's "`gather` is the same" as `plan_series_prune` does not
> hold. W5-3's landed block carries the measurement.
>
> **What is real is this paragraph's own sharp case**, re-anchored from `snapshot.py:928` to
> `:931`. `scan` derives twelve locals that differ only by a `movie_` / `tv_` prefix and hands six
> of each to `_judge_item`. Cross `custom_condemn`, `keeps` and `policy` at the movie call site
> and the new gate is the only test in the whole suite that fails: the keep rules a movie is
> judged against and the threshold it is condemned at can both come from the TV policy with
> nothing else reading it, which is rule 118's gap. `gates`, `signals` and
> `window_days` are the three `tests/test_scan_pipeline.py` already catches, measured one at a
> time. A lane carrier would close it by construction and costs a
> `safety-path` diff in `services/snapshot.py` plus S3 and C9; the gate closes the same hole from
> `tests/`, binds a third call site written by someone who never read this paragraph, and is
> CLAUDE.md's "write the gate instead". Driven red eight ways: each of the six crossed on its own,
> both sites made to read one lane, and one value computed inline rather than taken off a prefixed
> local.

> **Corrected, wave 3's `safety-path` items. Four must not be built as described.**
>
> **`clients/plex.py`'s `_call` would swallow the transport guard's refusal.** The eight mutating
> methods each carry `except SafetyViolationError: raise` *ahead* of the catch-all (`:1223, 1267,
> 1317, 1348, 1414, 1439, 1470, 1498`) — a second wrapper shape the "three opt-outs" does not
> name. Map `except Exception` uniformly and a guard refusal becomes a `PlexError`, which
> `services/leaving_soon.py`'s `sync_shelves._reconcile` catches per library (`except PlexError`)
> and *continues*, turning a loud stop into a string
> beside "Plex is unreachable" (rules 92/93, and a rule 21 regression). **That arm is entirely
> unpinned**: every `SafetyViolationError` test drives `GuardedSession` directly, and
> `test_plex_labels.py` / `test_plex_sweep.py` never construct a refusing safety state. Dropping it
> goes green. The helper must also keep `asyncio.to_thread`: `GuardedSession.request` reads the
> `_declared` ContextVar inside the worker thread, and `run_in_executor` does not propagate it, so
> a shared executor makes the guard refuse every journalled write. There are four structural
> opt-outs plus the write shape, not three. Counts: 23 `to_thread` sites, 13 of the identical read
> shape.
>
> **The sqlite pragma unification must not reach `_read_revision`.** `PRAGMA journal_mode=WAL`
> **writes the file** — header bytes 18/19 flip and persist. `_read_revision` runs at
> `restore.py:312` on the database unpacked from an operator-supplied `.reaper`, inside the rule 74
> artifact gate, before `_check_schema` and before the operator confirms. Adding the pragma set
> turns a pure read into a write against an unverified artifact. `isolation_level=None` at
> `retention.py:194` is load-bearing for its `VACUUM`. Actual `busy_timeout` spread: 5000 in one,
> 30000 in one, **absent in three**. The `foreign_keys=ON` cascade concern does not apply — the
> purge's tables are children, never parents.
>
> **The scheduler decorator would change three of the seven.** Five record on failure, not four —
> `scheduled_scan` does, at `:606`. `sweep_expired_sessions` and `sweep_old_snapshots` deliberately
> record nothing (both "Not operator-schedulable"), and `scheduled_scan` has two quiet skips ahead
> of its catch-all whose
> comment says the record exists so `ScanRow` prefers it over a stale snapshot — recording a
> "scan already running" skip would hide the last scan behind "Scan failed". Signatures also
> diverge. `services/leaving_soon.py`'s `_record_skip` is the writer, not the wrapper half; the wrapper is `after_scan`, which calls it at three sites.
>
> **The admin-password gate is four sites, and the
> 503 clause is already structural.** `PasswordVerificationBusyError` raises out of
> `api/deps.py:155-158` before `_record_password_failure` is reachable, so all four inherit it
> rather than re-deriving it. That pair was `api/auth.py:164-167` and `record_password_failure`
> until #681 moved both; the address is re-anchored here rather than left pointing at the
> `/context` route that now sits on those lines. The extraction belongs in `api/auth.py`, never in `settings.py`,
> which phase 6 splits. **It has now split**, so the four sites are two in `api/settings.py`
> (arming, and changing the password), one in `api/plex.py` (forgetting a watch record, which
> moved with the Plex routes) and one in `api/backup.py` (restore). The count is unchanged and
> only the addresses moved. The helper takes the throttle key tuple rather than deriving it: the four
> gates use distinct account keys, and merging them means a wrong restore password locks out
> arming.
>
> **`arr.py`'s three dict guards are untested** (`system_status`, `series_by_id`, `movie_by_id`;
> cited by line as `:68, :121, :229` before #652 collapsed all eleven, which is why they are named
> by symbol here) — the parametrize covers 7 of
> the 8 list guards. Add those cases in the same commit, or the pinned count covers a population
> that excludes three members (rules 145/147). The three dict messages also omit `self.prefix`
> where the eight list ones include it, so a message-generating helper changes three
> operator-facing strings. The helper must have no way to *not* raise: a `default=` or `coerce=`
> parameter reopens rule 28/93 exactly. The repeated comment is 2 lines, not 3; the executor's
> pinning tests are ~13 to 16, not 8.

## Wave 4: the declaration tax

The structural finding, and the one with the least line count and the most leverage.

### 4.1 Adding one setting touches about 20 sites

Traced end to end for `accent_color`, which is the *simple* case (DB-only, one validator):
key constant, default, getter, setter, two Pydantic fields, the `_general_out` read, two
`put_general` branches, the validator, two `api.ts` sites, then **six** echoes inside one React
component (`useState`, seed effect, `onSuccess` re-seed, dirty check, `pending.push`,
`discardDrafts`), plus the JSX row and two test fixtures. An env-seeded setting such as `timezone`
adds four more.

There is no registry. `services/app_settings.py` is **57 hand-written functions over 23 key
constants**, all wrapping two already-generic helpers, `_get` and `_set`. `put_general` is a
50-line if-chain. On the frontend, `GeneralPanel` keeps six fields in **five parallel hand-written
lists**, and issue #90 was two of those lists disagreeing.

Proposal: a `SETTINGS` table (key, type, default, optional env field, optional validator) driving
`_general_out` and `put_general` as loops, with `destructive_enabled`, the two encrypted
credentials and the per-job rows staying as **declared** exceptions rather than as anonymous
lines. A `FIELDS` descriptor on the panel side drives the six echoes. Keep the Pydantic models
hand-declared and add one drift test against the table (rule 103's shape).
**~300 backend lines, ~60 route lines, ~50 frontend lines.** Risk `behavior`, densely pinned.

### 4.2 The wire types are a 1,239-line hand mirror

`frontend/src/api.ts` is 2,053 lines, of which **1,239 (60%) are 108 hand-written type
declarations** mirroring `src/reaper/api/schemas.py`. The guard, `tests/test_api_type_mirror.py`
(468 lines), compares **field names only**, by its own statement, and carries an 11-entry rename
map plus two hand-reconciled counters that must be edited on every schema change.

The rest of `api.ts` is in excellent shape and should not be touched: 94 endpoint wrappers,
**zero unwired**, all funneling through one `fetchApi` whose comment records that the four
hand-rolled copies it replaced had already drifted.

FastAPI already serves the schema at `/api/openapi.json`, and the repo already has the
generated-asset pattern rule 68 requires. Proposal: a committed generator emitting
`api.types.gen.ts`, plus a **small hand-written overlay** for the ~8 documented deliberate
tightenings (the closed `status` union, the optional fields the fixtures do not carry) and the 3
client-only shapes. The overlay is load-bearing: generation *loosens* unions the
TypeScript deliberately narrows. **~1,100 TS lines and ~400 Python lines replaced by ~150**, and
the guard upgrades from "names match" to "types and optionality match".

### 4.3 Cross-language enums have no drift guard, and one was shipping wrong

Rule 103 requires a drift guard on a list mirroring a declaration. It lives in
`.claude/rules/backend.md`, scoped to `src/reaper/**/*.py`, so **an agent editing the TypeScript
copy of a Python enum never loads it**. The scoping split is by directory; this obligation is by
direction of the mirror.

| Concept | Python | Mirrored in | Guard |
| --- | --- | --- | --- |
| Gate ids | `engine/gates.py` `GateId` (11) | `policyMeta.ts` `GATE_META`, 2 components, the manual | **Done (#551)**: a `GateId` union `GATE_META` `satisfies`, pinned against the enum |
| Signal ids | `engine/signals.py:66` (5) | `SIGNAL_META`, `RAMPS`, `BUILTIN_SIGNAL_IDS`, 4 more | Partial: 3 TS maps unguarded |
| Verdict | **none: bare `str`** | `api.ts:17` closed union, `reviewFate.ts` | Impossible, no declaration to compare |
| Override | one model only | `api.ts:168`, `reviewFate.ts`, `StatusChip.tsx` | None |
| Chip tone | `schemas.py`'s `ChipOut.tone` | `api.ts:40`, **CSS classes** via interpolation | None |
| `InstanceKind`, `SignalState`, `MatchStatus`, `ListSource`, `ListHealth`, `ShowStatus`, `Channel` | various | `api.ts`, labels, `.env.example`, the Unraid template | None |

Two fixes, both small:

1. **`Verdict` and `Override` should be `Literal` types in Python.** The app's central vocabulary
   is currently declared only in TypeScript; `decide_verdict` returns `str`. Typing it makes mypy
   cover what no test does. Risk `safety-path` in location, typing-only in effect.
2. ~~**One cross-reference line** in `.claude/rules/frontend.md` under rule 66, pointing at rule
   103.~~ **Landed with #551**, no new rule number, and it closes the scoping gap for every row
   above.

`SimStale` (`test_api_type_mirror.py:442`) is the exemplary case and is what the others should
look like.

> **Corrected, wave 4.**
>
> **4.1 has a blocker the proposal walks into.** `tests/test_general_and_logs.py:236` pins that a
> refusal writes nothing, and its docstring names the regression: "Moving any `set_*` above a check
> would half-apply a six-field save with the operator told it failed." `put_general`'s own OpenAPI
> description carries the same promise (rule 144). **The table-driven version must be two passes**,
> validate-all then write-all. Note what the test does and does not reach: its body carries one
> invalid field, `application_url`, so a single-pass loop that happened to validate that field
> first would still pass. The test forbids a write before a *later* field's validation, and two
> passes is the shape that cannot regress. Validation *order* is unpinned and safe to change.
>
> `put_general` is **95 lines in three phases**, not a 50-line if-chain, and its third phase
> (`:1978-2002`) writes `launcher.conf` and mutates `os.environ` — not a DB setting, and not among
> the declared exceptions. Three of `GeneralPanel`'s six fields resist a uniform descriptor:
> `default_spare_days` is a two-half draft, `trusted_proxies` is a string-to-list conversion
> deliberately excluded from `pending` while counted in `hasDrafts` (rule 146), and `accent_color`
> alone blocks the save. A descriptor covering six fields carries three escape hatches, which is
> most of what it was meant to remove. The trace also misses `App.tsx:316` and, more importantly,
> **`frontend/src/accent.ts`, which duplicates both `_HEX_COLOR` (`:25`) and the default (`:42`)**
> — rule 144's shape, and a backend `SETTINGS` table does nothing about it. #90 was one shared
> `> 0` condition across three echoes plus a fourth that did not handle the field, not two lists
> disagreeing. `app_settings.py` has 57 top-level functions, of which **47** call `_get`/`_set`;
> neither figure is "55". The encrypted credentials and `destructive_enabled` can stay declared
> exceptions.
>
> **4.2's mechanism is wrong.** `/api/openapi.json` sits inside `/api` behind the `AuthGuard`
> (`main.py`'s `docs_url=None`/`openapi_url=None` block and the `@app.get("/api/openapi.json")`
> route under it), which a session cookie or an API key satisfies, so an HTTP-fetching
> generator needs a booted server and a credential. Build the document **in process** with
> `create_app(settings).openapi()` instead. No precedent exists for that in the tree:
> `tests/test_openapi_tags.py`'s fixture deliberately reads it over HTTP, signed in, and its header
> argues against rebuilding in process for a test that is *about* the served document. A generator
> is the opposite case, and the argument has to be made rather than borrowed. No OpenAPI-to-TS
> tooling exists in either lockfile, so the URL route would also add a dependency against rule 15
> and the plan's own no-unused-dependency claim. The rename map has 10 entries, not 11. Two bounds
> strengthen the case and go unmentioned: the guard counts `export interface` only, so all 17
> `export type` declarations — every enum in 4.3 — sit outside it, and nested inline objects are
> compared at their top level only.
>
> **4.3's gate row is wrong in the plan's favor.** Python has all 11 gate ids; **TS was short by
> 4** and Python by none — landed ahead of the phase as #551, since two of the four fired on
> ordinary scans and the operator was reading their raw ids. Everything else in the table is
> confirmed. On typing `Verdict`/`Override` as
> `Literal`: safe on `decide_verdict`'s return, and **not** automatically safe in `schemas.py`,
> where `verdict: str` and `override: str | None` validate rows read from the DB. Nothing
> constrains those columns — no CHECK, no migration ever wrote an out-of-set value — but narrowing
> the wire models turns a legacy value from "renders oddly" into a 500 on the review queue, which
> fails in the operator's face rather than toward keeping the file. State which.

## Owner decisions

Two items turned on a roadmap call rather than on the code. Both are now settled and carry a
recommendation; each reverses what this audit's first pass concluded. A third, the two unreachable
engines, was settled the other way and has moved to wave 1.2 as a deletion.

**1. `db/models.py`'s `AutonomyGrant` `AutonomyGrant`: a table, two CHECK constraints and an index for a feature
with no code. The recommendation here is to KEEP it and correct one sentence.** The audit's first
pass said delete, on the strength of "zero references in `src/`, `tests/` or `frontend/src/`. That
claim is true of *code* and false of *prose*, and the difference decides the question:

- `services/scheduler.py:22` — "This scheduler never deletes media ... automated deletion is an M8
  concern gated behind an earned autonomy grant". `tests/test_scheduler.py:168` and `:199` pin it.
- `engine/policy.py`'s `PolicyBody._pin_to_the_running_scorer` — a policy edit "voids pending approvals and any autonomy grant keyed to
  it".
- `engine/backtest.py`'s `BacktestResult.lift` docstring — the number the earned-autonomy flow
  was designed to consume. **Deleted with the module in #599**, so this is two live sites, not
  three; the keep recommendation is unaffected and the correction below already says why.

So the concept is load-bearing in the reasoning even though the table is inert, and the docstring
is the only full record of the design: the grant keys on `policy_hash`, so any policy edit mints a
new hash, the grant stops joining, and the profile reverts to approval-required. Autonomy cannot
be inherited by a policy nobody reviewed. The two CHECK constraints mean no row can honestly exist
until the backtest ships, so this is fail-closed rather than a footgun.

`STATUS.md:45` tracks the flow as **open work #1**, the top of the roadmap. Removing schema
immediately before building the feature is churn, and it costs a `RETIRED_TABLES` entry that would
be deleted again when M3b lands.

The docstring's self-defense is the broken part. It justifies keeping the schema on "pre-release,
single migration baseline", a premise that expired at revision 2 of 24, so the conclusion is still
right while the stated reason is false. That is #550's class. Rewrite it to name the real reason
(M3b is next, tracked at `STATUS.md` open 1) and the rule 25 tension is at least honestly stated.

If M3b leaves the roadmap, revisit. The mechanics then need **no migration**: `alembic/env.py:45`
already has an `include_name` filter for cache tables, so a `RETIRED_TABLES` arm plus deleting the
model class leaves existing databases with an inert empty table and keeps `alembic check` green.

> **Corrected: one of the three evidence sites died in wave 1.2.** `engine/backtest.py`'s
> `BacktestResult.lift` docstring was
> deleted there, and the sentence at the top of this item about no row existing "until the backtest
> ships" stops parsing with it. The recommendation survives on the two remaining citations
> (`scheduler.py:22`, `policy.py`'s `PolicyBody._pin_to_the_running_scorer`) and on M3b's place at the top of the roadmap. But the
> surviving `backtest_passed` column and its CHECK constraint then name a feature with no code, no
> plan and no successor issue, so the docstring rewrite this item already schedules must stop
> promising the backtest (rule 25). `include_name` is in `alembic/env.py`. No `RETIRED_TABLES`
> registry exists yet, though `RETIRED_COLUMNS` now does, which is the same mechanism one level
> down and the thing a `RETIRED_TABLES` arm would sit beside. `docs/DECISIONS.md` needs no edit
> either way: the dagger test walks only rows under `## Decisions locked`.

> **Decided 2026-08-07 (C1): keep the column, correct the docstring, and the successors already
> exist.** The rule 25 concern is narrower than the correction reads: #553 and #554 are open, so
> the feature is not code-less and planless, only unbuilt. What must change is `db/models.py`'s
> class docstring, which promises the backtest and justifies itself on "pre-release, single
> migration baseline" — false since revision 2 of 23. `docs/STATUS.md`'s M3c, M3f and M3g rows
> and open item 2 carry the same correction, in the same commit.
>
> **Do not drop this column alone, now or at rule 148's release M+1.** `backtest_passed = 1` and
> `min_supervised_runs >= 3` are one fail-closed pair that makes a dishonest grant row
> unconstructible; removing one leg weakens an inert table rather than removing it. The whole
> table is unreachable — no import, no query, no insert anywhere — so if it goes, it goes
> together, and that is a decision about M3b rather than about dead schema. Rule 148's sweep
> covers the other six columns.

**2. `AuthSession.user_agent`: keep it, and give it the reader it was missing.** Written once at
`auth/sessions.py:69` and read by nothing today, having been threaded through seven sites to get
there (`api/auth.py:241`, `:301`, `:392`; `services/login.py:156`, `:253`, `:317`, `:347`). The
audit read "no reader" as "delete the column"; the owner's call is the other resolution, which is
better: **emit it at debug when a session opens**, so a stored value that only a database client
could see becomes something a session can actually be diagnosed with.

That closes the finding rather than deferring it. The dead-state complaint is that the value is
recorded and unreachable; a debug line makes it reachable, and the column stops being write-only
state. The existing 300-character truncation carries over unchanged. Debug level specifically, not
info: a user agent on every session open is noise at the default level and useful only when
someone is chasing a login problem.

No migration, no staging, and nothing to keep out of the PIN-flow merge.

## Do not touch

The audit's own answer to its own bias. Sixty-odd items were flagged as complexity that looks
removable and is not; these are the ones a simplification pass is most likely to reach for.

**The two-layer safety model, in full.** `dry_run` and the transport guard are not redundant: one
is the executor's decision, the other a property of the host a browser cannot reach. Collapsing
either is the single worst change available in this repository. Likewise the two mutation guards'
**classification** logic (`request.extensions` versus a ContextVar, `_GET_SHAPED_MUTATIONS`,
`_benign_shape`): only their refusal *sentence* is duplicated, and only that should be shared.

**Predicates that look like one function with a flag.** `_may_send_unmeasured` versus
`size_confirmed` (the first is strictly stronger and the comments name the hole collapsing them
reopens). `_size_contradicts` versus `_size_unconfirmed` (one abstains, one stands down).
`_send` versus `_mutate` (a retried DELETE double-applies, so the retry decision must not sit
behind a boolean). `Throttle` versus `RateLimiter` (different granularities, rule 11/98).
`reapIsNoop` versus `showReapIsNoop` versus the bulk bar's check (rule 48: three distinct
questions).

**Three-state returns that look over-engineered.** `_movie_is_gone` returning `bool | None`,
`season_final_episode`'s `None`/`{}`/populated plus `episodes_unreadable`, `Known`/`Absent`/
`Unknown` throughout `build_facts`, `defers_to_owner`. Every one separates "we do not know" from
"no", which is rules 1, 93 and 143 and the whole fail-closed posture. Folding any of them cost
something real once, and the incident is in the comment.

**Sets of parallel branches that look like copy-paste.** The four progress-gap flags in
`plan_series_prune` (four causes, four remedies, four sentences, #489). `sync_protection_lists`'
four separate `_retire` calls (the per-family `when=` predicate is rule 115). `inspect`'s five
"shallow mirror" branches (one exists solely to speak where the other four go quiet). The
three-state safety read in `ReapConfirm`'s announce effect.

**Structures that exist because a specific thing broke.** `_JournalRow`/`_Terminal` (a rollback
expires every ORM attribute, #327). `QuantityInput`'s `mine`/`seen` ref pair. `GeneralPanel.tsx`'s
`seeded` and `ready` state (#139). `library_index`'s `degrade` rebinding (#513). `degrade()`'s
sentence-terminating logic (#514). `restore.apply_pending_restore`'s two markers and
`_DB_SIDECARS` (crash windows, rule 126). `PolicyEditor`'s scroll listener, where an
`IntersectionObserver` would leave the Deletion section permanently unmarkable.

**Test machinery that is doing its job.** The 12,621 docstring lines (only 78 tests have a
docstring longer than their body). `_policy_lab.py` and its 754 KB de-identified vector fixture,
which replays through the production `judge_facts` and ships with its generator. The 180
`respx`/`MockTransport` sites, which are the correct depth. `frontend/src/test/setup.ts:97-131`,
three throwing gates that are rule 135's only implementation. `test_api_type_mirror.py`'s
hand-reconciled counters (rule 145, correctly applied). `test_repo_hygiene.py`'s per-file count
dicts, where roughly half the counted sites are deliberately undivided.

**Prose that is the record.** `identity.py`'s 47%, the frontend's 31%, `models.py`'s per-column
docstrings explaining what each NULL means. Rule 7/24 makes these checkable claims, and #550 is
what happens when one stops being true. Cutting them is cutting the reason, not the code.

## Not in scope, and why

**Dependencies.** Every entry in `pyproject.toml` and `frontend/package.json` traces to a real
import, and each non-obvious one already carries its justification. No unused dependency exists on
either side.

**CI workflows.** The only duplication is the website build, written twice on different triggers
(~20 lines). A composite action if a third caller appears; two does not pay.

**The rules system.** 1,021 lines across five files, but the scoping works: an agent in
`src/reaper/**/*.py` loads 822 and one in `frontend/src/**` loads 598. Only rule 103's placement
was wrong, and that was one cross-reference line, landed under rule 66 with #551 (4.3).

**`docs/history/`.** 9,437 frozen lines, never loaded by a scoped path, holding the finding IDs the
rule numbers cite.

**An automated dead-CSS sweep.** The measurement is the argument against it: 11 selectors have no
literal occurrence in TSX and **9 of them are alive** via template literals (`kind-${kind}`,
`state-${state}`, `strip-${verdict}`). An automated sweep would have deleted nine live styles to
remove two dead ones. `docs/CSS_SPLIT_PLAN.md`'s "by hand, or not at all" is correct.

## Verification protocol

Any change from this plan carries, in the same commit:

1. **Its gate, run alone and read by exit code** (rule 134). Not piped to `tail` or `head`.
2. **The test that pinned the behavior before the change, still passing unmodified.** Where this
   plan names a pinning test, that test must not be edited in the same commit that changes the
   code it pins. If it has to change, the finding was wrong about the mechanism.
3. **A grep for the siblings** (rule 72), with each one fixed or deferred *in writing*.
4. **For anything classed `safety-path`**: the full `tests/test_reap_loop.py`,
   `tests/test_guarded_transport.py` and `tests/test_plex_guard.py`, plus a driven end-to-end pass
   (the `verify` skill) rather than green tests alone.
5. **For anything classed `behavior` on operator copy**: a test asserting the exact string, added
   if one does not exist. Several strings in wave 3 are pinned only by tests that check structure.

And before the sub-PR merges into the branch:

6. **A clean baseline diff** (S8), or, for the phases S6 names, an explained one, with the moved
   lines named in the PR body. Read that list from S6 and nowhere else: this line used to carry its
   own copy, and the copy omitted phase 8 — every `safety-path` item and all of W11's `behavior`
   work, whose legitimate diffs it would have called a stop.
7. **A `/reaper-review` pass** (S9), with each finding fixed or answered in the PR body.
8. **The [Progress](#progress) rows moved** in the same commit (S10): the phase row, the *Landed*
   row, and *Killed while executing* if the session disproved a finding.

Waves are landable independently and in any order, but wave 1 first is strongly preferred: it
removes ~2,000 lines that waves 2 and 3 would otherwise have to read, move and reason about.

> **Superseded by [Execution](#execution) on ordering.** The paragraph above predates the phase
> table and is kept because its reasoning still holds. The phase table is the order to follow.

---

# Second pass

Run 2026-08-07 against `4b73f14`. Twelve read-only lanes, each sliced by an **axis** instead of a
directory: concept vocabulary, abstraction altitude, the data model, control-flow shape,
concurrency and caching, frontend state, frontend components and CSS, the HTTP payload, the module
graph, the error and message supply chain, build and startup, and the test suite.

Every lane was handed the findings above and told not to repeat them. Nothing here overlaps waves
1 to 4 except where it makes one of those items cheaper, which is said in place. The verification
protocol above binds this pass unchanged.

## What slicing by axis found that slicing by directory could not

**The four kinds of waste above all live inside one file. Almost everything below crosses a
seam.** A pass that reads `services/` and later reads `api/` sees each half of a hand-copied
record once, and neither time as a copy. Fifteen findings here are one record declared twice on
opposite sides of a boundary with a hand-written converter between them, and four of those pairs
have already disagreed.

The headline stands with one correction. The codebase is not over-engineered; it is
**under-enforced**. Three shapes recur across all twelve lanes:

- **A rule stated in prose and implemented by hand at every site.** Rule 40's control standard is
  written out in `00-tokens.css` and implemented by nothing, then re-declared at 10 sites. Rule
  94's 500-key bound exists only in prose, spelled five ways in code. Rule 56's paging contract is
  cited by four loops that each re-derive it. Rule 88's case-fold is three helpers whose
  docstrings cross-reference each other, plus 28 inline copies.
- **A carrier type that exists and is never passed whole.** `Display`, `SeasonPolicy`,
  `ProfileSettings` and the `Explanation` models are each the parameter object the plan above asks
  for, already written, already tested, and unpacked into loose fields at the one call site that
  should hand them over intact. Wave 3's parameter-object item gets *cheaper*, not larger.
- **A mirror with no drift guard.** 4.3 named this for cross-language enums. This pass adds seven
  more mirrors on the same footing, three of which are already wrong.

That changes what most fixes look like: **one declaration plus a gate**, rather than a shared
helper. And it explains the one structural gap: `test_repo_hygiene.py` has 65 tests and not one of
them looks at an import, a mirror direction, or a column with no reader.

## Wave 5: one record declared twice

The largest new category, and the one with the clearest fix. In each row the second copy is
hand-written, and the code that writes it is the code that would have to be edited when a field is
added to the first.

| Site | The two copies | Lines | Risk |
| --- | --- | --- | --- |
| `snapshot.py:1600` vs `engine/explanation.py:226` | The stored explanation built as a 110-line hand-typed dict on the write side, declared as Pydantic models on the read side. The reader's own docstring (`:234`) records that `keeps` and `match` were **silently dropped** here until the fields were declared, which is why the panel's keep breakdown never rendered. `facts_codec.py:40` is the in-tree precedent that raises at import on an unhandled field | ~30 net | `behavior` |
| `snapshot.py:1286` `Display` | The 15-field carrier exists; `RawItem` and `SeasonJudgment` both re-declare its fields flat, then `:1057` and `:1157` re-pack them field for field, then `_judge_item` unpacks it again | ~60 | `none` |
| `season_scan.py` `gather` | Nine loose policy fields taken one frame above the `SeasonPolicy` that groups them, re-packed in `gather`'s own body. This is the sole reason the second road exists: `SeasonPolicy.from_body` is the same nine assignments written again, and `season_evidence.SeasonPolicy`'s docstring already names it "rule 144's shape". **Landed, #708**, and the row's three line citations were all wrong, so it is anchored by symbol now | ~40 | `safety-path` |
| `api/breakdown.py:22` + 6 siblings | 18 identically named fields copied by hand from the service dataclass to the wire model, plus a nested list re-packed 4 for 4. Same shape at `api/backup.py:214`, `api/fairness.py:159`, `api/settings.py:547` **and** `:626`, both `SeerrServiceOut(` (written twice; the second
citation read `:831` from the start, which was a banner comment even then), `api/runs.py:776`, `api/review.py`'s `_deep_links` (`return LinksOut(`) | ~180 | `none` |
| `api/runs.py:741`/`:775` `ProfileSettingsIO` | A 7-field record declared twice, **including re-typed `ge`/`le` bounds**, with a hand-written converter in each direction. Rule 131 wants a consumer's bound derived from the producer's; here it is transcribed | ~16 | `none` |
| `api/simulate.py`'s `_refused`, `_replay_simulation` and `simulate`, one `return SimulationOut(` each | `SimulationOut`'s 14-field constructor assembled verbatim at three sites. `no_longer_condemned` already went wrong exactly this way once, recorded in `schemas.py`'s `SimulationOut` ("owner actually needs before saving") | ~25 | `none` |
| `api/schemas.py`, once, since #662 | `PlexServerChoiceOut` **declared twice under the same class name in two modules**. Pydantic collapses them in `components.schemas` today; the moment either gains a field both operations get module-qualified component names and any generated client breaks silently | 8 | `none` |

**One caveat, and it is the reason this wave is not risk-free.** Building the explanation from the
model emits `defers_to_owner` and `unestablishable` as `null` where they are absent today. That is
semantically identical per the field's own docstring and `explanation_json` is in no hash, but no
test asserts a fired entry's key set, so the pinning test has to be written first.

> **Corrected: W5-1 is `safety-path`, not `behavior`.** The hash question is answered correctly and
> is the wrong question. Two deletion-path readers parse the stored explanation:
> `executor._equivalent_keys` (`:1888-1911`) raw-parses `match.merged_rating_keys` to build the key
> set the **streaming veto** and the played-since-approval check consult, catching only
> `(ValueError, AttributeError)`; and `condemned.reap_override_verdict_decoded` (`:167-226`)
> decides whether a hand reap condemns.
>
> Three more things the caveat misses. `Explanation` is already the *wire* model
> (`schemas.py`'s `CandidateDetail`), so making it the writer welds the on-disk format to the API: a later
> `exclude_none` or alias change made for the wire silently changes what is written to disk. And as
> declared it would **drop** an unhandled write-side key rather than raise, because Pydantic
> defaults to `extra="ignore"` — the cited precedent raises at `facts_codec.py:67`, so the rebuild
> needs `extra="forbid"` or it reproduces the exact incident this row is written about, moved to
> write time where no reader can recover it. `_explain` also writes the two flags on
> `protections_unknown` alone deliberately (`explanation.py:124-129`); building all three lists
> from one model writes them on the fired copy too, making that docstring false.
>
> Smaller corrections: `Display` is 16 fields, not 15, repacked at `:1057` and `:1160`; `RawItem`
> re-declares 10 of them, not all 16. `Explanation` is declared at `engine/explanation.py:213` and
> reaches the wire through `schemas.py`'s `CandidateDetail.explanation`. **This correction's own
> two figures for the "rule 144's shape" comment were both wrong**, measured at `df1a44c` while
> W5-3 landed: the comment stood at `season_evidence.py:131` and `:121` was a blank line, and the
> two citations the plan actually carried were `:131`, right, in W3c's parameter-object paragraph,
> and `:140`, wrong by nine, in W5-3's row, where it pointed at the `keep_last_seasons` field
> declaration. Neither `:121` nor `:130` appeared anywhere in the document. Both sites are anchored
> by symbol now. W5-4 names `runs.py:776`, which is
> W5-5's site in the reverse direction, and its real population is ~13 sites, not 7. W5-5 is not
> `none`: these are the deletion caps, and collapsing the models changes the 422 the operator sees,
> because FastAPI's own validation fires before `update_profile`'s hand-formatting. W5-6 has two
> verbatim copies, not three — `_refused` (`api/simulate.py`) is the refusal shape. W5-7's line numbers were wrong in the row AND in this
> correction, and are past tense rather than re-pointed: the two declarations stood at
> `api/auth.py:230` and `api/plex.py:129` when #662 collapsed them, and those lines now hold
> `PlexPollOut` and `PlexLinkPollOut`, which is the shape a re-anchor cannot see, and the duplicate **masks the mirror test**:
> `_server_models` buckets on `__name__`, so the two classes collide into one key and a future
> divergence is checked against only the survivor.

> **Killed: W5-1's collapse. The pinning test lands as the gate**,
> `test_engine_derivations.TestTheStoredExplanationIsWrittenAsItIsDeclared`. Built first, measured,
> then killed on the measurement.
>
> **There is no live drift to fix.** The writer's 13 top-level keys, `match`'s 6, a signal row's 8,
> a keep's 5 and a `protections_unknown` entry's 4 are exactly the fields the models declare, and
> `protections_fired`/`protections_checked` carry `{gate, detail}`, which is the documented
> asymmetry. Every raw `.get` in `src/` reads a declared key. So the row buys a guard against the
> NEXT divergence, which is what CLAUDE.md's "write the gate instead" clause is for.
>
> **The collapse, built as the row asks, drops the entire match block.**
> `Explanation(match=MatchOut(...))` yields `match=None` on `dev` today: `_thaw_match` is
> `mode="before"` and reads a submodel instance as "not a mapping". Nothing raises, and the
> correction's mandated `extra="forbid"` does not see it, being about unknown KEYS.
>
> **C9 drove what that costs**, 31 scenarios per tree. The streaming veto stops seeing the second
> listing of a merged bind, `watched_now` True to False. The played-since-approval check does the
> same, on the movie and the season query alike. And a bad Plex match stops holding a hand reap,
> `protect` to `condemn`, because `condemned.bad_match` reads an absent match block as genuinely
> absent. That is the permissive arm rule 96 exists to keep off an unreadable one.
>
> **The suite catches one of those and it is the cosmetic one.** Against the collapse, 4,162 tests
> pass and the only pre-existing failure is
> `test_signal_state.TestWhatReachesTheWire.test_the_score_is_the_whole_number_the_decision_used`,
> on `score` going `52` to `52.0`. Both runs were against copies of `src/` under `/tmp`, so the
> running-from-`/tmp` artifacts (six `test_launcher` failures and sixty collection errors out of
> `alembic_dir()`) appear on each side and cancel; the count above is the collapse tree's own.
> Zero pre-existing tests see the dropped match block. The four that do are the four written
> before the decision, which is the caveat's own instruction landing.
>
> **The general reason.** The read model's three `mode="before"` validators exist so an illegible
> STORED byte degrades one field instead of blanking the whole panel. On the write side the same
> leniency normalizes a writer's own value to `None` with nothing raising: `thaw_threshold(70.5)`
> and `thaw_threshold(True)` are both `None`. One model cannot be lenient for a reader of
> ten-month-old rows and strict for the writer, and the lenient half is the one that ships to
> disk. Two silent widenings come with it, `score` and `keeps[].max_discount` both int to float,
> because the read annotations are as wide as a legacy row needs. `_server_models`' pinned count
> moves 140 to 141 across three tests, `engine.explanation` being an INNER module of that walk,
> which is the correction's "already the wire model" measured.
>
> **What lands.** The walk derives its blocks off `Explanation` rather than listing them, so a
> nested block added there enters it without anyone extending the test (rule 145). It pins the
> block LABELS rather than a count, since a total survives a redistribution across the three
> protection lists that empties one, and the one deliberate asymmetry is a named exception. The
> round trip asserts `merged_rating_keys` raw as well as through the model, whose lax
> `list[int] | None` reads a stored `["4242"]` back as `[4242]` where `_equivalent_keys`' own
> `isinstance(value, int)` filter comes back empty. Driven red six ways: a written key the reader
> does not declare, a declared key the writer drops, the two flags written on the fired list, a
> nested block added to the reader as `MatchOut | None`, the same block spelled `list[X] | None`,
> and an entry moved between protection lists. `explanation.py:124-129` and `:147-150` claim the
> two flags are never written on the other two lists, which had no test at all and now has one
> (rule 7/24). Both source files name the gate and the failure message names both files
> (rule 144).

> **Killed: W5-2, the `Display` collapse.** `_judge_item` already takes the carrier as ONE
> parameter and both packs already pass it whole, so the row removes no parameter and no net
> line. `Display` is 15 fields today and the correction's 16 was right for the tree it was
> written against: #600 took `poster_url` out of `Display`, out of `RawItem` and out of both
> packs, which is the same reason `RawItem`'s overlap is 9 rather than 10. `SeasonJudgment`
> shares 14 of the 15. **Three of the movie lane's nine are join keys read on the identity
> path** — `imdb_id`, `tmdb_id` and `year`, at `snapshot.py:345`, `:346`, `:372`, `:439`,
> `:440`, `:518` and `:527` — so nesting them files a join key under a docstring opening
> "None of them decide anything" and turns `item.imdb_id` into the spelling rule 29/106's
> sweep greps for. The season half is clean and nets to zero, which S5 names by name, and the
> 15-line unpack survives any version of the row because `library` is written as
> `library_title`. `snapshot.py` is never split, so nothing downstream is owed the shrink.
> **Not the same work as W3c's `_judge_item` line**: that one is about 27 parameters and this
> row removes zero of them.
>
> > **Corrected: the collapse stays killed and the hazard under it lands as a gate**,
> > `test_every_display_field_the_source_carries_reaches_its_lanes_pack`. The kill measured
> > parameters and lines and stopped there, so it never said what the two packs cost. **All
> > fifteen fields default to `None`**, so a field set in the movie pack and forgotten in the
> > season pack raises nothing and mypy sees nothing: they are two hand-written mirrors of one
> > dataclass (rule 103). This row itself found that three of them are identity-path join keys,
> > and `title_slug` is a fourth reader off that path, so the miss is a dropped Scales join or a
> > dead Sonarr link rather than a blank column.
> >
> > **The permitted omissions are derived, not listed.** A pack may leave a field at its default
> > exactly when its own source record does not declare it, so the gate needs no edit when a
> > sixteenth field arrives. That covers all four omissions on this tree: `RawItem` has no
> > `group_key`, `group_title` or `title_slug`, and `SeasonJudgment` has no `video_resolution`.
> > Driven red five ways: a field dropped from each pack, a sixteenth field packed on one lane
> > only, a fourth `Display(` call site, and a pack emptied so it reads as the `_NO_DISPLAY`
> > singleton, which is the fail-open the empty-count assert exists for.
> >
> > **`Display`'s docstring opened "None of them decide anything"**, which this row quoted while
> > killing the collapse and left standing (rule 7/24). It now says which four are load-bearing
> > off the verdict path and names today's four omissions.

> **Landed: W5-3, `gather` takes the carrier.** The row is right about the shape and wrong about
> its class, and the fix is the one thing in the parameter-object paragraph that was never a new
> parameter object. **The carrier already existed and `_judge_series`, the function `gather` calls
> next, already took it** — #499 converted it and left `gather` holding the loose copy, with a
> comment on the converted half reading "as on `gather` above". So the work was one frame of
> unpacking, not an invention: `gather` 25 parameters to 17, its own nine-line repack deleted, the
> scan's nine keywords replaced by `SeasonPolicy.from_body(tv_policy)`, and the second road with
> them.
>
> **Measured, against the row and against W3c's correction.** Nine fields is exact. **Two of the
> nine were REQUIRED, not defaulted** (`keep_last_seasons`, `keep_first_season`), so the population
> the correction's carrier argument covers is seven. **One production call site against 18 in
> tests**, not `plan_series_prune`'s 2-against-87, and the production one passed all nine
> explicitly, so nothing about the operator's numbers changed. `from_body` is the same nine
> assignments written again, confirmed field for field.
>
> **The correction's "`gather` is the same" is wrong, and the direction it is wrong in is the
> useful part.** `plan_series_prune`'s trap is three protective `False` defaults a caller can omit;
> `gather` has none of those three as parameters at all, and its seven defaults mirror
> `PolicyBody`'s own, which is the *protective* pole for five of them. Driven across each field's
> range on the base tree, only `keep_specials` moved the prunable set, and it moved the protective
> way. So the row's danger was never a caller getting a permissive value; it was the **second
> road** — a tenth field reaching `from_body` and not `gather`, which the simulator would then
> replay differently from the scan that stored it. Six write sites become three.
>
> **The carrier makes omission impossible rather than unlikely**, which is what the class demanded:
> `SeasonPolicy` is a frozen slots dataclass declaring no default for any of the nine. Driven, the
> base tree ACCEPTED all seven omissions and this tree REFUSES all eight, seven fields plus the
> carrier itself, at mypy and at runtime.
>
> **C9 driven, 11 scenarios, both trees, module `__file__` printed on each run**: the nine settings
> swept one at a time, the shipped defaults, and Sonarr's `series()` raising below `gather`. 1,616
> lines of guard outcomes, guard details, per-show prunable/protected splits and degrade reasons,
> **byte-identical**; the only diff is the five header lines naming the tree. 7 of the 9 settings
> discriminate on that fixture, including the widening case, `keep_last_scope=requested` dropping
> the keep-last floor and taking Show A from two prunable seasons to three, identically on both
> trees. The other two are covered by the pinning test, which is unedited below its docstring.
>
> **Three of the row's line citations were wrong and its correction was wrong about two of them**;
> both sites are anchored by symbol now, and the correction above is corrected in place.

> **Killed: W5-5's collapse. A test replaces it**, `tests/test_api.py`'s
> `test_the_wire_and_the_domain_state_the_same_bounds`. The correction's verdict is right and
> its stated reason is not: `main.py`'s validation handler already strips `"Value error, "`
> and reshapes, so a collapsed route answers the byte-identical sentence and only `loc` moves,
> `[]` to `["body"]`, which nothing reads. What the collapse actually costs is that
> `ProfileSettings` defaults all seven fields where `ProfileSettingsIO` requires five, so
> **`PUT /api/profile {}` goes from a 422 to a 200 that saves the shipped defaults over every
> cap the operator narrowed**, on a route in the API-key write allowlist
> (`api/middleware.py`), with nothing in the suite pinning it: both existing cap tests GET the
> full body first, mutate one key and PUT it back. `settings_recovered` is wire-only and dies
> with the collapse; simulated against the mirror, `test_no_paired_type_has_lost_or_gained_a_field`
> reds on it. **The row is reclassified `safety-path`** — the fields are the four deletion caps
> plus the unmeasured allowance, and the failure direction is silently widening one. Rule 131's
> ask is met by asserting the seven `ge`/`le` pairs agree, which is CLAUDE.md's "write the gate
> instead" clause.

> **Killed: W5-6's collapse. A gate replaces it**,
> `test_every_field_of_the_answer_is_compared_across_the_two_tiers`. Two verbatim copies, not
> three, as the correction says: `_refused` is the refusal shape, the only site passing
> `stale_kind`/`stale_reason` and the only one omitting three of the counts. `SimulationOut` is
> 15 fields, not 14. Any extraction takes the same 13 locals as parameters and returns the
> model, which is S5's parameter object that nets to zero. **The incident the row cites was in
> the loop, not the constructor**: `gone` was incremented in the abstain arm ~30 lines above the
> `return`, so a shared constructor would not have prevented it and would not prevent its
> recurrence. The two loops are correctly different and must stay so, since a policy edit can
> move a row condemn → protect while a threshold change cannot. Cross-site drift is already
> gated by the two-tier parity sweep over all 13 keys; the one hole was `NUMBERS` mirroring
> `SimulationOut` by hand with no rule-103 guard, and that is what lands.

## Wave 6: a rule stated in prose that nothing enforces

Each of these is a constraint the repo already believes in. The fix is to write the declaration
once and, where the violation is greppable, the gate that holds it, per CLAUDE.md's "write the
gate instead."

| Constraint | Where it is stated | How it is implemented | Fix |
| --- | --- | --- | --- |
| Rule 40's one control standard | `00-tokens.css:212`, in prose | 10 rule blocks re-declare the same 6 fields; 8 more re-declare the identical focus ring | One grouped base rule, ~70 lines, risk `visual` (source order) |
| Rule 94's 500-key `IN` bound | prose only | `_KEY_CHUNK`, `_WATCH_KEY_CHUNK`, three bare `500` literals, one `_CHUNK = 200` whose comment already enumerates the others | **Landed, #618** as `db.KEY_CHUNK` plus an AST gate |
| Rule 56's paging contract | cited by all four loops | `clients/plex.py`'s `_iter_pages` hardened and with a backstop since #684, `history_sync.py:380` with one, `library_index.py:284` with one since #559, `seerr.py:352` and `:389` with one since #653 | One `paged()` iterator, ~60 lines, risk `safety-path` |
| Rule 88's case-fold | 3 docstrings cross-referencing each other | `normalize_label`, `_tag_key`, `_name_key` are all `x.strip().casefold()`, plus ~28 inline copies across 10 modules | One `fold()`, so the rule is greppable by symbol, ~16 lines |
| The layering in *Architecture* | CLAUDE.md prose | Holds today, measured: 0 top-level SCCs, no `engine/ → services`, no upward `api/` import | A ~30-line AST test lands **green** and pins it |
| "The only path list in the repository" | `ci.yml:55` **and** `CLAUDE.md:280` | `codeql.yml` restates it twice, `docs-deploy.yml` once | Correct the two sentences, 2 lines (rule 7/24) |
| The env-boolean vocabulary | 3 declarations | `launcher.py:54` and `buildinfo.py:28` are a `_TRUE` set; `update_check.py:65` is the inverse `_FALSE`, so `TRAY=maybe` reads false while `UPDATE_CHECK=maybe` reads true, for two keys on adjacent lines of one template | One `env_flag()`, ~8 lines |
| "The tests reach no network" | `conftest.py:13`, in prose | Nothing enforces it, and one test really dials out for 15s (wave 12) | A 12-line autouse socket guard, which found **exactly one** violation and zero false positives against the 180 respx sites |

**The gate is the point here, not the line count.** Wave 6 removes about 200 lines and the value
is elsewhere: four of these seven are cases where a rule the repository cites by number is
enforced by nothing but the next author's memory.

> **Corrected: W6-3 must not ship as written, and W6-2 and W6-8 need reshaping.**
>
> **One `paged()` cannot serve five loops with five failure contracts.** plex and both seerr loops
> are complete-or-raise; `history_sync` is complete-or-**stop**, deliberately, so a short mirror
> degrades downstream through the horizon gate. A shared helper would take
> `on_incomplete: raise | stop`, turning the most safety-load-bearing property in these files into
> a keyword argument that reads as safe because it uses the hardened helper. "No total" means three
> different things across them; plex and `history_sync` advance by `len(page)` while both seerr
> loops advance by the constant, and unifying either way reintroduces a bug one of them already
> fixed; and plex is synchronous plexapi over XML. **Replace the row with: add a page backstop to
> `library_index.py:284` and `seerr.py:352`/`:389`, modeled on `MAX_HISTORY_PAGES`, currently the
> repo's only page cap.** ~10 lines, risk `none`. `library_index.py:284` having no backstop is
> confirmed and is already **#559**; the row should point at it.
>
> **The `library_index.py` half landed early, with #559.** That walk was ending on a short page,
> which reads part of a library as the whole of it, so paging it on Tautulli's reported count was
> the fix — and that removed the short-page exit, which was the only thing bounding a server that
> reports no count and ignores `start`. `_SPINE_MAX_PAGES` replaces it. Phase 8 inherits `seerr.py`
> alone.
>
> **Landed, #653, and the correction's "modeled on `MAX_HISTORY_PAGES`" holds for the shape and
> not for the trip.** That model stops and warns, because a short history mirror degrades
> downstream on its own. Seerr's two walks promise completeness to a caller that cannot tell a
> short list from a whole one, so `MAX_PAGES` raises. The correction's own risk class is right:
> `none`, since every trip lands where an `IntegrationError` already did.
>
> **W6-2's `_CHUNK = 200` is not drift and must stay out of the shared constant.**
> `watch_evidence.py:78-88` says why in source: it chunks a multi-row INSERT at four variables per
> row, so 500 there would be 2,000 bound variables — the exact rule 94 failure. `snapshot.py:1812`
> carries a bare `300` on the same footing, unlisted. The hygiene grep allow-lists those two by
> name or it flags two correct values.
>
> **The sweep is nine `IN` sites, not five.** The row inherits the plan's "three bare `500`
> literals" and that is short by four: `_KEY_CHUNK` (`api/review.py`), `_WATCH_KEY_CHUNK`
> (`snapshot.py`), and bare literals at both `_group_rollups` chunk loops (`api/review.py`),
> `snapshot.record_first_flagged_bulk`,
> `imdb_dataset.lookup`, `services/fairness.py`'s `_evidence_index` and `_distinct_episodes` and `season_scan.season_watch_stats`. Under-scoping a sweep
> is rule 72's own failure mode, so count before extracting.
>
> **Landed as #618, out of band, because #556 is one of the nine.** The grace report read the
> whole condemned set in one `IN` and nothing bounds that set, so this stopped being a tidiness
> item. `db.KEY_CHUNK` is the one declaration, all nine sites read it, and `executor.execute`
> was an unlisted tenth. The `_CHUNK = 200` and `300` above need no allow-list after all: they
> bound multi-row INSERTs, which are not membership filters and never enter the walk.
> **The gate cost more than "a hygiene grep".** A grep cannot tell a scan-sized list from a
> two-element one, so it collects every `IN` by AST and requires each to carry a written
> classification — 18 functions, 29 sites. Its first draft read source text and stayed green
> with the chunking deleted, because the comment explaining the chunking still said `KEY_CHUNK`;
> its second was blind to `imdb_dataset`'s hand-built placeholder list, a third spelling, so the
> walk and the count agreed while both disagreed with the tree (rule 147).
>
> **W6-8's guard finds nothing at the obvious hook point.** The 15-second test never reaches
> `connect()`: measured, it makes three `getaddrinfo` calls, opens zero INET sockets, and the httpx
> connect timeout fires during resolution. The guard must hook `socket.getaddrinfo`. It must also
> allow `socket.socketpair()` and `AF_UNIX` — 38 tests produced 58 of them, every one asyncio's
> loop self-pipe, which `TestClient` creates per `with` block on a worker thread. Blocking sockets
> naively breaks every `TestClient` in the suite. "Zero false positives" is vacuously true of a
> guard that fires on nothing.
>
> **W6-5 lands green only under three unstated scoping choices.** `engine/ → services` is 0 and no
> `api/` import is upward, both confirmed. "0 top-level SCCs" is **false as stated**: `notify` ↔
> `services` is a real runtime 2-cycle (`notify/discord.py:31` ↔ `services/leaving_soon.py:49`).
> The test must scope to the four packages *Architecture* names, skip `TYPE_CHECKING` imports, and
> skip function-local ones, or it is red on day one. `clients → engine` is a live edge the prose
> does not predict; pin or exempt it deliberately.
>
> **W6-7's `env_flag()` already exists** as `desktop_flag`, called from two places. The row is
> adopt-at-six-readers, not write-one. Widen it so an unrecognized value falls to `default`
> rather than to False, since on a frozen build the tray icon is the only route to Quit.
>
> **Two claims in this paragraph were measured while building it and are wrong.** The
> incompatible-unification argument does not hold: `raw not in _FALSE` and
> `env_flag(key, default=True)` are the same function on every input, so the update check
> adopts the widened helper byte-identically and no second vocabulary is needed. And the
> "live divergence" is REFUTED rather than latent: `_desktop_out` returns `None` whenever
> `launcher.desktop_platform()` is, which it always is off a frozen build, so the tray field
> is `null` and the panel renders nothing. What is true is the smaller thing, that the tray
> default is one fact written twice and agrees only because of that gate (rule 104). Landed
> at #668, which carries the measurements.
>
> **W6-4's `fold()` leaves three sites alone** — `engine/fields.py`'s `_split_csv` and `_shared`,
> and `list_config._clean_config`. The correction used to say they "omit `strip()` on purpose",
> which is the wrong reason and the more frightening one: each reads input a line above already
> stripped, so folding again would be behavior-identical. They stay because the gate bans the
> COMPOSITE only, which keeps the exemption list empty. `list_config._refuse_name_twice` compares
> SQL `func.lower()` against Python `casefold()`, ASCII-only against full Unicode, which a shared
> `fold()` makes greppable and leaves wrong; the `NOCASE` collation behind it is ASCII-only too,
> so both layers answer the same way today and the divergence is named at both ends rather than
> repaired. `alembic/` carries the idiom in five frozen revisions and is out of the walk. Measured
> at the tip rather than quoted: **33 lines, 37 expressions, 13 modules**, against the row's "~28
> across 10" and this block's "30 across 11". Landed at #669.
>
> **W6-6 is three workflows with path lists, not four copies of one.** codeql restates the same
> list twice (the rule 7/24 falsehood); docs-deploy carries a different one against `ci.yml`'s
> `site` output.
>
> **W6-1's risk is specificity, not source order.** A base rule in `01-base.css` loads before all
> ten consumers, so ties go the right way. The hazard is `:is()`, which takes its most specific
> argument's specificity and would promote every selector in the group to `.set-row .set-control
> input`'s 0,2,1. Two blocks also deviate and stay overrides, and `31-qty.css` splits the standard
> across a wrapper and its children and cannot join at all. Both counts (10 and 8) are exact.

## Wave 7: dead distinctions, not dead code

Wave 1.1 found dead *symbols*. These are dead *concepts* that still cost branches, columns, wire
fields and a fail-open guard.

- **`ListMode` (`services/lists.py`) is a second discriminator for a split `ListKind` already carries,
  and it never varies.** Every writer passes `HARD`; `SOFT` appears only in its own declaration
  and two comments. It drags along the `mode` column, the `weight` column (written at
  `services/lists.py`'s `_record_sync_error` AND its `sync` upsert, two sites not one, **read by
  nothing**), `Membership.mode`, `ConfiguredList.mode`, and **two guard
  branches that can never fire** (`snapshot.py:2669`, `:2776`), both of which skip a degradation
  check. Those two point fail-*open*, which is the direction that matters here. `protection_list`
  lives in `cache.db`, which is disposable by contract, so there is no migration. ~35 lines.
- **`CandidateOut.spared`** is a dead second name for `override == "spare"`, set literally that
  way at `api/review.py`'s `_candidate_out` (`spared=override == "spare"`). No production frontend code reads it; every render site asks `override`
  directly. It is the one of the item's spare-shaped fields that rules 120 to 122 do not justify.
  ~12 lines.
- **`HealthOut`** was an orphan model describing a response the route stopped giving, still
  declaring `destructive_actions_enabled` and `safety_note`: a wire model asserting armed state
  on an unauthenticated probe. **Deleted in #597**, which the Landed row above records; kept
  here because the reasoning is what W7-3 was.
- **`DiscordNotifier(client=…)`** is an injection seam wired zero times, and `build_notifier`'s
  docstring claims it exists "so tests can drive it" while all four test sites construct the
  notifier bare. 8 lines, plus a false sentence. The lane also reported a leaked client here; that
  is wrong, `post`'s `async with` at `discord.py:93` closes the one it opened.
- **`SignalProbeIn.window_days` and `PolicyProbeOut.detail`** are each documented *in source* as
  read by nobody, which the same file's own note forbids ("a probe kind arrives with the surface
  that asks it, or it does not exist"). ~14 lines.
- **`alembic/env.py:45` `CACHE_TABLES`** filters six tables Alembic can never see: all six are raw
  DDL on the cache engine, and Alembic is pointed at `reaper.db`. The proof it is unreachable is
  that `history_sync_state` is missing from the set and that has never mattered. Deleting both
  arms leaves `include_name` as the empty hook wave 1.1 needs for `Profile.enabled`. ~16 lines.
- **`run_migrations_offline` (`alembic/env.py:63`)** has no invoker: no `--sql` exists anywhere in
  the tree. It also duplicates seven `context.configure` options with the online path.
- **Three write-only columns beyond the ones already registered**: `ListConfig.built_in` (0
  readers, and its "never deletable" docstring is contradicted by a later migration's own
  docstring), `PlexServer.owner_plex_account_id` (0 readers; the class docstring already names
  `machine_identifier` as the anchor), and `Profile.active_policy_id` (0 readers; the policy in
  force is resolved by pure recency, and the class docstring's "only the pointer moves" is false).
  All three sit in the same `alembic check` trap, so the near-term fix is the docstrings plus the
  `include_name` list.

> **Corrected: W7-5's `window_days` is read, W7-7 has an invoker, and W7-8 needs a migration.**
>
> **`SignalProbeIn.window_days` reaches the engine.** `api/policy.py`'s `probe_policy` (`window_days=payload.window_days`) passes it into
> `probe_signal` → `evaluate_signal`, where it reaches `signals.py:399` (the detail wording) and
> `:403` (`reach_shortfall`). The frontend omits it so the default always stands, which is why it
> looks inert. Removing `PolicyProbeOut.detail` first makes it genuinely dead; removing it alone
> changes what the engine returns. Do not sweep by name: `GateSettingIn.window_days` is a
> different, heavily-read field.
>
> **#597 removed `detail`, so the kill is spent and the field is now dead: it goes, in phase 7.**
> Measured there: `reach_shortfall` is the only other thing it reaches, and it can never fire in a
> preview, because the mirror `engine.preview` hands the engine is pinned to exactly the ceiling
> the field allows. Those two numbers were `36_500` written twice in two modules with nothing
> holding them together, which is rule 131 and is what #597 fixed instead — one
> `preview.MAX_PROBE_WINDOW_DAYS`, read by the schema, driven at the boundary in both directions.
> **It was not deleted there because removing a served REQUEST field is a two-language change
> under S1 and an external contract change under S4**, which is the same reason W1.1-n is its own
> PR. Phase 5 PR 1 removed a response field and left the request alone deliberately.
>
> **`run_migrations_offline` has a test invoker.** `tests/test_migrations.py`'s
> `test_env_py_configures_batch_mode` is
> parametrized `["offline", "online"]` and drives `as_sql=True` specifically to assert what the
> offline branch configures. Deleting the function fails it — a pin to remove deliberately, not to
> discover in CI. No `--sql` exists anywhere, so the row's premise holds.
>
> **W7-8's three columns are `NOT NULL` with no server default**, so deleting the attributes breaks
> the `INSERT` on a fresh install: `plex_server` kills Plex linking, `profile` kills the first
> settings save. Constraint S2 governs. `ListConfig.built_in`'s contradiction is bigger than the
> row says — `services/list_config.py`'s `delete` states outright that every list is removable, and
> `20260804_1200_lists_arr_style.py:108` has already zeroed every row. `20260803_1900:17` names an
> `ensure_built_ins` that does not exist (rule 7/24).
>
> **W7-2's `spared` has production writers**, at `useOverrideMutations.ts:146` and `:170`, both
> optimistic-cache patches typed against `Candidate`. Dropping `api.ts:116` without them is a
> `tsc` failure. The read claim is correct and rules 120-123 do not depend on it.
>
> **W7-1 and W7-6 are confirmed clean.** `ListMode.SOFT` was never written in any shipped version,
> both columns are raw DDL on `cache.db` **with** server defaults, and the two guard branches
> suppress a degradation, so deleting them is strictly more fail-closed. All six `CACHE_TABLES`
> are raw DDL and none is on `Base.metadata`. `HealthOut`'s deletion is already fenced by
> `tests/test_app.py`'s `test_destructive_actions_are_off_by_default` and `test_settings_api.py`'s
> `TestSafety.test_turning_deletion_on_requires_the_admin_password`, which is worth citing as the
> proof. `HealthOut` itself is gone (#597); the two tests still fence the behavior.
> `DiscordNotifier`'s docstring claim is false as described.

## Wave 8: payload weight

An axis no pass has measured. Two of these are the largest single wins in either document.

- **`/api/candidates` repeats every show-level field once per season row.** `api/review.py`'s `_candidate_out` stamps
  `group_seasons`, `group_condemned_count`, `group_condemned_bytes`, `group_unknown_size`,
  `group_title` and `show_status` onto every member, and `ReviewQueue.tsx` reads all six off
  `group.items[0]`. Measured: **157 KiB per 100-row page against 15.7 KiB**. An envelope (`{items,
  groups, total, …}`) also retires the four custom response headers that `api.ts`'s `candidates` hand-parses
  back into the object it already builds. ~15 net lines, ~90% of the show-level payload, risk
  `behavior` because the client lands in lockstep.

> **Corrected: only four of the six fields can move, and the rollup is not `items[0]`.**
> `group_title` and `show_status` are read off a **flat** `CandidateDetail` at `WhyPanel.tsx:1312`
> and `:1365`, and `api/review.py`'s `group_detail` derives `GroupOut.show_status` by iterating the nested season
> list, so both must stay on `CandidateOut`. `show_status` is deliberately
> `items.find(s => s.show_status)` at `ReviewQueue.tsx:1132`, not `[0]`, because a snapshot
> predating the field carries `null` on some rows; an envelope reproduces that rollup or blanks
> the chip. `ReviewQueue.tsx:2007` is a second `group_condemned_count` read feeding the bulk bar's
> count beside a destructive action. Six backend tests assert the four headers, and
> `api/review.py`'s `list_candidates` (`response.headers["X-Total-Count"] = "0"`) has an early-return branch setting only two of them. The ~15-line estimate does
> not survive any of this.
- **`GET /api/runs/{id}` ships the whole journal to render 50 rows**, including each step's `body`
  dict, the literal request payload. `ReapPlan.tsx:50`'s own comment says a 500-item plan is 1,500
  rows. Three components fetch it, and again on every history-row click. Cap `steps` at the same
  50 the UI draws and add `step_count`. ~96% of the body on a large plan, risk `behavior`: this is
  the journalled record an operator reads before approving, so `step_count` must render as "N
  more" and the cap forecloses paging past 50 without a route change. `confirmation_phrase` and
  the execute route are untouched.

> **Corrected: W8-2 is `safety-path`, and the obvious implementation silently uncouples the
> confirmation phrase from what gets deleted.** This is the one item in this document that can
> delete files nobody approved.
>
> `_run_steps` (`api/runs.py:88`) feeds two consumers: the serialized `steps` list **and**
> `_planned_candidates` (`:205`), which is where `item_count`, `total_bytes` and
> `confirmation_phrase` come from. The execute route re-derives `expected` through the same helper
> at `runs.py:522`. A `LIMIT 50` placed in `_run_steps` or `_planned_candidates` shrinks the
> detail route and the execute route *consistently*, so the typed phrase still matches, while the
> executor loads its own steps independently at `executor.py:622` and deletes every one. The
> operator types `REAP 50 SOULS` and 500 go.
>
> **The cap is only safe applied at the `steps=[...]` serialization inside `_run_out`.** The
> commit carries a test that plans more than 50 steps and asserts the phrase still names the full
> count, and C7 in [Execution](#execution) is the checkpoint for it. The plan's claim that the
> execute route is untouched is true of exactly one implementation and the plan does not say which.
>
> `step_count` also has to land in `api.ts` in the same commit (S1), even though rendering it can
> follow.
- **Eleven response fields are shipped on every response and read by nobody**: `LeavingSoonOut`'s
  three, `SignalCountOut.bytes`/`.unknown_size` (two ints per row of `condemned_by`),
  `ReapBreakdownOut`'s two `_unknown` counters, `PlexTrashOut.empties_after_scan`, `RunOut`'s
  three hash and approver fields, `RunSummaryOut.approved_by`, `UserOut.email`,
  `RestoreSummaryOut.revision`. Each must drop from the Python model and its `api.ts` mirror in
  one commit or the mirror test fails. `empties_after_scan` is the exception: its docstring claims
  a page reads it, so it is a rule 25 decision (wire it or drop it), not a deletion.
- **Eleven routes return a bare `dict[str, bool]`**, so the published contract types them as
  `Record<string, boolean>` with an anonymous title. Two shared models (`RemovedOut`, `OkOut`) and
  one `JobRunOut` fix it with byte-identical JSON on the wire.
- **`TestOut` carries three fields only one of its three routes can populate**, so the contract
  says a Discord webhook test may return Sonarr root folders. Rule 25. ~10 lines.

> **Corrected, W8-3 to W8-5.** The "eleven unread fields" are **14 across 8 models**, and
> `RunOut.approved_at` is a fourth unread audit field the list misses. `PlexTrashOut`'s docstring
> does not claim a page reads `empties_after_scan`, so that row's rule 25 framing rests on nothing;
> the substance holds, since `usePlexTrash.ts:44` is the surface that would use it. The three
> `RunOut` hash and approver fields are safe to drop from the *response*: every interlock reads the
> DB row (`executor.py:975`, `:995`), never the wire model. Ten routes return `dict[str, bool]`,
> not eleven, and **`api/backup.py`'s `restore_cancel` returns two keys** (`ok` and `cleared`, the second read at
> `api.ts`'s `restoreCancel`), so it needs its own model rather than a shared `OkOut`. `api/backup.py`'s `restore_restart`
> (`-> JSONResponse`, no response model) is a
> fifth anonymous-payload route the sweep misses (rule 72). W8-4 needs no lockstep; W8-3 and W8-5
> do.
>
> These are a published contract, not the SPA's private wire: `/api/openapi.json` is served and an
> API key reaches it. "Read by nobody" is measured against the SPA alone (S4).

## Wave 9: the module graph

Measured: **zero top-level cycles**, and the tree measures itself now.
`tests/test_repo_hygiene.py`'s `test_every_import_cycle_under_src_is_one_someone_declared` walks
every module under `src/reaper` and holds the graph to a declared set, so read the figures there
rather than here. This line's "108 Python modules, 514 edges" is 116 and neither: an edge count
depends on how a walk resolves `from package import name`, and the gate's is 677 at top level.
Zero top-level cycles is the durable half and still holds. Every cycle is already broken; the
question is which breaks earn their keep, and most do not.

- **All 8 remaining cycles pass through `reaper.launcher`, and 6 of them exist for one string.**
  `services/backup.py:56` and `restore.py:62` import the application entry point, which owns
  uvicorn, the tray and AppKit, to read `LAUNCHER_CONF_NAME`. Moving that constant to `config.py`
  is ~4 lines and de-lands `reaper.launcher` from `services.backup`'s 347-module import closure.

  > **Landed at #671, and every figure in the row above describes a tree that is gone.** The
  > constant is `config.LAUNCHER_CONF_NAME` and neither service imports `reaper.launcher` any
  > more, so both line citations are dead. The closure was **304 before and 278 after**, never
  > 347. **"Which owns uvicorn, the tray and AppKit" is wrong about what the import cost**:
  > all three are deferred inside `launcher.py`, so none of them was ever in that closure. The
  > 26 modules that left are `reaper.launcher` itself plus 25 stdlib it pulls at module level,
  > `webbrowser` and the `urllib` / `http` / `email` chain behind it. Measured either side, on
  > the tree #671 landed against.
- **`api/auth.py` holds five helpers it never calls**, and `api/backup.py` and `api/settings.py`
  reach across into its underscore namespace for them. `_verify_admin_password` and
  `record_password_failure` appear exactly once in `auth.py`: the `def` line. This is the gate
  that guards arming deletion, living in a private namespace two other routers depend on, and **no
  test imports any of the five**. Move them to the `api/deps.py` wave 3 proposes, drop the
  underscores, and add the missing test in the same commit. ~90 lines moved, risk `safety-path` as
  pure motion.

  > **Landed at #681, together with W3's ritual, which is what the contradiction paragraph
  > required. Every present-tense sentence in the row above and in its correction block now
  > describes a tree that is gone.** None of the six names appears in `api/auth.py` any more, so
  > "`_verify_admin_password` and `record_password_failure` appear exactly once in `auth.py`" and
  > "`_client_ip`, `_throttled` and `_busy_hashing` all have call sites in `auth.py`" were both
  > true when written and are both false now. Six functions moved and `api/auth.py` imports three
  > back, an `api → api` edge inside one layer. Two of the six stay private in `api/deps.py`: after the ritual collapses,
  > `_record_password_failure` and `_verify_admin_password` have exactly one caller each, so the
  > private namespace two routers used to reach into is now reachable by nobody. **"Pure motion"
  > understates the risk and the row should have said so**: the four call sites look
  > interchangeable and two of them are conditional, `set_safety` gating only `if payload.enabled`
  > because turning deletion OFF is deliberately ungated, and `set_admin_password` gating only
  > when a password exists and the session did not come through recovery (#433). Hoisting either
  > call out of its branch is a behavior change that no test at the base could see.
- **Three cycle-breaking workarounds in `scan_runner.py` break no cycle** (a `TYPE_CHECKING`
  import, the same symbol imported again inside a function, and a third function-local import),
  verified empirically. `executor.py:137` `TYPE_CHECKING`-imports a module already imported at
  `:118`. `api/simulate.py`'s `_replay_simulation` function-local `build_gates` import breaks nothing and carries no comment, unlike
  `launcher.py:531` and `:559`, which name their reasons and stay.

  > **Landed at #677, four of the five, and the executor's is killed.** The three in
  > `scan_runner.py` and `simulate.py`'s are promoted to module level. The runtime import graph
  > is unchanged either side, 679 edges and the same two cycles, because the walk in
  > `test_repo_hygiene.py` counts a function-local import as an edge already; what moved is the
  > top-level graph, 674 edges to 677, still acyclic, and all 116 modules import in isolation.
  > `_DEFERRED_CROSS_PACKAGE_IMPORTS` goes 3 to 1.
  >
  > **`launcher.py:559` now names its reason, and it is the only one of the two that was
  > missing.** `:531`'s environment-ordering comment was already there and is right. The other
  > explained why the factory is passed as an object, never why the import sits in the function.
  > Measured: promoted to module level, `main`, `api.settings` and `api.plex` all raise
  > ImportError, and the two cycles `_KNOWN_IMPORT_CYCLES` declares move into the top-level
  > graph. That edge closes both.
  >
  > **Killed at #677, un-killed by the owner, and landed at #683.** The kill's measurement was
  > right and is not what changed: `services/executor.py` imports `reaper.clients.plex` at `:118`,
  > so the `TYPE_CHECKING` block below it named two more symbols from a module the runtime graph
  > already had an edge to, and deleting it moves no graph, changes nothing a module import loads,
  > and cannot change behavior, both names being read only in a `Protocol`'s annotations under
  > `from __future__ import annotations`. What the kill refused was the *price*: any diff in
  > `services/executor.py` is `safety-path` and buys a driven pass plus a C9 read, which S5 says
  > not to spend on zero. #683 was already spending both for the size interlock in the same
  > file, so the price was zero a second time. The two names move up to the `:118` import that
  > was already loading their module. `TYPE_CHECKING` leaves the `typing` import with them, having
  > no other reader in the file. `_DEFERRED_CROSS_PACKAGE_IMPORTS` goes 1 to **0**, and the
  > written classification the kill attached to the site goes with the site.
- **Both frontend cycles are one borrowed symbol each.** `PolicyEditor ↔ PolicyRuleEditors` exists
  because the deliberate split left three lookup tables behind, and the same file re-exports
  `humanDays` from `format.ts` whose own comment says it moved there to break a cycle back through
  this module. `ScalesPanel ↔ UnmatchedList` is a 12-line presentational fallback.
- **12 modules import `clients/base.py` for `IntegrationError` alone** and pay a 384-module httpx
  closure. `api/scan.py` is the clean case. A leaf `clients/errors.py` with a re-export during the
  move keeps every `except` clause identical.

  > **Killed. The closure is identical either side of the cut, for all 13, measured one module at
  > a time.** The correction below said the transitive path survives; this is that claim as
  > numbers. Every one of the 13 still reaches `clients/base.py` through something it imports for
  > real work, so cutting the direct edge removes **zero** modules from **every** one of their
  > closures: `api/scan.py` 55 either side through `services.scan_runner`, `api/fairness.py` 32
  > through `clients.seerr`, `services.update_check` 6 through `clients.public`,
  > `services.library_index` 10 through `clients.plex`, `services.plex_link` and `services.login`
  > through `clients.plextv`. Not one is the leaf the row imagines. Counts exclude the module
  > itself, and hold under all four graph conventions: every import, module-level only,
  > `TYPE_CHECKING` dropped, and both.
  >
  > **`api/scan.py` is not "the clean case" the row calls it, and it is the sharpest one here.**
  > It is the only one of the 13 that imports no client at all, so `clients/base.py` is its whole
  > client edge and the row reads it as the leaf that proves the point. It reaches base through
  > `services.scan_runner` and `services.snapshot` instead, which it imports to run a scan. The
  > module with the best case for a leaf error type has no case at all.
  >
  > **Three figures in the row are wrong, and the population is 13.** 23 modules name
  > `IntegrationError`: one defines it, two name it in a comment
  > (`services/grace.py:111`, `services/history_sync.py:481`), 20 import it, and **13** import it
  > and nothing else. 14 if `errors.py` also carried `SafetyViolationError`, which is the only way
  > `services/executor.py:117` joins them.
  >
  > **The closure is 339 and the row's 384 is probably a total, so read both conventions.**
  > `import reaper.clients.base` in a fresh interpreter adds **339** modules and leaves
  > `sys.modules` at **387**, against a bare interpreter's 48, so a stale 384 total is the
  > likelier reading of the row than a wrong delta. **What is wrong either way is the word
  > httpx.** 145 of the 339 are non-stdlib
  > and pydantic's family is **74** of those (`pydantic` 45, `pydantic_settings` 22,
  > `pydantic_core` 3, `typing_inspection` 3, `annotated_types` 1), against structlog 20 and
  > tenacity 11. Modules spelled `httpx2.*` are 24, and `import httpx2` alone costs 159, so
  > neither number is 384 and the largest block in the closure is not httpx.
  >
  > **The cost is a module and a second declaration, against a benefit of nothing.**
  > `_EXPECTED_LAYERED_MODULES` and `_EXPECTED_SOURCE_MODULES` both move for a leaf holding one
  > `class`, and the re-export the row proposes to keep `except` clauses identical is one name
  > importable from two places, which is the shape rule 103 and rule 144 exist to catch. Graph
  > tidiness does not buy that.
- **`api/runs.py:472 reap_in_flight` is run state living in an HTTP router**, imported by
  `main.py` and by `api/backup.py`, which thereby depends on an 801-line router for one boolean.
  It is the only `api → api` edge that is not `schemas`, `tags` or `auth`. Risk `behavior`: it
  gates a database-lock interaction, and `tests/test_scheduler.py:357` names the chain in prose,
  so that comment moves too.

  > **Killed. Nothing imports `api/backup.py` without importing `api/runs.py` too, so the edge
  > this removes is paid by no one.** Cutting it does move the graph, unlike the row above:
  > `api.backup`'s static closure goes 73 to 38, dropping every client and the whole
  > executor/planner/snapshot chain. That figure has no consumer. All three importers of
  > `api/backup.py` already import `api/runs.py` in the same breath: `main.py` at `:30` and
  > `:50`, `tests/test_restore.py` at `:39` and `:41`, and `tests/test_api_type_mirror.py`
  > through the `pkgutil` walk over `reaper.api` that its own wire check runs on. A process
  > loads 931 modules through `reaper.main` either way, on the same convention as the 339 above:
  > `sys.modules` reaches 979 against a bare interpreter's 48.
  >
  > **Every write is in `api/runs.py`, so every destination splits the reader off from them.**
  > `app.state.reap_status` is *assigned* at exactly one site in `src/`, `_reap_status` at
  > `:468`; the object's fields are then mutated by 34 statements in ten regions of the same
  > file, `:549` to `:762`, the first twelve being `execute_run` claiming the slot and resetting
  > every field in one synchronous stretch. The correction's "created fresh by `create_app`" is
  > the one thing in it that does not hold: nothing in `main.py` touches `reap_status`, and
  > `_reap_status` makes one lazily on first reach. Its conclusion survives anyway, a fresh app
  > having no status to reset. `api/deps.py` is the destination that costs no new module and it
  > is the worst one, separating `reap_in_flight` from `_reap_status`, which is the same
  > `getattr` written to create rather than to refuse. A new `api/reap_state.py` holding the
  > model and both accessors keeps that pair together and still leaves all 34 mutations behind,
  > for +1 on both module counters. `services/` would put a response model and a `FastAPI`-typed
  > accessor below the layer that serves them.
  >
  > **"The only such edge" is five, and one of the three names exempting them is dead.**
  > Excluding `schemas`, `tags` and `deps`, the `api → api` edges are `backup:43 → runs`,
  > `runs:32 → scan`, `plex:54 → settings`, `simulate:30 → policy` and `simulate:31 → review`.
  > The correction found the second and stopped. Nothing under `src/reaper/api/` imports
  > `api/auth.py` at all since W9-2 moved its six helpers to `deps`, so the row's exempt set
  > reads `schemas`, `tags`, `deps` today. **The layering gate cannot see any of the five**:
  > `test_the_four_packages_import_only_downward` walks cross-*package* edges, and all five are
  > inside one package.

> **Corrected, wave 9. Four claims are wrong and one buys nothing.**
>
> **`clients/errors.py` saves no import cost.** All 12 modules still reach `clients/base.py`
> transitively after the direct edge is cut — `api/scan.py` imports `services.scan_runner`, which
> pulls all four clients. The benefit is graph tidiness, and the 384-module framing should go.
>
> **"`api/auth.py` holds five helpers it never calls" is false for three of them.** `_client_ip`,
> `_throttled` and `_busy_hashing` all have call sites in `auth.py`. Two are uncalled. The move is
> also six functions, not five: `_refuse_if_waiting` is a shared callee of a mover (`_throttled`)
> and a stayer (`_rate_limited`). The rest of the row is confirmed and better than it claims — no
> module-level mutable state exists, every limiter is a singleton in `auth/ratelimit.py` passed as
> a parameter, none of the five is a `Depends()`, and no test imports any of them or drives any of
> the four gates with a wrong password.
>
> **`reap_in_flight` is not run state.** It is a two-line pure accessor over `app.state`, created
> fresh by `create_app`, so nothing needs resetting and a move cannot duplicate it. The real
> coupling is that the reader would split from its writers. Soften off `behavior`. It is also not
> the only such edge: `api/runs.py:32 → api/scan.py` is a second.
>
> **"6 of the 8 cycles exist for one string" is 7, and the 8 is 9.** Re-measured at `80d8a39~1`,
> the tree #671 landed against, under the convention `_KNOWN_IMPORT_CYCLES` uses: function-local
> imports counted, `TYPE_CHECKING` excluded. Nine, of which seven pass through `backup.py` or
> `restore.py` and went with the constant. **Two survive, not one.** The second is
> `api.plex → api.settings → launcher → main`, created by phase 6's `api/plex.py` after W9 was
> measured. Counting `TYPE_CHECKING` edges as well gives a tenth,
> `services.list_config ↔ services.lists`, not through launcher; the sentence this replaces called
> that one the ninth, and it is the one cycle in the tree that never runs. Top level alone is zero
> cycles under every convention, either side of the move. `reaper.launcher` loads neither uvicorn
> nor AppKit at import, confirmed: both are deferred, as is pystray. `config.py` is a clean leaf
> and cannot cycle. The saving is 26 modules off `services.backup`'s closure, `reaper.launcher`
> plus the 25 stdlib modules it pulls, which is where the 25 above comes from.

## Wave 10: already drifted

These are not simplifications. Two copies of one fact disagree today, and the reader sees the
disagreement.

1. **The Leaving Soon summary contradicts itself on one screen.** `services/leaving_soon.py`'s `_run_pass` summary ladder (`if result.problems:`) and
   `JobsPanel.tsx`'s `LeavingSoonRow` (`syncResult`, "A real per-library problem always wins the
   message") are the same three-branch ladder in two languages, each with its own comment. `api/leaving_soon.py:44` injects a synthetic "no libraries are turned on" problem
   *after* `_run_pass` stored its summary, so with no libraries enabled the stored row reads
   "Preview only, nothing written" with a green tick while the button's flash says "Some shelves
   didn't update" in red. **Neither sentence is pinned by any test, on either side.** Fix: compute
   the summary in the route after the merge, ship it in `LeavingSoonOut`, render both from it.

   > **Fixed, #617, on this branch rather than off `dev`.** *Entering a phase* asks a session
   > that fixes item 1, 4 or 5 anyway to branch off `dev`, and the owner directed otherwise, so
   > the fix reaches operators when this branch does and #555 does not auto-close on merge.
   > Phase 4's row stays at 4 of 4: that paragraph is also what makes this not phase 4's work.
   >
   > **The summary is the service's, not the route's.** The route cannot own it:
   > `after_scan` reaches `_run_pass` without passing through a route at all, so a summary
   > computed at the edge leaves every automatic pass storing the sentence this item is about.
   > `LeavingSoonResult` grew `no_libraries`, `ok` and `summary`, the no-libraries case moved
   > into the service ahead of the store, and `LeavingSoonOut` ships `ok` and `result` off that
   > same derivation. A **fourth** copy the item does not name was found and fixed with it:
   > `PlexPanel.tsx`'s `lsStatus` worded the preview caveat itself off `applied`, on the very
   > screen those libraries are turned on. It was also the copy with no test on either end,
   > which is how it kept that caveat and a second defect the review found beside it: it read
   > the completed pass alone, so a shelf a later scan had skipped read there as a current
   > verdict. That comparison is now one declaration (`shelfStatus.ts`) both surfaces make.

2. **`InstanceError` maps to two different HTTP statuses.** `api/settings.py` hand-writes the
   mapping five times: 422 at three sites, 404 at two. The 404 arms are correct only by accident,
   because those callees can raise only `InstanceNotFoundError` today. The docstrings already
   declare the intended status per subclass, and `RestoreError` already demonstrates the fix by
   carrying `.status`.
3. **The startup scheduler replay and `reschedule_timezone` are the same ladder**, found
   independently by two lanes. `main.py`'s scheduler block ("one scheduled job that produces new review candidates") re-implements `scheduler.py:847` guard for guard and
   log event for log event, and the function's own docstring says it uses "the same `ValueError`
   guard startup uses (rule 87)." Only the shared function is tested; the startup copy is not.
4. **Four keys in `.env.example` do nothing in a `.env.local`.** `REAPER_UPDATE_CHECK`,
   `LAUNCH_BROWSER`, `TRAY` and `DOCK_ICON` are read from raw `os.environ`, and `config.py:279`'s
   own docstring states dotenv values never reach `os.environ`. The file's header says "copy to
   .env.local." Setting them there does nothing and warns nothing.
5. **`Settings.host`/`.port` and the launcher's own `REAPER_PORT` parse disagree.** The launcher
   re-reads both from `os.environ` with a second spelling of `8420` and `0.0.0.0`. Concretely: a
   source checkout with `REAPER_PORT` in `.env.local`, which `.env.example:27` ships uncommented,
   binds 8420 while the anti-lockout recovery link prints the dotenv value.
6. **`.view-tab` hand-copies two rules from `.tab, .seg`**, and `04-buttons.css:104` enumerates
   the bold-width strut's members without naming it. Rule 144's shape, drifted.
7. **`SetupPlexStep` open-codes a 3-key subset of `invalidateAllPlex`**, whose comment says all
   three server-changing paths must run it. The wizard misses `leaving-soon-settings`, `plexTrash`
   and `watch-evidence`.

> **Corrected, wave 10.** **Item 5 is already #558's second half** — strike it or cross-reference
> it the way item 4 does. **Item 3's fix as written changes behavior**: `main.py` iterates
> `maintenance_schedules.items()` (stored values only) while `scheduler.py` iterates
> `MAINTENANCE_JOB_IDS` through `effective_maintenance_cron`, so a naive dedup changes what startup
> applies. The guards and log events do match, and only the shared copy is tested. The cite is
> `main.py`'s scheduler block ("ladders written out again here"), not `:371`. **Item 2 is latent, not shipping**: every 404 arm is correct against
> today's callees, and the base `InstanceError` declares no status, which is the actual gap.
> **Item 6 is exact copies**, and the drift is `04-buttons.css:105`'s "every control that bolds
> when active" enumeration omitting `.view-tab`. **Item 7's three missing keys are confirmed and
> there is no symptom today** — the wizard renders in place of the app and its exit is a one-way
> latch, so the consumers are unmounted while it is up and it is unreachable after. The drifted
> fact is `PlexPanel.tsx:196`'s "every server-changing path", which is five paths, not three, with
> the enforcement test pinning only `PlexPanel`. That is #550's class and belongs there.

> **Decided in phase 4, item 7 stays here.** The correction's last sentence and the phase text
> disagreed, and this settles it: **fixed in #596, not moved to #550.** Three reasons, in the
> order they weighed.
>
> **The fix is not a comment edit, so it is not #550's class.** #550 is five *backend* comments
> asserting a safeguard that is absent, and its own *Fix* section closes three of them by
> deleting the thing the comment describes. Here the three missing keys are real and the cheapest
> correct fix adds them — the comment is wrong *because* the code is, which is the opposite
> direction. Rule 79 governs this one by name ("a cache-invalidation helper claiming completeness
> is grep-verified against every query key"), not rule 7/24 alone.
>
> **Folding it in would stop #550 being closable by one commit**, which the `reaper-review`
> skill makes the test of whether sites belong in one issue. It would add a frontend site with a
> different fix shape to a backend checklist.
>
> **No symptom, verified rather than assumed.** `App.tsx:649` returns `<SetupWizard>` *instead
> of* `<Dashboard>`, and `wasNeeded` latches, so the three missing keys have no mounted consumer
> while the wizard is up. Reaching the wizard from a configured install means unlinking Plex,
> which runs the full `invalidateAllPlex` on the way. "Symptomless" is a statement about
> reachability and never a reason to leave it (rule 38/117), and the count in the comment was
> wrong on a page whose job is deciding what Reaper may delete from.
>
> The fix hoists the helper to `frontend/src/plexServerQueries.ts` so one declaration serves all
> five paths, and the guard bans a *handler* from naming two of the keys by hand. `setConnection`
> is not a sixth path: it changes the server's address, not which server, so its one-key
> invalidation is correct in both components.

## Wave 11: the rest, by lane

Landable, small, and none of it changes behavior unless marked.

**44 items across seven lanes, enumerated 2026-08-10 so the phase can count them.** The wave sat
in phase 8's scope line from the day the plan was written and was never given IDs, so the phase
had no way to name what was left and its Progress cell read as two items remaining. The ID below
is the index and the sentence carrying it is the finding body; nothing is restated. Every count
in every sentence is the second pass's and none has been re-derived since, so measure before you
build (rule 145's own lesson, and the phase has already found a `~28 across 10` that was 33 across
13 and a `three literals` that was nine sites). **Two of the 44 are not duplications**: W11-15 is
an unbounded fan-out and W11-40 is two tables that grow for the life of the install. Both are
defects, and neither's value is the lines it saves. **The owner scoped it on 2026-08-10: all 44
are in, measured first, and each judged on whether it is worth building.**

> **Measured, 2026-08-10. Both lanes scouted, then each scout put through a verifier that
> re-derived every number blind before it was allowed to read the report.** The pattern is C3's:
> derive first, diff second, so a wrong figure cannot be inherited by agreement. It caught the
> scouts five times on the frontend and four on the backend, and it caught the verifiers three
> times the other way, which is the half worth recording. The per-item numbers below replace the
> sentences above wherever they disagree.
>
> **Most stated counts are wrong, and the wave sat unbuildable because of it.** Backend: 16 of
> 25. Frontend: 8 of 19. The frontend scout said 14 of 19 and its verifier reduced that to 8,
> the scout having counted its own build-or-kill calls as wrong numbers. A count and the symbol
> beside it are two claims, and the errors split across both: **W11-1** is 17 tests and 20 reads,
> not 22 sites; **W11-5** names `scan.py` for a counter that lives in `services/snapshot.py`;
> **W11-11** is 12 in `_run_scan_locked`, not 11 in `run_scan`; **W11-41** is 15 models, not 13;
> **W11-33**'s dead branch is at line 187, not 201; **W11-42**'s `~85 lines` is ~16 actually
> removable; **W11-3** is 30 dispatch sites, not six;
> **W11-16** is 19 keys redeclared and 12 of them divergent, not four; **W11-23** is 26
> containers across 9 files, not 21 across 7. **W11-31 went the other way and is the reason a
> verdict table exists at all**: the plan's four sentences is right, and the backend scout counted
> five. A measurement pass over-counting a figure the document already had correct is the failure
> nobody looks for, and it is only visible because the verifier re-derived it blind.
>
> **Build, in value order.** **W11-15** first, and it is not a duplication: `_enrich_titles` opens
> 80 concurrent connections to one Seerr per Scales load (`_TITLE_LOOKUP_CAP = 80`, and httpx's
> default pool of 100 imposes no bound below it). Three lines, copying `leaving_soon.py`'s
> `Semaphore`. The row's justification is wrong twice over: `ConcurrencyGate` has three production
> callers (corrected from four at #717, which counted the singleton as one) and is a load-shedder
> rather than a bound. Then **W11-3**, where the deliverable is the
> test and not the table: `FieldType` does not exist in the frontend at all, `VocabField.type` is
> a bare `string`, and no test touches `bytes`, `rating_tenths` or `days`, the three conversions
> that write policy values. Then **W11-12** at -14 lines, **W11-22** and **W11-24**, **W11-16**
> narrowed to the two keys that actually diverge, and **W11-39** at -8 lines and one fewer table
> read.
>
> **Kill.** **W11-4**: the `others_watching` arm cannot fire, established rather than assumed.
> `_kept_phrase` runs only over `protections_fired`, which is written from `r.fired` alone, and
> the historical gate's PROTECT branch was unreachable and never shipped in a release. Two
> caveats the scout missed: the item is worth about -1 line, not the ~20 the row implies, and
> four cross-references cite the arm (`review.py`, `gates.py`, `WhyPanel.tsx`, `policyMeta.ts`),
> which rule 72 pulls into the same change. **W11-13**: the divergence is unreachable, since
> `retry_after`'s sole reader treats `0.0` and `None` alike. **W11-23**: 8 distinct shapes behind
> 26 containers and the extraction nets about -7 lines, which is S5. **W11-30's plural helper**:
> the 66 sites do not agree, four are irregular and ten more need verb or pronoun agreement, so a
> one-form `plural(n, "person")` prints "persons" and "librarys", a rule 21 regression. A safe
> two-form helper saves nothing. What survives W11-30 is two real sentence duplications and the
> fact that `format.ts` pluralizes four times itself, so the file to centralize into is part of
> the problem.
>
> **W11-32 is closed**, at #698, out of the same function W3b-2 was measured in. 43 remain.
>
> **W11-40 is real and its framing is not.** The exclusion is exactly as stated: `_doomed`
> excludes every snapshot a run points at, nothing anywhere deletes a `ReapRun`, and
> `ActionStep.run_id` is unindexed, with `EXPLAIN QUERY PLAN` returning `SCAN action_step`. The
> index is worth adding. **What is not a leak is the pinned candidate rows.** A verifier measured
> 29,442 of them held by 7 runs and called it 120x the table the row names;
> `services/retention.py`'s own docstring answers that, because the rolling delete cap is priced
> off those same rows and the module calls the exclusion a safety interlock. It names the exact
> narrowing that would be proposed, to live runs or to recent runs, and says it silently unprices
> the cap. Priced growth, not a leak. Any row count here came from one hand-run dev box in any
> case; the three claims that decide the item are provable from the tree alone.
>
> **`.field-sm` does not collide with phase 9's W4.1.** `grep -c "field-sm" GeneralPanel.tsx` is
> zero, so **W11-23** and W4.1 are independently landable in either order. That is the whole
> contrast with `.set-row`, which is why the two were never the same question. On `.set-row`
> itself the plan's "22 of 40" holds under no single matcher: `set-row` gives 22 of 51 and
> `className="set-row` gives 18 of 40. The W3b-4 fold stands on the majority-overlap argument
> either way.
>
> **Two defects found while measuring, both on `dev`, both filed rather than fixed here.** A raw
> `imdbId` sentinel shadowing the cleaned Plex id, so a keep-listed title is not found on its list
> (#709, and the deletion direction was the verifier's, the scout having found only the harmless
> one). And `ScanBar`'s comment claiming the scan-status poll sits quiet when idle while the
> shell's observer keeps it polling (#712, which W11-16 closes on its own if built).

**Control-flow shape.** **W11-1** A `PlexItem | None` re-tested per field at **22 sites in 3
modules**, where every inline fallback is byte-identical to the dataclass default (~20 lines).
**W11-2** The same raw field re-parsed 3 to 5 times inside one loop body in `_raw_items` (~12).
**W11-3** `field.type` dispatched
by four separate if-ladders plus two inline ternaries in `PolicyRuleEditors.tsx`, so a new
`FieldType` is silently wrong in six places with no compile error, and **no test pins the
conversions** (~20, `behavior`). **Landed at the test and the type; the table is killed.**
**W11-4** `_kept_phrase`'s 12 `if` arms, six of which return a
bare constant, where the pinning test is *already* a parametrized table over exactly those pairs
(~20).
**W11-5** `scan.py`'s `condemned` counter kept in lockstep by hand with `len(condemned_keys)`.
**W11-6** `breakdown.py`'s (count, bytes, unknown) triple written four times plus three parallel
dicts re-zipped (~15). **W11-7** `build_sources`' Radarr and Sonarr loops differing by two class
names (~14).
**W11-8** `sync_protection_lists`' four parallel slug sets and four hand-written sweep calls (~10,
`safety-path`: rule 115's `when=` predicates survive verbatim).

**Concurrency and caching.** **W11-9** Two identical bounded poll loops in the executor (~10).
**W11-10** A lazy
`app.state` getter written four times, and two detached-background-job blocks that share their
whole shape (~45; the cancel-and-await asymmetry is rule 128 and must stay a parameter).
**W11-11** Elapsed
milliseconds computed 11 times, 7 of them inside `run_scan` (~15). **W11-12** `_maintenance_specs`
rebuilt
from six threaded dependencies at four call sites, once per job per reschedule (~40). **W11-13**
Two
`Retry-After` parsers that differ on a negative header (~10; the two *caps* are deliberate and
stay). **W11-14** The cooperative-yield stride spelled two ways at four sites. **W11-15**
Concurrency bounds written per
caller, with `services/fairness.py`'s `_enrich_titles` fanning out up to 80 live Seerr calls unbounded while
`auth/ratelimit.py:200` ships an unused `ConcurrencyGate` (~20, `behavior`: adding the bound is a
fix, so ship it separately).

> **W11-10 splits, and both halves of the disagreement were right about a different half.**
> **The four getters are built at code -6, so the scout's `-7` was very nearly right.** They are
> `api/scan.py:_status`, `api/runs.py:_reap_status`, `api/fairness.py:_request_cache` and
> `api/poster.py`'s inline `artwork_client_lock`; per file the change is `deps.py` +6,
> `fairness.py` -3, `poster.py` -2, `runs.py` -4, `scan.py` -3, counted non-comment and
> non-blank. **A first pass here reported +2 by counting the helper's docstring as code**, which
> is worth recording because it inverted the verdict the row would have carried.
> **The lines are the smaller half of the case.** All four are read-build-store with no
> `await` in the middle, which is
> what stops two concurrent requests installing different objects, and that reason was written
> down at exactly one of the four before this change, `api/poster.py`'s, and silently depended on
> at the other three.
> `deps.state_singleton` is a plain `def`, so it cannot acquire an await without every call site
> turning async first. `reap_in_flight` reads the same attribute and deliberately does not create
> one, so it is not a fifth site and stays as it is.
>
> **The two job blocks are killed: `~45` is unreachable.** `launch_scan`'s `run()` is 47 lines and
> `execute_run`'s `_reap()` is 87, and what they share is 5: `except Exception as exc:`,
> `phase="error"`, `error=str(exc)`, `finally:` and `running = False`. The `log.warning` between
> those two halves differs in event name and fields, so it is not a sixth. Everything else
> differs by kind, one
> looping to consume a queued follow-up scan and the other walking an `AsyncExitStack` over the
> deletion clients and publishing a report. The status models differ too, `stopping` existing only
> on the reap and `followup_queued` only on the scan, so a shared wrapper takes both as parameters
> and holds nothing. The row already conceded the shape by making the cancel-and-await asymmetry a
> parameter (rule 128); that asymmetry is the point, and the second block is the deletion path.

> **Built: W11-15 as the fan-out bound alone, and both halves of its justification were wrong.**
> `ConcurrencyGate` has three production callers, not none: `argon2_gate`, the one instance
> `ratelimit.py:265` builds, is acquired at `api/auth.py:296`, `api/settings.py:949` and
> `services/admin_password.py:85`. The measured block above said four, counting the singleton
> itself, and is corrected in place. It is also a load-shedder: a full gate returns a fast busy
> rather than queuing (rule 11/98), which is the opposite of what a fan-out wants.
> `_TITLE_LOOKUP_CAP = 80` bounds the work and nothing bounded the burst, httpx2's default pool
> of 100 sitting above the cap. `asyncio.Semaphore(_TITLE_LOOKUP_CONCURRENCY)` at 8, the figure
> from `season_scan.RESOLVE_CONCURRENCY`. Its earlier sentence pointing at `leaving_soon.py` is
> superseded: that file's `SHELF_CONCURRENCY` is 4 and bounds Plex sections, and the shape both
> files use is the same. Driven red at a peak of 24 in flight against a bound of 8.
>
> **The bound lengthened the tail, so the deadline ships with it.** A portal that accepts
> connections and never answers costs one read timeout per wave, so one wave of 80 became ten of
> 8 and the enrichment had no deadline of its own. `_TITLE_LOOKUP_DEADLINE_S` is one client read
> timeout, and the rows it cuts off keep the generic label a failed lookup already gives them.
> Found by this branch's own correctness review, fixed here rather than filed. The `~20` lines is
> a **+10 net** (+12 added, -2 removed) for the bound alone and **+31** for the landed file
> (+37/-6), the rest being the deadline and a five-line rule 72 note on `_enrich_accounts`.
> Bounding a fan-out is an addition, and this row was never a duplication to remove.

> **Corrected: W11-12 is -14 lines and five sites, not four, and one of them is `build_scheduler`.**
> `data_dir` is `settings.data_dir` at `_maintenance_specs`, `apply_maintenance_schedule`,
> `run_maintenance_now`, `build_scheduler` and `apply_stored_schedules`, and every one already
> takes the `settings` it came from. All five production call sites were checked: `main.py`'s two
> pass `settings.data_dir`, and `api/settings.py`'s three pass `runtime_settings(request).data_dir`
> beside `settings=runtime_settings(request)`, which is the same object. The row's `~40` measured
> the rebuild cost, which is not what the parameter costs. **One site could receive a divergent
> folder and it was a test**: `test_the_snapshot_sweep_is_handed_the_folder_the_database_is_in`
> passed a folder that was not the engine's, deliberately, to prove the job read the argument
> (rule 141). Removing the parameter retires that question, so the test reads the folder back off
> the engine's own URL and pins that the sweep vacuums the file the engine opened, which is what
> the old assertion took on trust. `refresh_ratings` is the sibling and had nothing pinning its
> wired folder at all (rule 72); it gets the same assertion. Keeping the parameter on
> `build_scheduler` alone was counted off the same diff at -13 and rejected: it leaves the ratings
> download reading `settings.data_dir` while the sweep reads the argument, which is the split this
> row exists to close.

**Frontend state.** **W11-16** Four query keys declared 2 to 5 times as literals with
**divergent** options,
including three different `staleTime`s for `general-settings` and three different refetch
intervals for `scanStatus`, while four other keys already have shared hooks (~40). **W11-17** The
running-to-not-running falling edge hand-written six times (~20). **W11-18** `ServiceModal`'s two
structurally identical map-plus-suggestion state machines, carrying the same `exhaustive-deps`
disable twice (~25). **W11-19** The switch-confirm *caller* written twice, where the component
half was
already extracted (~15). **W11-20** Six navigation callbacks drilled 3 to 4 levels over a
destination type
`navIntent.ts` says is already one value (~40). **W11-21** Three hand-rolled 250 ms debounces.
**W11-22** The
parent-Back-guard ref mirror written three times. **Landed**, as a rule 80 fix and not a dedup:
code net 0, and `ScheduleModal` mirrored one TERM of its `canClose`.

> **W11-19 built at -8, the one figure in this batch the plan had right.** `useSwitchConfirm` in
> `SwitchConfirm.tsx` holds the three rules that have to be true together and none of which is
> local to a caller: the nonce bumps on every refused press, the pending destination clears when
> `dirty` goes false, and a press of the section already open is not a switch. **The duplication
> had already cost once, in writing**: the policy editor found B-31 on its own, that a notice keyed
> to the Discard handler survives a Save and goes on offering a red Discard for changes that no
> longer exist, and Settings' copy then carried a comment *pointing at* that fix rather than
> sharing it. **A fourth caller was hiding in the JSX**, `JobsPanel`'s `onGoToPlex` at
> `Settings.tsx:208`, which the row's "written twice" does not count and which the extraction found
> by breaking it. **The test harness was a third copy** and is rewired onto the hook (rule 119), so
> the three focus tests now drive what ships; three more pin Discard, the clear-on-clean and the
> same-section press. All four mutations driven red, each by exactly one test, and the nonce bump
> was pinned by nothing before this.
>
> **W11-18 built at code -3, not the measured -8 to -12.** The two machines and both
> `exhaustive-deps` disables are exactly as the row says, and `useSuggestedMap` collapses them to
> one of each. **The lines are not the reason and one invariant was untestable in place.** All
> three rules the two copies shared are about what must not happen: a stored pick is never
> overwritten by a suggestion, a stored pick is never tagged "suggested", and picking clears the
> tag even when the value did not change. **The first was unpinned across the whole suite**, and
> the reason is rule 141: `keeps a saved mapping and does not tag it 'suggested'` set the saved
> value and the suggestion both to `TV`, so prefill clobbering a saved pick was invisible. Setting
> them apart was not enough either, since the assertion lands before the effect runs and passes on
> the first render; a second folder the prefill IS allowed to touch is what makes the wait mean
> something. **The one deliberate difference is preserved and now stated**: a blank
> `suggested_library` is no suggestion, where a null `suggested_instance_id` is, so each caller
> normalizes its own rather than the hook deciding for both.

**Frontend components and CSS.** **W11-23** The `.field-sm` triplet typed 21 times across 7 files,
which is
the modal-side sibling of wave 3's `.set-row` finding and the same rule 72 sweep (~40).
**W11-24** `WhyPanelFallback` and `ScalesPanelFallback` as the same 30-line component twice, with
a comment
saying so (~28). **Landed** for the copy divergence the two copies were hiding, not the lines:
the `~28` is code net +2.
**W11-25** Four decision tones written three times each across two CSS files, 12
blocks for
4 declaration sets (~28, `visual`). **W11-26** The `role="progressbar"` wrapper three times, each
carrying a
comment pointing at the other two (~24). **W11-27** The Scales balance bar computed and marked up
twice
verbatim (~14). **W11-28** Two duplicated SVGs where one is already exported. **W11-29** The chip
dismiss button as
three near-copies, where one comment says outright it "borrows" the other's shape (~14).
**W11-30** Pluralization inline **64 times** beside a `format.ts` that already implements it twice,
and 14
sites calling `.toLocaleString()` where `count()` exists (~20).

**Errors and messages.** **W11-31** Four `IntegrationError` sentences raised twice each in
`clients/base.py`
plus a third copy in `public.py`, with the explanatory comment duplicated verbatim (~20; the
`unreachable (…)` wording is hand-constructed in five test sites and is load-bearing). **W11-32**
Two inner
handlers in `refresh_curated_lists` that duplicate the outer catch-all exactly (~12). **Landed at
#698**, with W3b-2's kill, and the measured figure is 14. **W11-33** A
dead
refusal branch in `restore.py:201` whose only content is a sentence its sole caller already
refused 12 lines earlier, plus one prepare-failure sentence written verbatim four times.
**W11-34** The
cron-refusal sentence written twice **in one function**, pinned by nothing. **W11-35** Three
`except` arms
raising the identical 400. **W11-36** Four verbatim copies of one panel-load-failure sentence (the
fifth,
which drops "Reload to try again", is deliberate: #195, a reload inside an editor takes unsaved
edits with it). **W11-37** Three identity entries in `CHECK_COPY` that the fallback already
produces. **W11-38** The
`instanceof ApiError` unwrap ritual five times.

**Data model.** **W11-39** `whitelist.overrides()` and `spare_expiries()` are two full scans of one
table
always issued back to back at four call sites, while a third function in the same file already
selects all three columns in one statement (~15). **W11-40** `ActionStep.run_id` has no index,
SQLite does
not auto-index a foreign key, and `action_step`/`reap_run` are never swept because retention
deletes only snapshots and excludes every snapshot a run points at, so both tables grow for the
life of the install (one additive `create_index` revision). **W11-41** An `IntPk` annotation beside
the
existing `UtcTimestamp` idiom renders byte-identical DDL for 13 models.

> **Killed: W11-39 builds to +5 lines, and only two of its four sites are adjacent.** Built as the
> row asks, with `overrides_and_expiries` doing the three-column read and `spare_expiries` and
> `overrides_effective_at` deriving from it, then measured: `whitelist.py` +14/-7 and `review.py`
> +2/-4, a **net +5**. The extraction is bigger than what it replaces because the loop that splits
> one result set into two maps is ten lines, where each read it collapses is two statements. **"Back to back
> at four call sites" is wrong.** Two are adjacent pairs (`review.py:1336`-`:1337` and `:1397`-`:1398`). `breakdown.py`'s
> pair sits 40 lines apart across the condemned read and `effective_condemned`, and `review.py`'s
> fourth `spare_expiries` read sits about 150 lines below its nearest `overrides()`, so collapsing
> either moves a read rather than removing one.
> One caller is left worse: `review.py:489` reads `spare_expiries` alone, today a filtered
> two-column select and after the change an unfiltered three-column one. The benefit is two fewer
> SELECTs against a table holding one row per manual override, on two page loads. S5, and the
> verifier's warning was right about the risk: `reap_breakdown` is the ledger beside the
> destructive button, and the only version that reaches the row's own figure widens the
> `overrides()` read the executor issues before every item of a live reap (rule 112).

**Build and startup.** **W11-42** The uv bootstrap written three times, the ghcr login and image
name in four
jobs across three workflows, the store-credential probe byte-identical in two workflows plus a
third shape, provenance baked twice in one workflow, and the macOS boot probe written twice inside
one step (~85 lines total, all `ci`). **W11-43** "Install root, else repo root" spelled three
times, with
`main.py`'s SPA mount ("leave a stale second copy of the UI") re-inlining `launcher.py`'s three-parent walk from a different module that happens
to sit at the same depth, so moving either file breaks one of them silently. **W11-44** `preflight → migrate
→ serve` written three times where only `serve` is genuinely per-environment (~23, `behavior`, and
it is a deletion tool's boot path, so rank it last).

> **W11-42: the macOS probe is built at code -8, and the provenance pair is settled as
> deliberate.** The five populations all hold. What was built is the last of them, the boot probe
> written twice inside one step: one `probe()` taking binary, port and log, called for the
> one-folder build and for the `.app`.
>
> **The line the two copies shared was carrying a defect, which is the argument for the merge
> rather than the -8.** `curl -s … | head -c 200 | grep -qi` runs under `set -o pipefail`, so the
> pipeline reports curl's status, and curl dies of SIGPIPE once the page outgrows the pipe buffer.
> Measured: the same probe passes on a 4 KB page and **fails on a 200 KB one**, and today's
> built `index.html` is under 5 KB and moves with every build. So the gate is green on a size
> accident, and a healthy build would
> have gone red on a page that grew. The body lands in a file now. `dev` has three copies of that
> line, two in this step and one in the snap job, and the snap's is fixed here (rule 72); the
> Windows probe uses `Invoke-WebRequest` and has no pipeline.
>
> **The provenance question is settled: both differences are forced, and neither is drift.** The
> `--out` paths differ because the two consumers do: `packaging/pyinstaller/reaper.spec:25` reads
> the file out of `SPECPATH`, and `snap/snapcraft.yaml:100` reads it from the repo root. The
> interpreters differ because the jobs do: the `build` job installs uv and syncs, and runs on
> Windows and macOS; the `snap` job installs no Python toolchain at all, so `python3` is the
> runner's own, and `write_buildinfo.py` imports only the stdlib so neither needs a venv. Both
> steps now say so, so the next reader does not reopen it or fold them together. No gate: each
> mistake fails loudly at build time, on a missing file or a missing `uv`.
>
> **The composite actions are deferred, as the verdict asked**, and the reason is now measured
> rather than assumed: the uv bootstrap is 3 sites, the ghcr login and image name 4 jobs across 3
> workflows, and the store-credential probe 2 byte-identical plus a third shape in `virustotal.yml`
> and a fourth in `submit-winget.yml`'s pwsh. A composite action is a new file per family and a
> `uses:` line per site, so the ghcr family is the only one that could pay, and it cannot be
> measured without building it.

**Per-item verdicts, all 43 that remain. This table supersedes the counts in the sentences above
wherever they disagree.** `right` means the sentence's count and the symbol beside it both hold.
The eleven rows the measured block already argues say `block above` rather than repeating it.
`unsettled` means the scout and its verifier reached different verdicts and neither had a command
behind it; it is a verdict, not a gap left to fill in later.

| ID | Plan says | Measured | Verdict |
| --- | --- | --- | --- |
| W11-1 | 22 sites, 3 modules, every fallback the dataclass default | 17 tests, 20 reads, 3 modules; one fallback is `row.get(_SPINE_LIBRARY)` | kill, the rewrite fabricates a `rating_key` |
| W11-2 | 3 to 5 re-parses, ~12 | count right, size is +2 | defer, carrier for #709 only |
| W11-3 | 6 dispatch places, no test pins the conversions | block above; 30 sites over two spellings | **built**, test and type; table killed, 4 unlike dispatches |
| W11-4 | 12 arms, 6 bare, the test is already that table | block above | kill |
| W11-5 | `scan.py`'s condemned counter | right, in `services/snapshot.py`, and **-3 is exact** | **built**, code -3 |
| W11-6 | triple ×4 plus 3 parallel dicts | 1 triple, 4 pairs, 1 dict | kill, the file has no such shape |
| W11-7 | 2 class names, ~14 | 2 class pairs plus 3 local names, nets -2 | kill, #655 shipped the AST gate instead |
| W11-8 | 4 slug sets, 4 sweeps, ~10 | right, and `_retire` is already the helper | kill, nets 0 |
| W11-9 | 2 identical loops, ~10 | 2 loops, opposite polarity, different returns | kill, indirection on the canary check |
| W11-10 | getter ×4 plus 2 job blocks, ~45 | 4 getters exact and **code -6**, so the scout's -7 was near right; the job blocks share 5 lines of 47 and 87 | **split**: getters **built**, job blocks **killed** |
| W11-11 | 11 times, 7 in `run_scan` | 12 times, the 7 are in `_run_scan_locked` | kill, a helper costs +7 |
| W11-12 | 4 call sites, ~40 | 5 sites, -14; `~40` sized the rebuild, not the parameter | **built**, -10 net |
| W11-13 | 2 parsers differ on a negative header | right | kill, block above |
| W11-14 | 2 spellings, 4 sites | right | kill, two different numbers on purpose |
| W11-15 | 80 unbounded, `ratelimit.py:200` an unused gate | block above; the gate has 3 callers and sheds load | **built**, +31 (bound, deadline) |
| W11-16 | 4 keys, 4 existing shared hooks, ~40 | block above | build, 2 keys |
| W11-17 | hand-written 6 times, ~20 | 6 sites, 4 to 5 hand-written, the rest already hooks | kill, no two of the residue do the same work |
| W11-18 | 2 machines, deps-disable twice, ~25 | 2 machines and 2 disables right; **code -3**, not -8 to -12 | **built**, for the prefill rule no test could see |
| W11-19 | caller written twice, ~15 | right, and **-8 holds**, the one figure of these four that did | **built** |
| W11-20 | 6 callbacks, depth 3 to 4, ~40 | 7 or 8 props, max depth 2 | unsettled, scout kills it, verifier makes it an owner call |
| W11-21 | 3 hand-rolled 250 ms debounces | right, one is already a hook | unsettled, scout kills on ~0 lines, verifier builds the shared timer |
| W11-22 | mirror written 3 times | right, plus 3 child effects and a 4th copy of the guard in `ServiceModal` | **built**, code net 0; the value is `ScheduleModal`'s one-term mirror |
| W11-23 | 21 sites across 7 files, ~40 | block above | kill |
| W11-24 | same component twice, ~28 | right on the copies; **-23 is wrong, code net +2** | **built**, for the copy divergence, not the lines |
| W11-25 | 12 blocks, 4 sets, ~28 | right, the one count that survived re-derivation | build, cascade test scoped to `.strip-sq` |
| W11-26 | 3 wrappers, each commented at the other two | 3 of 6 sites are that shape, 2 of the 3 carry a comment, and both point at `ScanLine` | build, 3 of 6, `ReapBar` excluded |
| W11-27 | computed and marked up twice, ~14 | right, nets -6 | build, the shared `aria-label` is the reason |
| W11-28 | 2 SVGs, one already exported | right, 2 of 6 duplicated paths, -10 | build, ride along |
| W11-29 | 3 near-copies, one comment admits it | 5 controls, and the "borrows" comments are in the CSS | unsettled, both reports make it an owner split |
| W11-30 | inline 64 times, `format.ts` twice, 14 `toLocaleString` | block above | kill the helper |
| W11-31 | 4 sentences ×2, a third copy in `public.py`, comment verbatim | 4 right, the scout's 5 was the over-count; two carry the `public.py` copy, and the comment is a paraphrase | unsettled, scout -6, verifier ~0 |
| W11-33 | dead branch at `restore.py:201`, refused 12 lines earlier, sentence ×4 | branch at 187, the gap is 11 lines; the ×4 half closed at #720 | **built**, code -5, the branch alone |
| W11-34 | twice in one function, pinned by nothing | right; **-3 code, not -5** | **built**, code -3, and the pin is the value |
| W11-35 | 3 arms, one 400 | right, all three identical and **-3 is exact**; `PlexError` carries two causes | **built**, code -3 |
| W11-36 | 4 copies plus a deliberate 5th | right, plus an unnamed pair in `BackupPanel` and `AboutPanel` | kill as a dedup, write the hygiene gate instead |
| W11-37 | 3 identity entries | right | build, -3, never its own PR |
| W11-38 | the ritual 5 times | 9 sites in 2 rituals, 5 status and 4 message | kill, a helper is a rename |
| W11-39 | 4 call sites, ~15 | 4 sites, only 2 adjacent; built at **+5** | **killed**, S5 |
| W11-40 | unindexed, never swept, grows for the life of the install | 3 tree claims right, framing wrong, block above | **built**, the index only, +4 code and an 11-line revision |
| W11-41 | 13 models | 15 models | kill, byte-identical DDL is why nothing is gained |
| W11-42 | ~85 lines, all `ci` | populations right; the probe is **-8 code**, and the **provenance pair is settled**, both differences forced | **built** the probe, composite actions deferred |
| W11-43 | spelled 3 times | right, and `schema_gate.py` is a 4th deliberately different shape | **built**, **code -2**, so the +2 never had to be traded; no gate, one site would be left to scan |
| W11-44 | written 3 times, ~23 | count right, 3 runtimes rather than 3 copies | kill, sharing costs +10 on the boot path |

> **Built: the six backend rows W11-5, W11-33, W11-34, W11-35, W11-40 and W11-43. Every symbol
> the six name is where the table says, and the stated savings hold up better than the first
> count of them said.** Code net, non-comment and non-blank, the basis every other row in this
> table uses: **-3, -5, -3, -3, +4 with an 11-line revision, and -2.** Stated: -3, -7, -5, -3,
> index-only and +2. Two are exact (W11-5, W11-35), two are short of the estimate in the same
> direction (W11-33, W11-34), and **W11-43 beats its estimate outright**, at -2 against a
> stated +2.
>
> **This block first published those figures as -2, -2, +2, -1 and +7, which is the same total
> lines counted a different way, and it inverted the conclusion.** On that basis five of six read
> as costing more than stated and the row wrote up a one-way bias that is not there. #733 had
> already recorded the identical slip one PR earlier, a getters row read at +2 by counting a
> helper's docstring as code. **So the basis is the finding, not the arithmetic**: a total-line
> figure is not comparable to a code-net one, and this table is code-net throughout. The raw diff
> across the six files is +50, almost all of it the docstrings and the revision.
>
> **W11-33's second half was already built.** Its "prepare-failure sentence written verbatim four
> times" is `restore._PREPARE_FAILED`, landed at #720 with the four raise sites and four
> assertions on it. Only the dead branch was left, and it is at `_check_schema`'s `if revision is
> None`: `_summarize` refuses `None` eleven lines before it calls in, and needs the revision for
> its manifest cross-check, so it cannot stop asking. The parameter is `str` now, which puts the
> refusal in mypy's hands rather than in a second runtime copy of one operator sentence. That
> sentence had nothing asserting it either; the surviving copy is pinned now.
>
> **W11-34 is -3 code, and the pin is what it was worth.** Two arms of one function spelled one
> refusal; `_BAD_CRON` is the declaration. The row's own words say the value: *pinned by nothing*.
> Two tests drove a bad cron down both arms and asserted only the 422.
>
> **The first version of that pin was fail-open in two directions, and its own review lane caught
> one while the branch caught the other.** Matching only the declaration's halves against the
> rendered detail cannot see a re-inlined copy, because the copy it replaced rendered identically,
> which is the rule 144 gap the test exists to close. It also cannot see an arm raising `_BAD_CRON`
> unformatted: the raw template starts and ends with the very halves being compared and its
> literal `{reason}` satisfies a length bound, so the placeholder ships to the operator (rule 21).
> Both are fixed at #740, one assertion each, driven red separately. Every expectation is still
> derived from the declaration, so nothing restates the sentence.
>
> **W11-35's three arms are byte-identical and the collapse changes nothing, but one of them
> carries two causes.** `PlexError` reaches that arm both from the service, which raises it for
> "no linked Plex server" (a 400, and a test pins it), and from the client, for a server that is
> linked and unreachable. `api/plex.py:599` answers the second kind 502 and wraps the client's
> words. Telling the two apart needs a second exception type in the service, which is a behavior
> change this dedup is not; filed as a question instead.
>
> **W11-40 is the index and nothing else.** `EXPLAIN QUERY PLAN` for the executor's own filter
> returned `SCAN action_step` before and `SEARCH ... USING INDEX ix_action_step_run_id` after,
> asserted on the plan rather than on the index existing, so an index that is present and not
> chosen still fails. The revision is `f7a8b9c0d1e2`, one `create_index`: plain DDL that builds a
> b-tree and rebuilds nothing. `services/retention.py`'s exclusion is untouched, per the block
> above. **Its docstring named the wrong second caller** and said `render_as_batch` was what made
> the index free, neither true; both corrected at #740, along with the column docstring and the
> test docstring, which had each restated the revision's reasoning in full (rule 144).
>
> **W11-43 costs -2 code, so the trade the row offered never had to be made.** After the
> consolidation
> `src/` holds exactly one multi-parent walk, inside `buildinfo.project_root`, so a ban on the
> spelling would guard a population of one. A gate would also have to run *after* this change or
> exempt the two sites it exists to catch, which is the exclusion-list shape. The drift it removes
> is real and silent: `main.py` counted three parents from its own file to reach the directory it
> serves the SPA out of, matching `launcher.py`'s count by coincidence of depth, so moving either
> file serves an operator a stale UI rather than failing an import.

### The kills, sorted by whether a different shape rescues them

**The table above carried 16 kills at the measurement.** Three more are `unsettled` rows where
one of the two reports killed the item (**W11-10**, **W11-20**, **W11-21**). **W11-31**'s two
reports disagreed on a figure rather than on a verdict. 43 rows as measured: 21 build, 16 kill,
5 unsettled, 1 defer. **The cells move as the wave executes and this paragraph does
not**, so re-derive rather than quote it: **W11-39** has since been built, measured at +5 and
killed, which is the seventeenth kill. The 16 sorted below
are the measurement's, and W11-39 is not among them.

The sort below is the answer to a challenge the owner made on 2026-08-10, and S5 now carries the
general form of it. **The question is not "does the proposed shape work" but "what shape would".**
A kill that says only "nets to zero" has answered the first question and left the second one
open, and four verdict cells here answer only the first (W11-8, W11-11, W11-23, W11-44).
Three of those four carry a premise correction in the measured column beside them, which is the
reason each is sorted below on its substance rather than on the cell.

**Premise false, and no shape rescues them.** The claimed duplication or cost is not in the tree,
or the change breaks something these rows exist to protect. Each is correct as written.

- **W11-4** The `others_watching` arm cannot fire, and the four cross-references
  (`api/review.py`, `engine/gates.py`, `WhyPanel.tsx`, `policyMeta.ts`) each say it is kept so
  stored explanations still decode, so it is deliberate rather than dead. No table either:
  `min_dormancy` and `season_progression` return computed phrases, and the arm ORDER is the rule.
- **W11-6** `services/breakdown.py` holds one count/bytes/unknown triple, four pairs of differing
  arity and one dict. The three parallel re-zipped dicts do not exist.
- **W11-7** Already superseded by the shape this section argues for.
  `test_repo_hygiene.py`'s `_client_construction_sites` walk pins all six *arr constructions
  and their argument set. `test_every_arr_client_is_built_with_the_same_arguments` says in its
  docstring that a shared constructor "would only bind the sites that call it". That is right
  here. It was read into W3b-9's kill as a reason to prefer the gate OVER the helper, and the
  two are not alternatives.
- **W11-8** `_retire` is already the helper, and rule 115's four conditions live inside it. The
  four call lines are four different `when=` predicates over four different slug sets, and
  moving predicates into a table would detach three safety comments from the case each explains.
  `safety-path`, so the prime directive settles it whatever the arithmetic says.
- **W11-13** Correct kill for a reason the row does not give. `IntegrationError.retry_after`'s
  sole reader treats `0.0` and `None` alike, which is what the row says, but `notify/discord.py`
  has a second reader of its OWN parser that does not: `None` skips the retry and `0.0` would
  retry immediately. Unifying on the base clamp is a live behavior change, so the kill is firmer
  than recorded.
- **W11-14** Two numbers on purpose: 500 is a debounced simulator replay, 100 is the stride the
  scan's own `emit(Progress(...))` uses, so it is progress granularity rather than a yield
  budget. A shared constant would fuse two unrelated knobs. The only residue is naming
  `snapshot.py`'s bare `100`, two sites and one line.
- **W11-20** Max drill depth is 2, at two places, and `navIntent.ts` already made the destination
  one value with one `goTo`. Threading one untyped capability into the leaves in place of
  the seven or eight typed props is worse.
- **W11-41 is the row every other kill should be written like.** Byte-identical DDL means there
  is no invariant that can drift, so an `IntPk` alias would be a rename. It names the drift
  surface as absent instead of counting lines.

**Killed on line count, and worth re-asking.** The duplication is real; the extraction measured
to roughly zero; nobody asked what else would write the rule once. Each bullet below names the shape that would.

- **W11-1** One pure helper returning the seven display values off an optional `PlexItem`,
  called from `library_index.py`, `snapshot.py` and `season_scan.py`. The kill is right that a
  sentinel `PlexItem` must invent a `rating_key` (`identity.py`'s field has no default) and that
  one fallback is the Tautulli spine rather than the dataclass default; neither objection touches
  a helper that returns values instead of an object.
- **W11-9** No shape at an acceptable price. The no-trailing-sleep guard really is byte-identical
  at both loops, but `_exclusion_landed` gates the canary abort, and the prime directive does not allow indirection there for tidiness. Recorded so nobody re-asks.
- **W11-11** Not a helper, a gate: assert under `src/` that no duration is derived from
  `time.time()`. The rule at the 12 sites is "monotonic, rounded to integer milliseconds", and
  nothing in the tree states it.
- **W11-17** `useEdge(flag, {onFall})` holding the callback in a ref. The six bodies genuinely
  differ, but the shared thing was never the body: it is the ref protocol, and three sites carry
  the same prose about it. The bug is writing the mirror inside the `if` instead of after it, and the hook makes that
  unwritable.
- **W11-23** A text gate, not a component. `.field-sm` is a `<label>` wrapping exactly one
  control with `span.field-label` as its name, and a `<div>` exactly where there is no single
  control. That is an accessibility rule holding across 26 sites and declared nowhere, which is
  the reason this row's -7 lines decided nothing.
- **W11-30** Two named sentence helpers in `format.ts`, not a `plural()`. The kill of the helper
  is right, irregulars and agreement included. What the kill took with it is two operator sentences written verbatim more than once: "N size(s) unknown" at three sites including `format.ts`'s
  own, and the held-back-unmeasured sentence at two. Rule 144's exact case, on deletion copy.
- **W11-36** Three lines inside the gate that already exists.
  `test_the_reload_advice_population_is_pinned_per_file` counts the WORD `reload` per file; it
  cannot see the sentence drift. Pin the distinct never-loaded sentences across `_shipped_tsx()`
  to the two known strings, which also brings the unnamed `AboutPanel`/`BackupPanel` pair in.
- **W11-38** Split it. A status helper would be the rename the kill describes, so skip that
  half. A message helper is not a rename. It would hold "an error that is not an `ApiError` never
  shows the operator its raw text", which four call sites can leak past today, and `api.ts`
  already explains why a `TypeError` falls past every branch.
- **W11-44** A gate over the three boot paths, not shared code. They differ in process model,
  alembic addressing and failure handling, so nothing can be shared; the invariant is that none
  of the three serves a schema it did not just bring to head, and `launcher.py` is the only place
  that says so. Same answer W11-43's row already reached from the other side.

**Ranked, best first, and none of it is built here.** The order is how much a future author has
to keep in step, not how many lines come out. **W11-36**, three lines over two sentences at six sites. **W11-23**, an accessibility rule with 26 instances and zero declarations.
**W11-30's two sentences**, deletion-path copy written twice verbatim. **W11-1**, a degradation
rule at 20 sites where the wrong answer is a fabricated value. **W11-17**, where the residue is a
protocol rather than a body. **W11-38's message half**. **W11-10's four getters** from the
unsettled rows, which own the no-`await`-between-read-and-write rule that only
`api/poster.py` states among the four. Then **W11-11** and **W11-44** as gates, and **W11-21**'s shared 250 ms
constant, which is one line and closes a number three files hard-code.

## Wave 12: the test suite's wall clock

Wave 1.3 found one 44-second win. This lane measured the rest: **214.69s for `uv run pytest` with
`test_repo_hygiene.py` excluded**, 3,971 tests, and 27.11s wall for the frontend. Two findings are
most of it, both measured rather than estimated.

- **The at-rest scrypt KDF runs at production cost on every app boot: 60 to 75 seconds, about 30% of
  the suite.** `conftest.py:52` already cheapens Argon2 for passwords and stops there. `crypto.py:52`
  sets `_SCRYPT_N = 2**16` (64 MiB) and every `create_app` lifespan derives one, so
  `_hashlib.scrypt` is **123ms of the 164ms** each per-test app boot costs, and 495 tests take a
  `client` fixture. The fix is a `conftest` wrapper on `_derive_fernet_key` mapping each distinct `n`
  to a distinct small one. **Not** a patch of the `_SCRYPT_N` constant, which would break
  `test_kdf_and_session_upkeep.py:41`'s `max(_SUPERSEDED_SCRYPT_N) < _SCRYPT_N` and the five
  hardcoded legacy boxes beside it. Measured across 22 files and 1,797 tests: 116.9s to 64.1s, with
  193 tests in the KDF-sensitive files passing unchanged. **+10 lines.**

> **Confirmed and understated, with one blocker.** Measured end to end: **206.90s to 114.18s, a
> 44.8% cut**, all 3,971 passing. Per-derivation scrypt is 123.74ms; app boot drops from a 137.7ms
> median to 17.1ms. It cannot leak into production: `tests/` is outside the wheel, there is no
> root `conftest.py`, and nothing in `src/` imports it. Put the wrapper in `pytest_configure`
> rather than at module level, so `import tests.conftest` cannot apply it either.
>
> **Blocker: the KDF suite cannot detect a non-injective mapping, so the wrapper must ship with a
> test that can.** Running a deliberately collapsing wrapper (every `n` to one small `n`) leaves
> **30 tests passing** while `box._superseded` is never built — four of the six compatibility
> tests go vacuous, because `_legacy_box(...)` at `test_kdf_and_session_upkeep.py:127` runs after
> the patch and before the decrypt, so `assert built` at `:129` is already satisfied by the
> fixture. Assert `_derive_fernet_key(k, salt) != _derive_fernet_key(k, salt, 2**14)` and reset
> `built` after `:127`. Rule 145: the wrapper is what makes that guard load-bearing.
- **One test really dials the network for 15.04 seconds**, 7% of the suite.
  `test_settings_api.py:728` stubs `test_connection` but not `probe_root_folders`, so the route takes
  `settings.py:550`'s branch and eats a connect timeout to an unroutable host. The failure is
  swallowed into `map_error`, so it passes. Its four siblings at `:1510`, `:1538`, `:1561` and
  `:1594` already carry the missing stub and keep the same behavior pinned. **+3 lines, measured
  15.04s to 0.01s.**

> **Confirmed at 15.04s, with two corrections.** Three siblings carry the stub, not four: `:1594`
> is a Seerr test that never reaches the `probe_root_folders` branch. And the sharper argument is
> not the seconds. Both assertions hold identically with the stub, so the test is not asserting the
> wrong thing; it is resting on `a.local` failing to resolve, which is rule 119's environmental
> accident. On a LAN with an mDNS host named `a` it takes a different path, and on a Linux CI
> runner the 15 seconds mostly are not there — so this is a correctness fix that happens to be
> fast, and wave 12's payback is a local figure.
- **`test_openapi_tags.py:38`'s `schema` fixture is function-scoped for 11 read-only consumers**, each
  booting the app, logging in and re-fetching the document: 3.80s of setup against 0.37s of calls.
  Session scope saves 3.5s today and ~0.4s once the KDF item lands.
- **12 frontend test files never touch the DOM and pay jsdom for it.** A `@vitest-environment node`
  docblock per file, which needs `src/test/setup.ts` to guard its three DOM writes. ~4.8s of CPU,
  1 to 2s of the 27.11s wall under parallelism.

With wave 1.3's `lru_cache`, the two waves together take the Python suite from about 268s to about
150s.

**Do not module-scope the other `client` fixtures.** The lane checked the five read-only candidates
and recommends against: once the KDF fix lands the boot drops from 164ms to about 40ms, so scoping
all five buys **1.5 seconds** in exchange for state bleed across a suite whose `_hermetic` fixture
resets four process-global throttles per test.

> **Confirmed, and it is a hard blocker rather than a preference.** `_hermetic` is function-scoped
> and takes function-scoped `monkeypatch`, so a module-scoped `client` boots *before* it: the app
> reads the developer's real `.env` and starts the IMDb download. `test_openapi_tags.py`'s fixture
> is the exception and is safe — 10 consumers, not 11, all strictly read-only.
>
> **`_hermetic`'s own docstring was a rule 7/24 violation, and it is fixed.** It claimed "no
> network" while blocking no sockets, with the 15-second test as the proof. The socket guard
> landed as `conftest.py`'s autouse `_no_network`, and `_hermetic`'s docstring now says the
> network is that guard's job and never was its own, citing rule 7/24 by name.

**And do not go looking for parametrize candidates.** The lane AST-normalized all 3,164 test bodies
twice, once on constants and once on constants plus variable renaming. Bodies of 5 lines or more
yield **two** clusters of four. Parametrizing every candidate in the suite saves under 80 lines and
zero seconds. Also measured clean, so nobody re-checks: no vacuous assertions (all 29 loop-only tests
iterate something a named sibling pins non-empty), 12 mock-internal assertions in the whole Python
suite, 4 skips all genuine platform guards, and 1.22s of collection for 4,041 tests. One piece of
dead config: `pyproject.toml`'s `-m 'not live'` filters nothing, because no `live`-marked test exists.

## Do not touch, extended

The register above holds. This pass adds:

- **`executor.py`'s four Protocols** (`MovieDeleter`, `SeasonPruner`, `PlexOps`, `HistorySource`)
  each have one implementation and are **not** a testing seam: they narrow what a delete can reach
  to a written-down surface. Same for `armed_recheck`/`stop_recheck`, injected once, reason
  recorded.
- **`policy.py`'s `PolicyBody.keep_configs` identity repacks** (`GradedKeepSpec` → `KeepConfig`) look like wave 5's
  shape and are the opposite: the layer boundary is the point, because `score()` must not import
  the policy layer.
- **The raw-dict explanation readers beside the typed `read_explanation`.** A deliberate split,
  and the reason is the model's strictness: the extractors must degrade where the model must fail.
- **`plex.py`'s two locks**, whose scope comment forecloses the merge and names why the obvious
  fix looks right and is not. **`library_index`'s Tautulli spine against the plexapi sweep**,
  which is not a cache overlap: the drop is what keeps a phantom out of the index. **The three
  update-check TTLs**, each reasoned (#464).
- **`clients/plex.py:49 → engine.identity`**, the one `clients → engine` edge. `identity` is pure
  and downstream of nothing; the alternative is duplicating GUID parsing.
- **Five never-caught exception classes**, each pinned by `pytest.raises(<Class>)`, so the class
  identity is the assertion. **The five `list_config` sentences duplicated across languages**,
  bound both ways by a test whose failure message names the other file by path: that is 144's
  answer working.
- **The five unused baseline indexes**, because `drop_index` is not the additive shape the
  repository requires. The actionable part is negative: do not copy `index=True` onto the next
  hash column.
- **The twelve `Candidate` columns that never appear in a SQL expression.** One construction site,
  and retention's own measurement puts two thirds of the row in `facts_json` plus
  `explanation_json`.
- **The three migrations that hand-write the same policy-conversion body.** `20260730_1200`'s
  docstring states the doctrine: a migration must mean the same thing forever, so a shared helper
  under `versions/` is the same hazard with a nicer name. Recorded so nobody re-derives the idea.
- **Frontend, checked clean so nobody re-audits it**: no prop-forwarding-only components, no
  barrel files, one modal implementation with one documented exception, two `aria-live` regions
  both in `announce.tsx`, one `!important`, 13 load-bearing vendor prefixes, and 113 selectors at
  3+ classes with none built to beat an earlier rule.

## What this pass could not settle

Three items are stated as questions, with no `Reviewed/` claim behind them:

1. **`_kept_season_phrase` (`api/review.py`) recovers a discriminant by prefix-matching the
   producer's own sentence.** Rule 92/142's shape, and its docstring records that a reword already
   stranded three of these on older snapshots. Could the frozen explanation carry a reason id,
   leaving the prefix tests as the legacy-row fallback? The stored-schema cost was not measured.
2. **`executor.py:2748`'s `expected <= 0` branch** appears unreachable behind the caller's guard
   at `:2698`, the same caller/callee doubling as the `restore.py` finding. No path to it was
   demonstrated, so it is not asserted dead.
3. **The `aria-describedby` error-id idiom** recurs at 8 sites. Whether one `useFieldError(owner)`
   hook serves all eight, or the owner-matching predicates differ enough that sharing is worse
   than the repetition, was not settled.

## Defects this pass filed

Five findings are defects rather than simplifications, so they left the plan and went to the
tracker: **#555** (the Leaving Soon summary contradiction, wave 10.1), **#556** (`grace.py`'s
unchunked `IN` over the whole condemned set, the one site rule 94 actually bites), **#557** (the
scan and reap tasks have no failure callback, rule 102), **#558** (four `.env.example` keys a
`.env.local` cannot deliver, plus the port the recovery link can get wrong), and **#559** (the
Tautulli walk with no page backstop, filed as a question because nobody demonstrated a server that
triggers it). **#559 was re-headlined on evidence and fixed**: the unbounded spin needs a Tautulli
bug and stayed a question, but the same walk ending on a short page is ordinary API behavior, and
against the real `build_index` it read part of a library as the whole of it.

One lane finding was **wrong and is recorded here so it is not re-raised**: `DiscordNotifier.post`
was reported to leak an `httpx` client per notification. It does not; `discord.py:93`'s `async with`
closes the one it opened.

## Sequencing

**Superseded by [Execution](#execution).** Both passes proposed an order and they disagreed: the
first preferred wave 1 first, the second wave 12. The phase table reconciles them and adds the
cross-wave file collisions neither pass could see, because each read one axis. Wave 5's
explanation item still wants its pinning test written before the change, not with it.
