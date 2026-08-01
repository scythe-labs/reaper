# Reaper — current state

> **What is true right now**, and nothing else. Edited in place: when a line stops being true,
> change that line; when a milestone lands, change its row. Two budgets, both enforced by
> `tests/test_repo_hygiene.py`: **120 lines and 100 columns.**
>
> **A row holds a phrase, never a sentence.** A table row cannot wrap, so narration inside a cell
> becomes one unwrappable line, which is how a single cell here once reached 21,000 characters.
> Reasoning behind a locked choice goes to `docs/DECISIONS.md`, measured findings to
> `docs/LEARNINGS.md`, and the story of how a fix was chosen to `docs/history/`.
>
> **Closed work leaves this file.** A shipped fix is not state; its record is the tracker and the
> code. What stays is something still true, still open, or still constraining what may be built.

Last verified against the code: 2026-07-30.

## Milestones

| Milestone | State |
|---|---|
| **M0** Skeleton — uv, ruff, mypy strict, Alembic, Docker, CI | ✅ done |
| **M1** Auth + clients — Plex OAuth + owner check, Tautulli, Sonarr, Radarr, Seerr | ✅ done |
| **M2a** IMDb ratings dataset | ✅ done |
| **M2b** Curated lists (IMDb Top 250, *arr tags, Plex collections) | ✅ done |
| **M3a** Scoring engine — gates, signals, observations | ✅ done |
| **M3b** Policy persistence — immutable rows, hash, caps, autonomy grants | 🟡 see open 1 |
| **M3c** Backtest — replay against the operator's own watch history | 🟡 see open 2 |
| **M3d** Field registry + authorable protect rules | ✅ done |
| **M3e** Snapshot pipeline + REST API + polled progress | ✅ done |
| **M3f** Signal quality — lift metric, size removed, dormancy gate | 🟡 see open 2 |
| **M3g** Calibration — rewatch prior from the operator's own history | 🟡 see open 2 |
| **M4** React SPA — review queue, why-panel, policy editor, live simulator | ✅ done |
| **M5** The reap loop — journal, planner, executor, canary, caps | ✅ done |
| **M6** Season pruning | ✅ done |
| **M7a** Grace lifecycle — the notice countdown (DB-only) | ✅ done |
| **M7b** Leaving Soon label + Discord | ✅ done |
| **M8** Profiles + scheduler | ✅ done |
| **Whitelist** — manual "spare this file", scan + planner + grace | ✅ done |
| **Scales** — per-requester cards over the last scan | ✅ done |
| **Operator console** — service config, schedule, safety, review | ✅ done |
| **First start** — four steps, password forced, restore door, resume from the server | ✅ done |

## Open work

1. **The autonomy-grant flow (M3b).** Rows, hash and caps exist; nothing can create a grant.
2. **The backtest (M3c), its lift metric (M3f), and the calibration prior (M3g).** All three
   engines are complete and tested; none is reachable, and nothing in `src/` imports
   `engine.backtest`. The backtest needs `POST /api/policy/backtest` plus a minimal UI, and
   `calibration.derive` needs that route to call it and pass the result in, since `backtest.run`
   never calls `derive`. Three things the route must inject, none reachable from `engine/`: the
   prior; the watch-blind map, from `watch_evidence`'s marks, or a churned title replays on a
   confident zero; and `ensure_schema` before the first mirror read, since `backtest._plays` and
   `calibration.derive` query it raw (#283). The `rescued` count models grace as a delay before
   deletion, which production does not do, so it is a best case: fix or label it when wiring.
   Until they ship the live simulator is the threshold-tuning surface, and no operator copy may
   name the backtest or promise a fitted prior (rule 25).
3. **The screen-reader sweep is landed.** What landed and why each shape was chosen is
   `docs/history/SCREEN_READER_SWEEP.md`; the guard's own measurement is in `docs/LEARNINGS.md`.
   A scroll container is now held reachable by a stylesheet-driven gate, not by memory. Whether a
   notice speaks is `standing`, declared per call site and held by a count and a written reason.
4. **The stylesheet split has four optional stages left** (`docs/CSS_SPLIT_PLAN.md`). The cut
   itself landed: 31 files under `frontend/src/styles/`, load order declared by `index.css` and
   load-bearing. Left: naming the control-standard padding and the 12 unnamed `z-index` values;
   rehoming `.notice` and `.qty` out of the sections they were appended into; a type and space
   scale, since 39 font sizes and 25 gap values are drift, not variety; and a dead-CSS pass that
   must stay manual, because 96 sites compute their class name.

## Decisions locked

A **†** marks a row whose reasoning is a section of the same name in `docs/DECISIONS.md`.

| Decision | Choice |
|---|---|
| Condemn logic | **Flat AND** of typed conditions. No OR, no nesting, no NOT. |
| Protections | **Gates with no CONDEMN constructor** — structurally cannot delete |
| Protect authoring | **Catalog + user-authored protect rules** (worst case is nothing deleted) |
| Signals | **Unsigned**, fixed denominator including unknown weights |
| Observations | **Known / Absent / Unknown** — never conflated |
| What a hand reap may overrule | **Everything except a structural stop** † |
| Watch-history reach | **Every reader that goes through `Facts`** † |
| Watch history that vanished | **A high-water mark that cannot fall**, never a remapped key † |
| Why-panel scope | **Renders for keeps as well as deletes** † |
| Delete mode | **A notice window, not a gate** † |
| Autonomy | An **earned grant keyed to `policy_hash`** — any edit reverts to approval-required |
| Caps | **Four**: items + bytes, per-run + rolling 30-day |
| Size acquisition | **Sonarr or Radarr's own total, never a stand-in** † |
| Kill switch | **Asymmetric, not one-way** † |
| Section nav | **Its own grammar, not the pill track** † |
| Settings saves | **One save bar on General**, the policy editor's `.savebar` reused † |
| Settings row layout | **One fixed control track per box**, released for everything else † |
| Setup readiness | **Scanning and reaping are two readinesses, reported apart** † |
| Auth | Plex OAuth + `owned == true` check, local fallback that cannot be removed |
| Peer trust | **`reaper.auth.proxy` alone believes a forwarded header** † |
| ORM | **Plain SQLAlchemy, not SQLModel** † |
| Migrations | **Baseline `22777b2b5015` is frozen going forward** † |
| Gate retirement | **A stored body self-heals on load** † |
| Plex index retirement | **A row dropped only once the sweep has spoken** † |

## Where the pipeline stands

A full scan of a large library completes in tens of seconds, reporting progress while it runs (the
SPA polls `GET /api/scan/status`; there is no streaming transport), and produces a candidate list
partitioned into condemn / protect / abstain. The gather is concurrent across sources: it costs
roughly its slowest source plus the judge loop, which is in-memory per item.

A scan is a snapshot: all evidence is frozen and hashed before scoring, so a transient timeout
cannot flip an item's fate mid-run. The why-panel explains every verdict in both directions; the
worked example is under **Why-panel scope** in `docs/DECISIONS.md`.

Only the **newest 30 scans** are kept (`services.retention`, swept twice daily; a scan a run is
bound to stays regardless). Nothing may be built that reads scan history past that window. Its
compaction defers to a live scan or reap: past 1.2 GB the `VACUUM` outlasts the app's 5s wait.
