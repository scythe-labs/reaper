# Rewatch likelihood (#554): the plan

> **PLAN — approved, staged, nothing landed yet.** Written 2026-08-12; revised the same day
> after three independent verification passes ran out-of-sample backtests against a live
> library. The issue's interval-first premise did not survive measurement (see
> `docs/LEARNINGS.md`, *A regular-gap "rewatch cycle" does not exist here*); stage 1 is the
> formulation that did. Two stages, each its own PR; stage 1 ships before stage 2 starts.
> Kept current as work proceeds, and moved to `docs/history/` when both stages have landed or
> the work is abandoned.

## Context

Issue #554 asks Reaper to show, per title, how likely it is to be watched again, computed from
the operator's own watch history. The score today is unsigned deletion pressure, a different
question. The deleted `engine/calibration.py` (readable at `git show 163b6d8`) proved the
curve-fitting method but had no caller and answered per-population, not per-title. Two staged
pieces:

1. **Habitual-rewatch keep** (protection): a title watched again and again, and recently,
   replays at well above its dormancy-matched base rate. Wired as a keep discount, not a hard
   gate, because young watch history must not hard-block. The issue proposed detecting
   *regular intervals* instead; that formulation was backtested and refuted, and the why is
   recorded in `docs/LEARNINGS.md`.
2. **Rewatch probability**: "of titles in your library that looked like this one, what
   fraction got watched again." Context in the why card, plus one opt-in protective hold the
   operator can set as a percentage. It can never add deletion pressure.

Operator controls are simple knobs in the existing Policy row style, not rule authoring. Both
UI surfaces are mocked up and approved before frontend code (the `CLAUDE.md` mock-up rule,
`reaper-artifact` skill). Both stages are movies-only; TV is deferred behind its own
validation (its section below).

## What verification found (2026-08-12, read-only)

Three independent passes, each running out-of-sample backtests on a live library. Figures
never identify a server; the measured shapes live in `docs/LEARNINGS.md`, and the record here
is qualitative.

- **The cycle detector was refuted.** With two gaps, every spread statistic reduces to the
  same number and passes random play times at exactly the threshold rate. Titles passing the
  regularity test replayed no more often than matched titles failing it, and detected titles
  showed no phase structure at all. A positive control on the same harness found real signal,
  so the null is trusted.
- **Frequency plus recency is the signal that survived**: many qualified viewings plus a
  recent play replays at well above the dormancy-matched base rate, across hundreds of titles
  and three cutoffs, and it composes with the play filter below.
- **The play filter is load-bearing.** Unfiltered, over half of apparently cyclic titles owed
  their pattern to abandoned sub-50%-complete plays. Any play-derived count must count
  qualified plays.
- **The probability estimator held** (sound with changes): bucket rates are stable across
  cutoffs, calibrated out of time to a few points, and beat both a global constant and a
  shrinkage estimator. Survivorship bias was measured and is strictly in the over-protect
  direction, so the accepted-bias argument stands. Two changes were adopted: a monotonicity
  merge and a confidence-bound hold, both below.

## Stage 1: the habitual-rewatch keep (own PR)

### What counts as a play

A `watch_event` row qualifies as a play iff, in order:

1. `media_type` is `movie` or `episode`, never `track`;
2. when `watched_status` is not NULL: `watched_status >= 0.5` (it is quantized against the
   operator's own Tautulli watched threshold, so it is the calibrated field);
3. when `watched_status` is NULL: `percent_complete >= 50`;
4. when both are uninformative (`watched_status` NULL and `percent_complete == 0`): the play
   **counts**. Unknown resolves toward keeping.

This matches `season_scan.py`'s precedent (completed means `watched_status == 1` there; NULL
is "possibly watched" and fails protective). The filter is a shared helper in the new module,
used by every play-derived count this feature adds; nothing else in the app changes its play
semantics in this PR.

### Derivation

New module `src/reaper/services/rewatch.py` (successor to `calibration.py`, one derivation
for both stages, rule 104). Batch SQL over `watch_event` for the candidate key set, chunked
per rule 94; the existing `(rating_key, watched_at)` index covers it. **No schema change
anywhere**: no new `watch_event` column (that drops the mirror), no migration.

