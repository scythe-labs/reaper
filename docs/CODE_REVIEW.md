# Backend code review — `dev` @ `a7d7659`, 2026-07-24

> **Scope.** A whole-codebase review of the Python backend only (`src/reaper/**`, ~36.8k
> lines). The React SPA under `frontend/` was reviewed separately and is out of scope here.
> Seven scoped reviewer passes covered: the engine scoring/verdict core; the snapshot/scan
> pipeline; the plan/execute/safety-window deletion path; supporting services (fairness,
> history sync, lists, profiles, requested-by, scheduler, imdb dataset, …); the FastAPI
> routers; the HTTP client layer + Discord notify; and auth/crypto/config/db/infra. Each
> pass read every file in its group in full and verified each candidate against the working
> tree before reporting.
>
> **Method.** Every finding cites `file:line` on this tree and a concrete failure mode. The
> two `high` findings and the sharpest `medium`s were re-verified directly against the code
> during synthesis. Two independent reviewers reported the unthrottled Plex-login DoS from
> different directions (auth pass and API pass); it is merged below (S-1), which is as
> confirmed as it gets here.
>
> **Tally.** 53 findings — **0 critical, 2 high, 17 medium, 34 low/low-medium.** No
> fail-open hole in the *armed* deletion path itself: the manifest re-hash, the live
> confirmation-phrase recompute, the empty-selection fail-closed, the dual size interlock,
> and the atomic journal state-transitions were all checked and hold. The two highs are (B-1)
> a keep-tag that silently protects nothing when the operator's tag has any uppercase, and
> (B-2) the most dangerous Plex calls resolving a library by title instead of key. Both are
> narrow-trigger but high-blast-radius, and both resolve toward *deleting more* than the
> operator intended, so they lead the list.
>
> This is a whole-codebase pass; the prior *diff* reviews (fourth pass, dev @ `80d19f5`
> scope, and earlier) are preserved in this file's git history.

---

## 1. Bugs

### B-1 · A keep tag with any uppercase silently protects nothing, and reports success — **high**

`src/reaper/services/lists.py:234` (label side) vs `:239` (lookup side); fail-open realized in
`sync()` `:463-483`.

`ArrTagRule.fetch` builds its tag-id map with **lower-cased** keys
(`by_label.setdefault(str(row.get("label","")).lower(), tag_id)`, line 234) but looks the
operator's configured tags up **without** lower-casing: `found = by_label.get(tag)` (line
239). Sonarr and Radarr force every tag label to lowercase at the source, so `by_label` is
always lowercase; the comment at 227-229 and the `.lower()` on the label side both show the
intent was case-insensitive matching. Failure mode: operator configures keep tag
`Reaper-Keep` (natural capitalization) and tags items with it in Sonarr (stored there as
`reaper-keep`). `by_label.get("Reaper-Keep")` → `None` → the tag is treated as missing.
Under the default `match="any"` with that as the only tag, `wanted` is empty, so line 251
raises `ContainerMissingError`. In `sync()` the first-sync branch (`stored == 0`, line 481)
classifies that as "genuinely empty," writes an **empty membership with `last_error = NULL`
(a reported success)**, and every subsequent scan repeats it — so keep-tagged titles are
silently deletable, permanently, with the UI showing the keep list as healthy.

**Fix:** `found = by_label.get(tag.lower())`. Add a test with a mixed-case configured tag
and a lowercase *arr label asserting the item is protected. (If any settings path already
lowercases stored tags, this is latent rather than live — but the fetch is self-inconsistent
and must not depend on that.)

### B-2 · The most dangerous Plex calls resolve the library by title, not key — **high**

`src/reaper/clients/plex.py:694` (`item_count`), `:735` (`is_refreshing`), `:1215`
(`refresh_path`), `:1240` (`empty_trash`); contrast the correct `sectionByID` at `:961`,
`:1000`, `:1148`.

`empty_trash`, `item_count` (the input to the trash count-delta interlock), `refresh_path`,
and `is_refreshing` all call `server.library.section(section_title)`. Two Plex libraries can
legally share a title; `library.section(title)` silently returns the **first** match. The
module's own docstrings at 944/985/1121 say this is exactly why `add_label`/`remove_label`/
`remove_collection_members` were changed to `sectionByID` (rule 6/57) — but these four were
not. Failure mode: with duplicate library titles, `empty_trash("Movies")` purges the wrong
twin library's trash, and the count-delta interlock reads the wrong library's `totalSize`,
so the interlock that is supposed to catch an over-large trash purge is comparing against the
wrong baseline. Low trigger probability (duplicate titles are uncommon), whole-library blast
radius.

**Fix:** change these four signatures to take `section_key` and resolve via `sectionByID`,
as the label/collection writers already do; the executor call sites already hold section
keys. Then delete the by-title `section()` helper path so it can't be reintroduced.

### B-3 · A section-path map keyed by title drops a colliding library's locations — **medium**

`src/reaper/clients/plex.py:565`.

`section_paths` returns `{s.title: list(s.locations) for s in server.library.sections()}`.
Duplicate section titles collide in the dict (last-write-wins, rule 6), silently dropping one
library's `locations`. This table feeds the partial-refresh path mapping, so a colliding
library's post-reap refresh maps to nothing (or to the wrong paths) and "silently rescans
nothing at all," which the docstring at 560 warns against.

**Fix:** key by section id (return `{key: (title, locations)}` or a list of tuples) and have
the refresh path resolve by key.

### B-4 · `find_collection` / `collection_children` bypass the hardened paging loop — **medium**

`src/reaper/clients/plex.py:903-906`, `:925-929`.

Both call `server.query(...)` and iterate directly, bypassing the `_iter_section_pages`
complete-or-raise loop the module mandates for the four section sweeps. If Plex windows these
responses (as it does for `/all`): `find_collection` returns `None` when the wanted
collection sits past the first window → the caller creates a **duplicate** "Leaving Soon"
shelf and reconcile splits across two; `collection_children` returns a truncated member set →
stale members (`current − wanted`) are never detached and linger on the shelf, while
already-present members get redundantly re-added.

**Fix:** page both through `_iter_section_pages` (or send explicit container params and
enforce `totalSize` the same way the sweeps do), or assert the response is complete.

### B-5 · A populated list whose items all lose their ids wipes stored membership — **medium**

`src/reaper/services/lists.py:463`, `:491-495`.

`sync` distinguishes a missing container (raise, keep prior members) from a genuinely-empty
one, but not "container present, every item's ids unusable." `items = [i for i in await
provider.fetch() if i.has_any_id]` collapses to `[]` when a real, populated `PlexCollection`
returns items whose guids all fail `identity.parse_guids` (e.g. a Plex agent change). The code
then runs the atomic `DELETE ... WHERE slug` + insert-nothing, **wiping stored membership**
and un-protecting everything on the list — the silent-empty failure rule 27 exists to
prevent.

