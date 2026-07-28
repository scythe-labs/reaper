# Candidates that did not survive verification

Review candidates that were raised by a reviewer and **killed by an independent verifier
reading the same code**. Read this before a review; do not re-raise what is here.

**A refutation is bound to a commit, not to the repo forever.** Each entry records where the
code stood when it was refuted. If the cited code has changed since, the refutation is stale
and the candidate is live again — re-verify rather than assuming either answer.

Append here whenever a verifier kills a candidate. An entry that is later found to be *wrong*
— the defect was real after all — moves to the bottom section, so the record shows the miss
instead of hiding it.

## Refuted at `d3c3839` (2026-07-24, the fifth review pass)

Carried forward from `docs/history/CODE_REVIEW.md`, which is frozen. 45 candidates were raised
in that pass, 37 survived, and these 8 did not.

| Area | Candidate | Files it concerned |
| --- | --- | --- |
| season-path | Every spare/override click full-scans the entire, never-GC'd candidate table because `group_key` is unindexed | `services/season_pruning.py`, `db/models.py` |
| engine | A gate missing from `PolicyBody.gates` is silently not run and cannot be warned about; an empty gates tuple validates and removes every built-in protection | `engine/policy.py`, `engine/gates.py` |
| engine | `facts_from_dict` raises on any stored `facts_json` written before a `_OBS_FIELDS` entry or a `GateId` value existed, 500-ing the simulator instead of falling back to a fresh scan | `engine/facts_codec.py` |
| engine-identity | `identity.py`'s design rationale claims the single production join is reachable from the backtest and the planner; neither module references identity at all | `engine/identity.py` |
| api | The API-key lane may write `/api/profile`, so a header-only credential can turn off the run caps interlock and lower the grace window, contradicting the same block's stated rule that all setting changes stay behind the browser | `api/middleware.py`, `api/settings.py` |
| services-misc | A stored instance API key can be shipped to any host by editing `base_url` and pressing Test, defeating the module's stated write-only invariant | `services/instances.py` |
| infra | `PRAGMA synchronous=NORMAL` makes the deletion journal non-durable across a host crash, contradicting the durability the `ActionStep`/`StepState` docstrings and rule 26 assert | `db/session.py:33` |
| infra | Seerr paging advances the cursor by the requested page size, not by the number of rows the server actually returned, so a clamped or short page silently skips records | `clients/seerr.py` |

## Refuted at `59188c9` (2026-07-26, reviewing the season-prune commit `1b10c8c`)

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| executor | `measured` is typed `dict[int, int \| None]`, so `live_sizes` can hold `None` and the growth check sums it | `executor.py:1897` skips the whole season when any size is `None` and an approved size exists, and `:1904` filters `None` out of the sum. Both branches are reached before any delete. |
| executor | `_finalize_plex`'s new docstring claims a movie rescan is scoped to the movie's own folder, but the movie path may pass a root folder | `executor.py:1777` passes `movie["path"]`, which in Radarr is the movie's own folder, falling back to `folderName`. The claim holds. |
| executor | A flat season layout makes `_common_parent` return the series root, so the fix does nothing there | Correct, and correct *behavior*: when every episode sits directly in the series folder, the series folder genuinely is the narrowest directory holding the deleted files. There is no narrower true scope to pick, so this is the right answer rather than a gap. |

## Refuted at `be72828` (2026-07-26, verifying the open findings from the safety review)

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| services-history | `history_sync.py:383`'s `rows = page.get("data") or []` then `if not rows: break` truncates the watch mirror, making items look more dormant than they are | Three independent controls. `_check_regression` runs the same call shape first and raises `HistoryRegressionError` (a `RuntimeError`, so `scan_runner`'s `IntegrationError` handler does not swallow it); a mirror truncated to nothing degrades via `snapshot.py`'s `mirror.earliest is None`; and the direction is inverted anyway, because a truncated walk raises the horizon toward now, which SHRINKS observed dormancy. `INSERT OR REPLACE` with no deletes means an existing mirror cannot lose depth. |
| services-index | `library_index.py:165`'s same shape silently drops items from the index | A missing item has no `rating_key`, so its facts are Unknown, it abstains, and the executor spares a keyless item. The plexapi sweep in the same function unions the missing rows back in through a complete-or-raise pager. No condemn path exists. |
| engine-gates | `UnmanagedGate` (`gates.py:570`) can never PROTECT, so its half of `STRUCTURAL_GATES` is unreachable | **Not refuted as dead code** — confirmed, every producer of `facts.is_managed` is a hardcoded `Known(True)`. Refuted as a *safety* finding: unlike the retired `OthersWatchingGate`, whose input was never gathered so its ABSTAIN was a fabricated check, `is_managed` is genuinely observed and its "Managed by Sonarr or Radarr" line is true. The candidate set is built from the *arrs, so no unmanaged file can enter it. Rule 38/117 hygiene, no reachable wrong outcome. |

The first two were filed as #69 off the #60 fix and explicitly marked unverified. Verifying them
turned up a genuine twin they had missed, `clients/seerr.py`'s request and user walks, where the
same coercion DID convert a partial read into a confident `Known(value=False)` that withdraws a
protection. Fixed in `0dea343`. **The lesson is the one worth keeping: the rule 72 sweep was
right to run, and wrong about which sibling mattered.** Grep found three sites with the same
shape; only the one nobody had named was dangerous.

## Refuted at `f772a44` (2026-07-26, reviewing the gate-retirement commit)

Three lanes fired (`safety`, `seam`, `diff`). These candidates were raised and died on
verification; the survivors were fixed in `f1229ba`, `218f919`, `bf06199`, `653fdbb`.

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| policy | The new `_drop_retired_gates` validator sits mid-class above the field declarations and mutates a frozen model, so ordering or construction breaks | Confirmed at runtime: the `mode="after"` order is `_pin_to_the_running_scorer` → `_drop_retired_gates` → `_weights_total_one_hundred` → `_no_duplicates`. Nothing before it reads `gates`; the two after it read the cleaned tuple, which is the safe direction. Field order is unaffected, and `model_validate`, `model_validate_json` and direct construction all run it. |
| policy | `model_copy` skips the validator, so a retired gate can be reintroduced in code | True of `model_copy` and pinned by an existing test, but `PolicyBody.model_copy` has zero call sites in `src/`. No production path reaches it. |
| seam | The policy editor can still offer the retired gate, so a switch appears that the backend silently deletes | `PolicyEditor.tsx:1358` iterates the served `draft.gates`, never `GATE_META` keys or the `GateId` enum. All four load paths and the save path run the validator, so it cannot survive a round trip in either direction. |
| seam | A stored `unmanaged` explanation stops rendering because a hop narrowed its type | Every hop is `str`-typed with no narrowing: `snapshot.py` writes `r.gate.value`, `GateOutcomeOut.gate` and `GateCountOut.gate` are `str`, `api.ts` types it `string`, and `WhyPanel` renders the backend's `detail`. The round trip is intact. |
| seam | `policy.inspect()` dropping a warning breaks a count, index or snapshot on the warnings surface | The editor anchors warnings by a `field.startsWith("gates.")` predicate and computes the unanchored stack as "matched by no predicate". Both are index-free and count-free, and no test asserts the list length. |
| safety | Removing `UNMANAGED` from `GATE_TYPES` and the defaults leaves another consumer inferring something wrong | Checked every one: `build_gates`, `inspect()`, `popularity_window_days()`, `rating_on`. None reads the gate's presence or absence, and no fact builder gathers `is_managed` conditionally on it. |
| safety | The moved `policy_hash` lets something proceed on a stale policy | Every consumer fails closed. `executor.py:859` raises `ExecutionError` on mismatch, checked in the dry run too, and `simulate` falls to a zeroed tier rather than serving stale numbers. No code looks a policy row up *by* hash, so the stored column going stale is inert. |
| infra | `scripts/policy_lab_extract.py`'s `from_gate("unmanaged", False)` silently inverts once the gate is gone, drifting the generated fixture | An absent gate falls to the `not fired_means` branch and still yields `is_managed=True`. All 440 vector entries are `known/true`; no drift. |

## Refuted at `6a6231e` (2026-07-26, reviewing the prettier adoption `b082f86` / `b64ffbd`)

Three lanes fired by path (`safety` on `DeletionToggle.tsx` + `ReapConfirm.tsx`, `seam` on
`api.ts`, `diff` on the rest), but the src half was a whitespace-only reformat, so the review
question was the narrow one: **did any semantics move?** That is answerable mechanically, and
answering it by reading 2,973 reflowed lines would have been both slower and weaker. Comparing
the TypeScript AST before and after — positions and trivia excluded — left 16 files differing,
and every one reduced to an inert class. **The generalizable lesson: on a reformat commit, the
first three checkers you write are all wrong in the same direction**, reporting their own
normalization gaps as findings. Refuted candidates below are the checker's bugs, not the diff's.

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| frontend | Comment prose changed in `ServiceModal.test.tsx` and `Settings.tsx` — prettier must never reword a comment | The checker's `\/\/[^\n]*` matched the `//` inside `http://10.0.0.9:5055` and `https://reaper.example.com`, so reflowed *string and JSX* text read as comment text. Extracting comments with the real scanner: prose byte-identical in all 55 files. |
| frontend | `index.css` differs semantically | The normalizer collapsed newlines to spaces without stripping them adjacent to `(`, so `radial-gradient(\n  38%` compared unequal to `radial-gradient(38%`. Whitespace inside a CSS function paren is insignificant. Clean once stripped there while deliberately leaving spaces around `:` and between selectors, where CSS does give them meaning. |
| frontend | A string literal changed in `docs/content/understandingPolicy.ts` | Prettier flipped the delimiter to `'…'` because the string contains `\"Update while read-only\"`, and single quotes drop two escapes. Compared by `node.text` the value is byte-identical; only the source spelling moved. |
| safety | JSX whitespace moved between a `JsxText` node and an explicit `{" "}` in 8 files including `ReapConfirm.tsx`, so rendered operator copy may have gained or lost a space | The one class here that could really have changed what an operator reads, so it was rendered rather than reasoned about: re-implementing Babel's `cleanJSXElementLiteralChild` and concatenating each file's visible text gives a byte-identical string in all 43 `.tsx` files. |
| frontend | Added `SemicolonToken` in type members, added `ParenthesizedExpression` wrappers, a dropped leading `|` on the `RatingSource` union | Three separate spellings of the same non-event: a trailing member separator, parens around an expression, and TS's optional leading union bar are all inert. `tsc --noEmit` and 424 vitest tests agree. |

## Refuted at `60e035a` (2026-07-26, reviewing the Scales phone-layout commit)

One lane fired (`diff`, `frontend/src/index.css`). The commit reflows `.fair-card` into a grid
below 640px so the balance bar reaches the card edge.

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| frontend | The new media block's selectors are unscoped (`.fair-avatar`, `.fair-balance`, `.fair-legend`, `.fair-row1`, `.fair-watched`), and `ScalesPanel.tsx` reuses `.fair-avatar` (:231) and `.fair-balance` (:247), so the panel's header and bar get grid placement meant for the card | The declarations really do compute on the panel — measured `grid-column: 2/-1` on the panel's bar at ≤640px against `auto/auto` at 900px — but they are inert, because grid placement only applies to a grid item and neither parent is a grid: `.scales-head-id` is `display: flex` (`index.css:3720`) and `.block` is `display: block` (`:1983`). Rendering the panel at 320/390/500/640px against the stylesheet with and without the block gives identical geometry in every column (`barW`, `barRightGap`, `whyOverflow`, name overrun). A latent hazard if either parent ever becomes a grid, not a present defect. |
| frontend | The grid reflow makes the phone card taller, trading the bar fix for lost vertical density | Backwards. Measured at 390px the cards got *shorter* (180→147, 199→180, 217→180), and at 320px markedly so (277→180, 275→180, 296→180), because dropping the duplicated chip removes a whole row. |

