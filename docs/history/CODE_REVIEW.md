# The fifth review pass — archived

> **FROZEN 2026-07-26. Do not work from this file.**
>
> The whole-backend adversarial review at `dev @ d3c3839` (2026-07-24): two passes, 100
> findings. **All 100 are remediated** — see `CODE_REVIEW_PHASES.md` in this directory, whose
> ten phases are all recorded DONE with gates.
>
> **Its own preamble is wrong.** The text below still asserts that "all 53 of Part II's
> findings are still open on this tree," and that CLAUDE.md's numbered rules stop at 69.
> Neither was true after 2026-07-24. Ignore both claims.
>
> **The `# Agent Rules` section here is superseded.** Those rules became CLAUDE.md 88-119 and
> now live in `.claude/rules/`, where several were deliberately **reworded against what was
> actually built** rather than what this review proposed. Follow `.claude/rules/`, never this
> copy. Kept for the failure modes and the reasoning, which are still the best record of why
> those rules exist.

# Backend code review — `dev` @ `d3c3839`, 2026-07-24

> **This file holds two passes.** **Part I** is a second, independent whole-backend pass run
> after the first one was written. **Part II** is the first pass, preserved verbatim: all 53 of
> its findings are still open on this tree (spot-verified during this pass — `lists.py:239` is
> still `by_label.get(tag)` with no `.lower()`, and `plex.py:694` is still
> `server.library.section(section_title)`). Nothing here supersedes Part II; the two are additive
> and the **Agent Rules** at the end of the file cover both.
>
> **Scope.** The Python backend only (`src/reaper/**`, ~36.8k lines across 95 modules) plus
> `tests/` and the Alembic chain. The React SPA is being reviewed separately and is out of scope.
>
> **Method.** Eight reviewers each took a disjoint file group and read it in full, with the 53
> first-pass findings supplied up front as a do-not-rediscover list. Every candidate then went to
> an adversarial verifier whose job was to *refute* it: open the cited lines, grep the real call
> sites, check whether the claimed trigger can actually occur, and correct the line numbers. 45
> candidates were raised, **37 survived, 8 were refuted** and are listed at the end of Part I so
> they are not raised again. A ninth reviewer covered the test suite. The verifiers did not just
> vote — for B2-5 the verifier applied the proposed fix, watched 11 tests fail, and returned the
> corrected variant; that correction is in the finding. Where a verifier disagreed with the
> reviewer about the mechanism, severity, or fix, the disagreement is recorded inline as
> **Verifier's correction** and it governs.
>
> **Tally (Part I).** 37 code findings — **1 critical, 5 high, 8 medium, 23 low** — plus 10 test
> findings. Baseline at time of review: `ruff check`, `ruff format --check` and `mypy src/reaper`
> all clean; **2044 tests pass in 63s**; single Alembic head (`708192a3b4c5`).

---

## Read this first (for the agent doing the fixes)

Three things about this pass that change how you should work through it.

**1. B2-1 is the only critical, and it is live on existing installs right now.** It is not a
latent edge case: any database whose policy row was seeded before commit `a95ebd6` — which is
every tester DB created on the frozen `22777b2b5015` baseline — is currently running with its
"keep well-rated titles" protection silently doing nothing, and the scan does *not* degrade, so
nothing tells the operator. Reproduced directly during this review: load a stored body with no
`keep_rating_rules` key, it validates cleanly, `rating_rules()` returns `()`, and
`RatingFloorGate` answers `ABSTAIN — "No rating is set that would keep a title."` for every item
in the library. Fix this one first and ship it with the recovery flag, not silently.

**2. Six findings resolve toward deleting more than the operator asked for.** In severity order:
B2-1 (rating protection silently gone), B2-6 (keep-list lookup drops an id the item carries),
B2-3 (a TV protection that can never fire), B2-4 (`contains ""` matches the whole library),
B2-5 and B2-7 (an identity bind that overrules the listing actually holding the file). Everything
else either fails toward keeping the file, wedges a run, or is an honesty defect. If you are
triaging by the prime directive, that is your list.

**3. Some proposed fixes are booby-trapped, and the finding says so.** Do not apply a **Fix**
paragraph without reading the **Verifier's correction** above it. Three specific traps: B2-5's
obvious one-line fix breaks 11 identity tests (the cross-check must not originate binds); B2-10's
first-listed fix would mark a step `VERIFIED` whose verification explicitly failed; B2-8's fix
changes the behavior of *freshly* built plans, not just stale ones, so it needs operator copy and
a product decision in the same change.

**A repo-hygiene blocker that is not a code bug but blocks reviewing this codebase: CLAUDE.md's
numbered rules stop at 69, and the code cites rules 70–87.** Fifteen distinct rule numbers
(70–78, 80, 82–85, 87) are cited across 30+ comment sites in `src/` and `frontend/src`, and none
of them exist anywhere in the repo. Those rules were evidently written and then lost from the
file. Every citation to them is unverifiable, and an agent told to "follow rule 82" has nothing
to follow. This is I2-2 below; it wants fixing before the next review pass, not after.

---

## Part I — second pass

### Index

| ID | Severity | Location | Finding |
| --- | --- | --- | --- |
| B2-1 | critical | `engine/policy.py:393` | A policy saved before the multi-source rating change loses its "keep well-rated titles" protection entirely, silently, with no migration and no degradation |
| B2-2 | high | `clients/plex.py:567` | PlexClient.section_paths is the only Plex read that does not map failures to PlexError, so a live Plex fault escapes both call sites' `except PlexError` and aborts a reap mid-run |
| B2-3 | high | `engine/fields.py:455` | `quality` and `release_age` are offered on TV policies but are hardcoded Absent for every season, so a TV protection written on them can never keep anything |
| B2-4 | high | `engine/fields.py:555` | A blank or whitespace-only text value is accepted on both lanes: `contains ""` matches every item (full condemn weight library-wide) and `in ""` silently never protects |
| B2-5 | high | `engine/identity.py:1158` | The documented cross-tier contradiction veto never runs between the id tier and the file-basename tier, so an id bind can silently overrule the listing that provably holds the *arr's file |
| B2-6 | high | `services/snapshot.py:283` | The movie keep-list/curated-list lookup passes only Radarr's imdbId, never the Plex-matched imdb id the item also carries |
| B2-7 | medium | `engine/identity.py:1036` | resolve_show consults only tvdb, so a show's imdb id — present on both sides — never cross-checks the bind; a mis-tagged tvdb id binds a whole series to the wrong Plex show, where a movie in the same shape abstains |
| B2-8 | medium | `services/executor.py:761` | A run's recorded policy_hash is never checked at execute time, so a plan built under a looser policy still deletes after the operator tightens it |
| B2-9 | medium | `services/executor.py:742` | Hand spares are loaded once at run start, so sparing an item while the reap is in flight does not stop its deletion |
| B2-10 | medium | `services/executor.py:977` | The rolling 30-day budget counts only VERIFIED steps, but a movie whose file is confirmed gone with an unverifiable exclusion is marked FAILED, so real deletions are never charged against the monthly cap |
| B2-11 | medium | `services/season_pruning.py:150` | The mid-binge guard anchors on the highest season NUMBER a viewer touched, not the season they are actually watching, so a re-watcher or out-of-order viewer gets no protection at all |
| B2-12 | medium | `services/snapshot.py:649` | A degraded IMDb dataset is turned into `Absent` ratings for every item, which silently withdraws the rating protection instead of blocking it |
| PR2-1 | medium | `clients/plex.py:554` | execute() and _send_for_real have no catch-all, so a non-mapped exception after a file is deleted leaves the run stuck in EXECUTING with no report and the terminal step left SENT |
| S2-1 | medium | `logging.py:183` | _RingHandler.emit re-appends the RAW, unredacted log message whenever exc_info is set, defeating its own query-string credential scrubbing |
| B2-13 | low | `api/routes.py:502` | _primary_reason indexes into stored-explanation entries without the isinstance/key guards its siblings use, so one malformed row 500s the entire review-queue page |
| B2-14 | low | `api/settings.py:685` | A transient Plex probe failure during linking is mapped to 400, which aborts the browser's poll loop and throws away the still-valid PIN the service deliberately kept |
| B2-15 | low | `engine/gates.py:579` | `OthersWatchingGate` can never PROTECT: `others_watching` is Absent in every fact builder, so the count is always 0 against a floor of at least 1 |
| B2-16 | low | `ratings.py:263` | from_radarr parses the vote count with a bare int() while the sibling score parse is guarded, so a non-integer votes field raises out of the scan instead of degrading that one rating |
| B2-17 | low | `services/executor.py:1005` | The reap progress bar's total switches from the confirmed item count to the raw plan-step count on the first progress tick |
| B2-18 | low | `services/fairness.py:899` | `_fetch_available` fans out across live Seerr portals with a bare asyncio.gather, so the first portal failure leaves sibling reads running against clients the route is about to close |
| B2-19 | low | `services/fairness.py:532` | A season-scoped request whose seasons are not in the scan inflates the board's request count but appears in neither the person drawer's list nor the not-in-scan panel |
| B2-20 | low | `services/leaving_soon.py:401` | The Leaving Soon announced-set is a read-modify-write across minutes of network I/O, so two overlapping passes double-announce and one loses the other's entries |
| B2-21 | low | `services/restore.py:531` | A restore swap interrupted between the two move loops discards the backup's secret.key/secret.salt on the next boot and prints "current data kept" when the data was already replaced |
| B2-22 | low | `services/scan_runner.py:402` | `_flag` returns a definite `True` for an unparseable Tautulli value, so an unreadable Keep-History setting reads as "recording" instead of degrading |
| B2-23 | low | `services/season_scan.py:1422` | The keep-rule conflict detector reads a season Plex never resolved as "0 people watched it", producing spurious abstains and a false operator-facing claim |
| B2-24 | low | `services/season_scan.py:1124` | The stale-library-map guard and the unmatched-show log consult imdb candidates that the show resolver never uses, suppressing the warning for a genuinely wrong mapping |
| B2-25 | low | `services/snapshot.py:1935` | Changing the keep-tag match (any/all), or removing an *arr instance, leaves the old protection-list slug enabled and still protecting forever |
| B2-26 | low | `services/snapshot.py:913` | A degraded snapshot is un-plannable but still starts grace clocks and drives the Leaving Soon shelf / Discord announcement |
| P2-1 | low | `api/routes.py:1450` | /api/policy/simulate re-runs the whole scoring engine synchronously over every candidate inside the request handler, freezing the entire API while the operator drags a policy slider |
| P2-2 | low | `api/routes.py:750` | Every candidate row on the review queue's hot path parses the same explanation JSON three or four times |
| PR2-2 | low | `services/app_settings.py:339` | An API key can be rotated but never removed: clear_api_key exists with no route and no caller, so the header credential lane cannot be switched off |
| PR2-3 | low | `services/instances.py:566` | `api_path_prefix` is a column no code path ever writes, while the connection-test docstring claims Reaper version-gates the API path off the status probe |
| PR2-4 | low | `services/scan_runner.py:349` | `build_reap_gateway` constructs httpx clients into a plain list, so a raise part-way through leaks every client already built |
| S2-2 | low | `clients/plextv.py:248` | wait_for_pin sleeps for an uncapped, server-supplied Retry-After, so a hostile or misbehaving plex.tv response hangs `reaper-admin link-plex` far past its 5-minute deadline |
| I2-1 | low | `engine/gates.py:469` | MinDormancyGate's docstring tells the operator its dormancy curve is derived from their own watch history at calibration time, but engine.calibration.derive has no production caller anywhere |
| I2-2 | low | `secrets.py:69` | 37 comments across the backend and frontend cite engineering rules 70-87, but CLAUDE.md's rule list ends at 69 — every one of those citations is unverifiable |
| I2-3 | low | `services/executor.py:2014` | _row_timestamp's docstring says an unreadable timestamp reads as "no evidence of a play", but the caller treats it as a play and spares the item |

## 1. Bugs

### B2-1 · A policy saved before the multi-source rating change loses its "keep well-rated titles" protection entirely, silently, with no migration and no degradation — **critical**

`src/reaper/engine/policy.py:393`; also `src/reaper/engine/policy.py:101`, `src/reaper/engine/gates.py:307`, `src/reaper/services/scan_runner.py:127`, `src/reaper/services/profiles.py:203`

**Failure mode.** Before commit a95ebd6 the rating bar lived on the RATING_FLOOR gate row as `threshold` (tenths)
+ `secondary` (min votes). That commit moved it to `PolicyBody.keep_rating_rules` and deleted
the gate's validation, but shipped no backfill and did not bump SCHEMA_VERSION. A stored body
written before that commit — e.g.
`{"gate":"rating_floor","enabled":true,"threshold":75,"secondary":1000}` with no
`keep_rating_rules` key — validates cleanly, `keep_rating_rules` defaults to `()`,
`PolicyBody.rating_rules()` returns `()`, and `build_gates` (scan_runner.py:127) constructs
`RatingFloorGate(rules=())`, which returns ABSTAIN "No rating is set that would keep a title."
for every item. The operator's IMDb 7.5-from-1,000-votes keep is gone. Because the body still
validates, `ActivePolicy.repaired` is False, so the scan does NOT degrade and the run is
executable. Reproduced on this tree: a movie with an 8.8 IMDb rating from 500,000 votes,
protected under the stored policy, evaluates to `RATING_FLOOR -> ABSTAIN | No rating is set that
would keep a title.` and is condemnable on score alone. The only signal to the operator is a
`severity="warn"` string in the policy editor (policy.py:798) they must open the page to see.

**Verifier's correction.** Mechanism is right; two corrections the fixing agent needs. (1) BLAST RADIUS IS WIDER THAN 'a
hand-saved policy': `profiles._ensure_active_policy_row` (services/profiles.py:228-253) persists
DEFAULT_MOVIE_POLICY as a real row the first time a profile is saved, and `active_policy_row`
returns the newest row for the media type. So ANY install whose policy row was seeded before
a95ebd6 (2026-07-17) — i.e. every tester DB created on the frozen 20260714 baseline — is running
a body with no `keep_rating_rules`, whether or not the operator ever opened the editor. (2) THE
SCHEMA_VERSION HALF OF THE PROPOSED FIX DOES NOT DISCRIMINATE: `SCHEMA_VERSION` was already 2
before a95ebd6 (verified in `git show a95ebd6^:src/reaper/engine/policy.py`, line 49), so
affected bodies already carry `schema_version: 2` and a bump to 3 cannot retroactively identify
them. The shim must key exactly on what the reviewer describes — raw key `keep_rating_rules`
ABSENT (not `[]`, rule 1) plus a `rating_floor` gate entry whose `threshold >= 1` — and must NOT
be conditioned on schema_version. Bumping SCHEMA_VERSION is still fine for future-proofing (the
field is a plain `int` with `le=SCHEMA_VERSION`, so raising the ceiling keeps old bodies valid).
(3) The recovery flag is the right shape: mirror `ActivePolicy.rescaled`/rule 65 so `repaired`
is True and scan_runner.py:562 degrades the snapshot; do not silently synthesize without the
flag.

**Fix.** Add a `model_validator(mode="before")` (or an explicit loader shim in
`services.profiles.active_policy`) that, when the raw stored dict has NO `keep_rating_rules` key
at all AND carries a `rating_floor` gate with `threshold >= 1`, synthesizes
`keep_rating_rules=(RatingRuleSpec(source=RatingSource.IMDB, floor=threshold,
min_votes=secondary),)`. Distinguish key-absent from an explicit `[]` (rule 1): an operator who
deliberately cleared their bars must keep an empty set. Flag the result the way `rebalance` is
flagged — set an `ActivePolicy` recovery flag so `repaired` is True, the scan degrades, and the
editor opens on it as an unsaved draft. Bump `SCHEMA_VERSION` and have the migrated body write
back at schema_version 3 so the shim can eventually retire.

### B2-2 · PlexClient.section_paths is the only Plex read that does not map failures to PlexError, so a live Plex fault escapes both call sites' `except PlexError` and aborts a reap mid-run — **high**

`src/reaper/clients/plex.py:567`; also `src/reaper/services/executor.py:1756`, `src/reaper/services/executor.py:1768`, `src/reaper/api/settings.py:557`

_Violates CLAUDE.md rule 9, 26._

