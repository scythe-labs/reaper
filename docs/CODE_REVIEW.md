# Diff review — `dev`, changes since `4478aa7`, 2026-07-21

> **Resolution (branch `worktree-unraid-auto-perms`).** All 43 findings below are addressed:
> the three highs (B-1, B-2, B-3), the thirteen mediums, and the twenty-seven lows, each with
> a regression test where one was missing. The safety-path fixes carry the most: the Scales
> tmdb namespace (B-1), the season-sweep complete-or-raise pagination (B-4), the
> collection-detach spelling/section keying (B-5/B-13), the `ensure_schema` TOCTOU (B-6), and
> the profile-fallback degradation (PR-1). CI gates are green on this branch — `ruff`,
> `ruff format`, `mypy`, pytest, `eslint`, vitest, `tsc` + `vite build`, and `alembic upgrade
> head` + `alembic check`. **`docker build` was again not run** (no Docker daemon on this
> machine); run it before release. The findings text is preserved below as the record.
>
> **Scope.** The 16 commits and 84 files changed since `4478aa7` (the Jobs rebuild with
> per-job schedules, the switchable per-run caps and the caps matrix, the Reap-page
> breakdown, the Scales rebuild, the review-queue action-grammar pass across three
> commits, the Deep brand mark, and the four scan-perf commits: instrumentation, the
> paged season sweep, concurrent Leaving Soon reconciles, and batched shelf writes).
> This is a *diff* review, not a whole-codebase pass; the previous diff review (dev @
> `f750744`, 2026-07-19) is preserved in this file's git history.
>
> **Method.** Seven scoped reviewer passes (one per file group: Plex client + Leaving
> Soon, scan pipeline, jobs/scheduler/settings, caps/policy/executor, breakdown +
> Scales, review queue + overrides, brand/shell/CSS), each reviewing its group across
> all eight categories below and required to verify every candidate against the working
> tree before reporting — one reviewer confirmed its finding with a live vitest probe.
> The three high findings and four of the mediums were then re-verified independently
> against the tree; one high was re-scoped after the re-check refuted its broadest
> claim (the startup ratings catch-up *does* have a freshness guard; the finding
> survives in narrowed form as B-3). One bug (B-4) was found independently by two
> reviewers working from different directions, which is as confirmed as it gets here.
> Duplicates merged: **43 findings: 0 critical, 3 high, 13 medium, 27 low.**
>
> **CI gates on this tree:** `ruff`, `ruff format`, `mypy` (91 files), pytest (1736),
> `eslint`, vitest (145), `tsc` + `vite build`, and `alembic upgrade head` + `alembic
> check` are all green. `docker build` was **not** run (no Docker daemon on the review
> machine) — run it before release. Nothing below is caught by the gates.
>
> Reviewer verification notes worth keeping (things checked and found *correct*): the
> caps-off switch still enforces the unknown-size allowance; the confirmation phrase
> still derives from the exact effective set; the three override views
> (`override`/`override_own`/`show_override`) are built once server-side and every
> surface lights controls from own decisions and colors from effective ones;
> `showReapIsNoop`/`groupReapEffective` take whole-show season sets on every lane;
> `_sync_grace_clocks` runs on all four mutation routes with the rule-4 semantics;
> `handFate` is the single fate router and the dashed-red classes win by declaration
> order; the `_watch_stats` single-query rewrite is semantically identical to the three
> queries it replaced; the phase-band change cannot strand a landed snapshot; GracePanel
> left no dangling references; the new breakdown/fairness routers sit behind the `/api`
> auth middleware; `api.ts` types match the changed backend schemas exactly; and the
> pre-paint favicon script validates stored data before applying it.

---

## 1. Bugs

### B-1 · Scales binds TMDB ids without a movie/tv namespace — **high**

`src/reaper/services/fairness.py:128` (`_content_key`), `:152-156` (`by_tmdb`), `:185`

TMDB movie ids and TMDB TV ids are separate, numerically overlapping id spaces. Movie
candidates store Radarr's movie-namespace `tmdbId`; season candidates store Sonarr's
TV-namespace series `tmdbId`. The rewritten roll-up keys a request as bare
`("tmdb", id)` ignoring `MediaRequest.media_type`, and indexes candidates by the raw
integer ignoring `CandidateInfo.media_type`. A TV request whose show has TMDB TV id N
binds to any movie candidate with TMDB movie id N: the requester is charged that
movie's size as "granted", and if it is condemned their card shows a reclaimable chip
naming the wrong title and opening the wrong review card. The rest of the codebase
disambiguates exactly this (`membership_index` lookups pass `media_type`); the roll-up
does not (rules 6/29). The IMDb fallback is safe (globally unique); only the tmdb
branch collides.

**Fix.** Namespace the key by media kind on both sides — `("tmdb-movie", id)` for a
movie request/candidate, `("tmdb-tv", id)` for a TV request / season candidate — in
both `_content_key` and the `by_tmdb` index. Add a collision test with a movie and a
season sharing one numeric id.

### B-2 · Intent band and simulator still claim per-run caps while caps are off — **high**

`frontend/src/components/PolicyEditor.tsx:1658` (`paceClause`),
`frontend/src/components/PolicySimulator.tsx:200`