**Fix:** when a fetch returns a non-empty raw set that filters down to zero id-bearing items
while a populated membership is stored, treat it like `ContainerMissingError` (record error,
preserve prior membership, degrade), not as an empty success.

### B-6 · Season pruning never refreshes Plex, so the TV trash interlock never runs — **medium**

`src/reaper/services/executor.py:1570-1706` (`_send_season`); contrast `_send_movie`
`:1505-1509`.

`_send_season` never calls `_best_effort_refresh`; the only call site is in `_send_movie`.
After Sonarr deletes a season's episode files, Plex is never nudged to rescan, the TV section
is never added to `_affected_sections`, and the end-of-run trash purge (`_finalize_plex`,
interlock 10) therefore **never runs for TV libraries at all**. The class docstring (77-85)
says the trash interlock applies to "every deletion routed through an *arr," but it silently
applies to movies only. Fails safe (disk is reclaimed via Sonarr; nothing wrong is purged),
but TV libraries accumulate stale "unavailable" episode entries until Plex's own scheduled
scan, and the docstring overstates coverage (rule 24).

**Fix:** resolve the season's on-disk folder path and call `_best_effort_refresh(path,
plex_entries=...)` after a verified season delete, so TV sections join the count-delta trash
gate like movies. Correct the docstring if the gap is left intentionally.

### B-7 · An unrated show's rating signal is mislabeled "could not read," dragging coverage — **medium**

`src/reaper/engine/signals.py:311-323`; contrast `SEASON_RANK` `:280-288` and `evaluate_custom`
`:356-371`.

For built-in numeric signals other than `SEASON_RANK`, an `Absent` observation funnels through
`_numeric()` (which returns `None` for both `Absent` and `Unknown`) into the `raw is None`
branch → `evaluated=False, state=UNREADABLE`, i.e. "could not look." In the live TV path
`season_scan` deliberately sets `imdb_rating_tenths=Absent` for an unrated show, so `LOW_RATING`
on every unrated show is mislabeled amber/"could not read the IMDb rating" in the why-panel and
drags `coverage` to 90% instead of 100% under the default TV policy. `SEASON_RANK` and the
graded custom path already special-case `Absent → NOT_APPLICABLE / evaluated=True`; the
built-in numeric arms do not. Direction is fail-safe (lower coverage → abstain → keep), but it
is a real explainability bug and will silently over-abstain unrated shows once
`coverage_floor_bp` is raised past ~0.90.

**Fix:** before the `raw is None` branch, treat an `Absent` input as `NOT_APPLICABLE`
(`evaluated=True`, weight retained, coverage intact) for every numeric signal, exactly as
`SEASON_RANK`/`evaluate_custom` do.

### B-8 · `?limit=-1` returns every run and amplifies the per-run cost — **medium**

`src/reaper/api/runs.py:205`, `:210`.

`list_runs(limit: int = 50)` guards with `min(limit, 200)` and has no lower bound. A negative
value passes through: `min(-1, 200) == -1`, and `.limit(-1)` renders `LIMIT -1`, which SQLite
treats as **no limit**. `GET /api/runs?limit=-1` returns every run, and each run flows through
the expensive `_run_out` → `_planned_candidates` path (see P-2), so it also multiplies that
cost over the whole table.

**Fix:** `limit: int = Query(50, ge=1, le=200)` and drop the `min()`.

### B-9 · A season that deleted zero files is reported as a full-size success — **low-medium**

`src/reaper/services/executor.py:1673-1697` (with the verified-mark at `:1053-1061`).

In `_send_season`, after the unmonitor is verified, the episode file ids are re-resolved live
(`file_ids`, 1675-1679). If that second `episode_files` read returns no files (files removed
out-of-band between the size re-read at 1607-1611 and this resolve), `delete_episode_files([])`
is a documented no-op, `still_there` is empty, and the item is marked **VERIFIED** with
`deleted_items += 1` and `deleted_bytes += approved_size` — asserting a deletion that did not
happen and charging it against the rolling 30-day budget. Rare (millisecond window), but the
fail-closed reading of "no files resolved" is a SKIP, not a verified delete.

**Fix:** if `file_ids` is empty, `_mark_skipped` ("no files resolved to delete; kept") instead
of proceeding to verify.

### B-10 · A non-dict `match` in a stored explanation crashes the reap-override read instead of blocking — **low-medium**

`src/reaper/services/condemned.py:71`.

`reap_override_verdict` is documented to treat any malformed/unreadable explanation as *blocked*
(keep). It guards `exp` being a dict and the protection lists, but
`match_status = (exp.get("match") or {}).get("status")` is unguarded: a stored row with a
truthy non-dict `match` (e.g. `{"match": "x"}`) makes `("x" or {}).get(...)` raise
`AttributeError`, which escapes the `try` (that only wraps `json.loads`) instead of resolving
to blocked. Reaper always writes `match` as a dict today, so this is a contract/robustness gap,
not a live crash — but it defeats the stated "unreadable ⇒ keep" guarantee.

**Fix:** `m = exp.get("match"); match_status = m.get("status") if isinstance(m, dict) else None`.

### B-11 · A malformed/empty Tautulli `sessions` body fails open on the streaming veto — **low-medium**

`src/reaper/services/snapshot.py:477-486`.

A Tautulli `activity()` call that *succeeds* but returns a null/malformed `sessions`
(`{"sessions": null}` → `or []`) adds no rating keys and does **not** degrade, so every item
reads `is_streaming_now = Known(False)`. This is the same "success with a null body coerces to
empty, indistinguishable from real empty" case that `_keep_history_degradations`
(`scan_runner.py:429-438`) deliberately fails *closed* on; here it fails open. Bounded (scan-time
streaming only protects, and the executor re-checks streaming live at delete time), but the
scan-level veto is silently defeated.

**Fix:** distinguish a missing/malformed `sessions` key from a genuinely empty list; degrade
(or set the activity-degraded flag from H-1) when the shape is wrong, matching the keep-history
handling.

### B-12 · The recovery banner prints a non-routable bind address — **low-medium**

`src/reaper/main.py:104-105`.

The recovery banner URL is built as `f"http://{settings.host}:{settings.port}"`, and
`settings.host` defaults to `0.0.0.0`, so it prints `http://0.0.0.0:8420/recover` — a bind
address, not a routable one. `recovery.py`'s docstring claims the banner prints "a bare
`/recover` URL." An operator in a lockout must guess/substitute their real host.

**Fix:** print a relative `/recover` path or a `http://<your-reaper-host>:8420/recover`
placeholder rather than interpolating the bind address.

### B-13 · A single-use recovery token is burned on a recoverable error — **low**

