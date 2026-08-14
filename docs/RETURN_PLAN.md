# A title that came back (#553): the plan

> **Live.** Nothing has landed. Written 2026-08-14, after a design pass that replaced the
> issue's proposed mechanism with a different one, and a read-only measurement pass against a
> real library. Update this file as stages land; freeze it into `docs/history/` when #553
> closes.

## Context

Issue #553 asks Reaper to be slower to condemn a title it removed before, once that title
reappears. A return is the clearest evidence Reaper can get that it was wrong: somebody went
and fetched it again.

It is the successor to `engine/backtest.py`, deleted as unreachable in #552 and readable at
`163b6d8`. `STATUS.md`'s M3c row records that drop. Unlike a backtest it needs no rewind, no
historical rating archive and no population model, so it works from a fresh install and
improves on its own.

## What the design pass changed

The issue proposed reading Reaper's own action journal and matching returns on external ids.
Both halves were replaced. The reasoning below is the part a future reader needs most, because
the discarded design is the one that reads as obvious.

**The journal is the wrong source.** It only knows about deletions *Reaper* performed. An
operator who removes a title by hand and later re-fetches it has produced exactly the same
evidence, and the journal cannot see it. Worse, the journal is blind to the most common return
Reaper itself will ever cause: a season prune unmonitors the season and deletes its episode
files while the Sonarr series row survives, so nothing about the *arr entry changes.

**The \*arr's own arrival date is the wrong clock**, for the same reason. `Radarr.added` marks
when the movie row was created, so it never moves for an operator who deletes the file and
leaves the entry in place. And a file date does move on a quality upgrade, which is routine, so
it would fire on every 1080p to 4K replacement.