## Accepted by the operator at `a454a9f` (2026-07-26, reviewing the section nav, #71)

Not refutations. These survived verification and were then judged not worth acting on, with the
reason. A later pass must not re-raise one as new, and must re-open it only if the reason below
stops holding — which is why the reason is recorded rather than just the verdict.

| Lane | Finding | Why it stands unfixed |
| --- | --- | --- |
| safety | A **hung** safety read (not a failed one) leaves the section nav's armed dot in its pending state, which draws identically to "deletion is off" | Verified PLAUSIBLE, never CONFIRMED: it needs a request that neither resolves nor rejects, so it is bounded to the initial fetch, and `fetchApi` sets no timeout. The operator's ruling is that the dot going quiet costs visibility, not safety, because nothing downstream will act on that state: the executor re-reads the armed flag before every item (`executor._still_armed`, called at `executor.py:1175` through the `armed_recheck` the execute route injects at `api/runs.py:567`), and `ReapConfirm` derives `armed` from `destructive_enabled === true`, so a pending or errored read leaves Execute disabled through all three states. `SafetyBanner` carries the identical `isLoading` branch and predates this change, so a fix is one edit to both twins (rule 72) and belongs with a request timeout in `fetchApi`, not with the nav. |

## Refuted at `c73e959` (2026-07-26, reviewing the narrow-phone overflow fix)

One lane fired (`diff`, `frontend/src/index.css`). Three candidates survived and were fixed;
these were raised and died. **A process note worth more than the table: the reviewer's first
read called the surviving rail finding *pre-existing and narrowed by the change*, and it is the
opposite — comparing against `git show dev:frontend/src/index.css` in the same harness showed
the change introduced it.** A before/after claim about a layout is cheap to test and easy to
get backwards by reasoning; render both.

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| frontend | The new media blocks use unscoped selectors (`.tab`, `.seg`, `.settings-tab`), so they restyle components the change was not aimed at — the hazard raised at `60e035a` | Every consumer is a pill track or rail where less padding is the intent: `.tab` only `ReviewQueue.tsx:1973`, `.seg` only `Segmented.tsx:29`, `.settings-tab` only `Settings.tsx:2084` and `PolicyEditor.tsx:1200`. `App.tsx:102` uses `view-tab`, a different class with its own rules. The `fill` variant is floored by `min-width: 5.25rem` and measures 176.8px at every width. The Settings rail is not sticky, so it has no scroll-margin coupling to break. |
| frontend | `1fr` -> `minmax(0, 1fr)` converts "the page scrolls sideways" into "a child overflows a clipped ancestor and is unreachable" — rule 138 reached from inside | No `overflow: hidden/clip` on `html`, `body`, `#root`, `.app`, `main`, `main.split` or `.editor`. The only clipped containers inside the newly capped column are `.rules-table` and `.card`; measured with long realistic rows down to 320px, `scrollWidth === clientWidth` and every Remove button stayed inside, because `.rules-row`'s `auto` tracks fall back to min-content and wrap. |
| frontend | `.qty` moving from `inline-flex` to `flex` shifts the baseline between the number and its unit | A grid item is blockified regardless, so the used value was already `flex`. Measured identical geometry. |
| frontend | The 400px pace block is beaten by the 640px block above it, or by the base `.qty` rules that sit later in the file, so the fix silently does not apply | The later base rules lose on specificity, and the 640px block is beaten because the heavier `.qty-narrow` selector is repeated at equal specificity inside the 400px block. Measured widths match intent at every breakpoint. |
| frontend | `min-width: 2.9rem` makes the four-digit fit worse than before | Backwards. At 320px the "1,000" box goes from 59.2px wide holding 67px of content on `dev` (7.8px hidden) to 57.5 holding 61 (3.5px hidden), and from 360px up it now fits outright where `dev` did not. The floor only binds below a ~300px viewport. |
| frontend | The unit `<select>`'s right edge is clipped by `.qty`'s `overflow: hidden` at <=322px, the same defect as the "MB"/"ME" clipping the author already fixed | The ~2px clipped is border, not copy: the select's text measures 55px inside a 56px box, so every option still reads in full. |

## Refuted at `c8b0ddc` (2026-07-26, the "does any other string claim a play?" sweep)

Five lanes ran (`diff`, `seam`, and three themed sweeps) after an operator caught the review
queue asserting "Kept · watched too recently" about a title with zero plays all time. The class
hunted was **an operator-facing string asserting a fact the evidence does not establish**. Two
independent agents killed the lead candidate, which is worth recording precisely because it
looks like the fixed bug and is not.

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| engine-signals | `signals.py:271`'s `"not watched in {span}"`, and its consumers `routes._dormant_for` → the amber pill `Not watched in {span}` (`ReviewQueue.tsx:331`), assert a prior play on a never-played title exactly as the fixed chip did | **The distinction that decides the whole class: "watched too recently" is true only if a play exists; "not watched in X" is true whether or not one does.** The string states an ABSENCE, and where the span runs from arrival rather than a play it is still literally true. The coverage direction is safe too: `reference_instant` returns `last_played or max(added_at, horizon)`, so the reference is always ≥ the horizon and the span can never reach back past the evidence. Every divergence from the deeper truth *understates* dormancy (a 10-year-old file behind a 1-year mirror reads "not watched in 1 year"), which is the keep direction. |
| api-simulate | The frozen `distinct_watchers` re-phrased under a draft popularity window would repeat the popularity-coverage finding in the simulator | Tier 3 refuses to simulate at all when the edit touches a watch window or any gate, so `policy.popularity_window_days()` in `_replay_simulation` always equals the scan's, and no per-item detail is returned regardless. |
| frontend-queue | `ReviewQueue.tsx:328` `DormantPill` — the closest visual twin of the fixed chip | Same reasoning as the lead: literally true for a never-played title, and the horizon clamp only ever understates. Only the implicature of a prior play is wrong, and that is not this class. |

**The generalizable lesson, and the reason this entry is long:** the sweep's most obvious
candidate was the one that shared the fixed bug's *subject* (dormancy) rather than its *defect*
(asserting an event that may not have happened). Searching by subject surfaced a false positive
two agents had to argue down; searching by defect found the real twin thirty lines below the
fix, in the same function, under a different gate. Hunt the assertion, not the topic.

## Refuted at `394cc3a` (2026-07-27, reviewing the history-reach commit, PR #81)

Three lanes fired (`safety` on gates/signals/snapshot, `seam` on `api/routes.py`, `diff` on the
rest). **A process note worth more than the table: all three lanes independently reported the
same comment defect** -- the new block's comment cited `verdict.block_holds_reap` as what makes
the `could not check` wording hold a hand reap, and that function reads the prefix only for a
*deferrable* gate. Convergence across independently-prompted lanes was the signal it was real,
and each lane had measured it in-process rather than reasoning about it. Fixed in `1b1458c`,
`951e2ef`, `69d69f8`, `e0c7ca5`.

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| safety | `horizon` can be `None` at any of the three new `history_reach_days(...)` sites, crashing the scan | All three parameters are non-optional `datetime`. `snapshot.py` resolves `mirror.earliest is None` to `utcnow()` *before* building the `ScanContext`, and `season_scan`/`backtest.facts_as_of` take `horizon: datetime`. `Snapshot.horizon_at` is `nullable=False` in the frozen baseline, so the simulate route cannot see a `None` either. |
| safety | The replay backfill computes a reach *deeper* than the scan's own, re-opening the bug inside the simulator | Inverted. `Snapshot.created_at` is stamped *before* the judge loop, so a per-item reach is >= the replay's, i.e. the replay blocks at least as often. Keep direction, and three tests pin both arms plus the never-overwrite rule. |
| safety | A negative or absurd reach (clock skew, a future horizon, an epoch-0 row) breaks the gate | Negative falls to `reach.value < window` and blocks; `humanize_days(-1)` is "less than a day". `history_sync` drops a row whose date and started are both falsy, so epoch 0 cannot enter, and a deep-but-wrong horizon merely fails to *fix* rather than regressing anything. |
| safety | The new `blocked=True` distorts something that counts blocked gates | Every consumer is boolean or list-shaped, none a count: `Evaluation.blocked`, `reap_held_by_blocks`, `reap_override_verdict_decoded`, `routes._has_blocked_protections`, `WhyPanel.LeftForYou`. `breakdown.hand_reaped_held` rises, which is the reported-not-silent direction it exists for. |
| safety | The block widens something on a season, or fails to propagate | Seasons are their own `Candidate` rows with their own Facts; episodes are never judged individually; `season_pruning` reads no gate results. An unresolved season's `distinct_watchers` is `Unknown`, so `_blocked` fires before the reach check. |
| safety | The executor or planner re-derives a verdict and could disagree with the block | Neither imports `evaluate_all`/`decide_verdict`, and no Facts reaches the executor. Blocked -> abstain -> not planned. The change cannot reach a send. |
| safety | The PROTECT branch needs a reach check too | Sound as written. Every mirror row is at or after the horizon, so the count is a valid lower bound; `count >= floor` on a lower bound implies the true count clears the floor. No over-count path either. |
| safety | A fourth `_watch_stats`-shaped query was missed (rule 72) | Checked all of them: `fairness.py` is display-only and unwindowed, `calibration.py` windows from its own cutoff, `backtest._plays` takes all plays, and `season_scan`'s mid-binge guard already fails closed on a missing row. **The real miss was not a query but a *reader*** -- the finding behind rule 140. |
| safety | `history_sync.days_since_horizon` is a second, differently-rounded implementation of `dormancy.history_reach_days` (rule 3/22) | Pre-existing and dead: zero callers, so no divergence is observable. Worth deleting on the next touch, not a finding. |
| safety | `Facts.history_reach_days` becomes operator-authorable, or breaks the codec | `fields.REGISTRY` is hand-written and excludes it. `facts_codec` picks it up from the `Observation[float]` annotation, so it freezes and thaws, and an older row gets `Unknown`, which the gate reads as un-checkable. |
| seam | `simulate` 500s or serves a wrong tier when `horizon_at` is NULL or predates the column | `nullable=False` in the frozen baseline, and `_latest_snapshot` does a full entity load, so there is no deferred-column IO either. |
| seam | The new detail falls through `WhyPanel`'s `/^could not check (.+?): (.+)$/` and blanks the panel | It parses cleanly: check = "who watched it in the last year", cause = the reach clause. Both halves miss `CHECK_COPY`/`CAUSE_COPY` and take the documented raw fallbacks. |
| seam | An unmapped cause renders as a lowercase bold heading, unlike every mapped one | Real but pre-existing: `CAUSE_COPY` already lacks "no IMDb id to look up" and two others, so `rating_floor` on a movie with no IMDb id does this today. |
| seam | `routes.py`'s `if "could not check who watched" in detail` now matches the popularity detail and emits the season chip | Unreachable: the enclosing guard skips any detail *starting* with "could not check", and the inner branch additionally requires `gate == "season_progression"`. |
| seam | `_kept_phrase`'s `_WATCHED_HERE_RE` no longer matches | The PROTECT branch's wording is byte-unchanged; the reach check sits strictly below it. |
| seam | `_dormant_for` breaks on the reworded low-watchers detail | It reads the `unwatched` signal's "not watched in " prefix; the reword touched only `few_watchers`, whose detail no consumer in either tree parses. |
| seam | `GateOutcomeOut`/`api.ts` narrow the gate id, or a `Record<GateId, ...>` lookup misses a blocked `server_popularity` | Both sides are `str`. `GATE_META` is read only for `protected_by`, with a `titleCase` fallback, and a blocked gate never appears there. |
| seam | The gate's PROTECT detail claims "in the last year" on a mirror covering three months | Literally true (three months is inside one year) and the file is kept. The absence-vs-asserted-event distinction landing on the safe side. |
| seam | The `"this scan did not record how far back your watch history goes"` arm is dead | Correct that it is unreachable today (every builder sets the field and the replay always fills it), but it is the fail-closed default for an unforeseen builder, not a claim about a safeguard. |
| diff | `history_reach_days(horizon, now=cutoff)` can go negative when the cutoff precedes the horizon | Unreachable. With the horizon after the cutoff there are no plays at or before the cutoff, so `dormancy_days` goes negative and `facts_as_of` returns `None` before any Facts is built. Verified by direct call. |
| diff | `humanize_window(covered)` mis-renders a non-integer or sub-1 `covered`, and `covered >= 1` leaves a gap at 1.4 | `history_reach_days` returns `int` and `window_days` is `int`, so `covered` is integral on every production path. Sub-1 routes to "in the history Reaper holds". |
| diff | `_policy_lab.mirror_reach_days` derives the reach from items, so it can claim depth the mirror lacked | It is a genuine floor: a play logged N days ago proves a row that old, so the max can only *understate*, which makes the gate block more. The fixture's value sits far above every window the suite sweeps. |
| diff | `lru_cache` on a function reading the fixture leaves process-global state (rule 133) | Pure function of a committed read-only JSON file, one cache per xdist worker, nothing mutates it. |
| diff | Rule 68 -- the fixture generator needs a matching change and a drift test for the new field | `scripts/policy_lab_extract.py` computes its baseline *through* `tests._policy_lab.to_facts`, so it picks the field up automatically. The pinned-baseline test is the drift guard and is green. |
| diff | Rule 35 -- a fact builder was missed | All four `Facts(` sites in `src/` are covered (`snapshot`, `season_scan`, `backtest`, the codec thaw). `calibration.py` constructs no Facts. |
| diff | The two new builder tests sample `utcnow()` twice and compare against a fixed day count (rule 133) | Production's sample is microseconds later, so the day count is unconditional. The flake rule 133 guards against cannot occur here. |
| diff | `docs/STATUS.md`'s "masked by the shipped 1095-day dormancy floor" overstates the masking | Verified: dormancy is clamped to the reach, so condemning under a 1095-day floor requires a reach of at least 1095, hence a window fully spanned. The new branch genuinely cannot fire on shipped defaults. |
| diff | `services.history_sync` does not actually "name it that" (the reach), per the new Facts docstring | It does: "where :func:`horizon` is the reach question." |

