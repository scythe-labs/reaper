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

## Refutations later found to be wrong

None yet. When one lands here, record what the verifier missed — that reasoning is worth more
than the finding, because it is the failure mode of the review process itself.
