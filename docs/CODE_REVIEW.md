# Diff review — `dev`, changes since `80d19f5` (fourth pass), 2026-07-23

> **Scope.** Everything since the third review pass's findings doc landed: the reviewed
> range is `80d19f5..cea72d1` (87 commits, 188 files, +21.5k / −2.6k lines), which
> includes the third pass's own fix commits and the 78 commits after the resolution
> closed at `741105c` (2026-07-21). The
> range covers: the httpx → httpx2 client migration
> (BaseClient/GuardedTransport and Discord); backup-and-restore to one portable file; the
> in-app docs system with the safety-flow pages; the Seerr multi-portal / Scales drawer
> work (per-portal requester keying, kind+id service maps, the rating-key tier, root-folder
> → library mapping); timed spares; the operator-set scheduler time zone; rotating file
> logs with download; per-item scan decision logging; browser Back navigation; fail-closed
> hard-gate protection sync; and the review-queue freshness work. This is a *diff* review,
> not a whole-codebase pass; the third diff review (dev @ `4478aa7` scope, 2026-07-21) is
> preserved in this file's git history.
>
> **Method.** Nine scoped reviewer passes (clients/httpx2, backup/restore, scan pipeline,
> deletion path, platform/migrations, Seerr/Scales, review-queue frontend, frontend shell
> + docs, logging), each reviewing its group across all eight categories and required to
> verify every candidate against the working tree before reporting. Every critical, high,
> and medium candidate was then adversarially re-verified by an independent pass told to
> refute it: **0 were refuted**, 3 were rescoped to narrower claims, and the rest were
> confirmed with the failing path traced end to end. Two findings were discovered
> independently by two reviewers working from different directions (the timed-spare expiry
> root cause, and the external-URL validation gap) — those are merged below, which is as
> confirmed as it gets here. After merges: **36 findings — 0 critical, 2 high, 11 medium,
> 23 low.** The two highs share a single root cause (timed-spare expiry is never made
> durable) and one fix closes both.
>
> **CI gates on this tree (`cea72d1`):** `ruff`, `ruff format --check`, `mypy src/reaper`,
> pytest (1976 passed), `eslint`, vitest, `tsc` + `vite build`, and `alembic upgrade head`
> + `alembic check` are all green. `docker build` is CI-only per policy and was not run
> locally. One environment note: a stale local venv reproduces 92 collection errors
> (`fixture 'httpx2_mock' not found`) until `uv sync --extra dev` is re-run — the
> `pytest-httpx2` plugin is correctly declared and locked, and CI's
> `uv sync --frozen --extra dev` installs it, so this is a developer-machine footgun, not
> a repo defect. Nothing below is caught by the gates.

---

## 1. Bugs

### B-1 · An expired timed spare never leaves the live override set, so the item can never actually be reaped — **high**

`src/reaper/services/whitelist.py:86` (`overrides_effective_at`), `src/reaper/services/snapshot.py:731`,
`src/reaper/services/planner.py:348`, `src/reaper/services/executor.py:742` / `:1107`,
`src/reaper/services/grace.py:101`, `src/reaper/api/routes.py:274`

Found independently by two reviewers. The scan judges candidates through
`overrides_effective_at(session, now)`, which drops an expired spare so the item is
re-condemned — but that realization exists only in the scan's in-memory map. Nothing ever
deletes or invalidates the `WhitelistEntry` row (the only delete path is the operator's
explicit clear), and every live consumer reads `overrides()`, which returns the expired
row forever: the planner drops the item from every plan, the executor vetoes it at send,
the grace report and Leaving Soon exclude it, and the queue still shows it spared (chip
reads "expired"). Concrete failure: spare a condemned title for 7 days; after day 7 the
next scan re-condemns it, yet it is permanently unplannable and un-executable until the
operator manually clears the stale spare. The docstrings on `WhitelistEntry.spare_expires_at`,
`overrides()`, and the snapshot call site all promise a re-entry that is never implemented
(rules 7/24), and no live consumer handles the expired state at all (rule 23). Direction
is fail-toward-keeping, so no data loss — but the feature dead-ends for every operator who
uses it.

**Fix:** realize expiry durably in the scan transaction: after
`override_map = await whitelist.overrides_effective_at(session, now)`, delete the spare
rows the read dropped (`decision == "spare" AND spare_expires_at <= now`, same `now`) in
the same session the snapshot commits (e.g. a new `whitelist.purge_expired_spares`). The
live map (planner, executor, grace, routes) then converges the moment the snapshot lands,
and `record_first_flagged_bulk` already writes the fresh grace clock. Update the
docstrings to match, and add a test asserting that after a realizing scan the item is
plannable and appears in the grace report.

### B-2 · The grace window is silently spent while an expired spare still protects; clearing the stale spare lands the item on "ready" with no countdown — **high**

`src/reaper/api/whitelist.py:91` (`_sync_grace_clocks`), `src/reaper/services/snapshot.py:1232` (`_apply_first_flag`)

The consequence of B-1, and a rule-4 violation in its own right. Setting a timed spare
deletes the FirstFlagged clock. After expiry, every scan re-condemns the item and the
first post-expiry scan recreates the clock, then each scan refreshes
`last_seen_condemned_at` — while every live surface still treats the item as spared, so it
is absent from the grace report and Leaving Soon and the window burns down invisibly.
When the operator finally clears the expired spare (the only way to make the item
reapable), `_apply_first_flag` sees a recent `last_seen_condemned_at` and does NOT restart
the clock. Inputs: 7-day spare, daily scans, spare cleared 3 weeks after expiry with
7-day grace → remaining = 0, the item is immediately "ready," and the household warning
window never happened. Rule 50's promise ("re-enters on a FRESH window, never a spent
one") is not honored on this path. Deletion still requires the armed host, plan, and
phrase, so the loss is the warning window, not an unattended delete.

**Fix:** the B-1 root fix closes this (the clock and the item's visibility in grace start
together once expiry is realized durably). Defense in depth regardless: when
`_sync_grace_clocks` removes a spare override, delete the FirstFlagged row before calling
`record_first_flagged_bulk`, so a cleared spare always re-enters on a fresh window.