## Refuted at `4e069b1` (2026-07-27, reviewing the expand-seasons-mode commit, #82)

Two lanes fired by path (`seam` on `api/settings.py` + `api.ts`, `diff` on the rest); no `safety`
lane, since nothing in the safety file set changed. The commit turns a boolean display setting
into a four-value per-screen mode and hoists the 900px media query into a shared constant. **Both
lanes, run independently, returned the same two findings and nothing else** — both stale-comment
defects around the hoisted breakpoint, and neither reachable by any gate. The seam itself came
back clean in both directions.

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| frontend-queue | The "reads it live" claim is false: `expandSeasonsByDefault` only seeds `useState`, so changing the mode or crossing the breakpoint does nothing until a reload | It is consumed as BOTH, and the second half is what makes the claim true. `ReviewQueue.tsx:2308` passes it as `defaultOpen`; `ShowCard` seeds `useState(defaultOpen)` at `:1105` *and* re-seeds in `useEffect(() => { if (!touched.current) setOpen(defaultOpen) }, [defaultOpen])` at `:1111-1113`. The dependency is exactly the value that changes, `ShowCard` is unmemoized inside an inline `.map`, and `useMediaQuery` holds a live `change` listener (`:32`). `touched.current = true` is set in the season pill's `onToggle` (`:1245`) for BOTH directions, so a hand-opened *and* a hand-closed card keep their choice. The comment holds as written. |
| seam | `ReviewQueue.tsx` and `queueSettings.tsx` are now two subscriptions to general settings that can disagree | Identical key `["general-settings"]` at `ReviewQueue.tsx:1734`, `queueSettings.tsx:68`, `Settings.tsx:134`/`:1367`, `App.tsx:704`. React Query dedupes on the key: one cache entry, one fetch, no second key to drift. |
| seam | A successful `put_general` leaves the queue's copy stale | `Settings.tsx:177` writes `queryClient.setQueryData(["general-settings"], data)` with the canonical server response, notifying every observer of the shared key. |
| seam | On a 422 the `<select>` shows a mode the server refused | Controlled straight off the query cache, which is written only in `onSuccess`; a failure leaves the stored value and renders `save.error` (`Settings.tsx:704`). A 422 is unreachable from the UI anyway — the four `<option>` values are exactly `ExpandSeasonsMode`. |
| rule 64 | Something still refers to the old boolean shape | Full-repo grep for `expand_seasons_default`, `expandSeasonsByDefault`, `expand_seasons`, `ExpandSeasons`, `EXPAND_SEASONS` across `src/`, `frontend/`, `tests/`, `docs/`, `scripts/`, `alembic/`, `.env.example`: the only surviving old spelling is `EXPAND_SEASONS_MODE_KEY = "expand_seasons_default"`, the storage key, kept deliberately so no migration runs and documented as such at `app_settings.py:97-100`. `get_expand_seasons_default` and `EXPAND_SEASONS_DEFAULT_KEY` have zero remaining call sites. |
| seam | The settings export / backup / restore path carries the old boolean shape | There is no per-key settings export. `services/backup.py` copies the whole database file, so the row rides along untouched; `services/restore.py` names exactly one app-setting key (`DESTRUCTIVE_KEY`, `:57`). Nothing enumerates or reshapes app settings. |
| seam | The API-key lane serves or writes a stale general-settings shape | One route pair over the same two models. Reads are open to a key (`middleware._API_KEY_READ_DENY` lists only the key reveal, backup download and logs), writes are denied (`_API_KEY_WRITE_ALLOW` excludes settings), so the key lane gets the same `GeneralSettingsOut` and cannot write the mode at all. |
| backend | `_get(..., default=None)` misbehaves, or the `isinstance` ordering lets a stored value fall through wrongly | `_get` (`app_settings.py:145`) returns the default only when the row is absent, else `json.loads`. Every JSON shape is covered: `bool` → `both`/`off`, `str` → identity lookup falling back to `off`, and `null`/int/float/list/dict all reach the final `return DEFAULT_EXPAND_SEASONS_MODE`. `bool` before `str` is inert rather than load-bearing (`bool` subclasses `int`, not `str`, and there is no int branch). Pinned by the parametrized test at `tests/test_general_and_logs.py:164-181`. |
| backend | `_EXPAND_SEASONS_BY_NAME`, an identity dict, hides a defect a membership test would not | A type-narrowing idiom, not logic: `.get(stored, DEFAULT)` returns `ExpandSeasonsMode` where `stored in EXPAND_SEASONS_MODES` would leave `Any`. Behaviorally identical; `mypy src/reaper` clean. |
| backend | A downgrade breaks the setting: an older build reads `"off"` as `bool("off") is True` and expands every show | The trigger is real but the direction is unsupported repo-wide (additive-forward migrations, frozen baseline), and it costs a display preference only. The forward direction — the one that ships — is handled and tested (`True`→`both`, `False`→`off`, `"tablet"`/`3`→`off`). |
| rule 103 | `api.ts`'s `ExpandSeasonsMode` union and Settings' four `<option>`s are unguarded hand copies of the backend `Literal` | Real, but the codebase's standing convention rather than this commit's regression: `api.ts` hand-mirrors eight other backend sets (`Verdict`, `ShowStatus`, `Override`, `SignalState`, `InstanceKind`, …) with no drift guard anywhere, and no endpoint serves the mode list, so rule 66 does not bind. The backend's rule-103 claim is scoped to its three derivations and is true of them. |
| frontend | The new `<select>` skips the shared control standard (rules 40/41) or renders unstyled beside the Theme picker | Neither select carries a `className`; both are styled by the container selector `.set-row .set-control select` (`index.css:7099`, focus at `:7114`, narrow-screen block at `:7132`), and the new one sits in the same `.set-control` inside a `.set-row`. Identical chrome. Four options is exactly rule 41's sanctioned segmented-to-select growth, and `Switch` remains the on/off control elsewhere in the panel (`Settings.tsx:656`, import still live, `eslint` exits 0). |
| frontend | The controlled `<select>` has no `onError` and no optimistic state, so a failed write reverts it silently | Pre-existing and panel-wide: the same `save` mutation backs every row on General, the `Switch` it replaced reverted just as silently from the same controlled `data.*` value, and the panel renders `save.error`. Fixing it belongs to the whole panel (rules 17/36 + 72), not this control. |
| frontend | A third un-hoisted JS reader of the 900px boundary exists (rules 67/72) | Swept `matchMedia`, `useMediaQuery`, `innerWidth`, `clientWidth`, `ResizeObserver`, `visualViewport` and bare `900` across `frontend/src`. The only other JS spellings of the literal are `useMediaQuery.test.ts:43,49,57`, which stub `matchMedia` themselves and would pass with any query string — not readers of the boundary. `popoverFit.ts:67` and `OverrideControls.tsx:104` measure the viewport but read no breakpoint. Exactly two consumers, both from the constant. |
| tests | The new `afterEach(vi.unstubAllGlobals())` tears down a global stubbed outside `beforeEach`, poisoning later tests | The only stubbed global is `IntersectionObserver`, re-applied by the `beforeEach` immediately after. `Object.defineProperty(window, "localStorage", …)` at `:1391` and `setup.ts:10`'s direct `window.scrollTo` assignment are not `stubGlobal` calls, so `unstubAllGlobals` cannot revert them. File passes. |
| tests | The new `matchMedia` stub is too thin for the real hook, or leaks to later tests | The hook uses exactly `matches` (`:24`, `:30`), `addEventListener("change")` (`:32`) and `removeEventListener` (`:33`) — all three present; it never calls the legacy `addListener`. Stub is set in the last test of its describe and cleared by the new `afterEach`. The test also genuinely discriminates: under `mode: "mobile"`, dropping the media-query argument gives `shouldExpandSeasons("mobile", false) === false` and the assertion fails. |
| docs | `docs/STATUS.md` was left with a now-wrong line about the expand-seasons switch | No such line exists. Grepping `docs/` and `.env.example` for the setting returns only `docs/history/UI_REVIEW.md:1096`, which is frozen history and must not be edited. The originating feature commit `fa9fcb9` touched no doc either, so STATUS.md has never tracked this display preference and nothing in it is now false. |
| performance | "Mobile" makes every drawn show card fire `["group", key]` at once on the weakest device | Bounded and pre-existing: the drawn set is `groups.slice(0, visible)` with `PAGE = 40`, the group query carries `staleTime: 5 * 60 * 1000` (`ReviewQueue.tsx:838-840`, the P-2 fix), and the old `true` already expanded on phones. The new `desktop` mode strictly reduces this. |
| safety | Defaulting a show card to expanded changes what a bulk Spare/Reap covers | Selection keys on the group (`isSelected={selected.has(groupKeyOf(group))}`, `:2311`), independent of the card's `open` state. No count or destructive set reads expansion. |