**Failure mode.** Plex answers the connect handshake (`GET /` succeeds, so `_connect()` does NOT raise PlexError)
but then fails `GET /library` or `GET /library/sections` — a revoked token mid-run (plexapi
raises `Unauthorized`), a Plex restart between the two calls, or a reverse proxy 502 on that one
path. `section_paths()` re-raises the raw `plexapi.exceptions.*` /
`requests.exceptions.ConnectionError` because it has no try/except. Executor path:
`_capture_section_counts` (executor.py:1730) swallows the first failure with a bare `except
Exception` and returns WITHOUT populating plexapi's `_loadSections` cache, so the failure
recurs. Then a movie is deleted for real, `gone=True`, and `_best_effort_refresh`
(executor.py:1756) calls `section_paths()` again; its `except PlexError` (executor.py:1768) does
not match, `_send_for_real`'s handlers (`except IntegrationError / SafetyViolationError /
ExecutionError`, executor.py:1375-1387) do not match, and the run body's `except ExecutionError`
/ `except asyncio.CancelledError` (executor.py:857/863) do not match either. Result: the file is
already deleted, its ActionStep stays `SENT` and is never marked `VERIFIED`, `run.state` is
never moved off `EXECUTING`, every remaining approved deletion in the run is never attempted,
and the rolling 30-day budget (`_check_rolling_caps`, which counts verified deletions)
permanently under-counts that delete. Settings path: `api/settings.py:557` catches only
`PlexError`, so the same fault turns the Root Folders screen into an unhandled 500 instead of
the documented "best-effort — folders still come back, just with no suggestions".

**Verifier's correction.** Line numbers are correct on this tree (plex.py:567, executor.py:1730/1756/1768,
api/settings.py:556-558). One refinement to the reviewer's mechanism: `_capture_section_counts`
swallowing the first failure matters not because it 'fails to populate the cache' as a side
effect, but because plexapi's `cached_data_property` only caches on SUCCESS — so any persisting
Plex fault is guaranteed to recur at refresh time. Severity high is defensible for the executor
consequence (a real deletion with an unrecorded VERIFIED mark plus a stuck EXECUTING run), but
the practical trigger is narrow: it needs Plex to answer `/` and `/status/sessions`
(active_streams is re-polled fail-closed before every delete) yet fail `/library` or
`/library/sections`. A revoked token would break active_streams first and surface as PlexError.
The reliably-reachable consequence is the settings-route 500. Fix is exactly as proposed — wrap
the `to_thread` in `except Exception -> PlexError`. Worth also confirming in the same change
that `refresh_path` (called inside the same try at executor.py:1760) maps its failures to
PlexError.

**Fix.** Wrap the body of `section_paths` exactly like its siblings: `try: return await
asyncio.to_thread(read)` / `except Exception as exc: raise PlexError(f"Could not read Plex
section paths: {exc}") from exc`. Add a test that a raising `server.library.sections()` surfaces
as `PlexError`.

### B2-3 · `quality` and `release_age` are offered on TV policies but are hardcoded Absent for every season, so a TV protection written on them can never keep anything — **high**

`src/reaper/engine/fields.py:455`; also `src/reaper/engine/fields.py:441`, `src/reaper/services/season_scan.py:599`, `src/reaper/services/season_scan.py:600`, `src/reaper/api/routes.py:1625`

_Violates CLAUDE.md rule 25, 7/24 (FieldSpec.media_types docstring claims the filter is applied)._

**Failure mode.** `season_scan.build_season_facts` sets `release_age_days=Absent(source="sonarr")` and
`quality=Absent(source="sonarr")` unconditionally for every season. But neither FieldSpec
declares `media_types=("movie",)`, so `vocabulary(Lane.PROTECT, "tv")` returns both (verified:
`['days_unwatched', 'size_bytes', … 'release_age', 'quality', 'show_ended']`) and `GET
/api/vocabulary?lane=protect&media_type=tv` offers them in the TV policy editor. An operator
writes the protection "Keep it when File quality contains 2160p" on their TV policy to save
their 4K seasons. `fields.evaluate` reads Absent -> `ConditionResult(matched=False,
blocked=False)`, so `CustomProtectGate` returns ABSTAIN and the season is reported under
"protections CHECKED that did not fire" — green, indistinguishable from a real check that
legitimately found nothing. Every 4K season the operator believed protected is condemnable on
score. Same for a `release_age` protection on TV.

**Verifier's correction.** Severity and fix are right, but the reviewer missed the condemn-lane half, which is not merely
'safe': removal weights must total exactly 100 (`PolicyBody._weights_total_one_hundred`) and
`signals.score` divides by that fixed denominator, so a TV custom-condemn rule written on
`quality`/`release_age` permanently removes its weight from every TV item's achievable score —
the lane is depressed library-wide by that many points rather than the rule simply never firing.
That is conservative (keeps files) but it silently distorts every TV score, so BOTH lanes must
lose the field, i.e. `media_types=("movie",)` on both FieldSpecs, not a protect-only filter.
Also note for the 'tell the operator' half of the fix: existing stored TV rules live in three
places, not one — `protect_conditions`, `custom_condemn` (boolean AND graded), and
`graded_keeps` — and once `media_types` is narrowed,
`ConditionSpec`/`BooleanCondemnSpec`/`GradedCondemnSpec` validators call
`Condition.validate_for`, which only checks lane/op/type, not media type, so the stored rules
will keep validating and will simply vanish from the editor unless a surfacing path is added in
the same change.

**Fix.** Add `media_types=("movie",)` to the `release_age` and `quality` FieldSpecs, matching how
`season_rank`/`show_ended` are restricted to `("tv",)`. Then grep every stored TV policy on load
for `protect_conditions`/`custom_condemn`/`graded_keeps` naming those fields and surface them
the way `ActivePolicy.repaired` is surfaced, so an operator who already wrote one is told it
never fired rather than having it silently disappear from the editor.

### B2-4 · A blank or whitespace-only text value is accepted on both lanes: `contains ""` matches every item (full condemn weight library-wide) and `in ""` silently never protects — **high**

`src/reaper/engine/fields.py:555`; also `src/reaper/engine/fields.py:659`, `src/reaper/engine/policy.py:184`, `src/reaper/engine/policy.py:157`, `frontend/src/components/PolicyEditor.tsx:1082`

_Violates CLAUDE.md rule 32, 1, 7/24._

**Failure mode.** `_validate_value_type` checks only `isinstance(value, str)` for a TEXT field, so `""` and `" "`
pass. Condemn lane: a stored `BooleanCondemnSpec(field="genre", op=CONTAINS, value="",
weight=20)` makes `_compare` evaluate `"" in str(value).lower()` -> True for every item with a
Known genre, adding the rule's full weight to the entire library and rendering as the blank
sentence `Genre contains ` in the why-panel. Verified on this tree:
`evaluate(Condition(field='genre', op=CONTAINS, value=''), facts)` ->
`ConditionResult(matched=True, blocked=False, detail='Genre contains ')`. Protect lane:
`ConditionSpec(field="genre", op=IN, value="")` also saves, and `_split_csv("")` yields an empty
target set, so the condition can never match — a protection the owner believes exists does
nothing forever, reported as a green "checked and did not fire". The frontend disables Add only
on exactly `""` (PolicyEditor.tsx:1082 `disabled={… rValue === ""}`), so a single typed space is
UI-reachable and `genre contains " "` matches every multi-genre title (genres are comma-space
joined).

**Verifier's correction.** Real, but split the two halves — they have different reachability and the fixing agent should
not conflate them. (1) The dangerous half is CONTAINS with an empty/whitespace target on the
CONDEMN lane: `'' in anything` is True, so the rule's full weight lands on every item whose text
fact is Known, and the why-panel renders the truncated sentence 'Genre contains '. Exact `''` is
API-only (the Add button blocks it); `' '` is UI-reachable but narrower than the reviewer
implies — it matches only text that actually contains a space, i.e. multi-genre titles (comma-
space joined) and quality strings that contain one, not single-genre titles. (2) The quiet half
is IN with an empty target on the PROTECT lane: `_split_csv('')` yields `[]`, so it can never
match — a protection reported green forever. (3) I checked the ops the reviewer did not: `EQ`
with `''` is NOT affected — multi goes through `_split_csv` (empties dropped) and non-multi
compares against stripped text, so it only matches genuinely empty text. So the boundary check
belongs on CONTAINS/IN specifically (reject `value.strip() == ''`, and for IN reject a target
whose `_split_csv` is empty so a comma-only list cannot slip past); a blanket 'no empty text'
rule on every TEXT op is more than is needed but is also harmless. Fix at the save boundary in
`_validate_value_type` as proposed, and trim in `coerceValue` so the UI cannot compose one.

**Fix.** In `Condition._validate_value_type`, after the `isinstance(value, str)` check for
`FieldType.TEXT`, reject a value whose `.strip()` is empty with the operator-facing message the
API already renders (e.g. `f'"{spec.label}" needs a value.'`). For `Op.IN`, also reject a target
whose `_split_csv` yields no elements, so a comma-only list cannot slip past. Both refusals
belong at the save boundary so a stored policy can never carry a rule that matches everything or
nothing.

### B2-5 · The documented cross-tier contradiction veto never runs between the id tier and the file-basename tier, so an id bind can silently overrule the listing that provably holds the *arr's file — **high**

`src/reaper/engine/identity.py:1158`; also `src/reaper/engine/identity.py:74`, `src/reaper/engine/identity.py:1182`, `tests/test_identity.py:1496`

**Failure mode.** Radarr manages /movies/Title (2020)/the managed file.mkv (tmdbId 1001, 99 bytes). Plex holds two
listings: rk=200 is the row whose Part is exactly that file but Plex never matched it (local://
agent, so ExternalIds() is empty), and rk=100 is a second listing of the same content that DID
match and carries tmdb://1001, holding a different file. Tier 1 binds rk=100 on a single tmdb
hit. Tier 2 is never computed (the `tier1 is None` guard), even though `index.by_basename['the
managed file.mkv']` names exactly one row, rk=200. Tier 3 stays silent (regional title). Result:
`rating_key=100, matched_by=tmdb, status=matched`. Every downstream read — dormancy, distinct
watchers, the streaming veto, the executor's played-since-approval interlock — then describes
rk=100 while the delete routes by Radarr's own media_key and removes the file behind rk=200. A
user who plays the managed copy is invisible, the item reads "nobody's watching, long dormant,"
and it is condemned. This is precisely the failure the module docstring calls the prime-
directive catastrophe.

**Verifier's correction.** Mechanism is right; the PROPOSED FIX IS WRONG AS WRITTEN and must not be applied verbatim. I
applied it (drop `tier1 is None`, keep the >=2-hit abstain) and 11 tests fail:
TestByteIdenticalTwinListings (all 6), TestTheLibraryMapTellsTwoListingsApart (3),
TestTheLibraryMapWithMovies (1), and
test_review_scan.py::test_raw_items_merges_byte_identical_twin_listings. Cause: when an id names
several listings of the same file, `by_basename` names them too, so the unconditional tier-2
hits the `len(hits) >= 2` abstain at 1162-1165 and destroys exactly the merged-twins / library-
map narrowing tier 1 just did. The shape that works is CROSS-CHECK ONLY: keep the existing bind-
or-abstain branch when `tier1 is None`, and when tier1 bound, set `tier2 = hits[0]` only for
`len(hits) == 1` (a multi-hit basename is silence, already covered inside tier 1). I applied
that variant and got 128 passed on tests/test_identity.py + tests/test_review_scan.py, 536
passed across the identity/scan/snapshot/season selection, and both repro cases now return
`Kept: identifiers disagree (tmdb->100, basename->200)`. Two details for the fixer: (1) mirror
the tier3 group normalization at 1172-1175 for tier2 (a tier2 hit landing inside `tier1[3]` is
agreement with a merged group, not a contradiction); (2) tree was reverted — `git status` is
clean, no changes left behind. Also note `PlexItem.file_basename` is only the FIRST location's
leaf (see the docstring at 512-520), so this cross-check is silent, never wrong, for multi-file
merged listings.

**Fix.** Compute tier 2 unconditionally (drop `tier1 is None` from line 1158; keep the >=2-hit abstain),
and fold it into the existing reconcile at lines 1170-1185 the same way tier 3 already is:
normalize a tier2 hit that lands inside `tier1[3]` (the MERGED_LISTINGS group) to `tier1_rk`
alongside the tier3 normalization at 1172-1175, then let the `len(resolved) >= 2` abstain fire.
Binding behavior is unchanged when the tiers agree (line 1191 still returns tier1 and credits
its provenance); the only new outcome is an abstain, which keeps the file. Add a
`TestTheContradictionVeto` case for id-vs-basename.

### B2-6 · The movie keep-list/curated-list lookup passes only Radarr's imdbId, never the Plex-matched imdb id the item also carries — **high**

`src/reaper/services/snapshot.py:283`; also `src/reaper/services/snapshot.py:1666`, `src/reaper/services/lists.py:584`

_Violates CLAUDE.md rule 2, 29._

**Failure mode.** A Radarr movie whose record has no `imdbId` (Radarr is tmdb-native; a blank imdbId is common) is
bound to a Plex row that DOES carry one, so `RawItem.plex_imdb_id` is set (snapshot.py:1666).
`build_facts` then calls `membership_index.lookup(media_type="movie", imdb_id=item.imdb_id,
tmdb_id=item.tmdb_id)` with `imdb_id=None`. If the operator's "Never Reap" Plex collection row
for that film was stored with an imdb id and no tmdb id — exactly what `identity.parse_guids`
yields on a legacy-agent Plex library (`com.plexapp.agents.imdb://tt...`), the case
`lists.PlexCollection` explicitly says it supports — the `_by_tmdb` bucket has no entry for it
and the lookup returns []. `is_whitelisted` becomes `Known(False)`, `WhitelistGate` reports "not
on your keep list", and a film the operator put in Never Reap is condemned on a healthy,
EXECUTABLE snapshot and can be deleted.

**Verifier's correction.** Mechanism is right; narrow the trigger claim. IMDb Top 250 rows carry BOTH ImdbId and TmdbId
(lists.py:166-175) and ArrTagRule rows come from *arr payloads that carry tmdbId, so those lists
still match on tmdb. The genuinely exposed cases are (a) a Plex 'Never Reap' collection on a
legacy-agent library, where `identity.parse_guids` yields imdb only (lists.py:340-348), and (b)
a Radarr movie with no tmdbId (snapshot.py:1556 `tmdb_id = int(movie["tmdbId"]) if
movie.get("tmdbId") else None`) — in that case the lookup is called with imdb_id=None AND
tmdb_id=None and returns [] outright, losing the Top 250 and keep-tag protection too. Fix as
proposed (`item.imdb_id or item.plex_imdb_id`); the item carries no tvdb id on the movie path so
nothing else is missing. No existing test covers this (tests/test_fact_layer_states.py:72 is the
both-None case).

**Fix.** Mirror the TV path: compute `lookup_imdb = item.imdb_id or item.plex_imdb_id` and pass it as
`imdb_id=` in the `membership_index.lookup` call at snapshot.py:283. Add a test asserting a
movie with `imdb_id=None, plex_imdb_id="tt..."` matches an imdb-only stored keep-list row.

### B2-7 · resolve_show consults only tvdb, so a show's imdb id — present on both sides — never cross-checks the bind; a mis-tagged tvdb id binds a whole series to the wrong Plex show, where a movie in the same shape abstains — **medium**

`src/reaper/engine/identity.py:1036`; also `src/reaper/engine/identity.py:1077`, `src/reaper/engine/identity.py:1260`, `src/reaper/services/season_scan.py:1101`, `src/reaper/services/season_scan.py:1124`, `src/reaper/services/season_scan.py:1173`

**Failure mode.** Sonarr reports a series with imdbId tt0000042 and tvdbId 2001, but 2001 is stale/wrong (a
TheTVDB series split or a bad Sonarr match). Plex holds rk=100 (a different series, tvdb 2001 +
imdb tt0000001) and rk=200 (the real series, tvdb 9999 + imdb tt0000042). season_scan builds
`ExternalIds.of(imdb=series['imdbId'], tvdb=series['tvdbId'])`, so ids.imdb IS populated, and
the TV index's `by_imdb` IS populated from Plex's imdb:// GUIDs — but `_SHOW_ID_PRIORITY` is
`("tvdb",)`, so the loop at line 1077 never reaches imdb and the cross-check at line 1086 never
fires. The show binds rk=100. Every season of the real series is then judged on a stranger's
dormancy, watcher count and streaming state, and its seasons are deletable through Sonarr while
the show people actually watch reads untouched. The identical movie shape abstains:
resolve_movie(tmdb=2001, imdb=tt0000042) returns "the TMDB id and the IMDB id tt0000042 name
different Plex items; the ids contradict each other, so neither is trusted".

**Verifier's correction.** Severity kept at medium, not high: the mis-bind needs Plex to actually hold a *different* show
carrying the stale tvdb id, and tier 3 (title+year) already catches the subset where the titles
line up — the gap is real only when the title is regional/renamed or duplicated. Two
corrections to the reviewer's reasoning. (1) The 'corroborating inconsistency' at
season_scan.py:1124/1173 is weak evidence: `libraries_for_ids` / `candidate_libraries` are
explicitly diagnostics-only ('Never used to bind', identity.py:342-343, 364-365), so passing the
wider tuple there is harmless and should not be the argument for the fix. (2) Prefer the
reviewer's cross-check-only option and NOT the bare `_SHOW_ID_PRIORITY = ("tvdb", "imdb")`: as
written that also lets imdb ORIGINATE a show bind at 1103-1111 when tvdb names nothing, which
creates new binds (new deletable shows) rather than only new abstains. Gate the `len(hits) == 1`
/ `len(hits) >= 2` binding branches so only the first kind may bind, or add a separate show
cross-check list; then the change can only ever add abstains.

**Fix.** Consult imdb for shows as a CROSS-CHECK only, so the fix can only add abstains and never new
binds: after the tvdb pass, look up `index.by_imdb[ids.imdb]` and run the existing disjointness
veto at lines 1094-1100 against `{tier1[0], *tier1[3]}`. Either add a separate cross-check id
list for shows, or set `_SHOW_ID_PRIORITY = ("tvdb", "imdb")` and gate the `len(hits) == 1` /
`len(hits) >= 2` binding branches so only the first kind in the list may originate a bind. Then
make season_scan.py:1124 and :1173 pass the same tuple the resolver uses.

### B2-8 · A run's recorded policy_hash is never checked at execute time, so a plan built under a looser policy still deletes after the operator tightens it — **medium**

`src/reaper/services/executor.py:761`; also `src/reaper/services/planner.py:302`, `src/reaper/services/planner.py:527`, `src/reaper/api/runs.py:191`, `src/reaper/api/runs.py:382`

_Violates CLAUDE.md rule 7, 24, 2._

**Failure mode.** Operator plans a reap from snapshot S (run.policy_hash = P1) but does not execute it. They then
go to Policy and add a protection or raise the condemn threshold (new hash P2), which is exactly
the change that should void the pending approval. They come back to the Reap page and execute
the still-PLANNED run. `Executor.execute` reads only `run.approved_manifest_hash`; it never
reads `run.policy_hash`. The manifest is a hash of frozen, immutable candidate rows for that
snapshot, so it can never change for a policy edit — the run passes every gate and deletes the
items the *new* policy protects. Nothing expires PLANNED runs (grep for RunState.PLANNED shows
no sweeper), and `POST /runs/{id}/execute` accepts any run id in PLANNED state, so this also
applies to a plan built several scans ago.

**Verifier's correction.** Severity corrected high -> medium, and the fixer needs a mechanism the reviewer missed. (a) The
blast radius is bounded by disclosure: `save_policy`'s own docstring (api/routes.py:1206) tells
the operator "a saved policy takes effect on the next scan", and the frozen-snapshot design
means the current queue is under the old policy either way. (b) More importantly, the reviewer's
fix is not a pure add-a-check: `build_plan` copies `snapshot.policy_hash` (api/runs.py:191), and
a policy edit does NOT trigger a rescan, so comparing `run.policy_hash` to the live combined
hash would also refuse every FRESHLY built plan until the operator rescans, not just stale ones.
That is arguably the intended behavior (it matches planner.py:302 and the reasoning at
profiles.py:262-264 that tightening a cap is safe *because* caps are outside the policy hash),
but it is a behavior change to the normal flow and must ship with the operator copy telling them
to re-scan, plus a test. If the team decides plans should survive a policy edit, then the
required fix is the reverse: correct planner.py:301-303 and the profiles.py:262-264 comment in
the same change. Either way one of the two must move; today the code and the comments disagree.

**Fix.** In `Executor.execute`, after the manifest check, compare `run.policy_hash` against the current
combined policy hash (`combine_hashes(movie_policy.policy_hash(), tv_policy.policy_hash())`, as
`snapshot.py:693` builds it) and raise `ExecutionError` with plain copy telling the operator the
policy changed since approval and to re-scan and re-plan. Do it for dry runs too so the
simulation proves the same refusal. If a policy edit is deliberately meant NOT to void a pending
plan, delete the claim in planner.py:302-303 in the same change.

### B2-9 · Hand spares are loaded once at run start, so sparing an item while the reap is in flight does not stop its deletion — **medium**

`src/reaper/services/executor.py:742`; also `src/reaper/services/executor.py:1107`, `src/reaper/services/executor.py:1117`, `src/reaper/services/executor.py:1011`, `src/reaper/api/runs.py:421`

_Violates CLAUDE.md rule 2._

**Failure mode.** A 200-item reap takes minutes. The operator watches the follow-you bar, sees a title they want
to keep, navigates to Review and clicks Spare (which commits a WhitelistEntry through a
different session). The executor's per-item spare check at executor.py:1107 consults
`self._decisions`, a dict loaded exactly once at executor.py:742 before the first item, so the
new spare is invisible and the file is deleted. The same staleness applies to
`self._effective_keys` (executor.py:744), so withdrawing a hand reap mid-run also has no effect.
The only mid-run control that actually works is Stop, which halts the whole run.

**Verifier's correction.** Severity corrected high -> medium: the operator still has a working mid-run control (Stop, re-
read per item at executor.py:1015-1021), and every spare committed before `execute()` starts IS
honored. Two mechanism corrections for the fixer: (1) a fresh session is NOT strictly required
here. `whitelist.overrides` (services/whitelist.py:86) is a Core column select
(`select(WhitelistEntry.media_key, WhitelistEntry.decision)`), not an ORM entity load, so the
identity-map staleness that forced `armed_recheck` onto a fresh session does not apply; the run
session commits after every item (executor.py:1029-1033), so a plain re-query at the top of the
loop sees other sessions' committed rows. A fresh session is still acceptable and matches the
existing pattern. (2) The refreshed map must be used ONLY for the two per-item checks;
`_check_caps` and `_check_rolling_caps` (executor.py:851-853) already ran against the run-start
set and must stay there, and a refresh can only ever remove items from what is sent, so it is
fail-closed. Also update `whitelist.overrides`'s docstring line 75 ("what live consumers read
once") and executor.py:679-683 in the same change so the comments match the new contract.

