# Simplification plan

A whole-tree audit for accidental complexity, run 2026-08-07 against `6f36a0c`. Thirteen
read-only passes covered every directory: the deletion path, the scan pipeline, the season
subsystem, the engine, identity and clients, the API layer, settings and credentials, the
remaining services and persistence, three frontend lanes, the test suite, and the seams between
them.

**This document is a proposal, not a decision.** It is written to be attacked. Nothing in it has
been applied.

## How to review this

Every finding carries a file and a line, a size estimate, a risk class and the test that pins the
behavior today. A reviewer's job is to break the ones that are wrong, in this order:

1. **Find the load-bearing complexity mislabeled as accidental.** Reaper deletes irreplaceable
   data. A proposal that collapses two independent safety layers into one is a worse outcome than
   leaving the code alone, and this plan is more likely to be wrong in that direction than in any
   other. The *Do not touch* register at the bottom is the audit's own answer; extend it.
2. **Check the size estimates.** Several are extrapolated from a sample.
3. **Challenge the sequencing.** Waves are ordered by value-to-risk ratio, not by subsystem.
4. **Argue with the recommendations** in *Owner decisions*, and with wave 1.2. Those turn on the
   roadmap rather than on the code, and both reverse an earlier conclusion of this audit.

Risk classes used throughout: `none` (pure motion or deletion of unreachable code), `behavior`
(observable output could move), `safety-path` (touches or sits beside a deletion interlock),
`migration`, `a11y`, `visual`, `ci`.

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
- **The comments are the record, not padding.** `identity.py` is 47% prose, `plex.py` 41%,
  `policy.py` 26%, and the frontend runs 31%. Nearly all of it is incident history citing issue
  and rule numbers. Rule 7/24 makes a comment naming a safeguard a checkable claim, and five
  comments that failed that check are filed as #550. The other several thousand passed.

So this plan does not propose making Reaper smaller. It proposes removing four specific kinds of
waste, of which only the fourth is large:

| Kind | Where it is | Rough size |
| --- | --- | --- |
| Unreachable code and unread state | scattered, 30+ sites, plus two whole engines | ~3,600 lines |
| Files that hold several unrelated jobs | 8 files, all self-declaring their seams | ~9,000 lines moved |
| One derivation written N times | ~25 clusters | ~1,400 lines |
| The declaration tax on adding anything | settings, wire types, cross-language enums | ~1,700 lines, and the real cost is elsewhere |
| Test scaffolding that was never lifted | fixtures, fakes, render helpers | ~3,300 lines and ~44s per run |

## Measured baseline

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
| `engine/policy.py:1600 inspect` | 988 | 52 | 4 |
| `services/snapshot.py:570 scan` | 714 | 43 | 17 |
| `services/season_scan.py:1244 gather` | 478 | 34 | 25 |
| `services/planner.py:315 build_plan` | 375 | 27 | 5 |
| `services/snapshot.py:1447 _judge_item` | 111 | n/a | **27** |
| `components/PolicyEditor.tsx:1238` | 1,408 | n/a | n/a |
| `components/Settings.tsx` | 3,086 | 65 hooks, 25 `useState` | n/a |

## Wave 1: deletions and mechanical wins

Nothing here changes what the app does. Each item is independently landable and most are a single
commit. This wave is the one to run first because it shrinks the surface every later wave reads.