**The generalizable lesson: a commit that hoists a duplicated literal into a shared constant
moves the defect into the prose.** The code half was clean everywhere — one declaration, two
consumers, a pure decision helper with an exhaustive table, a seam that agreed in both
directions. Both surviving findings were comments *about* the hoisted value: one quoting the call
shape the hoist deleted, one crediting 900px with a layout change that lives at 1100px. Neither a
type checker, a linter nor a test can see either, and the second was only findable by reading
every `max-width: 900px` block in the stylesheet and asking what it actually does — which is the
check worth running whenever a comment enumerates what a breakpoint controls.

## Refuted at `ef0278d` (2026-07-27, reviewing the settings control-column branch, PR #88)

Two lanes fired by path (`diff` on `Settings.tsx`, `PlexPanel.tsx`, `index.css`; `seam` because the
save bar changed `saveGeneral` from one field per press to a merged six-field body), run as three
concurrent reviewers. No `safety` lane — nothing in the engine, the services, either transport
guard, the execute route or the arming UI is touched. The CSS lane measured everything in Chrome
against a harness holding all 23 `.set-row`s from the three consumer files, rendered against
`origin/dev`'s stylesheet, the branch's, and the intermediate commit `25d4afd`.

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| frontend-settings | `spareDirty`'s new `data.default_spare_days > 0` gate hides the bar while the operator types their first length, so moving off "Forever" is silently lost | The mode control writes immediately, so the gate can never be false while the box is on screen: the `Segmented`'s `onChange` calls `save.mutate({ default_spare_days: mode === "forever" ? 0 : spareDays })`, and driven from a stored `0`, pressing **Days** sent `{"default_spare_days":30}` and only then did the box render. Every writer of `spareDays` is itself gated on, or reached only when, the stored value is already `> 0`. The gate is redundant, not load-bearing. |
| frontend-settings | `discardDrafts` skipping `setSpareDays` when the stored value is `0` leaves a stale draft the bar cannot show | Same reason: with the stored value `0` the box is not rendered and `spareDirty` is false, so there is no draft to put back. The other five fields are restored to the exact canonical value each dirty-check compares against, and Discard was driven: bar cleared, nothing sent. |
| frontend-settings | `accentBlocks` can strand the operator with a permanently disabled Save and only Discard as a way out | Three ways out, all on screen: the bar's own "Enter a hex code like #25c3ff to save.", the same sentence on the row (rule 42), and the **Reset to default** link, which renders precisely because a half-typed hex is never `DEFAULT_ACCENT`. Driven with the bar naming only the accent; the button re-enables the moment the hex completes. |
| frontend-settings | `proxiesDirty && data.proxy_trust_enabled` silently drops typed proxies when the switch is turned off | Recoverable and unchanged from `dev`: the text stays in the (disabled) box, a later save sends only the other fields, and flipping the switch back on brings the bar back naming it. `git show origin/dev` shows the deleted per-row Save carried the identical guard, so this diff neither introduced nor widened it. |
| frontend-settings | `Object.assign({}, ...pending.map(p => p.patch))` can send an empty body | The button exists only inside `pending.length > 0`, and `pending` is recomputed on the render the click closes over. |
| frontend-settings | The narrow-screen `<select>` loses the `aria-current` the rail carried, or `<nav aria-label="Settings sections">` around a lone `<select aria-label="Settings section">` double-announces | A native select announces its selected option as the current value, and `SettingsNav.test.tsx` pins it. The landmark name and the control name are each correct for their own element and are read at different moments. |
| frontend-settings | Something deep-links to a settings tab by its label or DOM shape, so the swap or the "Backup & Restore" relabel breaks it | The only entry point is `initialPanel`, a `Panel` **id**. Repo-wide grep for the old label finds `docs/history/PLAN-narrative.md` (frozen) and one test that passes the id. |
| frontend-settings | Rule 64: something still names the six deleted per-row Saves | Nothing does. The surviving hits are a comment correctly recording that they are gone, the policy editor's own bar, and the STATUS.md row this PR added. |
| frontend-settings | The `//` line comment newly placed inside the parenthesized JSX expression in `PlexPanel.tsx` breaks the build or gets moved by prettier | It sits in a JS expression position, not JSX children, and is valid there; all four frontend gates pass on it. |
| frontend-settings | The `@media (max-width: 400px) { .settings-tab }` block is now dead, since the settings rail no longer renders below 900px | It governs the **Policy** rail, which shares `.settings-nav`/`.settings-tab` and is deliberately left on tabs, as the new block states in writing. Still live. |
| seam | `put_general` reads every field off the model, so a field the client omits is written as its default | `GeneralSettingsIn` declares all eight fields `\| None = None` and every write is guarded by `is not None`; an existing test already pins that a second single-field save leaves the first alone. |
| seam | A field validated later commits the earlier fields before it refuses | All four validations precede all eight writes, which share one `session.commit()`. Driven: six valid fields plus one bad `application_url` returns 422 and leaves the entire `GET` payload byte-identical. (Now pinned by a test, since only the ordering held it.) |
| seam | Writing `trusted_proxies` depends on `proxy_trust_enabled` being present in the same body | `_refresh_proxy_state` re-reads the *stored* switch rather than the payload, so a list-only patch still arms the live middleware. Driven. |
| seam | A field the client re-seeds from is absent or differently named in the response | `onSuccess` re-seeds five fields and `GeneralSettingsOut` carries all five under identical names, mirrored in `api.ts`. A six-field save round-tripped every value and cleared the bar. |
| seam | The route can refuse a `default_spare_days` the UI can produce, or a cleared number box sends `null`/`NaN` | Pydantic accepts `0..3650`, the box clamps `1..3650`, and `useTypedNumber` returns early on an empty box and ignores a non-finite parse. |
| seam | A post-commit failure leaves all six fields saved while the client is told the save failed | The only two post-commit calls cannot raise: the scheduler reschedule wraps every apply in the rule-87 guard, and `parse_proxy_networks` drops malformed entries rather than raising. |
| seam | The rewritten `GeneralPanel.test.tsx` no longer pins the B-18 regression it claims to | It does: dropping the five `"… " in sent` guards for an unconditional re-seed fails "a control that saves on the spot > leaves an in-progress edit alone" on exactly the assertion the comment names. |
| seam | `SettingsNav.test.tsx`'s `matchMedia` stub cannot discriminate | It discriminates both ways: hardcoding `narrow = false` fails 2 of 3 tests, hardcoding `narrow = true` fails the third. It does not pin the query *string*, but that is the shape already accepted at `4e069b1`. |
| seam | `SettingsNav.test.tsx` leaks its stubbed `matchMedia` into later tests | `afterEach(vi.unstubAllGlobals())` clears it, jsdom defines no `matchMedia` for the stub to clobber, and the full suite runs green in one process with the file present. |
| seam | A pydantic-shaped 422 renders as `[object Object]` | `api.ts` handles the list-shaped `detail` explicitly. The wording is internal-sounding, but no bound and no input attribute changed here, so it is pre-existing. |
| seam | Typing during an in-flight merged save loses keystrokes when `onSuccess` re-seeds | Unchanged by this PR: the per-row Saves re-seeded the same field on the same `onSuccess`, and the text inputs were never disabled on `save.isPending` on either side. |
| css | `.savebar` cannot stick on the Settings page, so the only save affordance scrolls away (rule 43) | Measured in a 700px-tall viewport: computed `position: sticky` throughout, pinned at scrollTop 0 and mid-scroll. The whole ancestor chain from `.panel` to `html` computes `overflow: visible`, `contain: none`, `transform: none` — no scrolling or containing ancestor, and `.settings-body` has no CSS rule at all. The 900px lift resolves to `bottom: 65.6px` (`--navbar-h` + 0.6rem) under the same query that makes `.views` the bottom bar. |
| css | `.set-row .set-control input.input-port { flex: none }` is a descendant selector overriding a direct-child rule, so it may not apply | It applies — (0,3,1) beats (0,2,1) regardless of combinator — and the port field is in fact a direct child. Computed `flex-grow: 0, flex-shrink: 0, width: 80px` at 1200/640/390px. |
| css | Deleting `min-width: 15rem` shrinks the inputs that are not direct children of `.set-control` (hex field, swatch, `QuantityInput`'s number) | Measured identical before and after at 1200 and 640px: the `.qty` number 57.6px both (its own `min-width: 0` already won on source order), `.hexfield` 136px both, the swatch 44.6px both. The one descendant the deletion did reach is `.switch input`, whose width goes 240px → 40px: on `dev` every settings Switch carried a 200px invisible clickable overhang, which this change removes. |
| css | The 640px comment's "593px inside a 350px card" is unverifiable or invented | The measurement is real but belongs to this PR's own intermediate state, not to `dev`: the connection select measures **273px on `dev`, 571px on `25d4afd`, 273px on the branch**, with zero overflow on `dev` at 320/350/390px. The bare `1fr` was harmless on `dev` because the same rule's `max-width: 22rem` clamped the select's min-content contribution. LEARNINGS already records exactly this ("A width cap can hide a collapse"). |
| css | The `.settings-picker` `<select>` forks the control standard or hides a list behind a dropdown (rules 40/41) | It matches the standard property for property; only `width: 100%` and `font-weight: 600` are its own, both declared in the comment, and width is the one dimension rule 40 lets vary. Nine sections is an open list, which is what rule 41 reserves a `<select>` for. |
| css | Something in `.settings` / `.settings-nav` / `.settings-body` assumed a `.settings-nav` child is always present | `.settings-body` has no CSS rule anywhere in the file, and there is no child, sibling or adjacency selector involving either class. `.settings-picker`'s `margin-bottom` matches `.settings-nav`'s exactly. |
| css | The seven `1fr` → `minmax(0, 1fr)` conversions let a child overflow a clipped ancestor (rule 138 from inside) | `.about-kv dd` and `.backup-facts dd` both carry `overflow-wrap: anywhere`, so the data-dir path wraps; `.docs-body`'s two children are both `overflow-y: auto`, so their inline-axis minimum was already 0; `.add-grid`'s one consumer sits in a plain panel. Zero overflowing elements measured at every width from 320 to 1400. |
| css | The 640px block loses to `.set-row.set-row-cluster` on specificity, so the two cluster rows keep a two-column grid on a phone | The block names both selectors at equal weight and sits later in the file, so it wins on source order. Measured at 640px: both cluster rows' `.set-control` is the full row width. |
| css | Plex's "Waiting for Plex…" box and the multi-server pick list break inside the 352px track | They narrow rather than break: the waiting box gains one wrapped line, and the pick list's rows get *wider* with identical row heights. |
| css | `justify-content: flex-end` on the base `.set-control` breaks the stacked accent row | The accent row re-declares `flex-start`, and its measured geometry is byte-identical old vs new. `.accent-row` is `display: block`, so the grid and `justify-self` rules never applied there anyway. |
| css | The `.set-row` comment's "168px / 249px / 147px" measurements are invented | Two of three reproduce exactly on `dev` in the stacked layout (167.7px, 147.2px). The time-zone figure differs only because the widest IANA zone name in the harness differs from theirs. |
| css | The change is a net regression in page length | Backwards for the whole fixture: including the missing-server state the panel is shorter at every width (−967px at 1200), and `dev` also overflowed horizontally at 768px (`scrollWidth` 929 vs `clientWidth` 768) where the branch measures 768/768. The taller-row findings are real but confined to the everyday no-error case. |

**The generalizable lesson: on a layout commit, the comments are the most productive place to
look, because each one is a falsifiable claim and the author wrote them believing they were
true.** Three of the five confirmed findings are a comment asserting something measurement
denies, and the two sharpest code findings were reached *through* a comment rather than around
it. The `.set-row` block names its own bug twice — "an `<input>` to the browser's default
twenty-character width" and "which is how the Plex server row's Refresh button ended up under
its picker" — and the branch reintroduces both: the manual-address host measures 167.7px, the
browser default, and Refresh wraps under the picker at every width ≥641px. **Testing a comment's
claim is cheaper than auditing the code it sits on, and it fails in the direction nobody
expects**, because a fix that says what it fixes is a fix nobody re-drives afterwards.

**A second note, on lanes disagreeing.** The `diff` and `seam` reviewers reached opposite verdicts
on identical measured behavior — the spare-length `Segmented` committing a draft the bar had just
called unsaved. Both drove the same probe and got the same events; they disagreed on whether the
bar's Discard constitutes a promise. Convergence across independent lanes was the signal at
`394cc3a`; divergence is the signal here, and it means the question is a product decision rather
than a defect. It was reported as such and filed unproven rather than fixed, since editing a
settings panel on the strength of a split verdict is how a review introduces a bug.

## Refuted at `b33bff1` (2026-07-27, settling the four open entries in `unproven.md`)

This pass raised nothing of its own. It took the four candidates sitting in `unproven.md` and
supplied the evidence each one had named as the thing that would settle it. One died.

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| tests | The policy lab's `mirror_reach_days` fallback recreates the gap its own docstring documents, so a `default=0.0` regeneration silently stops exercising `ServerPopularityGate` while reporting green (rule 7/24) | Answered by its own settling criterion, and answered no. Regenerating vectors and baseline together the way `scripts/policy_lab_extract.py:376-381` writes them, from a play-free candidate set, takes the sweep to **exit 1 with two tests erroring** — both variants: `play_recency_days` merely emptied (55/440 vectors still exercise the gate, 385 blocked) and a faithful play-free extract (0/440 exercised, 440 blocked, which is the docstring's "440 un-checkable rows" reproduced literally). `ServerPopularityGate` fails closed at reach `0.0` (`gates.py:481-503`), so no vector can reach `condemn`, and the subject generator at `test_policy_permutations.py:593-601` dies on `StopIteration`. Not a knife-edge: sweeping the reach shows a clean cliff at the 365-day window, with 0/100/300/334/364 all giving 0 condemns and 365/400/3108 all giving 45. The committed fixture exercises the gate on 440/440. |