`src/reaper/api/auth.py:346-359`.

In `recover`, `redeem_recovery_token` sets `used_at` and the request commits it even when
`_recovery_target` returns `None` (no admin to sign in as); the 409 path at 354-359 runs after
the token is already consumed. The operator's one valid 15-minute token is burned without
logging anyone in, forcing another `REAPER_RECOVERY=true` reboot — and "no admin exists" is
exactly when recovery is most needed.

**Fix:** mark the token used only after a target is found and a session is minted; if no target,
roll back the redemption so it can be retried after an admin is created.

### B-14 · Per-person fairness attribution double-counts across id-split groups — **low**

`src/reaper/services/fairness.py:477-482`, `:501-563`.

`roll_up` groups requests by a single `_content_key` (tmdb > tvdb > imdb) and dedups people
per-group. One title can split into two groups when some co-requests carry tmdb and others carry
only imdb. If the **same person** appears in both groups, they are counted once per group, so
`requests_made`, `gb_granted_bytes`, and `reclaimable_items` all double for them. Report-level
totals are safe (deduped by candidate-id frozenset), but the per-person row over-counts. Rare
(Seerr requests almost always carry tmdb).

**Fix:** dedup the per-person attribution across all groups of the same matched candidate set,
not per content-key group.

### B-15 · Duplicate instances can be seeded within one env batch — **low**

`src/reaper/services/seeding.py:62-83`.

For non-singleton kinds, duplicate detection is a DB query on `(kind, name)` with no in-batch
guard and no flush until line 86. Two `InstanceSeed`s with the same `(kind, name)` in one env
batch both pass the `existing is None` check (neither is flushed) and both get added, creating
duplicate instances. The singleton path has a `seeded_singletons` in-batch set for exactly this;
the general path lacks the equivalent.

**Fix:** track seeded `(kind, name)` pairs in a batch-local set and skip in-batch repeats,
mirroring `seeded_singletons`.

---

## 2. Hacks and workarounds

### H-1 · A degraded evidence source is detected by substring-matching a free-text reason — **medium**

`src/reaper/services/snapshot.py:303`, `:486`, `:541`.

Whether Tautulli activity failed is detected by `"tautulli-activity" in " ".join(context.
degraded_reasons)`. The producer (486, `f"tautulli-activity unreachable: {exc}"`) and the two
consumers (303 movie streaming veto, 541 season `activity_degraded`) are coupled *only* by that
literal. Reword the reason string and the streaming veto silently stops going `Unknown` — it
reads `Known(False)` ("nothing streaming"), quietly unprotecting files someone is watching. Line
303 also recomputes `" ".join(...)` once per movie inside the scoring loop.

**Fix:** carry an explicit `activity_degraded: bool` on `ScanContext`, set where the exception
is caught; both consumers read the flag instead of grepping the reason list. (This flag also
fixes B-11's degrade signal.)

### H-2 · An unwired second decision function produces CONDEMN outside `decide_verdict` — **low**

`src/reaper/engine/requester.py:113-264`.

`requester.evaluate` is a complete `CONDEMN`/`PROTECT`/`ABSTAIN` decision function with its own
`Verdict` type, parallel to `engine.verdict.decide_verdict`. It is consumed **only by tests**
(`test_requester_rule.py`); the sole production importer, `fairness.py`, imports only
`WatchEvidence`, never `evaluate`. Per rule 38, unwired safety-adjacent code that produces
`CONDEMN` outside the one decision function should land with its consumer (routing condemnation
through `decide_verdict`) or not exist.

**Fix:** wire it (with `CONDEMN` flowing through `decide_verdict`) or remove it; at minimum
document it as unreachable like `backtest`, so operator copy never implies it runs.

---

## 3. Refactor opportunities

### R-1 · `_OBS_FIELDS` is a hand-maintained parallel list of `Facts` observation fields — **low-medium**

`src/reaper/engine/facts_codec.py:30-48`.

`_OBS_FIELDS` duplicates the set of `Observation`-typed fields on `Facts`. A future custom-rule
field added to `Facts` (which defaults to `_UNSET` = `Absent`) but omitted here silently
round-trips as `Absent` — a fail-**open** on the keep lane (rule 35: `Absent` grants no keep
discount where the real value might have kept the file) — and because such fields carry a default
it does **not** raise at construction. The round-trip test catches it only if the `_facts`
fixture is updated in lockstep.

**Fix:** derive `_OBS_FIELDS` from `dataclasses.fields(Facts)` minus `title`/`ratings`,
eliminating the drift.

### R-2 · "Days since reference" is derived two ways across calibration and backtest — **low**

`src/reaper/engine/calibration.py:238` vs `src/reaper/engine/backtest.py:349`.

Calibration uses `(cutoff - reference).days` (integer floor) for bucketing; backtest uses
`(cutoff - reference).total_seconds() / 86_400` (float) for the scored fact. At a bucket boundary
the same item can bucket one way and score another. Impact is negligible (the prior is coarse and
floor rounds toward keeping), but it is the dual-derivation rule 3 warns against.

**Fix:** share one dormancy-days helper between the two modules.

### R-3 · The restore auth-purge list is a hardcoded literal with no drift guard — **low**

`src/reaper/services/restore.py:412-441`.

`_purge_auth_state` clears sessions on restore from a hardcoded list (`auth_session`,
`recovery_token`, `pending_plex_login`). A future auth/credential-bearing table would be silently
carried forward on restore, defeating the sign-out-everywhere guarantee this function exists to
enforce (rule 12). Nothing ties the list to the model set.

**Fix:** derive the purge list from a single source next to the models, or add a test that fails
when an auth-bearing table is added without being listed here (rule 64).

---

## 4. Performance

### P-1 · `_fold_merged_watch_stats` doesn't chunk its `IN`, overflowing SQLite's variable limit — **medium**

`src/reaper/services/snapshot.py:1785-1799`.

`_fold_merged_watch_stats` runs `... WHERE rating_key IN :keys` with an `expanding` bindparam over
`all_keys` and **does not chunk**, unlike every sibling (`season_watch_stats` batches at 500,
`record_first_flagged_bulk`/`_insert_first_flags` chunk at 500/300). A library with >999 merged
binds overflows SQLite's variable limit, raising `OperationalError` (which the scan does not
catch as `IntegrationError`), aborting the whole scan.

**Fix:** chunk `all_keys` with `itertools.batched(..., 500)` and accumulate, mirroring
`season_watch_stats`.

### P-2 · `list_runs` re-loads the condemned set and re-runs steps dozens of times per request — **medium**

`src/reaper/api/runs.py:113-124`, `:127-160`, `:216`.