**Fix.** Re-read the overrides before each item the way `_still_armed` re-reads the arm switch: either
refresh `self._decisions` / `self._effective_keys` at the top of `_run_deletes`'s loop from a
fresh session (the run session already commits per item, so a plain re-query would also see the
new row), or inject an `override_recheck` callable mirroring `armed_recheck`. Keep the cap math
on the set captured at run start (it must stay fixed) and use the refreshed map only for the
per-item spare / effective-set checks, which can only ever remove items.

### B2-10 · The rolling 30-day budget counts only VERIFIED steps, but a movie whose file is confirmed gone with an unverifiable exclusion is marked FAILED, so real deletions are never charged against the monthly cap — **medium**

`src/reaper/services/executor.py:977`; also `src/reaper/services/executor.py:1511`, `src/reaper/services/executor.py:1936`, `src/reaper/api/runs.py:488`

_Violates CLAUDE.md rule 5, 30._

**Failure mode.** Radarr accepts the delete, the file is removed, `_movie_is_gone` confirms the 404, but the
import exclusion does not appear in the exclusion list within the 5x1s poll window (a busy
Radarr, or a Radarr that ignores `addImportExclusion`). `_send_movie` takes the `not (excluded
and gone)` branch and returns `_fail(...)`, which sets the `radarr_delete` step to FAILED. The
file is gone and its bytes are reclaimed, but `_rolling_30d_deletions` filters on
`ActionStep.state == StepState.VERIFIED`, so neither the item nor its bytes enter the
trailing-30-day totals. Repeat over a month on a flaky Radarr and the operator's rolling
item/byte budget is exceeded by the sum of every such item, with the cap check reporting a
number it knows to be short. `report.deleted_items` is also not incremented, so `api/runs.py:488
if report.deleted_items > 0: launch_scan(app)` skips the rescan and the queue keeps showing
files that no longer exist.

**Verifier's correction.** Real, but the reviewer's first proposed fix is wrong and must not be applied: calling
`_mark_verified(step, {"gone": True, "excluded": False})` would mark a step VERIFIED whose
verification explicitly failed, which contradicts `_fail`'s journal contract
(executor.py:1919-1923) and would make the after-action report and the step journal claim the
exclusion path succeeded. Take the second option: add a durable per-step field (e.g.
`file_removed_at`, a nullable ADD COLUMN in a new Alembic revision per the frozen-baseline rule)
set whenever `gone` is True, and have `_rolling_30d_deletions` count `VERIFIED OR
file_removed_at IS NOT NULL`. Two scope notes: (a) the canary mitigates the systematic case — a
Radarr that never adds exclusions fails the FIRST real delete and aborts the run
(executor.py:1069-1085), so this bites on intermittent failures on non-canary items; (b) the
same undercount is produced by candidate 4's escape path, which leaves the terminal step SENT
after the file is gone, so fix both against the same new field. The `deleted_items`/rescan half
is true but secondary: the rescan is skipped only when NO item in the run verified.

**Fix.** Record the file's removal independently of the exclusion outcome: when `gone` is True, write the
terminal step's verification (e.g. `_mark_verified(step, {"gone": True, "excluded": False})`)
before returning the failure outcome, or add a durable `deleted_at` on ActionStep and count that
in `_rolling_30d_deletions` instead of `state == VERIFIED`. Also count such items in
`report.deleted_items` (or add a separate `deleted_unconfirmed` counter that `api/runs.py:488`
consults) so the post-run rescan fires.

### B2-11 · The mid-binge guard anchors on the highest season NUMBER a viewer touched, not the season they are actually watching, so a re-watcher or out-of-order viewer gets no protection at all — **medium**

`src/reaper/services/season_pruning.py:150`; also `src/reaper/services/season_scan.py:802`, `src/reaper/services/season_scan.py:1416`

_module contract in season_pruning.py:18-26 ("keep the season they last watched and the next one")_

**Failure mode.** A viewer who has completed the whole show and is now re-watching (or who sampled one later-
season episode) is judged to be at the highest-numbered season they have ANY play under.
Concrete: viewer completed S1-S6 of a 6-season show and is now on S2 episode 3 of 10.
`_progress_by_user` yields {1:10, 2:3, 3:10, 4:10, 5:10, 6:10}; `m = max(real) = 6`; `final[6] =
10`; `watched(10) >= final(10)` -> positions = {7}. Season 7 does not exist, so the guard
protects nothing. With keep_last=2 the plan is prunable [2, 3, 4] — season 2, which the viewer
is mid-binge on, is condemnable. `_detect_conflicts` does not catch it either (S2 and the kept
S5/S6 all have the same all-time watcher count, and the test is strictly-greater). Once the
viewer's pause on S2 exceeds the dormancy floor (which is typically far shorter than
`in_progress_hold_days`), the season they are half-way through is condemned. The same anchor
also excludes Season 0 entirely: `real` drops SPECIALS_SEASON, and m>=1 so positions are always
>=1, meaning a viewer part-way through specials is never held when `keep_specials` is off.

**Verifier's correction.** Mechanism is right; severity is overstated at 'high'. Under the STOCK policy this is inert:
DEFAULT_TV_POLICY inherits DEFAULT_MOVIE_POLICY.gates, which sets MIN_DORMANCY threshold=1095
(engine/policy.py:998), and a season's dormancy is measured from its OWN last_played
(build_season_facts), so a mid-binge season is hard-PROTECTed for 3 years after the viewer's
last play there — far past the 180-day `in_progress_hold_days` default. The exposure window
opens only when the operator lowers the dormancy floor below in_progress_hold_days (policy
validation allows MIN_DORMANCY down to 5, engine/policy.py:110). So frame the fix as 'the guard
silently fails to honor its documented contract for re-watchers/out-of-order viewers', not
'Reaper deletes what you are watching today'. On the fix: prefer the recency anchor (pick `m` by
the latest `user_season_last` timestamp, threading per-season times into
`sequential_protections`). Do NOT take the reviewer's stronger alternative verbatim — 'protect
EVERY season where the position is below that season's final episode' pins a season abandoned
mid-way years ago for as long as the viewer stays active anywhere in the show, which can make a
long-running show permanently unprunable. The specials point is a separate, minor issue (a
viewer part-way through Season 0 gets no hold when keep_specials is off) and should be fixed as
its own line, not folded into the anchor change.

**Fix.** Anchor per viewer on the season with the most recent play rather than the highest number: thread
the per-season timestamps (`user_season_last`, already scoped per show in `_progress_by_user`)
into `sequential_protections` and pick `m` by latest `watched_at`. Safer still, protect EVERY
season where the viewer's position is strictly below that season's final on-disk episode (an
incomplete position is an open binge, whatever its number), plus `m+1` for the most recent
completed one. Include SPECIALS_SEASON in the anchor set when `keep_specials` is off, since that
is the only configuration in which a special can be pruned.

### B2-12 · A degraded IMDb dataset is turned into `Absent` ratings for every item, which silently withdraws the rating protection instead of blocking it — **medium**

`src/reaper/services/snapshot.py:649`; also `src/reaper/services/season_scan.py:1302`, `src/reaper/services/display_meta.py:93`

_Violates CLAUDE.md rule 2, 7, 24, 28._

**Failure mode.** The IMDb dataset goes stale (the `refresh_ratings` job is operator-disableable in Settings, and
its off-warning even says so). `ImdbRatings.lookup` raises `DatasetDegradedError`; the handler
degrades the snapshot and sets `imdb = {}`. `build_facts` then calls `dataset_lookup({},
item.imdb_id, item.plex_imdb_id)`, which returns `(None, looked_up=True)` for every item that
has any imdb id, so `imdb_rating_tenths`/`imdb_votes` become **Absent** — "we looked, there is
genuinely no rating" — for the entire library. Consequences: (a) `RatingFloorGate._blocked`
only fires on `Unknown`, so the gate is NOT blocked; if `facts.ratings` carries no IMDb entry
for that item (always true on the TV path, and true for any movie whose Radarr `ratings` object
lacks imdb) the gate reports checked-and-did-not-fire with the false detail "no IMDb rating (you
keep 7.5 from 1,000 votes)" and the item is free to be condemned, where `Unknown` would have
blocked and forced ABSTAIN (keep); (b) `signals.evaluate_keep` gives an `Absent` value 0
discount where `Unknown` gives the FULL discount, so every rating-based graded keep is withdrawn
and every score rises. The result is a library-wide condemn set built on evidence Reaper itself
declared untrustworthy, with a why-panel that lies about why.

**Verifier's correction.** Severity is overstated at high — correct the mechanism for the fixer. The snapshot IS degraded,
and planner.build_plan refuses outright (planner.py:316-321), so nothing from this snapshot can
be planned or deleted; there is no path to a deletion. The concrete harm is (1) an inflated,
dishonest condemn set on a viewable snapshot (why-panel asserts a rating was checked and absent
when it was never readable), and (2) that condemn set feeds the unguarded grace-clock write and
Leaving Soon/Discord announce (see candidate 4), which are the only surfaces that act on a
degraded run. Also correct the comment at snapshot.py:246-248 in the same change: degrading
happens, but it does NOT prevent the 'silently unprotecting every film' half, which is what the
comment claims (rule 24). Fix both builders — snapshot.build_facts and the season_scan
equivalent (season_scan.py:1298-1304 feeds _judge_series).

