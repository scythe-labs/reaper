# Fourth-pass review remediation — phase plan & tracker

The fourth diff review (`docs/CODE_REVIEW.md`, dev @ `cea72d1`, 2026-07-23) found **36
findings**: 0 critical, 2 high, 11 medium, 23 low. This file breaks them into five
subsystem-cohesive phases and tracks progress. Each phase is sized to one agent context and
ends by running the CLAUDE.md verification gates for the files it touched.

**The conversation is compacted between phases.** Whoever picks up the next phase: read this
file, read the named findings in `docs/CODE_REVIEW.md`, then implement that phase end to end.
Fix *twins together* (rule 72) — the phases are grouped so twins land in one change.

## Progress

- [x] **Phase 1 — Timed-spare expiry + grace** (the two highs) — *done 2026-07-23*
- [x] **Phase 2 — Backup & restore security** — *done 2026-07-23*
- [x] **Phase 3 — Plex client hardening + Seerr/Scales attribution** — *done 2026-07-23*
- [x] **Phase 4 — Frontend (review queue + shell)** — *done 2026-07-23*
- [x] **Phase 5 — Platform, logging, settings, docs, misc** — *done 2026-07-23*

**All five phases are landed.** The 36 fourth-pass findings are remediated on `dev` (uncommitted).

---

## Phase 1 — Timed-spare expiry + grace  ✅ DONE

**Findings:** B-1 (high), B-2 (high). One root cause; one fix closes both.

**What was wrong:** `overrides_effective_at` dropped an expired spare only in the scan's
in-memory map; nothing ever deleted the `WhitelistEntry` row, so every live consumer
(planner, executor, grace, review queue — all read `overrides()`) kept the expired spare in
force forever. The item became permanently unplannable/un-executable (B-1), and the grace
window burned down invisibly so a later clear landed it on "ready" with no countdown (B-2).

**What was done:**
- `services/whitelist.py` — added `purge_expired_spares(session, now)`: the durable half of
  expiry. Selects then deletes `decision == "spare" AND spare_expires_at <= now` by predicate
  (no `IN`-list, no variable-limit risk), returns the purged keys for logging. Updated the
  `overrides()` docstring to name it as the durable realization.
- `services/snapshot.py` — `scan()` calls `whitelist.purge_expired_spares(session, now)` right
  after `overrides_effective_at(session, now)`, same `now`, same session the snapshot commits
  (scan_runner commits at `scan_runner.py:697`). Emits `scan.spares_expired` (count only).
  The existing `record_first_flagged_bulk` over `condemned_keys` already writes the fresh grace
  clock for the re-condemned item (its clock was deleted when the spare was set).
- `api/whitelist.py` — defense in depth for B-2 / rule 71: `_sync_grace_clocks` gained a
  `cleared_spare` param; the two removal routes capture the prior decision and pass
  `cleared_spare=(prior == "spare")`, forcing a fresh grace clock when a protective spare is
  cleared (never coast on a timestamp accrued while the item was invisibly condemned).
- `db/models.py` — updated the `spare_expires_at` docstring to describe the durable purge.
- Tests: `tests/test_whitelist.py::TestPurgeExpiredSpares`; scan-level realization test in
  `tests/test_snapshot_*` (see the phase-1 test note in that file).

**Rules this satisfies:** 70 (one durable realization point, shipped with the feature),
71 (clearing a protective override restarts the grace clock), 7/24 (docstrings match code),
23 (every consumer converges).

---

## Phase 2 — Backup & restore security  ✅ DONE

**Findings:** S-1, S-2, S-3 (all medium), B-4 (medium), S-4 (low), PR-3 (low), I-2 (low).

**What was done:**
- **S-1** (rule 73): `restore.stage_upload` now mints a per-staging token (`pysecrets.token_hex`),
  writes it beside the staged files (`TOKEN_MARKER`) so it travels with the atomic rename, and
  returns it on `RestoreSummary`. `arm(settings, token)` verifies it (`hmac.compare_digest`)
  before forcing deletion off, refusing with 409 if the staging was replaced since review.
  Threaded end to end: `RestoreSummaryOut.token`, `RestoreConfirmIn.token`,
  `restore_confirm` → `arm`. **Frontend was touched here too** (see "Deviation" below).
- **S-2** (rule 74): `_summarize` reads the staged database's OWN `alembic_version`
  (`backup._read_revision`), refuses a None, and refuses a manifest revision that disagrees with
  it; the schema gate now runs on the artifact's revision, and `RestoreSummary.revision` reports
  it. Never trusts the manifest's claim alone.
