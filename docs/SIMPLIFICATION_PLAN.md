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
`tests/test_repo_hygiene.py:2272` checks, and the same commit corrects `docs/README.md:117` and
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
| 5 | Deletions | **done** | 4 of 4 | W1.1-l killed: `tautulli.metadata` has a caller in `scripts/`. Release M's review found a keep collection silently unprotected since the first Pace save. Tier B moved by one line, the recorded alembic head; every decision identical |
| 6 | Structural motion | **done** | 6 of 8, 2 dropped | The by-design ceiling. Exit task finished: every `path:NNN` in this document resolves against the tree |
| 7 | Wire contract | not started | 0 of ~5 | C7 outstanding. W7-5's `window_days` arrives from phase 5, its third-pass kill spent |
| 8 | Dedup and carriers | not started | 0 of ~25 | |
| 9 | Declaration tax | not started | 0 of 2 | C10 outstanding |

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
| C3 | **counts settled by audit, owner read outstanding** | Three independent auditors re-derived every count without being shown it first, and **every one held exactly** — including the socket guard's 7 violations, re-measured in a detached worktree. **What did not hold is the matcher beside each count**, which is the half rule 145 says a count cannot cover: the layering walk could not see `from reaper import services` (#588), the path-filter gate pinned a number where the prose names files (#591), and the network guard hooked 3 of 10 exits (#592). All three fixed and driven. The lesson for later phases: re-deriving a count is cheap and confirmed it; what the count could not answer is whether the *walk* sees the tree, and that needed a second party. Below is what each gate now covers. **W6-5**: 78 modules under the four packages *as audited*, 6 directed pairs, 3 deferred imports (the module figure is 79 now: #599 deleted two and phase 6 added `api/plex.py`, `engine/policy_migrations.py` and `engine/policy_warnings.py`); the reconciliation is that all 14 `engine/` modules and 8 of 9 `clients/` produce no cross-package edge at all. **W6-8**: 2,023 socketpairs across 1,674 tests allowed, 9 `getaddrinfo` calls seen, 7 of them real violations; the allowlist is 7 hosts and every one is driven. **W6-6**: 3 path filters across 9 workflows, counted twice by two different matchers. W1.5-c landed no gate, so nothing there to check |
| C12 | **settled** | Owner, 2026-08-08: **the boot log keeps the added lines.** The one cost put to them was ~2 lines per restart saying a job runs on its built-in default, and they took it — a boot that states every job's schedule out loud is worth more than a quiet one, which is the same argument `main.py`'s per-job "next firing" table already rests on. Nothing to change; #594 ships as merged. The evidence behind the read follows. Two questions, both measured rather than argued. **What startup now applies: the same job table, byte for byte.** The same stored config booted on the phase-3 tip and on the phase-4 tip gives six jobs with identical ids and triggers; the boot log differs by exactly three lines, and the `scheduler.*` event diff is the whole behavioral surface of #594 — an orphaned stored row's warning renamed from `bad_maintenance_cron` to `unknown_maintenance_job` and moved earlier, plus one `maintenance_scheduled` line each for the two jobs still on their built-in defaults, which the replay now re-applies from the same constant `build_scheduler` used. The interval sweeps are outside `MAINTENANCE_JOB_IDS`, so `sweep_old_snapshots` keeps its start delay and jitter. **The deliberate re-freeze is a no-op, and that is the finding.** Tier A: 114 replay tests pass and `tests/_policy_lab.py` is untouched. Tier B: re-captured against snapshot 86 and **byte-for-byte identical** to the committed file — 5,965 items, protect 4,261 / condemn 543 / abstain 1,161, same plan and manifest hash. The phase text expected corrected behavior to move the baseline; the corrections are why it did not, since all four items proved latent or off the decision surface entirely |
| C6 | **settled** | Owner, 2026-08-08: **five modules, and *Vocabulary* gets its own.** `api/review.py` (1,315, the *Snapshots and candidates* banner), `api/policy.py` (~485), `api/simulate.py` (~841), `api/vocabulary.py` (85), `api/about.py` (47), off a ~150-line shared preamble. So the *Policy* banner is cut at `:1853` (`_SIM_YIELD_EVERY`), a seam the file does not draw, and the 85-line *Vocabulary* banner stands alone rather than riding under the POLICY tag it shares with the editor. **Two cross-module edges are created and were measured before the read, not after**: `_to_body` is called by both the policy routes and `simulate`, so `simulate.py` imports `policy.py`; and `_replayed_evidence` is defined inside the simulate block yet called from `_deep_links` (`api/review.py`, "the replay can never disagree"), which is *review*, so `review.py` imports `simulate.py`. Neither is a design problem at two import lines, but the wave's "pure motion" framing does not predict them, and phase 8 plans against this graph. One route is filed under a banner its tag disagrees with: `season_shape` (`:208`) is POLICY-tagged and sits in *Snapshots and candidates*. It moves by banner, to `review.py`, because `test_openapi_tags.py` keys on method and path and the served tag is unchanged either way |
| C6, corrected on landing | **the edge count was low, and the two it found were a cycle** | Owner, 2026-08-09. C6 measured two cross-module edges and got the direction of both right; there are **four**, and the two it missed are what make its own pair a loop. `simulate` reads `_decode_explanation` and `_entries` from *review*, while review reads `_replayed_evidence` from *simulate* — `from x import y` both ways does not load, so the five-module cut as settled would not have booted. Settled by moving `_replayed_evidence` into `review.py`, which inverts the one edge C6 reasoned about explicitly. It is not only a cycle break: all three are readers of a stored explanation or its evidence, its two siblings were already in review, and the panel is their first reader. The graph is now `simulate → review`, `simulate → policy`, `policy → review`, `vocabulary → review`, and acyclic. **The lesson is C3's again at a different target**: re-deriving the count was not what a second party added — measuring *two* of something is no evidence about how many there are, and a partial edge measurement cannot see a cycle by construction, because a cycle is a property of the set |
| C4, C5, C7 to C11, C14 | not started | — |

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