`_run_out` calls `_run_steps` (129) and then `_planned_candidates`, which independently re-runs
`_run_steps` (113), `whitelist.overrides` (114), `effective_condemned` (115), and
`active_profile_settings` (119) *per run*. `effective_condemned` loads the snapshot's entire
`verdict=="condemn"` set into memory (`condemned.py:163-171`). `list_runs` does this for every run
(up to 50), so the full condemned set and the overrides/profile reads are re-fetched dozens of
times per request and `_run_steps` is queried twice per run (~250+ queries).

**Fix:** compute `overrides`, `active_profile_settings`, and `effective_condemned(snapshot_id)`
once per request (cache by `snapshot_id`) and pass them into `_run_out`; fetch steps once.

### P-3 · The `override` candidate filter materializes every key and risks the SQL variable ceiling — **medium**

`src/reaper/api/routes.py:290-307` (used by totals `:312-320` and page `:346-354`).

The `override` filter resolves **all** matching `media_key`s into Python, filters in Python via
`whitelist.effective_override`, then applies `Candidate.media_key.in_(wanted)`. For `override=none`
on a large library, `wanted` is nearly every row. This risks `SQLITE_MAX_VARIABLE_NUMBER` (32766)
→ a 500, and is O(all-rows-in-lane) on every paged request. `_group_rollups` chunks its own `IN`
at 500 for exactly this reason (`:427-438`); this path does not.

**Fix:** chunk the `IN`, or express the state as an anti-join / `NOT IN (decisions)` subquery
rather than materializing every key.

### P-4 · `watch_event` has no index on `parent_rating_key`, forcing full scans on every Scales load — **medium**

`src/reaper/services/history_sync.py:128-131` (schema), consumed at `fairness.py:843`, `:877`.

`watch_event` is indexed on `rating_key`, `grandparent_rating_key`, and `watched_at`, but **not**
`parent_rating_key`. Both `fairness._evidence_index` (the `... WHERE parent_rating_key IN :keys`
UNION arm) and `_distinct_episodes` (`WHERE user_id = :pid AND parent_rating_key IN :keys`) filter
on it. On a mature mirror (hundreds of thousands of rows), every Scales board load and person-drawer
open runs a full table scan per 500-key chunk.

**Fix:** add `CREATE INDEX IF NOT EXISTS ix_watch_event_parent_key ON watch_event
(parent_rating_key, watched_at);` to `SCHEMA` — and bump the `_WATCH_EVENT_COLUMNS` shape tuple so
existing caches actually re-run the DDL, or the index never lands on upgraded installs.

### P-5 · A fresh Tautulli client is built and torn down per poster request — **low**

`src/reaper/api/poster.py:64-68`.

A new `TautulliClient` (new httpx client, new connection setup) is constructed and closed on every
`GET /api/poster/{key}`. A cold review-queue page fires many poster requests before the browser's
day-long cache warms.

**Fix:** reuse a shared/pooled Tautulli client (e.g. off `app.state`) for the read-only artwork
proxy.

### P-6 · `_twin_group` calls `_plex_size_of` twice per candidate — **low**

`src/reaper/engine/identity.py:696-702`.

The comprehension calls `_plex_size_of(rk, basename, index)` twice per candidate (the `is not None`
test and the `== file_size` test), and each call re-scans the listing's `files`. On a large
ambiguous-id group this doubles the file-scan work.

**Fix:** compute `size = _plex_size_of(...)` once per `rk` (walrus) and compare the single value.

---

## 5. Production readiness

### PR-1 · A settings-read failure silently widens enrichment to every library, in the condemn direction — **medium**

`src/reaper/services/scan_runner.py:176-183`.