**Fix.** Carry the dataset's degraded state into the fact builders: on `DatasetDegradedError`, set a flag
(e.g. `imdb_degraded = True`) and have `build_facts` (and `season_scan`'s equivalent) emit
`Unknown(reason="the IMDb ratings data could not be read", source="imdb")` for
`imdb_rating_tenths`/`imdb_votes` instead of routing through `dataset_lookup`'s `looked_up`
branch. Correct the comment at snapshot.py:246-248 in the same change.

### B2-13 · _primary_reason indexes into stored-explanation entries without the isinstance/key guards its siblings use, so one malformed row 500s the entire review-queue page — **low**

`src/reaper/api/routes.py:502`; also `src/reaper/api/routes.py:521`, `src/reaper/api/routes.py:643`

**Failure mode.** `_primary_reason` only wraps `json.loads` in its try. A stored explanation whose top level is
not an object (`json.loads` returns None/list) raises AttributeError on `exp.get(...)`; a
`protections_fired[0]` that is not a dict, or a dict without a `detail` key, raises
TypeError/KeyError on `fired[0]["detail"]`; a non-dict `match` value raises AttributeError on
`(exp.get("match") or {}).get("status")`. `_candidate_out` calls this for every row, so a single
bad row makes `GET /api/candidates` and `GET /api/groups/{key}` return 500 and the whole review
queue goes blank — not just that row's reason line. Its own sibling `_dormant_for` documents
the contract it breaks, and `_chip` defends against exactly these shapes (`fired[0] if fired and
isinstance(fired[0], dict) else None`, `[e for e in ... if isinstance(e, dict)]`) while
`_primary_reason` does not.

**Verifier's correction.** Line drift: the parse is 501-503, the unguarded accesses are 509 / 514 / 518 / 523 / 530, and
the _chip contrast lines are 645 / 658 (not 505 / 521 / 643). IMPORTANT trigger correction the
fixer needs: this is NOT live-triggerable with data Reaper writes. explanation_json has exactly
one writer (services/snapshot.py:1115) via the `Explanation` pydantic model
(api/schemas.py:71-105), where `detail` is a required str and `match` is a MatchOut-or-null — so
the malformed shapes require a corrupted or hand-edited DB, or a future schema change. Treat it
as a contract/robustness gap of the same class and severity as the already-documented B-10
(services/condemned.py:71, same unguarded `match` pattern), not as a live 500. Also worth
telling the fixer: docs/CODE_REVIEW.md PR-7 currently cites '_primary_reason 501-504' as one of
the DEFENDED siblings, which this finding shows is wrong — correct that line in the same change.

**Fix.** Guard `exp` with `isinstance(exp, dict)` after the parse (returning None otherwise), filter
`protections_fired` / `protections_unknown` / `signals` to dicts, use `.get("detail")` instead
of `["detail"]`, and coerce `exp.get("match")` through an isinstance check before
`.get("status")` — matching what `_chip` already does.

### B2-14 · A transient Plex probe failure during linking is mapped to 400, which aborts the browser's poll loop and throws away the still-valid PIN the service deliberately kept — **low**

`src/reaper/api/settings.py:685`; also `src/reaper/services/plex_link.py:151`, `src/reaper/services/plex_link.py:501`, `src/reaper/api/settings.py:805`, `frontend/src/components/PlexPin.tsx:101`

_Violates CLAUDE.md rule 10, 24._

**Failure mode.** Operator clicks Link, approves the PIN on plex.tv, and at that instant their Plex server is
restarting (or briefly unreachable on every advertised address). `reachable_connection` raises
`PlexLinkRetryableError` (plex_link.py:151). `poll_link` catches it at plex_link.py:501, sets
`consume_pending = False` and re-raises, so the PendingPlexLogin row survives on purpose.
`plex_link_poll` then catches it as the parent `PlexLinkError` and returns HTTP 400. The
browser's poll loop (`PlexPin.tsx:102-105`) does `catch (e) { stop(); setServers(null);
h.onFailed(...) }` on ANY thrown error, so it stops polling permanently and shows a hard
failure. The preserved PIN is never re-polled and the operator must redo the whole OAuth round-
trip — exactly the outcome the service comment says it prevents.

**Verifier's correction.** Line drift: the arm is at settings.py:685 (raise on 686), not 684; the route starts at 662.
Severity corrected medium -> low: the blast radius is one Plex link flow having to restart OAuth
after a transient blip — no data risk, fully recoverable, and only when the server is
unreachable at the exact instant of approval. MECHANISM CORRECTION the fixer must not miss:
adding `except PlexLinkRetryableError -> 502` alone changes NOTHING user-visible, because the
frontend aborts the poll loop on any non-2xx (it throws). The real fix is a non-throwing wire
shape — return `PlexLinkPollOut(status="pending"/"retrying", ...)` (with the reason for display)
so the existing loop keeps polling the still-valid PIN until the 10-minute LINK_TTL /
DEADLINE_MS expires — or a matched frontend change that treats that specific status as
retryable. Backend-only 502 is a no-op fix here.

**Fix.** Add `except PlexLinkRetryableError as exc: raise HTTPException(502, str(exc)) from exc` ahead of
the `PlexLinkError` arm in `plex_link_poll`, matching `plex_switch_server`. Since the frontend
poll loop aborts on any thrown error regardless of status, also give the retryable case a non-
throwing wire shape (e.g. return `PlexLinkPollOut(status="pending", ...)` with the reason, or
have `PlexPin` continue polling on 502) so the preserved PIN is actually re-polled.

### B2-15 · `OthersWatchingGate` can never PROTECT: `others_watching` is Absent in every fact builder, so the count is always 0 against a floor of at least 1 — **low**

`src/reaper/engine/gates.py:579`; also `src/reaper/services/snapshot.py:335`, `src/reaper/services/season_scan.py:592`, `src/reaper/engine/backtest.py:386`, `src/reaper/services/scan_runner.py:104`

_Violates CLAUDE.md rule 38, 25, 7/24._

**Failure mode.** All three `Facts` builders set `others_watching=Absent(...)` unconditionally — there is no code
path that ever produces a `Known` value. `OthersWatchingGate.evaluate` does `count =
others.value if isinstance(others, Known) else 0` and `floor = max(self.config.threshold, 1)`,
so `count >= floor` is never true and the gate always ABSTAINs with "Nobody besides the
requester has watched it." `GateId.OTHERS_WATCHING` is nonetheless in `GATE_TYPES` and accepted
by `GateSettingIn.gate`, so an operator who adds it via `PUT /api/policy` (expecting the
docstring's "The requester ignored it, but other people did not") gets a protection that is
built, evaluated, hashed into the policy, and can never keep a file — while its ABSTAIN line
reads to them as a check that ran and found nothing.

**Verifier's correction.** Severity low is right (not in either shipped default, so it takes a deliberate API write to
enable). One correction to the proposed fix: the reviewer warns off nothing, but the ORDER
matters and the reviewer's own candidate 6 gets it backwards. Deleting `others_watching` from
`facts_codec._OBS_FIELDS` (line 42) is codec-SAFE — `facts_from_dict` iterates `_OBS_FIELDS` and
indexes into the stored `obs` dict, so a stored key that is no longer listed is simply ignored.
It is ADDING a name to `_OBS_FIELDS` that breaks stored `facts_json`. So the delete path is:
remove the gate class, its `GATE_TYPES` entry, `GateId.OTHERS_WATCHING`, `Facts.others_watching`
(gates.py:166) and all three builders' assignments, the `_OBS_FIELDS` entry, the policyMeta
entry and the routes.py:604 phrase, in one change (rules 38/64). Note that removing the GateId
member does make an already-stored body naming it fail validation, which routes it through
`profiles.active_policy`'s flagged fallback — acceptable and loud, but it should be a conscious
decision. If instead you wire it, the evidence already exists: `engine/requester.py:50`
(`others_watching(exclude=...)`) computes exactly this count for the requester rule.

**Fix.** Either wire it — populate `others_watching` with a real `Known`/`Unknown` from the per-user
play map the requester rule already builds — or delete the gate, its `GATE_TYPES` entry,
`Facts.others_watching`, and its `_OBS_FIELDS` entry in one change, per rule 38. Until one of
those lands, `build_gates` should refuse `GateId.OTHERS_WATCHING` with the same
`ScanConfigError` it raises for an unimplemented gate, so an operator cannot enable a protection
that cannot fire.

### B2-16 · from_radarr parses the vote count with a bare int() while the sibling score parse is guarded, so a non-integer votes field raises out of the scan instead of degrading that one rating — **low**

`src/reaper/ratings.py:263`; also `src/reaper/services/snapshot.py:1679`

_Violates CLAUDE.md rule 32 (evaluation never raises out of a scan); the module's own defensive parsing at line 248-251._

**Failure mode.** Radarr (or a proxy/fork/older version) returns `{"imdb": {"value": 7.5, "votes": "1,234"}}` or
`{"votes": 1234.0}` serialized as "1234.0", or any non-integer shape. `int(raw_votes)` raises
ValueError (TypeError for a list/dict), which propagates out of `from_radarr` ->
`snapshot.py:1679 arr_ratings=tuple(from_radarr(movie.get("ratings")))` -> the movie fact build,
aborting the whole scan on one malformed field. Every other parse in this module degrades that
one value instead: the score three lines above is wrapped in `try/except (TypeError,
ValueError): continue`, and `from_plex` does the same. The asymmetry means a payload quirk that
should cost one rating costs the operator the entire run.

**Verifier's correction.** Real but it is a robustness/consistency gap, not a live bug against stock Radarr, whose schema
declares votes as an integer: the trigger is a fork, proxy, or future schema change. Blast
radius is correct as described (one malformed field aborts the operator's entire run) but the
direction is fail-CLOSED — nothing is deleted, so this is not a safety hole, which is why low
is the right severity. The proposed fix is correct as written; note `Rating.meets`
(ratings.py:127-132) already treats `votes=None` as failing a vote floor, so degrading to None
keeps the safe reading. `int(1234.7)` truncating is fine, not part of this.

**Fix.** Wrap the votes coercion the same way as the score: `try: votes = int(raw_votes) if raw_votes and
source not in _PERCENTAGE_SOURCES else None` / `except (TypeError, ValueError): votes = None`. A
None vote count already fails a vote floor closed via `Rating.meets` (line 127-132), so the safe
reading is preserved.

### B2-17 · The reap progress bar's total switches from the confirmed item count to the raw plan-step count on the first progress tick — **low**

`src/reaper/services/executor.py:1005`; also `src/reaper/api/runs.py:398`, `src/reaper/api/runs.py:436`, `src/reaper/services/executor.py:1219`

_Violates CLAUDE.md rule 62, 30._

**Failure mode.** Operator plans 10 items, then spares 3 in the review queue. `_planned_candidates` drops the 3,
the confirmation phrase reads "REAP 7 SOULS ...", and `execute_run` sets `status.total = 7`. The
executor then computes `total = len(deletes)`, which deliberately keeps the spared items in the
walk so the report can explain them, and every `ReapProgress` carries `total=10`. `on_progress`
overwrites `status.total`, so the moment the first item finishes the UI flips to "1 of 10 souls"
and a progress bar denominated in 10 — a larger number than the one the operator typed to
authorize the run, and it never reaches 100% of the confirmed set.

**Verifier's correction.** Real but one claim in the failure mode is wrong and should not be repeated in the fix rationale:
the bar DOES reach 100%. Skipped items emit progress too (`_emit_progress(index + 1, total,
...)` at executor.py:1045 before `continue`), so `done` always converges on `total`; the defect
is the denominator flipping mid-run to a number larger than the one the operator typed to
authorize, and disagreeing with the header in the same modal. Trigger is narrower than stated:
`planned` and `deletes` differ only when an override changed or the unmeasured allowance moved
between plan build and execute (a spare in the review queue after planning is the common case).
Preferred fix is the second option — carry both numbers on `ReapProgress` (acted-on total plus
kept count) so the UI can say "1 of 7 (3 kept)" without losing the report's visibility into
skipped items, since the walk must keep them (executor.py:770-777).

**Fix.** Emit progress against the same set the confirmation phrase counts: compute `total` in
`_run_deletes` from `_deletable(deletes, self._effective_keys, allow_unmeasured=...)` (the
executor's own exact acted-on set) and count skipped-by-override items toward `done` without
inflating the denominator, or carry both numbers on `ReapProgress` so the UI can say "1 of 7 (3
kept)".

### B2-18 · `_fetch_available` fans out across live Seerr portals with a bare asyncio.gather, so the first portal failure leaves sibling reads running against clients the route is about to close — **low**

`src/reaper/services/fairness.py:899`; also `src/reaper/services/fairness.py:771`, `src/reaper/api/fairness.py:129`

_Violates CLAUDE.md rule 34._

**Failure mode.** Two Seerr portals. Portal A raises IntegrationError two seconds in; portal B is mid-way through
paging several thousand requests. `asyncio.gather` (no return_exceptions) re-raises A
immediately while B's `all_requests` task keeps running detached. `build_report` propagates,
`get_fairness` (api/fairness.py:128-136) exits its `AsyncExitStack`, which calls `aclose()` on
BOTH SeerrClients, and B's next page then fails inside a task nobody is awaiting with httpx's
"Cannot send a request, as the client has been closed". Until that happens B keeps hammering the
operator's portal for a report that has already 502'd. This is the exact failure `reaper/aio.py`
was written for and every other multi-service fan-out in the codebase (scan_runner:649,
snapshot:1935, season_scan:998/1275, library_index:123, leaving_soon:323) uses `gather_reaped`
instead.

**Verifier's correction.** Severity is low, not medium — correct the mechanism before fixing. (1) 'Until that happens B
keeps hammering the operator's portal' is overstated: the AsyncExitStack unwinds immediately as
the exception propagates out of build_report, so the sibling dies on its very next request. The
real residue is an unobserved task plus an 'exception was never retrieved' warning at teardown,
and one extra in-flight page. No data corruption; RequestCache stores nothing on failure (it
assigns only after _fetch_available returns). (2) Only line 899 is a genuine instance. Line 771
(_enrich_titles) is NOT: the inner `_one` catches IntegrationError itself and returns, so no
ordinary failure can escape gather to detach siblings — changing it is harmless but not the
finding. Line 714 (_person_quotas) already passes return_exceptions=True and awaits every task,
so it is correct as written. (3) Requires 2+ configured Seerr portals to reproduce at all.

**Fix.** Import `gather_reaped` from `reaper.aio` and use it in `_fetch_available` (line 899) and in
`_enrich_titles` (line 771, up to 80 concurrent title lookups against the same clients),
matching every other fan-out in the codebase.

### B2-19 · A season-scoped request whose seasons are not in the scan inflates the board's request count but appears in neither the person drawer's list nor the not-in-scan panel — **low**

`src/reaper/services/fairness.py:532`; also `src/reaper/services/fairness.py:1074`, `src/reaper/services/fairness.py:445`

_Violates CLAUDE.md rule 30._

**Failure mode.** Someone requests Season 5 of a show; the scan holds candidates only for seasons 1-3 (4-5 fully
protected or filtered out). In `roll_up`, `_match_candidates` returns the seasons 1-3 rows so
the group is NOT unmatched, then `_scope_to_request` filters to the requested season set and
returns `[]` — but `row.requests_made += 1` at line 532 runs unconditionally, adding 1 to the
board's "N requests" and to the denominator of the watch rate (Fairness.tsx:48 `100 *
row.played_by_them / row.requests_made`) while `played_by_them` can never increment for it.
Meanwhile `build_person_detail` skips exactly this case (`if not scoped: continue`, line 1074),
so `requests_in_scan` excludes it, and `_collect_unmatched` also skips it because
`_match_candidates(index, rep)` is non-empty (line 445), so it is not in `not_in_scan` either.
The operator sees "7 requests" on the card and "6 requests in the last scan / 0 not in the last
scan" in the drawer, with one title findable nowhere.

**Verifier's correction.** Severity low is right, but the reviewer understated the contract violation, which is the
strongest argument for fixing it: fairness.py:197-198 documents requests_made as 'what the scan
still has' and line 187 documents played_by_them as 'Of the titles they requested (and the scan
has)' — the phantom breaks both, so the SAME person's watch rate differs between the board
(frontend/src/components/Fairness.tsx:47-48, denominator requests_made) and the drawer
(frontend/src/components/ScalesPanel.tsx:207-208, denominator requests_in_scan). Frontend paths
in the candidate are wrong: the files are frontend/src/components/Fairness.tsx (not pages/)
lines 47-48 and 102, and frontend/src/components/ScalesPanel.tsx lines 231 and 264. The board
headline is unaffected — total_requests is `len(requests)` (line 576), not a sum of
requests_made. Of the two proposed fixes, prefer the second (make _collect_unmatched classify a
group whose requested seasons produced no candidate), because merely adding the `if not scoped:
continue` guard to roll_up makes the request vanish from every surface instead of appearing in
the not-in-scan panel where the operator can see it — and note _collect_unmatched currently
groups per content key with no season awareness, so this is a per-request, not per-group,
classification change.

**Fix.** In `roll_up`, skip the per-person accounting when `scoped` is empty (mirroring
`build_person_detail`'s line 1074 guard) and route the request into `_collect_unmatched`, or
make `_collect_unmatched` classify a group whose requested seasons produced no candidate as
UNMATCHED_SET_ASIDE so the two surfaces and the not-in-scan count agree.

### B2-20 · The Leaving Soon announced-set is a read-modify-write across minutes of network I/O, so two overlapping passes double-announce and one loses the other's entries — **low**

`src/reaper/services/leaving_soon.py:401`; also `src/reaper/services/leaving_soon.py:442`, `src/reaper/services/leaving_soon.py:505`, `src/reaper/api/leaving_soon.py:34`

_Violates CLAUDE.md rule 8._

**Failure mode.** `run_sync` reads the announced set at line 401, then does the whole per-library Plex reconcile
(a whole-section rating-key dump per library) and the Discord post, and only writes the set back
at line 442. Nothing serializes the two entry points: `POST /api/leaving-soon/sync`
(api/leaving_soon.py:34, no lock) and `after_scan` (line 485, fired at the end of every scan).
Operator presses "Update now" while a scheduled scan is finishing: both passes read
`already={A}`, both compute `to_announce=[B]`, and the household gets the "leaving soon" heads-
up for B twice. Worse, if the grace set moves between the two reads (pass 1 announces B, pass 2
announces C), the later writer persists `{A}|{C}` and drops B, so B is announced a THIRD time on
the next pass. Rule 8 requires the announcement to be idempotent keyed on the durably-persisted
set; the set is durable but the update is not atomic.

**Verifier's correction.** Mechanism is right; two additions for whoever fixes it. (1) after_scan's own fallback path has
the identical shape and must be fixed in the same change: read at leaving_soon.py:505, write at
510 — that path runs whenever the shelf is off (or Plex is unreachable) but a webhook is set, so
it is the COMMON path for most operators, not an edge case. (2) The precedent named in the
proposed fix is right but its shape matters: history_sync.py:188-203 deliberately does NOT use a
bare module-level asyncio.Lock — it keys a WeakKeyDictionary on the running event loop, because
a module-level Lock binds to whichever loop first awaits it and breaks under per-test loops
(rule 37). Copy that shape. The merge-on-write alternative (re-read inside the final transaction
and persist `(stored | announced_now) & in_grace`) fixes the lost update but NOT the duplicate
Discord post, so it is only half a fix.

**Fix.** Serialize the pass (a per-process asyncio.Lock around `run_sync`, the same pattern
`history_sync._rebuild_lock` uses), or re-read the announced set inside the final write
transaction and merge (`stored | announced_now) & in_grace`) instead of overwriting with the
value derived from the pre-I/O read.

### B2-21 · A restore swap interrupted between the two move loops discards the backup's secret.key/secret.salt on the next boot and prints "current data kept" when the data was already replaced — **low**

`src/reaper/services/restore.py:531`; also `src/reaper/services/restore.py:543`, `src/reaper/services/restore.py:547`

_Violates CLAUDE.md rule 2._

**Failure mode.** `apply_pending_restore` moves the live reaper.db/-wal/-shm/secret.key/secret.salt into `pre-
restore-*`, then moves the staged reaper.db, secret.key and secret.salt in. If the container is
killed (host reboot, OOM, `docker stop` timeout during preflight) after `pending/reaper.db` has
been renamed into `data/` but before `pending/secret.key` follows, the next boot still sees
READY, but `staged_db = pending/reaper.db` no longer exists, so `_looks_like_sqlite` is False
and the code takes the "armed but unusable" branch: it `shutil.rmtree(pending)`, which deletes
the backup's secret.key and secret.salt — the only copies of the key that decrypts the
credentials in the reaper.db that is now live — and writes "a staged restore was unreadable and
was discarded; current data kept" to stderr, which is false: the current data was already
replaced. The install then boots on the restored database, mints a fresh key, and every stored
Sonarr/Radarr/Plex credential silently fails to decrypt.

**Verifier's correction.** Real; severity low is correct and the window is even narrower than the write-up implies — it is
the gap between two same-filesystem renames (microseconds), and it only bites when the staged
backup actually contained secret.key/secret.salt. One blast-radius correction: the `pre-
restore-*` directory still holds the OLD live key/salt, but those decrypt the OLD database, not
the restored one, so the reviewer's conclusion (stored *arr/Plex credentials in the restored DB
become undecryptable) stands. Prefer the minimal half of the fix: in the unusable branch, move
`pending/secret.key` and `pending/secret.salt` into a `pre-restore-*` directory instead of
rmtree-ing them, and correct the stderr copy so it does not assert "current data kept" when it
cannot know that (plain language, no em dash, per rule 21). A progress marker (READY ->
IN_PROGRESS before the first move loop) is the fuller fix and lets the resumed boot finish the
remaining moves honestly.

**Fix.** Write a progress marker (e.g. rename READY to IN_PROGRESS, or drop a `SWAPPED` file) before the
first move loop, and make the unusable branch check it: if the swap was already under way, do
not rmtree `pending` — finish the remaining moves (key/salt) and report honestly that the
database was already replaced. At minimum, never delete `pending/secret.key` /
`pending/secret.salt` on the unusable path; move them into the `pre-restore-*` directory instead
so the key survives.

### B2-22 · `_flag` returns a definite `True` for an unparseable Tautulli value, so an unreadable Keep-History setting reads as "recording" instead of degrading — **low**

`src/reaper/services/scan_runner.py:402`; also `src/reaper/services/scan_runner.py:450`

_Violates CLAUDE.md rule 2, 7, 28._

**Failure mode.** `_flag`'s docstring promises `None` "when the row or field is missing or unreadable — the
caller decides what absence means, because it differs per flag", and
`_keep_history_degradations` is built on that promise (`keeps is None` -> counted as unreadable
-> degrade). But the `except (TypeError, ValueError)` arm returns `bool(value)`, and every non-
empty string is truthy. A Tautulli row carrying `"keep_history": "false"` (or `"N"`, or `"off"`)
therefore yields `True`: the user is treated as recording history, no degradation is appended,
and the scan stays executable while everything that user alone watches reads never-played — the
exact evidence the condemn lane scores on, and the exact case this function exists to catch.

**Verifier's correction.** Real but the trigger is thinner than the reviewer implies, so treat this as a contract/rule-7
fix rather than a live bug: `clients/tautulli.py:115-125` passes `get_users` rows through
untouched, and Tautulli stores keep_history as an INTEGER, so real payloads are 1/0 (or "1"/"0",
which int() handles). Note also that the falsy-string case is already fail-closed (`bool("")` is
False, which degrades), so only a truthy unparseable value ('false', 'N', a dict) fails open.
The proposed fix is right; make sure the None return also covers the non-numeric-truthy case
rather than only the exception path, and add a `_flag` unit test since none exists.

**Fix.** Return `None` from the `except` arm (and for any string that is not a recognized boolean token),
so an unparseable value lands in the `unreadable` bucket and degrades, matching the docstring
and rule 28.

### B2-23 · The keep-rule conflict detector reads a season Plex never resolved as "0 people watched it", producing spurious abstains and a false operator-facing claim — **low**

`src/reaper/services/season_scan.py:1422`; also `src/reaper/services/season_pruning.py:343`, `src/reaper/services/season_pruning.py:345`

_Violates CLAUDE.md rule 30 (counts run over the exact acted-on set); 21 (operator copy must be true)._

**Failure mode.** `watchers_by_season` is built only from `item.seasons_in_plex` — the seasons Plex RESOLVED --
while `_detect_conflicts` runs over the seasons Sonarr has ON DISK. A season on disk but absent
from Plex (Plex has not scanned it yet, or `seasons_from_rows` dropped a duplicate "Season N" as
ambiguous — the exact case season_scan.py:479-496 already warns about) falls through
`watchers_by_season.get(n, 0)` and is asserted to have zero watchers. Concrete: a matched show
where Season 1 is missing from the Plex sweep and Season 4 has one watcher. `_detect_conflicts`
fires 1 > 0, the season-progression gate returns a blocked ABSTAIN, and the why-panel tells the
operator "1 person watched Season 4, more than watched Season 1, which Reaper is keeping because
it is the first season" — a comparison against a number that was never measured.

**Verifier's correction.** Real, but the direction matters and the reviewer did not state it: the collapse can only ADD a
spurious conflict (kept side unmeasured -> 0 -> pruned>kept fires), which forces a blocked
ABSTAIN — i.e. it keeps the file. The opposite direction (a prunable season's own watchers
unmeasured -> 0 -> conflict missed -> condemned) CANNOT lose a protection, because a season with
`plex_rating_key is None` gets Unknown dormancy/popularity in build_season_facts and abstains
through MIN_DORMANCY's blocked path regardless. So this is an honesty/UX defect (rules 7/21: a
false operator-facing claim, plus a show stuck in permanent abstain scan after scan), not a
deletion-safety defect — severity low, not medium. Note when fixing: `0` is a legitimate
measured value for a season Plex DID resolve that nobody watched, so the three-state map must
key on presence in `item.seasons_in_plex`, not on the count being 0.

**Fix.** Make the map three-state: emit `None` (or omit the key and have `_detect_conflicts` skip the
pair) for any on-disk season with no resolved Plex key, and skip comparisons where either side
is unmeasured rather than substituting 0. Build the map from the on-disk season set so an
unmeasured season is visibly unmeasured, not silently absent.

### B2-24 · The stale-library-map guard and the unmatched-show log consult imdb candidates that the show resolver never uses, suppressing the warning for a genuinely wrong mapping — **low**

`src/reaper/services/season_scan.py:1124`; also `src/reaper/services/season_scan.py:1173`, `src/reaper/engine/identity.py:1036`

_Violates CLAUDE.md rule 67 (values coupled across two sites derive from one declaration)._

**Failure mode.** `resolve_show` binds shows on `_SHOW_ID_PRIORITY = ("tvdb",)` only, but `gather` hardcodes
`("tvdb", "imdb")` when deciding whether a mapped library is a real candidate. A show whose tvdb
id lives in libraries "TV"/"TV 4K" but whose imdb id also matches a listing in "Anime": the
operator maps the Sonarr root to "Anime" (wrong or renamed). `resolve_show` narrows the tvdb
candidates by "Anime", matches none, ignores the map, and returns AMBIGUOUS — so every
duplicated show under that root is permanently unmatched and kept. But `libraries_for_ids(ids,
tv_index, ("tvdb", "imdb"))` returns "anime" via the imdb id, so the key lands in
`mapped_lib_hits` and the `scan.stale_library_map` warning at line 1184 is skipped for it. The
operator is never told the mapping is broken. The same mismatch makes the `candidate_libraries`
field of the `scan.plex_unmatched` warning (line 1173) list libraries the resolver would never
bind through, pointing the operator at a fix that cannot work.

**Verifier's correction.** Real but strictly diagnostic: the block is explicitly 'Advisory only — it never degrades the
scan or changes a verdict', so no file is at risk and no verdict moves. The reviewer's worked
example is more convoluted than it needs to be; the plain trigger is any Plex show listing
carrying the show's imdb id but not its tvdb id (common for shows matched by the Plex TV Series
agent, which often yields tmdb+imdb and no tvdb) sitting in the mapped library. Where the two id
kinds name the same listings, the sets are identical and nothing is observable. Fix as proposed
-- export `_SHOW_ID_PRIORITY` / `_MOVIE_ID_PRIORITY` from engine.identity and pass the constant
at both call sites — and note that the two call sites are arguably different questions: the
stale-map guard (libraries_for_ids) MUST use the resolver's priority, while the unmatched-log
`candidate_libraries` field being deliberately broader would at least be defensible; make it
consistent with the movie path either way rather than leaving a hand-typed tuple.

**Fix.** Export `_SHOW_ID_PRIORITY` / `_MOVIE_ID_PRIORITY` from `engine.identity` and pass those
constants at both call sites instead of re-typing the tuple, so the diagnostic can never
consider an id kind the resolver does not bind on.

### B2-25 · Changing the keep-tag match (any/all), or removing an *arr instance, leaves the old protection-list slug enabled and still protecting forever — **low**

`src/reaper/services/snapshot.py:1935`; also `src/reaper/services/lists.py:215`, `src/reaper/services/lists.py:611`

**Failure mode.** `ArrTagRule.slug` is `f"{service}{instance}-keeptags-{match}"`, so the slug changes when the
operator flips "Keep tags: match ANY" to "match ALL" in the policy editor (`keep_tags_match`,
wired through scan_runner.py:655/657). `sync_protection_lists` then syncs the NEW slug
`radarr-1-keeptags-all`, but nothing ever deletes or disables the old `radarr-1-keeptags-any`
row — there is no `UPDATE protection_list SET enabled = 0` anywhere in the tree.
`load_membership_index` joins `WHERE l.enabled = 1`, so every title that matched ANY tag stays
whitelisted indefinitely: the tightening the operator saved never takes effect, and the why-
panel cites a keep rule ("Radarr tag: a or b") that no longer exists in the policy. The same
happens when an *arr instance is deleted — its per-instance keep-list keeps protecting titles
from a server Reaper no longer reads.

**Verifier's correction.** Severity lowered to low: this fails toward KEEPING files, the sanctioned direction, so it is a
correctness/honesty defect, not a deletion risk. Two triggers are stronger and more common than
the match flip the reviewer named, and the fix must cover them: (1) clearing the keep tags
entirely — snapshot.py:1893-1918 only builds an ArrTagRule `if movie_keep_tags:` / `if
tv_keep_tags:`, so an emptied tag list means no sync at all and the whole stored keep-list stays
enabled and in force; (2) renaming the Plex collection, since `PlexCollection.slug` is derived
from `collection_name` (lists.py:306-307). Deleting an *arr instance is the third. Note the
table lives in cache.db (scan_runner.py:650 passes `cache_engine`), which is disposable, so an
operator can clear it by deleting cache.db — that bounds the impact but is not a fix. Prefer the
reap-orphans pass over dropping `match` from the slug: the per-instance slug component is load-
bearing (lists.py:198-204).

**Fix.** In `sync_protection_lists`, after the gather, disable every `protection_list` row whose slug is
not in the set the run just produced for that provider family (e.g. `UPDATE protection_list SET
enabled = 0 WHERE slug LIKE '%-keeptags-%' AND slug NOT IN (:current)`), or drop `match` from
the slug and let the atomic swap replace the one list's membership. Cover it with a test that
flips any->all and asserts the previously-tagged title is no longer whitelisted.

### B2-26 · A degraded snapshot is un-plannable but still starts grace clocks and drives the Leaving Soon shelf / Discord announcement — **low**

`src/reaper/services/snapshot.py:913`; also `src/reaper/services/scan_runner.py:719`, `src/reaper/services/grace.py:92`, `src/reaper/services/snapshot.py:1309`

_Violates CLAUDE.md rule 2, 4._

**Failure mode.** `planner.build_plan` is the only consumer that checks `snapshot.degraded`. The scan itself calls
`record_first_flagged_bulk` unconditionally, and `run_scan` calls `leaving_soon.after_scan`
unconditionally right after commit. So a degraded run (a stale IMDb dataset, a keep-list that
failed to sync, a stalled watch-history ingest) writes a `FirstFlagged` row — starting the
grace countdown — for every item its untrustworthy evidence condemned, labels those items in
Plex, and posts a Discord "leaving soon" message naming files that a healthy scan would have
kept. Nothing ever deletes those clocks again: the only delete path is
`api/whitelist._sync_grace_clocks`, which runs on an override change, not on a scan. Because
`_apply_first_flag` restarts a clock only when the gap since `last_seen_condemned_at` exceeds a
whole grace window, an item that is legitimately condemned within `grace_days` of the degraded
run inherits the degraded run's `first_flagged_at` and serves a shortened — possibly already-
expired — grace window, going straight into `grace_report.ready` with no countdown and no
Leaving Soon lead time.

**Verifier's correction.** Correct the mechanism before fixing: grace is NOT an execution interlock. grace_report has
exactly two consumers, both in leaving_soon.py (399, 504) — the planner and executor never
consult it, and the planner's degraded refusal already blocks deletion from the bad snapshot. So
the reviewer's 'straight into grace_report.ready with no countdown' is about losing the Leaving
Soon warning lead time and the Plex shelf label, not about becoming deletable. Real harm is
therefore: (a) Plex 'Leaving Soon' labels + Discord announcements naming files condemned on
evidence Reaper itself declared untrustworthy, and (b) a shortened or spent warning window on a
later legitimate condemnation (worst case: the dataset is stale for a run of consecutive
degraded daily scans, each refreshing last_seen_condemned_at so the clock never restarts, and
the first healthy scan finds the window already expired). Both proposed guards are in the safe
direction — skipping the clock write on a degraded run can only ever cause an extra restart,
i.e. more grace.

**Fix.** Skip the grace-clock write and the Leaving Soon reconcile/announce when `context.degraded` is
true (or gate them on `not snapshot.degraded` in `run_scan`), and either leave
`last_seen_condemned_at` untouched on a degraded run or record the degraded flag on the clock so
`_apply_first_flag` does not treat a degraded condemnation as continuous presence on the reap
list.

## 2. Hacks and workarounds

**No new findings — and that is a real result, not a gap in the review.** A sweep of the whole
backend for `TODO`, `FIXME`, `XXX`, `HACK`, `WORKAROUND`, `kludge`, `temporarily`, and `for now`
returns exactly two hits, and neither is a workaround:

- `src/reaper/api/routes.py:1498` — "A hand reap the engine will not honor yet is KEPT for now (a
  held reap)" — describes intended, documented behavior (CLAUDE.md rule 49), not a shortcut.
- `src/reaper/services/grace.py:8` — "and, for now, deliberately unbuilt" — an explicit,
  reasoned decision to not build something, which is the opposite of a hack.

Ten `except Exception` handlers exist backend-wide and each was read; the ones that swallow
without degrading are filed as bugs in section 1 (B2-12, B2-22, B2-26) rather than as hacks. The
first pass's H-1 (a degraded source detected by substring-matching a free-text reason) and H-2 (an
unwired second decision function that can produce CONDEMN outside `decide_verdict`) both still
stand and are still the two genuine workarounds in this codebase — see Part II.

## 3. Refactor opportunities

**No new findings that clear the "only flag if the benefit is meaningful" bar.** Every duplication
this pass surfaced turned out to have a live correctness edge, so it is filed as a bug where the
fixing agent will actually act on it rather than as a cleanup that can be deferred: B2-5 and B2-7
are the identity resolver's two tiers diverging, B2-12 is one `Absent`/`Unknown` distinction
implemented twice, and B2-3 is one `media_types` filter applied in one direction only. T-2 is the
one true duplication finding — a hand-rebuilt copy of the scan's judging pipeline that has already
drifted — and it lives in section 8 because the duplicate is in `tests/`.

The first pass's R-1 (`_OBS_FIELDS` as a hand-maintained parallel list of `Facts` fields), R-2
("days since reference" derived two ways) and R-3 (the restore auth-purge list with no drift guard)
all still stand. Fix those; this pass found nothing to add to them.

## 4. Performance

### P2-1 · /api/policy/simulate re-runs the whole scoring engine synchronously over every candidate inside the request handler, freezing the entire API while the operator drags a policy slider — **low**

`src/reaper/api/routes.py:1450`; also `src/reaper/api/routes.py:1267`, `src/reaper/api/routes.py:1418-1433`, `src/reaper/services/snapshot.py:759`

**Failure mode.** Any edit to a weight, rating rule, custom rule, or protect condition changes `scoring_hash` but
not `evidence_hash`, so `simulate` takes the tier-2 branch and calls
`_replay_simulation(list(rows), body, decisions)`. That is a plain `def` with no `await`
anywhere: for every candidate of the policy's media type it does `json.loads(row.facts_json)`,
`facts_from_dict`, `evaluate_all`, and `score`. `rows` is a full ORM `select(Candidate)`
(explanation_json + facts_json + summary + genres_json per row), materialized before the loop,
and the DB session stays open for the whole thing. On a library with tens of thousands of
candidates this holds the single event loop for seconds. The policy editor fires this on a 250
ms debounce (`PolicyEditor.tsx:1470-1480`), so dragging a weight slider queues one full-library
engine replay after another, and every other request — `/api/scan/status` polling, the review
queue, `/api/auth/me` — stalls behind them.

**Verifier's correction.** Severity corrected medium -> low, based on a measurement rather than an estimate: I benchmarked
the per-row replay body (facts_from_dict + evaluate_all + score under DEFAULT_MOVIE_POLICY,
1,072-byte facts_json) at 32 us/row on this machine — 0.32 s of blocked event loop per 10k
candidates, ~0.16 s at 5k. Real jank on a slider drag, not an outage. The reviewer's 'seconds'
is an overstatement of the engine loop itself; note that the SQLAlchemy materialization of full
Candidate ORM entities (explanation_json + facts_json + summary + genres_json per row) is likely
the larger share and happens on the tier-1 threshold path too (routes.py:1476 onward, which also
json.loads via `_fired_gates` per protect row). So if this is fixed, fix the row load (load_only
the columns actually read) and yield in BOTH loops, not just the replay branch — otherwise the
stall largely remains.

**Fix.** Make `_replay_simulation` async and `await asyncio.sleep(0)` every N rows the way `snapshot.py`
does, or run it in a thread via `asyncio.to_thread` after loading the rows. Also narrow the row
load to the columns the replay actually reads (`media_key`, `facts_json`, `size_bytes`,
`verdict`, `title`, `year`) instead of full ORM entities, and close the session before the CPU
phase.

### P2-2 · Every candidate row on the review queue's hot path parses the same explanation JSON three or four times — **low**

`src/reaper/api/routes.py:750`; also `src/reaper/api/routes.py:501`, `src/reaper/api/routes.py:545`, `src/reaper/api/routes.py:638`, `src/reaper/services/condemned.py:54`

**Failure mode.** `_candidate_out` calls `_dormant_for(r.explanation_json)`, `_primary_reason(r.explanation_json,
...)` and `_chip(r.explanation_json, ...)`, each of which independently runs `json.loads` on the
same multi-KB string; a row with a reap override adds a fourth parse via `reap_is_effective` ->
`reap_override_verdict`. `GET /api/candidates` returns up to 500 rows per page, so one page
request performs 1500-2000 redundant JSON parses of documents that are several KB each. The same
triple parse also runs for every season in `GET /api/groups/{group_key}`, which returns a whole
show unbounded.

**Verifier's correction.** Severity is correctly 'low' but the framing overstates the cost — I measured json.loads of a
realistic 4.1 KB explanation at 13.6 us, so a full 500-row page carries roughly 14 ms of
redundant parsing. That is a cleanliness/efficiency nit, not a hot-path problem; do not let it
justify a risky refactor. Two corrections to the reviewer's claims: (a) `group_detail`
(routes.py:900) is bounded by one show's season count, not 'unbounded'; (b) if this is touched,
do it together with the _primary_reason hardening finding, since both edit the same three
helpers' signatures — and keep the decoded value guarded with isinstance(dict) at the single
parse site so passing a decoded object cannot make the helpers LESS defensive than they are
today.

**Fix.** Parse once in `_candidate_out` (guarding for a non-dict result) and pass the decoded dict into
`_dormant_for`, `_primary_reason`, and `_chip`; give `reap_override_verdict` a variant that
accepts an already-decoded explanation so the reap path reuses the same parse.

## 5. Production readiness

### PR2-1 · execute() and _send_for_real have no catch-all, so a non-mapped exception after a file is deleted leaves the run stuck in EXECUTING with no report and the terminal step left SENT — **medium**

`src/reaper/clients/plex.py:554`; also `src/reaper/services/executor.py:1768`, `src/reaper/services/executor.py:1375`, `src/reaper/clients/plex.py:567`, `src/reaper/api/runs.py:497`

_Violates CLAUDE.md rule 26._

**Failure mode.** Plex restarts or the network blips after `PlexClient._connect()` already cached a server. The
movie's Radarr delete succeeds, `gone` is True, and `_best_effort_refresh` calls
`plex.section_paths()` — the one PlexClient read with no error wrapper — which raises a raw
`requests.exceptions.ConnectionError`. `_best_effort_refresh` catches only `PlexError`, so it
escapes; `_send_for_real` catches only IntegrationError / SafetyViolationError / ExecutionError,
so it escapes; `execute()` catches only ExecutionError and CancelledError, so it escapes.
`_mark_verified` (executor.py:1528) never runs, the step stays SENT, `run.state` is left
EXECUTING and the `finally` at executor.py:882 commits that state permanently. `execute()`
returns no RunReport, so `_reap()` lands in the generic handler and sets `status.report = None`
-- the operator sees only an error string and can never see which files were actually removed.
Nothing in the codebase reconciles an EXECUTING run (`grep -rn EXECUTING src/reaper/` matches
only models.py and executor.py), and `execute()` refuses anything but PLANNED, so the run row is
wedged forever with `aborted_reason` NULL.

**Verifier's correction.** Real; mechanism is right, but the fix order matters. The root cause is the missing error mapping
on `PlexClient.section_paths` (clients/plex.py:554-568) — it is the one Plex read that does not
wrap `to_thread(read)` in PlexError — plus `_best_effort_refresh`'s PlexError-only handler at
executor.py:1768 contradicting its own "Never fatal" docstring. Fix those two first (cheap,
local); the catch-alls in `_send_for_real` and `execute()` are defense in depth and should
funnel through `_fail` / record ABORTED respectively. Two additions the reviewer did not note:
(a) `_best_effort_refresh` is called from `_send_movie` only (executor.py:1506) — the season
path never refreshes Plex, which is already recorded as B-6 in docs/CODE_REVIEW.md, so the movie
path is the whole exposure here; (b) the wedged item's terminal step stays SENT, which produces
the same rolling-30d undercount as candidate 3 — fix both with one durable removal marker.

**Fix.** Add a final `except Exception as exc:` to `Executor.execute` that records the run as ABORTED
with the exception text (mirroring the CancelledError branch) before re-raising or returning the
report, and add a catch-all in `_send_for_real` that funnels through `_fail` so one item's
surprise cannot escape the loop. Separately, wrap `PlexClient.section_paths`'s `to_thread(read)`
in the same `except Exception -> PlexError` the sibling methods use, and widen
`_best_effort_refresh` to `except Exception` since it is documented as never fatal.

### PR2-2 · An API key can be rotated but never removed: clear_api_key exists with no route and no caller, so the header credential lane cannot be switched off — **low**

`src/reaper/services/app_settings.py:339`; also `src/reaper/services/app_settings.py:339`, `src/reaper/api/middleware.py:77`, `frontend/src/api.ts:1264`

_Violates CLAUDE.md rule 38._

**Failure mode.** The General settings surface exposes only `GET /api/settings/general/api-key` (reveal) and `POST
/api/settings/general/api-key` (generate/rotate). There is no DELETE. An operator who generated
a key for a one-off script, or who wants to close the header-credential lane entirely after
deciding it is too broad (it can write the policy and the reap profile — see the profile
finding), has no way to do so: rotating replaces the key but leaves the lane open with a new
working credential. `app_settings.clear_api_key` was written for this and is dead code — zero
callers in `src/` or `tests/` — which is exactly the stockpiled-safety-adjacent-code shape rule
38 forbids.

**Verifier's correction.** Severity correctly low; it is a production-readiness / dead-code gap, not a live vulnerability
(a key only exists if the operator generated one). Two corrections for the fixer: (1) the POST
route's own docstring frames the design as 'rotation IS revocation, there is nothing to clean
up' — so the honest finding is 'no way to turn the lane OFF', not 'no way to revoke a specific
key'; whichever direction is chosen, that docstring and the middleware comment block must be
updated in the same change. (2) The reviewer's fix note about fencing a DELETE from the key is
unnecessary: `_api_key_allowed` (middleware.py:104-111) is deny-by-default for all non-safe
methods, so a new DELETE is closed to the key automatically. What the fix DOES need is clearing
`request.app.state.api_key_digest` (set at main.py:135 on boot and settings.py:1547 on rotate) —
otherwise the deleted key keeps authenticating until restart. If revocation is not wanted,
delete `clear_api_key` instead; either resolution satisfies rule 38.

**Fix.** Add `DELETE /api/settings/general/api-key` that calls `app_settings.clear_api_key`, clears
`request.app.state.api_key_digest`, and is fenced from the API-key lane like the reveal route
(add its path to `_API_KEY_READ_DENY`'s write-side equivalent so a key cannot delete itself or
another). Wire a Remove control beside Generate. If revocation is genuinely not wanted, delete
`clear_api_key` instead.

### PR2-3 · `api_path_prefix` is a column no code path ever writes, while the connection-test docstring claims Reaper version-gates the API path off the status probe — **low**

`src/reaper/services/instances.py:566`; also `src/reaper/clients/arr.py:58`, `src/reaper/services/instances.py:543`, `src/reaper/db/models.py:74`

_Violates CLAUDE.md rule 7, 24._

**Failure mode.** `test_connection`'s docstring says the *arr status endpoint "doubles as the version probe
(Reaper version-gates its API path off it)", and `ArrClient.system_status` calls itself "The
basis of the version gate" — but `Instance.api_path_prefix` (db/models.py:74, default
"/api/v3") is read in four places (scan_runner.py:254/272/359/369, instances.py:250) and written
in ZERO. `test_saved_instance` stores `result.version` into `detected_version` (line 621) and
nothing consumes it. There is no route, service, or migration that sets `api_path_prefix`, and
the field is surfaced read-only to the browser (api/settings.py:115, api.ts:944). Concretely: an
operator on a Sonarr/Radarr that does not serve `/api/v3` gets a 404, `_explain_failure` tells
them "there is nothing at this address. Check for a missing or extra path at the end of the URL"
(line 498-502), and there is no way to act on that advice. Separately, `_client` (line 543-557)
builds the arr client WITHOUT `api_path_prefix` while `scan_runner` passes
`row.api_path_prefix`, so if the column ever did diverge from the default, Test Connection would
validate a path the scan never uses.

**Verifier's correction.** Two of the reviewer's supporting claims are WRONG and should not be carried into the fix. (1)
'detected_version ... nothing consumes it' is false — it is rendered in the UI at
frontend/src/components/Settings.tsx:691. Only api_path_prefix is dead; leave detected_version
alone. (2) 'there is no way to act on that advice' is false — base_url is passed straight to
httpx2.AsyncClient (clients/base.py:196-202), which merges a base path into every request, so a
reverse-proxy subpath (http://host/sonarr) IS fixable by editing base_url; what is unfixable is
a non-v3 API version, which is the narrower true statement. So the defect is documentation-vs-
implementation plus a latent test/scan path divergence, not an operator dead end. Given the
column has never held anything but its default, correcting the three comments and passing
api_path_prefix in instances._client is the proportionate fix; deleting the column is not
possible under the additive-migration rule.

**Fix.** Either wire it (have `test_saved_instance`/`test_connection` derive and persist the prefix from
the reported version, and pass `api_path_prefix=row.api_path_prefix` in `instances._client` so
the test and the scan hit the same path), or delete the dead column reads and correct both
docstrings in the same change per rule 24.

### PR2-4 · `build_reap_gateway` constructs httpx clients into a plain list, so a raise part-way through leaks every client already built — **low**

`src/reaper/services/scan_runner.py:349`; also `src/reaper/api/runs.py:404`, `src/reaper/services/scan_runner.py:257`

_Violates CLAUDE.md rule 34._

**Failure mode.** Each `RadarrClient`/`SonarrClient`/`TautulliClient`/`PlexClient` constructor allocates an
`httpx2.AsyncClient` (base.py:201). `build_reap_gateway` appends them to a local `closers` list
and only hands ownership to the caller on the final `return`. If `box.decrypt(row.api_key_enc)`
raises on the third instance row (a re-keyed or corrupted `api_key_enc` raises `InvalidToken`),
or the `PlexClient(...)` construction after the loop raises, the function propagates and the
already-constructed clients are never returned and never closed — `api/runs.py:410-419` only
resets the run status; it has no reference to `closers`. Each leaked client is an unclosed
connection pool for the life of the process. `build_sources` in the same file solves exactly
this by entering every client into the caller's stack the moment it exists, and its docstring
cites rule 34.

**Verifier's correction.** One factual correction to the reviewer's mechanism: `PlexClient.__init__`
(clients/plex.py:451-469) does NOT allocate an httpx client — it only stores fields and two
locks, and builds the plexapi session lazily in `_connect`. So the post-loop PlexClient
construction is not a raiser and a leaked PlexClient costs nothing; the leak window is exactly
the BaseClient subclasses (Radarr/Sonarr/Tautulli) built in the loop, and the realistic raiser
is `box.decrypt` on a row whose `api_key_enc` is corrupt or written under a different key (a
wholesale re-key fails on the first row, before anything is constructed, and leaks nothing).
Prefer the AsyncExitStack-parameter fix so it matches build_sources rather than a try/except
BaseException.

**Fix.** Give `build_reap_gateway` an `AsyncExitStack` parameter like `build_sources` has, entering each
client as it is constructed, and have `api/runs.py` own that stack; or wrap the body in
`try/except BaseException` that closes everything already in `closers` before re-raising.

## 6. Security

### S2-1 · _RingHandler.emit re-appends the RAW, unredacted log message whenever exc_info is set, defeating its own query-string credential scrubbing — **medium**

`src/reaper/logging.py:183`; also `src/reaper/logbuffer.py:97`, `src/reaper/logbuffer.py:148`

_Violates CLAUDE.md rule 7, 13, 24._

**Failure mode.** `emit` builds `text` from `_redact_str(record.getMessage())`, then, when the record carries an
exception, appends `self.format(record)`. `_RingHandler` sets no formatter, so `Handler.format`
falls back to `logging._defaultFormatter` (`"%(message)s"`), which renders `record.getMessage()`
UNREDACTED and appends the traceback. Every stdlib record with `exc_info` therefore lands in the
ring — and, via `LogRing.append` -> `_FileSink.write`, in `<data_dir>/logs/reaper.log` and the
Logs-tab download — with its message twice: once scrubbed, once in the clear. Any stdlib logger
that reports a request URL in its message alongside an exception (Tautulli, MDBList and Plex all
carry their key in the query string; the module docstring calls this out as the exact reason
redaction exists) writes the credential to disk. Even with no credential, the operator sees
every exception line duplicated in the Logs tab.

**Verifier's correction.** The reviewer's mechanism is right but understates it in one way worth passing on: the TRACEBACK
ITSELF is also never passed through `_redact_str`. That is the more reachable leak, because
`httpx2.HTTPStatusError`'s str() embeds the full request URL — so any stdlib logger that reports
an *arr/Tautulli/MDBList HTTP error with exc_info writes the query-string key to disk even when
the log MESSAGE is clean. The proposed fix (`_redact_str` over the formatted exception, appended
once) covers both halves and is correct. Severity medium is right. Line 183 is accurate;
logbuffer refs are 90-99 (append) and 148 (write), not 97/148 exactly.

**Fix.** Do not re-render the message. Format only the exception: replace `self.format(record)` with
`_redact_str("".join(traceback.format_exception(*record.exc_info)))` (or set a formatter and use
`_redact_str(self.formatException(record.exc_info))`), so the traceback is appended once and
passes the same scrubber. Add a test asserting a record with `exc_info` yields exactly one
redacted copy of the message.

### S2-2 · wait_for_pin sleeps for an uncapped, server-supplied Retry-After, so a hostile or misbehaving plex.tv response hangs `reaper-admin link-plex` far past its 5-minute deadline — **low**

`src/reaper/clients/plextv.py:248`; also `src/reaper/clients/base.py:92`, `src/reaper/services/plex_link.py:196`

_Violates CLAUDE.md rule 10._

**Failure mode.** `wait_for_pin` checks its deadline only at the top of the loop (`while loop.time() < deadline`)
and then sleeps `exc.retry_after or PIN_RATE_LIMIT_BACKOFF`. `_retry_after_seconds` parses the
header as `max(0.0, float(raw))` with no upper bound. A 429 carrying `Retry-After: 86400` --
from a rate-limiting edge in front of plex.tv, a captive-portal/proxy interposed on the LAN, or
a DNS-hijacked `plex.tv` — makes `reaper-admin link-plex` sit on `await asyncio.sleep(86400)`
with the terminal stuck on "Waiting...", never honoring `PIN_TIMEOUT = 300` and never returning
the documented "Sign-in was not completed in time. Nothing was saved." The codebase already
knows this pattern: `notify/discord.py` caps the same header at `_MAX_RETRY_AFTER = 5.0` for
exactly this reason ("a webhook that asks us to sleep for minutes must never stall a
scan/plan/run"); plextv is the inconsistent site.

**Verifier's correction.** Real, but correct the blast radius the reviewer implies — it is smaller than 'the app'.
`wait_for_pin` has exactly ONE caller: services/plex_link.py:196, reached only from cli.py:177
(`reaper-admin link-plex`). The WEB link and login flows do not use it — plex_link.py:469
(`poll_link`) and services/login.py:184 both call `check_pin` directly and are browser-driven,
so no request handler and no DB lock is held (plex_link.link's docstring at 168-177 explicitly
closes the session before the wait). So the worst case is an interactive admin command sitting
on a sleep until Ctrl-C: no data loss, no deletion-path exposure, nothing stuck in a bad state.
Low severity is right; it does not deserve promotion. Also note the 'hostile plex.tv / DNS
hijack' framing is weak (the client verifies TLS, so a MITM cannot inject the header) — the
honest justification is simply 'do not let an external server set an unbounded sleep', the same
reason discord.py caps it. The proposed fix is correct; the `min(..., max(0.0, deadline -
loop.time()))` term is the load-bearing part since it makes PIN_TIMEOUT actually binding.

**Fix.** Clamp the sleep to what is left of the deadline and to a sane ceiling: `await
asyncio.sleep(min(exc.retry_after or PIN_RATE_LIMIT_BACKOFF, PIN_RATE_LIMIT_BACKOFF * 4,
max(0.0, deadline - loop.time())))`, so the 5-minute PIN_TIMEOUT is always honored.

## 7. Improvements

### I2-1 · MinDormancyGate's docstring tells the operator its dormancy curve is derived from their own watch history at calibration time, but engine.calibration.derive has no production caller anywhere — **low**

`src/reaper/engine/gates.py:469`; also `src/reaper/engine/calibration.py:161`, `src/reaper/engine/backtest.py:25`, `src/reaper/engine/gates.py:483`

_Violates CLAUDE.md rule 7, 24 (a comment naming a safeguard must cite its implementing function, and you must verify that function is called)._

**Failure mode.** An operator (or an agent) reading MinDormancyGate believes the 1,095/1,825-day cliff positions
were fitted to this server and that a mis-set threshold will be corrected by calibration.
Nothing fits anything: `MinDormancyGate.evaluate` reads `self.config.threshold` straight off the
stored GateConfig, and `calibration.derive` is imported only by `engine/backtest.py`, which
itself has no route, CLI or scheduler caller. The threshold is therefore always the operator's
raw number with no curve behind it, and the documented "falls back to a documented default curve
when there is not enough history" branch does not exist in any code path.

**Verifier's correction.** Two mechanism corrections for the fixer. (1) The claim 'the documented fallback branch does not
exist in any code path' is WRONG: the fallback curve exists as `backtest.FALLBACK_REWATCH_PRIOR`
+ `backtest.rewatch_prior()` (backtest.py:93-113). It is just unreachable in production. (2) The
deeper falsehood is one the reviewer missed: calibration derives a bucketed REWATCH PRIOR used
as the backtest's lift baseline, not the gate's threshold — so even if `derive` were wired, it
would still not set `MinDormancyGate`'s number. The rewrite must say the cliff positions are
documented defaults and the threshold is the operator's own setting, not merely 'calibration is
not wired yet'. Same change should fix backtest.py:79-80 ('The real prior is DERIVED from the
owner's own history at scan time'), which is false in the same way and sits outside backtest's
own 'Engine-complete, not yet reachable' note at line 25-28. calibration.py's header (1-28)
indeed carries no unreachability note.

**Fix.** Rewrite the gates.py:469-473 paragraph to state what is true today — the cliff positions are
documented defaults measured elsewhere and the threshold is the operator's own number — and
drop the `see engine.calibration` citation until derive() has a caller. Add the same "not yet
reachable" note calibration.py lacks, mirroring backtest.py:25-28, so the next reader does not
re-derive the same false claim.

### I2-2 · 37 comments across the backend and frontend cite engineering rules 70-87, but CLAUDE.md's rule list ends at 69 — every one of those citations is unverifiable — **low**

`src/reaper/secrets.py:69`; also `src/reaper/logbuffer.py:135`, `src/reaper/clients/plex.py:373`, `src/reaper/api/backup.py:116`, `src/reaper/api/whitelist.py:119`, `src/reaper/services/fairness.py:23`

_Violates CLAUDE.md rule 7, 24._

**Failure mode.** Rule 24 requires that a comment naming a safeguard cite something a reviewer can go and verify.
Grepping `src/` and `frontend/src` for `(rule N)` / `(rules N/M)` yields the distinct numbers 2,
3,4,6,7,12,13,14,17,18,19,21,22,23,24,28,29,30,32,33,34,36,37,39,44,45,46,48,49,50,51,56,57,59,6
1,65,66,67 — all present — plus 70,71,72,73,74,75,76,77,78,80,82,83,84,85,87, none of which
exist anywhere in the repo (CLAUDE.md's last numbered rule is 69, `grep -c` = 541 lines, tail
ends at rule 69; no docs/*.md defines 70+). An agent or reviewer following `secrets.py:69`'s
"provenance follows runtime precedence, not file existence (rule 76)" has nothing to check the
claim against, and a future change that violates the intent behind rule 76/82/85 cannot be
caught by reading the rulebook. 37 citation sites are affected.

**Verifier's correction.** Real but it is a documentation-integrity finding, not a code defect — no runtime behavior is
affected, and low severity is correct. The fix belongs in CLAUDE.md (restore or renumber rules
70-87), NOT in the backend source files: rewriting 37 citations to point at existing rules would
destroy whatever intent those comments were pinning. Correct the reviewer's number set to
include 86. Note the citations are internally coherent and repeated (rule 82 appears 4x for the
log-sink degradation flag, rule 76 for secret-key provenance, rule 72 for the single paging
loop), which is strong evidence the rules were written and then lost from CLAUDE.md rather than
invented — git log on CLAUDE.md shows the last docs merge was 5229ce0 'merge the third review
pass's agent rules', so a fourth-pass block is likely missing. Recover it from history or from
docs/CODE_REVIEW.md before renumbering anything.

**Fix.** Restore rules 70-87 to CLAUDE.md (they were clearly written and then lost from the file), or
renumber/reword every citation to a rule that exists. Add a CI check that greps `src/` and
`frontend/src` for `(rule N)` and fails when N exceeds the highest number defined in CLAUDE.md.

### I2-3 · _row_timestamp's docstring says an unreadable timestamp reads as "no evidence of a play", but the caller treats it as a play and spares the item — **low**

`src/reaper/services/executor.py:2014`; also `src/reaper/services/executor.py:1343`

_Violates CLAUDE.md rule 7, 24._

**Failure mode.** The docstring describes the fail-OPEN reading of a None return while the code is fail-CLOSED. A
maintainer reconciling the two in the direction the docstring states — e.g. changing the caller
to `if played_ts is not None and played_ts >= approved_ts` — would silently disable the played-
since-approval interlock for every Tautulli history row whose `stopped`/`date` cannot be parsed:
the row survived the `after` filter, so it may well be a post-approval play, and the item would
be deleted instead of spared. The docstring is the only description of the contract at the call
site.

**Verifier's correction.** Real, but be explicit with whoever fixes it: there is NO runtime defect today — the code is
fail-closed and the tests pin it. This is a documentation-correctness finding whose only failure
mode is a future maintainer 'fixing' the caller to match the docstring, which would silently
disable the interlock for unparseable Tautulli rows. Fix is one sentence: None means 'no
readable time', which `_watched_since_approval` (executor.py:1287+) treats as a possible post-
approval play and spares on. Do not touch the caller.

**Fix.** Rewrite the last sentence of `_row_timestamp`'s docstring to say that None means "no readable
time", which the caller treats as a possible post-approval play and spares on — naming
`_watched_since_approval` as the consumer so the contract cites its implementer.

## 8. Test suite

This section is an addition to the seven requested categories. It is kept separate because the
findings are about what the suite *fails to hold still*, not about a defect in `src/`, and folding
"the byte-cap tripwire has no test" into Production readiness would bury it. The suite is
otherwise in good shape: 2044 tests, 63s, no skips outside one `getuid()` guard, and the
deletion-path interlocks are — with the exceptions below — genuinely covered rather than
nominally covered. The reviewer confirmed real coverage for the manifest re-hash abort, both cap
kinds aborting rather than truncating, the confirmation-phrase recompute (route *and* planner
level), the per-item streaming veto including its fail-closed arms, the played-since-approval
check, the size interlock at plan and at send, the grace-clock reset on re-entry, the
`GuardedTransport` undeclared-mutation refusal, journal durability across a crash, and the atomic
`EXECUTING` claim. Do not spend time re-checking those.

### T-1 · The `POST /api/runs` selection path has no route-level test at all, including the `None`-vs-`[]` fail-closed conversion — **critical (coverage)**

`src/reaper/api/runs.py:177-179`; every run-creating test posts a bare body —
`tests/test_api.py:327, 344, 355, 365, 428, 442, 466, 484, 529`.

Nothing in the suite ever sends `{"media_keys": [...]}` or `{"media_keys": []}`. The route's
ternary is exactly what golden rule 1 exists to protect:
`only = set(payload.media_keys) if payload is not None and payload.media_keys is not None else None`.
Rewriting that as the "obvious" `if payload and payload.media_keys else None` converts "nothing
selected" into a plan over the **whole condemned set**, and the suite stays green. The planner's
side of the contract *is* covered (`tests/test_review_reap.py:255-263` passes
`only_media_keys=set()` to `build_plan` directly) — but the route→planner conversion, the only
place `[]` is translated, is not.

**Fix.** Three route-level cases against the existing `client` fixture: `{"media_keys": []}` →
422 "No items were selected"; `{"media_keys": null}` → plans the whole set; a one-key list → plans
exactly that key.

### T-2 · `tests/_policy_lab.py`'s hand-rebuilt judging pipeline has already drifted from production — **high**

`tests/_policy_lab.py:191-200` (its own docstring at `:163` calls itself "Mirror of
`services.snapshot._judge_item`"); sole entry point for the 440-vector drift trip-wire at
`tests/test_policy_permutations.py:147`.

Two divergences, both safety-relevant. It calls `decide_verdict(...)` **without**
`blocked_holds_reap`, where production passes
`blocked_holds_reap=reap_held_by_blocks(evaluation.results)` (`services/snapshot.py:1172`) — so for
a vector carrying `override="reap"` plus the keep-rule-conflict block the lab itself synthesizes at
`_policy_lab.py:150-152`, production condemns and the lab protects. And it applies the hand
override straight through `decide_verdict(override=...)`, where production stores the *pure-policy*
verdict (`snapshot.py:1059`, `override=None`) and derives the effective fate via
`reap_override_verdict` off the frozen explanation (`snapshot.py:1125-1142`). Latent today only
because the committed fixture happens to contain no `reap` overrides and no `guard: "unknown"`
rows — but `scripts/policy_lab_extract.py:198-206, 271-272` emits both from a real library, so the
next fixture regeneration can pin a *wrong* baseline as ground truth. This is the rule 22 shape.

**Fix.** Extract the pure part of `_judge_item` (everything before the `session.add`) into a
function both call. At minimum pass `blocked_holds_reap` and route the override through the same
`reap_override_verdict` two-step.

### T-3 · The byte-cap regression tripwire has zero coverage, and its own docstring says it is the last line of defense — **high**

`src/reaper/services/executor.py:551-556`.

`_deletable_bytes` raises `ExecutionError` when an unmeasured item reaches it with
`allow_unmeasured=False`, and the docstring states outright: *"This should never fire, and must not
be deleted as unreachable. It is the only thing standing between a future regression in the
planner's filter and a cap that silently stops working."* Nothing tests it — the abort string
appears in no test file, and the branch is unreachable via `execute()` because `_deletable`
(`executor.py:501`) filters those items first. Deleting the `raise`, or softening it to a
`log.warning`, passes the entire suite; the consequence is a byte cap that under-counts and deletes
past what the operator approved.

**Fix.** A direct unit test beside `TestAnApprovedSizeThatWasNeverConfirmed` in
`tests/test_reap_loop.py`: a `_Delete` whose candidate has `size_bytes=None`,
`_deletable_bytes([d], allow_unmeasured=False)` → `ExecutionError`; and the mirror with
`allow_unmeasured=True` asserting the item is **omitted** from the total rather than summed as zero.

### T-4 · `decide_verdict`'s decision tree is transcribed as its own expectation — **medium**

`tests/test_policy_permutations.py:585-618`.

`test_the_decision_matches_its_spec_at_every_boundary` calls the real `decide_verdict` at `:598`,
then computes the expected answer by rewriting the function body inline at `:608-617`
(`expect = "protect" if (blocked or safety) else "condemn"` … `elif score >= condemn_at`). The two
must be edited in lockstep, and a reviewer seeing them agree learns nothing. The transcription is
already incomplete: the matrix never varies `blocked_holds_reap`, the one parameter that can flip a
reap from `protect` to `condemn` under `blocked=True`, while the test name claims "every boundary."
That parameter *is* covered elsewhere (`tests/test_verdict_agreement.py:197-233`), so this is an
honesty gap in the matrix rather than an absolute hole.

**Fix.** Replace the transcribed `expect` block with an explicit `(inputs) -> expected` table
written from the docstring, and add `blocked_holds_reap` to the swept dimensions.

### T-5 · Eight guard pass-through tests assert only "not a `SafetyViolationError`" and depend on a real socket — **medium**

`tests/test_plex_guard.py:39, 95, 103, 110, 119, 127, 200, 208`.

Each proves a write is *allowed* through the guard by issuing a live `requests` call to
`http://127.0.0.1:1/...` and asserting `pytest.raises(Exception)` plus
`not isinstance(caught.value, SafetyViolationError)`. That is unfalsifiable in practice: a
`TypeError` from a bad kwarg, an `AttributeError` from a refactored `GuardedSession`, or a
`ValueError` raised before the transport is reached all satisfy it — so
`test_the_label_writes_when_armed` would still pass against a `GuardedSession` that was broken
outright. It also depends on TCP port 1 on loopback being closed.

