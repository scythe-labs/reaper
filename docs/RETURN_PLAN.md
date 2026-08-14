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
> never recorded for it, when every key Reaper has recorded for it is gone from the Plex index.

Both halves are load-bearing, and the second half exists because of a measurement (below).

- **Never recorded before** rules out the ordinary state of a title that has sat in one place.
- **The old keys are gone** rules out a title listed more than once, where the bind moved
  between two listings that both still exist. Reaper already builds a full `PlexIndex` every
  scan, so this is a set lookup and costs nothing.

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

**The naive detector has a small but non-zero noise floor.** Roughly one movie entry in a
thousand (3 in ~3,500) changed its bound Plex rating key over the window while keeping its
*arr entry, and no season did. Some of those are likely the exact case the design is meant to
catch, an operator deleting a file and re-fetching it, and the rest is Plex churn. Either way
the failure direction is protective, so this is a bounded, fail-safe cost worth stating in
advance: expect a low single-digit percentage of a library to accumulate a hold over the
default window.

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

Both halves of the rule read Plex. A Plex library rebuilt from scratch reissues every rating
key at once, so every title in the library would satisfy both halves in the same scan.

That is not this feature's problem to solve, and it is filed as **#809**. Two reasons.

It is **not a new hole**. A rebuild's other effects already fail safe: an item with prior plays
goes `Unknown` rather than "never watched", because `WatchHighWater` catches a watcher count
that fell, and a reset `added_at` *lowers* dormancy and therefore lowers deletion pressure.

It is **broader than #553**. A scan-level check on how much of the library changed identity at
once belongs beside `history_sync._check_regression`, which is the only guard of that shape
today and is Tautulli-row-count only. Building it inside a gate would put a library-wide safety
check somewhere nobody would look for it.

This feature's dependency on #809 is one line: until that guard exists, a rebuild produces mass
protection, which deletes nothing and is repaired by clearing the ledger.

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

Backend:

| Piece | Where |
|---|---|
| `library_seen` table | `src/reaper/db/models.py`, one additive Alembic revision |
| Upsert on bind, read the set | new `src/reaper/services/library_seen.py` |
| `GateId.RETURNED` + the gate class | `src/reaper/engine/gates.py` |
| `Facts.returned_at`, `Facts.returned_by_reaper` | `src/reaper/engine/gates.py`, as `Observation` |
| Register the gate type | `src/reaper/services/scan_runner.py` `GATE_TYPES` |
| Allow it in a policy body | `gates.POLICY_AUTHORABLE_GATES` |
| Row for stored bodies predating it | a `model_validator` on `PolicyBody`, per `_rewatch_odds_row` |
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
| The hold-length control | `QuantityInput`, `units={TIME_UNITS}`, `min={1}` |
| Typed mirror of the new policy field | `frontend/src/api.ts` |
| Why-panel sentence | `frontend/src/components/WhyPanel.tsx` |

No CSS. The control renders into the existing `.qty` shape
(`frontend/src/styles/31-qty.css`), which `styles-control-standard.test.ts` explicitly exempts
from the shared-control walk because the wrapper carries the border. A hand-rolled box would
fail that walk, which is another reason not to build one.

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
  type between `api.ts` and the Pydantic models. The new hold-length field has to land on both
  sides with a matching type.
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

- Reaper removed it, and the journal join confirms it: **"You removed this before and it came
  back."** This is the confirmed regret, and it is the only mechanism Reaper has that can tell
  an operator their settings are too aggressive using their own library.
- It left some other way: **"This left your library and came back."**

Same hold strength for both. The distinction is the sentence, not the weight, because splitting
them means a second knob for a difference nobody has measured.

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
