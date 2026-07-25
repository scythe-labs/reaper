# Backend review remediation — phase plan & tracker

The whole-backend review in `docs/CODE_REVIEW.md` (dev @ `d3c3839`, 2026-07-24) holds **two
passes**: Part I (second pass, 37 code findings + 10 test findings) and Part II (first pass,
53 findings, all still open). **100 findings total.** This file breaks them into subsystem-
cohesive phases and tracks progress.

> The previous tracker in this file covered the *fourth-pass diff review*; all five of its
> phases had landed. It is preserved in git history at `d3c3839`.

**The conversation is compacted between phases.** Whoever picks up next: read this file, read
the named findings in `docs/CODE_REVIEW.md`, then implement that phase end to end. Fix *twins
together* — the phases are grouped so twins land in one change.

**Read `docs/CODE_REVIEW.md`'s "Read this first" section before touching anything.** Three
traps it names: B2-5's obvious fix breaks 11 tests, B2-10's first-listed fix marks a failed
verification `VERIFIED`, B2-8's fix changes freshly-built plans too. Every finding's
**Verifier's correction** paragraph governs over its **Fix** paragraph.

**Working agreement for this remediation**
- Work happens in the `backend-code-review` worktree, on branch `worktree-backend-code-review`.
- **Commit and push at the end of every phase**, once its gates are green — one commit per
  phase, then `git push origin worktree-backend-code-review`. The operator standing-authorized
  this on 2026-07-24 after an uncommitted tree nearly cost three phases of work. It overrides
  CLAUDE.md's "commit only when asked" for this branch and this remediation only.
  **Never `git checkout -- <directory>`**: to undo a bad bulk edit, revert the individual files
  you touched, or re-edit them forward.
- End every phase by running the CLAUDE.md verification gates for the files touched
  (`uv run ruff format .`, `ruff check .`, `mypy src/reaper`, `pytest`) and record the real
  result in that phase's section — including anything left failing.
- When a phase is finished, tick its box below, change its heading to `✅ DONE`, and replace its
  **Planned work** section with **What was done**. A phase section that still says "Planned
  work" has not been implemented, whatever the box says.

## Progress

- [x] **Phase 1 — Engine: protections that silently do nothing** (the critical + 2 highs) — *done 2026-07-24*
- [x] **Phase 2 — Identity binds & keep-list joins** (2 highs) — *done 2026-07-24*
- [x] **Phase 3 — Plex client hardening & protection lists** (3 highs) — *done 2026-07-24*
- [x] **Phase 4 — Executor & the deletion path** (10 findings) — *done 2026-07-24*
- [x] **Phase 5 — Snapshot & scan pipeline** (14 findings; B2-24 already fixed in Phase 2) — *done 2026-07-24*
- [x] **Phase 6 — API routes, runs & query performance** (14 findings) — *done 2026-07-24*
- [ ] **Phase 7 — Security, auth, restore & infra**
- [ ] **Phase 8 — Fairness, Leaving Soon & engine cleanup**
- [ ] **Phase 9 — Test suite**
- [ ] **Phase 10 — Merge the review's Agent Rules into CLAUDE.md**

Every finding in `docs/CODE_REVIEW.md` is assigned to exactly one phase below (100 total: 37
Part I code + 10 Part I test + 53 Part II).

---

## Phase 1 — Engine: protections that silently do nothing  ✅ DONE

**Findings:** B2-1 (**critical**), B2-3 (high), B2-4 (high), B-7 (medium), PR-2 (medium),
B2-15 (low), B2-16 (low), I2-1 (low), I2-2 (low).

**Theme.** Every finding here is a protection the operator believes is running that either
cannot fire or has been silently withdrawn — plus the engine honesty defects around them.
B2-1 is live on every tester DB right now.

**What was done**
- **I2-2** — rules **70–87 restored into `CLAUDE.md`** under "Blockers from the fourth review
  pass", recovered verbatim from `git show a7d7659:docs/CODE_REVIEW.md`. They had been written
  as that pass's "Agent Rules" and never merged, then left the tree when `d3c3839` replaced the
  review file. All 16 rule numbers cited across `src/` and `frontend/src` (70–78, 80, 82–87)
  now resolve; verified by grepping every citation against the file.