**Fix.** Assert the concrete transport failure (`pytest.raises(requests.exceptions.ConnectionError)`)
so a non-transport failure is a red test rather than a pass.

### T-6 · `wait_for_pin` has no test at all, and `conftest.py` implies it has one — **medium**

`src/reaper/clients/plextv.py:227-254`; `tests/conftest.py:50`.

`wait_for_pin` is live production code on the Plex account-link path
(`services/plex_link.py:196`) and is referenced nowhere in `tests/`. Its `429` back-pressure branch
(`plextv.py:243-249`, which honors `exc.retry_after` — and see S2-2) and its deadline-expiry return
are both untested, on the path that handles a full-power Plex account token. Worse, `conftest.py:50`
names "the plex.tv pin-poll loop" as one of the loops the global `asyncio.sleep` patch exists to
speed up, implying coverage that does not exist — and that patch makes the timeout path
*untestable as written*, since `loop.time()` still advances in real wall-clock while `sleep`
collapses to `sleep(0)`, so an unapproved pin would hot-spin HTTP for the full 300s `PIN_TIMEOUT`.

**Fix.** Add `tests/test_plex_auth.py` driving `check_pin` via `httpx2_mock`: a 429 with
`Retry-After` that is retried then succeeds; a token on the third poll; and
`wait_for_pin(pin_id, timeout=0.01)` returning `None`. Correct the conftest docstring.

