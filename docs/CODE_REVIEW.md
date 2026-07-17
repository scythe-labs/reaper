# Whole-codebase review — dev @ `5b885f5`, 2026-07-16

> This is the second whole-codebase review pass. The previous pass (whose findings were
> fixed) is preserved in git history for this file. Method: seven parallel area reviews
> (engine, execution path, data services, HTTP clients, API/auth/config, frontend,
> infra/tests); every critical and high finding was independently re-verified against
> source before inclusion. 55 findings: 1 critical, 9 high, 20 medium, 25 low.

> **Fix status (2026-07-16).** Every critical and high finding is fixed, each with
> regression tests: B-1, B-2, B-3, B-4, B-5, PE-1, PE-2, PE-3 (the soften-and-unwire
> option; wiring the backtest route stays open work in PLAN.md), P-1, P-2 — plus the
> three mediums the fixes bundle with them: B-6 (with B-5), B-9 (with B-4), and PE-5
> (with PE-1; `engine/verdict.py` is the one decision function, and
> `skipped_no_history` is deleted). Details in PLAN.md.

> **Fix status, second wave (2026-07-16).** The remaining medium and low findings are
> fixed, almost all with regression tests: PE-4, PE-6 through PE-13, B-7 through B-12,
> B-14 through B-21, H-1 (the comment-truth pass plus the small implementations), H-2,
> H-3, H-5, R-1 through R-3, P-3 through P-5, P-7, P-8, P-9, P-10's mechanical subset,
> and I-1 through I-3's cheap halves. Held, deliberately: B-13 (needs a per-group
> totals API), H-4 (adding ESLint/vitest is an infra decision), H-1's size-drift
> re-read and any real `keep_history` protection (features; the dead method is deleted
> and the gap is recorded in PLAN.md), P-6 (a test-pinned product decision on
> staleness), P-10's disarm-mid-run, shared scan lock, per-install KDF salt, and Plex
> TLS opt-out, and I-3's requests-app-aware warning. Details in PLAN.md's newest
> entry.

> **Fix status, third wave (2026-07-17).** The deliberately-held items are closed,
> each with regression tests: B-13 (season rows carry per-group condemned totals
> computed with the planner's own override function); H-4 (ESLint with the two
> react-hooks rules as errors, plus vitest component tests led by the reap
> confirmation's execute gate -- both CI gates); H-1's size-drift re-read (the executor
> re-reads live size before every send and keeps anything that grew materially or
> cannot be sized) and the `keep_history` protection (`TautulliClient.users` is back
> and wired: an active user with history recording off degrades the scan, closing the
> recorded gap); P-6 (a failed whitelist sync now coasts on stored members for at most
> 48 hours -- `WHITELIST_STALE_AFTER` -- then degrades; rule 2 updated to match);
> P-10's disarm-mid-run (the executor re-reads the arm switch before every item and
> aborts the rest of the run when it is off or unreadable), shared scan lock (one
> claim inside `run_scan`, so cron and manual scans cannot overlap; the grace-clock
> insert is conflict-tolerant), per-install KDF salt (`secret.salt` beside
> `secret.key`; fixed-salt and legacy derivations stay decrypt-only), and Plex TLS
> opt-out (`PlexServer.verify_tls`, threaded through linking, scans, the reap gateway
> and Leaving Soon, editable in Settings > Plex). Still open: I-3's requests-app-aware
> warning (needs instance knowledge inside `inspect()`).

**TLDR.** The safety architecture is genuinely sound where it is exercised: the gate and
signal engine's fail-closed math checks out, the execute route's interlock chain is
correct end to end, and auth/session handling is solid. The serious problems are almost
all of one species: **safeguards that are claimed but not implemented** (the action
journal is not crash-durable, the Plex trash purge runs without its promised count-delta
gate, the 30-day rolling caps are enforced nowhere, the backtest that operator copy tells
users to run is unreachable), plus **four fail-open holes on the protection side**
(tvdb-only keep tags never protect TV, a vanished keep tag silently wipes the whitelist,
a failed history sync scores on stale evidence without degrading, fileless seasons eat
keep-last slots).

---

## 1. Policy engine

The core invariants hold and are well tested: unsigned signals with a fixed denominator
(Unknown can only lower score and coverage), strictly subtractive keeps, no CONDEMN
constructor in the gate lane, round-once-decide-on-stored-ints, byte-stable integer-only
policy hashing with the correct post-score exclusion set, and an exemplary fail-closed
identity resolver. The findings are at the edges and seams.

**PE-1. Simulator disagrees with the production verdict on blocked and overridden rows.**
`high · bug` — `src/reaper/api/routes.py:775-799`, `src/reaper/services/snapshot.py:811`
`_verdict` returns "abstain" for any row with a blocked protection *before* it reads
score or coverage, and forces "condemn" for a hand-reap override. The simulate route
special-cases only `verdict == "protect"` and re-decides everything else on
`coverage_bp`/`score` alone. A row that abstained because a protection could not be
checked (score 85, coverage fine) is counted as condemned, and even reported under
"newly condemned" at thresholds identical to the saved policy; a hand-reaped row below a
draft threshold shows as "no longer condemned" though the scan keeps condemning it. This
is exactly the "plausible wrong answer" the route's docstring promises to refuse, and it
drives the operator's threshold choice.
**Fix:** Skip re-deciding rows whose stored explanation has non-empty
`protections_unknown` (or persist a blocked flag on Candidate) and keep override rows at
their stored verdict. Extend `tests/test_verdict_agreement.py` with blocked and override
rows.

