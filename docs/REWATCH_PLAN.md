# Rewatch likelihood (#554): the plan

> **PLAN — approved, staged, nothing landed yet.** Written 2026-08-12, after read-only
> validation of both mechanisms against a live library. Two stages, each its own PR; stage 1
> ships before stage 2 starts. Kept current as work proceeds, and moved to `docs/history/` when
> both stages have landed or the work is abandoned.

## Context

Issue #554 asks Reaper to show, per title, how likely it is to be watched again, computed from
the operator's own watch history. The score today is unsigned deletion pressure, a different
question. The deleted `engine/calibration.py` (readable at `git show 163b6d8`) proved the
curve-fitting method but had no caller and answered per-population, not per-title. This is a
fresh build in two staged pieces, per the issue's own ordering:

1. **Rewatch cycle** (protection): a title played at regular gaps is a recurring watch. Wired
   as a keep discount, not a hard gate, because young watch history must not hard-block. A show
   followed as it airs is not a cycle; stage 1 carries the mechanics.
2. **Rewatch probability**: "of titles in your library that looked like this one, what fraction
   got watched again." Context in the why card, plus one opt-in protective hold the operator
   can set as a percentage. It can never add deletion pressure.

Operator controls are simple knobs in the existing Policy row style, not rule authoring. Both
UI surfaces are mocked up and approved before frontend code (the `CLAUDE.md` mock-up rule,
`reaper-artifact` skill).

## Validation on live data (2026-08-12, read-only)

Both mechanisms were validated read-only against a live library before this plan was written.
Figures never identify a server, so the record here is qualitative.

- **Cycle detection works.** Clustering plays within 7 days into viewings, requiring 3+
  viewings, median gap 60-800d, max gap deviation <=35% of median, it surfaced a small set of
  genuinely cyclic movies, including textbook annual rewatches. **A meaningful share of
  historical cycles are dead** (last play several median-gaps ago), so the aliveness check is
  load-bearing, not a refinement.
- Show level (30-day clusters, gap 90-800d): cycles exist but are rarer, and most are dead.
  The larger cluster window correctly bridged weekly airing runs into one viewing period.
- **The curve fit is stable.** With the population drawn correctly (in the library now, added
  before the cutoff), every dormancy bucket cleared the sample floor comfortably, and the
  fitted rates tracked the shipped defaults closely in every bucket. The method produces
  defensible per-operator numbers.
- Rating-key joins between the snapshot and the history mirror were near-complete, so
  key-orphaning does not undermine either mechanism.

## Stage 1: rewatch cycle keep (own PR)

**Derivation.** New module `src/reaper/services/rewatch.py` (successor to `calibration.py`,
one derivation for both lanes, rule 104). Batch SQL over `watch_event` for the candidate key
set, chunked per rule 94; existing `(rating_key, watched_at)` / `(grandparent_rating_key,
watched_at)` indexes cover it. **No schema change anywhere**: no new `watch_event` column
(that drops the mirror), no migration.

- Movies: cluster a title's plays (any user) within 7 days into viewings. Shows: cluster
  episode plays by show within 30 days into viewing periods (bridges a weekly airing run into
  one period, so following a show as it airs is not a false cycle).
- A cycle needs >=3 viewings, median gap 60-800d (shows 90-800d), every gap within 35% of the
  median. Alive means the newest viewing is within 2x the median gap. Constants live in the
  module; they are validated starting values, not truths.
- **A show cycle counts only when its viewing periods replay previously played episodes.** A
  pattern whose bursts hit only new episodes is someone following the show as it airs; it
  never fires the keep, because the old seasons of a followed show can be genuinely dormant.
  The history rows carry the episode identity, so repeats are computable. Movies have no
  release cycle; this check is TV-only.

**Engine wiring.** New observations, populated in every `Facts` site (rule 35: `build_facts`,
`build_season_facts`, `facts_codec.facts_from_dict` thaw, `preview._bare_facts`): a boolean
"cycle alive" plus the numbers the panel needs (cycle days, viewing count). A show-level cycle
marks all that show's seasons (keep direction). Watch history unreadable routes to `Unknown`,
never `Absent` (rule 93); a stored row predating the field thaws `Unknown` (rule 104).

Protection is a **built-in `KeepConfig`** appended where `snapshot.py` assembles keeps for
both lanes: flat discount while the cycle is alive, zero otherwise. The existing keep arm
already gives `Unknown` the full discount and guarantees a discount can only lower a score, so
the fail-closed behavior comes free. Discount size: conservative start, sized against the real
score distribution during implementation.

**Policy controls.** One row in the existing house style (`policyMeta.ts`: plain label, one
help sentence): a toggle and a strength control, nothing else. Detection internals (spread,
aliveness) stay module constants, never knobs. The policy body gains the two fields; a stored
body lacking them thaws to enabled at the conservative default (the keep direction), and an
explicit off is the operator's choice and honored (rule 1's spirit). Not added to the
rule-authoring field vocabulary; that is a follow-up only if asked for.

**UI.** Keeps already render in `WhyPanel` (`KeepContributionOut`). Copy per rule 21, plain
and short: "Watched about every 12 months, 5 times. Due again soon." A cycle that existed and
stopped says so, so the operator knows why it no longer protects. Mock the why-card row
(active, stopped, unreadable) and the Policy row first, iterate to approval, then code.

**Docs and tests, same PR.** `docs/STATUS.md` open-work row edited in place; the validation
findings into `docs/LEARNINGS.md`, as shapes only, never one server's numbers. Tests pin: dead
cycle discounts nothing; alive cycle discounts; a release-following show never fires; Unknown
takes full discount; an airing run is one viewing period; the discount can never raise a score
(extend the existing engine invariants); a stored policy body without the new fields thaws to
the defaults. Rule 25 holds: backend and UI wire together.