- **B2-1** — new `policy.recover_rating_rules(raw)` beside `rebalance`, called from
  `profiles.active_policy` **before** validation (a legacy body validates cleanly, so
  validation cannot see the loss). Trigger is exactly: raw key `keep_rating_rules` **absent**
  (an explicit `[]` is left alone, rule 1) **and** an **enabled** `rating_floor` gate carrying
  numbers the old validator would have accepted (`1 <= threshold <= 100`, `secondary >= 1`).
  It synthesizes the equivalent IMDb `RatingRuleSpec` and stamps `schema_version` at the new
  `SCHEMA_VERSION = 3`. Not keyed on `schema_version` (affected bodies already carry 2).
  A **disabled** gate is deliberately left alone: nothing was protecting anything either way,
  so there is nothing to restore and no reason to degrade a scan over it.
  New `ActivePolicy.rating_rules_recovered` feeds `repaired`, so `scan_runner` degrades the
  snapshot (its message now names *which* part was recovered), and a new `PolicyOut
  .rating_rules_restored` opens the editor dirty with its own notice ("Keep well-rated titles
  had stopped keeping anything…"). Not folded into `needs_save`, whose copy is about the
  points rescale and would have been a lie here.
- **B2-3** — `media_types=("movie",)` on the `release_age` and `quality` FieldSpecs (both
  lanes, per the verifier: a dead TV *condemn* rule keeps its share of the fixed 100-point
  denominator and holds down every TV score). Stored rules that name a field their media type
  cannot read now raise a `danger` warning in `policy.inspect` — covering
  `protect_conditions`, `custom_condemn` and `graded_keeps`, each anchored to its own section —
  so an existing rule is *told*, not silently dropped from the editor.
- **B2-4** — `Condition._validate_value_type` refuses a TEXT value whose `.strip()` is empty,
  and for `Op.IN` a target whose `_split_csv` yields nothing (a comma-only list). Editor side:
  `coerceValue` trims and both Add buttons test `.trim()`, so the UI cannot compose one.
- **B-7** — `evaluate_signal` now carries the observation it read into the shared tail: an
  `Absent` input returns `NOT_APPLICABLE`/`evaluated=True` (weight kept, coverage intact) like
  `SEASON_RANK` and the graded custom arm already did, and the "could not read" details are
  split from the "there is none" ones. **Six of 440 policy-lab vectors moved, all coverage
  9000 → 10000, no score and no verdict changed** — re-pinned with a new
  `scripts/policy_lab_extract.py --rebaseline` mode that re-judges the committed fixture
  without needing a real library (so CI and any contributor can reproduce it, and it prints
  every vector that moved).
- **PR-2** — an enabled built-in `SIZE` signal now raises the same `danger` warning the
  hand-written size rule does, anchored to a new `signals` warning slot in the editor; the
  `SignalId.SIZE` docstring's "the UI warns about it" claim now cites the code that does it.
- **B2-15** — `OthersWatchingGate` **retired** (rule 38): no builder ever produced a `Known`
  `others_watching`, so it could not protect anything while reading as a check that ran. Gone:
  the gate class, its `GATE_TYPES` entry (a policy still enabling it now refuses to scan with
  the existing "no implementation for it" `ScanConfigError` — loud and fail-closed), the
  `Facts` field, all three builders' assignments, the `_OBS_FIELDS` entry, the frontend
  `policyMeta` copy, and the fixture/generator entries. `GateId.OTHERS_WATCHING` **survives**,
  documented as retired, so an explanation stored while it was built still decodes.
- **B2-16** — `ratings.from_radarr` guards the votes parse like its sibling score parse, so a
  non-integer votes field costs that one rating instead of raising out of the scan (rule 32).
- **I2-1** — `MinDormancyGate`'s docstring now says what is true: the cliff positions are
  documented defaults and the threshold is the operator's own stored number. The false
  "derived from your history" claim is gone from `backtest.py`'s prior comment too, and
  `calibration.py` gained the "engine-complete, not yet reachable" header it lacked.

**Gates run:** `ruff format`, `ruff check`, `mypy src/reaper` clean; **pytest 2062 passed**;
`alembic upgrade head` + `alembic check` clean (no schema change); frontend `lint`, `test`
(268 passed) and `build` clean.

---

## Phase 2 — Identity binds & keep-list joins  ✅ DONE

**Findings:** B2-5 (high), B2-6 (high), B2-7 (medium), P-6 (low).

**Theme.** Cross-system joins that bind the wrong item or drop an id the item carries. All
resolve toward deleting something the operator protected or never meant to touch.

**Files:** `engine/identity.py`, `services/snapshot.py`, `services/season_scan.py`.

**What was done**
- **B2-5** — the basename tier is now a **cross-check** of a bound id, not just a fallback for
  when no id bound. The verifier's variant, not the Fix paragraph: the bind-or-abstain branch
  is kept verbatim for `tier1 is None`, and when an id bound, only `len(hits) == 1` sets
  `tier2` — a name naming several listings is silence, because under a shared id that is the
  merged-twins shape tier 1 has already narrowed, and re-deciding it here destroys that
  narrowing (this is what fails 11 tests in the naive variant). A `tier2` landing inside the
  merged group normalizes to the canonical key, mirroring the tier-3 normalization two lines
  above it: agreement with a merged group, not a contradiction. The reconcile is untouched, so
  the only new outcome is an abstain, which keeps the file. Module docstring's tier-2 paragraph
  rewritten to describe the two roles.
- **B2-7** — shows consult imdb, but only as a cross-check. `resolve` gained a `binding_ids`
  argument beside `id_priority`: `SHOW_ID_PRIORITY` is now `("tvdb", "imdb")` while
  `_SHOW_BINDING_IDS` is `{"tvdb"}` alone, so a cross-check-only kind with nothing yet to check
  stands down instead of originating a bind. Deliberately **not** the bare priority-tuple
  change the Fix paragraph offers first: that lets imdb bind a show on its own, creating new
  deletable shows where the ladder abstains today. Both tuples are now public and the
  diagnostics helpers (`candidate_libraries`, `libraries_for_ids`) in `season_scan` and
  `snapshot` take them instead of their own literal copies, so the libraries an operator is
  shown cannot drift from the ones the resolver looked at.
- **B2-6** — the movie keep-list lookup passes `item.imdb_id or item.plex_imdb_id`, mirroring
  the TV path's `show_imdb_id` (rule 29). The exposed case is narrow but real: Radarr is
  tmdb-native, so a blank `imdbId` is ordinary, and a "Never Reap" collection on a legacy-agent
  Plex library is stored under an imdb id and nothing else.
- **P-6** — `_twin_group` reads each candidate's size once. The `is not None` half of the test
  was redundant under the `file_size is None` guard above it (an unknown size is `None`, and
  `None == file_size` is already False), so the equality test alone carries it; noted in a
  comment so it does not read as a dropped check.

**Tests added (9).** Three in `TestTheContradictionVeto` for the id-vs-basename cross-check —
the finding's own repro (the id binds the matched listing, the file name names the row that
actually holds the *arr's file) asserting the exact detail string, plus the agreeing case and
the multi-hit-is-silence case. A new `TestAShowsImdbIdCrossChecksItsTvdbBind` (4) covering the
stale-tvdb abstain, the agreeing bind, imdb-alone-never-binds, and imdb-alone-still-leaves-the-
title-backstop-intact. Two in `TestAKeepListRowIsFoundByEveryIdTheMovieCarries` for B2-6, both
directions (a Plex-matched imdb id finds the keep-list row; neither id present does not invent
a match).

**Gates run:** `ruff format` (2 files reformatted), `ruff check`, `mypy src/reaper` clean;
**pytest 2071 passed** (2062 + the 9 new); `alembic upgrade head` + `alembic check` clean (no
schema change). Frontend untouched this phase, so its gates were not re-run.

---

## Phase 3 — Plex client hardening & protection lists  ✅ DONE

**Findings:** B-1 (high), B-2 (high), B2-2 (high), B-3 (medium), B-4 (medium), B-5 (medium),
B2-25 (low), I-1 (low-medium), I-2 (low-medium).

**Theme.** The Plex client's remaining title-keyed lookups and unpaged reads, plus the
protection-list sync that can wipe stored membership. B-1 is the keep tag that silently
protects nothing for any operator whose tag has an uppercase letter.

**Files:** `clients/plex.py`, `services/lists.py`, `services/snapshot.py`,
`services/executor.py`, `api/settings.py`.

**What was done**
- **B-1** — new `lists._tag_key` (strip + casefold, the *arr side's `normalize_label`) is
  applied on **both** sides of the tag-id map, so the operator's own capitalization matches
  the label Sonarr/Radarr stored. Was live for anyone whose keep tag is not already
  lowercase: the tag read as missing, the first sync stored an empty membership with
  `last_error = NULL` (a reported success), and every keep-tagged title stayed deletable
  forever with the list showing as healthy.
- **B-2 / B-3** — `section_paths` now returns a list of `PlexSectionPaths(key, title,
  locations)` instead of `{title: paths}` (a title-keyed dict silently dropped one of two
  same-titled libraries, so its post-reap refresh mapped to nothing), and `item_count`,
  `is_refreshing`, `refresh_path` and `empty_trash` take a **section key** resolved through
  `sectionByID`, like `add_label` / `remove_label` / `remove_collection_members` already did.
  The executor's whole trash interlock (`_affected_sections`, `_section_pre_counts`,
  `_deleted_by_section`) is keyed by section key, with a `_section_titles` map so log lines
  still name the library; `_section_title` is the one place that falls back to the number.
  `api/settings.py` builds its title-keyed prefill map from the new rows, merging duplicate
  titles' folders instead of dropping one. **No `library.section(title)` call survives in
  `src/`** (rule 72's twin sweep) — including `lists.PlexCollection`, which now asks every
  library of that title in turn for the keep collection, since reading it off the wrong twin
  looks exactly like the collection having been deleted.
- **B2-2** — `section_paths` maps failures to `PlexError` like every sibling read. It was the
  one that did not, and the raw plexapi exception escaped `_best_effort_refresh`'s
  `except PlexError` *after* a file was already deleted: step stuck at SENT, run stuck
  EXECUTING, every remaining approved deletion never attempted. `refresh_path` was confirmed
  to map already.
- **B-4** — `_iter_section_pages` generalized to `_iter_pages(server, path, query)` (the
  section form is now a two-line wrapper), and `find_collection` / `collection_children` run
  through it. Unpaged, a windowed server made a shelf past the first window read as absent
  (the caller then creates a *second* "Leaving Soon" collection) and truncated the member set
  the reconcile computes `current - wanted` from. The loop's ratingKey contract also replaces
  `collection_children`'s old silent `if el.get("ratingKey")` filter.
- **B-5** — `lists.sync` distinguishes "the container came back empty" from "the container
  came back full and nothing in it could be identified" (rule 27). The second is now a
  `ContainerMissingError`, so a Plex agent change whose guids stop parsing keeps the stored
  membership instead of wiping it and unprotecting every title on the list.
- **B2-25** — new `lists.retire_absent(engine, family=, current=)` disables every enabled row
  in a slug family the current configuration no longer produces, called from
  `sync_protection_lists` for the keep-tag and Plex-collection families. Covers all three
  triggers the verifier named: flipping any→all, clearing the tags entirely (no provider is
  built at all, so nothing else touches the list), renaming the collection, plus a removed
  instance. Three deliberate constraints: a **failed** sync's slug is still in `current` and
  is never retired (its stored copy is the right list, just stale); the Plex family is only
  retired when `plex_server` is not None, so an unreachable Plex cannot unprotect a "Never
  Reap" collection; and `sync`'s upsert now sets `enabled = 1` on conflict, so flipping back
  resumes the old list instead of leaving a synced-but-disabled keep-list protecting nothing.
  Read and write share one transaction (rule 58).
- **I-1** — the batched `/library/metadata/{ids}` enrichment logs `plex.metadata_batch_short`
  when a server returns fewer elements than requested. Deliberately a log, not a degrade, and
  the comment says why: unlike the sweep, this read only *adds* evidence (ratings, folder
  paths), so losing it lowers pressure and widens abstains rather than making anything more
  deletable. Silence was the defect.
- **I-2** — `add_label`'s docstring no longer claims a runtime assertion that was never
  written (rules 7/24). It now says plainly that label preservation is verified against a
  live server and **assumed at runtime**, and where the read-back would go if it is ever to
  be enforced.

**Tests added (19).** `test_lists.py`: three parametrized mixed-case keep tags plus the
genuinely-absent tag still raising (B-1); the collection found in the second library of that
name, a missing library as a hard failure, and a populated collection whose ids all fail
never wiping the list (B-5). `test_protection_sync.py`: any→all actually tightening, flipping
back protecting again, clearing the tags retiring the whole keep-list, a failed sync never
retired, a renamed collection retiring the old one, and an unreachable Plex never retiring
(B2-25). `test_plex_sweep.py`: a collection past the first window still found, a truncated
member list never read as the whole shelf, an unbounded full page raising (B-4), two
same-titled libraries both surviving `section_paths`, and a failing read surfacing as
`PlexError` (B-2/B2-2). `test_reap_loop.py`: two libraries sharing a title refreshed and
purged by key, which the old title-keyed fake could not even express.

**Gates run:** `ruff format`, `ruff check`, `mypy src/reaper` clean; **pytest 2090 passed**
(2071 + the 19 new); `alembic upgrade head` + `alembic check` clean (no schema change).
Frontend untouched this phase (`lists.configured` has no route and no UI reads the table), so
its gates were not re-run.

---

## Phase 4 — Executor & the deletion path  ✅ DONE

**Findings:** PR2-1 (medium), B2-8 (medium), B2-9 (medium), B2-10 (medium), B-6 (medium),
B-9 (low-medium), B2-17 (low), I2-3 (low), PR-8 (low), PR-9 (low).

**Theme.** What happens once the operator has armed and pressed the button: staleness checks,
mid-flight overrides, honest accounting, and the catch-all that keeps a half-finished run from
wedging.

**Files:** `services/executor.py`, `services/planner.py`, `services/profiles.py`,
`services/whitelist.py`, `api/runs.py`, `api/schemas.py`, `db/models.py`, one new migration.

**What was done**

- **B2-8 — a policy edit now voids a pending plan.** `execute()` compares `run.policy_hash`
  against the policy in force (new `profiles.live_policy_hash`, the one place that combination
  is spelled) and refuses with plain copy telling the operator to re-scan. Checked in the dry
  run too, so the simulation proves the same refusal before they type the phrase.
  - **The product decision the tracker flagged.** As the verifier warned, this also refuses a
    plan built *after* the edit, because a policy change does not trigger a rescan. That is
    deliberate: both plans were scored under the old policy, the Policy page already says a
    saved policy takes effect on the next scan, and the prime directive settles the tie toward
    keeping the file. The remedy is one scan either way. The alternative (delete the claim in
    `planner.build_plan`'s docstring) would have left the hazard the finding is about: an
    operator adds a protection, then deletes the very files it protects.
  - **Removed the drift hazard that made the interlock untrustworthy.** `build_plan` took a
    `policy_hash` argument that every caller filled with `snapshot.policy_hash` and nothing
    checked; a caller free to pass a different value could feed the new interlock the wrong
    number. It now reads the hash off its own snapshot, and the parameter is gone (51 test
    call sites and the one route updated).
- **B2-9 — overrides reach a run already in flight.** `_refresh_overrides` re-reads the
  decisions before every item of a real run (a plain re-query: `whitelist.overrides` is a
  two-column Core select, so the run session sees other sessions' committed rows), and the
  per-item check now routes through the production `condemned.effective_verdict` rather than a
  second copy of the membership rule. Intersected with the run-start effective set, which stays
  frozen as the ceiling, so a refresh can only ever **remove** items — a reap added mid-run
  cannot smuggle in an item outside what the operator confirmed. Fail-closed: an unreadable
  re-read stops the run. `whitelist.overrides`'s docstring and the executor's field comments
  updated to the new contract, as the finding required.
- **B2-10 + the second half of PR2-1 — a removal is charged even when the step fails.** New
  nullable `action_step.file_removed_at` (migration `8192a3b4c5d6`, chained onto the frozen
  baseline's head; epoch-integer like every other timestamp here). Stamped and committed the
  moment `gone` is proven, **before** anything that could fail, so the exclusion poll, the Plex
  refresh and any surprise all happen after the record exists. `_rolling_30d_deletions` counts
  `VERIFIED OR file_removed_at`, so an intermittently slow Radarr can no longer buy unlimited
  deletions. The step stays FAILED — the verifier's correction, taken: marking it VERIFIED would
  make the journal claim a verification that explicitly failed. Same stamp on the season path
  when fewer files remain than were sent. `RunReport.removed_unconfirmed` /
  `library_changed` drive the post-run rescan, which `deleted_items` alone was skipping.
- **PR2-1 — no surprise wedges a run.** Catch-alls in `_send_for_real` (funnels through
  `_fail`, so one item's surprise fails that item, not the world) and in `execute()` (records
  ABORTED and **returns the report** rather than re-raising — the report is the only record of
  which files actually went, and the finding's real complaint was the operator seeing a bare
  error string). Rule 72 twins swept: `_best_effort_refresh`, `_finalize_plex` and
  `_mount_is_up` all widened from their narrow handlers to `except Exception`, since all three
  are documented as never fatal / fail-closed. (The `section_paths` error mapping this finding
  names as the root cause landed in Phase 3 as B2-2.)
- **B-6 — season pruning tidies Plex.** `_send_season` calls `_best_effort_refresh` on the
  series folder after a verified prune, so a TV section finally joins the affected set and the
  trash purge covers what the class docstring always claimed. `plex_entries=1` is the honest
  ceiling: a TV section counts shows, so pruning one season of a multi-season show removes
  none and the count-delta gate declines — the safe answer, not a wrong purge.
- **B-9 — a season with no files resolved is skipped, not verified.** An empty live resolve
  made `delete_episode_files([])` a no-op, found nothing remaining, and marked the item
  VERIFIED with the full approved size charged to the budget. It is now a visible skip.
  `_mark_skipped` also stopped overwriting already-VERIFIED steps (mirroring `_fail`), so the
  unmonitor that really took keeps its mark.
- **B2-17 — the progress bar counts what the operator authorized.** `total` is now
  `_deletable(...)` — the executor's own acted-on set, the same one the confirmation phrase and
  the caps count — and `done` advances only over that set, so the denominator no longer flips
  mid-run to a number larger than the one they typed. Items kept by an override are still
  walked and still reported; they land in `report.skipped`, which the UI already renders beside
  the bar as "spared". No new wire field and no frontend change: the two numbers the verifier
  asked for were both already carried.
- **PR-9 — shutdown no longer waits on Plex.** `_commit_and_finalize(cancelled=…)` commits the
  run's final state on the cancel path but skips the settle-wait and purge, which could hold
  shutdown open ~20s per section and purge trash mid-teardown.
- **PR-8 — bounds on the destructive path's inputs.** `CreateRunIn.media_keys` capped at 5000
  (omitting the field still means the whole set, so nothing is truncated) and both `media_key`
  fields at 100, the storage bound.
- **I2-3 — the docstring stopped inviting a maintainer to disable an interlock.**
  `_row_timestamp`'s None now says "no readable time", names `_watched_since_approval` as the
  consumer that spares on it, and says not to "fix" the caller.

**21 tests added** across `tests/test_reap_loop.py` (policy-edit refusal incl. the dry run and
the edit-then-undo case; spare / un-reap / re-reap mid-run plus the fail-closed unreadable
read; the failed-but-removed stamp, its rolling-budget charge and the rescan trigger; the
progress denominator; the season Plex refresh; the vanished-season skip; the unmapped-error
paths) and `tests/test_api.py` (both length bounds). Four existing tests were updated where
Phase 4 deliberately changed behavior — the hard-cancel test now asserts the purge is
*deferred*, the rolling-window test ages both stamps, the journal-durability test raises a
`BaseException` (an ordinary exception is now handled, which is the point), and the two
migration tests stopped pinning a hand-written head.

**Gates run:** `ruff format --check`, `ruff check`, `mypy src/reaper` clean; **pytest 2111
passed** (2090 + the 21 new); `alembic upgrade head`, `alembic downgrade -1` and `alembic
check` all clean. Frontend re-run for confidence after the incident below: eslint clean,
**vitest 268 passed**, build clean.

> **Incident, recorded so the next agent does not repeat it.** Mid-phase I ran
> `git checkout -- tests/` to undo an over-broad sed, which discarded every uncommitted test
> change from Phases 1–3 as well. All of it was recovered by replaying the Edit/Write tool
> calls out of the session transcripts in `~/.claude/projects/…/*.jsonl` plus the two helper
> scripts still in the job tmp dir, and verified by the suite returning to exactly its
> pre-loss count of 2090 before Phase 4's own tests were added. **The working tree is the only
> copy of this remediation until the operator commits — never `git checkout --` a directory,
> and prefer a targeted `git checkout -- <file>` or a re-edit.**

---

## Phase 5 — Snapshot & scan pipeline  ✅ DONE

**Findings:** PR-1 (medium), H-1 (medium), B2-11 (medium), B2-12 (medium), P-1 (medium),
B-11 (low-medium), PR-5 (low-medium), B2-22 (low), B2-23 (low), B2-24 (low), B2-26 (low),
PR-4 (low), PR-6 (low), PR2-4 (low).

**Theme.** Evidence sources that fail without degrading the snapshot (rule 28), the degradation
signal itself being detected by substring-matching free text (H-1), and the season/binge guards
that read missing data as a definite value.

**Files:** `services/snapshot.py`, `services/scan_runner.py`, `services/season_scan.py`,
`services/season_pruning.py`, `services/season_pruning.py`, `services/library_index.py`,
`services/history_sync.py`, `services/imdb_dataset.py`, `services/scheduler.py`.

**What was done**

- **H-1 + B-11** — two typed flags on `ScanContext`: `activity_degraded` and `imdb_degraded`.
  The streaming veto and the season gather now read the flag instead of
  `"tautulli-activity" in " ".join(context.degraded_reasons)`, so rewording a reason can no
  longer turn "we could not check" into "nothing is playing" (and the join is out of the
  per-movie scoring loop). B-11 rides the same flag: a 200 whose `sessions` is null, not a
  list, or holds non-dict entries now degrades instead of coercing to `[]`; so does a session
  whose rating key will not parse as an int (which previously escaped the `except
  IntegrationError` and aborted the scan).
- **B2-12** — a degraded IMDb dataset now reads as **Unknown**, not Absent, on **both** fact
  builders (`snapshot.build_facts` and `season_scan.build_season_facts`, via a new
  `rating_dataset_degraded` threaded through `_judge_series` and `_rating_obs`). Absent said
  "we checked and this title is unrated" for the entire library at once, which withdrew every
  rating-based keep and let the why-panel assert a check that never happened. The comment at
  the old Absent branch claimed degrading prevented this; it did not, and now says so
  accurately (rule 24).
- **PR-1** — `_allowed_sections` returns `(sections, degradation_reason)`. A settings-read
  failure still scans every library (a viewable snapshot beats an aborted one) but now
  degrades, because widening is the *condemn* direction: a library the operator turned off is
  one whose items resolve unmatched and are kept, so silently re-adding it walks those files
  into the condemnable set. Its docstring called that the "safe" fallback; corrected.
- **PR-4** — the Tautulli spine (`library_index._spine`) catches `IntegrationError` and
  degrades, matching the plexapi sweep beside it. A library-list timeout used to propagate
  through `gather_reaped` and kill the run with no snapshot at all.
- **B2-22** — `_flag` returns `None` for any value that is not a recognized boolean, including
  a truthy one. `bool("false")` is True, so an unparseable keep-history value read as "this
  user IS recording history": no degradation, and every title only that user watches scored as
  never-played. Recognized tokens (`true/t/yes/y/on`, `false/f/no/n/off`) map properly;
  everything else is unreadable, which degrades. Unit tests added, as the verifier asked.
- **B2-26** — a degraded snapshot no longer writes grace clocks or runs the Leaving Soon
  reconcile/announce. Both acted on a condemn set the planner already refuses. The clock is
  the one that outlives the run: it restarts only after a gap longer than a whole grace
  window, so consecutive degraded scans kept refreshing `last_seen_condemned_at` and the first
  healthy scan found the operator's warning window already spent.
- **B2-11** — `sequential_protections` now takes `last_play_by_user` and anchors each viewer
  on **both** the season of their most recent play and the highest-numbered season they have
  touched. Recency alone regresses someone mid-binge on the newest season who dips into an old
  one; number alone was the bug (a re-watcher is anchored on the finale, judged ready for a
  season that does not exist, and protected nowhere). The union is never less than the old
  behavior, which the existing suite confirms. Specials are a separate line, as the verifier
  asked: Season 0 joins the anchor set only when `keep_specials` is off (the one setting that
  can prune it), and finishing the specials protects nothing, since season 1 is not "the next
  special".
- **B2-23** — `watchers_by_season` is three-state (`int | None`), built over the seasons **on
  disk** rather than the ones Plex resolved, and `_detect_conflicts` skips a pair where either
  side is unmeasured. `0` still means "resolved, and nobody watched it". Reading an unresolved
  season as 0 invented conflicts, parked the show in permanent abstain, and told the operator a
  count that was never taken.
- **P-1** — `_fold_merged_watch_stats` chunks its `IN` at 500 like every sibling. Its
  regression test uses 36,000 keys and is a genuine tripwire: it raises `OperationalError`
  (uncaught, so the whole scan dies) without the fix.
- **PR-6** — the history page loop advances by `len(rows)` and only trusts `recordsFiltered`
  when it is actually present. `int(... or 0)` made "not told" identical to "none" and ended
  the loop after page one; a short middle page skipped the un-fetched remainder. Both truncate
  the mirror, and a shallow horizon is the largest mass-deletion vector here. A
  `MAX_HISTORY_PAGES` backstop bounds a source that never terminates.
- **PR-5** — `load`/`refresh` return a `LoadResult(rows, skipped)`, and the nightly job puts a
  high skip fraction in front of the operator on the Jobs page instead of only logging it. A
  format change that halves rating coverage clears the zero-row tripwire, and lost rating
  coverage is lost protection.
- **PR2-4** — `build_reap_gateway` registers each client for close as it is constructed and
  `pop_all()`s on success. A `box.decrypt` raising on a later instance row used to strand every
  pool already built. `push_async_callback` rather than `enter_async_context`, because the
  run's own stack enters these clients for real and must not double-enter them.
- **B2-24** — **already fixed in Phase 2.** B2-7's work exported `MOVIE_ID_PRIORITY` /
  `SHOW_ID_PRIORITY` from `engine.identity` and wired both diagnostic call sites
  (`season_scan.py` stale-library-map guard and unmatched-show log) to them. Verified, not
  re-done.

**Gates:** `ruff format --check` (179 files), `ruff check`, `mypy src/reaper` (95 files) all
clean; **pytest 2137 passed** (2114 + 23 new); `alembic upgrade head` + `alembic check` clean
(Phase 5 adds no migration).

---

## Phase 6 — API routes, runs & query performance  ✅ DONE

**Findings:** B-8 (medium), P-2 (medium), P-3 (medium), P-4 (medium), B-10 (low-medium),
B2-13 (low), B2-14 (low), B-15 (low), P2-1 (low), P2-2 (low), P-5 (low), PR-7 (low),
PR2-2 (low), PR2-3 (low).

**Theme.** Router-level robustness (one malformed stored row must not 500 a page), the
unbounded `?limit=-1`, and the review-queue/runs hot paths.

**Files:** `api/routes.py`, `api/runs.py`, `api/settings.py`, `api/poster.py`,
`api/auth.py`, `api/middleware.py`, `services/condemned.py`, `services/history_sync.py`,
`services/instances.py`, `services/app_settings.py`, `services/seeding.py`,
`services/login.py`, plus the frontend surfaces those changes reach.

**Correction to this phase's plan.** The plan said P-4 needed an additive Alembic revision.
It does not: `watch_event` lives in **`cache.db`**, which the golden rules keep disposable
and unmigrated (raw DDL, rebuildable). No migration was written, and `alembic check` stays
clean.

**What was done**
- **B2-13 + P2-2 + PR-7 (together, as the verifier asked -- they edit the same helpers'
  signatures).** One guarded parse per row, `_decode_explanation`, feeding `_dormant_for`,
  `_primary_reason`, `_chip` and the reap-override read; each still re-checks
  `isinstance(dict)` itself, so calling one directly is no less defensive than calling it
  through the queue. New `_entries` / `_match_status` / `_detail_of` helpers replace the
  unguarded `fired[0]["detail"]` and `(exp.get("match") or {}).get(...)` indexing.
  `candidate_detail` gained `_explanation_out`, which falls back to a minimal explanation
  built from the row's own columns and reports `explanation_unreadable`; the panel says so
  in the shared `.notice-warn` rather than rendering empty reason blocks that would read as
  "nothing protected this". `Explanation.threshold` is now optional and the fallback leaves
  it unset, so the panel drops its "your threshold is N" clause rather than print a number
  that is not the operator's setting.
- **B-10 -- and a correction to the review's own fix.** The review proposed reading a
  non-dict `match` as *no* match status. That trades a crash for the inversion this
  codebase exists to prevent: evidence nobody could read becoming evidence that nothing was
  wrong, which REMOVES a hold on a deletion. A `match` that is present but unreadable is
  now a BAD match (it holds the reap, like `unmatched`/`ambiguous`); a match that is
  genuinely absent stays permissive, since the field is optional precisely so rows scored
  before it shipped still read. Pinned both ways.
- **B-8** -- `limit: int = Query(50, ge=1, le=200)` on `list_runs`, `min()` dropped.
  `?limit=-1` rendered `LIMIT -1`, which SQLite reads as no limit.
- **P-2** -- new `_RunReads` per-request memo: the hand overrides, the reap profile and
  each snapshot's effective condemned set are read once per request instead of once per
  run, and steps are fetched once and handed down instead of queried twice per run. A test
  counts the `effective_condemned` calls rather than timing them.
- **P-3** -- the `override` filter no longer materializes every media_key in the lane.
  A row can only carry an effective override if its own key or its show's key is in the
  whitelist, so `_decided_keys` narrows in SQL to that bounded set (chunked at 500) and the
  real `whitelist.effective_override` still decides each one, so the precedence stays in
  one function. `override=none` became a `notin_` over that small set instead of an `IN`
  over nearly every row, which could run past SQLite's bound-variable ceiling into a 500.
  The narrowing leans on `Candidate.group_key` being exactly `whitelist.show_key`'s value;
  that invariant now has its own tripwire test.
- **P-4** -- `ix_watch_event_parent_key` added, and indexes moved OUT of `SCHEMA` into a
  new `INDEXES` map. The review said to bump `_WATCH_EVENT_COLUMNS` so existing caches
  re-run the DDL; that would drop the whole mirror and cost a full re-sync from Tautulli
  just to add an index. `ensure_schema` reconciles indexes by name instead (one
  `sqlite_master` read on the existing no-write fast path) and creates a missing one in
  place, leaving the mirrored rows alone.
- **P2-1** -- `_replay_simulation` is async and yields every `_SIM_YIELD_EVERY` rows; the
  threshold loop yields too (the verifier's correction: fixing only the replay branch
  leaves most of the stall). The row load is narrowed with `load_only` to the ten columns
  both loops actually read, which the verifier flagged as the larger share.
- **P-5** -- one Tautulli client for the artwork proxy, cached on the app and keyed on the
  instance's connection fingerprint, so rotating the key or editing the URL retires it
  rather than leaving a stale client serving. Closed by the lifespan (rule 34).
- **B2-14 -- fixed on both twins (rule 72).** The verifier's key point: a backend-only 502
  changes nothing, because the browser aborts its poll loop on any thrown status. Both poll
  routes now answer a non-throwing `status="retrying"` carrying the reason, and the hook
  keeps polling and shows it. The login twin (`services/login.py`) had the same bug one
  layer deeper: its `except PlexLinkError` arm consumed the pending sign-in, and the
  retryable error is a subclass, so a server restarting at the instant of approval burned
  the PIN during first-run setup. It now re-raises above that arm.
- **B-15** -- a batch-local `(kind, name)` set in `seed_instances`, mirroring the existing
  `seeded_singletons`.
- **PR2-2** -- `DELETE /api/settings/general/api-key` closes the header-credential lane
  (rotation only ever swaps one working key for another), clearing
  `app.state.api_key_digest` so the removed key stops authenticating immediately rather
  than at the next restart. Deny-by-default already fences it from the key itself; the POST
  docstring and the middleware comment were corrected in the same change, and a Remove
  control was added beside Replace using that row's existing two-step confirm.
- **PR2-3** -- `api_path_prefix` is now passed in `instances._client`, so Test Connection
  and the scan probe the same path. Both "version gate" claims (the `test_connection`
  docstring and `ArrClient.system_status`) and the model comment were corrected per rule
  24; per the verifier, `detected_version` is NOT dead (the UI renders it) and was left
  alone.

**Tests.** 38 added across `test_review_malformed_rows.py` (new), `test_poster_proxy.py`
(new), `test_api.py`, `test_candidate_filters.py`, `test_history_sync.py`,
`test_config_and_seeding.py`, `test_general_and_logs.py`, `test_settings_api.py` and
`test_sessions.py`; `test_review_chips.py` updated where the helpers changed shape.

**Gates.** `ruff format --check` (181 files) and `ruff check` clean; `mypy src/reaper`
clean (95 files); **pytest 2175 passed**; `alembic upgrade head` + `alembic check` clean
(no migration, see the correction above); frontend `lint`, `test` (268 passed) and `build`
clean.

---

## Phase 7 — Security, auth, restore & infra

**Findings:** S2-1 (medium), S-1 (medium), S-2 (medium), S-3 (medium), S-4 (medium-low),
S-5 (medium-low), S-6 (low-medium), S-7 (low-medium), B-12 (low-medium), S2-2 (low),
B-13 (low), B2-21 (low), PR-11 (low), PR-12 (low), PR-13 (low), R-3 (low), I-3 (low),
I-4 (low).

**Theme.** Credential material in logs, unthrottled pre-auth endpoints, and the restore swap's
interrupted-midway hole.

**Files:** `logging.py`, `logbuffer.py`, `crypto.py`, `secrets.py`, `auth/cookie.py`,
`auth/sessions.py`, `api/auth.py`, `services/admin_password.py`, `services/restore.py`,
`services/backup.py`, `clients/plextv.py`, `main.py`.

---

## Phase 8 — Fairness, Leaving Soon & engine cleanup

**Findings:** PR-3 (medium), R-1 (low-medium), B2-18 (low), B2-19 (low), B2-20 (low),
B-14 (low), H-2 (low), R-2 (low), I-5 (low), I-6 (low), I-7 (low).

**Theme.** The read-only surfaces (Scales, Leaving Soon) plus the engine's leftover parallel
implementations.

**Files:** `services/fairness.py`, `services/leaving_soon.py`, `engine/requester.py`,
`engine/facts_codec.py`, `engine/calibration.py`, `engine/backtest.py`, `clients/public.py`,
`services/history_sync.py`.

---

## Phase 9 — Test suite

**Findings:** T-1 (**critical, coverage**), T-2 (high), T-3 (high), T-4 (medium), T-5 (medium),
T-6 (medium), T-7 (medium), T-8 (medium-low), T-9 (low), T-10 (low).

**Theme.** Where the suite asserts a transcription of production instead of production itself
(T-2, T-4, T-10), and the destructive paths with no route-level test at all (T-1, T-3, T-6).

**Files:** `tests/**`, especially `tests/_policy_lab.py`, `tests/conftest.py`.

---

## Phase 10 — Merge the review's Agent Rules into CLAUDE.md

Not a code finding: `docs/CODE_REVIEW.md` ends with 41 Agent Rules (23 from the first pass, 18
new in the second). Once phases 1–9 land, merge them into `CLAUDE.md` as rules **88–128**,
continuing after the 70–87 block Phase 1 restores, and reconcile any that sharpen an existing
rule (the newer, more specific obligation governs — say which older rule each sharpens). Drop
or reword any rule whose finding was fixed differently than the review proposed.