**PE-2. Operator-authored condition values are never type-checked against the field, and
a mismatch crashes every scan.** `high · bug` — `src/reaper/engine/fields.py:397` (also
`:322`), `src/reaper/api/schemas.py:330`
`Condition.validate_for` checks lane and op only; `value: int | str | bool` at both the
API and model layer accepts a JSON string on a numeric field (Pydantic's smart union
keeps `"500"` a str). The policy saves and hashes cleanly; the next scan hits
`_num("500")`, raises ValueError inside `evaluate_all`/`score()`, and the whole scan
aborts with a traceback instead of a 422. Reproduced for protect conditions and boolean
condemn rules. Fail-closed (nothing deletes) but a legal-looking API payload bricks the
product's core function. Companion gap: `CONTAINS`/`IN` with a non-str value silently
never match instead of being rejected.
**Fix:** Validate value type against `FieldSpec.type` in `validate_for` (int for
DAYS/BYTES/COUNT/RATING_TENTHS, bool for BOOL, str for TEXT). Belt-and-suspenders: make
`fields.evaluate` catch ValueError and return a blocked `ConditionResult` so a bad stored
rule degrades per item instead of killing the scan.

**PE-3. The backtest and calibration subsystem is finished, tested, referenced in
operator copy, and unreachable.** `high · production-readiness` —
`src/reaper/engine/backtest.py`, `src/reaper/engine/calibration.py`,
`src/reaper/api/schemas.py:475`, `src/reaper/db/models.py:242`
No route, CLI, or frontend surface calls `backtest.run` or `calibration.derive`.
`BacktestOut` is imported by no router. Meanwhile `policy.py:549` warns "Run a backtest
before arming this", the size-rule warning says "Backtest this before arming it",
`backtest.py:194` declares "No profile may be armed while this is negative", and
`CheckConstraint("backtest_passed = 1")` can never be satisfied, so the AutonomyGrant
flow is dead on arrival. Operators are told to do something the product cannot do.
**Fix:** Wire `POST /api/policy/backtest` (build gates via `scan_runner.build_gates`,
derive the prior via `calibration.derive` over the scorer's population) plus minimal UI;
or soften every operator-facing reference and remove the dead schema until it ships, and
correct docs/PLAN.md.

**PE-4. `expected_regret_rate` still crashes despite the comment saying the fallback
prevents it.** `medium · bug` — `src/reaper/engine/backtest.py:160-172`
The guard is `prior_is_derived` (= `prior.calibrated`), but `Bucket.calibrated` is True
for an *empty* bucket while `Bucket.rate` is None for it, so one condemned item whose
dormancy lands in an empty bucket makes `rate_for` raise `NotCalibratedError`, taking
down `lift`, `beats_random`, and `summary()`. Reproduced.
**Fix:** Catch `NotCalibratedError` per item and fall back to `rewatch_prior` for that
item (reporting the mixed provenance), or strengthen the predicate to "every condemned
dormancy lands in a bucket with a rate".

**PE-5. The condemn decision exists in three places, and the agreement test pins a
transcription rather than the real code.** `medium · refactor` —
`src/reaper/services/snapshot.py:811`, `src/reaper/engine/backtest.py:428-439`,
`src/reaper/api/routes.py:787`, `tests/test_verdict_agreement.py:35`
`_verdict` is a pure function (Evaluation + ints + PolicyBody) yet backtest re-implements
it inline behind a comment claiming parity, and simulate re-implements it partially
(PE-1). The agreement test compares `_verdict` against its own hand-copied
`_simulator_verdict`, so a route-side regression (e.g. `>` for `>=` at the threshold)
passes the suite; no route-level test exercises `condemn_at` equal to a stored score.
Also `BacktestResult.skipped_no_history` is never incremented but is summed into the
summary line.
**Fix:** Move the decision into the engine (e.g. `engine/verdict.py`), import it from all
three call sites, make the agreement test call the real functions, and add a route-level
simulate test at an exact-threshold boundary. Delete or wire `skipped_no_history`.

**PE-6. `calibration.derive` is structurally wrong for TV.** `medium · bug (latent)` —
`src/reaper/engine/calibration.py:190-212`
It joins history on `rating_key` filtered by `media_type`, but TV history rows are
per-episode (`media_type='episode'`, episode keys) while the population passes show
keys. `backtest.py:269` documents and solves exactly this with `grandparent_rating_key`.
Latent only because derive is unwired (PE-3); the moment TV calibration is wired, every
show reads never-rewatched and the prior claims a ~0% baseline, inverting every lift
number.
**Fix:** Parameterize the column/filter pair like backtest `_plays`
(`grandparent_rating_key` + `'episode'` for TV) and add a TV calibration test before
wiring.

**PE-7. `Op.IN` is whitespace- and case-sensitive, and IN/EQ can never match
multi-valued fields.** `medium · bug` — `src/reaper/engine/fields.py:392`
`str(value) in target.split(",")` means `genre IN "Anime, Documentary"` fails on the
space, `"horror"` fails on case, and since `genres`/`on_curated_list` facts are
comma-joined strings ("Horror, Comedy"), a multi-genre title can never equal any single
element, so IN and EQ on those fields silently never fire. In the protect lane that is a
protection the operator believes exists doing nothing (rendered green "checked", not
amber).
**Fix:** Split with `strip()` and casefold both sides for IN (casefold EQ for TEXT); for
multi-valued fields evaluate against the fact's own comma-split elements, or drop IN/EQ
from `genre`/`on_curated_list` and keep CONTAINS.

**PE-8. Release age rounds in the condemn direction.** `low · bug` —
`src/reaper/services/snapshot.py:1090-1102`
`date(year, 1, 1)` overstates age by up to ~364 days on a condemn-lane field, so
`release_age >= N` custom rules over-match. The repo's own principle is to resolve
ambiguity toward keeping.
**Fix:** Round to `date(year, 12, 31)` (understates age, fail-safe), or use Radarr's
actual release-date fields already present in the payload.

**PE-9. A disabled popularity gate still supplies the popularity window.** `low · bug` —
`src/reaper/services/snapshot.py:1008-1012`, `src/reaper/engine/backtest.py:387-390`
`_popularity_window` reads `window_days` from the SERVER_POPULARITY setting without
checking `enabled`, so a disabled gate's stale window (say 30 days) silently drives the
`distinct_watchers` fact and the FEW_WATCHERS signal, increasing pressure.
**Fix:** Filter on `g.enabled` in both places, falling back to 365.

**PE-10. Dead engine surface, including a same-named duplicate of a safety class.**
`low · refactor` — `src/reaper/engine/custom_gate.py:24`, `src/reaper/engine/gates.py:186`
`engine/custom_gate.py` is imported nowhere; its `CustomProtectGate` (RuleSet-based)
duplicates the name of the live single-condition gate in `fields.py:447` with different
semantics. Also unused: `Facts.unknowns()` and
`observation.describe/value_or/map_known/is_unknown`.
**Fix:** Delete `custom_gate.py`; prune or test the unused observation helpers.

**PE-11. Boolean and graded custom rules treat `Absent` asymmetrically in coverage.**
`low · improvement` — `src/reaper/engine/signals.py:271-300`
A boolean rule reading Absent counts as evaluated (real evidence, no match); a graded
rule reading Absent counts as unevaluated, dragging coverage down. Both fail safe, but a
TV policy with a graded rule on a v1-Absent field (e.g. `release_age` on seasons) can
silently push every season under the coverage floor, and the backtest comment calling
such rules "inert" is inaccurate (they dilute score and coverage).
**Fix:** Treat Absent as evaluated-with-zero-pressure in the graded path to match the
boolean path, and reword the backtest comment.

**PE-12. Tier-1 identity matching does not cross-check id kinds.** `low · improvement` —
`src/reaper/engine/identity.py:538`
The first id kind that resolves uniquely binds; a second kind resolving to a *different*
Plex row is never consulted, so a mis-tagged tmdb id wins silently unless the title+year
backstop happens to fire. The contradiction veto already exists across tiers.
**Fix:** Extend the contradiction veto across kinds within tier 1: two kinds disagreeing
on the rating key means abstain.

**PE-13. Per-item policy config reconstruction.** `low · efficiency` —
`src/reaper/services/snapshot.py:742-743`, `src/reaper/engine/backtest.py:431-432`
`policy.custom_signal_configs()` and `keep_configs()` are rebuilt for every item in both
judge loops. Hoist them out of the loops.

---

## 2. Bugs

**B-1. TV protection lists are matched by IMDb id only, so tvdb-keyed keep tags never
protect seasons.** `critical · fails open` — `src/reaper/services/season_scan.py:889`,
`src/reaper/services/lists.py:261-271`, `src/reaper/services/snapshot.py:215`
`membership_index.lookup(imdb_id=series.get("imdbId") or None)` ignores the tvdb id even
though `ArrTagRule` stores TV rows with `tvdb_id` and `lookup` accepts it. A show without
an imdbId in Sonarr (the codebase itself calls this common) that the operator tagged
`reaper-keep` gets `whitelisted=False`; the gate never fires; its seasons are condemnable
and executable. The movie path passes two ids; the season path passes one. An
explicitly-set protection failing open on the deletion path.
**Fix:** `membership_index.lookup(imdb_id=..., tvdb_id=tvdb_id)` (tvdb_id is already
computed at line 850); add a season-path test whitelisting via a tvdb-only row.

**B-2. Keep-last-N slots are consumed by seasons with no files.** `high · bug` —
`src/reaper/clients/sonarr_stats.py:101`, `src/reaper/services/season_pruning.py:231`,
`src/reaper/services/season_scan.py:840`
`rank_seasons` ranks every non-special season regardless of `has_content`, and
protection is `rank <= keep_last`. Reproduced by script: seasons 1-5 on disk plus an
announced fileless season 6, `keep_last=2` protects only season 5 among real seasons and
deletes season 4. This re-introduces, through empty seasons, the exact rank-slot-shift
bug the module documents for specials. Any continuing show with an
announced-but-undownloaded next season hits it.
**Fix:** Rank over content-bearing seasons only (`[s for s in seasons if s.has_content]`)
in `plan_series_prune`, align the `season_rank` scoring fact, add a regression test.

**B-3. A failed watch-history sync only logs a warning; the scan then scores on the
stale mirror and stays executable.** `high · fails open` —
`src/reaper/services/scan_runner.py:332-336`, `src/reaper/services/executor.py:702`
Dormancy and watcher counts come from the local mirror; on `IntegrationError` the sync
failure goes to `log.warning` and is *not* appended to `pre_scan_degradations`, unlike
Plex and whitelist failures ten lines later. A play that landed after the last successful
sync is invisible: the streaming veto covers only right-now, and
`_watched_since_approval` checks plays at/after approval only. So an item watched during
the stale window can be condemned, approved, and deleted. Three independent reviews
converged on this. Rule 2 violation: the primary condemning evidence source fails open.
**Fix:** Append a degradation reason on sync failure (degraded snapshots already refuse
planning at `planner.py:252`); optionally also degrade when the mirror's newest event is
older than a bound.

**B-4. The action journal is not crash-durable: every SENT/VERIFIED mark only flushes
inside one run-long transaction.** `high · bug` —
`src/reaper/services/executor.py:1086-1095`, `src/reaper/api/runs.py:303`,
`src/reaper/db/models.py:508`
The single commit happens after the whole run returns. Kill the process after item 6 of
10 has deleted: the transaction rolls back, the run reverts to PLANNED with all steps
PENDING, and the operator cannot tell from Reaper that six files are gone. The "durable
audit record of what was in flight" claimed at `executor.py:636`, `planner.py:5`, and
`models.py:508` (StepState: "a step found still SENT at startup was in flight") does not
exist; the "a run executes once" guard is voided (re-execution converges only by accident
on the canary's 404). Side effect: the open write transaction holds the SQLite writer
lock for the entire multi-minute run. Three independent reviews converged on this.
**Fix:** Commit the PLANNED→EXECUTING flip before the first send and commit after every
step-state change (or journal on a dedicated per-step-committed connection). Pair with
B-9's atomic claim.

**B-5. `empty_trash` runs on every real run without the count-delta interlock three
docstrings claim, and the module docstring says it is not wired at all.**
`high · claimed safeguard missing` — `src/reaper/services/executor.py:1041` (and
`:59-67`), `src/reaper/clients/plex.py:646` (and `:473`)
`plex.py:646` promises the executor "only ever runs it after confirming the section
shrank by roughly what was deleted and no more"; `PlexClient.item_count` ("the input to
the trash interlock") has zero callers; `executor.py:67` still says trash is left to
Plex's own maintenance. Concrete hazard: Plex's nightly scan trashed 300 entries while a
mount flapped on the Plex host; the \*arr root-folder check (`_mount_is_up`) passes
because those are different mounts; Reaper reaps one movie and purges the whole section
trash, destroying 300 items' library records (watch states, collection membership).
**Fix:** Implement the promised delta gate using `item_count` (record pre-delete counts
per affected section; skip the purge when the section shrank by more than this run
deleted under it), or stop purging; in the same change make all three docstrings agree
with the code.

**B-6. Plex section mapping matches on a raw string prefix.** `medium · bug` —
`src/reaper/services/executor.py:1004`
`arr_path.startswith(location)` lets a section rooted at `/media/movies` claim files
under `/media/movies-4k/`, refreshing and (with B-5) trash-purging the wrong section.
**Fix:** `arr_path == location or arr_path.startswith(location.rstrip("/") + "/")`.

**B-7. A vanished keep tag or collection, or a malformed 200, silently wipes the
whitelist.** `medium · fails open` — `src/reaper/services/lists.py:239` (and
`:308-312`), `src/reaper/clients/arr.py:70`
Two paths, one failure: (a) a tag renamed or deleted in the \*arr, or a deleted "Never
Reap" collection, returns `[]` which sync treats as success, atomically replacing a
populated whitelist with nothing; `protection_sync_degradations` only inspects "error:"
outcomes, so the snapshot is not degraded and previously-protected titles become
executable on the same scan. (b) `tags()` masks a non-list 200 body (reverse-proxy error
page) as `[]`, feeding the same wipe. The Top 250 provider refuses a truncated payload
for exactly this reason; the whitelist providers have no analogous guard.
**Fix:** Distinguish container-missing/malformed from genuinely-empty: when the tag or
collection does not exist (or the body is not a list) and the stored list currently has
members, raise `IntegrationError` so the atomic swap keeps the previous membership and
the empty-membership degradation applies.

**B-8. `refresh_path` is a GET in plexapi, so the GuardedSession never gates it, while
the adjacent comment claims everything below requires arming plus a declared mutation.**
`medium · guard gap` — `src/reaper/clients/plex.py:552-555`
plexapi's `LibrarySection.update` issues `/library/sections/{key}/refresh` via GET. On a
server with Plex's "empty trash after every scan" library setting, an ungated refresh of
a path with missing files auto-purges those items: the GET-shaped-mutation class the
Tautulli client solves with a command allow-list, unsolved on the Plex twin.
**Fix:** Teach GuardedSession to treat known GET-shaped mutating paths (`/refresh`) as
mutations, or add the safety check inside `refresh_path`; correct the comment either way.

**B-9. Double-execute TOCTOU: the "a run executes once" check is a non-atomic
read-check-write.** `medium · race` — `src/reaper/services/executor.py:412`
Two concurrent POSTs to execute both read PLANNED and both run; the second re-executes
and clobbers the first's journal and final state (seasons re-verify as deleted-again,
double-counting bytes). Becomes strictly worse once B-4 introduces mid-run commits.
**Fix:** Claim atomically: `UPDATE reap_run SET state='executing' WHERE id=:id AND
state='planned'`, refuse on rowcount 0.

**B-10. Policy editor silently discards unsaved edits when toggling Movies/TV.**
`medium · frontend` — `frontend/src/components/PolicyEditor.tsx:1220`
The effect re-seeds the draft from the saved policy on every media-type change, both
directions, with no dirty guard on the toggle.
**Fix:** Gate the switch behind the existing confirm pattern when dirty, or hold one
draft per media type.

**B-11. The reap confirmation sheet can be closed mid-execution, losing the run report
and leaving the queue stale.** `medium · frontend` —
`frontend/src/components/ReapConfirm.tsx:68` (and `:126`)
Scrim click and Cancel are not gated on `exec.isPending`; unmounting skips `onSuccess`,
so after a real deletion the per-item checklist never shows and the review queue keeps
listing deleted items until something else refetches.
**Fix:** Ignore scrim/Cancel while pending; move invalidation to `onSettled` on a stable
queryClient; keep the sheet open until the report renders.

**B-12. Saving a policy with a newly added graded rule leaves the editor dirty
forever.** `medium · frontend` — `frontend/src/components/PolicyEditor.tsx:1308` (and
`:646-653`), `src/reaper/engine/policy.py:193-198`
`dirty` is a raw `JSON.stringify` comparison; the frontend inserts `floor` before
`saturate_at` while the backend serializes the reverse, so identical content compares
unequal after save. The Save button never returns to "Saved" and the staged banner can
fail to clear.
**Fix:** Re-seed the draft from the save response in `onSuccess`, or compare with a
key-order-insensitive canonicalization.

**B-13. Show cards understate what "Reap now" will plan.** `medium · count mismatch` —
`frontend/src/components/ReviewQueue.tsx:544`, `src/reaper/services/planner.py:313-324`
Card season-count and byte totals are computed over fetched pages only, while the planner
expands the group key over *all* condemned seasons in the snapshot. On large sorted lists
a show straddling unfetched pages shows "2 seasons · 12 GiB" and plans 6; only the typed
phrase reveals it. The count-must-match-confirmation rule applied to the card surface.
**Fix:** Return per-group condemned totals from the server for the card, or label card
numbers as partial until pagination is exhausted for that group.

**B-14. `_row_timestamp` treats `stopped=0` as epoch 0, the one fail-open corner of the
played-since-approval check.** `low · bug` — `src/reaper/services/executor.py:1164`
`0 is not None`, so `date` is never consulted and `0 >= approved_ts` is False: a row with
`stopped=0` but a post-approval `date` fails to spare.
**Fix:** `if not value: continue` so falsy falls through to `date`.

**B-15. Grace report filters spares by exact media_key only.** `low · display honesty` —
`src/reaper/services/grace.py:95`
Sparing a whole show leaves its condemned seasons listed as "ready" in the grace view
although planner and executor (which use `effective_override`) will never touch them.
**Fix:** Filter with `whitelist.effective_override(...) != "spare"` like the planner does.

**B-16. Seerr pagination silently truncates when `pageInfo` is missing.**
`low · possible` — `src/reaper/clients/seerr.py:214`
`total or 0` plus `skip >= total` exits after one page of 100 on an envelope-shape
change; requester attribution and the fairness view quietly undercount.
**Fix:** Treat a non-empty page with `total == 0` as `IntegrationError`.

**B-17. Instance-create maps validation errors to 409.** `low · wrong status` —
`src/reaper/api/settings.py:272`
A blank api_key returns 409 "conflict" with a "required" message; `update_instance`
already splits `InstanceConflictError` (409) from bare `InstanceError` (422).
**Fix:** Mirror the update handler's split.

**B-18. A movie without a usable tmdbId is deleted first and only then found
unverifiable.** `low · ordering` — `src/reaper/services/executor.py:822`
`_exclusion_landed` returns False for `tmdb_id == 0` after `delete_movie` already ran;
when it is the canary, the run aborts having performed one irreversible delete it could
have refused up front.
**Fix:** Skip the item (fail closed) before `_mark_sent` when the pre-read shows no
tmdbId.

**B-19. Settings claims a Discord webhook is configured when it no longer decrypts.**
`low · state mismatch` — `src/reaper/services/app_settings.py:177-187`
`has_discord_webhook` checks presence; `get_discord_webhook` returns None on decrypt
failure (e.g. rotated `REAPER_SECRET_KEY`), so the UI says notifications are on while
every send is skipped and grace warnings never post.
**Fix:** Attempt decryption in the has-check; ideally surface a "needs re-entry" state.

**B-20. Frontend small-bug cluster.** `low` — six verified items, each with its own
one-line fix:
- `PolicyEditor.tsx:1649`: keep-rule field list excludes fields whose matching built-in
  gate merely *exists*, even disabled; filter `draft.gates` on `enabled`.
- `ReapPlan.tsx:59` and `ReapConfirm.tsx:88`: `sim-stale` CSS class does not exist;
  aborted-run notices render unstyled. Rename to `sim-info` or add the rule.
- `QuantityInput.tsx:15`: size units use binary factors (1024³) labeled "GB" while
  coercion, presets, and descriptions use decimal 1e9, so the same rule shows two numbers
  ("465.66 GB" for a 500 GB cap). Pick one convention.
- `ReapPlan.tsx:133`: `item_count` labeled "steps"; a 4-season plan says "4 steps" above
  12 step rows. Say "items".
- `ReviewQueue.tsx:1053`: "Select all" selects the rendered window while its tooltip says
  "every card loaded". Align one to the other.
- `PolicyEditor.tsx:1349`: applying a preset before the profile query resolves stages
  only the policy half while the banner claims both; and `save.onSuccess` writes the
  response under the current mediaType key, briefly poisoning the other type's cache on a
  mid-flight toggle. Buffer preset caps until `pace` is non-null; key the cache write by
  `policy.body.media_type`.

**B-21. Fairness accounting inaccuracies.** `low · display accuracy` —
`src/reaper/services/fairness.py:120-123` (and `:249-258`)
`unmatched_requests` mixes units (per-group vs per-request) and never counts no-added-at
abstentions its docstring claims; a season-level request is charged the whole series'
`sizeOnDisk` in the ranking column.
**Fix:** Count unjudgeable per request; pro-rate season requests from Sonarr's per-season
statistics.

---

## 3. Hacks and workarounds

The repo has zero TODO/FIXME/HACK markers and no skipped tests: discipline is genuinely
good. What exists instead is a cluster of comments claiming safeguards the code does not
implement (the repo's own rule 7), plus a few sanctioned-looking bypasses.

**H-1. Claimed-but-unimplemented safeguard comments.** `medium · family` — fix each by
implementing or correcting the comment in the same change:
- `executor.py:33` and `planner.py:101`: claim "per-item existence/**size** re-reads at
  delete time"; only existence is checked. A 2 GB approval upgraded to a 60 GB remux
  deletes 60 GB while caps and the typed phrase counted 2. Re-read live size and skip on
  large drift, or drop "size" from both claims.
- `runs.py:211`: dry-run docstring claims "each per-item veto" runs; the streaming veto,
  played-since check, and no-rating-key skip run only on real sends. Name exactly what a
  dry run proves.
- `planner.py:372`: "Skip it LOUDLY, recorded, not silently dropped" followed by a bare
  `continue`. Add the log line or reword.
- `models.py:508`: StepState SENT-at-startup durability claim (see B-4).
- `executor.py:795`: "executor and clients share one RuntimeSafety, so this cannot
  happen" is false; the route and `build_reap_gateway` read two snapshots. Pass the
  route's `safety` in, making the comment true.
- `tautulli.py:115`: `users()`'s docstring declares the keep_history abstain protection
  mandatory; `users()` and `keep_history` have zero callers anywhere. A household member
  with Tautulli history recording off makes everything only they watch look never-played,
  and no gate abstains. Implement (read users during snapshot; degrade or force
  nobody-watched signals to abstain when an active user has keep_history off) or delete
  the method and record the gap in PLAN.md.
- `frontend/vite.config.ts`: describes the scan endpoint as SSE; the app polls
  `/api/scan/status`. Correct the comment.

**H-2. HTTP lives outside `clients/` in three places.** `medium · architecture bypass` —
`src/reaper/services/lists.py:149`, `src/reaper/services/imdb_dataset.py:118`,
`src/reaper/notify/discord.py:77`
Raw unguarded `httpx.AsyncClient`s, outside the guard, retry, and error-mapping layers,
contradicting "the only place HTTP lives". All are GET-or-Discord and contained by broad
catches, but they get zero retries where BaseClient reads get three.
**Fix:** Route the two GET fetchers through a thin read-only BaseClient; for Discord
either document it in CLAUDE.md as the sanctioned exception or allow-list the webhook
path via `non_media_mutations`.

**H-3. Services import from the API layer.** `low · layering` —
`src/reaper/services/scan_runner.py:310`
`from reaper.api.routes import active_policies` inside a function body is a
circular-import workaround inverting the layering.
**Fix:** Move `active_policies` into a service module (profiles or a policies service)
and have routes import it.

**H-4. `eslint-disable` comments with no ESLint.** `low · misleading marker` —
`frontend/src/components/ReapConfirm.tsx:59`, `frontend/package.json`
Three disable comments imply a linter pass that does not exist; the frontend has no
linter or test runner at all (only tsc), including on the component computing the
client-side execute gate.
**Fix:** Add ESLint with react-hooks (and ideally vitest for the confirm/gating
components) to the build gate, or remove the misleading comments.

**H-5. The pytest `live` marker claims "Deselected by default" with no deselection
mechanism.** `low · claim vs config` — `pyproject.toml:126`
The first live test added will run against a real instance in plain `uv run pytest` and
in CI.
**Fix:** Add `-m "not live"` to addopts or a conftest skip hook, or correct the
description.

---

## 4. Refactor opportunities

The big one, a shared verdict function, is PE-5. Beyond that:

**R-1. Dead-code sweep.** `low-medium` — `engine/custom_gate.py`, `clients/plex.py`
(`item_count`, `labels`), `clients/tautulli.py` (`users`), `clients/seerr.py` (`users`),
`api/schemas.py` (`BacktestOut`), `engine/backtest.py` (`skipped_no_history`)
Delete or wire, each with a test if wired. Untested safety-adjacent surface misleads
readers about what the product actually consults and drifts as upstream APIs change.

**R-2. Constrain the benign-label branch structurally.** `low · hardening` —
`src/reaper/clients/plex.py:203`
The leaving-soon write branch checks only the opt-in flag; "this branch can NEVER permit
a deletion" is enforced by call-site discipline alone. Any mutation issued inside a
`benign_label_write()` block passes with only the unarmed opt-in.
**Fix:** Require method PUT and a path matching the batch label-edit endpoints, failing
closed otherwise.

**R-3. Duplicate popularity-window and gate-construction glue.** `low` —
`src/reaper/services/snapshot.py:1008`, `src/reaper/engine/backtest.py:387`
`_popularity_window` exists in snapshot and inline in backtest (see PE-9); backtest `run`
accepts free-form gates relying on callers to remember `build_gates`. Centralize both in
the engine when extracting the verdict function.

---

## 5. Production readiness

**P-1. The 30-day rolling caps are validated, stored, shown in the UI, and enforced
nowhere.** `high · claimed safeguard missing` — `src/reaper/services/executor.py:316`,
`src/reaper/engine/policy.py:450-455`
The cap docstring delegates them to "the scheduler", which contains no deletion or cap
logic; policy.py promises "a 4 TB incident arithmetically unreachable: no sequence of
runs can exceed it". Five runs in a week sail past the 30-day budget without a warning.
**Fix:** In `execute()` for real runs, sum verified deletions from `ReapRun`/`ActionStep`
over the trailing 30 days and abort (not truncate) when the run would exceed either
rolling cap; until then correct the comment and UI copy.

**P-2. ~100 tests boot the real app lifespan non-hermetically: real `.env` credentials
get encrypted into test DBs and a real ~280 MB IMDb download fires per test.**
`high · test hermeticity` — `tests/test_app.py:36` and five sibling fixtures,
`tests/test_settings_api.py:46`
The repo defines the fix itself (`_hermetic`, stubbing `load_raw_env` and
`catch_up_on_startup`) but only one file uses it. Verified: the `client` fixtures in
test_app, test_api (×2), test_auth_gate, test_candidate_pagination,
test_candidate_filters, and test_review_auth run unstubbed, and `.env` with real service
keys exists at the repo root. Slow, flaky, network-dependent, and it copies live
credentials into throwaway DBs.
**Fix:** Promote `_hermetic` to an autouse conftest fixture; construct test Settings with
`_env_file=None`.

**P-3. The safety-arm and admin-password endpoints run Argon2 outside the throttle and
concurrency gate.** `medium · auth hardening` — `src/reaper/api/settings.py:547` (and
`:557`), `src/reaper/api/auth.py:277`
`PUT /api/settings/safety` verifies and `POST /api/settings/admin-password` hashes with
neither `argon2_gate.acquire()` nor any Throttle, unlike login: the exact CPU-exhaustion
vector the ratelimit module was written to bound. Related (low): the admin password can
be overwritten by any signed-in session with no current-password confirmation, so the arm
gate's "shared session" claim only holds against accidental clicks; and login/recovery
fields have no max-length bounds before hashing.
**Fix:** Route both through `argon2_gate` plus a per-account throttle; require the
current password (or recent re-auth) to change it; add `Field(max_length=...)` bounds.

**P-4. Client lifecycle leaks on every scan.** `medium · resource leak` —
`src/reaper/services/scan_runner.py:320-326`, `src/reaper/clients/plex.py:264`
The Seerr `httpx.AsyncClient` is built per scan and never entered into the exit stack;
`PlexClient` has no close at all (its GuardedSession/requests pool is reclaimed only by
GC) across scan, reap gateway, and leaving-soon; and if `active_policies`/`build_gates`
raises between client construction and stack entry, every constructed client leaks.
**Fix:** Enter seerr into the stack; give PlexClient a `close()` and context-manager
support and enter it too; construct clients inside the stack scope.

**P-5. Redirects carry the mutation-approved extension and credential headers
cross-origin.** `medium · likely · security` — `src/reaper/clients/base.py:147`
`follow_redirects=True` with httpx 0.28 strips only `Authorization` on origin change: a
compromised upstream or proxy can 301 any request to exfiltrate `X-Api-Key`, and a 307 on
an armed DELETE re-fires it at an attacker-chosen URL with approval intact.
**Fix:** `follow_redirects=False` for mutating requests; strip credential headers (or
refuse) on cross-origin redirects for reads.

**P-6. A failed whitelist sync with any prior members does not degrade, unprotecting the
newest additions.** `medium · deliberate tradeoff` —
`src/reaper/services/snapshot.py:1400`
Test-pinned behavior: stale membership "still protects", but a title keep-tagged *today*
is unprotected in tonight's scan if the sync fails, and the snapshot stays executable.
**Fix:** Bound the staleness (degrade when the last successful sync exceeds N hours) or
degrade on any failure, and align the engineering rule's wording with whichever is
chosen.

**P-7. The fairness report ignores the history horizon.** `medium · data honesty` —
`src/reaper/services/fairness.py:305-334`
A shallow Tautulli mirror makes every aged request read "never watched", inflating the
reclaimable numbers the leaderboard shows: the exact mass-wrong-verdict the scan path
stores the horizon to prevent. Read-only, but it exists to drive cleanup decisions.
**Fix:** Clamp the watch clock to the horizon like the scan path does, and surface the
horizon date in the report.

**P-8. Missing loading/error states on always-visible safety surfaces (rule 17
family).** `medium · frontend` —
- `frontend/src/App.tsx:169`: why-panel detail query handles neither loading nor error; a
  failed fetch leaves a reserved blank column with no message.
- `frontend/src/components/PolicyEditor.tsx:1257`: simulator shows "Working…" forever on
  failure; validator transport errors render as policy refusals and lock Save.
- `frontend/src/components/GracePanel.tsx:49`: `return null` on error reads as "nothing
  in grace".
- `frontend/src/components/ScanBar.tsx:53` plus two siblings: async onClick with no error
  handling; a failed scan start does nothing visible.
**Fix:** Explicit pending/error fallbacks; distinguish 422 from transport in
`invalidMessage`; convert fire-and-forget clicks to mutations.

**P-9. Supply-chain pinning gaps.** `low` — one pinning pass across five spots:
- `Dockerfile:4,20`: base images by floating tag (the repo's own rule 15 asks for
  digests).
- `.gitea/workflows/ci.yml:13`: CI actions by mutable major tag in the job that publishes
  the deletion-capable image; pin to commit SHAs.
- `.dockerignore:23`: root-anchored patterns (`__pycache__/`, `*.pyc`, `*.key`) miss
  nested paths, so local builds sweep host bytecode into images; use `**/` forms.
- `pyproject.toml`: python-dotenv imported directly (`config.py:31`) but undeclared (only
  transitive via pydantic-settings).
- `Dockerfile:56`: chowns `/app` to the runtime user when only `/data` needs it; leave
  code root-owned.

**P-10. Assorted low items.** `low` — each names its own fix; none blocks the others:
- `main.py:186`: unauthenticated `/api/health` returns armed-state, safety note, and
  exact version. Keep the open probe to `{"status":"ok"}`.
- `runs.py:265`: disarming mid-run does not halt an in-flight reap (safety snapshot read
  once). Documented; either implement a per-item re-read/cancel flag or state plainly in
  the UI that a started run is atomic.
- `scheduler.py:115`: scheduled scans bypass the UI's `scan_status` guard, so cron and
  manual scans can overlap and collide on `FirstFlagged` inserts. Share one scan lock;
  make the bulk recorder upsert-tolerant.
- `season_scan.py:511-535` and `fairness.py:282`: unchunked `IN :keys` lists can exceed
  SQLite's bound-variable limit on very large libraries; chunk at 500 like
  `imdb_dataset.lookup`.
- `imdb_dataset.py:169`: the 1.7M-row gzip parse runs on the event loop; move batch
  production to `asyncio.to_thread`.
- `plextv.py:246`: PIN poll ignores Retry-After on 429 (fixed 5s); carry the header onto
  the error and honor it.
- `plextv.py:324`: the per-service TLS opt-out has no Plex equivalent (`verify=True`
  hardcoded), so self-signed-HTTPS Plex cannot be linked at all. Fails closed; extend the
  opt-out with True default.
- `history_sync.py:73`: the one-day incremental overlap "guarantee" depends on unverified
  Tautulli date-boundary semantics; widen to two days and soften the comment, or verify
  and record the real semantics.
- `crypto.py:36`: fixed application-wide KDF salt and warn-only entropy floor; consider a
  per-install random salt stored beside `secret.key`, and document that the floor is
  advisory.

---

## 6. Improvements

**I-1. Operator-copy and design-token fixes.** `low` —
- `frontend/src/components/WhyPanel.tsx:210`: the verdict headline prints the raw enum,
  so operators see "ABSTAIN", banned jargon; map to the tab vocabulary (Would reap /
  Spared / Left alone).
- `frontend/src/index.css:2720`: `.select-tick.on` hardcodes white on `--accent`, which
  the file's own comment says fails WCAG AA in dark mode; use `--accent-ink`.
- Operator-visible backend strings render a double hyphen as an em dash (e.g.
  `observation.describe`: "could not check -- {reason}", several degrade reasons,
  requester findings). The no-em-dash copy rule's intent applies; reword with a colon or
  period where the string reaches the UI.

**I-2. `.env.example` honesty for REAPER_HOST/PORT.** `low` — `.env.example:29`,
`src/reaper/main.py:89`, Dockerfile CMD
Documented as server config; nothing binds to them (Dockerfile hardcodes 8420) and their
only consumer renders a `http://0.0.0.0:8420/recover` recovery link. Honor them in the
entrypoint, or annotate what they actually affect and default the link host to something
usable.

**I-3. Policy `inspect()` coverage.** `low` — `src/reaper/engine/policy.py:485`
`inspect` warns on rating-floor and threshold footguns but not on a very small popularity
`window_days` (legal at 1 day, which puts FEW_WATCHERS near full pressure library-wide)
or a `keep_last_scope="requested"` policy with no Seerr configured (the floor then
applies to everything, silently). Both are cheap warnings in the existing pattern.

---

## Verified clean (do not churn)

For the fixer agent's calibration, these were adversarially checked and held: the
transport guard's httpx core (non-GET refusal without armed+declared, exact-path
allow-list, redirect-off-allowlist fails closed) and plexapi funneling through
GuardedSession; mutation calls unretried (no double-delete on ambiguous timeout) with
reads retried on the right exception types; the execute route chain (authn → CSRF +
Sec-Fetch-Site middleware → armed 403 → server-recomputed content-bound phrase 409 →
executor re-checks → manifest hash → caps abort-not-truncate over the exact deletable set
→ canary-first ordering → live per-item vetoes, all fail-closed); session security
(hashed opaque tokens, `__Host-` cookies, absolute expiry, sign-out-everywhere on
password change/deactivation, throttled and timing-equalized login, atomic 0600
secret-file creation); grace-clock re-entry reset; None-vs-empty fail-closed at the route
and planner; degraded snapshots refusing to plan; aware-UTC datetimes end to end; the
frontend's CSRF coverage, server-owned confirmation phrase, and `Promise.allSettled` bulk
flows; lockfile-frozen installs, single Alembic baseline with `alembic check` in CI, no
secrets in the image, non-root container.

---

## Agent Rules

Direct constraints for the next coding agent, derived from what this review actually
found. Written as blockers, not suggestions. (These extend, and do not replace, the
engineering rules in CLAUDE.md.)

> **Adopted.** These now live in CLAUDE.md as standing engineering rules 22–39
> ("Blockers from the second review pass"), lightly updated where the fixes landed
> (rule 1 cites `engine/verdict.py`; rule 17's `item_count` example is now wired).
> CLAUDE.md is the maintained copy; this list is the review-time record.

1. **One decision function.** The condemn/abstain/protect decision lives in exactly one
   engine-level function. `snapshot`, `simulate`, and `backtest` must import it. Never
   write `score >= threshold` or `coverage_bp >= floor` inline outside it, and any
   agreement test must call the real functions, never a transcribed copy.
2. **Every re-decision surface handles every stored verdict state.** If you add or
   consume a Candidate verdict, enumerate all states (protect, abstain-blocked,
   abstain-by-score, condemn, overrides) at every consumer, and add the blocked/override
   cases to the simulator test in the same change.
3. **A comment naming a safeguard must cite its implementing function**, and you must
   verify that function exists and is called before merging. If you cannot cite it,
   change the comment in the same commit. This review found six safeguards that existed
   only as prose.
4. **Operator-facing copy may only reference features that are wired.** Before writing UI
   or warning text that names a mechanism (backtest, cap, interlock), confirm the route
   or UI path exists; a DB constraint or schema for an unwired feature is a blocker, not
   a placeholder.
5. **Journal and state-transition writes on the deletion path must be durably committed
   at each step.** Never rely on `flush()` inside a run-long transaction for anything
   described as an audit record, and every state-transition guard must be an atomic
   `UPDATE … WHERE state = :expected`.
6. **A protection container that cannot be found is an error, never an empty result.**
   When a tag, collection, or list fetch would replace stored members, distinguish
   container-missing or malformed-body from genuinely-empty; missing-with-existing-
   members must raise so the previous membership survives and the snapshot degrades.
7. **Failure of any evidence source degrades the snapshot.** Any `except` around a source
   read in the scan pipeline must append to `pre_scan_degradations` (or call
   `context.degrade`); a bare `log.warning` on a source failure is a review-blocker.
   Watch history is a source.
8. **Every identity or membership lookup passes every id the item carries.** When calling
   `membership_index.lookup` or any cross-system join, pass imdb+tmdb+tvdb together;
   adding a new id kind to storage requires grepping and updating every lookup call site
   in the same change.
9. **Rank, cap, and count computations run over the exact set acted on.** Filter first
   (content-bearing seasons, non-spared deletable items, fetched-vs-all groups), then
   rank or count; any number shown beside a destructive button must be derived from the
   same set the server will act on.
10. **Derived condemn-lane values round toward keeping.** When precision is reduced on
    any field that can add deletion pressure (dates from years, sizes, ages), choose the
    bound that produces less pressure.
11. **Typed condition values validate against the field's type at the boundary, and
    evaluation never raises out of a scan.** Rule evaluation errors degrade that item as
    blocked; a stored policy must not be able to crash `score()` or `evaluate_all`.
12. **All HTTP goes through `clients/`.** A raw `httpx`/`requests` usage outside
    `src/reaper/clients/` is a blocker unless CLAUDE.md names it as a sanctioned
    exception. GET-shaped mutating endpoints must be classified and gated by path in the
    guard, not assumed safe by method.
13. **Every constructed client has an owner that closes it.** A client constructed
    outside an exit stack (or without entering one in the same scope) is a leak; add the
    close path in the same diff as the construction.
14. **New `Facts` fields must be populated (or explicitly `Absent` with a comment) in
    every fact builder**: snapshot movies, season_scan, backtest, calibration. Grep all
    builders when adding a field; a field populated in one path silently changes scores
    and coverage in the others.
15. **Frontend gating and safety surfaces handle `isPending` and `error` explicitly.**
    `return null` on a failed query for an always-visible component is a blocker, and
    every async onClick is a mutation with a rendered error state.
16. **Tests that boot the app must be hermetic.** Use the shared autouse fixture that
    stubs env seeding and startup network; never let a test read the developer's `.env`
    or reach the network.
17. **Dead safety-adjacent code is deleted, not stockpiled.** A method "for when the
    interlock lands" (like `item_count`) must land with its interlock and tests, or not
    exist.
18. **Drafts and dirty checks compare canonical forms.** Never compare serialized state
    with raw `JSON.stringify` across frontend/backend boundaries; re-seed from the server
    response after a save.

---

## Suggested fix order

B-1, B-2, B-3 (fail-open protections, small diffs) → B-4/B-9 (journal durability) →
B-5/B-6 (trash purge) → P-1 (rolling caps) → PE-1/PE-5 (shared verdict + simulator) →
PE-2/PE-7 (condition validation) → H-1 (comment truth pass) → the rest by file.