Worth carrying forward: the catch is real but **indirect**. The failure reads `StopIteration`,
not "the popularity gate stopped being exercised," and no test anywhere asserts a floor on how
many vectors must exercise a given gate. A reviewer wanting a legible signal could add one. The
candidate as written is refuted all the same, because the sweep does go red.

**The other three left `unproven.md` confirmed** and so are not recorded here. The FEW_WATCHERS
coverage half was folded into issue #83 as rule 140's third reader, rather than filed separately:
a probe through `build_facts` → `build_gates` → `judge_facts` with the popularity gate off, a
90-day mirror and the 365-day fallback window returned `coverage_bp = 10000` while the signal took
its full `20.00/20` pressure, and `0.00/20` once the mirror reached the identical plays. The two
settings candidates became #90 and #91. The question the `ef0278d` note above left open — whether
a save bar's Discard is a promise the other control on that row must honor — went to the operator,
who answered yes; the measurement that removed the control-track candidate's "design judgment"
objection was that releasing the track moves the control's right edge 0.00px at every width, so
the reserved 312px buys no alignment at all.

## Refuted at `65359be` (2026-07-27, reviewing the rule-140 reader sweep, PR #93)

Two lanes fired by path (`safety` on `engine/{gates,signals}.py` + `services/snapshot.py`,
`diff` on the rest); no `seam`, since no route, schema or frontend file changed. The commit
teaches the four readers of the two watcher counts to check the mirror's reach first. **The
core derivation survived both lanes untouched** — `history_shortfall`, `reach_shortfall`,
`FieldSpec.reach_span` and `_survives_more_history` were each attacked independently and held.
Everything that did survive was a comment, a fixture, or the one reader nobody swept.

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| engine-fields | `_survives_more_history` gets `Op.LTE` backwards, or mis-composes against CONDEMN's AND and PROTECT's OR | All four outcomes × both lanes walked: a lower bound already clearing a `gte` floor stays clear, one already exceeding an `lte` ceiling stays exceeded, and the two overturnable outcomes are exactly the dangerous pair. `NUMERIC_OPS` is exactly `(GTE, LTE)`, both handled; the `case _:` returns `False` (block), so even a future numeric op fails closed. |
| safety | The new blocked arm withdraws a PROTECT and widens deletion | A blocked `CustomProtectGate` is `ABSTAIN, blocked=True`, which `decide_verdict` resolves to abstain, and `GateId.CUSTOM` is absent from `verdict.DEFERRABLE_BLOCK_GATES`, so it holds a hand reap too. No path to condemn. |
| safety | `evaluate_keep`'s `evaluated=False` corrupts coverage accounting | `score()` computes coverage over `results` only; keeps are deliberately excluded. `KeepResult.evaluated` is read only by the stored-explanation writer. |
| safety | `window_days=None` reaches `reach_shortfall` in production | `CustomProtectGate` is constructed at exactly one production site (`scan_runner.py:157`) with the policy's window, simulate included via the same `build_gates`; `evaluate_custom`/`evaluate_keep` are reached only from `score()`, whose parameter is typed `int`. The `None` arm is a fail-closed default for an unforeseen caller. |
| safety | A future `added_at` makes `days_since_added` negative and disables the check | True and correct: an item that did not exist yet has no plays the mirror could have missed. |
| safety | The reach is frozen on `ScanContext` while `days_since_added` samples `utcnow()` per item (rule 104 dual clock) | Real but inert: at most one day of drift, and it raises `needed`, which blocks more. |
| safety | `evaluate_rules`' new `window_days` mis-composes the two lanes | Moot: `evaluate_rules` has no caller in `src/` at all (test-only). Production custom condemn rules go one condition at a time through `evaluate_custom` → `fields.evaluate`. |
| engine-fields | `days_unwatched` is mirror-derived and needs a `reach_span` too | `reference_instant` clamps it to `max(added_at, horizon)`, so it can never exceed the reach; every divergence *understates* dormancy, the keep direction on both a `gte` condemn and an `lte` protect rule. The branch's own LEARNINGS entry states this correctly. |
| diff | `scan_runner.build_gates`'s window differs from the one `_watch_stats` counted with | Proven rather than assumed: `scan_runner.py:600-601` builds `build_gates(movie_policy)`/`build_gates(tv_policy)`, and the counts come from the same object's `popularity_window_days()` per media type. |
| diff | `season_scan.build_season_facts` is missing an import, or uses the show's arrival date rather than the season's | `dormancy_days`/`utcnow` are imported and already used ten lines above; `season_added_at` is fed from `in_plex.added_at`, the Plex *season* row. |
| diff | `backtest.facts_as_of`'s "the guard at the top returns None otherwise" is untrue | The guard is `if item.added_at is None or item.added_at > cutoff: return None`. The claim holds exactly. |
| rule 35 | A fact builder was missed | All four `Facts(` sites in `src/` set it (`snapshot`, `season_scan`, `backtest`, and `facts_codec` via the derived `_OBS_FIELDS`); `calibration.py` builds none. A row predating the field thaws `Unknown` (rule 104), which blocks. |
| rule 21 | The new operator strings breach plain language | All plain, no em dashes, no ids or internal vocabulary. The longest (`"kept fully: could not check … only goes back 3 months"`) stacks three clauses in the `WhyPanel` keeps list, but the two-clause form it extends is pre-existing and the panel prints an explanatory note directly beneath it. |
| docs | `docs/STATUS.md`'s carried-forward "masked by the shipped 1095-day dormancy floor" is now false for the new readers | **Half-refuted, and recorded as such rather than retired.** The argument holds for every reader whose span is the popularity *window*: condemning under that floor needs dormancy ≥ 1095, dormancy is clamped to the reach, so the reach already spans any ≤1095-day window. It does NOT hold for the all-time count, whose span is the item's AGE, not a window — an item dormant 1200 days behind a 1200-day reach but added 3000 days ago has every shipped gate answering normally while an operator's `watchers_all_time gte 1` protection returns blocked. Keep-direction, so not a fail-open, but the floor masks nothing there and STATUS.md now says which half it covers. |

**The generalizable lesson, and why it is worth the space: the sweep's own scope was the
defect.** Rule 140 was written by the commit under review and names "the season roll-up" in its
own text, and the season roll-up is precisely what went unswept — because it reads no `Facts`.
Every reader the sweep *did* find was found by grepping `facts.<field>`, and the one it missed
holds the same truncated count in a local variable (`season_scan.season_watch_stats` →
`watchers_by_season` → `season_pruning._detect_conflicts`), where no grep for the fact's name
reaches it. Filed as #94. **Sweep the value, not the attribute path** — and note that the
`394cc3a` pass recorded the mirror-image version of this same lesson 22 commits and four
review passes back ("the real miss was not a query but a *reader*"). Twice now the missed site
was the one that did not look like the others, and the second time nobody reread the first.

## Refutations later found to be wrong

### The mid-binge guard, refuted at `65359be` and confirmed hours later at `23f86fc`

**Refuted as:** "The mid-binge guard is a second unswept mirror reader — declined:
`sequential_protections` reads per-user positions, not a watcher count, and is already
time-bounded by `in_progress_hold_days`."

**It is real.** Filed as #95. Driven through the real `active_progress` and
`plan_series_prune`, one viewer who finished Season 3 120 days ago under the shipped 180-day
hold, identical but for the mirror:

```
mirror reaches 400 days:  prunable=[1, 2, 3]     protected=[(4, 'a viewer is part-way through the show')]
mirror reaches  90 days:  prunable=[1, 2, 3, 4]  protected=[]
```

**What the verifier missed, which is the part worth keeping.** Both halves of the refutation
were true statements that did not support the conclusion.

- *"Reads per-user positions, not a watcher count"* — correct, and irrelevant. Rule 140 binds
  readers of a re-qualified **value**, not readers of one named field. Both queries are
  unwindowed over `watch_event`, so both inherit the horizon whatever shape they read it into.
  The refutation checked what the guard reads instead of where it comes from.
- *"Already time-bounded by `in_progress_hold_days`"* — the bound runs the wrong way.
  `in_progress_hold_days` is not a bound on the mirror; it is the span the guard *claims* to
  cover, so a mirror shallower than it is precisely the unsupported claim. A window that
  defines the claim was mistaken for a window that constrains the evidence.

