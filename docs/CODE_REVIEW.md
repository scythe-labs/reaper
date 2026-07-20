# Diff review — `dev`, changes since `f750744`, 2026-07-19

> **Scope.** The 30 commits and 85 files changed since `f750744` (the fact-layer rework,
> the points/budget change, identity binding for split libraries, the watch-cache rebuild,
> the UI control-grammar pass, and the four new frontend panels). This is a *diff* review,
> not a whole-codebase pass; the second whole-codebase review (dev @ `5b885f5`, closed
> 2026-07-17) is preserved in this file's git history and its findings are all fixed.
>
> **Method.** 33 finder passes (7 file groups × 5 lenses: correctness, production,
> security, quality, UX) over the diff, then every candidate hit by 3 independent skeptics
> on different lenses (does-it-reproduce, is-it-already-handled, is-the-fix-correct), with
> majority rule. Then 5 completeness sweeps (cross-file contract drift, semantic regression
> against commit intent, untested behavior, a mechanical copy/UI-grammar sweep, and a
> devil's advocate pass over the files nobody scrutinised), each verified the same way.
> 91 candidates raised, 37 refuted, 54 confirmed, deduplicated here to **40 findings:
> 1 critical, 4 high, 12 medium, 23 low.**
>
> **Every CI gate is green on this tree** (`ruff`, `ruff format`, `mypy`, 1579 pytest,
> `eslint`, `tsc`, `vite build`, 48 vitest). Nothing below is caught by the gates. Two
> findings are regressions introduced by this diff (B-1, B-2); most of the rest are
> comments that now over-promise, recoveries that cannot be reached, and control-grammar
> misses from the consistency pass.

---

## 1. Bugs

### B-1 · The folder corroborator destroys the byte-identical-twins group — **critical**

`src/reaper/engine/identity.py:561` (regression, `1fabcf7`)

`_narrow_among_id_hits` now tries `_narrow_by_path_depth` **before** the size corroborator.
The size branch is the only path that can return several rating keys — the byte-identical
twins group, which exists precisely because "the file's plays are split across those
listings and reading only one would under-count watching, which is the direction that
condemns." `_narrow_by_path_depth` returns a single key and returns early, so whenever the
two listings of one file sit at different paths the group is never formed.

**Failure.** The exact shape the docstring says was verified live: one file listed twice,
a curated section re-listing it under its own rating key at a different path. Main listing
`/data/movies/Title/file.mkv`, curated re-listing `/data/curated/Title/file.mkv`, arr path
`/movies/Title/file.mkv`. Shared suffix depth is 3 vs 2, so the main listing binds alone.
Every play against the curated key is invisible: watcher counts fall, dormancy climbs, and
a file people actually watch is condemned. `LEARNINGS.md:483` records that on the tested
library *every* remaining ambiguity was a twin pair.

**Fix.** Do **not** simply swap the order — the size branch returns `()` when
`file_size is None`, and a show never has a size, so path narrowing would become
unreachable for shows and the split HD/4K bug this commit fixed would return. Instead make
`_narrow_by_path_depth` return `None` when the matched listings are byte-identical at this
basename (all carry this basename at exactly `file_size`, every such size known), so the
twins group still forms and path narrowing still runs for everything else. Update the
docstring at `identity.py:522-531`, which still claims all twins are returned. Add a
regression test passing `file_path` to the existing twins fixtures with divergent Plex
paths.

### B-2 · Folder-depth binding preempts exact byte size and can bind to the wrong copy — **high**

`src/reaper/engine/identity.py:491` (regression, `1fabcf7`) · rule 6

`_narrow_by_path_depth` treats "strictly deepest shared trailing path" as proof of
identity, but the module's premise is that mount roots differ. When the differing segment
is the library folder itself — exactly the HD-vs-4K case this was written for — a deeper
shared suffix is a coincidence between the arr's container root name and one library's
folder name, not evidence. `_shared_suffix_depth` is allowed to consume the arr's outermost
segment (its container mount root), and the strict-margin rule at 484/491 then treats that
coincidence as a win.

**Failure.** Two Radarr instances each map their own host directory to `/movies` inside
the container (the common setup); both report `/movies/Title/file.mkv`. Plex holds
`/data/media/movies/Title/file.mkv` and `/data/media/movies-4k/Title/file.mkv`. Shared
depth is 3 vs 2, so the 4K item binds to the HD listing and reads the HD copy's watch
history and `added_at`. Size would have separated them but is never reached. On the show
half there is no size at all, so this converts a correct abstain into a wrong bind.

**Fix.** Do not let `_shared_suffix_depth` consume the arr's outermost segment — compare
only below each side's root. Then require a margin over that reduced depth. Note the
proposed "consult `file_size` first" does nothing for shows (`resolve_show` always passes
`file_size=None`) and the suggested "shared depth must exceed the arr path length minus
root" guard kills the intended case, since Sonarr series paths are typically two segments.
Fix B-1 and B-2 together; they are one ordering/eligibility problem.

### B-3 · Watch-cache shape check compares column names only, so it never fires — **high**

`src/reaper/services/history_sync.py:204` (`c8fefdc`) · rules 7, 24

`ensure_schema` decides the table is stale by comparing PRAGMA column **names** against
`_WATCH_EVENT_COLUMNS`. The only shape change in this commit is
`watched_status REAL NOT NULL` → `watched_status REAL`. Names and order are identical
before and after, so the rebuild never fires on a real upgraded install. Installs that ran
the previous code already have `media_index` from the old `ALTER TABLE`, so they carry all
ten names in this order.

**Failure.** Verified in sqlite: the ten column names are byte-identical across the two
shapes, and the NULL insert fails. (a) Every pre-existing `0.0` written by the old `or 0`
coercion survives and is still read as "started, did not finish" — the exact ambiguity
`season_watch_stats`' new `max_unknown` branch defends against. (b) The next `sync` writes
`None` for any row without a `watched_status`, and SQLite raises
`IntegrityError: NOT NULL constraint failed`. `scan_runner._run_scan_locked` catches only
`IntegrationError` around `history_sync.sync`, so that propagates and the whole scan aborts
with a raw SQL error.

**Fix.** Store the expected `(name, type, notnull)` triple per column and compare against
`tuple((r[1], str(r[2]).upper(), int(r[3])) for r in cols)` (PRAGMA rows are
`cid, name, type, notnull, dflt_value, pk`). In `tests/test_season_scan.py`,
**add** the ten-column `watched_status REAL NOT NULL` legacy shape rather than replacing
the nine-column one — both must rebuild. The current test passes only because its
nine-column table trips the name mismatch, so it gives false assurance.

### B-4 · A quiet library is misdiagnosed as a stalled ingest and blocks every deletion — **high**

`src/reaper/services/snapshot.py:410-411` (`c8fefdc`) · rule 24

The new staleness guard calls `history_sync.latest()`, which is `MAX(watched_at)` over the
mirror: the time of the newest *play*, not the newest *sync*. Its docstring claims it is
the one thing that can tell a stalled ingest from a quiet library; the two produce an
identical `MAX(watched_at)`. The real freshness signal, `history_sync_state.synced_at`, is
already written by `_store_tautulli_total` on every successful sync and is never read.

**Failure.** A single-household server, or any operator away for a long weekend, records no
plays for 48 hours. Every scan degrades, `planner` refuses to plan a degraded snapshot
(`planner.py:257`), and nothing can be reviewed for removal until somebody watches
something. The operator is told "watch history has not updated recently", a false
diagnosis. This hits hardest on exactly the quiet libraries with the most to reclaim.

This fails in the safe direction (toward keeping files), so it is an availability and
false-diagnosis defect, not a data-loss one, and it clears as soon as any play lands.

**Fix.** Add `history_sync.last_synced_at(engine)` reading `history_sync_state.synced_at`
and gate the degrade on that. Correct two comments in the same change (rule 24):
`latest()`'s docstring claim that it distinguishes a stall from a quiet library, and
`MIRROR_STALE_AFTER`'s claim that it "matches `WHITELIST_STALE_AFTER`'s reasoning" —
`WHITELIST_STALE_AFTER` measures `last_synced_at`, a different quantity. Update
`tests/test_scan_pipeline.py`'s stale-mirror test to seed `synced_at`.

### B-5 · A fabricated `0` size reaches a real delete, and three comments say it cannot — **high**

`src/reaper/services/executor.py:1097`, `src/reaper/services/snapshot.py:653`,
`src/reaper/services/season_scan.py:1143` (`ae76eb8`, `2a4c7d2`, `406630b`) · rules 5, 24, 30

This diff correctly made an unreadable arr size `None` in the fact layer, then collapsed it
straight back to `0` on the candidate row on both writers (`item.size_bytes or 0`,
`season.size_on_disk or 0`). Each site carries a comment asserting the executor's second
layer catches it because "0 against any real size is unbounded growth". It does not:
`_grew_materially(0, live)` reduces to `live > 0 + max(0 // 10, _SIZE_DRIFT_FLOOR)`, i.e.
`live > 256 MB`.

**Failure, two halves.**
*The delete.* Radarr returns `hasFile: true` with `sizeOnDisk` missing (partial payload)
for a genuinely 180 MB file. The row stores `0`. At execute time
`_grew_materially(0, 188743680)` is `False`, the growth interlock is silent, and the file is
deleted against an approved size of 0. Bounded at 256 MB, which is the same absolute
allowance `_SIZE_DRIFT_FLOOR` deliberately grants any small item — so the delete itself is
close to documented design, and the item was condemned, gated and typed-confirmed.
*The accounting, which is not bounded.* `_check_caps` (`executor.py:410`) and
`_check_rolling_caps` (`executor.py:696`) both sum `candidate.size_bytes` before any send,
counting `0` for **every** unreported-size item including large ones, and
`api/runs.py:110` derives `total_bytes` into the server-recomputed confirmation phrase. So
a run can delete materially more than the cap the operator confirmed, and the byte total
they typed is wrong. That is a rule 5 / rule 30 violation.

The season comment is doubly wrong: it also claims `_send_season` "refuses twice", but the
first refusal (`executor.py:1229`) only fires when a live file size is unreadable.

**Fix.** Make an unconfirmed size fail closed rather than relying on growth math. In
`_send_movie` and `_send_season`, before the `_grew_materially` call:
`if int(candidate.size_bytes) <= 0: return self._mark_skipped(...)` with plain copy
("Reaper never got a size for this when it was scanned, so it can't confirm this is what
you approved. Kept."). Preferably also make `Candidate.size_bytes` nullable and have the
cap paths refuse to plan an item with no confirmed size rather than counting it as 0. Then
correct all three comments to cite the guard that actually fires.

### B-6 · The `fell_back` recovery notice can never render on load — **medium**

`frontend/src/components/PolicyEditor.tsx:1472` and `:2144` (`57d2405`) · rule 36

`dirty` forces true on `saved.needs_save` only. In `services/profiles.py:133-142` the two
recoveries are mutually exclusive: `rescaled=True` (which becomes `needs_save`) when
`rebalance()` repaired the body, and `fell_back=True` with `rescaled` left `False` when it
could not. So whenever `fell_back` is true, `dirty` is false, the savebar at `:2143` never
renders, and the notice inside it at `:2162` is unreachable on the load it exists to
explain. The Save button that would replace the broken row is unreachable with it, so every
subsequent load falls back again.

`scan_runner.py:530-535` does degrade the snapshot on `active.repaired`, so no deletion runs
under the fallback policy — this is a dead-end loop, not data loss. But the degradation
string at `scan_runner.py:533` tells the operator to "open the policy page, check the
points, and save", and the policy page shows them nothing to save.

**Fix.** (1) `dirty` must include the flag:
`Boolean(saved.needs_save) || Boolean(saved.fell_back) || JSON.stringify(draft) !== ...`.
(2) Move the `saved?.fell_back` notice out of the savebar to an unconditional render near
the top of `.editor-controls`, beside the `pendingSwitch` notice, so it does not depend on
any dirty computation. (3) Update the comment at `:1468-1471`, which names only the rescale
recovery. (4) Add a vitest case mounting `PolicyEditor` with
`{needs_save: false, fell_back: true}` asserting the notice renders. The backend already
has this covered at `tests/test_api.py:995`; only the frontend gate drops it.

### B-7 · Applying a preset always overshoots the 100-point budget and deadlocks both saves — **medium**

`frontend/src/components/PolicyEditor.tsx:1567` (`7e0f7ab`, `57d2405`)

`applyPreset` resets only `draft.signals` to `DEFAULT_WEIGHTS`, which already sum to
exactly 100. It leaves `draft.custom_condemn` untouched. The new budget check sums both and
disables Save whenever `pointsLeft !== 0`, matching the server's
`PolicyBody._weights_total_one_hundred`. So a preset click guarantees `100 + yourWeight`
for any operator with a custom removal rule.

**Failure.** An operator with one 15-point custom rule clicks "Cautious". The draft is 115.
The savebar reads "Take 15 away before saving" and Save is disabled. Because `applyPreset`
also stages the preset's caps into the pace draft and one Save button covers both, the
unrelated pace save is blocked too. The operator can recover only by manually lowering
built-ins or discarding, and the three presets are unusable for anyone with a custom rule.

**Fix.** Have `applyPreset` reset the whole removal lane to 100. Mirroring the server is
best: rescale the combined set with largest-remainder so the preset's mix and the
operator's rules together total exactly 100 — the same arithmetic as `engine/policy.rebalance`
(`policy.py:668-699`), which is score-preserving. State the behavior in the preset help
text, which currently promises only a threshold and a pace.

### B-8 · The why panel renders the value and the operator's own bar with the same lossy phrase — **medium**

`src/reaper/engine/fields.py:746` (`a26ac83`) · rule 21

`_render` now routes `FieldType.DAYS` through `humanize_days`, and `_explain_number` uses
`_render` for **both** sides: the measured value and `bar.format(_render(spec, target))`.
`humanize_days` collapses to at most two units and buckets months in 30-day steps, so any
two day-counts in the same bucket render identically.

**Failure.** Verified by running `evaluate`: a rule `days_unwatched >= 400` against an item
at 396 days gives "Not watched in 1 year, 1 month, within your 1 year, 1 month". Flipping
the bar to 396 gives "…past your 1 year, 1 month". The matched and unmatched cases are
word-for-word identical apart from within/past, and each line asserts the value is on one
side of a number it prints as equal to itself. Every day-count from 395 to 424 collapses to
the same phrase — exactly the marginal items an operator scrutinises before approving a
deletion.

**Fix.** Keep `humanize_days` for the measured value; render the operator's threshold
exactly, in the units they typed. The spec already carries `unit_suffix="days"`
(`fields.py:262`), served to the editor and rendered as the fixed unit beside the input, so
the operator types "400 days" and the panel echoes back "your 1 year, 1 month". Add
`_render_bar(spec, target)` formatting DAYS as `f"{_num(target):,.0f} days"`. `release_age`
(`fields.py:431`) has the same shape and needs the same fix. Add a regression test asserting
the value phrase and the bar phrase are never the same string for differing inputs.

### B-9 · A corrupt policy body raises out of the one function whose contract is "must not raise" — **medium**

`src/reaper/services/profiles.py:137`, `src/reaper/engine/policy.py:685` · rule 7

`active_policy`'s docstring promises in bold that it must not raise on a stored body that no
longer validates, because the editor, the simulator and the scan all read it and a raise
takes out the page that fixes the problem. Two holes, both verified by execution:

1. `json.loads(row.body_json)` at `profiles.py:137` sits **outside** the `try`. Pydantic v2
   raises `ValidationError` (`json_invalid`) for malformed JSON, so control reaches the
   handler and `json.loads` then raises `JSONDecodeError` uncaught.
2. Valid JSON that is not an object escapes by a different route: `rebalance([])` →
   `AttributeError: 'list' object has no attribute 'get'`, which `rebalance`'s
   `except (KeyError, TypeError, ValueError, ValidationError)` does not catch, defeating
   the "returns None when the body is unreadable" contract in its own docstring.

Reachability requires an externally edited, truncated or restored row — every in-app writer
is `body.model_dump_json()`. Impact is availability, and it fails closed (the scan crashes
rather than deleting); the harm is the editor lockout the docstrings say the fallback exists
to prevent.

**Fix.** In `active_policy`: `try: raw = json.loads(row.body_json) except ValueError: raw = None`,
then `repaired = rebalance(raw) if isinstance(raw, dict) else None`, falling through to the
existing `fell_back=True` return. In `rebalance`: `if not isinstance(body, dict): return None`
at the top of the try, and add `AttributeError` to the except tuple. Add tests for a
non-JSON and a JSON-array `body_json` asserting `fell_back=True` rather than a raise.

### B-10 · Sparing or reaping never refreshes the grace countdown — **medium**

`frontend/src/useOverrideMutations.ts:16` (`7fd871b`) · rule 7

The new shared hook's header comment declares it the single place "the list of caches an
override touches is written once, here" and that "every cache refreshes together". `refresh()`
invalidates `["candidates"]`, `["group"]` and `["candidate"]` — not `["grace"]`.
`grace_report` builds its list from `whitelist.overrides` + `effective_condemned`, so a
spare removes an item from the countdown immediately and a hand reap enters it immediately.
With `staleTime: 30_000` and `refetchOnWindowFocus: false`, the plan view serves the stale
countdown.

Not a regression — the baseline call sites had the same omission — but the diff created this
function wholesale and wrapped it in a comment asserting the opposite. `StatusChip.tsx`'s
`OverrideChip` doc makes the same false claim ("a reap takes effect immediately: counts,
grace countdown, the next plan").

**Failure.** Operator hand-reaps from the queue, switches to the plan view within 30 s:
a just-spared item is still listed as counting down toward removal, a just-reaped one is
missing, and the summary counts are wrong by the same amount — on the page where the
deletion plan is built. The plan itself is server-built and the phrase is server-recomputed,
so nothing incorrect is deleted.

**Fix.** Add `void queryClient.invalidateQueries({ queryKey: ["grace"] });` to `refresh()`.
The pattern already exists in three other places (`GracePanel.tsx:110`,
`PolicyEditor.tsx:1364`, `PlexPanel.tsx:268`). Have `GracePanel`'s `cancel` call the shared
`refresh()` instead of its own list (its `["whitelist"]` invalidation has no consumer query).
Correct both comments.

### B-11 · `chipWhy` strips only the protect-lane prefix, so a refused reap renders mid-sentence — **medium**

`frontend/src/components/StatusChip.tsx:33` (`7fd871b`) · rule 21

`chipWhy` removes a literal `"Kept · "` prefix and its result is interpolated into
`OverrideChip`'s lowercase sentence "Reap requested · kept for now: {keptWhy}". But
`reap_override_verdict` refuses a hand reap whenever `blocked` is true — the abstain lane —
and abstain chips carry no `"Kept · "` prefix. They pass through capitalized, and several
carry their own middot.

**Failure.** An operator hits Reap on an item whose protections could not all be checked.
The card renders "Reap requested · kept for now: Needs a look · left for you to decide" —
two middots in one chip, a capital mid-sentence, and a phrase naming the abstain reason
rather than why the reap was refused. The genuinely wrong pair is that one and "Needs a
look · watched more than a season your rule keeps"; `Couldn't be found in Plex` and
`Some checks couldn't run` interpolate accurately and suffer only the capital.

**Fix.** Do not fall through to `"a safety stop applies"` — that phrase is only true for the
`STRUCTURAL_GATES` path (`verdict.py:36`) and would be inaccurate for the unmatched and
unchecked-protection cases. Either have the server supply the refusal reason rather than
reusing the lane chip, or map the blocked-abstain cases to their own lowercase clause.

### B-12 · A saved manual Plex address can never be edited again — **medium**

`frontend/src/components/PlexPanel.tsx:432` (new file)

The Connection `<select>` collapses "the address you typed" and "open the manual editor"
onto one sentinel value. When the saved URI is not a discovered connection,
`connectionValue` is already `MANUAL_CONNECTION` and the only manual option rendered is
`Manual · {savedUri}` — the currently selected one. Re-selecting the selected option fires
no `onChange`, so `openManual()` never runs and the manual-address row can never be shown.

**Failure.** Not a wrong-port save (`PUT /plex/connection` probes before writing,
`settings.py:671`), but an address that worked when saved and later needs editing: the host
moved, the port changed, or it must switch to https. The escape hatch is narrow rather than
absent — if discovery succeeded and lists a reachable connection, selecting it fires
`onChange` and flips `savedIsDiscovered` true, at which point `Manual address…` appears. But
that mutates the live connection, fails if the alternate does not probe, and is unavailable
entirely when `connections` is empty.

**Fix.** Give the stored manual address its own option value: render
`<option value={savedUri}>Manual · {savedUri}</option>` when `!savedIsDiscovered`, set
`connectionValue` to `manualOpen ? MANUAL_CONNECTION : savedUri`, and always render a
separate `<option value={MANUAL_CONNECTION}>Manual address…</option>`.

### B-13 · A failed snapshot fetch is reported as "No scan has run yet" — **low**

`frontend/src/App.tsx:104` · rule 36

`ScanFreshness` treats `undefined` data as "no scan exists". The query uses `retry: false`,
and `/api/snapshots/latest` 404s only for the genuine no-scan case; every other failure
lands as `data === undefined` too. The component collapses them into a positive claim and
drops the `snapshot.degraded` line, the only staleness signal on the review screen.

React Query retains the last successful data across failed refetches, so this bites on the
first fetch of a session (with `retry: false`, one dropped request sticks until a focus
refetch), plus a brief flash on every load. `planner.py:257-261` refuses to plan a degraded
snapshot server-side, so it cannot enable a deletion.

**Fix.** Pass `isPending` and `error` into `ScanFreshness` (`App.tsx:285` already has them).
Three states: pending → "Checking the last scan…"; `error instanceof ApiError && error.status === 404`
→ the existing no-scan copy; any other error → a `.notice.notice-error` saying the last
scan's state couldn't be read. `ApiError` carries `status` (`api.ts:769-777`).

### B-14 · A failed sign-out's error notice is unreachable — **low**

`frontend/src/App.tsx:152` · rule 36

`UserMenu`'s new `onBlur` closes the disclosure whenever focus leaves the wrapper, and Sign
out becomes `disabled` while pending. Disabling a focused control moves focus off it
(Firefox and WebKit dispatch `focusout` with a null `relatedTarget`), so `onBlur` fires and
unmounts the dropdown including the `signOut.isError` notice added in the same change. The
mutation state persists, so reopening later shows a stale "Couldn't sign you out" with no
context. `api.logout()` genuinely rejects (`api.ts:807-810`), so the notice is not dead
code; the residual case is a 500 from `/api/auth/logout` on an engine that fires `focusout`
on disable.

**Fix.** `if (signOut.isPending || signOut.isError) return;` at the top of `onBlur` and in
the mousedown-outside listener, plus `signOut.reset()` when the menu opens.

### B-15 · The new dormancy phrase is mangled to "less than ad" in the queue — **low**

`src/reaper/clock.py:385` → `frontend/src/components/ReviewQueue.tsx` · rule 21

`humanize_days` changed its sub-day return from "today" to "less than a day". That string
reaches the frontend verbatim as `CandidateOut.dormant_for`, and `compactSpan()` rewrites
unit words with a regex that matches the " day" inside "a day". The old value contained no
unit word, so this never fired before.

**Failure.** A title played a few hours ago renders its amber pill as "Not watched in less
than ad". The `title` attribute (`ReviewQueue.tsx:393`) is still correct, and reaching the
card requires a hand reap or a deliberately disabled dormancy protection, so no decision is
affected.

**Fix.** Make `compactSpan` only rewrite a unit that follows a number:
`replace(/(\d+) days?/g, "$1d")` per unit. Add a case pinning
`compactSpan("less than a day") === "less than a day"`.

---

## 2. Hacks and workarounds

### H-1 · The repaired-policy degrade is pinned by grepping source text, not by behavior — **medium**

`tests/test_fact_layer_states.py:201-205`

The only test for "a scan on a repaired policy must not be executable" reads
`src/reaper/services/scan_runner.py` as a string and asserts the literal substring
`"if active.repaired:"` appears in it. It proves nothing: it passes if the branch appends to
a list nobody consumes, if `pre_scan_degradations` stops reaching `snapshot.degraded`, or if
`degraded` stops blocking execution — and it fails on a pure rename that keeps behavior
correct. The path is also cwd-relative, unlike `tests/test_multi_instance.py:128` which
anchors on `Path(__file__).parent.parent`; CI runs from the repo root so this is latent
fragility, not a live break.

**Fix, cheaper than it looks.** `tests/test_scan_pipeline.py:567-638` already monkeypatches
`scan_runner.profiles.active_policies` with a fake returning real `ActivePolicy` objects and
captures `extra_degrade_reasons`. Return `ActivePolicy(DEFAULT_MOVIE_POLICY, "default", rescaled=True)`
from that stub to exercise this branch with no new scaffolding, assert
`snapshot.degraded is True` and the reason names the policy, then assert the execute route
refuses it. Delete the source grep rather than keeping it alongside.

### H-2 · `_STATE` is documented as a key translation but is an identity map — **low**

`scripts/policy_lab_extract.py:86`

The comment says `_STATE` maps `facts_codec`'s compact keys to the fixture's spelled-out
ones. `facts_codec._obs_to_dict` already emits `"known"/"absent"/"unknown"`, so the dict maps
each key to itself. Its real job is validating an unrecognized state down to the caller's
default, which the comment does not say. Dev-only script; documentation-only impact.

**Fix.** `_STATES = frozenset({"known", "absent", "unknown"})` with a comment stating what it
does, and `kind = raw if (raw := str(entry.get("k"))) in _STATES else default`.

### H-3 · `parse_humanized` survives after its only call site was deleted — **low**

`scripts/policy_lab_extract.py:56`

The refactor to read frozen facts (`023b37e`) removed the dormancy reconstruction that
called `parse_humanized`, but left the function, its `UNITS` table, and the now-only-for-it
`import re`. The point of the change was to stop parsing operator-facing copy; leaving the
parser invites the next person to reach for it — and this commit's own comment records that
a reworded sentence silently deleted 210 known season ranks from the fixture.

**Fix.** Delete `parse_humanized`, `UNITS`, and the `re` import.
(`scripts/validate_ingest.py` has its own copy and still uses it; leave that one alone.)

---

## 3. Refactor opportunities

### R-1 · The hand-spare detail string is defined twice and coupled by exact equality — **low**

`src/reaper/services/snapshot.py:796`, `src/reaper/api/routes.py:488`

`routes._SPARE_DETAIL` and `snapshot._HAND_SPARE_DETAIL` hold the same literal, and the
review chip identifies a hand spare purely by string equality. This diff had to edit both in
lockstep, and each file's comment asks the other to "stay in step" — the definition of a
drift-prone duplicate. Note the diff **improved** this (the baseline had four raw inline
copies and no constant), and no numbered rule covers it; rule 18 is frontend-scoped and
rule 3 is scoped to condemn/score logic.

**Failure.** A future copy pass rewords one side; `_kept_phrase` stops matching and every
hand-spared item renders "Kept · on your keep list" instead of "Kept · you spared it",
telling the operator the item is protected by a list it is not on.
`tests/test_review_chips.py` still passes because it feeds a third copy of the literal.

**Fix.** One constant, imported by `routes.py:520`, `routes.py:1171`, `snapshot.py` and
`_replay_simulation`. Change the test to reference it rather than a literal.

### R-2 · `LocalSheet` hand-rolls the dialog contract `ModalShell` now owns — **low**

`frontend/src/components/Login.tsx:213` · rule 18

`ModalShell` was added as "the one modal" owning `role="dialog"`, `aria-modal`,
Escape-to-close, focus-in-on-open, focus-restore-on-close and Tab containment, with a header
comment saying it is "written once so no modal can ship without it". `LocalSheet` implements
its own version of the first four and has **no Tab trap**, so the two have already diverged
and the comment is untrue of this file. (The sheet's `role="dialog"` and some of its focus
handling predate the diff; the divergence is what this change created.)

**Failure.** A keyboard user opens the local-account sheet and tabs past Sign in. Focus
lands on the buttons behind the scrim, which the sheet declares unreachable via
`aria-modal="true"`.

**Fix.** Give `ModalShell` a stay-mounted mode (render always, `inert={!open}`, run focus
and trap only while open) and render `LocalSheet` through it. If the slide-in must stay
separate, export `ModalShell`'s `FOCUSABLE` list and `trapTab` helper, wire them into the
sheet, and correct the "every modal" comment to name the exception.

### R-3 · `SignalRow`'s docstring was stranded above `PointsBudget` — **low**

`frontend/src/components/PolicyEditor.tsx:485`

The commit that replaced the per-signal "share of the score" readout with flat points
inserted `PointsBudget` between `SignalRow`'s JSDoc and `SignalRow` itself. The result is two
stacked doc blocks, the first documenting a function 70 lines below and describing a readout
that no longer exists (`share` and the `totalWeight` prop were both deleted in the same
diff).

**Fix.** Delete the stale block, or move a corrected version above `function SignalRow` at
`:542` describing the flat "up to N points" readout it now renders.

### R-4 · `draftRuleCount` is named and documented as a rule count but prints point totals — **low**

`frontend/src/components/PolicyEditor.tsx:538`

The name and the docstring example ("4 built-in signals · 1 of your rules") promise a count
of rules; the two arguments are the point sums `builtInWeight` and `yourWeight`. The
rendered string happens to read acceptably as a points split, which is what makes it a trap.

**Failure.** A developer asked to also show the number of custom rules trusts the name and
passes `draft.custom_condemn.length`, producing "70 built in · 1 yours" beside "100 of 100
removal points used" — two numbers that no longer add to the total beside them.

**Fix.** Rename to `pointsSplit` and correct the docstring to the string it produces.

### R-5 · The second "less than a day" return in `humanize_days` is unreachable — **low**

`src/reaper/clock.py:106`

After the `whole <= 0` early return, `whole >= 1`, so at least one unit is non-zero and
`present` can never be empty. The unreachability is total: NaN and inf raise inside `round()`
before reaching it. The diff edited this exact line (it used to return "today"), so the dead
branch was re-stated rather than removed, leaving the wording in two places with one never
exercised.

**Fix.** Delete lines 106-107 and let the final join run.

---

## 4. Performance

### P-1 · The scan reads the watch mirror twice — **low**

`src/reaper/services/snapshot.py:410`

The new staleness check calls `history_sync.latest(engine)` a few lines after
`history_sync.horizon(engine)`. Both are thin wrappers over `_state()`, which runs
`ensure_schema()` (a PRAGMA plus every DDL statement inside an `engine.begin()` write
transaction) and then a `SELECT COUNT(*), MIN(watched_at), MAX(watched_at)` aggregate.
`history_sync.state()` already returns all three in one pass. The table has a covering index
on `watched_at`, so this is an O(n) index scan rather than a heap scan — modest at a few
hundred thousand rows, but it is pure duplicate work plus a second write-transaction DDL
pass on every scan.

**Fix.** `mirror = await history_sync.state(engine)` once before the `no_history` branch,
then use `mirror.earliest` and `mirror.latest`. Folds naturally into B-4's fix.

### P-2 · `stats()` computes and discards three values plus two aggregate queries — **low**

`scripts/policy_lab_extract.py:162`

After the switch to frozen facts the only value taken from `stats()` is `recency`. It still
computes `days`, `window` and `ever` for every candidate, and `movie_last` / `season_last` /
`window_start` exist solely to feed those dead outputs, costing two extra `GROUP BY` scans of
`watch_event` at startup. Dev-only script, so the cost is a one-off manual run; the durable
harm is that the dead outputs read as if the script still derives dormancy itself,
contradicting the new comment that says frozen facts are the source.

**Fix.** Narrow to `recency_days(keys, media_type) -> list[float]`, drop the three dead
outputs and the two queries that feed them, update the call site.

---

## 5. Production readiness

### PR-1 · The Plex link flow in Settings has no fallback URL and no cancel — **low**

`frontend/src/components/PlexPanel.tsx:118` (new file) · rules 42, 36

`startLink` awaits `api.plexLinkStart()` and only then calls `window.open`. The user gesture
has already been consumed by the await, so browsers routinely block the popup;
`window.open(..., "noopener")` returns null by spec, so the return value carries no signal
and is discarded along with `start.auth_url`. Polling begins regardless. While `linking` is
true the only control rendered is the disabled "Waiting for Plex…" button: no fallback link,
no cancel, no error. `Login.tsx` already keeps the URL in state and renders a "Didn't open?"
link, so the same flow now exists twice with different affordances.

**Failure.** An operator with popups blocked clicks Link with Plex. The plex.tv tab never
opens, the button stays disabled, nothing offers the approval URL or a way out, and five
minutes later a gray line says the sign-in timed out with no explanation.

**Fix.** Hold the auth URL in state and, while `linking`, render the same affordances
`Login.tsx` has: a visible fallback `<a href={authUrl} target="_blank" rel="noreferrer">` and
a Cancel wired to `pin.cancel()` + `setLinking(false)`. Route `onTimedOut` through
`setPlexError` (see U-5).

### PR-2 · The log viewer claims it is retrying when following is switched off — **low**

`frontend/src/components/LogsPanel.tsx:190` (new file) · rule 21

The error strip shown when the log query fails with lines already on screen says "Retrying…"
unconditionally. Retrying is only true while `live` is on, because that is the only thing
setting `refetchInterval`. With "Follow new lines" off, `refetchInterval` is `false`, React
Query's default retries are exhausted, and nothing further is scheduled. That branch also
offers no manual retry: the "Try again" button exists only in the `lines.length === 0`
branch at `:162`.

**Fix.** Make the copy match the state: when `live`, say updates hit a problem and are being
retried; when not, say updates are paused and render the same `logs.refetch()` "Try again"
button used in the empty branch.

### PR-3 · `_explain_failure`, ninety ordering-sensitive lines, has zero tests — **low**

`src/reaper/services/instances.py:244`

`test_connection`'s error reporting was replaced wholesale with `_causes` +
`_explain_failure`: a cause-chain walk and a fifteen-branch classifier whose correctness
depends entirely on branch order (the SSL check must precede the transport branch because
certificate problems surface as `ConnectError`; the `IntegrationError`-with-no-status branch
must come after the transport families). Nothing in `tests/` references `_explain_failure`,
`_causes` or `_GENERIC_FAILURE`. Branch order is correct at HEAD, so this is coverage
exposure, not a live defect — but this is the first screen a new operator sees.

**Failure.** Move the SSL check below `httpx.ConnectError`, or drop `__context__` from
`_causes`. Every test passes, and an operator with a self-signed certificate is told
"Couldn't reach the server at this address. Check the URL and port" instead of the one
message that names the actual fix.

**Fix.** A table-driven test feeding `_explain_failure` a constructed cause chain per family
(`IntegrationError` from `ssl.SSLError`, from `ConnectTimeout`, from `ConnectError`; statuses
401/404/429/302/500; a `ValueError` body; a bare `IntegrationError`; an unrecognized
exception), asserting the returned sentence. The SSL-under-`ConnectError` chain pins the
ordering.

---

## 6. Security

### S-1 · Certificate-failure copy recommends disabling verification on any `SSLError` — **low**

`src/reaper/services/instances.py:256`

The first branch of `_explain_failure` fires on any `ssl.SSLError` anywhere in the cause
chain and recommends turning off the certificate check. That branch also covers hostname
mismatch, an expired or wrong-CA certificate, and active interception — not only the
self-signed case the sentence names. The connection carries a full-admin API key (a Tautulli
key can delete libraries and restart the service).

The sentence is already conditioned ("If it is a self-signed certificate on a server you run
yourself…"), so this is closer to a copy-accuracy defect than a vulnerability, and the
verification toggle is a deliberate, operator-controlled feature. It is filed here because
the failure mode it mis-advises on is the one where the advice is harmful.

**Fix.** Bind the remedy to the case it is safe for and name the cost: "The server's
certificate couldn't be verified. Only turn off the certificate check if this is your own
server on your own network: your API key travels on this connection." Optionally split the
branch so `ssl.SSLCertVerificationError` with a self-signed or unknown-CA reason gets the
remedy and other `SSLError` kinds get a message that does not suggest disabling
verification.

*No other security findings survived verification.* Specifically checked and clean: no raw
`httpx`/`requests` outside `clients/` beyond the sanctioned Discord webhook (rule 33); no
secrets in URLs or logs; no new `verify=False` default; no `dangerouslySetInnerHTML`; the
new routes carry the auth dependency and the CSRF header requirement; no new unpinned
dependency.

---

## 7. UI/UX consistency

### U-1 · A plan built from an older scan warns only in the history list, never beside Execute — **medium**

`frontend/src/components/ReapPlan.tsx:222` · rules 42, 36

The new `olderScan` check renders only as a muted tail on the open row down in "Recent
plans" ("· open above, built from an older scan"). The plan summary above, which carries the
live `Execute…` button, says nothing. Rule 42 requires the warning beside the control that
fixes it. It also fails silent: `olderScan` is `latestSnapshot != null && …`, so while the
snapshot query is loading or if it errored, nothing appears and the operator gets no signal
either way.

Several executor interlocks do re-read fresh state on a stale plan (manual spares, the
streaming veto, played-since-approval, per-item existence and size, the canary), so the
blast radius is bounded. What a superseded scan loses is policy-derived protection only the
new scan would have found — a keep-tag added in the arr, a rating change.

**Fix.** Compute `staleRun` in the `run` block and render a `.notice.notice-warn` inside
`.plan-summary`, directly above Execute…, with a plain lead and the fixing control ("This
plan was built from an older scan. Build a new plan.") wired to `plan.mutate()`. Handle the
unknown case explicitly: when the query is pending or errored, say Reaper couldn't check.
Drop the staleness wording from the history list so it lives in one place.

### U-2 · The lean-keep discount is a bare number box beside loose "up to −" text — **medium**

`frontend/src/components/PolicyEditor.tsx:1256` · rule 40

In `KeepRulesEditor`'s "Leans toward keeping" form, the discount is a raw
`<input type="number">` with the unit outside it as loose muted text. Rule 40 forbids
exactly this. The same commit converted the neighboring "full effect at" input in this very
form to `FixedQuantity`, and the parallel control in `RemoveRulesEditor` (`:1012`) is already
`FixedQuantity suffix="points"`, so the two halves of the same page disagree. (The input is
nested in a `<label>`, so it does have an accessible name — but the name is "up to minus",
which is meaningless.)

**Fix.** `<FixedQuantity value={lPoints} onChange={setLPoints} suffix="points off" min={1} max={100} width="narrow" ariaLabel="Points this rule takes off" />`,
and reduce the loose text to "up to". For consistency make the summary row at `:1154` read
`lowers the score, up to −{k.max_discount} points`, matching the `+{r.weight} points` the
remove rows gained at `:921`.

### U-3 · Three two-option dropdowns survived the control-grammar pass — **medium**

`frontend/src/components/PolicyEditor.tsx:1000`, `:1195`, `:1229` · rule 41

Rule 41 states a choice between two visible options is the shared `Segmented`, and that
"hiding a binary inside a dropdown is never allowed". Three `<select>` elements hold exactly
two options each: the condemn-rule boolean picker (`:1000`), the hard-keep boolean picker
(`:1195`), and the lean-direction picker (`:1229`, "the more, the safer" / "the less, the
safer"). The diff touched all three (adding `aria-label`) as part of the consistency pass and
rebuilt the number input beside the third into a `FixedQuantity`, so the rows were reworked
with the control type left on the old pattern.

**Failure.** For the lean-direction picker specifically: `lDir` initializes to `"high_keeps"`,
so an operator who never opens the dropdown saves that default without ever seeing that a
direction choice exists. That is exactly what `Segmented.tsx`'s own header comment ("never
for a binary the user should see whole") exists to prevent.

**Fix.** Replace all three with `Segmented`. Worth a separate look while in here:
`BOOL_OPS = (Op.EQ,)` at `fields.py:69` means the comparison `<select>` beside a bool value
renders a single-option dropdown that cannot be changed at all.

### U-4 · The certificate warning is detached from the toggle that causes it — **low**

`frontend/src/components/PlexPanel.tsx:529` · rule 42

Turning off "Check the server's certificate" raises a `.notice.notice-warn` that renders
*after* the `</div>` closing `.set-rows` (`:527`), below the unrelated "Plex web address" row
(`:498-526`), rather than beside the switch that produced it (`:478-496`). `.set-rows` is a
bordered container, so the warning renders outside the box holding the control.
`ServiceModal.tsx` renders the identical warning directly beneath its own certificate switch,
so the same warning now has two placements.

**Fix.** Move the block inside the certificate `.set-row`, beneath its `.set-control`,
matching `ServiceModal.tsx`. Since the row's `.help` already says "Turn this off only for a
server you run yourself…", shorten the notice to the consequence alone: "Reaper will accept
this server's certificate without checking who issued it."

### U-5 · A timed-out Plex sign-in renders as gray status text, not a failure — **low**

`frontend/src/components/PlexPanel.tsx:103` · rule 42

`PlexPanel` deliberately splits `message` (info, `<p className="muted">`) from `plexError`
(`.notice.notice-error`) precisely so failures do not read as status — its own comment at
`:42-44` says so. `onTimedOut` writes into `message`, so a link attempt that never completed
is announced in the same gray type and the same slot a successful "Linked to …" occupies,
one line above `onFailed` which correctly uses `plexError`.

**Fix.** `setPlexError("Plex sign-in timed out. Try again.")` instead of `setMessage(...)`.
`startLink` already clears both before each attempt, so no lingering-error concern.

### U-6 · The log filter offers two options that do the same thing — **low**

`frontend/src/components/LogsPanel.tsx:128`

`DEBUG` is the lowest rank in `LEVEL_RANK` (10) and unknown levels fall back to 20, so every
line passes the `>= LEVEL_RANK["DEBUG"]` test. "Debug and up" filters nothing, which is
identical to "All levels" — two adjacent options in one dropdown with different labels and
identical behavior.

**Fix.** Drop the `DEBUG` option and keep "All levels" as the no-filter choice, or relabel so
each names a distinct floor ("All levels", "Info and up", "Warnings and up", "Errors only").

### U-7 · The show-status chip's accessible name is on an element that cannot take one — **low**

`frontend/src/components/ReviewQueue.tsx:667`

`ShowStatusChip` renders a `<span>` with no role and puts the disambiguating sentence in
`aria-label`. ARIA prohibits naming elements with an implicit generic role, so assistive tech
falls back to the visible text and announces the bare "Ended" / "Status unknown" that the
code's own comment identifies as ambiguous. Used at three call sites (`ReviewQueue.tsx:1067`,
`WhyPanel.tsx:749`, `ShowPanel.tsx:77`). A `<span>` is not focusable, so the route is browse
mode, not Tab.

**Fix.** Give it a role that supports naming (`role="img"` with the `aria-label`, or
`role="note"`), or drop `aria-label` and render the long form in a visually-hidden span
alongside. Keep `title` for the mouse tooltip.

### U-8 · Policy help text says "*arr", which no operator says — **low**

`frontend/src/components/policyMeta.ts:50` · rule 21

The `unmanaged` protection's help string, new in this diff's plain-language pass, uses the
community shorthand "*arr". Every other entry in the same file spells the services out. The
string is live: `GateRow` renders `meta.help` for every gate except `whitelisted` and
`rating_floor` (`PolicyEditor.tsx:1792`).

**Fix.** "If Sonarr or Radarr doesn't own the file, Reaper has no safe way to remove it."

---

## 8. Improvements

### I-1 · The ingest side of the NULL `watched_status` fix has no test — **medium**

`src/reaper/services/history_sync.py:296`

`a2120c3` exists so a completion Tautulli never reported is stored as NULL rather than `0.0`,
because the sequential guard reads `watched_status = 1` as completed and a fabricated `0.0`
makes a viewer look further behind than they are. `_float_or_none` is the only place NULL is
ever written in production, and it has no test — `tests/test_history_sync.py` is untouched by
the entire diff. Every test exercising the NULL path inserts rows with raw SQL and bypasses
`sync`, so they prove the *query* handles NULL while proving nothing about whether one is
ever produced.

**Failure.** Revert line 296 to `float(row.get("watched_status") or 0)` and the suite stays
fully green, while in production `max_unknown` is always NULL, the sequential guard parks the
viewer on their last confirmed episode, and the season they are about to watch loses its
protection.

**Fix.** Drive `sync` with rows whose payload omits `watched_status` and one where it is `""`;
assert the stored column is NULL, and assert a genuine `0.0` still round-trips as `0.0` so
the fix is pinned in both directions. Add direct `_float_or_none` unit cases.

### I-2 · The rescale test's `< 1.0` tolerance does not establish the claim the migration rests on — **low**

`tests/test_policy.py:496` · rule 31

`test_rescaling_preserves_every_score` is the only proof that `policy.rebalance()` cannot move
a score, and it asserts `abs(before - after) < 1.0` over a single four-signal shape.
Largest-remainder does not bound score drift below one point: each weight's delta is bounded
by 1, but the deltas do not cancel in the score, since
`score' - score = Σ (w'_i - w_i·100/T)·fill_i`, so positive deltas can land on filled signals
and negative ones on empty signals.

**Failure.** No exotic fixture needed. A four-signal legacy policy with weights `(1, 1, 1, 5)`
on the exact signal set the test itself uses rescales to `[13, 13, 12, 62]` and drifts past
the assertion; a six-equal-weight policy rescaling to `17,17,17,17,16,16` drifts 1.33 points
with pressure only on the 16s — past the boundary that decides condemn vs abstain.

**Fix.** Make it a property test over drawn weight vectors, varying the *count* of signals,
and assert what matters: `decide_verdict` is unchanged, or `round(before) == round(after)` at
the shipped thresholds. When a counterexample falls out, either correct
`policy.rebalance`'s docstring claim that "largest-remainder keeps that under a point", or
allocate remainder points to the largest weights so the residual lands where fill is
likeliest.

### I-3 · The show-status rollup test does not pin order-independence — **low**

`tests/test_show_status.py:223`

The test says the rollup "must skip [the empty row] rather than let row order blank a status
the group plainly has", but the fixture puts the season carrying `ended` **first** and the
null second. A naive `seasons[0].show_status` implementation passes. The assertion on the
seasons array order also locks that ordering in, so the test cannot be strengthened by
chance. `routes.py:875` is correct as shipped; this is coverage, not a defect.

**Fix.** Add a fourth show whose first season (by the order `/api/groups` returns) carries
`show_status=None` and whose later season carries `ended`, asserting the group still reports
`ended`. Keep the existing show as the both-orders case.

### I-4 · `_OBSERVED_FIELDS` claims to be exhaustive over the score lane but is not — **low**

`tests/test_engine_invariants.py:421` · rule 7

The comment says "Every `Observation` field on `Facts` that the score lane can read", and the
property tests iterate exactly this tuple to substitute `Unknown`. It omits `genres`, which is
condemn-lane authorable (`fields.BY_KEY` key `genre`). Harmless today because `_CUSTOM_CONDEMN`
and `_KEEPS` do not reference it, but the comment tells the next author the sweep is exhaustive
when it is not.

**Fix.** Add `"genres"` and derive the tuple from `fields.BY_KEY`'s fact attribute names rather
than hand-listing, so a new authorable field cannot leave the sweep behind. Do **not** add
`others_watching` (no `FieldSpec` at all; gate-lane only, via `OthersWatchingGate`) or
`in_curated_list` — the original suggestion was wrong on both. If a field must stay out, name it
in the comment with the reason.

### I-5 · The new bulk override and select-everything paths have no tests — **low**

`frontend/src/components/ReviewQueue.tsx:1242`

`7fd871b` added a bulk override mutation (`allSettled` + failure reconciliation + selection
retention) and a select-everything mutation that must fail closed when paging is incomplete.
Both write the override that decides whether an item is reaped or spared. Neither has a test;
`useOverrideMutations.ts`, extracted in the same batch and shared with the why panel, is also
untested. The code satisfies rule 20 today — this is regression exposure only.

**Failure.** Someone changes `onSuccess` to `setSelected(new Set())` unconditionally (a natural
cleanup). Three of fifty override writes failed; those three silently drop out of the selection
and the next plan is built from a selection the operator believes includes them. Nothing fails.
Equally, dropping the `if (result.hasNextPage || result.isError || !result.data) throw` guard at
`:1282` makes "select everything matching" silently mean "select the first page".

**Fix.** `frontend/src/components/ReviewQueue.test.tsx` mocking `../api` the way
`SeasonList.test.tsx` does, covering: a bulk spare where one of three `api.override` calls
rejects (assert `refresh()` ran, the failed key alone stays selected, the notice names the
count); `selectEverything` stopping short of the last page (assert selection untouched, error
rendered); and acting disabled while pending. The test must first enter select mode via the
toggle at `:1602`, since the bulk bar at `:1737` renders only then.

---

## Suggested fix order

1. **B-1 + B-2** together (one ordering/eligibility fix in `identity.py`, plus the docstring)
   — the only findings that change what gets deleted.
2. **B-3** (watch-cache shape) — an upgraded install currently crashes its scan on the first
   Tautulli row with no `watched_status`.
3. **B-5** (unconfirmed size fails closed in the executor + the cap paths + three comments).
4. **B-4 + P-1** together (`synced_at` instead of `latest()`, one mirror read).
5. **B-6 + B-7** (the policy editor's two dead ends), then **B-9** (corrupt body).
6. **H-1** and **I-1** (replace the source-grep test; pin the NULL ingest) — cheap, and both
   guard fixes already landed.
7. **U-1, U-2, U-3** (the control-grammar misses), then the remaining lows by file.

---

## Agent rules

Rules for the next agent working in this repo. These are constraints, not suggestions; each
one is derived from a confirmed finding above. They extend CLAUDE.md's numbered rules, and
where one sharpens an existing rule the more specific obligation governs.

1. **A corroborator that can return a group must run before one that returns a single
   match** — and reordering any step in an identity/binding ladder requires re-checking
   every case the earlier order served, including the case whose only evidence is absent for
   one media type (a show has no size). Adding a step to a ladder means testing every prior
   branch that step now preempts.
2. **A path-similarity comparison never consumes either side's mount root.** Shared-suffix
   depth is evidence only below each side's root; a match on a container root name is a
   coincidence, not identity.
3. **A schema-shape check compares the full column shape** — `(name, type, notnull)` at
   minimum — never names alone. A migration or rebuild triggered by a nullability, type, or
   default change must be tested against the *actual* legacy shape a real upgraded install
   carries, not a convenient older one.
4. **Never re-collapse an `Unknown` to a sentinel on the way to storage.** If the fact layer
   models "not reported" as `None`, the column it lands in is nullable and every consumer
   handles NULL. `x or 0` on a size, count, age, or score is a blocker.
5. **A count, cap, or byte total that backs a confirmation phrase is computed only from
   confirmed values.** An item with no confirmed size is excluded from the plan or blocks
   it; it is never counted as zero. (Sharpens rules 5 and 30.)
6. **An interlock's guarantee is proven by its arithmetic before it is written down.**
   Before a comment claims a downstream check catches a case, substitute the values and
   verify the branch actually fires. `_grew_materially(0, live)` does not flag growth below
   the drift floor. (Sharpens rules 7 and 24.)
7. **A staleness or liveness check reads the signal for the thing it names.** "The ingest
   ran" is `synced_at`; "somebody watched something" is `MAX(watched_at)`. Degrading a scan
   on the wrong one makes a quiet library indistinguishable from a broken one.
8. **A function whose docstring says it must not raise catches every exception its own body
   can produce** — including the ones outside the `try`, and including `AttributeError` from
   a shape that is valid JSON but not an object. Validate shape with `isinstance` before
   calling methods on decoded data.
9. **A recovery notice renders on the load it explains.** A warning nested inside a
   conditional container (a savebar, a dirty gate, a disclosure) is unreachable in exactly
   the state it exists for. Render safety and recovery notices unconditionally, and include
   every recovery flag in whatever gate offers the fix. (Sharpens rule 36.)
10. **Every operator-visible copy string is rendered through its actual display path before
    it ships.** A backend string that reaches a frontend rewriter (`compactSpan`,
    truncation, prefix-stripping, sentence interpolation) is tested end to end. A regex that
    rewrites units matches only a unit that follows a number.
11. **A string-prefix parser handles every lane that can produce the string.** Stripping a
    protect-lane prefix from an abstain-lane chip is a category error; enumerate the lanes.
    (Sharpens rule 23.)
12. **Two modules never hold the same literal.** A cross-module comparison by string equality
    imports one constant from one place. A comment asking another file to "stay in step" is
    the bug, not the mitigation.
13. **A test never asserts on source text.** No `Path(...).read_text()` grep for a code
    construct, no `hasattr` as a stand-in for behavior. Drive the real function and assert
    the observable outcome. Anchor any path a test does read on `Path(__file__).parent`.
14. **A behavior-changing commit lands with a test that fails when it is reverted.** Before
    committing, revert the change locally and confirm the suite goes red. If it stays green,
    the test does not pin the behavior.
15. **A tolerance in a test states the property, not a convenient bound.** `abs(a - b) < 1.0`
    over one fixture is not a preservation proof; assert the decision (`decide_verdict`) is
    unchanged, and vary the dimension the property actually depends on (the *count* of
    signals, not just their sizes).
16. **A fixture that pins order-independence contains the adverse order.** If the naive
    implementation passes the fixture, the fixture proves nothing.
17. **A tuple or list documented as exhaustive is derived, not hand-written.** Enumerate from
    the source of truth (`fields.BY_KEY`) so a new field cannot silently leave the sweep
    behind; if an entry is deliberately excluded, name it and say why.
18. **A regenerated fixture is diffed for behavior change, not just regenerated.** A fixture
    rebuilt from the code blesses whatever the code does; state in the commit which values
    moved and why.
19. **Deleting a call site deletes the callee.** Dead parsers, dead helpers, and their
    imports and lookup tables go in the same commit — especially a parser of operator copy,
    which is what the change was removing.
20. **A shared hook or shell that claims to be the single implementation is the single
    implementation.** Adding one means migrating every existing copy in the same change, or
    naming the exception in its header comment. A cache-invalidation list that says "written
    once, here" includes every query key the mutation's server side touches.
21. **A number with a unit is `QuantityInput` or `FixedQuantity`; a visible binary is
    `Segmented`.** When a consistency pass touches a row, it converts the row's controls, not
    just its `aria-label`. Before closing such a pass, grep the changed files for
    `<input type="number"` and for every `<select>` with exactly two `<option>` children.
    (Enforces rules 40 and 41.)
22. **A warning renders inside the container holding the control it is about.** Check the
    closing tags, not just the source order; a notice after `</div>` is in a different box on
    screen. (Enforces rule 42.)
23. **An accessible name goes on an element whose role can take one.** `aria-label` on a
    bare `<span>` or `<div>` is ignored; add a role or use a visually-hidden span.
24. **A flow duplicated across two screens has the same affordances on both.** If one has a
    fallback link, a cancel, and an error channel, so does the other — and a popup opened
    after an `await` is assumed blocked, so the URL is always rendered as a link too.
25. **Copy that names a mechanism is true in the current state.** "Retrying…" only when a
    retry is scheduled; a remedy ("turn off the certificate check") only in the branch where
    it is the right remedy, and never without naming what it costs. (Sharpens rules 21
    and 25.)
26. **Every option in a picker does something different from its neighbors.** If two values
    produce identical output, one of them is deleted.
27. **A dropdown-driven action that stages state must be reachable when its value is already
    selected.** A sentinel option that is also the current value fires no `onChange` and
    strands the operator.
28. **Applying a preset produces a valid, savable draft.** If a validity rule spans two parts
    of a document (built-in signals and custom rules), a preset resets or rescales both to
    satisfy it — never one half, leaving the other to break the invariant.
29. **A rendered comparison shows both sides at a precision that can distinguish them.** A
    lossy formatter may render a measured value; the operator's own threshold is echoed back
    in the units they typed it in.
30. **A comment or docstring displaced by an insertion is moved with the code it documents.**
    After inserting a component or function between a doc block and its subject, re-read both
    blocks; a name that stops matching its arguments is renamed in the same change.