**S6. `docs/STATUS.md` is full.** 120 of 120 lines, enforced at `tests/test_repo_hygiene.py:46`
alongside the 100-column bound at `:54`. Phases 4, 7, 8 and 9 all alter what the app does, which
CLAUDE.md's golden rule says updates STATUS in the same commit, so **every such PR removes a line
to add one**. A new dagger costs more: a `docs/DECISIONS.md` section *and* a bump to the
hand-reconciled `DECISION_SECTIONS` at `:59`, which is checked both ways. W1.1-a's correction is
the case that will hit this first.

**S7. Hand-reconciled counters move with the populations they count.** S1 names two
(`EXPECTED_INTERFACES`, `EXPECTED_PAIRS`); `DECISION_SECTIONS` is a third, and every gate phase 3
lands under rule 145 adds another. Phase 6 splits two routers and phase 8 creates `api/deps.py` and
moves `LAUNCHER_CONF_NAME` — both move populations that phase 3's gates count. Grep for the counter
before closing a PR that adds or removes a member. **The phase-3 counters, by name:**
`_EXPECTED_LAYERED_MODULES` (**83** modules under the four packages), the logger counter in
`tests/test_capturable_loggers.py` (**50**), and `_DEFERRED_CROSS_PACKAGE_IMPORTS` (the three
sites W9 deletes, one of which moved file in #612). The module figure has moved five times:
#599's deletion took it to 76 without this paragraph noticing, phase 6's `api/plex.py` took it to
77, its `policy_migrations` / `policy_warnings` pair to 79, and `routes.py` becoming five modules
to 83. Each gate's failure message now names its prose siblings, since nothing asserts them.

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
derived, and the missing throttle tests for the three gates that lack them.

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
function-local imports, and W9 deletes three such workarounds. Pin those three sites by name so the
gate is not blind to the change it would most want to police.

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
   > revision; a later release drops the seven columns in one sweep, under rule 148's three
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
the two `range(0, len(keys), _KEY_CHUNK)` loops in `_group_rollups`, `api/review.py`), so a bare symbol silently merges two sites into one and
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
`GeneralPanel`'s six fields cleanly and three with escape hatches. W4.2's generator runs in-process
off `create_app(settings).openapi()`; an HTTP fetch needs a booted, authenticated server.

**W4.2 last is a risk call, not a cost one.** It deletes 1,239 hand-written lines and both mirror
counters, so phase 7's edits to those lines are thrown away either way. It goes last because a
generator lands against the smallest, most settled schema surface, and because W4.3's `Literal`
types (phase 7) must precede it.

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
> `docs/DECISIONS.md` section, bumping `DECISION_SECTIONS` at `tests/test_repo_hygiene.py:58`
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

`tests/test_repo_hygiene.py:901`'s `_repo_text_files()` does a full `rglob` plus `read_text` of
every file in the repository and is called from **7** sites; `_uvicorn_launches()` re-enters it.
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