- **Viewings**: a movie's qualified plays (any user), sorted ascending; a play more than 7
  days after the previous play starts a new viewing. Equal timestamps share a viewing.
- **The keep condition**: `viewing_count >= 10` and last qualified play within 730 days.
  No median, no gaps, no spread, no periodicity. Constants live in the module with the
  backtest that set them cited; they are starting values from one heavy-rewatch library, not
  truths (a quieter library is untested; loosening to 8 viewings buys coverage at some
  precision, and that trade is written at the constant).

### Engine wiring

Two new observations on `Facts` (`engine/gates.py`), populated in every `Facts` site
(rule 35: `snapshot.build_facts`, `season_scan.build_season_facts`,
`engine/facts_codec.facts_from_dict` thaw, `preview._bare_facts`):

- `rewatch_viewings: Observation[int]` — qualified viewing count, all time.
- `rewatch_habit: Observation[bool]` — the keep condition above, decided by the builder.

Three-state semantics, exact:

| Situation | `rewatch_viewings` | `rewatch_habit` |
| --- | --- | --- |
| History read; condition met | Known n | Known True |
| History read; condition not met (few viewings, or stale) | Known n (0 included) | Known False |
| Watch history source failed (snapshot degrades anyway) | Unknown | Unknown |
| Title is watch-blind (#275 forget control) | Unknown | Unknown |
| Season lane, this release | Absent, with a comment | Absent, with a comment |
| Stored row predating the fields (codec thaw) | Unknown | Unknown |

Rule 93 throughout: a read failure is never `Absent`. `Known False` and `Absent` both take
zero discount; `Unknown` takes the full one.

The protection is a **built-in `KeepConfig`** appended where `snapshot.py` assembles keeps
for the movie lane. `evaluate_keep` today ramps numeric fields and has a flat membership arm
for `on_list`; add a third arm for a boolean field, mirroring the membership arm's shape:
Known True takes the full discount, Known False and Absent take zero (evaluated, honest
detail), Unknown takes the full discount with `evaluated=False`. The existing invariants
(a discount can only lower a score, and never un-protects a gated item) come free.

### Policy body, hashes, and the supply chain

Two fields on `PolicyBody` (`engine/policy.py`):

- `rewatch_keep_enabled: bool = True`
- `rewatch_keep_discount: int` with `ge=1, le=50`, default 20

A stored body lacking them thaws to the defaults (the keep direction); an explicit
`enabled=False` is the operator's choice and honored (rule 1's spirit). Classify both into
`_EVIDENCE_REPLAYABLE_FIELDS`: they are judging knobs over frozen Facts, exactly like
`graded_keeps`, and classifying them into the evidence hash instead would force a fresh scan
on every strength edit forever. The cost of the right classification is one transient window:
a simulator replay over a snapshot written before the upgrade thaws the observations
`Unknown`, so the preview takes the full discount (toward keeping, shown as "couldn't check")
until the first post-upgrade scan. `test_policy.py`'s drift guard forces the classification;
do not silence it another way. Adding body fields moves `policy_hash`, which voids a plan
approved before the upgrade and asks for a re-scan (rule 113); that is the documented,
accepted behavior.

Wire the fields through the whole supply chain in the same change (rule 64):
`api/schemas.py`, `frontend/src/api.ts`, the editor. Not added to the rule-authoring field
vocabulary; that is a follow-up only if asked for.

### Operator copy (draft; final strings come from the approved mockups)

- Why-card keep row, firing: "Watched 14 times, most recently 3 weeks ago. Titles watched
  this often keep getting watched."
- Why-card, condition not met: renders in the existing "didn't apply" group, no special copy.
- Why-card, Unknown: the existing "couldn't check" treatment.
- Policy row label: "Keep the titles you rewatch". Help: "A title watched again and again,
  and seen recently, tends to be watched once more. While that holds, its score is lowered."
- Strength control label: "How strongly it argues to keep".

### UI, docs, and tests

Keeps already render in `WhyPanel` (`KeepContributionOut`). Mock the why-card row (firing,
not-met, unreadable) and the Policy row first with `reaper-artifact`, iterate to approval,
then code. Rule 25 holds: backend and UI wire together.