### 1.1 Unreachable code

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
| `services/season_scan.py:256,198` | `_SeriesWork.plan` (assigned, never read; the plan is recomputed) and `SeasonJudgment.poster_url` (never set) | 10 | `none` |
| `db/session.py:83` | `session_scope`: zero references anywhere; all 30 call sites open the factory by hand | 5 | `none` |
| `services/history_sync.py:542,550` | `state` (a bare forwarder to `_state`) and `latest` (test-only, superseded by `last_synced_at`) | 20 | `none` |
| `clients/tautulli.py:270` | `metadata`, plus the `get_metadata` entry in `READ_COMMANDS` that exists only for it | 12 | `none` |
| `clients/seerr.py:74,121` | `Requester.is_mappable` (#550) and `MediaRequest.is_available` | 15 | `none` |
| `api/whitelist.py:181,212,259` | Three routes with no frontend caller, two byte-identical to their `/api/override` siblings, plus `SpareIn`. `api.ts:2019` already records deleting the client methods for this reason | 70 | `behavior` |
| `styles/00-tokens.css:167`, `21-queue-cards.css:337`, `23-queue-chips.css:34` | `--radius-xs`, `.row-actions`, `.chip-reap`: no user in any construction path | 20 | `none` |

**One trap, recorded here so it is not walked into.** `Profile.enabled` (`db/models.py:290`) is
also unread, and is deliberately kept: it is `NOT NULL` with no server default in the frozen
baseline, so removing the attribute leaves `alembic check`, a CI gate, reporting a pending
`drop_column` forever (#271). `PendingPlexLogin.pin_code` has the same shape. Both need the
`include_name` exclusion in `alembic/env.py:45` before the attribute can go. `SizeSource`'s three
never-written members are also *not* dead: the executor's growth interlock allow-lists them
exhaustively and fails closed, so deleting one narrows a fail-closed set (rule 143).

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

**Before deleting, rehome one constant.** `FALLBACK_REWATCH_PRIOR` lives in `backtest.py:109` and
is cited from `engine/gates.py:759` and `engine/policy.py:2644`, plus `docs/SIGNALS.md:155`,
`docs/LEARNINGS.md:121` and `docs/README.md:64`. `engine/dormancy.py` is its natural home. Rule 64:
the doc citations move in the same change.

**And correct the roadmap in the same commit.** `docs/STATUS.md` carries M3c, M3f and M3g plus
open work item 2, all of which describe wiring that is no longer going to happen. `SIGNALS.md`'s
"Your library is not this library" section promises a per-operator prior that `calibration.derive`
would have fitted; it must say the curve is borrowed, full stop, until #554 ships.

Removing these also retires a standing rule-35 tax: every new `Facts` field currently has to be
spelled in both modules' builders.

### 1.3 Test-suite wall clock, for two lines

`tests/test_repo_hygiene.py:890`'s `_repo_text_files()` does a full `rglob` plus `read_text` of
every file in the repository and is called from **7** sites; `_uvicorn_launches()` re-enters it.
The file takes **53.04s**, and its 7 slowest tests, all callers, are **50.2s** of that. There is
no `functools` import in the file.

Adding `@lru_cache` to `_repo_text_files()` and `_source_files_to_scan()` is **+2 lines for ~44s
per run**. This is the single best value-to-effort item in the audit.

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
- **67 structural fakes, 60 of them declaring no base class**, behind **65**
  `# type: ignore[arg-type]` suppressions. mypy is `strict = true`, so those suppressions are the
  only reason a real client signature change does not fail the build, and the fakes have already
  drifted from each other. Six independent Tautulli fakes, seven Sonarrs, four Seerrs. Proposal:
  one `tests/_fakes.py` per client, each inheriting the real class. **~600 lines and 65
  suppressions**, and it is strictly stronger than what it replaces.
- **No complete api mock.** 37 frontend files `vi.mock` the api module; only 16 import
  `apiFixtures`. Each redeclares its own `vi.fn()` set, ranging from 1 function to 32. Rule 135
  requires the mock to answer everything the tree reads, and the fixtures supply payloads but not
  the function list, which is why the count drifts. **~500 lines**, and it closes rule 135's gap
  by construction. Risk `coverage-loss`: a file relying on an *absent* mock to reject a read would
  start getting an answer, though `frontend/src/test/setup.ts:97` still fails the run on a genuine
  gap.

### 1.5 Four tests that a linter runs or that cannot fail

`test_no_bare_exception_assertions_in_tests` (`test_repo_hygiene.py:433`) greps for
`pytest.raises(Exception)`; **ruff B017 is enabled, runs in CI, and is strictly broader**.
`test_instruction_files_exist:278` filters a list built from a glob for absent files, which a glob
cannot return. `test_the_select_name_matcher_rejects_what_it_claims_to_reject`'s case at `:3116`
can only fail after the test above it. `test_the_tagline_sites_all_exist:2239` reads the tuple
`:2230` already read. Rule 118: **~40 lines**.

## Wave 2: files that hold several unrelated jobs

Pure motion. No signature changes, no behavior changes, ~9,000 lines relocated and none removed.
Each of these files draws its own seams already, in banner comments or in the fact that its
**tests are already split** along the boundary the source is not.

| File | Now | Split | Evidence the seam is real |
| --- | --- | --- | --- |
| `api/routes.py` | 2,789 | `api/review.py` (~1,315), `api/policy.py` (~480), `api/simulate.py` (~840), `api/about.py`. `routes.py` ceases to exist | Four banner comments already name the four. `main.py:46` imports only `router`, so the change is `include_router` calls |
| `engine/policy.py` | 2,263 | `+policy_migrations.py` (~530), `+policy_warnings.py` (~1,030) | The two halves import the model and nothing imports them back. No cycle exists |
| `components/Settings.tsx` | 3,086 | 6 panels to their own files; the barrel keeps `PANELS`, the dirty record and the shell (~180) | **The tests are already split per panel** (6 files). Three sibling panels were already extracted. Only the source never followed |
| `api/settings.py` | 2,025 | `api/plex.py` (~630, 12 routes) | `api/plex_trash.py` and `api/leaving_soon.py` already exist as sibling routers. Tags are per-route, so OpenAPI grouping survives |
| `components/PlexPanel.tsx` | 1,244 | 3 sections out (~450) | The file draws the seams as banner comments, and the rule-146 dirty contract is computed from connection-section drafts only, so the other three cannot break it |
| `App.tsx` | 1,225 | 5 components to `components/` (~520) | Three carry a comment saying they are "exported for its tests" |
| `components/ReviewQueue.tsx` | 2,654 | `QueueFilterBar` (~330), `queueChips.tsx` (~60), delete the re-export shim | The filter block never reads `override`, `verdict` or a candidate. The shim's own comment calls itself transitional |
| `services/season_scan.py` | 2,060 | `guard_result` + `no_key_reason` to `season_evidence.py` (~145) | Both are pure. `api/routes.py:2113` imports the 2k-line I/O module solely to call one of them |

**One caveat that applies only to `routes.py`.** Roughly ten cross-module comments cite
`api.routes._chip`, `api.routes.simulate`, `api.routes._season_guard_replay` and
`api.routes._explanation_out` by dotted name, and five test files import its internals. No hygiene
test guards a dotted symbol citation the way `test_docs_referenced_from_code_exist` guards a doc
path. Fixing the comments is part of the change (rule 64); adding that guard is worth considering
in the same commit.

**Not recommended:** splitting `db/models.py` (1,066 lines, roughly two thirds per-column prose
explaining what a NULL means) or `engine/identity.py` (47% prose, and rule 3 is better served by
`resolve` staying beside its narrowers). Both move lines without removing any and scatter the
reasoning.

## Wave 3: one derivation written N times

Rules 72, 104 and 144 are the same obligation at three scales: a copied function, a value derived
twice, a sentence stated twice. These are the clusters where the copies exist and, in several
cases, **have already drifted**. Ordered by drift risk rather than line count, because the value
here is preventing a future divergence, not the lines.

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
- `clients/seerr.py:336-430` — the paging contract written three times; its own test is titled
  "Rule 72: the same loop, twenty lines down". `plex.py`'s `_iter_pages` is the complete-or-raise
  helper rule 56/89 names; Seerr never got one.
- `services/scan_runner.py:323,441` + `services/instances.py:582` — per-kind client construction
  in three places, and `instances.py:597` already records the drift incident (`api_path_prefix`
  reached the scan but not Test Connection).

**Largest by volume:**

- `clients/plex.py` — **21 methods repeat the same off-thread plus error-map wrapper**
  (24 `to_thread` sites, 19 identical `except` arms). One `_call(fn, *, what, lock)` helper, with
  three documented opt-outs that must stay bespoke. **~100 lines**, risk `safety-path`, 32 pinning
  assertions.
- `services/scheduler.py` — **7 copies** of "run the job, record the outcome, swallow the failure",
  plus an eighth inner half in `leaving_soon.py:641`. One decorator. `refresh_curated_lists`'s
  docstring currently has to *state in prose* that every exit records a run, which is a guarantee
  a decorator holds structurally. **~55 lines**.
- `services/lists.py:777`, `history_sync.py:238`, `imdb_dataset.py:213` — three hand-rolled
  cache-database bootstraps and three sync-state stamps in two different SQL spellings. `cache.db`
  is disposable by contract, so all three want one primitive. **~90 lines**. The generalization
  must adopt `history_sync`'s rebuild lock, which is the strictest of the three, rather than the
  average.
- `components/Settings.tsx` and siblings — the `.set-row` label/help/control triplet typed out
  **26 times** across three files. A `<SetRow>` also makes rule 45 structural: one help slot per
  row means one paragraph cannot cover two controls. **~100 lines**.
- `api/deps.py` (new) — `_sessions` copy-pasted at **5** routers, `_latest_snapshot` at **7**
  sites. **~35 lines**.
- `services/login.py:115` vs `services/plex_link.py:395` — the Plex PIN flow written twice,
  differing in four tokens. Rules 11/98 and 125 sit above the seam and are untouched by the merge.
  **~65 lines**.
- `api/settings.py` — the admin-password gate ritual copied at **4** call sites, each re-deriving
  rule 11/98's hardest clause (a full gate returns 503 and must never register as a failed
  attempt). The pieces are already extracted in `api/auth.py`; only the ordering is duplicated.
  Risk `safety-path`, and note **only one of the four gates has a throttle test**.
- `services/leaving_soon.py:425` — Plex client construction at **6** sites, and the
  `None`-when-unlinked branch already reads differently in two. `safety` is keyword-only and
  required, so no copy can silently drop the guard: this is maintenance cost, not a hole.
- `services/app_settings.py:185` — the "stored wins, else env seed" rule written **7 times in 3
  spellings**, with log level resolving in `main.py` instead of a getter.
- `backup.py`/`restore.py`/`retention.py` — **5 raw `sqlite3.connect` blocks**, none using
  `db/session.py:33`'s declared pragma set, so `busy_timeout` is 5000 in two, 30000 in one and
  absent in two. Two share a byte-identical operator string. Risk `safety-path`; the pragma
  unification and the string lift should be separate commits.
- Frontend hooks: the image-fallback ladder **3 times** (`Backdrop`, `Poster`, `WhyHero`, whose
  comments already say they mirror each other), the upward dirty-report idiom **5 times**, the
  "a test result and the fingerprint it vouches for" pattern **3 times** (each fixed separately,
  in #178 twice and #264), the admin-password confirm form **twice** with a recorded drift.
- `App.tsx:702` — three parallel focus slots whose own comment reads "Rule 72: three of these now,
  and a fourth belongs in the same three places". One value keyed on `view` retires the obligation.

**Parameter objects.** Six functions take a cohesive record apart and rebuild it:
`snapshot._judge_item` (**27 parameters**), `season_scan.gather` (**25**, and it reconstructs a
`SeasonPolicy` that `SeasonPolicy.from_body` already builds; `season_evidence.py:121` names this
as rule 144's shape in its own comment), `build_season_facts` (24), `plan_series_prune` (20),
`snapshot.scan` (17), the Plex match record threaded as **6 loose parameters through 4
signatures**. `snapshot.py:929` is the sharp case: 12 parallel `movie_*`/`tv_*` locals with
nothing structurally preventing the movie loop being handed `tv_keeps`.

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
client-only shapes. The overlay is load-bearing, not optional: generation *loosens* unions the
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
| Chip tone | `schemas.py:112` | `api.ts:40`, **CSS classes** via interpolation | None |
| `InstanceKind`, `SignalState`, `MatchStatus`, `ListSource`, `ListHealth`, `ShowStatus`, `Channel` | various | `api.ts`, labels, `.env.example`, the Unraid template | None |

Two fixes, both small:

1. **`Verdict` and `Override` should be `Literal` types in Python.** The app's central vocabulary
   is currently declared only in TypeScript; `decide_verdict` returns `str`. Typing it makes mypy
   cover what no test does. Risk `safety-path` in location, typing-only in effect.
2. **One cross-reference line** in `.claude/rules/frontend.md` under rule 66, pointing at rule 103.
   No new rule number, and it closes the scoping gap for every row above.

`SimStale` (`test_api_type_mirror.py:442`) is the exemplary case and is what the others should
look like.

## Owner decisions

Two items turned on a roadmap call rather than on the code. Both are now settled and carry a
recommendation; each reverses what this audit's first pass concluded. A third, the two unreachable
engines, was settled the other way and has moved to wave 1.2 as a deletion.

**1. `db/models.py:314` `AutonomyGrant`: a table, two CHECK constraints and an index for a feature
with no code. The recommendation here is to KEEP it and correct one sentence.** The audit's first
pass said delete, on the strength of "zero references in `src/`, `tests/` or `frontend/src/`. That
claim is true of *code* and false of *prose*, and the difference decides the question:

- `services/scheduler.py:22` — "This scheduler never deletes media ... automated deletion is an M8
  concern gated behind an earned autonomy grant". `tests/test_scheduler.py:168` and `:199` pin it.
- `engine/policy.py:386` — a policy edit "voids pending approvals and any autonomy grant keyed to
  it".
- `engine/backtest.py:260` — the number the earned-autonomy flow is designed to consume.

So the concept is load-bearing in the reasoning even though the table is inert, and the docstring
is the only full record of the design: the grant keys on `policy_hash`, so any policy edit mints a
new hash, the grant stops joining, and the profile reverts to approval-required. Autonomy cannot
be inherited by a policy nobody reviewed. The two CHECK constraints mean no row can honestly exist
until the backtest ships, so this is fail-closed rather than a footgun.

`STATUS.md:45` tracks the flow as **open work #1**, the top of the roadmap. Removing schema
immediately before building the feature is churn, and it costs a `RETIRED_TABLES` entry that would
be deleted again when M3b lands.

What *is* wrong is the docstring's self-defense: it justifies keeping the schema on "pre-release,
single migration baseline", a premise that expired at revision 2 of 24. The conclusion is still
right; the stated reason is false, which is #550's class. Rewrite it to name the real reason
(M3b is next, tracked at `STATUS.md` open 1) and the rule 25 tension is at least honestly stated.

If M3b leaves the roadmap, revisit. The mechanics then need **no migration**: `alembic/env.py:45`
already has an `include_name` filter for cache tables, so a `RETIRED_TABLES` arm plus deleting the
model class leaves existing databases with an inert empty table and keeps `alembic check` green.

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
expires every ORM attribute, #327). `QuantityInput`'s `mine`/`seen` ref pair. `Settings.tsx`'s
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

Waves are landable independently and in any order, but wave 1 first is strongly preferred: it
removes ~2,000 lines that waves 2 and 3 would otherwise have to read, move and reason about.