`test_no_bare_exception_assertions_in_tests` (`test_repo_hygiene.py:442`) greps for
`pytest.raises(Exception)`; **ruff B017 is enabled, runs in CI, and is strictly broader**.
`test_instruction_files_exist:278` filters a list built from a glob for absent files, which a glob
cannot return. `test_the_select_name_matcher_rejects_what_it_claims_to_reject`'s case at `:3116`
can only fail after the test above it. `test_the_tagline_sites_all_exist:2239` reads the tuple
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
- `services/executor.py:2101,2328` — the size interlock written twice, and the season copy has
  grown an empty-list guard the movie copy has no analogue for. Extract the growth branch only;
  the unreadable-size branch's copy genuinely differs per path and rule 21 wants that. Risk
  `safety-path`, 8 pinning tests.
- `WhyPanel.tsx:1316` vs `ShowPanel.tsx:69` — the same panel head, except one carries a `↗` glyph
  and the other does not. The divergence should be **decided**, not inherited.
- `clients/seerr.py:299-393` — the paging contract written three times; its own test is titled
  "Rule 72: the same loop, twenty lines down". `plex.py`'s `_iter_pages` is the complete-or-raise
  helper rule 56/89 names; Seerr never got one.
- `services/scan_runner.py`'s `build_sources` (three `verify=r.verify_tls` arms) and
  `build_reap_gateway` (three `verify=row.verify_tls`, which is why grepping the scan spelling
  misses it) + `services/instances.py:582` — per-kind client construction
  in three places, and `instances.py:597` already records the drift incident (`api_path_prefix`
  reached the scan but not Test Connection).

**Largest by volume:**

- `clients/plex.py` — **21 methods repeat the same off-thread plus error-map wrapper**
  (24 `to_thread` sites, 19 identical `except` arms). One `_call(fn, *, what, lock)` helper, with
  three documented opt-outs that must stay bespoke. **~100 lines**, risk `safety-path`, 32 pinning
  assertions.
- `services/scheduler.py` — **7 copies** of "run the job, record the outcome, swallow the failure",
  plus an eighth inner half in `services/leaving_soon.py`'s `_record_skip`. One decorator. `refresh_curated_lists`'s
  docstring currently has to *state in prose* that every exit records a run, which is a guarantee
  a decorator holds structurally. **~55 lines**.
- `services/lists.py:777`, `history_sync.py:238`, `imdb_dataset.py:213` — three hand-rolled
  cache-database bootstraps and three sync-state stamps in two different SQL spellings. `cache.db`
  is disposable by contract, so all three want one primitive. **~90 lines**. The generalization
  must adopt `history_sync`'s rebuild lock, which is the strictest of the three, rather than the
  average.
- `components/GeneralPanel.tsx` and siblings — the `.set-row` label/help/control triplet typed out
  **26 times**. A `<SetRow>` also makes rule 45 structural: one help slot per
  row means one paragraph cannot cover two controls. **~100 lines**. The "three files" this said
  was counted before `Settings.tsx` split into seven panels: the triplets are conserved, the
  spread is not, so re-derive the file list before building this.
- `api/deps.py` (new) — a request accessor copy-pasted at **7** routers under two spellings
  (`_factory`/`_settings`/`_box` in `api/{auth,backup,settings,setup}.py`, `_sessions` in
  `api/{routes,runs,whitelist}.py`), `_latest_snapshot` at **7**
  sites. **~35 lines**.
- `services/login.py:115` vs `services/plex_link.py:395` — the Plex PIN flow written twice,
  differing in four tokens. Rules 11/98 and 125 sit above the seam and are untouched by the merge.
  **~65 lines**.
- The admin-password gate ritual, copied at **4** call sites, each re-deriving
  rule 11/98's hardest clause (a full gate returns 503 and must never register as a failed
  attempt). The pieces are already extracted in `api/auth.py`; only the ordering is duplicated.
  Risk `safety-path`, and note **only one of the four gates has a throttle test**.

> **Corrected: two of the four have one.** Arming (`tests/test_settings_api.py:839`) and forgetting
> a watch record (`tests/test_watch_evidence.py:451`). `change_password` and `restore` have none,
> and the extraction PR writes them.
- `services/leaving_soon.py:425` — Plex client construction at **6** sites, and the
  `None`-when-unlinked branch already reads differently in two. `safety` is keyword-only and
  required, so no copy can silently drop the guard: this is maintenance cost, not a hole.
- `services/app_settings.py:185` — the "stored wins, else env seed" rule written **7 times in 3
  spellings**, with log level resolving in `main.py` instead of a getter.