`docs/STATUS.md` open-work row edited in place. Tests pin: the play filter (an abandoned play
does not count; both-fields-unreadable counts; the NULL `watched_status` arm); the viewing
clustering (equal timestamps, the 7-day boundary); condition-not-met discounts nothing; a
stale last play discounts nothing; Unknown takes the full discount; the discount can never
raise a score (extend the existing engine invariants); a stored policy body without the new
fields thaws to the defaults; the season lane's `Absent` is explicit.

## Stage 2: rewatch probability context (own PR)

### Fit

In `services/rewatch.py`, at scan time, movies only. Population is **exactly the movie
candidate set of the running scan** (what the scorer scores), restricted to items with a
known added date at or before cutoff = scan time minus 365 days. Items with an unknown added
date and no play history are withheld, never fitted. That satisfies the population trap by
construction (`docs/LEARNINGS.md`, *The population trap*); pin it with a test that fails if
the fit ever reads keys outside the candidate set.

- Per item at cutoff: dormancy = cutoff minus the last play at or before cutoff (any user,
  any completion), else cutoff minus the added date. Via the shared `engine/dormancy.py`
  derivation. Outcome = any play in the following 365 days.
- Buckets, half-open (lo, hi] days: 0-365, 365-548, 548-730, 730-1095, 1095-1825, 1825+.
- Point estimate: k/n per bucket, then **merge adjacent buckets that violate
  monotone-decreasing** (pool-adjacent-violators). A merged block's rate, counts, and
  dormancy range are the pooled ones, and the display sentence uses them. Without the merge,
  a threshold hold can protect a more dormant bucket while skipping a less dormant one.
- Floor: a (possibly merged) block with n < 30 displays no number and can never fire the
  hold. A block deeper than the mirror reaches is withheld until history grows into it
  (`history_sync.horizon` / `Facts.history_reach_days`).
- Refit at every scan. Never persist a fitted rate as a property of a title; year-over-year
  movement of a few points per bucket is normal.