- **S-3** (rule 75/12): `_purge_auth_state` clears `auth_session`, `recovery_token`, and
  `pending_plex_login` from the staged db in `arm`, each guarded by an existence check (an older
  backup may predate a table). Uses literal `DELETE` statements (no interpolation → no S608).
- **B-4** (rule 76): new `secrets.env_key_active(settings)` mirrors `resolve_secret_key`
  precedence. `backup._build_into` sets `key_included = key_path.is_file() and not env_key` (so a
  stale file is never bundled and `key_source` reads "env"); `api/backup.backup_info` computes
  `key_in_backup` the same way.
- **S-4** (rule 83/14): `restore._member_writer` opens the key/salt via
  `os.open(..., O_CREAT|O_WRONLY|O_EXCL, 0o600)` under a clamped umask; `_copy_capped` takes
  `owner_only`, set for `secret.key`/`secret.salt`. The 0600 mode survives the boot `shutil.move`.
- **PR-3**: `_build_sync` wraps the build in try/except that rmtrees the temp dir and re-raises;
  `download_backup` cleans the archive on any pre-response exception; `backup.sweep_stale_temp`
  clears `.backup-tmp-*`/`.restore-tmp-*`/`.restore-upload-*` at boot from `preflight.main`
  (prefix constants now live in `backup.py`; restore + api import them). Sweep never touches
  `pending-restore` or `pre-restore-*` (no leading dot).
- **I-2** (rule 85): `download_backup` records `last_backup_at` from a Starlette `BackgroundTask`
  that runs after the stream completes, never before — a byte-zero-death no longer claims a copy.

**Tests:** `tests/test_restore.py` — token replaced/refused (service + API, 409), db-revision-not-
manifest gate, no-revision-db refused, auth purge, 0600 key/salt; arm calls thread the token, and
`_tiny_sqlite`/`_make_archive` gained `revision`/`db_revision`/`with_auth` knobs.
`tests/test_backup.py` — stale-key-not-bundled (B-4), `sweep_stale_temp` (PR-3). 1990 pass.

**Deviation from the phase split (read before Phase 4):** S-1 makes the confirm token
*required*, so it had to be threaded through the frontend in the SAME change or restore would
break across the compaction boundary (rule 64: a contract change carries its whole supply chain).
Done: `frontend/src/api.ts` (`RestoreSummary.token`, `restoreConfirm(password, token)`) and
`frontend/src/components/Settings.tsx` (`RestoreCard.restore` passes `summary.token`). **Phase 4
does NOT need to touch the restore token** — it is already wired. All frontend gates were run.

---

## Phase 3 — Plex client hardening + Seerr/Scales attribution  ✅ DONE

**Findings:** B-3 (medium), I-1 (low) [Plex client twins]; B-5 (medium), B-6 (medium),
B-10 (low), I-3 (low), P-1 (low) [fairness / requested_by].

**What was done:**
- **B-3** (rule 72/56): extracted the season sweep's hardened paging into one module-level
  `clients/plex._iter_section_pages(server, section_key, query, *, what)` — raw-count advance,
  a child without a `ratingKey` raises, `totalSize` is the sole paging authority (a clamped page
  is followed to the end, a full page with no `totalSize` raises), never `totalSize`→`size`. All
  four sweeps now run through it: `library_guid_index`, `library_season_index` (refactored to the
  helper too, so the four can never drift), `labeled_in_section`, `section_rating_keys`. New
  `tests/test_plex_sweep.py::TestTwinsHardenedPaging` pins clamped-follow and the two raise cases
  on the twins.
- **I-1** (rule 6/57): `add_label`/`remove_label` now take `section_key: int` and resolve via
  `server.library.sectionByID(section_key)`, dropping the `section_title` param; `leaving_soon.
  sync_section` passes `section_key`. `tests/test_plex_labels.py` asserts `section_ids == [7]`.
  **Deferred in writing (rule 72):** `item_count`, `is_refreshing`, `refresh_path`, `empty_trash`
  still resolve by title — they are single named-section operations outside the shelf-write pass,
  not twins of the label writes the review scoped. Left as-is this pass; convert when one is next
  touched.
- **B-5** (rule 77/61/3): `CandidateInfo` gained `override` and `effective_condemn`;
  `_load_candidates` merges `whitelist.overrides` + the ONE production `condemned.effective_
  condemned` (never a re-derived verdict), so a hand spare drops a scan-condemned title off the
  board and an engine-honored hand reap adds one. `roll_up` and `build_person_detail` gate
  reclaimable on `effective_condemn`. Module + `roll_up` docstrings corrected. Tests:
  `TestOverrideAwareReclaimable` (spare / honored-reap / held-reap) + a DB test proving the
  wiring (`test_a_hand_spared_condemned_title_drops_off_the_board`).