- `backup.py`/`restore.py`/`retention.py` — **5 raw `sqlite3.connect` blocks**, none using
  `db/session.py:31`'s declared pragma set, so `busy_timeout` is 5000 in two, 30000 in one and
  absent in two. Two share a byte-identical operator string. Risk `safety-path`; the pragma
  unification and the string lift should be separate commits.
- Frontend hooks: the image-fallback ladder **3 times** (`Backdrop`, `Poster`, `WhyHero`, whose
  comments already say they mirror each other), the upward dirty-report idiom **5 times**, the
  "a test result and the fingerprint it vouches for" pattern **3 times** (each fixed separately,
  in #178 twice and #264), the admin-password confirm form **twice** with a recorded drift.
- `App.tsx:196` — three parallel focus slots whose own comment reads "Rule 72: three of these now,
  and a fourth belongs in the same three places". One value keyed on `view` retires the obligation.

**Parameter objects.** Six functions take a cohesive record apart and rebuild it:
`snapshot._judge_item` (**27 parameters**), `season_scan.gather` (**25**, and it reconstructs a
`SeasonPolicy` that `SeasonPolicy.from_body` already builds; `season_evidence.py:131` names this
as rule 144's shape in its own comment), `build_season_facts` (24), `plan_series_prune` (20),
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
> `restore.py:348` on the database unpacked from an operator-supplied `.reaper`, inside the rule 74
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
> `api/auth.py:179-182` before `record_password_failure` is reachable, so all four inherit it
> rather than re-deriving it. The extraction belongs in `api/auth.py`, never in `settings.py`,
> which phase 6 splits. **It has now split**, so the four sites are two in `api/settings.py`
> (arming, and changing the password), one in `api/plex.py` (forgetting a watch record, which
> moved with the Plex routes) and one in `api/backup.py` (restore). The count is unchanged and
> only the addresses moved. The helper takes the throttle key tuple rather than deriving it: the four
> gates use distinct account keys, and merging them means a wrong restore password locks out
> arming.
>
> **`arr.py`'s three dict guards are untested** (`:68, :121, :229`) — the parametrize covers 7 of
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

### 4.3 Cross-language enums have no drift guard, and one is shipping wrong

Rule 103 requires a drift guard on a list mirroring a declaration. It lives in
`.claude/rules/backend.md`, scoped to `src/reaper/**/*.py`, so **an agent editing the TypeScript
copy of a Python enum never loads it**. The scoping split is by directory; this obligation is by
direction of the mirror.

| Concept | Python | Mirrored in | Guard |
| --- | --- | --- | --- |
| Gate ids | `engine/gates.py:62` (11) | `policyMeta.ts` `GATE_META` (7 real), 2 components, the manual | Partial, and both sides are short by the same 4 (**issue #551**) |
| Signal ids | `engine/signals.py:66` (5) | `SIGNAL_META`, `RAMPS`, `BUILTIN_SIGNAL_IDS`, 4 more | Partial: 3 TS maps unguarded |
| Verdict | **none: bare `str`** | `api.ts:17` closed union, `reviewFate.ts` | Impossible, no declaration to compare |
| Override | one model only | `api.ts:168`, `reviewFate.ts`, `StatusChip.tsx` | None |
| Chip tone | `schemas.py`'s `ChipOut.tone` | `api.ts:40`, **CSS classes** via interpolation | None |
| `InstanceKind`, `SignalState`, `MatchStatus`, `ListSource`, `ListHealth`, `ShowStatus`, `Channel` | various | `api.ts`, labels, `.env.example`, the Unraid template | None |

Two fixes, both small:

1. **`Verdict` and `Override` should be `Literal` types in Python.** The app's central vocabulary
   is currently declared only in TypeScript; `decide_verdict` returns `str`. Typing it makes mypy
   cover what no test does. Risk `safety-path` in location, typing-only in effect.
2. **One cross-reference line** in `.claude/rules/frontend.md` under rule 66, pointing at rule 103.
   No new rule number, and it closes the scoping gap for every row above.

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
> **4.3's gate row is wrong in the plan's favor.** Python has all 11 gate ids; **TS is short by 4**
> and Python by none. Everything else in the table is confirmed. On typing `Verdict`/`Override` as
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
there (`api/auth.py:341`, `:401`, `:492`; `services/login.py:159`, `:256`, `:320`, `:350`). The
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
is wrong, and that is one cross-reference line (4.3).

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
| `snapshot.py:1606` vs `engine/explanation.py:31` | The stored explanation built as a 110-line hand-typed dict on the write side, declared as Pydantic models on the read side. The reader's own docstring records that `keeps` and `match` were **silently dropped** here until the fields were declared, which is why the panel's keep breakdown never rendered. `facts_codec.py:39` is the in-tree precedent that raises at import on an unhandled field | ~30 net | `behavior` |
| `snapshot.py:1286` `Display` | The 15-field carrier exists; `RawItem` and `SeasonJudgment` both re-declare its fields flat, then `:1057` and `:1157` re-pack them field for field, then `_judge_item` unpacks it again | ~60 | `none` |
| `season_scan.py:1099` `gather` | Nine loose policy fields taken one frame above the `SeasonPolicy` that groups them, re-packed at `:1147`. This is the sole reason the second road exists: `SeasonPolicy.from_body` is the same nine assignments written again, and `season_evidence.py:140` already names it "rule 144's shape" | ~40 | `behavior` |
| `api/breakdown.py:22` + 6 siblings | 18 identically named fields copied by hand from the service dataclass to the wire model, plus a nested list re-packed 4 for 4. Same shape at `api/backup.py:224`, `api/fairness.py:159`, `api/settings.py:562` **and** `:641`, both `SeerrServiceOut(` (written twice; the second
citation read `:831` from the start, which was a banner comment even then), `api/runs.py:776`, `api/review.py`'s `_deep_links` (`return LinksOut(`) | ~180 | `none` |
| `api/runs.py:741`/`:775` `ProfileSettingsIO` | A 7-field record declared twice, **including re-typed `ge`/`le` bounds**, with a hand-written converter in each direction. Rule 131 wants a consumer's bound derived from the producer's; here it is transcribed | ~16 | `none` |
| `api/simulate.py`'s `_refused`, `_replay_simulation` and `simulate`, one `return SimulationOut(` each | `SimulationOut`'s 14-field constructor assembled verbatim at three sites. `no_longer_condemned` already went wrong exactly this way once, recorded in `schemas.py`'s `SimulationOut` ("owner actually needs before saving") | ~25 | `none` |
| `auth.py:220` vs `api/plex.py:118` | `PlexServerChoiceOut` **declared twice under the same class name in two modules**. Pydantic collapses them in `components.schemas` today; the moment either gains a field both operations get module-qualified component names and any generated client breaks silently | 8 | `none` |

**One caveat, and it is the reason this wave is not risk-free.** Building the explanation from the
model emits `defers_to_owner` and `unestablishable` as `null` where they are absent today. That is
semantically identical per the field's own docstring and `explanation_json` is in no hash, but no
test asserts a fired entry's key set, so the pinning test has to be written first.

> **Corrected: W5-1 is `safety-path`, not `behavior`.** The hash question is answered correctly and
> is the wrong question. Two deletion-path readers parse the stored explanation:
> `executor._equivalent_keys` (`:1870-1893`) raw-parses `match.merged_rating_keys` to build the key
> set the **streaming veto** and the played-since-approval check consult, catching only
> `(ValueError, AttributeError)`; and `condemned.reap_override_verdict_decoded` (`:167-226`)
> decides whether a hand reap condemns.
>
> Three more things the caveat misses. `Explanation` is already the *wire* model
> (`schemas.py`'s `CandidateDetail`), so making it the writer welds the on-disk format to the API: a later
> `exclude_none` or alias change made for the wire silently changes what is written to disk. And as
> declared it would **drop** an unhandled write-side key rather than raise, because Pydantic
> defaults to `extra="ignore"` — the cited precedent raises at `facts_codec.py:75`, so the rebuild
> needs `extra="forbid"` or it reproduces the exact incident this row is written about, moved to
> write time where no reader can recover it. `_explain` also writes the two flags on
> `protections_unknown` alone deliberately (`explanation.py:111-116`); building all three lists
> from one model writes them on the fired copy too, making that docstring false.
>
> Smaller corrections: `Display` is 16 fields, not 15, repacked at `:1057` and `:1160`; `RawItem`
> re-declares 10 of them, not all 16. `Explanation` is declared at `engine/explanation.py:213` and
> reaches the wire through `schemas.py`'s `CandidateDetail.explanation`. `season_evidence.py`'s "rule 144's shape" comment is
> at `:121` —
> the plan says `:121` in one place and `:130` in another. W5-4 names `runs.py:776`, which is
> W5-5's site in the reverse direction, and its real population is ~13 sites, not 7. W5-5 is not
> `none`: these are the deletion caps, and collapsing the models changes the 422 the operator sees,
> because FastAPI's own validation fires before `update_profile`'s hand-formatting. W5-6 has two
> verbatim copies, not three — `_refused` (`api/simulate.py`) is the refusal shape. W5-7's line numbers are both
> wrong (`auth.py:232`, `api/plex.py:118`), and the duplicate **masks the mirror test**:
> `_server_models` buckets on `__name__`, so the two classes collide into one key and a future
> divergence is checked against only the survivor.

## Wave 6: a rule stated in prose that nothing enforces

Each of these is a constraint the repo already believes in. The fix is to write the declaration
once and, where the violation is greppable, the gate that holds it, per CLAUDE.md's "write the
gate instead."

| Constraint | Where it is stated | How it is implemented | Fix |
| --- | --- | --- | --- |
| Rule 40's one control standard | `00-tokens.css:212`, in prose | 10 rule blocks re-declare the same 6 fields; 8 more re-declare the identical focus ring | One grouped base rule, ~70 lines, risk `visual` (source order) |
| Rule 94's 500-key `IN` bound | prose only | `_KEY_CHUNK`, `_WATCH_KEY_CHUNK`, three bare `500` literals, one `_CHUNK = 200` whose comment already enumerates the others | One constant + a hygiene grep, ~10 lines |
| Rule 56's paging contract | cited by all four loops | `clients/plex.py`'s `_iter_pages` hardened, `history_sync.py:380` with a backstop, `library_index.py:284` with one since #559, `seerr.py:345` and `:370` with **no backstop** | One `paged()` iterator, ~60 lines, risk `safety-path` |
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
> `library_index.py:284` and `seerr.py:345`/`:370`, modeled on `MAX_HISTORY_PAGES`, currently the
> repo's only page cap.** ~10 lines, risk `none`. `library_index.py:284` having no backstop is
> confirmed and is already **#559**; the row should point at it.
>
> **The `library_index.py` half landed early, with #559.** That walk was ending on a short page,
> which reads part of a library as the whole of it, so paging it on Tautulli's reported count was
> the fix — and that removed the short-page exit, which was the only thing bounding a server that
> reports no count and ignores `start`. `_SPINE_MAX_PAGES` replaces it. Phase 8 inherits `seerr.py`
> alone.
>
> **W6-2's `_CHUNK = 200` is not drift and must stay out of the shared constant.**
> `watch_evidence.py:78-88` says why in source: it chunks a multi-row INSERT at four variables per
> row, so 500 there would be 2,000 bound variables — the exact rule 94 failure. `snapshot.py:1812`
> carries a bare `300` on the same footing, unlisted. The hygiene grep allow-lists those two by
> name or it flags two correct values.
>
> **The sweep is nine `IN` sites, not five.** The row inherits the plan's "three bare `500`
> literals" and that is short by four: `_KEY_CHUNK` (`api/review.py`), `_WATCH_KEY_CHUNK`
> (`snapshot.py:1319`), and bare literals at both `_group_rollups` chunk loops (`api/review.py`), `snapshot.py:1844`,
> `imdb_dataset.py:341`, `services/fairness.py`'s `_evidence_index` and `_distinct_episodes` and `season_scan.py:873`. Under-scoping a sweep
> is rule 72's own failure mode, so count before extracting.
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
> **W6-7's `env_flag()` already exists** as `launcher.py:226 desktop_flag`, called from two places.
> The row is adopt-at-six-readers, not write-one. Neither `_TRUE` nor `_FALSE` is the right
> unification: fail-closed for the update check is "do not dial out", and for the tray it is the
> opposite, since on a frozen build the icon is the only route to Quit. Widen `desktop_flag` so an
> unrecognized value falls to `default` rather than to False. A live divergence sits beside it:
> `api/settings.py:1130` reports the tray as `default=True` while `launcher.py:379` defaults to
> `frozen`, so a source run on macOS tells the operator the tray is on and never starts one.
>
> **W6-4's `fold()` must skip three sites** that omit `strip()` on purpose (`engine/fields.py:821`,
> `:1000`, `services/list_config.py`'s `_clean_config`). The count is 30 inline copies across 11 modules. And
> `list_config.py`'s `_refuse_name_twice` compares SQL `func.lower()` against Python `casefold()` — ASCII-only against
> full Unicode — which a shared `fold()` makes greppable and leaves wrong.
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

Measured: 108 Python modules, 514 edges, **zero top-level cycles**. Every cycle is already broken;
the question is which breaks earn their keep, and most do not.

- **All 8 remaining cycles pass through `reaper.launcher`, and 6 of them exist for one string.**
  `services/backup.py:56` and `restore.py:62` import the application entry point, which owns
  uvicorn, the tray and AppKit, to read `LAUNCHER_CONF_NAME`. Moving that constant to `config.py`
  is ~4 lines and de-lands `reaper.launcher` from `services.backup`'s 347-module import closure.
- **`api/auth.py` holds five helpers it never calls**, and `api/backup.py` and `api/settings.py`
  reach across into its underscore namespace for them. `_verify_admin_password` and
  `record_password_failure` appear exactly once in `auth.py`: the `def` line. This is the gate
  that guards arming deletion, living in a private namespace two other routers depend on, and **no
  test imports any of the five**. Move them to the `api/deps.py` wave 3 proposes, drop the
  underscores, and add the missing test in the same commit. ~90 lines moved, risk `safety-path` as
  pure motion.
- **Three cycle-breaking workarounds in `scan_runner.py` break no cycle** (a `TYPE_CHECKING`
  import, the same symbol imported again inside a function, and a third function-local import),
  verified empirically. `executor.py:135` `TYPE_CHECKING`-imports a module already imported at
  `:117`. `api/simulate.py`'s `_replay_simulation` function-local `build_gates` import breaks nothing and carries no comment, unlike
  `launcher.py:551` and `:569`, which name their reasons and stay.
- **Both frontend cycles are one borrowed symbol each.** `PolicyEditor ↔ PolicyRuleEditors` exists
  because the deliberate split left three lookup tables behind, and the same file re-exports
  `humanDays` from `format.ts` whose own comment says it moved there to break a cycle back through
  this module. `ScalesPanel ↔ UnmatchedList` is a 12-line presentational fallback.
- **12 modules import `clients/base.py` for `IntegrationError` alone** and pay a 384-module httpx
  closure. `api/scan.py` is the clean case. A leaf `clients/errors.py` with a re-export during the
  move keeps every `except` clause identical.
- **`api/runs.py:422 reap_in_flight` is run state living in an HTTP router**, imported by
  `main.py` and by `api/backup.py`, which thereby depends on an 801-line router for one boolean.
  It is the only `api → api` edge that is not `schemas`, `tags` or `auth`. Risk `behavior`: it
  gates a database-lock interaction, and `tests/test_scheduler.py:356` names the chain in prose,
  so that comment moves too.

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
> the only such edge: `api/runs.py:31 → api/scan.py` is a second.
>
> **"6 of the 8 cycles exist for one string" is 7.** Only `api.settings → launcher → main →
> api.settings` does not pass through `backup.py`/`restore.py`. The 8-count also depends on an
> unstated convention — count `TYPE_CHECKING` edges and there is a 9th,
> `services.list_config ↔ services.lists`, not through launcher. `reaper.launcher` loads neither
> uvicorn nor AppKit at import; both are deferred. The move saves 25 modules, and `config.py` is a
> clean leaf, so it cannot cycle.

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
   `LAUNCH_BROWSER`, `TRAY` and `DOCK_ICON` are read from raw `os.environ`, and `config.py:248`'s
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

**Control-flow shape.** A `PlexItem | None` re-tested per field at **22 sites in 3 modules**,
where every inline fallback is byte-identical to the dataclass default (~20 lines). The same raw
field re-parsed 3 to 5 times inside one loop body in `_raw_items` (~12). `field.type` dispatched
by four separate if-ladders plus two inline ternaries in `PolicyRuleEditors.tsx`, so a new
`FieldType` is silently wrong in six places with no compile error, and **no test pins the
conversions** (~20, `behavior`). `_kept_phrase`'s 12 `if` arms, six of which return a bare
constant, where the pinning test is *already* a parametrized table over exactly those pairs (~20).
`scan.py`'s `condemned` counter kept in lockstep by hand with `len(condemned_keys)`.
`breakdown.py`'s (count, bytes, unknown) triple written four times plus three parallel dicts
re-zipped (~15). `build_sources`' Radarr and Sonarr loops differing by two class names (~14).
`sync_protection_lists`' four parallel slug sets and four hand-written sweep calls (~10,
`safety-path`: rule 115's `when=` predicates survive verbatim).

**Concurrency and caching.** Two identical bounded poll loops in the executor (~10). A lazy
`app.state` getter written four times, and two detached-background-job blocks that share their
whole shape (~45; the cancel-and-await asymmetry is rule 128 and must stay a parameter). Elapsed
milliseconds computed 11 times, 7 of them inside `run_scan` (~15). `_maintenance_specs` rebuilt
from six threaded dependencies at four call sites, once per job per reschedule (~40). Two
`Retry-After` parsers that differ on a negative header (~10; the two *caps* are deliberate and
stay). The cooperative-yield stride spelled two ways at four sites. Concurrency bounds written per
caller, with `services/fairness.py`'s `_enrich_titles` fanning out up to 80 live Seerr calls unbounded while
`auth/ratelimit.py:200` ships an unused `ConcurrencyGate` (~20, `behavior`: adding the bound is a
fix, so ship it separately).

**Frontend state.** Four query keys declared 2 to 5 times as literals with **divergent** options,
including three different `staleTime`s for `general-settings` and three different refetch
intervals for `scanStatus`, while four other keys already have shared hooks (~40). The
running-to-not-running falling edge hand-written six times (~20). `ServiceModal`'s two
structurally identical map-plus-suggestion state machines, carrying the same `exhaustive-deps`
disable twice (~25). The switch-confirm *caller* written twice, where the component half was
already extracted (~15). Six navigation callbacks drilled 3 to 4 levels over a destination type
`navIntent.ts` says is already one value (~40). Three hand-rolled 250 ms debounces. The
parent-Back-guard ref mirror written three times.

**Frontend components and CSS.** The `.field-sm` triplet typed 21 times across 7 files, which is
the modal-side sibling of wave 3's `.set-row` finding and the same rule 72 sweep (~40).
`WhyPanelFallback` and `ScalesPanelFallback` as the same 30-line component twice, with a comment
saying so (~28). Four decision tones written three times each across two CSS files, 12 blocks for
4 declaration sets (~28, `visual`). The `role="progressbar"` wrapper three times, each carrying a
comment pointing at the other two (~24). The Scales balance bar computed and marked up twice
verbatim (~14). Two duplicated SVGs where one is already exported. The chip dismiss button as
three near-copies, where one comment says outright it "borrows" the other's shape (~14).
Pluralization inline **64 times** beside a `format.ts` that already implements it twice, and 14
sites calling `.toLocaleString()` where `count()` exists (~20).

**Errors and messages.** Four `IntegrationError` sentences raised twice each in `clients/base.py`
plus a third copy in `public.py`, with the explanatory comment duplicated verbatim (~20; the
`unreachable (…)` wording is hand-constructed in five test sites and is load-bearing). Two inner
handlers in `refresh_curated_lists` that duplicate the outer catch-all exactly (~12). A dead
refusal branch in `restore.py:213` whose only content is a sentence its sole caller already
refused 12 lines earlier, plus one prepare-failure sentence written verbatim four times. The
cron-refusal sentence written twice **in one function**, pinned by nothing. Three `except` arms
raising the identical 400. Four verbatim copies of one panel-load-failure sentence (the fifth,
which drops "Reload to try again", is deliberate: #195, a reload inside an editor takes unsaved
edits with it). Three identity entries in `CHECK_COPY` that the fallback already produces. The
`instanceof ApiError` unwrap ritual five times.

**Data model.** `whitelist.overrides()` and `spare_expiries()` are two full scans of one table
always issued back to back at four call sites, while a third function in the same file already
selects all three columns in one statement (~15). `ActionStep.run_id` has no index, SQLite does
not auto-index a foreign key, and `action_step`/`reap_run` are never swept because retention
deletes only snapshots and excludes every snapshot a run points at, so both tables grow for the
life of the install (one additive `create_index` revision). An `IntPk` annotation beside the
existing `UtcTimestamp` idiom renders byte-identical DDL for 13 models.

**Build and startup.** The uv bootstrap written three times, the ghcr login and image name in four
jobs across three workflows, the store-credential probe byte-identical in two workflows plus a
third shape, provenance baked twice in one workflow, and the macOS boot probe written twice inside
one step (~85 lines total, all `ci`). "Install root, else repo root" spelled three times, with
`main.py`'s SPA mount ("leave a stale second copy of the UI") re-inlining `launcher.py`'s three-parent walk from a different module that happens
to sit at the same depth, so moving either file breaks one of them silently. `preflight → migrate
→ serve` written three times where only `serve` is genuinely per-environment (~23, `behavior`, and
it is a deletion tool's boot path, so rank it last).

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
> **`_hermetic`'s own docstring is a rule 7/24 violation.** `tests/conftest.py:225` claims "no
> network"; the fixture blocks no sockets, and the 15-second test is the proof. Correct it in
> whatever change lands the socket guard.

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