### T-7 · The only real-filesystem permission test vanishes under root — **medium**

`tests/test_data_dir_preflight.py:66`.

`@pytest.mark.skipif(os.getuid() == 0, ...)` guards `test_real_readonly_directory_is_caught`, the
sole test that exercises a genuinely unwritable directory. Its two siblings (`:34`, `:49`)
monkeypatch `tempfile.TemporaryFile` to raise, so they test the *message*, not the probe. If CI runs
as root in a container — the common default — the module's stated subject ("the single most common
deploy failure", per its docstring) is silently unverified, and the skip is invisible under `-q`.

**Fix.** Keep the skip but make it loud: assert in a companion test that the suite is not running as
root, or drop privileges for this one case. At minimum confirm and document the CI job's UID.

### T-8 · Over-broad `pytest.raises(Exception)` on the protection-list sync path — **medium-low**

`tests/test_lists.py:238, 249`.

Both `test_a_failed_fetch_leaves_the_previous_list_intact` and
`test_the_error_is_recorded_for_the_settings_screen` use `pytest.raises(Exception)  # noqa: B017`
on rule 27 / rule 2 territory — a protection list must never silently empty itself. Any exception
satisfies them, including one raised *before* the atomic swap is reached, which would make the
follow-up assertions cover a path that never ran. The sibling at `:223` does it right
(`pytest.raises(Exception, match="truncated")`).