Accepted bias, stated in the module docstring: titles deleted during the lookback year are
uncountable, which nudges rates up, the keep direction (rule 31's spirit). Measured, the bias
is real and strictly upward; do not attempt a ghost-corrected rate, because deletion dates
are unknowable here and the correction would push numbers in the deletion direction on an
assumption.

### The hold (opt-in, protect-only)

A new policy-authorable gate in the existing protection row style: "Keep anything likely to
be watched again," holding any title whose block satisfies n >= 30 and **the Wilson 95%
upper bound** of k/n at or above the operator's percentage. The upper bound rather than the
point rate, so a small library never loses protection to sampling noise; it converges to the
raw rate as n grows. It fires only on a measured block: a withheld block never blocks and
never condemns, and every other protection still stands, which the why card states plainly.
A low or missing number can never add pressure, in any configuration.

With six buckets the knob has only a handful of distinct settings and is effectively a
dormancy cutoff in the operator's own outcome units. So the row **echoes the consequence**
beside the input, recomputed from the current fit: "At 25%, this protects titles unwatched
under about 3 years, 2,475 of 3,192." Ships opt-in (default off) because it overlaps the
dormancy floor. Rule 38 holds: the gate ships wired to its facts in the same PR.

### Storage and display

Per-candidate context block added to the declared explanation shape
(`engine/explanation.py`) and written by `snapshot._explain`. No verdict input, no signal:
display only, plus the opt-in gate above. Old stored rows lack the key and read as nothing to
show (rule 104). `WhyPanel` renders three states, modeled on `watchReach.ts`:

- "Of 599 titles that had sat unwatched about this long, 207 were watched again within a
  year." (the block's pooled cohort and counts; never a fabricated decimal alone)
- "Too few titles like this to say."
- "Not enough watch history yet."

Mock the probability block (all three states) and the threshold Policy row first, iterate to
approval, then code.

### Docs and tests

`docs/SIGNALS.md`'s "the curve is borrowed, full stop" section is corrected: the display now
fits per-operator, the borrowed curve remains the scoring default. STATUS row edited;
LEARNINGS gains the fitted-vs-borrowed comparison, as shapes. Tests pin: the fit reads no key
outside the candidate set; the monotone merge fires on an inversion and is a no-op on a
monotone curve; a thin block yields no number; the hold fires only on a measured block and
uses the upper bound; unknown-added-date items are withheld; no configuration lets the
estimate raise a score or flip a verdict toward condemn.

## TV, deferred behind its own validation

Neither stage ships a TV answer, because none was validated. What is already known, recorded
for the follow-up:

- Episode plays cluster into show-level viewing periods at a 30-day window, which correctly
  bridges a weekly airing run into one period.
- **A period-based protection must separate rewatching from following.** The validated
  discriminator: a period *replays* when at least a quarter of its distinct episode
  identities (by `rating_key`, falling back to `(parent_rating_key, media_index)`) were
  already played in an earlier period, counting only periods holding at least two distinct
  episodes. On live data this split rewatch-driven from release-following shows cleanly, with
  every "following" verdict passing an independent new-content check.
- A TV keep or a TV probability fit ships only after the same out-of-sample backtest harness
  that refuted the movie cycle detector shows lift for the TV formulation, per
  `docs/SIGNALS.md`'s bar for any new signal.

Until then the season lane populates the stage 1 observations `Absent` with a comment
(rule 35), and no operator copy names a TV rewatch mechanism (rule 25).

## Out of scope

- #553 (returned-title regret) is separate work.
- Interval or seasonality detection in any form: refuted on real data, see
  `docs/LEARNINGS.md`. Re-opening it means clearing `docs/SIGNALS.md`'s lift bar first.
- Using the probability to add deletion pressure, ever. The only scoring surface it touches
  is the opt-in protective hold above.
- Exposing the new facts in the rule-authoring vocabulary. Simple knobs only, until asked.

## Execution order

An implementing agent works each stage top to bottom, inside that stage's single PR. The
repo's rule files load as the governed trees are touched and carry the fine-grained
conventions; this list is the sequence.

Stage 1:

1. Mockups: load the `reaper-artifact` skill, mock the why-card keep row (firing, not-met,
   unreadable) and the Policy row, and iterate with the operator until approved. No frontend
   code before approval.
2. `src/reaper/services/rewatch.py`: the play filter, viewing clustering, the keep condition.
   Unit tests beside it.
3. The two observations: declare on `Facts`, populate per the three-state table (all four
   sites), thaw as stated.
4. The keep: the boolean arm in `evaluate_keep`, the built-in `KeepConfig` in the movie
   lane's keep assembly, the two `PolicyBody` fields with their classification, the wire
   schema.
5. Frontend, matching the approved mockups: the Policy row (`policyMeta.ts`,
   `PolicyEditor.tsx`), the why-card keep copy (`WhyPanel.tsx`), the API types.
6. The stage 1 tests and doc edits listed above.
7. Gates per Verification, `verify` end-to-end, then PR, labels, squash-merge.

Stage 2, only after stage 1 has landed:

1. Mockups: the probability block in all three states and the threshold Policy row with its
   consequence echo; approval first.
2. The fit in `services/rewatch.py`: candidate-set population, cutoff, buckets, the monotone
   merge, the floor, horizon withholding.
3. The explanation block: `engine/explanation.py` declaration, `snapshot._explain` writer,
   thaw for old rows.
4. The gate: new `GateId`, `build_gates` wiring, the Wilson-bound comparison, policy schema
   and body field, facts input.
5. Frontend, matching the approved mockups: the why-card block, the Policy row with the
   echo, the API types.
6. The stage 2 tests and doc edits listed above, including the `docs/SIGNALS.md` correction.
7. Gates, `verify` end-to-end, then PR, labels, squash-merge, and move this plan to
   `docs/history/` with a frozen banner.

## Verification

Per CONTRIBUTING gates, each run alone with its exit code read (rule 134): `uv run ruff
format` + checks, `uv run pytest`, frontend `npm run test` (plus `--disableConsoleIntercept`
spot check), `npm run build` via CI. Then the `verify` skill end-to-end on a real data dir:
run a scan, open a why panel on a known heavily-rewatched title and confirm the keep row and
its copy; confirm the probability block shows a measured state on a well-sampled title and
the withheld state where thin. Both PRs squash-merge to `dev` with Conventional Commit titles
and `Kind/Feature,Priority/Low` labels inherited from #554.