`paceClause` renders "removes at most N titles or X per run" with no check of
`pace.caps_enabled`, and the simulator's Outcome pace note does the same. When the
operator uses the flow this range explicitly advertises ("Turn off for a big first
cleanup"), the executor skips `_check_caps` and `_check_rolling_caps` entirely, yet the
page's one-sentence policy summary still asserts a hard per-run bound — directly
contradicting the caps-off warning rendered further down the same page. The comment
above `paceClause` ("so it can never disagree with the controls below it") is now
false; the copy claims a safeguard that is not in force (rules 7/24/25).

**Fix.** Branch both strings on `pace.caps_enabled`: when off, say the run has no size
limit until limits are turned back on (the grace clause still binds and stays). Pass
`caps_enabled` through wherever the figures render.

### B-3 · The ratings job's off switch does not govern the startup catch-up, and its warning misses the real consequence — **high**

`frontend/src/components/Settings.tsx:698` (`offWarning`),
`src/reaper/services/scheduler.py:297-307` (`catch_up_on_startup`),
`src/reaper/main.py` (spawned unconditionally), `src/reaper/services/imdb_dataset.py:59`

The per-job off switch removes the cron job only. `catch_up_on_startup` never consults
`get_maintenance_schedules`: it refreshes whenever the dataset is degraded (missing or
past the 14-day `DEFAULT_MAX_AGE`). With the job off, the dataset inevitably passes 14
days, after which (a) every snapshot degrades via `DatasetDegradedError`
(`snapshot.py:615` — fail closed, so no run can start), and (b) every subsequent app
restart downloads the full ~280 MB dataset anyway, despite "off". The modal's warning
("With this off, scores keep using the ratings Reaper already has, and they slowly go
out of date") describes neither outcome: scores do *not* keep using stale ratings past
14 days — scans come back degraded and block runs — and downloads do not actually stop
across restarts. Rules 7/24/25. (An earlier draft of this finding claimed the download
runs on *every* restart; re-verification refuted that — the freshness guard is real,
the off-switch bypass and the wrong warning are what remain.)

**Fix.** Decide the contract and make code and copy match. Recommended: keep the
startup catch-up (it prevents the degraded-forever state) and rewrite the warning to
state the real consequences: after two weeks without a refresh, scans come back
incomplete and no run can start, and Reaper will still refresh once at startup when the
data is that old. Alternatively make `catch_up_on_startup` honor the stored off value
and have the warning lead with the degradation.

### B-4 · `library_season_index` can silently return a partial map, violating its complete-or-raise contract — **medium** (found independently twice)

`src/reaper/clients/plex.py:820, 844-852`; contract at `plex.py:798-800` and
`src/reaper/services/season_scan.py:983-985`

The docstring ("Raises `PlexError` on any failure rather than returning a partial
map") and the caller's load-bearing comment are what let the season scan skip
degradation on sweep results. But the pagination advances and terminates on the
*filtered* count: `elements` drops any child without a `ratingKey` (line 820), then
`start += len(elements)` and `len(elements) < SWEEP_PAGE_SIZE` ends the section — so a
full page containing even one dropped child silently truncates the rest of the
section. Separately, `int(container.get("totalSize") or container.get("size") or 0)`
falls back to the *page* size, so a server that omits `totalSize` (or clamps the
container below the requested page size) truncates after page one. Both return a
normal map, no raise. Shows entirely beyond the cut fall back safely to the per-show
path, but a show *straddling* the cut is present in the sweep result and gets no
fallback: its unswept seasons abstain (kept — fine), and a viewer whose newest watched
season is among the unswept ones vanishes from `_progress_by_user`, so
`sequential_protections` anchors them one season early and the season they are about
to start loses its mid-binge protection — a narrow fail-open. Reachability requires an
anomalous server response, but the code's own defensive filter treats that input as
possible while the termination logic treats it as end-of-section; the defense and the
paging math cannot both be right. (The same pattern pre-exists in
`library_guid_index`, `labeled_in_section`, and `section_rating_keys`; this range
added a fourth copy in the one function whose docstring promises completeness.)

**Fix.** Advance `start` and test the short-page condition on the raw container child
count, and treat a filtered-vs-raw mismatch (or an absent `totalSize` alongside a full
page) as `PlexError`, so anomalies raise and every show falls back per show as the
contract promises. Apply the same hardening to the three pre-existing twins when next
touched.

### B-5 · Collection detach removes by the constant name, not the stored spelling, so a case-variant shelf never shrinks — **medium**

`src/reaper/clients/plex.py:1065-1099` (`remove_collection_members`),
`src/reaper/services/leaving_soon.py:230-235`

`find_collection` deliberately adopts a shelf collection whose title matches
casefolded, so a collection stored as a case variant of the shelf name is treated as
*the* shelf and its members are read. But the new partial-removal path issues
`removeCollection(name)` — the tag-minus form — with the constant shelf name. The
codebase's own live-verified doctrine for the identical mechanism says tag removal is
case-sensitive: `remove_label` groups by "the exact spelling Plex stored" precisely
because "a case-sensitive removal silently removes nothing", and the new docstring
itself says "a collection is a tag". On a case-variant collection the detach returns
200 and removes nothing: an item that left grace stays on the shelf indefinitely (the
shelf over-claims — the exact dishonesty the "removals first" ordering exists to
prevent), while `ShelfOutcome` reports `removed=N, applied=True`. Every pass recomputes
the same removals and silently no-ops again; nothing converges. The full-clear path is
immune (deletes by key). The test fake asserts the constant name, cementing the bug.

**Fix.** Mirror `remove_label`: the chunk's items come back from the multi-id metadata
read already carrying their collection tags — group them by the exact stored spelling
that casefold-matches the shelf name and issue `removeCollection` per spelling. Or have
`find_collection` return the stored title and thread it through `sync_section`.

### B-6 · `ensure_schema`'s check and DDL now run in separate transactions — a TOCTOU the old code did not have — **medium**

`src/reaper/services/history_sync.py:211-226`

The perf change split `ensure_schema` into a read-connection shape check and a later
write transaction that acts on the *stale* `cols`/`live` captured before the write
lock was taken. The old code did check + DDL inside one `engine.begin()`, which SQLite
serialized. Now two concurrent callers can both read "shape stale" before either
commits, and the second executes `DROP TABLE watch_event` on the table the first just
rebuilt. Concurrent callers are real: the scan pipeline, `GET /api/fairness`, and the
scheduler's nightly sync all call it in the same process. The common interleaving is a
benign double rebuild; in the worst one the second DROP lands after an in-flight sync
has begun inserting pages, and since sync pages newest-first, the rows lost are the
*newest* plays — evidence whose absence adds deletion pressure (fail-open) for one
scan until the next sync's overlap re-fetches them. Only reachable on the one-time
stale-shape rebuild path, hence medium.

**Fix.** Keep the read-only fast path, but re-run `PRAGMA table_info(watch_event)`
inside the `engine.begin()` block and decide DROP/CREATE from that fresh read,
restoring the old atomicity.

### B-7 · Enter/Space on a revealed Spare/Reap button never saves the override — **medium**

`frontend/src/components/ReviewQueue.tsx:1002` (row handler), `:423`
(`OverrideControls`, the shared fix point)

Every season row and card carries an `onKeyDown` that does `e.preventDefault();
onOpen(...)` for Enter/Space with no target check. Keydown from the nested
`OverrideControls` buttons bubbles into it, so the preventDefault cancels the button's
native activation and the row opens the why-panel instead. Verified with a temporary
vitest probe: focus the season row's Spare button, press Enter — the override API is
called zero times and the panel opens once. The codebase already knows this hazard:
`SeasonStrip` squares stop Enter/Space propagation for exactly this reason (lines
877-881), but `OverrideControls` has no such guard. Partially pre-existing on cards,
but this range's headline change ("every season is actable in place", rule 51) extended
the broken keyboard path to every season row, and rule 46's contract explicitly
includes keyboard-focus reveal. Feedback is visible (the button stays unlit), so no
silent wrong deletion — but the advertised keyboard flow cannot spare or reap anything.

**Fix.** In `OverrideControls`, stop propagation of Enter/Space on the buttons
(mirroring the strip-square guard), or guard every row/card key handler with
`if (e.target !== e.currentTarget) return`. One change in the shared component fixes
all surfaces.

### B-8 · The Reap page's unmeasured-items line contradicts the plan, in both allowance modes — **medium**

`frontend/src/components/ReapBreakdown.tsx:157-162` (line), `:101-105` (headline),
`:142-146` (ledger)

"N titles can't be measured, so Reaper won't remove them" renders unconditionally
whenever `will_reap_unknown > 0`. With `max_unmeasured_per_run > 0` the planner admits
unmeasured items into the plan, so the Reap page tells the operator these files are
safe while the plan built directly below will delete them. The queue already solved
this with `useHoldsBackUnmeasured()`, which this component does not consult.
Conversely, under the default allowance of 0 the headline and "Will be reaped" row
count `will_reap` *including* the unmeasured items the plan holds back, so the ledger
total disagrees with the run's item count and the confirmation-phrase count shown on
the same page (rule 30; `api/breakdown.py`'s docstring claims the numbers match the
exact planned set, which the unmeasured tail breaks — rule 24).

**Fix.** Read `useHoldsBackUnmeasured()`: when true, keep the "won't remove them" line
and subtract `will_reap_unknown` from the headline/ledger count (or annotate the
total); when false, reword to say they are only removed within the unknown-size
allowance. Test the allowance-on wording.

### B-9 · All requesters without a Plex id merge into one row under the first person's name — **medium**

`src/reaper/services/fairness.py:171-181, 218-224`

`rows` is keyed on `req.requester.plex_id`, which is `None` for Seerr local users not
linked to Plex. Every such user collapses into a single row keyed `None`, named after
whichever request came first; worse, the per-title `seen: set[int | None]` dedupe
treats two distinct unlinked users as one person, so the second user's request of a
shared title is dropped from their own tally entirely. Requests, granted disk, and
reclaimable titles are misattributed to another person's name on a leaderboard the
operator reads before a run (rule 6). A stable per-user id exists (`seerr_user_id`).
The keying survived from the old code, but `roll_up` was fully rewritten in this range
and re-chose it, still untested for the `None` case.

**Fix.** Key `rows` and the per-title `seen` set on `seerr_user_id` (always present),
keeping `plex_id` only for the watch join. Add a test with two unlinked requesters.

### B-10 · Presets stage cap values but not `caps_enabled`, so "Cautious" can leave an uncapped profile — **medium**

`frontend/src/components/PolicyEditor.tsx:69` (`PresetCaps`)

`PresetCaps` picks only the five numeric keys and `applyPreset` merges only those. If
the operator previously turned caps off and saved, clicking Cautious — whose help
promises "removes less per run" — stages the values but leaves `caps_enabled: false`,
and the save persists an unbounded profile. The validator accepts it (the invariant
check is skipped while caps are off), and the intent band lies about the result (B-2
compounds this). Every preset's pace promise implies enforcement.

**Fix.** Add `caps_enabled: true` to `PresetCaps` and to each entry in `PRESETS`.

### B-11 · The static PNG favicon is declared after the SVG link, so the accent-following favicon can lose the tie-break — **medium**

`frontend/index.html:8-9`

The accent feature rewrites only the `#favicon` SVG link (the pre-paint script and
`applyFavicon` in `accent.ts`). But line 9 declares a 32×32 PNG icon *after* the SVG
link, and that PNG is baked at the default sky accent and never updated. Per the HTML
spec, when multiple icons are equally appropriate the user agent uses the last one
declared, and an exact-size PNG is a strong match (the SVG carries no `sizes`).
Browsers resolving that way show the stale sky icon forever, silently defeating the
feature the operator just configured. The standard pattern is raster fallback first,
SVG last.

**Fix.** Swap the two lines so the SVG is declared last, and verify in a real browser
session that changing the accent swaps the tab icon; optionally have `applyFavicon`
also retarget or remove the PNG link once a data-URI icon is applied.

### B-12 · `set_maintenance_schedule` is a read-modify-write of the whole overrides dict — **low**

`src/reaper/services/app_settings.py:297-302`

It reads the full `maintenance_schedules` JSON dict, mutates one key, and writes the
whole dict back. Two overlapping saves for different jobs (two browser tabs, or a save
racing startup) can both read the same base dict and last-write-wins the other's
override away; the scheduler then disagrees with the stored state until restart, when
the lost override silently reverts to its default.

**Fix.** Scope the storage per job (one key per job id), or perform the merge in a
single guarded UPDATE.

### B-13 · Collection member removal regressed from key-addressed to title-addressed, breaking duplicate-titled libraries — **low** (fails closed)

`src/reaper/clients/plex.py:1087-1092`

The removed `remove_from_collection` addressed the collection by rating key, immune to
section-title ambiguity. The replacement resolves the section via
`server.library.section(section_title)`; plexapi documents that duplicate titles
return the *last* match. With two libraries sharing a title (a common 4K/HD
arrangement), the first twin's detach raises `BadRequest` at plexapi's item
validation, so every partial detach in that library fails with a recorded
`ShelfOutcome.error`, every pass, where the old key-addressed path worked. Fail-closed
but persistent per-library breakage, and a rule 6 step backward on a path that
previously had the stable identifier. (`add_label`/`remove_label` share the title
addressing pre-range; only the collection path regressed here.)

**Fix.** Pass `section_key` into `remove_collection_members` (`sync_section` has it)
and resolve via `sectionByID`; consider the same for the label writers while there.

### B-14 · One condemned candidate can count twice in `total_reclaimable_items` — **low**

`src/reaper/services/fairness.py:196-199, 241-242`

Requests carrying a tmdb id group under the tmdb key; requests for the same content
carrying only an imdb id group under the imdb key. Both groups can bind the same
candidates, and each adds its own content key to `reclaimable_content`, so the items
total counts the title twice while the bytes total (deduped by candidate id) counts it
once — the summary strip can disagree with itself.

**Fix.** Dedupe the items total by candidate id, the way the bytes total already does.

---

## 2. Hacks and workarounds

### H-1 · `--btns` track widths hardcoded in TSX must silently stay in sync with three CSS values — **low**

`frontend/src/components/ReviewQueue.tsx:987`; `frontend/src/index.css:4059, 4153, 4420`

`SeasonList` sets `--btns` to `"11.8rem"` (two 5.75rem buttons plus a 0.3rem gap) or
`"5.75rem"`. Those numbers derive from `.ov-btn { min-width: 5.75rem }`, the
`.override-controls` gap, and the CSS fallback `var(--btns, 11.8rem)` — three
declarations that nothing flags as coupled. A future button-width tweak drifts or
clips the fixed columns rule 51 exists to keep straight.

**Fix.** Define one `--ov-btn-w` custom property and derive all four sites from it
(`min-width: var(--ov-btn-w)`; `--btns: calc(2 * var(--ov-btn-w) + 0.3rem)`), or at
minimum cross-comment both files.

---

## 3. Refactor opportunities

### R-1 · Dead `["grace"]` query invalidation with a comment naming a removed surface — **low** (found independently twice)

`frontend/src/components/PolicyEditor.tsx:1416`

`savePace.onSuccess` still invalidates `["grace"]` under the comment "Reap's read-only
grace countdown shows grace_days; keep it in step." This range replaced GracePanel
with ReapBreakdown (query key `["reap-breakdown"]`) and removed `api.grace` entirely;
no query registers under `["grace"]`, so the invalidation is a no-op and the comment
names a mechanism that no longer exists (rule 24). Meanwhile a saved grace change does
*not* invalidate `["reap-breakdown"]`, the surface that replaced it.

**Fix.** Delete the invalidation and comment; if the breakdown should refresh on a
grace change, invalidate `["reap-breakdown"]` instead.

### R-2 · `/api/grace` route and its schemas are consumed by nothing — **low**

`src/reaper/api/grace.py:33`; `src/reaper/api/schemas.py:685-700`

`api.ts` dropped `api.grace` and the GracePanel UI is gone, but the backend keeps the
route plus `GraceReportOut`/`GraceItemOut`. Nothing in the product calls it. Related:
the `App.tsx:331` comment still says "The grace countdown and Scales list titles…" —
the grace countdown surface no longer exists. Rule 38's spirit: dead safety-adjacent
surface area is deleted, not stockpiled.

**Fix.** Remove the route and both schemas (or fold what the breakdown still needs
into `breakdown.py`), and reword the App.tsx comment to name only Scales.

### R-3 · `searchFor` prop and its run-once effect are dead after App.tsx dropped the queue-search jump — **low**

`frontend/src/components/ReviewQueue.tsx:1315-1363`

This range removed `goToQueueSearch`/`queueSearch` from App.tsx (Scales now jumps by
id via `onOpenItem`/`onOpenGroup`) and stopped passing `searchFor`. ReviewQueue still
declares the prop, documents it, and carries the `handledSearch` ref and effect that no
caller can trigger — dead plumbing on an already-large prop surface that invites a
half-rewire later.

**Fix.** Delete the prop, its doc comment, and the effect.

### R-4 · `.fair-card` re-implements `.card`'s hover/selected/focus grammar declaration-for-declaration — **low**

`frontend/src/index.css:2982-3018` (duplicating `:3536-3573`)

The new Scales requester card copies the review-queue card's entire interaction
grammar — transition triple, hover accent border and wash, focus ring, inset open bar,
additive open+hover deepen — under new class names; the block's own comment admits it
copies "the review queue's card language." This is the drift surface rule 18 exists
for: a future card-hover tweak (rule 47 already forced one) now has to land twice.

**Fix.** Extract a shared base (grouped selectors or one common class both surfaces
apply) so the treatment is declared once.

### R-5 · The frontend hardcodes the upkeep job list, making the `jobMeta` fallback unreachable — **low**

`frontend/src/components/Settings.tsx:676, 1109-1112`

`MAINTENANCE_IDS` duplicates the backend's job-id list and `JobsPanel` maps only those
ids, dropping any job the server returns that is not in the hardcoded list. The
`jobMeta` fallback written "so the lookup is total" can never fire because the filter
runs first. A fourth upkeep job added server-side would silently not appear on the
Jobs page.

**Fix.** Render from `schedule.data.jobs` (filtering out the scan job), preserving the
server's order, with the `jobMeta` fallback covering ids without copy.

### R-6 · New season-row `hideReap` inlines `verdict === "condemn"` instead of the `isCondemned` helper — **low**

`frontend/src/components/ReviewQueue.tsx:983, 1034`

Rule 48 says never reimplement the already-condemned no-op test inline; `isCondemned`
(line 608) is the one expression. The new per-row control passes
`hideReap={season.verdict === "condemn"}` and computes `anyReapable` with the raw
comparison. The semantics are correct (own verdict, per rule 51), but it is a fresh
inline copy of the test the helper exists to centralize.

**Fix.** Use `isCondemned` in both places.

---

## 4. Performance

### PF-1 · Hourly presets are offered for the ratings job, which does a full ~280 MB download per run — **medium**

`frontend/src/components/Settings.tsx:730-738`;
`src/reaper/services/imdb_dataset.py:308-311`

`maintenancePresets` is one shared list for all three upkeep jobs. For the ratings
job, each run is a full download plus a multi-minute parse/load with no freshness
check and no conditional GET — and the scheduler module's own docstring says the
dataset publishes once a day and "there is no value in hammering it." An operator
innocently picking "Every hour" gets roughly 24 full downloads a day for zero data
benefit.

**Fix.** Per-job preset lists (ratings: Off / Every day only), or add a freshness
short-circuit to `refresh_ratings` (skip when the last sync is within ~20 hours) so
aggressive schedules are harmless. The short-circuit also softens B-3.

---

## 5. Production readiness

### PR-1 · An unrepairable profile blob silently resets to defaults, which can loosen grace and caps versus what the operator saved — **medium**

`src/reaper/services/profiles.py:84`

`active_profile_settings` now falls back: full parse, salvage-known-keys, then bare
`ProfileSettings()` with only a warning log. The final step fires when a stored value
no longer validates (exactly how the current `grace_days` floor came about). Defaults
are cautious in absolute terms, but relative to the operator's saved values they can
loosen the deletion path: a grace of 30 becomes 14 (items become deletable 16 days
earlier than promised to users), a run cap of 5 becomes 10. The directly analogous
`active_policy` recovery sets `fell_back`/`repaired`, degrades the scan, and shows a
loud editor notice; this degradation is invisible — the settings page shows defaults
as if chosen.

**Fix.** Mirror the policy pattern: return a flag when the blob was unreadable,
surface it on the profile GET so Pace shows a recovery notice, and have the scan treat
it as a degradation the way a repaired policy is.

### PR-2 · Held (refused) hand reaps are invisible in the reap ledger — **medium**

`src/reaper/services/breakdown.py:149-153`;
`frontend/src/components/ReapBreakdown.tsx:108-114, 132-137`

`hand_reaped` counts only reaps `decide_verdict` honors. A hand reap the engine
refuses (blocked evidence, structural gate) appears nowhere in the breakdown: an
operator who marked five items sees "+ 3 marked to reap by hand" with no line
explaining the other two. In the degenerate case (nothing condemned, all reaps held)
the page says the last scan condemned nothing while Review shows the operator's asks
dashed-red as held. Rule 23 requires every consumer of override states to enumerate
them; this new consumer silently drops one. The counts beside the destructive action
stay correct, so no wrong deletion — the ledger just under-reports the operator's own
decisions without saying so.

**Fix.** Also count reap decisions where `reap_is_effective` is false (the keys are
already in `decisions`), return it as `hand_reaped_held`, and render one line ("N of
your reap marks are held back for safety, see Review") when nonzero.

### PR-3 · The batch collection detach silently locks the collection field on every detached item — **low**

`src/reaper/clients/plex.py:1092`

plexapi's `removeCollection(name)` defaults `locked=True` and always emits the lock on
the edit. Every item taken off the shelf by the new tag-edit path gets its collection
field locked — a persistent metadata change the old per-child DELETE never made, which
alters how Plex agents and third-party collection tools treat those items afterward.
The docstring claims the write "removes ONLY the named collection", true for
membership but silent about the lock (rules 7/24). There is no leave-lock-alone option
in this API shape (`locked=False` would actively *clear* operator-set locks), so the
trade-off has to be chosen deliberately.

**Fix.** Choose the lock behavior explicitly, document the side effect in the
docstring, and note it as a known delta from the per-child DELETE path.

### PR-4 · The concurrent shelf reconcile shares one requests session across four threads, against the client's own documented premise — **low**

`src/reaper/services/leaving_soon.py:61-66, 285-316`; premise at
`src/reaper/clients/plex.py:399-404`

`sync_shelves` now runs up to four `sync_section` tasks, each pushing reads and writes
through `asyncio.to_thread` on the single shared `GuardedSession`, without taking
`_sweep_lock` — whose rationale states "requests does not promise a Session is safe to
share across threads, so the sweeps take this lock and run one at a time." The fan-out
breaks the stated invariant without updating it (rule 24). No concrete corruption was
constructed (urllib3's pool checkout is atomic; per-library section objects keep
plexapi edit state disjoint), so this is documented-invariant debt rather than a
demonstrated race — but the shelf path now writes, not just reads.

**Fix.** Either update the `_sweep_lock` and `SHELF_CONCURRENCY` comments to state why
unlocked cross-thread session sharing is acceptable here, or serialize the shelf
writes consistently with the sweeps.

### PR-5 · The schema fast path attests only `watch_event`'s columns, so future SCHEMA additions will never reach existing caches — **low**

`src/reaper/services/history_sync.py:207-215`

The old code re-ran the full idempotent SCHEMA (both tables, three indexes) on every
call; the new fast path returns as soon as `watch_event`'s columns match. That is safe
today only by accident of history — the companion table and all indexes date to the
initial commit — and the invariant is undocumented. The next person who adds an index
or companion table to SCHEMA without touching `_WATCH_EVENT_COLUMNS` ships DDL that
existing caches silently never execute.

**Fix.** Add a comment on `_WATCH_EVENT_COLUMNS` stating that any SCHEMA addition must
change the column tuple (forcing the rebuild path), or cheaply extend the fast-path
check to confirm the companion table exists.

### PR-6 · No committed way to regenerate the five PNG icon assets the comments say are generated — **low**

`frontend/src/brand/deepIcon.ts:5-9`; `frontend/src/brand/deepIcon.test.ts:3-5`

`deepIcon.ts` states the committed `favicon.svg` and the apple-touch/manifest PNGs are
generated from `deepIconSvg`, and the test instructs regenerating them on change — but
the repo contains no rasterization script, and the drift test guards only the SVG. A
future brand tweak regenerates the SVG to satisfy the test while the five PNGs
silently keep the old drawing, with no documented command to fix them (rules 7/24: a
process that exists only as prose).

**Fix.** Commit a small node script that writes `favicon.svg` and rasterizes the PNGs
from `deepIconSvg`, reference it from the comment, and have the test assert against it
(or at least assert the PNGs exist with the right dimensions).

---

## 6. Security

No findings. Checked across the range: the new breakdown and fairness routers sit
under `/api` behind the auth middleware; the pre-paint favicon script validates the
stored value's `data:image/svg+xml` prefix before applying it, so localStorage cannot
inject an arbitrary URL; no secrets appear in URLs or logs; all HTTP stays inside
`clients/` (the Discord webhook remains the one sanctioned exception); the scheduler
gained no path to a mutating call, and the transport guard still gates the new batched
shelf writes (journalled intent, armed host).

---

## 7. UI/UX consistency

### U-1 · `KeptByShowNote` states "will be removed" for a reap the engine refuses — **medium**

`frontend/src/components/ReviewQueue.tsx:486`; consumed at `:1037` and
`WhyPanel.tsx:860`

The component receives only `own` and `showOverride`, never `override_effective`. Two
branches assert removal as fact ("The whole show is set to reap, so this season will
be removed"; "You reaped this season, so it will be removed"). Reachable today: whole
show set to reap while one season is being streamed — that season's inherited reap is
refused, its chip reads "Reap requested · kept for now: playing right now" (dashed
red), and the note on the same row says it will be removed. Rule 49 established that a
held reap must never be presented as a done removal, and the `_candidate_out` comment
says the UI "never promises a removal the engine will refuse" — this note promises
exactly that. The component's own docstring ("the note never contradicts the row's
chip") is currently false for held reaps.

**Fix.** Pass the row's `override_effective` into `KeptByShowNote` and add a held-reap
wording branch ("marked to reap but kept for now") matching the chip's language.

### U-2 · The curated-lists off warning is factually wrong: every scan still refreshes the lists — **medium**

`frontend/src/components/Settings.tsx:704-705`

The warning says the protection lists stop updating and a title that joins one later
won't be protected until a hand refresh. But every scan re-syncs the curated lists
regardless of this job (`scan_runner.py:598` calls `sync_protection_lists`, which
defaults `include_top_250=True`), and list membership is only ever consulted during a
scan — which just refreshed it. Turning the job off removes essentially nothing for
anyone who scans; the warning's claim of lost protection is false (rule 25).

**Fix.** Correct the warning ("Scans still refresh these lists on their own; this only
affects the daily standalone refresh between scans."), or drop the off switch for this
job.

### U-3 · The show card's override chip claims "will be kept" even when a season's own effective reap wins — **low**

`frontend/src/components/ReviewQueue.tsx:1242`

The chip renders from the show's own decision. With a whole-show spare and one season
carrying its own honored reap (the season key wins per `effective_override`), the
card-level chip reads "Spared by hand · will be kept" while a season inside will be
removed. The replaced aggregate returned null on mixed sets, so it never overclaimed
this way. Lighting the *control* from `show_override` is correct (rule 50); the
display chip need not make an unqualified whole-show claim.

**Fix.** When any season's own decision opposes the show's, soften the chip (drop the
"will be kept/removed" clause or append "except N seasons"), computed from
`showSeasons`, which the card already holds.

### U-4 · Rule 45's deferred `.warn` → `.notice-warn` merge came due this range and was not done — **low**

`frontend/src/components/ScanBar.tsx:198`; `frontend/src/components/WhyPanel.tsx:235`

Rule 45 defers the `.warn` banner merge "whenever the review UI is next touched." This
range rebuilt the review UI across three commits and rewrote ScanBar into `ScanRow` —
which converted its two error paragraphs to the shared notice classes but left the
degraded-snapshot banner on the legacy `.warn` one block below. The trigger condition
fired twice over and the debt survived.

**Fix.** Convert the degraded banner in ScanRow and WhyPanel's `.warn kept-notice` to
`.notice.notice-warn`, then retire `.warn` from `index.css` (keep `.warn.danger` only
where still consumed).

### U-5 · Schedule clock times are shown without a timezone and are actually server-container time — **low**

`frontend/src/components/Settings.tsx:721-738, 757-793`

`CronTrigger.from_crontab` evaluates in the container's local timezone (commonly UTC
in Docker) while the new UI renders prominent wall-clock copy ("Every night at 2 AM",
"Default: Every day at 3:30 AM") with no qualifier. For an operator whose container
runs UTC in a UTC-7 home, "2 AM" fires at 7 PM local, and the relative "next in 3 hr"
line will visibly contradict the stated clock time. Pre-existing for the one scan
preset; this range multiplies the clock-time surfaces.

**Fix.** Add one help line in the modal ("times are the server's clock"), or derive
the rendered description from the trigger's actual next-run instant in the browser's
timezone.

### U-6 · On a schedule-load error the scan row says "Automatic scan: checking…" forever — **low**

`frontend/src/components/Settings.tsx:795-799, 1104`

`scanScheduleText(undefined)` returns the "checking…" line and `JobsPanel` passes
`undefined` both while pending and on error, so a failed load leaves the row claiming
to check indefinitely. The page-level error notice does render (rule 17's explicit
error state exists), but the row's own line conflates loading with failure.

**Fix.** Thread the error state into `scanScheduleText` ("Couldn't check the
schedule.") or pass a tri-state instead of `ScheduledJob | undefined`.

### U-7 · Unmeasured condemned titles display as "Reclaimable · 0 B" in Scales — **low**

`src/reaper/services/fairness.py:266-270`; `frontend/src/components/Fairness.tsx:61`

`_load_candidates` coerces a missing size to 0 (the comment owns the tradeoff, so rule
7 is satisfied), but the chip then renders a literal "Reclaimable · 0 B" for a title
the arr would not size, and the person's reclaimable bytes silently under-report. The
breakdown page carries unmeasured items as an explicit count; Scales shows a false
zero instead.

**Fix.** Carry `size_bytes: int | None` through `ReclaimableTitle`/`ReclaimableTitleOut`
and render "size unknown" on the chip when null (totals keep summing measured only).

### U-8 · Requester cards are keyed by display name, which is not unique — **low**

`frontend/src/components/Fairness.tsx:264-271`

`key={row.name}`, where the name falls back through display name and username; two
people can share one, producing duplicate sibling keys and wrong reconciliation of the
cards' open/closed state (rule 19). `RequesterRowOut` currently carries no id to key
on.

**Fix.** Add the stable per-user id to `RequesterRowOut` (pairs with B-9) and key on
it.

### U-9 · "across 1 people" in the Scales summary strip — **low**

`frontend/src/components/Fairness.tsx:237`

No singular form; a one-requester install reads "Requests across 1 people", and the
component test pins the ungrammatical string (rule 21).

**Fix.** Pluralize ("1 person" / "N people") and update the test.

### U-10 · The non-clickable `.media-chip.static` still lights the accent hover — **low**

`frontend/src/index.css:3130-3135` (and the `.mc-go` rule at `:3170`)

`.media-chip:hover:not(:disabled)` outranks the global button hover, but the static
variant is a `<span>`, which never matches `:disabled` — so a chip with nothing to
open gets the accent border and wash on hover while `cursor: default` says it does
nothing, contradicting the file's own rule stated 150 lines up ("hover only promises
the accent where there is something to open"). The branch is defensive, so the
contradiction surfaces exactly when backend data drifts.

**Fix.** Scope the hover to real actions: `.media-chip:not(.static):hover:not(:disabled)`.

### U-11 · Cap abort copy was reworded only for the two per-run messages — **low**

`src/reaper/services/executor.py:541, 848, 855`

The per-run cap messages were rewritten to plain language, but the unmeasured message
still says "aborted, not trimmed: which of them gets deleted must never come down to
sort order" and both rolling-cap messages still say "The run is aborted, not
truncated" — the jargon phrasing rule 21 targets and this range deliberately removed
elsewhere (tests were even updated away from asserting the old phrase). The rolling
messages also do not mention the caps switch the per-run ones now point to, though the
same switch governs them.

**Fix.** Reword the rolling-cap and unmeasured aborts in the same voice ("It stops
rather than deleting just part. Wait for the window to pass, raise the cap, or turn
limits off in Policy, under Pace and limits.").

---

## 8. Improvements

### I-1 · No test pins that caps-off skips the rolling 30-day budget or the byte caps — **low**

`tests/test_reap_loop.py:483`

`test_caps_off_lets_a_run_over_the_cap_proceed` exercises only the per-run item cap;
nothing pins the `caps_enabled` guard in `_check_rolling_caps` (`executor.py:839`) or
the per-run byte cap under the switch. A future refactor could silently regress the
rolling half of the switch in either direction without a test failing.

**Fix.** Add one executor test with caps off and a plan exceeding the 30-day and
per-run byte caps, asserting the run completes.

### I-2 · Two new fail-closed branches in the season gather have no test — **low**

`tests/test_season_scan.py` (`TestGatherEndToEnd`)

(1) `library_season_index` *raising* `PlexError` → warning plus whole-library per-show
fallback (`season_scan.py:988-991`) — the fake's empty-dict path exercises different
code than the except. (2) `keep_in_progress=False` skipping the entire Sonarr
`episodes()` fan-out (`season_scan.py:1016`) — correct today only because
`plan_series_prune` empties `seq_protected` when the guard is off, and nothing pins
that coupling.

**Fix.** Add a gather test whose fake Plex raises from `library_season_index` and
asserts seasons still resolve via the fallback, and one asserting `episodes()` is
never called when the guard is off. (Both also become the natural home for B-4's
regression tests.)

### I-3 · Two comments made stale by this range's own changes — **low**

`src/reaper/services/snapshot.py:414-416`; `src/reaper/services/season_scan.py:22-24`

The first justifies the single `history_sync.state` read with "each re-runs the schema
check inside a write transaction" — no longer true now that the common path is a read
connection (the choice is still right; the stated reason is not). The second still
claims resolution "bounds the per-show Plex calls to the shows that actually have
something removable" — the new sweep reads every season of every show in the allowed
sections; only the fallback and the Sonarr fan-out remain bounded (rule 24
discipline).

**Fix.** Reword both to match the new behavior.

### I-4 · Spelling and grammar slips in new comments — **low**

`frontend/src/components/Settings.tsx:952` ("greys" → "grays");
`frontend/src/index.css:1054, 5576` ("centerd" → "centered", twice — introduced by the
Americanization sweep itself); `src/reaper/engine/policy.py:675` ("Keep validating
them here would reject" → "Keeping the validation here would reject").

**Fix.** The three one-word edits.

---

## Suggested fix order

1. **The three highs.** B-1 (Scales tmdb namespace), B-2 + B-10 together (caps-off
   honesty in the editor: intent band, simulator, presets), B-3 + U-2 + PF-1 together
   (the jobs page's contract with reality: ratings catch-up vs the off switch, both
   off-warnings, per-job presets or the freshness short-circuit).
2. **The deletion-adjacent mediums.** B-4 (sweep pagination, plus I-2's tests), B-5 +
   B-13 + PR-3 together (one pass over `remove_collection_members`: spelling-grouped
   removal, key-addressed section, lock decision), B-6 + PR-5 (one pass over
   `ensure_schema`), PR-1 (profile fallback surfacing), PR-2 + B-8 together (the
   breakdown's held reaps and unmeasured lines).
3. **The queue's honesty and keyboard.** B-7 (one guard in `OverrideControls`), U-1,
   U-3, R-6, H-1.
4. **Scales cleanups in one pass.** B-9 + U-8 (seerr-id keying and card keys), B-14,
   U-7, U-9.
5. **The rest of the lows** grouped by file: Settings.tsx (B-12, U-5, U-6, R-5),
   frontend shell (B-11, R-1, R-2, R-3, R-4, U-4, U-10, PR-6), copy and comments
   (U-11, I-3, I-4, PR-4's comment decision), and I-1's test.

---

## Agent rules

Direct constraints for the next coding agent, derived from what this review actually
found. They extend CLAUDE.md's rules 1–51; where one sharpens an existing rule, the
sharper obligation governs.

1. **Every tmdb id key is namespaced by media kind.** A bare integer tmdb key in any
   map, index, or lookup is a blocker; the key carries movie-vs-TV alongside the
   number, on both the write and the read side (sharpens rules 6/29 — B-1).
2. **A rendered limit checks its enable switch.** Any UI string, summary clause, or
   simulator note that states a cap, budget, or bound must branch on the setting that
   enables enforcement; rendering the stored figure while the switch is off is a
   blocker (sharpens rules 7/25 — B-2).
3. **A preset that promises enforcement stages the enabling switch.** Applying a
   preset must set every switch its help text implies, not just the values behind the
   switch (B-10).
4. **A job's off switch governs every path that runs the job.** Startup catch-ups,
   recovery paths, and other side entrances either honor the stored off value or the
   off-warning copy explicitly names the exception. Off-warning copy states the real,
   code-verified consequence of turning the job off — including degradation that
   blocks runs — never a guessed softer one (B-3, U-2).
5. **Pagination advances and terminates on the raw page count.** A defensive filter
   that can shrink a page must either raise on anomaly or be kept out of the paging
   math; and a total-size fallback must never default to the page size. A
   complete-or-raise docstring is a contract: violating input raises, it never returns
   a partial result (sharpens rule 27 — B-4).
6. **Plex tag-style removals address the stored spelling and sections by key.**
   Any label or collection removal groups items by the exact stored tag spelling
   (casefold-matched, following `remove_label`) and resolves sections via
   `sectionByID`, never by title (B-5, B-13).
7. **A check-then-write re-reads inside the write transaction.** Splitting a state
   check into a read connection is fine only if the write transaction re-reads the
   state it acts on; DDL or destructive writes driven by pre-lock reads are a blocker
   (B-6).
8. **Multi-key JSON settings are updated per key or under a guarded merge.** A
   read-modify-write of a whole settings dict across an await is a blocker (B-12).
9. **Interactive children of a keyboard-handling row stop Enter/Space propagation.**
   Any control nested inside a row or card that has its own Enter/Space handler either
   stops propagation (the `SeasonStrip` guard is the model) or the container checks
   `e.target === e.currentTarget`. Adding a control to a row without this check is a
   blocker (B-7).
10. **Prose about a removal consults `override_effective`.** Any note, chip, or
    sentence that asserts an item "will be removed" or "will be kept" must branch on
    the effective state, including held reaps and opposing season-level decisions
    (extends rule 49 from color to wording — U-1, U-3).
11. **Every number on the Reap page derives from the planner's exact set.** Headline,
    ledger, and per-line counts consult the same branches the planner does — including
    the unknown-size allowance via `useHoldsBackUnmeasured()` — and every stored
    override state (held reaps included) appears in the ledger or is explicitly
    summarized (sharpens rules 23/30 — B-8, PR-2).
12. **Rows are keyed and aggregated by a stable server id, never a display name.**
    If the schema lacks an id, add one in the same change; user-level roll-ups key on
    the always-present per-user id, not an optional linked-account id (B-9, U-8).
13. **Removing a surface removes its whole supply chain in the same change.** Route,
    schemas, client method, props, query-key invalidations, and comments naming it —
    grep for the query key and prop name before closing (R-1, R-2, R-3).
14. **Silent recovery on operator-configured safety values is forbidden.** A fallback
    that replaces saved profile/policy values must surface a flag the UI renders and
    degrade the scan, following the `ActivePolicy` pattern; a log line alone is a
    blocker (sharpens rule 2 — PR-1).
15. **Server-defined lists render from the server response.** A hardcoded frontend
    copy of a backend id list (jobs, phases, states) is a blocker when the server
    already returns the list; fallback copy handles unknown ids (R-5).
16. **Values coupled across TSX and CSS are derived from one declaration.** A width,
    gap, or count that must agree between a component and a stylesheet lives in one
    custom property both read, or both sites carry a cross-reference comment (H-1).
17. **Generated assets ship with their generator.** A comment saying an asset is
    generated must name a committed, runnable script, and a drift test covers every
    generated artifact, not just one (PR-6; extends rule 24 to assets).
18. **The icon link the app rewrites is declared last.** Static fallback icons precede
    the dynamic one in `index.html`; adding an icon link after `#favicon` is a blocker
    (B-11).