## Stage 2: rewatch probability context (own PR)

**Fit.** In `services/rewatch.py`, at scan time, per media type. Population is **exactly the
candidate set of the running scan** (what the scorer scores), restricted to items added before
cutoff = scan time minus 365d. That satisfies the population trap by construction
(`docs/LEARNINGS.md`, *The population trap*); pin it with a test that fails if the fit ever
reads keys outside the candidate set. Dormancy at cutoff via the shared `engine/dormancy.py`
derivation. Buckets and `MIN_SAMPLES = 30` as in `calibration.py`; a bucket under 30 yields no
number. A bucket deeper than the mirror reaches is withheld until history grows into it (reuse
the horizon machinery, `history_sync.horizon` / `Facts.history_reach_days`).

**Policy threshold, protect-only.** A new policy-authorable gate in the existing protection
row style: "Keep anything likely to be watched again," holding any title whose measured chance
is at least the operator's percentage. It fires only on a measured number. A withheld estimate
(thin group, young history) means the row does not apply and every other protection still
stands, which the why card states plainly. A low or missing number can never add pressure, in
any configuration. Ships opt-in (default off) because it overlaps the dormancy floor, just in
friendlier units; when enabled it starts at 25%. Rule 38 holds: the gate ships wired to its
facts in the same PR.

**Storage and display.** Per-candidate context block added to the declared explanation shape
(`engine/explanation.py`) and written by `snapshot._explain`. No verdict input, no gate, no
signal: display only. Old stored rows lack the key and read as nothing to show (rule 104).
`WhyPanel` renders three states, modeled on `watchReach.ts`:

- "Of 214 titles here that had sat unwatched about this long, 64 were watched again within a
  year." (count carries the confidence)
- "Too few titles like this to say."
- "Not enough watch history yet."

Mock the probability block (all three states) and the threshold Policy row first, iterate to
approval, then code.

Accepted bias, stated in the module docstring: titles deleted during the lookback year are
uncountable, which nudges rates up, the keep direction (rule 31's spirit).

Tests pin: the fit reads no key outside the candidate set; a thin bucket yields no number; the
hold fires only on a measured estimate and a withheld one does not apply; no configuration
lets the estimate raise a score or flip a verdict toward condemn.

**Docs, same PR.** `docs/SIGNALS.md`'s "the curve is borrowed, full stop" section is
corrected: the display now fits per-operator, the borrowed curve remains the scoring default.
STATUS row edited; LEARNINGS gains the fitted-vs-borrowed comparison, as shapes.

## Out of scope

- #553 (returned-title regret) is separate work.
- Seasonality by calendar month: the interval approach already catches it, per the issue.
- Using the probability to add deletion pressure, ever. The only scoring surface it touches is
  the opt-in protective hold above.
- Exposing cycle facts in the rule-authoring vocabulary. Simple knobs only, until asked.

## Execution order

An implementing agent works each stage top to bottom, inside that stage's single PR. The
repo's rule files load as the governed trees are touched and carry the fine-grained
conventions; this list is the sequence.

Stage 1:

1. Mockups: load the `reaper-artifact` skill, mock the why-card keep row (active, stopped,
   unreadable) and the Policy row, and iterate with the operator until approved. No frontend
   code before approval.
2. `src/reaper/services/rewatch.py`: viewing clustering, gap statistics, the cycle and
   aliveness judgment, the TV replay check. Unit tests beside it.
3. The new observations: declare on `Facts`, populate in all four sites (rule 35 list above),
   state the thaw for stored rows that predate them.
4. The keep: built-in `KeepConfig` where `services/snapshot.py` assembles keeps, both lanes;
   the two policy body fields with their loader defaults; the policy API schema.
5. Frontend, matching the approved mockups: the Policy row (`policyMeta.ts`,
   `PolicyEditor.tsx`), the why-card keep copy (`WhyPanel.tsx`), the API types.
6. The stage 1 tests and doc edits listed above.
7. Gates per Verification, `verify` end-to-end, then PR, labels, squash-merge.

Stage 2, only after stage 1 has landed:

1. Mockups: the probability block in all three states and the threshold Policy row; approval
   first.
2. The curve fit in `services/rewatch.py`: candidate-set population, cutoff, buckets,
   `MIN_SAMPLES`, horizon withholding.
3. The explanation block: `engine/explanation.py` declaration, `snapshot._explain` writer,
   thaw for old rows.
4. The gate: new `GateId`, `build_gates` wiring, policy schema and body field, facts input.
5. Frontend, matching the approved mockups: the why-card block, the Policy row, the API types.
6. The stage 2 tests and doc edits listed above, including the `docs/SIGNALS.md` correction.
7. Gates, `verify` end-to-end, then PR, labels, squash-merge, and move this plan to
   `docs/history/` with a frozen banner.

## Verification

Per CONTRIBUTING gates, each run alone with its exit code read (rule 134): `uv run ruff
format` + checks, `uv run pytest`, frontend `npm run test` (plus `--disableConsoleIntercept`
spot check), `npm run build` via CI. Then the `verify` skill end-to-end on a real data dir:
run a scan, open a why panel on a known cycling title and confirm the keep row and its copy;
confirm the probability block shows a measured state on a well-sampled title and the withheld
state where thin. Both PRs squash-merge to `dev` with Conventional Commit titles and
`Kind/Feature,Priority/Low` labels inherited from #554.