**Plex is the witness.** A file that leaves the library and comes back gets a new Plex rating
key, and this repository already says so in two places, one per lane: `snapshot.py:341` on
movies ("a re-added file carries a fresh added_at while its earlier plays stay filed under the
key it no longer holds") and `season_scan.py:519` on seasons ("a re-added season carries a new
one"). Both lanes already carry the field. `season_scan.py:166` holds the season's own
`plex_rating_key`, explicitly distinct from the show's, and `PlexSeason` carries a per-season
`added_at`. A rating key also survives an in-place quality upgrade, so Plex is the more precise
witness as well as the one the operator's users actually see.

The journal keeps one job: saying which sentence the why panel gets.

## The rule

> A **return** is a title present in the library now under a Plex rating key that Reaper has
> never recorded for it, when every key Reaper has recorded for it is gone from the Plex index,
> and it was gone for a real span of time that Reaper was awake for.

Four conditions. Every one of them exists because of a measurement, not a worry.

1. **A key never recorded before** rules out the ordinary state of a title that has sat in one
   place.
2. **Every recorded key is gone from the index** rules out a title listed more than once, where
   the bind moved between two listings that both still exist. Reaper already builds a full
   `PlexIndex` every scan, so this is a set lookup and costs nothing.
3. **At least `cooling_off_days` between the last sighting and the new copy's Plex `added_at`.**
   Default 7 days, operator-set.
4. **At least two scans ran inside that window.** A count of `snapshot` rows between the two
   timestamps. No new state, no per-item bookkeeping.

### Why the cooling-off period, and why it needs both halves

A file replaced in place is gone and back within minutes. So is a title an operator deleted by
accident and put straight back. Neither is a regret, and both would otherwise read as one.

Measured: every rating-key change observed on the validation library happened within **2.5 to 30
hours**, and that figure is an upper bound on the true absence. A cooling-off of three days
would have rejected all of it. Seven leaves a margin of more than five times over.

**A clock alone is not enough, and the same library shows why.** `last_seen_at` is the last time
Reaper *looked*, not the moment the title left, so the measured gap overestimates the true
absence by up to one scan interval. That install averages a 17-hour interval but contains a
**202-hour** one. A file upgraded during a pause like that would read as an eight-day absence
against a seven-day bar.

Condition 4 is what closes it, and it closes the opposite failure too. Requiring that Reaper
actually *ran* while the title was missing makes the rule cadence-robust in both directions: an
operator scanning nightly satisfies it easily and leans on the clock, while one scanning monthly
trivially satisfies the clock and leans on the scan count. Neither cadence can be tuned into a
false return.

**This also retires a claim that was doing work it had not earned.** An earlier draft argued
Plex was the better witness partly because a rating key survives an in-place quality upgrade.
That is consistent with the measurement, roughly one entry in a thousand changing key over 24
days, but it was never verified against Plex's own behavior and it should not have been load
bearing. With a cooling-off period the feature is correct whether or not an upgrade reissues a
key, which is worth more than the claim being true.

## What was measured, 2026-08-14

Read-only, against one real library, 35 snapshots spanning 24 days. Ratios only; the shapes go
to `docs/LEARNINGS.md`. Four findings, in the order they change the plan.

**There are no confirmed regrets to fit against, anywhere.** The validation install has nine
planned runs and zero executions: no step has ever been sent, and nothing has ever been
deleted. So no weight can be fitted from real regret data, on this library or any other we can
reach. That is why the mechanism is a hold with a clock and not a fitted discount, and the plan
does not pretend otherwise.

**Accidental returns do not happen.** Across roughly 3,500 movies and 2,500 seasons, nothing
disappeared from the library and came back over the whole window. One movie and two seasons
left and stayed gone. The library only grows. This is a floor, not a proof: one library, 24
days, an operator who adds rather than removes.

**The detector's noise is real, and all of it is fast.** Roughly one movie entry in a thousand
(3 in ~3,500) changed its bound Plex rating key over the window while keeping its *arr entry,
and no season did. Every one of those changes completed within **2.5 to 30 hours**, measured as
the span between the last scan showing the old key and the first showing the new one, which is
an upper bound on the true absence.

That shape is what the cooling-off period is built on. Mechanical churn, a file replaced in
place or a mistake put straight back, resolves in hours. A regret takes as long as it takes an
operator to notice. The two populations do not overlap anywhere near a multi-day bar, so
condition 3 removes the entire measured noise floor rather than trading it off against
sensitivity.

**A ledger keyed on an external id alone would thrash.** About 21 TMDb ids on the measured
library carry **two** Radarr entries each, one per copy, each binding a different Plex listing.
Keyed on the id alone, the ledger would read the second copy's key as a change on every scan
and hold both copies forever. This is what forces the ledger to hold a **set** of keys per id,
and it is what forces the "old keys are gone from the index" half of the rule. It was found by
measurement, not by reading code, and it would have shipped.

## Why a ledger, and why external ids

The ledger is one new table, and it is the only new schema this feature needs.

```
library_seen
  id_key        TEXT PK      "tmdb:12345", "tvdb:678", "tvdb:678:s3"
  rating_keys   TEXT         JSON array: every Plex key ever recorded for this id
  first_seen_at TIMESTAMP
  last_seen_at  TIMESTAMP
```

Keyed on external id rather than `media_key`. A `media_key` is
`{radarr|sonarr}:{instance}:{arr_id}`, and a movie delete removes Radarr's row, so a re-add gets
a new internal id and the old `media_key` is retired. That is true whether Reaper or the
operator did the deleting.
`engine/identity.ExternalIds` is the existing id machinery and supplies the priority ladder.

`WatchHighWater` is the precedent for every property of this table: outside the snapshot
lifecycle, keyed on something stable rather than a Plex rating key, upserted once per scan,
monotonic, never pruned. Roughly one row per title, so a large library is tens of thousands of
rows.

**The ledger only ever writes on a confident Plex bind.** No bind, no write. This is the whole
answer to a broken scan:

| What went wrong | What the ledger does |
|---|---|
| Plex unreadable | nothing written, nothing compared |
| The item did not bind, or the bind abstained or conflicted | nothing written |
| Radarr or Sonarr partly down | the item is not in the scan at all |
| A whole instance missing | its items are not in the scan at all |

Absence is never an input. A return is only ever detected by comparing two keys Reaper actually
read, so missing data cannot manufacture one. It can only delay one to the next scan, which is
the safe direction.

## The library-rebuild exposure, and where it goes

A Plex library rebuilt from scratch reissues every rating key at once, so conditions 1 and 2
are satisfied for every title in the library in the same scan.

**Conditions 3 and 4 are what stop it, and they stop the ordinary case outright.** A rebuild
gives every item a fresh `added_at` of *now*. So the gap from the last sighting is one scan
interval, far under a seven-day bar, and no scan ran while the library was away. An operator who
rebuilds in an afternoon trips nothing.

**What still gets through is a slow rebuild**: one that leaves the library unreadable to Plex for
longer than the cooling-off period while Reaper keeps scanning. A staged migration, or a storage
outage. Reaper sees the \*arr entries unbound for days, writes nothing because there is no bind,
and then sees the whole library return at once with new keys and a genuine multi-day gap behind
it.

That residue is what **#809** covers, and it does not block this work. Three reasons.

It **fails safe**. The outcome is mass protection, which deletes nothing. Clearing the ledger
repairs it, at the cost of the feature's memory and nothing else.

It is **not a new hole**. A rebuild's other effects already fail safe on their own: an item with
prior plays goes `Unknown` rather than "never watched", because `WatchHighWater` catches a
watcher count that fell, and a reset `added_at` *lowers* dormancy and therefore lowers deletion
pressure.

It is **broader than #553**. A scan-level check on how much of the library changed identity at
once belongs beside `history_sync._check_regression`, which is the only guard of that shape
today and is Tautulli-row-count only. Building it inside a gate would put a library-wide safety
check somewhere nobody would look for it.

### Ordering

Neither issue blocks the other, and whichever lands second is the cheaper one.

If **#809 lands first**, this feature needs nothing extra: the general guard already refuses to
judge a scan where identity moved wholesale.

If **#553 lands first**, it should carry its own population cap, refusing to fire *this* hold
when an implausible share of the library looks returned in one scan. That is a few lines over
evidence the gather step already holds, and it is a different altitude from #809: a feature
declining to act on evidence it does not believe, rather than a scan declining to judge at all.
It stays after #809 lands, because it is about this feature's own inputs.

## One stage, not two

The halves cannot ship apart. Rule 25 forbids landing schema for an unwired feature, and a gate
the operator cannot turn off is not shippable either. The gate simply does not fire until the
ledger has history, which is a runtime property and not an unwired feature.

The control turned out to be free, which is what collapsed an expected second stage. The days /
weeks / months / years picker already exists as `QuantityInput` with `TIME_UNITS`
(`frontend/src/components/QuantityInput.tsx:30`), and it is already what every comparable
duration in the editor uses: dormancy days, the popularity `window_days`, `rewatch_recent_days`
and the grace period. So this is one `units={TIME_UNITS}` prop on the existing `.qty` control,
not a new component, and there is no rule 72 sweep to decide: the siblings already have it.

Two frontend rules bind the control and both point the same way. Rule 40 makes `QuantityInput`
the one shared number-with-a-unit control, and rule 41 forbids turning an open list like units
into a `Segmented`, so the unit stays a `<select>`.

**Two knobs, both durations, both on that control.**

| Knob | Default | What it decides |
|---|---|---|
| How long the hold lasts | 1.5 years (548 days) | how long a returned title resists condemning |
| How long counts as gone | 7 days | how long an absence must be to count as a return |

Both store plain days, as every duration in the policy body does. `GateConfig` already carries
two numeric fields, so the shape exists. The second knob's default is set by measurement rather
than taste: the observed churn tops out around 30 hours, so seven days clears it more than five
times over.

Backend:

| Piece | Where |
|---|---|
| `library_seen` table | `src/reaper/db/models.py`, one additive Alembic revision |
| Upsert on bind, read the set | new `src/reaper/services/library_seen.py` |
| `GateId.RETURNED` + the gate class | `src/reaper/engine/gates.py` |
| `Facts.returned_at`, `Facts.returned_by_reaper` | `src/reaper/engine/gates.py`, as `Observation` |
| Register the gate type | `src/reaper/services/scan_runner.py` `GATE_TYPES` |
| Allow it in a policy body | `gates.POLICY_AUTHORABLE_GATES` |
| Row for stored bodies predating it | `model_validator` on `PolicyBody`, per `_rewatch_odds_row` |
| Populate the facts on the movie lane | `snapshot.build_facts` |
| Populate the facts on the TV lane | `season_scan.build_season_facts` (rule 35) |
| The "Reaper removed it" label | the existing `ActionStep → ReapRun → Candidate` join |
| Why-panel block | `snapshot._explain` + `engine/explanation.py` |
| `SCORER_VERSION` bump | `src/reaper/engine/policy.py` |

`facts_codec.py` needs no change: it derives its field list from `dataclasses.fields(Facts)` and
raises at import time on a field it cannot handle.

The `SCORER_VERSION` bump is not optional. It is at **4** (`policy.py:76`) and goes to 5. A
stored policy body would otherwise hash identically before and after while scoring differently,
and rule 113 wants a plan approved under the old scorer voided rather than executed under the
new one. `SCHEMA_VERSION` stays at 3: `PolicyBody` is stored as JSON, so a new field with a
default needs no shape bump and no `policy_migrations.py` shim.

The one Alembic revision chains off the current head, **`f7a8b9c0d1e2`**
(`alembic/versions/20260810_1200_index_action_step_run_id.py`). A new table is additive by
definition, so no existing row is touched and no tester rebuilds.

Frontend:

| Piece | Where |
|---|---|
| The policy row and its help copy | `frontend/src/components/PolicyEditor.tsx` |
| `GATE_META` entry, label + help + `unit: "days"` | `frontend/src/components/policyMeta.ts` |
| `GateId` union gains the new id | `frontend/src/components/policyMeta.ts` |
| Both duration controls | `QuantityInput`, `units={TIME_UNITS}`, `min={1}` |
| Typed mirror of the new policy fields | `frontend/src/api.ts` |
| Why-panel sentence with its countdown | `frontend/src/components/WhyPanel.tsx` |
| The queue chip, outlined, with its countdown | `frontend/src/components/StatusChip.tsx` |
| One chip class | the chip's stylesheet |

**The controls need no CSS. The chip does.** Both durations render into the existing `.qty`
shape (`frontend/src/styles/31-qty.css`), which `styles-control-standard.test.ts` explicitly
exempts from the shared-control walk because the wrapper carries the border, so a hand-rolled
box would fail that walk. The chip is the opposite case: the `.chip` family has solid variants
for the owner's decisions and outlined ones for Reaper's, and this needs an outlined variant
that does not exist yet.

The countdown also has to reach the browser. The stored explanation carries the numbers a panel
renders, so the hold's end date belongs in the why-record beside the sentence rather than being
recomputed in the browser from a policy value the card does not hold.

Per the `CLAUDE.md` mock-up rule the frontend work still opens with a rendered HTML artifact in
Reaper's look and feel (the `reaper-artifact` skill), approved before any frontend file is
edited. It is a cheap round here, because the row reuses a control and a layout that already
exist.

## The gates this has to clear

Named now, because three of them fail on a new gate id by construction and are cheaper to plan
for than to discover.

- **`tests/test_api_type_mirror.py::TestEveryGateIdHasOperatorCopy`** diffs `policyMeta.ts`'s
  `GateId` union against `engine.gates.GateId` **in both directions**, and separately checks
  that the browser marks exactly the ids no policy row may carry against
  `POLICY_AUTHORABLE_GATES`. A backend-only gate fails this immediately.
- **`TestTheTwoCopiesAgree` / `TestTheTwoCopiesAgreeOnTypes`** diff every policy field name and
  type between `api.ts` and the Pydantic models. Both new duration fields have to land on both
  sides with matching types.
- **`PolicyEditor.test.tsx`'s `WARNING_ANCHORS`** is pinned at exactly nine anchors. If the new
  row carries a policy warning it needs a claiming anchor and that count goes to ten; if it does
  not, the warning falls to the bottom stack. Rule 42, and it has to be decided deliberately
  rather than discovered.
- **`test_engine_derivations`** keeps `_explain`'s output and `engine/explanation.py`'s declared
  model in sync, so the why-panel block and its wire type land together.
- **Both `Facts` builders**, per rule 35. A field populated on the movie lane and forgotten on
  the TV lane is the failure this rule exists for, and this feature has a TV lane that works.

## What the operator sees

Two sentences, one hold. Both are `rule 21` register: what happened, what it means for their
files.

- Reaper removed it, and the journal join confirms it: **"you removed this before and it came
  back."** This is the confirmed regret, and it is the only mechanism Reaper has that can tell
  an operator their settings are too aggressive using their own library.
- It left some other way: **"this left your library and came back."**

Same hold strength for both. The distinction is the sentence, not the weight, because splitting
them means a second knob for a difference nobody has measured.

### It has to say how long, and it has to say it where the card is closed

A hold measured in **months** is unlike every other protection Reaper has. The others are
re-decided from scratch every scan, so "why is this kept" is answered by conditions the operator
can go and look at. This one is a countdown against a date they cannot see, on evidence from a
scan that may be a year old. Left unstated, the honest operator question is not "why" but "is it
stuck forever."

So the remaining time rides on both surfaces, not just in the card:

- **The gate's `detail`, in the why card.** House style already carries the measurement inside
  the detail string (`gates.py`: "watched here: 3 people in the last 90 days", "titles like this
  keep getting watched: 12 of 40 within a year"). So: *"you removed this before and it came
  back, 412 days left."*
- **A chip on the queue row**, so it reads without opening anything: *"Came back, 412 days
  left."*

**Both patterns already exist**, built for the timed hand spare, and this reuses them rather
than inventing a surface. `StatusChip` already turns "will be kept" into a countdown ("Spared by
hand, 27 days left"), and `WhyPanel` already writes the matching sentence.

One thing genuinely is new. The `.chip` family is currently the **owner's** vocabulary, where a
solid fill means the owner decided and an outline means Reaper did. This hold is Reaper's, so it
takes an outlined variant, and that is a class the family does not have yet.

**Which protections get a chip: the ones with an expiry, and no others.** This is the line that
keeps it from becoming a sweep across every gate. "Someone is watching it right now" and "well
rated" are re-decided next scan and have nothing to count down, so a chip would add noise and no
information. A hand spare has an expiry and already has one. This has an expiry. That is the
whole rule, and it is worth writing into the change so the next protection is not argued about
from scratch.

**The countdown must not promise more than it delivers**, and the repository has already been
burned here: `WhyPanel.test.tsx` pins the case where a spare kept *forever* was told "10 days
left, then Reaper judges it again", which Reaper does not do. Here the sentence is true, because
the hold does expire and the title is then judged normally. It has to keep being true if the
mechanism is ever changed.

A title with no external id cannot be tracked at all. It resolves `Unknown`, gets no protection,
and that is a stated limitation rather than a bug: it is the same reach every id-matched feature
in the tree already has.

## Properties worth knowing before building

**It is silent on a fresh install, and stays silent for a while.** An empty ledger gives nothing
to compare against, so no title can look returned. It starts working only once Reaper has
watched a library long enough to have seen the thing that later leaves. That is the issue's
"improves on its own", and it means there is no day-one demonstration.

**It under-reports by construction.** Some operators will shrug and never re-fetch. So any
non-zero count is a floor on real regrets, never an estimate of them.

**Clearing the ledger is the repair for any wrong state**, including a rebuild that got through.
It costs the feature its memory and nothing else.

## Validation

The parts that can be validated on real data, and the parts that cannot, stated separately so
neither borrows the other's credibility.

**Can be validated now.** The detector's noise floor, by replaying the rule over the existing
snapshot history: for each stable `media_key`, did its bound Plex rating key change, and was the
old key still present. That measurement is what produced the two ratios above, and it re-runs
against any install with snapshot history.

**Cannot be validated now.** The hold's usefulness, the default window, and the rate of genuine
regrets. All three need an install that has actually deleted something and had it come back.
Nothing we can reach has. The 1.5-year default is therefore a judgment call, not a fit, and it
is written here as one so a later measurement can replace it without archaeology.

**Ships with.** A `LEARNINGS.md` entry carrying the measured ratios and the duplicate-copy
finding, including the negative result that no accidental return occurred in the window.