**Fix.** Name the domain error the sync actually raises and drop the `noqa: B017`.

### T-9 · A test mutates process-global logging with no restore — **low**

`tests/test_foundations.py:118`.

`test_the_httpx_logger_is_quieted_so_it_cannot_leak_urls` calls `configure_logging(level="INFO")`
in the test body with no cleanup. That call (`src/reaper/logging.py:192-220`) sets the root level
via `basicConfig`, adds a `_RingHandler` to the root logger, calls `logbuffer.set_level`, lowers
every `_NOISY_LOGGERS` entry, and re-runs `structlog.configure` — all process-global, all surviving
into whatever test the xdist worker picks up next. Its direct sibling
`tests/test_logging_quiet.py:25-37` has an explicit `_restore_logging` fixture for exactly this.
Nothing is broken today (the autouse `_capturable_logs` fixture absorbs the worst of it); it is an
unpulled pin.

**Fix.** Apply `test_logging_quiet.py`'s `_restore_logging` fixture to this test.

### T-10 · An assertion re-reads the wall clock independently of production — **low**

`tests/test_scan_pipeline.py:981`.

`assert obs.value == float((utcnow().date() - date(2000, 12, 31)).days)` — but `_release_age_days`
(`services/snapshot.py:1505`) calls `utcnow()` itself, so test and production sample the clock at
two different instants. Straddling UTC midnight between them yields an off-by-one-day failure:
microseconds wide, but nonzero under xdist on a slow runner, and it fails once at 00:00 UTC with no
obvious cause. The test is otherwise genuine — it would catch a Jan-1 implementation, which is the
rule 31 property it exists for.