### B-3 · `library_guid_index` (and two twins) still page on the filtered child count, so the GUID sweep can silently return a partial map despite its complete-or-raise docstring — **medium**

`src/reaper/clients/plex.py:574-585` (`library_guid_index`), `:717-722` (`labeled_in_section`), `:771-776` (`section_rating_keys`)

The third pass's B-4 hardening (raw-count paging, totalSize authority, fail-closed raises)
was applied only to `library_season_index`. `library_guid_index` — which WAS touched in
this range (library-title stamping), triggering the previous review's own "apply to the
twins when next touched" condition — still advances `start` by the ratingKey-filtered
count, terminates on the filtered short page, and falls back `totalSize` → `size`
(= the page size), which rule 56 explicitly forbids. Three silent-truncation paths, each
ending a section early with a normal return: a child without a ratingKey in a full page;
a server that clamps the container below the requested size (the documented-real case the
season fix follows to the end); `totalSize` absent on a large section. The consumer
(`services/library_index.py:93-100`) degrades the snapshot only on `PlexError`, so a
silent partial map bypasses the un-executable degradation: items beyond the cut lose
their id-tier identity and binding falls to basename/title matching, with wrong-bind and
misjoined-watch-history risk — the exact fail-open the docstring's raise contract exists
to prevent (rules 56, 7/24). `labeled_in_section` and `section_rating_keys` are
byte-identical twins with a smaller blast radius (shelf reconcile scoping).

**Fix:** port the season sweep's paging verbatim to all three: page and terminate on the
raw child count; raise `PlexError` on a child without a ratingKey, on a full page without
`totalSize`, and on an empty page before `start` reaches `totalSize`; never fall back from
`totalSize` to `size`. The consumers already handle `PlexError` by degrading, so raising
is safe.

### B-4 · Backup claims key_source "file" (and bundles a stale key) when the active key is the env key — **medium**

`src/reaper/services/backup.py:151` (`_build_sync`), `src/reaper/api/backup.py:100` (`backup_info`)

`_build_sync` sets `key_included = key_path.is_file()` and derives the manifest
`key_source` from it; `backup_info` reports `key_in_backup` the same way. But
`resolve_secret_key` gives `REAPER_SECRET_KEY` absolute precedence over the file, and the
operator copy tells env-key users to delete the file — which can linger. Failure: operator
runs with the env key set while an old `secret.key` file remains; the backup bundles the
stale, inactive key and both the panel and the restore summary claim self-sufficiency
(their env-key warnings stay hidden because `key_in_backup` is true). Restored onto a
target without the env var, the stale bundled key is used and every stored credential
silently fails to decrypt — despite the UI having said the key traveled inside.

**Fix:** derive the key source from actual precedence: when `settings.secret_key` is set,
write `key_source "env"` (omit the stale file or mark it inactive), and make
`BackupInfoOut.key_in_backup` consult the same precedence so the panel and restore summary
tell the operator the target still needs `REAPER_SECRET_KEY`.

### B-5 · Scales counts hand-spared titles as reclaimable because it never consults overrides — **medium**

`src/reaper/services/fairness.py:460` (`roll_up`), `:956` (`build_person_detail`)

Hand overrides live in the whitelist tables and are merged at review-queue read time;
`Candidate.verdict` stays the frozen scan verdict. `fairness` never imports whitelist and
gates "reclaimable" purely on `verdict == "condemn"`. State: operator hand-spares a
scan-condemned title in Review, then opens Scales before the next scan → the title still
counts in the board's reclaimable bytes/items and the drawer's fate ordering while Review
and the Reap page show it kept. (Via B-1, an expired timed spare makes the disagreement
permanent.) The symmetric hand-reap-on-abstain case disagrees the same way. Violates
rule 61, and falsifies the module docstring's claim that "Scales can never disagree with
Review" (rules 7/24). Read-only surface, no deletion impact.

**Fix:** load `whitelist.overrides(session)` in `_load_candidates`, carry the effective
override on `CandidateInfo`, and exclude spare-overridden candidates from the reclaimable
gate (optionally summarizing hand-reaped items separately). At minimum correct the
docstring in the same change if override-awareness is deferred.

### B-6 · Per-person disk attribution charges the whole show for a season-scoped request — **medium**

`src/reaper/services/fairness.py:459` (`roll_up`), `:951-982` (`build_person_detail`)

`roll_up` binds a TV request to every scanned season of the show via `_match_candidates`
and never reads `req.seasons` (populated by the Seerr client and actively used by
`requested_by` — the scope is available and simply ignored here). Inputs: person A
requests one small season of a show whose other seasons pre-existed or were requested by
others; output: A's row is granted the entire show's bytes, every condemned season counts
in A's reclaimable figures, and the drawer's watched figure spans seasons A never asked
for. On a board whose stated purpose is honest per-person attribution, the operator
misjudges who is holding the disk. The only show-shaped test uses an empty `seasons`
tuple, so partial-season scope (the default request shape in Seerr-family portals) is
untested and unhandled.

