# Reaper — current state

> **What is true right now.** This file is **edited in place, never appended to.** When a
> line stops being true, change that line; when a milestone lands, change its row. It stays
> short on purpose: a small file gets updated, a long one gets skipped. `tests/test_repo_hygiene.py`
> keeps it under its line budget.
>
> Everything this file is *not*: the story of how we got here is `docs/history/`, the measured
> findings are `docs/LEARNINGS.md`, and the rules for working on it are `CLAUDE.md` plus
> `.claude/rules/`.

Last verified against the code: 2026-07-26.

## Milestones

| Milestone | State |
|---|---|
| **M0** Skeleton — uv, ruff, mypy strict, Alembic, Docker, CI | ✅ done |
| **M1** Auth + clients — Plex OAuth + owner check, Tautulli, Sonarr, Radarr, Seerr | ✅ done — session gate + CSRF in front of the whole API |
| **M2a** IMDb ratings dataset | ✅ done |
| **M2b** Curated lists (IMDb Top 250, *arr tags, Plex collections) | ✅ done |
| **M3a** Scoring engine — gates, signals, observations | ✅ done |
| **M3b** Policy persistence — immutable rows, hash, caps, autonomy grants | 🟡 rows/hash/caps done; **the autonomy-grant flow is unwired** — no route can create a grant |
| **M3c** Backtest — replay against the operator's own watch history | 🟡 engine complete and tested (`engine/backtest.py`), **not reachable**: no route, CLI or UI calls it. Operator copy must not reference it until it ships (rule 25) |
| **M3d** Field registry + authorable protect rules | ✅ done |
| **M3e** Snapshot pipeline + REST API + polled progress | ✅ done |
| **M3f** Signal quality — lift metric, size removed, dormancy gate | 🟡 size removed and the dormancy gate are wired; the **lift metric is backtest-only** (`backtest.BacktestResult.lift`) and unreachable until M3c ships |
| **M3g** Calibration — rewatch prior derived from the operator's own history | 🟡 `engine/calibration.derive` is complete and tested, **not reachable**: no caller anywhere in `src/`, not even the backtest, which imports only `RewatchPrior` and `NotCalibratedError` and takes its prior as an injected argument. Every prior in use is the hardcoded `backtest.FALLBACK_REWATCH_PRIOR` |
| **M4** React SPA — review queue, why-panel, policy editor, live simulator | ✅ done |
| **M5** The reap loop — journal, planner, executor, canary, caps | ✅ done — the live send is wired (`executor._send_for_real`, `POST /api/runs/{id}/execute`), armed from the UI and phrase-gated |
| **M6** Season pruning | ✅ done — read-only scan through live execute (`executor._send_season`), unmonitor verified before any file is removed |
| **M7a** Grace lifecycle — the notice countdown (DB-only) | ✅ done — a countdown the household sees, not a hold on the file; see the Delete mode row below |
| **M7b** Leaving Soon label + Discord | ✅ done — reconcile, notifier, and the live label write (gated like a delete by default) |
| **M8** Profiles + scheduler | ✅ done |
| **Whitelist** — manual "spare this file", scan + planner + grace | ✅ done, including two-level (show/season) spares with expiry |
| **Scales** — per-requester cards over the last scan | ✅ done — joins Seerr requests to the latest snapshot so it can never disagree with Review |
| **Operator console** — service config, first-run setup, schedule, safety, review | ✅ done — the whole tool is configurable from the browser |

## Open work

1. **The season growth interlock is desensitized.** *(Deletion path — the sharpest open item.)*
   Season sizes come from the Sonarr season *folder* statistic while the executor re-reads
   summed episode *files*, so the frozen and live sides of the interlock measure different
   quantities; the folder is the larger number, so a real growth reads as a shrink.
   `executor.py`'s `_SEASON_COMPARABLE` comment states this outright. `SizeSource.SONARR_FILES`
   exists and is written by nothing in `src/`; preferring it at scan time is the repair.
   Tracked as Stage 5 in `docs/SIZE_TRUTH_PLAN.md`.
