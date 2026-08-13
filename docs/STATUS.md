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

Last verified against the code: 2026-08-02.

## Milestones

| Milestone | State |
|---|---|
| **M0** Skeleton — uv, ruff, mypy strict, Alembic, Docker, CI | ✅ done |
| **M1** Auth + clients — Plex OAuth + owner check, Tautulli, Sonarr, Radarr, Seerr | ✅ done |
| **M2a** IMDb ratings dataset | ✅ done |
| **M2b** Protection lists — Arr-style registry, act through on_list rules | ✅ done |
| **M3a** Scoring engine — gates, signals, observations | ✅ done |
| **M3b** Policy persistence — immutable rows, hash, caps, autonomy grants | 🟡 see open 1 |
| **M3c** Backtest — replay against the operator's own watch history | ❌ dropped, #553 |
| **M3d** Field registry + authorable protect rules | ✅ done |
| **M3e** Snapshot pipeline + REST API + polled progress | ✅ done |
| **M3f** Signal quality — banked as the shipped defaults, `docs/SIGNALS.md` | ✅ done |
| **M3g** Calibration — a fitted rewatch prior | 🟡 stage 1 shipped, stage 2 next, #554 |
| **M4** React SPA — review queue, why-panel, policy editor, live simulator | ✅ done |
| **M5** The reap loop — journal, planner, executor, canary, caps | ✅ done |
| **M6** Season pruning | ✅ done |
| **M7** Grace lifecycle, Leaving Soon label, Discord | ✅ done |
| **M8** Profiles + scheduler | ✅ done |
| **Whitelist** — manual "spare this file", scan + planner + grace | ✅ done |
| **Scales** — per-requester cards over the last scan | ✅ done |
| **Operator console** — service config, schedule, safety, review | ✅ done |
| **First start** — four steps, password forced, restore door, resume from the server | ✅ done |
| **Packaged installs** — Win/macOS binaries + tray, snap, CalVer, update check | 🟡 no cut yet |

## Open work

1. **The autonomy-grant flow (M3b).** Rows, hash and caps exist; nothing can create a grant,
   and `backtest_passed` is now a gate nothing can ever satisfy: M3c is dropped, so wiring M3b
   means choosing a different earned bar and dropping that column with it (rule 148).
2. **The screen-reader sweep is landed.** What landed and why each shape was chosen is
   `docs/history/SCREEN_READER_SWEEP.md`; the guard's own measurement is in `docs/LEARNINGS.md`.
   A scroll container is now held reachable by a stylesheet-driven gate, not by memory. Whether a
   notice speaks is `standing`, declared per call site and held by a count and a written reason.
3. **The stylesheet is 35 files with named scales** (`docs/history/CSS_SPLIT_PLAN.md`). Load
   declared by `index.css` and load-bearing. Type (9 steps), weight (4) and the constants
   (`--control-pad`, `--radius-pill`, a `--z-*` ladder) are adopted everywhere; space is 11 steps
   adopted only where nothing moved, with a ratchet on the 294 literals left. Gates hold the file
   cap, the theme blocks, the iOS zoom floor, and rule 40's control standard at all ten of its
   boxes. Left: `.notice` still lives in the simulator section, and a dead-CSS pass that must
   stay manual, because 96 sites compute their class name.
4. **The manual is one source, two renderers** (`frontend/src/docs/toMdx.ts`). Five pages are
   generated into `manual/` from the app's typed blocks, eight hand-written beside them, all
   thirteen served by `website/` on Docusaurus. `manual.gen.test.ts` fails on drift. Pages
   publishes from `dev`; revisit at the first release, probably as Docusaurus versioning.

## Decisions locked

A **†** marks a row whose reasoning is a section of the same name in `docs/DECISIONS.md`.

| Decision | Choice |
|---|---|
| Condemn logic | **One typed condition per rule**, weighted. No OR, no nesting, no NOT. † |
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
| Address bar | **A URL per section, lane, filters, panel, policy**, written on nav, read at mount |
| Settings saves | **One save bar on General**, the policy editor's `.savebar` reused † |
| Settings row layout | **One fixed control track per box**, released for everything else † |
| Setup readiness | **Scanning and reaping are two readinesses, reported apart** † |
| Adding a service | **Connect, test, then map** — Save waits on a pass and one mapped folder |
| Plex library list | **Synced when the server is linked**, never left for the operator to press |
| Protection lists | **Defined on Settings, checked and re-scanned on save**, nightly too; by id |
| Versioning | **CalVer `vYYYY.M.N`, tagged by CI on every push to `main`** † |
| Auth | Plex OAuth + `owned == true` check, local fallback that cannot be removed |
| Peer trust | **`reaper.auth.proxy` alone believes a forwarded header** † |
| ORM | **Plain SQLAlchemy, not SQLModel** † |
| Migrations | **Frozen baseline; schema leaves in two releases**; destructive ones copy first † |
| Rolling back | **A database a newer build migrated refuses to boot**, preflight and startup |
| Gate retirement | **Persisted by the upgrade where it can be, healed on load where it can't** † |
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
