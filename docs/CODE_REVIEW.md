## 1. Bugs

**1. Docker build fails: README.md declared as project readme but never copied before the uv installs**
`Dockerfile:32 (install triggering the failure; root cause is the missing README.md at COPY on line 31)`
Severity: critical
Description: pyproject.toml declares `readme = "README.md"`, so hatchling's metadata validation requires README.md at build time. The runtime stage copies `pyproject.toml` (line 31) then runs `uv pip install --system --no-cache .` (line 32) and `uv pip install --system --no-deps -e .` (line 37), but README.md is never COPYed into /app. Hatchling raises `OSError: Readme file does not exist: README.md`, so both install steps fail. The container is the production deliverable and cannot be built as written. (The secondary claim about `packages = ["src/reaper"]` making the layer unbuildable is inaccurate and can be disregarded.)
Failure scenario: An operator or CI runs `docker build .` → build aborts at line 32 with `OSError: Readme file does not exist: README.md`. No image is produced; Reaper cannot be deployed via its documented container path.
Recommended fix: Change line 31 from `COPY pyproject.toml ./` to `COPY pyproject.toml README.md ./`. Add a `docker build` step to CI to catch this.

**2. Movie→Plex join keys on title alone (last-wins), so duplicate-titled movies bind to the wrong Plex item**
`src/reaper/services/snapshot.py:685 (root cause in _plex_index); 713/725 (the poisoned join in _raw_items)`
Severity: high
Description: `_plex_index` builds a `title.lower() -> row` map with last-write-wins and no year disambiguation; `_raw_items` then does `plex_index.get(title.lower())` and stores that row's `rating_key` as the candidate's `plex_rating_key`. When two movies share a title (remakes/reboots), only one Plex row survives, so both Radarr movies join to whichever Plex row was indexed last. The season path was explicitly hardened against exactly this (`build_tv_index` keeps a `list[ShowRow]` and `match_show` refuses on year conflict). The movie path has `movie['year']` and `row.get('year')` available but uses neither. The mis-join poisons scoring dormancy/popularity and the live execute-time interlocks, since `_being_watched_now` and `_watched_since_approval` both key off the wrong `plex_rating_key`.
Failure scenario: Library has 'The Mummy' (1999, streamed weekly) and 'The Mummy' (2017, never watched). `_plex_index` keeps only the 2017 row; the 1999 candidate joins to the 2017 key, reads zero recent watchers, and is condemned. At execute time the streaming veto checks the 2017 key against the veto set (which holds the 1999 key someone is watching now) → no match → the 1999 movie is deleted mid-stream.
Recommended fix: Mirror the season path. Make `_plex_index` return `dict[str, list[dict]]` appending every row per lowercased title. In `_raw_items`, resolve by year the way `match_show` does; on any conflict or unresolved duplicate return no match so `plex_rating_key`/`added_at` stay None (Unknown facts → ABSTAIN, and the executor's `plex_rating_key is None` branch spares it). Never silently bind to the last-indexed row.

**3. Bulk "Reap now" on a selected TV show sends the show group_key, which build_plan rejects**
`frontend/src/components/ReviewQueue.tsx:979 (frontend/src/components/ReviewQueue.tsx); backend validation at src/reaper/services/planner.py:276-289`
Severity: high
Description: In Select mode a TV show is only selectable at the show level, so the only key entering `selected` for a show is `group.key` (the 3-part `sonarr:{inst}:{series}` group_key). Bulk "Reap now" calls `reapNow.mutate([...selected])` → `api.createRun(keys)` → `build_plan`, which validates each key against `condemned_keys` (4-part season keys `sonarr:{inst}:{series}:{season}`). A show group_key is never in that set, so build_plan raises PlanError. Bulk spare/reap override works because it resolves group_key via whitelist, but the reap path does not — an asymmetry that fails only for the destructive action.
Failure scenario: Operator opens "Would reap", enters Select mode, taps TV shows, clicks "Reap now". The confirmation never opens; `reapNow.error` renders "These items are not condemned in this snapshot…". There is no way to bulk-reap any TV title.
Recommended fix: Make the reap path symmetric with the override path. Preferably in `build_plan`, mirror `whitelist.effective_override`: when a requested key matches a candidate's `group_key` rather than a `media_key`, expand it to that group's condemned member media_keys before the `unknown` check. Add a test: select a condemned show → Reap now → plan contains all condemned seasons and no PlanError. Currently fails safe (loud 422, no deletion).

**4. An empty media_keys selection builds a whole-library reap plan instead of failing closed**
`src/reaper/api/runs.py:153`
Severity: medium
Description: `only = set(payload.media_keys) if payload and payload.media_keys else None` treats an explicitly-supplied empty list the same as an omitted field, because `[]` is falsy. A 'reap selected' request carrying an empty selection collapses to `only_media_keys=None`, which `build_plan` interprets as 'plan the entire condemned set'. This inverts intent on the destructive-planning path: a selection of nothing becomes a selection of everything. Had the empty set been passed through, `build_plan` would fail closed at its `if not plannable` guard (planner.py:298) with a 422.
Failure scenario: A UI 'Reap selected' button posts `{"media_keys": []}` when nothing is highlighted. The API builds and journals a full-library reap run covering thousands of condemned items and returns its whole-library confirmation_phrase.
Recommended fix: Distinguish omitted from explicit empty: `only = set(payload.media_keys) if (payload is not None and payload.media_keys is not None) else None`. An empty explicit selection then fails closed at planner.py:298. Optionally special-case it to a clearer 422 "No items selected to reap."

**5. Leaving Soon Discord announce re-fires on every call in the default read-only path**
`src/reaper/services/leaving_soon.py:126-130 (announce), driven by 117-119 (recomputed diff) and gated by apply at 79/122; route passes notifier unconditionally at src/reaper/api/leaving_soon.py:81`
Severity: medium
Description: The Discord announce is driven by `plan.to_add` (`should_be_labelled - currently_labelled`) with no persisted 'already announced' record. When `apply=False` the label is never written to Plex, so `target.current()` never learns about newly-marked items and `plan.to_add` is recomputed as the ENTIRE in-grace movie set on every invocation. `apply` is False in the default install state (`RuntimeSafety.leaving_soon_write_allowed = destructive_allowed or allow_leaving_soon_unarmed`, both off by default). So the announce is not idempotent in the configuration most installs run in. The single-section `PlexLabelTarget` aggravates this for multi-section movie libraries even when armed.
Failure scenario: Default install, Discord webhook configured, 5 movies in grace. Operator clicks 'Mark Leaving Soon' (POST /api/leaving-soon/sync) once → embed '5 titles are leaving soon'. Every subsequent click re-posts an identical duplicate embed because nothing was persisted to Plex.
Recommended fix: Make the announce idempotent independent of whether the write landed. Persist the set of already-announced media keys and announce only keys in `plan.to_add` not already in that set, adding them once announced and pruning as they leave grace (mirroring `plan.to_remove`). In armed/apply=True mode the bug self-corrects after the first click, so the fix mainly addresses the apply=False path. (Simply gating on `applied` would remove the intended read-only heads-up, so the persisted-set approach is preferable.)

**6. The @retry on BaseClient._send can never fire, so no read is ever retried on a transient failure**
`src/reaper/clients/base.py:159-188`
Severity: medium
Description: `_send` is decorated with `@retry(retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)), ...)`, but the body catches those and re-raises them as `IntegrationError` (a RuntimeError) before they can escape. Since all transient transport failures are subclasses of `httpx.TransportError`, every one is converted to `IntegrationError`, which is NOT in the retry predicate. With `reraise=True`, tenacity re-raises on the first attempt without ever retrying. The exponential-backoff retry is dead code.
Failure scenario: During a multi-minute scan, a single transient blip (dropped connection to Radarr, a ReadTimeout to Tautulli, a DNS hiccup to Seerr) makes `get_json` raise `IntegrationError` immediately with zero retries, aborting the whole scan on the first momentary glitch.
Recommended fix: Let raw httpx transport errors reach tenacity. Split `_send`: an inner `@retry`-decorated `_request` that calls `self._client.request` and lets `httpx.TransportError`/`TimeoutException` propagate (so the predicate matches and backoff runs); an outer `_send` that maps the final transport failure to `IntegrationError` and still raises `IntegrationError` for `status_code >= 400`. Do NOT add the 4xx/5xx `IntegrationError` to the retry predicate.

**7. Backtest condemn decision diverges from the production verdict function**
`src/reaper/engine/backtest.py:407-409`
Severity: medium
Description: The backtest reimplements the condemn decision instead of reusing `services/snapshot.py::_verdict`. Two divergences: (1) Rounding — production compares the STORED rounded score `round(item_score.value) >= policy.condemn_at`, whereas the backtest compares the raw float `item_score.value < policy.condemn_at`, so items scoring in [condemn_at-0.5, condemn_at) are condemned in production but skipped in backtest. (2) Coverage floor — production abstains when `coverage_bp < policy.coverage_floor_bp`; the backtest never checks coverage, so it condemns low-coverage items production would abstain on. The coverage-floor divergence is active even for the default policy (coverage_floor_bp defaults to 5000).
Failure scenario: condemn_at=70; item scores 69.6. Production: round(69.6)=70 ≥ 70 → CONDEMN; backtest: 69.6 < 70 → skipped, understating regret at the boundary. Separately, coverage_floor_bp=9000 and a TV item at 85% coverage → production ABSTAINS but backtest condemns, overstating deletions and lift.
Recommended fix: Have the backtest reach the verdict as production does: `score_value = round(item_score.value)`, `coverage_bp = round(item_score.coverage * 10_000)`, skip when `coverage_bp < policy.coverage_floor_bp`, condemn only when `score_value >= policy.condemn_at`. Better, extract `snapshot.py::_verdict` into the engine layer and call it from both paths so they cannot drift.

**8. Grace clock is never reset when an item is re-condemned after a rescue**
`src/reaper/services/grace.py:114 (stale value consumed) rooted in snapshot.py:652-658 (writer never resets first_flagged_at, and last_seen_condemned_at is write-only)`
Severity: medium
Description: `grace_report` derives the window start from `FirstFlagged.first_flagged_at` and never restarts it. `_record_first_flagged` sets `first_flagged_at` on the first-ever condemn and thereafter only bumps `last_seen_condemned_at` (written but read nowhere). Nothing deletes FirstFlagged when an item leaves the condemned set. When an item condemned long ago is rescued and later re-condemned, its `first_flagged_at` is already older than `grace_days`, so `grace_report` yields `in_grace=False`/`days_remaining=0` and drops it straight into `ready` with no fresh countdown — and `leaving_soon.sync` never labels or warns for a `ready` item. The core safety promise (a grace window before deletion) is violated on the second condemnation. The unused `last_seen_condemned_at` column suggests a reset was intended but never implemented.
Failure scenario: Movie X condemned at T0 → `first_flagged_at=T0`. User watches it at T1; next scan re-judges protect. ~13 months later it is dormant and re-condemned at T2; `_record_first_flagged` keeps `first_flagged_at=T0`. `ends = T0 + 14 days` (long past) → item is `ready`, days_remaining=0, no Leaving Soon label, no Discord warning, immediately eligible for reap with zero grace the second time.
Recommended fix: In `_record_first_flagged`, when the existing row's `last_seen_condemned_at` predates `now` by more than the grace window, reset `first_flagged_at = now` (thread `grace_days` in, or use a conservative fixed gap). Alternatively delete the FirstFlagged row whenever an item is judged non-condemn in a fresh, non-degraded snapshot. Key the reset on the gap exceeding the grace window, not any single missed snapshot, to survive transient outages.

**9. Select-mode keyboard toggle leaves dragRef set, so a later mouse hover paints selections**
`frontend/src/components/ReviewQueue.tsx:711-716 (set), 723 (only clear); triggered from card onKeyDown at lines 407 and 493`
Severity: medium
Description: `onSelectDown` sets `dragRef.current = { mode }` and is only cleared by window `pointerup`/`pointercancel`. The card keyboard handlers call `onSelectDown` for Enter/Space in Select mode (casting the KeyboardEvent to a PointerEvent). A keyboard press never emits pointerup/pointercancel, so after selecting with the keyboard `dragRef.current` stays populated; from then on, `onPointerEnter` → `onSelectEnter` applies the stuck mode to every card the mouse merely hovers over, no button held.
Failure scenario: In Select mode a keyboard user presses Space to select a card, then moves the mouse to click something: every card the cursor passes over gets toggled, silently corrupting the selection a subsequent bulk Spare/Reap/Reap-now acts on.
Recommended fix: Give keyboard activation its own path that does not set dragRef — call a plain `applySelect(key, selected.has(key) ? "remove" : "add")` in the onKeyDown handlers, or null `dragRef.current` at the end of a keyboard toggle. Belt-and-suspenders: have `onSelectEnter` ignore enters when no pointer button is pressed (check `e.buttons`).

**10. Renaming an instance into a name clash returns 404 Not Found instead of 409 Conflict**
`src/reaper/api/settings.py:210-211`
Severity: low
Description: `update_instance` catches every `instances.InstanceError` and maps it to HTTP 404. But `instances.update_instance` raises InstanceError for two distinct causes: the instance not existing (instances.py:94) and a name collision with another instance of the same kind (instances.py:160). Only the first is a 404; the clash is a conflict and `create_instance` already returns 409 for the identical condition.
Failure scenario: Admin renames a Radarr instance to a name already used by another Radarr instance. The service raises the name-clash error with status 404, so a client branching on status sees 'the instance disappeared' rather than 'name conflict', inconsistent with the add form's 409.
Recommended fix: Add InstanceNotFound and InstanceConflict subclasses of InstanceError; map Conflict to 409 and NotFound to 404 in the update route.

**11. save_policy returns the requested name for a content-identical save, but that name is never persisted**
`src/reaper/api/routes.py:479-491`
Severity: low
Description: Because the policy hash excludes the name, saving a policy whose body already exists is a no-op (`if existing is None`), so a name-only change is never written. But the route returns `_policy_out(body, payload.name)` with the new name, while `active_policy` keeps returning the stored row's name. The success response and the next reload disagree.
Failure scenario: A policy with hash H is stored as 'Aggressive'. The owner changes only the name to 'Nightly' and saves. The response shows 'Nightly' and looks successful, but nothing was written; reopening GET /api/policy shows 'Aggressive'.
Recommended fix: On the content-identical branch (`existing is not None`), return `_policy_out(body, existing.name)` so the response reflects the persisted name. (Actually renaming would mutate an append-only, hash-referenced row and should not be done without reconsidering those invariants.)

**12. Timeout error message always reports the read timeout even for connect/write/pool timeouts**
`src/reaper/clients/base.py:175-178 and 234-237`
Severity: low
Description: Both `_send` and `_mutate` build the timeout message as `f"timed out after {DEFAULT_TIMEOUT.read}s"`, hardcoding the 30s read timeout. A ConnectTimeout (5s), WriteTimeout (10s), or PoolTimeout (5s) all report '30s', misdirecting an operator diagnosing connectivity.
Failure scenario: Radarr's host is up but not accepting connections; the client fails with ConnectTimeout after 5s, but the IntegrationError says 'radarr: timed out after 30.0s', suggesting the service is slow rather than unreachable.
Recommended fix: Replace the hardcoded message in both methods with one reflecting the actual timeout kind, e.g. `f"timed out ({type(exc).__name__})"`, or branch on the httpx timeout subclass to name the specific configured budget.

**13. Per-run caps count items that will be skipped (late-spared), so sparing during grace can trip a false ABORT**
`src/reaper/services/executor.py:313-314 (count from unfiltered deletes); build at 447-460 and late per-item spare skip at 558`
Severity: low
Description: `_check_caps` computes `items = len(deletes)` and `total_bytes` over the full `deletes` list, which still includes items the owner spared by hand AFTER the plan was built (still `verdict='condemn'`, filtered later per-item in `_one_delete`). The cap is measured against the pre-spare plan size, not what will actually be deleted. It fails safe (over-counts) but can block a legitimate reduced run, and the abort message quotes a count that no longer matches the confirmation phrase (which excludes spares).
Failure scenario: max_items_per_run=50. A 55-item plan is built; the owner spares 10 during grace (intending 45). At execute, `deletes` still has 55 → `_check_caps` raises 'This plan would delete 55 items, over the per-run cap of 50', even though only 45 would be deleted and the phrase said 45.
Recommended fix: In `_check_caps`, exclude any `d` where `whitelist.effective_override(d.candidate.media_key, self._decisions) == "spare"` before computing `items`/`total_bytes` (pass `self._decisions` in), mirroring `runs.py::_planned_candidates`. Keep abort-not-truncate semantics for the deletable set.

**14. ReapPlan step rows keyed by media_key collide — a TV season emits three steps with the same media_key and ordinal**
`frontend/src/components/ReapPlan.tsx:34`
Severity: low
Description: The steps table renders `<tr key={step.media_key}>`. `planner._season_steps` emits three ActionSteps per season (sonarr_unmonitor, sonarr_verify_unmonitor, sonarr_delete_files), all with the same `media_key` AND the same `ordinal`. React sees duplicate keys within a season and across seasons, producing warnings and mis-keyed reconciliation where the wrong row shows the wrong State/`canary` tag as dry-run states change.
Failure scenario: A plan including any condemned TV season, dry-run: the table logs 'Encountered two children with the same key' and step states render against the wrong row as the dry run progresses.
Recommended fix: Key on a value unique among siblings, e.g. `key={`${step.media_key}-${step.kind}`}`, or use the idempotency_key. Review the Report list (line 72) too if outcomes can repeat a media_key.

**15. WhyPanel hero backdrop shows the previous item's art when switching between cached items**
`frontend/src/components/WhyPanel.tsx:43-46`
Severity: low
Description: WhyHero seeds its image src with `useState(`${posterUrl}?kind=art`)` and never resets it when `posterUrl` changes — no `useEffect([posterUrl])`, no key. The panel is mounted without a key and the detail query has no keepPreviousData, so when the new item is cached, `detail` transitions directly A→B without a null in between; WhyPanel is reused rather than remounted and keeps A's src. The sibling Backdrop in ReviewQueue does the reset; WhyHero omits it.
Failure scenario: Open why-panel for movie A, then re-select a cached neighbor B: the panel's title, score and reasoning are B's, but the hero backdrop still shows A's artwork — misleading on the one screen meant for trustworthy per-item explanation.
Recommended fix: Mirror Backdrop: `useEffect(() => { fellBack.current = false; setSrc(`${posterUrl}?kind=art`); }, [posterUrl]);`. Alternatively key the panel per item in App.tsx (`<WhyPanel key={detail.id} .../>`).

**16. Undefined --spare token makes the reap-confirm dry-run success message a hardcoded, off-theme green**
`frontend/src/index.css:2838`
Severity: low
Description: `.dry-ok { color: var(--spare, #2f9e57); }` references a CSS variable `--spare` that is never defined anywhere, so the fallback `#2f9e57` always wins in both themes. This is the only 'success/protect' colour not using the semantic `--protect` token, and it is not theme-aware. `.dry-ok` renders in ReapConfirm.tsx (line 94), the modal that confirms deletion.
Failure scenario: On dark mode an operator passes a dry run in the reap confirmation modal. The 'dry run passed' line renders fixed #2f9e57 on the dark surface instead of `--protect` #5fce97, reading darker/duller and mismatching every other green.
Recommended fix: Replace `var(--spare, #2f9e57)` with `var(--protect)`. If a distinct spare shade is genuinely intended, define `--spare` in both the light `:root` and the dark-theme block.

## 2. Hacks and Workarounds

**1. Manifest re-check (interlock #1) is a tautology: the executor re-hashes the same frozen snapshot it was planned from**
`src/reaper/services/executor.py:432-438; docstrings at planner.py:89-103 and 306-311, executor.py:425-438`
Severity: low
Description: `execute()` computes `current_hash = manifest_hash(sorted(condemned...))` from `_condemned(session, run.snapshot_id)` and compares it to `run.approved_manifest_hash`, which `build_plan` computed from the identical query over the identical immutable snapshot. Candidate rows are frozen per-snapshot and never mutated, and `execute` never re-reads the live *arr, so the two hashes are always equal — the check can only fire if candidate rows are deleted out from under the run, never if the actual library changed. The docstring sells it as catching 'an item added, removed, or resized', giving false confidence in drift detection that does not exist. The real staleness protection is the route's `confirmation_phrase` recompute.
Failure scenario: A condemned movie is deleted directly in Radarr, or grows on disk, between approval and execute. The executor re-reads the unchanged frozen rows, recomputes an identical hash, passes interlock #1, and proceeds — the very drift the docstring promises to catch sails through.
Recommended fix: Rewrite the docstrings to state accurately that interlock #1 is a frozen-snapshot integrity check (detecting loss/tampering of the condemned rows for this snapshot, not live drift), and that live drift is caught by the per-item interlocks (streaming veto, played-since-approval, per-item existence/size re-reads), stale-tab replay by the `run.state` 'executes once' guard and the confirmation-phrase recompute. If manifest-level live drift detection is genuinely wanted, re-read live *arr existence/size per condemned item and compare against frozen sizes rather than re-hashing immutable rows.

**2. PolicyEditor stores scan-transition flag in useMemo instead of useRef**
`frontend/src/components/PolicyEditor.tsx:593`
Severity: low
Description: `const wasScanning = useMemo(() => ({ v: false }), [])` is used as mutable persistent storage across renders and written inside an effect. React documents useMemo as a performance hint with no guarantee the value is preserved; if React discards it, `wasScanning.v` resets to false mid-scan and the running→stopped transition that invalidates ["simulate"]/["snapshot"] is missed. ScanBar and SetupWizard implement the identical pattern correctly with `useRef(false)`.
Failure scenario: If React drops the memoized object mid-scan, the scan-finished branch never fires, so the simulator keeps showing the stale pre-scan outcome until an unrelated invalidation.
Recommended fix: Replace with `const wasScanning = useRef(false);`, use `wasScanning.current` in the effect, and drop `wasScanning` from the dependency array — matching ScanBar.tsx and SetupWizard.tsx.

## 3. Refactor Opportunities

**1. Two duplicate segmented-control implementations, plus duplicated banner/safety-state semantics**
`frontend/src/index.css:214-221 (.views) and 346-365 (.tab) vs 1742-1767 (.segmented/.seg); 242-259 (banner-safe/armed) vs 2593-2602 (safety-state.safe/armed)`
Severity: low
Description: The pill segmented control is implemented twice with near-identical rules: `.views`+`.tab` (App.tsx nav) and `.segmented`+`.seg` (PolicyEditor.tsx) — both use `background: var(--surface-2)`, `border-radius: 999px`, and an active state of `background: var(--surface); box-shadow: var(--shadow-sm); font-weight: 600`. Separately, `.safety-state.safe`/`.armed` duplicate the exact soft-bg + color-mix border + dot-colour pattern of `.banner-safe`/`.banner-armed`. Two copies means a colour or radius change to one control silently diverges from the other.
Failure scenario: A future tweak to the active-tab treatment gets applied to `.tab` but not `.seg`, so the section nav and the policy-scope switch drift apart visually; likewise a banner colour fix misses the safety panel.
Recommended fix: Extract one shared pill segmented-control base (keep `.segmented`/`.seg` and have App.tsx's nav reuse it, dropping `.views`/`.tab`), and factor the safe/armed soft-surface + color-mix border into shared `.tone-safe`/`.tone-armed` classes consumed by both `.banner-*` and `.safety-state.*`. `.banner-dot` is already shared, so only the container rules need consolidating.

## 4. Performance

**1. IMDb dataset load holds the SQLite cache write lock for the entire multi-minute parse+insert**
`src/reaper/services/imdb_dataset.py:133-193`
Severity: medium
Description: `load` performs the full ~1.69M-row parse and batched INSERT inside a single `engine.begin()` transaction together with the atomic DROP/RENAME swap. The cache DB uses WAL with `busy_timeout=5000` (one writer). Holding one write transaction for the whole load means any other cache writer — history_sync during a scan, lists.sync, the nightly curated refresh — waits at most 5s and then fails with 'database is locked'. Only the DROP/RENAME needs to be atomic; the staging insert does not.
Failure scenario: On startup, `catch_up_on_startup` kicks off a ratings refresh as a background task. The operator triggers a scan, whose `history_sync.sync` INSERT OR REPLACE blocks on the load's write lock, exceeds 5s, and raises 'database is locked', failing the sync. Same collision for the 3:45 curated-list refresh if the 3:30 ratings load overruns.
Recommended fix: Split the transaction. Populate `imdb_rating_staging` outside the atomic swap, committing each 10k batch in its own short transaction (or a dedicated connection) so no long-lived write lock is held during the parse. Then a single short `engine.begin()` does only the fast swap (DROP/RENAME/CREATE INDEX + upsert). A killed process mid-populate is already safe because the next run drops the partial staging table and the previous `imdb_rating` stays live until the swap.

**2. list_candidates does not floor limit/offset, so a negative limit returns the entire filtered set**
`src/reaper/api/routes.py:133-134 (param declarations) and 205 (.limit(min(limit, 500)))`
Severity: low
Description: `limit` and `offset` are plain ints with no `Query(ge=...)` constraint, and the query only caps the upper bound (`.limit(min(limit, 500))`), never the lower. A negative limit passes through: `.limit(min(-1, 500))` renders `LIMIT -1`, which SQLite treats as 'no limit', so the whole filtered set is materialised and serialised in one response.
Failure scenario: `GET /api/candidates?verdict=protect&limit=-1` on a large library returns every protected candidate (thousands of rows with summary/poster fields) in one payload, spiking memory and latency.
Recommended fix: Constrain the params: `limit: int = Query(100, ge=1, le=500)` and `offset: int = Query(0, ge=0)`, then drop the redundant `min()` (use plain `.limit(limit)`). Or clamp defensively: `.limit(max(1, min(limit, 500))).offset(max(0, offset))`.

**3. IntersectionObserver is torn down and recreated on every ReviewQueue render**
`frontend/src/components/ReviewQueue.tsx:665 (dependency array); root cause at line 652 where data is allocated fresh each render`
Severity: low
Description: The sentinel effect depends on `[data]`, but `data = pages.pages.flatMap(...)` produces a new array reference every render, so the effect disconnects and reconstructs an IntersectionObserver on every render — including every keystroke and every card repaint during drag. If the sentinel is in view when the observer reconnects it fires immediately, so `setVisible(v => v + PAGE)` can run repeatedly while the sentinel stays visible, over-revealing the window.
Failure scenario: On a large condemned list, typing in the search field or painting a drag selection while the "Showing N of M" sentinel is on screen repeatedly re-fires the observer, jumping `visible` by PAGE each render and mounting far more cards (and lazy poster fetches) than intended.
Recommended fix: Give the effect a stable dependency. Either memoize data (`useMemo(() => pages ? pages.pages.flatMap((p) => p.items) : undefined, [pages])`) or change the deps to `[data?.length, hasNextPage]`, which also correctly re-runs when the conditionally-rendered sentinel mounts/unmounts.

## 5. Production Readiness

**1. Protection-list (whitelist) sync failures do not degrade the snapshot, so a scan can execute against empty/stale keep-lists**
`src/reaper/services/scan_runner.py:315-325 (call + log with no inspection); root cause spans snapshot.py:832-872 and 229-345; lists.py:443-470`
Severity: medium
Description: `run_scan` calls `sync_protection_lists`, which returns a per-provider map whose values are counts or strings like `"error: ..."`, then only logs it and never inspects it. The function's own docstring says a scan relying on a failed whitelist should treat itself as degraded and calls a fail-open whitelist 'the worst kind of bug this tool can have', but nothing implements that degrade — `context.degrade` isn't even reachable from `run_scan`. The atomic swap preserves prior membership on failure (bounding steady-state damage), but on a first scan (no prior membership) or when a newly-added protection fails to sync, the WhitelistGate/CuratedListGate read empty membership, fail to fire, and the snapshot is NOT degraded and is fully executable.
Failure scenario: Operator creates a 'Never Reap' Plex collection and runs the first scan. The Plex provider throws; `sync_protection_lists` records `'error: ...'`; `run_scan` logs it and proceeds non-degraded. The membership table is empty, so every film the collection was meant to protect scores normally, several are condemned, and they are reaped despite being on the keep-list.
Recommended fix: Thread the sync outcome into degradation. After the `synced` map returns, collect any provider slug whose value starts with "error:" and whose resulting membership is empty, and pass those slugs into `snapshot_service.scan(...)` so it calls `context.degrade(...)` — blocking plan-building/execution, matching the IMDb dataset treatment. Minimally, for WHITELIST-kind providers with an error result and zero rows in protection_list_item, force the snapshot degraded.

**2. run_scan aborts the whole scan on an uncaught PlexError from plex.connect(), instead of degrading**
`src/reaper/services/scan_runner.py:314`
Severity: medium
Description: `plex_server = await plex.connect() if plex is not None else None` is not wrapped in try/except. `PlexClient.connect()` raises `PlexError` when Plex is unreachable. Radarr and Tautulli failures degrade the snapshot (loud, viewable, un-executable), and Plex is explicitly optional, yet a transient Plex outage here raises out of `run_scan` and crashes the scan rather than degrading.
Failure scenario: Plex restarts during the nightly scheduled scan. `plex.connect()` raises PlexError; `run_scan` has no handler; the scan job dies with an exception and no snapshot is produced. Scans appear to stop working whenever Plex flaps.
Recommended fix: Wrap line 314 in try/except PlexError. On failure set `plex_server=None` so `sync_protection_lists` skips Plex collections, AND surface the failure into the snapshot's degraded state. Critically, treat a skipped Plex whitelist as fail-closed: items a Plex 'Never Reap' collection would have protected must not become reap candidates because the list couldn't refresh (retain last-known whitelist, or mark the snapshot un-executable). Do not simply swallow the error.

**3. CI never builds the Docker image, so a non-building Dockerfile passes green**
`.gitea/workflows/ci.yml:9-68 (both jobs); demonstrated breakage at Dockerfile:31-32 vs pyproject.toml:9`
Severity: medium
Description: The `check` job runs ruff, mypy, pytest and `alembic check`; the `frontend` job runs `npm run build`. Neither ever runs `docker build`. The shipped artifact is the container, yet its buildability is untested in CI — exactly how the critical README.md/COPY-ordering breakage reaches `main` with all checks green.
Failure scenario: A change to pyproject.toml, COPY ordering, or the base image breaks `docker build` (as it currently is). CI stays green, the PR merges, and the failure is only discovered when an operator tries to build/pull the image.
Recommended fix: Add a CI job that runs `docker build .` on push/PR so the container is a verified artifact; this immediately catches the current README.md breakage. Optionally gate an image push on tags only.

**4. plex.tv login/authorization calls bypass BaseClient error mapping, defeating owns_server's fail-closed guard**
`src/reaper/clients/plextv.py:264-282 (resources); also 200-209 (_post) and 248-262 (account); guard at 297-302`
Severity: low
Description: `_post`, `account` and `resources` call `self._client.request/get(...)` directly instead of going through `_send`/`get_json`, so transport errors surface as raw httpx exceptions and non-JSON bodies as `json.JSONDecodeError` rather than `IntegrationError`. `owns_server` wraps `owned_servers` in `except IntegrationError` intending to fail closed to `False` on a plex.tv outage, but a real outage produces a raw httpx exception that this narrow except does not catch, so it propagates uncaught (typically a 500 in the login endpoint). The documented 'a plex.tv outage must not become an open door' is only honored for the rare IntegrationError case.
Failure scenario: plex.tv is briefly unreachable during a PIN flow. `resources()` raises `httpx.ConnectTimeout`; `owns_server`'s except does not match; the raw exception crashes the auth handler instead of cleanly denying access. A plex.tv maintenance HTML page (HTTP 200) makes `resources()`'s `.json()` raise ValueError, which also escapes.
Recommended fix: Route `account`, `resources`, and `_post` through the base client's error mapping so transport failures and non-JSON bodies become IntegrationError, making `owns_server`'s existing `except IntegrationError` reliably fire and return False. Optionally broaden the guard to `except (IntegrationError, httpx.HTTPError)` as defense in depth.

**5. expected_regret_rate/lift/summary can raise NotCalibratedError instead of degrading gracefully**
`src/reaper/engine/backtest.py:157-161`
Severity: low
Description: `expected_regret_rate` calls `self.prior.rate_for(d)` for every condemned item's dormancy without checking `self.prior.calibrated` or catching the exception. `RewatchPrior.rate_for` deliberately raises `NotCalibratedError` for any dormancy in a thin bucket. Because `lift`, `beats_random`, and `summary()` all funnel through `expected_regret_rate`, a single condemned item in a thin bucket crashes the whole report/arming path, even though `prior_is_derived`/`calibrated` exist to signal partial calibration as reportable.
Failure scenario: A derived prior has a thin 1095–1825d bucket (12 samples). A backtest condemns a film dormant 1200 days. `summary()` → `beats_random` → `lift` → `expected_regret_rate` → `rate_for(1200)` raises NotCalibratedError; the backtest endpoint 500s instead of showing calibrated-bucket numbers and flagging lift unavailable for that range.
Recommended fix: In `expected_regret_rate`, don't call `rate_for` unconditionally: either fall back to `rewatch_prior` unless `self.prior is not None and self.prior.calibrated`, or restrict averaged dormancies to calibrated buckets (catching NotCalibratedError per item), and have `lift`/`beats_random`/`summary` report "lift unavailable (uncalibrated buckets)" rather than propagating. Address before wiring `run`/`BacktestResult` to any endpoint.

**6. DataHorizonGate never enforces the horizon; it only abstains with a reassuring message**
`src/reaper/engine/gates.py:432-437 (gate); derivation clamp at services/snapshot.py:136, engine/backtest.py:306, engine/calibration.py:224`
Severity: low
Description: The gate is documented as guarding 'the single biggest mass-deletion vector', but `evaluate()` only fails closed when `days_observed_unwatched` is Unknown and otherwise always returns ABSTAIN with 'Old enough that Reaper's watch history covers it.' It performs no comparison against any horizon and can never PROTECT. The actual horizon defense lives entirely in the derivation of `days_observed_unwatched` (`reference = last_played or max(added_at, horizon)`). So the gate provides no defense-in-depth, its why-panel line asserts 'history covers it' unconditionally, and its Unknown fail-closed duplicates MinDormancyGate.
Failure scenario: An item added before Tautulli was installed, never played. `days_observed_unwatched` is Known (clamped to now-horizon). DataHorizonGate emits 'Old enough that Reaper's watch history covers it' even though its true pre-horizon watch status is unknowable; a future refactor that stopped clamping upstream would silently lose all protection while this gate still reports 'covered'.
Recommended fix: Either (a) make the docstring honest (the gate is only a fail-closed on Unknown dormancy that duplicates MinDormancyGate; the true guard is the `max(added_at, horizon)` clamp) and fold its Unknown fail-closed into derivation, or (b) give it teeth by passing `added_at + horizon` into Facts and having it PROTECT (or distinctly report) when `added_at` predates the horizon with no in-horizon plays. Low priority; current deletion safety is not affected.

**7. Crash-recovery is aspirational: idempotency keys and the SENT journal are written but never consumed**
`src/reaper/services/executor.py:384 (re-entry); 446-460 (rebuild without state filter); 761 + base.py 182-186; 528-535 (canary abort)`
Severity: low
Description: `execute()` permits re-running a run already in `EXECUTING` state, and each step is journalled `SENT` before its guarded call with a stable `idempotency_key`. Docstrings promise crash recovery and de-dup, but `idempotency_key` is never read, no reconciler consumes `SENT` steps, and `_send_movie` reads `movie_by_id` up front, which 404s for an already-deleted movie → `_fail`. Because that failed item becomes the canary on a re-run, re-executing a partially-completed run aborts immediately at the first already-deleted movie and never converges. It is fail-safe (nothing double-deletes) but the documented guarantee is unimplemented.
Failure scenario: A run deletes A and B, then crashes while sending C, leaving the run EXECUTING with A/B VERIFIED and C SENT. An operator re-triggers execute: manifest/caps pass, A is retried first, `movie_by_id(A)` 404s → FAILED → canary → ExecutionError aborts. C is never finished.
Recommended fix: Either (a) tighten `execute()` to reject `RunState.EXECUTING` (allow only PLANNED) and delete the docstring/idempotency_key claims that promise unimplemented recovery; or (b) actually implement a recovery pass: filter already-VERIFIED steps out of the rebuilt delete list, have a reconciler read SENT/idempotency_key to converge in-flight items, and make `_send_movie` treat an up-front 404 as already-done/VERIFIED only on an explicit recovery pass (never on a first run).

**8. poll_link consumes the pending PIN even when complete_link fails for a transient reason**
`src/reaper/services/plex_link.py:323-332 (delete in finally); root cause spans complete_link line 187 and _reachable lines 102-106`
Severity: low
Description: In `poll_link`, once a token is obtained the `finally` block unconditionally deletes the PendingPlexLogin row, then any exception from `complete_link` propagates. `complete_link` calls `_reachable`, which raises `PlexLinkError` when none of the server's advertised connections answer the probe — a transient condition. Because the pending is already consumed, the next poll hits 'This link request is no longer valid' and the operator must restart the whole plex.tv PIN flow even though sign-in succeeded.
Failure scenario: Owner completes PIN approval; the backend obtains the token; at that instant Plex is briefly unreachable (mid-restart), so `_reachable` raises. The finally deletes the pending, the request 500s, the browser's next poll gets 'no longer valid', and the owner must re-run the whole sign-in.
Recommended fix: Consume the pending only on success or a definitively permanent refusal (owns 0 or >1 servers). For a retryable `_reachable` failure, distinguish it (a dedicated retryable subclass, or catching PlexLinkError from `_reachable` specifically) and leave the row intact so the browser can re-poll the still-valid PIN. Preserve delete-on-success to prevent token replay.

**9. DiscordNotifier.post() lets httpx.InvalidURL escape, breaking the "never raises into a scan/plan/run" guarantee**
`src/reaper/notify/discord.py:73-80`
Severity: low
Description: `post()` only catches `httpx.HTTPStatusError` and `httpx.HTTPError`. `httpx.InvalidURL` is a direct subclass of Exception (not an HTTPError), so a malformed webhook makes `client.post(self._url)` raise InvalidURL, bypassing both except clauses and propagating through `announce_leaving_soon()` into `leaving_soon.sync()` at line 130. The module docstring promises nothing here can raise into a scan, plan, or run.
Failure scenario: Operator sets a webhook with embedded newlines/control characters or a malformed IPv6 host. A Leaving Soon sync applies the Plex label and then raises `httpx.InvalidURL` at notify time, so POST /api/leaving-soon/sync returns 500 after the mutation already happened.
Recommended fix: Broaden the final catch in `post()` from `except httpx.HTTPError` to `except Exception as exc:` (logging only outcome + `type(exc).__name__`, never `str(exc)`/the URL, since the token lives in the path). Optionally validate/strip the webhook in `build_notifier()` and return None on an unusable URL.

**10. No configuration surface passes old keys to SecretBox, so key-rotation is unreachable and switching REAPER_SECRET_KEY bricks credentials**
`src/reaper/main.py; crypto.py:36-40 (SecretBox *old_keys unused); main.py:76 and cli.py:158; config.py:75; secrets.py:41-47`
Severity: low
Description: SecretBox is always constructed with a single key. config.py exposes only `secret_key` — no field/env for prior keys — and `SecretBox.rotate()` has no caller. So the MultiFernet multi-key capability the docstring advertises cannot be exercised, and following secrets.py's own advice to move to a managed REAPER_SECRET_KEY makes all stored credentials undecryptable.
Failure scenario: Operator runs for months on the auto-generated `data/secret.key`, then sets REAPER_SECRET_KEY to a fresh value from a secret manager. On next boot SecretBox holds only the new key; every `api_key_enc`/`token_enc` fails to decrypt, all integrations break, and the only recovery is discovering REAPER_SECRET_KEY must equal the exact old secret.key contents.
Recommended fix: Add a settings field for prior keys (e.g. REAPER_SECRET_KEY_OLD, comma-separated) and thread it as `*old_keys` into SecretBox at main.py:76 and cli.py:158 for a two-key window. At minimum, document explicitly that setting REAPER_SECRET_KEY after first boot must use the existing key contents (or supply the old key alongside). Optionally wire `box.rotate()` to lazily re-encrypt.

**11. Discord 429 rate-limit responses are dropped without honoring Retry-After or retrying**
`src/reaper/notify/discord.py:70-75`
Severity: low
Description: A Discord HTTP 429 is caught as a generic HTTPStatusError, logged as 'discord.rejected', and the embed discarded. Retry-After is ignored and there is no retry. Discord rate-limits per webhook, so bursts of announcements can be silently lost even though the request would succeed shortly after.
Failure scenario: Several Leaving Soon syncs fire in quick succession and hit the per-webhook limit; the 429'd messages are dropped entirely instead of retried, so users are never warned before titles are deleted.
Recommended fix: Special-case 429 before the generic handling: read Retry-After, sleep a bounded delay (`min(retry_after, small_max)`), and retry the post once. Keep it best-effort — still catch all errors, still never raise into a scan/plan/run, still never log the URL. On repeated failure, fall through to the existing log and return False.

**12. SafetyBanner renders nothing when the health fetch fails, hiding the only always-on delete/read-only indicator**
`frontend/src/App.tsx:34-35`
Severity: low
Description: SafetyBanner reads the ['health'] query and does `if (!data) return null;`. `data` is undefined not only on first load but on any error of `api.health` (network drop, 500, backend restart). The docstring promises the regime is stated 'always'. On a health-endpoint error the banner silently disappears, losing the safety indicator precisely when the backend is misbehaving.
Failure scenario: The backend hiccups or /health 500s while the dashboard is open. `data` becomes undefined, SafetyBanner returns null, and the 'Read-only'/'Deletion is on' banner vanishes; the operator cannot tell which regime is active without opening Settings.
Recommended fix: Read the query's error/loading flags. While loading, render a neutral placeholder; when isError (or data absent after settling), render a caution-styled "Safety state unknown — could not reach the server" instead of returning null. (React Query retains last-known data through refetch errors, so this mainly matters at initial mount when /me succeeds but /health fails.)

**13. request() assumes every successful response has a JSON body, turning any empty 200/204 into an opaque SyntaxError**
`frontend/src/api.ts:464`
Severity: low
Description: The shared `request<T>` helper ends with `return (await response.json()) as T;` with no guard for an empty body; every api.* call routes through it. Today all endpoints return JSON, so this is latent, but the moment anyone adds a 204 No Content or empty-body 200, the call rejects with a raw 'Unexpected end of JSON input' SyntaxError rather than the clean ApiError the rest of the code surfaces. The error path already tolerates this with `.catch(() => null)`; the success path does not.
Failure scenario: A future DELETE/logout-style endpoint returns 204 with no body. `response.json()` throws SyntaxError; the react-query error is a cryptic parser message with no status code, and UI relying on ApiError.status/message shows nothing useful.
Recommended fix: Mirror the error branch: `const text = await response.text(); return (text ? JSON.parse(text) : undefined) as T;`, or short-circuit on `response.status === 204`. Purely defensive; can be deferred until an empty-body endpoint is introduced.

**14. Authed swallows a setup-status query error and silently drops the operator onto the dashboard, skipping the wizard**
`frontend/src/App.tsx:220-233`
Severity: low
Description: Authed does `const { data: setup, isLoading } = useQuery({ queryKey: ['setup'], queryFn: api.setupStatus })` and gates the wizard on `if (setup && !setup.complete && !skipped)`. It never inspects the error state. If `api.setupStatus` fails, `setup` stays undefined, `isLoading` goes false, and the guard falls through to `<Dashboard>` — a genuinely unconfigured fresh install is dropped onto an empty dashboard with no error shown.
Failure scenario: A brand-new install where the setup-status call errors once (backend warming up). The wizard condition short-circuits on `setup` being undefined, Dashboard renders with no instances/scan configured, and the operator has no indication setup was needed.
Recommended fix: Read isError/error from the setup query. Treat unknown setup status as "setup needed" so a fresh install still lands on SetupWizard (e.g. `if (isError || (setup && !setup.complete)) return <SetupWizard .../>` while honoring `skipped`), or render an explicit error/retry state.

**15. Settings PlexPanel link polling has no timeout — polls forever and disables the button indefinitely**
`frontend/src/components/Settings.tsx:336`
Severity: low
Description: `startLink` opens a 2s `setInterval` that stops only on `status === 'ok'`, a thrown error, or unmount. There is no deadline. If the user opens the Plex approval tab and never approves, the poll runs every 2s for as long as Settings is mounted, leaving "Link with Plex" permanently disabled with no retry. Login's PlexButton implements the correct 5-minute deadline that this one lacks.
Failure scenario: Operator clicks "Link with Plex", gets distracted, never approves the PIN. Settings keeps POSTing /plex/link/poll every 2 seconds indefinitely, and the button reads "Waiting for Plex…" disabled until a full page reload.
Recommended fix: Mirror Login.tsx's PlexButton: capture `const deadline = Date.now() + 5*60*1000` before the interval, and at the top of the callback `if (Date.now() > deadline) { clearInterval + setLinking(false) + setMessage("Plex sign-in timed out. Please try again."); return; }`. Optionally add a Cancel button.

**16. Bulk override uses Promise.all — one failed request discards success handling for the rest**
`frontend/src/components/ReviewQueue.tsx:677-686`
Severity: low
Description: The `bulk` mutation maps selected keys to per-key requests and awaits `Promise.all`. Promise.all rejects on the first failure, so `onSuccess` (which invalidates ["candidates"] and clears the selection) never runs even though other requests already succeeded server-side. The UI is left showing a stale, still-selected list that no longer matches the backend.
Failure scenario: Operator selects 50 items and clicks Spare; item #12's request 500s. All 50 fire, ~49 succeed, but the mutation rejects: the selection isn't cleared and the queue isn't refreshed, so the operator sees 50 still-selected rows with old verdicts and an error, unsure what took effect.
Recommended fix: Switch the mutationFn to `Promise.allSettled`, then always invalidate ["candidates"] and clear the selection regardless of outcome, and surface a count of failures (e.g. "3 of 50 could not be updated"). Optionally keep only the failed keys selected for retry.

## 6. Security

**1. Local login has no rate limiting / brute-force protection, unauthenticated Argon2id is a CPU-exhaustion vector, and a code comment falsely claims a rate limit exists**
`src/reaper/api/auth.py:211-224 (POST /local); src/reaper/services/login.py:279-319 (no throttle); src/reaper/auth/passwords.py:18 (false comment); src/reaper/api/middleware.py:51-61 (CSRF bypass); admin_password.py:22 (MIN_PASSWORD_LENGTH=8); /recover at auth.py:242`
Severity: medium
Description: The local login route and `/recover` have no per-IP/per-username throttling, backoff, or lockout; a full-tree grep finds no limiter or lockout anywhere. `login_local` runs a full Argon2id `verify_password` on every request (deliberately against a dummy hash even for nonexistent users), so an unauthenticated caller can force heavy CPU work per request. The CSRF middleware does not help — the required header is a fixed literal (`x-reaper-csrf: 1`) any scripted attacker sets trivially. Operators may set an 8-char password with no complexity rules. Worse, passwords.py:18 asserts "Long enough that online guessing is hopeless, and we rate-limit anyway," which is untrue and misleads maintainers. This local account is the always-available anti-lockout path and the credential that arms deletion.
Failure scenario: Reaper is port-forwarded or on a shared LAN. An attacker scripts thousands of POST /api/auth/local requests with `x-reaper-csrf: 1` and dictionary passwords; each triggers a full Argon2id hash, pinning CPU and denying service, while the same loop brute-forces a weak-but-compliant password ('Summer24') with no lockout — then arms and executes library deletion.
Recommended fix: Add per-IP and per-username attempt throttling with exponential backoff and temporary lockout on POST /api/auth/local, returning 429 past a threshold (in-memory or DB-backed counter). Cap concurrent in-flight Argon2 verifications with a bounded semaphore. Emit a warning-level log on repeated failures rather than only info-level 'login.local_rejected'. Add a modest per-IP cap on /recover (it redeems a random single-use token, so lower priority). Correct or remove the false "and we rate-limit anyway" clause in passwords.py:18. Optionally raise MIN_PASSWORD_LENGTH or add a weak/breached-password check.

**2. Changing an admin's password does not revoke that admin's existing sessions; the primitive to do so exists but is never called**
`src/reaper/services/admin_password.py:91 and admins.py:84 (missing revocation); route settings.py:440-454; resolve_session sessions.py:58-93; unused primitive + wrong docstring sessions.py:103-106`
Severity: medium
Description: Both password-reset paths — `admin_password.set_password()` and `auth/admins.set_password()` — rewrite `password_hash` but never invalidate existing AuthSession rows. `resolve_session()` validates a cookie purely on token_hash, expiry, and user.is_active, with no dependency on password_hash, so every previously issued cookie stays valid for the full 30-day SESSION_TTL after a password change. `sessions.close_all_for_user()` is exactly the 'sign out everywhere' primitive that would fix this but has ZERO callers, and its docstring falsely claims it is 'also used implicitly when an admin is deactivated' (deactivate() never calls it either).
Failure scenario: An admin suspects their cookie was stolen and resets the admin password. The attacker's stolen cookie continues to authenticate for up to 30 days because no session row was touched. The operator believes they locked the attacker out; they have not.
Recommended fix: After a successful password change, call `sessions.close_all_for_user(session, user.id)` before commit in both write paths (or in the callers that own the commit: settings.py:449 and cli.py:120). Consider preserving the acting admin's own token (re-mint via open_session) when self-initiated. Add `close_all_for_user` to `deactivate()` for defense in depth, and fix or delete the inaccurate docstring at sessions.py:104-105.

**3. secret.key is created with default (umask) permissions and only chmod-ed afterward, contradicting the code's own 0600-from-the-outset claim**
`src/reaper/secrets.py:66`
Severity: medium
Description: Lines 63-65 claim the key is created with restrictive permissions from the outset to avoid a world-readable window. But `Path.open('x'|'w')` takes no mode and creates the file with the process default (`0o666 & ~umask`), then `_ensure_owner_only()` chmods to 0600 only after the key is written. The exact window the comment claims to avoid is left open on first boot.
Failure scenario: On a host with the common umask 0022, first boot writes secret.key as 0644 (world-readable). Between the write and the chmod, any local unprivileged user can read the master key that decrypts every stored Plex account token and Sonarr/Radarr/Tautulli API key.
Recommended fix: Create the file atomically with owner-only permissions: `fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)`, then `os.write`/`os.close`. Optionally wrap in a saved/restored `os.umask(0o077)` to guarantee 0o600 regardless of process umask. Keep `_ensure_owner_only()` as a belt-and-braces step for the reuse path and fix the comment. The "w" fallback branch should likewise chmod before writing.

**4. Committed uv.lock is ignored by both the image build and CI; installs resolve unpinned >= floors, and base images are floating tags**
`Dockerfile:32 and :37; .gitea/workflows/ci.yml:24; base-image tags at Dockerfile:4 and :20`
Severity: medium
Description: A 268 KB uv.lock is committed, but nothing installs from it: the Dockerfile uses `uv pip install --system --no-cache .` / `-e .` and CI uses `uv pip install --system -e ".[dev]"`, all of which resolve the `>=` floors in pyproject.toml fresh at build time. So builds are not reproducible, CI tests one resolved set while the image may ship a newer untested set (including newer transitive cryptography/httpx/plexapi that handle credentials and issue deletes), and both base images are floating tags, not digest-pinned.
Failure scenario: A compromised or breaking release of a transitive dependency is published; the next `docker build`/CI run silently pulls it because only a `>=` floor is enforced, differing from what was last tested against the lock.
Recommended fix: Install from the lockfile. In the Dockerfile, COPY uv.lock and use `uv sync --frozen --no-dev` (or `uv export --frozen --no-dev -o requirements.txt` then `uv pip install -r requirements.txt`); in CI use `uv sync --frozen` and add a `uv lock --check` step so a stale lock fails the build. Digest-pin both base images.

**5. Seerr client is built with TLS verification disabled (verify=False), exposing the decrypted API key to MITM**
`src/reaper/api/fairness.py:56-57`
Severity: low
Description: The fairness endpoint constructs SeerrClient with `verify=False`, disabling TLS certificate verification; `box.decrypt(seerr_row.api_key_enc)` is sent over an unvalidated connection. This is a repo-wide pattern (also services/instances.py:203 and services/scan_runner.py:189) and inconsistent with the Tautulli and *arr clients in this same file, which use `verify=True`. An operator cannot see or override this silent insecure default.
Failure scenario: An operator points Reaper at an https Seerr URL over an untrusted network segment. An on-path attacker presents any certificate; verify=False accepts it, and the decrypted Seerr API key plus full request/response is captured. With verify=True this would fail closed.
Recommended fix: Make TLS verification a per-instance configurable option defaulting to `verify=True`, so operators with self-signed internal Seerr certs opt in explicitly. Thread the instance's configured verify flag through the three Seerr construction sites instead of the hardcoded `False`, matching the Tautulli/*arr clients.

**6. Recovery link places the single-use token in a URL query string, reintroducing the log exposure the module deliberately avoids**
`src/reaper/auth/recovery.py:57`
Severity: low
Description: `mint_recovery_token()` builds the link as `f"{base_url.rstrip('/')}/recover?token={plaintext}"`. The module goes out of its way to avoid shipping the token to a log aggregator, printing it only to the console, but putting it in a query string undoes much of that care: the GET for the SPA page carries `?token=<secret>`, commonly recorded by reverse-proxy access logs, browser history, and Referer headers. The token is single-use, 15-minute, and obtaining it requires host access, but the recovery admin session it grants is full-power.
Failure scenario: Operator runs Reaper behind nginx with default access logging and clicks the recovery link. nginx writes `GET /recover?token=<valid-token>` to access.log, shipped to a central store with looser access than container stdout. Anyone with read access who reaches /recover within 15 min gains an admin session.
Recommended fix: Keep the token out of the URL. Preferred: point the link at `/recover` (no query param) and have the operator paste the token into a field that POSTs to /recover. If the query-string convenience is kept, at minimum document that fronting-proxy access logs may capture the token from the request line. The frontend already strips the token post-redemption; the residual exposure is the initial GET request line — moving it off the URL closes it.

**7. Encryption key is derived from the secret with a single unsalted SHA-256 (no KDF stretching), and secret_key has no length/entropy guard**
`src/reaper/crypto.py:23-26 (derivation); secrets.py:41-47 (unchecked acceptance in resolve_secret_key); config.py:75 (schema declaration, no validator)`
Severity: low
Description: `_derive_fernet_key()` turns REAPER_SECRET_KEY into the Fernet key with one unsalted SHA-256 — fine for the high-entropy auto-generated `token_urlsafe(32)` key, but nothing enforces entropy on an operator-supplied key, and there is no key-stretching. `config.py` declares `secret_key` as an optional SecretStr with no minimum-length/entropy validation, and `resolve_secret_key` accepts any non-empty string. The crypto docstring frames the threat as a DB copied into a backup/issue report/support thread — exactly where an offline dictionary attack applies.
Failure scenario: Operator sets REAPER_SECRET_KEY='myplexserver2024' (or a memorable passphrase) and later shares the DB in a support thread. An attacker runs a wordlist through SHA-256→Fernet against any `api_key_enc` (billions/sec on a GPU), recovering the full-power Plex account token and *arr keys in seconds.
Recommended fix: Derive the Fernet key with a real KDF (scrypt or Argon2id) over the secret plus a persisted per-install salt, replacing the bare SHA-256. Do it before first release (pre-release migrations acceptable); MultiFernet allows old-key decryption during transition. Also add a minimum-length/entropy guard in `resolve_secret_key` (or a validator on `Settings.secret_key`) that at least warns on a short/low-entropy operator key, pointing at the `secrets.token_urlsafe(32)` generator. The auto-generated random-key path can stay fast.

**8. redact_secrets only scrubs top-level string values, missing secrets nested in dict/list values**
`src/reaper/logging.py:50-56`
Severity: low
Description: The processor redacts a top-level key whose name is in `_SECRET_KEYS` and applies the query-string regex only to top-level values that are `isinstance(value, str)`. A secret nested inside a dict or list value, a bytes value, or a credential-bearing URL under a non-listed key without '=' passes through in cleartext. Described as the last line of defense for secrets, so the gap matters even though no current call site triggers it.
Failure scenario: A call like `log.info('arr.request', params={'apikey': key})` or logging a headers dict passes the secret as a nested dict value; redact_secrets leaves it because the value is a dict and 'params' isn't a secret key name. (No such call site exists today — defense-in-depth.)
Recommended fix: Optionally harden as defense-in-depth: recurse into dict and list values, redacting by key name at any depth and applying `_SECRET_QS` to every str leaf, decoding bytes leaves before matching. A simpler alternative is a lint/convention that secrets are only ever logged as top-level kwargs. Low priority.

**9. Poster proxy relays arbitrary upstream image content-type (incl. image/svg+xml) same-origin with no nosniff**
`src/reaper/api/poster.py:66-71 (passthrough + missing nosniff); tautulli.py:222 (substring guard admits image/svg+xml)`
Severity: low
Description: The poster endpoint returns bytes fetched from Tautulli's pms_image_proxy with the upstream Content-Type passed straight through. The upstream guard in `tautulli._image` only checks that 'image' appears in the content-type, which `image/svg+xml` satisfies. The response is served from Reaper's own origin (/api/poster/{rating_key}) with no `X-Content-Type-Options: nosniff`. An SVG opened/navigated directly renders same-origin, and any embedded `<script>` executes in Reaper's origin; the missing nosniff also permits MIME sniffing. Exploitation requires a malicious/compromised upstream image (normally trusted raster art), so this is defense-in-depth.
Failure scenario: A compromised Plex artwork entry serves image/svg+xml with script for a rating_key. An authenticated admin opens /api/poster/<key> directly; the SVG executes JavaScript in Reaper's same origin, able to act as the admin against the API.
Recommended fix: Add `"X-Content-Type-Options": "nosniff"` to the poster Response headers, and tighten the guard in tautulli.py:222 from a `"image" in ctype` substring test to a raster allow-list (image/jpeg, image/png, image/webp), rejecting image/svg+xml. Optionally normalize the served media_type to that allow-list in poster.py.

## 7. UI/UX Consistency

**1. White text on --accent fails WCAG AA contrast in dark mode across the primary CTAs**
`frontend/src/index.css:315 (button.primary), 2137 (.select-toggle.active), 2229 (.select-done), 393 (.user-avatar-fallback)`
Severity: medium
Description: Multiple primary action surfaces set `color: #fff` on `background: var(--accent)`: `button.primary`, `.select-toggle.active`, `.select-done`, and `.user-avatar-fallback`. In dark mode `--accent` becomes `#818cf8`, a light periwinkle; white text on it is only ~3.0:1 — below the 4.5:1 AA threshold for the ~15px, weight-550 button text. These are the most-clicked controls (Scan, Save, Reap, bulk-select actions).
Failure scenario: An operator on system dark theme sees the primary 'Save policy'/'Reap' buttons and the armed multi-select toggle show white label text on light-indigo at ~3:1. Low-vision users or users on a bright screen struggle to read the single most important action.
Recommended fix: Add an `--accent-ink` token mirroring `--plex-ink`: `--accent-ink: #fff` in light `:root`, a dark navy (e.g. `#10122b`, ~7:1 against #818cf8) in the dark block. Replace the four `color: #fff` declarations with `color: var(--accent-ink)`. (Darkening dark-mode `--accent` instead would also affect borders/rings that read fine, so the ink token is lower-risk.)

**2. Tab-style navigation is rendered three different ways across the app's primary surfaces**
`frontend/src/components/ReviewQueue.tsx:771-783 (.tabs container) with index.css:742-747 (.tabs border-bottom) vs index.css:346-365 (.tab), 214-221 (.views), 2338-2356 (.settings-tab), 1742-1767 (.seg)`
Severity: low
Description: The same conceptual control — a horizontal row of tabs switching sub-views — has three incompatible treatments. The masthead (`.views`+`.tab`) is a rounded pill group on a surface-2 track. ReviewQueue reuses the identical `.tab` pill but drops it into a `.tabs` container with `border-bottom: 1px solid var(--border)`, so the active pill floats on an underline meant for underline-style tabs. Settings (`.settings-tab`) uses a genuine underline tab. PolicyEditor adds a fourth pattern with `.segmented`/`.seg`.
Failure scenario: A user on Review sees rounded pill tabs on a horizontal rule; on Settings the tabs become square underline tabs; the Policy media toggle is yet another pill 'segmented' control. The inconsistent shapes make it non-obvious they're the same control, and the ReviewQueue pill-on-a-border looks unfinished.
Recommended fix: Pick one tab paradigm. Simplest: remove `border-bottom` from `.tabs` so ReviewQueue's `.tab` pills read like the masthead pill group (optionally wrap `.tabs` in the same surface-2 rounded track). Alternatively convert both masthead and ReviewQueue to the `.settings-tab` underline style. Keep `.segmented`/`.seg` for the binary media toggle only. Longer term, consolidate `.tab`, `.settings-tab`, and `.seg`.

**3. Four parallel error/warning message components with no shared system**
`frontend/src/index.css:261-278, 584, 1374-1391`
Severity: low
Description: Inline error/warning messaging is fragmented across four visually distinct treatments used interchangeably. `.error` (red text 0.88rem) is used in many components; `.auth-error` (red text 0.85rem, different margins) is used only in Login for the same concept; `.warn` (amber boxed) appears in Login; `.notice`/`.notice-error`/`.notice-warn` (boxed, bordered) appear only in PolicyEditor. So a validation failure is a red bordered box in Policy, plain red text in Settings, and a slightly-differently-sized plain red text on Login.
Failure scenario: The same class of message looks different on every screen — boxed and bordered in PolicyEditor, bare red text in Settings/Fairness, near-identical-but-subtly-different bare red text in Login. Users get no consistent cue for 'error' vs 'warning', and `.error`/`.auth-error` are functional duplicates that will drift.
Recommended fix: Consolidate to one severity-typed notice system, using `.notice`/`.notice-error`/`.notice-warn` as the base. Migrate `.error` and `.auth-error` to `.notice.notice-error` (deleting `.auth-error`), and fold `.warn`/`.warn.danger` into `.notice-warn`/`.notice-error`.

**4. Native browser confirm() dialog used for deletion in Settings, contradicting the app's custom confirmation UX**
`frontend/src/components/Settings.tsx:239`
Severity: low
Description: Removing a service instance triggers an unstyled native `confirm()` dialog. Everywhere else the app builds custom, styled confirmation surfaces (the reap modal, the local-login sheet, the inline password-confirm arm form). A raw OS-chrome alert in an otherwise carefully themed app is a UX disjoint, and it's the only confirmation ignoring light/dark theming and app typography.
Failure scenario: A user removing a Radarr instance gets a raw 'localhost says…' dialog with default OS buttons — no theming — while the far more consequential 'turn deletion on' and 'reap now' flows use bespoke in-app confirmations.
Recommended fix: Replace `window.confirm()` with an inline confirm affordance consistent with the app — a two-step "Remove" → "Confirm remove" toggle mirroring the Safety arm flow, or reuse the `.modal` pattern. Cosmetic/consistency only.

**5. Heading hierarchy is inconsistent: GracePanel jumps to <h4>, and ReviewQueue has no <h2> while every other main view does**
`frontend/src/components/GracePanel.tsx:95,111; ReviewQueue.tsx:770-785 (missing h2), 418,519 (card-title h3)`
Severity: low
Description: GracePanel emits `<h4 class="grace-heading">`, but the stylesheet only styles h1/h2/h3 — h4 is unstyled and skips h3, breaking the outline. Every primary view leads with an `<h2>` (Policy, Fairness, Settings panels, the simulator) but ReviewQueue has no top-level heading (it opens into `nav.tabs` then `.blurb`). ReviewQueue also repurposes `<h3>` for every card title, overriding the section-label h3 style and flooding the outline with dozens of card-title h3s.
Failure scenario: Screen-reader/outline navigation is uneven: Review has no view-level heading and is full of h3 card titles, Grace uses an h4 that skips h3, other views use clean h2→h3. A user relying on heading navigation cannot land on a 'Review' heading the way they can on 'Policy' or 'Fairness'.
Recommended fix: Give ReviewQueue a view-level `<h2>` (e.g. "Review queue") before the tabs nav. Change GracePanel's two `<h4>` headings to `<h3>` (keeping `.grace-heading` if the smaller size is wanted). Optionally demote `.card-title` from `<h3>` to a styled non-heading element.

**6. Fixed two/four-column grids in Settings and the grace panel have no mobile breakpoint**
`frontend/src/index.css:2459-2463 (.add-grid); also 2418-2425 (.instance-edit) and 1556-1564 (.grace-list li)`
Severity: low
Description: The `@media (max-width: 900px)` collapses only apply to `main.split`, `.editor`, and `.why`. Several fixed multi-column grids never collapse: `.add-grid` (`1fr 1fr`), `.instance-edit` (`1fr 1fr`), and `.grace-list li` (`1fr auto 5rem 4rem`). Inside `.panel` (max-width 760) these are the add/edit-instance forms, which stay two-column on a 375px phone.
Failure scenario: An operator adds a Radarr/Sonarr instance on a phone: the URL and API-key inputs sit in two ~150px columns too narrow to read pasted values; the edit form and grace rows crowd on the same width. Functional but hard to use, unlike the queue/editor which reflow.
Recommended fix: Add a mobile breakpoint (e.g. `@media (max-width: 640px)`) setting `.add-grid, .instance-edit { grid-template-columns: 1fr; }` and collapsing/wrapping `.grace-list li`. Alternatively use auto-fit/minmax.

**7. Reap-confirm modal uses max-height:88vh (not dvh), risking clipped actions under mobile browser chrome**
`frontend/src/index.css:2794`
Severity: low
Description: `.modal` sets `max-height: 88vh`. On mobile browsers `vh` resolves to the large viewport (toolbar hidden) while the visible area is smaller, so a centred modal sized to 88vh extends above and below the visible region. The `.why` mobile sheet was deliberately switched to `100dvh` to avoid this, but the reap confirmation modal — the one modal that deletes data — still uses `vh`.
Failure scenario: On mobile Safari/Chrome with the address bar shown, a reap plan with several items makes the modal ~88vh tall and centred; the confirm-phrase input and Reap/Cancel actions (and header) render partly outside the visible viewport on first paint.
Recommended fix: Change `.modal` `max-height: 88vh` to `88dvh` for consistency with the `.why` fix. Optionally add a `@media (max-width: 900px)` refinement, but the single-property change suffices.

**8. Form-field label wrappers use three different conventions, so labeled inputs look different per panel**
`frontend/src/components/Settings.tsx:583-641`
Severity: low
Description: Labeled fields are built three ways with three label typographies. Services uses `.field-sm` with a bare `<span>` (0.82rem/muted). Login and PolicyEditor sliders use `.field` + `.field-label` (0.88rem/weight-500/full-color). Limits uses bare `<label>` via `.caps-grid label` (0.8rem/muted). Three sibling panels within the same Settings screen render field labels at different sizes, weights, and colors.
Failure scenario: Within Settings alone, the 'Name'/'Address' labels (Services, muted 0.82rem) differ from 'Most titles per run' labels (Limits, muted 0.8rem, different wrapper), and Login's 'Username' label is 0.88rem weight-500 full-color. The same 'label-above-input' element is visibly inconsistent between adjacent panels.
Recommended fix: Standardize on one field-wrapper convention across Settings, Login, and PolicyEditor. Collapse `.field-sm span`, `.field-label`, and `.caps-grid label` into a single shared label style. Prioritize the more visibly divergent case (0.88rem weight-500 full-color vs the 0.8-0.82rem muted variants).

**9. Loading states alternate between a spinner and plain muted text with no rule**
`frontend/src/components/PolicyEditor.tsx:621 (plus ReviewQueue.tsx:862; Settings.tsx:296,565,737; GracePanel.tsx:48; Fairness.tsx:87; spinner usages App.tsx:225,250 and Login.tsx:107; spinner CSS index.css:443-457)`
Severity: low
Description: The app ships a spinner (`.spinner`/`.spinner-lg`) used for the auth gate and Plex wait state, but every data panel falls back to plain muted text: 'Loading policy…', 'Loading…', 'Loading grace…', and a bespoke sentence 'Reading requests from Seerr and history from Tautulli…'. So loading is sometimes a spinner, sometimes 'Loading…', sometimes '<noun> loading…', sometimes a full sentence.
Failure scenario: Navigating between views, a user sees a spinner on auth, then plain 'Loading…' on Review, then 'Loading policy…' on Policy, then a full sentence on Fairness. No shared loading affordance; text-only states have no motion cue.
Recommended fix: Standardize the data-panel loading affordance. Either reuse `.spinner` across all panels, or normalize the copy to a single phrasing (e.g. all "Loading…"). (The third Settings occurrence is at line 737, not 738.)

**10. Two adjacent 'Test connection' buttons use different loading labels ('…' vs 'Testing…')**
`frontend/src/components/Settings.tsx:228`
Severity: low
Description: The saved-instance Test button shows a bare ellipsis '…' while pending (`{testSaved.isPending ? "…" : "Test"}`), whereas the add-instance Test button in the same file shows the full 'Testing…' label. Every other async button uses verb+ellipsis ('Adding…', 'Saving…', 'Signing in…', 'Scanning…', 'Planning…', 'Marking…'). The lone '…' is an outlier for the identical action sitting next to its 'Testing…' sibling.
Failure scenario: A user testing an already-saved instance sees the button collapse to a lone '…', while the visually identical Test button in the Add-service form below says 'Testing…'. The mismatch looks like a bug.
Recommended fix: Change Settings.tsx:228 to `{testSaved.isPending ? "Testing…" : "Test"}` to match the app-wide verb+ellipsis convention.

## 8. Missing Discord Config in UI

**1. The Discord webhook — the only real notification channel — has no UI control and can be set solely via an env var**
`frontend/src/components/Settings.tsx:18-26 (missing UI panel); config.py:82 (backend root cause)`
Severity: medium
Description: The sole notification setting is `discord_webhook: SecretStr | None` (config.py:82), consumed by `build_notifier(settings)` (notify/discord.py:114) and the `/api/leaving-soon/sync` endpoint (api/leaving_soon.py:72). Its docstrings call it "the *real* notification channel" — the Plex "Leaving Soon" label reaches only users who pinned the library, so Discord is the one channel that actually warns people before deletion. Yet there is NO UI control anywhere: the Settings shell exposes exactly five panels (services, plex, jobs, limits, safety), none touching notifications; api.ts has no notifications endpoint; the only Discord string in the frontend is a read-only status pill `{mark.data.notified && " · Discord notified"}` (GracePanel.tsx:84). Because `build_notifier` reads only `settings.discord_webhook`, the ONLY way to configure it is the undocumented env var `REAPER_DISCORD_WEBHOOK`. This also violates config.py's own stated architecture — bootstrap `Settings` is reserved for pre-DB/kill-switch concerns, while credentials like instance API keys "live in the database, Fernet-encrypted, and are edited in the web UI." The webhook is a credential (its token lives in the URL path), is not needed before the DB, and is not a kill switch, so by the codebase's own rule it belongs in the DB and UI.
Failure scenario: An operator stands Reaper up entirely through the web UI and never edits config.py or sets REAPER_DISCORD_WEBHOOK because nothing in the UI or docs mentions it. Reaper begins condemning movies; the grace-period 'leaving soon' warning that should reach the household via Discord is silently never sent (`build_notifier` returns None), so the primary safety warning reaches no one and media is deleted after grace with users blindsided. The operator has no in-product way to discover or fix this.
Recommended fix — full specification:
- **What it controls:** the Discord webhook URL used by `build_notifier` to post the "Leaving Soon" grace-window announcements.
- **Where it belongs:** a new **Notifications** panel registered in `Settings.tsx` (the sixth panel alongside services/plex/jobs/limits/safety).
- **Control type / label / help / validation:** a single masked, write-only secret text input labeled **"Discord webhook URL"**, mirroring the existing write-only-secret pattern used for instance API keys. Help text: "Posts a heads-up to your Discord channel before titles are deleted. The entire URL is a secret — paste the full `https://discord.com/api/webhooks/…` URL." Show a **"Discord connected"** status (derived from a `has_webhook` boolean) rather than echoing the stored value, with **Save**, **Test** (posts a sample embed), and **Remove** actions. Client- and server-side validation must require an https URL whose host is a Discord webhook endpoint (`discord.com`/`discordapp.com` `/api/webhooks/...` path); reject other hosts and strip whitespace.
- **DB-backed vs env-only:** store the webhook **DB-backed, Fernet-encrypted**, exactly like an instance API key. Keep `REAPER_DISCORD_WEBHOOK` as a **first-boot seed only** (migrated into the DB on first run), not the primary configuration surface.
- **API change:** change `build_notifier` to read the stored (session-fetched) webhook rather than `Settings`, and update the caller in `api/leaving_soon.py:72`. Add `GET`/`PUT`/`POST-test` routes under `api/settings.py` exposing only `has_webhook` (never the URL). Add the corresponding `api.ts` helpers and register the `notifications` Panel in `Settings.tsx`.
Severity is medium (not high): the channel IS configurable via env var, destructive actions require explicit arming, and the Plex label is a weaker secondary warning — a real UX/architecture gap, not a correctness defect.

**2. .env.example omits REAPER_DISCORD_WEBHOOK, hiding the only currently-working way to configure notifications**
`.env.example: append after line 34 (RECOVERY); config.py:82 (discord_webhook) and config.py:97 (allow_unarmed_leaving_soon)`
Severity: low
Description: Until a UI control exists (finding above), the sole way to enable Discord notifications is the env var `REAPER_DISCORD_WEBHOOK`, yet `.env.example` never mentions it — it documents SECRET_KEY, DATA_DIR, HOST, PORT, LOG_LEVEL, LOG_JSON, DESTRUCTIVE_ACTIONS_ENABLED, RECOVERY, and seed integration vars, and stops. The same omission applies to `REAPER_ALLOW_UNARMED_LEAVING_SOON` (config.py:97), the flag that lets the 'Leaving Soon' label be written while Reaper is read-only, so the entire warning-before-deletion story is undocumented at the config layer.
Failure scenario: An operator wanting users warned before deletion reads .env.example end to end, finds no notification option, and concludes Reaper cannot notify — or must read config.py's Python source to discover REAPER_DISCORD_WEBHOOK and guess its format. Meanwhile the UI shows 'Discord notified' as a possible outcome, making the missing config doubly confusing.
Recommended fix: Add a commented "Notifications" block to .env.example after the RECOVERY entry documenting `REAPER_DISCORD_WEBHOOK` (note the entire URL is a secret; absent = notifications off) and `REAPER_ALLOW_UNARMED_LEAVING_SOON` (reversible label write while read-only; can never permit a file delete). Mirror the seed-integration block's wording. Strictly a documentation omission.

## 9. Improvements

**1. Inconsistent native_enum usage gives some enum columns DB-level CHECK validation and others none**
`src/reaper/db/models.py:56, 105, 485, 545`
Severity: low
Description: `Instance.kind` (line 56) and `AppUser.provider` (line 105) use `Enum(..., native_enum=False)` — plain VARCHAR with no CHECK — while `ReapRun.state` (line 485) and `ActionStep.state` (line 545) use bare `Enum(...)`, which on SQLite emits a named CHECK constraint. Equivalent columns thus get unequal integrity guarantees: an out-of-range state is rejected by the DB, an out-of-range kind/provider is not.
Failure scenario: A future code path, manual SQL fix, or batch migration writing an invalid `instance.kind` or `app_user.provider` is silently accepted, whereas the identical class of error on `reap_run.state` is rejected — an inconsistent, surprising integrity contract for columns of the same nature.
Recommended fix: Pick one convention across all enum columns and regenerate the baseline. Either add `native_enum=False` to `ReapRun.state` and `ActionStep.state` for uniform plain-VARCHAR behavior, or drop `native_enum=False` from `Instance.kind` and `AppUser.provider` so all enum columns carry a named CHECK constraint. Consistency, not correctness, is the goal.

## Agent Rules

1. **Always distinguish an omitted field from an explicit empty collection** on any destructive or filtering path — treat `None` and `[]` differently, and make an empty selection fail closed (never expand to "everything").
2. **Never fail open in the safety/deletion path.** When a whitelist/keep-list sync, a protection source, or an optional dependency (Plex) fails, degrade the snapshot to un-executable rather than proceeding with empty/stale protection data.
3. **Always disambiguate cross-system joins by a stable identifier (year + title, not title alone)** and refuse to bind on ambiguity (return Unknown/ABSTAIN); never silently last-write-wins into a `dict[title, row]` map.
4. **Always reuse the single production verdict/decision function** across engine, backtest, planner, and snapshot paths; never reimplement condemn/score/coverage logic (including rounding and floors) in a second place where it can drift.
5. **Never let a code comment or docstring claim a safeguard that is not implemented** (rate limiting, crash-recovery de-dup, drift detection, 0600-from-creation). Either implement it or correct the comment in the same change.
6. **Always route external HTTP through the shared client's error-mapping and retry layer** so transport/JSON errors become the domain error type; never call `self._client.request` directly, and ensure `@retry` predicates match the exceptions actually thrown (don't convert-then-fail-to-retry).
7. **Always add per-IP and per-account throttling with backoff/lockout to authentication and recovery endpoints,** and cap concurrent expensive (Argon2) verifications; never rely on a fixed CSRF header or password length as the only brute-force/DoS defense.
8. **Always invalidate existing sessions on a credential change** (call the sign-out-everywhere primitive on password reset and deactivation); never leave issued cookies valid solely on token_hash + expiry after the password changes.
9. **Never place secrets (tokens, keys, API keys) in URL query strings or path components that get logged;** keep them in request bodies/headers, default `verify=True` for TLS, and derive at-rest keys with a salted KDF plus an entropy floor on operator-supplied keys.
10. **Always create secret files atomically with owner-only mode** (`os.open(..., O_EXCL, 0o600)`), never write-then-chmod.
11. **Always make notifications and side-effecting writes idempotent across repeated calls,** keying on durably-persisted state (an announced-set) rather than a diff that is never persisted; gate announcements so preview/read-only mode cannot re-spam.
12. **Always reset time-window clocks (grace) on re-entry into the tracked state,** and remove or consult per-item tracking rows when an item leaves the set; never let stale first-flagged timestamps skip a safety window.
13. **Never expand caps/counts over items that will later be filtered out;** compute enforcement counts against the exact set that will be acted on, matching the count shown in the user's confirmation.
14. **Always keep the shipped artifact building in CI** (run `docker build`) and install from the committed lockfile with digest-pinned base images; never let unpinned `>=` floors resolve fresh at build time.
15. **Always handle React Query loading AND error states in gating/always-on UI** — render an explicit unknown/error fallback for safety indicators and setup gates; never `return null` on missing data for a component whose contract is "always visible."
16. **Always reuse the existing shared component/token/pattern** for tabs, segmented controls, notices, loading affordances, form-field labels, confirmation dialogs, CSS success/accent colors, and modal sizing (`dvh` on mobile); never introduce a parallel one-off implementation, an undefined CSS variable, a native `confirm()`, or white-on-`--accent` text that fails WCAG AA.
17. **Always give React components stable keys and stable effect dependencies** (unique-among-siblings list keys, memoized arrays, `useRef` for cross-render mutable flags, and `useEffect` resets on identity-changing props); never key on a value shared by sibling rows or depend an effect on a freshly-allocated array.
18. **Always use `Promise.allSettled` (not `Promise.all`) for independent bulk operations,** then reconcile UI state (invalidate queries, clear/retain selection) regardless of partial failure.
19. **Always keep every operator-configurable credential in the DB-backed, encrypted, UI-editable surface and documented in `.env.example`;** never strand a configuration option (e.g. the Discord webhook) as an env-only, undocumented Setting while the UI advertises its outcome.
20. **Always report the accurate error/status:** map name-clash to 409 (not 404), report the actual timeout kind (not a hardcoded budget), and honor upstream retry signals (Discord Retry-After) instead of dropping them.