`_allowed_sections` catches bare `Exception` on the settings read and returns `None` ("scan every
library"). The docstring (160-174) deliberately defends this as the "safe" full-coverage fallback,
reasoning that scoping only ever *removes* enrichment. But removing a library from enrichment is
precisely what *keeps* its items (unmatched → abstain → kept), so re-adding every library on a
transient/malformed settings read silently re-includes libraries the operator turned off — their
dormant items acquire watch facts and enter the condemnable set, with **no degradation flag**. Note
the asymmetry: an explicit "all libraries off" yields an empty set (enrich nothing), but a read
*failure* yields `None` (enrich everything) — the failure picks the strictly more permissive branch.

**Fix:** distinguish read-failure from no-scoping-configured. On a read *failure* by a returning
operator (a config exists but can't be read), append a `pre_scan_degradation` (un-executable)
rather than silently widening; only default to `None` when the read succeeds and genuinely finds no
scoping. Correct the docstring's "safe" claim to name the condemn-direction risk.

### PR-2 · An enabled `SIZE` signal gets no danger warning, unlike the equivalent custom rule — **medium**

`src/reaper/engine/policy.py:907-920` (`inspect`) + `src/reaper/engine/signals.py:71-72`.

`inspect()` emits a `severity="danger"` warning when a *custom* condemn rule uses
`field == "size_bytes"`, but there is **no** branch checking `body.signals` for an enabled built-in
`SignalId.SIZE`. An operator who adds `SignalSetting(signal=SignalId.SIZE, weight=30)` (valid;
weights still total 100) gets size scored as condemnation pressure — the exact footgun the codebase
repeatedly calls out (the −50% lift that condemned popular 4K files) — with zero warning, in the
delete-more direction. The `SignalId.SIZE` docstring even claims "the UI warns about it," which the
backend detector does not back up (rule 7/24).

**Fix:** add a danger warning `for s in body.signals: if s.signal is SignalId.SIZE and s.weight > 0`,
mirroring the custom-rule branch; or verify and cite the frontend note the docstring refers to.

### PR-3 · A `PlexClient` is leaked on the common read-only path of the shelf cleanup — **medium**

`src/reaper/services/leaving_soon.py:539-541` (and `:396-420`).

`cleanup_sections` constructs `plex = await _plex_client(...)` (539) and then returns early on the
very next line when `not safety.leaving_soon_write_allowed` — the DEFAULT read-only state — without
`plex.aclose()` (rule 34). Every time a library or the whole shelf feature is toggled off while
deletion is not armed (the common case), a `PlexClient` and its pooled connections leak. `run_sync`
has the same shape: `plex` is built at 396 but the `try/finally` that closes it does not begin until
409, so an exception from `grace_report`/`build_notifier` (399-400) leaks it too.

**Fix:** construct the client only after the write-allowed/`None` guard, or wrap
construction-through-use in a single `try/finally` / `AsyncExitStack` that always closes `plex`,
including the early-return branch.

### PR-4 · A Tautulli spine timeout aborts the whole scan instead of degrading — **low**

`src/reaper/services/library_index.py:102-123` and `src/reaper/services/season_scan.py:998`.

The Tautulli "spine" reads (`libraries()`, paginated `library_media_info`) in `_spine` have no
`try/except`; a raise propagates through `gather_reaped` → `build_index` → the awaited index task →
`except BaseException: reap(...); raise` in `scan()`, aborting the whole run with **no viewable
snapshot**. Every other evidence source (Radarr `_movies_from`, Sonarr `_series_from`, the plexapi
`_sweep`) degrades instead. A transient Tautulli library-list timeout therefore kills the run rather
than producing a degraded, viewable, un-executable snapshot — fail-closed, but worse operator UX
than a degrade and inconsistent with the module's own philosophy.

**Fix:** catch `IntegrationError` in `_spine` (or at the `build_index` boundary) and route it through
`degrade(...)`. If abort is intentional because the spine is foundational, document that at the call
site.

### PR-5 · The IMDb-dataset skip count is computed for drift detection but never surfaced — **low-medium**

`src/reaper/services/imdb_dataset.py:106-141`, `:247`, `:328`.

`parse_rows`' docstring says `counters["skipped"]` exists "so the caller can surface format drift,"
but `load` only *logs* it (247) and returns `rows` alone; `refresh` returns only `rows`. No caller
can read the skip fraction, so an upstream TSV-format change that silently drops (say) half the rows
while staying above the zero-row tripwire shrinks rating coverage — and lost rating coverage makes
well-rated titles deletable — with nothing surfaced (rule 7/24).

**Fix:** return/persist `skipped` alongside `rows` and gate a degradation on a high skip fraction, or
correct the docstring to say it is log-only.

### PR-6 · The Tautulli history page loop advances by a constant and trusts a filtered total — **low**

`src/reaper/services/history_sync.py:322-387`.

The page loop advances `start += PAGE_SIZE` (not `+= len(rows)`) and terminates on
`total = int(page.get("recordsFiltered") or 0)`. If Tautulli returns rows without `recordsFiltered`
(fallback `0`), the loop breaks after page 1; if a middle page returns fewer than `PAGE_SIZE` rows,
`start` skips the un-fetched remainder. Both truncate the mirror, and the horizon
(`MIN(watched_at)`) then reads shallow — the single largest mass-deletion vector. Backstopped
fail-closed by the horizon gate, so low severity, but the primary loop should not trust a filtered
total that defaults to the page size (rule 56).

**Fix:** terminate on `not rows` and advance by `len(rows)`.

### PR-7 · `candidate_detail` parses the explanation unguarded while every sibling extractor defends it — **low**

`src/reaper/api/routes.py:888-890`.

`candidate_detail` does `Explanation(**json.loads(row.explanation_json))` unguarded, while every
sibling display extractor (`_primary_reason` 501-504, `_chip` 637-640, `_dormant_for` 545-547,
`_fired_gates` 1578-1581) catches `(ValueError, TypeError)`. A malformed or legacy `explanation_json`
row 500s the why-panel instead of degrading — against the module's own "display extraction must never
error a row off the queue" rule.

**Fix:** guard the `json.loads` and the `Explanation(**…)` construction, falling back to a minimal
explanation.

### PR-8 · Destructive-path list/string inputs have no length bounds — **low**

`src/reaper/api/schemas.py:470-475` (`CreateRunIn.media_keys`), `:889-912` (`SpareIn.media_key` /
`OverrideIn.media_key`); route `runs.py:475`.

`CreateRunIn.media_keys: list[str] | None` has no `max_length`, and the `media_key` fields have no
length bound (only `note` is capped at 500). An authenticated caller — or a leaked API key on
`POST /api/runs`, which is in the write-allow list — can submit an arbitrarily large `media_keys`
list or a multi-megabyte key that flows into `build_plan`/whitelist queries.

**Fix:** add `Field(max_length=...)` to `media_keys` and a reasonable `max_length` to the `media_key`
fields.

### PR-9 · The shutdown path runs the Plex settle-wait and trash purge inside cancellation — **low**

`src/reaper/services/executor.py:875-906`.

The `finally` block calls `_commit_and_finalize` on every exit including the
`asyncio.CancelledError` (shutdown) path, and `_finalize_plex` → `_wait_for_scan` polls up to
`_plex_settle_attempts * _plex_settle_delay` (default 10 × 2s = 20s) **per affected section** and can
then `empty_trash`. A container shutdown that interrupts a real run can be delayed ~20s per section
inside the cancellation's `finally` and may purge trash during shutdown. Interlock-gated (not
unsafe), but the latency is real.

**Fix:** skip or hard-bound the settle wait / trash purge when the run ended via `CancelledError`
(pass a flag so `_commit_and_finalize` commits state and defers the purge).

### PR-10 · The grace clock resets for every in-grace item on the first post-migration scan — **low**

`src/reaper/services/snapshot.py:1307-1321`.

`_apply_first_flag` restarts the grace clock when `last_seen_condemned_at is None`. After the
additive migration that added that nullable column, every pre-existing `FirstFlagged` row is `NULL`,
so the first scan post-migration resets `first_flagged_at = now` for every item currently in grace,
silently extending everyone's countdown by a full window. The direction is safe (keeps files longer),
but it is an unannounced one-time reset.

**Fix:** treat `last_seen_condemned_at IS NULL` on an *existing* row as "already condemned, unknown
when" — keep `first_flagged_at` and just backfill `last_seen_condemned_at = now`.

### PR-11 · A backup can exceed the size its own restore will accept — **low**

`src/reaper/services/restore.py:94-99` vs `src/reaper/services/backup.py:130-219`.

Restore caps the extracted `reaper.db` member at 8 GiB (`_MEMBER_CAPS[DB_ARCNAME]`), but the backup
path (`_build_into`) imposes no size ceiling. A long-lived install whose `reaper.db` has grown past
8 GiB (snapshots with runs are never pruned — `ReapRun.snapshot_id` is `ondelete="RESTRICT"` — and
candidate rows accumulate) can produce a backup its own restore then refuses.

**Fix:** cap/warn on the backup side to match, or raise the restore ceiling (extraction is already
streamed and capped, so a higher bound is safe).

### PR-12 · A startup catch-up task's exception is never retrieved — **low**

`src/reaper/main.py:227`.

`catch_up = asyncio.create_task(catch_up_on_startup(...))` has no done-callback and is only cancelled
at shutdown. If it raises (beyond the graceful "dataset not downloaded yet" case), the exception is
never retrieved — asyncio logs a bare "Task exception was never retrieved" at GC time with no context.

**Fix:** attach a done-callback that logs any non-cancellation exception as a structured warning.

### PR-13 · Expired auth sessions are never swept — **low**

`src/reaper/auth/sessions.py:58-93`.

Expired `AuthSession` rows are pruned only when that specific token is presented again
(`resolve_session`) or on password change/deactivation. Sessions from devices never revisited (30-day
TTL each) are never swept, so the table grows indefinitely on an active install.

**Fix:** add a periodic `DELETE FROM auth_session WHERE expires_at <= now` to the maintenance
scheduler.

---

## 6. Security

### S-1 · The pre-auth Plex login endpoints have no throttle (DoS + plex.tv amplification) — **medium**

`src/reaper/api/auth.py:224-258` (`plex_start` / `plex_poll`), with `services/login.py:114-142`.
*Found independently by two reviewers.*

`POST /api/auth/plex/start` and `/plex/poll` are unauthenticated and have **no throttle at all**,
unlike `/local` (→ `login_throttle`) and `/recover` (→ `recover_throttle`). Each `plex/start` inserts
a `PendingPlexLogin` row and fires an outbound `plextv.create_pin()`. A scripted flood (a) grows the
DB with pending rows pruned only opportunistically on a later start, and (b) amplifies outbound
requests to plex.tv, which can get the instance's egress IP rate-limited (429) and lock the legitimate
operator out of Plex sign-in. The fixed CSRF header is an equality check any non-browser client can
send, so it is not a rate limiter. `ratelimit.py`'s docstring claims the login routes are "the only
unauthenticated, state-establishing surface" and are all covered — these two are not.

**Fix:** add a per-IP `Throttle` (looser, like `recover_throttle`) to `plex_start`/`plex_poll`, keyed
on `client_ip(request)`, recording a failure on refusals, and cap pending-row creation per IP.

### S-2 · The Discord webhook secret in the URL path is not scrubbed from logs — **medium**

`src/reaper/logging.py:49-55` + the `logbuffer` sink.

The Discord webhook secret lives in the URL **path**, not a query string. The scrubber that runs on
stdlib `logging` records (`_RingHandler.emit` → `_redact_str`) and on structlog string leaves
(`_redact_value` → `_redact_str`) removes **query-string** secrets only (`_SECRET_QS`). Path-embedded
secrets are caught *solely* by key-name matching (`_SECRET_KEYS`). So any path that emits the full
webhook URL as a plain string — a third-party HTTP library logging a request line at WARNING+ (which
survives the `_NOISY_LOGGERS` WARNING pin), or a structlog event putting the URL under a non-secret
key like `url=` — writes the live token into the in-memory ring and the on-disk `reaper.log`, both
downloadable from the Logs tab. `logging.py`'s own comment names this exact threat as the reason for
pinning httpx, but a WARNING pin does not remove WARNING+ lines.

**Fix:** extend `_redact_str` to also match the Discord webhook path shape
(`/api/webhooks/\d+/[^/\s?]+` → redact the token segment), so path-embedded secrets are scrubbed
regardless of the key they are logged under.

### S-3 · The scrypt cost is too low for the offline-dictionary threat it defends — **medium**

`src/reaper/crypto.py:42-45`.

`_SCRYPT_N = 2**14` (r=8 → ~16 MiB, a few ms). The stated threat is an offline dictionary attack
against a low-entropy operator-supplied `REAPER_SECRET_KEY` when the DB leaks. At n=2^14 an attacker
gets on the order of thousands of guesses/sec/core — thin protection for a memorable passphrase — and
the KDF runs only once per key at boot, so a far higher cost is affordable.

**Fix:** raise the scrypt cost to n=2^15–2^17 (adjust `_SCRYPT_MAXMEM`), forward-compatibly: derive
the primary key at the new N while keeping the old-N derivation registered decrypt-only, mirroring the
existing legacy-key pattern, so existing tokens still decrypt.

### S-4 · The Argon2 concurrency gate counts requests, not hashes, so N admins multiply the cost — **medium-low**

`src/reaper/services/admin_password.py:62-72`, with `api/settings.py:1204`, `:1245`.

`verify()` loops over **every** local admin and runs a full Argon2 `verify_password` for each, while
the whole `verify()` call is wrapped in a single `argon2_gate.acquire()`/`release()`. The gate counts
*requests*, not hashes, so one gated arming/password request performs N Argon2 verifications while
occupying one slot — the "cap concurrent expensive Argon2 verifications" invariant is not actually
bounded (rule 11). Most installs have one admin; multi-admin installs multiply CPU per gated request.

**Fix:** bound the number of admins verified (a single canonical admin-password row), or acquire one
gate slot per hash.

### S-5 · Corrupted salt/key material regenerates instead of refusing to boot, bricking stored credentials — **medium-low**

`src/reaper/secrets.py:98-119` (salt), `:181-202` (key).

If `secret.salt` is present but unreadable/malformed, `resolve_kdf_salt` silently mints a fresh random
salt and overwrites (only a `log.warning`). The docstring calls salt loss "survivable" via the
decrypt-only fixed v1 salt — but that only covers data written under the fixed salt; any credential
written under the *previous per-install random salt* becomes permanently undecryptable. The parallel
`secret.key` empty-file branch (186) likewise regenerates a new key, bricking every stored credential.
For a fail-closed tool, corruption of the material protecting destructive-capable credentials should
refuse to boot, not regenerate-and-proceed.

**Fix:** on unreadable-but-present salt/key material, raise (refuse boot) with an actionable message,
or gate regeneration behind an explicit operator flag. At minimum escalate the log to error and surface
it in the UI safety state.

### S-6 · The rotating log file is created world-readable while a comment claims owner-only-from-creation — **low-medium**

`src/reaper/logbuffer.py:138-146`, `:227-234`.

The comment at 231 says the log files are "Owner-only from creation … no more readable than the
databases beside it (rules 14/83)," but only the *directory* is chmod'd to `0o700`. `_FileSink`
constructs a stdlib `RotatingFileHandler` with no mode, so the file itself is created world-readable
(`0644` under a typical umask). The DEBUG trail carries per-item reasoning; if the `0700` dir is ever
loosened or the file copied out, it is world-readable — contradicting the `os.open(O_EXCL, 0o600)`
guarantee `secrets.py` actually implements (rule 7/14/24).

**Fix:** create the log file owner-only from the outset (open with `0o600` under a clamped umask before
handing to the handler, or `chmod` after first open), and correct the comment if the file mode is
intentionally left to the dir.

### S-7 · `X-Forwarded-Proto` is trusted from any peer to decide the cookie's `Secure`/`__Host-` flags — **low-medium**

`src/reaper/auth/cookie.py:31-41`.

`is_secure_request` honors `X-Forwarded-Proto` from **any** peer with no trusted-proxy gating, whereas
`middleware.client_ip` deliberately only honors `X-Forwarded-For` from configured trusted proxies. The
asymmetry means the header that decides the cookie's `Secure` flag and `__Host-` name is
attacker-influenceable: on a plain-HTTP deployment a client sending `X-Forwarded-Proto: https` causes a
`Secure`/`__Host-` cookie the browser then drops. Impact is limited to the sender's own response, but
it is an unguarded trust of a forwarded header in the auth path.

**Fix:** gate `X-Forwarded-Proto` on the same `trusted_proxies` check `client_ip` uses (only consult it
from a trusted peer).

---

## 7. Improvements

### I-1 · `library_guid_index` batch enrichment is issued raw with no window or completeness check — **low-medium**

`src/reaper/clients/plex.py:642-646`.

The batched `/library/metadata/{ids}` enrichment read (ratings children + folder paths) is issued raw
with no container window and no completeness check, unlike the primary sweep which is complete-or-raise.
A server that windows the multi-id response silently drops rating/path enrichment for the tail of each
400-key chunk. The primary map still contains every item and missing evidence only lowers pressure
(fail-safe), so this is not a completeness failure — but it silently weakens the rating/path signal for
large chunks with no log or degrade.

**Fix:** window/verify the batch, or at minimum log when the returned element count < requested.

### I-2 · `add_label`'s docstring claims a runtime assertion that does not exist — **low-medium**

`src/reaper/clients/plex.py:949-954`.

The docstring states the label-preservation property "is **asserted here** … because if a future Plex
release changed it, the failure would be silent and would destroy user data." No runtime assertion or
read-back exists — `add_label` just calls `addLabel(label).saveMultiEdits()`. This is the
comment-naming-a-safeguard-that-isn't-implemented pattern rules 7/24 forbid.

**Fix:** implement a read-back that verifies pre-existing labels survived, or reword the comment to say
the property was verified against a live server and is assumed at runtime.

### I-3 · The session-cookie comment claims a sliding refresh that is not implemented — **low**

`src/reaper/auth/sessions.py:26`.

The comment states "The cookie is refreshed on the client every request," justifying the throttled
`last_seen` write. In fact `set_session_cookie` is called only at login/poll/recover; neither the
middleware nor `/me` re-sets it. Sessions are fixed 30-day (non-sliding) from login — a fine design,
but the comment describes a sliding refresh that does not exist (rule 7).

**Fix:** correct the comment to describe the fixed-window session, or implement the sliding refresh if
that was the intent.

### I-4 · `probe_connection` builds a raw `httpx2.AsyncClient` outside the shared client machinery — **low**

`src/reaper/clients/plextv.py:330-335`.

`probe_connection` constructs a raw `httpx2.AsyncClient`, bypassing `BaseClient`'s retry, error-mapping,
and timeout-kind reporting. It is read-only (`GET /identity`), closed via `async with`, and the token
rides a header (not the URL), so it is safe — but it is a one-off HTTP path outside the shared machinery
and gets no retry on a transient blip during connection probing, where a retry would most help.

**Fix:** route through a small `BaseClient` GET, or accept the exception explicitly with a comment.

### I-5 · `stream_to` bypasses the retry layer every other read gets — **low**

`src/reaper/clients/public.py:57`.

The streaming download calls `self._client.stream(...)` directly, so it does not get the `@retry` layer
that wraps `_request`. A single transient transport blip mid-transfer aborts the entire ~280 MB dataset
fetch, forcing a full restart. Best-effort with caller-owned temp-file-and-rename, so low severity.

**Fix:** wrap the streamed fetch in a bounded retry, or note explicitly that streamed fetches opt out of
the retry policy.

### I-6 · `_plays` returns a misleading dead third tuple element — **low**

`src/reaper/engine/backtest.py:311-316`.

The third tuple element is documented as "friendly_name-ish" but is just `str(row.user_id)`; `run()`
never uses it (it resolves names via `names.get(...)` at 513). Dead, misleading data a reader may assume
regret names come from.

**Fix:** drop the third element (return `(user_id, when)`), or actually populate a friendly name.

### I-7 · An incremental-sync comment misstates the overlap window — **low**

`src/reaper/services/history_sync.py:311`.

The inline comment says the incremental sync re-asks "with a **day** of overlap," but
`INCREMENTAL_OVERLAP = timedelta(days=2)` (and the module docstring at 76-78 correctly says two days).
Harmless to behavior (overlap is free via `INSERT OR REPLACE`), but a comment that misstates a
safety-window constant should be corrected (rule 7).

**Fix:** correct the comment to "two days."

---

## What was checked and found sound

To keep these from being re-flagged later, reviewers explicitly confirmed the following hold on this
tree:

- **The armed deletion path.** `execute_run` re-derives the effective condemned set and recomputes the
  confirmation phrase live, so a post-plan override change forces a re-confirm; the manifest re-hash
  matches on both sides (whole condemned set, spares excluded); caps, the confirmation phrase, and
  `_deletable` all derive from the same effective set read live; journal SENT/VERIFIED marks are
  committed (not flushed) per step with an atomic `UPDATE ... WHERE state = PLANNED` claim.
- **Empty-selection fail-closed.** `build_plan` and `runs.create_run` distinguish `None` from `set()`
  (rule 1); the middleware API-key lane is deny-by-default and cannot arm/execute/change settings.
- **The size interlock's dual guard** (`build_plan` hold-back + `size_confirmed` / `_may_send_unmeasured`
  at send) is present in both places (rule 28).