- **B-6** (rule 78): new `CandidateInfo.season_number` (parsed via the one `MediaRef` parser)
  and `fairness._scope_to_request(cands, req)`; per-PERSON granted/reclaimable/watched/link now
  bind only the seasons a request asked for (co-requesters scope independently). The deduped
  report totals stay over the whole matched title (documented; B-6 is per-person). Tests:
  `TestScopeToRequest`, `TestSeasonScopedAttribution`, and a drawer DB test
  (`test_a_season_scoped_request_charges_only_its_season`).
- **B-10** (rule 6/29): extracted `season_scan.season_requester(...)` (pure, testable) with the
  corrected precedence — season-precise `season_key(tvdb, n)` now outranks the show-level
  `rating_key_key`, which still beats the whole-show union. `TestSeasonRequester` pins that two
  people asking for different seasons attribute to their own season.
- **I-3**: new `SeerrClient.plex_machine_id()` (reads `/settings/plex`); `build_map(sources, *,
  reaper_plex_machine_id=None)` files the `plex:rk` tier only when the portal shares Reaper's
  Plex (reaper id read off the already-open connection in `scan_runner`, no extra round-trip).
  Unknown on either side keeps today's behavior. Docstrings corrected. `TestBuildMapRatingKey
  Namespace` covers match / foreign / unknown-either-side.
- **P-1**: `_fetch_available` fetches portals CONCURRENTLY (`asyncio.gather`, fail-hard preserved);
  new app-scoped `fairness.RequestCache` (created lazily on `app.state` in `api/fairness.py`, one
  per app so tests stay hermetic — rule 37) is shared by the board and the drawer.
  `test_the_shared_cache_reuses_one_portal_read_across_calls` proves 3 loads → 1 read.

**Gates:** ruff check/format ✓, mypy ✓ (95 files), pytest **2012 passed** (+22 new), alembic
upgrade+check ✓ (no schema change this phase). Frontend untouched, so its gates were not run.

**Assumptions that held / notes for later:**
- B-5's expired-spare permanent-disagreement is already closed by Phase 1's scan-time purge; this
  fix covers the *live* hand-spare-between-scans case, which is real and now correct.
- B-6 report-total scoping is deliberately whole-matched-title (per-person is scoped); a reviewer
  wanting season-scoped *totals* would be a follow-up, not a gap.

---

## Phase 4 — Frontend (review queue + shell)  ✅ DONE

**Findings:** B-7 (medium), PR-1 (medium), B-11 (low), B-12 (low), PR-5 (low), U-2 (low),
U-3 (low), U-4 (low), U-5 (low), I-5 (low). All in `frontend/src/`.

**What was done:**
- **B-7** (rule 79/64): `refreshReview` (`ReviewQueue.tsx`) now also invalidates `["candidate"]`,
  so its "names every review cache" comment is true. But invalidation alone can't fix the open
  why-panel: its `selectedId` is a candidate row id bound to the OLD snapshot, so a refetch of
  that id can only return a stale row. `showLatest` therefore calls a new `onClearItemSelection`
  prop (App wires it to clear ONLY an item selection — the show panel is keyed on a stable group
  key and refreshes in place, so it is left open). Test: `ReviewQueue.test` "Show latest closes
  an open why-panel."
- **PR-1** (rule 36): `ReapSheetLoader` (`App.tsx`) destructures `isPending`/`error` and renders a
  `ModalShell` fallback (loading line, or a plain-language error, 404 worded specially) instead of
  `return null`. ModalShell's own ✕ is the working close, and the `useBackGuard(reapSheetRun)`
  no longer keys a dead press on an invisible sheet.
- **B-11** (rule 80): `useBackGuard` gained an optional `canClose` predicate; a refused Back
  re-registers (re-parks the sentinel) so Back stays armed rather than being spent on a close that
  never happened. `JobsPanel` holds a `savePendingRef` that `ScheduleModal` mirrors `save.isPending`
  into, and passes `() => !savePendingRef.current` as the guard — the same condition the modal's
  scrim/Escape/✕ already honor. Tests: `backnav.test` "Back refuses a guarded overlay while locked,
  then closes once unlocked."
- **B-12**: `BackNavProvider` reconciles a sentinel left parked before a reload — on mount it reads
  `history.state`, and if it is our sentinel, steps back over it (`history.back()` swallowed by
  `selfPopRef`) or, with nothing to step to, `replaceState(null)`. Guarded by a `reconciledRef` so
  StrictMode's double-effect runs it once. Test: `backnav.test` "reconciles a sentinel left parked
  before a reload."
- **PR-5** (rule 85): the "Updated to the latest scan" toast no longer fires synchronously with the
  invalidation. `useReviewFreshness` gained `onSilentCaughtUp` (fired only when a silent refresh's
  swap actually lands, `behind` → false) and `refreshFetching` (React Query keeps old data on a
  refetch error, so error flags stay clear; a fetch that went and *settled* while still behind is
  how a failed silent refresh is told from one in flight — it raises the nudge instead of a phantom
  toast). Tests: hook-level (caught-up + settled-still-behind) and `ReviewQueue.test` (toast only
  after catch-up; nudge, not toast, on a failed refetch).
- **U-2** (rule 61): new `showReapReach(seasons)` returns all/some/none over the same
  `override`/`override_effective` fields as `groupReapEffective`; `ShowInheritBanner` branches on it
  so a whole-show reap the engine holds on every season reads "the reap is noted, but the seasons
  are kept for now," a mix qualifies, and only all-effective keeps the blanket "removed" wording.
  Test: `SeasonList.test` "qualifies the whole-show reap banner when the engine holds every season."
- **U-3** (rule 17/36): `NotInScanPanel` accepts `isPending`/`error` and renders a loading line and
  a `notice-error` before the empty-state all-clear; `App` passes the fairness query's own
  `isPending`/`isError`. The definite "Every available request is in the last scan" now renders only
  when the report loaded and was genuinely empty. Tests in `NotInScanPanel.test`.
- **U-4**: `SpareMenu`'s capture-phase scroll-close now skips while the Custom-length input is open
  (read through a `customRef` so the listeners are not re-subscribed per keystroke, rule 19).
  **The code half is done and correct; the device half (a phone's keyboard-open scroll actually
  closing the menu) still wants the verify skill on real hardware — the review rescoped it that way
  and it is NOT reproduced here.**
- **U-5**: the nav tab handler (`App.tsx`) now clears `setScalesUnmatched(false)` beside
  `setScalesUser(null)`, so leaving and returning to Scales never re-shows the unmatched panel.
- **I-5** (rule 24): `ModalShell`'s header comment now cites the schedule editor as the `canClose`
  user and notes the reap sheet is deliberately closable (its run is detached, carried by the
  ReapBar).

**Gates:** `npm --prefix frontend run lint` ✓, `test` **239 passed** (+10 new), `build`
(tsc --noEmit + vite) ✓. No backend files touched, so the Python gates and alembic were not run.

**Notes for later / assumptions:**
- B-7 was fixed by *closing* the item panel, not re-resolving it to the new snapshot's row (both
  were sanctioned by the review). Closing is the fail-safe choice — the operator re-opens the item
  on the fresh scan rather than acting on evidence that may have moved. The show panel self-updates
  and is left open.
- PR-5's failure path keys on `isFetching` (a fetch that started and settled while still behind),
  NOT on `error`/`isError`, because React Query retains the last-good data on a background refetch
  error and leaves those flags clear. This is the load-bearing subtlety and is pinned by tests.
- U-4's on-device confirmation is the one open item; treat it as the verify-skill follow-up the
  review named, not a silent gap.
- Phase 2 already wired the restore token through the frontend, so Phase 4 did not touch it (as the
  Phase 2 deviation note promised).

---

## Phase 5 — Platform, logging, settings, docs, misc  ✅ DONE

**Findings:** PR-2 (medium), U-1 (medium), S-6 (low), S-5 (low), B-8 (low), B-9 (low),
PR-4 (low), U-6 (low), I-4 (low), I-6 (low).

**What was done:**
- **PR-2** (rule 82): `logbuffer._FileSink.write` replaced its `suppress(Exception)` with a
  one-shot degraded flag. On the first steady-state write failure it flips
  `_file_sink_healthy` and announces once through the ring (`_mark_file_sink_degraded`; the
  re-entry the announcement causes finds the flag already down and no-ops, so no recursion or
  re-spam). New `logbuffer.file_sink_healthy()`; `configure_file_logging` resets the flag on a
  fresh sink. `api/logs.download_logs` appends the in-memory ring behind a marker line when the
  sink is degraded, so a read-only-remount trail is never silently truncated.
- **U-1** (rule 86): `ScheduleModal` (`Settings.tsx`) now reads the effective zone from the
  shared `["general-settings"]` query and renders "Times use your server time zone: {zone}.
  Change it in Settings, General." (generic phrasing only while it loads), instead of the static
  "often UTC in Docker" guess.
- **S-6** (rules 83/14): `configure_file_logging` creates `logs/` with `mkdir(mode=0o700)` and an
  unconditional `chmod(0o700)`, so a dir left world-readable by an earlier version is tightened
  too. The 0700 dir confines the files (no other account can traverse in); the optional
  per-file 0600 opener was dropped because `RotatingFileHandler` has no `opener` param and rule
  83 mandates the *directory*, which is covered.
- **S-5** (rules 84/13): new `api/settings._validate_external_url` (http/https + hostname, the
  same `urlsplit` check the Plex/server-address fields use) runs at the edge of
  `create_instance`/`update_instance`; a scheme-less paste or `javascript:` value 422s and is
  never stored. Mirrored client-side in `ServiceModal` (`isWebUrl`) so the save is blocked
  before the round-trip. Blank still clears; an omitted field still keeps.
- **B-8** (rule 81): `20260721_2000_add_instance_import_exclusion` gained the sibling heal
  migration's reflection guard (`_has_column`); it skips the `add_column` when the column is
  already present, so a database created during the ~30-minute baseline-edit window upgrades
  instead of boot-looping on "duplicate column name." Editing this additive migration is safe
  (databases that ran it never re-run it); the frozen baseline `22777b2b5015` was NOT touched.
- **B-9** (rules 24/58): `history_sync.ensure_schema`'s rebuild is serialized behind a
  per-event-loop `asyncio.Lock` (`_rebuild_lock`, a `WeakKeyDictionary` keyed on the running
  loop, because a single module-level lock would bind to the first test's loop and break every
  other — the suite runs a fresh loop per test). The false "SQLite serializes writers, so the
  read under the write lock is the authority" comment is corrected to name the lock as the real
  mutual exclusion (pysqlite runs the PRAGMA + DDL in autocommit with no `BEGIN IMMEDIATE`).
- **PR-4** (rule 87): `scheduler.reschedule_timezone` wraps each `apply_scan_schedule` /
  `apply_maintenance_schedule` in the same `try/except ValueError` (`+ KeyError` for
  maintenance) guard startup uses, logging `scheduler.bad_scan_cron` /
  `scheduler.bad_maintenance_cron`. A stored-but-malformed cron can no longer 500 the timezone
  save or half-apply the zone (some jobs moved, some not).
- **U-6**: the pace table's per-run disk floor (`understandingPolicy.ts`) changed from a
  unitless "1" to "Any amount."
- **I-4** (rule 24): the four profile-fallback degradation citations (`profiles.py:59`/`:123`,
  `scan_runner.py`, `api/runs.py`) now cite rule 65 (silent recovery on operator-configured
  safety values), not rule 14 (atomic secret files).
- **I-6** (rules 66/67): `LogsOut` returns `files_kept` (`logbuffer.files_retained()` =
  `LOG_BACKUP_COUNT + 1`); `LogsPanel` renders that count instead of the hardcoded "3," so the
  copy tracks the backend constant, which now carries a cross-reference comment.

**Gates:** ruff check ✓, ruff format --check ✓, mypy ✓ (95 files), pytest **2023 passed** (+11
new), alembic upgrade head + check ✓ (no schema drift — B-8 edits an existing migration, adds no
revision). Frontend: eslint ✓, vitest **241 passed** (+2 new), build (tsc --noEmit + vite) ✓.

**Notes for later / assumptions:**
- B-9 was rescoped by the review to a false-comment/no-real-lock defect (the reachable outcomes
  are a redundant double rebuild or a loud `no such table`, both non-lossy; the nightly full
  sweep refills regardless). The per-loop-lock fix makes the serialization real *and* corrects
  the comment; it is modest-stakes, not a data-loss fix.
- S-6's per-file 0600 was intentionally NOT added: `RotatingFileHandler` exposes no `opener`
  hook (verified), subclassing `_open` was judged more risk than value, and the 0700 dir already
  confines the files. Rule 83 mandates the directory, which is done.
- The `os.chmod` → `Path.chmod` and constant-`.encode()` → bytes-literal changes were lint-driven
  (ruff PTH/S103/UP012); behavior is unchanged.

---

## Notes for every phase

- Run the CLAUDE.md verification gates for the surface you touched before calling the phase
  done. Backend phase: `ruff check`, `ruff format .`, `mypy src/reaper`, `pytest`,
  `alembic upgrade head` + `alembic check`. Frontend phase: `npm --prefix frontend run lint`,
  `test`, `build`. Always `ruff format .` (not `--check`) before staging.
- No identifying info anywhere (golden rule). American English. No em dashes in operator copy.
- Update the Progress checklist above when a phase lands, and record any assumption that turned
  out wrong in `docs/PLAN.md` / `docs/LEARNINGS.md`.
- Commit only when the user asks.
