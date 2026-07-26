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
| **M3f** Signal quality — lift metric, size removed, dormancy gate | ✅ done |
| **M3g** Calibration — rewatch prior derived from the operator's own history | ✅ done |
| **M4** React SPA — review queue, why-panel, policy editor, live simulator | ✅ done |
| **M5** The reap loop — journal, planner, executor, canary, caps | ✅ done — the live send is wired (`executor._send_for_real`, `POST /api/runs/{id}/execute`), armed from the UI and phrase-gated |
| **M6** Season pruning | ✅ done — read-only scan through live execute (`executor._send_season`), unmonitor verified before any file is removed |
| **M7a** Grace lifecycle — the cancellable countdown (DB-only) | ✅ done |
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
2. **The autonomy-grant flow (M3b).** Rows, hash and caps exist; nothing can create a grant.
3. **The backtest surface (M3c).** The engine is complete and tested but unreachable. It needs
   `POST /api/policy/backtest` plus a minimal UI. Until it ships, the live simulator is the
   threshold-tuning surface, and no operator copy may name the backtest.
4. **Size-truth leftovers** (`docs/SIZE_TRUTH_PLAN.md`): a real-data pass reading
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
| Delete mode | DB-only grace period → cancellable → then irreversible |
| Autonomy | An **earned grant keyed to `policy_hash`** — any edit reverts to approval-required |
| Caps | **Four**: items + bytes, per-run + rolling 30-day |
| Kill switch | **One-way**: the UI can disable deletion, never enable it |
| Auth | Plex OAuth + `owned == true` check, local fallback that cannot be removed |
| ORM | **Plain SQLAlchemy, not SQLModel** — the model layer carries safety-bearing nullability and constraints, and we keep them declared in one place we control |
| Migrations | **Baseline `22777b2b5015` is frozen** (testers have real data). Every schema change is its own additive revision chained onto head. `cache.db` stays disposable. |

## Where the pipeline stands

A full scan of a large library completes in tens of seconds, streaming progress while it runs,
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