- **Grace-clock reset on re-entry and on spare/un-spare** (rule 4) via `_apply_first_flag` +
  `_sync_grace_clocks`.
- **The engine core.** `score()` is unsigned over a fixed denominator with strictly-subtractive keeps
  (no inversion under failure); `decide_verdict` is the single decision function and `backtest.run`
  decides on rounded integers through it; `identity.resolve` fails closed on every id/tier contradiction
  and passes all id kinds; `fields.evaluate` degrades a bad stored condition to `blocked` rather than
  raising out of `score()`.
- **Protection sync fail-closed** for missing rows and empty HARD lists; the `_spine` pagination
  terminates on the raw page count (`len(rows) < 1000`), not a filtered one.
- **Auth.** Sign-out-everywhere is wired on both password-reset paths and on deactivation;
  `resolve_session` refuses deactivated users; timing-equalized local login with per-IP + per-account
  `login_throttle`/`argon2_gate` ordered throttle → shed → hash; the secret-*key* file is created
  atomically owner-only (`os.open(O_EXCL, 0o600)` under a clamped umask); CSRF (custom header +
  `Sec-Fetch-Site`) is sound; recovery tokens are `print`-only and never reach the `/api/logs` ring.
- **Additive-migration invariant** respected by the new nullable / `server_default` columns;
  `EpochDateTime` treats unknown/zero/naive as absent.