2. **The deletion-path review is closed out.** A seven-group adversarial pass on 2026-07-26
   raised 33 candidates; 14 survived verification and 19 were refuted. Re-verifying the
   survivors before fixing refuted 3 more (#69's two paging sites, and `UnmanagedGate` as a
   *safety* finding) and turned up one they had missed, `clients/seerr.py`'s short request
   walk, which did convert a partial read into a protection-withdrawing `Known(False)`. All
   fixed; issues #60–#69 closed. Refuted candidates live in
   `.claude/skills/reaper-review/references/refuted.md` so the next pass does not re-raise
   them; the run artifact is `.claude/review-findings/` (gitignored).

   `UnmanagedGate` was retired under rule 38/117 (it shipped enabled by default and could not
   fire). `PolicyBody.RETIRED_GATES` drops it from a stored body on load, which is what keeps
   existing installs scanning — every one of the 12 policy rows on the test server named it,
   and `build_gates` refuses a gate it cannot build. The set covers `OTHERS_WATCHING` too,
   retired earlier and missed by the first version: it never shipped in a default policy, but
   the save boundary accepts any `GateId`, and a body carrying one had no self-heal.
   `tests/test_policy.py` now pins the set against every id `build_gates` cannot construct, so
   the next retirement cannot forget it. The save boundary was the wider half of the same
   hole: `GateSettingIn.gate` took any `GateId`, including the two the engine emits with no
   policy row behind them, so a hand-crafted save could store a gate no scan could build.
   `POLICY_AUTHORABLE_GATES` (in `engine/gates.py`, pinned against `GATE_TYPES`) is what the
   boundary now checks, and a wire-schema refusal reaches the operator without pydantic's
   `Value error,` prefix. Retiring a gate moves `scoring_hash` and
   `evidence_hash` as well as `policy_hash`, so the first Policy page after an upgrade shows
   the simulator's "needs a fresh scan" state with no numbers; that notice now states the
   condition instead of telling the operator they changed something.
   `GateId.UNMANAGED` and the four surfaces
   that decode a stored explanation (`STRUCTURAL_GATES`, the chip phrase, the why-panel line,
   and `WhyPanel.tsx`'s `CHECK_COPY` entry for the gate's blocked branch) stay. `Facts.is_managed` stays too: it is a true observation and the evidence any re-wiring
   would need, which is a Plex-first scan path, not a change to the gate.
3. **The autonomy-grant flow (M3b).** Rows, hash and caps exist; nothing can create a grant.
4. **The backtest surface (M3c), the lift metric inside it (M3f), and the calibration prior
   beside it (M3g).** All three engines are complete and tested; none is reachable. Nothing in
   `src/` imports `engine.backtest`, so `BacktestResult.lift` is unreachable too. The backtest
   needs `POST /api/policy/backtest` plus a minimal UI. `calibration.derive` has no caller
   anywhere in `src/` — **wiring the backtest would not by itself give it one**, since
   `backtest.run` takes its prior as an injected argument and never calls `derive`; the new
   route must call it and pass the result in. The backtest also models grace as a delay before
   deletion, which production does not do, so its `rescued` count is a best case; the field says
   so and whoever wires it must fix or label it. Until they ship, the live simulator is the
   threshold-tuning surface, and no operator copy may name the backtest or promise a prior
   fitted to their own history.
5. **Size-truth leftovers** (`docs/SIZE_TRUTH_PLAN.md`): a real-data pass reading
   `scan.size_source_tally` recorded as ratios in `LEARNINGS.md` (Stage 4, and it gates Stage
   6); `"size_bytes"` added to `DEGRADABLE` in `tests/_policy_lab.py`; and the test-only
   `snapshot.candidates()` deleted, which has no caller in `src/` and is a standing rule 38
   violation.

## Decisions locked