**And the deeper miss: it was filed in the wrong place to begin with.** Nobody had shown the
trigger could not occur — the word in the row is "Declined", not "refuted" — so it belonged in
`unproven.md` with the evidence that would settle it. Putting a live candidate in the file the
next pass reads *in order to skip things* is how a real defect gets one line of prose and no
further looks. **"Declined", "unreachable today", and "not worth acting on" are three different
verdicts and none of them is a refutation.** A row here has to mean someone demonstrated the
trigger cannot occur; anything softer goes to `unproven.md` or to an issue.

## Refuted at `a191086` (2026-07-27, reviewing the deferrable-block fix, PR #96)

Three lanes fired by path (`safety` on `engine/{gates,verdict}.py` + `services/snapshot.py`,
`seam` on `api/routes.py`, `diff` on the rest). The commit moves what a hand reap may overrule
off a `detail.startswith("could not check")` test and onto a typed `GateResult.defers_to_owner`.
**The convergence signal fired again, on two findings**: the `safety` and `seam` lanes
independently reported the same orphaned frontend twin, and the `safety` and `diff` lanes
independently reported the same dropped codec field, each having driven it in-process rather
than reasoned about it. Fixed in `ba06511`, `5d24993`, `9df5e41`, `e663519`, `c9ea966`,
`a191086`.

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| safety | `defers_to_owner=True` on a #94-truncated comparison releases a hand reap on a count that was only a lower bound | The flagged question, and it inverts. A *resolved* kept season with no mirror rows reads `0`, not `None`, so a truncated mirror does produce spurious conflicts — but a spurious conflict is a spurious *hold*: with the deeper mirror there is no conflict at all, `guard_result` returns a plain ABSTAIN and the season is condemnable on score anyway. Releasing it by hand reaches the same end state, so the truncation only ever adds protection and the flag removes only what it added. The overstated *sentence* is #94, already filed. |
| safety | A producer sets the flag on a genuine plumbing failure | Only two `blocked=True` `SEASON_PROGRESSION` producers exist in `src/`: `season_scan.guard_result` (sets it from the conflicts' `kept_watchers`) and `facts_codec` (defaults it `False`). No other gate touches the field; the `fields.py` and `gates.py` blocked branches never set it. |
| safety | The added stored key changes a hash, breaking an interlock | Nothing hashes an explanation. `planner.manifest_hash` is over `(media_key, size_bytes)`; `policy_hash`/`scoring_hash`/`evidence_hash` are over `PolicyBody`. A full `hashlib` sweep of `src/reaper/` finds no other candidate-derived digest. |
| safety | `gates.py:550`'s "holds the reap on any detail at all" is stale now the function takes no detail | Still true, and now trivially so: `server_popularity` is absent from `DEFERRABLE_BLOCK_GATES`, so it holds whatever the detail says. The prefix stays load-bearing for the two surfaces the same comment names, which both still read it. |
| safety | The trailing defaulted field on a `frozen/slots` dataclass breaks a positional construction or a `dataclasses` helper | Appended last after `blocked`, and no site passes four or more positionally across `src/` or `tests/`. No `replace`/`astuple`/`asdict`/`fields()` over `GateResult` outside the codec, and it is never a set member or dict key, so the changed `__eq__`/`__hash__` is inert. |
| seam | The extra stored key breaks a pydantic model, a `GateOutcomeOut(**entry)` splat, or response validation | No splat exists; `_explanation_out` does `Explanation(**decoded)` and pydantic validates each entry field-by-field. The only `extra="forbid"` in `src/` is on policy bodies, which never see an explanation. Driven: the model constructs and `model_dump()` silently drops the key — which is finding 2's mechanism, not a crash. |
| seam | `routes.py:696`/`:716` classify by wording, or break on the new key | Both are `_primary_reason` printing `_detail_of(unknown[0])` verbatim. No classification, no key read. |
| seam | `_has_blocked_protections` breaks on the extra key | Presence and length only, deliberately not via `_entries`. A key inside an entry is invisible to it. **It was the correct posture all along**, which is what made `condemned.py`'s opposite one a finding. |
| seam | `_chip`'s outer `not detail.startswith("could not check")` guard wrongly skips one conflict shape | Measured `False` for both messages, so both reach the `season_progression` branch. Unchanged and still correct. |
| seam | Query-key skew lets a cached pre-flag explanation outlive the scan, so chip and reap decision disagree per cache entry | Impossible by construction: `chip` and `override_effective` are computed from the *same* decoded dict in the *same* response, from one `_decode_explanation`. The legacy disagreement that was real is a producer disagreement inside one payload, not a caching one. |
| rule 68 | `scripts/policy_lab_extract.py` must carry the new field, with a drift test | The extractor writes only `guard: {"state": …}`, and **no fixture vector reaches the arm at all**: counted across all 440, guard states are `movie/None` ×220, `season/"checked"` ×175, `season/"fired"` ×45, and zero `"unknown"`. `_policy_lab.guard_result`'s hardcoded `True` is reached only from two hand-built vectors in `test_policy_permutations.py`, which model the made-comparison arm. Latent for a future regeneration, not a present defect. |
| rule 132 | `_policy_lab.guard_result` implies coverage of the arm it does not build | The branch already disclaims it in the docstring and names the two files that pin it; both tests exist. Compliant — and the disclaimer is true by a stronger route than it claims, since no vector reaches the helper's blocked arm at all. |
| diff | A season is both `protected` and in `conflicts`, so the ordering picks the wrong arm | Impossible. `plan_series_prune` puts each on-disk season in exactly one of `prunable`/`protected`, and conflicts key on `pruned_season ∈ prunable`, so the protected-first loop can never be the tiebreaker. |
| diff | `kept_watchers == 0` is mishandled by the `is not None` spelling, or a twin uses a truthiness test | `0` correctly reads as "comparison made", and every reader spells it `is None` — `PruneConflict.message`, `_detect_conflicts`, and two tests. Grep finds no truthiness test on the field anywhere. |
| diff | The new `test_verdict_agreement` case and `CONFLICT_COMPARISON_REFUSED` duplicate the pre-existing plumbing tests | They discriminate where the old ones cannot: the pre-existing fixtures' details *start with* "could not check", so they pass under the retired wording inference, while the new ones carry the real message, which does not. `LEGACY_CONFLICT_NO_FLAG` additionally discriminates an "absent means defer" thaw. |
| diff | `snapshot._explain` can miss a blocked result | `Evaluation.could_not_be_checked` is exactly `[r for r in results if r.blocked]`. |
| diff | A stringified or numeric stored flag releases a file | `"true"`, `"false"`, `1`, `"1"`, `[]`, `{}` all fail `is True` and hold. Only relaxing the strictness would — which nothing pinned, so that became a finding rather than a refutation. |
| docs | The new STATUS.md row is too long for the file | The two rows around it are longer and the table already carries paragraph-length rows. Rule 21 binds operator-facing strings, not this file. |

**The generalizable lesson, and why it is the one worth keeping: the fix was correct and its
*reach* was the defect.** Every finding above tier 4 is the same shape — the typed flag was
right, and one of its boundaries never learned about it. The codec froze four fields out of
five, the schema served two out of three, and the frontend was left running the retired test
against the one message it never matched. None of the three is visible from the diff, because the
diff is where the flag was *added*; they are visible only by asking "who else answered this
question, and can they still?" That is now rule 142.

**A second note, on a lane's counterargument being locally sound and globally wrong.** The
`safety` and `diff` lanes both found the first-match conflict masking and split on severity:
`safety` argued the release was defensible, since reading the unresolved kept season could only
ever *remove* a conflict and never add a keep argument, so no evidence was being substituted for.
That is true, and it is not the test. The branch's own rule is that a comparison Reaper refused
to make holds a hand reap, and the operator was shown only the comparison that *had* been made —
so they could not have decided about the one that had not. Driven on shipped defaults, both
prunable seasons of a five-season show released. **When a lane argues an outcome is defensible on
the merits while the change's own stated rule says it holds, the rule wins**; the prime directive
does not have a "but it would have come out the same way" branch.

**A third, procedural, and cheap to avoid.** Two mutation probes in this run were restored with
`git checkout -- <path>` while the same file also held the fix under test, which silently reverted
the fix and produced a red suite that looked like the finding being wrong. Copy the file aside and
copy it back; `git checkout` cannot tell your edit from your mutation.

## Refuted at `8ff0a3e` (2026-07-27, reviewing the mid-binge reach fix, PR #97)

Two lanes fired by path (`safety` on `services/season_pruning.py` + `engine/policy.py`, `diff`
on `services/season_scan.py`, `PolicyEditor.tsx`, `docs/STATUS.md` and the tests). No `seam`:
the frontend change was help copy, touching no route, schema, prop or query key.

**The convergence signal fired again**, on `routes._kept_season_phrase` — both lanes
independently drove the same missing chip branch. **And the lanes contradicted each other on
the tier-1 finding**, which is the entry worth keeping: `safety` reported that the blanket hold
released a hand reap, `diff` refuted the same claim as "a blocked-abstain conflict is strictly
weaker than the PROTECT that replaces it." That reasoning inverts, and driving it settled it in
one run — for a **hand reap**, a blocked entry is *stronger* than a PROTECT, because
`decide_verdict`'s reap branch reads `blocked_holds_reap or safety_protected` and
`STRUCTURAL_GATES` carries neither this gate nor any season guard. Fixed in `8ff0a3e`.

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| safety | `plan_series_prune`'s `progress_established: bool = True` is a fail-open default reached by an unguarded caller | `season_scan.py:1133`'s offline first pass omits it, but that plan is never read for a deletion decision: `_SeriesWork.plan` has no reader after construction, and `fully_protected` only skips `_episodes_for`, which fills `season_final_episode` — skipping it drops the guard to whole-season protection, which keeps *more*. Optimism there is the safe direction. |
| safety | The in-code justification for that default is false for a configurable policy | Narrow but honest — it says "a default-configured operator". `MIN_DORMANCY` ships at 1095 (`policy.py:1255`), dormancy is clamped to the reach, so condemning requires reach ≥ 1095 > 180. An operator lowering the floor breaks the claim, but no production caller omits the argument. |
| safety | `reach_days` is optimistic when Tautulli is degraded, unreachable, or the mirror is empty | Every arm fails closed: `mirror.earliest is None` → `horizon = utcnow()` → reach 0 → not establishable, *and* the scan degrades (`snapshot.py:526-534`); a stale ingest degrades at `:547`; a future horizon gives a negative reach. `horizon` and `reach_days` come from one `ScanContext` construction and cannot disagree. |
| safety | The progress data is windowed to the popularity window, so the mirror reach is the wrong bound | The `pairs` and `progress` queries feeding `progress_by_user` / `last_watched_by_user` (`season_scan.py:797-823`) carry no `since` predicate. The mirror horizon is the only span bound, so `reach_days` is the correct comparison. |
| safety | `hold_days <= 0` makes `active_progress` and `progress_is_establishable` incoherent | They compose: `active_progress` returns every *visible* viewer, the predicate returns False, the planner holds everything. The consequence — `in_progress_hold_days = 0` disables TV season pruning outright — is the keep direction and is now stated in both the policy docstring and the editor help. |
| safety | The new reason breaks `season_pruning._because`'s closed-vocabulary parse | `_because` is only called from `_detect_conflicts`, which loops over `prunable`, which the blanket hold makes empty. **Note it becomes reachable if a future fix leaves these seasons prunable** — the `8ff0a3e` fix did not, it flags the protection instead. |
| safety | A season slips through another path when un-establishable | The new branch is last but after every early return, so it catches specials with `keep_specials` off and everything else; a `has_content == False` season produces no `SeasonJudgment` at all (`season_scan.py:1595`). |
| safety | The `in_progress_hold_days` 0 → 180 default move weakens a caller relying on 0 = hold forever | The only non-test caller of `gather` is `snapshot.scan` (`snapshot.py:665`), which passes the policy value explicitly; `_judge_series`'s only caller is `gather`, which always forwards it. No production path takes either default. |
| safety / diff | The policy simulator serves a stale preview when `in_progress_hold_days` is edited | `_EVIDENCE_REPLAYABLE_FIELDS` (`policy.py:708`) is an allow-list and omits it, so an edit moves `evidence_hash`, the replay tier is skipped, and simulate falls to tier 3 and returns the reason rather than a number. |
| safety | The blanket PROTECT distorts a count, a cap, or an auto-approval decision | `SeriesPrunePlan.auto_approvable` has zero consumers in `src/`, and with `prunable` empty there is nothing to auto-approve. `_chip`, `_primary_reason` and `_has_blocked_protections` are order- and presence-based, never count-based. |
| safety | An operator turning the `season_progression` gate off drops the new PROTECT | The guard rides in `extra_results`, merged ahead of `evaluate_all(gates, facts)` at `snapshot.py:1174`, and never goes through `build_gates`; the policy's gate list cannot remove it. |
| diff | The comment's claim that `services.snapshot.scan` is the only production caller of `gather` | True. `snapshot.py:646` is the sole production call, passing `in_progress_hold_days=tv_policy.in_progress_hold_days` at `:665`. On `_judge_series` it is imprecise (its only caller is `gather`) but the substance holds, so not a rule 7/24 defect. |
| diff | The default move silently changed `active_progress`'s expiry in existing gather tests | No. Every `TestGatherEndToEnd` case runs `_FakeTautulli(shows=[], children={})` with no Plex, so `seasons_in_plex` is empty and `progress` is `{}` regardless of `hold_days`. |
| diff | The other three `reach_days=0` gather tests also went vacuous | No — and there are four such calls, not three. `test_a_fully_protected_show_logs_the_keep_reasons` asserts on the first offline pass, which never sees the reach; the other two build no plan and produce no judgments. Only `test_a_fully_protected_short_show_is_surfaced_as_kept` was vacuous, fixed in `aafb5db`. |
| diff | `test_a_shallow_mirror_holds_every_season_of_a_prunable_show` is not mutation-proof | It is. Removing the `progress_established=` kwarg fails it, and so does hardcoding `hold_days=180` — the 190/200 straddle its docstring claims. |
| diff | `test_a_viewer_the_mirror_can_see_keeps_the_sharper_reason` pins ordering a naive implementation would also pass | No. Moving the `progress_unestablished` branch above the `seq_protected` branch fails it on exactly that assertion. |
| diff | `progress_established=False` can widen deletion or lose a conflict hold | **This refutation was WRONG on its second half** and is recorded here as the counterexample, not as a refutation — see the tier-1 finding above. The first half stands: the branch is last in `_protection_reason`, so it can only add protections. |
| diff | A hand reap on a season held for this reason is silently refused | Correct that `season_progression` is absent from `STRUCTURAL_GATES` — but the conclusion drawn from it ("unchanged, and the season was condemnable before the fix anyway") is what the tier-1 finding disproves for the conflict sub-case. |
| diff | `WhyPanel.isKeepRuleConflict` or `_chip`'s `season_progression` branch misclassify the new string | Both read `protections_unknown`; a held season is in `protections_fired`. `_primary_reason` returns the raw detail verbatim. (Re-derive after `8ff0a3e`, which now puts the blocked hold in **both** lists.) |
| diff | `docs/STATUS.md`'s rewritten row is inaccurate or claims something unwired | Accurate as written. #94 confirmed still open, #95 closed by the commit, the symbols named correctly, nothing unwired (rule 25). |
| diff | `_SeriesWork.plan` carries the un-reach-aware first-pass plan into the judgment | The field has zero readers; only `fully_protected` is consumed, and that gates a read whose loss only keeps more. |

**The lesson worth keeping, and it is a new shape.** `refuted.md` already records "a lane's
counterargument being locally sound and globally wrong." This run adds the sharper version:
**two lanes reported the same code and reached opposite verdicts, and the one that had *driven*
it was right.** The `diff` lane's refutation was a clean piece of reasoning about relative
strength — blocked-abstain vs PROTECT — that simply had the direction backwards, and no amount
of re-reading would have caught it, because both readings are plausible from the source. The
tiebreak was ten lines of script through `condemned.reap_override_verdict_decoded`. When two
lanes disagree, do not adjudicate on argument quality; run the thing.

**Procedural, confirming the note above it.** All mutation probes this run were restored by
copying a pristine file back, never `git checkout` — and it mattered, because the same two files
held the fixes under test the whole time. Also: two lanes sharing one worktree ran `uv run`
concurrently and re-synced the venv underneath each other, producing a `ModuleNotFoundError` at
a clean HEAD and one spurious red suite. Call `.venv/bin/python -m pytest` directly when lanes
share a tree.

## Refuted at `3b499f5` (2026-07-28, reviewing the truncated-mirror conflict fix, PR #101)

Three lanes fired by path (`safety` on `engine/gates.py` + `services/season_pruning.py`, `seam`
on `api/routes.py` and the chip's frontend consumers, `diff` on `engine/fields.py`,
`services/season_scan.py`, `WhyPanel.tsx`, `docs/STATUS.md` and the tests). The commit closes
#94 by giving the keep-rule conflict detector each season's shortfall beside its count.

**The convergence signal fired at full strength: all THREE lanes independently drove the same
tier-3 finding** — the `kept_watchers is None` arm building its `PruneConflict` without
`shortfall`, so a count the same call had just ruled unsupportable printed as "0 people watched
Season N". Three separate derivations, one mechanism, one fix. Fixed in `a95a9a7`, `89c197d`.

**The most valuable single artifact of this run was a mutation, not a reading.** The `diff` lane
turned `elif pruned_shortfall is not None:` into `elif pruned_shortfall is not None or
kept_shortfall is not None:` — precisely the degeneration `_detect_conflicts`'s docstring
disclaimed — and the **full 2626-test suite passed, exit 0**. The disclaimer was false *and*
nothing could tell. Both halves are now fixed.

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| safety | The change widens what gets deleted somewhere | 3000 randomized plans (2-6 seasons, random counts including `None`, random `keep_last`/`keep_first`/`keep_specials`/`protect_incomplete`, single monotone horizon) driven through `plan_series_prune` → `guard_result` on both trees: `prunable` identical in every trial, 383 season-decisions changed, **0 weaker** — no `blocked` lost, no `defers_to_owner` False→True. A separate 20,000-shape sweep says the pre-PR conflict set is a strict subset of the new one in every trial. A 64-cell exhaustive arm matrix agrees. |
| safety | `lifetime_shortfall` routes a non-`Known` age to a permissive answer (rule 93), `Absent` especially | Drove all four inputs: `Absent(age)`/`Unknown(age)` both return "this scan did not record when it was added"; `Absent(reach)`/`Unknown(reach)` both return "this scan did not record how far back your watch history goes". The guard is `not isinstance(age, Known)`, which `Absent` fails, so it never takes the permissive branch. |
| safety | `shortfall_by_season = shortfall_by_season or {}` is a rule-1 fail-open default a caller can forget | Two production callers. The offline first pass passes neither counts nor shortfalls, so every `pruned_watchers is None` and `_detect_conflicts` returns `[]`. `_judge_series` builds both in one loop over the same `item.seasons`, so the key sets cannot diverge. Deleting the kwarg from the production call fails a test, so the wiring is pinned rather than merely present. |
| safety | `guard_result`'s widened `refused` test could mask a settleable conflict or release a reap the old test held | A strict superset of dev's predicate, so it can only pick a refused conflict where dev picked a deferrable one — more refusals, never fewer. Reverting it fails 2 tests. |
| safety | The "already out-ranks" arm prints a truncated `pruned_watchers` as a fact | Lands on the safe side of the `c8b0ddc` distinction: a lower bound asserts an event that DID happen (at least N plays exist), unlike the retired "watched too recently" which asserted one that may not have. The comparison is definitive too — more history only raises the pruned count, so `pruned > kept` survives any deepening. |
| diff | The `fields.reach_shortfall` refactor is not behavior-identical on some input class | Drove the pre-PR body and the new delegation side by side over **10,368 combinations** — every `FieldSpec` in `REGISTRY` plus `None`, nine observation classes in both the reach and age slots, eight `window_days` values — comparing return value *and* exception. **0 mismatches.** The age-before-reach guard order is preserved because `lifetime_shortfall` checks `age` first. |
| diff | The age derivation is not "the same derivation `build_season_facts` records as `Facts.days_since_added`, off the same date" (rule 7/24) | Holds on both halves it states: same function (`dormancy_days`) and same date (`in_plex.added_at`, passed to `build_season_facts` as `season_added_at`). The clock differs (`now or utcnow()` vs a fresh `utcnow()`), a sub-second-to-one-day drift the comment does not claim to cover, and it makes the `Facts` lane block *more*, not less. |
| diff | `in_plex is None` leaves `watchers_by_season` and `shortfall_by_season` inconsistent | Consistent, and the stored shortfall is provably inert: on the pruned side `pruned_watchers is None` `continue`s before `pruned_shortfall` is fetched; on the kept side the `kept_watchers is None` arm `continue`s before `kept_shortfall` is fetched. Fabricating `Known(0.0)` there passes all four test files. |
| diff | `reach_days` reaching `_judge_series` can be 0, negative or stale and be read permissively | Every arm fails closed and is the keep direction: reach 0 or negative makes `reach.value >= needed` false for any positive age, so every prunable season conflicts. `humanize_days(0.0)` renders "less than a day", so the copy reads plainly and truthfully. |
| diff | The first offline `plan_series_prune` pass now needs `shortfall_by_season` too | It passes no `watchers_by_season` at all, so every `pruned_watchers` is `None` and `_detect_conflicts` returns `[]` whatever any shortfall map says. `_SeriesWork.plan` still has no reader beyond `fully_protected`. |
| diff | The `SEASONS_ADDED` fixture move made a pre-existing pipeline assertion vacuous | Dormancy is `reference_instant`-clamped to the horizon, so moving `added_at` off the 1970 placeholder leaves it unchanged; the only `Facts` field that moves is `days_since_added`, whose sole reader is the ITEM_LIFETIME arm, and no shipped policy rule carries that span. The pre-existing `condemn` assertions still discriminate. |
| seam | `_chip`'s outer `not detail.startswith("could not check")` skips the third shape entirely | Measured `False` for the new message, so it reaches the `season_progression` branch and returns the reworded chip. (Recorded at `a191086` for the first two shapes; re-verified for the third.) |
| seam | `LeftForYou`'s `/^could not check (.+?): (.+)$/` fails on the new message and blanks the block | Falls to the documented `kind: "raw"` row and renders the detail verbatim. Confirmed in jsdom. |
| seam | `GateOutcomeOut` / `api.ts` narrow the gate id or detail so the new shape does not round-trip | Both sides are plain `str`/`string` and round-trip unchanged. Only `defers_to_owner` is dropped, which is #86's mechanism, not a break. |
| seam | `_has_blocked_protections`, `_primary_reason` or `_kept_season_phrase` misclassify the new shape | The first is presence-and-shape only; the second prints `_detail_of(unknown[0])` verbatim. `_kept_season_phrase` is unreachable for a conflict, which is `GATE_ABSTAIN` and lands only in `could_not_be_checked`, while that helper reads `protections_fired[0]`. |
| seam | Rule 64: the chip reword orphaned a copy of "a season it's keeping" | Repo-wide grep across `src/`, `frontend/`, `tests/`, `docs/`, `.claude/`: zero hits; the two test assertions were updated in the same commit. No code parses `PruneConflict.message` in either tree. |
| seam | The chip's new plural "these seasons" has no antecedent on a single-season row (rule 21) | Considered and declined. The plural is the deliberate consequence of the shape it now shares: the unestablished season may be the pruned one rather than the kept one, so the old singular was wrong half the time. Both consumers read acceptably. |
| seam | `why="Reaper couldn't check who watched these seasons"` breaks `OverrideChip`'s lowercase-clause contract | "Reaper" is a proper noun and the identical shape already ships for `MATCH_UNREADABLE`. Reads correctly in both consumer surfaces. |
| seam / safety | Query-key skew lets a cached pre-fix chip outlive the scan, so chip and reap decision disagree | Same construction as `a191086`: both are computed from one `_decode_explanation` result inside one `_candidate_out`, in one response. No second key exists to drift. |
| safety | The new blocked results distort a count, a cap, or auto-approval | `SeriesPrunePlan.auto_approvable` still has zero consumers in `src/`; `plan.conflicts` has exactly one reader. Blocked → abstain → not condemned → never planned, so nothing reaches the executor or the cap math. |
| safety | Rule 72: another reader of an all-time count went unswept | Swept every site. `Facts.distinct_watchers_all_time` is display-only and no built-in gate reads it; `signals.py` records that it cannot reach the signal lane; the operator-authored field goes through `reach_shortfall` → `lifetime_shortfall`; `backtest` builds it from a complete play list. The season roll-up was the missing one and is what this PR wires. |
| safety | `PruneConflict`'s new trailing defaulted field breaks a positional construction or a `dataclasses` helper | All construction sites in `src/` and `tests/` are keyword-only; no `replace`/`astuple`/`fields()` over it; never a set member or dict key. |
| diff | `docs/STATUS.md`'s rewritten rows claim an unwired mechanism (rule 25) | Every symbol named resolves, and a repo-wide grep for `#94` finds no surviving "still open" claim anywhere. (The rows were nonetheless *incomplete* on the blanket hold, which became a finding rather than a refutation.) |

**The lesson worth keeping, and it is about what a test is for.** Every finding above tier 4 was
a statement — a message, a docstring, a field's contract — that had stopped matching the code
beside it, and in each case the code was RIGHT. `refuted.md` already records "the fix was correct
and its *reach* was the defect" from `a191086`. This run sharpens it: **the reach a fix fails to
cover is most often a sentence, not a branch**, because a branch has a test and a sentence does
not. The one place that was genuinely untested — the blanket hold — is exactly where a false
claim survived review, and the mutation proving it took ten seconds once someone thought to try.
When a docstring disclaims a behavior ("this does not degenerate into…"), that disclaimer is a
testable assertion; write the test or delete the sentence.

**Procedural, and it worked.** All three lanes ran concurrently in one worktree, called
`.venv/bin/python -m pytest` directly rather than `uv run`, and restored every mutation probe by
copying a pristine file back. No spurious red suite this run, against two in the previous two.
The `safety` lane went further and worked from `git show HEAD:` copies in a scratch directory
while other lanes mutated `src/`, which is the right move when lanes share a tree.

## Refuted at `2c3752a` (2026-07-28, reviewing the API-reference tagging, PR #102)

Three lanes fired by path (`safety` on `api/runs.py`, `seam` on every `api/*.py` plus the
schema hook, `diff` on `api/tags.py`, `main.py` and the new test). The commit gives every
operation an OpenAPI tag so `/api/docs` stops rendering as one flat scroll. **Nothing landed
above tier 4 in any lane**, which is the correct outcome for a change that adds no route, no
gate and no mutation path — worth recording, because a review of a documentation surface that
returns tier-1 findings is usually a review that went looking for them.

**The convergence signal fired at full strength again: all three lanes independently found the
same misfiling** — `runs.py`'s router-level tag sweeping the two `/api/profile` routes, which
carry the run caps, into `Reap` when the operator edits them on the Policy page. Three
derivations, one mechanism, one fix. Two further pairs converged on the hardcoded "87
operations" and on the false "every router picks one". Fixed in `81f60d3`, `52567f9`.

**The most valuable single artifact of this run was a rendering, not a reading.** The lead
candidate going in was that the vendored Scalar bundle might not read `x-tagGroups` at all,
which would have made the PR's headline claim unwired (rule 25). The `seam` lane settled it by
running the shipped `@scalar/workspace-store`'s own `createNavigation` over the real served
schema rather than reasoning about the bundle: `Start here (2) / Your library (5) / Settings
(9)`, all 87 operations nested. The same harness then turned a *different* candidate from
plausible to demonstrated — retagging one route to an undeclared name does not degrade its
section, it deletes the operation from the sidebar outright.

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| seam | Scalar does not read `x-tagGroups`, so the sidebar renders 16 flat sections and the PR's three-heading claim is unwired | Refuted by rendering. `createNavigation` on the served schema returns the three headings nested over all 87 operations. The bundle *does* contain an `x-tagGroups` stripper (`k9t` via `A9t`), and our document is `openapi: 3.1.0` so it would qualify — but the loader calls `oen(AT(f), "3.1")` and `oen` returns before `A9t` when the target is `"3.1"`. Never reached. |
| safety | A router-level `tags=` changes what a route on that router *does* — matching, dependencies, response model, or the auth guard | `route.tags` is written at `fastapi/routing.py:1022`/`1427` and read only at `fastapi/openapi/utils.py:240-241`. `tests/test_api.py` + `tests/test_guarded_transport.py`: 128 passed, exit 0. |
| safety | The middleware's path-based API-key fences are affected | `middleware.py` is not in the diff and `_api_key_allowed` keys purely on method plus path. Nine paths driven with a live key returned dev's answers. |
| diff | Adding `tags=` shifts `operationId`s and desynchronizes the hand-written `frontend/src/api.ts` | `fastapi/utils.py:95` `generate_unique_id` derives from `route.name`, `path_format` and method only. |
| diff | `/api/health` is newly advertised by the schema | It never carried `include_in_schema=False`; it was already published under `"default"`. The diff moves it from `"default"` to `"Setup"`, which is the best fit of the sixteen. |
| seam | An operation is reachable but absent from the schema, or in the schema but unreachable | 86 declared across the fourteen router modules plus `/api/health` = 87, all present. The only exclusions are `/api/openapi.json` and `/api/docs`, both deliberate; the SPA is served by `app.frontend`, not an `APIRoute`, so it never enters. |
| rule 68 | The vendored Scalar bundle is a generated asset with no generator and no drift test | `frontend/public/vendor/scalar.js` is gitignored (`.gitignore:231`) and produced by the committed `frontend/scripts/copy-scalar.mjs` from `predev`/`prebuild`. Rebuilt every run, nothing to drift from, and untouched here. |
| rule 64 | Something in `frontend/src/` reads the schema or the tag list | Nothing does. The only link is `Settings.tsx:628`'s `href="/api/docs"`, whose help copy this change does not falsify. |
| diff | `docs/STATUS.md` should have been updated in the same commit | No line there is now wrong: STATUS.md has never carried a line about the API reference, and the commit that introduced `/api/docs` (`63d8e65`) touched `docs/PLAN.md` instead. Both the `seam` and `diff` lanes checked independently. |
| diff | `ALL` can drift from `GROUPS` | It is a comprehension over `GROUPS` evaluated at import. There is no second declaration. |
| diff | Typing problems in `tags.py` | None. `get_openapi(tags=...)` takes `list[dict[str, Any]]`; `list[dict[str, object]]` is the correct LUB for `openapi_tag_groups()`. mypy exits 0. |
| diff | Rule 37: the new test fixture builds its own `Settings` and app, so it is non-hermetic | `_hermetic` (`tests/conftest.py:127`) is autouse and unconditional — it clears `Settings.model_config["env_file"]`, stubs `load_raw_env` and `catch_up_on_startup`, and resets the four throttle singletons. |
| diff | Rule 133: eight app boots leave process-global state behind | `create_app` calls the process-global `configure_logging`, but ~20 existing modules boot it the same way and the conftest scopes `_restore_logging` to tests calling those functions in their own body. `app.openapi_schema` is per-app-instance. Not introduced here. |
| diff | Rule 25: a section description names an unwired mechanism | All checked and wired: Policy's "try a change before you save it" → `/api/policy/validate` + `/simulate`; Reap's "read back what was removed" → `GET /api/runs/{run_id}`; Backup's "put one back" → the three restore routes; Logs' "set how much detail it keeps" → `PUT /api/logs/level`; Notifications' heads-up → `services/leaving_soon.py:562-586`. |
| diff | Rule 21: "Seerr" is internal vocabulary in the Services description | The app's own operator-facing word — `ServiceModal.tsx:53` labels the service "Seerr" and `ReviewQueue.tsx:467`'s tooltip says "through Seerr". |
| diff | Rule 21: the section descriptions are too long | Policy was the only two-sentence entry and both halves are short. Within "a sentence over two". |
| safety | `REVIEW`'s "the titles you keep or spare" hides `POST /api/override {decision:"reap"}`, which forces a title onto the delete list | `decision` is a required `Literal["spare","reap"]`, so misreading the blurb cannot produce an accidental force-reap, and the route's own description renders directly beneath it. |
| safety | `REAP`'s blurb omits `POST /api/runs/{id}/stop`, the intervention lever | The operation and its full docstring render inside the section the operator is already reading. |
| diff | Reordering `GROUPS` should fail the order test | It should not, and does not: `ALL` derives from `GROUPS`, so the test pins "served equals declared", which is the right contract. Serving a reversed array *does* fail it. |

**The lesson worth keeping, and it is the `ef0278d` layout lesson pointed at a different kind of
surface: on a change whose whole output is prose, the prose is the code.** Every finding this
run above the test-hygiene tier was a sentence — a section blurb promising a control its page
does not have, a docstring naming the wrong failure mode, a count that was right once. The
tagging itself was correct in 84 of 87 places on the first try, and no reading of the decorators
would have found the three that were wrong, because a decorator cannot be wrong about *where an
operator looks*. That question is answered only by opening the app beside the reference.

**And a second, sharper than it sounds: the docstring that named the guard was wrong about why
the guard exists.** `main.py` justified `tests/test_openapi_tags.py` as protection against
untidiness, when the real consequence is an endpoint that disappears from the only reference the
operator has. A test whose stated reason understates its own job is one a future author deletes
in good faith. Check what a guard actually prevents before writing down why it is there.

**Procedural, confirming the two prior runs.** All three lanes ran concurrently in one worktree
and called `.venv/bin/python -m pytest` directly rather than `uv run`; no spurious red suite. One
lane did hit a real-looking failure (`GET /api/logs` tagged `['Logs','Review']`) that was another
lane's live probe rather than the branch, and correctly said so instead of reporting it — a shared
tree makes a red suite ambiguous, so a lane that sees one should re-check against `git stash`-free
pristine copies before believing it.