- **All outbound HTTP goes through `clients/`** — no raw `httpx`/`requests` in the API or service layers
  (the sanctioned Discord and `public.py` exceptions aside); `GuardedTransport`/`GuardedSession` armed-
  and-declared gating, the GET-shaped-mutation classification, and the exact-path benign allow-list are
  all intact.

---

## Agent Rules

Direct, enforceable constraints for the next coding agent, derived from what this pass found. Written as
blockers, not suggestions. They extend the existing numbered rules in `CLAUDE.md`.

1. **Case-fold both sides of every label/tag/name match.** When one side of a lookup is lower-cased
   (`by_label` keys), the other side must be too. A keep-tag, collection, or list match that lower-cases
   the source but not the operator's configured value is a fail-open protection bug (B-1). Add a
   mixed-case test for any new name-matching path.
2. **Resolve every Plex library by section *key*, never by title.** `server.library.section(title)` is
   banned in new code — use `sectionByID(section_key)`. Duplicate library titles resolve to the wrong
   library; this applies to trash, refresh, count, and refresh-status calls, not just label/collection
   writes (B-2, B-3).
3. **Every dict keyed by a display name or title is a bug when the key can collide.** Key maps,
   membership indexes, and path tables by a stable id (section key, per-user id, media-kind + tmdb id).
   A bare tmdb id is not unique across movie vs TV (B-3, and rule 52).