| Decision | Choice |
|---|---|
| Condemn logic | **Flat AND** of typed conditions. No OR, no nesting, no NOT. |
| Protections | **Gates with no CONDEMN constructor** — structurally cannot delete |
| Protect authoring | **Catalog + user-authored protect rules** (worst case is nothing deletes) |
| Signals | **Unsigned**, fixed denominator including unknown weights |
| Observations | **Known / Absent / Unknown** — never conflated |
| What a hand reap may overrule | **A typed per-result flag, never the wording.** A blocked gate holds a hand reap unless its gate is in `verdict.DEFERRABLE_BLOCK_GATES` *and* the result carries `GateResult.defers_to_owner` — two independent conditions, both defaulting to hold. Only `season_scan.guard_result` sets it, and only when the comparison behind the block was one Reaper could actually make; the same conflict raised because a kept season could not be read is a plumbing failure and holds (#84), as is one raised because the watch mirror does not reach back to when a season arrived (#94) — that count is a lower bound, so there is nothing for the operator to settle. One season carries one conflict per kept season, so a single refusal holds the whole block and supplies its message — reading only the first let a readable comparison mask a refused one and released the reap anyway. It used to read "came back with a number", which a count off a truncated mirror satisfies while settling nothing; #94 closed that by giving the detector the reach and the flag a third refused shape (`PruneConflict.shortfall`). The flag replaces a `detail.startswith("could not check")` test that never matched the one message it existed for, so a hand reap removed a season whose comparison Reaper had refused to make. Stored explanations carry the flag; a row frozen before it does not defer, so the reap is held until a later scan replaces that row |
| Watch-history reach | **Every reader that goes through `Facts`** answers only for a span its history covers (`Facts.history_reach_days`, `fields.reach_shortfall`) — the popularity gate, the operator's own protect and removal rules, the graded keeps, and the `FEW_WATCHERS` signal. **#94 is closed**, and it was the last reader that did not: the keep-conflict detector compared two truncated counts and silently stopped flagging, reading the mirror through a local variable rather than a fact, which is how the first sweep missed it. It now takes each season's shortfall beside its count (`plan_series_prune(shortfall_by_season=...)`, from the shared `gates.lifetime_shortfall`) and raises the conflict wherever more history could overturn the outcome — the pruned count losing to a bound it may yet clear, or winning against one that may yet rise. An outcome the bound already earns still stands, so a season nobody watched over a mirror covering its whole life still clears. **The reach of that is wide, and it is the intended cost rather than a side effect**: a season the mirror does *not* cover conflicts against every kept season whatever either count says, because more history can always lift a lower bound above anything. So wherever the watch history is shallower than the library is old, every prunable season of an affected show is held *and* refuses a hand reap, and TV season pruning is inert until the mirror catches up — the alternative being decisions taken on two numbers Reaper knows are wrong. `_detect_conflicts` shipped claiming in writing that it did *not* degenerate this way; it does, the claim is gone, and `test_a_short_mirror_holds_every_prunable_season_of_an_old_show` pins it, because the mutation that makes the degeneration total passed all 2626 tests. **#95 is closed**: the mid-binge hold now consults the reach (`season_pruning.progress_is_establishable`, taken by `plan_series_prune` as `progress_established`). `in_progress_hold_days` is the span that guard *claims to cover*, not a bound on the mirror, so where the reach does not span it — 0 included, an unbounded claim no finite mirror supports — the viewer set is **un-establishable rather than empty** and every season on disk is held ("your watch history is too short to tell who is part-way through") instead of an unseeable viewer reading as an absent one. That hold is a **blocked** PROTECT, not a plain one (`ProtectedSeason.unestablishable` → `season_scan.guard_result`), because it is a check that could not be *answered* rather than a protection that fired: a plain PROTECT on this gate does not hold a hand reap, `verdict.STRUCTURAL_GATES` carrying only streaming and unmanaged, so emptying `prunable` — which is what `_detect_conflicts` iterates — would otherwise have retired the keep-rule conflict that used to hold exactly these seasons and turned a season a hand reap was refused on into one it deletes. Only the blanket hold carries the flag; a season an actually-visible viewer holds stays a definite keep. `season_scan.gather`'s own `in_progress_hold_days` default moved 0 → 180 to match the policy's, since 0 is a value no shipped policy has and every test omitting it was exercising that unbounded claim (rule 141). Below that the count is a *lower bound*, so an outcome a deeper mirror could overturn reports "could not check" instead: the gate and a protect rule block, the signal withholds its pressure and lets coverage fall, a keep takes its full discount. An outcome the bound already earns still fires — a count that clears "at least N" stays clear however much history arrives. The two counts need different spans: recent watchers the policy's window, all-time the item's whole life here (`Facts.days_since_added`). The shipped 1095-day dormancy floor masks the *window* half, since dormancy is clamped to the reach, so condemning under that floor means the reach already spans any ≤1095-day window; it bites operators who lowered it. It masks nothing on the all-time half, where the span needed is the item's age rather than a window, so a long-lived title behind a shorter mirror reads "could not check" on shipped defaults |
| Delete mode | Grace is a **notice** window, not a gate: it starts a DB-only countdown and drives Leaving Soon + Discord. Nothing on the deletion path reads it, so what actually spares a file at send time is the live played-since-approval and streaming vetoes |
| Autonomy | An **earned grant keyed to `policy_hash`** — any edit reverts to approval-required |
| Caps | **Four**: items + bytes, per-run + rolling 30-day |
| Kill switch | **Asymmetric, not one-way**: arming is password-gated, disarming is one ungated click. The UI is the live control; the env var supplies the default only until the toggle is first written, after which the stored value wins for good. Re-read before every item, so disarming halts a run in flight |
| Section nav | **Its own grammar, not the pill track.** A rail whose active cut is a segment of the masthead's own bottom border on a wide screen; under 900px a fixed bottom bar of 24px icons, labels kept in the accessibility tree. The bar is 3.5rem tall because that height *is* the tap target, and it has to clear 44px (WCAG AAA, Apple) and 48px (Material). The pill track (`.tabs`, `.segmented`) now means only "pick a view of the same set", so navigating and filtering stop looking alike. Reap carries the armed state as a dot, amber when the safety read fails. **Settings' own nine-section rail takes the same 900px boundary**: below it the wrapped tabs (two lines from 860px, three from 470px) become one `.settings-picker` select, swapped in JS off `NARROW_SCREEN_QUERY` so only one of the two is ever in the tree. The Policy rail keeps its tabs — four labels, and it reports what you have scrolled to |
| Settings saves | **One save bar on General, the policy editor's `.savebar` reused** (rule 43). Its six per-row Save buttons were rendered inside the right-aligned control box, so the first keystroke shoved the field being typed in 71px sideways. The bar names every unsaved field, sends them in one request, offers Discard, holds the whole save while the accent hex is half-typed, and renders a refusal inside itself, since the route writes all six fields or none. Controls that take effect the moment they change are not drafts and stay out of it: the reverse-proxy Switch, the expand-seasons select and the spare-length Segmented save on the spot, and the theme select is local to the browser. Plex keeps two inline Saves, deferred in writing |
| Auth | Plex OAuth + `owned == true` check, local fallback that cannot be removed |
| ORM | **Plain SQLAlchemy, not SQLModel** — the model layer carries safety-bearing nullability and constraints, and we keep them declared in one place we control |
| Migrations | **Baseline `22777b2b5015` is frozen going forward** (testers have real data). It was edited before the freeze held, which is why `heal_candidate_size_nullable` carries a reflection guard (rule 81). Every schema change is its own revision chained onto head: an add, a new table, a backfill, or a guarded rebuild. Nearly always *widening* — the one exception is that same heal migration also dropping a stray server default, safe only because the ORM carries the Python-side default. `cache.db` stays disposable. |

## Where the pipeline stands

A full scan of a large library completes in tens of seconds, reporting progress while it runs
(the SPA polls `GET /api/scan/status`; there is no streaming transport),
and produces a candidate list partitioned into condemn / protect / abstain. The gather is
concurrent across sources: it costs roughly its slowest source plus the judge loop, which is
in-memory per item.

The why-panel renders for **keeps as well as deletes** — an item can score high enough to be
condemned on score alone and still be protected by a gate, and the panel says so in as many
words, with the numbers that produced the verdict:

```
Example Movie  (5.9 GB)
VERDICT: CONDEMN   score 91/100  (threshold 70)

  +70.0/70   unwatched for 2059 days (full pressure at 1825)
  +20.0/20   0 distinct watchers
  + 1.0/10   IMDb 5.4

  ✓ checked: dormant long enough -- 2059 days, your floor is 1095
  ✓ IMDb 5.4 from 6,000 votes -- below your 7.5 floor
  ✓ checked: popular here -- 0 distinct watchers in the last 365 days, your floor is 3
```

A tool that only explains its deletions cannot be trusted about its keeps.