**Fix:** when `req.seasons` is non-empty, restrict the bound candidate set used for
granted/reclaimable/watched to those season numbers (season number is recoverable from
the candidate's media_key tail, or add it to `CandidateInfo`), keeping whole-show binding
only for empty-seasons requests. Alternatively state title-level granularity in the
drawer copy so the operator reads the number correctly.

### B-7 · `refreshReview` leaves an open why-panel showing the previous scan after "Show latest" — **medium**

`frontend/src/components/ReviewQueue.tsx:2075` (`refreshReview`), `frontend/src/App.tsx:527`

`refreshReview` invalidates `["candidates"]`, `["candidates-unfiltered"]`, `["group"]`,
`["snapshot"]`, `["reap-breakdown"]` but not `["candidate"]` (the open why-panel detail) —
despite its comment claiming it "names every review cache, the same way an override does"
(`useOverrideMutations.refresh` DOES invalidate `["candidate"]`). The nudge bar appears
precisely when the reviewer is busy, and an open panel is a busy condition, so the primary
path is: panel open → scan lands → "Show latest" → the list and tab counts move to the new
snapshot while the open why-panel keeps the previous scan's score/verdict/reasoning beside
them. Invalidation alone would not fix it: `selectedId` is the OLD snapshot's row id, so a
refetch returns the same stale row. The operator can Spare/Reap from evidence one scan
old. Violates rules 64 and 24.

**Fix:** in `showLatest` (and the silent-refresh path), either close the open panel or
re-resolve the selection to the new snapshot's row for the same media_key after the
candidates refetch lands; also add `["candidate"]` to `refreshReview` so the comment's
claim is true, or correct the comment if the panel is closed instead.

### B-8 · Unguarded `add_column` fails with "duplicate column name" on a DB created during the in-range baseline-edit window — **low**

`alembic/versions/20260721_2000_add_instance_import_exclusion.py:37`

An in-range commit added `add_import_exclusion` to the frozen baseline IN PLACE (merged to
dev), and a follow-up reverted the baseline and shipped the column as an additive revision
about 30 minutes later. A database created fresh during that window has the column in its
baseline-created `instance` table; the next `alembic upgrade head` runs the additive
revision's plain `add_column` with no existence guard, SQLite raises "duplicate column
name," and the container entrypoint (`set -eu` before `alembic upgrade head`) refuses to
boot on every restart until the operator hand-stamps or rebuilds — exactly the rebuild the
golden rule forbids. The sibling heal migration (`20260723_1000`, reflection-guarded
`_column()` check) proves the project already knows the pattern. Exposed population is
small (fresh dev-build DBs from one ~30-minute window), hence low — but the fix is
trivial and safe for every path that already succeeded.

**Fix:** add the heal migration's reflection guard to `20260721_2000`'s `upgrade()`: skip
the `add_column` when the column already exists. Editing the shipped migration is safe;
databases that already ran it never re-run it.

### B-9 · `ensure_schema`'s "re-read inside the write transaction" holds no write lock; the comment claims a mechanism the code does not provide — **low** (rescoped)

`src/reaper/services/history_sync.py:231`

The rule-58 fix re-reads `PRAGMA table_info` inside `async with engine.begin()` and
comments "the read under the write lock is the authority" — but the engine has no
`BEGIN IMMEDIATE` configured, and under pysqlite legacy transaction control the PRAGMA
read and the DROP/CREATE DDL all run in autocommit with no write lock held, so the
re-read gives no real mutual exclusion (rules 24/58). The adversarial pass refuted the
original data-loss framing: the reachable race outcomes are a redundant double rebuild or
a loud `no such table` OperationalError (fail-closed), and the nightly full sweep refills
within a day regardless — so this survives as a false-comment/no-actual-lock defect, not
a silent-partial-mirror bug.

**Fix:** serialize the rebuild path behind a module-level `asyncio.Lock` (all racing
callers share one process) or emit `BEGIN IMMEDIATE` for the block, and correct the
comment to name the mechanism actually used.

### B-10 · The show-level rating-key tier outranks the season-precise key, blurring per-season attribution — **low**

`src/reaper/services/requested_by.py:193`, `src/reaper/services/season_scan.py:1461`

`build_map` files a TV request's Plex ratingKey (stored at the show level by the portal)
into one `plex:rk:{show}` bucket for every requester of any season subset, and the season
consumer ranks that tier above `season_key(tvdb, n)`. In the common zero-config case
(one copy, portal sharing Reaper's Plex server): person A requests S1, person B requests
S2 → every season row now reads "A + 1 other," where the season-key tier previously
attributed S1 to A and S2 to B exactly. The rating-key commit silently traded season
precision for copy precision on all TV rows. Display-only, but a visible regression for
any multi-requester show.

**Fix:** for season rows, rank `season_key(tvdb, n)` above the show-level rating-key tier
(keeping rating-key above `show_key` so copy precision still beats the whole-show union),
or only file `plex:rk` for whole-show requests (`not req.seasons`).

### B-11 · Browser Back bypasses the schedule modal's `canClose` guard while a save is in flight — **low**

`frontend/src/components/Settings.tsx:1549` (`useBackGuard`), `:1297` (`canClose`)

`ScheduleModal` passes `canClose={!save.isPending}` so the scrim, Escape, and ✕ refuse to
close during a save, but `JobsPanel`'s `useBackGuard(editing !== null, () => setEditing(null))`
closes it unconditionally. Press Back while a save is pending: the modal is torn down, the
mutation's error (which renders inside the modal) is never shown, and the operator cannot
tell whether the schedule change stuck. The back layer silently bypasses the one close
guard the modal declares.

**Fix:** gate the guard's close on the same condition (`if (!savePendingRef.current)
setEditing(null)`), or thread a canClose-aware close through `useBackGuard` (re-park the
sentinel when close refuses).

### B-12 · A stale parked sentinel survives a page reload, making the first Back press after reload a dead press — **low**

`frontend/src/backnav.tsx:102`

`history.pushState(SENTINEL, ...)` entries persist across a reload, but `parkedRef` resets
to false and nothing at mount inspects `history.state`. Reload while any overlay/tab frame
is open: the current entry is the sentinel; the first Back press pops to the identical URL
beneath it and `onPop` finds no layers — one dead press, two to actually leave. Opening an
overlay after the reload parks a second sentinel on top of the stale one, so dead presses
accumulate.

**Fix:** on `BackNavProvider` mount, check `history.state` for the sentinel marker and
reconcile: `history.back()` with `selfPopRef` set (consume it), or
`history.replaceState(null, "")` to neutralize it.

## 2. Hacks and workarounds

None found. The nine passes specifically looked for guard bypasses, non-standard
patterns, and shortcut plumbing in this range; nothing rose to a finding. (The closest
candidates — the deliberate `overrides()` behavior between scans, the sanctioned Discord
webhook path, and the documented rating-key tier tradeoff — are all recorded as correct
or as scoped findings above.)

## 3. Refactor opportunities

None meeting the bar this pass. The one meaningful duplication found (the three unhardened
paging twins in `clients/plex.py`) is a correctness defect and is tracked as B-3; its fix
should extract or mirror the hardened paging contract rather than hand-copying it a third
time.

## 4. Performance

### P-1 · Every person-drawer open re-reads every Seerr portal in full and rebuilds the whole evidence index — **low**

`src/reaper/services/fairness.py:907` (`build_person_detail`)

`build_person_detail` re-pages all requests from every portal serially (one GET per 100
rows per portal), rebuilds the watch-evidence index over every candidate rating key, and
re-runs the user-list and quota reads — on each drawer click; `get_fairness` repeats the
same work on every board load with no shared cache. With a few thousand requests across
two portals, one click costs dozens of serial round-trips plus a burst of quota calls, so
the drawer feels seconds-slow and hammers the portal.

**Fix:** add a short-TTL in-process cache for the merged request list (and optionally the
evidence index) shared by `get_fairness` and `get_person`, or scope the drawer's evidence
query to the target person's bound candidate keys; fetch portals concurrently since the
reads are independent.

## 5. Production readiness

### PR-1 · `ReapSheetLoader` renders nothing forever on a failed run fetch, leaving the reap bar's View button dead — **medium**

`frontend/src/App.tsx:190`

`const { data: run } = useQuery(...); if (!run) return null;` with no error or pending
state. Query defaults are one retry and no refetch-on-focus, so after one failed retry the
query settles in error and the sheet renders null indefinitely — clicking "View report" on
the app-wide reap bar visibly does nothing, ever. Worse, `useBackGuard(reapSheetRun !==
null, ...)` keys on the state, not the render, so the next Back press is silently consumed
closing an invisible sheet. This is the surface for inspecting what a reap actually
deleted — a rule-36 recurrence on a safety surface (the sibling `ScanFreshness` component
already models the required fallback pattern).

**Fix:** destructure `isPending`/`error` and render a small fallback (loading line,
plain-language error, working close button that calls `onClose`).

### PR-2 · Steady-state log-file write failures are silent in-app, so the download serves a stale trail with no signal — **medium**

`src/reaper/logbuffer.py:144` (`_FileSink.write`), `src/reaper/api/logs.py:83-100`

`configure_file_logging` catches `OSError` only at setup, and with `delay=True` the
handler never touches disk at construction — so a volume that goes read-only after boot
passes setup cleanly, and every subsequent mirror write fails inside
`suppress(Exception)`. Nothing reaches the ring or the Logs tab. Failure: the data volume
remounts read-only mid-run (common on small-board/NAS deployments after disk errors); the
operator later clicks Download to attach logs to a bug report; `log_files()` still returns
the old on-disk files, so the ring fallback never engages, and the download is a trail
that silently ends at the remount while the Logs tab shows current lines. Contradicts the
module docstring's "the files carry exactly what the UI shows" (rule 24).

**Fix:** replace `suppress(Exception)` with a one-shot degraded flag that emits a single
warning through the ring; expose it (e.g. `logbuffer.file_sink_healthy()`) and have
`download_logs` append the in-memory ring (or at least a marker line) when the sink is
degraded, so the download is never silently stale.

### PR-3 · Backup temp dirs leak on failure and are never swept — **low**

`src/reaper/services/backup.py:123`, `src/reaper/api/backup.py:110-119`

`_build_sync` creates `data/.backup-tmp-*` then runs `VACUUM INTO` with no try/cleanup: if
it fails (disk full is the likely trigger, or "database is locked" past the busy timeout),
the dir and a possibly multi-GB partial snapshot stay behind — making the disk-full worse.
On the route side, any exception between `create_backup` and returning the
`StreamingResponse` leaks the finished archive (database plus master key) since cleanup
lives only in the stream generator's `finally`. Nothing at boot sweeps `.backup-tmp-*`,
`.restore-tmp-*`, or `.restore-upload-*` leftovers from a crash.

**Fix:** wrap `_build_sync`'s body in try/except that removes the temp dir and re-raises;
clean up the archive on any pre-response exception in `download_backup`; add a preflight
sweep for stale temp entries under `data/`.

### PR-4 · Timezone save does not guard `reschedule_timezone` against a stored malformed cron; startup does — **low**

`src/reaper/api/settings.py:1386`, `src/reaper/services/scheduler.py` (`reschedule_timezone`)

Startup deliberately wraps `apply_scan_schedule`/`apply_maintenance_schedule` in
try/except `ValueError` so a stored-but-malformed cron is logged and skipped. The same
stored values are replayed by the timezone save path with no such guard:
`CronTrigger.from_crontab` raises, the PUT returns 500 after the timezone was already
committed, and jobs earlier in the loop have moved to the new zone while later ones have
not — a partially rescheduled scheduler until restart. Same precondition startup defends
against (hand-edited row, or a future cron-parser tightening).

**Fix:** wrap each `apply_*` call in `reschedule_timezone` in try/except `ValueError` and
log exactly as startup does, so one bad stored cron cannot 500 the save or half-apply the
zone.

### PR-5 · "Updated to the latest scan" toast fires before the refetch settles — **low**

`frontend/src/components/ReviewQueue.tsx:2100` (`onSilentRefresh`)

The toast is set synchronously with the fire-and-forget invalidations, so it asserts
success before any refetch completes. If the candidates refetch fails (network blip right
after a scan), the list stays on the old snapshot: the toast has claimed the opposite,
the freshness hook has latched that snapshot as handled with no nudge, and no marker or
retry appears until an unrelated refetch. "Silently stale" is the exact state the hook's
docstring promises to prevent.

**Fix:** fire the toast when the list actually catches up (expose a caught-up transition
from `useReviewFreshness`, or await the invalidation promises and only then set the toast,
resetting the handled latch on failure so the marker can appear).

## 6. Security

### S-1 · Restore confirm is not content-bound: the password arms whatever is staged at that moment — **medium**

`src/reaper/api/backup.py:233` (`restore_confirm`), `src/reaper/services/restore.py:350` (`arm`)

`restore_confirm` verifies the admin password and then arms whatever sits in
`data/pending-restore` — no token, hash, or phrase binds the confirm to the staged content
the operator reviewed. `/restore/prepare` requires only a session + CSRF (no password) and
unconditionally replaces the staging dir. Failure: operator uploads backup A and reviews
its summary; before they confirm, a second session (a stolen cookie — a session alone is
below the password trust level this gate exists to enforce) stages a hostile-but-valid
backup B; the operator's password confirm arms B, and the next restart swaps in an
attacker-authored database (attacker-known admin password, attacker-controlled service
endpoints, emptied whitelist). `destructive` is forced off at arm, but the attacker can
log in post-restart and re-arm. The execute route's server-recomputed content-bound
confirmation phrase is the in-repo model; this equally consequential surface lacks the
binding. (The API-key lane cannot reach prepare — verified — so the vector is a second
session, which is exactly what the password step is supposed to outrank.)

**Fix:** have `stage_upload` mint a staging token (random, or a hash of the staged
manifest + db) written into the staging dir and returned in `RestoreSummaryOut`; require
it in `RestoreConfirmIn` and verify it in `arm()` before writing READY, refusing with
"the staged backup changed, review it again" on mismatch.

### S-2 · The restore schema gate validates the manifest's claimed revision, never the staged database's own `alembic_version` — **medium**

`src/reaper/services/restore.py:269` (`_summarize`)

`_summarize` takes the revision from `manifest.get("alembic_revision")` and passes it to
`_check_schema`; nothing reads the `alembic_version` table inside the staged `reaper.db`
(the backup side has `_read_revision` for exactly this; restore never calls it). A
tampered or repacked archive whose manifest claims a known revision while its db is any
SQLite file (foreign schema, ancient version, or no `alembic_version` at all) passes the
gate, gets armed, and is swapped in at boot. `alembic upgrade head` then runs against a
mismatched schema: with no `alembic_version` it replays every migration from base against
pre-existing tables — typically a boot loop until manual recovery from `pre-restore-*`,
or worse, a wrong-schema database the app then serves. The gate validates the claim, not
the artifact.

**Fix:** in `_summarize` (or `_extract`), read the staged database's own
`alembic_version` (reuse `backup._read_revision`'s logic), require it non-None and equal
to the manifest's claim before `_check_schema`, and refuse on mismatch with the existing
"couldn't be verified" copy.

### S-3 · Restore resurrects the backup's auth sessions; nothing invalidates them — **medium**

`src/reaper/services/restore.py:350` (`arm`)

`arm()` forces only `destructive_enabled` off in the staged database. The backup's
`auth_session` rows (30-day TTL) travel with it and become valid again after the swap.
Failure: operator's password is compromised, a backup is taken while the attacker's
session is live, operator later resets the password and signs out everywhere; restoring
that backup brings back both the old password hash AND the revoked session tokens, so the
attacker's cookie works again within its TTL. A restore is a wholesale credential change
and rule 12 requires session invalidation on credential change; the fail-closed precedent
(forcing deletion off) already lives in this exact function, and the sign-out-everywhere
primitive exists but is never called here.

**Fix:** in `arm()`, alongside `_force_destructive_off`, clear `auth_session` (and
recovery tokens and pending logins) in the staged database, so a restored install starts
signed out and everyone logs in fresh.

### S-4 · Restored `secret.key`/`secret.salt` are written with the default umask, not owner-only from creation — **low**

`src/reaper/services/restore.py:206` (`_copy_capped`)

Extraction writes every member with `out_path.open("wb")`, so under a 022 umask the staged
`secret.key` is 0644. The 0700 staging dir shields it until boot, but
`apply_pending_restore` then moves it into the data dir (typically a host bind mount),
where it sits world-readable from the preflight swap through `alembic upgrade head` until
`resolve_secret_key` finally runs `_ensure_owner_only`. Rule 14 forbids exactly this
write-then-tighten window; on a multi-user host any local account can read the master key
during migrations.

**Fix:** create the key/salt outputs via `os.open(path, O_CREAT|O_WRONLY|O_EXCL, 0o600)`
in `_copy_capped` (or chmod them in `apply_pending_restore` before the move).

### S-5 · Per-service external URL is stored and rendered with no scheme validation, unlike every sibling URL setting — **low** (merged: found on both sides of the wire; rescoped)

`src/reaper/services/instances.py:289` / `:371`, `src/reaper/api/settings.py` (`InstanceCreateIn`/`InstanceUpdateIn`), `frontend/src/components/ServiceModal.tsx:476`

`create_instance`/`update_instance` accept any non-blank string for `external_url` and
store it verbatim; the frontend field is `type="url"`, which accepts any scheme with a
colon. Every sibling URL boundary 422s unless the value starts with http(s). Failure
modes: (a) the common scheme-less paste (`host:8989`) saves silently and every jump link
built from it renders as a broken unknown-scheme URL, where the sibling fields would have
guided with a 422; (b) a `javascript:`/`data:` value is stored verbatim and rendered into
an `href` for every signed-in user. The adversarial pass refuted the original
privilege-escalation vector (the API-key lane cannot write instance settings — verified —
so only a full settings-trust session can plant it, and every consumer renders
`target="_blank" rel="noopener noreferrer"`, which neuters `javascript:` in modern
browsers), so what survives is a validation-consistency and UX defect plus a missing
defense-in-depth layer, off the deletion path.

**Fix:** validate at the API edge in `create_instance`/`update_instance`: non-blank values
must parse to scheme http/https with a hostname, else 422 with the same wording pattern as
the Plex web-address check; blank continues to clear. Optionally mirror the check
client-side before save.

### S-6 · The log dir and files are created world-readable under the default umask — **low**

`src/reaper/logbuffer.py:170`

`log_dir.mkdir(...)` uses the default mode and the rotating handler opens `reaper.log`
with builtin `open()` (0644 under umask 022). The logs are secret-redacted, but at DEBUG
they now carry the full per-item decision trail for the operator's library. On a
multi-user host with a world-traversable data mount, any local account can read that
trail. The exposure is comparable to the SQLite files in the same dir — hence low — but
the project's own standard is owner-only from creation (rule 14).

**Fix:** `log_dir.mkdir(mode=0o700, ...)` plus a chmod for a pre-existing dir; that one
change confines the live file and all rotations. Optionally a 0600 opener on the handler
for defense in depth.

## 7. UI/UX consistency

### U-1 · The schedule editor says times are "your server's clock, often UTC in Docker," but timed jobs run on the operator-set time zone — **medium**

`frontend/src/components/Settings.tsx:1323`

The range added a Time zone setting (Settings → General) that the scheduler uses for
every timed job, with the stored zone winning over the env seed and host zone. The
ScheduleModal help still unconditionally describes the server's clock and the common
UTC-in-Docker case. An operator who set their home zone reads this note, mentally converts
from UTC, and schedules the scan at the wrong local hour — the exact confusion the note
was written to prevent. Copy states a consequence the code no longer has (rules 24/53/55);
it is accurate only when no zone was stored.

**Fix:** render the actual effective zone (fetch general settings or pass the zone down):
"Times use your server time zone: {timezone}. Change it in Settings, General."

### U-2 · `ShowInheritBanner`'s reap wording asserts removal without branching on held reaps — **low**

`frontend/src/components/ReviewQueue.tsx:1347`

The banner reads "Every season below is removed unless you spare it here" from `override`
alone. For a show whose reap the engine refuses on every season, the card chip says "Reap
requested · kept for now" and every row carries a "Kept for now" chip, yet the header
between them still claims removal — and being spared is not why they are kept. Rule 61
requires removal prose to branch on effectiveness; the per-row chips mitigate but the
banner contradicts them on the same screen.

**Fix:** pass the show's effectiveness (`groupReapEffective` over the season set is
already computable in `SeasonList`) into the banner and branch: all-held → "the reap is
noted but the seasons are kept for now"; mixed → qualify.

### U-3 · `NotInScanPanel` renders a definite "Every available request is in the last scan." when the data is merely missing — **low**

`frontend/src/components/NotInScanPanel.tsx:55`, `frontend/src/App.tsx:723`

The panel takes a bare items array and the caller passes `fairnessReport?.unmatched ?? []`,
so pending/error collapse to "empty," and empty renders the definite all-clear. The
unmatched-panel flag is not cleared by the nav's view switch, so returning to Scales past
the query's gc window shows the panel open with a definite claim that was never checked.
"We could not look" rendered as "we looked and it was fine" is the anti-pattern rules
17/36 forbid.

**Fix:** have the panel accept `isPending`/`error` (or the query result) and render an
explicit loading line and notice-error fallback before the empty-state sentence.

### U-4 · `SpareMenu`'s capture-phase scroll close likely dismisses the Custom-length input the moment the mobile keyboard opens — **low** (rescoped)

`frontend/src/components/ReviewQueue.tsx:733`

The menu closes on any scroll anywhere (`window.addEventListener("scroll", onClose, true)`),
and "Custom length…" swaps in an autofocused number input. On phones the virtual keyboard
typically scrolls the viewport to reveal the focused field, which would close the menu
before a single digit is typed. The code half is fully traced (no suppression, reposition,
or visualViewport handling exists); the device half was not reproduced on hardware and may
not manifest in all layouts, so this needs the verify skill on a real phone. Presets,
Forever, and the default length are unaffected.

**Fix:** suppress the scroll-close while the custom input is open (skip `onClose` when
`custom` is true, or when the scroll originates from the focus reveal), or reposition
instead of closing; verify on a device.

### U-5 · Leaving Scales via the tab bar closes the person panel but leaves the "Not in the last scan" panel to reappear on return — **low**

`frontend/src/App.tsx:624`

The tab click handler calls `setScalesUser(null)` (with a comment saying the split view
should never linger on a tab with no panel to show) but not `setScalesUnmatched(false)`.
Open the unmatched panel, switch tabs, and return: it is still open, while a person panel
in the same situation would have been closed — inconsistent with the stated intent and
with the mutual-exclusion pairing three lines above.

**Fix:** add `setScalesUnmatched(false)` beside `setScalesUser(null)` in the handler.

### U-6 · The docs pace table lists a unitless floor of "1" for the per-run disk caps — **low**

`frontend/src/docs/content/understandingPolicy.ts:148`

The Floor column reads "1" for "Most disk freed per run" (the schema floor is one byte).
Next to "500 GB," a bare "1" reads as 1 GB or as nothing at all — failing read-at-a-glance
(rule 21 and the golden length rule).

**Fix:** replace with plain language ("any amount"), matching the other floors' wording.

## 8. Improvements

### I-1 · `add_label`/`remove_label` still resolve their section by title while rule 57 mandates `sectionByID`; the range fixed only the collection path — **low**

`src/reaper/clients/plex.py:958` (`add_label`), `:993` (`remove_label`)

The collection-detach fix moved to `sectionByID(section_key)`, but the label twins —
invoked in the same shelf sync pass right beside the now key-addressed call — still
resolve `server.library.section(section_title)`. With two libraries sharing a title,
plexapi returns the last match. Blast radius is thin (items are addressed by global
rating key, so the writes likely still land), but it is a live rule-57 violation and one
sync pass now mixes key- and title-addressed resolution for the same shelf.

**Fix:** thread `section_key` into both (the caller already has it, exactly as it did for
the collection fix) and resolve via `sectionByID`; drop the `section_title` parameters.

### I-2 · `last_backup_at` is recorded before any byte reaches the browser — **low**

`src/reaper/api/backup.py:116`

`download_backup` commits the timestamp before streaming starts, so a download that dies
at byte zero still updates the panel's "last backup" to now. The operator reads that
timestamp as evidence a safe copy exists — the state they check before a risky change —
but no file ever landed. The inline comment argues the copy is "not un-taken" by a dropped
connection, which inverts the honest direction: the operator's copy is what matters.

**Fix:** record the timestamp only after the final chunk is yielded (schedule the write
from the generator's `finally`), or reword the panel field to what the pre-stream write
can honestly claim.

### I-3 · The rating-key requester tier carries no Plex-server namespace, so a foreign-server portal can bind wrong items — **low**

`src/reaper/services/requested_by.py:111` (`rating_key_key`)

Rating keys are unique per Plex server only; a portal synced to a different server than
Reaper's (possible in multi-portal setups) files keys that can numerically collide with
Reaper's candidates, naming a requester on an unrelated item, and nothing abstains. The
docstring acknowledges and accepts this (rare, display-only), so it is a documented
tradeoff rather than an oversight — but the collision is detectable and avoidable.

**Fix:** read each portal's Plex machine identifier once and skip filing `plex:rk` keys
when it differs from Reaper's own server; keep current behavior when the check is
unavailable.

### I-4 · The profile fallback degradation cites "rule 14" instead of rule 65 in four places — **low**

`src/reaper/services/profiles.py:59`, `:123`, `src/reaper/services/scan_runner.py:573`, `src/reaper/api/runs.py:585`

Rule 14 is atomic secret-file creation; the rule governing this behavior is 65 ("silent
recovery on operator-configured safety values is forbidden"). A future auditor tracing
safeguard citations (rule 24 requires accuracy) is pointed at the wrong rule. The behavior
itself is correct — only the citations are wrong.

**Fix:** change all four citations to rule 65.

### I-5 · `ModalShell`'s header comment names the reap sheet as the `canClose` user, but `ReapConfirm` no longer passes `canClose` — **low**

`frontend/src/components/ModalShell.tsx:16`

Since the reap went detached, the reap sheet closes freely mid-run by design (the ReapBar
carries on); the only real `canClose` user is the schedule modal. A future reader will
believe the reap sheet is un-closable mid-flight and skip re-verifying (rule 24).

**Fix:** update the comment to cite the schedule modal, and note the reap sheet is
deliberately closable because the run is detached.

### I-6 · The "Newest 3 files" retention count is hardcoded with no cross-reference to the backend constant — **low**

`frontend/src/components/LogsPanel.tsx:239`, `src/reaper/logbuffer.py:53`

The help text hardcodes 3 while the real retention is `LOG_BACKUP_COUNT = 2` (+1 live
file). Currently accurate, but rule 67 requires coupled values to share one declaration
or carry cross-reference comments in both places; neither site names the other, so a
future bump silently makes the operator copy wrong.

**Fix:** add cross-reference comments both ways, or better, return the retention count in
the `/api/logs` response and render it (rule 66 shape).

---

## Verified correct (checked and found right — kept to prevent re-review churn)

The reviewers traced 115 suspicious-looking things and found them correct; the ones worth
keeping on record:

- **httpx2 migration**: GuardedTransport semantics survived exactly (SAFE_METHODS, the
  exact-path mutation allow-list, armed-then-journalled refusal order, the approval
  extension), verified against real clients in tests, not mocks; httpx2 2.9.0's exception
  hierarchy was introspected and matches httpx, so every `@retry` predicate and except
  clause still fires; timeouts, TLS verify default, redirect policy, and pool limits
  survived; request extensions reach a custom transport (executed probe); close owners
  intact; Discord Retry-After handling preserved and the webhook URL never logged; the
  `httpx2`/`httpcore2` noisy-logger pins are correct; no leftover respx decorators
  silently failing to intercept.
- **Backup/restore fundamentals**: no zip-slip (four fixed member names, bare filenames,
  decompression caps, magic checked at stage/arm/swap); upload spooled to a 0600 tempfile
  with an 8 GiB cap and unlinked on every path; READY-written-last arming with a
  timestamped pre-restore fallback; restored installs always boot unarmed; API-key lane
  fenced off both directions.
- **Scan pipeline fail-closed**: hard-mode protection lists degrade on failure with empty
  stored copies, missing rows fail closed; the root-folder library narrowing stands down
  on unknown libraries, twin groups, stale maps, and unconfirmed sizes; the
  fully-protected short-circuit can only protect more; both prune call sites use the one
  production function; new candidate fields are populated in all builders with additive
  migrations (rules 35/22 hold).
- **Deletion path**: journalled-intent-before-send ordering intact; the new logging moved
  no state transitions; caps count over the exact acted-on set; the timed-spare migration
  is additive and a NULL expiry reads kept-forever; `overrides()` keeping an expired spare
  protective BETWEEN scans is the deliberate safe direction (B-1 is only about the missing
  durable realization afterward); timed-spare datetimes are aware-to-aware throughout.
- **Platform**: the in-range baseline edit nets to docstring-only (the column add was
  reverted inside the range; B-8 covers the residue); the migration chain is linear and
  additive; DST behavior of the scheduler was verified by execution (nonexistent times
  shift, ambiguous times fire once); the entrypoint `chown -R` does not follow symlinks
  and leaves modes alone; all new env vars are documented and DB-backed where operator-
  configurable; per-job schedule rows cannot last-write-wins each other.
- **Seerr/Scales**: tmdb keys are media-kind namespaced both sides (the third pass's B-1
  does not recur); requester identities are portal-namespaced; service maps are keyed
  kind+id; the scoring-facing request index fails closed to Unknown while the display map
  stays fail-soft (the rule-2 split is right); pagination raises on missing totals;
  fairness endpoints are auth-guarded, read-only, and cannot feed the verdict path.
- **Review queue**: `spareRemaining` is correct at every boundary checked (a fresh N-day
  spare reads N, expiry reads "expired" while staying protective); `handFate` remains the
  single fate router; `showReapIsNoop`/`groupReapEffective` run over whole-show sets on
  every lane; bulk ops use `allSettled` with failed-key retention; the freshness hook
  decides once per snapshot and cannot fire against a partial scan; card numbers come from
  the server's planner-matched rollups (rule 62).
- **Docs pages vs code**: every preset value, signal point, protection threshold, and
  interlock the in-app docs name was checked against the engine and executor and matches;
  the save-triggers-a-scan claim is wired; the arming/env-seed claims match; no em dashes
  in operator copy.
- **Logging**: the HTTP-retry DEBUG line formats service name, attempt, sleep, and
  exception type only — no URL, params, or message, so tokens cannot leak at any level;
  the download endpoint is session-gated with server-generated filenames and no traversal
  surface; rotation is bounded (3 × 20 MiB, pinned by test); the WARNING pin on
  aiosqlite/SQLAlchemy cannot suppress operator-relevant errors; ring and file carry
  identical redacted lines.

---

## Agent Rules (fourth pass)

Direct constraints derived from what this review actually found. Written as blockers, not
suggestions, continuing CLAUDE.md's numbering; where one sharpens an earlier rule
(70 → 23, 71 → 4/50, 72 → 56/64, 74 → 27, 75 → 12, 77 → 61, 79 → 64, 82 → 24/28,
83 → 14, 86 → 21/53), the newer, more specific obligation governs.

70. **Time-bounded state has exactly one durable realization point, shipped with the
    feature.** Any stored decision that expires (a timed spare, a deadline, a TTL) must be
    realized by code that WRITES the transition — an in-memory filter at read time is not
    a realization — and every live consumer must converge after it. A docstring saying
    "the next scan realizes it" requires the scan to actually persist that realization in
    the same change; shipping the read half without the write half is a blocker (B-1;
    extends rule 23: "expired" is a stored verdict state every consumer must handle).
71. **Clearing a protective override always restarts the grace clock.** When an override
    that kept an item off the reap list is removed, the FirstFlagged row is deleted before
    `record_first_flagged_bulk` runs, unconditionally — never trust `last_seen_condemned_at`
    continuity across a period when the item was invisible to the operator (B-2; sharpens
    rules 4/50).
72. **A hardening fix lands on every twin of the fixed function in the same change.**
    Before closing a fix to a copied pattern (paging loops, section resolution, error
    mapping), grep for the pattern's siblings and fix or explicitly defer each in writing;
    "when next touched" deferrals are honored the moment ANY commit touches the twin, not
    only when someone remembers (B-3, I-1; sharpens rules 56/64).
73. **A password-gated destructive confirm is content-bound.** The confirm request carries
    a server-verified token derived from the exact content the operator reviewed
    (recomputed or stored server-side at stage time), and the action refuses if the
    content changed since review. The execute route's phrase is the model; any new
    stage-review-confirm flow (restore, import, bulk apply) must carry the same binding
    (S-1).
74. **A gate on an uploaded or restored artifact validates the artifact, never its
    manifest.** Any property a safety check depends on (schema revision, version, counts)
    is read from the artifact itself; a manifest or header claim may be cross-checked but
    never trusted alone (S-2; extends rule 27's spirit to imports).
75. **Restoring or importing an auth-bearing database is a credential change.** Purge
    session rows, recovery tokens, and pending logins in the staged data at arm time, in
    the same function that forces deletion off (S-3; extends rule 12).
76. **Provenance and self-sufficiency fields derive from runtime precedence, not file
    existence.** Anything reporting where a key/credential comes from or whether an
    artifact is self-contained must consult the same resolution order the runtime uses
    (`resolve_secret_key` precedence), never a bare `is_file()` (B-4).
77. **Backend reporting surfaces consult effective overrides.** Any service that
    summarizes items as removable/reclaimable/kept (Scales, breakdowns, exports) merges
    live override state the same way the review routes do, or its copy explicitly states
    it shows scan verdicts only (B-5; extends rule 61 from frontend prose to backend
    aggregation).
78. **Attribution honors the request's scope.** When a request carries a season (or any
    partial) scope, per-person figures bind only the scoped subset; whole-title binding is
    allowed only for unscoped requests or with the granularity stated in the copy beside
    the number (B-6).
79. **A cache-invalidation helper that claims completeness is grep-verified against every
    query key, and a detail panel keyed on a row id is closed or re-resolved when its
    snapshot is replaced.** Invalidation alone is insufficient when the key itself points
    at superseded data (B-7; sharpens rule 64).
80. **Every close affordance runs the modal's close guard.** Browser Back, gestures, and
    any new dismissal path must honor the same `canClose` the scrim/Escape/✕ honor; a
    back-layer close that bypasses a declared guard is a blocker (B-11; extends rule 60's
    spirit to the history layer).
81. **A baseline edit — even one reverted within hours — obligates a guarded migration.**
    If the frozen baseline was ever wrong in a merged commit, every additive migration
    covering that window carries the heal migration's reflection guard so in-window
    databases upgrade instead of boot-looping. Never edit the baseline, and when the rule
    is broken anyway, the follow-up is guarded, not plain (B-8; extends the frozen-
    baseline golden rule).
82. **A persistent sink degrades loudly, once.** Any always-on writer (log file mirror,
    export stream) that can fail after setup carries a one-shot degradation flag surfaced
    where its output is consumed; a bare `suppress(Exception)` around a steady-state write
    whose output is documented as an audit trail is a blocker (PR-2; extends rules 24/28
    to infrastructure sinks).
83. **Owner-only-from-creation applies to every copy of a secret and to decision-trail
    dirs.** Restored/extracted key material and newly created log directories get 0600 /
    0700 at creation, not after a later chmod window (S-4, S-6; extends rule 14 beyond
    first creation).
84. **Operator-supplied URLs validate scheme http/https at the API edge, everywhere, via
    one shared check.** Any new URL-shaped setting reuses the same validator the sibling
    fields use; a `type="url"` input is not validation (S-5; extends rule 13's boundary
    discipline).
85. **Success copy fires on settled state.** A toast, timestamp, or "done" indicator is
    set only after the operation it describes has actually completed (refetch settled,
    final chunk streamed) — never at issuance (PR-5, I-2; extends rule 21's honesty to
    timing).
86. **Copy describing a clock, zone, or schedule renders the effective stored setting.**
    Any help text that tells the operator what time base applies must read the setting
    that governs it, not a static guess about the deployment (U-1; sharpens rules 53/55
    for time).
87. **A guarded startup replay is mirrored on every runtime replay of the same data.**
    When startup wraps a stored-value replay in a tolerant guard (malformed cron, bad
    zone), every settings-save or reschedule path replaying the same stored values carries
    the same guard, so a save can never 500-and-half-apply what boot survives (PR-4;
    extends rule 55's side-entrance principle in the other direction).