**Fix.** Freeze `reaper.clock.utcnow` for this test and compute the expected value from the same
frozen instant, or pass a fixed `now` into `_release_age_days`.

</content>

## Candidates that did NOT survive verification

Recorded so they are not raised a third time. Each was proposed by a reviewer in this pass and
refuted by an independent verifier reading the same code.

| Area | Candidate that did NOT survive verification |
| --- | --- |
| season-path | Every spare/override click full-scans the entire, never-GC'd candidate table because group_key is unindexed |
| engine | A gate missing from `PolicyBody.gates` is silently not run and cannot be warned about; an empty gates tuple validates and removes every built-in protection |
| engine | `facts_from_dict` raises on any stored `facts_json` written before a `_OBS_FIELDS` entry or a `GateId` value existed, 500-ing the simulator instead of falling back to a fresh scan |
| engine-identity | identity.py's design rationale claims the single production join is reachable from the backtest and the planner; neither module references identity at all |
| api | The API-key lane may write /api/profile, so a header-only credential can turn off the run caps interlock and lower the grace window — contradicting the same block's stated rule that all setting changes stay behind the browser |
| services-misc | A stored instance API key can be shipped to any host by editing base_url and pressing Test, defeating the module's stated write-only invariant |
| infra | PRAGMA synchronous=NORMAL makes the deletion journal non-durable across a host crash, contradicting the durability the ActionStep/StepState docstrings and rule 26 assert |
| infra | Seerr paging advances the cursor by the REQUESTED page size, not by the number of rows the server actually returned, so a clamped or short page silently skips records |

---

# Part II — first pass (`dev` @ `a7d7659`)

> Preserved from the first pass, unedited apart from spelling. **All 53 findings below are still open on this tree.** Their IDs (`B-1`,
> `S-3`, …) are distinct from Part I's (`B2-1`, `S2-1`, …); the two sets never collide.

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

`catch_up = asyncio.create_task(catch_up_on_startup(...))` has no done-callback and is only canceled
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

---

# Agent Rules

Direct, enforceable constraints for the next coding agent. Written as blockers, not suggestions.
They extend the numbered rules in `CLAUDE.md`. **Rules 1–23 are from the first pass and still
stand; 24–41 are new in the second pass.** Where a new rule sharpens an older one, the newer,
more specific obligation governs.

> **Before using these: `CLAUDE.md`'s numbered rules end at 69, but code comments cite rules
> 70–87** (see I2-2). Restore or renumber those before treating a code comment's rule citation as
> checkable.

## From the first pass (1–23)

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

## New in the second pass (24–41)

24. **A stored policy body that gains a new protection-bearing field ships a loader shim in the
    same change, and the shim degrades the scan.** When a field moves out of a gate row and into
    the body (as the rating bars did), a body written before the move must be migrated on load —
    keyed on the raw key being **absent**, never on an explicit `[]` (rule 1), and never on
    `schema_version`, which does not discriminate across a change that did not bump it. The
    migrated body sets the `ActivePolicy.repaired`-style flag so the scan degrades and the editor
    opens on it. A protection that silently evaluates to "nothing configured" is the worst
    outcome this codebase has (B2-1, sharpens rule 65).
25. **Every id the item carries goes into every membership and keep-list lookup, on the movie path
    exactly as on the TV path.** `imdb_id=item.imdb_id` where the item also holds
    `item.plex_imdb_id` is a fail-open protection bug; the call passes `item.imdb_id or
    item.plex_imdb_id`. Adding a new id kind to storage means grepping every lookup call site in
    the same change (B2-6, sharpens rule 29).
26. **A field offered in the policy vocabulary is populated by the fact builder for every media
    type it is offered on.** A `FieldSpec` with no `media_types=` is offered on *both* policies;
    if the season builder hardcodes it `Absent`, restrict the spec to `("movie",)` in the same
    change. This binds both lanes, not just protect: removal weights sum to a fixed 100, so a
    condemn rule on an always-`Absent` field permanently depresses every score in that media type
    rather than merely never firing (B2-3, sharpens rule 35).
27. **A text condition value is rejected at the save boundary when it strips to empty.** `contains
    ""` matches every item and lands the rule's full weight library-wide; `in ""` can never match
    and reports as a green "checked, did not fire". Reject `value.strip() == ""` for CONTAINS/IN
    and reject an IN target whose split yields no elements, so a comma-only list cannot pass
    (B2-4, sharpens rule 32).
28. **An identity tier that can corroborate a bind is computed even when an earlier tier already
    bound — as a cross-check only, never as an originator.** Gate the binding branches so only the
    first id kind may originate; later tiers may only add an abstain. A `tier1 is None` guard in
    front of a corroborating tier makes the documented contradiction veto structurally
    undetectable. A multi-hit tier is silence, not a contradiction, and a hit landing inside the
    earlier tier's merged group is agreement (B2-5, B2-7, sharpens rule 6).
29. **A degraded evidence source produces `Unknown` observations, never `Absent`.** `Absent` means
    "we looked and there is genuinely nothing"; a source that could not be read is `Unknown`,
    which blocks the gate and takes the full keep discount. Degrading the snapshot is necessary
    but not sufficient — it does not stop a library-wide `Absent` from withdrawing the protection
    and printing a why-panel that asserts a check which never ran (B2-12, sharpens rules 2/28).
30. **Every client method maps its failures to the client's domain error type.** One read that
    lets a raw transport exception escape defeats every `except <Domain>Error` in the call chain.
    When a method is documented "never fatal," its handler catches `Exception`, not one mapped
    type (B2-2, sharpens rule 9).
31. **The executor's send loop and `execute()` each carry a catch-all that records terminal
    state.** An unmapped exception after a file is already deleted must not leave the step `SENT`,
    the run `EXECUTING`, and the report `None` with nothing in the tree able to reconcile it.
    Per-item surprises funnel through `_fail`; run-level surprises record `ABORTED` with the
    reason (PR2-1, sharpens rule 26).
32. **Anything that counts what was deleted counts the file's removal, not the success of the
    bookkeeping that follows it.** A step whose file is confirmed gone but whose follow-up
    (exclusion, refresh) failed must still charge the rolling caps. Add a durable
    `file_removed_at`-style column — a nullable `ADD COLUMN` on a new revision — and count on it;
    never mark a step `VERIFIED` whose verification explicitly failed (B2-10, PR2-1, sharpens
    rules 5/30).
33. **The executor re-reads the operator's spare decisions before every item.** A decision map
    loaded once at run start means a Spare clicked during a multi-minute reap is ignored and the
    file is deleted. Refresh only the per-item spare / effective-set checks — cap math stays on
    the run-start set — so the refresh can only ever remove items (B2-9, sharpens rule 2).
34. **A run's approval is bound to the policy it was planned under, or the code stops claiming it
    is.** `run.policy_hash` is recorded and never read at execute time. Either enforce it (and
    ship the operator copy telling them to re-scan, since a policy edit does not trigger one), or
    delete the claim in `planner.py:302-303`. Today the code and its comments disagree (B2-8,
    sharpens rules 7/24).
35. **A log handler never re-renders the raw record.** Everything appended to the ring or the file
    passes the scrubber — message *and* formatted traceback, since an HTTP error's `str()` embeds
    the full request URL. Appending `self.format(record)` after a redacted copy writes the secret
    in the clear and duplicates every line (S2-1, sharpens rule 13).
36. **A sleep, retry budget, or allocation whose size comes from a remote server is clamped.**
    Clamp to a ceiling *and* to the caller's remaining deadline. `notify/discord.py`'s
    `_MAX_RETRY_AFTER` is the pattern; any other site honoring `Retry-After` matches it (S2-2).
37. **A protection-list slug that changes shape disables its predecessor in the same transaction.**
    Slugs derived from operator settings (match mode, instance id) leave orphaned rows that
    `enabled = 1` keeps protecting forever, so the tightening the operator saved never takes
    effect. Either disable every slug not produced by the current run, or keep the setting out of
    the slug (B2-25).
38. **A degraded snapshot's side effects are gated with its plan.** Un-plannable must also mean
    un-announced: grace clocks, the Leaving Soon shelf, and Discord all read the condemned set and
    all currently act on evidence the scan itself declared untrustworthy (B2-26, sharpens rules
    2/8).
39. **A gate or option the operator can enable must be able to fire.** If every fact builder sets
    its input `Absent`, either wire the input or remove the option from `GATE_TYPES` and refuse it
    in `build_gates`; a protection that is built, evaluated, hashed and can never keep a file is
    worse than one that does not exist (B2-15, sharpens rule 38).
40. **Every deletion-path interlock has a test that fails when the interlock is deleted.** Write
    the test against the interlock function directly when the guard upstream makes it unreachable
    through the public path — an unreachable tripwire with no test is one refactor away from
    silently gone. This applies to the route→planner conversion of an empty selection as much as
    to the byte cap itself (T-1, T-3).
41. **A test never re-implements production logic to assert against it, and never asserts on a
    bare `Exception`.** Agreement tests call the real function; expectations are explicit tables
    written from the spec, not transcriptions of the branch structure they are checking. Where a
    test mirrors a production pipeline, extract the shared part instead (T-2, T-4, T-5, T-8,
    sharpens rule 22).
</content>