4. **Every Plex/`*arr` list read that can be windowed goes through the complete-or-raise paging loop.**
   Raw `server.query(...)` / raw multi-id metadata reads that silently truncate are banned; page through
   `_iter_section_pages` or assert `totalSize` completeness (B-4, I-1). A page loop advances by
   `len(rows)` and terminates on `not rows`, never on a filtered total that defaults to the page size
   (PR-6).
5. **A populated container that filters down to zero usable items is a failure, not an empty success.**
   Distinguish container-missing *and* all-items-unusable from genuinely-empty before any atomic
   `DELETE + reinsert` of protection membership; when members are stored, preserve them and degrade
   (B-5, and rule 27).
6. **A settings/config read *failure* is not the same as "no config."** On the safety-scoping path, a
   read error by a returning operator degrades the snapshot (un-executable); only a successful read that
   finds nothing configured may fall back to the permissive default. Never let a transient read error
   silently widen what can be reaped (PR-1).
7. **Detect a degraded evidence source by a typed flag on the context, never by substring-matching a
   free-text reason.** Any `"some-source" in " ".join(reasons)` coupling between a producer and a
   consumer is a blocker; carry an explicit boolean (`activity_degraded`) instead (H-1, B-11).
8. **A comment naming a runtime safeguard must cite the code that implements it, verified present.**
   This pass found docstrings claiming an `add_label` read-back assertion (I-2), a sliding session
   refresh (I-3), owner-only-from-creation log files (S-6), a UI SIZE-signal warning (PR-2), and a
   surfaced IMDb skip fraction (PR-5) — none implemented. Implement it or fix the comment in the same
   change (rules 7/24).
9. **`Absent` is "not applicable," never "could not read," on every numeric signal arm.** Route an
   `Absent` observation to `NOT_APPLICABLE` (evaluated, weight retained, coverage intact) exactly as
   `SEASON_RANK` and the graded custom path do; leaving it in the `raw is None`/`UNREADABLE` branch
   mislabels the why-panel and drags coverage (B-7).
10. **Every `WHERE col IN :keys` over a scan-sized set is chunked at ≤500.** An unchunked expanding
    bindparam overflows SQLite's variable ceiling and aborts the scan or 500s the request; chunk it, or
    express the filter as an anti-join/subquery (P-1, P-3). Any new `parent_rating_key`-style filter
    also needs its covering index, with the cache column-shape tuple bumped so the DDL re-runs (P-4).
11. **Every numeric API bound is validated at the boundary with `ge`/`le` (or `max_length`).** A `min()`
    cap without a floor lets `limit=-1` become `LIMIT -1` = unbounded; destructive-path list/string
    inputs (`media_keys`, `media_key`) carry a `max_length` (B-8, PR-8).
12. **Every constructed client is closed on every branch, including early returns and pre-`try`
    exceptions.** Build the client only after the guard that may return early, or wrap
    construction-through-use in one `try/finally`/`AsyncExitStack` (PR-3, rule 34).
13. **A display/why-panel extractor never raises a row off the queue.** Guard every `json.loads` +
    model construction on a stored explanation with `(ValueError, TypeError)` and fall back to a minimal
    value, matching the sibling extractors (PR-7, B-10).
14. **Mark an item VERIFIED only when a delete was actually issued.** If the live file/id re-resolve
    returns an empty set, `_mark_skipped` ("no files resolved; kept") — never count an approved size as
    deleted (B-9).
15. **Throttle every unauthenticated, state-establishing endpoint per-IP.** The fixed CSRF header is not
    a rate limiter; `plex/start` and `plex/poll` need the same per-IP throttle as `/local` and
    `/recover`, and outbound-amplifying routes cap their per-IP resource creation (S-1, rule 11).
16. **Redact path-embedded secrets in the log scrubber, not only query-string ones.** Add the Discord
    webhook path shape to `_redact_str` so a token in a URL path is scrubbed regardless of the log key
    it rides under (S-2, rule 13).
17. **Corruption of key/salt material refuses to boot; it never regenerates and proceeds.** Regenerating
    silently bricks every credential written under the prior material. Raise with an actionable message
    or gate regeneration behind an explicit operator flag, and surface it in the UI safety state (S-5,
    and rule 2).
18. **Cap concurrent expensive hashes by hash, not by request.** A gate wrapping a loop that runs N
    Argon2 verifications per slot does not bound CPU; acquire one slot per hash, or reduce to a single
    canonical admin-password row (S-4, rule 11).
19. **A forwarded request header that changes an auth/security decision is trusted only from a
    configured trusted proxy.** Gate `X-Forwarded-Proto` on the same `trusted_proxies` check
    `X-Forwarded-For` already uses (S-7).
20. **Every evidence-source read in the scan pipeline degrades on failure; it never aborts the whole
    run or fails open.** A Tautulli spine timeout routes through `degrade(...)`, not an uncaught raise
    (PR-4); a success response with a null/malformed body is distinguished from a genuine empty and
    degrades rather than reading "nothing found" (B-11, and rule 28).
21. **A background task created with `create_task` has a done-callback that logs its exception.** A
    fire-and-forget startup/maintenance task must not swallow a raise at GC time (PR-12).
22. **A hardcoded list that mirrors the model/schema set carries a drift guard.** The restore
    auth-purge list, generated-asset manifests, and server-defined id lists derive from one declaration
    or are covered by a test that fails when the set changes (R-3, and rules 64/68).
23. **A value derived two ways in two modules is derived once in a shared helper.** Dormancy-days,
    condemn/score/coverage, and any parallel field list (`_OBS_FIELDS`) have exactly one derivation;
    prefer `dataclasses.fields(...)` over a hand-maintained parallel list (R-1, R-2, and rule 3).
